"""Provider-free tests for the canonical workspace admission helper."""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_admission as a
import _workspace_protocol as p
import _workspace_state as ws
from test_workspace_protocol import delivery, message, ref, effect_fixture
from test_workspace_state import Fixture, add_hold


def envelope(kind, payload):
    return {"event": kind, "protocol": p.PROTOCOL, "workspace_id": "workspace",
            "run_id": "run", "operation_key": "operation", "expected_revision": 1,
            "payload": payload}


def delivery_event(state):
    return envelope("workspace_delivery_advanced", {
        "delivery": delivery(state), "evidence": {}, "supported_ack_levels": [],
    })


def coordinator_state(*, recipient_epoch=2):
    claim = {"task_id": "main-2", "claim_id": "claim", "worker_id": "worker-b",
             "attempt": 1, "lease_generation": 3, "lease_expires_at_ms": 1000,
             "request_sha256": "d" * 64, "status": "active",
             "cancel_requested": False, "cancel_acknowledged": False, "renewals": 0}
    return {
        "tasks": {"main-2": {"task_id": "main-2", "request_sha256": "d" * 64,
                               "attempts": ["claim"], "terminal": None}},
        "claims": {"claim": claim},
        "workers": {"worker-b": {"worker_id": "worker-b", "worker_instance_id": "worker-b",
                                   "epoch": recipient_epoch}},
    }


def admission_fixture(*, side_hold=False, recipient_epoch=2):
    fixture = Fixture()
    fixture.admit()
    fixture.register(ref("queued-proof"), delivery_id="delivery")
    fixture.add("workspace_delivery_advanced", {"delivery": delivery("queued"),
                  "evidence": {"durable_admission": ref("queued-proof")},
                  "supported_ack_levels": []})
    if side_hold:
        add_hold(fixture, ["side-1"])
    selection = copy.deepcopy(delivery("included_in_attempt")["selection"])
    selection["request_sha256"] = "d" * 64
    claim = coordinator_state(recipient_epoch=recipient_epoch)["claims"]["claim"]
    payload = {
        "claim": claim,
        "selection": selection,
        "recipient": {"instance_id": "worker-b", "epoch": recipient_epoch},
        "grant_ref": ref("grant"),
        "evidence_by_delivery": {"delivery": {key: ref("grant") if key == "current_grant" else ref(key) for key in (
            "current_grant", "recipient_owner_fence", "capacity_reservation",
            "physical_attempt_reservation", "selection_record", "whole_message_fit")}},
        "affected_task_ids": ["side-1"] if side_hold else [],
        "holds": ws.unresolved_holds(fixture.state, "side-1") if side_hold else [],
        "goal_revision": 1,
        "selected_delivery_ids": ["delivery"],
        "supported_ack_levels": [],
    }
    for key, reference in payload["evidence_by_delivery"]["delivery"].items():
        if key != "current_grant":
            fixture.register(reference, category="fence" if key in {
                "recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation"
            } else "event", delivery_id="delivery")
    grant = {"grant_ref": ref("grant"), "task_id": "main-2", "goal_revision": 1,
             "recipient": {"instance_id": "worker-b", "epoch": recipient_epoch}, "revoked": False}
    return fixture, envelope(a.ATOMIC_TURN_EVENT, payload), coordinator_state(recipient_epoch=recipient_epoch), grant


def test_message_admission_uses_canonical_records_and_reserves_disposition():
    event = envelope("workspace_message_admitted", {"message": message(), "delivery": delivery()})
    assert a.classify_event(event) == a.OBLIGATION_ACCEPTED
    assert a.obligations_for_event(event) == (
        a.AdmissionObligation("accepted", "message", True),
        a.AdmissionObligation("disposition", "delivery", False),
    )


def test_toy_message_record_and_unknown_event_are_refused():
    with pytest.raises(a.WorkspaceAdmissionError):
        a.obligations_for_event(envelope("workspace_message_admitted", {
            "message": {"message_id": "message"}, "delivery": {"delivery_id": "delivery", "state": "accepted"}}))
    with pytest.raises(a.WorkspaceAdmissionError):
        a.classify_event(envelope("invented_event", {}))


