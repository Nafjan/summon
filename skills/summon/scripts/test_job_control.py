"""Provider-inert tests for adaptive leases and durable job controls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import _executor
import _background
import _cli
import _job_control
import _jobs
from _liveness import LivenessTracker
from _stream import StreamProcessor


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _prepared(tmp_path):
    root = str(tmp_path)
    job_id = "a" * 32
    _jobs.write_prepared(
        root, job_id, nonce="nonce", agent="reviewer",
        prompt_sha256="b" * 64, cwd=root, flags={},
        summon={"scripts_sha256": "c" * 64})
    return root, job_id


def test_control_commands_are_authenticated_bounded_and_prompt_private(tmp_path):
    root, job_id = _prepared(tmp_path)
    queued = _job_control.queue_command(
        root, job_id, "steer", message="Focus on the failing parser test.")
    assert queued["steering_mode"] == "queued_for_resume"
    summary = _job_control.control_summary(root, job_id)
    assert summary["counts"]["steer"] == 1
    assert "Focus" not in json.dumps(summary)
    raw = _jobs.read_json(_job_control.control_path(root, job_id))
    assert raw["nonce"] == "nonce"
    assert raw["commands"][0]["message_sha256"]


def test_jobs_subcommands_rewrite_public_flags_without_prompt_mangling():
    job_id = "a" * 32
    assert _cli.rewrite_subcommand(
        ["jobs", "extend", job_id, "--duration", "15m"]
    ) == (["--jobs-extend", job_id, "--job-duration", "15m"], None)
    assert _cli.rewrite_subcommand(
        ["jobs", "steer", job_id, "--message", "focus now"]
    ) == (["--jobs-steer", job_id, "--job-message", "focus now"], None)


def test_adaptive_control_auto_extends_active_and_flags_idle(tmp_path):
    root, job_id = _prepared(tmp_path)
    clock = Clock()
    control = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000, clock=clock)
    clock.advance(1)
    assert control.checkpoint(active=True) == 1_000
    assert control.auto_extensions == 1
    clock.advance(1)
    assert control.checkpoint(active=False) > 0
    assert control.attention_required is True
    grace_deadline = control.deadline
    clock.value = grace_deadline
    assert control.checkpoint(active=False) == 0
    assert control.deadline == grace_deadline
    _job_control.queue_command(root, job_id, "extend", duration_ms=500)
    assert control.refresh() == 500
    _job_control.queue_command(root, job_id, "cancel")
    control.refresh()
    assert control.cancel_requested is True


def test_concurrent_extend_and_cancel_are_both_preserved(tmp_path):
    root, job_id = _prepared(tmp_path)
    barrier = threading.Barrier(3)
    errors = []

    def worker(action):
        try:
            barrier.wait()
            _job_control.queue_command(
                root, job_id, action,
                duration_ms=500 if action == "extend" else None)
        except Exception as exc:  # surfaced after both writers join
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(action,))
               for action in ("extend", "cancel")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert not errors
    raw = _jobs.read_json(_job_control.control_path(root, job_id))
    assert {item["action"] for item in raw["commands"]} == {"extend", "cancel"}
    assert raw["generation"] == 2


def test_jobs_status_authenticates_heartbeat_and_redacts_nonce(tmp_path, capsys):
    root, job_id = _prepared(tmp_path)
    heartbeat = {"schema": "summon.job-heartbeat/v1", "job_id": job_id,
                 "nonce": "nonce", "liveness": {"phase": "generation"}}
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), heartbeat)
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    status = json.loads(capsys.readouterr().out)
    assert "nonce" not in status["record"]
    assert "nonce" not in status["heartbeat"]
    assert status["heartbeat"]["liveness"]["phase"] == "generation"


def test_stream_processor_reports_authenticated_reconnect_and_agy_progress():
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    processor.process_line(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "one"}))
    processor.process_line(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "two"}))
    processor.process_line(json.dumps(
        {"event": "step_update", "conversation_id": "two",
         "tool_name": "pytest"}))
    snapshot = tracker.snapshot()
    assert snapshot["counts"]["reconnects"] == 1
    assert snapshot["counts"]["tools"] == 1
    assert snapshot["counts"]["meaningful"] == 1


def test_stream_processor_counts_claude_codex_gemini_and_kimi_tool_progress():
    fixtures = [
        ({"type": "system", "subtype": "init", "session_id": "claude"},
         {"type": "assistant", "session_id": "claude", "message": {"content": [
             {"type": "tool_use", "id": "private-id", "name": "Read"}]}}),
        ({"type": "thread.started", "thread_id": "codex"},
         {"type": "item.completed", "item": {"type": "command_execution"}}),
        ({"type": "init", "session_id": "gemini"},
         {"type": "tool_call", "session_id": "gemini", "name": "read_file"}),
        ({"role": "system", "session_id": "kimi"},
         {"role": "assistant", "session_id": "kimi", "content": "",
          "tool_calls": [{"id": "private-id"}]}),
    ]
    for index, (start, progress) in enumerate(fixtures):
        tracker = LivenessTracker(attempt_id=f"attempt-{index}", overall_ms=10_000,
                                  first_event_ms=1_000, idle_ms=2_000)
        processor = StreamProcessor(event_observer=tracker.emitter())
        processor.process_line(json.dumps(start))
        processor.process_line(json.dumps(progress))
        snapshot = tracker.snapshot()
        assert snapshot["counts"]["tools"] == 1
        assert snapshot["last_activity_kind"] == "tool"


def test_eof_finalization_timeout_still_reaps_child():
    program = "import os,time; os.close(1); time.sleep(2)"
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    response = _executor._drive_process(
        process, "codex", 2_000, parse_stream=True,
        attempt_id="d" * 32, first_event_ms=1_000, idle_ms=1_000,
        finalization_ms=75)
    assert response["timeout"]["stage"] == "finalization_timeout"
    assert response["liveness"]["phase"] == "timed_out"
    assert process.poll() is not None


def test_cancelled_is_absorbing_state():
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000)
    emit = tracker.emitter()
    emit.emit("cancelled")
    assert tracker.snapshot()["phase"] == "cancelled"
    emit.emit("output_text", output_chars=5)
    assert tracker.snapshot()["phase"] == "cancelled"
