"""Authenticated local browser surface for conversation rooms.

The surface is intentionally separate from the deliberation control surface:
it can browse rooms, show bounded context, and append a human message.  It
cannot create a ballot or change policy; an explicit roster-agent turn is a
separate bounded provider launch with its own durable fence.  The URL bearer
is kept in the fragment and every API request still requires the header token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
import tempfile
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from _conversation import (ConversationError, ConversationJournal, MAX_EVENTS,
                           _public_payload, _root_path, _safe_id, group_rooms, list_rooms)
from _conversation_runtime import ConversationRuntime, ConversationRuntimeError
from _conversation_page import page_bytes


TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BODY_BYTES = 16 * 1024
SSE_POLL_SECONDS = 0.5
SSE_MAX_SECONDS = 15.0
SURFACE_RECORD = ".summon-conversation-ui.json"
MAX_SURFACE_RECORD_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_ACTIVE_CLIENTS = 32


class ConversationUIError(ValueError):
    """A browser request was unsafe or the room could not be read."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"status": "blocked", "error_kind": "conversation_ui_request_failed",
            "error": f"conversation request refused ({type(exc).__name__})"}


def _surface_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / SURFACE_RECORD


def _surface_root_digest(root: str | os.PathLike[str]) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()


def _surface_binding_digest(path: str | os.PathLike[str]) -> str:
    """Hash a canonical project/roster path for the private handoff record.

    The sidecar intentionally stores no absolute paths.  The digest is only
    used to prevent a live browser process bound to one project (or explicit
    roster) from being silently reused by another caller that happens to use
    the same conversation atlas root.
    """
    return hashlib.sha256(str(Path(path).expanduser().resolve()).encode("utf-8")).hexdigest()


def _pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a query-only operation on every Windows
        # runtime.  OpenProcess with query-only access avoids signalling the
        # current process while still recognizing an access-denied live PID.
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5
            code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - stale metadata is fail-closed
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def read_surface_record(root: str | os.PathLike[str], *,
                        cwd: str | os.PathLike[str] | None = None,
                        agents_dir: str | os.PathLike[str] | None = None
                        ) -> dict[str, object] | None:
    try:
        canonical = _root_path(root, create=False)
        path = _surface_path(canonical)
        if path.is_symlink() or path.stat().st_size > MAX_SURFACE_RECORD_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    pid, url, token = value.get("pid"), value.get("url"), value.get("token")
    if (value.get("schema_version") != 1
            or value.get("root_sha256") != _surface_root_digest(canonical)
            or not _pid_is_alive(pid) or not isinstance(url, str)
            or len(url) > 512 or not url.startswith("http://127.0.0.1:")
            or not isinstance(token, str) or not TOKEN_RE.fullmatch(token)):
        return None
    # Binding fields were added without changing the sidecar schema number so
    # older live surfaces remain readable by callers that do not request a
    # project/roster binding.  If either field is present, validate both and
    # fail closed on malformed or partially-written metadata.
    has_cwd = "cwd_sha256" in value
    has_agents = "agents_dir_sha256" in value
    if has_cwd != has_agents:
        return None
    if has_cwd:
        cwd_digest = value.get("cwd_sha256")
        agents_digest = value.get("agents_dir_sha256")
        if (not isinstance(cwd_digest, str) or not _SHA256_RE.fullmatch(cwd_digest)
                or (agents_digest is not None
                    and (not isinstance(agents_digest, str)
                         or not _SHA256_RE.fullmatch(agents_digest)))):
            return None
        try:
            if (cwd is not None
                    and cwd_digest != _surface_binding_digest(cwd)):
                return None
            if (agents_dir is not None
                    and agents_digest != _surface_binding_digest(agents_dir)):
                return None
        except (OSError, RuntimeError):
            return None
    elif cwd is not None or agents_dir is not None:
        # A legacy sidecar has no project/roster identity to compare.  Keep it
        # readable for unbound observers, but fail closed for a bound caller.
        return None
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not port or not (1 <= port <= 65535) or parsed.path != "/"
            or parsed.query or parsed.fragment != "token=" + token):
        return None
    record = {"pid": pid, "url": url, "token": token}
    if has_cwd:
        record["cwd_sha256"] = value["cwd_sha256"]
        record["agents_dir_sha256"] = value["agents_dir_sha256"]
    return record


