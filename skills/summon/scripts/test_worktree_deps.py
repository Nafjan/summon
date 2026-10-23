"""Tests for Phase 4 opt-in worktree dependency bridging (_worktree_deps.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _background
import _cli
import _executor
import _progress_judge
import _receipt
import _worktree_deps
import _worktree_gc
import run_subagent


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"],
                   check=True, capture_output=True, text=True)
    (path / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
    (path / "package-lock.json").write_text('{"name":"demo","lockfileVersion":3}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "package.json", "package-lock.json"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"],
                   check=True, capture_output=True, text=True)
    return path


def _populate_deps(repo: Path) -> None:
    nm_pkg = repo / "node_modules" / "left-pad"
    nm_pkg.mkdir(parents=True, exist_ok=True)
    (nm_pkg / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
    (repo / "node_modules" / ".package-lock.json").write_text('{"packages":{}}\n', encoding="utf-8")

    venv_sp = repo / ".venv" / "Lib" / "site-packages" / "demo_pkg.dist-info"
    venv_sp.mkdir(parents=True, exist_ok=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv_sp / "METADATA").write_text("Name: demo-pkg\nVersion: 0.1.0\n", encoding="utf-8")


def test_parse_link_deps_validates_and_normalizes():
    assert _worktree_deps.parse_link_deps(None) == ()
    assert _worktree_deps.parse_link_deps("") == ()
    assert _worktree_deps.parse_link_deps("node_modules, .venv , packages\\web\\node_modules/") == (
        ".venv",
        "node_modules",
        "packages/web/node_modules",
    )

    for bad in (
        ",",
        "node_modules,",
        "/abs/path",
        "\\abs\\path",
        "C:\\node_modules",
        "../node_modules",
        "packages/../../node_modules",
        ".git",
        ".git/hooks",
        ".claude/worktrees",
        "packages/.git/deps",
        "node_modules,node_modules/sub",
    ):
        with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_info:
            _worktree_deps.parse_link_deps(bad)
        assert exc_info.value.kind == "link_deps_invalid_path"


def test_preflight_requires_worktree_and_refuses_yolo_without_allow_mutation(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)

    with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_wt:
        _worktree_deps.preflight_link_deps(
            str(repo), "node_modules", worktree_arg=None, permission="safe-edit"
        )
    assert exc_wt.value.kind == "link_deps_requires_worktree"

    with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_yolo:
        _worktree_deps.preflight_link_deps(
            str(repo), "node_modules", worktree_arg="", permission="yolo", allow_mutation=False
        )
    assert exc_yolo.value.kind == "link_deps_yolo_refused"

    ok = _worktree_deps.preflight_link_deps(
        str(repo), "node_modules", worktree_arg="", permission="yolo", allow_mutation=True
    )
    assert ok["enabled"] is True
    assert ok["allow_mutation"] is True
    assert ok["paths"] == ["node_modules"]

    with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_mut_only:
        _worktree_deps.preflight_link_deps(
            str(repo), None, worktree_arg="", permission="safe-edit", allow_mutation=True
        )
    assert exc_mut_only.value.kind == "link_deps_invalid_path"


def test_preflight_rejects_missing_or_symlink_targets(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)

    with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_missing:
        _worktree_deps.preflight_link_deps(
            str(repo), "nonexistent_deps", worktree_arg="", permission="read-only"
        )
    assert exc_missing.value.kind == "link_deps_invalid_target"

    with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_file:
        _worktree_deps.preflight_link_deps(
            str(repo), "package.json", worktree_arg="", permission="read-only"
        )
    assert exc_file.value.kind == "link_deps_invalid_target"

    outside = tmp_path / "outside_deps"
    outside.mkdir()
    link_in_repo = repo / "linked_outside"
    _worktree_deps._create_dir_link(str(outside), str(link_in_repo))
    try:
        with pytest.raises(_worktree_deps.WorktreeDepsError) as exc_link:
            _worktree_deps.preflight_link_deps(
                str(repo), "linked_outside", worktree_arg="", permission="read-only"
            )
        assert exc_link.value.kind == "link_deps_invalid_target"
    finally:
        if _worktree_deps._is_link_or_junction(str(link_in_repo)):
            _worktree_deps._remove_dir_link(str(link_in_repo))


def test_bridge_and_verify_linked_deps_round_trip(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)

    wt_info = run_subagent._setup_worktree(
        str(repo), "wt-roundtrip", "reviewer",
        link_deps=("node_modules", ".venv"),
        link_deps_allow_mutation=False,
    )
    wt_path = Path(wt_info["path"])
    try:
        assert "linked_deps" in wt_info
        ld_init = _worktree_deps.validate_projection(wt_info["linked_deps"])
        assert ld_init["count"] == 2
        assert ld_init["paths"] == [".venv", "node_modules"]
        assert ld_init["links_intact"] is True
        assert ld_init["deps_mutated"] is False

        assert _worktree_deps._is_link_or_junction(str(wt_path / "node_modules"))
        assert _worktree_deps._is_link_or_junction(str(wt_path / ".venv"))
        assert (wt_path / "node_modules" / "left-pad" / "index.js").read_text(encoding="utf-8") == (
            "module.exports = 42;\n"
        )

        # Workspace snapshot and ProgressJudge fingerprint must ignore intact bridged links.
        snap = _receipt.workspace_snapshot(str(wt_path))
        assert snap["coverage"] == "complete"
        assert snap["untracked"] == []

        fp_wt = _progress_judge.workspace_fingerprint(str(wt_path))
        assert fp_wt is not None
        (wt_path / "new_work.txt").write_text("agent progress\n", encoding="utf-8")
        fp_wt_after = _progress_judge.workspace_fingerprint(str(wt_path))
        assert fp_wt_after is not None and fp_wt_after != fp_wt
        (wt_path / "new_work.txt").unlink()

        verified = _worktree_deps.verify_linked_deps(wt_info)
        assert verified is not None
        assert verified["schema"] == _worktree_deps.SCHEMA
        assert verified["before_sha256"] == verified["after_sha256"]
        assert verified["deps_mutated"] is False
        assert verified["links_intact"] is True
        assert verified["mutated_paths"] == []
    finally:
        run_subagent._remove_worktree(wt_info)


def test_post_dispatch_mutation_detected_and_fails_closed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)

    wt_info = run_subagent._setup_worktree(
        str(repo), "wt-mutated", "reviewer",
        link_deps=("node_modules", ".venv"),
        link_deps_allow_mutation=False,
    )
    wt_path = Path(wt_info["path"])
    try:
        # Simulate an agent installing a new package inside bridged node_modules.
        new_pkg = wt_path / "node_modules" / "injected-pkg"
        new_pkg.mkdir(parents=True, exist_ok=True)
        (new_pkg / "index.js").write_text("module.exports = 'mutated';\n", encoding="utf-8")

        verified = _worktree_deps.verify_linked_deps(wt_info)
        assert verified is not None
        assert verified["deps_mutated"] is True
        assert verified["links_intact"] is True
        assert verified["mutated_paths"] == ["node_modules"]
        assert verified["before_sha256"] != verified["after_sha256"]

        # Simulate link replacement tampering: unlink .venv junction and replace with a real dir.
        _worktree_deps._remove_dir_link(str(wt_path / ".venv"))
        (wt_path / ".venv").mkdir()
        (wt_path / ".venv" / "rogue.txt").write_text("tampered\n", encoding="utf-8")

        verified_tampered = _worktree_deps.verify_linked_deps(wt_info)
        assert verified_tampered is not None
        assert verified_tampered["links_intact"] is False
        assert verified_tampered["deps_mutated"] is True
        assert ".venv" in verified_tampered["mutated_paths"]

        # Tampered non-link directory is no longer excluded from workspace_snapshot!
        snap = _receipt.workspace_snapshot(str(wt_path))
        assert any(p.startswith(".venv") for p in snap["untracked"])

        # _remove_worktree must preserve a worktree with a tampered non-link directory.
        cleanup = run_subagent._remove_worktree(wt_info)
        assert cleanup["preserved"] is True
        assert cleanup["worktree_removed"] is False
    finally:
        _worktree_deps.teardown_linked_deps(str(wt_path))
        shutil.rmtree(str(wt_path), ignore_errors=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                       capture_output=True, text=True, check=False)


def test_remove_worktree_and_worktree_gc_cleanly_unlink_bridged_deps(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)

    # 1. Gate-denial / _remove_worktree path on an untouched linked-deps worktree.
    wt_clean = run_subagent._setup_worktree(
        str(repo), "wt-clean-remove", "reviewer",
        link_deps=("node_modules", ".venv"),
    )
    cleanup = run_subagent._remove_worktree(wt_clean)
    assert cleanup["worktree_removed"] is True
    assert cleanup["branch_removed"] is True
    assert cleanup["preserved"] is False
    assert (repo / "node_modules" / "left-pad" / "index.js").is_file()
    assert (repo / ".venv" / "pyvenv.cfg").is_file()

    # 2. _worktree_gc plan() and apply() on an aged linked-deps worktree.
    wt_gc = run_subagent._setup_worktree(
        str(repo), "wt-gc-deps", "reviewer",
        link_deps=("node_modules", ".venv"),
    )
    wt_gc_path = wt_gc["path"]
    future_now = lambda: 10_000_000_000.0  # noqa: E731
    gc_plan = _worktree_gc.plan(
        str(repo), min_age_hours=1.0, now=future_now, caller_cwd=str(repo)
    )
    entries = [e for e in gc_plan["entries"] if e["branch"] == "agents/wt-gc-deps"]
    assert len(entries) == 1
    assert entries[0]["action"] == "remove"
    assert entries[0]["reason"] == "merged_into_base"

    applied = _worktree_gc.apply(
        gc_plan, running_jobs=0, now=future_now, caller_cwd=str(repo)
    )
    assert len(applied["results"]) == 1
    assert applied["results"][0]["ok"] is True
    assert applied["results"][0].get("skipped") is None
    assert not os.path.exists(wt_gc_path)
    # Base dependency contents are completely untouched.
    assert (repo / "node_modules" / "left-pad" / "index.js").is_file()
    assert (repo / ".venv" / "pyvenv.cfg").is_file()


def test_background_child_argv_and_request_identity_include_link_deps(tmp_path):
    parser = _cli.build_parser("3.6.2", 1)
    args = parser.parse_args([
        "--agent", "reviewer",
        "--prompt", "check deps",
        "--cwd", str(tmp_path),
        "--worktree", "wt1",
        "--link-deps", "node_modules,.venv",
        "--link-deps-allow-mutation",
    ])
    argv = _background.child_argv(args, str(tmp_path / "job.json"))
    assert "--link-deps" in argv
    assert "node_modules,.venv" in argv
    assert "--link-deps-allow-mutation" in argv

    id_plain = _executor.build_request_identity(
        agent="reviewer", prompt="p", cwd=str(tmp_path), agents_dir=None,
        cli="codex", model="gpt-5", effort="high", json_schema=None,
        resume=None, worktree="wt1",
    )
    id_linked = _executor.build_request_identity(
        agent="reviewer", prompt="p", cwd=str(tmp_path), agents_dir=None,
        cli="codex", model="gpt-5", effort="high", json_schema=None,
        resume=None, worktree="wt1", link_deps="node_modules,.venv",
    )
    id_linked_reordered = _executor.build_request_identity(
        agent="reviewer", prompt="p", cwd=str(tmp_path), agents_dir=None,
        cli="codex", model="gpt-5", effort="high", json_schema=None,
        resume=None, worktree="wt1", link_deps=".venv, node_modules/",
    )
    id_linked_mut = _executor.build_request_identity(
        agent="reviewer", prompt="p", cwd=str(tmp_path), agents_dir=None,
        cli="codex", model="gpt-5", effort="high", json_schema=None,
        resume=None, worktree="wt1", link_deps="node_modules,.venv",
        link_deps_allow_mutation=True,
    )
    fp_plain = _executor.request_fingerprint(**id_plain)
    fp_linked = _executor.request_fingerprint(**id_linked)
    fp_linked_reordered = _executor.request_fingerprint(**id_linked_reordered)
    fp_linked_mut = _executor.request_fingerprint(**id_linked_mut)

    assert fp_plain != fp_linked
    assert fp_linked == fp_linked_reordered
    assert fp_linked != fp_linked_mut


def test_run_subagent_dry_run_and_live_mutation_enforcement(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path / "repo")
    _populate_deps(repo)
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "ro-seat.md").write_text(
        "---\nname: ro-seat\ndescription: ro\nrun-agent: codex\nmodel: gpt-5\npermission: read-only\n---\nRO body\n",
        encoding="utf-8",
    )
    (agents_dir / "yolo-seat.md").write_text(
        "---\nname: yolo-seat\ndescription: yolo\nrun-agent: codex\nmodel: gpt-5\npermission: yolo\n---\nYOLO body\n",
        encoding="utf-8",
    )

    def _invoke(argv_list):
        monkeypatch.setattr(sys, "argv", ["run_subagent.py", *argv_list])
        with pytest.raises(SystemExit) as ex:
            run_subagent.main()
        return ex.value.code, json.loads(capsys.readouterr().out)

    import json

    # 1. --dry-run with --worktree and --link-deps previews worktree_deps
    code_ok, env_ok = _invoke([
        "--agent", "ro-seat",
        "--prompt", "inspect",
        "--cwd", str(repo),
        "--agents-dir", str(agents_dir),
        "--worktree", "wt-dry",
        "--link-deps", "node_modules,.venv",
        "--dry-run",
    ])
    assert code_ok == 0
    assert env_ok.get("would_refuse") is not True
    assert env_ok["worktree_deps"]["schema"] == _worktree_deps.SCHEMA
    assert env_ok["worktree_deps"]["paths"] == [".venv", "node_modules"]

    # 2. --dry-run with permission=yolo and no --link-deps-allow-mutation reports would_refuse
    code_yolo, env_yolo = _invoke([
        "--agent", "yolo-seat",
        "--prompt", "inspect",
        "--cwd", str(repo),
        "--agents-dir", str(agents_dir),
        "--worktree", "wt-yolo",
        "--link-deps", "node_modules",
        "--dry-run",
    ])
    assert code_yolo == 0
    assert env_yolo["would_refuse"] is True
    assert env_yolo["error_kind"] == "link_deps_yolo_refused"

    # 3. Live dispatch where child mutates bridged node_modules fails closed without allow_mutation
    def _mutating_execute(invocation, **_kwargs):
        nm_file = Path(invocation.cwd) / "node_modules" / "left-pad" / "index.js"
        nm_file.write_text("module.exports = 'tampered';\n", encoding="utf-8")
        return {
            "status": "success",
            "execution_status": "completed",
            "result": "Done.\n\n```json\n{\"status\":\"success\",\"summary\":\"ok\"}\n```",
            "cli": "codex",
            "model": "gpt-5",
            "served_model": "gpt-5",
            "served_model_evidence": "stream",
            "model_match": True,
            "named_model_verified": True,
            "provider_contacted": True,
            "attempts": 1,
            "attempt_status": "completed",
            "raw_backend_exit_code": 0,
            "normalized_exit_code": 0,
            "exit_code": 0,
            "retryable": False,
            "result_usable": True,
            "report_ok": True,
            "suspect": False,
        }

    monkeypatch.setattr(run_subagent, "_preflight_backend", lambda *_a, **_k: None)
    monkeypatch.setattr(run_subagent, "_dispatch_with_retries",
                        lambda inv, *_a, **_k: _mutating_execute(inv))

    code_live_fail, env_live_fail = _invoke([
        "--agent", "ro-seat",
        "--prompt", "inspect",
        "--cwd", str(repo),
        "--agents-dir", str(agents_dir),
        "--worktree", "wt-live-mut",
        "--link-deps", "node_modules",
        "--no-contract-repair",
    ])
    assert code_live_fail == 1
    assert env_live_fail["status"] == "error"
    assert env_live_fail["error_kind"] == "linked_deps_mutated"
    assert env_live_fail["result_usable"] is False
    assert env_live_fail["worktree"]["linked_deps"]["deps_mutated"] is True
    assert env_live_fail["worktree"]["linked_deps"]["mutated_paths"] == ["node_modules"]

    # 4. Same mutation with --link-deps-allow-mutation succeeds with a warning
    code_live_allow, env_live_allow = _invoke([
        "--agent", "ro-seat",
        "--prompt", "inspect",
        "--cwd", str(repo),
        "--agents-dir", str(agents_dir),
        "--worktree", "wt-live-allow",
        "--link-deps", "node_modules",
        "--link-deps-allow-mutation",
        "--no-contract-repair",
    ])
    assert code_live_allow == 0
    assert env_live_allow["status"] == "success"
    assert env_live_allow["worktree"]["linked_deps"]["deps_mutated"] is True
    assert env_live_allow["worktree"]["linked_deps"]["allow_mutation"] is True
    assert any("allowed by --link-deps-allow-mutation" in w for w in env_live_allow.get("warnings", []))

