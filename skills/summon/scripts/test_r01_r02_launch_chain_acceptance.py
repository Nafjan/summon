"""Provider-free R01/R02 producer-to-consumer acceptance coverage.

These tests deliberately cross the real local process boundary with a marked
``.cmd`` fixture.  The child is not a provider: it only emits a deterministic
JSON result.  The important assertion is that the observation produced by the
executor is the same authenticated host identity later consumed by the
governed continuation gate and provider controls.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import _job_continuation as continuation
import _jobs
import _job_resume as resume
import _launch_qualification as qualification
from _executor import ProviderLaunchControl, execute_agent
from test_job_continuation import _fixture, _publish_result
from test_launch_observer import _fake_claude, _invocation


def _source_from_real_observation(tmp_path: Path):
    """Create a certified source using an observation from ``execute_agent``."""
    root, job_id, job_file, fixture_invocation, args, result = _fixture(tmp_path)
    command, script = _fake_claude(tmp_path)
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            '"session_id":"fixture"',
            '"model":"claude-opus-5","session_id":"fixture"'),
        encoding="utf-8",
    )
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    profile_path = str(profile_dir.resolve())
    profile_path_sha = __import__("hashlib").sha256(
        profile_path.encode("utf-8")).hexdigest()[:32]
    invocation = replace(
        fixture_invocation,
        profile_command=command,
        extra_args=("/d", "/s", "/c", str(script)),
        profile="review-profile",
        profile_env={"CLAUDE_CONFIG_DIR": profile_path},
        model="claude-opus-5",
        model_exact_required=True,
        model_exact_source="agent",
    )
    result["profile"] = {
        "name": "review-profile",
        "cli": "claude",
        "path_sha256": profile_path_sha,
        "registry_sha256": "a" * 32,
        "command_sha256": __import__("hashlib").sha256(
            str(command).encode("utf-8")).hexdigest()[:32],
    }

    observed = []
    producer = execute_agent(
        invocation,
        timeout_ms=5000,
        launch_control=ProviderLaunchControl(
            before_launch=observed.append,
            requires_launch_observation=True,
        ),
    )
    assert producer["provider_contacted"] is True
    assert producer.get("_private_launch_observation")
    evidence = observed[0]
    observation = evidence["launch_observation"]
    assert observation["external_cli_version"] == "claude/9.9.9"

    # The fixture's terminal receipt supplies the certified model/session and
    # the real executor supplies the private host observation.  This mirrors
    # run_subagent._emit: the marker is consumed for the private sidecar and
    # never published in the public result.
    result["_private_launch_observation"] = observation
    continuation.write_private_source(job_file, result, invocation, args)
    result.pop("_private_launch_observation", None)
    _publish_result(job_file, result)

    record = _jobs.read_json(_jobs.record_path(root, job_id))
    material_contract = qualification.material_contract_for(
        backend=observation["backend"],
        transport=observation["transport"],
        adapter=observation["adapter"],
        adapter_version=observation["adapter_version"],
    )
    assert material_contract
    qualification_fields = dict(
        observation, operation="resume", material_contract=material_contract)
    qualified = qualification.issue(
        source_job_id=job_id,
        source_attempt_id=record["attempt_id"],
        operation="resume",
        backend=observation["backend"],
        transport=observation["transport"],
        registry_generation=observation["registry_generation"],
        registry_digest=observation["registry_digest"],
        adapter=observation["adapter"],
        adapter_version=observation["adapter_version"],
        external_cli_version=observation["external_cli_version"],
        material_contract=material_contract,
        executable_sha256=observation["executable_sha256"],
        launch_material_sha256=observation["launch_material_sha256"],
        expires_at=__import__("time").time() + 600,
        revocation_id=qualification.revocation_id_for(qualification_fields),
        source_nonce=record["nonce"],
    )
    qualification.write(root, job_id, qualified)
    return root, job_id, invocation, script


def _prepare_child(tmp_path: Path, *, gate: bool):
    root, job_id, invocation, script = _source_from_real_observation(tmp_path)
    reservation = resume.reserve_request(
        root,
        job_id,
        message="continue the bounded local fixture",
        request_id="a" * 32,
        gate_with="reviewer" if gate else None,
    )
    source = reservation.source
    _jobs.write_prepared(
        root,
        reservation.successor_job_id,
        nonce="successor-nonce",
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id,
        resume_lineage={
            "source_job_id": job_id,
            "request_id": reservation.request_id,
            "claim_id": reservation.claim_id,
            "request_sha256": reservation.request_sha256,
        },
    )
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    resume.claim_transition(
        root,
        job_id,
        reservation.claim_id,
        field="parent_phase",
        expected="successor_prepared",
        target="child_launch_claimed",
    )
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"],
    )
    assert context is not None
    child = replace(
        invocation,
        cwd=source["workspace"]["path"],
        prompt=context.prompt,
        resume_id=context.resume_handle,
        attempt_id=reservation.successor_job_id,
    )
    return root, job_id, reservation, context, child, script


@pytest.mark.skipif(__import__("os").name != "nt",
                    reason="fixture uses a Windows cmd wrapper")
def test_real_observation_flows_through_gate_then_main(tmp_path):
    root, job_id, reservation, context, child, _script = _prepare_child(
        tmp_path, gate=True)

    gate_result = execute_agent(
        child,
        timeout_ms=5000,
        launch_control=resume.provider_launch_control(context, gate=True),
    )
    assert gate_result["provider_contacted"] is True
    assert gate_result["status"] == "success", gate_result
    claim = resume.get_claim(reservation)
    assert claim["gate_phase"] in {"spawned", "reaped"}
    assert claim["gate_launch_binding"] is not None
    assert claim["gate_launch_binding"] == context.launch_binding
    assert claim["provider_launch_binding"] is None

    resume.mark_gate_terminal(
        context,
        approved=True,
        decision_sha256="b" * 64,
    )
    main_result = execute_agent(
        child,
        timeout_ms=5000,
        launch_control=resume.provider_launch_control(context),
    )
    assert main_result["provider_contacted"] is True
    assert main_result["status"] == "success"
    claim = resume.get_claim(reservation)
    assert claim["gate_phase"] == "approved"
    assert claim["provider_phase"] in {"spawned", "reaped"}
    assert claim["gate_launch_binding"] is not None
    assert claim["provider_launch_binding"] is not None
    # The same host identity may legitimately serve both local subprocesses,
    # but the durable claim keeps separate gate and provider slots.  This
    # prevents a gate receipt from being mistaken for the main launch binding.
    assert claim["gate_launch_binding"] == context.launch_binding
    assert claim["provider_launch_binding"] == context.launch_binding


@pytest.mark.skipif(__import__("os").name != "nt",
                    reason="fixture uses a Windows cmd wrapper")
def test_real_observation_refuses_substituted_material_before_child(tmp_path):
    root, job_id, reservation, context, child, script = _prepare_child(
        tmp_path, gate=False)
    script.write_text(
        "@echo off\nREM SUMMON_CLAUDE_ENTRYPOINT_V1\necho substituted\n",
        encoding="utf-8",
    )

    result = execute_agent(
        child,
        timeout_ms=5000,
        launch_control=resume.provider_launch_control(context),
    )
    assert result["status"] in {"blocked", "error"}, result
    assert result["provider_contacted"] is False
    assert result["attempt_status"] == "not_run"
    assert result["error_kind"] in {
        "resume_launch_qualification_invalid",
        "resume_launch_observation_stale",
        "launch_observation_changed",
        "launch_version_output_untrusted",
    }
    claim = resume.get_claim(reservation)
    assert claim["provider_phase"] == "pending"
    # The durable claim remains pending because the observation was rejected
    # before the CAS launch claim.  ``None`` preserves that pre-claim
    # uncertainty; the public executor envelope is the authoritative
    # provider_contacted=false statement for this refusal.
    assert claim["provider_contacted"] in {None, False}
