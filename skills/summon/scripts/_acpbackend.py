"""ACP (Agent Client Protocol) backend — run a native-ACP CLI over JSON-RPC.

Speaks ACP (JSON-RPC 2.0, newline-delimited JSON over stdio) to the backends
with NATIVE support — ``gemini --acp``, ``kimi acp``, ``cursor-agent acp`` —
instead of summon's usual one-shot spawn-and-drain. Registered as the ``"acp"``
key on a BACKENDS entry (see ``_builder.BACKENDS``); the executor calls
:func:`call` when ``inv.transport == "acp"`` exactly like it calls
``_apibackend.call`` for the api kind, and the returned dict flows through the
same ``_enrich``/``_stamp`` pipeline.

What this transport changes, relative to the subprocess path:

- The prompt travels as a JSON-RPC parameter over stdin, NOT as an argv token —
  no Windows 32767-char CreateProcess limit, no .cmd shim quoting issues.
- Output arrives as ONE normalized event schema (``session/update``), not
  per-vendor NDJSON that ``_stream.StreamProcessor`` has to sniff.
- Permissions are answered MID-RUN (``session/request_permission``) instead of
  being pre-approved by argv flags; summon auto-answers from ``inv.permission``
  (fail closed on anything it cannot classify).
- ACP has no system-prompt channel, so ``inv.system_context`` is PREPENDED to
  the user prompt text. Semantically different from ``--append-system-prompt``;
  an envelope warning says so.
- The session id is telemetry only. It is NOT a resume handle for the
  subprocess path (premortem T1): the executor leaves ``resume.session_id``
  None for ACP runs so ``_apply_schema`` takes its no-resume-lane branch.

Pure stdlib. No SDK, no node adapter.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import threading
import time

from _spawn import popen_flags, run_flags

# The ONE place that knows how each backend enters ACP mode. One supported
# spelling per backend, probed (see _probe_acp); legacy spellings (gemini's
# --experimental-acp era) are deliberately NOT auto-used.
ACP_ARGV = {
    "gemini": ["--acp"],
    "kimi": ["acp"],
    "cursor-agent": ["acp"],
}

_PROTOCOL_VERSION = 1
_TEARDOWN_GRACE_SEC = 3.0   # wait for exit after terminate() before kill-tree
_PROBE_TIMEOUT_SEC = 10.0
_MAX_ACP_FRAME_BYTES = 8 * 1024 * 1024
_MAX_ACP_SESSION_ID_CHARS = 512

# ACP stopReason -> summon status. Anything unknown maps conservative: a run we
# cannot classify is not a success.
_STOP_REASON_STATUS = {
    "end_turn": "success",
    "max_tokens": "partial",
    "max_turn_requests": "partial",
    "refusal": "error",
    "cancelled": "error",
}

# Tool-call kinds each permission tier lets through WITHOUT a human. Everything
# else is rejected (fail closed). Mirrors what the argv permission flags buy on
# the subprocess path: read-only = reads only, safe-edit = no shell execution.
_ALLOWABLE_KINDS = {
    "read-only": {"read", "fetch", "search", "think"},
    "safe-edit": {"read", "fetch", "search", "think", "edit"},
    "yolo": {"read", "fetch", "search", "think", "edit", "execute",
             "switch_mode", "move", "other"},
}


class _AcpError(Exception):
    """Protocol-level failure (JSON-RPC error result, broken pipe, bad spawn)."""


class _AcpTimeout(Exception):
    """A request did not answer inside its slice of the wall-clock deadline."""

    def __init__(self, message: str, *, stage: str | None = None,
                 error_kind: str | None = None) -> None:
        self.stage = stage
        self.error_kind = error_kind
        super().__init__(message)


class _AcpPayloadTooLarge(_AcpError):
    """A complete JSON-RPC frame exceeded Summon's local wire ceiling."""

    def __init__(self, budget: dict) -> None:
        self.budget = budget
        super().__init__(
            "ACP JSON-RPC frame exceeds Summon's local 8 MiB transport ceiling")


