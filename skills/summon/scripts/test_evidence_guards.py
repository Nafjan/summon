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
from _loader import parse_frontmatter  # noqa: E402
from _builder import AgentInvocation  # noqa: E402


class EvidenceGuardTests(unittest.TestCase):
    _REPORT = "STATUS: DONE\nSUMMARY: usable\nFOLLOW-UP: none\nHANDOFF: none"

    def test_exact_model_policy_is_opt_in_or_reserved_named_seat(self):
        self.assertEqual(
            _executor.model_exact_policy("architect", {"model": "claude-opus-5"}),
            (True, "named-seat"),
        )
        self.assertEqual(
            _executor.model_exact_policy("custom-review", {"model-policy": "exact"}),
            (True, "frontmatter"),
        )
        self.assertEqual(
            _executor.model_exact_policy("worker", {"model": "kimi-code/k3"}, True),
            (True, "cli"),
        )
        self.assertEqual(
            _executor.model_exact_policy("worker", {"model": "kimi-code/k3"}),
            (False, None),
        )
        frontmatter, _ = parse_frontmatter(
            "---\nmodel: claude-opus-5\nmodel-policy: exact\n---\nReview\n"
        )
        self.assertEqual(frontmatter["model-policy"], "exact")

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

    def _execute_claude_stream(self, *, served_model="claude-opus-5",
                               requested_model="claude-opus-5",
                               handshake_model="claude-opus-5",
                               exact_required=True, include_model_usage=True,
                               aggregate_only=False) -> dict:
        """Drive the Claude JSONL result path with deterministic modelUsage."""
        import subprocess
        usage = {
            "claude-opus-5": {"inputTokens": 2, "outputTokens": 1},
            "claude-haiku-4-5-20251001": {"inputTokens": 2, "outputTokens": 5},
        }
        terminal = {
            "type": "result", "subtype": "success", "result": self._REPORT,
            "modelUsage": usage, "usage": {"output_tokens": 6},
            "total_cost_usd": 0,
        }
        if served_model == "claude-opus-5":
            terminal["modelUsage"] = {
                "claude-opus-5": {"inputTokens": 2, "outputTokens": 6},
                "claude-haiku-4-5-20251001": {"inputTokens": 2, "outputTokens": 1},
            }
        elif served_model == "claude-haiku-4-5-20251001":
            terminal["modelUsage"] = {
                "claude-opus-5": {"inputTokens": 2, "outputTokens": 1},
                "claude-haiku-4-5-20251001": {"inputTokens": 2, "outputTokens": 6},
            }
        elif served_model is not None:
            terminal["modelUsage"] = {
                served_model: {"inputTokens": 2, "outputTokens": 6},
            }
        if not include_model_usage:
            terminal.pop("modelUsage", None)
        elif served_model is not None and not aggregate_only:
            terminal["model"] = served_model
        lines = [
            {"type": "system", "subtype": "init", "session_id": "session-test",
             "model": handshake_model},
            terminal,
        ]
        code = "import json; " + "; ".join(
            f"print({json.dumps(json.dumps(line))})" for line in lines)
        invocation = AgentInvocation(
            cli="claude", model=requested_model, prompt="review",
            cwd=tempfile.gettempdir(), permission="read-only",
            model_exact_required=exact_required, model_exact_source="test")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=(sys.executable, ("-c", code), None)), \
             mock.patch("_receipt.workspace_snapshot",
                        return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            return _executor.execute_agent(invocation, timeout_ms=5000)

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

    def test_complete_report_with_missing_child_tool_stays_advisory_error(self):
        """A report is retained, but model text cannot override a failed process."""
        raw = self._REPORT + '\nexec: "grep": executable file not found in %PATH%\n'
        base = _executor.build_final_response("agy", 1, None, [raw], "")
        result = _executor._enrich(base, None)
        _executor._attach_eligibility(result)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["backend_exit_code"], 1)
        self.assertTrue(result["report_ok"])
        self.assertEqual(result["tool_failure"]["kind"], "missing_executable")
        self.assertEqual(result["tool_failure"]["executable"], "grep")
        self.assertTrue(result["tool_failure"]["fatal"])
        self.assertFalse(result["result_usable"])
        self.assertNotEqual(result.get("error_kind"), "authentication_failed")

    def test_incomplete_report_missing_child_tool_is_typed_not_auth(self):
        raw = 'STATUS: PARTIAL\nSUMMARY: stopped\nexec: "grep": executable file not found in %PATH%\n'
        base = _executor.build_final_response("agy", 1, None, [raw], "")
        result = _executor._enrich(base, None)
        _executor._attach_eligibility(result)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_kind"], "missing_executable")
        self.assertFalse(result["result_usable"])
        self.assertFalse(result.get("interactive_required", True))
        self.assertNotIn("auth", result)

    def test_terminal_error_missing_child_tool_cannot_be_promoted_by_report(self):
        terminal = {
            "type": "result",
            "status": "error",
            "error": 'exec: "grep": executable file not found in %PATH%',
            "result": self._REPORT,
        }
        base = _executor.build_final_response(
            "gemini", 1, terminal, [json.dumps(terminal)], "")
        result = _executor._enrich(base, None)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["report_ok"])
        self.assertEqual(result["tool_failure"]["executable"], "grep")

    def test_clean_terminal_plus_fatal_missing_tool_stays_unusable_error(self):
        terminal = {"type": "result", "status": "success", "result": self._REPORT}
        raw = self._REPORT + '\nexec: "grep": executable file not found in %PATH%\n'
        result = _executor._enrich(
            _executor.build_final_response("gemini", 1, terminal, [raw], ""), None)
        _executor._attach_eligibility(result)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["execution_status"], "error")
        self.assertEqual(result["backend_exit_code"], 1)
        self.assertEqual(result["normalized_exit_code"], 1)
        self.assertTrue(result["report_ok"])
        self.assertTrue(result["tool_failure"]["fatal"])
        self.assertFalse(result["result_usable"])

    def test_missing_tool_text_cannot_mask_an_auth_failure(self):
        raw = (self._REPORT + '\nexec: "grep": executable file not found in %PATH%\n'
               + "HTTP 401 Unauthorized\n")
        result = _executor._enrich(
            _executor.build_final_response("agy", 1, None, [raw], ""), None)
        _executor._attach_eligibility(result)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["backend_exit_code"], 1)
        self.assertTrue(result["report_ok"])
        self.assertFalse(result["result_usable"])

    def test_missing_tool_diagnostic_does_not_publish_a_local_path(self):
        details = _executor.missing_executable_details(
            "The term 'C:\\private\\bin\\grep.exe' is not recognized "
            "as the name of a cmdlet")
        self.assertEqual(details["executable"], "grep.exe")
        self.assertNotIn("Users", repr(details))

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

    def test_exact_claude_seat_does_not_attribute_dominant_auxiliary_model(self):
        result = self._execute_claude_stream(
            served_model="claude-haiku-4-5-20251001", aggregate_only=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "served_model_unverified")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["result_usable"])
        self.assertEqual(result["model"]["requested"], "claude-opus-5")
        self.assertIsNone(result["model"]["served"])
        self.assertEqual(result["served_model_evidence"], "absent")
        self.assertEqual(
            result["model"]["models_used"],
            ["claude-haiku-4-5-20251001", "claude-opus-5"],
        )

    def test_exact_claude_seat_accepts_matching_terminal_model(self):
        result = self._execute_claude_stream(served_model="claude-opus-5")
        self.assertEqual(result["status"], "success")
        self.assertNotEqual(result.get("result_usable"), False)
        self.assertEqual(result["model"]["served"], "claude-opus-5")
        self.assertEqual(result["served_model_evidence"], "reported")

    def test_best_effort_claude_mixed_usage_does_not_infer_lead(self):
        result = self._execute_claude_stream(
            exact_required=False, aggregate_only=True)
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["model"]["served"])
        self.assertIsNone(result["model_match"])
        self.assertFalse(result["named_model_verified"])
        self.assertEqual(result["served_model_evidence"], "absent")
        self.assertTrue(any("provenance" in w for w in result["warnings"]))

    def test_ambiguous_claude_target_does_not_claim_backend_ran_model(self):
        result = self._execute_claude_stream(
            requested_model="claude-fable-5-1", exact_required=False,
            aggregate_only=True)
        self.assertIsNone(result["model"]["served"])
        self.assertEqual(result["model"]["targeted"], "claude-opus-5")
        self.assertFalse(any("backend ran" in w for w in result["warnings"]))

    def test_exact_claude_floating_alias_is_not_certified_as_exact(self):
        result = self._execute_claude_stream(
            requested_model="opus", handshake_model="claude-opus-5",
            served_model="claude-opus-5")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "target_model_mismatch")
        self.assertIs(result["model_match"], False)
        self.assertFalse(result["named_model_verified"])

    def test_exact_claude_seat_blocks_missing_terminal_model_evidence(self):
        result = self._execute_claude_stream(include_model_usage=False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "served_model_unverified")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["result_usable"])
        self.assertIsNone(result["model"]["served"])
        self.assertEqual(result["served_model_evidence"], "absent")

    def test_best_effort_claude_pin_keeps_mismatch_advisory(self):
        result = self._execute_claude_stream(
            served_model="claude-haiku-4-5-20251001", exact_required=False)
        self.assertEqual(result["status"], "success")
        self.assertTrue(any("requested model" in warning
                            for warning in result.get("warnings", [])))

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
        self.assertEqual(result["served_model_evidence"], "inferred")
        self.assertFalse(result["named_model_verified"])
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
