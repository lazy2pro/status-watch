#!/usr/bin/env python3
"""Poll public service status feeds and send deduplicated Telegram alerts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state.json"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
USER_AGENT = "PublicStatusWatch/1.0 (GitHub Actions; contact repository owner)"

# Every configured URL is a vendor-operated public status endpoint/feed. M365
# and Azure public channels intentionally have narrower coverage than tenant APIs.
SOURCES = [
    {"service": "Microsoft 365", "kind": "rss", "url": "https://status.cloud.microsoft/api/feed/mac", "page": "https://status.cloud.microsoft/m365"},
    {"service": "Azure", "kind": "rss", "url": "https://azure.status.microsoft/en-us/status/feed/", "page": "https://azure.status.microsoft/"},
    {"service": "Google Workspace", "kind": "google_workspace", "url": "https://www.google.com/appsstatus/dashboard/incidents.json", "page": "https://www.google.com/appsstatus/dashboard/"},
    {"service": "Google Cloud", "kind": "json_array", "url": "https://status.cloud.google.com/incidents.json", "page": "https://status.cloud.google.com/"},
    {"service": "AWS", "kind": "rss", "url": "https://status.aws.amazon.com/rss/all.rss", "page": "https://health.aws.amazon.com/health/status"},
    {"service": "Cloudflare", "kind": "statuspage", "url": "https://www.cloudflarestatus.com/api/v2/summary.json", "page": "https://www.cloudflarestatus.com/"},
    {"service": "GitHub", "kind": "statuspage", "url": "https://www.githubstatus.com/api/v2/summary.json", "page": "https://www.githubstatus.com/"},
    {"service": "OpenAI", "kind": "statuspage", "url": "https://status.openai.com/api/v2/summary.json", "page": "https://status.openai.com/"},
    {"service": "Slack", "kind": "slack", "url": "https://slack-status.com/api/v2.0.0/current", "page": "https://slack-status.com/"},
    {"service": "Zoom", "kind": "statuspage", "url": "https://status.zoom.us/api/v2/summary.json", "page": "https://status.zoom.us/"},
]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read(4_000_000)


def text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def rss_incidents(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    results = []
    for item in items:
        title = text(item.find("title")) or text(item.find("{http://www.w3.org/2005/Atom}title"))
        link = text(item.find("link")) or text(item.find("{http://www.w3.org/2005/Atom}link"))
        if not link:
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.attrib.get("href", "") if link_el is not None else ""
        guid = text(item.find("guid")) or text(item.find("{http://www.w3.org/2005/Atom}id")) or link or title
        description = text(item.find("description")) or text(item.find("{http://www.w3.org/2005/Atom}summary"))
        results.append({"id": guid, "name": title, "status": "investigating", "impact": "unknown", "updated_at": "", "url": link, "body": description})
    return results


def aws_latest(items: list[dict]) -> list[dict]:
    """AWS GUIDs identify updates, not events; keep the latest per region/topic."""
    latest = {}
    for item in items:
        match = re.fullmatch(r"https?://status\.aws\.amazon\.com/#(.+)_(\d+)", item["id"])
        if not match:
            # Do not silently merge an unfamiliar GUID format.
            key, stamp = item["id"], 0
        else:
            topic = re.sub(r"^(?:Service is operating normally|Service impact|Service disruption|Service degradation|Informational message):\s*", "", item["name"], flags=re.I)
            topic = re.sub(r"\[RESOLVED\]\s*", "", topic, flags=re.I).strip().casefold()
            key, stamp = f"aws:{match[1]}:{topic}", int(match[2])
        candidate = {**item, "id": key}
        resolved = "[resolved]" in item["name"].casefold() or item["name"].casefold().startswith("service is operating normally:")
        # On equal timestamps, an explicit resolution wins regardless of order.
        rank = (stamp, resolved)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, candidate)
    return [item for (_, resolved), item in latest.values() if not resolved]


def incidents(source: dict) -> list[dict]:
    raw = get(source["url"])
    kind = source["kind"]
    if kind == "rss":
        found = rss_incidents(raw)
        return aws_latest(found) if source["service"] == "AWS" else found
    data = json.loads(raw)
    if kind == "json_array":
        if not isinstance(data, list):
            raise ValueError("Expected JSON incident array")
        out = []
        for item in data:
            state = str(item.get("status", item.get("state", ""))).lower()
            if state in {"available", "resolved", "closed", "completed", "false_positive"} or item.get("end"):
                continue
            out.append({"id": str(item.get("id") or item.get("number") or item.get("uri") or json.dumps(item, sort_keys=True)), "name": str(item.get("summary") or item.get("title") or item.get("service_name") or "Service incident"), "status": state or "investigating", "impact": str(item.get("severity") or item.get("impact") or "unknown"), "updated_at": str(item.get("modified") or item.get("updated") or item.get("created") or ""), "url": str(item.get("uri") or source["page"]), "body": str(item.get("external_desc") or item.get("description") or "")})
        return out
    if kind == "google_workspace":
        if not isinstance(data, list):
            raise ValueError("Expected Google Workspace incident array")
        out = []
        for item in data:
            updates = item.get("updates") or []
            latest = item.get("most_recent_update") or (updates[0] if updates else {})
            state = str(latest.get("status") or item.get("status_impact") or "investigating").lower()
            # Only the incident's latest update decides active vs recovered;
            # older incidents can have an end timestamp despite still having
            # a SERVICE_INFORMATION (non-resolved) latest status.
            if state == "available":
                continue
            out.append({"id": str(item.get("id") or item.get("number")), "name": str(item.get("service_name") or "Google Workspace incident"), "status": state, "impact": str(item.get("severity") or item.get("status_impact") or "unknown"), "updated_at": str(latest.get("modified") or item.get("modified") or ""), "url": "https://www.google.com/appsstatus/dashboard/" + str(item.get("uri") or ""), "body": str(latest.get("text") or item.get("external_desc") or "")})
        return out
    if kind == "slack":
        active = data.get("active_incidents", [])
        return [{"id": str(i.get("id") or i.get("date_created") or json.dumps(i, sort_keys=True)), "name": str(i.get("title") or i.get("name") or "Slack incident"), "status": str(i.get("status") or data.get("status") or "investigating"), "impact": str(i.get("type") or i.get("impact") or "unknown"), "updated_at": str(i.get("date_updated") or ""), "url": source["page"], "body": str(i.get(" msg") or i.get("message") or i.get("title") or "") } for i in active]
    if kind == "statuspage":
        if not isinstance(data, dict) or "status" not in data:
            raise ValueError("Unexpected Statuspage API response")
        page_status = data.get("status", {})
        out = []
        for i in data.get("incidents", []):
            updates = i.get("incident_updates") or []
            latest = max(updates, key=lambda u: u.get("created_at", "")) if updates else {}
            status = str(i.get("status") or latest.get("status") or "investigating")
            out.append({"id": str(i.get("id", i.get("name", "unknown"))), "name": str(i.get("name") or "Service incident"), "status": status, "impact": str(i.get("impact") or page_status.get("indicator") or "unknown"), "updated_at": str(i.get("updated_at") or latest.get("updated_at") or ""), "url": str(i.get("shortlink") or source["page"]), "body": str(latest.get("body") or "")})
        # Component-only degradation is still alerted, even without an incident.
        for c in data.get("components", []):
            status = str(c.get("status", "operational"))
            if status != "operational":
                out.append({"id": "component:" + str(c.get("id") or c.get("name")), "name": str(c.get("name") or "Component") + " status", "status": status, "impact": status, "updated_at": str(c.get("updated_at") or ""), "url": source["page"], "body": str(c.get("description") or "")})
        if not out and page_status.get("indicator", "none") != "none":
            out.append({"id": "overall-status", "name": "Overall status", "status": str(page_status.get("indicator")), "impact": str(page_status.get("indicator")), "updated_at": "", "url": source["page"], "body": str(page_status.get("description") or "")})
        return out
    raise ValueError(f"Unknown source kind: {kind}")


def fingerprint(service: str, incident: dict) -> str:
    stable = f"{service}|{incident['id']}|{incident['status']}|{incident['updated_at']}|{incident['body']}"
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def send(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    payload = json.dumps({"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
            if response.status != 200 or not result.get("ok"):
                raise RuntimeError("Telegram sendMessage failed")
    except Exception:
        # Network exceptions can include the credential-bearing request URL.
        raise RuntimeError("Telegram sendMessage failed") from None
    print("Telegram sendMessage accepted")


def alert(service: str, incident: dict, label: str) -> str:
    body = incident.get("body", "")[:900]
    return f"{label}\n서비스: {service}\n장애: {incident.get('name','')}\n상태: {incident.get('status','')} / 영향: {incident.get('impact','')}\n업데이트: {incident.get('updated_at','미제공')}\n{body + chr(10) if body else ''}{incident.get('url','')}"


def run() -> int:
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"active": {}, "seen": []}
    except (OSError, json.JSONDecodeError) as e:
        print(f"state load failed: {e}", file=sys.stderr)
        return 2
    old_active = previous.get("active", {})
    # Migrate legacy AWS update IDs without emitting one false recovery per update.
    legacy_aws = [v["incident"] for v in old_active.values()
                  if v.get("service") == "AWS" and not v["incident"]["id"].startswith("aws:")]
    if legacy_aws:
        old_active = {k: v for k, v in old_active.items() if v.get("service") != "AWS"}
        for item in aws_latest(legacy_aws):
            old_active[f"AWS:{item['id']}"] = {"service": "AWS", "incident": item, "signature": fingerprint("AWS", item)}
    old_seen = set(previous.get("seen", []))
    current: dict[str, dict] = {}
    failures = []
    transition_messages = []
    for source in SOURCES:
        service = source["service"]
        try:
            found = incidents(source)
            service_ids = set()
            for item in found:
                incident_id = f"{service}:{item['id']}"
                service_ids.add(incident_id)
                sig = fingerprint(service, item)
                current[incident_id] = {"signature": sig, "incident": item, "service": service}
                old = old_active.get(incident_id)
                if old is None:
                    # A previously active incident missing from the last successful
                    # snapshot is a reopening, even if its last signature was seen.
                    if previous.get("initialized") and incident_id in old_active:
                        transition_messages.append(alert(service, item, "INCIDENT REOPENED"))
                    elif previous.get("initialized"):
                        transition_messages.append(alert(service, item, "NEW INCIDENT"))
                elif old.get("signature") != sig:
                    transition_messages.append(alert(service, item, "INCIDENT UPDATE"))
            for incident_id, old in old_active.items():
                if old.get("service") == service and incident_id not in service_ids:
                    transition_messages.append(alert(service, old.get("incident", {}), "INCIDENT RECOVERED"))
            print(f"OK {service}: {len(found)} active notices")
        except Exception as e:  # keep collecting other independent feeds
            failures.append(f"{service}: {type(e).__name__}: {e}")
            print(f"FAIL {service}: {type(e).__name__}: {e}", file=sys.stderr)

    # Never infer recovery while the corresponding service feed is unavailable.
    failed_services = {item.split(":", 1)[0] for item in failures}
    for incident_id, old in old_active.items():
        if old.get("service") in failed_services and incident_id not in current:
            current[incident_id] = old

    notifications = list(transition_messages)
    if failures:
        # Fingerprint failures so the same outage is emitted once, and a changed
        # collection failure summary can be surfaced as an update.
        fail_key = hashlib.sha256("\n".join(sorted(failures)).encode()).hexdigest()
        if fail_key not in old_seen:
            notifications.append("STATUS COLLECTION FAILURE\n" + "\n".join(failures))
        old_fail = previous.get("collection_failure")
        if old_fail and old_fail != fail_key and fail_key not in old_seen:
            notifications[-1] = "STATUS COLLECTION FAILURE UPDATE\n" + "\n".join(failures)
        collection_failure = fail_key
    else:
        collection_failure = None
        if previous.get("collection_failure"):
            notifications.append("STATUS COLLECTION RECOVERED\nAll public feeds are reachable again.")

    # Send sequentially; state is only committed after successful notifications.
    for message in notifications:
        try:
            send(message)
        except Exception as e:
            print(f"Telegram notification failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
    seen = list(old_seen)
    seen.extend(v["signature"] for v in current.values())
    if collection_failure:
        seen.append(collection_failure)
    # Bounded dedupe window; active incident signatures are always retained.
    active_signatures = {v["signature"] for v in current.values()}
    seen = list(dict.fromkeys(x for x in seen if x in active_signatures or x == collection_failure))[-500:]
    new_state = {"initialized": True, "checked_at": datetime.now(timezone.utc).isoformat(), "active": current, "seen": seen, "collection_failure": collection_failure}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)
    if failures:
        # Partial feed failure remains visible as a failed workflow run.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
