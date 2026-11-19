"""Regression tests for Summon v3.9.0 native --json-schema, consolidated _receipt
worktree scope & read-only --patch-out, --text/-t, --manifest -, and jobs wait --any."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _background
import _builder
import _cli
import _executor
import _jobs
import _manifest
import _receipt
import _schema
from _spawn import run_flags
import _stream
import run_subagent as rs


def _init_git_repo(root: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Summon Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Summon Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True, env=env, **run_flags())
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "outside.txt").write_text("keep\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True, env=env, **run_flags())
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True, env=env, **run_flags())
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True, **run_flags())
    return head.stdout.strip()


# --- Pillar 3: Native --json-schema compilation -------------------------------


def test_codex_strict_schema_compatibility_checks():
    strict_ok = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verdict", "items"],
        "additionalProperties": False,
    }
    assert _schema.is_codex_strict_schema_compatible(strict_ok) is True

    missing_ap = dict(strict_ok)
    missing_ap.pop("additionalProperties")
    assert _schema.is_codex_strict_schema_compatible(missing_ap) is False

    missing_req = dict(strict_ok, required=["verdict"])
    assert _schema.is_codex_strict_schema_compatible(missing_req) is False

    assert _schema.is_codex_strict_schema_compatible({"type": "array", "items": {"type": "string"}}) is False

    assert _schema.is_codex_strict_schema_compatible({
        "type": "object", "properties": {"a": {"$ref": "#/$defs/A"}},
        "required": ["a"], "additionalProperties": False,
    }) is False

    bad_nested = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    assert _schema.is_codex_strict_schema_compatible(bad_nested) is False


def test_claude_native_json_schema_build_stream_and_fallback(tmp_path):
    small_schema = json.dumps({
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }, separators=(",", ":"))
    inv = _builder.AgentInvocation(
        cli="claude", prompt="check", cwd=str(tmp_path),
        output_contract="schema", json_schema_inline=small_schema,
    )
    assert _builder.schema_transport_for(inv) == "claude-json-schema"
    _cmd, argv, _env = _builder.build_invocation_args(inv, dry_run=True)
    idx = argv.index("--json-schema")
    assert argv[idx + 1] == small_schema

    big_schema = json.dumps({
        "type": "object",
        "properties": {"blob": {"type": "string", "description": "x" * 17000}},
        "required": ["blob"],
        "additionalProperties": False,
    })
    big_inv = _builder.AgentInvocation(
        cli="claude", prompt="check", cwd=str(tmp_path),
        output_contract="schema", json_schema_inline=big_schema,
    )
    assert _builder.schema_transport_for(big_inv) == "prompt"
    _cmd2, argv2, _ = _builder.build_invocation_args(big_inv, dry_run=True)
    assert "--json-schema" not in argv2

    proc = _stream.StreamProcessor(cli="claude")
    terminal = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "",
        "structured_output": {"ok": True},
        "session_id": "33333333-3333-3333-3333-333333333333",
    })
    assert proc.process_line(terminal) is True
    assert proc.structured_output == {"ok": True}

    env_out = _executor._enrich(
        {"status": "success", "cli": "claude", "result": "", "exit_code": 0},
        proc,
        output_contract="schema",
    )
    assert env_out["status"] == "success"
    assert env_out["structured_output"] == {"ok": True}
    assert "suspect" not in env_out

    _schema.attach_parsed(env_out, json.loads(small_schema))
    assert env_out["parse_ok"] is True
    assert env_out["parsed"] == {"ok": True}


def test_codex_native_output_schema_dry_run_vs_live_cleanup(tmp_path):
    strict_schema = json.dumps({
        "type": "object",
        "properties": {"score": {"type": "integer"}},
        "required": ["score"],
        "additionalProperties": False,
    }, separators=(",", ":"))
    inv = _builder.AgentInvocation(
        cli="codex", prompt="score this", cwd=str(tmp_path),
        output_contract="schema", json_schema_inline=strict_schema,
    )
    assert _builder.schema_transport_for(inv) == "codex-output-schema"

    before_files = set(Path(tempfile.gettempdir()).glob("summon-codex-schema-*.json"))
    _cmd, dry_argv, _ = _builder.build_invocation_args(inv, dry_run=True)
    assert "--output-schema" in dry_argv
    idx = dry_argv.index("--output-schema")
    assert dry_argv[idx + 1] == "<temp-schema.json>"
    assert dry_argv[-1] != "--output-schema"
    after_dry = set(Path(tempfile.gettempdir()).glob("summon-codex-schema-*.json"))
    assert before_files == after_dry

    registered = []
    _cmd2, live_argv, env2 = _builder.build_invocation_args(
        inv, resource_register=lambda p, kind: registered.append((p, kind)), dry_run=False
    )
    live_idx = live_argv.index("--output-schema")
    schema_path = live_argv[live_idx + 1]
    assert Path(schema_path).is_file()
    assert json.loads(Path(schema_path).read_text(encoding="utf-8"))["required"] == ["score"]
    assert (schema_path, "codex-output-schema") in registered

    _executor._cleanup_launch_artifacts("codex", env2, live_argv)
    assert not Path(schema_path).exists()

    resume_inv = _builder.AgentInvocation(
        cli="codex", prompt="again", cwd=str(tmp_path),
        resume_id="44444444-4444-4444-4444-444444444444",
        output_contract="schema", json_schema_inline=strict_schema,
    )
    assert _builder.schema_transport_for(resume_inv) == "prompt"
    assert "Final report" not in _schema.correction_prompt({"type": "object"}, ["bad"])


def test_run_subagent_schema_end_to_end_marks_report_source_json_schema(tmp_path, monkeypatch):
    schema_file = tmp_path / "out.schema.json"
    schema_file.write_text(
        json.dumps({
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }),
        encoding="utf-8",
    )

    def fake_exec(inv, **kw):
        return {
            "status": "success",
            "execution_status": "success",
            "dispatcher_status": "success",
            "exit_code": 0,
            "raw_backend_exit_code": 0,
            "normalized_exit_code": 0,
            "cli": "claude",
            "result": '{"answer": "42"}',
            "structured_output": {"answer": "42"},
            "report": None,
            "report_ok": False,
            "output_contract": "schema",
            "attempts": 1,
            "attempt_id": inv.attempt_id,
            "resume": {"cli": "claude", "session_id": None},
        }

    emitted = []
    monkeypatch.setattr(rs, "_emit", lambda obj, **kw: emitted.append(obj))
    monkeypatch.setattr(rs, "execute_agent", fake_exec)
    monkeypatch.setattr(
        sys, "argv",
        ["run_subagent.py", "--cli", "claude", "--prompt", "hi", "--cwd", str(tmp_path), "--json-schema", str(schema_file)],
    )
    with pytest.raises(SystemExit) as exc:
        rs.main()
    assert exc.value.code == 0
    out = emitted[0]
    assert out["schema_transport"] == "claude-json-schema"
    assert out["parse_ok"] is True
    assert out["parsed"] == {"answer": "42"}
    assert out["report_ok"] is True
    assert out["report_source"] == "json_schema"
    assert "suspect" not in out


# --- Pillar 4: Consolidated _receipt worktree scope & --patch-out ------------


def test_receipt_worktree_scope_and_readonly_patch_export(tmp_path):
    assert _receipt.normalize_files_scope(["src/,tests/*.py", "./src/"]) == ["src/", "tests/*.py"]
    assert _receipt.path_matches_scope("src/nested/a.py", ["src/**/*.py"]) is True
    assert _receipt.path_matches_scope("src/a.py", ["src/**/*.py"]) is True
    assert _receipt.path_matches_scope("other/a.py", ["src/**/*.py"]) is False
    with pytest.raises(ValueError):
        _receipt.normalize_files_scope(["../escape"])

    repo = tmp_path / "repo"
    repo.mkdir()
    base_head = _init_git_repo(repo)
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "added.txt").write_text("hello", encoding="utf-8")
    changes = _receipt.collect_worktree_changes(str(repo), base_head=base_head)
    assert changes["changed_files"] == ["src/added.txt", "src/app.py"]

    patch_path = tmp_path / "out.patch"
    meta = _receipt.export_worktree_patch(str(repo), str(patch_path), base_head=base_head)
    assert meta["path"] == str(patch_path.resolve())
    assert meta["bytes"] > 0
    assert meta["untracked_files_included"] == ["src/added.txt"]
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "diff --git a/src/app.py b/src/app.py" in patch_text
    assert "diff --git a/src/added.txt b/src/added.txt" in patch_text

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(repo), str(clone)], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "checkout", base_head], cwd=str(clone), check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=str(clone), check=True, capture_output=True, **run_flags())

    (repo / "src" / "bin.dat").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError, match="binary"):
        _receipt.export_worktree_patch(str(repo), str(tmp_path / "bad.patch"), base_head=base_head)
    (repo / "src" / "bin.dat").unlink()

    fake_raw = subprocess.CompletedProcess(
        args=["git"], returncode=0,
        stdout=b":000000 120000 0000000 1111111 A\x00link\x00", stderr=b"",
    )
    with mock.patch.object(_receipt.subprocess, "run", return_value=fake_raw):
        with pytest.raises(ValueError, match="120000"):
            _receipt.export_worktree_patch(str(repo), str(tmp_path / "sym.patch"), base_head=base_head)


def test_single_dispatch_files_scope_and_patch_out_preflight_and_violation(tmp_path, monkeypatch):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_subagent.py"), "--cli", "claude", "--prompt", "hi", "--files-scope", "src/"],
        capture_output=True, text=True, timeout=20, **run_flags(),
    )
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["error_kind"] == "invalid_files_scope"

    repo = tmp_path / "repo2"
    repo.mkdir()
    base_head = _init_git_repo(repo)
    wt = tmp_path / "wt2"
    subprocess.run(["git", "worktree", "add", str(wt), base_head], cwd=str(repo), check=True, capture_output=True, **run_flags())

    def fake_setup(inv, cwd, wt_arg, name, **kw):
        return {"cwd": str(wt), "path": str(wt), "branch": "wt-branch", "base_head": base_head}, None

    def fake_exec_violation(inv, **kw):
        (wt / "outside.txt").write_text("oops\n", encoding="utf-8")
        return {
            "status": "success", "execution_status": "success", "dispatcher_status": "success",
            "exit_code": 0, "raw_backend_exit_code": 0, "normalized_exit_code": 0,
            "cli": "claude", "result": "done", "output_contract": "freeform",
            "attempts": 1, "attempt_id": inv.attempt_id, "resume": {"cli": "claude", "session_id": None},
        }

    emitted = []
    monkeypatch.setattr(rs, "_preflight_then_setup_worktree", fake_setup)
    monkeypatch.setattr(rs, "execute_agent", fake_exec_violation)
    monkeypatch.setattr(rs, "_emit", lambda obj, **kw: emitted.append(obj))
    patch_file = tmp_path / "should_not_exist.patch"
    monkeypatch.setattr(
        sys, "argv",
        [
            "run_subagent.py", "--cli", "claude", "--prompt", "edit src", "--cwd", str(repo),
            "--worktree", "wt2", "--files-scope", "src/", "--patch-out", str(patch_file),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        rs.main()
    assert exc.value.code == 1
    out = emitted[0]
    assert out["status"] == "error"
    assert out["error_kind"] == "files_scope_violation"
    assert out["files_scope"]["out_of_scope"] == ["outside.txt"]
    assert not patch_file.exists()


# --- Pillar 5: --text / -t, --manifest -, jobs wait --any --------------------


def test_text_mode_streaming_redaction_and_codex_profile_footer(tmp_path, capsys, monkeypatch):
    parser = _cli.build_parser("3.9.0", 1)
    parsed_fanout = parser.parse_args(["--manifest", "m.json", "-t"])
    err = _cli.unsupported_mode_flags(["--manifest", "m.json", "-t"], parsed_fanout)
    assert err is not None and "--text" in err

    p_bg = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_subagent.py"), "--cli", "claude", "--prompt", "hi", "-t", "--background"],
        capture_output=True, text=True, timeout=20, **run_flags(),
    )
    assert p_bg.returncode != 0
    p_sch = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_subagent.py"), "--cli", "claude", "--prompt", "hi", "--text", "--json-schema", "s.json"],
        capture_output=True, text=True, timeout=20, **run_flags(),
    )
    assert p_sch.returncode != 0

    def fake_exec_stream(inv, **kw):
        cb = kw.get("on_text_chunk")
        if cb:
            cb("hello api_key=super-secret-value-12345\n")
        return {
            "status": "success", "execution_status": "success", "dispatcher_status": "success",
            "exit_code": 0, "raw_backend_exit_code": 0, "normalized_exit_code": 0,
            "cli": "claude", "model": "claude-sonnet-5-5",
            "result": "hello api_key=super-secret-value-12345",
            "output_contract": "text", "attempts": 1, "attempt_id": inv.attempt_id,
            "resume": {"cli": "claude", "session_id": "55555555-5555-5555-5555-555555555555"},
        }

    monkeypatch.setattr(rs, "execute_agent", fake_exec_stream)
    monkeypatch.setattr(
        sys, "argv",
        ["run_subagent.py", "--cli", "claude", "--prompt", "hi", "--cwd", str(tmp_path), "-t"],
    )
    with pytest.raises(SystemExit) as exc:
        rs.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "super-secret-value-12345" not in captured.out
    assert "<redacted>" in captured.out
    assert "hello" in captured.out
    assert '"envelope"' not in captured.out
    assert "[summon] status=success" in captured.err
    assert "resume: summon dispatch" in captured.err

    codex_inv = _builder.AgentInvocation(
        cli="codex", prompt="hi", cwd=str(tmp_path), profile="work-profile",
    )
    footer = rs._format_text_footer(
        {"status": "success", "cli": "codex", "resume": {"cli": "codex", "session_id": "66666666-6666-6666-6666-666666666666"}},
        codex_inv,
        SimpleNamespace(agent=None, profile="work-profile"),
    )
    assert "unavailable" in footer
    assert "--resume" not in footer


def test_manifest_stdin_loading_and_invalid_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jobs": []}'))
    rc = _manifest.run_manifest(SimpleNamespace(manifest="-", cwd=str(tmp_path), results_dir=None, concurrency="default=1"))
    assert rc == 1
    out = capsys.readouterr()
    assert "no jobs" in out.out

    monkeypatch.setattr(sys, "stdin", io.StringIO("{bad json"))
    rc2 = _manifest.run_manifest(SimpleNamespace(manifest="-", cwd=str(tmp_path), results_dir=None, concurrency="default=1"))
    assert rc2 == 1
    out2 = capsys.readouterr()
    assert "stdin (-)" in out2.out


def test_jobs_wait_any_cli_and_polling_semantics(tmp_path, capsys):
    argv, mode_err = _cli.rewrite_subcommand(["wait", "--any", "j1", "j2", "--timeout", "5s"])
    assert mode_err is None
    assert argv == ["--jobs-wait-any", "--jobs-wait-ids", "j1", "--jobs-wait-ids", "j2", "--timeout", "5s"]

    _bad_argv, conflict_err = _cli.rewrite_subcommand(["jobs", "wait", "--all", "--any", "--active-only"])
    assert conflict_err and "cannot combine --all and --any" in conflict_err

    root = str(tmp_path / "jobs")
    os.makedirs(root, exist_ok=True)
    jid1 = "1" * 32
    jid2 = "2" * 32
    _jobs.write_prepared(
        root, jid1, nonce="nonce1", agent="a", prompt_sha256="b" * 64,
        cwd=root, flags={}, summon={"scripts_sha256": "c" * 64},
    )
    _jobs.write_prepared(
        root, jid2, nonce="nonce2", agent="b", prompt_sha256="b" * 64,
        cwd=root, flags={}, summon={"scripts_sha256": "c" * 64},
    )
    _jobs._atomic_write_json(_jobs.result_path(root, jid2), {
        "status": "success",
        "execution_status": "success",
        "job_nonce": "nonce2",
        "summon": {"version": "3.9.0", "scripts_sha256": "c" * 64},
        "result": "done j2",
    })

    errs = []
    rc_bad = _background.run_jobs_query(
        SimpleNamespace(
            job_dir=root, jobs_list=False, jobs_watch=False, jobs_status=None,
            jobs_wait=None, jobs_wait_all=False, jobs_wait_any=True, jobs_wait_ids=[],
            jobs_active_only=False, timeout=200, jobs_extend=None, jobs_cancel=None,
            jobs_steer=None, jobs_resume=None, jobs_revalidate=None, json=True,
        ),
        errs.append,
    )
    assert rc_bad == 1 and errs and "--active-only" in errs[0]

    empty_root = str(tmp_path / "empty_jobs")
    os.makedirs(empty_root, exist_ok=True)
    rc_empty = _background.run_jobs_query(
        SimpleNamespace(
            job_dir=empty_root, jobs_list=False, jobs_watch=False, jobs_status=None,
            jobs_wait=None, jobs_wait_all=False, jobs_wait_any=True, jobs_wait_ids=[],
            jobs_active_only=True, timeout=200, jobs_extend=None, jobs_cancel=None,
            jobs_steer=None, jobs_resume=None, jobs_revalidate=None, json=True,
        ),
        lambda m: None,
    )
    assert rc_empty == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "no_active_jobs"

    rc_hit = _background.run_jobs_query(
        SimpleNamespace(
            job_dir=root, jobs_list=False, jobs_watch=False, jobs_status=None,
            jobs_wait=None, jobs_wait_all=False, jobs_wait_any=True,
            jobs_wait_ids=[jid1, jid2], jobs_active_only=False, timeout=500,
            jobs_extend=None, jobs_cancel=None, jobs_steer=None, jobs_resume=None,
            jobs_revalidate=None, json=True,
        ),
        lambda m: None,
    )
    assert rc_hit == 0
    hit_payload = json.loads(capsys.readouterr().out)
    assert hit_payload["completed_job_id"] == jid2
    assert hit_payload["remaining_ids"] == [jid1]
    assert "schema" not in hit_payload

    rc_to = _background.run_jobs_query(
        SimpleNamespace(
            job_dir=root, jobs_list=False, jobs_watch=False, jobs_status=None,
            jobs_wait=None, jobs_wait_all=False, jobs_wait_any=True,
            jobs_wait_ids=[jid1], jobs_active_only=False, timeout=50,
            jobs_extend=None, jobs_cancel=None, jobs_steer=None, jobs_resume=None,
            jobs_revalidate=None, json=True,
        ),
        lambda m: None,
    )
    assert rc_to == 124
    to_payload = json.loads(capsys.readouterr().out)
    assert to_payload["wait_outcome"] == "timeout"
    assert to_payload["exit_code"] == 124
