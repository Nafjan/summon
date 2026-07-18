"""Run-directory protocol primitives for durable, resumable fan-out.

The design (three codex adversarial rounds; see the repo plan history):

- ONE OWNER per run: a fenced lock (``owner.lock``) with a lease. Concurrent
  resumes of the same run serialize; a second caller gets a clean "held by pid"
  error, never a silent skip.
- ONE GENERATION per ownership period: every acquisition (clean resume or
  expired-lease takeover) claims ``last_generation + 1`` and namespaces all its
  stage outputs ``g<N>-<stage>.json``. A deposed owner's late child can only
  ever write its OWN generation's names, so cross-generation clobber is
  impossible by construction -- fencing by namespace, no fencing tokens needed.
- ONE WRITER for the journal: only the lock holder appends. Each record is one
  checksummed line; ``started`` is fsynced BEFORE any paid dispatch so a crash
  can never lose the fact that spend may have occurred. A torn tail is
  truncated (and recorded) on the next acquisition; mid-file corruption raises.

Threat model: multiple summon processes on ONE machine sharing a run dir. This
is not a distributed lock manager; clocks are the local machine's. Envelopes +
journal are authoritative; any state index is derived.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# Single WRITER means single PROCESS (the owner); within that process the
# council fans member work across threads, so intra-process appends serialize
# here. Cross-process exclusion is the owner lock's job, not this lock's.
_JOURNAL_LOCK = threading.Lock()

# --- Identifiers ---------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Windows reserved device names: a file named CON / COM1 / "nul.txt" resolves to
# a device, so two distinct accepted ids could collide on the same "directory".
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

OWNER_LOCK = "owner.lock"
GENERATION_FILE = "generation.txt"
JOURNAL_FILE = "journal.jsonl"


def validate_run_id(run_id: str) -> str:
    """Validate a run/job id BEFORE any filesystem access. Raises ValueError."""
    if not run_id or not _ID_RE.match(run_id) or ".." in run_id:
        raise ValueError(f"invalid run id: {run_id!r} (letters/digits/._-, max 64, no '..')")
    if run_id[-1] in ".":
        # A trailing dot is silently stripped by Win32 path resolution, so
        # "abc" and "abc." would alias the same directory.
        raise ValueError(f"invalid run id: {run_id!r} (trailing dot)")
    base = run_id.split(".", 1)[0].upper()
    if base in _WIN_RESERVED:
        raise ValueError(f"invalid run id: {run_id!r} (Windows reserved device name)")
    return run_id


def run_path(runs_root: str, run_id: str) -> str:
    """Containment-checked absolute path of a run dir under ``runs_root``."""
    validate_run_id(run_id)
    root = Path(runs_root).resolve()
    p = (root / run_id).resolve()
    if not p.is_relative_to(root):  # defense in depth; the regex already blocks separators
        raise ValueError(f"run id escapes the runs root: {run_id!r}")
    return str(p)


def new_run_id(mode: str) -> str:
    """`<mode>-<utcstamp>-<rand4>`; stable, sortable, collision-safe enough."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{mode}-{stamp}-{uuid.uuid4().hex[:4]}"


def stage_path(run_dir: str, generation: int, stage: str) -> str:
    """`<run_dir>/g<generation>-<stage>.json` (the fencing namespace)."""
    if not _STAGE_RE.match(stage):
        raise ValueError(f"invalid stage name: {stage!r}")
    return os.path.join(run_dir, f"g{int(generation)}-{stage}.json")


# --- Small shared IO -----------------------------------------------------------

def atomic_write_json(path: str, obj: dict) -> None:
    """mkstemp + replace in the target's directory. Raises OSError on failure."""
    import tempfile
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".summon-run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> dict | None:
    """Parsed dict, or None when missing/unparseable/not-a-dict."""
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def content_sha256(obj) -> str:
    """Canonical hash of a JSON-able value (sorted keys, tight separators)."""
    ser = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode("utf-8")).hexdigest()


# --- Owner lock ----------------------------------------------------------------

class OwnerHeldError(Exception):
    """The run is owned by a live lease."""

    def __init__(self, pid, lease_expires):
        self.pid, self.lease_expires = pid, lease_expires
        super().__init__(
            f"run is owned by pid {pid} (lease expires at {lease_expires}); "
            "retry after expiry, or investigate that process")


class OwnerLockForeignError(Exception):
    """owner.lock exists but is not a valid summon owner record. Fail closed:
    a malformed or foreign file is never auto-broken."""


