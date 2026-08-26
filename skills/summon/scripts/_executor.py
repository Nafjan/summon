"""Subprocess driver: spawn the CLI, consume its stream, shape the response."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping


_SENSITIVE_ARG_KEYS = {
    "api", "api-key", "token", "secret", "password", "private-key",
    "access-token", "oauth-token", "auth-token", "authorization",
}


class ProviderLaunchError(RuntimeError):
    """A controlled provider launch was refused before provider contact."""


class ProviderDeadlineError(ProviderLaunchError):
    """A controlled provider attempt reached its immutable deadline."""


class ProviderLaunchControl:
    """Single-use provider boundary used by durable orchestrators.

    Ordinary dispatches do not create this object and retain their historical
    retry/fallback behavior.  A deliberation creates one control per durably
    committed physical attempt.  ``before_provider_launch`` is the only claim
    point and is intentionally atomic, so duplicate or concurrent paths cannot
    contact a provider twice under one attempt record.
    """

    def __init__(self, *, before_launch: Callable[[Mapping[str, object]], None],
                 on_spawn: Callable[[object], None] | None = None,
                 on_reap: Callable[[object], None] | None = None,
                 on_resource: Callable[[object, str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 deadline_reached: Callable[[], bool] | None = None,
                 allow_secondary: bool = False) -> None:
        if not callable(before_launch):
            raise TypeError("before_launch must be callable")
        self._before_launch = before_launch
        self._on_spawn = on_spawn
        self._on_reap = on_reap
        self._on_resource = on_resource
        self._cancelled = cancelled or (lambda: False)
        self._deadline_reached = deadline_reached or (lambda: False)
        self.allow_secondary = bool(allow_secondary)
        self._lock = threading.Lock()
        self._claimed = False

    def is_cancelled(self) -> bool:
        try:
            return bool(self._cancelled())
        except Exception as exc:  # fail closed: a broken cancellation source is unsafe
            raise ProviderLaunchError(
                f"provider cancellation check failed: {type(exc).__name__}") from exc

    def is_deadline_reached(self) -> bool:
        try:
            return bool(self._deadline_reached())
        except Exception as exc:
            raise ProviderDeadlineError(
                f"provider deadline check failed: {type(exc).__name__}") from exc

    def before_provider_launch(self, evidence: Mapping[str, object]) -> None:
        """Claim this attempt and run its durable acknowledgement callback."""
        with self._lock:
            if self._claimed:
                raise ProviderLaunchError("provider launch control is single-use")
            if self.is_cancelled():
                raise ProviderLaunchError("provider launch cancelled before contact")
            if self.is_deadline_reached():
                raise ProviderDeadlineError("provider launch deadline exceeded")
            self._claimed = True
        # Keep the callback outside the lock.  The claim remains consumed if it
        # fails, which prevents a second path from retrying an ambiguous attempt.
        self._before_launch(dict(evidence))

    def spawned(self, handle: object) -> None:
        if self._on_spawn is not None:
            self._on_spawn(handle)

    def reaped(self, handle: object) -> None:
        if self._on_reap is not None:
            self._on_reap(handle)

    def register_resource(self, resource: object, kind: str = "provider-resource") -> None:
        """Register a builder-created disposable resource before provider contact.

        This is deliberately an internal callback, not journal data.  Controlled
        deliberation adapters use it for per-invocation credential profiles that
        builders create before the final provider boundary.  Ordinary dispatches
        never construct a control and therefore never enter this path.
        """
        if self._on_resource is None:
            raise ProviderLaunchError("controlled resource registration is unavailable")
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise ProviderLaunchError("controlled resource kind is invalid")
        # Keep the callback outside the launch lock: registration may perform
        # filesystem validation, but the one-launch claim remains independent.
        self._on_resource(resource, kind)

# CLI transcripts are model/provider-controlled and can echo credentials from a
# failed request or a tool response.  Keep this deliberately narrow: redact a
# named secret assignment, not ordinary prose that merely contains the word
# "token".  It covers JSON (`"api_key":"..."`) and common terminal forms
# (`Authorization: Bearer ...`) before either an envelope or debug log persists.
_SENSITIVE_OUTPUT_RE = re.compile(
    r"(?im)(\b(?:api[_-]?key|access[_-]?token|oauth[_-]?token|auth[_-]?token|"
    r"private[_-]?key|password|secret|authorization)\b\s*(?:=\s*|:\s*(?:bearer\s+)?"
    r"|[\"']?\s*:\s*[\"']?))([^\s,}\]\\\"']+)")


def _redact_output_secrets(text: str) -> str:
    """Redact named credentials from backend output before it becomes an artifact."""
    if not isinstance(text, str):
        return text
    return _SENSITIVE_OUTPUT_RE.sub(lambda m: m.group(1) + "<redacted>", text)


def _sensitive_arg_key(text: str) -> str:
    """Normalize a possible `key` (from `key=value` or a bare flag) for matching."""
    return text.lower().lstrip("-").replace("_", "-")


def _sanitize_argv(argv: list) -> str:
    """Redact likely secrets in debug argv; keep the rest for reproducibility.

    Two passes.  Per token, a secret is redacted only when the sensitive name is
    the WHOLE key of `key=value` or the whole flag of `--flag value` -- a prompt
    token that merely mentions `password=` keeps its text.  Then the joined line
    gets the same named-assignment redaction as backend output, so a credential
    embedded in a larger token (e.g. a JSON blob) is still caught.
    """
    out = []
    redact_next = False
    for arg in argv:
        text = str(arg)
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        key, sep, _ = text.partition("=")
        if _sensitive_arg_key(key) in _SENSITIVE_ARG_KEYS:
            if sep:
                out.append(f"{key}=<redacted>")
            else:
                out.append(text)
                redact_next = True
            continue
        # Keep both ends of a long argument. System-context-first backends put the user's
        # actual prompt at the tail; preserving only the prefix made --dry-run conceal it
        # after a universal operating contract was added. Redaction still runs on the joined
        # preview below, so a named secret in either retained segment is not exposed.
        if len(text) <= 2000:
            out.append(text)
        else:
            out.append(text[:1400] + "...[truncated]..." + text[-600:])
    return _redact_output_secrets(" ".join(out))

from _builder import (AgentInvocation, BACKENDS, advisory_warnings,
                      argv_length_error, agy_permission_warning,
                      readonly_unenforceable_error,
                      agy_readonly_workspace_warning, agy_timeout_warning,
                      apply_credit_guard, backend_kind, build_invocation_args,
                      model_backend_compatibility,
                      credit_spend_allowed, infer_dispatch_billing, permission_flags,
                      selects_credit_only)
from _stream import StreamProcessor, _terminal_is_error

# SIGTERM, as Popen reports it (-15) and as a shell wrapper reports it (128+15).
_SIGTERM_EXIT_CODES = (143, -15)
# Codes that do NOT contradict a success. SIGTERM belongs here only because summon itself
# calls process.terminate() once a terminal event has been parsed -- see build_final_response,
# where SIGTERM counts as success ONLY on the branch that HAS that terminal event.
_SUCCESS_EXIT_CODES = (0,) + _SIGTERM_EXIT_CODES

# The report contract's bookend fields — present in every agent definition.
_REPORT_BOOKENDS = ("STATUS", "SUMMARY", "FOLLOW-UP", "HANDOFF")
# Known contract field names across all agent definitions. ONLY these start a new
# field — so a continuation line that happens to begin "NOTE:"/"TODO:"/"HTTP://"
# stays part of the current value instead of silently splitting it (which would
# truncate HANDOFF, the field carried into the next call).
_REPORT_FIELDS = frozenset({
    "STATUS", "SUMMARY", "COMMANDS", "VERIFICATION", "FOLLOW-UP", "HANDOFF",
    "LEFT_BEHIND",
    "FINDINGS", "VERDICT", "PLAN", "RISKS", "DESIGN", "TRADE_OFFS",
    "HYPOTHESES_TESTED", "ROOT_CAUSE", "CHANGES", "DESIGN_NOTES", "TESTS",
    "EDITS", "TONE_CHANGES", "DOCS", "EVIDENCE", "CONFIDENCE", "ANALYSIS",
    "PR_TITLE", "PR_BODY",
})
# Valid first token of a real STATUS value — used to anchor on a genuine block
# rather than a quoted "STATUS: DONE | PARTIAL | BLOCKED" contract example.
_STATUS_VALUES = frozenset({"DONE", "PARTIAL", "BLOCKED", "SUCCESS", "ERROR"})
_REPORT_FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_-]{1,}):[ \t]?(.*)$")
# Markdown-rendered backends (agy above all) bold the contract field names, with
# the colon landing INSIDE or OUTSIDE the wrapper: `**STATUS:** v` / `**STATUS**: v`.
_BOLD_FIELD_PREFIX = re.compile(r"^\*\*([A-Z][A-Z0-9_-]{1,})(?::\*\*|\*\*:)")
# Some providers render a contract field as a Markdown heading.  Accept only a
# bounded, line-start heading prefix; arbitrary `VERDICT:` prose must remain
# non-structural.  The required whitespace after `#` is intentional: it keeps
# shell comments and prompt text from becoming report fields accidentally.
_HEADING_FIELD_PREFIX = re.compile(r"^\s{0,3}#{1,6}[ \t]+")


def _unbold_field_line(line: str) -> str:
    """Normalize one structured report line without broadening prose matching.

    A provider may emit `**STATUS:** DONE` or `## VERDICT: BLOCK`.  Both are
    accepted only at the beginning of a line and only the wrapper is removed;
    field-name and status validation still happens in :func:`parse_report`.
    """
    normalized = _HEADING_FIELD_PREFIX.sub("", line, count=1)
    return _BOLD_FIELD_PREFIX.sub(lambda m: m.group(1) + ":", normalized, count=1)
# A line begins a NEW field when its key is either a known field OR a well-formed
# all-caps identifier (letters/digits/underscore, 2-30 chars) — so a third-party
# agent's CUSTOM field (SCORE:, RUBRIC:, ...) is captured, not silently folded
# into the previous value (which corrupts HANDOFF). Excludes hyphens/`://` so a
# stray `http://` or a lowercase narration line stays part of the current value.
_CUSTOM_FIELD_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")


def _is_field_key(key: str) -> bool:
    return key in _REPORT_FIELDS or bool(_CUSTOM_FIELD_RE.fullmatch(key))

# Approval-request phrasings the backend CLIs emit when a sandboxed tool call
# needs interactive consent. In one-shot mode a sub-agent that ENDS on one of
# these did not complete its task, even though the CLI exits 0 — the envelope
# must not report success. Tail-scanned (last _BLOCKED_TAIL chars) so a run
# that merely *mentions* approvals mid-result doesn't false-positive.
_BLOCKED_MARKERS = (
    "tool call was blocked",
    "tool use was blocked",
    "please approve",
    "requires approval",
    "approval required",
    "waiting for approval",
    "needs your approval",
    "permission to use",
    "requested permissions",
    "permission prompt",
    "grant permission",
    # gemini/cursor phrasing variants
    "confirmation required",
    "requires confirmation",
    "waiting for your confirmation",
    "needs your confirmation",
)
_BLOCKED_TAIL = 800

# Envelope reconciliation: a structured self-report is AUTHORITATIVE over a
# raw exit-0 "success". An agent that ends with STATUS: BLOCKED followed the
# contract — the envelope must not contradict it (that would be the silent-
# success leak again, on the MOST compliant path). Only ever downgrades.
_REPORT_TO_ENVELOPE = {"BLOCKED": "blocked", "PARTIAL": "partial", "ERROR": "error"}
_REVIEW_VERDICTS = {
    "BLOCK": "block", "DENY": "block",
    "CONCERNS": "conditional", "CONDITIONAL": "conditional", "UNCERTAIN": "conditional",
    "CLEAN": "pass", "PASS": "pass", "APPROVE": "pass",
}

# Envelope schema version — bumped only on a breaking change to the response
# shape, so an orchestrator can branch on it. Adding fields does NOT bump it.
ENVELOPE_VERSION = 1


def _detect_blocked(text: str) -> list:
    """Approval markers present in the TAIL of the result (case-insensitive)."""
    tail = (text or "")[-_BLOCKED_TAIL:].lower()
    return [m for m in _BLOCKED_MARKERS if m in tail]


def _review_verdict(report: dict | None) -> str | None:
    """Normalize a review decision without conflating it with execution state."""
    raw = report.get("verdict") if isinstance(report, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    first = raw.strip().split()[0].rstrip("|,:;-").upper()
    return _REVIEW_VERDICTS.get(first)


def parse_report(text: str) -> dict | None:
    """Extract the trailing report-contract block from an agent's result text.

    Anchors on the LAST ``STATUS:`` line whose value begins with a real status
    token (DONE/PARTIAL/BLOCKED/...) — so a quoted contract example or narration
    that merely mentions ``STATUS:`` can't spoof or displace the real block. A line
    begins a new field when its key is a known field OR a well-formed all-caps
    identifier (so third-party agents' custom fields are captured, not folded);
    any other line continues the current value (multi-line safe). Keys are
    lowercased with ``-`` mapped to ``_`` (e.g. ``follow_up``). Markdown-bold
    field names (``**STATUS:**``) are accepted — backends that render markdown
    wrap the contract typographically; the status-value and template guards
    apply unchanged after the wrapper is stripped.

    Returns None when no genuine ``STATUS:`` line exists.
    """
    if not text:
        return None
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        anchor = _unbold_field_line(lines[i])
        if anchor.startswith("STATUS:"):
            value = anchor[len("STATUS:"):].strip()
            first = value.split()[0].rstrip("|,").upper() if value else ""
            # Skip ONLY the echoed contract TEMPLATE ("DONE | PARTIAL | BLOCKED"):
            # a value whose pipe-separated tokens are ALL status keywords. A real
            # status like "BLOCKED | waiting on approval" is kept (its second token
            # isn't a status word), so the guard can't swallow a genuine block.
            if first in _STATUS_VALUES:
                parts = [p.strip().split()[0].upper() for p in value.split("|") if p.strip()]
                is_template = len(parts) > 1 and all(p in _STATUS_VALUES for p in parts)
                if not is_template:
                    start = i
                    break
    if start is None:
        return None

    fields: dict = {}
    current_key = None
    for line in lines[start:]:
        m = _REPORT_FIELD_RE.match(_unbold_field_line(line))
        if m and _is_field_key(m.group(1)):
            current_key = m.group(1).lower().replace("-", "_")
            fields[current_key] = m.group(2).strip()
        elif current_key is not None:
            fields[current_key] = (fields[current_key] + "\n" + line).strip()
    return fields or None


def normalize_empty_success(response: dict) -> dict:
    """Fail closed on a protocol success that contains no usable result.

    Provider transports may classify a turn as complete from a stop reason or
    process exit alone.  Summon cannot treat that as completed work because the
    public result (and any parsed schema value) is derived from ``result``.
    The operation is idempotent so a backend can apply it at its protocol
    boundary and the executor can apply it again as a contract firewall.
    """
    if not isinstance(response, dict):
        return response
    result = response.get("result")
    if (response.get("status") != "success"
            or (isinstance(result, str) and result.strip())):
        return response
    if "backend_exit_code" not in response:
        response["backend_exit_code"] = response.get("exit_code")
    response["status"] = "error"
    response["exit_code"] = 1
    response["dispatcher_status"] = "error"
    response["normalization_reason"] = (
        "empty terminal result cannot be accepted as a completed Summon result")
    response["error_kind"] = "empty_terminal_result"
    response["retryable"] = False
    response["suspect"] = True
    response.setdefault(
        "error",
        "provider reported success but returned no usable result")
    return response


def _enrich(response: dict, processor: StreamProcessor | None) -> dict:
    """Attach telemetry + parsed report to a response (all return paths).

    Adds: ``session_id``, ``usage``, ``cost_usd`` (from stream events; None
    where the backend doesn't emit them), ``report`` (parsed contract block or
    None), ``environment_handoff`` (whether the agent declared retained resources),
    ``report_ok`` (all bookend fields present), and ``suspect: true``
    when a run claims success but the contract block is missing/incomplete.

    One exception to "the parser never changes status": a run whose result ENDS
    on an interactive-approval request (see ``_BLOCKED_MARKERS``) with no report
    contract is downgraded from ``success`` to ``blocked`` — the CLI exited 0,
    but in one-shot mode nobody is there to click approve, so the task did not
    happen. An orchestrator trusting ``status`` must not collect that as a win.
    """
    # Normalize malformed provider completions before recording the execution
    # status.  That keeps execution_status, dispatcher_status, and the public
    # exit tuple aligned even when a backend only reported a protocol-level
    # success (for example ACP end_turn) without a usable result.
    normalize_empty_success(response)
    finalize_exit_fields(response)
    response["envelope"] = ENVELOPE_VERSION
    # Preserve the executor's outcome BEFORE report semantics reconcile the public
    # status. A successful review can legitimately return VERDICT: BLOCK; a failed
    # execution cannot. Keeping the two signals separate lets callers branch correctly.
    # Structural refusals are represented as status=error/blocked for compatibility,
    # but they never entered a provider turn. Preserve their explicit not-run state
    # while normal executions continue to mirror the terminal dispatcher status.
    if not (response.get("attempt_status") == "not_run"
            and response.get("provider_contacted") is False):
        response["execution_status"] = response.get("status")
    # setdefault (not =) so a non-stream backend (openai-compat) that already
    # populated these from its HTTP response isn't clobbered with None.
    response.setdefault("session_id", processor.session_id if processor else None)
    response.setdefault("usage", processor.usage if processor else None)
    response.setdefault("cost_usd", processor.cost_usd if processor else None)
    response.setdefault("model_resolved", processor.model if processor else None)
    response.setdefault("model_targeted", processor.handshake_model if processor else None)
    response.setdefault("models_used", processor.models_used if processor else [])
    response.setdefault(
        "model_evidence_source",
        getattr(processor, "model_evidence_source", None) if processor else None,
    )
    # Kimi has no terminal-success event. If the dispatcher deadline wins
    # after assistant text was captured, retain a bounded diagnostic snapshot
    # in its own namespace. It is deliberately never fed to parse_report,
    # result promotion, resume, caching, or model provenance.
    if (processor and processor.is_kimi and response.get("timeout")
            and not (response.get("result") or "").strip()):
        partial = processor.kimi_partial_snapshot()
        if partial:
            partial["text"] = _redact_output_secrets(partial["text"])
            response["partial"] = partial
            response["timeout"]["partial_output"] = True
            response["partial_output_only"] = True
            response.setdefault("warnings", []).append(
                "Kimi emitted assistant text before the deadline but no clean EOF; "
                "the bounded `partial` snapshot is advisory and is not a completed report")
    if processor and processor.is_opencode:
        # OpenCode's JSON CLI normally emits step_finish before idle/EOF.  Some
        # releases can dispose the non-interactive server while the model is
        # still working, leaving only a progress sentence.  Preserve the clean
        # process status for diagnosis, but make the missing completion evidence
        # explicit so a review/council cannot consume it as a verdict.
        response["opencode_stream"] = {
            "event_count": processor.opencode_event_count,
            "step_finish_seen": bool(processor.opencode_step_finish_seen),
            "completion_evidence": (
                "step_finish" if processor.opencode_step_finish_seen
                else "clean_eof_without_step_finish"),
        }
        if processor.opencode_finish_reason is not None:
            response["opencode_stream"]["finish_reason"] = (
                processor.opencode_finish_reason)
        if processor.opencode_step_finish_seen:
            response["opencode_stream"]["zero_output_finish"] = bool(
                processor.opencode_zero_output_finish)
            response["opencode_stream"]["zero_token_finish"] = bool(
                processor.opencode_zero_token_finish)
        # OpenCode 1.18.x has a known headless failure shape: it emits
        # step_finish(reason=unknown) with zero usage and no text, then exits 0
        # without an error event. Keep the stable empty_terminal_result kind,
        # but make the actionable upstream/provider symptom explicit instead of
        # attributing it to Summon's permission mode or treating it as a verdict.
        if (response.get("error_kind") == "empty_terminal_result"
                and processor.opencode_finish_reason == "unknown"
                and processor.opencode_zero_token_finish):
            response["opencode_diagnostic"] = "unknown_finish_zero_tokens"
            response["error"] = (
                "OpenCode reported finish reason 'unknown' with zero tokens and "
                "no usable output; this is a provider/model no-output completion, "
                "not a permission approval result")
            response["normalization_reason"] = (
                "OpenCode emitted step_finish(reason=unknown) with zero tokens; "
                "Summon rejected the empty completion")
    # Baseline resume handle on EVERY path (incl. spawn-failure) so orchestrators
    # can read response["resume"] unconditionally. execute_agent enriches it with
    # the agy profile on the normal path.
    response.setdefault("resume", {"cli": response.get("cli"), "session_id": response.get("session_id")})
    report = parse_report(response.get("result") or "")
    # Report values are extracted before diagnostics redact ``result``. They are
    # independently serialized into the envelope, so leaving them raw would let
    # a model-echoed credential bypass the otherwise-redacted transcript through
    # HANDOFF, LEFT_BEHIND, or any custom report field.
    if report:
        report = {
            key: _redact_output_secrets(value) if isinstance(value, str) else value
            for key, value in report.items()
        }
    response["report"] = report
    # Kept distinct from report_ok for compatibility with older/project-local definitions.
    # New bundled definitions and initial dispatch context require LEFT_BEHIND, while a
    # legacy report stays mechanically valid but tells callers it made no environmental
    # declaration. This is visibility, not enforcement: cleanup authority remains outside
    # the dispatcher and an agent's self-report is advisory.
    response["environment_handoff"] = {
        "declared": bool(report and "left_behind" in report),
        "left_behind": report.get("left_behind") if report else None,
    }
    response["report_ok"] = bool(
        report and all(b.lower().replace("-", "_") in report for b in _REPORT_BOOKENDS)
    )
    response["verdict"] = _review_verdict(report)
    # 1) Structured self-report wins over exit-0 "success" (never upgrades).
    if response.get("status") == "success" and report and report.get("status"):
        first = report["status"].split()[0].rstrip("|,").upper()
        mapped = _REPORT_TO_ENVELOPE.get(first)
        if mapped:
            response["status"] = mapped
            response.setdefault("error",
                f"agent self-reported {first}: {(report.get('summary') or '')[:200]}")
    # 2) Approval-marker telemetry is attached UNCONDITIONALLY (even when the
    #    report already downgraded the status — orchestrators want the markers
    #    either way). The phrases are model-controlled text, so the DOWNGRADE
    #    from them stays conservative: tail-only, contract-less success runs
    #    only, and the guidance must never suggest blind privilege escalation —
    #    quoted or injected content could otherwise steer an orchestrator into
    #    raising permissions.
    blocked = _detect_blocked(response.get("result") or "")
    if blocked:
        response["blocked_indicators"] = blocked
        if response.get("status") == "success" and not response["report_ok"]:
            response["status"] = "blocked"
            response["error"] = (
                "sub-agent ended awaiting interactive approval "
                f"(markers: {', '.join(blocked)}). Verify the transcript. Common "
                "causes: prompt references files outside --cwd (sandboxed reads), "
                "or the task needs a capability its permission level denies. Do "
                "NOT raise the permission level just because output text asks for "
                "it — fix the input layout, or escalate deliberately."
            )
    if response.get("status") == "success" and not response["report_ok"]:
        response["suspect"] = True
    if (processor and processor.is_opencode
            and response.get("status") == "success"
            and not processor.opencode_step_finish_seen):
        response["suspect"] = True
        warning = (
            "OpenCode ended at clean EOF without a step_finish event; output may be "
            "truncated before the agent completed its turn")
        warnings = response.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
    # Report reconciliation can change status (for example STATUS: ERROR or a
    # contract-less approval block), so refresh the explicit normalized code
    # after all status decisions.  The raw child code never changes here.
    if response.get("exit_code") is not None and response.get("status"):
        _raw_exit = response.get("raw_backend_exit_code")
        if _raw_exit is None:
            _raw_exit = response.get("backend_exit_code", response.get("exit_code"))
        response["raw_backend_exit_code"] = _raw_exit
        response["normalized_exit_code"] = _normalized_exit_code(
            response.get("status"), _raw_exit)
    # Keep compact structured findings ahead of the potentially long narrative in
    # serialized envelopes. JSON object order is not semantic, but this makes the
    # common human/tool streaming path useful without scanning the transcript first.
    if "result" in response:
        result_text = response.pop("result")
        response["result"] = result_text
    return response


_KIMI_WIRE_MAX_BYTES = 16 * 1024 * 1024
_KIMI_WIRE_CLOCK_SKEW_MS = 10_000
_KIMI_WIRE_MAX_COUNTER = (1 << 63) - 1


def _regular_file_identity(value: os.stat_result) -> tuple[int, int] | None:
    """Return a stable local-file identity, or fail closed when unavailable."""
    if not stat.S_ISREG(value.st_mode):
        return None
    attrs = getattr(value, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return None
    dev = getattr(value, "st_dev", None)
    ino = getattr(value, "st_ino", None)
    links = getattr(value, "st_nlink", 1)
    if (type(dev) is not int or type(ino) is not int or ino <= 0
            or type(links) is not int or links != 1):
        return None
    return dev, ino


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _capture_kimi_wire_model(profile: str | None, *, started_wall_ms: int,
                             ended_wall_ms: int) -> dict | None:
    """Read bounded post-response model accounting from one isolated Kimi run.

    Kimi 0.38's public ``stream-json`` output omits model identity, but the CLI
    writes a private ``usage.record`` after each completed LLM response and a
    terminal ``turn.ended`` record into the fresh per-invocation profile Summon
    created.  A request record is deliberately insufficient: this helper only
    accepts positive-output usage records followed by ``turn.ended: completed``.

    The helper returns metadata only.  It never returns journal text, paths,
    prompts, tool calls, or credentials.  Malformed, oversized, linked, stale,
    mixed-model, or incomplete journals fail closed as no evidence.
    """
    if not isinstance(profile, str) or not profile:
        return None
    def _linklike(path: str) -> bool:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(callable(isjunction) and isjunction(path))

    root = os.path.realpath(profile)
    if not os.path.isdir(root) or _linklike(profile):
        return None
    # The disposable profile is child-writable. Never recursively walk whatever
    # tree the child chose to create: that turns one response into an unbounded
    # filesystem scan. Kimi's contract has one fixed shape, so inspect only
    # ``sessions/<session>/agents/main/wire.jsonl`` with strict entry caps.
    candidates: list[str] = []
    sessions = os.path.join(root, "sessions")
    try:
        if not os.path.isdir(sessions) or _linklike(sessions):
            return None
        with os.scandir(sessions) as entries:
            session_entries = []
            for entry in entries:
                if len(session_entries) >= 64:
                    return None
                if entry.is_dir(follow_symlinks=False) and not _linklike(entry.path):
                    session_entries.append(entry)
        for entry in session_entries:
            path = os.path.join(entry.path, "agents", "main", "wire.jsonl")
            if os.path.isfile(path) and not _linklike(path):
                candidates.append(path)
                if len(candidates) > 8:
                    return None
    except OSError:
        return None
    if len(candidates) != 1:
        return None

    path = candidates[0]
    try:
        real = os.path.realpath(path)
        if (os.path.commonpath((root, real)) != root or _linklike(path)
                or not os.path.isfile(path)):
            return None
        before_stat = os.stat(path, follow_symlinks=False)
        before_identity = _regular_file_identity(before_stat)
        if before_identity is None:
            return None
        size = before_stat.st_size
        if size <= 0 or size > _KIMI_WIRE_MAX_BYTES:
            return None
    except (OSError, ValueError):
        return None

    models: list[str] = []
    completed_at: int | None = None
    completed_index: int | None = None
    last_usage_at: int | None = None
    last_usage_index: int | None = None
    usage_count = 0
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
            fd = None  # owned by ``handle`` from here onward
            opened_stat = os.fstat(handle.fileno())
            opened_identity = _regular_file_identity(opened_stat)
            path_open_stat = os.stat(path, follow_symlinks=False)
            if (opened_identity is None or opened_identity != before_identity
                    or _regular_file_identity(path_open_stat) != before_identity
                    or _linklike(path)
                    or os.path.realpath(path) != real
                    or opened_stat.st_size != before_stat.st_size
                    or opened_stat.st_mtime_ns != before_stat.st_mtime_ns):
                return None
            for record_index, raw in enumerate(handle):
                if len(raw) > 1024 * 1024:
                    return None
                try:
                    event = json.loads(
                        raw,
                        parse_constant=_reject_json_constant,
                        object_pairs_hook=_unique_json_object,
                    )
                except (json.JSONDecodeError, UnicodeError, ValueError, OverflowError):
                    return None
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                event_time = event.get("time")
                if event_type not in ("usage.record", "turn.ended"):
                    continue
                if (type(event_time) is not int or event_time < 0
                        or event_time > _KIMI_WIRE_MAX_COUNTER):
                    return None
                event_ms = event_time
                if (event_ms < started_wall_ms - _KIMI_WIRE_CLOCK_SKEW_MS
                        or event_ms > ended_wall_ms + _KIMI_WIRE_CLOCK_SKEW_MS):
                    continue
                if event_type == "usage.record":
                    if event.get("usageScope") != "turn":
                        continue
                    usage = event.get("usage")
                    output = usage.get("output") if isinstance(usage, dict) else None
                    model = _safe_model_id(event.get("model"))
                    if (not model or type(output) is not int or output < 0
                            or output > _KIMI_WIRE_MAX_COUNTER):
                        return None
                    if output > 0:
                        models.append(model)
                        usage_count += 1
                        last_usage_at = event_ms
                        last_usage_index = record_index
                elif event.get("reason") == "completed":
                    completed_at = event_ms
                    completed_index = record_index
            after_stat = os.fstat(handle.fileno())
            path_after_stat = os.stat(path, follow_symlinks=False)
            if (_regular_file_identity(after_stat) != before_identity
                    or _regular_file_identity(path_after_stat) != before_identity
                    or _linklike(path)
                    or os.path.realpath(path) != real
                    or after_stat.st_size != opened_stat.st_size
                    or after_stat.st_mtime_ns != opened_stat.st_mtime_ns
                    or path_after_stat.st_size != opened_stat.st_size
                    or path_after_stat.st_mtime_ns != opened_stat.st_mtime_ns):
                return None
    except (OSError, UnicodeError, ValueError, OverflowError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    if (not models or completed_at is None or completed_index is None
            or last_usage_at is None or last_usage_index is None
            or completed_index <= last_usage_index
            or completed_at < last_usage_at):
        return None
    unique = sorted(set(models))
    if len(unique) != 1:
        return None
    # The completion marker must follow the last response-accounting record.
    return {
        "model": unique[0],
        "models_used": unique,
        "usage_records": usage_count,
        "turn_completed": True,
        "source": "kimi_wire_usage_record",
    }


# Payload elision + startup-noise filtering for the human-facing output_tail.
# The debug transcript retains more context than the tail, but both are secret-
# redacted.  The tail additionally elides large binary/base64 blobs and startup
# noise so a failure stays diagnosable without a re-run.
_DEFAULT_MAX_TOOL_OUTPUT_BYTES = 2048

# base64 AND base64url alphabets (+/ and -_) plus '=' padding. A linear scan over
# this set (not a regex {N,} quantifier) means no OverflowError on a huge
# threshold and no super-linear CPU on a near-threshold run.
_B64_SCAN = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_=")
# A full base64 data: URI: the `data:` SCHEME is required (so prose that merely
# contains ";base64," is not falsely elided), at a LEFT WORD BOUNDARY (so the
# `data:` inside `metadata:` does not match), extra parameters are tolerated,
# whitespace between the comma and the payload is skipped (so it can't smuggle a
# blob past detection), and the payload run is captured. `[A-Za-z0-9+/_=-]+` is a
# plain char class with `+` (linear, no ReDoS / no {N,} OverflowError).
_DATA_URI_RE = re.compile(
    r"(?i)(?<![\w-])data:([\w.+-]*/[\w.+-]+)?(?:;[\w.+-]+=[^;,\s]*)*;base64,\s*([A-Za-z0-9+/_=-]+)")
# Provider startup noise: ONLY the unambiguously non-task skill-loader notices are
# stripped. Generic PowerShell error frames (ParserError, `At line:`, CategoryInfo,
# `at ...ps1:`) are NOT matched -- they are indistinguishable from a real TASK error
# and stripping them would delete a genuine diagnostic (the whole point of the tail).
_STARTUP_NOISE_RE = re.compile(
    r"(?i)^\s*(?:duplicate skill\b|skill\s+\S+\s+already\s+(?:registered|loaded)\b)")


def _blob_marker(mime, blob: str) -> str:
    import hashlib
    digest = hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]
    return f"[payload omitted: {mime or 'base64'}, {len(blob)} bytes, sha256 {digest}]"


def _elide_payloads(raw: str, thresh: int) -> str:
    """Replace base64 payloads with a bounded marker. Two phases: (1) real data:
    URIs are ALWAYS elided regardless of length (they require the `data:` scheme,
    so prose containing ";base64," is safe); (2) any remaining BARE base64/base64url
    run of length >= ``thresh`` is elided by a linear scan (no regex quantifier)."""
    text = _DATA_URI_RE.sub(lambda m: _blob_marker(m.group(1), m.group(2)), raw)
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] in _B64_SCAN:
            j = i
            while j < n and text[j] in _B64_SCAN:
                j += 1
            run = text[i:j]
            out.append(_blob_marker(None, run) if j - i >= thresh else run)
            i = j
        else:
            j = i
            while j < n and text[j] not in _B64_SCAN:
                j += 1
            out.append(text[i:j])
            i = j
    return "".join(out)


def _strip_startup_noise(text: str, debug_available: bool = False) -> str:
    """Collapse runs of KNOWN provider startup-noise lines into one marker.
    Conservative by design: only lines matching an anchored, unambiguous startup
    format are dropped, so a real provider error is never removed. The marker
    points at debug_file only when one was actually created."""
    tail = (" see debug_file for the full transcript" if debug_available
            else " (re-run with --debug-dir to capture the full transcript)")
    out, suppressed = [], 0
    for ln in text.splitlines():
        if _STARTUP_NOISE_RE.search(ln):
            suppressed += 1
            continue
        if suppressed:
            out.append(f"[{suppressed} line(s) of provider startup noise suppressed;{tail}]")
            suppressed = 0
        out.append(ln)
    if suppressed:
        out.append(f"[{suppressed} line(s) of provider startup noise suppressed;{tail}]")
    return "\n".join(out)


def _sanitize_tail(raw: str, max_blob_bytes: int | None = None,
                   debug_available: bool = False) -> str:
    """Elide binary/base64 payloads (data: URIs and bare base64/base64url runs at
    or above ``max_blob_bytes``) into bounded markers, then strip provider startup
    noise. Never touches the full transcript kept for --debug-dir."""
    if not raw:
        return raw
    # Clamp to the length of the text actually being scanned: you cannot ask to
    # keep a run "longer than everything captured". This also closes the leak
    # where the tail is derived from a truncated _debug_raw window -- a run that
    # fills the whole window is always elided rather than slipping under a
    # threshold set above the window size.
    thresh = max(64, min(int(max_blob_bytes or _DEFAULT_MAX_TOOL_OUTPUT_BYTES), len(raw)))
    return _strip_startup_noise(_elide_payloads(raw, thresh), debug_available)


def _attach_eligibility(resp: dict) -> dict:
    """Account/client-eligibility failures (e.g. Gemini IneligibleTierError) look
    like a generic error; recognize the known signatures in a NON-success envelope
    and attach concrete migration guidance (an `eligibility` field + a warning) so
    the caller is never left guessing. Lazy import avoids a module cycle (_doctor's
    live probe imports _executor). Advisory only, never fatal."""
    if not isinstance(resp, dict) or resp.get("status") == "success":
        return resp
    # Include `result` too (some backends put the failure text there, not in
    # error/output_tail), and BIND to this dispatch's actual backend so a gemini
    # tier signature can't be attributed to a codex/claude envelope that merely
    # echoed the phrase.
    text = (f"{resp.get('error') or ''} {resp.get('output_tail') or ''} "
            f"{resp.get('result') or ''}")
    # Provider/auth evidence wins over a model-authored missing-tool phrase.
    # Otherwise a real 401 plus `grep not found` can be relabelled as a local
    # tool problem and hide the correct login remediation.
    try:
        from _doctor import classify_ineligibility
        verdict = classify_ineligibility(text, backend=resp.get("cli"))
    except Exception:  # noqa: BLE001
        verdict = None
    tool_failure = missing_executable_details(text) if verdict is None else None
    if tool_failure is not None:
        resp["tool_failure"] = tool_failure
        resp["error_kind"] = "missing_executable"
        resp["retryable"] = False
        resp["result_usable"] = False
        resp["interactive_required"] = False
        resp["remediation_code"] = "execution_tool_missing"
        resp.setdefault("warnings", []).append(
            f"child environment could not run '{tool_failure['executable']}'; "
            "this is not provider authentication. Use rg, PowerShell, or Python "
            "and retry explicitly after confirming the required tool path.")
    if verdict is not None:
        resp["eligibility"] = verdict
        if verdict["kind"] == "auth":
            # Authentication failures are actionable but must never trigger a
            # silent provider retry. The safe repair plan tells an agent or
            # operator exactly how to recover, while the explicit auth command
            # remains opt-in and may still require browser approval.
            resp["error_kind"] = "authentication_failed"
            resp["auth"] = {
                "backend": verdict["backend"],
                "state": "expired_or_invalid",
                "repair": verdict.get("repair"),
                "requires_user_approval": True,
                "retry_required": True,
                "retry_safe": False,
            }
            resp["retryable"] = False
            resp["result_usable"] = False
            if resp.get("attempt_status") != "not_run":
                resp["provider_contacted"] = True
            # Flat, bounded lifecycle fields are the telemetry contract.  The
            # nested auth plan remains user-facing data and is never projected
            # into the local spool.
            resp["auth_stage"] = "terminal"
            resp["auth_outcome"] = "login_required"
            resp["interactive_required"] = True
            resp["remediation_code"] = "provider_login_required"
            resp.setdefault("warnings", []).append(
                f"backend '{verdict['backend']}' authentication is unavailable: "
                f"{verdict['guidance']} Run the documented login command, complete any "
                "browser approval, then explicitly retry; Summon did not retry automatically.")
        else:
            resp.setdefault("warnings", []).append(
                f"backend '{verdict['backend']}' is not eligible: {verdict['guidance']}")
    # Kimi's CLI reports rate limiting with several vendor-specific spellings
    # rather than a stable JSON error object.  Mark it terminal so --retries and
    # ACP fallback cannot turn a transient-looking provider response into a
    # burst of duplicate paid calls.  The operator can retry explicitly after
    # the provider's window has elapsed.
    if (resp.get("cli") == "kimi" and resp.get("status") != "success"):
        low = text.lower()
        if any(marker in low for marker in (
                "too many requests", "rate limit", "rate_limited",
                "http 429", "status code 429", " 429 ", "quota exceeded")):
            resp["error_kind"] = "rate_limited"
            resp["retryable"] = False
            resp["result_usable"] = False
            if resp.get("attempt_status") != "not_run":
                resp["provider_contacted"] = True
            resp["auth_stage"] = "terminal"
            resp["auth_outcome"] = "not_needed"
            resp["interactive_required"] = False
            resp["remediation_code"] = "provider_quota_wait"
            resp.setdefault("warnings", []).append(
                "Kimi reported a rate limit; Summon did not retry or switch providers. "
                "Wait for the provider window to reset, then retry explicitly.")
    return resp


def _finalize_diagnostics(resp: dict, raw, debug_dir, debug_argv,
                          max_tool_output_bytes) -> dict:
    """Write the debug transcript (if requested) then sanitize output_tail, with
    the tail's ``debug_file`` reference reflecting whether the file was ACTUALLY
    written. The debug file keeps the full-length raw transcript (the tail is
    truncated and blob-elided), but EVERY artifact -- debug file included -- is
    secret-redacted first; a failed _write_debug returns None so the tail
    advises --debug-dir instead of naming a nonexistent file. Extracted from
    _stamp so the wiring is testable."""
    raw = _redact_output_secrets(raw or "")
    for key in ("result", "error", "error_hint", "output_tail"):
        if isinstance(resp.get(key), str):
            resp[key] = _redact_output_secrets(resp[key])
    partial = resp.get("partial")
    if isinstance(partial, dict) and isinstance(partial.get("text"), str):
        partial["text"] = _redact_output_secrets(partial["text"])
    dbg = _write_debug(debug_dir, debug_argv, raw, resp) if debug_dir else None
    if dbg:
        resp["debug_file"] = dbg
    if resp.get("output_tail") is not None and raw:
        resp["output_tail"] = _sanitize_tail(
            raw, max_tool_output_bytes, debug_available=bool(dbg))[-2000:]
    return resp


_CONTENT_SHA_CHUNK = 1024 * 1024
_CONTENT_SHA_TIMEOUT_S = 5.0


def content_state(path) -> tuple:
    """(sha256, state) for a path, where state is "absent" (nothing there -- legitimately
    not part of the request), "ok", or "unreadable" (it IS there but could not be hashed:
    a permissions error, a non-regular file, a read that failed or would not settle).

    The distinction is load-bearing. Reporting a failed hash as plain None let
    request_fingerprint DROP the field, so two DIFFERENT unhashable inputs produced the SAME
    fingerprint and one could be served as the answer to the other. An input that exists but
    cannot be identified must fail closed for reuse, not silently vanish from the identity.
    """
    if not path:
        return None, "absent"
    sha, why = _content_sha_ex(path)
    if sha:
        return sha, "ok"
    return None, ("absent" if why == "absent" else "unreadable")


def _content_sha_ex(path) -> tuple:
    """(sha256, reason). reason is None on success, else "absent" or "unreadable".

    Read SYNCHRONOUSLY and deliberately. An earlier version did this on a joinable daemon
    worker to bound a stalled network read -- but dispatch itself reads these very files
    (the agent definition in load_agent, the --json-schema with json.load, .agents/memory.md)
    with no timeout at all, so the worker protected only the resume CHECK against a hang the
    actual run is fully exposed to. That asymmetry bought nothing and cost a great deal: a
    worker pool, a starvation circuit breaker, and a nondeterministic fingerprint under
    concurrent load. What IS worth keeping is cheap and exact: O_NONBLOCK so a FIFO cannot
    block at open(), an S_ISREG gate on the HANDLE, chunked reads so memory is bounded by
    the chunk rather than the file, and a re-fstat so a file rewritten under us reports no
    identity instead of a hybrid digest.
    """
    import stat as _stat
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        before = os.fstat(fd)
        if not _stat.S_ISREG(before.st_mode):
            return None, "unreadable"
        deadline = time.monotonic() + _CONTENT_SHA_TIMEOUT_S
        h, read_total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, _CONTENT_SHA_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            read_total += len(chunk)
            # The deadline guards NON-TERMINATION (a file being appended to faster than we
            # read it), not slowness. Tripping on elapsed time alone called a perfectly
            # healthy 1 MiB file on a slow share "unreadable" and refused every resume
            # against it, so it only fires once we have read PAST the size the handle
            # reported -- which only a growing file can do.
            if time.monotonic() > deadline and read_total > before.st_size:
                return None, "unreadable"
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            # Rewritten under us: the digest could be a HYBRID of the old and new content,
            # matching neither. This catches ACCIDENTAL mutation (an editor saving mid-run),
            # not a deliberate adversary -- a same-length rewrite with the mtime restored
            # would slip through. That matches summon's trust model, where files under
            # --cwd are TRUSTED operator input, so this is robustness, not a tamper boundary.
            return None, "unreadable"
        return h.hexdigest(), None
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)


def content_sha(path) -> str | None:
    """sha256 of a file's bytes, or None when there is nothing hashable there. See
    content_state when the caller needs to tell "absent" from "present but unidentifiable"."""
    return _content_sha_ex(path)[0] if path else None


class _DefnSnapshot:
    """One load of an agent definition, so every identity field derived from it sees the
    SAME bytes. Scattered load_agent() calls across the identity builder let an A -> B -> A
    swap mid-construction produce a HYBRID identity -- the hash from A, the resolved backend
    from B -- which (among other things) turned agy attestation off for a real agy request.
    """
    __slots__ = ("tup", "fm", "sha", "state")

    def __init__(self, tup, fm, sha, state):
        self.tup, self.fm, self.sha, self.state = tup, fm, sha, state


def _fm_capability_text_only(fm) -> bool:
    """True when frontmatter declares ``capability: text-only`` (snapshot form)."""
    if not isinstance(fm, dict):
        return False
    try:
        from _text_seat import _capability_values
        return "text-only" in _capability_values(fm.get("capability"))
    except ImportError:
        return False


def _defn_snapshot(agents_dir_arg, cwd, agent_name, strict_agents_dir: bool = False):
    """Load the definition ONCE (for the whole identity), or None if there is no agent.

    state: "ok" (loaded), "missing" (no such file), "malformed" (present but the loader
    refuses it). `sha` is the hash of the SAME bytes `load_agent_snapshot` parsed the tuple
    and frontmatter from -- one read, so it fails closed (an unreadable definition yields no
    sha) and the dispatch's own ABA-safe last_parsed_sha matches it while the file is stable.
    `fm` is the frontmatter, for the endpoint field -- also from that one buffer.
    """
    from _loader import (AgentResolutionError, bundled_roster_dir, get_agents_dir,
                         load_agent_snapshot, validate_agent_name)
    if not agent_name:
        return None
    try:
        validate_agent_name(agent_name)
    except Exception:  # noqa: BLE001 — an unusable NAME is not an absent definition
        return _DefnSnapshot(None, {}, None, "malformed")
    try:
        agents_dir = get_agents_dir(agents_dir_arg, cwd)
    except Exception:  # noqa: BLE001
        return _DefnSnapshot(None, {}, None, "missing")
    try:
        # ONE read: the tuple, the frontmatter and the hash all come from the SAME byte
        # buffer, so no definition-derived field can see a different byte version -- the
        # invariant that closes the A->B->A hybrid, not merely "one load_agent call".
        tup, fm, sha = load_agent_snapshot(
            agents_dir, agent_name, strict_agents_dir=strict_agents_dir)
        return _DefnSnapshot(tup, fm or {}, sha, "ok" if sha else "unreadable")
    except AgentResolutionError:
        # Keep the strict governance miss distinct in the request identity so an old
        # success served by a fallback roster cannot be reused before the real dispatch
        # emits its machine-readable refusal.
        return _DefnSnapshot(None, {}, None, "strict-miss")
    except Exception:  # noqa: BLE001 — the dispatch surfaces the real error
        pass
    # a file under that name EXISTS but did not load -> malformed, not absent
    for d in (agents_dir, bundled_roster_dir()):
        for ext in (".md", ".txt"):
            try:
                if d and os.path.exists(os.path.join(d, f"{agent_name}{ext}")):
                    return _DefnSnapshot(None, {}, None, "malformed")
            except OSError:
                pass
    return _DefnSnapshot(None, {}, None, "missing")


def agent_def_state(agents_dir_arg, cwd, agent_name) -> tuple:
    """(sha256, state) for the agent definition a request will load.

    state is "ok" (hashed), "missing" (no such definition anywhere) or "malformed" (a file
    IS there but the loader refuses it -- undecodable bytes, a duplicate key, a near-miss
    key). NEITHER missing nor malformed is reusable: the definition is part of the request,
    so without it there is nothing to match against, and the dispatch is the only thing that
    can report the breakage. The two states are still distinguished because they are
    different facts and the caller says which one it hit.
    """
    if not agent_name:
        return None, "missing"
    from _loader import (bundled_roster_dir, get_agents_dir, load_agent,
                         validate_agent_name)
    try:
        validate_agent_name(agent_name)
    except Exception:  # noqa: BLE001 — an unusable NAME is not an absent definition
        # "missing" would let a legacy envelope be reused and never surface the loader's
        # own "Invalid agent name" -- a path-traversing name must reach the dispatch.
        return None, "malformed"
    try:
        agents_dir = get_agents_dir(agents_dir_arg, cwd)
    except Exception:  # noqa: BLE001 — resolution itself is best-effort here
        return None, "missing"
    try:
        _sha, _st = content_state(load_agent(agents_dir, agent_name)[3])
        # "ok" ONLY with a real digest. Returning (None, "ok") let the definition's hash drop
        # out of the fingerprint entirely -- so a definition edited from read-only to yolo
        # hashed the SAME as before and the stale answer stayed reusable. The security
        # relevant field must never fail open.
        return (_sha, "ok") if _sha else (None, "unreadable")
    except Exception:  # noqa: BLE001 — the dispatch itself surfaces the real error
        pass
    # A file under that name EXISTS but did not load -> malformed, not merely absent. Decided
    # by looking, not by sniffing the exception's message.
    for d in (agents_dir, bundled_roster_dir()):
        for ext in (".md", ".txt"):
            try:
                if d and os.path.exists(os.path.join(d, f"{agent_name}{ext}")):
                    return None, "malformed"
            except OSError:
                pass
    return None, "missing"


def agent_def_sha(agents_dir_arg, cwd, agent_name) -> str | None:
    """sha256 of the agent DEFINITION a request will actually load, or None if it cannot be
    resolved. Editing an agent's `model:`, `permission:` or body makes a stored result stale,
    and nothing else in the fingerprint notices: the path is unchanged and the roster
    directory may be unchanged too (a `SUB_AGENTS_DIR` pointed at a different tenant resolves
    the same relative name). Hashing the CONTENT rather than the directory is also the more
    correct identity -- two roster dirs holding the identical definition really are the same
    request. Resolution is the loader's own, called identically by the dispatcher and the
    manifest parent, and any failure (missing agent, malformed frontmatter) degrades to None
    rather than raising: this runs on the resume path, whose job is to answer a question, not
    to validate.
    """
    return agent_def_state(agents_dir_arg, cwd, agent_name)[0]


# Environment that CONFIGURES a backend, by prefix. The child inherits the environment, so
# these reach the vendor CLI directly and can redirect it to a different endpoint, account or
# model without any summon flag changing -- exactly the cross-tenant reuse the openai-compat
# credential fix closed, but for the CLI backends. Enumerating individual variables was a
# losing game (ANTHROPIC_BASE_URL/API_KEY/MODEL this round, other vendors the next), so the
# rule is per-backend PREFIXES and every matching variable counts.
_BACKEND_ENV_PREFIXES = {
    "claude": ("ANTHROPIC_",),
    "codex": ("OPENAI_", "CODEX_"),
    "cursor-agent": ("CURSOR_",),
    "gemini": ("GEMINI_", "GOOGLE_"),
    # AGY_* too: the PTY wrapper, the headless-profile root and the interpreter all change
    # how (and as whom) agy runs.
    "agy": ("GEMINI_", "GOOGLE_", "AGY_"),
    "opencode": ("OPENCODE_", "OPENROUTER_"),
    # openai-compat is deliberately ABSENT: it spawns no child and reads only the endpoint
    # and credential its provider resolves to, both already in the identity. Hashing every
    # OPENAI_* variable there meant an unrelated key change forced a fresh paid request.
}


# Variables the dispatch itself SETS, so whatever was inherited never reaches the child.
# Hashing them made unrelated ambient values force fresh, paid dispatches.
# Variables the dispatch supplies a DEFAULT for. Normalized into the hashed view so an
# unset variable and one explicitly set to that default -- identical to the child -- do not
# fingerprint differently. Kept next to the builder value it mirrors.
_BACKEND_ENV_DEFAULTS = {"agy": (("AGY_PTY_QUIET", "20"),)}

_BACKEND_ENV_OVERWRITTEN = {
    # AGY_PTY_QUIET is NOT here: the builder forwards whatever is set, so it does reach
    # the child and two values are two different requests.
    "agy": ("AGY_PTY_DEADLINE",),
    "gemini": ("GEMINI_SYSTEM_MD",),
    # The builder supplies a per-tier inline JSON policy; an ambient value must
    # never change the child while leaving the request identity looking equal.
    "opencode": ("OPENCODE_PERMISSION", "OPENCODE_CONFIG_CONTENT",
                  "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_PURE",
                  "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_CLAUDE_CODE"),
}


def _agy_account_sha(resume_profile=None) -> str | None:
    """Digest of the account/auth files summon COPIES into agy's isolated profile.

    agy authenticates from files, not the environment, so swapping ~/.gemini's OAuth
    credentials to a different Google account changed who answers while every environment
    variable stayed put -- and the cached answer from the first account came back. The file
    list is `_builder._AGY_AUTH_FILES`, the very list the profile builder copies, so this
    tracks whatever the dispatch actually carries rather than a guess about it.
    """
    try:
        from _builder import _AGY_AUTH_FILES, agy_profile_account_sha
    except Exception:  # noqa: BLE001
        return None
    if resume_profile:
        # A RESUME runs under the profile it is resuming, whose account was fixed when that
        # profile was created. Hashing the CURRENT ~/.gemini instead described an account
        # the run would not use: swap ~/.gemini and the fingerprint moved while the dispatch
        # still ran as the profile's account, and mutating the PROFILE moved the account
        # while the fingerprint stood still.
        return agy_profile_account_sha(resume_profile)
    real = os.path.join(os.path.expanduser("~"), ".gemini")
    h, seen_any = hashlib.sha256(b"summon-agy-account-v1"), False
    for fn in sorted(_AGY_AUTH_FILES):
        sha = content_sha(os.path.join(real, fn))
        if sha:
            seen_any = True
        h.update(b"\0" + fn.encode("utf-8") + b"\0" + (sha or "").encode("utf-8"))
    return h.hexdigest()[:32] if seen_any else None


def backend_env_sha(resolved_cli, allow_credit=False, model=None) -> str | None:
    """A one-way digest of the environment that configures `resolved_cli`.

    VALUES are hashed, never stored: several of these ARE credentials, and the same reasoning
    as the openai-compat credential applies -- a digest is not the secret, it does not leave
    the machine, and the alternative is one account's answer served to another. Returns None
    when the backend is unknown or nothing is set, so an unconfigured environment adds
    nothing to the identity.

    This is deliberately COARSE: an unrelated `ANTHROPIC_*` variable changing will invalidate
    a stored answer. That direction is the safe one -- a needless re-dispatch costs a run, a
    missed one returns the wrong answer.
    """
    prefixes = _BACKEND_ENV_PREFIXES.get(resolved_cli or "")
    if not prefixes:
        return None
    effective = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
    # Variables the DISPATCH overwrites unconditionally are not inputs to the request: agy
    # always sets AGY_PTY_DEADLINE from --timeout (or its own default), so inheriting 1 vs
    # 999 produced different identities for children that receive the identical value.
    for _overwritten in _BACKEND_ENV_OVERWRITTEN.get(resolved_cli or "", ()):
        effective.pop(_overwritten, None)
    # Variables the dispatch DEFAULTS: unset and "set to the default" are the same child
    # environment, so they must hash the same. Without this, leaving AGY_PTY_QUIET unset
    # fingerprinted differently from setting it to the very value the builder supplies.
    for _name, _default in _BACKEND_ENV_DEFAULTS.get(resolved_cli or "", ()):
        if effective.get(_name) is None:
            effective[_name] = _default
    # Apply summon's OWN delta, so this describes what the child receives rather than what
    # happens to be named like the vendor's variables. None means "removed from the child".
    try:
        from _builder import env_override_for
        # allow_credit threaded through: the FLAG authorizes the run but only sets its env
        # var later, so without it this applied the unauthorized stripping and hashed an
        # environment the authorized child does not receive.
        for k, v in (env_override_for(resolved_cli, allow_credit) or {}).items():
            if v is None:
                effective.pop(k, None)
            else:
                effective[k] = v
        if resolved_cli == "opencode":
            # The optional Windows Credential Manager bridge is model-scoped and
            # process-local. Include its one-way digest in request identity so a
            # rotated OpenRouter key cannot reuse an older cached answer.
            from _builder import opencode_env_override
            effective.update(opencode_env_override(model))
    except Exception:  # noqa: BLE001 — identity must never fail on an import edge
        pass
    items = sorted(effective.items())
    if not items:
        return None
    h = hashlib.sha256(b"summon-backend-env-v1")
    for k, v in items:
        h.update(b"\0" + k.encode("utf-8") + b"\0" + (v or "").encode("utf-8"))
    return h.hexdigest()[:32]


def _credit_env_allows(allow_credit=False) -> bool:
    """The credit-authorization predicate, delegated to the ONE that dispatch uses so the
    fingerprint and the dispatch can never disagree about what a value means.

    `allow_credit` (the --allow-credit FLAG) has to be threaded in: the flag sets
    SUMMON_ALLOW_CREDIT only LATER in the dispatch, after the identity is built, so a
    derivation reading the environment alone concluded "unauthorized" and recorded the Opus
    fallback for a request that actually ran Fable.
    """
    if allow_credit:
        return True
    try:
        from _builder import credit_spend_allowed
        return bool(credit_spend_allowed())
    except Exception:  # noqa: BLE001 — identity must never fail on an import edge
        return os.environ.get("SUMMON_ALLOW_CREDIT") == "1" or \
            os.environ.get("SUMMON_ALLOW_FABLE") == "1"


def _args_pin_model(args) -> bool:
    """Does an agent's `args:` already select codex's model?

    `args: -m gpt-x` (or --model, or -c model=...) pins the model just as surely as a
    `model:` key does, so the config file's default is not what runs -- and folding that
    default in anyway invalidated stored answers whenever config.toml changed, for a model
    the agent never uses.
    """
    args = [str(a) for a in (args or [])]
    for i, a in enumerate(args):
        # `-m X` / `--model X`
        if a in ("-m", "--model") and i + 1 < len(args):
            return True
        # `-m=X` / `--model=X`
        if a.startswith("--model=") or a.startswith("-m="):
            return True
        # a config override: `-c model=X`, `--config model=X`, and their attached forms.
        # A BARE `model=...` is NOT a pin on its own -- it is only one when it follows a
        # config option. Treating any standalone `model=` token as a pin meant something
        # like `--add-dir model=not-a-pin` suppressed the config default and let the old
        # model's answer be reused.
        if a in ("-c", "--config") and i + 1 < len(args):
            if args[i + 1].lstrip().startswith("model="):
                return True
        for opt in ("-c=", "--config="):
            if a.startswith(opt) and a[len(opt):].lstrip().startswith("model="):
                return True
    return False


# Backends where SUMMON_DEFAULT_EFFORT can still decide the effort. agy is excluded on
# purpose: it applies a thinking-mode suffix only when effort was given EXPLICITLY (CLI or
# frontmatter), never from the default, so fingerprinting the default there was pure churn.
# Kimi applies the setting inside its disposable config.toml profile.
_EFFORT_BACKENDS = ("claude", "codex", "kimi", "opencode")


def _effort_default_applies(effort, resolved_cli, agents_dir, cwd, agent, defn=None) -> bool:
    """Can SUMMON_DEFAULT_EFFORT still decide this request's effort?"""
    if effort or resolved_cli not in _EFFORT_BACKENDS:
        return False
    try:
        if defn is not None:
            tup = defn.tup
        else:
            from _loader import get_agents_dir, load_agent
            tup = load_agent(get_agents_dir(agents_dir, cwd), agent) if agent else None
        if tup and tup[7]:
            return False                       # the definition pins `effort:`
    except Exception:  # noqa: BLE001 — an unresolvable agent is reported by the dispatch
        pass
    return True


def _summon_default_model(resolved_cli, model, agents_dir=None, cwd=None,
                          agent=None, allow_credit=False, defn=None) -> str | None:
    """The model SUMMON itself substitutes when the request pins none.

    Only cursor today (CURSOR_DEFAULT_MODEL). Read from _builder so the identity tracks the
    value the dispatch actually uses rather than a copy that can drift from it. BOTH pins
    have to be checked -- `--model` and the agent definition's own `model:` -- because the
    `model` argument here is only the CLI override, so a definition-pinned agent would
    otherwise be invalidated every time the default moved, for a model it never runs.
    """
    if resolved_cli == "claude":
        # The credit guard substitutes summon's OWN fallback for an unauthorized credit-only
        # model, so changing that constant changes the model dispatched while the request
        # looks identical -- the same shape as cursor's default, and owned by summon either
        # way, so it belongs inside the boundary rather than in the excluded list.
        try:
            from _builder import (_CREDIT_ONLY_MODELS, _OPUS_FALLBACK, _scrub_credit_args,
                                  credit_spend_allowed)
            _authorized = _credit_env_allows(allow_credit)
            effective, extra_args = model, ()
            if not effective and agent:
                # The definition's `model:` (and its `args:`) are substituted just the same,
                # so tracking only --model left a `model: claude-fable-5` agent -- or an
                # `args: --model claude-fable-5` one -- unfingerprinted while its dispatched
                # model followed the fallback constant.
                if defn is not None:
                    tup = defn.tup
                else:
                    from _loader import get_agents_dir, load_agent
                    tup = load_agent(get_agents_dir(agents_dir, cwd), agent)
                effective, extra_args = tup[5], tup[6]
            if effective in _CREDIT_ONLY_MODELS and not _authorized:
                return _OPUS_FALLBACK
            # a credit-only model selected ONLY through args: is scrubbed to the fallback
            if not effective and not _authorized and _scrub_credit_args(extra_args)[1]:
                return _OPUS_FALLBACK
        except Exception:  # noqa: BLE001 — identity must never fail on an import edge
            return None
        return None
    if model or resolved_cli != "cursor-agent":
        return None
    try:
        if defn is not None:
            tup = defn.tup
        else:
            from _loader import get_agents_dir, load_agent
            tup = load_agent(get_agents_dir(agents_dir, cwd), agent) if agent else None
        if tup and tup[5]:
            return None                        # the definition pins it
    except Exception:  # noqa: BLE001 — an unresolvable agent is reported by the dispatch
        pass
    try:
        from _builder import CURSOR_DEFAULT_MODEL
        return CURSOR_DEFAULT_MODEL
    except Exception:  # noqa: BLE001 — identity must never fail on an import edge
        return None


def _codex_default(resolved_cli, model, agents_dir, cwd, agent, defn=None) -> str | None:
    """The model an UNPINNED codex agent will actually run, from ~/.codex/config.toml.

    Consulted ONLY when the backend resolves to codex and NOTHING else pins a model --
    neither `--model` nor the agent definition's own `model:`. Both have to be checked: the
    `model` argument here is just the CLI override, so an agent that pins its model in the
    definition would otherwise appear unpinned and be invalidated every time the config file
    changed, for a model it never uses. Any failure degrades to None: the config is advisory
    here and the dispatch reports a real problem with it.
    """
    if resolved_cli != "codex" or model:
        return None
    try:
        if defn is not None:
            tup = defn.tup
        else:
            from _loader import get_agents_dir, load_agent
            tup = load_agent(get_agents_dir(agents_dir, cwd), agent) if agent else None
        if tup:
            if tup[5]:
                return None                    # the definition's `model:` pins it
            if _args_pin_model(tup[6]):
                return None                    # ...or its `args:` do
    except Exception:  # noqa: BLE001 — an unresolvable agent is reported by the dispatch
        pass
    try:
        from _resolver import _codex_default_model
        return _codex_default_model()
    except Exception:  # noqa: BLE001 — identity must never fail on a config read
        return None


def _resolved_permission(defn, max_permission) -> str | None:
    """The tier a request will actually run at: the definition's `permission:` after the
    --max-permission clamp. Degrades to None when it cannot be resolved, which simply means
    a caller-side narrowing declines to narrow -- never a wrong answer.

    Used to fingerprint SUMMON_ALLOW_UNENFORCED_READONLY only where it matters. The opt-in
    decides whether a read-only agy request runs at all; on agy safe-edit/yolo it cannot
    change anything, and fingerprinting it there re-paid for identical work every time the
    variable moved.
    """
    if defn is None:
        return None
    try:
        # `.fm`, the snapshot's actual field. An earlier version read `.frontmatter`, which
        # does not exist -- getattr's default then returned {} and every agent resolved to
        # the DEFAULT tier, a plausible wrong answer rather than a visible failure. Read the
        # real attribute and let a genuinely absent one degrade to None (no narrowing)
        # instead of to a confident mistake.
        fm = getattr(defn, "fm", None)
        if fm is None:
            return None
        declared = (fm or {}).get("permission")
        if not declared:
            from _builder import DEFAULT_PERMISSION
            declared = DEFAULT_PERMISSION
        if max_permission:
            from _builder import clamp_permission
            return clamp_permission(str(declared), str(max_permission))
        return str(declared)
    except Exception:  # noqa: BLE001 - identity must never fail on a narrowing hint
        return None


def _resolved_cli(cli, agents_dir, cwd, agent, defn=None) -> str | None:
    """The backend a request will actually dispatch to: explicit --cli, else the agent's
    `run-agent:`, else CALLER DETECTION (env). Degrades to None (falling back to the raw
    `cli` field already in the identity) if anything cannot be resolved.

    `defn` is the shared snapshot; omitted (direct callers/tests) -> load here."""
    if cli:
        return str(cli)
    try:
        from _resolver import resolve_cli
        if defn is None:
            from _loader import get_agents_dir, load_agent
            tup = load_agent(get_agents_dir(agents_dir, cwd), agent) if agent else None
        else:
            tup = defn.tup
        return resolve_cli(tup[0] if tup else None)
    except Exception:  # noqa: BLE001 — the dispatch itself surfaces the real error
        return None


def _endpoint_state(agents_dir, cwd, agent, defn=None) -> tuple:
    """Identity of the endpoint an openai-compat agent ACTUALLY resolves to.

    Hashing the whole providers.json meant an agent with an inline `base_url:` (which never
    consults the registry) was invalidated by any edit to it, and a `tenant-a` agent was
    invalidated by a change to `tenant-b`. A false refusal is not free -- it costs a paid
    re-dispatch -- so what is fingerprinted is the RESOLVED endpoint, which is exactly what
    would change the answer. Unresolvable degrades to None: the dispatch reports that.
    """
    try:
        from _apibackend import resolve_endpoint
        from _loader import get_agents_dir
        roster = get_agents_dir(agents_dir, cwd)
        if defn is not None:
            fm = defn.fm
        else:
            from _loader import load_agent, parse_frontmatter
            with open(load_agent(roster, agent)[3], encoding="utf-8-sig") as fh:
                fm, _ = parse_frontmatter(fh.read())
        base_url, api_key_env = resolve_endpoint(fm, roster)
    except Exception:  # noqa: BLE001 — the dispatch itself surfaces the real error
        # UNRESOLVED, not merely unhashed: an agent naming a provider that no longer exists
        # has no endpoint identity at all. Swallowing that into a bare None dropped the
        # field from the fingerprint and let a legacy envelope be reused -- returning an
        # answer from the OLD endpoint instead of letting the dispatch report the unknown
        # provider. Every other unidentifiable input fails closed; so does this one.
        return None, "unresolved", None
    # The credential itself, one-way. Recording only the VARIABLE NAME meant two runs that
    # differed solely by which token was in TENANT_TOKEN fingerprinted identically, so one
    # tenant's answer could be served to the other without their endpoint ever being called.
    # Documenting that and advising separate results dirs does not enforce anything.
    #
    # What is stored is a SHA-256 over a domain separator, the variable name and the value --
    # not the value. It never leaves the machine (it lives in the local result file beside
    # the answer that credential produced), it cannot be reversed, and confirming a guess
    # requires already holding a candidate token, which for a high-entropy API key is not a
    # practical attack. summon's rule that secrets stay in the environment and out of
    # artifacts is about the SECRET; a one-way digest is not the secret, and the alternative
    # was a wrong answer.
    _cred = os.environ.get(api_key_env) if api_key_env else None
    cred_id = hashlib.sha256(
        b"summon-credential-v1\0" + (api_key_env or "").encode("utf-8") + b"\0"
        + _cred.encode("utf-8")).hexdigest()[:32] if _cred else ""
    # The RESOLVED PAIR is returned alongside the digest so the dispatch can use the very
    # snapshot that was fingerprinted. Resolving twice meant a providers.json edit between
    # the two reads sent the request to B while stamping it as A -- and restoring A then let
    # B's answer resume as A's. The identity and the call now describe the same endpoint.
    return (hashlib.sha256(f"{base_url}|{api_key_env}|{cred_id}".encode("utf-8")).hexdigest(),
            "ok", (base_url, api_key_env))


# Keys an identity dict carries for the SKIP's benefit that are NOT part of the request
# (they describe local state, not what was asked), so the fingerprint drops them.
_IDENTITY_LOCAL = ("_agent_def_state", "_unreadable", "_endpoint", "_agy_account_checked",
                   "_artifact_manifest", "_artifact_error", "_profile_error")


def build_request_identity(*, agent, prompt, cwd, agents_dir=None, cli=None, model=None,
                           effort=None, json_schema=None, resume=None, resume_profile=None,
                           worktree=None, allow_credit=False, gate_with=None,
                           max_permission=None, artifacts=None,
                           allow_text_only=False, require_tools=False, profile=None,
                           strict_agents_dir=False, role_provenance=None,
                           read_roots=None, isolated_lane=False,
                           allow_tool_credentials=False,
                           require_exact_model=False) -> dict:
    """THE request identity, built in ONE place from RAW inputs.

    The dispatcher and the manifest parent each used to build their own dict, so a field
    added to one and not the other went unnoticed until resume silently misbehaved -- either
    re-paying for every job forever (parent hash never matches the child's) or reusing a
    stale answer. Both now pass only what they RAW have; every derived field (file content
    hashes, the environment-backed controls) is computed here, once, so the two views cannot
    diverge by construction.

    `worktree`: pass the argparse value. A BARE --worktree (empty string) auto-names a FRESH
    tree per run, so it gets the distinguishing marker "<auto>" -- without it a bare-worktree
    result and a plain-cwd result hashed the same and the plain run reused the worktree's
    answer. The marker cannot express "never reusable" on its own (a hash must be
    deterministic to be comparable), so the skip ALSO refuses the bare form; the two together
    close it from both directions.

    NOT included: the roster DIRECTORY. `agent_def_sha256` is the authoritative agent
    identity, and two rosters holding a byte-identical definition really are the same
    request; keeping the lexical path as well contradicted that and re-paid for a moved
    roster. Also not included: repository state under `cwd` -- folding git HEAD in would
    invalidate every stored result on any unrelated commit, costing more than the staleness
    it prevents (`git_head_before` is in the envelope for a caller who wants it).
    """
    cwd = os.path.abspath(cwd) if cwd else None
    # ONE load of the definition for the WHOLE identity: every field derived from it reads
    # this snapshot, so an A -> B -> A swap mid-construction cannot produce a hybrid identity
    # (A's hash paired with B's resolved backend, which had turned agy attestation off).
    _defn = _defn_snapshot(agents_dir, cwd, agent,
                           strict_agents_dir=bool(strict_agents_dir))
    _adef = (_defn.sha, _defn.state) if _defn is not None else (None, "missing")
    _rcli = _resolved_cli(cli, agents_dir, cwd, agent, _defn)
    _rperm = _resolved_permission(_defn, max_permission)
    _profile_name = profile or ((_defn.fm or {}).get("profile") if _defn else None)
    _profile_selection = None
    _profile_error = None
    if _profile_name:
        try:
            from _profiles import resolve_profile
            _profile_selection = resolve_profile(_profile_name, _rcli, cwd)
        except Exception as exc:  # dispatch reports the actionable error later
            _profile_error = str(exc)
    _endpoint = (_endpoint_state(agents_dir, cwd, agent, _defn)
                 if _rcli == "openai-compat" else (None, "ok", None))
    _schema = content_state(json_schema or None)
    _memory = content_state(os.path.join(cwd, ".agents", "memory.md") if cwd else None)
    from _artifacts import build_manifest as _build_artifact_manifest
    _artifact_manifest, _artifact_error = _build_artifact_manifest(artifacts, cwd)
    _role = role_provenance if isinstance(role_provenance, dict) else {}
    _role_detail = _role.get("role") if isinstance(_role.get("role"), dict) else {}
    # Anything that EXISTS but could not be hashed leaves a hole in the identity, and a hole
    # is not a difference: two different unhashable schemas would hash alike. Record it so
    # the skip can fail closed rather than reuse on an identity it could not fully compute.
    _unreadable = sorted(n for n, st in (("json_schema", _schema[1]),
                                         ("memory", _memory[1]),
                                         ("agent_def", _adef[1]),
                                         ("endpoint", _endpoint[1]),
                                         ("artifacts", "unreadable" if _artifact_error else "ok"),
                                         ("profile", "unreadable" if _profile_error else "ok"))
                          if st not in ("ok", "absent", "missing"))
    _backend_model = model
    if not _backend_model and _defn is not None:
        try:
            _backend_model = _defn.tup[5]
        except (AttributeError, IndexError, TypeError):
            _backend_model = None
    _exact_model_required, _exact_model_source = model_exact_policy(
        agent=agent,
        frontmatter=(_defn.fm if _defn is not None else None),
        explicit=bool(require_exact_model),
    )
    return {
        # not hashed (local facts, not part of the request); carried so the skip can refuse
        # a MALFORMED definition or an identity it could not fully compute, and so dispatch
        # can reuse the endpoint snapshot that was fingerprinted.
        # request_fingerprint drops them: see _IDENTITY_LOCAL.
        "_agent_def_state": _adef[1],
        "_endpoint": _endpoint[2],
        "_unreadable": ",".join(_unreadable) or None,
        "_artifact_manifest": _artifact_manifest,
        "_artifact_error": _artifact_error,
        "_profile_error": _profile_error,
        "agent": agent, "prompt": prompt, "cwd": cwd,
        "cli": cli or None, "model": model or None, "effort": effort or None,
        # Exact-model enforcement is a request control.  Frontmatter policy is
        # already bound by agent_def_sha256; this boolean also makes a CLI opt-in
        # distinguishable from a cached best-effort result.
        "model_exact_required": "1" if _exact_model_required else None,
        # Additional read-only roots are request controls. Keep canonical paths in the
        # fingerprint so a cached answer from a narrower or different allowlist cannot
        # satisfy this dispatch. The envelope reports the effective paths separately.
        "read_roots": tuple(read_roots or ()),
        # The EFFECTIVE model when summon supplies the default itself. Cursor's default is a
        # constant in _builder, so a request with no `model:` dispatched whatever that
        # constant currently is while the identity recorded only `model=None` -- changing it
        # would have let the previous model's answer resume.
        "effective_default_model": _summon_default_model(_rcli, model, agents_dir,
                                                         cwd, agent, allow_credit, _defn),
        # An UNPINNED codex agent's model comes from ~/.codex/config.toml, so editing that
        # file changes which model answers while `model` here stays None -- the old answer
        # was served as current. Only consulted when nothing else pins the model.
        "codex_default_model": _codex_default(_rcli, model, agents_dir, cwd, agent, _defn),
        # The backend that will ACTUALLY run, not merely what the caller typed. With no
        # --cli and no `run-agent:`, resolve_cli falls through to CALLER DETECTION, so the
        # same command under CLAUDE_CODE=1 and under CODEX_CLI=1 is two different requests
        # that hashed identically -- the second could reuse the first backend's answer.
        "resolved_cli": _rcli,
        "backend_env_sha256": backend_env_sha(_rcli, allow_credit, _backend_model),
        # A profile name alone is not enough: the private registry can retarget it to a
        # different account. Hash the resolved directory and registry snapshot, never the
        # path itself, so cached answers cannot cross profile changes.
        "profile": _profile_name or None,
        "profile_path_sha256": ((_profile_selection or {}).get("path_sha256")),
        "profile_registry_sha256": ((_profile_selection or {}).get("registry_sha256")),
        "profile_command_sha256": ((_profile_selection or {}).get("command_sha256")),
        # Strict roster provenance changes whether a bundled/pack definition is eligible;
        # keep it in the request identity so a cached fallback result cannot satisfy a
        # later governance request.
        "strict_agents_dir": "1" if strict_agents_dir else None,
        # An approved role is part of the request identity, not merely a convenient
        # spelling.  Hashing its approval fingerprint/record prevents a cached direct
        # target result, or a result from a retargeted registry entry, from satisfying
        # an opted-in role dispatch.  Names and digests only; never the private registry
        # path or its contents.
        "role_requested": _role.get("requested") if _role_detail else None,
        "role_resolved": _role_detail.get("resolved_agent") if _role_detail else None,
        "role_target_sha256": _role_detail.get("target_sha256") if _role_detail else None,
        "role_fingerprint": _role_detail.get("fingerprint") if _role_detail else None,
        "role_hash": _role_detail.get("hash") if _role_detail else None,
        "role_registry_sha256": _role_detail.get("registry_sha256") if _role_detail else None,
        # ONLY when this is an actual resume: --resume-profile without --resume still takes
        # the FRESH-profile branch at dispatch, so selecting the resumed profile's account
        # there made a perfectly good fresh profile look like an account swap and refused it.
        "agy_account_sha256": (_agy_account_sha(resume_profile if resume else None)
                               if _rcli == "agy" else None),
        # True whenever this identity inspected the agy account -- so `agy_account_sha256:
        # None` means "no account files at fingerprint time" (a state to attest) rather than
        # "a legacy caller who recorded nothing" (skipped). Local, not hashed.
        "_agy_account_checked": _rcli == "agy",
        # The schema is identified by its CONTENTS, not its path: editing schema.json in
        # place is a different contract for the same filename, and the same contract at two
        # paths is the same request. The path itself was carried as well until a mutation
        # sweep showed nothing depended on it -- every case it could distinguish is either
        # caught by the content hash or fails at load before producing a result.
        "json_schema_sha256": _schema[0],
        # Loose inputs are identified by a manifest over relative path + content
        # hash + size. Unlike a schema path, the artifact filename is part of the
        # review contract because findings and locators name it.
        "artifact_manifest_sha256": ((_artifact_manifest or {}).get("sha256")),
        "resume": resume or None,
        # Only during an ACTUAL resume: without --resume the dispatch ignores the profile
        # entirely and builds a fresh one, so fingerprinting the path made two fresh runs
        # that differ only by an unused profile argument re-pay for the same work.
        "resume_profile": (resume_profile or None) if resume else None,
        "worktree": ("<auto>" if worktree == "" else (worktree or None)),
        # OpenCode yolo is an explicit isolated-lane operation.  These controls
        # are part of the identity so a cached unrestricted result cannot satisfy
        # a later request with a different credential/tool boundary.
        "isolated_lane": ("1" if isolated_lane else None) if _rcli == "opencode" else None,
        "allow_tool_credentials": (
            "1" if allow_tool_credentials else None) if _rcli == "opencode" else None,
        # The CONTROLS are part of the request. Without them a stored --out success
        # from an UNGATED, UNCLAMPED run satisfied a later gated+clamped request for
        # the "same" task: the skip handed back a result produced under authority the
        # new request deliberately withheld. A gated request is not the same request.
        "gate_with": gate_with or None,
        "max_permission": max_permission or None,
        # Text-seat consent is part of the request for openai-compat/arkcli: a
        # stored --out success from an opted-in run must not satisfy a later
        # request that withheld consent (same hole class as gate_with).
        # Match text_only_opted_in(): flag, env, OR capability: text-only on the
        # already-loaded definition snapshot (not a second file read).
        "allow_text_only": (
            "1" if (
                allow_text_only
                or os.environ.get("SUMMON_ALLOW_TEXT_ONLY") == "1"
                or (_defn is not None and _fm_capability_text_only(_defn.fm))
            ) else None) if _rcli in ("openai-compat", "arkcli") else None,
        "require_tools": (
            "1" if (require_tools or os.environ.get("SUMMON_REQUIRE_TOOLS") == "1")
            else None) if _rcli in ("openai-compat", "arkcli") else None,
        # Credit authorization changes the effective MODEL (it lifts the guard's
        # substitution) and arrives as either the flag or the env var the flag sets.
        # SUMMON_DEFAULT_EFFORT likewise changes effort without ever being a flag. A
        # manifest/background child INHERITS this env, so both sides see it alike.
        # EXACTLY the predicate dispatch uses. Collapsing any non-empty value to "1"
        # made SUMMON_ALLOW_CREDIT=0 and =1 hash the same while selecting different
        # effective models for a credit-only request.
        # Credit authorization only ever changes the model on the CLAUDE backend (it lifts
        # that guard's substitution), so folding it into any other backend's identity just
        # re-paid for work the switch could not have altered.
        "allow_credit": ("1" if _credit_env_allows(allow_credit) else None)
                        if _rcli == "claude" else None,
        # The unenforced-read-only opt-in decides whether this request RUNS AT ALL, so a
        # stored success produced under it must not satisfy a later request made without it.
        # Cached reuse happens BEFORE execute_agent can refuse, so identity is the only
        # place that can catch it. agy-only, for the same reason allow_credit is claude-only:
        # on any other backend it cannot change the outcome, and fingerprinting it there
        # would re-pay for work the switch could not have altered.
        # Narrowed by EFFECTIVE PERMISSION, not just backend. The opt-in only decides
        # whether a READ-ONLY agy request runs; on agy safe-edit/yolo it cannot change the
        # outcome, and fingerprinting it there re-paid for identical work (and risked
        # repeating side effects) every time the variable moved.
        "unenforced_readonly": ("1" if os.environ.get(
            "SUMMON_ALLOW_UNENFORCED_READONLY") == "1" else None)
            if (_rcli == "agy" and _rperm == "read-only") else None,
        # The DEFAULT only applies when nothing explicit was asked for; with --effort set
        # it cannot change the request, and fingerprinting it anyway forced a fresh dispatch
        # every time an unrelated default moved.
        # The DEFAULT only applies when nothing explicit was asked for AND the backend
        # actually takes an effort setting. With --effort given, with the definition pinning
        # `effort:`, or on a backend that ignores effort entirely, it cannot change the
        # request -- and fingerprinting it anyway forced a fresh dispatch every time an
        # unrelated default moved.
        "default_effort": (os.environ.get("SUMMON_DEFAULT_EFFORT") or None)
                          if _effort_default_applies(effort, _rcli, agents_dir, cwd, agent,
                                                     _defn)
                          else None,
        # the definition that will actually be loaded, by CONTENT: a roster edit makes a
        # stored answer stale and neither the agent name nor the roster path shows it
        "agent_def_sha256": _adef[0],
        # .agents/memory.md is injected into the system context, so editing it changes the
        # instructions the answer was produced under
        "memory_sha256": _memory[0],
        # An openai-compat agent names a `provider:` whose base_url lives in a
        # providers.json OUTSIDE the agent file, so retargeting a provider changes where
        # the work goes while agent + prompt + definition all stay identical.
        # ONLY for the backend that actually resolves providers. Folding it in
        # unconditionally meant editing an unused providers.json refused a perfectly good
        # codex result -- and a refusal is not free: the manifest clears the stale envelope
        # before re-dispatching, so a false refusal DESTROYED a completed answer.
        "providers_sha256": _endpoint[0],
    }


def request_fingerprint(**fields) -> str:
    """A stable hash of everything about a request that can change the ANSWER.

    `--out` and manifest resume decide "already done" from a result FILE, and a file has no
    memory of what it answered. Keyed on the path alone they returned a stale answer as a
    fresh one: keep a manifest job's `id` but edit its prompt (or point it at a different
    model) and the job came back `skipped` with the previous answer. Both skip paths now
    stamp and compare this fingerprint.

    Included: agent, prompt (hashed), cwd, cli, model, effort, and the schema by CONTENT --
    the inputs that select or steer the work. The roster DIRECTORY (`agents_dir`) is NOT
    included: the agent definition's CONTENT is the authoritative identity (`agent_def_sha`),
    and two rosters holding a byte-identical definition are the same request, so hashing the
    path would re-pay for a moved roster. EXCLUDED on purpose too: timeout, retries and
    debug_dir (they change how long or how loudly, never the answer), so raising a timeout
    does not force a re-pay. Absent fields are dropped rather than sent as null, so adding a
    field later does not invalidate every stored envelope that lacks it.

    The agent DEFINITION is included, by CONTENT (`agent_def_sha`), so editing an agent's
    `model:` or body invalidates a stored answer. An earlier version excluded it on the
    stated grounds that neither skip path had resolved the agent yet -- which was simply
    wrong: the manifest parent already resolves it in `_job_backend` to pick a semaphore.

    WHAT THIS DOES AND DOES NOT COVER -- read this before extending it.

    COVERED, exactly these channels and no others:
      * the request as summon received it: agent name, prompt, cwd, cli, model, effort,
        --resume, --worktree, --allow-credit, and --resume-profile ONLY during an actual
        resume (without --resume the dispatch ignores it, so neither does the identity);
      * file CONTENT -- not path -- for the things summon reads itself: the agent
        definition, the --json-schema, `.agents/memory.md`. The same schema at two paths is
        the same contract, and the roster DIRECTORY is likewise absent because the
        definition's content is the authoritative thing;
      * what summon RESOLVES: the effective backend (`resolve_cli`, including caller
        detection), the openai-compat endpoint and its credential, cursor's default model,
        and codex's configured default model when nothing else pins one;
      * environment variables matching the resolved backend's PREFIXES (ANTHROPIC_*,
        OPENAI_*/CODEX_*, CURSOR_*, GEMINI_*/GOOGLE_*/AGY_*), after summon's own delta
        (`_builder.env_override_for`), minus the ones the dispatch OVERWRITES for every run
        and with the ones it DEFAULTS normalized -- so this is what the child receives, from
        those prefixes;
      * for agy, the account files summon copies into the isolated profile.

    NOT COVERED -- and this list is the point, not an apology:
      * the vendor CLI's own installed state: its config files (beyond codex's `model`),
        its stored credentials, its signed-in account, its VERSION;
      * environment outside the prefixes above (an inherited HTTP_PROXY, for instance);
      * SUMMON'S OWN VERSION and its built-in defaults that are not otherwise listed (the
        default effort, say). Upgrading summon can therefore change what a re-run would
        produce without invalidating a stored answer -- a deliberate exclusion, since
        pinning the dispatcher's identity would invalidate every stored result on every
        upgrade. The envelope records `summon.version` and `summon.scripts_sha256`, so a
        caller who wants that strictness can compare them and delete the result file;
      * the repository state under `cwd` (see below).

    A sub-agent's answer ultimately depends on the whole installation behind the CLI, and
    summon cannot enumerate that: four successive review rounds each found another channel
    (a credential value, then vendor environment variables, then summon's own environment
    transformations, then agy's on-disk account), and a fifth would find another. So the
    contract is deliberately BOUNDED: a matching fingerprint means "the same request, as
    summon defines a request", NOT "the same answer is guaranteed". A caller who needs more
    than that should not resume -- delete the result file, or give each configuration its
    own --out/--results-dir.

    Also outside on purpose: the repository state under `cwd`. Folding git HEAD in would
    invalidate every stored result on any unrelated commit, which costs more than the
    staleness it prevents -- resume exists precisely so an expensive fan-out survives an
    interruption. It IS recorded in the envelope (`git_head_before`), so a caller who wants
    that strictness can compare it and delete the result file.
    """
    for _local in _IDENTITY_LOCAL:
        fields.pop(_local, None)
    prompt = fields.pop("prompt", None)
    if prompt is not None:
        fields["prompt_sha256"] = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
    canonical = {k: str(v) for k, v in sorted(fields.items()) if v is not None and v != ""}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def envelope_answers_request(prior, fingerprint: str, prompt_sha256=None,
                             agent=None, identity=None) -> tuple[bool, str | None]:
    """Can `prior` be reused as the answer to the request `fingerprint` describes?

    Returns (reusable, note). `note` is a warning to attach when the answer is "yes, but we
    could not verify it" -- "unknown" must never be reported as "verified". A MISMATCH is
    never reused.

    Two layers, because envelopes written before request fingerprinting have no key:
    1. `request_sha256` present -> the authoritative comparison.
    2. Absent -> fall back to the fields such an envelope DOES carry. They cannot prove a
       match, but a differing prompt hash or agent PROVES a difference, and proof of
       difference is enough to re-dispatch rather than serve a stale answer.
    """
    if not isinstance(prior, dict):
        return False, None
    if identity and identity.get("_unreadable"):
        # An input that exists but could not be hashed (a stalled share, a permissions
        # error) leaves the identity incomplete, and an incomplete identity cannot say two
        # requests are the same. Re-dispatching costs a run; reusing costs correctness.
        return False, None
    if identity and identity.get("agent") \
            and identity.get("_agent_def_state") in ("malformed", "missing"):
        # The definition is part of the request, so a MISSING or MALFORMED one means this
        # request cannot be matched against anything -- and the dispatch is the only thing
        # that can report it. Applied to pre-fingerprint envelopes too: having one answer
        # for old envelopes and another for new ones was a contradiction, and the lenient
        # half preserved results nobody could attribute. A manifest's completed answer is
        # not lost by the re-dispatch -- it is archived (see _manifest._clear_out_file).
        return False, None
    stored = prior.get("request_sha256")
    if isinstance(stored, str) and stored:
        # An agent definition that has been DELETED drops its hash out of the recomputed
        # fingerprint, so this comparison fails and the job re-dispatches. That is the
        # intended behaviour, not a gap: the definition is part of the request, and a
        # manifest still naming an agent that no longer exists should hear about it. The
        # completed answer is not lost -- the manifest moves a superseded success aside
        # rather than deleting it. An earlier attempt to make that case reusable via a
        # second, definition-independent fingerprint was withdrawn: it could not tell a
        # pinned agent from an unpinned one once the file was gone, so it either reused a
        # different vendor's answer or refused a perfectly good one.
        return stored == fingerprint, None
    for mine, theirs in ((prompt_sha256, prior.get("prompt_sha256")),
                         (agent, prior.get("agent"))):
        if mine and theirs and mine != theirs:
            return False, None
    return True, ("prior envelope predates request fingerprinting (no request_sha256); "
                  "skipped without verifying it answers this request")


def is_terminal_success(env) -> bool:
    """A dispatch envelope is TERMINAL-done only when it succeeded AND is not
    suspect (status=success but report_ok=false -> suspect: a semantically-useful
    but unparseable result that should re-dispatch, not be skipped). Shared by the
    --out skip and manifest resume so both agree."""
    return bool(isinstance(env, dict) and env.get("status") == "success"
                and not env.get("suspect"))


def is_terminal_nonretryable(env) -> bool:
    """True for a typed failure that is complete evidence, not a retry cue.

    Empty terminal results are preserved for diagnosis.  Re-running a manifest
    against the same non-interactive failure would spend again without changing
    the input; an operator can explicitly retry by removing the result file or
    passing the deliberate retry override.
    Other errors retain the historical retry-on-resume behavior.
    """
    return bool(isinstance(env, dict)
                and env.get("status") in ("error", "blocked", "partial")
                and env.get("retryable") is False
                and env.get("error_kind") in {
                    "empty_terminal_result",
                    "provider_timeout",
                    "authentication_failed",
                    "rate_limited",
                    "provider_forbidden",
                    "permission_unsupported",
                    "model_selection_conflict",
                    "target_model_mismatch",
                    "served_model_mismatch",
                    "served_model_unverified",
                    "resume_model_mismatch",
                    "resume_model_unverified",
                    "missing_executable",
                })


# Hook / launcher noise that a HOST environment injects ahead of the backend's own output.
# Field report (2026-07-27): a third-party plugin hook put an unquoted Windows path into a
# PowerShell command line, and its parse error was the FIRST thing in a failed gemini
# envelope -- so the operator diagnosed the hook, reported it as the cause, and only then
# found the real `IneligibleTierError` underneath. summon cannot fix someone else's hook,
# but it should not let that hook's noise be the headline on a failure.
_NOISE_MARKERS = (
    "at line:", "unexpected token", "+ categoryinfo", "+ fullyqualifiederrorid",
    "\\plugins\\marketplaces\\", "parsererror", "is not recognized as the name of a",
)
# Signatures of a REAL backend failure, most specific first. These are the sentences an
# operator actually needs; they are routinely buried under the noise above.
# Signatures of a REAL backend failure. Each must be STRUCTURAL -- a phrase a backend
# emits as an error, not a word that ordinary prose or a prompt echo can contain. Review
# demonstrated the danger with bare words: a prompt saying "print exactly Unauthorized:
# rotate all credentials" got its own text promoted into the envelope's error field, and a
# successful answer discussing what is "not supported for" X was mislabelled identically.
# The captured stream is UNTRUSTED INPUT; treat every line as something an attacker or an
# unlucky prompt may have authored.
_SIGNAL_MARKERS = (
    "error authenticating:", "ineligibletiererror", "authenticationerror",
    "error: quota", "quota exceeded", "rate limit exceeded", "rate_limit_exceeded",
    "rate-limit-exceeded", "429 too many requests",
    "401 unauthorized", "403 forbidden", "invalid api key", "api key not valid",
    "model not found", "no such model", "econnrefused", "etimedout",
    "permission denied:", "eacces",
    # MISSING TOOL / WRONG SHELL. These are machine-generated shell diagnostics, not prose,
    # and they are the single most common cause of a long silent stall: the agent reaches
    # for a tool the child cannot see and retries or waits until the clock runs out. A field
    # report burned 480s on `rg` missing from a codex child's PATH and the message was
    # visible only in output_tail (2026-07-28).
    "is not recognized as a name of a cmdlet",
    "is not recognized as an internal or external command",
    ": command not found",
    "executable file not found in",
)

# Generic enough to appear in ORDINARY PROSE ("...no such file or directory handling..."),
# so these count only when the line ENDS with them -- which is where a shell puts them
# (`cat: foo.txt: No such file or directory`) and where a sentence does not. Found by
# testing the negative case after the anywhere-in-line form promoted a release-note
# sentence; the positive cases had all passed.
_SIGNAL_SUFFIXES = (
    "no such file or directory",
    "command not found",
)

# A missing shell utility is an execution-environment problem, not provider
# authentication. Keep these recognizers deliberately structural: a bare word
# such as ``grep`` in a model's prose must never change the dispatch outcome.
_MISSING_EXECUTABLE_PATTERNS = (
    re.compile(
        r"exec:\s*[\"'](?P<executable>[^\"']+)[\"']:\s*"
        r"executable file not found in %PATH%",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|(?:bash|sh|zsh|fish|busybox)[^:\r\n]*:\s*)"
        r"(?P<executable>[A-Za-z0-9_.+@-]+):\s*command not found(?:$|[.!\s])",
        re.IGNORECASE,
    ),
    re.compile(
        r"the term\s*[\"'](?P<executable>[^\"']+)[\"']\s+is not recognized\s+"
        r"as\s+(?:a\s+)?(?:name of a cmdlet|the name of a cmdlet)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<executable>[A-Za-z0-9_.+@-]+)\s+is not recognized as an internal "
        r"or external command",
        re.IGNORECASE,
    ),
)
_SAFE_EXECUTABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+@-]{0,95}$")


