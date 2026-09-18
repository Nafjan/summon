"""Explicit transport refusals cannot authorize a secondary route."""

import os

import pytest

from _builder import AgentInvocation
import _executor as executor


def _capability():
    if os.name == "nt":
        return {"kind": "argv", "platform": "nt", "max_utf16_units": 1000}
    return {"kind": "argv", "platform": "posix",
            "max_single_argument_bytes": 1000, "max_total_bytes": 1000}


def _intercept_secondary(monkeypatch):
    original = executor.execute_agent
    secondary = []

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("transport preflight reached a process launch")

    def intercepted(invocation, **_kwargs):
        secondary.append(invocation.transport)
        return {"status": "error", "error_kind": "synthetic_secondary_interception",
                "provider_contacted": False}

    monkeypatch.setenv("SUMMON_ACP_FALLBACK", "1")
    monkeypatch.setattr(executor.subprocess, "Popen", unexpected_spawn)
    monkeypatch.setattr(executor, "_resolve_launch", lambda command, args: (command, args))
    monkeypatch.setattr(executor, "execute_agent", intercepted)
    return original, secondary


@pytest.mark.parametrize("malformed", [False, True], ids=['p001_case_001', 'p001_case_002'])
def test_explicit_capability_refusal_never_selects_acp(tmp_path, monkeypatch, malformed):
    invoke, secondary = _intercept_secondary(monkeypatch)
    capability = _capability()
    if malformed:
        capability["unexpected_field"] = "unsupported"
    invocation = AgentInvocation(
        cli="gemini", prompt="x" * 4000, cwd=str(tmp_path),
        transport="subprocess", transport_capability=capability,
    )
    response = invoke(invocation, timeout_ms=5000)
    assert secondary == [], "explicit transport refusal selected another transport"
    assert response["error_kind"] == (
        "transport_capability_invalid" if malformed else "transport_budget_exceeded")
    assert response["provider_contacted"] is False
    assert response.get("attempt_status") == "not_run"
    assert "fallback" not in response


def test_legacy_argv_length_fallback_remains_separate(tmp_path, monkeypatch):
    invoke, secondary = _intercept_secondary(monkeypatch)
    monkeypatch.setattr(executor, "argv_length_error",
                        lambda *_args, **_kwargs: "synthetic legacy OS argv limit")
    invocation = AgentInvocation(
        cli="gemini", prompt="synthetic prompt", cwd=str(tmp_path),
        transport="subprocess",
    )
    response = invoke(invocation, timeout_ms=5000)
    assert secondary == ["acp"]
    assert response["fallback"]["from"] == "subprocess"
    assert response["fallback"]["to"] == "acp"
    assert response["provider_contacted"] is False


def test_explicit_capability_platform_mismatch_refuses_before_secondary_route(tmp_path, monkeypatch):
    invoke, secondary = _intercept_secondary(monkeypatch)
    capability = _capability()
    capability["platform"] = "posix" if os.name == "nt" else "nt"
    invocation = AgentInvocation(
        cli="gemini", prompt="synthetic prompt", cwd=str(tmp_path),
        transport="subprocess", transport_capability=capability,
    )
    response = invoke(invocation, timeout_ms=5000)
    assert secondary == []
    assert response["error_kind"] == "transport_capability_invalid"
    assert response["provider_contacted"] is False
    assert response.get("attempt_status") == "not_run"
    assert "fallback" not in response
