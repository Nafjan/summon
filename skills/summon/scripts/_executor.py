"""Subprocess driver: spawn the CLI, consume its stream, shape the response."""

from __future__ import annotations

import glob
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time

from _builder import (AgentInvocation, BACKENDS, agy_permission_warning,
                      apply_credit_guard, backend_kind, build_invocation_args,
                      credit_spend_allowed, infer_billing, permission_flags,
                      selects_credit_only)
from _stream import StreamProcessor, _terminal_is_error

_SUCCESS_EXIT_CODES = (0, 143, -15)  # 0 ok, 143/-15 = SIGTERM (we asked it to stop)

# The report contract's bookend fields — present in every agent definition.
_REPORT_BOOKENDS = ("STATUS", "SUMMARY", "FOLLOW-UP", "HANDOFF")
# Known contract field names across all agent definitions. ONLY these start a new
# field — so a continuation line that happens to begin "NOTE:"/"TODO:"/"HTTP://"
# stays part of the current value instead of silently splitting it (which would
# truncate HANDOFF, the field carried into the next call).
_REPORT_FIELDS = frozenset({
    "STATUS", "SUMMARY", "COMMANDS", "VERIFICATION", "FOLLOW-UP", "HANDOFF",
    "FINDINGS", "VERDICT", "PLAN", "RISKS", "DESIGN", "TRADE_OFFS",
    "HYPOTHESES_TESTED", "ROOT_CAUSE", "CHANGES", "DESIGN_NOTES", "TESTS",
    "EDITS", "TONE_CHANGES", "DOCS", "EVIDENCE", "CONFIDENCE", "ANALYSIS",
    "PR_TITLE", "PR_BODY",
})
# Valid first token of a real STATUS value — used to anchor on a genuine block
# rather than a quoted "STATUS: DONE | PARTIAL | BLOCKED" contract example.
_STATUS_VALUES = frozenset({"DONE", "PARTIAL", "BLOCKED", "SUCCESS", "ERROR"})
_REPORT_FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_-]{1,}):[ \t]?(.*)$")
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

# Envelope schema version — bumped only on a breaking change to the response
# shape, so an orchestrator can branch on it. Adding fields does NOT bump it.
ENVELOPE_VERSION = 1


def _detect_blocked(text: str) -> list:
    """Approval markers present in the TAIL of the result (case-insensitive)."""
    tail = (text or "")[-_BLOCKED_TAIL:].lower()
    return [m for m in _BLOCKED_MARKERS if m in tail]


def parse_report(text: str) -> dict | None:
    """Extract the trailing report-contract block from an agent's result text.

    Anchors on the LAST ``STATUS:`` line whose value begins with a real status
    token (DONE/PARTIAL/BLOCKED/...) — so a quoted contract example or narration
    that merely mentions ``STATUS:`` can't spoof or displace the real block. A line
    begins a new field when its key is a known field OR a well-formed all-caps
    identifier (so third-party agents' custom fields are captured, not folded);
    any other line continues the current value (multi-line safe). Keys are
    lowercased with ``-`` mapped to ``_`` (e.g. ``follow_up``).

    Returns None when no genuine ``STATUS:`` line exists.
    """
    if not text:
        return None
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("STATUS:"):
            value = lines[i][len("STATUS:"):].strip()
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
        m = _REPORT_FIELD_RE.match(line)
        if m and _is_field_key(m.group(1)):
            current_key = m.group(1).lower().replace("-", "_")
            fields[current_key] = m.group(2).strip()
        elif current_key is not None:
            fields[current_key] = (fields[current_key] + "\n" + line).strip()
    return fields or None


def _enrich(response: dict, processor: StreamProcessor | None) -> dict:
    """Attach telemetry + parsed report to a response (all return paths).

    Adds: ``session_id``, ``usage``, ``cost_usd`` (from stream events; None
    where the backend doesn't emit them), ``report`` (parsed contract block or
    None), ``report_ok`` (all bookend fields present), and ``suspect: true``
    when a run claims success but the contract block is missing/incomplete.

    One exception to "the parser never changes status": a run whose result ENDS
    on an interactive-approval request (see ``_BLOCKED_MARKERS``) with no report
    contract is downgraded from ``success`` to ``blocked`` — the CLI exited 0,
    but in one-shot mode nobody is there to click approve, so the task did not
    happen. An orchestrator trusting ``status`` must not collect that as a win.
    """
    response["envelope"] = ENVELOPE_VERSION
    # setdefault (not =) so a non-stream backend (openai-compat) that already
    # populated these from its HTTP response isn't clobbered with None.
    response.setdefault("session_id", processor.session_id if processor else None)
    response.setdefault("usage", processor.usage if processor else None)
    response.setdefault("cost_usd", processor.cost_usd if processor else None)
    response.setdefault("model_resolved", processor.model if processor else None)
    response.setdefault("model_targeted", processor.handshake_model if processor else None)
    response.setdefault("models_used", processor.models_used if processor else [])
    # Baseline resume handle on EVERY path (incl. spawn-failure) so orchestrators
    # can read response["resume"] unconditionally. execute_agent enriches it with
    # the agy profile on the normal path.
    response.setdefault("resume", {"cli": response.get("cli"), "session_id": response.get("session_id")})
    report = parse_report(response.get("result") or "")
    response["report"] = report
    response["report_ok"] = bool(
        report and all(b.lower().replace("-", "_") in report for b in _REPORT_BOOKENDS)
    )
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
    return response


