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
from _spawn import run_flags


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
            **run_flags(),
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
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
            **run_flags())
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
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
            **run_flags())
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
                **run_flags(),
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


def test_keyboard_interrupt_crash_envelope_is_typed_cancelled():
    env = run_subagent._crash_envelope(KeyboardInterrupt())
    assert env["status"] == "cancelled"
    assert env["dispatcher_status"] == "cancelled"
    assert env["execution_status"] == "cancelled"
    assert env["error_kind"] == "interrupted"
    assert env["failure_class"] == "cancelled"
    assert env["exit_code"] == 130
    assert env["backend_exit_code"] == 130
    assert env["raw_backend_exit_code"] == 130
    assert env["normalized_exit_code"] == 130
    assert env["retryable"] is True
    assert env["result_usable"] is False
    event = _telemetry.event_from_envelope(env)
    assert event["status"] == "cancelled"
    assert event["failure_class"] == "cancelled"
    assert event["exit_code"] == 130


def test_report_parsing_bullets_case_and_report_source_fallback():
    bullet_report = _executor.parse_report(
        "Analysis complete.\n"
        "- **Status**: DONE\n"
        "* Summary: Fixed the race condition\n"
        "- Follow-up: None\n"
        "- Handoff: Ready for merge\n"
    )
    assert bullet_report is not None
    assert bullet_report["status"] == "DONE"
    assert bullet_report["summary"] == "Fixed the race condition"
    assert bullet_report["follow_up"] == "None"
    assert bullet_report["handoff"] == "Ready for merge"

    structured = _executor._enrich(
        {"status": "success", "exit_code": 0, "cli": "claude",
         "result": "STATUS: DONE\nSUMMARY: Structured summary\nFOLLOW-UP: None\nHANDOFF: Ready"},
        None,
    )
    assert structured["report_ok"] is True
    assert structured["report_source"] == "structured"
    assert structured["report_text"] == "Structured summary"

    raw_only = _executor._enrich(
        {"status": "success", "exit_code": 0, "cli": "opencode",
         "result": "Plain prose response without contract block."},
        None,
    )
    assert raw_only["report"] is None
    assert raw_only["report_ok"] is False
    assert raw_only["report_source"] == "raw"
    assert raw_only["report_text"] == "Plain prose response without contract block."


