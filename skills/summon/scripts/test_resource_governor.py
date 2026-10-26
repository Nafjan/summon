"""Tests for hardware-aware dynamic resource governor (`_resource_governor.py`)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _doctor
import _manifest
import _resource_governor as rg


def test_windows_cpu_busy_pct_accounts_for_idle_inside_kernel():
    """On Windows, GetSystemTimes lpKernelTime includes lpIdleTime."""
    # d_idle = 80, d_kernel = 100 (so non-idle kernel = 20), d_user = 20 -> total = 120, busy = 40 -> 33.3%
    s1 = (1000, 2000, 500)
    s2 = (1080, 2100, 520)
    pct = rg.compute_win_cpu_busy_pct(s1, s2)
    assert pct == 33.3

    # Fully idle: d_idle = 100, d_kernel = 100, d_user = 0 -> busy = 0.0%
    assert rg.compute_win_cpu_busy_pct((0, 0, 0), (100, 100, 0)) == 0.0

    # Fully busy: d_idle = 0, d_kernel = 50, d_user = 50 -> busy = 100.0%
    assert rg.compute_win_cpu_busy_pct((0, 0, 0), (0, 50, 50)) == 100.0

    # Invalid / non-monotonic counters return None
    assert rg.compute_win_cpu_busy_pct((100, 100, 100), (90, 100, 100)) is None
    assert rg.compute_win_cpu_busy_pct((0, 0, 0), (200, 100, 0)) is None


def test_parse_linux_meminfo_with_and_without_memavailable():
    meminfo_modern = """\
MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:   10240000 kB
Buffers:          512000 kB
Cached:          4096000 kB
"""
    parsed = rg._parse_linux_meminfo(meminfo_modern)
    assert parsed["status"] == "ok"
    assert parsed["source"] == "proc.meminfo"
    assert parsed["total_ram_mb"] == 16000
    assert parsed["avail_ram_mb"] == 10000
    assert parsed["memory_load_pct"] == 37.5

    meminfo_legacy = """\
MemTotal:       8192000 kB
MemFree:        1024000 kB
Buffers:         512000 kB
Cached:         2560000 kB
"""
    parsed_fallback = rg._parse_linux_meminfo(meminfo_legacy)
    assert parsed_fallback["status"] == "ok"
    assert parsed_fallback["source"] == "proc.meminfo.fallback"
    assert parsed_fallback["total_ram_mb"] == 8000
    assert parsed_fallback["avail_ram_mb"] == 4000


def test_parse_darwin_vm_stat():
    vm_out = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                            200000.
Pages inactive:                           50000.
Pages speculative:                        10000.
Pages wired down:                        150000.
"""
    total_bytes = 16 * 1024 * 1024 * 1024  # 16 GB
    parsed = rg._parse_darwin_vm_stat(vm_out, total_bytes)
    assert parsed["status"] == "ok"
    assert parsed["source"] == "darwin.vm_stat"
    assert parsed["total_ram_mb"] == 16384
    # (100000 + 50000 + 10000) * 16384 / (1024*1024) = 160000 / 64 = 2500 MB
    assert parsed["avail_ram_mb"] == 2500