# Payload elision + startup-noise filtering for the human-facing output_tail.
# The FULL raw transcript is kept for --debug-dir (the debug_file pointer); only
# the tail is sanitized so a failure stays diagnosable without a re-run and
# without a base64 image blob or provider startup noise drowning the signal.
_DEFAULT_MAX_TOOL_OUTPUT_BYTES = 2048

# base64 AND base64url alphabets (+/ and -_) plus '=' padding. A linear scan over
# this set (not a regex {N,} quantifier) means no OverflowError on a huge
# threshold and no super-linear CPU on a near-threshold run.
_B64_SCAN = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_=")
# A full base64 data: URI: the `data:` SCHEME is required (so prose that merely
# contains ";base64," is not falsely elided), extra parameters are tolerated,
# whitespace between the comma and the payload is skipped (so it can't smuggle a
# blob past detection), and the payload run is captured. `[A-Za-z0-9+/_=-]+` is a
# plain char class with `+` (linear, no ReDoS / no {N,} OverflowError).
_DATA_URI_RE = re.compile(
    r"(?i)data:([\w.+-]*/[\w.+-]+)?(?:;[\w.+-]+=[^;,\s]*)*;base64,\s*([A-Za-z0-9+/_=-]+)")
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


def _finalize_diagnostics(resp: dict, raw, debug_dir, debug_argv,
                          max_tool_output_bytes) -> dict:
    """Write the debug transcript (if requested) then sanitize output_tail, with
    the tail's ``debug_file`` reference reflecting whether the file was ACTUALLY
    written. The debug file keeps the UNsanitized raw (the full-detail pointer);
    a failed _write_debug returns None so the tail advises --debug-dir instead of
    naming a nonexistent file. Extracted from _stamp so the wiring is testable."""
    dbg = _write_debug(debug_dir, debug_argv, raw or "", resp) if debug_dir else None
    if dbg:
        resp["debug_file"] = dbg
    if resp.get("output_tail") is not None and raw:
        resp["output_tail"] = _sanitize_tail(
            raw, max_tool_output_bytes, debug_available=bool(dbg))[-2000:]
    return resp


def is_terminal_success(env) -> bool:
    """A dispatch envelope is TERMINAL-done only when it succeeded AND is not
    suspect (status=success but report_ok=false -> suspect: a semantically-useful
    but unparseable result that should re-dispatch, not be skipped). Shared by the
    --out skip and manifest resume so both agree."""
    return bool(isinstance(env, dict) and env.get("status") == "success"
                and not env.get("suspect"))


