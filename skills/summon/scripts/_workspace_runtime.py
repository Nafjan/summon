"""Capability-gated trusted-host facade over the existing swarm journal.

Owned fixed worker/consumer handles provide simulated local authentication;
caller JSON never does. No provider, second scheduler, raw append or second
journal. Central base reconciliation and reserved atomic admission remain
mandatory. Private content durability is qualified for process restart only.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from typing import Callable

from _swarm_coordinator import (SwarmCoordinator, SwarmNotFoundError, SwarmCoordinatorError,
                                SwarmConflictError, SwarmBudgetRefusal, MAX_ATTEMPTS)
from _rundir import encode_journal_record, OwnershipLostError
from _fleet_approval import _reject_reparse_ancestors, _secure_private_root, _verify_private
from _workspace_content import ContentError, ContentRef, ContentStore
from _workspace_layout import create_workspace_layout, resolve_workspace_layout
import _workspace_protocol as protocol
import _workspace_state as projection
import _workspace_transport as transport
import _workspace_admission as admission
import _workspace_budget as budget
import _context_policy
import _submission_accounting


ADMISSION_SCHEMA = "summon.workspace-admission/v1"
CONTAINER = "workspace_event"
EVENT_CLASSES = frozenset({
    projection.FEATURE_EVENT, "workspace_goal_defined", "workspace_lane_defined",
    "workspace_evidence_registered", "workspace_assessed", "workspace_next_lane_selected",
    "workspace_message_admitted", "workspace_delivery_advanced", "workspace_delivery_linked",
    "workspace_effects_resolved", admission.OPERATOR_DISPOSITION_EVENT,
})
SUPERVISOR_EVENTS = frozenset({"workspace_supervisor_endpoint_defined", "workspace_supervisor_endpoint_activated",
    "workspace_supervisor_endpoint_revoked", "workspace_supervisor_endpoint_retired", "workspace_supervisor_message_sent",
    "workspace_inbox_delivery_advanced", "workspace_inbox_delivery_linked"})
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


class WorkspaceRuntimeError(ValueError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"workspace runtime refused ({kind})")


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise WorkspaceRuntimeError("invalid_id")
    return value


def _decode_workspace_source(raw: bytes, reference: dict) -> dict:
    """Decode one exact value from a plan aggregate or a legacy source."""
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise WorkspaceRuntimeError("operator_scope_source_unverified") from None
    if (isinstance(decoded, dict) and isinstance(decoded.get("entries"), list)
            and isinstance(decoded.get("schema"), str)
            and decoded["schema"].startswith("summon.workspace.plan-")):
        identifier = reference.get("id") if isinstance(reference, dict) else None
        if type(identifier) is not str or "." not in identifier:
            raise WorkspaceRuntimeError("operator_scope_source_unverified")
        suffix = identifier.rsplit(".", 1)[-1]
        matches = [item for item in decoded["entries"]
                   if isinstance(item, dict) and item.get("entry_id") == suffix]
        if len(matches) != 1 or not isinstance(matches[0].get("value"), dict):
            raise WorkspaceRuntimeError("operator_scope_source_unverified")
        return matches[0]["value"]
    if not isinstance(decoded, dict):
        raise WorkspaceRuntimeError("operator_scope_source_unverified")
    return decoded


def _task_view(state: dict, *, run_id: str, now_ms: int) -> dict:
    """Reconstruct only reducer input from real coordinator state at this point.

    The claim lease generation is distinct from the transient journal writer's
    generation. No task status supplied by workspace payloads participates here.
    """
    tasks = []
    for task in state["tasks"].values():
        claims = [state["claims"][identifier] for identifier in task["attempts"]]
        latest = claims[-1] if claims else None
        active = latest if latest and latest["status"] == "active" else None
        status = task["terminal"] or (
            "expired" if active and active["lease_expires_at_ms"] <= now_ms else
            "claimed" if active else "pending")
        tasks.append({"task_id": task["task_id"], "request_sha256": task["request_sha256"],
                      "status": status, "attempts": len(claims),
                      "active_claim": ({"claim_id": active["claim_id"],
                                        "lease_generation": active["lease_generation"]}
                                       if active else None)})
    return {"run_id": run_id, "tasks": tasks}


class _WorkspaceCoordinator(SwarmCoordinator):
    """Replay extension only. Never override central mutation/admission/append."""

    def _apply_record(self, state, record):
        if record.get("event") != CONTAINER:
            return SwarmCoordinator._apply_record(state, record)
        if (isinstance(record.get("workspace_event"), dict)
                and record["workspace_event"].get("event") in SUPERVISOR_EVENTS | {admission.ATOMIC_SEND_EVENT}):
            # Composite replay owns historical source-claim/time validation.
            return SwarmCoordinator._apply_record(state, record)
        if (set(record) - {"event", "workspace_event", "observed_at_ms", "generation", "ts"}
                or not {"event", "workspace_event", "observed_at_ms"} <= set(record)):
            raise WorkspaceRuntimeError("invalid_workspace_container")
        observed = record["observed_at_ms"]
        if type(observed) is not int or not 1 <= observed <= protocol.MAX_INT:
            raise WorkspaceRuntimeError("invalid_observation_time")
        event = record["workspace_event"]
        if type(event) is not dict or event.get("run_id") != self.run_id:
            raise WorkspaceRuntimeError("workspace_run_mismatch")
        state["workspace"] = projection.apply_event(
            state.get("workspace"), event,
            _task_view(state, run_id=self.run_id, now_ms=observed))


def goal_plan(goal: dict, lanes: list[dict], *, operation_prefix: str,
              economics: dict | None = None) -> dict:
    """Validate a reviewable definition without filesystem or authority effects."""
    _id(operation_prefix)
    checked_goal = protocol.validate_record(goal)
    if checked_goal["kind"] != "goal" or type(lanes) is not list or not 1 <= len(lanes) <= 16:
        raise WorkspaceRuntimeError("invalid_goal_plan")
    checked_lanes = [protocol.validate_record(lane) for lane in lanes]
    if len({lane.get("task_id") for lane in checked_lanes}) != len(checked_lanes):
        raise WorkspaceRuntimeError("duplicate_lane_task")
    for lane in checked_lanes:
        protocol.validate_goal_binding(checked_goal, lane)
    if checked_goal.get("protocol") == protocol.PROTOCOL_V2:
        try:
            protocol.validate_goal_plan_v2(checked_goal, checked_lanes, economics=economics)
        except protocol.WorkspaceProtocolError as exc:
            raise WorkspaceRuntimeError("invalid_goal_plan") from exc
    view = {"run_id": checked_goal["run_id"], "tasks": [
        {"task_id": lane["task_id"], "request_sha256": lane["request_sha256"],
         "status": "pending", "attempts": 0, "active_claim": None}
        for lane in checked_lanes]}
    events, state = [], None
    definitions = [(projection.FEATURE_EVENT, {"feature": "goal_messages", "version": 1}),
                   ("workspace_goal_defined", {"goal": checked_goal})]
    definitions.extend(("workspace_lane_defined", {"lane": lane}) for lane in checked_lanes)
    for index, (kind, payload) in enumerate(definitions):
        key = _id(f"{operation_prefix}-{index}")
        event = {"event": kind, "protocol": checked_goal["protocol"],
                 "workspace_id": checked_goal["workspace_id"], "run_id": checked_goal["run_id"],
                 "operation_key": key, "expected_revision": index, "payload": payload}
        state = projection.apply_event(state, event, view)
        events.append(event)
    result = {"status": "proposed", "execution_authorized": False,
            "tasks": [{"task_id": lane["task_id"], "request_sha256": lane["request_sha256"]}
                      for lane in checked_lanes], "events": events}
    if checked_goal.get("protocol") == protocol.PROTOCOL_V2:
        result["economics"] = copy.deepcopy(economics)
    return result


def transport_observation_plan(kind: str) -> dict:
    """Pure integration advice, never authenticated evidence or durable state.

    OwnedFakeWorker must independently authenticate AND bind received frames to
    the frozen request. A full pipe write does not establish recipient receipt.
    No pipe is opened here; base-admitted evidence/transition remains required.
    """
    mappings = {
        "full_write": ("submission_started", None, None, ["bound_authenticated_frame_received"]),
        "authenticated_frame_received": ("submitted", None, None,
                                           ["bound_adapter_receipt", "durable_base_transition"]),
        "authenticated_acknowledged": ("acknowledged", None, "recipient_received",
                                         ["bound_ack_receipt", "prior_submitted", "durable_base_transition"]),
        "partial_or_unknown": ("held_for_recovery", "contact_uncertain", None,
                               ["write_observation", "durable_base_transition"]),
        "zero_write": ("not_submitted", None, None,
                       ["affirmative_no_contact", "no_side_effects", "cleanup_evidence", "durable_base_transition"]),
        # A native/session collision is an authenticated adapter observation,
        # not a provider result.  It must retain the selected attempt and
        # uncertainty until an operator resolves the contact/spend boundary.
        "native_turn_collision": ("held_for_recovery", "native_turn_collision", None,
                                   ["authenticated_collision_observation", "durable_base_transition"]),
    }
    if type(kind) is not str or kind not in mappings:
        raise WorkspaceRuntimeError("unsupported_transport_observation")
    state, reason, ack, required = mappings[kind]
    return {"candidate_state": state, "reason": reason, "ack_level": ack,
            "durable": False, "evidence_verified": False, "requires": list(required)}


def _collision_observation_event(event: dict, *, kind: str, evidence: dict) -> dict:
    """Validate the provider-free adapter boundary for a turn collision.

    This helper only checks the typed shape.  ``WorkspaceRuntime`` still
    performs authority, source, journal and replay checks through
    :meth:`record_event`; a caller cannot turn a prose collision claim into a
    durable hold by calling this function alone.
    """
    plan = transport_observation_plan(kind)
    if kind != "native_turn_collision" or type(event) is not dict:
        raise WorkspaceRuntimeError("native_turn_collision_required")
    if event.get("event") != "workspace_delivery_advanced":
        raise WorkspaceRuntimeError("native_turn_collision_event_required")
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != {"delivery", "evidence", "supported_ack_levels"}:
        raise WorkspaceRuntimeError("native_turn_collision_event_invalid")
    delivery = payload["delivery"]
    if (type(delivery) is not dict
            or delivery.get("state") != plan["candidate_state"]
            or delivery.get("reason") != plan["reason"]
            or delivery.get("certainty") != {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}):
        raise WorkspaceRuntimeError("native_turn_collision_uncertainty_required")
    if payload["evidence"].get("hold_observation") != evidence:
        raise WorkspaceRuntimeError("native_turn_collision_evidence_mismatch")
    if payload["supported_ack_levels"] != []:
        raise WorkspaceRuntimeError("native_turn_collision_ack_unsupported")
    return event


def verify_fixed_result(worker, *, expected_binding: dict, request: dict,
                        request_frame_sha256: str, request_sequence: int,
                        evidence_id: str, timeout=3.0) -> dict:
    """Receive and independently verify one fixed-child result, without writes.

    Host-only boundary: expectations come from the supervisor's frozen request,
    not from worker prose. Caller consumes the separate receipt pair first.
    Reading through the actual owned transport authenticates and correlates the
    result; accepting a plain result dictionary here would lose that property.
    Returned source bytes are private evidence proposed for later registration.
    This function neither launches workers nor grants/completes/approves work.
    """
    if type(worker) is not transport.OwnedFakeWorker:
        raise WorkspaceRuntimeError("owned_worker_required")
    _id(evidence_id)
    binding, original = copy.deepcopy(expected_binding), copy.deepcopy(request)
    try:
        transport._binding(binding)
        transport._digest(request_frame_sha256)
        transport._integer(request_sequence)
    except transport.TransportError:
        raise WorkspaceRuntimeError("invalid_fixture_expectation") from None
    if (type(original) is not dict
            or set(original) != {"message_id", "delivery_id", "attempt_id", "context", "context_sha256", "operation"}
            or type(original["operation"]) is not str
            or original["operation"] not in {transport.FIXTURE_OPERATION, transport.CONTEXT_FIXTURE_OPERATION}
            or type(original["context"]) is not str):
        raise WorkspaceRuntimeError("invalid_fixture_expectation")
    for name in ("message_id", "delivery_id", "attempt_id"):
        _id(original[name])
    if binding != worker.channel.binding:
        raise WorkspaceRuntimeError("fixture_task_binding_mismatch")
    # Independently recompute from the original bytes. Do not use the child's
    # computation/helper or accept its declared result as the expected answer.
    try:
        raw = original["context"].encode("utf-8")
        if not 1 <= len(raw) <= 4096 or hashlib.sha256(raw).hexdigest() != original["context_sha256"]:
            raise ValueError
        decoded = json.loads(raw)
        if original["operation"] == transport.CONTEXT_FIXTURE_OPERATION:
            fields = {"schema", "plane", "task_id", "claim_id", "attempt", "lease_generation", "request_sha256", "entries"}
            if (type(decoded) is not dict or not fields <= set(decoded) or set(decoded) - fields - {"turn_id"}
                    or decoded["schema"] != "summon.workspace.context/v1" or decoded["plane"] != "payload"
                    or decoded["task_id"] != binding["task_id"] or decoded["claim_id"] != original["attempt_id"]
                    or json.dumps(decoded, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8") != raw):
                raise ValueError
            for name in ("task_id", "claim_id"):
                _id(decoded[name])
            if "turn_id" in decoded:
                _id(decoded["turn_id"])
            if (type(decoded["attempt"]) is not int or not 1 <= decoded["attempt"] <= 1024
                    or type(decoded["lease_generation"]) is not int or not 1 <= decoded["lease_generation"] <= 2**63 - 1
                    or type(decoded["request_sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", decoded["request_sha256"]) is None):
                raise ValueError
            entries = decoded["entries"]
            if type(entries) is not list or not 1 <= len(entries) <= 8:
                raise ValueError
            values, seen_messages, seen_deliveries = [], set(), set()
            for entry in entries:
                if type(entry) is not dict or set(entry) != {"message_id", "delivery_id", "content_sha256", "content_utf8"}:
                    raise ValueError
                _id(entry["message_id"])
                _id(entry["delivery_id"])
                if (entry["message_id"] in seen_messages or entry["delivery_id"] in seen_deliveries
                        or type(entry["content_utf8"]) is not str):
                    raise ValueError
                seen_messages.add(entry["message_id"])
                seen_deliveries.add(entry["delivery_id"])
                body = entry["content_utf8"].encode("utf-8")
                if hashlib.sha256(body).hexdigest() != entry["content_sha256"]:
                    raise ValueError
                integers = json.loads(body)
                if type(integers) is not list or not 1 <= len(integers) <= 64:
                    raise ValueError
                values.extend(integers)
            if entries[0]["message_id"] != original["message_id"] or entries[0]["delivery_id"] != original["delivery_id"]:
                raise ValueError
        else:
            values = decoded
        if (type(values) is not list or not 1 <= len(values) <= 64
                or any(type(value) is not int or not -1_000_000 <= value <= 1_000_000 for value in values)):
            raise ValueError
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkspaceRuntimeError("invalid_fixture_input") from None
    count, total, squares = 0, 0, 0
    for value in values:
        count += 1
        total += value
        squares += value * value
    expected_result = {"count": count, "sum": total, "sum_squares": squares}
    expected = {key: original[key] for key in ("message_id", "delivery_id", "attempt_id", "context_sha256")}
    expected.update(request_frame_sha256=request_frame_sha256, request_sequence=request_sequence,
                    operation=original["operation"], result=expected_result)
    frame = worker.receive(timeout=timeout)
    if frame["kind"] != "fixture_result":
        raise WorkspaceRuntimeError("computation_result_required")
    if frame["binding"] != binding or frame["payload"] != expected:
        worker.close()
        raise WorkspaceRuntimeError("fixture_result_mismatch")
    source = {"kind": "fixed_fixture_verification", "qualification": "simulated",
              "binding": binding, "request": {key: value for key, value in expected.items() if key != "result"},
              "result": expected_result}
    source_bytes = json.dumps(source, sort_keys=True, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
    registration = {"reference": {"id": evidence_id, "sha256": hashlib.sha256(source_bytes).hexdigest()},
                    "category": "artifact", "task_id": binding["task_id"]}
    return {"status": "evidence_proposed", "verified": True, "durable": False,
            "task_completed": False, "execution_authorized": False,
            "registration": registration, "source_bytes": source_bytes}


def compile_turn_context(selection: dict, entries: list[dict]) -> bytes:
    """Canonical context-only envelope: 1–8 complete messages, 4096 total bytes.

    The context digest is an output, so selection.context_sha256 is excluded
    from the envelope. No authority blocks or implicit instructions are added.
    Caller source verification and actual turn admission remain separate.
    """
    protocol._attempt(selection)
    if type(entries) is not list or not 1 <= len(entries) <= 8:
        raise WorkspaceRuntimeError("invalid_context_entries")
    copied = copy.deepcopy(entries)
    for entry in copied:
        if type(entry) is not dict or set(entry) != {"message_id", "delivery_id", "content_sha256", "content_utf8"}:
            raise WorkspaceRuntimeError("invalid_context_entry")
        _id(entry["message_id"])
        _id(entry["delivery_id"])
        if type(entry["content_utf8"]) is not str:
            raise WorkspaceRuntimeError("invalid_context_content")
        try:
            raw = entry["content_utf8"].encode("utf-8")
        except UnicodeError:
            raise WorkspaceRuntimeError("invalid_context_content") from None
        if not raw or hashlib.sha256(raw).hexdigest() != entry["content_sha256"]:
            raise WorkspaceRuntimeError("context_content_digest_mismatch")
    if ([entry["message_id"] for entry in copied] != selection["selected_message_ids"]
            or len({entry["message_id"] for entry in copied}) != len(copied)
            or len({entry["delivery_id"] for entry in copied}) != len(copied)):
        raise WorkspaceRuntimeError("context_order_or_identity_mismatch")
    envelope = {"schema": "summon.workspace.context/v1", "plane": "payload",
                "task_id": selection["task_id"], "claim_id": selection["claim_id"],
                "attempt": selection["attempt"], "lease_generation": selection["owner_generation"],
                "request_sha256": selection["request_sha256"], "entries": copied}
    if "turn_id" in selection:
        envelope["turn_id"] = selection["turn_id"]
    compiled = json.dumps(envelope, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    if len(compiled) > transport.MAX_CONTEXT_BYTES:
        raise WorkspaceRuntimeError("compiled_context_limit")
    return compiled


def fixed_effect_sources(worker, delivery):
    """Build evidence proposals from actual fixed-launcher/owned-exit facts."""
    if type(worker) is not transport.OwnedFakeWorker:
        raise WorkspaceRuntimeError("owned_worker_required")
    observed = worker.observed_effects()
    selection, recipient = delivery["selection"], delivery["recipient"]
    binding = observed["binding"]
    if observed["operation"] == transport.CONTEXT_FIXTURE_OPERATION:
        expected_identity = {key: selection[key] for key in ("task_id", "claim_id", "attempt", "request_sha256")}
        expected_identity["lease_generation"] = selection["owner_generation"]
        if (type(selection["attempt"]) is not int or type(selection["owner_generation"]) is not int
                or observed["context_identity"] != expected_identity):
            raise WorkspaceRuntimeError("fixed_effect_binding_mismatch")
    if (binding["workspace_id"] != delivery["workspace_id"] or binding["run_id"] != delivery["run_id"]
            or binding["task_id"] != selection["task_id"] or binding["instance_id"] != recipient["instance_id"]
            or binding["epoch"] != recipient["epoch"] or binding["grant_id"] != delivery["grant_ref"]["id"]
            or binding["grant_sha256"] != delivery["grant_ref"]["sha256"]
            or observed["attempt_id"] != selection["claim_id"] or observed["context_sha256"] != selection["context_sha256"]
            or [entry["message_id"] for entry in observed["selected_entries"]] != selection["selected_message_ids"]
            or {"message_id": delivery["message_id"], "delivery_id": delivery["delivery_id"]} not in observed["selected_entries"]):
        raise WorkspaceRuntimeError("fixed_effect_binding_mismatch")
    common = {"schema": protocol.EFFECT_OBSERVATION_SCHEMA, "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
              "qualification": "simulated", "binding": protocol.effect_binding(delivery),
              "child_instance_id": recipient["instance_id"], "request_frame_sha256": observed["request_frame_sha256"],
              "request_sequence": observed["request_sequence"]}
    scope = {**common, "kind": "fixed_execution_scope", **{key: observed[key] for key in ("launcher", "operation", "provider_invoked")}}
    cleanup = {**common, "kind": "owned_child_cleanup", **{key: observed[key] for key in
               ("child_exit_observed", "pipes_closed", "channel_revoked", "exit_code")}}
    return protocol.validate_effect_observations(delivery, scope, cleanup)


class WorkspaceRuntime:
    """Host-only integration object; workers must never receive this API.

    ``authorize`` independently validates each typed command against current
    supervisor/channel grants. ``resolve_evidence`` returns verified source bytes
    for the named role/reference/scope. Neither has a permissive default; a digest
    or successful pure reducer call is not a substitute for source verification.
    """

    def __init__(self, runs_root, run_id: str, workspace_id: str, *, content: ContentStore | None = None,
                 authorize: Callable, resolve_evidence: Callable, clock=None):
        _id(workspace_id)
        if (content is not None and type(content) is not ContentStore
                or not callable(authorize) or not callable(resolve_evidence)):
            raise WorkspaceRuntimeError("trusted_integration_required")
        layout = resolve_workspace_layout(runs_root, run_id)
        self._coordinator = _WorkspaceCoordinator(layout.inner_runs_root, run_id, clock=clock)
        self.workspace_id = workspace_id
        self._content_root = os.path.join(layout.inner_run_directory, "workspace-content")
        if content is not None and os.path.normcase(content._root) != os.path.normcase(self._content_root):
            raise WorkspaceRuntimeError("content_root_scope_mismatch")
        # No directory here: base create rejects foreign files before preparation.
        self._content = content
        self._authorize = authorize
        self._resolve_evidence = resolve_evidence
        self._worker_ingress = {}  # Issued connections only; no durable authority.
        self._supervisor_consumers = {}
        self._supervisor_offers = {}
        self._operator_handles = {}
        # Linked replacement authority is deliberately separate from the
        # legacy command handle.  A linked proposal can create one new queued
        # delivery and therefore must never be accepted by the v1 cancel /
        # dispose binder or by a browser-controlled generic event append.
        self._operator_link_handles = {}
        self._operator_send_handles = {}
        self._operator_draft_handles = {}

    def _operator_send_contract(self):
        self._contract()
        expected = {"schema": "summon.workspace.operator-send-base/v1",
                    "event_class": admission.OPERATOR_SEND_EVENT, "atomic_message_queue": True,
                    "exact_operation_lookup": True, "derived_admission_evidence": True,
                    "authenticated_browser_ingress": False, "max_operations": 32,
                    "max_content_bytes": 2048, "max_request_bytes": 4096}
        method = getattr(SwarmCoordinator, "workspace_operator_send_contract", None)
        if not callable(method) or method() != expected:
            raise WorkspaceRuntimeError("operator_send_base_unavailable")
        return expected

    def install_operator_messages(self, scope, *, authorize_message, resolve_grant):
        """Install finite local-operator authority, independently of read sessions.

        Scope sources bind the stable host operator to a workspace/run and a
        receiving task/grant. Handles are memory-only and must be reinstalled
        after restart. Both callbacks must be nonblocking and perform no source
        I/O or nested mutation; source verification happens outside ownership.
        """
        self._operator_send_contract()
        if (not callable(authorize_message) or not callable(resolve_grant)
                or len(self._operator_send_handles) >= 16):
            raise WorkspaceRuntimeError("operator_authority_required")
        scope = copy.deepcopy(scope)
        admission._object(scope, {"workspace_id", "run_id", "operator_id", "targets"})
        _id(scope["operator_id"])
        if (scope["workspace_id"] != self.workspace_id or scope["run_id"] != self._coordinator.run_id
                or type(scope["targets"]) is not list or not 1 <= len(scope["targets"]) <= 16):
            raise WorkspaceRuntimeError("operator_scope_invalid")
        targets = {}
        for item in scope["targets"]:
            admission._object(item, {"target", "task_id", "send_scope_ref"}, {"recipient_instance_id"})
            _id(item["target"]); _id(item["task_id"])
            if "recipient_instance_id" in item:
                _id(item["recipient_instance_id"])
            protocol._reference(item["send_scope_ref"])
            if item["target"] in targets:
                raise WorkspaceRuntimeError("operator_scope_invalid")
            targets[item["target"]] = copy.deepcopy(item)
        current, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = current.get("workspace")
        if type(workspace) is not dict or len(targets) > len(workspace["lanes"]):
            raise WorkspaceRuntimeError("operator_scope_invalid")
        if self._authorize({"operation": "install_operator_messages", "scope": copy.deepcopy(scope)},
                           copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        installed = {"scope": scope, "targets": targets, "authorize": authorize_message, "resolve_grant": resolve_grant}
        for target in targets:
            self._operator_message_facts(installed, current, target)
        handle = object()
        self._operator_send_handles[handle] = installed
        return handle

    def revoke_operator_messages(self, handle):
        if self._operator_send_handles.pop(handle, None) is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")

    def install_operator_draft_retention(self, scope, *, authorize_retention, retention_secret,
                                         key_epoch):
        """Install independent, memory-only authority for browser draft keys.

        The retention secret never crosses this host boundary.  This authority
        is deliberately separate from worker ingress and message admission;
        a browser read session cannot create or install it.
        """
        if (not callable(authorize_retention) or type(retention_secret) is not bytes
                or not 32 <= len(retention_secret) <= 64
                or type(key_epoch) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", key_epoch)):
            raise WorkspaceRuntimeError("operator_retention_required")
        scope = copy.deepcopy(scope)
        admission._object(scope, {"workspace_id", "run_id", "operator_id", "targets"})
        _id(scope["workspace_id"]); _id(scope["run_id"]); _id(scope["operator_id"])
        if (scope["workspace_id"] != self.workspace_id or scope["run_id"] != self._coordinator.run_id
                or type(scope["targets"]) is not list or not 1 <= len(scope["targets"])
                or len(scope["targets"]) > 16):
            raise WorkspaceRuntimeError("operator_retention_scope_invalid")
        targets = {}
        current, torn = self._coordinator._load()
        if torn or type(current.get("workspace")) is not dict:
            raise WorkspaceRuntimeError("journal_recovery_required")
        for item in scope["targets"]:
            admission._object(item, {"target", "task_id"})
            _id(item["target"]); _id(item["task_id"])
            if item["target"] in targets or item["task_id"] not in current["workspace"]["lanes"]:
                raise WorkspaceRuntimeError("operator_retention_scope_invalid")
            targets[item["target"]] = item["task_id"]
        if self._authorize({"operation": "install_operator_draft_retention", "scope": copy.deepcopy(scope)},
                           copy.deepcopy(current["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        if len(self._operator_draft_handles) >= 16:
            raise WorkspaceRuntimeError("operator_retention_required")
        handle = object()
        self._operator_draft_handles[handle] = {
            "scope": scope, "targets": targets, "authorize": authorize_retention,
            "secret": bytes(retention_secret), "key_epoch": key_epoch}
        return handle

    def revoke_operator_draft_retention(self, handle):
        if self._operator_draft_handles.pop(handle, None) is None:
            raise WorkspaceRuntimeError("operator_retention_required")

    def describe_operator_draft_retention(self, handle):
        installed = self._operator_draft_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("operator_retention_required")
        current, torn = self._coordinator._load()
        if torn or type(current.get("workspace")) is not dict:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if installed["authorize"](None, copy.deepcopy(current["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        operator_scope = self._operator_draft_scope(installed)
        return {"workspace_id": installed["scope"]["workspace_id"],
                "run_id": installed["scope"]["run_id"],
                "operator_scope": operator_scope, "key_epoch": installed["key_epoch"],
                "targets": [{"target": target, "task_id": task_id} for target, task_id in installed["targets"].items()]}

    def derive_operator_draft_key(self, handle, request, *, authorization_constraint=None):
        if (type(request) is not dict or set(request) != {"target", "operation_key", "state"}
                or type(request["target"]) is not str or type(request["operation_key"]) is not str
                or not re.fullmatch(r"[a-f0-9]{32}", request["operation_key"])
                or request["state"] not in {"unsent", "sending", "uncertain"}):
            raise WorkspaceRuntimeError("operator_retention_request_invalid")
        installed = self._operator_draft_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("operator_retention_required")
        if authorization_constraint is not None and (not callable(authorization_constraint)
                                                       or authorization_constraint() is not True):
            raise WorkspaceRuntimeError("operator_constraint_refused")
        current, torn = self._coordinator._load()
        if torn or type(current.get("workspace")) is not dict:
            raise WorkspaceRuntimeError("journal_recovery_required")
        task_id = installed["targets"].get(request["target"])
        if task_id is None or task_id not in current["workspace"]["lanes"]:
            raise WorkspaceRuntimeError("operator_target_not_permitted")
        if installed["authorize"](None, copy.deepcopy(current["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        if authorization_constraint is not None and authorization_constraint() is not True:
            raise WorkspaceRuntimeError("operator_constraint_refused")
        descriptor = {"version": 1, "workspace_id": installed["scope"]["workspace_id"],
                      "run_id": installed["scope"]["run_id"], "operator_id": installed["scope"]["operator_id"],
                      "target": request["target"], "task_id": task_id, "operation_key": request["operation_key"],
                      "state": request["state"], "key_epoch": installed["key_epoch"]}
        canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
        raw_key = hmac.new(installed["secret"], b"summon.workspace.operator-draft-key/v1\0" + canonical,
                           hashlib.sha256).digest()
        operator_scope = self._operator_draft_scope(installed)
        return {"key_b64": base64.urlsafe_b64encode(raw_key).decode("ascii").rstrip("="),
                "operator_scope": operator_scope, "key_epoch": installed["key_epoch"],
                "max_text_bytes": 2048, "max_record_bytes": 4096}

    @staticmethod
    def _operator_draft_scope(installed):
        scope = installed["scope"]
        descriptor = json.dumps({"version": 1, "workspace_id": scope["workspace_id"],
                                 "run_id": scope["run_id"], "operator_id": scope["operator_id"],
                                 "key_epoch": installed["key_epoch"]}, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
        return hmac.new(installed["secret"], b"summon.workspace.operator-draft-scope/v1\0" + descriptor,
                        hashlib.sha256).hexdigest()

    def _operator_message_facts(self, installed, current, target):
        """Read and bind exact private sources before entering the writer."""
        item = installed["targets"].get(target)
        if item is None:
            raise WorkspaceRuntimeError("operator_target_not_permitted")
        workspace = current["workspace"]
        known = projection._known_ref(workspace, item["send_scope_ref"], item["task_id"])
        raw = self._resolve_evidence(copy.deepcopy(known))
        if (known["category"] != "grant" or type(raw) is not bytes or not 1 <= len(raw) <= 8192
                or hashlib.sha256(raw).hexdigest() != item["send_scope_ref"]["sha256"]):
            raise WorkspaceRuntimeError("operator_scope_source_unverified")
        source = _decode_workspace_source(raw, item["send_scope_ref"])
        admission._object(source, {"schema", "workspace_id", "run_id", "operator_id", "scope"})
        if (source["schema"] != "summon.workspace.operator-scope/v1"
                or any(source[key] != installed["scope"][key] for key in ("workspace_id", "run_id", "operator_id"))
                or type(source["scope"]) is not dict or "reference" in source["scope"]):
            raise WorkspaceRuntimeError("operator_scope_source_mismatch")
        resolved = {"operator_id": source["operator_id"],
                    "scope": {**source["scope"], "reference": copy.deepcopy(item["send_scope_ref"])}}
        if ("recipient_instance_id" in item
                and resolved["scope"].get("recipient", {}).get("instance_id") != item["recipient_instance_id"]):
            raise WorkspaceRuntimeError("operator_destination_source_mismatch")
        resolved = admission.validate_operator_scope(resolved, {"target": target}, coordinator_state=current,
            workspace_state=workspace, now_ms=int(self._coordinator.clock() * 1000))
        if resolved["scope"]["destination_task_id"] != item["task_id"]:
            raise WorkspaceRuntimeError("operator_scope_source_mismatch")
        reference = resolved["scope"]["delivery_grant_ref"]
        receiving = projection._known_ref(workspace, reference, item["task_id"])
        grant_raw = self._resolve_evidence(copy.deepcopy(receiving))
        if (receiving["category"] != "grant" or type(grant_raw) is not bytes or not 1 <= len(grant_raw) <= 8192
                or hashlib.sha256(grant_raw).hexdigest() != reference["sha256"]):
            raise WorkspaceRuntimeError("execution_grant_source_unverified")
        self._operator_receiving_grant(installed, current, resolved)
        return resolved, [copy.deepcopy(known), copy.deepcopy(receiving)]

    @staticmethod
    def _operator_receiving_grant(installed, current, resolved):
        workspace, scope = current["workspace"], resolved["scope"]
        reference = scope["delivery_grant_ref"]
        expected = {"grant_ref": reference, "task_id": scope["destination_task_id"],
                    "goal_revision": workspace["goal"]["revision"], "recipient": scope["recipient"], "revoked": False}
        observed = installed["resolve_grant"](copy.deepcopy(current), copy.deepcopy(workspace), copy.deepcopy(reference))
        if (type(observed) is not dict or type(observed.get("goal_revision")) is not int
                or type(observed.get("revoked")) is not bool or observed != expected):
            raise WorkspaceRuntimeError("execution_grant_scope_mismatch")

    def describe_operator_messages(self, handle):
        """Private host target descriptions, never authority to send or launch."""
        self._operator_send_contract()
        installed = self._operator_send_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        current, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if installed["authorize"](None, copy.deepcopy(current["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        for target in installed["targets"]:
            self._operator_message_facts(installed, current, target)
        return copy.deepcopy(installed["scope"])

    def send_operator_message(self, handle, request, *, authorization_constraint=None):
        return self._operator_message_request(handle, request, authorization_constraint=authorization_constraint, lookup=False)

    def reconcile_operator_message(self, handle, request, *, authorization_constraint=None):
        return self._operator_message_request(handle, request, authorization_constraint=authorization_constraint, lookup=True)

    def _operator_message_request(self, handle, request, *, authorization_constraint, lookup):
        self._operator_send_contract()
        checked = admission.operator_request(request)
        installed = self._operator_send_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        if authorization_constraint is not None and not callable(authorization_constraint):
            raise WorkspaceRuntimeError("operator_constraint_invalid")
        current, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        # Decline read-only/expired sessions before reading private grant sources.
        if authorization_constraint is not None and authorization_constraint() is not True:
            raise WorkspaceRuntimeError("operator_constraint_refused")
        resolved, sources = self._operator_message_facts(installed, current, checked["target"])
        operation, digest = admission.operator_request_identity(current["workspace"], resolved, checked)
        command = {"operation": "operator_message.lookup" if lookup else "operator_message.send",
                   **{key: installed["scope"][key] for key in ("workspace_id", "run_id", "operator_id")},
                   "request": {"operation_key": checked["operation_key"], "target": checked["target"], "request_sha256": digest}}

        def resolve_operator(state, workspace, intent):
            if self._operator_send_handles.get(handle) is not installed or intent != checked:
                raise WorkspaceRuntimeError("installed_operator_authority_required")
            if authorization_constraint is not None and authorization_constraint() is not True:
                raise WorkspaceRuntimeError("operator_constraint_refused")
            if (self._authorize(copy.deepcopy(command), copy.deepcopy(workspace)) is not True
                    or installed["authorize"](copy.deepcopy(command), copy.deepcopy(workspace)) is not True):
                raise WorkspaceRuntimeError("command_not_authorized")
            if any(workspace["evidence"].get(source["reference"]["id"]) != source for source in sources):
                raise WorkspaceRuntimeError("registered_source_changed")
            self._operator_receiving_grant(installed, state, resolved)
            return copy.deepcopy(resolved)

        resolve_operator(current, current["workspace"], checked)
        previous = current["workspace"].get("send_operations", {}).get(operation)
        if previous is not None and previous.get("request_sha256") != digest:
            raise WorkspaceRuntimeError("operator_operation_conflict")

        def publish_content(intent, content_id):
            raw = intent["text"].encode("utf-8")
            descriptor = ContentRef(content_id, hashlib.sha256(raw).hexdigest(), len(raw))
            self._content_store().put(descriptor, raw, require_namespace_durable=False)
            return {"ref": descriptor.reference, "sha256": descriptor.sha256, "utf8_bytes": descriptor.payload_bytes}

        try:
            if lookup:
                return SwarmCoordinator.reconcile_operator_message(self._coordinator, checked, resolve_operator=resolve_operator)
            return SwarmCoordinator.admit_operator_message(self._coordinator, checked,
                resolve_operator=resolve_operator, prepare_content=publish_content)
        except (ValueError, RuntimeError, OSError) as exc:
            # The base may have appended before a lost response or revocation.
            # Only same-key owned reconciliation may resolve this uncertainty.
            raise WorkspaceRuntimeError("operator_lookup_uncertain" if lookup else "operator_send_uncertain") from exc

    def install_operator_commands(self, scope, *, authorize_command):
        """Install finite host authority; browser/read credentials cannot do this.

        The callback receives detached canonical request and current workspace.
        It must return True independently before and during each owner operation.
        A restarted host must explicitly reinstall its authority; no handle is
        serialized or reconstructed from a command, name, source or receipt.
        """
        self._contract()
        if not callable(authorize_command) or len(self._operator_handles) >= 16:
            raise WorkspaceRuntimeError("operator_authority_required")
        scope = copy.deepcopy(scope)
        admission._object(scope, {"workspace_id", "run_id", "targets"})
        if (scope["workspace_id"] != self.workspace_id or scope["run_id"] != self._coordinator.run_id
                or type(scope["targets"]) is not list or not 1 <= len(scope["targets"]) <= 128):
            raise WorkspaceRuntimeError("operator_scope_invalid")
        targets = {}
        for item in scope["targets"]:
            admission._object(item, {"delivery_id", "actions"})
            _id(item["delivery_id"])
            if (type(item["actions"]) is not list or not 1 <= len(item["actions"]) <= 2
                    or any(action not in self._OPERATOR_ACTIONS for action in item["actions"])
                    or len(set(item["actions"])) != len(item["actions"]) or item["delivery_id"] in targets):
                raise WorkspaceRuntimeError("operator_scope_invalid")
            targets[item["delivery_id"]] = tuple(item["actions"])
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if self._authorize({"operation": "install_operator_commands", "scope": copy.deepcopy(scope)},
                           copy.deepcopy(state["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        handle = object()
        self._operator_handles[handle] = {"scope": scope, "targets": targets, "authorize": authorize_command}
        return handle

    def revoke_operator_commands(self, handle):
        """Revoke one in-memory command handle without touching the journal."""
        if self._operator_handles.pop(handle, None) is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")

    def install_operator_linked_replacements(self, scope, *, authorize_link,
                                             resolve_link):
        """Install a finite host-only scope for linked replacement admission.

        ``scope`` contains opaque parent/recipient handles only.  The trusted
        resolver maps those handles to the current delivery, worker and typed
        authority sources; neither the public proposal nor the browser can
        provide private IDs, proof bytes or journal payloads.  The handle is
        memory-only and must be reinstalled after restart.
        """
        self._contract()
        if (not callable(authorize_link) or not callable(resolve_link)
                or len(self._operator_link_handles) >= 16):
            raise WorkspaceRuntimeError("operator_authority_required")
        scope = copy.deepcopy(scope)
        admission._object(scope, {"workspace_id", "run_id", "targets"})
        if (scope["workspace_id"] != self.workspace_id
                or scope["run_id"] != self._coordinator.run_id
                or type(scope["targets"]) is not list
                or not 1 <= len(scope["targets"] ) <= 128):
            raise WorkspaceRuntimeError("operator_link_scope_invalid")
        targets = {}
        for item in scope["targets"]:
            admission._object(item, {"parent_target", "recipient_targets"})
            _id(item["parent_target"])
            if (type(item["recipient_targets"]) is not list
                    or not 1 <= len(item["recipient_targets"]) <= 16
                    or any(type(value) is not str for value in item["recipient_targets"])
                    or len(set(item["recipient_targets"])) != len(item["recipient_targets"])
                    or item["parent_target"] in targets):
                raise WorkspaceRuntimeError("operator_link_scope_invalid")
            for value in item["recipient_targets"]:
                _id(value)
            targets[item["parent_target"]] = tuple(item["recipient_targets"])
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if self._authorize({"operation": "install_operator_linked_replacements",
                            "scope": copy.deepcopy(scope)},
                           copy.deepcopy(state["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        handle = object()
        self._operator_link_handles[handle] = {
            "scope": scope, "targets": targets,
            "authorize": authorize_link, "resolve": resolve_link,
        }
        return handle

    def revoke_operator_linked_replacements(self, handle):
        if self._operator_link_handles.pop(handle, None) is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")

    def describe_operator_linked_replacements(self, handle):
        installed = self._operator_link_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if installed["authorize"](None, copy.deepcopy(state["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        return copy.deepcopy(installed["scope"])

    @staticmethod
    def _linked_source_matches(raw, reference, expected, role):
        """Verify one registered authority source, including its bytes."""
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise WorkspaceRuntimeError("linked_replacement_source_unverified")
        try:
            source = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise WorkspaceRuntimeError("linked_replacement_source_unverified") from None
        required = {"schema", "role", "parent_delivery_id", "task_id", "recipient",
                    "generation", "expires_at_ms", "revoked"}
        if (type(source) is not dict or set(source) != required
                or source["schema"] != "summon.workspace.linked-replacement-authority/v1"
                or source["role"] != role):
            raise WorkspaceRuntimeError("linked_replacement_source_unverified")
        if any(source[key] != expected[key]
               for key in ("parent_delivery_id", "task_id", "recipient", "generation",
                           "expires_at_ms", "revoked")):
            raise WorkspaceRuntimeError("linked_replacement_source_binding")
        return copy.deepcopy(source)

    def _operator_linked_request(self, handle, proposal, workspace,
                                 authorization_constraint=None):
        """Resolve one opaque proposal to a source-authenticated child plan."""
        installed = self._operator_link_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        try:
            checked = admission.linked_replacement_proposal(proposal)
        except (ValueError, TypeError, KeyError, admission.WorkspaceAdmissionError):
            raise WorkspaceRuntimeError("linked_replacement_proposal_invalid") from None
        allowed = installed["targets"].get(checked["parent_target"])
        if not allowed or checked["recipient_target"] not in allowed:
            raise WorkspaceRuntimeError("operator_link_scope_conflict")
        if authorization_constraint is not None and (
                not callable(authorization_constraint)
                or authorization_constraint() is not True):
            raise WorkspaceRuntimeError("operator_authorization_constraint_refused")
        request = {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
                   **copy.deepcopy(checked)}
        if installed["authorize"](copy.deepcopy(request), copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        try:
            authority = installed["resolve"](
                checked["parent_target"], checked["recipient_target"], copy.deepcopy(workspace))
        except Exception as exc:
            raise WorkspaceRuntimeError("linked_replacement_authority_unavailable") from exc
        try:
            admission._object(authority, {"parent_delivery_id", "parent_target",
                                          "recipient_target", "task_id", "recipient",
                                          "generation", "now_ms", "sources"})
            parent = workspace["deliveries"][authority["parent_delivery_id"]]
            message = workspace["messages"][parent["message_id"]]
            checked_authority = admission.validate_linked_replacement_authority(
                checked, parent=parent, message=message, authority=authority)
        except (KeyError, TypeError, ValueError, admission.WorkspaceAdmissionError):
            raise WorkspaceRuntimeError("linked_replacement_authority_invalid") from None
        if any(parent.get(key) != workspace.get(key) or message.get(key) != workspace.get(key)
               for key in ("workspace_id", "run_id")):
            raise WorkspaceRuntimeError("operator_link_scope_conflict")
        task_id = checked_authority["task_id"]
        for role, reference in checked_authority["evidence"].items():
            registered = workspace.get("evidence", {}).get(reference["id"])
            if (not isinstance(registered, dict)
                    or registered.get("reference") != reference
                    or registered.get("task_id") != task_id
                    or registered.get("delivery_id") != parent["delivery_id"]):
                raise WorkspaceRuntimeError("linked_replacement_source_unverified")
            self._linked_source_matches(
                self._resolve_evidence(copy.deepcopy(registered)), reference,
                authority["sources"][role], role)
        inherited = sorted(set(parent["inherited_uncertainty"]) |
                            {key for key, value in parent["certainty"].items()
                             if value == "unknown"})
        grant_ref = (checked_authority["evidence"].get("fresh_attempt_grant")
                     or copy.deepcopy(parent["grant_ref"]))
        child = copy.deepcopy(parent)
        child.update({
            "delivery_id": "linked-" + checked["operation_key"],
            "parent_delivery_id": parent["delivery_id"],
            "recipient": copy.deepcopy(checked_authority["recipient"]),
            "grant_ref": copy.deepcopy(grant_ref), "state": "queued",
            "reason": None, "selection": None, "ack_level": None,
            "certainty": {"contact": "not_attempted", "spend": "not_incurred",
                           "cleanup": "not_started"},
            "inherited_uncertainty": inherited,
        })
        event = {"event": "workspace_delivery_linked", "protocol": protocol.PROTOCOL,
                 "workspace_id": self.workspace_id, "run_id": self._coordinator.run_id,
                 "operation_key": "linked-replacement-" + checked["operation_key"],
                 "expected_revision": workspace["revision"],
                 "payload": {"delivery": child,
                             "evidence": copy.deepcopy(checked_authority["evidence"] )}}
        return {"proposal": checked, "request": request, "authority": authority,
                "authority_check": checked_authority, "parent": copy.deepcopy(parent),
                "message": copy.deepcopy(message), "child": child, "event": event}

    def execute_operator_linked_replacement(self, handle, proposal, *,
                                            authorization_constraint=None):
        """Queue exactly one trusted linked child through the owner append.

        A proposal is not itself a delivery or retry.  All private sources are
        resolved and byte-verified before the owner-fenced append; the parent
        remains unchanged.  Capacity or lost-response failures stay explicit.
        """
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        plan = self._operator_linked_request(handle, proposal, state["workspace"],
                                             authorization_constraint)
        current_event = state["workspace"].get("operations", {}).get(
            plan["event"]["operation_key"])
        if current_event is not None:
            records, later_torn, _ = self._coordinator._read_records_snapshot()
            if later_torn:
                raise WorkspaceRuntimeError("linked_replacement_lookup_uncertain")
            matches = [record.get("workspace_event") for record in records
                       if record.get("event") == CONTAINER
                       and isinstance(record.get("workspace_event"), dict)
                       and record["workspace_event"].get("operation_key") == plan["event"]["operation_key"]]
            historical = matches[0] if len(matches) == 1 else None
            immutable_keys = ("event", "protocol", "workspace_id", "run_id",
                              "operation_key", "payload")
            same_immutable = (isinstance(historical, dict)
                              and all(historical.get(key) == plan["event"].get(key)
                                      for key in immutable_keys))
            if (not same_immutable
                    or projection._event(historical) != current_event):
                raise WorkspaceRuntimeError("linked_replacement_operation_conflict")
            return {"status": "already_recorded", "operation_key": plan["event"]["operation_key"],
                    "delivery_id": plan["child"]["delivery_id"],
                    "revision": state["workspace"]["revision"]}
        with self._coordinator._mutation() as (owner, current, _torn):
            if current["workspace"]["revision"] != state["workspace"]["revision"]:
                raise WorkspaceRuntimeError("journal_snapshot_changed")
            current_plan = self._operator_linked_request(
                handle, proposal, current["workspace"], authorization_constraint)
            if current_plan["event"] != plan["event"]:
                raise WorkspaceRuntimeError("linked_replacement_operation_conflict")
            # Re-resolve the host authority immediately before freezing the
            # append.  This catches a revoked/expired fence or recipient
            # change that occurred after the initial coherent read, while the
            # sole owner still holds the mutation boundary.
            final_plan = self._operator_linked_request(
                handle, proposal, current["workspace"], authorization_constraint)
            if final_plan["event"] != plan["event"]:
                raise WorkspaceRuntimeError("linked_replacement_authority_changed")
            following = projection.apply_event(
                current["workspace"], plan["event"],
                _task_view(current, run_id=self._coordinator.run_id,
                           now_ms=int(self._coordinator.clock() * 1000)))
            wrapper = {"event": CONTAINER, "workspace_event": copy.deepcopy(plan["event"]),
                       "observed_at_ms": int(self._coordinator.clock() * 1000)}
            contract = self._contract()
            frozen = encode_journal_record({**wrapper, "generation": owner.generation},
                                            timestamp=self._coordinator.clock())
            if len(frozen) > contract["max_event_bytes"]:
                raise WorkspaceRuntimeError("workspace_envelope_limit")
            installed = self._operator_link_handles.get(handle)
            if installed is None:
                raise WorkspaceRuntimeError("installed_operator_authority_required")

            def final_append_authority(latest):
                # This is the last authority check before the base writer's
                # frozen append.  Re-resolve the opaque proposal against the
                # latest coherent workspace so revocation, expiry, recipient
                # changes and handle removal cannot be hidden by a captured
                # pre-mutation callback.
                if self._operator_link_handles.get(handle) is not installed:
                    raise WorkspaceRuntimeError("installed_operator_authority_required")
                latest_plan = self._operator_linked_request(
                    handle, proposal, latest["workspace"], authorization_constraint)
                if self._operator_link_handles.get(handle) is not installed:
                    raise WorkspaceRuntimeError("installed_operator_authority_required")
                if latest_plan["event"] != plan["event"]:
                    raise WorkspaceRuntimeError("linked_replacement_authority_changed")
                if self._authorize({"operation": "execute_operator_linked_replacement",
                                    "proposal": copy.deepcopy(plan["proposal"])},
                                   copy.deepcopy(latest["workspace"])) is not True:
                    raise WorkspaceRuntimeError("command_not_authorized")
                if installed["authorize"](copy.deepcopy(plan["request"]),
                                            copy.deepcopy(latest["workspace"])) is not True:
                    raise WorkspaceRuntimeError("command_not_authorized")
                if (authorization_constraint is not None
                        and authorization_constraint() is not True):
                    raise WorkspaceRuntimeError("operator_authorization_constraint_refused")
                return True
            try:
                self._coordinator._append_with_state(
                    owner, wrapper, state=current, frozen=frozen,
                    before_append=final_append_authority)
            except SwarmCoordinatorError as exc:
                if str(exc) in {
                        "swarm journal limit reached",
                        "swarm journal capacity degraded: settlement reserve cannot be preserved",
                        "swarm journal capacity degraded: economics record reserve cannot be preserved",
                        "workspace evidence capacity cannot preserve settlement slots",
                        "workspace event capacity cannot preserve settlement slots"}:
                    raise WorkspaceRuntimeError("linked_replacement_capacity_refused") from None
                raise WorkspaceRuntimeError("linked_replacement_append_uncertain") from exc
            except (SwarmConflictError, OSError, OwnershipLostError) as exc:
                # An I/O/ownership failure may occur after the append boundary
                # and is never converted into a definite capacity refusal.
                raise WorkspaceRuntimeError("linked_replacement_append_uncertain") from exc
            return {"status": "recorded", "operation_key": plan["event"]["operation_key"],
                    "delivery_id": plan["child"]["delivery_id"],
                    "revision": following["revision"]}

    def reconcile_operator_linked_replacement(self, handle, proposal, *,
                                              authorization_constraint=None):
        """Look up one linked operation without creating a child.

        A missing operation is deliberately ``not_observed``; lookup never
        turns a lost response into an implicit retry.  Existing history is
        compared by immutable event content and its recorded operations digest
        rather than by the current revision, which changes after unrelated
        workspace events.
        """
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        plan = self._operator_linked_request(handle, proposal, state["workspace"],
                                             authorization_constraint)
        operation_key = plan["event"]["operation_key"]
        observed_digest = state["workspace"].get("operations", {}).get(operation_key)
        base = {"operation_key": operation_key,
                "delivery_id": plan["child"]["delivery_id"],
                "revision": state["workspace"]["revision"]}
        if observed_digest is None:
            return {"status": "not_observed", **base}
        records, later_torn, _ = self._coordinator._read_records_snapshot()
        if later_torn:
            raise WorkspaceRuntimeError("linked_replacement_lookup_uncertain")
        matches = [record.get("workspace_event") for record in records
                   if record.get("event") == CONTAINER
                   and isinstance(record.get("workspace_event"), dict)
                   and record["workspace_event"].get("operation_key") == operation_key]
        historical = matches[0] if len(matches) == 1 else None
        immutable_keys = ("event", "protocol", "workspace_id", "run_id",
                          "operation_key", "payload")
        if (not isinstance(historical, dict)
                or not all(historical.get(key) == plan["event"].get(key)
                           for key in immutable_keys)
                or projection._event(historical) != observed_digest):
            raise WorkspaceRuntimeError("linked_replacement_operation_conflict")
        return {"status": "already_recorded", **base}

    # Short explicit aliases for trusted-host integrations.  They retain the
    # operator prefix so callers do not confuse this path with generic events.
    install_operator_link = install_operator_linked_replacements
    execute_operator_link = execute_operator_linked_replacement

    def describe_operator_commands(self, handle):
        """Host-only installed scope discovery; never serialize raw targets to UI.

        ``authorize_command(None, workspace)`` must separately permit discovery.
        A successful description grants no execution or future lookup authority.
        """
        installed = self._operator_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if installed["authorize"](None, copy.deepcopy(state["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        return copy.deepcopy(installed["scope"])

    _OPERATOR_ACTIONS = {
        "cancel_queued_context": ("queued", "cancelled", "authorized_cancellation", "grant"),
        "dispose_held_context": ("held_for_recovery", "dead_lettered", "authenticated_disposition", "event"),
        "retain_held_context": ("held_for_recovery", "held_for_recovery", "operator_hold", "event"),
    }

    @staticmethod
    def _linked_delivery_scope_valid(workspace, delivery):
        """Validate the immutable parent binding before an operator action.

        Linked children may be advanced or disposed only after the trusted
        scope names the concrete child.  This check keeps that extension from
        becoming a generic raw-ID capability: the child must retain its
        parent's message/workspace lineage and the conservative inherited
        uncertainty union.
        """
        parent_id = delivery.get("parent_delivery_id")
        if parent_id is None:
            return True
        parent = workspace.get("deliveries", {}).get(parent_id)
        if not isinstance(parent, dict) or parent_id == delivery.get("delivery_id"):
            return False
        if (delivery.get("message_id") != parent.get("message_id")
                or delivery.get("workspace_id") != parent.get("workspace_id")
                or delivery.get("run_id") != parent.get("run_id")):
            return False
        expected = sorted(set(parent.get("inherited_uncertainty", [])) |
                          {key for key, value in parent.get("certainty", {}).items()
                           if value == "unknown"})
        return delivery.get("inherited_uncertainty") == expected

    def _operator_request(self, handle, raw, workspace, authorization_constraint=None):
        installed = self._operator_handles.get(handle)
        if installed is None or type(raw) is not bytes or not 1 <= len(raw) <= 4096:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        try:
            source = transport._decode(raw)
            admission._object(source, {"schema", "operation_key", "action", "delivery", "task_id", "content"})
            if (source["schema"] != "summon.workspace.operator-command-request/v1"
                    or type(source["operation_key"]) is not str
                    or re.fullmatch(r"[a-f0-9]{32}", source["operation_key"]) is None
                    or source["action"] not in self._OPERATOR_ACTIONS
                    or admission._canonical_event_bytes(source) != raw):
                raise WorkspaceRuntimeError("operator_request_invalid")
            identity = source["delivery"]
            delivery = workspace["deliveries"][identity["delivery_id"]]
            message = workspace["messages"][delivery["message_id"]]
            protocol.validate_record(delivery)
            protocol.validate_record(message)
            if "parent_delivery_id" not in delivery:
                protocol.validate_delivery_binding(message, delivery)
            immutable = {key: value for key, value in delivery.items()
                         if key not in {"state", "reason", "ack_level", "certainty"}}
            if (delivery["kind"] != "delivery" or message["kind"] not in {"message", "operator_message"}
                    or not self._linked_delivery_scope_valid(workspace, delivery)
                    or immutable != identity or source["content"] != message["content"]
                    or source["task_id"] != message["destination_task_id"]
                    or source["task_id"] not in workspace["lanes"]
                    or any(delivery[key] != workspace[key] or message[key] != workspace[key]
                           for key in ("workspace_id", "run_id"))
                    or source["action"] not in installed["targets"].get(delivery["delivery_id"], ())):
                raise WorkspaceRuntimeError("operator_request_scope_conflict")
        except (KeyError, TypeError, AttributeError):
            raise WorkspaceRuntimeError("operator_request_invalid") from None
        if installed["authorize"](copy.deepcopy(source), copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        if authorization_constraint is not None and (not callable(authorization_constraint)
                                                   or authorization_constraint() is not True):
            raise WorkspaceRuntimeError("operator_authorization_constraint_refused")
        request_sha = hashlib.sha256(raw).hexdigest()
        decision = admission._canonical_event_bytes({"schema": "summon.workspace.operator-decision/v1",
            "decision": "authorized", "request_sha256": request_sha, "request": source})
        if len(decision) > 4096:
            raise WorkspaceRuntimeError("operator_decision_content_limit")
        decision_sha = hashlib.sha256(decision).hexdigest()
        descriptor = ContentRef("blob-" + decision_sha[:32], decision_sha, len(decision))
        initial, target, role, category = self._OPERATOR_ACTIONS[source["action"]]
        reference = {"id": descriptor.reference + ".operator-" + source["operation_key"], "sha256": decision_sha}
        proof = {"reference": reference, "category": category, "task_id": source["task_id"],
                 "delivery_id": delivery["delivery_id"],
                 "settlement_for": {"delivery_id": delivery["delivery_id"], "target_state": target, "role": role}}
        query = {"workspace_id": self.workspace_id, "run_id": self._coordinator.run_id,
                 "operation_key": source["operation_key"], "request_sha256": request_sha,
                 "evidence_operation_key": "operator-evidence-" + source["operation_key"],
                 "transition_operation_key": "operator-transition-" + source["operation_key"]}
        return {"source": source, "decision": decision, "descriptor": descriptor, "proof": proof,
                "query": query, "initial": initial, "target": target, "role": role}

    def _operator_disposition_binding(self, handle, request, workspace,
                                      authorization_constraint=None, *, for_lookup=False):
        """Authenticate and bind one opaque retain request to current state."""
        installed = self._operator_handles.get(handle)
        if installed is None or type(request) is not dict:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        admission._object(request, {"operation_key", "delivery_id", "task_id", "reason"})
        if (type(request["operation_key"]) is not str
                or re.fullmatch(r"[a-f0-9]{32}", request["operation_key"]) is None
                or request["operation_key"] == "0" * 32
                or type(request["delivery_id"]) is not str
                or type(request["task_id"]) is not str
                or request["reason"] not in admission.OPERATOR_DISPOSITION_REASONS):
            raise WorkspaceRuntimeError("operator_disposition_request_invalid")
        if authorization_constraint is not None and (
                not callable(authorization_constraint) or authorization_constraint() is not True):
            raise WorkspaceRuntimeError("operator_authorization_constraint_refused")
        targets = installed.get("targets", {})
        allowed = targets.get(request["delivery_id"])
        if not allowed or "retain_held_context" not in allowed:
            raise WorkspaceRuntimeError("operator_request_scope_conflict")
        delivery = workspace.get("deliveries", {}).get(request["delivery_id"])
        if (not isinstance(delivery, dict)
                or delivery.get("state") != "held_for_recovery"
                and not (for_lookup and delivery.get("state") == "dead_lettered")):
            raise WorkspaceRuntimeError("operator_disposition_state_refused")
        if not self._linked_delivery_scope_valid(workspace, delivery):
            raise WorkspaceRuntimeError("operator_disposition_state_refused")
        message = workspace.get("messages", {}).get(delivery.get("message_id"))
        if not isinstance(message, dict) or message.get("destination_task_id") != request["task_id"]:
            raise WorkspaceRuntimeError("operator_request_scope_conflict")
        if any(delivery.get(key) != workspace.get(key) or message.get(key) != workspace.get(key)
               for key in ("workspace_id", "run_id")):
            raise WorkspaceRuntimeError("operator_request_scope_conflict")
        if installed["authorize"](
                {"schema": "summon.workspace.operator-disposition-request/v1",
                 "action": "retain_held_context",
                 **copy.deepcopy(request)}, copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        return installed, delivery, message

    def _operator_disposition_proof_refs(self, request, workspace):
        """Resolve uniquely matching typed proof refs; ignore unrelated history."""
        candidates = []
        for registered in workspace.get("evidence", {}).values():
            if (isinstance(registered, dict) and registered.get("category") == "event"
                    and registered.get("task_id") == request["task_id"]
                    and registered.get("delivery_id") == request["delivery_id"]
                    and "settlement_for" not in registered
                    and isinstance(registered.get("reference"), dict)):
                candidates.append(copy.deepcopy(registered))
        proofs, references = {}, {}
        for registered in candidates:
            try:
                raw = self._resolve_evidence(copy.deepcopy(registered))
                reference = registered["reference"]
                if (type(raw) is not bytes or not 1 <= len(raw) <= 4096
                        or hashlib.sha256(raw).hexdigest() != reference["sha256"]):
                    continue
                source = _decode_workspace_source(raw, reference)
                role = source.get("role") if isinstance(source, dict) else None
                if role not in {"request", "decision"}:
                    continue
                checked = admission.canonical_operator_disposition_proof(
                    source, role=role, operation_key=request["operation_key"],
                    delivery_id=request["delivery_id"], task_id=request["task_id"],
                    reason=request["reason"])
            except (WorkspaceRuntimeError, ContentError, ValueError, TypeError, KeyError,
                    admission.WorkspaceAdmissionError):
                continue
            if role in proofs:
                raise WorkspaceRuntimeError("operator_disposition_proof_unavailable")
            proofs[role] = checked
            references[role] = copy.deepcopy(registered["reference"])
        return proofs, references

    def _operator_disposition_request(self, handle, request, workspace,
                                      authorization_constraint=None, *, for_lookup=False):
        """Resolve one opaque retain request against the current workspace.

        The caller supplies no evidence references or private delivery IDs to
        the public surface.  They are selected only from the coherent current
        projection and must be exactly two ordinary, task/delivery-bound event
        references (request and decision).  Missing or ambiguous proof refuses
        closed instead of fabricating authority.
        """
        installed, delivery, message = self._operator_disposition_binding(
            handle, request, workspace, authorization_constraint, for_lookup=for_lookup)

        proofs, references = self._operator_disposition_proof_refs(request, workspace)
        if set(proofs) != {"request", "decision"}:
            raise WorkspaceRuntimeError("operator_disposition_proof_unavailable")
        event = admission.build_operator_disposition_event(
            workspace, operation_key=request["operation_key"],
            delivery_id=request["delivery_id"], task_id=request["task_id"],
            request_ref=references["request"], decision_ref=references["decision"], reason=request["reason"])
        raw = admission._canonical_event_bytes({
            "schema": "summon.workspace.operator-disposition-request/v1",
            **copy.deepcopy(request), "request_ref": references["request"],
            "decision_ref": references["decision"]})
        if len(raw) > 4096:
            raise WorkspaceRuntimeError("operator_disposition_request_limit")
        return {"request": copy.deepcopy(request), "event": event,
                "request_ref": references["request"], "decision_ref": references["decision"],
                "request_sha256": hashlib.sha256(raw).hexdigest()}

    def provision_operator_disposition_proofs(self, handle, request, *,
                                               authorization_constraint=None):
        """Owner-only mint/register path for a new retain operation.

        The public adapter never accepts proof bytes.  When an authorized
        current request has no matching typed sources, this method mints the
        two bounded private sources and registers them through the normal
        owner writer.  Existing historical sources are retained and filtered
        by exact operation binding on every later lookup.
        """
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = state.get("workspace")
        if not isinstance(workspace, dict):
            raise WorkspaceRuntimeError("workspace_snapshot_unavailable")
        self._operator_disposition_binding(handle, request, workspace,
                                           authorization_constraint)
        proofs, references = self._operator_disposition_proof_refs(request, workspace)
        if set(proofs) == {"request", "decision"}:
            return {"status": "already_provisioned", "references": references}
        if proofs:
            # A partial pair can be completed only when the missing role is
            # unambiguous; duplicate/malformed current sources remain fail
            # closed in the resolver above.
            missing = {"request", "decision"} - set(proofs)
        else:
            missing = {"request", "decision"}
        for role in ("request", "decision"):
            if role not in missing:
                continue
            source = {
                "schema": admission.OPERATOR_DISPOSITION_PROOF_SCHEMA,
                "role": role,
                "operation_key": request["operation_key"],
                "action": admission.OPERATOR_DISPOSITION_ACTION,
                "delivery_id": request["delivery_id"],
                "task_id": request["task_id"],
                "reason": request["reason"],
                "authority": "installed_operator_policy",
            }
            admission.canonical_operator_disposition_proof(
                source, role=role, operation_key=request["operation_key"],
                delivery_id=request["delivery_id"], task_id=request["task_id"],
                reason=request["reason"])
            raw = admission._canonical_event_bytes(source)
            digest = hashlib.sha256(raw).hexdigest()
            descriptor = ContentRef("blob-" + digest[:32], digest, len(raw))
            self._content_store().put(descriptor, raw, require_namespace_durable=False)
            reference = {"id": descriptor.reference + ".retain-" + request["operation_key"] + "-" + role,
                         "sha256": digest}
            event = {"event": "workspace_evidence_registered", "protocol": protocol.PROTOCOL,
                     "workspace_id": self.workspace_id,
                     "run_id": self._coordinator.run_id,
                     "operation_key": "operator-proof-" + request["operation_key"] + "-" + role,
                     "expected_revision": workspace["revision"],
                     "payload": {"reference": reference, "category": "event",
                                 "task_id": request["task_id"],
                                 "delivery_id": request["delivery_id"]}}
            self.record_event(event)
            workspace = self._coordinator._load()[0]["workspace"]
            references[role] = reference
        return {"status": "provisioned", "references": references}

    @staticmethod
    def _operator_disposition_observation(plan, workspace, records):
        operation_key = plan["request"]["operation_key"]
        stored = workspace.get("operations", {}).get(operation_key)
        if stored is None:
            return {"status": "not_observed", "request_sha256": plan["request_sha256"],
                    "durable_prefix_verified": True, "revision": workspace["revision"]}
        matches = [record.get("workspace_event") for record in records
                   if isinstance(record, dict) and record.get("event") == CONTAINER
                   and isinstance(record.get("workspace_event"), dict)
                   and record["workspace_event"].get("operation_key") == operation_key]
        if len(matches) != 1:
            raise WorkspaceRuntimeError("operator_disposition_conflict")
        try:
            observed = admission.canonical_operator_disposition_event(matches[0])
        except (ValueError, TypeError, KeyError):
            raise WorkspaceRuntimeError("operator_disposition_conflict") from None
        expected_payload = plan["event"]["payload"]
        if (observed["payload"] != expected_payload
                or projection._event(observed) != stored):
            raise WorkspaceRuntimeError("operator_disposition_conflict")
        return {"status": "recorded", "request_sha256": plan["request_sha256"],
                "durable_prefix_verified": True, "revision": workspace["revision"]}

    def describe_operator_dispositions(self, handle):
        """Return only retained-action targets from an installed command scope."""
        installed = self._operator_handles.get(handle)
        if installed is None:
            raise WorkspaceRuntimeError("installed_operator_authority_required")
        current, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        if installed["authorize"](None, copy.deepcopy(current["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        targets = []
        for delivery_id, actions in installed["targets"].items():
            if "retain_held_context" not in actions:
                continue
            delivery = current["workspace"].get("deliveries", {}).get(delivery_id)
            if not isinstance(delivery, dict) or delivery.get("state") != "held_for_recovery":
                continue
            message = current["workspace"].get("messages", {}).get(delivery.get("message_id"))
            if not isinstance(message, dict):
                continue
            targets.append({"delivery_id": delivery_id, "actions": ["retain_held_context"],
                            "task_id": message["destination_task_id"]})
        return {"workspace_id": self.workspace_id, "run_id": self._coordinator.run_id,
                "targets": targets}

    def reconcile_operator_disposition(self, handle, request, *, authorization_constraint=None):
        state, torn = self._coordinator._load()
        plan = self._operator_disposition_request(
            handle, request, state["workspace"], authorization_constraint, for_lookup=True)
        if torn:
            return {"status": "uncertain", "operation_key": request["operation_key"],
                    "request_sha256": plan["request_sha256"],
                    "durable_prefix_verified": False, "revision": state["workspace"]["revision"]}
        records, later_torn, _after = self._coordinator._read_records_snapshot()
        if later_torn:
            return {"status": "uncertain", "operation_key": request["operation_key"],
                    "request_sha256": plan["request_sha256"],
                    "durable_prefix_verified": False, "revision": state["workspace"]["revision"]}
        observed = self._operator_disposition_observation(plan, state["workspace"], records)
        return {"operation_key": request["operation_key"], **observed}

    def execute_operator_disposition(self, handle, request, *, authorization_constraint=None):
        try:
            observed = self.reconcile_operator_disposition(
                handle, request, authorization_constraint=authorization_constraint)
        except WorkspaceRuntimeError as error:
            if error.kind != "operator_disposition_proof_unavailable":
                raise
            observed = {"status": "not_observed", "operation_key": request["operation_key"]}
        if observed["status"] in {"recorded", "uncertain"}:
            return observed
        self.provision_operator_disposition_proofs(
            handle, request, authorization_constraint=authorization_constraint)
        observed = self.reconcile_operator_disposition(
            handle, request, authorization_constraint=authorization_constraint)
        if observed["status"] in {"recorded", "uncertain"}:
            return observed
        state, torn = self._coordinator._load()
        plan = self._operator_disposition_request(
            handle, request, state["workspace"], authorization_constraint)
        if torn:
            return {"status": "uncertain", "operation_key": request["operation_key"],
                    "request_sha256": plan["request_sha256"],
                    "durable_prefix_verified": False, "revision": state["workspace"]["revision"]}
        try:
            self.record_event(plan["event"])
        except (WorkspaceRuntimeError, SwarmConflictError):
            raise
        except (SwarmCoordinatorError, OSError, OwnershipLostError):
            return {"status": "uncertain", "operation_key": request["operation_key"],
                    "request_sha256": plan["request_sha256"],
                    "durable_prefix_verified": False, "revision": observed["revision"]}
        return self.reconcile_operator_disposition(
            handle, request, authorization_constraint=authorization_constraint)

    def _operator_observation(self, plan, workspace, events):
        """Exact retained event/proof matching, never target-state inference."""
        query = plan["query"]
        evidence = events.get(query["evidence_operation_key"])
        transition = events.get(query["transition_operation_key"])
        if evidence is not None:
            if (evidence["event"] != "workspace_evidence_registered" or evidence["payload"] != plan["proof"]
                    or workspace["evidence"].get(plan["proof"]["reference"]["id"]) != plan["proof"]):
                raise WorkspaceRuntimeError("operator_request_conflict")
        if transition is not None:
            delivery = copy.deepcopy(workspace["deliveries"][plan["proof"]["delivery_id"]])
            delivery.update(state=plan["target"], reason=plan["source"]["action"], ack_level=None)
            expected = {"delivery": delivery, "evidence": {plan["role"]: plan["proof"]["reference"]},
                        "supported_ack_levels": []}
            if (evidence is None or transition["event"] != "workspace_delivery_advanced"
                    or transition["payload"] != expected):
                raise WorkspaceRuntimeError("operator_request_conflict")
        return {"status": "recorded" if transition else "evidence_only" if evidence else "not_observed",
                "request_sha256": query["request_sha256"], "revision": workspace["revision"]}

    def reconcile_operator_command(self, handle, source_bytes, *, authorization_constraint=None):
        """Authenticate and synchronize the exact prefix before reporting success.

        Absence, evidence-only and uncertain results never authorize a fresh key.
        No content is exposed and no event is appended by this lookup.
        """
        hook = getattr(SwarmCoordinator, "reconcile_operator_command", None)
        if not callable(hook):
            raise WorkspaceRuntimeError("operator_reconciliation_unavailable")
        state, torn = self._coordinator._load()
        plan = self._operator_request(handle, source_bytes, state["workspace"], authorization_constraint)
        revision = state["workspace"]["revision"]
        uncertain = {"status": "uncertain", "request_sha256": None,
                     "durable_prefix_verified": False, "revision": revision}
        if torn:
            return uncertain
        # Verify private source before ownership. Any later registration must
        # still equal the exact verified source-bearing record under the owner.
        source_verified = False
        if plan["proof"]["reference"]["id"] in state["workspace"]["evidence"]:
            try:
                source_verified = self._content_store().read(plan["descriptor"]) == plan["decision"]
            except ContentError:
                return uncertain
        def authorize(current, workspace, request):
            current_plan = self._operator_request(handle, source_bytes, workspace, authorization_constraint)
            if current_plan["query"] != request or request != plan["query"]:
                raise WorkspaceRuntimeError("operator_request_conflict")
            return True
        def inspect(current, workspace, request, events):
            result = self._operator_observation(plan, workspace, events)
            if result["status"] != "not_observed" and not source_verified:
                raise WorkspaceRuntimeError("operator_source_not_verified")
            return result
        try:
            return hook(self._coordinator, plan["query"], authorize_command=authorize, inspect_command=inspect)
        except (SwarmConflictError, WorkspaceRuntimeError):
            raise
        except (SwarmCoordinatorError, OSError, OwnershipLostError):
            return uncertain

    def execute_operator_command(self, handle, source_bytes, *, authorization_constraint=None):
        """Explicit fixed local disposition; never task cancellation or approval."""
        observed = self.reconcile_operator_command(handle, source_bytes, authorization_constraint=authorization_constraint)
        if observed["status"] in {"recorded", "uncertain"}:
            return observed
        state, torn = self._coordinator._load()
        plan = self._operator_request(handle, source_bytes, state["workspace"], authorization_constraint)
        if torn:
            return {"status": "uncertain", "request_sha256": None, "durable_prefix_verified": False,
                    "revision": state["workspace"]["revision"]}
        if state["workspace"]["deliveries"][plan["proof"]["delivery_id"]]["state"] != plan["initial"]:
            raise WorkspaceRuntimeError("operator_command_state_refused")
        try:
            self._content_store().put(plan["descriptor"], plan["decision"], require_namespace_durable=False)
            for phase in ("evidence", "transition"):
                self._record_operator_phase(handle, source_bytes, plan, phase, authorization_constraint=authorization_constraint)
        except (WorkspaceRuntimeError, SwarmConflictError):
            raise
        except (SwarmCoordinatorError, OSError, ContentError, OwnershipLostError):
            return {"status": "uncertain", "request_sha256": None, "durable_prefix_verified": False,
                    "revision": observed["revision"]}
        return self.reconcile_operator_command(handle, source_bytes, authorization_constraint=authorization_constraint)

    def _record_operator_phase(self, handle, raw, plan, phase, *, authorization_constraint=None):
        # Actual existing owner/reserved writer only. No nested mutation, repair,
        # generic event callback or browser-controlled evidence/proof attributes.
        contract = self._contract()
        verified = self._content_store().read(plan["descriptor"])
        if verified != plan["decision"]:
            raise WorkspaceRuntimeError("operator_source_not_verified")
        with self._coordinator._mutation(repair=False) as (owner, state, _torn):
            workspace = state["workspace"]
            current_plan = self._operator_request(handle, raw, workspace, authorization_constraint)
            if current_plan != plan:
                raise WorkspaceRuntimeError("operator_request_conflict")
            records, torn, snapshot = self._coordinator._read_records_snapshot()
            if torn or self._coordinator._prefix_fingerprint(snapshot) != state["_prefix_fingerprint"]:
                raise WorkspaceRuntimeError("journal_snapshot_changed")
            keys = {plan["query"]["evidence_operation_key"], plan["query"]["transition_operation_key"]}
            events = {record["workspace_event"]["operation_key"]: record["workspace_event"] for record in records
                      if record.get("event") == CONTAINER and record["workspace_event"]["operation_key"] in keys}
            observation = self._operator_observation(plan, workspace, events)
            if observation["status"] == "recorded" or phase == "evidence" and observation["status"] == "evidence_only":
                return
            delivery = copy.deepcopy(workspace["deliveries"][plan["proof"]["delivery_id"]])
            if delivery["state"] != plan["initial"]:
                raise WorkspaceRuntimeError("operator_command_state_refused")
            if phase == "evidence":
                kind, payload = "workspace_evidence_registered", plan["proof"]
            else:
                if observation["status"] != "evidence_only":
                    raise WorkspaceRuntimeError("operator_decision_evidence_required")
                delivery.update(state=plan["target"], reason=plan["source"]["action"], ack_level=None)
                kind, payload = "workspace_delivery_advanced", {"delivery": delivery,
                    "evidence": {plan["role"]: plan["proof"]["reference"]}, "supported_ack_levels": []}
            event = {"event": kind, "protocol": protocol.PROTOCOL, "workspace_id": self.workspace_id,
                "run_id": self._coordinator.run_id, "operation_key": plan["query"][phase + "_operation_key"],
                "expected_revision": workspace["revision"], "payload": payload}
            now = int(self._coordinator.clock() * 1000)
            projection.apply_event(workspace, event, _task_view(state, run_id=self._coordinator.run_id, now_ms=now))
            wrapper = {"event": CONTAINER, "workspace_event": event, "observed_at_ms": now}
            if len(encode_journal_record({**wrapper, "generation": owner.generation}, timestamp=now / 1000)) > contract["max_event_bytes"]:
                raise WorkspaceRuntimeError("workspace_envelope_limit")
            def before_append(current_state):
                if self._operator_request(handle, raw, current_state["workspace"], authorization_constraint) != plan:
                    raise WorkspaceRuntimeError("operator_request_conflict")
                return True
            SwarmCoordinator._append_with_state(self._coordinator, owner, wrapper, state=state, before_append=before_append)

    def _supervisor_contract(self):
        self._contract()
        hook = getattr(SwarmCoordinator, "supervisor_inbox_contract", None)
        if not callable(hook):
            raise WorkspaceRuntimeError("supervisor_inbox_base_unavailable")
        expected = {"schema": "summon.workspace.supervisor-inbox-base/v1", "event_classes": sorted(SUPERVISOR_EVENTS),
            "atomic_message_queue": True, "control_reserve": True, "consumer_authentication": False,
            "max_endpoints": 1, "max_messages": 32, "max_deliveries": 128, "max_content_bytes": 4096}
        if hook() != expected:
            raise WorkspaceRuntimeError("supervisor_inbox_base_unavailable")
        if not all(callable(getattr(SwarmCoordinator, name, None)) for name in ("supervisor_inbox_command", "admit_supervisor_message")):
            raise WorkspaceRuntimeError("supervisor_inbox_base_unavailable")
        return expected

    def _receiver_facts(self, workspace, binding):
        reference = {"id": binding["receiver_grant_id"], "sha256": binding["receiver_grant_sha256"]}
        registered = projection._known_ref(workspace, reference, endpoint_id=binding["endpoint_id"])
        if registered["category"] != "grant":
            raise WorkspaceRuntimeError("receiver_grant_required")
        raw = self._resolve_evidence(copy.deepcopy(registered))
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise WorkspaceRuntimeError("receiver_grant_source_unverified")
        grant = transport._decode(raw)
        admission._object(grant, {"kind", "workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch",
                                  "lease_expires_at_ms", "permission", "revoked"})
        transport._integer(grant["epoch"])
        transport._integer(grant["lease_expires_at_ms"])
        if (grant["kind"] != "supervisor_receiver_grant" or grant["permission"] != "supervisor_context_receive"
                or grant["revoked"] is not False
                or any(grant[key] != binding[key] for key in ("workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch"))):
            raise WorkspaceRuntimeError("receiver_grant_scope_mismatch")
        return grant, registered

    def install_supervisor_consumer(self, consumer):
        """Install an actual fixed handle; names/grants cannot reattach a session."""
        self._supervisor_contract()
        if type(consumer) is not transport.OwnedSupervisorConsumer or id(consumer) in self._supervisor_consumers:
            raise WorkspaceRuntimeError("owned_supervisor_consumer_required")
        observed = consumer.observe_consumer()
        binding = observed["binding"]
        if binding["workspace_id"] != self.workspace_id or binding["run_id"] != self._coordinator.run_id:
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = state["workspace"]
        endpoint = workspace.get("supervisor_endpoints", {}).get(binding["endpoint_id"])
        if endpoint is None:
            raise WorkspaceRuntimeError("supervisor_endpoint_required")
        owner = {"instance_id": binding["owner_instance_id"], "epoch": binding["epoch"]}
        if endpoint.get("owner") == owner:
            raise WorkspaceRuntimeError("consumer_reattachment_not_supported")
        if len(self._supervisor_consumers) >= 2 or any(item["binding"] == binding for item in self._supervisor_consumers.values()):
            raise WorkspaceRuntimeError("consumer_registry_bound")
        grant, source = self._receiver_facts(workspace, binding)
        command = {"operation": "install_supervisor_consumer", "binding": binding}
        if self._authorize(command, copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        self._supervisor_consumers[id(consumer)] = {"consumer": consumer, "binding": binding, "grant": grant, "source": source,
                                                   "exposure_guard": threading.Lock()}

    def _installed_consumer(self, consumer):
        if type(consumer) is not transport.OwnedSupervisorConsumer:
            raise WorkspaceRuntimeError("owned_supervisor_consumer_required")
        registered = self._supervisor_consumers.get(id(consumer))
        observed = consumer.observe_consumer()
        if registered is None or registered["consumer"] is not consumer or registered["binding"] != observed["binding"]:
            raise WorkspaceRuntimeError("current_installed_consumer_required")
        return registered, observed

    def prepare_supervisor_offer(self, consumer, delivery_id):
        """Prepare private proof/wire bytes, granting no exposure or acknowledgement."""
        self._supervisor_contract()
        registered, observed = self._installed_consumer(consumer)
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = state["workspace"]
        delivery = workspace.get("inbox_deliveries", {}).get(delivery_id)
        if delivery is None or delivery["state"] != "queued" or len(self._supervisor_offers) >= 128:
            raise WorkspaceRuntimeError("queued_inbox_delivery_required")
        binding = observed["binding"]
        if delivery["recipient"] != {"kind": "supervisor_inbox", "endpoint_id": binding["endpoint_id"],
                                    "owner_instance_id": binding["owner_instance_id"], "epoch": binding["epoch"]}:
            raise WorkspaceRuntimeError("consumer_recipient_mismatch")
        message = workspace["messages"][delivery["message_id"]]
        content = message["content"]
        raw = self._content_store().read(ContentRef(content["ref"], content["sha256"], content["utf8_bytes"]))
        offer_id = "offer-" + secrets.token_hex(16)
        payload = {"offer_id": offer_id, "delivery_id": delivery_id, "message_id": delivery["message_id"],
            "content_sha256": content["sha256"], "content_utf8": raw.decode("utf-8"),
            "possible_duplicate": delivery["possible_duplicate"], "prior_offer_id": delivery["prior_offer_id"]}
        checked = consumer.check_offer(payload)
        proof = {"schema": "summon.workspace.supervisor-offer-observation/v1", "kind": "offer_intent",
            "consumer": binding, "offer_id": offer_id, "delivery_id": delivery_id, "message_id": delivery["message_id"],
            "content_sha256": content["sha256"], "content_utf8_bytes": checked["content_utf8_bytes"],
            "possible_duplicate": delivery["possible_duplicate"], "prior_offer_id": delivery["prior_offer_id"],
            "consumer_kind": observed["consumer_kind"], "qualification": observed["qualification"]}
        token = object()
        source = admission._canonical_event_bytes(proof)
        self._supervisor_offers[token] = {"consumer": consumer, "binding": binding, "payload": payload,
            "source_bytes": source, "offered": False, "exposure_attempted": False}
        return {"token": token, "offer_id": offer_id, "source_bytes": source, "exposure_authorized": False}

    def supervisor_receipt_source(self, consumer, token):
        self._installed_consumer(consumer)
        observed = consumer.observe_receipt(token)
        return admission._canonical_event_bytes({"schema": "summon.workspace.supervisor-receipt-observation/v1", **observed})

    def supervisor_inbox_command(self, command, *, consumer=None, evidence=None, offer_token=None, receipt_token=None):
        """Explicit host command through central reserve; never an event-append API."""
        self._supervisor_contract()
        command, evidence = copy.deepcopy(command), copy.deepcopy(evidence if evidence is not None else {})
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = state["workspace"]
        action = command.get("action")
        live_required = action in {"activate", "offer", "receipt", "link"}
        if live_required and consumer is None:
            raise WorkspaceRuntimeError("current_installed_consumer_required")
        registered = observed = grant = receiver_source = None
        if consumer is not None:
            registered, observed = self._installed_consumer(consumer)
            grant, receiver_source = self._receiver_facts(workspace, observed["binding"])
        authorization = {"operation": "supervisor_inbox_command", "command": command, "evidence": evidence,
                         "consumer_binding": observed["binding"] if observed else None}
        if self._authorize(copy.deepcopy(authorization), copy.deepcopy(workspace)) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        source_facts = []
        for role, reference in evidence.items():
            known = projection._known_ref(workspace, reference, endpoint_id=command["endpoint_id"])
            raw = self._resolve_evidence(copy.deepcopy(known))
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                raise WorkspaceRuntimeError("inbox_evidence_source_unverified")
            source_facts.append((role, known, raw))
        plan = self._supervisor_offers.get(offer_token)
        if action == "offer":
            if (plan is None or plan["consumer"] is not consumer or plan["payload"]["delivery_id"] != command.get("delivery_id")
                    or plan["binding"] != observed["binding"]):
                raise WorkspaceRuntimeError("issued_offer_required")
            expected = plan["source_bytes"]
            if not any(role == "offer_intent" and raw == expected for role, _known, raw in source_facts):
                raise WorkspaceRuntimeError("actual_offer_intent_required")
        receipt = None
        if action == "receipt":
            receipt = consumer.observe_receipt(receipt_token)
            expected = self.supervisor_receipt_source(consumer, receipt_token)
            if not any(role == "consumer_receipt" and raw == expected for role, _known, raw in source_facts):
                raise WorkspaceRuntimeError("actual_consumer_receipt_required")

        def resolve_consumer(current, current_workspace, requested):
            if requested != command or self._authorize(copy.deepcopy(authorization), copy.deepcopy(current_workspace)) is not True:
                raise WorkspaceRuntimeError("command_not_authorized")
            for _role, known, _raw in source_facts:
                if current_workspace["evidence"].get(known["reference"]["id"]) != known:
                    raise WorkspaceRuntimeError("inbox_source_changed")
            binding = None
            if consumer is not None:
                current_registration, current_observed = self._installed_consumer(consumer)
                if (current_registration is not registered or current_observed != observed
                        or current_workspace["evidence"].get(receiver_source["reference"]["id"]) != receiver_source):
                    raise WorkspaceRuntimeError("current_consumer_changed")
                binding = current_observed["binding"]
            if action in {"offer", "receipt"}:
                delivery = current_workspace["inbox_deliveries"].get(command["delivery_id"])
                if delivery is None:
                    raise WorkspaceRuntimeError("inbox_delivery_missing")
                message = current_workspace["messages"][delivery["message_id"]]
                if action == "offer":
                    if (plan["binding"] != binding or plan["payload"]["message_id"] != delivery["message_id"]
                            or plan["payload"]["content_sha256"] != message["content"]["sha256"]
                            or len(plan["payload"]["content_utf8"].encode("utf-8")) != message["content"]["utf8_bytes"]
                            or plan["payload"]["possible_duplicate"] != delivery["possible_duplicate"]
                            or plan["payload"]["prior_offer_id"] != delivery["prior_offer_id"]):
                        raise WorkspaceRuntimeError("prepared_offer_changed")
                else:
                    actual = consumer.observe_receipt(receipt_token)
                    offer = delivery["offer"]
                    if (actual != receipt or offer is None or actual["binding"] != offer["consumer"]
                            or any(actual["receipt"][key] != offer[key] for key in
                                   ("offer_id", "delivery_id", "message_id", "content_sha256", "content_utf8_bytes"))
                            or command["offer_id"] != offer["offer_id"]):
                        raise WorkspaceRuntimeError("current_offer_receipt_required")
            return {"consumer": binding, "evidence": evidence,
                    "receiver_grant_ref": receiver_source["reference"] if receiver_source else None,
                    "lease_expires_at_ms": grant["lease_expires_at_ms"] if grant else None,
                    "offer_id": plan["payload"]["offer_id"] if action == "offer" else None}

        result = SwarmCoordinator.supervisor_inbox_command(self._coordinator, command, resolve_consumer=resolve_consumer)
        if action == "offer":
            if result["offer_id"] != plan["payload"]["offer_id"]:
                raise WorkspaceRuntimeError("committed_offer_differs")
            plan["offered"] = True
        return result

    def expose_supervisor_offer(self, consumer, token, *, timeout=3.0):
        """Expose a locally prepared durable offer at most once in this host.

        Durable ``offered`` is the uncertainty boundary before physical exposure.
        This local guard prevents concurrent host writes; it does not make a
        journal fence and a pipe write one cross-process atomic operation. A
        later revocation/expiry cannot erase possible exposure or accept a stale
        receipt. No coordinator mutation is held during this bounded pipe I/O.
        """
        self._supervisor_contract()
        registered, _observed = self._installed_consumer(consumer)
        if not registered["exposure_guard"].acquire(blocking=False):
            raise WorkspaceRuntimeError("consumer_exposure_in_progress")
        try:
            return self._expose_supervisor_offer_locked(consumer, token, registered, timeout=timeout)
        finally:
            registered["exposure_guard"].release()

    def _expose_supervisor_offer_locked(self, consumer, token, registered, *, timeout):
        transport._timeout(timeout)
        plan = self._supervisor_offers.get(token)
        if plan is None or plan["consumer"] is not consumer or not plan["offered"] or plan["exposure_attempted"]:
            raise WorkspaceRuntimeError("durable_unexposed_local_offer_required")
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        current_registration, observed = self._installed_consumer(consumer)
        if current_registration is not registered:
            raise WorkspaceRuntimeError("current_consumer_changed")
        workspace = state["workspace"]
        binding = observed["binding"]
        endpoint = workspace["supervisor_endpoints"][binding["endpoint_id"]]
        delivery = workspace["inbox_deliveries"][plan["payload"]["delivery_id"]]
        if (endpoint["status"] != "active" or endpoint["owner"] != {"instance_id": binding["owner_instance_id"], "epoch": binding["epoch"]}
                or endpoint["receiver_grant_ref"] != registered["source"]["reference"]
                or endpoint["lease_expires_at_ms"] <= int(self._coordinator.clock() * 1000)
                or delivery["state"] != "offered" or delivery["offer"]["offer_id"] != plan["payload"]["offer_id"]
                or delivery["offer"]["consumer"] != binding):
            raise WorkspaceRuntimeError("current_durable_offer_required")
        # Consumer exposure is not an execution slot, but the complete offer
        # still consumes bounded control/journal budget.  Bind that decision to
        # the current journal prefix and persist it before touching the pipe.
        stream_ids = sorted({message.get("stream_id") for message in workspace.get("messages", {}).values()
                             if isinstance(message, dict) and isinstance(message.get("stream_id"), str)})
        policy = {
            "schema": budget.POLICY_SCHEMA, "rate_window_ms": 60_000,
            "rate_limit": 32, "rate_bytes_limit": 256 * 1024,
            "max_message_bytes": 4096, "journal_capacity_bytes": 8 * 1024 * 1024,
            "control_reserve_bytes": 4096, "recovery_reserve_bytes": 4096,
            "execution_slot_limit": 0,
            "stream_limits": {stream: {"message_limit": 32, "byte_limit": 256 * 1024}
                              for stream in stream_ids or ["supervisor-inbox"]},
            "oversize_head_action": "refuse",
        }
        offer_bytes = len(admission._canonical_event_bytes(plan["payload"])) + 16 * 1024
        request = {
            "schema": budget.REQUEST_SCHEMA,
            "selected_message_ids": [plan["payload"]["message_id"]],
            "journal_bytes": offer_bytes, "execution_slots_requested": 0,
            "kind": "message",
        }
        try:
            budget_receipt = self._coordinator.consume_supervisor_offer_budget(
                admission_id="supervisor-offer-budget-" + plan["payload"]["offer_id"],
                delivery_id=plan["payload"]["delivery_id"],
                offer_id=plan["payload"]["offer_id"], offer_payload=plan["payload"],
                binding=binding, policy=policy, request=request)
        except WorkspaceRuntimeError:
            raise
        except (SwarmBudgetRefusal, SwarmCoordinatorError, ValueError) as exc:
            reason = getattr(exc, "reason", "invalid")
            raise WorkspaceRuntimeError(
                "supervisor_offer_budget_refused_" + str(reason)) from None
        plan["budget_receipt"] = budget_receipt
        # Set before writing: partial/unknown write can never become a retry.
        plan["exposure_attempted"] = True
        return consumer.send_offer(copy.deepcopy(plan["payload"]), timeout=timeout)

    def _worker_send_contract(self):
        self._contract(execution=True)
        contract = SwarmCoordinator.workspace_worker_send_contract()
        expected = {"schema": "summon.workspace.worker-send-base/v1", "event_class": admission.ATOMIC_SEND_EVENT,
                    "atomic_message_queue": True, "derived_admission_evidence": True,
                    "authenticated_worker_ingress": False, "max_operations": 32, "max_content_bytes": 4096}
        if contract != expected:
            raise WorkspaceRuntimeError("worker_send_base_unavailable")
        return contract

    def read_send_admission_evidence(self, registered):
        """Resolve a derived admission source only from one verified journal prefix."""
        state, torn, before = self._coordinator._load_with_snapshot()
        records, record_torn, after = self._coordinator._read_records_snapshot()
        if torn or record_torn or before != after:
            raise WorkspaceRuntimeError("journal_snapshot_changed")
        reference = registered.get("reference", {})
        if state.get("workspace", {}).get("evidence", {}).get(reference.get("id")) != registered:
            raise WorkspaceRuntimeError("registered_admission_source_missing")
        for record in records:
            event = record.get("workspace_event") if record.get("event") == CONTAINER else None
            if isinstance(event, dict) and event.get("event") in {admission.ATOMIC_SEND_EVENT, "workspace_supervisor_message_sent", admission.OPERATOR_SEND_EVENT}:
                proof = (admission.operator_admission_evidence(event) if event["event"] == admission.OPERATOR_SEND_EVENT
                         else admission.send_admission_evidence(event))
                if proof == registered:
                    domain = (b"summon.workspace.operator-message-admission/v1\0" if event["event"] == admission.OPERATOR_SEND_EVENT
                              else b"summon.workspace.supervisor-message-admission/v1\0"
                              if event["event"] == "workspace_supervisor_message_sent"
                              else b"summon.workspace.message-admission/v1\0")
                    raw = domain + admission._canonical_event_bytes(event)
                    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                        raise WorkspaceRuntimeError("admission_source_mismatch")
                    return raw
        raise WorkspaceRuntimeError("admission_source_missing")

    def install_worker_ingress(self, worker, *, claim_id, send_scope_ref, resolve_grant):
        return self._install_ingress(worker, claim_id=claim_id, send_scope_ref=send_scope_ref, resolve_grant=resolve_grant,
                                     mode="worker")

    def install_supervisor_ingress(self, worker, *, claim_id, send_scope_ref, resolve_grant):
        self._supervisor_contract()
        return self._install_ingress(worker, claim_id=claim_id, send_scope_ref=send_scope_ref, resolve_grant=resolve_grant,
                                     mode="supervisor")

    def _install_ingress(self, worker, *, claim_id, send_scope_ref, resolve_grant, mode):
        """Install one explicitly authorized route for an actual owned channel.

        The registered send-scope source contains the exact scope fields except
        its own reference (which cannot hash itself). Source and destination
        execution grants stay separate. This live registry is lost on restart.
        """
        self._worker_send_contract()
        if (type(worker) is not transport.OwnedFakeWorker or not callable(resolve_grant)
                or worker._closed or worker.channel.revoked or id(worker) in self._worker_ingress
                or len(self._worker_ingress) >= 16):
            raise WorkspaceRuntimeError("owned_ingress_installation_refused")
        _id(claim_id)
        binding = copy.deepcopy(worker.channel.binding)
        if binding["workspace_id"] != self.workspace_id or binding["run_id"] != self._coordinator.run_id:
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        claim = state["claims"].get(claim_id)
        issued = state["workers"].get(claim["worker_id"]) if claim is not None else None
        if (claim is None or claim["task_id"] != binding["task_id"] or issued is None
                or issued["worker_instance_id"] != binding["instance_id"]):
            raise WorkspaceRuntimeError("worker_send_claim_binding")
        command = {"operation": "install_supervisor_ingress" if mode == "supervisor" else "install_worker_ingress", "binding": binding, "claim_id": claim_id,
                   "send_scope_ref": copy.deepcopy(send_scope_ref)}
        if self._authorize(command, copy.deepcopy(state["workspace"])) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        registration = {"worker": worker, "binding": binding, "claim_id": claim_id,
                        "send_scope_ref": copy.deepcopy(send_scope_ref), "resolve_grant": resolve_grant, "mode": mode}
        scope, sources = self._ingress_facts(registration, state)  # Source-backed installation, no payload write.
        registration.update(scope=scope, sources=sources)
        worker.install_send_handler(self._receive_worker_send)
        self._worker_ingress[id(worker)] = registration

    def revoke_worker_ingress(self, worker):
        """Trusted supervisor cancellation revokes and closes the actual channel."""
        registration = self._worker_ingress.pop(id(worker), None)
        if registration is None or registration["worker"] is not worker:
            raise WorkspaceRuntimeError("owned_ingress_not_installed")
        worker.close()

    def _ingress_facts(self, registration, state):
        """Read exact private grant sources outside the coordinator mutation."""
        workspace, binding = state["workspace"], registration["binding"]
        scope_ref = registration["send_scope_ref"]
        registered = projection._known_ref(workspace, scope_ref, binding["task_id"])
        if registered["category"] != "grant":
            raise WorkspaceRuntimeError("send_scope_not_grant")
        raw = self._resolve_evidence(copy.deepcopy(registered))
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != scope_ref["sha256"]:
            raise WorkspaceRuntimeError("send_scope_source_unverified")
        scope = transport._decode(raw)
        supervisor = registration["mode"] == "supervisor"
        common = {"revision", "expires_at_ms", "revoked", "operation", "goal_id", "source_task_id", "sender", "channel_grant_ref", "recipient"}
        admission._object(scope, common | ({"receiver_grant_ref"} if supervisor else {"destination_task_id", "delivery_grant_ref"}))
        scope["reference"] = copy.deepcopy(scope_ref)
        protocol._worker(scope["sender"])
        if supervisor:
            protocol.supervisor_recipient(scope["recipient"])
        else:
            protocol._worker(scope["recipient"])
        transport._integer(scope["revision"])
        transport._integer(scope["expires_at_ms"])
        if (scope["revoked"] is not False or scope["operation"] != "message.send" or scope["source_task_id"] != binding["task_id"]
                or scope["sender"] != {"instance_id": binding["instance_id"], "epoch": binding["epoch"]}
                or scope["goal_id"] != workspace["goal"]["goal_id"]
                or scope["channel_grant_ref"] != {"id": binding["grant_id"], "sha256": binding["grant_sha256"]}):
            raise WorkspaceRuntimeError("send_scope_binding_mismatch")
        facts = [copy.deepcopy(registered)]
        execution_grants = [(scope["channel_grant_ref"], binding["task_id"], scope["sender"])]
        if supervisor:
            recipient_binding = {"workspace_id": binding["workspace_id"], "run_id": binding["run_id"],
                **{key: scope["recipient"][key] for key in ("endpoint_id", "owner_instance_id", "epoch")},
                "receiver_grant_id": scope["receiver_grant_ref"]["id"], "receiver_grant_sha256": scope["receiver_grant_ref"]["sha256"]}
            _receiver, receiver_source = self._receiver_facts(workspace, recipient_binding)
            facts.append(receiver_source)
        else:
            execution_grants.append((scope["delivery_grant_ref"], scope["destination_task_id"], scope["recipient"]))
        for reference, task, recipient in execution_grants:
            known = projection._known_ref(workspace, reference, task)
            if known["category"] != "grant":
                raise WorkspaceRuntimeError("execution_grant_missing")
            raw = self._resolve_evidence(copy.deepcopy(known))
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                raise WorkspaceRuntimeError("execution_grant_source_unverified")
            resolved = registration["resolve_grant"](copy.deepcopy(state), copy.deepcopy(workspace), copy.deepcopy(reference))
            expected = {"grant_ref": reference, "task_id": task, "goal_revision": workspace["goal"]["revision"],
                        "recipient": recipient, "revoked": False}
            if (type(resolved) is not dict or type(resolved.get("goal_revision")) is not int
                    or resolved != expected):
                raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
            facts.append(copy.deepcopy(known))
        return scope, facts

    def _receive_worker_send(self, worker, token):
        """Called only by the installed pipe's sole reader for an actual frame."""
        registration = self._worker_ingress.get(id(worker))
        try:
            self._worker_send_contract()
            if registration is None or registration["worker"] is not worker:
                raise WorkspaceRuntimeError("owned_ingress_not_installed")
            if registration["mode"] == "supervisor":
                self._supervisor_contract()
            observed = worker.observed_send(token)
            state, torn = self._coordinator._load()
            if torn:
                raise WorkspaceRuntimeError("journal_recovery_required")
            operation, request_digest = admission.send_request_identity(observed["binding"], observed["request"])
            previous = state["workspace"].get("send_operations", {}).get(operation)
            if previous is not None:
                if previous["request_sha256"] != request_digest:
                    raise WorkspaceRuntimeError("send_operation_conflict")
                # This is a historical response, not fresh execution authority.
                scope, sources = copy.deepcopy(registration["scope"]), copy.deepcopy(registration["sources"])
            else:
                scope, sources = self._ingress_facts(registration, state)
        except (ValueError, RuntimeError, OSError):
            return {"outcome": "refused", "reason": "not_authorized", "result": None}

        def resolve_source(current, workspace, request):
            # No content/pipe reads, process launches or nested owner locks here.
            fresh = worker.observed_send(token)
            if (self._worker_ingress.get(id(worker)) is not registration or fresh != observed
                    or fresh["binding"] != registration["binding"] or request != fresh["request"]):
                raise WorkspaceRuntimeError("observed_principal_changed")
            for source in sources:
                if workspace["evidence"].get(source["reference"]["id"]) != source:
                    raise WorkspaceRuntimeError("registered_source_changed")
            claim = current["claims"].get(registration["claim_id"])
            work = fresh["work_request"]
            identity = fresh["context_identity"]
            if (claim is None or claim["claim_id"] != work["attempt_id"]
                    or claim["task_id"] != fresh["binding"]["task_id"]
                    or identity is None or identity != {
                        "task_id": claim["task_id"], "claim_id": claim["claim_id"], "attempt": claim["attempt"],
                        "lease_generation": claim["lease_generation"], "request_sha256": claim["request_sha256"]}):
                raise WorkspaceRuntimeError("worker_send_claim_binding")
            admissions = [item for item in current.get("admissions", {}).values()
                          if item["workspace_event"]["payload"]["claim"]["claim_id"] == claim["claim_id"]]
            if (len(admissions) != 1 or admissions[0]["workspace_event"]["payload"]["selection"]["context_sha256"] != work["context_sha256"]):
                raise WorkspaceRuntimeError("worker_send_selection_binding")
            return {"binding": fresh["binding"], "claim": copy.deepcopy(claim), "send_scope": copy.deepcopy(scope)}

        def publish_content(request, content_id):
            raw = request["content"].encode("utf-8")
            descriptor = ContentRef(content_id, hashlib.sha256(raw).hexdigest(), len(raw))
            self._content_store().put(descriptor, raw, require_namespace_durable=False)
            return {"ref": descriptor.reference, "sha256": descriptor.sha256, "utf8_bytes": descriptor.payload_bytes}

        try:
            method = SwarmCoordinator.admit_supervisor_message if registration["mode"] == "supervisor" else SwarmCoordinator.admit_worker_message
            response = method(self._coordinator, observed["request"],
                resolve_source=resolve_source, prepare_content=publish_content)
        except (ValueError, RuntimeError, OSError):
            # Once the base is entered, a failure may follow publication/append.
            # Preserve uncertainty; never translate an unknown commit into no-op.
            return {"outcome": "indeterminate", "reason": "commit_uncertain", "result": None}
        return {"outcome": "queued", "reason": None, "result": response}

    @classmethod
    def new(cls, runs_root, run_id, workspace_id, *, authorize, resolve_evidence, clock=None):
        """Provision private layout only, before init; grant no execution.

        Existing addresses are never adopted. Workspace preparation still needs
        the R13 contract and a separately authorized goal plan.
        """
        _id(workspace_id)
        if not callable(authorize) or not callable(resolve_evidence):
            raise WorkspaceRuntimeError("trusted_integration_required")
        root = os.fspath(runs_root)
        if os.path.lexists(root):
            # Never normalize an arbitrary existing root's ACL. Reuse only an
            # already-private empty/dedicated workspace namespace, not rooms,
            # council, deliberation or ordinary swarm directories.
            try:
                _reject_reparse_ancestors(root)
                _verify_private(root, directory=True)
                names = os.listdir(root)
                if len(names) > 1024:
                    raise WorkspaceRuntimeError("workspace_namespace_limit")
                for name in names:
                    resolve_workspace_layout(root, name)
            except (OSError, ValueError):
                raise WorkspaceRuntimeError("dedicated_private_namespace_required") from None
        else:
            _secure_private_root(root)
        create_workspace_layout(runs_root, run_id)
        return cls(runs_root, run_id, workspace_id, authorize=authorize,
                   resolve_evidence=resolve_evidence, clock=clock)

    def _provision_content_after_create(self):
        # Called after authorized base preparation, never before create. This
        # helper does not bypass workspace event admission or launch anything.
        with self._coordinator._mutation():
            _secure_private_root(self._content_root)
            content = ContentStore(self._content_root)
            content.provision()
            self._content = content

    def _content_store(self):
        if self._content is None:
            # Read path does not provision/repair missing content directories.
            self._content = ContentStore(self._content_root)
        return self._content

    def _contract(self, *, execution=False):
        # Consult BASE support, never a subclass-installed feature flag.
        hook = getattr(SwarmCoordinator, "workspace_admission_contract", None)
        if not callable(hook):
            raise WorkspaceRuntimeError("workspace_admission_unqualified")
        value = hook()
        required = {"schema", "protocol", "event_container", "event_classes",
                    "reconciliation", "control_reserve", "max_event_bytes",
                    "atomic_claim_selection", "budget_preflight"}
        budget = value.get("budget_preflight") if isinstance(value, dict) else None
        if (type(value) is not dict or not required.issubset(set(value))
                or value["schema"] != ADMISSION_SCHEMA or value["protocol"] != protocol.PROTOCOL
                or value["event_container"] != CONTAINER or type(value["event_classes"]) is not list
                or any(type(kind) is not str for kind in value["event_classes"])
                or set(value["event_classes"]) != EVENT_CLASSES
                or value["reconciliation"] is not True or value["control_reserve"] is not True
                or type(value["max_event_bytes"]) is not int
                or not 1 <= value["max_event_bytes"] <= protocol.MAX_INT
                or type(value["atomic_claim_selection"]) is not bool
                or type(budget) is not dict
                or set(budget) != {"policy_schema", "snapshot_schema", "request_schema",
                                   "result_schema", "authoritative_inputs_required",
                                   "durable_execution_authorization"}
                or budget["policy_schema"] != "summon.workspace.admission-policy/v1"
                or budget["snapshot_schema"] != "summon.workspace.admission-snapshot/v1"
                or budget["request_schema"] != "summon.workspace.admission-request/v1"
                or budget["result_schema"] != "summon.workspace.admission-result/v1"
                or budget["authoritative_inputs_required"] is not True
                or budget["durable_execution_authorization"] is not False):
            raise WorkspaceRuntimeError("workspace_admission_unqualified")
        if execution and value["atomic_claim_selection"] is not True:
            raise WorkspaceRuntimeError("atomic_turn_admission_unqualified")
        return value

    def inspect(self):
        """Read-only, source-backed projection; never acquire or repair."""
        try:
            state, torn = self._coordinator._load()
        except SwarmNotFoundError:
            return {"status": "unprepared", "workspace_id": self.workspace_id}
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        workspace = state.get("workspace")
        if workspace is None:
            return {"status": "workspace_unprepared", "workspace_id": self.workspace_id}
        if workspace["workspace_id"] != self.workspace_id:
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        view = _task_view(state, run_id=self._coordinator.run_id,
                          now_ms=int(self._coordinator.clock() * 1000))
        return {"status": "inspected", "projection": projection.project(workspace, view),
                "execution_qualification": "requires_base_admission"}

    def prepare(self, plan, *, project_root_sha256, roster_definition_sha256, max_attempts=1):
        self._contract()  # Missing R13 support refuses before ownership/files.
        if (type(plan) is not dict or plan.get("status") != "proposed"
                or plan.get("execution_authorized") is not False):
            raise WorkspaceRuntimeError("invalid_goal_plan")
        # Revalidate the complete plan instead of accepting edited proposal flags.
        events = plan.get("events")
        if type(events) is not list or not 3 <= len(events) <= 18:
            raise WorkspaceRuntimeError("invalid_goal_plan")
        check = goal_plan(events[1]["payload"]["goal"],
                          [event["payload"]["lane"] for event in events[2:]],
                          operation_prefix=events[0]["operation_key"].rsplit("-", 1)[0],
                          economics=plan.get("economics"))
        if check != plan:
            raise WorkspaceRuntimeError("invalid_goal_plan")
        if (type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS
                or any(event["payload"]["lane"]["budget"]["max_attempts"] < max_attempts for event in events[2:])):
            raise WorkspaceRuntimeError("prepared_attempt_budget_invalid")
        if any(event["workspace_id"] != self.workspace_id
               or event["run_id"] != self._coordinator.run_id for event in events):
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        authorization = {"operation": "prepare", "plan": copy.deepcopy(plan)}
        if max_attempts != 1:
            authorization["max_attempts"] = max_attempts
        if self._authorize(authorization, None) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        _WorkspaceCoordinator.create(self._coordinator.runs_root, self._coordinator.run_id,
                                     project_root_sha256=project_root_sha256,
                                     roster_definition_sha256=roster_definition_sha256,
                                     tasks=plan["tasks"], max_attempts=max_attempts,
                                     economics=plan.get("economics"), clock=self._coordinator.clock)
        self._provision_content_after_create()
        for event in events:
            self.record_event(event)
        return self.inspect()

    def _verify_sources(self, event):
        payload = event["payload"]
        if event["event"] == "workspace_evidence_registered":
            raw = self._resolve_evidence(copy.deepcopy(payload))
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != payload["reference"]["sha256"]:
                raise WorkspaceRuntimeError("evidence_source_unverified")
        if event["event"] == "workspace_message_admitted":
            message = payload["message"]
            descriptor = message["content"]
            raw = self._content_store().read(ContentRef(descriptor["ref"], descriptor["sha256"], descriptor["utf8_bytes"]))
            protocol.validate_content(message, raw)

    def record_transport_observation(self, event, *, kind: str, evidence: dict,
                                     resolve_grant=None):
        """Admit one authenticated adapter observation through the runtime.

        The first supported observation is ``native_turn_collision``.  It is
        deliberately provider-free: the adapter proves that two native turns
        collided, while the workspace journal records a held delivery with
        contact/spend/cleanup uncertainty.  No new attempt or submission is
        created, and all durable admission remains in ``record_event``.
        """
        self._contract()
        checked = _collision_observation_event(event, kind=kind, evidence=evidence)
        return self.record_event(checked, resolve_grant=resolve_grant)

    def record_event(self, event, *, resolve_grant=None, fixed_worker=None):
        """Admit one authorized event solely through the base reserved seam.

        Control-capacity classification and reconciliation belong to R13. The
        callback must reject worker attempts to register arbitrary grant evidence.
        """
        contract = self._contract()
        if type(event) is not dict or event.get("event") not in EVENT_CLASSES:
            raise WorkspaceRuntimeError("unsupported_workspace_event")
        if event.get("workspace_id") != self.workspace_id or event.get("run_id") != self._coordinator.run_id:
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        target = event.get("payload", {}).get("delivery", {}).get("state")
        if event["event"] == "workspace_delivery_advanced" and target in {"included_in_attempt", "submission_started"}:
            self._contract(execution=True)
            if target == "included_in_attempt":
                raise WorkspaceRuntimeError("atomic_turn_admission_required")
            if not callable(resolve_grant):
                raise WorkspaceRuntimeError("trusted_grant_resolver_required")
        with self._coordinator._mutation() as (owner, state, _torn):
            now = int(self._coordinator.clock() * 1000)
            current = state.get("workspace")
            following = projection.apply_event(current, event,
                _task_view(state, run_id=self._coordinator.run_id, now_ms=now))
            if self._authorize(copy.deepcopy(event), copy.deepcopy(current)) is not True:
                raise WorkspaceRuntimeError("command_not_authorized")
            if following == current:
                return {"status": "already_recorded", "revision": current["revision"]}
            if event["event"] == "workspace_effects_resolved":
                payload = event["payload"]
                before = current["deliveries"][payload["delivery"]["delivery_id"]]
                actual = fixed_effect_sources(fixed_worker, before)
                for role, source in zip(("fixed_execution_scope", "owned_child_cleanup"), actual):
                    reference = payload["evidence"][role]
                    registered = current["evidence"].get(reference["id"])
                    if registered is None or registered["reference"] != reference:
                        raise WorkspaceRuntimeError("unregistered_effect_source")
                    raw = self._resolve_evidence(copy.deepcopy(registered))
                    expected_raw = json.dumps(source, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                              separators=(",", ":")).encode("utf-8")
                    if type(raw) is not bytes or raw != expected_raw or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                        raise WorkspaceRuntimeError("effect_source_not_actual_observation")
            if event["event"] == "workspace_delivery_advanced" and target == "submission_started":
                delivery = event["payload"]["delivery"]
                selection = delivery["selection"]
                claim = state["claims"].get(selection["claim_id"])
                worker = state["workers"].get(claim["worker_id"]) if claim else None
                if (claim is None or claim["status"] != "active" or claim["cancel_requested"]
                        or claim["lease_expires_at_ms"] <= now or worker is None
                        or worker["worker_instance_id"] != delivery["recipient"]["instance_id"]
                        or any(claim[key] != selection[key] for key in ("task_id", "claim_id", "attempt", "request_sha256"))
                        or claim["lease_generation"] != selection["owner_generation"]):
                    raise WorkspaceRuntimeError("submission_claim_not_current")
                projection.require_no_unresolved_hold(current, selection["task_id"])
                reference = event["payload"]["evidence"]["current_grant"]
                grant = resolve_grant(copy.deepcopy(state), copy.deepcopy(current), copy.deepcopy(reference))
                expected = {"grant_ref": reference, "task_id": selection["task_id"],
                            "goal_revision": current["goal"]["revision"], "recipient": delivery["recipient"], "revoked": False}
                if grant != expected or type(grant.get("revoked")) is not bool:
                    raise WorkspaceRuntimeError("submission_grant_not_current")
                for value in event["payload"]["evidence"].values():
                    registered = current["evidence"].get(value.get("id")) if type(value) is dict else None
                    if registered is None or registered["reference"] != value:
                        raise WorkspaceRuntimeError("unregistered_turn_evidence")
                    self._verify_sources({"event": "workspace_evidence_registered", "payload": registered})
            self._verify_sources(event)
            wrapper = {"event": CONTAINER, "workspace_event": copy.deepcopy(event), "observed_at_ms": now}
            # Full-envelope preflight, including stamp/checksum/LF. The BASE
            # writer must additionally enforce its own exact frozen bytes;
            # nested protocol validity never guarantees this wrapper will fit.
            preflight = encode_journal_record({**wrapper, "generation": owner.generation}, timestamp=now / 1000)
            if len(preflight) > contract["max_event_bytes"]:
                raise WorkspaceRuntimeError("workspace_envelope_limit")
            # Use actual state from this same mutation/reconciled prefix. Never
            # call plain _append, creation-only _append_initial, or raw IO.
            SwarmCoordinator._append_with_state(self._coordinator, owner, wrapper, state=state)
            return {"status": "recorded", "revision": following["revision"]}

    def send_context(self, event, value):
        """Persist complete private content before its authorized message event.

        Process-restart preview only; Windows namespace qualification stays
        explicit. Failure may leave a bounded orphan, never an accepted message.
        """
        self._contract()
        if type(event) is not dict or event.get("event") != "workspace_message_admitted":
            raise WorkspaceRuntimeError("message_event_required")
        message = protocol.validate_record(event["payload"]["message"])
        raw = value.encode("utf-8") if type(value) is str else value
        protocol.validate_content(message, raw)
        if self._authorize(copy.deepcopy(event), None) is not True:
            raise WorkspaceRuntimeError("command_not_authorized")
        descriptor = message["content"]
        store = self._content_store()
        store.provision()
        receipt = store.put(ContentRef(descriptor["ref"], descriptor["sha256"], descriptor["utf8_bytes"]),
                                    raw, require_namespace_durable=False)
        result = self.record_event(event)
        return {**result, "content_durability": {"file": receipt.file_durability,
                                                "namespace": receipt.namespace_durability}}

    def inspect_context(self, content: ContentRef):
        """Private inspection only; failure is a non-durable hold observation.

        Preserve the exact diagnostic category under a typed content hold. The
        protocol's observation-backed transition and base reserve are separate;
        this inspection never claims the observation was journaled.
        """
        try:
            return {"status": "verified", "bytes": self._content_store().read(content)}
        except ContentError as exc:
            reason = ("content_missing" if exc.kind == "content_missing" else
                      "content_mismatch" if exc.kind in {"content_mismatch", "invalid_utf8"} else
                      "content_unavailable")
            return {"status": "held", "reason": reason, "diagnostic_kind": exc.kind, "durable": False}

    def _turn_grant_resolver(self, worker_id, admission_event, resolve_grant):
        """Build checks for the BASE's same-state resolver boundary, no locks.

        The deterministic typed envelope preserves the selected whole messages
        in canonical order; its exact bytes must match the frozen context hash.
        The callback verifies grants; neither its prose nor source digests mint
        execution authority. Base canonical replay/admission validates claims,
        delivery lineage, ordering, registered proof categories and reserves.
        """
        import _workspace_admission as admission
        worker_id = _id(worker_id)
        if not callable(resolve_grant):
            raise WorkspaceRuntimeError("trusted_grant_resolver_required")
        event = admission._event_envelope(admission_event)
        if (event["event"] != admission.ATOMIC_TURN_EVENT
                or event["workspace_id"] != self.workspace_id
                or event["run_id"] != self._coordinator.run_id):
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        try:
            target = _id(event["payload"]["claim"]["task_id"])
            if event["payload"]["claim"]["worker_id"] != worker_id:
                raise WorkspaceRuntimeError("claim_worker_mismatch")
        except (KeyError, TypeError):
            raise WorkspaceRuntimeError("invalid_turn_request") from None

        def checked_resolver(coordinator_state, workspace, grant_ref):
            if (type(workspace) is not dict or coordinator_state.get("workspace") != workspace
                    or workspace.get("workspace_id") != self.workspace_id
                    or workspace.get("run_id") != self._coordinator.run_id
                    or event["payload"].get("grant_ref") != grant_ref):
                raise WorkspaceRuntimeError("workspace_scope_mismatch")
            command = {"operation": "admit_turn", "worker_id": worker_id,
                       "admission_event": copy.deepcopy(event), "context_format": "summon.workspace.context/v1"}
            if self._authorize(command, copy.deepcopy(workspace)) is not True:
                raise WorkspaceRuntimeError("command_not_authorized")
            # Same-mutation recheck, even after an earlier next-lane decision.
            projection.require_no_unresolved_hold(workspace, target)
            payload = event["payload"]
            selection = payload.get("selection", {})
            raw = self._compile_selected_context(workspace, payload)
            if selection.get("context_sha256") != hashlib.sha256(raw).hexdigest():
                raise WorkspaceRuntimeError("selected_context_digest_mismatch")
            # Conservative complete frame budget for both current fixed request
            # kinds; no key, pipe, Channel or speculative authenticated receipt.
            binding = {"workspace_id": self.workspace_id, "run_id": self._coordinator.run_id,
                       "instance_id": payload["recipient"]["instance_id"], "epoch": payload["recipient"]["epoch"],
                       "task_id": target, "grant_id": grant_ref["id"], "grant_sha256": grant_ref["sha256"]}
            transport._json({"protocol": transport.PROTOCOL, "nonce": "0" * 64, "binding": binding,
                "direction": "supervisor", "sequence": transport.MAX_SEQUENCE, "kind": "fixture_work",
                "payload": {"message_id": "x" * 128, "delivery_id": "x" * 128,
                            "attempt_id": "x" * 128, "context": raw.decode("utf-8"),
                            "context_sha256": selection["context_sha256"], "operation": transport.CONTEXT_FIXTURE_OPERATION},
                "payload_sha256": "0" * 64, "mac": "0" * 64})
            evidence_by_delivery = payload.get("evidence_by_delivery")
            if (type(evidence_by_delivery) is not dict
                    or set(evidence_by_delivery) != set(payload["selected_delivery_ids"])):
                raise WorkspaceRuntimeError("delivery_evidence_coverage_mismatch")
            for delivery_id in payload["selected_delivery_ids"]:
                evidence = evidence_by_delivery[delivery_id]
                if type(evidence) is not dict or evidence.get("current_grant") != grant_ref:
                    raise WorkspaceRuntimeError("delivery_grant_proof_mismatch")
                required = {"current_grant", "recipient_owner_fence", "capacity_reservation",
                            "physical_attempt_reservation", "selection_record", "whole_message_fit"}
                if not required <= set(evidence) or set(evidence) - required - {"durable_launch_intent"}:
                    raise WorkspaceRuntimeError("incomplete_delivery_evidence")
                # Preserve existing per-delivery role/category/task binding;
                # a proof for another selected delivery is not interchangeable.
                projection._delivery_refs(workspace, evidence, target, delivery_id)
                for reference in evidence.values():
                    if type(reference) is not dict:
                        raise WorkspaceRuntimeError("unregistered_turn_evidence")
                    registered = workspace["evidence"].get(reference.get("id"))
                    if registered is None or registered["reference"] != reference:
                        raise WorkspaceRuntimeError("unregistered_turn_evidence")
                    self._verify_sources({"event": "workspace_evidence_registered", "payload": registered})
            return resolve_grant(copy.deepcopy(coordinator_state), copy.deepcopy(workspace), copy.deepcopy(grant_ref))

        return checked_resolver

    def _compile_selected_context(self, workspace, payload):
        selection = payload.get("selection", {})
        selected = selection.get("selected_message_ids")
        delivery_ids = payload.get("selected_delivery_ids")
        if (type(selected) is not list or not 1 <= len(selected) <= 8
                or type(delivery_ids) is not list or len(delivery_ids) != len(selected)
                or len(set(delivery_ids)) != len(delivery_ids)):
            raise WorkspaceRuntimeError("invalid_selected_context")
        deliveries = [workspace["deliveries"].get(_id(identifier)) for identifier in delivery_ids]
        entries = []
        for identifier in selected:
            message = workspace["messages"].get(_id(identifier))
            matched = [item for item in deliveries if item is not None and item["message_id"] == identifier]
            if (message is None or len(matched) != 1 or message["destination_task_id"] != selection.get("task_id")
                    or matched[0]["recipient"] != payload.get("recipient")):
                raise WorkspaceRuntimeError("selected_context_scope_mismatch")
            content = message["content"]
            raw = self._content_store().read(ContentRef(content["ref"], content["sha256"], content["utf8_bytes"]))
            protocol.validate_content(message, raw)
            entries.append({"message_id": identifier, "delivery_id": matched[0]["delivery_id"],
                            "content_sha256": content["sha256"], "content_utf8": raw.decode("utf-8")})
        return compile_turn_context(selection, entries)

    def read_admitted_context(self, admission_id):
        """Privately rebuild frozen context, including after a duplicate reply.

        Missing/corrupt content refuses; reading grants no execution/retry. This
        does not reconnect workers or certify present claim/grant liveness.
        """
        _id(admission_id)
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        item = state.get("admissions", {}).get(admission_id)
        if item is None:
            raise WorkspaceRuntimeError("admission_not_found")
        event = item["workspace_event"]
        if event["workspace_id"] != self.workspace_id or event["run_id"] != self._coordinator.run_id:
            raise WorkspaceRuntimeError("workspace_scope_mismatch")
        raw = self._compile_selected_context(state["workspace"], event["payload"])
        if hashlib.sha256(raw).hexdigest() != event["payload"]["selection"]["context_sha256"]:
            raise WorkspaceRuntimeError("selected_context_digest_mismatch")
        return {"bytes": raw, "sha256": hashlib.sha256(raw).hexdigest(), "execution_authorized": False}

    def _v2_economics_reservation(self, event, supplied):
        """Bind a v2 turn to the persisted lane policy and economics fence.

        The caller may provide an explicit reservation, but it cannot choose a
        different policy or capacity from the persisted plan.  When omitted,
        the host derives the deterministic reservation for this exact claim and
        selected context; the physical attempt identity is still recorded in
        the reservation and the later accounting record must bind to it.
        """
        state, torn = self._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        config = state.get("economics")
        if config is None:
            return supplied
        try:
            payload = event["payload"]
            selection = payload["selection"]
            workspace = state["workspace"]
            lane = workspace["lanes"][selection["task_id"]]
            policy = _context_policy.validate(lane["context_policy"])
            if policy.get("mode") != "off":
                raise WorkspaceRuntimeError("context_policy_not_supported")
            policy_sha = hashlib.sha256(admission._canonical_event_bytes(policy)).hexdigest()
            raw_context = self._compile_selected_context(workspace, payload)
            estimate = _submission_accounting.estimate_payload(
                [("context", raw_context.decode("utf-8"))],
                boundary="workspace-turn")
            material_sha = estimate["material_sha256"]
            selection_sha = hashlib.sha256(admission._canonical_event_bytes({
                "task_id": selection["task_id"], "attempt": selection["attempt"],
                "request_sha256": selection["request_sha256"],
                "context_sha256": selection["context_sha256"],
                "selected_message_ids": selection["selected_message_ids"],
            })).hexdigest()
            if supplied is None:
                claim_id = selection["claim_id"]
                attempt_id = hashlib.sha256(
                    ("summon-economics-attempt/v1:" + claim_id + ":" +
                     selection["context_sha256"]).encode("ascii")).hexdigest()[:32]
                supplied = {
                    "schema": admission.ECONOMICS_RESERVATION_SCHEMA,
                    "feature": admission.ECONOMICS_FEATURE,
                    "status": "reserved",
                    "max_records": config["max_records"],
                    "max_settlement_bytes": config["max_settlement_bytes"],
                    "claim_id": claim_id,
                    "selection_sha256": selection_sha,
                    "attempt_id": attempt_id,
                    "payload_sha256": selection["context_sha256"],
                    "material_sha256": material_sha,
                    "policy_sha256": policy_sha,
                }
            elif (supplied.get("max_records") != config["max_records"]
                  or supplied.get("max_settlement_bytes") != config["max_settlement_bytes"]
                  or supplied.get("policy_sha256") != policy_sha
                  or supplied.get("material_sha256") != material_sha):
                raise WorkspaceRuntimeError("economics_policy_binding_required")
            return admission.economics_reservation(
                supplied, claim_id=selection["claim_id"], selection=selection)
        except WorkspaceRuntimeError:
            raise
        except (KeyError, TypeError, ValueError, admission.WorkspaceAdmissionError):
            raise WorkspaceRuntimeError("economics_policy_binding_required") from None

    def admit_turn(self, worker_id, admission_event=None, *, resolve_grant=None,
                   lease_ms=30_000, message_id=None, budget_policy=None,
                   budget_request=None, economics_reservation=None):
        """Delegate to the real atomic base hook; never acquire a nested owner.

        The base owns mutation, lease time, prospective claim, canonical frozen
        selection, idempotent response and reserved append. Duplicate durable
        responses do not create another launch or rerun a fresh grant resolver.
        No worker starts and no context bytes are returned/submitted here.
        """
        self._contract(execution=True)
        # Keep the pre-R13 refusal surface callable by older adapters/tests
        # that supplied only the historical request object.  The contract gate
        # remains first, so an unqualified runtime refuses before inspecting
        # or authorizing any request; qualified callers must provide the full
        # typed event and resolver below.
        if admission_event is None or resolve_grant is None:
            raise WorkspaceRuntimeError("invalid_turn_request")
        event = copy.deepcopy(admission_event)
        economics_reservation = self._v2_economics_reservation(event, economics_reservation)
        if economics_reservation is not None:
            event["payload"] = copy.deepcopy(event.get("payload", {}))
            event["payload"]["economics_reservation"] = copy.deepcopy(economics_reservation)
        resolver = self._turn_grant_resolver(worker_id, event, resolve_grant)
        return SwarmCoordinator.admit_claim_selection(
            self._coordinator, worker_id, event, resolve_grant=resolver,
            lease_ms=lease_ms, message_id=message_id,
            budget_policy=budget_policy, budget_request=budget_request,
            economics_reservation=economics_reservation)

    def settle_turn_economics(self, admission_id, *, accounting_record,
                              submission_state, result_status,
                              result_sha256=None):
        """Durably settle one admitted v2 turn through the sole writer.

        Callers must provide the complete private submission-accounting record
        produced for the bound physical attempt.  A digest-only or otherwise
        unbound result cannot release the reservation; exact duplicate
        settlements are idempotent and changed outcomes conflict.
        """
        self._contract(execution=True)
        return self._coordinator.settle_workspace_turn_economics(
            admission_id, accounting_record=accounting_record,
            submission_state=submission_state, result_status=result_status,
            result_sha256=result_sha256)
