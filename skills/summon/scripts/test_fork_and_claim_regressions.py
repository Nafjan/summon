"""Canonical synthetic journals; no provider, child process or network calls."""
import json
import threading
from pathlib import Path

import pytest

from _rundir import OwnerHeldError
from _swarm_protocol import SwarmProtocolError
from _conversation import ConversationJournal
from _conversation_runtime import ConversationRuntime, ConversationRuntimeError
from _swarm_coordinator import (SwarmCoordinator, SwarmCoordinatorError,
                                SwarmConflictError, MAX_CLOCK_SKEW_MS)


def _room(tmp_path, mode):
    root = tmp_path / "rooms"
    roster = tmp_path / "agents"
    roster.mkdir()
    room = ConversationJournal.create(
        root, session_id="room", project_id="synthetic", project_root=tmp_path,
        initiator_host="codex", initiator_agent="human", mode="chat",
        participants=[{"agent": "worker", "role": "researcher"}])
    if mode == "historical":
        records = [json.loads(line) for line in room.path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            record["schema_version"] = 1
        room.path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        room = ConversationJournal.open(root, "room")
    if mode == "bound":
        room.append("fork_created", "system", "summon", {
            "parent_session_id": "ancestor", "child_session_id": "room",
            "reason": "synthetic replacement", "parent_schema_version": 2,
            "parent_history_sha256": "a" * 64, "lineage": "explicit-participant-replacement",
            "participant_replacement": {"from_participant": "former", "to_participant": "worker",
                "agent_definition_sha256": "b" * 64, "permission_ceiling": "read-only"}},
            event_id="initial-binding")
    runtime = ConversationRuntime(root, cwd=tmp_path, agents_dir=roster)
    return root, room, runtime


def _inventory(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mode", ["ordinary", "historical", "bound"],
                         ids=["ordinary", "historical", "bound"])
@pytest.mark.parametrize("length", [128, 129, 256, 512],
                         ids=["reason_128", "reason_129", "reason_256", "reason_512"])
def test_fork_reason_is_validated_before_mutation(tmp_path, mode, length):
    root, room, runtime = _room(tmp_path, mode)
    before = _inventory(root)
    reason = "R" * length
    try:
        if length > 128:
            with pytest.raises(ConversationRuntimeError, match="invalid fork reason"):
                runtime.fork_turn("room", "worker", "synthetic context", reason=reason)
            assert _inventory(root) == before
        else:
            result = runtime.fork_turn("room", "worker", "synthetic context", reason=reason)
            assert result["status"] == "forked" and result["reason"] == reason
            child = ConversationJournal.open(root, result["session_id"])
            assert child._header["participants"] == room._header["participants"]
            assert any(event["event"] == "human_message" for event in child.events(native=True))
            if mode != "ordinary":
                assert room.path.read_bytes() == before[room.path.name]
                assert result["parent_unchanged"] is True
            if mode == "bound":
                bindings = runtime._replacement_bindings(child)
                assert bindings["worker"]["permission_ceiling"] == "read-only"
                assert bindings["worker"]["agent_definition_sha256"] == "b" * 64
                assert result["execution_authorized"] is False
                assert result["provider_contacted"] is False
    finally:
        runtime.close(timeout=1)


class Clock:
    value = 2_000_000_000.0
    def __call__(self):
        return self.value


def _coordinator(tmp_path):
    clock = Clock()
    coordinator = SwarmCoordinator.create(
        tmp_path, "run", project_root_sha256="a" * 64, roster_definition_sha256="b" * 64,
        tasks=[{"task_id": "task", "request_sha256": "c" * 64},
               {"task_id": "other", "request_sha256": "c" * 64}],
        max_attempts=2, clock=clock)
    for worker in ("worker", "other-worker"):
        coordinator.register_worker(worker, worker_instance_id=worker + "-instance")
    return coordinator, clock


def _claim(coordinator, **overrides):
    args = {"worker_id": "worker", "task_id": "task", "request_sha256": "c" * 64,
            "lease_ms": 30000, "message_id": "same-claim"}
    args.update(overrides)
    return coordinator.claim(**args)


@pytest.mark.parametrize("replay", ["direct", "reopened", "renewed"],
                         ids=["direct_retry", "reopened_retry", "renewed_retry"])
def test_claim_helper_returns_original_response_without_new_authority(tmp_path, replay):
    coordinator, clock = _coordinator(tmp_path)
    original = _claim(coordinator)
    clock.value += 1
    if replay == "renewed":
        renewed = coordinator.renew("worker", original["claim_id"], original["lease_generation"], lease_ms=60000)
        assert renewed["lease_expires_at_ms"] > original["lease_expires_at_ms"]
    if replay != "direct":
        coordinator = SwarmCoordinator(tmp_path, "run", clock=clock)
    before = coordinator._load_with_snapshot()[2]
    assert _claim(coordinator) == original
    assert coordinator._load_with_snapshot()[2] == before
    state, _ = coordinator._load()
    assert len(state["claims"]) == 1 and len(state["tasks"]["task"]["attempts"]) == 1
    if replay == "renewed":
        assert state["claims"][original["claim_id"]]["lease_expires_at_ms"] == renewed["lease_expires_at_ms"]


@pytest.mark.parametrize("change", ["worker", "task", "request", "lease"],
                         ids=["worker_conflict", "task_conflict", "request_conflict", "lease_conflict"])
def test_claim_same_id_changed_semantics_remain_conflicts(tmp_path, change):
    coordinator, clock = _coordinator(tmp_path)
    _claim(coordinator)
    before = coordinator._load_with_snapshot()[2]
    changes = {"worker": {"worker_id": "other-worker"}, "task": {"task_id": "other"},
               "request": {"request_sha256": "d" * 64}, "lease": {"lease_ms": 30001}}
    with pytest.raises(SwarmConflictError):
        _claim(coordinator, **changes[change])
    assert coordinator._load_with_snapshot()[2] == before


def test_claim_replay_window_refusal_does_not_append(tmp_path):
    coordinator, clock = _coordinator(tmp_path)
    _claim(coordinator)
    before = coordinator._load_with_snapshot()[2]
    clock.value += MAX_CLOCK_SKEW_MS / 1000 + 1
    with pytest.raises(SwarmCoordinatorError, match="outside the replay window"):
        _claim(coordinator)
    assert coordinator._load_with_snapshot()[2] == before


@pytest.mark.parametrize("refusal", ["malformed_digest", "unknown_task"],
                         ids=["malformed_digest_before_owner", "unknown_task_before_owner"])
def test_claim_preowner_refusal_preserves_torn_run_inventory(tmp_path, refusal):
    coordinator, clock = _coordinator(tmp_path)
    root = Path(coordinator.run_dir)
    journal = max(root.glob("journal-g*.jsonl"),
                  key=lambda path: int(path.stem.removeprefix("journal-g")))
    with journal.open("ab") as handle:
        handle.write(b'{"torn":')
    assert coordinator.status()["torn_tail"] is True
    # Include generation, owner/control files and every journal segment, not
    # merely the logical journal projection: even acquiring ownership is an
    # unauthorized effect for these pre-owner input refusals.
    before = _inventory(root)
    if refusal == "malformed_digest":
        with pytest.raises(SwarmProtocolError, match="invalid request digest"):
            _claim(coordinator, request_sha256="invalid")
    else:
        with pytest.raises(SwarmCoordinatorError, match="unknown task"):
            _claim(coordinator, task_id="missing-task")
    assert _inventory(root) == before
    assert coordinator.status()["torn_tail"] is True
    # A subsequent valid mutation still performs the existing canonical torn
    # tail repair. Prevalidation did not disable or reinterpret repair policy.
    assert _claim(coordinator)["status"] == "claimed"
    assert coordinator.status()["torn_tail"] is False


def test_concurrent_helper_owner_contention_then_same_id_recovery(tmp_path, monkeypatch):
    coordinator, clock = _coordinator(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_apply = coordinator._apply_claim
    outcomes, errors = [], []
    def hold_claim(*args, **kwargs):
        entered.set()
        assert release.wait(5), "synthetic owner barrier timed out"
        return original_apply(*args, **kwargs)
    monkeypatch.setattr(coordinator, "_apply_claim", hold_claim)
    def first():
        try:
            outcomes.append(_claim(coordinator))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        # Existing physical owner contention is still a refusal. It is distinct
        # from the semantic retry conflict repaired by this helper change.
        with pytest.raises(OwnerHeldError):
            _claim(coordinator)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == [] and len(outcomes) == 1
    before = coordinator._load_with_snapshot()[2]
    assert _claim(coordinator) == outcomes[0]
    assert coordinator._load_with_snapshot()[2] == before