def finalize_exit_fields(resp: dict) -> dict:
    """Backfill the exit-code-clarity fields on any dispatch-shaped envelope
    (has both status and exit_code). Idempotent: a builder that already set the
    detailed reason keeps it. Used by _stamp AND the pre-dispatch emit paths."""
    if not isinstance(resp, dict) or resp.get("exit_code") is None or not resp.get("status"):
        return resp
    resp.setdefault("backend_exit_code", resp.get("exit_code"))
    resp.setdefault("dispatcher_status", resp.get("status"))
    if "normalization_reason" not in resp:
        _ec, _st = resp.get("exit_code"), resp.get("status")
        if _st == "success" and _ec not in _SUCCESS_EXIT_CODES:
            resp["normalization_reason"] = f"normalized to success (raw backend exit {_ec})"
        elif _st == "success":
            resp["normalization_reason"] = "exit and status agree"
        else:
            resp["normalization_reason"] = f"status {_st} (backend exit {_ec})"
    return resp


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
    # ('opus' -> 'claude-opus-4-8'); a substring test would wrongly hide a real
    # reroute ('opus' -> 'notopus'), so split on id separators and match exactly.
    if r in _CLAUDE_ALIASES and r in re.split(r"[-_/.:]+", s):
        return False
    return True


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
    cli: str, exit_code: int, error: str, partial_result: dict | None = None
) -> dict:
    return {
        "result": partial_result.get("result", "") if partial_result else "",
        "exit_code": exit_code,
        "status": "error",
        "cli": cli,
        "error": error,
        "backend_exit_code": exit_code,
        "dispatcher_status": "error",
        "normalization_reason": "execution failed before a usable terminal result",
    }


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
    elif exit_code in _SUCCESS_EXIT_CODES and "".join(stdout_lines).strip():
        # Plain-text backend that exited cleanly WITH output (no parsed terminal event).
        status = "success"
        norm_reason = "clean exit with output, no terminal event to parse"
    else:
        status = "error"
        norm_reason = f"no usable terminal result and backend exit {exit_code} is not success"

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
        "dispatcher_status": status,
        "normalization_reason": norm_reason,
    }
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
            response["error"] = msg
    if status != "success":
        # Diagnosability: the tail of the RAW captured output (stdout+stderr are
        # merged at spawn), so a failure is inspectable without a re-run.
        response["output_tail"] = "".join(stdout_lines)[-2000:]
    response["_debug_raw"] = "".join(stdout_lines)[-200_000:]
    return response


_LINE = "line"
_EOF = "eof"

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
    resp = _partial_response(cli, processor.get_result(), 124, f"Timeout after {timeout_ms}ms")
    return _attach_raw(resp, stdout_lines)


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
            # this PID mid-teardown — taskkill /T targets the right tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=10)
        else:
            # start_new_session=True makes the child its own group leader, so the
            # PGID equals the child PID. Signal the group by PID directly instead
            # of os.getpgid(pid) — getpgid raises if the child was already reaped
            # (child exited but a grandchild still holds stdout), which would skip
            # the kill and orphan the grandchild.
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


def _drive_process(process: subprocess.Popen, cli: str, timeout_ms: int) -> dict:
    """Drive the subprocess and enrich whatever response path it takes.

    Single choke point: every return from the read loop (success, timeout,
    output-cap abort, I/O error) passes through ``_enrich`` so callers always
    see the same telemetry/report keys.
    """
    processor = StreamProcessor()
    response = _drive_process_loop(process, cli, timeout_ms, processor)
    return _enrich(response, processor)


