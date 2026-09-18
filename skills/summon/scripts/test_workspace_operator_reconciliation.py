"""Provider-inert sole-owner reconciliation; callbacks are explicit host fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from test_workspace_worker_send import Harness, STAMP, NOW, used_and_reserve
import _rundir as rd
import _swarm_coordinator as swarm
import _workspace_protocol as protocol
import _workspace_admission as admission
from _context_target import _final_open_path


class OperatorHarness(Harness):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.sent = self.send()
        logical = "a" * 32
        self.query = {"workspace_id": "workspace", "run_id": "run", "operation_key": logical,
                      "request_sha256": "b" * 64, "evidence_operation_key": "operator-evidence-" + logical,
                      "transition_operation_key": "operator-transition-" + logical}
        self.source = b'{"schema":"synthetic-host-decision/v1","decision":"authorized"}'
        self.proof = {"id": "operator-proof", "sha256": hashlib.sha256(self.source).hexdigest()}
        self.registration = {"reference": self.proof, "category": "grant", "task_id": "main-2",
                             "delivery_id": self.sent["delivery_id"], "settlement_for": {
                                 "delivery_id": self.sent["delivery_id"], "target_state": "cancelled", "role": "authorized_cancellation"}}
        after = copy.deepcopy(self.state()["workspace"]["deliveries"][self.sent["delivery_id"]])
        after.update(state="cancelled", reason="operator_requested")
        self.transition = {"delivery": after, "evidence": {"authorized_cancellation": self.proof}, "supported_ack_levels": []}
        self.authorizations = []
        self.inspections = []

    def operator_event(self, evidence):
        workspace = self.state()["workspace"]
        return {"event": "workspace_evidence_registered" if evidence else "workspace_delivery_advanced",
                "protocol": protocol.PROTOCOL, "workspace_id": "workspace", "run_id": "run",
                "operation_key": self.query["evidence_operation_key" if evidence else "transition_operation_key"],
                "expected_revision": workspace["revision"], "payload": copy.deepcopy(self.registration if evidence else self.transition)}

    def authorize(self, state, workspace, query):
        assert state["workspace"] == workspace
        self.authorizations.append(workspace["revision"])
        return True

    def inspect(self, state, workspace, query, events):
        self.inspections.append(copy.deepcopy(events))
        if query != self.query:
            raise swarm.SwarmProtocolError("operator source binding mismatch")
        for key, expected in ((query["evidence_operation_key"], self.registration),
                              (query["transition_operation_key"], self.transition)):
            if key in events and events[key]["payload"] != expected:
                raise swarm.SwarmProtocolError("operator source binding mismatch")
        if query["evidence_operation_key"] in events and workspace["evidence"].get(self.proof["id"]) != self.registration:
            raise swarm.SwarmProtocolError("operator registered source mismatch")
        status = "recorded" if query["transition_operation_key"] in events else "evidence_only" if events else "not_observed"
        return {"status": status, "request_sha256": query["request_sha256"], "revision": workspace["revision"]}

    def lookup(self, **kwargs):
        return self.coordinator.reconcile_operator_command(kwargs.get("query", self.query),
            authorize_command=kwargs.get("authorize_command", self.authorize),
            inspect_command=kwargs.get("inspect_command", self.inspect))


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm.time, "time", lambda: STAMP)
    return OperatorHarness(tmp_path)


@pytest.mark.parametrize("stage,expected", [(0, "not_observed"), (1, "evidence_only"), (2, "recorded")], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_public_lookup_distinguishes_exact_synchronized_outcomes_without_append(h, stage, expected):
    if stage >= 1: h.append(h.operator_event(True))
    if stage >= 2: h.append(h.operator_event(False))
    before, reserve = h.journals(), used_and_reserve(h)
    result = h.lookup()
    assert result == {"status": expected, "request_sha256": h.query["request_sha256"],
                      "durable_prefix_verified": True, "revision": h.state()["workspace"]["revision"]}
    assert h.journals() == before and used_and_reserve(h) == reserve
    assert len(h.authorizations) == 2 and len(h.inspections) == 1
    assert len(h.inspections[0]) == stage


def test_complete_line_uncertain_fsync_requires_successful_sync_before_recorded(h, monkeypatch):
    h.append(h.operator_event(True))
    event = h.operator_event(False)
    original_fsync = os.fsync
    syncs = []
    def target(fd):
        name = _final_open_path(fd)
        return name is not None and Path(name).name.startswith("journal-g") and h.query["transition_operation_key"].encode() in Path(name).read_bytes()
    def failing(fd):
        if target(fd): raise OSError("synthetic complete-line fsync uncertainty")
        return original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(swarm.os, "fsync", failing)
        with pytest.raises(rd.JournalWriteError) as caught:
            h.append(event)
        assert caught.value.phase == "fsync" and caught.value.durability == "unknown"
    uncertain = h.journals()
    # A fresh coordinator can replay the complete line, but that passive read
    # does not establish the durable result returned by reconciliation.
    h.coordinator = swarm.SwarmCoordinator(h.coordinator.runs_root, "run", clock=lambda: h.clock)
    assert h.state()["workspace"]["deliveries"][h.sent["delivery_id"]]["state"] == "cancelled"
    with monkeypatch.context() as patch:
        patch.setattr(swarm.os, "fsync", failing)
        with pytest.raises(swarm.SwarmCoordinatorError, match="synchronize"):
            h.lookup()
    assert h.inspections == [] and h.journals() == uncertain
    def successful(fd):
        if target(fd): syncs.append(True)
        return original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(swarm.os, "fsync", successful)
        assert h.lookup()["status"] == "recorded"
    assert syncs and h.journals() == uncertain
    records = [json.loads(line) for raw in uncertain.values() for line in raw.splitlines()]
    assert sum(record.get("workspace_event", {}).get("operation_key") == h.query["transition_operation_key"] for record in records) == 1


def test_scope_revoked_between_preflight_and_owner_refuses_without_inspection(h):
    h.append(h.operator_event(True))
    before = h.journals()
    calls = []
    def authorize(state, workspace, query):
        calls.append(True)
        return len(calls) == 1
    with pytest.raises(swarm.SwarmProtocolError, match="not authorized"):
        h.lookup(authorize_command=authorize)
    assert len(calls) == 2 and h.inspections == [] and h.journals() == before


def test_preflight_scope_refusal_never_acquires_owner(h, monkeypatch):
    def forbidden_acquire(*_args, **_kwargs):
        pytest.fail("denied request acquired owner")
    monkeypatch.setattr(h.coordinator, "_acquire", forbidden_acquire)
    with pytest.raises(swarm.SwarmProtocolError, match="not authorized"):
        h.lookup(authorize_command=lambda *_: False)


@pytest.mark.parametrize("change", ["query_digest", "registered_source", "transition_proof"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_mismatched_source_or_semantics_cannot_become_recorded(h, change):
    h.append(h.operator_event(True))
    h.append(h.operator_event(False))
    before = h.journals()
    if change == "query_digest":
        query = h.query | {"request_sha256": "c" * 64}
    else:
        query = h.query
        if change == "registered_source": h.registration = h.registration | {"category": "event"}
        else: h.transition = h.transition | {"supported_ack_levels": ["recipient_received"]}
    with pytest.raises(swarm.SwarmProtocolError, match="source binding"):
        h.lookup(query=query)
    assert h.journals() == before


@pytest.mark.parametrize("damage", ["torn", "corrupt"], ids=['p003_case_001', 'p003_case_002'])
def test_lookup_never_repairs_torn_or_corrupt_command_prefix(h, damage):
    h.append(h.operator_event(True))
    path = max(Path(h.coordinator.run_dir).glob("journal-g*.jsonl"), key=lambda item: int(item.stem[9:]))
    original = path.read_bytes()
    if damage == "torn": path.write_bytes(original + b'{"partial_operator":')
    else: path.write_bytes(original.replace(b'"workspace_id":"workspace"', b'"workspace_id":"workspacd"', 1))
    before = h.journals()
    with pytest.raises(swarm.SwarmCoordinatorError): h.lookup()
    assert h.journals() == before and h.inspections == []


@pytest.mark.parametrize("field,value", [("status", "recorded"), ("request_sha256", "c" * 64), ("revision", True), ("revision", 999)], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004'])
def test_callback_cannot_self_certify_empty_or_wrong_snapshot(h, field, value):
    before = h.journals()
    def wrong(state, workspace, query, events):
        return {**h.inspect(state, workspace, query, events), field: value}
    with pytest.raises(swarm.SwarmProtocolError, match="inspection conflicts"):
        h.lookup(inspect_command=wrong)
    assert h.journals() == before


@pytest.mark.parametrize("change", ["extra", "short_key", "wrong_evidence_key", "wrong_transition_key", "scope", "digest"], ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004', 'p005_case_005', 'p005_case_006'])
def test_invalid_query_refuses_before_callbacks(h, change):
    query = copy.deepcopy(h.query)
    if change == "extra": query["authorized"] = True
    elif change == "short_key": query["operation_key"] = "a"
    elif change == "wrong_evidence_key": query["evidence_operation_key"] = "unrelated"
    elif change == "wrong_transition_key": query["transition_operation_key"] = "unrelated"
    elif change == "scope": query["run_id"] = "other"
    else: query["request_sha256"] = float("nan")
    before = h.journals()
    with pytest.raises((swarm.SwarmProtocolError, admission.WorkspaceAdmissionError)):
        h.lookup(query=query)
    assert h.journals() == before and h.authorizations == h.inspections == []


def test_recorded_terminal_state_under_another_key_is_not_this_command(h):
    event = h.operator_event(True)
    event["operation_key"] = "another-proof"
    h.append(event)
    event = h.operator_event(False)
    event["operation_key"] = "another-transition"
    h.append(event)
    assert h.lookup()["status"] == "not_observed"


def test_transition_without_matching_evidence_operation_refuses(h):
    event = h.operator_event(True)
    event["operation_key"] = "another-proof"
    h.append(event)
    h.append(h.operator_event(False))
    before = h.journals()
    with pytest.raises(swarm.SwarmProtocolError, match="no matching evidence"):
        h.lookup()
    assert h.journals() == before


def test_prefix_changed_during_inspection_is_not_certified(h):
    def inspect(state, workspace, query, events):
        result = h.inspect(state, workspace, query, events)
        path = max(Path(h.coordinator.run_dir).glob("journal-g*.jsonl"), key=lambda item: int(item.stem[9:]))
        path.write_bytes(path.read_bytes() + b'{"external_change":')
        return result
    with pytest.raises(swarm.SwarmCorruptError, match="changed"):
        h.lookup(inspect_command=inspect)


def test_torn_tail_appearing_after_preflight_is_not_repaired_on_acquire(h):
    damaged = []
    def authorize(state, workspace, query):
        path = max(Path(h.coordinator.run_dir).glob("journal-g*.jsonl"), key=lambda item: int(item.stem[9:]))
        path.write_bytes(path.read_bytes() + b'{"between_preflight_and_owner":')
        damaged.append(h.journals())
        return True
    with pytest.raises(swarm.SwarmCorruptError, match="torn"):
        h.lookup(authorize_command=authorize)
    assert len(damaged) == 1 and h.journals() == damaged[0] and h.inspections == []


@pytest.mark.parametrize("result", [False, "exception"], ids=['p006_case_001', 'p006_case_002'])
def test_final_append_constraint_rechecks_after_blocking_prefix_sync(h, monkeypatch, result):
    event = h.operator_event(True)
    before = h.journals()
    original_sync = h.coordinator._sync_prefix
    calls = []
    revoked = False
    def sync(owner, raw):
        nonlocal revoked
        value = original_sync(owner, raw)
        calls.append(True)
        if len(calls) == 2: revoked = True
        return value
    def constraint(current):
        assert len(calls) == 2 and revoked
        assert current["workspace"]["revision"] == event["expected_revision"]
        if result == "exception": raise swarm.SwarmProtocolError("installed session revoked")
        return False
    monkeypatch.setattr(h.coordinator, "_sync_prefix", sync)
    with h.coordinator._mutation() as (owner, state, _):
        with pytest.raises(swarm.SwarmProtocolError, match="constraint refused|session revoked"):
            h.coordinator._append_with_state(owner, {"event": "workspace_event", "workspace_event": event,
                "observed_at_ms": NOW}, state=state, before_append=constraint)
    assert h.journals() == before


def test_final_append_true_preserves_existing_reserved_append(h):
    event = h.operator_event(True)
    calls = []
    with h.coordinator._mutation() as (owner, state, _):
        def constraint(current):
            calls.append(current["workspace"]["revision"])
            return True
        h.coordinator._append_with_state(owner, {"event": "workspace_event", "workspace_event": event,
            "observed_at_ms": NOW}, state=state, before_append=constraint)
    assert calls == [event["expected_revision"]]
    assert h.lookup()["status"] == "evidence_only"
