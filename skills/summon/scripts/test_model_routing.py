"""Regression tests for deterministic backend/model compatibility preflight."""

from __future__ import annotations

import tempfile
import unittest
import sys
import os
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _builder import AgentInvocation, model_backend_compatibility
from _executor import execute_agent
import run_subagent


class ModelRoutingTests(unittest.TestCase):
    def test_known_cross_vendor_pair_is_blocked_with_explicit_reroutes(self):
        refusal = model_backend_compatibility("codex", "claude-opus-5")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["error_kind"], "backend_model_incompatible")
        self.assertEqual(refusal["model_vendor"], "anthropic")
        self.assertEqual(refusal["recommended_backend"], "claude")
        self.assertIn("claude", refusal["compatible_backends"])

    def test_known_compatible_and_unknown_models_are_not_guessed(self):
        self.assertIsNone(model_backend_compatibility("claude", "claude-opus-5"))
        self.assertIsNone(model_backend_compatibility("codex", "gpt-5.6-sol"))
        self.assertIsNone(model_backend_compatibility("codex", "future-private-model"))

    def test_live_refusal_happens_before_builder_or_process(self):
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="codex", model="claude-opus-5", prompt="probe", cwd=cwd,
                permission="read-only",
            )
            with mock.patch("_executor.build_invocation_args") as builder, \
                    mock.patch("subprocess.Popen") as popen:
                response = execute_agent(invocation, timeout_ms=1000)
            self.assertEqual(response["status"], "blocked")
            self.assertEqual(response["error_kind"], "backend_model_incompatible")
            self.assertEqual(response["model_requested"], "claude-opus-5")
            self.assertEqual(builder.call_count, 0)
            self.assertEqual(popen.call_count, 0)

    def test_dry_run_reports_the_same_refusal_without_contact(self):
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="codex", model="claude-opus-5", prompt="probe", cwd=cwd,
                permission="read-only",
            )
            args = SimpleNamespace(
                agent="architect", _resolved_agent="architect",
                strict_agents_dir=False, timeout=1000, worktree=None,
                _role_provenance={}, agents_dir=None, gate_with=None,
            )
            view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view["would_refuse"])
        self.assertEqual(view["error_kind"], "backend_model_incompatible")
        self.assertEqual(view["recommended_backend"], "claude")


if __name__ == "__main__":
    unittest.main()
