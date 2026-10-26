"""Coordinator-led long-horizon mission engine for Summon.

Provides a durable, single-owner state machine built on ``_rundir.py``,
``_manifest.py``, ``_mission_budget.py``, and ``_resource_governor.py``:

- ``summon.mission/v1``: typed status projection, digest, budget, and git
  integration state.
- ``summon.mission-plan/v1``: validated coordinator proposal schema with
  downward permission clamping, team allowlist verification, DAG cycle
  checking, and ``files_scope`` normalization.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import _mission_budget
import _resource_governor
import _rundir
from _builder import clamp_permission
from _executor import is_terminal_success
from _spawn import run_flags

MISSION_SCHEMA = "summon.mission/v1"
MISSION_PLAN_SCHEMA = "summon.mission-plan/v1"

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VALID_PLAN_STATUSES = frozenset({"ready", "needs_human", "done", "blocked"})
_PERMISSION_RANK = {"read-only": 0, "safe-edit": 1, "yolo": 2}


class MissionError(ValueError):
    """Typed mission error carrying an ``error_kind``."""

    def __init__(self, message: str, *, error_kind: str = "mission_invalid"):
        super().__init__(message)
        self.error_kind = error_kind


def resolve_missions_root(
    cwd: str | None = None,
    run_dir: str | None = None,
    results_dir: str | None = None,
) -> str:
    """Resolve the root directory for durable mission runs."""
    explicit = (
        run_dir
        or results_dir
        or os.environ.get("SUMMON_MISSIONS_DIR")
        or os.environ.get("SUMMON_RUNS_DIR")
    )
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    base = os.path.abspath(cwd or os.getcwd())
    return os.path.join(base, ".agents", "missions")


def normalize_files_scope(files_scope: Any) -> list[str]:
    """Validate and normalize a task's ``files_scope`` patterns into relative POSIX paths."""
    if files_scope is None:
        return []
    if not isinstance(files_scope, list):
        raise MissionError(
            "task files_scope must be a list of relative path patterns",
            error_kind="invalid_files_scope",
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in files_scope:
        if not isinstance(raw, str) or not raw.strip():
            raise MissionError(
                "task files_scope entries must be non-empty strings",
                error_kind="invalid_files_scope",
            )
        clean = raw.strip().replace("\\", "/")
        if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
            raise MissionError(
                f"task files_scope entry {raw!r} must be relative, not absolute",
                error_kind="invalid_files_scope",
            )
        norm = posixpath.normpath(clean)
        if norm in ("", ".") or norm == ".." or norm.startswith("../") or "/../" in f"/{norm}/":
            raise MissionError(
                f"task files_scope entry {raw!r} escapes the repository root",
                error_kind="invalid_files_scope",
            )
        if clean.endswith("/") and not norm.endswith("/"):
            norm = norm + "/"
        if norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    return normalized


def path_matches_scope(rel_path: str, files_scope: list[str]) -> bool:
    """Return True if ``rel_path`` is covered by ``files_scope`` (empty scope = unrestricted)."""
    if not files_scope:
        return True
    candidate = rel_path.strip().replace("\\", "/").lstrip("/")
    cand_norm = posixpath.normpath(candidate)
    for pattern in files_scope:
        pat = pattern.strip().replace("\\", "/")
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            if cand_norm == prefix or cand_norm.startswith(prefix + "/"):
                return True
        elif "*" in pat or "?" in pat or "[" in pat:
            if fnmatch.fnmatchcase(cand_norm, pat):
                return True
        else:
            pat_norm = posixpath.normpath(pat)
            if cand_norm == pat_norm or cand_norm.startswith(pat_norm + "/"):
                return True
    return False


def _clamp_tier(proposed: str | None, ceiling: str) -> tuple[str, bool]:
    """Clamp ``proposed`` permission tier downward to ``ceiling``."""
    ceil_norm = ceiling if ceiling in _PERMISSION_RANK else "safe-edit"
    if not proposed or proposed not in _PERMISSION_RANK:
        return ceil_norm, False
    if _PERMISSION_RANK[proposed] > _PERMISSION_RANK[ceil_norm]:
        return ceil_norm, True
    return proposed, False


def _validate_dag(tasks: list[dict[str, Any]]) -> list[list[str]]:
    """Verify task IDs and dependency graph acyclicity via Kahn's algorithm."""
    by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        tid = task["id"]
        if tid in by_id:
            raise MissionError(
                f"duplicate task id {tid!r} in mission plan",
                error_kind="duplicate_task_id",
            )
        by_id[tid] = task

    in_degree: dict[str, int] = {tid: 0 for tid in by_id}
    dependents: dict[str, list[str]] = {tid: [] for tid in by_id}
    for tid, task in by_id.items():
        for dep in task.get("depends_on") or []:
            if dep == tid:
                raise MissionError(
                    f"task {tid!r} cannot depend on itself",
                    error_kind="dag_cycle",
                )
            if dep not in by_id:
                raise MissionError(
                    f"task {tid!r} depends on unknown task {dep!r}",
                    error_kind="unknown_dependency",
                )
            in_degree[tid] += 1
            dependents[dep].append(tid)

    waves: list[list[str]] = []
    ready = sorted(tid for tid, deg in in_degree.items() if deg == 0)
    visited = 0
    while ready:
        waves.append(ready)
        next_ready: list[str] = []
        for tid in ready:
            visited += 1
            for child in dependents[tid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_ready.append(child)
        ready = sorted(next_ready)

    if visited != len(by_id):
        cycle_nodes = sorted(tid for tid, deg in in_degree.items() if deg > 0)
        raise MissionError(
            f"mission plan contains a dependency cycle involving: {', '.join(cycle_nodes)}",
            error_kind="dag_cycle",
        )
    return waves


def validate_mission_plan(
    raw_plan: dict[str, Any],
    *,
    allowed_team: list[str] | None = None,
    max_permission: str = "safe-edit",
    roster_permissions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and clamp a coordinator proposal into a canonical ``summon.mission-plan/v1``."""
    if not isinstance(raw_plan, dict):
        raise MissionError(
            "mission plan must be a JSON object",
            error_kind="invalid_plan_schema",
        )
    schema = raw_plan.get("schema", MISSION_PLAN_SCHEMA)
    if schema != MISSION_PLAN_SCHEMA:
        raise MissionError(
            f"unsupported mission plan schema {schema!r} (expected {MISSION_PLAN_SCHEMA!r})",
            error_kind="invalid_plan_schema",
        )
    status = str(raw_plan.get("status") or "ready").strip().lower()
    if status not in _VALID_PLAN_STATUSES:
        raise MissionError(
            f"invalid mission plan status {status!r} (expected one of {sorted(_VALID_PLAN_STATUSES)})",
            error_kind="invalid_plan_status",
        )

    team_allow = {a.strip() for a in (allowed_team or []) if a and a.strip()}
    raw_tasks = raw_plan.get("tasks")
    if raw_tasks is None:
        raw_tasks = []
    if not isinstance(raw_tasks, list):
        raise MissionError(
            "mission plan 'tasks' must be a list",
            error_kind="invalid_plan_tasks",
        )

    validated_tasks: list[dict[str, Any]] = []
    clamped_events: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise MissionError(
                f"task at index {idx} must be a JSON object",
                error_kind="invalid_plan_tasks",
            )
        tid = str(item.get("id") or f"task-{idx + 1}").strip()
        if not _TASK_ID_RE.fullmatch(tid) or ".." in tid:
            raise MissionError(
                f"invalid task id {tid!r}",
                error_kind="invalid_task_id",
            )
        agent = str(item.get("agent") or "").strip()
        if not agent:
            raise MissionError(
                f"task {tid!r} is missing required field 'agent'",
                error_kind="missing_task_agent",
            )
        if team_allow and agent not in team_allow:
            raise MissionError(
                f"task {tid!r} proposed agent {agent!r} which is not in the allowed team: {sorted(team_allow)}",
                error_kind="unapproved_team_agent",
            )
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise MissionError(
                f"task {tid!r} is missing a non-empty 'prompt'",
                error_kind="missing_task_prompt",
            )

        proposed_perm = item.get("permission") or item.get("max_permission")
        if proposed_perm is not None:
            proposed_perm = str(proposed_perm).strip().lower()
            if proposed_perm not in _PERMISSION_RANK:
                raise MissionError(
                    f"task {tid!r} has invalid permission {proposed_perm!r}",
                    error_kind="invalid_task_permission",
                )
        eff_perm, was_clamped = _clamp_tier(proposed_perm, max_permission)
        if roster_permissions and agent in roster_permissions:
            roster_perm = roster_permissions[agent]
            final_perm = clamp_permission(roster_perm, eff_perm)
            if final_perm != eff_perm:
                was_clamped = True
                eff_perm = final_perm
        if was_clamped:
            clamped_events.append({
                "task_id": tid,
                "agent": agent,
                "proposed_permission": proposed_perm,
                "effective_permission": eff_perm,
                "ceiling": max_permission,
            })

        raw_deps = item.get("depends_on") or []
        if not isinstance(raw_deps, list) or not all(isinstance(d, str) and d.strip() for d in raw_deps):
            raise MissionError(
                f"task {tid!r} depends_on must be a list of task id strings",
                error_kind="invalid_depends_on",
            )
        deps = [d.strip() for d in raw_deps]
        files_scope = normalize_files_scope(item.get("files_scope"))
        raw_wt = item.get("worktree")
        if eff_perm != "read-only":
            worktree_val = raw_wt.strip() if isinstance(raw_wt, str) and raw_wt.strip() else True
        else:
            worktree_val = raw_wt.strip() if isinstance(raw_wt, str) and raw_wt.strip() else bool(raw_wt)

        validated_tasks.append({
            "id": tid,
            "agent": agent,
            "prompt": prompt,
            "permission": eff_perm,
            "depends_on": deps,
            "files_scope": files_scope,
            "worktree": worktree_val,
            "forward_handoff": bool(item.get("forward_handoff", True)),
            "link_deps": item.get("link_deps"),
        })

    waves = _validate_dag(validated_tasks) if validated_tasks else []

    raw_questions = raw_plan.get("questions") or []
    if not isinstance(raw_questions, list):
        raise MissionError(
            "mission plan 'questions' must be a list",
            error_kind="invalid_questions",
        )
    questions: list[dict[str, Any]] = []
    seen_qids: set[str] = set()
    for q_idx, q_item in enumerate(raw_questions):
        if isinstance(q_item, str) and q_item.strip():
            qid = f"q-{q_idx + 1}"
            q_obj = {
                "question_id": qid,
                "prompt": q_item.strip(),
                "options": [],
                "default_action": None,
            }
        elif isinstance(q_item, dict):
            qid = str(q_item.get("question_id") or q_item.get("id") or f"q-{q_idx + 1}").strip()
            if not _TASK_ID_RE.fullmatch(qid):
                raise MissionError(
                    f"invalid question_id {qid!r}",
                    error_kind="invalid_question_id",
                )
            q_prompt = str(q_item.get("prompt") or q_item.get("question") or "").strip()
            if not q_prompt:
                raise MissionError(
                    f"question {qid!r} requires a non-empty prompt",
                    error_kind="invalid_questions",
                )
            opts = q_item.get("options") or []
            if not isinstance(opts, list):
                opts = []
            q_obj = {
                "question_id": qid,
                "prompt": q_prompt,
                "options": [str(o) for o in opts if str(o).strip()],
                "default_action": (
                    str(q_item["default_action"]).strip()
                    if q_item.get("default_action") is not None
                    else None
                ),
            }
        else:
            raise MissionError(
                f"question at index {q_idx} must be an object or string",
                error_kind="invalid_questions",
            )
        if q_obj["question_id"] in seen_qids:
            raise MissionError(
                f"duplicate question_id {q_obj['question_id']!r}",
                error_kind="duplicate_question_id",
            )
        seen_qids.add(q_obj["question_id"])
        questions.append(q_obj)

    if status == "needs_human" and not questions:
        raise MissionError(
            "mission plan with status='needs_human' must include at least one question",
            error_kind="missing_questions_for_needs_human",
        )

    return {
        "schema": MISSION_PLAN_SCHEMA,
        "status": status,
        "summary": str(raw_plan.get("summary") or "").strip(),
        "tasks": validated_tasks,
        "waves": waves,
        "questions": questions,
        "clamped_permissions": clamped_events,
    }


def _git_prefix() -> list[str]:
    return ["git", "-c", "core.longpaths=true"]


def _git_repo_root(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            [*_git_prefix(), "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            **run_flags(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.abspath(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def _git_head(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            [*_git_prefix(), "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            **run_flags(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def setup_integration_worktree(
    repo_root: str,
    mission_dir: str,
    mission_id: str,
    base_head: str,
) -> dict[str, Any]:
    """Create or attach the mission integration branch ``mission/<mission_id>``."""
    branch = f"mission/{mission_id}"
    wt_path = os.path.join(mission_dir, "integration_wt")
    git_cmd = [*_git_prefix(), "-C", repo_root]
    if os.path.isdir(wt_path) and os.path.exists(os.path.join(wt_path, ".git")):
        head = _git_head(wt_path) or base_head
        return {
            "enabled": True,
            "repo_root": repo_root,
            "branch": branch,
            "worktree_path": wt_path,
            "base_head": base_head,
            "current_head": head,
        }
    proc = subprocess.run(
        [*git_cmd, "worktree", "add", "-B", branch, wt_path, base_head],
        capture_output=True,
        text=True,
        timeout=30,
        **run_flags(),
    )
    if proc.returncode != 0:
        return {
            "enabled": False,
            "repo_root": repo_root,
            "branch": branch,
            "worktree_path": None,
            "base_head": base_head,
            "current_head": base_head,
            "error": (proc.stderr or proc.stdout or "git worktree add failed").strip(),
        }
    return {
        "enabled": True,
        "repo_root": repo_root,
        "branch": branch,
        "worktree_path": wt_path,
        "base_head": base_head,
        "current_head": _git_head(wt_path) or base_head,
    }


def verify_and_integrate_task(
    *,
    task: dict[str, Any],
    envelope: dict[str, Any],
    integration: dict[str, Any],
) -> dict[str, Any]:
    """Verify a task's terminal verdict, enforce ``files_scope``, and merge into ``mission/<id>``."""
    tid = task["id"]
    if not is_terminal_success(envelope):
        return {
            "task_id": tid,
            "status": "skipped_non_success",
            "merged": False,
            "reason": f"task ended with status={envelope.get('status')!r}",
        }
    report = envelope.get("report") if isinstance(envelope.get("report"), dict) else {}
    env_verdict = str(envelope.get("verdict") or "").strip().lower()
    rep_verdict = str(report.get("verdict") or "").strip().lower()
    if env_verdict == "block" or rep_verdict == "block":
        return {
            "task_id": tid,
            "status": "blocked_by_verdict",
            "merged": False,
            "reason": "task emitted verdict=block",
        }

    wt_info = envelope.get("worktree") if isinstance(envelope.get("worktree"), dict) else {}
    task_wt_path = wt_info.get("path") or wt_info.get("cwd")
    base_head = integration.get("base_head") or wt_info.get("base_head")
    if not task_wt_path or not os.path.isdir(str(task_wt_path)):
        return {
            "task_id": tid,
            "status": "no_worktree",
            "merged": False,
            "reason": "task ran without an isolated git worktree",
        }

    task_wt = str(task_wt_path)
    git_wt = [*_git_prefix(), "-C", task_wt]

    try:
        # Auto-commit any uncommitted edits left in the task worktree so diff/merge is complete.
        status_proc = subprocess.run(
            [*git_wt, "status", "--porcelain=v1", "--untracked-files=all", "-z"],
            capture_output=True,
            timeout=20,
            **run_flags(),
        )
        if status_proc.returncode != 0:
            return {
                "task_id": tid,
                "status": "git_verification_failed",
                "merged": False,
                "reason": "git status failed in task worktree",
            }
        dirty_paths: list[str] = []
        if status_proc.stdout:
            entries = [e.decode("utf-8", errors="replace") for e in status_proc.stdout.split(b"\x00") if e]
            idx = 0
            while idx < len(entries):
                entry = entries[idx]
                if len(entry) >= 4:
                    xy = entry[:2]
                    rel = entry[3:]
                    dirty_paths.append(rel)
                    if "R" in xy or "C" in xy:
                        idx += 1
                        if idx < len(entries):
                            dirty_paths.append(entries[idx])
                idx += 1

        files_scope = task.get("files_scope") or []
        # Check dirty paths against files_scope BEFORE staging/committing!
        out_of_scope: list[str] = [
            p for p in dirty_paths if not path_matches_scope(p, files_scope)
        ]

        if dirty_paths and not out_of_scope:
            add_proc = subprocess.run([*git_wt, "add", "-A"], capture_output=True, timeout=20, **run_flags())
            if add_proc.returncode != 0:
                return {
                    "task_id": tid,
                    "status": "git_verification_failed",
                    "merged": False,
                    "reason": "git add failed in task worktree",
                }
            commit_proc = subprocess.run(
                [
                    *_git_prefix(),
                    "-C",
                    task_wt,
                    "-c",
                    "user.name=Summon Mission",
                    "-c",
                    "user.email=mission@summon.local",
                    "commit",
                    "-m",
                    f"mission task({tid}): auto-commit worktree changes",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                **run_flags(),
            )
            if commit_proc.returncode != 0:
                return {
                    "task_id": tid,
                    "status": "git_verification_failed",
                    "merged": False,
                    "reason": "git commit failed in task worktree",
                }

        task_head = _git_head(task_wt)
        if not task_head or not base_head:
            return {
                "task_id": tid,
                "status": "no_commits",
                "merged": False,
                "reason": "could not resolve task HEAD or base HEAD",
            }

        # Inspect all files changed between base_head and task_head.
        diff_proc = subprocess.run(
            [*git_wt, "diff", "--no-renames", "--name-only", "-z", f"{base_head}..{task_head}"],
            capture_output=True,
            timeout=20,
            **run_flags(),
        )
        if diff_proc.returncode != 0:
            return {
                "task_id": tid,
                "status": "git_verification_failed",
                "merged": False,
                "reason": "git diff failed in task worktree",
            }
        committed_paths: list[str] = []
        if diff_proc.stdout:
            committed_paths = [
                p.decode("utf-8", errors="replace")
                for p in diff_proc.stdout.split(b"\x00")
                if p
            ]

        for p in committed_paths:
            if not path_matches_scope(p, files_scope) and p not in out_of_scope:
                out_of_scope.append(p)

        touched_files = sorted(set(dirty_paths) | set(committed_paths))
        if out_of_scope:
            return {
                "task_id": tid,
                "status": "scope_violation",
                "merged": False,
                "touched_files": touched_files,
                "out_of_scope_files": sorted(set(out_of_scope)),
                "files_scope": files_scope,
                "reason": (
                    f"task {tid!r} modified files outside its allowed files_scope "
                    f"{files_scope}: {sorted(set(out_of_scope))}"
                ),
            }

        if task_head == base_head:
            return {
                "task_id": tid,
                "status": "clean_no_changes",
                "merged": False,
                "touched_files": [],
                "reason": "task completed with no repository changes",
            }

        if not integration.get("enabled") or not integration.get("worktree_path"):
            return {
                "task_id": tid,
                "status": "verified_unmerged",
                "merged": False,
                "task_head": task_head,
                "touched_files": touched_files,
                "reason": "integration worktree not enabled",
            }

        int_wt = str(integration["worktree_path"])
        merge_proc = subprocess.run(
            [
                *_git_prefix(),
                "-C",
                int_wt,
                "-c",
                "user.name=Summon Mission",
                "-c",
                "user.email=mission@summon.local",
                "merge",
                "--no-ff",
                "-m",
                f"mission: integrate task {tid}",
                task_head,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **run_flags(),
        )
        if merge_proc.returncode != 0:
            subprocess.run(
                [*_git_prefix(), "-C", int_wt, "merge", "--abort"],
                capture_output=True,
                timeout=15,
                **run_flags(),
            )
            return {
                "task_id": tid,
                "status": "merge_conflict",
                "merged": False,
                "task_head": task_head,
                "touched_files": touched_files,
                "reason": (merge_proc.stderr or merge_proc.stdout or "git merge conflict").strip(),
            }
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "task_id": tid,
            "status": "git_verification_failed",
            "merged": False,
            "reason": f"git verification error: {exc}",
        }

    merged_head = _git_head(int_wt)
    integration["current_head"] = merged_head
    return {
        "task_id": tid,
        "status": "merged",
        "merged": True,
        "task_head": task_head,
        "merged_head": merged_head,
        "touched_files": touched_files,
    }


def _coordinator_prompt(
    *,
    objective: str,
    team: list[str],
    max_permission: str,
    wave_number: int,
    max_waves: int,
    prior_tasks: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    steering: list[dict[str, Any]],
) -> str:
    """Build a structured planning prompt for the Coordinator Agent."""
    team_str = ", ".join(team) if team else "architect, implementer, reviewer"
    lines = [
        "You are the Summon Mission Coordinator. Decompose the objective into a safe, parallelizable DAG of sub-agent tasks.",
        "IMPORTANT: Your output is a PROPOSAL that Summon's deterministic engine will validate and clamp.",
        "",
        f"Mission Objective:\n{objective}",
        "",
        f"Wave: {wave_number} of {max_waves}",
        f"Allowed Team Agents: {team_str}",
        f"Maximum Permission Ceiling: {max_permission}",
    ]
    if prior_tasks:
        lines.append("\nCompleted / Prior Tasks:")
        for pt in prior_tasks:
            lines.append(
                f"- {pt.get('id')} ({pt.get('agent')}): status={pt.get('status')}, "
                f"integration={pt.get('integration_status')}, summary={pt.get('summary')!r}"
            )
    if answers:
        lines.append("\nOperator Answers to Prior Questions:")
        for ans in answers:
            lines.append(f"- [{ans.get('question_id')}]: {ans.get('answer')}")
    if steering:
        lines.append("\nOperator Steering Directives:")
        for st in steering:
            lines.append(f"- {st.get('message')}")

    lines.extend([
        "",
        "Respond with ONLY a valid JSON object matching schema 'summon.mission-plan/v1':",
        json.dumps(
            {
                "schema": MISSION_PLAN_SCHEMA,
                "status": "ready",
                "summary": "Brief one-line summary of this wave's plan",
                "tasks": [
                    {
                        "id": "task-1",
                        "agent": team[0] if team else "architect",
                        "prompt": "Detailed self-contained task instructions",
                        "permission": max_permission,
                        "depends_on": [],
                        "files_scope": ["src/"],
                        "worktree": max_permission != "read-only",
                    }
                ],
                "questions": [],
            },
            indent=2,
        ),
    ])
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from raw model output (tolerating markdown fences)."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise MissionError("coordinator returned empty output", error_kind="coordinator_output_invalid")
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    raise MissionError(
        "coordinator response did not contain a valid JSON mission plan object",
        error_kind="coordinator_output_invalid",
    )


def project_mission_status(mission_dir: str) -> dict[str, Any]:
    """Reconstruct and validate the ``summon.mission/v1`` projection from durable state."""
    receipt = _rundir.read_json(os.path.join(mission_dir, "receipt.json")) or {}
    saved_proj = _rundir.read_json(os.path.join(mission_dir, "projection.json")) or {}
    records, torn_tail = _rundir.journal_read(mission_dir)

    mission_id = receipt.get("mission_id") or saved_proj.get("mission_id") or os.path.basename(mission_dir)
    status = saved_proj.get("status") or receipt.get("status") or "running"
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for t in saved_proj.get("tasks") or []:
        if isinstance(t, dict) and t.get("id"):
            tasks_by_id[str(t["id"])] = dict(t)

    questions_by_id: dict[str, dict[str, Any]] = {}
    for q in saved_proj.get("open_questions") or []:
        if isinstance(q, dict) and q.get("question_id"):
            questions_by_id[str(q["question_id"])] = dict(q)

    answers: list[dict[str, Any]] = list(saved_proj.get("answers") or [])
    answered_ids = {str(a.get("question_id")) for a in answers if isinstance(a, dict)}
    steering: list[dict[str, Any]] = list(saved_proj.get("steering") or [])
    seen_steering_ids = {str(s.get("steering_id")) for s in steering if isinstance(s, dict) and s.get("steering_id")}

    last_ts = float(saved_proj.get("updated_at") or receipt.get("created_at") or time.time())
    cancel_requested = False
    cancel_reason = None

    for rec in records:
        ts = float(rec.get("ts") or last_ts)
        if ts > last_ts:
            last_ts = ts
        ev = rec.get("event")
        if ev == "question_asked":
            q = rec.get("question")
            if isinstance(q, dict) and q.get("question_id"):
                qid = str(q["question_id"])
                if qid not in answered_ids:
                    questions_by_id[qid] = {**q, "asked_at": ts}
        elif ev == "question_replied":
            qid = str(rec.get("question_id") or "")
            if qid and qid not in answered_ids:
                answered_ids.add(qid)
                questions_by_id.pop(qid, None)
                answers.append({
                    "question_id": qid,
                    "answer": rec.get("answer"),
                    "replied_at": ts,
                })
        elif ev == "steering_queued":
            sid = str(rec.get("steering_id") or "")
            if sid and sid not in seen_steering_ids:
                seen_steering_ids.add(sid)
                steering.append({
                    "steering_id": sid,
                    "task_id": rec.get("task_id"),
                    "message": rec.get("message"),
                    "created_at": ts,
                })
        elif ev == "mission_cancel_requested":
            cancel_requested = True
            cancel_reason = rec.get("reason")
        elif ev == "mission_terminal":
            status = str(rec.get("status") or status)

    #Also check outbox commands (replies, steering, cancel) written by non-owner CLI commands
    commands_dir = os.path.join(mission_dir, "commands")
    if os.path.isdir(commands_dir):
        try:
            for fname in sorted(os.listdir(commands_dir)):
                if not fname.endswith(".json"):
                    continue
                cmd = _rundir.read_json(os.path.join(commands_dir, fname))
                if not isinstance(cmd, dict):
                    continue
                kind = cmd.get("kind")
                cts = float(cmd.get("created_at") or time.time())
                if cts > last_ts:
                    last_ts = cts
                if kind == "reply":
                    qid = str(cmd.get("question_id") or "")
                    if qid and qid not in answered_ids:
                        answered_ids.add(qid)
                        questions_by_id.pop(qid, None)
                        answers.append({
                            "question_id": qid,
                            "answer": cmd.get("answer"),
                            "replied_at": cts,
                        })
                elif kind == "steer":
                    sid = str(cmd.get("steering_id") or fname)
                    if sid not in seen_steering_ids:
                        seen_steering_ids.add(sid)
                        steering.append({
                            "steering_id": sid,
                            "task_id": cmd.get("task_id"),
                            "message": cmd.get("message"),
                            "created_at": cts,
                        })
                elif kind == "cancel":
                    cancel_requested = True
                    cancel_reason = cmd.get("reason")
        except OSError:
            pass

    if cancel_requested and status in ("running", "paused_needs_human"):
        status = "cancelled"
    elif status == "paused_needs_human" and not questions_by_id and answered_ids:
        status = "ready_to_resume"

    now = time.time()
    freshness_ms = max(0, int(round((now - last_ts) * 1000.0)))

    budget_view = None
    try:
        budget_view = _mission_budget.read_mission_budget(mission_id)
    except Exception:  # noqa: BLE001
        budget_view = saved_proj.get("budget")

    resources_view = None
    try:
        resources_view = _resource_governor.snapshot_host_resources(
            cwd=mission_dir
        )
    except Exception:  # noqa: BLE001
        resources_view = saved_proj.get("resources")

    projection = {
        "schema": MISSION_SCHEMA,
        "mission_id": mission_id,
        "status": status,
        "objective": receipt.get("objective") or saved_proj.get("objective") or "",
        "coordinator": receipt.get("coordinator") or saved_proj.get("coordinator") or "architect",
        "team": receipt.get("team") or saved_proj.get("team") or [],
        "max_permission": receipt.get("max_permission") or saved_proj.get("max_permission") or "safe-edit",
        "current_wave": int(saved_proj.get("current_wave") or 1),
        "max_waves": int(receipt.get("max_waves") or saved_proj.get("max_waves") or 1),
        "observed_at": now,
        "updated_at": last_ts,
        "freshness_ms": freshness_ms,
        "torn_tail": torn_tail,
        "cancel_requested": cancel_requested,
        "cancel_reason": cancel_reason,
        "summary": saved_proj.get("summary") or "",
        "tasks": list(tasks_by_id.values()),
        "open_questions": list(questions_by_id.values()),
        "answers": answers,
        "steering": steering,
        "clamped_permissions": saved_proj.get("clamped_permissions") or [],
        "integration": saved_proj.get("integration") or {
            "enabled": False,
            "branch": f"mission/{mission_id}",
            "merged_tasks": [],
            "scope_violations": [],
            "conflicts": [],
        },
        "budget": budget_view,
        "resources": resources_view,
    }
    return projection


def render_mission_digest(proj: dict[str, Any]) -> str:
    """Render a concise, scannable Markdown/text digest of a ``summon.mission/v1`` projection."""
    mid = proj.get("mission_id", "unknown")
    status = str(proj.get("status", "unknown")).upper()
    wave = f"{proj.get('current_wave', 1)}/{proj.get('max_waves', 1)}"
    fresh_ms = proj.get("freshness_ms", 0)
    lines = [
        f"=== MISSION {mid} [{status}] ===",
        f"Objective: {proj.get('objective', '')}",
        f"Coordinator: {proj.get('coordinator')} | Team: {', '.join(proj.get('team') or []) or 'default'} | Ceiling: {proj.get('max_permission')} | Wave: {wave} | Freshness: {fresh_ms}ms",
    ]
    if proj.get("summary"):
        lines.append(f"Summary: {proj['summary']}")

    tasks = proj.get("tasks") or []
    if tasks:
        lines.append("\nTasks:")
        badge_map = {
            "success": "[DONE]",
            "running": "[RUN ]",
            "pending": "[WAIT]",
            "blocked": "[BLK ]",
            "error": "[ERR ]",
            "scope_violation": "[VIOL]",
        }
        for t in tasks:
            t_stat = str(t.get("status") or "pending")
            int_stat = str(t.get("integration_status") or "none")
            if int_stat == "scope_violation":
                badge = "[VIOL]"
            else:
                badge = badge_map.get(t_stat, f"[{t_stat[:4].upper():<4}]")
            deps = ",".join(t.get("depends_on") or []) or "-"
            scope = ",".join(t.get("files_scope") or []) or "*"
            summary = str(t.get("summary") or t.get("error") or "").strip()
            lines.append(
                f"  {badge} {t.get('id')} ({t.get('agent')}, perm={t.get('permission')}, deps={deps}, scope={scope}, git={int_stat})"
                + (f" — {summary}" if summary else "")
            )

    integ = proj.get("integration") or {}
    if integ.get("enabled"):
        lines.append(
            f"\nGit Integration: branch={integ.get('branch')} | "
            f"merged={len(integ.get('merged_tasks') or [])} | "
            f"scope_violations={len(integ.get('scope_violations') or [])} | "
            f"conflicts={len(integ.get('conflicts') or [])}"
        )

    budget = proj.get("budget")
    if isinstance(budget, dict):
        caps = budget.get("caps") if isinstance(budget.get("caps"), dict) else {}
        tok_spent = budget.get("total_known_work_tokens", budget.get("spent_work_tokens", 0))
        tok_max = caps.get("max_work_tokens", budget.get("max_work_tokens"))
        usd_spent = float(budget.get("total_known_cost_usd", budget.get("spent_cost_usd", 0.0)) or 0.0)
        usd_max = caps.get("max_cost_usd", budget.get("max_cost_usd"))
        top = budget.get("top_spender") or {}
        top_str = f"{top.get('job_id')} ({top.get('work_tokens', 0)} tok)" if top.get("job_id") else "none"
        lines.append(
            f"Budget: tokens={tok_spent}/{tok_max if tok_max is not None else 'unlimited'} | "
            f"cost=${usd_spent:.4f}/${f'{float(usd_max):.4f}' if usd_max is not None else 'unlimited'} | "
            f"top_spender={top_str}"
        )

    res = proj.get("resources")
    if isinstance(res, dict):
        pressure = res.get("pressure", "healthy")
        mem = res.get("memory") or {}
        avail_mb = mem.get("avail_ram_mb", mem.get("available_mb"))
        lines.append(
            f"Resources: pressure={pressure} | available_ram={avail_mb if avail_mb is not None else '?'}MB | "
            f"recommended_slots={res.get('recommended_total_slots', '?')}"
        )

    open_qs = proj.get("open_questions") or []
    if open_qs:
        lines.append("\nOpen Questions Requiring Reply:")
        for q in open_qs:
            qid = q.get("question_id")
            opts = f" (options: {', '.join(q.get('options') or [])})" if q.get("options") else ""
            lines.append(f"  - [{qid}] {q.get('prompt')}{opts}")
            lines.append(f"    Reply with: summon mission reply {mid} --question-id {qid} --answer \"<your answer>\"")

    return "\n".join(lines)


def _resolve_roster_permissions(args: Any, cwd: str) -> dict[str, str]:
    """Load roster permission tiers from agent definitions so tasks cannot exceed their roster tier."""
    try:
        import _loader
        agents_dir = _loader.get_agents_dir(getattr(args, "agents_dir", None), cwd)
        roster = _loader.list_agents(
            agents_dir,
            strict=bool(getattr(args, "strict_agents_dir", False)),
            cwd=cwd,
        )
        return {
            str(name): str(cfg.get("permission") or "safe-edit")
            for name, cfg in (roster or {}).items()
            if isinstance(cfg, dict)
        }
    except Exception:  # noqa: BLE001
        return {}


def _load_plan_from_coordinator_or_file(
    *,
    args: Any,
    cwd: str,
    mission_id: str,
    objective: str,
    coordinator: str,
    team: list[str],
    max_permission: str,
    wave_number: int,
    max_waves: int,
    prior_tasks: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    steering: list[dict[str, Any]],
    mission_dir: str,
) -> dict[str, Any]:
    """Obtain a validated ``summon.mission-plan/v1`` either from ``--plan-file`` or by dispatching ``coordinator``."""
    roster_permissions = _resolve_roster_permissions(args, cwd)
    plan_file = getattr(args, "mission_plan_file", None)
    if plan_file:
        try:
            with open(plan_file, encoding="utf-8-sig") as fh:
                raw_plan = json.load(fh)
        except (OSError, ValueError) as exc:
            raise MissionError(
                f"cannot read --plan-file {plan_file!r}: {exc}",
                error_kind="invalid_plan_file",
            ) from exc
        if isinstance(raw_plan, list):
            raw_plan = {
                "schema": MISSION_PLAN_SCHEMA,
                "status": "ready",
                "tasks": raw_plan,
            }
        elif isinstance(raw_plan, dict) and "schema" not in raw_plan and "jobs" in raw_plan:
            raw_plan = {
                "schema": MISSION_PLAN_SCHEMA,
                "status": "ready",
                "tasks": raw_plan["jobs"],
            }
        elif isinstance(raw_plan, dict) and "schema" not in raw_plan:
            raw_plan = {**raw_plan, "schema": MISSION_PLAN_SCHEMA}
        return validate_mission_plan(
            raw_plan,
            allowed_team=team,
            max_permission=max_permission,
            roster_permissions=roster_permissions,
        )

    # Dispatch the coordinator agent in read-only mode to propose a plan
    prompt = _coordinator_prompt(
        objective=objective,
        team=team,
        max_permission=max_permission,
        wave_number=wave_number,
        max_waves=max_waves,
        prior_tasks=prior_tasks,
        answers=answers,
        steering=steering,
    )
    coord_out = os.path.join(mission_dir, f"coordinator-wave-{wave_number}.json")
    entry_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    cmd = [
        sys.executable,
        entry_script,
        "--agent",
        coordinator,
        "--prompt",
        prompt,
        "--cwd",
        cwd,
        "--max-permission",
        "read-only",
        "--out",
        coord_out,
    ]
    if getattr(args, "agents_dir", None):
        cmd.extend(["--agents-dir", args.agents_dir])
    if getattr(args, "strict_agents_dir", False):
        cmd.append("--strict-agents-dir")
    if getattr(args, "enable_roles", False):
        cmd.append("--enable-roles")
    if mission_id:
        cmd.extend(["--mission-id", mission_id])

    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        **run_flags(),
    )
    coord_env = _rundir.read_json(coord_out)
    if not isinstance(coord_env, dict) or coord_env.get("status") != "success":
        err_msg = (coord_env or {}).get("error") or "coordinator dispatch failed"
        raise MissionError(
            f"coordinator {coordinator!r} failed to produce a mission plan: {err_msg}",
            error_kind="coordinator_dispatch_failed",
        )
    raw_plan = _extract_json_object(str(coord_env.get("result") or ""))
    if "schema" not in raw_plan:
        raw_plan["schema"] = MISSION_PLAN_SCHEMA
    return validate_mission_plan(
        raw_plan,
        allowed_team=team,
        max_permission=max_permission,
        roster_permissions=roster_permissions,
    )


def _consume_outbox_commands(mission_dir: str, owner: _rundir.OwnerHandle) -> None:
    """Drain any command files in ``<mission_dir>/commands/*.json`` into the checksummed journal."""
    commands_dir = os.path.join(mission_dir, "commands")
    if not os.path.isdir(commands_dir):
        return
    try:
        names = sorted(f for f in os.listdir(commands_dir) if f.endswith(".json"))
    except OSError:
        return
    for fname in names:
        fpath = os.path.join(commands_dir, fname)
        cmd = _rundir.read_json(fpath)
        if isinstance(cmd, dict):
            kind = cmd.get("kind")
            if kind == "reply":
                _rundir.journal_append(
                    mission_dir,
                    {
                        "event": "question_replied",
                        "generation": owner.generation,
                        "question_id": str(cmd.get("question_id") or ""),
                        "answer": str(cmd.get("answer") or ""),
                    },
                    owner=owner,
                )
            elif kind == "steer":
                _rundir.journal_append(
                    mission_dir,
                    {
                        "event": "steering_queued",
                        "generation": owner.generation,
                        "steering_id": str(cmd.get("steering_id") or fname),
                        "task_id": cmd.get("task_id"),
                        "message": str(cmd.get("message") or ""),
                    },
                    owner=owner,
                )
            elif kind == "cancel":
                _rundir.journal_append(
                    mission_dir,
                    {
                        "event": "mission_cancel_requested",
                        "generation": owner.generation,
                        "reason": str(cmd.get("reason") or "operator requested cancellation"),
                    },
                    owner=owner,
                )
        try:
            os.unlink(fpath)
        except OSError:
            pass


def _augment_task_prompt_with_directives(
    prompt: str,
    task_id: str,
    answers: list[dict[str, Any]],
    steering: list[dict[str, Any]],
) -> str:
    """Append operator answers and applicable steering directives to a task prompt."""
    extra_sections: list[str] = []
    if answers:
        ans_lines = [
            f"- [{a.get('question_id')}]: {a.get('answer')}"
            for a in answers
            if isinstance(a, dict) and a.get("answer")
        ]
        if ans_lines:
            extra_sections.append("[Operator Answers]\n" + "\n".join(ans_lines))

    task_steers = [
        str(s.get("message")).strip()
        for s in steering
        if isinstance(s, dict)
        and s.get("message")
        and (s.get("task_id") in (None, "", task_id))
    ]
    if task_steers:
        extra_sections.append(
            "[Operator Steering Directives]\n" + "\n".join(f"- {m}" for m in task_steers)
        )

    if not extra_sections:
        return prompt
    return prompt.rstrip() + "\n\n" + "\n\n".join(extra_sections)


def _execute_mission_waves(
    *,
    mission_dir: str,
    args: Any,
    owner: _rundir.OwnerHandle,
    receipt: dict[str, Any],
    start_wave: int = 1,
    initial_plan: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Run one or more planning/execution waves under ``owner``."""
    mission_id = str(receipt["mission_id"])
    objective = str(receipt["objective"])
    coordinator = str(receipt["coordinator"])
    team = list(receipt.get("team") or [])
    max_permission = str(receipt.get("max_permission") or "safe-edit")
    max_waves = max(1, int(receipt.get("max_waves") or 1))
    concurrency = str(getattr(args, "concurrency", None) or receipt.get("concurrency") or "auto")
    cwd = str(receipt.get("cwd") or os.path.abspath(getattr(args, "cwd", None) or os.getcwd()))
    integrate_git = not bool(getattr(args, "mission_no_integrate", False))
    max_tokens = getattr(args, "mission_max_work_tokens", None)
    max_cost = getattr(args, "mission_max_cost_usd", None)

    saved_proj = _rundir.read_json(os.path.join(mission_dir, "projection.json")) or {}
    all_task_projections: list[dict[str, Any]] = [
        t for t in (saved_proj.get("tasks") or [])
        if isinstance(t, dict) and t.get("status") not in (None, "pending")
    ]
    all_clamped: list[dict[str, Any]] = list(saved_proj.get("clamped_permissions") or [])

    repo_root = _git_repo_root(cwd) if integrate_git else None
    base_head = _git_head(repo_root) if repo_root else None
    if repo_root and base_head and integrate_git:
        integration = setup_integration_worktree(repo_root, mission_dir, mission_id, base_head)
    else:
        integration = {
            "enabled": False,
            "branch": f"mission/{mission_id}",
            "base_head": base_head,
            "current_head": base_head,
        }
    prev_integ = saved_proj.get("integration") if isinstance(saved_proj.get("integration"), dict) else {}
    integration.setdefault("merged_tasks", list(prev_integ.get("merged_tasks") or []))
    integration.setdefault("scope_violations", list(prev_integ.get("scope_violations") or []))
    integration.setdefault("conflicts", list(prev_integ.get("conflicts") or []))

    results_dir = os.path.join(mission_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    any_failed = False
    any_scope_violation = False
    final_status = "completed"
    last_plan_summary = ""
    current_wave = start_wave

    for wave_idx in range(start_wave, max_waves + 1):
        current_wave = wave_idx
        _consume_outbox_commands(mission_dir, owner)
        live_proj = project_mission_status(mission_dir)
        answers = list(live_proj.get("answers") or [])
        steering = list(live_proj.get("steering") or [])

        if live_proj.get("cancel_requested"):
            final_status = "cancelled"
            break

        if initial_plan is not None and wave_idx == start_wave:
            plan = initial_plan
        else:
            plan = _load_plan_from_coordinator_or_file(
                args=args,
                cwd=cwd,
                mission_id=mission_id,
                objective=objective,
                coordinator=coordinator,
                team=team,
                max_permission=max_permission,
                wave_number=wave_idx,
                max_waves=max_waves,
                prior_tasks=all_task_projections,
                answers=answers,
                steering=steering,
                mission_dir=mission_dir,
            )
            _rundir.atomic_write_json(os.path.join(mission_dir, f"plan-wave-{wave_idx}.json"), plan)
            _rundir.journal_append(
                mission_dir,
                {
                    "event": "plan_validated",
                    "generation": owner.generation,
                    "wave": wave_idx,
                    "plan_status": plan["status"],
                    "task_count": len(plan["tasks"]),
                    "clamped_count": len(plan["clamped_permissions"]),
                },
                owner=owner,
            )
            for q in plan.get("questions") or []:
                _rundir.journal_append(
                    mission_dir,
                    {
                        "event": "question_asked",
                        "generation": owner.generation,
                        "question": q,
                    },
                    owner=owner,
                )

        last_plan_summary = plan.get("summary") or last_plan_summary
        for ce in plan.get("clamped_permissions") or []:
            if ce not in all_clamped:
                all_clamped.append(ce)

        if plan["status"] == "needs_human":
            proj_seed = {
                "schema": MISSION_SCHEMA,
                "mission_id": mission_id,
                "status": "paused_needs_human",
                "objective": objective,
                "coordinator": coordinator,
                "team": team,
                "max_permission": max_permission,
                "current_wave": wave_idx,
                "max_waves": max_waves,
                "updated_at": time.time(),
                "summary": plan.get("summary") or "Mission paused awaiting human reply",
                "tasks": all_task_projections + [
                    {
                        **t,
                        "status": "pending",
                        "integration_status": "pending",
                        "summary": "",
                    }
                    for t in plan["tasks"]
                ],
                "open_questions": plan["questions"],
                "answers": answers,
                "steering": steering,
                "clamped_permissions": all_clamped,
                "integration": integration,
            }
            _rundir.atomic_write_json(os.path.join(mission_dir, "projection.json"), proj_seed)
            _rundir.journal_append(
                mission_dir,
                {
                    "event": "mission_terminal",
                    "generation": owner.generation,
                    "status": "paused_needs_human",
                },
                owner=owner,
            )
            return project_mission_status(mission_dir), 0

        if plan["status"] == "blocked":
            final_status = "blocked"
            break

        tasks = plan["tasks"]
        if plan["status"] == "done" and not tasks:
            final_status = "completed"
            break

        if tasks:
            manifest_doc = {
                "defaults": {
                    "cwd": cwd,
                    "max_permission": max_permission,
                },
                "jobs": [
                    {
                        "id": t["id"],
                        "agent": t["agent"],
                        "prompt": _augment_task_prompt_with_directives(
                            t["prompt"], t["id"], answers, steering
                        ),
                        "max_permission": t["permission"],
                        "depends_on": t["depends_on"],
                        "files_scope": t["files_scope"],
                        "worktree": t["worktree"],
                        "forward_handoff": t["forward_handoff"],
                        **({"link_deps": t["link_deps"]} if t.get("link_deps") else {}),
                    }
                    for t in tasks
                ],
            }
            manifest_path = os.path.join(mission_dir, f"manifest-wave-{wave_idx}.json")
            _rundir.atomic_write_json(manifest_path, manifest_doc)

            from types import SimpleNamespace
            from _manifest import run_manifest

            manifest_args = SimpleNamespace(
                manifest=manifest_path,
                concurrency=concurrency,
                results_dir=results_dir,
                cwd=cwd,
                agents_dir=getattr(args, "agents_dir", None),
                retries=int(getattr(args, "retries", 0) or 0),
                retry_nonretryable=False,
                strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)),
                enable_roles=bool(getattr(args, "enable_roles", False)),
                allow_kimi_acp_fallback=False,
                mission_id=mission_id,
                mission_max_work_tokens=max_tokens,
                mission_max_cost_usd=max_cost,
            )
            import io
            from contextlib import redirect_stdout

            manifest_stdout = io.StringIO()
            with redirect_stdout(manifest_stdout):
                run_manifest(manifest_args)

        task_map = {t["id"]: t for t in tasks}
        ordered_ids = [tid for wave in (plan.get("waves") or []) for tid in wave]
        if not ordered_ids:
            ordered_ids = [t["id"] for t in tasks]

        for tid in ordered_ids:
            t = task_map[tid]
            env_path = os.path.join(results_dir, f"{tid}.json")
            env = _rundir.read_json(env_path) or {}
            t_status = str(env.get("status") or "error")
            int_res = verify_and_integrate_task(
                task=t,
                envelope=env,
                integration=integration,
            )
            int_status = int_res["status"]
            if int_status == "merged":
                if tid not in integration["merged_tasks"]:
                    integration["merged_tasks"].append(tid)
            elif int_status == "scope_violation":
                any_scope_violation = True
                t_status = "blocked"
                integration["scope_violations"].append({
                    "task_id": tid,
                    "out_of_scope_files": int_res.get("out_of_scope_files", []),
                    "files_scope": t.get("files_scope", []),
                })
            elif int_status == "merge_conflict":
                any_failed = True
                integration["conflicts"].append({
                    "task_id": tid,
                    "reason": int_res.get("reason"),
                })
            elif int_status == "git_verification_failed":
                any_failed = True
                t_status = "error"
            elif int_status == "no_worktree" and t.get("permission") != "read-only" and integration.get("enabled"):
                any_failed = True
                t_status = "error"
            elif int_status == "blocked_by_verdict":
                any_failed = True
                t_status = "blocked"

            if t_status not in ("success",):
                any_failed = True

            _rundir.journal_append(
                mission_dir,
                {
                    "event": "task_completed",
                    "generation": owner.generation,
                    "wave": wave_idx,
                    "task_id": tid,
                    "status": t_status,
                    "integration_status": int_status,
                },
                owner=owner,
            )

            report = env.get("report") if isinstance(env.get("report"), dict) else {}
            tp_entry = {
                "id": tid,
                "agent": t["agent"],
                "permission": t["permission"],
                "depends_on": t["depends_on"],
                "files_scope": t["files_scope"],
                "status": t_status,
                "integration_status": int_status,
                "verdict": env.get("verdict") or report.get("verdict"),
                "summary": (
                    int_res.get("reason")
                    if int_status in ("scope_violation", "git_verification_failed")
                    else env.get("summary") or report.get("summary") or env.get("error") or ""
                ),
                "handoff_note": env.get("handoff_note") or report.get("handoff") or "",
                "touched_files": int_res.get("touched_files", []),
            }
            all_task_projections = [p for p in all_task_projections if p.get("id") != tid]
            all_task_projections.append(tp_entry)

        _consume_outbox_commands(mission_dir, owner)
        if project_mission_status(mission_dir).get("cancel_requested"):
            final_status = "cancelled"
            break

        if any_scope_violation or any_failed or plan["status"] == "done":
            break
        if getattr(args, "mission_plan_file", None):
            break

    # Index completed mission handoffs into _memory_index for cross-mission search
    try:
        import _memory_index
        _memory_index.index_workspace_memory(cwd, results_dir=results_dir)
    except Exception:  # noqa: BLE001
        pass

    if final_status == "cancelled":
        pass
    elif any_scope_violation or final_status == "blocked":
        final_status = "blocked"
    elif any_failed:
        final_status = (
            "partial"
            if any(tp.get("status") == "success" for tp in all_task_projections)
            else "error"
        )
    else:
        final_status = "completed"

    live_after = project_mission_status(mission_dir)
    proj_seed = {
        "schema": MISSION_SCHEMA,
        "mission_id": mission_id,
        "status": final_status,
        "objective": objective,
        "coordinator": coordinator,
        "team": team,
        "max_permission": max_permission,
        "current_wave": current_wave,
        "max_waves": max_waves,
        "updated_at": time.time(),
        "summary": last_plan_summary or f"Mission {final_status}",
        "tasks": all_task_projections,
        "open_questions": [],
        "answers": list(live_after.get("answers") or []),
        "steering": list(live_after.get("steering") or []),
        "clamped_permissions": all_clamped,
        "integration": integration,
    }
    _rundir.atomic_write_json(os.path.join(mission_dir, "projection.json"), proj_seed)
    _rundir.journal_append(
        mission_dir,
        {
            "event": "mission_terminal",
            "generation": owner.generation,
            "status": final_status,
        },
        owner=owner,
    )
    proj = project_mission_status(mission_dir)
    exit_code = 0 if final_status in ("completed", "paused_needs_human") else 1
    return proj, exit_code


def start_mission(args: Any) -> tuple[dict[str, Any], int]:
    """Execute ``summon mission start`` and return ``(projection, exit_code)``."""
    cwd = os.path.abspath(getattr(args, "cwd", None) or os.getcwd())
    if not os.path.isdir(cwd):
        raise MissionError(f"mission --cwd does not exist: {cwd!r}", error_kind="invalid_cwd")

    objective = getattr(args, "mission_objective", None) or getattr(args, "prompt", None)
    obj_file = getattr(args, "mission_objective_file", None) or getattr(args, "prompt_file", None)
    if objective is not None and obj_file is not None:
        raise MissionError("give --objective or --objective-file, not both", error_kind="invalid_input")
    if obj_file is not None:
        try:
            with open(obj_file, encoding="utf-8-sig") as fh:
                objective = fh.read()
        except OSError as exc:
            raise MissionError(f"cannot read --objective-file {obj_file!r}: {exc}", error_kind="invalid_input") from exc
    if not objective or not str(objective).strip():
        raise MissionError(
            "mission start requires --objective TEXT or --objective-file FILE",
            error_kind="missing_objective",
        )
    objective = str(objective).strip()

    missions_root = resolve_missions_root(
        cwd=cwd,
        run_dir=getattr(args, "run_dir", None),
        results_dir=getattr(args, "results_dir", None),
    )
    os.makedirs(missions_root, exist_ok=True)
    mission_id = getattr(args, "mission_id", None) or _rundir.new_run_id("mission")
    _rundir.validate_run_id(mission_id)
    mission_dir = _rundir.run_path(missions_root, mission_id)
    os.makedirs(mission_dir, exist_ok=True)

    coordinator = str(getattr(args, "mission_coordinator", None) or "architect").strip()
    raw_team = getattr(args, "mission_team", None)
    team = [a.strip() for a in str(raw_team).split(",") if a.strip()] if raw_team else []
    max_permission = str(getattr(args, "max_permission", None) or "safe-edit").strip()
    if max_permission not in ("read-only", "safe-edit"):
        raise MissionError(
            f"mission --max-permission must be 'read-only' or 'safe-edit', got {max_permission!r}",
            error_kind="invalid_permission_ceiling",
        )
    max_waves = max(1, int(getattr(args, "mission_max_waves", None) or 1))
    concurrency = str(getattr(args, "concurrency", None) or "auto").strip()

    # Configure shared mission budget if requested or initialize default tracker
    max_tokens = getattr(args, "mission_max_work_tokens", None)
    max_cost = getattr(args, "mission_max_cost_usd", None)
    _mission_budget.configure_mission_budget(
        mission_id,
        max_work_tokens=max_tokens,
        max_cost_usd=max_cost,
    )

    owner = _rundir.acquire_owner(mission_dir, lease_sec=1800.0)
    try:
        _rundir.journal_repair(mission_dir, owner)
        created_at = time.time()
        receipt = {
            "schema": MISSION_SCHEMA,
            "mission_id": mission_id,
            "objective": objective,
            "coordinator": coordinator,
            "team": team,
            "max_permission": max_permission,
            "max_waves": max_waves,
            "concurrency": concurrency,
            "cwd": cwd,
            "created_at": created_at,
            "generation": owner.generation,
        }
        _rundir.atomic_write_json(os.path.join(mission_dir, "receipt.json"), receipt)
        _rundir.journal_append(
            mission_dir,
            {
                "event": "mission_started",
                "generation": owner.generation,
                "mission_id": mission_id,
                "coordinator": coordinator,
                "team": team,
                "max_permission": max_permission,
            },
            owner=owner,
        )
        return _execute_mission_waves(
            mission_dir=mission_dir,
            args=args,
            owner=owner,
            receipt=receipt,
            start_wave=1,
        )
    finally:
        _rundir.release_owner(owner)


def resume_mission(mission_dir: str, args: Any) -> tuple[dict[str, Any], int]:
    """Resume a paused or interrupted mission from its durable journal and receipt."""
    owner = _rundir.acquire_owner(mission_dir, lease_sec=1800.0)
    try:
        _rundir.journal_repair(mission_dir, owner)
        _consume_outbox_commands(mission_dir, owner)
        receipt = _rundir.read_json(os.path.join(mission_dir, "receipt.json"))
        if not isinstance(receipt, dict):
            raise MissionError("mission receipt.json is missing or corrupt", error_kind="corrupt_receipt")
        proj = project_mission_status(mission_dir)
        if proj.get("open_questions"):
            raise MissionError(
                f"cannot resume mission {proj.get('mission_id')!r} while open_questions remain unanswered: "
                f"{[q.get('question_id') for q in proj['open_questions']]}",
                error_kind="unanswered_questions",
            )
        cur_wave = max(1, int(proj.get("current_wave") or 1))
        saved_plan = _rundir.read_json(os.path.join(mission_dir, f"plan-wave-{cur_wave}.json"))
        initial_plan: dict[str, Any] | None = None
        if (
            isinstance(saved_plan, dict)
            and saved_plan.get("status") == "needs_human"
            and saved_plan.get("tasks")
            and not getattr(args, "mission_plan_file", None)
        ):
            initial_plan = {
                **saved_plan,
                "status": "ready",
                "questions": [],
            }
        _rundir.journal_append(
            mission_dir,
            {
                "event": "mission_resumed",
                "generation": owner.generation,
                "wave": cur_wave,
            },
            owner=owner,
        )
        return _execute_mission_waves(
            mission_dir=mission_dir,
            args=args,
            owner=owner,
            receipt=receipt,
            start_wave=cur_wave,
            initial_plan=initial_plan,
        )
    finally:
        _rundir.release_owner(owner)


def _write_command_or_journal(
    mission_dir: str,
    *,
    kind: str,
    journal_event: dict[str, Any],
    command_payload: dict[str, Any],
) -> None:
    """Append to the mission journal if unowned, or drop an atomic command file if an active owner holds the lock."""
    try:
        owner = _rundir.acquire_owner(mission_dir, lease_sec=60.0)
    except _rundir.OwnerHeldError:
        cmd_dir = os.path.join(mission_dir, "commands")
        os.makedirs(cmd_dir, exist_ok=True)
        cmd_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        _rundir.atomic_write_json(
            os.path.join(cmd_dir, f"{kind}-{cmd_id}.json"),
            {**command_payload, "kind": kind, "created_at": time.time()},
        )
        return
    try:
        _rundir.journal_repair(mission_dir, owner)
        _rundir.journal_append(
            mission_dir,
            {**journal_event, "generation": owner.generation},
            owner=owner,
        )
    finally:
        _rundir.release_owner(owner)


def reply_mission(
    mission_dir: str,
    *,
    question_id: str,
    answer: str,
) -> dict[str, Any]:
    """Record a question-ID bound reply for an open mission question."""
    if not question_id or not _TASK_ID_RE.fullmatch(question_id):
        raise MissionError(f"invalid --question-id {question_id!r}", error_kind="invalid_question_id")
    if not answer or not str(answer).strip():
        raise MissionError("mission reply requires a non-empty --answer", error_kind="empty_answer")

    proj = project_mission_status(mission_dir)
    open_qids = {str(q.get("question_id")) for q in (proj.get("open_questions") or [])}
    if question_id not in open_qids:
        raise MissionError(
            f"question_id {question_id!r} is not an open question on mission {proj.get('mission_id')!r} "
            f"(open: {sorted(open_qids)})",
            error_kind="unknown_or_closed_question_id",
        )

    _write_command_or_journal(
        mission_dir,
        kind="reply",
        journal_event={
            "event": "question_replied",
            "question_id": question_id,
            "answer": str(answer).strip(),
        },
        command_payload={
            "question_id": question_id,
            "answer": str(answer).strip(),
        },
    )
    return project_mission_status(mission_dir)


def steer_mission(
    mission_dir: str,
    *,
    message: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Queue a steering message for the mission coordinator or a specific task."""
    if not message or not str(message).strip():
        raise MissionError("mission steer requires a non-empty --message", error_kind="empty_message")
    if task_id is not None and not _TASK_ID_RE.fullmatch(task_id):
        raise MissionError(f"invalid --task-id {task_id!r}", error_kind="invalid_task_id")

    steering_id = f"steer-{uuid.uuid4().hex[:8]}"
    _write_command_or_journal(
        mission_dir,
        kind="steer",
        journal_event={
            "event": "steering_queued",
            "steering_id": steering_id,
            "task_id": task_id,
            "message": str(message).strip(),
        },
        command_payload={
            "steering_id": steering_id,
            "task_id": task_id,
            "message": str(message).strip(),
        },
    )
    return project_mission_status(mission_dir)


def cancel_mission(
    mission_dir: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Queue cancellation for a mission and any active background jobs tagged with its mission_id."""
    proj = project_mission_status(mission_dir)
    mid = proj.get("mission_id")
    clean_reason = (reason or "operator requested cancellation").strip()
    _write_command_or_journal(
        mission_dir,
        kind="cancel",
        journal_event={
            "event": "mission_cancel_requested",
            "reason": clean_reason,
        },
        command_payload={
            "reason": clean_reason,
        },
    )
    if mid:
        try:
            _mission_budget._enforce_budget_exhaustion_on_active_jobs(
                str(mid),
                reason=f"mission {mid} cancelled: {clean_reason}",
                mark_exhausted=True,
                terminate_in_flight_owners=True,
            )
        except Exception:  # noqa: BLE001
            pass
    return project_mission_status(mission_dir)


def list_missions(missions_root: str) -> dict[str, Any]:
    """List all missions under ``missions_root``."""
    missions: list[dict[str, Any]] = []
    if os.path.isdir(missions_root):
        for name in sorted(os.listdir(missions_root)):
            candidate = os.path.join(missions_root, name)
            if not os.path.isdir(candidate):
                continue
            if not os.path.exists(os.path.join(candidate, "receipt.json")):
                continue
            try:
                proj = project_mission_status(candidate)
                missions.append({
                    "mission_id": proj["mission_id"],
                    "status": proj["status"],
                    "objective": proj["objective"],
                    "coordinator": proj["coordinator"],
                    "task_count": len(proj.get("tasks") or []),
                    "open_question_count": len(proj.get("open_questions") or []),
                    "updated_at": proj["updated_at"],
                    "freshness_ms": proj["freshness_ms"],
                })
            except Exception:  # noqa: BLE001
                continue
    return {
        "schema": MISSION_SCHEMA,
        "missions_root": missions_root,
        "count": len(missions),
        "missions": missions,
    }


def run_command(args: Any) -> int:
    """Entry point for ``summon mission <action>``."""
    action = getattr(args, "mission_action", None)
    cwd = os.path.abspath(getattr(args, "cwd", None) or os.getcwd())
    missions_root = resolve_missions_root(
        cwd=cwd,
        run_dir=getattr(args, "run_dir", None),
        results_dir=getattr(args, "results_dir", None),
    )

    try:
        if action == "list":
            report = list_missions(missions_root)
            print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2))
            return 0

        if action == "start":
            proj, code = start_mission(args)
            if getattr(args, "mission_digest", False) and not getattr(args, "json", False):
                print(render_mission_digest(proj))
            else:
                print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return code

        mission_id = getattr(args, "mission_id", None)
        if not mission_id:
            raise MissionError(f"mission {action} requires a MISSION_ID", error_kind="missing_mission_id")
        mission_dir = _rundir.run_path(missions_root, mission_id)
        if not os.path.isdir(mission_dir) or not os.path.exists(os.path.join(mission_dir, "receipt.json")):
            raise MissionError(f"mission {mission_id!r} not found under {missions_root!r}", error_kind="mission_not_found")

        if action == "status":
            proj = project_mission_status(mission_dir)
            if getattr(args, "mission_digest", False) and not getattr(args, "json", False):
                print(render_mission_digest(proj))
            else:
                print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return 0

        if action == "resume":
            proj, code = resume_mission(mission_dir, args)
            if getattr(args, "mission_digest", False) and not getattr(args, "json", False):
                print(render_mission_digest(proj))
            else:
                print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return code

        if action == "watch":
            once = bool(getattr(args, "jobs_watch_once", False))
            interval = float(getattr(args, "jobs_watch_interval", 2.0) or 2.0)
            while True:
                proj = project_mission_status(mission_dir)
                if getattr(args, "json", False):
                    print(json.dumps(proj, ensure_ascii=False), flush=True)
                else:
                    print(render_mission_digest(proj), flush=True)
                if once or proj.get("status") in ("completed", "partial", "blocked", "cancelled", "error", "paused_needs_human"):
                    return 0
                time.sleep(interval)

        if action == "reply":
            proj = reply_mission(
                mission_dir,
                question_id=getattr(args, "mission_question_id", None) or "",
                answer=getattr(args, "mission_answer", None) or "",
            )
            print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return 0

        if action == "steer":
            proj = steer_mission(
                mission_dir,
                message=getattr(args, "mission_message", None) or getattr(args, "job_message", None) or "",
                task_id=getattr(args, "mission_task_id", None),
            )
            print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return 0

        if action == "cancel":
            proj = cancel_mission(
                mission_dir,
                reason=getattr(args, "mission_reason", None),
            )
            print(json.dumps(proj, ensure_ascii=False, indent=None if args.json else 2))
            return 0

        raise MissionError(f"unknown mission action {action!r}", error_kind="unknown_action")
    except _rundir.OwnerHeldError as exc:
        err_env = {
            "schema": MISSION_SCHEMA,
            "status": "error",
            "error_kind": "mission_already_running",
            "error": str(exc),
        }
        print(json.dumps(err_env, ensure_ascii=False, indent=None if getattr(args, "json", False) else 2))
        return 1
    except (MissionError, ValueError, OSError) as exc:
        err_kind = getattr(exc, "error_kind", "mission_error")
        err_env = {
            "schema": MISSION_SCHEMA,
            "status": "error",
            "error_kind": err_kind,
            "error": str(exc),
        }
        print(json.dumps(err_env, ensure_ascii=False, indent=None if getattr(args, "json", False) else 2))
        return 1