def test_out_lock_refuses_active_collision_without_clobbering(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "shared-out.json"
    original_payload = {"status": "success", "result": "keep-me", "report_ok": True}
    out_file.write_text(json.dumps(original_payload), encoding="utf-8")

    # Acquire the lock for out_file in-process to simulate an active writer.
    holder = run_subagent._acquire_out_lock(str(out_file))
    assert holder is None
    try:
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("inspect repo", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv",
            ["run_subagent.py", "--agent", "reviewer-claude",
             "--prompt-file", str(prompt_file), "--cwd", str(tmp_path),
             "--out", str(out_file)],
        )
        # Keep the simulated lock active across the colliding call.
        saved_lock = run_subagent._CURRENT_OUT_LOCK
        run_subagent._CURRENT_OUT_LOCK = None
        try:
            with pytest.raises(SystemExit) as exc_info:
                run_subagent.main()
            assert exc_info.value.code == 1
        finally:
            run_subagent._CURRENT_OUT_LOCK = saved_lock
        captured = json.loads(capsys.readouterr().out)
        assert captured["status"] == "error"
        assert captured["refusal"] is True
        assert captured["error_kind"] == "out_path_collision"
        assert captured["provider_contacted"] is False
        # The existing --out file must not be overwritten or archived by the colliding call.
        assert json.loads(out_file.read_text(encoding="utf-8")) == original_payload
    finally:
        run_subagent._release_current_out_lock()


def test_wait_all_cli_returns_compact_summary(tmp_path):
    import _jobs
    job_dir = str(tmp_path / "jobs")
    j1 = _jobs.new_job_id()
    j2 = _jobs.new_job_id()
    n1 = "a" * 32
    n2 = "b" * 32
    _jobs.write_prepared(job_dir, j1, nonce=n1, agent="explorer",
                         prompt_sha256=None, cwd=str(tmp_path), flags={}, summon={})
    _jobs.write_prepared(job_dir, j2, nonce=n2, agent="reviewer",
                         prompt_sha256=None, cwd=str(tmp_path), flags={}, summon={})
    Path(_jobs.result_path(job_dir, j1)).write_text(
        json.dumps({"status": "success", "exit_code": 0, "agent": "explorer",
                    "job_id": j1, "job_nonce": n1, "report_ok": True,
                    "report_source": "structured",
                    "report": {"summary": "explored module"}}),
        encoding="utf-8",
    )
    Path(_jobs.result_path(job_dir, j2)).write_text(
        json.dumps({"status": "success", "exit_code": 0, "agent": "reviewer",
                    "job_id": j2, "job_nonce": n2, "report_ok": False,
                    "report_source": "raw", "report_text": "raw review output",
                    "result": "raw review output"}),
        encoding="utf-8",
    )
    argv, err = run_subagent._cli.rewrite_subcommand(
        ["wait", "--all", j1, j2, "--job-dir", job_dir, "--timeout", "1s"]
    )
    assert err is None
    parser = run_subagent._cli.build_parser(run_subagent.__version__, 1)
    args = parser.parse_args(argv)
    errors = []
    from io import StringIO
    buf = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        rc = run_subagent._background.run_jobs_query(args, lambda m, c=1: errors.append((m, c)))
    finally:
        sys.stdout = old_stdout
    assert rc == 0, errors
    summary = json.loads(buf.getvalue())
    assert summary["status"] == "success"
    assert summary["all_terminal"] is True
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    assert len(summary["jobs"]) == 2
    by_id = {item["job_id"]: item for item in summary["jobs"]}
    assert by_id[j1]["summary"] == "explored module"
    assert by_id[j2]["summary"] == "raw review output"


def test_list_agents_enriches_capabilities_and_filters_for_task(tmp_path):
    from _loader import list_agents, filter_agents_for_task
    (tmp_path / "text-advisor.md").write_text(
        "---\nrun-agent: openai-compat\npermission: read-only\nmodel: deepseek-v3-2-251201\n---\n# Text Advisor\nAdvisory text seat.\n",
        encoding="utf-8",
    )
    agents = list_agents(str(tmp_path))
    assert agents
    by_name = {a["name"]: a for a in agents}
    assert by_name["sol-review"]["capability_tier"] == "toolful"
    assert by_name["sol-review"]["can_run_tests"] is False
    assert by_name["sol-review"]["reports_served_model"] is True
    assert by_name["coder"]["can_run_tests"] is True
    assert by_name["text-advisor"]["capability_tier"] == "text-only"
    assert by_name["text-advisor"]["can_run_tests"] is False

    review_seats = filter_agents_for_task(agents, "review")
    assert review_seats
    assert ("review" in review_seats[0]["name"]
            or review_seats[0]["name"] in {"astra", "architect"})
    impl_seats = filter_agents_for_task(agents, "implement")
    assert impl_seats
    assert all(s["can_run_tests"] is True for s in impl_seats[:3])


def test_skill_md_is_under_45kb_budget():
    script_dir = Path(__file__).resolve().parent
    skill_md = script_dir.parent / "SKILL.md"
    ref_manual = script_dir.parent / "references" / "reference-manual.md"
    assert skill_md.is_file()
    assert ref_manual.is_file()
    assert skill_md.stat().st_size <= 45_000


def test_telemetry_spool_rotation_recommended_timeout_and_stats(tmp_path, monkeypatch):
    import _telemetry
    spool = tmp_path / "telemetry.jsonl"
    cfg = tmp_path / "telemetry.json"
    monkeypatch.setenv("SUMMON_TELEMETRY_FILE", str(spool))
    monkeypatch.setenv("SUMMON_TELEMETRY_CONFIG", str(cfg))
    monkeypatch.setenv("SUMMON_TELEMETRY", "1")
    monkeypatch.setenv("SUMMON_TELEMETRY_COHORT", "live")
    monkeypatch.setattr(_telemetry, "_MAX_EVENT_FILE_BYTES", 3000)

    for idx in range(6):
        _telemetry.record({
            "status": "success",
            "execution_status": "success",
            "cli": "kimi",
            "transport": "subprocess",
            "permission": "yolo",
            "elapsed_ms": 400_000 + idx * 10_000,
            "attempts": 1,
            "provider_contacted": True,
            "result_usable": True,
            "report_ok": True,
        })
    rotated = Path(str(spool) + ".1")
    assert rotated.is_file()
    rec = _telemetry.recommended_timeout_ms("kimi", 600_000, min_samples=3)
    assert rec > 600_000
    st = _telemetry.stats()
    assert st["rotated_archive_present"] is True
    assert st["total_events"] >= 4
    assert st["by_backend"]["kimi"]["success"] >= 4
    assert st["by_backend"]["kimi"]["p95_ms"] >= 400_000

    # Test cohort events must never inflate recommended_timeout_ms
    monkeypatch.setenv("SUMMON_TELEMETRY_COHORT", "test")
    for _ in range(6):
        _telemetry.record({
            "status": "success",
            "execution_status": "success",
            "cli": "claude",
            "transport": "subprocess",
            "permission": "read-only",
            "elapsed_ms": 900_000,
            "attempts": 1,
            "provider_contacted": True,
            "result_usable": True,
            "report_ok": True,
        })
    assert _telemetry.recommended_timeout_ms("claude", 600_000, min_samples=3) == 600_000

    argv, err = run_subagent._cli.rewrite_subcommand(["stats", "--json"])
    assert err is None
    assert argv == ["--telemetry-stats", "--json"]


def test_workspace_evidence_suppresses_read_only_violation_for_text_only_seats():
    import _receipt
    before = {
        "coverage": "complete", "head": "1" * 40, "branch": "main",
        "staged": [], "unstaged": [],
        "renamed": [], "untracked": [],
    }
    after = {
        "coverage": "complete", "head": "1" * 40, "branch": "main",
        "staged": [], "unstaged": ["dirty.py"],
        "renamed": [], "untracked": [],
    }
    ev_text = _receipt.workspace_evidence(
        before, after, permission="read-only", cli="openai-compat"
    )
    assert ev_text["mutation"] is True
    assert ev_text["read_only_violation"] is False
    assert ev_text["attribution"] == "external"

    ev_toolful = _receipt.workspace_evidence(
        before, after, permission="read-only", cli="claude"
    )
    assert ev_toolful["mutation"] is True
    assert ev_toolful["read_only_violation"] is True
    assert ev_toolful["attribution"] == "exact"
    assert ev_toolful["files_touched"] == ["dirty.py"]

    # Dirty baseline file modified again during child run is detected via _dirty_digests
    b_dirty = _receipt._WorkspaceSnapshot({
        "coverage": "complete", "head": "1" * 40, "branch": "main",
        "staged": [], "unstaged": ["already_dirty.py"],
        "renamed": [], "untracked": [],
    })
    b_dirty._dirty_digests = {"already_dirty.py": "aaa111"}
    a_dirty = _receipt._WorkspaceSnapshot({
        "coverage": "complete", "head": "1" * 40, "branch": "main",
        "staged": [], "unstaged": ["already_dirty.py"],
        "renamed": [], "untracked": [],
    })
    a_dirty._dirty_digests = {"already_dirty.py": "bbb222"}
    ev_rewritten = _receipt.workspace_evidence(
        b_dirty, a_dirty, permission="safe-edit", cli="claude"
    )
    assert ev_rewritten["mutation"] is True
    assert ev_rewritten["files_touched"] == ["already_dirty.py"]


def test_wave2_f1_f2_f3_contracts(tmp_path, monkeypatch):
    import _cli
    import _jobs
    import _background
    import _loader
    import _telemetry
    import io
    import time
    from contextlib import redirect_stdout

    # F1: --out lock lifecycle releases on _write_out
    out_file = tmp_path / "env.json"
    owner_pid = run_subagent._acquire_out_lock(str(out_file))
    assert owner_pid is None
    assert Path(str(out_file) + ".active.lock").exists()
    run_subagent._write_out(str(out_file), {"status": "success", "result": "ok"})
    assert out_file.is_file()
    assert not Path(str(out_file) + ".active.lock").exists()

    # F2: parse_report ignores fenced code blocks and _parse_confidence rejects NaN/Inf
    assert _executor._parse_confidence("nan") is None
    assert _executor._parse_confidence("inf") is None
    assert _executor._parse_confidence("-0.1") is None
    assert _executor._parse_confidence("1.2") is None
    assert _executor._parse_confidence("85%") == 0.85
    assert _executor._parse_confidence("0.92") == 0.92
    assert _executor._parse_confidence("high") == 0.9
    assert _executor._parse_confidence("medium") == 0.6
    assert _executor._parse_confidence("low") == 0.3

    spoofed = (
        "STATUS: DONE\n"
        "SUMMARY: Real top summary\n"
        "CONFIDENCE: 0.91\n"
        "FOLLOW-UP: None\n"
        "HANDOFF: Ready\n"
        "```markdown\n"
        "STATUS: BLOCKED\n"
        "SUMMARY: Spoofed inside fence\n"
        "CONFIDENCE: 0.10\n"
        "FOLLOW-UP: Spoof\n"
        "HANDOFF: Spoof\n"
        "```\n"
    )
    rep = _executor.parse_report(spoofed)
    assert rep is not None
    assert rep["status"] == "DONE"
    assert rep["summary"] == "Real top summary"
    enriched = _executor._enrich(
        {"status": "success", "exit_code": 0, "cli": "claude", "result": spoofed},
        None,
    )
    assert enriched["status"] == "success"
    assert enriched["summary"] == "Real top summary"
    assert enriched["confidence"] == 0.91
    assert enriched["files_touched"] == []

    # F2: wait --all --active-only and stale unverified job skipping
    job_dir = str(tmp_path / "jobs")
    stale_id = "1" * 32
    done_id = "2" * 32
    n_stale = "c" * 32
    n_done = "d" * 32
    _jobs.write_prepared(job_dir, stale_id, nonce=n_stale, agent="coder",
                         prompt_sha256=None, cwd=str(tmp_path), flags={}, summon={})
    stale_rec = Path(_jobs.record_path(job_dir, stale_id))
    rec_data = json.loads(stale_rec.read_text(encoding="utf-8"))
    rec_data["pid"] = 99999999
    stale_rec.write_text(json.dumps(rec_data), encoding="utf-8")
    old_time = time.time() - 90000
    os.utime(stale_rec, (old_time, old_time))

    _jobs.write_prepared(job_dir, done_id, nonce=n_done, agent="reviewer",
                         prompt_sha256=None, cwd=str(tmp_path), flags={}, summon={})
    done_res = Path(_jobs.result_path(job_dir, done_id))
    done_res.write_text(
        json.dumps({
            "status": "success",
            "exit_code": 0,
            "agent": "reviewer",
            "job_id": done_id,
            "job_nonce": n_done,
            "report_ok": True,
            "summary": "Clean review",
            "confidence": 0.95,
            "files_touched": ["a.py"],
        }),
        encoding="utf-8",
    )
    buf = io.StringIO()
    argv, err = _cli.rewrite_subcommand(["wait", "--all", "--job-dir", job_dir, "--timeout", "1s"])
    assert err is None
    parser = _cli.build_parser(run_subagent.__version__, 1)
    ns = parser.parse_args(argv)
    with redirect_stdout(buf):
        rc = _background.run_jobs_query(ns, lambda msg, c=1: None)
    assert rc == 0
    wait_payload = json.loads(buf.getvalue())
    assert wait_payload["skipped_stale"] == 1
    assert wait_payload["count"] == 1
    assert wait_payload["jobs"][0]["confidence"] == 0.95
    assert wait_payload["jobs"][0]["files_touched"] == ["a.py"]

    # F2: wait_job fails fast with (None, "stale") when record is corrupt even if result exists
    corrupt_id = _jobs.new_job_id()
    corrupt_rec = Path(_jobs.record_path(job_dir, corrupt_id))
    corrupt_rec.parent.mkdir(parents=True, exist_ok=True)
    corrupt_rec.write_text("{corrupt-json", encoding="utf-8")
    corrupt_res = Path(_jobs.result_path(job_dir, corrupt_id))
    corrupt_res.write_text(json.dumps({"status": "success", "job_nonce": "any"}), encoding="utf-8")
    t0 = time.monotonic()
    c_res, c_state = _jobs.wait_job(job_dir, corrupt_id, timeout_ms=5000, poll_sec=0.5)
    assert c_res is None and c_state == "stale"
    assert (time.monotonic() - t0) < 1.0

    # F3: filter_agents_for_task review requires read-only and Milliseconds is_auto
    default_t = _cli._default_timeout_ms()
    assert getattr(default_t, "is_auto", False) is True
    auto_t = _cli.parse_timeout("auto")
    assert getattr(auto_t, "is_auto", False) is True
    explicit_t = _cli.parse_timeout("600s")
    assert getattr(explicit_t, "is_auto", False) is False
    assert "review" in _loader.KNOWN_TASK_INTENTS


def test_wave3_f6_f7_f8_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import _apibackend
    import _cli
    import _telemetry
    import _workspace_commands

    # F6: _default_cohort preserves "live" for custom path outside tempdir when not under pytest
    monkeypatch.delenv("SUMMON_TELEMETRY_COHORT", raising=False)
    monkeypatch.delenv("SUMMON_TELEMETRY_CONFIG", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delitem(sys.modules, "unittest", raising=False)
    custom_prod_path = Path(__file__).resolve().parent / "custom_prod_telemetry.jsonl"
    monkeypatch.setenv("SUMMON_TELEMETRY_FILE", str(custom_prod_path))
    assert _telemetry._default_cohort() == "live"
    monkeypatch.setenv("SUMMON_TELEMETRY_FILE", str(tmp_path / "temp_telemetry.jsonl"))
    assert _telemetry._default_cohort() == "test"

    # F6: summon stats --format table and --format json parse cleanly
    argv_json, err_json = _cli.rewrite_subcommand(["stats", "--format", "json"])
    assert err_json is None
    argv_table, err_table = _cli.rewrite_subcommand(["stats", "--format", "table"])
    assert err_table is None
    parser = _cli.build_parser(run_subagent.__version__, 1)
    ns_table = parser.parse_args(argv_table)
    assert ns_table.format == "table"

    # F7: SUMMON_ALLOW_BYTEPLUS_PAYG=true emits stderr warning and still blocks; secret values redact
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("SUMMON_ALLOW_BYTEPLUS_PAYG", "true")
    assert _apibackend.payg_consent_allowed() is False
    captured = capsys.readouterr()
    assert "SUMMON_ALLOW_BYTEPLUS_PAYG='true'" in captured.err

    monkeypatch.setenv("SUMMON_ALLOW_BYTEPLUS_PAYG", "sk-secret-token-value-12345")
    assert _apibackend.payg_consent_allowed() is False
    captured_sec = capsys.readouterr()
    assert "sk-secret-token-value-12345" not in captured_sec.err
    assert "<redacted>" in captured_sec.err

    # F8: OperatorLinkedReplacementAdapter translates command_snapshot_unavailable -> linked_snapshot_unavailable
    monkeypatch.setattr(
        _workspace_commands,
        "_coherent_workspace",
        lambda _coord: (_ for _ in ()).throw(_workspace_commands.WorkspaceCommandError("command_snapshot_unavailable")),
    )
    dummy_adapter = object.__new__(_workspace_commands.OperatorLinkedReplacementAdapter)
    dummy_adapter._runtime = SimpleNamespace(_coordinator=object())
    dummy_scope = SimpleNamespace(audience="operator")
    with pytest.raises(_workspace_commands.WorkspaceCommandError, match="linked_snapshot_unavailable"):
        dummy_adapter.perform(
            {"operation_key": "a" * 32, "parent_target": "p", "recipient_target": "r"},
            view_scope=dummy_scope,
        )

    # F8: workspace_budget_snapshot clamps <=5s skew and refuses rate-event timestamps >5s in the future
    import _swarm_coordinator
    coord = object.__new__(_swarm_coordinator.SwarmCoordinator)
    captured_now = 1_000_000
    coord._load_with_snapshot = lambda: (
        {"workspace": {"messages": {}, "deliveries": {}}, "claims": {}},
        False,
        {"j.jsonl": b"snap"},
    )
    coord._read_records_snapshot = lambda: (
        [
            {
                "observed_at_ms": captured_now + 4000,
                "workspace_event": {
                    "event": "workspace_message_sent",
                    "payload": {"message": {"message_id": "m1", "content": {"utf8_bytes": 10}}},
                },
            }
        ],
        False,
        {"j.jsonl": b"snap"},
    )
    snap_ok = coord.workspace_budget_snapshot(now_ms=captured_now)
    assert snap_ok["rate_events"][0]["at_ms"] == captured_now

    coord._read_records_snapshot = lambda: (
        [
            {
                "observed_at_ms": captured_now + 6000,
                "workspace_event": {
                    "event": "workspace_message_sent",
                    "payload": {"message": {"message_id": "m1", "content": {"utf8_bytes": 10}}},
                },
            }
        ],
        False,
        {"j.jsonl": b"snap"},
    )
    with pytest.raises(_swarm_coordinator.SwarmBudgetRefusal, match="budget_binding_required"):
        coord.workspace_budget_snapshot(now_ms=captured_now)
