#!/usr/bin/env python3
"""Focused OpenCode backend tests (no provider call required)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _builder import (AgentInvocation, build_invocation_args,
                      model_backend_compatibility, opencode_env_override,
                      opencode_router_env_override, parse_openrouter_options,
                      read_allowlist)
import _builder
import _doctor
import _executor
from _resolver import _opencode_live_models
from _stream import StreamProcessor


class OpenCodeBuilderTests(unittest.TestCase):
    def _inv(self, permission="read-only", model="openrouter/stealth/ox-alpha", **kw):
        return AgentInvocation(
            cli="opencode", prompt="Return OK", cwd=tempfile.gettempdir(),
            system_context="context", permission=permission, model=model, **kw)

    def test_read_only_uses_deny_by_default_policy(self):
        command, args, env = build_invocation_args(self._inv())
        self.assertEqual(command, "opencode")
        self.assertEqual(args[0:3], ["run", "--format", "json"])
        self.assertIn("--model", args)
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(policy["*"], "deny")
        self.assertEqual(policy["read"], "allow")
        self.assertNotIn("edit", policy)
        self.assertEqual(list(policy)[:2], ["*", "external_directory"])
        self.assertGreater(list(policy).index("read"), list(policy).index("*"))
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(env["OPENCODE_PURE"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_EXTERNAL_SKILLS"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
        self.assertIn("--pure", args)
        self.assertIn("--auto", args)
        self.assertEqual(args[-1].splitlines()[0], "[System Context]")

    def test_safe_edit_allows_edit_but_not_shell(self):
        _, args, env = build_invocation_args(self._inv(permission="safe-edit"))
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(policy["edit"], "allow")
        self.assertEqual(policy.get("bash", "deny"), "deny")
        self.assertIn("--auto", args)

    def test_yolo_enables_auto_and_allow_policy(self):
        _, args, env = build_invocation_args(
            self._inv(permission="yolo", isolated_lane=True,
                      allow_tool_credentials=True))
        self.assertIn("--auto", args)
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"]), {"*": "allow"})

    def test_yolo_requires_an_explicit_isolated_lane(self):
        with self.assertRaisesRegex(ValueError, "requires --worktree or --isolated-lane"):
            build_invocation_args(self._inv(permission="yolo"))

    def test_yolo_credential_bridge_requires_explicit_consent(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("_windows_credentials.resolve_openrouter_api_key",
                        return_value=("test-secret", "windows_credential")):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with self.assertRaisesRegex(ValueError, "allow-tool-credentials"):
                build_invocation_args(self._inv(permission="yolo", isolated_lane=True))

    def test_worktree_does_not_authorize_private_credential_bridge(self):
        """A worktree is mutation isolation, not the OS boundary a yolo key bridge needs."""
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}, clear=False):
            with self.assertRaisesRegex(ValueError, "--isolated-lane"):
                build_invocation_args(self._inv(
                    permission="yolo", worktree="ox-review",
                    allow_tool_credentials=True))

    def test_yolo_scrubs_ambient_inline_opencode_config(self):
        """Unacknowledged yolo turns cannot inherit a config that may contain a key."""
        with mock.patch.dict(os.environ, {
            "OPENCODE_CONFIG_CONTENT": '{"provider":{"other":{"options":'
                                          '{"apiKey":"test-secret"}}}}',
        }, clear=False), \
             mock.patch("_windows_credentials.resolve_openrouter_api_key",
                        return_value=(None, None)):
            os.environ.pop("OPENROUTER_API_KEY", None)
            env = opencode_env_override(
                "openrouter/stealth/ox-alpha", permission="yolo")
        self.assertIsNone(env["OPENCODE_CONFIG_CONTENT"])

    def test_yolo_router_overlay_does_not_inherit_ambient_inline_config(self):
        """An acknowledged router turn gets only its validated child overlay."""
        inv = self._inv(
            permission="yolo", isolated_lane=True, allow_tool_credentials=True,
            model="openrouter/openrouter/fusion",
            openrouter_options={
                "plugins": [{"id": "fusion", "preset": "general-fast"}],
            },
        )
        with mock.patch.dict(os.environ, {
            "OPENCODE_CONFIG_CONTENT": '{"provider":{"other":{"options":'
                                          '{"apiKey":"test-secret"}}}}',
            "OPENROUTER_API_KEY": "test-key",
        }, clear=False):
            _command, _args, env = build_invocation_args(inv)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("other", config.get("provider", {}))
        self.assertEqual(
            config["provider"]["openrouter"]["models"]["openrouter/fusion"]
            ["options"]["plugins"][0]["preset"], "general-fast")

    def test_yolo_credential_bridge_is_explicit_and_scrubs_other_provider_keys(self):
        with mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-secret",
            "ANTHROPIC_API_KEY": "other-secret",
        }, clear=False):
            _, _, env = build_invocation_args(
                self._inv(permission="yolo", isolated_lane=True,
                          allow_tool_credentials=True))
        self.assertEqual(env["OPENROUTER_API_KEY"], "test-secret")
        self.assertIsNone(env["ANTHROPIC_API_KEY"])

    def test_provider_inert_child_fixture_receives_policy_and_auto(self):
        """Exercise the real executor boundary without contacting OpenCode/provider."""
        invocation = self._inv(permission="safe-edit")
        code = (
            "import json,os,sys; "
            "p=json.loads(os.environ['OPENCODE_PERMISSION']); "
            "ok=(p.get('*')=='deny' and p.get('edit')=='allow' "
            "and p.get('bash','deny')=='deny' and '--auto' in sys.argv); "
            "events=[{'type':'step_start','part':{'modelID':'openrouter/stealth/ox-alpha'}}, "
            "{'type':'text','part':{'text':'fixture ok'}}, "
            "{'type':'step_finish','part':{'tokens':{'input':1,'output':1,'total':2},'cost':0}}]; "
            "[print(json.dumps(e)) for e in (events if ok else "
            "[{'type':'error','error':'policy mismatch'}])]"
        )

        def fake_launch(_command, args):
            return sys.executable, ("-c", code, *args)

        with mock.patch.object(_executor, "_resolve_launch", side_effect=fake_launch), \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["result"], "fixture ok")
        self.assertEqual(out["served_model_evidence"], "inferred")

    def test_zero_token_finish_without_total_is_reported_truthfully(self):
        """OpenCode's zero-token shape may omit the aggregate ``total`` field."""
        invocation = self._inv(permission="read-only")
        events = [
            {"type": "step_start", "sessionID": "s"},
            {"type": "step_finish", "sessionID": "s", "part": {
                "reason": "stop",
                "tokens": {"input": 0, "output": 0, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}},
            }},
        ]
        code = "import json; " + "; ".join(
            f"print({json.dumps(json.dumps(event))})" for event in events)
        with mock.patch.object(_executor, "_resolve_launch",
                               return_value=(sys.executable,
                                             ("-c", code))), \
             mock.patch("_receipt.workspace_snapshot",
                        return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error_kind"], "empty_terminal_result")
        self.assertTrue(out["opencode_stream"]["zero_output_finish"])
        self.assertTrue(out["opencode_stream"]["zero_token_finish"])

    def test_bare_endorse_with_inferred_model_is_not_authoritative_review(self):
        """A bare decision word must stay suspect when the report contract is absent.

        This mirrors the observed Ox/OpenCode shape: the child emits a normal
        step lifecycle and useful text (``ENDORSE``), but no STATUS/SUMMARY/
        FOLLOW-UP/HANDOFF block and no provider-authored served-model receipt.
        The result may be advisory, never a machine-verifiable endorsement.
        """
        invocation = self._inv(permission="safe-edit")
        events = [
            {"type": "step_start", "sessionID": "s",
             "part": {"modelID": "openrouter/stealth/ox-alpha"}},
            {"type": "text", "sessionID": "s",
             "part": {"type": "text", "text": "ENDORSE"}},
            {"type": "step_finish", "sessionID": "s",
             "part": {"reason": "stop", "tokens": {
                 "input": 3, "output": 2, "total": 5, "reasoning": 0},
                 "cost": 0}},
        ]
        code = "import json; " + "; ".join(
            f"print({json.dumps(json.dumps(event))})" for event in events)
        with mock.patch.object(_executor, "_resolve_launch",
                               return_value=(sys.executable,
                                             ("-c", code))), \
             mock.patch("_receipt.workspace_snapshot",
                        return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["execution_status"], "success")
        self.assertEqual(out["result"], "ENDORSE")
        self.assertIsNone(out["report"])
        self.assertFalse(out["report_ok"])
        self.assertTrue(out["suspect"])
        self.assertIsNone(out["verdict"])
        self.assertFalse(_executor.is_terminal_success(out))
        self.assertEqual(out["opencode_stream"]["completion_evidence"],
                         "step_finish")
        self.assertEqual(out["served_model_evidence"], "inferred")
        self.assertEqual(out["model"]["served"],
                         "openrouter/stealth/ox-alpha")

    def test_restricted_windows_external_cwd_refuses_before_dispatch(self):
        with mock.patch.object(_builder.os, "name", "nt"), \
             mock.patch.object(_builder.tempfile, "gettempdir", return_value=r"C:\\Temp"):
            denied = read_allowlist("opencode", "safe-edit", r"I:\\review", ())
            denied_with_root = read_allowlist(
                "opencode", "read-only", r"I:\\review", (r"C:\\review-packet",))
            allowed = read_allowlist("opencode", "safe-edit", r"C:\\review", ())
        self.assertTrue(denied["would_refuse"])
        self.assertFalse(denied["enforced"])
        self.assertEqual(denied["error_kind"], "opencode_external_cwd_denied")
        self.assertEqual(denied["allowed_root"], r"C:\Temp\opencode")
        self.assertTrue(denied["requires_packet_refreeze"])
        self.assertEqual(denied["reroute"]["action"], "copy_sanitized_packet_and_refreeze")
        self.assertEqual(denied_with_root["error_kind"], "opencode_external_cwd_denied")
        self.assertFalse(denied_with_root["enforced"])
        self.assertFalse(allowed["would_refuse"])
        self.assertTrue(allowed["enforced"])

    def test_doctor_surfaces_restricted_windows_cwd_before_a_turn(self):
        with mock.patch.object(_builder.os, "name", "nt"), \
             mock.patch.object(_builder.tempfile, "gettempdir", return_value=r"C:\\Temp"):
            policy = _doctor._opencode_cwd_policy(r"I:\\review")
        self.assertTrue(policy["checked"])
        self.assertFalse(policy["allowed"])
        self.assertEqual(policy["error_kind"], "opencode_external_cwd_denied")
        self.assertEqual(policy["reroute"]["action"], "copy_sanitized_packet_and_refreeze")

    def test_openrouter_credential_bridge_is_child_only(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("_windows_credentials.resolve_openrouter_api_key",
                        return_value=("test-secret", "windows_credential")):
            os.environ.pop("OPENROUTER_API_KEY", None)
            env = opencode_env_override("openrouter/stealth/ox-alpha")
        self.assertEqual(env, {"OPENROUTER_API_KEY": "test-secret"})
        # The helper returns a launch delta; it never mutates the parent env.
        self.assertNotEqual(os.environ.get("OPENROUTER_API_KEY"), "test-secret")

    def test_openrouter_credential_bridge_is_model_scoped(self):
        with mock.patch("_windows_credentials.resolve_openrouter_api_key",
                        return_value=("test-secret", "windows_credential")):
            self.assertEqual(opencode_env_override("coding-plan/deepseek-v4-pro"), {})

    @unittest.skipUnless(os.name == "nt", "Windows launcher regression")
    def test_windows_launch_prefers_npm_bundled_cli_over_desktop_launcher(self):
        desktop_launcher = r"C:\OpenCode\opencode.exe"
        npm_shim = r"C:\npm\opencode.cmd"
        npm_cli = r"C:\npm\node_modules\opencode-ai\bin\opencode.exe"
        with mock.patch.object(_executor.os, "name", "nt"), \
             mock.patch.object(_executor.shutil, "which",
                               side_effect=lambda name: desktop_launcher
                               if name.lower() == "opencode.exe" else
                               npm_shim if name.lower() == "opencode.cmd" else None), \
             mock.patch.object(_executor.os.path, "isfile",
                               side_effect=lambda p: p == npm_cli):
            command, args = _executor._resolve_launch("opencode", ["run", "--format", "json"])
        self.assertEqual(command, npm_cli)
        self.assertEqual(args[:2], ["run", "--format"])

    def test_windows_launch_refuses_unresolved_command_shim(self):
        npm_shim = r"C:\npm\opencode.cmd"
        with mock.patch.object(_executor.os, "name", "nt"), \
             mock.patch.object(_executor.shutil, "which",
                               side_effect=lambda name: npm_shim
                               if name.lower() in {"opencode", "opencode.cmd"}
                               else None), \
             mock.patch.object(_executor.os.path, "isfile", return_value=False):
            with self.assertRaisesRegex(ValueError, "native executable"):
                _executor._resolve_launch(
                    "opencode", ["run", "prompt & must remain data"])

    def test_windows_launcher_refusal_is_zero_contact_envelope(self):
        npm_shim = r"C:\npm\opencode.cmd"
        invocation = AgentInvocation(
            cli="opencode", prompt="provider-inert", cwd=tempfile.gettempdir(),
            model="custom-model", permission="yolo")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=("opencode", ["run"], None)), \
             mock.patch.object(_executor.os, "name", "nt"), \
             mock.patch.object(_executor.shutil, "which",
                               side_effect=lambda name: npm_shim
                               if name.lower() in {"opencode", "opencode.cmd"}
                               else None), \
             mock.patch.object(_executor.os.path, "isfile", return_value=False):
            response = _executor.execute_agent(invocation, timeout_ms=1000)
        self.assertEqual(response["error_kind"], "unsafe_windows_launcher")
        self.assertEqual(response["attempts"], 0)
        self.assertEqual(response["attempt_status"], "not_run")
        self.assertEqual(response["execution_status"], "not_run")
        self.assertFalse(response["provider_contacted"])

    def test_fusion_plugin_is_forwarded_through_child_config(self):
        inv = self._inv(
            model="openrouter/openrouter/fusion",
            openrouter_options={
                "plugins": [{"id": "fusion", "preset": "general-budget"}]
            },
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
            env = opencode_router_env_override(inv)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = config["provider"]["openrouter"]
        self.assertEqual(provider["npm"], "@openrouter/ai-sdk-provider")
        self.assertEqual(
            provider["models"]["openrouter/fusion"]["options"]["plugins"][0],
            {"id": "fusion", "preset": "general-budget"},
        )

    def test_auto_router_settings_are_bounded(self):
        parsed = parse_openrouter_options(
            '{"plugins":[{"id":"auto-router","cost_tier":"max",'
            '"allowed_models":["anthropic/*"]}]}',
            "openrouter/openrouter/auto",
        )
        self.assertEqual(parsed["plugins"][0]["cost_tier"], "max")
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            parse_openrouter_options(
                '{"extraBody":{"messages":[]}}', "openrouter/openrouter/auto")

    def test_router_options_reject_wrong_alias_and_non_openrouter_model(self):
        with self.assertRaisesRegex(ValueError, "require model selector"):
            parse_openrouter_options(
                '{"plugins":[{"id":"auto-router","cost_tier":"max"}]}',
                "openrouter/stealth/ox-alpha",
            )
        with self.assertRaisesRegex(ValueError, "requires an OpenRouter model selector"):
            parse_openrouter_options(
                '{"plugins":[{"id":"fusion","preset":"general-high"}]}',
                "coding-plan/deepseek-v4-pro",
            )

    def test_router_overlay_merges_existing_inline_config(self):
        inv = self._inv(
            model="openrouter/openrouter/fusion",
            openrouter_options={"plugins": [{"id": "fusion", "preset": "general-fast"}]},
        )
        existing = {"model": "coding-plan/deepseek-v4-flash", "permission": {"*": "deny"}}
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}, clear=False):
            env = opencode_router_env_override(inv)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], existing["model"])
        self.assertEqual(config["permission"], existing["permission"])
        self.assertEqual(
            config["provider"]["openrouter"]["models"]["openrouter/fusion"]
            ["options"]["plugins"][0]["preset"],
            "general-fast",
        )

    def test_agent_passthrough_cannot_override_model_or_directory(self):
        inv = self._inv(extra_args=("--model", "coding-plan/deepseek-v4-pro",
                                    "--dir", "C:/elsewhere", "--format", "default",
                                    "--variant", "low", "--foo", "bar"))
        _, args, _ = build_invocation_args(inv)
        self.assertEqual(args.count("--model"), 1)
        self.assertIn("openrouter/stealth/ox-alpha", args)
        self.assertNotIn("C:/elsewhere", args)
        self.assertNotIn("default", args)
        self.assertNotIn("low", args)
        self.assertIn("--foo", args)

    def test_open_models_are_compatible_with_known_vendor_namespaces(self):
        self.assertIsNone(model_backend_compatibility("opencode", "deepseek-v4-pro"))
        self.assertIsNone(model_backend_compatibility("opencode", "claude-opus-5"))


