"""Provider-free coordinator budget admission at the owned transport boundary."""
from __future__ import annotations

import hashlib
import json

import pytest

import _workspace_transport as transport
from test_workspace_worker_send import Harness, NOW


def policy(**overrides):
    value = {
        "schema": "summon.workspace.admission-policy/v1",
        "rate_window_ms": 60_000,
        "rate_limit": 4,
        "rate_bytes_limit": 1_000,
        "max_message_bytes": 128,
        "journal_capacity_bytes": 100_000,
        "control_reserve_bytes": 1_000,
        "recovery_reserve_bytes": 1_000,
        "execution_slot_limit": 1,
        "stream_limits": {"alpha": {"message_limit": 4, "byte_limit": 1_000}},
        "oversize_head_action": "refuse",
    }
    value.update(overrides)
    return value


def payload():
    context = "[1,2,3]"
    return {
        "message_id": "m1", "delivery_id": "d1", "attempt_id": "a1",
        "context": context, "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
        "operation": transport.FIXTURE_OPERATION,
    }


def context_payload():
    entries = []
    for message_id, delivery_id, values in (("m1", "d1", "[1]"), ("m2", "d2", "[2]")):
        entries.append({
            "message_id": message_id, "delivery_id": delivery_id,
            "content_sha256": hashlib.sha256(values.encode()).hexdigest(),
            "content_utf8": values,
        })
    envelope = {
        "schema": "summon.workspace.context/v1", "plane": "payload",
        "task_id": "main-1", "claim_id": "a1", "attempt": 1,
        "lease_generation": 1, "request_sha256": "a" * 64, "entries": entries,
    }
    context = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return {
        "message_id": "m1", "delivery_id": "d1", "attempt_id": "a1",
        "context": context, "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
        "operation": transport.CONTEXT_FIXTURE_OPERATION,
    }


def snapshot(*, bytes_value=3, active_slots=0, pending=None, journal_used=0, rate_events=None):
    return {
        "schema": "summon.workspace.admission-snapshot/v1",
        "now_ms": NOW,
        "journal_used_bytes": journal_used,
        "active_execution_slots": active_slots,
        "rate_events": list(rate_events or []),
        "pending_messages": pending or [{
            "message_id": "m1", "stream_id": "alpha", "sequence": 1,
            "bytes": bytes_value, "state": "queued",
        }],
        "stream_usage": {"alpha": {"messages": 0, "bytes": 0}},
    }


def request(**overrides):
    value = {
        "schema": "summon.workspace.admission-request/v1",
        "selected_message_ids": ["m1"], "journal_bytes": 3,
        "execution_slots_requested": 1, "kind": "message",
    }
    value.update(overrides)
    return value


def gate(h, *, pol=None, snap=None, req=None, planned=None):
    pol = pol or policy()
    snap = snap or snapshot()
    req = req or request()
    planned = planned or payload()
    decision = h.coordinator.workspace_budget_decision(policy=pol, snapshot=snap, request=req)
    return h.coordinator.workspace_transport_admission(
        decision=decision, policy=pol, snapshot=snap, request=req,
        binding=h.binding, planned_work=planned)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr("_swarm_coordinator.time.time", lambda: NOW / 1000)
    return Harness(tmp_path)


def test_bound_admission_consumes_before_popen_and_keeps_accounting_separate(h, monkeypatch):
    admission = gate(h)
    calls = []
    original_consume = admission._consume

    def observe(**kwargs):
        calls.append("budget")
        return original_consume(**kwargs)

    admission._consume = observe
    original_popen = transport.subprocess.Popen

    def popen(*args, **kwargs):
        assert calls == ["budget"]
        calls.append("popen")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(transport.subprocess, "Popen", popen)
    with transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                   planned_work=payload()) as worker:
        assert worker._ready is True
        assert worker._transport_admission_receipt["status"] == "accepted"
        assert worker._transport_admission_receipt["provider_contacted"] is False
        assert worker._transport_admission_receipt["state_mutated"] is False
    assert calls == ["budget", "popen"]


@pytest.mark.parametrize("change", ["policy", "snapshot", "request", "journal"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_binding_mismatch_refuses_before_popen(h, monkeypatch, change):
    pol, snap, req, planned = policy(), snapshot(), request(), payload()
    decision = h.coordinator.workspace_budget_decision(policy=pol, snapshot=snap, request=req)
    if change == "policy":
        pol = {**pol, "rate_limit": 3}
    elif change == "snapshot":
        snap = {**snap, "journal_used_bytes": 1}
    elif change == "request":
        req = {**req, "journal_bytes": 4}
    else:
        admission = h.coordinator.workspace_transport_admission(
            decision=decision, policy=pol, snapshot=snap, request=req,
            binding=h.binding, planned_work=planned)
        h.append_payload("workspace_evidence_registered", {
            "reference": {"id": "transport-stale", "sha256": "a" * 64},
            "category": "artifact", "task_id": "main-1"})
        before = h.journals()
        monkeypatch.setattr(transport.subprocess, "Popen",
                            lambda *args, **kwargs: pytest.fail("Popen before stale refusal"))
        with pytest.raises(transport.TransportAdmissionError) as caught:
            transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                      planned_work=planned)
        assert caught.value.reason == "budget_binding_required"
        assert caught.value.__cause__ is None
        assert h.journals() == before
        return
    admission = h.coordinator.workspace_transport_admission(
        decision=decision, policy=pol, snapshot=snap, request=req,
        binding=h.binding, planned_work=planned)
    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Popen before budget refusal"))
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                  planned_work=planned)
    assert caught.value.reason == "budget_binding_required"


