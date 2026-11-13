#!/usr/bin/env python3
"""Poll public service status feeds and send deduplicated Telegram alerts."""
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
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
        results.append({"id": guid, "name": title, "status": "investigating", "impact": "unknown", "updated_at": "", "url": link, "body": description, "source_status": text(item.find("status")), "published_at": text(item.find("pubDate"))})
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


def microsoft_notice(item: dict) -> dict:
    # The public feed GUID changes on updates to the same service bulletin.
    identity = item.get("url", "").rstrip("/") + "|" + item.get("name", "").strip().casefold()
    key = "m365:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    source_status = str(item.get("source_status", "")).lower()
    statuses = {"servicedegradation": "service_degradation", "serviceinterruption": "service_interruption",
                "restoringservice": "restoring", "extendedrecovery": "restoring",
                "verifyingservice": "monitoring", "investigating": "investigating",
                "servicerestored": "resolved", "serviceoperational": "resolved", "resolved": "resolved"}
    stamp = item.get("updated_at", "")
    if item.get("published_at"):
        try:
            stamp = parsedate_to_datetime(item["published_at"]).isoformat()
        except (ValueError, TypeError):
            pass
    return {**item, "id": key, "status": statuses.get(source_status, item.get("status", "investigating")), "updated_at": stamp}


def microsoft_latest(items: list[dict]) -> list[dict]:
    latest = {}
    for raw in items:
        item = microsoft_notice(raw)
        try:
            stamp = datetime.fromisoformat(item["updated_at"]).timestamp()
        except (ValueError, TypeError):
            stamp = 0
        rank = (stamp, item["status"] != "resolved")
        if item["id"] not in latest or rank > latest[item["id"]][0]:
            latest[item["id"]] = (rank, item)
    return [item for _, item in latest.values()]


