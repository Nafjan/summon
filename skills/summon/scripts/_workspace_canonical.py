"""Canonical workspace detail bindings.

The workspace detail UI is intentionally not a second record store.  This
module binds a finite, host-owned set of source keys to the existing canonical
writers/readers: the workspace journal, council receipts, and deliberation
journals.  Browser requests see only opaque detail handles; source paths and
receipt contents stay in the trusted host process.
"""
from __future__ import annotations

import copy
import json
import os
import unicodedata
from typing import Callable, Mapping

import _council
import _deliberation_store
from _workspace_details import (BODY_SCHEMA, SCHEMA, SCOPE_SCHEMA, SOURCE_KINDS,
                                WorkspaceDetailError)


class CanonicalDetailError(WorkspaceDetailError):
    """A host-owned canonical binding is malformed or no longer readable."""


_MAX_EXTERNAL = 64
_MAX_BODY = 64 * 1024
_EVIDENCE_CATEGORIES = frozenset({
    "artifact", "event", "adapter_receipt", "observation", "fence",
})
_DELIBERATION_DECISION_LABELS = {
    "DECIDED": "decision_recorded",
    "UNRESOLVED": "decision_unresolved",
    "REJECTED": "decision_rejected",
    "CANCELLED": "decision_cancelled",
    "TIMED_OUT": "decision_timed_out",
    "ATTEMPT_BUDGET_EXHAUSTED": "decision_attempt_budget_exhausted",
    "FAILED": "decision_failed",
}


def _fail(reason: str):
    raise CanonicalDetailError(reason)


def _text(value, *, name: str, limit: int = 256) -> str:
    if type(value) is not str or not value or len(value) > limit or not value.isprintable():
        _fail("canonical_binding_invalid")
    return value


def _root(value, *, name: str) -> str:
    value = _text(value, name=name, limit=4096)
    result = os.path.abspath(value)
    if not os.path.isdir(result):
        _fail("canonical_binding_unavailable")
    return result


def _safe_json(value, limit: int = _MAX_BODY) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        _fail("canonical_detail_unavailable")
    if not 1 <= len(text) <= limit or not text.isprintable():
        _fail("canonical_detail_unavailable")
    return text


def _body_text(value) -> str:
    """Accept bounded human text while rejecting executable/control bytes."""
    if type(value) is not str or not 1 <= len(value) <= _MAX_BODY:
        _fail("canonical_detail_unavailable")
    if any((unicodedata.category(char) in {"Cc", "Cf"}
            and char not in "\n\r\t") for char in value):
        _fail("canonical_detail_unavailable")
    return value


def _record(*, source_kind: str, task_id: str, state: str, title: str,
            revision: int, summary: str | None = None,
            disposition: str | None = None, next_decision: str | None = None,
            decision: str | None = None, criteria: list[dict] | None = None,
            body: str | None = None) -> dict:
    result = {
        "source_kind": source_kind, "task_id": task_id, "state": state,
        "title": _text(title, name="title", limit=160), "revision": revision,
    }
    for key, value in (("summary", summary), ("disposition", disposition),
                       ("next_decision", next_decision), ("decision", decision)):
        if value is not None:
            result[key] = _text(value, name=key, limit=160)
    if criteria is not None:
        if type(criteria) is not list or len(criteria) > 16:
            _fail("canonical_detail_unavailable")
        result["criteria"] = copy.deepcopy(criteria)
    if body is not None:
        result["body"] = _body_text(body)
    return result


def _scope_target(source_kind: str, source_key: str, task_id: str, *, state: str,
                  label: str, revision: int, body_available: bool) -> dict:
    if source_kind not in SOURCE_KINDS:
        _fail("canonical_binding_invalid")
    return {"source_kind": source_kind, "source_key": _text(source_key, name="source_key"),
            "task_id": _text(task_id, name="task_id"), "state": _text(state, name="state"),
            "label": _text(label, name="label", limit=160), "revision": revision,
            "body_available": bool(body_available)}


def _workspace_snapshot(runtime, workspace_id: str, run_id: str) -> dict:
    state, torn = runtime._coordinator._load()
    workspace = state.get("workspace") if isinstance(state, dict) else None
    if torn or not isinstance(workspace, dict):
        _fail("canonical_snapshot_unavailable")
    if workspace.get("workspace_id") != workspace_id or workspace.get("run_id") != run_id:
        _fail("canonical_scope_refused")
    return workspace


