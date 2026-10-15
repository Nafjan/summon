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
        for name, expected in (("sol-review", "gpt-6.1-sol"),
                               ("terra-review", "gpt-5.6-terra"),
                               ("luna-review", "gpt-6-luna")):
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
        # This fixture deliberately models the metered Platform /api/v3 route.
        # It is not a shipped Coding Plan seat; those definitions use the
        # byteplus-coding provider and /api/coding/v3.
        import os as _os
        with tempfile.TemporaryDirectory() as cwd:
            invocation = AgentInvocation(
                cli="openai-compat", prompt="probe", cwd=cwd,
                permission="read-only",
                model="deepseek-v4-1-flash-260910",
                base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
                api_key_env="SUMMON_TEST_MISSING_MODELARK_KEY",
                allow_payg=True)
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
                api_key_env="SUMMON_TEST_MISSING_MODELARK_KEY",
                allow_payg=True)
            env = {k: v for k, v in os.environ.items()
                   if k != "SUMMON_TEST_MISSING_MODELARK_KEY"}
            with mock.patch.dict(os.environ, env, clear=True),                     mock.patch("_apibackend._do_request") as request:
                response = _apibackend.call(invocation, 1000)
            self.assertEqual(response.get("provider_contacted"), False)
            self.assertEqual(response.get("error_kind"), "credential_missing")
            self.assertIn("SUMMON_TEST_MISSING_MODELARK_KEY",
                          str(response.get("error")))
            request.assert_not_called()

    def test_direct_platform_payg_refuses_before_credential_or_provider_contact(self):
        import _apibackend
        invocation = AgentInvocation(
            cli="openai-compat", prompt="probe", cwd=".",
            permission="read-only", model="deepseek-v4-1-flash-260910",
            base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            api_key_env="MODELARK_API_KEY", allow_payg=False)
        with mock.patch.object(_apibackend, "payg_consent_allowed",
                               return_value=False), \
                mock.patch.object(_apibackend, "resolve_api_credential") as resolve, \
                mock.patch.object(_apibackend, "_do_request") as request:
            response = _apibackend.call(invocation, 1000)
        self.assertEqual(response.get("error_kind"), "payg_consent_missing")
        self.assertFalse(response.get("provider_contacted"))
        self.assertEqual(response.get("submission_state"), "not_submitted")
        resolve.assert_not_called()
        request.assert_not_called()

    def test_direct_platform_consent_still_requires_declared_key_boundary(self):
        import _apibackend
        invocation = AgentInvocation(
            cli="openai-compat", prompt="probe", cwd=".",
            permission="read-only", model="deepseek-v4-1-flash-260910",
            base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            api_key_env="", allow_payg=True)
        with mock.patch.object(_apibackend, "payg_consent_allowed",
                               return_value=True), \
                mock.patch.object(_apibackend, "resolve_api_credential") as resolve, \
                mock.patch.object(_apibackend, "_do_request") as request:
            response = _apibackend.call(invocation, 1000)
        self.assertEqual(response.get("error_kind"), "credential_missing")
        self.assertFalse(response.get("provider_contacted"))
        self.assertEqual(response.get("submission_state"), "not_submitted")
        resolve.assert_not_called()
        request.assert_not_called()

    def test_direct_platform_dry_run_shows_payg_gate_before_credential_probe(self):
        import _apibackend
        invocation = AgentInvocation(
            cli="openai-compat", prompt="probe", cwd=".",
            permission="read-only", model="deepseek-v4-1-flash-260910",
            base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            api_key_env="MODELARK_API_KEY", allow_payg=False)
        args = SimpleNamespace(
            agent="deepseek-41-flash", _resolved_agent="deepseek-41-flash",
            strict_agents_dir=False, timeout=1000, worktree=None,
            _role_provenance={}, agents_dir=None, gate_with=None,
            allow_text_only=True, allow_credit=False, allow_payg=False)
        with mock.patch.object(_apibackend, "payg_consent_allowed",
                               return_value=False), \
                mock.patch.object(_apibackend, "api_key_available") as key_available:
            view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view.get("would_refuse"))
        self.assertEqual(view.get("error_kind"), "payg_consent_missing")
        self.assertFalse(view.get("provider_contacted"))
        self.assertEqual(view.get("payg_consent"), False)
        self.assertIsNone(view.get("api_key_present"))
        self.assertEqual(view["billing"]["source"], "api")
        self.assertTrue(view["billing"]["payg_consent_required"])
        decision = view["effective_decision"]
        self.assertTrue(decision["candidates"][0]["requires_spend"])
        self.assertFalse(decision["candidates"][0]["eligible"])
        self.assertEqual(decision["authority"]["api"], {
            "authorized": False, "source": "none",
        })
        key_available.assert_not_called()

    def test_direct_platform_dry_run_accepts_explicit_payg_consent(self):
        import _apibackend
        invocation = AgentInvocation(
            cli="openai-compat", prompt="probe", cwd=".",
            permission="read-only", model="deepseek-v4-1-flash-260910",
            base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            api_key_env="MODELARK_API_KEY", allow_payg=True)
        args = SimpleNamespace(
            agent="deepseek-41-flash", _resolved_agent="deepseek-41-flash",
            strict_agents_dir=False, timeout=1000, worktree=None,
            _role_provenance={}, agents_dir=None, gate_with=None,
            allow_text_only=True, allow_credit=False, allow_payg=True)
        with mock.patch.object(_apibackend, "payg_consent_allowed",
                               side_effect=lambda flag=False: bool(flag)), \
                mock.patch.object(_apibackend, "api_key_available",
                                  return_value=True) as key_available:
            view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertFalse(view.get("would_refuse", False))
        self.assertTrue(view.get("payg_consent"))
        self.assertTrue(view["effective_decision"]["candidates"][0]["eligible"])
        self.assertEqual(view["effective_decision"]["authority"]["api"], {
            "authorized": True, "source": "dispatch_flag",
        })
        self.assertEqual(view["billing"]["source"], "api")
        self.assertTrue(view["billing"]["payg_consent_required"])
        key_available.assert_called()

    def test_direct_platform_dry_run_requires_declared_key_after_consent(self):
        import _apibackend
        invocation = AgentInvocation(
            cli="openai-compat", prompt="probe", cwd=".",
            permission="read-only", model="deepseek-v4-1-flash-260910",
            base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            api_key_env="", allow_payg=True)
        args = SimpleNamespace(
            agent="deepseek-41-flash", _resolved_agent="deepseek-41-flash",
            strict_agents_dir=False, timeout=1000, worktree=None,
            _role_provenance={}, agents_dir=None, gate_with=None,
            allow_text_only=True, allow_credit=False, allow_payg=True)
        with mock.patch.object(_apibackend, "payg_consent_allowed",
                               return_value=True), \
                mock.patch.object(_apibackend, "api_key_available") as key_available:
            view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view.get("would_refuse"))
        self.assertEqual(view.get("error_kind"), "credential_missing")
        self.assertTrue(view.get("payg_consent"))
        self.assertIsNone(view.get("api_key_present"))
        key_available.assert_not_called()

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

    def test_byteplus_coding_model_normalization_and_bundled_agents(self):
        from _apibackend import normalize_coding_plan_model
        from _loader import bundled_roster_dir, load_agent, list_agents
        from _resolver import discover_models

        bp_url = "https://ark.ap-southeast.bytepluses.com/api/coding/v3"
        bp_payg_url = "https://ark.ap-southeast.bytepluses.com/api/v3"
        or_url = "https://openrouter.ai/api/v1"
        self.assertEqual(normalize_coding_plan_model(bp_url, "glm-5.3-flash"), "glm-5-3-flash")
        self.assertEqual(normalize_coding_plan_model(bp_url, "glm-5-3-flash-260828"), "glm-5-3-flash-260828")
        self.assertEqual(normalize_coding_plan_model(bp_url, "deepseek-v4-1-flash-260910"), "deepseek-v4-1-flash-260910")
        self.assertEqual(normalize_coding_plan_model(bp_payg_url, "glm-5.3-flash"), "glm-5.3-flash")
        self.assertEqual(normalize_coding_plan_model(bp_payg_url, "deepseek-v4-1-flash-260910"), "deepseek-v4-1-flash-260910")
        self.assertEqual(normalize_coding_plan_model(or_url, "z-ai/glm-5.3-flash"), "z-ai/glm-5.3-flash")

        bdir = bundled_roster_dir()
        glm = load_agent(bdir, "byteplus-glm-5-3-flash")
        bpc = load_agent(bdir, "byteplus-coder")
        self.assertEqual((glm[0], glm[5]), ("openai-compat", "glm-5-3-flash"))
        self.assertEqual((bpc[0], bpc[5]), ("openai-compat", "deepseek-v4-pro"))

        default_disc = discover_models()
        self.assertNotIn("openai-compat", default_disc)
        disc = discover_models("openai-compat")
        self.assertIn("openai-compat", disc)
        self.assertIn("glm-5-3-flash", disc["openai-compat"]["models"])

        roster = {a["name"]: a for a in list_agents(bdir)}
        self.assertTrue(roster["byteplus-glm-5-3-flash"]["reports_served_model"])
        with tempfile.TemporaryDirectory() as tmp:
            ark_md = os.path.join(tmp, "ark-test.md")
            with open(ark_md, "w", encoding="utf-8") as fh:
                fh.write("---\nrun-agent: arkcli\nmodel: deepseek-v4-1-flash-260910\npermission: read-only\n---\nArk\n")
            kimi_md = os.path.join(tmp, "kimi-test.md")
            with open(kimi_md, "w", encoding="utf-8") as fh:
                fh.write("---\nrun-agent: kimi\nmodel: k2.6\npermission: read-only\n---\nKimi\n")
            tmp_roster = {a["name"]: a for a in list_agents(tmp)}
            self.assertTrue(tmp_roster["ark-test"]["reports_served_model"])
            self.assertFalse(tmp_roster["kimi-test"]["reports_served_model"])

    def test_doctor_detects_shadowed_bundled_agents(self):
        import _doctor
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "sol-review.md")
            with open(stale, "w", encoding="utf-8") as fh:
                fh.write("---\nrun-agent: codex\nmodel: gpt-5.6-sol\npermission: read-only\n---\nStale\n")
            ad = _doctor._check_agents_dir(tmp, tmp)
        self.assertTrue(ad["found"])
        shadowed = ad.get("shadowed_bundled_agents") or []
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0]["name"], "sol-review")
        self.assertEqual(shadowed[0]["active_model"], "gpt-5.6-sol")
        self.assertEqual(shadowed[0]["bundled_model"], "gpt-6.1-sol")

    def test_resolve_codex_binary_ranks_highest_semver(self):
        from _builder import resolve_codex_binary
        with tempfile.TemporaryDirectory() as tmp:
            la = os.path.join(tmp, "LocalAppData")
            bin_old = os.path.join(la, "OpenAI", "Codex", "bin", "aaaa", "codex.exe")
            bin_new = os.path.join(la, "OpenAI", "Codex", "bin", "bbbb", "codex.exe")
            os.makedirs(os.path.dirname(bin_old), exist_ok=True)
            os.makedirs(os.path.dirname(bin_new), exist_ok=True)
            with open(bin_old, "w", encoding="utf-8") as fh:
                fh.write("old")
            with open(bin_new, "w", encoding="utf-8") as fh:
                fh.write("new")

            def _fake_semver(p: str):
                return (0, 162, 0, 2) if "bbbb" in p else (0, 118, 0, 9999)

            with mock.patch("_builder._codex_semver_key", side_effect=_fake_semver), \
                 mock.patch.dict(os.environ, {"SUMMON_CODEX_BIN": "", "CODEX_CLI": ""}, clear=False):
                chosen = resolve_codex_binary(
                    which_fn=lambda _: None,
                    localappdata=la,
                    codex_home=os.path.join(tmp, "empty-codex-home"),
                )
            self.assertEqual(chosen, bin_new)

    def test_arkcli_provenance_and_canonical_model_matching(self):
        import json
        import _arkcli_backend
        import _executor
        from _apibackend import normalize_coding_plan_model
        inv = AgentInvocation(
            cli="arkcli", model="deepseek-v4-1-flash-260910",
            prompt="ping", cwd=tempfile.gettempdir(), permission="read-only",
        )
        with mock.patch("_arkcli_backend.shutil.which", return_value=None):
            missing_resp = _arkcli_backend.call(inv, timeout_ms=5000)
        self.assertEqual(missing_resp["status"], "error")

        fake_plain = SimpleNamespace(returncode=0, stdout="STATUS: DONE\nSUMMARY: ok", stderr="")
        with mock.patch("_arkcli_backend.shutil.which", return_value="/usr/bin/arkcli"), \
             mock.patch("_arkcli_backend._arkcli_cmd", return_value=["arkcli"]), \
             mock.patch("subprocess.run", return_value=fake_plain):
            plain_resp = _arkcli_backend.call(inv, timeout_ms=5000)
        self.assertIsNone(plain_resp["model"]["served"])
        self.assertIsNone(plain_resp["model_resolved"])
        self.assertIsNone(plain_resp["model_evidence_source"])

        fake_json = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": "STATUS: DONE", "model": "deepseek-v4-1-flash-260910"}),
            stderr="",
        )
        with mock.patch("_arkcli_backend.shutil.which", return_value="/usr/bin/arkcli"), \
             mock.patch("_arkcli_backend._arkcli_cmd", return_value=["arkcli"]), \
             mock.patch("subprocess.run", return_value=fake_json):
            json_resp = _arkcli_backend.call(inv, timeout_ms=5000)
        self.assertEqual(json_resp["model"]["served"], "deepseek-v4-1-flash-260910")
        self.assertEqual(json_resp["model_evidence_source"], "arkcli_json_envelope")

        self.assertIs(
            _executor.model_match_state(
                "glm-5-3-flash", "glm-5-3-flash", "glm-5-3-flash-260828", "reported"
            ),
            True,
        )
        self.assertIs(
            _executor.model_match_state(
                "deepseek-v4-1-flash", "deepseek-v4-1-flash", "deepseek-v4-1-flash-260910", "reported"
            ),
            True,
        )
        self.assertIs(
            _executor.model_match_state(
                "openrouter/z-ai/glm-5.3-flash", "z-ai/glm-5.3-flash", "glm-5-3-flash-260828", "reported"
            ),
            True,
        )
        self.assertFalse(_executor._model_mismatch("glm-5-3-flash", "glm-5-3-flash-260828"))
        self.assertFalse(_executor._model_mismatch("deepseek-v4-1-flash", "deepseek-v4-1-flash-260910"))
        # Explicit dated snapshot requests must reject different served dated snapshots
        self.assertTrue(
            _executor._model_mismatch("deepseek-v4-1-flash-260910", "deepseek-v4-1-flash-250101")
        )
        self.assertIs(
            _executor.model_match_state(
                "deepseek-v4-1-flash-260910",
                "deepseek-v4-1-flash-260910",
                "deepseek-v4-1-flash-250101",
                "reported",
            ),
            False,
        )
        self.assertEqual(
            normalize_coding_plan_model("byteplus-coding", "deepseek-v4-1-flash-260910"),
            "deepseek-v4-1-flash-260910",
        )
        self.assertEqual(
            normalize_coding_plan_model("byteplus-coding", "glm-5-3-flash-260828"),
            "glm-5-3-flash-260828",
        )
        self.assertEqual(
            normalize_coding_plan_model("byteplus-coding", "glm-5.3-flash"),
            "glm-5-3-flash",
        )
        self.assertEqual(
            normalize_coding_plan_model("byteplus-coding", "glm-5.1"),
            "glm-5.1",
        )
        self.assertIs(
            _executor.model_match_state("opus", "opus", "claude-opus-5", "reported"),
            False,
        )

    def test_codex_rollout_turn_context_populates_targeted_only_and_fails_closed(self):
        import json
        import _executor
        with tempfile.TemporaryDirectory() as tmp:
            sid = "01a13946-662e-7292-a577-1c549685d8e7"
            sdir = os.path.join(tmp, "sessions", "2026", "10", "14")
            os.makedirs(sdir, exist_ok=True)
            rfile = os.path.join(sdir, f"rollout-2026-10-14T10-17-57-{sid}.jsonl")
            with open(rfile, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "session_meta", "payload": {"session_id": sid}}) + "\n")
                fh.write(json.dumps({"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-luna"}}) + "\n")
                fh.write(json.dumps({"type": "event_msg", "payload": {"turn_id": "t1", "type": "task_complete"}}) + "\n")
                fh.write(json.dumps({"type": "turn_context", "payload": {"turn_id": "t2", "model": "gpt-6.1-sol"}}) + "\n")
                fh.write(json.dumps({"type": "event_msg", "payload": {"turn_id": "t2", "type": "task_complete"}}) + "\n")
            t_mod, used_mods = _executor._codex_rollout_model_evidence(sid, codex_home=tmp)
            self.assertEqual(t_mod, "gpt-6.1-sol")
            self.assertEqual(used_mods, ["gpt-6.1-sol"])

            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}, clear=False):
                enriched = _executor._enrich(
                    {
                        "status": "success",
                        "exit_code": 0,
                        "cli": "codex",
                        "session_id": sid,
                        "result": (
                            "STATUS: DONE\nSUMMARY: ok\nCONFIDENCE: 0.9\n"
                            "FOLLOW-UP: None\nHANDOFF: None\n"
                        ),
                    },
                    None,
                )
            self.assertEqual(enriched["model_targeted"], "gpt-6.1-sol")
            self.assertIsNone(enriched["model_resolved"])
            self.assertEqual(enriched["models_used"], ["gpt-6.1-sol"])
            self.assertEqual(enriched["model_evidence_source"], "codex_rollout_turn_context")

            # An unexecuted trailing turn_context must not inherit earlier turn activity
            with open(rfile, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "turn_context", "payload": {"turn_id": "t3", "model": "gpt-6-astra"}}) + "\n")
            self.assertEqual(_executor._codex_rollout_model_evidence(sid, codex_home=tmp), (None, []))


if __name__ == "__main__":
    unittest.main()

