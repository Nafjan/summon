"""Synthetic trusted-host core tests: no consumer authentication/process claim."""
import copy
import hashlib
import json

import pytest

from test_workspace_worker_send import Harness, STAMP, NOW, request, clone, used_and_reserve, receiver_turn, admit_receiver, late_proof
from test_workspace_protocol import ref
import _swarm_coordinator as swarm
import _workspace_admission as admission
import _workspace_protocol as protocol
import _workspace_state as projection


class InboxHarness(Harness):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.consumer = {"workspace_id": "workspace", "run_id": "run", "endpoint_id": "supervisor",
                         "owner_instance_id": "consumer-1", "epoch": 1,
                         "receiver_grant_id": "receiver-1", "receiver_grant_sha256": "a" * 64}
        self.command("define", {})
        self.register("receiver-1", "grant")
        self.register("owner-1", "fence")
        self.command("activate", {"receiver_grant": ref("receiver-1"), "owner_fence": ref("owner-1")},
                     receiving_grant_ref=ref("receiver-1"))
        self.scope.pop("destination_task_id")
        self.scope.pop("delivery_grant_ref")
        self.scope.update(recipient={"kind": "supervisor_inbox", "endpoint_id": "supervisor",
                                    "owner_instance_id": "consumer-1", "epoch": 1}, receiver_grant_ref=ref("receiver-1"))

    def register(self, name, category, *, delivery_id=None, target=None, role=None, endpoint_id="supervisor"):
        payload = {"reference": ref(name), "category": category, "endpoint_id": endpoint_id}
        if delivery_id:
            payload["delivery_id"] = delivery_id
        if target:
            payload["settlement_for"] = ({"delivery_id": delivery_id} if delivery_id else
                                         {"endpoint_id": endpoint_id, "epoch": self.consumer["epoch"]}) | {
                                             "target_state": target, "role": role}
        self.append_payload("workspace_evidence_registered", payload)
        return ref(name)

    def command(self, action, evidence, *, command=None, consumer="default", **extras):
        state = self.state()["workspace"]
        command = command or {"operation_key": "inbox-op-" + str(state["revision"]), "expected_revision": state["revision"],
                              "endpoint_id": "supervisor", "action": action, **extras}
        observed = self.consumer if consumer == "default" and action in admission.INBOX_LIVE_ACTIONS else None if consumer == "default" else consumer
        facts = {"consumer": observed, "evidence": evidence,
                 "receiver_grant_ref": ref(self.consumer["receiver_grant_id"]) if observed else None,
                 "lease_expires_at_ms": NOW + 90_000 if observed else None,
                 "offer_id": "offer-" + str(state["revision"]) if action == "offer" else None}
        return self.coordinator.supervisor_inbox_command(command, resolve_consumer=lambda *_: copy.deepcopy(facts))

    def send(self, intent=None, **kwargs):
        return self.coordinator.admit_supervisor_message(intent or request(), resolve_source=kwargs.get("resolve_source", self.resolve),
                                                        prepare_content=kwargs.get("prepare_content", self.prepare))

    def offer(self, delivery_id):
        proof = self.register("offer-proof-" + str(self.state()["workspace"]["revision"]), "event", delivery_id=delivery_id,
                              target="offered", role="offer_intent")
        return self.command("offer", {"receiver_grant": ref(self.consumer["receiver_grant_id"]), "offer_intent": proof}, delivery_id=delivery_id)

    def receipt(self, delivery_id, offer_id):
        proof = self.register("receipt-proof", "adapter_receipt", delivery_id=delivery_id, target="acknowledged", role="consumer_receipt")
        return self.command("receipt", {"consumer_receipt": proof}, delivery_id=delivery_id, offer_id=offer_id)

    def retire(self):
        rev = self.register("revocation-" + str(self.consumer["epoch"]), "fence", target="revoked", role="owner_revocation")
        self.command("revoke", {"owner_revocation": rev})
        fence = self.register("retirement-" + str(self.consumer["epoch"]), "fence", target="retired", role="recovery_fence")
        self.command("retire", {"recovery_fence": fence})


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: STAMP)
    return InboxHarness(tmp_path)