class _TurnState:
    """Accumulates everything one prompt turn produces. Reader thread writes,
    main thread reads at turn end — guarded by one lock.

    Bounded like the subprocess path's output cap: a noisy or hostile child
    must not exhaust dispatcher memory before the wall-clock deadline or the
    downstream truncation runs."""

    _MAX_TEXT_CHARS = 200_000
    _MAX_DIAG_LINES = 2_000

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.text: list = []        # agent_message_chunk content, in order
        self.diag: list = []        # human-readable tool/plan/thought lines
        self.usage: dict | None = None
        self._text_chars = 0
        self.truncated = False
        self.meaningful_events = 0
        try:
            from _progress_signals import ProgressSignals
            self.progress_signals = ProgressSignals()
        except Exception:  # noqa: BLE001
            self.progress_signals = None

    def add_text(self, chunk: str) -> None:
        with self.lock:
            self.meaningful_events += 1
            room = self._MAX_TEXT_CHARS - self._text_chars
            if room <= 0:
                self.truncated = True
                return
            if len(chunk) > room:
                chunk = chunk[:room]
                self.truncated = True
            self.text.append(chunk)
            self._text_chars += len(chunk)

    def add_diag(self, line: str) -> None:
        with self.lock:
            if len(self.diag) < self._MAX_DIAG_LINES:
                self.diag.append(line)
            elif len(self.diag) == self._MAX_DIAG_LINES:
                self.diag.append(
                    f"... diagnostics truncated at {self._MAX_DIAG_LINES} lines")

    def set_usage(self, usage: dict) -> None:
        with self.lock:
            self.usage = usage

    def record_tool_update(self, update: dict) -> None:
        with self.lock:
            self.meaningful_events += 1
            if self.progress_signals is None:
                return
            call_id = update.get("toolCallId")
            title = update.get("title") or update.get("kind") or "acp_tool"
            args = update.get("rawInput") or update.get("locations") or update.get("title") or ""
            status = str(update.get("status") or "").lower()
            try:
                if status in ("", "pending", "in_progress", "running"):
                    if isinstance(call_id, str):
                        if call_id not in self.progress_signals._pending:
                            self.progress_signals.tool_call(str(title), args)
                            self.progress_signals.tool_started(call_id)
                    else:
                        self.progress_signals.tool_call(str(title), args)
                elif status in ("completed", "failed", "error", "cancelled"):
                    if isinstance(call_id, str) and call_id not in self.progress_signals._pending:
                        self.progress_signals.tool_call(str(title), args)
                    self.progress_signals.tool_finished(call_id)
                    self.progress_signals.tool_result(is_error=status in ("failed", "error"))
            except Exception:  # noqa: BLE001
                pass

    def snapshot(self) -> tuple:
        with self.lock:
            text = "".join(self.text)
            if self.truncated:
                text += f"\n...[turn text truncated at {self._MAX_TEXT_CHARS} chars]"
            return text, list(self.diag), self.usage


