"""Graduated circuit breaker for adaptive and long-horizon jobs (Phase 3).

The breaker evaluates checkpoint-time health signals and walks a bounded
four-level ladder:

    healthy -> steering -> constrained -> stopped

Rules:

* Escalates at most **one level per checkpoint** when one or more trigger
  signals hold (``tool_loop``, ``error_storm``, ``no_progress``,
  ``work_token_cap``).
* Recovers **one level per clean checkpoint** while below ``stopped``.
* ``stopped`` is terminal and disabled by default (``allow_stop=False``);
  without it the ladder tops out at ``constrained``.
* A 5-minute compaction grace exempts ``work_token_cap`` after context
  compaction is observed.
* Steering text is fixed by policy, never model-generated, and is omitted
  from all public projections.
"""

from __future__ import annotations

import math
import os
import time
from typing import Callable, Mapping, Sequence

SCHEMA = "summon.breaker/v1"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
LEVELS = ("healthy", "steering", "constrained", "stopped")
TRIGGER_SIGNALS = ("tool_loop", "error_storm", "no_progress", "work_token_cap")
ALL_SIGNALS = frozenset((*TRIGGER_SIGNALS, "clean_checkpoint"))
ACTIONS = frozenset({"none", "steer", "constrain", "stop", "recover"})
DEFAULT_COMPACTION_GRACE_S = 300.0
_MAX_INT = (1 << 63) - 1


