"""Actual journal admission with synthetic installed host authority, no browser."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

import _rundir as rd
import _swarm_coordinator as swarm
import _workspace_admission as admission
from _context_target import _final_open_path
from test_workspace_protocol import ref
from test_workspace_worker_send import Harness, STAMP, NOW, clone, used_and_reserve


class OperatorSendHarness(Harness):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.append_payload("workspace_evidence_registered", {
            "reference": ref("operator-scope"), "category": "grant", "task_id": "main-2"})
        self.operator = {"operator_id": "local-operator", "scope": {
            "reference": ref("operator-scope"), "revision": 1, "expires_at_ms": NOW + 60000,
            "revoked": False, "operation": "operator.message.send", "goal_id": "goal",
            "destination_task_id": "main-2", "recipient": {"instance_id": "instance-b", "epoch": 1},
            "delivery_grant_ref": ref("delivery-grant"), "target": "opaque-target"}}
        self.intent = {"operation_key": "a" * 32, "target": "opaque-target", "text": "Preserve exact\nλ context."}

    def resolve_operator(self, current, workspace, intent):
        self.source_calls += 1
        assert current["workspace"] == workspace
        return copy.deepcopy(self.operator)

    def prepare(self, intent, content_id):
        self.content_calls += 1
        raw = intent["text"].encode("utf-8")
        self.blobs[content_id] = raw
        return {"ref": content_id, "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}

    def send(self, intent=None, **kwargs):
        return self.coordinator.admit_operator_message(intent or self.intent,
            resolve_operator=kwargs.get("resolve_operator", self.resolve_operator),
            prepare_content=kwargs.get("prepare_content", self.prepare))

    def lookup(self, intent=None, **kwargs):
        return self.coordinator.reconcile_operator_message(intent or self.intent,
            resolve_operator=kwargs.get("resolve_operator", self.resolve_operator))


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: STAMP)
    return OperatorSendHarness(tmp_path)


def test_actual_send_is_one_atomic_queue_and_reopened_lookup_never_resends(h):
    original = h.state()
    before = h.journals()
    assert h.lookup()["status"] == "not_observed"
    assert h.journals() == before and h.content_calls == 0
    response = h.send()
    after = h.state()
    assert response["status"] == "queued" and response["execution_authorized"] is False
    assert len(after["workspace"]["messages"]) == len(after["workspace"]["deliveries"]) == 1
    for key in ("tasks", "claims", "workers"):
        assert after[key] == original[key]
    assert h.blobs[next(iter(h.blobs))] == h.intent["text"].encode("utf-8")
    frozen = h.journals()
    h.coordinator = swarm.SwarmCoordinator(h.coordinator.runs_root, "run", clock=lambda: h.clock)
    assert h.send() == response
    assert h.lookup() == response | {"durable_prefix_verified": True}
    assert h.journals() == frozen and h.content_calls == 1
    records = [json.loads(line) for raw in frozen.values() for line in raw.splitlines()]
    assert sum(r.get("workspace_event", {}).get("event") == admission.OPERATOR_SEND_EVENT for r in records) == 1


@pytest.mark.parametrize("operation", ["send", "lookup"], ids=['p001_case_001', 'p001_case_002'])
@pytest.mark.parametrize("refusal", ["revoked", "expired", "target", "missing_grant"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_current_scope_refusal_before_owner_and_content(h, monkeypatch, operation, refusal):
    if refusal == "revoked": h.operator["scope"]["revoked"] = True
    elif refusal == "expired": h.operator["scope"]["expires_at_ms"] = NOW
    elif refusal == "target": h.operator["scope"]["target"] = "foreign"
    else: h.operator["scope"]["reference"] = ref("missing")
    before = h.journals()
    monkeypatch.setattr(h.coordinator, "_acquire", lambda **_: pytest.fail("refused scope acquired ownership"))
    with pytest.raises((ValueError, RuntimeError)):
        getattr(h, operation)()
    assert h.journals() == before and h.content_calls == 0


@pytest.mark.parametrize("change", ["text", "target", "recipient"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003'])
def test_changed_intent_cannot_reuse_committed_key(h, change):
    h.send()
    before = h.journals()
    if change == "text": h.intent["text"] += "changed"
    elif change == "target": h.intent["target"] = h.operator["scope"]["target"] = "another"
    else: h.operator["scope"]["recipient"]["epoch"] = 2
    for method in (h.send, h.lookup):
        with pytest.raises(swarm.SwarmConflictError, match="reused"):
            method()
    assert h.journals() == before and h.content_calls == 1


def test_scope_change_during_content_publication_refuses_with_only_orphan_blob(h):
    before = h.journals()
    def publish(intent, content_id):
        result = h.prepare(intent, content_id)
        h.operator["scope"]["revoked"] = True
        return result
    with pytest.raises((ValueError, RuntimeError)):
        h.send(prepare_content=publish)
    assert h.journals() == before and h.blobs
    assert h.state()["workspace"]["messages"] == {}


def test_revocation_during_final_prefix_sync_prevents_append(h, monkeypatch):
    original = h.coordinator._sync_prefix
    calls = []
    def sync(owner, raw):
        result = original(owner, raw)
        calls.append(True)
        if len(calls) == 2: h.operator["scope"]["revoked"] = True
        return result
    before = h.journals()
    monkeypatch.setattr(h.coordinator, "_sync_prefix", sync)
    with pytest.raises((ValueError, RuntimeError)):
        h.send()
    assert len(calls) == 2 and h.journals() == before


def test_post_append_revocation_requires_fresh_authority_and_lookup(h, monkeypatch):
    original = swarm.journal_append_encoded
    def append(*args, **kwargs):
        result = original(*args, **kwargs)
        h.operator["scope"]["revoked"] = True
        return result
    with monkeypatch.context() as patch:
        patch.setattr(swarm, "journal_append_encoded", append)
        with pytest.raises((ValueError, RuntimeError)):
            h.send()
    assert len(h.state()["workspace"]["messages"]) == 1
    before = h.journals()
    with pytest.raises((ValueError, RuntimeError)):
        h.lookup()
    h.operator["scope"]["revoked"] = False  # Explicit synthetic host reinstatement.
    assert h.lookup()["status"] == "queued"
    assert h.journals() == before and h.content_calls == 1


def test_lookup_rechecks_authority_after_reading_retained_event(h, monkeypatch):
    h.send()
    before = h.journals()
    original = h.coordinator._read_records_snapshot
    def read():
        result = original()
        h.operator["scope"]["revoked"] = True
        return result
    monkeypatch.setattr(h.coordinator, "_read_records_snapshot", read)
    with pytest.raises((ValueError, RuntimeError)):
        h.lookup()
    assert h.journals() == before and h.content_calls == 1


def test_uncertain_complete_line_requires_owned_sync_before_queue_lookup(h, monkeypatch):
    original = os.fsync
    def failing(fd):
        location = _final_open_path(fd)
        if location is not None and Path(location).name.startswith("journal-g"):
            if admission.OPERATOR_SEND_EVENT.encode() in Path(location).read_bytes():
                raise OSError("synthetic operator queue fsync uncertainty")
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(swarm.os, "fsync", failing)
        with pytest.raises(rd.JournalWriteError) as caught:
            h.send()
        assert caught.value.durability == "unknown"
        assert len(h.state()["workspace"]["messages"]) == 1
        with pytest.raises(swarm.SwarmCoordinatorError):
            h.lookup()
    before = h.journals()
    assert h.lookup()["status"] == "queued"
    assert h.journals() == before and h.content_calls == 1


@pytest.mark.parametrize("operation", ["send", "lookup"], ids=['p004_case_001', 'p004_case_002'])
def test_torn_prefix_refuses_without_repair_or_publication(h, operation):
    journal = next(Path(h.coordinator.run_dir).glob("journal-g*.jsonl"))
    with journal.open("ab") as stream:
        stream.write(b'{"unfinished":')
    before = h.journals()
    with pytest.raises(swarm.SwarmCorruptError):
        getattr(h, operation)()
    assert h.journals() == before and h.content_calls == 0


def test_actual_exact_capacity_admission_reserves_later_cancellation(h, tmp_path, monkeypatch):
    calibration = clone(h, tmp_path / "calibration")
    calibration.send()
    cap = used_and_reserve(calibration)
    too_small = clone(h, tmp_path / "too-small")
    before = too_small.journals()
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap - 1)
    with pytest.raises(swarm.SwarmCoordinatorError, match="capacity"):
        too_small.send()
    assert too_small.journals() == before and too_small.state()["workspace"]["messages"] == {}
    fits = clone(h, tmp_path / "fits")
    monkeypatch.setattr(swarm, "MAX_JOURNAL_BYTES", cap)
    response = fits.send()
    assert fits.journals() == calibration.journals() and used_and_reserve(fits) == cap
    assert fits.lookup()["status"] == "queued" and used_and_reserve(fits) == cap
    delivery_id = response["delivery_id"]
    proof = {"reference": ref("operator-cancel"), "category": "grant", "task_id": "main-2",
             "delivery_id": delivery_id, "settlement_for": {
                 "delivery_id": delivery_id, "target_state": "cancelled", "role": "authorized_cancellation"}}
    fits.append_payload("workspace_evidence_registered", proof)
    after = copy.deepcopy(fits.state()["workspace"]["deliveries"][delivery_id])
    after.update(state="cancelled", reason="explicit_synthetic_cancellation")
    fits.append_payload("workspace_delivery_advanced", {"delivery": after,
        "evidence": {"authorized_cancellation": proof["reference"]}, "supported_ack_levels": []})
    assert used_and_reserve(fits) <= cap
    assert fits.state()["workspace"]["deliveries"][delivery_id]["certainty"] == {
        "contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}
    # Historic admission remains queued evidence even after later cancellation.
    assert fits.lookup()["delivery_state"] == "queued"
