"""Provider-free tests for derived work tokens, context occupancy and shadow signals."""
from __future__ import annotations

import pytest

import _progress_signals as signals
import _usage_metrics as metrics


# --- work tokens ---------------------------------------------------------

def test_claude_shape_excludes_cache_reads_from_work_tokens():
    usage = {"input_tokens": 12, "output_tokens": 300,
             "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 950_000}
    out = metrics.work_tokens(usage, cli="claude", observation_scope="attempt_total")
    assert out["input_semantics"] == metrics.EXCLUDES_CACHE
    assert out["work_tokens"] == 12 + 300 + 2_000
    assert out["cache_read_tokens"] == 950_000
    assert out["evidence"] == "derived"
    assert out["observation_scope"] == "attempt_total"


def test_openai_shape_subtracts_cached_input_that_is_already_inside_input():
    usage = {"prompt_tokens": 10_000, "completion_tokens": 500,
             "prompt_tokens_details": {"cached_tokens": 9_000}}
    out = metrics.work_tokens(usage, cli="openai-compat")
    assert out["input_semantics"] == metrics.INCLUDES_CACHE
    assert out["work_tokens"] == 1_000 + 500
    assert out["cache_read_tokens"] == 9_000


def test_codex_shape_uses_cached_input_tokens():
    out = metrics.work_tokens({"input_tokens": 800, "cached_input_tokens": 600,
                               "output_tokens": 40}, cli="codex")
    assert out["work_tokens"] == 240
    assert out["input_semantics"] == metrics.INCLUDES_CACHE


def test_field_dialect_beats_cli_hint():
    # An Anthropic-shaped record is classified by its fields even under another CLI.
    out = metrics.work_tokens({"input_tokens": 1, "output_tokens": 2,
                               "cache_read_input_tokens": 50}, cli="codex")
    assert out["input_semantics"] == metrics.EXCLUDES_CACHE
    assert out["work_tokens"] == 3


def test_unknown_semantics_without_cache_fields_is_labelled_not_guessed():
    out = metrics.work_tokens({"input_tokens": 5, "output_tokens": 6}, cli="kimi")
    assert out["work_tokens"] == 11
    assert out["evidence"] == "derived_no_cache_fields"


def test_unknown_semantics_with_cache_fields_refuses_to_derive():
    out = metrics.work_tokens({"input_tokens": 5, "output_tokens": 6,
                               "cache_read_tokens": 3}, cli="kimi")
    assert out["work_tokens"] is None
    assert out["evidence"] == "ambiguous_cache_semantics"


@pytest.mark.parametrize("usage, evidence", [
    (None, "absent"),
    ("not a dict", "absent"),
    ({"output_tokens": 3}, "incomplete"),
    ({"input_tokens": True, "output_tokens": 3}, "incomplete"),
    ({"input_tokens": float("nan"), "output_tokens": 3}, "incomplete"),
    ({"input_tokens": -1, "output_tokens": 3}, "incomplete"),
])
def test_missing_or_malformed_usage_never_produces_a_number(usage, evidence):
    out = metrics.work_tokens(usage, cli="claude")
    assert out["work_tokens"] is None
    assert out["evidence"] == evidence


def test_inconsistent_openai_cache_fields_fail_closed():
    out = metrics.work_tokens({"prompt_tokens": 10, "completion_tokens": 1,
                               "prompt_tokens_details": {"cached_tokens": 20}})
    assert out["work_tokens"] is None
    assert out["evidence"] == "inconsistent_cache_fields"


# --- context occupancy -----------------------------------------------------

def test_claude_context_occupancy_counts_cache_reads():
    last = {"input_tokens": 3, "cache_creation_input_tokens": 1_000,
            "cache_read_input_tokens": 149_000, "output_tokens": 200}
    out = metrics.context_occupancy(last, 200_000, cli="claude")
    assert out["occupied_tokens"] == 150_003
    assert out["window_tokens"] == 200_000
    assert out["ratio"] == pytest.approx(0.75, abs=1e-4)
    assert out["over_window"] is False
    assert out["evidence"] == "reported"


def test_occupancy_without_window_reports_tokens_but_no_ratio():
    out = metrics.context_occupancy({"input_tokens": 10, "cache_read_input_tokens": 5}, None)
    assert out["occupied_tokens"] == 15
    assert out["ratio"] is None
    assert out["evidence"] == "occupied_without_window"


@pytest.mark.parametrize("window", [0, -5, True, "200000", float("inf")])
def test_invalid_window_is_treated_as_absent(window):
    out = metrics.context_occupancy({"input_tokens": 10}, window, cli="claude")
    assert out["window_tokens"] is None
    assert out["ratio"] is None


def test_over_window_is_flagged_not_clamped():
    out = metrics.context_occupancy({"input_tokens": 300}, 200, cli="claude")
    assert out["ratio"] == 1.5
    assert out["over_window"] is True


def test_absent_last_request_yields_absent_occupancy():
    assert metrics.context_occupancy(None, 200_000)["evidence"] == "absent"


# --- shadow progress signals ----------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_identical_tool_calls_trip_tool_loop_in_shadow_only():
    tracker = signals.ProgressSignals(repeat_limit=3)
    for _ in range(3):
        tracker.tool_call("Bash", {"command": "pytest -q"})
    snap = tracker.snapshot()
    assert snap["mode"] == "shadow"
    assert snap["current_repeat"] == 3
    assert snap["active"] == ["tool_loop"]
    assert snap["observed"] == ["tool_loop"]
    assert snap["distinct_tool_calls"] == 1


def test_varied_tool_calls_never_trip_and_reset_the_streak():
    tracker = signals.ProgressSignals(repeat_limit=3)
    for i in range(20):
        tracker.tool_call("Bash", {"command": f"step {i}"})
    snap = tracker.snapshot()
    assert snap["max_repeat"] == 1
    assert snap["observed"] == []


def test_shared_long_preamble_does_not_collide():
    preamble = "cd /very/long/absolute/path/" + "x" * 500 + " && python measure.py "
    tracker = signals.ProgressSignals(repeat_limit=2)
    tracker.tool_call("Bash", {"command": preamble + "--case 1"})
    tracker.tool_call("Bash", {"command": preamble + "--case 2"})
    assert tracker.snapshot()["max_repeat"] == 1


def test_differences_beyond_the_string_cap_are_treated_as_identical():
    base = "y" * signals.MAX_STRING_CHARS
    assert (signals.tool_signature("Write", {"content": base + "A"})
            == signals.tool_signature("Write", {"content": base + "B"}))


def test_signature_is_key_order_independent_and_tolerates_odd_payloads():
    assert (signals.tool_signature("T", {"a": 1, "b": 2})
            == signals.tool_signature("T", {"b": 2, "a": 1}))
    deep: dict = {}
    node = deep
    for _ in range(200):
        node["n"] = {}
        node = node["n"]
    assert len(signals.tool_signature("T", deep)) == 64
    assert len(signals.tool_signature(None, object())) == 64
    assert len(signals.tool_signature("T", {"f": float("nan")})) == 64


def test_error_storm_counts_consecutive_failures_and_success_resets():
    tracker = signals.ProgressSignals(error_limit=3)
    for _ in range(2):
        tracker.tool_result(is_error=True)
    tracker.tool_result(is_error=False)
    for _ in range(3):
        tracker.tool_result(is_error=True)
    snap = tracker.snapshot()
    assert snap["current_error_streak"] == 3
    assert snap["max_error_streak"] == 3
    assert snap["failed_results"] == 5
    assert snap["active"] == ["error_storm"]


def test_observed_survives_recovery_but_active_clears():
    tracker = signals.ProgressSignals(repeat_limit=2)
    tracker.tool_call("Read", {"p": 1})
    tracker.tool_call("Read", {"p": 1})
    tracker.tool_call("Read", {"p": 2})
    snap = tracker.snapshot()
    assert snap["active"] == []
    assert snap["observed"] == ["tool_loop"]


def test_ms_since_distinct_tool_uses_injected_clock():
    clock = FakeClock()
    tracker = signals.ProgressSignals(clock=clock)
    assert tracker.snapshot()["ms_since_distinct_tool"] is None
    tracker.tool_call("Read", {"p": 1})
    clock.now += 2.5
    tracker.tool_call("Read", {"p": 1})
    assert tracker.snapshot()["ms_since_distinct_tool"] == 2500


def test_snapshot_carries_no_tool_names_inputs_or_digests():
    tracker = signals.ProgressSignals()
    tracker.tool_call("SecretTool", {"token": "sk-live-abcdef"})
    rendered = repr(tracker.snapshot())
    assert "SecretTool" not in rendered
    assert "sk-live" not in rendered
    assert signals.tool_signature("SecretTool", {"token": "sk-live-abcdef"}) not in rendered


@pytest.mark.parametrize("bad", [1, 0, True, 2.5, "8"])
def test_limits_are_validated(bad):
    with pytest.raises(ValueError):
        signals.ProgressSignals(repeat_limit=bad)


# --- stream wiring (provider-free NDJSON fixtures) ---------------------------

import json as _json

import _executor
from _stream import StreamProcessor


def _feed(processor, events):
    for event in events:
        processor.process_line(_json.dumps(event))


def _claude_tool_turn(i, command, *, is_error=False, usage=None, parent=None):
    message = {"id": f"msg_{i}", "model": "claude-opus-5", "role": "assistant",
               "content": [{"type": "tool_use", "id": f"tu_{i}", "name": "Bash",
                            "input": {"command": command}}]}
    if usage is not None:
        message["usage"] = usage
    return [
        {"type": "assistant", "session_id": "s1", "parent_tool_use_id": parent,
         "message": message},
        {"type": "user", "session_id": "s1", "parent_tool_use_id": parent,
         "message": {"id": f"u_{i}", "role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}",
                                  "is_error": is_error, "content": "x"}]}},
    ]


