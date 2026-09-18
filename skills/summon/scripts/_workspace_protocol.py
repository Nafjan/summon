"""Preliminary pure workspace contracts; no authentication or execution.

Passing validation establishes shape and internal consistency of supplied claims,
never their truth, durability, provenance, authority or provider certification.
The future coordinator/adapter must independently establish those facts before
using a transition. Text is context only; assessments cannot grant execution.
Existing swarm/v1 and conversation schemas are deliberately untouched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

import _context_policy

PROTOCOL = "summon.workspace/v1"
PROTOCOL_V2 = "summon.workspace/v2"
MAX_RECORD_BYTES = 48 * 1024
MAX_CONTENT_BYTES = 4096
# A settlement record contains the complete attempt accounting envelope, not
# just a digest.  Reserve enough bounded space before launch so the sole
# writer can always append the terminal outcome without discovering that the
# journal fence was undersized.
MIN_ECONOMICS_SETTLEMENT_BYTES = 4096
MAX_INT = 2**63 - 1
ACK_LEVELS = frozenset({"adapter_received", "recipient_received"})
INBOX_CONSUMER_KIND = "owned_fixed_supervisor_consumer/v1"
INBOX_STATES = frozenset({"accepted", "queued", "offered", "acknowledged", "held_for_recovery", "cancelled", "expired", "dead_lettered"})
INBOX_EDGES = frozenset({("accepted", "queued"), ("queued", "offered"), ("offered", "acknowledged"),
                        ("queued", "held_for_recovery"), ("offered", "held_for_recovery"),
                        ("queued", "cancelled"), ("queued", "expired"), ("held_for_recovery", "dead_lettered")})
INBOX_PROOF_CATEGORIES = {"durable_admission": "event", "receiver_grant": "grant", "offer_intent": "event",
    "consumer_receipt": "adapter_receipt", "hold_observation": "observation", "authorized_cancellation": "grant",
    "expiry_observation": "observation", "authenticated_disposition": "event", "fresh_receiving_grant": "grant",
    "recovery_fence": "fence", "owner_fence": "fence", "owner_revocation": "fence"}
INBOX_ROLES = {"queued": frozenset({"durable_admission"}), "offered": frozenset({"receiver_grant", "offer_intent"}),
    "acknowledged": frozenset({"consumer_receipt"}), "held_for_recovery": frozenset({"hold_observation"}),
    "cancelled": frozenset({"authorized_cancellation"}), "expired": frozenset({"expiry_observation"}),
    "dead_lettered": frozenset({"authenticated_disposition"})}
EFFECT_RESOLUTION_KIND = "fixed_worker_fixture/v1"
EFFECT_OBSERVATION_SCHEMA = "summon.workspace.effects-observation/v1"
EFFECT_EVIDENCE_CATEGORIES = {
    "fixed_execution_scope": "observation", "owned_child_cleanup": "fence",
}
STATES = frozenset({"accepted", "queued", "included_in_attempt",
                    "submission_started", "submitted", "acknowledged",
                    "not_submitted", "held_for_recovery", "rejected",
                    "expired", "cancelled", "dead_lettered"})
HOLD_REASONS = frozenset({"native_turn_collision", "recipient_drift",
                          "grant_revoked", "contact_uncertain",
                          "cleanup_uncertain", "incompatible_fork",
                          "content_missing", "content_mismatch", "content_unavailable"})
EDGES = frozenset({
    ("accepted", "queued"), ("queued", "included_in_attempt"),
    ("included_in_attempt", "submission_started"),
    ("submission_started", "submitted"), ("submitted", "acknowledged"),
    ("included_in_attempt", "not_submitted"),
    ("submission_started", "not_submitted"),
    *((state, "held_for_recovery") for state in
      ("queued", "included_in_attempt", "submission_started", "submitted")),
    *(("queued", state) for state in ("rejected", "expired", "cancelled")),
    ("held_for_recovery", "dead_lettered"),
})
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class WorkspaceProtocolError(ValueError):
    """Malformed claims, unsupported schema, or forbidden lifecycle change."""


def _fail(label: str) -> None:
    raise WorkspaceProtocolError(label)


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        _fail("missing, unknown or invalid object fields")


def _id(value: Any) -> None:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("invalid opaque id")


def _digest(value: Any) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _fail("invalid sha256")


def _integer(value: Any, minimum: int = 1, maximum: int = MAX_INT) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid bounded integer")


def _enum(value: Any, choices: Any) -> None:
    if type(value) is not str or value not in choices:
        _fail("unsupported enum")


def _text(value: Any, maximum: int = 4096) -> None:
    if type(value) is not str or not value or "\x00" in value:
        _fail("invalid text")
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail("text byte bound exceeded")
    except UnicodeError:
        _fail("invalid unicode")


def _list(value: Any, validate, maximum: int = 32, minimum: int = 1) -> None:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        _fail("invalid bounded list")
    for item in value:
        validate(item)
    if len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
        _fail("duplicate list item")


def _reference(value: Any) -> None:
    _object(value, {"id", "sha256"})
    _id(value["id"])
    _digest(value["sha256"])


def _worker(value: Any) -> None:
    _object(value, {"instance_id", "epoch"})
    _id(value["instance_id"])
    _integer(value["epoch"])


def _attempt(value: Any) -> None:
    _object(value, {"task_id", "claim_id", "attempt", "owner_generation",
                    "request_sha256", "context_sha256", "selected_message_ids"}, {"turn_id"})
    for key in ("task_id", "claim_id"):
        _id(value[key])
    if "turn_id" in value:
        _id(value["turn_id"])
    _integer(value["attempt"], maximum=1024)
    _integer(value["owner_generation"])
    for key in ("request_sha256", "context_sha256"):
        _digest(value[key])
    _list(value["selected_message_ids"], _id, maximum=8)


def _certainty(value: Any) -> None:
    _object(value, {"contact", "spend", "cleanup"})
    _enum(value["contact"], {"not_attempted", "none", "occurred", "unknown"})
    _enum(value["spend"], {"not_incurred", "incurred", "unknown"})
    _enum(value["cleanup"], {"not_started", "complete", "unknown"})


def _record(value: Any, kind: str, fields: set[str], optional: set[str] = frozenset(),
            *, protocol: str = PROTOCOL) -> None:
    _object(value, fields | {"protocol", "kind", "workspace_id", "run_id"}, optional)
    if value["protocol"] != protocol or value["kind"] != kind:
        _fail("unsupported workspace schema")
    _id(value["workspace_id"])
    _id(value["run_id"])


def _goal(value: dict, *, protocol: str = PROTOCOL) -> None:
    _record(value, "goal", {"goal_id", "revision", "objective", "criteria",
                             "constraints", "active_priority", "unresolved_decisions"},
            protocol=protocol)
    _id(value["goal_id"])
    _integer(value["revision"])
    _text(value["objective"])
    _id(value["active_priority"])
    def criterion(item):
        _object(item, {"criterion_id", "description", "evidence_requirement"})
        _id(item["criterion_id"])
        _text(item["description"])
        _text(item["evidence_requirement"])
    _list(value["criteria"], criterion, maximum=16)
    if len({c["criterion_id"] for c in value["criteria"]}) != len(value["criteria"]):
        _fail("duplicate criterion id")
    for key in ("constraints", "unresolved_decisions"):
        _list(value[key], _text, maximum=16, minimum=0)


def _lane(value: dict, *, protocol: str = PROTOCOL) -> None:
    _record(value, "lane", {"goal_id", "goal_revision", "task_id", "lane_id",
                             "request_sha256", "role", "outcome", "scope",
                             "authority_ref", "budget", "criterion_ids",
                             "return_condition", "escalation_trigger"},
            {"depends_on"}, protocol=protocol)
    for key in ("goal_id", "task_id", "lane_id"):
        _id(value[key])
    _integer(value["goal_revision"])
    _digest(value["request_sha256"])
    _enum(value["role"], {"main", "investigation", "review", "validation"})
    for key in ("outcome", "scope", "return_condition", "escalation_trigger"):
        _text(value[key])
    _reference(value["authority_ref"])
    _object(value["budget"], {"max_duration_ms", "max_attempts", "max_context_bytes"})
    _integer(value["budget"]["max_duration_ms"])
    _integer(value["budget"]["max_attempts"], maximum=32)
    _integer(value["budget"]["max_context_bytes"], maximum=MAX_RECORD_BYTES)
    _list(value["criterion_ids"], _id, maximum=16)
    if "depends_on" in value:
        _list(value["depends_on"], _id, maximum=8, minimum=0)
        if len(set(value["depends_on"])) != len(value["depends_on"]):
            _fail("duplicate lane dependency")


def _goal_v2(value: dict) -> None:
    """Additive goal record for workspace economics-aware plans."""
    _record(value, "goal", {"goal_id", "revision", "objective", "criteria",
                             "constraints", "active_priority", "unresolved_decisions"},
            protocol=PROTOCOL_V2)
    base = dict(value)
    base["protocol"] = PROTOCOL
    _goal(base)


def _lane_v2(value: dict) -> None:
    """Economics-aware lane; v1 lane fields retain their exact validators."""
    _record(value, "lane", {"goal_id", "goal_revision", "task_id", "lane_id",
                             "request_sha256", "role", "outcome", "scope",
                             "authority_ref", "budget", "criterion_ids",
                             "return_condition", "escalation_trigger", "context_policy"},
            {"depends_on"}, protocol=PROTOCOL_V2)
    base = dict(value)
    base.pop("context_policy", None)
    base["protocol"] = PROTOCOL
    _lane(base)
    try:
        _context_policy.validate(value["context_policy"])
    except _context_policy.ContextPolicyError as exc:
        raise WorkspaceProtocolError("invalid lane context policy") from exc


def _message(value: dict) -> None:
    _record(value, "message", {"message_id", "goal_id", "source_task_id",
                               "destination_task_id", "sender", "recipient",
                               "grant_ref", "stream_id", "sequence", "content"},
            {"correlation_id", "reply_to", "causation_id"})
    for key in ("message_id", "goal_id", "source_task_id", "destination_task_id", "stream_id"):
        _id(value[key])
    for key in ("correlation_id", "reply_to", "causation_id"):
        if key in value:
            _id(value[key])
    _worker(value["sender"])
    _worker(value["recipient"])
    _reference(value["grant_ref"])
    _integer(value["sequence"])
    _object(value["content"], {"ref", "sha256", "utf8_bytes"})
    _id(value["content"]["ref"])
    _digest(value["content"]["sha256"])
    _integer(value["content"]["utf8_bytes"], maximum=MAX_CONTENT_BYTES)


def _operator_message(value: dict) -> None:
    """Local host identity, never a worker or a verified personal identity."""
    _record(value, "operator_message", {"message_id", "goal_id", "destination_task_id",
        "sender", "recipient", "grant_ref", "stream_id", "sequence", "content"})
    for key in ("message_id", "goal_id", "destination_task_id", "stream_id"):
        _id(value[key])
    _object(value["sender"], {"kind", "operator_id"})
    _enum(value["sender"]["kind"], {"local_operator"})
    _id(value["sender"]["operator_id"])
    _worker(value["recipient"])
    _reference(value["grant_ref"])
    _integer(value["sequence"])
    _object(value["content"], {"ref", "sha256", "utf8_bytes"})
    _id(value["content"]["ref"])
    _digest(value["content"]["sha256"])
    _integer(value["content"]["utf8_bytes"], maximum=2048)


def message_stream_binding(message: dict) -> dict:
    """Canonical typed stream identity; no synthetic source task for operators."""
    m = validate_record(message)
    fields = {"message": ("sender", "recipient", "source_task_id", "destination_task_id"),
              "operator_message": ("sender", "recipient", "destination_task_id"),
              "supervisor_message": ("sender", "recipient", "source_task_id")}.get(m["kind"])
    if fields is None:
        _fail("message stream requires a logical message")
    return {key: m[key] for key in fields}


def _delivery(value: dict) -> None:
    _record(value, "delivery", {"delivery_id", "message_id", "recipient", "grant_ref",
                                "state", "reason", "selection", "certainty",
                                "inherited_uncertainty", "ack_level"}, {"parent_delivery_id"})
    for key in ("delivery_id", "message_id"):
        _id(value[key])
    if "parent_delivery_id" in value:
        _id(value["parent_delivery_id"])
        if value["parent_delivery_id"] == value["delivery_id"]:
            _fail("self-linked delivery")
    _worker(value["recipient"])
    _reference(value["grant_ref"])
    _enum(value["state"], STATES)
    _certainty(value["certainty"])
    _list(value["inherited_uncertainty"], lambda x: _enum(x, {"contact", "spend", "cleanup"}), minimum=0, maximum=3)
    state = value["state"]
    if value["selection"] is not None:
        _attempt(value["selection"])
        if value["message_id"] not in value["selection"]["selected_message_ids"]:
            _fail("delivery absent from frozen selection")
    if state in {"accepted", "queued", "rejected", "expired", "cancelled"} and value["selection"] is not None:
        _fail("unselected delivery has attempt")
    if state in {"included_in_attempt", "submission_started", "submitted", "acknowledged", "not_submitted"} and value["selection"] is None:
        _fail("selected delivery missing attempt")
    if state == "held_for_recovery":
        _enum(value["reason"], HOLD_REASONS)
    elif state in {"rejected", "expired", "cancelled", "dead_lettered"}:
        _id(value["reason"])
    elif value["reason"] is not None:
        _fail("unexpected reason")
    if state == "acknowledged":
        _enum(value["ack_level"], ACK_LEVELS)
    elif value["ack_level"] is not None:
        _fail("acknowledgement without acknowledged state")
    certainty = value["certainty"]
    if state in {"accepted", "queued", "included_in_attempt", "rejected", "expired", "cancelled"} and certainty != {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}:
        _fail("pre-submission state cannot claim effects")
    if state == "not_submitted" and (certainty["contact"] != "none" or certainty["spend"] != "not_incurred" or certainty["cleanup"] not in {"not_started", "complete"}):
        _fail("not_submitted needs affirmative no-effect certainty")
    if state in {"submitted", "acknowledged"} and certainty["contact"] != "occurred":
        _fail("submission requires contact claim")


def _assessment(value: dict) -> None:
    _record(value, "assessment", {"assessment_id", "goal_id", "goal_revision", "task_id",
                                  "lane_id", "supervisor_id", "criterion_results", "evidence",
                                  "limitations", "handoff", "disposition", "next_decision", "reason"},
            {"affected_task_ids"})
    for key in ("assessment_id", "goal_id", "task_id", "lane_id", "supervisor_id"):
        _id(value[key])
    _integer(value["goal_revision"])
    def result(item):
        _object(item, {"criterion_id", "result", "evidence_ids"})
        _id(item["criterion_id"])
        _enum(item["result"], {"met", "unmet", "unknown"})
        _list(item["evidence_ids"], _id, minimum=0)
        if item["result"] == "met" and not item["evidence_ids"]:
            _fail("met criterion lacks evidence link")
    _list(value["criterion_results"], result, maximum=16)
    _list(value["evidence"], _reference)
    ids = [r["id"] for r in value["evidence"]]
    if len(set(ids)) != len(ids):
        _fail("conflicting evidence identity")
    criteria = [r["criterion_id"] for r in value["criterion_results"]]
    if len(set(criteria)) != len(criteria):
        _fail("duplicate criterion assessment")
    if any(set(r["evidence_ids"]) - set(ids) for r in value["criterion_results"]):
        _fail("unbound evidence id")
    _list(value["limitations"], _text, minimum=0, maximum=16)
    for key in ("handoff", "reason"):
        _text(value[key])
    _enum(value["disposition"], {"continue_main", "hold_affected_lane", "promote_issue_to_priority"})
    if value["disposition"] in {"hold_affected_lane", "promote_issue_to_priority"}:
        if "affected_task_ids" not in value:
            _fail("scoped disposition requires affected task ids")
        _list(value["affected_task_ids"], _id, maximum=16)
    elif "affected_task_ids" in value:
        _list(value["affected_task_ids"], _id, minimum=0, maximum=0)
    _enum(value["next_decision"], {"criteria_satisfied", "propose_lane", "blocked", "continue"})
    if value["next_decision"] == "criteria_satisfied" and any(r["result"] != "met" for r in value["criterion_results"]):
        _fail("criteria satisfied with unresolved result")


def supervisor_recipient(value: Any) -> None:
    _object(value, {"kind", "endpoint_id", "owner_instance_id", "epoch"})
    if value["kind"] != "supervisor_inbox":
        _fail("supervisor recipient kind required")
    _id(value["endpoint_id"])
    _id(value["owner_instance_id"])
    _integer(value["epoch"])


def consumer_binding(value: Any) -> dict:
    """Structural binding only; runtime must derive it from an owned handle."""
    _object(value, {"workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch", "receiver_grant_id", "receiver_grant_sha256"})
    for key in ("workspace_id", "run_id", "endpoint_id", "owner_instance_id", "receiver_grant_id"):
        _id(value[key])
    _integer(value["epoch"])
    _digest(value["receiver_grant_sha256"])
    return copy.deepcopy(value)


def _supervisor_endpoint(value: dict) -> None:
    _record(value, "supervisor_endpoint", {"endpoint_id", "goal_id", "revision", "owner", "receiver_grant_ref", "recovery_fence_ref", "lease_expires_at_ms", "status"})
    _id(value["endpoint_id"])
    _id(value["goal_id"])
    _integer(value["revision"], maximum=64)
    _enum(value["status"], {"unbound", "active", "revoked", "retired"})
    if (value["status"] == "active" and value["revision"] > 62
            or value["status"] == "revoked" and value["revision"] > 63):
        _fail("endpoint revision capacity must retain revocation and retirement")
    if value["recovery_fence_ref"] is not None:
        _reference(value["recovery_fence_ref"])
    if value["status"] == "unbound":
        if value["owner"] is not None or value["receiver_grant_ref"] is not None or value["recovery_fence_ref"] is not None or type(value["lease_expires_at_ms"]) is not int or value["lease_expires_at_ms"] != 0:
            _fail("unbound endpoint cannot own receiving authority")
    else:
        _worker(value["owner"])
        _reference(value["receiver_grant_ref"])
        _integer(value["lease_expires_at_ms"])


def _supervisor_message(value: dict) -> None:
    _record(value, "supervisor_message", {"message_id", "goal_id", "source_task_id", "sender", "recipient", "grant_ref", "stream_id", "sequence", "content"},
            {"correlation_id", "reply_to", "causation_id"})
    for key in ("message_id", "goal_id", "source_task_id", "stream_id", "correlation_id", "reply_to", "causation_id"):
        if key in value:
            _id(value[key])
    _worker(value["sender"])
    supervisor_recipient(value["recipient"])
    _reference(value["grant_ref"])
    _integer(value["sequence"])
    _object(value["content"], {"ref", "sha256", "utf8_bytes"})
    _id(value["content"]["ref"])
    _digest(value["content"]["sha256"])
    _integer(value["content"]["utf8_bytes"], maximum=MAX_CONTENT_BYTES)


def _inbox_delivery(value: dict) -> None:
    _record(value, "inbox_delivery", {"delivery_id", "message_id", "recipient", "grant_ref", "state", "reason", "offer", "receipt",
            "exposure", "prior_offer_id", "inherited_exposure", "possible_duplicate"}, {"parent_delivery_id"})
    for key in ("delivery_id", "message_id", "parent_delivery_id"):
        if key in value:
            _id(value[key])
    if value.get("parent_delivery_id") == value["delivery_id"]:
        _fail("self-linked inbox delivery")
    supervisor_recipient(value["recipient"])
    _reference(value["grant_ref"])
    _enum(value["state"], INBOX_STATES)
    _enum(value["exposure"], {"not_exposed", "unknown", "consumer_received"})
    if type(value["possible_duplicate"]) is not bool or value["inherited_exposure"] not in (None, "unknown"):
        _fail("invalid inbox uncertainty")
    if value["prior_offer_id"] is not None:
        _id(value["prior_offer_id"])
    if value["inherited_exposure"] is not None and (not value["possible_duplicate"] or value["prior_offer_id"] is None or "parent_delivery_id" not in value):
        _fail("inherited inbox exposure needs explicit predecessor offer")
    if value["possible_duplicate"] != (value["inherited_exposure"] == "unknown"):
        _fail("duplicate-exposure flag differs from inherited facts")
    state = value["state"]
    if state in {"accepted", "queued", "cancelled", "expired"} and value["offer"] is not None:
        _fail("unoffered inbox delivery has an offer")
    if state in {"offered", "acknowledged"} and value["offer"] is None:
        _fail("offered inbox delivery lacks offer")
    if value["offer"] is not None:
        offer = value["offer"]
        _object(offer, {"offer_id", "delivery_id", "message_id", "content_sha256", "content_utf8_bytes", "consumer"})
        _id(offer["offer_id"])
        _digest(offer["content_sha256"])
        _integer(offer["content_utf8_bytes"], maximum=MAX_CONTENT_BYTES)
        consumer = consumer_binding(offer["consumer"])
        if (any(offer[key] != value[key] for key in ("delivery_id", "message_id"))
                or any(consumer[key] != value[key] for key in ("workspace_id", "run_id"))
                or any(consumer[key] != value["recipient"][key] for key in ("endpoint_id", "owner_instance_id", "epoch"))
                or {"id": consumer["receiver_grant_id"], "sha256": consumer["receiver_grant_sha256"]} != value["grant_ref"]):
            _fail("inbox offer binding differs")
    if state == "acknowledged":
        receipt = value["receipt"]
        _object(receipt, {"receipt_kind", "consumer_kind", "qualification", "offer_id", "content_sha256", "content_utf8_bytes"})
        _id(receipt["offer_id"])
        _digest(receipt["content_sha256"])
        _integer(receipt["content_utf8_bytes"], maximum=MAX_CONTENT_BYTES)
        if (receipt["receipt_kind"] != "supervisor_context_received" or receipt["consumer_kind"] != INBOX_CONSUMER_KIND
                or receipt["qualification"] != "simulated" or value["exposure"] != "consumer_received"
                or any(receipt[key] != value["offer"][key] for key in ("offer_id", "content_sha256", "content_utf8_bytes"))):
            _fail("inbox receipt identity or qualification differs")
    elif value["receipt"] is not None or value["exposure"] == "consumer_received":
        _fail("consumer receipt without acknowledgement")
    if state == "offered" and value["exposure"] != "unknown":
        _fail("offer must retain possible exposure")
    if state in {"accepted", "queued", "cancelled", "expired"} and value["exposure"] != ("unknown" if value["inherited_exposure"] else "not_exposed"):
        _fail("unoffered inbox exposure differs")
    if value["offer"] is not None and value["exposure"] == "not_exposed":
        _fail("offer cannot imply nonexposure")
    if state in {"held_for_recovery", "cancelled", "expired", "dead_lettered"}:
        _id(value["reason"])
    elif value["reason"] is not None:
        _fail("unexpected inbox reason")


def validate_inbox_transition(before: dict, after: dict, evidence: dict) -> dict:
    """Finite local-context claims only; supplied proofs are not authentication."""
    b, a = validate_record(before), validate_record(after)
    if b["kind"] != "inbox_delivery" or a["kind"] != "inbox_delivery" or (b["state"], a["state"]) not in INBOX_EDGES:
        _fail("invalid inbox transition")
    mutable = {"state", "reason", "exposure"}
    if a["state"] == "offered": mutable.add("offer")
    if a["state"] == "acknowledged": mutable.add("receipt")
    if {k: v for k, v in b.items() if k not in mutable} != {k: v for k, v in a.items() if k not in mutable}:
        _fail("immutable inbox binding changed")
    _proof(evidence, set(INBOX_ROLES[a["state"]]))
    if a["state"] not in {"offered", "acknowledged"} and a["exposure"] != b["exposure"]:
        _fail("inbox disposition cannot erase exposure")
    return a


def validate_inbox_lineage(parent: dict, child: dict) -> dict:
    """Validate immutable ancestry after any valid child progress; no proof/auth claim."""
    p, c = validate_record(parent), validate_record(child)
    if (p["kind"] != "inbox_delivery" or c["kind"] != "inbox_delivery" or p["state"] not in {"held_for_recovery", "dead_lettered"}
            or c.get("parent_delivery_id") != p["delivery_id"]
            or any(c[key] != p[key] for key in ("workspace_id", "run_id", "message_id"))
            or c["recipient"]["endpoint_id"] != p["recipient"]["endpoint_id"]
            or c["recipient"]["epoch"] <= p["recipient"]["epoch"]):
        _fail("invalid linked inbox successor")
    if c["grant_ref"] == p["grant_ref"]:
        _fail("inbox successor needs a fresh receiving grant")
    unknown = p["exposure"] == "unknown" or p["inherited_exposure"] == "unknown"
    prior = p["offer"]["offer_id"] if p["offer"] else p["prior_offer_id"]
    if (c["inherited_exposure"] != ("unknown" if unknown else None)
            or c["possible_duplicate"] is not unknown or c["prior_offer_id"] != prior):
        _fail("inbox successor erased inherited exposure")
    return c


def validate_inbox_link(parent: dict, child: dict, evidence: dict) -> dict:
    c = validate_inbox_lineage(parent, child)
    if c["state"] != "queued" or c["exposure"] != ("unknown" if c["inherited_exposure"] else "not_exposed"):
        _fail("new inbox successor must be queued with inherited exposure")
    _proof(evidence, {"fresh_receiving_grant", "recovery_fence"})
    if evidence["fresh_receiving_grant"] != c["grant_ref"]:
        _fail("inbox successor needs matching fresh receiving grant")
    return c


def validate_record(value: Any) -> dict:
    """Return a detached validated record, not a capability or certified fact."""
    if type(value) is not dict or type(value.get("kind")) is not str:
        _fail("invalid workspace record")
    original = value
    validation_value = value
    if value.get("protocol") == PROTOCOL_V2:
        validator = {"goal": _goal_v2, "lane": _lane_v2}.get(value["kind"])
        if validator is None:
            # v2 changes the prepared goal/lane envelope; ordinary message,
            # delivery, assessment, and operator records retain the v1 field
            # contract but carry the v2 workspace scope. Validate a detached
            # v1-protocol view so the journal can keep one coherent version.
            validator = {"message": _message, "operator_message": _operator_message,
                         "delivery": _delivery, "assessment": _assessment,
                         "supervisor_endpoint": _supervisor_endpoint,
                         "supervisor_message": _supervisor_message,
                         "inbox_delivery": _inbox_delivery}.get(value["kind"])
            if validator is not None:
                validation_value = dict(value)
                validation_value["protocol"] = PROTOCOL
    else:
        validator = {"goal": _goal, "lane": _lane, "message": _message, "operator_message": _operator_message,
                     "delivery": _delivery, "assessment": _assessment, "supervisor_endpoint": _supervisor_endpoint,
                     "supervisor_message": _supervisor_message, "inbox_delivery": _inbox_delivery}.get(value["kind"])
    if validator is None:
        _fail("unsupported workspace kind")
    try:
        validator(validation_value)
        encoded = json.dumps(original, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise WorkspaceProtocolError("invalid JSON record") from exc
    if len(encoded) > MAX_RECORD_BYTES:
        _fail("complete serialized record exceeds byte limit")
    return copy.deepcopy(original)


def validate_goal_binding(goal: dict, lane: dict, assessment: dict | None = None) -> None:
    """Check scope/criterion links; never authenticate a supervisor judgment."""
    goal, lane = validate_record(goal), validate_record(lane)
    if goal["kind"] != "goal" or lane["kind"] != "lane":
        _fail("wrong goal binding kind")
    if any(goal[k] != lane[k] for k in ("workspace_id", "run_id", "goal_id")) or goal["revision"] != lane["goal_revision"]:
        _fail("goal binding conflict")
    if set(lane["criterion_ids"]) - {c["criterion_id"] for c in goal["criteria"]}:
        _fail("unknown goal criterion")
    if assessment is not None:
        a = validate_record(assessment)
        if a["kind"] != "assessment" or any(a[k] != lane[k] for k in ("workspace_id", "run_id", "goal_id", "goal_revision", "task_id", "lane_id")):
            _fail("assessment binding conflict")
        if {r["criterion_id"] for r in a["criterion_results"]} != set(lane["criterion_ids"]):
            _fail("assessment must cover assigned criteria")


def validate_goal_plan(goal: dict, lanes: list[dict]) -> None:
    """Validate complete prepared lane scope, including initial priority target.

    Single-lane validation cannot prove references to other prepared lanes. Use
    this check at the full plan boundary before assessment or execution admission.
    """
    g = validate_record(goal)
    if g["kind"] != "goal" or type(lanes) is not list or not 1 <= len(lanes) <= 16:
        _fail("invalid complete goal plan")
    task_ids, lane_ids = set(), set()
    for lane in lanes:
        validate_goal_binding(g, lane)
        if lane["task_id"] in task_ids or lane["lane_id"] in lane_ids:
            _fail("duplicate prepared lane binding")
        task_ids.add(lane["task_id"])
        lane_ids.add(lane["lane_id"])
    for lane in lanes:
        dependencies = lane.get("depends_on", [])
        if set(dependencies) - task_ids:
            _fail("unknown lane dependency")
    visiting, visited = set(), set()
    by_task = {lane["task_id"]: lane for lane in lanes}
    def visit(task_id):
        if task_id in visiting:
            _fail("lane dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_task[task_id].get("depends_on", []):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in task_ids:
        visit(task_id)
    roots = {task_id for task_id, lane in by_task.items() if not lane.get("depends_on", [])}
    if g["active_priority"] not in roots:
        _fail("initial priority must be a dependency root")
    if g["active_priority"] not in task_ids:
        _fail("initial priority must name a prepared lane task")


def validate_goal_plan_v2(goal: dict, lanes: list[dict], *, economics: dict | None = None) -> None:
    """Validate an economics-fenced additive workspace plan.

    The v1 goal/lane lifecycle is not relaxed.  New readers must opt into this
    function and explicitly reserve the bounded economics capacity before any
    admission mutation.
    """
    g = validate_record(goal)
    if g["protocol"] != PROTOCOL_V2 or g["kind"] != "goal":
        _fail("invalid v2 complete goal plan")
    if type(lanes) is not list or not 1 <= len(lanes) <= 16:
        _fail("invalid v2 complete goal plan")
    if economics is not None:
        _object(economics, {"schema", "feature", "enabled", "max_records",
                            "max_settlement_bytes"})
        if (economics["schema"] != "summon.workspace.economics/v1"
                or economics["feature"] != "submission-accounting-v1"
                or economics["enabled"] is not True
                or type(economics["max_records"]) is not int
                or not 1 <= economics["max_records"] <= 64
                or type(economics["max_settlement_bytes"]) is not int
                or not MIN_ECONOMICS_SETTLEMENT_BYTES <= economics["max_settlement_bytes"] <= 65536):
            _fail("invalid economics feature fence")
    task_ids, lane_ids = set(), set()
    for lane in lanes:
        row = validate_record(lane)
        if row["protocol"] != PROTOCOL_V2 or row["kind"] != "lane":
            _fail("invalid v2 lane")
        if any(row[key] != g[key] for key in ("workspace_id", "run_id", "goal_id")):
            _fail("v2 goal binding conflict")
        if row["goal_revision"] != g["revision"]:
            _fail("v2 goal revision conflict")
        if set(row["criterion_ids"]) - {c["criterion_id"] for c in g["criteria"]}:
            _fail("v2 unknown goal criterion")
        if row["task_id"] in task_ids or row["lane_id"] in lane_ids:
            _fail("duplicate v2 prepared lane binding")
        task_ids.add(row["task_id"]); lane_ids.add(row["lane_id"])
    by_task = {lane["task_id"]: lane for lane in lanes}
    for lane in lanes:
        if set(lane.get("depends_on", [])) - task_ids:
            _fail("unknown v2 lane dependency")
    visiting, visited = set(), set()
    def visit(task_id):
        if task_id in visiting:
            _fail("v2 lane dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_task[task_id].get("depends_on", []):
            visit(dependency)
        visiting.remove(task_id); visited.add(task_id)
    for task_id in task_ids:
        visit(task_id)
    roots = {task_id for task_id, lane in by_task.items() if not lane.get("depends_on", [])}
    if g["active_priority"] not in roots or g["active_priority"] not in task_ids:
        _fail("v2 initial priority must be a dependency root")


def validate_delivery_binding(message: dict, delivery: dict) -> None:
    """Validate an initial binding. Linked children need resolved-parent validation."""
    m, d = validate_record(message), validate_record(delivery)
    if m["kind"] not in {"message", "operator_message"} or d["kind"] != "delivery":
        _fail("wrong delivery binding kind")
    if any(m[k] != d[k] for k in ("workspace_id", "run_id", "message_id")):
        _fail("logical message binding conflict")
    if "parent_delivery_id" in d:
        _fail("linked binding requires resolved parent and disposition validation")
    if any(m[k] != d[k] for k in ("recipient", "grant_ref")):
        _fail("initial recipient/grant binding conflict")
    if d["selection"] and d["selection"]["task_id"] != m["destination_task_id"]:
        _fail("destination task conflict")


def validate_immutable_record(before: dict, after: dict) -> dict:
    """Check duplicate immutable definitions; revisions need a later explicit API."""
    b, a = validate_record(before), validate_record(after)
    if b["kind"] not in {"goal", "lane", "message", "operator_message", "assessment"} or b != a:
        _fail("conflicting immutable record")
    return a


def validate_content(message: dict, content: bytes) -> None:
    """Verify whole private content bytes against a message, without reading files."""
    m = validate_record(message)
    if m["kind"] not in {"message", "operator_message", "supervisor_message"} or type(content) is not bytes:
        _fail("message and UTF-8 bytes required")
    try:
        content.decode("utf-8")
    except UnicodeError:
        _fail("content is not UTF-8")
    if (len(content) != m["content"]["utf8_bytes"]
            or hashlib.sha256(content).hexdigest() != m["content"]["sha256"]):
        _fail("whole content binding mismatch")


def _proof(value: Any, refs: set[str], extras: set[str] = frozenset()) -> None:
    _object(value, refs | extras)
    for key in refs:
        _reference(value[key])


def validate_transition(before: dict, after: dict, evidence: dict, *, supported_ack_levels=()) -> dict:
    """Validate a normative edge and required evidence *references* only.

    Caller must authenticate and verify every reference before durable apply.
    No successful return authorizes dispatch or establishes external effects.
    """
    b, a = validate_record(before), validate_record(after)
    if b["kind"] != "delivery" or a["kind"] != "delivery":
        _fail("transition requires delivery")
    edge = b["state"], a["state"]
    if edge not in EDGES:
        _fail("forbidden delivery transition; linked successor required")
    mutable = {"state", "reason", "certainty", "ack_level"}
    if edge == ("queued", "included_in_attempt"):
        mutable.add("selection")
    if {k: v for k, v in b.items() if k not in mutable} != {k: v for k, v in a.items() if k not in mutable}:
        _fail("immutable delivery binding changed")
    target = a["state"]
    if target == "queued":
        _proof(evidence, {"durable_admission"})
    elif target == "included_in_attempt":
        _proof(evidence, {"selection_record", "whole_message_fit"})
    elif target == "submission_started":
        _proof(evidence, {"current_grant", "recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation", "durable_launch_intent"})
    elif target == "submitted":
        _proof(evidence, {"adapter_receipt"}, {"submission_boundary"})
        _id(evidence["submission_boundary"])
    elif target == "acknowledged":
        _proof(evidence, {"ack_receipt"}, {"ack_level"})
        if (type(supported_ack_levels) not in (tuple, list, frozenset, set)
                or any(type(x) is not str or x not in ACK_LEVELS for x in supported_ack_levels)):
            _fail("invalid adapter acknowledgement capabilities")
        _enum(evidence["ack_level"], supported_ack_levels)
        if evidence["ack_level"] != a["ack_level"]:
            _fail("acknowledgement level mismatch")
    elif target == "not_submitted":
        _proof(evidence, {"no_contact_receipt", "cleanup_evidence"}, {"provider_contact", "side_effects"})
        if evidence["provider_contact"] is not False or evidence["side_effects"] is not False:
            _fail("no contact and no effects require affirmative false evidence")
        if b["certainty"]["contact"] == "occurred" or b["certainty"]["spend"] == "incurred":
            _fail("no-contact proof contradicts prior effects")
        if b["certainty"]["cleanup"] == "complete" and a["certainty"]["cleanup"] != "complete":
            _fail("no-contact disposition cannot erase completed cleanup")
    elif target == "held_for_recovery":
        _proof(evidence, {"hold_observation"})
    elif target == "rejected":
        _proof(evidence, {"validated_refusal"})
    elif target == "expired":
        _proof(evidence, {"expiry_observation"}, {"now_ms", "expires_at_ms"})
        _integer(evidence["now_ms"])
        _integer(evidence["expires_at_ms"])
        if evidence["now_ms"] < evidence["expires_at_ms"]:
            _fail("not expired")
    elif target == "cancelled":
        _proof(evidence, {"authorized_cancellation"})
    else:
        _proof(evidence, {"authenticated_disposition"})
    # No bookkeeping edge can erase uncertainty. Only an affirmative no-contact
    # receipt resolves uncertain contact on the specifically permitted edges.
    if target not in {"not_submitted", "submission_started", "submitted", "held_for_recovery"}:
        if a["certainty"] != b["certainty"]:
            _fail("bookkeeping cannot change effect certainty")
    elif target == "submission_started":
        if a["certainty"] != {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}:
            _fail("launch intent must conservatively retain uncertainty")
    elif target == "submitted":
        if any(a["certainty"][k] != b["certainty"][k] for k in ("spend", "cleanup")):
            _fail("submission receipt does not settle spend or cleanup")
    elif target == "held_for_recovery":
        for key, old in b["certainty"].items():
            new = a["certainty"][key]
            if new != old and (new != "unknown" or old in {"occurred", "incurred", "complete"}):
                _fail("hold cannot resolve effects")
    return a


def effect_binding(delivery: dict) -> dict:
    """Detached full immutable delivery identity for host-issued observations."""
    record = validate_record(delivery)
    if record["kind"] != "delivery" or record["selection"] is None:
        _fail("effect observation requires a selected delivery")
    return {key: copy.deepcopy(record[key]) for key in (
        "protocol", "workspace_id", "run_id", "delivery_id", "message_id",
        "recipient", "grant_ref", "selection")}


def validate_effect_resolution(before: dict, after: dict, evidence: dict, *,
                               resolution_kind: str, qualification: str) -> dict:
    """Validate effect-resolution claims and refs; never establish their truth.

    The trusted host must independently verify both launcher-issued source
    observations in the same mutation. Neither a reference, a digest nor a
    user-supplied simulated label proves fixed execution or actual cleanup.
    """
    _enum(resolution_kind, {EFFECT_RESOLUTION_KIND})
    _enum(qualification, {"simulated"})
    b, a = validate_record(before), validate_record(after)
    if b["kind"] != "delivery" or a["kind"] != "delivery":
        _fail("effect resolution requires delivery")
    if (b["state"] != "acknowledged" or b["inherited_uncertainty"]
            or "parent_delivery_id" in b):
        _fail("effect resolution supports only an acknowledged original delivery")
    if {k: v for k, v in b.items() if k != "certainty"} != {k: v for k, v in a.items() if k != "certainty"}:
        _fail("effect resolution changed immutable delivery or lifecycle facts")
    if b["certainty"] != {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"}:
        _fail("effect resolution cannot replace prior known facts")
    if a["certainty"] != {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}:
        _fail("unsupported fixed fixture effect resolution")
    _proof(evidence, set(EFFECT_EVIDENCE_CATEGORIES))
    if evidence["fixed_execution_scope"]["id"] == evidence["owned_child_cleanup"]["id"]:
        _fail("execution and cleanup require separate evidence references")
    return a


def validate_effect_observations(delivery: dict, execution_scope: dict,
                                 cleanup: dict) -> tuple[dict, dict]:
    """Validate observation shape and binding, not issuer provenance or facts.

    The runtime additionally requires observations issued by its actual fixed
    launcher for this request. Caller dictionaries and hashes alone are never
    sufficient. No process, file, provider or billing inspection happens here.
    """
    record = validate_record(delivery)
    if (record["kind"] != "delivery" or record["state"] != "acknowledged"
            or record["inherited_uncertainty"] or "parent_delivery_id" in record):
        _fail("unsupported effect observation delivery")
    expected = effect_binding(record)
    common = {"schema", "kind", "resolution_kind", "qualification", "binding",
              "child_instance_id", "request_frame_sha256", "request_sequence"}
    extras = {
        "fixed_execution_scope": {"launcher", "operation", "provider_invoked"},
        "owned_child_cleanup": {"child_exit_observed", "pipes_closed", "channel_revoked", "exit_code"},
    }
    for role, observation in (("fixed_execution_scope", execution_scope), ("owned_child_cleanup", cleanup)):
        _object(observation, common | extras[role])
        if (observation["schema"] != EFFECT_OBSERVATION_SCHEMA or observation["kind"] != role
                or observation["resolution_kind"] != EFFECT_RESOLUTION_KIND
                or observation["qualification"] != "simulated"):
            _fail("unsupported effect observation source")
        binding = observation["binding"]
        _object(binding, set(expected))
        for key in ("workspace_id", "run_id", "delivery_id", "message_id"):
            _id(binding[key])
        _worker(binding["recipient"])
        _reference(binding["grant_ref"])
        _attempt(binding["selection"])
        if binding != expected:
            _fail("effect observation full delivery binding mismatch")
        _id(observation["child_instance_id"])
        _digest(observation["request_frame_sha256"])
        _integer(observation["request_sequence"])
    for key in ("child_instance_id", "request_frame_sha256", "request_sequence"):
        if execution_scope[key] != cleanup[key]:
            _fail("effect observations refer to different child or request")
    if execution_scope["launcher"] != "summon_owned_fake_worker/v1":
        _fail("effect resolution requires fixed owned launcher")
    _enum(execution_scope["operation"], {"sum_integers_v1", "sum_context_integers_v1"})
    if execution_scope["provider_invoked"] is not False:
        _fail("fixed fixture cannot establish provider spend")
    if any(cleanup[key] is not True for key in ("child_exit_observed", "pipes_closed", "channel_revoked")):
        _fail("owned child cleanup is not established")
    _integer(cleanup["exit_code"], minimum=-(2**31), maximum=2**32 - 1)
    return copy.deepcopy(execution_scope), copy.deepcopy(cleanup)


def validate_linked_delivery(parent: dict, child: dict, evidence: dict) -> dict:
    """Validate a separate queued successor; never modify/rewind the parent."""
    p, c = validate_record(parent), validate_record(child)
    if p["kind"] != "delivery" or c["kind"] != "delivery":
        _fail("link requires deliveries")
    if p["state"] not in {"not_submitted", "held_for_recovery", "dead_lettered"} or c["state"] != "queued":
        _fail("ineligible linked successor")
    if c.get("parent_delivery_id") != p["delivery_id"] or c["delivery_id"] == p["delivery_id"]:
        _fail("invalid successor identity")
    if any(p[k] != c[k] for k in ("workspace_id", "run_id", "message_id")):
        _fail("successor logical binding conflict")
    refs = {"authenticated_disposition", "remaining_authority"}
    fresh = p["state"] in {"held_for_recovery", "dead_lettered"}
    if fresh:
        refs |= {"fresh_attempt_grant", "physical_fence"}
    if p["recipient"] != c["recipient"]:
        refs.add("recipient_transfer_authority")
    _proof(evidence, refs)
    if fresh and (c["grant_ref"] == p["grant_ref"] or evidence["fresh_attempt_grant"] != c["grant_ref"]):
        _fail("fresh successor grant required")
    required = set(p["inherited_uncertainty"]) | {k for k, v in p["certainty"].items() if v == "unknown"}
    if set(c["inherited_uncertainty"]) != required:
        _fail("successor must preserve all parent uncertainty")
    if c["certainty"] != {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}:
        _fail("new delivery cannot claim an attempted submission")
    return c
