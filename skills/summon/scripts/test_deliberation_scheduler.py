#!/usr/bin/env python3
"""Focused tests for the fake-only deliberation scheduler orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _deliberation import (  # noqa: E402
    AdapterResult,
    CleanupReceipt,
    DeliberationPolicy,
    ExecutionEvidence,
    HumanCommand,
    LaunchSpec,
    LaunchToken,
    RunState,
    TurnContext,
)
from _deliberation_scheduler import (  # noqa: E402
    DeliberationScheduler,
    DeliberationSchedulerError,
    LEFT_BEHIND_ELISION,
    MAX_REPORT_LEFT_BEHIND_ITEMS,
    MAX_PROMPT_BYTES,
    SeatDefinition,
    StaticSeatResolver,
)
import _deliberation_scheduler  # noqa: E402


SNAPSHOT = "a" * 64


@dataclass
class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class RecordingAdapter:
    """A deterministic adapter that never contacts a provider."""

    def __init__(self, prompt_for, *, option_by_seat=None,
                 left_behind=None, cleanup=CleanupReceipt(True), on_launch=None):
        self.prompt_for = prompt_for
        self.option_by_seat = option_by_seat or {}
        self.left_behind = list(left_behind or [])
        self.cleanup_receipt = cleanup
        self.on_launch = on_launch
        self.prepared = {}
        self.contexts = []
        self.launches = 0
        self.provider_calls = 0
        self.cleanup_calls = 0

    def prepare(self, context: TurnContext) -> LaunchSpec:
        self.contexts.append(context)
        payload = (context.decision_id + ":" + context.seat_id + ":"
                   + context.turn_id).encode("ascii")
        spec = LaunchSpec("fake", "recording:" + context.seat_id,
                          payload, SNAPSHOT)
        self.prepared[spec.digest] = context
        return spec

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        prepared = self.prepared.get(spec.digest)
        if prepared != context:
            return False
        prompt = self.prompt_for(context)
        return hashlib.sha256(prompt.encode("utf-8", errors="surrogatepass")).hexdigest() == context.request_digest

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        context = self.prepared[spec.digest]
        self.launches += 1
        if self.on_launch is not None:
            self.on_launch(context)
        option = self.option_by_seat.get(context.seat_id)
        decision = "vote" if option is not None else "undecided"
        ballot = {
            "schema_version": 1,
            "decision_id": token.binding.decision_id,
            "seat_id": token.binding.seat_id,
            "turn_id": token.binding.turn_id,
            "attempt_id": token.binding.attempt_id,
            "decision": decision,
            "option_id": option,
            "confidence": "high",
            "evidence_refs": [],
        }
        return AdapterResult(
            ExecutionEvidence(True, 0),
            {"ballot": ballot, "left_behind": list(self.left_behind)},
        )

    def cleanup(self) -> CleanupReceipt:
        self.cleanup_calls += 1
        return self.cleanup_receipt


def policy(*, attempts=4, approval=False, quorum="all"):
    return DeliberationPolicy(
        "decision", ("seat-a", "seat-b"), ("red", "blue"),
        quorum, attempts, approval)


def seats():
    return {
        "seat-a": SeatDefinition(
            "seat-a", "reviewer", ("security", "testing"), "skeptical auditor"),
        "seat-b": SeatDefinition(
            "seat-b", "designer", ("ux",), "pragmatic designer"),
    }


def make_scheduler(*, clock=None, policy_value=None, rounds=1,
                   option_by_seat=None, cleanup=CleanupReceipt(True),
                   left_behind=None, cancel_requested=None, on_launch=None,
                   durable=None, live_provider=False):
    clock = clock or Clock()
    durable_events = durable if durable is not None else []
    resolver = StaticSeatResolver(seats())
    holder = {}

    def prompt_for(context):
        return holder["scheduler"].prompt_for(context)

    adapter = RecordingAdapter(
        prompt_for, option_by_seat=option_by_seat,
        cleanup=cleanup, left_behind=left_behind, on_launch=on_launch)
    scheduler = DeliberationScheduler(
        question="Should the change ship?",
        policy=policy_value or policy(), seat_resolver=resolver,
        adapter=adapter, generation=1, durable_append=durable_events.append,
        owner_is_current=lambda: True, deadline=10.0, clock=clock,
        rounds=rounds, cancel_requested=cancel_requested,
        live_provider=live_provider)
    holder["scheduler"] = scheduler
    return scheduler, adapter, durable_events, clock


class SchedulerTests(unittest.TestCase):
    def test_live_provider_flag_refuses_before_resolver_or_adapter(self):
        calls = []

        class Resolver:
            def resolve(self, seat_ids):
                calls.append("resolve")
                return seats()

        adapter = RecordingAdapter(lambda context: "unused")
        with self.assertRaisesRegex(DeliberationSchedulerError, "live deliberation"):
            DeliberationScheduler(
                question="q", policy=policy(), seat_resolver=Resolver(),
                adapter=adapter, generation=1, durable_append=lambda event: None,
                owner_is_current=lambda: True, deadline=10.0, clock=lambda: 0.0,
                live_provider=True)
        self.assertEqual(calls, [])
        self.assertEqual(adapter.launches, 0)

    def test_round_robin_prompt_hash_and_max_attempt_bound(self):
        scheduler, adapter, events, _clock = make_scheduler(rounds=3)
        report = scheduler.run()
        self.assertEqual([context.seat_id for context in adapter.contexts],
                         ["seat-a", "seat-b", "seat-a", "seat-b"])
        self.assertEqual([context.turn_ordinal for context in adapter.contexts], [0, 1, 2, 3])
        for context in adapter.contexts:
            prompt = scheduler.prompt_for(context)
            self.assertEqual(
                hashlib.sha256(prompt.encode("utf-8", errors="surrogatepass")).hexdigest(),
                context.request_digest)
            self.assertLessEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)
        self.assertEqual(adapter.launches, 4)
        self.assertEqual(adapter.provider_calls, 0)
        self.assertEqual(report.state, RunState.ATTEMPT_BUDGET_EXHAUSTED.value)
        self.assertEqual(report.turns_started, 4)
        self.assertTrue(any(event.get("event") == "attempt_started" for event in events))

    def test_first_round_prompts_are_blind_to_peer_ballots(self):
        scheduler, adapter, _events, _clock = make_scheduler(
            rounds=2, option_by_seat={"seat-a": "red", "seat-b": "blue"})
        scheduler.run()
        packets = []
        for context in adapter.contexts:
            prompt = scheduler.prompt_for(context)
            packet = json.loads(prompt.split("DELIBERATION_PACKET:\n", 1)[1])
            packets.append((context.turn_ordinal, packet["prior_transcript"]))
        self.assertEqual(packets[0][1], [])
        self.assertEqual(packets[1][1], [])
        self.assertTrue(packets[2][1])
        self.assertTrue(packets[3][1])

    def test_prompt_binds_exact_ballot_identity_and_allowed_decisions(self):
        scheduler, adapter, _events, _clock = make_scheduler(rounds=1)
        context = scheduler._context("seat-a", 0)[0]
        packet = json.loads(
            scheduler.prompt_for(context).split("DELIBERATION_PACKET:\n", 1)[1])
        self.assertEqual(packet["turn_id"], context.turn_id)
        self.assertEqual(packet["attempt_id"], "g1-a0")
        self.assertEqual(packet["turn_ordinal"], 0)
        self.assertEqual(packet["policy"]["allowed_decisions"],
                         ["vote", "abstain", "undecided"])

    def test_prepared_prompt_cannot_be_reused_with_a_different_ordinal(self):
        scheduler, adapter, _events, _clock = make_scheduler(
            policy_value=policy(attempts=1))
        scheduler.run()
        context = adapter.contexts[0]
        forged = TurnContext(
            context.decision_id, context.seat_id, context.turn_id,
            context.turn_ordinal + 1, context.request_digest)
        with self.assertRaisesRegex(DeliberationSchedulerError, "ordinal"):
            scheduler.prompt_for(forged)

    def test_consensus_waits_for_human_then_can_be_approved(self):
        scheduler, adapter, _events, _clock = make_scheduler(
            policy_value=policy(attempts=2, approval=True),
            option_by_seat={"seat-a": "red", "seat-b": "red"})
        report = scheduler.run()
        self.assertEqual(adapter.launches, 2)
        self.assertEqual(report.state, RunState.WAITING_HUMAN.value)
        self.assertEqual(report.candidate_option, "red")
        self.assertIsNone(report.decision_option)
        scheduler.apply_human([HumanCommand(1, "approve")])
        self.assertEqual(scheduler._engine.state.status, RunState.DECIDED)
        self.assertEqual(scheduler._engine.state.decision_option, "red")
        self.assertEqual(scheduler._report.state, RunState.DECIDED.value)

    def test_concurrent_cancel_waits_for_inflight_result_boundary(self):
        started = threading.Event()
        release = threading.Event()
        holder = {}

        class BlockingAdapter(RecordingAdapter):
            def launch(self, spec, token):
                started.set()
                self.assert_release = release
                if not release.wait(2.0):
                    raise RuntimeError("test release timeout")
                return super().launch(spec, token)

        clock = Clock()
        resolver = StaticSeatResolver(seats())
        adapter = BlockingAdapter(lambda context: holder["scheduler"].prompt_for(context))
        scheduler = DeliberationScheduler(
            question="q", policy=policy(attempts=4), seat_resolver=resolver,
            adapter=adapter, generation=1, durable_append=lambda event: events.append(event),
            owner_is_current=lambda: True, deadline=10.0, clock=clock)
        holder["scheduler"] = scheduler
        events = []
        result = []
        worker = threading.Thread(target=lambda: result.append(scheduler.run()))
        worker.start()
        self.assertTrue(started.wait(2.0))
        scheduler.cancel()
        release.set()
        worker.join(3.0)
        self.assertFalse(worker.is_alive())
        report = result[0]
        self.assertEqual(report.state, RunState.CANCELLED.value)
        self.assertIsNone(report.error_kind)
        self.assertFalse(report.uncertain_spend)
        self.assertTrue(any(event.get("event") == "attempt_finished" for event in events))

    def test_deadline_after_first_result_stops_next_turn(self):
        clock = Clock(0.0)

        def advance(_context):
            clock.value = 11.0

        scheduler, adapter, _events, _clock = make_scheduler(
            clock=clock, on_launch=advance, policy_value=policy(attempts=4))
        report = scheduler.run()
        self.assertEqual(adapter.launches, 1)
        self.assertEqual(report.state, RunState.TIMED_OUT.value)

    def test_cancel_callback_stops_without_a_following_launch(self):
        holder = {}

        def cancel_when_first_launches():
            return holder["adapter"].launches >= 1

        scheduler, adapter, _events, _clock = make_scheduler(
            cancel_requested=cancel_when_first_launches)
        holder["adapter"] = adapter
        report = scheduler.run()
        self.assertEqual(adapter.launches, 1)
        self.assertEqual(report.state, RunState.CANCELLED.value)

    def test_cancel_during_launch_wins_before_ballot_acceptance(self):
        holder = {}

        def cancel_after_physical_launch():
            return holder["adapter"].launches >= 1

        scheduler, adapter, events, _clock = make_scheduler(
            policy_value=policy(attempts=4, quorum=1),
            option_by_seat={"seat-a": "red"},
            left_behind=["server:8000"],
            cancel_requested=cancel_after_physical_launch)
        holder["adapter"] = adapter
        report = scheduler.run()
        self.assertEqual(adapter.launches, 1)
        self.assertEqual(report.state, RunState.CANCELLED.value)
        self.assertIsNone(report.decision_option)
        self.assertIn("server:8000", report.left_behind)
        self.assertTrue(any(event.get("event") == "advisory_left_behind" for event in events))
        self.assertFalse(any(event.get("event") == "ballot_accepted" for event in events))

    def test_cleanup_and_left_behind_handoff_are_distinct_and_bounded(self):
        cleanup = CleanupReceipt(False, ("registered-service",), (4242,))
        scheduler, adapter, _events, _clock = make_scheduler(
            policy_value=policy(attempts=1), cleanup=cleanup,
            left_behind=["listener on 127.0.0.1"])
        report = scheduler.run()
        self.assertEqual(adapter.cleanup_calls, 1)
        self.assertFalse(report.cleanup_verified)
        self.assertEqual(report.retained_resources,
                         ("registered-service", "pid:4242:report_only"))
        self.assertEqual(report.left_behind,
                         ("listener on 127.0.0.1", "registered-service",
                          "pid:4242:report_only"))

    def test_seat_snapshot_is_immutable_after_resolver_returns(self):
        original = seats()
        resolver = StaticSeatResolver(original)
        holder = {}
        adapter = RecordingAdapter(
            lambda context: holder["scheduler"].prompt_for(context))
        scheduler = DeliberationScheduler(
            question="Should the change ship?", policy=policy(),
            seat_resolver=resolver, adapter=adapter, generation=1,
            durable_append=lambda event: None, owner_is_current=lambda: True,
            deadline=10.0, clock=lambda: 0.0)
        holder["scheduler"] = scheduler
        before = scheduler.prompt_for(
            TurnContext("decision", "seat-a", "r1-t0", 0, "0" * 64))
        original["seat-a"] = SeatDefinition(
            "seat-a", "mutated", ("malicious",), "changed")
        after = scheduler.prompt_for(
            TurnContext("decision", "seat-a", "r1-t0", 0, "0" * 64))
        self.assertEqual(before, after)
        self.assertIn("skeptical auditor", after)
        self.assertNotIn("malicious", after)

    def test_turn_identity_is_durable_before_adapter_prepare_and_prompt_is_not_journaled(self):
        durable = []
        observations = []

        class ObservingAdapter(RecordingAdapter):
            def prepare(self, context):
                observations.append(tuple(event["event"] for event in durable))
                return super().prepare(context)

        holder = {}
        adapter = ObservingAdapter(
            lambda context: holder["scheduler"].prompt_for(context))
        scheduler = DeliberationScheduler(
            question="private question text", policy=policy(attempts=1),
            seat_resolver=StaticSeatResolver(seats()), adapter=adapter,
            generation=1, durable_append=durable.append,
            owner_is_current=lambda: True, deadline=10.0, clock=lambda: 0.0)
        holder["scheduler"] = scheduler
        scheduler.run()
        self.assertTrue(observations)
        self.assertIn("turn_prepared", observations[0])
        prepared = next(event for event in durable if event["event"] == "turn_prepared")
        self.assertEqual(prepared["request_digest"], adapter.contexts[0].request_digest)
        self.assertEqual(prepared["seat_id"], adapter.contexts[0].seat_id)
        self.assertEqual(prepared["turn_id"], adapter.contexts[0].turn_id)
        self.assertEqual(prepared["turn_ordinal"], adapter.contexts[0].turn_ordinal)
        self.assertNotIn("private question text", repr(durable))
        self.assertNotIn("prompt", prepared)

    def test_round_exhaustion_is_durable_unresolved_not_running(self):
        scheduler, _adapter, events, _clock = make_scheduler(
            policy_value=policy(attempts=4), rounds=1)
        report = scheduler.run()
        self.assertEqual(report.state, RunState.UNRESOLVED.value)
        self.assertEqual(report.status, "error")
        transition = [event for event in events if event.get("event") == "state_transition"][-1]
        self.assertEqual(transition["to"], RunState.UNRESOLVED.value)
        self.assertEqual(transition["reason"], "max_rounds")

    def test_scheduler_has_no_provider_executor_or_recovery_paths(self):
        source = Path(_deliberation_scheduler.__file__).read_text(encoding="utf-8")
        self.assertNotIn("execute_agent", source)
        self.assertNotIn("_executor", source)
        self.assertNotRegex(
            source.lower(), r"(?m)\b(?:retry|retries|fallback|repair|gate|gates)\b")

    def test_owner_loss_prevents_launch_and_no_implicit_retry(self):
        owner = [True]
        events = []

        def depose_after_start(event):
            events.append(event)
            if event.get("event") == "state_transition" and event.get("to") == RunState.RUNNING.value:
                owner[0] = False

        resolver = StaticSeatResolver(seats())
        adapter = RecordingAdapter(lambda context: "unused")
        scheduler = DeliberationScheduler(
            question="q", policy=policy(attempts=4), seat_resolver=resolver,
            adapter=adapter, generation=1, durable_append=depose_after_start,
            owner_is_current=lambda: owner[0], deadline=10.0,
            clock=lambda: 0.0)
        report = scheduler.run()
        self.assertEqual(adapter.launches, 0)
        self.assertEqual(report.state, RunState.FAILED.value)
        self.assertEqual(report.error_kind, "ownership_lost")
        self.assertFalse(any(event.get("event") == "turn_prepared" for event in events))

    def test_report_left_behind_is_run_bounded_and_marks_elision(self):
        left = ["resource-" + ("x" * 248) + str(index) for index in range(32)]
        scheduler, _adapter, _events, _clock = make_scheduler(
            policy_value=policy(attempts=32), rounds=10, left_behind=left)
        report = scheduler.run()
        self.assertTrue(report.left_behind_elided)
        self.assertIn(LEFT_BEHIND_ELISION, report.left_behind)
        self.assertLessEqual(len(report.left_behind), MAX_REPORT_LEFT_BEHIND_ITEMS + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
