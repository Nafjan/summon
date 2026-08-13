#!/usr/bin/env python3
"""Focused mutation tests for deliberation's real provider launch boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _acpbackend  # noqa: E402
import _apibackend  # noqa: E402
import _executor  # noqa: E402
from _builder import AgentInvocation  # noqa: E402
from _deliberation import (AttemptBinding, CleanupReceipt, DeliberationError,
                           DuplicateAttemptError, LaunchSpec, LaunchToken,
                           SnapshotDriftError, TurnContext)  # noqa: E402
from _deliberation_adapter import (FreshDispatchAdapter,
                                   SeatMultiplexAdapter)  # noqa: E402
from _executor import ProviderLaunchControl, ProviderLaunchError  # noqa: E402


SNAPSHOT = "a" * 64


def turn() -> TurnContext:
    return TurnContext("decision", "seat-a", "turn-1", 0,
                       hashlib.sha256(b"TOP SECRET").hexdigest())


def turn_with_prompt(prompt: str, *, turn_id: str, ordinal: int,
                     seat: str = "seat-a") -> TurnContext:
    return TurnContext("decision", seat, turn_id, ordinal,
                       hashlib.sha256(prompt.encode("utf-8")).hexdigest())


def token_for(spec, attempt_id="attempt-1") -> LaunchToken:
    return LaunchToken(AttemptBinding(attempt_id, 1, "decision", "seat-a",
                                      "turn-1", 0, spec.digest))


def token_for_context(spec, context: TurnContext, attempt_id: str) -> LaunchToken:
    return LaunchToken(AttemptBinding(
        attempt_id, 1, context.decision_id, context.seat_id, context.turn_id,
        context.turn_ordinal, spec.digest))


class AdapterBoundaryTests(unittest.TestCase):
    def invocation(self, *, cli="claude", transport="subprocess", prompt="TOP SECRET"):
        return AgentInvocation(cli=cli, prompt=prompt, cwd=r"C:\private\project",
                               model="private-model-name", permission="yolo",
                               transport=transport)

    def test_owner_fence_callback_is_required(self):
        with self.assertRaises(TypeError):
            FreshDispatchAdapter(
                self.invocation(), snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                timeout_ms=1000, generation=1,
                executor=lambda *args, **kwargs: {})

    def test_spec_and_launch_evidence_do_not_leak_prompt_path_or_model(self):
        seen = []

        def executor(inv, **kwargs):
            control = kwargs["launch_control"]
            control.before_provider_launch({"backend": inv.cli,
                                            "transport": inv.transport,
                                            "command_sha256": "b" * 64})
            handle = object()
            control.spawned(handle)
            control.reaped(handle)
            seen.append(kwargs)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        spec = adapter.prepare(turn())
        result = adapter.launch(spec, token_for(spec))
        serialized = spec.command_identity + spec.sealed_payload.decode()
        self.assertNotIn("TOP SECRET", serialized)
        self.assertNotIn("private\\project", serialized.lower())
        self.assertNotIn("private-model-name", serialized)
        self.assertTrue(result.evidence.transport_ok)
        self.assertFalse(seen[0]["launch_control"].allow_secondary)
        self.assertTrue(adapter.cleanup().clean)

    def test_concurrent_duplicate_attempt_enters_executor_once(self):
        entered = 0
        entered_lock = threading.Lock()

        def executor(inv, **kwargs):
            nonlocal entered
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            with entered_lock:
                entered += 1
            time.sleep(0.05)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        spec = adapter.prepare(turn())
        tok = token_for(spec)
        barrier = threading.Barrier(3)
        outcomes = []

        def launch():
            barrier.wait()
            try:
                adapter.launch(spec, tok)
                outcomes.append("ok")
            except DuplicateAttemptError:
                outcomes.append("duplicate")

        threads = [threading.Thread(target=launch) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(entered, 1)
        self.assertCountEqual(outcomes, ["ok", "duplicate"])

    def test_snapshot_is_revalidated_at_irreversible_boundary(self):
        current = [SNAPSHOT]

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            self.fail("snapshot drift must fail before provider contact")

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: current[0],
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        spec = adapter.prepare(turn())
        self.assertTrue(adapter.revalidate(spec, turn()))
        current[0] = "b" * 64
        with self.assertRaises(SnapshotDriftError):
            adapter.launch(spec, token_for(spec))

    def test_takeover_between_scheduler_claim_and_provider_contact_is_fenced(self):
        owner = [True]
        contacted = []

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            contacted.append(True)
            self.fail("provider contact must be refused after ownership loss")

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: owner[0], timeout_ms=1000, generation=1,
            executor=executor)
        spec = adapter.prepare(turn())
        # The scheduler/ledger may have already durably committed and claimed
        # the token; this is the takeover window immediately before the adapter
        # reaches the provider launch boundary.
        owner[0] = False
        with self.assertRaises(SnapshotDriftError):
            adapter.launch(spec, token_for(spec))
        self.assertEqual(contacted, [])

    def test_prompt_digest_is_bound_to_the_invocation(self):
        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=lambda *args, **kwargs: {"result": "{}", "exit_code": 0})
        forged = TurnContext("decision", "seat-a", "turn-1", 0,
                             hashlib.sha256(b"different prompt").hexdigest())
        spec = adapter.prepare(forged)
        self.assertFalse(adapter.revalidate(spec, forged))

    def test_context_factory_binds_each_prompt_and_launch_uses_prepared_value(self):
        base = self.invocation(prompt="base prompt")
        prompts = []
        calls = []

        def factory(context):
            calls.append(context.turn_id)
            prompt = f"turn prompt {context.turn_id}"
            return replace(base, prompt=prompt)

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            prompts.append(inv.prompt)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            base, snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            invocation_for_context=factory, executor=executor)
        first = turn_with_prompt("turn prompt turn-1", turn_id="turn-1", ordinal=0)
        second = turn_with_prompt("turn prompt turn-2", turn_id="turn-2", ordinal=1)
        first_spec = adapter.prepare(first)
        second_spec = adapter.prepare(second)
        # Mutating the scheduler's prompt source after prepare must not change
        # the exact invocation that was selected before the durable commit.
        calls.append("factory-complete")
        adapter.launch(first_spec, token_for_context(first_spec, first, "attempt-1"))
        adapter.launch(second_spec, token_for_context(second_spec, second, "attempt-2"))
        self.assertEqual(prompts, ["turn prompt turn-1", "turn prompt turn-2"])
        self.assertEqual(calls, ["turn-1", "turn-2", "factory-complete"])

    def test_context_factory_immutable_identity_drift_fails_before_provider(self):
        base = self.invocation(prompt="base prompt")

        def factory(context):
            return replace(base, prompt="turn prompt",
                           model="different-model")

        adapter = FreshDispatchAdapter(
            base, snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            invocation_for_context=factory,
            executor=lambda *args, **kwargs: self.fail("provider contact"))
        context = turn_with_prompt("turn prompt", turn_id="turn-1", ordinal=0)
        with self.assertRaisesRegex(SnapshotDriftError, "immutable execution identity"):
            adapter.prepare(context)

    def test_context_factory_output_is_not_recomputed_at_launch(self):
        base = self.invocation(prompt="base prompt")
        prompt = ["first prompt"]
        seen = []

        def factory(context):
            return replace(base, prompt=prompt[0])

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            seen.append(inv.prompt)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            base, snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            invocation_for_context=factory, executor=executor)
        context = turn_with_prompt("first prompt", turn_id="turn-1", ordinal=0)
        spec = adapter.prepare(context)
        prompt[0] = "second prompt"
        adapter.launch(spec, token_for_context(spec, context, "attempt-1"))
        self.assertEqual(seen, ["first prompt"])

    def test_token_turn_binding_is_checked_before_provider_contact(self):
        contacted = []

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            contacted.append(True)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        spec = adapter.prepare(turn())
        forged = LaunchToken(AttemptBinding(
            "attempt-forged", 1, "decision", "seat-b", "turn-forged", 99,
            spec.digest))
        with self.assertRaisesRegex(DeliberationError, "prepared decision turn"):
            adapter.launch(spec, forged)
        self.assertEqual(contacted, [])

    def test_one_prepared_spec_is_single_use_across_attempt_ids(self):
        contacted = []

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            contacted.append(True)
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        context = turn()
        spec = adapter.prepare(context)
        adapter.launch(spec, token_for_context(spec, context, "attempt-1"))
        with self.assertRaises(DuplicateAttemptError):
            adapter.launch(spec, token_for_context(spec, context, "attempt-2"))
        self.assertEqual(contacted, [True])

    def test_profile_environment_is_deeply_sealed_before_prepare(self):
        profile_env = {"SAFE": "1"}
        base = replace(self.invocation(), profile_env=profile_env)
        seen = []

        def executor(inv, **kwargs):
            kwargs["launch_control"].before_provider_launch(
                {"backend": inv.cli, "transport": inv.transport})
            seen.append(dict(inv.profile_env or {}))
            return {"result": "{}", "exit_code": 0}

        adapter = FreshDispatchAdapter(
            base, snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor)
        context = turn()
        spec = adapter.prepare(context)
        profile_env["INJECTED"] = "YES"
        adapter.launch(spec, token_for(spec))
        self.assertEqual(seen, [{"SAFE": "1"}])

    def test_cleanup_reports_non_process_handle_instead_of_killing_by_pid(self):
        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=lambda *args, **kwargs: {})
        adapter._on_spawn(object())
        receipt = adapter.cleanup()
        self.assertFalse(receipt.verified)
        self.assertEqual(receipt.retained_resources,
                         ("registered-provider-operation:cleanup-unverified",))

    def test_disposable_profile_is_registered_and_removed_without_path_receipt(self):
        import _deliberation_adapter as adapter_module
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs")
            os.makedirs(runs)
            profile = tempfile.mkdtemp(prefix="run-", dir=runs)
            with mock.patch.dict(os.environ, {"AGY_HEADLESS_PROFILE": root}, clear=False):
                adapter = FreshDispatchAdapter(
                    self.invocation(), snapshot_digest=SNAPSHOT,
                    current_snapshot_digest=lambda: SNAPSHOT,
                    owner_is_current=lambda: True, timeout_ms=1000, generation=1,
                    executor=lambda *args, **kwargs: {})
                adapter._on_resource(profile, "agy-profile")
                receipt = adapter.cleanup()
            self.assertTrue(receipt.clean)
            self.assertFalse(os.path.exists(profile))
            self.assertNotIn(root, repr(receipt))
            self.assertIsNotNone(adapter_module._profile_runs_root("agy-profile"))

    def test_resource_registration_rejects_named_or_outside_profile(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.join(root, "named-profile")
            os.makedirs(outside)
            adapter = FreshDispatchAdapter(
                self.invocation(), snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True, timeout_ms=1000, generation=1,
                executor=lambda *args, **kwargs: {})
            with self.assertRaisesRegex(ValueError, "disposable profile"):
                adapter._on_resource(outside, "agy-profile")
            self.assertTrue(os.path.isdir(outside))

    def test_replaced_profile_is_not_deleted_by_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs")
            os.makedirs(runs)
            profile = tempfile.mkdtemp(prefix="run-", dir=runs)
            adapter = FreshDispatchAdapter(
                self.invocation(), snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True, timeout_ms=1000, generation=1,
                executor=lambda *args, **kwargs: {})
            with mock.patch.dict(os.environ, {"AGY_HEADLESS_PROFILE": root}, clear=False):
                adapter._on_resource(profile, "agy-profile")
            # Simulate a path replacement by another run.  Cleanup must not
            # turn a stale path into deletion authority for the replacement.
            shutil.rmtree(profile)
            os.makedirs(profile)
            receipt = adapter.cleanup()
            self.assertFalse(receipt.clean)
            self.assertTrue(os.path.isdir(profile))
            self.assertIn("agy-profile:cleanup-unverified",
                          receipt.retained_resources)

    def test_late_resource_registration_is_rejected_after_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs")
            os.makedirs(runs)
            profile = tempfile.mkdtemp(prefix="run-", dir=runs)
            adapter = FreshDispatchAdapter(
                self.invocation(), snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True, timeout_ms=1000, generation=1,
                executor=lambda *args, **kwargs: {})
            with mock.patch.dict(os.environ, {"AGY_HEADLESS_PROFILE": root}, clear=False):
                self.assertTrue(adapter.cleanup().clean)
                with self.assertRaisesRegex(ValueError, "after cleanup"):
                    adapter._on_resource(profile, "agy-profile")
            self.assertTrue(os.path.isdir(profile))
            shutil.rmtree(profile)

    def test_controlled_builder_error_does_not_expose_private_path(self):
        import _executor as executor_module
        original = executor_module.build_invocation_args
        try:
            executor_module.build_invocation_args = lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError(r"PermissionError: C:\Users\nside\private-project\secret"))
            adapter = FreshDispatchAdapter(
                replace(self.invocation(), cwd=os.getcwd()),
                snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True, timeout_ms=1000, generation=1)
            context = turn_with_prompt("provider smoke", turn_id="turn-error", ordinal=0)
            spec = adapter.prepare(context)
            result = adapter.launch(
                spec, token_for_context(spec, context, "attempt-error"))
            self.assertNotIn("private-project", result.model_prose)
        finally:
            executor_module.build_invocation_args = original

    def test_control_resource_callback_is_optional_for_legacy_control_users(self):
        called = []
        control = ProviderLaunchControl(
            before_launch=lambda evidence: None,
            on_resource=lambda path, kind: called.append((path, kind)))
        control.register_resource("opaque-internal-path", "fixture")
        self.assertEqual(called, [("opaque-internal-path", "fixture")])

    def test_executor_path_tracks_builder_profile_before_real_subprocess(self):
        """The controlled bridge owns a builder-created profile on the real Popen path."""
        import _executor as executor_module
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs")
            os.makedirs(runs)
            created = []

            def fake_build(inv, timeout_ms=None, *, resource_register=None):
                profile = tempfile.mkdtemp(prefix="run-", dir=runs)
                with open(os.path.join(profile, "credential.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write("PRIVATE-CREDENTIAL")
                created.append(profile)
                resource_register(profile, "agy-profile")
                return sys.executable, [
                    "-c",
                    "print('STATUS: DONE\\nSUMMARY: fake\\nFOLLOW-UP: none\\n"
                    "HANDOFF: none')",
                ], None

            inv = replace(self.invocation(cli="claude", prompt="provider smoke"),
                          cwd=os.getcwd())
            with mock.patch.dict(os.environ, {"AGY_HEADLESS_PROFILE": root}, clear=False), \
                    mock.patch.object(executor_module, "build_invocation_args",
                                      side_effect=fake_build):
                adapter = FreshDispatchAdapter(
                    inv, snapshot_digest=SNAPSHOT,
                    current_snapshot_digest=lambda: SNAPSHOT,
                    owner_is_current=lambda: True, timeout_ms=5000, generation=1)
                context = turn_with_prompt(
                    "provider smoke", turn_id="turn-provider", ordinal=0)
                spec = adapter.prepare(context)
                result = adapter.launch(
                    spec, token_for_context(spec, context, "attempt-provider"))
                receipt = adapter.cleanup()
            self.assertEqual(result.evidence.exit_code, 0)
            self.assertTrue(result.evidence.transport_ok)
            self.assertEqual(len(created), 1)
            self.assertFalse(os.path.exists(created[0]))
            self.assertTrue(receipt.clean)
            self.assertNotIn("PRIVATE-CREDENTIAL", repr(receipt))

    def test_owner_refusal_after_profile_build_still_cleans_profile(self):
        import _executor as executor_module
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs")
            os.makedirs(runs)
            created = []
            owner = {"current": True}

            def fake_build(inv, timeout_ms=None, *, resource_register=None):
                profile = tempfile.mkdtemp(prefix="run-", dir=runs)
                created.append(profile)
                resource_register(profile, "agy-profile")
                return sys.executable, ["-c", "print('CONTACT')"], None

            with mock.patch.dict(os.environ, {"AGY_HEADLESS_PROFILE": root}, clear=False), \
                    mock.patch.object(executor_module, "build_invocation_args",
                                      side_effect=fake_build):
                adapter = FreshDispatchAdapter(
                    replace(self.invocation(cli="claude", prompt="owner smoke"),
                            cwd=os.getcwd()),
                    snapshot_digest=SNAPSHOT,
                    current_snapshot_digest=lambda: SNAPSHOT,
                    owner_is_current=lambda: owner["current"],
                    timeout_ms=5000, generation=1)
                context = turn_with_prompt(
                    "owner smoke", turn_id="turn-owner", ordinal=0)
                spec = adapter.prepare(context)
                owner["current"] = False
                result = adapter.launch(
                    spec, token_for_context(spec, context, "attempt-owner"))
                receipt = adapter.cleanup()
            self.assertNotEqual(result.evidence.exit_code, 0)
            self.assertEqual(len(created), 1)
            self.assertFalse(os.path.exists(created[0]))
            self.assertTrue(receipt.clean)

    def test_unbounded_http_transport_is_refused_for_deliberation(self):
        with self.assertRaisesRegex(ValueError, "cancellation is bounded"):
            FreshDispatchAdapter(
                self.invocation(cli="openai-compat", transport="api"),
                snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True,
                timeout_ms=1000, generation=1,
                executor=lambda *args, **kwargs: {})

    def _multiplex_child(self, *, executor=None):
        return FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000, generation=1,
            executor=executor or (lambda *args, **kwargs: {
                "result": "{}", "exit_code": 0}))

    def test_seat_multiplex_routes_by_prepared_spec_and_rejects_cross_seat(self):
        contacted = []

        def executor(label):
            def run(inv, **kwargs):
                kwargs["launch_control"].before_provider_launch(
                    {"backend": inv.cli, "transport": inv.transport})
                contacted.append(label)
                return {"result": "{}", "exit_code": 0}
            return run

        child_a = self._multiplex_child(executor=executor("seat-a"))
        child_b = self._multiplex_child(executor=executor("seat-b"))
        children = {"seat-a": child_a, "seat-b": child_b}
        mux = SeatMultiplexAdapter(children)
        context_a = turn_with_prompt(
            "TOP SECRET", turn_id="turn-a", ordinal=0, seat="seat-a")
        context_b = turn_with_prompt(
            "TOP SECRET", turn_id="turn-b", ordinal=0, seat="seat-b")
        spec_a = mux.prepare(context_a)
        self.assertTrue(mux.revalidate(spec_a, context_a))
        # The caller cannot switch the child by supplying another seat context;
        # the spec remains bound to child-a and its own child fence rejects the
        # forged token before provider contact.
        self.assertFalse(mux.revalidate(spec_a, context_b))
        forged = token_for_context(spec_a, context_b, "attempt-forged")
        with self.assertRaisesRegex(DeliberationError, "prepared decision turn"):
            mux.launch(spec_a, forged)
        mux.launch(spec_a, token_for_context(spec_a, context_a, "attempt-a"))
        self.assertEqual(contacted, ["seat-a"])

    def test_seat_multiplex_rejects_unknown_spec_without_provider_contact(self):
        contacted = []

        def executor(inv, **kwargs):
            contacted.append(True)
            return {"result": "{}", "exit_code": 0}

        child = self._multiplex_child(executor=executor)
        mux = SeatMultiplexAdapter({"seat-a": child})
        unknown = LaunchSpec("scripted", "unknown", b"opaque", SNAPSHOT)
        context = turn()
        self.assertFalse(mux.revalidate(unknown, context))
        token = token_for_context(unknown, context, "unknown-attempt")
        with self.assertRaisesRegex(DeliberationError, "not prepared"):
            mux.launch(unknown, token)
        self.assertEqual(contacted, [])

    def test_seat_multiplex_rejects_duplicate_spec_digest_across_children(self):
        child_a = self._multiplex_child()
        child_b = self._multiplex_child()
        mux = SeatMultiplexAdapter({"seat-a": child_a, "seat-b": child_b})
        context_a = turn_with_prompt(
            "TOP SECRET", turn_id="turn-a", ordinal=0, seat="seat-a")
        context_b = turn_with_prompt(
            "TOP SECRET", turn_id="turn-b", ordinal=0, seat="seat-b")
        spec_a = mux.prepare(context_a)
        with mock.patch.object(child_b, "prepare", return_value=spec_a):
            with self.assertRaisesRegex(DeliberationError, "another seat adapter"):
                mux.prepare(context_b)

    def test_seat_multiplex_copies_seat_map_at_construction(self):
        child_a = self._multiplex_child()
        child_b = self._multiplex_child()
        children = {"seat-a": child_a}
        mux = SeatMultiplexAdapter(children)
        children["seat-b"] = child_b
        context_b = turn_with_prompt(
            "TOP SECRET", turn_id="turn-b", ordinal=0, seat="seat-b")
        with self.assertRaisesRegex(DeliberationError, "no deliberation adapter"):
            mux.prepare(context_b)

    def test_seat_multiplex_cleanup_aggregates_receipts_honestly(self):
        child_a = self._multiplex_child()
        child_b = self._multiplex_child()
        mux = SeatMultiplexAdapter({"seat-b": child_b, "seat-a": child_a})
        with mock.patch.object(
                child_a, "cleanup",
                return_value=CleanupReceipt(True, ("a-resource",), (11,))) as clean_a, \
             mock.patch.object(
                 child_b, "cleanup",
                 return_value=CleanupReceipt(False, ("b-resource",), (22,))) as clean_b:
            receipt = mux.cleanup()
        clean_a.assert_called_once_with()
        clean_b.assert_called_once_with()
        self.assertFalse(receipt.verified)
        self.assertFalse(receipt.clean)
        self.assertEqual(receipt.retained_resources,
                         ("a-resource", "b-resource"))
        self.assertEqual(receipt.unverified_pids, (11, 22))

    def test_seat_multiplex_cleanup_reports_child_exception_without_leak_claim(self):
        child_a = self._multiplex_child()
        child_b = self._multiplex_child()
        mux = SeatMultiplexAdapter({"seat-a": child_a, "seat-b": child_b})
        with mock.patch.object(child_a, "cleanup", return_value=CleanupReceipt(True)), \
             mock.patch.object(child_b, "cleanup",
                               side_effect=RuntimeError("private cleanup detail")):
            receipt = mux.cleanup()
        self.assertFalse(receipt.verified)
        self.assertEqual(receipt.retained_resources,
                         ("seat:seat-b:cleanup-unverified",))
        self.assertNotIn("private", repr(receipt))


class LaunchControlTests(unittest.TestCase):
    def test_single_use_claim_is_atomic(self):
        callbacks = []
        control = ProviderLaunchControl(before_launch=lambda evidence: callbacks.append(evidence))
        barrier = threading.Barrier(3)
        outcomes = []

        def claim():
            barrier.wait()
            try:
                control.before_provider_launch({"transport": "test"})
                outcomes.append("ok")
            except ProviderLaunchError:
                outcomes.append("denied")

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len(callbacks), 1)
        self.assertCountEqual(outcomes, ["ok", "denied"])

    def test_cancelled_control_refuses_before_callback(self):
        callback = mock.Mock()
        control = ProviderLaunchControl(before_launch=callback, cancelled=lambda: True)
        with self.assertRaises(ProviderLaunchError):
            control.before_provider_launch({"transport": "test"})
        callback.assert_not_called()

    def test_broken_cancellation_source_is_redacted(self):
        control = ProviderLaunchControl(
            before_launch=lambda evidence: None,
            cancelled=lambda: (_ for _ in ()).throw(
                RuntimeError("TOP-SECRET-CANCELLATION-TEXT")))
        with self.assertRaisesRegex(ProviderLaunchError, "RuntimeError") as caught:
            control.before_provider_launch({"transport": "test"})
        self.assertNotIn("TOP-SECRET", str(caught.exception))


class ExecutorPathTests(unittest.TestCase):
    def test_subprocess_hook_wraps_exactly_one_popen_and_reap(self):
        events = []
        fake_process = mock.Mock()
        fake_process.pid = 123
        control = ProviderLaunchControl(
            before_launch=lambda evidence: events.append(("before", evidence)),
            on_spawn=lambda handle: events.append(("spawn", handle)),
            on_reap=lambda handle: events.append(("reap", handle)))
        inv = AgentInvocation(cli="claude", prompt="secret", cwd=tempfile.gettempdir(),
                              permission="yolo")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=("fake-cli", ["secret"], {})), \
             mock.patch.object(_executor, "_resolve_launch",
                               return_value=("fake-cli", ["secret"])), \
             mock.patch.object(_executor, "argv_length_error", return_value=None), \
             mock.patch.object(_executor.subprocess, "Popen", return_value=fake_process) as popen, \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}), \
             mock.patch.object(_executor, "_drive_process",
                               return_value={"result": "ok", "exit_code": 0,
                                             "status": "success", "cli": "claude"}):
            response = _executor.execute_agent(inv, timeout_ms=1000,
                                               launch_control=control)
        self.assertEqual(response["exit_code"], 0)
        popen.assert_called_once()
        self.assertEqual([event[0] for event in events], ["before", "spawn", "reap"])
        self.assertNotIn("secret", json.dumps(events[0][1]))

    def test_registration_failure_terminates_untracked_child_and_redacts_error(self):
        fake_process = mock.Mock()
        fake_process.pid = 124
        control = ProviderLaunchControl(
            before_launch=lambda evidence: None,
            on_spawn=lambda handle: (_ for _ in ()).throw(
                RuntimeError("TOP-SECRET-CALLBACK-TEXT")))
        inv = AgentInvocation(cli="claude", prompt="p", cwd=tempfile.gettempdir(),
                              permission="yolo")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=("fake-cli", ["p"], {})), \
             mock.patch.object(_executor, "_resolve_launch",
                               return_value=("fake-cli", ["p"])), \
             mock.patch.object(_executor, "argv_length_error", return_value=None), \
             mock.patch.object(_executor.subprocess, "Popen", return_value=fake_process), \
             mock.patch.object(_executor, "_kill_tree") as kill, \
             mock.patch.object(_executor, "_safe_communicate"), \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            response = _executor.execute_agent(inv, timeout_ms=1000,
                                               launch_control=control)
        kill.assert_called_once_with(fake_process)
        self.assertEqual(response["status"], "error")
        self.assertNotIn("TOP-SECRET", json.dumps(response))

    def test_oversized_subprocess_does_not_route_to_acp(self):
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        inv = AgentInvocation(cli="kimi", prompt="x", cwd=tempfile.gettempdir(),
                              permission="yolo", transport="subprocess")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=("kimi", ["x"], {})), \
             mock.patch.object(_executor, "_resolve_launch", return_value=("kimi", ["x"])), \
             mock.patch.object(_executor, "argv_length_error", return_value="too long"), \
             mock.patch.object(_executor.subprocess, "Popen") as popen:
            response = _executor.execute_agent(inv, timeout_ms=1000,
                                               launch_control=control)
        self.assertEqual(response["status"], "error")
        self.assertNotIn("fallback", response)
        popen.assert_not_called()

    def test_api_control_disables_payg_retry(self):
        inv = AgentInvocation(cli="openai-compat", prompt="p", cwd=".", model="m",
                              base_url="https://example.test/api/coding/v3",
                              allow_payg=True)
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        first = {"result": "", "exit_code": 1, "status": "error",
                 "cli": "openai-compat", "error": "quota",
                 "_payg_fallback_worthy": True, "_fallback_reason": "quota"}
        with mock.patch.object(_apibackend, "_do_request", return_value=first) as request:
            response = _apibackend.call(inv, 1000, launch_control=control)
        request.assert_called_once()
        self.assertNotIn("fallback", response)

    def test_executor_wrapper_propagates_control_to_openai_backend(self):
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        inv = AgentInvocation(cli="openai-compat", prompt="p", cwd=tempfile.gettempdir(),
                              model="m", base_url="https://example.test/v1")
        with mock.patch.object(_apibackend, "call",
                               return_value={"result": "", "exit_code": 1,
                                             "status": "error", "cli": "openai-compat"}) as call, \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            _executor.execute_agent(inv, timeout_ms=1000, launch_control=control)
        self.assertIs(call.call_args.kwargs["launch_control"], control)

    def test_executor_wrapper_propagates_control_to_acp_backend(self):
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        inv = AgentInvocation(cli="kimi", prompt="p", cwd=tempfile.gettempdir(),
                              permission="yolo", transport="acp")
        with mock.patch.object(_acpbackend, "call",
                               return_value={"result": "", "exit_code": 1,
                                             "status": "error", "cli": "kimi"}) as call, \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            _executor.execute_agent(inv, timeout_ms=1000, launch_control=control)
        self.assertIs(call.call_args.kwargs["launch_control"], control)

    def test_arkcli_control_refuses_before_backend_contact(self):
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        inv = AgentInvocation(cli="arkcli", prompt="p", cwd=tempfile.gettempdir(),
                              model="concrete-model")
        backend = mock.Mock(return_value={"status": "success", "exit_code": 0,
                                          "result": "contacted"})
        with mock.patch.dict(_executor.BACKENDS["arkcli"], {"call": backend}), \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            response = _executor.execute_agent(inv, timeout_ms=1000,
                                               launch_control=control)
        self.assertEqual(response["status"], "error")
        self.assertIn("before contact", response["error"])
        backend.assert_not_called()

    def test_api_boundary_registers_and_reaps_without_exposing_origin(self):
        evidence, handles = [], []
        control = ProviderLaunchControl(
            before_launch=lambda item: evidence.append(item),
            on_spawn=lambda item: handles.append(("spawn", item)),
            on_reap=lambda item: handles.append(("reap", item)))
        payload = {"choices": [{"message": {"content": "ok"}}], "model": "m"}
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with mock.patch.object(_apibackend, "_opener", return_value=opener):
            result = _apibackend._do_request(
                "https://private.example/v1", "m", "s", "p", None, 1000,
                "openai-compat", launch_control=control)
        self.assertEqual(result["status"], "success")
        self.assertEqual([item[0] for item in handles], ["spawn", "reap"])
        self.assertNotIn("private.example", json.dumps(evidence))

    def test_acp_control_skips_probe_before_single_popen(self):
        inv = AgentInvocation(cli="kimi", prompt="p", cwd=".", permission="yolo",
                              transport="acp")
        control = ProviderLaunchControl(before_launch=lambda evidence: None)
        with mock.patch.object(_acpbackend, "_probe_acp",
                               side_effect=AssertionError("probe is a second process")), \
             mock.patch.object(_executor, "_resolve_launch", return_value=("kimi", ["acp"])), \
             mock.patch("_builder.build_invocation_args", return_value=("kimi", [], {})), \
             mock.patch.object(_acpbackend.subprocess, "Popen",
                               side_effect=FileNotFoundError) as popen:
            response = _acpbackend.call(inv, 1000, launch_control=control)
        self.assertEqual(response["exit_code"], 127)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
