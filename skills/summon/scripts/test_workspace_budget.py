"""Provider-free tests for the versioned workspace budget evaluator."""
from __future__ import annotations

import copy

import pytest

import _workspace_budget as budget


def policy(**overrides):
    value = {
        "schema": budget.POLICY_SCHEMA,
        "rate_window_ms": 1000,
        "rate_limit": 3,
        "rate_bytes_limit": 24,
        "max_message_bytes": 8,
        "journal_capacity_bytes": 20,
        "control_reserve_bytes": 4,
        "recovery_reserve_bytes": 4,
        "execution_slot_limit": 1,
        "stream_limits": {
            "alpha": {"message_limit": 3, "byte_limit": 16},
            "beta": {"message_limit": 3, "byte_limit": 16},
        },
        "oversize_head_action": "hold",
    }
    value.update(overrides)
    return value


def snapshot(*, pending=None, rate_events=None, journal=0, active=0, usage=None):
    return {
        "schema": budget.SNAPSHOT_SCHEMA,
        "now_ms": 10_000,
        "journal_used_bytes": journal,
        "active_execution_slots": active,
        "rate_events": rate_events or [],
        "pending_messages": pending or [],
        "stream_usage": usage or {},
    }


def message(message_id, stream="alpha", sequence=1, size=4, state="queued"):
    return {"message_id": message_id, "stream_id": stream, "sequence": sequence,
            "bytes": size, "state": state}


def request(*ids, journal=None, slots=0, kind="message"):
    return {"schema": budget.REQUEST_SCHEMA, "selected_message_ids": list(ids),
            "journal_bytes": journal if journal is not None else 4 * len(ids),
            "execution_slots_requested": slots, "kind": kind}


def test_policy_snapshot_and_request_reject_unknown_or_boolean_shapes():
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_policy({**policy(), "unknown": True})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_policy({**policy(), "rate_limit": True})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_policy({**policy(), "control_reserve_bytes": 17})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_policy({**policy(), "oversize_head_action": []})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_snapshot({**snapshot(), "active_execution_slots": False})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_request({**request("m1"), "execution_slots_requested": True})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_request({**request("m1"), "kind": []})
    with pytest.raises(budget.WorkspaceBudgetError):
        budget.validate_request({**request("m1", kind="control")})


@pytest.mark.parametrize("event", [
    [{"at_ms": 9_500, "messages": 3, "bytes": 8}],
    [{"at_ms": 9_500, "messages": 0, "bytes": 22}],
], ids=['p001_case_001', 'p001_case_002'])
def test_rate_window_refusal_is_typed_and_does_not_mutate_inputs(event):
    pol = policy()
    snap = snapshot(pending=[message("m1")], rate_events=event)
    req = request("m1")
    before = (copy.deepcopy(pol), copy.deepcopy(snap), copy.deepcopy(req))
    result = budget.evaluate(pol, snap, req)
    assert result["status"] == "blocked"
    assert result["reason"] == "rate_window_exhausted"
    assert result["selected_message_ids"] == []
    assert result["execution_status"] == "not_run"
    assert result["provider_contacted"] is False
    assert result["state_mutated"] is False
    assert (pol, snap, req) == before


def test_oversized_head_blocks_same_stream_but_not_independent_stream():
    snap = snapshot(pending=[
        message("a1", "alpha", 1, 9), message("a2", "alpha", 2, 1),
        message("b1", "beta", 1, 1),
    ])
    blocked = budget.evaluate(policy(), snap, request("a2", journal=1))
    assert blocked["reason"] == "oversize_head_blocks_stream"
    accepted = budget.evaluate(policy(), snap, request("b1", journal=1))
    assert accepted["status"] == "accepted"
    assert accepted["selected_message_ids"] == ["b1"]
    assert accepted["execution_status"] == "not_requested"


def test_oversized_head_action_can_refuse_or_hold_the_head():
    snap = snapshot(pending=[message("a1", size=9)])
    held = budget.evaluate(policy(), snap, request("a1", journal=9))
    assert held["reason"] == "oversize_head_held"
    refused = budget.evaluate(policy(oversize_head_action="refuse"), snap, request("a1", journal=9))
    assert refused["reason"] == "oversize_head_refused"


def test_control_reserve_is_consumable_without_spending_recovery_reserve():
    pol = policy(journal_capacity_bytes=10, control_reserve_bytes=2, recovery_reserve_bytes=2)
    snap = snapshot(pending=[message("m1", size=1)], journal=6)
    ordinary = budget.evaluate(pol, snap, request("m1", journal=1))
    assert ordinary["reason"] == "control_reserve_protected"
    control = budget.evaluate(pol, snap, request(journal=2, kind="control"))
    assert control["status"] == "accepted"
    assert control["execution_status"] == "not_requested"
    after_recovery = budget.evaluate(pol, snapshot(journal=8), request(journal=1, kind="control"))
    assert after_recovery["reason"] == "recovery_reserve_protected"


def test_stream_prefix_and_budget_refusals_preserve_independent_ordering():
    snap = snapshot(pending=[message("a1", "alpha", 1), message("a2", "alpha", 2),
                             message("b1", "beta", 1)],
                    usage={"alpha": {"messages": 3, "bytes": 12}})
    skipped = budget.evaluate(policy(), snap, request("a2", journal=4))
    assert skipped["reason"] == "stream_prefix_required"
    exhausted = budget.evaluate(policy(), snap, request("a1", journal=4))
    assert exhausted["reason"] == "stream_budget_exhausted"
    independent = budget.evaluate(policy(), snap, request("b1", journal=4))
    assert independent["status"] == "accepted"
    assert independent["selected_message_ids"] == ["b1"]
    assert [item["message_id"] for item in snap["pending_messages"]] == ["a1", "a2", "b1"]


def test_message_acceptance_is_separate_from_execution_slot_deferral():
    snap = snapshot(pending=[message("m1")], active=1)
    result = budget.evaluate(policy(execution_slot_limit=1), snap, request("m1", slots=1))
    assert result["status"] == "accepted"
    assert result["message_status"] == "accepted"
    assert result["execution_status"] == "deferred"
    assert result["selected_message_ids"] == ["m1"]
    assert result["deferred_message_ids"] == ["m1"]
    assert budget.result_sha256(result) == budget.result_sha256(result)
    assert result["provider_contacted"] is False
    assert result["state_mutated"] is False


def test_inbox_offer_cannot_request_worker_slots_or_mix_with_task_messages():
    snap = snapshot(pending=[
        {"message_id": "inbox-1", "stream_id": "inbox", "sequence": 1,
         "bytes": 3, "state": "offered", "source": "inbox"},
        {"message_id": "task-1", "stream_id": "task", "sequence": 1,
         "bytes": 3, "state": "queued"},
    ])
    with pytest.raises(budget.WorkspaceBudgetError, match="zero-slot"):
        budget.evaluate(policy(execution_slot_limit=1), snap,
                        request("inbox-1", slots=1))
    with pytest.raises(budget.WorkspaceBudgetError, match="zero-slot"):
        budget.evaluate(policy(execution_slot_limit=1), snap,
                        request("inbox-1", "task-1", slots=0, journal=6))


def test_control_cannot_request_execution_slots():
    result = budget.evaluate(policy(), snapshot(), request(journal=1, slots=1, kind="control"))
    assert result["status"] == "blocked"
    assert result["reason"] == "control_execution_slots_invalid"
    assert result["execution_status"] == "not_run"
