"""Provider-free transport budget and whole-message refusal facts."""
from __future__ import annotations

import pytest

import _transport_budget as budget
import _builder
import _executor


def argv_capability(*, platform):
    if platform == "nt":
        return {"kind": "argv", "max_utf16_units": 32_767}
    return {"kind": "argv", "max_single_argument_bytes": 131_072,
            "max_total_bytes": 2_000_000}


def test_windows_budget_counts_serialized_utf16_units_and_refuses_whole_operation():
    accepted = budget.evaluate_invocation(
        operation_id="attempt-1", content="🙂" * 100,
        command="codex.exe", args=["exec", "prompt"], env={}, platform="nt",
        capability=argv_capability(platform="nt"))
    assert accepted["status"] == "accepted"
    assert accepted["provider_contacted"] is False
    assert accepted["state_mutated"] is False

    blocked = budget.evaluate_invocation(
        operation_id="attempt-2", content="x" * 32_000,
        command="codex.exe", args=["exec", "x" * 32_000], env={}, platform="nt",
        capability={"kind": "argv", "max_utf16_units": 1000})
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "serialized_invocation_too_large"
    assert blocked["whole_message"] is True


def test_posix_budget_counts_encoded_bytes_and_environment():
    blocked = budget.evaluate_invocation(
        operation_id="attempt-posix", content="é" * 100,
        command="codex", args=["exec", "x"],
        env={"PROMPT_CONTEXT": "é" * 100}, platform="posix",
        capability={"kind": "argv", "max_single_argument_bytes": 1000,
                    "max_total_bytes": 100})
    assert blocked["status"] == "blocked"
    assert blocked["measurement"]["serialized_arg_env_bytes"] > 100


def test_posix_argument_boundary_matches_executor_nul_contract():
    limit = 131_072
    accepted = budget.evaluate_invocation(
        operation_id="attempt-posix-limit", content="x",
        command="codex", args=["x" * (limit - 1)], env={}, platform="posix",
        capability={"kind": "argv", "max_single_argument_bytes": limit,
                    "max_total_bytes": 2_000_000})
    assert accepted["status"] == "accepted"
    assert accepted["measurement"]["max_argument_bytes"] == limit - 1
    blocked = budget.evaluate_invocation(
        operation_id="attempt-posix-limit-1", content="x",
        command="codex", args=["x" * limit], env={}, platform="posix",
        capability={"kind": "argv", "max_single_argument_bytes": limit,
                    "max_total_bytes": 2_000_000})
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "serialized_invocation_too_large"


def test_private_attachment_path_accepts_64k_plus_and_refuses_only_oversize():
    capability = {"kind": "private_attachment", "max_attachment_bytes": 8 * 1024 * 1024}
    accepted = budget.evaluate_invocation(
        operation_id="attempt-attachment", content=b"x" * (64 * 1024 + 1),
        command="zcode", args=["--attach", "opaque-temp-ref"], env={}, platform="nt",
        capability=capability, attachment_bytes=64 * 1024 + 1)
    assert accepted["status"] == "accepted"
    assert accepted["measurement"]["attachment_bytes"] == 64 * 1024 + 1

    blocked = budget.evaluate_invocation(
        operation_id="attempt-attachment-2", content=b"x" * (8 * 1024 * 1024 + 1),
        command="zcode", args=["--attach", "opaque-temp-ref"], env={}, platform="nt",
        capability=capability, attachment_bytes=8 * 1024 * 1024 + 1)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "private_attachment_too_large"


def test_system_file_budget_is_distinct_from_private_attachment():
    accepted = budget.evaluate_invocation(
        operation_id="attempt-system-file", content=b"system\n" * 20,
        command="gemini", args=["--prompt", "p"], env={}, platform="nt",
        capability={"kind": "system_file", "max_attachment_bytes": 1024},
        attachment_bytes=140)
    assert accepted["status"] == "accepted"
    blocked = budget.evaluate_invocation(
        operation_id="attempt-system-file-2", content=b"x" * 1025,
        command="gemini", args=["--prompt", "p"], env={}, platform="nt",
        capability={"kind": "system_file", "max_attachment_bytes": 1024},
        attachment_bytes=1025)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "system_file_too_large"


