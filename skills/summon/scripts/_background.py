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
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import _jobs
from _receipt import scripts_sha256


_INSTALL_MARKER = ".summon-install.json"
_EXECUTION_LOCK = "summon.execution.lock"


def _managed_host_root(entry_path: str) -> str | None:
    """Return the host root for a managed dispatcher, else ``None``."""
    scripts = Path(entry_path).resolve().parent
    skill = scripts.parent
    marker = skill / _INSTALL_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("installed_by") != "summon":
        return None
    return str(skill.parent.parent)


def _release_execution_lease(lease: str, token: str) -> None:
    """Remove only the exact short-lived lease created by this launcher."""
    try:
        with open(lease, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("token") == token:
            os.unlink(lease)
    except (OSError, ValueError):
        pass


def _acquire_execution_lease(entry_path: str) -> tuple[str, str] | None:
    """Interlock an immutable background snapshot with managed install.

    The lease lives beside (not inside) the replaceable skill tree. A launcher
    refuses to race an active installer; an installer similarly refuses while a
    launcher is copying and spawning its bundle. Existing leases are never
    removed here because they may require operator investigation.
    """
    host_root = _managed_host_root(entry_path)
    if host_root is None:
        return None
    install_lock = os.path.join(host_root, "summon.install.lock")
    if os.path.lexists(install_lock):
        raise ValueError("managed Summon install is in progress; retry the background dispatch shortly")
    lease = os.path.join(host_root, _EXECUTION_LOCK)
    token = uuid.uuid4().hex
    try:
        fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("another managed Summon background launch is preparing an immutable bundle; retry shortly") from exc
    except OSError as exc:
        raise ValueError(f"cannot create managed Summon execution lease: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"installed_by": "summon", "purpose": "background_snapshot",
                       "pid": os.getpid(), "token": token}, fh)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        try:
            os.unlink(lease)
        except OSError:
            pass
        raise
    if os.path.lexists(install_lock):
        _release_execution_lease(lease, token)
        raise ValueError("managed Summon install started while preparing this dispatch; retry shortly")
    return lease, token


