"""Real journal/reopen fixtures paired with runtime's actual consumer recovery.

Core callbacks and evidence here are synthetic trusted-host facts, not process
authentication. No providers, scheduler, alternate store or approval inference.
"""
import copy

import pytest

import _swarm_coordinator as swarm
import _workspace_protocol as protocol
import _workspace_state as projection
import _workspace_admission as admission
from test_workspace_inbox import InboxHarness, complete_tasks
from test_workspace_worker_send import STAMP, NOW, request, clone, used_and_reserve
from test_workspace_protocol import ref


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: STAMP)
    return InboxHarness(tmp_path)


def hold(h, delivery_id):
    proof = h.register("recovery-hold", "observation", delivery_id=delivery_id,
                       target="held_for_recovery", role="hold_observation")
    h.command("hold", {"hold_observation": proof}, delivery_id=delivery_id, reason="owner_restarted")


def dispose(h, delivery_id):
    proof = h.register("recovery-disposition", "event", delivery_id=delivery_id,
                       target="dead_lettered", role="authenticated_disposition")
    h.command("dispose", {"authenticated_disposition": proof}, delivery_id=delivery_id, reason="explicit_old_owner_disposition")


def replace_endpoint(h):
    revoke = h.register("recovery-revoke", "fence", target="revoked", role="owner_revocation")
    h.command("revoke", {"owner_revocation": revoke})
    recovery = h.register("recovery-fence", "fence", target="retired", role="recovery_fence")
    h.register("receiver-2", "grant")
    h.register("owner-fence-2", "fence")
    h.consumer.update(owner_instance_id="consumer-2", epoch=2, receiver_grant_id="receiver-2")
    h.command("activate", {"receiver_grant": ref("receiver-2"), "owner_fence": ref("owner-fence-2"), "recovery_fence": recovery},
              receiving_grant_ref=ref("receiver-2"))
    return recovery


def link_command(h, parent_id):
    return {"action": "link", "operation_key": "explicit-recovery-link", "endpoint_id": "supervisor",
            "expected_revision": h.state()["workspace"]["revision"], "parent_delivery_id": parent_id, "new_delivery_id": "recovered-delivery"}


@pytest.mark.parametrize("boundary", ["queued", "offered"], ids=['p001_case_001', 'p001_case_002'])
def test_reopened_parent_explicit_disposition_and_linked_receipt_preserve_history(h, boundary):
    parent_id = h.send()["delivery_id"]
    original_message = copy.deepcopy(next(iter(h.state()["workspace"]["messages"].values())))
    old_consumer = copy.deepcopy(h.consumer)
    if boundary == "offered": h.offer(parent_id)
    original = copy.deepcopy(h.state()["workspace"]["inbox_deliveries"][parent_id])
    before = h.journals()
    h.coordinator = swarm.SwarmCoordinator(h.coordinator.runs_root, "run", clock=lambda: h.clock)
    assert h.state()["workspace"]["inbox_deliveries"][parent_id] == original
    assert h.journals() == before
    hold(h, parent_id)
    dispose(h, parent_id)
    recovery = replace_endpoint(h)
    parent = copy.deepcopy(h.state()["workspace"]["inbox_deliveries"][parent_id])
    command = link_command(h, parent_id)
    evidence = {"fresh_receiving_grant": ref("receiver-2"), "recovery_fence": recovery}
    linked = h.command("link", evidence, command=command)
    before = h.journals()
    assert h.command("link", evidence, command=command) == linked and h.journals() == before
    with pytest.raises(swarm.SwarmConflictError, match="key reused"):
        h.command("link", evidence, command=command | {"new_delivery_id": "second-child"})
    assert h.journals() == before
    child_id = linked["delivery_id"]
    offered = h.offer(child_id)
    h.receipt(child_id, offered["offer_id"])
    current = h.state()["workspace"]
    child = current["inbox_deliveries"][child_id]
    assert current["inbox_deliveries"][parent_id] == parent
    assert current["messages"][original_message["message_id"]] == original_message
    assert child["state"] == "acknowledged" and child["exposure"] == "consumer_received"
    assert child["possible_duplicate"] is (boundary == "offered")
    assert child["inherited_exposure"] == ("unknown" if boundary == "offered" else None)
    assert child["prior_offer_id"] == (original["offer"]["offer_id"] if boundary == "offered" else None)
    assert protocol.validate_inbox_lineage(parent, child) == child
    # A consistent old binding is stale authority, not merely malformed JSON.
    receipt_query = {"operation_key": "stale-consumer-receipt", "endpoint_id": "supervisor", "action": "receipt",
                     "expected_revision": current["revision"], "delivery_id": child_id, "offer_id": offered["offer_id"]}
    old_facts = {"consumer": old_consumer, "evidence": {}, "receiver_grant_ref": ref("receiver-1"),
                 "lease_expires_at_ms": NOW + 90_000, "offer_id": None}
    before = h.journals()
    with pytest.raises((protocol.WorkspaceProtocolError, swarm.SwarmProtocolError, admission.WorkspaceAdmissionError), match="current|binding"):
        h.coordinator.supervisor_inbox_command(receipt_query, resolve_consumer=lambda *_: old_facts)
    assert h.journals() == before
    h.retire()
    complete_tasks(h)
    assert h.coordinator.close()["status"] == "closed"
    replay = swarm.SwarmCoordinator(h.coordinator.runs_root, "run", clock=lambda: h.clock)
    final = replay._load()[0]["workspace"]
    assert final["inbox_deliveries"][parent_id] == parent and final["inbox_deliveries"][child_id] == child
    assert final["messages"][original_message["message_id"]] == original_message
    assert projection.inbox_summary(final)["unknown_exposure"] == (2 if boundary == "offered" else 0)


