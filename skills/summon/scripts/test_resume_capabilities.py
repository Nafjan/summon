"""Provider-inert contract tests for the resume capability registry."""

from __future__ import annotations

import ast
import json
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
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005', 'p001_case_006', 'p001_case_007', 'p001_case_008', 'p001_case_009', 'p001_case_010', 'p001_case_011', 'p001_case_012', 'p001_case_013'])
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
    assert imported <= {"__future__", "hashlib", "json"}


@pytest.mark.parametrize("operation", ["resume", "steer"], ids=['p002_case_001', 'p002_case_002'])
def test_v2_rows_are_exact_and_never_grant_launch_permission(operation):
    for cli, transport in capabilities._CAPABILITIES:
        row = capabilities.resume_capability_v2(operation, cli, transport)
        assert row["schema"] == "summon.resume-capabilities/v2"
        assert row["registry_generation"] == capabilities.REGISTRY_GENERATION
        assert row["registry_digest"] == capabilities.REGISTRY_DIGEST
        assert row["launch_permission"] == "not_granted"
        assert row["adapter_version_scope"] == "summon-executor/3.4.0"
        assert row["external_cli_version_scope"] == "not_declared"
        assert row["qualification"] == {
            "provenance": "source_registry_only",
            "status": "unqualified",
            "executable_binding": "unavailable",
            "provider_receipt": "unavailable",
            "external_cli_version": "unavailable",
            "expiry": {"state": "unavailable", "expires_at": None},
        }
        assert capabilities.is_exact_capability_v2(row)
        assert set(row["evidence_requirements"]) == {
            "exact_identity", "owner_fence", "fresh_handle", "provider_receipt",
        }


def test_v2_unknown_inputs_fail_closed_without_echoing_private_values():
    secret_like = "C:/private/session/private-capability-handle"
    row = capabilities.resume_capability_v2(secret_like, secret_like, secret_like)
    assert row["operation"] == "unknown"
    assert row["backend"] == "unknown"
    assert row["transport"] == "unknown"
    assert row["resume_state"] == "unsupported"
    assert row["resume_reason"] == "unknown_operation"
    assert row["launch_permission"] == "not_granted"
    assert secret_like not in repr(row)
    assert capabilities.is_exact_capability_v2(row) is False


def test_v2_rejects_forged_scope_permission_and_extra_fields():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    forged_permission = dict(row, launch_permission="granted")
    forged_scope = dict(row, adapter_version_scope="*")
    extra = dict(row, session_id="private")
    forged_evidence = dict(row, evidence_requirements=dict(
        row["evidence_requirements"], provider_receipt=False))
    assert capabilities.is_exact_capability_v2(forged_permission) is False
    assert capabilities.is_exact_capability_v2(forged_scope) is False
    assert capabilities.is_exact_capability_v2(extra) is False
    assert capabilities.is_exact_capability_v2(forged_evidence) is False


