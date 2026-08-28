"""Provider-inert Phase 0 trust, lifecycle, and Windows transport regressions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import _background
import _executor
import _jobs
import _manifest
import _spawn
import _telemetry
import run_subagent


def test_posix_arkcli_roster_refresh_fails_cleanly_without_shell_fallback(
        monkeypatch, tmp_path):
    import _apibackend as api
    import _arkcli_backend as ark

    calls = []

    def missing(argv, **kwargs):
        calls.append((argv, kwargs))
        raise FileNotFoundError("fixture missing")

    monkeypatch.setattr(ark, "_is_windows", lambda: False)
    monkeypatch.setattr(ark.shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", missing)
    monkeypatch.setattr(api, "_roster_cache_path", lambda: str(tmp_path / "cache.json"))
    with pytest.raises(RuntimeError, match="arkcli not found on PATH"):
        api.refresh_coding_plan_roster()
    assert len(calls) == 1
    assert calls[0][0][:3] == ["arkcli", "plans", "model-list"]
    assert calls[0][1].get("shell") is False


def test_windows_arkcli_roster_refresh_uses_node_entry_without_shell(
        monkeypatch, tmp_path):
    import _apibackend as api
    import _arkcli_backend as ark

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"plan": "coding-plan", "models": []}),
            stderr="",
        )

    monkeypatch.setattr(ark, "_is_windows", lambda: True)
    monkeypatch.setattr(_spawn, "run_flags", dict)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(ark.shutil, "which", lambda name: {
        "arkcli.cmd": r"C:\\npm\\arkcli.cmd",
        "node": r"C:\\node\\node.exe",
    }.get(name))
    monkeypatch.setattr(ark, "_arkcli_node_entry",
                        lambda _shim: r"C:\\npm\\node_modules\\arkcli\\run.js")
    monkeypatch.setattr(api, "_roster_cache_path", lambda: str(tmp_path / "cache.json"))
    api.refresh_coding_plan_roster()
    assert len(calls) == 1
    assert calls[0][0][:3] == [
        r"C:\\node\\node.exe", r"C:\\npm\\node_modules\\arkcli\\run.js", "plans"]
    assert calls[0][1]["shell"] is False


def test_windows_arkcli_chat_refuses_unresolved_command_shim(monkeypatch):
    import _arkcli_backend as ark

    monkeypatch.setattr(ark, "_is_windows", lambda: True)
    monkeypatch.setattr(ark.shutil, "which",
                        lambda name: r"C:\\npm\\arkcli.cmd"
                        if name in {"arkcli", "arkcli.cmd"} else None)
    monkeypatch.setattr(ark, "_arkcli_node_entry", lambda _shim: None)
    with pytest.raises(RuntimeError, match="package entry point"):
        ark._arkcli_cmd()


def test_windows_arkcli_chat_launcher_refusal_is_explicitly_not_run(monkeypatch):
    import _arkcli_backend as ark

    cwd = str(Path.cwd())
    monkeypatch.setitem(_executor.BACKENDS, "arkcli", {
        "call": ark.call, "kind": "api",
    })
    monkeypatch.setattr(ark.shutil, "which",
                        lambda name: r"C:\\npm\\arkcli.cmd"
                        if name in {"arkcli", "arkcli.cmd"} else None)
    monkeypatch.setattr(ark, "_is_windows", lambda: True)
    monkeypatch.setattr(ark, "_arkcli_node_entry", lambda _shim: None)
    result = _executor.execute_agent(
        _executor.AgentInvocation(
            cli="arkcli", prompt="provider-inert", cwd=cwd,
            model="concrete-model"),
        timeout_ms=5000,
    )
    assert result["error_kind"] == "unsafe_windows_launcher"
    assert result["attempts"] == 0
    assert result["attempt_status"] == "not_run"
    assert result["execution_status"] == "not_run"
    assert result["provider_contacted"] is False
    assert result["result_usable"] is False


def test_direct_provider_attempt_gets_opaque_attempt_id(monkeypatch):
    report = "STATUS: DONE\nSUMMARY: ok\nFOLLOW-UP: none\nHANDOFF: none"
    code = "import sys; print(sys.argv[1])"

    def fake_build(inv, timeout_ms):
        return sys.executable, ("-c", code, report), None

    monkeypatch.setattr(_executor, "build_invocation_args", fake_build)
    result = _executor.execute_agent(
        _executor.AgentInvocation(cli="codex", prompt="review", cwd=tempfile.gettempdir()),
        timeout_ms=5000,
    )

    assert result["provider_contacted"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", result["attempt_id"])
    assert result["attempt_status"] == "completed"
    assert result["attempts"] == 1


def test_structural_refusal_has_no_physical_attempt_id():
    result = _executor._enrich(
        _executor._error_response("codex", 1, "blocked", not_run=True), None)
    assert result["attempt_status"] == "not_run"
    assert result["provider_contacted"] is False
    assert "attempt_id" not in result


def test_generic_early_error_is_explicitly_not_run(monkeypatch):
    emitted = []
    monkeypatch.setattr(run_subagent, "_emit", lambda value, **kwargs: emitted.append(value))
    run_subagent._print_error("invalid local arguments")
    assert emitted == [{
        "result": "", "exit_code": 1, "status": "error",
        "error": "invalid local arguments", "attempts": 0,
        "attempt_status": "not_run", "execution_status": "not_run",
        "provider_contacted": False,
        "model": {"requested": None, "targeted": None, "served": None,
                  "resolved": None, "models_used": [], "evidence_source": None},
        "served_model_evidence": "absent", "model_match": None,
        "named_model_verified": False,
    }]


def test_background_structural_refusal_does_not_gain_attempt_id(monkeypatch):
    monkeypatch.setenv("SUMMON_JOB_ID", "a" * 32)
    monkeypatch.setattr(run_subagent, "_JOB_FILE", "synthetic-job.json")
    refusal = run_subagent._mark_not_run({"status": "error", "result": ""})
    run_subagent._stamp_job(refusal)
    assert "attempt_id" not in refusal


def test_background_record_binds_attempt_identity():
    with tempfile.TemporaryDirectory(prefix="summon-phase0-record-") as root:
        job_id = _jobs.new_job_id()
        _jobs.write_prepared(
            root,
            job_id,
            nonce="n" * 32,
            agent="reviewer",
            prompt_sha256=None,
            cwd=root,
            flags={},
            summon={},
            attempt_id=job_id,
        )
        record = _jobs.read_json(_jobs.record_path(root, job_id))
        assert record["attempt_id"] == job_id
        assert _jobs.job_status(root, job_id)["attempt_id"] == job_id


def test_background_result_prompt_must_match_immutable_launch_record():
    record = {
        "nonce": "n" * 32, "prompt_sha256": "a" * 64,
        "summon": {"scripts_sha256": "b" * 64,
                   "background_bundle": {"prompt_sha256": "a" * 64}},
    }
    result = {
        "status": "success", "job_nonce": "n" * 32,
        "prompt_sha256": "c" * 64,
        "summon": record["summon"],
    }
    assert _jobs._classify(record, "ok", result, "ok") == (
        "identity_mismatch", False)
    result["prompt_sha256"] = "a" * 64
    assert _jobs._classify(record, "ok", result, "ok") == ("success", True)


def test_background_context_metadata_must_match_immutable_launch_record():
    metadata = {"schema": "summon.context-dispatch/v1", "provider_contacted": False}
    digest = __import__("hashlib").sha256(json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    record = {
        "nonce": "n" * 32, "prompt_sha256": "a" * 64,
        "summon": {"scripts_sha256": "b" * 64, "background_bundle": {
            "prompt_sha256": "a" * 64,
            "context_compilation_sha256": digest,
        }},
    }
    result = {
        "status": "success", "job_nonce": "n" * 32,
        "prompt_sha256": "a" * 64, "summon": record["summon"],
        "context_compilation": metadata,
    }
    assert _jobs._classify(record, "ok", result, "ok") == ("success", True)
    result["context_compilation"] = {**metadata, "provider_contacted": True}
    assert _jobs._classify(record, "ok", result, "ok") == (
        "identity_mismatch", False)


def test_background_child_refuses_changed_frozen_prompt_before_contact(monkeypatch):
    expected = __import__("hashlib").sha256(b"approved").hexdigest()
    monkeypatch.setenv("SUMMON_JOB_PROMPT_SHA", expected)
    args = SimpleNamespace(prompt="changed")
    with pytest.raises(ValueError, match="launch record"):
        run_subagent._verify_frozen_background_prompt(args)
    args.prompt = "approved"
    run_subagent._verify_frozen_background_prompt(args)
    assert args._background_prompt_verified is True


def test_frozen_prompt_file_mutation_is_refused_before_child_contact(
        tmp_path, monkeypatch):
    scripts = tmp_path / "bundle" / "scripts"
    scripts.mkdir(parents=True)
    entry = scripts / "run_subagent.py"
    entry.write_text("# frozen dispatcher", encoding="utf-8")
    digest = __import__("hashlib").sha256(b"approved").hexdigest()
    prompt_file = Path(_background._freeze_background_prompt(
        str(entry), "approved", digest))
    prompt_file.write_text("changed", encoding="utf-8")
    monkeypatch.setenv("SUMMON_JOB_PROMPT_SHA", digest)
    with pytest.raises(ValueError, match="launch record"):
        run_subagent._verify_frozen_background_prompt(
            SimpleNamespace(prompt=prompt_file.read_text(encoding="utf-8")))


def test_background_first_dispatch_reuses_precommitted_attempt_id(monkeypatch):
    seen = []

    def fake_execute(invocation, **_kwargs):
        seen.append(invocation.attempt_id)
        return {"status": "success", "execution_status": "success",
                "provider_contacted": True, "attempt_status": "completed",
                "backend_exit_code": 0, "raw_backend_exit_code": 0,
                "normalized_exit_code": 0, "served_model_evidence": "absent",
                "model": {"served": None}}

    monkeypatch.setattr(_executor, "execute_agent", fake_execute)
    # run_subagent imports execute_agent into module scope.
    monkeypatch.setattr(run_subagent, "execute_agent", fake_execute)
    job_id = "b" * 32
    invocation = _executor.AgentInvocation(
        cli="codex", prompt="x", cwd=tempfile.gettempdir(), attempt_id=job_id)
    args = SimpleNamespace(timeout=1000, debug_dir=None, retries=0,
                           transient_retries=False, no_acp_fallback=True,
                           max_tool_output_bytes=None, gate_with=None)
    result = run_subagent._dispatch_with_retries(invocation, args)
    assert seen == [job_id]
    assert result["attempt_history"][0]["attempt_id"] == job_id


def test_gate_denial_after_attempt_preserves_execution_provenance():
    invocation = _executor.AgentInvocation(
        cli="claude", prompt="x", cwd=tempfile.gettempdir())
    primary = {
        "status": "error", "execution_status": "error",
        "provider_contacted": True, "attempt_status": "completed",
        "attempt_id": "c" * 32, "backend_exit_code": 1,
        "raw_backend_exit_code": 1, "normalized_exit_code": 1,
        "cost_usd": 0.25, "usage": {"input_tokens": 10},
        "model": {"requested": "m", "targeted": "m", "served": "m"},
        "served_model_evidence": "reported", "model_match": True,
        "named_model_verified": True,
    }
    history = [run_subagent._attempt_projection(primary, primary["attempt_id"])]
    denied = run_subagent._blocked_after_attempt(
        {"approved": False, "reason": "no second turn"}, primary, invocation,
        SimpleNamespace(_receipt=None), 1, next_kind="retry",
        attempt_history=history)
    assert denied["status"] == "blocked"
    assert denied["execution_status"] == "error"
    assert denied["attempts"] == 1
    assert denied["attempt_status"] == "completed"
    assert denied["provider_contacted"] is True
    assert denied["model"]["served"] == "m"
    assert denied["served_model_evidence"] == "reported"
    assert denied["cost_usd"] == 0.25
    assert denied["next_attempt"]["status"] == "not_run"


def test_background_terminalization_is_first_writer_wins():
    with tempfile.TemporaryDirectory(prefix="summon-phase0-terminal-") as root:
        path = str(Path(root) / "job.json")
        first = json.dumps({"status": "success", "result": "first"})
        second = json.dumps({"status": "error", "result": "second"})
        run_subagent._write_job_file_text(first, path)
        run_subagent._write_job_file_text(second, path)
        assert json.loads(Path(path).read_text(encoding="utf-8"))["result"] == "first"
        assert not Path(path + ".terminal.lock").exists()


def test_background_terminalization_competing_finalizers_are_idempotent():
    with tempfile.TemporaryDirectory(prefix="summon-phase0-terminal-race-") as root:
        path = str(Path(root) / "job.json")
        payloads = [json.dumps({"status": "success", "result": "a"}),
                    json.dumps({"status": "error", "result": "b"})]
        failures = []
        barrier = threading.Barrier(2)

        def publish(payload):
            try:
                barrier.wait(timeout=2)
                run_subagent._write_job_file_text(payload, path)
            except Exception as exc:  # pragma: no cover - diagnostic assertion below
                failures.append(exc)

        threads = [threading.Thread(target=publish, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        assert failures == [], failures
        assert json.loads(Path(path).read_text(encoding="utf-8"))["status"] in {
            "success", "error"}
        assert not Path(path + ".terminal.lock").exists()


def test_cmd_transport_rejects_unsafe_raw_prompt_without_launch(monkeypatch):
    monkeypatch.setenv("SUMMON_CMD_LAUNCHER", "1")
    message = run_subagent._cmd_prompt_transport_error("one\ntwo & three", None)
    assert message and "--prompt-file" in message

    # The real entry point emits the same refusal envelope before roster/backend
    # resolution. No provider command is available in this test process.
    script = Path(__file__).with_name("run_subagent.py")
    with tempfile.TemporaryDirectory(prefix="summon-phase0-cmd-") as cwd:
        env = os.environ.copy()
        env["SUMMON_CMD_LAUNCHER"] = "1"
        env["SUMMON_TELEMETRY"] = "0"
        completed = subprocess.run(
            [sys.executable, str(script), "--agent", "reviewer", "--prompt",
             "one\ntwo & three", "--cwd", cwd, "--dry-run", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["error_kind"] == "prompt_transport_unsafe"
    assert envelope["attempts"] == 0
    assert envelope["attempt_status"] == "not_run"
    assert envelope["provider_contacted"] is False


def test_prompt_file_is_safe_cmd_transport(monkeypatch):
    monkeypatch.setenv("SUMMON_CMD_LAUNCHER", "1")
    assert run_subagent._cmd_prompt_transport_error("one\ntwo & three", "prompt.md") is None


def test_context_dry_run_is_provider_inert_and_reports_compilation_without_paths():
    script = Path(__file__).with_name("run_subagent.py")
    with tempfile.TemporaryDirectory(prefix="summon-context-dry-") as cwd:
        agents = Path(cwd, "agents")
        agents.mkdir()
        Path(agents, "reviewer.md").write_text(
            "---\nrun-agent: codex\npermission: read-only\nmodel: gpt-5.6-sol\n---\nReview.",
            encoding="utf-8")
        context_path = Path(cwd, "context.json")
        context_path.write_text(json.dumps({
            "schema": "summon.context-input/v1",
            "blocks": [{"id": "note", "plane": "payload", "kind": "note",
                        "body": "provider-inert fixture"}],
        }), encoding="utf-8")
        env = os.environ.copy()
        env["SUMMON_TELEMETRY"] = "0"
        completed = subprocess.run(
            [sys.executable, str(script), "--agent", "reviewer", "--prompt", "review",
             "--cwd", cwd, "--agents-dir", str(agents), "--strict-agents-dir",
             "--context-input-file", str(context_path), "--context-profile", "safe",
             "--dry-run", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        view = json.loads(completed.stdout)
        projection = view["context_compilation"]
        assert view["provider_contacted"] is False
        assert projection["provider_contacted"] is False
        assert projection["profile"] == "safe"
        assert projection["block_count"] == 1
        assert cwd not in json.dumps(projection)


def test_context_input_outside_read_allowlist_refuses_before_provider_contact():
    script = Path(__file__).with_name("run_subagent.py")
    with tempfile.TemporaryDirectory(prefix="summon-context-cwd-") as cwd, \
            tempfile.TemporaryDirectory(prefix="summon-context-outside-") as outside:
        context_path = Path(outside, "context.json")
        context_path.write_text(json.dumps({
            "schema": "summon.context-input/v1",
            "blocks": [{"id": "note", "plane": "payload", "kind": "note",
                        "body": "outside"}],
        }), encoding="utf-8")
        env = os.environ.copy()
        env["SUMMON_TELEMETRY"] = "0"
        completed = subprocess.run(
            [sys.executable, str(script), "--agent", "reviewer", "--prompt", "review",
             "--cwd", cwd, "--context-input-file", str(context_path),
             "--dry-run", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        assert completed.returncode == 1
        refusal = json.loads(completed.stdout)
        assert refusal["error_kind"] == "context_target_outside_allowlist"
        assert refusal["attempts"] == 0
        assert refusal["provider_contacted"] is False


def test_real_windows_cmd_refuses_all_raw_prompt_bytes_before_dispatch():
    if os.name != "nt":
        return
    launcher = Path(__file__).with_name("summon.cmd")
    with tempfile.TemporaryDirectory(prefix="summon-real-cmd-") as cwd:
        raw = "literal %PATH% & caret ^ unicode مرحبا"
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout, \
                tempfile.TemporaryFile("w+", encoding="utf-8") as stderr:
            completed = subprocess.run(
                [str(launcher), "dispatch", "--agent", "reviewer", "--prompt", raw,
                 "--cwd", cwd, "--dry-run", "--json"],
                stdout=stdout, stderr=stderr, text=True, encoding="utf-8", timeout=60,
            )
            stdout.seek(0)
            captured_stdout = stdout.read()
            stderr.seek(0)
            captured_stderr = stderr.read()
    assert completed.returncode == 1, captured_stdout + captured_stderr
    envelope = json.loads(captured_stdout)
    assert envelope["error_kind"] == "prompt_transport_unsafe"
    assert envelope["attempts"] == 0
    assert envelope["provider_contacted"] is False
    # cmd.exe may expand percent variables before Python sees argv, but the
    # refusal must never echo either the original or expanded prompt.
    assert raw not in captured_stdout
    assert os.environ.get("PATH", "") not in captured_stdout


def test_fanout_python_child_does_not_inherit_cmd_transport_marker(monkeypatch):
    monkeypatch.setenv("SUMMON_CMD_LAUNCHER", "1")
    child, error = _manifest._dispatch_child(
        [sys.executable, "-c",
         "import os;print(os.environ.get('SUMMON_CMD_LAUNCHER','absent'))"],
        timeout_sec=10)
    assert error is None
    assert child is not None and child.returncode == 0
    assert child.stdout.strip() == "absent"


def test_crash_telemetry_preserves_unknown_attempt_count():
    event = _telemetry.event_from_envelope(run_subagent._crash_envelope(RuntimeError("x")))
    assert event["attempts"] is None
    assert event["attempt_status"] == "unknown"
    assert event["provider_contacted"] is None


def test_classifier_cannot_relabel_structural_refusal_as_contacted():
    refusal = _executor._error_response(
        "kimi", 1, "login required", not_run=True)
    _executor._attach_eligibility(refusal)
    assert refusal["attempts"] == 0
    assert refusal["attempt_status"] == "not_run"
    assert refusal["provider_contacted"] is False


def test_telemetry_preserves_opaque_attempt_id():
    context = _telemetry.new_operation_context("dispatch")
    attempt_id = "a" * 32
    event = _telemetry.event_from_envelope(
        {"status": "error", "cli": "codex", "attempt_id": attempt_id,
         "attempts": 1, "provider_contacted": True, "execution_status": "error",
         "attempt_status": "completed"},
        operation_context=context,
    )
    assert event["attempt_id"] == attempt_id
