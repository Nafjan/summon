"""Independent fake-only acceptance for governed launch observation."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from _builder import AgentInvocation
from _executor import ProviderLaunchControl, _subprocess_launch_evidence, execute_agent
from _launch_binding import measure_executable, measure_launch_material
from _launch_observer import (MAX_VERSION_OUTPUT, LaunchObservationError,
                              _bounded_output, _parse_version, _run_version,
                              _version_environment)


@pytest.mark.parametrize("suffix", ["-beta", "+build.4"], ids=['p001_case_001', 'p001_case_002'])
def test_vendor_suffix_identity_is_preserved_across_supported_formats(suffix):
    leading = _parse_version("claude 1.2.3" + suffix)
    trailing = _parse_version("1.2.3" + suffix + " (Claude Code)")
    assert leading == trailing
    assert trailing != _parse_version("1.2.3 (Claude Code)")


def test_replacement_after_version_producer_cannot_receive_old_version(tmp_path, monkeypatch):
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(b"synthetic original executable; never executed")
    original = measure_executable(str(executable))
    material = measure_launch_material(str(executable), [], [])

    def replacement(*_args):
        executable.write_bytes(b"synthetic replacement executable; never executed")
        return {
            "external_cli_version": "claude/1.2.3",
            "material_paths": [],
            "executable": original,
            "launch_material_sha256": material,
        }

    monkeypatch.setattr("_launch_observer.observe_claude_launch", replacement)
    with pytest.raises(LaunchObservationError) as caught:
        _subprocess_launch_evidence(
            str(executable), [], str(tmp_path), {}, backend="claude",
            include_launch_observation=True, require_trusted_observation=True,
        )
    assert caught.value.kind == "launch_observation_changed"


@pytest.mark.skipif(os.name != "nt", reason="Windows fake batch launch boundary")
@pytest.mark.parametrize("fence", ["cancelled", "deadline"], ids=['p002_case_001', 'p002_case_002'])
def test_invalidated_after_durable_callback_cannot_launch_main_child(tmp_path, fence):
    # This script has no provider/network behavior. Only its main branch writes
    # the contact marker; version observation cannot create it.
    script = tmp_path / "fake-claude.cmd"
    contact = tmp_path / "main-contacted"
    script.write_text(
        '@echo off\n'
        'REM SUMMON_CLAUDE_ENTRYPOINT_V1\n'
        'if "%1"=="--version" (echo claude 9.9.9&exit /b 0)\n'
        'echo main>"%~dp0main-contacted"\n'
        'echo {"type":"result","result":"fixture completed","session_id":"fixture"}\n',
        encoding="utf-8",
    )
    invalidated = False
    gate_calls = 0
    spawned = []

    def after_durable_callback(_evidence):
        nonlocal invalidated, gate_calls
        gate_calls += 1
        invalidated = True

    invocation = AgentInvocation(
        cli="claude", prompt="synthetic boundary acceptance", cwd=str(tmp_path),
        permission="read-only", profile_command=os.environ.get("COMSPEC", "cmd.exe"),
        extra_args=("/d", "/s", "/c", str(script)), transport="subprocess",
    )
    result = execute_agent(
        invocation, timeout_ms=5000,
        launch_control=ProviderLaunchControl(
            before_launch=after_durable_callback,
            on_spawn=lambda _handle: spawned.append(True),
            cancelled=lambda: invalidated and fence == "cancelled",
            deadline_reached=lambda: invalidated and fence == "deadline",
            requires_launch_observation=True,
        ),
    )
    assert gate_calls == 1
    assert not spawned, "invalidated launch authority crossed the main Popen boundary"
    assert not contact.exists(), "invalidated launch authority reached fake main child"
    assert result.get("provider_contacted") is False
    assert result.get("attempt_status") == "not_run"


def test_version_probe_environment_isolated_from_provider_credentials(tmp_path):
    env = _version_environment(
        {
            "ANTHROPIC_API_KEY": "secret",
            "AWS_PROFILE": "work",
            "PATH": os.environ.get("PATH", ""),
            "KEEP_ME": "yes",
        },
        str(tmp_path),
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_PROFILE" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["KEEP_ME"] == "yes"
    assert env["SUMMON_VERSION_PROBE"] == "1"


def test_bounded_version_output_marks_overflow_without_unbounded_buffer():
    class Stream:
        def __init__(self):
            self.chunks = [b"x" * MAX_VERSION_OUTPUT, b"overflow"]

        def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

    class Process:
        stdout = Stream()

    output, overflow = _bounded_output(Process())
    assert len(output) == MAX_VERSION_OUTPUT
    assert overflow is True


def test_parent_exit_still_closes_owned_job_handle(monkeypatch, tmp_path):
    class Stream:
        def __init__(self):
            self.done = False

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return b"claude 1.2.3\n"

    class Process:
        pid = 4242
        stdout = Stream()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    process = Process()
    calls = []
    monkeypatch.setattr("_launch_observer.subprocess.Popen", lambda *a, **k: process)
    monkeypatch.setattr("_jobobj.attach", lambda value: calls.append(("attach", value)) or True)
    monkeypatch.setattr("_jobobj.close", lambda value: calls.append(("close", value)) or True)
    version = _run_version(
        {"version_argv": ["fake", "--version"]}, str(tmp_path),
        {"ANTHROPIC_API_KEY": "secret"},
    )
    assert version == "claude/1.2.3"
    assert [kind for kind, _ in calls] == ["attach", "close"]


def test_job_termination_precedes_taskkill_fallback(monkeypatch):
    class Process:
        pid = 5151

    monkeypatch.setattr("_jobobj.terminate", lambda _value: True)
    monkeypatch.setattr(
        "_launch_observer.subprocess.run",
        lambda *args, **kwargs: pytest.fail("taskkill fallback should not run"),
    )
    from _launch_observer import _terminate_owned
    _terminate_owned(Process())


def test_real_version_probe_terminates_stalled_local_process(monkeypatch, tmp_path):
    import _jobobj

    pid_file = tmp_path / "stalled-pid"
    code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    attached = []
    terminated = []
    real_attach = _jobobj.attach
    real_terminate = _jobobj.terminate

    def attach(process):
        result = real_attach(process)
        attached.append((process, result))
        return result

    def terminate(process):
        terminated.append(process)
        return real_terminate(process)

    monkeypatch.setattr(_jobobj, "attach", attach)
    monkeypatch.setattr(_jobobj, "terminate", terminate)
    monkeypatch.setattr("_launch_observer.VERSION_TIMEOUT_SECONDS", 0.25)
    with pytest.raises(LaunchObservationError) as caught:
        _run_version(
            {"version_argv": [sys.executable, "-c", code]},
            str(tmp_path), {},
        )
    assert caught.value.kind == "launch_version_timeout"
    assert attached and attached[0][0].wait(timeout=2) is not None
    assert terminated, "the owned process was not passed through termination"


def test_real_version_probe_rejects_oversized_output(monkeypatch, tmp_path):
    with pytest.raises(LaunchObservationError) as caught:
        _run_version(
            {"version_argv": [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"]},
            str(tmp_path), {},
        )
    assert caught.value.kind == "launch_version_output_oversized"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Object descendant cleanup")
def test_parent_exit_descendant_cannot_retain_version_pipe(monkeypatch, tmp_path):
    """A child that survives its leader must be killed before the probe returns."""
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    marker = tmp_path / "leaked"
    started = tmp_path / "child-started"
    child_code = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        "time.sleep(4.5); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked'); "
        "time.sleep(30)"
    )
    child_hidden = (
        "startupinfo = subprocess.STARTUPINFO(); "
        "startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW; "
        "startupinfo.wShowWindow = subprocess.SW_HIDE; "
        "child = subprocess.Popen([sys.executable, '-c', "
        f"{child_code!r}], creationflags=subprocess.CREATE_NO_WINDOW, "
        "startupinfo=startupinfo, close_fds=False)\n"
        f"while not pathlib.Path({str(started)!r}).exists():\n"
        "    time.sleep(0.01)"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        f"while not pathlib.Path({str(go)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        f"{child_hidden}\n"
    )
    import _jobobj
    attached = []
    terminated = []
    attached_event = threading.Event()
    real_attach = _jobobj.attach
    real_terminate = _jobobj.terminate

    def attach(process):
        result = real_attach(process)
        attached.append((process, result))
        attached_event.set()
        return result

    def terminate(process):
        terminated.append(process)
        return real_terminate(process)

    monkeypatch.setattr(_jobobj, "attach", attach)
    monkeypatch.setattr(_jobobj, "terminate", terminate)
    monkeypatch.setattr("_launch_observer.VERSION_TIMEOUT_SECONDS", 3.0)

    def release_parent():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
                not ready.exists() or not attached_event.is_set()):
            time.sleep(0.01)
        go.write_text("go", encoding="utf-8")

    releaser = threading.Thread(target=release_parent, daemon=True)
    releaser.start()
    with pytest.raises(LaunchObservationError) as caught:
        _run_version(
            {"version_argv": [sys.executable, "-c", parent_code]},
            str(tmp_path), {},
        )
    releaser.join(2)
    time.sleep(5.0)
    assert caught.value.kind == "launch_version_timeout"
    assert attached and attached[0][1] is True
    assert terminated, "the owned parent was not passed through termination"
    assert started.exists(), "the descendant did not reach its positive-start marker"
    assert not marker.exists(), "descendant escaped version-probe cleanup"
