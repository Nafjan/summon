"""Regression tests for Summon v3.8.1 hindrance fixes (Items 1-19 + gate lockstep)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _backend_policy
import _background
import _builder
import _executor
import _gate
import _jobs
import _profiles
import _receipt
import _stream
import _submission_accounting
import _zcode
import run_subagent as rs


def _done_report(summary: str = "Implemented requested change", files: str = "a.py") -> str:
    return (
        "STATUS: DONE\n"
        f"SUMMARY: {summary}\n"
        "CONFIDENCE: high\n"
        f"FILES: {files}\n"
        "TESTS: pytest -q passed\n"
        "HANDOFF: none\n"
        "FOLLOW-UP: none\n"
        "LEFT_BEHIND: none\n"
    )


def _run_cli(argv: list[str], *, cwd: Path | None = None, stdin_text: str | None = None):
    env = os.environ.copy()
    env.pop("SUMMON_CMD_LAUNCHER", None)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "run_subagent.py"), *argv],
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# --- A. Wrong status signals (Items 1-6) ------------------------------------

def test_item01_success_with_raw_exit_1_normalizes_exit_code_to_zero():
    env = {
        "status": "success",
        "exit_code": 1,
        "result": _done_report(),
        "cli": "agy",
    }
    _executor.finalize_exit_fields(env)
    assert env["status"] == "success"
    assert env["exit_code"] == 0
    assert env["normalized_exit_code"] == 0
    assert env["raw_backend_exit_code"] == 1


@pytest.mark.parametrize(
    "post_error",
    [
        "response cut off due to output token limit (Retries remaining: 3)",
        "Subscriber fell behind updates, stalled for 8s",
    ],
)
def test_item02_and_06_post_done_output_limit_or_stall_rescued_with_warning(post_error):
    proc = _stream.StreamProcessor(cli="agy")
    proc.process_line(json.dumps({
        "event": "result",
        "result": {
            "status": "error",
            "response": _done_report("All checks passed"),
            "error": post_error,
        },
    }))
    proc.finalize_stream()
    res_obj = proc.get_result()
    resp = _executor._enrich({
        "status": "error",
        "exit_code": 1,
        "raw_backend_exit_code": 1,
        "result": res_obj["result"] if isinstance(res_obj, dict) else res_obj,
        "error": post_error,
        "cli": "agy",
        "attempt_status": "error",
        "provider_contacted": True,
    }, proc)
    assert resp["status"] == "success"
    assert resp["exit_code"] == 0
    assert resp["completed_before_cutoff"] is True
    assert resp["provider_post_report_error"] == post_error
    assert any("STATUS: DONE" in w for w in resp.get("warnings", []))


def test_item03_background_refuses_bad_cwd_before_returning_job_id(tmp_path):
    job_dir = tmp_path / "jobs"
    missing_cwd = tmp_path / "does-not-exist"
    proc = _run_cli([
        "--cli", "codex",
        "--cwd", str(missing_cwd),
        "--prompt", "check status",
        "--background",
        "--job-dir", str(job_dir),
    ])
    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload.get("job_id") is None
    assert payload.get("provider_contacted") is False
    assert not list(job_dir.glob("*.json")) if job_dir.exists() else True


def test_item04_empty_readonly_result_never_succeeds_and_jobs_full_shows_results(tmp_path, capsys):
    # 1) Empty / whitespace-only result on success is downgraded to error.
    empty_env = {
        "status": "success",
        "exit_code": 0,
        "result": "   \n\t  ",
        "cli": "codex",
        "attempt_status": "success",
    }
    _executor.normalize_empty_success(empty_env)
    assert empty_env["status"] == "error"
    assert empty_env["error_kind"] == "empty_terminal_result"
    assert empty_env["result_usable"] is False

    # 2) Background jobs status keeps default redacted, reveals on --full, and wait --all includes report fields.
    root = str(tmp_path / "jobs")
    _jobs.ensure_jobs_dir(root)
    jid = _jobs.new_job_id()
    nonce = "ab" * 32
    _jobs.write_prepared(
        root, jid, nonce=nonce, agent="luna-review",
        prompt_sha256="c" * 64, cwd=str(tmp_path),
        flags={"agent": "luna-review"},
        summon={"version": rs.__version__, "script": "run_subagent.py"},
    )
    term = {
        "status": "success",
        "exit_code": 0,
        "result_usable": True,
        "agent": "luna-review",
        "cli": "codex",
        "result": "Secret detailed review finding",
        "final_result": "Final finding summary",
        "report_text": "Structured review text",
        "summary": "Review approved",
        "report": {"status": "DONE", "summary": "Review approved", "verdict": "PASS"},
        "verdict": "PASS",
    }
    _jobs._atomic_write_json(
        _jobs.result_path(root, jid),
        {**term, "job_nonce": nonce},
    )

    errors = []
    emit_err = lambda msg, exit_code=1: errors.append((msg, exit_code))
    default_args = SimpleNamespace(
        job_dir=root,
        jobs_list=False, jobs_watch=False, jobs_status=jid, jobs_full=False,
        jobs_wait=None, jobs_wait_all=False, jobs_extend=None, jobs_cancel=None,
        jobs_steer=None, jobs_resume=None, jobs_revalidate=None, json=True,
    )
    capsys.readouterr()
    assert _background.run_jobs_query(default_args, emit_err) == 0, errors
    default_out = json.loads(capsys.readouterr().out)
    assert "result_file" not in default_out
    assert "result" not in (default_out.get("result") or {})

    full_args = SimpleNamespace(**{**default_args.__dict__, "jobs_full": True})
    assert _background.run_jobs_query(full_args, emit_err) == 0, errors
    full_out = json.loads(capsys.readouterr().out)
    assert full_out["result_file"] == _jobs.result_path(root, jid)
    assert full_out["result"]["result"] == "Secret detailed review finding"
    assert full_out["result"]["final_result"] == "Final finding summary"
    assert full_out["result"]["verdict"] == "PASS"

    wait_args = SimpleNamespace(**{
        **default_args.__dict__,
        "jobs_status": None,
        "jobs_wait_all": True,
        "jobs_wait_ids": [jid],
        "jobs_active_only": False,
        "timeout": 5000,
    })
    assert _background.run_jobs_query(wait_args, emit_err) == 0, errors
    wait_out = json.loads(capsys.readouterr().out)
    item = wait_out["jobs"][0]
    assert item["wait_outcome"] == "done"
    assert item["status"] == "success"
    assert item["summary"] == "Review approved"
    assert item["final_result"] == "Final finding summary"
    assert item["verdict"] == "PASS"


def test_item05_out_lock_blocks_concurrent_launch_and_background_allows_out_handoff(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "review.json"
    lock_path = os.path.abspath(str(out_file)) + ".active.lock"
    rs._release_current_out_lock()
    try:
        owner = rs._acquire_out_lock(str(out_file))
        assert owner is None
        assert os.path.isfile(lock_path)

        # Second process sees live owner PID holding --out lock and fails closed pre-dispatch.
        proc = _run_cli([
            "--cli", "codex",
            "--cwd", str(tmp_path / "missing-for-safety"),
            "--prompt", "check out lock",
            "--out", str(out_file),
        ])
        assert proc.returncode != 0
        err_payload = json.loads(proc.stdout)
        assert err_payload.get("error_kind") == "out_path_collision"
        assert "already locked" in proc.stdout
    finally:
        rs._release_current_out_lock()
        assert not os.path.exists(lock_path)

    # Background + --out is permitted and spawns with lock handoff instead of rejecting.
    job_dir = tmp_path / "jobs"
    monkeypatch.setattr(rs, "_spawn_background", lambda args, out_lock_handoff=None: {
        "job_id": "job-test123",
        "state": "running",
        "out_file": args.out,
        "handoff_received": out_lock_handoff is not None,
        "handoff_path": out_lock_handoff[0] if out_lock_handoff else None,
    })
    monkeypatch.setattr(sys, "argv", [
        "run_subagent.py",
        "--cli", "codex",
        "--cwd", str(tmp_path),
        "--prompt", "bg out",
        "--background",
        "--out", str(out_file),
        "--job-dir", str(job_dir),
    ])
    capsys.readouterr()
    try:
        with pytest.raises(SystemExit) as exc:
            rs.main()
        assert exc.value.code == 0
        bg_res = json.loads(capsys.readouterr().out)
        assert bg_res["job_id"] == "job-test123"
        assert bg_res["out_file"] == str(out_file)
        assert bg_res["handoff_received"] is True
        assert os.path.normcase(bg_res["handoff_path"]) == os.path.normcase(lock_path)
    finally:
        rs._release_current_out_lock()
        if os.path.exists(lock_path):
            os.unlink(lock_path)


def test_item06_long_agy_stream_buffers_tools_and_keeps_partial_on_eof():
    proc = _stream.StreamProcessor(cli="agy")
    for idx in range(215):
        proc.process_line(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": idx,
                "step_type": "tool",
                "tool_name": "read_file",
                "state": "completed",
            },
        }))
    proc.process_line(json.dumps({
        "event": "step_update",
        "step_update": {
            "step_index": 215,
            "step_type": "assistant",
            "text_delta": "Findings after 215 tool calls:\nAll clear.",
        },
    }))
    proc.finalize_stream()
    res = proc.get_result()
    assert isinstance(res, dict)
    assert "Findings after 215 tool calls" in res.get("partial_result", "")
    assert "Findings after 215 tool calls" in (proc.final_message or "")


# --- B. Evidence fields that look stronger than they are (Items 7-11) -------

def test_item07_unverified_served_model_never_copies_requested():
    env = {
        "status": "success",
        "exit_code": 0,
        "result": _done_report(),
        "cli": "agy",
        "model": {"requested": "gemini-4-argon-high", "served": None},
    }
    resp = _executor._enrich(env, None)
    assert resp["model"]["requested"] is None or isinstance(resp["model"]["requested"], str)
    # Specifically test execute_agent's _stamp normalization invariant via served_model_evidence:
    # Build minimal response matching _executor's model block guard.
    proc = _stream.StreamProcessor(cli="agy")
    proc.process_line(json.dumps({"event": "init", "model": "gemini-4-argon-high"}))
    proc.process_line(json.dumps({
        "event": "result",
        "result": {"status": "DONE", "response": _done_report()},
    }))
    proc.finalize_stream()
    assert proc.model is None
    assert proc.handshake_model == "gemini-4-argon-high"


def test_item08_and_19_agy_reports_sandbox_and_read_allowlist_unenforced(tmp_path):
    assert _backend_policy.is_sandbox_enforced("agy", "read-only") is False
    assert _backend_policy.is_sandbox_enforced("agy", "safe-edit") is False
    assert _backend_policy.is_sandbox_enforced("agy", "yolo") is False
    assert _backend_policy.is_sandbox_enforced("codex", "read-only") is True

    allow = _builder.read_allowlist("agy", "yolo", str(tmp_path))
    assert allow["enforced"] is False

    proc = _run_cli([
        "--cli", "agy",
        "--permission", "yolo",
        "--cwd", str(tmp_path),
        "--prompt", "inspect tree",
        "--dry-run",
    ])
    assert proc.returncode == 0
    dry = json.loads(proc.stdout)
    assert dry["ad_hoc"] is True
    assert dry["sandbox_enforced"] is False
    assert dry["read_allowlist"]["enforced"] is False


def test_item09_shared_tree_attribution_and_unreported_files_warning():
    before = {
        "coverage": "complete",
        "head": "abc123",
        "branch": "main",
        "staged": [],
        "unstaged": [],
        "renamed": [],
        "untracked": [],
    }
    after = {
        "coverage": "complete",
        "head": "abc123",
        "branch": "main",
        "staged": [],
        "unstaged": ["agent_edit.py", "other_writer.py"],
        "renamed": [],
        "untracked": ["helper.tmp"],
    }
    ev = _receipt.workspace_evidence(
        before, after, permission="yolo", cli="agy",
        reported_files=["agent_edit.py"],
        concurrent_shared_tree=True,
    )
    assert ev["attribution"] == "shared_tree"
    assert sorted(ev["unreported_tree_changes"]) == ["helper.tmp", "other_writer.py"]


def test_item10_provider_contact_counters_stay_consistent():
    rec = _submission_accounting.private_record(
        attempt_id="b" * 32,
        attempt_kind="initial",
        attempt_ordinal=1,
        parent_attempt_id=None,
        request_sha256="a" * 64,
        estimate={"method": "utf8_bytes_divided_by_4_ceiling",
                  "represented_bytes": 40, "estimated_tokens": 10},
        envelope={"provider_contacted": True, "status": "success",
                  "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}},
        submission_state="submitted",
    )
    summary = _submission_accounting.public_summary([rec])
    assert summary["physical_submissions"] == 1
    assert summary["physical_attempts"] == 1
    assert summary["indeterminate_contact"] == 0

    not_sub_rec = _submission_accounting.private_record(
        attempt_id="c" * 32,
        attempt_kind="initial",
        attempt_ordinal=1,
        parent_attempt_id=None,
        request_sha256="a" * 64,
        estimate={"method": "utf8_bytes_divided_by_4_ceiling",
                  "represented_bytes": 40, "estimated_tokens": 10},
        envelope={"provider_contacted": False, "status": "error", "attempt_status": "completed"},
    )
    not_sub_summary = _submission_accounting.public_summary([not_sub_rec])
    assert not_sub_summary["physical_submissions"] == 0
    assert not_sub_summary["physical_attempts"] == 0
    assert not_sub_summary["indeterminate_contact"] == 0

    subproc_rec = _submission_accounting.private_record(
        attempt_id="d" * 32,
        attempt_kind="initial",
        attempt_ordinal=1,
        parent_attempt_id=None,
        request_sha256="a" * 64,
        estimate={"method": "utf8_bytes_divided_by_4_ceiling",
                  "represented_bytes": 40, "estimated_tokens": 10},
        envelope={"provider_contacted": True, "status": "success",
                  "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}},
    )
    subproc_summary = _submission_accounting.public_summary([subproc_rec])
    assert subproc_summary["physical_submissions"] == 0
    assert subproc_summary["physical_attempts"] == 1
    assert subproc_summary["indeterminate_contact"] == 1


def test_item11_effort_downgrade_surfaces_warning_and_requested_field(tmp_path):
    applied, warn = _builder.effective_effort("codex", "gpt-5.4", "max")
    assert applied == "high"
    assert warn and "downgraded" in warn

    applied_agy, warn_agy = _builder.effective_effort("agy", "gemini-4-argon", "max")
    assert applied_agy == "high"
    assert warn_agy and "downgraded" in warn_agy

    proc = _run_cli([
        "--cli", "codex",
        "--model", "gpt-5.4",
        "--effort", "max",
        "--cwd", str(tmp_path),
        "--prompt", "check effort",
        "--dry-run",
    ])
    assert proc.returncode == 0
    dry = json.loads(proc.stdout)
    assert dry["effort"] == "high"
    assert dry["effort_requested"] == "max"
    assert dry["effort_downgraded"] is True
    assert any("downgraded" in w for w in dry.get("warnings", []))


# --- C. Adapters that failed where the bare CLI worked (Items 12-15) --------

def test_item12_codex_profile_auto_enables_login_flags_and_env(tmp_path, monkeypatch):
    work_cwd = tmp_path / "repo"
    work_cwd.mkdir()
    profile_dir = tmp_path / "codex-work-home"
    profile_dir.mkdir()
    (profile_dir / "auth.json").write_text('{"tokens": {"access_token": "tok"}}', encoding="utf-8")
    reg_path = tmp_path / "summon-profiles.json"
    reg_path.write_text(json.dumps({
        "profiles": {
            "codex-work": {
                "cli": "codex",
                "config_dir": str(profile_dir),
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(_profiles, "registry_path", lambda: str(reg_path))

    sel = _profiles.resolve_profile("codex-work", "codex", cwd=str(work_cwd))
    assert sel["auth_mode"] == "login"

    inv = _builder.AgentInvocation(
        cli="codex",
        prompt="hi",
        cwd=str(work_cwd),
        model="gpt-5.4",
        permission="read-only",
        profile="codex-work",
        profile_env=sel["env"],
        profile_auth_mode=sel["auth_mode"],
    )
    _cmd, argv, env = _builder._build_codex_args(inv)
    assert 'cli_auth_credentials_store="file"' in argv
    assert 'forced_login_method="chatgpt"' in argv
    assert 'model_provider="openai"' in argv
    assert env["CODEX_HOME"] == str(profile_dir.resolve())
    assert env["OPENAI_API_KEY"] is None


def test_item13_zcode_config_status_and_model_selector_handling(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    missing = _zcode.zcode_config_status()
    assert missing["configured"] is False
    assert "zcode doctor" in missing["hint"]

    (tmp_path / ".zcode" / "cli").mkdir(parents=True)
    (tmp_path / ".zcode" / "cli" / "config.json").write_text("{}", encoding="utf-8")
    cli_only = _zcode.zcode_config_status()
    assert cli_only["cli_config_present"] is True
    assert cli_only["configured"] is True

    # Bare ZCode model without <provider>/ is refused cleanly pre-dispatch.
    inv = _builder.AgentInvocation(
        cli="zcode", prompt="test", cwd=str(tmp_path), permission="yolo",
        model="glm-5-3-flash",
    )
    preflight = _builder.zcode_invocation_preflight(inv)
    assert preflight is not None

    parsed = _zcode.zcode_result_fields({
        "response": "Done",
        "sessionId": "sess_abc123",
        "model": "zai-coding-plan/GLM-5.3-Flash",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "projection": {"contextWindow": 128000},
    })
    assert parsed["parse_ok"] is True
    assert parsed["session_id"] == "sess_abc123"
    assert parsed["model"] == "zai-coding-plan/GLM-5.3-Flash"


def test_item14_opencode_local_windows_cwd_allowed_and_early_stop_headline(tmp_path):
    # OpenCode allowlist must permit local working directories (not reject non-temp local drives).
    policy = _builder.read_allowlist("opencode", "read-only", str(tmp_path))
    assert policy["would_refuse"] is False

    # Premature idle/startup stop must never claim full budget Timeout after 5400000ms.
    headline = _executor._timeout_headline(5_400_000, "generation_idle_timeout", 240_000)
    assert not headline.startswith("Timeout after 5400000ms")
    assert "240000ms" in headline
    assert "generation_idle_timeout" in headline


def test_item15_claude_safe_edit_preapproves_python_pytest_and_read_git(tmp_path):
    inv = _builder.AgentInvocation(
        cli="claude",
        prompt="run tests",
        cwd=str(tmp_path),
        permission="safe-edit",
        model="claude-sonnet-5",
    )
    _cmd, argv, _env = _builder._build_claude_args(inv)
    assert "--allowedTools" in argv
    tools = argv[argv.index("--allowedTools") + 1]
    for expected in ("Bash(python:*)", "Bash(pytest:*)", "Bash(git status*)"):
        assert expected in tools


# --- D. Zero-ceremony UX, compact overview, and split notes (Items 16-19) ---

def test_item16_zero_ceremony_adhoc_stdin_and_default_cwd_and_ps1(tmp_path):
    # 1) Relative --cwd + --prompt-file - (stdin) + --cli without --agent.
    rel = tmp_path.name
    proc = _run_cli(
        [
            "--cli", "codex",
            "--permission", "safe-edit",
            "--cwd", rel,
            "--prompt-file", "-",
            "--dry-run",
        ],
        cwd=tmp_path.parent,
        stdin_text="Line one\nLine two from stdin",
    )
    assert proc.returncode == 0, proc.stderr
    dry = json.loads(proc.stdout)
    assert dry["ad_hoc"] is True
    assert dry["permission"] == "safe-edit"
    assert dry["cwd"] == str(tmp_path.resolve())
    assert "Line two from stdin" in dry["args"][-1]

    # 2) Omitting --cwd defaults to os.getcwd().
    proc_def_cwd = _run_cli(
        ["--cli", "codex", "--prompt", "-", "--dry-run"],
        cwd=tmp_path,
        stdin_text="default cwd prompt",
    )
    assert proc_def_cwd.returncode == 0
    assert json.loads(proc_def_cwd.stdout)["cwd"] == str(tmp_path.resolve())

    # 3) --permission with --agent is refused (must use --max-permission to clamp).
    proc_bad = _run_cli([
        "--agent", "pair",
        "--permission", "yolo",
        "--cwd", str(tmp_path),
        "--prompt", "forbidden",
        "--dry-run",
    ])
    assert proc_bad.returncode != 0
    assert "--permission is only valid with ad-hoc --cli" in proc_bad.stdout

    # 4) summon.ps1 no longer sets $env:SUMMON_CMD_LAUNCHER=1 or rejects multiline --prompt.
    ps1 = (SCRIPTS / "summon.ps1").read_text(encoding="utf-8")
    assert "$env:SUMMON_CMD_LAUNCHER" not in ps1
    assert "& py -3" in ps1


def test_item17_overview_first_and_scripts_hash_excludes_tests(tmp_path):
    projected = rs._public_submission_projection({
        "status": "success",
        "exit_code": 0,
        "result_usable": True,
        "agent": None,
        "cli": "codex",
        "model": {"requested": "gpt-5.4", "served": "gpt-5.4"},
        "served_model_evidence": "reported",
        "sandbox_enforced": True,
        "elapsed_ms": 123,
        "files_touched": ["a.py"],
        "workspace_evidence": {"attribution": "shared_tree"},
        "warnings": ["w1"],
    })
    assert list(projected.keys())[0] == "overview"
    ov = projected["overview"]
    assert ov["status"] == "success"
    assert ov["exit_code"] == 0
    assert ov["sandbox_enforced"] is True
    assert ov["files_touched_count"] == 1
    assert ov["files_attribution"] == "shared_tree"
    assert ov["warning_count"] == 1

    # Editing a test_* file does not alter scripts_sha256.
    (tmp_path / "run_subagent.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_demo.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    h1 = _receipt.scripts_sha256(str(tmp_path))
    (tmp_path / "test_demo.py").write_text("def test_a(): assert 1 == 1\n", encoding="utf-8")
    h2 = _receipt.scripts_sha256(str(tmp_path))
    assert h1 == h2


def test_item18_final_result_and_progress_notes_separated():
    proc = _stream.StreamProcessor(cli="codex")
    proc.process_line(json.dumps({"type": "thread.started", "thread_id": "th_1"}))
    proc.process_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Checking files..."}}))
    proc.process_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": _done_report("All fixed")}}))
    proc.process_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}))
    proc.finalize_stream()
    assert proc.progress_notes == ["Checking files..."]
    assert "All fixed" in (proc.final_message or "")

    res_obj = proc.get_result()
    resp = _executor._enrich({
        "status": "success",
        "exit_code": 0,
        "raw_backend_exit_code": 0,
        "result": res_obj["result"] if isinstance(res_obj, dict) else res_obj,
        "cli": "codex",
        "attempt_status": "success",
    }, proc)
    assert resp["progress_notes"] == ["Checking files..."]
    assert "All fixed" in resp["final_result"]
    assert "Checking files..." not in resp["final_result"]


def test_gate_markdown_verdicts_and_defang_lockstep():
    variants = [
        ("**VERDICT**: APPROVE", "APPROVE"),
        ("### `VERDICT`: DENY", "DENY"),
        ("- _VERDICT_: UNCERTAIN", "UNCERTAIN"),
        ("> **VERDICT:** APPROVE", "APPROVE"),
    ]
    for raw, expected in variants:
        assert _gate.parse_verdict(raw) == expected
        # Every parseable variant MUST be neutralized when passed through defang().
        defanged = _gate.defang(raw)
        assert _gate.parse_verdict(defanged) is None
