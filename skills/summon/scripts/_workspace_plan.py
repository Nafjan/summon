"""Strict, provider-free workspace plan parsing and compilation.

The plan format is intentionally smaller than the runtime protocol.  It is a
user-facing definition language, not an authority or provider configuration.
Unknown fields are rejected so that a plan cannot smuggle routing, spend,
permission, or execution controls into a workspace definition.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from typing import Any

from _context_target import _final_open_path
from _fleet_approval import _regular_single_link, _reject_reparse_ancestors
import _workspace_protocol as protocol
import _context_policy


PLAN_SCHEMA = "summon.workspace.plan/v1"
PLAN_V2_SCHEMA = "summon.workspace.plan/v2"
ECONOMICS_SCHEMA = "summon.workspace.economics/v1"
ECONOMICS_FEATURE = "submission-accounting-v1"
PLAN_HOST_SCHEMA = "summon.workspace-host/v2"
PLAN_FILE_NAME = "workspace-plan.json"
PROJECT_BINDING_SCHEMA = "summon.workspace.plan-project-binding/v1"
ROSTER_BINDING_SCHEMA = "summon.workspace.plan-recipient-binding/v1"
MAX_PLAN_BYTES = 65536
MAX_TASKS = 16
MAX_CRITERIA = 16
MAX_CONSTRAINTS = 16
MAX_DECISIONS = 16
MAX_TARGETS = 16
MAX_ID_BYTES = 128
MAX_OBJECTIVE_BYTES = 4096
MAX_CRITERION_BYTES = 2048
MAX_TASK_TEXT_BYTES = 2048
MAX_SHORT_TEXT_BYTES = 1024
MAX_DEPENDENCIES = 8
MAX_CONTEXT_BYTES = 16384
MAX_DURATION_MS = 86_400_000
SCOPE_TTL_MS = 7 * 24 * 60 * 60 * 1000
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


class WorkspacePlanError(ValueError):
    """Safe, stable plan failure; never includes input paths or content."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"workspace plan refused ({kind})")


def _fail(kind: str):
    raise WorkspacePlanError(kind)


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict or set(value) != required | (optional or set()):
        _fail("plan_fields")
    return value


def _id(value: Any):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("plan_id")
    try:
        if len(value.encode("ascii")) > MAX_ID_BYTES:
            _fail("plan_id")
    except UnicodeEncodeError:
        _fail("plan_id")
    return value


