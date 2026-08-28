#!/usr/bin/env python3
"""Provider-inert tests for native ZCode and direct Z.AI Coding Plan support."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _builder
import _executor
import _apibackend
import _stream
import _zcode
import run_subagent
from _builder import AgentInvocation, build_invocation_args, model_backend_compatibility
from _zai_coding_plan import resolve_zai_coding_api_key
from _zcode import ZCodeTarget, parse_zcode_json_output, zcode_result_fields
from _stream import StreamProcessor


class ZCodeParserTests(unittest.TestCase):
    def test_banner_then_terminal_document_is_accepted(self):
        raw = "ZCode starting\n" + json.dumps({
            "response": "STATUS: DONE\nSUMMARY: fixture\nFOLLOW-UP: none\nHANDOFF: none\nLEFT_BEHIND: none",
            "sessionId": "sess_fixture_1",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
            "projection": {"contextWindow": 128000},
        })
        fields = zcode_result_fields(parse_zcode_json_output(raw))
        self.assertTrue(fields["parse_ok"])
        self.assertEqual(fields["session_id"], "sess_fixture_1")
        self.assertEqual(fields["usage"]["total_tokens"], 8)
        self.assertEqual(fields["context_window"], 128000)

    def test_random_or_incomplete_json_is_not_completion_evidence(self):
        self.assertIsNone(parse_zcode_json_output("banner only"))
        self.assertFalse(zcode_result_fields({})["parse_ok"])
        self.assertFalse(zcode_result_fields({"response": "hello", "sessionId": "x"})["parse_ok"])

    def test_stream_byte_cap_fails_closed_instead_of_truncating_success(self):
        processor = StreamProcessor(cli="zcode")
        with mock.patch.object(_stream, "_ZCODE_OUTPUT_MAX_BYTES", 8):
            processor.process_line("{" + "x" * 32)
            processor.finalize_stream()
        self.assertEqual(processor.result_json["status"], "error")
        self.assertIn("exceeded", processor.result_json["error"])


class ZCodeBuilderTests(unittest.TestCase):
    def _inv(self, **kw):
        permission = kw.pop("permission", "yolo")
        allow_tool_credentials = kw.pop("allow_tool_credentials", True)
        return AgentInvocation(
            cli="zcode", prompt="line one\nline two", system_context="system context",
            cwd=tempfile.gettempdir(), permission=permission, isolated_lane=True,
            allow_tool_credentials=allow_tool_credentials, **kw)

    def _target(self):
        return ZCodeTarget(command=sys.executable, prefix_args=(), source="fixture")

    def test_explicit_model_refuses_before_provider_preparation(self):
        refusal = model_backend_compatibility("zcode", "glm-5.3-flash")
        self.assertEqual(refusal["error_kind"], "zcode_model_selector_unsupported")
        self.assertIn("OpenCode/direct", refusal["message"])

    def test_dry_run_refuses_native_model_pin_without_contact_or_keyerror(self):
        invocation = self._inv(model="glm-5.3-flash")
        args = SimpleNamespace(
            agent="zcode-coding-plan", _resolved_agent="zcode-coding-plan",
            strict_agents_dir=False, timeout=1000, worktree=None,
            _role_provenance={}, agents_dir=None, gate_with=None,
            allow_text_only=False, require_tools=False,
        )
        view = run_subagent._dry_run_view(invocation, args, None, None)
        self.assertTrue(view["would_refuse"])
        self.assertEqual(view["error_kind"], "zcode_model_selector_unsupported")
        self.assertEqual(view["attempts"], 0)
        self.assertEqual(view["attempt_status"], "not_run")
        self.assertEqual(view["execution_status"], "not_run")
        self.assertFalse(view["provider_contacted"])

    def test_preflight_accepts_a_discovered_bundle_without_path_shim(self):
        with mock.patch.object(run_subagent.shutil, "which", return_value=None), \
             mock.patch("_zcode.resolve_zcode_cli", return_value=self._target()):
            self.assertIsNone(run_subagent._preflight_backend("zcode"))

    def test_bundle_discovery_prefers_well_known_location_before_registry_hint(self):
        with mock.patch.object(_zcode.os, "name", "nt"), \
             mock.patch("_zcode._well_known_bundle_candidates",
                        return_value=[("well-known", "windows_bundle")]), \
             mock.patch("_zcode._registry_install_locations", return_value=["registry-root"]):
            candidates = _zcode._bundle_candidates()
        self.assertEqual(candidates[0], ("well-known", "windows_bundle"))
        self.assertEqual(candidates[1][1], "windows_registry")

    def test_path_shim_does_not_block_direct_executable_discovery(self):
        target = self._target()
        def which(name):
            return {"zcode": "shim.cmd", "zcode.exe": "direct.exe"}.get(name)
        def resolve_target(path, source, **_kwargs):
            return target if path == "direct.exe" else None
        with mock.patch.object(_zcode.os, "name", "nt"), \
             mock.patch("_zcode._bundle_candidates", return_value=[]), \
             mock.patch("_zcode._target", side_effect=resolve_target):
            resolved = _zcode.resolve_zcode_cli(which=which)
        self.assertEqual(resolved, target)

    def test_preflight_names_rejected_path_shim_without_treating_it_as_available(self):
        with mock.patch("_zcode.resolve_zcode_cli", return_value=None), \
             mock.patch("_zcode.rejected_zcode_path_shim", return_value=True), \
             mock.patch("_doctor.doctor", return_value={"usable_backends": []}):
            result = run_subagent._preflight_backend("zcode")
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(result["provider_contacted"])
        self.assertIn("command-wrapper", result["error"])

    def test_stale_attachment_janitor_is_bounded_and_regular_files_only(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("_builder.tempfile.gettempdir", return_value=directory):
            stale = Path(directory) / "summon-zcode-stale.md"
            stale.write_text("fixture", encoding="utf-8")
            old = time.time() - _builder._ZCODE_STALE_ATTACHMENT_TTL_SECONDS - 1
            os.utime(stale, (old, old))
            fresh = Path(directory) / "summon-zcode-fresh.md"
            fresh.write_text("fixture", encoding="utf-8")
            _builder._reap_stale_zcode_attachments()
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())

    def test_builder_uses_private_attachment_not_prompt_argv(self):
        with mock.patch("_zcode.resolve_zcode_cli", return_value=self._target()), \
             mock.patch("_builder._lock_zcode_attachment"):
            _command, args, env = build_invocation_args(self._inv())
        self.assertEqual(_command, sys.executable)
        self.assertIn("--attach", args)
        attachment = args[args.index("--attach") + 1]
        self.assertNotIn("line one\nline two", args)
        self.assertIn("--prompt", args)
        self.assertNotIn("SUMMON_ZCODE_ATTACH_FILE", env or {})
        self.assertTrue(Path(attachment).is_file())
        _builder.cleanup_zcode_attachment(args)
        self.assertFalse(Path(attachment).exists())

    def test_builder_rejects_unsafe_modes_and_bad_resume(self):
        with self.assertRaisesRegex(ValueError, "safe-edit"):
            build_invocation_args(self._inv(permission="safe-edit"))
        with self.assertRaisesRegex(ValueError, "requires --worktree or --isolated-lane"):
            build_invocation_args(AgentInvocation(
                cli="zcode", prompt="p", cwd=tempfile.gettempdir(), permission="yolo"))
        with self.assertRaisesRegex(ValueError, "sess_"):
            build_invocation_args(self._inv(resume_id="not-a-session"))

    def test_yolo_requires_acknowledgement_and_scrubs_provider_environment(self):
        with self.assertRaisesRegex(ValueError, "allow-tool-credentials"):
            build_invocation_args(self._inv(allow_tool_credentials=False))
        with mock.patch("_zcode.resolve_zcode_cli", return_value=self._target()), \
             mock.patch("_builder._lock_zcode_attachment"), \
             mock.patch.dict(os.environ, {"ZAI_CODING_API_KEY": "fixture", "GLM_TOKEN": "fixture"},
                             clear=False):
            _command, args, env = build_invocation_args(self._inv())
        _builder.cleanup_zcode_attachment(args)
        self.assertIsNone(env["ZAI_CODING_API_KEY"])
        self.assertIsNone(env["GLM_TOKEN"])

    def test_effective_permission_never_upgrades_refused_safe_edit_to_yolo(self):
        from _backend_policy import effective_permission, permission_enforcement
        self.assertEqual(effective_permission("zcode", "safe-edit"), "unenforceable")
        self.assertEqual(permission_enforcement("zcode", "safe-edit"), "unenforceable")

    def test_windows_attachment_acl_uses_effective_token_principal(self):
        proc = SimpleNamespace(returncode=0, stdout="DOMAIN\\worker\n")
        with mock.patch.object(_builder.os, "name", "nt"), \
             mock.patch("_builder.subprocess.run", return_value=proc) as command:
            self.assertEqual(_builder._windows_current_principal(), "DOMAIN\\worker")
        self.assertEqual(command.call_args.args[0], ["whoami"])

    def test_zcode_request_identity_binds_isolation_and_credential_acknowledgement(self):
        base = dict(agent=None, prompt="p", cwd=tempfile.gettempdir(), cli="zcode",
                    model=None, allow_tool_credentials=True)
        baseline = _executor.request_fingerprint(**_executor.build_request_identity(
            **base, isolated_lane=False))
        isolated = _executor.request_fingerprint(**_executor.build_request_identity(
            **base, isolated_lane=True))
        self.assertNotEqual(baseline, isolated)

    def test_executor_preserves_terminal_only_liveness_and_cleans_attachment(self):
        invocation = self._inv()
        captured = {}
        payload = {
            "response": "STATUS: DONE\nSUMMARY: fixture\nFINDINGS: none\nFOLLOW-UP: none\nHANDOFF: none\nLEFT_BEHIND: none",
            "sessionId": "sess_fixture_2",
            "usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6},
        }
        code = "import json; print('ZCode banner'); print(json.dumps(" + repr(payload) + "))"

        def launch(_command, args):
            captured["attachment"] = args[args.index("--attach") + 1]
            return sys.executable, ("-c", code, *args)

        with mock.patch("_zcode.resolve_zcode_cli", return_value=self._target()), \
             mock.patch("_builder._lock_zcode_attachment"), \
             mock.patch.object(_executor, "_resolve_launch", side_effect=launch), \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["session_id"], "sess_fixture_2")
        self.assertEqual(out["usage"]["total_tokens"], 6)
        self.assertEqual(out["zcode_stream"]["completion_evidence"], "terminal_json")
        self.assertEqual(out["zcode_stream"]["streaming_progress"], "unavailable")
        self.assertIsNone(out["model"]["served"])
        self.assertFalse(Path(captured["attachment"]).exists())


class ZaiCodingPlanCredentialTests(unittest.TestCase):
    def test_direct_provider_is_fixed_to_the_coding_endpoint(self):
        base_url, key_env = _apibackend.resolve_endpoint(
            {"provider": "zai-coding-plan"}, None)
        self.assertEqual(base_url, "https://api.z.ai/api/coding/paas/v4")
        self.assertEqual(key_env, "ZAI_CODING_API_KEY")
        self.assertEqual(_apibackend.coding_plan_billing(base_url)["source"], "subscription")
        with self.assertRaisesRegex(ValueError, "Z.AI Coding Plan endpoint"):
            _apibackend.resolve_endpoint({
                "provider": "zai-coding-plan", "base_url": "https://example.invalid/api/coding/paas/v4"}, None)

    def test_direct_provider_response_is_required_for_named_model_proof(self):
        invocation = AgentInvocation(
            cli="openai-compat", prompt="p", cwd=tempfile.gettempdir(),
            permission="read-only", model="glm-5.3-flash", model_exact_required=True,
            model_exact_source="fixture", base_url="https://api.z.ai/api/coding/paas/v4",
            api_key_env="ZAI_CODING_API_KEY")
        response = {
            "status": "success", "exit_code": 0, "cli": "openai-compat",
            "result": "STATUS: DONE\nSUMMARY: fixture\nFINDINGS: none\nFOLLOW-UP: none\nHANDOFF: none\nLEFT_BEHIND: none",
            "model_resolved": "glm-5.3-flash", "usage": {"completion_tokens": 1},
        }
        original = _builder.BACKENDS["openai-compat"]["call"]
        _builder.BACKENDS["openai-compat"]["call"] = lambda _inv, _timeout: dict(response)
        try:
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        finally:
            _builder.BACKENDS["openai-compat"]["call"] = original
        self.assertEqual(out["model"]["served"], "glm-5.3-flash")
        self.assertEqual(out["served_model_evidence"], "reported")
        self.assertTrue(out["named_model_verified"])

    def test_explicit_env_wins_without_reading_helper_config(self):
        key, source = resolve_zai_coding_api_key(environ={
            "ZAI_CODING_API_KEY": "test-explicit", "ZAI_CODING_HELPER_CONFIG": "missing.yaml"})
        self.assertEqual((key, source), ("test-explicit", "env"))

    def test_bounded_top_level_coding_plan_scalars_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: glm_coding_plan_global\napi_key: test#helper-key # comment\nother: ignored\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertEqual((key, source), ("test#helper-key", "coding_helper_config"))

    def test_global_plan_name_is_supported_but_ambiguous_scalars_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: glm_coding_plan_global\napi_key: test-helper-key\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
            self.assertEqual((key, source), ("test-helper-key", "coding_helper_config"))
            config.write_text("plan: glm_coding_plan_global\napi_key: one\napi_key: two\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertEqual((key, source), (None, None))

    def test_direct_endpoint_rejects_transport_and_url_ambiguity(self):
        self.assertTrue(_apibackend.is_zai_coding_plan_endpoint(
            "https://api.z.ai/api/coding/paas/v4"))
        for endpoint in (
            "http://api.z.ai/api/coding/paas/v4",
            "https://api.z.ai/api/coding/paas/v4?route=other",
            "https://api.z.ai/api/coding/paas/v4#other",
            "https://api.z.ai/api/coding/paas/v4/other",
        ):
            self.assertFalse(_apibackend.is_zai_coding_plan_endpoint(endpoint))

    def test_direct_zai_endpoint_cannot_be_rewritten_as_payg(self):
        with self.assertRaisesRegex(ValueError, "no reviewed automatic PAYG"):
            _apibackend.coding_to_payg_base_url(
                "https://api.z.ai/api/coding/paas/v4")

    def test_non_coding_helper_is_not_repurposed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: general\napi_key: test-helper-key\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertIsNone(key)
        self.assertEqual(source, "helper_not_coding_plan")

    def test_missing_or_generic_helper_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("api_key: fixture\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(
                environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertEqual((key, source), (None, "helper_not_coding_plan"))

    def test_china_helper_plan_is_refused_without_a_reviewed_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: glm_coding_plan_china\napi_key: test-helper-key\n", encoding="utf-8")
            key, source = resolve_zai_coding_api_key(environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertEqual((key, source), (None, "helper_not_coding_plan"))

    def test_helper_path_descriptor_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: glm_coding_plan_global\napi_key: fixture\n", encoding="utf-8")
            real = config.stat()
            changed = SimpleNamespace(
                st_mode=real.st_mode, st_size=real.st_size + 1,
                st_dev=real.st_dev, st_ino=real.st_ino)
            with mock.patch("_zai_coding_plan.os.fstat", return_value=changed):
                key, source = resolve_zai_coding_api_key(
                    environ={"ZAI_CODING_HELPER_CONFIG": str(config)})
        self.assertEqual((key, source), (None, None))

    def test_helper_credential_rotation_changes_identity_and_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("plan: glm_coding_plan_global\napi_key: fixture-one\n", encoding="utf-8")
            definition = SimpleNamespace(fm={"provider": "zai-coding-plan"})
            with mock.patch.dict(os.environ, {"ZAI_CODING_HELPER_CONFIG": str(config)}, clear=False):
                os.environ.pop("ZAI_CODING_API_KEY", None)
                first = _executor._endpoint_state(directory, directory, "zai", definition)
                config.write_text("plan: glm_coding_plan_global\napi_key: fixture-two\n", encoding="utf-8")
                second = _executor._endpoint_state(directory, directory, "zai", definition)
                self.assertNotEqual(first[0], second[0])
                invocation = AgentInvocation(
                    cli="openai-compat", prompt="p", cwd=directory, permission="read-only",
                    model="glm-5.3-flash", base_url=first[2][0], api_key_env=first[2][1],
                    api_key_fingerprint=first[2][2])
                with mock.patch("_apibackend._do_request") as request:
                    response = _apibackend.call(invocation, 1000)
        self.assertEqual(response["status"], "error")
        self.assertIn("credential changed", response["error"])
        self.assertEqual(request.call_count, 0)


if __name__ == "__main__":
    unittest.main()
