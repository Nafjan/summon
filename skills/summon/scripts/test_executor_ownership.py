"""Provider-inert regressions for executor process ownership on abnormal exits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _executor
from _liveness import LivenessTracker
from _deliberation_adapter import ProviderLaunchControl
from _spawn import popen_flags, scrub_provider_env
from _stream import StreamProcessor


def _sleeping_child(*, terminal: bool = False) -> subprocess.Popen:
    source = "import time; time.sleep(60)"
    if terminal:
        event = json.dumps({
            "type": "result",
            "result": "STATUS: DONE",
            "session_id": "session",
        })
        source = (
            "import time\n"
            f"print({event!r}, flush=True)\n"
            "time.sleep(60)\n"
        )
    return subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_flags(),
    )


def test_unexpected_driver_exception_kills_and_reaps_real_sleeping_child():
    process = _sleeping_child()
    marker = RuntimeError("sentinel driver failure")

    class ExplodingLiveness:
        def expired(self):
            raise marker

    started = time.monotonic()
    with pytest.raises(RuntimeError) as raised:
        _executor._drive_process_loop(
            process, "codex", 60_000, StreamProcessor(),
            liveness=ExplodingLiveness())

    assert raised.value is marker
    assert process.poll() is not None
    assert time.monotonic() - started < 10


def test_keyboard_interrupt_is_not_swallowed_and_reaps_real_sleeping_child():
    process = _sleeping_child()

    class InterruptingLiveness:
        def expired(self):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _executor._drive_process_loop(
            process, "codex", 60_000, StreamProcessor(),
            liveness=InterruptingLiveness())

    assert process.poll() is not None


def test_unexpected_exception_after_terminal_preserves_result_and_reaps_child():
    process = _sleeping_child(terminal=True)
    tracker = LivenessTracker(
        attempt_id="a" * 32, overall_ms=60_000,
        first_event_ms=60_000, idle_ms=60_000)
    processor = StreamProcessor(event_observer=tracker.emitter())

    class ExplodeAfterTerminal:
        checkpoint_ms = 1_000
        deadline = time.monotonic() + 60
        hard_deadline = time.monotonic() + 60
        cancel_requested = False

        def refresh(self):
            # Bind the fault to the parser's actual terminal state rather than
            # scheduler timing; under a loaded full suite the reader thread may
            # not publish its first line before several refresh polls.
            if processor.get_result() is not None:
                raise RuntimeError("post-terminal driver failure")

        def checkpoint(self, *, active):
            return 0

        def publish(self, _snapshot):
            return None

        def expired(self):
            return False

    response = _executor._drive_process_loop(
        process, "codex", 60_000, processor,
        liveness=tracker, liveness_emitter=None,
        runtime_control=ExplodeAfterTerminal())

    assert response["status"] == "success"
    assert "STATUS: DONE" in response["result"]
    assert response["cleanup"] == {
        "forced": True,
        "stage": "driver_exception_after_terminal",
        "reason": "backend_process_outlived_terminal_event",
    }
    assert process.poll() is not None


def test_terminate_error_after_parsed_terminal_preserves_result_and_reaps_child(
        monkeypatch):
    process = _sleeping_child(terminal=True)

    def fail_terminate():
        raise OSError("terminate failed after terminal")

    monkeypatch.setattr(process, "terminate", fail_terminate)
    response = _executor._drive_process_loop(
        process, "claude", 60_000, StreamProcessor())

    assert response["status"] == "success"
    assert response["result"] == "STATUS: DONE"
    assert response["cleanup"]["stage"] == "driver_io_exception_after_terminal"
    assert process.poll() is not None


def test_provider_env_scrubs_outer_job_control_capabilities(monkeypatch):
    monkeypatch.setenv("SUMMON_ADAPTIVE_TIMEOUT", "1")
    monkeypatch.setenv("SUMMON_MAX_RUNTIME_MS", "999999")
    monkeypatch.setenv("SUMMON_JOB_ID", "a" * 32)
    monkeypatch.setenv("SUMMON_JOB_NONCE", "b" * 32)
    monkeypatch.setenv("SUMMON_JOB_CONTROL_FILE", "outer-control.json")
    monkeypatch.setenv("SUMMON_JOB_HEARTBEAT_FILE", "outer-heartbeat.json")
    monkeypatch.setenv("SUMMON_RESUME_CLAIM_FILE", "outer-claim.json")
    monkeypatch.setenv("SUMMON_FRESH_CONSENT_ONLY", "1")
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", "approval-key-path")
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", "approval-store-path")
    monkeypatch.setenv("SUMMON_KEEP_ME", "ordinary-setting")

    merged = _executor._merge_env({
        "SUMMON_JOB_ID": "attempted-reintroduction",
        "PROVIDER_SETTING": "kept",
    })

    assert merged is not None
    assert merged["SUMMON_KEEP_ME"] == "ordinary-setting"
    assert merged["PROVIDER_SETTING"] == "kept"
    assert not any(key.startswith("SUMMON_JOB_") for key in merged)
    for key in (
        "SUMMON_ADAPTIVE_TIMEOUT", "SUMMON_MAX_RUNTIME_MS",
        "SUMMON_RESUME_CLAIM_FILE", "SUMMON_FRESH_CONSENT_ONLY",
        "SUMMON_FLEET_APPROVAL_KEY", "SUMMON_FLEET_APPROVAL_STORE",
    ):
        assert key not in merged


def test_plain_foreground_provider_env_is_always_scrubbed(monkeypatch):
    monkeypatch.delenv("SUMMON_ADAPTIVE_TIMEOUT", raising=False)
    monkeypatch.delenv("SUMMON_CMD_LAUNCHER", raising=False)
    for key in tuple(os.environ):
        if key.startswith("SUMMON_JOB_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", "private-key-path")
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", "private-store-path")
    monkeypatch.setenv("SUMMON_KEEP_ME", "ordinary-setting")

    merged = _executor._merge_env(None)

    assert merged is not None
    assert merged["SUMMON_KEEP_ME"] == "ordinary-setting"
    assert "SUMMON_FLEET_APPROVAL_KEY" not in merged
    assert "SUMMON_FLEET_APPROVAL_STORE" not in merged


def test_controlled_subprocess_snapshot_scrubs_internal_capabilities(monkeypatch):
    import _receipt

    monkeypatch.setenv("SUMMON_ADAPTIVE_TIMEOUT", "0")
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", "private-key-path")
    captured = {}

    def refused_popen(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("provider-inert fixture")

    control = ProviderLaunchControl(before_launch=lambda _evidence: None)
    invocation = _executor.AgentInvocation(
        cli="claude", prompt="provider-inert", cwd=os.getcwd(),
        permission="yolo")
    monkeypatch.setattr(
        _executor, "build_invocation_args",
        lambda _inv, _timeout, **_kwargs: ("fake-provider", [], None))
    monkeypatch.setattr(
        _executor, "_resolve_launch", lambda command, args: (command, args))
    monkeypatch.setattr(_executor, "argv_length_error", lambda *_args: None)
    monkeypatch.setattr(_executor.subprocess, "Popen", refused_popen)
    monkeypatch.setattr(
        _receipt, "workspace_snapshot", lambda _cwd: {"coverage": "none"})
    monkeypatch.setattr(
        _receipt, "workspace_evidence", lambda *_args, **_kwargs: {})

    result = _executor.execute_agent(
        invocation, timeout_ms=1000, launch_control=control)

    assert result["provider_contacted"] is False
    assert captured["env"] is not None
    assert "SUMMON_ADAPTIVE_TIMEOUT" not in captured["env"]
    assert "SUMMON_FLEET_APPROVAL_KEY" not in captured["env"]


def test_scrub_provider_env_leaves_unrelated_summon_preferences_unchanged():
    original = {
        "SUMMON_JOB_ID": "private-control",
        "SUMMON_TELEMETRY": "1",
        "SUMMON_DEFAULT_EFFORT": "high",
        "PATH": "fixture-path",
    }
    scrubbed = scrub_provider_env(original)
    assert scrubbed == {
        "SUMMON_TELEMETRY": "1",
        "SUMMON_DEFAULT_EFFORT": "high",
        "PATH": "fixture-path",
    }
    assert original["SUMMON_JOB_ID"] == "private-control"
