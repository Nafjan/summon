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
from _spawn import popen_flags
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
