"""Provider-free admission policy evaluation for the workspace fabric.

This module is deliberately detached from the coordinator and journal.  It
validates an explicit policy, a detached snapshot, and one request, then
returns a deterministic decision without I/O, mutation, scheduling, account
selection, authentication, or provider contact.  The caller must persist any
accepted decision through the existing coordinator-owned admission path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

import _workspace_protocol as protocol


POLICY_SCHEMA = "summon.workspace.admission-policy/v1"
SNAPSHOT_SCHEMA = "summon.workspace.admission-snapshot/v1"
REQUEST_SCHEMA = "summon.workspace.admission-request/v1"
RESULT_SCHEMA = "summon.workspace.admission-result/v1"
DECISION_SCHEMA = "summon.workspace.admission-decision/v1"

_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_MESSAGE_STATES = frozenset({"queued", "held", "included_in_attempt"})
_REQUEST_KINDS = frozenset({"message", "control"})
_OVERSIZE_ACTIONS = frozenset({"hold", "refuse"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REASONS = frozenset({
    "none", "rate_window_exhausted", "oversize_head_refused",
    "oversize_head_held", "oversize_head_blocks_stream",
    "stream_prefix_required", "stream_budget_exhausted",
    "journal_capacity_exhausted", "control_reserve_protected",
    "recovery_reserve_protected", "control_execution_slots_invalid",
})


class WorkspaceBudgetError(ValueError):
    """A malformed or internally inconsistent detached budget fact."""


def _fail(message: str) -> None:
    raise WorkspaceBudgetError(message)


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if type(value) is not dict:
        _fail("object required")
    keys = set(value)
    if not required <= keys or keys - required - optional:
        _fail("missing, unknown or invalid fields")


def _id(value: Any, label: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail(f"invalid {label}")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0,
             maximum: int = protocol.MAX_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"invalid {label}")
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkspaceBudgetError("budget fact is not finite canonical JSON") from exc
    if len(result) > protocol.MAX_RECORD_BYTES:
        _fail("budget fact exceeds the workspace record bound")
    return result


def _stream_limit(value: Any) -> dict[str, int]:
    _object(value, {"message_limit", "byte_limit"})
    return {
        "message_limit": _integer(value["message_limit"], "stream message limit", minimum=1),
        "byte_limit": _integer(value["byte_limit"], "stream byte limit", minimum=1),
    }


def validate_policy(value: Any) -> dict[str, Any]:
    """Validate one complete, explicit policy; no defaults are inferred."""
    _object(value, {"schema", "rate_window_ms", "rate_limit", "rate_bytes_limit",
                    "max_message_bytes", "journal_capacity_bytes", "control_reserve_bytes",
                    "recovery_reserve_bytes", "execution_slot_limit", "stream_limits",
                    "oversize_head_action"})
    if value["schema"] != POLICY_SCHEMA:
        _fail("unsupported admission policy schema")
    checked = {
        "schema": POLICY_SCHEMA,
        "rate_window_ms": _integer(value["rate_window_ms"], "rate window", minimum=1),
        "rate_limit": _integer(value["rate_limit"], "rate message limit", minimum=1),
        "rate_bytes_limit": _integer(value["rate_bytes_limit"], "rate byte limit", minimum=1),
        "max_message_bytes": _integer(value["max_message_bytes"], "maximum message bytes", minimum=1,
                                        maximum=protocol.MAX_CONTENT_BYTES),
        "journal_capacity_bytes": _integer(value["journal_capacity_bytes"], "journal capacity", minimum=1),
        "control_reserve_bytes": _integer(value["control_reserve_bytes"], "control reserve"),
        "recovery_reserve_bytes": _integer(value["recovery_reserve_bytes"], "recovery reserve"),
        "execution_slot_limit": _integer(value["execution_slot_limit"], "execution slot limit"),
        "oversize_head_action": value["oversize_head_action"],
    }
    if (type(checked["oversize_head_action"]) is not str
            or checked["oversize_head_action"] not in _OVERSIZE_ACTIONS):
        _fail("unsupported oversized-head action")
    if checked["control_reserve_bytes"] + checked["recovery_reserve_bytes"] > checked["journal_capacity_bytes"]:
        _fail("control and recovery reserves exceed journal capacity")
    limits = value["stream_limits"]
    if type(limits) is not dict or not limits:
        _fail("explicit stream limits required")
    checked["stream_limits"] = {}
    for stream_id, limit in limits.items():
        checked["stream_limits"][_id(stream_id, "stream id")] = _stream_limit(limit)
    _canonical_bytes(checked)
    return copy.deepcopy(checked)


def _rate_event(value: Any, *, now_ms: int) -> dict[str, int]:
    _object(value, {"at_ms", "messages", "bytes"})
    event = {
        "at_ms": _integer(value["at_ms"], "rate event time"),
        "messages": _integer(value["messages"], "rate event messages"),
        "bytes": _integer(value["bytes"], "rate event bytes"),
    }
    if event["at_ms"] > now_ms:
        _fail("rate event is from the future")
    return event


def _pending_message(value: Any) -> dict[str, Any]:
    _object(value, {"message_id", "stream_id", "sequence", "bytes", "state"}, {"source"})
    message = {
        "message_id": _id(value["message_id"], "message id"),
        "stream_id": _id(value["stream_id"], "stream id"),
        "sequence": _integer(value["sequence"], "message sequence", minimum=1),
        "bytes": _integer(value["bytes"], "message bytes", minimum=1,
                           maximum=protocol.MAX_CONTENT_BYTES),
        "state": value["state"],
    }
    if type(message["state"]) is not str or (
            message["state"] not in _MESSAGE_STATES
            and not (message["state"] == "offered" and value.get("source") == "inbox")):
        _fail("unsupported pending message state")
    if "source" in value:
        if value["source"] not in {"task", "inbox"}:
            _fail("unsupported pending message source")
        message["source"] = value["source"]
    return message


def _stream_usage(value: Any) -> dict[str, int]:
    _object(value, {"messages", "bytes"})
    return {
        "messages": _integer(value["messages"], "stream message usage"),
        "bytes": _integer(value["bytes"], "stream byte usage"),
    }


def validate_snapshot(value: Any) -> dict[str, Any]:
    """Validate a detached snapshot and reject duplicate ordering facts."""
    _object(value, {"schema", "now_ms", "journal_used_bytes", "active_execution_slots",
                    "rate_events", "pending_messages", "stream_usage"})
    if value["schema"] != SNAPSHOT_SCHEMA:
        _fail("unsupported admission snapshot schema")
    now_ms = _integer(value["now_ms"], "snapshot time")
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "now_ms": now_ms,
        "journal_used_bytes": _integer(value["journal_used_bytes"], "journal usage"),
        "active_execution_slots": _integer(value["active_execution_slots"], "active execution slots"),
        "rate_events": [_rate_event(item, now_ms=now_ms) for item in value["rate_events"]]
        if type(value["rate_events"]) is list else None,
        "pending_messages": [_pending_message(item) for item in value["pending_messages"]]
        if type(value["pending_messages"]) is list else None,
        "stream_usage": {},
    }
    if snapshot["rate_events"] is None or snapshot["pending_messages"] is None:
        _fail("rate events and pending messages must be lists")
    seen_messages: set[str] = set()
    seen_sequences: set[tuple[str, int]] = set()
    for message in snapshot["pending_messages"]:
        if message["message_id"] in seen_messages:
            _fail("duplicate pending message")
        key = (message["stream_id"], message["sequence"])
        if key in seen_sequences:
            _fail("duplicate stream sequence")
        seen_messages.add(message["message_id"])
        seen_sequences.add(key)
    if type(value["stream_usage"]) is not dict:
        _fail("stream usage must be an object")
    for stream_id, usage in value["stream_usage"].items():
        snapshot["stream_usage"][_id(stream_id, "stream id")] = _stream_usage(usage)
    _canonical_bytes(snapshot)
    return copy.deepcopy(snapshot)


def validate_request(value: Any) -> dict[str, Any]:
    """Validate one admission request without assigning policy defaults."""
    _object(value, {"schema", "selected_message_ids", "journal_bytes",
                    "execution_slots_requested", "kind"})
    if value["schema"] != REQUEST_SCHEMA:
        _fail("unsupported admission request schema")
    selected = value["selected_message_ids"]
    if type(selected) is not list:
        _fail("selected message ids must be a list")
    checked_selected = [_id(item, "selected message id") for item in selected]
    if len(set(checked_selected)) != len(checked_selected):
        _fail("duplicate selected message id")
    if type(value["kind"]) is not str or value["kind"] not in _REQUEST_KINDS:
        _fail("unsupported admission request kind")
    if value["kind"] == "control" and checked_selected:
        _fail("control request cannot select messages")
    checked = {
        "schema": REQUEST_SCHEMA,
        "selected_message_ids": checked_selected,
        "journal_bytes": _integer(value["journal_bytes"], "request journal bytes"),
        "execution_slots_requested": _integer(value["execution_slots_requested"], "requested execution slots"),
        "kind": value["kind"],
    }
    _canonical_bytes(checked)
    return copy.deepcopy(checked)


def _result(*, status: str, reason: str, selected: list[str], deferred: list[str],
            message_status: str, execution_status: str, journal_bytes: int = 0,
            rate_messages: int = 0, rate_bytes: int = 0) -> dict[str, Any]:
    result = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "reason": reason,
        "message_status": message_status,
        "execution_status": execution_status,
        "selected_message_ids": list(selected),
        "deferred_message_ids": list(deferred),
        "reserved": {"journal_bytes": journal_bytes,
                      "rate_messages": rate_messages, "rate_bytes": rate_bytes},
        "provider_contacted": False,
        "state_mutated": False,
    }
    _canonical_bytes(result)
    return result


def _blocked(reason: str) -> dict[str, Any]:
    if reason not in _REASONS - {"none"}:
        _fail("internal unsupported refusal reason")
    return _result(status="blocked", reason=reason, selected=[], deferred=[],
                   message_status="blocked", execution_status="not_run")


def _rate_totals(snapshot: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[int, int]:
    now_ms = snapshot["now_ms"]
    window = policy["rate_window_ms"]
    return tuple(sum(event[field] for event in snapshot["rate_events"]
                     if 0 <= now_ms - event["at_ms"] < window)
                 for field in ("messages", "bytes"))


def evaluate(policy: Mapping[str, Any], snapshot: Mapping[str, Any],
             request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic admission decision from detached facts only."""
    policy = validate_policy(policy)
    snapshot = validate_snapshot(snapshot)
    request = validate_request(request)
    if request["kind"] == "message" and not request["selected_message_ids"]:
        _fail("message request requires selected messages")
    if request["kind"] == "control" and request["execution_slots_requested"]:
        return _blocked("control_execution_slots_invalid")

    pending = {item["message_id"]: item for item in snapshot["pending_messages"]}
    selected = request["selected_message_ids"]
    if any(item not in pending for item in selected):
        _fail("selected message is absent from snapshot")
    if any(pending[item]["state"] not in {"queued", "held"}
           and not (pending[item]["state"] == "offered" and pending[item].get("source") == "inbox")
           for item in selected):
        _fail("selected message is not pending")
    selected_messages = [pending[item] for item in selected]
    selected_sources = {item.get("source", "task") for item in selected_messages}
    if "inbox" in selected_sources and (
            len(selected_messages) != 1 or request["execution_slots_requested"] != 0):
        _fail("inbox message requires one zero-slot request")
    selected_bytes = sum(item["bytes"] for item in selected_messages)
    if request["kind"] == "message" and request["journal_bytes"] < selected_bytes:
        _fail("request journal bytes understate selected messages")

    by_stream: dict[str, list[dict[str, Any]]] = {}
    for item in snapshot["pending_messages"]:
        if item["state"] in {"queued", "held"} or (
                item["state"] == "offered" and item.get("source") == "inbox"):
            by_stream.setdefault(item["stream_id"], []).append(item)
    for items in by_stream.values():
        items.sort(key=lambda item: item["sequence"])
    selected_set = set(selected)
    for stream_id, items in by_stream.items():
        stream_selected = [item for item in selected_messages if item["stream_id"] == stream_id]
        if not stream_selected:
            continue
        head = items[0]
        if head["bytes"] > policy["max_message_bytes"]:
            if head["message_id"] in selected_set:
                reason = "oversize_head_refused" if policy["oversize_head_action"] == "refuse" else "oversize_head_held"
            else:
                reason = "oversize_head_blocks_stream"
            return _blocked(reason)
        if head["message_id"] not in selected_set:
            return _blocked("stream_prefix_required")
        expected = head["sequence"]
        for item in stream_selected:
            if item["sequence"] != expected:
                return _blocked("stream_prefix_required")
            expected += 1

    rate_messages, rate_bytes = _rate_totals(snapshot, policy)
    if (rate_messages + len(selected) > policy["rate_limit"]
            or rate_bytes + selected_bytes > policy["rate_bytes_limit"]):
        return _blocked("rate_window_exhausted")

    for stream_id, items in by_stream.items():
        stream_selected = [item for item in selected_messages if item["stream_id"] == stream_id]
        if not stream_selected:
            continue
        limit = policy["stream_limits"].get(stream_id)
        if limit is None:
            _fail("selected stream has no explicit policy limit")
        usage = snapshot["stream_usage"].get(stream_id, {"messages": 0, "bytes": 0})
        if (usage["messages"] + len(stream_selected) > limit["message_limit"]
                or usage["bytes"] + sum(item["bytes"] for item in stream_selected) > limit["byte_limit"]):
            return _blocked("stream_budget_exhausted")

    total_journal = snapshot["journal_used_bytes"] + request["journal_bytes"]
    capacity = policy["journal_capacity_bytes"]
    recovery_floor = capacity - policy["recovery_reserve_bytes"]
    ordinary_floor = recovery_floor - policy["control_reserve_bytes"]
    if request["kind"] == "message":
        if total_journal > recovery_floor:
            return _blocked("recovery_reserve_protected")
        if total_journal > ordinary_floor:
            return _blocked("control_reserve_protected")
    elif total_journal > recovery_floor:
        return _blocked("recovery_reserve_protected")
    if total_journal > capacity:
        return _blocked("journal_capacity_exhausted")

    slots = request["execution_slots_requested"]
    if not slots:
        execution_status, deferred = "not_requested", []
    elif snapshot["active_execution_slots"] + slots <= policy["execution_slot_limit"]:
        execution_status, deferred = "available", []
    else:
        execution_status, deferred = "deferred", list(selected)
    return _result(status="accepted", reason="none", selected=selected, deferred=deferred,
                   message_status="accepted", execution_status=execution_status,
                   journal_bytes=request["journal_bytes"], rate_messages=rate_messages + len(selected),
                   rate_bytes=rate_bytes + selected_bytes)


