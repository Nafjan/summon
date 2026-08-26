"""Provider-inert tests for adaptive leases and durable job controls."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

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
    assert raw["schema"] == "summon.job-control/v2"
    assert "nonce" not in raw
    assert raw["commands"][0]["message_sha256"]
    assert raw["commands"][0]["auth"]


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
    assert control.refresh(force=True) == 500
    _job_control.queue_command(root, job_id, "cancel")
    control.refresh(force=True)
    assert control.cancel_requested is True


def test_retry_control_preserves_job_origin_and_exposes_attempt_identity(tmp_path):
    root, job_id = _prepared(tmp_path)
    monotonic = Clock()
    wall = Clock()
    wall.value = 110.0
    control = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=10_000,
        max_runtime_ms=120_000, attempt_id="b" * 32,
        attempt_kind="contract_repair", attempt_ordinal=2,
        job_started_at=100.0, clock=monotonic, wall_clock=wall)
    assert control.started == -10.0
    assert control.hard_deadline == 110.0
    control.publish({"phase": "startup"}, force=True)
    heartbeat = _jobs.read_json(_job_control.heartbeat_path(root, job_id))
    assert heartbeat["attempt_id"] == "b" * 32
    assert heartbeat["attempt_kind"] == "contract_repair"
    assert heartbeat["attempt_ordinal"] == 2
    assert heartbeat["state"] == "repairing"
    assert heartbeat["job_elapsed_ms"] == 10_000


def test_expired_job_budget_refuses_before_provider_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMON_ADAPTIVE_TIMEOUT", "1")
    monkeypatch.setenv("SUMMON_JOB_STARTED_AT", "1")
    monkeypatch.setenv("SUMMON_MAX_RUNTIME_MS", "1000")
    monkeypatch.setattr(
        _executor.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired job reached provider spawn")))
    invocation = _executor.AgentInvocation(
        cli="codex", prompt="review", cwd=str(tmp_path),
        model="gpt-5.6-luna", permission="safe-edit")
    response = _executor.execute_agent(invocation, timeout_ms=1000)
    assert response["attempts"] == 0
    assert response["attempt_status"] == "not_run"
    assert response["execution_status"] == "not_run"
    assert response["provider_contacted"] is False
    assert response["error_kind"] == "job_hard_timeout"
    assert response["timeout"]["stage"] == "adaptive_job_hard_timeout"


@pytest.mark.parametrize("origin", ("nan", "inf", "-inf"))
def test_nonfinite_job_origin_is_legacy_unknown_not_an_exception(
        tmp_path, monkeypatch, origin):
    monkeypatch.setenv("SUMMON_ADAPTIVE_TIMEOUT", "1")
    monkeypatch.setenv("SUMMON_JOB_STARTED_AT", origin)
    monkeypatch.setenv("SUMMON_MAX_RUNTIME_MS", "1000")
    budget = _job_control.environment_job_budget(1000, wall_clock=lambda: 10.0)
    assert budget["job_started_at"] is None
    assert budget["deadline_at"] is None
    assert budget["expired"] is False
    monkeypatch.setattr(
        _executor.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    invocation = _executor.AgentInvocation(
        cli="codex", prompt="review", cwd=str(tmp_path),
        model="gpt-5.6-luna", permission="safe-edit")
    response = _executor.execute_agent(invocation, timeout_ms=1000)
    assert response["attempt_status"] == "not_run"
    assert response["provider_contacted"] is False
    assert response.get("error_kind") != "job_hard_timeout"


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
    heartbeat = {"schema": "summon.job-heartbeat/v2", "job_id": job_id,
                 "attempt_id": job_id, "attempt_kind": "initial",
                 "attempt_ordinal": 1, "liveness": {"phase": "generation"}}
    heartbeat["auth"] = _job_control.heartbeat_auth("nonce", heartbeat)
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), heartbeat)
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    status = json.loads(capsys.readouterr().out)
    assert "nonce" not in status["record"]
    assert "nonce" not in status["heartbeat"]
    assert "auth" not in status["heartbeat"]
    assert status["heartbeat"]["liveness"]["phase"] == "generation"
    assert status["heartbeat"]["integrity"] == "payload_authenticated"


def test_heartbeat_contains_auth_tag_not_nonce_and_rejects_forgery(tmp_path, capsys):
    root, job_id = _prepared(tmp_path)
    clock = Clock()
    control = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000, clock=clock)
    control.publish({"phase": "startup"}, force=True)
    raw = _jobs.read_json(_job_control.heartbeat_path(root, job_id))
    assert "nonce" not in raw
    body = {key: value for key, value in raw.items() if key != "auth"}
    assert raw["auth"] == _job_control.heartbeat_auth("nonce", body)
    raw["liveness"] = {"phase": "terminal"}
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), raw)
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    assert json.loads(capsys.readouterr().out)["heartbeat"] is None


def test_jobs_status_rejects_v2_heartbeat_with_legacy_nonce_bypass(
        tmp_path, capsys):
    root, job_id = _prepared(tmp_path)
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), {
        "schema": "summon.job-heartbeat/v2", "job_id": job_id,
        "nonce": "nonce", "liveness": {"phase": "terminal"}})
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    assert json.loads(capsys.readouterr().out)["heartbeat"] is None


def test_jobs_status_rejects_nonascii_heartbeat_auth_without_crashing(
        tmp_path, capsys):
    root, job_id = _prepared(tmp_path)
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), {
        "schema": "summon.job-heartbeat/v1", "job_id": job_id,
        "auth": "\u00e9", "liveness": {"phase": "generation"}})
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    assert json.loads(capsys.readouterr().out)["heartbeat"] is None


def test_jobs_status_accepts_legacy_inflight_nonce_but_never_exposes_it(
        tmp_path, capsys):
    root, job_id = _prepared(tmp_path)
    _jobs._atomic_write_json(_job_control.heartbeat_path(root, job_id), {
        "schema": "summon.job-heartbeat/v1", "job_id": job_id,
        "nonce": "nonce", "liveness": {"phase": "generation"}})
    args = SimpleNamespace(
        job_dir=root, jobs_extend=None, jobs_cancel=None, jobs_steer=None,
        jobs_list=False, jobs_status=job_id, jobs_wait=None, json=True)
    assert _background.run_jobs_query(args, lambda *_args, **_kwargs: None) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["heartbeat"]["liveness"]["phase"] == "generation"
    assert "nonce" not in json.dumps(status)
    assert status["heartbeat"]["integrity"] == "legacy_unverified"


def test_control_generation_rollback_stops_controls_without_killing_work(tmp_path):
    root, job_id = _prepared(tmp_path)
    clock = Clock()
    control = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000, clock=clock)
    _job_control.queue_command(root, job_id, "extend", duration_ms=500)
    assert control.refresh(force=True) == 500
    os.unlink(_job_control.control_path(root, job_id))
    _job_control.queue_command(root, job_id, "cancel")
    control.refresh(force=True)
    assert control.cancel_requested is False
    assert control.control_untrusted is True


def test_control_rejects_duplicate_or_skipped_generations(tmp_path):
    root, job_id = _prepared(tmp_path)
    path = _job_control.control_path(root, job_id)
    duplicate = {
        "schema": "summon.job-control/v2", "job_id": job_id,
        "generation": 2,
        "commands": [
            {"generation": 2, "action": "extend", "duration_ms": 100,
             "queued_at": 1.0, "auth": "0" * 64},
            {"generation": 2, "action": "extend", "duration_ms": 100,
             "queued_at": 2.0, "auth": "0" * 64},
        ],
    }
    _jobs._atomic_write_json(path, duplicate)
    control = _job_control.RuntimeControl(
        path=path, heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000)
    assert control.refresh(force=True) == 0
    assert control.control_untrusted is True
    assert control.cancel_requested is False


def test_control_rejects_payload_mutation_under_retained_auth(tmp_path):
    root, job_id = _prepared(tmp_path)
    _job_control.queue_command(root, job_id, "extend", duration_ms=500)
    path = _job_control.control_path(root, job_id)
    raw = _jobs.read_json(path)
    raw["commands"][0]["duration_ms"] = 50_000
    _jobs._atomic_write_json(path, raw)
    control = _job_control.RuntimeControl(
        path=path, heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000)
    assert control.refresh(force=True) == 0
    assert control.control_untrusted is True
    assert control.cancel_requested is False


def test_public_control_projection_omits_steering_text_and_digest(tmp_path):
    root, job_id = _prepared(tmp_path)
    message = "Focus on parser edge case."
    _job_control.queue_command(root, job_id, "steer", message=message)
    clock = Clock()
    control = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id, nonce="nonce", checkpoint_ms=1_000,
        max_runtime_ms=120_000, clock=clock)
    control.refresh(force=True)
    public = json.dumps(control.projection())
    assert message not in public
    assert "message_sha256" not in public


def test_flags_projection_preserves_explicit_hard_timeout():
    args = SimpleNamespace(hard_timeout=True)
    assert _jobs.flags_projection(args)["hard_timeout"] is True


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


def test_agy_wrapped_result_emits_terminal_liveness():
    tracker = LivenessTracker(attempt_id="agy-terminal", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    assert processor.process_line(json.dumps({
        "event": "result",
        "result": {"status": "success", "response": "STATUS: DONE"},
    })) is True
    snapshot = tracker.snapshot()
    assert snapshot["phase"] == "terminal"
    assert processor.get_result()["status"] == "success"


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
    program = "import os,time; time.sleep(.4); os.close(1); time.sleep(2)"
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


def test_eof_finalization_keeps_its_grace_after_dispatch_budget():
    program = "import os,time; time.sleep(.3); os.close(1); time.sleep(2)"
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    started = __import__("time").monotonic()
    response = _executor._drive_process(
        process, "codex", 500, parse_stream=False,
        attempt_id="f" * 32, first_event_ms=500, idle_ms=500,
        finalization_ms=250)
    elapsed = __import__("time").monotonic() - started
    assert response["timeout"]["stage"] == "finalization_timeout"
    assert elapsed >= 0.50
    assert process.poll() is not None


def test_terminal_event_with_stuck_child_is_force_reaped_and_preserved(monkeypatch):
    events = queue.Queue()
    events.put(("line", json.dumps({"type": "thread.started",
                                     "thread_id": "session"}) + "\n"))
    events.put(("line", json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message",
                                              "text": "STATUS: DONE"}}) + "\n"))
    events.put(("line", json.dumps({"type": "turn.completed",
                                     "usage": {"input_tokens": 1,
                                               "output_tokens": 1}}) + "\n"))

    class Stuck:
        returncode = None
        killed = False
        stdout = None
        stderr = None

        def poll(self):
            return 0 if self.killed else None

        def terminate(self):
            pass

    process = Stuck()
    monkeypatch.setattr(_executor, "_spawn_reader", lambda _process: events)
    monkeypatch.setattr(
        _executor, "_kill_tree",
        lambda target: (setattr(target, "killed", True),
                        setattr(target, "returncode", -9)))
    monkeypatch.setattr(_executor, "_drain_to_eof", lambda _queue: None)
    monkeypatch.setattr(_executor, "_safe_communicate",
                        lambda _process, timeout=1.0: (None, ""))
    response = _executor._drive_process(
        process, "codex", 2_000, parse_stream=True,
        attempt_id="e" * 32, first_event_ms=1_000, idle_ms=1_000,
        finalization_ms=10)
    assert response["status"] == "success"
    assert response["cleanup"]["forced"] is True
    assert response["raw_backend_exit_code"] == -9
    assert process.killed is True


def test_adaptive_expiry_cannot_replace_a_trusted_terminal_result(monkeypatch):
    events = queue.Queue()
    events.put(("line", json.dumps({"type": "thread.started",
                                     "thread_id": "session"}) + "\n"))
    events.put(("line", json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message",
                                              "text": "STATUS: DONE"}}) + "\n"))
    events.put(("line", json.dumps({"type": "turn.completed",
                                     "usage": {"input_tokens": 1,
                                               "output_tokens": 1}}) + "\n"))

    class Stuck:
        returncode = None
        killed = False
        stdout = None
        stderr = None

        def poll(self):
            return 0 if self.killed else None

        def terminate(self):
            pass

    class ExpiringControl:
        checkpoint_ms = 1
        deadline = float("inf")
        hard_deadline = float("inf")
        attention_required = True
        cancel_requested = False

        def __init__(self):
            self.terminal_seen = False

        def refresh(self):
            return 0

        def checkpoint(self, *, active):
            return 0

        def publish(self, snapshot):
            self.terminal_seen = snapshot.get("phase") == "terminal"
            if self.terminal_seen:
                self.cancel_requested = True
            return None

        def expired(self):
            return self.terminal_seen

    process = Stuck()
    tracker = LivenessTracker(attempt_id="terminal", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000,
                              finalization_ms=10)
    emitter = tracker.emitter()
    processor = StreamProcessor(event_observer=emitter)
    monkeypatch.setattr(_executor, "_spawn_reader", lambda _process: events)
    monkeypatch.setattr(
        _executor, "_kill_tree",
        lambda target: (setattr(target, "killed", True),
                        setattr(target, "returncode", -9)))
    monkeypatch.setattr(_executor, "_drain_to_eof", lambda _queue: None)
    monkeypatch.setattr(_executor, "_safe_communicate",
                        lambda _process, timeout=1.0: (None, ""))
    response = _executor._drive_process_loop(
        process, "codex", 2_000, processor, parse_stream=True,
        liveness=tracker, liveness_emitter=emitter,
        runtime_control=ExpiringControl())
    assert response["status"] == "success"
    assert response["cleanup"]["forced"] is True
    assert response.get("timeout") is None
    assert response.get("error_kind") != "operator_cancelled"


@pytest.mark.parametrize("control_kind", ("cancel", "deadline"))
def test_launch_control_cannot_replace_a_trusted_terminal_result(
        monkeypatch, control_kind):
    events = queue.Queue()
    events.put(("line", json.dumps({"type": "thread.started",
                                     "thread_id": "session"}) + "\n"))
    events.put(("line", json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message",
                                              "text": "STATUS: DONE"}}) + "\n"))
    events.put(("line", json.dumps({"type": "turn.completed",
                                     "usage": {"input_tokens": 1,
                                               "output_tokens": 1}}) + "\n"))

    class Stuck:
        returncode = None
        killed = False
        stdout = None
        stderr = None

        def poll(self):
            return 0 if self.killed else None

        def terminate(self):
            pass

    process = Stuck()
    tracker = LivenessTracker(attempt_id="terminal-launch-control",
                              overall_ms=10_000, first_event_ms=1_000,
                              idle_ms=2_000, finalization_ms=10)
    emitter = tracker.emitter()
    processor = StreamProcessor(event_observer=emitter)
    terminal_now = lambda: tracker.snapshot().get("phase") == "terminal"
    launch_control = _executor.ProviderLaunchControl(
        before_launch=lambda _evidence: None,
        cancelled=terminal_now if control_kind == "cancel" else None,
        deadline_reached=terminal_now if control_kind == "deadline" else None)
    monkeypatch.setattr(_executor, "_spawn_reader", lambda _process: events)
    monkeypatch.setattr(
        _executor, "_kill_tree",
        lambda target: (setattr(target, "killed", True),
                        setattr(target, "returncode", -9)))
    monkeypatch.setattr(_executor, "_drain_to_eof", lambda _queue: None)
    monkeypatch.setattr(_executor, "_safe_communicate",
                        lambda _process, timeout=1.0: (None, ""))
    response = _executor._drive_process_loop(
        process, "codex", 2_000, processor, parse_stream=True,
        liveness=tracker, liveness_emitter=emitter,
        launch_control=launch_control)
    assert response["status"] == "success"
    assert response["cleanup"]["forced"] is True
    assert response.get("timeout") is None
    assert "cancel" not in str(response.get("error", "")).lower()


def test_liveness_expiry_cannot_replace_agy_wrapped_terminal_result(monkeypatch):
    events = queue.Queue()
    events.put(("line", json.dumps({
        "event": "result",
        "result": {"status": "success", "response": "STATUS: DONE"},
    }) + "\n"))

    class Stuck:
        returncode = None
        killed = False
        stdout = None
        stderr = None

        def poll(self):
            return 0 if self.killed else None

        def terminate(self):
            pass

    class TerminalExpiringTracker(LivenessTracker):
        def expired(self):
            if self.snapshot()["phase"] == "terminal":
                return "generation_idle_timeout"
            return super().expired()

    process = Stuck()
    tracker = TerminalExpiringTracker(
        attempt_id="agy-terminal-expiry", overall_ms=10_000,
        first_event_ms=1_000, idle_ms=2_000, finalization_ms=10)
    emitter = tracker.emitter()
    processor = StreamProcessor(event_observer=emitter)
    monkeypatch.setattr(_executor, "_spawn_reader", lambda _process: events)
    monkeypatch.setattr(
        _executor, "_kill_tree",
        lambda target: (setattr(target, "killed", True),
                        setattr(target, "returncode", 0)))
    monkeypatch.setattr(_executor, "_drain_to_eof", lambda _queue: None)
    monkeypatch.setattr(_executor, "_safe_communicate",
                        lambda _process, timeout=1.0: (None, ""))
    response = _executor._drive_process_loop(
        process, "agy", 2_000, processor, parse_stream=True,
        liveness=tracker, liveness_emitter=emitter)
    assert response["status"] == "success"
    assert response["cleanup"]["forced"] is True
    assert response.get("timeout") is None


def test_whitespace_only_generation_is_not_meaningful_activity():
    tracker = LivenessTracker(attempt_id="whitespace", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    processor.process_line(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "claude"}))
    processor.process_line(json.dumps({
        "type": "assistant", "session_id": "claude",
        "message": {"content": [{"type": "text", "text": "  \n\t"}]}}))
    snapshot = tracker.snapshot()
    assert snapshot["counts"]["meaningful"] == 0
    assert snapshot["last_activity_kind"] is None


def test_cancelled_is_absorbing_state():
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000)
    emit = tracker.emitter()
    emit.emit("cancelled")
    assert tracker.snapshot()["phase"] == "cancelled"
    emit.emit("output_text", output_chars=5)
    assert tracker.snapshot()["phase"] == "cancelled"
