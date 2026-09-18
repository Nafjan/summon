"""Provider-free retain-held-context journal slice."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_admission as admission
import _workspace_state as state
from test_workspace_protocol import ref
from test_workspace_state import Fixture


def _held_fixture() -> Fixture:
    fixture = Fixture()
    fixture.admit()
    fixture.advance("queued")
    fixture.status("main-2", "claimed")
    fixture.advance("included_in_attempt")
    fixture.advance("submission_started")
    fixture.advance("held_for_recovery")
    fixture.register(ref("retain-request"), delivery_id="delivery")
    fixture.register(ref("retain-decision"), delivery_id="delivery")
    return fixture


def _retain_event(fixture: Fixture, *, operation_key: str = "retain-op",
                  reason: str = "awaiting_evidence", task_id: str = "main-2",
                  delivery_id: str = "delivery", request_ref=None,
                  decision_ref=None) -> dict:
    return admission.build_operator_disposition_event(
        fixture.state,
        operation_key=operation_key,
        delivery_id=delivery_id,
        task_id=task_id,
        request_ref=request_ref or ref("retain-request"),
        decision_ref=decision_ref or ref("retain-decision"),
        reason=reason,
    )


def _disposition_proof(*, role: str, operation_key: str = "a" * 32,
                       delivery_id: str = "delivery", task_id: str = "main-2",
                       reason: str = "awaiting_evidence") -> dict:
    return {"schema": admission.OPERATOR_DISPOSITION_PROOF_SCHEMA,
            "role": role, "operation_key": operation_key,
            "action": "retain_held_context", "delivery_id": delivery_id,
            "task_id": task_id, "reason": reason,
            "authority": "installed_operator_policy"}


@pytest.mark.parametrize("reason", sorted(admission.OPERATOR_DISPOSITION_REASONS))
def test_retain_is_audit_only_and_replays_without_delivery_mutation(reason):
    fixture = _held_fixture()
    before = copy.deepcopy(fixture.state)
    before_reservations = state.settlement_record_counts(before)
    delivery_before = copy.deepcopy(before["deliveries"]["delivery"])
    event = _retain_event(fixture, operation_key="retain-" + reason, reason=reason)

    assert admission.canonical_operator_disposition_event(event) == event
    assert admission.classify_event(event) == admission.OBLIGATION_DISPOSITION
    assert admission.obligations_for_event(event) == ()

    after = state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before
    assert after["revision"] == before["revision"] + 1
    assert after["deliveries"]["delivery"] == delivery_before
    assert after["messages"] == before["messages"]
    assert after["evidence"] == before["evidence"]
    assert state.settlement_record_counts(after) == before_reservations
    assert set(after["operations"]) == set(before["operations"]) | {event["operation_key"]}

    replay_steps = fixture.steps + [(copy.deepcopy(event), copy.deepcopy(fixture.view))]
    assert state.replay(replay_steps) == after


def test_retain_operation_key_is_idempotent_and_conflicts_are_refused():
    fixture = _held_fixture()
    event = _retain_event(fixture)
    after = state.apply_event(fixture.state, event, fixture.view)

    assert state.apply_event(after, event, fixture.view) == after
    conflicting = copy.deepcopy(event)
    conflicting["payload"]["reason"] = "operator_hold"
    with pytest.raises(state.WorkspaceStateError, match="conflicting operation key"):
        state.apply_event(after, conflicting, fixture.view)
    assert after["deliveries"]["delivery"]["state"] == "held_for_recovery"


def test_retain_requires_held_delivery_and_exact_scoped_ordinary_event_refs():
    fixture = Fixture()
    fixture.admit()
    fixture.advance("queued")
    fixture.register(ref("retain-request"), delivery_id="delivery")
    fixture.register(ref("retain-decision"), delivery_id="delivery")
    event = _retain_event(fixture)
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError, match="requires a held delivery"):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before

    fixture = _held_fixture()
    fixture.register(ref("retain-observation"), category="observation", delivery_id="delivery")
    held_revision = fixture.state["revision"]
    for mutation, message in (
        ("wrong_task", "task binding differs"),
        ("unknown_delivery", "unknown delivery"),
        ("missing_ref", "dangling or conflicting evidence"),
        ("wrong_category", "ordinary bound event"),
    ):
        event = _retain_event(fixture, operation_key="retain-" + mutation)
        if mutation == "wrong_task":
            event["payload"]["task_id"] = "side-1"
        elif mutation == "unknown_delivery":
            event["payload"]["delivery_id"] = "other"
        elif mutation == "missing_ref":
            event["payload"]["request_ref"] = ref("missing")
        else:
            event["payload"]["request_ref"] = ref("retain-observation")
        with pytest.raises(state.WorkspaceStateError, match=message):
            state.apply_event(fixture.state, event, fixture.view)
        assert fixture.state["revision"] == held_revision


@pytest.mark.parametrize("mutation", ["unknown_reason", "free_text", "same_ref", "schema", "action"])
def test_retain_payload_has_finite_closed_shape(mutation):
    fixture = _held_fixture()
    event = _retain_event(fixture)
    if mutation == "unknown_reason":
        event["payload"]["reason"] = "because the operator said so"
    elif mutation == "free_text":
        event["payload"]["note"] = "unbounded explanation"
    elif mutation == "same_ref":
        event["payload"]["decision_ref"] = copy.deepcopy(event["payload"]["request_ref"])
    elif mutation == "schema":
        event["payload"]["schema"] = "summon.workspace.operator-disposition/v2"
    else:
        event["payload"]["action"] = "replace_linked_context"
    with pytest.raises(admission.WorkspaceAdmissionError):
        admission.canonical_operator_disposition_event(event)


def test_retain_event_boundary_and_capacity_are_explicit_and_atomic(monkeypatch):
    import _swarm_coordinator as coordinator
    import _workspace_runtime as runtime

    assert admission.OPERATOR_DISPOSITION_EVENT in admission.SUPPORTED_EVENTS
    assert admission.OPERATOR_DISPOSITION_EVENT in runtime.EVENT_CLASSES
    assert admission.OPERATOR_DISPOSITION_EVENT in coordinator._WORKSPACE_EVENT_CLASSES

    fixture = _held_fixture()
    event = _retain_event(fixture)
    before = copy.deepcopy(fixture.state)
    monkeypatch.setattr(state, "MAX_EVENTS", len(before["operations"]))
    with pytest.raises(state.WorkspaceStateError, match="workspace event bound exhausted"):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before


def test_typed_retain_proofs_bind_roles_operation_reason_and_scope():
    request = _disposition_proof(role="request")
    decision = _disposition_proof(role="decision")
    assert admission.canonical_operator_disposition_proof(
        request, role="request", operation_key="a" * 32,
        delivery_id="delivery", task_id="main-2", reason="awaiting_evidence") == request
    assert admission.canonical_operator_disposition_proof(
        decision, role="decision", operation_key="a" * 32,
        delivery_id="delivery", task_id="main-2", reason="awaiting_evidence") == decision
    unrelated_request = {"kind": "unrelated-event", "role": "request"}
    unrelated_decision = {"kind": "unrelated-event", "role": "decision"}
    with pytest.raises(admission.WorkspaceAdmissionError):
        admission.canonical_operator_disposition_proof(
            unrelated_request, role="request", operation_key="a" * 32,
            delivery_id="delivery", task_id="main-2", reason="awaiting_evidence")
    with pytest.raises(admission.WorkspaceAdmissionError):
        admission.canonical_operator_disposition_proof(
            unrelated_decision, role="decision", operation_key="a" * 32,
            delivery_id="delivery", task_id="main-2", reason="awaiting_evidence")
    for mutation in (
        {"operation_key": "b" * 32},
        {"reason": "operator_hold"},
    ):
        changed = {**request, **mutation}
        with pytest.raises(admission.WorkspaceAdmissionError):
            admission.canonical_operator_disposition_proof(
                changed, role="request", operation_key="a" * 32,
                delivery_id="delivery", task_id="main-2", reason="awaiting_evidence")
    with pytest.raises(admission.WorkspaceAdmissionError):
        admission.canonical_operator_disposition_proof(
            decision, role="request", operation_key="a" * 32,
            delivery_id="delivery", task_id="main-2", reason="awaiting_evidence")


def test_unknown_future_retain_event_refuses_without_mutation():
    fixture = _held_fixture()
    event = _retain_event(fixture)
    future = copy.deepcopy(event)
    future["event"] = admission.OPERATOR_DISPOSITION_EVENT + "_v2"
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError, match="unsupported workspace event"):
        state.apply_event(fixture.state, future, fixture.view)
    assert fixture.state == before
