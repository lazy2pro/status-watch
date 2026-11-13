import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import monitor


class ParserTests(unittest.TestCase):
    @staticmethod
    def aws_feed(entries):
        from xml.sax.saxutils import escape
        return ("<rss><channel>" + "".join(
            f"<item><guid>https://status.aws.amazon.com/#{region}_{stamp}</guid>"
            f"<title>{escape(title)}</title><description>{body}</description></item>"
            for region, stamp, title, body in entries) + "</channel></rss>").encode()

    def test_aws_latest_resolution_suppresses_older_updates_in_any_order(self):
        entries = [
            ("ec2-us-east-1", 100, "Service disruption: API errors", "old"),
            ("ec2-us-east-1", 200, "Service is operating normally: [RESOLVED] API errors", "done"),
        ]
        source = next(s for s in monitor.SOURCES if s["service"] == "AWS")
        for ordered in (entries, entries[::-1]):
            with self.subTest(ordered=ordered), patch.object(monitor, "get", return_value=self.aws_feed(ordered)):
                self.assertEqual([], monitor.incidents(source))

    def test_aws_updates_keep_stable_id_and_separate_regions_and_topics(self):
        source = next(s for s in monitor.SOURCES if s["service"] == "AWS")
        entries = [
            ("ec2-us-east-1", 100, "Service degradation: API errors", "old"),
            ("ec2-us-east-1", 200, "Service impact: API errors", "new"),
            ("ec2-us-west-2", 300, "Service impact: API errors", "other region"),
            ("ec2-us-east-1", 300, "Service impact: Network errors", "other topic"),
        ]
        with patch.object(monitor, "get", return_value=self.aws_feed(entries)):
            result = monitor.incidents(source)
        self.assertEqual(3, len(result))
        self.assertEqual("new", result[0]["body"])
        self.assertEqual(monitor.aws_latest([{"id": "https://status.aws.amazon.com/#ec2-us-east-1_100", "name": "Service impact: API errors"}])[0]["id"], result[0]["id"])
        # A later active update must not be suppressed by an older resolution.
        entries.append(("ec2-us-east-1", 50, "[RESOLVED] API errors", "past"))
        with patch.object(monitor, "get", return_value=self.aws_feed(entries)):
            self.assertEqual(3, len(monitor.incidents(source)))

    def test_statuspage_incident_and_component(self):
        source = {"service": "GitHub", "kind": "statuspage", "url": "unused", "page": "https://example.test"}
        payload = {"status": {"indicator": "minor", "description": "Partial"}, "incidents": [{"id": "abc", "name": "API errors", "status": "investigating", "impact": "minor", "incident_updates": [{"body": "Checking", "updated_at": "2026-01-01T00:00:00Z"}]}], "components": [{"id": "api", "name": "API", "status": "operational"}, {"id": "web", "name": "Web", "status": "degraded_performance"}]}
        with patch.object(monitor, "get", return_value=json.dumps(payload).encode()):
            result = monitor.incidents(source)
        self.assertEqual({"abc", "component:web"}, {item["id"] for item in result})

    def test_rss_parser(self):
        data = b"<rss><channel><item><guid>inc-1</guid><title>Outage</title><link>https://example.test/i</link><description>Details</description></item></channel></rss>"
        result = monitor.rss_incidents(data)
        self.assertEqual("inc-1", result[0]["id"])
        self.assertEqual("Outage", result[0]["name"])

    def test_slack_empty_and_incident(self):
        source = {"service": "Slack", "kind": "slack", "url": "unused", "page": "https://slack-status.com"}
        with patch.object(monitor, "get", return_value=b'{"status":"ok","active_incidents":[]}'):
            self.assertEqual([], monitor.incidents(source))
        payload = {"status": "active", "active_incidents": [{"id": "x", "title": "Search errors", "status": "investigating", "date_updated": "today"}]}
        with patch.object(monitor, "get", return_value=json.dumps(payload).encode()):
            self.assertEqual("x", monitor.incidents(source)[0]["id"])

    def test_google_workspace_resolved_and_active(self):
        source = {"service": "Google Workspace", "kind": "google_workspace", "url": "unused", "page": "https://www.google.com/appsstatus/dashboard/"}
        payload = [
            {"id": "active", "service_name": "Gmail", "modified": "t2", "most_recent_update": {"status": "SERVICE_INFORMATION", "modified": "t2", "text": "Investigating"}},
            {"id": "done", "service_name": "Drive", "end": "t3", "most_recent_update": {"status": "AVAILABLE", "modified": "t3", "text": "Resolved"}},
            {"id": "past-open", "service_name": "Meet", "end": "t4", "most_recent_update": {"status": "SERVICE_INFORMATION", "modified": "t4", "text": "Incident report"}},
        ]
        with patch.object(monitor, "get", return_value=json.dumps(payload).encode()):
            result = monitor.incidents(source)
        self.assertEqual({"active", "past-open"}, {item["id"] for item in result})