class OwnershipLostError(Exception):
    """The lock no longer carries our nonce (a successor took over)."""


class Owner:
    """A held run ownership: the lock's identity plus this period's generation.

    ``payload`` is the EXACT bytes we wrote to owner.lock; the lock is
    immutable for its whole ownership period (renewals go to a nonce-named
    SIDECAR), so byte-equality is the ownership test for every fenced
    operation -- a successor's lock can never be byte-identical (fresh nonce)."""

    __slots__ = ("run_dir", "nonce", "generation", "lease_sec", "pid", "payload")

    def __init__(self, run_dir: str, nonce: str, generation: int,
                 lease_sec: float, payload: bytes):
        self.run_dir, self.nonce, self.generation = run_dir, nonce, generation
        self.lease_sec, self.pid, self.payload = lease_sec, os.getpid(), payload


def _lock_path(run_dir: str) -> str:
    return os.path.join(run_dir, OWNER_LOCK)


def _lease_path(run_dir: str, nonce: str) -> str:
    """Renewals live in a NONCE-NAMED sidecar: an owner can only ever write its
    own lease file, so renewal physically cannot clobber a successor's lock or
    lease (the same fencing-by-namespace trick as generation-named stages)."""
    return os.path.join(run_dir, f"lease-{nonce}.json")


def _effective_expiry(run_dir: str, lock_data: dict) -> float:
    """The lock's lease, extended by its owner's lease sidecar when valid."""
    exp = float(lock_data["lease_expires"])
    side = read_json(_lease_path(run_dir, lock_data["nonce"]))
    if (isinstance(side, dict) and side.get("summon_owner_lease") is True
            and side.get("nonce") == lock_data["nonce"]):
        s = side.get("lease_expires")
        if isinstance(s, (int, float)) and not isinstance(s, bool) and s == s:
            exp = max(exp, float(s))
    return exp


def owner_still_current(owner: Owner) -> bool:
    """Byte-exact ownership test against the immutable lock file."""
    try:
        with open(_lock_path(owner.run_dir), "rb") as fh:
            return fh.read() == owner.payload
    except OSError:
        return False