def _drive_process_loop(
    process: subprocess.Popen, cli: str, timeout_ms: int, processor: StreamProcessor
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
    # agy's output is the ConPTY+pyte wrapper's plain-text scrape, NOT stream
    # JSON; never treat one of its lines as a terminal event (a JSON-looking
    # answer line such as a bare number would otherwise truncate the result).
    # Plain-text success is decided by build_final_response (exit code + stdout).
    parse_stream = cli != "agy"
    stdout_lines: list = []
    accumulated_chars = 0
    line_q = _spawn_reader(process)
    saw_terminal = False

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_tree(process)
                _drain_to_eof(line_q)
                _safe_communicate(process)
                return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

            try:
                kind, line = line_q.get(timeout=remaining)
            except queue.Empty:
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
                process.terminate()
                saw_terminal = True

        # stdout fully drained by reader; communicate() only needs stderr.
        # Floor at 100ms: even if the deadline expired, give the process a
        # brief grace window to exit before we escalate to kill().
        wait_remaining = max(0.1, deadline - time.monotonic())
        try:
            _, stderr = process.communicate(timeout=wait_remaining)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            _, stderr = _safe_communicate(process)
            return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

        return build_final_response(
            cli, process.returncode, processor.get_result(), stdout_lines, stderr
        )
    except KeyboardInterrupt:
        # start_new_session detaches the child from the terminal's signal group,
        # so a Ctrl+C on the parent won't reach it — tree-kill it ourselves so an
        # interrupt doesn't leave an orphaned backend running.
        _kill_tree(process)
        raise
    except (OSError, ValueError) as e:
        # OSError covers I/O failures on the pipe; ValueError covers reading
        # from a closed file. Anything else propagates so it's not silently
        # swallowed.
        _kill_tree(process)
        return _attach_raw(_error_response(
            cli, 1, f"{type(e).__name__}: {e}", partial_result=processor.get_result()
        ), stdout_lines)


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
            fh.write(" ".join(a if len(a) <= 2000 else a[:2000] + "...[truncated]" for a in argv))
            fh.write(nl + nl + "# raw captured output (stdout+stderr merged)" + nl)
            fh.write(raw or "(none)")
            fh.write(nl + nl + "# final envelope" + nl)
            fh.write(_json.dumps(response, ensure_ascii=False, indent=1))
        return path
    except OSError:
        return None


# agy can't read files under --cwd (isolated profile), so a "read <file>" prompt
# makes it review the pointer sentence and return a confident-but-empty verdict.
_AGY_FILE_READ_RE = re.compile(
    r"(?i)\b(read|open|review|inspect|check|see|look\s+at)\b[^\n]{0,80}?"
    r"[\w./\\-]+\.(md|txt|py|js|ts|tsx|jsx|json|ya?ml|toml|docx?|pdf|csv|html?|xml|"
    r"rs|go|java|c|cpp|h|hpp|sh|ps1|sql|rb|php)\b")


def _agy_prompt_references_file(prompt: str | None) -> bool:
    return bool(prompt and _AGY_FILE_READ_RE.search(prompt))


def execute_agent(inv: AgentInvocation, timeout_ms: int = 600000,
                  debug_dir: str | None = None,
                  max_tool_output_bytes: int | None = None) -> dict:
    """Execute agent CLI for the given invocation. Returns a response dict.

    Response shape: ``{result, exit_code, status, cli, error?}`` plus the
    telemetry/trust fields documented in SKILL.md (report, model, permission,
    elapsed_ms, ...). ``max_tool_output_bytes`` sets the bare-base64 elision
    threshold for the human-facing output_tail (None -> the built-in default).
    """
    started = time.monotonic()
    # Credit-only model guard (Fable): build_invocation_args enforces it in the
    # argv/env (so --dry-run and real dispatch agree); here we keep the ORIGINAL
    # request, the GUARDED effective model (feeds model.targeted), and the guard
    # warnings for the envelope's transparency.
    _requested_model = inv.model
    _guarded_inv, _, _guard_warnings = apply_credit_guard(inv)
    debug_argv = [inv.cli]  # what --debug-dir records; each path refines it

    def _stamp(resp: dict) -> dict:
        # Wall-clock per dispatch — orchestrators need this for concurrency
        # tuning and it costs nothing to provide.
        resp["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        # Exit-code clarity on EVERY envelope (api-kind backends and other paths
        # build their own response and never touch build_final_response). The
        # response builders set the detailed reason first; this preserves it.
        finalize_exit_fields(resp)
        # Trust fields, split by EVIDENCE (field case: a failed Fable dispatch
        # reported the handshake model as `resolved` with all-zero usage):
        #   requested  what the caller asked for (unchanged).
        #   targeted   what the session was POINTED AT: the init handshake, else
        #              the post-credit-guard effective model, else the backend's
        #              knowable default (cursor pin, codex config).
        #   served     ONLY on service evidence — a terminal-event model report,
        #              or output tokens with a known target. Task status is NOT
        #              evidence (a served run can be downgraded to blocked).
        #   resolved   LEGACY v1 semantics, byte-for-byte unchanged (handshake-
        #              or-terminal + the codex config backfill); consumers
        #              migrate to targeted/served; retired in envelope v2.
        _terminal_model = resp.pop("model_resolved", None)
        _handshake = resp.pop("model_targeted", None)
        _mu = resp.pop("models_used", [])
        _effective = _guarded_inv.model
        if not _effective:
            if inv.cli == "cursor-agent":
                from _builder import CURSOR_DEFAULT_MODEL as _cursor_default
                _effective = _cursor_default
            elif inv.cli == "codex":
                try:
                    from _resolver import _codex_default_model
                    _effective = _codex_default_model()
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
        if _terminal_model:
            _served = _terminal_model
        elif _out_tokens > 0 and _targeted:
            _served = _targeted
        else:
            _served = None
        _legacy = _terminal_model or _handshake
        if inv.cli == "codex" and not _legacy:
            try:
                from _resolver import _codex_default_model
                _legacy = _codex_default_model()
            except Exception:  # noqa: BLE001 — telemetry best-effort, never fatal
                pass
        resp["model"] = {"requested": _requested_model, "targeted": _targeted,
                         "served": _served, "resolved": _legacy, "models_used": _mu}
        # Model-mismatch warning: if an EXPLICIT request differs from what actually
        # ran (served, else the legacy resolved), surface it prominently -- a pinned
        # agent model silently downgraded/rerouted is a spend + fidelity surprise.
        # Suppress only KNOWN alias expansions (opus/sonnet/haiku float to the
        # latest release, so requested 'opus' vs served 'claude-opus-4-8' is not a
        # mismatch); everything else warns.
        _ran = _served or _legacy
        if _model_mismatch(_requested_model, _ran):
            resp.setdefault("warnings", []).append(
                f"requested model {_requested_model!r} but the backend ran {_ran!r}; "
                f"both are kept in the `model` field (a pinned agent model may have "
                f"been rerouted or fallen back)")
        resp["permission"] = inv.permission
        resp["effort"] = inv.effort   # reasoning effort actually applied (None = backend default)
        # agy can't read --cwd files; a "read <file>" prompt makes it review the
        # pointer sentence and return a confident-but-empty verdict. Warn so a
        # schema-valid result isn't trusted blindly.
        if inv.cli == "agy" and _agy_prompt_references_file(inv.prompt):
            resp.setdefault("warnings", []).append(
                "agy runs in an isolated profile and CANNOT read files under --cwd — this "
                "prompt appears to reference a file to read; agy sees only the prompt text, "
                "so inline the file's content instead of pointing at a path")
        _pw = agy_permission_warning(inv.cli, inv.permission)
        if _pw:
            resp.setdefault("warnings", []).append(_pw)
        try:
            resp["permission_flags"] = permission_flags(inv.cli, inv.permission)
        except ValueError:
            resp["permission_flags"] = None
        # Which billing source this run drew from (subscription vs API credits) —
        # pairs with usage/cost_usd so an orchestrator can attribute spend.
        resp.setdefault("billing", infer_billing(inv.cli))
        # Fable / credit-only transparency: surface every guard warning (model
        # fallback, scrubbed args, stripped env alias, resume caveat) …
        for w in _guard_warnings:
            resp.setdefault("warnings", []).append(w)
        # … and correct the billing source for a Fable run. The effective model
        # can come from --model OR an `args:` selector, so key off that (not just
        # inv.model). An ANTHROPIC_API_KEY meters the API instead of account credit.
        if inv.cli == "claude":
            _picks_credit = selects_credit_only(_requested_model, inv.extra_args)
            if credit_spend_allowed() and _picks_credit:
                if os.environ.get("ANTHROPIC_API_KEY"):
                    resp["billing"] = {"source": "api",
                        "note": "credit-only model (Fable) via ANTHROPIC_API_KEY (metered API, not credit)"}
                else:
                    resp["billing"] = {"source": "credit",
                        "note": "credit-only model (Fable) billed to account credit "
                                "(no longer on the Claude Max subscription)"}
            elif inv.resume_id and _picks_credit:
                # Unauthorized resume of a Fable request: --resume keeps the
                # session's original model (the guard can't re-pin it), so the
                # billing source is genuinely not determinable here.
                resp["billing"] = {"source": "unknown",
                    "note": "resumed claude session runs its original model (guard can't re-pin "
                            "on --resume); if it was Fable this bills account credit"}
        raw = resp.pop("_debug_raw", None)
        _finalize_diagnostics(resp, raw, debug_dir, debug_argv, max_tool_output_bytes)
        return resp

    # API-kind backends (e.g. openai-compat): the backend performs the request
    # itself instead of spawning a process. Flows through the same _enrich/_stamp
    # so the envelope shape is identical to a subprocess backend's.
    if backend_kind(inv.cli) == "api":
        debug_argv = [inv.cli, inv.base_url or "?", inv.model or "?"]
        resp = _enrich(BACKENDS[inv.cli]["call"](inv, timeout_ms), None)
        resp["resume"] = {"cli": inv.cli, "session_id": None}  # stateless: no resume
        return _stamp(resp)

    # timeout_ms is threaded to the builder so agy's wrapper deadline AND its
    # profile-TTL cleanup (which runs during build) both reflect the real request.
    command, args, env_override = build_invocation_args(inv, timeout_ms)
    command, args = _resolve_launch(command, args)
    proc_env = _merge_env(env_override)
    debug_argv = [command, *args]

    # POSIX: put the child in its own session so _kill_tree can signal the whole
    # group (a shim's grandchild otherwise survives process.kill() and keeps
    # stdout open, defeating the timeout). Windows walks the tree via taskkill /T.
    popen_extra = {"start_new_session": True} if os.name != "nt" else {}
    try:
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
            **popen_extra,
        )
    except FileNotFoundError:
        return _stamp(_enrich(_error_response(inv.cli, 127, f"CLI not found: {command}"), None))
    except OSError as e:
        return _stamp(_enrich(_error_response(inv.cli, 1, f"{type(e).__name__}: {e}"), None))

    response = _drive_process(process, inv.cli, timeout_ms)
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
        return None
    merged = {**os.environ}
    for key, value in env_override.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
