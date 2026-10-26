"""Tests for the zero-socket HMAC-authenticated subprocess hook bridge (_hook_bridge.py)."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _builder  # noqa: E402
import _hook_bridge  # noqa: E402
import _job_control  # noqa: E402
import _jobs  # noqa: E402


def _setup_mock_job(tmp_path) -> tuple[str, str, str]:
    job_root = str(tmp_path / "jobs")
    _jobs.ensure_jobs_dir(job_root)
    job_id = _jobs.new_job_id()
    nonce = "deadbeef" * 8
    _jobs.write_prepared(
        job_root,
        job_id,
        nonce=nonce,
        agent="sol",
        prompt_sha256="a" * 64,
        cwd=str(tmp_path),
        flags={"agent": "sol"},
        summon={"version": "3.8.0", "script": "run_subagent.py"},
    )
    return job_root, job_id, nonce


def test_hook_bridge_host_secret_isolation_and_claude_args_preservation(tmp_path, monkeypatch):
    job_root, job_id, _ = _setup_mock_job(tmp_path)
    paths = _hook_bridge.prepare_claude_hook_settings(job_root, job_id)

    # Secret and hook settings live inside <job_root>/.secrets/ outside worker cwd
    assert os.path.isfile(paths["secret_file"])
    assert os.path.isfile(paths["settings_file"])
    assert os.path.isfile(paths["state_file"])

    monkeypatch.setenv("SUMMON_CLAUDE_HOOK_SETTINGS_FILE", paths["settings_file"])
    inv = _builder.AgentInvocation(
        cli="claude",
        model="claude-sonnet-4-6",
        permission="read-only",
        prompt="Inspect files",
        cwd=str(tmp_path),
        system_context="You are a reviewer.",
    )
    _, args, _ = _builder._build_claude_args(inv)
    # Must preserve BOTH --setting-sources "" AND --settings <settings_file>
    assert "--setting-sources" in args
    idx_ss = args.index("--setting-sources")
    assert args[idx_ss + 1] == ""
    assert "--settings" in args
    idx_set = args.index("--settings")
    assert args[idx_set + 1] == paths["settings_file"]


def test_hook_bridge_constrained_mode_denies_writes_unknown_and_mcp_tools(tmp_path):
    job_root, job_id, nonce = _setup_mock_job(tmp_path)
    _hook_bridge.prepare_claude_hook_settings(job_root, job_id)

    # Transition control log to healthy -> steering -> constrained (enforce mode)
    ctrl_path = _job_control.control_path(job_root, job_id)
    _job_control._append_breaker_command(
        ctrl_path,
        job_id,
        nonce,
        {
            "transitioned": True,
            "from_level": "healthy",
            "to_level": "steering",
            "mode": "enforce",
            "signals": ["tool_loop"],
        },
    )
    _job_control._append_breaker_command(
        ctrl_path,
        job_id,
        nonce,
        {
            "transitioned": True,
            "from_level": "steering",
            "to_level": "constrained",
            "mode": "enforce",
            "signals": ["no_progress"],
        },
    )

    # 1. Strict read-only tool ("Read") returns only the pending breaker steering
    # on first call, and {} on second call (NEVER permissionDecision="allow"!)
    out1 = _hook_bridge.evaluate_hook_event(
        job_root,
        job_id,
        event_name="PreToolUse",
        hook_input={"tool_name": "Read"},
    )
    assert "permissionDecision" not in out1.get("hookSpecificOutput", {})
    assert "Summon Operator Steering" in out1["hookSpecificOutput"]["additionalContext"]

    # Second Read call has no new steers -> returns {}
    out2 = _hook_bridge.evaluate_hook_event(
        job_root,
        job_id,
        event_name="PreToolUse",
        hook_input={"tool_name": "Read"},
    )
    assert out2 == {}

    # 2. Write, Bash, unknown, and mcp__* tools are all synchronously denied!
    for forbidden_tool in ("Write", "Edit", "MultiEdit", "Bash", "CustomUnknownTool", "mcp__github__create_issue"):
        res = _hook_bridge.evaluate_hook_event(
            job_root,
            job_id,
            event_name="PreToolUse",
            hook_input={"tool_name": forbidden_tool},
        )
        hook_out = res["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        assert "constrained" in hook_out["permissionDecisionReason"]


def test_hook_bridge_steer_queued_to_emitted_once_and_hmac_tamper_fails_closed(tmp_path):
    job_root, job_id, _ = _setup_mock_job(tmp_path)
    paths = _hook_bridge.prepare_claude_hook_settings(job_root, job_id)

    # Queue an operator steer command
    _job_control.queue_command(job_root, job_id, "steer", message="Focus on _mission.py only.")

    # First PostToolUse delivers additionalContext and records status='emitted'
    res1 = _hook_bridge.evaluate_hook_event(
        job_root,
        job_id,
        event_name="PostToolUse",
        hook_input={"tool_name": "Grep"},
    )
    assert "Focus on _mission.py only." in res1["hookSpecificOutput"]["additionalContext"]

    state = _hook_bridge.read_hook_state(job_root, job_id)
    assert state is not None
    assert state["emitted_generations"] == [1]
    assert state["emitted_commands"][0]["status"] == "emitted"

    # Second PostToolUse does NOT re-emit generation 1
    res2 = _hook_bridge.evaluate_hook_event(
        job_root,
        job_id,
        event_name="PostToolUse",
        hook_input={"tool_name": "Grep"},
    )
    assert res2 == {}

    # Tamper with hook_state.json -> read_hook_state raises HookBridgeError and PreToolUse denies
    with open(paths["state_file"], "r", encoding="utf-8") as fh:
        raw_state = json.load(fh)
    raw_state["emitted_generations"] = []
    with open(paths["state_file"], "w", encoding="utf-8") as fh:
        json.dump(raw_state, fh)

    with pytest.raises(_hook_bridge.HookBridgeError, match="HMAC verification"):
        _hook_bridge.read_hook_state(job_root, job_id)


def test_webfetch_denied_and_control_log_rollback_and_worker_root_containment(tmp_path):
    job_root, job_id, nonce = _setup_mock_job(tmp_path)
    # Rejects preparing hook settings when worker_cwd contains job_root/.secrets
    with pytest.raises(_hook_bridge.HookBridgeError, match="outside worker cwd"):
        _hook_bridge.prepare_claude_hook_settings(job_root, job_id, worker_cwd=str(tmp_path))

    ws = tmp_path / "worker_ws"
    ws.mkdir()
    _hook_bridge.prepare_claude_hook_settings(job_root, job_id, worker_cwd=str(ws))

    ctrl_path = _job_control.control_path(job_root, job_id)
    _job_control._append_breaker_command(
        ctrl_path,
        job_id,
        nonce,
        {
            "transitioned": True,
            "from_level": "healthy",
            "to_level": "steering",
            "mode": "enforce",
            "signals": ["tool_loop"],
        },
    )
    _job_control._append_breaker_command(
        ctrl_path,
        job_id,
        nonce,
        {
            "transitioned": True,
            "from_level": "steering",
            "to_level": "constrained",
            "mode": "enforce",
            "signals": ["no_progress"],
        },
    )

    # WebFetch and WebSearch must be denied in constrained mode (no data exfiltration)
    for web_tool in ("WebFetch", "WebSearch"):
        res = _hook_bridge.evaluate_hook_event(
            job_root,
            job_id,
            event_name="PreToolUse",
            hook_input={"tool_name": web_tool},
        )
        assert res["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Now delete control.jsonl to simulate rollback/truncation -> high-water mark fails closed!
    os.unlink(ctrl_path)
    res_rollback = _hook_bridge.evaluate_hook_event(
        job_root,
        job_id,
        event_name="PreToolUse",
        hook_input={"tool_name": "Read"},
    )
    assert res_rollback["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "rollback" in res_rollback["hookSpecificOutput"]["permissionDecisionReason"]