@pytest.mark.parametrize(
    ("name", "pol", "snap", "req", "reason", "budget_reason"),
    [
        ("oversize", policy(max_message_bytes=1), snapshot(), request(),
         "budget_decision_blocked", "oversize_head_refused"),
        ("rate", policy(rate_limit=1),
         snapshot(rate_events=[{"at_ms": NOW, "messages": 1, "bytes": 0}]),
         request(), "budget_decision_blocked", "rate_window_exhausted"),
        ("stream", policy(), snapshot(pending=[
            {"message_id": "head", "stream_id": "alpha", "sequence": 1, "bytes": 3, "state": "queued"},
            {"message_id": "m1", "stream_id": "alpha", "sequence": 2, "bytes": 3, "state": "queued"},
        ]), request(), "budget_decision_blocked", "stream_prefix_required"),
        ("control-reserve", policy(journal_capacity_bytes=10, control_reserve_bytes=2,
                                    recovery_reserve_bytes=2),
         snapshot(journal_used=6, bytes_value=1), request(journal_bytes=1),
         "budget_decision_blocked", "control_reserve_protected"),
        ("recovery-reserve", policy(journal_capacity_bytes=10, control_reserve_bytes=2,
                                     recovery_reserve_bytes=2),
         snapshot(journal_used=8, bytes_value=1), request(journal_bytes=1),
         "budget_decision_blocked", "recovery_reserve_protected"),
        ("execution-slot", policy(), snapshot(active_slots=1), request(),
         "execution_slot_unavailable", "none"),
    ],
 ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006'])
def test_budget_refusal_and_slot_deferral_happen_before_popen(
        h, monkeypatch, name, pol, snap, req, reason, budget_reason):
    del name
    admission = gate(h, pol=pol, snap=snap, req=req)
    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Popen before budget refusal"))
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                  planned_work=payload())
    assert caught.value.reason == reason
    assert caught.value.result["reason"] == budget_reason
    assert caught.value.result["provider_contacted"] is False
    assert caught.value.result["state_mutated"] is False
    assert caught.value.__cause__ is None


def test_binding_payload_mismatch_and_duplicate_readback_are_one_shot(h, monkeypatch):
    admission = gate(h)
    original_popen = transport.subprocess.Popen
    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Popen on mismatch"))
    altered = payload()
    altered["context"] = "[1,2,4]"
    altered["context_sha256"] = hashlib.sha256(altered["context"].encode()).hexdigest()
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                  planned_work=altered)
    assert caught.value.reason == "transport_request_binding_mismatch"

    monkeypatch.setattr(transport.subprocess, "Popen", original_popen)
    admission = gate(h)
    with transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                   planned_work=payload()):
        pass
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                  planned_work=payload())
    assert caught.value.reason == "transport_admission_already_consumed"


def test_multi_entry_context_preserves_order_and_accepts_before_popen(h, monkeypatch):
    planned = context_payload()
    snap = snapshot(pending=[
        {"message_id": "m1", "stream_id": "alpha", "sequence": 1, "bytes": 3, "state": "queued"},
        {"message_id": "m2", "stream_id": "alpha", "sequence": 2, "bytes": 3, "state": "queued"},
    ])
    req = request(selected_message_ids=["m1", "m2"], journal_bytes=6)
    admission = gate(h, snap=snap, req=req, planned=planned)
    calls = []
    original_popen = transport.subprocess.Popen

    def popen(*args, **kwargs):
        calls.append("popen")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(transport.subprocess, "Popen", popen)
    with transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                   planned_work=planned) as worker:
        assert worker._transport_admission_receipt["status"] == "accepted"
    assert calls == ["popen"]


def test_two_independent_adapters_cannot_consume_one_bound_decision(h):
    first = gate(h)
    second = gate(h)
    with transport.OwnedFakeWorker(h.binding, budget_admission=first,
                                   planned_work=payload()):
        pass
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=second,
                                  planned_work=payload())
    assert caught.value.reason == "budget_binding_required"


@pytest.mark.parametrize("field", ["journal_revision", "journal_prefix_sha256"], ids=['p003_case_001', 'p003_case_002'])
def test_consumer_revision_or_prefix_mismatch_refuses_before_popen(h, monkeypatch, field):
    admission = gate(h)
    original_consume = admission._consume

    def wrong(**kwargs):
        envelope = original_consume(**kwargs)
        envelope[field] = envelope[field] + (1 if field == "journal_revision" else "")
        if field == "journal_prefix_sha256":
            envelope[field] = "b" * 64
        return envelope

    admission._consume = wrong
    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Popen before consumer mismatch"))
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(h.binding, budget_admission=admission,
                                  planned_work=payload())
    assert caught.value.reason == "coordinator_consumer_invalid"
    assert caught.value.__cause__ is None


def test_worker_binding_and_malformed_decision_refuse_before_popen(h, monkeypatch):
    admission = gate(h)
    wrong_binding = {**h.binding, "instance_id": "instance-b"}
    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Popen before binding refusal"))
    with pytest.raises(transport.TransportAdmissionError) as caught:
        transport.OwnedFakeWorker(wrong_binding, budget_admission=admission,
                                  planned_work=payload())
    assert caught.value.reason == "transport_request_binding_mismatch"
    assert caught.value.__cause__ is None

    with pytest.raises(transport.TransportAdmissionError) as caught:
        h.coordinator.workspace_transport_admission(
            decision={"schema": "invalid"}, policy=policy(), snapshot=snapshot(),
            request=request(), binding=h.binding, planned_work=payload())
    assert caught.value.reason == "budget_decision_invalid"
    assert caught.value.__cause__ is None


def test_legacy_worker_path_stays_explicitly_unqualified(h):
    with pytest.raises(transport.TransportError, match="transport_admission_required"):
        transport.OwnedFakeWorker(h.binding, planned_work=payload())
