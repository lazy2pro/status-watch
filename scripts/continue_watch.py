"""Continue the monitor without depending solely on GitHub's cron queue."""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def request(path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("https://api.github.com/" + path, data=data,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Watch continuation rejected (HTTP {exc.code})") from None
    except Exception:
        raise RuntimeError("Watch continuation request failed") from None


def newer_pending_run(runs, current_id):
    pending = {"queued", "in_progress", "waiting", "pending", "requested"}
    return any(int(run["id"]) > current_id and run["status"] in pending for run in runs)


def continue_watch():
    # Next wall-clock five-minute boundary, regardless of previous runner delay.
    deadline = (int(time.time()) // 300 + 1) * 300
    print("Next check target: " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(deadline)), flush=True)
    while time.time() < deadline:
        time.sleep(min(30, max(0, deadline - time.time())))
    token = os.environ["WATCH_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    base = f"repos/{repo}/actions/workflows/status-watch.yml"
    for attempt in range(3):
        try:
            runs = request(base + "/runs?branch=main&per_page=10", token).get("workflow_runs", [])
            if newer_pending_run(runs, run_id):
                print("A newer queued run will continue monitoring")
                return 0
            request(base + "/dispatches", token, {"ref": "main", "inputs": {"auto_cycle": "true"}})
            print("Next five-minute check dispatched")
            return 0
        except RuntimeError as exc:
            if attempt == 2:
                print(str(exc), file=sys.stderr)
                return 1
            time.sleep(10)
    return 1


if __name__ == "__main__":
    raise SystemExit(continue_watch())
