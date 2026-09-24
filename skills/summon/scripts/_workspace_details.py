"""Bounded, typed workspace review/decision and evidence navigation.

This module is deliberately a host-installed read adapter.  It never discovers
records from a browser request and it never treats an opaque presentation ID as
an authority.  The host supplies a finite binding table and a resolver; the
adapter checks workspace/run/task/type scope before invoking either resolver.
Metadata and body access are separate installations.  In particular,
``allow_text`` on a normal workspace view does not grant access to a detail
body.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import copy
import hashlib
import hmac
import json
import re
from typing import Callable

import _workspace_protocol as protocol
from _workspace_view import ViewScope, _opaque as _view_opaque

SCHEMA = "summon.workspace.detail/v1"
BODY_SCHEMA = "summon.workspace.detail-body/v1"
SCOPE_SCHEMA = "summon.workspace.detail-scope/v1"
MAX_TARGETS = 256
MAX_LABEL = 160
MAX_BODY = 64 * 1024
MAX_DETAIL_FIELDS = 16
SOURCE_KINDS = frozenset({
    "workspace_assessment", "workspace_decision", "council_review",
    "deliberation_decision", "deliberation_pending", "workspace_evidence",
})
STATES = frozenset({"pending", "active", "completed", "blocked", "revoked", "stale"})
_KEY = re.compile(r"[a-f0-9]{32}\Z")


class WorkspaceDetailError(ValueError):
    """Stable, non-sensitive reason returned by the loopback facade."""


def _fail(reason: str):
    raise WorkspaceDetailError(reason)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("detail_scope_invalid")


def _opaque(key: bytes, workspace_id: str, run_id: str, kind: str, value: str) -> str:
    digest = hmac.new(key, _json([SCHEMA, workspace_id, run_id, kind, value]), hashlib.sha256).digest()[:24]
    return kind + "_" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _safe_label(value, *, required=False):
    if type(value) is not str or not value or len(value) > MAX_LABEL or not value.isprintable():
        if required:
            _fail("detail_scope_invalid")
        return None
    return value


def _workspace_tasks(workspace):
    lanes = workspace.get("lanes") if isinstance(workspace, dict) else None
    if not isinstance(lanes, dict):
        _fail("detail_workspace_invalid")
    return set(lanes)


def _scope(scope, workspace, *, body=False):
    if type(scope) is not dict:
        _fail("detail_scope_invalid")
    required = {"schema", "workspace_id", "run_id", "targets"}
    if set(scope) != required or scope.get("schema") != SCOPE_SCHEMA:
        _fail("detail_scope_invalid")
    if (scope["workspace_id"] != workspace.get("workspace_id")
            or scope["run_id"] != workspace.get("run_id")
            or type(scope["targets"]) is not list
            or not 1 <= len(scope["targets"]) <= MAX_TARGETS):
        _fail("detail_scope_refused")
    tasks = _workspace_tasks(workspace)
    seen = set()
    result = []
    for item in scope["targets"]:
        if type(item) is not dict:
            _fail("detail_scope_invalid")
        required_item = {"source_kind", "source_key", "task_id", "state", "label"}
        if set(item) - required_item - {"revision", "body_available"} or not required_item <= set(item):
            _fail("detail_scope_invalid")
        kind = item["source_kind"]
        source_key = item["source_key"]
        task_id = item["task_id"]
        if (kind not in SOURCE_KINDS or type(source_key) is not str or not 1 <= len(source_key) <= 256
                or type(task_id) is not str or task_id not in tasks
                or item["state"] not in STATES or _safe_label(item["label"], required=True) is None
                or source_key in seen):
            _fail("detail_scope_refused")
        revision = item.get("revision", 0)
        if type(revision) is not int or revision < 0:
            _fail("detail_scope_invalid")
        body_available = item.get("body_available", False)
        if type(body_available) is not bool:
            _fail("detail_scope_invalid")
        if body and body_available is not True:
            continue
        seen.add(source_key)
        result.append({"source_kind": kind, "source_key": source_key, "task_id": task_id,
                       "state": item["state"], "label": item["label"], "revision": revision,
                       "body_available": body_available})
    if body and not result:
        _fail("detail_body_scope_required")
    return result


def _safe_record(record, binding, *, body=False):
    if type(record) is not dict:
        _fail("detail_unavailable")
    allowed = {"source_kind", "task_id", "state", "title", "revision",
               "summary", "disposition", "next_decision", "decision", "criteria", "body"}
    if set(record) - allowed:
        _fail("detail_unavailable")
    if (record.get("source_kind") != binding.source_kind
            or record.get("task_id") != binding.task_id
            or record.get("state") != binding.state
            or _safe_label(record.get("title"), required=True) is None
            or type(record.get("revision")) is not int or record["revision"] != binding.revision):
        _fail("detail_unavailable")
    result = {"source_kind": binding.source_kind, "state": record["state"],
              "title": record["title"], "revision": record["revision"]}
    for key in ("summary", "disposition", "next_decision", "decision"):
        if key in record:
            value = record[key]
            if type(value) is not str or len(value) > MAX_LABEL or not value.isprintable():
                _fail("detail_unavailable")
            result[key] = value
    if "criteria" in record:
        criteria = record["criteria"]
        if type(criteria) is not list or len(criteria) > MAX_DETAIL_FIELDS:
            _fail("detail_unavailable")
        clean = []
        for item in criteria:
            if type(item) is not dict or set(item) - {"label", "result"} or "label" not in item or "result" not in item:
                _fail("detail_unavailable")
            _safe_label(item["label"], required=True)
            if type(item["result"]) is not str or len(item["result"]) > MAX_LABEL or not item["result"].isprintable():
                _fail("detail_unavailable")
            clean.append({"label": item["label"], "result": item["result"]})
        result["criteria"] = clean
    if body:
        text = record.get("body")
        if type(text) is not str or not 1 <= len(text) <= MAX_BODY:
            _fail("detail_body_unavailable")
        result = {"schema": BODY_SCHEMA, "source_kind": binding.source_kind,
                  "task_id": binding.task_id, "body": text}
    return result


@dataclass(frozen=True)
class DetailBinding:
    source_kind: str
    source_key: str
    task_id: str
    state: str
    label: str
    revision: int
    body_available: bool


class WorkspaceDetailAdapter:
    """Finite typed metadata/detail reader installed by a trusted host."""

    def __init__(self, coordinator, workspace_id, scope, *, resolve: Callable,
                 presentation_key: bytes, body_scope=None, resolve_body: Callable | None = None):
        if coordinator is None or type(workspace_id) is not str:
            _fail("detail_runtime_unavailable")
        if type(presentation_key) is not bytes or not 32 <= len(presentation_key) <= 64:
            _fail("detail_scope_invalid")
        if not callable(resolve):
            _fail("detail_runtime_unavailable")
        if resolve_body is not None and not callable(resolve_body):
            _fail("detail_runtime_unavailable")
        self.coordinator = coordinator
        self.workspace_id = workspace_id
        self._scope = copy.deepcopy(scope)
        self._key = presentation_key
        self._resolve = resolve
        self._resolve_body = resolve_body
        self._active = True
        self._body_scope = copy.deepcopy(body_scope)

    def revoke(self):
        self._active = False

    def _bindings(self, workspace, *, body=False):
        if not self._active:
            _fail("detail_scope_revoked")
        values = _scope(self._scope if not body else self._body_scope, workspace, body=body)
        if body:
            metadata = {item.source_key: item for item in self._bindings(workspace)}
            for item in values:
                prior = metadata.get(item["source_key"])
                if (prior is None or prior.source_kind != item["source_kind"]
                        or prior.task_id != item["task_id"]):
                    _fail("detail_scope_refused")
        return [DetailBinding(**item) for item in values]

    def _id(self, binding):
        return _opaque(self._key, self.workspace_id, self.coordinator.run_id,
                       "detail", binding.source_key)

    def _task(self, task_id):
        # Task handles must match the task shell; detail item handles retain
        # their separate namespace. Neither presentation handle is authority.
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key)
        return _view_opaque(scope, "task", task_id)

    def _workspace(self):
        state, torn = self.coordinator._load()
        if torn or not isinstance(state.get("workspace"), dict):
            _fail("detail_snapshot_unavailable")
        workspace = state["workspace"]
        if (workspace.get("workspace_id") != self.workspace_id
                or workspace.get("run_id") != self.coordinator.run_id):
            _fail("detail_scope_refused")
        return workspace

    def describe(self, workspace, view_scope):
        if getattr(view_scope, "audience", None) != "operator":
            return {"available": False, "targets": [], "reason": "detail_scope_required"}
        bindings = self._bindings(workspace)
        return {"available": True, "targets": [{
            "id": self._id(item), "task_id": self._task(item.task_id),
            "source_kind": item.source_kind, "state": item.state, "label": item.label,
            "revision": item.revision, "body_available": bool(item.body_available and self._body_scope),
        } for item in bindings]}

    def _lookup(self, workspace, target, *, source_kind=None, task=None):
        if type(target) is not str or not target:
            _fail("detail_target_refused")
        for binding in self._bindings(workspace):
            if hmac.compare_digest(self._id(binding), target):
                if binding.state in {"stale", "revoked"}:
                    _fail("detail_binding_stale")
                if source_kind is not None and source_kind != binding.source_kind:
                    _fail("detail_type_refused")
                if task is not None and not hmac.compare_digest(self._task(binding.task_id), task):
                    _fail("detail_task_refused")
                return binding
        _fail("detail_target_refused")

    def list_or_detail(self, workspace, *, target=None, source_kind=None, task=None):
        if target is None:
            if source_kind is not None and source_kind not in SOURCE_KINDS:
                _fail("detail_type_refused")
            bindings = self._bindings(workspace)
            if task is not None:
                matches = [item for item in bindings if hmac.compare_digest(self._task(item.task_id), task)]
                if not matches:
                    _fail("detail_task_refused")
                bindings = matches
            if source_kind is not None:
                bindings = [item for item in bindings if item.source_kind == source_kind]
            targets = [{"id": self._id(item), "task_id": self._task(item.task_id),
                        "source_kind": item.source_kind, "state": item.state,
                        "label": item.label, "revision": item.revision,
                        "body_available": bool(item.body_available and self._body_scope)}
                       for item in bindings]
            return {"schema": SCHEMA, "status": "listed", "targets": targets}
        binding = self._lookup(workspace, target, source_kind=source_kind, task=task)
        # The resolver is reached only after all opaque binding checks pass.
        result = _safe_record(self._resolve(dict(binding.__dict__)), binding)
        return {"schema": SCHEMA, "status": "detail", "id": self._id(binding),
                "task_id": self._task(binding.task_id), **result,
                "body_available": bool(binding.body_available and self._body_scope)}

    def body(self, workspace, *, target, source_kind=None, task=None):
        if self._resolve_body is None or self._body_scope is None:
            _fail("detail_body_scope_required")
        if not self._active:
            _fail("detail_scope_revoked")
        # Require a separately installed body scope; metadata scope alone is not enough.
        body_bindings = self._bindings(workspace, body=True)
        binding = None
        for item in body_bindings:
            if hmac.compare_digest(self._id(item), target):
                binding = item
                break
        if binding is None:
            _fail("detail_body_scope_required")
        if source_kind is not None and source_kind != binding.source_kind:
            _fail("detail_type_refused")
        if task is not None and not hmac.compare_digest(self._task(binding.task_id), task):
            _fail("detail_task_refused")
        if binding.state in {"stale", "revoked"}:
            _fail("detail_binding_stale")
        result = _safe_record(self._resolve_body(dict(binding.__dict__)), binding, body=True)
        result["task_id"] = self._task(binding.task_id)
        return result
