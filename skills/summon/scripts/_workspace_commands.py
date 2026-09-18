"""Bounded ordinary operator bindings and an explicit trusted runtime bridge.

Pure helpers grant no authority and perform no I/O. OperatorCommandAdapter uses
coherent reads and actual public runtime methods; its handle must already be
installed by the trusted host. Runtime independently rechecks authorization
under its public reconciliation/mutation seam. A digest or read bearer is never
permission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import copy
import hashlib
import hmac
import json
import re

import _workspace_admission as admission
import _workspace_protocol as protocol
from _workspace_view import ViewScope, _check_scope, _opaque

ACTIONS = {
    "cancel_queued_context": ("queued", "cancelled", "authorized_cancellation", "grant"),
    "dispose_held_context": ("held_for_recovery", "dead_lettered", "authenticated_disposition", "event"),
    # Retention is a separate versioned request contract.  It is present in
    # the installed authority scope, but the legacy /api/commands v1 binder
    # rejects it; OperatorDispositionAdapter owns the new route.
    "retain_held_context": ("held_for_recovery", "held_for_recovery", "operator_hold", "event"),
}
COMMAND_ACTIONS = frozenset({"cancel_queued_context", "dispose_held_context"})
DISPOSITION_ACTIONS = frozenset({"retain_held_context"})
COMMAND_POLICY_SCHEMA = "summon.workspace-command-policy/v1"
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")


class WorkspaceCommandError(ValueError):
    """Public-safe typed refusal; never include private command/source contents."""


@dataclass(frozen=True, repr=False)
class CommandScope:
    workspace_id: str
    run_id: str
    # Immutable finite host declaration: (raw delivery ID, tuple of action names).
    targets: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, repr=False)
class CommandBinding:
    operation_key: str
    action: str
    target_state: str
    request_sha256: str = field(repr=False)
    source_bytes: bytes = field(repr=False)
    delivery_id: str = field(repr=False)
    task_id: str = field(repr=False)
    proof_role: str
    proof_category: str


def _refuse(kind):
    raise WorkspaceCommandError(kind)


def _linked_delivery_scope_valid(workspace, delivery):
    """Keep linked command targets bound to their immutable parent lineage."""
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


def _scope(value, workspace):
    if (type(value) is not CommandScope or value.workspace_id != workspace.get("workspace_id")
            or value.run_id != workspace.get("run_id") or type(value.targets) is not tuple
            or not 1 <= len(value.targets) <= 128):
        _refuse("command_scope_required")
    result = {}
    for item in value.targets:
        if (type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                or type(item[1]) is not tuple or not 1 <= len(item[1]) <= len(ACTIONS)
                or any(type(action) is not str or action not in ACTIONS for action in item[1])
                or len(set(item[1])) != len(item[1]) or item[0] in result):
            _refuse("command_scope_required")
        protocol._id(item[0])
        result[item[0]] = item[1]
    return result


def bind_command(workspace, *, view_scope, command_scope, body, for_lookup=False):
    """Bind one request to retained ordinary delivery facts; never authorize it.

    ``workspace`` must come from the host's coherent validated current prefix.
    ``for_lookup`` is a host-only switch, never a browser body field. It permits
    reconstructing the same immutable request after its terminal transition.
    """
    try:
        if type(workspace) is not dict or type(view_scope) is not ViewScope or type(for_lookup) is not bool:
            _refuse("invalid_command_context")
        _check_scope(view_scope, workspace)
        if view_scope.audience != "operator":
            _refuse("command_scope_required")
        allowed = _scope(command_scope, workspace)
        if (type(body) is not dict or set(body) != {"operation_key", "action", "target"}
                or type(body["operation_key"]) is not str or not _OPERATION.fullmatch(body["operation_key"])
                or type(body["action"]) is not str or body["action"] not in COMMAND_ACTIONS
                or type(body["target"]) is not str or not re.fullmatch(r"delivery_[A-Za-z0-9_-]{32}", body["target"])):
            _refuse("invalid_command_request")
        deliveries = workspace.get("deliveries")
        if type(deliveries) is not dict or len(deliveries) > 128:
            _refuse("invalid_command_context")
        matches = [key for key in allowed if key in deliveries
                   and hmac.compare_digest(_opaque(view_scope, "delivery", key), body["target"])]
        if len(matches) != 1 or body["action"] not in allowed[matches[0]]:
            _refuse("command_target_refused")
        delivery = protocol.validate_record(deliveries[matches[0]])
        message = protocol.validate_record(workspace["messages"][delivery["message_id"]])
        if not _linked_delivery_scope_valid(workspace, delivery):
            _refuse("linked_command_unavailable")
        if "parent_delivery_id" not in delivery:
            protocol.validate_delivery_binding(message, delivery)
        if (delivery["kind"] != "delivery" or message["kind"] not in {"message", "operator_message"}
                or delivery["delivery_id"] != matches[0]
                or any(delivery[key] != workspace[key] or message[key] != workspace[key]
                       for key in ("workspace_id", "run_id"))
                or message["destination_task_id"] not in workspace["lanes"]):
            _refuse("invalid_command_context")
        initial, target, role, category = ACTIONS[body["action"]]
        if delivery["state"] not in ({initial, target} if for_lookup else {initial}):
            _refuse("command_state_refused")
        immutable = {key: value for key, value in delivery.items()
                     if key not in {"state", "reason", "ack_level", "certainty"}}
        source = {"schema": "summon.workspace.operator-command-request/v1",
                  "operation_key": body["operation_key"], "action": body["action"],
                  "delivery": immutable, "task_id": message["destination_task_id"],
                  "content": message["content"]}
        raw = json.dumps(source, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > 4096:
            _refuse("command_binding_limit")
        return CommandBinding(body["operation_key"], body["action"], target, hashlib.sha256(raw).hexdigest(),
                              raw, delivery["delivery_id"], message["destination_task_id"], role, category)
    except WorkspaceCommandError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WorkspaceCommandError("invalid_command_context") from None


def present_lookup(binding, observation):
    """Redact a trusted runtime observation without upgrading uncertain reads.

    This establishes no authentication or durability itself. The public runtime
    reconciliation method must produce authoritative observations.
    """
    if (type(binding) is not CommandBinding or type(observation) is not dict
            or set(observation) != {"status", "request_sha256", "durable_prefix_verified", "revision"}
            or type(observation["status"]) is not str
            or observation["status"] not in {"not_observed", "evidence_only", "recorded", "uncertain"}
            or type(observation["durable_prefix_verified"]) is not bool
            or type(observation["revision"]) is not int or not 0 <= observation["revision"] <= protocol.MAX_INT):
        _refuse("invalid_command_observation")
    fingerprint = observation["request_sha256"]
    if fingerprint is not None and (type(fingerprint) is not str or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
                                   or not hmac.compare_digest(fingerprint, binding.request_sha256)):
        _refuse("command_request_conflict")
    verified = observation["durable_prefix_verified"] and fingerprint == binding.request_sha256
    status = ("recorded" if observation["status"] == "recorded" and verified else
              "pending" if observation["status"] == "evidence_only" and verified else "uncertain")
    return {"operation_key": binding.operation_key, "action": binding.action, "status": status,
            "target_state": binding.target_state if status == "recorded" else None,
            "revision": observation["revision"], "retry_with_new_key": False,
            "task_or_provider_action": False}


class OperatorCommandAdapter:
    """Trusted-host bridge to actual public runtime methods, never a grant issuer.

    The caller must obtain ``handle`` from Runtime.install_operator_commands.
    This adapter neither installs authority nor reaches into coordinator mutation.
    A coherent read binds the request; only runtime reconciliation can verify its
    durability or authorize execution. No raw binding is returned to a browser.
    """
    def __init__(self, runtime, handle):
        from _workspace_runtime import WorkspaceRuntime
        if type(runtime) is not WorkspaceRuntime or any(not callable(getattr(runtime, name, None)) for name in
                ("describe_operator_commands", "execute_operator_command", "reconcile_operator_command")):
            _refuse("operator_runtime_unavailable")
        self._runtime = runtime
        self._handle = handle
        self._installed_scope()

    @property
    def coordinator(self):
        # Read-only identity binding to the same coordinator used by the runtime;
        # the UI must not present one namespace while commanding another.
        return self._runtime._coordinator

    def _installed_scope(self):
        try:
            raw = self._runtime.describe_operator_commands(self._handle)
            if type(raw) is not dict or set(raw) != {"workspace_id", "run_id", "targets"} or type(raw["targets"]) is not list:
                _refuse("command_scope_required")
            targets = []
            for entry in raw["targets"]:
                if type(entry) is not dict or set(entry) != {"delivery_id", "actions"} or type(entry["actions"]) is not list:
                    _refuse("command_scope_required")
                targets.append((entry["delivery_id"], tuple(entry["actions"])))
            scope = CommandScope(raw["workspace_id"], raw["run_id"], tuple(targets))
            _scope(scope, raw)
            return scope
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("command_scope_required") from None

    def _workspace(self):
        try:
            for _ in range(2):
                state, torn, before = self.coordinator._load_with_snapshot()
                _records, later_torn, after = self.coordinator._read_records_snapshot()
                if not torn and not later_torn and before == after:
                    return state["workspace"]
        except Exception:
            pass
        raise WorkspaceCommandError("command_snapshot_unavailable")

    def describe(self, workspace, view_scope):
        if view_scope.audience != "operator":
            return {"available": False, "actions_by_delivery": {}, "reason": "read_only_export"}
        scope = self._installed_scope()
        allowed = _scope(scope, workspace)
        actions = {}
        for delivery_id, candidates in allowed.items():
            delivery = workspace["deliveries"].get(delivery_id)
            if delivery is None or not _linked_delivery_scope_valid(workspace, delivery):
                continue
            applicable = [name for name in candidates
                          if name in COMMAND_ACTIONS and delivery["state"] == ACTIONS[name][0]]
            if applicable:
                actions[_opaque(view_scope, "delivery", delivery_id)] = applicable
        return {"available": True, "actions_by_delivery": actions, "reason": None}

    def perform(self, body, *, view_scope, lookup=False, authorization_constraint=None):
        if type(lookup) is not bool:
            _refuse("invalid_command_request")
        scope = self._installed_scope()
        binding = bind_command(self._workspace(), view_scope=view_scope, command_scope=scope,
                               body=body, for_lookup=True)
        method = self._runtime.reconcile_operator_command if lookup else self._runtime.execute_operator_command
        try:
            observation = method(self._handle, binding.source_bytes, authorization_constraint=authorization_constraint)
            return present_lookup(binding, observation)
        except WorkspaceCommandError:
            raise
        except Exception as error:
            # Runtime exceptions may contain private data. Only map explicit
            # known public categories; uncertain failures are never non-admission.
            from _workspace_runtime import WorkspaceRuntimeError
            reason = error.kind if type(error) is WorkspaceRuntimeError else None
            if reason == "operator_authorization_constraint_refused":
                raise WorkspaceCommandError("operator_session_refused") from None
            if reason in {"command_not_authorized", "installed_operator_authority_required"}:
                raise WorkspaceCommandError("command_scope_required") from None
            if reason in {"operator_request_conflict", "operator_request_scope_conflict", "operator_command_state_refused"}:
                raise WorkspaceCommandError("command_request_conflict") from None
            raise WorkspaceCommandError("command_outcome_uncertain") from None


class OperatorDispositionAdapter:
    """Versioned authenticated retain-hold bridge.

    The public body contains only an opaque delivery handle, operation key and
    finite reason.  The trusted runtime resolves the task and registered
    request/decision evidence under its owner fence before appending the typed
    disposition event.  This deliberately does not widen the legacy command
    request contract.
    """
    def __init__(self, runtime, handle):
        from _workspace_runtime import WorkspaceRuntime
        if (type(runtime) is not WorkspaceRuntime
                or any(not callable(getattr(runtime, name, None)) for name in (
                    "describe_operator_dispositions", "execute_operator_disposition",
                    "reconcile_operator_disposition"))):
            _refuse("operator_runtime_unavailable")
        self._runtime = runtime
        self._handle = handle

    @property
    def coordinator(self):
        return self._runtime._coordinator

    def _scope(self):
        try:
            raw = self._runtime.describe_operator_dispositions(self._handle)
            if (type(raw) is not dict
                    or set(raw) != {"workspace_id", "run_id", "targets"}
                    or type(raw["targets"]) is not list
                    or not 1 <= len(raw["targets"]) <= 128):
                _refuse("command_scope_required")
            targets = {}
            for item in raw["targets"]:
                if (type(item) is not dict
                        or set(item) != {"delivery_id", "actions", "task_id"}
                        or type(item["actions"]) is not list
                        or item["actions"] != ["retain_held_context"]):
                    _refuse("command_scope_required")
                protocol._id(item["delivery_id"])
                protocol._id(item["task_id"])
                if item["delivery_id"] in targets:
                    _refuse("command_scope_required")
                targets[item["delivery_id"]] = copy.deepcopy(item)
            return raw, targets
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("command_scope_required") from None

    def describe(self, workspace, view_scope):
        if view_scope.audience != "operator":
            return {"available": False, "targets": [], "reason": "read_only_export"}
        raw, targets = self._scope()
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("command_scope_required")
        rows = []
        for index, (delivery_id, item) in enumerate(sorted(targets.items()), 1):
            delivery = workspace.get("deliveries", {}).get(delivery_id)
            if delivery is None or delivery.get("state") != "held_for_recovery":
                continue
            rows.append({"id": _opaque(view_scope, "delivery", delivery_id),
                         "task_id": _opaque(view_scope, "task", item["task_id"]),
                         "label": "Held context " + str(index), "available": True,
                         "reasons": ["awaiting_evidence", "awaiting_authorization", "operator_hold"]})
        return {"available": True, "targets": rows, "reason": None}

    def _bind(self, workspace, view_scope, body, *, for_lookup=False):
        if (type(body) is not dict or set(body) != {"operation_key", "target", "reason"}
                or type(body["operation_key"]) is not str
                or not _OPERATION.fullmatch(body["operation_key"])
                or type(body["target"]) is not str
                or type(body["reason"]) is not str
                or body["reason"] not in {"awaiting_evidence", "awaiting_authorization", "operator_hold"}):
            _refuse("invalid_command_request")
        try:
            raw, targets = self._scope()
            if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
                _refuse("command_scope_required")
            matches = [delivery_id for delivery_id in targets
                       if hmac.compare_digest(_opaque(view_scope, "delivery", delivery_id), body["target"])]
            if len(matches) != 1:
                _refuse("command_target_refused")
            delivery_id = matches[0]
            item = targets[delivery_id]
            delivery = workspace.get("deliveries", {}).get(delivery_id)
            if (not isinstance(delivery, dict)
                    or delivery.get("state") != "held_for_recovery"
                    and not (for_lookup and delivery.get("state") == "dead_lettered")):
                _refuse("command_state_refused")
            return {"operation_key": body["operation_key"], "delivery_id": delivery_id,
                    "task_id": item["task_id"], "reason": body["reason"]}, item
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("invalid_command_request") from None

    def _present(self, response, request, item, view_scope):
        if (type(response) is not dict or type(response.get("status")) is not str
                or response["status"] not in {"not_observed", "recorded", "uncertain"}
                or response.get("operation_key") != request["operation_key"]
                or type(response.get("revision")) is not int
                or not 0 <= response["revision"] <= protocol.MAX_INT):
            _refuse("invalid_command_observation")
        return {"schema": "summon.workspace.operator-disposition-result/v1",
                "status": response["status"], "operation_key": request["operation_key"],
                "action": "retain_held_context",
                "target": _opaque(view_scope, "delivery", request["delivery_id"]),
                "task_id": _opaque(view_scope, "task", item["task_id"]),
                "reason": request["reason"], "revision": response["revision"],
                "retry_with_new_key": False, "execution_authorized": False}

    def perform(self, body, *, view_scope, lookup=False, authorization_constraint=None):
        if type(lookup) is not bool:
            _refuse("invalid_command_request")
        workspace = self._runtime._coordinator._load()[0].get("workspace")
        if not isinstance(workspace, dict):
            _refuse("command_snapshot_unavailable")
        request, item = self._bind(workspace, view_scope, body, for_lookup=lookup)
        try:
            method = (self._runtime.reconcile_operator_disposition if lookup
                      else self._runtime.execute_operator_disposition)
            response = method(self._handle, request, authorization_constraint=authorization_constraint)
            return self._present(response, request, item, view_scope)
        except WorkspaceCommandError:
            raise
        except Exception as error:
            from _workspace_runtime import WorkspaceRuntimeError
            reason = error.kind if type(error) is WorkspaceRuntimeError else None
            if reason in {"operator_authorization_constraint_refused", "command_not_authorized"}:
                raise WorkspaceCommandError("command_scope_required") from None
            if reason in {"operator_disposition_state_refused", "operator_disposition_proof_unavailable"}:
                raise WorkspaceCommandError("command_state_refused") from None
            if reason in {"operator_disposition_conflict", "operator_request_conflict"}:
                raise WorkspaceCommandError("command_request_conflict") from None
            raise WorkspaceCommandError("command_outcome_uncertain") from None


class OperatorLinkedReplacementAdapter:
    """Opaque proposal bridge for the trusted linked-replacement host path.

    This adapter exposes only declared parent/recipient handles.  The runtime
    resolves their current delivery, authority sources and fresh grant; the
    browser never receives those private values or a generic event writer.
    """
    def __init__(self, runtime, handle):
        from _workspace_runtime import WorkspaceRuntime
        if (type(runtime) is not WorkspaceRuntime
                or any(not callable(getattr(runtime, name, None)) for name in (
                    "describe_operator_linked_replacements",
                    "execute_operator_linked_replacement",
                    "reconcile_operator_linked_replacement"))):
            _refuse("operator_runtime_unavailable")
        self._runtime = runtime
        self._handle = handle

    @property
    def coordinator(self):
        return self._runtime._coordinator

    def _scope(self):
        try:
            raw = self._runtime.describe_operator_linked_replacements(self._handle)
            if (type(raw) is not dict
                    or set(raw) != {"workspace_id", "run_id", "targets"}
                    or type(raw["targets"]) is not list
                    or not 1 <= len(raw["targets"]) <= 128):
                _refuse("linked_scope_required")
            targets = {}
            for item in raw["targets"]:
                if (type(item) is not dict
                        or set(item) != {"parent_target", "recipient_targets"}
                        or type(item["recipient_targets"]) is not list
                        or not item["recipient_targets"]):
                    _refuse("linked_scope_required")
                protocol._id(item["parent_target"])
                if item["parent_target"] in targets:
                    _refuse("linked_scope_required")
                for recipient in item["recipient_targets"]:
                    protocol._id(recipient)
                targets[item["parent_target"]] = tuple(item["recipient_targets"])
            return raw, targets
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("linked_scope_required") from None

    def describe(self, workspace, view_scope):
        if view_scope.audience != "operator":
            return {"available": False, "targets": [], "reason": "read_only_export"}
        raw, targets = self._scope()
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("linked_scope_required")
        rows = []
        for parent_target, recipients in sorted(targets.items()):
            for recipient_target in recipients:
                rows.append({
                    "parent_target": _opaque(view_scope, "linked-parent", parent_target),
                    "recipient_target": _opaque(view_scope, "linked-recipient",
                                                parent_target + ":" + recipient_target),
                    "available": True,
                })
        return {"available": True, "targets": rows, "reason": None}

    def _bind(self, workspace, view_scope, body):
        if (type(body) is not dict or set(body) != {"operation_key", "parent_target", "recipient_target"}
                or type(body["operation_key"]) is not str
                or not _OPERATION.fullmatch(body["operation_key"])
                or type(body["parent_target"]) is not str
                or type(body["recipient_target"]) is not str):
            _refuse("invalid_linked_request")
        raw, targets = self._scope()
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("linked_scope_required")
        parents = [parent for parent in targets
                   if hmac.compare_digest(_opaque(view_scope, "linked-parent", parent), body["parent_target"])]
        if len(parents) != 1:
            _refuse("linked_target_refused")
        parent = parents[0]
        recipients = [recipient for recipient in targets[parent]
                      if hmac.compare_digest(_opaque(view_scope, "linked-recipient", parent + ":" + recipient),
                                             body["recipient_target"])]
        if len(recipients) != 1:
            _refuse("linked_target_refused")
        return {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
                "action": admission.LINKED_REPLACEMENT_ACTION,
                "operation_key": body["operation_key"], "parent_target": parent,
                "recipient_target": recipients[0]}, (parent, recipients[0])

    def _present(self, response, proposal, body, view_scope):
        if (type(response) is not dict or response.get("status") not in {
                "recorded", "already_recorded", "not_observed", "uncertain"}
                or response.get("operation_key") != "linked-replacement-" + proposal["operation_key"]
                or type(response.get("revision")) is not int
                or not 0 <= response["revision"] <= protocol.MAX_INT):
            _refuse("invalid_linked_observation")
        return {"schema": "summon.workspace.linked-replacement-result/v1",
                "status": response["status"], "operation_key": proposal["operation_key"],
                "parent_target": body["parent_target"], "recipient_target": body["recipient_target"],
                "child_delivery": (_opaque(view_scope, "delivery", response["delivery_id"])
                                    if response.get("delivery_id") else None),
                "revision": response["revision"], "execution_authorized": False,
                "retry_with_new_key": False}

    def perform(self, body, *, view_scope, lookup=False, authorization_constraint=None):
        if type(lookup) is not bool or view_scope.audience != "operator":
            _refuse("invalid_linked_request")
        workspace = self._runtime._coordinator._load()[0].get("workspace")
        if not isinstance(workspace, dict):
            _refuse("command_snapshot_unavailable")
        proposal, _targets = self._bind(workspace, view_scope, body)
        try:
            method = (self._runtime.reconcile_operator_linked_replacement if lookup
                      else self._runtime.execute_operator_linked_replacement)
            response = method(self._handle, proposal, authorization_constraint=authorization_constraint)
            return self._present(response, proposal, body, view_scope)
        except WorkspaceCommandError:
            raise
        except Exception as error:
            from _workspace_runtime import WorkspaceRuntimeError
            reason = error.kind if type(error) is WorkspaceRuntimeError else None
            if reason in {"command_not_authorized", "operator_authorization_constraint_refused",
                          "operator_scope_link_conflict", "installed_operator_authority_required"}:
                raise WorkspaceCommandError("linked_scope_required") from None
            if reason in {"linked_replacement_proposal_invalid", "linked_replacement_authority_invalid",
                          "linked_replacement_source_unverified", "linked_replacement_source_binding",
                          "operator_link_scope_conflict"}:
                raise WorkspaceCommandError("linked_target_refused") from None
            if reason in {"linked_replacement_operation_conflict", "linked_replacement_lookup_uncertain"}:
                raise WorkspaceCommandError("linked_request_conflict") from None
            raise WorkspaceCommandError("linked_outcome_uncertain") from None


class OperatorMessageAdapter:
    """Scoped bridge for host-authored context, never a worker launcher.

    The browser receives only opaque target handles and bounded status. Raw
    operator targets, grant references, content blobs and runtime handles stay
    in this trusted host object. A send calls the installed runtime authority
    exactly once; a lookup uses the same operation key and never resends.
    """

    def __init__(self, runtime, handle):
        from _workspace_runtime import WorkspaceRuntime
        if (type(runtime) is not WorkspaceRuntime
                or any(not callable(getattr(runtime, name, None)) for name in
                       ("describe_operator_messages", "send_operator_message", "reconcile_operator_message"))):
            _refuse("operator_runtime_unavailable")
        self._runtime = runtime
        self._handle = handle

    @property
    def coordinator(self):
        return self._runtime._coordinator

    def _scope(self):
        try:
            raw = self._runtime.describe_operator_messages(self._handle)
            if (type(raw) is not dict
                    or set(raw) != {"workspace_id", "run_id", "operator_id", "targets"}
                    or type(raw["targets"]) is not list
                    or not 1 <= len(raw["targets"]) <= 16):
                _refuse("message_scope_required")
            targets = {}
            for item in raw["targets"]:
                if (type(item) is not dict
                        or not {"target", "task_id", "send_scope_ref"} <= set(item)
                        or set(item) - {"target", "task_id", "send_scope_ref", "recipient_instance_id"}):
                    _refuse("message_scope_required")
                protocol._id(item["target"])
                protocol._id(item["task_id"])
                if "recipient_instance_id" in item:
                    protocol._id(item["recipient_instance_id"])
                protocol._reference(item["send_scope_ref"])
                if item["target"] in targets:
                    _refuse("message_scope_required")
                targets[item["target"]] = copy.deepcopy(item)
            return raw, targets
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("message_scope_required") from None

    def describe(self, workspace, view_scope):
        if view_scope.audience != "operator":
            return {"available": False, "targets": [], "reason": "read_only_export"}
        raw, targets = self._scope()
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("message_scope_required")
        rows = []
        for index, (target, item) in enumerate(sorted(targets.items()), 1):
            task = workspace.get("lanes", {}).get(item["task_id"])
            if task is None:
                _refuse("message_scope_required")
            rows.append({"id": _opaque(view_scope, "operator_target", target),
                         "task_id": _opaque(view_scope, "task", item["task_id"]),
                         "label": "Task " + str(index),
                         "available": True})
        return {"available": True, "targets": rows, "reason": None}

    def _bind(self, workspace, view_scope, body):
        if (type(body) is not dict or set(body) != {"operation_key", "target", "text"}
                or type(body["operation_key"]) is not str
                or not _OPERATION.fullmatch(body["operation_key"])
                or type(body["target"]) is not str
                or type(body["text"]) is not str):
            _refuse("invalid_message_request")
        try:
            raw, targets = self._scope()
            if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
                _refuse("message_scope_required")
            matches = [target for target in targets
                       if hmac.compare_digest(_opaque(view_scope, "operator_target", target), body["target"])]
            if len(matches) != 1:
                _refuse("message_target_refused")
            admission._bounded_text(body["text"], "operator text", 2048)
            return {"operation_key": body["operation_key"], "target": matches[0], "text": body["text"]}, targets[matches[0]]
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("invalid_message_request") from None

    def _present(self, response, body, item, view_scope):
        if type(response) is not dict or type(response.get("status")) is not str:
            _refuse("invalid_message_observation")
        allowed = {"queued", "not_observed", "uncertain"}
        if response["status"] not in allowed:
            _refuse("invalid_message_observation")
        if response.get("operation_key") != body["operation_key"] or response.get("execution_authorized") is not False:
            _refuse("message_request_conflict")
        if type(response.get("revision")) is not int or not 0 <= response["revision"] <= protocol.MAX_INT:
            _refuse("invalid_message_observation")
        result = {"status": response["status"], "operation_key": body["operation_key"],
                  "target": _opaque(view_scope, "operator_target", body["target"]),
                  "task_id": _opaque(view_scope, "task", item["task_id"]),
                  "revision": response["revision"], "execution_authorized": False,
                  "retry_with_new_key": False, "message_id": None, "delivery_id": None}
        if response["status"] == "queued":
            if (response.get("request_sha256") is None or response.get("delivery_state") != "queued"
                    or type(response.get("stream_sequence")) is not int):
                _refuse("invalid_message_observation")
            result.update({"delivery_state": "queued", "message_id": _opaque(view_scope, "message", response["message_id"]),
                           "delivery_id": _opaque(view_scope, "delivery", response["delivery_id"]),
                           "stream_sequence": response["stream_sequence"]})
        else:
            result.update({"delivery_state": None, "stream_sequence": None})
        return result

    def perform(self, body, *, view_scope, lookup=False, authorization_constraint=None):
        if view_scope.audience != "operator":
            _refuse("message_scope_required")
        workspace = self._workspace()
        request, item = self._bind(workspace, view_scope, body)
        method = (self._runtime.reconcile_operator_message if lookup
                  else self._runtime.send_operator_message)
        try:
            response = method(self._handle, request, authorization_constraint=authorization_constraint)
            return self._present(response, request, item, view_scope)
        except WorkspaceCommandError:
            raise
        except Exception as error:
            from _workspace_runtime import WorkspaceRuntimeError
            reason = error.kind if type(error) is WorkspaceRuntimeError else None
            if reason == "operator_constraint_refused":
                raise WorkspaceCommandError("message_session_refused") from None
            if reason in {"operator_authority_required", "operator_target_not_permitted",
                          "operator_scope_source_unverified", "execution_grant_source_unverified", "command_not_authorized"}:
                raise WorkspaceCommandError("message_scope_required") from None
            if reason in {"operator_operation_conflict", "operator_send_conflict"}:
                raise WorkspaceCommandError("message_request_conflict") from None
            if reason in {"operator_send_uncertain", "operator_lookup_uncertain"}:
                raise WorkspaceCommandError("message_outcome_uncertain") from None
            raise WorkspaceCommandError("message_outcome_uncertain") from None

    def _workspace(self):
        try:
            for _ in range(2):
                state, torn, before = self.coordinator._load_with_snapshot()
                _records, later_torn, after = self.coordinator._read_records_snapshot()
                if not torn and not later_torn and before == after:
                    workspace = state.get("workspace")
                    if type(workspace) is dict:
                        return workspace
        except Exception:
            pass
        raise WorkspaceCommandError("message_snapshot_unavailable")


class OperatorDraftAdapter:
    """Host-only AES-GCM key bridge for one encrypted browser draft.

    The browser receives a derived key only after an authenticated POST.  The
    host retention secret, stable operator identity and raw target bindings
    never enter the view, journal, URL or browser persistence.
    """

    def __init__(self, runtime, handle):
        from _workspace_runtime import WorkspaceRuntime
        if (type(runtime) is not WorkspaceRuntime
                or not callable(getattr(runtime, "describe_operator_draft_retention", None))
                or not callable(getattr(runtime, "derive_operator_draft_key", None))):
            _refuse("operator_runtime_unavailable")
        self._runtime = runtime
        self._handle = handle

    @property
    def coordinator(self):
        return self._runtime._coordinator

    def _scope(self):
        try:
            raw = self._runtime.describe_operator_draft_retention(self._handle)
            if (type(raw) is not dict
                    or set(raw) != {"workspace_id", "run_id", "operator_scope", "key_epoch", "targets"}
                    or type(raw["targets"]) is not list or not 1 <= len(raw["targets"]) <= 16
                    or type(raw["operator_scope"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", raw["operator_scope"])
                    or type(raw["key_epoch"]) is not str or not 1 <= len(raw["key_epoch"]) <= 64):
                _refuse("message_retention_required")
            targets = {}
            for item in raw["targets"]:
                if type(item) is not dict or set(item) != {"target", "task_id"}:
                    _refuse("message_retention_required")
                protocol._id(item["target"]); protocol._id(item["task_id"])
                if item["target"] in targets:
                    _refuse("message_retention_required")
                targets[item["target"]] = item["task_id"]
            return raw, targets
        except WorkspaceCommandError:
            raise
        except Exception:
            raise WorkspaceCommandError("message_retention_required") from None

    def describe(self, workspace, view_scope):
        if view_scope.audience != "operator":
            return {"available": False, "operator_scope": None, "key_epoch": None,
                    "max_text_bytes": 2048, "max_record_bytes": 4096,
                    "reason": "read_only_export"}
        try:
            raw, targets = self._scope()
        except WorkspaceCommandError:
            return {"available": False, "operator_scope": None, "key_epoch": None,
                    "max_text_bytes": 2048, "max_record_bytes": 4096,
                    "reason": "message_retention_required"}
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("message_retention_required")
        return {"available": True, "operator_scope": raw["operator_scope"], "key_epoch": raw["key_epoch"],
                "max_text_bytes": 2048, "max_record_bytes": 4096, "reason": None}

    def _bind(self, workspace, view_scope, body):
        if (type(body) is not dict or set(body) != {"target", "operation_key", "state"}
                or type(body["target"]) is not str or type(body["operation_key"]) is not str
                or type(body["state"]) is not str or not _OPERATION.fullmatch(body["operation_key"])
                or body["state"] not in {"unsent", "sending", "uncertain"}):
            _refuse("invalid_message_key_request")
        raw, targets = self._scope()
        if raw["workspace_id"] != workspace.get("workspace_id") or raw["run_id"] != workspace.get("run_id"):
            _refuse("message_retention_required")
        matches = [target for target in targets
                   if hmac.compare_digest(_opaque(view_scope, "operator_target", target), body["target"])]
        if len(matches) != 1:
            _refuse("message_target_refused")
        return {"target": matches[0], "operation_key": body["operation_key"], "state": body["state"]}

    def perform(self, body, *, view_scope, authorization_constraint=None):
        if view_scope.audience != "operator":
            _refuse("message_retention_required")
        try:
            workspace = self._workspace()
            request = self._bind(workspace, view_scope, body)
            raw_scope, _ = self._scope()
            result = self._runtime.derive_operator_draft_key(
                self._handle, request, authorization_constraint=authorization_constraint)
            if (type(result) is not dict
                    or set(result) != {"key_b64", "operator_scope", "key_epoch", "max_text_bytes", "max_record_bytes"}
                    or type(result["key_b64"]) is not str
                    or len(result["key_b64"]) != 43
                    or type(result["operator_scope"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", result["operator_scope"])
                    or type(result["key_epoch"]) is not str
                    or result["operator_scope"] != raw_scope["operator_scope"]
                    or result["key_epoch"] != raw_scope["key_epoch"]
                    or result["max_text_bytes"] != 2048 or result["max_record_bytes"] != 4096):
                _refuse("invalid_message_key_observation")
            try:
                decoded = base64.urlsafe_b64decode(result["key_b64"] + "=")
            except (ValueError, TypeError):
                _refuse("invalid_message_key_observation")
            if len(decoded) != 32:
                _refuse("invalid_message_key_observation")
            return result
        except WorkspaceCommandError:
            raise
        except Exception as error:
            from _workspace_runtime import WorkspaceRuntimeError
            reason = error.kind if type(error) is WorkspaceRuntimeError else None
            if reason == "operator_constraint_refused":
                raise WorkspaceCommandError("operator_session_refused") from None
            if reason in {"operator_authority_required", "operator_target_not_permitted",
                          "command_not_authorized", "operator_retention_required", "operator_retention_scope_invalid"}:
                raise WorkspaceCommandError("message_retention_required") from None
            if reason in {"operator_retention_request_invalid"}:
                raise WorkspaceCommandError("invalid_message_key_request") from None
            raise WorkspaceCommandError("message_key_unavailable") from None

    def _workspace(self):
        try:
            for _ in range(2):
                state, torn, before = self.coordinator._load_with_snapshot()
                _records, later_torn, after = self.coordinator._read_records_snapshot()
                if not torn and not later_torn and before == after and type(state.get("workspace")) is dict:
                    return state["workspace"]
        except Exception:
            pass
        raise WorkspaceCommandError("message_snapshot_unavailable")
