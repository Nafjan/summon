import hashlib
import json
from pathlib import Path

import pytest

import _workspace_plan as plan
import _workspace_protocol as protocol
from _fleet_approval import _secure_private_root


def _plan(**overrides):
    value = {
        "schema": "summon.workspace.plan/v1",
        "goal": {
            "goal_id": "ship-next",
            "objective": "Build and verify the next bounded release slice.",
            "criteria": [{
                "criterion_id": "tests",
                "description": "Provider-free tests cover the slice.",
                "evidence_requirement": "A focused and broad test receipt.",
            }],
            "constraints": ["No providers or credentials."],
            "active_priority": "main",
            "unresolved_decisions": [],
        },
        "tasks": [{
            "task_id": "main",
            "role": "main",
            "outcome": "Return a tested implementation.",
            "scope": "The provider-free workspace slice.",
            "depends_on": [],
            "criterion_ids": ["tests"],
            "return_condition": "Tests pass and the report is complete.",
            "escalation_trigger": "A boundary or evidence check fails.",
            "limits": {"max_duration_ms": 300000, "max_attempts": 1, "max_context_bytes": 4096},
        }],
        "operator_message_targets": [{"target": "main-target", "task_id": "main"}],
    }
    value.update(overrides)
    return value


def test_parse_is_strict_and_returns_a_detached_value():
    value = _plan()
    parsed = plan.parse_plan_bytes(json.dumps(value).encode())
    assert parsed == value
    parsed["goal"]["criteria"][0]["description"] = "changed"
    assert value["goal"]["criteria"][0]["description"] != "changed"


@pytest.mark.parametrize("mutator", [
    lambda value: value.update(extra=True),
    lambda value: value["goal"].update(provider="claude"),
    lambda value: value["tasks"][0].update(permission="yolo"),
    lambda value: value["tasks"][0]["limits"].update(max_spend=10),
    lambda value: value["operator_message_targets"][0].update(model="gpt"),
], ids=['case0', 'case1', 'case2', 'case3', 'case4'])
def test_unknown_or_authority_fields_are_rejected(mutator):
    value = _plan()
    mutator(value)
    with pytest.raises(plan.WorkspacePlanError):
        plan.validate_plan(value)


def test_duplicate_json_keys_are_rejected():
    raw = b'{"schema":"summon.workspace.plan/v1","schema":"summon.workspace.plan/v1"}'
    with pytest.raises(plan.WorkspacePlanError, match="duplicate"):
        plan.parse_plan_bytes(raw)


def test_dependencies_must_exist_and_be_acyclic():
    value = _plan()
    value["tasks"].append({
        "task_id": "review", "role": "review", "outcome": "Review.", "scope": "Review.",
        "depends_on": ["main"], "criterion_ids": ["tests"],
        "return_condition": "Return findings.", "escalation_trigger": "Mismatch.",
        "limits": {"max_duration_ms": 300000, "max_attempts": 1, "max_context_bytes": 4096},
    })
    assert plan.validate_plan(value)["tasks"][1]["depends_on"] == ["main"]
    value["tasks"][0]["depends_on"] = ["review"]
    with pytest.raises(plan.WorkspacePlanError, match="cycle"):
        plan.validate_plan(value)


def test_active_priority_must_be_a_root():
    value = _plan()
    value["tasks"][0]["depends_on"] = ["later"]
    value["tasks"].append({
        "task_id": "later", "role": "validation", "outcome": "Validate.", "scope": "Validate.",
        "depends_on": [], "criterion_ids": ["tests"],
        "return_condition": "Return evidence.", "escalation_trigger": "Mismatch.",
        "limits": {"max_duration_ms": 300000, "max_attempts": 1, "max_context_bytes": 4096},
    })
    with pytest.raises(plan.WorkspacePlanError, match="priority"):
        plan.validate_plan(value)


def test_compile_generates_inert_authority_and_message_grants():
    value = _plan()
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    compiled = plan.compile_plan(value, raw_sha256=hashlib.sha256(raw).hexdigest(),
                                 workspace_id="workspace", run_id="run-a", now_ms=1_000_000)
    protocol.validate_goal_plan(compiled["goal"], compiled["lanes"])
    assert compiled["operator_targets"][0]["target"] == "main-target"
    assert len(compiled["sources"]) == 3
    assert all(source["category"] in {"observation", "grant"} for source in compiled["sources"])
    grant = next(item for item in compiled["sources"] if item["category"] == "grant"
                 and json.loads(item["raw"])["aggregate_id"] == "grants")
    grant_value = json.loads(grant["raw"])
    entry = grant_value["entries"][0]["value"]
    assert entry["operation"] == "operator.message.receive"
    assert entry["execution_authorized"] is False
    assert not any(key in entry for key in ("provider", "model", "permission", "spend"))


def test_read_plan_file_is_stable_and_does_not_expose_path(tmp_path):
    source = tmp_path / "plan.json"
    _secure_private_root(str(tmp_path))
    raw = json.dumps(_plan(), ensure_ascii=False).encode()
    source.write_bytes(raw)
    parsed, loaded, digest = plan.read_plan_file(str(source))
    assert parsed["schema"] == "summon.workspace.plan/v1"
    assert loaded == raw
    assert digest == hashlib.sha256(raw).hexdigest()


def test_plan_file_size_and_encoding_fail_closed(tmp_path):
    source = tmp_path / "bad-plan.json"
    _secure_private_root(str(tmp_path))
    source.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(plan.WorkspacePlanError, match="encoding"):
        plan.read_plan_file(str(source))
    source.write_bytes(b"{}" + b"x" * (plan.MAX_PLAN_BYTES + 1))
    with pytest.raises(plan.WorkspacePlanError, match="changed"):
        plan.read_plan_file(str(source))


def test_sixteen_tasks_and_targets_stay_within_aggregate_capacity():
    value = _plan()
    value["tasks"] = []
    value["operator_message_targets"] = []
    value["goal"]["active_priority"] = "task-00"
    for index in range(16):
        task_id = f"task-{index:02d}"
        value["tasks"].append({
            "task_id": task_id, "role": "main" if index == 0 else "review",
            "outcome": "Return a bounded result.", "scope": "Provider-free source review.",
            "depends_on": [] if index == 0 else ["task-00"], "criterion_ids": ["tests"],
            "return_condition": "Return evidence.", "escalation_trigger": "Mismatch.",
            "limits": {"max_duration_ms": 300000, "max_attempts": 1, "max_context_bytes": 4096},
        })
        value["operator_message_targets"].append({"target": f"target-{index:02d}", "task_id": task_id})
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    compiled = plan.compile_plan(value, raw_sha256=hashlib.sha256(raw).hexdigest(),
                                 workspace_id="workspace", run_id="run-a", now_ms=1_000_000)
    assert len(compiled["sources"]) == 48
    assert len({hashlib.sha256(source["raw"]).hexdigest() for source in compiled["sources"]}) <= 32
    assert max(len(source["raw"]) for source in compiled["sources"]) <= 4096
    assert all(len(source["reference"]["id"]) <= 128 for source in compiled["sources"])
    protocol.validate_goal_plan(compiled["goal"], compiled["lanes"])