def _workspace_targets(runtime, workspace: dict, *, workspace_id: str, run_id: str):
    targets: list[dict] = []
    descriptors: dict[tuple[str, str], dict] = {}

    for source_key, item in sorted(workspace.get("assessments", {}).items()):
        record = item.get("record") if isinstance(item, dict) else None
        if not isinstance(record, dict) or record.get("task_id") not in workspace.get("lanes", {}):
            continue
        key = "workspace_assessment:" + source_key
        state = "blocked" if record.get("disposition") == "hold_affected_lane" else "completed"
        revision = item.get("event_revision")
        if type(revision) is not int:
            continue
        target = _scope_target("workspace_assessment", key, record["task_id"], state=state,
                               label="Workspace assessment", revision=revision,
                               body_available=True)
        targets.append(target)
        descriptors[(target["source_kind"], key)] = {"kind": "workspace_assessment",
                                                       "raw_key": source_key}

    for source_key, item in sorted(workspace.get("decisions", {}).items()):
        if not isinstance(item, dict) or item.get("from_task_id") not in workspace.get("lanes", {}):
            continue
        key = "workspace_decision:" + source_key
        target = _scope_target("workspace_decision", key, item["from_task_id"], state="completed",
                               label="Workspace decision", revision=workspace["revision"],
                               body_available=True)
        targets.append(target)
        descriptors[(target["source_kind"], key)] = {"kind": "workspace_decision",
                                                       "raw_key": source_key}

    for source_key, item in sorted(workspace.get("evidence", {}).items()):
        if not isinstance(item, dict) or item.get("category") not in _EVIDENCE_CATEGORIES:
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or task_id not in workspace.get("lanes", {}):
            continue
        reference = item.get("reference")
        if not isinstance(reference, dict) or reference.get("id") != source_key:
            continue
        # The source body is checked at detail time.  Mark it separately
        # authorized, but do not make the metadata path read arbitrary blobs.
        key = "workspace_evidence:" + source_key
        target = _scope_target("workspace_evidence", key, task_id, state="completed",
                               label="Workspace evidence", revision=workspace["revision"],
                               body_available=True)
        targets.append(target)
        descriptors[(target["source_kind"], key)] = {"kind": "workspace_evidence",
                                                       "raw_key": source_key}

    return targets, descriptors


def _external_observation(root: str, run_id: str, *, kind: str):
    """Read the authoritative external state used to validate a binding."""
    if kind == "council_review":
        receipt = _council._read_council_receipt(root, run_id)
        if not isinstance(receipt, dict):
            _fail("canonical_binding_unavailable")
        raw_status = receipt.get("status") or receipt.get("council_state")
        observed_state = ("completed" if raw_status in {"success", "final", "completed"}
                          else "blocked" if raw_status in {"error", "failed", "blocked"}
                          else "pending")
        observed_revision = receipt.get("generation", 1)
        if type(observed_revision) is not int or observed_revision < 0:
            _fail("canonical_binding_unavailable")
        return observed_state, observed_revision, receipt

    if kind not in {"deliberation_decision", "deliberation_pending"}:
        _fail("canonical_binding_invalid")
    status = _deliberation_store.inspect_run(root, run_id)
    projection = status.get("projection") if isinstance(status, dict) else None
    raw_state = projection.get("state") if isinstance(projection, dict) else None
    if not isinstance(raw_state, str):
        _fail("canonical_binding_unavailable")
    observed_state = ("completed" if raw_state.upper() in {
        "DECIDED", "UNRESOLVED", "REJECTED", "CANCELLED", "TIMED_OUT",
        "ATTEMPT_BUDGET_EXHAUSTED", "FAILED"}
                      else "blocked" if status.get("status") == "blocked"
                      else "pending")
    observed_revision = status.get("current_generation")
    if type(observed_revision) is not int or observed_revision < 0:
        _fail("canonical_binding_unavailable")
    expected_kind = ("deliberation_pending" if observed_state in {"pending", "blocked"}
                     else "deliberation_decision")
    if kind != expected_kind:
        _fail("canonical_binding_mismatch")
    return observed_state, observed_revision, status


def _external_target(item: Mapping[str, object], *, kind: str, index: int,
                     workspace: dict) -> tuple[dict, dict]:
    if type(item) is not dict:
        _fail("canonical_binding_invalid")
    raw_key = _text(item.get("source_key"), name="source_key")
    task_id = _text(item.get("task_id"), name="task_id")
    if task_id not in workspace.get("lanes", {}):
        _fail("canonical_scope_refused")
    label = _text(item.get("label", kind.replace("_", " ").title()), name="label", limit=160)
    root = _root(item.get("root"), name="root")
    run_id = _text(item.get("run_id"), name="run_id")
    observed_state, observed_revision, _ = _external_observation(root, run_id, kind=kind)
    declared_state = item.get("state", observed_state)
    declared_revision = item.get("revision", observed_revision)
    if declared_state != observed_state or declared_revision != observed_revision:
        _fail("canonical_binding_mismatch")
    key = kind + ":" + raw_key
    target = _scope_target(kind, key, task_id, state=observed_state, label=label,
                           revision=observed_revision, body_available=False)
    descriptor = {"kind": kind, "root": root, "run_id": run_id, "raw_key": raw_key,
                  "source_key": key, "index": index, "state": observed_state,
                  "revision": observed_revision}
    return target, descriptor


