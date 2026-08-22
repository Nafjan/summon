#!/usr/bin/env python3
"""Focused OpenCode backend tests (no provider call required)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _builder import (AgentInvocation, build_invocation_args,
                      model_backend_compatibility, opencode_env_override,
                      opencode_router_env_override, parse_openrouter_options)
from _resolver import _opencode_live_models
from _stream import StreamProcessor


class OpenCodeBuilderTests(unittest.TestCase):
    def _inv(self, permission="read-only", model="openrouter/stealth/ox-alpha", **kw):
        return AgentInvocation(
            cli="opencode", prompt="Return OK", cwd=os.getcwd(),
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
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(env["OPENCODE_PURE"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_EXTERNAL_SKILLS"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
        self.assertEqual(args[-1].splitlines()[0], "[System Context]")

    def test_safe_edit_allows_edit_but_not_shell(self):
        _, args, env = build_invocation_args(self._inv(permission="safe-edit"))
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(policy["edit"], "allow")
        self.assertEqual(policy.get("bash", "deny"), "deny")
        self.assertNotIn("--auto", args)

    def test_yolo_enables_auto_and_allow_policy(self):
        _, args, env = build_invocation_args(self._inv(permission="yolo"))
        self.assertIn("--auto", args)
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"]), {"*": "allow"})

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
        self.assertEqual(p.model, "openrouter/stealth/ox-alpha")
        self.assertEqual(p.usage["output_tokens"], 2)

    def test_error_event_is_terminal(self):
        p = StreamProcessor()
        self.assertTrue(p.process_line(json.dumps({
            "type": "error", "sessionID": "s", "error": "provider unavailable"})))
        self.assertEqual(p.get_result()["status"], "error")


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