def test_serialized_payload_budget_binds_exact_utf8_bytes_and_boundary():
    accepted = budget.evaluate_serialized_payload(
        operation_id="attempt-json", payload='λ🙂\n"quoted"',
        capability={"kind": "serialized", "platform": "nt",
                    "max_serialized_bytes": len('λ🙂\n"quoted"'.encode("utf-8"))},
        boundary="fixture-json")
    assert accepted["status"] == "accepted"
    assert accepted["measurement"]["serialized_bytes"] == accepted["content_bytes"]
    blocked = budget.evaluate_serialized_payload(
        operation_id="attempt-json-2", payload=b"x" * 10,
        capability={"kind": "serialized", "max_serialized_bytes": 9},
        boundary="fixture-json")
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "serialized_payload_too_large"
    assert blocked["provider_contacted"] is False


def test_transport_capability_binds_platform_and_rejects_mixed_shapes():
    with pytest.raises(budget.TransportBudgetError, match="platform"):
        budget.evaluate_invocation(
            operation_id="attempt-platform", content="x", command="codex",
            args=["exec"], env={}, platform="nt",
            capability={"kind": "argv", "platform": "posix",
                        "max_utf16_units": 32_767})
    with pytest.raises(budget.TransportBudgetError, match="unknown fields"):
        budget.evaluate_invocation(
            operation_id="attempt-extra", content="x", command="codex",
            args=["exec"], env={}, platform="posix",
            capability={"kind": "argv", "max_single_argument_bytes": 100,
                        "max_total_bytes": 100, "route": "unexpected"})
    with pytest.raises(budget.TransportBudgetError, match="attachment limits"):
        budget.evaluate_invocation(
            operation_id="attempt-mixed", content="x", command="codex",
            args=["--attach", "opaque"], env={}, platform="nt",
            capability={"kind": "argv", "max_utf16_units": 100,
                        "max_attachment_bytes": 100})


def test_result_is_bound_to_operation_and_content_digest():
    first = budget.evaluate_invocation(
        operation_id="attempt-a", content="same",
        command="codex", args=["exec", "same"], env={}, platform="posix",
        capability=argv_capability(platform="posix"))
    second = budget.evaluate_invocation(
        operation_id="attempt-b", content="changed",
        command="codex", args=["exec", "same"], env={}, platform="posix",
        capability=argv_capability(platform="posix"))
    assert first["operation_id"] != second["operation_id"]
    assert first["content_sha256"] != second["content_sha256"]
    assert budget.result_sha256(first) != budget.result_sha256(second)


def test_declared_budget_blocks_before_executor_spawn(monkeypatch):
    called = {"popen": 0}

    def forbidden(*_args, **_kwargs):
        called["popen"] += 1
        raise AssertionError("provider process was contacted after a transport refusal")

    monkeypatch.setattr(_executor.subprocess, "Popen", forbidden)
    monkeypatch.setattr(_executor, "_resolve_launch", lambda command, args: (command, args))
    invocation = _builder.AgentInvocation(
        cli="codex", prompt="x" * 4_000, cwd=".",
        transport_capability={"kind": "argv", "platform": "nt", "max_utf16_units": 1000})
    response = _executor.execute_agent(invocation, timeout_ms=30_000)
    assert called["popen"] == 0
    assert response["status"] == "error"
    assert response["error_kind"] == "transport_budget_exceeded"
    assert response["provider_contacted"] is False
    assert response["transport_budget"]["status"] == "blocked"


@pytest.mark.parametrize("kwargs", [
    {"operation_id": "bad id", "content": "x"},
    {"operation_id": "attempt", "content": "x", "capability": {"kind": "unknown"}},
], ids=['p001_case_001', 'p001_case_002'])
def test_malformed_capability_or_operation_refuses_provider_free(kwargs):
    defaults = {"operation_id": "attempt", "content": "x", "command": "cmd",
                "args": [], "env": {}, "platform": "nt",
                "capability": {"kind": "argv", "max_utf16_units": 32767}}
    defaults.update(kwargs)
    with pytest.raises(budget.TransportBudgetError):
        budget.evaluate_invocation(**defaults)