def build(runtime, workspace_id: str, run_id: str, *, council_sources=None,
          deliberation_sources=None, body_source_keys=None):
    """Return ``scope, body_scope, readers, body_readers`` for canonical data.

    ``council_sources`` and ``deliberation_sources`` are finite host-owned
    descriptor lists.  Their absolute roots never enter the browser scope.
    Metadata is the default: ``body_source_keys`` must explicitly contain the
    canonical ``<source_kind>:<source_key>`` values that may expose a body.
    This keeps evidence and external journal contents out of the body scope
    unless the trusted host deliberately authorizes each source.
    """
    workspace = _workspace_snapshot(runtime, workspace_id, run_id)
    targets, descriptors = _workspace_targets(runtime, workspace,
                                               workspace_id=workspace_id,
                                               run_id=run_id)
    external = []
    for kind, values in (("council_review", council_sources),
                         ("deliberation_decision", deliberation_sources)):
        if values is None:
            continue
        if type(values) is not list or len(values) > _MAX_EXTERNAL:
            _fail("canonical_binding_invalid")
        for index, item in enumerate(values):
            selected_kind = kind
            if kind == "deliberation_decision" and isinstance(item, dict):
                if item.get("state", "completed") in {"pending", "active"}:
                    selected_kind = "deliberation_pending"
            target, descriptor = _external_target(item, kind=selected_kind, index=index,
                                                  workspace=workspace)
            external.append((target, descriptor))
    targets.extend(item[0] for item in external)
    descriptors.update(((target[0]["source_kind"], target[0]["source_key"]), target[1])
                       for target in external)
    if not targets:
        _fail("canonical_scope_empty")
    if body_source_keys is None:
        body_keys = set()
    elif type(body_source_keys) is list and len(body_source_keys) <= len(targets):
        body_keys = set()
        for key in body_source_keys:
            if type(key) is not str or key not in {item["source_key"] for item in targets}:
                _fail("canonical_body_scope_invalid")
            body_keys.add(key)
    else:
        _fail("canonical_body_scope_invalid")
    for item in targets:
        item["body_available"] = item["source_key"] in body_keys

    def _workspace_record(binding, *, body=False):
        current = _workspace_snapshot(runtime, workspace_id, run_id)
        descriptor = descriptors.get((binding["source_kind"], binding["source_key"]))
        if not descriptor:
            _fail("canonical_target_refused")
        kind, raw_key = descriptor["kind"], descriptor["raw_key"]
        if kind == "workspace_assessment":
            item = current.get("assessments", {}).get(raw_key)
            record = item.get("record") if isinstance(item, dict) else None
            revision = item.get("event_revision") if isinstance(item, dict) else None
            if not isinstance(record, dict) or revision != binding["revision"]:
                _fail("canonical_binding_stale")
            body_text = _safe_json({"reason": record.get("reason"),
                                    "handoff": record.get("handoff"),
                                    "limitations": record.get("limitations", [])})
            criteria = [{"label": item.get("criterion_id", "criterion"),
                         "result": item.get("result", "unknown")}
                        for item in record.get("criterion_results", [])]
            return _record(source_kind=binding["source_kind"], task_id=record["task_id"],
                           state=binding["state"], title="Workspace assessment",
                           revision=revision, summary="Canonical supervisor assessment recorded",
                           disposition=record.get("disposition"),
                           next_decision=record.get("next_decision"), criteria=criteria,
                           body=body_text if body else None)
        if kind == "workspace_decision":
            item = current.get("decisions", {}).get(raw_key)
            if not isinstance(item, dict) or current.get("revision") != binding["revision"]:
                _fail("canonical_binding_stale")
            body_text = _safe_json(item)
            return _record(source_kind=binding["source_kind"], task_id=item["from_task_id"],
                           state=binding["state"], title="Workspace decision",
                           revision=binding["revision"],
                           summary="Canonical successor decision recorded", decision="successor_selected",
                           body=body_text if body else None)
        if kind == "workspace_evidence":
            item = current.get("evidence", {}).get(raw_key)
            if not isinstance(item, dict) or current.get("revision") != binding["revision"]:
                _fail("canonical_binding_stale")
            reference = item.get("reference", {})
            summary = "Registered " + str(item.get("category", "evidence")) + " evidence"
            body_text = None
            if body:
                try:
                    raw = runtime._content_store().read(runtime._content_store().descriptor_for_reference(reference))
                except AttributeError:
                    # The canonical content resolver is intentionally used
                    # through the host's existing resolver contract when a
                    # store implementation does not expose a descriptor helper.
                    try:
                        from _workspace_entry import _content_resolver
                        raw = _content_resolver(runtime)(item)
                    except Exception as exc:  # noqa: BLE001 - normalize source errors
                        raise CanonicalDetailError("canonical_detail_unavailable") from exc
                except Exception as exc:  # noqa: BLE001 - normalize source errors
                    raise CanonicalDetailError("canonical_detail_unavailable") from exc
                try:
                    body_text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                except (UnicodeError, TypeError) as exc:
                    raise CanonicalDetailError("canonical_detail_unavailable") from exc
                body_text = _body_text(body_text)
            return _record(source_kind=binding["source_kind"], task_id=item["task_id"],
                           state=binding["state"], title="Workspace evidence",
                           revision=binding["revision"], summary=summary,
                           body=body_text)
        _fail("canonical_target_refused")

    def _external_record(binding, *, body=False):
        descriptor = descriptors.get((binding["source_kind"], binding["source_key"]))
        if not descriptor:
            _fail("canonical_target_refused")
        observed_state, observed_revision, observed_payload = _external_observation(
            descriptor["root"], descriptor["run_id"], kind=descriptor["kind"])
        if (observed_state != binding["state"]
                or observed_revision != binding["revision"]):
            _fail("canonical_binding_stale")
        if descriptor["kind"] == "council_review":
            # Keep validation and returned content on the same receipt read.
            # A second read could observe a newer generation while the record
            # remains labeled with the generation validated above.
            receipt = observed_payload
            if not isinstance(receipt, dict):
                _fail("canonical_detail_unavailable")
            body_text = _safe_json({
                "mode": receipt.get("mode"), "run_id": receipt.get("run_id"),
                "question": receipt.get("question"), "state": receipt.get("council_state"),
                "status": receipt.get("status"),
            })
            return _record(source_kind=binding["source_kind"], task_id=binding["task_id"],
                           state=binding["state"], title="Council review",
                           revision=binding["revision"], summary="Durable council receipt",
                           decision="receipt_recorded",
                           body=body_text if body else None)
        if descriptor["kind"] in {"deliberation_decision", "deliberation_pending"}:
            # Reuse the generation-stable status view that supplied the
            # validation above; do not inspect the run a second time.
            status = observed_payload
            if not isinstance(status, dict) or status.get("run_id") != descriptor["run_id"]:
                _fail("canonical_detail_unavailable")
            if descriptor["kind"] == "deliberation_pending":
                decision_label = "pending"
            else:
                projection = status.get("projection")
                projection_state = (projection.get("state")
                                    if isinstance(projection, dict) else None)
                if not isinstance(projection_state, str):
                    _fail("canonical_detail_unavailable")
                decision_label = _DELIBERATION_DECISION_LABELS.get(
                    projection_state.upper())
                if decision_label is None:
                    _fail("canonical_detail_unavailable")
            body_text = None
            if body:
                if descriptor["kind"] == "deliberation_pending":
                    body_text = _safe_json({"status": status.get("status"),
                                            "projection": status.get("projection"),
                                            "pending_commands": status.get("pending_commands")})
                else:
                    # ``inspect_run`` already returns the bounded public
                    # status/projection/receipt view and the validated
                    # current generation.  Replaying here would read the
                    # journal again without a generation in its result to
                    # bind that payload to the validation above.
                    body_text = _safe_json({"status": status.get("status"),
                                            "projection": status.get("projection"),
                                            "receipt": status.get("receipt")})
            return _record(source_kind=binding["source_kind"], task_id=binding["task_id"],
                           state=binding["state"],
                           title=("Deliberation pending" if descriptor["kind"] == "deliberation_pending"
                                  else "Deliberation decision"),
                           revision=binding["revision"], summary="Durable deliberation journal",
                           decision=decision_label,
                           body=body_text)
        _fail("canonical_target_refused")

    def resolve(binding):
        kind = descriptors.get((binding["source_kind"], binding["source_key"]), {}).get("kind")
        return (_workspace_record(binding) if kind in {
            "workspace_assessment", "workspace_decision", "workspace_evidence"}
            else _external_record(binding))

    def resolve_body(binding):
        kind = descriptors.get((binding["source_kind"], binding["source_key"]), {}).get("kind")
        return (_workspace_record(binding, body=True) if kind in {
            "workspace_assessment", "workspace_decision", "workspace_evidence"}
            else _external_record(binding, body=True))

    scope = {"schema": SCOPE_SCHEMA, "workspace_id": workspace_id, "run_id": run_id,
             "targets": targets}
    body_scope = None
    if body_keys:
        body_scope = {"schema": SCOPE_SCHEMA, "workspace_id": workspace_id, "run_id": run_id,
                      "targets": [item for item in targets if item.get("body_available") is True]}
    return scope, body_scope, resolve, resolve_body
