"""Provider-inert Kimi timeout and ACP-fallback contract tests."""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _builder
import _cli
import _executor
import run_subagent
from _builder import AgentInvocation
from _stream import StreamProcessor


def _args(**overrides):
    values = {
        "retries": 0,
        "retry_nonretryable": False,
        "transient_retries": False,
        "no_acp_fallback": False,
        "allow_kimi_acp_fallback": False,
        "timeout": 1_000,
        "agent": "kimi-worker",
        "_receipt": None,
        "gate_with": None,
        "debug_dir": None,
        "max_tool_output_bytes": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _kimi_processor(*contents: str) -> StreamProcessor:
    processor = StreamProcessor()
    for content in contents:
        assert processor.process_line(json.dumps({
            "role": "assistant", "content": content,
        })) is False
    return processor


def _feed_kimi_fixture(events):
    """Feed sanitized Kimi 0.38 stream shapes without contacting a provider."""
    processor = StreamProcessor()
    for event in events:
        line = event if isinstance(event, str) else json.dumps(event)
        assert processor.process_line(line) is False
    return processor


_KIMI_038_TOOL_THEN_REPORT = [
    {"role": "meta", "type": "system.version", "version": "0.38.0"},
    {"role": "tool", "tool_call_id": "fixture-tool", "content": "verifier ok"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "fixture-tool"}]},
    {"authority_issued": True, "governance_conformance": "PASS",
     "product_readiness": "ADVISORY", "verdict": "PASS"},
]

_KIMI_038_VERIFIER_ONLY = [
    {"role": "meta", "type": "system.version", "version": "0.38.0"},
    {"authority_issued": True, "governance_conformance": "PASS",
     "measured_blockers": [], "verdict": "PASS"},
]


def test_kimi_acp_fallback_flag_is_explicit_and_fanout_safe():
    parser = _cli.build_parser("3.2.1", 1)
    parsed = parser.parse_args([
        "--agent", "kimi-worker", "--prompt", "test",
        "--allow-kimi-acp-fallback",
    ])

    assert parsed.allow_kimi_acp_fallback is True
    for mode in ("manifest", "council", "council-resume"):
        assert "allow_kimi_acp_fallback" in _cli.MODE_FLAGS[mode]


def test_kimi_timeout_snapshot_is_bounded_redacted_and_non_authoritative():
    processor = _kimi_processor(
        "draft\napi_key=fixture-value\n",
        "final-looking text",
    )
    raw = [json.dumps({"role": "assistant", "content": "final-looking text"}) + "\n"]
    envelope = _executor._timeout_payload("kimi", processor, 1_000, raw)
    envelope = _executor._enrich(envelope, processor)

    assert envelope["result"] == ""
    assert envelope["report"] is None
    assert envelope["report_ok"] is False
    assert envelope["timeout"]["partial_output"] is True
    assert envelope["partial_output_only"] is True
    assert envelope["partial"]["authoritative"] is False
    assert envelope["partial"]["finalized"] is False
    assert envelope["partial"]["source"] == "stream_parts_pre_eof"
    assert "fixture-value" not in envelope["partial"]["text"]
    assert "<redacted>" in envelope["partial"]["text"]
    # ``served_model_evidence`` is stamped by the full executor response path;
    # this provider-inert unit path has no model identity to attest.
    assert envelope.get("served_model_evidence") in (None, "absent")


def test_kimi_partial_snapshot_is_bounded_at_accumulation():
    processor = _kimi_processor("x" * 50_000)
    snapshot = processor.kimi_partial_snapshot()

    assert snapshot is not None
    assert snapshot["bytes_retained"] <= 32 * 1024
    assert snapshot["truncated"] is True
    assert snapshot["truncated_chars"] > 0
    assert snapshot["captured_chars"] == 50_000


