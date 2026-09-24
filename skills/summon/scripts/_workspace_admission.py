"""Pure admission classification for the preview workspace fabric.

This module describes what a future trusted admission boundary must reserve and
bind. It performs no I/O, authentication, journal mutation, scheduling,
worker launch, account selection, or provider contact. A successful return is
therefore a structural check only.

The coordinator and ``_workspace_state`` remain the authorities for task,
claim, grant, delivery, and hold state. This helper consumes their detached
snapshots; it does not define another record schema or another journal.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import _workspace_protocol as protocol
import _workspace_state as projection


ADMISSION_SCHEMA = "summon.workspace-admission/v1"
ATOMIC_TURN_EVENT = "workspace_turn_admitted"
ATOMIC_SEND_EVENT = "workspace_message_sent"
SUPERVISOR_SEND_EVENT = "workspace_supervisor_message_sent"
OPERATOR_SEND_EVENT = "workspace_operator_message_sent"
OPERATOR_SEND_SCHEMA = "summon.workspace.operator-send-base/v1"
OPERATOR_DISPOSITION_EVENT = "workspace_operator_disposition_recorded"
OPERATOR_DISPOSITION_SCHEMA = "summon.workspace.operator-disposition/v1"
OPERATOR_DISPOSITION_ACTION = "retain_held_context"
OPERATOR_DISPOSITION_PROOF_SCHEMA = "summon.workspace.operator-disposition-proof/v1"
OPERATOR_DISPOSITION_REASONS = frozenset({
    "awaiting_evidence", "awaiting_authorization", "operator_hold",
})
LINKED_REPLACEMENT_PROPOSAL_SCHEMA = "summon.workspace.linked-replacement-proposal/v1"
LINKED_REPLACEMENT_ACTION = "propose_linked_replacement"
LINKED_REPLACEMENT_PARENT_STATES = frozenset({
    "not_submitted", "held_for_recovery", "dead_lettered",
})
INBOX_EVENTS = frozenset({
    "workspace_supervisor_endpoint_defined", "workspace_supervisor_endpoint_activated",
    "workspace_supervisor_endpoint_revoked", "workspace_supervisor_endpoint_retired",
    SUPERVISOR_SEND_EVENT, "workspace_inbox_delivery_advanced", "workspace_inbox_delivery_linked",
})
SEND_SCHEMA = "summon.workspace.message-send/v1"
ECONOMICS_RESERVATION_SCHEMA = "summon.workspace.economics-reservation/v1"
ECONOMICS_FEATURE = "submission-accounting-v1"
MAX_SEND_CONTENT_BYTES = 4096
FEATURE_EVENT = "workspace_feature"
ORDINARY = "ordinary"

OBLIGATION_ACCEPTED = "accepted"
OBLIGATION_SUBMISSION_STARTED = "submission_started"
OBLIGATION_DISPOSITION = "disposition"
OBLIGATIONS = frozenset({
    OBLIGATION_ACCEPTED,
    OBLIGATION_SUBMISSION_STARTED,
    OBLIGATION_DISPOSITION,
})

SUPPORTED_EVENTS = INBOX_EVENTS | frozenset({
    FEATURE_EVENT, "workspace_goal_defined", "workspace_lane_defined",
    "workspace_evidence_registered", "workspace_assessed",
    "workspace_next_lane_selected", "workspace_message_admitted",
    "workspace_delivery_advanced", "workspace_delivery_linked",
    "workspace_effects_resolved",
    ATOMIC_TURN_EVENT, ATOMIC_SEND_EVENT, OPERATOR_SEND_EVENT,
    OPERATOR_DISPOSITION_EVENT,
})
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ATTEMPT = re.compile(r"[a-f0-9]{32}\Z")
_DELIVERY_TERMINAL = frozenset({
    "acknowledged", "not_submitted", "dead_lettered", "rejected",
    "expired", "cancelled",
})
_DELIVERY_PENDING = frozenset({"accepted", "queued"})
_DELIVERY_SELECTED = frozenset({"included_in_attempt", "submission_started"})
_DELIVERY_SETTLEMENT = frozenset({"submitted", "held_for_recovery"})
_UNCERTAINTY_FIELDS = frozenset({"contact", "spend", "cleanup"})
_TURN_PROOF_FIELDS = frozenset({"current_grant", "recipient_owner_fence",
                                "capacity_reservation", "physical_attempt_reservation",
                                "selection_record", "whole_message_fit"})
_UNCERTAINTY_VALUES = {
    "contact": frozenset({"not_attempted", "none", "occurred", "unknown"}),
    "spend": frozenset({"not_incurred", "incurred", "unknown"}),
    "cleanup": frozenset({"not_started", "complete", "unknown"}),
}


class WorkspaceAdmissionError(ValueError):
    """A malformed, stale, or unsafe pure admission claim."""


@dataclass(frozen=True)
class AdmissionObligation:
    """One bounded obligation associated with a candidate event."""

    kind: str
    subject_id: str
    settled: bool


def economics_reservation(value: Any, *, claim_id: str,
                          selection: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the bounded accounting reservation attached to one turn.

    This is a sole-writer admission fact, not provider spend or a retry grant.
    The reservation is bound to the fresh claim and exact selected-message
    identity so replay cannot move it to another physical attempt.
    """
    _object(value, {"schema", "feature", "status", "max_records",
                    "max_settlement_bytes", "claim_id", "selection_sha256",
                    "attempt_id", "payload_sha256", "material_sha256", "policy_sha256"})
    if (value["schema"] != ECONOMICS_RESERVATION_SCHEMA
            or value["feature"] != ECONOMICS_FEATURE
            or value["status"] != "reserved"):
        _fail("invalid economics reservation status")
    _id(claim_id, "claim id")
    if value["claim_id"] != claim_id:
        _fail("economics reservation claim differs")
    if type(value["attempt_id"]) is not str or not _ATTEMPT.fullmatch(value["attempt_id"]):
        _fail("invalid economics reservation attempt")
    _digest(value["payload_sha256"], "economics reservation payload digest")
    _digest(value["material_sha256"], "economics reservation material digest")
    _digest(value["policy_sha256"], "economics reservation policy digest")
    if (type(value["max_records"]) is not int
            or not 1 <= value["max_records"] <= 64
            or type(value["max_settlement_bytes"]) is not int
            or not protocol.MIN_ECONOMICS_SETTLEMENT_BYTES <= value["max_settlement_bytes"] <= 65536):
        _fail("invalid economics reservation bounds")
    try:
        selection_sha = hashlib.sha256(_canonical_event_bytes({
            "task_id": selection["task_id"],
            "attempt": selection["attempt"],
            "request_sha256": selection["request_sha256"],
            "context_sha256": selection["context_sha256"],
            "selected_message_ids": selection["selected_message_ids"],
        })).hexdigest()
    except (KeyError, TypeError, ValueError):
        _fail("economics reservation selection is invalid")
    if value["selection_sha256"] != selection_sha:
        _fail("economics reservation selection differs")
    if value["payload_sha256"] != selection["context_sha256"]:
        _fail("economics reservation payload differs")
    return copy.deepcopy(dict(value))


def _fail(message: str) -> None:
    raise WorkspaceAdmissionError(message)


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if type(value) is not dict:
        _fail("object required")
    keys = set(value)
    if not required <= keys or keys - required - optional:
        _fail("missing, unknown or invalid fields")


def _id(value: Any, label: str = "id") -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail(f"invalid {label}")
    return value


def _digest(value: Any, label: str = "digest") -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _fail(f"invalid {label}")
    return value


def _integer(value: Any, label: str, minimum: int = 1, maximum: int = protocol.MAX_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"invalid {label}")
    return value


def _bounded_text(value: Any, label: str, maximum: int = 4096) -> str:
    if type(value) is not str or not value or "\x00" in value:
        _fail(f"invalid {label}")
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail(f"{label} byte bound exceeded")
    except UnicodeError:
        _fail(f"invalid {label}")
    return value