def test_claude_repeated_bash_calls_with_fresh_ids_register_as_a_loop():
    processor = StreamProcessor(cli="claude")
    events = [{"type": "system", "subtype": "init", "session_id": "s1",
               "model": "claude-opus-5"}]
    for i in range(9):
        events += _claude_tool_turn(i, "npm test", is_error=True)
    _feed(processor, events)
    snap = processor.progress_snapshot()
    assert snap["coverage"] == "instrumented"
    assert snap["tool_calls"] == 9
    assert snap["current_repeat"] == 9
    assert "tool_loop" in snap["active"] and "error_storm" in snap["active"]


def test_claude_root_usage_and_model_window_feed_context_occupancy():
    processor = StreamProcessor(cli="claude")
    root_usage = {"input_tokens": 4, "cache_creation_input_tokens": 1_000,
                  "cache_read_input_tokens": 99_000, "output_tokens": 50}
    sub_usage = {"input_tokens": 999_999, "output_tokens": 1}
    events = [{"type": "system", "subtype": "init", "session_id": "s1",
               "model": "claude-opus-5"}]
    events += _claude_tool_turn(1, "ls", usage=root_usage)
    events += _claude_tool_turn(2, "ls -la", usage=sub_usage, parent="tu_1")
    events.append({"type": "result", "subtype": "success", "session_id": "s1",
                   "result": "done", "usage": {"input_tokens": 10, "output_tokens": 60,
                                               "cache_read_input_tokens": 99_000},
                   "modelUsage": {"claude-opus-5": {"contextWindow": 200_000},
                                  "claude-haiku-5": {"contextWindow": 50_000}}})
    _feed(processor, events)
    assert processor.last_request_usage == root_usage   # sub-agent usage ignored
    assert processor.context_window == 200_000           # served model's window only
    response = {"cli": "claude", "usage": processor.usage,
                "usage_observation": processor.usage_observation}
    _executor._attach_long_horizon_metrics(response, processor)
    assert response["context_occupancy"]["occupied_tokens"] == 100_004
    assert response["context_occupancy"]["ratio"] == pytest.approx(0.5, abs=1e-4)
    assert response["usage_metrics"]["work_tokens"] == 70
    assert response["usage_metrics"]["observation_scope"] == "attempt_total"
    assert response["progress_signals"]["mode"] == "shadow"


