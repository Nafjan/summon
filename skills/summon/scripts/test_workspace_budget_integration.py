"""Provider-free coordinator seam for explicit workspace-budget preflight."""
from __future__ import annotations

import pytest

import _workspace_budget as budget
import _swarm_coordinator as swarm
from test_workspace_worker_send import Harness, NOW, request


def policy(**overrides):
    value = {
        "schema": budget.POLICY_SCHEMA,
        "rate_window_ms": 60_000,
        "rate_limit": 4,
        "rate_bytes_limit": 1_000,
        "max_message_bytes": 128,
        "journal_capacity_bytes": 100_000,
        "control_reserve_bytes": 1_000,
        "recovery_reserve_bytes": 1_000,
        "execution_slot_limit": 2,
        "stream_limits": {"alpha": {"message_limit": 4, "byte_limit": 1_000}},
        "oversize_head_action": "hold",
    }
    value.update(overrides)
    return value


def snapshot(*, rate_events=None):
    return {
        "schema": budget.SNAPSHOT_SCHEMA,
        "now_ms": NOW,
        "journal_used_bytes": 0,
        "active_execution_slots": 0,
        "rate_events": list(rate_events or []),
        "pending_messages": [{
            "message_id": "m1", "stream_id": "alpha", "sequence": 1,
            "bytes": len(request()["content"].encode("utf-8")), "state": "queued",
        }],
        "stream_usage": {"alpha": {"messages": 0, "bytes": 0}},
    }


def budget_request():
    return {
        "schema": budget.REQUEST_SCHEMA,
        "selected_message_ids": ["m1"],
        "journal_bytes": len(request()["content"].encode("utf-8")),
        "execution_slots_requested": 0,
        "kind": "message",
    }


def send_with_budget(h, *, policy_value=None, snapshot_value=None,
                     request_value=None, decision=None, require=False):
    return h.coordinator.admit_worker_message(
        request(), resolve_source=h.resolve, prepare_content=h.prepare,
        budget_policy=policy_value, budget_snapshot=snapshot_value,
        budget_request=request_value, budget_decision=decision, require_budget=require)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: 2_000_000_000.25)
    return Harness(tmp_path)


def test_observational_budget_preflight_is_explicit_and_replay_is_idempotent(h):
    before = h.journals()
    first = h.coordinator.workspace_budget_preflight(
        policy=policy(), snapshot=snapshot(), request=budget_request())
    assert first["phase"] == "observational"
    assert first["status"] == "accepted"
    assert first["execution_status"] == "not_requested"
    assert first["provider_contacted"] is False
    assert first["state_mutated"] is False
    replay = h.coordinator.workspace_budget_preflight(
        policy=policy(), snapshot=snapshot(), request=budget_request())
    assert replay == first
    assert h.journals() == before and h.content_calls == 0


def test_budget_binding_refusal_happens_before_source_or_content_or_journal(h):
    before = h.journals()
    with pytest.raises(swarm.SwarmBudgetRefusal) as caught:
        send_with_budget(
            h, policy_value=policy(), snapshot_value=snapshot(),
            request_value=budget_request(), require=True)
    assert caught.value.reason == "budget_binding_required"
    assert caught.value.result is None
    assert h.journals() == before and h.source_calls == 0 and h.content_calls == 0


def test_budget_preflight_requires_all_authoritative_facts(h):
    before = h.journals()
    with pytest.raises(swarm.SwarmBudgetRefusal) as caught:
        send_with_budget(h, policy_value=policy(), require=True)
    assert caught.value.reason == "budget_policy_required"
    assert caught.value.result is None
    assert h.journals() == before and h.source_calls == 0 and h.content_calls == 0


def test_budget_preflight_rejects_malformed_fact_without_mutation(h):
    before = h.journals()
    with pytest.raises(swarm.SwarmBudgetRefusal) as caught:
        h.coordinator.workspace_budget_preflight(
            policy=policy(), snapshot=snapshot(),
            request={"schema": budget.REQUEST_SCHEMA})
    assert caught.value.reason == "budget_input_invalid"
    assert h.journals() == before and h.source_calls == 0 and h.content_calls == 0


