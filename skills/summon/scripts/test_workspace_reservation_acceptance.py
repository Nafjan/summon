"""Independent no-worker acceptance of durable preview turn reservations."""
from pathlib import Path
import copy
import json
import os
import subprocess
import sys
import threading

import pytest

from _swarm_coordinator import (
    SwarmBudgetRefusal, SwarmCoordinator, rebuild_projection_from_journal,
)
from _workspace_demo import ConductorDemo
from _workspace_transport import TransportAdmissionError
from _spawn import run_flags
from _rundir import OwnerHeldError


def reserve(demo, task="main-1", worker="worker-a-1"):
    grant = demo.grant(task, worker)[0]
    binding = demo.register_worker(task, worker, grant)
    entry = demo.message(task, worker, grant, 1, "[2,3]")
    return demo.admit(task, worker, grant, [entry]), binding


@pytest.fixture
def demo(tmp_path, monkeypatch):
    # These tests must never obtain success by starting a real fixed worker.
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("reservation acceptance must not spawn a worker")

    monkeypatch.setattr("_workspace_transport.OwnedFakeWorker.__init__", forbidden_spawn)
    value = ConductorDemo(tmp_path / "workspaces")
    value.prepare()
    yield value
    value.cleanup()


def test_consumed_reservation_rebuilds_and_refuses_second_coordinator_object(demo):
    turn, binding = reserve(demo)
    assert turn["budget_admission"].before_spawn(binding, turn["planned_work"])["status"] == "accepted"
    original = demo.runtime._coordinator
    # A second coordinator object rereads the durable journal in this process;
    # the separate process/restart contract remains a follow-up gate.
    fresh = SwarmCoordinator(original.runs_root, original.run_id, clock=original.clock)
    admission_id = turn["event"]["operation_key"]
    state = fresh._load()[0]
    assert state["admissions"][admission_id]["budget_admission"]["status"] == "consumed"
    reservation = turn["response"]["budget_admission"]
    with pytest.raises(SwarmBudgetRefusal):
        fresh.consume_workspace_turn_budget(
            admission_id, decision=reservation["decision"], policy=reservation["policy"],
            snapshot=reservation["snapshot"], request=reservation["request"],
            binding=binding, planned_work=turn["planned_work"], resolve_grant=demo.resolve_grant)
    assert fresh._load()[0] == state


def test_durable_consumption_requires_current_grant_resolver(demo):
    turn, binding = reserve(demo)
    with pytest.raises(SwarmBudgetRefusal):
        demo.runtime._coordinator.consume_workspace_turn_budget(
            turn["event"]["operation_key"],
            decision=turn["response"]["budget_admission"]["decision"],
            policy=turn["response"]["budget_admission"]["policy"],
            snapshot=turn["response"]["budget_admission"]["snapshot"],
            request=turn["response"]["budget_admission"]["request"],
            binding=binding, planned_work=turn["planned_work"])
    assert "launch_intent" not in demo.state()["admissions"][turn["event"]["operation_key"]]


@pytest.mark.parametrize("grant_result", [
    {"revoked": True, "recipient": {"instance_id": "worker-a-1", "epoch": 1}},
    {"revoked": False, "recipient": {"instance_id": "rotated", "epoch": 1}},
], ids=['p001_case_001', 'p001_case_002'])
def test_durable_consumption_refuses_revoked_or_rotated_grant(demo, grant_result):
    turn, binding = reserve(demo)
    admission = turn["response"]["budget_admission"]
    with pytest.raises(SwarmBudgetRefusal):
        demo.runtime._coordinator.consume_workspace_turn_budget(
            turn["event"]["operation_key"], decision=admission["decision"],
            policy=admission["policy"], snapshot=admission["snapshot"],
            request=admission["request"], binding=binding,
            planned_work=turn["planned_work"],
            resolve_grant=lambda *_args: grant_result)
    assert demo.state()["admissions"][turn["event"]["operation_key"]]["budget_admission"]["status"] == "reserved"


def test_rebuild_preserves_consumption_provenance_and_later_record(demo):
    turn, binding = reserve(demo)
    turn["budget_admission"].before_spawn(binding, turn["planned_work"])
    coordinator = demo.runtime._coordinator
    coordinator.register_worker("later-worker", worker_instance_id="later-instance")
    journals = {p.name: p.read_bytes() for p in Path(coordinator.run_dir).glob("journal-g*.jsonl")}
    rebuilt = rebuild_projection_from_journal(coordinator.run_dir, expected_run_id=coordinator.run_id)
    state = rebuilt["projection"]
    admission = state["admissions"][turn["event"]["operation_key"]]
    assert admission["budget_admission"]["status"] == "consumed"
    assert admission["launch_intent"]["planned_work_sha256"]
    assert "later-worker" in state["workers"]
    assert journals == {p.name: p.read_bytes() for p in Path(coordinator.run_dir).glob("journal-g*.jsonl")}


def test_consumed_reservation_survives_fresh_python_process(demo):
    turn, binding = reserve(demo)
    turn["budget_admission"].before_spawn(binding, turn["planned_work"])
    coordinator = demo.runtime._coordinator
    probe = (
        "import json, sys; "
        "from _swarm_coordinator import SwarmCoordinator; "
        "c = SwarmCoordinator(sys.argv[1], sys.argv[2]); "
        "s, torn = c._load(); "
        "print(json.dumps({'torn': torn, 'status': s['admissions'][sys.argv[3]]['budget_admission']['status']}))"
    )
    env = os.environ.copy()
    scripts = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(coordinator.runs_root),
         coordinator.run_id, turn["event"]["operation_key"]],
        cwd=scripts, env=env, capture_output=True, text=True, timeout=30,
        check=False, **run_flags())
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"torn": False, "status": "consumed"}