def test_old_epoch_other_sender_same_endpoint_blocks_until_explicit_hold(h):
    old_id = h.send()["delivery_id"]
    replace_endpoint(h)
    h.coordinator.claim("worker-b", "side-1", request_sha256="d" * 64)
    for name in ("side-channel", "side-send"):
        h.append_payload("workspace_evidence_registered", {"reference": ref(name), "category": "grant", "task_id": "side-1"})
    def source(state, workspace, intent):
        claim = state["claims"][state["tasks"]["side-1"]["attempts"][-1]]
        binding = {"workspace_id": "workspace", "run_id": "run", "instance_id": "instance-b", "epoch": 1,
                   "task_id": "side-1", "grant_id": "side-channel", "grant_sha256": "a" * 64}
        scope = {**h.scope, "reference": ref("side-send"), "source_task_id": "side-1",
                 "sender": {"instance_id": "instance-b", "epoch": 1}, "channel_grant_ref": ref("side-channel"),
                 "recipient": {"kind": "supervisor_inbox", "endpoint_id": "supervisor", "owner_instance_id": "consumer-2", "epoch": 2},
                 "receiver_grant_ref": ref("receiver-2")}
        return {"binding": binding, "claim": copy.deepcopy(claim), "send_scope": scope}
    sent = h.send(request() | {"operation_key": "side-message", "destination_route": "side-send"}, resolve_source=source)
    proof = h.register("side-offer", "event", delivery_id=sent["delivery_id"])
    before = h.journals()
    with pytest.raises(projection.WorkspaceStateError, match="old epoch"):
        h.command("offer", {"receiver_grant": ref("receiver-2"), "offer_intent": proof}, delivery_id=sent["delivery_id"])
    assert h.journals() == before
    hold(h, old_id)
    offered = h.command("offer", {"receiver_grant": ref("receiver-2"), "offer_intent": proof}, delivery_id=sent["delivery_id"])
    assert offered["status"] == "offered"


def test_offer_order_does_not_cross_endpoint_boundary_pure_scope_guard():
    # The shipped slice still caps endpoints at one. This directly tests the
    # guard's scope without constructing a fictitious two-endpoint journal.
    from test_workspace_protocol import inbox_record
    delivery = inbox_record("queued", epoch=2)
    unrelated = inbox_record("queued")
    unrelated.update(delivery_id="foreign-delivery", message_id="foreign-message")
    unrelated["recipient"]["endpoint_id"] = "foreign-endpoint"
    workspace = {"messages": {delivery["message_id"]: {"source_task_id": "main-1", "stream_id": "stream", "sequence": 2},
                              unrelated["message_id"]: {"source_task_id": "main-1", "stream_id": "stream", "sequence": 1}},
                 "inbox_deliveries": {delivery["delivery_id"]: delivery, unrelated["delivery_id"]: unrelated}}
    assert projection.validate_inbox_offer_order(workspace, delivery) is None


def test_exact_cap_link_admission_keeps_parent_child_and_endpoint_settlement(h, tmp_path, monkeypatch):
    parent_id = h.send()["delivery_id"]
    h.offer(parent_id)
    hold(h, parent_id)
    recovery = replace_endpoint(h)
    command = link_command(h, parent_id)
    evidence = {"fresh_receiving_grant": ref("receiver-2"), "recovery_fence": recovery}
    calibration = clone(h, tmp_path / "link-calibration")
    calibration.command("link", evidence, command=command)
    cap = used_and_reserve(calibration)
    denied = clone(h, tmp_path / "link-denied")
    before = denied.journals()
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap - 1)
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        denied.command("link", evidence, command=command)
    assert denied.journals() == before
    assert "recovered-delivery" not in denied.state()["workspace"]["inbox_deliveries"]
    fit = clone(h, tmp_path / "link-fit")
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap)
    response = fit.command("link", evidence, command=command)
    assert used_and_reserve(fit) == cap
    dispose(fit, parent_id)
    child_id = response["delivery_id"]
    cancellation = fit.register("child-cancel", "grant", delivery_id=child_id, target="cancelled", role="authorized_cancellation")
    fit.command("cancel", {"authorized_cancellation": cancellation}, delivery_id=child_id, reason="explicit_child_disposition")
    fit.retire()
    assert used_and_reserve(fit) <= cap
    final = fit.state()["workspace"]
    assert fit.coordinator._workspace_reserve(final) == 0
    assert final["inbox_deliveries"][parent_id]["exposure"] == "unknown"
    assert final["inbox_deliveries"][child_id]["inherited_exposure"] == "unknown"
    assert projection.inbox_summary(final)["disposed_with_unknown_exposure"] == 2