def test_kimi_assistant_metadata_exposes_provider_model_without_guessing():
    provider = StreamProcessor()
    assert provider.process_line(json.dumps({
        "role": "system", "model": "kimi-code/k3",
    })) is False
    assert provider.process_line(json.dumps({
        "role": "assistant", "model": "kimi-code/k3",
        "usage": {"output_tokens": 7}, "content": "answer",
    })) is False
    provider.finalize_stream()

    assert provider.handshake_model == "kimi-code/k3"
    assert provider.model == "kimi-code/k3"
    assert provider.models_used == ["kimi-code/k3"]
    assert provider.model_evidence_source == "kimi_assistant_record"
    assert provider.usage["output_tokens"] == 7


def test_kimi_without_provider_model_keeps_served_identity_unverified():
    provider = _kimi_processor("answer")
    provider.finalize_stream()

    assert provider.model is None
    assert provider.handshake_model is None
    assert provider.models_used == []
    assert provider.model_evidence_source is None


def test_kimi_provider_model_metadata_reaches_the_envelope(monkeypatch):
    """Exercise the real executor stamp with a provider-inert Kimi JSONL child."""
    lines = [
        {"role": "assistant", "model": "kimi-code/k3",
         "usage": {"output_tokens": 4}, "content": "answer"},
    ]
    code = "import json; " + "; ".join(
        f"print({json.dumps(json.dumps(item))})" for item in lines)
    invocation = AgentInvocation(
        cli="kimi", model="kimi-code/k3", prompt="review",
        cwd=os.getcwd(), permission="yolo")
    monkeypatch.setattr(
        _executor, "build_invocation_args",
        lambda _inv, *_args: (sys.executable, ("-c", code), None),
    )
    monkeypatch.setattr("_receipt.workspace_snapshot", lambda _cwd: {"coverage": "none"})
    monkeypatch.setattr("_receipt.workspace_evidence", lambda *_args: {})

    result = _executor.execute_agent(invocation, timeout_ms=5_000)
    assert result["status"] == "success"
    assert result["model"]["served"] == "kimi-code/k3"
    assert result["served_model_evidence"] == "inferred"
    assert result["model"]["evidence_source"] == "kimi_assistant_record"
    assert result["usage"]["output_tokens"] == 4


def test_kimi_terminal_parser_branch_never_mints_reported_identity(monkeypatch):
    """Even a generic terminal shape is child stdout on the Kimi CLI."""
    terminal = {
        "type": "result", "status": "success", "result": "answer",
        "model": "kimi-code/k3", "usage": {"output_tokens": 4},
    }
    code = f"print({json.dumps(json.dumps(terminal))})"
    invocation = AgentInvocation(
        cli="kimi", model="kimi-code/k3", prompt="review", cwd=os.getcwd(),
        permission="yolo", model_exact_required=True, model_exact_source="test")
    monkeypatch.setattr(
        _executor, "build_invocation_args",
        lambda _inv, *_args: (sys.executable, ("-c", code), None),
    )
    monkeypatch.setattr("_receipt.workspace_snapshot", lambda _cwd: {"coverage": "none"})
    monkeypatch.setattr("_receipt.workspace_evidence", lambda *_args: {})
    result = _executor.execute_agent(invocation, timeout_ms=5_000)
    assert result["served_model_evidence"] != "reported"
    assert result["named_model_verified"] is False
    assert result["status"] in {"blocked", "error"}


def _write_kimi_wire(profile, events):
    wire = profile / "sessions" / "fixture" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("".join(json.dumps(item) + "\n" for item in events),
                    encoding="utf-8")


def test_kimi_wire_requires_post_response_usage_and_completed_turn(tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "profile"
    _write_kimi_wire(profile, [
        {"type": "llm.request", "provider": "openai", "model": "k3",
         "modelAlias": "kimi-code/k3", "time": now},
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 8}, "usageScope": "turn", "time": now + 1},
        {"type": "turn.ended", "reason": "completed", "time": now + 2},
    ])

    evidence = _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1, ended_wall_ms=now + 3)

    assert evidence == {
        "model": "kimi-code/k3",
        "models_used": ["kimi-code/k3"],
        "usage_records": 1,
        "turn_completed": True,
        "source": "kimi_wire_usage_record",
    }