def _text(value: Any, maximum: int, *, allow_empty: bool = False):
    if type(value) is not str or (not allow_empty and not value) or "\x00" in value:
        _fail("plan_text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("plan_text")
    if len(encoded) > maximum:
        _fail("plan_text_limit")
    return value


def _list(value: Any, maximum: int, *, minimum: int = 0):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        _fail("plan_list")
    return value


def _integer(value: Any, minimum: int, maximum: int):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("plan_integer")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("plan_duplicate_key")
        result[key] = value
    return result


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("plan_json")


def parse_plan_bytes(raw: bytes) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PLAN_BYTES:
        _fail("plan_size")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        _fail("plan_encoding")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except WorkspacePlanError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        _fail("plan_json")
    validate_plan(value)
    return copy.deepcopy(value)


def read_plan_file(path: str) -> tuple[dict, bytes, str]:
    """Read one stable regular private file without exposing its path."""
    if type(path) is not str or not path:
        _fail("plan_file")
    try:
        _reject_reparse_ancestors(path)
        expected = _regular_single_link(path, "workspace plan")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            final = _final_open_path(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                    or final is None
                    or os.path.normcase(final) != os.path.normcase(os.path.realpath(path))):
                _fail("plan_file_changed")
            chunks = []
            remaining = MAX_PLAN_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            current = _regular_single_link(path, "workspace plan")
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            if (identity(before) != identity(after) or identity(after) != identity(current)
                    or len(raw) != after.st_size):
                _fail("plan_file_changed")
        finally:
            os.close(fd)
    except WorkspacePlanError:
        raise
    except (OSError, ValueError, TypeError):
        _fail("plan_file_unavailable")
    value = parse_plan_bytes(raw)
    return value, raw, hashlib.sha256(raw).hexdigest()


def _validate_plan_v1(value: Any) -> dict:
    root = _object(value, {"schema", "goal", "tasks", "operator_message_targets"})
    if root["schema"] != PLAN_SCHEMA:
        _fail("plan_schema")
    goal = _object(root["goal"], {"goal_id", "objective", "criteria", "constraints",
                                  "active_priority", "unresolved_decisions"})
    for key in ("goal_id", "active_priority"):
        _id(goal[key])
    _text(goal["objective"], MAX_OBJECTIVE_BYTES)
    criteria = _list(goal["criteria"], MAX_CRITERIA, minimum=1)
    criterion_ids = set()
    for criterion in criteria:
        item = _object(criterion, {"criterion_id", "description", "evidence_requirement"})
        _id(item["criterion_id"])
        _text(item["description"], MAX_CRITERION_BYTES)
        _text(item["evidence_requirement"], MAX_CRITERION_BYTES)
        if item["criterion_id"] in criterion_ids:
            _fail("plan_duplicate_criterion")
        criterion_ids.add(item["criterion_id"])
    for key, maximum in (("constraints", MAX_CONSTRAINTS), ("unresolved_decisions", MAX_DECISIONS)):
        values = _list(goal[key], maximum)
        for value_item in values:
            _text(value_item, MAX_SHORT_TEXT_BYTES)

    tasks = _list(root["tasks"], MAX_TASKS, minimum=1)
    task_ids = set()
    normalized_tasks = []
    for task in tasks:
        item = _object(task, {"task_id", "role", "outcome", "scope", "depends_on",
                              "criterion_ids", "return_condition", "escalation_trigger", "limits"})
        _id(item["task_id"])
        if item["task_id"] in task_ids:
            _fail("plan_duplicate_task")
        task_ids.add(item["task_id"])
        if item["role"] not in {"main", "investigation", "review", "validation"}:
            _fail("plan_role")
        for key in ("outcome", "scope", "return_condition", "escalation_trigger"):
            _text(item[key], MAX_TASK_TEXT_BYTES)
        dependencies = _list(item["depends_on"], MAX_DEPENDENCIES)
        for dependency in dependencies:
            _id(dependency)
        if len(set(dependencies)) != len(dependencies):
            _fail("plan_duplicate_dependency")
        assigned = _list(item["criterion_ids"], MAX_CRITERIA, minimum=1)
        if len(set(assigned)) != len(assigned) or set(assigned) - criterion_ids:
            _fail("plan_criterion_reference")
        limits = _object(item["limits"], {"max_duration_ms", "max_attempts", "max_context_bytes"})
        _integer(limits["max_duration_ms"], 1000, MAX_DURATION_MS)
        _integer(limits["max_attempts"], 1, 8)
        _integer(limits["max_context_bytes"], 256, MAX_CONTEXT_BYTES)
        normalized_tasks.append(copy.deepcopy(item))
    if goal["active_priority"] not in task_ids:
        _fail("plan_priority")

    # Validate dependency references and acyclicity after all task IDs exist.
    task_map = {item["task_id"]: item for item in normalized_tasks}
    for item in normalized_tasks:
        if set(item["depends_on"]) - task_ids:
            _fail("plan_dependency_reference")
    visiting, visited = set(), set()
    def visit(task_id):
        if task_id in visiting:
            _fail("plan_dependency_cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in task_map[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in task_ids:
        visit(task_id)
    if task_map[goal["active_priority"]]["depends_on"]:
        _fail("plan_priority")

    targets = _list(root["operator_message_targets"], MAX_TARGETS, minimum=1)
    target_ids = set()
    target_tasks = set()
    normalized_targets = []
    for target in targets:
        item = _object(target, {"target", "task_id"})
        _id(item["target"]); _id(item["task_id"])
        if item["task_id"] not in task_ids:
            _fail("plan_target_reference")
        if item["target"] in target_ids or item["task_id"] in target_tasks:
            _fail("plan_duplicate_target")
        target_ids.add(item["target"]); target_tasks.add(item["task_id"])
        normalized_targets.append(copy.deepcopy(item))

    normalized = {"schema": PLAN_SCHEMA, "goal": copy.deepcopy(goal),
                  "tasks": normalized_tasks, "operator_message_targets": normalized_targets}
    _canonical(normalized)
    return normalized


def _validate_economics_fence(value: Any) -> dict:
    if type(value) is not dict or set(value) != {
            "schema", "feature", "enabled", "max_records", "max_settlement_bytes"}:
        _fail("economics_feature_fields")
    if (value["schema"] != ECONOMICS_SCHEMA or value["feature"] != ECONOMICS_FEATURE
            or value["enabled"] is not True
            or type(value["max_records"]) is not int
            or not 1 <= value["max_records"] <= 64
            or type(value["max_settlement_bytes"]) is not int
            or not protocol.MIN_ECONOMICS_SETTLEMENT_BYTES <= value["max_settlement_bytes"] <= 65536):
        _fail("economics_feature_fence")
    return copy.deepcopy(value)


def _validate_plan_v2(value: Any) -> dict:
    """Validate the additive economics-aware plan without changing v1 rules."""
    root = _object(value, {"schema", "goal", "tasks", "operator_message_targets", "economics"})
    if root["schema"] != PLAN_V2_SCHEMA:
        _fail("plan_schema")
    economics = _validate_economics_fence(root["economics"])
    # Reuse every v1 goal/task/recipient rule, then require an explicit policy
    # on each task.  This is a validation projection only; no bytes are rewritten.
    base = copy.deepcopy(root)
    base["schema"] = PLAN_SCHEMA
    # The v1 validator is intentionally exact-fielded.  Keep the economics
    # fence in the v2 projection, but remove it from the compatibility view
    # rather than weakening the unchanged v1 contract.
    base.pop("economics", None)
    base["tasks"] = []
    for task in root["tasks"]:
        item = _object(task, {"task_id", "role", "outcome", "scope", "depends_on",
                              "criterion_ids", "return_condition", "escalation_trigger",
                              "limits", "context_policy"})
        try:
            checked_policy = _context_policy.validate(item["context_policy"])
        except _context_policy.ContextPolicyError as exc:
            raise WorkspacePlanError("plan_context_policy") from exc
        stripped = copy.deepcopy(item)
        stripped.pop("context_policy", None)
        base["tasks"].append(stripped)
    checked = _validate_plan_v1(base)
    normalized_tasks = []
    for task, original in zip(checked["tasks"], root["tasks"]):
        row = copy.deepcopy(task)
        row["context_policy"] = _context_policy.validate(original["context_policy"])
        normalized_tasks.append(row)
    return {"schema": PLAN_V2_SCHEMA, "goal": checked["goal"],
            "tasks": normalized_tasks,
            "operator_message_targets": checked["operator_message_targets"],
            "economics": economics}


def validate_plan(value: Any) -> dict:
    if type(value) is dict and value.get("schema") == PLAN_V2_SCHEMA:
        return _validate_plan_v2(value)
    return _validate_plan_v1(value)


def _aggregate_chunks(schema: str, aggregate_id: str, workspace_id: str, run_id: str,
                      entries: list[tuple[str, dict]]) -> list[tuple[str, bytes, list[dict]]]:
    """Pack entries into bounded aggregate sources.

    The content store deliberately caps one object at 4096 bytes.  Keep a
    safety margin for future wrapper metadata while retaining one logical
    aggregate identity and total-entry count across chunks.
    """
    if not entries:
        _fail("plan_empty_aggregate")
    chunks: list[list[tuple[str, dict]]] = []
    current: list[tuple[str, dict]] = []
    for entry in entries:
        candidate = current + [entry]
        probe = {"schema": schema, "aggregate_id": aggregate_id,
                 "workspace_id": workspace_id, "run_id": run_id,
                 "entry_count": len(entries), "chunk_index": 0,
                 "chunk_count": max(1, len(entries)),
                 "entries": [{"entry_id": key, "value": value} for key, value in candidate]}
        if current and len(_canonical(probe)) > 3500:
            chunks.append(current)
            current = [entry]
        else:
            current = candidate
    if current:
        chunks.append(current)
    result = []
    chunk_count = len(chunks)
    for index, chunk in enumerate(chunks):
        root = {"schema": schema, "aggregate_id": aggregate_id,
                "workspace_id": workspace_id, "run_id": run_id,
                "entry_count": len(entries), "chunk_index": index,
                "chunk_count": chunk_count,
                "entries": [{"entry_id": key, "value": value} for key, value in chunk]}
        raw = _canonical(root)
        if len(raw) > 3500:
            _fail("plan_aggregate_capacity")
        result.append((aggregate_id, raw, root["entries"]))
    return result


def compile_plan(value: dict, *, raw_sha256: str, workspace_id: str, run_id: str,
                 operator_id: str = "local-operator",
                 now_ms: int) -> dict:
    """Compile validated input into inert runtime records and private sources."""
    if isinstance(value, dict) and value.get("schema") == PLAN_V2_SCHEMA:
        return compile_plan_v2(value, raw_sha256=raw_sha256, workspace_id=workspace_id,
                                run_id=run_id, operator_id=operator_id, now_ms=now_ms)
    checked = validate_plan(value)
    protocol._id(workspace_id)
    protocol._id(run_id)
    protocol._id(operator_id)
    if type(raw_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", raw_sha256):
        _fail("plan_source_digest")
    base = {"protocol": protocol.PROTOCOL, "workspace_id": workspace_id, "run_id": run_id}
    goal_input = checked["goal"]
    goal = {**base, "kind": "goal", "goal_id": goal_input["goal_id"], "revision": 1,
            "objective": goal_input["objective"], "criteria": goal_input["criteria"],
            "constraints": goal_input["constraints"], "active_priority": goal_input["active_priority"],
            "unresolved_decisions": goal_input["unresolved_decisions"]}
    sources = []
    lanes = []
    task_requests = {}
    authority_entries = []
    for task in checked["tasks"]:
        intent = {"plan_sha256": raw_sha256, "task_id": task["task_id"],
                  "goal_id": goal["goal_id"], "outcome": task["outcome"], "scope": task["scope"],
                  "depends_on": task["depends_on"], "criteria": task["criterion_ids"]}
        request_sha256 = hashlib.sha256(b"summon.workspace.task-request/v1\0" + _canonical(intent)).hexdigest()
        task_requests[task["task_id"]] = request_sha256
        authority_source = {"schema": "summon.workspace.task-authority/v1",
                            "workspace_id": workspace_id, "run_id": run_id,
                            "goal_id": goal["goal_id"], "goal_revision": 1,
                            "task_id": task["task_id"], "operation": "definition.context.queue",
                            "execution_authorized": False, "revoked": False}
        authority_entries.append((task["task_id"], authority_source))
        authority_ref = {"id": "plan-auth-" + hashlib.sha256(
            (raw_sha256 + "\0" + task["task_id"]).encode("ascii")).hexdigest()[:32],
            "sha256": ""}
        lane = {**base, "kind": "lane", "goal_id": goal["goal_id"], "goal_revision": 1,
                "task_id": task["task_id"], "lane_id": task["task_id"],
                "request_sha256": request_sha256, "role": task["role"],
                "outcome": task["outcome"], "scope": task["scope"],
                "authority_ref": authority_ref,
                "budget": {"max_duration_ms": task["limits"]["max_duration_ms"],
                            "max_attempts": task["limits"]["max_attempts"],
                            "max_context_bytes": task["limits"]["max_context_bytes"]},
                "criterion_ids": task["criterion_ids"],
                "return_condition": task["return_condition"],
                "escalation_trigger": task["escalation_trigger"],
                "depends_on": list(task["depends_on"])}
        lanes.append(lane)

    authority_pairs = [("auth-" + hashlib.sha256(
        (raw_sha256 + "\0" + task_id).encode("ascii")).hexdigest()[:32], value)
                       for task_id, value in authority_entries]
    authority_ref_by_task = {}
    for _aggregate, authority_raw, entries in _aggregate_chunks(
            "summon.workspace.plan-authority/v1", "authority", workspace_id, run_id, authority_pairs):
        authority_sha = hashlib.sha256(authority_raw).hexdigest()
        for entry in entries:
            task_id = entry["value"]["task_id"]
            full_id = "blob-" + authority_sha[:32] + "." + entry["entry_id"]
            authority_ref_by_task[task_id] = {"id": full_id, "sha256": authority_sha}
            sources.append({"reference": authority_ref_by_task[task_id],
                            "category": "observation", "task_id": task_id, "raw": authority_raw})
    for lane in lanes:
        lane["authority_ref"] = authority_ref_by_task[lane["task_id"]]
    for lane in lanes:
        protocol.validate_record(lane)

    # One bounded operator destination per target keeps the recipient binding
    # explicit while three aggregate blobs keep the ContentStore below its
    # object and per-payload limits even at the 16-task/16-target maximum.
    expiry = now_ms + SCOPE_TTL_MS
    grant_entries = []
    scope_entries = []
    target_records = []
    for index, target in enumerate(checked["operator_message_targets"]):
        task_id = target["task_id"]
        recipient_id = "operator-" + hashlib.sha256(
            (workspace_id + "\0" + run_id + "\0" + task_id + "\0" + target["target"]).encode("utf-8")).hexdigest()[:24]
        grant_source = {"schema": "summon.workspace.operator-message-grant/v1",
                        "workspace_id": workspace_id, "run_id": run_id,
                        "task_id": task_id, "goal_revision": 1,
                        "recipient": {"instance_id": recipient_id, "epoch": 1},
                        "revoked": False, "operation": "operator.message.receive",
                        "execution_authorized": False}
        grant_id = "grant-" + hashlib.sha256(
            (raw_sha256 + "\0grant\0" + str(index)).encode("ascii")).hexdigest()[:32]
        grant_ref = {"id": grant_id, "sha256": ""}
        grant_entries.append((grant_id, grant_source))
        scope = {"revision": 1, "expires_at_ms": expiry, "revoked": False,
                 "operation": "operator.message.send", "goal_id": goal["goal_id"],
                 "destination_task_id": task_id,
                 "recipient": {"instance_id": recipient_id, "epoch": 1},
                 "delivery_grant_ref": grant_ref, "target": target["target"]}
        scope_source = {"schema": "summon.workspace.operator-scope/v1",
                        "workspace_id": workspace_id, "run_id": run_id,
                        "operator_id": operator_id, "scope": scope}
        scope_id = "scope-" + hashlib.sha256(
            (raw_sha256 + "\0scope\0" + str(index)).encode("ascii")).hexdigest()[:32]
        scope_ref = {"id": scope_id, "sha256": ""}
        scope_entries.append((scope_id, scope_source))
        target_records.append({"target": target["target"], "task_id": task_id,
                               "recipient_instance_id": recipient_id,
                               "send_scope_ref": scope_ref})

    grant_ref_by_suffix = {}
    for _aggregate, grant_raw, entries in _aggregate_chunks(
            "summon.workspace.plan-grants/v1", "grants", workspace_id, run_id, grant_entries):
        grant_sha = hashlib.sha256(grant_raw).hexdigest()
        for entry in entries:
            entry_id, value = entry["entry_id"], entry["value"]
            full_id = "blob-" + grant_sha[:32] + "." + entry_id
            grant_ref_by_suffix[entry_id] = {"id": full_id, "sha256": grant_sha}
            sources.append({"reference": grant_ref_by_suffix[entry_id],
                            "category": "grant", "task_id": value["task_id"], "raw": grant_raw})
    # Scope values contain the final grant references; rebuild the grant refs
    # now that their aggregate digest is known.
    for _entry_id, scope_source in scope_entries:
        grant_ref = scope_source["scope"]["delivery_grant_ref"]
        resolved_grant = grant_ref_by_suffix[grant_ref["id"]]
        grant_ref.update(resolved_grant)
    for _aggregate, scope_raw, entries in _aggregate_chunks(
            "summon.workspace.plan-scopes/v1", "scopes", workspace_id, run_id, scope_entries):
        scope_sha = hashlib.sha256(scope_raw).hexdigest()
        for entry in entries:
            entry_id, value = entry["entry_id"], entry["value"]
            full_id = "blob-" + scope_sha[:32] + "." + entry_id
            scope_ref = {"id": full_id, "sha256": scope_sha}
            sources.append({"reference": scope_ref,
                            "category": "grant", "task_id": value["scope"]["destination_task_id"],
                            "raw": scope_raw})
            target = next(item for item in target_records
                          if item["send_scope_ref"]["id"] == entry_id)
            target["send_scope_ref"] = scope_ref
    result = {"schema": PLAN_SCHEMA, "raw_sha256": raw_sha256, "workspace_id": workspace_id,
            "run_id": run_id, "goal": goal, "lanes": lanes, "task_requests": task_requests,
            "operator_targets": target_records, "sources": sources,
            "operator_id": operator_id,
            "operator_expiry_ms": expiry,
            "tasks": [{"task_id": item["task_id"], "request_sha256": task_requests[item["task_id"]]}
                      for item in checked["tasks"]]}
    result["bindings"] = binding_descriptors(result)
    return result


def compile_plan_v2(value: dict, *, raw_sha256: str, workspace_id: str, run_id: str,
                    operator_id: str = "local-operator", now_ms: int) -> dict:
    """Compile a v2 plan with explicit policy/fence facts.

    The existing content/grant compiler remains the sole writer.  This adapter
    converts its inert v1 projection to protocol/v2 records and adds the
    bounded economics reservation requirement; it does not consume a provider
    budget or claim that capacity has been durably reserved.
    """
    checked = _validate_plan_v2(value)
    stripped = copy.deepcopy(checked)
    stripped["schema"] = PLAN_SCHEMA
    stripped.pop("economics", None)
    for task in stripped["tasks"]:
        task.pop("context_policy", None)
    base = compile_plan(stripped, raw_sha256=raw_sha256, workspace_id=workspace_id,
                        run_id=run_id, operator_id=operator_id, now_ms=now_ms)
    goal = dict(base["goal"])
    goal["protocol"] = protocol.PROTOCOL_V2
    lanes = []
    policy_by_task = {task["task_id"]: task["context_policy"] for task in checked["tasks"]}
    for lane in base["lanes"]:
        row = dict(lane)
        row["protocol"] = protocol.PROTOCOL_V2
        row["context_policy"] = copy.deepcopy(policy_by_task[row["task_id"]])
        lanes.append(row)
    protocol.validate_goal_plan_v2(goal, lanes, economics=checked["economics"])
    result = dict(base)
    result.update({"schema": PLAN_V2_SCHEMA, "goal": goal, "lanes": lanes,
                   "economics": copy.deepcopy(checked["economics"]),
                   "economics_reservation": {
                       "feature": ECONOMICS_FEATURE,
                       "max_records": checked["economics"]["max_records"],
                       "max_settlement_bytes": checked["economics"]["max_settlement_bytes"],
                       "reserved": False,
                       "requires_sole_writer_admission": True,
                   }})
    result["bindings"] = binding_descriptors(result)
    return result


def binding_descriptors(compiled: dict) -> dict:
    """Return canonical provider-neutral coordinator binding descriptors."""
    if type(compiled) is not dict:
        _fail("plan_binding")
    workspace_id, run_id = compiled.get("workspace_id"), compiled.get("run_id")
    operator_id = compiled.get("operator_id", "local-operator")
    _id(workspace_id); _id(run_id); _id(operator_id)
    targets = compiled.get("operator_targets")
    if type(targets) is not list or not 1 <= len(targets) <= MAX_TARGETS:
        _fail("plan_binding")
    recipients = []
    for target in targets:
        if (type(target) is not dict or
                set(target) != {"target", "task_id", "recipient_instance_id", "send_scope_ref"}):
            _fail("plan_binding")
        _id(target["target"]); _id(target["task_id"])
        _id(target["recipient_instance_id"])
        recipients.append({"target": target["target"], "task_id": target["task_id"],
                           "instance_id": target["recipient_instance_id"], "epoch": 1})
    recipients.sort(key=lambda item: (item["task_id"], item["target"], item["instance_id"]))
    project = {"schema": PROJECT_BINDING_SCHEMA, "workspace_id": workspace_id,
               "run_id": run_id, "kind": "unbound", "access": "none",
               "execution_authorized": False}
    roster = {"schema": ROSTER_BINDING_SCHEMA, "workspace_id": workspace_id,
              "run_id": run_id, "operator_id": operator_id,
              "kind": "dormant-operator-message-destinations",
              "execution_authorized": False, "recipients": recipients}
    return {
        "project": project,
        "roster": roster,
        "project_sha256": hashlib.sha256(_canonical(project)).hexdigest(),
        "roster_sha256": hashlib.sha256(_canonical(roster)).hexdigest(),
        "project_kind": project["kind"],
        "roster_kind": roster["kind"],
    }


def compiled_plan_digest(compiled: dict) -> str:
    """Hash the durable plan binding, excluding raw source bytes.

    The digest intentionally includes every semantic record and every source
    reference/digest that the plan creator registers.  Reopen can recompute it
    from the copied plan plus the host-bound expiry without trusting a mutable
    journal field or an unbounded source body.
    """
    if type(compiled) is not dict:
        _fail("plan_compiled_digest")
    sources = compiled.get("sources")
    if type(sources) is not list:
        _fail("plan_compiled_digest")
    normalized_sources = []
    for source in sources:
        if type(source) is not dict or not isinstance(source.get("reference"), dict):
            _fail("plan_compiled_digest")
        reference = source["reference"]
        if set(reference) != {"id", "sha256"}:
            _fail("plan_compiled_digest")
        raw = source.get("raw")
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            _fail("plan_compiled_digest")
        normalized_sources.append({"reference": copy.deepcopy(reference),
                                   "category": source.get("category"),
                                   "task_id": source.get("task_id")})
    value = {
        "schema": compiled.get("schema"),
        "raw_sha256": compiled.get("raw_sha256"),
        "workspace_id": compiled.get("workspace_id"),
        "run_id": compiled.get("run_id"),
        "goal": compiled.get("goal"),
        "lanes": compiled.get("lanes"),
        "task_requests": compiled.get("task_requests"),
        "operator_targets": compiled.get("operator_targets"),
        "operator_id": compiled.get("operator_id"),
        "operator_expiry_ms": compiled.get("operator_expiry_ms"),
        "bindings": binding_descriptors(compiled),
        "tasks": compiled.get("tasks"),
        "sources": normalized_sources,
    }
    return hashlib.sha256(_canonical(value)).hexdigest()


__all__ = ["PLAN_SCHEMA", "PLAN_V2_SCHEMA", "ECONOMICS_SCHEMA", "ECONOMICS_FEATURE",
           "PLAN_HOST_SCHEMA", "PLAN_FILE_NAME", "PROJECT_BINDING_SCHEMA",
           "ROSTER_BINDING_SCHEMA", "WorkspacePlanError",
           "parse_plan_bytes", "read_plan_file", "validate_plan", "compile_plan",
           "compile_plan_v2", "binding_descriptors", "compiled_plan_digest"]
