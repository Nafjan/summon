#!/usr/bin/env python3
"""Regression tests for dispatch outcome and served-model evidence guards."""

from __future__ import annotations

import os
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _executor  # noqa: E402
import _schema  # noqa: E402
from _builder import AgentInvocation  # noqa: E402


class EvidenceGuardTests(unittest.TestCase):
    _REPORT = "STATUS: DONE\nSUMMARY: usable\nFOLLOW-UP: none\nHANDOFF: none"

    def _execute_acp(self, response: dict) -> dict:
        """Drive execute_agent through the registered ACP callable.

        Patching the nested BACKENDS entry is intentional: it proves the
        executor's transport routing and final envelope guard without starting
        a vendor process or relying on a provider-specific test double.
        """
        invocation = AgentInvocation(
            cli="kimi", prompt="review", cwd=tempfile.gettempdir(),
            permission="yolo", transport="acp")
        backend = mock.Mock(return_value=dict(response))
        with mock.patch.dict(_executor.BACKENDS["kimi"]["acp"],
                             {"call": backend}), \
             mock.patch("_receipt.workspace_snapshot",
                        return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}), \
             mock.patch.object(_executor.subprocess, "Popen") as popen:
            result = _executor.execute_agent(invocation, timeout_ms=1000)
        backend.assert_called_once()
        popen.assert_not_called()
        return result

    def _execute_codex_stream(self, *, served_model=None,
                              handshake_model="gpt-5.6-sol") -> dict:
        """Run the real subprocess stream parser against a deterministic fake Codex."""
        import subprocess
        lines = [
            {"type": "thread.started", "thread_id": "thread-test",
             "model": handshake_model},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": self._REPORT}},
            {"type": "turn.completed", "usage": {"output_tokens": 3}},
        ]
        if served_model is not None:
            lines[-1]["model"] = served_model
        code = "import json; " + "; ".join(
            f"print({json.dumps(json.dumps(line))})" for line in lines)
        invocation = AgentInvocation(
            cli="codex", model="gpt-5.6-sol", prompt="review",
            cwd=tempfile.gettempdir(), permission="read-only")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=(sys.executable, ("-c", code), None)), \
             mock.patch("_receipt.workspace_snapshot",
                        return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            result = _executor.execute_agent(invocation, timeout_ms=5000)
        return result

    def test_acp_empty_success_is_normalized_as_one_consistent_error_tuple(self):
        result = self._execute_acp({
            "result": "", "exit_code": 0, "status": "success", "cli": "kimi",
        })
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["execution_status"], "error")
        self.assertEqual(result["dispatcher_status"], "error")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("empty terminal result", result["normalization_reason"])
        self.assertEqual(result["served_model_evidence"], "absent")
        self.assertTrue(result["suspect"])
        # Keep the raw provider exit separately; the dispatcher outcome is the
        # field orchestration branches on after normalization.
        self.assertEqual(result["backend_exit_code"], 0)

    def test_success_without_served_evidence_is_terminal_but_warns(self):
        result = self._execute_acp({
            "result": self._REPORT, "exit_code": 0,
            "status": "success", "cli": "kimi",
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution_status"], "success")
        self.assertEqual(result["dispatcher_status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["served_model_evidence"], "absent")
        self.assertNotIn("suspect", result)
        self.assertTrue(any("provenance is not confirmed" in w
                            for w in result.get("warnings", [])))

    def test_explicit_codex_model_without_terminal_receipt_is_blocked(self):
        result = self._execute_codex_stream()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "served_model_unverified")
        self.assertFalse(result["result_usable"])
        self.assertTrue(result["provider_contacted"])
        self.assertEqual(result["model"]["requested"], "gpt-5.6-sol")
        self.assertEqual(result["model"]["targeted"], "gpt-5.6-sol")
        self.assertIsNone(result["model"]["served"])
        # A handshake is retained for legacy consumers, but it is not treated
        # as served evidence (the exact request is still blocked).
        self.assertEqual(result["model"]["resolved"], "gpt-5.6-sol")

    def test_explicit_codex_model_without_any_identity_does_not_inherit_default(self):
        result = self._execute_codex_stream(handshake_model=None)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "served_model_unverified")
        self.assertIsNone(result["model"]["served"])
        # The ambient Codex default is not evidence about this invocation and
        # must not make an explicit Sol request look like it ran on Luna.
        self.assertIsNone(result["model"]["resolved"])

    def test_explicit_codex_model_mismatch_is_blocked_and_not_retryable(self):
        result = self._execute_codex_stream(served_model="gpt-5.6-luna")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "served_model_mismatch")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["result_usable"])
        self.assertEqual(result["model"]["served"], "gpt-5.6-luna")
        self.assertTrue(_executor.is_terminal_nonretryable(result))

    def test_explicit_codex_handshake_mismatch_is_blocked(self):
        result = self._execute_codex_stream(handshake_model="gpt-5.6-luna",
                                            served_model="gpt-5.6-sol")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "target_model_mismatch")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["result_usable"])
        self.assertEqual(result["model"]["targeted"], "gpt-5.6-luna")

    def test_codex_model_mismatch_never_retries(self):
        import run_subagent
        result = self._execute_codex_stream(served_model="gpt-5.6-luna")
        args = mock.Mock(retries=3, timeout=5000, debug_dir=None,
                         max_tool_output_bytes=None, transient_retries=True,
                         no_acp_fallback=False)
        invocation = AgentInvocation(
            cli="codex", model="gpt-5.6-sol", prompt="review",
            cwd=tempfile.gettempdir(), permission="read-only")
        with mock.patch.object(run_subagent, "execute_agent",
                               return_value=result) as execute:
            final = run_subagent._dispatch_with_retries(invocation, args)
        execute.assert_called_once()
        self.assertEqual(final["error_kind"], "served_model_mismatch")
        self.assertTrue(final["retry_suppressed"])

    def test_success_with_terminal_model_is_reported(self):
        result = self._execute_acp({
            "result": self._REPORT, "exit_code": 0,
            "status": "success", "cli": "kimi",
            "model_resolved": "kimi-code/k3",
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["served_model_evidence"], "reported")
        self.assertEqual(result["model"]["served"], "kimi-code/k3")

    def test_malformed_terminal_model_never_becomes_public_provenance(self):
        for value in (["bad"], {"id": "forged"}, 7, True, "   ",
                      r"C:\private\model", "https://example.invalid/model",
                      "Bearer secret", "sk-live-secret", "foo/../../private",
                      "org/token/secret", "org/api-key/live", "org/password/value",
                      "Users/example/AppData/model"):
            with self.subTest(value=value):
                result = self._execute_acp({
                    "result": self._REPORT, "exit_code": 0,
                    "status": "success", "cli": "kimi",
                    "model_resolved": value,
                })
                self.assertIsNone(result["model"]["served"])
                self.assertEqual(result["served_model_evidence"], "absent")
                self.assertIsNone(result["model"]["resolved"])
                self.assertNotIn(str(value), repr(result["model"]))

    def test_output_tokens_without_terminal_model_are_inferred(self):
        result = self._execute_acp({
            "result": self._REPORT, "exit_code": 0,
            "status": "success", "cli": "kimi",
            "model_targeted": "kimi-code/k3",
            "usage": {"output_tokens": 12},
        })
        self.assertEqual(result["served_model_evidence"], "inferred")
        self.assertEqual(result["model"]["served"], "kimi-code/k3")
        self.assertNotIn("suspect", result)

    def test_invalid_target_cannot_be_used_for_inferred_provenance(self):
        result = self._execute_acp({
            "result": self._REPORT, "exit_code": 0,
            "status": "success", "cli": "kimi",
            "model_targeted": {"id": "forged"},
            "usage": {"output_tokens": 12},
        })
        self.assertIsNone(result["model"]["targeted"])
        self.assertIsNone(result["model"]["served"])
        self.assertEqual(result["served_model_evidence"], "absent")

    def test_empty_terminal_is_not_retried_by_the_dispatch_loop(self):
        args = mock.Mock(retries=3, timeout=1000, debug_dir=None,
                         max_tool_output_bytes=None)
        invocation = AgentInvocation(
            cli="kimi", prompt="review", cwd=tempfile.gettempdir(),
            permission="yolo", transport="acp")
        empty = _executor.normalize_empty_success({
            "result": "", "exit_code": 0, "status": "success", "cli": "kimi",
        })
        # Import the retry helper lazily to keep this focused test's setup
        # identical to the command path. run_subagent binds execute_agent at
        # import time, so patch that actual seam rather than the module source.
        import run_subagent
        with mock.patch.object(run_subagent, "execute_agent",
                               return_value=empty) as execute:
            result = run_subagent._dispatch_with_retries(invocation, args)
        execute.assert_called_once()
        self.assertEqual(result["error_kind"], "empty_terminal_result")
        self.assertTrue(result["retry_suppressed"])

    def test_manifest_terminal_helper_suppresses_only_typed_empty_result(self):
        from _executor import is_terminal_nonretryable
        self.assertTrue(is_terminal_nonretryable({
            "status": "error", "error_kind": "empty_terminal_result",
            "retryable": False,
        }))
        self.assertFalse(is_terminal_nonretryable({
            "status": "error", "error_kind": "provider_unavailable",
            "retryable": False,
        }))

    def test_manifest_resume_suppresses_empty_until_explicit_retry(self):
        import _manifest

        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "manifest.json")
            results_dir = os.path.join(root, "results")
            os.makedirs(results_dir)
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump({"jobs": [{"id": "empty", "agent": "worker",
                                     "cli": "codex", "prompt": "review"}]}, fh)
            result_path = os.path.join(results_dir, "empty.json")
            with open(result_path, "w", encoding="utf-8") as fh:
                json.dump({"status": "error", "error_kind": "empty_terminal_result",
                           "retryable": False, "request_sha256": "fp"}, fh)

            def run_args(force=False):
                return argparse.Namespace(
                    manifest=manifest_path, concurrency=None, cwd=root,
                    results_dir=results_dir, agents_dir=None,
                    strict_agents_dir=False, enable_roles=False, retries=0,
                    retry_nonretryable=force,
                )

            identity = {"agent": "worker", "prompt": "review", "cwd": root}
            child_calls = []
            def unexpected_child(*args, **kwargs):
                child_calls.append(args[0])
                raise AssertionError("typed empty result was dispatched without override")

            output = io.StringIO()
            with mock.patch.object(_manifest, "_job_identity", return_value=identity), \
                 mock.patch.object(_manifest, "_dispatch_child", side_effect=unexpected_child), \
                 mock.patch("_executor.request_fingerprint", return_value="fp"), \
                 contextlib.redirect_stdout(output):
                code = _manifest.run_manifest(run_args())
            summary = json.loads(output.getvalue().strip())
            self.assertEqual(code, 1)
            self.assertEqual(child_calls, [])
            self.assertEqual(summary["skipped"], ["empty"])
            self.assertEqual(summary["retry_suppressed"], ["empty"])

            def forced_child(cmd, timeout, **kwargs):
                child_calls.append(cmd)
                self.assertIn("--retry-nonretryable", cmd)
                with open(result_path, "w", encoding="utf-8") as fh:
                    json.dump({"status": "success", "result": "ok",
                               "request_sha256": "fp"}, fh)
                return _manifest._ChildResult(0, "", "", False), None

            output = io.StringIO()
            with mock.patch.object(_manifest, "_job_identity", return_value=identity), \
                 mock.patch.object(_manifest, "_dispatch_child", side_effect=forced_child), \
                 mock.patch("_executor.request_fingerprint", return_value="fp"), \
                 contextlib.redirect_stdout(output):
                code = _manifest.run_manifest(run_args(force=True))
            summary = json.loads(output.getvalue().strip())
            self.assertEqual(code, 0)
            self.assertEqual(len(child_calls), 1)
            self.assertEqual(summary["skipped"], [])
            self.assertEqual(summary["retry_suppressed"], [])

    def test_schema_parser_has_no_independent_payload_for_empty_result(self):
        response = {"result": ""}
        _schema.attach_parsed(response, {
            "type": "object", "required": ["answer"],
        })
        self.assertFalse(response["parse_ok"])
        self.assertIsNone(response["parsed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
