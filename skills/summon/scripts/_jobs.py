"""Background-job registry: durable launch records + read-only `jobs` commands.

A `--background` dispatch used to return a handle and write its result to a temp
file. If the child died before writing, the job was zero-forensics, and there
was no way to list, inspect, or wait on jobs. This module adds:

- a launch RECORD written (and fsynced) BEFORE the child spawns, so a job that
  dies pre-result is still traceable to what was launched;
- a best-effort integrity NONCE the child stamps into its result envelope, so a
  result at a job's path can be checked against the job that created it;
- read-only `jobs list / status / wait` over those records.

Threat model (single-user, single-machine): records and results live under a
per-user directory with the OS's default permissions. summon does not defend
against a hostile OTHER local user on a shared host -- point ``--job-dir`` at a
directory only you can read there. Liveness is a process-existence probe, not a
cryptographic process-identity claim: a sufficiently old record whose pid has
been reused can still look alive.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RECORDS = ".summon-records"
_DEFAULT_DIRNAME = "subagents_jobs"   # unchanged default, so existing pollers work


def resolve_jobs_dir(job_dir: str | None) -> str:
    """--job-dir > $SUMMON_JOBS_DIR > <tempdir>/subagents_jobs."""
    return os.path.abspath(job_dir or os.environ.get("SUMMON_JOBS_DIR")
                           or os.path.join(tempfile.gettempdir(), _DEFAULT_DIRNAME))


def new_job_id() -> str:
    return uuid.uuid4().hex


def valid_job_id(job_id: str) -> bool:
    return bool(job_id and _ID_RE.match(job_id))


def _records_dir(root: str) -> str:
    return os.path.join(root, _RECORDS)


def record_path(root: str, job_id: str) -> str:
    """Metadata path for a validated id, contained under the records dir. Only the
    PARENT is resolved (so a symlinked jobs root still works); the leaf is joined
    WITHOUT ``resolve()`` so a symlink planted at ``<records>/<id>.json`` is never
    followed here (``_read`` refuses it). The id is a strict 32-hex token, so it
    cannot contain a separator or ``..``; containment is asserted defensively."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r} (expected 32 hex chars)")
    rd = Path(_records_dir(root)).resolve()
    p = rd / f"{job_id}.json"          # leaf NOT resolved (would follow a symlink)
    if p.parent != rd:
        raise ValueError(f"job id escapes the records dir: {job_id!r}")
    return str(p)


def result_path(root: str, job_id: str) -> str:
    """The result envelope path (kept flat as ``<root>/<id>.json`` so the
    ``--background`` handle's ``result_file`` is unchanged). As with
    ``record_path``, only the parent is resolved; the leaf is not, so a symlink
    planted at the result path is not followed by the path builder."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    rt = Path(root).resolve()
    p = rt / f"{job_id}.json"          # leaf NOT resolved (would follow a symlink)
    if p.parent != rt:
        raise ValueError(f"job id escapes the jobs dir: {job_id!r}")
    return str(p)


def ensure_jobs_dir(root: str) -> None:
    """Create the jobs dir and its records subdir. 0700 on POSIX is basic
    hygiene (owner-only), not a cross-user security guarantee (single-user
    threat model)."""
    os.makedirs(root, exist_ok=True)
    os.makedirs(_records_dir(root), exist_ok=True)
    if os.name != "nt":
        for d in (root, _records_dir(root)):
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


# The two Windows error codes an antivirus/Search-Indexer transiently holding the
# temp or destination file produces: 5 = ERROR_ACCESS_DENIED, 32 = ERROR_SHARING_
# VIOLATION. Any OTHER PermissionError (a real ACL denial with a different code)
# propagates immediately rather than eating the retry budget.
_WIN_TRANSIENT_REPLACE = (5, 32)


def _replace_with_retry(src: str, dst: str, attempts: int = 5) -> None:
    """os.replace, retried only on the transient Windows sharing/access errors
    (WinError 5/32) that antivirus or the Search Indexer briefly holding the file
    produces. The replace is atomic; only the *scheduling* is retried, so a reader
    still sees either the old file or the new one, never a partial. POSIX renames
    don't hit this and succeed on the first pass. A same-code PERMANENT failure is
    delayed by the bounded (<0.5s) budget before it still propagates."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            transient = getattr(e, "winerror", None) in _WIN_TRANSIENT_REPLACE
            if os.name != "nt" or not transient or i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))    # 50/100/150/200ms: brief AV/indexer locks


