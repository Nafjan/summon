"""Regression tests for deterministic backend/model compatibility preflight."""

from __future__ import annotations

import tempfile
import unittest
import sys
import os
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _builder import (AgentInvocation, model_backend_compatibility,
                      codex_model_selection, build_invocation_args)
from _executor import execute_agent
import run_subagent


class ModelRoutingTests(unittest.TestCase):
    def test_roster_listing_exposes_declared_model_and_effort(self):
        from _loader import list_agents
        with tempfile.TemporaryDirectory() as roster:
            with open(os.path.join(roster, "sol-review.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nrun-agent: codex\nmodel: gpt-5.6-sol\n"
                         "effort: high\npermission: read-only\n---\n# Sol Review\n")
            seat = next(item for item in list_agents(roster)
                        if item["name"] == "sol-review")
        self.assertEqual(seat["run_agent"], "codex")
        self.assertEqual(seat["permission"], "read-only")
        self.assertEqual(seat["model"], "gpt-5.6-sol")
        self.assertEqual(seat["effort"], "high")

    def test_bundled_sol_and_terra_review_seats_are_explicitly_pinned(self):
        from _loader import bundled_roster_dir, load_agent
        roster = bundled_roster_dir()
        self.assertIsNotNone(roster)
        for name, expected in (("sol-review", "gpt-5.6-sol"),
                               ("terra-review", "gpt-5.6-terra"),
                               ("luna-review", "gpt-5.6-luna")):
            loaded = load_agent(roster, name)
            self.assertEqual(loaded[0], "codex")
            self.assertEqual(loaded[4], "read-only")
            self.assertEqual(loaded[5], expected)

    def test_codex_model_selection_collapses_agreeing_selectors(self):
        selection = codex_model_selection(
            "gpt-5.6-sol", ("-m", "gpt-5.6-sol", "-c", "model=gpt-5.6-sol",
                             "-c", "model_reasoning_effort=high"))
        self.assertEqual(selection["canonical"], "gpt-5.6-sol")
        self.assertTrue(selection["exact_required"])
        self.assertIsNone(selection["conflict"])
        inv = AgentInvocation(
            cli="codex", model="gpt-5.6-sol", extra_args=(
             "-m", "gpt-5.6-sol", "-c", "model=gpt-5.6-sol",
             "-c", "model_reasoning_effort=high"), prompt="p", cwd=".")
        command, args, _ = build_invocation_args(inv)
        self.assertEqual(command, "codex")
        self.assertEqual(args.count("-m"), 1)
        self.assertEqual(args[args.index("-m") + 1], "gpt-5.6-sol")
        self.assertIn("-c", args)
        self.assertIn("model_reasoning_effort=high", args)
        self.assertNotIn("model=gpt-5.6-sol", args)

    def test_codex_model_selection_conflict_is_explicit(self):
        selection = codex_model_selection(
            "gpt-5.6-sol", ("--model", "gpt-5.6-luna"))
        self.assertEqual(selection["canonical"], "gpt-5.6-sol")
        self.assertIn("conflicts", selection["conflict"])
        with self.assertRaisesRegex(ValueError, "model selection conflict"):
            build_invocation_args(AgentInvocation(
                cli="codex", model="gpt-5.6-sol",
                extra_args=("--model", "gpt-5.6-luna"), prompt="p", cwd="."))

    def test_codex_model_can_be_pinned_by_legacy_args_only(self):
        selection = codex_model_selection(None, ("-c", "model=gpt-5.6-sol"))
        self.assertEqual(selection["canonical"], "gpt-5.6-sol")
        self.assertEqual(selection["source"], "legacy_args")
        command, args, _ = build_invocation_args(AgentInvocation(
            cli="codex", prompt="p", cwd=".",
            extra_args=("-c", "model=gpt-5.6-sol")))
        self.assertEqual(command, "codex")
        self.assertEqual(args[args.index("-m") + 1], "gpt-5.6-sol")
        self.assertNotIn("model=gpt-5.6-sol", args)

    def test_codex_model_selector_missing_value_blocks(self):
        selection = codex_model_selection("gpt-5.6-sol", ("--model",))
        self.assertIn("missing", selection["conflict"])
        with self.assertRaisesRegex(ValueError, "missing its model value"):
            build_invocation_args(AgentInvocation(
                cli="codex", model="gpt-5.6-sol", prompt="p", cwd=".",
                extra_args=("--model",)))

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

    def test_dry_run_reports_codex_selector_conflict_without_contact(self):
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="codex", model="gpt-5.6-sol", prompt="probe", cwd=cwd,
                permission="read-only", extra_args=("--model", "gpt-5.6-luna"),
            )
            args = SimpleNamespace(
                agent="architect", _resolved_agent="architect",
                strict_agents_dir=False, timeout=1000, worktree=None,
                _role_provenance={}, agents_dir=None, gate_with=None,
                allow_text_only=False, require_tools=False,
            )
            view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view["would_refuse"])
        self.assertEqual(view["error_kind"], "model_selection_conflict")
        self.assertFalse(view["provider_contacted"])
        self.assertFalse(view["result_usable"])


if __name__ == "__main__":
    unittest.main()
