"""Synthetic driver and producer-to-envelope regressions; no provider execution.

MemoryProcess replaces only the OS child. Kimi wire files are synthetic,
child-writable observations, never authoritative provider/model receipts.
"""
import io
import json
import sys
import time

import pytest

import _builder
import _executor
import _job_control
import _jobobj


class MemoryProcess:
    def __init__(self, events):
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.returncode = 0
        self.terminated = 0
        self.communicated = 0

    def terminate(self):
        self.terminated += 1

    def poll(self):
        return self.returncode

    def communicate(self, *, timeout):
        self.communicated += 1
        return "", ""


def _terminal_process():
    return MemoryProcess([{
        "type": "result", "subtype": "success", "is_error": False,
        "result": "STATUS: DONE\nsynthetic terminal", "session_id": "synthetic-session",
        "usage": {"output_tokens": 7},
    }])


def _adaptive(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    for name, value in {
        "SUMMON_ADAPTIVE_TIMEOUT": "1",
        "SUMMON_JOB_CONTROL_FILE": str(tmp_path / "control.json"),
        "SUMMON_JOB_HEARTBEAT_FILE": str(heartbeat),
        "SUMMON_JOB_ID": "a" * 32, "SUMMON_JOB_NONCE": "synthetic-only",
        "SUMMON_MAX_RUNTIME_MS": "120000", "SUMMON_JOB_STARTED_AT": str(time.time()),
    }.items():
        monkeypatch.setenv(name, value)
    return heartbeat


@pytest.mark.parametrize("failure", ["none", "midstream", "final"],
                         ids=["successful_control", "midstream_io", "final_io"])
def test_observational_write_preserves_driver_terminal(monkeypatch, tmp_path, failure):
    heartbeat = _adaptive(monkeypatch, tmp_path)
    actual_write = _job_control._write_control_json
    failed_phases = []

    def write(path, value):
        phase = value["liveness"]["phase"]
        if ((failure == "midstream" and phase != "terminal")
                or (failure == "final" and phase == "terminal")):
            failed_phases.append(phase)
            raise OSError("synthetic private publisher detail")
        return actual_write(path, value)

    monkeypatch.setattr(_job_control, "_write_control_json", write)
    process = _terminal_process()
    response = _executor._drive_process(process, "claude", 60000, attempt_id="b" * 32)
    assert response["status"] == "success"
    assert response["result"] == "STATUS: DONE\nsynthetic terminal"
    assert response["usage"]["output_tokens"] == 7
    assert response["liveness"]["phase"] == "terminal"
    assert response["runtime_control"]["heartbeat_unavailable"] is (failure != "none")
    assert response["runtime_control"]["hard_runtime_ms"] == 120000
    assert "synthetic private publisher detail" not in json.dumps(response)
    assert bool(failed_phases) is (failure != "none")
    if failure != "final":
        assert json.loads(heartbeat.read_text(encoding="utf-8"))["liveness"]["phase"] == "terminal"
    else:
        assert failed_phases == ["terminal"]
    assert process.terminated == 1 and process.communicated == 1


@pytest.mark.parametrize("boundary", ["durable_control", "heartbeat_auth"],
                         ids=["durable_control_strict", "heartbeat_auth_strict"])
def test_nonobservational_driver_failure_remains_strict(monkeypatch, tmp_path, boundary):
    _adaptive(monkeypatch, tmp_path)
    process = _terminal_process()
    reaped = []
    monkeypatch.setattr(_executor, "_kill_tree", lambda child: reaped.append(child))
    if boundary == "durable_control":
        def fail_read(*args, **kwargs):
            raise OSError("synthetic durable control failure")
        monkeypatch.setattr(_job_control._jobs, "_read", fail_read)
    else:
        def fail_auth(*args, **kwargs):
            raise ValueError("synthetic authentication failure")
        monkeypatch.setattr(_job_control, "heartbeat_auth", fail_auth)
        # Authentication also remains strict at the final publication boundary.
        with pytest.raises(ValueError, match="synthetic authentication failure"):
            _executor._drive_process(process, "claude", 60000)
        assert reaped == [process] and process.communicated == 1
        return
    response = _executor._drive_process(process, "claude", 60000)
    assert response["status"] == "error"
    assert response["result"] != "STATUS: DONE\nsynthetic terminal"
    assert response["runtime_control"]["heartbeat_unavailable"] is False
    assert reaped == [process] and process.communicated == 1


@pytest.mark.parametrize("conflict,exact,output", [
    (True, False, 7), (True, True, 7), (True, False, 0), (False, False, 7),
], ids=["conflict_nonexact", "conflict_exact", "conflict_zero_output", "ordinary_inference"])
def test_kimi_actual_conflict_producer_reaches_final_envelope(
        monkeypatch, tmp_path, conflict, exact, output):
    profile = tmp_path / "profile"
    target = "kimi-code/k3"
    event = {"role": "assistant", "content": "synthetic answer",
             "usage": {"output_tokens": output}}
    if conflict:
        event["model"] = target
    process = MemoryProcess([event])
    launched = []
    synced = []
    closed = []

    def build(inv, *args):
        if conflict:
            wire = profile / "sessions/fixture/agents/main/wire.jsonl"
            wire.parent.mkdir(parents=True)
            now = int(time.time() * 1000)
            events = [
                {"type": "usage.record", "model": "kimi-code/other",
                 "usage": {"output": 4}, "usageScope": "turn", "time": now},
                {"type": "turn.ended", "reason": "completed", "time": now},
            ]
            wire.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        return sys.executable, (), {"KIMI_CODE_HOME": str(profile)}

    def spawn(*args, **kwargs):
        launched.append(True)
        return process

    monkeypatch.setattr(_executor, "build_invocation_args", build)
    monkeypatch.setattr(_executor.subprocess, "Popen", spawn)
    # Native executable observation and OS Job Object ownership are replaced
    # only at their external boundaries; these tests claim neither proof.
    monkeypatch.setattr(_executor, "_subprocess_launch_evidence", lambda *a, **kw: {})
    monkeypatch.setattr(_jobobj, "attach", lambda child: None)
    monkeypatch.setattr(_jobobj, "close", lambda child: closed.append(child))
    monkeypatch.setattr(_builder, "sync_kimi_profile_credentials", lambda path: synced.append(path))
    monkeypatch.setattr("_receipt.workspace_snapshot", lambda cwd: {"coverage": "none"})
    monkeypatch.setattr("_receipt.workspace_evidence", lambda *args: {})
    invocation = _builder.AgentInvocation(
        cli="kimi", prompt="synthetic review", model=target, cwd=str(tmp_path),
        permission="yolo", model_exact_required=exact, model_exact_source="test" if exact else None)
    response = _executor.execute_agent(invocation, timeout_ms=5000)
    assert launched == [True] and closed == [process] and synced == [str(profile)]
    assert process.communicated == 1
    assert response["usage"]["output_tokens"] == output
    assert response["model"]["targeted"] == target
    assert response["named_model_verified"] is False
    assert response["model_match"] is None
    if conflict:
        assert response["model"]["evidence_source"] == "kimi_model_evidence_conflict"
        assert response["kimi_runtime_evidence"]["usage_records"] == 1
        assert response["kimi_runtime_evidence"]["turn_completed"] is True
        assert response["model"]["served"] is None
        assert response["served_model_evidence"] == "absent"
        assert any("different models" in warning for warning in response["warnings"])
    else:
        assert response["model"]["served"] == target
        assert response["served_model_evidence"] == "inferred"
    if exact:
        assert response["status"] == "blocked"
        assert response["error_kind"] == "served_model_unverified"
        assert response["result_usable"] is False and response["retryable"] is False
    else:
        assert response["status"] == "success"
        assert response["result"] == "synthetic answer"
