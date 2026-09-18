# Extracted unchanged from the named public fixture functions; no ledger/digest mocks.
from dataclasses import replace
from types import SimpleNamespace
import hashlib, json
import _jobs, _job_control
import _job_continuation as continuation
import _job_resume as resume
import _launch_qualification as qualification
from _resume_capabilities import resume_capability_v2
from _builder import AgentInvocation

def _fixture(tmp_path):
    root = str(tmp_path / "jobs")
    workspace = tmp_path / "workspace"
    roster = tmp_path / "agents"
    workspace.mkdir()
    roster.mkdir()
    agent_file = roster / "reviewer.md"
    agent_file.write_text("agent", encoding="utf-8")
    job_id = "a" * 32
    scripts_sha = "b" * 64
    _jobs.write_prepared(
        root, job_id, nonce="nonce", agent="reviewer",
        prompt_sha256="c" * 64, cwd=str(workspace),
        flags={"cli": "claude", "model": "claude-opus-5"},
        summon={"version": "3.2.1", "scripts_sha256": scripts_sha},
        attempt_id=job_id)
    _jobs.update_spawned(root, job_id, 123)
    invocation = AgentInvocation(
        cli="claude", prompt="private", cwd=str(workspace),
        agent_file=str(agent_file), permission="safe-edit", transport="subprocess",
        model="claude-opus-5", model_exact_required=True,
        model_exact_source="agent", effort="high", read_roots=(), attempt_id=job_id)
    args = SimpleNamespace(
        agent="reviewer", agents_dir=str(roster), max_permission="safe-edit",
        strict_agents_dir=True, enable_roles=False, no_contract_repair=True,
        allow_credit=False, allow_payg=False, gate_with=None)
    result = {
        "status": "error", "execution_status": "error", "attempts": 1,
        "attempt_id": job_id, "provider_contacted": True, "report_ok": False,
        "summon": {"version": "3.2.1", "scripts_sha256": scripts_sha},
        "request_sha256": "d" * 64, "prompt_sha256": "c" * 64,
        "agent_def": {"sha256": "e" * 64, "source": "explicit"},
        "model": {"requested": "claude-opus-5", "targeted": "claude-opus-5",
                  "served": "claude-opus-5"},
        "served_model_evidence": "reported", "model_match": True,
        "named_model_verified": True,
        "cli": "claude", "backend_type": "cli_agent", "served_via": "cli_agent",
        "provider": {"driver": "cli"}, "served": {"via": "cli_agent"},
        "resume": {"cli": "claude", "session_id": "session-private"},
        "billing": {"source": "subscription"},
    }
    job_file = _jobs.result_path(root, job_id)
    return root, job_id, job_file, invocation, args, result

def _publish_result(job_file, result):
    value = dict(result, job_nonce="nonce")
    _jobs._atomic_write_json(job_file, value)