def request_identity_sha256(request: Mapping[str, Any]) -> str:
    """Digest one validated budget request for a bound admission decision."""
    checked = validate_request(request)
    return hashlib.sha256(_canonical_bytes(checked)).hexdigest()


def facts_identity_sha256(policy: Mapping[str, Any], snapshot: Mapping[str, Any],
                          request: Mapping[str, Any]) -> str:
    """Digest the exact validated policy/snapshot/request fact set."""
    checked = {
        "policy": validate_policy(policy),
        "snapshot": validate_snapshot(snapshot),
        "request": validate_request(request),
    }
    return hashlib.sha256(_canonical_bytes(checked)).hexdigest()


def _validate_binding_digest(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        _fail(f"invalid {label}")
    return value


def bind_decision(*, policy: Mapping[str, Any], snapshot: Mapping[str, Any],
                  request: Mapping[str, Any], result: Mapping[str, Any],
                  journal_revision: int,
                  journal_prefix_sha256: str) -> dict[str, Any]:
    """Bind an observational result to exact facts and journal state.

    The binding is local metadata only.  It is not a capability and cannot be
    consumed unless the coordinator rechecks every fact and the current
    journal state at the live admission boundary.
    """
    if type(journal_revision) is not int or journal_revision < 0:
        _fail("invalid journal revision")
    _validate_binding_digest(journal_prefix_sha256, "journal prefix digest")
    expected = evaluate(policy, snapshot, request)
    expected_sha = result_sha256(expected)
    supplied_sha = result_sha256(result)
    if supplied_sha != expected_sha or dict(result) != expected:
        _fail("budget result does not match validated facts")
    checked_request = validate_request(request)
    return {
        "schema": DECISION_SCHEMA,
        "result": copy.deepcopy(expected),
        "result_sha256": expected_sha,
        "request_identity_sha256": request_identity_sha256(checked_request),
        "facts_identity_sha256": facts_identity_sha256(policy, snapshot, checked_request),
        "journal_revision": journal_revision,
        "journal_prefix_sha256": journal_prefix_sha256,
    }


def consume_bound_decision(decision: Mapping[str, Any], *,
                           policy: Mapping[str, Any], snapshot: Mapping[str, Any],
                           request: Mapping[str, Any], journal_revision: int,
                           journal_prefix_sha256: str) -> dict[str, Any]:
    """Revalidate and consume a decision only at its exact bound state."""
    _object(decision, {"schema", "result", "result_sha256", "request_identity_sha256",
                       "facts_identity_sha256", "journal_revision", "journal_prefix_sha256"},
            {"phase"})
    if decision["schema"] != DECISION_SCHEMA:
        _fail("unsupported admission decision schema")
    if "phase" in decision and decision["phase"] != "bound":
        _fail("unsupported admission decision phase")
    if type(journal_revision) is not int or journal_revision < 0:
        _fail("invalid current journal revision")
    _validate_binding_digest(decision["result_sha256"], "decision result digest")
    _validate_binding_digest(decision["request_identity_sha256"], "decision request digest")
    _validate_binding_digest(decision["facts_identity_sha256"], "decision facts digest")
    _validate_binding_digest(decision["journal_prefix_sha256"], "decision journal prefix digest")
    _validate_binding_digest(journal_prefix_sha256, "current journal prefix digest")
    if decision["journal_revision"] != journal_revision:
        _fail("budget decision journal revision is stale")
    if decision["journal_prefix_sha256"] != journal_prefix_sha256:
        _fail("budget decision journal prefix is stale")
    checked_request = validate_request(request)
    if decision["request_identity_sha256"] != request_identity_sha256(checked_request):
        _fail("budget decision request identity differs")
    if decision["facts_identity_sha256"] != facts_identity_sha256(policy, snapshot, checked_request):
        _fail("budget decision facts differ")
    current = evaluate(policy, snapshot, checked_request)
    current_sha = result_sha256(current)
    if decision["result_sha256"] != current_sha or decision["result"] != current:
        _fail("budget decision result differs")
    return copy.deepcopy(current)


def result_sha256(result: Mapping[str, Any]) -> str:
    """Return a stable digest for a validated decision, without provider data."""
    _object(result, {"schema", "status", "reason", "message_status", "execution_status",
                     "selected_message_ids", "deferred_message_ids", "reserved",
                     "provider_contacted", "state_mutated"})
    if result["schema"] != RESULT_SCHEMA:
        _fail("unsupported admission result schema")
    return hashlib.sha256(_canonical_bytes(result)).hexdigest()
