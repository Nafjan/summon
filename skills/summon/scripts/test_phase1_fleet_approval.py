"""Provider-inert M3b1 fleet approval-store regression tests."""

from __future__ import annotations

import ast
import concurrent.futures
from contextlib import contextmanager
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _evidence
import _fleet
import _fleet_approval
import _fleet_compile
import _job_control


def _agents():
    return [{
        "name": "reviewer", "run_agent": "definitely-missing-provider",
        "permission": "read-only", "model": "frontier-reviewer",
        "source": "project", "lifecycle": "active",
    }]


def _plan(tmp_path):
    _report, fleet, plan = _fleet.proposal(
        lane="review", seats=["reviewer"], agents=_agents(), cwd=str(tmp_path),
        permission_ceiling="read-only",
        data_boundary="local_sanitized",
        corrective={"contract_repair": False, "retry": False,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": False, "payg": False,
               "max_provider_contacts": 1, "max_billable_attempts": 1,
               "max_parallel": 1})
    return fleet, plan


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    return root


def test_approval_module_cannot_select_or_dispatch_and_expiry_is_explicit():
    tree = ast.parse(Path(_fleet_approval.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert not imports.intersection({"_decision", "_executor", "_background",
                                     "_resolver", "_apibackend"})
    assert not hasattr(_fleet_approval, "dispatch")
    assert not hasattr(_fleet_approval, "select")
    assert _fleet_approval.parse_expiry("60s") == 60
    assert _fleet_approval.parse_expiry("24h") == 86400
    for invalid in ("60", "1ms", "59s", "31d", "0h"):
        with pytest.raises(_evidence.EvidenceError):
            _fleet_approval.parse_expiry(invalid)


def test_recorded_approval_is_authenticated_redacted_and_non_authoritative(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    assert receipt["provider_contacted"] is False
    assert receipt["authorization"] == "recorded_not_activated"
    assert receipt["selection"] is None
    assert receipt["dispatch_available"] is False
    assert receipt["approval"]["state"] == "active"
    assert receipt["approval"]["fleet_sha256"] == fleet["sha256"]
    assert receipt["approval"]["plan_sha256"] == plan["sha256"]
    constraints = plan["lanes"][0]["constraints"]
    assert receipt["authority"] == {
        "permission_ceiling": constraints["permission_ceiling"],
        "data_boundary": constraints["data_boundary"],
        "corrective": constraints["corrective"],
        "spend": constraints["spend"],
    }
    public = json.dumps(receipt, sort_keys=True)
    for forbidden in (str(private_store), "store_id", '"actor"', '"mac"',
                      "store.key"):
        assert forbidden not in public

    status = _fleet_approval.status()
    assert status["selection"] is None
    assert status["dispatch_available"] is False
    assert status["generation"] == 1
    assert status["counts"] == {"active": 1, "expired": 0, "revoked": 0}
    listed = _fleet_approval.list_approvals()
    assert listed["selection"] is None
    assert listed["dispatch_available"] is False
    assert len(listed["approvals"]) == 1
    inspected = _fleet_approval.inspect(receipt["approval"]["approval_id"])
    assert inspected == receipt | {"action": "fleet_approval_inspected"}


def test_identical_concurrent_approval_is_idempotent_and_stale_conflict_fails(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)

    def record():
        return _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: record(), range(2)))
    assert results[0]["approval"]["approval_id"] == results[1]["approval"]["approval_id"]
    assert _fleet_approval.status()["generation"] == 1


def test_same_scope_different_lifetime_creates_distinct_issuance(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    long = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=30 * 86400, expected_generation=0)
    short = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=60, expected_generation=1)
    assert short["approval"]["approval_id"] != long["approval"]["approval_id"]
    assert short["approval"]["generation"] == 2
    assert (dt.datetime.fromisoformat(
        short["approval"]["expires_at"].replace("Z", "+00:00"))
        - dt.datetime.fromisoformat(
            short["approval"]["issued_at"].replace("Z", "+00:00"))).total_seconds() == 60

    renewed = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=30 * 86400, expected_generation=2)
    assert renewed["approval"]["approval_id"] == long["approval"]["approval_id"]
    assert renewed["approval"]["expires_at"] == long["approval"]["expires_at"]
    assert renewed["approval"]["generation"] == 1
    assert renewed["approval"]["store_generation"] == 2
    assert _fleet_approval.status()["generation"] == 2

    changed_payload = _evidence.verify(fleet)
    changed_payload = json.loads(json.dumps(changed_payload))
    changed_payload["lanes"][0]["constraints"]["data_boundary"] = "private_local"
    changed = _evidence.seal("summon.fleet/v1", changed_payload)
    changed_plan, _ = _fleet.compile_document(
        fleet=changed, agents=_agents(), cwd=str(tmp_path))
    with pytest.raises(_evidence.EvidenceError, match="generation changed"):
        _fleet_approval.approve(
            fleet=changed, plan=changed_plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    assert _fleet_approval.status()["generation"] == 2


def test_store_and_inner_record_forgery_fail_closed(tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    path = Path(_fleet_approval.store_path())
    key = Path(_fleet_approval.key_path()).read_bytes()
    original = json.loads(path.read_text(encoding="utf-8"))
    approval_id = receipt["approval"]["approval_id"]

    tampered = json.loads(json.dumps(original))
    tampered["generation"] = 99
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="authentication failed"):
        _fleet_approval.status()

    tampered = json.loads(json.dumps(original))
    tampered["approvals"][approval_id]["bindings"]["project_sha256"] = "0" * 64
    body = _fleet_approval._unsigned(tampered)
    tampered["mac"] = _fleet_approval._mac(key, body)
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="record|identity|authentication"):
        _fleet_approval.status()

    path.write_text(json.dumps(original), encoding="utf-8")
    assert _fleet_approval.status()["generation"] == 1


