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
    def test_explicit_effort_refuses_unreviewed_kimi_model(self):
        refusal = run_subagent._unsupported_explicit_effort(
            "kimi", "kimi-code/kimi-for-coding", "max")
        self.assertIn("unsupported", refusal)
        self.assertIn("kimi-code/k3", refusal)

    def test_explicit_effort_refuses_backends_without_reviewed_transport(self):
        for backend in ("gemini", "zcode", "openai-compat"):
            with self.subTest(backend=backend):
                refusal = run_subagent._unsupported_explicit_effort(
                    backend, "some-model", "high")
                self.assertTrue(
                    "unsupported" in refusal or "no reviewed transport" in refusal,
                    refusal,
                )

    def test_ambient_effort_remains_best_effort_for_unpinned_route(self):
        self.assertIsNone(run_subagent._unsupported_explicit_effort(
            "kimi", "kimi-code/kimi-for-coding", None))

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

    def test_dry_run_refuses_openai_compat_dispatch_with_missing_credential(self):
        # FF-01: pins the dry-run credential gate itself -- deleting the
        # _dry_run_view credential block must fail this test.
        import os as _os
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="openai-compat", prompt="probe", cwd=cwd,
                permission="read-only",
                model="deepseek-v4-1-flash-260910",
                base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
                api_key_env="SUMMON_TEST_MISSING_MODELARK_KEY")
            args = SimpleNamespace(
                agent="deepseek-41-flash", _resolved_agent="deepseek-41-flash",
                strict_agents_dir=False, timeout=1000, worktree=None,
                _role_provenance={}, agents_dir=None, gate_with=None,
                allow_text_only=True)
            env = {k: v for k, v in _os.environ.items()
                   if k != "SUMMON_TEST_MISSING_MODELARK_KEY"}
            with mock.patch.dict(_os.environ, env, clear=True):
                view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view.get("would_refuse"))
        self.assertEqual(view.get("error_kind"), "credential_missing")
        self.assertIn("SUMMON_TEST_MISSING_MODELARK_KEY", view.get("refusal"))
        self.assertFalse(view.get("provider_contacted"))

    def test_live_openai_compat_preflight_refuses_before_any_request(self):
        # FF-01: the live call() preflight must refuse typed and never issue
        # the HTTP request when the declared credential is missing.
        import _apibackend
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="openai-compat", prompt="probe", cwd=cwd,
                permission="read-only",
                model="deepseek-v4-1-flash-260910",
                base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
                api_key_env="SUMMON_TEST_MISSING_MODELARK_KEY")
            env = {k: v for k, v in os.environ.items()
                   if k != "SUMMON_TEST_MISSING_MODELARK_KEY"}
            with mock.patch.dict(os.environ, env, clear=True),                     mock.patch("_apibackend._do_request") as request:
                response = _apibackend.call(invocation, 1000)
            self.assertEqual(response.get("provider_contacted"), False)
            self.assertEqual(response.get("error_kind"), "credential_missing")
            self.assertIn("SUMMON_TEST_MISSING_MODELARK_KEY",
                          str(response.get("error")))
            request.assert_not_called()

    def test_credential_missing_is_typed_refusal_dry_and_live(self):
        import os as _os
        from _apibackend import api_key_available, resolve_api_credential
        env = {k: v for k, v in _os.environ.items()
               if k != "MODELARK_API_KEY"}
        env["MODELARK_API_KEY"] = ""
        assert resolve_api_credential("MODELARK_API_KEY",
                                      "https://ark.ap-southeast.bytepluses.com/api/v3",
                                      environ=env) == ("", None)  # empty key still refuses
        with mock.patch.dict(_os.environ, env, clear=True):
            assert api_key_available("MODELARK_API_KEY",
                                     "https://ark.ap-southeast.bytepluses.com/api/v3") is False
            assert api_key_available("", "http://127.0.0.1:8000/v1") is True  # local, no key

    def test_agent_required_message_lists_registered_agents_for_the_cli(self):
        rows = [{"name": "kimi-worker", "run_agent": "kimi"},
                {"name": "kimi-coder", "run_agent": "kimi"},
                {"name": "editor", "run_agent": "claude"}]
        args = SimpleNamespace(cli="kimi")
        msg = run_subagent._agent_required_message(args, rows)
        self.assertIn("--agent is required", msg)
        self.assertIn("Registered agents for CLI 'kimi'", msg)
        self.assertIn("kimi-worker", msg)
        self.assertIn("kimi-coder", msg)
        self.assertNotIn("editor", msg)
        empty = run_subagent._agent_required_message(SimpleNamespace(cli="kimi"), [])
        self.assertIn("No registered agents for CLI 'kimi'", empty)
        plain = run_subagent._agent_required_message(SimpleNamespace(cli=None), rows)
        self.assertEqual(plain, "--agent is required")

    def test_agents_table_renders_human_columns(self):
        table = run_subagent._format_agents_table([
            {"name": "kimi-worker", "run_agent": "kimi", "model": "kimi-code/k3",
             "permission": "yolo"},
            {"name": "editor", "run_agent": "claude", "model": None,
             "permission": "read-only"}])
        self.assertIn("NAME", table)
        self.assertIn("CLI", table)
        self.assertNotIn("CAPABILITY", table)  # column dropped: never populated
        self.assertIn("kimi-worker", table)
        self.assertIn("editor", table)
        self.assertIn("-", table)  # empty cells render as dash
        empty = run_subagent._format_agents_table([])
        self.assertIn("NAME", empty)  # empty roster renders headers, no crash

    def test_modelark_subscription_models_are_arkcli_compatible(self):
        # The BytePlus ModelArk backend is exactly the deepseek/zhipu case:
        # arkcli +chat with a concrete marketplace model id must pass the
        # namespace preflight instead of being refused as cross-vendor.
        self.assertIsNone(model_backend_compatibility("arkcli", "deepseek-v4-1-flash-260910"))
        self.assertIsNone(model_backend_compatibility("arkcli", "glm-5-3-flash-260828"))
        self.assertIsNone(model_backend_compatibility("openai-compat", "deepseek-v4-1-flash-260910"))
        refusal = model_backend_compatibility("codex", "deepseek-v4-1-flash-260910")
        self.assertIsNotNone(refusal)
        self.assertIn("arkcli", refusal["compatible_backends"])

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