def test_atomic_taskless_send_receipt_retirement_replay_and_no_authority(h):
    before = h.state()
    response = h.send()
    delivery_id = response["delivery_id"]
    offered = h.offer(delivery_id)
    ack = h.receipt(delivery_id, offered["offer_id"])
    h.retire()
    state = h.state()
    assert state["tasks"] == before["tasks"] and state["claims"] == before["claims"]
    assert state["workspace"]["active_priority"] == before["workspace"]["active_priority"]
    assert ack["status"] == "acknowledged" and ack["qualification"] == "simulated"
    assert response["execution_authorized"] is False
    assert not state["workspace"]["deliveries"]
    assert projection.inbox_summary(state["workspace"])["closure_ready"] is True
    assert h.coordinator._workspace_reserve(state["workspace"]) == 0
    event = next(json.loads(line)["workspace_event"] for raw in h.journals().values() for line in raw.splitlines()
                 if json.loads(line).get("workspace_event", {}).get("event") == admission.SUPERVISOR_SEND_EVENT)
    proof = admission.send_admission_evidence(event)
    assert proof["endpoint_id"] == "supervisor" and "task_id" not in proof
    assert state["workspace"]["evidence"][proof["reference"]["id"]] == proof
    assert proof["reference"]["sha256"] == hashlib.sha256(
        b"summon.workspace.supervisor-message-admission/v1\0" + admission._canonical_event_bytes(event)).hexdigest()
    h.clock += 180
    assert h.state()["workspace"] == state["workspace"]


def test_send_duplicate_shared_identity_no_second_publication(h):
    original = h.send()
    before = h.journals()
    assert h.send() == original
    assert h.content_calls == 1 and h.journals() == before
    # Same principal/key across modes returns only the exact original outcome.
    assert h.coordinator.admit_worker_message(request(), resolve_source=h.resolve, prepare_content=h.prepare) == original
    altered = request() | {"destination_route": "other"}
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)):
        h.coordinator.admit_worker_message(altered, resolve_source=h.resolve, prepare_content=h.prepare)
    assert h.journals() == before


@pytest.mark.parametrize("case", ["both_subjects", "dangling_endpoint", "task_substitution", "wrong_category", "delivery_substitution"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_evidence_subject_category_and_delivery_refusal(h, case):
    response = h.send()
    delivery_id = response["delivery_id"]
    payload = {"reference": ref("bad"), "category": "event", "endpoint_id": "supervisor", "delivery_id": delivery_id,
               "settlement_for": {"delivery_id": delivery_id, "target_state": "offered", "role": "offer_intent"}}
    if case == "both_subjects": payload["task_id"] = "main-1"
    if case == "dangling_endpoint": payload["endpoint_id"] = "missing"
    if case == "task_substitution": payload.pop("endpoint_id"); payload["task_id"] = "main-1"
    if case == "wrong_category": payload["category"] = "artifact"
    if case == "delivery_substitution": payload["delivery_id"] = "missing"
    before = h.journals()
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)): h.append_payload("workspace_evidence_registered", payload)
    assert h.journals() == before


@pytest.mark.parametrize("case", ["expired", "wrong_owner", "wrong_grant", "null_consumer", "wrong_offer"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005'])
def test_late_or_wrong_receipt_refuses_unchanged_journal(h, case):
    delivery_id = h.send()["delivery_id"]
    offer = h.offer(delivery_id)
    proof = h.register("receipt", "adapter_receipt", delivery_id=delivery_id)
    consumer = copy.deepcopy(h.consumer)
    if case == "expired": h.clock += 100
    if case == "wrong_owner": consumer["owner_instance_id"] = "impostor"
    if case == "wrong_grant": consumer["receiver_grant_id"] = "other"
    if case == "null_consumer": consumer = None
    before = h.journals()
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)):
        h.command("receipt", {"consumer_receipt": proof}, consumer=consumer, delivery_id=delivery_id,
                  offer_id="wrong" if case == "wrong_offer" else offer["offer_id"])
    assert h.journals() == before
    assert h.state()["workspace"]["inbox_deliveries"][delivery_id]["exposure"] == "unknown"