def _atomic_write_json(path: str, obj: dict) -> None:
    """Write via temp + fsync + rename, then fsync the DIRECTORY entry so the
    rename is durable before the paid child spawns. A directory-fsync failure on
    POSIX is fatal (raised), because the launch record's durability is part of
    the fail-closed traceability contract; on Windows there is no dir fsync and
    the rename's own durability is relied on."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".summon-job-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":            # durable directory entry (POSIX); no-op on Windows
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)          # raises on failure -> write_prepared aborts the launch
        finally:
            os.close(dfd)


# Read states: a MISSING file is not the same as a CORRUPT one. A symlinked leaf
# is refused (never followed), so a record/result cannot be aliased to another
# job's file.
_MISSING, _OK, _CORRUPT = "missing", "ok", "corrupt"


def _read(path: str):
    """``(obj_or_None, state)`` where state is missing/ok/corrupt.

    A symlinked record/result leaf is REFUSED, not followed. On POSIX the refusal
    is atomic: ``O_NOFOLLOW`` makes the ``open`` itself fail on a symlink leaf, so
    there is no islink/open TOCTOU a swap could win. Windows lacks ``O_NOFOLLOW``
    and pure-stdlib ``os.open`` has no open-reparse-point flag, so a best-effort
    ``islink`` pre-check is used there; a deterministic symlink-swap RACE on
    Windows is out of the single-user threat model (and creating a symlink there
    needs elevation). A post-open ``fstat`` also rejects any non-regular file
    (fifo/dir/device) on both platforms."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt":
        try:
            if os.path.islink(path):
                return None, _CORRUPT   # best-effort (no O_NOFOLLOW on Windows)
        except OSError:
            return None, _CORRUPT
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, _MISSING
    except OSError:
        return None, _CORRUPT           # ELOOP on a POSIX symlink leaf, or any open error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, _CORRUPT        # not a regular file (symlink target on Win, fifo, ...)
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read()
    except OSError:
        return None, _CORRUPT
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, _CORRUPT
    return (obj, _OK) if isinstance(obj, dict) else (None, _CORRUPT)


def read_json(path: str):
    """Back-compat convenience: the object or None (missing OR corrupt)."""
    obj, _state = _read(path)
    return obj


# Argv keys allowed into a launch record (default-DENY). The prompt is stored
# as a hash only; resume ids, profile paths, schema/debug paths, and unknown flags
# are omitted so no prompt text or secret is persisted.
_FLAG_ALLOWLIST = ("agent", "cli", "model", "effort", "timeout", "cwd",
                   "agents_dir", "worktree", "profile", "allow_text_only", "require_tools",
                   "strict_agents_dir", "enable_roles", "read_root", "isolated_lane",
                   "allow_tool_credentials", "adaptive_timeout", "hard_timeout",
                   "max_runtime")


def flags_projection(args) -> dict:
    out: dict = {}
    for key in _FLAG_ALLOWLIST:
        if key == "read_root":
            # Keep the launch record auditable without preserving an un-normalized
            # parser value.  The parent has already rejected missing/reparse roots;
            # a minimal unit-test Namespace may only expose ``read_root``.
            val = getattr(args, "_read_roots_cli", None)
            if val is None:
                val = getattr(args, "read_root", None)
            if val:
                out[key] = list(val)
            continue
        val = getattr(args, key, None)
        if key == "worktree":
            # worktree is tri-state: None (not requested), "" (bare = auto-named),
            # or an explicit name. A bare request still matters forensically, so
            # record it as "(auto)" rather than dropping the empty string.
            if val is not None:
                out[key] = val if val != "" else "(auto)"
            continue
        if val is not None and val != "":
            out[key] = val
    return out


