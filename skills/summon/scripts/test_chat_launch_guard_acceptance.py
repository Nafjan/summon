"""Independent provider-free acceptance of the private chat launch guard.

These fixtures exercise the real file/owner boundary with synthetic observations.
They do not qualify observation production, a provider adapter, or a full resume.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys
import threading
import time

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_launch_guard as guard
import _context_policy
import _rundir as rundir
from _launch_binding import SCHEMA, registry_scope


def _evidence(stamp: int | None = None) -> dict:
    observation = {
        "schema": SCHEMA, "backend": "claude", "transport": "subprocess",
        **registry_scope("claude", "subprocess"),
    }
    for key in (
        "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256",
        "env_sha256", "executable_path_sha256", "executable_sha256",
        "launch_material_sha256",
    ):
        observation[key] = "a" * 64
    observation.update(
        executable_size=1, executable_mtime_ns=1,
        executable_content_revision="sha256:" + "a" * 64,
        external_cli_version="claude-test/1.0", observation_nonce="b" * 32,
        observed_at_ns=time.time_ns() if stamp is None else stamp,
    )
    return {"launch_observation": observation}


def _consume(path: str, evidence: dict, attempt: str = "attempt-a") -> str:
    try:
        guard.before_launch(
            path, "synthetic-test-token", evidence, backend="claude",
            transport="subprocess", attempt_id=attempt,
        )
        return "accepted"
    except (ValueError, FileExistsError):
        return "refused"


@pytest.mark.parametrize("case", [
    "fresh", "stale", "future", "wrong_attempt", "missing_owner",
    "replaced_owner", "expired_owner", "duplicate", "thread_contention",
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005', 'p001_case_006', 'p001_case_007', 'p001_case_008', 'p001_case_009'])
def test_guard_consumption_and_current_owner(tmp_path: Path, case: str):
    owner = rundir.acquire_owner(str(tmp_path), 0 if case == "expired_owner" else 120)
    path = str(tmp_path / "guard.json")
    try:
        guard.create(
            path, "synthetic-test-token", expected=None, backend="claude",
            transport="subprocess", turn_id="turn-a",
            owner_generation=owner.generation, owner_nonce=owner.nonce,
            attempt_id="attempt-a",
        )
        if case in {"missing_owner", "replaced_owner"}:
            rundir.release_owner(owner)
            if case == "replaced_owner":
                owner = rundir.acquire_owner(str(tmp_path), 120)
        stamp = (1 if case == "stale" else
                 time.time_ns() + 10**12 if case == "future" else None)
        evidence = _evidence(stamp)
        if case == "thread_contention":
            barrier = threading.Barrier(2, timeout=5)

            def competitor(_):
                barrier.wait()
                return _consume(path, evidence)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = sorted(pool.map(competitor, range(2)))
            assert results == ["accepted", "refused"]
        else:
            expected = "accepted" if case in {"fresh", "duplicate"} else "refused"
            attempt = "different-attempt" if case == "wrong_attempt" else "attempt-a"
            assert _consume(path, evidence, attempt) == expected
            if case == "duplicate":
                assert _consume(path, evidence) == "refused"
            if expected == "refused":
                assert guard._read(path, "synthetic-test-token")["phase"] == "pending"
    finally:
        rundir.release_owner(owner)


def test_v2_guard_binds_turn_policy_and_payload_before_handoff(tmp_path: Path):
    owner = rundir.acquire_owner(str(tmp_path), 120)
    try:
        path = str(tmp_path / "guard-v2.json")
        policy = _context_policy.make("off", policy_id="chat-context")
        guard.create_v2(
            path, "synthetic-test-token", expected=None,
            session_id="room", participant="worker", policy=policy,
            payload_sha256="c" * 64, context_selection_sha256="d" * 64,
            backend="claude", transport="subprocess", turn_id="turn-v2",
            owner_generation=owner.generation, owner_nonce=owner.nonce,
            attempt_id="attempt-v2")
        guard.before_launch_v2(
            path, "synthetic-test-token",
            {**_evidence(),
             "dispatch_payload_sha256": "c" * 64,
             "attempt_id_sha256": hashlib.sha256(b"attempt-v2").hexdigest()}, backend="claude",
            transport="subprocess", attempt_id="attempt-v2")
        receipt = guard.read_v2(path, "synthetic-test-token")
        assert receipt is not None
        assert receipt["session_id"] == "room"
        assert receipt["participant"] == "worker"
        assert receipt["payload_sha256"] == "c" * 64
        assert receipt["policy"]["revision"] == 1
    finally:
        rundir.release_owner(owner)


def test_v2_guard_refuses_changed_payload_or_physical_attempt(tmp_path: Path):
    owner = rundir.acquire_owner(str(tmp_path), 120)
    try:
        policy = _context_policy.make("off", policy_id="chat-context")
        good = {**_evidence(), "dispatch_payload_sha256": "c" * 64,
                "attempt_id_sha256": hashlib.sha256(b"attempt-v2").hexdigest()}
        for suffix, evidence, attempt in (
                ("payload", {**good, "dispatch_payload_sha256": "e" * 64}, "attempt-v2"),
                ("attempt", good, "different-attempt")):
            path = str(tmp_path / f"guard-{suffix}.json")
            guard.create_v2(
                path, "synthetic-test-token", expected=None,
                session_id="room", participant="worker", policy=policy,
                payload_sha256="c" * 64, context_selection_sha256="d" * 64,
                backend="claude", transport="subprocess", turn_id="turn-v2",
                owner_generation=owner.generation, owner_nonce=owner.nonce,
                attempt_id="attempt-v2")
            with pytest.raises(ValueError):
                guard.before_launch_v2(
                    path, "synthetic-test-token", evidence, backend="claude",
                    transport="subprocess", attempt_id=attempt)
    finally:
        rundir.release_owner(owner)
