"""Mutation-sensitive tests for provider-inert resume reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_replay as replay
import _deliberation_resume as resume
import _deliberation_store as store
import _rundir


def receipt(run_id: str, *, approval: bool = False) -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "question_sha256": hashlib.sha256(b"question").hexdigest(),
        "decision_id": "decision-1", "seat_ids": ["a", "b"],
        "option_ids": ["yes", "no"], "quorum_rule": "all",
        "max_attempts": 4, "require_human_approval": approval,
        "rounds": 1, "deadline_unix_ms": 4_000_000_000_000,
    }


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "deliberations")
        self.owners = []

    def tearDown(self) -> None:
        for owner in self.owners:
            _rundir.release_owner(owner)
        self.temp.cleanup()

    def create(self, run_id: str = "run-1", *, approval: bool = False):
        value = receipt(run_id, approval=approval)
        path, owner = store.initialize_run(self.root, value)
        self.owners.append(owner)
        return value, path, owner

    @staticmethod
    def append(path, owner, event, **fields):
        _rundir.journal_append(path, {
            "event": event, "schema_version": 1,
            "generation": owner.generation, **fields,
        }, owner=owner)

    def release(self, owner):
        _rundir.release_owner(owner)
        if owner in self.owners:
            self.owners.remove(owner)

    def running(self, path, owner):
        self.append(path, owner, "state_transition", **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started",
            "decision_option": None,
        })

    def consensus_prefix(self, path, owner):
        self.running(path, owner)
        for ordinal, seat in enumerate(("a", "b")):
            turn = f"turn-{seat}-{ordinal}"
            attempt = f"attempt-{seat}-{ordinal}"
            request = hashlib.sha256(turn.encode()).hexdigest()
            spec = hashlib.sha256(attempt.encode()).hexdigest()
            self.append(path, owner, "turn_prepared", decision_id="decision-1",
                        seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                        request_digest=request)
            self.append(path, owner, "attempt_started", attempt_id=attempt,
                        decision_id="decision-1", seat_id=seat, turn_id=turn,
                        turn_ordinal=ordinal, launch_spec_sha256=spec)
            self.append(path, owner, "attempt_finished", attempt_id=attempt,
                        launch_spec_sha256=spec, transport_ok=True, exit_code=0,
                        timed_out=False, parser_valid=True, ballot_valid=True)
            self.append(path, owner, "ballot_accepted", attempt_id=attempt,
                        seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                        decision="vote", option_id="yes")

    def sealed_cancel(self, path, owner):
        commands = (replay.ReplayCommand("cmd-1", 1, "cancel"),)
        digest = replay.command_batch_sha256(commands)
        self.append(path, owner, "human_command_batch",
                    batch_id="batch-" + digest[:32],
                    source_generation=owner.generation,
                    commands=[command.__dict__ for command in commands],
                    command_batch_sha256=digest)

    def test_sealed_batch_across_torn_repair_recovers_without_provider(self):
        _value, path, owner = self.create()
        self.sealed_cancel(path, owner)
        with open(_rundir._journal_path(path, owner.generation), "ab") as handle:
            handle.write(b"{torn-after-command-batch")
        self.release(owner)
        result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["decision_status"], "cancelled")
        self.assertTrue(result["journal_repaired"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(_rundir.read_owner(path))

    def test_consensus_prefix_reconciles_decision_and_next_owner_is_idempotent(self):
        _value, path, owner = self.create()
        self.consensus_prefix(path, owner)
        self.release(owner)
        first = resume.reconcile_run(self.root, "run-1")
        second = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(first["status"], "reconciled_terminal")
        self.assertEqual(first["decision_status"], "decided")
        self.assertEqual(first["decision_option"], "yes")
        self.assertEqual(second["status"], "already_terminal")
        self.assertEqual(second["checkpoint_digest"], first["checkpoint_digest"])
        self.assertEqual(first["provider_calls"], 0)
        self.assertEqual(second["provider_calls"], 0)

    def test_consensus_prefix_survives_current_owner_repair(self):
        _value, path, owner = self.create()
        self.consensus_prefix(path, owner)
        with open(_rundir._journal_path(path, owner.generation), "ab") as handle:
            handle.write(b"{torn-after-consensus")
        self.release(owner)
        result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(result["status"], "reconciled_terminal")
        self.assertEqual(result["decision_status"], "decided")
        self.assertTrue(result["journal_repaired"])
        self.assertEqual(result["provider_calls"], 0)

    def test_consensus_with_approval_reconciles_to_waiting_human_once(self):
        _value, path, owner = self.create(approval=True)
        self.consensus_prefix(path, owner)
        self.release(owner)
        first = resume.reconcile_run(self.root, "run-1")
        second = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(first["status"], "waiting_human")
        self.assertEqual(first["decision_status"], "waiting_human")
        self.assertEqual(second["status"], "waiting_human")
        tagged, torn = _rundir.journal_read_tagged(path)
        self.assertFalse(torn)
        self.assertEqual(sum(record.get("to") == "WAITING_HUMAN"
                             for _generation, record in tagged), 1)

    def test_existing_terminal_prefix_is_read_only(self):
        _value, path, owner = self.create()
        self.append(path, owner, "state_transition", **{
            "from": "PREPARED", "to": "CANCELLED", "reason": "cancelled",
            "decision_option": None,
        })
        self.release(owner)
        before, _ = _rundir.journal_read_tagged(path)
        result = resume.reconcile_run(self.root, "run-1")
        after, _ = _rundir.journal_read_tagged(path)
        self.assertEqual(result["status"], "already_terminal")
        self.assertEqual(before, after)

    def test_indeterminate_attempt_is_blocked_without_retry(self):
        _value, path, owner = self.create()
        self.running(path, owner)
        request = hashlib.sha256(b"turn-a").hexdigest()
        spec = hashlib.sha256(b"attempt-a").hexdigest()
        self.append(path, owner, "turn_prepared", decision_id="decision-1",
                    seat_id="a", turn_id="turn-a", turn_ordinal=0,
                    request_digest=request)
        self.append(path, owner, "attempt_started", attempt_id="attempt-a",
                    decision_id="decision-1", seat_id="a", turn_id="turn-a",
                    turn_ordinal=0, launch_spec_sha256=spec)
        self.release(owner)
        result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual((result["status"], result["error_kind"]),
                         ("blocked", "uncertain_spend"))
        self.assertEqual(result["provider_calls"], 0)

    def test_terminal_failed_prefix_cannot_hide_uncertain_spend(self):
        _value, path, owner = self.create()
        self.running(path, owner)
        request = hashlib.sha256(b"turn-a").hexdigest()
        spec = hashlib.sha256(b"attempt-a").hexdigest()
        self.append(path, owner, "turn_prepared", decision_id="decision-1",
                    seat_id="a", turn_id="turn-a", turn_ordinal=0,
                    request_digest=request)
        self.append(path, owner, "attempt_started", attempt_id="attempt-a",
                    decision_id="decision-1", seat_id="a", turn_id="turn-a",
                    turn_ordinal=0, launch_spec_sha256=spec)
        self.append(path, owner, "state_transition", **{
            "from": "RUNNING", "to": "FAILED",
            "reason": "adapter_indeterminate", "decision_option": None,
        })
        self.release(owner)
        result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual((result["status"], result["error_kind"]),
                         ("blocked", "uncertain_spend"))
        self.assertEqual(result["decision_status"], "failed")
        self.assertEqual(result["provider_calls"], 0)

    def test_pending_turn_and_plain_prepared_are_not_started(self):
        _value, path, owner = self.create("run-pending")
        self.running(path, owner)
        self.append(path, owner, "turn_prepared", decision_id="decision-1",
                    seat_id="a", turn_id="turn-a", turn_ordinal=0,
                    request_digest=hashlib.sha256(b"turn-a").hexdigest())
        self.release(owner)
        pending = resume.reconcile_run(self.root, "run-pending")
        self.assertEqual(pending["error_kind"], "pending_turn")

        _value, _path, owner = self.create("run-prepared")
        self.release(owner)
        prepared = resume.reconcile_run(self.root, "run-prepared")
        self.assertEqual(prepared["error_kind"], "no_deterministic_recovery")

    def test_legacy_unsealed_commands_fail_closed(self):
        _value, path, owner = self.create()
        self.append(path, owner, "human_command", command_id="cmd-1",
                    sequence=1, action="cancel")
        self.release(owner)
        result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual((result["status"], result["error_kind"]),
                         ("blocked", "legacy_unsealed_commands"))

    def test_owner_loss_before_recovery_append_is_blocked(self):
        _value, path, owner = self.create()
        self.sealed_cancel(path, owner)
        self.release(owner)
        original = resume._recovery.recover_pending_human_commands

        def lose_owner(**kwargs):
            _rundir.release_owner(kwargs["owner"])
            return original(**kwargs)

        with mock.patch.object(resume._recovery,
                               "recover_pending_human_commands",
                               side_effect=lose_owner):
            result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual((result["status"], result["error_kind"]),
                         ("blocked", "ownership_lost"))
        tagged, _ = _rundir.journal_read_tagged(path)
        self.assertFalse(any(record.get("recovery_kind")
                             for _generation, record in tagged))

    def test_owner_loss_before_candidate_transition_is_blocked(self):
        _value, path, owner = self.create()
        self.consensus_prefix(path, owner)
        self.release(owner)
        original = resume.DeliberationEngine.restore

        def lose_owner(*args, **kwargs):
            _rundir.release_owner(kwargs["owner"])
            return original(*args, **kwargs)

        with mock.patch.object(resume.DeliberationEngine, "restore",
                               side_effect=lose_owner):
            result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual((result["status"], result["error_kind"]),
                         ("blocked", "ownership_lost"))
        tagged, _ = _rundir.journal_read_tagged(path)
        self.assertFalse(any(record.get("to") == "DECIDED"
                             for _generation, record in tagged))

    def test_tampered_receipt_and_corrupt_journal_are_bounded(self):
        value, path, owner = self.create("run-receipt")
        self.release(owner)
        value["option_ids"] = ["yes", "maybe"]
        _rundir.atomic_write_json(os.path.join(path, "receipt.json"), value)
        changed = resume.reconcile_run(self.root, "run-receipt")
        self.assertEqual(changed["status"], "blocked")
        self.assertIn(changed["error_kind"], {"journal_invalid", "receipt_invalid"})

        _value, path, owner = self.create("run-corrupt")
        self.running(path, owner)
        self.release(owner)
        journal = _rundir._journal_path(path, 1)
        with open(journal, "r+b") as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(b"X" if first != b"X" else b"Y")
        corrupt = resume.reconcile_run(self.root, "run-corrupt")
        self.assertEqual((corrupt["status"], corrupt["error_kind"]),
                         ("blocked", "journal_invalid"))

    def test_live_owner_unknown_run_and_invalid_id_are_bounded(self):
        _value, _path, owner = self.create()
        held = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(held["error_kind"], "owner_held")
        self.assertEqual(resume.reconcile_run(self.root, "missing")["error_kind"],
                         "unknown_run")
        invalid = resume.reconcile_run(self.root, "../secret")
        self.assertEqual(invalid["error_kind"], "invalid_run_id")
        self.assertNotIn(self.temp.name, json.dumps(invalid))
        path_like = resume.reconcile_run(
            self.root, r"C:\Users\nside\private\project")
        self.assertEqual(path_like["error_kind"], "invalid_run_id")
        self.assertEqual(path_like["run_id"], "<invalid>")
        self.assertNotIn("private", json.dumps(path_like).lower())

    def test_coordinator_acquires_and_releases_exactly_one_identical_owner(self):
        _value, _path, owner = self.create()
        self.release(owner)
        acquired, released = [], []
        original_acquire = _rundir.acquire_owner
        original_release = _rundir.release_owner

        def acquire(*args, **kwargs):
            value = original_acquire(*args, **kwargs)
            acquired.append(value)
            return value

        def release(value):
            released.append(value)
            original_release(value)

        with mock.patch.object(resume._rundir, "acquire_owner",
                               side_effect=acquire), mock.patch.object(
                                   resume._rundir, "release_owner",
                                   side_effect=release):
            result = resume.reconcile_run(self.root, "run-1")
        self.assertEqual(result["error_kind"], "no_deterministic_recovery")
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(released), 1)
        self.assertIs(released[0], acquired[0])

    def test_module_has_no_provider_process_or_network_import_surface(self):
        source = Path(resume.__file__).read_text(encoding="utf-8")
        for forbidden in ("_executor", "_deliberation_scheduler", "subprocess",
                          "socket", "urllib", "commands_dir"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
