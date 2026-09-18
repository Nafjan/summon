"""Provider-free fleet callback compatibility, not live launch qualification.

The ten-field envelope mirrors the executor's reachable final enrichment. The
observation-bearing envelope exercises schema/control compatibility only: the
fleet control does not request observations. The real builder-evidence helper,
control, fleet callback, authority checks and synthetic ledger run in process;
the executor itself and provider process creation are never invoked.
"""

from dataclasses import replace
import hashlib
import json
import re

import pytest

import _executor
import _fleet_dispatch
import _fleet_runtime
from test_phase1_fleet_dispatch import (
    approved, _activation_material, _reserve_activation,
)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _runtime(context, monkeypatch, *, prompt="review this"):
    invocation, candidate, definition_sha, request_sha = _activation_material(
        context, prompt=prompt)
    reservation = _reserve_activation(
        context, invocation=invocation, activation_candidate=candidate,
        agent_definition_sha256=definition_sha,
        current_request_identity_sha256=request_sha)
    runtime = _fleet_runtime.FleetLaunchRuntime(
        reservation=reservation, fleet=context["fleet"], plan=context["plan"],
        catalog=context["catalog"], lane_name="review", cwd=context["cwd"],
        data_boundary={"boundary": "local_sanitized", "proof": "operator_attested",
                       "evidence_sha256": candidate["bindings"]["prompt_sha256"]},
        invocation=invocation, activation_candidate=candidate,
        agent_definition_sha256=definition_sha, request_identity_sha256=request_sha,
        fleet_path="synthetic discovery replaced", agents_dir="synthetic discovery replaced",
        strict_agents_dir=True)
    # Only declarative discovery is replaced. The real callback still reopens
    # and authenticates the approval/ledger and revalidates this frozen context.
    monkeypatch.setattr(runtime, "_current_context", lambda: (
        context["fleet"], context["plan"], context["catalog"]))
    return runtime


