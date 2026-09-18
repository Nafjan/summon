"""Attempt-scoped provider submission accounting.

Private records bind immutable request/material identities to one physical adapter
attempt.  ``public_summary`` is the only projector intended for public/job-status
surfaces; it deliberately drops attempt ids, hashes, prompts, launch observations,
account details, and provider handles.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Iterable


ACCOUNTING_SCHEMA = "summon.submission-accounting/v1"
SUMMARY_SCHEMA = "summon.submission-summary/v1"
METRICS = (
    "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
    "cache_read_tokens", "cache_write_tokens",
)
_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "inputTokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "outputTokens", "output"),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
    "reasoning_tokens": ("reasoning_tokens", "thinking_tokens", "reasoningTokens", "reasoning"),
    "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens", "cache_read"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWriteTokens", "cache_write"),
}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _json_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def estimate_payload(parts: Iterable[tuple[str, str]], *, boundary: str,
                     excludes: Iterable[str] = ()) -> dict:
    """Estimate only text Summon can see at the final adapter boundary.

    The digest uses length-framed role/text pairs while ``represented_bytes`` is
    the exact UTF-8 text byte count.  Framing is identity metadata and therefore
    is not included in the bytes/4 estimate.
    """
    normalized = []
    represented = 0
    for role, text in parts:
        if not isinstance(role, str) or not isinstance(text, str):
            raise ValueError("adapter payload parts must be text pairs")
        raw = text.encode("utf-8")
        represented += len(raw)
        normalized.append({"role": role, "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()})
    return {
        "method": "utf8_bytes_divided_by_4_ceiling",
        "represented_bytes": represented,
        "estimated_tokens": (represented + 3) // 4,
        "scope": "summon_visible_adapter_input",
        "boundary": boundary,
        "complete_provider_input": False,
        "outside_scope": sorted(set(excludes) | {
            "provider_or_cli_added_system_context",
            "provider_wrapper_or_tool_context",
        }),
        "material_sha256": _json_sha(normalized),
    }


def _number(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return None
    return value


def normalize_reported_usage(usage: object, observation: object = None) -> dict:
    source = None
    scope = "unestablished"
    if isinstance(observation, dict):
        if isinstance(observation.get("source"), str):
            source = observation["source"][:80]
        if observation.get("scope") in {"attempt_total", "last_step_snapshot"}:
            scope = observation["scope"]
    values = {name: None for name in METRICS}
    states = {name: "absent" for name in METRICS}
    malformed = []
    if isinstance(usage, dict):
        for name, aliases in _ALIASES.items():
            present = [key for key in aliases if key in usage]
            if not present:
                continue
            selected = None
            for key in present:
                candidate = _number(usage.get(key))
                if candidate is not None:
                    selected = candidate
                    break
            if selected is None:
                states[name] = "malformed"
                malformed.append(name)
            else:
                values[name] = selected
                states[name] = "reported"
    elif usage is not None:
        malformed = list(METRICS)
        states = {name: "malformed" for name in METRICS}
    reported_count = sum(value is not None for value in values.values())
    return {
        "metrics": values,
        "field_state": states,
        "provenance": source or "unavailable",
        "observation_scope": scope,
        "completeness": ("missing" if reported_count == 0 and not malformed
                         else "malformed" if malformed
                         else "complete" if reported_count == len(METRICS)
                         else "partial"),
    }


def private_record(*, attempt_id: str, attempt_kind: str, attempt_ordinal: int,
                   parent_attempt_id: str | None, request_sha256: str | None,
                   estimate: dict, envelope: dict | None = None,
                   submission_state: str | None = None) -> dict:
    if not _ID_RE.fullmatch(attempt_id or ""):
        raise ValueError("attempt_id is invalid")
    if request_sha256 is not None and not _SHA_RE.fullmatch(request_sha256):
        raise ValueError("request_sha256 is invalid")
    if parent_attempt_id is not None and not _ID_RE.fullmatch(parent_attempt_id):
        raise ValueError("parent_attempt_id is invalid")
    env = envelope if isinstance(envelope, dict) else {}
    contact = env.get("provider_contacted")
    uncertain_spend = env.get("uncertain_spend") is True
    if submission_state is None:
        explicit = env.get("submission_state")
        if explicit in {"possible", "submitted", "not_submitted", "indeterminate"}:
            submission_state = explicit
        else:
            # A transport may know that it opened a process/socket without
            # knowing whether the remote endpoint accepted the request. Never
            # upgrade that launch fact into a verified submission.
            submission_state = ("not_submitted" if contact is False
                                else "indeterminate")
    if submission_state not in {"possible", "submitted", "not_submitted", "indeterminate"}:
        raise ValueError("submission_state is invalid")
    reported = normalize_reported_usage(env.get("usage"), env.get("usage_observation"))
    possible = (True if submission_state == "submitted" else False
                if submission_state == "not_submitted" else None)
    local_process = env.get("local_process_created")
    unknown_spend = (uncertain_spend
                     or submission_state in {"possible", "indeterminate"})
    if submission_state == "submitted":
        unknown_spend = unknown_spend or not (
            reported["observation_scope"] == "attempt_total"
            and reported["metrics"]["total_tokens"] is not None)
    return {
        "schema": ACCOUNTING_SCHEMA,
        "attempt": {
            "id": attempt_id,
            "kind": attempt_kind if isinstance(attempt_kind, str) else "initial",
            "ordinal": attempt_ordinal if isinstance(attempt_ordinal, int) else 1,
            "parent_id": parent_attempt_id,
        },
        "identity": {
            "request_sha256": request_sha256,
            "material_sha256": estimate.get("material_sha256"),
        },
        "estimate": copy.deepcopy(estimate),
        "reported": reported,
        "contact": {
            "local_process_created": (local_process if isinstance(local_process, bool) else None),
            "possible_submission": possible,
            "evidence": ("adapter_report" if reported["provenance"] != "unavailable"
                         else "launch_boundary_only" if contact is True
                         else "no_contact" if contact is False else "unavailable"),
        },
        "submission_state": submission_state,
        "uncertain_spend": uncertain_spend,
        "unknown_spend": bool(unknown_spend),
    }


def _records(value: object) -> list[dict]:
    if isinstance(value, dict) and value.get("schema") == ACCOUNTING_SCHEMA:
        return [value]
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, dict) and item.get("schema") == ACCOUNTING_SCHEMA]
    return []


def public_summary(value: object) -> dict:
    """Purpose-limited aggregate. Never copy private record fields here."""
    unique: dict[str, dict] = {}
    replay_conflicts = 0
    for record in _records(value):
        attempt = record.get("attempt") or {}
        attempt_id = attempt.get("id")
        if not _ID_RE.fullmatch(attempt_id or ""):
            continue
        prior = unique.get(attempt_id)
        if prior is None:
            unique[attempt_id] = record
        elif _json_sha(prior) != _json_sha(record):
            replay_conflicts += 1
    records = list(unique.values())
    states = {name: 0 for name in ("submitted", "possible", "indeterminate", "not_submitted")}
    estimate_bytes = estimate_tokens = estimate_covered = 0
    submitted = []
    attempts = []
    for record in records:
        state = record.get("submission_state")
        if state in states:
            states[state] += 1
        # A ``possible`` prelaunch record is a candidate physical attempt whose
        # submission outcome is unresolved.  Keep it out of the confirmed
        # physical-submissions count, but include it in completeness
        # denominators so a missing terminal settlement cannot look complete.
        if state in {"submitted", "possible", "indeterminate"}:
            attempts.append(record)
        if state == "submitted":
            submitted.append(record)
        if state in {"submitted", "possible", "indeterminate"}:
            estimate = record.get("estimate") or {}
            b = _number(estimate.get("represented_bytes"))
            t = _number(estimate.get("estimated_tokens"))
            if b is not None and t is not None:
                estimate_bytes += b
                estimate_tokens += t
                estimate_covered += 1
    reported_public = {}
    for metric in METRICS:
        subtotal = 0
        covered = 0
        for record in attempts:
            reported = record.get("reported") or {}
            value_for_metric = (reported.get("metrics") or {}).get(metric)
            field_state = (reported.get("field_state") or {}).get(metric)
            if (reported.get("observation_scope") == "attempt_total"
                    and field_state == "reported"
                    and _number(value_for_metric) is not None):
                subtotal += value_for_metric
                covered += 1
        reported_public[metric] = {
            "known_subtotal": subtotal if covered else None,
            "covered_attempts": covered,
            "missing_attempts": len(attempts) - covered,
            "covered_submissions": covered,
            "missing_submissions": len(attempts) - covered,
            "completeness": "complete" if covered == len(attempts) else "partial",
        }
    total_metric = reported_public["total_tokens"]
    unknown_spend = bool(
        states["possible"] or states["indeterminate"] or replay_conflicts
        or any(record.get("unknown_spend") is True for record in records)
        or total_metric["missing_submissions"]
    )
    return {
        "schema": SUMMARY_SCHEMA,
        "candidate_records": len(records),
        "physical_submissions": len(submitted),
        "physical_attempts": len(attempts),
        "not_submitted": states["not_submitted"],
        "indeterminate_contact": states["possible"] + states["indeterminate"],
        "replay_conflicts": replay_conflicts,
        "estimate": {
            "method": "utf8_bytes_divided_by_4_ceiling",
            "scope": "summon_visible_adapter_input",
            "represented_bytes_subtotal": estimate_bytes if estimate_covered else None,
            "estimated_tokens_subtotal": estimate_tokens if estimate_covered else None,
            "covered_attempts": estimate_covered,
            "missing_attempts": len(attempts) - estimate_covered,
            "covered_submissions": estimate_covered,
            "missing_submissions": len(attempts) - estimate_covered,
            "completeness": "complete" if estimate_covered == len(attempts) else "partial",
            "complete_provider_input": False,
        },
        "reported": reported_public,
        "unknown_spend": unknown_spend,
    }


def records_from_envelope(envelope: dict) -> list[dict]:
    records = []
    history = envelope.get("attempt_history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                records.extend(_records(item.get("submission_accounting")))
    if not records:
        records.extend(_records(envelope.get("submission_accounting")))
    return records


def attach_public_summary(envelope: dict) -> None:
    records = records_from_envelope(envelope)
    if records:
        envelope["submission_summary"] = public_summary(records)
