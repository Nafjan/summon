
"""Actual detached dispatcher/immutable-bundle publication with a local fake CLI.

No executor, emitter, profile admission or provider result is monkeypatched.
The restricted child PATH contains only our marked Claude-shaped cmd fixture.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import _job_continuation as continuation
import _jobs
from _launch_observer import ENTRYPOINT_MARKER
from _receipt import scripts_sha256
from _spawn import run_flags


pytestmark = pytest.mark.skipif(os.name != "nt", reason="marked PATH shim requires Windows")
ENTRY = Path(__file__).with_name("run_subagent.py")


class _OwnedDispatcher:
    """Hold the returned process object while waiting/cleaning its owned tree."""
    def __init__(self, pid, env):
        self.pid, self.env = pid, env
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            assert ctypes.get_last_error() == 87, "cannot observe owned detached dispatcher"

    def wait(self, milliseconds):
        return not self.handle or self.kernel.WaitForSingleObject(self.handle, milliseconds) == 0

    def close(self):
        if self.handle:
            try:
                if not self.wait(0):
                    # The retained handle prevents PID recycling during teardown.
                    taskkill = str(Path(self.env["SystemRoot"]) / "System32" / "taskkill.exe")
                    subprocess.run([taskkill, "/F", "/T", "/PID", str(self.pid)],
                                   env=self.env, capture_output=True, timeout=10, **run_flags())
                    assert self.wait(5000), "owned detached dispatcher did not stop"
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def _fixture(tmp_path, served_model):
    home, workspace, profile, roster, binaries = (tmp_path / name for name in (
        "home", "workspace", "profile", "roster", "bin"))
    for directory in (home, workspace, profile, roster, binaries):
        directory.mkdir()
    shim = binaries / "claude.cmd"
    shim.write_text(
        "@echo off\n" + f"REM {ENTRYPOINT_MARKER}\n"
        'if "%1"=="--version" (echo claude 9.9.9&exit /b 0)\n'
        'echo invoked>> "%~dp0invocations.txt"\n'
        'echo {"type":"result","result":"STATUS: COMPLETE VERDICT: PASS HANDOFF: done",'
        '"model":' + json.dumps(served_model) + ',"session_id":"synthetic-background-session"}\n',
        encoding="utf-8")
    (roster / "fixture-seat.md").write_text(
        "---\nrun-agent: claude\npermission: read-only\nmodel: claude-opus-5\n"
        "model-policy: exact\nprofile: fixture-profile\n---\nLocal deterministic fixture.\n",
        encoding="utf-8")
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"profiles": {"fixture-profile": {
        "cli": "claude", "config_dir": str(profile), "models": ["claude-opus-5"],
    }}}), encoding="utf-8")
    # No inherited provider/auth routes or installed-provider PATH entries.
    system_root = os.environ["SystemRoot"]
    env = {
        "SystemRoot": system_root, "WINDIR": system_root,
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "PATH": str(binaries), "PATHEXT": ".CMD", "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "SUMMON_PROFILES_FILE": str(registry), "SUMMON_TELEMETRY": "0",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
    }
    assert Path(shutil.which("claude.cmd", path=env["PATH"])).resolve() == shim.resolve()
    prompt = "Return the deterministic local fixture."
    jobs = tmp_path / "jobs"
    argv = [sys.executable, "-B", str(ENTRY), "dispatch", "--background",
            "--agent", "fixture-seat", "--prompt", prompt, "--cwd", str(workspace),
            "--agents-dir", str(roster), "--strict-agents-dir", "--no-contract-repair",
            "--hard-timeout", "--timeout", "5s", "--job-dir", str(jobs)]
    return env, argv, jobs, shim, prompt, workspace


def _no_private_marker(value, observation=None):
    text = value if isinstance(value, str) else json.dumps(value)
    if "_private_launch_observation" in text:
        pytest.fail("public background output contains private launch marker", pytrace=False)
    if observation:
        for key in ("executable_sha256", "launch_material_sha256"):
            if observation[key] in text:
                pytest.fail("public background output contains private launch digest", pytrace=False)


@pytest.mark.parametrize("served_model", ["claude-opus-5", "claude-sonnet-4-6"], ids=['p001_case_001', 'p001_case_002'])
def test_actual_background_bundle_preserves_source_and_publication_boundaries(tmp_path, served_model):
    env, argv, jobs, shim, prompt, workspace = _fixture(tmp_path, served_model)
    parent = subprocess.run(argv, cwd=workspace, env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=45, **run_flags())
    assert parent.returncode == 0, "public background parent refused synthetic fixture"
    handle = json.loads(parent.stdout)
    assert handle["status"] == "background"
    job_id = handle["job_id"]
    assert Path(handle["job_dir"]).resolve() == jobs.resolve()
    owned = _OwnedDispatcher(handle["pid"], env)
    try:
        assert owned.wait(45000), "detached fixture did not reach terminal exit"
        record = _jobs.read_json(_jobs.record_path(str(jobs), job_id))
        terminal = _jobs.read_json(_jobs.result_path(str(jobs), job_id))
        assert terminal is not None, "detached child exited without a terminal receipt"
        assert record["pid"] == handle["pid"]
        assert terminal["attempt_id"] == record["attempt_id"] == job_id
        assert terminal["job_nonce"] == record["nonce"]
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        assert terminal["prompt_sha256"] == record["prompt_sha256"] == prompt_sha
        bundle_entry = Path(record["summon"]["script"])
        assert bundle_entry != ENTRY and bundle_entry.is_relative_to(jobs)
        assert (bundle_entry.parent.parent / "dispatch-prompt.txt").read_text(encoding="utf-8") == prompt
        digest = scripts_sha256(str(bundle_entry.parent))
        assert terminal["summon"]["scripts_sha256"] == record["summon"]["scripts_sha256"] == digest
        assert terminal["summon"]["background_bundle"]["kind"] == "immutable_per_job_snapshot"
        assert terminal["attempts"] == 1 and terminal["provider_contacted"] is True
        assert (shim.parent / "invocations.txt").read_text(encoding="utf-8").splitlines() == ["invoked"]
        assert terminal["profile"]["command_sha256"] is None
        assert terminal["profile"]["name"] == "fixture-profile"
        assert not Path(handle["result_file"] + ".terminal.lock").exists()
        assert not list(jobs.glob(job_id + ".*.tmp"))
        assert _jobs._pid_liveness(handle["pid"]) == "dead"
        public_status = _jobs.job_status(str(jobs), job_id)
        assert public_status["trusted"] is True
        if served_model == "claude-opus-5":
            assert terminal["status"] == "success" and terminal["named_model_verified"] is True
            assert terminal["continuation"]["available"] is True
            source = continuation.read_private_source(str(jobs), job_id)
            observation = continuation.read_launch_binding(str(jobs), job_id)
            assert source["request_sha256"] == terminal["request_sha256"]
            assert source["attempt_id"] == job_id
            assert source["scripts_sha256"] == digest
            assert source["continuation"]["handle"] == terminal["resume"]["session_id"]
            assert source["backend"]["backend_type"] == "cli"
            assert observation["executable_sha256"] == hashlib.sha256(shim.read_bytes()).hexdigest()
            assert observation["external_cli_version"] is None
        else:
            assert terminal["status"] == "blocked" and terminal["named_model_verified"] is False
            assert terminal["continuation"]["available"] is False
            assert not Path(continuation.continuation_path(str(jobs), job_id)).exists()
            assert not Path(continuation.launch_binding_path(str(jobs), job_id)).exists()
            observation = None
        for public in (parent.stdout, parent.stderr, terminal, public_status):
            _no_private_marker(public, observation)
    finally:
        owned.close()


def test_background_out_is_refused_before_detaching_or_writing_output(tmp_path):
    env, argv, jobs, shim, _prompt, workspace = _fixture(tmp_path, "claude-opus-5")
    output = tmp_path / "public-out.json"
    parent = subprocess.run(argv + ["--out", str(output)], cwd=workspace, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30,
                            **run_flags())
    assert parent.returncode != 0
    assert not jobs.exists() and not output.exists()
    assert not (shim.parent / "invocations.txt").exists()
    _no_private_marker(parent.stdout)
    _no_private_marker(parent.stderr)
