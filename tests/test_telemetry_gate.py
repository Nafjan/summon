from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("summon_telemetry_gate", ROOT / "skills" / "summon" / "scripts" / "_telemetry.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TelemetryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.config = base / "config.json"
        self.events = base / "events.jsonl"
        self.reports = base / "reports"
        self.env = {
            "SUMMON_TELEMETRY_CONFIG": str(self.config),
            "SUMMON_TELEMETRY_FILE": str(self.events),
            "SUMMON_REPORTS_DIR": str(self.reports),
        }
        self.old = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        os.environ.pop("SUMMON_TELEMETRY", None)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_clean_home_is_disabled_and_env_override_wins(self):
        self.assertFalse(MODULE.enabled())
        os.environ["SUMMON_TELEMETRY"] = "1"
        self.assertTrue(MODULE.enabled())
        os.environ["SUMMON_TELEMETRY"] = "0"
        self.assertFalse(MODULE.enabled())

    def test_record_is_bounded_and_redacts_secrets_paths_and_prompt(self):
        os.environ["SUMMON_TELEMETRY"] = "1"
        envelope = {
            "status": "error", "cli": "claude", "permission": "safe-edit",
            "error": "Bearer sk-super-secret /tmp/private/repo prompt=do not publish",
            "model": {"requested": "claude-opus-5", "served": "claude-opus-5"},
            "prompt_sha256": "a" * 64,
            "summon": {"version": "3.0.0", "scripts_sha256": "b" * 64},
            "workspace_evidence": {"coverage": "full"},
        }
        event = MODULE.record(envelope)
        self.assertIsInstance(event, dict)
        raw = self.events.read_text(encoding="utf-8")
        self.assertLessEqual(self.events.stat().st_size, MODULE._MAX_EVENT_FILE_BYTES)
        self.assertNotIn("sk-super-secret", raw)
        self.assertNotIn("/tmp/private/repo", raw)
        self.assertNotIn("do not publish", raw)
        json.loads(raw)

    def test_clear_does_not_change_opt_in(self):
        MODULE.set_enabled(True)
        self.assertTrue(MODULE.enabled())
        MODULE.record({"status": "success", "cli": "claude"})
        MODULE.clear_events()
        self.assertTrue(MODULE.enabled())

    def test_opt_in_salt_is_local_and_clear_rotates_it(self):
        MODULE.set_enabled(True)
        first = json.loads(self.config.read_text(encoding="utf-8"))["salt"]
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        MODULE.record({"status": "error", "error": "first"})
        MODULE.clear_events()
        second = json.loads(self.config.read_text(encoding="utf-8"))["salt"]
        self.assertRegex(second, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        self.assertNotIn(first, self.events.read_text(encoding="utf-8") if self.events.exists() else "")

    def test_schema_two_terminal_has_required_nulls_and_bounded_projection(self):
        os.environ["SUMMON_TELEMETRY"] = "1"
        event = MODULE.record({
            "status": "not-a-status", "cli": "codex",
            "timeout": {"stage": "backend-execution"},
            "auth_outcome": "token=https://private.invalid",
            "model": {"requested": "gpt-5.6-sol", "served": "gpt-5.6-luna"},
        })
        self.assertEqual(event["schema"], MODULE.EVENT_SCHEMA_VERSION)
        self.assertEqual(event["event_kind"], "operation_terminal")
        self.assertEqual(event["terminal_kind"], "operation")
        self.assertEqual(event["operation"], "dispatch")
        self.assertEqual(event["cohort"], "test")
        self.assertEqual(event["validation_outcome"], "invalid_status")
        self.assertEqual(event["status"], "unknown")
        self.assertEqual(event["timeout_stage"], "backend_execution")
        self.assertIsNone(event["model_mismatch"])
        self.assertEqual(event["served_model_evidence"], "absent")
        self.assertIsNone(event["auth_outcome"])
        for key in ("attempt_id", "provider_turn_id", "report_ok", "result_usable",
                    "provider_contacted", "cohort_provenance_digest"):
            self.assertIn(key, event)
            self.assertIsNone(event[key])

    def test_operation_context_is_opaque_and_closes_after_one_terminal(self):
        parent = "a" * 32
        context = MODULE.new_operation_context("manifest", parent_operation_id=parent,
                                               cohort="production_like")
        first = MODULE.event_from_envelope({"status": "success"}, operation_context=context)
        second = MODULE.event_from_envelope({"status": "success"}, operation_context=context)
        self.assertEqual(first["operation"], "manifest")
        self.assertEqual(first["cohort"], "test")
        self.assertEqual(first["parent_operation_id"], parent)
        self.assertRegex(first["operation_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["event_sequence"], 0)
        self.assertIsNone(second)

    def test_resume_preflight_failure_is_labeled_resume(self):
        """An early single-dispatch refusal must retain the caller's resume lane."""
        env = os.environ.copy()
        env["SUMMON_TELEMETRY"] = "1"
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills" / "summon" / "scripts" / "run_subagent.py"),
             "--resume", "session-1", "--worktree", "isolated",
             "--agent", "unused", "--prompt", "test", "--cwd", str(ROOT)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        records = [json.loads(line) for line in self.events.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        self.assertTrue(records)
        self.assertEqual(records[-1]["operation"], "resume")

    def test_invalid_terminal_kind_is_rejected_and_trusted_evidence_is_required(self):
        self.assertIsNone(MODULE.event_from_envelope(
            {"status": "success"}, terminal_kind="provider_turn"))
        untrusted = MODULE.event_from_envelope({
            "status": "success", "model": {"served": "gpt-5.6-sol"},
            "served_model_evidence": "reported", "model_mismatch": True,
        })
        self.assertEqual(untrusted["served_model_evidence"], "absent")
        self.assertIsNone(untrusted["model_mismatch"])
        trusted = MODULE.event_from_envelope({
            "status": "success", "model": {"served": "gpt-5.6-sol"},
            "_telemetry_model_evidence": MODULE._trusted_model_evidence(
                "reported", False, _capability=MODULE._EVIDENCE_CAPABILITY),
        })
        self.assertEqual(trusted["served_model_evidence"], "reported")
        self.assertFalse(trusted["model_mismatch"])

    def test_auth_lifecycle_requires_the_executor_marker(self):
        direct = MODULE.event_from_envelope({
            "status": "error", "auth_stage": "terminal", "auth_outcome": "recovered",
            "interactive_required": False, "remediation_code": "none",
        })
        self.assertEqual(direct["validation_outcome"], "invalid_auth")
        self.assertEqual(direct["auth_lifecycle_evidence"], "absent")
        trusted = MODULE.event_from_envelope({
            "status": "error", "_telemetry_auth_lifecycle": MODULE._trusted_auth_lifecycle(
                "terminal", "login_required", True, "provider_login_required",
                _capability=MODULE._AUTH_CAPABILITY),
            "auth_stage": "terminal", "auth_outcome": "login_required",
            "interactive_required": True, "remediation_code": "provider_login_required",
        })
        self.assertEqual(trusted["validation_outcome"], "valid")
        self.assertEqual(trusted["auth_lifecycle_evidence"], "reported")

    def test_envelope_cannot_spoof_operation_cohort_or_raw_model_metadata(self):
        event = MODULE.event_from_envelope({
            "status": "success", "operation": "council",
            "telemetry_operation": "manifest", "model": {"served": "https://private.invalid/x"},
            "provider_adapter_revision": "https://private.invalid/revision",
        }, cohort="production_like")
        self.assertEqual(event["operation"], "dispatch")
        self.assertEqual(event["cohort"], "test")
        self.assertIsNone(event["model_served"])
        self.assertIsNone(event["provider_adapter_revision"])

    def test_mutated_context_cannot_promote_cohort_or_identity(self):
        context = MODULE.new_operation_context("dispatch")
        with self.assertRaises(AttributeError):
            context._cohort = "production_like"
        with self.assertRaises(AttributeError):
            context._operation_id = "not-an-opaque-id"
        event = MODULE.event_from_envelope({"status": "success"}, operation_context=context)
        self.assertEqual(event["cohort"], "test")
        self.assertEqual(event["validation_outcome"], "valid")
        self.assertRegex(event["operation_id"], r"^[0-9a-f]{32}$")

    def test_invalid_terminal_does_not_close_context(self):
        context = MODULE.new_operation_context("dispatch")
        invalid = MODULE.event_from_envelope({"status": ["forged"]}, operation_context=context)
        accepted = MODULE.event_from_envelope({"status": "success"}, operation_context=context)
        self.assertEqual(invalid["validation_outcome"], "invalid_status")
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["validation_outcome"], "valid")

    def test_closed_context_rejects_late_invalid_projection(self):
        context = MODULE.new_operation_context("dispatch")
        self.assertIsNotNone(MODULE.event_from_envelope(
            {"status": "success"}, operation_context=context))
        self.assertIsNone(MODULE.event_from_envelope(
            {"status": ["late-forged"]}, operation_context=context))

    def test_malformed_nested_spool_and_report_inputs_fail_soft(self):
        nested = ("[" * 2000) + "0" + ("]" * 2000)
        self.assertEqual(MODULE._parse_event_lines(nested.encode("ascii")), [])
        report = self.reports / "invalid-utf8.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"\xff\xfe")
        with self.assertRaises(ValueError):
            MODULE.validate_report_file(str(report))

    def test_public_sanitizer_rejects_forged_schema_two_shape_without_crashing(self):
        native = MODULE.event_from_envelope({"status": "success"})
        for key, value in (
            ("event_kind", ["operation_terminal"]),
            ("terminal_kind", "provider_turn"),
            ("summon_version", "999.0.0"),
            ("timeout_stage", {"nested": True}),
            ("status", ["success"]),
            ("artifacts", {"count": [1]}),
        ):
            forged = dict(native)
            forged[key] = value
            self.assertIsNone(MODULE._sanitize_event(forged), key)

    def test_reviewed_report_validator_requires_generated_shape_and_bounds(self):
        text, _ = MODULE.make_report({"status": "success", "cli": "codex"})
        report = self.reports / "reviewed.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
        self.assertTrue(MODULE.validate_report_file(str(report))["ok"])
        legacy_text, _ = MODULE.make_report({"schema": 1, "event_id": "a" * 32,
                                             "recorded_at": "now", "status": "error",
                                             "backend": "codex"})
        report.write_text(legacy_text, encoding="utf-8")
        self.assertTrue(MODULE.validate_report_file(str(report))["ok"])

        marker = "```json\n"
        before, rest = text.split(marker, 1)
        payload, after = rest.split("\n```", 1)
        base = json.loads(payload)
        for key, value in (
            ("event_kind", "forged-secret"),
            ("terminal_kind", "provider_turn"),
            ("summon_version", "999.0.0"),
            ("timeout_stage", {"private": "value"}),
            ("execution_status", []),
            ("status", {}),
            ("warning_count", 999),
            ("artifacts", {"count": [1]}),
        ):
            forged = dict(base)
            forged[key] = value
            report.write_text(before + marker + json.dumps(forged, sort_keys=True, indent=2)
                              + "\n```" + after, encoding="utf-8")
            with self.assertRaises(ValueError, msg=key):
                MODULE.validate_report_file(str(report))

        report.write_text(before + marker + '{"source_trust": "unverified"}'
                          + "\n```" + after, encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.validate_report_file(str(report))

    def test_schema_one_event_is_imported_as_legacy_unknown(self):
        legacy = {
            "schema": 1, "event_id": "b" * 32,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "status": "success", "backend": "claude", "transport": "subprocess",
            "model_requested": "opus", "model_served": "claude-opus-5",
        }
        imported = MODULE._sanitize_event(legacy)
        self.assertIsNotNone(imported)
        self.assertEqual(imported["schema"], MODULE.LEGACY_EVENT_SCHEMA_VERSION)
        self.assertEqual(imported["cohort"], "legacy_unknown")
        self.assertEqual(imported["source_trust"], "unverified")
        self.assertIsNone(imported["served_model_evidence"])
        for key in ("event_kind", "operation", "operation_id", "parent_operation_id",
                    "attempt_id", "provider_turn_id", "cohort_provenance_digest"):
            self.assertIsNone(imported[key])

    def test_config_schema_stays_one_and_public_view_strips_local_bindings(self):
        status = MODULE.set_enabled(True)
        self.assertEqual(status["schema"], MODULE.CONFIG_SCHEMA_VERSION)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["schema"],
                         MODULE.CONFIG_SCHEMA_VERSION)
        native = MODULE.event_from_envelope({
            "status": "success", "provider_adapter_revision": "adapter.1",
            "evidence_registry_revision": "registry.1",
        })
        public = MODULE._sanitize_event(native)
        self.assertIsNotNone(public)
        for key in ("event_id", "operation_id", "parent_operation_id", "attempt_id",
                    "provider_turn_id", "event_sequence", "cohort_provenance_digest",
                    "provider_adapter_revision", "evidence_registry_revision",
                    "error_sha256", "prompt_sha256", "scripts_sha256", "platform",
                    "workspace"):
            self.assertIsNone(public[key])

    def test_raw_envelope_report_uses_the_same_public_sanitizer(self):
        source = {
            "status": "error", "cli": "tenant/customer-private",
            "model": {"requested": "alice-private-tenant-123",
                       "served": "alice-private-tenant-123"},
            "error": "private prompt and result should not appear",
        }
        text, meta = MODULE.make_report(source)
        self.assertIsNone(meta["event"].get("model_requested"))
        self.assertNotIn("alice-private-tenant-123", text)
        self.assertNotIn("tenant/customer-private", text)
        self.assertNotIn("private prompt", text)

    def test_local_spool_rejects_private_provider_metadata(self):
        os.environ["SUMMON_TELEMETRY"] = "1"
        event = MODULE.record({
            "status": "error", "cli": "tenant/customer-private",
            "permission": "acct_987", "billing": {"source": "acct_987"},
            "model": {"requested": "alice-private-tenant-123",
                       "served": "alice-private-tenant-123"},
        })
        self.assertIsNone(event["backend"])
        self.assertIsNone(event["permission"])
        self.assertIsNone(event["billing_source"])
        self.assertIsNone(event["model_requested"])
        raw = self.events.read_text(encoding="utf-8")
        for value in ("tenant/customer-private", "acct_987", "alice-private-tenant-123"):
            self.assertNotIn(value, raw)


if __name__ == "__main__":
    unittest.main()
