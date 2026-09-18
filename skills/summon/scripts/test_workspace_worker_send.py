"""Provider-inert pure/base composite qualification, not worker authentication.

Content/source callbacks below are explicit synthetic trusted-host fixtures.
Actual owned-process request ingress belongs to the runtime/transport gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _rundir as rd
import _swarm_coordinator as swarm
import _workspace_admission as admission
import _workspace_protocol as protocol
import _workspace_state as projection
from test_workspace_state import Fixture, add_hold
from test_workspace_protocol import ref

STAMP = 2_000_000_000.25
NOW = int(STAMP * 1000)


def request():
    return {"schema": admission.SEND_SCHEMA, "operation_key": "worker-send-1",
            "destination_route": "send-scope", "observed_grant_revision": 1,
            "content": "[1, 2, 3]", "correlation_id": "investigation"}


class Harness:
    def __init__(self, tmp_path):
        self.clock = STAMP
        self.coordinator = swarm.SwarmCoordinator.create(
            tmp_path / "runs", "run", project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64,
            tasks=[{"task_id": name, "request_sha256": "d" * 64}
                   for name in ("main-1", "side-1", "main-2")], clock=lambda: self.clock)
        self.coordinator.register_worker("worker-a", worker_instance_id="instance-a")
        self.coordinator.register_worker("worker-b", worker_instance_id="instance-b")
        self.coordinator.claim("worker-a", "main-1", request_sha256="d" * 64, lease_ms=30_000)
        fixture = Fixture()
        fixture.register(ref("channel-grant"), task_id="main-1", category="grant")
        fixture.register(ref("send-scope"), task_id="main-1", category="grant")
        fixture.register(ref("delivery-grant"), task_id="main-2", category="grant")
        for event, _ in fixture.steps:
            self.append(event)
        self.binding = {"workspace_id": "workspace", "run_id": "run", "instance_id": "instance-a", "epoch": 1,
                        "task_id": "main-1", "grant_id": "channel-grant", "grant_sha256": "a" * 64}
        self.scope = {"reference": ref("send-scope"), "revision": 1, "expires_at_ms": NOW + 60_000,
                      "revoked": False, "operation": "message.send", "goal_id": "goal",
                      "source_task_id": "main-1", "sender": {"instance_id": "instance-a", "epoch": 1},
                      "channel_grant_ref": ref("channel-grant"), "destination_task_id": "main-2",
                      "recipient": {"instance_id": "instance-b", "epoch": 1}, "delivery_grant_ref": ref("delivery-grant")}
        self.source_calls = 0
        self.content_calls = 0
        self.blobs = {}

    def state(self):
        return self.coordinator._load()[0]

    def journals(self):
        return {p.name: p.read_bytes() for p in Path(self.coordinator.run_dir).glob("journal-g*.jsonl")}

    def resolve(self, state, workspace, intent):
        self.source_calls += 1
        assert state["workspace"] == workspace
        claim_id = state["tasks"]["main-1"]["attempts"][-1]
        return {"binding": copy.deepcopy(self.binding), "claim": copy.deepcopy(state["claims"][claim_id]),
                "send_scope": copy.deepcopy(self.scope)}

    def prepare(self, intent, content_id):
        self.content_calls += 1
        raw = intent["content"].encode("utf-8")
        if content_id in self.blobs:
            assert self.blobs[content_id] == raw
        self.blobs[content_id] = raw
        return {"ref": content_id, "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}

    def send(self, intent=None, **kwargs):
        return self.coordinator.admit_worker_message(
            intent or request(), resolve_source=kwargs.get("resolve_source", self.resolve),
            prepare_content=kwargs.get("prepare_content", self.prepare))

    def event(self, intent=None):
        intent = intent or request()
        state = self.state()
        return admission.build_send_event(intent, self.resolve(state, state["workspace"], intent),
                                           self.prepare(intent, "blob-" + hashlib.sha256(intent["content"].encode()).hexdigest()[:32]),
                                           state["workspace"])

    def append(self, event):
        with self.coordinator._mutation() as (owner, state, _):
            self.coordinator._append_with_state(owner, {"event": "workspace_event", "workspace_event": copy.deepcopy(event),
                                                       "observed_at_ms": int(self.clock * 1000)}, state=state)

    def append_payload(self, kind, payload):
        state = self.state()["workspace"]
        event = {"event": kind, "protocol": protocol.PROTOCOL, "workspace_id": "workspace", "run_id": "run",
                 "operation_key": "test-op-" + str(state["revision"]), "expected_revision": state["revision"], "payload": payload}
        self.append(event)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: STAMP)
    return Harness(tmp_path)


def test_one_composite_queues_registers_derived_proof_and_replays_without_claim_change(h):
    before = h.state()
    response = h.send()
    after = h.state()
    ws = after["workspace"]
    assert response["status"] == response["delivery_state"] == "queued"
    assert response["execution_authorized"] is False
    assert before["tasks"] == after["tasks"] and before["claims"] == after["claims"]
    assert len(ws["messages"]) == len(ws["deliveries"]) == len(ws["send_operations"]) == 1
    assert ws["revision"] == before["workspace"]["revision"] + 1
    records = [json.loads(line) for raw in h.journals().values() for line in raw.splitlines()]
    composite = [record for record in records if record.get("workspace_event", {}).get("event") == admission.ATOMIC_SEND_EVENT]
    assert len(composite) == 1
    event = composite[0]["workspace_event"]
    evidence = admission.send_admission_evidence(event)
    assert ws["evidence"][evidence["reference"]["id"]] == evidence
    assert evidence["reference"]["sha256"] == hashlib.sha256(
        b"summon.workspace.message-admission/v1\0" + admission._canonical_event_bytes(event)).hexdigest()
    assert ws["deliveries"][response["delivery_id"]]["state"] == "queued"
    assert composite[0]["workspace_event"]["payload"]["delivery"]["state"] == "accepted"
    assert h.source_calls == 2 and h.content_calls == 1
    h.clock += 60
    assert h.state()["workspace"] == ws  # historical time, not live expired lease


def test_lost_response_retry_is_same_result_no_append_or_blob_even_after_lease_expiry(h):
    first = h.send()
    snapshot = h.journals()
    h.clock += 60
    assert h.send() == first
    assert h.journals() == snapshot and h.content_calls == 1
    assert len(h.state()["workspace"]["send_operations"]) == 1


@pytest.mark.parametrize("change", ["content", "destination_route", "correlation_id", "observed_grant_revision"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_conflicting_semantic_operation_refuses_before_publication(h, change):
    h.send()
    before = h.journals()
    intent = request()
    intent[change] = 2 if change == "observed_grant_revision" else "different"
    with pytest.raises(swarm.SwarmConflictError):
        h.send(intent)
    assert h.journals() == before and h.content_calls == 1


@pytest.mark.parametrize("change", ["unknown_sender", "boolean_revision", "nonfinite", "unicode_oversize", "unknown_schema"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005'])
def test_invalid_request_refuses_without_callbacks_or_journal_mutation(h, change):
    intent = request()
    if change == "unknown_sender":
        intent["sender"] = "supervisor"
    elif change == "boolean_revision":
        intent["observed_grant_revision"] = True
    elif change == "nonfinite":
        intent["observed_grant_revision"] = float("nan")
    elif change == "unicode_oversize":
        intent["content"] = "😀" * 1025
    else:
        intent["schema"] = "next/v9"
    before = h.journals()
    with pytest.raises(admission.WorkspaceAdmissionError):
        h.send(intent)
    assert h.journals() == before and h.source_calls == h.content_calls == 0


def test_exact_unicode_content_bound_and_two_message_sequence(h):
    intent = request()
    intent["content"] = "😀" * 1024
    first = h.send(intent)
    intent["operation_key"] = "worker-send-2"
    intent["reply_to"] = first["message_id"]
    second = h.send(intent)
    assert second["stream_sequence"] == first["stream_sequence"] + 1
    assert len(h.blobs) == 1


@pytest.mark.parametrize("change", ["revoked", "expired", "wrong_operation", "wrong_epoch", "wrong_recipient",
                                    "wrong_task", "wrong_claim", "cancelled_claim", "wrong_grant_category"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007', 'p003_case_008', 'p003_case_009'])
def test_current_authority_mismatches_refuse_with_unchanged_journal(h, change):
    def resolve(state, workspace, intent):
        source = h.resolve(state, workspace, intent)
        if change == "revoked": source["send_scope"]["revoked"] = True
        elif change == "expired": source["send_scope"]["expires_at_ms"] = NOW
        elif change == "wrong_operation": source["send_scope"]["operation"] = "launch"
        elif change == "wrong_epoch": source["binding"]["epoch"] = 2
        elif change == "wrong_recipient": source["send_scope"]["recipient"] = {"instance_id": "other", "epoch": False}
        elif change == "wrong_task": source["send_scope"]["source_task_id"] = "side-1"
        elif change == "wrong_claim": source["claim"]["claim_id"] = "non-current"
        elif change == "cancelled_claim": source["claim"]["cancel_requested"] = True
        else: source["send_scope"]["reference"] = ref("artifact-route")
        return source
    intent = request()
    if change == "wrong_grant_category":
        h.append_payload("workspace_evidence_registered", {"reference": ref("artifact-route"), "category": "artifact", "task_id": "main-1"})
        intent["destination_route"] = "artifact-route"
    before = h.journals()
    with pytest.raises((ValueError, swarm.SwarmCoordinatorError)):
        h.send(intent, resolve_source=resolve)
    assert h.journals() == before


def test_content_callback_is_outside_owner_and_final_recheck_detects_revocation(h, monkeypatch):
    active = False
    original = h.coordinator._mutation
    @contextmanager
    def watched():
        nonlocal active
        with original() as value:
            active = True
            try: yield value
            finally: active = False
    monkeypatch.setattr(h.coordinator, "_mutation", watched)
    def prepare(intent, name):
        assert active is False
        result = h.prepare(intent, name)
        h.scope["revoked"] = True
        return result
    before = h.journals()
    with pytest.raises(admission.WorkspaceAdmissionError):
        h.send(prepare_content=prepare)
    assert len(h.blobs) == 1 and h.journals() == before


def test_wrong_content_descriptor_and_publication_failure_never_admit(h):
    before = h.journals()
    for prepare in (lambda intent, name: {"ref": name, "sha256": "0" * 64, "utf8_bytes": 1},
                    lambda intent, name: (_ for _ in ()).throw(OSError("synthetic publication failure"))):
        with pytest.raises(swarm.SwarmCoordinatorError):
            h.send(prepare_content=prepare)
        assert h.journals() == before


def test_held_destination_can_receive_context_without_releasing_hold_or_grant(h):
    f = Fixture()
    f.state = h.state()["workspace"]
    f.steps = [(None, None)] * f.state["revision"]
    add_hold(f, ["main-2"])
    for event, _ in f.steps[h.state()["workspace"]["revision"]:]:
        h.append(event)
    before = h.state()
    h.send()
    after = h.state()
    assert projection.unresolved_holds(after["workspace"], "main-2") == projection.unresolved_holds(before["workspace"], "main-2")
    assert after["tasks"] == before["tasks"]


@pytest.mark.parametrize("change", ["queued_initial", "extra_evidence", "wrong_digest", "wrong_stream", "dangling_reply", "stale_source_lease"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005', 'p004_case_006'])
def test_raw_composite_counterexamples_cannot_bypass_central_replay(h, change):
    event = h.event()
    if change == "queued_initial": event["payload"]["delivery"]["state"] = "queued"
    elif change == "extra_evidence": event["payload"]["admission_evidence"] = ref("invented")
    elif change == "wrong_digest": event["payload"]["request"]["request_sha256"] = "0" * 64
    elif change == "wrong_stream": event["payload"]["message"]["sequence"] = 2
    elif change == "dangling_reply":
        intent = request(); intent["reply_to"] = "missing"; event = h.event(intent)
    else: event["payload"]["source"]["claim"]["lease_expires_at_ms"] += 1
    before = h.journals()
    with pytest.raises((ValueError, swarm.SwarmCoordinatorError)):
        h.append(event)
    assert h.journals() == before


def test_send_index_and_generated_evidence_bounds_are_enforced_purely(h, monkeypatch):
    event = h.event()
    state = h.state()
    view = admission._coordinator_view(state, "run", now_ms=NOW)
    with monkeypatch.context() as patch:
        patch.setattr(projection, "MAX_MESSAGES", 0)
        with pytest.raises(projection.WorkspaceStateError):
            projection.apply_event(state["workspace"], event, view)
    with monkeypatch.context() as patch:
        patch.setattr(projection, "MAX_EVIDENCE", len(state["workspace"]["evidence"]))
        with pytest.raises(projection.WorkspaceStateError):
            projection.apply_event(state["workspace"], event, view)


def test_empty_descriptor_cannot_be_disguised_as_valid_semantic_digest(h):
    event = h.event()
    payload = event["payload"]
    payload["message"]["content"].update(sha256=hashlib.sha256(b"").hexdigest(), utf8_bytes=0)
    _, digest = admission._send_identity(payload["source"]["binding"], payload["request"], payload["message"]["content"])
    payload["request"]["request_sha256"] = digest
    before = h.journals()
    with pytest.raises(admission.WorkspaceAdmissionError, match="bounded"):
        h.append(event)
    assert h.journals() == before


@pytest.mark.parametrize("relation", ["reply_to", "causation_id"], ids=['p005_case_001', 'p005_case_002'])
def test_existing_unrelated_lane_message_is_not_an_authorized_logical_parent(h, relation):
    payload = h.event()["payload"]
    message, delivery = payload["message"], payload["delivery"]
    message.update(message_id="unrelated-message", source_task_id="side-1", stream_id="unrelated-stream",
                   sender={"instance_id": "unrelated-worker", "epoch": 1})
    delivery.update(message_id="unrelated-message", delivery_id="unrelated-delivery")
    h.append_payload("workspace_message_admitted", {"message": message, "delivery": delivery})
    intent = request()
    intent[relation] = "unrelated-message"
    before = h.journals()
    with pytest.raises(projection.WorkspaceStateError, match="endpoint pair"):
        h.send(intent)
    assert h.journals() == before


def test_base_capability_does_not_claim_installed_authenticated_ingress():
    assert "workspace_message_sent" not in swarm.SwarmCoordinator.workspace_admission_contract()["event_classes"]
    contract = swarm.SwarmCoordinator.workspace_worker_send_contract()
    assert contract["atomic_message_queue"] is True and contract["authenticated_worker_ingress"] is False


def clone(h, destination):
    shutil.copytree(h.coordinator.runs_root, destination)
    result = copy.copy(h)
    result.coordinator = swarm.SwarmCoordinator(destination, "run", clock=lambda: result.clock)
    result.source_calls = result.content_calls = 0
    result.blobs = {}
    return result


def used_and_reserve(h):
    state = h.state()
    return sum(map(len, h.journals().values())) + h.coordinator._reserve_after(state, {"event": "message_posted"})


def test_exact_complete_composite_cap_and_late_cancellation_proof_settle_same_budget(h, tmp_path, monkeypatch):
    calibration = clone(h, tmp_path / "calibration")
    calibration.send()
    cap = used_and_reserve(calibration)
    before_bytes = sum(map(len, h.journals().values()))
    records = [json.loads(line) for raw in calibration.journals().values() for line in raw.splitlines()]
    record = next(item for item in records if item.get("workspace_event", {}).get("event") == admission.ATOMIC_SEND_EVENT)
    frozen = dict(record); frozen.pop("sha256")
    assert sum(map(len, calibration.journals().values())) - before_bytes == len(rd.encode_journal_record(frozen, timestamp=frozen["ts"]))
    too_small = clone(h, tmp_path / "too-small")
    before = too_small.journals()
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap - 1)
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        too_small.send()
    assert too_small.journals() == before
    assert too_small.state()["workspace"]["messages"] == {}
    assert too_small.state()["workspace"]["send_operations"] == {}

    fits = clone(h, tmp_path / "fits")
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap)
    response = fits.send()
    assert used_and_reserve(fits) == cap
    assert fits.journals() == calibration.journals()
    before = fits.journals()
    assert fits.send() == response and fits.journals() == before
    assert used_and_reserve(fits) == cap
    # Untagged later observations cannot borrow delivery settlement capacity.
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        fits.append_payload("workspace_evidence_registered", {"reference": ref("unrelated"), "category": "observation", "task_id": "main-2"})
    assert fits.journals() == before
    delivery_id = response["delivery_id"]
    cancellation = {"reference": ref("actual-cancel"), "category": "grant", "task_id": "main-2", "delivery_id": delivery_id,
                    "settlement_for": {"delivery_id": delivery_id, "target_state": "cancelled", "role": "authorized_cancellation"}}
    fits.append_payload("workspace_evidence_registered", cancellation)
    assert used_and_reserve(fits) <= cap
    before_duplicate = fits.journals()
    # Identical slot under a different event key is a semantic no-op; the base
    # must not grant more reserve credit even if it journals that bounded no-op.
    state = fits.state()["workspace"]
    slot_count = projection.settlement_record_counts(state)
    duplicate_event = {"event": "workspace_evidence_registered", "protocol": protocol.PROTOCOL,
                       "workspace_id": "workspace", "run_id": "run", "operation_key": "same-slot",
                       "expected_revision": state["revision"], "payload": cancellation}
    assert projection.apply_event(state, duplicate_event, admission._coordinator_view(fits.state(), "run", now_ms=NOW)) == state
    assert projection.settlement_record_counts(state) == slot_count
    assert fits.journals() == before_duplicate
    after = copy.deepcopy(state["deliveries"][delivery_id])
    after.update(state="cancelled", reason="explicit_synthetic_cancellation")
    fits.append_payload("workspace_delivery_advanced", {"delivery": after,
                        "evidence": {"authorized_cancellation": cancellation["reference"]}, "supported_ack_levels": []})
    assert used_and_reserve(fits) <= cap
    assert fits.coordinator._workspace_reserve(fits.state()["workspace"]) == 0
    assert fits.state()["workspace"]["deliveries"][delivery_id]["certainty"] == {
        "contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}


def test_source_epoch_or_lease_change_during_content_preparation_refuses(h):
    before = h.journals()
    def changed_epoch(intent, name):
        descriptor = h.prepare(intent, name)
        h.binding["epoch"] = 2
        h.scope["sender"]["epoch"] = 2
        return descriptor
    with pytest.raises(swarm.SwarmConflictError, match="binding"):
        h.send(prepare_content=changed_epoch)
    assert h.journals() == before
    h.binding["epoch"] = h.scope["sender"]["epoch"] = 1
    def expired_lease(intent, name):
        descriptor = h.prepare(intent, name)
        h.clock += 31
        return descriptor
    with pytest.raises(admission.WorkspaceAdmissionError, match="expired"):
        h.send(prepare_content=expired_lease)
    assert h.journals() == before


@pytest.mark.parametrize("failure", ["wrong_route", "stale_claim"], ids=['p006_case_001', 'p006_case_002'])
def test_known_invalid_preflight_refuses_before_any_content_publication(h, failure):
    intent = request()
    if failure == "wrong_route":
        intent["destination_route"] = "not-permitted"
    else:
        h.clock += 31
    before = h.journals()
    with pytest.raises(admission.WorkspaceAdmissionError):
        h.send(intent)
    assert h.journals() == before and h.content_calls == 0 and h.blobs == {}
    assert h.source_calls == 1


def test_append_failure_or_lost_return_preserves_honest_retry_boundary(h, monkeypatch):
    original = h.coordinator._append_with_state
    before = h.journals()
    def fail_before(*args, **kwargs):
        raise OSError("synthetic before-append failure")
    monkeypatch.setattr(h.coordinator, "_append_with_state", fail_before)
    with pytest.raises(OSError):
        h.send()
    assert h.journals() == before and len(h.blobs) == 1
    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic lost committed return")
    monkeypatch.setattr(h.coordinator, "_append_with_state", fail_after)
    with pytest.raises(OSError):
        h.send()
    committed = h.journals()
    assert len(h.state()["workspace"]["send_operations"]) == 1
    monkeypatch.setattr(h.coordinator, "_append_with_state", original)
    assert h.send()["status"] == "queued"
    assert h.journals() == committed and len(h.blobs) == 1


def receiver_turn(h, sent):
    """New independent execution admission, with bounded synthetic host proofs."""
    evidence = {"current_grant": ref("delivery-grant")}
    for role in ("recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation",
                 "selection_record", "whole_message_fit"):
        evidence[role] = ref("receiver-" + role)
        target = "included_in_attempt" if role in {"selection_record", "whole_message_fit"} else "submission_started"
        h.append_payload("workspace_evidence_registered", {
            "reference": evidence[role], "category": "event" if target == "included_in_attempt" else "fence",
            "task_id": "main-2", "delivery_id": sent["delivery_id"],
            "settlement_for": {"delivery_id": sent["delivery_id"], "target_state": target, "role": role}})
    claim = {"task_id": "main-2", "claim_id": "receiver-claim", "worker_id": "worker-b", "attempt": 1,
             "lease_generation": 1, "lease_expires_at_ms": NOW + 30_000, "request_sha256": "d" * 64,
             "status": "active", "cancel_requested": False, "cancel_acknowledged": False, "renewals": 0}
    selection = {"task_id": "main-2", "claim_id": "receiver-claim", "attempt": 1, "owner_generation": 1,
                 "request_sha256": "d" * 64, "context_sha256": "c" * 64, "selected_message_ids": [sent["message_id"]]}
    recipient = {"instance_id": "instance-b", "epoch": 1}
    event = {"event": admission.ATOMIC_TURN_EVENT, "protocol": protocol.PROTOCOL, "workspace_id": "workspace", "run_id": "run",
             "operation_key": "receiver-turn", "expected_revision": h.state()["workspace"]["revision"], "payload": {
                 "claim": claim, "selection": selection, "recipient": recipient, "grant_ref": ref("delivery-grant"),
                 "evidence_by_delivery": {sent["delivery_id"]: evidence}, "affected_task_ids": [], "holds": [],
                 "goal_revision": 1, "selected_delivery_ids": [sent["delivery_id"]], "supported_ack_levels": []}}
    grant = {"grant_ref": ref("delivery-grant"), "task_id": "main-2", "goal_revision": 1, "recipient": recipient, "revoked": False}
    return event, grant


def admit_receiver(h, event, grant):
    return h.coordinator.admit_claim_selection("worker-b", copy.deepcopy(event),
        resolve_grant=lambda _state, _workspace, _reference: copy.deepcopy(grant), lease_ms=30_000)


def late_proof(h, delivery_id, target, role, category):
    reference = ref("observed-" + target + "-" + role)
    h.append_payload("workspace_evidence_registered", {
        "reference": reference, "category": category, "task_id": "main-2", "delivery_id": delivery_id,
        "settlement_for": {"delivery_id": delivery_id, "target_state": target, "role": role}})
    return reference


def test_composite_origin_separate_receiver_claim_boundary_then_late_ack_effect_settlement(h, tmp_path, monkeypatch):
    sent = h.send()
    source_task = copy.deepcopy(h.state()["tasks"]["main-1"])
    source_claim_id = source_task["attempts"][-1]
    source_claim = copy.deepcopy(h.state()["claims"][source_claim_id])
    send_evidence = {key: copy.deepcopy(value) for key, value in h.state()["workspace"]["evidence"].items()
                     if value.get("settlement_for", {}).get("target_state") == "queued"}
    assert h.state()["tasks"]["main-2"]["attempts"] == []
    event, grant = receiver_turn(h, sent)
    calibration = clone(h, tmp_path / "receiver-calibration")
    assert admit_receiver(calibration, event, grant)["status"] == "admitted"
    receiver_cap = used_and_reserve(calibration)
    # This is an independently measured admission boundary, not a promise
    # that the earlier message budget authorized or reserved a receiver claim.
    denied = clone(h, tmp_path / "receiver-denied")
    before = denied.journals()
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", receiver_cap - 1)
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        admit_receiver(denied, event, grant)
    assert denied.journals() == before
    assert denied.state()["tasks"]["main-2"]["attempts"] == []
    assert denied.state()["workspace"]["deliveries"][sent["delivery_id"]]["state"] == "queued"

    fit = clone(h, tmp_path / "receiver-fit")
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", receiver_cap)
    admit_receiver(fit, event, grant)
    assert used_and_reserve(fit) == receiver_cap
    delivery_id = sent["delivery_id"]
    proofs = copy.deepcopy(event["payload"]["evidence_by_delivery"][delivery_id])
    launch = {key: proofs[key] for key in ("current_grant", "recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation")}
    launch["durable_launch_intent"] = late_proof(fit, delivery_id, "submission_started", "durable_launch_intent", "event")
    after = copy.deepcopy(fit.state()["workspace"]["deliveries"][delivery_id])
    after.update(state="submission_started", certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
    fit.append_payload("workspace_delivery_advanced", {"delivery": after, "evidence": launch, "supported_ack_levels": []})
    assert used_and_reserve(fit) <= receiver_cap
    # Each fixture proof is first registered after its corresponding simulated
    # observation boundary. These source claims do NOT certify an actual worker.
    receipt = late_proof(fit, delivery_id, "submitted", "adapter_receipt", "adapter_receipt")
    after = copy.deepcopy(after); after["state"] = "submitted"; after["certainty"]["contact"] = "occurred"
    fit.append_payload("workspace_delivery_advanced", {"delivery": after,
        "evidence": {"adapter_receipt": receipt, "submission_boundary": "synthetic-owned-pipe"}, "supported_ack_levels": []})
    assert used_and_reserve(fit) <= receiver_cap
    ack = late_proof(fit, delivery_id, "acknowledged", "ack_receipt", "adapter_receipt")
    after = copy.deepcopy(after); after.update(state="acknowledged", ack_level="recipient_received")
    fit.append_payload("workspace_delivery_advanced", {"delivery": after,
        "evidence": {"ack_receipt": ack, "ack_level": "recipient_received"}, "supported_ack_levels": ["recipient_received"]})
    assert used_and_reserve(fit) <= receiver_cap
    effects = {role: late_proof(fit, delivery_id, "effects_resolved", role, category)
               for role, category in protocol.EFFECT_EVIDENCE_CATEGORIES.items()}
    after = copy.deepcopy(after)
    after["certainty"] = {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}
    fit.append_payload("workspace_effects_resolved", {"delivery": after, "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
                                                       "qualification": "simulated", "evidence": effects})
    assert used_and_reserve(fit) <= receiver_cap
    final = fit.state()
    assert fit.coordinator._workspace_reserve(final["workspace"]) == 0
    assert final["workspace"]["deliveries"][delivery_id] == after
    assert final["tasks"]["main-1"] == source_task and final["claims"][source_claim_id] == source_claim
    assert {key: final["workspace"]["evidence"][key] for key in send_evidence} == send_evidence
    assert final["workspace"]["effect_resolutions"][delivery_id]["qualification"] == "simulated"
