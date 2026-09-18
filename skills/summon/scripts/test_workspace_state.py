"""No-I/O replay fixtures. Supplied coordinator views do not prove live concurrency."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_state as s
import _workspace_protocol as p
from test_workspace_protocol import goal, lane, message, delivery, assessment, ref, evidence_for


class Fixture:
    def __init__(self):
        self.state = None
        self.steps = []
        self.view = {"run_id": "run", "tasks": [{"task_id": task, "request_sha256": "d" * 64,
                      "status": "pending", "attempts": 0, "active_claim": None}
                     for task in ("main-1", "side-1", "main-2")]}
        self.add(s.FEATURE_EVENT, {"feature": "goal_messages", "version": 1})
        self.add("workspace_goal_defined", {"goal": goal()})
        for name in ("main-1", "side-1", "main-2"):
            record = lane()
            record.update(task_id=name, lane_id=name, role="investigation" if name == "side-1" else "main")
            self.add("workspace_lane_defined", {"lane": record})

    def event(self, kind, payload, key=None):
        return {"event": kind, "protocol": p.PROTOCOL, "workspace_id": "workspace", "run_id": "run",
                "operation_key": key or "op-" + str(len(self.steps)), "expected_revision": self.state["revision"] if self.state else 0,
                "payload": copy.deepcopy(payload)}

    def add(self, kind, payload):
        event = self.event(kind, payload)
        self.state = s.apply_event(self.state, event, self.view)
        self.steps.append((copy.deepcopy(event), copy.deepcopy(self.view)))
        return event

    def status(self, task_id, status):
        task = next(t for t in self.view["tasks"] if t["task_id"] == task_id)
        task.update(status=status, attempts=0 if status == "pending" else 1,
                    active_claim={"claim_id": "claim", "lease_generation": 3} if status == "claimed" else None)

    def register(self, reference, task_id="main-2", category="event", delivery_id=None):
        payload = {"reference": reference, "task_id": task_id, "category": category}
        if delivery_id:
            payload["delivery_id"] = delivery_id
        return self.add("workspace_evidence_registered", payload)

    def assess(self, task_id, name):
        self.register(ref(name + "-proof"), task_id)
        a = assessment()
        a.update(assessment_id=name, task_id=task_id, lane_id=task_id,
                 evidence=[ref(name + "-proof")], next_decision="continue")
        a["criterion_results"][0]["evidence_ids"] = [name + "-proof"]
        return self.add("workspace_assessed", {"assessment": a})

    def admit(self):
        self.register(ref("grant"), category="grant")
        return self.add("workspace_message_admitted", {"message": message(), "delivery": delivery()})

    def proofs(self, target, delivery_id="delivery"):
        proof = evidence_for(target)
        for key, value in proof.items():
            if type(value) is dict:
                category = {"current_grant": "grant", "recipient_owner_fence": "fence",
                            "capacity_reservation": "fence", "physical_attempt_reservation": "fence",
                            "adapter_receipt": "adapter_receipt", "ack_receipt": "adapter_receipt",
                            "cleanup_evidence": "fence", "expiry_observation": "observation",
                            "authorized_cancellation": "grant"}.get(key, "event")
                self.register(value, category=category,
                              delivery_id=None if key == "current_grant" else delivery_id)
        return proof

    def advance(self, target):
        after = copy.deepcopy(self.state["deliveries"]["delivery"])
        template = delivery(target)
        after.update(state=target, reason=template["reason"], ack_level=template["ack_level"])
        if target == "included_in_attempt":
            after["selection"] = template["selection"]
            after["selection"]["request_sha256"] = "d" * 64
        if target in {"submission_started", "submitted", "not_submitted"}:
            after["certainty"] = template["certainty"]
        return self.add("workspace_delivery_advanced", {"delivery": after, "evidence": self.proofs(target), "supported_ack_levels": ["recipient_received"]})


def test_two_iterations_replay_goal_focus_and_concurrent_side_observation():
    f = Fixture()
    f.status("main-1", "completed")
    f.status("side-1", "claimed")
    first = f.assess("main-1", "assessment-1")
    decision = f.add("workspace_next_lane_selected", {"decision_id": "decision-1", "goal_revision": 1,
                     "assessment_id": "assessment-1", "from_task_id": "main-1", "to_task_id": "main-2"})
    assert f.state["active_priority"] == "main-2"
    assert f.state["assessments"]["assessment-1"]["observed_task_status"] == {"main-1": "completed", "side-1": "claimed", "main-2": "pending"}
    f.status("main-2", "claimed")
    f.status("main-2", "completed")
    f.status("side-1", "completed")
    f.assess("main-2", "assessment-2")
    assert s.replay(f.steps) == f.state
    assert s.apply_event(f.state, first, f.view) == f.state
    assert s.apply_event(f.state, decision, f.view) == f.state
    projected = s.project(f.state, f.view)
    assert projected["goal"]["goal_id"] == "goal"
    assert len(projected["assessments"]) == 2 and len(projected["decisions"]) == 1
    assert all(row["status"] == "completed" for row in projected["lanes"])
    # Assessments never own a task terminal; changing the external view changes
    # only current projection, not earlier historical observations.
    f.status("side-1", "indeterminate")
    assert next(x for x in s.project(f.state, f.view)["lanes"] if x["task_id"] == "side-1")["status"] == "indeterminate"
    assert f.state["assessments"]["assessment-1"]["observed_task_status"]["side-1"] == "claimed"


def test_delivery_replay_full_lifecycle_and_duplicate_suppression():
    f = Fixture()
    admitted = f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    for state in ("included_in_attempt", "submission_started", "submitted", "acknowledged"):
        f.advance(state)
    assert s.replay(f.steps) == f.state
    assert s.apply_event(f.state, admitted, f.view) == f.state
    assert len(f.state["messages"]) == len(f.state["deliveries"]) == 1
    assert f.state["deliveries"]["delivery"]["certainty"]["spend"] == "unknown"


def test_held_dead_letter_link_retains_parent_and_uncertainty():
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    f.advance("included_in_attempt")
    f.advance("submission_started")
    f.advance("held_for_recovery")
    f.advance("dead_lettered")
    parent = copy.deepcopy(f.state["deliveries"]["delivery"])
    child = delivery("queued")
    child.update(delivery_id="child", parent_delivery_id="delivery", grant_ref=ref("fresh"), inherited_uncertainty=["contact", "spend", "cleanup"])
    proof = {"authenticated_disposition": ref("dispose-child"), "remaining_authority": ref("remaining"),
             "fresh_attempt_grant": ref("fresh"), "physical_fence": ref("fence")}
    for key, value in proof.items():
        f.register(value, category="grant" if key in {"remaining_authority", "fresh_attempt_grant"} else "fence" if key == "physical_fence" else "event",
                   delivery_id="delivery" if key == "authenticated_disposition" else None)
    linked = f.add("workspace_delivery_linked", {"delivery": child, "evidence": proof})
    assert f.state["deliveries"]["delivery"] == parent
    assert s.replay(f.steps) == f.state
    assert s.apply_event(f.state, linked, f.view) == f.state
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_delivery_linked", {"delivery": dict(child, delivery_id="another"), "evidence": proof})


def test_native_turn_collision_holds_delivery_without_new_attempt_or_submission():
    """An authenticated collision observation holds work and preserves uncertainty."""
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    f.advance("included_in_attempt")
    before = copy.deepcopy(f.state)
    collision = ref("native-turn-collision")
    f.register(collision, category="observation", delivery_id="delivery")
    held = copy.deepcopy(f.state["deliveries"]["delivery"])
    held.update(state="held_for_recovery", reason="native_turn_collision",
                certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
    event = f.add("workspace_delivery_advanced", {
        "delivery": held,
        "evidence": {"hold_observation": collision},
        "supported_ack_levels": [],
    })
    recorded = f.state["deliveries"]["delivery"]
    assert recorded["state"] == "held_for_recovery"
    assert recorded["reason"] == "native_turn_collision"
    assert recorded["selection"] == before["deliveries"]["delivery"]["selection"]
    assert recorded["certainty"] == {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
    projected = s.project(f.state, f.view)
    projected_delivery = next(item for item in projected["deliveries"] if item["delivery_id"] == "delivery")
    assert projected_delivery["state"] == "held_for_recovery"
    assert projected_delivery["reason"] == "native_turn_collision"
    assert f.view["tasks"][2]["attempts"] == 1 and f.view["tasks"][2]["active_claim"] is not None
    assert not any(item[0]["event"] in {"workspace_submission_started", "workspace_delivery_submitted"}
                   for item in f.steps)
    assert s.apply_event(f.state, event, f.view) == f.state
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_delivery_advanced", {
            "delivery": dict(held, state="submission_started", reason=None,
                              certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}),
            "evidence": f.proofs("submission_started"),
            "supported_ack_levels": [],
        })


@pytest.mark.parametrize("mutation", ["missing_assessment", "wrong_source", "stale_goal", "reopen", "duplicate_target"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_decisions_refuse_dangling_stale_reopened_or_duplicate_targets(mutation):
    f = Fixture()
    f.assess("main-1", "a")
    payload = {"decision_id": "decision", "goal_revision": 1, "assessment_id": "a", "from_task_id": "main-1", "to_task_id": "main-2"}
    if mutation == "missing_assessment":
        payload["assessment_id"] = "missing"
    elif mutation == "wrong_source":
        payload["from_task_id"] = "side-1"
    elif mutation == "stale_goal":
        payload["goal_revision"] = 2
    elif mutation == "reopen":
        f.status("main-2", "completed")
    else:
        f.add("workspace_next_lane_selected", payload)
        payload["decision_id"] = "duplicate"
    before = copy.deepcopy(f.state)
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_next_lane_selected", payload)
    assert f.state == before


@pytest.mark.parametrize("mutation", ["criterion", "evidence", "digest", "task", "revision"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005'])
def test_assessments_require_current_criteria_and_registered_scoped_evidence(mutation):
    f = Fixture()
    f.register(ref(), task_id="main-2")
    a = assessment()
    a["lane_id"] = "main-2"
    if mutation == "criterion":
        a["criterion_results"][0]["criterion_id"] = "missing"
    elif mutation == "evidence":
        a["evidence"] = [ref("missing")]
        a["criterion_results"][0]["evidence_ids"] = ["missing"]
    elif mutation == "digest":
        a["evidence"][0]["sha256"] = "b" * 64
    elif mutation == "task":
        a.update(task_id="side-1", lane_id="side-1")
    else:
        a["goal_revision"] = 2
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_assessed", {"assessment": a})


def test_operation_key_conflict_and_entity_duplicate_refuse():
    f = Fixture()
    original = f.admit()
    changed = copy.deepcopy(original)
    changed["payload"]["message"]["content"]["utf8_bytes"] = 6
    with pytest.raises(s.WorkspaceStateError, match="conflicting operation key"):
        s.apply_event(f.state, changed, f.view)
    with pytest.raises(s.WorkspaceStateError):
        f.add(original["event"], original["payload"])


@pytest.mark.parametrize("mutation", ["unknown_proof", "wrong_delivery", "wrong_task", "fake_parent", "stream_gap", "reply_missing"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006'])
def test_delivery_refs_parent_and_order_are_resolved(mutation):
    f = Fixture()
    if mutation in {"stream_gap", "reply_missing"}:
        f.register(ref("grant"), category="grant")
        m = message()
        if mutation == "stream_gap":
            m["sequence"] = 2
        else:
            m["reply_to"] = "missing"
        with pytest.raises(s.WorkspaceStateError):
            f.add("workspace_message_admitted", {"message": m, "delivery": delivery()})
        return
    f.admit()
    if mutation == "fake_parent":
        child = delivery("queued")
        child.update(delivery_id="child", parent_delivery_id="invented")
        with pytest.raises(s.WorkspaceStateError):
            f.add("workspace_delivery_linked", {"delivery": child, "evidence": {}})
        return
    proof = {"durable_admission": ref("proof")}
    if mutation == "wrong_task":
        f.register(ref("proof"), task_id="side-1")
    elif mutation == "wrong_delivery":
        f.register(ref("proof"), category="grant")
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_delivery_advanced", {"delivery": delivery("queued"), "evidence": proof, "supported_ack_levels": []})


@pytest.mark.parametrize("mutation", ["claim", "request", "attempt", "generation", "expired", "selected_missing"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005', 'p004_case_006'])
def test_selection_binds_existing_coordinator_claim_and_messages(mutation):
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    after = delivery("included_in_attempt")
    after["selection"]["request_sha256"] = "d" * 64
    proof = f.proofs("included_in_attempt")
    if mutation == "expired":
        f.status("main-2", "expired")
    else:
        field, value = {"claim": ("claim_id", "wrong"), "request": ("request_sha256", "b" * 64),
                        "attempt": ("attempt", 2), "generation": ("owner_generation", 4),
                        "selected_missing": ("selected_message_ids", ["message", "missing"])}[mutation]
        after["selection"][field] = value
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_delivery_advanced", {"delivery": after, "evidence": proof, "supported_ack_levels": []})


def test_feature_marker_required_and_old_pure_reducer_refuses_without_mutation():
    from _swarm_coordinator import SwarmCoordinator
    f = Fixture()
    marker = f.steps[0][0]
    state = {"tasks": {}, "claims": {}, "workers": {}, "messages": [], "artifacts": [], "closed": False}
    original = copy.deepcopy(state)
    with pytest.raises(ValueError, match="unknown swarm event"):
        SwarmCoordinator._apply_record(state, marker)
    assert state == original
    with pytest.raises(s.WorkspaceStateError):
        s.apply_event(None, f.steps[1][0], f.view)
    changed = copy.deepcopy(marker)
    changed["payload"]["version"] = 2
    with pytest.raises(s.WorkspaceStateError):
        s.apply_event(None, changed, f.view)


@pytest.mark.parametrize("mutation", ["unknown", "boolean_revision", "nonfinite", "oversize", "old_version", "stale_revision"], ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004', 'p005_case_005', 'p005_case_006'])
def test_strict_event_shape_and_bounds(mutation):
    f = Fixture()
    event = f.event("workspace_evidence_registered", {"reference": ref(), "category": "event", "task_id": "main-2"})
    if mutation == "unknown":
        event["auth_context"] = {"authenticated": True}
    elif mutation == "boolean_revision":
        event["expected_revision"] = True
    elif mutation == "nonfinite":
        event["payload"]["reference"]["sha256"] = float("nan")
    elif mutation == "oversize":
        event["payload"]["task_id"] = "x" * (p.MAX_RECORD_BYTES + 1)
    elif mutation == "old_version":
        event["protocol"] = "summon.swarm/v1"
    else:
        event["expected_revision"] -= 1
    with pytest.raises(s.WorkspaceStateError):
        s.apply_event(f.state, event, f.view)


def test_state_is_detached_and_projection_does_not_mutate_authoritative_view():
    f = Fixture()
    old = copy.deepcopy(f.state)
    view = copy.deepcopy(f.view)
    output = s.project(f.state, f.view)
    output["goal"]["objective"] = "changed"
    assert f.state == old and f.view == view
    f.view["tasks"][0]["request_sha256"] = "a" * 64
    with pytest.raises(s.WorkspaceStateError):
        s.project(f.state, f.view)


def test_current_bounds_refuse_without_pruning_history(monkeypatch):
    f = Fixture()
    monkeypatch.setattr(s, "MAX_EVENTS", len(f.state["operations"]))
    before = copy.deepcopy(f.state)
    with pytest.raises(s.WorkspaceStateError):
        f.register(ref())
    assert f.state == before


@pytest.mark.parametrize("independent_stream", [False, True], ids=['p006_case_001', 'p006_case_002'])
def test_held_predecessor_blocks_its_stream_but_not_independent_stream(independent_stream):
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.advance("held_for_recovery")
    m, d = message(), delivery()
    m.update(message_id="second", stream_id="other-stream" if independent_stream else "stream", sequence=1 if independent_stream else 2)
    d.update(message_id="second", delivery_id="second-delivery")
    f.add("workspace_message_admitted", {"message": m, "delivery": d})
    queued = dict(d, state="queued")
    f.register(ref("admit-second"), delivery_id="second-delivery")
    f.add("workspace_delivery_advanced", {"delivery": queued, "evidence": {"durable_admission": ref("admit-second")}, "supported_ack_levels": []})
    f.status("main-2", "claimed")
    selected = delivery("included_in_attempt")
    selected.update(message_id="second", delivery_id="second-delivery")
    selected["selection"].update(selected_message_ids=["second"], request_sha256="d" * 64)
    proof = {}
    for key in ("selection_record", "whole_message_fit"):
        proof[key] = ref("second-" + key)
        f.register(proof[key], delivery_id="second-delivery")
    payload = {"delivery": selected, "evidence": proof, "supported_ack_levels": []}
    if independent_stream:
        f.add("workspace_delivery_advanced", payload)
    else:
        with pytest.raises(s.WorkspaceStateError, match="held required predecessor"):
            f.add("workspace_delivery_advanced", payload)


@pytest.mark.parametrize("target,wrong_category", [("submitted", "observation"), ("submitted", "artifact"), ("acknowledged", "observation"), ("acknowledged", "event")], ids=['p007_case_001', 'p007_case_002', 'p007_case_003', 'p007_case_004'])
def test_adapter_receipts_cannot_be_substituted_with_other_evidence_categories(target, wrong_category):
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    f.advance("included_in_attempt")
    f.advance("submission_started")
    if target == "acknowledged":
        f.advance("submitted")
    after = copy.deepcopy(f.state["deliveries"]["delivery"])
    template = delivery(target)
    after.update(state=target, certainty=template["certainty"], ack_level=template["ack_level"])
    proof = evidence_for(target)
    key = "adapter_receipt" if target == "submitted" else "ack_receipt"
    f.register(proof[key], category=wrong_category, delivery_id="delivery")
    with pytest.raises(s.WorkspaceStateError, match="category incompatible"):
        f.add("workspace_delivery_advanced", {"delivery": after, "evidence": proof, "supported_ack_levels": ["recipient_received"]})


def test_coordinator_view_bound_matches_existing_coordinator_and_refuses_overflow():
    from _swarm_coordinator import MAX_TASKS
    assert s.MAX_COORDINATOR_TASKS == MAX_TASKS
    f = Fixture()
    f.view["tasks"] = [copy.deepcopy(f.view["tasks"][0]) for _ in range(MAX_TASKS + 1)]
    with pytest.raises(s.WorkspaceStateError):
        s.project(f.state, f.view)


def test_claim_fence_is_independent_of_transient_writer_owner():
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    # Unrelated writer metadata is deliberately not a claim identity input.
    f.view["journal_writer_generation"] = 999
    f.advance("included_in_attempt")
    assert f.state["deliveries"]["delivery"]["selection"]["owner_generation"] == 3


@pytest.mark.parametrize("child_category", ["artifact", "grant"], ids=['p008_case_001', 'p008_case_002'])
def test_no_contact_successor_still_requires_grant_category(child_category):
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    f.advance("included_in_attempt")
    f.advance("not_submitted")
    child = delivery("queued")
    child.update(delivery_id="child", parent_delivery_id="delivery", grant_ref=ref("child-grant"))
    f.register(ref("child-grant"), category=child_category)
    f.register(ref("remaining"), category="grant")
    f.register(ref("disposition"), delivery_id="delivery")
    payload = {"delivery": child, "evidence": {"authenticated_disposition": ref("disposition"), "remaining_authority": ref("remaining")}}
    if child_category == "artifact":
        original = copy.deepcopy(f.state)
        with pytest.raises(s.WorkspaceStateError, match="linked delivery requires a registered grant"):
            f.add("workspace_delivery_linked", payload)
        assert f.state == original
    else:
        f.add("workspace_delivery_linked", payload)
        assert f.state["deliveries"]["child"]["grant_ref"] == ref("child-grant")


def add_hold(f, affected, name="hold", disposition="hold_affected_lane"):
    f.register(ref(name + "-evidence"), task_id="side-1")
    a = assessment()
    a.update(assessment_id=name, task_id="side-1", lane_id="side-1", disposition=disposition,
             affected_task_ids=affected, next_decision="blocked", evidence=[ref(name + "-evidence")])
    a["criterion_results"][0]["evidence_ids"] = [name + "-evidence"]
    return f.add("workspace_assessed", {"assessment": a})


def decision_payload():
    return {"decision_id": "next", "goal_revision": 1, "assessment_id": "main-result",
            "from_task_id": "main-1", "to_task_id": "main-2"}


@pytest.mark.parametrize("affected,blocked", [(["main-2"], True), (["side-1"], False)], ids=['p009_case_001', 'p009_case_002'])
def test_critical_side_hold_blocks_only_its_typed_affected_lane(affected, blocked):
    f = Fixture()
    f.assess("main-1", "main-result")
    add_hold(f, affected)
    if blocked:
        with pytest.raises(s.WorkspaceStateError, match="unresolved typed hold"):
            f.add("workspace_next_lane_selected", decision_payload())
    else:
        f.add("workspace_next_lane_selected", decision_payload())
    assert s.replay(f.steps) == f.state
    holds = s.project(f.state, f.view)["holds"]
    assert len(holds) == 1 and holds[0]["affected_task_ids"] == affected
    assert holds[0]["evidence"] == [ref("hold-evidence")]


def test_later_happy_assessment_does_not_clear_hold_and_grant_guard_rechecks():
    f = Fixture()
    f.assess("main-1", "main-result")
    selected = f.add("workspace_next_lane_selected", decision_payload())
    add_hold(f, ["main-2"])
    f.assess("side-1", "happy-later")
    # The old selection remains a historical fact, never a fresh grant.
    assert s.apply_event(f.state, selected, f.view) == f.state
    with pytest.raises(s.WorkspaceStateError, match="unresolved typed hold"):
        s.require_no_unresolved_hold(f.state, "main-2")
    assert s.unresolved_holds(f.state, "main-2")[0]["assessment_id"] == "hold"
    with pytest.raises(s.WorkspaceStateError, match="unsupported workspace event"):
        f.add("workspace_hold_released", {"assessment_id": "hold"})


@pytest.mark.parametrize("disposition", ["hold_affected_lane", "promote_issue_to_priority"], ids=['p010_case_001', 'p010_case_002'])
def test_scoped_disposition_cannot_name_unbound_lane(disposition):
    f = Fixture()
    with pytest.raises(s.WorkspaceStateError, match="unknown lane task"):
        add_hold(f, ["unbound-task"], disposition=disposition)


def test_goal_priority_must_bind_coordinator_task_then_complete_workspace_plan():
    f = Fixture()
    marker = f.steps[0][0]
    initial = s.apply_event(None, marker, f.view)
    event = copy.deepcopy(f.steps[1][0])
    event["payload"]["goal"]["active_priority"] = "missing"
    with pytest.raises(s.WorkspaceStateError, match="existing coordinator task"):
        s.apply_event(initial, event, f.view)
    # A prepared coordinator task is insufficient if omitted from the lane plan.
    f.state["lanes"].pop("main-2")
    with pytest.raises(s.WorkspaceStateError, match="initial priority"):
        f.assess("main-1", "incomplete-plan")


def second_queued(f, independent=False):
    m, d = message(), delivery()
    m.update(message_id="second", stream_id="independent" if independent else "stream", sequence=1 if independent else 2)
    d.update(message_id="second", delivery_id="second-delivery")
    f.add("workspace_message_admitted", {"message": m, "delivery": d})
    f.register(ref("admit-second"), delivery_id="second-delivery")
    f.add("workspace_delivery_advanced", {"delivery": dict(d, state="queued"),
          "evidence": {"durable_admission": ref("admit-second")}, "supported_ack_levels": []})
    after = delivery("included_in_attempt")
    after.update(message_id="second", delivery_id="second-delivery")
    after["selection"].update(selected_message_ids=["second"], request_sha256="d" * 64)
    proof = {key: ref("second-" + key) for key in ("selection_record", "whole_message_fit")}
    for reference in proof.values():
        f.register(reference, delivery_id="second-delivery")
    return {"delivery": after, "evidence": proof, "supported_ack_levels": []}


@pytest.mark.parametrize("earlier", ["queued", "included_in_attempt"], ids=['p011_case_001', 'p011_case_002'])
def test_selection_cannot_skip_queued_or_previously_included_same_stream_message(earlier):
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    if earlier == "included_in_attempt":
        f.advance(earlier)
    selected = second_queued(f)
    with pytest.raises(s.WorkspaceStateError, match="unresolved predecessor"):
        f.add("workspace_delivery_advanced", selected)


def test_whole_ordered_prefix_is_selectable_and_reversed_prefix_refuses():
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    selected = second_queued(f)
    selected["delivery"]["selection"]["selected_message_ids"] = ["second", "message"]
    with pytest.raises(s.WorkspaceStateError, match="out of order"):
        f.add("workspace_delivery_advanced", selected)
    selected["delivery"]["selection"]["selected_message_ids"] = ["message", "second"]
    f.add("workspace_delivery_advanced", selected)
    assert f.state["deliveries"]["second-delivery"]["selection"]["selected_message_ids"] == ["message", "second"]


def test_queued_predecessor_does_not_block_independent_stream():
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.status("main-2", "claimed")
    f.add("workspace_delivery_advanced", second_queued(f, independent=True))


@pytest.mark.parametrize("reason", ["content_missing", "content_mismatch", "content_unavailable"], ids=['p012_case_001', 'p012_case_002', 'p012_case_003'])
@pytest.mark.parametrize("stage", ["queued", "submission_started", "submitted"], ids=['p013_case_001', 'p013_case_002', 'p013_case_003'])
def test_content_holds_need_scoped_registered_observation_and_preserve_facts(reason, stage):
    f = Fixture()
    f.admit()
    f.advance("queued")
    if stage != "queued":
        f.status("main-2", "claimed")
        f.advance("included_in_attempt")
        f.advance("submission_started")
        if stage == "submitted":
            f.advance(stage)
    before = copy.deepcopy(f.state["deliveries"]["delivery"])
    after = dict(before, state="held_for_recovery", reason=reason)
    f.register(ref("content-check"), category="observation", delivery_id="delivery")
    f.add("workspace_delivery_advanced", {"delivery": after, "evidence": {"hold_observation": ref("content-check")}, "supported_ack_levels": []})
    assert f.state["deliveries"]["delivery"]["certainty"] == before["certainty"]
    assert f.state["deliveries"]["delivery"]["selection"] == before["selection"]


def test_content_hold_cannot_substitute_generic_event_for_observation():
    f = Fixture()
    f.admit()
    f.advance("queued")
    f.register(ref("generic"), category="event", delivery_id="delivery")
    after = dict(f.state["deliveries"]["delivery"], state="held_for_recovery", reason="content_missing")
    with pytest.raises(s.WorkspaceStateError, match="registered observation"):
        f.add("workspace_delivery_advanced", {"delivery": after, "evidence": {"hold_observation": ref("generic")}, "supported_ack_levels": []})


def settlement_payload(*, target="queued", role="durable_admission", name="settlement-proof"):
    return {"reference": ref(name), "category": "event", "task_id": "main-2",
            "delivery_id": "delivery", "settlement_for": {
                "delivery_id": "delivery", "target_state": target, "role": role}}


def test_actual_evidence_consumes_one_immutable_slot_then_transition_consumes_its_event():
    f = Fixture()
    f.admit()
    before_counts = s.settlement_record_counts(f.state)
    payload = settlement_payload()
    f.add("workspace_evidence_registered", payload)
    assert s.settlement_record_counts(f.state) == (before_counts[0], before_counts[1] - 1)
    registered_state = copy.deepcopy(f.state)
    duplicate = f.event("workspace_evidence_registered", payload, "duplicate-registration")
    assert s.apply_event(f.state, duplicate, f.view) == registered_state
    conflict = copy.deepcopy(payload)
    conflict["reference"] = ref("replacement")
    with pytest.raises(s.WorkspaceStateError, match="immutable"):
        f.add("workspace_evidence_registered", conflict)
    f.register(ref("unallocated-proof"), delivery_id="delivery")
    before = copy.deepcopy(f.state)
    with pytest.raises(s.WorkspaceStateError, match="substitutes"):
        f.add("workspace_delivery_advanced", {"delivery": delivery("queued"),
            "evidence": {"durable_admission": ref("unallocated-proof")}, "supported_ack_levels": []})
    assert f.state == before
    f.add("workspace_delivery_advanced", {"delivery": delivery("queued"),
        "evidence": {"durable_admission": payload["reference"]}, "supported_ack_levels": []})
    assert s.settlement_record_counts(f.state) == (before_counts[0] - 1, before_counts[1] - 1)
    # Historical exact registration remains a no-op, never new credit.
    assert s.apply_event(f.state, f.event("workspace_evidence_registered", payload), f.view) == f.state


@pytest.mark.parametrize("mutation", ["unknown_role", "wrong_target", "wrong_category",
                                      "wrong_task", "wrong_delivery", "missing_delivery", "extra_field"], ids=['p014_case_001', 'p014_case_002', 'p014_case_003', 'p014_case_004', 'p014_case_005', 'p014_case_006', 'p014_case_007'])
def test_settlement_registration_refuses_irrelevant_or_misbound_slots(mutation):
    f = Fixture()
    f.admit()
    payload = settlement_payload()
    if mutation == "unknown_role":
        payload["settlement_for"]["role"] = "invented"
    elif mutation == "wrong_target":
        payload["settlement_for"]["target_state"] = "acknowledged"
    elif mutation == "wrong_category":
        payload["category"] = "artifact"
    elif mutation == "wrong_task":
        payload["task_id"] = "side-1"
    elif mutation == "wrong_delivery":
        payload["settlement_for"]["delivery_id"] = "other"
    elif mutation == "missing_delivery":
        payload.pop("delivery_id")
    else:
        payload["settlement_for"]["authority"] = True
    before = copy.deepcopy(f.state)
    with pytest.raises(s.WorkspaceStateError):
        f.add("workspace_evidence_registered", payload)
    assert f.state == before


def test_consumed_or_abandoned_branch_slots_cannot_be_reallocated():
    f = Fixture()
    f.admit()
    f.advance("queued")
    with pytest.raises(s.WorkspaceStateError, match="no longer reachable"):
        f.add("workspace_evidence_registered", settlement_payload())
    f.advance("held_for_recovery")
    with pytest.raises(s.WorkspaceStateError, match="no longer reachable"):
        f.add("workspace_evidence_registered", settlement_payload(
            target="submitted", role="adapter_receipt"))
    assert s.settlement_record_counts(f.state) == (1, 1)
    assert f.state["deliveries"]["delivery"]["state"] == "held_for_recovery"
