"""Synthetic roster drift across explicit freezes; no provider or task grant."""
from pathlib import Path
from dataclasses import replace
import hashlib
import json
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_roster as roster
import _roles as roles


@pytest.mark.parametrize("change", ["rename", "remove", "approved_alias_remap"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_frozen_roster_does_not_adopt_changed_definition_or_alias(tmp_path, monkeypatch, change):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setenv("SUMMON_ROLES_FILE", str(tmp_path / "roles.json"))
    monkeypatch.setenv("SUMMON_ROLES_PENDING_FILE", str(tmp_path / "roles-pending.json"))
    definition = "---\nrun-agent: claude\npermission: read-only\ntransport: subprocess\nmodel: claude-sonnet-4-6\n---\nSynthetic roster fixture.\n"
    first = agents / "e12-first.md"
    first.write_text(definition, encoding="utf-8")
    second = agents / "e12-second.md"
    second.write_text(definition + "Distinct second definition.\n", encoding="utf-8")
    alias = "e12-synthetic-role"

    def approve(target):
        roles.propose(alias, target, cwd=str(tmp_path), agents_dir=str(agents))
        roles.approve(alias, cwd=str(tmp_path), agents_dir=str(agents))

    def freeze(agent):
        return roster.freeze_roster(
            (roster.SeatRequest("seat-one", agent),), cwd=str(tmp_path),
            agents_dir=str(agents), role_enabled=change == "approved_alias_remap",
            strict_agents_dir=True,
        )

    if change == "approved_alias_remap":
        approve("e12-first")
    requested = alias if change == "approved_alias_remap" else "e12-first"
    frozen = freeze(requested)
    original = frozen.as_dict(native=True)
    assert frozen.revalidate()
    if change == "rename":
        first.rename(agents / "e12-renamed.md")
        next_request = "e12-renamed"
    elif change == "remove":
        first.unlink()
        next_request = "e12-second"
    else:
        # Build a separately approved registry through the real proposal APIs,
        # then simulate an external operator replacing the synthetic registry.
        # This does not claim that the CLI supports overwriting an approved role.
        alternate = tmp_path / "alternate-roles.json"
        with monkeypatch.context() as replacement_environment:
            replacement_environment.setenv("SUMMON_ROLES_FILE", str(alternate))
            replacement_environment.setenv("SUMMON_ROLES_PENDING_FILE", str(tmp_path / "alternate-pending.json"))
            approve("e12-second")
        (tmp_path / "roles.json").write_bytes(alternate.read_bytes())
        next_request = alias
    assert frozen.revalidate() is False
    assert frozen.as_dict(native=True) == original
    # A separately requested freeze is distinct evidence, not a repair of history
    # and not permission to execute an existing task or create a ballot.
    replacement = freeze(next_request)
    assert replacement.revalidate()
    assert replacement.roster_digest != frozen.roster_digest
    assert frozen.as_dict(native=True) == original


def test_roles_enabled_invocation_refuses_stored_target_drift_before_launch(
        tmp_path, monkeypatch, capsys):
    """The public dispatcher must reject an approved alias after its target drifts."""
    import run_subagent

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setenv("SUMMON_ROLES_FILE", str(tmp_path / "roles.json"))
    monkeypatch.setenv("SUMMON_ROLES_PENDING_FILE", str(tmp_path / "roles-pending.json"))
    monkeypatch.setenv("SUMMON_TELEMETRY", "0")
    definition = (
        "---\nrun-agent: claude\npermission: read-only\ntransport: subprocess\n"
        "model: claude-sonnet-4-6\neffort: high\n---\nStored seat fixture.\n")
    target = agents / "e12-first.md"
    target.write_text(definition, encoding="utf-8")
    alias = "e12-synthetic-role"
    approved = roles.propose(alias, "e12-first", cwd=str(tmp_path), agents_dir=str(agents))
    roles.approve(alias, cwd=str(tmp_path), agents_dir=str(agents))

    frozen = roster.freeze_roster(
        (roster.SeatRequest("seat-one", alias),), cwd=str(tmp_path),
        agents_dir=str(agents), role_enabled=True, strict_agents_dir=True,
    )
    stored_seat = frozen.seat("seat-one")
    assert stored_seat.requested_agent == alias
    assert stored_seat.resolved_agent == "e12-first"
    assert stored_seat.role_provenance["target_sha256"] == approved["target_sha256"]
    assert stored_seat.definition_sha256 == approved["target_sha256"]

    target.write_text(definition + "Target drift after approval.\n", encoding="utf-8")
    assert frozen.revalidate() is False

    launches = []

    def forbidden(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("provider/process launch attempted")

    monkeypatch.setattr(run_subagent, "execute_agent", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sys, "argv", [
        "summon", "--agent", alias, "--enable-roles", "--prompt", "stored task",
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--strict-agents-dir", "--model", "claude-sonnet-4-6", "--effort", "high",
        "--json",
    ])
    with pytest.raises(SystemExit) as exit_info:
        run_subagent.main()

    output = capsys.readouterr().out.strip()
    envelope = json.loads(output)
    assert exit_info.value.code == 1
    assert envelope["status"] == "error"
    assert envelope["error_kind"] == "role_resolution"
    assert "target changed after approval" in envelope["error"]
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert envelope["attempt_status"] == "not_run"
    assert envelope["execution_status"] == "not_run"
    assert launches == []


def test_durable_alias_remap_refuses_loaded_child_before_provider_launch(
        tmp_path, monkeypatch):
    """A remapped approved alias cannot rewrite a stored continuation source."""
    import _job_continuation as continuation
    import _job_resume as resume
    import _jobs
    from types import SimpleNamespace
    from test_job_continuation import _fixture, _publish_result

    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    agents = tmp_path / "agents"
    original_target = agents / "reviewer.md"
    replacement_target = agents / "replacement.md"
    replacement_target.write_text("replacement agent\n", encoding="utf-8")
    alias = "durable-synthetic-role"
    monkeypatch.setenv("SUMMON_ROLES_FILE", str(tmp_path / "roles.json"))
    monkeypatch.setenv("SUMMON_ROLES_PENDING_FILE", str(tmp_path / "roles-pending.json"))

    roles.propose(alias, "reviewer", cwd=str(tmp_path), agents_dir=str(agents))
    roles.approve(alias, cwd=str(tmp_path), agents_dir=str(agents))
    old_provenance = roles.resolve_for_dispatch(
        alias, cwd=str(tmp_path), agents_dir=str(agents), enabled=True,
        strict_agents_dir=True)
    old_target_sha = hashlib.sha256(original_target.read_bytes()).hexdigest()

    record_path = _jobs.record_path(root, job_id)
    record = _jobs.read_json(record_path)
    record["agent"] = alias
    _jobs._atomic_write_json(record_path, record)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    invocation = replace(
        invocation, agent_file=str(original_target), profile="review-profile",
        profile_env={"CLAUDE_CONFIG_DIR": str(profile_dir.resolve())})
    args.agent = alias
    args.agents_dir = str(agents)
    args.enable_roles = True
    args.strict_agents_dir = True
    args._resolved_agent = "reviewer"
    args._role_provenance = old_provenance
    args.allow_text_only = False
    args.require_tools = False
    args.gate_timeout = 600000
    args.timeout = 600000
    args.max_runtime = 24 * 60 * 60 * 1000
    args.retries = 0
    args.no_acp_fallback = True
    result["agent_def"] = {"sha256": old_target_sha, "source": "explicit"}
    result.update({"agent_requested": alias, "agent_resolved": "reviewer",
                   "role": old_provenance["role"],
                   "profile": {
                       "name": "review-profile", "cli": "claude",
                       "path_sha256": hashlib.sha256(
                           str(profile_dir.resolve()).encode("utf-8")).hexdigest()[:32],
                       "registry_sha256": "a" * 32, "command_sha256": None}})
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is True
    _publish_result(job_file, result)
    source_path = Path(continuation.continuation_path(root, job_id))
    source_bytes = source_path.read_bytes()
    result_bytes = Path(job_file).read_bytes()

    reservation = resume.reserve_request(
        root, job_id, message="continue the stored task", request_id="1" * 32)
    source = reservation.source
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce="successor-nonce",
        agent=source["agent"]["requested"], prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id,
        resume_lineage={"source_job_id": job_id, "request_id": reservation.request_id,
                        "claim_id": reservation.claim_id,
                        "request_sha256": reservation.request_sha256})
    successor_record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=successor_record, bundle_sha256="9" * 64)
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id), prepared["claim_file"])
    bound_receipt = {
        "agent_def": {"sha256": source["agent"]["definition_sha256"]},
        "profile": {
            "name": source["backend"]["profile"],
            "path_sha256": source["backend"]["profile_path_sha256"],
            "registry_sha256": source["backend"]["profile_registry_sha256"],
            "command_sha256": source["backend"]["profile_command_sha256"],
        },
    }
    # First prove the loaded child still matches the originally approved alias
    # and its authenticated source.  The later refusal must therefore be caused
    # by the approved replacement, not by a malformed child fixture.
    bound_invocation = replace(
        invocation, resume_id=context.resume_handle,
        attempt_id=reservation.successor_job_id)
    resume.validate_loaded_invocation(context, bound_invocation, args, bound_receipt)

    replacement_registry = tmp_path / "replacement-roles.json"
    replacement_pending = tmp_path / "replacement-roles-pending.json"
    with monkeypatch.context() as replacement_environment:
        replacement_environment.setenv("SUMMON_ROLES_FILE", str(replacement_registry))
        replacement_environment.setenv("SUMMON_ROLES_PENDING_FILE", str(replacement_pending))
        roles.propose(alias, "replacement", cwd=str(tmp_path), agents_dir=str(agents))
        roles.approve(alias, cwd=str(tmp_path), agents_dir=str(agents))
        fresh = roles.resolve_for_dispatch(
            alias, cwd=str(tmp_path), agents_dir=str(agents), enabled=True,
            strict_agents_dir=True)
        assert fresh["resolved"] == "replacement"
        assert "resume" not in fresh and "ballot" not in fresh

        remapped_args = SimpleNamespace(**vars(args))
        remapped_args._resolved_agent = "replacement"
        remapped_args._role_provenance = fresh
        remapped_invocation = replace(
            invocation, agent_file=str(replacement_target),
            resume_id=context.resume_handle,
            attempt_id=reservation.successor_job_id)
        launches = []

        def forbidden(*call_args, **call_kwargs):
            launches.append((call_args, call_kwargs))
            raise AssertionError("provider/process launch attempted")

        monkeypatch.setattr(subprocess, "Popen", forbidden)
        with pytest.raises(resume.ResumeError) as refusal:
            resume.validate_loaded_invocation(
                context, remapped_invocation, remapped_args, bound_receipt)
        assert refusal.value.kind == "resume_child_drift"
        assert launches == []

    assert source_path.read_bytes() == source_bytes
    assert Path(job_file).read_bytes() == result_bytes
