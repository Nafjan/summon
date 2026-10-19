from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "summon" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _executor
import _telemetry
import _telemetry_audit
import run_subagent


class TelemetryProducerTests(unittest.TestCase):
    """Exercise the emitter seam without a provider or external spool."""

    def envelope(self, evidence, *, served="gpt-5.6-sol"):
        return {
            "status": "success", "execution_status": "success",
            "exit_code": 0, "attempts": 1, "provider_contacted": True,
            "served_model_evidence": evidence,
            "model": {"requested": "gpt-5.6-sol", "targeted": "gpt-5.6-sol",
                      "served": served},
            # Caller-visible booleans are deliberately untrusted.
            "model_match": True, "named_model_verified": True,
        }

    def emit_event(self, envelope, *, trusted=True, cohort=None):
        events = []

        def project(obj, *, operation):
            events.append(_telemetry.event_from_envelope(
                obj, operation_context=_telemetry.new_operation_context(operation),
                cohort=cohort))

        output = io.StringIO()
        with (mock.patch.object(run_subagent, "_JOB_FILE", None),
              mock.patch.object(run_subagent, "_GOVERNED_RESUME_LINEAGE", None),
              mock.patch.object(run_subagent, "_resolve_job_file", return_value=None),
              mock.patch.object(_telemetry, "record", side_effect=project),
              contextlib.redirect_stdout(output)):
            run_subagent._emit(envelope, trusted_executor_result=trusted)
        self.assertEqual(len(events), 1)
        self.assertIsNotNone(events[0])
        public = json.loads(output.getvalue())
        self.assertNotIn("_telemetry_model_evidence", public)
        self.assertNotIn("_telemetry_model_evidence", envelope)
        return events[0], public

    def assert_unknown_model(self, event):
        self.assertIsNone(event["model_match"])
        self.assertIsNone(event["model_mismatch"])
        self.assertFalse(event["named_model_verified"])

    def test_fresh_inferred_result_is_valid_with_unknown_identity(self):
        event, _ = self.emit_event(self.envelope("inferred"))
        self.assertEqual(event["validation_outcome"], "valid")
        self.assertEqual(event["served_model_evidence"], "inferred")
        self.assert_unknown_model(event)

    def test_fresh_absent_result_is_valid_with_unknown_identity(self):
        event, _ = self.emit_event(self.envelope("absent", served=None))
        self.assertEqual(event["validation_outcome"], "valid")
        self.assertEqual(event["served_model_evidence"], "absent")
        self.assert_unknown_model(event)

    def test_fresh_nonreported_mismatch_is_unknown(self):
        for evidence in ("inferred", "absent"):
            with self.subTest(evidence=evidence):
                event, _ = self.emit_event(self.envelope(evidence, served="gpt-5.6-luna"))
                self.assertEqual(event["validation_outcome"], "valid")
                self.assert_unknown_model(event)

    def test_fresh_not_run_result_preserves_no_execution_contract(self):
        envelope = self.envelope("absent", served=None)
        envelope.update(status="blocked", execution_status="not_run",
                        attempts=0, attempt_status="not_run",
                        provider_contacted=False)
        event, _ = self.emit_event(envelope)
        self.assertEqual(event["validation_outcome"], "valid")
        self.assertEqual(event["attempts"], 0)
        self.assertEqual(event["attempt_status"], "not_run")
        self.assertFalse(event["provider_contacted"])
        self.assertIsNone(event["model_served"])
        self.assert_unknown_model(event)

    def test_fresh_reported_exact_identity_still_verifies(self):
        event, _ = self.emit_event(self.envelope("reported"))
        self.assertTrue(event["model_match"])
        self.assertFalse(event["model_mismatch"])
        self.assertTrue(event["named_model_verified"])

    def test_fresh_reported_mismatch_still_fails_named_identity(self):
        event, _ = self.emit_event(self.envelope("reported", served="gpt-5.6-luna"))
        self.assertFalse(event["model_match"])
        self.assertTrue(event["model_mismatch"])
        self.assertFalse(event["named_model_verified"])

    def test_public_and_serialized_envelopes_cannot_mint_evidence(self):
        for evidence in ("reported", "inferred", "absent"):
            with self.subTest(evidence=evidence):
                raw = self.envelope(evidence)
                _, public = self.emit_event(dict(raw))
                for envelope in (raw, json.loads(json.dumps(public))):
                    with mock.patch.object(_executor, "trusted_telemetry_model_evidence") as producer:
                        event, _ = self.emit_event(envelope, trusted=False)
                    producer.assert_not_called()
                    self.assertEqual(event["validation_outcome"], "invalid_model_evidence")
                    self.assert_unknown_model(event)

    def test_unrecognized_fresh_evidence_remains_invalid(self):
        for evidence in ("unknown", "forged", [], {}, 1):
            with self.subTest(evidence=evidence):
                event, _ = self.emit_event(self.envelope(evidence))
                self.assertEqual(event["validation_outcome"], "invalid_model_evidence")
                self.assert_unknown_model(event)

    def test_fresh_evidence_does_not_promote_production_cohort(self):
        for evidence in ("reported", "inferred", "absent"):
            with self.subTest(evidence=evidence):
                event, _ = self.emit_event(self.envelope(evidence), cohort="production_like")
                self.assertEqual(event["validation_outcome"], "invalid_cohort")
                self.assertEqual(event["cohort"], "test")

    def test_audit_accepts_nonreported_terminals_but_requires_reported_binding(self):
        for evidence in ("inferred", "absent", "reported"):
            with self.subTest(evidence=evidence):
                event, _ = self.emit_event(self.envelope(evidence))
                result = _telemetry_audit.reconcile([event])
                self.assertEqual(result["counts"]["accepted"], int(evidence != "reported"))
                self.assertEqual(result["counts"]["invalid"], int(evidence == "reported"))

    def test_dry_run_and_skipped_cached_envelopes_do_not_emit_telemetry(self):
        for preview in (
            {"dry_run": True, "cli": "codex"},
            {"status": "dry_run", "cli": "codex"},
            {"status": "success", "skipped": True, "cli": "codex"},
        ):
            with self.subTest(preview=preview):
                with (
                    mock.patch.object(run_subagent._telemetry, "record") as record,
                    mock.patch("builtins.print"),
                ):
                    run_subagent._emit(dict(preview), operation="dispatch")
                record.assert_not_called()

    def test_incompatible_background_out_refusal_records_valid_invalid_input(self):
        envelope = {
            "status": "error",
            "result": "",
            "exit_code": 1,
            "error": (
                "--background and --out are incompatible: background reports "
                "completion via its own result_file; --out is the (manifest) "
                "result-file mechanism. Use --manifest for fan-out with result files."
            ),
            "error_kind": "invalid_input",
        }
        run_subagent._mark_not_run(envelope)
        event, _ = self.emit_event(envelope, trusted=True)
        self.assertEqual(event["validation_outcome"], "valid")
        self.assertEqual(event["failure_class"], "invalid_input")
        self.assertEqual(event["execution_status"], "not_run")

    def test_opencode_zcode_t3_backends_are_preserved_in_telemetry(self):
        for backend in ("opencode", "zcode", "t3"):
            with self.subTest(backend=backend):
                env = self.envelope("absent", served=None)
                env["cli"] = backend
                event, _ = self.emit_event(env, trusted=True)
                self.assertEqual(event["backend"], backend)


if __name__ == "__main__":
    unittest.main()

