"""Provider-free Claude launch-observation producer tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _builder import AgentInvocation, build_invocation_args
from _executor import (ProviderLaunchControl, ProviderLaunchRefusal,
                       execute_agent, _resolve_launch)
from _launch_observer import (ENTRYPOINT_MARKER, LaunchObservationError,
                              observe_claude_launch, resolve_claude_layout)


def _fake_claude(tmp_path: Path, version: str = "claude 9.9.9") -> tuple[str, Path]:
    script = tmp_path / "fake-claude.cmd"
    script.write_text(
        "@echo off\n"
        f"REM {ENTRYPOINT_MARKER}\n"
        f"if \"%1\"==\"--version\" (echo {version}&exit /b 0)\n"
        "echo {\"type\":\"result\",\"result\":\"STATUS: COMPLETE VERDICT: PASS HANDOFF: done\",\"session_id\":\"fixture\"}\n",
        encoding="utf-8",
    )
    return os.environ.get("COMSPEC", "cmd.exe"), script


def _invocation(tmp_path: Path, command: str, script: Path) -> AgentInvocation:
    return AgentInvocation(
        cli="claude", prompt="fixture", cwd=str(tmp_path), permission="read-only",
        profile_command=command,
        extra_args=("/d", "/s", "/c", str(script)),
        transport="subprocess",
    )


@pytest.mark.skipif(os.name != "nt", reason="entry-script fixture uses cmd.exe")
def test_builder_executor_produces_trusted_version_and_material(tmp_path):
    command, script = _fake_claude(tmp_path)
    inv = _invocation(tmp_path, command, script)
    built_command, built_args, env = build_invocation_args(inv, 5000)
    resolved_command, resolved_args = _resolve_launch(built_command, built_args)
    seen = []
    control = ProviderLaunchControl(
        before_launch=lambda evidence: seen.append(evidence),
        requires_launch_observation=True,
    )
    result = execute_agent(inv, timeout_ms=5000, launch_control=control)
    assert seen and seen[0]["launch_observation"]["external_cli_version"] == "claude/9.9.9"
    assert seen[0]["launch_observation"]["launch_material_sha256"]
    assert result.get("provider_contacted") is True
    assert result.get("_private_launch_observation", {}).get("external_cli_version") == "claude/9.9.9"
    assert resolved_command == built_command
    assert resolved_args == built_args
    assert env is None or isinstance(env, dict)


@pytest.mark.skipif(os.name != "nt", reason="entry-script fixture uses cmd.exe")
@pytest.mark.parametrize("version_output", ["not a Claude version", "2.1.263", "claude 1.2.3\nspoof"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_version_output_spoof_or_shape_is_rejected(tmp_path, version_output):
    command, script = _fake_claude(tmp_path, version_output)
    inv = _invocation(tmp_path, command, script)
    built_command, built_args, env = build_invocation_args(inv, 5000)
    resolved_command, resolved_args = _resolve_launch(built_command, built_args)
    with pytest.raises(LaunchObservationError):
        observe_claude_launch(resolved_command, resolved_args, str(tmp_path), env)


@pytest.mark.skipif(os.name != "nt", reason="entry-script fixture uses cmd.exe")
def test_unmarked_wrapper_and_missing_material_fail_closed(tmp_path):
    wrapper = tmp_path / "unknown.cmd"
    wrapper.write_text("@echo off\necho claude 1.2.3\n", encoding="utf-8")
    command = os.environ.get("COMSPEC", "cmd.exe")
    with pytest.raises(LaunchObservationError) as exc:
        resolve_claude_layout(command, ["/c", str(wrapper)])
    assert exc.value.kind == "launch_resolver_unsupported"


@pytest.mark.skipif(os.name != "nt", reason="entry-script fixture uses cmd.exe")
def test_material_replacement_between_measurement_and_spawn_is_refused(tmp_path):
    command, script = _fake_claude(tmp_path)
    inv = _invocation(tmp_path, command, script)
    seen = []

    def mutate_after_gate(_evidence):
        seen.append(True)
        script.write_text(
            "@echo off\nREM " + ENTRYPOINT_MARKER + "\necho changed\n",
            encoding="utf-8",
        )

    result = execute_agent(
        inv, timeout_ms=5000,
        launch_control=ProviderLaunchControl(
            before_launch=mutate_after_gate,
            requires_launch_observation=True,
        ),
    )
    assert seen
    assert result["status"] == "error"
    assert result["error_kind"] == "launch_observation_changed"
    assert result["provider_contacted"] is False
    assert result["attempt_status"] == "not_run"


@pytest.mark.skipif(os.name != "nt", reason="entry-script fixture uses cmd.exe")
def test_typed_policy_refusal_is_not_relabelled_as_cancellation(tmp_path):
    """A durable policy refusal must survive the generic launch-error handler."""
    command, script = _fake_claude(tmp_path)
    inv = _invocation(tmp_path, command, script)

    def refuse(_evidence):
        raise ProviderLaunchRefusal("resume_launch_qualification_revoked")

    result = execute_agent(
        inv,
        timeout_ms=5000,
        launch_control=ProviderLaunchControl(
            before_launch=refuse,
            requires_launch_observation=True,
        ),
    )

    assert result["status"] == "blocked"
    assert result["error_kind"] == "resume_launch_qualification_revoked"
    assert result["provider_contacted"] is False
    assert result["attempt_status"] == "not_run"
