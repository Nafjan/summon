"""Adversarial, provider-inert tests for deliberation journal replay."""

from __future__ import annotations

import hashlib
import json
import copy
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
import _deliberation_context as context
import _rundir


def receipt(run_id="run-1", *, max_attempts=4, quorum="all"):
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "question_sha256": hashlib.sha256(b"question").hexdigest(),
        "decision_id": "decision-1", "seat_ids": ["a", "b"],
        "option_ids": ["yes", "no"], "quorum_rule": quorum,
        "max_attempts": max_attempts, "require_human_approval": False,
        "rounds": 1, "deadline_unix_ms": 4_000_000_000_000,
    }


def event(name, generation=1, **fields):
    return {"event": name, "schema_version": 1, "generation": generation, **fields}


def prepared(run_id="run-1", generation=1, **receipt_kwargs):
    value = receipt(run_id, **receipt_kwargs)
    return value, [(generation, event(
        "run_prepared", generation, run_id=run_id,
        receipt_sha256=replay._sha(value)))]


def legacy_context_projection():
    now = 2_000_000
    packet = {
        "schema": context.PACKET_SCHEMA,
        "source": {"kind": "git_commit", "revision": "a" * 40,
                   "source_digest": "1" * 64,
                   "captured_at_unix_ms": now - 100},
        "freshness_policy": {"fresh_max_age_ms": 1_000,
                             "hard_max_age_ms": 20_000,
                             "hard_max_revision_delta": 3},
        "entries": [{"id": "constraint-1", "kind": "constraint",
                     "body": "Historical constraint.",
                     "provenance": {"kind": "authored",
                                    "source_sha256": "3" * 64}}],
    }
    observation = {
        "schema": context.OBSERVATION_SCHEMA, "kind": "git_commit",
        "captured_revision": "a" * 40, "current_revision": "a" * 40,
        "current_source_digest": "1" * 64, "relation": "same",
        "revision_delta": 0, "verification_method": "git-readback",
        "observed_at_unix_ms": now,
    }
    current = context.private_projection(context.bind_context(
        packet, observation, run_id="run-1", decision_id="decision-1",
        unix_now_ms=now, runs_root_sha256="4" * 64))
    current["schema"] = context.LEGACY_BINDING_SCHEMA
    current.pop("runs_root_sha256")
    identity = {
        "schema": context.LEGACY_BINDING_SCHEMA,
        "packet_sha256": current["packet_sha256"],
        "source_revision_sha256": current["source_revision_sha256"],
        "run_id": current["run_id"], "decision_id": current["decision_id"],
        "bound_at_unix_ms": current["bound_at_unix_ms"], "state": current["state"],
        "actual_age_ms": current["actual_age_ms"],
        "actual_revision_delta": current["actual_revision_delta"],
        "mismatch_reasons": current["mismatch_reasons"],
        "observation_identity": {key: current["observation"][key] for key in (
            "kind", "captured_revision", "current_revision", "current_source_digest",
            "relation", "revision_delta", "verification_method")},
        "acceptance_sha256": None, "routing_authority": False,
    }
    current["binding_sha256"] = context._sha(identity)
    return current


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

    def test_receipt_question_and_created_at_metadata_are_bounded(self):
        value, records = prepared()
        missing = dict(value)
        missing.pop("question_sha256")
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=missing)
        bad_timestamp = dict(value, created_at="C:/private/secret")
        records_bad = [(1, event("run_prepared", 1, run_id="run-1",
                                 receipt_sha256=replay._sha(bad_timestamp)))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records_bad, value=bad_timestamp)

    def test_historical_v1_context_replays_read_only_but_is_not_current_authority(self):
        value = receipt()
        value["durable_context"] = legacy_context_projection()
        records = [(1, event("run_prepared", 1, run_id="run-1",
                             receipt_sha256=replay._sha(value)))]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)
        checkpoint = replay.replay_checkpoint(
            value, records, 2, allow_legacy_context=True)
        self.assertEqual(checkpoint.status, "PREPARED")
        with self.assertRaises(replay.ReplayError):
            replay._receipt_metadata(value)
        replay._receipt_metadata(value, allow_legacy_context=True)

    def test_public_audit_fields_are_schema_validated(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.append((1, event("turn_prepared", 1, decision_id="decision-1",
                                 seat_id="a", turn_id="turn-a-0", turn_ordinal=0,
                                 request_digest="a" * 64)))
        records.append((1, event("ballot_inert", 1, attempt_id="SECRET-TOKEN",
                                 seat_id="a", turn_id="SECRET-TURN")))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(valid=False))
        records[-1] = (1, event("ballot_inert", 1, attempt_id="g1-a0",
                                 seat_id="a", turn_id="turn-a-0"))
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.status, "RUNNING")

    def test_attempt_model_identity_is_public_bounded_evidence(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        attempts = turn_events()
        attempts.insert(-1, (1, event(
            "attempt_model_identity", 1, attempt_id="g1-a0",
            model_served="claude-opus-4-7", model_targeted="claude-opus-4-7",
            served_model_evidence="reported")))
        records.extend(attempts)
        checkpoint = self.run_replay(records, value=value)
        public_identity = next(
            item for item in checkpoint.transcript_events
            if item.get("event") == "attempt_model_identity")
        self.assertNotIn("model_served", public_identity)
        self.assertNotIn("model_targeted", public_identity)
        self.assertEqual(
            public_identity["model_served_sha256"],
            hashlib.sha256(b"claude-opus-4-7").hexdigest())

        bad = list(records)
        bad[-2] = (1, event(
            "attempt_model_identity", 1, attempt_id="g1-a0",
            model_served=r"C:\\private\\secret", model_targeted=None))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad, value=value)

        bad_evidence = list(records)
        bad_evidence[-2] = (1, event(
            "attempt_model_identity", 1, attempt_id="g1-a0",
            model_served="claude-opus-4-7", model_targeted="claude-opus-4-7",
            served_model_evidence="caller_claimed"))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(bad_evidence, value=value)

    def test_attempt_model_identity_requires_finished_order_generation_and_uniqueness(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        base = turn_events()
        identity = (1, event(
            "attempt_model_identity", 1, attempt_id="g1-a0",
            model_served="claude-opus-4-7", model_targeted="claude-opus-4-7",
            served_model_evidence="reported"))

        before_finish = records + base[:2] + [identity] + base[2:]
        with self.assertRaisesRegex(
                replay.ReplayError, "matching finished attempt"):
            self.run_replay(before_finish, value=value)

        after_ballot = records + base + [identity]
        with self.assertRaisesRegex(replay.ReplayError, "after its ballot"):
            self.run_replay(after_ballot, value=value)

        duplicate = records + base[:3] + [identity, identity] + base[3:]
        with self.assertRaisesRegex(replay.ReplayError, "duplicate model identity"):
            self.run_replay(duplicate, value=value)

        wrong_generation = records + base[:3] + [(2, event(
            "attempt_model_identity", 2, attempt_id="g1-a0",
            model_served="claude-opus-4-7",
            model_targeted="claude-opus-4-7",
            served_model_evidence="reported"))]
        with self.assertRaisesRegex(
                replay.ReplayError, "matching finished attempt"):
            self.run_replay(wrong_generation, value=value, owner_generation=3)

    def test_journal_repair_is_first_record_of_its_segment(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.append((2, event("journal_repaired", 2,
                                 repaired_generation=1)))
        checkpoint = self.run_replay(records, value=value, owner_generation=3)
        self.assertEqual(checkpoint.status, "RUNNING")

        gapped = [records[0], records[1],
                  (3, event("journal_repaired", 3, repaired_generation=1))]
        checkpoint = self.run_replay(gapped, value=value, owner_generation=4)
        self.assertEqual(checkpoint.status, "RUNNING")

        value, records = prepared()
        records.append((1, event("journal_repaired", 1,
                                 repaired_generation="SECRET-C:/private")))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_attempt_exit_code_is_bounded_before_public_projection(self):
        value, records = prepared()
        records.extend([*[(1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"}))],
            *[(1, event("turn_prepared", 1, decision_id="decision-1",
                         seat_id="a", turn_id="turn-a-0", turn_ordinal=0,
                         request_digest="a" * 64))],
            *[(1, event("attempt_started", 1, attempt_id="g1-a0",
                         decision_id="decision-1", seat_id="a",
                         turn_id="turn-a-0", turn_ordinal=0,
                         launch_spec_sha256="b" * 64))],
            *[(1, event("attempt_finished", 1, attempt_id="g1-a0",
                         launch_spec_sha256="b" * 64, ballot_valid=False,
                         transport_ok=True, exit_code="SECRET-C:/private",
                         timed_out=False, parser_valid=True))]])
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_receipt_quorum_alias_is_not_an_alternate_schema(self):
        value, records = prepared()
        value.pop("quorum_rule")
        value["quorum"] = "all"
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)

    def test_schedule_fields_are_canonical_and_sealed(self):
        value, records = prepared()
        first = self.run_replay(records, value=value)
        changed_rounds = dict(value, rounds=2)
        changed_deadline = dict(value, deadline_unix_ms=4_000_000_000_001)
        second_records = [(1, event(
            "run_prepared", 1, run_id=changed_rounds["run_id"],
            receipt_sha256=replay._sha(changed_rounds)))]
        third_records = [(1, event(
            "run_prepared", 1, run_id=changed_deadline["run_id"],
            receipt_sha256=replay._sha(changed_deadline)))]
        second = self.run_replay(second_records, value=changed_rounds)
        third = self.run_replay(third_records, value=changed_deadline)
        self.assertNotEqual(first.schedule_digest, second.schedule_digest)
        self.assertNotEqual(first.schedule_digest, third.schedule_digest)
        self.assertNotEqual(first.digest, second.digest)
        self.assertNotEqual(first.digest, third.digest)

    def test_schedule_rejects_invalid_and_missing_fields(self):
        value, records = prepared()
        for field, bad in (("rounds", 0), ("rounds", True),
                           ("rounds", 11), ("deadline_unix_ms", 0),
                           ("deadline_unix_ms", True),
                           ("deadline_unix_ms", 1 << 63),
                           ("deadline_unix_ms", 1.5)):
            changed = dict(value, **{field: bad})
            with self.assertRaises(replay.ReplayError):
                self.run_replay(records, value=changed)
        for field in ("rounds", "deadline_unix_ms"):
            changed = dict(value)
            changed.pop(field)
            with self.assertRaises(replay.ReplayError):
                self.run_replay(records, value=changed)

    def test_schedule_seat_order_and_round_bound_are_enforced(self):
        value, records = prepared()
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="b", turn="wrong-seat", ordinal=0,
                                    attempt="g1-wrong"))
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=value)
        value = dict(value, rounds=1)
        records = prepared()[1]
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="a", turn="a0", ordinal=0, attempt="g1-a0"))
        records.extend(turn_events(seat="b", turn="b1", ordinal=1, attempt="g1-a1"))
        records.append((1, event("turn_prepared", 1, decision_id="decision-1",
                                  seat_id="a", turn_id="a2", turn_ordinal=2,
                                  request_digest="a" * 64)))
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

    def test_schedule_is_required_bounded_and_sealed(self):
        value, records = prepared()
        checkpoint = self.run_replay(records, value=value)
        self.assertEqual(checkpoint.schedule_digest,
                         replay.schedule_digest(1, 4_000_000_000_000))
        for field, invalid in (("rounds", 0), ("rounds", 11),
                               ("rounds", True), ("deadline_unix_ms", 0),
                               ("deadline_unix_ms", 1 << 63),
                               ("deadline_unix_ms", False)):
            changed = dict(value)
            changed[field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(replay.ReplayError):
                self.run_replay(records, value=changed)
        missing = dict(value)
        del missing["rounds"]
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=missing)

    def test_schedule_digest_mutation_is_not_accepted(self):
        value, records = prepared()
        changed = dict(value)
        changed["deadline_unix_ms"] = 4_000_000_000_001
        with self.assertRaises(replay.ReplayError):
            self.run_replay(records, value=changed)

    def test_turn_ordinal_cannot_escape_receipt_schedule(self):
        value, records = prepared()
        records.extend(turn_events(ordinal=2, turn="turn-a-2", attempt="g1-a2"))
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
        value["rounds"] = 2
        records[0] = (1, event("run_prepared", 1, run_id=value["run_id"],
                               receipt_sha256=replay._sha(value)))
        records.append((1, event("state_transition", 1, **{
            "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
        records.extend(turn_events(seat="a", turn="turn-a-0", ordinal=0,
                                   attempt="g1-a0", option="yes"))
        records.extend(turn_events(seat="b", turn="turn-b-1", ordinal=1,
                                   attempt="g1-a1", option="no"))
        records.extend(turn_events(seat="a", turn="turn-a-2", ordinal=2,
                                   attempt="g1-a2", option="yes"))
        checkpoint = self.run_replay(records, value=value)
        # The latest a-seat revision is ``yes``; counting the stale first
        # ballot cumulatively would incorrectly produce a unanimous candidate.
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


    def test_atomic_command_batch_is_typed_and_checkpoint_sealed(self):
        commands = (replay.ReplayCommand("cmd-c", 1, "cancel"),)
        digest = replay.command_batch_sha256(commands)
        value, records = prepared()
        records.append((1, event(
            "human_command_batch", 1,
            batch_id="batch-" + digest[:32], source_generation=1,
            commands=[command.__dict__ for command in commands],
            command_batch_sha256=digest)))
        checkpoint = replay.replay_checkpoint(value, records, 2)
        self.assertEqual(checkpoint.pending_commands, commands)
        self.assertIsNotNone(checkpoint.pending_command_batch)
        self.assertEqual(checkpoint.pending_command_batch.digest, digest)
        self.assertEqual(checkpoint.pending_command_batch.source_generation, 1)
        self.assertNotEqual(checkpoint.digest,
                            replay.replay_checkpoint(value, prepared()[1], 2).digest)

    def test_atomic_command_batch_rejects_mutated_identity_action_count_and_digest(self):
        commands = (replay.ReplayCommand("cmd-a", 1, "approve"),
                    replay.ReplayCommand("cmd-c", 1, "cancel"))
        digest = replay.command_batch_sha256(commands)
        original = event(
            "human_command_batch", 1,
            batch_id="batch-" + digest[:32], source_generation=1,
            commands=[command.__dict__ for command in commands],
            command_batch_sha256=digest)
        mutations = []
        changed = copy.deepcopy(original)
        changed["commands"][0]["command_id"] = "cmd-other"
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["commands"][0]["action"] = "deny"
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["commands"].pop()
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["command_batch_sha256"] = "0" * 64
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["batch_id"] = "batch-" + "0" * 32
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["commands"][1]["sequence"] = 10 ** 12
        far_commands = tuple(replay.ReplayCommand(**item)
                             for item in changed["commands"])
        changed["command_batch_sha256"] = replay.command_batch_sha256(far_commands)
        changed["batch_id"] = "batch-" + changed["command_batch_sha256"][:32]
        mutations.append(changed)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(replay.ReplayError):
                replay.replay_checkpoint(receipt(), prepared()[1] + [(1, mutation)], 2)

    def test_atomic_command_batch_cannot_cross_generation(self):
        command = replay.ReplayCommand("cmd-1", 1, "cancel")
        digest = replay.command_batch_sha256((command,))
        record = event(
            "human_command_batch", 1,
            batch_id="batch-" + digest[:32], source_generation=2,
            commands=[command.__dict__], command_batch_sha256=digest)
        with self.assertRaises(replay.ReplayError):
            replay.replay_checkpoint(receipt(), prepared()[1] + [(1, record)], 3)

    def test_non_recovery_transition_rejects_command_generation_metadata(self):
        value, records = prepared()
        records.append((1, event(
            "state_transition", 1, **{
                "from": "PREPARED", "to": "RUNNING", "reason": "started",
                "decision_option": None,
                "command_source_generation": r"SECRET-C:\private",
            })))
        with self.assertRaises(replay.ReplayError):
            replay.replay_checkpoint(value, records, 2)

    def test_legacy_eof_commands_are_visible_but_not_typed_recoverable(self):
        records = prepared()[1] + [(1, event(
            "human_command", 1, command_id="cmd-1", sequence=1,
            action="cancel"))]
        checkpoint = replay.replay_checkpoint(receipt(), records, 2)
        self.assertEqual(checkpoint.pending_commands[0].action, "cancel")
        self.assertIsNone(checkpoint.pending_command_batch)


if __name__ == "__main__":
    unittest.main()
