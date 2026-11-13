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
                self.assertEqual({}, json.loads(state_path.read_text(encoding="utf-8"))["active"])
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
                state = json.loads(state_path.read_text(encoding="utf-8"))
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
            self.assertEqual({"Fixture:evt"}, set(json.loads(state_path.read_text(encoding="utf-8"))["active"]))


if __name__ == "__main__":
    unittest.main()

class AlertFormattingTests(unittest.TestCase):
    def test_notice_is_summarized_not_forwarded(self):
        item = {"id": "x", "name": "Raw vendor headline", "status": "service_degradation",
                "body": "<div>User impact: raw English prose</div><div>- Exchange Online: mail errors</div><div>- Microsoft Teams: calendar errors</div>"}
        message = monitor.alert("Microsoft 365", item, "INCIDENT UPDATE")
        self.assertIn("성능 저하 지속", message)
        self.assertIn("Exchange Online / Microsoft Teams", message)
        for unwanted in ("<div>", "raw English prose", "Raw vendor headline", "mail errors"):
            self.assertNotIn(unwanted, message)

    def test_recovery_does_not_repeat_old_status_or_body(self):
        message = monitor.alert("Cloudflare", {"name": "Prague status", "status": "under_maintenance",
                                "body": "Old warning"}, "INCIDENT RECOVERED")
        self.assertIn("복구 확인", message)
        self.assertNotIn("under_maintenance", message)
        self.assertNotIn("Old warning", message)

    def test_long_notice_is_omitted(self):
        message = monitor.alert("X", {"name": "Long", "body": "<div>" + "word " * 400 + "</div>"},
                                "NEW INCIDENT")
        self.assertLess(len(message), 400)
        self.assertNotIn("word", message)


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