@pytest.mark.parametrize(("state", "classification", "outstanding"), [
    ("submission_started", "submission_started", {"submission_started", "disposition"}),
    ("submitted", "disposition", {"disposition"}),
    ("held_for_recovery", "disposition", {"disposition"}),
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_delivery_obligations_preserve_nonterminal_settlement(state, classification, outstanding):
    event = delivery_event(state)
    assert a.classify_event(event) == classification
    names = {item.kind for item in a.outstanding_obligations(state, "delivery")}
    assert names == outstanding


def test_terminal_state_requires_complete_certainty_before_refund():
    unknown = {"contact": "none", "spend": "unknown", "cleanup": "complete"}
    assert a.outstanding_obligations("not_submitted", "delivery", certainty=unknown)
    clean = {"contact": "none", "spend": "not_incurred", "cleanup": "complete"}
    assert a.outstanding_obligations("not_submitted", "delivery", certainty=clean) == ()
    assert a.outstanding_obligations("acknowledged", "delivery", certainty=clean,
                                     inherited_uncertainty=["spend"])


def test_linked_delivery_is_canonical_and_keeps_a_new_disposition_obligation():
    child = delivery("queued")
    child.update(delivery_id="child", parent_delivery_id="delivery")
    event = envelope("workspace_delivery_linked", {"delivery": child, "evidence": {}})
    assert a.classify_event(event) == a.OBLIGATION_DISPOSITION
    assert {item.kind for item in a.obligations_for_event(event)} == {"accepted", "disposition"}


def test_side_hold_is_explicit_at_grant_and_target_can_still_proceed():
    fixture, event, coordinator, grant = admission_fixture(side_hold=True)
    result = a.validate_atomic_claim_selection(event, coordinator_state=coordinator,
                                               workspace_state=fixture.state, grant_state=grant)
    assert result["payload"]["affected_task_ids"] == ["side-1"]
    assert result["payload"]["holds"][0]["source_task_id"] == "side-1"
    assert a.validate_grant_obligations({"grant_id": "grant", "goal_id": "goal",
        "goal_revision": 1, "task_id": "main-2", "affected_task_ids": ["side-1"],
        "holds": result["payload"]["holds"]}, state=fixture.state)


@pytest.mark.parametrize("mutation", ["omitted_holds", "revoked", "stale_revision", "wrong_recipient_epoch"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_atomic_admission_refuses_stale_or_incomplete_authority(mutation):
    fixture, event, coordinator, grant = admission_fixture(side_hold=True)
    if mutation == "omitted_holds":
        event["payload"]["affected_task_ids"] = []
        event["payload"]["holds"] = []
    elif mutation == "revoked":
        grant["revoked"] = True
    elif mutation == "stale_revision":
        grant["goal_revision"] = 2
    else:
        event["payload"]["recipient"]["epoch"] = 3
        grant["recipient"]["epoch"] = 3
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(event, coordinator_state=coordinator,
                                          workspace_state=fixture.state, grant_state=grant)


def test_atomic_admission_requires_all_current_snapshots():
    fixture, event, coordinator, grant = admission_fixture()
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(event, coordinator_state=None,
                                          workspace_state=fixture.state, grant_state=grant)
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(event, coordinator_state=coordinator,
                                          workspace_state=fixture.state, grant_state=None)


@pytest.mark.parametrize("mutation", ["shared", "missing_map", "extra_map", "missing_role",
                                      "wrong_grant", "wrong_delivery_scope", "wrong_category"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007'])
def test_atomic_requires_complete_scoped_per_delivery_proofs(mutation):
    fixture, event, coordinator, grant = admission_fixture()
    payload = event["payload"]
    maps = payload["evidence_by_delivery"]
    if mutation == "shared":
        payload["evidence"] = payload.pop("evidence_by_delivery")["delivery"]
    elif mutation == "missing_map":
        maps.clear()
    elif mutation == "extra_map":
        maps["other"] = copy.deepcopy(maps["delivery"])
    elif mutation == "missing_role":
        maps["delivery"].pop("physical_attempt_reservation")
    elif mutation == "wrong_grant":
        maps["delivery"]["current_grant"] = ref("other-grant")
    else:
        item = fixture.state["evidence"]["recipient_owner_fence"]
        item["delivery_id" if mutation == "wrong_delivery_scope" else "category"] = (
            "other" if mutation == "wrong_delivery_scope" else "artifact")
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(event, coordinator_state=coordinator,
                                          workspace_state=fixture.state, grant_state=grant)


def test_atomic_admission_binds_claim_selection_recipient_and_delivery():
    fixture, event, coordinator, grant = admission_fixture()
    result = a.validate_atomic_claim_selection(event, coordinator_state=coordinator,
                                               workspace_state=fixture.state, grant_state=grant)
    assert a.classify_event(event) == a.OBLIGATION_SUBMISSION_STARTED
    obligations = a.obligations_for_event(event, coordinator_state=coordinator,
                                          workspace_state=fixture.state, grant_state=grant)
    assert {item.kind for item in obligations} == {"submission_started", "disposition"}
    assert result == event


def test_atomic_admission_binds_optional_economics_reservation_to_exact_selection():
    fixture, event, coordinator, grant = admission_fixture()
    selection = event["payload"]["selection"]
    selection_sha256 = hashlib.sha256(a._canonical_event_bytes({
        "task_id": selection["task_id"],
        "attempt": selection["attempt"],
        "request_sha256": selection["request_sha256"],
        "context_sha256": selection["context_sha256"],
        "selected_message_ids": selection["selected_message_ids"],
    })).hexdigest()
    event["payload"]["economics_reservation"] = {
        "schema": a.ECONOMICS_RESERVATION_SCHEMA,
        "feature": a.ECONOMICS_FEATURE,
        "status": "reserved",
        "max_records": 1,
        "max_settlement_bytes": 4096,
        "claim_id": event["payload"]["claim"]["claim_id"],
        "selection_sha256": selection_sha256,
        "attempt_id": "1" * 32,
        "payload_sha256": selection["context_sha256"],
        "material_sha256": "b" * 64,
        "policy_sha256": "b" * 64,
    }
    result = a.validate_atomic_claim_selection(
        event, coordinator_state=coordinator,
        workspace_state=fixture.state, grant_state=grant)
    assert result["payload"]["economics_reservation"]["selection_sha256"] == selection_sha256

    mutated = copy.deepcopy(event)
    mutated["payload"]["selection"]["selected_message_ids"] = ["other-message"]
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(
            mutated, coordinator_state=coordinator,
            workspace_state=fixture.state, grant_state=grant)


@pytest.mark.parametrize("mutation", ["claim", "selection", "bounds"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003'])
def test_atomic_admission_refuses_forged_economics_reservation(mutation):
    fixture, event, coordinator, grant = admission_fixture()
    selection = event["payload"]["selection"]
    selection_sha256 = hashlib.sha256(a._canonical_event_bytes({
        "task_id": selection["task_id"],
        "attempt": selection["attempt"],
        "request_sha256": selection["request_sha256"],
        "context_sha256": selection["context_sha256"],
        "selected_message_ids": selection["selected_message_ids"],
    })).hexdigest()
    reservation = {
        "schema": a.ECONOMICS_RESERVATION_SCHEMA,
        "feature": a.ECONOMICS_FEATURE,
        "status": "reserved",
        "max_records": 1,
        "max_settlement_bytes": 4096,
        "claim_id": event["payload"]["claim"]["claim_id"],
        "selection_sha256": selection_sha256,
        "attempt_id": "1" * 32,
        "payload_sha256": selection["context_sha256"],
        "material_sha256": "b" * 64,
        "policy_sha256": "b" * 64,
    }
    if mutation == "claim":
        reservation["claim_id"] = "other-claim"
    elif mutation == "selection":
        reservation["selection_sha256"] = "0" * 64
    else:
        reservation["max_settlement_bytes"] = a.protocol.MIN_ECONOMICS_SETTLEMENT_BYTES - 1
    event["payload"]["economics_reservation"] = reservation
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_atomic_claim_selection(
            event, coordinator_state=coordinator,
            workspace_state=fixture.state, grant_state=grant)


@pytest.mark.parametrize("selected_delivery_ids", [[], ["delivery", "delivery"], ["delivery", {}]], ids=['p005_case_001', 'p005_case_002', 'p005_case_003'])
def test_atomic_projection_rejects_empty_duplicate_or_non_id_delivery_selection(selected_delivery_ids):
    fixture, event, _coordinator, _grant = admission_fixture()
    fixture.status("main-2", "claimed")
    event["expected_revision"] = fixture.state["revision"]
    event["operation_key"] = "atomic-invalid-delivery-selection"
    event["payload"]["selected_delivery_ids"] = selected_delivery_ids
    with pytest.raises(ws.WorkspaceStateError, match="invalid atomic delivery selection"):
        ws.apply_event(fixture.state, event, fixture.view)


def test_target_hold_blocks_even_if_caller_claims_empty_side_set():
    fixture, _event, _coordinator, _grant = admission_fixture(side_hold=False)
    add_hold(fixture, ["main-2"], name="main-hold")
    with pytest.raises(a.WorkspaceAdmissionError):
        a.validate_affected_holds(state=fixture.state, target_task_id="main-2",
                                  affected_task_ids=[], holds=[], goal_revision=1)


def test_explicit_effect_candidate_classification_is_bounded_and_keeps_qualification():
    _before, after, evidence, _scope, _cleanup = effect_fixture()
    event = envelope("workspace_effects_resolved", {"delivery": after, "evidence": evidence,
        "resolution_kind": p.EFFECT_RESOLUTION_KIND, "qualification": "simulated"})
    assert a.classify_event(event) == a.OBLIGATION_DISPOSITION
    assert a.obligations_for_event(event) == (a.AdmissionObligation("disposition", "delivery", True),)
    for mutation in ("qualification", "scope", "same_ref", "unknown_field"):
        bad = copy.deepcopy(event)
        if mutation == "qualification":
            bad["payload"]["qualification"] = "provider_verified"
        elif mutation == "scope":
            bad["run_id"] = "other"
        elif mutation == "same_ref":
            bad["payload"]["evidence"]["owned_child_cleanup"] = evidence["fixed_execution_scope"]
        else:
            bad["payload"]["approved"] = True
        with pytest.raises(a.WorkspaceAdmissionError):
            a.classify_event(bad)