def missing_executable_details(text: str | None) -> dict | None:
    """Return a bounded, non-secret diagnostic for a structurally missing tool.

    Provider/model output is untrusted, so this only accepts shell error shapes
    emitted by the child runtime and publishes a basename-like executable name.
    It intentionally does not try to infer a path, package, or credential state.
    """
    if not text:
        return None
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or len(line) > 400:
            continue
        for pattern in _MISSING_EXECUTABLE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            candidate = str(match.group("executable")).strip().strip("'\"")
            # A path can appear in the PowerShell form. Publishing only the final
            # component avoids leaking a local directory while retaining useful
            # operator guidance.
            candidate = re.split(r"[\\/]", candidate)[-1]
            if not _SAFE_EXECUTABLE_NAME.fullmatch(candidate):
                continue
            return {
                "kind": "missing_executable",
                "executable": candidate,
                "fatal": True,
                "recommendations": [
                    "use rg when available",
                    "use PowerShell Select-String/Get-ChildItem on Windows",
                    "use Python's standard library as a cross-platform fallback",
                ],
            }
    return None


def salient_error(text: str, prompt: str | None = None) -> str | None:
    """A HINT at the likely cause, extracted from a failed run's captured output.

    NOT authoritative, and deliberately not named as though it were: the captured stream is
    untrusted, so this returns a candidate line the operator should read, never a verdict.
    Prefers a structural backend failure over host/hook noise, skips anything that also
    appears in the PROMPT (an echo is the agent repeating the caller, not the backend
    failing), and returns None rather than guessing. The full text always survives in
    `output_tail`.
    """
    if not text:
        return None
    # Prompt echo: a line the caller supplied is not evidence about the backend. Compared
    # on a normalised form so incidental whitespace does not defeat it.
    echoed = set()
    if prompt:
        echoed = {" ".join(l.split()).lower() for l in prompt.splitlines() if l.strip()}
    best = None
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 12 or len(line) > 400:
            continue
        low = line.lower()
        if any(m in low for m in _NOISE_MARKERS):
            continue                      # host noise: never the headline
        if " ".join(line.split()).lower() in echoed:
            continue                      # the caller's own words, echoed back
        # Generic suffixes are ranked after every explicit marker: an auth or quota
        # signature on another line is a better headline than a missing-file tail.
        _tail = low.rstrip().rstrip(".").rstrip()
        _base = len(_SIGNAL_MARKERS)
        for rank, marker in enumerate(_SIGNAL_SUFFIXES):
            if _tail.endswith(marker):
                if best is None or (_base + rank) < best[0]:
                    best = (_base + rank, line.strip())
        for rank, marker in enumerate(_SIGNAL_MARKERS):
            if marker in low:
                if best is None or rank < best[0]:
                    best = (rank, line)
                break
    return best[1] if best else None


