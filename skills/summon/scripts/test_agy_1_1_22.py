"""Provider-inert compatibility tests for AGY 1.1.22 headless contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import _resolver
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