def _freeze_background_bundle(root: str, job_id: str, entry_path: str,
                              launcher_summon: dict) -> tuple[str, dict]:
    """Copy dispatcher scripts into a durable per-job execution bundle.

    The child executes this copy, not the mutable managed install. Both the
    launch record and terminal receipt therefore name the same script digest
    even if an installer replaces the managed tree while a provider turn runs.
    The bundle is retained under the job root as provenance evidence.
    """
    source_entry = Path(entry_path).resolve()
    source_scripts = source_entry.parent
    bundle_root = Path(tempfile.mkdtemp(prefix=f".summon-bundle-{job_id}-", dir=root))
    bundle_scripts = bundle_root / "scripts"
    try:
        shutil.copytree(source_scripts, bundle_scripts,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        child_entry = bundle_scripts / source_entry.name
        if not child_entry.is_file():
            raise ValueError("immutable background bundle is missing its dispatcher entry script")
        digest = scripts_sha256(str(bundle_scripts))
    except Exception:
        shutil.rmtree(bundle_root, ignore_errors=True)
        raise
    execution = dict(launcher_summon)
    execution.update({
        "script": str(child_entry),
        "scripts_sha256": digest,
        "background_bundle": {
            "kind": "immutable_per_job_snapshot",
            "scripts_sha256": digest,
        },
    })
    return str(child_entry), execution


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
    if getattr(args, "allow_payg", False):
        out += ["--allow-payg"]
    if getattr(args, "allow_text_only", False):
        out += ["--allow-text-only"]
    if getattr(args, "require_tools", False):
        out += ["--require-tools"]
    if getattr(args, "require_exact_model", False):
        out += ["--require-exact-model"]
    if getattr(args, "no_contract_repair", False):
        out += ["--no-contract-repair"]     # honor the opt-out in the detached child
    # ``--read-root`` is repeatable.  Background argv is rebuilt field by field,
    # so omitting it silently narrowed the child to only ``--cwd`` even though
    # the foreground request had already validated and fingerprinted the roots.
    # Prefer the canonical values computed by the parent; the fallback keeps this
    # helper compatible with callers/tests that construct a minimal Namespace.
    _read_roots = getattr(args, "_read_roots_cli", None)
    if _read_roots is None:
        _read_roots = getattr(args, "read_root", None)
    for root in _read_roots or ():
        out += ["--read-root", root]
    if args.agents_dir:
        out += ["--agents-dir", args.agents_dir]
    if getattr(args, "strict_agents_dir", False):
        out += ["--strict-agents-dir"]
    if getattr(args, "enable_roles", False):
        out += ["--enable-roles"]
    if args.timeout:
        out += ["--timeout", str(args.timeout)]
    if getattr(args, "adaptive_timeout", False):
        out += ["--adaptive-timeout"]
    if getattr(args, "max_runtime", None):
        out += ["--max-runtime", str(args.max_runtime)]
    for flag, val in (("--cli", args.cli), ("--model", args.model), ("--effort", args.effort),
                      ("--profile", getattr(args, "profile", None)),
                      ("--resume", args.resume), ("--resume-profile", args.resume_profile),
                      ("--out", args.out), ("--json-schema", args.json_schema),
                      ("--debug-dir", args.debug_dir)):
        if val:
            out += [flag, val]
    if args.retries:
        out += ["--retries", str(args.retries)]
    # The gate MUST survive detachment. This argv is rebuilt field by field, so a
    # flag omitted here is silently dropped -- and dropping --gate-with meant a
    # gated background dispatch ran with no approval at all. The gate runs in the
    # CHILD, which is where the dispatch it authorizes actually happens.
    # Same class of bug as the gate below, and reintroduced with a NEW flag three
    # releases after the gate one was fixed: a control that is not forwarded here is
    # silently absent in the child, so --background --max-permission ran UNCLAMPED.
    if getattr(args, "max_permission", None):
        out += ["--max-permission", args.max_permission]
    if getattr(args, "gate_with", None):
        out += ["--gate-with", args.gate_with]
    if getattr(args, "gate_timeout", None):
        out += ["--gate-timeout", str(args.gate_timeout)]
    if args.worktree is not None:
        out += [f"--worktree={args.worktree}"]  # =form is unambiguous for the bare case
    if getattr(args, "isolated_lane", False):
        out += ["--isolated-lane"]
    if getattr(args, "allow_tool_credentials", False):
        out += ["--allow-tool-credentials"]
    for artifact in getattr(args, "artifacts", ()) or ():
        out += ["--artifact", artifact]
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
    lease = _acquire_execution_lease(entry_path)
    # Launch record BEFORE spawn (fail-closed: a record we cannot write aborts
    # the dispatch rather than launching an untraceable job). Keep the path it
    # returns: every value the handle needs is now computed BEFORE Popen, so
    # nothing fallible runs between a successful spawn and returning the handle.
    try:
        child_entry, execution_summon = _freeze_background_bundle(
            root, job_id, entry_path, summon)
        record_file = _jobs.write_prepared(
            root, job_id, nonce=nonce, agent=args.agent,
            prompt_sha256=prompt_sha, cwd=args.cwd,
            flags=_jobs.flags_projection(args), summon=execution_summon,
            attempt_id=job_id,
            launcher_summon=summon)
    except (OSError, ValueError) as e:
        if lease is not None:
            _release_execution_lease(*lease)
        raise ValueError(f"cannot write the background launch record: {e}") from e
    try:
        cmd = [sys.executable, child_entry, *child_argv(args, result_file)]
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        child_env = {**os.environ, "SUMMON_JOB_NONCE": nonce,
                     "SUMMON_JOB_ID": job_id,
                     "SUMMON_JOB_STARTED_AT": str(time.time()),
                     "SUMMON_JOB_SCRIPTS_SHA256": execution_summon["scripts_sha256"]}
        if getattr(args, "adaptive_timeout", False):
            from _job_control import control_path, heartbeat_path
            child_env["SUMMON_ADAPTIVE_TIMEOUT"] = "1"
            child_env["SUMMON_MAX_RUNTIME_MS"] = str(int(args.max_runtime))
            child_env["SUMMON_JOB_CONTROL_FILE"] = control_path(root, job_id)
            child_env["SUMMON_JOB_HEARTBEAT_FILE"] = heartbeat_path(root, job_id)
        child_env.pop("SUMMON_CMD_LAUNCHER", None)
        if prompt_sha:
            child_env["SUMMON_JOB_PROMPT_SHA"] = prompt_sha   # lets the crash path verify
        kwargs["env"] = child_env
        from _spawn import popen_flags
        proc = subprocess.Popen(cmd, **kwargs, **popen_flags(detached=True))
    finally:
        if lease is not None:
            _release_execution_lease(*lease)
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
    if args.jobs_extend or args.jobs_cancel or args.jobs_steer:
        from _job_control import queue_command
        action = ("extend" if args.jobs_extend else
                  "cancel" if args.jobs_cancel else "steer")
        job_id = args.jobs_extend or args.jobs_cancel or args.jobs_steer
        try:
            if action == "extend" and args.job_duration is None:
                raise ValueError("jobs extend requires --duration")
            if action == "steer" and not args.job_message:
                raise ValueError("jobs steer requires --message")
            report = queue_command(
                root, job_id, action,
                duration_ms=(int(args.job_duration) if action == "extend" else None),
                message=(args.job_message if action == "steer" else None))
        except (OSError, ValueError) as exc:
            emit_error(str(exc)); return 1
        print(json.dumps(report, ensure_ascii=False))
        return 0
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
        private_result_binding = None
        try:
            from _job_continuation import result_binding_sha256
            if st.get("trusted") is True and isinstance(st.get("result"), dict):
                private_result_binding = result_binding_sha256(st["result"])
        except (TypeError, ValueError):
            private_result_binding = None
        try:
            from _job_control import (control_summary, heartbeat_auth,
                                      heartbeat_path, legacy_heartbeat_auth,
                                      public_heartbeat)
            st["control"] = control_summary(root, args.jobs_status)
            heartbeat, _state = _jobs._read(heartbeat_path(root, args.jobs_status))
            record = st.get("record") if isinstance(st.get("record"), dict) else {}
            record_nonce = record.get("nonce")
            auth_ok = False
            if isinstance(heartbeat, dict) and isinstance(record_nonce, str) and record_nonce:
                schema = heartbeat.get("schema")
                auth_value = str(heartbeat.get("auth") or "").encode("utf-8")
                if schema == "summon.job-heartbeat/v2":
                    body = {key: value for key, value in heartbeat.items()
                            if key != "auth"}
                    expected_auth = heartbeat_auth(
                        record_nonce, body).encode("ascii")
                    auth_ok = hmac.compare_digest(auth_value, expected_auth)
                elif schema == "summon.job-heartbeat/v1":
                    expected_auth = legacy_heartbeat_auth(
                        record_nonce, args.jobs_status).encode("ascii")
                    auth_ok = hmac.compare_digest(auth_value, expected_auth)
                # Immutable v1 jobs launched by Summon <=3.2.1 wrote the nonce
                # directly. Accept that legacy shape only for v1 so a v2
                # payload can never bypass its full-body MAC.
                if schema == "summon.job-heartbeat/v1":
                    auth_ok = auth_ok or hmac.compare_digest(
                        str(heartbeat.get("nonce") or "").encode("utf-8"),
                        record_nonce.encode("utf-8"))
            if (not isinstance(heartbeat, dict)
                    or heartbeat.get("job_id") != args.jobs_status
                    or not auth_ok):
                st["heartbeat"] = None
            else:
                st["heartbeat"] = public_heartbeat(
                    heartbeat,
                    "payload_authenticated"
                    if heartbeat.get("schema") == "summon.job-heartbeat/v2"
                    else "legacy_unverified")
            if isinstance(st.get("record"), dict):
                st["record"] = {key: value for key, value in st["record"].items()
                                if key != "nonce"}
            if isinstance(st.get("result"), dict):
                st["result"] = {key: value for key, value in st["result"].items()
                                if key != "job_nonce"}
        except (OSError, TypeError, ValueError):
            st["control"] = {"state": "unavailable"}
            st["heartbeat"] = None
        private_control = st.pop("control", None)
        private_heartbeat = st.pop("heartbeat", None)
        st = _jobs.public_job_status(st)
        # Available continuation is an authenticated capability claim. Never
        # trust the public-shaped object carried by the terminal result itself;
        # derive it from the HMAC-bound private sidecar and current capability
        # registry. Fail closed by omission on any missing/tampered/mismatched
        # source while retaining the completed job status.
        try:
            from _job_continuation import public_projection, read_private_source
            source = read_private_source(root, args.jobs_status)
            if (isinstance(st.get("result"), dict)
                    and private_result_binding is not None
                    and source.get("result_binding_sha256") == private_result_binding):
                st["result"]["continuation"] = public_projection(source)
        except (OSError, TypeError, ValueError):
            pass
        st["control"] = private_control
        st["heartbeat"] = private_heartbeat
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
    if outcome == "stale":
        emit_error(f"background job {args.jobs_wait!r} is stale: its process is no longer "
                   "alive and no verified result was written")
        return 1
    if outcome == "identity_mismatch":
        emit_error(f"background job {args.jobs_wait!r} ended with an execution identity "
                   "mismatch; its result is not trusted")
        return 1
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
