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
                self.assertIn("NEW INCIDENT", messages[-1])
                message_count = len(messages)
                self.assertEqual(0, monitor.run())
                self.assertEqual(message_count, len(messages))
                updated = {**item, "status": "identified", "updated_at": "t2", "body": "identified"}
                with patch.object(monitor, "get", return_value=json.dumps({"status": {"indicator": "minor"}, "incidents": [updated], "components": []}).encode()):
                    self.assertEqual(0, monitor.run())
                self.assertIn("INCIDENT UPDATE", messages[-1])
                with patch.object(monitor, "get", return_value=json.dumps({"status": {"indicator": "none"}, "incidents": [], "components": []}).encode()):
                    self.assertEqual(0, monitor.run())
                self.assertIn("INCIDENT RECOVERED", messages[-1])

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