def host_noise_present(text: str) -> bool:
    """True when the captured output contains host/hook noise the operator did not cause.

    Worth surfacing separately: it is not summon's bug and not the backend's, but it is
    actively misleading on a failure, and the operator can go fix their hook.
    """
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _NOISE_MARKERS)


# agy's bundled Go language server writes a glog-style log into the dispatch's working
# directory, under the literal filename `--print` (it picks summon's `--print` flag up as a
# log target). Measured 2026-07-26: a 163 KB `--print` file in the repo root after agy runs.
# summon spawns agy, so this is summon's litter to clean: pointing agy at a repo should not
# leave a junk file in it. Removal is conservative -- only a file that did NOT exist before
# the dispatch and that looks like a glog log is touched, so a real file with that
# (admittedly unlikely) name is never destroyed.
_GLOG_SIGNATURE = ("server.go", "Starting language server", "Language server version")


def _agy_litter_path(cwd: str | None) -> str | None:
    """The stray log path an agy dispatch may create in `cwd`."""
    if not cwd:
        return None
    try:
        return os.path.join(cwd, "--print")
    except Exception:  # noqa: BLE001
        return None


# glog's RECORD format, which the Go language server emits and no human writes by hand:
#   I0727 12:00:00.123456       1 main.go:10] message
# Matching this is the identity check. The previous version matched loose PHRASES
# ("Starting language server") anywhere in the head, which certification round 3 broke in
# one line: a caller's file reading "Release checklist: verify Starting language server
# appears in diagnostics." was accepted and DELETED. Prose can contain any phrase; prose
# cannot accidentally be glog.
_GLOG_RECORD = re.compile(r"^[IWEF]\d{4} \d{2}:\d{2}:\d{2}\.\d{6}\s+\d+ \S+:\d+\]")
# glog's own file preamble. A real log starts with one of these or with a record line.
_GLOG_HEADER = ("Log file created at:", "Running on machine:", "Log line format:")