def test_store_rejects_duplicate_oversize_hardlink_and_clock_rollback(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    store_path = Path(_fleet_approval.store_path())
    key_path = Path(_fleet_approval.key_path())
    original_store = store_path.read_bytes()
    key = key_path.read_bytes()

    duplicate = original_store.decode("utf-8").rstrip()[:-1] + ',"generation":1}'
    store_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="duplicate"):
        _fleet_approval.status()

    store_path.write_bytes(b"x" * (_fleet_approval.MAX_STORE_BYTES + 1))
    with pytest.raises(_evidence.EvidenceError, match="exceeds"):
        _fleet_approval.status()

    store_path.write_bytes(original_store)
    hardlink = private_store / "key-link"
    try:
        os.link(key_path, hardlink)
    except OSError:
        hardlink = None
    if hardlink is not None:
        with pytest.raises(_evidence.EvidenceError, match="single-link"):
            _fleet_approval.status()
        hardlink.unlink()

    value = json.loads(original_store)
    value["last_seen_at"] = (dt.datetime.now(dt.timezone.utc)
                             + dt.timedelta(hours=2)).replace(
                                 microsecond=0).isoformat().replace("+00:00", "Z")
    body = _fleet_approval._unsigned(value)
    value["mac"] = _fleet_approval._mac(key, body)
    store_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="clock rollback"):
        _fleet_approval.status()


def test_store_refuses_oversize_write_without_corrupting_prior_state(
        tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)
    _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    path = Path(_fleet_approval.store_path())
    original = path.read_bytes()
    changed_payload = json.loads(json.dumps(_evidence.verify(fleet)))
    changed_payload["lanes"][0]["constraints"]["data_boundary"] = "private_local"
    changed = _evidence.seal("summon.fleet/v1", changed_payload)
    changed_plan, _ = _fleet.compile_document(
        fleet=changed, agents=_agents(), cwd=str(tmp_path))
    original_limit = _fleet_approval.MAX_STORE_BYTES
    monkeypatch.setattr(_fleet_approval, "MAX_STORE_BYTES", len(original) + 32)
    with pytest.raises(_evidence.EvidenceError, match="revocation headroom"):
        _fleet_approval.approve(
            fleet=changed, plan=changed_plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=1)
    assert path.read_bytes() == original
    monkeypatch.setattr(_fleet_approval, "MAX_STORE_BYTES", original_limit)
    assert _fleet_approval.status()["generation"] == 1


