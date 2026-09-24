"""Synthetic, provider-inert presentation tests; no server/rendered qualification."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _submission_accounting as submission_accounting
import _workspace_admission as admission
import _workspace_protocol as protocol
import _workspace_view as view
from test_workspace_protocol import ref, base, message as ordinary_message
from test_workspace_state import Fixture, add_hold, decision_payload


def scope(*, audience="public", text=False, key=b"v" * 32):
    return view.ViewScope("workspace", "run", key, audience, text)


def status(fixture, *, closed=False):
    tasks = copy.deepcopy(fixture.view["tasks"])
    deliveries = fixture.state["deliveries"]
    unsettled = sum(bool(admission.outstanding_obligations(d["state"], key,
        certainty=d["certainty"], inherited_uncertainty=d["inherited_uncertainty"])) for key, d in deliveries.items())
    effects = sorted({field for d in deliveries.values() for field, value in d["certainty"].items() if value == "unknown"}
                     | {field for d in deliveries.values() for field in d["inherited_uncertainty"]})
    terminal = all(task["status"] in {"completed", "cancelled", "failed", "blocked"} for task in tasks)
    state = "closed" if closed else "settlement_required" if terminal and unsettled else "completed" if terminal else "running" if any(task["status"] == "claimed" for task in tasks) else "prepared"
    report = {"delivery_count": len(deliveries), "unsettled_delivery_count": unsettled,
              "uncertain_effects": effects, "closure_ready": not closed and terminal and not unsettled}
    if fixture.state.get("supervisor_endpoints") or fixture.state.get("inbox_deliveries"):
        inbox = view.state_protocol.inbox_summary(fixture.state)
        report["inbox"] = inbox
        report["closure_ready"] = report["closure_ready"] and inbox["closure_ready"]
        if terminal and not closed and not inbox["closure_ready"]:
            state = "settlement_required"
    resolutions = fixture.state.get("effect_resolutions", {})
    if resolutions:
        report["effect_resolution"] = {"count": len(resolutions), "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
                                       "qualification": "simulated"}
    return {"run_id": "run", "status": state, "generation": fixture.state["revision"], "tasks": tasks,
            "uncertain_spend": "spend" in effects, "torn_tail": False, "workspace": report}


def lifecycle(target):
    fixture = Fixture()
    fixture.admit()
    paths = {
        "accepted": [], "queued": ["queued"],
        "included_in_attempt": ["queued", "included_in_attempt"],
        "submission_started": ["queued", "included_in_attempt", "submission_started"],
        "submitted": ["queued", "included_in_attempt", "submission_started", "submitted"],
        "acknowledged": ["queued", "included_in_attempt", "submission_started", "submitted", "acknowledged"],
        "not_submitted": ["queued", "included_in_attempt", "not_submitted"],
        "held_for_recovery": ["queued", "included_in_attempt", "submission_started", "held_for_recovery"],
        "dead_lettered": ["queued", "included_in_attempt", "submission_started", "held_for_recovery", "dead_lettered"],
        "rejected": ["queued", "rejected"], "expired": ["queued", "expired"], "cancelled": ["queued", "cancelled"],
    }
    for target_state in paths[target]:
        if target_state == "included_in_attempt":
            fixture.status("main-2", "claimed")
        fixture.advance(target_state)
    return fixture


def render(fixture, **kwargs):
    return view.project_workspace(fixture.state, status(fixture), scope=kwargs.pop("scope", scope()), **kwargs)


def test_operator_capabilities_are_host_scoped_detached_and_snapshot_bound():
    fixture = lifecycle("queued")
    operator = scope(audience="operator")
    delivery_id = next(iter(fixture.state["deliveries"]))
    target = view._opaque(operator, "delivery", delivery_id)
    capabilities = {"available": True, "actions_by_delivery": {target: ["cancel_queued_context"]}, "reason": None}
    before = copy.deepcopy(fixture.state)
    result = render(fixture, scope=operator, operator_commands=capabilities)
    assert result["operator_commands"] == capabilities
    capabilities["actions_by_delivery"][target].clear()
    assert result["operator_commands"]["actions_by_delivery"][target] == ["cancel_queued_context"]
    assert render(fixture, scope=operator)["snapshot"] != result["snapshot"]
    assert fixture.state == before
    assert render(fixture)["operator_commands"] == {
        "available": False, "actions_by_delivery": {}, "reason": "read_only_export"}


def test_retain_disposition_projection_binds_target_to_delivery_task():
    fixture = lifecycle("held_for_recovery")
    operator = scope(audience="operator")
    delivery_id = next(iter(fixture.state["deliveries"]))
    delivery_target = view._opaque(operator, "delivery", delivery_id)
    task_id = fixture.state["messages"][fixture.state["deliveries"][delivery_id]["message_id"]]["destination_task_id"]
    task_target = view._opaque(operator, "task", task_id)
    capability = {"available": True, "targets": [{"id": delivery_target, "task_id": task_target,
                  "label": "Held context 1", "available": True,
                  "reasons": ["awaiting_evidence"]}], "reason": None}
    result = render(fixture, scope=operator, operator_dispositions=capability)
    assert result["operator_dispositions"] == capability
    wrong_task = next(key for key in fixture.state["lanes"] if key != task_id)
    capability["targets"][0]["task_id"] = view._opaque(operator, "task", wrong_task)
    with pytest.raises(view.WorkspaceViewError, match="invalid_disposition_capabilities"):
        render(fixture, scope=operator, operator_dispositions=capability)


def test_public_accounting_summary_rejects_unknown_nested_fields():
    fixture = lifecycle("acknowledged")
    coordinator = status(fixture)
    summary = submission_accounting.public_summary([])
    summary["estimate"]["synthetic_private_field"] = "must-refuse"
    coordinator["workspace"]["economics"] = {
        "enabled": True,
        "reservation": {"outstanding_bytes": 0, "outstanding_records": 0},
        "settled_count": 0,
        "accounting": summary,
    }
    with pytest.raises(view.WorkspaceViewError):
        view.project_workspace(fixture.state, coordinator, scope=scope())


@pytest.mark.parametrize("change", ["public", "unknown", "raw_target", "wrong_state", "fake_reason"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_operator_capability_projection_refuses_scope_or_state_contradiction(change):
    fixture = lifecycle("queued")
    operator = scope(audience="operator")
    target = view._opaque(operator, "delivery", next(iter(fixture.state["deliveries"])))
    capabilities = {"available": True, "actions_by_delivery": {target: ["cancel_queued_context"]}, "reason": None}
    if change == "public": operator = scope()
    elif change == "unknown": capabilities["receipt"] = "SYNTHETIC-PRIVATE"
    elif change == "raw_target": capabilities["actions_by_delivery"] = {"delivery": ["cancel_queued_context"]}
    elif change == "wrong_state": capabilities["actions_by_delivery"][target] = ["dispose_held_context"]
    else: capabilities["reason"] = "SYNTHETIC-PRIVATE"
    with pytest.raises(view.WorkspaceViewError):
        render(fixture, scope=operator, operator_commands=capabilities)


def inbox_fixture(target, *, inherited=False):
    """Protocol-valid structural snapshot; this is not a runtime receipt fixture."""
    fixture = Fixture()
    endpoint_id = "supervisor-endpoint"
    grant = ref("receiving-grant")
    endpoint = dict(base("supervisor_endpoint"), endpoint_id=endpoint_id, goal_id="goal", revision=1,
                    owner={"instance_id": "supervisor-owner", "epoch": 1}, receiver_grant_ref=grant,
                    recovery_fence_ref=None, lease_expires_at_ms=9999, status="retired")
    fixture.state["supervisor_endpoints"] = {endpoint_id: endpoint}
    fixture.state["evidence"][grant["id"]] = {"reference": grant, "category": "grant", "endpoint_id": endpoint_id}
    message = ordinary_message()
    message.update(kind="supervisor_message", grant_ref=grant,
                   recipient={"kind": "supervisor_inbox", "endpoint_id": endpoint_id,
                              "owner_instance_id": "supervisor-owner", "epoch": 1})
    message.pop("destination_task_id")
    fixture.state["messages"][message["message_id"]] = message
    fixture.state["streams"][message["stream_id"]] = {"sequence": message["sequence"],
        "binding": {key: message[key] for key in ("sender", "recipient", "source_task_id")}}
    offered = target in {"offered", "acknowledged", "held_for_recovery", "dead_lettered"}
    consumer = {"workspace_id": "workspace", "run_id": "run", "endpoint_id": endpoint_id,
                "owner_instance_id": "supervisor-owner", "epoch": 1,
                "receiver_grant_id": grant["id"], "receiver_grant_sha256": grant["sha256"]}
    offer = {"offer_id": "synthetic-offer", "delivery_id": "inbox-delivery", "message_id": message["message_id"],
             "content_sha256": message["content"]["sha256"], "content_utf8_bytes": message["content"]["utf8_bytes"],
             "consumer": consumer} if offered else None
    receipt = {"receipt_kind": "supervisor_context_received", "consumer_kind": protocol.INBOX_CONSUMER_KIND,
               "qualification": "simulated", **{key: offer[key] for key in ("offer_id", "content_sha256", "content_utf8_bytes")}} if target == "acknowledged" else None
    delivery = dict(base("inbox_delivery"), delivery_id="inbox-delivery", message_id=message["message_id"],
                    recipient=copy.deepcopy(message["recipient"]), grant_ref=grant, state=target,
                    reason="synthetic-private-reason" if target in {"held_for_recovery", "cancelled", "expired", "dead_lettered"} else None,
                    offer=offer, receipt=receipt, exposure="consumer_received" if receipt else "unknown" if offered else "not_exposed",
                    prior_offer_id=None, inherited_exposure=None, possible_duplicate=False)
    fixture.state["inbox_deliveries"] = {delivery["delivery_id"]: delivery}
    if inherited:
        # Retain an explicitly disposed predecessor, with a fresh receiving grant
        # and a later recipient epoch on the child. No current endpoint implies
        # that this historic delivery was offered by its current owner.
        parent = copy.deepcopy(delivery)
        parent.update(delivery_id="prior-inbox", state="dead_lettered", reason="prior-disposition", receipt=None, exposure="unknown")
        parent["offer"]["delivery_id"] = parent["delivery_id"]
        parent["offer"]["offer_id"] = "prior-offer"
        fixture.state["inbox_deliveries"][parent["delivery_id"]] = parent
        fresh = ref("fresh-receiving-grant")
        fixture.state["evidence"][fresh["id"]] = {"reference": fresh, "category": "grant", "endpoint_id": endpoint_id}
        delivery.update(parent_delivery_id=parent["delivery_id"], grant_ref=fresh, prior_offer_id="prior-offer",
                        inherited_exposure="unknown", possible_duplicate=True)
        delivery["recipient"]["epoch"] = 2
        delivery["offer"]["consumer"].update(epoch=2, receiver_grant_id=fresh["id"], receiver_grant_sha256=fresh["sha256"])
        endpoint["owner"]["epoch"] = 2
        endpoint["receiver_grant_ref"] = fresh
    return fixture


@pytest.mark.parametrize("target", sorted(protocol.INBOX_STATES), ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007', 'p002_case_008'])
def test_every_inbox_state_preserves_context_exposure_without_live_supervisor_inference(target):
    fixture = inbox_fixture(target)
    before = copy.deepcopy(fixture.state)
    result = render(fixture)
    card = result["inbox"]["deliveries"][0]
    assert card["state"] == target and card["content_available"] is False
    assert result["mode"]["mode"] == "passive"
    assert result["inbox"]["endpoints"][0]["live_owner"] == "unverified"
    assert (card["receipt"] is not None) == (target == "acknowledged")
    if target == "dead_lettered": assert card["label"] == "Disposed with unknown exposure"
    encoded = json.dumps(result)
    for sentinel in ("supervisor-owner", "receiving-grant", "synthetic-offer", "synthetic-private-reason", "content_sha256"):
        assert sentinel not in encoded
    assert fixture.state == before


def test_disposed_unknown_exposure_remains_visible_after_workspace_close():
    fixture = inbox_fixture("dead_lettered")
    for task in ("main-1", "main-2", "side-1"):
        fixture.status(task, "completed")
    result = view.project_workspace(fixture.state, status(fixture, closed=True), scope=scope())
    assert result["closure"]["closed"] is True
    assert result["inbox"]["summary"]["disposed_with_unknown_exposure"] == 1
    assert result["inbox"]["deliveries"][0]["label"] == "Disposed with unknown exposure"


def test_received_context_with_inherited_exposure_does_not_clear_prior_unknown():
    fixture = inbox_fixture("acknowledged", inherited=True)
    result = render(fixture)
    child = next(item for item in result["inbox"]["deliveries"] if item["state"] == "acknowledged")
    assert child["label"] == "Simulated context received; prior exposure unknown"
    assert child["possible_duplicate"] and child["inherited_exposure"] == "unknown"
    assert child["receipt"] == {"level": "supervisor_context_received", "qualification": "simulated"}


@pytest.mark.parametrize("change", ["strip_uncertainty", "wrong_prior_offer", "nonterminal_parent", "wrong_parent"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004'])
def test_progressed_inbox_child_cannot_erase_or_substitute_lineage(change):
    fixture = inbox_fixture("acknowledged", inherited=True)
    child = fixture.state["inbox_deliveries"]["inbox-delivery"]
    parent = fixture.state["inbox_deliveries"]["prior-inbox"]
    if change == "strip_uncertainty":
        child.update(inherited_exposure=None, possible_duplicate=False, prior_offer_id=None)
    elif change == "wrong_prior_offer":
        child["prior_offer_id"] = "unrelated-prior-offer"
    elif change == "nonterminal_parent":
        parent.update(state="offered", reason=None)
    else:
        child["parent_delivery_id"] = "missing-parent"
    with pytest.raises(view.WorkspaceViewError):
        render(fixture)


@pytest.mark.parametrize("change", ["private_field", "receipt", "offer_content", "summary", "endpoint", "stream", "evidence_subject"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005', 'p004_case_006', 'p004_case_007'])
def test_inbox_contradictions_or_unknown_fields_refuse(change):
    fixture = inbox_fixture("acknowledged")
    report = status(fixture)
    item = fixture.state["inbox_deliveries"]["inbox-delivery"]
    if change == "private_field": item["raw_receipt"] = "SYNTHETIC-PRIVATE"
    elif change == "receipt": item["receipt"]["qualification"] = "provider_verified"
    elif change == "offer_content": item["offer"]["content_sha256"] = "f" * 64
    elif change == "summary": report["workspace"]["inbox"]["unknown_exposure"] = 1
    elif change == "endpoint": fixture.state["supervisor_endpoints"].clear()
    elif change == "stream": next(iter(fixture.state["streams"].values()))["binding"]["destination_task_id"] = "main-2"
    else: fixture.state["evidence"]["receiving-grant"]["task_id"] = "main-2"
    with pytest.raises(view.WorkspaceViewError):
        view.project_workspace(fixture.state, report, scope=scope())


@pytest.mark.parametrize("target", sorted(protocol.STATES), ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004', 'p005_case_005', 'p005_case_006', 'p005_case_007', 'p005_case_008', 'p005_case_009', 'p005_case_010', 'p005_case_011', 'p005_case_012'])
def test_every_delivery_boundary_keeps_acknowledgement_and_effects_distinct(target):
    fixture = lifecycle(target)
    result = render(fixture)
    delivery = result["deliveries"][0]
    original = fixture.state["deliveries"]["delivery"]
    assert delivery["state"] == target and delivery["label"] == view.DELIVERY_LABELS[target]
    assert delivery["certainty"] == original["certainty"]
    assert delivery["acknowledgement"] == original["ack_level"]
    assert delivery["automatic_retry"] is False and delivery["provider_billing_verified"] is False
    assert result["closure"]["closed"] is False
    if target == "not_submitted":
        assert "no automatic retry" in delivery["label"]


def test_public_export_redacts_free_text_raw_ids_hashes_and_receipts():
    fixture = lifecycle("acknowledged")
    sentinel = "SYNTHETIC-PRIVATE-PROMPT-ACCOUNT-PATH"
    fixture.state["goal"]["objective"] = sentinel
    fixture.state["goal"]["criteria"][0]["description"] = sentinel
    fixture.state["lanes"]["main-2"]["outcome"] = sentinel
    before = copy.deepcopy(fixture.state)
    result = render(fixture, events=[event for event, _ in fixture.steps])
    encoded = json.dumps(result)
    assert sentinel not in encoded
    for raw in ("worker-a", "worker-b", "main-1", "main-2", "side-1", "recipient_owner_fence",
                "current_grant", "claim", "a" * 64, "b" * 64, "c" * 64, "d" * 64):
        assert '"' + raw + '"' not in encoded
    assert "grant_ref" not in encoded and "sha256" not in encoded and "source_bytes" not in encoded
    assert result["goal"]["text_available"] is False
    assert all(control["available"] is False for control in result["controls"])
    assert fixture.state == before


def test_operator_text_requires_explicit_scope_and_never_opens_receipts():
    fixture = lifecycle("acknowledged")
    private = render(fixture, scope=scope(audience="operator", text=True))
    assert private["goal"]["objective"] == fixture.state["goal"]["objective"]
    assert private["goal"]["criteria"][0]["description"] == fixture.state["goal"]["criteria"][0]["description"]
    assert private["tasks"][0]["text"]["outcome"]
    assert not private["evidence"]["bodies_available"] and not private["deliveries"][0]["content"]["available"]
    assert "objective" not in render(fixture, scope=scope(audience="operator"))["goal"]
    with pytest.raises(view.WorkspaceViewError, match="scope_refused"):
        render(fixture, scope=scope(text=True))


@pytest.mark.parametrize("bad_scope", [None, {}, view.ViewScope("other", "run", b"v" * 32),
    view.ViewScope("workspace", "other", b"v" * 32), view.ViewScope("workspace", "run", b"short"),
    view.ViewScope("workspace", "run", b"v" * 32, "worker")], ids=['p006_case_001', 'p006_case_002', 'p006_case_003', 'p006_case_004', 'p006_case_005', 'p006_case_006'])
def test_missing_or_wrong_trusted_scope_refuses(bad_scope):
    with pytest.raises(view.WorkspaceViewError, match="scope_refused"):
        render(Fixture(), scope=bad_scope)


@pytest.mark.parametrize("location", ["workspace", "coordinator", "goal", "task", "evidence"], ids=['p007_case_001', 'p007_case_002', 'p007_case_003', 'p007_case_004', 'p007_case_005'])
def test_unknown_private_fields_refuse_without_echoing_the_value(location):
    fixture = lifecycle("accepted")
    coordinator = status(fixture)
    sentinel = "SYNTHETIC-SECRET-RECEIPT"
    target = {"workspace": fixture.state, "coordinator": coordinator, "goal": fixture.state["goal"],
              "task": coordinator["tasks"][0], "evidence": next(iter(fixture.state["evidence"].values()))}[location]
    target["raw_receipt"] = sentinel
    with pytest.raises(view.WorkspaceViewError) as error:
        view.project_workspace(fixture.state, coordinator, scope=scope())
    assert sentinel not in str(error.value)


@pytest.mark.parametrize("change", ["wrong_run", "missing_task", "contradictory_claim", "wrong_request",
                                     "torn", "wrong_delivery_count", "invented_close", "unknown_state"], ids=['p008_case_001', 'p008_case_002', 'p008_case_003', 'p008_case_004', 'p008_case_005', 'p008_case_006', 'p008_case_007', 'p008_case_008'])
def test_incomplete_stale_or_contradictory_snapshots_refuse(change):
    fixture = lifecycle("acknowledged")
    coordinator = status(fixture)
    if change == "wrong_run": coordinator["run_id"] = "other"
    elif change == "missing_task": coordinator["tasks"].pop()
    elif change == "contradictory_claim": coordinator["tasks"][-1]["active_claim"] = None
    elif change == "wrong_request": coordinator["tasks"][-1]["request_sha256"] = "e" * 64
    elif change == "torn": coordinator["torn_tail"] = True
    elif change == "wrong_delivery_count": coordinator["workspace"]["delivery_count"] = 0
    elif change == "invented_close": coordinator["status"] = "closed"
    else: coordinator["status"] = "connected"
    with pytest.raises(view.WorkspaceViewError):
        view.project_workspace(fixture.state, coordinator, scope=scope())


def test_task_completion_and_delivery_settlement_and_closure_are_separate():
    fixture = lifecycle("acknowledged")
    for task in ("main-1", "side-1", "main-2"):
        fixture.status(task, "completed")
    result = render(fixture, scope=scope(audience="operator"),
                    capabilities=[{"action": "close", "available": True, "reason": None}])
    assert all(task["status"] == "completed" for task in result["tasks"])
    assert result["deliveries"][0]["acknowledgement"] == "recipient_received"
    assert result["closure"]["state"] == "settlement_required" and not result["closure"]["ready"]
    assert next(control for control in result["controls"] if control["action"] == "close")["reason"] == "workspace_unsettled"


def test_explicit_simulated_resolution_is_retained_without_provider_billing_claim():
    fixture = lifecycle("acknowledged")
    evidence = {role: ref("resolution-" + role) for role in protocol.EFFECT_EVIDENCE_CATEGORIES}
    for role, reference in evidence.items():
        fixture.register(reference, category=protocol.EFFECT_EVIDENCE_CATEGORIES[role], delivery_id="delivery")
    after = copy.deepcopy(fixture.state["deliveries"]["delivery"])
    after["certainty"].update(spend="not_incurred", cleanup="complete")
    fixture.add("workspace_effects_resolved", {"delivery": after, "evidence": evidence,
                "resolution_kind": protocol.EFFECT_RESOLUTION_KIND, "qualification": "simulated"})
    for task in ("main-1", "side-1", "main-2"):
        fixture.status(task, "completed")
    result = render(fixture)
    row = result["deliveries"][0]
    assert row["effect_resolution"] == {"resolution_kind": protocol.EFFECT_RESOLUTION_KIND, "qualification": "simulated"}
    assert not row["provider_billing_verified"] and row["certainty"]["contact"] == "occurred"
    assert result["closure"]["ready"] and not result["closure"]["closed"]
    closed = view.project_workspace(fixture.state, status(fixture, closed=True), scope=scope())
    assert closed["closure"]["closed"] and not closed["closure"]["ready"]


def test_missing_history_and_identity_and_supervisor_do_not_imply_live_capabilities():
    result = render(Fixture())
    assert result["mode"] == {"mode": "passive", "owner_state": "unknown", "verified_active": False}
    assert result["timeline"]["unavailable_reason"] == "event_history_not_supplied"
    assert all(task["agent"]["served_model"] is None for task in result["tasks"])
    assert all(task["agent"]["requested_model"] is None for task in result["tasks"])


def test_only_trusted_consistent_supervisor_observation_can_show_supervised_mode():
    fixture = Fixture()
    supervisor = {"mode": "supervised", "owner_state": "active", "verified_active": True}
    assert render(fixture, supervisor=supervisor)["mode"] == supervisor
    supervisor["verified_active"] = False
    with pytest.raises(view.WorkspaceViewError, match="supervisor_observation_unverified"):
        render(fixture, supervisor=supervisor)


@pytest.mark.parametrize("capability", [
    {"action": "review", "available": False, "reason": "not_implemented"},
    {"action": "deliberate", "available": False, "reason": "scope_required"},
    {"action": "resume", "available": False, "reason": "adapter_unverified"},
], ids=['p009_case_001', 'p009_case_002', 'p009_case_003'])
def test_unavailable_typed_controls_keep_explicit_reason(capability):
    result = render(Fixture(), scope=scope(audience="operator"), capabilities=[capability])
    assert capability in result["controls"]


def test_capability_payload_cannot_leak_credentials_or_free_text_reason():
    with pytest.raises(view.WorkspaceViewError):
        render(Fixture(), capabilities=[{"action": "review", "available": True, "reason": None, "bearer": "secret"}])
    with pytest.raises(view.WorkspaceViewError):
        render(Fixture(), capabilities=[{"action": "review", "available": False, "reason": "private-account-details"}])


def test_stable_bounded_timeline_pagination_and_stale_cursor_refusal():
    fixture = Fixture()
    for number in range(220):
        fixture.register(ref("proof-" + str(number)))
    events = [event for event, _ in fixture.steps]
    original = copy.deepcopy((fixture.state, events))
    first = render(fixture, events=events, limit=100)
    again = render(fixture, events=list(reversed(events)), limit=100)
    assert first == again
    pages, cursor = [first], first["timeline"]["next_cursor"]
    while cursor:
        page = render(fixture, events=events, limit=100, cursor=cursor, expected_snapshot=first["snapshot"])
        pages.append(page)
        cursor = page["timeline"]["next_cursor"]
    items = [item for page in pages for item in page["timeline"]["items"]]
    assert len(items) == len(events) and len({item["id"] for item in items}) == len(items)
    assert all(len(page["timeline"]["items"]) <= 100 for page in pages)
    assert all(item["occurred_at"] is None for item in items)
    assert first["timeline"]["history_complete"]
    assert (fixture.state, events) == original
    fixture.register(ref("later-proof"))
    with pytest.raises(view.WorkspaceViewError, match="stale"):
        render(fixture, events=[event for event, _ in fixture.steps], limit=100, cursor=first["timeline"]["next_cursor"])
    newer = render(fixture)
    assert newer["tasks"][0]["id"] == first["tasks"][0]["id"]


def test_outer_journal_times_are_allowlisted_bound_and_paginated():
    fixture = Fixture()
    for number in range(4):
        fixture.register(ref("timed-proof-" + str(number)))
    events = [event for event, _ in fixture.steps]
    times = {event["operation_key"]: 1_700_000_000.0 + index
             for index, event in enumerate(events)}
    first = render(fixture, events=events, event_times=times, limit=2)
    assert [item["occurred_at"] for item in first["timeline"]["items"]] == [
        times[event["operation_key"]] for event in events[:2]]
    assert all(item["timestamp_reason"] == "journal_recorded"
               for item in first["timeline"]["items"])
    upper = dict(times)
    upper[events[0]["operation_key"]] = 9_999_999_999_999.0
    assert render(fixture, events=events, event_times=upper)["timeline"]["items"][0]["occurred_at"] == 9_999_999_999_999.0
    cursor = first["timeline"]["next_cursor"]
    second = render(fixture, events=events, event_times=times, limit=2,
                    cursor=cursor, expected_snapshot=first["snapshot"])
    assert [item["occurred_at"] for item in second["timeline"]["items"]] == [
        times[event["operation_key"]] for event in events[2:4]]
    changed = dict(times)
    changed[events[0]["operation_key"]] += 1
    with pytest.raises(view.WorkspaceViewError, match="stale"):
        render(fixture, events=events, event_times=changed, limit=2,
               cursor=cursor, expected_snapshot=first["snapshot"])


@pytest.mark.parametrize("bad", [
    {"not-an-event": 1_700_000_000.0},
    {"x": float("nan")},
    {"x": True},
    {"x": -1.0},
], ids=['p010_case_001', 'p010_case_002', 'p010_case_003', 'p010_case_004'])
def test_event_times_reject_invalid_or_unmatched_metadata(bad):
    fixture = Fixture()
    event = fixture.steps[0][0]
    if "x" in bad:
        bad = {event["operation_key"]: bad["x"]}
    with pytest.raises(view.WorkspaceViewError, match="event_timing|invalid_event_timing"):
        render(fixture, events=[event], event_times=bad)


def test_event_anchor_locates_later_page_after_new_canonical_event_without_input_mutation():
    fixture = Fixture()
    for number in range(205):
        fixture.register(ref("anchor-proof-" + str(number)))
    first = render(fixture, events=[event for event, _ in fixture.steps])
    last = render(fixture, events=[event for event, _ in fixture.steps], cursor=first["timeline"]["next_cursor"])
    anchor = last["timeline"]["items"][2]
    fixture.register(ref("anchor-later-proof"))
    events = [event for event, _ in fixture.steps]
    before = copy.deepcopy((fixture.state, events))
    newer = render(fixture, events=events, anchor=anchor["anchor"])
    assert newer["snapshot"] != last["snapshot"]
    assert newer["timeline"]["items"][2]["id"] == anchor["id"]
    assert newer["timeline"]["items"][0]["revision"] == last["timeline"]["items"][0]["revision"]
    assert len(newer["timeline"]["items"]) <= 200
    assert (fixture.state, events) == before
    with pytest.raises(view.WorkspaceViewError, match="stale"):
        render(fixture, events=events, cursor=first["timeline"]["next_cursor"])


@pytest.mark.parametrize("change", ["audience", "text", "key", "task", "unknown", "missing_history", "cursor"], ids=['p011_case_001', 'p011_case_002', 'p011_case_003', 'p011_case_004', 'p011_case_005', 'p011_case_006', 'p011_case_007'])
def test_anchor_scope_and_history_refusal(change):
    fixture = lifecycle("acknowledged")
    events = [event for event, _ in fixture.steps]
    first = render(fixture, events=events, scope=scope(audience="operator"))
    options = {"events": events, "scope": scope(audience="operator"),
               "anchor": first["timeline"]["items"][-1]["anchor"]}
    if change == "audience": options["scope"] = scope()
    elif change == "text": options["scope"] = scope(audience="operator", text=True)
    elif change == "key": options["scope"] = scope(audience="operator", key=b"x" * 32)
    elif change == "task": options["selected_task"] = first["priority_task_id"]
    elif change == "unknown": options["anchor"] = "unknown"
    elif change == "missing_history": options["events"] = events[:-1]
    else: options["cursor"] = "also-a-cursor"
    with pytest.raises(view.WorkspaceViewError, match="anchor_scope_or_history_refused|incompatible_page_selectors"):
        render(fixture, **options)


@pytest.mark.parametrize("limit", [0, 201, True, "100"], ids=['p012_case_001', 'p012_case_002', 'p012_case_003', 'p012_case_004'])
def test_page_limits_are_strict(limit):
    with pytest.raises(view.WorkspaceViewError, match="page_bound_exceeded"):
        render(Fixture(), limit=limit)


def test_foreign_audience_key_filter_and_modified_history_cannot_reuse_cursor():
    fixture = lifecycle("acknowledged")
    events = [event for event, _ in fixture.steps]
    first = render(fixture, events=events, limit=2)
    cursor = first["timeline"]["next_cursor"]
    for overrides in ({"scope": scope(audience="operator")}, {"scope": scope(key=b"w" * 32)},
                      {"selected_task": first["priority_task_id"]}, {"limit": 3}):
        options = dict(events=events, limit=2, cursor=cursor)
        options.update(overrides)
        with pytest.raises(view.WorkspaceViewError):
            render(fixture, **options)
    events[-1]["payload"]["delivery"]["certainty"]["cleanup"] = "complete"
    with pytest.raises(view.WorkspaceViewError, match="history_snapshot_mismatch"):
        render(fixture, events=events)


def test_raw_task_identity_is_not_a_presentation_selection_handle():
    fixture = lifecycle("queued")
    with pytest.raises(view.WorkspaceViewError, match="task_scope_refused"):
        render(fixture, selected_task="main-2")
    initial = render(fixture)
    selected = render(fixture, selected_task=initial["priority_task_id"])
    assert selected["selected_task_id"] == initial["priority_task_id"]


def test_scoped_side_hold_and_main_progress_do_not_become_council_approval():
    fixture = Fixture()
    fixture.status("main-1", "completed")
    fixture.status("side-1", "claimed")
    fixture.assess("main-1", "main-result")
    fixture.add("workspace_next_lane_selected", decision_payload())
    add_hold(fixture, ["side-1"])
    original = copy.deepcopy(fixture.state)
    result = render(fixture, events=[event for event, _ in fixture.steps])
    assert len(result["assessments"]) == 2 and len(result["decisions"]) == 1
    assert all(item["kind"] == "supervisor_assessment" for item in result["assessments"])
    assert result["holds"][0]["affected_task_ids"] != [result["priority_task_id"]]
    assert next(task for task in result["tasks"] if task["id"] == result["priority_task_id"])["role"] == "main"
    labels = [item["label"] for item in result["timeline"]["items"] if item["kind"] == "workspace_assessed"]
    assert labels == ["Supervisor assessment recorded"] * 2
    assert fixture.state == original
    result["assessments"][0]["criterion_results"][0]["result"] = "unknown"
    assert fixture.state == original


@pytest.mark.parametrize("location", ["streams", "grant", "parent"], ids=['p013_case_001', 'p013_case_002', 'p013_case_003'])
def test_dangling_display_bindings_are_not_presented_as_valid(location):
    fixture = lifecycle("queued")
    if location == "streams": fixture.state["streams"].clear()
    elif location == "grant": fixture.state["evidence"].pop("grant")
    else: fixture.state["deliveries"]["delivery"]["parent_delivery_id"] = "missing-parent"
    with pytest.raises(view.WorkspaceViewError):
        render(fixture)


def test_reordered_canonical_maps_keep_ids_labels_and_snapshot_stable():
    fixture = lifecycle("acknowledged")
    original = render(fixture)
    for field in ("lanes", "messages", "deliveries", "evidence"):
        fixture.state[field] = dict(reversed(list(fixture.state[field].items())))
    assert render(fixture) == original


def test_released_attempt_may_be_pending_without_fabricating_an_active_worker():
    fixture = Fixture()
    task = fixture.view["tasks"][0]
    task.update(status="pending", attempts=1, active_claim=None)
    result = render(fixture)
    assert any(task["status"] == "pending" and task["attempt_count"] == 1 and task["claim_state"] == "none"
               for task in result["tasks"])


def test_new_send_idempotency_metadata_is_bounded_internal_and_never_exported():
    fixture = lifecycle("queued")
    key = next(iter(fixture.state["operations"]))
    fixture.state["send_operations"] = {key: {"request_sha256": "e" * 64, "response": {
        "status": "queued", "operation_key": "synthetic-private-send-operation", "request_sha256": "e" * 64,
        "message_id": "message", "delivery_id": "delivery", "stream_sequence": 1,
        "revision": fixture.state["revision"], "delivery_state": "queued", "execution_authorized": False}}}
    result = render(fixture)
    encoded = json.dumps(result)
    assert "send_operations" not in encoded and "synthetic-private-send-operation" not in encoded and "e" * 64 not in encoded
    fixture.state["send_operations"][key]["response"]["execution_authorized"] = True
    with pytest.raises(view.WorkspaceViewError):
        render(fixture)


@pytest.mark.parametrize("mode,owner_state,verified", [
    pytest.param("passive", "unknown", False, id="passive-unknown"),
    pytest.param("passive", "not_running", False, id="passive-not-running"),
    pytest.param("passive", "stale", False, id="passive-stale"),
    pytest.param("supervised", "active", True, id="supervised-active"),
])
def test_owner_observation_is_snapshot_only_and_does_not_supply_evaluator(mode, owner_state, verified):
    fixture = Fixture()
    before = copy.deepcopy(fixture.state)
    owner = {"mode": mode, "owner_state": owner_state, "verified_active": verified}
    result = render(fixture, scope=scope(audience="operator"), supervisor=owner)
    assert result["mode"] == owner
    assert result["mode"] is not owner
    assert result["freshness"] == "snapshot_only"
    assert "Queue evaluation is unavailable on this surface." in result["queue_evaluation"]
    assert "Queueing context or reopening this workspace does not start workers." in result["queue_evaluation"]
    evaluator = next(control for control in result["controls"] if control["action"] == "evaluate_queue")
    assert evaluator == {"action": "evaluate_queue", "available": False, "reason": "not_implemented"}
    assert fixture.state == before


@pytest.mark.parametrize("owner", [
    pytest.param({"mode": "passive", "owner_state": "active", "verified_active": False}, id="passive-active"),
    pytest.param({"mode": "passive", "owner_state": "unknown", "verified_active": True}, id="passive-verified"),
    pytest.param({"mode": "supervised", "owner_state": "stale", "verified_active": True}, id="supervised-stale"),
    pytest.param({"mode": "supervised", "owner_state": "active", "verified_active": False}, id="supervised-unverified"),
])
def test_owner_presentation_does_not_relax_trusted_observation_consistency(owner):
    with pytest.raises(view.WorkspaceViewError, match="supervisor_observation_unverified"):
        render(Fixture(), supervisor=owner)


@pytest.mark.parametrize("field", ["owner_id", "path", "pid", "account"],
                         ids=["owner-id", "path", "pid", "account"])
def test_owner_presentation_rejects_raw_owner_metadata(field):
    owner = {"mode": "supervised", "owner_state": "active", "verified_active": True,
             field: "PRIVATE_OWNER_METADATA"}
    with pytest.raises(view.WorkspaceViewError) as error:
        render(Fixture(), supervisor=owner)
    assert "PRIVATE_OWNER_METADATA" not in str(error.value)
