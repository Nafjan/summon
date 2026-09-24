"""Real local producer -> public argv dispatcher -> private/public publication.

The marked cmd child emits synthetic Claude-shaped terminal JSON. Only the
profile-command admission adapter is replaced to admit that local interpreter;
this does not qualify installed profile admission or a live provider. Neither
the executor result nor its model/launch evidence is fabricated by the harness.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

import _executor
import _chat_launch_guard
import _context_policy
import _job_continuation as continuation
import _jobs
import _launch_binding
import _profiles
import _rundir
import run_subagent
from test_launch_observer import _fake_claude


pytestmark = pytest.mark.skipif(os.name != "nt", reason="marked cmd fixture requires Windows")


def _dispatch(tmp_path, monkeypatch, *, served_model="claude-opus-5", out=False,
              job=True, chat_guard=False):
    # Keep every config lookup and child environment inside synthetic inputs.
    safe_env = {key: os.environ[key] for key in (
        "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP")
        if key in os.environ}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in safe_env.items():
        monkeypatch.setenv(key, value)
    home, workspace, profile, roster = (tmp_path / name for name in (
        "home", "workspace", "profile", "agents"))
    for directory in (home, workspace, profile, roster):
        directory.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SUMMON_TELEMETRY", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    command, script = _fake_claude(tmp_path)
    script.write_text(script.read_text(encoding="utf-8").replace(
        '"session_id":"fixture"',
        '"model":' + json.dumps(served_model) + ',"session_id":"fixture"'),
        encoding="utf-8")
    registry = tmp_path / "fixture-profiles.json"
    registry.write_text(json.dumps({"profiles": {"fixture-profile": {
        "cli": "claude", "config_dir": str(profile), "command": command,
        "models": ["claude-opus-5"],
    }}}), encoding="utf-8")
    monkeypatch.setenv("SUMMON_PROFILES_FILE", str(registry))

    def fixture_command(raw, cli, cwd):
        assert raw == command and cli == "claude" and cwd == str(workspace)
        assert Path(command).is_file()
        return command, hashlib.sha256(command.encode("utf-8")).hexdigest()[:32]

    monkeypatch.setattr(_profiles, "_profile_command", fixture_command)
    (roster / "fixture-seat.md").write_text(
        "---\nrun-agent: claude\npermission: read-only\nmodel: claude-opus-5\n"
        "model-policy: exact\nprofile: fixture-profile\n"
        f'args: /d /s /c "{script}"\n---\nOnly the deterministic fixture runs.\n',
        encoding="utf-8")
    job_id, nonce = "a" * 32, "synthetic-source-nonce"
    prompt = "Return the local deterministic fixture result."
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    guard_owner = None
    guard_path = None
    guard_token = None
    guard_attempt_id = None
    if chat_guard:
        guard_root = tmp_path / "chat-guard-root"
        guard_root.mkdir()
        guard_owner = _rundir.acquire_owner(str(guard_root), 120)
        guard_path = str(guard_root / "guard.json")
        guard_token = "chat-guard-token"
        guard_attempt_id = "c" * 32
        _chat_launch_guard.create_v2(
            guard_path, guard_token, expected=None, session_id="fixture-session",
            participant="fixture-seat", policy=_context_policy.make("off", policy_id="chat-context"),
            payload_sha256=prompt_sha,
            context_selection_sha256=hashlib.sha256(b"[]").hexdigest(),
            backend="claude", transport="subprocess", turn_id="fixture-turn",
            owner_generation=guard_owner.generation, owner_nonce=guard_owner.nonce,
            attempt_id=guard_attempt_id)
        for key, value in {
            "SUMMON_CHAT_LAUNCH_GUARD_PATH": guard_path,
            "SUMMON_CHAT_LAUNCH_GUARD_TOKEN": guard_token,
            "SUMMON_CHAT_LAUNCH_GUARD_VERSION": "2",
            "SUMMON_CHAT_LAUNCH_ATTEMPT_ID": guard_attempt_id,
        }.items():
            monkeypatch.setenv(key, value)
    jobs_root = str(tmp_path / "jobs")
    job_file = _jobs.result_path(jobs_root, job_id)
    summon_identity = run_subagent._receipt_base()["summon"]
    if job:
        _jobs.write_prepared(
            jobs_root, job_id, nonce=nonce, agent="fixture-seat",
            prompt_sha256=prompt_sha, cwd=str(workspace),
            flags={"cli": "claude", "model": "claude-opus-5"},
            summon=summon_identity, attempt_id=job_id)
        _jobs.update_spawned(jobs_root, job_id, os.getpid())
        for key, value in {
            "SUMMON_JOB_DIR": jobs_root,
            "SUMMON_JOB_ID": job_id, "SUMMON_JOB_NONCE": nonce,
            "SUMMON_JOB_PROMPT_SHA": prompt_sha,
        }.items():
            monkeypatch.setenv(key, value)
    # Restore dispatcher process globals after invoking its actual main().
    # Each in-process main() call must start with a clean dispatcher global;
    # otherwise a prior background test can leak SUMMON_JOB_DIR/nonce state
    # into a foreground fixture and make accounting refuse before contact.
    monkeypatch.setattr(run_subagent, "_JOB_FILE", None)
    monkeypatch.setattr(run_subagent, "_GOVERNED_RESUME_LINEAGE", None)
    monkeypatch.setattr(run_subagent, "_EMIT_OPERATION", "dispatch")
    produced, emitted = [], []
    real_execute, real_emit = _executor.execute_agent, run_subagent._emit

    def capture_producer(invocation, **kwargs):
        result = real_execute(invocation, **kwargs)
        produced.append((result, copy.deepcopy(result), invocation))
        return result

    def capture_emitter(result, **kwargs):
        emitted.append((result, copy.deepcopy(result), kwargs))
        return real_emit(result, **kwargs)

    monkeypatch.setattr(run_subagent, "execute_agent", capture_producer)
    monkeypatch.setattr(run_subagent, "_emit", capture_emitter)
    argv = ["run_subagent.py", "dispatch", "--agent", "fixture-seat",
            "--prompt", prompt, "--cwd", str(workspace), "--agents-dir", str(roster),
            "--strict-agents-dir", "--no-contract-repair", "--timeout", "5s"]
    if job:
        argv.extend(["--job-file", job_file])
    out_file = tmp_path / "public-out.json"
    if out:
        argv.extend(["--out", str(out_file)])
    monkeypatch.setattr(sys, "argv", argv)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        with pytest.raises(SystemExit) as stopped:
            run_subagent.main()
    assert len(produced) == 1, "public dispatcher never reached the real fixture executor"
    assert len(emitted) == 1
    assert emitted[0][0] is produced[0][0], "terminal result was replaced before emission"
    assert emitted[0][2]["trusted_executor_result"] is True
    context_invocation = emitted[0][2]["continuation_context"][0]
    # The retry controller tags an invocation copy with the physical attempt
    # identity. Its route, request and authority must match the emitter context.
    for field in ("cli", "transport", "prompt", "cwd", "agent_file", "permission",
                  "model", "model_exact_required", "profile", "profile_env",
                  "profile_command", "extra_args"):
        assert getattr(context_invocation, field) == getattr(produced[0][2], field)
    return {
        "root": jobs_root, "job_id": job_id, "job_file": job_file,
        "out_file": out_file, "producer": produced[0][1],
        "pre_emit": emitted[0][1], "exit_code": stopped.value.code,
        "published": _jobs.read_json(job_file) if job else json.loads(stdout.getvalue()),
        "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
        "guard_owner": guard_owner, "guard_path": guard_path,
        "guard_token": guard_token, "guard_attempt_id": guard_attempt_id,
    }


def _assert_no_private_launch_material(envelope, observation):
    text = json.dumps(envelope, sort_keys=True)
    if "_private_launch_observation" in text:
        pytest.fail("public artifact contains private launch marker", pytrace=False)
    for field in ("executable_sha256", "launch_material_sha256"):
        if observation[field] in text:
            pytest.fail("public artifact contains private launch material digest", pytrace=False)


def test_public_dispatcher_seals_actual_producer_observation_before_terminal_publication(tmp_path, monkeypatch):
    run = _dispatch(tmp_path, monkeypatch)
    result, published = run["producer"], run["published"]
    assert run["exit_code"] == 0
    assert result["status"] == "success" and result["provider_contacted"] is True
    assert result["attempts"] == 1 and result["attempt_id"] == run["job_id"]
    assert result["served_model_evidence"] == "reported"
    assert result["named_model_verified"] is True
    observation = result["_private_launch_observation"]
    # Ordinary dispatch does not request a trusted version probe. Preserve
    # that missing evidence rather than upgrading it from the fixture label.
    assert observation["external_cli_version"] is None
    assert run["pre_emit"]["_private_launch_observation"] == observation
    _assert_no_private_launch_material(published, observation)
    _assert_no_private_launch_material({"stdout": run["stdout"], "stderr": run["stderr"]}, observation)
    if published["continuation"]["available"] is not True:
        pytest.fail("real source publication refused: "
                    + published["continuation"]["resume_reason"]
                    + "; producer backend_type=" + result["backend_type"], pytrace=False)
    source = continuation.read_private_source(run["root"], run["job_id"])
    binding = continuation.read_launch_binding(run["root"], run["job_id"])
    assert binding == _launch_binding.binding_projection(observation)
    assert source["continuation"]["handle"] == result["resume"]["session_id"]
    assert source["attempt_id"] == result["attempt_id"]
    assert published["request_sha256"] == run["pre_emit"]["request_sha256"]
    assert published["profile"]["name"] == "fixture-profile"
    _assert_no_private_launch_material(published, observation)


def test_public_dispatcher_model_mismatch_cannot_publish_a_certified_source(tmp_path, monkeypatch):
    run = _dispatch(tmp_path, monkeypatch, served_model="claude-sonnet-4-6")
    result, published = run["producer"], run["published"]
    assert run["exit_code"] == 1
    assert result["status"] == "blocked" and result["provider_contacted"] is True
    assert result["named_model_verified"] is False
    assert published["continuation"]["available"] is False
    assert not Path(continuation.continuation_path(run["root"], run["job_id"])).exists()
    assert not Path(continuation.launch_binding_path(run["root"], run["job_id"])).exists()
    _assert_no_private_launch_material(published, result["_private_launch_observation"])
    _assert_no_private_launch_material({"stdout": run["stdout"], "stderr": run["stderr"]},
                                       result["_private_launch_observation"])


def test_public_out_artifact_does_not_publish_private_executor_observation(tmp_path, monkeypatch):
    run = _dispatch(tmp_path, monkeypatch, out=True)
    assert run["exit_code"] == 0
    observation = run["producer"]["_private_launch_observation"]
    _assert_no_private_launch_material(run["published"], observation)
    output = json.loads(run["out_file"].read_text(encoding="utf-8"))
    _assert_no_private_launch_material(output, observation)


def test_chat_guard_binds_the_parent_attempt_through_real_dispatcher_and_executor(tmp_path, monkeypatch):
    run = _dispatch(tmp_path, monkeypatch, job=False, chat_guard=True)
    try:
        assert run["exit_code"] == 0
        assert run["producer"]["attempt_id"] == run["guard_attempt_id"]
        observed = _chat_launch_guard.read_v2(run["guard_path"], run["guard_token"])
        assert observed is not None
        assert observed["attempt_id"] == run["guard_attempt_id"]
        assert observed["payload_sha256"] == hashlib.sha256(
            "Return the local deterministic fixture result.".encode("utf-8")).hexdigest()
    finally:
        if run["guard_owner"] is not None:
            _rundir.release_owner(run["guard_owner"])


@pytest.mark.parametrize("served_model", ["claude-opus-5", "claude-sonnet-4-6"], ids=['p001_case_001', 'p001_case_002'])
def test_foreground_stdout_and_out_omit_private_marker_without_job_sealing(tmp_path, monkeypatch, served_model):
    run = _dispatch(tmp_path, monkeypatch, served_model=served_model, out=True, job=False)
    assert run["exit_code"] == (0 if served_model == "claude-opus-5" else 1)
    assert run["producer"]["provider_contacted"] is True
    assert run["producer"]["attempts"] == 1
    assert "continuation" not in run["published"]
    assert not Path(run["root"]).exists()
    observation = run["producer"]["_private_launch_observation"]
    assert run["pre_emit"]["_private_launch_observation"] == observation
    _assert_no_private_launch_material(run["published"], observation)
    _assert_no_private_launch_material({"stdout": run["stdout"], "stderr": run["stderr"]}, observation)
    _assert_no_private_launch_material(json.loads(run["out_file"].read_text(encoding="utf-8")), observation)