def test_budget_refusal_result_is_provider_free_and_detached(h):
    result = h.coordinator.workspace_budget_preflight(
        policy=policy(rate_limit=1),
        snapshot=snapshot(rate_events=[{"at_ms": NOW, "messages": 1, "bytes": 0}]),
        request=budget_request())
    assert result["status"] == "blocked"
    assert result["reason"] == "rate_window_exhausted"
    assert result["provider_contacted"] is False
    assert result["state_mutated"] is False


def test_bound_budget_decision_matches_request_and_current_journal(h):
    pol, snap, req = policy(), snapshot(), budget_request()
    decision = h.coordinator.workspace_budget_decision(
        policy=pol, snapshot=snap, request=req)
    before = h.journals()
    result = send_with_budget(h, policy_value=pol, snapshot_value=snap,
                              request_value=req, decision=decision, require=True)
    assert result["status"] == result["delivery_state"] == "queued"
    assert result["budget_preflight"]["phase"] == "bound"
    assert result["budget_preflight"]["provider_contacted"] is False
    assert h.journals() != before


@pytest.mark.parametrize("change", ["policy", "snapshot", "request", "journal"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_bound_budget_decision_refuses_stale_identity_or_revision_before_callbacks(h, change):
    pol, snap, req = policy(), snapshot(), budget_request()
    decision = h.coordinator.workspace_budget_decision(
        policy=pol, snapshot=snap, request=req)
    if change == "policy":
        pol = {**pol, "rate_limit": pol["rate_limit"] + 1}
    elif change == "snapshot":
        snap = {**snap, "journal_used_bytes": 1}
    elif change == "request":
        req = {**req, "journal_bytes": req["journal_bytes"] + 1}
    else:
        # A durable, unrelated workspace event advances the exact revision and
        # makes the previously bound decision stale without provider contact.
        h.append_payload("workspace_evidence_registered", {
            "reference": {"id": "artifact-route", "sha256": "a" * 64},
            "category": "artifact", "task_id": "main-1"})
    before = h.journals()
    with pytest.raises(swarm.SwarmBudgetRefusal) as caught:
        send_with_budget(h, policy_value=pol, snapshot_value=snap,
                         request_value=req, decision=decision, require=True)
    assert caught.value.reason == "budget_binding_required"
    assert h.journals() == before
    assert h.source_calls == 0 and h.content_calls == 0


@pytest.mark.parametrize(
    ("journal_used", "expected_reason"),
    [(6, "control_reserve_protected"), (8, "recovery_reserve_protected")],
 ids=['p002_case_001', 'p002_case_002'])
def test_bound_budget_reserve_refusal_is_enforced_at_worker_adapter_boundary(
        h, journal_used, expected_reason):
    pol = policy(journal_capacity_bytes=10, control_reserve_bytes=2,
                 recovery_reserve_bytes=2)
    snap = snapshot()
    snap["journal_used_bytes"] = journal_used
    snap["pending_messages"] = [{
        "message_id": "m1", "stream_id": "alpha", "sequence": 1,
        "bytes": 1, "state": "queued",
    }]
    req = {**budget_request(), "journal_bytes": 1}
    decision = h.coordinator.workspace_budget_decision(
        policy=pol, snapshot=snap, request=req)
    assert decision["result"]["reason"] == expected_reason
    before = h.journals()
    with pytest.raises(swarm.SwarmBudgetRefusal) as caught:
        send_with_budget(h, policy_value=pol, snapshot_value=snap,
                         request_value=req, decision=decision, require=True)
    assert caught.value.reason == "budget_decision_blocked"
    assert caught.value.result["reason"] == expected_reason
    assert h.journals() == before
    assert h.source_calls == 0 and h.content_calls == 0