def _evidence(invocation, tmp_path, *, shape="current"):
    command = tmp_path / "inert-launch-material.bin"
    command.write_bytes(b"synthetic content; never executed")
    result = _executor._subprocess_launch_evidence(
        str(command), ["--print", invocation.prompt], invocation.cwd,
        {"SYNTHETIC_INPUT": "synthetic value"}, backend=invocation.cli,
        include_launch_observation=(shape == "observed"))
    if shape != "historical":
        # These are the exact two final-launch additions in execute(). A fleet
        # invocation has no explicit attempt id; the executor generates one.
        attempt_id = getattr(invocation, "attempt_id", None)
        if not isinstance(attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            attempt_id = "a" * 32
        result["dispatch_payload_sha256"] = _sha(invocation.prompt)
        result["attempt_id_sha256"] = _sha(attempt_id)
    return result


def _assert_reserved(runtime):
    claim = _fleet_dispatch.get_claim(runtime.reservation)
    assert claim["phase"] == "reserved"
    assert claim["contact_slot_consumed"] is False
    assert "final_launch_sha256" not in claim["activation"]


@pytest.mark.parametrize("shape", ["historical", "current", "observed"], ids=[
    "historical_eight", "current_ten", "current_observed_eleven",
])
def test_callback_commits_complete_evidence_once_and_keeps_it_private(
        approved, monkeypatch, tmp_path, shape):
    runtime = _runtime(approved, monkeypatch, prompt='review "café"\nsecond line')
    evidence = _evidence(runtime.invocation, tmp_path, shape=shape)
    assert len(evidence) == {"historical": 8, "current": 10, "observed": 11}[shape]
    control = runtime.control()
    assert control.requires_launch_observation is False
    control.before_provider_launch(evidence)
    claim = _fleet_dispatch.get_claim(runtime.reservation)
    assert claim["phase"] == "provider_launch_claimed"
    assert claim["contact_slot_consumed"] is True
    assert claim["provider_contacted"] is None
    assert claim["activation"]["final_launch_sha256"] == _fleet_dispatch._digest(evidence)
    public = json.dumps(_fleet_dispatch.public_receipt(runtime.reservation))
    for field in evidence:
        if field.endswith("_sha256") or field == "launch_observation":
            assert field not in public
    with pytest.raises(_executor.ProviderLaunchError, match="single-use"):
        control.before_provider_launch(evidence)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as duplicate:
        runtime.control().before_provider_launch(evidence)
    assert duplicate.value.kind == "fleet_dispatch_cas_conflict"


@pytest.mark.parametrize("mutation", [
    "missing_payload", "missing_attempt", "unknown", "null_observation",
    "superseded_observation", "wrong_prompt", "invalid_attempt", "invalid_payload",
], ids=[
    "missing_payload", "missing_attempt", "unknown_field", "null_observation",
    "superseded_observation", "wrong_prompt", "invalid_attempt", "invalid_payload",
])
def test_invalid_current_evidence_does_not_consume_capacity(
        approved, monkeypatch, tmp_path, mutation):
    runtime = _runtime(approved, monkeypatch)
    evidence = _evidence(runtime.invocation, tmp_path, shape="observed")
    if mutation == "missing_payload":
        del evidence["dispatch_payload_sha256"]
    elif mutation == "missing_attempt":
        del evidence["attempt_id_sha256"]
    elif mutation == "unknown":
        evidence["unrecognized_proof"] = "not authority"
    elif mutation == "null_observation":
        evidence["launch_observation"] = None
    elif mutation == "superseded_observation":
        del evidence["dispatch_payload_sha256"], evidence["attempt_id_sha256"]
    elif mutation == "wrong_prompt":
        evidence["dispatch_payload_sha256"] = _sha("different prompt")
    else:
        field = "attempt_id_sha256" if mutation == "invalid_attempt" else "dispatch_payload_sha256"
        evidence[field] = "not a digest"
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        runtime.control().before_provider_launch(evidence)
    assert refused.value.kind == "fleet_activation_launch_evidence_invalid"
    _assert_reserved(runtime)


@pytest.mark.parametrize("field", [
    "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256", "env_sha256",
    "dispatch_payload_sha256", "attempt_id_sha256",
], ids=[
    "command_digest", "argv_digest", "cwd_digest", "env_names_digest", "env_digest",
    "payload_digest", "attempt_digest",
])
@pytest.mark.parametrize("invalid", [False, "A" * 64, "a" * 63], ids=[
    "boolean_value", "uppercase_digest", "short_digest",
])
def test_every_included_envelope_digest_is_validated(tmp_path, field, invalid):
    invocation = _executor.AgentInvocation(cli="claude", prompt="synthetic", cwd=str(tmp_path))
    evidence = _evidence(invocation, tmp_path)
    evidence[field] = invalid
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        _fleet_dispatch._validate_launch_evidence(evidence, invocation=invocation, route={"backend": "claude"})
    assert refused.value.kind == "fleet_activation_launch_evidence_invalid"


@pytest.mark.parametrize("field,value", [
    ("command_sha256", "b" * 64), ("argv_sha256", "b" * 64),
    ("cwd_sha256", "b" * 64), ("env_names_sha256", "b" * 64),
    ("env_sha256", "b" * 64), ("backend", "codex"), ("transport", "acp"),
    ("schema", "other/v1"), ("unknown", "not evidence"),
    ("executable_path_sha256", "bad"), ("executable_sha256", "bad"),
    ("launch_material_sha256", "bad"), ("registry_digest", "bad"),
    ("executable_size", True), ("registry_generation", True),
    ("observed_at_ns", True), ("observation_nonce", "bad"),
], ids=[
    "command_binding", "argv_binding", "cwd_binding", "env_names_binding",
    "env_binding", "backend_binding", "transport_binding", "schema_identity",
    "unknown_field", "executable_path_digest", "executable_digest",
    "launch_material_digest", "registry_digest", "boolean_executable_size",
    "boolean_registry_generation", "boolean_observation_time", "invalid_nonce",
])
def test_observation_shape_and_every_shared_binding_are_checked(tmp_path, field, value):
    invocation = _executor.AgentInvocation(cli="claude", prompt="synthetic", cwd=str(tmp_path))
    evidence = _evidence(invocation, tmp_path, shape="observed")
    evidence["launch_observation"][field] = value
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        _fleet_dispatch._validate_launch_evidence(evidence, invocation=invocation, route={"backend": "claude"})
    assert refused.value.kind == "fleet_activation_launch_evidence_invalid"


@pytest.mark.parametrize("attempt_id", [None, "invalid", "A" * 32, "a" * 31, False, "a" * 32], ids=[
    "absent_id", "malformed_id", "uppercase_id", "short_id", "boolean_id", "valid_explicit_id",
])
def test_validator_matches_executor_attempt_id_validity_and_fallback(tmp_path, attempt_id):
    # Non-None ids remain ineligible for fleet activation. This checks only the
    # validator's correspondence with execute(), never expanded fleet authority.
    invocation = _executor.AgentInvocation(
        cli="claude", prompt="synthetic", cwd=str(tmp_path), attempt_id=attempt_id)
    evidence = _evidence(invocation, tmp_path)
    assert _fleet_dispatch._validate_launch_evidence(
        evidence, invocation=invocation, route={"backend": "claude"}) == evidence
    if attempt_id == "a" * 32:
        evidence["attempt_id_sha256"] = _sha("b" * 32)
        with pytest.raises(_fleet_dispatch.FleetDispatchError):
            _fleet_dispatch._validate_launch_evidence(
                evidence, invocation=invocation, route={"backend": "claude"})


@pytest.mark.parametrize("fence", ["cancelled", "deadline_reached"], ids=[
    "cancelled", "deadline_reached",
])
def test_control_owner_and_deadline_fences_precede_durable_claim(
        approved, monkeypatch, tmp_path, fence):
    runtime = _runtime(approved, monkeypatch)
    control = _executor.ProviderLaunchControl(before_launch=runtime._claim, **{fence: lambda: True})
    with pytest.raises(_executor.ProviderLaunchError):
        control.before_provider_launch(_evidence(runtime.invocation, tmp_path))
    _assert_reserved(runtime)


def test_changed_frozen_context_refuses_before_current_evidence_can_claim(
        approved, monkeypatch, tmp_path):
    runtime = _runtime(approved, monkeypatch)
    runtime.invocation = replace(runtime.invocation, prompt="changed after reservation")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        runtime.control().before_provider_launch(_evidence(runtime.invocation, tmp_path))
    assert refused.value.kind == "fleet_activation_binding_changed"
    _assert_reserved(runtime)