def test_expired_state_is_reported_without_mutation(
        tmp_path, private_store, monkeypatch):
    issued = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(_fleet_approval, "_now", lambda: issued)
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=60, expected_generation=0)
    monkeypatch.setattr(
        _fleet_approval, "_now", lambda: issued + dt.timedelta(seconds=61))
    assert _fleet_approval.inspect(
        receipt["approval"]["approval_id"])["approval"]["state"] == "expired"
    assert _fleet_approval.status()["counts"] == {
        "active": 0, "expired": 1, "revoked": 0}


def test_next_mutation_compacts_expired_authority(
        tmp_path, private_store, monkeypatch):
    monkeypatch.setattr(_fleet_approval, "MAX_COLLECTION_RECORDS", 1)
    issued = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(_fleet_approval, "_now", lambda: issued)
    fleet, plan = _plan(tmp_path)
    expired = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=60, expected_generation=0)
    monkeypatch.setattr(
        _fleet_approval, "_now", lambda: issued + dt.timedelta(seconds=61))
    active = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=120, expected_generation=1)
    assert active["approval"]["generation"] == 2
    with pytest.raises(_evidence.EvidenceError, match="does not exist"):
        _fleet_approval.inspect(expired["approval"]["approval_id"])
    assert _fleet_approval.status()["counts"] == {
        "active": 1, "expired": 0, "revoked": 0}


def test_active_capacity_refuses_without_mutating_store(
        tmp_path, private_store, monkeypatch):
    monkeypatch.setattr(_fleet_approval, "MAX_COLLECTION_RECORDS", 1)
    fleet, plan = _plan(tmp_path)
    _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    path = Path(_fleet_approval.store_path())
    original = path.read_bytes()
    with pytest.raises(_evidence.EvidenceError, match="full of active approvals"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=7200, expected_generation=1)
    assert path.read_bytes() == original
    assert _fleet_approval.status()["generation"] == 1


def test_byte_cap_reserves_first_revocation(
        tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)
    issued = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    path = Path(_fleet_approval.store_path())
    original = path.read_bytes()

    _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=7200, expected_generation=1)
    two_approval_size = path.stat().st_size
    path.write_bytes(original)

    _fleet_approval.revoke(
        issued["approval"]["approval_id"], expected_generation=1)
    first_revoke_size = path.stat().st_size
    path.write_bytes(original)

    limit = max(two_approval_size, first_revoke_size)
    monkeypatch.setattr(_fleet_approval, "MAX_STORE_BYTES", limit)
    with pytest.raises(_evidence.EvidenceError, match="revocation headroom"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=7200, expected_generation=1)
    assert path.read_bytes() == original
    revoked = _fleet_approval.revoke(
        issued["approval"]["approval_id"], expected_generation=1)
    assert revoked["approval"]["state"] == "revoked"
    assert path.stat().st_size <= limit


