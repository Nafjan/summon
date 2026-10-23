"""Provenance receipt: the identity summon stamps onto every dispatch envelope.

Split out of run_subagent.py (the entry point) so the hashing/identity logic
lives on its own. The entry script injects its OWN path and version into
``receipt_base`` (via the zero-arg ``_receipt_base`` wrapper there), so the
receipt's ``script``/``version`` always name run_subagent.py, never THIS module
(a sibling in the same scripts dir).
"""

from __future__ import annotations

import argparse
import hashlib
import ntpath
import os
import queue
import re
import stat as _stat
import subprocess
import threading
import time
from pathlib import Path

from _loader import bundled_roster_dir


def _read_regular_bounded(path, max_bytes):
    """Open ``path`` NON-BLOCKING (O_NONBLOCK on POSIX) so a FIFO/device named ``*.py``
    cannot hang us at ``open()`` (opening a FIFO for read blocks until a writer appears),
    fstat the HANDLE, and return one of:
      - (bytes, None)           a regular file; content read up to ``max_bytes`` (+1 so a
                                file that GREW past the bound on the handle is detectable)
      - (None, 'nonfile')       not a regular file (FIFO/device/dir, incl. behind a symlink)
      - (None, 'oversize:<N>')  a regular file larger than ``max_bytes``
    Raises OSError on any open/read/stat failure; the fd is ALWAYS closed. Windows has no
    O_NONBLOCK and no filesystem FIFOs on a scripts path, so it degrades to a normal open."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return None, "nonfile"
        if st.st_size > max_bytes:
            return None, "oversize:" + str(st.st_size)
        data = b""
        while len(data) <= max_bytes:          # loop: os.read may short-read a regular file
            chunk = os.read(fd, max_bytes + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return data, None
    finally:
        os.close(fd)


def scripts_sha256(scripts_dir, max_bytes=None) -> str:
    """One SHA-256 over every dispatcher runtime asset in ``scripts_dir``.

    This includes all ``*.py`` files except ``test_discovery.py`` and the Windows
    ``summon.cmd`` launcher. The launcher is an execution entry point, so omitting
    it would let a managed install appear converged while its Windows invocation
    path was missing or stale. Length-prefixed framing
    (``len(name)|name|len(data)|data``, names sorted) makes boundaries unambiguous.

    SINGLE source of truth for install identity: the dispatch receipt and the
    install-drift detector both hash the same way, so a hash difference is always a
    REAL code divergence, never an algorithm mismatch. A missing/unreadable module
    hashes as empty content (kept diagnosable, never raises).

    ``max_bytes`` bounds the work per file so hashing a FOREIGN copy (drift enumeration
    of OTHER installs) cannot exhaust memory: a non-regular file (a symlink to a device
    or FIFO) is hashed by a marker instead of read, and a file larger than ``max_bytes``
    is hashed by a name+size marker without reading its content. The dispatch receipt
    passes ``max_bytes=None`` (EXACT legacy behavior, hashing the operator's OWN trusted
    install); every real, small, regular module hashes identically under either, so drift
    detection still matches the receipt for any genuine copy."""
    here = Path(scripts_dir)
    h = hashlib.sha256()
    names = {p.name for p in here.glob("*.py") if p.name != "test_discovery.py"}
    # Always frame the launcher name, including on an older copy where it is
    # absent, so missing-versus-present is provenance-visible.
    names.add("summon.cmd")
    for name in sorted(names):
        target = here / name
        try:
            if max_bytes is None:
                data = target.read_bytes()        # RECEIPT path: EXACT legacy behavior
            else:
                # BOUNDED path (enumerating OTHER copies): non-blocking open + fstat the
                # HANDLE + bounded read from that same handle -- so neither a FIFO/device
                # named *.py (blocking open), nor a path repointed to a huge file between a
                # stat and the read (TOCTOU), nor an unbounded read, can hang or blow memory.
                payload, note = _read_regular_bounded(str(target), max_bytes)
                if note == "nonfile":
                    data = b"\x00__nonfile__"
                elif note is not None:            # "oversize:<N>"
                    data = b"\x00__" + note.encode("ascii")
                elif len(payload) > max_bytes:    # grew past the bound on this handle
                    data = b"\x00__oversize__:racy"
                else:
                    data = payload
        except OSError:
            data = b""
        nb = name.encode("utf-8")
        h.update(len(nb).to_bytes(8, "big"))
        h.update(nb)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def receipt_base(entry_path: str, version: str) -> dict:
    """summon identity: available before ANY validation, so even a missing-agent
    or unknown-backend error names the install that produced it. ``entry_path``
    and ``version`` are injected by the entry script so ``script``/``version``
    name run_subagent.py; ``here`` is that file's directory (this module is a
    sibling, so the scripts dir is the same either way)."""
    here = Path(entry_path).resolve().parent
    return {"summon": {"version": version,
                       "script": str(Path(entry_path).resolve()),
                       "scripts_sha256": scripts_sha256(here)}}


def receipt_agent(args: argparse.Namespace, agent_file: str) -> dict:
    """Agent-definition provenance. ``agents_dir`` records the ABSOLUTE roster
    directory the definition was ACTUALLY loaded from (a bundled-fallback hit
    must not record the project dir that failed the lookup)."""
    try:
        fsha = hashlib.sha256(Path(agent_file).read_bytes()).hexdigest()
    except OSError:
        fsha = None  # hashed as read-back; a vanished file stays diagnosable
    served_dir = str(Path(agent_file).resolve().parent)
    _bundled = bundled_roster_dir()
    if _bundled and Path(served_dir) == Path(_bundled).resolve():
        source = "bundled"
    elif args.agents_dir:
        source = "explicit"
    elif os.environ.get("SUB_AGENTS_DIR"):
        source = "env"
    else:
        source = "project"
    return {"agent_def": {"file": agent_file, "sha256": fsha,
                          "agents_dir": served_dir, "source": source}}


def receipt_prompt(prompt: str | None) -> dict:
    if prompt is None:
        return {}
    return {"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


def git_head(cwd: str) -> str | None:
    """HEAD of the EFFECTIVE dispatch cwd, captured BEFORE the agent runs (input
    provenance -- hence `git_head_before` in the envelope; an editing agent may
    commit during the run). Best-effort: None outside a repo or without git.
    Uses the shared ``subprocess`` module object so a test patching it is seen."""
    try:
        from _spawn import run_flags
        # Git can take longer than the normal lightweight receipt budget on a
        # Windows checkout under Defender/index contention.  A short timeout
        # made the provenance field nondeterministically disappear in the broad
        # discovery suite even though the same command was healthy in isolation.
        # Keep POSIX tight, but give Windows a bounded retry window large enough
        # to preserve the evidence instead of silently returning ``None``.
        git_timeout = 6.0 if os.name == "nt" else 2.0
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=git_timeout,
                           stdin=subprocess.DEVNULL, **run_flags())
        head = (r.stdout or "").strip()
        return head if r.returncode == 0 and head else None
    except (OSError, subprocess.SubprocessError):
        return None


# Mutation evidence is deliberately small and conservative.  It is an audit hint, not a
# filesystem scanner: only Git identity and porcelain names are retained, never contents,
# cwd, or the repository root.
_WORKSPACE_STATUS_MAX_BYTES = 128 * 1024
_WORKSPACE_MAX_PATHS = 2048
# Windows Defender/index locking can make the first status read after a temporary
# commit exceed the POSIX budget. Keep the probe bounded, but give Windows enough
# headroom for a real child commit so release repeatability does not depend on luck.
# Windows process creation and Defender/indexer scans can consume several seconds
# even for a tiny temporary repository. A two-second per-command budget made the
# mutation receipt nondeterministically report ``coverage:unavailable`` on an
# otherwise healthy Git checkout. Keep the bound finite, but leave enough room for
# the four reads that make up one snapshot.
_WORKSPACE_CALL_TIMEOUT_S = 5.0 if os.name == "nt" else 0.75
_WORKSPACE_TOTAL_TIMEOUT_S = 15.0 if os.name == "nt" else 2.0
_HEAD_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _workspace_empty(coverage: str = "unavailable", error: str | None = None) -> dict:
    return {
        "head": None,
        "branch": None,
        "staged": None,
        "unstaged": None,
        "renamed": None,
        "untracked": None,
        "coverage": coverage,
        "error": error,
    }


def _workspace_git_error(kind: str) -> tuple[None, None, str]:
    # Do not include subprocess text: Git errors can echo absolute paths, usernames, or
    # credentials from a hostile environment.  These labels are intentionally bounded.
    return None, None, kind


def _run_git_bounded(argv: list[str], cwd: str, deadline: float) -> tuple[bytes | None, int | None, str | None]:
    """Run one Git read with both a wall-clock bound and a bounded stdout payload."""
    call_deadline = min(deadline, time.monotonic() + _WORKSPACE_CALL_TIMEOUT_S)
    try:
        from _spawn import popen_flags
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, **popen_flags())
    except FileNotFoundError:
        return _workspace_git_error("git unavailable")
    except (OSError, ValueError):
        return _workspace_git_error("git read failed")

    chunks: queue.Queue[bytes] = queue.Queue(maxsize=64)
    done = threading.Event()

    stream_lock = threading.Lock()
    stream_closed = False

    def _close_streams() -> None:
        # A Windows pipe close can block while the reader thread is inside
        # ``read``. Only the reader (after EOF) or a caller that observes it
        # finished may close the handle; never trade a resource warning for a
        # hung dispatch.
        nonlocal stream_closed
        if not done.is_set():
            return
        with stream_lock:
            if stream_closed:
                return
            stream_closed = True
            stream = proc.stdout
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _reader() -> None:
        try:
            while True:
                chunk = proc.stdout.read(8192) if proc.stdout is not None else b""
                if not chunk:
                    break
                # A full queue applies backpressure; the main thread keeps draining it.
                chunks.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            done.set()
            _close_streams()

    threading.Thread(target=_reader, daemon=True).start()
    data = bytearray()
    reason = None
    while not done.is_set() or proc.poll() is None or not chunks.empty():
        if time.monotonic() >= call_deadline or time.monotonic() >= deadline:
            reason = "git timeout"
            break
        try:
            data.extend(chunks.get(timeout=0.025))
        except queue.Empty:
            pass
        if len(data) > _WORKSPACE_STATUS_MAX_BYTES:
            reason = "status payload exceeded bound"
            break
    if reason:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _close_streams()
        return _workspace_git_error(reason)
    try:
        rc = proc.wait(timeout=max(0.001, min(call_deadline, deadline) - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
        _close_streams()
        return _workspace_git_error("git timeout")
    done.wait(timeout=0.2)
    _close_streams()
    return bytes(data), rc, None


def _safe_repo_path(raw: bytes) -> str | None:
    """Decode a raw -z path and accept only a repo-relative, non-escaping name."""
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not name or "\x00" in name:
        return None
    slash_name = name.replace("\\", "/")
    if slash_name.startswith("/") or ntpath.isabs(name) or ntpath.splitdrive(name)[0]:
        return None
    parts = slash_name.split("/")
    if any(part == ".." for part in parts):
        return None
    return "/".join(part for part in parts if part not in ("", ".")) or None


def _parse_git_status_porcelain(payload: bytes) -> dict | None:
    """Parse Git v1 -z output without quote processing or line splitting.

    Returns only relative names.  ``None`` means malformed/hostile output, including a
    rename without its second NUL-delimited name or a payload with too many entries.
    """
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > _WORKSPACE_STATUS_MAX_BYTES:
        return None
    fields = bytes(payload).split(b"\0")
    if fields and fields[-1] != b"":
        return None
    fields = fields[:-1]
    out = {"staged": [], "unstaged": [], "renamed": [], "untracked": []}
    i = 0
    while i < len(fields):
        if len(out["staged"]) + len(out["unstaged"]) + len(out["untracked"]) + len(out["renamed"]) > _WORKSPACE_MAX_PATHS:
            return None
        entry = fields[i]
        if len(entry) < 4 or entry[2:3] != b" ":
            return None
        if entry[0] not in b" MADRCU?!T" or entry[1] not in b" MADRCU?!T":
            return None
        xy = entry[:2].decode("ascii")
        path = _safe_repo_path(entry[3:])
        if path is None:
            return None
        i += 1
        if xy[0] in "RC" or xy[1] in "RC":
            if i >= len(fields):
                return None
            old = _safe_repo_path(fields[i])
            if old is None:
                return None
            out["renamed"].append({"from": old, "to": path})
            i += 1
        if xy == "??":
            out["untracked"].append(path)
        else:
            if xy[0] not in (" ", "?"):
                out["staged"].append(path)
            if xy[1] not in (" ", "?"):
                out["unstaged"].append(path)
    for key in out:
        if key != "renamed":
            out[key] = sorted(set(out[key]))
    out["renamed"] = sorted(out["renamed"], key=lambda x: (x["from"], x["to"]))
    return out


class _WorkspaceSnapshot(dict):
    """Dict subclass carrying non-serialized root and dirty-file digests."""
    _root: str | None = None
    _dirty_digests: dict[str, str] | None = None


def _capture_dirty_digests(root: str, status: dict) -> dict[str, str]:
    """Capture bounded content digests for dirty/untracked files in ``status``."""
    digests: dict[str, str] = {}
    candidates: set[str] = set()
    for bucket in ("staged", "unstaged", "untracked"):
        for p in status.get(bucket) or ():
            if isinstance(p, str) and p:
                candidates.add(p)
    for p in sorted(candidates)[:128]:
        full = os.path.join(root, p)
        try:
            payload, note = _read_regular_bounded(full, 262_144)
            if payload is not None and note is None:
                digests[p] = hashlib.sha256(payload).hexdigest()
            elif note is not None:
                st = os.stat(full)
                digests[p] = f"{note}:{st.st_size}"
        except OSError:
            digests[p] = "missing"
    return digests


def workspace_snapshot(cwd: str | None) -> dict:
    """Capture bounded Git identity/status around the effective dispatch cwd."""
    if not cwd or not isinstance(cwd, str) or not os.path.isdir(cwd):
        return _workspace_empty("unavailable", "workspace unavailable")
    deadline = time.monotonic() + _WORKSPACE_TOTAL_TIMEOUT_S
    root_raw, rc, err = _run_git_bounded(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"], cwd, deadline)
    if err:
        return _workspace_empty("unavailable", err)
    if rc != 0 or root_raw is None:
        return _workspace_empty("unavailable", "not a git repository")
    try:
        root = root_raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return _workspace_empty("incomplete", "invalid git output")
    if (not root or not os.path.isabs(root) or "\x00" in root
            or "\n" in root or "\r" in root):
        return _workspace_empty("incomplete", "invalid git output")

    head_raw, head_rc, head_err = _run_git_bounded(
        ["git", "-C", root, "rev-parse", "--verify", "HEAD"], cwd, deadline)
    branch_raw, branch_rc, branch_err = _run_git_bounded(
        ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], cwd, deadline)
    # An unborn repository has no HEAD yet; that is a valid complete snapshot and lets
    # before=None -> after=<hash> prove the first child commit.
    head = None
    if head_rc == 0 and head_raw is not None:
        try:
            candidate = head_raw.decode("ascii").strip()
        except UnicodeDecodeError:
            return _workspace_empty("incomplete", "invalid git output")
        if not _HEAD_RE.fullmatch(candidate):
            return _workspace_empty("incomplete", "invalid git output")
        head = candidate
    elif head_err or head_rc not in (128, 129):
        return _workspace_empty("incomplete", head_err or "git read failed")
    if branch_rc != 0 or branch_raw is None or branch_err:
        return _workspace_empty("incomplete", branch_err or "git read failed")
    try:
        branch = branch_raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return _workspace_empty("incomplete", "invalid git output")
    if not branch or "\x00" in branch or "\n" in branch or "\r" in branch:
        return _workspace_empty("incomplete", "invalid git output")
    status_raw, status_rc, status_err = _run_git_bounded(
        ["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd, deadline)
    if status_rc != 0 or status_raw is None or status_err:
        return _workspace_empty("incomplete", status_err or "git status failed")
    status = _parse_git_status_porcelain(status_raw)
    if status is None:
        return _workspace_empty("incomplete", "invalid git status output")
    snap = _WorkspaceSnapshot(
        {"head": head, "branch": branch, **status, "coverage": "complete", "error": None}
    )
    snap._root = root
    snap._dirty_digests = _capture_dirty_digests(root, status)
    return snap


_TOUCHED_DIGEST_MAX_FILES = 64
_TOUCHED_DIGEST_MAX_FILE_BYTES = 2 * 1024 * 1024
_TOUCHED_DIGEST_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_TOUCHED_DIGEST_TIMEOUT_S = 2.0


def _compute_files_touched_digests(root: str | None, files_touched: list[str]) -> list[dict]:
    """Compute bounded ``sha256`` and ``sha256_lf`` digests for ``files_touched``."""
    if not root or not isinstance(root, str) or not os.path.isdir(root) or not files_touched:
        return []
    root_real = os.path.realpath(root)
    deadline = time.monotonic() + _TOUCHED_DIGEST_TIMEOUT_S
    total_bytes = 0
    out: list[dict] = []
    for rel in files_touched[:_TOUCHED_DIGEST_MAX_FILES]:
        if time.monotonic() >= deadline or total_bytes >= _TOUCHED_DIGEST_MAX_TOTAL_BYTES:
            break
        if not isinstance(rel, str) or not rel:
            continue
        candidate = os.path.abspath(os.path.normpath(os.path.join(root, rel)))
        try:
            st = os.lstat(candidate)
            if _stat.S_ISLNK(st.st_mode):
                continue
            reparse_attr = getattr(st, "st_file_attributes", 0)
            if reparse_attr & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                continue
            cand_real = os.path.realpath(candidate)
            if os.path.commonpath((root_real, cand_real)) != root_real:
                continue
        except (OSError, ValueError):
            out.append({"path": rel, "status": "deleted", "bytes": None,
                        "sha256": None, "sha256_lf": None})
            continue
        try:
            payload, note = _read_regular_bounded(cand_real, _TOUCHED_DIGEST_MAX_FILE_BYTES)
        except OSError:
            out.append({"path": rel, "status": "deleted", "bytes": None,
                        "sha256": None, "sha256_lf": None})
            continue
        if note == "nonfile":
            continue
        if note is not None or payload is None or len(payload) > _TOUCHED_DIGEST_MAX_FILE_BYTES:
            try:
                sz = os.stat(cand_real).st_size
            except OSError:
                sz = None
            out.append({"path": rel, "status": "oversize", "bytes": sz,
                        "sha256": None, "sha256_lf": None})
            continue
        total_bytes += len(payload)
        sha_raw = hashlib.sha256(payload).hexdigest()
        sha_lf = hashlib.sha256(payload.replace(b"\r\n", b"\n")).hexdigest()
        out.append({
            "path": rel,
            "status": "present",
            "bytes": len(payload),
            "sha256": sha_raw,
            "sha256_lf": sha_lf,
        })
    return out


def partial_worktree_summary(before: dict | None, after: dict | None,
                             ws_evidence: dict | None) -> dict:
    """Summarize partial working-tree progress when a toolful turn times out (exit 124)."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    ws_evidence = ws_evidence if isinstance(ws_evidence, dict) else {}
    touched = list(ws_evidence.get("files_touched") or [])
    digests = list(ws_evidence.get("files_touched_digests") or [])
    staged = list(after.get("staged") or [])
    unstaged = list(after.get("unstaged") or [])
    untracked = list(after.get("untracked") or [])
    child_commit = bool(ws_evidence.get("child_commit"))
    diff_stat = None
    root = (
        getattr(after, "_root", None)
        or after.get("_root")
        or getattr(before, "_root", None)
        or before.get("_root")
    )
    if isinstance(root, str) and root and os.path.isdir(root):
        try:
            stat_raw, stat_rc, stat_err = _run_git_bounded(
                ["git", "--no-optional-locks", "-C", root, "diff", "--stat", "HEAD"],
                root,
                time.monotonic() + min(_WORKSPACE_CALL_TIMEOUT_S, 2.0),
            )
            if stat_rc == 0 and stat_raw and not stat_err:
                diff_stat = stat_raw.decode("utf-8", errors="replace").strip()[:4000] or None
        except Exception:  # noqa: BLE001
            diff_stat = None
    return {
        "timed_out": True,
        "has_partial_work": bool(touched or child_commit or staged or unstaged or untracked),
        "child_commit": child_commit,
        "head_before": before.get("head"),
        "head_after": after.get("head"),
        "files_touched_count": len(touched),
        "files_touched": touched,
        "files_touched_digests": digests,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "diff_stat": diff_stat,
    }


