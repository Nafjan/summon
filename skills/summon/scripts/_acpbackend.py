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

    def add_text(self, chunk: str) -> None:
        with self.lock:
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

    def __init__(self, process: subprocess.Popen, permission: str) -> None:
        self._proc = process
        self._permission = permission
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict = {}
        self.turn = _TurnState()
        self.stderr_lines: list = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._err_reader.start()

    # -- IO ---------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
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
        if not slot["event"].wait(max(0.05, timeout)):
            with self._id_lock:
                self._pending.pop(rid, None)
            raise _AcpTimeout(f"{method} did not answer within {timeout:.0f}s")
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
        allowed = tool_kind in _ALLOWABLE_KINDS.get(str(self._permission).lower(), set())

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
                f"permission {self._permission}: tool kind {tool_kind!r} has no "
                f"safely classifiable option; cancelling the turn (fail closed)")
        else:
            decision = "allow" if _find("allow_once") == chosen else "reject"
            self.turn.add_diag(
                f"permission {self._permission}: tool kind {tool_kind!r} -> "
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

def _err(cli: str, code: int, msg: str, diag: list | None = None) -> dict:
    resp = {"result": "", "exit_code": code, "status": "error", "cli": cli,
            "error": msg}
    if diag:
        resp["_debug_raw"] = "\n".join(diag)
    return resp


def call(inv, timeout_ms: int) -> dict:
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
    from _executor import _kill_tree, _resolve_launch

    probe_err = _probe_acp(cli)
    if probe_err:
        return _err(cli, 2, probe_err)

    command, args = _resolve_launch(cli, list(acp_args))
    # The subprocess builders install the per-backend identity (kimi's isolated
    # credential profile above all). Running ACP without that env would
    # authenticate as the AMBIENT account with the full real profile (MCP
    # config, sessions, logs) -- a different identity than the receipt records,
    # and exactly what the isolation work exists to prevent. Reuse the
    # builder's env, minus the channel ACP supersedes: GEMINI_SYSTEM_MD, since
    # the system context is prepended to the prompt on this transport.
    from _builder import build_invocation_args as _build_args
    try:
        _, _, env_override = _build_args(inv)
    except ValueError as e:
        return _err(cli, 2, str(e))
    child_env = dict(os.environ)
    for key, value in (env_override or {}).items():
        if key != "GEMINI_SYSTEM_MD":
            child_env[key] = value
    try:
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
    deadline = time.monotonic() + timeout_budget_ms / 1000
    client = _AcpClient(process, inv.permission)
    stage = "initialize"
    try:
        def _remaining() -> float:
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
        prompt_text = ((inv.system_context + "\n\n" + inv.prompt)
                       if inv.system_context else inv.prompt)

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
        if status != "success":
            resp["error"] = f"agent stopped: {stop}"
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
        return resp

    except _AcpTimeout:
        text, diag, _ = client.turn.snapshot()
        resp = {
            "result": text,
            "exit_code": 124,
            "status": "partial" if text else "error",
            "cli": cli,
            "error": f"timed out after {timeout_budget_ms}ms over ACP during {stage}"
                     + ("; partial output preserved" if text else ""),
            "normalization_reason": ("timed out; partial output preserved" if text
                                     else "timed out before any usable output"),
            "timeout": {"budget_ms": timeout_budget_ms, "stage": stage,
                        "partial_output": bool(text)},
        }
        raw = diag + (["", "# stderr"] + client.stderr_lines
                      if client.stderr_lines else [])
        if raw:
            resp["_debug_raw"] = "\n".join(raw)
        return resp
    except _AcpError as e:
        _, diag, _ = client.turn.snapshot()
        tail = "\n".join(client.stderr_lines[-20:])
        return _err(cli, 1, f"{e}" + (f" | stderr tail: {tail[:400]}" if tail else ""),
                    diag or None)
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
