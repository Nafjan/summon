"""Provider-inert tests for private authenticated continuation sources."""

from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import _job_continuation as continuation
import _jobs
import run_subagent
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
        "cli": "claude", "backend_type": "cli", "served_via": "cli_agent",
        "provider": {"driver": "cli"}, "served": {"via": "cli_agent"},
        "resume": {"cli": "claude", "session_id": "session-private"},
        "billing": {"source": "subscription"},
    }
    job_file = _jobs.result_path(root, job_id)
    return root, job_id, job_file, invocation, args, result


def _publish_result(job_file, result):
    value = dict(result, job_nonce="nonce")
    _jobs._atomic_write_json(job_file, value)


def test_private_source_round_trip_and_public_projection_redacts_capabilities(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    public = continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    source = continuation.read_private_source(root, job_id)
    assert public == {
        "schema": "summon.job-continuation/v1", "available": True,
        "resume_state": "certified", "resume_reason": "private_source_authenticated",
        "backend": "claude", "transport": "subprocess",
        "steering_mode": "queued_for_resume", "live_steering_acknowledged": False,
    }
    public_text = json.dumps(public)
    for private in ("session-private", str(tmp_path), "reviewer.md", "nonce"):
        assert private not in public_text
    assert source["continuation"]["handle"] == "session-private"
    assert source["workspace"]["path"] == str((tmp_path / "workspace").resolve())


@pytest.mark.parametrize(("field", "value", "kind"), [
    ("provider_contacted", False, "single_contact_source_required"),
    ("attempts", 2, "single_contact_source_required"),
    ("served_model_evidence", "inferred", "reported_exact_model_required"),
    ("named_model_verified", False, "reported_exact_model_required"),
])
def test_ineligible_source_writes_no_private_sidecar(tmp_path, field, value, kind):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    result[field] = value
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is False
    assert public["resume_reason"] == kind
    assert not Path(continuation.continuation_path(root, job_id)).exists()


def test_unsupported_backend_is_fail_closed_without_handle_write(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    invocation = AgentInvocation(**{**invocation.__dict__, "cli": "codex"})
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is False and public["resume_state"] == "candidate"
    assert not Path(continuation.continuation_path(root, job_id)).exists()


@pytest.mark.parametrize(("field", "value"), [
    ("cli", "opencode"), ("backend", "opencode"),
    ("transport", "api"), ("provider", "openrouter"),
    ("resume.cli", "opencode"),
])
def test_terminal_route_must_match_certified_invocation_before_sealing(
        tmp_path, field, value):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    if field == "resume.cli":
        result["resume"]["cli"] = value
    else:
        result[field] = value
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is False
    assert public["resume_reason"] == "continuation_route_mismatch"
    assert not Path(continuation.continuation_path(root, job_id)).exists()


def test_candidate_public_projection_is_never_available(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    source = {
        "backend": {"cli": "opencode", "transport": "subprocess", "profile": None}}
    public = continuation.public_projection(source)
    assert public["available"] is False
    assert public["resume_state"] == "candidate"


def test_named_profile_requires_and_binds_real_profile_receipt(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    invocation = AgentInvocation(**{
        **invocation.__dict__, "profile": "review-profile",
        "profile_env": {"CLAUDE_CONFIG_DIR": str(profile_dir.resolve())},
    })
    missing = continuation.write_private_source(job_file, result, invocation, args)
    assert missing["available"] is False
    assert missing["resume_reason"] == "profile_provenance_required"
    result["profile"] = {
        "name": "review-profile", "cli": "claude",
        "path_sha256": hashlib.sha256(
            str(profile_dir.resolve()).encode("utf-8")).hexdigest()[:32],
        "registry_sha256": "a" * 32, "command_sha256": None,
    }
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is True
    _publish_result(job_file, result)
    source = continuation.read_private_source(root, job_id)
    assert source["backend"]["profile_registry_sha256"] == "a" * 32


def test_role_alias_requires_complete_approved_provenance(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    record_path = _jobs.record_path(root, job_id)
    record = _jobs.read_json(record_path)
    record["agent"] = "review-role"
    _jobs._atomic_write_json(record_path, record)
    args.agent = "review-role"
    args._resolved_agent = "reviewer"
    args._role_provenance = {"role": None}
    missing = continuation.write_private_source(job_file, result, invocation, args)
    assert missing["available"] is False
    assert missing["resume_reason"] == "approved_role_provenance_required"
    role = {
        "name": "review-role", "resolved_agent": "reviewer",
        "target_sha256": result["agent_def"]["sha256"],
        "fingerprint": "a" * 64, "hash": "sha256:" + "b" * 64,
        "registry_sha256": "c" * 64,
    }
    args._role_provenance = {"role": role}
    result.update({"agent_requested": "review-role", "agent_resolved": "reviewer",
                   "role": role})
    public = continuation.write_private_source(job_file, result, invocation, args)
    assert public["available"] is True
    _publish_result(job_file, result)
    source = continuation.read_private_source(root, job_id)
    assert source["agent"]["role_approval_sha256"] == "sha256:" + "b" * 64


def test_mutation_auth_launch_result_and_workspace_fail_closed(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    path = Path(continuation.continuation_path(root, job_id))
    original = path.read_text(encoding="utf-8")

    source = json.loads(original)
    source["authority"]["permission"] = "yolo"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(continuation.ContinuationError,
                       match="authentication failed"):
        continuation.read_private_source(root, job_id)

    path.write_text(original, encoding="utf-8")
    record_path = _jobs.record_path(root, job_id)
    record = _jobs.read_json(record_path)
    record["flags"]["model"] = "changed"
    _jobs._atomic_write_json(record_path, record)
    with pytest.raises(continuation.ContinuationError,
                       match="launch record changed"):
        continuation.read_private_source(root, job_id)


def test_launch_and_terminal_prompt_identity_must_match(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    result["prompt_sha256"] = "f" * 64
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.write_private_source(job_file, result, invocation, args)
    assert error.value.kind == "source_prompt_mismatch"


@pytest.mark.parametrize(("path", "value", "kind"), [
    (("attempt_id",), "f" * 32, "source_attempt_mismatch"),
    (("agent_def", "sha256"), "f" * 64, "source_result_changed"),
    (("agent_def", "source"), "bundled", "source_result_changed"),
    (("billing", "source"), "credit", "source_result_changed"),
    (("gate",), {"approved": True}, "source_result_changed"),
    (("resume", "cli"), "codex", "source_result_changed"),
])
def test_each_bound_receipt_field_rejects_post_seal_mutation(
        tmp_path, path, value, kind):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    changed = _jobs.read_json(job_file)
    if len(path) == 1:
        changed[path[0]] = value
    else:
        changed[path[0]][path[1]] = value
    _jobs._atomic_write_json(job_file, changed)
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == kind

@pytest.mark.parametrize(("field", "value"), [
    ("status", "cancelled"),
    ("execution_status", "cancelled"),
    ("provider_contacted", False),
    ("report_ok", True),
    ("attempts", 2),
])
def test_each_terminal_field_is_bound_to_private_source(tmp_path, field, value):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    changed = _jobs.read_json(job_file)
    changed[field] = value
    _jobs._atomic_write_json(job_file, changed)
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == "source_terminal_changed"


def test_registry_downgrade_revokes_an_authentic_source(tmp_path, monkeypatch):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    original = continuation.resume_capability

    def downgraded(cli, transport):
        row = original(cli, transport)
        return dict(row, resume_state="candidate", resume_reason="receipt_required")

    monkeypatch.setattr(continuation, "resume_capability", downgraded)
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == "resume_capability_revoked"


def test_duplicate_keys_nonfinite_and_oversized_sources_are_rejected(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    path = Path(continuation.continuation_path(root, job_id))
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"job_id":', '"schema":"duplicate","job_id":', 1),
                    encoding="utf-8")
    with pytest.raises(continuation.ContinuationError, match="duplicate JSON key"):
        continuation.read_private_source(root, job_id)
    path.write_text('{"created_at":NaN}', encoding="utf-8")
    with pytest.raises(continuation.ContinuationError, match="non-finite"):
        continuation.read_private_source(root, job_id)
    path.write_bytes(b"x" * (continuation.MAX_PRIVATE_BYTES + 1))
    with pytest.raises(continuation.ContinuationError, match="invalid size"):
        continuation.read_private_source(root, job_id)


def test_deeply_nested_private_json_is_typed_fail_closed(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    path = Path(continuation.continuation_path(root, job_id))
    path.write_text("[" * 5_000 + "0" + "]" * 5_000, encoding="utf-8")
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == "invalid_private_source"


@pytest.mark.parametrize("target", ["record", "result"])
@pytest.mark.parametrize("corruption", ["duplicate", "nonfinite", "oversized"])
def test_launch_and_result_inputs_use_bounded_strict_json(
        tmp_path, target, corruption):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    path = Path(_jobs.record_path(root, job_id) if target == "record" else job_file)
    raw = path.read_text(encoding="utf-8")
    if corruption == "duplicate":
        raw = raw.replace("{", '{"attempt_id":"' + "f" * 32 + '",', 1)
    elif corruption == "nonfinite":
        raw = raw.replace("{", '{"unbound":NaN,', 1)
    else:
        raw = raw.replace("{", '{"unbound":"' +
                          "x" * (continuation.MAX_JOB_BYTES + 1) + '",', 1)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == "source_job_untrusted"


@pytest.mark.parametrize("corruption", ["duplicate", "nonfinite", "oversized"])
def test_source_construction_strictly_reads_launch_record(tmp_path, corruption):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    path = Path(_jobs.record_path(root, job_id))
    raw = path.read_text(encoding="utf-8")
    if corruption == "duplicate":
        raw = raw.replace("{", '{"attempt_id":"' + "f" * 32 + '",', 1)
    elif corruption == "nonfinite":
        raw = raw.replace("{", '{"unbound":NaN,', 1)
    else:
        raw = raw.replace("{", '{"unbound":"' +
                          "x" * (continuation.MAX_JOB_BYTES + 1) + '",', 1)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(continuation.ContinuationError) as error:
        continuation.write_private_source(job_file, result, invocation, args)
    assert error.value.kind == "source_record_untrusted"


def test_identical_source_write_is_idempotent_and_preserves_exact_bytes(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    first = continuation.write_private_source(job_file, result, invocation, args)
    path = Path(continuation.continuation_path(root, job_id))
    original = path.read_bytes()
    second = continuation.write_private_source(job_file, result, invocation, args)
    assert second == first
    assert path.read_bytes() == original


def test_conflicting_concurrent_writers_publish_one_immutable_source(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    other = json.loads(json.dumps(result))
    other["resume"]["session_id"] = "other-private-handle"

    def write(candidate):
        try:
            return ("ok", continuation.write_private_source(
                job_file, candidate, invocation, args))
        except continuation.ContinuationError as exc:
            return (exc.kind, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, (result, other)))
    assert sorted(kind for kind, _ in outcomes) == ["continuation_source_conflict", "ok"]
    frozen = Path(continuation.continuation_path(root, job_id)).read_bytes()
    source = json.loads(frozen)
    assert source["continuation"]["handle"] in {
        "session-private", "other-private-handle"}
    assert Path(continuation.continuation_path(root, job_id)).read_bytes() == frozen


def test_terminal_emitter_seals_private_source_before_publishing_result(
        tmp_path, monkeypatch):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    monkeypatch.setattr(run_subagent, "_JOB_FILE", job_file)
    monkeypatch.setenv("SUMMON_JOB_ID", job_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", "nonce")
    monkeypatch.setenv("SUMMON_JOB_PROMPT_SHA", "c" * 64)
    run_subagent._emit(
        result, operation="dispatch", trusted_executor_result=True,
        continuation_context=(invocation, args))
    published = _jobs.read_json(job_file)
    assert published["continuation"]["available"] is True
    assert "session-private" not in json.dumps(published["continuation"])
    source = continuation.read_private_source(root, job_id)
    assert source["continuation"]["handle"] == "session-private"


def test_workspace_recreation_is_not_continuity(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    workspace = Path(invocation.cwd)
    before = continuation.capture_workspace(str(workspace))
    workspace.rmdir()
    # Some POSIX filesystems immediately recycle the just-freed inode.  Keep a
    # decoy directory alive so the recreated workspace gets a distinct
    # directory-object identity and the test exercises the continuity check,
    # not an allocator coincidence.
    decoy = workspace.with_name("workspace-identity-decoy")
    decoy.mkdir()
    workspace.mkdir()
    after = continuation.capture_workspace(str(workspace))
    if (after["device"], after["inode"]) == (before["device"], before["inode"]):
        pytest.skip("filesystem reused a live directory identity")
    with pytest.raises(continuation.ContinuationError,
                       match="workspace continuity") as error:
        continuation.read_private_source(root, job_id)
    assert error.value.kind == "workspace_identity_changed"
