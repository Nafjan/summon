#!/usr/bin/env python3
"""Installer safety regression tests. Every case here was reproduced as a real
defect during adversarial review — do not remove without replacing coverage.

Run: python tests/test_install.py   (plain asserts, exits non-zero on failure).
Each test runs install.py as a SUBPROCESS inside an isolated fake HOME, so the
real user profile is never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _run(home: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": home, "USERPROFILE": home}
    return subprocess.run([sys.executable, os.path.join(REPO, "install.py"), *args],
                          capture_output=True, text=True, env=env, cwd=REPO)


def _fake_home():
    home = tempfile.mkdtemp(prefix="summon-test-home-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    return home


def _dest(home: str) -> str:
    return os.path.join(home, ".claude", "skills", "summon")


def test_unowned_dir_survives_uninstall_and_reinstall():
    home = _fake_home()
    try:
        unowned = _dest(home)
        os.makedirs(unowned)
        open(os.path.join(unowned, "USER_FILE"), "w").write("precious")
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isfile(os.path.join(unowned, "USER_FILE"))
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isfile(os.path.join(unowned, "USER_FILE"))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_corrupt_manifest_fails_closed():
    # A garbage .summon-install.json must NOT authorize deletion or replacement.
    home = _fake_home()
    try:
        d = _dest(home)
        os.makedirs(d)
        open(os.path.join(d, ".summon-install.json"), "w").write("not even json")
        open(os.path.join(d, "USER_FILE"), "w").write("precious")
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isfile(os.path.join(d, "USER_FILE"))
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isfile(os.path.join(d, "USER_FILE"))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_foreign_manifest_fails_closed():
    home = _fake_home()
    try:
        d = _dest(home)
        os.makedirs(d)
        with open(os.path.join(d, ".summon-install.json"), "w") as fh:
            json.dump({"installed_by": "someone-else"}, fh)
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isdir(d)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_unowned_sibling_artifacts_survive_install():
    # User dirs that merely LOOK like our staging/backup names must survive.
    home = _fake_home()
    try:
        parent = os.path.join(home, ".claude", "skills")
        os.makedirs(parent)
        for name in ("summon.staging-user", "summon.previous"):
            os.makedirs(os.path.join(parent, name))
            open(os.path.join(parent, name, "USER_FILE"), "w").write("precious")
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0, (r.returncode, r.stdout + r.stderr)
        for name in ("summon.staging-user", "summon.previous"):
            assert os.path.isfile(os.path.join(parent, name, "USER_FILE")), name
        assert os.path.isfile(os.path.join(_dest(home), "SKILL.md"))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_crash_recovery_restores_owned_backup():
    # Simulate: a prior run moved the good tree to .previous and died. The next
    # run must restore it (and then refresh it), never build-from-nothing while
    # the only good copy sits in the backup slot.
    home = _fake_home()
    try:
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0, r.stdout + r.stderr
        d = _dest(home)
        os.rename(d, d + ".previous")          # the "crash" state
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.path.isfile(os.path.join(d, "SKILL.md"))
        assert not os.path.isdir(d + ".previous")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_true_refresh_and_clean_uninstall():
    home = _fake_home()
    try:
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0
        stale = os.path.join(_dest(home), "scripts", "OBSOLETE.py")
        open(stale, "w").write("old")
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0 and not os.path.exists(stale)
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 0 and not os.path.isdir(_dest(home))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_agents_never_overwritten():
    home = _fake_home()
    try:
        agents = os.path.join(home, ".agents")
        os.makedirs(agents)
        mine = os.path.join(agents, "reviewer.md")
        open(mine, "w").write("MINE")
        r = _run(home, "--hosts", "claude")
        assert r.returncode == 0, r.stdout + r.stderr
        assert open(mine).read() == "MINE"
        # and the rest of the roster did arrive
        n = len([f for f in os.listdir(agents) if f.endswith(".md")])
        assert n >= 20, n
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_lock_blocks_concurrent_install():
    home = _fake_home()
    try:
        parent = os.path.join(home, ".claude", "skills")
        os.makedirs(parent)
        open(os.path.join(home, ".claude", "summon.install.lock"), "w").write("12345")
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 2 and "lock" in r.stdout.lower(), (r.returncode, r.stdout)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_manifest_non_object_json_fails_closed():
    # Valid JSON of the wrong shape ([]) must refuse cleanly - exit 2, no traceback.
    home = _fake_home()
    try:
        d = _dest(home)
        os.makedirs(d)
        open(os.path.join(d, ".summon-install.json"), "w").write("[]")
        open(os.path.join(d, "USER_FILE"), "w").write("precious")
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr, r.stderr
        assert os.path.isfile(os.path.join(d, "USER_FILE"))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_stale_unowned_lock_never_deleted():
    # A user file named summon.install.lock - even an OLD one - is not summon's
    # to delete. Install must refuse (exit 2) and leave it byte-identical.
    import time as _t
    home = _fake_home()
    try:
        parent = os.path.join(home, ".claude", "skills")
        os.makedirs(parent)
        lock = os.path.join(home, ".claude", "summon.install.lock")
        open(lock, "w").write("user data, not a summon marker")
        old = _t.time() - 4000  # > LOCK_STALE_SEC (3600)
        os.utime(lock, (old, old))  # stale by the 600s policy
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert open(lock).read() == "user data, not a summon marker"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_uninstall_blocked_by_held_lock():
    # Uninstall participates in the same lock protocol as install.
    home = _fake_home()
    try:
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0, r.stdout + r.stderr
        parent = os.path.join(home, ".claude", "skills")
        with open(os.path.join(home, ".claude", "summon.install.lock"), "w") as fh:
            json.dump({"installed_by": "summon", "pid": 99999}, fh)
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout)
        assert os.path.isdir(_dest(home))  # nothing was removed
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_dry_run_is_mutation_free_even_during_crash_recovery():
    # --dry-run must not perform the crash-recovery rename it reports.
    home = _fake_home()
    try:
        r = _run(home, "--hosts", "claude", "--no-agents")
        assert r.returncode == 0
        d = _dest(home)
        os.rename(d, d + ".previous")  # crash state
        r = _run(home, "--hosts", "claude", "--no-agents", "--dry-run")
        assert r.returncode == 0, (r.returncode, r.stdout)
        assert os.path.isdir(d + ".previous") and not os.path.isdir(d)
        assert "restore" in r.stdout.lower(), r.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_lock_release_only_removes_own_token():
    sys.path.insert(0, REPO)
    import importlib
    install = importlib.import_module("install")
    d = tempfile.mkdtemp(prefix="summon-lock-")
    try:
        acq = install._acquire_lock(d)
        assert acq is not None
        lock, token = acq
        assert os.path.isfile(lock)
        install._release_lock(lock, "not-our-token")   # wrong token: must NOT remove
        assert os.path.isfile(lock), "lock removed by wrong token!"
        install._release_lock(lock, token)             # our token: removes
        assert not os.path.isfile(lock)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_uninstall_absent_host_root_is_nothing_not_contention():
    # --uninstall against a host whose root dir doesn't exist must report
    # "nothing", not a false "another install running".
    home = tempfile.mkdtemp(prefix="summon-nohost-")  # NO .claude created
    try:
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 0, (r.returncode, r.stdout)
        assert "nothing at" in r.stdout and "running" not in r.stdout, r.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_install_agents_returns_ok_tuple():
    # install_agents now returns (lines, ok); main() folds ok into the exit code
    # so a failed starter-agent copy can't exit 0. Verify the tuple contract on
    # both the no-dir and normal paths.
    sys.path.insert(0, REPO)
    import importlib
    install = importlib.import_module("install")
    lines, ok = install.install_agents(dry=True)
    assert isinstance(lines, list) and ok is True


def test_uninstall_with_absent_dest_still_respects_held_lock():
    # dest missing + a held lock = a concurrent install may be mid-flight;
    # uninstall must refuse (exit 2), not report "nothing to do".
    home = _fake_home()
    try:
        parent = os.path.join(home, ".claude", "skills")
        os.makedirs(parent)
        with open(os.path.join(home, ".claude", "summon.install.lock"), "w") as fh:
            json.dump({"installed_by": "summon", "pid": 99999}, fh)
        r = _run(home, "--hosts", "claude", "--uninstall")
        assert r.returncode == 2, (r.returncode, r.stdout)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_lock_owned_rejects_non_object_json():
    sys.path.insert(0, REPO)
    import importlib
    install = importlib.import_module("install")
    with tempfile.NamedTemporaryFile("w", suffix=".lock", delete=False) as fh:
        fh.write("[]")
        p = fh.name
    try:
        assert install._lock_owned(p) is False
    finally:
        os.unlink(p)


def test_doctor_rejects_nonzero_version_probe():
    # A CLI that errors on --version must not be verified/usable.
    import types
    import _doctor
    orig_run, orig_which = _doctor.subprocess.run, _doctor.shutil.which
    _doctor.shutil.which = lambda name: "/fake/" + name
    _doctor.subprocess.run = lambda *a, **k: types.SimpleNamespace(
        returncode=7, stdout="FATAL BROKEN INSTALL", stderr="")
    try:
        rep = _doctor.doctor()
    finally:
        _doctor.subprocess.run, _doctor.shutil.which = orig_run, orig_which
    assert rep["usable_backends"] == [], rep["usable_backends"]
    assert rep["ok"] is False
    for b in rep["backends"].values():
        assert b["found"] is True and b["verified"] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
