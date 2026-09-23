"""Provider-inert compatibility tests for AGY 1.1.22 headless contracts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _builder
import _executor
import _resolver
import _spawn
import agy_stream_proxy
from _liveness import LivenessTracker
from _stream import StreamProcessor


FIXTURE = Path(__file__).parents[3] / "tests" / "fixtures" / "agy_1_1_22_stream.jsonl"


def test_nested_stream_captures_target_session_usage_and_meaningful_activity():
    tracker = LivenessTracker(attempt_id="agy-1-1-22", overall_ms=60_000,
                              first_event_ms=10_000, idle_ms=20_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    for line in lines[:-1]:
        assert processor.process_line(line) is False
    assert processor.process_line(lines[-1]) is True

    assert processor.session_id == "fixture-conversation"
    assert processor.handshake_model == "gemini-3.7-flash-high"
    # Even hostile model-looking fields on progress identify neither the served
    # model nor a provider-authored terminal receipt.
    assert processor.model is None
    assert processor.usage == {
        "input_tokens": 22, "output_tokens": 4, "thinking_tokens": 1,
        "cache_read_tokens": 0, "total_tokens": 27,
    }
    assert processor.get_result() == {
        "type": "result", "result": "done\n", "status": "success",
        "_summon_provider_terminal_state": "SUCCESS",
        "_summon_terminal_outcome": "success",
    }
    live = tracker.snapshot()
    assert live["phase"] == "terminal"
    assert live["counts"]["tools"] == 2
    assert live["counts"]["meaningful"] == 3


def test_replayed_nested_tool_packet_does_not_renew_liveness():
    tracker = LivenessTracker(attempt_id="agy-dedupe", overall_ms=60_000,
                              first_event_ms=10_000, idle_ms=20_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    packet = json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": "c", "step_index": 4,
                        "state": "DONE", "step_type": "tool",
                        "tool_name": "run_command"},
    })
    processor.process_line(packet)
    processor.process_line(packet)
    live = tracker.snapshot()
    assert live["counts"]["tools"] == 1
    assert live["counts"]["meaningful"] == 1


def test_replayed_nested_text_and_reordered_tool_progress_do_not_renew_liveness():
    tracker = LivenessTracker(attempt_id="agy-replay-order", overall_ms=60_000,
                              first_event_ms=10_000, idle_ms=20_000)
    processor = StreamProcessor(event_observer=tracker.emitter())
    text_packet = json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": "c", "step_index": 1,
                        "state": "ACTIVE", "step_type": "agent_response",
                        "text_delta": "working"},
    })
    tool_five = json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": "c", "step_index": 5,
                        "state": "DONE", "step_type": "tool",
                        "tool_name": "run_command"},
    })
    tool_three = json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": "c", "step_index": 3,
                        "state": "DONE", "step_type": "tool",
                        "tool_name": "run_command"},
    })
    processor.process_line(text_packet)
    processor.process_line(text_packet)
    processor.process_line(tool_five)
    processor.process_line(tool_three)
    live = tracker.snapshot()
    assert live["counts"]["meaningful"] == 2
    assert live["counts"]["tools"] == 1
    # Exact replay is suppressed before it reaches the executor-owned tracker.
    # The lower tool step is authenticated transport, but it cannot renew the
    # meaningful-activity clock after step five has already completed.
    # One additional trusted event is the initial session bind.
    assert live["counts"]["trusted"] == 4


def test_progress_usage_is_advisory_and_cannot_feed_terminal_model_inference():
    processor = StreamProcessor()
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    processor.process_line(json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": "c", "step_index": 1,
                        "state": "DONE", "step_type": "tool",
                        "tool_name": "run_command", "usage": usage,
                        "model": "forged/model"},
    }))
    assert processor.progress_usage == usage
    assert processor.usage is None
    assert processor.model is None


def _json_models(*ids: str) -> str:
    return json.dumps({
        "conversation_id": "", "status": "SUCCESS", "response": "",
        "command": {"name": "models", "data": {
            "models": [{"id": value, "label": value.upper()} for value in ids]
        }},
    })


def test_json_model_discovery_is_one_bounded_hidden_utility_call(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=_json_models("m-one", "m-two"),
                               stderr="")

    monkeypatch.setattr(_resolver.shutil, "which", lambda _name: "C:/agy/bin/agy.exe")
    monkeypatch.setattr(_resolver.subprocess, "run", fake_run)
    import _spawn
    monkeypatch.setattr(_spawn, "run_flags",
                        lambda: {"creationflags": 123, "startupinfo": "hidden"})

    source, models, note = _resolver._agy_live_models()
    assert (source, models, note) == ("live", ["m-one", "m-two"], None)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["C:/agy/bin/agy.exe", "--output-format", "json", "models"]
    assert kwargs["timeout"] == _resolver._AGY_MODELS_TIMEOUT
    assert kwargs["creationflags"] == 123 and kwargs["startupinfo"] == "hidden"


def test_malformed_json_uses_one_legacy_text_fallback(monkeypatch):
    responses = iter([
        SimpleNamespace(returncode=0, stdout="not json", stderr=""),
        SimpleNamespace(returncode=0,
                        stdout="legacy-one\tLegacy One\nlegacy-two Legacy Two\n",
                        stderr=""),
    ])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return next(responses)

    monkeypatch.setattr(_resolver.shutil, "which", lambda _name: "/usr/bin/agy")
    monkeypatch.setattr(_resolver.subprocess, "run", fake_run)
    source, models, note = _resolver._agy_live_models()
    assert source == "live"
    assert models == ["legacy-one", "legacy-two"]
    assert "Legacy text fallback" in note
    assert [item[0] for item in calls] == [
        ["/usr/bin/agy", "--output-format", "json", "models"],
        ["/usr/bin/agy", "models"],
    ]
    assert all(call[1]["stdin"] is not None for call in calls)


def test_failed_json_envelope_falls_back_but_valid_empty_list_does_not(monkeypatch):
    calls = []
    responses = iter([
        SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": "FAILED", "command": {"name": "models", "data": {"models": []}},
        }), stderr=""),
        SimpleNamespace(returncode=0, stdout="legacy-one\n", stderr=""),
    ])
    monkeypatch.setattr(_resolver.shutil, "which", lambda _name: "/usr/bin/agy")
    monkeypatch.setattr(
        _resolver.subprocess, "run",
        lambda argv, **kwargs: (calls.append(argv), next(responses))[1])
    assert _resolver._agy_live_models()[0:2] == ("live", ["legacy-one"])
    assert len(calls) == 2

    calls.clear()
    monkeypatch.setattr(
        _resolver.subprocess, "run",
        lambda argv, **kwargs: (calls.append(argv), SimpleNamespace(
            returncode=0, stdout=_json_models(), stderr=""))[1])
    assert _resolver._agy_live_models() == ("live", [], None)
    assert len(calls) == 1


def test_windows_cmd_shim_and_native_executable_commands_are_explicit():
    assert _resolver._agy_command(
        "C:/bin/agy.cmd", "--output-format", "json", "models", _platform="nt") == [
            "cmd", "/d", "/c", "C:/bin/agy.cmd",
            "--output-format", "json", "models",
        ]
    assert _resolver._agy_command(
        "C:/bin/agy.exe", "--output-format", "json", "models", _platform="nt") == [
            "C:/bin/agy.exe", "--output-format", "json", "models",
        ]
    assert _resolver._agy_command(
        "C:/bin/agy.bat", "models", _platform="nt") == [
            "cmd", "/d", "/c", "C:/bin/agy.bat", "models",
        ]


def test_terminal_states_survive_parser_and_executor_normalization():
    cases = {
        "SUCCESS": "success",
        "ERROR": "error",
        "INVALID": "error",
        "CANCELED": "blocked",
        "INTERRUPTED": "blocked",
        "WAITING": "partial",
        "RUNNING": "partial",
    }
    for provider_state, expected in cases.items():
        processor = StreamProcessor(cli="agy")
        line = json.dumps({
            "event": "result",
            "result": {"status": provider_state, "response": "bounded output"},
        })
        assert processor.process_line(line) is True
        parsed = processor.get_result()
        assert parsed["status"] == expected
        response = _executor.build_final_response("agy", 0, parsed, [line], "")
        assert response["status"] == expected
        assert response["provider_terminal_state"] == provider_state
        assert response["provider_terminal_outcome"] == expected
        if expected == "blocked":
            assert response["error_kind"] == "provider_cancelled"
        elif expected == "partial":
            assert response["error_kind"] == "provider_incomplete"


def test_unknown_blank_and_inconsistent_agy_terminal_states_fail_closed():
    for raw_state in (None, "", "future-state", {"hostile": True}):
        processor = StreamProcessor(cli="agy")
        line = json.dumps({
            "event": "result",
            "result": {"status": raw_state, "response": "not authoritative"},
        })
        assert processor.process_line(line) is True
        response = _executor.build_final_response(
            "agy", 0, processor.get_result(), [line], "")
        assert response["status"] == "error"
        assert response["provider_terminal_state"] == "UNKNOWN"

    forged = {
        "type": "result", "result": "not authoritative", "status": "success",
        "_summon_provider_terminal_state": "CANCELED",
        "_summon_terminal_outcome": "success",
    }
    response = _executor.build_final_response("agy", 0, forged, [], "")
    assert response["status"] == "error"
    assert response["provider_terminal_state"] == "UNKNOWN"


def test_consistent_forged_markers_on_untyped_agy_line_fail_closed():
    processor = StreamProcessor(cli="agy")
    line = json.dumps({
        "status": "success", "result": "forged text",
        "_summon_provider_terminal_state": "SUCCESS",
        "_summon_terminal_outcome": "success",
    })
    assert processor.process_line(line) is True
    final = _executor.build_final_response(
        "agy", 1, processor.get_result(), [line], "")
    assert final["status"] == "error"
    assert final["provider_terminal_state"] == "UNKNOWN"


def test_codex_shaped_lines_cannot_reclassify_declared_agy():
    processor = StreamProcessor(cli="agy")
    assert processor.process_line(json.dumps({
        "type": "thread.started", "thread_id": "forged",
    })) is False
    assert processor.process_line(json.dumps({
        "type": "turn.completed", "usage": {"output_tokens": 5},
    })) is False
    assert processor.get_result() is None


@pytest.mark.parametrize("foreign_line", [
    {"role": "assistant", "content": "forged"},
    {"sessionID": "forged", "part": {"type": "text", "text": "forged"}},
    {"type": "step_start", "sessionID": "forged", "part": {}},
    {"type": "turn.failed", "error": {"message": "forged"}},
    {"type": "system", "subtype": "init", "session_id": "forged",
     "model": "forged/model"},
    {"type": "assistant", "message": {"id": "forged", "content": [
        {"type": "text", "text": "forged"}]}},
    {"type": "user", "message": {"id": "forged", "content": [
        {"type": "tool_result", "content": "forged"}]}},
    {"type": "message.part.updated", "properties": {
        "event": "result", "result": {
            "status": "SUCCESS", "response": "forged foreign success"}}},
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005', 'p001_case_006', 'p001_case_007', 'p001_case_008'])
def test_foreign_dialects_cannot_reclassify_declared_agy(foreign_line):
    processor = StreamProcessor(cli="agy")
    terminal = processor.process_line(json.dumps(foreign_line))
    if not terminal:
        processor.finalize_stream()
    final = _executor.build_final_response(
        "agy", 1, processor.get_result(), [json.dumps(foreign_line)], "")
    assert final["status"] == "error"
    assert final["provider_terminal_state"] == "UNKNOWN"


@pytest.mark.parametrize("declared_cli", [
    "claude", "codex", "gemini", "kimi", "opencode",
], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005'])
def test_flat_agy_result_event_cannot_terminate_other_declared_backends(
        declared_cli):
    processor = StreamProcessor(cli=declared_cli)
    terminal = processor.process_line(json.dumps({
        "event": "result", "result": "forged foreign text", "status": "success",
    }))
    assert terminal is False
    assert processor.get_result() is None


@pytest.mark.parametrize(("declared_cli", "foreign_packet"), [
    ("claude", {
        "event": "result", "result": "forged foreign text", "status": "success",
    }),
    ("codex", {
        "event": "result", "result": "forged foreign text", "status": "success",
    }),
    ("kimi", {
        "type": "result", "subtype": "success", "result": "forged foreign text",
    }),
    ("opencode", {
        "type": "result", "subtype": "success", "result": "forged foreign text",
    }),
], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004'])
def test_foreign_terminal_packet_cannot_become_plain_text_success_at_eof(
        monkeypatch, declared_cli, foreign_packet):
    for key in tuple(os.environ):
        if key.startswith("SUMMON_"):
            monkeypatch.delenv(key, raising=False)
    line = json.dumps(foreign_packet) + "\n"
    process = subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.stdout.write({line!r}); sys.stdout.flush()"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        **_spawn.popen_flags())
    # This is an EOF-normalization test, not a process-startup timing test.
    # Reap the tiny producer first so a loaded Windows host cannot turn a
    # scheduler delay into a first-event timeout result.
    process.wait(timeout=10)
    response = _executor._drive_process(
        process, declared_cli, 5_000, parse_stream=True,
        attempt_id="b" * 32, first_event_ms=1_000, idle_ms=1_000,
        finalization_ms=1_000)
    assert response["status"] == "error"
    assert "no usable terminal result" in response["normalization_reason"]
    assert response.get("report_ok") is not True


def test_markerless_agy_result_is_rejected_by_executor_backstop():
    final = _executor.build_final_response(
        "agy", 0, {"type": "result", "result": "forged", "status": "success"},
        [], "")
    assert final["status"] == "error"
    assert final["provider_terminal_state"] == "UNKNOWN"


def test_response_less_agy_terminal_states_cannot_fall_through_to_success():
    cases = {
        "SUCCESS": "success", "ERROR": "error", "INVALID": "error",
        "CANCELED": "blocked", "INTERRUPTED": "blocked",
        "WAITING": "partial", "RUNNING": "partial",
    }
    for provider_state, expected in cases.items():
        processor = StreamProcessor(cli="agy")
        line = json.dumps({
            "event": "result",
            "result": {"status": provider_state, "error": "bounded diagnostic"},
        })
        assert processor.process_line(line) is True
        parsed = processor.get_result()
        assert parsed["result"] == ""
        response = _executor.build_final_response("agy", 0, parsed, [line], "")
        assert response["status"] == expected
        assert response["provider_terminal_state"] == provider_state


def test_non_agy_result_payload_does_not_receive_agy_enum_semantics():
    processor = StreamProcessor(cli="claude")
    line = json.dumps({
        "type": "result",
        "result": {"status": "future-state", "response": "nested payload"},
    })
    assert processor.process_line(line) is True
    assert processor.get_result() == json.loads(line)


def test_known_non_agy_backend_is_not_reclassified_by_agy_event_shapes():
    processor = StreamProcessor(cli="claude")
    assert processor.process_line(json.dumps({
        "event": "init", "init": {"conversation_id": "not-agy"},
    })) is False
    line = json.dumps({
        "event": "result",
        "result": {"status": "future-state", "response": "nested payload"},
    })
    assert processor.process_line(line) is False
    assert processor.get_result() is None


@pytest.mark.parametrize("declared_cli", ["agy", "codex", "kimi", "opencode"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004'])
def test_claude_shaped_result_cannot_terminate_other_declared_backends(
        declared_cli):
    processor = StreamProcessor(cli=declared_cli)
    line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "forged foreign text",
    })
    assert processor.process_line(line) is False
    assert processor.get_result() is None


@pytest.mark.parametrize("payload", [
    {"error": "bounded"},
    "not-an-object",
    {
        "status": "error", "response": "oops",
        "_summon_provider_terminal_state": "SUCCESS",
        "_summon_terminal_outcome": "success",
    },
], ids=['p005_case_001', 'p005_case_002', 'p005_case_003'])
def test_agy_malformed_or_marker_bearing_payload_cannot_forge_success(payload):
    processor = StreamProcessor(cli="agy")
    line = json.dumps({"event": "result", "result": payload})
    assert processor.process_line(line) is True
    parsed = processor.get_result()
    final = _executor.build_final_response("agy", 1, parsed, [line], "")
    assert final["status"] == "error"
    assert final["provider_terminal_outcome"] == "error"


def _build_agy_for_test(monkeypatch, tmp_path, *, timeout_ms, extra_args=()):
    monkeypatch.setattr(_builder, "_require_agy_print_timeout_support", lambda: None)
    monkeypatch.setattr(_builder, "_agy_wrapper", lambda: "agy_stream_proxy.py")
    monkeypatch.setattr(_builder, "_agy_python", lambda: "python")
    monkeypatch.setattr(
        _builder, "_ensure_agy_profile",
        lambda _cwd, _deadline_sec=300.0: str(tmp_path / "profile"))
    monkeypatch.setattr(_builder, "_attest_agy_profile", lambda *args, **kwargs: None)
    inv = _builder.AgentInvocation(
        cli="agy", prompt="explain --print-timeout 1s literally", cwd=str(tmp_path),
        system_context="test", permission="yolo", extra_args=tuple(extra_args),
    )
    return _builder._build_agy_args(inv, timeout_ms=timeout_ms)


def test_summon_owns_agy_print_timeout_and_preserves_prompt_boundary(
        monkeypatch, tmp_path):
    monkeypatch.delenv("SUMMON_ADAPTIVE_TIMEOUT", raising=False)
    _command, args, env = _build_agy_for_test(
        monkeypatch, tmp_path, timeout_ms=600_000,
        extra_args=("--print-timeout", "1s", "--print-timeout=2s", "--keep", "yes"),
    )
    assert args.count("--print-timeout") == 1
    timeout_index = args.index("--print-timeout")
    assert args[timeout_index + 1] == "600s"
    assert "--print-timeout=2s" not in args and "1s" not in args
    assert args[args.index("--print") + 1].find("--print-timeout 1s literally") >= 0
    assert args[args.index("--print") - 2:args.index("--print")] == [
        "--print-timeout", "600s"]
    assert env["AGY_PTY_DEADLINE"] == "600.0"
    assert "--keep" in args and "yes" in args


def test_adaptive_agy_print_timeout_uses_remaining_hard_budget(
        monkeypatch, tmp_path):
    import _job_control

    monkeypatch.setenv("SUMMON_ADAPTIVE_TIMEOUT", "1")
    monkeypatch.setenv("SUMMON_MAX_RUNTIME_MS", "1200000")
    monkeypatch.setenv("SUMMON_JOB_STARTED_AT", "100")
    monkeypatch.setattr(_job_control.time, "time", lambda: 400.0)
    _command, args, env = _build_agy_for_test(
        monkeypatch, tmp_path, timeout_ms=600_000)
    timeout_index = args.index("--print-timeout")
    assert args[timeout_index + 1] == "900s"
    assert env["AGY_PTY_DEADLINE"] == "900.0"


def test_agy_orphan_profile_retention_is_bounded_without_clamping_runtime():
    assert _builder._agy_profile_retention_sec(600) == 1500
    assert _builder._agy_profile_retention_sec(7 * 24 * 60 * 60) == 24 * 60 * 60


@pytest.fixture
def agy_probe_env(monkeypatch, tmp_path):
    """Isolated capability cache plus a fake agy identity; returns the cache path."""
    cache = tmp_path / "agy-capability.json"
    monkeypatch.setenv("SUMMON_AGY_CAPABILITY_CACHE", str(cache))
    monkeypatch.setattr(_builder.shutil, "which", lambda _name: "C:/bin/agy.exe")
    monkeypatch.setattr(_builder, "_agy_binary_identity",
                        lambda _path: ("C:/bin/agy.exe", 7))
    monkeypatch.setattr(_builder, "_AGY_PRINT_TIMEOUT_CAPABILITY", {})
    return cache


def test_agy_print_timeout_capability_gate_is_provider_inert_and_cached(
        monkeypatch, agy_probe_env):
    calls = []
    monkeypatch.setattr(
        _builder, "_run_agy_help",
        lambda command, timeout: calls.append((command, timeout))
        or (0, "--print-timeout duration"))
    _builder._require_agy_print_timeout_support()
    _builder._require_agy_print_timeout_support()
    assert len(calls) == 1
    assert calls[0][0][-1] == "--help"
    assert calls[0][1] == _builder._AGY_PROBE_TIMEOUT_SEC >= 60


def test_agy_print_timeout_capability_gate_rejects_old_cli(monkeypatch, agy_probe_env):
    monkeypatch.setattr(_builder, "_run_agy_help", lambda *a: (0, "usage without flag"))
    with pytest.raises(_builder.AgyCapabilityError, match="1.1.22") as caught:
        _builder._require_agy_print_timeout_support()
    assert caught.value.kind == "agy_cli_outdated"
    assert caught.value.retryable is False
    # A negative answer is never persisted: an upgrade must not be masked.
    assert not agy_probe_env.exists()


def test_agy_capability_probe_stall_is_retryable_and_not_called_outdated(
        monkeypatch, agy_probe_env):
    calls = []

    def stalled(command, timeout):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(_builder, "_run_agy_help", stalled)
    for _ in range(2):
        with pytest.raises(_builder.AgyCapabilityError) as caught:
            _builder._require_agy_print_timeout_support()
        assert caught.value.kind == "agy_capability_probe_stalled"
        assert caught.value.retryable is True
        assert "1.1.22" not in str(caught.value)
    # A stall is not an answer: it is neither memoized nor persisted.
    assert len(calls) == 2
    assert not agy_probe_env.exists()


def test_agy_capability_probe_nonzero_exit_is_not_called_outdated(
        monkeypatch, agy_probe_env):
    monkeypatch.setattr(_builder, "_run_agy_help", lambda *a: (3, "boom"))
    with pytest.raises(_builder.AgyCapabilityError) as caught:
        _builder._require_agy_print_timeout_support()
    assert caught.value.kind == "agy_capability_probe_failed"
    assert "1.1.22" not in str(caught.value)


def test_agy_help_probe_bound_kills_a_hung_child():
    """The probe bound must hold even when the child never exits."""
    import time
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _builder._run_agy_help([sys.executable, "-c", "import time; time.sleep(60)"], 1)
    assert time.monotonic() - started < 20


def test_agy_capability_positive_answer_survives_a_new_process(
        monkeypatch, agy_probe_env):
    monkeypatch.setattr(_builder, "_run_agy_help", lambda *a: (0, "--print-timeout x"))
    _builder._require_agy_print_timeout_support()
    assert agy_probe_env.exists()

    # A fresh dispatcher process (empty in-memory memo) must not probe again.
    monkeypatch.setattr(_builder, "_AGY_PRINT_TIMEOUT_CAPABILITY", {})
    monkeypatch.setattr(_builder, "_run_agy_help",
                        lambda *a: pytest.fail("probe re-ran despite disk cache"))
    _builder._require_agy_print_timeout_support()

    # A changed binary (upgrade/downgrade) is a new identity and is probed again.
    monkeypatch.setattr(_builder, "_agy_binary_identity",
                        lambda _path: ("C:/bin/agy.exe", 8))
    probed = []
    monkeypatch.setattr(_builder, "_probe_agy_print_timeout",
                        lambda path: probed.append(path) or True)
    _builder._require_agy_print_timeout_support()
    assert probed == ["C:/bin/agy.exe"]


def test_agy_binary_identity_changes_when_size_changes_but_mtime_is_kept(tmp_path):
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"v1")
    stamp = os.stat(binary).st_mtime_ns
    first = _builder._agy_binary_identity(str(binary))
    binary.write_bytes(b"version-2")
    os.utime(binary, ns=(stamp, stamp))
    assert _builder._agy_binary_identity(str(binary)) != first


def test_concurrent_agy_dispatches_share_one_capability_probe(
        monkeypatch, agy_probe_env):
    import threading

    # Another dispatcher holds the probe lock and publishes a positive answer
    # before releasing it -- the same order the real owner uses.
    lock = Path(str(agy_probe_env) + ".lock")
    lock.write_text("peer-token", encoding="utf-8")

    def peer_finishes():
        _builder._store_agy_capability(("C:/bin/agy.exe", 7))
        lock.unlink()

    timer = threading.Timer(0.5, peer_finishes)
    timer.start()
    try:
        monkeypatch.setattr(_builder, "_probe_agy_print_timeout",
                            lambda path: pytest.fail("waiter probed instead of sharing"))
        _builder._require_agy_print_timeout_support()
    finally:
        timer.cancel()


def test_agy_probe_owner_publishes_before_releasing_the_lock(monkeypatch, agy_probe_env):
    lock = Path(str(agy_probe_env) + ".lock")
    seen = []
    real_store = _builder._store_agy_capability

    def store(identity):
        seen.append(lock.exists())
        real_store(identity)

    monkeypatch.setattr(_builder, "_store_agy_capability", store)
    monkeypatch.setattr(_builder, "_run_agy_help", lambda *a: (0, "--print-timeout x"))
    _builder._require_agy_print_timeout_support()
    assert seen == [True]
    assert not lock.exists()


def test_abandoned_lock_that_cannot_be_removed_does_not_spin(monkeypatch, agy_probe_env):
    import time
    lock = Path(str(agy_probe_env) + ".lock")
    lock.write_text("dead-owner", encoding="utf-8")
    old = time.time() - 10 * _builder._AGY_PROBE_LOCK_STALE_SEC
    os.utime(lock, (old, old))
    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self == lock:
            raise PermissionError("locked by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    probed = []
    monkeypatch.setattr(_builder, "_probe_agy_print_timeout",
                        lambda path: probed.append(path) or True)
    started = time.monotonic()
    _builder._require_agy_print_timeout_support()
    assert time.monotonic() - started < 5
    assert probed == ["C:/bin/agy.exe"]


def test_unusable_cache_location_degrades_to_an_unserialized_probe(
        monkeypatch, tmp_path, agy_probe_env):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("SUMMON_AGY_CAPABILITY_CACHE", str(blocker / "cache.json"))
    probed = []
    monkeypatch.setattr(_builder, "_probe_agy_print_timeout",
                        lambda path: probed.append(path) or True)
    _builder._require_agy_print_timeout_support()
    assert probed == ["C:/bin/agy.exe"]


def test_lock_of_a_dead_owner_is_stolen_without_waiting_for_the_stale_window(
        monkeypatch, agy_probe_env):
    """An owner killed mid-probe must not hold every agy dispatch for ~150s."""
    import time
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    lock = Path(str(agy_probe_env) + ".lock")
    lock.write_text(f"{child.pid}:{'0' * 32}", encoding="ascii")  # fresh mtime
    probed = []
    monkeypatch.setattr(_builder, "_probe_agy_print_timeout",
                        lambda path: probed.append(path) or True)
    started = time.monotonic()
    _builder._require_agy_print_timeout_support()
    assert time.monotonic() - started < 5
    assert probed == ["C:/bin/agy.exe"]


def test_live_owner_lock_is_not_stolen_early(monkeypatch, agy_probe_env):
    slot = _builder._agy_capability_probe_slot(("C:/bin/agy.exe", 7))
    lock = Path(str(agy_probe_env) + ".lock")
    lock.write_text(f"{os.getpid()}:{'0' * 32}", encoding="ascii")
    assert slot._owner_exited() is False
    lock.write_text("not-a-token", encoding="ascii")
    assert slot._owner_exited() is False


def test_lock_open_error_rechecks_the_cache_before_probing(monkeypatch, agy_probe_env):
    _builder._store_agy_capability(("C:/bin/agy.exe", 7))
    slot = _builder._agy_capability_probe_slot(("C:/bin/agy.exe", 7))
    real_open = _builder.os.open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError("delete pending")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(_builder.os, "open", denied)
    with slot as cached:
        assert cached is True


def test_probe_owner_never_releases_a_lock_it_no_longer_owns(agy_probe_env):
    slot = _builder._agy_capability_probe_slot(("C:/bin/agy.exe", 7))
    with slot:
        lock = Path(str(agy_probe_env) + ".lock")
        # A peer judged the lock stale and replaced it with its own.
        lock.write_text("peer-token", encoding="utf-8")
    assert lock.read_text(encoding="utf-8") == "peer-token"


def test_typed_agy_refusal_reaches_envelope_and_telemetry(monkeypatch, tmp_path):
    import _telemetry
    import run_subagent

    def refuse(*_args, **_kwargs):
        raise _builder.AgyCapabilityError("agy capability probe (`agy --help`) stalled",
                                          kind="agy_capability_probe_stalled",
                                          retryable=True)

    monkeypatch.setattr(_executor, "build_invocation_args", refuse)
    inv = _builder.AgentInvocation(cli="agy", prompt="hi", cwd=str(tmp_path))
    envelope = _executor.execute_agent(inv, timeout_ms=60_000)
    assert envelope["status"] == "error"
    assert envelope["error_kind"] == "agy_capability_probe_stalled"
    assert envelope["retryable"] is True
    assert run_subagent._is_transient_dispatch_error(envelope) is True
    assert _telemetry._failure_class(envelope) == "dispatch"

    outdated = dict(envelope, error_kind="agy_cli_outdated", retryable=False,
                    error="agy does not expose --print-timeout; install AGY 1.1.22")
    assert run_subagent._is_transient_dispatch_error(outdated) is False
    assert _telemetry._failure_class(outdated) == "missing_cli"
    # Untyped prose naming the flag is not a timeout either.
    untyped = {"status": "error", "error": "agy does not expose --print-timeout"}
    assert _telemetry._failure_class(untyped) != "timeout"


def test_agy_profile_cleanup_preserves_live_owner_and_reaps_dead_owner(
        monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    token = "a" * 32
    profile = runs / f"run-123-{token}-owned"
    profile.mkdir(parents=True)
    (profile / _builder._AGY_OWNER_PID_FILE).write_text("123", encoding="utf-8")
    (profile / ".summon_expiry").write_text("0", encoding="utf-8")
    monkeypatch.setattr(_builder, "_agy_lease_state", lambda _path: "held")
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert profile.exists()
    monkeypatch.setattr(_builder, "_agy_lease_state", lambda _path: "free")
    monkeypatch.setattr(_builder.os.path, "getmtime", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert not profile.exists()


@pytest.mark.parametrize("forged_expiry", ["inf", "nan", "1e300"], ids=['p006_case_001', 'p006_case_002', 'p006_case_003'])
def test_agy_dead_owner_cannot_extend_retention_with_forged_expiry(
        monkeypatch, tmp_path, forged_expiry):
    runs = tmp_path / "runs"
    profile = runs / f"run-123-{'b' * 32}-forged"
    profile.mkdir(parents=True)
    (profile / _builder._AGY_OWNER_PID_FILE).write_text(
        "999999", encoding="utf-8")
    (profile / ".summon_expiry").write_text(forged_expiry, encoding="utf-8")
    monkeypatch.setattr(_builder, "_agy_lease_state", lambda _path: "free")
    monkeypatch.setattr(_builder.os.path, "getctime", lambda _path: 0)
    monkeypatch.setattr(_builder.os.path, "getmtime", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert not profile.exists()


def test_agy_forged_owner_marker_cannot_create_live_owner_exemption(
        monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    profile = runs / "run-321-forged"
    profile.mkdir(parents=True)
    (profile / _builder._AGY_OWNER_PID_FILE).write_text(
        "777", encoding="utf-8")
    (profile / ".summon_expiry").write_text("0", encoding="utf-8")
    observed = []
    monkeypatch.setattr(
        _builder, "_agy_lease_state",
        lambda path: observed.append(path) or "held")
    monkeypatch.setattr(_builder.os.path, "getctime", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert observed == []
    assert not profile.exists()


def test_agy_profile_lease_is_os_held_until_released(tmp_path):
    runs = tmp_path / "runs"
    handle, path, token = _builder._agy_create_profile_lease(
        str(runs), _builder.time.time() + 3600)
    profile = str(runs / f"run-1-{token}-test")
    _builder._AGY_PROFILE_LEASES[profile] = (handle, path)
    assert _builder._agy_lease_state(path) == "held"
    _builder._agy_release_profile_lease(profile)
    assert _builder._agy_lease_state(path) == "missing"
    assert not os.path.exists(_builder._agy_private_lease_path(path))


def test_agy_profile_lease_is_locked_before_publication(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    real_link = _builder.os.link
    observed = []

    def inspect_publication(private_path, public_path):
        observed.append((private_path, public_path))
        assert not os.path.exists(public_path)
        assert _builder._agy_lease_state(private_path) == "held"
        _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
        assert os.path.exists(private_path)
        real_link(private_path, public_path)

    monkeypatch.setattr(_builder.os, "link", inspect_publication)
    handle, path, token = _builder._agy_create_profile_lease(
        str(runs), _builder.time.time() + 3600)
    profile = str(runs / f"run-1-{token}-test")
    _builder._AGY_PROFILE_LEASES[profile] = (handle, path)
    try:
        assert len(observed) == 1
        assert _builder._agy_lease_state(path) == "held"
    finally:
        _builder._agy_release_profile_lease(profile)


def test_legacy_agy_plain_output_is_not_replaced_by_stream_finalization(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("SUMMON_"):
            monkeypatch.delenv(key, raising=False)
    process = subprocess.Popen(
        [sys.executable, "-c", "print('legacy wrapper report', flush=True)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        **_spawn.popen_flags())
    # Generous clocks: this pins output handling, not startup latency, and a loaded
    # host can take over a second just to start the interpreter.
    response = _executor._drive_process(
        process, "agy", 30_000, parse_stream=False,
        attempt_id="a" * 32, first_event_ms=15_000, idle_ms=15_000,
        finalization_ms=15_000)
    assert response["status"] == "success"
    assert response["result"].strip() == "legacy wrapper report"
    assert "provider_terminal_state" not in response


def test_real_live_owner_lease_survives_orphan_cap(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    created_at = _builder.time.time()
    handle, path, token = _builder._agy_create_profile_lease(
        str(runs), created_at + 7 * 24 * 60 * 60)
    profile = runs / f"run-{os.getpid()}-{token}-owned"
    profile.mkdir(parents=True)
    (profile / ".summon_expiry").write_text("0", encoding="utf-8")
    profile_key = os.path.realpath(profile)
    _builder._AGY_PROFILE_LEASES[profile_key] = (handle, path)
    try:
        assert _builder._agy_lease_state(path) == "held"
        assert _builder._agy_lease_metadata(path) is not None
        assert _builder._agy_lease_owner_live(path) is True
        monkeypatch.setattr(
            _builder.time, "time", lambda: created_at + 25 * 60 * 60)
        _builder._agy_cleanup_old_runs(str(runs), deadline_sec=7 * 24 * 60 * 60)
        assert profile.exists()
    finally:
        _builder._agy_release_profile_lease(profile_key)


def test_lease_metadata_is_bound_to_profile_token(tmp_path):
    runs = tmp_path / "runs"
    handle, path, _token = _builder._agy_create_profile_lease(
        str(runs), _builder.time.time() + 3600)
    try:
        metadata = _builder._agy_lease_metadata(path)
        assert metadata is not None
        with open(path, "r+b", buffering=0) as taker:
            forged = {
                "schema": _builder._AGY_LEASE_SCHEMA,
                "expires_at": _builder.time.time() + 3600,
                "owner_pid": os.getpid(),
                "owner_birth": _builder._agy_process_birth_token(os.getpid()),
                "nonce": "0" * 64,
            }
            taker.seek(0)
            taker.write((json.dumps(
                forged, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
            taker.flush()
        assert _builder._agy_lease_metadata(path) is None
        assert _builder._agy_lease_owner_live(path) is False
    finally:
        try:
            _builder._agy_unlock_lease(handle)
        except OSError:
            pass
        handle.close()
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(os.name != "nt", reason="reproduces Windows pre-open takeover")
def test_preopened_taken_over_lease_cannot_bypass_orphan_cap(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    created_at = _builder.time.time()
    owner, path, token = _builder._agy_create_profile_lease(
        str(runs), created_at + 7 * 24 * 60 * 60)
    profile = runs / f"run-{os.getpid()}-{token}-owned"
    profile.mkdir(parents=True)
    (profile / ".summon_expiry").write_text("inf", encoding="utf-8")
    profile_key = os.path.realpath(profile)
    _builder._AGY_PROFILE_LEASES[profile_key] = (owner, path)
    taker = open(path, "r+b", buffering=0)
    try:
        forged = {
            "schema": _builder._AGY_LEASE_SCHEMA,
            "expires_at": created_at + 7 * 24 * 60 * 60,
            "owner_pid": os.getpid(),
            "owner_birth": _builder._agy_process_birth_token(os.getpid()),
            "nonce": "1" * 64,
        }
        taker.seek(0)
        taker.write((json.dumps(
            forged, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
        taker.flush()
        _builder._agy_release_profile_lease(profile_key)
        _builder._agy_lock_lease(taker, blocking=False)
        monkeypatch.setattr(
            _builder.time, "time", lambda: created_at + 25 * 60 * 60)
        _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
        assert not profile.exists()
    finally:
        try:
            _builder._agy_unlock_lease(taker)
        except OSError:
            pass
        taker.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_agy_cleanup_never_treats_lease_registry_as_a_profile(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    registry = runs / ".summon-leases"
    registry.mkdir(parents=True)
    marker = registry / "lease.lock"
    marker.write_bytes(b"1")
    monkeypatch.setattr(_builder.os.path, "getctime", lambda _path: 0)
    monkeypatch.setattr(_builder.os.path, "getmtime", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert marker.read_bytes() == b"1"


def test_taken_over_lease_cannot_exceed_host_hard_cap(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    token = "d" * 32
    profile = runs / f"run-123-{token}-owned"
    profile.mkdir(parents=True)
    (profile / ".summon_expiry").write_text("inf", encoding="utf-8")
    monkeypatch.setattr(_builder, "_agy_lease_state", lambda _path: "held")
    monkeypatch.setattr(_builder, "_agy_lease_owner_live", lambda _path: False)
    monkeypatch.setattr(_builder, "_agy_lease_expiry_cap", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert not profile.exists()


def test_proven_live_owner_outlives_orphan_cap(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    token = "e" * 32
    profile = runs / f"run-123-{token}-owned"
    profile.mkdir(parents=True)
    (profile / ".summon_expiry").write_text("0", encoding="utf-8")
    monkeypatch.setattr(_builder, "_agy_lease_state", lambda _path: "held")
    monkeypatch.setattr(_builder, "_agy_lease_owner_live", lambda _path: True)
    monkeypatch.setattr(_builder, "_agy_lease_expiry_cap", lambda _path: 0)
    _builder._agy_cleanup_old_runs(str(runs), deadline_sec=1)
    assert profile.exists()


def test_proxy_strips_caller_timeout_but_not_prompt_text():
    assert agy_stream_proxy._strip_boundary_flags([
        "--print-timeout", "1s", "--print-timeout=2s",
        "--print", "quote --print-timeout 3s",
    ]) == ["--print", "quote --print-timeout 3s"]


def test_proxy_strips_go_style_single_dash_timeout_spellings():
    assert agy_stream_proxy._strip_boundary_flags([
        "-print-timeout", "1s", "-print-timeout=2s",
        "--print", "quote -print-timeout=3s",
    ]) == ["--print", "quote -print-timeout=3s"]


def test_proxy_child_joins_outer_posix_process_group(monkeypatch):
    monkeypatch.setattr(_spawn.os, "name", "posix")
    assert _spawn.popen_flags() == {"start_new_session": True}
    assert _spawn.popen_flags(join_parent_group=True) == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_proxy_timeout_kills_real_descendant_process_group(tmp_path):
    pid_file = tmp_path / "pids.json"
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys, time\n"
        "from _spawn import run_flags\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], **run_flags())\n"
        "with open(os.environ['FAKE_AGY_PIDS'], 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'agy': os.getpid(), 'child': child.pid}, fh)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    env["FAKE_AGY_PIDS"] = str(pid_file)
    proxy = subprocess.Popen(
        [sys.executable, str(Path(agy_stream_proxy.__file__)), "--print", "x"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        **_spawn.popen_flags(),
    )
    try:
        for _ in range(100):
            if pid_file.is_file():
                break
            import time
            time.sleep(0.05)
        assert pid_file.is_file(), "fake AGY did not start"
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        assert os.getpgid(pids["agy"]) == proxy.pid
        assert os.getpgid(pids["child"]) == proxy.pid
        _executor._kill_tree(proxy)
        proxy.wait(timeout=5)

        def alive_non_zombie(pid):
            try:
                os.kill(pid, 0)
                stat = Path(f"/proc/{pid}/stat")
                return not stat.is_file() or stat.read_text().split()[2] != "Z"
            except (OSError, IndexError):
                return False

        for _ in range(100):
            if not any(alive_non_zombie(pid) for pid in pids.values()):
                break
            import time
            time.sleep(0.05)
        assert not any(alive_non_zombie(pid) for pid in pids.values())
    finally:
        if proxy.poll() is None:
            try:
                os.killpg(proxy.pid, signal.SIGKILL)
            except OSError:
                pass
            proxy.wait(timeout=5)


def test_builder_strips_every_proxy_boundary_flag_from_agent_args():
    supplied = [
        "--agent", "foreign", "--project=p", "--log-file", "outside.log",
        "--output-file=outside.md", "--conversation", "other", "--continue-id=c",
        "--resume", "--continue", "--new-project", "--output-format", "text",
        "--output=json", "-of", "markdown", "--keep", "allowed",
    ]
    assert _builder._strip_agy_boundary_flags(supplied) == ["--keep", "allowed"]


def test_posix_process_birth_falls_back_to_bounded_native_ps(monkeypatch):
    monkeypatch.setattr(_builder.Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no proc")))
    monkeypatch.setattr(_builder.os.path, "isfile", lambda path: path == "/bin/ps")
    observed = {}

    def fake_run(argv, **kwargs):
        observed.update({"argv": argv, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="Thu Aug 27 10:00:00 2026\n")

    monkeypatch.setattr(_builder.subprocess, "run", fake_run)
    token = _builder._agy_posix_process_birth_token(123)
    assert token is not None and token.startswith("ps:") and len(token) == 67
    assert observed["argv"] == ["/bin/ps", "-o", "lstart=", "-p", "123"]
    assert observed["kwargs"]["timeout"] == 2.0