def test_hard_safety_floors_enforce_zero_admissions():
    """Never admit a Minimum-1 worker when RAM or disk is below safety floor."""
    healthy_cpu = {"status": "ok", "cpu_count": 8, "cpu_busy_pct": 20.0, "normalized_load_ratio": 0.2}
    empty_leases = {"active_leases": 0, "pending_reserved_ram_mb": 0}

    # 1. Disk below floor -> 0 slots even with 0 active leases
    low_disk = {"status": "ok", "effective_free_gb": 1.2}
    good_mem = {"status": "ok", "total_ram_mb": 32768, "avail_ram_mb": 16384}
    dec_disk = rg.evaluate_admission(
        memory=good_mem,
        cpu=healthy_cpu,
        disk=low_disk,
        lease_summary=empty_leases,
        min_free_disk_gb=2.0,
    )
    assert dec_disk["admitted"] is False
    assert dec_disk["floor_blocked"] == "disk_floor"
    assert dec_disk["additional_slots"] == 0
    assert dec_disk["recommended_total_slots"] == 0

    # 2. RAM below floor -> 0 slots even with 0 active leases
    low_mem = {"status": "ok", "total_ram_mb": 16384, "avail_ram_mb": 3200}
    good_disk = {"status": "ok", "effective_free_gb": 50.0}
    dec_ram = rg.evaluate_admission(
        memory=low_mem,
        cpu=healthy_cpu,
        disk=good_disk,
        lease_summary=empty_leases,
        min_free_ram_mb=3072,
        worker_ram_estimate_mb=512,
    )
    # 3200 - 512 = 2688 < 3072 -> blocked!
    assert dec_ram["admitted"] is False
    assert dec_ram["floor_blocked"] == "ram_floor"
    assert dec_ram["additional_slots"] == 0
    assert dec_ram["recommended_total_slots"] == 0


def test_lease_reservation_release_and_dead_pid_reaping(tmp_path):
    job_root = str(tmp_path / "jobs")

    def fake_probe():
        return (
            {"status": "ok", "total_ram_mb": 16384, "avail_ram_mb": 4200},
            {"status": "ok", "cpu_count": 8, "cpu_busy_pct": 25.0, "normalized_load_ratio": 0.25},
            {"status": "ok", "effective_free_gb": 20.0},
        )

    gov = rg.ResourceGovernor(
        cwd=str(tmp_path),
        job_root=job_root,
        min_free_ram_mb=3072,
        worker_ram_estimate_mb=512,
        probe_fn=fake_probe,
    )
    # Available above floor: 4200 - 3072 = 1128 MB -> 2 workers of 512 MB
    l1, d1 = gov.try_acquire_lease(backend="codex", job_id="j1")
    assert l1 is not None
    assert d1["admitted"] is True

    l2, d2 = gov.try_acquire_lease(backend="codex", job_id="j2")
    assert l2 is not None
    assert d2["admitted"] is True

    # 3rd worker would subtract 2 * 512 = 1024 pending -> 3176 avail; 3176 - 512 = 2664 < 3072 -> denied!
    l3, d3 = gov.try_acquire_lease(backend="codex", job_id="j3")
    assert l3 is None
    assert d3["admitted"] is False
    assert d3["floor_blocked"] == "ram_floor"

    # Release l1 -> now j3 can acquire
    assert gov.release_lease(l1) is True
    l3_retry, d3_retry = gov.try_acquire_lease(backend="codex", job_id="j3")
    assert l3_retry is not None
    assert d3_retry["admitted"] is True

    # Inject a dead-PID lease and verify reconcile_leases reaps it
    reg_path, lock_path, key_file = rg._registry_paths(job_root)
    with rg._registry_lock(lock_path):
        secret = rg._load_or_create_secret(key_file)
        reg = rg._read_registry_unlocked(reg_path, secret)
        dead_id = "f" * 32
        reg["leases"][dead_id] = {
            "lease_id": dead_id,
            "owner_pid": 99999999,
            "owner_birth": "dead:token",
            "backend": "claude",
            "state": "reserved",
            "reserved_ram_mb": 512,
            "created_at": time.time(),
            "updated_at": time.time(),
            "expires_at": time.time() + 3600,
        }
        rg._write_registry_unlocked(reg_path, reg, secret)

    summary = rg.reconcile_leases(job_root)
    assert summary["active_leases"] == 2  # l2 and l3_retry remain; dead_id reaped

    gov.release_lease(l2)
    gov.release_lease(l3_retry)