@pytest.mark.parametrize("field, value", [
    ("registry_generation", True),
    ("registry_generation", 1.0),
    ("operation", []),
    ("backend", {}),
    ("evidence_requirements", {"exact_identity": 1}),
    ("qualification", {"status": True}),
], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006'])
def test_v2_rejects_malformed_primitives_and_nested_shapes(field, value):
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    row[field] = value
    assert capabilities.is_exact_capability_v2(row) is False


def test_v2_digest_binds_all_semantic_fields():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    material = capabilities._v2_registry_material()
    matching = next(item for item in material
                    if item["operation"] == row["operation"]
                    and item["backend"] == row["backend"]
                    and item["transport"] == row["transport"])
    for field in (
            "schema", "steering_mode", "handle_kind", "evidence_requirements",
            "qualification", "launch_permission", "adapter_version_scope",
            "external_cli_version_scope"):
        assert matching[field] == row[field]


def test_v2_serialization_is_deterministic_utf8_and_exactly_represented():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    first = capabilities.serialize_capability_v2(row)
    second = capabilities.serialize_capability_v2(dict(row))
    assert first == second
    assert first.decode("utf-8") == json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert json.loads(first.decode("utf-8")) == row


def test_v2_registry_scope_match_is_consistency_only_not_authority():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    observation = {
        "schema": row["schema"],
        "registry_generation": row["registry_generation"],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version_scope": row["adapter_version_scope"],
        "external_cli_version_scope": row["external_cli_version_scope"],
    }
    assert capabilities.matches_registry_scope(row, observation) is True
    assert capabilities.matches_registry_scope(
        row, dict(observation, registry_generation=999)) is False
    assert capabilities.matches_registry_scope(
        row, dict(observation, registry_generation=True)) is False
    assert capabilities.matches_registry_scope(
        row, dict(observation, adapter=[])) is False
    # A matching observation still exposes no launch grant or delivery receipt.
    assert row["launch_permission"] == "not_granted"
    assert row["steering_mode"] == "queued_for_resume"


def test_v2_launch_scope_match_is_precontact_only_and_preserves_v1_projection():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    observation = {
        "schema": capabilities.LAUNCH_SCOPE_SCHEMA,
        "registry_generation": row["registry_generation"],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version_scope"],
        "external_cli_version": row["external_cli_version_scope"],
    }
    result = capabilities.compare_launch_scope(row, observation)
    assert result == {
        "schema": capabilities.LAUNCH_SCOPE_SCHEMA,
        "status": "match",
        "reason": "scope_match",
        "contact_allowed": False,
        "provider_contacted": False,
        "launch_permission": capabilities.V2_LAUNCH_PERMISSION,
    }
    assert capabilities.resume_capability("claude", "subprocess")["schema"] == capabilities.SCHEMA


@pytest.mark.parametrize(("cli", "transport"), [
    ("agy", "subprocess"),
    ("openai-compat", "api"),
], ids=['p004_case_001', 'p004_case_002'])
def test_v2_launch_scope_refuses_exact_but_unsupported_capability(cli, transport):
    row = capabilities.resume_capability_v2("resume", cli, transport)
    observation = {
        "schema": capabilities.LAUNCH_SCOPE_SCHEMA,
        "registry_generation": row["registry_generation"],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version_scope"],
        "external_cli_version": row["external_cli_version_scope"],
    }
    result = capabilities.compare_launch_scope(row, observation)
    assert result["status"] == "refused"
    assert result["reason"] == "capability_unsupported"
    assert result["contact_allowed"] is False
    assert result["provider_contacted"] is False


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("registry_generation", 99, "registry_scope_stale"),
    ("registry_digest", "sha256:" + "b" * 64, "registry_scope_stale"),
    ("adapter_version", "summon-executor/old", "adapter_version_mismatch"),
    ("external_cli_version", "cli/old", "external_cli_version_mismatch"),
], ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004'])
def test_v2_launch_scope_refuses_stale_facts_before_contact(field, value, reason):
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    observation = {
        "schema": capabilities.LAUNCH_SCOPE_SCHEMA,
        "registry_generation": row["registry_generation"],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version_scope"],
        "external_cli_version": row["external_cli_version_scope"],
    }
    observation[field] = value
    result = capabilities.compare_launch_scope(row, observation)
    assert result["status"] == "refused"
    assert result["reason"] == reason
    assert result["contact_allowed"] is False
    assert result["provider_contacted"] is False


def test_v2_launch_scope_rejects_malformed_values_without_raw_type_errors():
    row = capabilities.resume_capability_v2("resume", "claude", "subprocess")
    assert capabilities.compare_launch_scope(row, [])["reason"] == "observation_malformed"
    assert capabilities.compare_launch_scope([], {})["reason"] == "capability_malformed"
    malformed = {
        "schema": capabilities.LAUNCH_SCOPE_SCHEMA,
        "registry_generation": [],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version_scope"],
        "external_cli_version": row["external_cli_version_scope"],
    }
    assert capabilities.compare_launch_scope(row, malformed)["reason"] == "observation_malformed"
