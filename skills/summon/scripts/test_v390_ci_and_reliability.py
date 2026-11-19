"""Regression tests for Summon v3.9.0 CI resolver fix, opt-in contract repair,
output_contract prompt fidelity, and graceful natural process exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _background
import _builder
import _cli
import _executor
import _job_resume
import _schema
from _spawn import run_flags
import run_subagent as rs


# --- Pillar 0: Lazy _builder import after monkeypatching shutil.which ---------


def test_lazy_builder_import_honors_monkeypatched_shutil_which_for_doctor(tmp_path):
    """Simulate test_doctor_rejects_nonzero_version_probe in a bare runner subprocess."""
    script = """
import json
import os
import shutil
import sys

scripts = sys.argv[1]
sys.path.insert(0, scripts)

import _doctor

def fake_which(name):
    return f"/fake/{name}" if name in ("claude", "codex", "gemini", "cursor-agent", "agy") else None

def fake_run(args, *a, **kw):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "bad install"
    return Proc()

_doctor.shutil.which = fake_which
shutil.which = fake_which
_doctor.subprocess.run = fake_run

rep = _doctor.doctor()
codex = rep["backends"]["codex"]
agy = rep["backends"]["agy"]
print(json.dumps({"codex": codex, "agy": agy}))
"""
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "missing_local")
    env["USERPROFILE"] = str(tmp_path / "missing_profile")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(SCRIPTS)],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        **run_flags(),
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["codex"]["found"] is True
    assert data["codex"]["verified"] is False
    assert data["codex"]["path"] == "/fake/codex"
    assert data["agy"]["found"] is True
    assert data["agy"]["verified"] is False
    assert data["agy"]["path"] == "/fake/agy"


# --- Pillar 1: Opt-in --contract-repair & output_contract gating -------------


def _dispatch_args(**overrides):
    base = {
        "agent": None,
        "cli": "claude",
        "prompt": "do work",
        "cwd": str(SCRIPTS),
        "agents_dir": None,
        "timeout": 30000,
        "debug_dir": None,
        "retries": 0,
        "transient_retries": False,
        "retry_nonretryable": False,
        "contract_repair": False,
        "no_contract_repair": False,
        "json_schema": None,
        "gate_with": None,
        "gate_timeout": None,
        "max_permission": None,
        "allow_credit": False,
        "allow_payg": False,
        "allow_text_only": False,
        "require_tools": False,
        "strict_agents_dir": False,
        "enable_roles": False,
        "isolated_lane": False,
        "allow_tool_credentials": False,
        "profile": None,
        "resume": None,
        "resume_profile": None,
        "transport": None,
        "worktree": None,
        "text": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cli_contract_repair_defaults_opt_in_and_forwards():
    parser = _cli.build_parser("3.9.0", 1)
    default_args = parser.parse_args(["--agent", "coder", "--prompt", "hi"])
    assert default_args.contract_repair is False
    assert default_args.no_contract_repair is False

    opt_in = parser.parse_args(["--agent", "coder", "--prompt", "hi", "--contract-repair"])
    assert opt_in.contract_repair is True
    assert _background._submission_accounting_grants(opt_in)["contract_repair"] == 1
    assert "--contract-repair" in _background.child_argv(opt_in, "job.json")

    overridden = parser.parse_args(
        ["--agent", "coder", "--prompt", "hi", "--contract-repair", "--no-contract-repair"]
    )
    assert overridden.contract_repair is True
    assert overridden.no_contract_repair is True
    assert _background._submission_accounting_grants(overridden)["contract_repair"] == 0
    child = _background.child_argv(overridden, "job.json")
    assert "--no-contract-repair" in child
    assert "--contract-repair" not in child


def test_governed_resume_rejects_contract_repair_flag(tmp_path):
    source = {
        "agent": {
            "requested": "coder",
            "resolved": "coder",
            "file": None,
            "agents_dir": str(tmp_path),
            "definition_sha256": "d" * 64,
        },
        "workspace": {"path": str(SCRIPTS)},
        "backend": {
            "profile": "p1",
            "profile_path_sha256": "p" * 64,
            "profile_registry_sha256": "r" * 64,
            "profile_command_sha256": "c" * 64,
        },
        "model": {"targeted": "claude-sonnet-5-5"},
        "authority": {
            "effort": "high",
            "read_roots": [],
            "isolated_lane": False,
            "allow_tool_credentials": False,
            "strict_agents_dir": False,
            "enable_roles": False,
            "allow_text_only": False,
            "require_tools": False,
        },
    }
    context = _job_resume.ChildContext(
        root=str(tmp_path),
        source_job_id="1" * 32,
        successor_job_id="2" * 32,
        claim_id="3" * 32,
        claim_sha256="4" * 64,
        source=source,
        prompt="follow up",
        resume_handle="sess-1",
        effective_permission="safe-edit",
        gate_with=None,
        gate_timeout_ms=0,
        allow_credit=False,
        allow_payg=False,
        timeout_ms=30000,
        max_runtime_ms=0,
        launch_binding=None,
        launch_qualification=None,
    )
    inv = _builder.AgentInvocation(
        cli="claude",
        prompt="follow up",
        cwd=str(SCRIPTS),
        resume_id="sess-1",
        profile="p1",
        model="claude-sonnet-5-5",
        model_exact_required=True,
        permission="safe-edit",
        effort="high",
    )
    receipt = {
        "agent_def": {"sha256": "d" * 64},
        "profile": {
            "name": "p1",
            "path_sha256": "p" * 64,
            "registry_sha256": "r" * 64,
            "command_sha256": "c" * 64,
        },
    }
    args = _dispatch_args(
        agent="coder",
        _resolved_agent="coder",
        agents_dir=str(tmp_path),
        effort="high",
        max_runtime=0,
        gate_timeout=0,
        no_contract_repair=True,
        no_acp_fallback=True,
        contract_repair=True,
    )
    with pytest.raises(_job_resume.ResumeError, match="resolved successor differs"):
        _job_resume.validate_loaded_invocation(context, inv, args, receipt)


def test_approved_fleet_lane_rejects_contract_repair_and_new_single_flags(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "run_subagent.py"),
            "dispatch", "--lane", "lane1",
            "--fleet-file", str(tmp_path / "fleet.json"),
            "--fleet-approval-id", "a" * 64,
            "--fleet-data-proof", "operator_attested",
            "--prompt-file", str(prompt_file), "--cwd", str(tmp_path),
            "--contract-repair", "--text", "--patch-out", str(tmp_path / "p.patch"),
            "--files-scope", "src/",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        **run_flags(),
    )
    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["error_kind"] == "fleet_activation_usage_invalid"
    for flag in ("--contract-repair", "--files-scope", "--patch-out", "--text"):
        assert flag in payload["error"]


def test_freeform_text_schema_omit_phantom_final_report_and_suspect(tmp_path):
    free_inv = _builder.AgentInvocation(
        cli="claude", prompt="say hi", cwd=str(tmp_path),
        output_contract="freeform",
    )
    sys_prompt = _builder._claude_system_prompt(free_inv)
    assert "Final report" not in sys_prompt
    assert "HANDOFF" not in sys_prompt
    res_prompt = _builder._resume_prompt(
        _builder.AgentInvocation(
            cli="claude", prompt="again", cwd=str(tmp_path),
            resume_id="12345678-1234-1234-1234-123456789012",
            output_contract="freeform",
        )
    )
    assert "Final report" not in res_prompt

    text_inv = _builder.AgentInvocation(
        cli="claude", prompt="stream hi", cwd=str(tmp_path), output_contract="text",
    )
    assert "Final report" not in _builder._claude_system_prompt(text_inv)

    schema_inv = _builder.AgentInvocation(
        cli="claude", prompt="json hi", cwd=str(tmp_path), output_contract="schema",
    )
    assert "Final report" not in _builder._claude_system_prompt(schema_inv)

    rep_inv = _builder.AgentInvocation(
        cli="claude", prompt="report", cwd=str(tmp_path), output_contract="report",
    )
    assert "Final report" in _builder._claude_system_prompt(rep_inv)

    # _enrich marks report_contract missing report as suspect, but never freeform/text
    for contract_name in ("freeform", "text"):
        env = _executor._enrich(
            {"status": "success", "cli": "claude", "result": "plain prose answer", "exit_code": 0},
            None,
            output_contract=contract_name,
        )
        assert env["output_contract"] == contract_name
        assert env["report_source"] == contract_name
        assert "suspect" not in env

    rep_env = _executor._enrich(
        {"status": "success", "cli": "claude", "result": "missing bookends", "exit_code": 0},
        None,
        output_contract="report",
    )
    assert rep_env["suspect"] is True
    assert rep_env["report_source"] == "raw"


def test_main_contract_repair_gate_default_off_vs_opt_in(tmp_path):
    """Verify opt-in contract repair triggers only when --contract-repair is enabled."""
    calls = []

    def fake_exec(inv, **kw):
        calls.append(inv.attempt_kind)
        if len(calls) == 1:
            return {
                "status": "success", "execution_status": "success", "exit_code": 0,
                "cli": "claude", "result": "answer without bookends",
                "report_ok": False, "suspect": True,
                "resume": {"cli": "claude", "session_id": "12345678-1234-1234-1234-123456789012"},
                "attempts": 1, "attempt_id": inv.attempt_id,
            }
        return {
            "status": "success", "execution_status": "success", "exit_code": 0,
            "cli": "claude",
            "result": "STATUS: DONE\nSUMMARY: ok\nHANDOFF: none\nFOLLOW-UP: none\n",
            "report": {"status": "DONE", "summary": "ok", "handoff": "none", "follow_up": "none"},
            "report_ok": True,
            "resume": {"cli": "claude", "session_id": "12345678-1234-1234-1234-123456789012"},
            "attempts": 1, "attempt_id": inv.attempt_id,
        }

    orig_result = fake_exec(_builder.AgentInvocation(cli="claude", prompt="x", cwd=str(tmp_path)))
    assert len(calls) == 1

    inv = _builder.AgentInvocation(cli="claude", prompt="x", cwd=str(tmp_path), output_contract="report")
    args_on = _dispatch_args(contract_repair=True, no_contract_repair=False)
    with mock.patch.object(rs, "execute_agent", side_effect=fake_exec):
        repaired = rs._apply_contract_repair(dict(orig_result), inv, args_on)
    assert repaired["contract_repaired"] is True
    assert repaired["attempts"] == 2
    assert len(calls) == 2

    assert rs._effective_decision_view(inv, _dispatch_args(contract_repair=False))["authority"]["corrective_allowed"] is False
    assert rs._effective_decision_view(inv, _dispatch_args(contract_repair=True))["authority"]["corrective_allowed"] is True
    assert rs._effective_decision_view(inv, _dispatch_args(contract_repair=True, no_contract_repair=True))["authority"]["corrective_allowed"] is False


# --- Pillar 2: Natural Terminal Exit Grace Window ----------------------------


class _FakeStreamStdout:
    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self._idx = 0

    def readline(self) -> str:
        if self._idx < len(self._lines):
            ln = self._lines[self._idx]
            self._idx += 1
            return ln
        return ""

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, lines: list[str], *, exit_after_terminate: bool = False):
        self.stdout = _FakeStreamStdout(lines)
        self.returncode = None if exit_after_terminate else 0
        self.terminated = 0
        self.killed = 0
        self._done = not exit_after_terminate

    def poll(self):
        return 0 if self._done else None

    def terminate(self) -> None:
        self.terminated += 1
        self._done = True
        self.returncode = 15

    def kill(self) -> None:
        self.killed += 1
        self._done = True
        self.returncode = 9

    def communicate(self, timeout=None):
        return "", ""


def test_natural_exit_grace_fast_child_exits_zero_without_terminate():
    assert _executor._NATURAL_TERMINAL_EXIT_GRACE_SEC == 2.5
    proc = _FakeProcess([
        json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-1111-1111-1111-111111111111"}) + "\n",
        json.dumps({"type": "result", "subtype": "success", "result": "STATUS: DONE\nSUMMARY: ok\nHANDOFF: none\nFOLLOW-UP: none\n"}) + "\n",
    ])
    resp = _executor._drive_process(proc, "claude", timeout_ms=5000, parse_stream=True)
    assert proc.terminated == 0
    assert resp["status"] == "success"
    assert resp["raw_backend_exit_code"] == 0


def test_natural_exit_grace_hanging_child_terminated_after_grace(monkeypatch):
    monkeypatch.setattr(_executor, "_NATURAL_TERMINAL_EXIT_GRACE_SEC", 0.05)

    class _HangingPostEOFProcess(_FakeProcess):
        def __init__(self):
            super().__init__(
                [
                    json.dumps({"type": "system", "subtype": "init", "session_id": "22222222-2222-2222-2222-222222222222"}) + "\n",
                    json.dumps({"type": "result", "subtype": "success", "result": "STATUS: DONE\nSUMMARY: ok\nHANDOFF: none\nFOLLOW-UP: none\n"}) + "\n",
                ],
                exit_after_terminate=True,
            )

    proc = _HangingPostEOFProcess()
    resp = _executor._drive_process(proc, "claude", timeout_ms=5000, parse_stream=True)
    assert proc.terminated == 1
    assert resp["status"] == "success"
    assert resp["raw_backend_exit_code"] == 15