def mode_from_environment(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    value = source.get("SUMMON_BREAKER", "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def allow_stop_from_environment(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("SUMMON_BREAKER_ALLOW_STOP", "").strip() == "1"


def max_work_tokens_from_environment(env: Mapping[str, str] | None = None) -> int | None:
    source = env if env is not None else os.environ
    raw = source.get("SUMMON_MAX_WORK_TOKENS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= _MAX_INT else None


def normalize_signals(signals: Sequence[str] | None) -> list[str]:
    """Return canonical, deduplicated signal names in deterministic order."""
    if not isinstance(signals, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in signals:
        if isinstance(item, str) and item in ALL_SIGNALS and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def fixed_steering_message(signals: Sequence[str]) -> str:
    """Deterministic operator guardrail text; never admits model prose."""
    cleaned = [s for s in normalize_signals(signals) if s in TRIGGER_SIGNALS]
    if not cleaned:
        cleaned = ["no_progress"]
    joined = ", ".join(cleaned)
    return (
        f"Automated guardrail: {joined}. "
        "Stop repeating, summarise attempts, propose next step."
    )


def _extract_work_tokens(work_tokens: object) -> int | None:
    if isinstance(work_tokens, int) and not isinstance(work_tokens, bool):
        return work_tokens if work_tokens >= 0 else None
    if isinstance(work_tokens, dict):
        val = work_tokens.get("work_tokens")
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
    return None


class GraduatedBreaker:
    """Pure policy state machine for checkpoint-time circuit breaking."""

    def __init__(self, *, mode: str = DEFAULT_MODE,
                 allow_stop: bool = False,
                 max_work_tokens: int | None = None,
                 compaction_grace_s: float = DEFAULT_COMPACTION_GRACE_S,
                 clock: Callable[[], float] = time.monotonic):
        if mode not in MODES or mode == "off":
            raise ValueError("GraduatedBreaker requires mode 'shadow' or 'enforce'")
        if not isinstance(allow_stop, bool):
            raise ValueError("allow_stop must be a boolean")
        if max_work_tokens is not None and (
                not isinstance(max_work_tokens, int)
                or isinstance(max_work_tokens, bool)
                or max_work_tokens <= 0):
            raise ValueError("max_work_tokens must be a positive integer or None")
        if (not isinstance(compaction_grace_s, (int, float))
                or isinstance(compaction_grace_s, bool)
                or not math.isfinite(compaction_grace_s)
                or compaction_grace_s < 0):
            raise ValueError("compaction_grace_s must be a non-negative finite number")
        self.mode = mode
        self.allow_stop = allow_stop
        self.max_work_tokens = max_work_tokens
        self.compaction_grace_s = float(compaction_grace_s)
        self._clock = clock
        self.level = "healthy"
        self.transitions = 0
        self.checkpoints = 0
        self._compaction_until: float | None = None
        self.last: dict | None = None

    def note_compaction(self) -> None:
        """Grant a bounded grace window so compaction token spikes do not trip."""
        self._compaction_until = self._clock() + self.compaction_grace_s

    def in_compaction_grace(self) -> bool:
        if self._compaction_until is None:
            return False
        return self._clock() < self._compaction_until

    def collect_triggers(self, *, signals=None,
                         progress_record: Mapping[str, object] | None = None,
                         work_tokens: object = None,
                         extra_triggers: Sequence[str] | None = None) -> list[str]:
        active: list[str] = []
        if signals is not None:
            try:
                sig_active = signals.active()
            except Exception:  # noqa: BLE001
                sig_active = ()
            for name in ("tool_loop", "error_storm"):
                if name in sig_active and name not in active:
                    active.append(name)
        if isinstance(progress_record, Mapping):
            if progress_record.get("judged_active") is False and "no_progress" not in active:
                active.append("no_progress")
            if progress_record.get("tool_loop") is True and "tool_loop" not in active:
                active.insert(0, "tool_loop")
        if self.max_work_tokens is not None and not self.in_compaction_grace():
            observed_tokens = _extract_work_tokens(work_tokens)
            if observed_tokens is not None and observed_tokens > self.max_work_tokens:
                if "work_token_cap" not in active:
                    active.append("work_token_cap")
        for extra in normalize_signals(extra_triggers):
            if extra in TRIGGER_SIGNALS and extra not in active:
                active.append(extra)
        return active

    def evaluate(self, *, signals=None,
                 progress_record: Mapping[str, object] | None = None,
                 work_tokens: object = None,
                 extra_triggers: Sequence[str] | None = None) -> dict:
        """Advance or recover the breaker by at most one level for one checkpoint."""
        triggers = self.collect_triggers(
            signals=signals, progress_record=progress_record,
            work_tokens=work_tokens, extra_triggers=extra_triggers)
        prev = self.level
        prev_idx = LEVELS.index(prev)
        max_idx = 3 if self.allow_stop else 2

        if prev == "stopped":
            next_idx = 3
        elif triggers:
            next_idx = min(prev_idx + 1, max_idx)
        else:
            next_idx = max(0, prev_idx - 1)

        nxt = LEVELS[next_idx]
        self.level = nxt
        self.checkpoints += 1
        transitioned = nxt != prev
        escalated = next_idx > prev_idx
        recovered = next_idx < prev_idx
        if transitioned:
            self.transitions += 1

        if escalated:
            if nxt == "steering":
                action = "steer"
            elif nxt == "constrained":
                action = "constrain"
            else:
                action = "stop"
        elif recovered:
            action = "recover"
        else:
            action = "none"

        recorded_signals = triggers if triggers else ["clean_checkpoint"]
        steering_msg = (fixed_steering_message(recorded_signals)
                        if nxt == "steering" and (escalated or bool(triggers))
                        else None)
        decision = {
            "schema": SCHEMA,
            "mode": self.mode,
            "allow_stop": self.allow_stop,
            "from_level": prev,
            "to_level": nxt,
            "level": nxt,
            "transitioned": transitioned,
            "escalated": escalated,
            "recovered": recovered,
            "action": action,
            "enforced": bool(self.mode == "enforce" and action != "none"),
            "signals": recorded_signals,
            "transitions": self.transitions,
            "checkpoints": self.checkpoints,
            "compaction_grace": self.in_compaction_grace(),
            "constrained_permission": "read-only" if nxt == "constrained" else None,
            "steering_message": steering_msg,
        }
        self.last = decision
        return decision

    def effective_permission(self, base_permission: str) -> str:
        """Clamp live permission when the breaker enforces ``constrained``."""
        if self.mode == "enforce" and self.level in {"constrained", "stopped"}:
            from _builder import clamp_permission
            return clamp_permission(base_permission, "read-only")
        return base_permission

    def projection(self) -> dict:
        if self.last is not None:
            out = sanitize_record(self.last)
            if out is not None:
                return out
        return {
            "schema": SCHEMA,
            "mode": self.mode,
            "allow_stop": self.allow_stop,
            "from_level": self.level,
            "to_level": self.level,
            "level": self.level,
            "transitioned": False,
            "escalated": False,
            "recovered": False,
            "action": "none",
            "enforced": False,
            "signals": ["clean_checkpoint"],
            "transitions": self.transitions,
            "checkpoints": self.checkpoints,
            "compaction_grace": self.in_compaction_grace(),
            "constrained_permission": (
                "read-only" if self.level == "constrained" else None),
        }


def sanitize_record(value: object) -> dict | None:
    """Strict allowlist copy for public projections; never copies steering text."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    out: dict = {"schema": SCHEMA}
    if value.get("mode") in MODES:
        out["mode"] = value["mode"]
    for key in ("allow_stop", "transitioned", "escalated", "recovered",
                "enforced", "compaction_grace"):
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    for key in ("from_level", "to_level", "level"):
        if value.get(key) in LEVELS:
            out[key] = value[key]
    if value.get("action") in ACTIONS:
        out["action"] = value["action"]
    signals = normalize_signals(value.get("signals"))
    if signals:
        out["signals"] = signals[:8]
    for key in ("transitions", "checkpoints"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= _MAX_INT:
            out[key] = item
    perm = value.get("constrained_permission")
    if perm in {None, "read-only"}:
        out["constrained_permission"] = perm
    return out


def constrain_via_resume(root: str, source_job_id: str, *,
                         signals: Sequence[str] = ("no_progress",),
                         request_id: str | None = None,
                         gate_with: str | None = None,
                         allow_credit: bool = False,
                         allow_payg: bool = False,
                         timeout_ms: int = 600_000,
                         gate_timeout_ms: int | None = None,
                         max_runtime_ms: int = 24 * 60 * 60 * 1000):
    """Reserve at most one constrained (``read-only``) successor for a terminal job.

    Delegates to :func:`_job_resume.reserve_request` with ``max_permission="read-only"``
    and the deterministic fixed guardrail prompt so that the source resume
    ledger's single-claim CAS guarantees at-most-once successor creation.
    """
    from _job_resume import reserve_request
    message = fixed_steering_message(signals)
    return reserve_request(
        root, source_job_id,
        message=message,
        request_id=request_id,
        max_permission="read-only",
        gate_with=gate_with,
        allow_credit=allow_credit,
        allow_payg=allow_payg,
        timeout_ms=timeout_ms,
        gate_timeout_ms=gate_timeout_ms,
        max_runtime_ms=max_runtime_ms,
    )