def test_kimi_wire_request_or_mixed_usage_never_mints_model_proof(tmp_path):
    now = int(time.time() * 1000)
    request_only = tmp_path / "request-only"
    _write_kimi_wire(request_only, [
        {"type": "llm.request", "provider": "openai", "model": "k3",
         "modelAlias": "kimi-code/k3", "time": now},
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 99}, "usageScope": "session", "time": now},
        {"type": "turn.ended", "reason": "completed", "time": now + 1},
    ])
    assert _executor._capture_kimi_wire_model(
        str(request_only), started_wall_ms=now - 1,
        ended_wall_ms=now + 2) is None

    mixed = tmp_path / "mixed"
    _write_kimi_wire(mixed, [
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 2}, "usageScope": "turn", "time": now},
        {"type": "usage.record", "model": "other/model",
         "usage": {"output": 2}, "usageScope": "turn", "time": now + 1},
        {"type": "turn.ended", "reason": "completed", "time": now + 2},
    ])
    assert _executor._capture_kimi_wire_model(
        str(mixed), started_wall_ms=now - 1,
        ended_wall_ms=now + 3) is None


def test_kimi_wire_rejects_nonfinite_and_unbounded_numbers(tmp_path):
    now = int(time.time() * 1000)
    for name, output, event_time in (
            ("infinite-output", "Infinity", str(now)),
            ("nan-output", "NaN", str(now)),
            ("infinite-time", "1", "Infinity"),
            ("float-output", "1.5", str(now)),
            ("huge-output", str(1 << 63), str(now))):
        profile = tmp_path / name
        wire = profile / "sessions" / "fixture" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text(
            '{"type":"usage.record","model":"kimi-code/k3",'
            f'"usage":{{"output":{output}}},"usageScope":"turn","time":{event_time}}}\n'
            f'{{"type":"turn.ended","reason":"completed","time":{now + 1}}}\n',
            encoding="utf-8")
        assert _executor._capture_kimi_wire_model(
            str(profile), started_wall_ms=now - 1,
            ended_wall_ms=now + 2) is None


def test_kimi_wire_valid_record_cannot_mask_malformed_neighbor(tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "composite-malformed"
    _write_kimi_wire(profile, [
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 2}, "usageScope": "turn", "time": now},
        {"type": "usage.record", "model": "other/model",
         "usage": {"output": 1 << 63}, "usageScope": "turn", "time": now + 1},
        {"type": "turn.ended", "reason": "completed", "time": now + 2},
    ])
    assert _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1,
        ended_wall_ms=now + 3) is None


def test_kimi_wire_rejects_duplicate_json_keys(tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "duplicate-key"
    wire = profile / "sessions" / "fixture" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text(
        '{"type":"usage.record","model":"other/model",'
        '"model":"kimi-code/k3","usage":{"output":2},'
        f'"usageScope":"turn","time":{now}}}\n'
        f'{{"type":"turn.ended","reason":"completed","time":{now + 1}}}\n',
        encoding="utf-8")
    assert _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1,
        ended_wall_ms=now + 2) is None


def test_kimi_wire_requires_completion_after_usage_in_record_order(tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "reordered"
    _write_kimi_wire(profile, [
        {"type": "turn.ended", "reason": "completed", "time": now + 2},
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 2}, "usageScope": "turn", "time": now + 1},
    ])
    assert _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1,
        ended_wall_ms=now + 3) is None


