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
import copy
import hashlib
import hmac
import json
import os
import shutil
import stat
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


def _freeze_background_prompt(child_entry: str, prompt: str, prompt_sha256: str) -> str:
    """Persist the exact parent-approved prompt beside the immutable script tree."""
    bundle_root = Path(child_entry).resolve().parent.parent
    prompt_path = bundle_root / "dispatch-prompt.txt"
    raw = prompt.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != prompt_sha256:
        raise ValueError("background prompt digest changed before it was frozen")
    fd = os.open(prompt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
        raise
    if hashlib.sha256(prompt_path.read_bytes()).hexdigest() != prompt_sha256:
        raise ValueError("frozen background prompt failed readback verification")
    return str(prompt_path)


def child_argv(args: argparse.Namespace, result_file: str, *, private_resume: bool = False) -> list:
    """Reconstruct the child argv from PARSED args (not by filtering sys.argv,
    which would wrongly drop a token that is another option's *value*). Drops
    --background, adds --job-file."""
    # Fresh background launches use the exact prompt bytes frozen beside their
    # immutable script bundle. Resume successors use their separate authenticated
    # private prompt channel.
    if private_resume:
        # The authenticated child replaces this bounded placeholder from its
        # private digest-bound prompt file.  Neither the continuation handle nor
        # operator steering appears in the detached process command line.
        out = ["--agent", args.agent, "--prompt", "governed continuation",
               "--cwd", args.cwd]
    elif getattr(args, "_background_frozen_prompt_file", None):
        out = ["--agent", args.agent, "--prompt-file",
               args._background_frozen_prompt_file, "--cwd", args.cwd]
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
    if getattr(args, "no_acp_fallback", False):
        out += ["--no-acp-fallback"]
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
                      ("--transport", getattr(args, "transport", None)),
                      ("--resume", None if private_resume else args.resume),
                      ("--resume-profile", args.resume_profile),
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
    if isinstance(getattr(args, "_context_compilation", None), dict):
        out += ["--context-compilation-json", json.dumps(
            args._context_compilation, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)]
    return out + ["--job-file", result_file]


def _load_resume_recovery(root: str, reservation) -> tuple[str, dict, str, dict, str]:
    """Verify and return an immutable successor after a proven pre-spawn failure.

    This path never rebuilds or rewrites the bundle/record/claim. It is eligible
    only when Popen returned no process, provider contact is durably false, and
    every frozen byte still matches the authenticated claim.
    """
    from _job_resume import (claim_path, get_claim, load_child_context,
                             prompt_path)
    job_id = reservation.successor_job_id
    claim = get_claim(reservation)
    phase = claim.get("parent_phase")
    if (phase not in {"spawn_failed", "successor_prepared"}
            or (phase == "spawn_failed" and claim.get("provider_contacted") is not False)
            or (phase == "successor_prepared" and claim.get("provider_contacted") is not None)
            or not isinstance(claim.get("claim_sha256"), str)
            or not isinstance(claim.get("bundle_sha256"), str)):
        raise ValueError("resume successor is not safely recoverable")
    record_file = _jobs.record_path(root, job_id)
    record, state = _jobs._read(record_file)
    if state != _jobs._OK or not isinstance(record, dict):
        raise ValueError("resume recovery launch record is unavailable")
    execution = record.get("summon")
    child_entry = (execution or {}).get("script") if isinstance(execution, dict) else None
    scripts_digest = (execution or {}).get("scripts_sha256") if isinstance(execution, dict) else None
    if (record.get("job_id") != job_id or record.get("attempt_id") != job_id
            or record.get("pid") is not None
            or record.get("prompt_sha256") != reservation.prompt_sha256
            or scripts_digest != claim.get("bundle_sha256")
            or record.get("resume_lineage") != {
                "source_job_id": reservation.source_job_id,
                "request_id": reservation.request_id,
                "claim_id": reservation.claim_id,
                "request_sha256": reservation.request_sha256,
            }
            or not isinstance(child_entry, str)):
        raise ValueError("resume recovery launch record differs from its claim")
    child_entry = os.path.abspath(child_entry)
    root_abs = os.path.abspath(root)
    try:
        if os.path.commonpath((root_abs, child_entry)) != root_abs:
            raise ValueError("resume recovery bundle is outside its job root")
    except ValueError as exc:
        raise ValueError("resume recovery bundle path is invalid") from exc
    info = os.stat(child_entry, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("resume recovery dispatcher is not a regular file")
    if scripts_sha256(os.path.dirname(child_entry)) != scripts_digest:
        raise ValueError("resume recovery bundle digest changed")
    claim_file = claim_path(root, job_id)
    context = load_child_context(_jobs.result_path(root, job_id), claim_file)
    if context is None or context.claim_sha256 != claim.get("claim_sha256"):
        raise ValueError("resume recovery private claim changed")
    prompt_file = prompt_path(root, job_id)
    prompt_info = os.stat(prompt_file, follow_symlinks=False)
    if not stat.S_ISREG(prompt_info.st_mode):
        raise ValueError("resume recovery prompt is not a regular file")
    return child_entry, execution, record_file, {
        "claim_file": claim_file, "claim": claim,
        "claim_sha256": claim["claim_sha256"], "prompt_file": prompt_file,
    }, record["nonce"]


def _load_resume_preparation(root: str, reservation) -> tuple[str, dict, str, dict, str]:
    """Finish a previously interrupted record/claim preparation provider-inertly."""
    from _job_resume import get_claim, prepare_successor
    job_id = reservation.successor_job_id
    claim = get_claim(reservation)
    if (claim.get("parent_phase") != "reserved"
            or claim.get("provider_contacted") is not None
            or claim.get("claim_sha256") is not None
            or claim.get("bundle_sha256") is not None):
        raise ValueError("resume preparation is not safely recoverable")
    record_file = _jobs.record_path(root, job_id)
    record, state = _jobs._read(record_file)
    if state != _jobs._OK or not isinstance(record, dict):
        raise ValueError("resume preparation launch record is unavailable")
    execution = record.get("summon")
    child_entry = (execution or {}).get("script") if isinstance(execution, dict) else None
    scripts_digest = (execution or {}).get("scripts_sha256") if isinstance(execution, dict) else None
    if (record.get("job_id") != job_id or record.get("attempt_id") != job_id
            or record.get("pid") is not None
            or record.get("prompt_sha256") != reservation.prompt_sha256
            or record.get("resume_lineage") != {
                "source_job_id": reservation.source_job_id,
                "request_id": reservation.request_id,
                "claim_id": reservation.claim_id,
                "request_sha256": reservation.request_sha256,
            }
            or not isinstance(child_entry, str)
            or not isinstance(scripts_digest, str)):
        raise ValueError("resume preparation launch record differs from its reservation")
    child_entry = os.path.abspath(child_entry)
    try:
        if os.path.commonpath((os.path.abspath(root), child_entry)) != os.path.abspath(root):
            raise ValueError("resume preparation bundle is outside its job root")
    except ValueError as exc:
        raise ValueError("resume preparation bundle path is invalid") from exc
    info = os.stat(child_entry, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode)
            or scripts_sha256(os.path.dirname(child_entry)) != scripts_digest):
        raise ValueError("resume preparation bundle digest changed")
    prepared = prepare_successor(
        reservation, successor_record=record, bundle_sha256=scripts_digest)
    return child_entry, execution, record_file, prepared, record["nonce"]


def spawn_background(args: argparse.Namespace, entry_path: str, summon: dict, *,
                     resume_reservation=None, resume_recovery: bool = False,
                     resume_prepare_recovery: bool = False) -> dict:
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
    job_id = (resume_reservation.successor_job_id
              if resume_reservation is not None else _jobs.new_job_id())
    result_file = _jobs.result_path(root, job_id)
    nonce = uuid.uuid4().hex
    prompt_sha = (hashlib.sha256(args.prompt.encode("utf-8")).hexdigest()
                  if args.prompt is not None else None)
    if resume_reservation is not None:
        if prompt_sha != resume_reservation.prompt_sha256:
            raise ValueError("resume prompt bytes differ from the authenticated reservation")
        prompt_sha = resume_reservation.prompt_sha256
    lease = _acquire_execution_lease(entry_path)
    # Launch record BEFORE spawn (fail-closed: a record we cannot write aborts
    # the dispatch rather than launching an untraceable job). Keep the path it
    # returns: every value the handle needs is now computed BEFORE Popen, so
    # nothing fallible runs between a successful spawn and returning the handle.
    try:
        if resume_recovery:
            if resume_reservation is None:
                raise ValueError("resume recovery requires an authenticated reservation")
            (child_entry, execution_summon, record_file,
             resume_prepared, nonce) = _load_resume_recovery(
                 root, resume_reservation)
        elif resume_prepare_recovery:
            if resume_reservation is None:
                raise ValueError("resume preparation recovery requires an authenticated reservation")
            (child_entry, execution_summon, record_file,
             resume_prepared, nonce) = _load_resume_preparation(
                 root, resume_reservation)
        else:
            child_entry, execution_summon = _freeze_background_bundle(
                root, job_id, entry_path, summon)
            frozen_prompt = _freeze_background_prompt(
                child_entry, args.prompt, prompt_sha)
            args._background_frozen_prompt_file = frozen_prompt
            execution_summon["background_bundle"]["prompt_sha256"] = prompt_sha
            if isinstance(getattr(args, "_context_compilation", None), dict):
                compilation_bytes = json.dumps(
                    args._context_compilation, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                execution_summon["background_bundle"][
                    "context_compilation_sha256"] = hashlib.sha256(
                        compilation_bytes).hexdigest()
            record_file = _jobs.write_prepared(
                root, job_id, nonce=nonce, agent=args.agent,
                prompt_sha256=prompt_sha, cwd=args.cwd,
                flags=_jobs.flags_projection(args), summon=execution_summon,
                attempt_id=job_id,
                launcher_summon=summon,
                resume_lineage=(
                    {"source_job_id": resume_reservation.source_job_id,
                     "request_id": resume_reservation.request_id,
                     "claim_id": resume_reservation.claim_id,
                     "request_sha256": resume_reservation.request_sha256}
                    if resume_reservation is not None else None))
            resume_prepared = None
            if resume_reservation is not None:
                from _job_resume import prepare_successor
                successor_record = _jobs.read_json(record_file)
                resume_prepared = prepare_successor(
                    resume_reservation, successor_record=successor_record,
                    bundle_sha256=execution_summon["scripts_sha256"])
    except (OSError, ValueError) as e:
        # Preparation has not crossed a Popen boundary. Keep an unprepared claim
        # reserved so the exact same request can retry safely. If an immutable
        # record already exists, the next call verifies and finishes it in place.
        if lease is not None:
            _release_execution_lease(*lease)
        raise ValueError(f"cannot write the background launch record: {e}") from e
    try:
        cmd = [sys.executable, child_entry,
               *child_argv(args, result_file,
                           private_resume=resume_reservation is not None)]
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        child_env = {**os.environ, "SUMMON_JOB_NONCE": nonce,
                     "SUMMON_JOB_ID": job_id,
                     "SUMMON_JOB_STARTED_AT": str(time.time()),
                     "SUMMON_JOB_SCRIPTS_SHA256": execution_summon["scripts_sha256"]}
        if resume_reservation is not None:
            from _job_resume import claim_transition
            child_env["SUMMON_RESUME_CLAIM_FILE"] = resume_prepared["claim_file"]
            child_env["SUMMON_FRESH_CONSENT_ONLY"] = "1"
            # Fresh resume consent is claim-bound. Ambient consent from the
            # launcher cannot silently authorize the successor.
            child_env.pop("SUMMON_ALLOW_CREDIT", None)
            child_env.pop("SUMMON_ALLOW_FABLE", None)
            child_env.pop("SUMMON_ALLOW_BYTEPLUS_PAYG", None)
            child_env.pop("SUMMON_ALLOW_TEXT_ONLY", None)
            if resume_reservation.allow_credit:
                child_env["SUMMON_ALLOW_CREDIT"] = "1"
            recovery_phase = (resume_prepared["claim"].get("parent_phase")
                              if resume_recovery else "successor_prepared")
            claim_transition(
                root, resume_reservation.source_job_id,
                resume_reservation.claim_id, field="parent_phase",
                expected=recovery_phase, target="child_launch_claimed",
                updates=({"provider_contacted": None}
                         if recovery_phase == "spawn_failed" else None))
        if getattr(args, "adaptive_timeout", False):
            from _job_control import control_path, heartbeat_path
            child_env["SUMMON_ADAPTIVE_TIMEOUT"] = "1"
            child_env["SUMMON_MAX_RUNTIME_MS"] = str(int(args.max_runtime))
            child_env["SUMMON_JOB_CONTROL_FILE"] = control_path(root, job_id)
            child_env["SUMMON_JOB_HEARTBEAT_FILE"] = heartbeat_path(root, job_id)
        child_env.pop("SUMMON_CMD_LAUNCHER", None)
        if prompt_sha:
            child_env["SUMMON_JOB_PROMPT_SHA"] = prompt_sha   # lets the crash path verify
        context_sha = (execution_summon.get("background_bundle") or {}).get(
            "context_compilation_sha256")
        if context_sha:
            child_env["SUMMON_JOB_CONTEXT_SHA256"] = context_sha
        kwargs["env"] = child_env
        from _spawn import popen_flags
        try:
            proc = subprocess.Popen(cmd, **kwargs, **popen_flags(detached=True))
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            if resume_reservation is not None:
                try:
                    claim_transition(
                        root, resume_reservation.source_job_id,
                        resume_reservation.claim_id, field="parent_phase",
                        expected="child_launch_claimed", target="indeterminate")
                except Exception:
                    pass
            raise
        except OSError:
            if resume_reservation is not None:
                try:
                    claim_transition(
                        root, resume_reservation.source_job_id,
                        resume_reservation.claim_id, field="parent_phase",
                        expected="child_launch_claimed", target="spawn_failed",
                        updates={"provider_contacted": False})
                except Exception:
                    pass
            raise
        except Exception:
            if resume_reservation is not None:
                try:
                    claim_transition(
                        root, resume_reservation.source_job_id,
                        resume_reservation.claim_id, field="parent_phase",
                        expected="child_launch_claimed", target="indeterminate")
                except Exception:
                    pass
            raise
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
    if resume_reservation is not None:
        from _job_resume import claim_transition, public_projection
        try:
            claim = claim_transition(
                root, resume_reservation.source_job_id,
                resume_reservation.claim_id, field="parent_phase",
                expected="child_launch_claimed", target="spawned",
                updates={"pid": proc.pid})
            handle["resume"] = public_projection(claim)
        except Exception:
            handle.setdefault("warnings", []).append(
                "resume launch metadata update failed; inspect the successor claim before recovery")
    return handle


def _resume_dispatch_args(args, reservation):
    """Build the exact child dispatch from authenticated source authority."""
    child = copy.copy(args)
    source = reservation.source
    child.agent = source["agent"]["requested"]
    child._resolved_agent = source["agent"]["resolved"]
    child.prompt = reservation.prompt
    child.prompt_file = None
    child.cwd = source["workspace"]["path"]
    child.agents_dir = source["agent"]["agents_dir"]
    child.strict_agents_dir = source["authority"]["strict_agents_dir"]
    child.enable_roles = source["authority"]["enable_roles"]
    child.cli = "claude"
    child.transport = "subprocess"
    child.model = source["model"]["targeted"]
    child.require_exact_model = True
    child.effort = source["authority"]["effort"]
    child.profile = source["backend"]["profile"]
    child.resume = source["continuation"]["handle"]
    child.resume_profile = None
    child.max_permission = reservation.effective_permission
    child.read_root = list(source["authority"]["read_roots"])
    child._read_roots_cli = tuple(source["authority"]["read_roots"])
    child.isolated_lane = source["authority"]["isolated_lane"]
    child.allow_tool_credentials = source["authority"]["allow_tool_credentials"]
    child.allow_credit = reservation.allow_credit
    child.allow_payg = reservation.allow_payg
    child.allow_text_only = source["authority"]["allow_text_only"]
    child.require_tools = source["authority"]["require_tools"]
    child.retries = 0
    child.transient_retries = False
    child.retry_nonretryable = False
    child.no_contract_repair = True
    child.no_acp_fallback = True
    child.allow_kimi_acp_fallback = False
    child.json_schema = None
    child.out = None
    child.debug_dir = None
    child.worktree = None
    child.artifacts = []
    child.gate_with = reservation.gate_with
    child.gate_timeout = reservation.gate_timeout_ms
    child.timeout = reservation.timeout_ms
    child.adaptive_timeout = True
    child.hard_timeout = False
    child.max_runtime = reservation.max_runtime_ms
    child.background = True
    child.job_dir = reservation.root
    return child


def run_jobs_query(args, emit_error, *, entry_path: str | None = None,
                   summon: dict | None = None) -> int:
    """`jobs list/status/wait`: read-only registry queries. Returns exit code.
    ``emit_error(message, exit_code=1)`` is the hub's error emitter (injected so
    this module does not import the entry point)."""
    root = _jobs.resolve_jobs_dir(args.job_dir)
    if getattr(args, "jobs_resume", None):
        if not entry_path or not isinstance(summon, dict):
            emit_error("governed resume launcher is unavailable")
            return 1
        try:
            if args.job_message is not None and args.job_message_file is not None:
                raise ValueError("jobs resume accepts --message or --message-file, not both")
            message = args.job_message
            if args.job_message_file is not None:
                message_path = os.path.abspath(args.job_message_file)
                info = os.stat(message_path, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
                    raise ValueError("jobs resume message file has invalid type or size")
                message = Path(message_path).read_text(encoding="utf-8-sig")
                if not message.strip():
                    raise ValueError("jobs resume message file is empty")
            from _job_resume import (ResumeError, claim_transition, get_claim,
                                     public_projection, reconcile_terminal,
                                     reserve_request, successor_launch_lock)
            checkpoint = int(args.timeout)
            maximum = (int(args.max_runtime) if args.max_runtime is not None
                       else max(checkpoint, 24 * 60 * 60 * 1000))
            reservation = reserve_request(
                root, args.jobs_resume, message=message,
                request_id=args.job_request_id,
                max_permission=args.max_permission,
                gate_with=args.gate_with,
                allow_credit=bool(args.allow_credit),
                allow_payg=bool(args.allow_payg),
                timeout_ms=checkpoint,
                gate_timeout_ms=(int(args.gate_timeout)
                                 if args.gate_timeout is not None else checkpoint),
                max_runtime_ms=maximum)
            with successor_launch_lock(root, reservation.successor_job_id):
                existing = get_claim(reservation)
                if existing["parent_phase"] in {
                        "child_launch_claimed", "spawned", "indeterminate"}:
                    # A durable parent ``spawned`` phase proves that a child process
                    # existed. ``child_launch_claimed`` is the adjacent uncertainty
                    # window: Popen may have succeeded before the parent recorded it.
                    # Classify the immutable successor record/result first. A later
                    # authenticated terminal result is allowed to close either state.
                    successor = _jobs.job_status(root, reservation.successor_job_id)
                    if successor is not None and successor.get("trusted") is True:
                        existing = reconcile_terminal(reservation)
                    elif existing["parent_phase"] == "child_launch_claimed":
                        pid = successor.get("pid") if isinstance(successor, dict) else None
                        if (successor is not None
                                and successor.get("state") == "running"
                                and isinstance(pid, int) and not isinstance(pid, bool)):
                            existing = claim_transition(
                                root, reservation.source_job_id,
                                reservation.claim_id, field="parent_phase",
                                expected="child_launch_claimed", target="spawned",
                                updates={"pid": pid})
                        else:
                            existing = claim_transition(
                                root, reservation.source_job_id,
                                reservation.claim_id, field="parent_phase",
                                expected="child_launch_claimed",
                                target="indeterminate")
                    elif (existing["parent_phase"] == "spawned"
                          and (successor is None
                               or successor.get("state") != "running")):
                        existing = claim_transition(
                            root, reservation.source_job_id, reservation.claim_id,
                            field="parent_phase", expected="spawned",
                            target="indeterminate")
                if existing["parent_phase"] not in {
                        "reserved", "spawn_failed", "successor_prepared"}:
                    projection = public_projection(existing)
                    report = {"status": (
                                  "background" if existing["parent_phase"] == "spawned"
                                  else "complete" if (
                                      existing["parent_phase"] == "terminal"
                                      and not projection["recovery_required"])
                                  else "blocked"),
                              "job_id": reservation.successor_job_id,
                              "resume": projection,
                              "provider_contacted": existing.get("provider_contacted")}
                    print(json.dumps(report, ensure_ascii=False))
                    return 0 if report["status"] in {"background", "complete"} else 1
                child = _resume_dispatch_args(args, reservation)
                handle = spawn_background(
                    child, entry_path, summon, resume_reservation=reservation,
                    resume_recovery=existing["parent_phase"] in {
                        "spawn_failed", "successor_prepared"},
                    resume_prepare_recovery=(
                        existing["parent_phase"] == "reserved"
                        and os.path.lexists(_jobs.record_path(
                            root, reservation.successor_job_id))))
                print(json.dumps(handle, ensure_ascii=False))
                return 0
        except (ResumeError, OSError, UnicodeError, ValueError) as exc:
            kind = getattr(exc, "kind", type(exc).__name__)
            emit_error(f"governed resume refused ({kind})")
            return 1
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
