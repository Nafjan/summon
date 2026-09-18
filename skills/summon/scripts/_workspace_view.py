"""Pure, bounded task-shell presentation; no authentication, store or controls.

The trusted host supplies a replayed workspace and complete coordinator status
from the same reconciled prefix. ViewScope/capabilities/supervisor observations
must come from that host, never worker payload or browser JSON. This module only
checks their consistency: it establishes no authentication or execution grant.
Opaque IDs/cursors are presentation handles, never capabilities. The host keeps
the scope's random key stable within its intended export/operator scope.

Public exports contain no arbitrary text, source hashes, receipt bodies, grants
or raw task/worker/session identities. Explicit operator text scope permits only
goal/criteria/lane descriptions; message bodies and evidence remain unavailable.
Timeline events are matched to retained operation fingerprints, not invented
from current delivery state. Missing history and model evidence stay unavailable.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re

import _submission_accounting as submission_accounting
import _workspace_admission as admission
import _workspace_protocol as protocol
import _workspace_state as state_protocol

SCHEMA = "summon.workspace.view/v1"
MAX_PAGE = 200
MAX_INPUT_BYTES = 8 * 1024 * 1024
DELIVERY_LABELS = {
    "accepted": "Accepted durably", "queued": "Queued for a later turn",
    "included_in_attempt": "Included in the selected attempt",
    "submission_started": "Submission started; effects uncertain",
    "submitted": "Submission receipt recorded", "acknowledged": "Acknowledgement recorded",
    "not_submitted": "Proven no contact; no automatic retry",
    "held_for_recovery": "Held for explicit recovery", "rejected": "Rejected",
    "expired": "Expired", "cancelled": "Cancelled", "dead_lettered": "Explicitly disposed",
}
INBOX_LABELS = {
    "accepted": "Supervisor context accepted", "queued": "Supervisor context queued",
    "offered": "Offered; receiving status unknown", "acknowledged": "Simulated supervisor context received",
    "held_for_recovery": "Supervisor context held for explicit recovery",
    "cancelled": "Supervisor context cancelled", "expired": "Supervisor context expired",
    "dead_lettered": "Supervisor context explicitly disposed",
}
ACTIONS = ("inspect", "submit_message", "evaluate_queue", "extend", "cancel", "steer",
           "pause", "resume", "fork", "review", "deliberate", "start_supervisor", "close")
UNAVAILABLE_REASONS = frozenset({"not_implemented", "not_authorized", "scope_required",
    "adapter_unverified", "supervisor_required", "workspace_unsettled", "not_applicable",
    "stale_snapshot", "read_only_export", "workspace_closed"})
EVENT_LABELS = {
    "workspace_feature": "Workspace prepared", "workspace_goal_defined": "Goal defined",
    "workspace_lane_defined": "Task lane defined", "workspace_evidence_registered": "Evidence registered",
    "workspace_assessed": "Supervisor assessment recorded", "workspace_next_lane_selected": "Next task selected",
    "workspace_message_admitted": "Message accepted", "workspace_turn_admitted": "Task turn admitted",
    "workspace_message_sent": "Message durably queued",
    "workspace_operator_message_sent": "Local operator context queued",
    "workspace_delivery_advanced": "Delivery boundary recorded", "workspace_delivery_linked": "Replacement delivery recorded",
    "workspace_effects_resolved": "Simulated effects explicitly resolved",
    "workspace_operator_disposition_recorded": "Held context disposition recorded",
    "workspace_supervisor_endpoint_defined": "Supervisor inbox defined",
    "workspace_supervisor_endpoint_activated": "Supervisor receiver binding recorded",
    "workspace_supervisor_endpoint_revoked": "Supervisor receiver binding revoked",
    "workspace_supervisor_endpoint_retired": "Supervisor receiver binding retired",
    "workspace_supervisor_message_sent": "Supervisor context durably queued",
    "workspace_inbox_delivery_advanced": "Supervisor context boundary recorded",
    "workspace_inbox_delivery_linked": "Supervisor context replacement recorded",
}
_INBOX_COMMAND_EVENTS = frozenset({"workspace_supervisor_endpoint_defined", "workspace_supervisor_endpoint_activated",
    "workspace_supervisor_endpoint_revoked", "workspace_supervisor_endpoint_retired",
    "workspace_inbox_delivery_advanced", "workspace_inbox_delivery_linked"})
_WORKSPACE_FIELDS = {"protocol", "workspace_id", "run_id", "revision", "goal", "goal_history",
    "active_priority", "lanes", "evidence", "assessments", "decisions", "messages", "deliveries",
    "streams", "operations"}
_STATUS_FIELDS = {"status", "run_id", "project_root_sha256", "roster_definition_sha256",
    "max_attempts", "generation", "tasks", "worker_count", "message_count", "artifact_count",
    "uncertain_spend", "torn_tail", "workspace"}
_RUN_STATES = {"prepared", "running", "uncertain_spend", "completed", "settlement_required", "closed"}
_TERMINAL = {"completed", "failed", "cancelled", "blocked"}


class WorkspaceViewError(ValueError):
    """A non-sensitive reason code; never include caller strings in errors."""


@dataclass(frozen=True)
class ViewScope:
    workspace_id: str
    run_id: str
    opaque_key: bytes
    audience: str = "public"
    allow_text: bool = False


def _fail(reason):
    raise WorkspaceViewError(reason)


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail("incompatible_snapshot")


def _nonnegative_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _validate_accounting_summary(value):
    """Validate the complete public accounting projection shape.

    The schema marker alone is not a boundary: an untrusted nested object must
    not be allowed to smuggle arbitrary fields through a public workspace view.
    Keep this allowlist synchronized with ``public_summary`` rather than copying
    private attempt identities or provider observations.
    """
    _object(value, {"schema", "candidate_records", "physical_submissions",
                    "physical_attempts", "not_submitted", "indeterminate_contact",
                    "replay_conflicts", "estimate", "reported", "unknown_spend"})
    if (value["schema"] != submission_accounting.SUMMARY_SCHEMA
            or any(type(value[key]) is not int or value[key] < 0 for key in (
                "candidate_records", "physical_submissions", "physical_attempts",
                "not_submitted", "indeterminate_contact", "replay_conflicts"))
            or type(value["unknown_spend"]) is not bool):
        _fail("incompatible_snapshot")
    estimate = value["estimate"]
    _object(estimate, {"method", "scope", "represented_bytes_subtotal",
                       "estimated_tokens_subtotal", "covered_attempts",
                       "missing_attempts", "covered_submissions",
                       "missing_submissions", "completeness",
                       "complete_provider_input"})
    if (estimate["method"] != "utf8_bytes_divided_by_4_ceiling"
            or estimate["scope"] != "summon_visible_adapter_input"
            or (estimate["represented_bytes_subtotal"] is not None
                and (type(estimate["represented_bytes_subtotal"]) is not int
                     or estimate["represented_bytes_subtotal"] < 0))
            or (estimate["estimated_tokens_subtotal"] is not None
                and (type(estimate["estimated_tokens_subtotal"]) is not int
                     or estimate["estimated_tokens_subtotal"] < 0))
            or any(type(estimate[key]) is not int or estimate[key] < 0 for key in (
                "covered_attempts", "missing_attempts", "covered_submissions",
                "missing_submissions"))
            or estimate["completeness"] not in {"complete", "partial"}
            or estimate["complete_provider_input"] is not False):
        _fail("incompatible_snapshot")
    reported = value["reported"]
    _object(reported, set(submission_accounting.METRICS))
    for metric in submission_accounting.METRICS:
        item = reported[metric]
        _object(item, {"known_subtotal", "covered_attempts", "missing_attempts",
                       "covered_submissions", "missing_submissions", "completeness"})
        if (item["known_subtotal"] is not None and not _nonnegative_number(item["known_subtotal"])
                or any(type(item[key]) is not int or item[key] < 0 for key in (
                    "covered_attempts", "missing_attempts", "covered_submissions",
                    "missing_submissions"))
                or item["completeness"] not in {"complete", "partial"}):
            _fail("incompatible_snapshot")


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("incompatible_snapshot")


def _opaque(scope, kind, value):
    raw = hmac.new(scope.opaque_key, _json([SCHEMA, scope.workspace_id, scope.run_id, kind, value]),
                   hashlib.sha256).digest()[:24]
    return kind + "_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _check_scope(scope, workspace):
    if (type(scope) is not ViewScope or type(scope.opaque_key) is not bytes
            or not 32 <= len(scope.opaque_key) <= 64 or scope.audience not in {"public", "operator"}
            or type(scope.allow_text) is not bool or scope.audience == "public" and scope.allow_text):
        _fail("scope_refused")
    if scope.workspace_id != workspace.get("workspace_id") or scope.run_id != workspace.get("run_id"):
        _fail("scope_refused")


def _validate_inbox(workspace):
    inbox = workspace.get("inbox_deliveries", {})
    endpoints = workspace.get("supervisor_endpoints", {})
    for key, delivery in inbox.items():
        protocol.validate_record(delivery)
        message = workspace["messages"].get(delivery["message_id"])
        endpoint_id = delivery["recipient"]["endpoint_id"]
        if (delivery["kind"] != "inbox_delivery" or delivery["delivery_id"] != key
                or message is None or message["kind"] != "supervisor_message" or endpoint_id not in endpoints
                or any(delivery[field] != workspace[field] for field in ("workspace_id", "run_id"))
                or message["recipient"]["endpoint_id"] != endpoint_id
                or state_protocol._known_ref(workspace, delivery["grant_ref"], endpoint_id=endpoint_id)["category"] != "grant"):
            _fail("incompatible_snapshot")
        parent = inbox.get(delivery.get("parent_delivery_id"))
        if "parent_delivery_id" in delivery:
            if (parent is None or parent["message_id"] != delivery["message_id"]
                    or parent["recipient"]["endpoint_id"] != endpoint_id
                    or delivery["recipient"]["epoch"] <= parent["recipient"]["epoch"]
                    or parent["grant_ref"] == delivery["grant_ref"]):
                _fail("incompatible_snapshot")
            lineage = getattr(protocol, "validate_inbox_lineage", None)
            if not callable(lineage):
                _fail("inbox_lineage_unqualified")
            lineage(parent, delivery)
        elif any(delivery[field] != message[field] for field in ("recipient", "grant_ref")):
            _fail("incompatible_snapshot")
        if delivery["offer"] is not None and any(delivery["offer"][field] != message["content"][source]
                for field, source in (("content_sha256", "sha256"), ("content_utf8_bytes", "utf8_bytes"))):
            _fail("incompatible_snapshot")
    for key, item in workspace.get("inbox_operations", {}).items():
        protocol._id(key)
        _object(item, {"request_sha256", "consumer", "response"})
        protocol._digest(item["request_sha256"])
        if key not in workspace["operations"]:
            _fail("incomplete_snapshot")
        consumer = item["consumer"]
        if consumer is not None:
            protocol.consumer_binding(consumer)
            if (any(consumer[field] != workspace[field] for field in ("workspace_id", "run_id"))
                    or consumer["endpoint_id"] not in endpoints):
                _fail("incompatible_snapshot")
        response = item["response"]
        _object(response, {"status", "operation_key", "revision", "endpoint_id", "delivery_id", "offer_id", "exposure",
                           "possible_duplicate", "consumer_kind", "qualification"})
        protocol._id(response["operation_key"])
        protocol._integer(response["revision"], maximum=workspace["revision"])
        if (response["endpoint_id"] not in endpoints or type(response["possible_duplicate"]) is not bool
                or response["delivery_id"] is not None and response["delivery_id"] not in inbox):
            _fail("incompatible_snapshot")
        if response["offer_id"] is not None:
            protocol._id(response["offer_id"])
        if response["delivery_id"] is None:
            if (response["status"] not in {"unbound", "active", "revoked", "retired"}
                    or any(response[field] is not None for field in ("offer_id", "exposure", "consumer_kind", "qualification"))
                    or response["possible_duplicate"]):
                _fail("incompatible_snapshot")
        elif (response["status"] not in protocol.INBOX_STATES
                or response["exposure"] not in {"not_exposed", "unknown", "consumer_received"}
                or inbox[response["delivery_id"]]["recipient"]["endpoint_id"] != response["endpoint_id"]
                or (response["consumer_kind"], response["qualification"]) !=
                   ((protocol.INBOX_CONSUMER_KIND, "simulated") if response["status"] == "acknowledged" else (None, None))):
            _fail("incompatible_snapshot")


def _validate(workspace, coordinator):
    _object(workspace, _WORKSPACE_FIELDS, {"effect_resolutions", "send_operations", "supervisor_endpoints", "inbox_deliveries", "inbox_operations"})
    _object(coordinator, {"status", "run_id", "generation", "tasks", "uncertain_spend", "torn_tail", "workspace"},
            _STATUS_FIELDS)
    if workspace["protocol"] not in {protocol.PROTOCOL, protocol.PROTOCOL_V2} or coordinator["status"] not in _RUN_STATES:
        _fail("incompatible_snapshot")
    protocol._id(workspace["workspace_id"])
    protocol._id(workspace["run_id"])
    protocol._integer(workspace["revision"])
    protocol._integer(coordinator["generation"], minimum=0)
    if type(coordinator["uncertain_spend"]) is not bool or type(coordinator["torn_tail"]) is not bool:
        _fail("incompatible_snapshot")
    if coordinator["torn_tail"]:
        _fail("reconciliation_required")
    limits = {"lanes": 16, "evidence": state_protocol.MAX_EVIDENCE,
              "assessments": state_protocol.MAX_ASSESSMENTS, "decisions": state_protocol.MAX_DECISIONS,
              "messages": state_protocol.MAX_MESSAGES, "deliveries": state_protocol.MAX_DELIVERIES,
              "streams": state_protocol.MAX_MESSAGES, "operations": state_protocol.MAX_EVENTS,
              "effect_resolutions": state_protocol.MAX_DELIVERIES}
    limits["send_operations"] = state_protocol.MAX_MESSAGES
    limits.update(supervisor_endpoints=state_protocol.MAX_ENDPOINTS,
                  inbox_deliveries=state_protocol.MAX_DELIVERIES, inbox_operations=state_protocol.MAX_EVENTS)
    for name, maximum in limits.items():
        value = workspace.get(name, {})
        if type(value) is not dict or len(value) > maximum:
            _fail("snapshot_bound_exceeded")
    if type(workspace["goal_history"]) is not list or len(workspace["goal_history"]) > state_protocol.MAX_EVENTS:
        _fail("snapshot_bound_exceeded")
    if len(_json([workspace, coordinator])) > MAX_INPUT_BYTES:
        _fail("snapshot_bound_exceeded")
    if (len(workspace["deliveries"]) + len(workspace.get("inbox_deliveries", {})) > state_protocol.MAX_DELIVERIES
            or set(workspace["deliveries"]) & set(workspace.get("inbox_deliveries", {}))):
        _fail("snapshot_bound_exceeded")
    tasks = state_protocol._tasks(coordinator, workspace["run_id"])
    for task in tasks.values():
        _object(task, {"task_id", "request_sha256", "status", "attempts", "active_claim"})
        if ((task["status"] in {"claimed", "expired"}) != (task["active_claim"] is not None)
                or task["status"] != "pending" and task["attempts"] < 1):
            _fail("contradictory_task_snapshot")
        if task["active_claim"] is not None:
            _object(task["active_claim"], {"claim_id", "lease_generation"},
                    {"worker_id", "lease_expires_at_ms", "cancel_requested"})
    state_protocol._goal_ready(workspace)
    for record in [workspace["goal"], *workspace["lanes"].values()]:
        if record["workspace_id"] != workspace["workspace_id"] or record["run_id"] != workspace["run_id"]:
            _fail("incompatible_snapshot")
    for key, lane in workspace["lanes"].items():
        if lane["task_id"] != key:
            _fail("incompatible_snapshot")
    if workspace["active_priority"] not in workspace["lanes"]:
        _fail("incomplete_snapshot")
    for key, endpoint in workspace.get("supervisor_endpoints", {}).items():
        protocol.validate_record(endpoint)
        if (endpoint["kind"] != "supervisor_endpoint" or endpoint["endpoint_id"] != key
                or endpoint["goal_id"] != workspace["goal"]["goal_id"]
                or any(endpoint[field] != workspace[field] for field in ("workspace_id", "run_id"))):
            _fail("incompatible_snapshot")
        for field, category in (("receiver_grant_ref", "grant"), ("recovery_fence_ref", "fence")):
            if endpoint[field] is not None and state_protocol._known_ref(workspace, endpoint[field], endpoint_id=key)["category"] != category:
                _fail("incompatible_snapshot")
    for key, message in workspace["messages"].items():
        protocol.validate_record(message)
        supervisor_message = message["kind"] == "supervisor_message"
        operator_message = message["kind"] == "operator_message"
        fields = (("source_task_id",) if supervisor_message else
                  ("destination_task_id",) if operator_message else
                  ("source_task_id", "destination_task_id"))
        if (message["kind"] not in {"message", "operator_message", "supervisor_message"} or message["message_id"] != key
                or message["workspace_id"] != workspace["workspace_id"] or message["run_id"] != workspace["run_id"]
                or message["goal_id"] != workspace["goal"]["goal_id"]
                or any(message[field] not in workspace["lanes"] for field in fields)):
            _fail("incompatible_snapshot")
        if operator_message:
            if (message["sender"]["kind"] != "local_operator"
                    or state_protocol._known_ref(workspace, message["grant_ref"], message["destination_task_id"])["category"] != "grant"):
                _fail("incomplete_snapshot")
        elif supervisor_message:
            endpoint = message["recipient"]["endpoint_id"]
            if endpoint not in workspace.get("supervisor_endpoints", {}) or state_protocol._known_ref(
                    workspace, message["grant_ref"], endpoint_id=endpoint)["category"] != "grant":
                _fail("incomplete_snapshot")
            owner = workspace["supervisor_endpoints"][endpoint]["owner"]
            if owner is None or message["recipient"]["epoch"] > owner["epoch"]:
                _fail("incompatible_snapshot")
        else:
            state_protocol._known_ref(workspace, message["grant_ref"], message["destination_task_id"])
        stream = workspace["streams"].get(message["stream_id"])
        if (type(stream) is not dict or set(stream) != {"sequence", "binding"}
                or type(stream["sequence"]) is not int or stream["sequence"] < message["sequence"]
                or stream["binding"] != protocol.message_stream_binding(message)):
            _fail("incomplete_snapshot")
    unsettled, effects = 0, set()
    for key, delivery in workspace["deliveries"].items():
        protocol.validate_record(delivery)
        if delivery["kind"] != "delivery" or delivery["delivery_id"] != key:
            _fail("incompatible_snapshot")
        message = workspace["messages"].get(delivery["message_id"])
        if message is None:
            _fail("incomplete_snapshot")
        if "parent_delivery_id" not in delivery:
            protocol.validate_delivery_binding(message, delivery)
        else:
            parent = workspace["deliveries"].get(delivery["parent_delivery_id"])
            if parent is None or parent["message_id"] != delivery["message_id"]:
                _fail("incomplete_snapshot")
        if (delivery["workspace_id"] != workspace["workspace_id"] or delivery["run_id"] != workspace["run_id"]
                or delivery["selection"] is not None
                and (delivery["selection"]["task_id"] != message["destination_task_id"]
                     or delivery["selection"]["request_sha256"] != tasks[message["destination_task_id"]]["request_sha256"])):
            _fail("incompatible_snapshot")
        unsettled += bool(admission.outstanding_obligations(delivery["state"], key,
            certainty=delivery["certainty"], inherited_uncertainty=delivery["inherited_uncertainty"]))
        effects.update(field for field, value in delivery["certainty"].items() if value == "unknown")
        effects.update(delivery["inherited_uncertainty"])
    _validate_inbox(workspace)
    for key, evidence in workspace["evidence"].items():
        _object(evidence, {"reference", "category"}, {"task_id", "endpoint_id", "delivery_id", "settlement_for"})
        protocol._reference(evidence["reference"])
        endpoint_scoped = "endpoint_id" in evidence
        subjects = workspace.get("supervisor_endpoints", {}) if endpoint_scoped else workspace["lanes"]
        deliveries = workspace.get("inbox_deliveries", {}) if endpoint_scoped else workspace["deliveries"]
        if (evidence["reference"]["id"] != key or ("task_id" in evidence) == endpoint_scoped
                or evidence.get("endpoint_id" if endpoint_scoped else "task_id") not in subjects
                or evidence["category"] not in {"artifact", "event", "grant", "fence", "adapter_receipt", "observation"}
                or "delivery_id" in evidence and evidence["delivery_id"] not in deliveries):
            _fail("incompatible_snapshot")
        if endpoint_scoped and "delivery_id" in evidence and deliveries[evidence["delivery_id"]]["recipient"]["endpoint_id"] != evidence["endpoint_id"]:
            _fail("incompatible_snapshot")
    for key, item in workspace["assessments"].items():
        _object(item, {"record", "observed_task_status", "event_revision"})
        record = protocol.validate_record(item["record"])
        if record["kind"] != "assessment" or record["assessment_id"] != key or record["task_id"] not in tasks:
            _fail("incompatible_snapshot")
        protocol.validate_goal_binding(workspace["goal"], workspace["lanes"][record["task_id"]], record)
        for reference in record["evidence"]:
            state_protocol._known_ref(workspace, reference, record["task_id"])
        protocol._integer(item["event_revision"], maximum=workspace["revision"])
        if (record["workspace_id"] != workspace["workspace_id"] or record["run_id"] != workspace["run_id"]
                or type(item["observed_task_status"]) is not dict
                or any(key not in tasks or value not in state_protocol._STATUSES
                       for key, value in item["observed_task_status"].items())):
            _fail("incompatible_snapshot")
    for key, decision in workspace["decisions"].items():
        _object(decision, {"decision_id", "goal_revision", "assessment_id", "from_task_id", "to_task_id"})
        if (decision["decision_id"] != key or decision["assessment_id"] not in workspace["assessments"]
                or any(decision[name] not in workspace["lanes"] for name in ("from_task_id", "to_task_id"))):
            _fail("incompatible_snapshot")
    for key, digest in workspace["operations"].items():
        protocol._id(key)
        protocol._digest(digest)
    # Retained send idempotency metadata is internal, never a presentation
    # capability or source of permission. Actual send envelopes are checked by
    # their canonical helper below when event history is supplied.
    for key, item in workspace.get("send_operations", {}).items():
        protocol._id(key)
        _object(item, {"request_sha256", "response"})
        protocol._digest(item["request_sha256"])
        response = item["response"]
        _object(response, {"status", "operation_key", "request_sha256", "message_id", "delivery_id",
                           "stream_sequence", "revision", "delivery_state", "execution_authorized"})
        protocol._id(response["operation_key"])
        protocol._integer(response["stream_sequence"])
        protocol._integer(response["revision"], maximum=workspace["revision"])
        if (key not in workspace["operations"] or response["request_sha256"] != item["request_sha256"]
                or response["status"] != "queued" or response["delivery_state"] != "queued"
                or response["execution_authorized"] is not False
                or response["message_id"] not in workspace["messages"]
                or response["delivery_id"] not in (set(workspace["deliveries"]) | set(workspace.get("inbox_deliveries", {})))):
            _fail("incompatible_snapshot")
    report = coordinator["workspace"]
    _object(report, {"delivery_count", "unsettled_delivery_count", "uncertain_effects", "closure_ready"},
            {"effect_resolution", "inbox", "economics"})
    inbox = state_protocol.inbox_summary(workspace)
    if report.get("inbox", inbox if not workspace.get("supervisor_endpoints") and not workspace.get("inbox_deliveries") else None) != inbox:
        _fail("contradictory_workspace_snapshot")
    if "inbox" in report:
        _object(report["inbox"], set(inbox))
        if any(type(report["inbox"][key]) is not type(value) for key, value in inbox.items()):
            _fail("contradictory_workspace_snapshot")
    if (type(report["delivery_count"]) is not int or report["delivery_count"] != len(workspace["deliveries"])
            or type(report["unsettled_delivery_count"]) is not int or report["unsettled_delivery_count"] != unsettled
            or report["uncertain_effects"] != sorted(effects) or type(report["closure_ready"]) is not bool):
        _fail("contradictory_workspace_snapshot")
    economics = report.get("economics")
    if economics is not None:
        _object(economics, {"enabled", "reservation", "settled_count", "accounting"})
        reservation = economics["reservation"]
        _object(reservation, {"outstanding_bytes", "outstanding_records"})
        if (economics["enabled"] is not True
                or type(economics["settled_count"]) is not int or economics["settled_count"] < 0
                or type(reservation["outstanding_bytes"]) is not int
                or reservation["outstanding_bytes"] < 0
                or type(reservation["outstanding_records"]) is not int
                or reservation["outstanding_records"] < 0
                or len(_json(economics)) > 32 * 1024):
            _fail("incompatible_snapshot")
        _validate_accounting_summary(economics["accounting"])
    resolutions = workspace.get("effect_resolutions", {})
    for key, resolution in resolutions.items():
        _object(resolution, {"delivery_id", "resolution_kind", "qualification", "evidence", "event_revision"})
        after = workspace["deliveries"].get(key)
        if after is None or resolution["delivery_id"] != key:
            _fail("incompatible_snapshot")
        before = json.loads(_json(after))
        before["certainty"].update(spend="unknown", cleanup="unknown")
        protocol.validate_effect_resolution(before, after, resolution["evidence"],
            resolution_kind=resolution["resolution_kind"], qualification=resolution["qualification"])
        protocol._integer(resolution["event_revision"], maximum=workspace["revision"])
    expected_resolution = ({"count": len(resolutions), "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
                            "qualification": "simulated"} if resolutions else None)
    if report.get("effect_resolution") != expected_resolution:
        _fail("contradictory_workspace_snapshot")
    terminal = bool(tasks) and all(task["status"] in _TERMINAL for task in tasks.values())
    closed = coordinator["status"] == "closed"
    unresolved = bool(unsettled) or not inbox["closure_ready"]
    if (closed and (not terminal or unresolved)
            or report["closure_ready"] != (not closed and terminal and not unresolved)
            or coordinator["status"] == "completed" and (not terminal or unresolved)
            or coordinator["status"] == "settlement_required" and (not terminal or not unresolved)
            or "spend" in effects and coordinator["uncertain_spend"] is not True):
        _fail("contradictory_workspace_snapshot")
    return state_protocol.project(workspace, coordinator), tasks


def _capabilities(scope, entries, *, closed, closure_ready):
    if type(entries) not in (tuple, list) or len(entries) > len(ACTIONS):
        _fail("invalid_capabilities")
    supplied = {}
    for item in entries:
        _object(item, {"action", "available", "reason"})
        action = item["action"]
        if (action not in ACTIONS or action in supplied or type(item["available"]) is not bool
                or item["available"] and item["reason"] is not None
                or not item["available"] and item["reason"] not in UNAVAILABLE_REASONS):
            _fail("invalid_capabilities")
        supplied[action] = item
    result = []
    for action in ACTIONS:
        item = dict(supplied.get(action, {"action": action, "available": False, "reason": "not_implemented"}))
        if scope.audience == "public":
            item.update(available=False, reason="read_only_export")
        elif action != "inspect" and closed:
            item.update(available=False, reason="workspace_closed")
        elif action == "close" and not closure_ready:
            item.update(available=False, reason="workspace_unsettled")
        result.append(item)
    return result


def _supervisor(value):
    if value is None:
        return {"mode": "passive", "owner_state": "unknown", "verified_active": False}
    _object(value, {"mode", "owner_state", "verified_active"})
    if (value["mode"] not in {"passive", "supervised"}
            or value["owner_state"] not in {"active", "not_running", "stale", "unknown"}
            or type(value["verified_active"]) is not bool
            or (value["mode"] == "supervised") != value["verified_active"]
            or (value["owner_state"] == "active") != value["verified_active"]):
        _fail("supervisor_observation_unverified")
    return dict(value)


def _present_inbox(workspace, scope, selected):
    """Allowlisted presentation only; caller must validate the canonical state."""
    rows = []
    for delivery in sorted(workspace.get("inbox_deliveries", {}).values(), key=lambda row: row["delivery_id"]):
        message = workspace["messages"][delivery["message_id"]]
        if selected is not None and message["source_task_id"] != selected:
            continue
        unknown = delivery["exposure"] == "unknown" or delivery["inherited_exposure"] == "unknown"
        disposed = delivery["state"] in {"cancelled", "expired", "dead_lettered"}
        label = ("Disposed with unknown exposure" if disposed and unknown else
                 "Simulated context received; prior exposure unknown" if delivery["state"] == "acknowledged" and unknown
                 else INBOX_LABELS[delivery["state"]])
        rows.append({"id": _opaque(scope, "inbox", delivery["delivery_id"]),
                     "source_task_id": _opaque(scope, "task", message["source_task_id"]),
                     "state": delivery["state"], "label": label,
                     "exposure": delivery["exposure"], "inherited_exposure": delivery["inherited_exposure"],
                     "possible_duplicate": delivery["possible_duplicate"],
                     "receipt": ({"level": "supervisor_context_received", "qualification": "simulated"}
                                 if delivery["receipt"] is not None else None),
                     "content_available": False})
    return {"summary": state_protocol.inbox_summary(workspace), "deliveries": rows,
            "endpoints": [{"id": _opaque(scope, "endpoint", endpoint["endpoint_id"]),
                           "label": "Supervisor inbox", "persisted_status": endpoint["status"], "live_owner": "unverified"}
                          for endpoint in sorted(workspace.get("supervisor_endpoints", {}).values(),
                                                 key=lambda row: row["endpoint_id"])]}


def _delivery_reason(delivery):
    """Project finite reason categories; never expose arbitrary terminal IDs."""
    state = delivery["state"]
    if state == "held_for_recovery":
        return delivery["reason"]
    known = {
        ("cancelled", "cancel_queued_context"): "queued_context_cancellation_recorded",
        ("dead_lettered", "dispose_held_context"): "held_context_disposition_recorded",
    }
    if state in {"rejected", "expired", "cancelled", "dead_lettered"}:
        return known.get((state, delivery["reason"]), state + "_reason_unavailable")
    return None


def _delivery_guidance(target, commands, dispositions, linked):
    """Display only host-projected actions; this never authorizes an operation."""
    actions = list(commands["actions_by_delivery"].get(target, ()))
    if dispositions["available"] and any(item["id"] == target for item in dispositions["targets"]):
        actions.append("retain_held_context")
    if linked["available"] and any(item["parent_target"] == target for item in linked["targets"]):
        actions.append("propose_linked_replacement")
    return {"actions": actions, "unavailable_reason": None if actions else (
        "read_only_export" if commands["reason"] == "read_only_export"
        else "no_delivery_action_in_current_scope")}


def _operator_capabilities(workspace, scope, value):
    """Trusted-host display declaration, never a command authorization check."""
    if value is None:
        return {"available": False, "actions_by_delivery": {},
                "reason": "read_only_export" if scope.audience == "public" else "read_only_surface"}
    _object(value, {"available", "actions_by_delivery", "reason"})
    if (type(value["available"]) is not bool or type(value["actions_by_delivery"]) is not dict
            or len(value["actions_by_delivery"]) > state_protocol.MAX_DELIVERIES
            or value["available"] and (scope.audience != "operator" or value["reason"] is not None)
            or not value["available"] and (value["actions_by_delivery"] or value["reason"] not in {
                "read_only_export", "read_only_surface", "command_scope_required", "operator_runtime_unavailable"})):
        _fail("invalid_capabilities")
    targets = {_opaque(scope, "delivery", key): delivery for key, delivery in workspace["deliveries"].items()}
    states = {"cancel_queued_context": "queued", "dispose_held_context": "held_for_recovery"}
    for key, actions in value["actions_by_delivery"].items():
        if (key not in targets or type(actions) is not list
                or not 1 <= len(actions) <= 2 or any(type(action) is not str or action not in states for action in actions)
                or len(set(actions)) != len(actions) or any(targets[key]["state"] != states[action] for action in actions)):
            _fail("invalid_capabilities")
    return json.loads(_json(value))


def _operator_message_capabilities(workspace, scope, value):
    """Validate the opaque target list supplied by the trusted host adapter."""
    if value is None:
        return {"available": False, "targets": [],
                "reason": "read_only_export" if scope.audience == "public" else "message_scope_required"}
    _object(value, {"available", "targets", "reason"})
    if (type(value["available"]) is not bool or type(value["targets"]) is not list
            or len(value["targets"]) > 16
            or value["available"] and (scope.audience != "operator" or value["reason"] is not None)
            or not value["available"] and (value["targets"] or value["reason"] not in {
                "read_only_export", "message_scope_required", "message_runtime_unavailable"})):
        _fail("invalid_message_capabilities")
    seen = set()
    valid_tasks = {_opaque(scope, "task", task_id) for task_id in workspace["lanes"]}
    for item in value["targets"]:
        _object(item, {"id", "task_id", "label", "available"})
        if (type(item["id"]) is not str or not item["id"].startswith("operator_target_")
                or item["id"] in seen or type(item["task_id"]) is not str
                or item["task_id"] not in valid_tasks or type(item["label"]) is not str
                or not item["label"] or len(item["label"]) > 128 or item["available"] is not True):
            _fail("invalid_message_capabilities")
        seen.add(item["id"])
    return json.loads(_json(value))


def _operator_draft_capabilities(scope, value):
    """Project only bounded retention descriptors; never a key or raw target."""
    if value is None:
        return {"available": False, "operator_scope": None, "key_epoch": None,
                "max_text_bytes": 2048, "max_record_bytes": 4096,
                "reason": "read_only_export" if scope.audience == "public" else "message_retention_required"}
    _object(value, {"available", "operator_scope", "key_epoch", "max_text_bytes", "max_record_bytes", "reason"})
    if (type(value["available"]) is not bool or type(value["max_text_bytes"]) is not int
            or type(value["max_record_bytes"]) is not int or value["max_text_bytes"] != 2048
            or value["max_record_bytes"] != 4096
            or value["available"] and (scope.audience != "operator" or value["reason"] is not None
                                        or type(value["operator_scope"]) is not str
                                        or not re.fullmatch(r"[a-f0-9]{64}", value["operator_scope"])
                                        or type(value["key_epoch"]) is not str or not 1 <= len(value["key_epoch"]) <= 64)
            or not value["available"] and (value["operator_scope"] is not None or value["key_epoch"] is not None
                                            or value["reason"] not in {"read_only_export", "message_retention_required"})):
        _fail("invalid_message_retention")
    return json.loads(_json(value))


def _operator_disposition_capabilities(workspace, scope, value):
    """Project bounded retain-hold targets; never expose runtime handles."""
    if value is None:
        return {"available": False, "targets": [],
                "reason": "read_only_export" if scope.audience == "public" else "command_scope_required"}
    _object(value, {"available", "targets", "reason"})
    if (type(value["available"]) is not bool or type(value["targets"]) is not list
            or len(value["targets"]) > state_protocol.MAX_DELIVERIES
            or value["available"] and (scope.audience != "operator" or value["reason"] is not None)
            or not value["available"] and (value["targets"] or value["reason"] not in {
                "read_only_export", "command_scope_required", "operator_runtime_unavailable",
                "command_state_refused"})):
        _fail("invalid_disposition_capabilities")
    valid_tasks = {_opaque(scope, "task", task_id) for task_id in workspace["lanes"]}
    valid_deliveries = {_opaque(scope, "delivery", delivery_id): delivery
                        for delivery_id, delivery in workspace["deliveries"].items()
                        if delivery.get("state") == "held_for_recovery"}
    seen = set()
    reasons = {"awaiting_evidence", "awaiting_authorization", "operator_hold"}
    for item in value["targets"]:
        _object(item, {"id", "task_id", "label", "available", "reasons"})
        delivery = valid_deliveries.get(item["id"]) if type(item.get("id")) is str else None
        expected_task = None
        if delivery is not None:
            message = workspace["messages"].get(delivery.get("message_id"))
            if isinstance(message, dict):
                expected_task = _opaque(scope, "task", message.get("destination_task_id"))
        if (type(item["id"]) is not str or item["id"] in seen or item["id"] not in valid_deliveries
                or type(item["task_id"]) is not str or item["task_id"] not in valid_tasks
                or item["task_id"] != expected_task
                or type(item["label"]) is not str or not item["label"] or len(item["label"]) > 128
                or item["available"] is not True or type(item["reasons"]) is not list
                or not 1 <= len(item["reasons"]) <= 3
                or len(set(item["reasons"])) != len(item["reasons"])
                or any(reason not in reasons for reason in item["reasons"])):
            _fail("invalid_disposition_capabilities")
        seen.add(item["id"])
    return json.loads(_json(value))


def _operator_linked_replacement_capabilities(workspace, scope, value):
    """Project opaque linked-replacement targets for an operator only.

    The host adapter remains the authority for binding a proposal.  The view
    validates the public shape and rejects any target that is not a stable
    presentation handle.  No raw delivery, task, recipient or authority
    reference crosses this boundary.
    """
    if value is None:
        return {"available": False, "targets": [],
                "reason": "read_only_export" if scope.audience == "public" else "linked_scope_required"}
    _object(value, {"available", "targets", "reason"})
    if (type(value["available"]) is not bool or type(value["targets"]) is not list
            or len(value["targets"]) > state_protocol.MAX_DELIVERIES
            or value["available"] and (scope.audience != "operator" or value["reason"] is not None)
            or not value["available"] and (value["targets"] or value["reason"] not in {
                "read_only_export", "linked_scope_required", "operator_runtime_unavailable"})):
        _fail("invalid_linked_capabilities")
    seen = set()
    for item in value["targets"]:
        _object(item, {"parent_target", "recipient_target", "available"})
        parent = item.get("parent_target")
        recipient = item.get("recipient_target")
        if (type(parent) is not str or type(recipient) is not str
                or not parent or not recipient or len(parent) > 256 or len(recipient) > 256
                or item["available"] is not True
                or (parent, recipient) in seen):
            _fail("invalid_linked_capabilities")
        seen.add((parent, recipient))
    return json.loads(_json(value))


def _detail_capabilities(value):
    """Validate the finite typed navigation projection supplied by the host."""
    if value is None:
        return {"available": False, "targets": [], "reason": "detail_scope_required"}
    _object(value, {"available", "targets"}, {"reason"})
    if type(value["available"]) is not bool or type(value["targets"]) is not list:
        _fail("invalid_detail_capabilities")
    if not value["available"]:
        if value["targets"] or value.get("reason") != "detail_scope_required":
            _fail("invalid_detail_capabilities")
        return {"available": False, "targets": [], "reason": "detail_scope_required"}
    if len(value["targets"]) > 256 or value.get("reason") is not None:
        _fail("invalid_detail_capabilities")
    seen = set()
    for item in value["targets"]:
        _object(item, {"id", "task_id", "source_kind", "state", "label", "revision",
                       "body_available"})
        if (type(item["id"]) is not str or type(item["task_id"]) is not str
                or item["source_kind"] not in {"workspace_assessment", "workspace_decision",
                    "council_review", "deliberation_decision", "deliberation_pending",
                    "workspace_evidence"}
                or item["state"] not in {"pending", "active", "completed", "blocked", "revoked", "stale"}
                or type(item["label"]) is not str or not 1 <= len(item["label"]) <= 160
                or not item["label"].isprintable() or type(item["revision"]) is not int
                or item["revision"] < 0 or type(item["body_available"]) is not bool
                or item["id"] in seen):
            _fail("invalid_detail_capabilities")
        seen.add(item["id"])
    return json.loads(_json(value))


def project_workspace(workspace, coordinator, *, scope, events=(), capabilities=(),
                      supervisor=None, cursor=None, limit=MAX_PAGE, selected_task=None,
                      expected_snapshot=None, anchor=None, operator_commands=None,
                      operator_messages=None, operator_drafts=None,
                      operator_dispositions=None, operator_linked_replacements=None,
                      event_times=None, workspace_details=None):
    """Return an allowlisted snapshot and one revision-bound timeline page.

    ``selected_task`` is an opaque task ID from a prior view. ``events`` contains
    optional canonical workspace envelopes, not raw journals or worker text.
    Missing event history is explicit. When supplied, ``event_times`` contains
    allowlisted outer-journal timestamps bound to the same operation keys; these
    are journal-record times, never provider or acknowledgement times. ``anchor`` is an opaque
    previously displayed event handle bound to this audience/text/task scope. It
    locates one current bounded page, never grants authority or relaxes a cursor's
    snapshot binding. Cursor and anchor selectors are mutually exclusive.
    """
    try:
        _check_scope(scope, workspace)
        projected, tasks = _validate(workspace, coordinator)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            _fail("page_bound_exceeded")
        if anchor is not None and cursor is not None:
            _fail("incompatible_page_selectors")
        if type(events) not in (tuple, list) or len(events) > state_protocol.MAX_EVENTS:
            _fail("history_bound_exceeded")
        owner = _supervisor(supervisor)
        report = coordinator["workspace"]
        economics = report.get("economics")
        controls = _capabilities(scope, capabilities, closed=coordinator["status"] == "closed",
                                 closure_ready=report["closure_ready"])
        command_capabilities = _operator_capabilities(workspace, scope, operator_commands)
        message_capabilities = _operator_message_capabilities(workspace, scope, operator_messages)
        draft_capabilities = _operator_draft_capabilities(scope, operator_drafts)
        disposition_capabilities = _operator_disposition_capabilities(workspace, scope, operator_dispositions)
        linked_capabilities = _operator_linked_replacement_capabilities(
            workspace, scope, operator_linked_replacements)
        detail_capabilities = _detail_capabilities(workspace_details)
        task_id = lambda value: _opaque(scope, "task", value)
        selected = None
        if selected_task is not None:
            selected = next((key for key in workspace["lanes"] if task_id(key) == selected_task), None)
            if selected is None:
                _fail("task_scope_refused")
        if event_times is not None and not isinstance(event_times, dict):
            _fail("invalid_event_timing")
        supplied_times = {} if event_times is None else dict(event_times)
        if any(type(key) is not str for key in supplied_times):
            _fail("invalid_event_timing")
        for value in supplied_times.values():
            if (type(value) not in (int, float) or isinstance(value, bool)
                    or not math.isfinite(value) or not 0 <= value <= 9_999_999_999_999.0):
                _fail("invalid_event_timing")
        history, seen, seen_revisions, event_keys = [], set(), set(), set()
        for event in events:
            digest = state_protocol._event(event)
            if event["event"] == "workspace_message_sent":
                admission.canonical_send_event(event)
            elif event["event"] == "workspace_operator_message_sent":
                admission.canonical_operator_send_event(event)
                if workspace.get("send_operations", {}).get(event["operation_key"]) != {
                        "request_sha256": event["payload"]["request"]["request_sha256"],
                        "response": admission.operator_send_response(event)}:
                    _fail("history_snapshot_mismatch")
            elif event["event"] == "workspace_supervisor_message_sent":
                admission.canonical_supervisor_send_event(event)
                if workspace.get("send_operations", {}).get(event["operation_key"]) != {
                        "request_sha256": event["payload"]["request"]["request_sha256"], "response": admission.send_response(event)}:
                    _fail("history_snapshot_mismatch")
            elif event["event"] in _INBOX_COMMAND_EVENTS:
                admission.canonical_inbox_command_event(event)
                if workspace.get("inbox_operations", {}).get(event["operation_key"]) != {
                        "request_sha256": event["payload"]["request_sha256"], "consumer": event["payload"]["consumer"],
                        "response": admission.inbox_response(event)}:
                    _fail("history_snapshot_mismatch")
            elif event["event"] == admission.OPERATOR_DISPOSITION_EVENT:
                admission.canonical_operator_disposition_event(event)
            if (event["event"] not in EVENT_LABELS or event["workspace_id"] != scope.workspace_id
                    or event["run_id"] != scope.run_id or workspace["operations"].get(event["operation_key"]) != digest
                    or event["expected_revision"] >= workspace["revision"]):
                _fail("history_snapshot_mismatch")
            event_keys.add(event["operation_key"])
            if event["operation_key"] in seen:
                continue
            if event["expected_revision"] in seen_revisions:
                _fail("history_snapshot_mismatch")
            seen.add(event["operation_key"])
            seen_revisions.add(event["expected_revision"])
            payload = event["payload"]
            related = set()
            for key in ("task_id", "from_task_id", "to_task_id"):
                if key in payload:
                    related.add(payload[key])
            for key in ("lane", "assessment", "claim", "selection"):
                if type(payload.get(key)) is dict and "task_id" in payload[key]:
                    related.add(payload[key]["task_id"])
            message = payload.get("message")
            delivery = payload.get("delivery")
            if type(delivery) is dict:
                message = workspace["messages"].get(delivery.get("message_id"))
            if type(message) is dict:
                related.update(message[key] for key in ("source_task_id", "destination_task_id") if key in message)
            if selected is not None and selected not in related:
                continue
            recorded_at = supplied_times.get(event["operation_key"])
            history.append({"id": _opaque(scope, "event", event["operation_key"]),
                            "anchor": _opaque(scope, "anchor", [scope.audience, scope.allow_text,
                                       selected_task, event["operation_key"]]),
                            "revision": event["expected_revision"] + 1,
                            "kind": event["event"], "label": EVENT_LABELS[event["event"]],
                            "origin": ("local_operator" if event["event"] == "workspace_operator_message_sent"
                                        else "supervisor" if event["event"] == "workspace_supervisor_message_sent"
                                        else "worker" if event["event"] in {"workspace_message_sent", "workspace_message_admitted"}
                                        else None),
                            "task_ids": sorted(task_id(key) for key in related if key in workspace["lanes"]),
                            "occurred_at": recorded_at,
                            "timestamp_reason": "journal_recorded" if recorded_at is not None
                            else "not_in_event_envelope"})
        if supplied_times.keys() - event_keys:
            _fail("event_timing_mismatch")
        history.sort(key=lambda item: (item["revision"], item["id"]))
        binding = [workspace, coordinator, sorted(seen), controls, owner, command_capabilities,
                   scope.audience, scope.allow_text, selected_task, limit, draft_capabilities,
                   disposition_capabilities,
                   linked_capabilities, message_capabilities, detail_capabilities,
                   sorted(supplied_times.items())]
        snapshot = _opaque(scope, "snapshot", binding)
        if expected_snapshot is not None and expected_snapshot != snapshot:
            _fail("stale_snapshot")
        offset = 0
        if anchor is not None:
            matches = [index for index, item in enumerate(history)
                       if hmac.compare_digest(item["anchor"], anchor)] if type(anchor) is str else []
            if not matches:
                _fail("anchor_scope_or_history_refused")
            offset = (matches[0] // limit) * limit
        if cursor is not None:
            # Compare against the bounded set of valid page starts; tokens
            # contain no encoded private identities and need no JSON decoding.
            matches = [index for index in range(0, len(history), limit)
                       if hmac.compare_digest(_opaque(scope, "cursor", [snapshot, index]), cursor)] if type(cursor) is str else []
            if not matches:
                _fail("stale_or_invalid_cursor")
            offset = matches[0]
        rows = []
        for index, lane in enumerate(sorted(projected["lanes"], key=lambda row: row["task_id"]), 1):
            task = tasks[lane["task_id"]]
            row = {"id": task_id(lane["task_id"]), "label": "Task " + str(index), "role": lane["role"],
                   "status": task["status"], "attempt_count": task["attempts"],
                   "claim_state": "present" if task["active_claim"] else "none",
                   "is_priority": lane["task_id"] == workspace["active_priority"],
                   "agent": {"logical_label": "Unspecified", "worker_label": "Unavailable",
                             "requested_model": None, "served_model": None,
                             "model_reason": "authoritative_identity_not_supplied"}}
            if scope.allow_text:
                original = workspace["lanes"][lane["task_id"]]
                row["text"] = {key: original[key] for key in ("outcome", "scope", "return_condition")}
            rows.append(row)
        deliveries = []
        for delivery in sorted(workspace["deliveries"].values(), key=lambda row: row["delivery_id"]):
            message = workspace["messages"][delivery["message_id"]]
            if selected is not None and selected not in {message.get("source_task_id"), message["destination_task_id"]}:
                continue
            resolution = workspace.get("effect_resolutions", {}).get(delivery["delivery_id"])
            operator_message = message["kind"] == "operator_message"
            deliveries.append({"id": _opaque(scope, "delivery", delivery["delivery_id"]),
                "message_id": _opaque(scope, "message", delivery["message_id"]),
                "source_task_id": None if operator_message else task_id(message["source_task_id"]),
                "destination_task_id": task_id(message["destination_task_id"]),
                "sender": ({"kind": "local_operator", "label": "Local operator"} if operator_message else
                           {"kind": "supervisor", "label": "Supervisor"} if message["kind"] == "supervisor_message" else
                           {"kind": "worker", "label": "Worker"}),
                "state": delivery["state"], "label": DELIVERY_LABELS[delivery["state"]],
                "acknowledgement": delivery["ack_level"], "certainty": dict(delivery["certainty"]),
                "inherited_uncertainty": list(delivery["inherited_uncertainty"]),
                "reason": _delivery_reason(delivery),
                "action_guidance": _delivery_guidance(
                    _opaque(scope, "delivery", delivery["delivery_id"]), command_capabilities,
                    disposition_capabilities, linked_capabilities),
                "stream_id": _opaque(scope, "stream", message["stream_id"]),
                "sequence": message["sequence"],
                "turn_id": _opaque(scope, "turn", delivery["selection"]) if delivery["selection"] else None,
                "automatic_retry": False, "provider_billing_verified": False,
                "effect_resolution": ({"resolution_kind": resolution["resolution_kind"],
                                       "qualification": resolution["qualification"]} if resolution else None),
                "content": {"available": False, "reason": "separate_content_scope_required"}})
        goal = {"id": _opaque(scope, "goal", workspace["goal"]["goal_id"]),
                "revision": workspace["goal"]["revision"], "criteria_count": len(workspace["goal"]["criteria"]),
                "text_available": scope.allow_text}
        if scope.allow_text:
            goal["objective"] = workspace["goal"]["objective"]
            goal["criteria"] = [{"id": _opaque(scope, "criterion", item["criterion_id"]),
                                  "description": item["description"], "evidence_requirement": item["evidence_requirement"]}
                                 for item in workspace["goal"]["criteria"]]
        holds = [{"id": _opaque(scope, "hold", item["assessment_id"]),
                  "source_task_id": task_id(item["source_task_id"]),
                  "affected_task_ids": [task_id(key) for key in item["affected_task_ids"]],
                  "label": "Explicit hold remains unresolved"} for item in sorted(projected["holds"], key=lambda row: row["assessment_id"])]
        decisions = [{"id": _opaque(scope, "decision", item["decision_id"]),
                      "from_task_id": task_id(item["from_task_id"]), "to_task_id": task_id(item["to_task_id"]),
                      "goal_revision": item["goal_revision"]} for item in sorted(projected["decisions"], key=lambda row: row["decision_id"])]
        assessments = [{"id": _opaque(scope, "assessment", item["record"]["assessment_id"]),
            "task_id": task_id(item["record"]["task_id"]), "kind": "supervisor_assessment",
            "revision": item["event_revision"], "disposition": item["record"]["disposition"],
            "next_decision": item["record"]["next_decision"],
            "criterion_results": [{"id": _opaque(scope, "criterion", criterion["criterion_id"]),
                                   "result": criterion["result"], "evidence_count": len(criterion["evidence_ids"])}
                                  for criterion in item["record"]["criterion_results"]]}
            for item in sorted(projected["assessments"], key=lambda row: (row["event_revision"], row["record"]["assessment_id"]))]
        return {"schema": SCHEMA, "audience": scope.audience, "workspace_id": _opaque(scope, "workspace", scope.workspace_id),
            "snapshot": snapshot, "workspace_revision": workspace["revision"], "freshness": "snapshot_only",
            "mode": owner, "goal": goal, "tasks": rows, "selected_task_id": selected_task,
            "priority_task_id": task_id(workspace["active_priority"]), "holds": holds, "decisions": decisions,
            "assessments": assessments, "inbox": _present_inbox(workspace, scope, selected),
            "economics": json.loads(_json(economics)) if economics is not None else None,
            "operator_commands": command_capabilities,
            "operator_messages": message_capabilities,
            "operator_drafts": draft_capabilities,
            "operator_dispositions": disposition_capabilities,
            "operator_linked_replacements": linked_capabilities,
            "details": detail_capabilities,
            "deliveries": deliveries, "closure": {"state": coordinator["status"], "closed": coordinator["status"] == "closed",
                "ready": report["closure_ready"], "unsettled_delivery_count": report["unsettled_delivery_count"],
                "uncertain_effects": list(report["uncertain_effects"]), "uncertain_spend": coordinator["uncertain_spend"]},
            "controls": controls, "queue_evaluation": "Queue evaluation is unavailable on this surface. Queueing context or reopening this workspace does not start workers.",
            "evidence": {"count": len(workspace["evidence"]), "bodies_available": False, "reason": "separate_evidence_scope_required"},
            "timeline": {"items": history[offset:offset + limit], "limit": limit,
                "history_complete": len(seen) == len(workspace["operations"]),
                "unavailable_reason": None if events else "event_history_not_supplied",
                "next_cursor": _opaque(scope, "cursor", [snapshot, offset + limit]) if offset + limit < len(history) else None}}
    except WorkspaceViewError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WorkspaceViewError("incompatible_snapshot") from None