@pytest.mark.parametrize("change", ["cancel", "expire"], ids=['p002_case_001', 'p002_case_002'])
def test_stale_reserved_turn_refuses_without_launch_intent(demo, change):
    turn, binding = reserve(demo)
    coordinator = demo.runtime._coordinator
    if change == "cancel":
        coordinator.cancel("main-1")
    else:
        future = coordinator.clock() + 301
        coordinator.clock = lambda: future
    with pytest.raises(TransportAdmissionError):
        turn["budget_admission"].before_spawn(binding, turn["planned_work"])
    admission = coordinator._load()[0]["admissions"][turn["event"]["operation_key"]]
    assert admission["budget_admission"]["status"] == "reserved"
    assert "launch_intent" not in admission


def test_different_task_cannot_reserve_occupied_last_slot(demo, monkeypatch):
    original_spec = demo._budget_spec

    def one_slot(*args, **kwargs):
        policy, request, planned = original_spec(*args, **kwargs)
        policy["execution_slot_limit"] = 1
        return policy, request, planned

    monkeypatch.setattr(demo, "_budget_spec", one_slot)
    reserve(demo)
    with pytest.raises(SwarmBudgetRefusal):
        reserve(demo, "side-1", "worker-b-1")
    state = demo.state()
    assert len(state["tasks"]["main-1"]["attempts"]) == 1
    assert state["tasks"]["side-1"]["attempts"] == []


def test_simultaneous_distinct_tasks_compete_for_one_execution_slot(demo, monkeypatch):
    original_spec = demo._budget_spec

    def one_slot(*args, **kwargs):
        policy, request, planned = original_spec(*args, **kwargs)
        policy["execution_slot_limit"] = 1
        return policy, request, planned

    monkeypatch.setattr(demo, "_budget_spec", one_slot)
    grant_a = demo.grant("main-1", "worker-a-1")[0]
    grant_b = demo.grant("side-1", "worker-b-1")[0]
    demo.register_worker("main-1", "worker-a-1", grant_a)
    demo.register_worker("side-1", "worker-b-1", grant_b)
    entry_a = demo.message("main-1", "worker-a-1", grant_a, 1, "[2,3]")
    entry_b = demo.message("side-1", "worker-b-1", grant_b, 1, "[4,5]", source_task="main-1")
    barrier = threading.Barrier(2)
    results, errors = [], []

    def admit(task, instance, grant, entry):
        try:
            barrier.wait(timeout=10)
            results.append(demo.admit(task, instance, grant, [entry]))
        except Exception as exc:  # assert the serialized loser below
            errors.append((task, instance, grant, entry, exc))

    threads = [threading.Thread(target=admit, args=args) for args in (
        ("main-1", "worker-a-1", grant_a, entry_a),
        ("side-1", "worker-b-1", grant_b, entry_b),
    )]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 1
    assert len(errors) == 1
    failed_task, failed_instance, failed_grant, failed_entry, error = errors[0]
    assert isinstance(error, (OwnerHeldError, SwarmBudgetRefusal))
    # The concurrent loser is retried only after the writer lease is released;
    # the durable capacity gate then returns the typed last-slot refusal.
    with pytest.raises(SwarmBudgetRefusal):
        demo.admit(failed_task, failed_instance, failed_grant, [failed_entry])
    assert len(demo.state()["tasks"]["main-1"]["attempts"]) + len(demo.state()["tasks"]["side-1"]["attempts"]) == 1


def test_snapshot_binds_selected_recovery_successor_in_either_delivery_order(tmp_path):
    """A logical message's selected successor cannot be shadowed by its parent."""
    demo = ConductorDemo(tmp_path / "workspaces")
    try:
        setup = demo.supervisor_message_setup()
        coordinator = demo.runtime._coordinator
        state, torn, raw_map = coordinator._load_with_snapshot()
        records, records_torn, records_raw = coordinator._read_records_snapshot()
        assert not torn and not records_torn and records_raw == raw_map
        original = state["workspace"]["inbox_deliveries"][setup["delivery_id"]]
        message_id = original["message_id"]
        for order in ("parent-first", "successor-first"):
            projected = copy.deepcopy(state)
            parent = copy.deepcopy(original)
            parent["delivery_id"] = "recovery-parent"
            parent["state"] = "held"
            successor = copy.deepcopy(original)
            successor["delivery_id"] = "recovery-successor"
            successor["state"] = "queued"
            projected["workspace"]["inbox_deliveries"] = (
                {parent["delivery_id"]: parent, successor["delivery_id"]: successor}
                if order == "parent-first" else
                {successor["delivery_id"]: successor, parent["delivery_id"]: parent}
            )
            snapshot = coordinator.workspace_budget_snapshot(
                state=projected, raw_map=raw_map, records=records,
                preferred_delivery_ids={successor["delivery_id"]})
            selected = [item for item in snapshot["pending_messages"]
                        if item["message_id"] == message_id]
            assert len(selected) == 1
            assert selected[0]["state"] == "queued"
            assert selected[0]["source"] == "inbox"
            assert selected[0]["stream_id"] == projected["workspace"]["messages"][message_id]["stream_id"]
            assert selected[0]["sequence"] == projected["workspace"]["messages"][message_id]["sequence"]
            assert selected[0]["bytes"] > 0
    finally:
        demo.cleanup()