def test_operational_duplicate_requires_current_original_consumer(h):
    delivery_id = h.send()["delivery_id"]
    proof = h.register("offer-proof", "event", delivery_id=delivery_id)
    ws = h.state()["workspace"]
    command = {"operation_key": "offer-op", "expected_revision": ws["revision"], "endpoint_id": "supervisor",
               "action": "offer", "delivery_id": delivery_id}
    evidence = {"receiver_grant": ref("receiver-1"), "offer_intent": proof}
    response = h.command("offer", evidence, command=command)
    before = h.journals()
    assert h.command("offer", evidence, command=command) == response
    h.clock += 100
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)): h.command("offer", evidence, command=command)
    assert h.journals() == before


def test_disposed_unknown_exposure_survives_retirement_and_source_obligation(h):
    delivery_id = h.send()["delivery_id"]
    h.offer(delivery_id)
    hold = h.register("hold-proof", "observation", delivery_id=delivery_id, target="held_for_recovery", role="hold_observation")
    h.command("hold", {"hold_observation": hold}, delivery_id=delivery_id, reason="consumer_lost")
    disposition = h.register("dispose-proof", "event", delivery_id=delivery_id, target="dead_lettered", role="authenticated_disposition")
    h.command("dispose", {"authenticated_disposition": disposition}, delivery_id=delivery_id, reason="explicit_disposition")
    h.retire()
    summary = h.coordinator.status()["workspace"]["inbox"]
    assert summary["closure_ready"] and summary["disposed_with_unknown_exposure"] == 1
    assert summary["unknown_exposure"] == 1 and summary["received"] == 0
    before = h.journals()
    with pytest.raises(swarm.SwarmConflictError): h.coordinator.close()
    assert h.journals() == before  # independent source claim still active


def test_later_same_stream_offer_cannot_skip_queued_or_held_predecessor(h):
    first = h.send()
    second = h.send(request() | {"operation_key": "second"})
    proof = h.register("second-offer", "event", delivery_id=second["delivery_id"])
    before = h.journals()
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)):
        h.command("offer", {"receiver_grant": ref("receiver-1"), "offer_intent": proof}, delivery_id=second["delivery_id"])
    assert h.journals() == before
    hold = h.register("hold-first", "observation", delivery_id=first["delivery_id"])
    h.command("hold", {"hold_observation": hold}, delivery_id=first["delivery_id"], reason="stalled")
    before = h.journals()
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)):
        h.command("offer", {"receiver_grant": ref("receiver-1"), "offer_intent": proof}, delivery_id=second["delivery_id"])
    assert h.journals() == before


@pytest.mark.parametrize("field,value", [("owner_instance_id", "wrong"), ("epoch", 2)], ids=['p003_case_001', 'p003_case_002'])
def test_wrong_route_recipient_fails_before_content_publication(h, field, value):
    h.scope["recipient"][field] = value
    before = h.journals()
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)): h.send()
    assert h.journals() == before and h.content_calls == 0


def test_canonical_wire_digest_same_but_proof_domain_distinct(h):
    before = h.state()
    raw = request()
    event = admission.build_supervisor_send_event(raw, h.resolve(before, before["workspace"], raw),
        h.prepare(raw, "blob-" + hashlib.sha256(raw["content"].encode()).hexdigest()[:32]), before["workspace"])
    assert event["payload"]["request"]["request_sha256"] == admission.send_request_identity(h.binding, raw)[1]
    assert admission.classify_event(event) == admission.OBLIGATION_ACCEPTED
    bad = copy.deepcopy(event); bad["payload"]["send_scope"]["destination_task_id"] = "main-2"
    with pytest.raises((swarm.SwarmCoordinatorError, admission.WorkspaceAdmissionError, projection.WorkspaceStateError, protocol.WorkspaceProtocolError)): admission.canonical_supervisor_send_event(bad)