class _AcpClient:
    """Minimal JSON-RPC peer: one request in flight per id, notifications and
    agent->client requests dispatched from a single daemon reader thread.

    stdout is a PROTOCOL channel here — unlike the subprocess backends, stderr
    is NOT merged into it (a vendor log line would corrupt framing); stderr is
    drained by its own thread into diagnostics so a full pipe can never block
    the child.
    """

    def __init__(self, process: subprocess.Popen, permission: str,
                 cancelled=None, deadline_reached=None,
                 max_frame_bytes: int = _MAX_ACP_FRAME_BYTES,
                 breaker=None, runtime_control=None,
                 cli: str | None = None) -> None:
        self._proc = process
        self._permission = permission
        self._breaker = breaker
        self._runtime_control = runtime_control
        self._cli = cli
        self._cancelled = cancelled or (lambda: False)
        self._deadline_reached = deadline_reached or (lambda: False)
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._max_frame_bytes = max_frame_bytes
        self._pending: dict = {}
        self.turn = _TurnState()
        self.stderr_lines: list = []
        self._last_meaningful_events = 0
        if self._runtime_control is not None:
            try:
                self._runtime_control.refresh(force=True)
            except Exception:  # noqa: BLE001
                pass
            if self._breaker is not None:
                try:
                    sync_fn = getattr(self._breaker, "sync_from_control", None)
                    if callable(sync_fn):
                        sync_fn(self._runtime_control)
                except Exception:  # noqa: BLE001
                    pass
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _poll_breaker(self) -> int:
        """Evaluate runtime_control / breaker, return newly applied operator extension ms,
        and raise _AcpTimeout on cancellation, expiry, or enforced containment."""
        rc = self._runtime_control
        breaker = self._breaker
        operator_added_ms = 0
        if rc is not None:
            try:
                added = rc.refresh(force=True)
                if isinstance(added, int) and not isinstance(added, bool) and added > 0:
                    operator_added_ms = added
            except Exception:  # noqa: BLE001
                pass
            if breaker is not None:
                try:
                    sync_fn = getattr(breaker, "sync_from_control", None)
                    if callable(sync_fn):
                        sync_fn(rc)
                except Exception:  # noqa: BLE001
                    pass
            if getattr(rc, "cancel_requested", False):
                raise _AcpTimeout("ACP session cancelled by job control",
                                  stage="cancelled", error_kind="cancelled")
            due_fn = getattr(rc, "checkpoint_due", None)
            is_due = bool(callable(due_fn) and due_fn())
            with self.turn.lock:
                cur_events = self.turn.meaningful_events
                usage_snap = dict(self.turn.usage) if isinstance(self.turn.usage, dict) else None
            stream_active = cur_events > self._last_meaningful_events
            if is_due:
                self._last_meaningful_events = cur_events
                try:
                    rc.checkpoint(active=stream_active)
                except Exception:  # noqa: BLE001
                    pass
            if breaker is not None and is_due:
                try:
                    _wt = None
                    if usage_snap:
                        try:
                            from _usage_metrics import work_tokens as _calc_wt
                            _wt = _calc_wt(usage_snap, cli=self._cli)
                        except Exception:  # noqa: BLE001
                            _wt = None
                    decision = breaker.evaluate(
                        signals=self.turn.progress_signals,
                        progress_record=getattr(rc, "progress", None),
                        work_tokens=_wt,
                        extra_triggers=("no_progress",) if not stream_active else (),
                    )
                    record_fn = getattr(rc, "record_breaker", None)
                    if callable(record_fn):
                        record_fn(decision)
                    if isinstance(decision, dict) and decision.get("enforced"):
                        if decision.get("action") == "steer":
                            rc.attention_required = True
                except Exception:  # noqa: BLE001
                    pass
            enforced_stop = bool(
                (breaker is not None and getattr(breaker, "mode", None) == "enforce"
                 and getattr(breaker, "level", None) == "stopped")
                or ((breaker is None or getattr(breaker, "mode", None) == "enforce")
                    and getattr(rc, "breaker_stopped", False))
            )
            enforced_constrain = bool(
                (breaker is not None and getattr(breaker, "mode", None) == "enforce"
                 and getattr(breaker, "level", None) == "constrained")
                or ((breaker is None or getattr(breaker, "mode", None) == "enforce")
                    and getattr(rc, "constrained", False))
            )
            if enforced_stop or enforced_constrain:
                stage = "breaker_stopped" if enforced_stop else "breaker_constrained"
                raise _AcpTimeout(f"ACP turn stopped by {stage}",
                                  stage=stage, error_kind=stage)
            expired_fn = getattr(rc, "expired", None)
            if callable(expired_fn) and expired_fn():
                clock_fn = getattr(rc, "clock", None) or time.monotonic
                hard_dl = getattr(rc, "hard_deadline", None)
                is_hard = isinstance(hard_dl, (int, float)) and clock_fn() >= hard_dl
                exp_stage = "adaptive_hard_timeout" if is_hard else "adaptive_attention_timeout"
                exp_kind = "job_hard_timeout" if is_hard else "adaptive_attention_timeout"
                raise _AcpTimeout(f"ACP turn expired at {exp_stage}",
                                  stage=exp_stage, error_kind=exp_kind)
        elif breaker is not None:
            if getattr(breaker, "mode", None) == "enforce":
                lvl = getattr(breaker, "level", None)
                if lvl in ("constrained", "stopped"):
                    stage = f"breaker_{lvl}"
                    raise _AcpTimeout(f"ACP turn stopped by {stage}",
                                      stage=stage, error_kind=stage)
        return operator_added_ms

    # -- IO ---------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        # Popen's text-mode stdin uses the host newline policy. On Windows the
        # TextIOWrapper rewrites this delimiter to CRLF; measure those exact
        # bytes rather than the pre-translation LF string. JSON string content
        # is escaped, so the delimiter is the only literal newline here.
        wire_line = line.replace("\n", "\r\n") if os.name == "nt" else line
        from _transport_budget import evaluate_serialized_payload
        budget = evaluate_serialized_payload(
            operation_id=hashlib.sha256(
                wire_line.encode("utf-8", errors="surrogatepass")).hexdigest()[:32],
            payload=wire_line,
            capability={"kind": "serialized", "platform": os.name,
                         "max_serialized_bytes": self._max_frame_bytes},
            boundary="acp_json_rpc")
        if budget["status"] == "blocked":
            raise _AcpPayloadTooLarge(budget)
        with self._write_lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise _AcpError(f"agent stdin pipe broken: {type(e).__name__}: {e}")

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr:
                # Bounded like the turn state: stderr is diagnostics, not a
                # memory sink for a noisy child.
                if len(self.stderr_lines) <= _TurnState._MAX_DIAG_LINES:
                    self.stderr_lines.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    # A non-JSON line on a protocol channel is vendor noise
                    # (banner/log bleed). Diagnostics only, never fatal.
                    self.turn.add_diag(f"non-JSON stdout line skipped: {line[:200]}")
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception as e:  # noqa: BLE001 — the reader must not die
                    self.turn.add_diag(f"dispatch error: {type(e).__name__}: {e}")
        except (OSError, ValueError):
            pass
        finally:
            # EOF on stdout: fail every in-flight request so waiters wake up
            # instead of running out the full wall-clock deadline.
            with self._id_lock:
                pending = list(self._pending.values())
            for slot in pending:
                slot["response"] = {"error": {"code": -32000,
                                              "message": "agent closed stdout (EOF)"}}
                slot["event"].set()

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:
            self._handle_agent_request(msg)
        elif "method" in msg:
            self._handle_notification(msg)
        elif "id" in msg:
            with self._id_lock:
                slot = self._pending.pop(msg["id"], None)
            if slot is not None:
                slot["response"] = msg
                slot["event"].set()

    # -- RPC --------------------------------------------------------------

    def request(self, method: str, params: dict, timeout: float) -> dict:
        """Send a request and wait for its result. Raises _AcpError on a
        JSON-RPC error result, _AcpTimeout when the slice expires."""
        clock_fn = getattr(self._runtime_control, "clock", None) or time.monotonic
        seen_op_ext_ms = int(getattr(self._runtime_control, "operator_extension_ms", 0) or 0)
        pre_added_ms = self._poll_breaker()
        cur_op_ext_ms = int(getattr(self._runtime_control, "operator_extension_ms", 0) or 0)
        delta_pre = max(pre_added_ms, max(0, cur_op_ext_ms - seen_op_ext_ms))
        if delta_pre > 0:
            timeout += delta_pre / 1000.0
            seen_op_ext_ms = max(seen_op_ext_ms + delta_pre, cur_op_ext_ms)
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "response": None}
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params})
        except Exception:
            with self._id_lock:
                self._pending.pop(rid, None)
            raise
        deadline = clock_fn() + max(0.05, timeout)
        while not slot["event"].wait(min(0.1, max(0.0, deadline - clock_fn()))):
            try:
                cancelled = self._cancelled()
            except Exception as e:
                with self._id_lock:
                    self._pending.pop(rid, None)
                raise _AcpError(
                    f"{method} cancellation check failed ({type(e).__name__})") from e
            if cancelled:
                with self._id_lock:
                    self._pending.pop(rid, None)
                raise _AcpTimeout(f"{method} cancelled")
            try:
                if self._deadline_reached():
                    with self._id_lock:
                        self._pending.pop(rid, None)
                    raise _AcpTimeout(f"{method} deadline exceeded")
            except _AcpTimeout:
                raise
            except Exception as e:
                with self._id_lock:
                    self._pending.pop(rid, None)
                raise _AcpError(
                    f"{method} deadline check failed ({type(e).__name__})") from e
            try:
                added_ms = self._poll_breaker()
            except _AcpTimeout:
                with self._id_lock:
                    self._pending.pop(rid, None)
                raise
            cur_op_ext_ms = int(getattr(self._runtime_control, "operator_extension_ms", 0) or 0)
            delta_ms = max(added_ms, max(0, cur_op_ext_ms - seen_op_ext_ms))
            if delta_ms > 0:
                deadline += delta_ms / 1000.0
                timeout += delta_ms / 1000.0
                seen_op_ext_ms = max(seen_op_ext_ms + delta_ms, cur_op_ext_ms)
            if clock_fn() >= deadline:
                break
        if not slot["event"].is_set():
            with self._id_lock:
                self._pending.pop(rid, None)
            raise _AcpTimeout(f"{method} did not answer within {timeout:.0f}s")
        # Evaluate breaker/control even when the RPC response arrives before the
        # first wait slice expires (fast responses must not bypass containment).
        self._poll_breaker()
        resp = slot["response"]
        if "error" in resp:
            err = resp["error"] or {}
            raise _AcpError(f"{method} failed: {err.get('code')}: "
                            f"{err.get('message', '?')}")
        result = resp.get("result")
        if result is not None and not isinstance(result, dict):
            raise _AcpError(f"{method} returned a non-object result: protocol violation")
        return result or {}

    def notify(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except _AcpError:
            pass  # notifications are best-effort (notably session/cancel)

    # -- agent -> client traffic -------------------------------------------

    def _handle_notification(self, msg: dict) -> None:
        if msg.get("method") != "session/update":
            return
        params = msg.get("params") or {}
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                self.turn.add_text(content["text"])
        elif kind == "usage_update":
            self.turn.set_usage({k: v for k, v in update.items()
                                 if k != "sessionUpdate"})
        elif kind in ("tool_call", "tool_call_update"):
            title = update.get("title") or update.get("toolCallId") or "?"
            status = update.get("status") or ""
            self.turn.add_diag(f"tool_call {title} {status}".strip())
            self.turn.record_tool_update(update)
        elif kind in ("plan", "agent_thought_chunk", "available_commands_update",
                      "current_mode_update"):
            self.turn.add_diag(f"{kind} received")

    def _handle_agent_request(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "session/request_permission":
            chosen = self._answer_permission(msg.get("params") or {})
            if chosen is None:
                # No offered option can be selected safely; the turn cannot
                # proceed within the declared tier, so cancel it. Inventing or
                # positionally guessing an optionId could GRANT authority.
                outcome = {"outcome": "cancelled"}
            else:
                outcome = {"outcome": "selected", "optionId": chosen}
            response = {"jsonrpc": "2.0", "id": msg["id"],
                        "result": {"outcome": outcome}}
        else:
            # We declare no fs/terminal capability at initialize, so anything
            # else is out of contract; refuse it rather than hanging the agent.
            response = {"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601,
                                  "message": f"summon does not implement {method}"}}
        try:
            self._send(response)
        except _AcpError:
            pass

    def _answer_permission(self, params: dict) -> str | None:
        """Pick an optionId for session/request_permission from the invocation's
        permission tier. Prefers *_once kinds — one dispatch must never grant
        standing permission. Fail CLOSED: the only options ever selected are a
        present allow_once (in-tier) or a present reject_*. The ACP option list
        carries NO ordering guarantee, so position is not evidence: when
        nothing offered can be classified safely, return None and let the
        caller cancel the turn instead of guessing an optionId the agent never
        offered."""
        options = params.get("options") or []
        tool_kind = ((params.get("toolCall") or {}).get("kind") or "other")
        rc = self._runtime_control
        if rc is not None:
            try:
                rc.refresh(force=True)
            except Exception:  # noqa: BLE001
                pass
            if self._breaker is not None:
                try:
                    sync_fn = getattr(self._breaker, "sync_from_control", None)
                    if callable(sync_fn):
                        sync_fn(rc)
                except Exception:  # noqa: BLE001
                    pass
            if getattr(rc, "cancel_requested", False):
                return None
            if ((self._breaker is None or getattr(self._breaker, "mode", None) == "enforce")
                    and getattr(rc, "breaker_stopped", False)):
                return None
        if (self._breaker is not None
                and getattr(self._breaker, "mode", None) == "enforce"
                and getattr(self._breaker, "level", None) == "stopped"):
            return None
        effective_perm = (
            self._breaker.effective_permission(self._permission)
            if self._breaker is not None
            and callable(getattr(self._breaker, "effective_permission", None))
            else self._permission
        )
        if (rc is not None
                and (self._breaker is None or getattr(self._breaker, "mode", None) == "enforce")
                and getattr(rc, "constrained", False)):
            from _builder import clamp_permission
            effective_perm = clamp_permission(effective_perm, "read-only")
        allowed = tool_kind in _ALLOWABLE_KINDS.get(str(effective_perm).lower(), set())

        def _find(*kinds: str) -> str | None:
            for opt in options:
                if str(opt.get("kind", "")) in kinds:
                    oid = opt.get("optionId")
                    # Only a string optionId can be echoed back lawfully.
                    if isinstance(oid, str):
                        return oid
            return None

        if allowed:
            # allow_once ONLY: one dispatch must never grant standing
            # permission, so allow_always is treated as "no allow option".
            chosen = _find("allow_once") or _find("reject_once", "reject_always")
        else:
            chosen = _find("reject_once", "reject_always")
        if chosen is None:
            self.turn.add_diag(
                f"permission {effective_perm}: tool kind {tool_kind!r} has no "
                f"safely classifiable option; cancelling the turn (fail closed)")
        else:
            decision = "allow" if _find("allow_once") == chosen else "reject"
            self.turn.add_diag(
                f"permission {effective_perm}: tool kind {tool_kind!r} -> "
                f"{decision} (option {chosen!r})")
        return chosen


# -- capability probe ---------------------------------------------------------

_probe_cache: dict = {}
_probe_lock = threading.Lock()


def _probe_acp(cli: str) -> str | None:
    """Cheap up-front check that the installed CLI plausibly speaks ACP.

    Returns an error string when ACP is clearly unsupported, None when the
    probe passes or is inconclusive (a timed-out probe must not block a
    dispatch — the handshake will surface a real failure). Cached per cli:
    probing on every fallback would add a spawn to an already-slow path.
    """
    with _probe_lock:
        if cli in _probe_cache:
            return _probe_cache[cli]
    verdict = None
    path = shutil.which(cli)
    if not path:
        verdict = f"CLI not found on PATH: {cli}"
    else:
        # Windows: a .cmd/.bat shim cannot be exec'd directly — route through
        # cmd.exe (same handling as _doctor._probe_version).
        cmd = [path, "--help"]
        if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c", path, "--help"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=_PROBE_TIMEOUT_SEC, stdin=subprocess.DEVNULL,
                               **run_flags())
            blob = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
            if "acp" not in blob:
                verdict = (f"installed {cli} does not advertise ACP support "
                           f"(no 'acp' in --help); upgrade the CLI or use "
                           f"--no-acp-fallback")
        except (OSError, ValueError, subprocess.SubprocessError):
            verdict = None  # inconclusive: let the handshake speak
    with _probe_lock:
        _probe_cache[cli] = verdict
    return verdict


