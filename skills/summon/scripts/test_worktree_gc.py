"""Provider-free tests for conservative agent-worktree garbage collection."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

import _cli
import _worktree_gc as gc

FUTURE = lambda: time.time() + 72 * 3600   # noqa: E731 - everything reads as idle


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    try:
        git(root, "init", "-q", "-b", "main")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    (root / "a.txt").write_text("a", encoding="utf-8")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "init")
    return root


def add_wt(repo, name):
    path = repo.parent / f"wt-{name}"
    git(repo, "worktree", "add", "-q", "-b", f"agents/{name}", str(path), "HEAD")
    return path


def entry(report, name):
    return next(e for e in report["entries"] if e["branch"] == f"agents/{name}")


def test_plan_classifies_every_safety_condition(repo):
    merged = add_wt(repo, "merged")
    dirty = add_wt(repo, "dirty")
    (dirty / "a.txt").write_text("changed", encoding="utf-8")
    unmerged = add_wt(repo, "unmerged")
    (unmerged / "b.txt").write_text("b", encoding="utf-8")
    git(unmerged, "add", "b.txt")
    git(unmerged, "commit", "-q", "-m", "work")
    locked = add_wt(repo, "locked")
    git(repo, "worktree", "lock", str(locked))
    git(repo, "worktree", "add", "-q", "-b", "feature/human", str(repo.parent / "human"), "HEAD")

    report = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    assert report["schema"] == gc.SCHEMA and report["mode"] == "plan"
    assert entry(report, "merged")["action"] == "remove"
    assert entry(report, "merged")["reason"] == "merged_into_base"
    assert entry(report, "dirty")["reason"] == "dirty"
    assert entry(report, "unmerged")["reason"] == "unmerged_commits"
    assert entry(report, "locked")["reason"] == "locked"
    assert all(not e["branch"].startswith("feature/") for e in report["entries"])
    assert {e["action"] for e in report["entries"]} <= {"keep", "remove"}
    assert merged.exists()   # planning never mutates


def test_recent_activity_is_kept(repo):
    add_wt(repo, "fresh")
    report = gc.plan(str(repo), caller_cwd=str(repo))   # real clock: just created
    assert entry(report, "fresh")["reason"] == "too_recent"


def test_caller_inside_worktree_is_kept(repo):
    path = add_wt(repo, "mine")
    report = gc.plan(str(repo), now=FUTURE, caller_cwd=str(path))
    assert entry(report, "mine")["reason"] == "in_use_by_caller"


def test_tree_identical_branch_counts_as_integrated(repo):
    path = add_wt(repo, "squashed")
    (path / "c.txt").write_text("c", encoding="utf-8")
    git(path, "add", "c.txt")
    git(path, "commit", "-q", "-m", "c")
    # Land the same content on main via a separate commit (squash-merge shape).
    (repo / "c.txt").write_text("c", encoding="utf-8")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "squash")
    report = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    assert entry(report, "squashed")["reason"] == "tree_identical_to_base"


def test_missing_directory_is_reported_never_pruned(repo):
    path = add_wt(repo, "gone")
    import shutil
    shutil.rmtree(path)
    report = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    assert entry(report, "gone")["action"] == "keep"
    assert entry(report, "gone")["reason"] == "missing_directory"
    applied = gc.apply(report, running_jobs=0, now=FUTURE, caller_cwd=str(repo))
    assert applied["results"] == []
    assert "wt-gone" in git(repo, "worktree", "list", "--porcelain")   # metadata kept


def test_apply_skips_worktrees_whose_branch_changed_after_planning(repo):
    path = add_wt(repo, "switched")
    planned = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    git(path, "switch", "-q", "-c", "human/topic")
    report = gc.apply(planned, running_jobs=0, now=FUTURE, caller_cwd=str(repo))
    assert report["results"][0]["skipped"] in {"no_longer_listed", "changed_since_plan"}
    assert path.exists()


def test_apply_skips_worktrees_with_new_commits_after_planning(repo):
    path = add_wt(repo, "advanced")
    planned = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    (path / "n.txt").write_text("new", encoding="utf-8")
    git(path, "add", "n.txt")
    git(path, "commit", "-q", "-m", "new work")
    report = gc.apply(planned, running_jobs=0, now=FUTURE, caller_cwd=str(repo))
    assert report["results"][0]["skipped"] == "changed_since_plan"
    assert path.exists() and "agents/advanced" in git(repo, "branch", "--list", "agents/*")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_non_finite_or_invalid_age_is_rejected(repo, bad):
    with pytest.raises(ValueError):
        gc.plan(str(repo), min_age_hours=bad)


@pytest.mark.parametrize("rows, expected", [
    ([], 0),
    ([{"state": "success"}, {"state": "stale"}], 0),
    ([{"state": "running"}, {"state": "error"}], 1),
    ([{"state": "prepared"}], 1),
    ([{"state": "unverified"}], -1),
    ([{"state": "corrupt"}], -1),
    ([{"state": "identity_mismatch"}], -1),
])
def test_running_job_count_treats_uncertain_states_as_unknown(monkeypatch, tmp_path, rows, expected):
    import _jobs
    monkeypatch.setattr(_jobs, "list_jobs", lambda _root: rows)
    assert gc.running_job_count(str(tmp_path)) == expected


def test_running_job_count_unreadable_job_dir_is_unknown(monkeypatch, tmp_path):
    import _jobs
    real_listdir = os.listdir

    def denied(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(tmp_path)):
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(gc.os, "listdir", denied)
    assert gc.running_job_count(str(tmp_path)) == -1


def test_cli_rejects_apply_with_dry_run(repo):
    import sys
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    proc = subprocess.run([sys.executable, script, "worktree", "gc", "--apply", "--dry-run",
                           "--cwd", str(repo)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert "contradictory" in (proc.stdout + proc.stderr)


def test_apply_removes_only_planned_worktrees_and_branches(repo):
    merged = add_wt(repo, "merged")
    dirty = add_wt(repo, "dirty")
    (dirty / "a.txt").write_text("changed", encoding="utf-8")
    report = gc.apply(gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo)), running_jobs=0,
                      now=FUTURE, caller_cwd=str(repo))
    assert report["mode"] == "apply"
    assert [r["branch"] for r in report["results"]] == ["agents/merged"]
    assert report["results"][0]["ok"] is True and report["results"][0]["branch_deleted"] is True
    assert "skipped" not in report["results"][0]
    assert not merged.exists() and dirty.exists()
    branches = git(repo, "branch", "--list", "agents/*")
    assert "agents/merged" not in branches and "agents/dirty" in branches


def test_apply_refuses_while_jobs_run_or_are_uncountable(repo):
    merged = add_wt(repo, "merged")
    planned = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    for running in (1, -1):
        report = gc.apply(planned, running_jobs=running, now=FUTURE, caller_cwd=str(repo))
        assert "refused" in report and report["results"] == []
    assert merged.exists()
    report = gc.apply(planned, running_jobs=1, ignore_running_jobs=True,
                      now=FUTURE, caller_cwd=str(repo))
    assert report["results"][0]["ok"] is True and not merged.exists()


def test_apply_revalidates_when_state_changed_after_planning(repo):
    path = add_wt(repo, "raced")
    planned = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    (path / "a.txt").write_text("edited after the plan", encoding="utf-8")
    report = gc.apply(planned, running_jobs=0, now=FUTURE, caller_cwd=str(repo))
    assert report["results"][0]["skipped"] == "no_longer_eligible:dirty"
    assert path.exists()


def test_apply_respects_the_age_floor_at_apply_time(repo):
    path = add_wt(repo, "fresh")
    planned = gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo))
    report = gc.apply(planned, running_jobs=0, caller_cwd=str(repo))   # real clock
    assert report["results"][0]["skipped"] == "no_longer_eligible:too_recent"
    assert path.exists()


def test_plan_rejects_bad_inputs(tmp_path, repo):
    with pytest.raises(ValueError):
        gc.plan(str(tmp_path / "nope"))
    with pytest.raises(ValueError):
        gc.plan(str(repo), base="does-not-exist")
    with pytest.raises(ValueError):
        gc.plan(str(repo), min_age_hours=-1)
    with pytest.raises(ValueError):
        gc.apply({"schema": "other"})


def test_parse_worktree_list_handles_flags():
    text = ("worktree /r\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /w\nHEAD bbb\nbranch refs/heads/agents/x\nlocked busy\n\n"
            "worktree /d\nHEAD ccc\ndetached\nprunable gitdir file points to non-existent location\n")
    parsed = gc.parse_worktree_list(text)
    assert parsed[1]["locked"] == "busy"
    assert parsed[2]["detached"] is True and parsed[2]["prunable"]


def test_render_mentions_dry_run_and_reasons(repo):
    add_wt(repo, "merged")
    text = gc.render(gc.plan(str(repo), now=FUTURE, caller_cwd=str(repo)))
    assert "merged_into_base" in text and "--apply" in text


def test_cli_rewrites_worktree_gc_aliases():
    argv, mode = _cli.rewrite_subcommand(
        ["worktree", "gc", "--base", "main", "--min-age-hours=2", "--apply",
         "--ignore-running-jobs", "--json"])
    assert mode is None
    assert argv == ["--worktree-gc", "--gc-base", "main", "--gc-min-age-hours=2",
                    "--gc-apply", "--gc-ignore-running-jobs", "--json"]
    assert _cli.rewrite_subcommand(["worktree"])[1] == "help:worktree"
    assert _cli.rewrite_subcommand(["worktree", "nuke"])[1].startswith("error:")
    args = _cli.build_parser("0.0.0", 1).parse_args(argv)
    assert args.worktree_gc and args.gc_apply and args.gc_base == "main"
    assert args.gc_min_age_hours == 2.0