@pytest.mark.parametrize("disposition", ["receipt", "held"], ids=['p004_case_001', 'p004_case_002'])
def test_exact_cap_late_evidence_full_inbox_and_endpoint_settlement(h, tmp_path, monkeypatch, disposition):
    calibration = clone(h, tmp_path / "calibration")
    calibration.send()
    cap = used_and_reserve(calibration)
    refused = clone(h, tmp_path / "refused")
    before = refused.journals()
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap - 1)
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        refused.send()
    assert refused.journals() == before
    fits = clone(h, tmp_path / "fits")
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap)
    response = fits.send()
    assert used_and_reserve(fits) == cap
    before = fits.journals()
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        fits.register("unrelated", "observation")
    assert fits.journals() == before
    delivery_id = response["delivery_id"]
    offered = fits.offer(delivery_id)
    assert used_and_reserve(fits) <= cap
    if disposition == "receipt":
        fits.receipt(delivery_id, offered["offer_id"])
    else:
        hold = fits.register("held", "observation", delivery_id=delivery_id, target="held_for_recovery", role="hold_observation")
        fits.command("hold", {"hold_observation": hold}, delivery_id=delivery_id, reason="consumer_lost")
        proof = fits.register("disposed", "event", delivery_id=delivery_id, target="dead_lettered", role="authenticated_disposition")
        fits.command("dispose", {"authenticated_disposition": proof}, delivery_id=delivery_id, reason="explicit_disposition")
    fits.retire()
    assert used_and_reserve(fits) <= cap
    assert fits.coordinator._workspace_reserve(fits.state()["workspace"]) == 0


def test_reserved_slot_duplicate_no_credit_and_conflicting_reference_refuses(h):
    delivery_id = h.send()["delivery_id"]
    proof = h.register("hold-first", "observation", delivery_id=delivery_id, target="held_for_recovery", role="hold_observation")
    state = h.state()
    evidence = state["workspace"]["evidence"][proof["id"]]
    slot_counts = projection.settlement_record_counts(state["workspace"])
    event = {"event": "workspace_evidence_registered", "protocol": protocol.PROTOCOL,
             "workspace_id": "workspace", "run_id": "run", "operation_key": "duplicate-proof",
             "expected_revision": state["workspace"]["revision"], "payload": evidence}
    assert projection.apply_event(state["workspace"], event, admission._coordinator_view(state, "run", now_ms=NOW)) == state["workspace"]
    before = h.journals()
    with pytest.raises(projection.WorkspaceStateError, match="immutable"):
        h.register("hold-conflict", "observation", delivery_id=delivery_id, target="held_for_recovery", role="hold_observation")
    assert h.journals() == before and projection.settlement_record_counts(h.state()["workspace"]) == slot_counts