def _synthetic_launch_observation():
    """Provider-free host observation used by governed continuation fixtures."""
    row = resume_capability_v2("resume", "claude", "subprocess")
    executable = "a" * 64
    return {
        "schema": "summon.resume-launch-observation/v1",
        "backend": "claude", "transport": "subprocess",
        "command_sha256": "b" * 64, "argv_sha256": "c" * 64,
        "cwd_sha256": "d" * 64, "env_names_sha256": "e" * 64,
        "env_sha256": "f" * 64,
        "executable_path_sha256": "1" * 64,
        "executable_sha256": executable, "executable_size": 1,
        "executable_mtime_ns": 1,
        "launch_material_sha256": "3" * 64,
        "executable_content_revision": f"sha256:{executable}",
        "external_cli_version": "claude-test/1.0",
        "registry_generation": row["registry_generation"],
        "registry_digest": row["registry_digest"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version_scope"],
        "external_cli_version_scope": row["external_cli_version_scope"],
        "observation_nonce": "2" * 32, "observed_at_ns": 1,
    }

def _launch_evidence():
    """Build the strict fleet envelope consumed at the governed Popen seam."""
    observation = _synthetic_launch_observation()
    return {
        "schema": "summon.fleet-launch-evidence/v1",
        "backend": observation["backend"],
        "transport": observation["transport"],
        "command_sha256": observation["command_sha256"],
        "argv_sha256": observation["argv_sha256"],
        "cwd_sha256": observation["cwd_sha256"],
        "env_names_sha256": observation["env_names_sha256"],
        "env_sha256": observation["env_sha256"],
        "launch_observation": observation,
    }

def _eligible_source(tmp_path, *, commands=(), allow_text_only=False,
                     require_tools=False):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    invocation = replace(
        invocation, profile="review-profile",
        profile_env={"CLAUDE_CONFIG_DIR": str(profile_dir.resolve())})
    args.allow_text_only = allow_text_only
    args.require_tools = require_tools
    result["profile"] = {
        "name": "review-profile", "cli": "claude",
        "path_sha256": hashlib.sha256(
            str(profile_dir.resolve()).encode("utf-8")).hexdigest()[:32],
        "registry_sha256": "a" * 32, "command_sha256": None,
    }
    result["_private_launch_observation"] = _synthetic_launch_observation()
    continuation.write_private_source(job_file, result, invocation, args)
    record = _jobs.read_json(_jobs.record_path(root, job_id))
    obs = result["_private_launch_observation"]
    qualified = qualification.issue(
        source_job_id=job_id, source_attempt_id=record["attempt_id"],
        operation="resume", backend=obs["backend"], transport=obs["transport"],
        registry_generation=obs["registry_generation"], registry_digest=obs["registry_digest"],
        adapter=obs["adapter"], adapter_version=obs["adapter_version"],
        external_cli_version=obs["external_cli_version"],
        material_contract="summon-claude-subprocess-material/v1",
        executable_sha256=obs["executable_sha256"],
        launch_material_sha256=obs["launch_material_sha256"],
        expires_at=__import__("time").time() + 3600,
        revocation_id=qualification.revocation_id_for({
            "operation": "resume", "backend": obs["backend"],
            "transport": obs["transport"],
            "registry_generation": obs["registry_generation"],
            "registry_digest": obs["registry_digest"],
            "adapter": obs["adapter"], "adapter_version": obs["adapter_version"],
            "external_cli_version": obs["external_cli_version"],
            "material_contract": "summon-claude-subprocess-material/v1",
            "executable_sha256": obs["executable_sha256"],
            "launch_material_sha256": obs["launch_material_sha256"],
        }), source_nonce=record["nonce"])
    qualification.write(root, job_id, qualified)
    if commands:
        from _job_control import queue_command
        for action, value in commands:
            if action == "steer":
                queue_command(root, job_id, action, message=value)
            else:
                queue_command(root, job_id, action, duration_ms=value)
    _publish_result(job_file, result)
    return root, job_id

def _prepare(tmp_path, *, gate_with=None):
    root, job_id = _eligible_source(tmp_path)
    reservation = resume.reserve_request(root, job_id, message="continue",
                                         request_id="5" * 32,
                                         gate_with=gate_with)
    source = reservation.source
    successor_nonce = "successor-nonce"
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce=successor_nonce,
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id,
        resume_lineage={
            "source_job_id": job_id, "request_id": reservation.request_id,
            "claim_id": reservation.claim_id,
            "request_sha256": reservation.request_sha256,
        })
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    return root, job_id, reservation, prepared

def _canonical_terminal_result(root, source_job_id, reservation, *, contacted):
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    claim = resume.get_claim(reservation)
    return {
        "status": "error", "execution_status": "error",
        "attempt_status": "completed" if contacted else "not_run",
        "attempts": 1 if contacted else 0,
        "attempt_id": reservation.successor_job_id if contacted else None,
        "job_nonce": record["nonce"], "provider_contacted": contacted,
        "summon": record["summon"], "prompt_sha256": record["prompt_sha256"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": source_job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": claim["claim_sha256"],
        },
    }