def _reset_probe_cache() -> None:
    """Test hook: probes cache per process, but tests install fake CLIs."""
    with _probe_lock:
        _probe_cache.clear()


# -- main entry point -----------------------------------------------------------

def _err(cli: str, code: int, msg: str, diag: list | None = None, *,
         not_run: bool = False, error_kind: str | None = None) -> dict:
    resp = {"result": "", "exit_code": code, "status": "error", "cli": cli,
            "error": msg}
    if not_run:
        resp.update({
            "attempts": 0, "attempt_status": "not_run",
            "execution_status": "not_run", "provider_contacted": False,
            "result_usable": False, "retryable": False,
        })
    if error_kind:
        resp["error_kind"] = error_kind
    if diag:
        resp["_debug_raw"] = "\n".join(diag)
    return resp


def call(inv, timeout_ms: int, *, launch_control=None) -> dict:
    """Run one prompt turn over ACP. Returns a response dict in the same shape
    the subprocess backends produce (result/status/exit_code/cli + session_id/
    usage/_debug_raw), so it flows through _enrich/_stamp unchanged."""
    cli = inv.cli
    acp_args = ACP_ARGV.get(cli)
    if acp_args is None:
        return _err(cli, 2, f"backend {cli!r} has no acp transport")

    if inv.resume_id:
        # An ACP session id is telemetry, not a resume handle (premortem T1);
        # starting a fresh session while the caller believes they resumed is
        # worse than refusing.
        return _err(cli, 2, "resume is not supported over the ACP transport: an "
                            "ACP session id cannot continue a prior dispatch. Use the "
                            "subprocess transport's resume, or a fresh run.")
    # Lazy: _builder imports this module's call at dispatch time; importing the
    # executor at module load would create an import cycle.
    from _executor import ProviderDeadlineError, _kill_tree, _resolve_launch

    if launch_control is None or launch_control.allow_secondary:
        probe_err = _probe_acp(cli)
        if probe_err:
            return _err(cli, 2, probe_err)

    try:
        command, args = _resolve_launch(cli, list(acp_args))
    except ValueError as exc:
        return _err(
            cli, 2, str(exc), not_run=True,
            error_kind="unsafe_windows_launcher")
    # Bound the complete prompt frame before creating adapter-owned profiles or
    # the ACP child. The future session id is not known yet, so reserve a
    # conservative local identifier envelope; _AcpClient re-measures the exact
    # final frame before each write.
    prompt_text = ((inv.system_context + "\n\n" + inv.prompt)
                   if inv.system_context else inv.prompt)
    try:
        from _transport_budget import evaluate_serialized_payload
        _prompt_frame = json.dumps({
            "jsonrpc": "2.0", "id": 2**63 - 1, "method": "session/prompt",
            "params": {"sessionId": "s" * _MAX_ACP_SESSION_ID_CHARS,
                        "prompt": [{"type": "text", "text": prompt_text}]},
        }, ensure_ascii=False) + "\n"
        _prompt_wire_frame = (_prompt_frame.replace("\n", "\r\n")
                              if os.name == "nt" else _prompt_frame)
        _prompt_budget = evaluate_serialized_payload(
            operation_id=hashlib.sha256(
                _prompt_wire_frame.encode("utf-8", errors="surrogatepass")).hexdigest()[:32],
            payload=_prompt_wire_frame,
            capability={"kind": "serialized", "platform": os.name,
                         "max_serialized_bytes": _MAX_ACP_FRAME_BYTES},
            boundary="acp_json_rpc")
    except Exception:
        return _err(cli, 1, "ACP transport capability preflight refused",
                    not_run=True, error_kind="transport_capability_invalid")
    if _prompt_budget["status"] == "blocked":
        response = _err(
            cli, 1,
            "serialized ACP prompt exceeds Summon's local 8 MiB transport "
            "ceiling; shorten the required prompt or use a bounded file-aware "
            "route",
            not_run=True,
            error_kind="transport_budget_exceeded",
        )
        response["transport_budget"] = _prompt_budget
        return response

    # The subprocess builders install the per-backend identity (kimi's isolated
    # credential profile above all). Running ACP without that env would
    # authenticate as the AMBIENT account with the full real profile (MCP
    # config, sessions, logs) -- a different identity than the receipt records,
    # and exactly what the isolation work exists to prevent. Reuse the
    # builder's env, minus the channel ACP supersedes: GEMINI_SYSTEM_MD, since
    # the system context is prepended to the prompt on this transport.
    from _builder import build_invocation_args as _build_args
    try:
        if launch_control is None:
            _, _, env_override = _build_args(inv)
        else:
            # Kimi's ACP path also creates a fresh credential profile during
            # builder setup.  Register it before the ACP provider boundary so
            # controlled deliberation cleanup owns the same resource contract
            # as the subprocess path.  Legacy calls keep the old signature.
            _, _, env_override = _build_args(
                inv, resource_register=launch_control.register_resource)
    except (ValueError, TypeError) as e:
        if launch_control is not None:
            return _err(cli, 2,
                        f"provider preparation refused ({type(e).__name__})")
        return _err(cli, 2, str(e))
    from _spawn import scrub_provider_env
    child_env = dict(os.environ)
    for key, value in (env_override or {}).items():
        if key != "GEMINI_SYSTEM_MD":
            child_env[key] = value
    child_env = scrub_provider_env(child_env)
    try:
        if launch_control is not None:
            launch_control.before_provider_launch({
                "backend": cli,
                "transport": "acp",
            })
        process = subprocess.Popen(
            [command, *args],
            cwd=inv.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,   # NOT merged: stdout is a protocol channel
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=child_env,
            **popen_flags(),
        )
    except FileNotFoundError:
        return _err(cli, 127, f"CLI not found: {command}")
    except OSError as e:
        return _err(cli, 1, f"{type(e).__name__}: {e}")
    except ProviderDeadlineError:
        return _err(cli, 124, "provider launch deadline exceeded")
    except Exception as e:
        return _err(cli, 1,
                    f"provider launch refused by control ({type(e).__name__})")

    if launch_control is not None:
        try:
            launch_control.spawned(process)
        except Exception as e:
            _kill_tree(process)
            try:
                process.wait(timeout=_TEARDOWN_GRACE_SEC)
            except Exception:
                _kill_tree(process)
            try:
                launch_control.reaped(process)
            except Exception:
                pass
            return _err(
                cli, 1,
                f"provider process registration failed ({type(e).__name__}); "
                "child was terminated")

    # Windows: kill-on-close Job Object, same reasoning as the subprocess path
    # (an unexpected summon death must not orphan a paid backend).
    try:
        from _jobobj import attach as _job_attach
        _job_attach(process)
    except Exception:  # noqa: BLE001 — teardown plumbing never breaks dispatch
        pass

    # ``Milliseconds`` deliberately renders as e.g. ``360000ms`` when it is
    # forwarded to a detached child. Keep a plain integer for arithmetic and
    # human diagnostics here: appending another unit to the rich int produced
    # the unhelpful ``360000msms`` timeout report in the field.
    timeout_budget_ms = int(timeout_ms)
    runtime_control = None
    breaker = None
    try:
        from _job_control import RuntimeControl
        runtime_control = RuntimeControl.from_environment(
            timeout_budget_ms,
            attempt_id=getattr(inv, "attempt_id", None),
            attempt_kind=getattr(inv, "attempt_kind", "initial"),
            attempt_ordinal=getattr(inv, "attempt_ordinal", 1),
        )
    except Exception as e:
        _kill_tree(process)
        try:
            process.wait(timeout=_TEARDOWN_GRACE_SEC)
        except Exception:
            _kill_tree(process)
        try:
            from _jobobj import close as _job_close
            _job_close(process)
        except Exception:
            pass
        if launch_control is not None:
            try:
                launch_control.reaped(process)
            except Exception:
                pass
        return _err(
            cli, 1,
            f"adaptive job control initialization failed ({type(e).__name__})",
            error_kind="job_control_unavailable",
        )
    if runtime_control is not None:
        try:
            from _breaker import (
                GraduatedBreaker,
                allow_stop_from_environment,
                max_work_tokens_from_environment,
                mode_from_environment as _breaker_mode_from_environment,
            )
            _bmode = _breaker_mode_from_environment()
            if _bmode != "off":
                breaker = GraduatedBreaker(
                    mode=_bmode,
                    allow_stop=allow_stop_from_environment(),
                    max_work_tokens=max_work_tokens_from_environment(),
                    clock=getattr(runtime_control, "clock", time.monotonic),
                )
        except Exception as e:
            _kill_tree(process)
            try:
                process.wait(timeout=_TEARDOWN_GRACE_SEC)
            except Exception:
                _kill_tree(process)
            try:
                from _jobobj import close as _job_close
                _job_close(process)
            except Exception:
                pass
            if launch_control is not None:
                try:
                    launch_control.reaped(process)
                except Exception:
                    pass
            return _err(
                cli, 1,
                f"adaptive circuit breaker initialization failed ({type(e).__name__})",
                error_kind="job_control_unavailable",
            )

    def _attach_control_projections(resp: dict) -> dict:
        if breaker is not None:
            try:
                resp["breaker"] = breaker.projection()
            except Exception:  # noqa: BLE001
                pass
        if runtime_control is not None:
            try:
                resp["runtime_control"] = runtime_control.projection()
            except Exception:  # noqa: BLE001
                pass
        if client.turn.progress_signals is not None:
            try:
                resp["progress_signals"] = client.turn.progress_signals.snapshot()
            except Exception:  # noqa: BLE001
                pass
        return resp

    hard_budget_ms = (runtime_control.max_runtime_ms
                      if runtime_control is not None else timeout_budget_ms)
    deadline = time.monotonic() + hard_budget_ms / 1000
    client = _AcpClient(
        process, inv.permission,
        cancelled=(launch_control.is_cancelled if launch_control is not None else None),
        deadline_reached=(launch_control.is_deadline_reached
                          if launch_control is not None else None),
        max_frame_bytes=_MAX_ACP_FRAME_BYTES,
        breaker=breaker,
        runtime_control=runtime_control,
        cli=cli)
    stage = "initialize"
    try:
        def _remaining() -> float:
            if runtime_control is not None:
                return runtime_control.hard_deadline - runtime_control.clock()
            return deadline - time.monotonic()

        # 1. Handshake. No fs/terminal capability: agents then use their own
        # built-in tools instead of proxying I/O through us.
        init = client.request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "summon", "version": "1"},
        }, timeout=min(30.0, max(0.05, _remaining())))

        if init.get("protocolVersion") != _PROTOCOL_VERSION:
            raise _AcpError(
                f"agent negotiated ACP protocol {init.get('protocolVersion')!r}; "
                f"summon speaks version {_PROTOCOL_VERSION}")

        # 2. Session.
        stage = "session/new"
        session = client.request("session/new", {
            "cwd": os.path.abspath(inv.cwd),
            "mcpServers": [],
        }, timeout=min(60.0, max(0.05, _remaining())))
        session_id = session.get("sessionId")

        if not isinstance(session_id, str) or not session_id:
            raise _AcpError("session/new returned no usable sessionId "
                            "(protocol violation)")

        # 3. Model pinning is best-effort: ACP's stable schema has no model on
        # session/new; sessions that advertise a model list may accept
        # session/set_model. Never FAKE the pin — an unverifiable request gets
        # a warning, not a model_resolved claim.
        model_targeted = None
        warnings: list = []
        if inv.model:
            models = (session.get("models") or {})
            available = models.get("availableModels") or []
            wanted = str(inv.model).lower()
            hit = next((m for m in available
                        if str(m.get("modelId", "")).lower() == wanted
                        or str(m.get("name", "")).lower() == wanted), None)
            if hit:
                try:
                    stage = "session/set_model"
                    client.request("session/set_model", {
                        "sessionId": session_id,
                        "modelId": hit["modelId"],
                    }, timeout=min(15.0, max(0.05, _remaining())))
                    model_targeted = inv.model
                except (_AcpError, _AcpTimeout):
                    warnings.append(
                        f"model {inv.model!r} was advertised but session/set_model "
                        f"was rejected over ACP; the backend default may run instead")
            else:
                warnings.append(
                    f"model pinning over ACP is best-effort and {cli} advertised no "
                    f"model list; requested {inv.model!r}, the backend default runs "
                    f"(subprocess transport pins via argv)")

        if inv.system_context:
            warnings.append(
                "ACP has no system-prompt channel; the agent definition was "
                "prepended to the user prompt (subprocess transport uses a "
                "dedicated flag)")
        # 4. The turn. Ends at the session/prompt RESPONSE, not at process
        # exit — ACP agents are long-lived by design.
        stage = "session/prompt"
        try:
            result = client.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt_text}],
            }, timeout=max(0.05, _remaining()))
        except _AcpTimeout:
            # Politeness first: cancel the turn so a cooperative agent stops
            # spending tokens; the finally-block teardown kills the tree either
            # way. The late "cancelled" prompt response is simply discarded
            # (our pending slot is already gone).
            client.notify("session/cancel", {"sessionId": session_id})
            raise

        if "stopReason" not in result:
            raise _AcpError("session/prompt response carried no stopReason: "
                            "protocol violation (a malformed success is not a success)")
        stop = result["stopReason"]
        text, diag, usage = client.turn.snapshot()
        status = _STOP_REASON_STATUS.get(stop, "error")
        resp = {
            "result": text,
            "exit_code": 0 if status == "success" else 1,
            "status": status,
            "cli": cli,
            "session_id": session_id,
        }
        if status == "success" and (not isinstance(text, str) or not text.strip()):
            # ``end_turn`` describes protocol state, not result usefulness.
            # Reuse the executor's idempotent contract firewall here so the
            # backend and every other transport cannot diverge on the exit
            # tuple.  The ACP-specific diagnostic remains bounded and avoids
            # asserting an authentication failure as fact.
            from _executor import normalize_empty_success
            normalize_empty_success(resp)
            resp["normalization_reason"] = (
                "empty ACP completion cannot be accepted as a completed "
                "Summon result")
            resp["error"] = (
                f"ACP stopReason {stop!r} reported success but returned no usable "
                "result")
            status = resp["status"]
        if status != "success":
            resp.setdefault("error", f"agent stopped: {stop}")
        if usage:
            resp["usage"] = usage
        if model_targeted:
            resp["model_targeted"] = model_targeted
        if warnings:
            resp["warnings"] = warnings
        raw = diag + (["", "# stderr"] + client.stderr_lines
                      if client.stderr_lines else [])
        if raw:
            resp["_debug_raw"] = "\n".join(raw)
        return _attach_control_projections(resp)

    except _AcpTimeout as e:
        eff_stage = getattr(e, "stage", None) or stage
        err_kind = getattr(e, "error_kind", None)
        text, diag, usage = client.turn.snapshot()
        if eff_stage == "cancelled" or err_kind == "cancelled":
            resp = {
                "result": text,
                "exit_code": 130,
                "status": "cancelled",
                "cli": cli,
                "error": "ACP session cancelled by job control"
                         + ("; partial output preserved" if text else ""),
                "normalization_reason": "cancelled by job control",
                "error_kind": "cancelled",
            }
        else:
            resp = {
                "result": text,
                "exit_code": 124,
                "status": "partial" if text else "error",
                "cli": cli,
                "error": f"timed out after {timeout_budget_ms}ms over ACP during {eff_stage}"
                         + ("; partial output preserved" if text else ""),
                "normalization_reason": ("timed out; partial output preserved" if text
                                         else "timed out before any usable output"),
                "timeout": {"budget_ms": timeout_budget_ms, "stage": eff_stage,
                            "partial_output": bool(text)},
            }
            if err_kind:
                resp["error_kind"] = err_kind
        if usage:
            resp["usage"] = usage
        raw = diag + (["", "# stderr"] + client.stderr_lines
                      if client.stderr_lines else [])
        if raw:
            resp["_debug_raw"] = "\n".join(raw)
        return _attach_control_projections(resp)
    except _AcpPayloadTooLarge as e:
        _, diag, _ = client.turn.snapshot()
        resp = _err(cli, 1, str(e), diag or None,
                    error_kind="transport_budget_exceeded")
        resp["provider_contacted"] = True
        resp["submission_state"] = "indeterminate"
        resp["transport_budget"] = e.budget
        return _attach_control_projections(resp)
    except _AcpError as e:
        _, diag, _ = client.turn.snapshot()
        tail = "\n".join(client.stderr_lines[-20:])
        return _attach_control_projections(
            _err(cli, 1, f"{e}" + (f" | stderr tail: {tail[:400]}" if tail else ""),
                 diag or None))
    finally:
        # Premortem T4: NEVER wait on natural process exit after the turn ends —
        # ACP agents stay alive for the next prompt. Close stdin, then kill the
        # TREE while its leader is still alive. Calling ``process.terminate()``
        # first opens the exact Windows escape hatch the Job Object/taskkill
        # fallback exists to close: an unassigned grandchild is orphaned as its
        # leader exits, and the later tree walk cannot find it. ``_kill_tree``
        # uses the Job Object when available and taskkill while the leader is
        # still attached otherwise. Runs on every exit path (success, error,
        # timeout) and costs milliseconds on a cooperative agent.
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        _kill_tree(process)
        try:
            process.wait(timeout=_TEARDOWN_GRACE_SEC)
        except Exception:  # noqa: BLE001
            _kill_tree(process)
        try:
            from _jobobj import close as _job_close
            _job_close(process)
        except Exception:  # noqa: BLE001
            pass
        if cli == "kimi" and env_override:
            try:
                from _builder import sync_kimi_profile_credentials
                sync_kimi_profile_credentials(env_override.get("KIMI_CODE_HOME"))
            except Exception:  # noqa: BLE001 - credential sync must not mask result
                pass
        if launch_control is not None:
            try:
                launch_control.reaped(process)
            except Exception:
                pass