class TelegramDashboardTests(unittest.TestCase):
    def test_dashboard_creates_once_then_edits_same_message(self):
        state = {"checked_at": "2026-11-13T01:00:00+00:00", "active": {}, "collection_failure": None}
        with patch.object(monitor, "send", return_value=123) as send:
            dashboard = monitor.update_dashboard(state, {})
            send.assert_called_once()
            self.assertTrue(send.call_args.kwargs["silent"])
            self.assertIsNone(send.call_args.kwargs["message_id"])
            send.reset_mock()
            self.assertEqual(dashboard, monitor.update_dashboard(state, {"dashboard": dashboard}))
            send.assert_not_called()
            changed = {**state, "checked_at": "2026-11-13T01:05:00+00:00"}
            monitor.update_dashboard(changed, {"dashboard": dashboard})
            self.assertEqual(123, send.call_args.kwargs["message_id"])

    def test_deleted_dashboard_is_recreated_silently(self):
        state = {"checked_at": "2026-11-13T01:00:00+00:00", "active": {}}
        with patch.object(monitor, "send", side_effect=[RuntimeError("dashboard_message_missing"), 456]) as send:
            result = monitor.update_dashboard(state, {"dashboard": {"message_id": 123}})
            self.assertEqual(456, result["message_id"])
            self.assertTrue(send.call_args.kwargs["silent"])

    def test_dashboard_is_bounded_plain_text(self):
        active = {}
        for source in monitor.SOURCES:
            for i in range(100):
                active[source["service"] + str(i)] = {"service": source["service"],
                    "incident": {"id": str(i), "name": "<div>" + "Long name " * 30 + "</div>"}}
        result = monitor.dashboard_text({"active": active, "checked_at": "2026-11-13T01:00:00+00:00"})
        self.assertLess(len(result.encode("utf-16-le")) // 2, 4096)
        self.assertNotIn("<div>", result)
        self.assertIn("10:00", result)

class DashboardGradeTests(unittest.TestCase):
    def test_explicit_grades_and_unknown(self):
        cases = [
            ({"impact": "critical", "status": "investigating"}, "심각"),
            ({"status": "major_outage"}, "심각"),
            ({"impact": "major", "status": "monitoring"}, "주요"),
            ({"status": "partial_outage"}, "주요"),
            ({"impact": "minor"}, "경미"),
            ({"status": "degraded_performance"}, "경미"),
            ({"status": "under_maintenance"}, "점검"),
            ({"impact": "unknown", "status": "investigating"}, "등급 미제공"),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(expected, monitor.issue_grade(item)[2])

    def test_services_and_items_sorted_by_highest_grade(self):
        active = {
            "aws": {"service": "AWS", "incident": {"id": "one", "name": "Unknown", "impact": "unknown"}},
            "cf1": {"service": "Cloudflare", "incident": {"id": "component:one", "name": "Critical API", "status": "major_outage"}},
            "cf2": {"service": "Cloudflare", "incident": {"id": "two", "name": "Minor API", "impact": "minor"}},
        }
        text = monitor.dashboard_text({"active": active, "checked_at": "2026-11-13T01:00:00+00:00"})
        self.assertLess(text.index("Cloudflare"), text.index("AWS"))
        self.assertIn("Cloudflare 🔴", text)
        self.assertNotIn("Minor API", text)
        self.assertIn("Cloudflare 🔴", text)
        self.assertNotIn("🟡 경미", text)
        self.assertIn("AWS 🟡", text)
        self.assertIn("Azure 🟢", text)

class MicrosoftRecoveryTests(unittest.TestCase):
    def test_old_resolved_update_cannot_hide_new_active_update(self):
        old = {"id": "old", "name": "Microsoft Admin Center", "url": "https://status.cloud.microsoft/",
               "status": "investigating", "source_status": "ServiceRestored", "updated_at": "2026-11-13T01:00:00+00:00"}
        new = {**old, "id": "new", "source_status": "ServiceDegradation", "updated_at": "2026-11-13T02:00:00+00:00"}
        for items in ([old, new], [new, old]):
            self.assertEqual("service_degradation", monitor.microsoft_latest(items)[0]["status"])

    source = {"service": "Microsoft 365", "kind": "rss", "url": "fixture", "page": "https://status.cloud.microsoft/m365"}

    @staticmethod
    def feed(guid, status="ServiceDegradation", body="Still affected"):
        from xml.sax.saxutils import escape
        return (f"<rss><channel><item><guid>{guid}</guid><title>Microsoft Admin Center</title>"
                f"<link>https://status.cloud.microsoft/</link><status>{status}</status>"
                f"<description>{escape(body)}</description></item></channel></rss>").encode()

    def test_guid_rotation_is_not_new_incident_or_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            messages = []
            with patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [self.source]), patch.object(monitor, "send", side_effect=messages.append):
                with patch.object(monitor, "get", return_value=self.feed("old-guid")):
                    self.assertEqual(0, monitor.run())
                with patch.object(monitor, "get", return_value=self.feed("new-guid")):
                    self.assertEqual(0, monitor.run())
                self.assertEqual([], messages)
                with patch.object(monitor, "get", return_value=self.feed("third-guid", body="Changed update")):
                    self.assertEqual(0, monitor.run())
                self.assertEqual(1, len(messages))
                self.assertIn("장애 내용 변경", messages[0])
                self.assertNotIn("복구 확인", messages[0])
                self.assertEqual("service_degradation", next(iter(json.loads(state_path.read_text(encoding="utf-8"))["active"].values()))["incident"]["status"])

    def test_missing_notice_stays_tracked_until_explicit_restoration(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            messages = []
            with patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [self.source]), patch.object(monitor, "send", side_effect=messages.append):
                def poll(raw):
                    with patch.object(monitor, "get", return_value=raw):
                        self.assertEqual(0, monitor.run())
                poll(self.feed("one"))
                poll(b"<rss><channel/></rss>")
                self.assertIn("복구 미확인", messages[-1])
                self.assertEqual(1, len(json.loads(state_path.read_text(encoding="utf-8"))["active"]))
                count = len(messages)
                poll(b"<rss><channel/></rss>")
                self.assertEqual(count, len(messages))
                poll(self.feed("new-guid", "ServiceRestored"))
                self.assertIn("복구 확인", messages[-1])
                self.assertEqual({}, json.loads(state_path.read_text(encoding="utf-8"))["active"])
                poll(self.feed("new-guid", "ServiceRestored"))
                self.assertEqual(count + 1, len(messages))

    def test_legacy_ids_migrate_without_false_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            item = monitor.rss_incidents(self.feed("old"))[0]
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "Microsoft 365:old": {"service": "Microsoft 365", "incident": item,
                                    "signature": monitor.fingerprint("Microsoft 365", item)}
            }, "seen": []}))
            messages = []
            with patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [self.source]), patch.object(monitor, "send", side_effect=messages.append), patch.object(monitor, "get", return_value=self.feed("new")):
                self.assertEqual(0, monitor.run())
                self.assertEqual(1, len(messages))
                self.assertIn("알림 정정", messages[0])
                self.assertNotIn("복구 확인", messages[0])
                self.assertEqual(0, monitor.run())
                self.assertEqual(1, len(messages))

    def test_service_notifications_are_grouped(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {}, "seen": []}))
            items = [{"id": str(i), "name": "Raw headline", "status": "investigating", "body": "raw vendor prose", "updated_at": ""} for i in range(5)]
            with patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [{**self.source, "service": "Fixture"}]), patch.object(monitor, "incidents", return_value=items), patch.object(monitor, "send") as send:
                self.assertEqual(0, monitor.run())
                send.assert_called_once()
                self.assertIn("활성 공지: 5건", send.call_args.args[0])
                self.assertNotIn("raw vendor prose", send.call_args.args[0])

