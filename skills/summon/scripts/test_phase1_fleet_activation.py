"""Provider-inert tests for the unbound M3 activation contract."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from _builder import AgentInvocation
import _fleet_activation


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bindings(prompt: str = "review this") -> dict:
    return {
        "approval_id": _digest("approval"),
        "fleet_sha256": _digest("fleet"),
        "plan_sha256": _digest("plan"),
        "lane_sha256": _digest("lane"),
        "catalog_sha256": _digest("catalog"),
        "project_sha256": _digest("project"),
        "prompt_sha256": _fleet_activation._sha(prompt),
        "request_identity_sha256": _digest("request"),
    }


def _invocation(**changes) -> AgentInvocation:
    value = AgentInvocation(
        cli="codex", prompt="review this", cwd="C:\\workspace",
        permission="read-only", transport="subprocess", model="gpt-test",
        model_source="agent", model_exact_required=True,
        model_exact_source="agent", extra_args=("--json",),
        read_roots=("C:\\workspace",), output_contract="report",
    )
    return replace(value, **changes)


def _freeze(invocation=None):
    return _fleet_activation.freeze_activation_candidate(
        bindings=_bindings(), claim_id="1" * 32, seat="reviewer",
        agent_definition_sha256=_digest("definition"),
        invocation=invocation or _invocation())


def test_module_is_provider_inert_and_has_no_activation_surface():
    tree = ast.parse(Path(_fleet_activation.__file__).read_text(encoding="utf-8"))
    imports, calls = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
    assert not imports.intersection({
        "_executor", "_background", "_resolver", "_apibackend", "subprocess",
        "socket", "requests", "urllib", "httpx",
    })
    assert not calls.intersection({
        "Popen", "run", "call", "execute_agent", "reserve_dispatch",
        "claim_provider_launch",
    })
    assert not hasattr(_fleet_activation, "launch")
    assert not hasattr(_fleet_activation, "authorize")


def test_contract_is_unbound_non_authority_and_private_values_are_only_digests():
    invocation = _invocation(
        prompt="review this", cwd="C:\\private\\client",
        profile_env={"TOKEN": "private-capability-handle"},
        system_context="private system context")
    contract = _freeze(invocation)
    assert _fleet_activation.validate_activation_contract(contract) == contract
    assert contract["authorization"] == "evidence_only"
    assert contract["activation_allowed"] is False
    assert contract["candidate_eligible"] is True
    assert contract["binding_state"] == "unbound"
    assert contract["final_launch"] is None
    serialized = json.dumps(contract, sort_keys=True)
    for private in ("client", "private-capability-handle", "private system context"):
        assert private not in serialized


def test_api_credential_fingerprint_is_private_structure_not_exported_value():
    fingerprint = "a" * 32
    contract = _freeze(_invocation(api_key_fingerprint=fingerprint))
    serialized = json.dumps(contract, sort_keys=True)
    assert fingerprint not in serialized
    projected = _fleet_activation._invocation_projection(
        _invocation(api_key_fingerprint=fingerprint))
    assert projected["api_key_fingerprint"] == {
        "present": True, "kind": "scalar", "items": 1}


@pytest.mark.parametrize("change", [
    {"permission": "safe-edit"},
    {"model": "gpt-other"},
])
def test_public_structural_change_cannot_be_certified(change):
    original = _invocation()
    contract = _freeze(original)
    changed = replace(original, **change)
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("definition"),
        current_request_identity_sha256=_digest("request"),
        invocation=changed)


def test_definition_change_breaks_match():
    contract = _freeze()
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("other-definition"),
        current_request_identity_sha256=_digest("request"),
        invocation=_invocation())


def test_billing_is_derived_and_rechecked(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SUBAGENTS_ALLOW_OPENAI_KEY", raising=False)
    contract = _freeze()
    assert contract["billing"]["class"] == "subscription"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("SUBAGENTS_ALLOW_OPENAI_KEY", "1")
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("definition"),
        current_request_identity_sha256=_digest("request"),
        invocation=_invocation())
    assert _fleet_activation.derive_billing_class(_invocation())["class"] == "payg"


@pytest.mark.parametrize("model", ["claude-fable-5", "claude-fable-5-1"])
def test_plan_dependent_and_unsupported_billing_remain_unknown(monkeypatch, model):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fable = _invocation(cli="claude", model=model)
    assert _fleet_activation.derive_billing_class(fable)["class"] == "unknown"
    assert _fleet_activation.derive_billing_class(
        _invocation(cli="opencode"))["class"] == "unknown"


def test_unknown_billing_is_explicitly_ineligible(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "C:\\custom-codex")
    contract = _freeze()
    assert contract["billing"]["class"] == "unknown"
    assert contract["billing"]["candidate_eligible"] is False
    assert contract["candidate_eligible"] is False


@pytest.mark.parametrize("name", ["CLI_API_KEY", "CURSOR_API_KEY"])
def test_cursor_api_keys_never_project_subscription(monkeypatch, name):
    monkeypatch.setenv(name, "rotating-test-value")
    result = _fleet_activation.derive_billing_class(
        _invocation(cli="cursor-agent"))
    assert result["class"] == "payg"
    assert result["credential_binding_required"] is True


def test_cursor_subscription_account_is_ineligible_until_privately_attested(monkeypatch):
    for name in ("CLI_API_KEY", "CURSOR_API_KEY", "CURSOR_API_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    result = _fleet_activation.derive_billing_class(
        _invocation(cli="cursor-agent"))
    assert result["class"] == "unknown"
    assert result["candidate_eligible"] is False


@pytest.mark.parametrize("name", [
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_BASE_URL",
])
def test_claude_custom_provider_routes_are_unknown(monkeypatch, name):
    monkeypatch.setenv(name, "configured")
    assert _fleet_activation.derive_billing_class(
        _invocation(cli="claude"))["class"] == "unknown"


def test_claude_profile_environment_controls_effective_billing(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    payg = _invocation(
        cli="claude", profile_env={
            "CLAUDE_CONFIG_DIR": str(tmp_path),
            "ANTHROPIC_API_KEY": "synthetic-test-key"})
    custom = _invocation(
        cli="claude", profile_env={
            "CLAUDE_CONFIG_DIR": str(tmp_path),
            "ANTHROPIC_BASE_URL": "https://example.invalid"})
    assert _fleet_activation.derive_billing_class(payg)["class"] == "payg"
    assert _fleet_activation.derive_billing_class(custom)["class"] == "unknown"


def test_claude_profile_settings_control_effective_billing(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    profile = tmp_path / "claude-profile"
    profile.mkdir()
    invocation = _invocation(
        cli="claude", profile_env={"CLAUDE_CONFIG_DIR": str(profile)})
    (profile / "settings.json").write_text(
        '{"env":{"ANTHROPIC_API_KEY":"synthetic-test-key"}}', encoding="utf-8")
    assert _fleet_activation.derive_billing_class(invocation)["class"] == "payg"
    (profile / "settings.json").write_text(
        '{"env":{"ANTHROPIC_BASE_URL":"https://example.invalid"}}', encoding="utf-8")
    assert _fleet_activation.derive_billing_class(invocation)["class"] == "unknown"


def test_named_claude_profile_binds_project_settings_billing_and_bytes(
        tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    profile = tmp_path / "claude-profile"
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    profile.mkdir()
    invocation = _invocation(
        cli="claude", cwd=str(project),
        profile_env={"CLAUDE_CONFIG_DIR": str(profile)})
    settings = project / ".claude" / "settings.json"
    settings.write_text(
        '{"env":{"ANTHROPIC_API_KEY":"synthetic-project-key"}}',
        encoding="utf-8")
    assert _fleet_activation.derive_billing_class(invocation)["class"] == "payg"
    kwargs = {
        "master_key": b"k" * 32, "approval_id": "a" * 64,
        "claim_id": "b" * 32, "contract_sha256": "c" * 64,
        "agent_definition_sha256": "d" * 64,
        "current_request_identity_sha256": "e" * 64,
    }
    first = _fleet_activation.private_binding_hmac(invocation, **kwargs)
    settings.write_text(
        '{"env":{"ANTHROPIC_API_KEY":"rotated-project-key"}}',
        encoding="utf-8")
    second = _fleet_activation.private_binding_hmac(invocation, **kwargs)
    assert first != second


def test_arbitrary_api_key_environment_value_is_keyed(monkeypatch):
    invocation = _invocation(api_key_env="PRIVATE_VENDOR_KEY")
    kwargs = {
        "master_key": b"k" * 32, "approval_id": "a" * 64,
        "claim_id": "b" * 32, "contract_sha256": "c" * 64,
        "agent_definition_sha256": "d" * 64,
        "current_request_identity_sha256": "e" * 64,
    }
    monkeypatch.setenv("PRIVATE_VENDOR_KEY", "first-low-entropy-value")
    first = _fleet_activation.private_binding_hmac(invocation, **kwargs)
    monkeypatch.setenv("PRIVATE_VENDOR_KEY", "second-low-entropy-value")
    second = _fleet_activation.private_binding_hmac(invocation, **kwargs)
    assert first != second


@pytest.mark.parametrize("name", [
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_GENAI_USE_VERTEXAI",
])
def test_gemini_vertex_and_adc_routes_are_unknown(monkeypatch, name):
    monkeypatch.setenv(name, "configured")
    assert _fleet_activation.derive_billing_class(
        _invocation(cli="gemini"))["class"] == "unknown"
    with pytest.raises(_fleet_activation.FleetActivationError):
        _freeze(_invocation(cli="gemini"))


def test_codex_custom_provider_args_are_unknown():
    value = _invocation(extra_args=("-c", "model_provider=custom"))
    assert _fleet_activation.derive_billing_class(value)["class"] == "unknown"


@pytest.mark.parametrize("selector", [
    ("--profile", "custom"), ("-p", "custom"),
    ("--profile=custom",), ("-p=custom",),
])
def test_codex_profile_selector_is_unknown_until_profile_is_privately_bound(selector):
    billing = _fleet_activation.derive_billing_class(
        _invocation(extra_args=selector))
    assert billing["class"] == "unknown"
    assert billing["source"] == "custom_provider_configuration"
    assert billing["candidate_eligible"] is False


def test_codex_model_provider_config_is_unknown(tmp_path, monkeypatch):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model_provider = "custom"\n', encoding="utf-8")
    monkeypatch.setattr(_fleet_activation.os.path, "expanduser", lambda value: str(tmp_path))
    assert _fleet_activation.derive_billing_class(_invocation())["class"] == "unknown"


def test_codex_standard_config_uses_available_toml_parser(tmp_path, monkeypatch):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    monkeypatch.setattr(_fleet_activation.os.path, "expanduser", lambda value: str(tmp_path))
    assert _fleet_activation.tomllib is not None
    assert _fleet_activation._codex_custom_provider_configured({}) is False


def test_codex_missing_toml_parser_fails_closed(tmp_path, monkeypatch):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    monkeypatch.setattr(_fleet_activation.os.path, "expanduser", lambda value: str(tmp_path))
    monkeypatch.setattr(_fleet_activation, "tomllib", None)
    billing = _fleet_activation.derive_billing_class(_invocation())
    assert billing["class"] == "unknown"
    assert billing["candidate_eligible"] is False


def test_fresh_request_identity_is_required_and_slice_a_never_certifies():
    contract = _freeze()
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("definition"),
        current_request_identity_sha256=_digest("different-request"),
        invocation=_invocation())
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("definition"),
        current_request_identity_sha256=_digest("request"), invocation=_invocation())


def test_same_class_credential_rotation_is_not_certifiable(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "first-test-value")
    invocation = _invocation(cli="cursor-agent")
    contract = _freeze(invocation)
    assert "first-test-value" not in json.dumps(contract, sort_keys=True)
    monkeypatch.setenv("CURSOR_API_KEY", "second-test-value")
    assert contract["billing"]["class"] == "payg"
    assert not _fleet_activation.matches_activation_candidate(
        contract, agent_definition_sha256=_digest("definition"),
        current_request_identity_sha256=_digest("request"), invocation=invocation)


def test_private_environment_and_profile_values_are_not_plain_hash_inputs():
    first = _invocation(
        profile_env={"TOKEN": "low-entropy-one"},
        profile="private-profile-one", profile_command="C:\\one\\runner.exe")
    second = _invocation(
        profile_env={"TOKEN": "low-entropy-two"},
        profile="private-profile-two", profile_command="D:\\two\\runner.exe")
    assert (_fleet_activation.invocation_structural_sha256(first)
            == _fleet_activation.invocation_structural_sha256(second))
    contract = _freeze(first)
    serialized = json.dumps(contract, sort_keys=True)
    for private in ("low-entropy-one", "private-profile-one", "runner.exe"):
        assert private not in serialized


@pytest.mark.parametrize("change", [
    {"transport": "acp"}, {"resume_id": "session"},
    {"attempt_id": "2" * 32}, {"attempt_kind": "retry"},
    {"attempt_ordinal": 2}, {"parent_attempt_id": "3" * 32},
    {"cli": "kimi"}, {"cli": "opencode"}, {"cli": "agy"}, {"cli": "gemini"},
])
def test_secondary_and_side_effectful_paths_are_structurally_refused(change):
    with pytest.raises(_fleet_activation.FleetActivationError):
        _freeze(_invocation(**change))


def test_prompt_binding_and_contract_forgery_fail_closed():
    with pytest.raises(_fleet_activation.FleetActivationError, match="prompt"):
        _fleet_activation.freeze_activation_candidate(
            bindings=_bindings("different"), claim_id="1" * 32,
            seat="reviewer", agent_definition_sha256=_digest("definition"),
            invocation=_invocation())
    contract = _freeze()
    forged = json.loads(json.dumps(contract))
    forged["attempt_policy"]["retry"] = True
    with pytest.raises(_fleet_activation.FleetActivationError, match="digest"):
        _fleet_activation.validate_activation_contract(forged)

    resealed = json.loads(json.dumps(contract))
    resealed["billing"]["source"] = "C:\\private\\billing"
    payload = {key: value for key, value in resealed.items()
               if key not in {"schema", "sha256"}}
    resealed["sha256"] = hashlib.sha256(
        _fleet_activation.SCHEMA.encode("ascii") + b"\0"
        + _fleet_activation._canonical(payload)).hexdigest()
    with pytest.raises(_fleet_activation.FleetActivationError, match="billing"):
        _fleet_activation.validate_activation_contract(resealed)


@pytest.mark.parametrize("mutation", ["cyclic", "deep", "oversize"])
def test_contract_validation_is_bounded_before_digesting(mutation):
    contract = _freeze()
    if mutation == "cyclic":
        contract["billing"]["source"] = contract
    elif mutation == "deep":
        value = "x"
        for _ in range(30):
            value = [value]
        contract["billing"]["source"] = value
    else:
        contract["billing"]["source"] = "x" * (
            _fleet_activation.MAX_CANONICAL_BYTES + 1)
    with pytest.raises(_fleet_activation.FleetActivationError):
        _fleet_activation.validate_activation_contract(contract)
