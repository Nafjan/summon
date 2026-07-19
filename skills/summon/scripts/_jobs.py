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
directory only you can read there. Liveness verification and reaping are B5.
"""

from __future__ import annotations

import json
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
# as a hash only; resume ids, profiles, schema/debug paths, and unknown flags
# are omitted so no prompt text or secret is persisted.
_FLAG_ALLOWLIST = ("agent", "cli", "model", "effort", "timeout", "cwd",
                   "agents_dir", "worktree")


def flags_projection(args) -> dict:
    out: dict = {}
    for key in _FLAG_ALLOWLIST:
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
                   summon: dict) -> str:
    """Write the launch record BEFORE spawn. The record path never appears as a
    zero-byte file: the whole content is written to a temp file, fsynced, and
    atomically renamed into place (a reader sees either nothing or a complete
    record). The id is a full uuid4, so a pre-existing record means real reuse,
    not a collision to tolerate: refuse it rather than clobber another job."""
    ensure_jobs_dir(root)
    path = record_path(root, job_id)
    if os.path.lexists(path):     # lexists: a symlink here is reuse too, don't follow it
        raise FileExistsError(f"launch record already exists for job {job_id}")
    _atomic_write_json(path, {
        "job_id": job_id, "nonce": nonce, "agent": agent,
        "prompt_sha256": prompt_sha256, "cwd": cwd, "flags": flags,
        "summon": summon, "prepared_at": time.time(), "pid": None,
    })
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


def _classify(rec, rec_state: str, result, res_state: str) -> tuple[str, bool]:
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
            return status, True
        # a result with no record (legacy --background / record loss) or a nonce
        # that does not match: surface it, but never as trusted.
        return "unverified", False
    # no result yet
    if rec.get("pid") is None:
        return "prepared", False      # spawn unconfirmed (likely died between phases)
    return "running", False           # pid known; liveness NOT verified (B5)


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
    state, trusted = _classify(rec, rec_state, result, res_state)
    return {
        "job_id": job_id, "state": state, "trusted": trusted,
        "agent": (rec or {}).get("agent"),
        "pid": (rec or {}).get("pid"),
        "prepared_at": (rec or {}).get("prepared_at"),
        "spawned_at": (rec or {}).get("spawned_at"),
        "result_file": rpath if res_state == _OK else None,
        "result_status": (result or {}).get("status"),
        "record": rec, "result": result,
    }


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
            rows.append({k: st[k] for k in ("job_id", "state", "trusted", "agent",
                                            "pid", "prepared_at", "result_status")})
    rows.sort(key=lambda r: r.get("prepared_at") or 0, reverse=True)
    return rows


def wait_job(root: str, job_id: str, timeout_ms: int, poll_sec: float = 0.5):
    """Poll (monotonic deadline, bounded sleep) for a TRUSTED result. A stale,
    unverifiable, or corrupt file present at the path is skipped until the
    current child replaces it with a nonce-matching result or the deadline
    passes. Returns ``(result_envelope, "done")`` or ``(None, "timeout")``.
    Raises ValueError on a bad id."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
    rec, rec_state = _read(record_path(root, job_id))
    rpath = result_path(root, job_id)
    while True:
        result, res_state = _read(rpath)
        if result is not None:
            _state, trusted = _classify(rec, rec_state, result, res_state)
            if trusted:
                return result, "done"
            # unverifiable/corrupt/nonce-not-yet-matching: keep waiting for the
            # child's own write (deterministic: never returns an untrusted result)
        if time.monotonic() >= deadline:
            return None, "timeout"
        time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
        if rec_state != _OK:                # record may land (or repair) late
            rec, rec_state = _read(record_path(root, job_id))