def _canonical_event_bytes(event: Mapping[str, Any]) -> bytes:
    """Return the complete finite canonical event envelope.

    Validation must cover the wrapper, not only its payload.  In particular,
    NaN/Infinity and a huge unknown envelope must be refused before a caller
    can reserve or append a composite admission record.
    """
    try:
        encoded = json.dumps(event, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkspaceAdmissionError("event envelope is not finite canonical JSON") from exc
    if len(encoded) > protocol.MAX_RECORD_BYTES:
        _fail("complete event envelope exceeds byte bound")
    return encoded


def _event_envelope(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the common event envelope and reject unknown event kinds."""
    _object(event, {"event", "protocol", "workspace_id", "run_id",
                    "operation_key", "expected_revision", "payload"})
    if event["protocol"] not in {protocol.PROTOCOL, protocol.PROTOCOL_V2}:
        _fail("unsupported workspace protocol")
    _id(event["workspace_id"], "workspace id")
    _id(event["run_id"], "run id")
    _id(event["operation_key"], "operation key")
    _integer(event["expected_revision"], "expected revision", minimum=0)
    if event["event"] not in SUPPORTED_EVENTS:
        _fail("unsupported workspace event")
    if type(event["payload"]) is not dict:
        _fail("event payload must be an object")
    checked = copy.deepcopy(dict(event))
    _canonical_event_bytes(checked)
    return checked


def _canonical_message_delivery(payload: Mapping[str, Any], *, workspace_id: str | None = None,
                                 run_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    _object(payload, {"message", "delivery"})
    try:
        message = protocol.validate_record(payload["message"])
        delivery = protocol.validate_record(payload["delivery"])
    except protocol.WorkspaceProtocolError as exc:
        raise WorkspaceAdmissionError(str(exc)) from exc
    if message["kind"] != "message" or delivery["kind"] != "delivery":
        _fail("message admission requires canonical message and delivery records")
    if workspace_id is not None and any(item["workspace_id"] != workspace_id or item["run_id"] != run_id
                                       for item in (message, delivery)):
        _fail("message and delivery scope differs from event")
    return message, delivery


def _canonical_delivery_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    _object(payload, {"delivery", "evidence", "supported_ack_levels"})
    try:
        delivery = protocol.validate_record(payload["delivery"])
    except protocol.WorkspaceProtocolError as exc:
        raise WorkspaceAdmissionError(str(exc)) from exc
    if delivery["kind"] != "delivery" or type(payload["evidence"]) is not dict:
        _fail("delivery event requires canonical delivery and evidence")
    if delivery["workspace_id"] != event["workspace_id"] or delivery["run_id"] != event["run_id"]:
        _fail("delivery scope differs from event")
    if type(payload["supported_ack_levels"]) is not list:
        _fail("delivery event requires acknowledgement capabilities")
    return delivery


def _canonical_reference(value: Any, label: str) -> dict[str, str]:
    try:
        protocol._reference(value)
    except (protocol.WorkspaceProtocolError, AttributeError) as exc:
        raise WorkspaceAdmissionError(f"invalid {label}") from exc
    return {"id": value["id"], "sha256": value["sha256"]}


def _canonical_effect_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Structural candidate only; state/runtime must verify before and sources."""
    payload = event["payload"]
    _object(payload, {"delivery", "resolution_kind", "qualification", "evidence"})
    try:
        delivery = protocol.validate_record(payload["delivery"])
        protocol._enum(payload["resolution_kind"], {protocol.EFFECT_RESOLUTION_KIND})
        protocol._enum(payload["qualification"], {"simulated"})
        protocol._proof(payload["evidence"], set(protocol.EFFECT_EVIDENCE_CATEGORIES))
    except protocol.WorkspaceProtocolError as exc:
        raise WorkspaceAdmissionError(str(exc)) from exc
    if (delivery["kind"] != "delivery" or delivery["state"] != "acknowledged"
            or delivery["workspace_id"] != event["workspace_id"] or delivery["run_id"] != event["run_id"]
            or delivery["inherited_uncertainty"] or "parent_delivery_id" in delivery
            or delivery["certainty"] != {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}):
        _fail("invalid fixed-fixture effect-resolution result")
    if len({item["id"] for item in payload["evidence"].values()}) != len(protocol.EFFECT_EVIDENCE_CATEGORIES):
        _fail("fixed execution and cleanup require separate evidence")
    return delivery


def _turn_evidence(value: Any, grant_ref: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Validate one delivery's canonical proof map."""
    _object(value, set(_TURN_PROOF_FIELDS), {"durable_launch_intent"})
    checked = {}
    for key in _TURN_PROOF_FIELDS:
        checked[key] = _canonical_reference(value[key], key)
    if "durable_launch_intent" in value:
        checked["durable_launch_intent"] = _canonical_reference(
            value["durable_launch_intent"], "durable_launch_intent")
    if checked["current_grant"] != grant_ref:
        _fail("current grant proof does not match message grant")
    return checked


def _turn_evidence_payload(payload: Mapping[str, Any], selected_delivery_ids: list[str],
                           grant_ref: Mapping[str, str]) -> dict[str, dict[str, dict[str, str]]]:
    """Require a complete proof map for each selected delivery, including one.

    This unreleased draft has no shared-proof compatibility form. Persisted
    records are never rewritten or silently converted during replay.
    """
    per_delivery = payload.get("evidence_by_delivery")
    if "evidence" in payload or type(per_delivery) is not dict:
        _fail("per-delivery evidence is required")
    if set(per_delivery) != set(selected_delivery_ids):
        _fail("delivery evidence coverage mismatch")
    return {delivery_id: _turn_evidence(per_delivery[delivery_id], grant_ref)
            for delivery_id in selected_delivery_ids}


def send_request(value: Any) -> dict[str, Any]:
    """Canonical worker intent only; no sender or executable authority fields."""
    _object(value, {"schema", "operation_key", "destination_route", "observed_grant_revision", "content"},
            {"correlation_id", "reply_to", "causation_id"})
    if value["schema"] != SEND_SCHEMA:
        _fail("unsupported message send schema")
    for key in ("operation_key", "destination_route", "correlation_id", "reply_to", "causation_id"):
        if key in value:
            _id(value[key], key)
    _integer(value["observed_grant_revision"], "send grant revision")
    _bounded_text(value["content"], "message content", MAX_SEND_CONTENT_BYTES)
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def operator_request(value: Any) -> dict[str, Any]:
    """Bounded intent, not host authority. HTTP must also bound original bytes."""
    _object(value, {"operation_key", "target", "text"})
    if type(value["operation_key"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", value["operation_key"]):
        _fail("operator operation key must be 32 lowercase hexadecimal characters")
    _id(value["target"], "operator target")
    _bounded_text(value["text"], "operator text", 2048)
    if len(_canonical_event_bytes(value)) > 4096:
        _fail("operator request exceeds complete JSON byte bound")
    return copy.deepcopy(value)


def operator_resolution(value: Any) -> dict[str, Any]:
    """Validate supplied facts only; only installed host code supplies authority."""
    _object(value, {"operator_id", "scope"})
    _id(value["operator_id"], "local operator id")
    scope = value["scope"]
    _object(scope, {"reference", "revision", "expires_at_ms", "revoked", "operation",
                    "goal_id", "destination_task_id", "recipient", "delivery_grant_ref", "target"})
    for key in ("reference", "delivery_grant_ref"):
        _canonical_reference(scope[key], key)
    for key in ("goal_id", "destination_task_id", "target"):
        _id(scope[key], key)
    _integer(scope["revision"], "operator scope revision")
    _integer(scope["expires_at_ms"], "operator scope expiry")
    if scope["revoked"] is not False or scope["operation"] != "operator.message.send":
        _fail("operator scope is revoked or has the wrong operation")
    protocol._worker(scope["recipient"])
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def _operator_identity(workspace: Mapping[str, Any], resolved: dict, request: dict, content: dict) -> tuple[str, str]:
    principal = {key: workspace[key] for key in ("workspace_id", "run_id")}
    principal["operator_id"] = resolved["operator_id"]
    key = {"principal": principal, "operation_key": request["operation_key"]}
    operation = "operator-send-" + hashlib.sha256(b"summon.workspace.operator-send-operation/v1\0" + _canonical_event_bytes(key)).hexdigest()
    binding = {name: resolved["scope"][name] for name in ("destination_task_id", "recipient", "delivery_grant_ref")}
    intent = {**key, "target": request["target"], "binding": binding,
              "content_sha256": content["sha256"], "content_utf8_bytes": content["utf8_bytes"]}
    digest = hashlib.sha256(b"summon.workspace.operator-send-request/v1\0" + _canonical_event_bytes(intent)).hexdigest()
    return operation, digest


def operator_request_identity(workspace: dict, resolved: dict, request: dict) -> tuple[str, str]:
    request, resolved = operator_request(request), operator_resolution(resolved)
    raw = request["text"].encode("utf-8")
    return _operator_identity(workspace, resolved, request, {"sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)})


def canonical_operator_send_event(event: Mapping[str, Any]) -> dict[str, Any]:
    checked = _event_envelope(event)
    if checked["event"] != OPERATOR_SEND_EVENT:
        _fail("operator message composite required")
    payload = checked["payload"]
    _object(payload, {"message", "delivery", "request", "operator"})
    message, delivery = (protocol.validate_record(payload[key]) for key in ("message", "delivery"))
    resolved = operator_resolution(payload["operator"])
    scope, request = resolved["scope"], payload["request"]
    _object(request, {"operation_key", "target", "request_sha256"})
    # Validate the intent fields without inventing original text during replay.
    operator_request({"operation_key": request["operation_key"], "target": request["target"], "text": "x"})
    _digest(request["request_sha256"], "operator request digest")
    if message["kind"] != "operator_message" or delivery["kind"] != "delivery":
        _fail("operator send requires its explicit message variant and ordinary delivery")
    protocol.validate_delivery_binding(message, delivery)
    if (any(item[key] != checked[key] for item in (message, delivery) for key in ("workspace_id", "run_id"))
            or delivery["state"] != "accepted" or delivery["inherited_uncertainty"]
            or message["sender"] != {"kind": "local_operator", "operator_id": resolved["operator_id"]}
            or any(scope[key] != message[key] for key in ("goal_id", "destination_task_id", "recipient"))
            or scope["delivery_grant_ref"] != message["grant_ref"] or request["target"] != scope["target"]):
        _fail("operator sender, destination or initial binding differs")
    operation, digest = _operator_identity(checked, resolved, request, message["content"])
    suffix = operation[len("operator-send-"):]
    stream = "stream-" + hashlib.sha256(_canonical_event_bytes(protocol.message_stream_binding(message))).hexdigest()
    if (checked["operation_key"] != operation or request["request_sha256"] != digest
            or message["message_id"] != "operator-message-" + suffix
            or delivery["delivery_id"] != "operator-delivery-" + suffix or message["stream_id"] != stream):
        _fail("operator immutable operation identity differs")
    return checked


def operator_disposition_request(value: Any) -> dict[str, Any]:
    """Validate the versioned, finite retain-hold request payload.

    The references are registered evidence handles. Their bytes and current
    authorization are resolved by the trusted owner before append; this pure
    boundary fixes only the exact request shape and finite reason vocabulary.
    """
    _object(value, {"schema", "action", "delivery_id", "task_id",
                    "request_ref", "decision_ref", "reason"})
    if value["schema"] != OPERATOR_DISPOSITION_SCHEMA:
        _fail("unsupported operator disposition schema")
    if value["action"] != OPERATOR_DISPOSITION_ACTION:
        _fail("unsupported operator disposition action")
    _id(value["delivery_id"], "disposition delivery id")
    _id(value["task_id"], "disposition task id")
    request_ref = _canonical_reference(value["request_ref"], "disposition request")
    decision_ref = _canonical_reference(value["decision_ref"], "disposition decision")
    if request_ref == decision_ref:
        _fail("disposition request and decision must differ")
    if (type(value["reason"]) is not str
            or value["reason"] not in OPERATOR_DISPOSITION_REASONS):
        _fail("unsupported operator disposition reason")
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def canonical_operator_disposition_proof(value: Mapping[str, Any], *, role: str,
                                         operation_key: str, delivery_id: str,
                                         task_id: str, reason: str) -> dict[str, Any]:
    """Validate one trusted, role-specific retain proof source.

    These sources are resolved only by the owner runtime.  A registered
    ``category=event`` reference is not proof by itself: the private bytes must
    carry this exact schema, role and current request binding.  The public
    operator request never supplies or chooses these references.
    """
    if role not in {"request", "decision"}:
        _fail("unsupported operator disposition proof role")
    required = {"schema", "role", "operation_key", "action", "delivery_id",
                "task_id", "reason", "authority"}
    _object(value, required)
    if (value["schema"] != OPERATOR_DISPOSITION_PROOF_SCHEMA
            or value["role"] != role
            or value["operation_key"] != operation_key
            or value["action"] != OPERATOR_DISPOSITION_ACTION
            or value["delivery_id"] != delivery_id
            or value["task_id"] != task_id
            or value["reason"] != reason
            or value["authority"] != "installed_operator_policy"):
        _fail("operator disposition proof binding mismatch")
    _id(value["delivery_id"], "disposition proof delivery id")
    _id(value["task_id"], "disposition proof task id")
    if (type(value["operation_key"]) is not str
            or not re.fullmatch(r"[a-f0-9]{32}", value["operation_key"])
            or value["operation_key"] == "0" * 32
            or value["reason"] not in OPERATOR_DISPOSITION_REASONS):
        _fail("operator disposition proof request binding invalid")
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def canonical_operator_disposition_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one retain-hold intent without changing delivery state."""
    checked = _event_envelope(event)
    if checked["event"] != OPERATOR_DISPOSITION_EVENT:
        _fail("operator disposition event required")
    operator_disposition_request(checked["payload"])
    return checked


def linked_replacement_proposal(value: Any) -> dict[str, str]:
    """Validate the opaque public proposal for a linked replacement.

    This is deliberately only a shape check. The installed host must resolve
    the handles and prove current authority before it can append a child
    delivery event.
    """
    _object(value, {"schema", "action", "operation_key", "parent_target", "recipient_target"})
    if value["schema"] != LINKED_REPLACEMENT_PROPOSAL_SCHEMA:
        _fail("unsupported linked replacement proposal schema")
    if value["action"] != LINKED_REPLACEMENT_ACTION:
        _fail("unsupported linked replacement proposal action")
    if type(value["operation_key"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", value["operation_key"]):
        _fail("invalid linked replacement operation key")
    _id(value["parent_target"], "opaque parent target")
    _id(value["recipient_target"], "opaque recipient target")
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def validate_linked_replacement_authority(proposal: Mapping[str, Any], *,
                                           parent: Mapping[str, Any],
                                           message: Mapping[str, Any],
                                           authority: Mapping[str, Any]) -> dict[str, Any]:
    """Check host-resolved sources before a future linked-child append.

    The result is detached evidence for a later owner-fenced admission; this
    helper performs no journal write and never contacts a provider.
    """
    checked = linked_replacement_proposal(proposal)
    try:
        parent_record = protocol.validate_record(parent)
        message_record = protocol.validate_record(message)
    except (protocol.WorkspaceProtocolError, KeyError, TypeError) as exc:
        raise WorkspaceAdmissionError("linked replacement parent or message is not canonical") from exc
    if parent_record["kind"] != "delivery" or message_record["kind"] not in {"message", "operator_message"}:
        _fail("linked replacement requires a delivery and logical message")
    if parent_record["state"] not in LINKED_REPLACEMENT_PARENT_STATES:
        _fail("linked replacement parent state is ineligible")
    if any(parent_record[key] != message_record[key]
           for key in ("workspace_id", "run_id", "message_id")):
        _fail("linked replacement parent/message binding differs")
    _object(authority, {"parent_delivery_id", "parent_target", "recipient_target", "task_id",
                        "recipient", "generation", "now_ms", "sources"})
    _id(authority["parent_delivery_id"], "resolved parent delivery id")
    _id(authority["parent_target"], "resolved parent target")
    _id(authority["recipient_target"], "resolved recipient target")
    _id(authority["task_id"], "resolved destination task id")
    try:
        protocol._worker(authority["recipient"])
    except (protocol.WorkspaceProtocolError, KeyError, TypeError) as exc:
        raise WorkspaceAdmissionError("resolved recipient is not canonical") from exc
    _integer(authority["generation"], "resolved authority generation")
    _integer(authority["now_ms"], "authority observation time", minimum=0)
    if authority["parent_delivery_id"] != parent_record["delivery_id"]:
        _fail("resolved authority parent differs")
    if authority["parent_target"] != checked["parent_target"]:
        _fail("resolved parent target differs")
    if authority["recipient_target"] != checked["recipient_target"]:
        _fail("resolved recipient target differs")
    if message_record["destination_task_id"] != authority["task_id"]:
        _fail("resolved authority task differs")
    required = {"authenticated_disposition", "remaining_authority"}
    if parent_record["state"] in {"held_for_recovery", "dead_lettered"}:
        required |= {"fresh_attempt_grant", "physical_fence"}
    if authority["recipient"] != parent_record["recipient"]:
        required.add("recipient_transfer_authority")
    sources = authority["sources"]
    if type(sources) is not dict or set(sources) != required:
        _fail("missing or unknown linked replacement authority")
    source_fields = {"reference", "parent_delivery_id", "task_id", "recipient", "generation", "expires_at_ms", "revoked"}
    references = {}
    for role in sorted(required):
        source = sources[role]
        _object(source, source_fields)
        reference = _canonical_reference(source["reference"], role)
        _id(source["parent_delivery_id"], role + " parent delivery id")
        _id(source["task_id"], role + " task id")
        try:
            protocol._worker(source["recipient"])
        except (protocol.WorkspaceProtocolError, KeyError, TypeError) as exc:
            raise WorkspaceAdmissionError(f"{role} recipient is not canonical") from exc
        _integer(source["generation"], role + " generation")
        _integer(source["expires_at_ms"], role + " expiry")
        if source["revoked"] is not False:
            _fail(f"{role} authority is revoked")
        if source["parent_delivery_id"] != parent_record["delivery_id"]:
            _fail(f"{role} authority parent differs")
        if source["task_id"] != authority["task_id"] or source["recipient"] != authority["recipient"]:
            _fail(f"{role} authority scope differs")
        if source["generation"] != authority["generation"] or source["expires_at_ms"] <= authority["now_ms"]:
            _fail(f"{role} authority is stale")
        if reference["id"] in {item["id"] for item in references.values()}:
            _fail("linked replacement authority references must be distinct")
        references[role] = reference
    if ("fresh_attempt_grant" in references
            and references["fresh_attempt_grant"] == parent_record["grant_ref"]):
        _fail("linked replacement requires a fresh grant")
    return {"proposal": checked, "parent_delivery_id": parent_record["delivery_id"],
            "task_id": authority["task_id"], "recipient": copy.deepcopy(authority["recipient"]),
            "generation": authority["generation"], "evidence": references}


def build_operator_disposition_event(workspace: Mapping[str, Any], *, operation_key: str,
                                     delivery_id: str, task_id: str,
                                     request_ref: Mapping[str, Any],
                                     decision_ref: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Build a canonical retain-hold event for the current workspace revision."""
    common = {"protocol": protocol.PROTOCOL,
              "workspace_id": workspace["workspace_id"],
              "run_id": workspace["run_id"]}
    return canonical_operator_disposition_event({
        **common, "event": OPERATOR_DISPOSITION_EVENT,
        "operation_key": operation_key,
        "expected_revision": workspace["revision"],
        "payload": {
            "schema": OPERATOR_DISPOSITION_SCHEMA,
            "action": OPERATOR_DISPOSITION_ACTION,
            "delivery_id": delivery_id,
            "task_id": task_id,
            "request_ref": copy.deepcopy(dict(request_ref)),
            "decision_ref": copy.deepcopy(dict(decision_ref)),
            "reason": reason,
        },
    })


def build_operator_send_event(request: dict, resolved: dict, content: dict, workspace: dict) -> dict[str, Any]:
    request, resolved = operator_request(request), operator_resolution(resolved)
    raw = request["text"].encode("utf-8")
    _object(content, {"ref", "sha256", "utf8_bytes"})
    if (content["sha256"] != hashlib.sha256(raw).hexdigest() or type(content["utf8_bytes"]) is not int
            or content["utf8_bytes"] != len(raw)):
        _fail("operator prepared content differs from exact request")
    operation, digest = _operator_identity(workspace, resolved, request, content)
    common = {"protocol": protocol.PROTOCOL, **{key: workspace[key] for key in ("workspace_id", "run_id")}}
    scope = resolved["scope"]
    binding = {"sender": {"kind": "local_operator", "operator_id": resolved["operator_id"]},
               "recipient": copy.deepcopy(scope["recipient"]), "destination_task_id": scope["destination_task_id"]}
    stream = "stream-" + hashlib.sha256(_canonical_event_bytes(binding)).hexdigest()
    suffix = operation[len("operator-send-"):]
    message = {**common, "kind": "operator_message", "message_id": "operator-message-" + suffix,
               "goal_id": scope["goal_id"], **binding, "grant_ref": copy.deepcopy(scope["delivery_grant_ref"]),
               "stream_id": stream, "sequence": workspace["streams"].get(stream, {}).get("sequence", 0) + 1,
               "content": copy.deepcopy(content)}
    delivery = {**common, "kind": "delivery", "delivery_id": "operator-delivery-" + suffix,
                "message_id": message["message_id"], "recipient": copy.deepcopy(message["recipient"]),
                "grant_ref": copy.deepcopy(message["grant_ref"]), "state": "accepted", "reason": None,
                "selection": None, "certainty": {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"},
                "inherited_uncertainty": [], "ack_level": None}
    return canonical_operator_send_event({**common, "event": OPERATOR_SEND_EVENT, "operation_key": operation,
        "expected_revision": workspace["revision"], "payload": {"message": message, "delivery": delivery,
        "operator": resolved, "request": {"operation_key": request["operation_key"], "target": request["target"], "request_sha256": digest}}})


def validate_operator_scope(resolved: dict, request: dict, *, coordinator_state: dict, workspace_state: dict, now_ms: int) -> dict:
    """Current installed scope facts; eligibility is checked only for fresh sends."""
    resolved = operator_resolution(resolved)
    scope = resolved["scope"]
    _integer(now_ms, "operator observation time")
    if (coordinator_state.get("workspace") != workspace_state or coordinator_state.get("run_id") != workspace_state.get("run_id")
            or scope["target"] != request["target"] or scope["expires_at_ms"] <= now_ms
            or workspace_state.get("goal", {}).get("goal_id") != scope["goal_id"]):
        _fail("operator scope is stale, expired or outside this workspace")
    projection._goal_lane(workspace_state, scope["destination_task_id"])
    for key in ("reference", "delivery_grant_ref"):
        if projection._known_ref(workspace_state, scope[key], scope["destination_task_id"])["category"] != "grant":
            _fail("operator scope requires registered task-bound grant evidence")
    return resolved


def validate_operator_send(event: Mapping[str, Any], *, coordinator_state: dict, workspace_state: dict, now_ms: int) -> dict:
    checked = canonical_operator_send_event(event)
    payload = checked["payload"]
    resolved = validate_operator_scope(payload["operator"], payload["request"], coordinator_state=coordinator_state,
        workspace_state=workspace_state, now_ms=now_ms)
    scope = resolved["scope"]
    task = coordinator_state["tasks"].get(scope["destination_task_id"])
    if task is None or task.get("terminal") is not None:
        _fail("operator target task is not eligible")
    workers = coordinator_state.get("workers", {})
    active = [coordinator_state["claims"][key] for key in task["attempts"] if coordinator_state["claims"][key]["status"] == "active"]
    if active:
        if (len(active) != 1 or active[0]["lease_expires_at_ms"] <= now_ms
                or active[0]["worker_id"] not in workers
                or workers[active[0]["worker_id"]].get("worker_instance_id") != scope["recipient"]["instance_id"]):
            _fail("operator recipient conflicts with the current task claim")
    # A plan-created destination is intentionally dormant. Its grant and
    # scope are source-verified by the trusted host before this pure admission
    # check; absence from the worker registry is allowed only while the task
    # has no active claim. Registration is a separate adapter action.
    projection.apply_event(workspace_state, checked, _coordinator_view(coordinator_state, checked["run_id"], now_ms=now_ms))
    return checked


def operator_admission_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    checked = canonical_operator_send_event(event)
    digest = hashlib.sha256(b"summon.workspace.operator-message-admission/v1\0" + _canonical_event_bytes(checked)).hexdigest()
    message, delivery = (checked["payload"][key] for key in ("message", "delivery"))
    return {"reference": {"id": "admission-" + digest, "sha256": digest}, "category": "event",
        "task_id": message["destination_task_id"], "delivery_id": delivery["delivery_id"],
        "settlement_for": {"delivery_id": delivery["delivery_id"], "target_state": "queued", "role": "durable_admission"}}


def operator_send_response(event: Mapping[str, Any]) -> dict[str, Any]:
    checked = canonical_operator_send_event(event)
    message, delivery, request = (checked["payload"][key] for key in ("message", "delivery", "request"))
    return {"status": "queued", "operation_key": request["operation_key"], "request_sha256": request["request_sha256"],
        "message_id": message["message_id"], "delivery_id": delivery["delivery_id"], "stream_sequence": message["sequence"],
        "revision": checked["expected_revision"] + 1, "delivery_state": "queued", "execution_authorized": False}


def send_binding(value: Any) -> dict[str, Any]:
    """Exact transport binding shape; successful validation is not authentication."""
    _object(value, {"workspace_id", "run_id", "instance_id", "epoch", "task_id", "grant_id", "grant_sha256"})
    for key in ("workspace_id", "run_id", "instance_id", "task_id", "grant_id"):
        _id(value[key], key)
    _integer(value["epoch"], "worker epoch")
    _digest(value["grant_sha256"], "channel grant digest")
    return copy.deepcopy(value)


def _send_identity(binding: dict, request: dict, content: dict) -> tuple[str, str]:
    principal = {key: binding[key] for key in ("workspace_id", "run_id", "task_id", "instance_id", "epoch")}
    key_bytes = _canonical_event_bytes({"principal": principal, "operation_key": request["operation_key"]})
    operation = "send-" + hashlib.sha256(b"summon.workspace.send-operation/v1\0" + key_bytes).hexdigest()
    intent = {"principal": principal, "operation_key": request["operation_key"],
              "destination_route": request["destination_route"], "observed_grant_revision": request["observed_grant_revision"],
              "content_sha256": content["sha256"], "content_utf8_bytes": content["utf8_bytes"]}
    for key in ("correlation_id", "reply_to", "causation_id"):
        if key in request:
            intent[key] = request[key]
    digest = hashlib.sha256(b"summon.workspace.send-request/v1\0" + _canonical_event_bytes(intent)).hexdigest()
    return operation, digest


def send_request_identity(binding: dict, request: dict) -> tuple[str, str]:
    binding, request = send_binding(binding), send_request(request)
    raw = request["content"].encode("utf-8")
    return _send_identity(binding, request, {"sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)})


def canonical_send_event(event: Mapping[str, Any], *, _supervisor=False) -> dict[str, Any]:
    """Validate the composite's strict, non-authenticating structural bindings."""
    checked = _event_envelope(event)
    if checked["event"] != (SUPERVISOR_SEND_EVENT if _supervisor else ATOMIC_SEND_EVENT):
        _fail("atomic message send event required")
    payload = checked["payload"]
    _object(payload, {"message", "delivery", "request", "source", "send_scope"})
    if _supervisor:
        message, delivery = (protocol.validate_record(payload[key]) for key in ("message", "delivery"))
        if (message["kind"] != "supervisor_message" or delivery["kind"] != "inbox_delivery"
                or any(item[key] != checked[key] for item in (message, delivery) for key in ("workspace_id", "run_id"))
                or delivery["message_id"] != message["message_id"] or delivery["recipient"] != message["recipient"]
                or delivery["grant_ref"] != message["grant_ref"]):
            _fail("supervisor message/delivery binding differs")
    else:
        message, delivery = _canonical_message_delivery({"message": payload["message"], "delivery": payload["delivery"]},
                                                        workspace_id=checked["workspace_id"], run_id=checked["run_id"])
    if (delivery["state"] != "accepted" or "parent_delivery_id" in delivery or delivery.get("inherited_uncertainty")
            or delivery.get("inherited_exposure") is not None
            or not 1 <= message["content"]["utf8_bytes"] <= MAX_SEND_CONTENT_BYTES):
        _fail("atomic send requires a fresh bounded accepted delivery")
    source, request, scope = payload["source"], payload["request"], payload["send_scope"]
    _object(source, {"binding", "claim"})
    binding = send_binding(source["binding"])
    _object(request, {"operation_key", "request_sha256", "destination_route", "observed_grant_revision"},
            {"correlation_id", "reply_to", "causation_id"})
    for key in ("operation_key", "destination_route", "correlation_id", "reply_to", "causation_id"):
        if key in request:
            _id(request[key], key)
    _integer(request["observed_grant_revision"], "send grant revision")
    _digest(request["request_sha256"], "send request digest")
    grant_key = "receiver_grant_ref" if _supervisor else "delivery_grant_ref"
    destination_keys = set() if _supervisor else {"destination_task_id"}
    _object(scope, {"reference", "revision", "expires_at_ms", "revoked", "operation", "goal_id",
                    "source_task_id", "sender", "channel_grant_ref", "recipient", grant_key} | destination_keys)
    for key in ("reference", "channel_grant_ref", grant_key):
        _canonical_reference(scope[key], key)
    for key in {"goal_id", "source_task_id"} | destination_keys:
        _id(scope[key], key)
    _integer(scope["revision"], "send scope revision")
    _integer(scope["expires_at_ms"], "send scope expiry")
    protocol._worker(scope["sender"])
    (protocol.supervisor_recipient if _supervisor else protocol._worker)(scope["recipient"])
    if scope["revoked"] is not False or scope["operation"] != "message.send":
        _fail("send scope is revoked or has the wrong operation")
    claim = source["claim"]
    _object(claim, {"task_id", "claim_id", "worker_id", "attempt", "lease_generation", "lease_expires_at_ms",
                    "request_sha256", "status", "cancel_requested", "cancel_acknowledged", "renewals"})
    for key in ("task_id", "claim_id", "worker_id"):
        _id(claim[key], key)
    for key in ("attempt", "lease_generation", "lease_expires_at_ms"):
        _integer(claim[key], key, maximum=1024 if key == "attempt" else protocol.MAX_INT)
    _integer(claim["renewals"], "claim renewals", minimum=0)
    _digest(claim["request_sha256"], "source task digest")
    if claim["status"] != "active" or claim["cancel_requested"] is not False or claim["cancel_acknowledged"] is not False:
        _fail("source claim is not active")
    if (any(binding[key] != checked[key] for key in ("workspace_id", "run_id"))
            or binding["task_id"] != message["source_task_id"] or claim["task_id"] != binding["task_id"]
            or message["sender"] != {"instance_id": binding["instance_id"], "epoch": binding["epoch"]}
            or scope["sender"] != message["sender"] or scope["recipient"] != message["recipient"]
            or any(scope[key] != message[key] for key in {"goal_id", "source_task_id"} | destination_keys)
            or scope[grant_key] != message["grant_ref"]
            or scope["channel_grant_ref"] != {"id": binding["grant_id"], "sha256": binding["grant_sha256"]}
            or request["destination_route"] != scope["reference"]["id"]
            or request["observed_grant_revision"] != scope["revision"]):
        _fail("send source, route or grant binding differs")
    for key in ("correlation_id", "reply_to", "causation_id"):
        if request.get(key) != message.get(key):
            _fail("send correlation differs from message")
    operation, digest = _send_identity(binding, request, message["content"])
    if operation != checked["operation_key"] or digest != request["request_sha256"]:
        _fail("send operation or semantic digest differs")
    return checked


def canonical_supervisor_send_event(event):
    return canonical_send_event(event, _supervisor=True)


def send_admission_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    """The committed event is the source; candidate hashing proves no durability."""
    supervisor = event.get("event") == SUPERVISOR_SEND_EVENT
    checked = canonical_send_event(event, _supervisor=supervisor)
    domain = b"summon.workspace.supervisor-message-admission/v1\0" if supervisor else b"summon.workspace.message-admission/v1\0"
    digest = hashlib.sha256(domain + _canonical_event_bytes(checked)).hexdigest()
    message, delivery = checked["payload"]["message"], checked["payload"]["delivery"]
    return {"reference": {"id": "admission-" + digest, "sha256": digest}, "category": "event",
            **({"endpoint_id": message["recipient"]["endpoint_id"]} if supervisor else {"task_id": message["destination_task_id"]}),
            "delivery_id": delivery["delivery_id"],
            "settlement_for": {"delivery_id": delivery["delivery_id"], "target_state": "queued", "role": "durable_admission"}}


def send_response(event: Mapping[str, Any]) -> dict[str, Any]:
    checked = canonical_send_event(event, _supervisor=event.get("event") == SUPERVISOR_SEND_EVENT)
    message, delivery = checked["payload"]["message"], checked["payload"]["delivery"]
    return {"status": "queued", "operation_key": checked["payload"]["request"]["operation_key"],
            "request_sha256": checked["payload"]["request"]["request_sha256"], "message_id": message["message_id"],
            "delivery_id": delivery["delivery_id"], "stream_sequence": message["sequence"],
            "revision": checked["expected_revision"] + 1, "delivery_state": "queued", "execution_authorized": False}


def validate_worker_send(event: Mapping[str, Any], *, coordinator_state: Mapping[str, Any],
                         workspace_state: Mapping[str, Any], now_ms: int, _supervisor=False) -> dict[str, Any]:
    """Validate actual same-transaction snapshots; runtime still owns authentication."""
    checked = canonical_send_event(event, _supervisor=_supervisor)
    _integer(now_ms, "admission observation time")
    payload = checked["payload"]
    source, scope = payload["source"], payload["send_scope"]
    binding, claim = source["binding"], source["claim"]
    if coordinator_state.get("run_id") != checked["run_id"] or coordinator_state.get("workspace") != workspace_state:
        _fail("send needs authoritative same-state workspace")
    _validate_coordinator_claim(claim, coordinator_state)
    task = coordinator_state["tasks"][claim["task_id"]]
    if task["attempts"][-1] != claim["claim_id"] or claim["lease_expires_at_ms"] <= now_ms or scope["expires_at_ms"] <= now_ms:
        _fail("source claim or send grant expired or stale")
    worker = coordinator_state["workers"].get(claim["worker_id"])
    if (type(worker) is not dict or worker.get("worker_instance_id") != binding["instance_id"]
            or "epoch" in worker and worker["epoch"] != binding["epoch"]):
        _fail("source worker instance differs")
    references = [(scope["reference"], binding["task_id"]), (scope["channel_grant_ref"], binding["task_id"])]
    if _supervisor:
        endpoint = projection._endpoint(workspace_state, scope["recipient"]["endpoint_id"])
        require_current_consumer(endpoint, consumer_for_endpoint(endpoint), now_ms)
        if projection._known_ref(workspace_state, scope["receiver_grant_ref"], endpoint_id=endpoint["endpoint_id"])["category"] != "grant":
            _fail("receiver requires endpoint-scoped grant")
    else:
        references.append((scope["delivery_grant_ref"], scope["destination_task_id"]))
    for reference, task_id in references:
        if projection._known_ref(workspace_state, reference, task_id)["category"] != "grant":
            _fail("send authority requires a registered scoped grant")
    projection.apply_event(workspace_state, checked, _coordinator_view(coordinator_state, checked["run_id"], now_ms=now_ms))
    return checked


def build_send_event(request: dict, resolved: dict, content: dict, workspace: dict, *, _supervisor=False) -> dict[str, Any]:
    """Construct one host-bound candidate; this does not mint permission."""
    request = send_request(request)
    _object(resolved, {"binding", "claim", "send_scope"})
    binding = send_binding(resolved["binding"])
    _object(content, {"ref", "sha256", "utf8_bytes"})
    _id(content["ref"], "content ref")
    _digest(content["sha256"], "content digest")
    _integer(content["utf8_bytes"], "content bytes", maximum=MAX_SEND_CONTENT_BYTES)
    raw = request["content"].encode("utf-8")
    if content["sha256"] != hashlib.sha256(raw).hexdigest() or content["utf8_bytes"] != len(raw):
        _fail("prepared content differs from authenticated request")
    operation, digest = send_request_identity(binding, request)
    scope = resolved["send_scope"]
    if type(scope) is not dict:
        _fail("send scope required")
    common = {"protocol": protocol.PROTOCOL, "workspace_id": binding["workspace_id"], "run_id": binding["run_id"]}
    stream_binding = {"sender": {"instance_id": binding["instance_id"], "epoch": binding["epoch"]},
                      "recipient": copy.deepcopy(scope.get("recipient")), "source_task_id": binding["task_id"],
                      "destination_task_id": scope.get("destination_task_id")}
    if _supervisor:
        del stream_binding["destination_task_id"]
    stream_id = "stream-" + hashlib.sha256(_canonical_event_bytes(stream_binding)).hexdigest()
    message = {**common, "kind": "message", "message_id": "message-" + operation[5:],
               "goal_id": workspace["goal"]["goal_id"], **stream_binding,
               "grant_ref": copy.deepcopy(scope.get("delivery_grant_ref")), "stream_id": stream_id,
               "sequence": workspace["streams"].get(stream_id, {}).get("sequence", 0) + 1, "content": copy.deepcopy(content)}
    if _supervisor:
        message.update(kind="supervisor_message", grant_ref=copy.deepcopy(scope.get("receiver_grant_ref")))
    for key in ("correlation_id", "reply_to", "causation_id"):
        if key in request:
            message[key] = request[key]
    delivery = {**common, "kind": "delivery", "delivery_id": "delivery-" + operation[5:],
                "message_id": message["message_id"], "recipient": copy.deepcopy(message["recipient"]),
                "grant_ref": copy.deepcopy(message["grant_ref"]), "state": "accepted", "reason": None,
                "selection": None, "certainty": {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"},
                "inherited_uncertainty": [], "ack_level": None}
    if _supervisor:
        delivery = {**common, "kind": "inbox_delivery", "delivery_id": "delivery-" + operation[5:],
                    "message_id": message["message_id"], "recipient": copy.deepcopy(message["recipient"]),
                    "grant_ref": copy.deepcopy(message["grant_ref"]), "state": "accepted", "reason": None,
                    "offer": None, "receipt": None, "exposure": "not_exposed", "inherited_exposure": None,
                    "prior_offer_id": None, "possible_duplicate": False}
    canonical_request = {key: value for key, value in request.items() if key not in {"schema", "content"}}
    canonical_request["request_sha256"] = digest
    return canonical_send_event({**common, "event": SUPERVISOR_SEND_EVENT if _supervisor else ATOMIC_SEND_EVENT, "operation_key": operation,
                                 "expected_revision": workspace["revision"], "payload": {
                                     "message": message, "delivery": delivery, "request": canonical_request,
                                     "source": {"binding": binding, "claim": copy.deepcopy(resolved["claim"])},
                                     "send_scope": copy.deepcopy(scope)}}, _supervisor=_supervisor)


def build_supervisor_send_event(request, resolved, content, workspace):
    return build_send_event(request, resolved, content, workspace, _supervisor=True)


def validate_supervisor_send(event, *, coordinator_state, workspace_state, now_ms):
    return validate_worker_send(event, coordinator_state=coordinator_state,
                                workspace_state=workspace_state, now_ms=now_ms, _supervisor=True)


INBOX_ACTIONS = {
    "define": ("workspace_supervisor_endpoint_defined", set()),
    "activate": ("workspace_supervisor_endpoint_activated", {"receiving_grant_ref"}),
    "revoke": ("workspace_supervisor_endpoint_revoked", set()),
    "retire": ("workspace_supervisor_endpoint_retired", set()),
    "offer": ("workspace_inbox_delivery_advanced", {"delivery_id"}),
    "receipt": ("workspace_inbox_delivery_advanced", {"delivery_id", "offer_id"}),
    "hold": ("workspace_inbox_delivery_advanced", {"delivery_id", "reason"}),
    "dispose": ("workspace_inbox_delivery_advanced", {"delivery_id", "reason"}),
    "cancel": ("workspace_inbox_delivery_advanced", {"delivery_id", "reason"}),
    "expire": ("workspace_inbox_delivery_advanced", {"delivery_id", "reason"}),
    "link": ("workspace_inbox_delivery_linked", {"parent_delivery_id", "new_delivery_id"}),
}
INBOX_LIVE_ACTIONS = frozenset({"activate", "offer", "receipt", "link"})


def inbox_command(value):
    if type(value) is not dict or type(value.get("action")) is not str or value["action"] not in INBOX_ACTIONS:
        _fail("unsupported supervisor inbox command")
    extras = INBOX_ACTIONS[value["action"]][1]
    _object(value, {"operation_key", "endpoint_id", "expected_revision", "action"} | extras)
    _integer(value["expected_revision"], "expected workspace revision", minimum=0)
    for key in {"operation_key", "endpoint_id"} | extras - {"receiving_grant_ref"}:
        _id(value[key], key)
    if "receiving_grant_ref" in value:
        _canonical_reference(value["receiving_grant_ref"], "receiving grant")
    _canonical_event_bytes(value)
    return copy.deepcopy(value)


def inbox_command_identity(command):
    return hashlib.sha256(b"summon.workspace.inbox-command/v1\0" + _canonical_event_bytes(inbox_command(command))).hexdigest()


def inbox_resolution(value):
    """Strict detached callback facts; this function authenticates nothing."""
    _object(value, {"consumer", "evidence", "receiver_grant_ref", "lease_expires_at_ms", "offer_id"})
    _canonical_event_bytes(value)
    if value["consumer"] is None:
        if value["receiver_grant_ref"] is not None or value["lease_expires_at_ms"] is not None:
            _fail("administrative observation cannot assert a consumer lease")
    else:
        consumer = protocol.consumer_binding(value["consumer"])
        _canonical_reference(value["receiver_grant_ref"], "observed receiving grant")
        _integer(value["lease_expires_at_ms"], "observed consumer lease")
        if value["receiver_grant_ref"] != {"id": consumer["receiver_grant_id"], "sha256": consumer["receiver_grant_sha256"]}:
            _fail("observed receiving grant differs from consumer")
    if value["offer_id"] is not None:
        _id(value["offer_id"], "observed offer id")
    if type(value["evidence"]) is not dict:
        _fail("observed evidence map required")
    for role, reference in value["evidence"].items():
        if role not in protocol.INBOX_PROOF_CATEGORIES:
            _fail("unknown observed proof role")
        _canonical_reference(reference, role)
    return copy.deepcopy(value)


def consumer_for_endpoint(endpoint):
    if endpoint["owner"] is None:
        _fail("supervisor endpoint has no owner")
    return {key: endpoint[key] for key in ("workspace_id", "run_id", "endpoint_id")} | {
        "owner_instance_id": endpoint["owner"]["instance_id"], "epoch": endpoint["owner"]["epoch"],
        "receiver_grant_id": endpoint["receiver_grant_ref"]["id"],
        "receiver_grant_sha256": endpoint["receiver_grant_ref"]["sha256"]}


def require_current_consumer(endpoint, consumer, now_ms=None):
    protocol.consumer_binding(consumer)
    if endpoint["status"] != "active" or consumer != consumer_for_endpoint(endpoint):
        _fail("consumer is not the current active endpoint binding")
    if now_ms is not None:
        _integer(now_ms, "consumer observation time")
        if endpoint["lease_expires_at_ms"] <= now_ms:
            _fail("consumer lease expired")


def canonical_inbox_command_event(event):
    checked = _event_envelope(event)
    payload = checked["payload"]
    if checked["event"] not in INBOX_EVENTS - {SUPERVISOR_SEND_EVENT}:
        _fail("inbox command event required")
    endpoint_event = checked["event"].startswith("workspace_supervisor_endpoint_")
    record_key = "endpoint" if endpoint_event else "delivery"
    _object(payload, {"command", "consumer", "evidence", "request_sha256", record_key})
    command = inbox_command(payload["command"])
    if (INBOX_ACTIONS[command["action"]][0] != checked["event"]
            or command["expected_revision"] != checked["expected_revision"]
            or command["operation_key"] != checked["operation_key"]
            or inbox_command_identity(command) != payload["request_sha256"]):
        _fail("inbox command/event identity differs")
    record = protocol.validate_record(payload[record_key])
    if record["kind"] != ("supervisor_endpoint" if endpoint_event else "inbox_delivery"):
        _fail("inbox command record kind differs")
    endpoint_id = record["endpoint_id"] if endpoint_event else record["recipient"]["endpoint_id"]
    if (endpoint_id != command["endpoint_id"] or any(record[key] != checked[key] for key in ("workspace_id", "run_id"))):
        _fail("inbox command record scope differs")
    consumer = payload["consumer"]
    if consumer is not None:
        protocol.consumer_binding(consumer)
        if any(consumer[key] != checked[key] for key in ("workspace_id", "run_id")) or consumer["endpoint_id"] != endpoint_id:
            _fail("inbox consumer scope differs")
    elif command["action"] in INBOX_LIVE_ACTIONS:
        _fail("live consumer observation required")
    if type(payload["evidence"]) is not dict:
        _fail("inbox proof map required")
    for role, reference in payload["evidence"].items():
        if role not in protocol.INBOX_PROOF_CATEGORIES:
            _fail("unknown inbox evidence role")
        _canonical_reference(reference, role)
    return checked


def inbox_response(event):
    checked = canonical_inbox_command_event(event)
    payload, command = checked["payload"], checked["payload"]["command"]
    delivery = payload.get("delivery")
    return {"status": delivery["state"] if delivery else payload["endpoint"]["status"],
            "operation_key": command["operation_key"], "revision": checked["expected_revision"] + 1,
            "endpoint_id": command["endpoint_id"], "delivery_id": delivery["delivery_id"] if delivery else None,
            "offer_id": delivery["offer"]["offer_id"] if delivery and delivery["offer"] else None,
            "exposure": delivery["exposure"] if delivery else None,
            "possible_duplicate": delivery["possible_duplicate"] if delivery else False,
            "consumer_kind": protocol.INBOX_CONSUMER_KIND if delivery and delivery["receipt"] else None,
            "qualification": "simulated" if delivery and delivery["receipt"] else None}


def build_inbox_command_event(command, resolved, workspace):
    command = inbox_command(command)
    resolved = inbox_resolution(resolved)
    consumer = resolved["consumer"]
    if consumer is not None:
        protocol.consumer_binding(consumer)
    action, endpoint_id = command["action"], command["endpoint_id"]
    common = {"protocol": protocol.PROTOCOL, "workspace_id": workspace["workspace_id"], "run_id": workspace["run_id"]}
    payload = {"command": command, "consumer": copy.deepcopy(consumer), "evidence": copy.deepcopy(resolved["evidence"]),
               "request_sha256": inbox_command_identity(command)}
    if action == "define":
        record = {**common, "kind": "supervisor_endpoint", "endpoint_id": endpoint_id,
                  "goal_id": workspace["goal"]["goal_id"], "revision": 1, "owner": None,
                  "receiver_grant_ref": None, "recovery_fence_ref": None, "lease_expires_at_ms": 0, "status": "unbound"}
    elif action in {"activate", "revoke", "retire"}:
        record = copy.deepcopy(projection._endpoint(workspace, endpoint_id))
        record["revision"] += 1
        record["status"] = {"activate": "active", "revoke": "revoked", "retire": "retired"}[action]
        if action == "activate":
            if consumer is None:
                _fail("activation requires observed consumer")
            if command["receiving_grant_ref"] != resolved["receiver_grant_ref"]:
                _fail("activation receiving grant differs")
            record.update(owner={"instance_id": consumer["owner_instance_id"], "epoch": consumer["epoch"]},
                          receiver_grant_ref=copy.deepcopy(resolved["receiver_grant_ref"]),
                          recovery_fence_ref=copy.deepcopy(resolved["evidence"].get("recovery_fence")),
                          lease_expires_at_ms=resolved["lease_expires_at_ms"])
    else:
        before_id = command.get("delivery_id", command.get("parent_delivery_id"))
        record = copy.deepcopy(workspace.get("inbox_deliveries", {}).get(before_id))
        if record is None:
            _fail("unknown inbox delivery")
        if action == "link":
            endpoint = projection._endpoint(workspace, endpoint_id)
            unknown = record["exposure"] == "unknown" or record["inherited_exposure"] == "unknown"
            prior_offer = record["offer"]["offer_id"] if record["offer"] else record["prior_offer_id"]
            record.update(delivery_id=command["new_delivery_id"], parent_delivery_id=before_id,
                          recipient={"kind": "supervisor_inbox", "endpoint_id": endpoint_id,
                                     "owner_instance_id": consumer["owner_instance_id"], "epoch": consumer["epoch"]},
                          grant_ref=copy.deepcopy(endpoint["receiver_grant_ref"]), state="queued", reason=None,
                          offer=None, receipt=None, exposure="unknown" if unknown else "not_exposed",
                          inherited_exposure="unknown" if unknown else None, possible_duplicate=unknown,
                          prior_offer_id=prior_offer if unknown else None)
        else:
            record["state"] = {"offer": "offered", "receipt": "acknowledged", "hold": "held_for_recovery",
                               "dispose": "dead_lettered", "cancel": "cancelled", "expire": "expired"}[action]
            record["reason"] = command.get("reason")
            if action == "offer":
                message = workspace["messages"][record["message_id"]]
                record["offer"] = {"offer_id": resolved["offer_id"], "delivery_id": record["delivery_id"],
                                   "message_id": record["message_id"], "content_sha256": message["content"]["sha256"],
                                   "content_utf8_bytes": message["content"]["utf8_bytes"], "consumer": copy.deepcopy(consumer)}
                record["exposure"] = "unknown"
            elif action == "receipt":
                if record["offer"] is None or record["offer"]["offer_id"] != command["offer_id"]:
                    _fail("receipt offer differs")
                record["receipt"] = {"receipt_kind": "supervisor_context_received", "consumer_kind": protocol.INBOX_CONSUMER_KIND,
                                     "qualification": "simulated", **{key: record["offer"][key] for key in (
                                         "offer_id", "content_sha256", "content_utf8_bytes")}}
                record["exposure"] = "consumer_received"
    payload["endpoint" if action in {"define", "activate", "revoke", "retire"} else "delivery"] = record
    return canonical_inbox_command_event({**common, "event": INBOX_ACTIONS[action][0],
                                          "operation_key": command["operation_key"], "expected_revision": command["expected_revision"],
                                          "payload": payload})


def validate_inbox_command_event(event, *, coordinator_state, workspace_state, now_ms):
    checked = canonical_inbox_command_event(event)
    if coordinator_state.get("workspace") != workspace_state or coordinator_state.get("run_id") != checked["run_id"]:
        _fail("inbox command requires same-transaction workspace")
    payload, action = checked["payload"], checked["payload"]["command"]["action"]
    if action in INBOX_LIVE_ACTIONS:
        endpoint = payload["endpoint"] if action == "activate" else projection._endpoint(workspace_state, payload["command"]["endpoint_id"])
        require_current_consumer(endpoint, payload["consumer"], now_ms)
    projection.apply_event(workspace_state, checked, _coordinator_view(coordinator_state, checked["run_id"], now_ms=now_ms))
    return checked


def classify_event(event: Mapping[str, Any]) -> str:
    """Return the bounded obligation class for a canonical workspace event."""
    checked = _event_envelope(event)
    kind = checked["event"]
    if kind == OPERATOR_SEND_EVENT:
        canonical_operator_send_event(checked)
        return OBLIGATION_ACCEPTED
    if kind == OPERATOR_DISPOSITION_EVENT:
        canonical_operator_disposition_event(checked)
        return OBLIGATION_DISPOSITION
    if kind == SUPERVISOR_SEND_EVENT:
        canonical_supervisor_send_event(checked)
        return OBLIGATION_ACCEPTED
    if kind in INBOX_EVENTS:
        canonical_inbox_command_event(checked)
        return OBLIGATION_DISPOSITION
    if kind == ATOMIC_SEND_EVENT:
        canonical_send_event(checked)
        return OBLIGATION_ACCEPTED
    if kind == "workspace_effects_resolved":
        _canonical_effect_event(checked)
        return OBLIGATION_DISPOSITION
    if kind == "workspace_message_admitted":
        _canonical_message_delivery(checked["payload"], workspace_id=checked["workspace_id"],
                                    run_id=checked["run_id"])
        return OBLIGATION_ACCEPTED
    if kind == "workspace_delivery_linked":
        payload = checked["payload"]
        _object(payload, {"delivery", "evidence"})
        try:
            delivery = protocol.validate_record(payload["delivery"])
        except protocol.WorkspaceProtocolError as exc:
            raise WorkspaceAdmissionError(str(exc)) from exc
        if (delivery["kind"] != "delivery" or delivery["state"] != "queued"
                or delivery["workspace_id"] != checked["workspace_id"]
                or delivery["run_id"] != checked["run_id"]):
            _fail("linked delivery must be a canonical queued delivery")
        return OBLIGATION_DISPOSITION
    if kind == ATOMIC_TURN_EVENT:
        return OBLIGATION_SUBMISSION_STARTED
    if kind != "workspace_delivery_advanced":
        return ORDINARY
    delivery = _canonical_delivery_event(checked)
    state = delivery["state"]
    if state == OBLIGATION_SUBMISSION_STARTED:
        return OBLIGATION_SUBMISSION_STARTED
    if state in _DELIVERY_SETTLEMENT or state in _DELIVERY_TERMINAL:
        return OBLIGATION_DISPOSITION
    return ORDINARY


def _delivery_uncertain(delivery: Mapping[str, Any]) -> bool:
    certainty = delivery["certainty"]
    if any(certainty[field] == "unknown" for field in _UNCERTAINTY_FIELDS):
        return True
    return bool(delivery.get("inherited_uncertainty"))


def obligations_for_event(event: Mapping[str, Any], *, coordinator_state: Mapping[str, Any] | None = None,
                          workspace_state: Mapping[str, Any] | None = None,
                          grant_state: Mapping[str, Any] | None = None) -> tuple[AdmissionObligation, ...]:
    """Describe obligations created or settled by a canonical event."""
    checked = _event_envelope(event)
    kind = checked["event"]
    if kind == OPERATOR_SEND_EVENT:
        checked = canonical_operator_send_event(checked)
        return (AdmissionObligation(OBLIGATION_ACCEPTED, checked["payload"]["message"]["message_id"], True),
                AdmissionObligation(OBLIGATION_DISPOSITION, checked["payload"]["delivery"]["delivery_id"], False))
    if kind == OPERATOR_DISPOSITION_EVENT:
        # Retain records are audit intents only. They do not advance or settle
        # the held delivery's existing recovery obligation.
        canonical_operator_disposition_event(checked)
        return ()
    if kind == ATOMIC_SEND_EVENT:
        checked = canonical_send_event(checked)
        return (AdmissionObligation(OBLIGATION_ACCEPTED, checked["payload"]["message"]["message_id"], True),
                AdmissionObligation(OBLIGATION_DISPOSITION, checked["payload"]["delivery"]["delivery_id"], False))
    if kind == "workspace_effects_resolved":
        delivery = _canonical_effect_event(checked)
        return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery["delivery_id"], True),)
    if kind == "workspace_message_admitted":
        message, delivery = _canonical_message_delivery(checked["payload"],
                                                        workspace_id=checked["workspace_id"],
                                                        run_id=checked["run_id"])
        if delivery["state"] != "accepted":
            _fail("message admission must begin in accepted state")
        return (
            AdmissionObligation(OBLIGATION_ACCEPTED, message["message_id"], True),
            AdmissionObligation(OBLIGATION_DISPOSITION, delivery["delivery_id"], False),
        )
    if kind == "workspace_delivery_linked":
        payload = checked["payload"]
        _object(payload, {"delivery", "evidence"})
        try:
            delivery = protocol.validate_record(payload["delivery"])
        except protocol.WorkspaceProtocolError as exc:
            raise WorkspaceAdmissionError(str(exc)) from exc
        if (delivery["kind"] != "delivery" or delivery["state"] != "queued"
                or delivery["workspace_id"] != checked["workspace_id"]
                or delivery["run_id"] != checked["run_id"]):
            _fail("linked delivery must start queued")
        return (
            AdmissionObligation(OBLIGATION_ACCEPTED, delivery["delivery_id"], True),
            AdmissionObligation(OBLIGATION_DISPOSITION, delivery["delivery_id"], False),
        )
    if kind == "workspace_delivery_advanced":
        delivery = _canonical_delivery_event(checked)
        state = delivery["state"]
        delivery_id = delivery["delivery_id"]
        if state == "submission_started":
            return (
                AdmissionObligation(OBLIGATION_SUBMISSION_STARTED, delivery_id, False),
                AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),
            )
        if state in _DELIVERY_SETTLEMENT:
            return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),)
        if state in _DELIVERY_TERMINAL:
            return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id,
                                        not _delivery_uncertain(delivery)),)
        return ()
    if kind == ATOMIC_TURN_EVENT:
        checked = validate_atomic_claim_selection(
            checked, coordinator_state=coordinator_state,
            workspace_state=workspace_state, grant_state=grant_state)
        payload = checked["payload"]
        subject = payload["claim"]["claim_id"]
        obligations = [AdmissionObligation(OBLIGATION_SUBMISSION_STARTED, subject, False),
                       AdmissionObligation(OBLIGATION_DISPOSITION, subject, False)]
        obligations.extend(AdmissionObligation(OBLIGATION_DISPOSITION, item, False)
                           for item in payload.get("selected_delivery_ids", []))
        return tuple(obligations)
    return ()


def outstanding_obligations(delivery_state: str, delivery_id: str, *,
                            certainty: Mapping[str, Any] | None = None,
                            inherited_uncertainty: Sequence[str] = ()) -> tuple[AdmissionObligation, ...]:
    """Return obligations remaining after a delivery reaches a state.

    A state name alone never refunds uncertain contact, spend, or cleanup.
    Callers must provide canonical certainty before a terminal state settles.
    """
    delivery_id = _id(delivery_id, "delivery id")
    if delivery_state not in protocol.STATES:
        _fail("unsupported delivery state")
    if type(inherited_uncertainty) not in (list, tuple) or any(item not in _UNCERTAINTY_FIELDS for item in inherited_uncertainty):
        _fail("invalid inherited uncertainty")
    if len(set(inherited_uncertainty)) != len(inherited_uncertainty):
        _fail("duplicate inherited uncertainty")
    if delivery_state in _DELIVERY_PENDING:
        return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),)
    if delivery_state in _DELIVERY_SELECTED:
        return (
            AdmissionObligation(OBLIGATION_SUBMISSION_STARTED, delivery_id, False),
            AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),
        )
    if delivery_state in _DELIVERY_SETTLEMENT:
        return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),)
    uncertain = bool(inherited_uncertainty)
    if certainty is None:
        uncertain = True
    else:
        if type(certainty) is not dict or set(certainty) != _UNCERTAINTY_FIELDS:
            _fail("invalid certainty snapshot")
        for field in _UNCERTAINTY_FIELDS:
            if certainty[field] not in _UNCERTAINTY_VALUES[field]:
                _fail("invalid certainty value")
            uncertain = uncertain or certainty[field] == "unknown"
    if uncertain:
        return (AdmissionObligation(OBLIGATION_DISPOSITION, delivery_id, False),)
    return ()