def _parse_owner(raw: bytes):
    """A dict ONLY for a strictly valid summon owner record, else None.
    Strict on purpose: nonce must be hex, generation an int, timestamps finite
    -- anything else is foreign and must never be stale-broken."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("summon_owner") is not True:
        return None
    nonce, gen = data.get("nonce"), data.get("generation")
    acq, exp = data.get("acquired_at"), data.get("lease_expires")
    if not (isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{32}", nonce)):
        return None
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        return None
    for t in (acq, exp):
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not (t == t and abs(t) < 1e12):
            return None
    return data


def read_owner(run_dir: str):
    """The current valid owner record, or None (missing or foreign)."""
    try:
        with open(_lock_path(run_dir), "rb") as fh:
            return _parse_owner(fh.read())
    except OSError:
        return None


def _last_generation(run_dir: str) -> int:
    """Highest generation ever claimed. Consults ALL evidence and takes the max:
    generation.txt, the g<N>-* filename scan, AND a parseable owner.lock's own
    generation (a crash between lock creation and any other write must never
    let a successor reuse the same generation)."""
    best = 0
    try:
        with open(os.path.join(run_dir, GENERATION_FILE), encoding="utf-8") as fh:
            n = int(fh.read().strip())
            if n > best:
                best = n
    except (OSError, ValueError):
        pass
    try:
        for name in os.listdir(run_dir):
            m = re.match(r"^g(\d+)-", name)
            if m:
                best = max(best, int(m.group(1)))
    except OSError:
        pass
    data = read_owner(run_dir)
    if data:
        best = max(best, data["generation"])
    return best


def _write_generation(run_dir: str, generation: int) -> None:
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=".summon-gen-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(generation))
    os.replace(tmp, os.path.join(run_dir, GENERATION_FILE))


def acquire_owner(run_dir: str, lease_sec: float) -> Owner:
    """Become the run's single owner at a fresh generation.

    Crash-safe generation claim: the lock itself CARRIES its generation and
    _last_generation reads it, so a crash between lock creation and the
    generation.txt write cannot let a successor reuse a generation (and a
    failed acquisition attempt never inflates the persisted counter). The lock
    file is IMMUTABLE for its whole ownership period (renewals go to the
    nonce-named lease sidecar), which is what makes byte-equality a sound
    ownership test.

    Raises OwnerHeldError (live lease), OwnerLockForeignError (fail-closed on a
    non-summon lock), or OSError (filesystem).
    """
    os.makedirs(run_dir, exist_ok=True)
    lock = _lock_path(run_dir)
    for _ in range(3):
        generation = _last_generation(run_dir) + 1
        nonce = uuid.uuid4().hex
        now = time.time()
        payload = json.dumps({
            "summon_owner": True, "nonce": nonce, "pid": os.getpid(),
            "generation": generation, "acquired_at": now,
            "lease_expires": now + lease_sec,
        }, ensure_ascii=False).encode("utf-8")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            # AFTER the lock: a crash before this write is covered by the
            # lock-aware _last_generation (the lock names its generation).
            _write_generation(run_dir, generation)
            return Owner(run_dir, nonce, generation, lease_sec, payload)
        except FileExistsError:
            pass
        # Somebody holds (or held) it. Judge the existing lock.
        try:
            with open(lock, "rb") as fh:
                raw = fh.read()
            observed_mtime = os.path.getmtime(lock)
        except OSError:
            continue  # vanished under us -> retry the O_EXCL create
        data = _parse_owner(raw)
        if data is None:
            raise OwnerLockForeignError(
                f"{lock} exists but is not a summon owner record; refusing to break it. "
                "Remove it manually if you are sure no owner is alive.")
        expiry = _effective_expiry(run_dir, data)   # incl. the lease sidecar
        if time.time() < expiry:
            raise OwnerHeldError(data.get("pid"), expiry)
        # Expired: break it, but only if BOTH content and mtime are unchanged
        # since we judged it (never delete a lock somebody just replaced).
        try:
            with open(lock, "rb") as fh:
                raw2 = fh.read()
            if raw2 == raw and os.path.getmtime(lock) == observed_mtime:
                # Persist the broken lock's generation BEFORE unlinking, so the
                # next loop iteration's _last_generation (which can no longer
                # read the deleted lock) still advances past it -- a stale lock
                # broken at generation N must never let us re-claim <= N.
                try:
                    _write_generation(run_dir, max(_last_generation(run_dir),
                                                   data["generation"]))
                except OSError:
                    pass
                os.unlink(lock)
                try:  # the deposed owner's lease sidecar is dead weight now
                    os.unlink(_lease_path(run_dir, data["nonce"]))
                except OSError:
                    pass
        except OSError:
            pass
        # loop: retry the O_EXCL create at a re-read generation
    raise OwnerHeldError(None, None)


def renew_owner(owner: Owner) -> None:
    """Extend the lease by writing OUR nonce-named lease sidecar. Called after
    every completed stage, so a live council renews naturally and only a truly
    suspended owner ever expires.

    Structurally race-free against a successor: this never touches owner.lock
    (immutable) and the sidecar name embeds our nonce, so there is no shared
    file a successor and a deposed owner could both write. Raises
    OwnershipLostError when the lock no longer carries our exact bytes
    (before AND after the write, so a mid-renew takeover is also caught)."""
    if not owner_still_current(owner):
        raise OwnershipLostError("owner.lock no longer carries our bytes")
    atomic_write_json(_lease_path(owner.run_dir, owner.nonce), {
        "summon_owner_lease": True, "nonce": owner.nonce,
        "lease_expires": time.time() + owner.lease_sec,
    })
    if not owner_still_current(owner):
        # A successor appeared mid-renew (our lease must have been expired).
        # Our orphan sidecar is harmless: it is keyed to OUR nonce, which no
        # longer matches the lock, so _effective_expiry ignores it.
        raise OwnershipLostError("ownership changed during renewal")


def release_owner(owner: Owner) -> None:
    """Remove OUR lock only: byte-exact compare, then unlink. A successor's
    lock (fresh nonce, different bytes) is never touched; releasing twice is a
    no-op. The residual compare-then-unlink window is closed in practice by
    the lease discipline: a successor can only exist if our lease expired, and
    a releasing owner is by definition still live."""
    lock = _lock_path(owner.run_dir)
    try:
        with open(lock, "rb") as fh:
            if fh.read() != owner.payload:
                return
        os.unlink(lock)
    except OSError:
        pass
    try:
        os.unlink(_lease_path(owner.run_dir, owner.nonce))
    except OSError:
        pass


def default_lease_sec(stage_timeout_sec: float) -> float:
    """Lease sized so one stuck stage cannot expire a live owner: renewed after
    every stage, so it only needs to outlive ONE stage plus margin."""
    return max(2 * stage_timeout_sec + 120.0, 600.0)


# --- Journal -------------------------------------------------------------------

class JournalCorruptError(Exception):
    """A NON-final journal line failed its checksum: real corruption, never
    auto-repaired (the torn-tail rule covers only the final line)."""


def _journal_line(record: dict) -> str:
    ser = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(ser.encode("utf-8")).hexdigest()
    return json.dumps({**record, "sha256": digest}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def journal_append(run_dir: str, record: dict, owner: Owner | None = None) -> None:
    """Append one checksummed line and FSYNC it. When ``owner`` is given the
    write is FENCED: the lock must still carry the owner's exact bytes, or
    OwnershipLostError raises and nothing is written -- a suspended parent that
    resumes after a takeover cannot corrupt the successor's single-writer
    journal. ``ts`` is stamped here. fsync BEFORE returning is the durability
    contract: an ``attempt started`` record must hit disk before the paid
    dispatch it announces."""
    rec = {**record, "ts": time.time()}
    line = _journal_line(rec)
    path = os.path.join(run_dir, JOURNAL_FILE)
    with _JOURNAL_LOCK:
        if owner is not None and not owner_still_current(owner):
            raise OwnershipLostError("journal write refused: ownership changed")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def journal_read(run_dir: str):
    """``(records, torn_tail)``. Verifies every line's checksum. A final line
    that is torn or checksum-invalid sets ``torn_tail=True`` (the acquisition
    path repairs it); an earlier invalid line raises JournalCorruptError."""
    path = os.path.join(run_dir, JOURNAL_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return [], False
    if lines and lines[-1] == "":
        lines.pop()
    records: list = []
    for i, line in enumerate(lines):
        ok = False
        try:
            data = json.loads(line)
            if isinstance(data, dict) and "sha256" in data:
                claimed = data.pop("sha256")
                ser = json.dumps(data, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False)
                ok = hashlib.sha256(ser.encode("utf-8")).hexdigest() == claimed
        except ValueError:
            ok = False
        if ok:
            records.append(data)
        elif i == len(lines) - 1:
            return records, True   # torn tail: repairable
        else:
            raise JournalCorruptError(
                f"journal line {i + 1} failed its checksum (mid-file corruption)")
    return records, False


def journal_repair(run_dir: str, owner: Owner) -> bool:
    """Owner-only: truncate a torn final line and record the repair. Returns
    whether a repair happened. Never called without holding the lock; status
    reads NEVER repair."""
    records, torn = journal_read(run_dir)
    if not torn:
        return False
    path = os.path.join(run_dir, JOURNAL_FILE)
    good = "".join(_journal_line(r) + "\n" for r in records)
    # Rewrite atomically: the journal is small relative to envelopes, and this
    # runs once, at acquisition, under the lock.
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=".summon-journal-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(good)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    journal_append(run_dir, {"event": "journal_repaired",
                             "generation": owner.generation}, owner=owner)
    return True


# --- Carry-forward -------------------------------------------------------------

def carry_forward(run_dir: str, owner: Owner, stage: str, prior_generation: int,
                  expected_input_sha: str | None) -> bool:
    """Reuse a validated prior-generation stage in the current generation.

    Reads the prior envelope, requires status success and (when the stage has
    upstream inputs) a matching recorded ``input_sha256``, writes a copy under
    the CURRENT generation name with ``carried_from_generation`` added, then
    re-reads the destination as post-copy validation before journaling. No hard
    links anywhere (Windows/non-NTFS determinism). Returns False on any
    mismatch or IO failure -- the caller re-runs the stage instead."""
    src = stage_path(run_dir, prior_generation, stage)
    env = read_json(src)
    if not env or env.get("status") != "success":
        return False
    if expected_input_sha is not None and env.get("input_sha256") != expected_input_sha:
        return False
    dst = stage_path(run_dir, owner.generation, stage)
    copied = {**env, "carried_from_generation": prior_generation}
    try:
        atomic_write_json(dst, copied)
    except OSError:
        return False
    check = read_json(dst)  # post-copy validation: parse + hash agreement
    if not check or check.get("status") != "success" or (
            expected_input_sha is not None and check.get("input_sha256") != expected_input_sha):
        # A failed copy must leave NO current-generation residue: the child's
        # --out skip-if-success would otherwise reuse the bad file instead of
        # the promised re-run.
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False
    try:
        journal_append(run_dir, {"event": "carried_forward", "stage": stage,
                                 "generation": owner.generation,
                                 "from_generation": prior_generation}, owner=owner)
    except OwnershipLostError:
        try:
            os.unlink(dst)   # deposed mid-carry: withdraw our copy entirely
        except OSError:
            pass
        raise
    return True
