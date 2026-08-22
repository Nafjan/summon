"""Provider-inert reconciliation for the local telemetry spool.

This module deliberately does not read a provider, a prompt, or a release
artifact.  It operates on already-projected schema-2 events and gives the
offline audit a deterministic way to collapse replayed terminals and exclude
ambiguous records.  It is intentionally not a public CLI yet.
"""

from __future__ import annotations

from typing import Iterable, Mapping
import re


SCHEMA_VERSION = 2
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_COHORTS = {"test", "review", "preflight", "orchestration"}
_OPERATIONS = {
    "dispatch", "chat_turn", "council", "deliberate", "manifest",
    "resume", "swarm", "doctor", "onboarding",
}
_STATUSES = {"success", "error", "blocked", "partial", "cancelled", "unknown"}
_FAILURE_CLASSES = {
    "success", "blocked", "timeout", "cancelled", "authentication", "quota",
    "permission", "transport", "missing_cli", "invalid_input", "report_contract",
    "worktree", "backend", "dispatch", "partial", "unknown",
}
_EVIDENCE = {"reported", "inferred", "absent", "unknown"}
_AUTH_EVIDENCE = {"reported", "absent"}
_AUTH_OUTCOMES = {
    "not_needed", "refreshed", "login_started", "login_required", "recovered",
    "failed", "declined", "cancelled", "unknown",
}
_AUTH_STAGES = {"preflight", "call", "refresh", "login", "retry", "handoff", "terminal", "unknown"}
_REMEDIATIONS = {
    "none", "provider_login_required", "provider_refresh_failed",
    "provider_auth_cancelled", "provider_quota_wait", "provider_cli_missing",
    "provider_config_required", "unknown",
}
_TIMEOUT_STAGES = {
    "queue", "preflight", "backend_execution", "stream", "report_validation",
    "council_overall", "owner_shutdown", "unknown",
}
_REPORT_ERROR_CODES = {"missing", "incomplete", "invalid", "parse_error", "unknown"}
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
TERMINAL_KINDS = {
    "provider_turn_terminal": "provider_turn",
    "attempt_finished": "attempt",
    "operation_terminal": "operation",
}
START_KINDS = {
    "provider_turn_started": "provider_turn",
    "attempt_started": "attempt",
    "operation_started": "operation",
}
CANONICAL_TERMINAL_FIELDS = (
    "terminal_kind", "status", "execution_status", "failure_class",
    "report_ok", "report_error_code", "result_usable", "model_requested_class",
    "model_served_class", "served_model_evidence", "model_mismatch",
    "auth_stage", "auth_outcome", "interactive_required", "remediation_code",
    "auth_lifecycle_evidence", "timeout_stage", "exit_code",
)


def _enum(value: object, allowed: set[str]) -> bool:
    """Membership that remains safe for unhashable JSON-shaped values."""
    return isinstance(value, str) and value in allowed