def _canonical_hold(value: Any, *, goal_revision: int) -> dict[str, Any]:
    _object(value, {"assessment_id", "source_task_id", "affected_task_ids",
                    "evidence", "goal_revision", "reason"})
    assessment_id = _id(value["assessment_id"], "assessment id")
    source_task_id = _id(value["source_task_id"], "source task id")
    revision = _integer(value["goal_revision"], "hold goal revision")
    if revision != goal_revision:
        _fail("affected hold goal revision mismatch")
    affected = value["affected_task_ids"]
    if type(affected) is not list or not 1 <= len(affected) <= 16:
        _fail("invalid affected task set")
    affected = [_id(item, "affected task id") for item in affected]
    if len(set(affected)) != len(affected):
        _fail("duplicate affected task id")
    evidence = value["evidence"]
    if type(evidence) is not list or len(evidence) > 16:
        _fail("invalid hold evidence")
    evidence = [_canonical_reference(item, "hold evidence") for item in evidence]
    reason = _bounded_text(value["reason"], "hold reason")
    return {"assessment_id": assessment_id, "source_task_id": source_task_id,
            "affected_task_ids": affected, "evidence": evidence,
            "goal_revision": revision, "reason": reason}


def _hold_key(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _state_holds(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if type(state) is not dict or type(state.get("lanes")) is not dict:
        _fail("authoritative workspace state required")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for task_id in state["lanes"]:
            for item in projection.unresolved_holds(state, task_id):
                key = _hold_key(item)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
    except Exception as exc:
        raise WorkspaceAdmissionError("invalid authoritative workspace state") from exc
    return result


def validate_affected_holds(*, state: Mapping[str, Any] | None, target_task_id: str,
                            affected_task_ids: Sequence[str], holds: Sequence[Mapping[str, Any]],
                            goal_revision: int) -> tuple[dict[str, Any], ...]:
    """Validate the canonical current hold snapshot carried by a grant."""
    if state is None:
        _fail("authoritative workspace state required")
    target_task_id = _id(target_task_id, "grant target task id")
    goal_revision = _integer(goal_revision, "goal revision")
    if type(affected_task_ids) is not list or type(holds) is not list:
        _fail("affected holds must be explicit lists")
    if len(affected_task_ids) > 16 or len(holds) > 32:
        _fail("affected hold bound exceeded")
    affected = [_id(item, "affected task id") for item in affected_task_ids]
    if len(set(affected)) != len(affected):
        _fail("duplicate affected task id")
    try:
        projection.require_no_unresolved_hold(state, target_task_id)
    except Exception as exc:
        raise WorkspaceAdmissionError("grant target has an unresolved typed hold") from exc
    checked = tuple(_canonical_hold(item, goal_revision=goal_revision) for item in holds)
    union = {task_id for item in checked for task_id in item["affected_task_ids"]}
    if target_task_id in union or union != set(affected):
        _fail("affected task set and typed holds disagree")
    expected = [_canonical_hold(item, goal_revision=goal_revision)
                for item in _state_holds(state)]
    if sorted(map(_hold_key, checked)) != sorted(map(_hold_key, expected)):
        _fail("grant hold snapshot is stale or incomplete")
    return copy.deepcopy(checked)


def _validate_coordinator_claim(claim: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    _object(claim, {"task_id", "claim_id", "worker_id", "attempt", "lease_generation",
                    "lease_expires_at_ms", "request_sha256", "status",
                    "cancel_requested", "cancel_acknowledged", "renewals"})
    checked = copy.deepcopy(dict(claim))
    _id(checked["task_id"], "claim task id")
    _id(checked["claim_id"], "claim id")
    _id(checked["worker_id"], "claim worker id")
    _integer(checked["attempt"], "claim attempt", maximum=1024)
    _integer(checked["lease_generation"], "claim lease generation")
    _integer(checked["lease_expires_at_ms"], "claim lease expiry")
    _digest(checked["request_sha256"], "claim request digest")
    if checked["status"] != "active" or checked["cancel_requested"] is not False or checked["cancel_acknowledged"] is not False:
        _fail("claim is not currently admissible")
    if type(checked["renewals"]) is not int or checked["renewals"] < 0:
        _fail("invalid claim renewal count")
    if type(state) is not dict or type(state.get("claims")) is not dict or type(state.get("tasks")) is not dict:
        _fail("authoritative coordinator state required")
    current = state["claims"].get(checked["claim_id"])
    if type(current) is not dict:
        _fail("claim is not present in current coordinator state")
    if any(current.get(key) != checked[key] for key in checked):
        _fail("claim snapshot is stale or altered")
    task = state["tasks"].get(checked["task_id"])
    if type(task) is not dict or checked["claim_id"] not in task.get("attempts", []):
        _fail("claim is not bound to its current task")
    if task.get("terminal") is not None:
        _fail("terminal task cannot admit a turn")
    if task.get("request_sha256") != checked["request_sha256"]:
        _fail("claim request differs from current task")
    return checked


def _prospective_claim(claim: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a claim against pre-claim state and return a detached state.

    A fresh atomic turn intentionally arrives before ``claim_granted`` exists.
    This helper derives the exact post-claim snapshot without mutating the
    authoritative input, so selection/proof validation can run against the
    state that the one composite journal record will create.
    """
    if type(state) is not dict or type(state.get("claims")) is not dict or type(state.get("tasks")) is not dict:
        _fail("authoritative coordinator state required")
    checked = copy.deepcopy(dict(claim))
    _object(checked, {"task_id", "claim_id", "worker_id", "attempt", "lease_generation",
                      "lease_expires_at_ms", "request_sha256", "status",
                      "cancel_requested", "cancel_acknowledged", "renewals"})
    _id(checked["task_id"], "claim task id")
    _id(checked["claim_id"], "claim id")
    _id(checked["worker_id"], "claim worker id")
    _integer(checked["attempt"], "claim attempt", maximum=1024)
    _integer(checked["lease_generation"], "claim lease generation")
    _integer(checked["lease_expires_at_ms"], "claim lease expiry")
    _digest(checked["request_sha256"], "claim request digest")
    if checked["status"] != "active" or checked["cancel_requested"] is not False or checked["cancel_acknowledged"] is not False:
        _fail("claim is not currently admissible")
    if type(checked["renewals"]) is not int or checked["renewals"] != 0:
        _fail("fresh claim must have zero renewals")
    worker = state["workers"].get(checked["worker_id"]) if type(state.get("workers")) is dict else None
    if type(worker) is not dict:
        _fail("claim worker is not registered")
    task = state["tasks"].get(checked["task_id"])
    if type(task) is not dict:
        _fail("claim references unknown task")
    existing = state["claims"].get(checked["claim_id"])
    if existing is not None:
        return _validate_coordinator_claim(checked, state), copy.deepcopy(dict(state))
    attempts = task.get("attempts")
    if type(attempts) is not list:
        _fail("task attempt history is invalid")
    active = [state["claims"].get(item) for item in attempts
              if type(item) is str and type(state["claims"].get(item)) is dict
              and state["claims"][item].get("status") == "active"]
    if active:
        _fail("fresh claim conflicts with an active claim")
    if task.get("terminal") in {"completed", "cancelled", "blocked"}:
        _fail("terminal task cannot admit a claim")
    if task.get("terminal") == "indeterminate" and not task.get("retry_authorized"):
        _fail("uncertain spend requires explicit retry authorization")
    expected_attempt = len(attempts) + 1
    if checked["attempt"] != expected_attempt:
        _fail("claim attempt does not match the prospective task state")
    previous_generations = [state["claims"][item]["lease_generation"] for item in attempts]
    expected_generation = max(previous_generations) + 1 if previous_generations else 1
    if checked["lease_generation"] != expected_generation:
        _fail("claim lease generation does not match the prospective task state")
    if checked["request_sha256"] != task.get("request_sha256"):
        _fail("claim request differs from current task")
    prospective = copy.deepcopy(dict(state))
    prospective.setdefault("claims", {})[checked["claim_id"]] = copy.deepcopy(checked)
    prospective["tasks"][checked["task_id"]]["attempts"] = list(attempts) + [checked["claim_id"]]
    prospective["tasks"][checked["task_id"]]["retry_authorized"] = False
    return checked, prospective


def _coordinator_view(state: Mapping[str, Any], run_id: str, *, now_ms: int | None = None) -> dict[str, Any]:
    """Project a detached full coordinator snapshot into workspace view shape."""
    tasks = []
    for task in state.get("tasks", {}).values():
        claims = [state["claims"][claim_id] for claim_id in task.get("attempts", [])]
        latest = claims[-1] if claims else None
        active = latest if latest and latest.get("status") == "active" else None
        status = task.get("terminal") or (
            "expired" if active and now_ms is not None and active["lease_expires_at_ms"] <= now_ms
            else "claimed" if active else "pending")
        tasks.append({
            "task_id": task["task_id"],
            "request_sha256": task["request_sha256"],
            "status": status,
            "attempts": len(claims),
            "active_claim": ({
                "claim_id": active["claim_id"],
                "worker_id": active["worker_id"],
                "lease_generation": active["lease_generation"],
                "lease_expires_at_ms": active["lease_expires_at_ms"],
                "cancel_requested": active["cancel_requested"],
            } if active else None),
        })
    return {"run_id": run_id, "tasks": tasks}


def validate_atomic_claim_selection(event: Mapping[str, Any], *,
                                    coordinator_state: Mapping[str, Any] | None,
                                    workspace_state: Mapping[str, Any] | None,
                                    grant_state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate one atomic claim+selection event against current snapshots."""
    checked = _event_envelope(event)
    if checked["event"] != ATOMIC_TURN_EVENT:
        _fail("atomic turn event required")
    payload = checked["payload"]
    _object(payload, {"claim", "selection", "recipient", "grant_ref",
                      "affected_task_ids", "holds", "goal_revision", "selected_delivery_ids",
                      "supported_ack_levels", "evidence_by_delivery"},
            {"budget_admission", "economics_reservation"})
    if "budget_admission" in payload and type(payload["budget_admission"]) is not dict:
        _fail("invalid atomic budget admission")
    if "economics_reservation" in payload and type(payload["economics_reservation"]) is not dict:
        _fail("invalid atomic economics reservation")
    if coordinator_state is None or workspace_state is None or grant_state is None:
        _fail("current coordinator, workspace, and grant state are required")
    goal_revision = _integer(payload["goal_revision"], "goal revision")
    claim, prospective_coordinator = _prospective_claim(payload["claim"], coordinator_state)
    selection = payload["selection"]
    _object(selection, {"task_id", "claim_id", "attempt", "owner_generation",
                        "request_sha256", "context_sha256", "selected_message_ids"}, {"turn_id"})
    try:
        protocol._attempt(selection)
        recipient = dict(payload["recipient"])
        protocol._worker(recipient)
    except (protocol.WorkspaceProtocolError, AttributeError) as exc:
        raise WorkspaceAdmissionError("invalid canonical selection or recipient") from exc
    grant_ref = _canonical_reference(payload["grant_ref"], "grant reference")
    for key in ("task_id", "claim_id", "attempt", "request_sha256"):
        if selection[key] != claim[key]:
            _fail("claim and selection fence mismatch")
    if selection["owner_generation"] != claim["lease_generation"]:
        _fail("selection owner generation differs from claim lease")
    if type(payload["supported_ack_levels"]) is not list:
        _fail("supported acknowledgement levels must be a list")
    if any(type(level) is not str or level not in protocol.ACK_LEVELS
           for level in payload["supported_ack_levels"]):
        _fail("unsupported acknowledgement level")
    workers = prospective_coordinator.get("workers", {})
    worker = workers.get(claim["worker_id"]) if type(workers) is dict else None
    if type(worker) is not dict or worker.get("worker_instance_id") != recipient["instance_id"]:
        _fail("recipient is not the current claimed worker")
    if "epoch" in worker and worker["epoch"] != recipient["epoch"]:
        _fail("recipient epoch is stale")
    _object(grant_state, {"grant_ref", "task_id", "goal_revision", "recipient", "revoked"})
    if (_canonical_reference(grant_state["grant_ref"], "current grant state") != grant_ref
            or grant_state["task_id"] != claim["task_id"]
            or grant_state["goal_revision"] != goal_revision
            or grant_state["revoked"] is not False):
        _fail("grant is stale, revoked, or bound to another task")
    try:
        protocol._worker(grant_state["recipient"])
    except (protocol.WorkspaceProtocolError, AttributeError) as exc:
        raise WorkspaceAdmissionError("invalid grant recipient") from exc
    if grant_state["recipient"] != recipient:
        _fail("grant recipient or epoch differs")
    holds = validate_affected_holds(
        state=workspace_state, target_task_id=claim["task_id"],
        affected_task_ids=payload["affected_task_ids"], holds=payload["holds"],
        goal_revision=goal_revision)
    if type(workspace_state) is not dict:
        _fail("workspace message/delivery state required")
    selected = selection["selected_message_ids"]
    try:
        projection.validate_turn_selection(
            workspace_state,
            _coordinator_view(prospective_coordinator, checked["run_id"]),
            selection,
            recipient,
        )
    except projection.WorkspaceStateError as exc:
        raise WorkspaceAdmissionError(str(exc)) from exc
    selected_delivery_ids = payload["selected_delivery_ids"]
    if (type(selected_delivery_ids) is not list
            or not 1 <= len(selected_delivery_ids) <= 8):
        _fail("invalid selected delivery set")
    selected_delivery_ids = [_id(item, "selected delivery id") for item in selected_delivery_ids]
    if len(set(selected_delivery_ids)) != len(selected_delivery_ids):
        _fail("invalid selected delivery set")
    deliveries = workspace_state.get("deliveries", {})
    if type(deliveries) is not dict:
        _fail("workspace delivery state required")
    selected_messages = set(selected)
    selected_deliveries = []
    for delivery_id in selected_delivery_ids:
        item = deliveries.get(delivery_id)
        if (type(item) is not dict or item.get("message_id") not in selected_messages
                or item.get("recipient") != recipient
                or item.get("state") not in {"queued", "included_in_attempt"}):
            _fail("selected delivery is not an eligible recipient-bound delivery")
        selected_deliveries.append(item)
    if {item["message_id"] for item in selected_deliveries} != selected_messages:
        _fail("selected delivery set does not cover selected messages")
    evidence_by_delivery = _turn_evidence_payload(
        payload, selected_delivery_ids, grant_ref)
    try:
        for delivery_id, evidence in evidence_by_delivery.items():
            if deliveries[delivery_id]["grant_ref"] != grant_ref:
                _fail("selected delivery grant differs")
            projection._delivery_refs(workspace_state, evidence, selection["task_id"], delivery_id)
    except projection.WorkspaceStateError as exc:
        raise WorkspaceAdmissionError(str(exc)) from exc
    result = copy.deepcopy(checked)
    result["payload"]["holds"] = list(holds)
    result["payload"]["selected_delivery_ids"] = selected_delivery_ids
    result["payload"]["evidence_by_delivery"] = evidence_by_delivery
    if "economics_reservation" in payload:
        result["payload"]["economics_reservation"] = economics_reservation(
            payload["economics_reservation"],
            claim_id=claim["claim_id"], selection=selection)
    return result


def validate_grant_obligations(grant: Mapping[str, Any], *, state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the explicit grant hold snapshot against current workspace state."""
    _object(grant, {"grant_id", "goal_id", "goal_revision", "task_id",
                    "affected_task_ids", "holds"})
    _id(grant["grant_id"], "grant id")
    _id(grant["goal_id"], "goal id")
    _integer(grant["goal_revision"], "grant goal revision")
    _id(grant["task_id"], "grant task id")
    checked = validate_affected_holds(
        state=state, target_task_id=grant["task_id"],
        affected_task_ids=grant["affected_task_ids"], holds=grant["holds"],
        goal_revision=grant["goal_revision"])
    result = copy.deepcopy(dict(grant))
    result["holds"] = list(checked)
    return result