def test_replacement_epoch_requires_hold_then_link_retains_unknown_exposure(h):
    old = h.send()["delivery_id"]
    old_offer = h.offer(old)
    fence = h.register("revoke", "fence", target="revoked", role="owner_revocation")
    h.command("revoke", {"owner_revocation": fence})
    recovery = h.register("recovery", "fence", target="retired", role="recovery_fence")
    h.consumer.update(owner_instance_id="consumer-2", epoch=2, receiver_grant_id="receiver-2")
    h.register("receiver-2", "grant")
    h.register("owner-2", "fence")
    h.command("activate", {"receiver_grant": ref("receiver-2"), "owner_fence": ref("owner-2"), "recovery_fence": recovery},
              receiving_grant_ref=ref("receiver-2"))
    h.scope.update(recipient={"kind": "supervisor_inbox", "endpoint_id": "supervisor", "owner_instance_id": "consumer-2", "epoch": 2},
                   receiver_grant_ref=ref("receiver-2"))
    new = h.send(request() | {"operation_key": "second-epoch"})["delivery_id"]
    offer_proof = h.register("new-offer-proof", "event", delivery_id=new)
    before = h.journals()
    with pytest.raises(projection.WorkspaceStateError, match="old epoch"):
        h.command("offer", {"receiver_grant": ref("receiver-2"), "offer_intent": offer_proof}, delivery_id=new)
    assert h.journals() == before
    hold = h.register("old-held", "observation", delivery_id=old, target="held_for_recovery", role="hold_observation")
    h.command("hold", {"hold_observation": hold}, delivery_id=old, reason="old_consumer_lost")
    response = h.command("link", {"fresh_receiving_grant": ref("receiver-2"), "recovery_fence": recovery},
                         parent_delivery_id=old, new_delivery_id="successor")
    linked = h.state()["workspace"]["inbox_deliveries"]["successor"]
    assert response["possible_duplicate"] is True
    assert linked["prior_offer_id"] == old_offer["offer_id"] and linked["inherited_exposure"] == "unknown"
    assert linked["exposure"] == "unknown"
    assert h.state()["workspace"]["inbox_deliveries"][old]["state"] == "held_for_recovery"


def test_message_parent_task_mode_and_other_source_refused(h):
    response = h.send()
    before = h.journals()
    state = h.state()
    intent = request() | {"operation_key": "reply", "reply_to": response["message_id"]}
    event = admission.build_supervisor_send_event(intent, h.resolve(state, state["workspace"], intent),
        h.prepare(intent, "blob-" + hashlib.sha256(intent["content"].encode()).hexdigest()[:32]), state["workspace"])
    for mode in ("message", "different_sender"):
        ws = copy.deepcopy(state["workspace"])
        if mode == "message": ws["messages"][response["message_id"]]["kind"] = "message"
        else: ws["messages"][response["message_id"]]["sender"]["instance_id"] = "different"
        with pytest.raises(projection.WorkspaceStateError, match="parent"):
            projection.apply_event(ws, event, admission._coordinator_view(state, "run", now_ms=NOW))
    assert h.journals() == before


def complete_tasks(h):
    for task_id in ("main-1", "main-2", "side-1"):
        state = h.state()
        if not state["tasks"][task_id]["attempts"]:
            h.coordinator.claim("worker-a", task_id, request_sha256="d" * 64)
            state = h.state()
        claim = state["claims"][state["tasks"][task_id]["attempts"][-1]]
        h.coordinator.complete(claim["worker_id"], **{key: claim[key] for key in (
            "task_id", "claim_id", "attempt", "lease_generation", "request_sha256")}, envelope_sha256="e" * 64)


def test_terminal_tasks_still_report_inbox_settlement_then_close_keeps_unknown(h):
    delivery_id = h.send()["delivery_id"]
    h.offer(delivery_id)
    complete_tasks(h)
    assert h.coordinator.status()["status"] == "settlement_required"
    before = h.journals()
    with pytest.raises(swarm.SwarmConflictError, match="workspace"):
        h.coordinator.close()
    assert h.journals() == before
    hold = h.register("late-hold", "observation", delivery_id=delivery_id, target="held_for_recovery", role="hold_observation")
    h.command("hold", {"hold_observation": hold}, delivery_id=delivery_id, reason="lost_consumer")
    disposition = h.register("late-dispose", "event", delivery_id=delivery_id, target="dead_lettered", role="authenticated_disposition")
    h.command("dispose", {"authenticated_disposition": disposition}, delivery_id=delivery_id, reason="context_disposed")
    h.retire()
    assert h.coordinator.close()["status"] == "closed"
    status = h.coordinator.status()
    assert status["status"] == "closed" and status["workspace"]["inbox"]["disposed_with_unknown_exposure"] == 1


