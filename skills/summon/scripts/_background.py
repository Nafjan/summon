"""Background dispatch + read-only jobs registry queries (the run_subagent side).

Split out of the entry point. ``spawn_background`` is handed the ENTRY-SCRIPT
PATH to re-exec (this module's own ``__file__`` would be the wrong target) and
the summon receipt dict; ``run_jobs_query`` is handed an error-emitter callback.
Both are injected by the hub so this module never imports run_subagent back
(which would be a cycle). ``subprocess`` is used via the module object, so a test
patching ``run_subagent.subprocess.Popen`` -- the same cached module -- is seen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid

import _jobs


def child_argv(args: argparse.Namespace, result_file: str) -> list:
    """Reconstruct the child argv from PARSED args (not by filtering sys.argv,
    which would wrongly drop a token that is another option's *value*). Drops
    --background, adds --job-file."""
    # A file-sourced prompt is re-passed AS THE FILE (not the loaded text): the
    # child re-reads it, keeping the detached argv small and mojibake-free.
    if getattr(args, "prompt_file", None):
        out = ["--agent", args.agent, "--prompt-file", args.prompt_file, "--cwd", args.cwd]
    else:
        out = ["--agent", args.agent, "--prompt", args.prompt, "--cwd", args.cwd]
    if getattr(args, "allow_credit", False):
        out += ["--allow-credit"]
    if getattr(args, "no_contract_repair", False):
        out += ["--no-contract-repair"]     # honor the opt-out in the detached child
    if args.agents_dir:
        out += ["--agents-dir", args.agents_dir]
    if args.timeout:
        out += ["--timeout", str(args.timeout)]
    for flag, val in (("--cli", args.cli), ("--model", args.model), ("--effort", args.effort),
                      ("--resume", args.resume), ("--resume-profile", args.resume_profile),
                      ("--out", args.out), ("--json-schema", args.json_schema),
                      ("--debug-dir", args.debug_dir)):
        if val:
            out += [flag, val]
    if args.retries:
        out += ["--retries", str(args.retries)]
    if args.worktree is not None:
        out += [f"--worktree={args.worktree}"]  # =form is unambiguous for the bare case
    return out + ["--job-file", result_file]


def spawn_background(args: argparse.Namespace, entry_path: str, summon: dict) -> dict:
    """Re-exec the dispatcher detached, streaming its result to a job file. Writes
    a durable launch RECORD (fsynced) BEFORE the spawn so a child that dies before
    its result is still traceable, and hands the child a nonce it stamps into its
    result envelope. ``entry_path`` is the dispatcher script to re-exec and
    ``summon`` is the receipt identity dict -- both injected by the hub, so this
    module needs no __file__ or _receipt import. Returns the
    {status, job_id, pid, result_file, job_dir, record_file} handle.

    ``entry_path`` is an EXECUTION-CAPABILITY parameter (it becomes argv[1] of a
    spawned interpreter): it must be a trusted internal path. The only caller
    injects ``os.path.abspath(__file__)`` of the entry script; never route
    user/agent-controlled input here."""
    root = _jobs.resolve_jobs_dir(args.job_dir)
    _jobs.ensure_jobs_dir(root)
    job_id = _jobs.new_job_id()                  # full uuid4 hex
    result_file = _jobs.result_path(root, job_id)
    nonce = uuid.uuid4().hex
    prompt_sha = (hashlib.sha256(args.prompt.encode("utf-8")).hexdigest()
                  if args.prompt is not None else None)
    # Launch record BEFORE spawn (fail-closed: a record we cannot write aborts
    # the dispatch rather than launching an untraceable job). Keep the path it
    # returns: every value the handle needs is now computed BEFORE Popen, so
    # nothing fallible runs between a successful spawn and returning the handle.
    try:
        record_file = _jobs.write_prepared(
            root, job_id, nonce=nonce, agent=args.agent,
            prompt_sha256=prompt_sha, cwd=args.cwd,
            flags=_jobs.flags_projection(args), summon=summon)
    except OSError as e:
        raise ValueError(f"cannot write the background launch record: {e}") from e
    cmd = [sys.executable, entry_path, *child_argv(args, result_file)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    child_env = {**os.environ, "SUMMON_JOB_NONCE": nonce}
    if prompt_sha:
        child_env["SUMMON_JOB_PROMPT_SHA"] = prompt_sha   # lets the crash path verify
    kwargs["env"] = child_env
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    # Handle built from ONLY pre-Popen values (no path recompute, no fs call), so
    # it cannot throw here and strand the live child.
    handle = {"status": "background", "job_id": job_id, "pid": proc.pid,
              "result_file": result_file, "job_dir": root,
              "record_file": record_file}
    # The child is ALREADY running. A failure to stamp the pid onto the record
    # must never sink the handle -- that would strand a live job with no way for
    # the caller to find its result. Surface the metadata failure as a warning
    # and return the handle regardless.
    try:
        _jobs.update_spawned(root, job_id, proc.pid)  # record the pid post-spawn
    except OSError as e:
        handle["warnings"] = [f"launch record pid update failed (job is running): {e}"]
    return handle


def run_jobs_query(args, emit_error) -> int:
    """`jobs list/status/wait`: read-only registry queries. Returns exit code.
    ``emit_error(message, exit_code=1)`` is the hub's error emitter (injected so
    this module does not import the entry point)."""
    root = _jobs.resolve_jobs_dir(args.job_dir)
    if args.jobs_list:
        rows = _jobs.list_jobs(root)
        if args.json:
            print(json.dumps({"job_dir": root, "jobs": rows}, ensure_ascii=False))
        else:
            print(render_jobs(root, rows))
        return 0
    if args.jobs_status:
        try:
            st = _jobs.job_status(root, args.jobs_status)
        except ValueError as e:
            emit_error(str(e)); return 1
        if st is None:
            emit_error(f"no such job {args.jobs_status!r} under {root}"); return 1
        print(json.dumps(st, ensure_ascii=False))
        return 0
    # jobs wait
    try:
        result, outcome = _jobs.wait_job(root, args.jobs_wait, args.timeout)
    except ValueError as e:
        emit_error(str(e)); return 1
    if outcome == "timeout":
        emit_error(f"timed out waiting for job {args.jobs_wait!r} (no verified result yet)",
                   exit_code=124)
        return 124
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "success" else 1


def render_jobs(root: str, rows: list) -> str:
    """ASCII table of jobs. Windows consoles default to cp1252 -> ASCII only."""
    lines = [f"job dir: {root}", f"jobs: {len(rows)}"]
    for r in rows:
        rid = r["job_id"][:12]
        trust = "" if r.get("trusted") else "  [unverified]" if r["state"] == "unverified" else ""
        pid = f" pid={r['pid']}" if r.get("pid") else ""
        lines.append(f"  {rid}  {r['state']:<14} {r.get('agent') or '?':<16}"
                     f"{pid}{trust}")
    return "\n".join(lines)
