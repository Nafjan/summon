"""Mutation-sensitive tests for provider-inert deliberation restoration."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _deliberation import (  # noqa: E402
    AdapterResult, AttemptBinding, AttemptLedger, BallotBook, CleanupReceipt,
    DeliberationEngine, DeliberationError, DeliberationPolicy,
    DuplicateAttemptError, ExecutionEvidence, LaunchSpec, LaunchToken,
    NextAction, OwnershipLostError, RunState,
)
import _rundir  # noqa: E402
import _deliberation_replay as replay  # noqa: E402


class NoProviderAdapter:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, context):
        self.calls.append("prepare")
        raise AssertionError("restore contacted provider preparation")

    def revalidate(self, spec, context):
        self.calls.append("revalidate")
        raise AssertionError("restore contacted provider revalidation")

    def launch(self, spec, token):
        self.calls.append("launch")
        raise AssertionError("restore contacted provider launch")

    def cleanup(self):
        self.calls.append("cleanup")
        raise AssertionError("restore contacted provider cleanup")


def policy(*, max_attempts: int = 4) -> DeliberationPolicy:
    return DeliberationPolicy("decision-1", ("a", "b"), ("yes", "no"),
                              "all", max_attempts, False)


def receipt_for(value: DeliberationPolicy) -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": "run-1",
        "decision_id": value.decision_id, "seat_ids": list(value.seat_ids),
        "option_ids": list(value.option_ids), "quorum_rule": value.quorum_rule,
        "max_attempts": value.max_attempts,
        "require_human_approval": value.require_human_approval,
    }


def attempt(attempt_id: str, seat_id: str, ordinal: int, *,
            phase: str = "finished") -> SimpleNamespace:
    return SimpleNamespace(
        attempt_id=attempt_id, generation=1, decision_id="decision-1",
        seat_id=seat_id, turn_id=f"turn-{ordinal}", turn_ordinal=ordinal,
        launch_spec_digest=(str(ordinal + 1) * 64)[:64], phase=phase,
        ballot_valid=(True if phase == "finished" else None),
    )


def ballot(attempt_id: str, seat_id: str, ordinal: int,
           option_id: str = "yes") -> SimpleNamespace:
    return SimpleNamespace(
        decision_id="decision-1", seat_id=seat_id,
        turn_id=f"turn-{ordinal}", attempt_id=attempt_id,
        turn_ordinal=ordinal, decision="vote", option_id=option_id,
        confidence=None, evidence_refs=(),
    )


def checkpoint(**changes) -> SimpleNamespace:
    values = dict(
        prior_generation=1, decision_id="decision-1", status="RUNNING",
        candidate_option=None, decision_option=None, termination_reason="started",
        attempts=(), ballots=(), pending_turn=None, next_ordinal=0,
        uncertain_spend=False,
    )
    values.update(changes)
    def fields(value, names):
        return {name: getattr(value, name, None) for name in names}
    default_receipt = receipt_for(policy())
    values.setdefault("receipt_sha256", hashlib.sha256(json.dumps(
        default_receipt, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).hexdigest())
    values.setdefault("run_id", "run-1")
    values.setdefault("policy_digest", hashlib.sha256(json.dumps({
        "decision_id": "decision-1", "seat_ids": ["a", "b"],
        "option_ids": ["yes", "no"], "quorum_rule": "all",
        "max_attempts": 4, "require_human_approval": False,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    values.setdefault("applied_commands", ())
    values.setdefault("pending_commands", ())
    values.setdefault("transcript_events", ())
    payload = {
        "receipt_sha256": values["receipt_sha256"], "run_id": values["run_id"],
        "policy_digest": values["policy_digest"],
        "prior_generation": values["prior_generation"], "status": values["status"],
        "termination_reason": values["termination_reason"],
        "candidate_option": values["candidate_option"],
        "decision_option": values["decision_option"],
        "attempts": [fields(item, ("attempt_id", "generation", "decision_id",
                                    "seat_id", "turn_id", "turn_ordinal",
                                    "launch_spec_digest", "phase", "ballot_valid"))
                     for item in values["attempts"]],
        "ballots": [fields(item, ("decision_id", "seat_id", "turn_id", "attempt_id",
                                   "turn_ordinal", "decision", "option_id",
                                   "confidence", "evidence_refs"))
                    for item in values["ballots"]],
        "pending_turn": (None if values["pending_turn"] is None else fields(
            values["pending_turn"], ("decision_id", "seat_id", "turn_id",
                                      "turn_ordinal", "request_digest"))),
        "next_ordinal": values["next_ordinal"],
        "applied_commands": [fields(item, ("command_id", "sequence", "action"))
                             for item in values["applied_commands"]],
        "pending_commands": [fields(item, ("command_id", "sequence", "action"))
                             for item in values["pending_commands"]],
        "transcript_events": list(values["transcript_events"]),
        "uncertain_spend": values["uncertain_spend"],
    }
    values["digest"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    return SimpleNamespace(**values)


def restore(value, *, configured_policy=None, owner=lambda: True,
            events=None, adapter=None):
    events = [] if events is None else events
    adapter = NoProviderAdapter() if adapter is None else adapter
    configured = configured_policy or policy()
    if configured != policy():
        configured_receipt = receipt_for(configured)
        receipt_sha = hashlib.sha256(json.dumps(
            configured_receipt, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode()).hexdigest()
        if isinstance(value, dict):
            value["receipt_sha256"] = receipt_sha
        else:
            value.receipt_sha256 = receipt_sha
        policy_sha = hashlib.sha256(json.dumps({
            "decision_id": configured.decision_id,
            "seat_ids": list(configured.seat_ids),
            "option_ids": list(configured.option_ids),
            "quorum_rule": configured.quorum_rule,
            "max_attempts": configured.max_attempts,
            "require_human_approval": configured.require_human_approval,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if isinstance(value, dict):
            value["policy_digest"] = policy_sha
            value["digest"] = _digest_for(value)
        else:
            value.policy_digest = policy_sha
            value.digest = _digest_for(value)
    configured_receipt = receipt_for(configured)
    engine = DeliberationEngine._restore_sealed(
        value, configured, adapter, 2, events.append,
        deadline=100.0, clock=lambda: 0.0,
        receipt=configured_receipt,
        owner_is_current=owner,
        cancel_requested=lambda: False,
    )
    return engine, adapter, events


def _digest_for(value) -> str:
    from _deliberation import _restore_digest
    return _restore_digest(value)


class AttemptRestoreTests(unittest.TestCase):
    def test_restored_attempts_count_but_have_no_launch_capability(self) -> None:
        restored = attempt("old-attempt", "a", 0, phase="indeterminate")
        ledger = AttemptLedger(2, lambda event: None,
                               restored_entries=(restored,))
        self.assertEqual(ledger.counts,
                         {"started": 1, "finished": 0, "indeterminate": 1})
        self.assertTrue(ledger.uncertain_spend)

        spec = LaunchSpec("fake", "fake:a", b"payload", "a" * 64)
        binding = AttemptBinding("old-attempt", 1, "decision-1", "a",
                                 "turn-0", 0, spec.digest)
        entered = []
        with self.assertRaises(DuplicateAttemptError):
            ledger.launch_once(spec, LaunchToken(binding),
                               lambda _spec, _token: entered.append(True))
        self.assertEqual(entered, [])

    def test_restore_budget_includes_prior_physical_starts(self) -> None:
        value = checkpoint(attempts=(attempt("old-attempt", "a", 0),),
                           next_ordinal=1)
        engine, adapter, events = restore(value,
                                          configured_policy=policy(max_attempts=1))
        self.assertEqual(engine.next_action(), NextAction.DONE)
        self.assertEqual(engine.state.status, RunState.ATTEMPT_BUDGET_EXHAUSTED)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events[-1]["reason"], "attempt_budget")

    def test_uncertain_spend_must_equal_restored_attempt_phases(self) -> None:
        indeterminate = attempt("old-attempt", "a", 0, phase="indeterminate")
        engine, adapter, events = restore(checkpoint(
            attempts=(indeterminate,), uncertain_spend=True, next_ordinal=1))
        self.assertTrue(engine.state.uncertain_spend)
        self.assertTrue(engine.attempts.uncertain_spend)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])
        with self.assertRaises(DeliberationError):
            restore(checkpoint(attempts=(indeterminate,), uncertain_spend=False))


class BallotRestoreTests(unittest.TestCase):
    def test_latest_ballots_recompute_candidate(self) -> None:
        ballots = (ballot("a1", "a", 0), ballot("b1", "b", 1))
        configured = DeliberationPolicy("decision-1", ("a", "b"), ("yes", "no"),
                                         "all", 4, True)
        engine, adapter, events = restore(checkpoint(status="WAITING_HUMAN",
            attempts=(attempt("a1", "a", 0), attempt("b1", "b", 1)),
            ballots=ballots, candidate_option="yes", next_ordinal=2),
            configured_policy=configured)
        self.assertEqual(engine.ballots.tally(), {"yes": 2, "no": 0})
        self.assertEqual(engine.ballots.candidate(), "yes")
        self.assertEqual(engine.state.candidate_option, "yes")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_ballot_restore_rejects_regression_and_false_candidate(self) -> None:
        with self.assertRaises(DeliberationError):
            BallotBook(policy(), restored_ballots=(
                ballot("a2", "a", 2), ballot("a1", "a", 1)))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(ballots=(ballot("a1", "a", 0),),
                               candidate_option="yes", next_ordinal=1))

    def test_ballot_requires_matching_finished_valid_attempt(self) -> None:
        with self.assertRaises(DeliberationError):
            restore(checkpoint(ballots=(ballot("a1", "a", 0),),
                               next_ordinal=1))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(
                attempts=(attempt("a1", "a", 0),),
                ballots=(ballot("other", "a", 0),), next_ordinal=1))

    def test_restored_attempts_are_bound_to_policy_and_schedule(self) -> None:
        with self.assertRaises(DeliberationError):
            restore(checkpoint(attempts=(attempt("evil", "evil", 0),),
                               next_ordinal=1))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(attempts=(attempt("far", "a", 50),),
                               next_ordinal=0))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(
                attempts=(attempt("a1", "a", 0), attempt("b1", "b", 0)),
                next_ordinal=1))

        # A durable replay cannot jump over an unresolved scheduled turn.
        with self.assertRaises(DeliberationError):
            restore(checkpoint(
                attempts=(attempt("a1", "a", 0), attempt("b2", "b", 2)),
                next_ordinal=3))

    def test_attempt_budget_is_rejected_not_deferred_to_next_action(self) -> None:
        value = checkpoint(
            attempts=(attempt("a1", "a", 0), attempt("b1", "b", 1)),
            next_ordinal=2)
        with self.assertRaises(DeliberationError):
            restore(value, configured_policy=policy(max_attempts=1))


class EngineRestoreTests(unittest.TestCase):
    def test_public_restore_accepts_actual_replay_checkpoint(self) -> None:
        configured = policy()
        receipt = receipt_for(configured)
        with tempfile.TemporaryDirectory() as temp:
            _rundir.atomic_write_json(os.path.join(temp, "receipt.json"), receipt)
            first = _rundir.acquire_owner(temp, 60.0)
            _rundir.journal_append(temp, {
                "event": "run_prepared", "schema_version": 1,
                "generation": first.generation, "run_id": "run-1",
                "receipt_sha256": _rundir.content_sha256(receipt),
            }, owner=first)
            _rundir.journal_append(temp, {
                "event": "state_transition", "schema_version": 1,
                "generation": first.generation, "from": "PREPARED",
                "to": "RUNNING", "reason": "started",
                "decision_option": None,
            }, owner=first)
            _rundir.release_owner(first)
            owner = _rundir.acquire_owner(temp, 60.0)
            tagged, torn = _rundir.journal_read_tagged(temp)
            self.assertFalse(torn)
            restored = replay.replay_checkpoint(receipt, tagged, owner.generation)
            adapter = NoProviderAdapter()
            engine = DeliberationEngine.restore(
                restored, configured, adapter, owner=owner, run_dir=temp,
                deadline=100.0, clock=lambda: 0.0, receipt=receipt)
            self.assertEqual(engine.state.status, RunState.RUNNING)
            self.assertEqual(adapter.calls, [])
            _rundir.release_owner(owner)

    def test_stateful_mapping_is_snapshotted_before_digest_and_semantics(self) -> None:
        base = vars(checkpoint())

        class FlippingCheckpoint(dict):
            def __init__(self, value):
                super().__init__(value)
                self.next_reads = 0

            def get(self, key, default=None):
                if key == "next_ordinal":
                    self.next_reads += 1
                    return 0 if self.next_reads == 1 else 99
                return super().get(key, default)

        engine, adapter, events = restore(FlippingCheckpoint(base))
        self.assertEqual(engine.next_ordinal, 0)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_pending_command_batch_is_refused_before_provider_or_journal(self) -> None:
        command = SimpleNamespace(command_id="cmd-1", sequence=1, action="cancel")
        adapter, events = NoProviderAdapter(), []
        with self.assertRaises(DeliberationError):
            restore(checkpoint(pending_commands=(command,)),
                    adapter=adapter, events=events)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_policy_digest_rejects_same_decision_with_changed_policy(self) -> None:
        changed = DeliberationPolicy("decision-1", ("a", "b"), ("yes", "other"),
                                     "all", 4, False)
        value = checkpoint()
        # Even a caller that forges both public seal fields for the substituted
        # policy cannot make it match the independently supplied receipt.
        value.policy_digest = hashlib.sha256(json.dumps({
            "decision_id": changed.decision_id,
            "seat_ids": list(changed.seat_ids),
            "option_ids": list(changed.option_ids),
            "quorum_rule": changed.quorum_rule,
            "max_attempts": changed.max_attempts,
            "require_human_approval": changed.require_human_approval,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        value.digest = _digest_for(value)
        with self.assertRaises(DeliberationError):
            DeliberationEngine._restore_sealed(
                value, changed, NoProviderAdapter(), 2, lambda event: None,
                deadline=100.0, clock=lambda: 0.0,
                receipt=receipt_for(policy()),
                owner_is_current=lambda: True, cancel_requested=lambda: False)

    def test_transcript_is_part_of_checkpoint_seal(self) -> None:
        transcript = ({"event": "ballot_accepted", "option_id": "yes"},)
        value = checkpoint(transcript_events=transcript)
        value.transcript_events = (
            {"event": "ballot_accepted", "option_id": "no"},)
        with self.assertRaises(DeliberationError):
            restore(value)

    def test_public_restore_binds_generation_append_and_owner_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = _rundir.acquire_owner(temp, 60.0)
            _rundir.release_owner(first)
            owner = _rundir.acquire_owner(temp, 60.0)
            value = checkpoint()
            adapter = NoProviderAdapter()
            # An empty or foreign run cannot receive a checkpoint from another
            # run, even when its owner generation happens to match.
            with self.assertRaises(DeliberationError):
                DeliberationEngine.restore(
                    value, policy(), adapter, owner=owner, run_dir=temp,
                    deadline=100.0, clock=lambda: 0.0,
                    receipt=receipt_for(policy()))
            _rundir.release_owner(owner)
            self.assertEqual(adapter.calls, [])

    def test_consensus_crash_prefix_is_recovered_before_any_launch(self) -> None:
        import _deliberation_replay as replay
        from test_deliberation_replay import event, prepared, turn_events
        for approval, expected_state, expected_reason in (
                (False, RunState.DECIDED, "consensus"),
                (True, RunState.WAITING_HUMAN, "approval_required")):
            with self.subTest(approval=approval):
                configured = DeliberationPolicy("decision-1", ("a", "b"),
                                                ("yes", "no"), 1, 4, approval)
                receipt = receipt_for(configured)
                value, records = prepared(quorum="all")
                value["quorum_rule"] = 1
                value["require_human_approval"] = approval
                records[0] = (1, event("run_prepared", 1, run_id="run-1",
                                       receipt_sha256=replay._sha(value)))
                records.append((1, event("state_transition", 1, **{
                    "from": "PREPARED", "to": "RUNNING", "reason": "started"})))
                records.extend(turn_events())
                checkpoint_value = replay.replay_checkpoint(value, records, 2)
                adapter, events = NoProviderAdapter(), []
                engine = DeliberationEngine._restore_sealed(
                    checkpoint_value, configured, adapter, 2, events.append,
                    deadline=100.0, clock=lambda: 0.0, receipt=receipt,
                    owner_is_current=lambda: True, cancel_requested=lambda: False)
                self.assertEqual(engine.state.status, expected_state)
                self.assertEqual(engine.state.candidate_option, "yes")
                if expected_state == RunState.DECIDED:
                    self.assertEqual(engine.state.decision_option, "yes")
                else:
                    self.assertIsNone(engine.state.decision_option)
                self.assertEqual(events[-1]["reason"], expected_reason)
                self.assertEqual(adapter.calls, [])

    def test_terminal_checkpoint_is_refused_without_provider_or_journal_calls(self) -> None:
        for status in ("DECIDED", "FAILED", "CANCELLED"):
            adapter, events = NoProviderAdapter(), []
            with self.subTest(status=status), self.assertRaises(DeliberationError):
                restore(checkpoint(status=status), adapter=adapter, events=events)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(events, [])

    def test_current_owner_callback_is_mandatory_and_must_pass(self) -> None:
        adapter, events = NoProviderAdapter(), []
        with self.assertRaises(TypeError):
            DeliberationEngine.restore(
                checkpoint(), policy(), adapter, 2, events.append,
                deadline=100.0, clock=lambda: 0.0)
        with self.assertRaises(OwnershipLostError):
            restore(checkpoint(), owner=lambda: False,
                    adapter=adapter, events=events)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_pending_turn_is_restored_as_inert_data(self) -> None:
        pending = SimpleNamespace(
            decision_id="decision-1", seat_id="a", turn_id="turn-0",
            turn_ordinal=0, request_digest="f" * 64)
        engine, adapter, events = restore(checkpoint(
            pending_turn=pending, next_ordinal=1))
        self.assertEqual(engine.pending_turn.request_digest, "f" * 64)
        self.assertEqual(engine.next_ordinal, 1)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_pending_turn_cannot_duplicate_any_physical_attempt(self) -> None:
        pending = SimpleNamespace(
            decision_id="decision-1", seat_id="a", turn_id="turn-0",
            turn_ordinal=0, request_digest="f" * 64)
        with self.assertRaises(DeliberationError):
            restore(checkpoint(
                attempts=(attempt("old", "a", 0, phase="indeterminate"),),
                pending_turn=pending, next_ordinal=1))

    def test_pending_turn_must_be_the_contiguous_schedule_tail(self) -> None:
        pending = SimpleNamespace(
            decision_id="decision-1", seat_id="a", turn_id="turn-4",
            turn_ordinal=4, request_digest="f" * 64)
        with self.assertRaises(DeliberationError):
            restore(checkpoint(
                attempts=(attempt("old", "a", 0, phase="indeterminate"),),
                pending_turn=pending, next_ordinal=5))

    def test_restore_rejects_private_termination_reason(self) -> None:
        with self.assertRaises(DeliberationError):
            restore(checkpoint(termination_reason="C:/TOP_SECRET"))

    def test_status_decision_and_approval_invariants_are_fail_closed(self) -> None:
        with self.assertRaises(DeliberationError):
            restore(checkpoint(status="RUNNING", decision_option="yes"))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(status="WAITING_HUMAN"),
                    configured_policy=DeliberationPolicy(
                        "decision-1", ("a", "b"), ("yes", "no"), "all", 4, True))
        with self.assertRaises(DeliberationError):
            restore(checkpoint(status="RUNNING", candidate_option="yes"),
                    configured_policy=DeliberationPolicy(
                        "decision-1", ("a", "b"), ("yes", "no"), "all", 4, True))

    def test_restore_accepts_mapping_checkpoint_without_replay_import(self) -> None:
        value = vars(checkpoint())
        engine, adapter, events = restore(value)
        self.assertEqual(engine.state.status, RunState.RUNNING)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(events, [])

    def test_checkpoint_seal_rejects_post_replay_mutation(self) -> None:
        value = vars(checkpoint())
        value["candidate_option"] = "yes"
        with self.assertRaises(DeliberationError):
            restore(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
