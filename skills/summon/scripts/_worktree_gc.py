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
(never ``-D``), so git itself re-checks cleanliness and merge state. ``apply``
also recomputes a fresh plan and re-checks branch, HEAD and cleanliness right
before each removal, so a stale plan cannot authorize deletion. Missing
worktree directories are reported, never pruned (``git worktree prune`` is
repository-wide). Applying is refused while any Summon background job is running
or job activity cannot be established, unless explicitly overridden.
"""

from __future__ import annotations

import math
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
    if (not isinstance(min_age_hours, (int, float)) or isinstance(min_age_hours, bool)
            or not math.isfinite(min_age_hours) or min_age_hours < 0):
        raise ValueError("min_age_hours must be a finite, non-negative number")
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
            # Repository-wide `git worktree prune` would exceed this command's
            # scope (it also prunes non-Summon metadata), so only report it.
            item["reason"] = "missing_directory"
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
        status_out = status.stdout
        try:
            from _worktree_deps import filter_porcelain_v1_text
            status_out = filter_porcelain_v1_text(status_out, path)
        except Exception:  # noqa: BLE001
            pass
        if status_out.strip():
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


_SETTLED_JOB_STATES = frozenset({"stale", "success", "partial", "blocked", "error",
                                 "cancelled"})
_ACTIVE_JOB_STATES = frozenset({"running", "prepared"})


def running_job_count(job_dir: str | None = None) -> int:
    """Possibly-active Summon background jobs; -1 whenever activity is uncertain.

    Unreadable job state and states such as ``unverified`` or ``corrupt`` are
    uncertain, never "idle": apply() refuses on -1 without an explicit override.
    """
    try:
        import _jobs
        root = _jobs.resolve_jobs_dir(job_dir)
        records = os.path.join(root, _jobs._RECORDS)
        for directory in (root, records):
            # Enumerate directly: os.path.exists() hides access errors, and
            # list_jobs() swallows listing errors. Only true absence is "no jobs".
            try:
                os.listdir(directory)
            except FileNotFoundError:
                continue
            except OSError:
                return -1
        active = 0
        for row in _jobs.list_jobs(root):
            state = row.get("state")
            if state in _ACTIVE_JOB_STATES:
                active += 1
            elif state not in _SETTLED_JOB_STATES:
                return -1
        return active
    except Exception:  # noqa: BLE001 - unknown is treated as "not idle" by apply()
        return -1


def _revalidate(item: dict, current: dict | None) -> str | None:
    """Reason to skip a planned removal when the fresh plan no longer agrees."""
    if current is None:
        return "no_longer_listed"
    if current.get("branch") != item.get("branch") or current.get("head") != item.get("head"):
        return "changed_since_plan"
    if current.get("action") != "remove":
        return f"no_longer_eligible:{current.get('reason')}"
    return None


def apply(gc_plan: dict, *, runner: Runner = subprocess.run,
          running_jobs: int = 0, ignore_running_jobs: bool = False,
          now: Callable[[], float] = time.time, caller_cwd: str | None = None) -> dict:
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
    # Revalidate: a stale plan must not authorize removal. Recompute the full
    # eligibility (integration, age, cleanliness) now, then re-check branch,
    # HEAD and cleanliness immediately before each individual removal.
    fresh = plan(repo, base=gc_plan.get("base", "HEAD"),
                 min_age_hours=gc_plan.get("min_age_hours", DEFAULT_MIN_AGE_HOURS),
                 runner=runner, now=now, caller_cwd=caller_cwd)
    current_by_path = {os.path.normcase(e["path"]): e for e in fresh["entries"]}
    results = []
    for item in gc_plan.get("entries", []):
        if item.get("action") != "remove":
            continue
        entry = {"branch": item["branch"], "action": "remove", "ok": False}
        skip = _revalidate(item, current_by_path.get(os.path.normcase(item["path"])))
        if skip is None:
            head = _git(runner, "-C", item["path"], "rev-parse", "HEAD")
            ref = _git(runner, "-C", item["path"], "symbolic-ref", "-q", "HEAD")
            status = _git(runner, "-C", item["path"], "status", "--porcelain",
                          "--untracked-files=normal")
            if not (_ok(head) and _ok(ref) and _ok(status)):
                skip = "recheck_unavailable"
            elif (head.stdout.strip() != item.get("head")
                  or ref.stdout.strip() != f"refs/heads/{item['branch']}"):
                skip = "changed_since_plan"
            else:
                status_out = status.stdout
                try:
                    from _worktree_deps import filter_porcelain_v1_text
                    status_out = filter_porcelain_v1_text(status_out, item["path"])
                except Exception:  # noqa: BLE001
                    pass
                if status_out.strip():
                    skip = "dirty"
        if skip is None:
            try:
                from _worktree_deps import teardown_linked_deps
                td = teardown_linked_deps(item["path"])
                if td.get("tampered") or td.get("errors"):
                    skip = "dirty"
            except Exception:  # noqa: BLE001
                skip = "dirty"
        if skip is not None:
            entry["skipped"] = skip
            entry["ok"] = True          # a refusal to act is the safe, expected outcome
            results.append(entry)
            continue
        removed = _git(runner, "-C", repo, "worktree", "remove", item["path"])
        entry["ok"] = _ok(removed)
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
        state = ("ok" if result.get("ok") and not result.get("skipped")
                 else f"skipped ({result['skipped']})" if result.get("skipped")
                 else f"FAILED: {result.get('error', '')}")
        lines.append(f"  {result.get('action')} {result.get('branch')}: {state}")
    if report.get("mode") == "plan" and report.get("counts", {}).get("remove"):
        lines.append("dry run: rerun with --apply to remove the planned worktrees")
    return "\n".join(lines)
