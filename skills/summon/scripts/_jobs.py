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
    """Metadata path for a validated id, contained under the records dir. The
    id is a strict 32-hex token, so it can never escape the directory; we also
    verify containment defensively."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r} (expected 32 hex chars)")
    rd = Path(_records_dir(root)).resolve()
    p = (rd / f"{job_id}.json").resolve()
    if not p.is_relative_to(rd):
        raise ValueError(f"job id escapes the records dir: {job_id!r}")
    return str(p)


def result_path(root: str, job_id: str) -> str:
    """The result envelope path (kept flat as ``<root>/<id>.json`` so the
    ``--background`` handle's ``result_file`` is unchanged)."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    rt = Path(root).resolve()
    p = (rt / f"{job_id}.json").resolve()
    if not p.is_relative_to(rt):
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


def _atomic_write_json(path: str, obj: dict) -> None:
    """Write via temp + fsync + rename, then fsync the directory entry, so a
    launch record is durable before the paid child spawns."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".summon-job-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":            # durable directory entry (POSIX); no-op on Windows
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


# Argv keys allowed into a launch record (default-DENY). The prompt is stored
# as a hash only; resume ids, profiles, schema/debug paths, and unknown flags
# are omitted so no prompt text or secret is persisted.
_FLAG_ALLOWLIST = ("agent", "cli", "model", "effort", "timeout", "cwd",
                   "agents_dir", "worktree")


def flags_projection(args) -> dict:
    out: dict = {}
    for key in _FLAG_ALLOWLIST:
        val = getattr(args, key, None)
        if val is not None and val != "":
            out[key] = val
    return out


def write_prepared(root: str, job_id: str, *, nonce: str, agent: str,
                   prompt_sha256: str | None, cwd: str, flags: dict,
                   summon: dict) -> str:
    """Reserve + write the launch record BEFORE spawn (O_EXCL against an
    astronomically unlikely id reuse; content written whole). Returns the path."""
    ensure_jobs_dir(root)
    path = record_path(root, job_id)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # reserve the id
    os.close(fd)
    _atomic_write_json(path, {
        "job_id": job_id, "nonce": nonce, "agent": agent,
        "prompt_sha256": prompt_sha256, "cwd": cwd, "flags": flags,
        "summon": summon, "prepared_at": time.time(), "pid": None,
    })
    return path


def update_spawned(root: str, job_id: str, pid: int) -> None:
    rec = read_json(record_path(root, job_id))
    if rec is None:
        return
    rec["pid"] = pid
    rec["spawned_at"] = time.time()
    _atomic_write_json(record_path(root, job_id), rec)


def _classify(rec: dict | None, result: dict | None) -> tuple[str, bool]:
    """(state, trusted). Never trusts a result we cannot authenticate against a
    record's nonce."""
    if rec is None and result is None:
        return "unknown", False
    if result is not None:
        r_nonce = result.get("job_nonce")
        if rec is not None and r_nonce is not None and r_nonce == rec.get("nonce"):
            return result.get("status") or "unknown", True
        # a result with no record (legacy --background / record loss) or a nonce
        # that does not match: surface it, but never as trusted.
        return "unverified", False
    # no result yet
    if rec.get("pid") is None:
        return "prepared", False      # spawn unconfirmed (likely died between phases)
    return "running", False           # pid known; liveness NOT verified (B5)


def job_status(root: str, job_id: str) -> dict | None:
    """Full status for one job, or None if neither a record nor a result exists."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    rec = read_json(record_path(root, job_id))
    rpath = result_path(root, job_id)
    result = read_json(rpath)
    if rec is None and result is None:
        return None
    state, trusted = _classify(rec, result)
    return {
        "job_id": job_id, "state": state, "trusted": trusted,
        "agent": (rec or {}).get("agent"),
        "pid": (rec or {}).get("pid"),
        "prepared_at": (rec or {}).get("prepared_at"),
        "spawned_at": (rec or {}).get("spawned_at"),
        "result_file": rpath if result is not None else None,
        "result_status": (result or {}).get("status"),
        "record": rec, "result": result,
    }


def list_jobs(root: str) -> list[dict]:
    """Summary rows for every job with a record or a result, newest first."""
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
    """Poll (monotonic deadline, bounded sleep) for a TRUSTED result. A stale or
    unverifiable file present at the path is skipped until the current child
    replaces it with a nonce-matching result or the deadline passes. Returns
    ``(result_envelope, "done")`` or ``(None, "timeout")``. Raises ValueError on
    a bad id."""
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
    rec = read_json(record_path(root, job_id))
    rpath = result_path(root, job_id)
    while True:
        result = read_json(rpath)
        if result is not None:
            state, trusted = _classify(rec, result)
            if trusted:
                return result, "done"
            # a nonce we cannot match yet: keep waiting for the child's own write
        if time.monotonic() >= deadline:
            return None, "timeout"
        time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
        rec = rec or read_json(record_path(root, job_id))   # record may land late
