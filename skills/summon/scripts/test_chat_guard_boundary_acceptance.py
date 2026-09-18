"""Independent synthetic checks at chat's durable authorization boundary."""
from pathlib import Path
import sys
import threading

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_launch_guard as guard
import _chat_source_family as family
import _rundir as rundir
from test_chat_source_family_acceptance import _packet


def test_revocation_cannot_complete_between_check_and_durable_consumption(tmp_path, monkeypatch):
    family_path, original, observation, issue = _packet(tmp_path / "family")
    qualified = issue("source-turn")
    owner = rundir.acquire_owner(str(tmp_path), 120)
    guard_path = str(tmp_path / "guard.json")
    token = "synthetic-guard-token"
    started = threading.Event()
    finished = threading.Event()
    errors = []
    worker = None
    try:
        guard.create(
            guard_path, token, expected=None, backend="claude", transport="subprocess",
            turn_id="next-turn", owner_generation=owner.generation,
            owner_nonce=owner.nonce, attempt_id="next-attempt",
            qualification=qualified, qualification_required=True,
            source_family_id=original["source_family_id"],
            qualification_source_turn_id="source-turn",
            qualification_token=original["nonce"], source_family_path=family_path,
        )
        original_write = guard._write

        def revoke():
            started.set()
            try:
                family.revoke(family_path, original, qualified["revocation_id"])
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                finished.set()

        def intercept_commit(path, value):
            nonlocal worker
            if value.get("phase") == "observed":
                worker = threading.Thread(target=revoke, daemon=True)
                worker.start()
                assert started.wait(2), "revocation contender did not start"
                assert not finished.wait(0.3), (
                    "revocation completed while authorization was still durably pending"
                )
            return original_write(path, value)

        monkeypatch.setattr(guard, "_write", intercept_commit)
        guard.before_launch(
            guard_path, token,
            {"launch_observation": observation, "launch_qualification": qualified},
            backend="claude", transport="subprocess", attempt_id="next-attempt",
            qualification_token=original["nonce"], source_family_path=family_path,
        )
        assert finished.wait(3), "revocation did not proceed after durable consumption"
        assert not errors
        assert guard.read(guard_path, token) is not None
        assert family.is_revoked(family_path, original, qualified["revocation_id"])
    finally:
        if worker is not None:
            worker.join(3)
        rundir.release_owner(owner)


@pytest.mark.parametrize("override", [None, {"SYNTHETIC_PROVIDER_SETTING": "kept"}], ids=['p001_case_001', 'p001_case_002'])
def test_executor_environment_removes_chat_qualification_authority(monkeypatch, override):
    from _executor import _merge_env
    keys = (
        "SUMMON_CHAT_LAUNCH_GUARD_PATH", "SUMMON_CHAT_LAUNCH_GUARD_TOKEN",
        "SUMMON_CHAT_LAUNCH_ATTEMPT_ID", "SUMMON_CHAT_LAUNCH_BACKEND",
        "SUMMON_CHAT_LAUNCH_TRANSPORT", "SUMMON_CHAT_LAUNCH_QUALIFICATION",
        "SUMMON_CHAT_QUALIFICATION_TOKEN", "SUMMON_CHAT_SOURCE_FAMILY_ID",
        "SUMMON_CHAT_SOURCE_FAMILY_PATH", "SUMMON_CHAT_QUALIFICATION_SOURCE_TURN_ID",
        "SUMMON_CHAT_MIGRATION_KEY",
    )
    for key in keys:
        monkeypatch.setenv(key, "synthetic-private-capability")
    monkeypatch.setenv("SYNTHETIC_PROVIDER_SETTING", "kept")
    merged = _merge_env(override)
    assert all(key not in merged for key in keys)
    assert merged["SYNTHETIC_PROVIDER_SETTING"] == "kept"
