"""Tests for attempt-scoped mission budget enforcement and accounting (_mission_budget.py)."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _mission_budget  # noqa: E402


def test_mission_budget_attempt_scoped_accounting_no_double_counting(tmp_path):
    job_dir = str(tmp_path / "jobs")
    mid = "mission-test-01"

    # 1. Reserve launch
    state = _mission_budget.reserve_launch(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        agent="sol",
        reserve_work_tokens=10_000,
        max_work_tokens=100_000,
        job_dir=job_dir,
    )
    assert state["schema"] == "summon.mission-budget/v1"
    assert state["reserved_in_flight_work_tokens"] == 10_000
    assert state["total_known_work_tokens"] == 0
    assert state["committed_work_tokens"] == 10_000
    assert state["exhausted"] is False

    # 2. Heartbeat with last_step_snapshot must NOT update total_known_work_tokens
    state = _mission_budget.record_heartbeat_usage(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 5_000,
            "observation_scope": "last_step_snapshot",
            "evidence": "reported",
        },
        observation_seq=1,
        job_dir=job_dir,
    )
    assert state["total_known_work_tokens"] == 0
    assert state["reserved_in_flight_work_tokens"] == 10_000
    assert state["committed_work_tokens"] == 10_000

    # 3. Heartbeat with attempt_total updates the single attempt monotonically
    state = _mission_budget.record_heartbeat_usage(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 12_000,
            "observation_scope": "attempt_total",
            "evidence": "reported",
        },
        observation_seq=2,
        job_dir=job_dir,
    )
    assert state["total_known_work_tokens"] == 12_000
    assert state["reserved_in_flight_work_tokens"] == 0
    assert state["committed_work_tokens"] == 12_000

    # 4. Out-of-order lower observation_seq is ignored
    state = _mission_budget.record_heartbeat_usage(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 90_000,
            "observation_scope": "attempt_total",
            "evidence": "reported",
        },
        observation_seq=1,
        job_dir=job_dir,
    )
    assert state["total_known_work_tokens"] == 12_000
    assert state["committed_work_tokens"] == 12_000

    # 5. Later attempt_total replaces prior attempt_total (never adds 12k + 18k = 30k)
    state = _mission_budget.record_heartbeat_usage(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 18_000,
            "observation_scope": "attempt_total",
            "evidence": "reported",
        },
        observation_seq=3,
        job_dir=job_dir,
    )
    assert state["total_known_work_tokens"] == 18_000
    assert state["committed_work_tokens"] == 18_000


def test_mission_budget_pre_launch_reservation_gate(tmp_path):
    job_dir = str(tmp_path / "jobs")
    mid = "mission-gate-01"

    # Cap is 8,000 tokens; default reserve is min(4096, 8000 // 4) = 2,000 tokens per launch,
    # or explicit 4,000 tokens per launch.
    # Two concurrent launches reserve 4,000 + 4,000 = 8,000 (allowed).
    _mission_budget.reserve_launch(
        mid,
        job_id="job-a",
        attempt_id="att-a",
        agent="sol",
        reserve_work_tokens=4_000,
        max_work_tokens=8_000,
        job_dir=job_dir,
    )
    _mission_budget.reserve_launch(
        mid,
        job_id="job-b",
        attempt_id="att-b",
        agent="luna",
        reserve_work_tokens=4_000,
        max_work_tokens=8_000,
        job_dir=job_dir,
    )

    # Third concurrent launch would push committed to 8,000 + 4,000 > 8,000 -> blocked before spawn!
    with pytest.raises(_mission_budget.MissionBudgetExhaustedError, match="insufficient remaining work_tokens"):
        _mission_budget.reserve_launch(
            mid,
            job_id="job-c",
            attempt_id="att-c",
            agent="fable",
            reserve_work_tokens=4_000,
            max_work_tokens=8_000,
            job_dir=job_dir,
        )

    # Once job-a settles at 1,500 tokens, known=1,500 + reserved=4,000 = 5,500,
    # and a launch with reserve_work_tokens=2,500 succeeds.
    _mission_budget.settle_attempt(
        mid,
        job_id="job-a",
        attempt_id="att-a",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 1_500,
            "observation_scope": "attempt_total",
            "evidence": "reported",
        },
        provider_contacted=True,
        terminal_status="ok",
        job_dir=job_dir,
    )
    state = _mission_budget.reserve_launch(
        mid,
        job_id="job-c",
        attempt_id="att-c",
        agent="fable",
        reserve_work_tokens=2_500,
        max_work_tokens=8_000,
        job_dir=job_dir,
    )
    assert state["total_known_work_tokens"] == 1_500
    assert state["reserved_in_flight_work_tokens"] == 6_500
    assert state["committed_work_tokens"] == 8_000


def test_mission_budget_multi_attempt_envelope_and_unknown_spend(tmp_path):
    job_dir = str(tmp_path / "jobs")
    mid = "mission-multi-01"

    _mission_budget.configure_mission_budget(
        mid, max_work_tokens=100_000, job_dir=job_dir
    )

    # Settle an envelope that had an initial attempt (15k tokens) and a contract_repair attempt (5k tokens)
    envelope = {
        "status": "ok",
        "agent": "sol",
        "provider_contacted": True,
        "attempt_id": "att-repair",
        "attempt_kind": "contract_repair",
        "usage_metrics": {
            "schema": "summon.usage-metrics/v1",
            "work_tokens": 5_000,
            "observation_scope": "attempt_total",
            "evidence": "reported",
        },
        "attempt_history": [
            {
                "attempt_id": "att-initial",
                "attempt_kind": "initial",
                "status": "error",
                "provider_contacted": True,
                "usage_metrics": {
                    "schema": "summon.usage-metrics/v1",
                    "work_tokens": 15_000,
                    "observation_scope": "attempt_total",
                    "evidence": "reported",
                },
            },
            {
                "attempt_id": "att-repair",
                "attempt_kind": "contract_repair",
                "status": "ok",
                "provider_contacted": True,
                "usage_metrics": {
                    "schema": "summon.usage-metrics/v1",
                    "work_tokens": 5_000,
                    "observation_scope": "attempt_total",
                    "evidence": "reported",
                },
            },
        ],
    }
    state = _mission_budget.settle_envelope(
        mid, job_id="job-multi", envelope=envelope, job_dir=job_dir
    )
    assert state["total_known_work_tokens"] == 20_000
    assert state["committed_work_tokens"] == 20_000
    assert state["top_spender"]["job_id"] == "job-multi"
    assert state["top_spender"]["work_tokens"] == 20_000

    # Now settle an attempt where provider_contacted=True but usage_metrics is unestablished
    state = _mission_budget.settle_attempt(
        mid,
        job_id="job-opaque",
        attempt_id="att-opaque",
        usage_metrics={
            "schema": "summon.usage-metrics/v1",
            "work_tokens": None,
            "observation_scope": "unestablished",
            "evidence": "absent",
        },
        provider_contacted=True,
        terminal_status="ok",
        agent="ark",
        job_dir=job_dir,
    )
    assert state["unknown_spend"] is True
    assert state["unknown_spend_attempts"] == ["att-opaque"]
    assert state["exhausted"] is True
    assert "unknown_spend" in state["exhausted_reason"]

    # Subsequent launches must fail closed once exhausted
    with pytest.raises(_mission_budget.MissionBudgetExhaustedError, match="exhausted"):
        _mission_budget.reserve_launch(
            mid, job_id="job-after", attempt_id="att-after", job_dir=job_dir
        )


def test_mission_budget_hmac_tampering_fails_closed(tmp_path):
    job_dir = str(tmp_path / "jobs")
    mid = "mission-hmac-01"
    _mission_budget.configure_mission_budget(
        mid, max_work_tokens=50_000, job_dir=job_dir
    )
    bpath = _mission_budget.budget_path(mid, job_dir=job_dir)
    with open(bpath, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["caps"]["max_work_tokens"] = 999_999_999
    with open(bpath, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    with pytest.raises(_mission_budget.MissionBudgetError, match="HMAC verification"):
        _mission_budget.read_mission_budget(mid, job_dir=job_dir)


def test_dead_owner_reservation_reclaimed_and_idempotent_exhaustion_check(tmp_path):
    job_dir = str(tmp_path / "jobs")
    mid = "mission-reclaim-01"
    _mission_budget.reserve_launch(
        mid,
        job_id="job-dead",
        attempt_id="att-dead",
        agent="sol",
        reserve_work_tokens=8_000,
        max_work_tokens=8_000,
        job_dir=job_dir,
    )
    # Force att-dead to appear owned by a non-existent PID
    bdir = _mission_budget.resolve_mission_budget_dir(mid, job_dir=job_dir)
    bpath, _, key_path = _mission_budget._budget_paths(bdir)
    secret = _mission_budget._load_or_create_secret(key_path)
    state = _mission_budget._read_sealed_state(bpath, secret, mid)
    state["attempts"]["att-dead"]["owner_pid"] = 99999999
    state["attempts"]["att-dead"]["owner_birth"] = "dead:pid"
    _mission_budget._write_sealed_state(bpath, secret, state)

    # Next reserve_launch reclaims the dead owner's reservation as abandoned_owner_dead!
    state2 = _mission_budget.reserve_launch(
        mid,
        job_id="job-live",
        attempt_id="att-live",
        agent="luna",
        reserve_work_tokens=4_000,
        max_work_tokens=8_000,
        job_dir=job_dir,
    )
    assert state2["reserved_in_flight_work_tokens"] == 4_000
    raw_after = _mission_budget._read_sealed_state(bpath, secret, mid)
    assert raw_after["attempts"]["att-dead"]["status"] == "abandoned_owner_dead"


def test_terminate_in_flight_owners_requires_positive_birth_token_match(tmp_path, monkeypatch):
    import _jobs
    import _resource_governor

    job_dir = str(tmp_path / "jobs")
    mid = "mission-birth-guard"
    _mission_budget.reserve_launch(
        mid,
        job_id="job-1",
        attempt_id="att-1",
        agent="sol",
        reserve_work_tokens=1_000,
        max_work_tokens=10_000,
        job_dir=job_dir,
    )
    bdir = _mission_budget.resolve_mission_budget_dir(mid, job_dir=job_dir)
    bpath, _, key_path = _mission_budget._budget_paths(bdir)
    secret = _mission_budget._load_or_create_secret(key_path)
    state = _mission_budget._read_sealed_state(bpath, secret, mid)
    state["attempts"]["att-1"]["owner_pid"] = 54321
    state["attempts"]["att-1"]["owner_birth"] = "expected:token"
    _mission_budget._write_sealed_state(bpath, secret, state)

    killed_pids: list[int] = []
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda pid: "alive")
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append(pid))

    # 1. process_birth_token returns None -> must NOT signal PID
    monkeypatch.setattr(_resource_governor, "process_birth_token", lambda pid: None)
    _mission_budget._enforce_budget_exhaustion_on_active_jobs(
        mid, terminate_in_flight_owners=True, job_dir=job_dir
    )
    assert killed_pids == []

    # Reset status to in_flight and test mismatching token -> must NOT signal PID
    state = _mission_budget._read_sealed_state(bpath, secret, mid)
    state["attempts"]["att-1"]["status"] = "in_flight"
    _mission_budget._write_sealed_state(bpath, secret, state)
    monkeypatch.setattr(_resource_governor, "process_birth_token", lambda pid: "different:token")
    _mission_budget._enforce_budget_exhaustion_on_active_jobs(
        mid, terminate_in_flight_owners=True, job_dir=job_dir
    )
    assert killed_pids == []

    # Reset status to in_flight and test exact match -> signals PID
    state = _mission_budget._read_sealed_state(bpath, secret, mid)
    state["attempts"]["att-1"]["status"] = "in_flight"
    _mission_budget._write_sealed_state(bpath, secret, state)
    monkeypatch.setattr(_resource_governor, "process_birth_token", lambda pid: "expected:token")
    _mission_budget._enforce_budget_exhaustion_on_active_jobs(
        mid, terminate_in_flight_owners=True, job_dir=job_dir
    )
    assert killed_pids == [54321]