def test_codex_items_signal_repeats_and_failed_commands():
    processor = StreamProcessor(cli="codex")
    events = [{"type": "thread.started", "thread_id": "t1"}]
    for i in range(3):
        events.append({"type": "item.completed", "item": {
            "id": f"item_{i}", "type": "command_execution", "command": "pytest -q",
            "aggregated_output": f"run {i}", "exit_code": 1, "status": "failed"}})
    _feed(processor, events)
    snap = processor.progress_snapshot()
    assert snap["current_repeat"] == 3
    assert snap["current_error_streak"] == 3


def test_metrics_attach_without_processor_and_never_override_existing_keys():
    response = {"cli": "openai-compat",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "progress_signals": {"sentinel": True}}
    _executor._attach_long_horizon_metrics(response, None)
    assert response["usage_metrics"]["work_tokens"] == 110
    assert "context_occupancy" not in response
    assert response["progress_signals"] == {"sentinel": True}


def test_signal_failures_never_break_stream_parsing(monkeypatch):
    processor = StreamProcessor(cli="claude")

    def boom(*_a, **_k):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(processor.progress_signals, "tool_call", boom)
    monkeypatch.setattr(processor.progress_signals, "tool_result", boom)
    _feed(processor, [{"type": "system", "subtype": "init", "session_id": "s1",
                       "model": "claude-opus-5"}] + _claude_tool_turn(1, "ls"))
    assert processor._tool_progress == 2  # liveness accounting unaffected