def test_kimi_wire_rejects_same_metadata_file_replacement(monkeypatch, tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "replaced"
    original_events = [
        {"type": "usage.record", "model": "kimi-code/k3",
         "usage": {"output": 2}, "usageScope": "turn", "time": now},
        {"type": "turn.ended", "reason": "completed", "time": now + 1},
    ]
    replacement_events = [
        {"type": "usage.record", "model": "evil-code/k3",
         "usage": {"output": 2}, "usageScope": "turn", "time": now},
        {"type": "turn.ended", "reason": "completed", "time": now + 1},
    ]
    _write_kimi_wire(profile, original_events)
    wire = profile / "sessions" / "fixture" / "agents" / "main" / "wire.jsonl"
    original_stat = wire.stat()
    original_open = _executor.os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and os.path.normcase(os.fspath(path)) == os.path.normcase(str(wire)):
            replaced = True
            replacement = wire.with_suffix(".replacement")
            replacement.write_text(
                "".join(json.dumps(item) + "\n" for item in replacement_events),
                encoding="utf-8")
            assert replacement.stat().st_size == original_stat.st_size
            os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            os.replace(replacement, wire)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(_executor.os, "open", replacing_open)
    assert _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1,
        ended_wall_ms=now + 2) is None


def test_kimi_child_writable_wire_cannot_certify_exact_envelope(monkeypatch, tmp_path):
    """A child-authored Kimi journal is routing evidence, never provider proof."""
    profile = tmp_path / "profile"
    code = (
        "import json,os,time;"
        "p=os.path.join(os.environ['KIMI_CODE_HOME'],'sessions','fixture',"
        "'agents','main','wire.jsonl');"
        "os.makedirs(os.path.dirname(p),exist_ok=True);"
        "n=int(time.time()*1000);"
        "events=["
        "{'type':'usage.record','model':'kimi-code/k3','usage':{'output':4},"
        "'usageScope':'turn','time':n},"
        "{'type':'turn.ended','reason':'completed','time':n+1}];"
        "open(p,'w',encoding='utf-8').write(''.join(json.dumps(x)+'\\n' for x in events));"
        "print(json.dumps({'role':'assistant','content':'answer'}))"
    )
    invocation = AgentInvocation(
        cli="kimi", model="kimi-code/k3", prompt="review",
        cwd=os.getcwd(), permission="yolo", model_exact_required=True,
        model_exact_source="cli")
    monkeypatch.setattr(
        _executor, "build_invocation_args",
        lambda _inv, *_args: (
            sys.executable, ("-c", code),
            {"KIMI_CODE_HOME": str(profile), "HOME": str(profile),
             "USERPROFILE": str(profile)}),
    )
    monkeypatch.setattr("_receipt.workspace_snapshot", lambda _cwd: {"coverage": "none"})
    monkeypatch.setattr("_receipt.workspace_evidence", lambda *_args: {})
    monkeypatch.setattr(_builder, "sync_kimi_profile_credentials", lambda _profile: 0)

    result = _executor.execute_agent(invocation, timeout_ms=5_000)

    assert result["status"] == "blocked"
    assert result["model"]["served"] == "kimi-code/k3"
    assert result["served_model_evidence"] == "inferred"
    assert result["model"]["evidence_source"] == "kimi_wire_usage_record"
    assert result["model_match"] is None
    assert result["named_model_verified"] is False
    assert result["error_kind"] == "served_model_unverified"
    assert result["kimi_runtime_evidence"] == {
        "source": "kimi_wire_usage_record",
        "usage_records": 1,
        "turn_completed": True,
    }


def test_kimi_wire_scan_is_bounded_to_contract_shape(tmp_path):
    now = int(time.time() * 1000)
    profile = tmp_path / "profile"
    # A tempting file outside the exact sessions/<id>/agents/main shape is not
    # recursively discovered.
    stray = profile / "deep" / "nested" / "agents" / "main" / "wire.jsonl"
    stray.parent.mkdir(parents=True)
    stray.write_text(
        json.dumps({"type": "usage.record", "model": "kimi-code/k3",
                    "usage": {"output": 2}, "usageScope": "turn", "time": now})
        + "\n" + json.dumps({"type": "turn.ended", "reason": "completed",
                               "time": now + 1}) + "\n",
        encoding="utf-8")
    (profile / "sessions").mkdir(parents=True)
    assert _executor._capture_kimi_wire_model(
        str(profile), started_wall_ms=now - 1, ended_wall_ms=now + 2) is None


