"""Private turn-economics records and safe public summaries.

This module is intentionally provider-inert.  It records what Summon can prove
at its own adapter boundary and keeps provider-side accounting unknown unless a
separate verified receipt supplies it.  A process launch is a possible
submission, not proof that a remote provider accepted a request.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import _context_policy
import _submission_accounting


SCHEMA = "summon.turn-economics/v1"
SUMMARY_SCHEMA = "summon.turn-economics-summary/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class TurnEconomicsError(ValueError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"turn economics refused ({kind})")


def _fail(kind: str) -> None:
    raise TurnEconomicsError(kind)


def _id(value: Any) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("invalid_id")
    return value


def _sha(value: Any) -> str:
    if type(value) is not str or not _SHA.fullmatch(value):
        _fail("invalid_digest")
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("invalid_json")


def _policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("origin") == "derived-compatibility":
        if dict(value) != _context_policy.derived_legacy():
            _fail("invalid_derived_policy")
        return dict(value)
    return _context_policy.validate(dict(value))


def make(*, session_id: str, participant: str, turn_id: str,
         owner_generation: int, attempt_id: str, policy: Mapping[str, Any],
         source_selection_sha256: str, payload_sha256: str,
         source_count: int, omitted_count: int, status: str,
         submission_state: str, envelope: Mapping[str, Any] | None = None,
         launch_observation: Mapping[str, Any] | None = None,
         handoff_verified: bool = False) -> dict[str, Any]:
    """Create one private, identity-bound turn economics record."""
    for item in (session_id, participant, turn_id, attempt_id):
        _id(item)
    if type(owner_generation) is not int or isinstance(owner_generation, bool) or owner_generation < 1:
        _fail("owner_generation")
    _sha(source_selection_sha256); _sha(payload_sha256)
    if type(source_count) is not int or not 0 <= source_count <= 1024:
        _fail("source_count")
    if type(omitted_count) is not int or not 0 <= omitted_count <= 1024:
        _fail("omitted_count")
    if status not in {"success", "partial", "blocked", "error", "cancelled", "timeout"}:
        _fail("status")
    if submission_state not in {"not_submitted", "possible", "submitted", "indeterminate"}:
        _fail("submission_state")
    checked_policy = _policy(policy)
    env = dict(envelope) if isinstance(envelope, Mapping) else {}
    prompt_text = env.get("prompt_text")
    if not isinstance(prompt_text, str) or not prompt_text:
        _fail("prompt_boundary_missing")
    authenticated_handoff = False
    if handoff_verified and isinstance(launch_observation, Mapping):
        try:
            from _launch_binding import valid_projection
            authenticated_handoff = bool(valid_projection(launch_observation))
        except (ImportError, TypeError, ValueError):
            authenticated_handoff = False
    # Keep the accounting estimate to Summon's visible prompt boundary.  The
    # provider may add system/tool context that is outside this record's scope.
    estimate = _submission_accounting.estimate_payload(
        [("chat", prompt_text)],
        boundary="conversation-dispatch",
        excludes=("conversation_journal_metadata",))
    usage_record = _submission_accounting.private_record(
        attempt_id=attempt_id, attempt_kind="chat-turn", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=payload_sha256, estimate=estimate,
        envelope=env, submission_state=submission_state)
    record = {
        "schema": SCHEMA,
        "turn": {"session_id": session_id, "participant": participant,
                 "turn_id": turn_id, "owner_generation": owner_generation,
                 "attempt_id": attempt_id},
        "policy": {"value": checked_policy,
                    "digest": hashlib.sha256(_canonical(checked_policy)).hexdigest()},
        "source": {"selection_sha256": source_selection_sha256,
                    "selected_count": source_count, "omitted_count": omitted_count},
        "payload_sha256": payload_sha256,
        "status": status,
        "submission": usage_record,
        "handoff": {"verified": authenticated_handoff,
                     "observation_schema": (launch_observation.get("schema")
                                             if authenticated_handoff
                                             else None)},
    }
    _canonical(record)
    return record


def public_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only turn-economics shape allowed on public chat events."""
    if not isinstance(record, Mapping) or record.get("schema") != SCHEMA:
        _fail("record_schema")
    turn = record.get("turn") if isinstance(record.get("turn"), Mapping) else {}
    policy = record.get("policy", {}).get("value") if isinstance(record.get("policy"), Mapping) else None
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    submission = record.get("submission") if isinstance(record.get("submission"), Mapping) else {}
    state = submission.get("submission_state")
    if state not in {"not_submitted", "possible", "submitted", "indeterminate"}:
        _fail("submission_state")
    public_policy = _context_policy.public(policy) if isinstance(policy, Mapping) else None
    return {
        "schema": SUMMARY_SCHEMA,
        "status": record.get("status"),
        "submission_state": state,
        "provider_contact": ("unknown" if state in {"possible", "indeterminate"}
                             else "occurred" if state == "submitted" else "none"),
        "unknown_spend": submission.get("unknown_spend") is True,
        "context_policy": public_policy,
        "selected_messages": source.get("selected_count"),
        "omitted_messages": source.get("omitted_count"),
        "handoff_verified": (record.get("handoff", {}).get("verified") is True
                              if isinstance(record.get("handoff"), Mapping) else False),
        "owner_generation": turn.get("owner_generation"),
    }


def validate_public_summary(value: Any) -> dict[str, Any]:
    """Validate a summary crossing the conversation public boundary."""
    if not isinstance(value, Mapping):
        _fail("public_summary_shape")
    required = {"schema", "status", "submission_state", "provider_contact",
                "unknown_spend", "context_policy", "selected_messages",
                "omitted_messages", "handoff_verified", "owner_generation"}
    if set(value) != required or value.get("schema") != SUMMARY_SCHEMA:
        _fail("public_summary_fields")
    if value.get("status") not in {"success", "partial", "blocked", "error", "cancelled", "timeout"}:
        _fail("public_summary_status")
    if value.get("submission_state") not in {"not_submitted", "possible", "submitted", "indeterminate"}:
        _fail("public_summary_submission")
    expected_contact = ("unknown" if value["submission_state"] in {"possible", "indeterminate"}
                       else "occurred" if value["submission_state"] == "submitted" else "none")
    if value.get("provider_contact") != expected_contact or type(value.get("unknown_spend")) is not bool:
        _fail("public_summary_contact")
    if (type(value.get("selected_messages")) is not int or not 0 <= value["selected_messages"] <= 1024
            or type(value.get("omitted_messages")) is not int or not 0 <= value["omitted_messages"] <= 1024
            or type(value.get("handoff_verified")) is not bool
            or type(value.get("owner_generation")) is not int or value["owner_generation"] < 1):
        _fail("public_summary_bounds")
    context_policy = value.get("context_policy")
    if not isinstance(context_policy, Mapping):
        _fail("public_summary_policy")
    try:
        _context_policy.validate_public(dict(context_policy))
    except _context_policy.ContextPolicyError as exc:
        raise TurnEconomicsError("public_summary_policy") from exc
    return dict(value)


__all__ = ["SCHEMA", "SUMMARY_SCHEMA", "TurnEconomicsError", "make", "public_summary",
           "validate_public_summary"]
