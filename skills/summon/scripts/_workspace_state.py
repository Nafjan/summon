"""Pure projection of previously admitted coordinator-owned workspace events.

No store, scheduler, authentication or capabilities live here. The coordinator
must verify events/evidence and supply its own task view at EACH replay position.
That view remains the only task lifecycle authority. Stored status observations
describe an assessment's historical context, never current task terminal state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

import _workspace_protocol as protocol

FEATURE_EVENT = "workspace_feature"
FEATURE_VERSION = 1
ATOMIC_TURN_EVENT = "workspace_turn_admitted"
MAX_EVENTS = 2048
MAX_EVIDENCE = 256
MAX_MESSAGES = 32
MAX_DELIVERIES = 128
MAX_ASSESSMENTS = 64
MAX_DECISIONS = 64
MAX_ENDPOINTS = 1
# Matches the existing coordinator's MAX_TASKS; not an active-worker promise.
MAX_COORDINATOR_TASKS = 1024
PROOF_CATEGORIES = {
    "durable_admission": {"event"}, "selection_record": {"event"},
    "whole_message_fit": {"event", "observation"}, "current_grant": {"grant"},
    "recipient_owner_fence": {"fence"}, "capacity_reservation": {"fence"},
    "physical_attempt_reservation": {"fence"}, "durable_launch_intent": {"event"},
    "adapter_receipt": {"adapter_receipt"}, "ack_receipt": {"adapter_receipt"},
    "no_contact_receipt": {"adapter_receipt", "event"}, "cleanup_evidence": {"fence"},
    "hold_observation": {"observation", "event"}, "validated_refusal": {"event"},
    "expiry_observation": {"observation"}, "authorized_cancellation": {"grant"},
    "authenticated_disposition": {"event"}, "remaining_authority": {"grant"},
    "fresh_attempt_grant": {"grant"}, "physical_fence": {"fence"},
    "recipient_transfer_authority": {"grant"},
    "fixed_execution_scope": {"observation"}, "owned_child_cleanup": {"fence"},
}

# Finite proof-registration obligations for each reachable delivery target.
# These slots reserve journal space, never future receipt contents or authority.
SETTLEMENT_ROLES = {
    "queued": frozenset({"durable_admission"}),
    "included_in_attempt": frozenset({"selection_record", "whole_message_fit"}),
    "submission_started": frozenset({"current_grant", "recipient_owner_fence",
        "capacity_reservation", "physical_attempt_reservation", "durable_launch_intent"}),
    "submitted": frozenset({"adapter_receipt"}),
    "acknowledged": frozenset({"ack_receipt"}),
    "not_submitted": frozenset({"no_contact_receipt", "cleanup_evidence"}),
    "held_for_recovery": frozenset({"hold_observation"}),
    "rejected": frozenset({"validated_refusal"}),
    "expired": frozenset({"expiry_observation"}),
    "cancelled": frozenset({"authorized_cancellation"}),
    "dead_lettered": frozenset({"authenticated_disposition"}),
    "effects_resolved": frozenset({"fixed_execution_scope", "owned_child_cleanup"}),
}


def settlement_targets(delivery_state: str) -> frozenset[str]:
    """All still-reachable targets in the fixed acyclic attempt lifecycle."""
    if type(delivery_state) is not str or delivery_state not in protocol.STATES:
        _fail("unknown settlement state")
    targets, pending = set(), [delivery_state]
    while pending:
        current = pending.pop()
        for source, target in protocol.EDGES:
            if source == current and target not in targets:
                targets.add(target)
                pending.append(target)
    return frozenset(targets)


def settlement_record_counts(state: dict) -> tuple[int, int]:
    """Remaining transition and unfulfilled evidence records, without effects.

    Reserve every reachable branch once; transition to one branch releases the
    unreachable alternatives. Tagged evidence fulfills exactly its own slot.
    A linked child is a new delivery with a new budget; parent facts stay intact.
    """
    filled = {tuple(item["settlement_for"][key] for key in ("delivery_id", "target_state", "role"))
              for item in state["evidence"].values() if "settlement_for" in item and "delivery_id" in item["settlement_for"]}
    transitions = registrations = 0
    for delivery_id, delivery in state["deliveries"].items():
        targets = delivery_settlement_targets(delivery)
        transitions += len(targets)
        registrations += sum((delivery_id, target, role) not in filled
                             for target in targets for role in SETTLEMENT_ROLES[target])
    inbox_events, inbox_proofs = inbox_record_counts(state)
    return transitions + inbox_events, registrations + inbox_proofs


def delivery_settlement_targets(delivery: dict) -> frozenset[str]:
    """Reserve explicit fixture resolution separately, while ack is reachable.

    Eligibility for this potential budget is not fixture qualification. Actual
    source observations and the typed resolution operation remain mandatory.
    """
    targets = set(settlement_targets(delivery["state"]))
    if ("parent_delivery_id" not in delivery and not delivery["inherited_uncertainty"]
            and ("acknowledged" in targets or (
                delivery["state"] == "acknowledged" and delivery["certainty"] == {
                    "contact": "occurred", "spend": "unknown", "cleanup": "unknown"}))):
        targets.add("effects_resolved")
    return frozenset(targets)


def _settlement_registration(state: dict, payload: dict) -> bool:
    """Check one immutable slot binding; True means an identical no-op."""
    if "endpoint_id" in payload:
        return _inbox_registration(state, payload)
    slot = payload["settlement_for"]
    _obj(slot, {"delivery_id", "target_state", "role"})
    delivery_id = slot["delivery_id"]
    _id(delivery_id)
    delivery = state["deliveries"].get(delivery_id)
    if delivery is None or payload.get("delivery_id") != delivery_id:
        _fail("settlement evidence must bind its delivery")
    target, role = slot["target_state"], slot["role"]
    if type(target) is not str or type(role) is not str or role not in SETTLEMENT_ROLES.get(target, ()):
        _fail("unsupported settlement target or proof role")
    # Check existing slot first so an exact registration is a no-op after its
    # transition too; a different proof can never reclaim that consumed credit.
    for previous in state["evidence"].values():
        if previous.get("settlement_for") == slot:
            if previous != payload:
                _fail("settlement proof slot is immutable")
            return True
    if target not in delivery_settlement_targets(delivery):
        _fail("settlement target is no longer reachable")
    if payload["category"] not in PROOF_CATEGORIES[role]:
        _fail("settlement evidence category differs from proof role")
    if role == "current_grant" and payload["reference"] != delivery["grant_ref"]:
        _fail("settlement current grant differs from delivery")
    return False


def _settlement_transition(state: dict, delivery_id: str, target: str, evidence: dict) -> None:
    """A reserved proof cannot be substituted after consuming its allowance."""
    for item in state["evidence"].values():
        slot = item.get("settlement_for")
        if slot and slot.get("delivery_id") == delivery_id and slot["target_state"] == target:
            if evidence.get(slot["role"]) != item["reference"]:
                _fail("transition substitutes a reserved settlement proof")
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_STATUSES = {"pending", "claimed", "completed", "failed", "cancelled", "blocked", "expired", "indeterminate"}


class WorkspaceStateError(ValueError):
    """Invalid replay input; caller must refuse mutation, never repair by guessing."""


def _fail(message):
    raise WorkspaceStateError(message)


def _obj(value, keys, optional=frozenset()):
    if type(value) is not dict or set(value) - set(keys) - set(optional) or not set(keys) <= set(value):
        _fail("invalid event fields")


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("invalid id")


def _int(value, minimum=1):
    if type(value) is not int or not minimum <= value <= protocol.MAX_INT:
        _fail("invalid integer")


def _ref(value):
    _obj(value, {"id", "sha256"})
    _id(value["id"])
    if type(value["sha256"]) is not str or not _DIGEST.fullmatch(value["sha256"]):
        _fail("invalid evidence digest")


def _canonical(value):
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkspaceStateError("invalid serialized event") from exc
    if len(data) > protocol.MAX_RECORD_BYTES:
        _fail("complete workspace event exceeds byte bound")
    return data


def _event(value):
    _obj(value, {"event", "protocol", "workspace_id", "run_id", "operation_key", "expected_revision", "payload"})
    for key in ("workspace_id", "run_id", "operation_key"):
        _id(value[key])
    _int(value["expected_revision"], 0)
    if value["protocol"] not in {protocol.PROTOCOL, protocol.PROTOCOL_V2} \
            or type(value["event"]) is not str or type(value["payload"]) is not dict:
        _fail("unsupported workspace event version or type")
    return hashlib.sha256(_canonical(value)).hexdigest()


def _tasks(view, run_id):
    # This is the narrow read-only input seam to SwarmCoordinator.status().
    # Other coordinator projection fields are ignored, never copied as authority.
    if (type(view) is not dict or view.get("run_id") != run_id or type(view.get("tasks")) is not list
            or len(view["tasks"]) > MAX_COORDINATOR_TASKS):
        _fail("coordinator task view required")
    result = {}
    for task in view["tasks"]:
        if type(task) is not dict:
            _fail("invalid coordinator task")
        _id(task.get("task_id"))
        if task["task_id"] in result or type(task.get("status")) is not str or task["status"] not in _STATUSES:
            _fail("duplicate task or unsupported coordinator status")
        if type(task.get("request_sha256")) is not str or not _DIGEST.fullmatch(task["request_sha256"]):
            _fail("invalid task request binding")
        _int(task.get("attempts"), 0)
        claim = task.get("active_claim")
        if claim is not None:
            if type(claim) is not dict:
                _fail("invalid active claim")
            _id(claim.get("claim_id"))
            _int(claim.get("lease_generation"))
        result[task["task_id"]] = task
    return result


def _known_ref(state, reference, task_id=None, delivery_id=None, *, endpoint_id=None):
    _ref(reference)
    evidence = state["evidence"].get(reference["id"])
    if evidence is None or evidence["reference"] != reference:
        _fail("dangling or conflicting evidence reference")
    if ("task_id" in evidence) == ("endpoint_id" in evidence) or (task_id is not None and endpoint_id is not None):
        _fail("evidence requires exactly one subject")
    if task_id is not None and evidence.get("task_id") != task_id:
        _fail("evidence task scope mismatch")
    if endpoint_id is not None and evidence.get("endpoint_id") != endpoint_id:
        _fail("evidence endpoint scope mismatch")
    if delivery_id is not None and evidence.get("delivery_id") != delivery_id:
        _fail("evidence delivery binding mismatch")
    return evidence


def _goal_lane(state, task_id):
    lane = state["lanes"].get(task_id)
    if lane is None:
        _fail("unknown lane task")
    return lane


def _goal_ready(state):
    if state["goal"] is None:
        _fail("goal plan not defined")
    protocol.validate_goal_plan(state["goal"], list(state["lanes"].values()))


def _admit_message(result, payload, *, operator=False):
    """Shared canonical initial admission; callers never bypass accepted state."""
    _obj(payload, {"message", "delivery"})
    _goal_ready(result)
    message = _record(result, payload["message"], "operator_message" if operator else "message")
    delivery = _record(result, payload["delivery"], "delivery")
    if message["goal_id"] != result["goal"]["goal_id"]:
        _fail("message goal mismatch")
    for key in (("destination_task_id",) if operator else ("source_task_id", "destination_task_id")):
        _goal_lane(result, message[key])
    if _known_ref(result, message["grant_ref"], message["destination_task_id"])["category"] != "grant":
        _fail("message requires a registered grant reference")
    if delivery["state"] != "accepted":
        _fail("initial delivery must be accepted")
    protocol.validate_delivery_binding(message, delivery)
    for key in ("reply_to", "causation_id"):
        if key in message and message[key] not in result["messages"]:
            _fail("dangling logical message parent")
    stream = result["streams"].get(message["stream_id"])
    binding = protocol.message_stream_binding(message)
    if message["sequence"] != (stream["sequence"] + 1 if stream else 1) or (stream and stream["binding"] != binding):
        _fail("stream ordering or immutable binding conflict")
    _new(result["messages"], message["message_id"], message, MAX_MESSAGES)
    _new_delivery(result, delivery)
    result["streams"][message["stream_id"]] = {"sequence": message["sequence"], "binding": binding}


def _new_delivery(state, delivery):
    inbox = state.get("inbox_deliveries", {})
    if delivery["delivery_id"] in state["deliveries"] or delivery["delivery_id"] in inbox:
        _fail("delivery ID already exists in another mode")
    if len(state["deliveries"]) + len(inbox) >= MAX_DELIVERIES:
        _fail("combined delivery capacity exhausted")
    target = state.setdefault("inbox_deliveries", {}) if delivery["kind"] == "inbox_delivery" else state["deliveries"]
    _new(target, delivery["delivery_id"], delivery, MAX_DELIVERIES)


def _endpoint(state, endpoint_id):
    _id(endpoint_id)
    endpoint = state.get("supervisor_endpoints", {}).get(endpoint_id)
    if endpoint is None:
        _fail("unknown supervisor endpoint")
    return endpoint


def inbox_targets(delivery):
    targets, pending = set(), [delivery["state"]]
    while pending:
        current = pending.pop()
        for source, target in protocol.INBOX_EDGES:
            if source == current and target not in targets:
                targets.add(target); pending.append(target)
    return targets


def endpoint_targets(endpoint):
    return {"revoked", "retired"} if endpoint["status"] == "active" else {"retired"} if endpoint["status"] == "revoked" else set()


ENDPOINT_ROLES = {"revoked": frozenset({"owner_revocation"}), "retired": frozenset({"recovery_fence"})}


def inbox_record_counts(state):
    transitions = registrations = 0
    slots = [entry["settlement_for"] for entry in state["evidence"].values() if "settlement_for" in entry]
    for delivery in state.get("inbox_deliveries", {}).values():
        for target in inbox_targets(delivery):
            transitions += 1
            for role in protocol.INBOX_ROLES[target]:
                if {"delivery_id": delivery["delivery_id"], "target_state": target, "role": role} not in slots:
                    registrations += 1
    for endpoint in state.get("supervisor_endpoints", {}).values():
        for target in endpoint_targets(endpoint):
            transitions += 1
            for role in ENDPOINT_ROLES[target]:
                if {"endpoint_id": endpoint["endpoint_id"], "epoch": endpoint["owner"]["epoch"], "target_state": target, "role": role} not in slots:
                    registrations += 1
    return transitions, registrations


def _inbox_registration(state, payload):
    slot = payload["settlement_for"]
    if "delivery_id" in slot:
        _obj(slot, {"delivery_id", "target_state", "role"})
        delivery = state.get("inbox_deliveries", {}).get(slot["delivery_id"])
        if delivery is None or payload.get("delivery_id") != slot["delivery_id"] or payload["endpoint_id"] != delivery["recipient"]["endpoint_id"]:
            _fail("inbox proof delivery/endpoint mismatch")
        targets, roles = inbox_targets(delivery), protocol.INBOX_ROLES
        if slot["role"] == "receiver_grant" and payload["reference"] != delivery["grant_ref"]:
            _fail("inbox receiving grant differs")
    else:
        _obj(slot, {"endpoint_id", "epoch", "target_state", "role"})
        endpoint = _endpoint(state, slot["endpoint_id"])
        if (payload["endpoint_id"] != slot["endpoint_id"] or "delivery_id" in payload
                or endpoint["owner"] is None or type(slot["epoch"]) is not int or slot["epoch"] != endpoint["owner"]["epoch"]):
            _fail("endpoint control slot owner differs")
        targets, roles = endpoint_targets(endpoint), ENDPOINT_ROLES
    target, role = slot["target_state"], slot["role"]
    if type(target) is not str or type(role) is not str or role not in roles.get(target, ()):
        _fail("invalid inbox/control proof slot")
    for previous in state["evidence"].values():
        if previous.get("settlement_for") == slot:
            if previous != payload:
                _fail("inbox/control proof slot is immutable")
            return True
    if target not in targets or payload["category"] != protocol.INBOX_PROOF_CATEGORIES[role]:
        _fail("inbox/control proof slot is unreachable or wrong category")
    return False


def _inbox_refs(state, evidence, endpoint_id, *, delivery_id=None):
    if type(evidence) is not dict:
        _fail("inbox proof map required")
    for role, reference in evidence.items():
        if role not in protocol.INBOX_PROOF_CATEGORIES:
            _fail("unknown inbox proof role")
        item = _known_ref(state, reference, endpoint_id=endpoint_id)
        if item["category"] != protocol.INBOX_PROOF_CATEGORIES[role]:
            _fail("inbox proof category differs")
        if delivery_id is not None and role not in {"receiver_grant", "fresh_receiving_grant", "recovery_fence"} and item.get("delivery_id") != delivery_id:
            _fail("inbox proof is not delivery-bound")


def inbox_summary(state):
    deliveries = list(state.get("inbox_deliveries", {}).values())
    terminal = {"acknowledged", "cancelled", "expired", "dead_lettered"}
    unknown = lambda d: d["exposure"] == "unknown" or d["inherited_exposure"] == "unknown"
    unsettled = sum(d["state"] not in terminal for d in deliveries)
    endpoint_pending = sum(bool(endpoint_targets(e)) for e in state.get("supervisor_endpoints", {}).values())
    return {"total": len(deliveries), "unsettled": unsettled,
            "received": sum(d["state"] == "acknowledged" for d in deliveries), "unknown_exposure": sum(map(unknown, deliveries)),
            "disposed_with_unknown_exposure": sum(d["state"] in {"cancelled", "expired", "dead_lettered"} and unknown(d) for d in deliveries),
            "possible_duplicate": sum(d["possible_duplicate"] for d in deliveries), "active_endpoints": endpoint_pending,
            "closure_ready": not unsettled and not endpoint_pending}


def _inbox_slots_used(state, evidence, *, delivery_id=None, endpoint=None, target):
    """Registered reserved slots are immutable; a different proof cannot spend them."""
    for role, reference in evidence.items():
        slot = ({"delivery_id": delivery_id, "target_state": target, "role": role} if delivery_id else
                {"endpoint_id": endpoint["endpoint_id"], "epoch": endpoint["owner"]["epoch"], "target_state": target, "role": role})
        for item in state["evidence"].values():
            if item.get("settlement_for") == slot and item["reference"] != reference:
                _fail("inbox transition conflicts with reserved proof slot")


def _inbox_source(result, payload, tasks):
    claim, scope = payload["source"]["claim"], payload["send_scope"]
    task = tasks.get(claim["task_id"])
    if (task is None or task["status"] != "claimed" or task["request_sha256"] != claim["request_sha256"]
            or task["attempts"] != claim["attempt"] or task["active_claim"] is None
            or any(task["active_claim"].get(key) != claim[key] for key in ("claim_id", "lease_generation"))):
        _fail("supervisor send source claim is not current")
    for ref in (scope["reference"], scope["channel_grant_ref"]):
        if _known_ref(result, ref, claim["task_id"])["category"] != "grant":
            _fail("supervisor send needs scoped source grants")


def validate_inbox_offer_order(state, delivery):
    """Endpoint recovery fences cover every sender; stream order stays local.

    This pure guard consumes already validated replay records. It does not
    admit endpoints, authenticate a consumer, or resolve a recovery fence.
    """
    message = state["messages"][delivery["message_id"]]
    endpoint_id = delivery["recipient"]["endpoint_id"]
    for prior in state.get("inbox_deliveries", {}).values():
        if prior["delivery_id"] == delivery["delivery_id"] or prior["recipient"]["endpoint_id"] != endpoint_id:
            continue
        if prior["recipient"]["epoch"] < delivery["recipient"]["epoch"] and prior["state"] in {"queued", "offered"}:
            _fail("old epoch requires explicit hold or disposition before new exposure")
        prior_message = state["messages"][prior["message_id"]]
        if (prior_message["source_task_id"] == message["source_task_id"]
                and prior_message["stream_id"] == message["stream_id"]
                and prior_message["sequence"] < message["sequence"]
                and prior["state"] in {"accepted", "queued", "offered", "held_for_recovery"}):
            _fail("earlier supervisor stream message remains unresolved")


def _apply_inbox_event(result, event, tasks):
    import _workspace_admission as admission
    payload, kind = event["payload"], event["event"]
    _goal_ready(result)
    if kind == admission.SUPERVISOR_SEND_EVENT:
        admission.canonical_supervisor_send_event(event)
        _inbox_source(result, payload, tasks)
        message, delivery = payload["message"], payload["delivery"]
        endpoint = _endpoint(result, message["recipient"]["endpoint_id"])
        admission.require_current_consumer(endpoint, admission.consumer_for_endpoint(endpoint))
        expected_recipient = {"kind": "supervisor_inbox", "endpoint_id": endpoint["endpoint_id"],
                              "owner_instance_id": endpoint["owner"]["instance_id"], "epoch": endpoint["owner"]["epoch"]}
        if (message["goal_id"] != result["goal"]["goal_id"] or message["recipient"] != expected_recipient
                or message["grant_ref"] != endpoint["receiver_grant_ref"]):
            _fail("supervisor message endpoint binding differs")
        _goal_lane(result, message["source_task_id"])
        if _known_ref(result, message["grant_ref"], endpoint_id=endpoint["endpoint_id"])["category"] != "grant":
            _fail("supervisor message requires endpoint grant")
        for key in ("reply_to", "causation_id"):
            if key in message:
                parent = result["messages"].get(message[key])
                if (parent is None or parent["kind"] != "supervisor_message"
                        or any(parent[field] != message[field] for field in ("source_task_id", "sender"))
                        or parent["recipient"]["endpoint_id"] != message["recipient"]["endpoint_id"]):
                    _fail("supervisor parent must be forward same-source same-endpoint context")
        binding = {key: message[key] for key in ("sender", "source_task_id", "recipient")}
        stream = result["streams"].get(message["stream_id"])
        if message["sequence"] != (stream["sequence"] + 1 if stream else 1) or (stream and stream["binding"] != binding):
            _fail("supervisor stream order or binding differs")
        _new(result["messages"], message["message_id"], copy.deepcopy(message), MAX_MESSAGES)
        _new_delivery(result, copy.deepcopy(delivery))
        result["streams"][message["stream_id"]] = {"sequence": message["sequence"], "binding": copy.deepcopy(binding)}
        proof = admission.send_admission_evidence(event)
        _inbox_registration(result, proof)
        _new(result["evidence"], proof["reference"]["id"], proof, MAX_EVIDENCE)
        after = copy.deepcopy(delivery)
        after["state"] = "queued"
        result["inbox_deliveries"][delivery["delivery_id"]] = protocol.validate_inbox_transition(
            delivery, after, {"durable_admission": proof["reference"]})
        _new(result.setdefault("send_operations", {}), event["operation_key"], {
            "request_sha256": payload["request"]["request_sha256"], "response": admission.send_response(event)}, MAX_MESSAGES)
        return
    admission.canonical_inbox_command_event(event)
    command, consumer, evidence = payload["command"], payload["consumer"], payload["evidence"]
    action, endpoint_id = command["action"], command["endpoint_id"]
    if action == "define":
        endpoint = _record(result, payload["endpoint"], "supervisor_endpoint")
        if endpoint["status"] != "unbound" or endpoint["revision"] != 1 or endpoint["goal_id"] != result["goal"]["goal_id"] or evidence:
            _fail("invalid initial supervisor endpoint")
        _new(result.setdefault("supervisor_endpoints", {}), endpoint_id, endpoint, MAX_ENDPOINTS)
    elif action in {"activate", "revoke", "retire"}:
        before, after = _endpoint(result, endpoint_id), _record(result, payload["endpoint"], "supervisor_endpoint")
        mutable = {"status", "revision"} | ({"owner", "receiver_grant_ref", "recovery_fence_ref", "lease_expires_at_ms"} if action == "activate" else set())
        if ({key: value for key, value in before.items() if key not in mutable} !=
                {key: value for key, value in after.items() if key not in mutable} or after["revision"] != before["revision"] + 1):
            _fail("endpoint immutable binding or revision differs")
        if action == "activate":
            if before["status"] not in {"unbound", "revoked"} or after["status"] != "active":
                _fail("endpoint replacement requires explicit revocation")
            roles = {"receiver_grant", "owner_fence"} | ({"recovery_fence"} if before["status"] == "revoked" else set())
            if (after["owner"]["epoch"] != (1 if before["owner"] is None else before["owner"]["epoch"] + 1)
                    or after["receiver_grant_ref"] == before["receiver_grant_ref"]
                    or evidence.get("receiver_grant") != after["receiver_grant_ref"]
                    or after["receiver_grant_ref"] != command["receiving_grant_ref"]
                    or after["recovery_fence_ref"] != evidence.get("recovery_fence")):
                _fail("endpoint activation requires fresh grant and next owner epoch")
            admission.require_current_consumer(after, consumer)
        else:
            expected = {"revoke": ("active", "revoked"), "retire": ("revoked", "retired")}[action]
            if (before["status"], after["status"]) != expected:
                _fail("invalid endpoint control transition")
            roles = set(ENDPOINT_ROLES[after["status"]])
            _inbox_slots_used(result, evidence, endpoint=before, target=after["status"])
        protocol._proof(evidence, roles)
        _inbox_refs(result, evidence, endpoint_id)
        result["supervisor_endpoints"][endpoint_id] = after
    else:
        endpoint = _endpoint(result, endpoint_id)
        after = _record(result, payload["delivery"], "inbox_delivery")
        before_id = command.get("delivery_id", command.get("parent_delivery_id"))
        before = result.get("inbox_deliveries", {}).get(before_id)
        if before is None or before["recipient"]["endpoint_id"] != endpoint_id:
            _fail("unknown inbox command delivery")
        if action in admission.INBOX_LIVE_ACTIONS:
            admission.require_current_consumer(endpoint, consumer)
            recipient = {"kind": "supervisor_inbox", "endpoint_id": endpoint_id,
                         "owner_instance_id": consumer["owner_instance_id"], "epoch": consumer["epoch"]}
            if after["recipient"] != recipient or after["grant_ref"] != endpoint["receiver_grant_ref"]:
                _fail("inbox current receiving authority differs")
        _inbox_refs(result, evidence, endpoint_id, delivery_id=before_id)
        if action == "link":
            if (after["delivery_id"] != command["new_delivery_id"]
                    or any(item.get("parent_delivery_id") == before_id for item in result.get("inbox_deliveries", {}).values())):
                _fail("inbox successor identity reused")
            protocol.validate_inbox_link(before, after, evidence)
            if evidence["recovery_fence"] != endpoint["recovery_fence_ref"]:
                _fail("linked successor requires current explicit recovery fence")
            _new_delivery(result, after)
        else:
            if after["delivery_id"] != before_id:
                _fail("inbox command delivery identity differs")
            message = result["messages"][before["message_id"]]
            if action == "offer":
                if (after["offer"]["consumer"] != consumer or any(after["offer"][field] != message["content"][source]
                        for field, source in (("content_sha256", "sha256"), ("content_utf8_bytes", "utf8_bytes")))
                        or evidence.get("receiver_grant") != after["grant_ref"]):
                    _fail("offer content or consumer binding differs")
                if any(item["offer"] and item["offer"]["offer_id"] == after["offer"]["offer_id"]
                       for item in result.get("inbox_deliveries", {}).values()):
                    _fail("offer ID already used")
                validate_inbox_offer_order(result, after)
                if after["recipient"]["epoch"] > 1 and endpoint["recovery_fence_ref"] is None:
                    _fail("replacement offer requires explicit recovery fence")
            elif action == "receipt" and (before["offer"] is None or before["offer"]["offer_id"] != command["offer_id"]
                                           or before["offer"]["consumer"] != consumer):
                _fail("receipt does not bind current original offer consumer")
            _inbox_slots_used(result, evidence, delivery_id=before_id, target=after["state"])
            result["inbox_deliveries"][before_id] = protocol.validate_inbox_transition(before, after, evidence)
    _new(result.setdefault("inbox_operations", {}), event["operation_key"], {
        "request_sha256": payload["request_sha256"], "consumer": copy.deepcopy(consumer),
        "response": admission.inbox_response(event)}, MAX_EVENTS)


def unresolved_holds(state: dict, task_id: str) -> list[dict]:
    """Return retained typed holds; later prose/assessments cannot release them."""
    _id(task_id)
    _goal_lane(state, task_id)
    holds = []
    for item in state["assessments"].values():
        record = item["record"]
        if record["disposition"] == "hold_affected_lane" and task_id in record["affected_task_ids"]:
            holds.append({"assessment_id": record["assessment_id"], "source_task_id": record["task_id"],
                          "affected_task_ids": record["affected_task_ids"], "evidence": record["evidence"],
                          "goal_revision": record["goal_revision"], "reason": record["reason"]})
    return copy.deepcopy(holds)


def require_no_unresolved_hold(state: dict, task_id: str) -> None:
    """Admission precondition only, never an execution grant or capability."""
    if unresolved_holds(state, task_id):
        _fail("target has an unresolved typed hold")


def _record(state, record, kind):
    result = protocol.validate_record(record)
    if result["kind"] != kind or any(result[k] != state[k] for k in ("workspace_id", "run_id")):
        _fail("record kind or workspace binding mismatch")
    return result


def _new(target, key, value, maximum):
    if key in target:
        _fail("duplicate entity requires original operation key")
    if len(target) >= maximum:
        _fail("bounded projection capacity exhausted")
    target[key] = value


def _delivery_refs(state, evidence, task_id, delivery_id):
    if type(evidence) is not dict:
        _fail("invalid delivery evidence")
    for key, value in evidence.items():
        if type(value) is dict:
            item = _known_ref(state, value, task_id)
            if item["category"] not in PROOF_CATEGORIES.get(key, set()):
                _fail("evidence category incompatible with required proof")
            unbound_category = ({"current_grant": "grant", "fresh_attempt_grant": "grant",
                                 "remaining_authority": "grant", "recipient_transfer_authority": "grant",
                                 "physical_fence": "fence"}).get(key)
            if unbound_category is not None and item["category"] != unbound_category:
                _fail("evidence category incompatible with required proof")
            if unbound_category is None and item.get("delivery_id") != delivery_id:
                _fail("receipt is not bound to this delivery")


def validate_turn_selection(state: dict, coordinator_view: dict,
                            selection: dict, recipient: dict) -> dict:
    """Validate a frozen attempt selection against detached workspace state.

    This is the shared guard used by replay and the coordinator's prospective
    claim admission path.  It deliberately consumes only detached snapshots:
    it checks task/claim fences, complete stream prefixes, held
    predecessors, and the *delivery* recipient.  The logical message's
    historical recipient is not authoritative for linked replacement
    deliveries; a new delivery leaf may carry a new, separately proved
    recipient.
    """
    tasks = _tasks(coordinator_view, state["run_id"])
    try:
        protocol._attempt(selection)
        protocol._worker(recipient)
    except (protocol.WorkspaceProtocolError, KeyError, TypeError) as exc:
        raise WorkspaceStateError("invalid selection or recipient") from exc
    task = tasks.get(selection["task_id"])
    if task is None:
        _fail("selection references an unknown task")
    claim = task.get("active_claim")
    if (selection["request_sha256"] != task["request_sha256"]
            or task["status"] != "claimed" or claim is None
            or selection["claim_id"] != claim["claim_id"]
            or selection["attempt"] != task["attempts"]
            or selection["owner_generation"] != claim["lease_generation"]):
        _fail("selection conflicts with coordinator claim")
    selected_ids = selection["selected_message_ids"]
    for selected_id in selected_ids:
        selected = state["messages"].get(selected_id)
        if selected is None or selected.get("destination_task_id") != task["task_id"]:
            _fail("dangling or wrong-task selected message")
        linked_parents = {
            delivery["parent_delivery_id"]
            for delivery in state["deliveries"].values()
            if "parent_delivery_id" in delivery
        }
        predecessors = {
            message["message_id"]
            for message in state["messages"].values()
            if message["stream_id"] == selected["stream_id"]
            and message["sequence"] < selected["sequence"]
        }
        selected_order = {message_id: index for index, message_id in enumerate(selected_ids)}
        if any(message_id in selected_order and selected_order[message_id] > selected_order[selected_id]
               for message_id in predecessors):
            _fail("selected stream prefix is out of order")
        if any(delivery["message_id"] in predecessors
               and delivery["delivery_id"] not in linked_parents
               and delivery["state"] == "held_for_recovery"
               for delivery in state["deliveries"].values()):
            _fail("held required predecessor blocks this stream")
        if any(delivery["message_id"] in predecessors
               and delivery["delivery_id"] not in linked_parents
               and delivery["state"] in {"accepted", "queued", "included_in_attempt"}
               and (delivery["message_id"] not in selected_ids
                    or delivery["state"] == "accepted"
                    or (delivery["selection"] is not None
                        and delivery["selection"] != selection))
               for delivery in state["deliveries"].values()):
            _fail("unresolved predecessor omitted from selected stream prefix")
        # Match the recipient on the selected delivery leaf, not on the
        # immutable logical message.  Linked replacements intentionally may
        # transfer delivery to a newly fenced worker/epoch.
        candidates = [delivery for delivery in state["deliveries"].values()
                      if delivery["message_id"] == selected_id
                      and delivery["recipient"] == recipient
                      and delivery["state"] in {"queued", "included_in_attempt"}]
        if (not candidates
                or any(delivery["selection"] is not None
                       and delivery["selection"] != selection
                       for delivery in candidates)):
            _fail("selected message lacks eligible recipient-bound delivery")
    return copy.deepcopy(selection)


def apply_event(state: dict | None, event: dict, coordinator_view: dict) -> dict:
    """Return a detached projection. Refusal never mutates the supplied state.

    Events must already be admitted by the trusted coordinator. Passing forged
    dictionaries through this reducer does NOT authenticate them or authorize
    contact. Repeated identical operation keys are no-ops, including after newer
    events; conflicts refuse. The coordinator must independently gate launches.
    """
    fingerprint = _event(event)
    tasks = _tasks(coordinator_view, event["run_id"])
    if state is None:
        if event["event"] != FEATURE_EVENT or event["expected_revision"] != 0:
            _fail("workspace feature marker must precede workspace events")
        _obj(event["payload"], {"feature", "version"})
        if event["payload"] != {"feature": "goal_messages", "version": FEATURE_VERSION} or type(event["payload"]["version"]) is not int:
            _fail("unsupported workspace feature version")
        return {"protocol": event["protocol"], "workspace_id": event["workspace_id"],
                "run_id": event["run_id"], "revision": 1, "goal": None,
                "goal_history": [], "active_priority": None, "lanes": {},
                "evidence": {}, "assessments": {}, "decisions": {},
                "messages": {}, "deliveries": {}, "streams": {},
                "effect_resolutions": {}, "send_operations": {},
                "operations": {event["operation_key"]: fingerprint}}
    if any(state[k] != event[k] for k in ("protocol", "workspace_id", "run_id")):
        _fail("workspace event scope mismatch")
    previous = state["operations"].get(event["operation_key"])
    if previous is not None:
        if previous != fingerprint:
            _fail("conflicting operation key")
        return copy.deepcopy(state)
    if event["expected_revision"] != state["revision"]:
        _fail("stale workspace revision")
    for task_id, lane in state["lanes"].items():
        if task_id not in tasks or tasks[task_id]["request_sha256"] != lane["request_sha256"]:
            _fail("coordinator task binding disappeared or changed")
    if len(state["operations"]) >= MAX_EVENTS:
        _fail("workspace event bound exhausted")
    result = copy.deepcopy(state)
    payload = event["payload"]
    kind = event["event"]
    try:
        if kind == "workspace_goal_defined":
            _obj(payload, {"goal"})
            goal = _record(result, payload["goal"], "goal")
            if result["goal"] is not None or goal["revision"] != 1:
                _fail("one initial goal at revision one required")
            if goal["active_priority"] not in tasks:
                _fail("initial priority must name an existing coordinator task")
            result["goal"] = goal
            result["goal_history"].append(goal)
            result["active_priority"] = goal["active_priority"]
        elif kind == "workspace_lane_defined":
            _obj(payload, {"lane"})
            lane = _record(result, payload["lane"], "lane")
            if result["goal"] is None:
                _fail("goal must precede lane")
            protocol.validate_goal_binding(result["goal"], lane)
            task = tasks.get(lane["task_id"])
            if task is None or task["request_sha256"] != lane["request_sha256"]:
                _fail("lane must bind an existing coordinator task request")
            if any(existing["lane_id"] == lane["lane_id"] for existing in result["lanes"].values()):
                _fail("duplicate lane id")
            _new(result["lanes"], lane["task_id"], lane, 16)
        elif kind == "workspace_evidence_registered":
            _obj(payload, {"reference", "category"}, {"task_id", "endpoint_id", "delivery_id", "settlement_for"})
            _ref(payload["reference"])
            if ("task_id" in payload) == ("endpoint_id" in payload):
                _fail("evidence requires task XOR endpoint subject")
            if "task_id" in payload:
                _goal_lane(result, payload["task_id"])
            else:
                _endpoint(result, payload["endpoint_id"])
            if type(payload["category"]) is not str or payload["category"] not in {"artifact", "event", "grant", "fence", "adapter_receipt", "observation"}:
                _fail("unsupported evidence category")
            if "delivery_id" in payload:
                delivery = (result["deliveries"] if "task_id" in payload else result.get("inbox_deliveries", {})).get(payload["delivery_id"])
                if delivery is None:
                    _fail("dangling evidence delivery target")
                if "task_id" in payload and result["messages"][delivery["message_id"]]["destination_task_id"] != payload["task_id"]:
                    _fail("delivery evidence task differs")
                if "endpoint_id" in payload and delivery["recipient"]["endpoint_id"] != payload["endpoint_id"]:
                    _fail("delivery evidence endpoint differs")
            if "settlement_for" in payload and _settlement_registration(result, payload):
                return copy.deepcopy(state)
            _new(result["evidence"], payload["reference"]["id"], copy.deepcopy(payload), MAX_EVIDENCE)
        elif kind == "workspace_assessed":
            _obj(payload, {"assessment"})
            _goal_ready(result)
            assessment = _record(result, payload["assessment"], "assessment")
            lane = _goal_lane(result, assessment["task_id"])
            protocol.validate_goal_binding(result["goal"], lane, assessment)
            for reference in assessment["evidence"]:
                _known_ref(result, reference, lane["task_id"])
            for task_id in assessment.get("affected_task_ids", []):
                _goal_lane(result, task_id)
            observations = {task_id: tasks[task_id]["status"] for task_id in result["lanes"] if task_id in tasks}
            _new(result["assessments"], assessment["assessment_id"],
                 {"record": assessment, "observed_task_status": observations, "event_revision": result["revision"] + 1}, MAX_ASSESSMENTS)
        elif kind == "workspace_next_lane_selected":
            _obj(payload, {"decision_id", "goal_revision", "assessment_id", "from_task_id", "to_task_id"})
            _goal_ready(result)
            _id(payload["decision_id"])
            _int(payload["goal_revision"])
            assessment = result["assessments"].get(payload["assessment_id"])
            if assessment is None or assessment["record"]["task_id"] != payload["from_task_id"]:
                _fail("decision needs prior source-lane assessment")
            lane = _goal_lane(result, payload["to_task_id"])
            require_no_unresolved_hold(result, payload["to_task_id"])
            if payload["from_task_id"] == payload["to_task_id"]:
                _fail("next iteration requires a distinct prepared lane task")
            if payload["goal_revision"] != result["goal"]["revision"] or assessment["record"]["goal_revision"] != payload["goal_revision"]:
                _fail("decision is not bound to current goal revision")
            if assessment["record"]["disposition"] != "continue_main" or assessment["record"]["next_decision"] == "blocked":
                _fail("blocking disposition cannot select a successor")
            if tasks[lane["task_id"]]["status"] != "pending" or tasks[lane["task_id"]]["attempts"] != 0:
                _fail("decision cannot reopen or re-admit attempted tasks")
            if any(d["to_task_id"] == payload["to_task_id"] for d in result["decisions"].values()):
                _fail("successor task already selected")
            _new(result["decisions"], payload["decision_id"], copy.deepcopy(payload), MAX_DECISIONS)
            result["active_priority"] = payload["to_task_id"]
        elif kind == "workspace_message_admitted":
            _admit_message(result, payload)
        elif kind == "workspace_operator_message_sent":
            import _workspace_admission as admission
            admission.canonical_operator_send_event(event)
            scope = payload["operator"]["scope"]
            for reference in (scope["reference"], scope["delivery_grant_ref"]):
                if _known_ref(result, reference, scope["destination_task_id"])["category"] != "grant":
                    _fail("operator send requires registered scoped grant evidence")
            # Replay consumes a previously admitted host decision. It cannot
            # authenticate a browser or replace the writer's current scope check.
            _admit_message(result, {"message": payload["message"], "delivery": payload["delivery"]}, operator=True)
            proof = admission.operator_admission_evidence(event)
            if _settlement_registration(result, proof):
                _fail("new operator send cannot reuse a consumed admission slot")
            _new(result["evidence"], proof["reference"]["id"], proof, MAX_EVIDENCE)
            before = result["deliveries"][payload["delivery"]["delivery_id"]]
            after = copy.deepcopy(before)
            after["state"] = "queued"
            evidence = {"durable_admission": proof["reference"]}
            _delivery_refs(result, evidence, scope["destination_task_id"], before["delivery_id"])
            _settlement_transition(result, before["delivery_id"], "queued", evidence)
            result["deliveries"][before["delivery_id"]] = protocol.validate_transition(before, after, evidence)
            _new(result.setdefault("send_operations", {}), event["operation_key"], {
                "request_sha256": payload["request"]["request_sha256"],
                "response": admission.operator_send_response(event)}, MAX_MESSAGES)
        elif kind == "workspace_operator_disposition_recorded":
            import _workspace_admission as admission
            admission.canonical_operator_disposition_event(event)
            delivery_id = payload["delivery_id"]
            delivery = result["deliveries"].get(delivery_id)
            if delivery is None:
                _fail("retain disposition references an unknown delivery")
            message = result["messages"].get(delivery["message_id"])
            if message is None or message["destination_task_id"] != payload["task_id"]:
                _fail("retain disposition task binding differs")
            if delivery["state"] != "held_for_recovery":
                _fail("retain disposition requires a held delivery")
            for reference_key in ("request_ref", "decision_ref"):
                registered = _known_ref(result, payload[reference_key],
                                        payload["task_id"], delivery_id)
                if registered["category"] != "event" or "settlement_for" in registered:
                    _fail("retain disposition reference is not an ordinary bound event")
            # This is deliberately an audit-only event. It records the
            # operation fingerprint below while preserving every delivery
            # field, including uncertainty and the original hold reason.
        elif kind == "workspace_message_sent":
            import _workspace_admission as admission
            admission.canonical_send_event(event)
            source, scope = payload["source"], payload["send_scope"]
            claim = source["claim"]
            source_task = tasks.get(claim["task_id"])
            if (source_task is None or source_task["status"] != "claimed"
                    or source_task["request_sha256"] != claim["request_sha256"]
                    or source_task["attempts"] != claim["attempt"]
                    or source_task["active_claim"] is None
                    or any(source_task["active_claim"].get(key) != claim[key] for key in ("claim_id", "lease_generation"))):
                _fail("send source claim is not current in coordinator view")
            for reference, task_id in ((scope["reference"], claim["task_id"]),
                                       (scope["channel_grant_ref"], claim["task_id"]),
                                       (scope["delivery_grant_ref"], scope["destination_task_id"])):
                if _known_ref(result, reference, task_id)["category"] != "grant":
                    _fail("send requires registered scoped grant evidence")
            message = payload["message"]
            for key in ("reply_to", "causation_id"):
                if key not in message:
                    continue
                parent = result["messages"].get(message[key])
                if parent is None:
                    _fail("dangling logical message parent")
                if parent["kind"] != "message":
                    _fail("task messages cannot use supervisor-only parents")
                forward = all(parent[field] == message[field] for field in (
                    "source_task_id", "destination_task_id", "sender", "recipient"))
                reverse = (parent["source_task_id"] == message["destination_task_id"]
                           and parent["destination_task_id"] == message["source_task_id"]
                           and parent["sender"] == message["recipient"] and parent["recipient"] == message["sender"])
                if not forward and not reverse:
                    _fail("logical parent is outside the authorized endpoint pair")
            _admit_message(result, {"message": payload["message"], "delivery": payload["delivery"]})
            proof = admission.send_admission_evidence(event)
            if _settlement_registration(result, proof):
                _fail("new send cannot reuse a consumed admission slot")
            _new(result["evidence"], proof["reference"]["id"], proof, MAX_EVIDENCE)
            before = result["deliveries"][payload["delivery"]["delivery_id"]]
            after = copy.deepcopy(before)
            after["state"] = "queued"
            evidence = {"durable_admission": proof["reference"]}
            _delivery_refs(result, evidence, payload["message"]["destination_task_id"], before["delivery_id"])
            _settlement_transition(result, before["delivery_id"], "queued", evidence)
            result["deliveries"][before["delivery_id"]] = protocol.validate_transition(before, after, evidence)
            operations = result.setdefault("send_operations", {})
            _new(operations, event["operation_key"], {"request_sha256": payload["request"]["request_sha256"],
                 "response": admission.send_response(event)}, MAX_MESSAGES)
        elif kind == ATOMIC_TURN_EVENT:
            # Import lazily: admission reuses this module's pure selection and
            # reservation guards without making the state reducer own a second
            # economics schema.
            import _workspace_admission as admission
            _obj(payload, {"claim", "selection", "recipient", "grant_ref", "evidence_by_delivery",
                           "affected_task_ids", "holds", "goal_revision",
                           "selected_delivery_ids", "supported_ack_levels"},
                 {"budget_admission", "economics_reservation"})
            if "budget_admission" in payload and type(payload["budget_admission"]) is not dict:
                _fail("invalid atomic budget admission")
            if "economics_reservation" in payload and type(payload["economics_reservation"]) is not dict:
                _fail("invalid atomic economics reservation")
            _goal_ready(result)
            selection = payload["selection"]
            recipient = payload["recipient"]
            try:
                protocol._attempt(selection)
                protocol._worker(recipient)
            except protocol.WorkspaceProtocolError as exc:
                _fail(str(exc))
            validate_turn_selection(
                result, coordinator_view, selection, recipient)
            claim = payload["claim"]
            if (type(claim) is not dict or claim.get("task_id") != selection["task_id"]
                    or claim.get("claim_id") != selection["claim_id"]
                    or claim.get("attempt") != selection["attempt"]
                    or claim.get("lease_generation") != selection["owner_generation"]
                    or claim.get("request_sha256") != selection["request_sha256"]):
                _fail("atomic claim and selection differ")
            if "economics_reservation" in payload:
                try:
                    admission.economics_reservation(
                        payload["economics_reservation"],
                        claim_id=claim["claim_id"], selection=selection)
                except admission.WorkspaceAdmissionError as exc:
                    raise WorkspaceStateError("invalid atomic economics reservation") from exc
            grant_ref = payload["grant_ref"]
            _ref(grant_ref)
            selected_ids = set(selection["selected_message_ids"])
            selected_delivery_ids = payload["selected_delivery_ids"]
            if (type(selected_delivery_ids) is not list
                    or not 1 <= len(selected_delivery_ids) <= 8):
                _fail("invalid atomic delivery selection")
            try:
                for delivery_id in selected_delivery_ids:
                    _id(delivery_id)
            except WorkspaceStateError:
                _fail("invalid atomic delivery selection")
            if len(set(selected_delivery_ids)) != len(selected_delivery_ids):
                _fail("invalid atomic delivery selection")
            try:
                evidence_by_delivery = admission._turn_evidence_payload(
                    payload, selected_delivery_ids, grant_ref)
                if payload["goal_revision"] != result["goal"]["revision"]:
                    _fail("atomic goal revision differs")
                admission.validate_affected_holds(
                    state=result, target_task_id=selection["task_id"],
                    affected_task_ids=payload["affected_task_ids"], holds=payload["holds"],
                    goal_revision=payload["goal_revision"])
            except admission.WorkspaceAdmissionError as exc:
                raise WorkspaceStateError(str(exc)) from exc
            if type(payload["supported_ack_levels"]) is not list or any(
                    type(level) is not str or level not in protocol.ACK_LEVELS
                    for level in payload["supported_ack_levels"]):
                _fail("unsupported acknowledgement capability")
            for delivery_id in selected_delivery_ids:
                before = result["deliveries"].get(delivery_id)
                if before is None or before["message_id"] not in selected_ids:
                    _fail("atomic selection references an unknown delivery")
                if before["state"] != "queued":
                    _fail("atomic selection requires queued deliveries")
                if before["recipient"] != recipient:
                    _fail("atomic delivery recipient differs")
                message = result["messages"].get(before["message_id"])
                if message is None or message.get("destination_task_id") != selection["task_id"]:
                    _fail("atomic delivery is not bound to the selected task")
                if before["grant_ref"] != grant_ref:
                    _fail("atomic delivery grant differs")
                evidence = evidence_by_delivery[delivery_id]
                _delivery_refs(result, evidence, selection["task_id"], delivery_id)
                after = copy.deepcopy(before)
                after["state"] = "included_in_attempt"
                after["selection"] = copy.deepcopy(selection)
                transition_evidence = {
                    key: evidence[key]
                    for key in ("selection_record", "whole_message_fit")
                }
                _settlement_transition(result, delivery_id, "included_in_attempt", transition_evidence)
                result["deliveries"][delivery_id] = protocol.validate_transition(
                    before, after, transition_evidence,
                    supported_ack_levels=payload["supported_ack_levels"])
            if {result["deliveries"][item]["message_id"] for item in selected_delivery_ids} != selected_ids:
                _fail("atomic delivery selection does not cover messages")
        elif kind == "workspace_effects_resolved":
            _obj(payload, {"delivery", "resolution_kind", "qualification", "evidence"})
            after = _record(result, payload["delivery"], "delivery")
            before = result["deliveries"].get(after["delivery_id"])
            if before is None:
                _fail("dangling effect-resolution delivery")
            task_id = result["messages"][before["message_id"]]["destination_task_id"]
            _delivery_refs(result, payload["evidence"], task_id, before["delivery_id"])
            _settlement_transition(result, before["delivery_id"], "effects_resolved", payload["evidence"])
            result["deliveries"][before["delivery_id"]] = protocol.validate_effect_resolution(
                before, after, payload["evidence"], resolution_kind=payload["resolution_kind"],
                qualification=payload["qualification"])
            # Retain the explicit qualification/evidence alongside immutable
            # receipt history. Clean fixture effects never imply provider proof.
            _new(result.setdefault("effect_resolutions", {}), before["delivery_id"], {
                "delivery_id": before["delivery_id"], "resolution_kind": payload["resolution_kind"],
                "qualification": payload["qualification"], "evidence": copy.deepcopy(payload["evidence"]),
                "event_revision": result["revision"] + 1}, MAX_DELIVERIES)
        elif kind == "workspace_delivery_advanced":
            _obj(payload, {"delivery", "evidence", "supported_ack_levels"})
            after = _record(result, payload["delivery"], "delivery")
            before = result["deliveries"].get(after["delivery_id"])
            if before is None:
                _fail("dangling delivery transition")
            message = result["messages"][before["message_id"]]
            _delivery_refs(result, payload["evidence"], message["destination_task_id"], before["delivery_id"])
            _settlement_transition(result, before["delivery_id"], after["state"], payload["evidence"])
            if after["state"] == "held_for_recovery" and after["reason"] in {"content_missing", "content_mismatch", "content_unavailable"}:
                reference = payload["evidence"].get("hold_observation")
                if _known_ref(result, reference, message["destination_task_id"], before["delivery_id"])["category"] != "observation":
                    _fail("content hold requires a registered observation")
            if type(payload["supported_ack_levels"]) is not list or any(type(x) is not str or x not in protocol.ACK_LEVELS for x in payload["supported_ack_levels"]):
                _fail("unsupported acknowledgement capability")
            if after["state"] == "included_in_attempt":
                selection = after["selection"]
                validate_turn_selection(
                    result, coordinator_view, selection, after["recipient"])
            result["deliveries"][after["delivery_id"]] = protocol.validate_transition(before, after, payload["evidence"], supported_ack_levels=payload["supported_ack_levels"])
        elif kind == "workspace_delivery_linked":
            _obj(payload, {"delivery", "evidence"})
            child = _record(result, payload["delivery"], "delivery")
            parent = result["deliveries"].get(child.get("parent_delivery_id"))
            if parent is None:
                _fail("dangling linked-delivery parent")
            message = result["messages"][parent["message_id"]]
            _delivery_refs(result, payload["evidence"], message["destination_task_id"], parent["delivery_id"])
            if _known_ref(result, child["grant_ref"], message["destination_task_id"])["category"] != "grant":
                _fail("linked delivery requires a registered grant reference")
            if any(d.get("parent_delivery_id") == parent["delivery_id"] for d in result["deliveries"].values()):
                _fail("parent already has a linked successor")
            child = protocol.validate_linked_delivery(parent, child, payload["evidence"])
            _new_delivery(result, child)
        elif kind in {"workspace_supervisor_endpoint_defined", "workspace_supervisor_endpoint_activated", "workspace_supervisor_endpoint_revoked",
                      "workspace_supervisor_endpoint_retired", "workspace_supervisor_message_sent", "workspace_inbox_delivery_advanced", "workspace_inbox_delivery_linked"}:
            _apply_inbox_event(result, event, tasks)
        else:
            _fail("unsupported workspace event")
    except (protocol.WorkspaceProtocolError, KeyError, TypeError) as exc:
        raise WorkspaceStateError(str(exc)) from exc
    result["revision"] += 1
    result["operations"][event["operation_key"]] = fingerprint
    return result


def project(state: dict, coordinator_view: dict) -> dict:
    """Project live task status from the supplied coordinator, never stored copies."""
    tasks = _tasks(coordinator_view, state["run_id"])
    lanes = []
    for task_id, lane in state["lanes"].items():
        if task_id not in tasks or tasks[task_id]["request_sha256"] != lane["request_sha256"]:
            _fail("coordinator task binding disappeared or changed")
        lanes.append({"task_id": task_id, "lane_id": lane["lane_id"], "role": lane["role"],
                      "status": tasks[task_id]["status"], "return_condition": lane["return_condition"]})
    return copy.deepcopy({"protocol": protocol.PROTOCOL, "revision": state["revision"],
                          "goal": state["goal"], "active_priority": state["active_priority"],
                          "lanes": lanes, "assessments": list(state["assessments"].values()),
                          "holds": [hold for task_id in state["lanes"] for hold in unresolved_holds(state, task_id)
                                    if hold["affected_task_ids"][0] == task_id],
                          "decisions": list(state["decisions"].values()),
                          "effect_resolutions": list(state.get("effect_resolutions", {}).values()),
                          "inbox": inbox_summary(state),
                          "deliveries": [{k: d[k] for k in ("delivery_id", "message_id", "state", "reason", "certainty", "inherited_uncertainty")}
                                         for d in state["deliveries"].values()]})


def replay(steps) -> dict:
    """Replay (event, coordinator-view-at-that-position) pairs without I/O."""
    state = None
    for event, view in steps:
        state = apply_event(state, event, view)
    if state is None:
        _fail("workspace feature marker missing")
    return state
