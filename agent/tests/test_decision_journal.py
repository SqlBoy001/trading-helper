"""Isolated regression tests for decision history; no network or real records."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src.decision_journal import DecisionJournal
from src.tools.decision_journal_tool import ReadDecisionJournalTool, RecordDecisionEventTool


class DecisionJournalTests(unittest.TestCase):
    """Exercise persistence, knowledge time and erroneous/duplicate writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"VIBE_TRADING_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = "2026-09-22T08:00:00.000000+00:00"
        clock = patch("src.decision_journal.store._now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.journal = DecisionJournal()

    def record(self, kind, data, **kwargs):
        args = dict(journal_id="mock:company", kind=kind, request_key=kwargs.pop("request_key", kind),
                    source="user", source_ref="SYNTHETIC user quote", data=data)
        args.update(kwargs)
        return self.journal.append(**args)["event"]

    def plan(self, **kwargs):
        data = dict(thesis="Synthetic thesis", rules=["Synthetic rule: 20% limit"],
                    confirmed=True, confirmation_quote="I confirm this synthetic rule")
        data.update(kwargs.pop("data", {}))
        return self.record("plan", data, **kwargs)

    def intent(self, **kwargs):
        return self.record("intent", {"action": "add", "reason": "synthetic rationale"}, **kwargs)

    def execution(self, parent, **kwargs):
        data = dict(symbol="SYNTHETIC", side="buy", quantity=100, price=10, currency="CNY",
                    external_source="mock-broker:account-A", external_id="fill-1")
        data.update(kwargs.pop("data", {}))
        return self.record("execution", data, parent_id=parent, source="import", **kwargs)

    def test_latest_snapshot_applies_corrections_only_at_cutoff(self):
        data = dict(positions=[dict(symbol="MOCK", quantity=100)], cash=0, complete=True, notes="synthetic")
        original = self.record("snapshot", data)
        self.now = "2026-09-22T08:02:00.000000+00:00"
        self.record("correction", dict(reason="synthetic fix", replacement=dict(data, positions=[])), parent_id=original["id"])
        self.assertEqual(self.journal.latest_snapshot("mock:company")["effective_data"]["positions"], [])
        old = self.journal.latest_snapshot("mock:company", as_of="2026-09-22T08:01:00Z")
        self.assertEqual(old["effective_data"]["positions"][0]["quantity"], 100)
        self.assertEqual(old["corrections"], [])
        self.assertEqual(self.journal.latest_snapshot("other")["status"], "missing")

    def test_shared_path_override_and_explicit_path_precedence(self):
        shared = self.root / "shared" / "journal.sqlite3"
        with patch.dict(os.environ, {"VIBE_TRADING_DECISION_JOURNAL_PATH": str(shared)}):
            self.assertEqual(DecisionJournal().path, shared)
            self.assertEqual(DecisionJournal(self.root / "explicit.db").path, self.root / "explicit.db")
        with patch.dict(os.environ, {"VIBE_TRADING_DECISION_JOURNAL_PATH": "relative.db"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                DecisionJournal()

    def test_read_empty_does_not_create_database(self):
        self.assertEqual(self.journal.history("empty")["events"], [])
        self.assertFalse(self.journal.path.exists())

    def test_full_record_survives_new_instance_without_snippet_truncation(self):
        rule = "模拟规则" * 900 + "END_CRITICAL_RULE"
        plan = self.plan(data={"rules": [rule]})
        restored = DecisionJournal().get(plan["id"])["event"]
        self.assertEqual(restored["data"]["rules"], [rule])

    def test_idempotent_retry_and_conflicting_request(self):
        first = self.intent()
        self.now = "2026-09-22T08:01:00.000000+00:00"
        self.assertEqual(first, self.intent())
        with self.assertRaisesRegex(ValueError, "different content"):
            self.record("intent", {"action": "buy", "reason": "changed"})
        self.assertEqual(len(self.journal.history("mock:company")["events"]), 1)

    def test_concurrent_duplicate_requests_commit_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: self.intent()["id"], range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.journal.history("mock:company")["events"]), 1)

    def test_plan_version_does_not_rewrite_intent(self):
        v1 = self.plan()
        self.now = "2026-09-22T08:01:00.000000+00:00"
        intent = self.intent()
        self.now = "2026-09-22T08:02:00.000000+00:00"
        v2 = self.plan(parent_id=v1["id"], request_key="plan-v2", data={"rules": ["new rule"]})
        self.assertEqual(v2["version"], 2)
        self.assertEqual(self.journal.get(intent["id"])["event"]["parent_id"], v1["id"])
        old = self.journal.history("mock:company", as_of="2026-09-22T16:01:00+08:00")
        self.assertEqual(old["active_plan"]["id"], v1["id"])
        self.assertEqual(self.journal.history("mock:company")["active_plan"]["id"], v2["id"])
        with self.assertRaisesRegex(ValueError, "latest plan"):
            self.plan(parent_id=v1["id"], request_key="stale-revision")

    def test_future_plan_and_draft_do_not_replace_current_confirmed_plan(self):
        v1 = self.plan()
        v2 = self.plan(parent_id=v1["id"], request_key="future", effective_at="2026-09-23T00:00:00Z")
        self.assertEqual(self.intent()["parent_id"], v1["id"])
        self.plan(parent_id=v2["id"], request_key="draft", data={"confirmed": False})
        self.assertEqual(self.journal.history("mock:company")["active_plan"]["id"], v1["id"])

    def test_backdated_plan_rejected_and_late_intent_has_no_invented_plan(self):
        self.plan()
        for kwargs in ({"effective_at": "2026-09-21T00:00:00Z"},
                       {"occurred_at": "2026-09-21T00:00:00Z"}):
            with self.assertRaises(ValueError):
                self.plan(journal_id="other", request_key="backdated", **kwargs)
        intent = self.intent(occurred_at="2026-09-21T09:00:00Z")
        self.assertIsNone(intent["parent_id"])
        self.assertEqual(self.journal.history("mock:company", as_of="2026-09-21T12:00:00Z")["events"], [])

    def test_explicit_old_or_cross_journal_parent_rejected(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "another journal"):
            self.intent(journal_id="other", parent_id=plan["id"])
        self.plan(parent_id=plan["id"], request_key="v2")
        with self.assertRaisesRegex(ValueError, "active at its time"):
            self.intent(parent_id=plan["id"])

    def test_intent_and_deferred_decision_are_not_executions(self):
        intent = self.intent()
        self.record("decision", {"action": "defer", "reason": "check evidence"}, parent_id=intent["id"])
        events = self.journal.history("mock:company")["events"]
        self.assertEqual([x["kind"] for x in events], ["intent", "decision"])
        self.assertEqual(list(self.root.iterdir()), [self.journal.path])

    def test_external_execution_dedup_and_required_import_identity(self):
        intent = self.intent()
        self.execution(intent["id"])
        with self.assertRaisesRegex(ValueError, "duplicate external execution"):
            self.execution(intent["id"], request_key="different-import-file")
        with self.assertRaisesRegex(ValueError, "external_id"):
            self.record("execution", {"symbol": "S", "side": "buy", "price": 1, "quantity": 1,
                                      "currency": "CNY"}, source="import", parent_id=intent["id"])

    def test_corrections_preserve_original_and_require_linear_revisions(self):
        trade = self.execution(self.intent()["id"])
        replacement = dict(trade["data"], quantity=80)
        c1 = self.record("correction", {"reason": "entry typo", "replacement": replacement}, parent_id=trade["id"])
        with self.assertRaisesRegex(ValueError, "latest amendment"):
            self.record("correction", {"reason": "stale", "replacement": replacement},
                        parent_id=trade["id"], request_key="stale")
        c2 = self.record("correction", {"reason": "second correction", "replacement": dict(replacement, quantity=60)},
                         parent_id=c1["id"], request_key="correction2")
        result = self.journal.get(trade["id"])
        self.assertEqual(result["event"]["data"]["quantity"], 100)
        self.assertEqual([x["id"] for x in result["corrections"]], [c1["id"], c2["id"]])

    def test_assistant_cannot_assert_user_actions_or_rewrite_them(self):
        with self.assertRaisesRegex(ValueError, "assistant inference"):
            self.plan(source="assistant")
        intent = self.intent()
        with self.assertRaisesRegex(ValueError, "assistant inference"):
            self.record("decision", {"action": "buy", "reason": "model decided"},
                        parent_id=intent["id"], source="assistant")
        trade = self.execution(intent["id"])
        with self.assertRaisesRegex(ValueError, "assistant inference"):
            self.record("correction", {"reason": "model guess", "replacement": trade["data"]},
                        parent_id=trade["id"], source="assistant")

    def test_correction_cannot_bypass_import_identity(self):
        trade = self.execution(self.intent()["id"])
        with self.assertRaisesRegex(ValueError, "external execution identity"):
            self.record("correction", {"reason": "change id", "replacement": dict(trade["data"], external_id="fill-2")},
                        parent_id=trade["id"])

    def test_assistant_cannot_rewrite_user_intent_via_correction(self):
        intent = self.intent()
        with self.assertRaisesRegex(ValueError, "assistant inference"):
            self.record("correction", {"reason": "inferred motive", "replacement": intent["data"]},
                        parent_id=intent["id"], source="assistant")

    def test_reminder_corrections_also_validate_evidence(self):
        intent = self.intent()
        data = {"status": "needs_information", "observation": "test", "next_step": "check",
                "evidence_ids": [intent["id"]]}
        reminder = self.record("reminder", data, parent_id=intent["id"], source="assistant")
        with self.assertRaisesRegex(ValueError, "event not found"):
            self.record("correction", {"reason": "bad citation", "replacement": dict(data, evidence_ids=["missing"])},
                        parent_id=reminder["id"], source="assistant")

    def test_plan_cannot_be_replaced_through_generic_correction(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "revise plans"):
            self.record("correction", {"reason": "backfill", "replacement": plan["data"]}, parent_id=plan["id"])

    def test_evidence_recorded_later_cannot_support_backdated_reminder(self):
        intent = self.intent()
        self.now = "2026-09-22T08:05:00.000000+00:00"
        evidence = self.intent(request_key="later-intent")
        with self.assertRaisesRegex(ValueError, "known at reminder time"):
            self.record("reminder", {"status": "needs_information", "observation": "test",
                                     "next_step": "check", "evidence_ids": [evidence["id"]]},
                        parent_id=intent["id"], occurred_at="2026-09-22T08:02:00Z", source="assistant")

    def test_reminder_evidence_cannot_be_fabricated_or_from_other_journal(self):
        intent = self.intent()
        other = self.intent(journal_id="other", request_key="other-intent")
        for ref in ["missing", other["id"]]:
            with self.assertRaises(ValueError):
                self.record("reminder", {"status": "needs_information", "observation": "unknown",
                                         "next_step": "check", "evidence_ids": [ref]},
                            parent_id=intent["id"], source="assistant")
        result = self.record("reminder", {"status": "needs_information", "observation": "unknown",
                                         "next_step": "check", "evidence_ids": [intent["id"]]},
                             parent_id=intent["id"], source="assistant")
        self.assertEqual(result["kind"], "reminder")

    def test_invalid_numbers_timezone_and_future_occurrences(self):
        intent = self.intent()
        for value in [True, 0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                self.execution(intent["id"], data={"quantity": value})
        for date in ["2026-09-22T08:00:00", "2026-09-23T08:00:00Z", "", False, 0]:
            with self.assertRaises(ValueError):
                self.intent(request_key="bad-time", occurred_at=date)

    def test_pagination_returns_all_events_without_silent_truncation(self):
        ids = [self.intent(request_key=f"intent-{i}")["id"] for i in range(5)]
        result = self.journal.history("mock:company", limit=2)
        actual = [x["id"] for x in result["events"]]
        while result["next_after"] is not None:
            result = self.journal.history("mock:company", after=result["next_after"], limit=2)
            actual.extend(x["id"] for x in result["events"])
        self.assertEqual(actual, ids)

    def test_sql_update_delete_rejected(self):
        self.intent()
        with sqlite3.connect(self.journal.path) as conn:
            for query in ["DELETE FROM events", "UPDATE events SET body='{}'"]:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(query)

    def test_tools_return_errors_and_support_full_roundtrip(self):
        write, read = RecordDecisionEventTool(), ReadDecisionJournalTool()
        self.assertFalse(write.is_readonly)
        self.assertTrue(read.is_readonly)
        result = json.loads(write.execute(journal_id="tool", kind="intent", request_key="tool-write",
                                          source="user", source_ref="synthetic", data={"action": "watch", "reason": "test"}))
        self.assertEqual(result["status"], "ok")
        record = json.loads(read.execute(event_id=result["event"]["id"]))
        self.assertEqual(record["event"]["data"]["reason"], "test")
        for args in [{}, {"event_id": "missing", "journal_id": "tool"},
                     {"journal_id": "tool", "limit": True}]:
            self.assertEqual(json.loads(read.execute(**args))["status"], "error")


if __name__ == "__main__":
    unittest.main()