class StateTests(unittest.TestCase):
    def test_aws_legacy_cleanup_update_recovery_and_dedupe(self):
        source = next(s for s in monitor.SOURCES if s["service"] == "AWS")
        entries = [
            ("ec2-us-east-1", 100, "Service impact: API errors", "old"),
            ("ec2-us-east-1", 200, "Service is operating normally: [RESOLVED] API errors", "done"),
        ]
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            legacy = monitor.rss_incidents(ParserTests.aws_feed(entries))
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "AWS:" + i["id"]: {"service": "AWS", "incident": i, "signature": monitor.fingerprint("AWS", i)}
                for i in legacy}, "seen": []}))
            messages = []
            with patch.object(monitor, "SOURCES", [source]), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "send", side_effect=messages.append):
                def poll(rows):
                    with patch.object(monitor, "get", return_value=ParserTests.aws_feed(rows)):
                        self.assertEqual(0, monitor.run())
                poll(entries)
                self.assertEqual([], messages)
                self.assertEqual({}, json.loads(state_path.read_text())["active"])
                poll([("ec2-us-east-1", 300, "Service impact: API errors", "reopened")])
                self.assertEqual(1, len(messages))
                self.assertIn("새 장애", messages[-1])
                poll([("ec2-us-east-1", 400, "Service impact: API errors", "changed")])
                self.assertIn("장애 내용 변경", messages[-1])
                poll([("ec2-us-east-1", 500, "[RESOLVED] API errors", "done")])
                self.assertIn("복구 확인", messages[-1])
                poll([("ec2-us-east-1", 500, "[RESOLVED] API errors", "done")])
                self.assertEqual(3, len(messages))

    def test_fingerprint_changes_on_update(self):
        a = {"id": "1", "status": "investigating", "updated_at": "t1", "body": "a"}
        b = {**a, "status": "identified", "updated_at": "t2"}
        self.assertNotEqual(monitor.fingerprint("X", a), monitor.fingerprint("X", b))

    def test_new_update_recovery_and_dedupe(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as td:
            state_path = Path(td) / "state.json"
            source = {"service": "Fixture", "kind": "statuspage", "url": "fixture", "page": "https://fixture"}
            item = {"id": "evt", "name": "Fixture outage", "status": "investigating", "impact": "minor", "updated_at": "t1", "url": "https://fixture/i", "body": "investigating"}
            good_payload = {"status": {"indicator": "minor", "description": "Partial"}, "incidents": [item], "components": []}
            messages = []
            with patch.object(monitor, "SOURCES", [source]), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "get", return_value=json.dumps(good_payload).encode()), patch.object(monitor, "send", side_effect=messages.append):
                self.assertEqual(0, monitor.run())  # first pass establishes baseline
                self.assertEqual([], messages)
                state = json.loads(state_path.read_text())
                state["initialized"] = True
                state["active"] = {}
                state_path.write_text(json.dumps(state))
                self.assertEqual(0, monitor.run())
                self.assertIn("새 장애", messages[-1])
                message_count = len(messages)
                self.assertEqual(0, monitor.run())
                self.assertEqual(message_count, len(messages))
                updated = {**item, "status": "identified", "updated_at": "t2", "body": "identified"}
                with patch.object(monitor, "get", return_value=json.dumps({"status": {"indicator": "minor"}, "incidents": [updated], "components": []}).encode()):
                    self.assertEqual(0, monitor.run())
                self.assertIn("장애 내용 변경", messages[-1])
                with patch.object(monitor, "get", return_value=json.dumps({"status": {"indicator": "none"}, "incidents": [], "components": []}).encode()):
                    self.assertEqual(0, monitor.run())
                self.assertIn("복구 확인", messages[-1])

    def test_collection_failure_does_not_create_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as td:
            state_path = Path(td) / "state.json"
            source = {"service": "Fixture", "kind": "statuspage", "url": "fixture", "page": "https://fixture"}
            old_incident = {"id": "evt", "name": "Open", "status": "investigating", "impact": "minor", "updated_at": "t1", "url": "https://fixture/i", "body": ""}
            state_path.write_text(json.dumps({"initialized": True, "active": {"Fixture:evt": {"service": "Fixture", "incident": old_incident, "signature": "old"}}, "seen": [], "collection_failure": None}))
            messages = []
            with patch.object(monitor, "SOURCES", [source]), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "get", side_effect=OSError("offline")), patch.object(monitor, "send", side_effect=messages.append):
                self.assertEqual(1, monitor.run())
            self.assertEqual(1, len(messages))
            self.assertIn("STATUS COLLECTION FAILURE", messages[0])
            self.assertEqual({"Fixture:evt"}, set(json.loads(state_path.read_text())["active"]))


