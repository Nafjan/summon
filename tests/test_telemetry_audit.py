from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "summon_telemetry_audit",
    ROOT / "skills" / "summon" / "scripts" / "_telemetry_audit.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def terminal(*, operation="a" * 32, status="success", kind="operation_terminal",
             attempt=None, turn=None, **extra):
    event = {
        "schema": 2,
        "event_kind": kind,
        "operation_id": operation,
        "attempt_id": attempt,
        "provider_turn_id": turn,
        "cohort": "test",
        "operation": "dispatch",
        "summon_version": "3.1.0",
        "validation_outcome": "valid",
        "served_model_evidence": "absent",
        "terminal_kind": "operation" if kind == "operation_terminal" else "provider_turn",
        "status": status,
        "failure_class": "success" if status == "success" else "backend",
        "report_expected": True,
        "report_ok": None,
        "report_error_code": "missing",
        "auth_lifecycle_evidence": "absent",
        **extra,
    }
    return event


class TelemetryAuditTests(unittest.TestCase):
    def test_exact_duplicate_collapses_and_changed_terminal_conflicts(self):
        first = terminal()
        duplicate = dict(first, event_id="different")
        conflict = dict(first, status="error", failure_class="backend")
        result = MODULE.reconcile([first, duplicate, conflict])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["counts"]["conflicts"], 1)
        self.assertEqual(result["accepted"], [])

    def test_provider_turn_requires_start_and_open_is_distinct(self):
        complete = terminal(kind="provider_turn_terminal", attempt="b" * 32, turn="c" * 32)
        start = {"schema": 2, "event_kind": "provider_turn_started",
                 "operation_id": "a" * 32, "attempt_id": "b" * 32,
                 "provider_turn_id": "c" * 32, "cohort": "test",
                 "operation": "dispatch", "summon_version": "3.1.0",
                 "validation_outcome": "valid", "served_model_evidence": "absent"}
        open_start = {"schema": 2, "event_kind": "attempt_started",
                      "operation_id": "d" * 32, "attempt_id": "e" * 32,
                      "cohort": "test", "validation_outcome": "valid",
                      "operation": "dispatch", "summon_version": "3.1.0",
                      "served_model_evidence": "absent"}
        result = MODULE.reconcile([start, complete, open_start])
        self.assertEqual(result["counts"]["accepted"], 1)
        self.assertEqual(result["counts"]["incomplete_truncated"], 0)
        self.assertEqual(result["counts"]["incomplete_open"], 1)

        orphan = terminal(kind="provider_turn_terminal", operation="d" * 32,
                          attempt="e" * 32, turn="f" * 32)
        orphan["operation_id"] = "d" * 32
        result = MODULE.reconcile([orphan])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["incomplete_truncated"], 1)

    def test_late_start_cannot_authenticate_prior_terminal(self):
        terminal_first = terminal(kind="provider_turn_terminal", attempt="b" * 32,
                                  turn="c" * 32)
        late_start = {"schema": 2, "event_kind": "provider_turn_started",
                      "operation_id": "a" * 32, "attempt_id": "b" * 32,
                      "provider_turn_id": "c" * 32, "cohort": "test",
                      "operation": "dispatch", "summon_version": "3.1.0",
                      "validation_outcome": "valid", "served_model_evidence": "absent"}
        result = MODULE.reconcile([terminal_first, late_start])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["late"], 1)
        self.assertEqual(result["counts"]["incomplete_truncated"], 1)

    def test_legacy_and_malformed_records_never_enter_denominator(self):
        result = MODULE.reconcile([
            {"schema": 1, "event_kind": "operation_terminal"},
            {"schema": 2, "event_kind": "bogus", "operation_id": "x" * 32},
        ])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["invalid"], 2)

    def test_invalid_validation_cohort_and_auth_claims_are_excluded(self):
        forged = terminal(cohort="review", validation_outcome="invalid_model_evidence",
                           auth_outcome="recovered")
        self.assertEqual(MODULE.reconcile([forged])["counts"]["invalid"], 1)

        missing_validation = terminal(validation_outcome=None)
        invalid_auth = terminal(auth_stage="unbounded-auth-stage",
                                remediation_code="https://private.invalid/login")
        result = MODULE.reconcile([missing_validation, invalid_auth])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["invalid"], 2)

        invalid_start = {"schema": 2, "event_kind": "attempt_started",
                         "operation_id": "a" * 32, "attempt_id": "b" * 32,
                         "cohort": "test", "validation_outcome": "invalid_auth",
                         "operation": "dispatch", "summon_version": "3.1.0",
                         "served_model_evidence": "absent"}
        completed = terminal(kind="attempt_finished", attempt="b" * 32)
        completed["terminal_kind"] = "attempt"
        result = MODULE.reconcile([invalid_start, completed])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["counts"]["incomplete_truncated"], 1)

    def test_valid_standalone_operation_terminal_remains_compatible(self):
        result = MODULE.reconcile([terminal(validation_outcome="valid")])
        self.assertEqual(result["counts"]["accepted"], 1)

    def test_malformed_terminal_scalars_and_report_pair_are_excluded(self):
        for field, value in (("failure_class", "bogus"),
                             ("result_usable", {"nested": True}),
                             ("provider_contacted", "yes"),
                             ("exit_code", [1]),
                             ("model_requested_class", {"name": "private"})):
            forged = terminal(**{field: value})
            result = MODULE.reconcile([forged])
            self.assertEqual(result["counts"]["accepted"], 0, field)
            self.assertEqual(result["counts"]["invalid"], 1, field)
        forged = terminal(report_ok=True, report_error_code="invalid")
        result = MODULE.reconcile([forged])
        self.assertEqual(result["counts"]["invalid"], 1)

    def test_unhashable_and_unbound_reported_evidence_are_fail_closed(self):
        malformed = terminal(cohort=[], event_kind=["operation_terminal"],
                             timeout_stage={"stage": "backend_execution"})
        result = MODULE.reconcile([malformed])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["invalid"], 1)

        reported_model = terminal(served_model_evidence="reported",
                                  provider_adapter_revision="adapter.1",
                                  evidence_registry_revision="registry.1")
        reported_auth = terminal(auth_stage="terminal", auth_outcome="recovered",
                                interactive_required=False, remediation_code="none",
                                auth_lifecycle_evidence="reported")
        result = MODULE.reconcile([reported_model, reported_auth])
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["invalid"], 2)

    def test_terminal_payload_conflicts_on_auth_lifecycle_fields(self):
        first = terminal(auth_lifecycle_evidence="absent")
        changed = dict(first, auth_lifecycle_evidence="reported")
        self.assertNotEqual(MODULE.canonical_terminal_payload(first),
                            MODULE.canonical_terminal_payload(changed))


if __name__ == "__main__":
    unittest.main()