def _write_surface_record(root: str, *, pid: int, url: str, token: str,
                          cwd_sha256: str | None = None,
                          agents_dir_sha256: str | None = None) -> None:
    target = _surface_path(_root_path(root, create=True))
    if target.exists() and target.is_symlink():
        raise ConversationUIError("surface record may not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".summon-conversation-ui-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            record = {"schema_version": 1,
                      "root_sha256": _surface_root_digest(target.parent),
                      "pid": pid, "url": url, "token": token,
                      "started_at": time.time()}
            if cwd_sha256 is not None or agents_dir_sha256 is not None:
                if (not isinstance(cwd_sha256, str) or not _SHA256_RE.fullmatch(cwd_sha256)
                        or (agents_dir_sha256 is not None
                            and (not isinstance(agents_dir_sha256, str)
                                 or not _SHA256_RE.fullmatch(agents_dir_sha256)))):
                    raise ConversationUIError("surface binding metadata is invalid")
                record["cwd_sha256"] = cwd_sha256
                record["agents_dir_sha256"] = agents_dir_sha256
            json.dump(record, fh,
                      ensure_ascii=True, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _page(*, nonce: str, timeout_ms: int = 600_000) -> bytes:
    return page_bytes(nonce=nonce, timeout_ms=timeout_ms)

class _ConversationHandler(BaseHTTPRequestHandler):
    server_version = "SummonConversationUI/1"

    def log_message(self, *_args):
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        semaphore = getattr(self.server, "client_semaphore", None)
        self._client_slot = bool(semaphore is None or semaphore.acquire(timeout=0.25))

    def handle(self):
        if not self._client_slot:
            self.close_connection = True
            return
        # Browsers routinely close an SSE connection while the handler is
        # blocked in ``readline`` (tab switch, reconnect, or page teardown).
        # Treat that as normal disconnect noise rather than emitting a server
        # traceback that looks like a product failure.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError,
                TimeoutError, OSError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        finally:
            if self._client_slot:
                semaphore = getattr(self.server, "client_semaphore", None)
                if semaphore is not None:
                    semaphore.release()

    @property
    def surface(self):
        return self.server.surface  # type: ignore[attr-defined]

    def _authorized(self, *, require_origin: bool = False) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host", "") != expected:
            return False
        if require_origin and self.headers.get("Origin") != f"http://{expected}":
            return False
        return secrets.compare_digest(self.headers.get("Authorization", ""),
                                     "Bearer " + self.surface.token)

    def _send_json(self, value: object, status: int = 200):
        data = _json_bytes(value)
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _send_error(self, exc: BaseException, status: int = 400) -> None:
        """Best-effort error response; a disconnected browser is not an error path."""
        try:
            self._send_json(_safe_error(exc), status)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    def _drain_rejected_body(self) -> None:
        """Consume a bounded rejected body so the client receives the error cleanly.

        Returning a response while a small request body is still unread can make
        Windows reset the connection instead of delivering the intended 401/400.  Do
        not drain unbounded or malformed bodies: close those connections after the
        bounded error response rather than turning rejection into a slowloris.
        """
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining:
                chunk = self.rfile.read(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            self.close_connection = True

    def _session(self) -> str:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) not in (4, 5) or parts[:3] != ["api", "v1", "rooms"]:
            raise ConversationUIError("unknown conversation route")
        session = _safe_id(parts[3], "session id")
        if len(parts) == 5 and parts[4] not in {"messages", "events", "stream", "turns", "cancel", "recover", "fork"}:
            raise ConversationUIError("unknown conversation route")
        return session

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            if self.headers.get("Host", "") != f"127.0.0.1:{self.server.server_port}":
                self._send_json(_safe_error(ConversationUIError("host refused")), 400); return
            nonce = secrets.token_urlsafe(18); data = _page(nonce=nonce, timeout_ms=self.surface.runtime.timeout_ms)
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        try:
            if not self._authorized():
                raise ConversationUIError("unauthorized")
            if parsed.path == "/api/v1/rooms":
                self._send_json({"status": "ok", "rooms": list_rooms(self.surface.root),
                                 "redaction": "public-redacted"}); return
            session = self._session()
            journal = ConversationJournal.open(self.surface.root, session)
            if parsed.path.endswith("/events"):
                query = parse_qs(parsed.query); after = query.get("after", ["0"])[0]
                try: cursor = int(after)
                except ValueError as exc: raise ConversationUIError("invalid cursor") from exc
                self._send_json({"status": "ok", "room": journal.room.as_dict(),
                                 "events": journal.events(after_cursor=cursor),
                                 "cursor": journal.room.cursor,
                                 "redaction": "public-redacted"}); return
            if parsed.path.endswith("/stream"):
                query = parse_qs(parsed.query); raw_after = query.get("after", ["0"])[0]
                try:
                    cursor = int(raw_after)
                except ValueError as exc:
                    raise ConversationUIError("invalid cursor") from exc
                if cursor < 0 or cursor > MAX_EVENTS:
                    raise ConversationUIError("invalid cursor")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                deadline = time.monotonic() + SSE_MAX_SECONDS
                while (time.monotonic() < deadline
                       and not self.surface._closed.is_set()):
                    events = journal.events(after_cursor=cursor)
                    if events:
                        for event in events:
                            frame = (f"id: {event['cursor']}\n"
                                     "event: conversation\n"
                                     f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n")
                            self.wfile.write(frame.encode("utf-8"))
                            self.wfile.flush()
                            cursor = int(event["cursor"])
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    time.sleep(SSE_POLL_SECONDS)
                return
            self._send_json({"status": "ok", **journal.as_dict(), "redaction": "public-redacted"})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_error(exc, 401 if str(exc) == "unauthorized" else 400)

    def do_POST(self):
        try:
            if not self._authorized(require_origin=True):
                self._drain_rejected_body()
                raise ConversationUIError("unauthorized")
            session = self._session()
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise ConversationUIError("request body is too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ConversationUIError("request body must be an object")
            route = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            if route in {"messages", "turns", "fork"} and (
                    not isinstance(body.get("message"), str) or not body.get("message", "").strip()):
                raise ConversationUIError("message must be text")
            event_id = body.get("message_id")
            if event_id is not None:
                _safe_id(event_id, "message id")
            if route == "messages":
                journal = ConversationJournal.open(self.surface.root, session)
                event = journal.append_human_message(body["message"], message_id=event_id)
                self._send_json({"status": "posted", "event": event,
                                 "cursor": journal.room.cursor,
                                 "redaction": "public-redacted"})
                return
            if route == "turns":
                if body.get("reviewed") is not True:
                    raise ConversationUIError(
                        "bounded turn requires explicit review confirmation")
                participant = _safe_id(body.get("participant"), "participant")
                result = self.surface.runtime.start_turn(
                    session, participant, body["message"], wait=False)
                self._send_json({"status": "started", **result,
                                 "redaction": "public-redacted"}, status=202)
                return
            if route == "cancel":
                participant = _safe_id(body.get("participant"), "participant")
                result = self.surface.runtime.cancel_turn(session, participant)
                self._send_json({"status": "cancelling", **result,
                                 "redaction": "public-redacted"})
                return
            if route == "recover":
                participant = _safe_id(body.get("participant"), "participant")
                if body.get("confirm") is not True:
                    raise ConversationUIError("recovery requires explicit confirmation")
                result = self.surface.runtime.recover_turn(session, participant, confirm=True)
                self._send_json({"status": "recovered", **result,
                                 "redaction": "public-redacted"})
                return
            if route == "fork":
                participant = _safe_id(body.get("participant"), "participant")
                reason = body.get("reason", "operator recovery fork")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
                    raise ConversationUIError("fork reason is invalid")
                result = self.surface.runtime.fork_turn(
                    session, participant, body["message"], reason=reason.strip())
                self._send_json({"status": "forked", **result,
                                 "redaction": "public-redacted"})
                return
            raise ConversationUIError("only context messages or agent turns may be posted")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_error(exc, 401 if str(exc) == "unauthorized" else 400)


class ConversationSurface:
    """One authenticated loopback room atlas for a private conversation root."""

    def __init__(self, root: str, *, token: str | None = None,
                 cwd: str | os.PathLike[str] | None = None,
                 agents_dir: str | os.PathLike[str] | None = None,
                 timeout_ms: int = 600_000):
        if not isinstance(root, str) or not root:
            raise ConversationUIError("invalid conversation root")
        try:
            self.root = str(_root_path(root, create=True))
        except ConversationError as exc:
            raise ConversationUIError("invalid conversation root") from exc
        self.token = token or secrets.token_urlsafe(32)
        if not TOKEN_RE.fullmatch(self.token):
            raise ConversationUIError("surface token is invalid")
        try:
            self.runtime = ConversationRuntime(
                self.root, cwd=cwd or os.getcwd(), agents_dir=agents_dir,
                timeout_ms=timeout_ms, strict_agents_dir=bool(agents_dir),
            )
        except ConversationRuntimeError as exc:
            raise ConversationUIError("conversation runtime is unavailable") from exc
        # Keep the exact caller binding separate from the runtime's resolved
        # default roster.  ``agents_dir=None`` means the caller accepted the
        # loader-selected roster; an explicit path must be fenced on reuse.
        self._surface_cwd_sha256 = _surface_binding_digest(self.runtime.cwd)
        self._surface_agents_dir_sha256 = (
            _surface_binding_digest(agents_dir) if agents_dir is not None else None
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ConversationHandler)
        self._server.daemon_threads = True
        self._server.request_queue_size = MAX_ACTIVE_CLIENTS
        self._server.client_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
        self._server.surface = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.address[1]}/#token={self.token}"

    def start(self, *, _launch_lock_held: bool = False) -> str:
        if self._thread is None:
            self._closed.clear()
            if _launch_lock_held:
                guard = nullcontext()
            else:
                # Import lazily: the browser handoff imports this module for
                # its record/auth constants, while direct callers need the
                # exact same cross-process launch fence.
                from _conversation_browser import _launch_guard
                guard = _launch_guard(self.root, 5.0)
            with guard:
                existing = read_surface_record(self.root)
                if existing is not None:
                    raise ConversationUIError("a conversation surface is already running")
                self._thread = threading.Thread(target=self._server.serve_forever,
                                                 name="summon-conversation-ui", daemon=True)
                self._thread.start()
                try:
                    _write_surface_record(
                        self.root, pid=os.getpid(), url=self.url, token=self.token,
                        cwd_sha256=self._surface_cwd_sha256,
                        agents_dir_sha256=self._surface_agents_dir_sha256,
                    )
                except Exception:
                    # Do not leak the serving thread when the durable handoff
                    # record cannot be published.
                    self._server.shutdown()
                    self._server.server_close()
                    self._thread.join(timeout=2)
                    self._thread = None
                    raise
        return self.url

    def close(self) -> None:
        self._closed.set()
        # The surface owns the runtime lifecycle.  Stop local provider turns
        # before tearing down HTTP so closing a browser tab cannot leave a
        # paid child process running out of band.
        runtime_error = None
        try:
            close_runtime = getattr(self.runtime, "close", None)
            if callable(close_runtime):
                close_runtime(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - preserve shutdown diagnostics
            runtime_error = exc
        try:
            if self._thread is not None:
                self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=2)
        except OSError:
            pass
        try:
            record = json.loads(_surface_path(self.root).read_text(encoding="utf-8"))
            if (isinstance(record, dict) and record.get("pid") == os.getpid()
                    and secrets.compare_digest(str(record.get("token", "")), self.token)):
                _surface_path(self.root).unlink()
        except (OSError, ValueError):
            pass
        if runtime_error is not None:
            raise ConversationUIError("conversation runtime did not close cleanly") from runtime_error


def serve_foreground(root: str, *, cwd: str | None = None,
                     agents_dir: str | None = None,
                     timeout_ms: int = 600_000,
                     launch_lock_held: bool = False) -> int:
    surface = ConversationSurface(root, cwd=cwd, agents_dir=agents_dir,
                                  timeout_ms=timeout_ms)
    url = surface.start(_launch_lock_held=launch_lock_held)
    print(json.dumps({"mode": "conversation-ui", "status": "ready", "url": url},
                     ensure_ascii=True), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        surface.close()
    return 0


__all__ = ["ConversationSurface", "ConversationUIError", "read_surface_record", "serve_foreground"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="serve a local Summon conversation surface")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--cwd")
    parser.add_argument("--agents-dir")
    parser.add_argument("--timeout", type=int, default=600_000)
    parser.add_argument("--launch-lock-held", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("root")
    args = parser.parse_args()
    raise SystemExit(serve_foreground(args.root, cwd=args.cwd, agents_dir=args.agents_dir,
                                      timeout_ms=args.timeout,
                                      launch_lock_held=args.launch_lock_held) if args.serve else 2)