def test_kimi_038_tool_then_untyped_report_is_content_not_terminal():
    processor = _feed_kimi_fixture(_KIMI_038_TOOL_THEN_REPORT)

    assert processor.is_kimi is True
    assert processor.get_result() is None

    processor.finalize_stream()
    result = processor.get_result()
    assert result["status"] == "success"
    assert '"verdict":"PASS"' in result["result"]


def test_kimi_038_verifier_only_shape_is_content_not_terminal():
    processor = _feed_kimi_fixture(_KIMI_038_VERIFIER_ONLY)

    assert processor.is_kimi is True
    assert processor.get_result() is None

    processor.finalize_stream()
    result = processor.get_result()
    assert result["status"] == "success"
    assert '"governance_conformance":"PASS"' in result["result"]


def test_kimi_038_nonzero_exit_remains_fail_closed_for_both_shapes():
    for events in (_KIMI_038_TOOL_THEN_REPORT, _KIMI_038_VERIFIER_ONLY):
        processor = _feed_kimi_fixture(events)
        processor.finalize_stream()
        raw = [
            (event if isinstance(event, str) else json.dumps(event)) + "\n"
            for event in events
        ]
        response = _executor.build_final_response(
            "kimi", 1, processor.get_result(), raw, "")
        response = _executor._enrich(response, processor)
        assert response["status"] == "error"
        assert response["backend_exit_code"] == 1
        assert response["report_ok"] is False
        assert response["result"]


def test_kimi_timeout_does_not_auto_spend_an_acp_fallback(monkeypatch):
    invocation = AgentInvocation(
        cli="kimi", prompt="test", cwd=os.getcwd(), permission="yolo")
    primary = {
        "status": "error", "cli": "kimi", "exit_code": 124,
        "backend_exit_code": 124, "result": "",
        "normalization_reason": "timed out; partial output preserved",
        "error": "Timeout after 1000ms",
    }
    calls = []

    def fake_execute(current, **_kwargs):
        calls.append(current.transport)
        return dict(primary)

    monkeypatch.setattr(run_subagent, "execute_agent", fake_execute)
    monkeypatch.setattr(run_subagent, "_regate_or_none", lambda *_args: None)
    monkeypatch.setattr(_builder, "supports_acp", lambda cli: cli == "kimi")

    result = run_subagent._dispatch_with_retries(invocation, _args())

    assert calls == ["subprocess"]
    assert result["fallback"] == {
        "to": "acp",
        "status": "not_attempted",
        "reason": "kimi_timeout_requires_explicit_opt_in",
    }
    assert any("disabled by default" in warning
               for warning in result.get("warnings", []))


def test_kimi_acp_fallback_requires_explicit_opt_in(monkeypatch):
    invocation = AgentInvocation(
        cli="kimi", prompt="test", cwd=os.getcwd(), permission="yolo")
    primary = {
        "status": "error", "cli": "kimi", "exit_code": 124,
        "backend_exit_code": 124, "result": "",
        "normalization_reason": "timed out; partial output preserved",
        "error": "Timeout after 1000ms",
    }
    calls = []

    def fake_execute(current, **_kwargs):
        calls.append(current.transport)
        if len(calls) == 1:
            return dict(primary)
        return {"status": "success", "cli": "kimi", "result": "recovered"}

    monkeypatch.setattr(run_subagent, "execute_agent", fake_execute)
    monkeypatch.setattr(run_subagent, "_regate_or_none", lambda *_args: None)
    monkeypatch.setattr(_builder, "supports_acp", lambda cli: cli == "kimi")

    result = run_subagent._dispatch_with_retries(
        invocation, _args(allow_kimi_acp_fallback=True))

    assert calls == ["subprocess", "acp"]
    assert result["status"] == "success"
    assert result["fallback"]["from"] == "subprocess"
    assert result["fallback"]["to"] == "acp"
