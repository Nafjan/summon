"""Context-aware handoff evaluation, structured handoff notes, and headroom routing.

Long-horizon agent runs degrade well before a model's nominal context window hits
100%. This module provides three provider-inert capabilities:

1. **Window-aware handoff evaluation (`evaluate_context_handoff`,
   `ContextHandoffTracker`)**:
   Consumes `_usage_metrics.context_occupancy` (`summon.context-occupancy/v1`).
   Default occupancy thresholds scale with window size (`0.75` at `<= 200k`
   tokens, linearly interpolated down to `0.50` at `>= 1M` tokens, or overridden
   via `SUMMON_CONTEXT_HANDOFF_THRESHOLD`).
   * **Fail-safe on missing evidence:** When `context_occupancy.evidence != "reported"`,
     handoff never triggers (`triggered=False`, `reason="occupancy_evidence_unavailable"`).
   * **At-most-once latch:** A running adaptive job triggers handoff steering at
     most once per attempt (`trigger_count <= 1`).
   * **Modes:** `SUMMON_CONTEXT_HANDOFF=off|shadow|enforce` (default `shadow`).
     In `shadow` mode, threshold crossings are recorded in the projection without
     queueing steering or opening attention grace. In `enforce` mode, crossing
     the threshold queues a deterministic handoff-preparation steering directive
     and requests checkpoint attention.

2. **Structured handoff notes (`normalize_handoff_note`,
   `extract_handoff_note_from_envelope`, `handoff_note_digest`,
   `format_handoff_for_resume`)**:
   Validates, secret-redacts, and canonically hashes structured handoff state
   (`state`, `completed`, `next_steps`, `open_questions`, `artifacts_touched`)
   so `_job_resume` can bind the handoff note into the private successor prompt
   and semantic request digest without leaking raw text into public projections.

3. **Continuation vs. fresh-lane routing (`evaluate_continuation_headroom`)**:
   Determines whether a resumable session has sufficient context headroom to
   continue or whether orchestration should prefer a fresh lane.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

SCHEMA = "summon.context-handoff/v1"
CONTEXT_HANDOFF_SCHEMA = SCHEMA

MODES = ("off", "shadow", "enforce")
ACTIONS = ("none", "observe_handoff", "prepare_handoff")
REASONS = (
    "mode_off",
    "occupancy_evidence_unavailable",
    "within_context_headroom",
    "context_occupancy_threshold_reached",
    "handoff_already_triggered",
)
HANDOFF_STATES = ("partial", "blocked", "ready_for_continuation", "done")
HEADROOM_REASONS = (
    "occupancy_evidence_unavailable",
    "sufficient_context_headroom",
    "context_occupancy_exceeded",
)

SMALL_WINDOW_TOKENS = 200_000
LARGE_WINDOW_TOKENS = 1_000_000
SMALL_WINDOW_THRESHOLD = 0.75
LARGE_WINDOW_THRESHOLD = 0.50
MIN_THRESHOLD = 0.10
MAX_THRESHOLD = 0.99

MAX_LIST_ITEMS = 32
MAX_ITEM_CHARS = 512
MAX_ARTIFACTS = 64
MAX_ARTIFACT_PATH_CHARS = 240
_MAX_TOKENS = (1 << 53) - 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHORTNAME_RE = re.compile(r"^[^~]+~[0-9]+(?:\..+)?$")
_SECRET_RE = re.compile(
    r"(?i)(?:sk-(?:or-|ant-|proj-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|"
    r"(?:bearer|api[_-]?key|access[_-]?token|auth[_-]?token|oauth[_-]?token|private[_-]?key|token|secret|password)\s*[=:]\s*(?:\"[^\"\r\n]{6,}\"|'[^'\r\n]{6,}'|[^\s,;\"']{6,}))"
)

HANDOFF_STEERING_PROMPT = (
    "Context window occupancy has reached the handoff threshold. Complete your "
    "current atomic step without starting new broad exploration, verify workspace "
    "cleanliness, and emit a structured final report (STATUS, SUMMARY, FINDINGS, "
    "FOLLOW-UP, HANDOFF, LEFT_BEHIND) capturing completed items, exact next steps, "
    "open questions, and touched artifacts so a fresh successor can resume cleanly."
)

_PROJECTION_KEYS = frozenset({
    "schema",
    "mode",
    "triggered",
    "newly_triggered",
    "enforced",
    "action",
    "reason",
    "evidence",
    "occupied_tokens",
    "window_tokens",
    "ratio",
    "threshold",
    "headroom_tokens",
    "headroom_ratio",
    "trigger_count",
    "evaluations",
    "handoff_note_present",
    "handoff_note_sha256",
})

_NOTE_KEYS = frozenset({
    "state",
    "completed",
    "next_steps",
    "open_questions",
    "artifacts_touched",
})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _redact_text(text: str) -> str:
    cleaned = _SECRET_RE.sub("[REDACTED]", text)
    try:
        from _executor import _redact_output_secrets
        cleaned = _redact_output_secrets(cleaned)
    except Exception:  # noqa: BLE001
        pass
    return cleaned


def _sanitize_text_item(value: Any, *, max_chars: int = MAX_ITEM_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    # Strip control characters and collapse internal whitespace runs.
    cleaned = "".join(ch if (ord(ch) >= 32 and ord(ch) != 127) else " " for ch in value)
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        return None
    cleaned = _redact_text(cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned if cleaned else None


def _sanitize_artifact_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if (
        not raw
        or len(raw) > MAX_ARTIFACT_PATH_CHARS
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw)
        or _SECRET_RE.search(raw)
    ):
        return None
    win = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    if (
        win.drive
        or win.root
        or win.is_absolute()
        or posix.is_absolute()
        or raw.startswith(("/", "\\", "~"))
        or ":" in raw
    ):
        return None
    normalized = raw.replace("\\", "/").strip("/")
    if not normalized:
        return None
    segments = [seg for seg in normalized.split("/") if seg and seg != "."]
    if not segments:
        return None
    for seg in segments:
        if (
            seg in {"..", ".git", ".claude"}
            or seg.endswith((".", " "))
            or _SHORTNAME_RE.match(seg)
        ):
            return None
    joined = "/".join(segments)
    return joined if len(joined) <= MAX_ARTIFACT_PATH_CHARS else None


def mode_from_environment(raw: str | None = None) -> str:
    """Resolve `SUMMON_CONTEXT_HANDOFF` (`off|shadow|enforce`, default `shadow`)."""
    val = raw if raw is not None else os.environ.get("SUMMON_CONTEXT_HANDOFF")
    if not isinstance(val, str) or not val.strip():
        return "shadow"
    norm = val.strip().lower()
    if norm in {"0", "false", "no", "off", "disabled"}:
        return "off"
    if norm in {"1", "true", "yes", "enforce", "enforced"}:
        return "enforce"
    if norm in {"shadow", "observe"}:
        return "shadow"
    return "shadow"


def threshold_from_environment(raw: str | None = None) -> float | None:
    """Parse optional `SUMMON_CONTEXT_HANDOFF_THRESHOLD` in `[0.10, 0.99]`."""
    val = raw if raw is not None else os.environ.get("SUMMON_CONTEXT_HANDOFF_THRESHOLD")
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    try:
        num = float(str(val).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < MIN_THRESHOLD or num > MAX_THRESHOLD:
        return None
    return round(num, 4)


def handoff_threshold_for_window(
    window_tokens: Any, *, override: float | None = None
) -> float:
    """Return the occupancy ratio threshold for `window_tokens`.

    * `<= 200_000` tokens -> `0.75`
    * `>= 1_000_000` tokens -> `0.50`
    * Between `200_000` and `1_000_000` -> linear interpolation in `[0.50, 0.75]`
    """
    if override is not None and _is_finite_number(override):
        ov = float(override)
        if MIN_THRESHOLD <= ov <= MAX_THRESHOLD:
            return round(ov, 4)
    if not _is_int(window_tokens) or window_tokens <= 0:
        return SMALL_WINDOW_THRESHOLD
    if window_tokens <= SMALL_WINDOW_TOKENS:
        return SMALL_WINDOW_THRESHOLD
    if window_tokens >= LARGE_WINDOW_TOKENS:
        return LARGE_WINDOW_THRESHOLD
    span = LARGE_WINDOW_TOKENS - SMALL_WINDOW_TOKENS
    progress = (window_tokens - SMALL_WINDOW_TOKENS) / span
    interpolated = SMALL_WINDOW_THRESHOLD - (
        (SMALL_WINDOW_THRESHOLD - LARGE_WINDOW_THRESHOLD) * progress
    )
    return round(interpolated, 4)


def _extract_verified_occupancy(
    occupancy: Any,
) -> tuple[str, int | None, int | None, float | None]:
    """Extract `(evidence, occupied_tokens, window_tokens, ratio)` fail-safely."""
    if not isinstance(occupancy, dict):
        return ("absent", None, None, None)
    raw_evidence = occupancy.get("evidence")
    evidence = (
        raw_evidence
        if isinstance(raw_evidence, str)
        and raw_evidence
        in {
            "reported",
            "absent",
            "incomplete",
            "occupied_without_window",
            "ambiguous_cache_semantics",
        }
        else "absent"
    )
    occupied = occupancy.get("occupied_tokens")
    window = occupancy.get("window_tokens")
    ratio = occupancy.get("ratio")
    if not (_is_int(occupied) and 0 <= occupied <= _MAX_TOKENS):
        occupied = None
    if not (_is_int(window) and 0 < window <= _MAX_TOKENS):
        window = None
    if evidence != "reported" or occupied is None or window is None:
        return (
            evidence if evidence != "reported" else "incomplete",
            occupied,
            window,
            None,
        )
    expected_ratio = round(occupied / window, 4)
    if not _is_finite_number(ratio):
        return ("incomplete", occupied, window, None)
    ratio_f = round(float(ratio), 4)
    if ratio_f < 0.0 or abs(ratio_f - expected_ratio) > 0.01:
        return ("incomplete", occupied, window, None)
    return ("reported", occupied, window, expected_ratio)


def evaluate_context_handoff(
    occupancy: Any,
    *,
    mode: str = "shadow",
    threshold_override: float | None = None,
    already_triggered: bool = False,
    trigger_count: int = 0,
    evaluations: int = 1,
    handoff_note_sha256: str | None = None,
) -> dict:
    """Evaluate one `context_occupancy` snapshot and return a `summon.context-handoff/v1` record."""
    eff_mode = mode if mode in MODES else "shadow"
    ev_count = evaluations if _is_int(evaluations) and evaluations >= 0 else 1
    trig_count = trigger_count if _is_int(trigger_count) and trigger_count >= 0 else 0
    note_sha = (
        handoff_note_sha256
        if isinstance(handoff_note_sha256, str) and _SHA256_RE.fullmatch(handoff_note_sha256)
        else None
    )
    evidence, occupied, window, ratio = _extract_verified_occupancy(occupancy)
    threshold = (
        handoff_threshold_for_window(window, override=threshold_override)
        if window is not None
        else (
            round(float(threshold_override), 4)
            if threshold_override is not None
            and _is_finite_number(threshold_override)
            and MIN_THRESHOLD <= float(threshold_override) <= MAX_THRESHOLD
            else None
        )
    )
    if eff_mode == "off":
        is_reported = (
            evidence == "reported"
            and occupied is not None
            and window is not None
            and ratio is not None
        )
        return {
            "schema": SCHEMA,
            "mode": "off",
            "triggered": False,
            "newly_triggered": False,
            "enforced": False,
            "action": "none",
            "reason": "mode_off",
            "evidence": evidence,
            "occupied_tokens": occupied,
            "window_tokens": window,
            "ratio": ratio if is_reported else None,
            "threshold": threshold,
            "headroom_tokens": (
                max(0, window - occupied) if is_reported else None
            ),
            "headroom_ratio": (
                round(max(0.0, 1.0 - ratio), 4) if is_reported else None
            ),
            "trigger_count": 0,
            "evaluations": ev_count,
            "handoff_note_present": note_sha is not None,
            "handoff_note_sha256": note_sha,
        }

    if evidence != "reported" or occupied is None or window is None or ratio is None or threshold is None:
        return {
            "schema": SCHEMA,
            "mode": eff_mode,
            "triggered": bool(already_triggered),
            "newly_triggered": False,
            "enforced": False,
            "action": "none",
            "reason": "occupancy_evidence_unavailable",
            "evidence": evidence,
            "occupied_tokens": occupied,
            "window_tokens": window,
            "ratio": None,
            "threshold": threshold,
            "headroom_tokens": None,
            "headroom_ratio": None,
            "trigger_count": 1 if already_triggered else min(1, trig_count),
            "evaluations": ev_count,
            "handoff_note_present": note_sha is not None,
            "handoff_note_sha256": note_sha,
        }

    headroom_tokens = max(0, window - occupied)
    headroom_ratio = round(max(0.0, 1.0 - ratio), 4)

    if already_triggered:
        return {
            "schema": SCHEMA,
            "mode": eff_mode,
            "triggered": True,
            "newly_triggered": False,
            "enforced": False,
            "action": "none",
            "reason": "handoff_already_triggered",
            "evidence": "reported",
            "occupied_tokens": occupied,
            "window_tokens": window,
            "ratio": ratio,
            "threshold": threshold,
            "headroom_tokens": headroom_tokens,
            "headroom_ratio": headroom_ratio,
            "trigger_count": 1,
            "evaluations": ev_count,
            "handoff_note_present": note_sha is not None,
            "handoff_note_sha256": note_sha,
        }

    if ratio >= threshold:
        enforced = eff_mode == "enforce"
        action = "prepare_handoff" if enforced else "observe_handoff"
        return {
            "schema": SCHEMA,
            "mode": eff_mode,
            "triggered": True,
            "newly_triggered": True,
            "enforced": enforced,
            "action": action,
            "reason": "context_occupancy_threshold_reached",
            "evidence": "reported",
            "occupied_tokens": occupied,
            "window_tokens": window,
            "ratio": ratio,
            "threshold": threshold,
            "headroom_tokens": headroom_tokens,
            "headroom_ratio": headroom_ratio,
            "trigger_count": 1,
            "evaluations": ev_count,
            "handoff_note_present": note_sha is not None,
            "handoff_note_sha256": note_sha,
        }

    return {
        "schema": SCHEMA,
        "mode": eff_mode,
        "triggered": False,
        "newly_triggered": False,
        "enforced": False,
        "action": "none",
        "reason": "within_context_headroom",
        "evidence": "reported",
        "occupied_tokens": occupied,
        "window_tokens": window,
        "ratio": ratio,
        "threshold": threshold,
        "headroom_tokens": headroom_tokens,
        "headroom_ratio": headroom_ratio,
        "trigger_count": 0,
        "evaluations": ev_count,
        "handoff_note_present": note_sha is not None,
        "handoff_note_sha256": note_sha,
    }


class _PendingHandoffDecision(dict):
    """Dict carrying two-phase commit/rollback hooks for `ContextHandoffTracker`."""

    def __init__(self, payload: dict, *, commit_fn=None, rollback_fn=None):
        super().__init__(payload)
        self._commit = commit_fn
        self._rollback = rollback_fn


class ContextHandoffTracker:
    """Per-attempt stateful evaluator with at-most-once trigger latching."""

    def __init__(
        self,
        *,
        mode: str = "shadow",
        threshold_override: float | None = None,
    ) -> None:
        self.mode = mode if mode in MODES else "shadow"
        self.threshold_override = (
            round(float(threshold_override), 4)
            if threshold_override is not None
            and _is_finite_number(threshold_override)
            and MIN_THRESHOLD <= float(threshold_override) <= MAX_THRESHOLD
            else None
        )
        self.triggered = False
        self.trigger_count = 0
        self.evaluations = 0
        self.handoff_note: dict | None = None
        self.handoff_note_sha256: str | None = None
        self._last_record: dict = evaluate_context_handoff(
            None,
            mode=self.mode,
            threshold_override=self.threshold_override,
            already_triggered=False,
            trigger_count=0,
            evaluations=0,
            handoff_note_sha256=None,
        )

    def sync_from_control(self, control: Any) -> None:
        """Synchronize trigger latch from replayed `RuntimeControl` state."""
        if bool(getattr(control, "handoff_triggered", False)):
            self.triggered = True
            self.trigger_count = 1

    def evaluate(self, occupancy: Any) -> dict:
        """Evaluate `occupancy` and return a `summon.context-handoff/v1` decision."""
        prev_triggered = self.triggered
        prev_count = self.trigger_count
        prev_evals = self.evaluations
        prev_record = copy.deepcopy(self._last_record)

        next_evals = prev_evals + 1
        decision = evaluate_context_handoff(
            occupancy,
            mode=self.mode,
            threshold_override=self.threshold_override,
            already_triggered=prev_triggered,
            trigger_count=prev_count,
            evaluations=next_evals,
            handoff_note_sha256=self.handoff_note_sha256,
        )
        self.evaluations = next_evals
        if decision.get("triggered"):
            self.triggered = True
            self.trigger_count = 1
        self._last_record = copy.deepcopy(decision)

        def _commit() -> None:
            pass

        def _rollback() -> None:
            self.triggered = prev_triggered
            self.trigger_count = prev_count
            self.evaluations = prev_evals
            self._last_record = prev_record

        return _PendingHandoffDecision(decision, commit_fn=_commit, rollback_fn=_rollback)

    def attach_handoff_note(self, note: Any) -> str | None:
        """Normalize and bind a structured handoff note into the projection."""
        normalized = normalize_handoff_note(note)
        if normalized is None:
            return None
        digest = handoff_note_digest(normalized)
        self.handoff_note = normalized
        self.handoff_note_sha256 = digest
        self._last_record["handoff_note_present"] = True
        self._last_record["handoff_note_sha256"] = digest
        return digest

    def projection(self) -> dict:
        return copy.deepcopy(self._last_record)


def validate_projection(value: Any) -> bool:
    """Strictly validate a `summon.context-handoff/v1` public projection."""
    if not isinstance(value, dict) or set(value.keys()) != _PROJECTION_KEYS:
        return False
    if value.get("schema") != SCHEMA:
        return False
    mode = value.get("mode")
    if mode not in MODES:
        return False
    triggered = value.get("triggered")
    newly_triggered = value.get("newly_triggered")
    enforced = value.get("enforced")
    note_present = value.get("handoff_note_present")
    if not all(isinstance(b, bool) for b in (triggered, newly_triggered, enforced, note_present)):
        return False
    action = value.get("action")
    reason = value.get("reason")
    if action not in ACTIONS or reason not in REASONS:
        return False
    evidence = value.get("evidence")
    if evidence not in {
        "reported",
        "absent",
        "incomplete",
        "occupied_without_window",
        "ambiguous_cache_semantics",
    }:
        return False
    trig_count = value.get("trigger_count")
    evals = value.get("evaluations")
    if not (_is_int(trig_count) and trig_count in {0, 1}):
        return False
    if not (_is_int(evals) and evals >= 0):
        return False
    if triggered and trig_count != 1:
        return False
    if not triggered and (newly_triggered or enforced or trig_count != 0):
        return False
    if newly_triggered and not triggered:
        return False
    if enforced and (mode != "enforce" or not newly_triggered or action != "prepare_handoff"):
        return False
    if not newly_triggered and action != "none":
        return False
    if newly_triggered:
        expected_action = "prepare_handoff" if mode == "enforce" else "observe_handoff"
        if action != expected_action or reason != "context_occupancy_threshold_reached":
            return False

    occupied = value.get("occupied_tokens")
    window = value.get("window_tokens")
    ratio = value.get("ratio")
    threshold = value.get("threshold")
    headroom_tokens = value.get("headroom_tokens")
    headroom_ratio = value.get("headroom_ratio")

    if occupied is not None and not (_is_int(occupied) and 0 <= occupied <= _MAX_TOKENS):
        return False
    if window is not None and not (_is_int(window) and 0 < window <= _MAX_TOKENS):
        return False
    if threshold is not None:
        if not _is_finite_number(threshold):
            return False
        if not (MIN_THRESHOLD <= float(threshold) <= MAX_THRESHOLD):
            return False

    if mode == "off":
        if (
            triggered
            or newly_triggered
            or enforced
            or action != "none"
            or reason != "mode_off"
            or trig_count != 0
        ):
            return False
    elif reason == "mode_off":
        return False

    if evidence == "reported":
        if occupied is None or window is None or threshold is None:
            return False
        if not _is_finite_number(ratio) or not _is_finite_number(headroom_ratio):
            return False
        if not (_is_int(headroom_tokens) and headroom_tokens == max(0, window - occupied)):
            return False
        expected_ratio = round(occupied / window, 4)
        if abs(float(ratio) - expected_ratio) > 0.01:
            return False
        expected_headroom = round(max(0.0, 1.0 - float(ratio)), 4)
        if abs(float(headroom_ratio) - expected_headroom) > 0.01:
            return False
    else:
        if ratio is not None or headroom_tokens is not None or headroom_ratio is not None:
            return False
        if newly_triggered or enforced or action != "none":
            return False
        if mode != "off" and reason != "occupancy_evidence_unavailable":
            return False

    note_sha = value.get("handoff_note_sha256")
    if note_present:
        if not (isinstance(note_sha, str) and _SHA256_RE.fullmatch(note_sha)):
            return False
    elif note_sha is not None:
        return False
    return True


def sanitize_record(value: Any) -> dict | None:
    """Return a defensive copy of `value` if it is a valid `summon.context-handoff/v1` record."""
    if not validate_projection(value):
        return None
    return copy.deepcopy(value)


def _normalize_string_list(value: Any, *, max_items: int = MAX_LIST_ITEMS) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [
            line.lstrip("-*•0123456789.) ").strip()
            for line in value.splitlines()
            if line.strip()
        ]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if not isinstance(item, str):
                return None
            parts.append(item)
    else:
        return None
    out: list[str] = []
    for raw in parts:
        cleaned = _sanitize_text_item(raw)
        if cleaned:
            out.append(cleaned)
            if len(out) >= max_items:
                break
    return out


def normalize_handoff_note(value: Any) -> dict | None:
    """Validate and normalize a structured handoff note.

    Required shape (extra keys rejected when passed as a structured note):
    * `state`: `"partial" | "blocked" | "ready_for_continuation" | "done"`
    * `completed`: list of bounded redacted strings
    * `next_steps`: list of bounded redacted strings
    * `open_questions`: list of bounded redacted strings
    * `artifacts_touched`: list of safe repo-relative paths
    """
    if not isinstance(value, dict):
        return None
    if not set(value.keys()).issubset(_NOTE_KEYS):
        return None
    raw_state = value.get("state", "ready_for_continuation")
    if not isinstance(raw_state, str):
        return None
    state_norm = raw_state.strip().lower()
    state_map = {
        "partial": "partial",
        "blocked": "blocked",
        "ready": "ready_for_continuation",
        "ready_for_continuation": "ready_for_continuation",
        "done": "done",
        "success": "done",
    }
    state = state_map.get(state_norm)
    if state not in HANDOFF_STATES:
        return None
    completed = _normalize_string_list(value.get("completed"))
    next_steps = _normalize_string_list(value.get("next_steps"))
    open_questions = _normalize_string_list(value.get("open_questions"))
    if completed is None or next_steps is None or open_questions is None:
        return None

    raw_artifacts = value.get("artifacts_touched")
    artifacts: list[str] = []
    if raw_artifacts is not None:
        if not isinstance(raw_artifacts, (list, tuple)):
            return None
        seen: set[str] = set()
        for item in raw_artifacts:
            safe_path = _sanitize_artifact_path(item)
            if safe_path is None:
                continue
            if safe_path not in seen:
                seen.add(safe_path)
                artifacts.append(safe_path)
                if len(artifacts) >= MAX_ARTIFACTS:
                    break

    if not (completed or next_steps or open_questions or artifacts):
        return None

    return {
        "state": state,
        "completed": completed,
        "next_steps": next_steps,
        "open_questions": open_questions,
        "artifacts_touched": artifacts,
    }


def extract_handoff_note_from_envelope(envelope: Any) -> dict | None:
    """Derive a normalized handoff note from an envelope's `report` and `files_touched`."""
    if not isinstance(envelope, dict):
        return None
    explicit = envelope.get("handoff_note")
    if isinstance(explicit, dict):
        normalized = normalize_handoff_note(explicit)
        if normalized is not None:
            return normalized
    report = envelope.get("report")
    if not isinstance(report, dict) or not report:
        return None
    raw_status = str(report.get("status") or envelope.get("status") or "partial").strip().lower()
    first_token = raw_status.split()[0].rstrip("|,:;-") if raw_status else "partial"
    if first_token in {"done", "success"}:
        state = "done"
    elif first_token == "blocked":
        state = "blocked"
    elif first_token == "partial":
        state = "partial"
    else:
        state = "ready_for_continuation"

    completed: list[str] = []
    for key in ("summary", "handoff"):
        val = report.get(key)
        if isinstance(val, str) and val.strip():
            items = _normalize_string_list(val)
            if items:
                for it in items:
                    if it not in completed and len(completed) < MAX_LIST_ITEMS:
                        completed.append(it)

    next_steps: list[str] = []
    follow_up = report.get("follow_up")
    if isinstance(follow_up, str) and follow_up.strip().lower() not in {"", "none", "n/a"}:
        items = _normalize_string_list(follow_up)
        if items:
            next_steps.extend(items[:MAX_LIST_ITEMS])

    open_questions: list[str] = []
    findings = report.get("findings")
    if isinstance(findings, str) and findings.strip().lower() not in {"", "none", "n/a"}:
        items = _normalize_string_list(findings)
        if items:
            open_questions.extend(items[:MAX_LIST_ITEMS])

    artifacts: list[str] = []
    seen_paths: set[str] = set()
    raw_files = envelope.get("files_touched")
    if isinstance(raw_files, (list, tuple)):
        for fpath in raw_files:
            safe = _sanitize_artifact_path(fpath)
            if safe and safe not in seen_paths:
                seen_paths.add(safe)
                artifacts.append(safe)
                if len(artifacts) >= MAX_ARTIFACTS:
                    break

    return normalize_handoff_note({
        "state": state,
        "completed": completed,
        "next_steps": next_steps,
        "open_questions": open_questions,
        "artifacts_touched": artifacts,
    })


