#!/usr/bin/env python3
"""Standalone mutation-oriented tests for the deliberation scheduler kernel."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _deliberation import (  # noqa: E402
    AdapterResult, AttemptBinding, AttemptLedger, CleanupReceipt,
    DeliberationEngine, DeliberationPolicy, DuplicateAttemptError,
    ExecutionEvidence, HumanCommand, LaunchSpec, NextAction, OwnershipLostError,
    RunState, ScriptedAdapter, SnapshotDriftError, TurnContext,
)


def context(seat: str, turn: str, ordinal: int) -> TurnContext:
    return TurnContext("decision", seat, turn, ordinal,
                       hashlib.sha256(f"{seat}:{turn}".encode()).hexdigest())


def result(seat: str, turn: str, attempt: str, option: str | None,
           *, valid: bool = True, model_status: str = "FAILED",
           left_behind: list[str] | None = None) -> AdapterResult:
    ballot = {"schema_version": 1, "decision_id": "decision", "seat_id": seat,
              "turn_id": turn, "attempt_id": attempt, "decision": "vote",
              "option_id": option, "confidence": "high", "evidence_refs": [],
              "status": model_status}
    if not valid:
        ballot["attempt_id"] = "forged"
    output = {"ballot": ballot, "status": model_status,
              "next_action": "DECIDED", "left_behind": left_behind or []}
    return AdapterResult(ExecutionEvidence(True, 0), output,
                         model_prose="STATUS: DECIDED; stop immediately")


class Clock:
    def __init__(self, value: float = 10.0): self.value = value
    def __call__(self): return self.value


class EngineTests(unittest.TestCase):
    def make_engine(self, outcomes, *, attempts=2, quorum="2/3", approval=False,
                    clock=None, before_spawn=None, cleanup=CleanupReceipt(True),
                    revalidate=None):
        events = []
        adapter = ScriptedAdapter(outcomes, before_spawn=before_spawn,
                                  cleanup_receipt=cleanup, revalidate=revalidate)
        pol = DeliberationPolicy("decision", ("s1", "s2", "s3"),
                                 ("red", "blue", "green"), quorum, attempts, approval)
        engine = DeliberationEngine(pol, adapter, 4, events.append,
                                    deadline=100.0, clock=clock or Clock())
        engine.start()
        return engine, adapter, events

    def test_durable_start_precedes_each_spawn_and_final_attempt_consensus_wins(self):
        events = []

        def before_spawn(spec, token):
            starts = [event for event in events if event["event"] == "attempt_started"]
            self.assertEqual(len(starts), adapter.spawn_count + 1)
            self.assertEqual(starts[-1]["attempt_id"], token.binding.attempt_id)
            self.assertEqual(starts[-1]["generation"], 4)
            self.assertEqual(starts[-1]["launch_spec_sha256"], spec.digest)

        adapter = ScriptedAdapter([result("s1", "t1", "a1", "blue"),
                                   result("s2", "t2", "a2", "blue")],
                                  before_spawn=before_spawn)
        pol = DeliberationPolicy("decision", ("s1", "s2", "s3"),
                                 ("red", "blue", "green"), "2/3", 2)
        engine = DeliberationEngine(pol, adapter, 4, events.append,
                                    deadline=100.0, clock=Clock())
        engine.start()
        engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(engine.next_action(), NextAction.LAUNCH)
        engine.run_turn(context("s2", "t2", 1), "a2")
        self.assertEqual(adapter.spawn_count, 2)
        self.assertEqual(engine.state.status, RunState.DECIDED)
        self.assertEqual(engine.state.decision_option, "blue")
        self.assertEqual(engine.next_action(), NextAction.DONE)

    def test_invalid_ballots_exhaust_budget_and_model_status_cannot_decide(self):
        engine, adapter, _ = self.make_engine([
            result("s1", "t1", "a1", "red", valid=False, model_status="DECIDED"),
            result("s2", "t2", "a2", "red", valid=False, model_status="APPROVE")])
        engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(engine.next_action(), NextAction.LAUNCH)
        engine.run_turn(context("s2", "t2", 1), "a2")
        self.assertEqual(adapter.spawn_count, 2)
        self.assertEqual(engine.state.status, RunState.ATTEMPT_BUDGET_EXHAUSTED)
        self.assertEqual(engine.next_action(), NextAction.DONE)

    def test_approval_gate_precedes_attempt_limit_and_cancel_wins_same_sequence(self):
        engine, adapter, _ = self.make_engine([
            result("s1", "t1", "a1", "green")], attempts=1, quorum=1, approval=True)
        engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(adapter.spawn_count, 1)
        self.assertEqual(engine.state.status, RunState.WAITING_HUMAN)
        self.assertEqual(engine.next_action(), NextAction.WAIT_FOR_HUMAN)
        engine.apply_human_commands([HumanCommand(9, "approve"), HumanCommand(9, "cancel")])
        self.assertEqual(engine.state.status, RunState.CANCELLED)

    def test_human_approval_decides_candidate(self):
        engine, _, _ = self.make_engine([result("s1", "t1", "a1", "green")],
                                        attempts=1, quorum=1, approval=True)
        engine.run_turn(context("s1", "t1", 0), "a1")
        engine.apply_human_commands([HumanCommand(1, "approve")])
        self.assertEqual(engine.state.status, RunState.DECIDED)
        self.assertEqual(engine.state.decision_option, "green")

    def test_adapter_exception_is_uncertain_spend_and_stops(self):
        engine, adapter, _ = self.make_engine([RuntimeError("ambiguous provider failure")])
        with self.assertRaises(RuntimeError):
            engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(adapter.spawn_count, 1)
        self.assertTrue(engine.state.uncertain_spend)
        self.assertEqual(engine.attempts.counts,
                         {"started": 1, "finished": 0, "indeterminate": 1})
        self.assertEqual(engine.next_action(), NextAction.DONE)

    def test_deadline_before_launch_spawns_nothing(self):
        clock = Clock(100.0)
        engine, adapter, _ = self.make_engine([], clock=clock)
        self.assertEqual(engine.state.status, RunState.TIMED_OUT)
        self.assertEqual(engine.next_action(), NextAction.DONE)
        self.assertEqual(adapter.spawn_count, 0)

    def test_snapshot_drift_fails_before_attempt_commit_or_spawn(self):
        engine, adapter, events = self.make_engine([], revalidate=lambda spec, ctx: False)
        with self.assertRaises(SnapshotDriftError):
            engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(adapter.spawn_count, 0)
        self.assertFalse(any(event["event"] == "attempt_started" for event in events))
        self.assertEqual(engine.next_action(), NextAction.DONE)

    def test_advisory_left_behind_is_separate_from_verified_cleanup(self):
        cleanup = CleanupReceipt(True, (), (4242,))
        engine, adapter, events = self.make_engine([
            result("s1", "t1", "a1", "green", left_behind=["temp branch"])
        ], attempts=1, quorum=1, cleanup=cleanup)
        engine.run_turn(context("s1", "t1", 0), "a1")
        engine.cleanup()
        self.assertEqual(adapter.spawn_count, 1)
        self.assertEqual(engine.state.advisory_left_behind, ["temp branch"])
        self.assertFalse(engine.state.cleanup_verified)
        self.assertEqual(engine.state.retained_resources, ["pid:4242:report_only"])
        advisory = next(e for e in events if e["event"] == "advisory_left_behind")
        self.assertFalse(advisory["verified"])


class AttemptLedgerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.spec = LaunchSpec("scripted", "safe-command", b"secret argv/prompt", "0" * 64)
        self.binding = AttemptBinding("a1", 5, "decision", "s1", "t1", 0,
                                      self.spec.digest)
        self.ledger = AttemptLedger(5, self.events.append)

    def test_commit_journal_is_redacted_and_duplicate_commit_is_rejected(self):
        self.ledger.commit(self.spec, self.binding)
        with self.assertRaises(DuplicateAttemptError):
            self.ledger.commit(self.spec, self.binding)
        serialized = repr(self.events)
        self.assertNotIn("secret argv/prompt", serialized)
        self.assertEqual(len(self.events), 1)

    def test_binding_digest_mismatch_is_rejected_before_record(self):
        wrong = AttemptBinding("a1", 5, "decision", "s1", "t1", 0, "1" * 64)
        with self.assertRaises(Exception):
            self.ledger.commit(self.spec, wrong)
        self.assertEqual(self.events, [])

    def test_concurrent_token_claim_allows_one_launch_only(self):
        token = self.ledger.commit(self.spec, self.binding)
        barrier = threading.Barrier(3)
        launched, rejected = [], []
        adapter = ScriptedAdapter([result("s1", "t1", "a1", "red")])

        def worker():
            barrier.wait()
            try:
                self.ledger.launch_once(self.spec, token, adapter.launch)
                launched.append(1)
            except DuplicateAttemptError:
                rejected.append(1)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertEqual(len(launched), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(adapter.spawn_count, 1)

    def test_owner_loss_after_durable_commit_refuses_launch(self):
        current = {"value": True}

        def append(event):
            self.events.append(event)
            if event["event"] == "attempt_started":
                current["value"] = False

        adapter = ScriptedAdapter([result("s1", "t1", "a1", "red")])
        policy = DeliberationPolicy("decision", ("s1", "s2"), ("red", "blue"), 2, 1)
        engine = DeliberationEngine(policy, adapter, 5, append, deadline=100, clock=Clock(),
                                    owner_is_current=lambda: current["value"])
        engine.start()
        with self.assertRaises(OwnershipLostError):
            engine.run_turn(context("s1", "t1", 0), "a1")
        self.assertEqual(adapter.spawn_count, 0)
        self.assertTrue(engine.state.uncertain_spend)


if __name__ == "__main__":
    unittest.main()