def test_disposed_inbox_does_not_resolve_provider_uncertainty_after_all_tasks_terminal(h):
    inbox_id = h.send()["delivery_id"]
    h.offer(inbox_id)
    hold = h.register("inbox-hold", "observation", delivery_id=inbox_id)
    h.command("hold", {"hold_observation": hold}, delivery_id=inbox_id, reason="lost_consumer")
    proof = h.register("inbox-dispose", "event", delivery_id=inbox_id)
    h.command("dispose", {"authenticated_disposition": proof}, delivery_id=inbox_id, reason="context_disposed")
    h.retire()
    def source(state, workspace, intent):
        resolved = h.resolve(state, workspace, intent)
        scope = resolved["send_scope"]
        scope.pop("receiver_grant_ref")
        scope.update(destination_task_id="main-2", recipient={"instance_id": "instance-b", "epoch": 1},
                     delivery_grant_ref=ref("delivery-grant"))
        return resolved
    sent = h.coordinator.admit_worker_message(request() | {"operation_key": "ordinary-independent"},
                                              resolve_source=source, prepare_content=h.prepare)
    event, grant = receiver_turn(h, sent)
    admit_receiver(h, event, grant)
    delivery_id = sent["delivery_id"]
    evidence = event["payload"]["evidence_by_delivery"][delivery_id]
    launch = {role: evidence[role] for role in ("current_grant", "recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation")}
    launch["durable_launch_intent"] = late_proof(h, delivery_id, "submission_started", "durable_launch_intent", "event")
    after = copy.deepcopy(h.state()["workspace"]["deliveries"][delivery_id])
    after.update(state="submission_started", certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
    h.append_payload("workspace_delivery_advanced", {"delivery": after, "evidence": launch, "supported_ack_levels": []})
    complete_tasks(h)
    status = h.coordinator.status()
    assert all(task["status"] == "completed" for task in status["tasks"])
    assert status["workspace"]["inbox"]["closure_ready"] and status["uncertain_spend"]
    before = h.journals()
    with pytest.raises(swarm.SwarmConflictError, match="workspace"):
        h.coordinator.close()
    assert h.journals() == before


def test_combined_127_task_plus_one_inbox_quota_is_shared_pure_guard():
    """Guard fixture only: does not claim this snapshot is reachable under evidence quota."""
    from test_workspace_protocol import delivery, inbox_record
    state = {"deliveries": {}, "inbox_deliveries": {}}
    for index in range(127):
        record = delivery("cancelled")
        record["delivery_id"] = "ordinary-" + str(index)
        projection._new_delivery(state, protocol.validate_record(record))
    projection._new_delivery(state, protocol.validate_record(inbox_record()))
    assert len(state["deliveries"]) == 127 and len(state["inbox_deliveries"]) == 1
    before = copy.deepcopy(state)
    for record in (delivery("cancelled"), inbox_record()):
        record["delivery_id"] = "overflow"
        with pytest.raises(projection.WorkspaceStateError, match="combined delivery capacity"):
            projection._new_delivery(state, protocol.validate_record(record))
        assert state == before


def test_last_endpoint_revision_refuses_activation_preserves_retirement(h):
    with h.coordinator._mutation() as (owner, state, _):
        def append(event):
            record = {"event": "workspace_event", "workspace_event": event, "observed_at_ms": NOW}
            fingerprint = h.coordinator._append_with_state(owner, record, state=state)
            swarm.SwarmCoordinator._apply_record(state, record)
            state["_prefix_fingerprint"] = fingerprint
        def register(name, category):
            ws = state["workspace"]
            append({"event": "workspace_evidence_registered", "protocol": protocol.PROTOCOL,
                    "workspace_id": "workspace", "run_id": "run", "operation_key": "revision-" + str(ws["revision"]),
                    "expected_revision": ws["revision"], "payload": {"reference": ref(name), "category": category, "endpoint_id": "supervisor"}})
            return ref(name)
        def command(action, evidence, consumer=None):
            ws = state["workspace"]
            cmd = {"operation_key": "revision-" + str(ws["revision"]), "expected_revision": ws["revision"],
                   "endpoint_id": "supervisor", "action": action}
            if action == "activate": cmd["receiving_grant_ref"] = ref(consumer["receiver_grant_id"])
            resolved = {"consumer": consumer, "evidence": evidence, "receiver_grant_ref": cmd.get("receiving_grant_ref"),
                        "lease_expires_at_ms": NOW + 90_000 if consumer else None, "offer_id": None}
            append(admission.build_inbox_command_event(cmd, resolved, ws))
        for epoch in range(2, 32):
            revoke = register("revision-revoke-" + str(epoch), "fence")
            command("revoke", {"owner_revocation": revoke})
            recovery = register("revision-recovery-" + str(epoch), "fence")
            receiver = register("revision-receiver-" + str(epoch), "grant")
            owner_fence = register("revision-owner-" + str(epoch), "fence")
            consumer = copy.deepcopy(h.consumer)
            consumer.update(epoch=epoch, owner_instance_id="consumer-" + str(epoch), receiver_grant_id=receiver["id"])
            command("activate", {"receiver_grant": receiver, "owner_fence": owner_fence, "recovery_fence": recovery}, consumer)
        assert state["workspace"]["supervisor_endpoints"]["supervisor"]["revision"] == 62
        revoke = register("last-revoke", "fence")
        command("revoke", {"owner_revocation": revoke})
        recovery = register("last-recovery", "fence")
        receiver = register("last-receiver", "grant")
        owner_fence = register("last-owner", "fence")
        consumer.update(epoch=32, owner_instance_id="consumer-32", receiver_grant_id=receiver["id"])
        before = h.journals()
        with pytest.raises(protocol.WorkspaceProtocolError, match="revision capacity"):
            command("activate", {"receiver_grant": receiver, "owner_fence": owner_fence, "recovery_fence": recovery}, consumer)
        assert h.journals() == before
        command("retire", {"recovery_fence": recovery})
        assert state["workspace"]["supervisor_endpoints"]["supervisor"]["revision"] == 64
        assert h.coordinator._workspace_reserve(state["workspace"]) == 0
    assert h.state()["workspace"]["supervisor_endpoints"]["supervisor"]["status"] == "retired"


def test_real_evidence_quota_refusal_preserves_required_settlement(h):
    delivery_id = h.send()["delivery_id"]
    with h.coordinator._mutation() as (owner, state, _):
        for index in range(projection.MAX_EVIDENCE):
            ws = state["workspace"]
            event = {"event": "workspace_evidence_registered", "protocol": protocol.PROTOCOL,
                     "workspace_id": "workspace", "run_id": "run", "operation_key": "evidence-limit-" + str(index),
                     "expected_revision": ws["revision"], "payload": {
                         "reference": ref("evidence-limit-" + str(index)), "category": "observation", "endpoint_id": "supervisor"}}
            record = {"event": "workspace_event", "workspace_event": event, "observed_at_ms": NOW}
            before = h.journals()
            try:
                fingerprint = h.coordinator._append_with_state(owner, record, state=state)
            except swarm.SwarmCoordinatorError as exc:
                assert "evidence capacity" in str(exc)
                assert h.journals() == before
                assert len(ws["evidence"]) + projection.settlement_record_counts(ws)[1] == projection.MAX_EVIDENCE
                break
            swarm.SwarmCoordinator._apply_record(state, record)
            state["_prefix_fingerprint"] = fingerprint
        else:
            pytest.fail("evidence bound was not enforced")
    # Proofs first obtained after the failed unrelated registration still fit
    # the explicitly reserved slots; no limit or source truth is overridden.
    offered = h.offer(delivery_id)
    h.receipt(delivery_id, offered["offer_id"])
    h.retire()
    assert h.coordinator._workspace_reserve(h.state()["workspace"]) == 0
    complete_tasks(h)
    assert h.coordinator.close()["status"] == "closed"