if __name__ == "__main__":
    unittest.main()

class AlertFormattingTests(unittest.TestCase):
    def test_html_removed_and_unknown_impact_omitted(self):
        item = {"name": "API errors", "status": "investigating", "impact": "unknown",
                "body": "<div>First &amp; second<br>Details<script>hidden</script></div>",
                "updated_at": "2026-11-13T01:00:00Z", "url": "https://example.test"}
        message = monitor.alert("Microsoft 365", item, "INCIDENT UPDATE")
        self.assertIn("장애 내용 변경", message)
        self.assertIn("First & second\nDetails", message)
        self.assertIn("11/13 10:00", message)
        for unwanted in ("<div>", "hidden", "unknown", "investigating"):
            self.assertNotIn(unwanted, message)

    def test_recovery_does_not_repeat_old_status_or_body(self):
        message = monitor.alert("Cloudflare", {"name": "Prague status", "status": "under_maintenance",
                                 "body": "Old warning", "url": "https://example.test"}, "INCIDENT RECOVERED")
        self.assertIn("복구 확인", message)
        self.assertIn("활성 목록에서 해제", message)
        self.assertNotIn("under_maintenance", message)
        self.assertNotIn("Old warning", message)

    def test_long_notice_is_shortened_after_html_cleanup(self):
        message = monitor.alert("X", {"name": "Long", "body": "<div>" + "word " * 400 + "</div>",
                               "url": "https://example.test"}, "NEW INCIDENT")
        self.assertLess(len(message), 650)
        self.assertIn("…", message)
        self.assertIn("https://example.test", message)

class ActiveOnlyTests(unittest.TestCase):
    def test_only_active_services_are_polled_and_recovery_is_not_repeated(self):
        source = {"service": "Active", "kind": "rss", "url": "unused", "page": "unused"}
        item = {"id": "one", "name": "Outage", "status": "investigating", "body": "", "updated_at": ""}
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "Active:one": {"service": "Active", "incident": item, "signature": monitor.fingerprint("Active", item)}
            }, "seen": []}))
            with patch.dict(os.environ, {"MONITOR_ACTIVE_ONLY": "true"}), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [source, {**source, "service": "Healthy"}]), patch.object(monitor, "incidents", return_value=[]) as poll, patch.object(monitor, "send") as send:
                self.assertEqual(0, monitor.run())
                poll.assert_called_once_with(source)
                send.assert_called_once()
                poll.reset_mock()
                send.reset_mock()
                self.assertEqual(0, monitor.run())
                poll.assert_not_called()
                send.assert_not_called()

    def test_collection_failure_rechecks_all_services(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {}, "seen": [], "collection_failure": "prior"}))
            with patch.dict(os.environ, {"MONITOR_ACTIVE_ONLY": "true"}), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "incidents", return_value=[]) as poll, patch.object(monitor, "send"):
                self.assertEqual(0, monitor.run())
                self.assertEqual(10, poll.call_count)
