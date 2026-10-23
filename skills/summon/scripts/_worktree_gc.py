"""Conservative garbage collection for Summon-created agent worktrees.

Summon's ``--worktree`` creates a branch ``agents/<name>`` and a linked worktree.
Long-horizon fan-out leaves many of them behind. This module plans (and, only on
explicit request, applies) reclamation of worktrees that are provably safe:

* the branch is a Summon agent branch (``refs/heads/agents/``),
* the worktree is not locked and is not the caller's current directory,
* nothing changed recently (commit time, directory and index mtimes are all
  older than ``min_age_hours``),
* the working tree is clean (``git status --porcelain`` empty), and
* its commits are integrated: ``base..HEAD`` is empty, or the trees are identical.

Removal uses ``git worktree remove`` without ``--force`` and ``git branch -d``
(never ``-D``), so git itself re-checks cleanliness and merge state. Applying is
refused while any Summon background job is running unless explicitly overridden.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable

SCHEMA = "summon.worktree-gc/v1"
AGENT_BRANCH_PREFIX = "refs/heads/agents/"
DEFAULT_MIN_AGE_HOURS = 24.0
GIT_TIMEOUT_S = 30.0

Runner = Callable[..., subprocess.CompletedProcess]


def _git(runner: Runner, *args: str) -> subprocess.CompletedProcess | None:
    cmd = ["git"]
    if os.name == "nt":
        cmd += ["-c", "core.longpaths=true"]
    try:
        return runner([*cmd, *args], capture_output=True, text=True,
                      stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ok(proc) -> bool:
    return proc is not None and getattr(proc, "returncode", 1) == 0


def parse_worktree_list(text: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` blocks."""
    entries: list[dict] = []
    current: dict = {}
    for line in (text or "").splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"path": value}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key in {"locked", "prunable"}:
            current[key] = value or True
        elif key in {"detached", "bare"}:
            current[key] = True
    if current:
        entries.append(current)
    return entries


