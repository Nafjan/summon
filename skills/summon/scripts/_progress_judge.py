"""Progress-aware adaptive extension judgement (long-horizon plan, Phase 2).

At each adaptive checkpoint the legacy rule extends a job when the stream showed
"meaningful" activity in the last interval. That rule has two failure shapes:

* a looping agent whose repeated identical tool calls carry fresh provider ids
  keeps looking active and keeps extending up to the hard cap;
* a long silent tool (a build, a test suite) emits nothing and can lose its
  extension even though the agent is legitimately waiting on it.

``ProgressJudge`` combines bounded evidence into one decision:

* stream activity counts unless the shadow signals report an active tool loop;
* a tool that started and has not returned counts as progress for at most
  ``MAX_PENDING_CREDIT_CHECKPOINTS`` intervals (a hung tool cannot live forever);
* a change in the workspace fingerprint (git porcelain plus stat tuples of
  changed paths) counts as progress.

Modes (``SUMMON_PROGRESS_JUDGE``): ``off``, ``shadow`` (default: record the
judgement next to the legacy decision, enforce the legacy one) and ``enforce``.
The judge never extends past the hard deadline; RuntimeControl still owns that.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from typing import Callable, Mapping

SCHEMA = "summon.progress-judgement/v1"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
DEFAULT_PREFETCH_MS = 15_000
MAX_PENDING_CREDIT_CHECKPOINTS = 3
MAX_FINGERPRINT_ENTRIES = 4096
MAX_PORCELAIN_BYTES = 4 * 1024 * 1024
FINGERPRINT_TIMEOUT_S = 5.0
_REASONS = frozenset({
    "workspace_changed", "stream_activity", "stream_activity_discounted_tool_loop",
    "tool_in_flight", "tool_in_flight_too_long", "no_progress_evidence",
})


def mode_from_environment(env: Mapping[str, str] | None = None) -> str:
    value = (env if env is not None else os.environ).get("SUMMON_PROGRESS_JUDGE", "")
    value = value.strip().lower()
    return value if value in MODES else DEFAULT_MODE


def _porcelain_paths(raw: bytes) -> list[str] | None:
    """Paths from ``git status --porcelain=v2 -z`` records (None when malformed)."""
    tokens = raw.split(b"\0")
    paths: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if not token:
            continue
        text = token.decode("utf-8", errors="surrogateescape")
        kind = text[:2]
        try:
            if kind == "1 ":
                paths.append(text.split(" ", 8)[8])
            elif kind == "2 ":
                paths.append(text.split(" ", 9)[9])
                skip_next = True   # original path follows as its own record
            elif kind == "u ":
                paths.append(text.split(" ", 10)[10])
            elif kind in ("? ", "! "):
                paths.append(text[2:])
            elif text.startswith("#"):
                continue
            else:
                return None
        except IndexError:
            return None
    return paths


def workspace_fingerprint(cwd: str | None, *,
                          runner: Callable = subprocess.run) -> str | None:
    """Digest of working-tree change state, or None when it cannot be measured.

    The porcelain alone does not change when an already-modified file is
    modified again, so each listed path also contributes its lstat size and
    mtime. Ignored build output is out of scope by design; in-flight tool
    tracking covers silent builds.
    """
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return None
    common = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
              "stdin": subprocess.DEVNULL, "timeout": FINGERPRINT_TIMEOUT_S, "check": False}
    try:
        # Porcelain paths are relative to the repository root, not to ``cwd``
        # (which may be a subdirectory), so resolve the root first.
        top = runner(["git", "-C", cwd, "rev-parse", "--show-toplevel"], **common)
        top_raw = getattr(top, "stdout", None)
        if getattr(top, "returncode", 1) != 0 or not isinstance(top_raw, bytes):
            return None
        root = top_raw.decode("utf-8", errors="surrogateescape").strip()
        if not root or not os.path.isdir(root):
            return None
        proc = runner(
            ["git", "-C", root, "status", "--porcelain=v2", "-z",
             "--untracked-files=normal", "--ignore-submodules=all"], **common)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    raw = getattr(proc, "stdout", None)
    if getattr(proc, "returncode", 1) != 0 or not isinstance(raw, bytes):
        return None
    if len(raw) > MAX_PORCELAIN_BYTES:
        return None
    try:
        from _worktree_deps import filter_porcelain_v2_raw
        raw = filter_porcelain_v2_raw(raw, root)
    except Exception:  # noqa: BLE001
        pass
    paths = _porcelain_paths(raw)
    if paths is None:
        return None
    digest = hashlib.sha256()
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(hashlib.sha256(raw).digest())
    for rel in paths[:MAX_FINGERPRINT_ENTRIES]:
        try:
            st = os.lstat(os.path.join(root, rel))
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stamp = "missing"
        digest.update(rel.encode("utf-8", errors="surrogateescape") + b"\0"
                      + stamp.encode("ascii") + b"\0")
    digest.update(str(len(paths)).encode("ascii"))
    return digest.hexdigest()


class ProgressJudge:
    """Checkpoint-time progress decision.

    ``judge`` never blocks on git: workspace fingerprints are measured on a
    background thread, started by :meth:`prefetch` shortly before a checkpoint
    is due (and once at construction for the baseline). A checkpoint consumes
    only a measurement that completed since the previous checkpoint; otherwise
    the workspace leg reports ``unmeasured`` instead of waiting.
    """

    def __init__(self, *, checkpoint_ms: int, mode: str = DEFAULT_MODE,
                 workspace_cwd: str | None = None,
                 fingerprint_fn: Callable[[str | None], str | None] = workspace_fingerprint,
                 prefetch_ms: int = DEFAULT_PREFETCH_MS):
        if mode not in MODES or mode == "off":
            raise ValueError("ProgressJudge requires mode 'shadow' or 'enforce'")
        if not isinstance(checkpoint_ms, int) or isinstance(checkpoint_ms, bool) \
                or checkpoint_ms <= 0:
            raise ValueError("checkpoint_ms must be a positive integer")
        self.mode = mode
        self.checkpoint_ms = checkpoint_ms
        self.prefetch_ms = prefetch_ms
        self._cwd = workspace_cwd
        self._fingerprint_fn = fingerprint_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._baseline: str | None = None
        self._baseline_set = False
        self._latest: str | None = None
        self._latest_seq = 0
        self._consumed_seq = 0
        self.checkpoints = 0
        self.disagreements = 0
        self.last: dict | None = None
        self.prefetch()

    def _measure(self) -> str | None:
        try:
            return self._fingerprint_fn(self._cwd)
        except Exception:  # noqa: BLE001 - measurement is advisory evidence
            return None

    def _worker(self) -> None:
        value = self._measure()
        with self._lock:
            if not self._baseline_set:
                self._baseline, self._baseline_set = value, True
            else:
                self._latest = value
                self._latest_seq += 1
            self._thread = None

    def prefetch(self) -> bool:
        """Start one background measurement unless one is running or unconsumed."""
        with self._lock:
            if self._thread is not None:
                return False
            if self._baseline_set and self._latest_seq != self._consumed_seq:
                return False
            thread = threading.Thread(target=self._worker, name="summon-progress-fingerprint",
                                      daemon=True)
            self._thread = thread
        try:
            thread.start()
        except Exception:  # noqa: BLE001 - release the reservation so a later checkpoint retries
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            return False
        return True

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Test/diagnostic helper: wait for an in-flight measurement to finish."""
        with self._lock:
            thread = self._thread
        if thread is not None and thread.ident is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def judge(self, *, stream_active: bool, signals=None) -> tuple[bool, dict]:
        """Return ``(decision_to_apply, record)`` without blocking.

        In shadow mode the applied decision is the legacy ``stream_active``.
        """
        legacy = bool(stream_active)
        reasons: list[str] = []
        loop = False
        pending_ms = None
        if signals is not None:
            try:
                loop = "tool_loop" in signals.active()
                pending_ms = signals.oldest_pending_ms()
            except Exception:  # noqa: BLE001
                loop, pending_ms = False, None
        with self._lock:
            fresh = self._latest_seq != self._consumed_seq
            current = self._latest if fresh else None
            self._consumed_seq = self._latest_seq
            baseline = self._baseline
            if fresh and current is not None:
                self._baseline = current
        if current is not None and baseline is not None:
            workspace = "changed" if current != baseline else "unchanged"
        else:
            workspace = "unmeasured"

        judged = False
        if workspace == "changed":
            judged = True
            reasons.append("workspace_changed")
        if legacy and not loop:
            judged = True
            reasons.append("stream_activity")
        elif legacy and loop:
            reasons.append("stream_activity_discounted_tool_loop")
        if pending_ms is not None and not loop:
            if pending_ms <= MAX_PENDING_CREDIT_CHECKPOINTS * self.checkpoint_ms:
                judged = True
                reasons.append("tool_in_flight")
            else:
                reasons.append("tool_in_flight_too_long")
        if not reasons:
            reasons.append("no_progress_evidence")

        self.checkpoints += 1
        if judged != legacy:
            self.disagreements += 1
        applied = judged if self.mode == "enforce" else legacy
        record = {
            "schema": SCHEMA,
            "mode": self.mode,
            "legacy_active": legacy,
            "judged_active": judged,
            "applied_active": applied,
            "reasons": reasons,
            "workspace": workspace,
            "tool_loop": loop,
            "oldest_pending_tool_ms": pending_ms,
        }
        self.last = record
        return applied, record

    def projection(self) -> dict:
        return {
            "schema": SCHEMA,
            "mode": self.mode,
            "checkpoints": self.checkpoints,
            "disagreements": self.disagreements,
            "last": dict(self.last) if self.last else None,
        }


def sanitize_record(value: object) -> dict | None:
    """Strict allowlist copy for public heartbeat projections."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    out: dict = {"schema": SCHEMA}
    if value.get("mode") in MODES:
        out["mode"] = value["mode"]
    for key in ("legacy_active", "judged_active", "applied_active", "tool_loop"):
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    reasons = value.get("reasons")
    if isinstance(reasons, list):
        out["reasons"] = [r for r in reasons if r in _REASONS][:8]
    if value.get("workspace") in {"changed", "unchanged", "unmeasured"}:
        out["workspace"] = value["workspace"]
    pending = value.get("oldest_pending_tool_ms")
    if pending is None or (isinstance(pending, int) and not isinstance(pending, bool)
                           and 0 <= pending <= 2 ** 63 - 1):
        out["oldest_pending_tool_ms"] = pending
    return out
