import hashlib
import json

import pytest

import _context_policy as context_policy
import _workspace_plan as plan
import _workspace_protocol as protocol


def _v2():
    policy = context_policy.make("off", policy_id="task-context")
    return {
        "schema": plan.PLAN_V2_SCHEMA,
        "goal": {
            "goal_id": "ship-next", "objective": "Build the next bounded slice.",
            "criteria": [{"criterion_id": "tests", "description": "Tests pass.",
                           "evidence_requirement": "A provider-free receipt."}],
            "constraints": ["No providers."], "active_priority": "main",
            "unresolved_decisions": [],
        },
        "tasks": [{
            "task_id": "main", "role": "main", "outcome": "Return a result.",
            "scope": "Provider-free source work.", "depends_on": [],
            "criterion_ids": ["tests"], "return_condition": "Tests pass.",
            "escalation_trigger": "Boundary fails.",
            "limits": {"max_duration_ms": 300000, "max_attempts": 1,
                       "max_context_bytes": 4096},
            "context_policy": policy,
        }],
        "operator_message_targets": [{"target": "main-target", "task_id": "main"}],
        "economics": {"schema": plan.ECONOMICS_SCHEMA,
                       "feature": plan.ECONOMICS_FEATURE, "enabled": True,
                       "max_records": 64, "max_settlement_bytes": 16384},
    }


def test_v2_requires_explicit_policy_and_economics_fence():
    value = _v2()
    checked = plan.validate_plan(value)
    assert checked["schema"] == plan.PLAN_V2_SCHEMA
    assert checked["tasks"][0]["context_policy"]["mode"] == "off"
    with pytest.raises(plan.WorkspacePlanError):
        plan.validate_plan({**value, "economics": {"enabled": True}})
    missing = json.loads(json.dumps(value))
    missing["tasks"][0].pop("context_policy")
    with pytest.raises(plan.WorkspacePlanError):
        plan.validate_plan(missing)


def test_v1_remains_unchanged_and_v2_compiles_to_v2_protocol_records():
    value = _v2()
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    compiled = plan.compile_plan(value, raw_sha256=hashlib.sha256(raw).hexdigest(),
                                 workspace_id="workspace", run_id="run-a", now_ms=1_000_000)
    assert compiled["schema"] == plan.PLAN_V2_SCHEMA
    assert compiled["economics_reservation"]["reserved"] is False
    protocol.validate_goal_plan_v2(compiled["goal"], compiled["lanes"],
                                   economics=compiled["economics"])
    assert compiled["lanes"][0]["context_policy"]["origin"] == "operator"
    legacy = dict(value)
    legacy["schema"] = plan.PLAN_SCHEMA
    legacy["tasks"] = [dict(value["tasks"][0])]
    legacy["tasks"][0].pop("context_policy")
    legacy.pop("economics")
    assert plan.validate_plan(legacy)["schema"] == plan.PLAN_SCHEMA


def test_v2_policy_revision_and_mode_are_bound_per_task():
    value = _v2()
    value["tasks"][0]["context_policy"] = context_policy.make(
        "safe", policy_id="task-context")
    checked = plan.validate_plan(value)
    assert checked["tasks"][0]["context_policy"]["implementation"] == "typed-context-compiler-v1"
