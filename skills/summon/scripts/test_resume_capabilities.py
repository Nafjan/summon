"""Provider-inert contract tests for the resume capability registry."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import _resume_capabilities as capabilities


@pytest.mark.parametrize(("cli", "transport", "state", "reason"), [
    ("claude", "subprocess", "certified", "claude_subprocess_governed_lane"),
    ("codex", "subprocess", "candidate", "provider_receipt_required"),
    ("cursor-agent", "subprocess", "candidate", "provider_receipt_required"),
    ("opencode", "subprocess", "candidate", "provider_receipt_required"),
    ("zcode", "subprocess", "candidate", "installed_resume_smoke_required"),
    ("agy", "subprocess", "unsupported", "agy_profile_continuity_unreliable"),
    ("gemini", "subprocess", "unsupported", "stable_session_resume_unavailable"),
    ("kimi", "subprocess", "unsupported", "stable_session_id_unavailable"),
    ("cursor-agent", "acp", "unsupported", "acp_session_namespace_not_resumable"),
    ("gemini", "acp", "unsupported", "acp_session_namespace_not_resumable"),
    ("kimi", "acp", "unsupported", "acp_session_namespace_not_resumable"),
    ("openai-compat", "api", "unsupported", "stateless_api_transport"),
    ("arkcli", "api", "unsupported", "response_id_not_captured"),
])
def test_each_known_backend_transport_has_a_exact_safe_capability(
        cli, transport, state, reason):
    row = capabilities.resume_capability(cli, transport)
    assert row == {
        "schema": "summon.resume-capabilities/v1",
        "backend": cli,
        "transport": transport,
        "resume_state": state,
        "resume_reason": reason,
        "steering_mode": "queued_for_resume",
        "live_steering_acknowledged": False,
    }
    assert capabilities.is_exact_capability(row)
    assert capabilities.governed_resume_supported(cli, transport) == (cli == "claude")


def test_unknown_or_wrong_direction_values_fail_closed_without_echoing_input():
    secret_like = "C:/private/session/private-capability-handle"
    for cli, transport in ((secret_like, "subprocess"),
                           ("claude", "api"),
                           (None, None)):
        row = capabilities.resume_capability(cli, transport)
        assert row["backend"] in {"claude", "unknown"}
        assert row["transport"] in {"subprocess", "api", "unknown"}
        assert row["resume_state"] == "unsupported"
        assert row["resume_reason"] == "unknown_backend_or_transport"
        assert secret_like not in repr(row)
        assert capabilities.governed_resume_supported(cli, transport) is False


def test_capability_schema_rejects_extra_fields_and_forged_claims():
    row = capabilities.resume_capability("claude", "subprocess")
    forged = dict(row, live_steering_acknowledged=True)
    extra = dict(row, session_id="private")
    assert capabilities.is_exact_capability(forged) is False
    assert capabilities.is_exact_capability(extra) is False


def test_registry_is_pure_and_has_no_adapter_or_provider_imports():
    path = Path(capabilities.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__"}