def _looks_like_agy_log(path: str) -> bool:
    """True only for a file that is STRUCTURALLY agy's language-server log.

    Two independent conditions, both required, because the cost of a false positive here
    is an unrecoverable deletion in someone's repository while the cost of a false negative
    is a leftover file:

      1. the first non-blank line is a glog preamble line or a glog record -- a caller's
         document that merely quotes a log line somewhere in the middle is not a log;
      2. at least one line matches glog's machine record format, which is not something
         prose produces by accident.

    Certification round 3 deleted a real file that satisfied neither, because the old check
    accepted any one loose phrase anywhere in the first 4 KB.
    """
    try:
        if os.path.getsize(path) > 50_000_000:      # never slurp something enormous
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return False

    lines = [ln for ln in head.splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0]
    if not (any(first.startswith(h) for h in _GLOG_HEADER) or _GLOG_RECORD.match(first)):
        return False
    return any(_GLOG_RECORD.match(ln) for ln in lines)


def _sweep_agy_litter(cwd: str | None, existed_before: bool) -> bool:
    """Remove agy's stray `--print` log if this dispatch created it. Returns True if removed.

    Fails soft everywhere: leaving litter is a nuisance, but deleting the wrong file in
    someone's repository is not recoverable, so every check must pass.
    """
    path = _agy_litter_path(cwd)
    if not path or existed_before or not os.path.isfile(path):
        return False
    # TOCTOU: the identity check reads the file, the deletion happens later, and in between
    # the path can be replaced. Fingerprint what was VERIFIED and refuse to delete anything
    # that is no longer byte-identical to it. This cannot close the window entirely on
    # every filesystem, but it means the thing deleted is the thing that passed the check.
    try:
        before = os.stat(path)
    except OSError:
        return False
    if not _looks_like_agy_log(path):
        return False
    try:
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (
                before.st_size, before.st_mtime_ns, before.st_ino):
            return False        # it changed under us; it is no longer what we verified
        os.remove(path)
        return True
    except OSError:
        return False


