"""Deterministic attempt clocks and canonical prepared/PID/result transitions.

All time advances and process-liveness observations are synthetic. No sleeps,
provider processes or physical process-identity qualification are required.
"""
from types import SimpleNamespace

import pytest

import _job_control
import _jobs


class Clock:
    def __init__(self, value=0.0):
        self.value = value
        self.sleeps = []
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.value

    def time(self):
        return 100.0 + self.value

    def sleep(self, duration):
        assert 0 < duration <= 0.5
        self.sleeps.append(duration)
        self.value += duration
        assert len(self.sleeps) <= 10, "wait exceeded deterministic caller budget"
        self.on_sleep()


def _prepared(tmp_path):
    root, job_id = str(tmp_path), "a" * 32
    _jobs.write_prepared(root, job_id, nonce="synthetic-nonce", agent="synthetic",
                         prompt_sha256=None, cwd=root, flags={}, summon={})
    return root, job_id


def _control(root, job_id, clock, kind="retry"):
    return _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="synthetic-nonce", checkpoint_ms=10000,
        max_runtime_ms=120000, attempt_id="b" * 32, attempt_kind=kind,
        attempt_ordinal=2, job_started_at=100.0,
        clock=clock.monotonic, wall_clock=clock.time)


@pytest.mark.parametrize("kind", ["initial", "retry", "schema_correction", "contract_repair"],
                         ids=["late_initial", "late_retry", "late_schema_correction", "late_contract_repair"])
def test_late_attempt_progress_keeps_job_origin_and_hard_budget(tmp_path, kind):
    root, job_id = _prepared(tmp_path)
    clock = Clock(90.0)
    control = _control(root, job_id, clock, kind)
    assert control.started == 0.0 and control.job_started_at == 100.0
    assert control.deadline == 100.0 and control.hard_deadline == 120.0
    assert control.expired() is False
    assert control.checkpoint(active=True) == 0
    clock.value = 100.0
    assert control.checkpoint(active=True) == 10000
    assert control.expired() is False and control.deadline == 110.0
    assert control.hard_deadline == 120.0 and control.max_runtime_ms == 120000
    assert control.auto_extensions == 1 and control.operator_extensions == 0
    assert control.attempt_kind == kind and control.attempt_id == "b" * 32


@pytest.mark.parametrize("admitted", [115.0, 120.0, 130.0],
                         ids=["remaining_hard_cap", "at_hard_end", "past_hard_end"])
def test_late_attempt_cannot_reset_expired_hard_authority(tmp_path, admitted):
    root, job_id = _prepared(tmp_path)
    clock = Clock(admitted)
    control = _control(root, job_id, clock)
    assert control.started == 0.0 and control.hard_deadline == 120.0
    assert control.deadline == 120.0
    assert control.expired() is (admitted >= 120.0)
    clock.value = max(admitted, 120.0)
    assert control.checkpoint(active=True) == 0
    assert control.expired() is True and control.auto_extensions == 0


def test_late_attempt_authenticated_cancel_and_extension_replay(tmp_path, monkeypatch):
    root, job_id = _prepared(tmp_path)
    clock = Clock(90.0)
    monkeypatch.setattr(_job_control, "time", clock)
    _job_control.queue_command(root, job_id, "extend", duration_ms=20000)
    _job_control.queue_command(root, job_id, "cancel")
    control = _control(root, job_id, clock)
    assert control.refresh(force=True) == 20000
    assert control.cancel_requested is True
    assert control.started == 0.0 and control.job_started_at == 100.0
    assert control.hard_deadline == 140.0 and control.deadline == 120.0
    assert control.max_runtime_ms == 140000 and control.operator_extensions == 1
    assert control.extension_ms == 20000 and control.generation == 2
    assert control.control_untrusted is False
    assert control.refresh(force=True) == 0
    assert control.hard_deadline == 140.0 and control.deadline == 120.0
    assert control.extension_ms == 20000 and control.cancel_requested is True
    # Another attempt replays the same authenticated job authority once;
    # it does not mint an additional aggregate extension.
    clock.value = 100.0
    next_attempt = _control(root, job_id, clock, "contract_repair")
    assert next_attempt.refresh(force=True) == 20000
    assert next_attempt.hard_deadline == 140.0
    assert next_attempt.deadline == 130.0 and next_attempt.cancel_requested is True
    assert next_attempt.refresh(force=True) == 0


def test_extension_queued_after_job_hard_end_does_not_resurrect(tmp_path, monkeypatch):
    root, job_id = _prepared(tmp_path)
    clock = Clock(121.0)
    monkeypatch.setattr(_job_control, "time", clock)
    _job_control.queue_command(root, job_id, "extend", duration_ms=20000)
    control = _control(root, job_id, clock)
    assert control.refresh(force=True) == 0
    assert control.hard_deadline == 120.0 and control.expired() is True
    assert control.operator_extensions == 0


@pytest.mark.parametrize("race", ["none", "before_probe", "at_death", "wrong_nonce", "wrong_identity"],
                         ids=["prepared_pid_dead", "result_before_probe", "result_at_death", "foreign_nonce", "foreign_source_identity"])
def test_wait_refreshes_prepared_record_and_preserves_result_races(tmp_path, monkeypatch, race):
    root, job_id = _prepared(tmp_path)
    clock = Clock()
    monkeypatch.setattr(_jobs, "time", clock)
    probes = []
    result = {"status": "success", "job_nonce": "synthetic-nonce"}
    if race == "wrong_identity":
        rec = _jobs.read_json(_jobs.record_path(root, job_id))
        rec["summon"] = {"scripts_sha256": "c" * 64}
        _jobs._atomic_write_json(_jobs.record_path(root, job_id), rec)
    if race == "wrong_nonce":
        result["job_nonce"] = "foreign-nonce"

    def publish():
        _jobs._atomic_write_json(_jobs.result_path(root, job_id), result)

    def transition():
        _jobs.update_spawned(root, job_id, 4242)
        if race == "before_probe":
            publish()

    clock.on_sleep = transition

    def probe(pid):
        probes.append(pid)
        if len(probes) == 1:
            return "alive"
        if race in {"at_death", "wrong_nonce", "wrong_identity"}:
            publish()
        return "dead"

    monkeypatch.setattr(_jobs, "_pid_liveness", probe)
    actual, outcome = _jobs.wait_job(root, job_id, timeout_ms=5000, poll_sec=0.5)
    if race in {"before_probe", "at_death"}:
        assert actual == result and outcome == "done"
    elif race == "wrong_identity":
        assert actual is None and outcome == "identity_mismatch"
    else:
        assert actual is None and outcome == "stale"
    if race == "before_probe":
        assert probes == [] and clock.sleeps == [0.5]
    else:
        assert probes == [4242, 4242] and clock.sleeps == [0.5, 0.5]
    assert clock.value < 5.0


def test_wait_prepared_without_pid_obeys_original_caller_deadline(tmp_path, monkeypatch):
    root, job_id = _prepared(tmp_path)
    clock = Clock()
    monkeypatch.setattr(_jobs, "time", clock)
    def unexpected_probe(pid):
        raise AssertionError("PID-less prepared record must not probe a process")
    monkeypatch.setattr(_jobs, "_pid_liveness", unexpected_probe)
    result, outcome = _jobs.wait_job(root, job_id, timeout_ms=1200, poll_sec=0.5)
    assert result is None and outcome == "timeout"
    assert clock.value == 1.2
    assert clock.sleeps == pytest.approx([0.5, 0.5, 0.2])
