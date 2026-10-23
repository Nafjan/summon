"""Provider-inert shadow progress signals: tool loops and error storms.

This module only *observes*. It never changes a dispatch status, never stops a
process, and never feeds the liveness clocks. Phase 1 of the long-horizon plan
ships it in ``mode: "shadow"`` so thresholds can be calibrated against real runs
before any enforcement is attached.

Signals:

* **Tool loop**: the same tool invoked with the same (bounded) input many times
  in a row. The key is a SHA-256 of a canonical serialization in which every
  string is capped at 4096 characters. Hashing a capped serialization, not
  slicing a prefix, keeps two commands that share a long preamble (an absolute
  path, a ``cd``, an interpreter) distinguishable.
* **Error storm**: consecutive failed tool results with no successful result
  in between.

The public snapshot carries counts only: no tool names, inputs, or digests.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Callable

SCHEMA = "summon.progress-signals/v1"
MAX_STRING_CHARS = 4096
MAX_DEPTH = 32
DEFAULT_REPEAT_LIMIT = 8
DEFAULT_ERROR_LIMIT = 5
_MAX_COUNT = (1 << 63) - 1


def _bounded(value, depth: int = 0):
    if depth >= MAX_DEPTH:
        return "<depth>"
    if isinstance(value, str):
        return value[:MAX_STRING_CHARS]
    if isinstance(value, dict):
        return {str(k)[:MAX_STRING_CHARS]: _bounded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(v, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    return repr(value)[:MAX_STRING_CHARS]


def tool_signature(name: object, payload: object) -> str:
    """Stable, bounded identity of one tool invocation."""
    bounded = [str(name)[:256] if name is not None else None, _bounded(payload)]
    try:
        raw = json.dumps(bounded, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raw = repr(bounded)[:MAX_STRING_CHARS * 4]
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


class ProgressSignals:
    """Counts consecutive identical tool calls and consecutive tool failures."""

    def __init__(self, *, repeat_limit: int = DEFAULT_REPEAT_LIMIT,
                 error_limit: int = DEFAULT_ERROR_LIMIT,
                 clock: Callable[[], float] = time.monotonic,
                 instrumented: bool = True):
        for label, value in (("repeat_limit", repeat_limit), ("error_limit", error_limit)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 2:
                raise ValueError(f"{label} must be an integer >= 2")
        self.repeat_limit = repeat_limit
        self.error_limit = error_limit
        self.instrumented = bool(instrumented)
        self._clock = clock
        self._started = clock()
        self._last_signature: str | None = None
        self._last_distinct_at: float | None = None
        self.tool_calls = 0
        self.distinct_tool_calls = 0
        self.current_repeat = 0
        self.max_repeat = 0
        self.tool_results = 0
        self.failed_results = 0
        self.current_error_streak = 0
        self.max_error_streak = 0
        # call id -> monotonic start; bounded so a lost result cannot grow memory.
        self._pending: dict[str, float] = {}

    MAX_PENDING = 256

    def tool_started(self, call_id: object) -> None:
        """A tool began and has not yet returned (waiting on a tool is not idling)."""
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            return
        if call_id in self._pending:
            return
        if len(self._pending) >= self.MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        self._pending[call_id] = self._clock()

    def tool_finished(self, call_id: object) -> None:
        if isinstance(call_id, str):
            self._pending.pop(call_id, None)

    def oldest_pending_ms(self) -> int | None:
        if not self._pending:
            return None
        oldest = min(self._pending.values())
        return max(0, int((self._clock() - oldest) * 1000))

    @staticmethod
    def _inc(value: int) -> int:
        return value + 1 if value < _MAX_COUNT else value

    def tool_call(self, name: object, payload: object) -> bool:
        """Record one invocation; return True when it differs from the previous one."""
        signature = tool_signature(name, payload)
        self.tool_calls = self._inc(self.tool_calls)
        if signature == self._last_signature:
            self.current_repeat = self._inc(self.current_repeat)
            self.max_repeat = max(self.max_repeat, self.current_repeat)
            return False
        self._last_signature = signature
        self.current_repeat = 1
        self.max_repeat = max(self.max_repeat, 1)
        self.distinct_tool_calls = self._inc(self.distinct_tool_calls)
        self._last_distinct_at = self._clock()
        return True

    def tool_result(self, *, is_error: bool) -> None:
        self.tool_results = self._inc(self.tool_results)
        if is_error:
            self.failed_results = self._inc(self.failed_results)
            self.current_error_streak = self._inc(self.current_error_streak)
            self.max_error_streak = max(self.max_error_streak, self.current_error_streak)
        else:
            self.current_error_streak = 0

    def active(self) -> list[str]:
        """Signals that hold right now."""
        out = []
        if self.current_repeat >= self.repeat_limit:
            out.append("tool_loop")
        if self.current_error_streak >= self.error_limit:
            out.append("error_storm")
        return out

    def observed(self) -> list[str]:
        """Signals that held at any point during the attempt."""
        out = []
        if self.max_repeat >= self.repeat_limit:
            out.append("tool_loop")
        if self.max_error_streak >= self.error_limit:
            out.append("error_storm")
        return out

    def snapshot(self) -> dict:
        now = self._clock()
        since_distinct = (None if self._last_distinct_at is None
                          else max(0, int((now - self._last_distinct_at) * 1000)))
        return {
            "schema": SCHEMA,
            "mode": "shadow",
            "coverage": "instrumented" if self.instrumented else "not_instrumented",
            "tool_calls": self.tool_calls,
            "distinct_tool_calls": self.distinct_tool_calls,
            "current_repeat": self.current_repeat,
            "max_repeat": self.max_repeat,
            "tool_results": self.tool_results,
            "failed_results": self.failed_results,
            "current_error_streak": self.current_error_streak,
            "max_error_streak": self.max_error_streak,
            "repeat_limit": self.repeat_limit,
            "error_limit": self.error_limit,
            "ms_since_distinct_tool": since_distinct,
            "pending_tools": len(self._pending),
            "oldest_pending_tool_ms": self.oldest_pending_ms(),
            "active": self.active(),
            "observed": self.observed(),
        }
