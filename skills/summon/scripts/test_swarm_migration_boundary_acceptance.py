"""Ordinary swarm operations must refuse foreign history before ownership."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _swarm_coordinator as swarm


@pytest.fixture
def history():
    from test_swarm_projection_rebuild import ProjectionRebuildTests
    fixture = ProjectionRebuildTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


@pytest.mark.parametrize("invalid,torn", [
    pytest.param("future-schema", False, id="future-schema-complete"),
    pytest.param("foreign-run", False, id="foreign-run-complete"),
    pytest.param("unknown-event", False, id="unknown-event-complete"),
    pytest.param("future-schema", True, id="future-schema-torn"),
    pytest.param("foreign-run", True, id="foreign-run-torn"),
    pytest.param("unknown-event", True, id="unknown-event-torn"),
])
def test_ordinary_mutation_refuses_unsupported_history_before_ownership(
        history, monkeypatch, invalid, torn):
    path = history.run_dir / "journal-g1.jsonl"
    records = history.records(path.read_bytes())
    if invalid == "future-schema":
        records[0]["schema"] = 2
    elif invalid == "foreign-run":
        records[0]["run_id"] = "another-run"
    else:
        records.append({"event": "future-workspace-event", "generation": 1,
                        "message_id": "future-event"})
    path.write_bytes(history.encoded(records) + (b'{"partial":' if torn else b""))
    before = snapshot(history.run_dir)
    acquired = []
    repaired = []
    real_acquire = swarm.acquire_owner
    real_repair = swarm.journal_repair

    def acquire(*args, **kwargs):
        acquired.append(True)
        return real_acquire(*args, **kwargs)

    def repair(*args, **kwargs):
        repaired.append(True)
        return real_repair(*args, **kwargs)

    monkeypatch.setattr(swarm, "acquire_owner", acquire)
    monkeypatch.setattr(swarm, "journal_repair", repair)
    refused = False
    try:
        history.coordinator.register_worker("worker-1", worker_instance_id="instance-1")
    except swarm.SwarmCoordinatorError:
        refused = True
    assert refused, "ordinary operation accepted an unsupported history"
    assert not acquired, "unsupported history acquired mutation ownership"
    assert not repaired, "unsupported history reached journal repair"
    assert snapshot(history.run_dir) == before, "refusal changed retained history"


@pytest.mark.parametrize("field,value", [("schema", 2), ("run_id", "another-run")],
                         ids=["future-schema", "foreign-run"])
def test_ordinary_status_refuses_foreign_preparation(history, field, value):
    path = history.run_dir / "journal-g1.jsonl"
    records = history.records(path.read_bytes())
    records[0][field] = value
    path.write_bytes(history.encoded(records))
    before = snapshot(history.run_dir)
    with pytest.raises(swarm.SwarmCoordinatorError):
        history.coordinator.status()
    assert snapshot(history.run_dir) == before


def registration_frame(history):
    return swarm.make_frame(
        "register_worker", run_id=history.coordinator.run_id,
        message_id="direct-registration", sent_at_ms=swarm._now_ms(history.coordinator.clock),
        payload={"worker_id": "worker-1", "worker_instance_id": "instance-1",
                 "project_root_sha256": history.project,
                 "roster_definition_sha256": history.roster,
                 "capabilities": [], "permission_ceiling": "read-only"},
    )


def rewrite_history(history, invalid, *, torn=True):
    path = history.run_dir / "journal-g1.jsonl"
    original = path.read_bytes().split(b'{"partial":', 1)[0]
    records = history.records(original)
    if invalid == "future-schema":
        records[0]["schema"] = 2
    elif invalid == "foreign-run":
        records[0]["run_id"] = "another-run"
    else:
        records.append({"event": "future-workspace-event", "generation": 1,
                        "message_id": "future-event"})
    path.write_bytes(history.encoded(records) + (b'{"partial":' if torn else b""))


@pytest.mark.parametrize("operation,invalid,torn", [
    pytest.param('apply-frame', 'future-schema', False, id='apply-frame-future-schema-complete'),
    pytest.param('apply-frame', 'future-schema', True, id='apply-frame-future-schema-torn'),
    pytest.param('apply-frame', 'foreign-run', False, id='apply-frame-foreign-run-complete'),
    pytest.param('apply-frame', 'foreign-run', True, id='apply-frame-foreign-run-torn'),
    pytest.param('apply-frame', 'unknown-event', False, id='apply-frame-unknown-event-complete'),
    pytest.param('apply-frame', 'unknown-event', True, id='apply-frame-unknown-event-torn'),
    pytest.param('close', 'future-schema', False, id='close-future-schema-complete'),
    pytest.param('close', 'future-schema', True, id='close-future-schema-torn'),
    pytest.param('close', 'foreign-run', False, id='close-foreign-run-complete'),
    pytest.param('close', 'foreign-run', True, id='close-foreign-run-torn'),
    pytest.param('close', 'unknown-event', False, id='close-unknown-event-complete'),
    pytest.param('close', 'unknown-event', True, id='close-unknown-event-torn'),
])
def test_direct_mutation_entrypoints_refuse_before_ownership(history, monkeypatch, operation, invalid, torn):
    frame = registration_frame(history)
    rewrite_history(history, invalid, torn=torn)
    before = snapshot(history.run_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported history crossed ownership or repair boundary")

    monkeypatch.setattr(swarm, "acquire_owner", forbidden)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmCorruptError):
        if operation == "apply-frame":
            history.coordinator.apply_frame(frame, worker_id="worker-1")
        else:
            history.coordinator.close()
    assert snapshot(history.run_dir) == before


@pytest.mark.parametrize("invalid", ["future-schema", "foreign-run", "unknown-event"],
                         ids=["future-schema", "foreign-run", "unknown-event"])
def test_acquisition_race_revalidates_current_history_before_repair(history, monkeypatch, invalid):
    frame = registration_frame(history)
    path = history.run_dir / "journal-g1.jsonl"
    path.write_bytes(path.read_bytes() + b'{"partial":')
    before_generation = (history.run_dir / "generation.txt").read_bytes()
    real_acquire = swarm.acquire_owner
    acquired, external = [], {}

    def acquire(*args, **kwargs):
        owner = real_acquire(*args, **kwargs)
        acquired.append(owner)
        rewrite_history(history, invalid)
        external.update(history.journals())
        return owner

    def forbidden(*args, **kwargs):
        pytest.fail("new unsupported prefix reached repair")

    monkeypatch.setattr(swarm, "acquire_owner", acquire)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmCorruptError):
        history.coordinator.apply_frame(frame, worker_id="worker-1")
    assert len(acquired) == 1
    assert (history.run_dir / "generation.txt").read_bytes() != before_generation
    assert history.journals() == external
    assert not (history.run_dir / "owner.lock").exists()


@pytest.mark.parametrize("field,value", [
    pytest.param("schema", True, id="boolean-schema"),
    pytest.param("schema", "1", id="string-schema"),
    pytest.param("schema", 1.0, id="float-schema"),
    pytest.param("schema", None, id="null-schema"),
    pytest.param("run_id", None, id="null-run-id"),
    pytest.param("run_id", "../foreign", id="invalid-run-id"),
])
def test_preparation_identity_is_exact_without_coercion(history, monkeypatch, field, value):
    path = history.run_dir / "journal-g1.jsonl"
    records = history.records(path.read_bytes())
    records[0][field] = value
    path.write_bytes(history.encoded(records))
    before = snapshot(history.run_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid preparation crossed ownership boundary")

    monkeypatch.setattr(swarm, "acquire_owner", forbidden)
    with pytest.raises(swarm.SwarmCorruptError):
        history.coordinator.status()
    with pytest.raises(swarm.SwarmCorruptError):
        history.coordinator.events()
    with pytest.raises(swarm.SwarmCorruptError):
        history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
    assert snapshot(history.run_dir) == before


def test_supported_torn_prefix_retains_pending_identity_and_repairs_on_direct_mutation(history):
    before = history.coordinator._load()[0]
    path = history.run_dir / "journal-g1.jsonl"
    canonical = path.read_bytes()
    path.write_bytes(canonical + b'{"partial":')
    observed = snapshot(history.run_dir)
    assert history.coordinator.status()["torn_tail"] is True
    assert snapshot(history.run_dir) == observed
    result = history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
    assert result["status"] == "registered"
    after, torn = history.coordinator._load()
    assert not torn
    assert after["run_id"] == before["run_id"]
    assert after["tasks"] == before["tasks"]
    assert after["project_root_sha256"] == before["project_root_sha256"]
    assert after["roster_definition_sha256"] == before["roster_definition_sha256"]
    assert path.read_bytes().splitlines() == canonical.splitlines()
    assert any(record["event"] == "journal_repaired" for record in history.coordinator._read_records()[0])


def test_supported_new_prefix_during_acquisition_is_replayed_and_preserved(history, monkeypatch):
    real_acquire = swarm.acquire_owner
    path = history.run_dir / "journal-g1.jsonl"
    external = {}

    def acquire(*args, **kwargs):
        owner = real_acquire(*args, **kwargs)
        records = history.records(path.read_bytes())
        records.append({"event": "worker_registered", "generation": 1,
                        "message_id": "external-registration", "worker_id": "external-worker",
                        "worker_instance_id": "external-instance",
                        "project_root_sha256": history.project,
                        "roster_definition_sha256": history.roster, "permission_ceiling": "read-only"})
        external["canonical"] = history.encoded(records)
        path.write_bytes(external["canonical"] + b'{"partial":')
        return owner

    monkeypatch.setattr(swarm, "acquire_owner", acquire)
    assert history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")["status"] == "registered"
    state, torn = history.coordinator._load()
    assert not torn
    assert set(state["workers"]) == {"external-worker", "worker-1"}
    assert path.read_bytes().splitlines() == external["canonical"].splitlines()


def test_lost_owner_after_revalidation_cannot_repair_torn_prefix(history, monkeypatch):
    path = history.run_dir / "journal-g1.jsonl"
    path.write_bytes(path.read_bytes() + b'{"partial":')
    before = history.journals()
    real_load = history.coordinator._load_with_snapshot
    real_acquire = swarm.acquire_owner
    real_release = swarm.release_owner
    acquired, successors = [], []

    def acquire(*args, **kwargs):
        owner = real_acquire(*args, **kwargs)
        acquired.append(owner)
        return owner

    def load(**kwargs):
        result = real_load(**kwargs)
        if acquired and not successors:
            real_release(acquired[0])
            successors.append(real_acquire(history.coordinator.run_dir, 30.0))
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("lost owner reached journal repair")

    monkeypatch.setattr(swarm, "acquire_owner", acquire)
    monkeypatch.setattr(history.coordinator, "_load_with_snapshot", load)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    try:
        with pytest.raises(swarm.SwarmCorruptError, match="owner changed before journal repair"):
            history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
        assert len(acquired) == len(successors) == 1
        assert history.journals() == before
        assert swarm.owner_still_current(successors[0])
        assert not swarm.owner_still_current(acquired[0])
    finally:
        for owner in successors:
            real_release(owner)


def test_reconciliation_still_refuses_supported_torn_prefix_without_repair(history, monkeypatch):
    path = history.run_dir / "journal-g1.jsonl"
    path.write_bytes(path.read_bytes() + b'{"partial":')
    before = history.journals()

    def forbidden(*args, **kwargs):
        pytest.fail("reconciliation unexpectedly repaired a torn prefix")

    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmCorruptError, match="reconciliation refuses"):
        with history.coordinator._mutation(repair=False):
            pytest.fail("torn reconciliation admitted mutation")
    assert history.journals() == before
    assert not (history.run_dir / "owner.lock").exists()


def test_unsupported_history_retains_existing_uncertain_claim_evidence(history, monkeypatch):
    now = [2_000_000_000.0]
    history.coordinator.clock = lambda: now[0]
    history.coordinator.register_worker("worker-1", worker_instance_id="instance-1")
    claim = history.coordinator.claim("worker-1", "task-1", request_sha256=history.request,
                                      lease_ms=1000, message_id="pending-claim")
    now[0] += 10
    status = history.coordinator.status()
    assert status["uncertain_spend"] is True
    assert status["tasks"][0]["status"] == "expired"
    assert claim["claim_id"]
    rewrite_history(history, "future-schema", torn=False)
    before = snapshot(history.run_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported uncertain history acquired ownership or repair")

    monkeypatch.setattr(swarm, "acquire_owner", forbidden)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmCorruptError):
        history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
    assert snapshot(history.run_dir) == before


@pytest.mark.parametrize("operation", ["status", "events", "apply-frame", "close", "create"],
                         ids=["status", "events", "apply-frame", "close", "create"])
def test_wholly_torn_preparation_is_preserved_before_ownership(history, monkeypatch, operation):
    path = history.run_dir / "journal-g1.jsonl"
    complete = path.read_bytes()
    path.write_bytes(complete[:len(complete) // 2])
    before = snapshot(history.run_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("unrecognized preparation acquired ownership or repair")

    monkeypatch.setattr(swarm, "acquire_owner", forbidden)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmCorruptError, match="no complete preparation"):
        if operation == "apply-frame":
            history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
        elif operation == "create":
            swarm.SwarmCoordinator.create(history.temp.name, "run-1",
                project_root_sha256=history.project, roster_definition_sha256=history.roster,
                tasks=[{"task_id": "task-1", "request_sha256": history.request}])
        else:
            getattr(history.coordinator, operation)()
    assert snapshot(history.run_dir) == before


@pytest.mark.parametrize("operation", ["apply-frame", "create"], ids=["apply-frame", "create"])
def test_observed_preparation_cannot_disappear_during_acquisition(history, monkeypatch, operation):
    path = history.run_dir / "journal-g1.jsonl"
    real_acquire = swarm.acquire_owner
    external, acquired = {}, []

    def acquire(*args, **kwargs):
        owner = real_acquire(*args, **kwargs)
        acquired.append(owner)
        path.unlink()
        external.update(history.journals())
        return owner

    def forbidden(*args, **kwargs):
        pytest.fail("disappeared preparation reached repair")

    monkeypatch.setattr(swarm, "acquire_owner", acquire)
    monkeypatch.setattr(swarm, "journal_repair", forbidden)
    with pytest.raises(swarm.SwarmNotFoundError):
        if operation == "apply-frame":
            history.coordinator.apply_frame(registration_frame(history), worker_id="worker-1")
        else:
            swarm.SwarmCoordinator.create(history.temp.name, "run-1",
                project_root_sha256=history.project, roster_definition_sha256=history.roster,
                tasks=[{"task_id": "task-1", "request_sha256": history.request}])
    assert len(acquired) == 1
    assert history.journals() == external == {}
    assert not (history.run_dir / "owner.lock").exists()


def test_only_explicit_creation_accepts_empty_history(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    coordinator = swarm.SwarmCoordinator(str(root), "fresh")

    def forbidden(*args, **kwargs):
        pytest.fail("ordinary empty-history mutation acquired ownership")

    with monkeypatch.context() as patch:
        patch.setattr(swarm, "acquire_owner", forbidden)
        with pytest.raises(swarm.SwarmNotFoundError):
            coordinator.close()
    assert not Path(coordinator.run_dir).exists()
    created = swarm.SwarmCoordinator.create(str(root), "fresh",
        project_root_sha256="a" * 64, roster_definition_sha256="b" * 64,
        tasks=[{"task_id": "task-1", "request_sha256": "c" * 64}])
    assert created.status()["status"] == "prepared"

