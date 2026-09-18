"""Provider-free acceptance of adapter-owned payload ceilings.

These tests exercise the final serialized request boundary for the API, ArkCLI
and Gemini system-file paths. They deliberately replace launch/opening hooks
with local refusals; no provider, account or network is contacted.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import _apibackend
import _arkcli_backend
import _executor
from _builder import AgentInvocation


def test_openai_compat_oversize_body_refuses_before_opener(monkeypatch):
    called = {"open": 0}

    def forbidden(*_args, **_kwargs):
        called["open"] += 1
        raise AssertionError("HTTP opener was reached after payload refusal")

    monkeypatch.setattr(_apibackend, "_opener", forbidden)
    result = _apibackend._do_request(
        "https://example.invalid/v1", "fixture-model", "system",
        "x" * (8 * 1024 * 1024), "fixture-key", 5_000, "openai-compat")
    assert called["open"] == 0
    assert result["status"] == "error"
    assert result["error_kind"] == "transport_budget_exceeded"
    assert result["attempts"] == 0
    assert result["attempt_status"] == "not_run"
    assert result["provider_contacted"] is False
    assert result["transport_budget"]["boundary"] == "openai_chat_completions"


def test_arkcli_oversize_final_argv_refuses_before_subprocess(monkeypatch):
    called = {"run": 0}

    def forbidden(*_args, **_kwargs):
        called["run"] += 1
        raise AssertionError("arkcli was launched after argv refusal")

    monkeypatch.setattr(_arkcli_backend.shutil, "which", lambda _name: "arkcli")
    monkeypatch.setattr(_arkcli_backend, "_arkcli_cmd", lambda: ["arkcli"])
    monkeypatch.setattr(_arkcli_backend.subprocess, "run", forbidden)
    inv = SimpleNamespace(model="fixture-model", prompt="x" * 2_100_000,
                          system_context="", resume_id=None)
    result = _arkcli_backend.call(inv, 5_000)
    assert called["run"] == 0
    assert result["status"] == "error"
    assert result["error_kind"] == "transport_budget_exceeded"
    assert result["attempts"] == 0
    assert result["provider_contacted"] is False
    assert result["transport_budget"]["operation_id"]


def test_gemini_system_file_budget_refuses_before_spawn(tmp_path, monkeypatch):
    system_file = tmp_path / "GEMINI_SYSTEM.md"
    system_file.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    called = {"popen": 0}

    def forbidden(*_args, **_kwargs):
        called["popen"] += 1
        raise AssertionError("Gemini child was launched after system-file refusal")

    monkeypatch.setattr(_executor, "build_invocation_args",
                        lambda _inv, _timeout: (
                            sys.executable, ["synthetic-gemini"],
                            {"GEMINI_SYSTEM_MD": str(system_file)}))
    monkeypatch.setattr(_executor, "_resolve_launch",
                        lambda command, args: (command, args))
    monkeypatch.setattr(_executor.subprocess, "Popen", forbidden)
    invocation = AgentInvocation(
        cli="gemini", prompt="small prompt", cwd=str(tmp_path),
        permission="yolo", agent_file=str(system_file), transport="subprocess")
    result = _executor.execute_agent(invocation, timeout_ms=5_000)
    assert called["popen"] == 0
    assert result["status"] == "error"
    assert result["error_kind"] == "transport_budget_exceeded"
    assert result["attempts"] == 0
    assert result["provider_contacted"] is False
    assert result["transport_budget"]["reason"] == "system_file_too_large"
