"""Mutation-sensitive tests for owner-bound human-command recovery."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_recovery as recovery
import _deliberation_replay as replay
import _rundir


def receipt(*, approval: bool = False, run_id: str = "run-1") -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "decision_id": "decision-1", "seat_ids": ["a", "b"],
        "option_ids": ["yes", "no"], "quorum_rule": "all",
        "max_attempts": 4, "require_human_approval": approval,
    }


def event(name: str, generation: int, **fields) -> dict:
    return {"event": name, "schema_version": 1,
            "generation": generation, **fields}


def _digest(value: object) -> str:
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = self.temp.name
        self.first = _rundir.acquire_owner(self.run_dir, 600)
        self.value = receipt(approval=getattr(self, "_approval", False))
        _rundir.atomic_write_json(os.path.join(self.run_dir, "receipt.json"), self.value)
        _rundir.journal_append(self.run_dir, event(
            "run_prepared", self.first.generation, run_id="run-1",
            receipt_sha256=_digest(self.value)), self.first)

    def new_run(self, *, approval: bool = False):
        self.tearDown()
        self._approval = approval
        self.setUp()

    def tearDown(self) -> None:
        try:
            if _rundir.owner_still_current(self.first):
                _rundir.release_owner(self.first)
        except Exception:
            pass
        self.temp.cleanup()

    def take_over(self):
        _rundir.release_owner(self.first)
        return _rundir.acquire_owner(self.run_dir, 600)

    def append(self, owner, name, **fields):
        _rundir.journal_append(self.run_dir, event(name, owner.generation, **fields), owner)

    def command(self, owner, command_id, sequence, action):
        self.append(owner, "human_command", command_id=command_id,
                    sequence=sequence, action=action)

    def running(self, owner):
        self.append(owner, "state_transition", **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})

    def waiting_human(self, owner):
        self.running(owner)
        for seat, turn, attempt, ordinal in (
                ("a", "turn-a-0", "g1-a0", 0),
                ("b", "turn-b-1", "g1-a1", 1)):
            spec = hashlib.sha256((attempt + "-spec").encode()).hexdigest()
            self.append(owner, "turn_prepared", decision_id="decision-1",
                        seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                        request_digest=hashlib.sha256(turn.encode()).hexdigest())
            self.append(owner, "attempt_started", attempt_id=attempt,
                        decision_id="decision-1", seat_id=seat, turn_id=turn,
                        turn_ordinal=ordinal, launch_spec_sha256=spec)
            self.append(owner, "attempt_finished", attempt_id=attempt,
                        launch_spec_sha256=spec, ballot_valid=True,
                        transport_ok=True, timed_out=False, parser_valid=True,
                        exit_code=0)
            self.append(owner, "ballot_accepted", attempt_id=attempt,
                        seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                        decision="vote", option_id="yes")
        self.append(owner, "state_transition", **{
            "from": "RUNNING", "to": "WAITING_HUMAN",
            "reason": "approval_required", "decision_option": None})

    def recover(self, owner, *, configured=None):
        from _deliberation import DeliberationPolicy
        configured = configured or receipt()
        policy = DeliberationPolicy(
            configured["decision_id"], tuple(configured["seat_ids"]),
            tuple(configured["option_ids"]), configured["quorum_rule"],
            configured["max_attempts"], configured["require_human_approval"])
        return recovery.recover_pending_human_commands(
            owner=owner, receipt=configured, policy=policy)

    def test_prepared_cancel_is_recovered_once(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        result = self.recover(owner)
        self.assertEqual(result.status, "recovered")
        self.assertTrue(result.appended)
        self.assertEqual(result.target_state, "CANCELLED")
        self.assertEqual(result.reason, "human_cancel")
        again = self.recover(owner)
        self.assertEqual(again.status, "already_recovered")
        self.assertFalse(again.appended)
        self.assertEqual(again.command_batch_sha256, result.command_batch_sha256)

    def test_running_cancel_and_waiting_denial(self):
        self.running(self.first)
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        result = self.recover(owner)
        self.assertEqual((result.target_state, result.reason),
                         ("CANCELLED", "human_cancel"))

        # A separate run exercises the WAITING_HUMAN denial boundary.
        self.new_run(approval=True)
        self.waiting_human(self.first)
        self.command(self.first, "cmd-1", 1, "deny")
        owner = self.take_over()
        result = self.recover(owner, configured=self.value)
        self.assertEqual((result.target_state, result.reason),
                         ("REJECTED", "human_denied"))

    def test_waiting_approval_recomputes_candidate(self):
        self.new_run(approval=True)
        self.waiting_human(self.first)
        self.command(self.first, "cmd-1", 1, "approve")
        owner = self.take_over()
        result = self.recover(owner, configured=self.value)
        self.assertEqual((result.target_state, result.reason),
                         ("DECIDED", "human_approved"))
        tagged, torn = _rundir.journal_read_tagged(self.run_dir)
        self.assertFalse(torn)
        transition = tagged[-1][1]
        self.assertEqual(transition["decision_option"], "yes")
        self.assertEqual(transition["recovery_kind"], "human_command_eof")
        self.assertEqual(transition["command_batch_sha256"], result.command_batch_sha256)

    def test_equal_sequence_priority_and_earlier_sequence_wins(self):
        self.new_run(approval=True)
        self.waiting_human(self.first)
        self.command(self.first, "cmd-a", 1, "approve")
        self.command(self.first, "cmd-c", 1, "cancel")
        self.command(self.first, "cmd-d", 1, "deny")
        self.command(self.first, "cmd-late", 2, "cancel")
        owner = self.take_over()
        result = self.recover(owner, configured=self.value)
        self.assertEqual((result.target_state, result.reason),
                         ("CANCELLED", "human_cancel"))

        # An earlier approve is authoritative even when a later cancel exists.
        self.new_run(approval=True)
        self.waiting_human(self.first)
        self.command(self.first, "cmd-a", 1, "approve")
        self.command(self.first, "cmd-late", 2, "cancel")
        owner = self.take_over()
        result = self.recover(owner, configured=self.value)
        self.assertEqual((result.target_state, result.reason),
                         ("DECIDED", "human_approved"))

    def test_approval_and_denial_before_waiting_are_refused(self):
        self.command(self.first, "cmd-1", 1, "approve")
        owner = self.take_over()
        with self.assertRaises(recovery.RecoveryError):
            self.recover(owner)
        self.new_run()
        self.command(self.first, "cmd-1", 1, "deny")
        owner = self.take_over()
        with self.assertRaises(recovery.RecoveryError):
            self.recover(owner)

    def test_missing_candidate_approval_is_refused(self):
        self.new_run(approval=True)
        self.running(self.first)
        self.append(self.first, "state_transition", **{
            "from": "RUNNING", "to": "WAITING_HUMAN",
            "reason": "approval_required", "decision_option": None})
        self.command(self.first, "cmd-1", 1, "approve")
        owner = self.take_over()
        with self.assertRaises(recovery.RecoveryError):
            self.recover(owner, configured=self.value)

    def test_duplicate_or_gapped_commands_fail_before_append(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        self.command(self.first, "cmd-1", 2, "cancel")
        owner = self.take_over()
        with self.assertRaises(recovery.RecoveryError):
            self.recover(owner)
        records, _ = _rundir.journal_read(self.run_dir)
        self.assertFalse(any(item.get("recovery_kind") for item in records))

    def test_receipt_and_policy_drift_fail_closed(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        changed = receipt()
        changed["option_ids"] = ["yes", "maybe"]
        from _deliberation import DeliberationPolicy
        policy = DeliberationPolicy("decision-1", ("a", "b"),
                                    ("yes", "maybe"), "all", 4, False)
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover_pending_human_commands(owner=owner,
                                                    receipt=changed,
                                                    policy=policy)

    def test_successor_owner_is_idempotent_and_no_provider_surface_exists(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        self.assertEqual(self.recover(owner).status, "recovered")
        _rundir.release_owner(owner)
        successor = _rundir.acquire_owner(self.run_dir, 600)
        result = self.recover(successor)
        self.assertEqual(result.status, "already_recovered")
        source = Path(recovery.__file__).read_text(encoding="utf-8")
        for forbidden in ("_executor", "_builder", "_deliberation_adapter",
                          "_deliberation_scheduler", "subprocess"):
            self.assertNotIn(forbidden, source)

    def test_torn_predecessor_is_repaired_before_recovery(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        with open(_rundir._journal_path(self.run_dir, self.first.generation),
                  "ab") as handle:
            handle.write(b"{torn")
        owner = self.take_over()
        self.assertTrue(_rundir.journal_repair(self.run_dir, owner))
        result = self.recover(owner)
        self.assertEqual(result.status, "recovered")
        self.assertTrue(result.appended)

    def test_stale_owner_returns_without_append(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        stale = self.take_over()
        _rundir.release_owner(stale)
        successor = _rundir.acquire_owner(self.run_dir, 600)
        result = self.recover(stale)
        self.assertEqual(result.status, "ownership_lost")
        records, _ = _rundir.journal_read(self.run_dir)
        self.assertFalse(any(item.get("recovery_kind") for item in records))
        _rundir.release_owner(successor)

    def test_durable_append_then_return_failure_is_idempotent(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        original = _rundir.journal_append
        state = {"raised": False}

        def append_then_fail(run_dir, record, owner):
            original(run_dir, record, owner)
            if not state["raised"]:
                state["raised"] = True
                raise OSError("simulated return failure after fsync")

        _rundir.journal_append = append_then_fail
        try:
            with self.assertRaises(OSError):
                self.recover(owner)
        finally:
            _rundir.journal_append = original
        result = self.recover(owner)
        self.assertEqual(result.status, "already_recovered")
        self.assertFalse(result.appended)

    def test_terminal_cleanup_audit_does_not_break_same_owner_idempotency(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        self.assertEqual(self.recover(owner).status, "recovered")
        self.append(owner, "cleanup_receipt", verified=True, clean=True)
        self.append(owner, "advisory_left_behind")
        result = self.recover(owner)
        self.assertEqual(result.status, "already_recovered")
        self.assertFalse(result.appended)

    def test_terminal_cleanup_audit_does_not_break_successor_idempotency(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        self.assertEqual(self.recover(owner).status, "recovered")
        self.append(owner, "cleanup_receipt", verified=True, clean=True)
        self.append(owner, "advisory_left_behind")
        _rundir.release_owner(owner)
        successor = _rundir.acquire_owner(self.run_dir, 600)
        result = self.recover(successor)
        self.assertEqual(result.status, "already_recovered")
        self.assertFalse(result.appended)

    def test_tampered_current_recovery_uses_public_error_boundary(self):
        self.command(self.first, "cmd-1", 1, "cancel")
        owner = self.take_over()
        self.append(owner, "state_transition", **{
            "from": "PREPARED", "to": "CANCELLED", "reason": "human_cancel",
            "decision_option": None, "recovery_kind": "human_command_eof",
            "command_batch_sha256": "0" * 64,
        })
        with self.assertRaises(recovery.RecoveryError):
            self.recover(owner)


if __name__ == "__main__":
    unittest.main()
