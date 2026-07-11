"""Subprocess driver: spawn the CLI, consume its stream, shape the response."""

from __future__ import annotations

import glob
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from _builder import AgentInvocation, build_invocation_args, permission_flags
from _stream import StreamProcessor

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
            if first in _STATUS_VALUES:
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
    response["session_id"] = processor.session_id if processor else None
    response["usage"] = processor.usage if processor else None
    response["cost_usd"] = processor.cost_usd if processor else None
    response["model_resolved"] = processor.model if processor else None
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


def _partial_response(cli: str, result: dict | None, exit_code: int, error: str) -> dict:
    return {
        "result": result.get("result", "") if result else "",
        "exit_code": exit_code,
        "status": "partial" if result else "error",
        "cli": cli,
        "error": error,
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

    if result:
        # Terminal event parsed -> task completed. A non-zero exit (e.g. from
        # terminate() of a Windows .cmd shim after we got the result) is not a failure.
        status = "success"
    elif exit_code in _SUCCESS_EXIT_CODES and "".join(stdout_lines).strip():
        # Plain-text backend that exited cleanly WITH output (no parsed terminal event).
        status = "success"
    else:
        status = "error"

    response = {
        "result": result.get("result", "") if result else "".join(stdout_lines),
        "exit_code": exit_code,
        "status": status,
        "cli": cli,
    }
    if status == "error":
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
    line_q: queue.Queue = queue.Queue()

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
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
                return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

            try:
                kind, line = line_q.get(timeout=remaining)
            except queue.Empty:
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
                return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

            if kind == _EOF:
                break
            stdout_lines.append(line)
            accumulated_chars += len(line)
            if accumulated_chars > _MAX_STDOUT_CHARS:
                # Defensive cap: a sub-agent emitting unbounded non-terminal
                # output would otherwise grow stdout_lines until the wall-clock
                # deadline (default 10 min). Kill it and report partial.
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
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
            process.kill()
            _, stderr = process.communicate()
            return _timeout_payload(cli, processor, timeout_ms, stdout_lines)

        return build_final_response(
            cli, process.returncode, processor.get_result(), stdout_lines, stderr
        )
    except (OSError, ValueError) as e:
        # OSError covers I/O failures on the pipe; ValueError covers reading
        # from a closed file. Anything else propagates so it's not silently
        # swallowed.
        process.kill()
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


def execute_agent(inv: AgentInvocation, timeout_ms: int = 600000,
                  debug_dir: str | None = None) -> dict:
    """Execute agent CLI for the given invocation. Returns a response dict.

    Response shape: ``{result, exit_code, status, cli, error?}`` plus the
    telemetry/trust fields documented in SKILL.md (report, model, permission,
    elapsed_ms, ...).
    """
    started = time.monotonic()
    command, args, env_override = build_invocation_args(inv)
    command, args = _resolve_launch(command, args)
    proc_env = _merge_env(env_override)

    def _stamp(resp: dict) -> dict:
        # Wall-clock per dispatch — orchestrators need this for concurrency
        # tuning and it costs nothing to provide.
        resp["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        # Trust fields: what was ASKED FOR vs what the backend REPORTED serving.
        # resolved=None means the backend didn't say (agy never does) — absence
        # of proof, not proof of the requested model.
        resp["model"] = {"requested": inv.model, "resolved": resp.pop("model_resolved", None)}
        resp["permission"] = inv.permission
        try:
            resp["permission_flags"] = permission_flags(inv.cli, inv.permission)
        except ValueError:
            resp["permission_flags"] = None
        raw = resp.pop("_debug_raw", None)
        if debug_dir:
            dbg = _write_debug(debug_dir, [command, *args], raw or "", resp)
            if dbg:
                resp["debug_file"] = dbg
        return resp

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
