"""Adversarial, provider-inert tests for deliberation journal replay."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_replay as replay
import _rundir


def receipt(run_id="run-1", *, max_attempts=4, quorum="all"):
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "decision_id": "decision-1", "seat_ids": ["a", "b"],
        "option_ids": ["yes", "no"], "quorum_rule": quorum,
        "max_attempts": max_attempts, "require_human_approval": False,
    }


def event(name, generation=1, **fields):
    return {"event": name, "schema_version": 1, "generation": generation, **fields}


def prepared(run_id="run-1", generation=1, **receipt_kwargs):
    value = receipt(run_id, **receipt_kwargs)
    return value, [(generation, event(
        "run_prepared", generation, run_id=run_id,
        receipt_sha256=replay._sha(value)))]


def turn_events(*, seat="a", turn="turn-a-0", ordinal=0, attempt="g1-a0",
                generation=1, digest=None, valid=True, option="yes"):
    digest = digest or hashlib.sha256((seat + turn).encode()).hexdigest()
    spec = hashlib.sha256((attempt + "-spec").encode()).hexdigest()
    return [
        (generation, event("turn_prepared", generation, decision_id="decision-1",
                           seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                           request_digest=digest)),
        (generation, event("attempt_started", generation,
                           attempt_id=attempt, decision_id="decision-1", seat_id=seat,
                           turn_id=turn, turn_ordinal=ordinal,
                           launch_spec_sha256=spec)),
        (generation, event("attempt_finished", generation, attempt_id=attempt,
                           launch_spec_sha256=spec, ballot_valid=valid,
                           transport_ok=True, exit_code=0, timed_out=False,
                           parser_valid=True)),
        (generation, event("ballot_accepted", generation, attempt_id=attempt,
                           seat_id=seat, turn_id=turn, turn_ordinal=ordinal,
                           decision="vote", option_id=option)),
    ]


class ReplayTests(unittest.TestCase):
    def run_replay(self, records, *, value=None, owner_generation=2):
        value = value or receipt()
        return replay.replay_checkpoint(value, records, owner_generation)

    def test_requires_first_unique_receipt_bound_run_prepared(self):
        value, records = prepared()
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records[1:])
        duplicate = records + records
        with self.assertRaises(replay.ReplayError):
            self.run_replay(duplicate)
        wrong = [(1, event("run_prepared", 1, run_id="run-1",
                           receipt_sha256="0" * 64))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(wrong, value=value)

    def test_receipt_quorum_alias_is_not_an_alternate_schema(self):
        value, records = prepared()
        value.pop("quorum_rule")
        value["quorum"] = "all"
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_receipt_requires_canonical_approval_field(self):
        value, records = prepared()
        value.pop("require_human_approval")
        records[0] = (1, event(
            "run_prepared", 1, run_id="run-1",
            receipt_sha256=replay._sha(value),
        ))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_recovery_transition_seals_the_exact_command_batch(self):
        value, records = prepared()
        records.extend([
            (1, event("human_command", 1, command_id="cmd-1",
                      sequence=1, action="cancel")),
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "CANCELLED",
                "reason": "human_cancel", "decision_option": None,
                "recovery_kind": "human_command_eof",
                "command_batch_sha256": replay.command_batch_sha256((
                    replay.ReplayCommand("cmd-1", 1, "cancel"),)),
            })),
        ])
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.status, "CANCELLED")
        self.assertEqual(checkpoint.applied_command_ids, ("cmd-1",))

    def test_recovery_transition_batch_mutation_is_rejected(self):
        value, records = prepared()
        records.extend([
            (1, event("human_command", 1, command_id="cmd-1",
                      sequence=1, action="cancel")),
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "CANCELLED",
                "reason": "human_cancel", "decision_option": None,
                "recovery_kind": "human_command_eof",
                "command_batch_sha256": "0" * 64,
            })),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_recovery_metadata_must_be_all_or_none(self):
        value, records = prepared()
        records.extend([
            (1, event("human_command", 1, command_id="cmd-1",
                      sequence=1, action="cancel")),
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "CANCELLED",
                "reason": "human_cancel", "decision_option": None,
                "recovery_kind": "human_command_eof",
            })),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_prepared_human_cancel_requires_recovery_metadata(self):
        value, records = prepared()
        records.extend([
            (1, event("human_command", 1, command_id="cmd-1",
                      sequence=1, action="cancel")),
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "CANCELLED",
                "reason": "human_cancel", "decision_option": None,
            })),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_receipt_is_snapshotted_before_policy_and_hash_reads(self):
        class StatefulReceipt(dict):
            def __init__(self, value):
                super().__init__(value)
                self.flipped = False

            def get(self, key, default=None):
                if key == "seat_ids" and not self.flipped:
                    self.flipped = True
                    return ["a", "b"]
                if self.flipped and key == "seat_ids":
                    return ["evil", "other"]
                return super().get(key, default)

        value, records = prepared()
        # A single canonical snapshot observes the underlying dict consistently;
        # accessor tricks cannot split policy parsing from the receipt hash.
        checkpoint = self.run_replay(records, value=StatefulReceipt(value))
        self.assertEqual(checkpoint.decision_id, "decision-1")

    def test_segment_generation_is_authoritative(self):
        value, records = prepared()
        bad = [(2, records[0][1])]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad, value=value)
        bad_order = records + [(0, event("cleanup_receipt", 0))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad_order, value=value)

    def test_journal_tag_reader_preserves_and_checks_segment_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = event("run_prepared", 1, run_id="run-1", receipt_sha256="0" * 64)
            path = _rundir._journal_path(directory, 1)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_rundir._journal_line(valid) + "\n")
            tagged, torn = _rundir.journal_read_tagged(directory)
            self.assertFalse(torn)
            self.assertEqual(tagged, [(1, valid)])
            tampered = dict(valid, generation=2)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_rundir._journal_line(tampered) + "\n")
            with self.assertRaises(_rundir.JournalCorruptError):
                _rundir.journal_read_tagged(directory)

    def test_illegal_state_transition_is_not_replayed(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, from_="PREPARED", to="DECIDED",
                                 reason="forged")))
        # The production field is named `from`; build it without Python keyword syntax.
        records[-1][1]["from"] = records[-1][1].pop("from_")
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_attempt_finish_must_match_start_generation_and_spec(self):
        value, records = prepared()
        records.extend(turn_events())
        start = records[-3][1]
        records[-1] = (1, event("attempt_finished", 1,
                                attempt_id=start["attempt_id"],
                                launch_spec_sha256="f" * 64, ballot_valid=True))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_ballot_requires_valid_finished_attempt(self):
        value, records = prepared()
        records.extend(turn_events(valid=False))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_unmatched_start_is_uncertain_budget_consuming_and_not_pending(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events()[:2])
        checkpoint = self.run_replay(records, value=value)
        self.assertTrue(checkpoint.uncertain_spend)
        self.assertEqual(checkpoint.attempts[0].phase, "indeterminate")
        self.assertEqual(checkpoint.next_ordinal, 1)
        self.assertIsNone(checkpoint.pending_turn)

    def test_candidate_is_recomputed_not_copied_from_candidate_selected(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="a", turn="turn-a", ordinal=0, attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="turn-b", ordinal=1, attempt="g1-a1"))
        records.append((1, event("candidate_selected", 1, option_id="no")))
        records.append((1, event("state_transition", 1, **{
            "from": "RUNNING", "to": "DECIDED", "reason": "consensus",
            "decision_option": "yes"})))
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.candidate_option, "yes")
        self.assertEqual(checkpoint.decision_option, "yes")

    def test_consensus_candidate_must_be_followed_by_its_transition(self):
        value, records = prepared(quorum=1)
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events())
        # A second turn after a durable quorum ballot is an impossible crash
        # prefix.  Recovery must not attach it as pending work.
        records.append((1, event("turn_prepared", 1, decision_id="decision-1",
                                  seat_id="b", turn_id="turn-b-1", turn_ordinal=1,
                                  request_digest="b" * 64)))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_decided_without_recomputed_candidate_is_rejected(self):
        value, records = prepared()
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "RUNNING", "reason": "started"})),
            (1, event("candidate_selected", 1, option_id="yes")),
            (1, event("state_transition", 1, **{
                "from": "RUNNING", "to": "DECIDED", "reason": "consensus",
                "decision_option": "yes"})),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_duplicate_or_regressed_ballots_are_rejected(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events())
        records.extend(turn_events(seat="a", turn="turn-a-1", ordinal=0,
                                   attempt="g1-a1"))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_later_ballot_revision_replaces_same_seat_not_cumulative(self):
        value, records = prepared(quorum="all")
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="a", turn="turn-a-0", ordinal=0,
                                   attempt="g1-a0", option="yes"))
        records.extend(turn_events(seat="a", turn="turn-a-1", ordinal=1,
                                   attempt="g1-a1", option="no"))
        checkpoint = self.run_replay(records, value=value)
        self.assertIsNone(checkpoint.candidate_option)

    def test_human_cancel_precedes_later_approval_transition(self):
        value = receipt()
        value["require_human_approval"] = True
        records = [(1, event("run_prepared", 1, run_id=value["run_id"],
                              receipt_sha256=replay._sha(value)))]
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "RUNNING", "reason": "started"})),
        ])
        records.extend(turn_events(seat="a", turn="turn-a", ordinal=0,
                                   attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="turn-b", ordinal=1,
                                   attempt="g1-a1"))
        records.append((1, event("state_transition", 1, **{
            "from": "RUNNING", "to": "WAITING_HUMAN", "reason": "approval_required"})))
        records.append((1, event("human_command", 1, command_id="cmd-2", sequence=1,
                                 action="cancel")))
        records.append((1, event("state_transition", 1, **{
            "from": "WAITING_HUMAN", "to": "DECIDED", "reason": "human_approved",
            "decision_option": "yes"})))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_human_minimum_sequence_and_equal_priority_match_kernel(self):
        value = receipt()
        value["require_human_approval"] = True
        records = [(1, event("run_prepared", 1, run_id=value["run_id"],
                              receipt_sha256=replay._sha(value)))]
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="a", turn="turn-a", ordinal=0,
                                   attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="turn-b", ordinal=1,
                                   attempt="g1-a1"))
        records.append((1, event("state_transition", 1, **{
            "from": "RUNNING", "to": "WAITING_HUMAN", "reason": "approval_required"})))
        records.extend([
            (1, event("human_command", 1, command_id="cmd-approve", sequence=1,
                       action="approve")),
            (1, event("human_command", 1, command_id="cmd-cancel-late", sequence=2,
                       action="cancel")),
            (1, event("state_transition", 1, **{
                "from": "WAITING_HUMAN", "to": "DECIDED", "reason": "human_approved",
                "decision_option": "yes"})),
        ])
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.status, "DECIDED")

        equal = list(records[:-1])
        equal[-2] = (1, event("human_command", 1, command_id="cmd-cancel-equal",
                               sequence=1, action="cancel"))
        equal[-1] = (1, event("human_command", 1, command_id="cmd-approve-equal",
                               sequence=1, action="approve"))
        equal.append((1, event("state_transition", 1, **{
            "from": "WAITING_HUMAN", "to": "CANCELLED", "reason": "human_cancel"})))
        checkpoint = self.run_replay(equal, value=value)
        self.assertEqual(checkpoint.status, "CANCELLED")
        self.assertEqual([(item.sequence, item.action)
                          for item in checkpoint.applied_commands],
                         [(1, "cancel"), (1, "approve")])
        self.assertEqual(checkpoint.pending_commands, ())

    def test_command_crash_prefix_is_pending_and_bound_into_digest(self):
        value = receipt()
        value["require_human_approval"] = True
        records = [(1, event("run_prepared", 1, run_id=value["run_id"],
                              receipt_sha256=replay._sha(value))),
                   (1, event("state_transition", 1, **{
                       "from": "PREPARED", "to": "RUNNING", "reason": "started"}))]
        records.extend(turn_events(seat="a", turn="turn-a", ordinal=0,
                                   attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="turn-b", ordinal=1,
                                   attempt="g1-a1"))
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "RUNNING", "to": "WAITING_HUMAN",
                "reason": "approval_required"})),
            (1, event("human_command", 1, command_id="cmd-1", sequence=1,
                      action="cancel")),
        ])
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.applied_commands, ())
        self.assertEqual(checkpoint.pending_commands[0].action, "cancel")
        changed = replace(
            checkpoint,
            pending_commands=(replay.ReplayCommand("cmd-1", 1, "approve"),))
        from _deliberation import _restore_digest
        self.assertNotEqual(changed.digest, _restore_digest(changed))

    def test_command_batch_requires_contiguous_sequences_and_immediate_transition(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        gap = records + [(1, event("human_command", 1, command_id="cmd-2",
                                   sequence=2, action="cancel"))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(gap, value=value)
        intervening = records + [
            (1, event("human_command", 1, command_id="cmd-1",
                      sequence=1, action="cancel")),
            (1, event("journal_repaired", 1, repaired_generation=0)),
        ]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(intervening, value=value)

    def test_transition_reason_and_decision_option_match_kernel_boundary(self):
        value, records = prepared()
        bad_reason = records + [(1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "consensus"}))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad_reason, value=value)
        bad_option = records + [(1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "CANCELLED", "reason": "cancelled",
            "decision_option": "yes"}))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad_option, value=value)

    def test_turn_material_cannot_precede_running_transition(self):
        value, records = prepared()
        records.extend(turn_events())
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_invalid_executor_evidence_cannot_accept_ballot(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events())
        for index, (generation, record) in enumerate(records):
            if record.get("event") == "attempt_finished":
                records[index] = (generation, dict(record, transport_ok=False))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_approval_policy_cannot_be_bypassed_by_direct_decided_transition(self):
        value = receipt()
        value["require_human_approval"] = True
        records = prepared(value["run_id"])[1]
        records.extend(turn_events()[:0])
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "RUNNING", "reason": "started"})),
        ])
        records.extend(turn_events(seat="a", turn="turn-a", ordinal=0,
                                   attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="turn-b", ordinal=1,
                                   attempt="g1-a1"))
        records.append((1, event("state_transition", 1, **{
            "from": "RUNNING", "to": "DECIDED", "reason": "consensus",
            "decision_option": "yes"})))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_event_projection_drops_private_nested_fields(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        turn = event("turn_prepared", 1, decision_id="decision-1", seat_id="a",
                     turn_id="turn-a-0", turn_ordinal=0,
                     request_digest="a" * 64,
                     metadata={"prompt": "TOP_SECRET", "argv": ["SECRET"]})
        records.append((1, turn))
        records.append((1, event("attempt_started", 1, attempt_id="g1-a0",
                                  decision_id="decision-1", seat_id="a",
                                  turn_id="turn-a-0", turn_ordinal=0,
                                  launch_spec_sha256="b" * 64,
                                  metadata={"prompt": "TOP_SECRET"})))
        records.append((1, event("attempt_finished", 1, attempt_id="g1-a0",
                                  launch_spec_sha256="b" * 64, ballot_valid=False,
                                  transport_ok=True, timed_out=False, parser_valid=True,
                                  metadata={"prompt": "TOP_SECRET"})))
        records.append((1, event("cleanup_receipt", 1, verified=True, clean=True,
                                  metadata={"prompt": "TOP_SECRET_CLEANUP"})))
        checkpoint = self.run_replay(records, value=value)
        encoded = json.dumps(list(checkpoint.transcript_events), default=str)
        self.assertNotIn("TOP_SECRET", encoded)

    def test_public_projection_rejects_private_reason_and_omits_backend_identity(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "C:/TOP_SECRET_PATH"})))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events()[:2])
        records[-1] = (1, dict(records[-1][1], transport="TOP_SECRET_BACKEND",
                                command_identity_sha256="TOP_SECRET_HASH"))
        checkpoint = self.run_replay(records, value=value)
        encoded = json.dumps(list(checkpoint.transcript_events), default=str)
        self.assertNotIn("TOP_SECRET_BACKEND", encoded)
        self.assertNotIn("TOP_SECRET_HASH", encoded)

    def test_turn_ordinals_must_not_skip(self):
        value, records = prepared()
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "RUNNING", "reason": "started"})),
            (1, event("turn_prepared", 1, decision_id="decision-1", seat_id="a",
                       turn_id="turn-a-50", turn_ordinal=50,
                       request_digest="a" * 64)),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_two_prepared_turns_without_attempt_are_not_collapsed(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.append((1, event("turn_prepared", 1, decision_id="decision-1",
                                  seat_id="a", turn_id="turn-a-0", turn_ordinal=0,
                                  request_digest="a" * 64)))
        records.append((1, event("turn_prepared", 1, decision_id="decision-1",
                                  seat_id="b", turn_id="turn-b-1", turn_ordinal=1,
                                  request_digest="b" * 64)))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_terminal_material_after_state_is_rejected(self):
        value, records = prepared()
        records.extend([
            (1, event("state_transition", 1, **{
                "from": "PREPARED", "to": "CANCELLED", "reason": "cancelled"})),
            (1, event("turn_prepared", 1, decision_id="decision-1", seat_id="a",
                       turn_id="late", turn_ordinal=0,
                       request_digest="a" * 64)),
        ])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_checkpoint_digest_is_stable_and_transcript_has_no_prompt(self):
        value, records = prepared()
        records.extend(turn_events()[:1])
        records[1][1]["private_prompt"] = "TOP_SECRET"
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_checkpoint_transcript_is_defensively_immutable(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events()[:1])
        checkpoint = self.run_replay(records, value=value)
        with self.assertRaises(TypeError):
            checkpoint.transcript_events[0]["event"] = "tampered"


if __name__ == "__main__":
    unittest.main()