def test_revocation_reserve_covers_bounded_worst_case_generation(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    issued = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    key = Path(_fleet_approval.key_path()).read_bytes()
    now = _fleet_approval._now()
    store = json.loads(Path(_fleet_approval.store_path()).read_text(encoding="utf-8"))
    store["generation"] = _fleet_approval.MAX_GENERATION - 1
    store["last_seen_at"] = _fleet_approval._timestamp(now)
    body = _fleet_approval._unsigned(store)
    store["mac"] = _fleet_approval._mac(key, body)
    _fleet_approval._validate_store(store, key, now)
    unrevoked = json.loads(json.dumps(store))
    before = len(json.dumps(store, ensure_ascii=False).encode("utf-8"))

    approval_id = issued["approval"]["approval_id"]
    store["revocations"][approval_id] = {
        "generation": _fleet_approval.MAX_GENERATION,
        "revoked_at": _fleet_approval._timestamp(now),
        "reason": "operator_revoked",
    }
    store["generation"] = _fleet_approval.MAX_GENERATION
    body = _fleet_approval._unsigned(store)
    store["mac"] = _fleet_approval._mac(key, body)
    _fleet_approval._validate_store(store, key, now)
    after = len(json.dumps(store, ensure_ascii=False).encode("utf-8"))
    assert after - before <= _fleet_approval.REVOCATION_RESERVE_BYTES

    path = Path(_fleet_approval.store_path())
    frozen = json.dumps(unrevoked).encode("utf-8")
    path.write_bytes(frozen)
    with pytest.raises(_evidence.EvidenceError, match="generation headroom"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=7200,
            expected_generation=_fleet_approval.MAX_GENERATION - 1)
    assert path.read_bytes() == frozen
    revoked = _fleet_approval.revoke(
        approval_id, expected_generation=_fleet_approval.MAX_GENERATION - 1)
    assert revoked["approval"]["store_generation"] == _fleet_approval.MAX_GENERATION
    assert revoked["approval"]["state"] == "revoked"


def test_approval_copied_between_authenticated_stores_is_rejected(
        tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)
    first = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    first_store = json.loads(Path(_fleet_approval.store_path()).read_text(encoding="utf-8"))
    first_record = first_store["approvals"][first["approval"]["approval_id"]]

    other = tmp_path / "other-private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(other / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(other / "store.key"))
    second = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    second_path = Path(_fleet_approval.store_path())
    second_key = Path(_fleet_approval.key_path()).read_bytes()
    second_store = json.loads(second_path.read_text(encoding="utf-8"))
    copied_id = first_record["approval_id"]
    assert copied_id != second["approval"]["approval_id"]
    second_store["approvals"][copied_id] = first_record
    body = _fleet_approval._unsigned(second_store)
    second_store["mac"] = _fleet_approval._mac(second_key, body)
    second_path.write_text(json.dumps(second_store), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="another store|authentication"):
        _fleet_approval.status()


def test_revoke_is_cas_bound_and_publicly_redacted(tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    approval_id = receipt["approval"]["approval_id"]
    with pytest.raises(_evidence.EvidenceError, match="generation changed"):
        _fleet_approval.revoke(approval_id, expected_generation=0)
    revoked = _fleet_approval.revoke(approval_id, expected_generation=1)
    assert revoked["approval"]["state"] == "revoked"
    assert revoked["approval"]["store_generation"] == 2
    assert _fleet_approval.status()["counts"]["revoked"] == 1

    reissued = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=2)
    assert reissued["approval"]["approval_id"] != approval_id
    assert reissued["approval"]["generation"] == 3
    with pytest.raises(_evidence.EvidenceError, match="does not exist"):
        _fleet_approval.inspect(approval_id)
    assert _fleet_approval.inspect(
        reissued["approval"]["approval_id"])["approval"]["state"] == "active"
    assert _fleet_approval.status()["counts"] == {
        "active": 1, "expired": 0, "revoked": 0}


def test_wrong_initial_generation_writes_no_store_or_key(tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    with pytest.raises(_evidence.EvidenceError, match="generation changed"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=3)
    with pytest.raises(_evidence.EvidenceError, match="bounded"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600,
            expected_generation=_fleet_approval.MAX_GENERATION + 1)
    assert not Path(_fleet_approval.store_path()).exists()
    assert not Path(_fleet_approval.key_path()).exists()


def test_linked_lock_refuses_before_private_mutation(tmp_path, private_store):
    private_store.mkdir(parents=True)
    _fleet_approval._secure_private_root(str(private_store))
    target = tmp_path / "unrelated"
    target.write_bytes(b"unchanged")
    lock = Path(_fleet_approval.store_path() + ".lock")
    try:
        lock.symlink_to(target)
    except OSError:
        pytest.skip("symbolic-link creation is unavailable")
    fleet, plan = _plan(tmp_path)
    with pytest.raises(_evidence.EvidenceError, match="reparse"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    assert target.read_bytes() == b"unchanged"
    assert not Path(_fleet_approval.store_path()).exists()
    assert not Path(_fleet_approval.key_path()).exists()


def test_lock_open_error_is_redacted(tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)

    @contextmanager
    def broken_lock(_path):
        raise OSError(f"cannot open {private_store / 'private-name'}")
        yield

    monkeypatch.setattr(_fleet_approval, "_exclusive_control_lock", broken_lock)
    with pytest.raises(_evidence.EvidenceError) as caught:
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    assert str(private_store) not in str(caught.value)
    assert "lock could not be acquired" in str(caught.value)


def test_busy_lock_is_typed_and_retryable_by_the_cli_contract(
        tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)

    @contextmanager
    def busy_lock(_path):
        raise _job_control.ControlBusyError(
            "job control is busy; retry the command")
        yield

    monkeypatch.setattr(_fleet_approval, "_exclusive_control_lock", busy_lock)
    with pytest.raises(_fleet_approval.ApprovalBusyError, match="retry"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    entry = Path(__file__).with_name("run_subagent.py").read_text(encoding="utf-8")
    assert '"fleet_busy"' in entry
    assert "isinstance(exc, _fleet_approval.ApprovalBusyError)" in entry


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_windows_hardening_removes_foreign_explicit_ace(
        tmp_path, private_store):
    private_store.mkdir(parents=True)
    seeded = subprocess.run(
        ["icacls", str(private_store), "/grant", "*S-1-1-0:(OI)(CI)R"],
        capture_output=True, text=True, timeout=15)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    before = _fleet_approval._windows_acl_snapshot(str(private_store))
    before_rules = before["rules"] if isinstance(before["rules"], list) else [before["rules"]]
    assert any(rule["sid"] == "S-1-1-0" for rule in before_rules)
    _fleet_approval._secure_private_root(str(private_store))
    after = _fleet_approval._windows_acl_snapshot(str(private_store))
    after_rules = after["rules"] if isinstance(after["rules"], list) else [after["rules"]]
    assert after["protected"] is True
    assert after["owner"] == _fleet_approval._effective_user_sid()
    assert len(after_rules) == 1
    assert after_rules[0]["sid"] == _fleet_approval._effective_user_sid()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
@pytest.mark.parametrize("collision", ["unrelated.txt", "store.json"])
def test_windows_refuses_to_reacl_shared_override_directory(
        tmp_path, private_store, collision):
    private_store.mkdir(parents=True)
    unrelated = private_store / collision
    unrelated.write_text("unchanged", encoding="utf-8")
    before = _fleet_approval._windows_acl_snapshot(str(private_store))
    fleet, plan = _plan(tmp_path)
    with pytest.raises(_evidence.EvidenceError, match="dedicated empty"):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    assert unrelated.read_text(encoding="utf-8") == "unchanged"
    assert _fleet_approval._windows_acl_snapshot(str(private_store)) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_interrupted_lock_bootstrap_is_cleaned_up(
        tmp_path, private_store, monkeypatch):
    fleet, plan = _plan(tmp_path)
    original = _fleet_approval._secure_file

    def interrupt_lock(path):
        if path.endswith(".lock"):
            raise KeyboardInterrupt()
        return original(path)

    monkeypatch.setattr(_fleet_approval, "_secure_file", interrupt_lock)
    with pytest.raises(KeyboardInterrupt):
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)
    assert not Path(_fleet_approval.store_path() + ".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_safe_inherited_lock_is_repaired_but_foreign_lock_refuses(private_store):
    _fleet_approval._secure_private_root(str(private_store))
    lock = Path(_fleet_approval.store_path() + ".lock")
    lock.write_bytes(b"")
    with _fleet_approval._store_lock():
        pass
    _fleet_approval._verify_private(str(lock), directory=False)

    seeded = subprocess.run(
        ["icacls", str(lock), "/grant", "*S-1-1-0:R"],
        capture_output=True, text=True, timeout=15)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    with pytest.raises(_evidence.EvidenceError, match="lock is unsafe") as caught:
        with _fleet_approval._store_lock():
            pass
    assert not isinstance(caught.value, _fleet_approval.ApprovalBusyError)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_windows_acl_snapshot_uses_native_api_not_shell(
        private_store, monkeypatch):
    private_store.mkdir(parents=True)
    _fleet_approval._secure_private_root(str(private_store))

    def refuse_shell(*_args, **_kwargs):
        raise AssertionError("ACL verification must not spawn a shell")

    _fleet_approval._effective_user_sid.cache_clear()
    monkeypatch.setattr(subprocess, "run", refuse_shell)
    snapshot = _fleet_approval._windows_acl_snapshot(str(private_store))
    assert snapshot["protected"] is True
    assert snapshot["owner"] == _fleet_approval._effective_user_sid()


def test_reparse_inspection_error_does_not_expose_private_path(monkeypatch):
    private = r"C:\\private-user-path\\fleet-approvals.json"

    def refuse_lstat(_path):
        raise PermissionError(13, "access denied", private)

    monkeypatch.setattr(_fleet_approval.os, "lstat", refuse_lstat)
    with pytest.raises(_evidence.EvidenceError) as caught:
        _fleet_approval._is_reparse(private)
    assert private not in str(caught.value)
    assert str(caught.value) == "fleet approval private path could not be inspected"


def test_lock_bootstrap_error_does_not_expose_private_path(
        private_store, monkeypatch):
    _fleet_approval._secure_private_root(str(private_store))
    lock = _fleet_approval.store_path() + ".lock"

    def refuse_open(_path, _flags, _mode):
        raise PermissionError(13, "access denied", lock)

    monkeypatch.setattr(_fleet_approval.os, "open", refuse_open)
    with pytest.raises(_evidence.EvidenceError) as caught:
        with _fleet_approval._store_lock():
            pass
    assert lock not in str(caught.value)
    assert str(caught.value) == "fleet approval store lock could not be created"


def test_authenticated_non_object_approval_is_typed_malformed(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    path = Path(_fleet_approval.store_path())
    key = Path(_fleet_approval.key_path()).read_bytes()
    store = json.loads(path.read_text(encoding="utf-8"))
    store["approvals"][receipt["approval"]["approval_id"]] = "not-an-object"
    store["mac"] = _fleet_approval._mac(
        key, _fleet_approval._unsigned(store))
    path.write_text(json.dumps(store), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="index is malformed"):
        _fleet_approval.status()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_read_refuses_foreign_explicit_key_ace(tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    seeded = subprocess.run(
        ["icacls", _fleet_approval.key_path(), "/grant", "*S-1-1-0:R"],
        capture_output=True, text=True, timeout=15)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    with pytest.raises(_evidence.EvidenceError, match="owner-only"):
        _fleet_approval.status()


def test_cli_records_without_launching_missing_backend(tmp_path, private_store):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text(
        "---\nrun-agent: definitely-missing-provider\n"
        "model: frontier-reviewer\npermission: read-only\n---\n# Reviewer\nReview.\n",
        encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    fleet_path = tmp_path / "fleet.json"
    proposed = subprocess.run(
        [sys.executable, str(script), "fleet", "propose", "review",
         "--seats", "reviewer", "--allow-subscription",
         "--cwd", str(tmp_path), "--agents-dir", str(agents),
         "--out", str(fleet_path), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr

    approved = subprocess.run(
        [sys.executable, str(script), "fleet", "approval", "approve",
         str(fleet_path), "review", "--expires-in", "1h",
         "--expect-generation", "0", "--cwd", str(tmp_path),
         "--agents-dir", str(agents), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    result = json.loads(approved.stdout)
    assert result["provider_contacted"] is False
    assert result["selection"] is None
    assert result["dispatch_available"] is False
    assert "definitely-missing-provider" not in approved.stderr

    refused = subprocess.run(
        [sys.executable, str(script), "fleet", "approval", "approve",
         str(fleet_path), "review", "--expires-in", "1",
         "--expect-generation", "1", "--cwd", str(tmp_path),
         "--agents-dir", str(agents), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert refused.returncode == 1
    error = json.loads(refused.stdout)
    assert error["provider_contacted"] is False
    assert error["selection"] is None
    assert error["dispatch_available"] is False
    assert error["authorization"] == "not_recorded"


def test_cli_occupied_receipt_refuses_before_recording_authority(
        tmp_path, private_store):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text(
        "---\nrun-agent: definitely-missing-provider\n"
        "model: frontier-reviewer\npermission: read-only\n---\n# Reviewer\nReview.\n",
        encoding="utf-8")
    _report, fleet, _plan_value = _fleet.proposal(
        lane="review", seats=["reviewer"], agents=_agents(), cwd=str(tmp_path),
        permission_ceiling="read-only")
    fleet_path = tmp_path / "fleet.json"
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
    occupied = tmp_path / "occupied.json"
    occupied.write_text("do not replace", encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "fleet", "approval", "approve",
         str(fleet_path), "review", "--expires-in", "1h",
         "--expect-generation", "0", "--cwd", str(tmp_path),
         "--agents-dir", str(agents), "--out", str(occupied), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    assert occupied.read_text(encoding="utf-8") == "do not replace"
    result = json.loads(completed.stdout)
    assert result["authorization"] == "not_recorded"
    assert not Path(_fleet_approval.store_path()).exists()
    assert not Path(_fleet_approval.key_path()).exists()


def test_cli_revoke_receipt_failure_reports_durable_mutation(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    approved = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    approval_id = approved["approval"]["approval_id"]
    output = tmp_path / "revocation.json"
    wrapper = tmp_path / "revoke_with_failed_receipt.py"
    script_dir = Path(__file__).parent
    argv = [
        "run_subagent.py", "fleet", "approval", "revoke", approval_id,
        "--expect-generation", "1", "--out", str(output), "--json",
    ]
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import _evidence, _fleet\n"
        "def refuse_receipt(path, value):\n"
        "    if value.get('action') == 'fleet_approval_revoked':\n"
        "        raise _evidence.EvidenceError('simulated receipt publication race')\n"
        "    raise AssertionError('unexpected fleet output')\n"
        "_fleet.write_json = refuse_receipt\n"
        "import run_subagent\n"
        f"sys.argv = {argv!r}\n"
        "run_subagent.main()\n",
        encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(wrapper)], capture_output=True, text=True,
        encoding="utf-8", timeout=30)

    assert completed.returncode == 1
    error = json.loads(completed.stdout)
    assert error["authorization"] == "recorded_receipt_undelivered"
    assert error["provider_contacted"] is False
    assert not output.exists()
    assert _fleet_approval.inspect(approval_id)["approval"]["state"] == "revoked"
    assert _fleet_approval.status()["generation"] == 2


def test_store_parser_reads_more_than_shared_evidence_item_limit(
        tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    first = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    key = _fleet_approval._load_key(create=False)
    store = _fleet_approval._read_store(key, _fleet_approval._now())
    template = store["approvals"][first["approval"]["approval_id"]]
    store["approvals"] = {}
    for generation in range(1, 97):
        approval = json.loads(json.dumps(template))
        approval["generation"] = generation
        approval["bindings"]["lane"] = f"review-{generation}"
        unsigned = _fleet_approval._unsigned(approval)
        unsigned["approval_id"] = "0" * 64
        approval_id = _fleet_approval.hashlib.sha256(
            _fleet_approval._canonical(
                _fleet_approval._approval_identity(unsigned))).hexdigest()
        unsigned["approval_id"] = approval_id
        approval = {**unsigned, "mac": _fleet_approval._mac(key, unsigned)}
        store["approvals"][approval_id] = approval
    store["generation"] = 96
    _fleet_approval._write_store(store, key)

    raw = Path(_fleet_approval.store_path()).read_bytes()
    with pytest.raises(_evidence.EvidenceError, match="values"):
        _evidence.loads(raw, max_bytes=_fleet_approval.MAX_STORE_BYTES)
    loaded = _fleet_approval._read_store(key, _fleet_approval._now())
    assert len(loaded["approvals"]) == 96
    assert _fleet_approval.status()["counts"]["active"] == 96


def test_public_receipt_cannot_be_used_as_fleet_or_plan(tmp_path, private_store):
    fleet, plan = _plan(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    with pytest.raises(_evidence.EvidenceError):
        _fleet.compile_document(fleet=receipt, agents=_agents(), cwd=str(tmp_path))
    with pytest.raises(_evidence.EvidenceError):
        _fleet_compile.verify_plan_for_fleet(receipt, fleet)


def test_private_store_overrides_are_documented():
    skill = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "SUMMON_FLEET_APPROVAL_STORE" in skill
    assert "SUMMON_FLEET_APPROVAL_KEY" in skill


def test_private_store_overrides_require_absolute_paths(monkeypatch):
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", "relative/store.json")
    with pytest.raises(_evidence.EvidenceError, match="must be an absolute path"):
        _fleet_approval.store_path()
    monkeypatch.setenv(
        "SUMMON_FLEET_APPROVAL_STORE", str(Path.cwd() / "private" / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", "relative/store.key")
    with pytest.raises(_evidence.EvidenceError, match="must be an absolute path"):
        _fleet_approval.key_path()
