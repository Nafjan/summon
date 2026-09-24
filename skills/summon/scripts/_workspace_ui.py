"""Explicitly started loopback workspace shell, read-only by default.

Optional context commands require independently installed host authority. No
provider calls, worker start or implicit supervisor. A local
operator obtains ``bootstrap_code`` out of band after start; never log it or put
it in a URL. Codes and sessions live only in this object and die on stop/restart.
Browser closure does not stop this explicitly owned server or change tasks.
"""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from _swarm_coordinator import SwarmCoordinator
from _workspace_page import page_bytes
from _workspace_view import ViewScope, WorkspaceViewError, project_workspace
from _workspace_details import WorkspaceDetailAdapter, WorkspaceDetailError
from _workspace_canonical import CanonicalDetailError
from _workspace_commands import (ACTIONS, OperatorCommandAdapter, OperatorDispositionAdapter,
                                 OperatorLinkedReplacementAdapter, OperatorMessageAdapter,
                                 OperatorDraftAdapter,
                                 WorkspaceCommandError)

MAX_BODY = 4096
MAX_TARGET = 2048
MAX_HEADERS = 8192
MAX_CLIENTS = 16
REQUEST_TIMEOUT = 3.0
REQUEST_LIFETIME = 5.0
BOOTSTRAP_LIFETIME = 120.0
SESSION_LIFETIME = 600.0
MAX_BOOTSTRAP_FAILURES = 5


class WorkspaceUIError(ValueError):
    """Non-sensitive public reason; never copy source exceptions or identifiers."""