def incidents(source: dict) -> list[dict]:
    raw = get(source["url"])
    kind = source["kind"]
    if kind == "rss":
        found = rss_incidents(raw)
        if source["service"] == "Microsoft 365":
            return microsoft_latest(found)
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
            out[-1]["scope_regions"] = item.get("affected_locations") or item.get("impacted_locations") or []
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
            out[-1]["scope_regions"] = item.get("affected_locations") or item.get("impacted_locations") or []
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
    stable = f"{service}|{incident['id']}|{incident['status']}|{incident['updated_at']}|{incident['body']}|{issue_grade(incident)[0]}"
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def send(message: str, *, message_id: int | None = None, silent: bool = False) -> int:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    data = {"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}
    method = "editMessageText" if message_id is not None else "sendMessage"
    if message_id is not None:
        data["message_id"] = message_id
    elif silent:
        data["disable_notification"] = True
    payload = json.dumps(data).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
            if response.status != 200 or not result.get("ok"):
                raise RuntimeError("Telegram sendMessage failed")
    except urllib.error.HTTPError as exc:
        # Never log the response body: classify only known, non-sensitive errors.
        reason = "unclassified"
        try:
            description = str(json.loads(exc.read(4096)).get("description", "")).lower()
            for phrase, label in (
                ("chat not found", "chat_not_found"),
                ("message is too long", "message_too_long"),
                ("message text is empty", "empty_message"),
                ("bot was blocked", "bot_blocked"),
                ("user is deactivated", "user_deactivated"),
                ("not enough rights", "insufficient_chat_permissions"),
                ("chat_id is empty", "missing_chat_id"),
                ("chat migrated", "chat_migrated"),
                ("message to edit not found", "dashboard_message_missing"),
            ):
                if phrase in description:
                    reason = label
                    break
        except Exception:
            pass
        raise RuntimeError(f"Telegram sendMessage failed (HTTP {exc.code}; {reason})") from None
    except Exception:
        # Network exceptions can include the credential-bearing request URL.
        raise RuntimeError("Telegram sendMessage failed") from None
    print(f"Telegram {method} accepted")
    return int(result["result"]["message_id"])


class NoticeText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "div", "p", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"div", "p", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_notice(value: str) -> str:
    parser = NoticeText()
    parser.feed(unescape(str(value)))
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


STATUS_LABELS = {"investigating": "조사 중", "identified": "원인 확인 · 조치 중",
                 "monitoring": "복구 후 검증 중", "service_degradation": "성능 저하 지속",
                 "service_interruption": "서비스 장애 지속", "restoring": "복구 작업 중",
                 "recovery_unconfirmed": "복구 미확인 · 계속 추적",
                 "degraded_performance": "성능 저하", "partial_outage": "일부 장애",
                 "major_outage": "주요 기능 장애", "under_maintenance": "점검 중",
                 "service_information": "서비스 공지", "resolved": "공식 복구 확인"}


def korea_impact(service: str, item: dict) -> dict:
    """Conservative evidence filter, not a claim that excluded notices are harmless."""
    title = clean_notice(item.get("name", ""))
    body = clean_notice(item.get("body", ""))
    korean = r"(?:south korea|republic of korea|korea central|korea south|대한민국|한국|서울|부산|\bseoul\b|\bbusan\b|\bicn\b|ap-northeast-2|asia-northeast3|koreacentral|koreasouth)"
    negative = r"(?:\bnot affected\b|\bnot impacted\b|\bnot affecting\b|\bdoes not affect\b|\bnot (?:experience|experiencing|see|seeing)\b|\bunrelated\b|\bnot all\b|\bno longer\b|\bunaffected\b|\bno impact\b|\bno issues\b|\bexcluding\b|\bexcept\b|영향[^.。]*없|영향 없음|무관|제외)"
    def confirmed(basis):
        return {"confirmed": True, "basis": basis}
    def excluded(basis):
        return {"confirmed": False, "basis": basis}
    # Provider region identifiers are stronger than narrative mentions/workarounds.
    if service == "AWS":
        regions = re.findall(r"\b[a-z]{2}-(?:gov-)?[a-z]+-\d\b", str(item.get("id", "")))
        if regions:
            return confirmed("AWS 서울 리전 공지") if "ap-northeast-2" in regions else excluded("해외 AWS 리전 공지")
    structured = json.dumps(item.get("scope_regions") or [], ensure_ascii=False).lower()
    if structured != "[]":
        if re.search(korean, structured, re.I):
            return confirmed("공식 영향 지역에 한국 포함")
        if re.search(r"\bglobal\b|\bworldwide\b|all regions", structured):
            return confirmed("공식 영향 지역이 전 세계로 명시")
        if not re.search(r"global|worldwide|all regions", structured):
            return excluded("공식 영향 지역에서 한국 미확인")
    if str(item.get("id", "")).startswith("component:"):
        if re.search(korean, title, re.I) and not re.search(negative, title, re.I):
            return confirmed("한국 지역 구성요소 공지")
        if "," in title or re.search(r"\b(north america|south america|europe|asia|africa|oceania|middle east)\b", title, re.I):
            return excluded("한국으로 특정되지 않은 지역 구성요소")
    # Ignore past-status text; location mentions in old updates are not current evidence.
    body = re.split(r"previous status:|이전 상태:", body, flags=re.I)[0]
    statements = [title] + re.split(r"\n|(?<=[.!?])\s+", body)
    positive = r"affect|impact|error|outage|disrupt|degrad|unavailable|unable|issue|fail|latency|incident|maintenance|영향|장애|오류|점검|지연"
    global_scope = r"worldwide|globally|global (?:outage|incident|disruption)|all regions|all geographies|all locations|all customers|all users|전 세계|전세계|모든 지역|모든 사용자"
    for statement in statements:
        if re.search(negative, statement, re.I):
            continue
        if re.search(r"recommend|encourage|as a workaround|please|권장|권고", statement, re.I):
            continue
        has_korea = re.search(korean, statement, re.I)
        if has_korea and re.search(positive, statement, re.I):
            return confirmed("공식 공지에 한국 영향 명시")
        if re.search(global_scope, statement, re.I) and re.search(positive, statement, re.I):
            if re.search(r"limited to|confined to|only in|all (?:users|customers|regions|locations) (?:in|within|from|across) |일부 지역에 한정", statement, re.I):
                continue
            return confirmed("공식 공지의 전 세계 영향 범위")
    return excluded("한국 영향 근거 미확인")


def visible_records(state: dict) -> list[dict]:
    values = list(state.get("active", {}).values())
    if not state.get("korea_only"):
        return values
    return [v for v in values if korea_impact(v["service"], v["incident"])["confirmed"]]


def service_brief(service: str, items: list[dict]) -> list[str]:
    if not items:
        return ["상태: 현재 활성 공지 없음"]
    ranked = sorted(items, key=lambda i: issue_grade(i)[0])
    states = list(dict.fromkeys(STATUS_LABELS.get(str(i.get("status", "")).lower(), "서비스 공지 확인 중") for i in ranked))
    lines = ["상태: " + " · ".join(states[:3])]
    assessment = severity_assessment(ranked[0])
    if assessment["source"] == "assessment":
        lines.append("평가: " + assessment["basis"])
    types = []
    titles = " ".join(clean_notice(i.get("name", "")) for i in items).lower()
    for pattern, label in (
        (r"\bapi\b", "API 관련 문제"),
        (r"connectivity|connection", "접속·연결 문제"),
        (r"authentication|sign.in|login", "로그인·인증 문제"),
        (r"latency|slow|performance|degradation", "성능 저하"),
        (r"\bemail\b|\bmail\b|outlook", "메일 관련 문제"),
        (r"unavailable|inaccessible", "서비스 접근 불가"),
        (r"\berrors?\b|\bfailures?\b", "오류 발생"),
    ):
        if re.search(pattern, titles):
            types.append(label)
    if types:
        lines.append("이슈: " + " · ".join(types[:3]))
    if service == "Microsoft 365":
        body = clean_notice(ranked[0].get("body", ""))
        names = []
        for line in body.splitlines():
            if line.startswith("- "):
                name = line[2:].split(":", 1)[0].strip()
                if name and len(name) <= 60:
                    names.append(name)
        if names:
            names = list(dict.fromkeys(names))
            lines.append("관련 서비스: " + " / ".join(names[:5]) + (" 외 " + str(len(names) - 5) + "개" if len(names) > 5 else ""))
    components = [clean_notice(i.get("name", "")).removesuffix(" status") for i in ranked
                  if str(i.get("id", "")).startswith("component:")]
    if components:
        lines.append("관련 구성요소: " + " / ".join(name[:35] for name in components[:3]) + (" 외 " + str(len(components) - 3) + "개" if len(components) > 3 else ""))
    lines.append("활성 공지: " + str(len(items)) + "건 (장애·구성요소·점검 포함)")
    return lines


def service_update(service: str, items: list[dict], changes: list[str], *, correction: bool = False) -> str:
    labels = {"NEW INCIDENT": "새 장애", "INCIDENT UPDATE": "장애 내용 변경",
              "INCIDENT RECOVERED": "복구 확인", "RECOVERY UNCONFIRMED": "복구 미확인"}
    if items:
        labels["INCIDENT RECOVERED"] = "일부 항목 해제"
    icon = min(issue_grade(i) for i in items)[1] if items else "🟢"
    heading = "⚠️ 알림 정정" if correction else icon + " " + " · ".join(dict.fromkeys(labels.get(x, x) for x in changes))
    lines = [heading + " · " + service]
    if correction:
        lines.append("이전 복구 알림은 공지 ID 변경을 복구로 잘못 판단한 결과입니다.")
    lines.extend(service_brief(service, items))
    if os.getenv("KOREA_ONLY", "") == "true" and items:
        bases = list(dict.fromkeys(korea_impact(service, item)["basis"] for item in items))
        lines.append("한국 영향 근거: " + " / ".join(bases[:2]))
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%m/%d %H:%M")
    lines.append("확인: " + stamp + " (한국 시간)")
    page = next((s["page"] for s in SOURCES if s["service"] == service), "")
    if page:
        lines.append("공식 확인: " + page)
    return "\n".join(lines)


def alert(service: str, incident: dict, label: str) -> str:
    return service_update(service, [] if label == "INCIDENT RECOVERED" else [incident], [label])


def severity_assessment(item: dict, now: datetime | None = None) -> dict:
    status = str(item.get("status", "")).lower()
    impact = str(item.get("impact", "")).lower()
    def result(rank, icon, label, basis, source="vendor"):
        return {"rank": rank, "icon": icon, "label": label, "basis": basis, "source": source}
    if status == "under_maintenance":
        return result(4, "🔵", "점검", "공식 점검 상태")
    if impact == "critical" or status == "major_outage":
        return result(0, "🔴", "심각", "공식 중대 영향·장애 상태")
    if impact == "major" or status == "partial_outage":
        base = result(1, "🟠", "주요", "공식 주요 영향·일부 장애")
    elif impact == "minor" or status == "degraded_performance":
        base = result(2, "🟡", "경미", "공식 경미 영향·성능 저하")
    elif status in {"service_degradation", "service_interruption"}:
        base = result(1, "🟠", "주요", "서비스 영향이 공식 확인됨", "assessment")
    else:
        base = result(3, "❔", "등급 미제공", "심각도 판단 근거 부족")
    if status in {"resolved", "monitoring", "restoring", "recovery_unconfirmed"}:
        return base
    text_body = clean_notice(item.get("body", ""))
    text_body = re.split(r"previous status:|이전 상태:", text_body, flags=re.I)[0]
    lines = text_body.splitlines()
    bullets = [line for line in lines if line.startswith("- ")]
    core = r"exchange online|mailbox|email|authentication|sign.in|login|admin center|microsoft graph|defender|purview|database|storage"
    blocked = r"unable|cannot|unavailable|inaccessible|fail(?:ure|ures|s)?|time.?out|time out|접속 불가|사용 불가|실패"
    not_impacted = r"not impacted|not affected|unaffected|remain functional|working fine|no impact|영향 없음"
    blocking_lines = [line for line in (bullets or [clean_notice(item.get("name", "")) + " " + text_body])
                      if re.search(core, line, re.I) and re.search(blocked, line, re.I)
                      and not re.search(not_impacted, line, re.I)]
    hours = None
    try:
        first = datetime.fromisoformat(item.get("first_observed_at", "").replace("Z", "+00:00"))
        if first.tzinfo is not None:
            hours = max(0, ((now or datetime.now(timezone.utc)) - first).total_seconds() / 3600)
    except (ValueError, TypeError):
        pass
    broad = len(bullets) >= 5
    global_scope = bool(re.search(r"all users|all customers|worldwide|globally|전 세계|모든 사용자", text_body, re.I))
    if hours is not None and hours >= 2 and ((broad and len(blocking_lines) >= 2) or (global_scope and blocking_lines)):
        return result(0, "🔴", "심각", "핵심 기능 사용 불가·광범위 영향·2시간 이상 관측", "assessment")
    if status == "service_interruption" and broad and len(blocking_lines) >= 2:
        return result(0, "🔴", "심각", "다수 핵심 서비스 중단·광범위 영향", "assessment")
    if blocking_lines and global_scope and base["rank"] > 1:
        return result(1, "🟠", "주요", "핵심 기능 사용 불가·전 세계 영향", "assessment")
    return base


def issue_grade(item: dict) -> tuple[int, str, str]:
    assessment = severity_assessment(item)
    return assessment["rank"], assessment["icon"], assessment["label"]


def full_check_due(state: dict, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(str(state.get("full_checked_at", "")).replace("Z", "+00:00"))
        if last.tzinfo is None:
            return True
        # Fixed quarter-hour windows: 00, 15, 30 and 45 minutes.
        return int(now.timestamp()) // 900 != int(last.timestamp()) // 900
    except (ValueError, TypeError):
        return True


def display_time(value: str | None, *, full: bool = False) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return "미확인"
        return stamp.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M" if full else "%m/%d %H:%M")
    except (ValueError, TypeError):
        return "미확인"


def dashboard_text(state: dict) -> str:
    current = visible_records(state)
    stamp = datetime.fromisoformat(state["checked_at"]).astimezone(timezone(timedelta(hours=9)))
    title = "🇰🇷 한국 영향 확인 상태판" if state.get("korea_only") else "📊 서비스 이슈 상태판"
    lines = [title, ""]
    if state.get("collection_failure"):
        lines.extend(["⚠️ 일부 피드 수집 실패 · 이전 정보가 포함될 수 있습니다.", ""])
    services = []
    for source in SOURCES:
        items = [v["incident"] for v in current if v["service"] == source["service"]]
        ranked = sorted(items, key=lambda i: issue_grade(i)[0])
        priority = issue_grade(ranked[0])[0] if ranked else 5
        services.append((priority, source, ranked))
    for _, source, items in sorted(services, key=lambda row: row[0]):
        if not items:
            lines.append(source["service"] + " 🟢")
            continue
        _, icon, _ = issue_grade(items[0])
        # Unknown impact is a caution light, without asserting a severity grade.
        if icon == "❔":
            icon = "🟡"
        lines.append(source["service"] + " " + icon)
        brief = service_brief(source["service"], items)
        lines.extend("  " + line.removeprefix("상태: ") for line in brief[:2])
        lines.extend([source["page"], ""])
    lines.extend(["", "최근 확인: " + display_time(state["checked_at"], full=True) + " (한국 시간)"])
    return "\n".join(lines).rstrip()


def update_dashboard(state: dict, previous: dict) -> dict:
    message = dashboard_text(state)
    sig = hashlib.sha256(message.encode()).hexdigest()
    old = previous.get("dashboard") or {}
    if old.get("signature") == sig and old.get("message_id"):
        return old
    message_id = old.get("message_id")
    try:
        new_id = send(message, message_id=message_id, silent=True)
    except RuntimeError as exc:
        if message_id and "dashboard_message_missing" in str(exc):
            new_id = send(message, silent=True)
        else:
            raise
    return {"message_id": new_id, "signature": sig}


def run() -> int:
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"active": {}, "seen": []}
    except (OSError, json.JSONDecodeError) as e:
        print(f"state load failed: {e}", file=sys.stderr)
        return 2
    poll_started = datetime.now(timezone.utc)
    source_checks = dict(previous.get("source_checks", {}))
    old_active = previous.get("active", {})
    # Migrate legacy AWS update IDs without emitting one false recovery per update.
    legacy_aws = [v["incident"] for v in old_active.values()
                  if v.get("service") == "AWS" and not v["incident"]["id"].startswith("aws:")]
    if legacy_aws:
        old_active = {k: v for k, v in old_active.items() if v.get("service") != "AWS"}
        for item in aws_latest(legacy_aws):
            old_active[f"AWS:{item['id']}"] = {"service": "AWS", "incident": item, "signature": fingerprint("AWS", item)}
    ms_correction = False
    migrated = {}
    for key, old in old_active.items():
        if old.get("service") == "Microsoft 365" and not old["incident"]["id"].startswith("m365:"):
            item = microsoft_notice(old["incident"])
            migrated["Microsoft 365:" + item["id"]] = {**old, "incident": item, "signature": fingerprint("Microsoft 365", item)}
            ms_correction = ms_correction or not old["incident"]["id"].startswith("m365:")
        else:
            migrated[key] = old
    old_active = migrated
    korea_only = os.getenv("KOREA_ONLY", "") == "true"
    def relevant(service, item):
        return not korea_only or korea_impact(service, item)["confirmed"]
    active_only = os.getenv("MONITOR_ACTIVE_ONLY", "") == "true"
    if active_only and os.getenv("MONITOR_AUTO_CYCLE", "") == "true":
        active_only = not full_check_due(previous, poll_started)
    sources = SOURCES
    if active_only and not previous.get("collection_failure"):
        active_services = {item.get("service") for item in old_active.values() if relevant(item["service"], item["incident"])}
        sources = [source for source in SOURCES if source["service"] in active_services]
        if not sources:
            print("No active incidents; waiting for the next full check")
            return 0
    old_seen = set(previous.get("seen", []))
    print("Poll mode: " + ("active-only" if active_only else "full") + f"; {len(sources)} services")
    selected_services = {source["service"] for source in sources}
    current: dict[str, dict] = {key: value for key, value in old_active.items() if value.get("service") not in selected_services}
    failures = []
    transitions = {}
    for source in sources:
        service = source["service"]
        try:
            found = incidents(source)
            source_checks[service] = datetime.now(timezone.utc).isoformat()
            service_ids = set()
            resolved_ids = {f"{service}:{i['id']}" for i in found if service == "Microsoft 365" and i.get("status") == "resolved"}
            for item in found:
                incident_id = f"{service}:{item['id']}"
                if incident_id in resolved_ids:
                    continue
                service_ids.add(incident_id)
                old = old_active.get(incident_id)
                first = (old or {}).get("incident", {}).get("first_observed_at")
                if not first:
                    first = previous.get("checked_at") if old else datetime.now(timezone.utc).isoformat()
                first_basis = (old or {}).get("incident", {}).get("first_observed_basis", "monitor observation")
                item = {**item, "first_observed_at": first or datetime.now(timezone.utc).isoformat(),
                        "first_observed_basis": first_basis}
                item["severity_assessment"] = severity_assessment(item)
                sig = fingerprint(service, item)
                current[incident_id] = {"signature": sig, "incident": item, "service": service}
                old = old_active.get(incident_id)
                if relevant(service, item) and (old is None or not relevant(service, old["incident"])) and previous.get("initialized"):
                    transitions.setdefault(service, []).append("NEW INCIDENT")
                elif relevant(service, item) and old is not None and old.get("signature") != sig:
                    transitions.setdefault(service, []).append("INCIDENT UPDATE")
            for incident_id, old in old_active.items():
                if old.get("service") == service and incident_id not in service_ids:
                    if service == "Microsoft 365" and incident_id not in resolved_ids:
                        item = {**old["incident"], "status": "recovery_unconfirmed"}
                        current[incident_id] = {**old, "incident": item, "signature": fingerprint(service, item)}
                        if relevant(service, old["incident"]) and old["incident"].get("status") != "recovery_unconfirmed":
                            transitions.setdefault(service, []).append("RECOVERY UNCONFIRMED")
                    elif relevant(service, old["incident"]):
                        transitions.setdefault(service, []).append("INCIDENT RECOVERED")
            print(f"OK {service}: {len(found)} active notices")
        except Exception as e:  # keep collecting other independent feeds
            failures.append(f"{service}: {type(e).__name__}: {e}")
            print(f"FAIL {service}: {type(e).__name__}: {e}", file=sys.stderr)

    # Never infer recovery while the corresponding service feed is unavailable.
    failed_services = {item.split(":", 1)[0] for item in failures}
    for incident_id, old in old_active.items():
        if old.get("service") in failed_services and incident_id not in current:
            current[incident_id] = old

    if ms_correction and any(v["service"] == "Microsoft 365" and relevant(v["service"], v["incident"]) for v in current.values()):
        transitions.setdefault("Microsoft 365", []).append("INCIDENT UPDATE")
    notifications = [
        service_update(service, [v["incident"] for v in current.values() if v["service"] == service and relevant(service, v["incident"])],
                       changes, correction=ms_correction and service == "Microsoft 365")
        for service, changes in transitions.items()
    ]
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
    new_state["source_checks"] = source_checks
    new_state["full_checked_at"] = previous.get("full_checked_at")
    if not active_only and not failures:
        new_state["full_checked_at"] = poll_started.isoformat()
    new_state["korea_only"] = korea_only
    if korea_only:
        for record in current.values():
            record["korea_impact"] = korea_impact(record["service"], record["incident"])
        included = sum(v["korea_impact"]["confirmed"] for v in current.values())
        print(f"Korea scope: {included} included; {len(current) - included} excluded")
    if previous.get("dashboard"):
        new_state["dashboard"] = previous["dashboard"]
    if os.getenv("TELEGRAM_DASHBOARD", "") == "true":
        try:
            new_state["dashboard"] = update_dashboard(new_state, previous)
        except Exception as exc:
            print(f"Telegram dashboard failed: {exc}", file=sys.stderr)
            return 3
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)
    if failures:
        # Partial feed failure remains visible as a failed workflow run.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
