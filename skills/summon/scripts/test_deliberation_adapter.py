#!/usr/bin/env python3
"""Focused mutation tests for deliberation's real provider launch boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _acpbackend  # noqa: E402
import _apibackend  # noqa: E402
import _executor  # noqa: E402
from _builder import AgentInvocation  # noqa: E402
from _deliberation import (AttemptBinding, DuplicateAttemptError, LaunchToken,
                           SnapshotDriftError, TurnContext)  # noqa: E402
from _deliberation_adapter import FreshDispatchAdapter  # noqa: E402
from _executor import ProviderLaunchControl, ProviderLaunchError  # noqa: E402


SNAPSHOT = "a" * 64


def turn() -> TurnContext:
    return TurnContext("decision", "seat-a", "turn-1", 0,
                       hashlib.sha256(b"TOP SECRET").hexdigest())


def token_for(spec, attempt_id="attempt-1") -> LaunchToken:
    return LaunchToken(AttemptBinding(attempt_id, 1, "decision", "seat-a",
                                      "turn-1", 0, spec.digest))


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
                timeout_ms=1000,
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
            owner_is_current=lambda: True, timeout_ms=1000,
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
            owner_is_current=lambda: True, timeout_ms=1000,
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
            owner_is_current=lambda: True, timeout_ms=1000,
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
            owner_is_current=lambda: owner[0], timeout_ms=1000,
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
            owner_is_current=lambda: True, timeout_ms=1000,
            executor=lambda *args, **kwargs: {"result": "{}", "exit_code": 0})
        forged = TurnContext("decision", "seat-a", "turn-1", 0,
                             hashlib.sha256(b"different prompt").hexdigest())
        spec = adapter.prepare(forged)
        self.assertFalse(adapter.revalidate(spec, forged))

    def test_cleanup_reports_non_process_handle_instead_of_killing_by_pid(self):
        adapter = FreshDispatchAdapter(
            self.invocation(), snapshot_digest=SNAPSHOT,
            current_snapshot_digest=lambda: SNAPSHOT,
            owner_is_current=lambda: True, timeout_ms=1000,
            executor=lambda *args, **kwargs: {})
        adapter._on_spawn(object())
        receipt = adapter.cleanup()
        self.assertFalse(receipt.verified)
        self.assertEqual(receipt.retained_resources,
                         ("registered-provider-operation:cleanup-unverified",))

    def test_unbounded_http_transport_is_refused_for_deliberation(self):
        with self.assertRaisesRegex(ValueError, "cancellation is bounded"):
            FreshDispatchAdapter(
                self.invocation(cli="openai-compat", transport="api"),
                snapshot_digest=SNAPSHOT,
                current_snapshot_digest=lambda: SNAPSHOT,
                owner_is_current=lambda: True,
                timeout_ms=1000,
                executor=lambda *args, **kwargs: {})


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