def collect_snapshot(coordinator, *, scope, cursor=None, selected_task=None,
                     expected_snapshot=None, limit=200, anchor=None, operator_commands=None,
                     operator_messages=None, operator_drafts=None, operator_dispositions=None,
                     operator_linked_replacements=None, workspace_details=None):
    """Two bounded read attempts; no owner acquisition, repair or journal write.

    Status must include every coordinator task. Bracket its reads with identical
    complete journal bytes; a changed/torn prefix is refused, never displayed as
    coherent state. A later mutation remains possible: every future command must
    recheck its own authorization and current state. This is a read-only view.
    """
    if not isinstance(coordinator, SwarmCoordinator):
        raise WorkspaceUIError("coordinator_required")
    for _ in range(2):
        try:
            state, torn, before = coordinator._load_with_snapshot()
            status = coordinator.status()
            records, later_torn, after = coordinator._read_records_snapshot()
            if torn or later_torn or status.get("torn_tail") or before != after:
                continue
            if {task["task_id"] for task in status["tasks"]} != set(state["tasks"]):
                raise WorkspaceUIError("incomplete_snapshot")
            workspace = state.get("workspace")
            if workspace is None:
                raise WorkspaceUIError("workspace_unprepared")
            events = []
            event_times = {}
            for record in records:
                event = record.get("workspace_event")
                if event is None:
                    continue
                events.append(event)
                # Keep the outer journal timestamp separate from the signed
                # inner event; it is the only timing authority for the view.
                operation_key = event.get("operation_key") if isinstance(event, dict) else None
                if isinstance(operation_key, str) and "ts" in record:
                    event_times[operation_key] = record["ts"]
            capabilities = None
            if operator_commands is not None:
                try:
                    capabilities = operator_commands.describe(workspace, scope)
                except WorkspaceCommandError:
                    capabilities = {"available": False, "actions_by_delivery": {},
                                    "reason": "command_scope_required"}
            message_capabilities = None
            if operator_messages is not None:
                try:
                    message_capabilities = operator_messages.describe(workspace, scope)
                except WorkspaceCommandError:
                    message_capabilities = {"available": False, "targets": [],
                                            "reason": "message_scope_required"}
            draft_capabilities = None
            if operator_drafts is not None:
                try:
                    draft_capabilities = operator_drafts.describe(workspace, scope)
                except WorkspaceCommandError:
                    draft_capabilities = {"available": False, "operator_scope": None, "key_epoch": None,
                                          "max_text_bytes": 2048, "max_record_bytes": 4096,
                                          "reason": "message_retention_required"}
            disposition_capabilities = None
            if operator_dispositions is not None:
                try:
                    disposition_capabilities = operator_dispositions.describe(workspace, scope)
                except WorkspaceCommandError:
                    disposition_capabilities = {"available": False, "targets": [],
                                                "reason": "command_scope_required"}
            linked_capabilities = None
            if operator_linked_replacements is not None:
                try:
                    linked_capabilities = operator_linked_replacements.describe(workspace, scope)
                except WorkspaceCommandError:
                    linked_capabilities = {"available": False, "targets": [],
                                           "reason": "linked_scope_required"}
            detail_capabilities = None
            if workspace_details is not None:
                try:
                    detail_capabilities = workspace_details.describe(workspace, scope)
                except WorkspaceDetailError:
                    detail_capabilities = {"available": False, "targets": [],
                                           "reason": "detail_scope_required"}
            return project_workspace(workspace, status, scope=scope, events=events,
                cursor=cursor, selected_task=selected_task, expected_snapshot=expected_snapshot, limit=limit,
                anchor=anchor, operator_commands=capabilities, operator_messages=message_capabilities,
                operator_drafts=draft_capabilities, operator_dispositions=disposition_capabilities,
                operator_linked_replacements=linked_capabilities,
                event_times=event_times, workspace_details=detail_capabilities)
        except WorkspaceViewError:
            raise
        except WorkspaceUIError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise WorkspaceUIError("snapshot_unavailable") from None
    raise WorkspaceUIError("snapshot_changed")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CLIENTS
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        # Windows SO_REUSEADDR can allow two live listeners on the same origin.
        # Explicit restart ports must fail when occupied, never share or fall back.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, surface):
        self.surface = surface
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(("127.0.0.1", surface._port), _Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                # On Windows, closing with unread request bytes can reset the
                # socket before a tiny 503 is observable. Drain only bounded
                # headers on this accept thread; never create an extra worker
                # or wait indefinitely for an overloaded/idle client.
                header = b""
                deadline = time.monotonic() + 0.2
                while b"\r\n\r\n" not in header and len(header) < MAX_HEADERS:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    request.settimeout(remaining)
                    part = request.recv(min(1024, MAX_HEADERS - len(header)))
                    if not part:
                        break
                    header += part
                request.settimeout(0.2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, *_):
        # Standard HTTP logging may print private request paths or exceptions.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "SummonWorkspace"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self._body_bytes_read = 0
        self.connection.settimeout(REQUEST_TIMEOUT)
        # Socket idle timeouts alone allow indefinite slow-drip headers. One
        # bounded timer per admitted client closes its transport after a total
        # lifetime; it never cancels coordinator work or changes task state.
        self._deadline = threading.Timer(REQUEST_LIFETIME, self._expire_transport)
        self._deadline.daemon = True
        self._deadline.start()

    def _expire_transport(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            self.connection.close()

    def finish(self):
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self._deadline.cancel()

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        self._send(code, {"error": "request_refused"})

    def _send(self, code, value, *, page=False, nonce=None):
        if code >= 400:
            self._discard_rejected_body()
        raw = value if page else json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if page else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        policy = ("default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; "
                  "connect-src 'self'; img-src 'none'; "
                  + (f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'" if page else "script-src 'none'; style-src 'none'"))
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if self.command != "HEAD":
                self.wfile.write(raw)
        except (OSError, TimeoutError):
            pass

    def _discard_rejected_body(self):
        # Windows can reset a close with unread inbound body bytes before the
        # peer receives the refusal. Discard only a bounded declared body; this
        # neither parses nor authorizes it, and cannot prolong a slot indefinitely.
        headers = getattr(self, "headers", None)
        lengths = headers.get_all("Content-Length", []) if headers is not None else []
        if (len(lengths) != 1 or len(lengths[0]) > 8
                or not lengths[0].isascii() or not lengths[0].isdigit()):
            return
        remaining = min(MAX_BODY + 1, max(0, int(lengths[0]) - self._body_bytes_read))
        deadline = time.monotonic() + 0.15
        try:
            while remaining and time.monotonic() < deadline:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                raw = self.rfile.read1(min(1024, remaining))
                if not raw:
                    break
                remaining -= len(raw)
                self._body_bytes_read += len(raw)
        except (OSError, TimeoutError):
            pass
        finally:
            try:
                self.connection.settimeout(REQUEST_TIMEOUT)
            except OSError:
                pass

    def _boundary(self, *, origin_required=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get_all("Host", []) != [expected]:
            raise WorkspaceUIError("host_refused")
        origins = self.headers.get_all("Origin", [])
        if (origin_required and origins != ["http://" + expected]
                or origins and origins != ["http://" + expected]):
            raise WorkspaceUIError("origin_refused")
        if sum(len(key) + len(value) + 4 for key, value in self.headers.items()) > MAX_HEADERS:
            raise WorkspaceUIError("headers_too_large")
        if len(self.path) > MAX_TARGET or self.headers.get_all("Transfer-Encoding"):
            raise WorkspaceUIError("request_refused")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise WorkspaceUIError("request_refused")
        return parsed

    def _authenticate(self):
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise WorkspaceUIError("authentication_required")
        self.server.surface._authenticate(values[0][7:])
        return values[0][7:]

    def _body(self):
        if self.headers.get_all("Content-Type", []) != ["application/json"]:
            raise WorkspaceUIError("json_required")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            raise WorkspaceUIError("body_length_required")
        length = int(values[0])
        if not 1 <= length <= MAX_BODY:
            raise WorkspaceUIError("body_bound_exceeded")
        raw = self.rfile.read(length)
        self._body_bytes_read += len(raw)
        if len(raw) != length:
            raise WorkspaceUIError("incomplete_body")
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise WorkspaceUIError("invalid_json")
                result[key] = value
            return result
        try:
            value = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, UnicodeError, RecursionError):
            raise WorkspaceUIError("invalid_json") from None
        if type(value) is not dict:
            raise WorkspaceUIError("invalid_json")
        return value

    def _refused(self, error):
        reason = str(error)
        if reason in {"authentication_required", "session_expired", "bootstrap_refused",
                      "command_refresh_authentication_required"}:
            code = 401
        elif reason in {"scope_refused", "host_refused", "origin_refused", "command_scope_required", "message_scope_required",
                        "message_retention_required", "linked_scope_required", "detail_scope_required",
                        "detail_body_scope_required", "detail_runtime_unavailable",
                        "canonical_scope_refused", "canonical_body_scope_invalid"}:
            code = 403
        elif reason in {"stale_snapshot", "stale_or_invalid_cursor", "snapshot_changed", "history_snapshot_mismatch",
                       "anchor_scope_or_history_refused", "command_request_conflict", "command_state_refused",
                       "message_request_conflict", "command_refresh_generation_stale",
                       "command_refresh_history_full", "command_refresh_request_conflict",
                       "command_policy_changed", "linked_request_conflict", "linked_target_refused",
                       "detail_scope_refused", "detail_target_refused", "detail_type_refused", "detail_task_refused",
                       "detail_scope_revoked", "detail_binding_stale",
                       "canonical_binding_mismatch", "canonical_binding_stale",
                       "canonical_target_refused"}:
            code = 409
        elif reason in {"snapshot_unavailable", "incomplete_snapshot", "workspace_unprepared", "reconciliation_required",
                       "command_outcome_uncertain", "command_snapshot_unavailable", "operator_runtime_unavailable",
                       "message_outcome_uncertain", "message_snapshot_unavailable", "message_runtime_unavailable",
                       "message_key_unavailable", "command_refresh_unavailable",
                       "command_refresh_request_invalid", "workspace_snapshot_unavailable",
                       "linked_outcome_uncertain", "detail_snapshot_unavailable", "detail_unavailable",
                       "detail_body_unavailable", "canonical_snapshot_unavailable",
                       "canonical_detail_unavailable", "canonical_binding_unavailable",
                       "canonical_scope_empty"}:
            code = 503
        else:
            code = 400
        self._send(code, {"error": reason if isinstance(error, (WorkspaceUIError, WorkspaceViewError,
                                                                   WorkspaceCommandError,
                                                                   WorkspaceDetailError,
                                                                   CanonicalDetailError))
                          else "request_refused"})

    def do_GET(self):
        try:
            parsed = self._boundary()
            if self.headers.get_all("Content-Length", []) not in ([], ["0"]):
                raise WorkspaceUIError("request_refused")
            if parsed.path == "/" and not parsed.query:
                nonce = secrets.token_urlsafe(18)
                self._send(200, page_bytes(nonce=nonce), page=True, nonce=nonce)
                return
            if parsed.path == "/api/identity" and not parsed.query:
                self._authenticate()
                self._send(200, self.server.surface.identity())
                return
            if parsed.path == "/api/commands/refresh/status":
                values = self.headers.get_all("X-Summon-Command-Refresh", [])
                if len(values) != 1:
                    raise WorkspaceUIError("command_refresh_authentication_required")
                query = (parse_qs(parsed.query, keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=1)
                         if parsed.query else {})
                if set(query) != {"request_id"} or len(query["request_id"]) != 1:
                    raise WorkspaceUIError("command_refresh_request_invalid")
                request_id = query["request_id"][0]
                if (not request_id or len(request_id) > 128
                        or not request_id.isascii()
                        or any(char not in "0123456789abcdef" for char in request_id)):
                    raise WorkspaceUIError("command_refresh_request_invalid")
                self._send(200, self.server.surface.refresh_command_policy_status(
                    values[0], request_id))
                return
            if parsed.path == "/api/details":
                self._authenticate()
                query = (parse_qs(parsed.query, keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=3)
                         if parsed.query else {})
                if (set(query) - {"target", "source", "task"}
                        or any(len(values) != 1 or not values[0] for values in query.values())):
                    raise WorkspaceUIError("query_refused")
                result = self.server.surface.details(
                    target=query.get("target", [None])[0],
                    source_kind=query.get("source", [None])[0],
                    task=query.get("task", [None])[0])
                self._authenticate()
                self._send(200, result)
                return
            if parsed.path not in {"/api/view", "/api/export"}:
                self._send(404, {"error": "not_found"})
                return
            self._authenticate()
            query = (parse_qs(parsed.query, keep_blank_values=True,
                              strict_parsing=True, max_num_fields=5)
                     if parsed.query else {})
            if (set(query) - {"cursor", "task", "snapshot", "limit", "anchor"}
                    or any(len(values) != 1 or not values[0] for values in query.values())):
                raise WorkspaceUIError("query_refused")
            limit = query.get("limit", ["200"])[0]
            if not limit.isascii() or not limit.isdigit():
                raise WorkspaceUIError("query_refused")
            result = self.server.surface.snapshot(public=parsed.path == "/api/export",
                cursor=query.get("cursor", [None])[0], selected_task=query.get("task", [None])[0],
                expected_snapshot=query.get("snapshot", [None])[0], limit=int(limit),
                anchor=query.get("anchor", [None])[0])
            self._authenticate()  # Session/code renewal may revoke an in-flight read.
            self._send(200, result)
        except (WorkspaceUIError, WorkspaceViewError, WorkspaceDetailError) as error:
            self._refused(error)
        except (ValueError, OSError, TimeoutError):
            self._send(400, {"error": "request_refused"})

    def do_POST(self):
        try:
            parsed = self._boundary(origin_required=True)
            if parsed.path == "/api/commands/provision-message" and not parsed.query:
                values = self.headers.get_all("X-Summon-Command-Refresh", [])
                if len(values) != 1:
                    raise WorkspaceUIError("command_refresh_authentication_required")
                payload = self._body()
                actions = payload.get("actions")
                if (set(payload) != {"request_id", "operation_key", "actions", "workspace_id", "run_id"}
                        or type(payload.get("request_id")) is not str
                        or not payload["request_id"].isascii()
                        or not 16 <= len(payload["request_id"]) <= 128
                        or any(char not in "0123456789abcdef" for char in payload["request_id"])
                        or type(payload.get("operation_key")) is not str
                        or len(payload["operation_key"]) != 32
                        or any(char not in "0123456789abcdef" for char in payload["operation_key"])
                        or type(actions) is not list
                        or not 1 <= len(actions) <= len(ACTIONS)
                        or any(type(action) is not str or action not in ACTIONS for action in actions)
                        or len(set(actions)) != len(actions)
                        or type(payload.get("workspace_id")) is not str
                        or type(payload.get("run_id")) is not str
                        or payload["workspace_id"] != self.server.surface.workspace_id
                        or payload["run_id"] != self.server.surface.coordinator.run_id):
                    raise WorkspaceUIError("command_refresh_request_invalid")
                self._send(200, self.server.surface.provision_command_policy_for_operator_message(
                    values[0], payload["operation_key"], tuple(actions), payload["request_id"]))
                return
            if parsed.path == "/api/commands/refresh" and not parsed.query:
                values = self.headers.get_all("X-Summon-Command-Refresh", [])
                if len(values) != 1:
                    raise WorkspaceUIError("command_refresh_authentication_required")
                payload = self._body()
                if (set(payload) != {"request_id", "workspace_id", "run_id"}
                        or type(payload.get("request_id")) is not str
                        or not payload["request_id"].isascii()
                        or not 16 <= len(payload["request_id"]) <= 128
                        or any(char not in "0123456789abcdef" for char in payload["request_id"])
                        or type(payload.get("workspace_id")) is not str
                        or type(payload.get("run_id")) is not str
                        or payload["workspace_id"] != self.server.surface.workspace_id
                        or payload["run_id"] != self.server.surface.coordinator.run_id):
                    raise WorkspaceUIError("command_refresh_request_invalid")
                self._send(200, self.server.surface.refresh_command_policy(
                    values[0], payload["request_id"]))
                return
            if parsed.path == "/api/details/body" and not parsed.query:
                token = self._authenticate()
                payload = self._body()
                if (set(payload) - {"target", "source", "task"}
                        or type(payload.get("target")) is not str or not payload["target"]
                        or ("source" in payload and type(payload["source"]) is not str)
                        or ("task" in payload and type(payload["task"]) is not str)):
                    raise WorkspaceUIError("detail_request_invalid")
                self._authenticate()
                result = self.server.surface.detail_body(
                    target=payload["target"], source_kind=payload.get("source"),
                    task=payload.get("task"),
                    authorization_constraint=lambda: self.server.surface._session_allows(token))
                self._authenticate()
                self._send(200, result)
                return
            if parsed.path in {"/api/commands", "/api/commands/lookup", "/api/messages", "/api/messages/lookup",
                               "/api/messages/draft-key", "/api/dispositions", "/api/dispositions/lookup",
                               "/api/linked-replacements", "/api/linked-replacements/lookup"} and not parsed.query:
                token = self._authenticate()
                is_draft = parsed.path == "/api/messages/draft-key"
                is_message = parsed.path.startswith("/api/messages") and not is_draft
                is_disposition = parsed.path.startswith("/api/dispositions")
                is_linked = parsed.path.startswith("/api/linked-replacements")
                adapter = (self.server.surface.operator_drafts if is_draft else
                           self.server.surface.operator_messages if is_message else
                           self.server.surface.operator_dispositions if is_disposition else
                           self.server.surface.operator_linked_replacements if is_linked else
                           self.server.surface.operator_commands)
                if adapter is None:
                    self._send(405, {"error": "read_only_surface"})
                    return
                payload = self._body()
                self._authenticate()  # A slow body may outlive or revoke this session.
                if is_draft:
                    result = self.server.surface.perform_draft_key(payload,
                        authorization_constraint=lambda: self.server.surface._session_allows(token))
                elif is_message:
                    result = self.server.surface.perform_message(payload,
                        lookup=parsed.path.endswith("/lookup"), authorization_constraint=lambda:
                            self.server.surface._session_allows(token))
                elif is_disposition:
                    result = self.server.surface.perform_disposition(payload,
                        lookup=parsed.path.endswith("/lookup"), authorization_constraint=lambda:
                            self.server.surface._session_allows(token))
                elif is_linked:
                    result = self.server.surface.perform_linked_replacement(payload,
                        lookup=parsed.path.endswith("/lookup"), authorization_constraint=lambda:
                            self.server.surface._session_allows(token))
                else:
                    result = self.server.surface.perform_command(payload,
                        lookup=parsed.path.endswith("/lookup"), authorization_constraint=lambda:
                            self.server.surface._session_allows(token))
                self._authenticate()
                self._send(200, result)
                return
            if parsed.path != "/api/bootstrap" or parsed.query:
                self._send(405, {"error": "read_only_surface"})
                return
            payload = self._body()
            if set(payload) != {"code"}:
                raise WorkspaceUIError("bootstrap_refused")
            token = self.server.surface._exchange(payload["code"])
            self._send(200, {"bearer": token, "expires_in_seconds": int(SESSION_LIFETIME),
                "scope": "workspace_operator" if (self.server.surface.operator_commands is not None
                                                    or self.server.surface.operator_messages is not None
                                                    or self.server.surface.operator_drafts is not None
                                                    or self.server.surface.operator_dispositions is not None
                                                    or self.server.surface.operator_linked_replacements is not None
                                                    or self.server.surface.workspace_details is not None) else "workspace_read"})
        except (WorkspaceUIError, WorkspaceViewError, WorkspaceCommandError, WorkspaceDetailError) as error:
            self._refused(error)
        except (ValueError, OSError, TimeoutError):
            self._send(400, {"error": "request_refused"})

    def do_OPTIONS(self):
        self._send(405, {"error": "cross_origin_not_supported"})

    def _unsupported(self):
        self._send(405, {"error": "read_only_surface"})

    do_PUT = do_PATCH = do_DELETE = _unsupported


class WorkspaceSurface:
    """One local operator's explicitly started workspace server.

    The constructor takes an actual coordinator, not an arbitrary success
    callback. There is no HTTP API for starting/stopping a daemon or granting
    authority. ``bootstrap_code`` is private local handoff material only.

    Command-enabled restart requires a host-held private presentation_key and
    independently reinstalled runtime authority. The key only stabilizes opaque
    targets; it grants nothing. Bootstrap and bearer secrets always rotate. This
    class never persists a key. Losing it leaves pending browser requests
    unresolved; they must not be retargeted or retried under a fresh key.
    """
    def __init__(self, coordinator, workspace_id, *, operator_commands=None, operator_messages=None,
                 operator_drafts=None, operator_dispositions=None, operator_linked_replacements=None,
                 workspace_details=None,
                 command_policy_refresh=None,
                 command_policy_refresh_status=None,
                 command_policy_provision=None,
                 presentation_key=None, port=0):
        if not isinstance(coordinator, SwarmCoordinator):
            raise WorkspaceUIError("coordinator_required")
        from _workspace_protocol import _id
        _id(workspace_id)
        if type(port) is not int or not 0 <= port <= 65535:
            raise WorkspaceUIError("invalid_loopback_port")
        if operator_commands is not None and (type(operator_commands) is not OperatorCommandAdapter
                or operator_commands.coordinator is not coordinator):
            raise WorkspaceUIError("operator_runtime_unavailable")
        if operator_messages is not None and (type(operator_messages) is not OperatorMessageAdapter
                or operator_messages.coordinator is not coordinator):
            raise WorkspaceUIError("message_runtime_unavailable")
        from _workspace_commands import OperatorDraftAdapter
        if operator_drafts is not None and (type(operator_drafts) is not OperatorDraftAdapter
                or operator_drafts.coordinator is not coordinator):
            raise WorkspaceUIError("message_retention_required")
        if operator_dispositions is not None and (type(operator_dispositions) is not OperatorDispositionAdapter
                or operator_dispositions.coordinator is not coordinator):
            raise WorkspaceUIError("operator_runtime_unavailable")
        if operator_linked_replacements is not None and (
                type(operator_linked_replacements) is not OperatorLinkedReplacementAdapter
                or operator_linked_replacements.coordinator is not coordinator):
            raise WorkspaceUIError("operator_runtime_unavailable")
        if workspace_details is not None and (type(workspace_details) is not WorkspaceDetailAdapter
                or workspace_details.coordinator is not coordinator):
            raise WorkspaceUIError("detail_runtime_unavailable")
        if command_policy_refresh is not None and not callable(command_policy_refresh):
            raise WorkspaceUIError("command_refresh_unavailable")
        if command_policy_refresh_status is not None and not callable(command_policy_refresh_status):
            raise WorkspaceUIError("command_refresh_unavailable")
        if command_policy_provision is not None and not callable(command_policy_provision):
            raise WorkspaceUIError("command_refresh_unavailable")
        if (presentation_key is not None and (type(presentation_key) is not bytes or not 32 <= len(presentation_key) <= 64)
                or (operator_commands is not None or operator_messages is not None
                or operator_drafts is not None or operator_dispositions is not None
                or operator_linked_replacements is not None or workspace_details is not None)
                and presentation_key is None):
            raise WorkspaceUIError("private_presentation_key_required")
        self.coordinator = coordinator
        self.workspace_id = workspace_id
        self.operator_commands = operator_commands
        self.operator_messages = operator_messages
        self.operator_drafts = operator_drafts
        self.operator_dispositions = operator_dispositions
        self.operator_linked_replacements = operator_linked_replacements
        self.workspace_details = workspace_details
        self._command_policy_refresh = command_policy_refresh
        self._command_policy_refresh_status = command_policy_refresh_status
        self._command_policy_provision = command_policy_provision
        self._configured_key = presentation_key
        self._port = port
        self._lock = threading.Lock()
        self._server = self._thread = None
        self._key = b""
        self.bootstrap_code = None
        self._bootstrap_deadline = 0.0
        self._bootstrap_failures = 0
        self._session_digest = None
        self._session_deadline = 0.0

    @property
    def url(self):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        return f"http://127.0.0.1:{self._server.server_port}/"

    def start(self):
        if self._server is not None:
            raise WorkspaceUIError("server_already_started")
        self._key = self._configured_key or secrets.token_bytes(32)
        try:
            self._server = _Server(self)
        except OSError:
            self._key = b""
            raise WorkspaceUIError("loopback_port_unavailable") from None
        self.issue_bootstrap()
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="summon-workspace-read-ui", daemon=True)
        self._thread.start()
        return self.url

    def issue_bootstrap(self):
        """Explicit local renewal; invalidate the previous read session/code."""
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        with self._lock:
            self.bootstrap_code = secrets.token_urlsafe(24)
            self._bootstrap_deadline = time.monotonic() + BOOTSTRAP_LIFETIME
            self._bootstrap_failures = 0
            self._session_digest = None
            self._session_deadline = 0.0
            return self.bootstrap_code

    def _exchange(self, code):
        with self._lock:
            if (type(code) is not str or not code.isascii() or not 1 <= len(code) <= 128 or self.bootstrap_code is None
                    or time.monotonic() >= self._bootstrap_deadline
                    or self._bootstrap_failures >= MAX_BOOTSTRAP_FAILURES
                    or not hmac.compare_digest(code, self.bootstrap_code)):
                self._bootstrap_failures += 1
                raise WorkspaceUIError("bootstrap_refused")
            self.bootstrap_code = None
            token = secrets.token_urlsafe(32)
            self._session_digest = hashlib.sha256(token.encode("ascii")).digest()
            self._session_deadline = time.monotonic() + SESSION_LIFETIME
            return token

    def _authenticate(self, token):
        if type(token) is not str or not 1 <= len(token) <= 128 or not token.isascii():
            raise WorkspaceUIError("authentication_required")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        with self._lock:
            if self._session_digest is None or not hmac.compare_digest(digest, self._session_digest):
                raise WorkspaceUIError("authentication_required")
            if time.monotonic() >= self._session_deadline:
                self._session_digest = None
                raise WorkspaceUIError("session_expired")

    def _session_allows(self, token):
        """Conjunctive per-request condition; never installs host authority.

        Runtime checks it at its actual owner/admission boundary. The short
        session lock is never held over journal/content I/O. Revocation after
        admitted append may hide a successful response, requiring reconciliation.
        """
        try:
            self._authenticate(token)
            return True
        except WorkspaceUIError:
            return False

    def snapshot(self, *, public=False, **options):
        # Host-only method. HTTP reaches this only after scoped authentication.
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key,
                          audience="public" if public else "operator", allow_text=not public)
        return collect_snapshot(self.coordinator, scope=scope, operator_commands=self.operator_commands,
                                operator_messages=self.operator_messages, operator_drafts=self.operator_drafts,
                                operator_dispositions=self.operator_dispositions,
                                operator_linked_replacements=self.operator_linked_replacements,
                                workspace_details=self.workspace_details, **options)

    def details(self, *, target=None, source_kind=None, task=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.workspace_details is None:
            raise WorkspaceDetailError("detail_scope_required")
        state, torn = self.coordinator._load()
        if torn or not isinstance(state.get("workspace"), dict):
            raise WorkspaceDetailError("detail_snapshot_unavailable")
        return self.workspace_details.list_or_detail(state["workspace"], target=target,
                                                     source_kind=source_kind, task=task)

    def detail_body(self, *, target, source_kind=None, task=None, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.workspace_details is None:
            raise WorkspaceDetailError("detail_body_scope_required")
        if authorization_constraint is not None and not authorization_constraint():
            raise WorkspaceUIError("session_expired")
        state, torn = self.coordinator._load()
        if torn or not isinstance(state.get("workspace"), dict):
            raise WorkspaceDetailError("detail_snapshot_unavailable")
        result = self.workspace_details.body(state["workspace"], target=target,
                                             source_kind=source_kind, task=task)
        if authorization_constraint is not None and not authorization_constraint():
            raise WorkspaceUIError("session_expired")
        return result

    def identity(self):
        """Return only the authenticated endpoint's opaque workspace identity."""
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        return {"schema": "summon.workspace.identity/v1",
                "workspace_id": self.workspace_id,
                "run_id": self.coordinator.run_id}

    def refresh_command_policy(self, token, request_id):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self._command_policy_refresh is None:
            raise WorkspaceUIError("command_refresh_unavailable")
        try:
            result = self._command_policy_refresh(token, request_id=request_id)
        except Exception as error:
            # Keep the UI module independent of entry implementation details;
            # the host callback exposes only its safe typed reason.
            reason = getattr(error, "kind", None)
            raise WorkspaceUIError(reason if isinstance(reason, str)
                                   else "command_refresh_unavailable") from None
        if not isinstance(result, dict):
            raise WorkspaceUIError("command_refresh_unavailable")
        return result

    def refresh_command_policy_status(self, token, request_id):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self._command_policy_refresh_status is None:
            raise WorkspaceUIError("command_refresh_unavailable")
        try:
            result = self._command_policy_refresh_status(token, request_id)
        except Exception as error:
            reason = getattr(error, "kind", None)
            raise WorkspaceUIError(reason if isinstance(reason, str)
                                   else "command_refresh_unavailable") from None
        if not isinstance(result, dict):
            raise WorkspaceUIError("command_refresh_unavailable")
        return result

    def provision_command_policy_for_operator_message(self, token, operation_key, actions, request_id):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self._command_policy_provision is None:
            raise WorkspaceUIError("command_refresh_unavailable")
        try:
            result = self._command_policy_provision(token, operation_key, actions, request_id)
        except Exception as error:
            reason = getattr(error, "kind", None)
            raise WorkspaceUIError(reason if isinstance(reason, str)
                                   else "command_refresh_unavailable") from None
        if not isinstance(result, dict):
            raise WorkspaceUIError("command_refresh_unavailable")
        return result

    def perform_command(self, body, *, lookup=False, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.operator_commands is None:
            raise WorkspaceCommandError("command_scope_required")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key, audience="operator", allow_text=True)
        try:
            return self.operator_commands.perform(body, view_scope=scope, lookup=lookup,
                authorization_constraint=authorization_constraint)
        except WorkspaceCommandError as error:
            if str(error) == "operator_session_refused":
                raise WorkspaceUIError("session_expired") from None
            raise

    def perform_disposition(self, body, *, lookup=False, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.operator_dispositions is None:
            raise WorkspaceCommandError("command_scope_required")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key,
                          audience="operator", allow_text=True)
        try:
            return self.operator_dispositions.perform(body, view_scope=scope, lookup=lookup,
                authorization_constraint=authorization_constraint)
        except WorkspaceCommandError as error:
            if str(error) == "operator_session_refused":
                raise WorkspaceUIError("session_expired") from None
            raise

    def perform_linked_replacement(self, body, *, lookup=False, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.operator_linked_replacements is None:
            raise WorkspaceCommandError("linked_scope_required")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key,
                          audience="operator", allow_text=True)
        try:
            return self.operator_linked_replacements.perform(
                body, view_scope=scope, lookup=lookup,
                authorization_constraint=authorization_constraint)
        except WorkspaceCommandError as error:
            if str(error) == "operator_session_refused":
                raise WorkspaceUIError("session_expired") from None
            raise

    def perform_message(self, body, *, lookup=False, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.operator_messages is None:
            raise WorkspaceCommandError("message_scope_required")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key,
                          audience="operator", allow_text=True)
        try:
            return self.operator_messages.perform(body, view_scope=scope, lookup=lookup,
                authorization_constraint=authorization_constraint)
        except WorkspaceCommandError as error:
            if str(error) == "message_session_refused":
                raise WorkspaceUIError("session_expired") from None
            raise

    def perform_draft_key(self, body, *, authorization_constraint=None):
        if self._server is None:
            raise WorkspaceUIError("server_not_started")
        if self.operator_drafts is None:
            raise WorkspaceCommandError("message_retention_required")
        scope = ViewScope(self.workspace_id, self.coordinator.run_id, self._key,
                          audience="operator", allow_text=True)
        try:
            return self.operator_drafts.perform(body, view_scope=scope,
                authorization_constraint=authorization_constraint)
        except WorkspaceCommandError as error:
            if str(error) == "operator_session_refused":
                raise WorkspaceUIError("session_expired") from None
            raise

    def stop(self):
        server, thread = self._server, self._thread
        with self._lock:
            self.bootstrap_code = self._session_digest = None
            self._bootstrap_deadline = self._session_deadline = 0.0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self._server = self._thread = None
        self._key = b""

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