def test_resource_exhausted_error_on_timeout(tmp_path):
    job_root = str(tmp_path / "jobs")

    def exhausted_probe():
        return (
            {"status": "ok", "total_ram_mb": 8192, "avail_ram_mb": 1024},
            {"status": "ok", "cpu_count": 4, "cpu_busy_pct": 10.0, "normalized_load_ratio": 0.1},
            {"status": "ok", "effective_free_gb": 10.0},
        )

    gov = rg.ResourceGovernor(
        cwd=str(tmp_path),
        job_root=job_root,
        min_free_ram_mb=3072,
        worker_ram_estimate_mb=512,
        probe_fn=exhausted_probe,
    )
    with pytest.raises(rg.ResourceExhaustedError) as exc_info:
        with gov.acquire_slot(backend="claude", job_id="job-oom", wait_timeout_sec=0.1, poll_interval_sec=0.02):
            pass
    assert exc_info.value.error_kind == "host_resources_exhausted"
    assert exc_info.value.snapshot["floor_blocked"] == "ram_floor"


def test_doctor_and_manifest_concurrency_auto_integration(tmp_path):
    rep = _doctor.doctor(cwd=str(tmp_path), probe=False)
    assert "resource_governor" in rep
    rg_block = rep["resource_governor"]
    assert rg_block["schema"] == "summon.resource-governor/v1"
    rendered = _doctor.render(rep)
    assert "resources:" in rendered

    caps = _manifest._parse_concurrency("auto")
    assert caps["_auto"] is True
    assert caps["default"] >= 1

    caps_mixed = _manifest._parse_concurrency("codex=auto,claude=2")
    assert caps_mixed["_auto"] is True
    assert caps_mixed["codex"] >= 1
    assert caps_mixed["claude"] == 2


def test_linux_cpu_busy_pct_two_sample_calculation():
    # total delta = 1000, idle delta = 250 -> busy = 75.0%
    assert rg.compute_linux_cpu_busy_pct((200, 1000), (450, 2000)) == 75.0
    assert rg.compute_linux_cpu_busy_pct((0, 1000), (1000, 2000)) == 0.0
    assert rg.compute_linux_cpu_busy_pct((500, 1000), (400, 2000)) is None


def test_tampered_registry_or_secret_fails_closed(tmp_path):
    job_root = str(tmp_path / "jobs")
    gov = rg.ResourceGovernor(cwd=str(tmp_path), job_root=job_root)
    l1, d1 = gov.try_acquire_lease(backend="codex", job_id="j1")
    assert l1 is not None
    assert d1["pressure"] in {"healthy", "elevated", "critical"}

    reg_path, _, key_file = rg._registry_paths(job_root)
    # Tamper with registry JSON mac -> fails closed with ResourceGovernorError
    with open(reg_path, "r", encoding="utf-8") as fh:
        import json
        bad_reg = json.load(fh)
    bad_reg["mac"] = "0" * 64
    with open(reg_path, "w", encoding="utf-8") as fh:
        json.dump(bad_reg, fh)

    with pytest.raises(rg.ResourceGovernorError, match="HMAC verification"):
        gov.try_acquire_lease(backend="codex", job_id="j2")


def test_step_cap_and_long_lived_owner_lease_survival(tmp_path):
    healthy_cpu = {"status": "ok", "cpu_count": 16, "cpu_busy_pct": 10.0, "normalized_load_ratio": 0.1}
    good_mem = {"status": "ok", "total_ram_mb": 65536, "avail_ram_mb": 32768}
    good_disk = {"status": "ok", "effective_free_gb": 100.0}
    dec = rg.evaluate_admission(
        memory=good_mem,
        cpu=healthy_cpu,
        disk=good_disk,
        lease_summary={"active_leases": 8, "pending_reserved_ram_mb": 0},
        max_auto_workers=8,
    )
    assert dec["admitted"] is False
    assert dec["floor_blocked"] == "max_workers_cap"
    assert dec["pressure"] == "max_workers_cap"

    # Lease owned by current live process with matching birth token stays live past initial TTL
    now = time.time()
    rec = {
        "owner_pid": os.getpid(),
        "owner_birth": rg.process_birth_token(os.getpid()),
        "created_at": now - 1200,
        "expires_at": now - 10,
    }
    assert rg._is_lease_live(rec, now) is True