class OpenCodeStreamTests(unittest.TestCase):
    def test_event_stream_finishes_at_eof_and_preserves_telemetry(self):
        p = StreamProcessor()
        lines = [
            {"type": "step_start", "sessionID": "s", "part":
             {"modelID": "openrouter/stealth/ox-alpha"}},
            {"type": "text", "sessionID": "s", "part": {"text": "OK"}},
            {"type": "step_finish", "sessionID": "s", "part":
             {"tokens": {"input": 1, "output": 2, "total": 3}, "cost": 0}},
        ]
        for item in lines:
            self.assertFalse(p.process_line(json.dumps(item)))
        p.finalize_stream()
        self.assertEqual(p.get_result()["status"], "success")
        self.assertEqual(p.get_result()["result"], "OK")
        self.assertEqual(p.session_id, "s")
        self.assertIsNone(p.model)
        self.assertEqual(p.handshake_model, "openrouter/stealth/ox-alpha")
        self.assertEqual(p.usage["output_tokens"], 2)

    def test_dotted_message_part_events_unwrap_properties_and_data(self):
        """Parse the event envelope persisted by current OpenCode releases.

        The real event stream uses ``message.part.updated.<revision>`` with
        either a ``properties`` or ``data`` payload.  Its part type has also
        appeared as both ``step-start`` and ``step_finish``.  ModelID comes
        from the message metadata and remains a handshake target, never
        served-model evidence.
        """
        lines = [
            {"type": "message.updated.1", "data": {
                "sessionID": "s-real-shape",
                "info": {"role": "assistant", "modelID": "nous/stealth/ox-alpha"},
            }},
            {"type": "message.part.updated.1", "properties": {
                "sessionID": "s-real-shape",
                "part": {"id": "p1", "messageID": "m1",
                         "sessionID": "s-real-shape", "type": "step-start"},
            }},
            {"type": "message.part.updated.1", "properties": {
                "sessionID": "s-real-shape",
                "part": {"id": "p2", "messageID": "m1",
                         "sessionID": "s-real-shape", "type": "text",
                         "text": "OK"},
            }},
            {"type": "message.part.updated.1", "data": {
                "sessionID": "s-real-shape",
                "part": {"id": "p3", "messageID": "m1",
                         "sessionID": "s-real-shape", "type": "step_finish",
                         "reason": "stop", "tokens": {
                             "input": 1, "output": 2, "total": 3,
                             "reasoning": 0}, "cost": 0},
            }},
        ]
        p = StreamProcessor()
        for item in lines:
            self.assertFalse(p.process_line(json.dumps(item)))
        p.finalize_stream()
        result = p.get_result()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"], "OK")
        self.assertTrue(p.is_opencode)
        self.assertEqual(p.opencode_event_count, 4)
        self.assertTrue(p.opencode_step_finish_seen)
        self.assertEqual(p.opencode_finish_reason, "stop")
        self.assertEqual(p.handshake_model, "nous/stealth/ox-alpha")
        self.assertIsNone(p.model)
        self.assertEqual(p.usage["output_tokens"], 2)

    def test_error_event_is_terminal(self):
        p = StreamProcessor()
        self.assertTrue(p.process_line(json.dumps({
            "type": "error", "sessionID": "s", "error": "provider unavailable"})))
        self.assertEqual(p.get_result()["status"], "error")

    def test_typeless_opencode_progress_is_not_cursor_terminal(self):
        p = StreamProcessor()
        self.assertFalse(p.process_line(json.dumps({
            "sessionID": "s", "part": {"type": "text", "text": "progress"}})))
        self.assertIsNone(p.get_result())
        p.finalize_stream()
        self.assertEqual(p.get_result()["result"], "progress")
        self.assertFalse(p.opencode_step_finish_seen)

    def test_clean_eof_without_step_finish_is_suspect(self):
        p = StreamProcessor()
        lines = [
            {"type": "step_start", "sessionID": "s", "part": {"type": "step-start"}},
            {"type": "text", "sessionID": "s", "part":
             {"type": "text", "text": "I'll start by listing the directory contents."}},
        ]
        for item in lines:
            self.assertFalse(p.process_line(json.dumps(item)))
        p.finalize_stream()
        base = _executor.build_final_response(
            "opencode", 0, p.get_result(), [json.dumps(item) for item in lines], "")
        out = _executor._enrich(base, p)
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["suspect"])
        self.assertEqual(out["opencode_stream"]["completion_evidence"],
                         "clean_eof_without_step_finish")
        self.assertTrue(any("step_finish" in w for w in out["warnings"]))

    def test_unknown_zero_token_step_finish_is_diagnostic_error(self):
        p = StreamProcessor()
        lines = [
            {"type": "step_start", "sessionID": "s", "part":
             {"modelID": "openrouter/stealth/ox-alpha"}},
            {"type": "step_finish", "sessionID": "s", "part": {
                "reason": "unknown",
                "tokens": {"input": 0, "output": 0, "total": 0, "reasoning": 0},
                "cost": 0,
            }},
        ]
        for item in lines:
            self.assertFalse(p.process_line(json.dumps(item)))
        p.finalize_stream()
        base = _executor.build_final_response(
            "opencode", 0, p.get_result(), [json.dumps(item) for item in lines], "")
        out = _executor._enrich(base, p)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error_kind"], "empty_terminal_result")
        self.assertEqual(out["opencode_diagnostic"], "unknown_finish_zero_tokens")
        self.assertEqual(out["opencode_stream"]["finish_reason"], "unknown")
        self.assertTrue(out["opencode_stream"]["zero_output_finish"])
        self.assertTrue(out["opencode_stream"]["zero_token_finish"])
        self.assertIn("provider/model no-output", out["error"])

    def test_step_start_model_is_not_served_evidence_on_empty_finish(self):
        import tempfile
        from _builder import AgentInvocation
        lines = [
            {"type": "step_start", "sessionID": "s", "part":
             {"modelID": "openrouter/stealth/ox-alpha"}},
            {"type": "step_finish", "sessionID": "s", "part": {
                "reason": "unknown",
                "tokens": {"input": 0, "output": 0, "total": 0, "reasoning": 0},
            }},
        ]
        code = "import json; " + "; ".join(
            f"print({json.dumps(json.dumps(line))})" for line in lines)
        invocation = AgentInvocation(
            cli="opencode", model="openrouter/stealth/ox-alpha", prompt="review",
            cwd=tempfile.gettempdir(), permission="read-only")
        with mock.patch.object(_executor, "build_invocation_args",
                               return_value=(sys.executable, ("-c", code), None)), \
             mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
             mock.patch("_receipt.workspace_evidence", return_value={}):
            out = _executor.execute_agent(invocation, timeout_ms=5000)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["served_model_evidence"], "absent")
        self.assertIsNone(out["model"]["served"])
        self.assertEqual(out["model"]["targeted"], "openrouter/stealth/ox-alpha")


class OpenCodeDiscoveryTests(unittest.TestCase):
    def test_live_models_parse_provider_model_lines(self):
        fake = mock.Mock(returncode=0, stdout="openrouter/stealth/ox-alpha\nnot-a-model\n",
                         stderr="")
        with mock.patch("_resolver.shutil.which", return_value="opencode"), \
             mock.patch("_resolver.subprocess.run", return_value=fake):
            source, models, note = _opencode_live_models()
        self.assertEqual(source, "live")
        self.assertEqual(models, ["openrouter/stealth/ox-alpha"])
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