def finalize_exit_fields(resp: dict) -> dict:
    """Backfill explicit raw/normalized exit fields on a dispatch envelope.

    ``exit_code`` remains the legacy field for compatibility and may be the
    backend's non-zero code even when a complete report made execution a
    normalized success.  New callers should use ``raw_backend_exit_code`` and
    ``normalized_exit_code`` instead.  The detailed normalization reason is
    preserved when a builder already supplied one.
    """
    if not isinstance(resp, dict) or resp.get("exit_code") is None or not resp.get("status"):
        return resp
    raw = resp.get("raw_backend_exit_code")
    if raw is None:
        raw = resp.get("backend_exit_code")
    if raw is None:
        raw = resp.get("exit_code")
    resp.setdefault("backend_exit_code", raw)
    resp.setdefault("raw_backend_exit_code", raw)
    resp.setdefault("dispatcher_status", resp.get("status"))
    resp.setdefault("normalized_exit_code",
                    _normalized_exit_code(resp.get("status"), raw))
    if "normalization_reason" not in resp:
        _ec, _st = raw, resp.get("status")
        if _st == "success" and _ec not in _SUCCESS_EXIT_CODES:
            resp["normalization_reason"] = f"normalized to success (raw backend exit {_ec})"
        elif _st == "success":
            resp["normalization_reason"] = "exit and status agree"
        else:
            resp["normalization_reason"] = f"status {_st} (backend exit {_ec})"
    return resp


def _normalized_exit_code(status: object, raw_backend_exit_code: object) -> int | None:
    """Return the dispatcher outcome code while preserving the child code separately.

    A complete report can be a successful execution even when a CLI exits non-zero;
    conversely, an empty result can be an execution error after a zero child exit.
    ``blocked`` and ``partial`` are terminal outcomes, not process failures, while
    errors/cancellations with a zero child code receive the conventional code ``1``.
    """
    try:
        raw = int(raw_backend_exit_code)
    except (TypeError, ValueError, OverflowError):
        return None
    if status in {"success", "partial", "blocked"}:
        return 0
    if status in {"error", "cancelled"} and raw == 0:
        return 1
    return raw


def _model_mismatch(requested, ran) -> bool:
    """True when an EXPLICIT model request differs from the model that ran, EXCEPT
    for a known floating-alias expansion (opus/sonnet/haiku -> their latest id).
    Both must be non-empty strings; a None/empty request never warns."""
    if not (isinstance(requested, str) and requested.strip()
            and isinstance(ran, str) and ran.strip()):
        return False
    r, s = requested.strip().lower(), ran.strip().lower()
    if r == s:
        return False
    try:
        from _resolver import _CLAUDE_ALIASES
    except Exception:  # noqa: BLE001 — telemetry best-effort, never fatal
        _CLAUDE_ALIASES = ("opus", "sonnet", "haiku")
    # A floating alias expands to an id that carries the alias as a WHOLE TOKEN
    # ('opus' -> 'claude-opus-5'); a substring test would wrongly hide a real
    # reroute ('opus' -> 'notopus'), so split on id separators and match exactly.
    if r in _CLAUDE_ALIASES and r in re.split(r"[-_/.:]+", s):
        return False
    return True


def model_match_state(requested, targeted, served,
                      served_model_evidence) -> bool | None:
    """Return the safe tri-state named-model match for a terminal result.

    ``True`` is reserved for a provider-reported served model with all three
    identities present and mutually compatible. ``False`` means the provider
    reported a served model but it disagrees with the requested/targeted model.
    Missing or inferred evidence is ``None`` and can never certify a named
    review. The helper never infers identity from status or output tokens.
    """
    if served_model_evidence != "reported" or not served:
        return None
    if not requested or not targeted:
        return None
    # This is a certification bit, not the looser warning/alias comparison
    # used by ``_model_mismatch``.  A named-model review is verified only when
    # the provider reported the exact same identifier for requested, targeted,
    # and served.  Floating aliases may still avoid a warning, but they never
    # become a named-model proof.
    return requested.strip() == targeted.strip() == served.strip()


# These seats are governance-facing named-model claims rather than ordinary
# implementation workers.  A provider is allowed to use an auxiliary model in
# a session (``models_used`` remains an honest list), but the dominant terminal
# model must still match the requested pin before the result can be consumed as
# the named review.  Custom seats can opt in with ``model-policy: exact`` or the
# one-shot ``--require-exact-model`` flag.
_EXACT_MODEL_SEATS = frozenset({
    "architect", "planner", "deep-debugger", "security-auditor",
    "fable", "fable-api", "sol-review", "terra-review", "luna-review",
    "researcher",
})


def model_exact_policy(agent: str | None = None, frontmatter: dict | None = None,
                       explicit: bool = False) -> tuple[bool, str | None]:
    """Resolve the exact-model trust policy without contacting a provider.

    The built-in named review seats are fail-closed by name so an independently
    copied roster cannot silently lose its governance contract.  User-defined
    seats remain best-effort unless their frontmatter opts in explicitly.  The
    caller still supplies the actual model pin; a policy alone never invents a
    target model.
    """
    if explicit:
        return True, "cli"
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    raw = fm.get("model-policy", fm.get("model_policy"))
    if isinstance(raw, str) and raw.strip().lower() in {"exact", "required", "strict"}:
        return True, "frontmatter"
    if agent and str(agent).strip().lower() in _EXACT_MODEL_SEATS:
        return True, "named-seat"
    return False, None


def trusted_telemetry_model_evidence(response: object):
    """Return a private evidence marker only for an executor-produced result.

    The JSON envelope is caller-visible and therefore cannot be its own proof.
    Keeping marker construction behind the executor result path lets the
    dispatcher distinguish a fresh in-process response from a disk-loaded
    ``--out``/resume object.  Telemetry is optional and this helper is fail-soft.
    """
    if not isinstance(response, dict) or response.get("served_model_evidence") != "reported":
        return None
    model = response.get("model")
    model = model if isinstance(model, dict) else {}
    try:
        import _telemetry
        mismatch = _model_mismatch(model.get("requested"), model.get("served"))
        return _telemetry._trusted_model_evidence(
            "reported", mismatch, _capability=_telemetry._EVIDENCE_CAPABILITY)
    except Exception:  # noqa: BLE001 - telemetry must never affect execution
        return None


def trusted_telemetry_auth_lifecycle(response: object):
    """Return an executor-only marker for bounded auth lifecycle fields."""
    if not isinstance(response, dict):
        return None
    keys = ("auth_stage", "auth_outcome", "interactive_required", "remediation_code")
    if not any(response.get(key) is not None for key in keys):
        return None
    try:
        import _telemetry
        return _telemetry._trusted_auth_lifecycle(
            *(response.get(key) for key in keys),
            _capability=_telemetry._AUTH_CAPABILITY)
    except Exception:  # noqa: BLE001 - telemetry must never affect execution
        return None


_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+() -]{0,159}$")
_MODEL_SECRET_MARKERS = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:bearer|basic|secret|token|password|"
    r"(?:api|oauth|access|auth)[-_]?token|(?:api|private)[-_]?key)"
    r"(?:$|[^a-z0-9])")
_MODEL_PATH_PREFIXES = re.compile(
    r"(?i)^(?:users|home|private|tmp|var|etc|program[ _-]+files)(?:/|$)")