def _newest_activity(runner: Runner, path: str) -> float | None:
    stamps: list[float] = []
    proc = _git(runner, "-C", path, "log", "-1", "--format=%ct", "HEAD")
    if _ok(proc) and proc.stdout.strip().isdigit():
        stamps.append(float(proc.stdout.strip()))
    for candidate in (path,):
        try:
            stamps.append(os.stat(candidate).st_mtime)
        except OSError:
            pass
    index = _git(runner, "-C", path, "rev-parse", "--path-format=absolute",
                 "--git-path", "index")
    if _ok(index):
        try:
            stamps.append(os.stat(index.stdout.strip()).st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else None


def _inside(child: str, parent: str) -> bool:
    try:
        child_r = os.path.normcase(os.path.realpath(child))
        parent_r = os.path.normcase(os.path.realpath(parent))
        return os.path.commonpath([child_r, parent_r]) == parent_r
    except (OSError, ValueError):
        return False


def plan(repo: str, *, base: str = "HEAD", min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
         runner: Runner = subprocess.run, now: Callable[[], float] = time.time,
         caller_cwd: str | None = None) -> dict:
    """Return a dry-run plan. Never mutates anything."""
    if not isinstance(min_age_hours, (int, float)) or isinstance(min_age_hours, bool) \
            or min_age_hours < 0:
        raise ValueError("min_age_hours must be a non-negative number")
    top = _git(runner, "-C", repo, "rev-parse", "--show-toplevel")
    if not _ok(top):
        raise ValueError(f"not inside a git repository: {repo}")
    repo_root = top.stdout.strip()
    base_proc = _git(runner, "-C", repo_root, "rev-parse", "--verify", "--quiet",
                     f"{base}^{{commit}}")
    if not _ok(base_proc):
        raise ValueError(f"base revision does not resolve to a commit: {base!r}")
    base_sha = base_proc.stdout.strip()
    listing = _git(runner, "-C", repo_root, "worktree", "list", "--porcelain")
    if not _ok(listing):
        raise ValueError("git worktree list failed")
    caller = caller_cwd if caller_cwd is not None else os.getcwd()
    cutoff = now() - float(min_age_hours) * 3600.0
    entries = []
    for wt in parse_worktree_list(listing.stdout):
        branch = wt.get("branch") or ""
        if not branch.startswith(AGENT_BRANCH_PREFIX) or wt.get("bare"):
            continue
        path = wt.get("path", "")
        item = {"path": path, "branch": branch[len("refs/heads/"):],
                "head": wt.get("head"), "action": "keep", "reason": None}
        entries.append(item)
        if wt.get("locked"):
            item["reason"] = "locked"
            continue
        if wt.get("prunable") or not os.path.isdir(path):
            item.update(action="prune", reason="missing_directory")
            continue
        if _inside(caller, path):
            item["reason"] = "in_use_by_caller"
            continue
        newest = _newest_activity(runner, path)
        if newest is None:
            item["reason"] = "activity_unmeasured"
            continue
        if newest > cutoff:
            item["reason"] = "too_recent"
            continue
        status = _git(runner, "-C", path, "status", "--porcelain", "--untracked-files=normal")
        if not _ok(status):
            item["reason"] = "status_unavailable"
            continue
        if status.stdout.strip():
            item["reason"] = "dirty"
            continue
        head = wt.get("head")
        ahead = _git(runner, "-C", repo_root, "rev-list", "--count", f"{base_sha}..{head}")
        if _ok(ahead) and ahead.stdout.strip() == "0":
            item.update(action="remove", reason="merged_into_base")
            continue
        same_tree = _git(runner, "-C", repo_root, "diff", "--quiet", base_sha, head)
        if same_tree is not None and same_tree.returncode == 0:
            item.update(action="remove", reason="tree_identical_to_base")
            continue
        item["reason"] = "unmerged_commits"
    counts: dict[str, int] = {}
    for item in entries:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return {"schema": SCHEMA, "mode": "plan", "repo": repo_root, "base": base,
            "base_commit": base_sha, "min_age_hours": float(min_age_hours),
            "counts": counts, "entries": entries}


def running_job_count(job_dir: str | None = None) -> int:
    """Running Summon background jobs in the resolved jobs dir; -1 when unknown."""
    try:
        import _jobs
        root = _jobs.resolve_jobs_dir(job_dir)
        return sum(1 for row in _jobs.list_jobs(root) if row.get("state") == "running")
    except Exception:  # noqa: BLE001 - unknown is treated as "not idle" by apply()
        return -1


def apply(gc_plan: dict, *, runner: Runner = subprocess.run,
          running_jobs: int = 0, ignore_running_jobs: bool = False) -> dict:
    """Apply a plan produced by :func:`plan`. Uses only non-forcing git commands."""
    if not isinstance(gc_plan, dict) or gc_plan.get("schema") != SCHEMA:
        raise ValueError("not a worktree-gc plan")
    out = dict(gc_plan)
    out["mode"] = "apply"
    if running_jobs != 0 and not ignore_running_jobs:
        out["refused"] = ("background jobs are running or could not be counted; "
                          "rerun when idle or pass --ignore-running-jobs")
        out["results"] = []
        return out
    repo = gc_plan["repo"]
    results = []
    pruned = False
    for item in gc_plan.get("entries", []):
        action = item.get("action")
        if action == "prune" and not pruned:
            proc = _git(runner, "-C", repo, "worktree", "prune")
            pruned = True
            results.append({"branch": item["branch"], "action": "prune", "ok": _ok(proc)})
        elif action == "remove":
            removed = _git(runner, "-C", repo, "worktree", "remove", item["path"])
            entry = {"branch": item["branch"], "action": "remove", "ok": _ok(removed)}
            if _ok(removed):
                deleted = _git(runner, "-C", repo, "branch", "-d", item["branch"])
                entry["branch_deleted"] = _ok(deleted)
            else:
                entry["error"] = ((getattr(removed, "stderr", "") or "").strip()[:300]
                                  if removed is not None else "git unavailable")
            results.append(entry)
    out["results"] = results
    return out


def render(report: dict) -> str:
    """Human-readable plan/apply summary (paths are local operator output)."""
    lines = [f"worktree gc ({report.get('mode')}) base={report.get('base')} "
             f"min_age_hours={report.get('min_age_hours')}"]
    for item in report.get("entries", []):
        lines.append(f"  {item.get('action'):6} {item.get('branch')}  [{item.get('reason')}]"
                     f"  {item.get('path')}")
    if not report.get("entries"):
        lines.append("  no Summon agent worktrees found")
    if report.get("refused"):
        lines.append(f"refused: {report['refused']}")
    for result in report.get("results", []):
        state = "ok" if result.get("ok") else f"FAILED: {result.get('error', '')}"
        lines.append(f"  {result.get('action')} {result.get('branch')}: {state}")
    if report.get("mode") == "plan" and report.get("counts", {}).get("remove"):
        lines.append("dry run: rerun with --apply to remove the planned worktrees")
    return "\n".join(lines)