def handoff_note_digest(note: Any) -> str | None:
    """Return the canonical SHA-256 hex digest of a valid structured handoff note."""
    normalized = normalize_handoff_note(note)
    if normalized is None:
        return None
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def format_handoff_for_resume(note: Any) -> str | None:
    """Format a normalized handoff note for private successor prompt injection."""
    normalized = normalize_handoff_note(note)
    if normalized is None:
        return None
    lines = [f"Prior handoff state: {normalized['state']}"]
    if normalized["completed"]:
        lines.append("Completed in prior turn:")
        for item in normalized["completed"]:
            lines.append(f"- {item}")
    if normalized["next_steps"]:
        lines.append("Next steps to execute:")
        for item in normalized["next_steps"]:
            lines.append(f"- {item}")
    if normalized["open_questions"]:
        lines.append("Open questions / findings:")
        for item in normalized["open_questions"]:
            lines.append(f"- {item}")
    if normalized["artifacts_touched"]:
        lines.append(
            "Artifacts touched: " + ", ".join(normalized["artifacts_touched"])
        )
    return "\n".join(lines)


def evaluate_continuation_headroom(
    occupancy: Any,
    *,
    floor: float | None = None,
    threshold_override: float | None = None,
) -> dict:
    """Decide whether a resumable lane has enough context headroom to continue.

    * When `occupancy.evidence != "reported"`, returns `prefer_fresh_lane=False`,
      `continuation_eligible=True`, `reason="occupancy_evidence_unavailable"`.
    * When `occupancy.evidence == "reported"`, compares `ratio` against the
      window-scaled threshold (or `headroom_ratio` against `floor` when supplied).
      If occupancy is at or above threshold (or headroom is at or below `floor`),
      returns `prefer_fresh_lane=True`, `continuation_eligible=False`,
      `reason="context_occupancy_exceeded"`.
    """
    evidence, occupied, window, ratio = _extract_verified_occupancy(occupancy)
    valid_floor = (
        round(float(floor), 4)
        if floor is not None
        and _is_finite_number(floor)
        and 0.01 <= float(floor) <= 0.90
        else None
    )
    if evidence != "reported" or occupied is None or window is None or ratio is None:
        return {
            "prefer_fresh_lane": False,
            "continuation_eligible": True,
            "reason": "occupancy_evidence_unavailable",
            "evidence": evidence,
            "occupied_tokens": occupied,
            "window_tokens": window,
            "ratio": None,
            "threshold": None,
            "headroom_ratio": None,
            "headroom_floor": valid_floor,
        }

    if valid_floor is not None:
        threshold = round(1.0 - valid_floor, 4)
        headroom_floor = valid_floor
    else:
        threshold = handoff_threshold_for_window(window, override=threshold_override)
        headroom_floor = round(1.0 - threshold, 4)

    headroom_ratio = round(max(0.0, 1.0 - ratio), 4)
    exceeded = ratio >= threshold or headroom_ratio <= headroom_floor
    return {
        "prefer_fresh_lane": bool(exceeded),
        "continuation_eligible": not bool(exceeded),
        "reason": (
            "context_occupancy_exceeded"
            if exceeded
            else "sufficient_context_headroom"
        ),
        "evidence": "reported",
        "occupied_tokens": occupied,
        "window_tokens": window,
        "ratio": ratio,
        "threshold": threshold,
        "headroom_ratio": headroom_ratio,
        "headroom_floor": headroom_floor,
    }