def workspace_evidence(before: dict | None, after: dict | None,
                       permission: str | None = None,
                       cli: str | None = None) -> dict:
    """Compare two snapshots without attributing a dirty baseline to the child."""
    before = before if isinstance(before, dict) else _workspace_empty()
    after = after if isinstance(after, dict) else _workspace_empty()
    b_view = {k: before.get(k) for k in ("head", "branch", "staged", "unstaged", "renamed", "untracked")}
    a_view = {k: after.get(k) for k in ("head", "branch", "staged", "unstaged", "renamed", "untracked")}
    coverage = "complete"
    for snap in (before, after):
        if snap.get("coverage") == "unavailable":
            coverage = "unavailable"
            break
        if snap.get("coverage") != "complete":
            coverage = "incomplete"
    proven = coverage == "complete"
    child_commit = mutation = read_only_violation = None
    attribution = "unavailable"
    files_touched: list[str] = []
    files_touched_digests: list[dict] = []
    if proven:
        child_commit = before.get("head") != after.get("head")
        before_state = tuple(b_view[k] for k in ("branch", "staged", "unstaged", "renamed", "untracked"))
        after_state = tuple(a_view[k] for k in ("branch", "staged", "unstaged", "renamed", "untracked"))
        baseline_dirty = any(before.get(k) for k in ("staged", "unstaged", "renamed", "untracked"))
        b_digests = getattr(before, "_dirty_digests", None) or before.get("_dirty_digests")
        a_digests = getattr(after, "_dirty_digests", None) or after.get("_dirty_digests")
        dirty_rewritten: set[str] = set()
        if isinstance(b_digests, dict) and isinstance(a_digests, dict):
            for p, b_dig in b_digests.items():
                if p not in a_digests or a_digests[p] != b_dig:
                    dirty_rewritten.add(p)
        state_changed = bool(child_commit or before_state != after_state or dirty_rewritten)
        # Git status and bounded dirty-file digests cannot prove zero mutation across an
        # arbitrarily large dirty baseline. Only a new observable state, digest change,
        # or child commit proves mutation; otherwise a dirty baseline remains unknown.
        mutation = (True if state_changed else None) if baseline_dirty else state_changed
        attribution = "ambiguous" if baseline_dirty else "exact"
        if cli in ("openai-compat", "arkcli"):
            # Text-only seats have no filesystem or tool execution surface; any
            # concurrent working-tree change came from outside this child.
            read_only_violation = False
            if state_changed:
                attribution = "external"
        else:
            if permission == "read-only":
                read_only_violation = True if mutation is True else (None if mutation is None else False)
            else:
                read_only_violation = False
            touched_set: set[str] = set(dirty_rewritten)
            for bucket in ("staged", "unstaged", "untracked"):
                b_items = set(before.get(bucket) or ())
                for p in (after.get(bucket) or ()):
                    if isinstance(p, str) and p and p not in b_items:
                        touched_set.add(p)
            b_renames = {
                (r.get("from"), r.get("to")) if isinstance(r, dict) else (str(r), str(r))
                for r in (before.get("renamed") or ())
            }
            for r in (after.get("renamed") or ()):
                key = (r.get("from"), r.get("to")) if isinstance(r, dict) else (str(r), str(r))
                if key not in b_renames:
                    dst = key[1]
                    if isinstance(dst, str) and dst:
                        touched_set.add(dst)
            root = (
                getattr(after, "_root", None)
                or after.get("_root")
                or getattr(before, "_root", None)
                or before.get("_root")
            )
            if child_commit and after.get("head"):
                if isinstance(root, str) and root and os.path.isdir(root):
                    try:
                        if before.get("head"):
                            diff_cmd = [
                                "git", "-C", root, "diff", "--name-only", "-z",
                                str(before["head"]), str(after["head"]),
                            ]
                        else:
                            diff_cmd = [
                                "git", "-C", root, "diff-tree", "--no-commit-id",
                                "--name-only", "-r", "-z", "--root", str(after["head"]),
                            ]
                        diff_raw, diff_rc, diff_err = _run_git_bounded(
                            diff_cmd,
                            root,
                            time.monotonic() + _WORKSPACE_CALL_TIMEOUT_S,
                        )
                        if diff_rc == 0 and diff_raw and not diff_err:
                            for raw_p in diff_raw.split(b"\0"):
                                if not raw_p:
                                    continue
                                safe_p = _safe_repo_path(raw_p)
                                if safe_p:
                                    touched_set.add(safe_p)
                    except Exception:  # noqa: BLE001
                        pass
            files_touched = sorted(touched_set)
            files_touched_digests = _compute_files_touched_digests(root, files_touched)
    return {
        "before": b_view,
        "after": a_view,
        "coverage": coverage,
        "child_commit": child_commit,
        "mutation": mutation,
        "read_only_violation": read_only_violation,
        "attribution": attribution,
        "files_touched": files_touched,
        "files_touched_digests": files_touched_digests,
    }
