#!/usr/bin/env python3
"""Mutation-sensitive tests for provider-inert Phase-A composition."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_phase_a as phase_a
from _deliberation import (AdapterResult, AttemptBinding, AttemptLedger,
                           DeliberationPolicy, DuplicateAttemptError,
                           ExecutionEvidence, LaunchToken, SnapshotDriftError,
                           TurnContext)


SHA_A = "a" * 64
SHA_B = "b" * 64
PROFILE = "c" * 64


def policy():
    return DeliberationPolicy("decision", ("one", "two"), ("yes", "no"), 2, 2)


def receipt(**changes):
    value = {
        "mode": "deliberation", "schema_version": 1, "run_id": "run-1",
        "decision_id": "decision", "seat_ids": ["one", "two"],
        "option_ids": ["yes", "no"], "quorum_rule": 2,
        "max_attempts": 2, "require_human_approval": False,
        "rounds": 1, "deadline_unix_ms": 20_000,
    }
    value.update(changes)
    return value


def context():
    digest = hashlib.sha256(b"prompt").hexdigest()
    return TurnContext("decision", "one", "turn-1", 0, digest)


def ok_result():
    return AdapterResult(ExecutionEvidence(True, 0), {"ballot": {}})


class PhaseATests(unittest.TestCase):
    def adapter(self, *, owner=None, snapshot=None, profile=None, now=None,
                contact=None, cleanup=None, deadline=10.0):
        owner = owner or [True]
        snapshot = snapshot or [SHA_A]
        profile = profile or [PROFILE]
        now = now or [0.0]
        contacts = []

        def fake(item):
            contacts.append(item)
            return ok_result() if contact is None else contact(item)

        adapter = phase_a.PhaseAAdapter(
            snapshot_digest=SHA_A, profile_revision_digest=PROFILE,
            current_snapshot_digest=lambda: snapshot[0],
            current_profile_revision_digest=lambda: profile[0],
            owner_is_current=lambda: owner[0], generation=1, deadline=deadline,
            clock=lambda: now[0], fake_contact=fake, fake_cleanup=cleanup)
        return adapter, contacts

    def authorize(self, adapter, *, attempt_id="attempt-1"):
        ctx = context()
        spec = adapter.prepare(ctx)
        binding = AttemptBinding(attempt_id, 1, "decision", "one", "turn-1", 0,
                                 spec.digest)
        events = []
        ledger = AttemptLedger(1, lambda event: events.append(dict(event)))
        token = ledger.commit(spec, binding)
        self.assertEqual(events[-1]["event"], "attempt_started")
        return ctx, spec, token, ledger, events

    def test_durable_authorize_contact_result_cleanup_closed_lifecycle(self):
        adapter, contacts = self.adapter()
        _ctx, spec, token, ledger, events = self.authorize(adapter)
        result = ledger.launch_once(spec, token, adapter.launch)
        self.assertEqual(adapter.lifecycle["attempt-1"], "result_available")
        ledger.finish(token, result.evidence, False)
        adapter.observe_durable(events[-1])
        cleanup = adapter.cleanup()
        self.assertEqual(len(contacts), 1)
        self.assertEqual([event["event"] for event in events],
                         ["attempt_started", "attempt_finished"])
        self.assertTrue(cleanup.clean)
        self.assertEqual(adapter.lifecycle["attempt-1"], "result_committed")
        self.assertEqual(adapter.lifecycle["_adapter"], "cleanup_verified/closed")

    def test_pre_authorization_stale_or_expired_has_zero_contact(self):
        for mutation in ("owner", "snapshot", "profile", "deadline"):
            with self.subTest(mutation=mutation):
                owner, snapshot, profile, now = [True], [SHA_A], [PROFILE], [0.0]
                if mutation == "owner": owner[0] = False
                if mutation == "snapshot": snapshot[0] = SHA_B
                if mutation == "profile": profile[0] = SHA_B
                if mutation == "deadline": now[0] = 10.0
                adapter, contacts = self.adapter(
                    owner=owner, snapshot=snapshot, profile=profile, now=now)
                with self.assertRaises(SnapshotDriftError):
                    adapter.prepare(context())
                self.assertEqual(contacts, [])

    def test_immediate_pre_contact_expiry_and_drift_have_zero_contact(self):
        for mutation in ("owner", "snapshot", "profile", "deadline"):
            with self.subTest(mutation=mutation):
                owner, snapshot, profile, now = [True], [SHA_A], [PROFILE], [0.0]
                adapter, contacts = self.adapter(
                    owner=owner, snapshot=snapshot, profile=profile, now=now)
                _ctx, spec, token, ledger, _events = self.authorize(adapter)
                if mutation == "owner": owner[0] = False
                if mutation == "snapshot": snapshot[0] = SHA_B
                if mutation == "profile": profile[0] = SHA_B
                if mutation == "deadline": now[0] = 10.0
                with self.assertRaises(SnapshotDriftError):
                    ledger.launch_once(spec, token, adapter.launch)
                self.assertEqual(contacts, [])
                self.assertEqual(adapter.lifecycle["attempt-1"], "spawn_not_possible")

    def test_concurrent_duplicate_delivery_contacts_at_most_once(self):
        gate = threading.Barrier(2)

        def contact(_item):
            gate.wait(timeout=2)
            return ok_result()

        adapter, contacts = self.adapter(contact=contact)
        _ctx, spec, token, _ledger, _events = self.authorize(adapter)
        outcomes = []

        def deliver():
            try:
                outcomes.append(adapter.launch(spec, token))
            except DuplicateAttemptError:
                outcomes.append("duplicate")

        first = threading.Thread(target=deliver)
        first.start()
        gate.wait(timeout=2)
        second = threading.Thread(target=deliver)
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(sum(item == "duplicate" for item in outcomes), 1)

    def test_same_spec_under_second_attempt_id_has_zero_second_contact(self):
        adapter, contacts = self.adapter()
        _ctx, spec, first, _ledger, _events = self.authorize(adapter)
        adapter.launch(spec, first)
        second = LaunchToken(AttemptBinding(
            "attempt-2", 1, "decision", "one", "turn-1", 0, spec.digest))
        with self.assertRaises(DuplicateAttemptError):
            adapter.launch(spec, second)
        self.assertEqual(len(contacts), 1)

    def test_post_contact_crash_is_uncertain_and_same_attempt_never_retries(self):
        def crash(_item):
            raise RuntimeError("crash after fake spawn")

        adapter, contacts = self.adapter(contact=crash)
        _ctx, spec, token, ledger, _events = self.authorize(adapter)
        with self.assertRaises(RuntimeError):
            ledger.launch_once(spec, token, adapter.launch)
        ledger.mark_indeterminate(token)
        self.assertTrue(ledger.uncertain_spend)
        self.assertEqual(adapter.lifecycle["attempt-1"], "uncertain_spend")
        with self.assertRaises(DuplicateAttemptError):
            adapter.launch(spec, token)
        self.assertEqual(len(contacts), 1)

    def test_forged_generation_spec_and_context_are_refused_before_contact(self):
        cases = (
            ("wrong-generation", 2, "one", None),
            ("wrong-context", 1, "two", None),
            ("wrong-spec", 1, "one", SHA_B),
        )
        for attempt_id, generation, seat_id, forged_spec in cases:
            with self.subTest(attempt=attempt_id):
                adapter, contacts = self.adapter()
                spec = adapter.prepare(context())
                binding = AttemptBinding(
                    attempt_id, generation, "decision", seat_id, "turn-1", 0,
                    forged_spec or spec.digest)
                with self.assertRaises(phase_a.PhaseAError):
                    adapter.launch(spec, LaunchToken(binding))
                self.assertEqual(contacts, [])
                valid = LaunchToken(AttemptBinding(
                    "valid-after-forgery", 1, "decision", "one", "turn-1", 0,
                    spec.digest))
                adapter.launch(spec, valid)
                self.assertEqual(len(contacts), 1)

    def test_cleanup_identity_mismatch_quarantines_and_never_deletes_replacement(self):
        deleted = []
        identity = [SHA_A]
        adapter, _contacts = self.adapter(
            cleanup=lambda resource_id, digest: deleted.append((resource_id, digest)) or True)
        adapter.register_resource("scratch-1", SHA_A, lambda: identity[0])
        identity[0] = SHA_B
        receipt_value = adapter.cleanup()
        self.assertFalse(receipt_value.clean)
        self.assertEqual(receipt_value.retained_resources, ("quarantine:scratch-1",))
        self.assertEqual(deleted, [])
        self.assertEqual(adapter.cleanup(), receipt_value)
        self.assertEqual(adapter.lifecycle["_adapter"], "quarantine/closed")

    def test_result_is_not_committed_without_matching_durable_finish(self):
        for mutation in ("spec", "generation"):
            with self.subTest(mutation=mutation):
                adapter, _contacts = self.adapter()
                _ctx, spec, token, _ledger, _events = self.authorize(adapter)
                adapter.launch(spec, token)
                event = {
                    "event": "attempt_finished", "attempt_id": "attempt-1",
                    "generation": 1, "launch_spec_sha256": spec.digest,
                }
                event["launch_spec_sha256" if mutation == "spec" else "generation"] = (
                    SHA_B if mutation == "spec" else 2)
                with self.assertRaises(phase_a.PhaseAError):
                    adapter.observe_durable(event)
                self.assertEqual(adapter.lifecycle["attempt-1"], "result_available")

    def test_durable_result_cannot_cross_bind_two_attempts(self):
        adapter, _contacts = self.adapter()
        first_context = context()
        second_context = TurnContext(
            "decision", "one", "turn-2", 1, first_context.request_digest)
        first_spec = adapter.prepare(first_context)
        second_spec = adapter.prepare(second_context)
        first_token = LaunchToken(AttemptBinding(
            "attempt-1", 1, "decision", "one", "turn-1", 0, first_spec.digest))
        second_token = LaunchToken(AttemptBinding(
            "attempt-2", 1, "decision", "one", "turn-2", 1, second_spec.digest))
        adapter.launch(first_spec, first_token)
        adapter.launch(second_spec, second_token)
        with self.assertRaises(phase_a.PhaseAError):
            adapter.observe_durable({
                "event": "attempt_finished", "attempt_id": "attempt-1",
                "generation": 1, "launch_spec_sha256": second_spec.digest,
            })
        self.assertEqual(adapter.lifecycle["attempt-1"], "result_available")

    def test_cleanup_failure_is_quarantined_and_reconcile_is_idempotent(self):
        calls = []
        adapter, _contacts = self.adapter(
            cleanup=lambda resource_id, digest: calls.append(resource_id) or False)
        adapter.register_resource("scratch-1", SHA_A, lambda: SHA_A)
        first = adapter.cleanup()
        second = adapter.cleanup()
        self.assertEqual(first, second)
        self.assertEqual(calls, ["scratch-1"])
        self.assertFalse(first.verified)

    def test_concurrent_cleanup_waits_for_the_authoritative_result(self):
        entered = threading.Event()
        release = threading.Event()

        def cleanup(_resource_id, _digest):
            entered.set()
            self.assertTrue(release.wait(2.0))
            return False

        adapter, _contacts = self.adapter(cleanup=cleanup)
        adapter.register_resource("scratch-1", SHA_A, lambda: SHA_A)
        results = []
        first = threading.Thread(target=lambda: results.append(adapter.cleanup()))
        first.start()
        self.assertTrue(entered.wait(2.0))
        second = threading.Thread(target=lambda: results.append(adapter.cleanup()))
        second.start()
        second.join(0.05)
        self.assertTrue(second.is_alive(), "second cleanup must wait, not report clean")
        release.set()
        first.join(2.0)
        second.join(2.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertFalse(results[0].clean)

    def test_huge_ambient_unix_clock_is_rejected_without_overflow(self):
        with self.assertRaises(phase_a.PhaseAError):
            phase_a.bind_schedule(receipt(), policy(), clock_now=0.0,
                                  unix_now_ms=10 ** 100)

    def test_private_path_cannot_become_a_durable_quarantine_identifier(self):
        adapter, _contacts = self.adapter()
        with self.assertRaises(ValueError):
            adapter.register_resource(r"C:\private\credential-profile", SHA_A,
                                      lambda: SHA_A)

    def test_receipt_schedule_is_canonical_and_not_recomputed_from_ambient_values(self):
        frozen = phase_a.bind_schedule(
            receipt(), policy(), clock_now=5.0, unix_now_ms=15_000)
        self.assertEqual(frozen.rounds, 1)
        self.assertEqual(frozen.deadline_clock, 10.0)
        self.assertEqual(frozen.schedule_digest,
                         phase_a._schedule_binding(receipt())[2])
        mutated = receipt(rounds=2, deadline_unix_ms=30_000)
        self.assertNotEqual(phase_a.bind_schedule(
            mutated, policy(), clock_now=5.0, unix_now_ms=15_000), frozen)

    def test_scheduler_composition_rejects_deadline_and_owner_authority_drift(self):
        clock = lambda: 5.0
        owner = lambda: True
        seats = {
            "one": phase_a.SeatDefinition("one", "reviewer"),
            "two": phase_a.SeatDefinition("two", "reviewer"),
        }
        # Receipt deadline projects to 10.0 on the injected clock.
        adapter = phase_a.PhaseAAdapter(
            snapshot_digest=SHA_A, profile_revision_digest=PROFILE,
            current_snapshot_digest=lambda: SHA_A,
            current_profile_revision_digest=lambda: PROFILE,
            owner_is_current=owner, generation=1, deadline=10.0,
            clock=clock, fake_contact=lambda _item: ok_result())
        scheduler = phase_a.build_scheduler(
            receipt=receipt(), policy=policy(), question="question",
            seat_resolver=seats, adapter=adapter, generation=1,
            durable_append=lambda _event: None, owner_is_current=owner,
            clock=clock, unix_now_ms=15_000)
        self.assertEqual(scheduler.rounds, 1)
        self.assertEqual(scheduler.deadline, 10.0)
        for mutation in ("deadline", "owner", "generation"):
            with self.subTest(mutation=mutation):
                wrong_owner = (lambda: True) if mutation == "owner" else owner
                wrong_generation = 2 if mutation == "generation" else 1
                if mutation == "deadline":
                    adapter._deadline = 11.0
                try:
                    with self.assertRaises(phase_a.PhaseAError):
                        phase_a.build_scheduler(
                            receipt=receipt(), policy=policy(), question="question",
                            seat_resolver=seats, adapter=adapter,
                            generation=wrong_generation,
                            durable_append=lambda _event: None,
                            owner_is_current=wrong_owner, clock=clock,
                            unix_now_ms=15_000)
                finally:
                    adapter._deadline = 10.0

    def test_composed_scheduler_runs_only_the_injected_fake_and_closes(self):
        clock = lambda: 5.0
        owner = lambda: True
        contacts = []
        seats = {
            "one": phase_a.SeatDefinition("one", "reviewer"),
            "two": phase_a.SeatDefinition("two", "reviewer"),
        }
        adapter = phase_a.PhaseAAdapter(
            snapshot_digest=SHA_A, profile_revision_digest=PROFILE,
            current_snapshot_digest=lambda: SHA_A,
            current_profile_revision_digest=lambda: PROFILE,
            owner_is_current=owner, generation=1, deadline=10.0,
            clock=clock,
            fake_contact=lambda item: contacts.append(item) or ok_result())
        scheduler = phase_a.build_scheduler(
            receipt=receipt(), policy=policy(), question="question",
            seat_resolver=seats, adapter=adapter, generation=1,
            durable_append=lambda _event: None, owner_is_current=owner,
            clock=clock, unix_now_ms=15_000)
        report = scheduler.run()
        self.assertEqual(report.turns_started, 2)
        self.assertEqual(len(contacts), 2)
        self.assertEqual(adapter.lifecycle["_adapter"], "cleanup_verified/closed")
        self.assertEqual(report.cleanup_verified, True)

    def test_receipt_policy_drift_is_refused(self):
        for key, value in (("rounds", 0), ("deadline_unix_ms", 0),
                           ("max_attempts", 3), ("seat_ids", ["one", "evil"])):
            with self.subTest(key=key):
                with self.assertRaises(Exception):
                    phase_a.bind_schedule(receipt(**{key: value}), policy(),
                                          clock_now=0.0, unix_now_ms=1)

    def test_module_has_no_real_provider_or_ambient_selection_surface(self):
        source = Path(phase_a.__file__).read_text(encoding="utf-8")
        tree = __import__("ast").parse(source)
        imports = {alias.name for node in __import__("ast").walk(tree)
                   if isinstance(node, (__import__("ast").Import,
                                        __import__("ast").ImportFrom))
                   for alias in node.names}
        forbidden = {"_executor", "subprocess", "socket", "urllib", "requests"}
        self.assertTrue(forbidden.isdisjoint(imports))
        for selector in ("getenv(", "environ", "expanduser", "which("):
            self.assertNotIn(selector, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