def _null_normalize(value):
    """Keep only JSON scalar values used by the terminal contract."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return None


def canonical_terminal_payload(event: Mapping[str, object]) -> dict:
    """Return the exact payload used for duplicate/conflict comparison."""
    return {key: _null_normalize(event.get(key)) for key in CANONICAL_TERMINAL_FIELDS}


def canonical_start_payload(event: Mapping[str, object]) -> dict:
    """Return the stable start fields used for duplicate/conflict checks."""
    return {key: _null_normalize(event.get(key)) for key in (
        "event_kind", "operation", "cohort", "summon_version",
        "validation_outcome", "served_model_evidence",
    )}


def terminal_grain(event: Mapping[str, object]) -> str | None:
    kind = event.get("event_kind")
    return TERMINAL_KINDS.get(kind) if isinstance(kind, str) else None


def identity_key(event: Mapping[str, object]) -> tuple | None:
    """Return the stable identity tuple for a terminal event, if complete."""
    grain = terminal_grain(event)
    if grain == "provider_turn":
        fields = ("operation_id", "attempt_id", "provider_turn_id")
    elif grain == "attempt":
        fields = ("operation_id", "attempt_id")
    elif grain == "operation":
        fields = ("operation_id",)
    else:
        return None
    values = tuple(event.get(field) for field in fields)
    if any(not isinstance(value, str) or not value for value in values):
        return None
    return (grain, *values)


def _start_key(event: Mapping[str, object]) -> tuple | None:
    kind = event.get("event_kind")
    grain = START_KINDS.get(kind) if isinstance(kind, str) else None
    if grain == "provider_turn":
        fields = ("operation_id", "attempt_id", "provider_turn_id")
    elif grain == "attempt":
        fields = ("operation_id", "attempt_id")
    elif grain == "operation":
        fields = ("operation_id",)
    else:
        return None
    values = tuple(event.get(field) for field in fields)
    if any(not isinstance(value, str) or not value for value in values):
        return None
    return (grain, *values)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _valid_event(event: Mapping[str, object]) -> bool:
    """Reject forged/ambiguous records before they reach any denominator."""
    if not _enum(event.get("cohort"), _COHORTS):
        # production_like needs the signed provenance verifier, which is not
        # part of this provider-inert module; legacy_unknown is never eligible.
        return False
    if not _enum(event.get("operation"), _OPERATIONS):
        return False
    if not isinstance(event.get("summon_version"), str) or not _VERSION_RE.fullmatch(
            event.get("summon_version")):
        return False
    if not _valid_id(event.get("operation_id")):
        return False
    kind = event.get("event_kind")
    if not _enum(kind, set(TERMINAL_KINDS) | set(START_KINDS)):
        return False
    if _enum(kind, set(TERMINAL_KINDS)):
        # A terminal enters a denominator only when the writer explicitly
        # declared its projection valid.  ``None`` remains compatible only for
        # non-terminal lifecycle records from the Milestone-1 transition.
        if event.get("validation_outcome") != "valid":
            return False
        if event.get("terminal_kind") != TERMINAL_KINDS[kind]:
            return False
    elif _enum(kind, set(START_KINDS)):
        # Start events carry no terminal kind.
        if event.get("validation_outcome") not in (None, "valid"):
            return False
        if event.get("terminal_kind") is not None:
            return False
    if kind in ("provider_turn_started", "provider_turn_terminal"):
        if not _valid_id(event.get("attempt_id")) or not _valid_id(event.get("provider_turn_id")):
            return False
    elif kind in ("attempt_started", "attempt_finished"):
        if not _valid_id(event.get("attempt_id")):
            return False
    is_start = _enum(kind, set(START_KINDS))
    if not _enum(event.get("status"), _STATUSES) and not (is_start and event.get("status") is None):
        return False
    if not _enum(event.get("failure_class"), _FAILURE_CLASSES) and not (
            is_start and event.get("failure_class") is None):
        return False
    execution_status = event.get("execution_status")
    if execution_status is not None and not _enum(execution_status, _STATUSES):
        return False
    for key in ("result_usable", "provider_contacted"):
        value = event.get(key)
        if value is not None and not isinstance(value, bool):
            return False
    exit_code = event.get("exit_code")
    if (exit_code is not None and
            (not isinstance(exit_code, int) or isinstance(exit_code, bool)
             or exit_code < -255 or exit_code > 255)):
        return False
    for key in ("model_requested_class", "model_served_class"):
        value = event.get(key)
        if value is not None and (not isinstance(value, str)
                                  or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)):
            return False
    if not _enum(event.get("served_model_evidence"), _EVIDENCE) and not (
            is_start and event.get("served_model_evidence") is None):
        return False
    if event.get("model_mismatch") is not None and not isinstance(event.get("model_mismatch"), bool):
        return False
    if event.get("auth_stage") is not None and not _enum(event.get("auth_stage"), _AUTH_STAGES):
        return False
    if event.get("auth_outcome") is not None and not _enum(event.get("auth_outcome"), _AUTH_OUTCOMES):
        return False
    if event.get("interactive_required") is not None and not isinstance(event.get("interactive_required"), bool):
        return False
    if event.get("remediation_code") is not None and not _enum(event.get("remediation_code"), _REMEDIATIONS):
        return False
    auth_evidence = event.get("auth_lifecycle_evidence")
    if not _enum(auth_evidence, _AUTH_EVIDENCE) and not (is_start and auth_evidence is None):
        return False
    if any(event.get(key) is not None for key in (
            "auth_stage", "auth_outcome", "interactive_required", "remediation_code")):
        if auth_evidence != "reported":
            return False
    if event.get("timeout_stage") is not None and not _enum(event.get("timeout_stage"), _TIMEOUT_STAGES):
        return False
    if not is_start:
        if not isinstance(event.get("report_expected"), bool):
            return False
        report_ok = event.get("report_ok")
        if report_ok is not None and not isinstance(report_ok, bool):
            return False
        report_code = event.get("report_error_code")
        if report_code is not None and not _enum(report_code, _REPORT_ERROR_CODES):
            return False
        if event.get("operation") in {"council", "manifest"}:
            if (event.get("report_expected") is not False
                    or report_ok is not None or report_code is not None):
                return False
        elif event.get("report_expected") is not True:
            return False
        elif report_ok is True and report_code is not None:
            return False
        elif report_ok is False and report_code is None:
            return False
        elif report_ok is None and report_code != "missing":
            return False
    # A reported model is only denominator-safe once both the adapter and the
    # evidence-registry revision are bound.  This compatibility slice emits no
    # such binding, so it remains diagnostic-only rather than pretending to be
    # release evidence.
    if event.get("served_model_evidence") == "reported":
        # The spool is user-writable.  Plain revision strings do not prove a
        # registry/parser actually verified the receipt, so reported evidence
        # stays out of every denominator until the signed registry binding is
        # implemented by a later slice.
        return False
    if auth_evidence == "reported":
        # Same rule for auth recovery claims: offline audit cannot authenticate
        # their source without the deferred signed provenance binding.
        return False
    for key in ("provider_adapter_revision", "evidence_registry_revision"):
        value = event.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value)):
            return False
    return True


def reconcile(events: Iterable[Mapping[str, object]]) -> dict:
    """Reconcile projected events without trusting event order or event IDs.

    Exact duplicate terminals collapse.  A changed payload for the same
    identity becomes a conflict and is excluded.  Provider-turn and attempt
    terminals require a retained start; operation terminals may stand alone in
    the Milestone-1 compatibility writer.  A start without a terminal is
    ``incomplete_open``; a terminal whose start was trimmed is
    ``incomplete_truncated``.
    """
    rows = list(events)
    starts: dict[tuple, Mapping[str, object]] = {}
    terminals: dict[tuple, Mapping[str, object]] = {}
    duplicates: list[Mapping[str, object]] = []
    duplicate_starts: list[Mapping[str, object]] = []
    conflicts: list[dict] = []
    start_conflicts: list[dict] = []
    invalid: list[Mapping[str, object]] = []
    orphan: list[Mapping[str, object]] = []
    late: list[Mapping[str, object]] = []
    conflict_keys: set[tuple] = set()
    start_conflict_keys: set[tuple] = set()
    late_keys: set[tuple] = set()
    accepted: list[Mapping[str, object]] = []

    for event in rows:
        if not isinstance(event, Mapping) or event.get("schema") != SCHEMA_VERSION:
            invalid.append(event)
            continue
        kind = event.get("event_kind")
        if isinstance(kind, str) and kind in START_KINDS:
            if not _valid_event(event):
                invalid.append(event)
                continue
            key = _start_key(event)
            if key is None:
                invalid.append(event)
            elif key in terminals:
                # Retention/order says the terminal was already observed.  A
                # later start cannot retroactively authenticate it.
                late.append(event)
                late_keys.add(key)
            else:
                prior_start = starts.get(key)
                if prior_start is None:
                    starts[key] = event
                elif canonical_start_payload(prior_start) == canonical_start_payload(event):
                    duplicate_starts.append(event)
                else:
                    start_conflicts.append({"key": key, "first": prior_start, "second": event})
                    start_conflict_keys.add(key)
            continue
        grain = terminal_grain(event)
        if grain is None:
            invalid.append(event)
            continue
        if not _valid_event(event):
            invalid.append(event)
            continue
        key = identity_key(event)
        if key is None:
            invalid.append(event)
            continue
        prior = terminals.get(key)
        if prior is not None:
            if canonical_terminal_payload(prior) == canonical_terminal_payload(event):
                duplicates.append(event)
            else:
                conflicts.append({"key": key, "first": prior, "second": event})
                conflict_keys.add(key)
            continue
        terminals[key] = event

    for key, event in terminals.items():
        grain = key[0]
        if key in conflict_keys or key in start_conflict_keys:
            # A conflict invalidates the identity, including the first record
            # retained in ``terminals`` before the second appeared.
            continue
        if key in late_keys or (grain in ("provider_turn", "attempt") and key not in starts):
            orphan.append(event)
        else:
            accepted.append(event)

    incomplete_open = [event for key, event in starts.items()
                       if key not in terminals and key not in start_conflict_keys]
    # Operation terminals are intentionally standalone-compatible in this
    # slice; provider-turn/attempt orphans are the trimmed-start condition.
    incomplete_truncated = list(orphan)
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "duplicate_starts": duplicate_starts,
        "conflicts": conflicts,
        "start_conflicts": start_conflicts,
        "invalid": invalid,
        "late": late,
        "incomplete_open": incomplete_open,
        "incomplete_truncated": incomplete_truncated,
        "counts": {
            "input": len(rows),
            "accepted": len(accepted),
            "duplicates": len(duplicates),
            "duplicate_starts": len(duplicate_starts),
            "conflicts": len(conflicts),
            "start_conflicts": len(start_conflicts),
            "invalid": len(invalid),
            "late": len(late),
            "incomplete_open": len(incomplete_open),
            "incomplete_truncated": len(incomplete_truncated),
        },
    }
