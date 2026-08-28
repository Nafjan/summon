"""Provider-inert trusted-liveness state machine with an injected clock.

Raw provider objects never enter :class:`LivenessTracker`. An executor-owned
emitter is the capability boundary: parser adapters translate only recognized
transport events, while model text cannot assert trust.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time
from typing import Callable


SCHEMA = "summon.liveness/v1"
TRUSTED_KINDS = {
    "transport_started", "stream_event", "output_text", "tool_activity",
    "reconnect", "finalizing", "terminal", "cancelled",
}
FINAL_PHASES = {"terminal", "cancelled", "timed_out"}
MAX_PROGRESS = (1 << 63) - 1
MAX_DEDUPE_IDS = 4096
MAX_LIVENESS_MS = 7 * 24 * 60 * 60 * 1000


class LivenessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _TrustedEvent:
    authority: object
    sequence: int
    kind: str
    attempt_id: str
    session_id: str | None
    source_event_id: str | None
    output_tokens: int | None
    output_chars: int | None
    tool_id: str | None
    progress: int | None


class _Emitter:
    """Capability held only by the executor/parser adapter for one attempt."""

    def __init__(self, tracker: "LivenessTracker"):
        self._tracker = tracker
        self._sequence = 0

    def emit(self, kind: str, *, session_id: str | None = None,
             source_event_id: str | None = None,
             output_tokens: int | None = None,
             output_chars: int | None = None,
             tool_id: str | None = None,
             progress: int | None = None) -> bool:
        self._sequence += 1
        return self._tracker.observe(_TrustedEvent(
            authority=self._tracker._authority,
            sequence=self._sequence,
            kind=kind,
            attempt_id=self._tracker.attempt_id,
            session_id=session_id,
            source_event_id=source_event_id,
            output_tokens=output_tokens,
            output_chars=output_chars,
            tool_id=tool_id,
            progress=progress,
        ))

    def reconnect(self, *, old_session_id: str | None,
                  new_session_id: str, source_event_id: str | None = None) -> bool:
        self._sequence += 1
        return self._tracker._reconnect(
            authority=self._tracker._authority,
            sequence=self._sequence,
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            source_event_id=source_event_id,
        )


class LivenessTracker:
    def __init__(self, *, attempt_id: str, session_id: str | None = None,
                 overall_ms: int, first_event_ms: int, idle_ms: int,
                 finalization_ms: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 128:
            raise LivenessError("attempt_id is required and bounded")
        if session_id is not None and (not isinstance(session_id, str)
                                       or not session_id or len(session_id) > 256):
            raise LivenessError("session_id must be a bounded string")
        if finalization_ms is None:
            finalization_ms = idle_ms
        for name, value in (("overall_ms", overall_ms),
                            ("first_event_ms", first_event_ms),
                            ("idle_ms", idle_ms),
                            ("finalization_ms", finalization_ms)):
            if (not isinstance(value, int) or isinstance(value, bool)
                    or not 1 <= value <= MAX_LIVENESS_MS):
                raise LivenessError(f"{name} must be a bounded positive integer")
        self.attempt_id = attempt_id
        self.session_id = session_id
        self.overall_ms = overall_ms
        self.first_event_ms = first_event_ms
        self.idle_ms = idle_ms
        self.finalization_ms = finalization_ms
        self._clock = clock
        self._authority = object()
        self.started = self._sample(None)
        self._last_now = self.started
        self.first_trusted: float | None = None
        self.last_meaningful: float | None = None
        self.finalization_started: float | None = None
        self.terminal: float | None = None
        self.timeout_reason: str | None = None
        self.last_trusted_kind: str | None = None
        self.last_activity_kind: str | None = None
        self.phase = "startup"
        self.counts = {
            "trusted": 0, "meaningful": 0, "ignored": 0, "untrusted": 0,
            "duplicates": 0, "reordered": 0, "reconnects": 0, "tools": 0,
        }
        self._last_sequence = 0
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._tool_progress: dict[str, int] = {}
        self._emitter_issued = False

    def emitter(self) -> _Emitter:
        """Issue the single parser-adapter capability for this attempt."""
        if self._emitter_issued:
            raise LivenessError("liveness emitter has already been issued")
        self._emitter_issued = True
        return _Emitter(self)

    def _sample(self, prior: float | None) -> float:
        now = self._clock()
        if (not isinstance(now, (int, float)) or isinstance(now, bool)
                or not math.isfinite(now) or (prior is not None and now < prior)):
            raise LivenessError("monotonic clock moved backwards or became non-finite")
        return float(now)

    @staticmethod
    def _bounded_count(value: int | None) -> int | None:
        if (not isinstance(value, int) or isinstance(value, bool)
                or not 0 <= value <= MAX_PROGRESS):
            return None
        return value

    def _accept_identity(self, event: _TrustedEvent) -> bool:
        if event.authority is not self._authority or event.attempt_id != self.attempt_id:
            self.counts["untrusted"] += 1
            return False
        if event.kind not in TRUSTED_KINDS:
            self.counts["ignored"] += 1
            return False
        if event.sequence <= self._last_sequence:
            self.counts["reordered"] += 1
            return False
        if self.session_id is None and isinstance(event.session_id, str) \
                and event.session_id:
            self.session_id = event.session_id
        elif self.session_id is not None and event.session_id != self.session_id:
            self.counts["untrusted"] += 1
            return False
        source_id = event.source_event_id
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id or len(source_id) > 256:
                self.counts["untrusted"] += 1
                return False
            if source_id in self._seen_ids:
                self.counts["duplicates"] += 1
                return False
        self._last_sequence = event.sequence
        if source_id is not None:
            self._seen_ids.add(source_id)
            self._seen_order.append(source_id)
            while len(self._seen_order) > MAX_DEDUPE_IDS:
                self._seen_ids.discard(self._seen_order.popleft())
        return True

    def observe(self, event: object) -> bool:
        """Observe one capability-bound parser event; return whether it reset a clock."""
        if self.phase in FINAL_PHASES:
            self.counts["ignored"] += 1
            return False
        if not isinstance(event, _TrustedEvent):
            self.counts["untrusted"] += 1
            return False
        if not self._accept_identity(event):
            return False
        now = self._sample(self._last_now)
        self._last_now = now
        # An event observed at or after a deadline cannot revive the attempt.
        if self._expire_at(now) is not None:
            return False
        self.counts["trusted"] += 1
        self.last_trusted_kind = event.kind
        first = self.first_trusted is None
        if first:
            self.first_trusted = now
        meaningful = False
        if event.kind == "output_text":
            tokens = self._bounded_count(event.output_tokens)
            chars = self._bounded_count(event.output_chars)
            meaningful = bool((tokens or 0) > 0 or (chars or 0) > 0)
        elif event.kind == "tool_activity":
            progress = self._bounded_count(event.progress)
            if (isinstance(event.tool_id, str) and 0 < len(event.tool_id) <= 128
                    and progress is not None
                    and progress > self._tool_progress.get(event.tool_id, -1)):
                self._tool_progress[event.tool_id] = progress
                meaningful = True
                self.counts["tools"] += 1
        if meaningful:
            self.last_meaningful = now
            # A genuinely new output/tool event means finalization was not the
            # current phase after all. Permit a later finalizing transition to
            # start a fresh bounded grace, but never let repeated finalizing
            # packets slide an existing deadline forward.
            self.finalization_started = None
            self.last_activity_kind = (
                "tool" if event.kind == "tool_activity" else "generation")
            self.counts["meaningful"] += 1
            self.phase = "generation"
        elif event.kind == "reconnect":
            self.counts["reconnects"] += 1
            self.phase = "reconnect"
        elif event.kind == "finalizing":
            if self.finalization_started is None:
                self.finalization_started = now
            self.phase = "finalization"
        if event.kind in {"terminal", "cancelled"}:
            self.terminal = now
            self.phase = "cancelled" if event.kind == "cancelled" else "terminal"
        return meaningful or first

    def _reconnect(self, *, authority: object, sequence: int,
                   old_session_id: str | None, new_session_id: str,
                   source_event_id: str | None) -> bool:
        if self.phase in FINAL_PHASES or authority is not self._authority:
            self.counts["untrusted"] += 1
            return False
        if old_session_id != self.session_id or not isinstance(new_session_id, str) \
                or not new_session_id or len(new_session_id) > 256:
            self.counts["untrusted"] += 1
            return False
        event = _TrustedEvent(authority, sequence, "reconnect", self.attempt_id,
                              old_session_id, source_event_id, None, None, None, None)
        if not self._accept_identity(event):
            return False
        now = self._sample(self._last_now)
        self._last_now = now
        if self._expire_at(now) is not None:
            return False
        self.session_id = new_session_id
        self.counts["trusted"] += 1
        self.counts["reconnects"] += 1
        self.phase = "reconnect"
        return True

    def _expire_at(self, now: float) -> str | None:
        if self.timeout_reason is not None:
            return self.timeout_reason
        if self.terminal is not None:
            return None
        elapsed = int((now - self.started) * 1000)
        reason = None
        if self.finalization_started is not None:
            if int((now - self.finalization_started) * 1000) >= self.finalization_ms:
                reason = "finalization_timeout"
        elif elapsed >= self.overall_ms:
            reason = "overall_timeout"
        elif self.first_trusted is None and elapsed >= self.first_event_ms:
            reason = "startup_timeout"
        else:
            baseline = self.last_meaningful or self.first_trusted
            if baseline is not None and int((now - baseline) * 1000) >= self.idle_ms:
                reason = "generation_idle_timeout"
        if reason is not None:
            self.timeout_reason = reason
            self.phase = "timed_out"
        return reason

    def expired(self) -> str | None:
        now = self._sample(self._last_now)
        self._last_now = now
        return self._expire_at(now)

    def extend_overall(self, duration_ms: int) -> None:
        """Advance the hard liveness clock after a bounded operator extension.

        RuntimeControl authenticates and bounds the command. The tracker keeps
        an independent overall clock, so it must advance in the same executor
        iteration or the original liveness deadline can still kill healthy
        work.
        """
        if (not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
                or duration_ms <= 0
                or self.overall_ms + duration_ms > MAX_LIVENESS_MS):
            raise LivenessError("overall extension exceeds the bounded job runtime")
        if self.phase in FINAL_PHASES or self.timeout_reason is not None:
            raise LivenessError("a terminal liveness clock cannot be extended")
        self.overall_ms += duration_ms

    def next_deadline_ms(self) -> int | None:
        """Milliseconds until the next active deadline, for a bounded pipe poll."""
        if self.phase in FINAL_PHASES:
            return None
        now = self._sample(self._last_now)
        self._last_now = now
        if self.finalization_started is not None:
            return max(0, int((self.finalization_started
                               + self.finalization_ms / 1000 - now) * 1000))
        deadlines = [self.started + self.overall_ms / 1000]
        if self.first_trusted is None:
            deadlines.append(self.started + self.first_event_ms / 1000)
        baseline = self.last_meaningful or self.first_trusted
        if baseline is not None:
            deadlines.append(baseline + self.idle_ms / 1000)
        return max(0, int((min(deadlines) - now) * 1000))

    def meaningful_within(self, interval_ms: int) -> bool:
        """Whether trusted output/tool progress occurred in the recent interval."""
        if not isinstance(interval_ms, int) or isinstance(interval_ms, bool) \
                or interval_ms <= 0:
            raise LivenessError("activity interval must be a positive integer")
        now = self._sample(self._last_now)
        self._last_now = now
        baseline = self.last_meaningful
        return baseline is not None and (now - baseline) * 1000 < interval_ms

    def snapshot(self) -> dict:
        now = self._sample(self._last_now)
        self._last_now = now
        expired = self._expire_at(now)
        return {
            "schema": SCHEMA,
            "phase": self.phase,
            "elapsed_ms": max(0, int((now - self.started) * 1000)),
            "first_trusted_event_ms": (None if self.first_trusted is None else
                                       int((self.first_trusted - self.started) * 1000)),
            "last_meaningful_event_ms": (None if self.last_meaningful is None else
                                          int((self.last_meaningful - self.started) * 1000)),
            "expired": expired,
            "last_trusted_kind": self.last_trusted_kind,
            "last_activity_kind": self.last_activity_kind,
            "counts": dict(self.counts),
        }
