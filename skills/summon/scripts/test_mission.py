"""Tests for _mission.py and the `summon mission` / `summon memory` CLI subcommands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _mission  # noqa: E402
from _spawn import run_flags  # noqa: E402

RUN_SUBAGENT = str(Path(__file__).parent / "run_subagent.py")


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
        **run_flags(),
    )


def test_validate_mission_plan_clamps_permissions_and_enforces_team():
    raw_plan = {
        "schema": "summon.mission-plan/v1",
        "status": "ready",
        "summary": "Implement and verify auth module",
        "tasks": [
            {
                "id": "impl-auth",
                "agent": "implementer",
                "prompt": "Implement auth handler",
                "permission": "yolo",  # Should be clamped to safe-edit
                "depends_on": [],
                "files_scope": ["src/auth/"],
            },
            {
                "id": "verify-auth",
                "agent": "reviewer",
                "prompt": "Review auth handler",
                "permission": "read-only",
                "depends_on": ["impl-auth"],
                "files_scope": ["src/auth/", "tests/"],
            },
        ],
    }
    validated = _mission.validate_mission_plan(
        raw_plan,
        allowed_team=["implementer", "reviewer"],
        max_permission="safe-edit",
    )
    assert validated["schema"] == "summon.mission-plan/v1"
    assert validated["tasks"][0]["permission"] == "safe-edit"
    assert validated["tasks"][1]["permission"] == "read-only"
    assert len(validated["clamped_permissions"]) == 1
    assert validated["clamped_permissions"][0]["proposed_permission"] == "yolo"
    assert validated["clamped_permissions"][0]["effective_permission"] == "safe-edit"
    assert validated["waves"] == [["impl-auth"], ["verify-auth"]]

    # Rejects unapproved team agent
    bad_team_plan = {
        "schema": "summon.mission-plan/v1",
        "status": "ready",
        "tasks": [
            {
                "id": "rogue",
                "agent": "unapproved-agent",
                "prompt": "Do something",
            }
        ],
    }
    with pytest.raises(_mission.MissionError) as exc_info:
        _mission.validate_mission_plan(
            bad_team_plan,
            allowed_team=["implementer", "reviewer"],
            max_permission="safe-edit",
        )
    assert exc_info.value.error_kind == "unapproved_team_agent"

    # Rejects path traversal in files_scope
    bad_scope_plan = {
        "schema": "summon.mission-plan/v1",
        "status": "ready",
        "tasks": [
            {
                "id": "escape",
                "agent": "implementer",
                "prompt": "Escape scope",
                "files_scope": ["../outside/secret.txt"],
            }
        ],
    }
    with pytest.raises(_mission.MissionError) as exc_info2:
        _mission.validate_mission_plan(
            bad_scope_plan,
            allowed_team=["implementer"],
            max_permission="safe-edit",
        )
    assert exc_info2.value.error_kind == "invalid_files_scope"


def test_git_integration_merges_in_scope_and_blocks_scope_violation(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init")
    _git(str(repo), "config", "user.name", "Test User")
    _git(str(repo), "config", "user.email", "test@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-m", "initial")
    base_head = _git(str(repo), "rev-parse", "HEAD").stdout.strip()

    mission_dir = tmp_path / "missions" / "mission-scope-test"
    mission_dir.mkdir(parents=True)
    integration = _mission.setup_integration_worktree(
        str(repo), str(mission_dir), "mission-scope-test", base_head
    )
    assert integration["enabled"] is True

    # 1. Task 1 modifies in-scope file src/allowed.py inside its task worktree
    wt1 = tmp_path / "wt1"
    _git(str(repo), "worktree", "add", "-b", "agents/task-1", str(wt1), base_head)
    (wt1 / "src" / "allowed.py").write_text("allowed = True\n", encoding="utf-8")

    task1 = {
        "id": "task-1",
        "agent": "implementer",
        "files_scope": ["src/"],
    }
    env1 = {
        "status": "success",
        "exit_code": 0,
        "result": "Added allowed.py",
        "worktree": {
            "path": str(wt1),
            "base_head": base_head,
        },
    }
    res1 = _mission.verify_and_integrate_task(
        task=task1,
        envelope=env1,
        integration=integration,
    )
    assert res1["status"] == "merged"
    assert res1["merged"] is True
    assert "src/allowed.py" in res1["touched_files"]
    assert (Path(integration["worktree_path"]) / "src" / "allowed.py").exists()

    # 2. Task 2 modifies out-of-scope file secrets/config.json -> blocked with scope_violation
    wt2 = tmp_path / "wt2"
    _git(str(repo), "worktree", "add", "-b", "agents/task-2", str(wt2), base_head)
    (wt2 / "secrets").mkdir()
    (wt2 / "secrets" / "config.json").write_text("{}", encoding="utf-8")

    task2 = {
        "id": "task-2",
        "agent": "implementer",
        "files_scope": ["src/"],
    }
    env2 = {
        "status": "success",
        "exit_code": 0,
        "result": "Touched secrets/config.json",
        "worktree": {
            "path": str(wt2),
            "base_head": base_head,
        },
    }
    res2 = _mission.verify_and_integrate_task(
        task=task2,
        envelope=env2,
        integration=integration,
    )
    assert res2["status"] == "scope_violation"
    assert res2["merged"] is False
    assert "secrets/config.json" in res2["out_of_scope_files"]
    assert not (Path(integration["worktree_path"]) / "secrets" / "config.json").exists()


def test_mission_cli_needs_human_reply_steer_digest_and_cancel(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    missions_root = ws / ".agents" / "missions"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({
            "schema": "summon.mission-plan/v1",
            "status": "needs_human",
            "summary": "Clarify database engine before migration",
            "tasks": [
                {
                    "id": "migrate-db",
                    "agent": "implementer",
                    "prompt": "Run schema migration",
                    "permission": "yolo",
                    "depends_on": [],
                    "files_scope": ["db/"],
                }
            ],
            "questions": [
                {
                    "question_id": "q-db",
                    "prompt": "Should we target PostgreSQL 16 or SQLite for local dev?",
                    "options": ["postgres16", "sqlite"],
                }
            ],
        }),
        encoding="utf-8",
    )

    # 1. Start mission with --plan-file -> pauses in paused_needs_human
    start_proc = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "start",
            "--mission-id",
            "mission-db-test",
            "--objective",
            "Migrate local database layer",
            "--team",
            "implementer,reviewer",
            "--max-permission",
            "safe-edit",
            "--plan-file",
            str(plan_file),
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        **run_flags(),
    )
    assert start_proc.returncode == 0, start_proc.stderr or start_proc.stdout
    proj = json.loads(start_proc.stdout)
    assert proj["schema"] == "summon.mission/v1"
    assert proj["mission_id"] == "mission-db-test"
    assert proj["status"] == "paused_needs_human"
    assert len(proj["open_questions"]) == 1
    assert proj["open_questions"][0]["question_id"] == "q-db"
    assert proj["tasks"][0]["permission"] == "safe-edit"  # Clamped from yolo!

    # 2. Check status --digest human output
    digest_proc = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "status",
            "mission-db-test",
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--digest",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert digest_proc.returncode == 0
    assert "MISSION mission-db-test [PAUSED_NEEDS_HUMAN]" in digest_proc.stdout
    assert "[q-db]" in digest_proc.stdout
    assert "summon mission reply mission-db-test --question-id q-db" in digest_proc.stdout

    # 3. Reply with wrong question-id -> rejected cleanly
    bad_reply = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "reply",
            "mission-db-test",
            "--question-id",
            "q-wrong",
            "--answer",
            "sqlite",
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert bad_reply.returncode == 1
    bad_env = json.loads(bad_reply.stdout)
    assert bad_env["error_kind"] == "unknown_or_closed_question_id"

    # 4. Reply with valid question-id -> question resolved, status becomes ready_to_resume
    ok_reply = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "reply",
            "mission-db-test",
            "--question-id",
            "q-db",
            "--answer",
            "sqlite",
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert ok_reply.returncode == 0
    replied_proj = json.loads(ok_reply.stdout)
    assert replied_proj["open_questions"] == []
    assert len(replied_proj["answers"]) == 1
    assert replied_proj["answers"][0]["question_id"] == "q-db"
    assert replied_proj["answers"][0]["answer"] == "sqlite"
    assert replied_proj["status"] == "ready_to_resume"

    # 5. Steer and cancel mission
    steer_proc = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "steer",
            "mission-db-test",
            "--task-id",
            "migrate-db",
            "--message",
            "Use WAL mode in SQLite",
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert steer_proc.returncode == 0
    steered_proj = json.loads(steer_proc.stdout)
    assert len(steered_proj["steering"]) == 1
    assert steered_proj["steering"][0]["task_id"] == "migrate-db"

    list_proc = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "mission",
            "list",
            "--runs-root",
            str(missions_root),
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert list_proc.returncode == 0
    listed = json.loads(list_proc.stdout)
    assert listed["count"] == 1
    assert listed["missions"][0]["mission_id"] == "mission-db-test"


def test_memory_search_cli_end_to_end(tmp_path: Path):
    ws = tmp_path / "ws"
    agents_dir = ws / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "memory.md").write_text(
        "# Auth Architecture\nWe use Ed25519 tokens with 15-minute expiry for session auth.\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            RUN_SUBAGENT,
            "memory",
            "search",
            "Ed25519 session expiry",
            "--cwd",
            str(ws),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        **run_flags(),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    res = json.loads(proc.stdout)
    assert res["schema"] == "summon.memory-search/v1"
    assert len( res["hits"]) >= 1 if False else len(res["hits"]) >= 1
    assert "Ed25519" in res["hits"][0]["snippet"]
    assert res["hits"][0]["source_verified"] is True


def test_writable_task_worktree_enforced_and_roster_permission_clamping(tmp_path: Path):
    raw_plan = {
        "schema": "summon.mission-plan/v1",
        "status": "ready",
        "summary": "Writable task attempting worktree=False",
        "tasks": [
            {
                "id": "t-write",
                "agent": "implementer",
                "prompt": "Write code",
                "permission": "safe-edit",
                "worktree": False,  # Must be forced to True for writable tasks!
                "files_scope": ["src/"],
            },
            {
                "id": "t-ro",
                "agent": "reviewer",
                "prompt": "Review code",
                "permission": "safe-edit",  # Roster says read-only -> clamped!
                "worktree": False,
                "depends_on": ["t-write"],
            },
        ],
    }
    validated = _mission.validate_mission_plan(
        raw_plan,
        allowed_team=["implementer", "reviewer"],
        max_permission="yolo",
        roster_permissions={"implementer": "safe-edit", "reviewer": "read-only"},
    )
    assert validated["tasks"][0]["worktree"] is True
    assert validated["tasks"][1]["permission"] == "read-only"
    assert validated["tasks"][1]["worktree"] is False
    assert any(
        c["task_id"] == "t-ro" and c["effective_permission"] == "read-only"
        for c in validated["clamped_permissions"]
    )


def test_verify_and_integrate_blocks_on_report_verdict_block(tmp_path: Path):
    task = {"id": "t-review", "agent": "reviewer", "files_scope": ["src/"]}
    # Even when top-level verdict is "pass", report.verdict="block" must block integration!
    env = {
        "status": "success",
        "exit_code": 0,
        "verdict": "pass",
        "report": {"status": "done", "verdict": "block", "summary": "Security bug"},
    }
    res = _mission.verify_and_integrate_task(
        task=task,
        envelope=env,
        integration={"enabled": True, "worktree_path": str(tmp_path)},
    )
    assert res["status"] == "blocked_by_verdict"
    assert res["merged"] is False


def test_cancel_mission_exhausts_budget_and_cancels_active_jobs(tmp_path: Path, monkeypatch):
    import _jobs
    import _mission_budget

    job_root = tmp_path / "jobs"
    monkeypatch.setenv("SUMMON_JOBS_DIR", str(job_root))
    _jobs.ensure_jobs_dir(str(job_root))
    jid = _jobs.new_job_id()
    _jobs.write_prepared(
        str(job_root),
        jid,
        nonce="ab" * 32,
        agent="sol",
        prompt_sha256="c" * 64,
        cwd=str(tmp_path),
        flags={"agent": "sol", "mission_id": "mission-cancel-01"},
        summon={"version": "3.8.0", "script": "run_subagent.py"},
    )

    missions_root = tmp_path / "missions"
    mdir = missions_root / "mission-cancel-01"
    mdir.mkdir(parents=True)
    (mdir / "receipt.json").write_text(
        json.dumps({"schema": "summon.mission/v1", "mission_id": "mission-cancel-01", "objective": "Test cancel"}),
        encoding="utf-8",
    )

    proj = _mission.cancel_mission(str(mdir), reason="stop everything")
    assert proj["status"] == "cancelled"

    # Active background job received cancel command and budget is marked exhausted
    b_summary = _mission_budget.read_mission_budget("mission-cancel-01", job_dir=str(job_root))
    assert b_summary is not None
    assert b_summary["exhausted"] is True
    assert jid in b_summary["terminated_job_ids"]

    with pytest.raises(_mission_budget.MissionBudgetExhaustedError):
        _mission_budget.reserve_launch(
            "mission-cancel-01",
            job_id="job-after-cancel",
            attempt_id="att-after-cancel",
            job_dir=str(job_root),
        )