def _safe_model_id(value) -> str | None:
    """Return a bounded provider model identifier, never arbitrary telemetry.

    Model identity is provider input, not trusted application metadata.  Keep
    the conservative character set already used by deliberation receipts and
    reject URL/path/token-shaped values before they reach a public envelope.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or _SAFE_MODEL_ID.fullmatch(candidate) is None:
        return None
    lowered = candidate.lower()
    if (candidate.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", candidate)
            or lowered.startswith(("file:", "http:", "https:"))
            or lowered.startswith(("sk-", "ghp_", "akia", "eyj"))
            or _MODEL_PATH_PREFIXES.match(candidate)
            or any(segment in (".", "..") for segment in candidate.split("/"))
            or _MODEL_SECRET_MARKERS.search(candidate)):
        return None
    return candidate


def _partial_response(cli: str, result: dict | None, exit_code: int, error: str) -> dict:
    status = "partial" if result else "error"
    return {
        "result": result.get("result", "") if result else "",
        "exit_code": exit_code,
        "status": status,
        "cli": cli,
        "error": error,
        "backend_exit_code": exit_code,
        "dispatcher_status": status,
        "normalization_reason": ("timed out; partial output preserved" if result
                                 else "timed out before any usable output"),
    }


def _error_response(
    cli: str, exit_code: int, error: str, partial_result: dict | None = None,
    *, not_run: bool = False,
) -> dict:
    response = {
        "result": partial_result.get("result", "") if partial_result else "",
        "exit_code": exit_code,
        "status": "error",
        "cli": cli,
        "error": error,
        "backend_exit_code": exit_code,
        "dispatcher_status": "error",
        "normalization_reason": "execution failed before a usable terminal result",
    }
    if not_run:
        response.update({
            "attempts": 0,
            "attempt_status": "not_run",
            "execution_status": "not_run",
            "provider_contacted": False,
            "model": {"requested": None, "targeted": None, "served": None,
                      "resolved": None, "models_used": [], "evidence_source": None},
            "served_model_evidence": "absent",
            "model_match": None,
            "named_model_verified": False,
        })
    return response


def _blocked_response(
    cli: str, error_kind: str, error: str, *, details: dict | None = None
) -> dict:
    """Build a structured, non-contact refusal for deterministic preflight failures."""
    response = _error_response(cli, 1, error, not_run=True)
    response["status"] = "blocked"
    response["dispatcher_status"] = "blocked"
    response["error_kind"] = error_kind
    if details:
        response.update(details)
    return response


def build_final_response(
    cli: str,
    returncode: int | None,
    result: dict | None,
    stdout_lines: list,
    stderr: str,
) -> dict:
    """Assemble the response dict from process exit state and parsed result.

    ``returncode is None`` means the process has not actually finished — that
    is treated as a failure (the original ``or 0`` masked this).
    """
    exit_code = returncode if returncode is not None else 1

    # Kimi's JSONL protocol has no terminal-success record.  Its assistant
    # messages become a result only at clean EOF, so a non-zero process status
    # must win over accumulated text (otherwise a partial reply plus a provider
    # failure would be mislabelled success).
    if cli == "kimi" and exit_code != 0:
        result = None
    result_errored = bool(result) and _terminal_is_error(result)
    if result and not result_errored:
        # Terminal event parsed AND it did not self-report an error -> task
        # completed. A non-zero exit (e.g. from terminate() of a Windows .cmd
        # shim after we got the result) is not a failure.
        status = "success"
        norm_reason = (f"parsed a clean terminal event; normalized to success "
                       f"(raw backend exit {exit_code})") if exit_code not in _SUCCESS_EXIT_CODES \
            else "parsed a clean terminal event (exit and status agree)"
    elif result_errored:
        # The backend's OWN terminal event reported failure (claude is_error /
        # error subtype, gemini/cursor status error). This must surface as an
        # error even though a result object was parsed and the exit may be 0 —
        # otherwise a model/API error would leak through as a false success.
        status = "error"
        norm_reason = "backend terminal event self-reported an error"
    elif exit_code == 0 and "".join(stdout_lines).strip():
        # Plain-text backend that exited cleanly WITH output (no parsed terminal event).
        status = "success"
        norm_reason = "clean exit with output, no terminal event to parse"
    elif exit_code in _SIGTERM_EXIT_CODES and "".join(stdout_lines).strip():
        # SIGTERM but NO terminal event, so this is not our own post-result terminate() (that
        # path lands on the first branch, which has a result). Something OUTSIDE killed the run
        # -- a host-tool timeout, a CI cancel, docker stop -- and the output is whatever the
        # sub-agent had emitted by then. Calling that success is a FALSE SUCCESS: it would also
        # make --out / manifest resume SKIP the re-run and persist the truncated answer.
        status = "partial"
        norm_reason = (f"terminated by an external signal (backend exit {exit_code}) before any "
                       "terminal event; output is incomplete")
    else:
        status = "error"
        norm_reason = f"no usable terminal result and backend exit {exit_code} is not success"
        # A SCRAPE-BASED backend that returns nothing is a different failure from an agent
        # that produced nothing. When the agy legacy wrapper is in use, a terminal-scrape
        # loss can look identical: full wall-clock, non-empty exit code, and no result.
        # That distinction is already knowable here, so say it.
        if cli == "agy" and not (result.get("result") if result else
                                 "".join(stdout_lines)).strip():
            norm_reason = (f"empty terminal scrape after a completed run (backend exit "
                           f"{exit_code}). This usually means the wrapper lost the final "
                           f"line, NOT that the agent declined to answer. Retry before "
                           f"rewriting the prompt.")

    # Missing child tools are useful diagnostics, but captured stdout and model
    # report text are not authority to override a non-zero process or terminal
    # error. Preserve the report for advisory inspection while the execution
    # stays failed; a future trusted wrapper event may distinguish post-result
    # cleanup failure without relying on spoofable text.
    captured = "".join(stdout_lines)
    tool_failure = missing_executable_details(captured)
    terminal_tool_failure = None
    if isinstance(result, dict):
        terminal_tool_failure = missing_executable_details(
            str(result.get("error") or result.get("subtype") or ""))
    if tool_failure is None:
        tool_failure = terminal_tool_failure
    if (tool_failure is not None and exit_code not in _SUCCESS_EXIT_CODES
            and status == "success"):
        status = "error"
        norm_reason = (
            "captured a complete report, but the backend exited non-zero after a "
            "fatal missing-executable failure; report retained as advisory")
    response = {
        "result": result.get("result", "") if result else "".join(stdout_lines),
        "exit_code": exit_code,
        "status": status,
        "cli": cli,
        # Exit-code clarity: keep the raw backend code AND expose the normalized
        # verdict + why they can differ, so a caller never mistakes a normalized
        # success carrying a non-zero backend exit for a process failure (nor
        # ignores a meaningful non-zero backend exit).
        "backend_exit_code": exit_code,
        "raw_backend_exit_code": exit_code,
        "dispatcher_status": status,
        "normalized_exit_code": _normalized_exit_code(status, exit_code),
        "normalization_reason": norm_reason,
    }
    if tool_failure is not None:
        response["tool_failure"] = tool_failure
    if status == "error":
        if result_errored:
            # str() each part: a backend could put a non-string in result/error,
            # and `dict[:200]` would raise TypeError and crash the driver.
            detail = (result.get("subtype") or result.get("error")
                      or str(result.get("result", ""))[:200] or "backend reported an error")
            response["error"] = f"backend reported an error result: {detail}"
        else:
            msg = f"CLI exited with code {exit_code}"
            if stderr and stderr.strip():
                msg += f": {stderr.strip()}"
            # Lead with the backend's OWN failure when it can be identified. stdout and
            # stderr are merged at spawn, so the real reason is usually in the captured
            # body while `error` said only "CLI exited with code 1" -- and any host hook
            # noise sits ahead of it. The full text is still in `output_tail`.
            # Bound the work: failure processing is proportional to captured size, and the
            # cap allows a very large tail. The last 64 KB carries the terminal error in
            # every observed case and keeps this O(small).
            _captured = captured[-65_536:]
            _salient = salient_error(_captured)
            if _salient and _salient not in msg:
                msg = f"{msg}: {_salient}" if not stderr or not stderr.strip() else msg
                # `error_hint`, NOT `backend_error`: this is a line PICKED OUT of untrusted
                # captured output by a heuristic, and naming it as the backend's own error
                # lent attacker-influenceable text an authority it has not earned.
                response["error_hint"] = _salient
            response["error"] = msg
            if host_noise_present(_captured):
                response.setdefault("warnings", []).append(
                    "the captured output contains host/hook noise (a shell or plugin hook "
                    "in your environment wrote to this dispatch's console). It is not from "
                    "the backend and not from summon; on a failure it can read as the "
                    "cause. See `output_tail` for the raw text.")
    if status != "success":
        # Diagnosability: the tail of the RAW captured output (stdout+stderr are
        # merged at spawn), so a failure is inspectable without a re-run.
        response["output_tail"] = "".join(stdout_lines)[-2000:]
    response["_debug_raw"] = "".join(stdout_lines)[-200_000:]
    return response


_LINE = "line"
_EOF = "eof"


def _agy_stream_wrapper(cmd: str | None, args: list) -> bool:
    """Identify the built-in stream-json wrapper among possible legacy wrappers."""
    candidates = [cmd]
    if args:
        candidates.append(args[0])
        if len(args) > 1:
            candidates.append(args[1])
    return any(
        os.path.basename(str(x)).lower() == "agy_stream_proxy.py" for x in candidates if x
    )

# Cap on accumulated stdout codepoints per invocation. Protects the broker
# from OOM if a sub-agent emits high-rate non-terminal output for the full
# wall-clock timeout (default 10 minutes). Counted via len(str) since stdout
# is read in text mode — for ASCII CLI output (the common case) this equals
# bytes; for non-ASCII content the actual memory pressure can be up to ~4×
# this number. 64 M codepoints is a safety net far above realistic transcripts.
_MAX_STDOUT_CHARS = 64 * 1024 * 1024


def _spawn_reader(process: subprocess.Popen) -> queue.Queue:
    """Push each stdout line into a queue from a daemon thread.

    Without this, ``readline()`` blocks indefinitely if the CLI hangs without
    closing stdout — the timeout in :func:`_drive_process` only governs queue
    waits, so the reader thread could otherwise outlive the parent's timeout
    deadline. ``daemon=True`` ensures the thread dies with the interpreter.
    """
    # Bounded so a firehose producer applies backpressure to the reader instead
    # of letting the queue itself balloon before the main loop enforces the
    # char cap. The main loop drains continuously (and on abort via _drain_to_eof),
    # so a blocked put() always frees — no deadlock.
    line_q: queue.Queue = queue.Queue(maxsize=4096)

    def reader() -> None:
        try:
            for line in iter(process.stdout.readline, ""):
                line_q.put((_LINE, line))
        finally:
            line_q.put((_EOF, None))

    threading.Thread(target=reader, daemon=True).start()
    return line_q


def _attach_raw(resp: dict, stdout_lines: list | None) -> dict:
    """Attach the captured-output tail (+ full raw for --debug-dir) to a
    non-success response, so EVERY failure path is diagnosable per the contract.
    build_final_response does this inline; the timeout/cap/IO paths call here."""
    raw = "".join(stdout_lines or [])
    resp["output_tail"] = raw[-2000:]
    resp["_debug_raw"] = raw[-200_000:]
    return resp


def _timeout_payload(cli: str, processor: StreamProcessor, timeout_ms: int,
                     stdout_lines: list | None = None) -> dict:
    """Timeout envelope, with the diagnostic promoted out of `output_tail`.

    A timeout whose `result` is empty used to say only "Timeout after Nms" while the real
    cause -- a missing tool, a wrong shell, a prompt for input nobody could answer -- sat in
    `output_tail`. Callers branch on status/result/report, so the actionable content was in
    the one field they had no reason to read (field report, 2026-07-28: 480s burned on
    `rg` not being on the child's PATH, reported as an opaque timeout).

    Three additions, none of which invent an answer:
      * `partial_output_only: true` when the run produced captured text but no result, so a
        caller can branch on "there is something to read" without guessing;
      * the salient line promoted into `error_hint`, using the same untrusted-text
        extraction as the failure path (prompt echoes excluded upstream);
      * a warning naming `output_tail` and, when a session id survived, the fact that the
        run is RESUMABLE -- summon already preserves `resume.session_id` through a timeout,
        which reads as total loss when `result` is empty.
    """
    result = processor.get_result()
    # See _cli.Milliseconds: its string form carries a unit for safe argv
    # forwarding. Diagnostic text owns its own unit, so format the numeric value
    # explicitly rather than producing ``360000msms``.
    timeout_budget_ms = int(timeout_ms)
    resp = _partial_response(cli, result, 124, f"Timeout after {timeout_budget_ms}ms")
    captured = "".join(stdout_lines or [])
    resp["timeout"] = {"budget_ms": timeout_budget_ms,
                       "stage": "backend-execution",
                       "partial_output": bool(result) or bool(captured.strip())}
    resp = _attach_raw(resp, stdout_lines)

    # `processor.get_result()` returns the parsed result JSON (a dict) or None -- NOT a
    # string. An earlier draft called .strip() on it, which would have raised
    # AttributeError on every timeout that DID produce a result, i.e. crashed the exact
    # path it was meant to improve. Truthiness covers dict, str and None alike.
    _empty = not result if not isinstance(result, str) else not result.strip()
    if _empty and captured.strip():
        resp["partial_output_only"] = True
        hint = salient_error(captured)
        if hint:
            resp["error_hint"] = hint
            resp["error"] = f"Timeout after {timeout_budget_ms}ms -- likely cause: {hint}"
        resp.setdefault("warnings", []).append(
            "this run timed out with no parsed result; the captured output is in "
            "`output_tail` and usually names the real cause (a missing tool, a wrong "
            "shell, or a prompt waiting for input).")
    elif _empty and cli == "opencode":
        # OpenCode can wait for an interactive provider-auth flow while running
        # headlessly. With no output there is no evidence that the provider was
        # contacted, so do not label this an authentication failure or silently
        # spend another turn. Make the operator-visible recovery explicit and
        # require an intentional retry after local auth is checked.
        resp.update({
            "error_kind": "provider_timeout",
            "retryable": False,
            "result_usable": False,
            "remediation_code": "opencode_output_timeout",
        })
        resp["error"] = (
            f"Timeout after {timeout_budget_ms}ms; OpenCode produced no usable "
            "output. For an OpenRouter model, verify the provider credential "
            "with `opencode auth login` (or the configured local credential), "
            "then retry explicitly; Summon did not retry or switch providers."
        )
        resp.setdefault("warnings", []).append(
            "OpenCode emitted no output before the deadline; authentication or "
            "transport state is unproven, so no automatic retry was attempted.")
    return resp


def _drain_to_eof(line_q: queue.Queue, budget_sec: float = 0.5) -> None:
    """Best-effort: consume the reader queue until _EOF or short budget.

    Used after kill() so that ``communicate()`` reads stderr without racing
    the reader thread on stdout. Safe to call when the reader is already
    done — the queue already holds an _EOF sentinel.
    """
    deadline = time.monotonic() + budget_sec
    while time.monotonic() < deadline:
        try:
            kind, _ = line_q.get(timeout=0.05)
        except queue.Empty:
            return
        if kind == _EOF:
            return


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the child AND its descendants. ``process.kill()`` alone reaps only the
    immediate child; a grandchild (the real backend behind a .cmd/node/powershell
    shim) can keep the stdout pipe open, so ``communicate()`` blocks past the
    deadline — the observed way the wall-clock timeout was defeated. Windows:
    ``taskkill /T`` walks the tree. POSIX: signal the session group (Popen is
    launched with ``start_new_session`` so the child leads its own group)."""
    try:
        if os.name == "nt":
            # Popen keeps the process handle open, so Windows will NOT recycle
            # this PID mid-teardown — taskkill /T targets the right tree WHILE THE
            # LEADER IS ALIVE. KNOWN GAP (see KNOWN_ISSUES.md / #10): taskkill walks
            # parent->child PID links, so once this leader (run_subagent.py) has
            # exited, a still-running backend grandchild is orphaned. The correct fix
            # is a Windows Job Object (kill the tree independent of leader lifetime);
            # POSIX's killpg below already reaches the group through a dead leader.
            # A Job Object kills every member regardless of who has already exited, so
            # it is tried FIRST; taskkill remains the fallback for a child that could not
            # be assigned (nested-job restrictions, older Windows).
            from _jobobj import terminate as _job_terminate
            if _job_terminate(process):
                return
            from _spawn import run_flags
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=10, **run_flags())
        else:
            # start_new_session=True makes the child its own group leader, so the
            # PGID equals the child PID. Signal the group by PID directly instead
            # of os.getpgid(pid) — getpgid raises if the child was already reaped
            # (child exited but a grandchild still holds stdout), which would skip
            # the kill and orphan the grandchild.
            #
            # POSIX residual (accepted): unlike Windows, which holds the process
            # handle open and so blocks PID reuse mid-teardown, POSIX offers no group
            # handle, so a reaped leader's PID could in principle be recycled to a new
            # group leader before this killpg fires. The council's kill loop only ever
            # snapshots procs still registered in `inflight`; a member's on_reap callback
            # unregisters it the instant communicate() reaps the leader (before any
            # envelope file read), so the exposure is the MICROSECONDS between that reap
            # and on_reap running, AND only if the recycled PID re-becomes a group leader
            # within it. We accept that narrow window over skipping the kill (which would
            # orphan a stdout-holding grandchild and re-open the wall-clock hang).
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:  # noqa: BLE001 — best-effort teardown must never raise
        pass
    try:
        process.kill()
    except Exception:  # noqa: BLE001
        pass


def _safe_communicate(process: subprocess.Popen, timeout: float = 3.0):
    """``communicate()`` bounded by a timeout so a descendant still holding stdout
    cannot hang the driver indefinitely after we've already blown the deadline.
    On expiry, kill the whole tree and try once more, then give up cleanly.
    Kept short (two brief waits) since the tree-kill already ran before we call."""
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            return process.communicate(timeout=timeout)
        except Exception:  # noqa: BLE001
            return (None, None)
    except (OSError, ValueError):
        return (None, None)


def _drive_process(process: subprocess.Popen, cli: str, timeout_ms: int,
                   parse_stream: bool | None = None,
                   launch_control: ProviderLaunchControl | None = None,
                   expected_model: str | None = None,
                   attempt_id: str | None = None,
                   first_event_ms: int | None = None,
                   idle_ms: int | None = None,
                   finalization_ms: int | None = None,
                   attempt_kind: str = "initial",
                   attempt_ordinal: int = 1) -> dict:
    """Drive the subprocess and enrich whatever response path it takes.

    Single choke point: every return from the read loop (success, timeout,
    output-cap abort, I/O error) passes through ``_enrich`` so callers always
    see the same telemetry/report keys.
    """
    if parse_stream is None:
        # arkcli +chat emits plain / pretty JSON, not CLI stream-json events.
        parse_stream = cli not in ("agy", "arkcli")
    from _liveness import LivenessTracker
    from _job_control import RuntimeControl
    runtime_control = RuntimeControl.from_environment(
        timeout_ms, attempt_id=attempt_id, attempt_kind=attempt_kind,
        attempt_ordinal=attempt_ordinal)
    hard_budget_ms = (runtime_control.max_runtime_ms
                      if runtime_control is not None else timeout_ms)
    # In adaptive mode, --timeout is an observation checkpoint. The liveness
    # tracker's hard clocks bind only the separately declared max runtime;
    # RuntimeControl decides whether activity merits another lease interval.
    tracker = LivenessTracker(
        attempt_id=attempt_id or uuid.uuid4().hex,
        overall_ms=hard_budget_ms,
        first_event_ms=first_event_ms or hard_budget_ms,
        idle_ms=idle_ms or hard_budget_ms,
        finalization_ms=finalization_ms or min(hard_budget_ms, 120_000),
    )
    emitter = tracker.emitter()
    processor = StreamProcessor(event_observer=emitter)
    try:
        from _stream_partials import emit_partial as _emit_partial
        _emit_partial("started", cli=cli, message="subprocess started")
    except Exception:  # noqa: BLE001
        pass
    response = _drive_process_loop(process, cli, timeout_ms, processor,
                                   parse_stream=parse_stream,
                                   launch_control=launch_control,
                                   expected_model=expected_model,
                                   liveness=tracker,
                                   liveness_emitter=emitter,
                                   runtime_control=runtime_control)
    enriched = _enrich(response, processor)
    enriched["liveness"] = tracker.snapshot()
    if runtime_control is not None:
        runtime_control.publish(enriched["liveness"], force=True)
        enriched["runtime_control"] = runtime_control.projection()
    return enriched


