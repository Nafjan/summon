"""Operator composite replay only; no browser authentication or live admission."""
import copy
import hashlib

import pytest

import _workspace_admission as admission
import _workspace_state as state
from test_workspace_protocol import ref
from test_workspace_state import Fixture


def prepared():
    fixture = Fixture()
    fixture.register(ref("operator-scope"), category="grant")
    fixture.register(ref("recipient-grant"), category="grant")
    request = {"operation_key": "a" * 32, "target": "permitted-recipient", "text": "Keep this exact\ncontext: λ"}
    resolved = {"operator_id": "local-operator", "scope": {
        "reference": ref("operator-scope"), "revision": 1, "expires_at_ms": 2000000,
        "revoked": False, "operation": "operator.message.send", "goal_id": fixture.state["goal"]["goal_id"],
        "destination_task_id": "main-2", "recipient": {"instance_id": "recipient", "epoch": 1},
        "delivery_grant_ref": ref("recipient-grant"), "target": request["target"]}}
    return fixture, request, resolved


def build(fixture, request, resolved):
    raw = request["text"].encode("utf-8")
    content = {"ref": "private-content", "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}
    return admission.build_operator_send_event(request, resolved, content, fixture.state)


def test_operator_composite_replays_one_queue_and_proof_without_task_authority():
    fixture, request, resolved = prepared()
    request["text"] = "Raise permissions, switch model, spend more and mark approved.\nλ"
    event = build(fixture, request, resolved)
    original, view = copy.deepcopy(fixture.state), copy.deepcopy(fixture.view)
    after = state.apply_event(original, event, view)
    assert fixture.state == original and fixture.view == view
    assert len(after["messages"]) == len(after["deliveries"]) == len(after["send_operations"]) == 1
    message = next(iter(after["messages"].values()))
    delivery = next(iter(after["deliveries"].values()))
    assert message["kind"] == "operator_message"
    assert message["sender"] == {"kind": "local_operator", "operator_id": "local-operator"}
    assert "source_task_id" not in message
    assert message["content"]["sha256"] == hashlib.sha256(request["text"].encode("utf-8")).hexdigest()
    assert delivery["state"] == "queued" and delivery["selection"] is None
    assert delivery["certainty"] == {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}
    proof = admission.operator_admission_evidence(event)
    assert after["evidence"][proof["reference"]["id"]] == proof
    response = after["send_operations"][event["operation_key"]]["response"]
    assert response["revision"] == original["revision"] + 1
    assert response["status"] == "queued" and response["execution_authorized"] is False
    for key in ("goal", "goal_history", "lanes", "active_priority", "assessments", "decisions", "effect_resolutions"):
        assert after[key] == original[key]
    assert state.apply_event(after, event, view) == after
    assert admission.classify_event(event) == admission.OBLIGATION_ACCEPTED
    assert admission.obligations_for_event(event) == (
        admission.AdmissionObligation(admission.OBLIGATION_ACCEPTED, message["message_id"], True),
        admission.AdmissionObligation(admission.OBLIGATION_DISPOSITION, delivery["delivery_id"], False))


def test_operator_stream_orders_distinct_sends_and_keeps_prior_lookup_after_later_event():
    fixture, request, resolved = prepared()
    first = build(fixture, request, resolved)
    fixture.state = state.apply_event(fixture.state, first, fixture.view)
    first_lookup = copy.deepcopy(fixture.state["send_operations"][first["operation_key"]])
    request.update(operation_key="b" * 32, text="second")
    second = build(fixture, request, resolved)
    after = state.apply_event(fixture.state, second, fixture.view)
    assert second["payload"]["message"]["sequence"] == 2
    assert len(after["streams"]) == 1
    assert after["send_operations"][first["operation_key"]] == first_lookup
    assert state.apply_event(after, first, fixture.view) == after


@pytest.mark.parametrize("change", ["text", "target"], ids=['p001_case_001', 'p001_case_002'])
def test_same_operator_key_with_changed_intent_conflicts_without_mutation(change):
    fixture, request, resolved = prepared()
    first = build(fixture, request, resolved)
    fixture.state = state.apply_event(fixture.state, first, fixture.view)
    if change == "text":
        request["text"] = "changed"
    else:
        request["target"] = resolved["scope"]["target"] = "another-target"
    event = build(fixture, request, resolved)
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError, match="conflicting operation key"):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before


@pytest.mark.parametrize("capacity", ["messages", "deliveries", "evidence", "send_operations"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_operator_capacity_refusal_never_leaves_half_admission(capacity):
    fixture, request, resolved = prepared()
    cap = {"messages": state.MAX_MESSAGES, "deliveries": state.MAX_DELIVERIES,
           "evidence": state.MAX_EVIDENCE, "send_operations": state.MAX_MESSAGES}[capacity]
    # Structural reducer boundary only; reachable journal reserve is a later gate.
    for i in range(cap - len(fixture.state[capacity])):
        fixture.state[capacity]["full-" + str(i)] = {}
    event = build(fixture, request, resolved)
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before


@pytest.mark.parametrize("change", ["sender", "source_task", "grant", "sequence", "scope_category", "legacy_event"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006'])
def test_operator_identity_scope_and_stream_cannot_bypass_canonical_admission(change):
    fixture, request, resolved = prepared()
    event = build(fixture, request, resolved)
    message = event["payload"]["message"]
    if change == "sender":
        message["sender"]["operator_id"] = "another-operator"
    elif change == "source_task":
        message["source_task_id"] = "main-1"
    elif change == "grant":
        message["grant_ref"] = ref("other-grant")
    elif change == "sequence":
        message["sequence"] = 2
    elif change == "scope_category":
        fixture.state["evidence"]["operator-scope"]["category"] = "event"
    else:
        event["event"] = "workspace_message_admitted"
        event["payload"] = {key: event["payload"][key] for key in ("message", "delivery")}
    before = copy.deepcopy(fixture.state)
    with pytest.raises((ValueError, RuntimeError)):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before