def write_prepared(root: str, job_id: str, *, nonce: str, agent: str,
                   prompt_sha256: str | None, cwd: str, flags: dict,
                   summon: dict, attempt_id: str | None = None,
                   launcher_summon: dict | None = None,
                   resume_lineage: dict | None = None) -> str:
    """Write the launch record BEFORE spawn. The record path never appears as a
    zero-byte file: the whole content is written to a temp file, fsynced, and
    atomically renamed into place (a reader sees either nothing or a complete
    record). The id is a full uuid4, so a pre-existing record means real reuse,
    not a collision to tolerate: refuse it rather than clobber another job."""
    ensure_jobs_dir(root)
    path = record_path(root, job_id)
    if os.path.lexists(path):     # lexists: a symlink here is reuse too, don't follow it
        raise FileExistsError(f"launch record already exists for job {job_id}")
    if attempt_id is None:
        attempt_id = job_id
    if not valid_job_id(attempt_id):
        raise ValueError("attempt_id must be a 32-character lowercase hexadecimal token")
    record = {
        "job_id": job_id, "nonce": nonce, "agent": agent,
        # A background job is one physical attempt.  Keep the identity explicit
        # even though the current default equals job_id so future orchestration
        # layers can distinguish logical turns from provider launches.
        "attempt_id": attempt_id,
        "prompt_sha256": prompt_sha256, "cwd": cwd, "flags": flags,
        "summon": summon, "prepared_at": time.time(), "pid": None,
    }
    if launcher_summon is not None:
        # ``summon`` identifies the frozen child bundle. Keep the mutable
        # parent's identity separately so an install-era race stays visible.
        record["launcher_summon"] = launcher_summon
    if resume_lineage is not None:
        expected = {"source_job_id", "request_id", "claim_id", "request_sha256"}
        if (not isinstance(resume_lineage, dict) or set(resume_lineage) != expected
                or not all(valid_job_id(resume_lineage.get(key))
                           for key in ("source_job_id", "request_id", "claim_id"))
                or not isinstance(resume_lineage.get("request_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", resume_lineage["request_sha256"])):
            raise ValueError("resume lineage is invalid")
        record["resume_lineage"] = dict(resume_lineage)
    _atomic_write_json(path, record)
    return path


def update_spawned(root: str, job_id: str, pid: int) -> None:
    """Stamp the pid/spawned_at onto an existing record. A missing or corrupt
    record is left as-is (the caller surfaces the failure); it is never
    recreated, so a lost record cannot masquerade as a fresh launch."""
    path = record_path(root, job_id)
    rec, state = _read(path)
    if state != _OK or rec is None:
        raise FileNotFoundError(f"launch record unreadable for job {job_id} ({state})")
    rec["pid"] = pid
    rec["spawned_at"] = time.time()
    _atomic_write_json(path, rec)


# A record's authenticity turns on a non-empty string nonce; a result's on a
# non-empty string status. Anything else is corrupt, not merely "unverified".
def _valid_nonce(v) -> bool:
    return isinstance(v, str) and bool(v)


def _pid_liveness(pid) -> str:
    """``alive`` / ``dead`` / ``unknown`` from a no-side-effect process probe.

    POSIX ``kill(pid, 0)`` checks existence without sending a signal. Windows
    uses OpenProcess + GetExitCodeProcess so `jobs status` does not shell out to
    tasklist. Access-denied means the process exists but is protected.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "alive"
        except OSError:
            return "unknown"
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit = kernel32.GetExitCodeProcess
        get_exit.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:                         # ERROR_INVALID_PARAMETER: no such pid
                return "dead"
            if err == 5:                          # ERROR_ACCESS_DENIED: protected but alive
                return "alive"
            return "unknown"
        try:
            code = wintypes.DWORD()
            if not get_exit(handle, ctypes.byref(code)):
                return "unknown"
            return "alive" if code.value == 259 else "dead"  # STILL_ACTIVE
        finally:
            close(handle)
    except Exception:  # noqa: BLE001 - a status query must stay fail-soft
        return "unknown"


def _classify(rec, rec_state: str, result, res_state: str,
              pid_liveness: str | None = None) -> tuple[str, bool]:
    """(state, trusted). corrupt is REACHABLE: a malformed record or a malformed
    result file classifies the job corrupt rather than silently reading as
    missing/running. Never trusts a result it cannot authenticate against the
    record's nonce."""
    if rec_state == _CORRUPT or res_state == _CORRUPT:
        return "corrupt", False
    if rec is None and result is None:
        return "unknown", False
    if result is not None:
        r_nonce = result.get("job_nonce")
        rec_nonce = rec.get("nonce") if rec is not None else None
        if _valid_nonce(r_nonce) and _valid_nonce(rec_nonce) and r_nonce == rec_nonce:
            status = result.get("status")
            if not isinstance(status, str) or not status:
                return "corrupt", False      # authenticated but malformed envelope
            expected = rec.get("summon") if isinstance(rec, dict) else None
            actual = result.get("summon") if isinstance(result, dict) else None
            # Immutable-bundle records authenticate executable identity in
            # addition to the result nonce. Legacy records without a digest
            # retain their historic nonce-only classification.
            if isinstance(expected, dict) and expected.get("scripts_sha256"):
                if (not isinstance(actual, dict)
                        or actual.get("scripts_sha256") != expected.get("scripts_sha256")):
                    return "identity_mismatch", False
                bundle = expected.get("background_bundle")
                if isinstance(bundle, dict) and bundle.get("prompt_sha256") is not None:
                    if (not isinstance(rec.get("prompt_sha256"), str)
                            or bundle.get("prompt_sha256") != rec.get("prompt_sha256")
                            or result.get("prompt_sha256") != rec.get("prompt_sha256")):
                        return "identity_mismatch", False
                if isinstance(bundle, dict) and bundle.get(
                        "context_compilation_sha256") is not None:
                    try:
                        context_bytes = json.dumps(
                            result.get("context_compilation"), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"),
                            allow_nan=False).encode("utf-8")
                    except (TypeError, ValueError, UnicodeEncodeError):
                        return "identity_mismatch", False
                    if hashlib.sha256(context_bytes).hexdigest() != bundle.get(
                            "context_compilation_sha256"):
                        return "identity_mismatch", False
            return status, True
        # a result with no record (legacy --background / record loss) or a nonce
        # that does not match: surface it, but never as trusted.
        return "unverified", False
    # no result yet
    if rec.get("pid") is None:
        return "prepared", False      # spawn unconfirmed (likely died between phases)
    if pid_liveness == "alive":
        return "running", False
    if pid_liveness == "dead":
        return "stale", False          # child is gone and never wrote a result
    return "unverified", False         # probe unavailable; never assert "running"


def job_status(root: str, job_id: str) -> dict | None:
    """Full status for one job, or None if neither a record nor a result exists.
    A record or result that exists but is unreadable yields state ``corrupt``."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    rec, rec_state = _read(record_path(root, job_id))
    rpath = result_path(root, job_id)
    result, res_state = _read(rpath)
    if rec_state == _MISSING and res_state == _MISSING:
        return None
    liveness = None
    if rec is not None and result is None and rec.get("pid") is not None:
        liveness = _pid_liveness(rec.get("pid"))
    state, trusted = _classify(rec, rec_state, result, res_state, liveness)
    recorded_attempt = (rec or {}).get("attempt_id")
    if not valid_job_id(recorded_attempt):
        recorded_attempt = job_id if rec_state == _OK else None
    return {
        "job_id": job_id, "state": state, "trusted": trusted,
        "attempt_id": recorded_attempt,
        "agent": (rec or {}).get("agent"),
        "pid": (rec or {}).get("pid"),
        "prepared_at": (rec or {}).get("prepared_at"),
        "spawned_at": (rec or {}).get("spawned_at"),
        "liveness": liveness,
        "result_file": rpath if res_state == _OK else None,
        "result_status": (result or {}).get("status"),
        "record": rec, "result": result,
    }


_PUBLIC_RECORD_FLAGS = {
    "cli", "model", "effort", "timeout", "allow_text_only",
    "require_tools", "strict_agents_dir", "enable_roles", "isolated_lane",
    "allow_tool_credentials", "adaptive_timeout", "hard_timeout", "max_runtime",
}

_SAFE_PUBLIC_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+() -]{0,159}$")
_SAFE_PUBLIC_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_SECRET_MARKERS = re.compile(
    r"(?i)(?:secret|token|password|credential|capability|session|handle|private|"
    r"(?:api|oauth|access|auth)[-_]?key)")
_PUBLIC_STATES = {
    "unknown", "corrupt", "unverified", "identity_mismatch", "prepared",
    "running", "stale", "success", "partial", "blocked", "error", "cancelled",
    "not_run",
}


def _safe_public_token(value, *, slug: bool = False):
    if not isinstance(value, str):
        return None
    pattern = _SAFE_PUBLIC_SLUG if slug else _SAFE_PUBLIC_TOKEN
    candidate = value.strip()
    lowered = candidate.lower()
    if (pattern.fullmatch(candidate) is None
            or candidate.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", candidate)
            or lowered.startswith(("file:", "http:", "https:", "sk-", "ghp_", "akia", "eyj"))
            or _PUBLIC_SECRET_MARKERS.search(candidate)
            or any(part in {".", ".."} for part in candidate.split("/"))):
        return None
    return candidate


def _safe_sha(value):
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _put_typed(out: dict, key: str, value, kind: str) -> None:
    if ((kind == "bool" and isinstance(value, bool))
            or (kind == "int" and isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= 2 ** 63 - 1)
            or (kind == "signed_int" and isinstance(value, int)
                and not isinstance(value, bool) and -(2 ** 31) <= value <= 2 ** 31 - 1)
            or (kind == "number" and isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
            or (kind == "state" and value in _PUBLIC_STATES)):
        out[key] = value

_PUBLIC_RESULT_FIELDS = {
    "status", "execution_status", "attempt_status", "attempts", "attempt_id",
    "agent", "cli", "backend", "provider", "transport", "model",
    "served_model_evidence", "model_match", "model_match_state",
    "named_model_verified", "report_ok", "verdict", "error_kind", "exit_code",
    "raw_backend_exit_code", "normalized_exit_code", "provider_contacted",
    "result_usable", "retryable", "resumed", "prompt_sha256", "request_sha256",
}


def _public_summon(value) -> dict | None:
    """Return executable identity without exposing a local dispatcher path."""
    if not isinstance(value, dict):
        return None
    out = {}
    version = _safe_public_token(value.get("version"))
    scripts = _safe_sha(value.get("scripts_sha256"))
    if version is not None:
        out["version"] = version
    if scripts is not None:
        out["scripts_sha256"] = scripts
    bundle = value.get("background_bundle")
    if isinstance(bundle, dict):
        projected = {}
        kind = _safe_public_token(bundle.get("kind"), slug=True)
        digest = _safe_sha(bundle.get("scripts_sha256"))
        if kind is not None:
            projected["kind"] = kind
        if digest is not None:
            projected["scripts_sha256"] = digest
        if projected:
            out["background_bundle"] = projected
    return out or None


def public_job_status(status: dict) -> dict:
    """Allowlisted local status projection with private capabilities removed.

    Raw job records/results are execution authority: they can contain cwd/read-root
    paths, provider session handles, an OAuth-bearing AGY profile path, argv-derived
    values, and the job nonce.  ``jobs status`` is an inspection surface, not a
    capability-export API, so it exposes bounded evidence only.  Internal resume
    code must read the authenticated private files directly rather than reconstruct
    authority from this projection.
    """
    projected = {}
    job_id = status.get("job_id")
    if valid_job_id(job_id):
        projected["job_id"] = job_id
    _put_typed(projected, "state", status.get("state"), "state")
    _put_typed(projected, "trusted", status.get("trusted"), "bool")
    attempt_id = status.get("attempt_id")
    if valid_job_id(attempt_id):
        projected["attempt_id"] = attempt_id
    agent = _safe_public_token(status.get("agent"), slug=True)
    if agent is not None:
        projected["agent"] = agent
    _put_typed(projected, "pid", status.get("pid"), "int")
    _put_typed(projected, "prepared_at", status.get("prepared_at"), "number")
    _put_typed(projected, "spawned_at", status.get("spawned_at"), "number")
    liveness = status.get("liveness")
    if liveness in {None, "alive", "dead", "unknown"}:
        projected["liveness"] = liveness
    _put_typed(projected, "result_status", status.get("result_status"), "state")
    record = status.get("record")
    if isinstance(record, dict):
        public_record = {}
        for key in ("job_id", "attempt_id"):
            if valid_job_id(record.get(key)):
                public_record[key] = record[key]
        agent = _safe_public_token(record.get("agent"), slug=True)
        if agent is not None:
            public_record["agent"] = agent
        prompt_sha = _safe_sha(record.get("prompt_sha256"))
        if prompt_sha is not None:
            public_record["prompt_sha256"] = prompt_sha
        for key, kind in (("prepared_at", "number"), ("spawned_at", "number"),
                          ("pid", "int")):
            _put_typed(public_record, key, record.get(key), kind)
        flags = record.get("flags")
        if isinstance(flags, dict):
            public_flags = {}
            for key in ("cli", "effort"):
                value = _safe_public_token(flags.get(key), slug=True)
                if value is not None:
                    public_flags[key] = value
            model = _safe_public_token(flags.get("model"))
            if model is not None:
                public_flags["model"] = model
            for key in ("allow_text_only", "require_tools", "strict_agents_dir",
                        "enable_roles", "isolated_lane", "allow_tool_credentials",
                        "adaptive_timeout"):
                _put_typed(public_flags, key, flags.get(key), "bool")
            for key in ("timeout", "hard_timeout", "max_runtime"):
                _put_typed(public_flags, key, flags.get(key), "int")
            if public_flags:
                public_record["flags"] = public_flags
        summon = _public_summon(record.get("summon"))
        if summon:
            public_record["summon"] = summon
        launcher = _public_summon(record.get("launcher_summon"))
        if launcher:
            public_record["launcher_summon"] = launcher
        projected["record"] = public_record
    else:
        projected["record"] = None
    result = status.get("result")
    if isinstance(result, dict):
        public_result = {}
        record_flags = record.get("flags") if isinstance(record, dict) else {}
        if not isinstance(record_flags, dict):
            record_flags = {}
        record_agent = _safe_public_token(
            record.get("agent") if isinstance(record, dict) else None, slug=True)
        record_cli = _safe_public_token(record_flags.get("cli"), slug=True)
        record_model = _safe_public_token(record_flags.get("model"))
        for key in ("status", "execution_status", "attempt_status"):
            _put_typed(public_result, key, result.get(key), "state")
        for key in ("attempts",):
            _put_typed(public_result, key, result.get(key), "int")
        attempt = result.get("attempt_id")
        if valid_job_id(attempt):
            public_result["attempt_id"] = attempt
        if record_agent is not None and result.get("agent") == record_agent:
            public_result["agent"] = record_agent
        for key in ("verdict", "error_kind"):
            value = _safe_public_token(result.get(key), slug=True)
            if value is not None:
                public_result[key] = value
        for key in ("cli", "backend"):
            if record_cli is not None and result.get(key) == record_cli:
                public_result[key] = record_cli
        transport = result.get("transport")
        if transport in {"subprocess", "api", "acp"}:
            public_result["transport"] = transport
        model = result.get("model")
        if isinstance(model, dict) and record_model is not None:
            safe_model = {}
            for key in ("requested", "targeted"):
                value = _safe_public_token(model.get(key))
                if value == record_model:
                    safe_model[key] = value
            served = _safe_public_token(model.get("served"))
            targeted = safe_model.get("targeted") or safe_model.get("requested")
            if served is not None and served == targeted:
                safe_model["served"] = served
            if safe_model:
                public_result["model"] = safe_model
        evidence = result.get("served_model_evidence")
        if evidence in {"reported", "inferred", "absent"}:
            public_result["served_model_evidence"] = evidence
        match = result.get("model_match")
        if match is None or isinstance(match, bool):
            public_result["model_match"] = match
        match_state = result.get("model_match_state")
        if match_state in {"match", "mismatch", "unverified", "not_run"}:
            public_result["model_match_state"] = match_state
        for key in ("named_model_verified", "report_ok", "provider_contacted",
                    "result_usable", "retryable", "resumed"):
            _put_typed(public_result, key, result.get(key), "bool")
        for key in ("exit_code", "raw_backend_exit_code", "normalized_exit_code"):
            _put_typed(public_result, key, result.get(key), "signed_int")
        for key in ("prompt_sha256", "request_sha256"):
            digest = _safe_sha(result.get(key))
            if digest is not None:
                public_result[key] = digest
        summon = _public_summon(result.get("summon"))
        if summon:
            public_result["summon"] = summon
        billing = result.get("billing")
        if isinstance(billing, dict):
            source = billing.get("source")
            if source in {"subscription", "credit", "api", "payg", "free", "unknown"}:
                public_result["billing"] = {"source": source}
        agent_def = result.get("agent_def")
        if isinstance(agent_def, dict):
            safe_agent_def = {}
            digest = _safe_sha(agent_def.get("sha256"))
            if digest is not None:
                safe_agent_def["sha256"] = digest
            if safe_agent_def:
                public_result["agent_def"] = safe_agent_def
        read_policy = result.get("read_allowlist")
        if isinstance(read_policy, dict):
            safe_read = {}
            for key in ("enforced", "would_refuse"):
                _put_typed(safe_read, key, read_policy.get(key), "bool")
            enforcement = read_policy.get("enforcement")
            if enforcement in {"provider_native", "summon_wrapper", "unsupported", "not_run"}:
                safe_read["enforcement"] = enforcement
            error_kind = _safe_public_token(read_policy.get("error_kind"), slug=True)
            if error_kind is not None:
                safe_read["error_kind"] = error_kind
            for source, target in (("requested_paths", "requested_count"),
                                   ("effective_paths", "effective_count")):
                if isinstance(read_policy.get(source), list):
                    safe_read[target] = len(read_policy[source])
            if safe_read:
                public_result["read_allowlist"] = safe_read
        continuation = result.get("continuation")
        continuation_fields = {
            "schema", "available", "resume_state", "resume_reason", "backend",
            "transport", "steering_mode", "live_steering_acknowledged",
        }
        if (isinstance(continuation, dict)
                and set(continuation) == continuation_fields
                and continuation.get("schema") == "summon.job-continuation/v1"
                and isinstance(continuation.get("available"), bool)
                and isinstance(continuation.get("live_steering_acknowledged"), bool)
                and continuation.get("live_steering_acknowledged") is False
                and all(isinstance(continuation.get(key), str)
                        and re.fullmatch(r"[a-z0-9_-]{1,128}", continuation[key])
                        for key in ("resume_state", "resume_reason", "backend",
                                    "transport", "steering_mode"))):
            from _resume_capabilities import resume_capability
            capability = resume_capability(continuation["backend"],
                                           continuation["transport"])
            claims_current_capability = (
                continuation["resume_state"] == capability["resume_state"]
                and continuation["backend"] == capability["backend"]
                and continuation["transport"] == capability["transport"]
                and continuation["steering_mode"] == capability["steering_mode"]
            )
            # A certified backend can still have an ineligible individual result
            # (missing handle/model/contact evidence). Only an available claim
            # requires certification; unavailable is always a legitimate state.
            availability_valid = (
                not continuation["available"]
                or capability["resume_state"] == "certified"
            )
            # ``available:true`` is an authority claim and cannot be trusted from
            # the result envelope alone. The jobs command adds it only after
            # authenticating the private sidecar. Unavailable is non-authority
            # diagnostic state and may be projected directly.
            if (claims_current_capability and availability_valid
                    and continuation["available"] is False):
                public_result["continuation"] = dict(continuation)
        projected["result"] = public_result
    else:
        projected["result"] = None
    return projected


def list_jobs(root: str) -> list[dict]:
    """Summary rows for every job with a record or a result, newest first. A
    corrupt record/result still enumerates (as state ``corrupt``) so a poller
    sees it rather than a silent gap."""
    ids: set = set()
    try:
        for name in os.listdir(_records_dir(root)):
            if name.endswith(".json") and valid_job_id(name[:-5]):
                ids.add(name[:-5])
    except OSError:
        pass
    try:  # result-only (legacy) jobs
        for name in os.listdir(root):
            if name.endswith(".json") and valid_job_id(name[:-5]):
                ids.add(name[:-5])
    except OSError:
        pass
    rows = []
    for jid in ids:
        st = job_status(root, jid)
        if st:
            rows.append({k: st[k] for k in ("job_id", "attempt_id", "state", "trusted", "agent",
                                            "pid", "prepared_at", "result_status",
                                            "liveness")})
    rows.sort(key=lambda r: r.get("prepared_at") or 0, reverse=True)
    return rows


def wait_job(root: str, job_id: str, timeout_ms: int, poll_sec: float = 0.5):
    """Poll (monotonic deadline, bounded sleep) for a TRUSTED result. A stale,
    unverifiable, or corrupt file present at the path is skipped until the
    current child replaces it with a nonce-matching result or the deadline
    passes. Returns ``(result_envelope, "done")`` or ``(None, "timeout")``.
    Returns ``(None, "stale")`` as soon as the recorded process is gone with no
    result, rather than burning the caller's whole wait budget. Raises ValueError
    on a bad id."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
    rec, rec_state = _read(record_path(root, job_id))
    rpath = result_path(root, job_id)
    while True:
        observed_state = None
        result, res_state = _read(rpath)
        if result is not None:
            observed_state, trusted = _classify(rec, rec_state, result, res_state)
            if trusted:
                return result, "done"
            # unverifiable/corrupt/nonce-not-yet-matching: keep waiting for the
            # child's own write (deterministic: never returns an untrusted result)
        if rec is not None and rec.get("pid") is not None \
                and _pid_liveness(rec.get("pid")) == "dead":
            # The child can publish its atomic result and exit between the read
            # above and this liveness probe. Re-read once after observing death
            # so that a verified terminal result wins that unavoidable race.
            result, res_state = _read(rpath)
            if result is not None:
                observed_state, trusted = _classify(rec, rec_state, result, res_state)
                if trusted:
                    return result, "done"
            if observed_state == "identity_mismatch":
                return None, "identity_mismatch"
            return None, "stale"
        if time.monotonic() >= deadline:
            return None, "timeout"
        time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
        if rec_state != _OK:                # record may land (or repair) late
            rec, rec_state = _read(record_path(root, job_id))