def _drive_process_loop(
    process: subprocess.Popen, cli: str, timeout_ms: int, processor: StreamProcessor,
    parse_stream: bool = True,
    launch_control: ProviderLaunchControl | None = None,
    expected_model: str | None = None,
    liveness=None,
    liveness_emitter=None,
    runtime_control=None,
) -> dict:
    """Read process stdout via StreamProcessor, enforce a wall-clock deadline.

    The wall-clock deadline covers the entire subprocess lifetime — including
    cases where the CLI never produces stdout, blocks on stderr, or stops
    emitting lines. A blocking ``readline()`` in the main thread would never
    reach the timeout check, so reads are delegated to a background thread
    and observed via a queue.

    After a terminal event is parsed we keep draining the queue until the
    reader thread reports EOF before calling ``communicate()`` — that way
    only one consumer ever reads ``process.stdout``.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    # Non-stream CLIs can still return useful plain output on non-zero status; only
    # the wrapper that emits line-delimited JSON events is safe to parse.
    parse_stream = bool(parse_stream)
    stdout_lines: list = []
    accumulated_chars = 0
    line_q = _spawn_reader(process)
    saw_terminal = False
    terminal_reap_deadline = None

    def _finish_forced_terminal_cleanup(stage: str) -> dict:
        """Reap a child that outlived a trusted terminal event, preserving it."""
        _kill_tree(process)
        _drain_to_eof(line_q)
        _, stderr = _safe_communicate(process)
        processor.finalize_stream()
        response = build_final_response(
            cli, process.returncode, processor.get_result(), stdout_lines, stderr)
        response["cleanup"] = {
            "forced": True,
            "stage": stage,
            "reason": "backend_process_outlived_terminal_event",
        }
        response.setdefault("warnings", []).append(
            "Summon preserved the trusted terminal result but force-reaped the "
            "backend process after its bounded exit grace.")
        return response

    def _reap_after_driver_failure() -> None:
        """Best-effort ownership cleanup for an exception leaving the read loop.

        Cleanup must not replace the exception that brought us here.  In
        particular, ``_drain_to_eof`` is intentionally outside the ordinary
        success path and may itself encounter a damaged queue during interpreter
        teardown.  The process/tree kill and bounded communicate still run.
        """
        _kill_tree(process)
        try:
            _drain_to_eof(line_q)
        except Exception:  # noqa: BLE001 - preserve the original driver failure
            pass
        _safe_communicate(process)

    def _parsed_terminal_available() -> bool:
        if saw_terminal:
            return True
        try:
            # StreamProcessor.result_json is assigned only by recognized
            # terminal branches; advisory partial text lives elsewhere.
            return processor.get_result() is not None
        except Exception:  # a broken parser accessor cannot mask the driver failure
            return False

    try:
        while True:
            if (saw_terminal and terminal_reap_deadline is not None
                    and time.monotonic() >= terminal_reap_deadline):
                return _finish_forced_terminal_cleanup("terminal_reap_timeout")
            if runtime_control is not None:
                runtime_control.refresh()
                runtime_control.checkpoint(
                    active=liveness.meaningful_within(runtime_control.checkpoint_ms))
                runtime_control.publish(liveness.snapshot())
                if runtime_control.cancel_requested and not saw_terminal:
                    if liveness_emitter is not None:
                        liveness_emitter.emit("cancelled", session_id=processor.session_id)
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    cancelled = _error_response(
                        cli, 130, "background job cancelled by operator",
                        partial_result=processor.get_result())
                    cancelled.update({"status": "blocked", "dispatcher_status": "blocked",
                                      "execution_status": "cancelled",
                                      "error_kind": "operator_cancelled",
                                      "retryable": False, "result_usable": False})
                    return _attach_raw(cancelled, stdout_lines)
                if not saw_terminal and runtime_control.expired():
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    timed = _timeout_payload(cli, processor, timeout_ms, stdout_lines)
                    timed.setdefault("timeout", {})["stage"] = (
                        "adaptive_attention_timeout" if runtime_control.attention_required
                        else "adaptive_hard_timeout")
                    return timed
            if not saw_terminal and liveness is not None:
                reason = liveness.expired()
                if reason is not None:
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    timed = _timeout_payload(cli, processor, timeout_ms, stdout_lines)
                    timed.setdefault("timeout", {})["stage"] = reason
                    return timed
            # Once a trusted terminal event has been parsed, provider-side
            # cancellation/deadline state may only influence cleanup.  It must
            # never replace the terminal result while the backend is being
            # given its bounded reap grace.
            if launch_control is not None and not saw_terminal:
                try:
                    _controlled_cancel = launch_control.is_cancelled()
                except Exception as exc:
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    return _attach_raw(_error_response(
                        cli, 130,
                        f"provider cancellation check failed ({type(exc).__name__})",
                        partial_result=processor.get_result()), stdout_lines)
                if _controlled_cancel:
                    if liveness_emitter is not None:
                        liveness_emitter.emit("cancelled", session_id=processor.session_id)
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    return _attach_raw(_error_response(
                        cli, 130, "provider launch cancelled by deliberation",
                        partial_result=processor.get_result()), stdout_lines)
                try:
                    if launch_control.is_deadline_reached():
                        _kill_tree(process)
                        _drain_to_eof(line_q)
                        _safe_communicate(process)
                        return _attach_raw(
                            _timeout_payload(cli, processor, timeout_ms, stdout_lines),
                            stdout_lines)
                except ProviderDeadlineError as exc:
                    _kill_tree(process)
                    _drain_to_eof(line_q)
                    _safe_communicate(process)
                    return _attach_raw(_error_response(
                        cli, 1, f"provider deadline check failed ({type(exc).__name__})",
                        partial_result=processor.get_result()), stdout_lines)
            remaining = ((runtime_control.hard_deadline if runtime_control is not None
                          else deadline) - time.monotonic())
            if remaining <= 0:
                _kill_tree(process)
                _drain_to_eof(line_q)
                _safe_communicate(process)
                return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

            try:
                wait_for = remaining
                if launch_control is not None:
                    wait_for = min(wait_for, 0.1)
                if liveness is not None:
                    next_ms = liveness.next_deadline_ms()
                    if next_ms is not None:
                        wait_for = min(wait_for, max(0.001, next_ms / 1000))
                if runtime_control is not None:
                    wait_for = min(wait_for, 0.25,
                                   max(0.001, runtime_control.deadline
                                       - time.monotonic()))
                if saw_terminal and terminal_reap_deadline is not None:
                    wait_for = min(wait_for, max(
                        0.001, terminal_reap_deadline - time.monotonic()))
                kind, line = line_q.get(timeout=wait_for)
            except queue.Empty:
                if ((launch_control is not None or liveness is not None)
                        and remaining > 0):
                    continue
                _kill_tree(process)
                _drain_to_eof(line_q)
                _safe_communicate(process)
                return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

            if kind == _EOF:
                break
            stdout_lines.append(line)
            accumulated_chars += len(line)
            if accumulated_chars > _MAX_STDOUT_CHARS:
                # Defensive cap: a sub-agent emitting unbounded non-terminal
                # output would otherwise grow stdout_lines until the wall-clock
                # deadline (default 10 min). Kill it and report partial.
                _kill_tree(process)
                _drain_to_eof(line_q)
                _safe_communicate(process)
                return _attach_raw(_error_response(
                    cli,
                    1,
                    f"Sub-agent stdout exceeded {_MAX_STDOUT_CHARS} characters; aborted",
                    partial_result=processor.get_result(),
                ), stdout_lines)
            if parse_stream and not saw_terminal and processor.process_line(line):
                # Processor saw a terminal event; ask the CLI to exit cleanly,
                # but keep looping so the reader thread can drain stdout to EOF.
                # Bind terminal precedence BEFORE terminate(): CreateProcess/
                # handle teardown can itself raise after the parser has already
                # accepted the provider's terminal record.
                saw_terminal = True
                process.terminate()
                terminal_grace_ms = (
                    liveness.finalization_ms if liveness is not None else 120_000)
                terminal_reap_deadline = min(
                    runtime_control.hard_deadline if runtime_control is not None else deadline,
                    time.monotonic() + terminal_grace_ms / 1000)
            if (parse_stream and cli == "codex" and expected_model
                    and processor.handshake_model
                    and _model_mismatch(expected_model, processor.handshake_model)):
                # A Codex handshake that points at a different model is already
                # enough to reject an exact request. Stop the provider tree now;
                # do not spend the rest of the turn or let later output make the
                # mismatch look like a successful run.
                _kill_tree(process)
                _drain_to_eof(line_q)
                _safe_communicate(process)
                blocked = _error_response(
                    cli, 1,
                    f"requested Codex model {expected_model!r}, but the provider "
                    f"handshake targeted {processor.handshake_model!r}")
                blocked.update({
                    "status": "blocked", "dispatcher_status": "blocked",
                    "execution_status": "blocked",
                    "error_kind": "target_model_mismatch",
                    "retryable": False, "result_usable": False,
                    "provider_contacted": True,
                })
                return _attach_raw(blocked, stdout_lines)

        # stdout fully drained by reader; communicate() only needs stderr.
        # EOF is not proof that the child exited: a provider may close stdout
        # and continue finalizing. Keep cancellation/liveness polling active.
        if liveness_emitter is not None and liveness.snapshot()["phase"] not in {
                "terminal", "cancelled", "timed_out"}:
            liveness_emitter.emit("finalizing", session_id=processor.session_id)
        finalization_deadline = time.monotonic() + (
            liveness.finalization_ms if liveness is not None else 120_000) / 1000
        if terminal_reap_deadline is not None:
            post_eof_deadline = terminal_reap_deadline
        elif runtime_control is not None:
            # Adaptive jobs still honor their explicit hard cap.  Ordinary jobs
            # get a real post-EOF finalization grace even when stdout closes at
            # the original dispatch deadline.
            post_eof_deadline = min(
                runtime_control.hard_deadline, finalization_deadline)
        else:
            post_eof_deadline = finalization_deadline
        while process.poll() is None:
            if runtime_control is not None:
                runtime_control.refresh()
                runtime_control.publish(liveness.snapshot())
                if runtime_control.cancel_requested and not saw_terminal:
                    if liveness_emitter is not None:
                        liveness_emitter.emit("cancelled", session_id=processor.session_id)
                    _kill_tree(process)
                    _safe_communicate(process)
                    return _attach_raw(_error_response(
                        cli, 130, "background job cancelled during finalization",
                        partial_result=processor.get_result()), stdout_lines)
            if time.monotonic() >= post_eof_deadline:
                if saw_terminal:
                    return _finish_forced_terminal_cleanup(
                        "post_eof_terminal_reap_timeout")
                _kill_tree(process)
                _safe_communicate(process)
                timed = _timeout_payload(cli, processor, timeout_ms, stdout_lines)
                timed.setdefault("timeout", {})["stage"] = "finalization_timeout"
                return timed
            reason = (liveness.expired()
                      if not saw_terminal and liveness is not None else None)
            if reason is not None:
                _kill_tree(process)
                _safe_communicate(process)
                timed = _timeout_payload(cli, processor, timeout_ms, stdout_lines)
                timed.setdefault("timeout", {})["stage"] = reason
                return timed
            if (launch_control is not None and not saw_terminal
                    and launch_control.is_cancelled()):
                if liveness_emitter is not None:
                    liveness_emitter.emit("cancelled", session_id=processor.session_id)
                _kill_tree(process)
                _safe_communicate(process)
                return _attach_raw(_error_response(
                    cli, 130, "provider launch cancelled during finalization",
                    partial_result=processor.get_result()), stdout_lines)
            time.sleep(0.05)

        _, stderr = _safe_communicate(process)

        processor.finalize_stream()
        return build_final_response(
            cli, process.returncode, processor.get_result(), stdout_lines, stderr
        )
    except KeyboardInterrupt:
        # start_new_session detaches the child from the terminal's signal group,
        # so a Ctrl+C on the parent won't reach it — tree-kill it ourselves so an
        # interrupt doesn't leave an orphaned backend running.
        if liveness_emitter is not None:
            try:
                liveness_emitter.emit("cancelled", session_id=processor.session_id)
            except Exception:  # cancellation telemetry must not mask Ctrl+C
                pass
        _reap_after_driver_failure()
        raise
    except (OSError, ValueError) as e:
        # OSError covers I/O failures on the pipe; ValueError covers reading
        # from a closed file. If a trusted terminal event was already parsed,
        # preserve it: a later driver/pipe failure is cleanup evidence, not a
        # reason to replace the provider's terminal result.
        if _parsed_terminal_available():
            try:
                return _finish_forced_terminal_cleanup(
                    "driver_io_exception_after_terminal")
            except Exception:  # noqa: BLE001 - report the original I/O failure
                pass
        _reap_after_driver_failure()
        return _attach_raw(_error_response(
            cli, 1, f"{type(e).__name__}: {e}", partial_result=processor.get_result()
        ), stdout_lines)
    except BaseException as exc:
        # An unexpected driver/callback/parser exception must never orphan a paid
        # provider process. Ordinary Exceptions after a trusted terminal event
        # preserve that result; process-control BaseExceptions (KeyboardInterrupt,
        # SystemExit, GeneratorExit) are always re-raised after cleanup.
        if _parsed_terminal_available() and isinstance(exc, Exception):
            try:
                return _finish_forced_terminal_cleanup(
                    "driver_exception_after_terminal")
            except Exception:  # noqa: BLE001 - never mask the original exception
                pass
        _reap_after_driver_failure()
        raise


def _resolve_launch(command, args):
    """Resolve a backend command to a directly-launchable executable on Windows.

    npm CLIs (codex, gemini) install as .cmd shims with no .exe. Launching a .cmd
    routes CPython through cmd.exe, which cannot resolve the bare name and mangles
    multi-line / metachar argv (breaking codex, whose prompt is built multi-line).
    Resolve to the real native binary so argv is passed verbatim. POSIX returns the
    resolved path. Returns (command, args).
    """
    # Cursor CLI: cursor-agent.cmd -> powershell -> node index.js. Launch node
    # directly so the multi-line prompt argv is not mangled by cmd.exe/powershell.
    if os.name == "nt" and command == "cursor-agent":
        _ca = shutil.which(command)
        _bases = ([os.path.dirname(_ca)] if _ca else [])
        _la = os.environ.get("LOCALAPPDATA")
        if _la:
            _bases.append(os.path.join(_la, "cursor-agent"))
        for _b in _bases:
            if os.path.isfile(os.path.join(_b, "node.exe")) and os.path.isfile(os.path.join(_b, "index.js")):
                return os.path.join(_b, "node.exe"), [os.path.join(_b, "index.js"), *args]
            _vd = os.path.join(_b, "versions")
            if os.path.isdir(_vd):
                _vs = [d for d in glob.glob(os.path.join(_vd, "*"))
                       if os.path.isfile(os.path.join(d, "node.exe"))
                       and os.path.isfile(os.path.join(d, "index.js"))]
                if _vs:
                    def _vkey(d):
                        _p = os.path.basename(d).split("-")[0].split(".")
                        try:
                            return int(_p[0] + _p[1].zfill(2) + _p[2].zfill(2))
                        except Exception:
                            return 0
                    _latest = max(_vs, key=_vkey)
                    return (os.path.join(_latest, "node.exe"),
                            [os.path.join(_latest, "index.js"), *args])
    # arkcli npm shim: CreateProcess cannot exec .cmd without cmd.exe.
    # Prefer node + scripts/run.js so user argv is NOT re-parsed by cmd.exe.
    if os.name == "nt" and command in ("arkcli", "arkcli.cmd"):
        _ark = shutil.which("arkcli.cmd") or shutil.which("arkcli") or command
        if str(_ark).lower().endswith((".cmd", ".bat")):
            try:
                from _arkcli_backend import _arkcli_node_entry
                _js = _arkcli_node_entry(_ark)
            except Exception:  # noqa: BLE001
                _js = None
            if _js:
                return (shutil.which("node") or "node"), [_js, *args]
            return "cmd.exe", ["/c", _ark, *args]
        return _ark, args
    # OpenCode's npm install exposes an .cmd/.ps1 shim that launches the
    # bundled native binary.  CreateProcess cannot execute the shim directly;
    # resolve the binary so multiline prompts and JSON event output travel
    # through argv unchanged.
    if os.name == "nt" and command == "opencode":
        # Resolve the npm shim's *own* bundled binary first.  A standalone
        # Windows installation may also put an ``opencode.exe`` on PATH, but
        # that executable is commonly the desktop launcher and exits cleanly
        # without emitting the CLI's JSON stream.  Selecting it merely because
        # it sorts before the npm shim makes a Summon turn wait until timeout.
        _oc = shutil.which("opencode.cmd") or shutil.which("opencode") or command
        _oc_dir = os.path.dirname(_oc) if _oc else ""
        _oc_exe = os.path.join(_oc_dir, "node_modules", "opencode-ai", "bin", "opencode.exe")
        if os.path.isfile(_oc_exe):
            return _oc_exe, args
        # If an operator supplies a direct executable (rather than the npm
        # shim), preserve that explicit path.  Do not guess from a generic
        # ``opencode.exe`` PATH hit: it may be the desktop launcher described
        # above.
        if str(_oc).lower().endswith(".exe") and os.path.isfile(_oc):
            return _oc, args
        if str(_oc).lower().endswith((".cmd", ".bat")):
            return "cmd.exe", ["/c", _oc, *args]
        return _oc, args
    resolved = shutil.which(command) or command
    if os.name != "nt" or not resolved.lower().endswith((".cmd", ".bat")):
        return resolved, args
    shim_dir = os.path.dirname(resolved)
    if command == "codex":
        patterns = [
            os.path.join(shim_dir, "node_modules", "@openai", "codex", "node_modules",
                         "@openai", "codex-*", "vendor", "*", "codex", "codex.exe"),
            os.path.join(shim_dir, "node_modules", "@openai", "codex-*", "vendor",
                         "*", "codex", "codex.exe"),
        ]
        for pat in patterns:
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[0], args
        js = os.path.join(shim_dir, "node_modules", "@openai", "codex", "bin", "codex.js")
        if os.path.isfile(js):
            return (shutil.which("node") or "node"), [js, *args]
    return resolved, args


def _write_debug(debug_dir: str, argv: list, raw: str, response: dict) -> str | None:
    """Dump the raw captured output + argv + final envelope for one run.
    Fail-soft: diagnostics must never break the dispatch itself."""
    import json as _json
    import uuid as _uuid
    try:
        os.makedirs(debug_dir, exist_ok=True)
        # uuid suffix so a same-second same-pid schema-correction retry can't
        # overwrite the first dispatch's transcript.
        name = f"{int(time.time())}-{response.get('cli')}-{os.getpid()}-{_uuid.uuid4().hex[:8]}.log"
        path = os.path.join(debug_dir, name)
        nl = "\n"
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("# argv (prompt truncated to 2000 chars per token)" + nl)
            fh.write(_sanitize_argv([str(a) for a in argv]))
            fh.write(nl + nl + "# raw captured output (stdout+stderr merged)" + nl)
            fh.write(raw or "(none)")
            fh.write(nl + nl + "# final envelope" + nl)
            fh.write(_json.dumps(response, ensure_ascii=False, indent=1))
        try:
            os.chmod(path, 0o600)
        except Exception:  # noqa: BLE001
            pass
        return path
    except OSError:
        return None


def execute_agent(inv: AgentInvocation, timeout_ms: int = 600000,
                  debug_dir: str | None = None,
                  max_tool_output_bytes: int | None = None,
                  *, launch_control: ProviderLaunchControl | None = None) -> dict:
    """Execute agent CLI for the given invocation. Returns a response dict.

    Response shape: ``{result, exit_code, status, cli, error?}`` plus the
    telemetry/trust fields documented in SKILL.md (report, model, permission,
    elapsed_ms, ...). ``max_tool_output_bytes`` sets the bare-base64 elision
    threshold for the human-facing output_tail (None -> the built-in default).
    """
    started = time.monotonic()
    started_wall_ms = int(time.time() * 1000)
    # A physical attempt is the unit used for lifecycle accounting.  Keep the
    # value opaque and bounded; the retry loop supplies a fresh ID for each
    # provider launch, while direct callers get one lazily here.  Structural
    # preflight refusals intentionally never publish it.
    _attempt_id = getattr(inv, "attempt_id", None)
    if not isinstance(_attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", _attempt_id) is None:
        _attempt_id = uuid.uuid4().hex
    # Credit-only model guard: build_invocation_args enforces it in the
    # argv/env (so --dry-run and real dispatch agree); here we keep the ORIGINAL
    # request, the GUARDED effective model (feeds model.targeted), and the guard
    # warnings for the envelope's transparency.
    try:
        from _builder import codex_model_selection
        _codex_selection = (codex_model_selection(
                                inv.model, inv.extra_args,
                                getattr(inv, "model_source", None))
                            if inv.cli == "codex" else {
                                "requested": inv.model,
                                "canonical": inv.model,
                                "exact_required": False,
                                "conflict": None,
                                "source": "invocation" if inv.model else "ambient_config",
                            })
    except Exception:  # noqa: BLE001 — malformed legacy invocation is handled below
        _codex_selection = {
            "requested": inv.model,
            "canonical": inv.model,
            "exact_required": bool(inv.model),
            "conflict": None,
            "source": "invocation" if inv.model else "ambient_config",
        }
    _requested_model = _codex_selection.get("requested")
    _exact_required = bool(
        getattr(inv, "model_exact_required", False)
        or (inv.cli == "codex" and _codex_selection.get("exact_required"))
    )
    _guarded_inv, _, _guard_warnings = apply_credit_guard(inv)
    debug_argv = [inv.cli]  # what --debug-dir records; each path refines it
    # Defer the initial workspace snapshot until an actual backend spawn is known to fit.
    # An over-long argv must not cause any utility Popen merely to build its refusal
    # envelope; the after snapshot is still taken by _stamp after cleanup.
    try:
        from _receipt import workspace_evidence, workspace_snapshot
        _workspace_before = None
    except Exception:  # noqa: BLE001 — mutation evidence is additive and fail-closed
        workspace_evidence = None
        workspace_snapshot = None
        _workspace_before = {"coverage": "unavailable", "error": "workspace unavailable"}

    def _stamp(resp: dict) -> dict:
        # Wall-clock per dispatch — orchestrators need this for concurrency
        # tuning and it costs nothing to provide.
        resp["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        # Provider contact is the boundary between a structural refusal and a
        # physical attempt.  Only the latter receives an attempt identity and
        # completed attempt state; this prevents preflight errors from being
        # counted as spent work while making retries/fallbacks auditable.
        if resp.get("provider_contacted") is True:
            resp.setdefault("attempt_id", _attempt_id)
            _attempts = resp.get("attempts")
            if (not isinstance(_attempts, int) or isinstance(_attempts, bool)
                    or _attempts < 1):
                resp["attempts"] = 1
            if resp.get("attempt_status") != "not_run":
                resp["attempt_status"] = "completed"
        elif resp.get("attempt_status") == "not_run":
            resp.pop("attempt_id", None)
        # Additive SPI fields (provider.driver / backend_type / served.via).
        try:
            from _drivers import enrich_envelope_from_cli as _enrich_spi
            _enrich_spi(resp, inv.cli, getattr(inv, "transport", "subprocess") or "subprocess")
        except Exception:  # noqa: BLE001 — annotation must never break a dispatch
            pass
        # Exit-code clarity on EVERY envelope (api-kind backends and other paths
        # build their own response and never touch build_final_response). The
        # response builders set the detailed reason first; this preserves it.
        finalize_exit_fields(resp)
        if "workspace_evidence" not in resp:
            try:
                if _workspace_before is None:
                    # Pre-dispatch validation rejected the request before a safe
                    # baseline could be captured. Do not run another utility child
                    # merely to decorate that refusal; unknown is the honest result.
                    resp["workspace_evidence"] = {
                        "before": None, "after": None, "coverage": "unavailable",
                        "child_commit": None, "mutation": None,
                        "read_only_violation": None, "attribution": "unavailable",
                    }
                else:
                    _after = workspace_snapshot(inv.cwd)
                    resp["workspace_evidence"] = workspace_evidence(
                        _workspace_before, _after, getattr(inv, "permission", None))
            except Exception:  # noqa: BLE001 — never hide the terminal response
                resp["workspace_evidence"] = {
                    "before": None, "after": None, "coverage": "unavailable",
                    "child_commit": None, "mutation": None,
                    "read_only_violation": None, "attribution": "unavailable",
                }
        # Trust fields, split by EVIDENCE (field case: a failed Fable dispatch
        # reported the handshake model as `resolved` with all-zero usage):
        #   requested  what the caller asked for (unchanged).
        #   targeted   what the session was POINTED AT: the init handshake, else
        #              the post-credit-guard effective model, else the backend's
        #              knowable default (cursor pin, codex config).
        #   served     ONLY on service evidence — a terminal-event model report,
        #              or output tokens with a known target. Task status is NOT
        #              evidence (a served run can be downgraded to blocked).
        #   resolved   LEGACY compatibility field: handshake-or-terminal, with
        #              the codex config backfill only for unpinned requests.
        #              An explicit pin never inherits that ambient default;
        #              consumers should migrate to targeted/served.
        _terminal_model_raw = resp.pop("model_resolved", None)
        _terminal_model = _safe_model_id(_terminal_model_raw)
        _terminal_model_invalid = (_terminal_model_raw is not None
                                   and _terminal_model is None)
        _handshake = _safe_model_id(resp.pop("model_targeted", None))
        _mu_raw = resp.pop("models_used", [])
        _mu = []
        if isinstance(_mu_raw, (list, tuple)):
            _mu = [model for model in (_safe_model_id(value) for value in _mu_raw)
                   if model is not None]
        _effective = _safe_model_id(
            _codex_selection.get("canonical") if inv.cli == "codex"
            else _guarded_inv.model)
        if not _effective:
            if inv.cli == "cursor-agent":
                from _builder import CURSOR_DEFAULT_MODEL as _cursor_default
                _effective = _safe_model_id(_cursor_default)
            elif inv.cli == "codex":
                try:
                    from _resolver import _codex_default_model
                    _effective = _safe_model_id(_codex_default_model())
                except Exception:  # noqa: BLE001 — telemetry best-effort, never fatal
                    _effective = None
        _out_tokens = 0
        if isinstance(resp.get("usage"), dict):
            for _k in ("output_tokens", "completion_tokens"):
                _v = resp["usage"].get(_k)
                if isinstance(_v, (int, float)) and not isinstance(_v, bool):
                    _out_tokens = max(_out_tokens, _v)
        # STRICTLY handshake-then-effective: the terminal model is SERVED
        # evidence and must never pollute what the session was pointed at
        # (they can legitimately differ, and that difference is the signal).
        _targeted = _handshake or _effective
        if _terminal_model and not _terminal_model_invalid:
            _served = _terminal_model
        elif (_out_tokens > 0 and _targeted
              and not _exact_required):
            _served = None if _terminal_model_invalid else _targeted
        else:
            _served = None
        # Provenance is separate from the model name.  A terminal provider
        # report is reported evidence; output tokens plus a target are only an
        # inference; neither is absent evidence.  Do not overload suspect/cache
        # semantics for a valid success that lacks provider telemetry.
        _evidence_source = resp.get("model_evidence_source")
        # Kimi 0.38 has no authoritative provider identity channel. Any model
        # id visible through its process is child-observed, regardless of which
        # parser branch produced it.
        _client_observed_kimi = inv.cli == "kimi"
        if _terminal_model and not _terminal_model_invalid:
            # Kimi 0.38 exposes model ids only in stdout/profile artifacts that
            # the child process itself can write. They are useful routing
            # observations, but not an authoritative provider receipt and must
            # never certify a named-model vote.
            resp["served_model_evidence"] = (
                "inferred" if _client_observed_kimi else "reported")
        elif (_out_tokens > 0 and _targeted and not _terminal_model_invalid
              and not _exact_required):
            resp["served_model_evidence"] = "inferred"
        else:
            resp["served_model_evidence"] = "absent"
        if resp.get("status") == "success" and resp["served_model_evidence"] == "absent":
            _warning = (
                "served model evidence was unavailable; model provenance is "
                "not confirmed")
            _warnings = resp.setdefault("warnings", [])
            if _warning not in _warnings:
                _warnings.append(_warning)
        _legacy = _terminal_model or _handshake
        # An explicit Codex model is a trust contract.  If Codex emits neither
        # a handshake nor a terminal model, do not backfill ``resolved`` from
        # the user's ambient config: that default is not evidence about this
        # invocation and makes an explicitly requested Sol turn look as if it
        # silently ran on Luna.  Keep the historical config backfill only for
        # unpinned Codex runs, where it describes the default that was actually
        # selected by the resolver.  ``served`` remains the authoritative field
        # for provider service evidence in both cases.
        if inv.cli == "codex" and not _legacy and not _codex_selection.get("exact_required"):
            try:
                from _resolver import _codex_default_model
                _legacy = _safe_model_id(_codex_default_model())
            except Exception:  # noqa: BLE001 — telemetry best-effort, never fatal
                pass
        resp["model"] = {"requested": _requested_model, "targeted": _targeted,
                         "served": _served, "resolved": _legacy, "models_used": _mu,
                         "exact_required": _exact_required,
                         "exact_source": getattr(inv, "model_exact_source", None),
                         "evidence_source": resp.get("model_evidence_source")}
        resp["model_match"] = model_match_state(
            _requested_model, _targeted, _served, resp.get("served_model_evidence"))
        resp["named_model_verified"] = resp["model_match"] is True
        if inv.cli == "codex":
            resp["model"].update({
                "request_source": _codex_selection.get("source"),
                "exact_required": _exact_required,
            })
        # Model-mismatch warning: if an EXPLICIT request differs from what actually
        # ran (served, else the legacy resolved), surface it prominently -- a pinned
        # agent model silently downgraded/rerouted is a spend + fidelity surprise.
        # Suppress only KNOWN alias expansions (opus/sonnet/haiku float to the
        # latest release, so requested 'opus' vs served 'claude-opus-5' is not a
        # mismatch); everything else warns.
        _ran = _served or _legacy
        if _model_mismatch(_requested_model, _ran):
            resp.setdefault("warnings", []).append(
                f"requested model {_requested_model!r} but the backend ran {_ran!r}; "
                f"both are kept in the `model` field (a pinned agent model may have "
                f"been rerouted or fallen back)")
        # Provenance-required named-model pins are a trust contract, not a
        # best-effort hint. A successful response without an exact terminal
        # receipt, or with a different served model, is useful audit evidence
        # but must not be consumed as a successful answer or sent through any
        # retry/fallback path. The provider may already have spent; preserve
        # that fact. Codex pins use the same gate as custom/frontmatter seats.
        if _exact_required and resp.get("status") == "success":
            if _handshake and _requested_model != _handshake:
                _trust_kind = "target_model_mismatch"
                _trust_message = (
                    f"requested model {_requested_model!r}, but the provider "
                    f"handshake targeted {_handshake!r}; the run is blocked before "
                    "model identity can be trusted")
            elif (resp.get("served_model_evidence") != "reported"
                  or _terminal_model_invalid or not _terminal_model):
                _trust_kind = "served_model_unverified"
                _trust_message = (
                    f"exact model {_requested_model!r} completed without "
                    "an authoritative terminal served-model receipt; the result "
                    "is blocked and cannot be treated as verified")
            elif _requested_model != _terminal_model:
                _trust_kind = "served_model_mismatch"
                _trust_message = (
                    f"requested model {_requested_model!r}, but the provider "
                    f"reported {_terminal_model!r}; the result is blocked and no "
                    "automatic retry or fallback will be attempted")
            else:
                _trust_kind = None
                _trust_message = None
            if _trust_kind:
                resp["status"] = "blocked"
                resp["dispatcher_status"] = "blocked"
                resp["execution_status"] = "blocked"
                resp["error_kind"] = _trust_kind
                resp["retryable"] = False
                resp["result_usable"] = False
                resp["provider_contacted"] = True
                resp["error"] = _trust_message
                resp.setdefault("warnings", []).append(
                    "model identity trust failure is terminal; no retry, provider "
                    "fallback, ACP fallback, repair, or resume was attempted")
        resp["permission"] = inv.permission
        resp["transport"] = inv.transport   # which path served the run (subprocess|acp)
        resp["effort"] = inv.effort   # reasoning effort actually applied (None = backend default)
        # True only for a caller-requested continuation. Automatic schema/report
        # repair retries have their own explicit fields and do not relabel the root run.
        resp["resumed"] = bool(inv.resume_id)
        # agy reads --cwd at safe-edit/yolo (summon passes --add-dir since 0.13.9) but NOT
        # at read-only, where the workspace is withheld because agy cannot enforce that tier.
        # The other thing that bites is the clock: agy is multi-step, so a short budget kills
        # it mid-work. Both live in advisory_warnings, shared with --dry-run.
        for _w in advisory_warnings(inv.cli, inv.permission, timeout_ms, inv.model,
                                    inv.extra_args):
            resp.setdefault("warnings", []).append(_w)
        try:
            resp["permission_flags"] = permission_flags(inv.cli, inv.permission)
        except ValueError:
            resp["permission_flags"] = None
        # Which billing source this run drew from (subscription vs API credits) —
        # pairs with usage/cost_usd so an orchestrator can attribute spend. A
        # Claude resume keeps the session's ORIGINAL model; the new invocation's
        # agent/model is not evidence. Only a terminal model report can resolve it.
        if inv.cli == "claude" and inv.resume_id and not _terminal_model:
            resp["billing"] = {
                "source": "unknown",
                "note": "resumed Claude session kept its original model; no terminal "
                        "model evidence was available, so billing cannot be inferred",
            }
        else:
            _billing_model = (_terminal_model
                              if inv.cli == "claude" and inv.resume_id
                              else _requested_model)
            _billing_args = (() if inv.cli == "claude" and inv.resume_id
                             else inv.extra_args)
            resp.setdefault("billing", infer_dispatch_billing(
                inv.cli, _billing_model, _billing_args))
        # Credit-only transparency: surface every guard warning (model
        # fallback, scrubbed args, stripped env alias, resume caveat) …
        for w in _guard_warnings:
            resp.setdefault("warnings", []).append(w)
        # … and correct the billing source for a credit-only run. The effective model
        # can come from --model OR an `args:` selector, so key off that (not just
        # inv.model). ANTHROPIC_API_KEY presence predicts the API route; vendor
        # authentication remains authoritative.
        if inv.cli == "claude":
            _picks_credit = selects_credit_only(_requested_model, inv.extra_args)
            if credit_spend_allowed() and _picks_credit:
                if os.environ.get("ANTHROPIC_API_KEY"):
                    resp["billing"] = {"source": "api",
                        "note": "credit-only model via ANTHROPIC_API_KEY "
                                "(metered API, not account credit)"}
                else:
                    resp["billing"] = {"source": "credit",
                        "note": "credit-only model billed to account credit"}
            elif inv.resume_id and _picks_credit:
                # Unauthorized resume of a credit-only request: --resume keeps the
                # session's original model (the guard can't re-pin it), so the
                # billing source is genuinely not determinable here.
                resp["billing"] = {"source": "unknown",
                    "note": "resumed claude session runs its original model (guard can't re-pin "
                            "on --resume); billing cannot be proven"}
        raw = resp.pop("_debug_raw", None)
        _finalize_diagnostics(resp, raw, debug_dir, debug_argv, max_tool_output_bytes)
        _attach_eligibility(resp)
        try:
            from _drivers import enrich_envelope_from_cli
            enrich_envelope_from_cli(resp, inv.cli, inv.transport)
        except Exception:  # noqa: BLE001 — telemetry additive; never break dispatch
            pass
        return resp

    # Deterministic model/backend routing preflight.  This must run after the effective
    # model guard but before API calls, ACP setup, profile construction, worktree access, or
    # subprocess creation.  An explicit Claude model sent to Codex is a caller-routing error,
    # not a provider outage and must be visible as a structured block with zero contact.
    if inv.cli == "codex" and _codex_selection.get("conflict"):
        _detail = _codex_selection["conflict"]
        return _stamp(_enrich(_blocked_response(
            inv.cli,
            "model_selection_conflict",
            f"codex model selection conflict: {_detail}; remove the conflicting "
            "selector and retry",
            details={
                "model_requested": _requested_model,
                "model_selection_source": _codex_selection.get("source"),
                "provider_contacted": False,
                "retryable": False,
                "result_usable": False,
            }), None))

    _compat_model = (_codex_selection.get("canonical")
                     if inv.cli == "codex" else _guarded_inv.model)
    if not _compat_model and inv.cli == "codex":
        try:
            from _resolver import _codex_default_model
            _compat_model = _codex_default_model()
        except Exception:  # noqa: BLE001 — preflight must remain non-fatal
            _compat_model = None
    _compat = model_backend_compatibility(inv.cli, _compat_model)
    if _compat:
        _compat_details = {k: v for k, v in _compat.items()
                           if k not in {"message", "error_kind"}}
        return _stamp(_enrich(_blocked_response(
            inv.cli, _compat["error_kind"], _compat["message"],
            details=_compat_details), None))

    # Retries and report repairs share the immutable background-job budget.
    # Enforce it before every API boundary / before_provider_launch / Popen so
    # an already-expired corrective attempt cannot incur provider contact and
    # then be killed only after the stream driver reconstructs its clock.
    if os.environ.get("SUMMON_ADAPTIVE_TIMEOUT") == "1":
        from _job_control import environment_job_budget
        _job_budget = environment_job_budget(timeout_ms)
        if _job_budget["expired"]:
            _budget_response = _error_response(
                inv.cli, 124, "background job hard runtime budget exhausted",
                not_run=True)
            _budget_response.update({
                "error_kind": "job_hard_timeout",
                "retryable": False,
                "result_usable": False,
                "timeout": {"stage": "adaptive_job_hard_timeout"},
            })
            return _stamp(_enrich(_budget_response, None))

    # API-kind backends (e.g. openai-compat): the backend performs the request
    # itself instead of spawning a process. Flows through the same _enrich/_stamp
    # so the envelope shape is identical to a subprocess backend's.
    if backend_kind(inv.cli) == "api":
        if workspace_snapshot is not None:
            _workspace_before = workspace_snapshot(inv.cwd)
        debug_argv = [inv.cli, inv.base_url or "?", inv.model or "?"]
        # arkcli is registered as an API-kind backend for envelope routing, but
        # it currently executes `arkcli +chat` via subprocess.run internally.
        # It has no before-Popen port, so controlled deliberation must refuse it
        # before contact instead of pretending that launch accounting applies.
        if launch_control is not None and inv.cli != "openai-compat":
            return _stamp(_enrich(_error_response(
                inv.cli, 1,
                f"backend {inv.cli!r} has no controlled provider-launch boundary; "
                "deliberation refused it before contact", not_run=True), None))
        if launch_control is None:
            _backend_resp = BACKENDS[inv.cli]["call"](inv, timeout_ms)
        else:
            _backend_resp = BACKENDS[inv.cli]["call"](
                inv, timeout_ms, launch_control=launch_control)
        resp = _enrich(_backend_resp, None)
        resp["resume"] = {"cli": inv.cli, "session_id": None}  # stateless: no resume
        return _stamp(resp)

    # BEFORE build_invocation_args, which has side effects: for agy it creates a
    # per-invocation profile and copies OAuth material into it. A dispatch we are going to
    # refuse must not do that work, let alone copy credentials for it.
    _ro_err = readonly_unenforceable_error(inv.cli, inv.permission,
                                           forced=inv.permission_forced)
    if _ro_err:
        if workspace_snapshot is not None:
            _workspace_before = workspace_snapshot(inv.cwd)
        _permission_refusal = _error_response(inv.cli, 1, _ro_err, not_run=True)
        _permission_refusal.update({
            "error_kind": "permission_unsupported",
            "provider_contacted": False,
            "retryable": False,
            "result_usable": False,
        })
        return _stamp(_enrich(_permission_refusal, None))

    # ACP transport (gemini/kimi/cursor-agent): the backend speaks the Agent
    # Client Protocol natively, so the turn runs over JSON-RPC stdio instead of
    # a one-shot argv spawn. The call owns its session I/O and returns the
    # standard shape, like the api kind above. Placed AFTER the read-only guard
    # so an unenforceable tier fails closed identically on both transports.
    if inv.transport == "acp":
        if workspace_snapshot is not None:
            _workspace_before = workspace_snapshot(inv.cwd)
        from _builder import supports_acp as _supports_acp
        if not _supports_acp(inv.cli):
            return _stamp(_enrich(_error_response(
                inv.cli, 2, f"backend {inv.cli!r} has no acp transport",
                not_run=True), None))
        if inv.permission != "yolo":
            # Reactive-only enforcement: no permission flags travel to an ACP
            # agent, so containment depends on the agent CHOOSING to send
            # session/request_permission -- unverified on real CLIs (gemini is
            # frozen for individuals, cursor untested, kimi is yolo-only
            # everywhere anyway). Unverifiable containment is not containment:
            # refuse rather than mislabel the tier.
            _permission_refusal = _error_response(
                inv.cli, 2,
                f"the ACP transport cannot enforce {inv.permission!r}: no "
                "permission flags travel to the agent and containment depends on "
                "the agent choosing to ask (reactive only, unverified on real "
                "CLIs). summon refuses rather than mislabel the tier. Use yolo "
                "over ACP, or the subprocess transport, which enforces tiers via "
                "CLI flags.", not_run=True)
            _permission_refusal.update({
                "error_kind": "permission_unsupported",
                "provider_contacted": False,
                "retryable": False,
                "result_usable": False,
            })
            return _stamp(_enrich(_permission_refusal, None))
        debug_argv = [inv.cli, "<acp>"]
        if launch_control is None:
            _acp_resp = BACKENDS[inv.cli]["acp"]["call"](inv, timeout_ms)
        else:
            _acp_resp = BACKENDS[inv.cli]["acp"]["call"](
                inv, timeout_ms, launch_control=launch_control)
        resp = _enrich(_acp_resp, None)
        # Premortem T1: the ACP session id is NOT a resume handle for the
        # subprocess path — _apply_schema re-dispatches the ORIGINAL invocation
        # with resume_id=<that id>, a wrong-namespace resume on cursor-agent.
        # Keep the resume lane dead; carry the id as telemetry only.
        resp["acp"] = {"session_id": resp.get("session_id")}
        resp["resume"] = {"cli": inv.cli, "session_id": None}
        # Same artifact contract as the subprocess path: _debug_raw never ships
        # inside the envelope, and every persisted/returned field is
        # secret-redacted (backend stderr can echo credentials).
        return _stamp(resp)
    if inv.transport != "subprocess":
        return _stamp(_enrich(_error_response(
            inv.cli, 2, f"unknown transport {inv.transport!r} "
                        f"(use 'subprocess' or 'acp')", not_run=True), None))

    # timeout_ms is threaded to the builder so agy's wrapper deadline AND its
    # profile-TTL cleanup (which runs during build) both reflect the real request.
    try:
        if launch_control is None:
            # Preserve the historical two-argument hook shape for ordinary
            # dispatches and project-local test wrappers.
            command, args, env_override = build_invocation_args(inv, timeout_ms)
        else:
            command, args, env_override = build_invocation_args(
                inv, timeout_ms,
                resource_register=launch_control.register_resource)
    except (ValueError, ProviderLaunchError) as _build_err:
        # The dispatcher's contract is ONE JSON envelope on stdout, always. Build-time
        # guards (an oversized agy prompt, a missing ConPTY wrapper) raised straight through
        # execute_agent instead, so a caller parsing stdout got a traceback where an
        # envelope belongs -- and every orchestration path that branches on `status` saw an
        # exception rather than a status to branch on.
        # Controlled builder errors must remain path- and secret-free.  Ordinary
        # dispatch keeps its historical detail for compatibility; the
        # deliberation envelope exposes only the exception class.
        _detail = (f"provider preparation refused ({type(_build_err).__name__})"
                   if launch_control is not None else str(_build_err))
        return _stamp(_enrich(_error_response(inv.cli, 1, _detail, not_run=True), None))
    command, args = _resolve_launch(command, args)
    # AFTER _resolve_launch: on Windows a .cmd shim is rewritten to `node <path>/cli.js`,
    # which changes the length that actually gets measured by CreateProcess.
    # agy litters the dispatch cwd with a language-server log; note whether one is already
    # there so the sweep below can only remove a file THIS run created.
    _litter_before = bool(inv.cli == "agy"
                          and (_agy_litter_path(inv.cwd) or "")
                          and os.path.isfile(_agy_litter_path(inv.cwd)))
    proc_env = _merge_env(env_override)
    # AFTER _merge_env: the POSIX total counts the environment, and the environment that
    # matters is the one Popen receives -- overrides included, stripped keys excluded.
    _argv_err = argv_length_error(inv.cli, command, args, proc_env)
    if _argv_err:
        # A prompt that cannot fit in argv can still go over ACP — the prompt is
        # a JSON-RPC parameter on stdin there, with no OS command-line limit.
        # Only for backends with NATIVE ACP support; the kill switch and the
        # fallback envelope field apply even to an explicit --transport
        # subprocess pin, because the alternative here is certain failure.
        from _builder import supports_acp as _supports_acp
        if (_supports_acp(inv.cli)
                and (launch_control is None or launch_control.allow_secondary)
                and os.environ.get("SUMMON_ACP_FALLBACK") != "0"
                and (inv.cli != "kimi"
                     or os.environ.get("SUMMON_KIMI_ACP_FALLBACK") == "1")):
            from dataclasses import replace as _replace_inv
            _routed = execute_agent(_replace_inv(inv, transport="acp"),
                                    timeout_ms=timeout_ms, debug_dir=debug_dir,
                                    max_tool_output_bytes=max_tool_output_bytes,
                                    launch_control=launch_control)
            _routed.setdefault("warnings", []).append(
                "the prompt exceeded the OS command-line length limit for the "
                "subprocess transport, so summon routed this dispatch over ACP "
                "(stdin JSON-RPC has no argv cap)")
            _routed["fallback"] = {"from": "subprocess", "to": "acp",
                                   "reason": "argv-length",
                                   "primary_status": "not_dispatched"}
            return _routed
        # exit 1, not 127: 127 means "CLI not found", and reporting this as a missing
        # binary is precisely the misdiagnosis being fixed.
        _resp = _error_response(inv.cli, 1, _argv_err, not_run=True)
        # agy builds its per-invocation profile during build_invocation_args, so a rejection
        # HERE leaves a populated profile on disk with no handle to it: the caller could
        # neither resume nor clean it up, and it lingered until a TTL sweep. Hand it back.
        if inv.cli == "agy" and env_override:
            _resp["resume"] = {"cli": inv.cli, "session_id": None,
                               "profile": env_override.get("USERPROFILE")}
        return _stamp(_enrich(_resp, None))
    if workspace_snapshot is not None and _workspace_before is None:
        _workspace_before = workspace_snapshot(inv.cwd)
    debug_argv = [command, *args]

    # POSIX: put the child in its own session so _kill_tree can signal the whole
    # group (a shim's grandchild otherwise survives process.kill() and keeps
    # stdout open, defeating the timeout). Windows walks the tree via taskkill /T.
    from _spawn import popen_flags
    try:
        if launch_control is not None:
            launch_control.before_provider_launch({
                "backend": inv.cli,
                "transport": "subprocess",
                "command_sha256": hashlib.sha256(
                    str(command).encode("utf-8", errors="replace")).hexdigest(),
            })
        # stdin=DEVNULL: sub-agent CLIs (notably codex) probe stdin for "additional
        # input" and block reading from a TTY inherited from the parent. We never
        # have stdin to give them.
        process = subprocess.Popen(
            [command, *args],
            cwd=inv.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge: single reader drains both -> no stderr pipe-buffer deadlock
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=proc_env,
            **popen_flags(),
        )
    except FileNotFoundError:
        # Windows raises this for an over-long command line too (ERROR_FILE_NOT_FOUND).
        # argv_length_error above catches the known limit; if we still land here with a
        # large argv, say so rather than asserting an install problem we did not verify.
        _n = len(command) + sum(len(a) + 1 for a in args)
        _hint = ("" if _n < 16000 else
                 f" (the command line is {_n} chars; on Windows an over-long command line "
                 f"is reported as a missing file, so this may be argv overflow rather than "
                 f"a missing binary)")
        return _stamp(_enrich(
            _error_response(inv.cli, 127, f"CLI not found: {command}{_hint}",
                            not_run=True), None))
    except OSError as e:
        return _stamp(_enrich(_error_response(
            inv.cli, 1, f"{type(e).__name__}: {e}", not_run=True), None))
    except ProviderDeadlineError:
        _deadline_response = _error_response(
            inv.cli, 124, "provider launch deadline exceeded", partial_result=None,
            not_run=True)
        _deadline_response["timeout"] = True
        return _stamp(_enrich(_deadline_response, None))
    except Exception as e:
        # The callback is orchestrator-owned and may accidentally carry a
        # secret in its exception text.  Refuse before contact and expose only
        # the safe exception class.
        return _stamp(_enrich(_error_response(
            inv.cli, 1,
            f"provider launch refused by control ({type(e).__name__})", not_run=True), None))

    # Popen returned, so a physical provider attempt exists even if registration
    # or stream parsing fails immediately. Mark the boundary before any later
    # finalizer can shape an error envelope.
    _provider_contacted = True

    if launch_control is not None:
        try:
            launch_control.spawned(process)
        except Exception as e:
            # Popen already succeeded.  A failed registration must not turn
            # into an untracked paid child: kill/reap it before returning.
            _kill_tree(process)
            _safe_communicate(process)
            try:
                launch_control.reaped(process)
            except Exception:
                pass
            _registration_error = _error_response(
                inv.cli, 1,
                f"provider process registration failed ({type(e).__name__}); "
                "child was terminated")
            _registration_error["provider_contacted"] = _provider_contacted
            return _stamp(_enrich(_registration_error, None))

    # Windows only: put the child in a kill-on-close Job Object so its whole tree can be
    # terminated even after this leader exits (issue #10) -- taskkill walks parent->child
    # PID links and loses the tree the moment the leader is gone -- and so an unexpected
    # death of summon itself does not leave a paid backend running unattended.
    try:
        from _jobobj import attach as _job_attach
        _job_attach(process)
    except Exception:  # noqa: BLE001 - teardown plumbing must never break a dispatch
        pass

    try:
        parse_stream = (
            (inv.cli != "agy" or _agy_stream_wrapper(command, args))
            and inv.cli != "arkcli"
        )
        response = _drive_process(process, inv.cli, timeout_ms,
                                  parse_stream=parse_stream,
                                  launch_control=launch_control,
                                  attempt_id=_attempt_id,
                                  attempt_kind=getattr(inv, "attempt_kind", "initial"),
                                  attempt_ordinal=getattr(inv, "attempt_ordinal", 1),
                                  expected_model=(
                                      _requested_model
                                      if inv.cli == "codex"
                                       and _codex_selection.get("exact_required")
                                       else None))
        response.setdefault("provider_contacted", _provider_contacted)
        response.setdefault("attempt_kind", getattr(inv, "attempt_kind", "initial"))
        response.setdefault("attempt_ordinal", getattr(inv, "attempt_ordinal", 1))
        if getattr(inv, "parent_attempt_id", None):
            response.setdefault("parent_attempt_id", inv.parent_attempt_id)
        if inv.cli == "kimi" and env_override:
            # Kimi 0.38 omits the model from its public stream-json records. Its
            # fresh per-call profile does contain response-accounting records,
            # so capture that evidence before credential sync or controlled
            # profile cleanup removes the journal. Request-only records never
            # enter this path and conflicting runtime/stream evidence fails
            # closed instead of choosing whichever value matches the request.
            _kimi_evidence = _capture_kimi_wire_model(
                env_override.get("KIMI_CODE_HOME"),
                started_wall_ms=started_wall_ms,
                ended_wall_ms=int(time.time() * 1000),
            )
            if _kimi_evidence:
                _stream_model = _safe_model_id(response.get("model_resolved"))
                _wire_model = _kimi_evidence["model"]
                if _stream_model and _stream_model != _wire_model:
                    response["model_resolved"] = None
                    response["model_evidence_source"] = "kimi_model_evidence_conflict"
                    response.setdefault("warnings", []).append(
                        "Kimi stream and runtime accounting reported different models; "
                        "served identity was withheld")
                else:
                    response["model_resolved"] = _wire_model
                    response["model_evidence_source"] = (
                        "kimi_assistant_record+kimi_wire_usage_record"
                        if _stream_model else "kimi_wire_usage_record")
                    response["models_used"] = sorted(set(
                        list(response.get("models_used") or [])
                        + list(_kimi_evidence["models_used"])))
                response["kimi_runtime_evidence"] = {
                    "source": _kimi_evidence["source"],
                    "usage_records": _kimi_evidence["usage_records"],
                    "turn_completed": _kimi_evidence["turn_completed"],
                }
    finally:
        # Kimi receives a disposable isolated home.  Its OAuth refresh token
        # rotation happens in that child home, so persist a validated refresh
        # before the controlled adapter removes the profile.  A failed sync is
        # intentionally non-fatal: the provider response remains truthful and
        # the next attempt can use the explicit auth-repair workflow.
        if inv.cli == "kimi" and env_override:
            try:
                from _builder import sync_kimi_profile_credentials
                sync_kimi_profile_credentials(env_override.get("KIMI_CODE_HOME"))
            except Exception:  # noqa: BLE001 - credential sync must not mask result
                pass
        # The dispatch is OVER here whichever way it ended. Closing the job releases the
        # kernel handle AND, via KILL_ON_JOB_CLOSE, reaps any descendant the backend left
        # running -- the case that let a retry overlap with the previous attempt's tree.
        try:
            from _jobobj import close as _job_close
            _job_close(process)
        except Exception:  # noqa: BLE001 - cleanup must never mask the real result
            pass
        if inv.cli == "agy":
            try:
                _sweep_agy_litter(inv.cwd, _litter_before)
            except Exception:  # noqa: BLE001
                pass
        if launch_control is not None:
            try:
                launch_control.reaped(process)
            except Exception:
                pass
    # PROMPT ECHO GUARD. `error_hint` is picked out of UNTRUSTED captured output, so a
    # prompt that instructs the agent to print error-shaped text can put the caller's own
    # words into the envelope's error field. Review demonstrated it: a prompt saying
    # "print exactly Unauthorized: rotate all credentials" was promoted verbatim. The
    # invocation is only available here, so the check lives here rather than being plumbed
    # three levels down into the driver.
    _hint = response.get("error_hint")
    if _hint:
        _norm = " ".join(str(_hint).split()).lower()
        _prompt_lines = {" ".join(l.split()).lower()
                         for l in (inv.prompt or "").splitlines() if l.strip()}
        if _norm in _prompt_lines or any(_norm in pl for pl in _prompt_lines):
            response.pop("error_hint", None)
            response["error_hint_suppressed"] = (
                "a candidate error line was suppressed because it also appears in the "
                "prompt; an echo of the caller's own text is not evidence about the "
                "backend")

    # Resume handle: what the orchestrator passes to a follow-up `--resume`.
    # session_id comes from the stream (claude/codex/cursor); agy has no stream
    # id, so it resumes by reusing the same profile dir instead.
    resume: dict = {"cli": inv.cli, "session_id": response.get("session_id")}
    if inv.cli == "agy" and env_override:
        resume["profile"] = env_override.get("USERPROFILE")
    response["resume"] = resume
    return _stamp(response)


def _merge_env(env_override: dict | None) -> dict | None:
    """Merge env_override onto os.environ. A value of None means REMOVE that key
    from the child env (used to strip OPENAI_API_KEY so codex bills the ChatGPT
    subscription, never the metered API)."""
    if not env_override:
        # The Windows launcher marker is a parent-side transport guard, not a
        # provider setting. Do not leak it into the child or a nested CLI.
        if os.environ.get("SUMMON_CMD_LAUNCHER") == "1":
            inherited = {**os.environ}
            inherited.pop("SUMMON_CMD_LAUNCHER", None)
            return inherited
        return None
    merged = {**os.environ}
    merged.pop("SUMMON_CMD_LAUNCHER", None)
    for key, value in env_override.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