class KoreaImpactTests(unittest.TestCase):
    def test_active_only_checks_korean_service_and_retains_other_snapshots(self):
        source = {"service": "AWS", "kind": "rss", "url": "fixture", "page": "https://health.aws.amazon.com"}
        kr = {"id": "aws:ec2-ap-northeast-2:errors", "name": "API errors", "body": "", "status": "investigating", "updated_at": ""}
        foreign = {"id": "component:prg", "name": "Prague, Czechia", "body": "", "status": "major_outage", "updated_at": ""}
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "AWS:" + kr["id"]: {"service": "AWS", "incident": kr, "signature": monitor.fingerprint("AWS", kr)},
                "Cloudflare:component:prg": {"service": "Cloudflare", "incident": foreign, "signature": monitor.fingerprint("Cloudflare", foreign)}
            }, "seen": []}))
            with patch.dict(os.environ, {"KOREA_ONLY": "true", "MONITOR_ACTIVE_ONLY": "true"}), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [source, {**source, "service": "Cloudflare"}]), patch.object(monitor, "incidents", return_value=[kr]) as poll, patch.object(monitor, "send") as send:
                self.assertEqual(0, monitor.run())
                poll.assert_called_once_with(source)
                send.assert_not_called()
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIn("Cloudflare:component:prg", state["active"])
                self.assertEqual(1, len(monitor.visible_records(state)))

    def test_confirmed_scope_and_exclusions(self):
        cases = [
            ("AWS", {"id": "aws:ec2-ap-northeast-2:errors", "name": "API errors"}, True),
            ("AWS", {"id": "aws:ec2-ap-northeast-1:errors", "body": "All users affected worldwide"}, False),
            ("Cloudflare", {"id": "component:icn", "name": "Seoul, South Korea - (ICN) status"}, True),
            ("Cloudflare", {"id": "component:prg", "name": "Prague, Czechia - (PRG) status", "body": "Global outage"}, False),
            ("Cloudflare", {"id": "component:asia", "name": "Asia status"}, False),
            ("Google Cloud", {"scope_regions": [{"id": "asia-northeast3"}]}, True),
            ("Google Cloud", {"scope_regions": [{"id": "us-east1"}], "body": "All users affected"}, False),
            ("Azure", {"name": "Service degradation in Korea Central"}, True),
            ("OpenAI", {"body": "All users are experiencing elevated errors."}, True),
            ("Microsoft 365", {"body": "All users may be unable to access email."}, True),
            ("GitHub", {"body": "We are investigating a global outage."}, True),
            ("Slack", {"body": "Some users are experiencing errors."}, False),
            ("Slack", {"body": "We recommend all users retry failed requests."}, False),
            ("Slack", {"body": "The errors are unrelated to South Korea."}, False),
            ("Zoom", {"body": "South Korea is not affected."}, False),
            ("Zoom", {"body": "This issue does not affect Seoul."}, False),
            ("Zoom", {"body": "No impact to South Korea. All users in Europe are affected."}, False),
            ("Cloudflare", {"body": "Current status: Investigating. Previous status: All users affected worldwide."}, False),
        ]
        for service, item, expected in cases:
            with self.subTest(service=service, item=item):
                self.assertEqual(expected, monitor.korea_impact(service, item)["confirmed"])

    def test_dashboard_hides_foreign_and_unknown_notices(self):
        state = {"korea_only": True, "checked_at": "2026-11-13T01:00:00+00:00", "active": {
            "kr": {"service": "AWS", "incident": {"id": "aws:ec2-ap-northeast-2:errors", "name": "API errors", "status": "investigating"}},
            "foreign": {"service": "Cloudflare", "incident": {"id": "component:prg", "name": "Prague, Czechia", "status": "major_outage"}},
            "unknown": {"service": "Zoom", "incident": {"id": "one", "name": "Some users impacted", "status": "major_outage"}},
        }}
        records = monitor.visible_records(state)
        self.assertEqual(["AWS"], [v["service"] for v in records])
        text = monitor.dashboard_text(state)
        self.assertIn("한국 영향 확인 상태판", text)
        self.assertNotIn("Prague", text)
        self.assertIn("Zoom 🟢", text)
        self.assertNotIn("🔴", text)

    def test_filter_removal_is_not_reported_as_recovery(self):
        source = {"service": "Cloudflare", "kind": "statuspage", "url": "fixture", "page": "https://www.cloudflarestatus.com"}
        item = {"id": "component:prg", "name": "Prague, Czechia", "body": "", "status": "major_outage", "updated_at": ""}
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "Cloudflare:component:prg": {"service": "Cloudflare", "incident": item, "signature": monitor.fingerprint("Cloudflare", item)}
            }, "seen": []}))
            with patch.dict(os.environ, {"KOREA_ONLY": "true"}), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [source]), patch.object(monitor, "incidents", return_value=[]), patch.object(monitor, "send") as send:
                self.assertEqual(0, monitor.run())
                send.assert_not_called()

    def test_newly_confirmed_korea_scope_sends_one_notice(self):
        source = {"service": "OpenAI", "kind": "statuspage", "url": "fixture", "page": "https://status.openai.com"}
        old = {"id": "one", "name": "API errors", "status": "investigating", "body": "Some users affected", "updated_at": ""}
        new = {**old, "body": "All users worldwide are affected"}
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"initialized": True, "active": {
                "OpenAI:one": {"service": "OpenAI", "incident": old, "signature": monitor.fingerprint("OpenAI", old)}
            }, "seen": []}))
            with patch.dict(os.environ, {"KOREA_ONLY": "true"}), patch.object(monitor, "STATE_PATH", state_path), patch.object(monitor, "SOURCES", [source]), patch.object(monitor, "incidents", return_value=[new]), patch.object(monitor, "send") as send:
                self.assertEqual(0, monitor.run())
                send.assert_called_once()
                self.assertIn("한국 영향 근거", send.call_args.args[0])
                send.reset_mock()
                self.assertEqual(0, monitor.run())
                send.assert_not_called()

class DashboardLampLayoutTests(unittest.TestCase):
    def test_one_lamp_per_service_and_no_legend_or_footer(self):
        state = {"korea_only": True, "checked_at": "2026-11-13T01:00:00+00:00", "active": {
            "ms": {"service": "Microsoft 365", "incident": {"id": "one", "name": "Admin Center",
                   "status": "service_degradation", "body": "All users are affected."}}
        }}
        message = monitor.dashboard_text(state)
        self.assertIn("Microsoft 365 🟡", message)
        self.assertIn("성능 저하 지속", message)
        for source in monitor.SOURCES:
            rows = [line for line in message.splitlines() if line.startswith(source["service"] + " ")]
            self.assertEqual(1, len(rows))
        for unwanted in ("등급은", "심각 0", "경미 0", "전체 15분", "무영향 판정", "근거:", "건수에는"):
            self.assertNotIn(unwanted, message)

    def test_collection_failure_warning_remains_visible(self):
        message = monitor.dashboard_text({"active": {}, "checked_at": "2026-11-13T01:00:00+00:00",
                                         "collection_failure": "failed"})
        self.assertIn("수집 실패", message)
