"""Private noninteractive client for an already-running WorkspaceHost.

The client only speaks to the foreground-owned loopback HTTP surface. It never
opens the journal, starts a second writer, bootstraps a session, or launches a
provider. Tokens and request bodies are read from private files and are never
included in errors or telemetry.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import stat
from urllib.parse import urlsplit

from _fleet_approval import _regular_single_link, _verify_private


class WorkspaceClientError(ValueError):
    def __init__(self, kind: str, *, details: dict | None = None):
        self.kind = kind
        self.details = details or {}
        super().__init__(kind)


_PUBLIC_ERRORS = {
    "authentication_required", "session_expired", "bootstrap_refused",
    "scope_refused", "host_refused", "origin_refused",
    "command_scope_required", "message_scope_required",
    "message_retention_required", "stale_snapshot", "stale_or_invalid_cursor",
    "snapshot_changed", "history_snapshot_mismatch",
    "anchor_scope_or_history_refused", "command_request_conflict",
    "command_state_refused", "message_request_conflict",
    "snapshot_unavailable", "incomplete_snapshot", "workspace_unprepared",
    "reconciliation_required", "command_outcome_uncertain",
    "command_snapshot_unavailable", "operator_runtime_unavailable",
    "message_outcome_uncertain", "message_snapshot_unavailable",
    "message_runtime_unavailable", "message_key_unavailable",
    "read_only_surface", "query_refused", "request_refused", "not_found",
    "command_refresh_authentication_required", "command_refresh_unavailable",
    "command_refresh_request_invalid", "command_refresh_generation_stale",
    "command_refresh_history_full", "command_refresh_request_conflict",
    "command_target_refused", "command_policy_changed", "workspace_snapshot_unavailable",
    "disposition_scope_required", "disposition_state_refused", "disposition_request_conflict",
    "disposition_outcome_uncertain", "disposition_snapshot_unavailable",
    "linked_scope_required", "linked_target_refused", "linked_request_conflict",
    "linked_outcome_uncertain", "linked_snapshot_unavailable",
    "detail_scope_required", "detail_body_scope_required", "detail_runtime_unavailable",
    "detail_scope_refused", "detail_target_refused", "detail_type_refused", "detail_task_refused",
    "detail_scope_revoked", "detail_binding_stale", "detail_snapshot_unavailable", "detail_unavailable",
    "detail_body_unavailable", "detail_request_invalid",
}
_OPERATION_KEY = re.compile(r"[a-f0-9]{32}\Z")


def _private_bytes(path: str, maximum: int, kind: str) -> bytes:
    if type(path) is not str or not path:
        raise WorkspaceClientError(kind + "_required")
    descriptor = None
    try:
        expected = _regular_single_link(path, kind)
        _verify_private(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        raw = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        current = _regular_single_link(path, kind)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (not raw or len(raw) > maximum or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                or identity(before) != identity(after) or identity(after) != identity(current)
                or before.st_size != len(raw)):
            raise OSError
        return raw
    except WorkspaceClientError:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkspaceClientError(kind + "_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _endpoint(url: str) -> tuple[str, int, str]:
    if type(url) is not str or len(url) > 256:
        raise WorkspaceClientError("host_url_invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, UnicodeError):
        raise WorkspaceClientError("host_url_invalid") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise WorkspaceClientError("host_url_invalid")
    if port is None or not 1 <= port <= 65535:
        raise WorkspaceClientError("host_url_invalid")
    return parsed.hostname, port, "127.0.0.1:" + str(port)


def _decode_response(response: http.client.HTTPResponse) -> tuple[int, dict]:
    raw = response.read(65537)
    if len(raw) > 65536:
        raise WorkspaceClientError("response_too_large")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise WorkspaceClientError("response_invalid") from None
    if not isinstance(result, dict):
        raise WorkspaceClientError("response_invalid")
    return response.status, result


def _error_from_response(status: int, result: dict) -> WorkspaceClientError:
    if 200 <= status < 300:
        return WorkspaceClientError("response_invalid")
    error = result.get("error")
    if not isinstance(error, str) or error not in _PUBLIC_ERRORS:
        error = "request_refused"
    return WorkspaceClientError(error)


def _validate_refresh_result(result: dict, *, request_id: str,
                              expected_workspace_id: str | None = None,
                              expected_run_id: str | None = None,
                              allow_history: bool = True) -> None:
    required = {"schema", "status", "workspace_id", "run_id", "generation",
                "target_count", "provider_calls", "request_id"}
    allowed_status = {"refreshed", "history_not_retained"} if allow_history else {"refreshed"}
    if (set(result) != required
            or result.get("schema") not in {
                "summon.workspace.command-refresh-status/v1",
                "summon.workspace.command-refresh/v1"}
            or result.get("status") not in allowed_status
            or type(result.get("workspace_id")) is not str
            or type(result.get("run_id")) is not str
            or type(result.get("generation")) is not int
            or result["generation"] < 1
            or type(result.get("target_count")) is not int
            or not 1 <= result["target_count"] <= 128
            or type(result.get("provider_calls")) is not int
            or result["provider_calls"] != 0
            or result.get("request_id") != request_id
            or (expected_workspace_id is not None
                and result["workspace_id"] != expected_workspace_id)
            or (expected_run_id is not None and result["run_id"] != expected_run_id)):
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id})


def _validate_identity(result: dict, *, expected_workspace_id: str | None,
                       expected_run_id: str | None) -> None:
    if (set(result) != {"schema", "workspace_id", "run_id"}
            or result.get("schema") != "summon.workspace.identity/v1"
            or type(result.get("workspace_id")) is not str
            or type(result.get("run_id")) is not str
            or not result["workspace_id"] or not result["run_id"]):
        raise WorkspaceClientError("response_invalid")
    if (expected_workspace_id is not None
            and result["workspace_id"] != expected_workspace_id):
        raise WorkspaceClientError("workspace_scope_mismatch")
    if expected_run_id is not None and result["run_id"] != expected_run_id:
        raise WorkspaceClientError("workspace_scope_mismatch")


def _validate_result(result: dict, *, kind: str, lookup: bool,
                     expected_operation_key: str | None = None,
                     expected_action: str | None = None,
                     expected_target: str | None = None,
                     expected_reason: str | None = None,
                     expected_parent_target: str | None = None,
                     expected_recipient_target: str | None = None) -> None:
    if kind == "command":
        required = {"operation_key", "action", "status", "target_state", "revision",
                    "retry_with_new_key", "task_or_provider_action"}
        if set(result) != required or type(result.get("operation_key")) is not str \
                or not _OPERATION_KEY.fullmatch(result["operation_key"]) \
                or type(result.get("action")) is not str \
                or (expected_operation_key is not None
                    and result["operation_key"] != expected_operation_key) \
                or (expected_action is not None and result["action"] != expected_action) \
                or type(result.get("status")) is not str \
                or result["status"] not in {"pending", "recorded", "uncertain"} \
                or (result["target_state"] is not None and type(result["target_state"]) is not str) \
                or type(result.get("revision")) is not int or result["revision"] < 0 \
                or type(result.get("retry_with_new_key")) is not bool \
                or result["task_or_provider_action"] is not False:
            raise WorkspaceClientError("response_invalid")
        expected_state = {"cancel_queued_context": "cancelled",
                          "dispose_held_context": "dead_lettered"}.get(result["action"])
        if (result["status"] == "recorded" and result["target_state"] != expected_state) \
                or (result["status"] != "recorded" and result["target_state"] is not None):
            raise WorkspaceClientError("response_invalid")
        return
    if kind == "disposition":
        required = {"schema", "status", "operation_key", "action", "target", "task_id", "reason",
                    "revision", "retry_with_new_key", "execution_authorized"}
        if (set(result) != required
                or result.get("schema") != "summon.workspace.operator-disposition-result/v1"
                or result.get("status") not in {"recorded", "not_observed", "uncertain"}
                or type(result.get("operation_key")) is not str
                or not _OPERATION_KEY.fullmatch(result["operation_key"])
                or (expected_operation_key is not None and result["operation_key"] != expected_operation_key)
                or result.get("action") != "retain_held_context"
                or type(result.get("target")) is not str
                or (expected_target is not None and result["target"] != expected_target)
                or type(result.get("task_id")) is not str
                or result.get("reason") not in {"awaiting_evidence", "awaiting_authorization", "operator_hold"}
                or (expected_reason is not None and result["reason"] != expected_reason)
                or type(result.get("revision")) is not int or result["revision"] < 0
                or result.get("retry_with_new_key") is not False
                or result.get("execution_authorized") is not False):
            raise WorkspaceClientError("response_invalid")
        return
    if kind == "linked":
        required = {"schema", "status", "operation_key", "parent_target", "recipient_target",
                    "child_delivery", "revision", "execution_authorized", "retry_with_new_key"}
        if (set(result) != required
                or result.get("schema") != "summon.workspace.linked-replacement-result/v1"
                or result.get("status") not in {"recorded", "already_recorded", "not_observed", "uncertain"}
                or type(result.get("operation_key")) is not str
                or not _OPERATION_KEY.fullmatch(result["operation_key"])
                or (expected_operation_key is not None and result["operation_key"] != expected_operation_key)
                or type(result.get("parent_target")) is not str
                or type(result.get("recipient_target")) is not str
                or (expected_parent_target is not None and result["parent_target"] != expected_parent_target)
                or (expected_recipient_target is not None and result["recipient_target"] != expected_recipient_target)
                or (result.get("child_delivery") is not None and (
                    type(result["child_delivery"]) is not str
                    or not re.fullmatch(r"delivery_[A-Za-z0-9_-]{32}", result["child_delivery"])))
                or type(result.get("revision")) is not int or result["revision"] < 0
                or result.get("execution_authorized") is not False
                or result.get("retry_with_new_key") is not False):
            raise WorkspaceClientError("response_invalid")
        if result["status"] in {"recorded", "already_recorded"} and result["child_delivery"] is None:
            raise WorkspaceClientError("response_invalid")
        if result["status"] == "not_observed" and result["child_delivery"] is not None:
            raise WorkspaceClientError("response_invalid")
        return
    required = {"status", "operation_key", "target", "task_id", "revision",
                "execution_authorized", "retry_with_new_key", "message_id",
                "delivery_id", "delivery_state", "stream_sequence"}
    if (set(result) != required or type(result.get("status")) is not str
            or result["status"] not in {"queued", "not_observed", "uncertain"}
            or type(result.get("operation_key")) is not str
            or not _OPERATION_KEY.fullmatch(result["operation_key"])
            or (expected_operation_key is not None
                and result["operation_key"] != expected_operation_key)
            or type(result.get("target")) is not str or type(result.get("task_id")) is not str
            or (expected_target is not None and result["target"] != expected_target)
            or type(result.get("revision")) is not int or result["revision"] < 0
            or result["execution_authorized"] is not False
            or type(result.get("retry_with_new_key")) is not bool
            or (result["message_id"] is not None and type(result["message_id"]) is not str)
            or (result["delivery_id"] is not None and type(result["delivery_id"]) is not str)
            or (result["delivery_state"] is not None and result["delivery_state"] != "queued")
            or (result["stream_sequence"] is not None
                and (type(result["stream_sequence"]) is not int or result["stream_sequence"] < 0))):
        raise WorkspaceClientError("response_invalid")


def request(*, url: str, token_file: str, request_file: str,
            kind: str, lookup: bool = False, expected_workspace_id: str | None = None,
            expected_run_id: str | None = None) -> dict:
    if kind not in {"command", "message", "disposition", "linked"}:
        raise WorkspaceClientError("request_kind_invalid")
    host, port, host_header = _endpoint(url)
    token = _private_bytes(token_file, 2048, "session_token").strip()
    if not token or any(byte < 0x21 or byte > 0x7e for byte in token):
        raise WorkspaceClientError("session_token_invalid")
    raw = _private_bytes(request_file, 8192, "request").strip()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise WorkspaceClientError("request_invalid") from None
    if type(payload) is not dict:
        raise WorkspaceClientError("request_invalid")
    expected_operation_key = payload.get("operation_key")
    if (type(expected_operation_key) is not str
            or not _OPERATION_KEY.fullmatch(expected_operation_key)):
        raise WorkspaceClientError("request_invalid")
    expected_action = payload.get("action") if kind == "command" else None
    expected_target = payload.get("target") if kind in {"message", "disposition"} else None
    expected_parent_target = payload.get("parent_target") if kind == "linked" else None
    expected_recipient_target = payload.get("recipient_target") if kind == "linked" else None
    expected_reason = payload.get("reason") if kind == "disposition" else None
    if kind == "command" and (type(expected_action) is not str
                               or expected_action not in {"cancel_queued_context", "dispose_held_context"}
                               or type(payload.get("target")) is not str):
        raise WorkspaceClientError("request_invalid")
    if kind == "message" and type(expected_target) is not str:
        raise WorkspaceClientError("request_invalid")
    if kind == "disposition" and (type(expected_target) is not str
                                   or type(expected_reason) is not str
                                   or expected_reason not in {"awaiting_evidence", "awaiting_authorization", "operator_hold"}):
        raise WorkspaceClientError("request_invalid")
    if kind == "linked" and (set(payload) != {"operation_key", "parent_target", "recipient_target"}
                              or type(payload.get("parent_target")) is not str
                              or type(payload.get("recipient_target")) is not str):
        raise WorkspaceClientError("request_invalid")
    try:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkspaceClientError("request_invalid") from None
    path = "/api/" + ("messages" if kind == "message" else
                       "dispositions" if kind == "disposition" else
                       "linked-replacements" if kind == "linked" else "commands")
    if lookup:
        path += "/lookup"
    headers = {
        "Host": host_header,
        "Origin": "http://" + host_header,
        "Authorization": "Bearer " + token.decode("ascii"),
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    identity_connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        identity_connection.request("GET", "/api/identity", headers={
            "Host": host_header, "Origin": "http://" + host_header,
            "Authorization": "Bearer " + token.decode("ascii"),
        })
        identity_status, identity_result = _decode_response(identity_connection.getresponse())
    except (OSError, TimeoutError, http.client.HTTPException):
        raise WorkspaceClientError("host_unavailable") from None
    finally:
        identity_connection.close()
    if identity_status < 200 or identity_status >= 300:
        raise _error_from_response(identity_status, identity_result)
    _validate_identity(identity_result, expected_workspace_id=expected_workspace_id,
                       expected_run_id=expected_run_id)
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        try:
            status, result = _decode_response(response)
        except WorkspaceClientError:
            raise WorkspaceClientError("outcome_unknown_lookup_required") from None
    except (OSError, TimeoutError, http.client.HTTPException):
        # The request may have reached the host; never blindly retry a command
        # or message.  Callers must use the same operation key with --lookup.
        raise WorkspaceClientError("outcome_unknown_lookup_required") from None
    finally:
        connection.close()
    if status < 200 or status >= 300:
        raise _error_from_response(status, result)
    try:
        _validate_result(result, kind=kind, lookup=lookup,
                         expected_operation_key=expected_operation_key,
                         expected_action=expected_action,
                         expected_target=expected_target,
                         expected_reason=expected_reason,
                         expected_parent_target=expected_parent_target,
                         expected_recipient_target=expected_recipient_target)
    except WorkspaceClientError:
        raise WorkspaceClientError("outcome_unknown_lookup_required") from None
    return result


def refresh_status(*, url: str, control_token_file: str, request_id: str,
                   expected_workspace_id: str | None = None,
                   expected_run_id: str | None = None) -> dict:
    """Read-only reconciliation for one previously issued refresh request."""
    host, port, host_header = _endpoint(url)
    token = _private_bytes(control_token_file, 2048, "command_refresh_token").strip()
    if not token or any(byte < 0x21 or byte > 0x7e for byte in token):
        raise WorkspaceClientError("command_refresh_token_invalid")
    if (type(request_id) is not str or not 16 <= len(request_id) <= 128
            or not request_id.isascii()
            or any(char not in "0123456789abcdef" for char in request_id)):
        raise WorkspaceClientError("command_refresh_request_invalid")
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(
            "GET", "/api/commands/refresh/status?request_id=" + request_id,
            headers={"Host": host_header, "Origin": "http://" + host_header,
                     "X-Summon-Command-Refresh": token.decode("ascii")})
        response = connection.getresponse()
        status, result = _decode_response(response)
    except WorkspaceClientError:
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id}) from None
    except (OSError, TimeoutError, http.client.HTTPException):
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id}) from None
    finally:
        connection.close()
    if status < 200 or status >= 300:
        raise _error_from_response(status, result)
    required = {"schema", "status", "workspace_id", "run_id", "generation",
                "target_count", "provider_calls", "request_id"}
    if (set(result) != required
            or result.get("schema") not in {
                "summon.workspace.command-refresh-status/v1",
                "summon.workspace.command-refresh/v1"}
            or result.get("status") not in {"history_not_retained", "refreshed"}
            or type(result.get("workspace_id")) is not str
            or type(result.get("run_id")) is not str
            or type(result.get("generation")) is not int
            or result["generation"] < 1
            or type(result.get("target_count")) is not int
            or not 1 <= result["target_count"] <= 128
            or type(result.get("provider_calls")) is not int
            or result["provider_calls"] != 0
            or result.get("request_id") != request_id
            or (expected_workspace_id is not None
                and result["workspace_id"] != expected_workspace_id)
            or (expected_run_id is not None and result["run_id"] != expected_run_id)):
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id})
    return result


def refresh(*, url: str, control_token_file: str,
            expected_workspace_id: str | None = None,
            expected_run_id: str | None = None,
            request_id: str | None = None) -> dict:
    """Reload the host's explicitly configured policy using separate control auth."""
    host, port, host_header = _endpoint(url)
    token = _private_bytes(control_token_file, 2048, "command_refresh_token").strip()
    if not token or any(byte < 0x21 or byte > 0x7e for byte in token):
        raise WorkspaceClientError("command_refresh_token_invalid")
    if request_id is None:
        request_id = secrets.token_hex(16)
    if (type(request_id) is not str or not 16 <= len(request_id) <= 128
            or not request_id.isascii()
            or any(char not in "0123456789abcdef" for char in request_id)):
        raise WorkspaceClientError("command_refresh_request_invalid")

    def control_headers():
        return {
            "Host": host_header,
            "Origin": "http://" + host_header,
            "X-Summon-Command-Refresh": token.decode("ascii"),
        }

    def status_request():
        connection = http.client.HTTPConnection(host, port, timeout=10)
        try:
            connection.request("GET", "/api/commands/refresh/status?request_id=" + request_id,
                               headers=control_headers())
            response = connection.getresponse()
            status, result = _decode_response(response)
        except WorkspaceClientError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise WorkspaceClientError("outcome_unknown_lookup_required",
                                       details={"request_id": request_id}) from None
        finally:
            connection.close()
        if status < 200 or status >= 300:
            raise _error_from_response(status, result)
        required = {"schema", "status", "workspace_id", "run_id", "generation",
                    "target_count", "provider_calls", "request_id"}
        if (set(result) != required
                or result.get("schema") not in {
                    "summon.workspace.command-refresh-status/v1",
                    "summon.workspace.command-refresh/v1"}
                or result.get("status") not in {"history_not_retained", "refreshed"}
                or type(result.get("workspace_id")) is not str
                or type(result.get("run_id")) is not str
                or type(result.get("generation")) is not int
                or result["generation"] < 1
                or type(result.get("target_count")) is not int
                or not 1 <= result["target_count"] <= 128
                or type(result.get("provider_calls")) is not int
                or result["provider_calls"] != 0
                or result.get("request_id") != request_id):
            raise WorkspaceClientError("outcome_unknown_lookup_required",
                                       details={"request_id": request_id})
        if (expected_workspace_id is not None
                and result["workspace_id"] != expected_workspace_id):
            raise WorkspaceClientError("workspace_scope_mismatch")
        if expected_run_id is not None and result["run_id"] != expected_run_id:
            raise WorkspaceClientError("workspace_scope_mismatch")
        return result

    baseline = status_request()
    workspace_id = expected_workspace_id or baseline["workspace_id"]
    run_id = expected_run_id or baseline["run_id"]
    body = json.dumps({"request_id": request_id, "workspace_id": workspace_id,
                       "run_id": run_id}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    headers = {
        "Host": host_header,
        "Origin": "http://" + host_header,
        "X-Summon-Command-Refresh": token.decode("ascii"),
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("POST", "/api/commands/refresh", body=body, headers=headers)
        response = connection.getresponse()
        try:
            status, result = _decode_response(response)
        except WorkspaceClientError:
            # A successful append may have been followed by a lost/malformed
            # response. Reconcile by request id; never replay blindly.
            try:
                observed = status_request()
            except WorkspaceClientError:
                raise WorkspaceClientError("outcome_unknown_lookup_required",
                                           details={"request_id": request_id}) from None
            if observed.get("status") == "refreshed":
                return observed
            raise WorkspaceClientError("outcome_unknown_lookup_required",
                                       details={"request_id": request_id}) from None
    except (OSError, TimeoutError, http.client.HTTPException):
        try:
            observed = status_request()
        except WorkspaceClientError:
            raise WorkspaceClientError("outcome_unknown_lookup_required",
                                       details={"request_id": request_id}) from None
        if observed.get("status") == "refreshed":
            return observed
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id}) from None
    finally:
        connection.close()
    if status < 200 or status >= 300:
        raise _error_from_response(status, result)
    required = {"schema", "status", "workspace_id", "run_id", "generation",
                "target_count", "provider_calls", "request_id"}
    if (set(result) != required or result.get("schema") != "summon.workspace.command-refresh/v1"
            or result.get("status") != "refreshed"
            or type(result.get("workspace_id")) is not str
            or type(result.get("run_id")) is not str
            or (expected_workspace_id is not None and result["workspace_id"] != expected_workspace_id)
            or (expected_run_id is not None and result["run_id"] != expected_run_id)
            or type(result.get("generation")) is not int or result["generation"] < 1
            or type(result.get("target_count")) is not int or not 1 <= result["target_count"] <= 128
            or type(result.get("provider_calls")) is not int
            or result.get("provider_calls") != 0
            or result.get("request_id") != request_id):
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id})
    return result


def provision_message(*, url: str, control_token_file: str, operation_key: str,
                      actions, request_id: str,
                      expected_workspace_id: str | None = None,
                      expected_run_id: str | None = None) -> dict:
    """Owner-control bridge for provisioning a sent operator message.

    This is intentionally separate from browser bearer requests.  It accepts
    only the opaque send operation key and finite action names; the host
    resolves the private delivery identity and returns the ordinary refresh
    projection.  A transport failure is unknown and must be reconciled by
    ``refresh_status`` with the same request id.
    """
    host, port, host_header = _endpoint(url)
    token = _private_bytes(control_token_file, 2048, "command_refresh_token").strip()
    if not token or any(byte < 0x21 or byte > 0x7e for byte in token):
        raise WorkspaceClientError("command_refresh_token_invalid")
    if type(operation_key) is not str or not _OPERATION_KEY.fullmatch(operation_key):
        raise WorkspaceClientError("command_refresh_request_invalid")
    if (type(actions) not in (list, tuple) or not 1 <= len(actions) <= 2
            or any(type(action) is not str
                   or action not in {"cancel_queued_context", "dispose_held_context"}
                   for action in actions)
            or len(set(actions)) != len(actions)):
        raise WorkspaceClientError("command_policy_invalid")
    if (type(request_id) is not str or not 16 <= len(request_id) <= 128
            or not request_id.isascii()
            or any(char not in "0123456789abcdef" for char in request_id)):
        raise WorkspaceClientError("command_refresh_request_invalid")
    payload = {"request_id": request_id, "operation_key": operation_key,
               "actions": list(actions),
               "workspace_id": expected_workspace_id or "workspace",
               "run_id": expected_run_id or "run"}
    try:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkspaceClientError("command_refresh_request_invalid") from None
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(
            "POST", "/api/commands/provision-message", body=body,
            headers={"Host": host_header, "Origin": "http://" + host_header,
                     "X-Summon-Command-Refresh": token.decode("ascii"),
                     "Content-Type": "application/json",
                     "Content-Length": str(len(body))})
        response = connection.getresponse()
        result_status, result = _decode_response(response)
    except WorkspaceClientError:
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id}) from None
    except (OSError, TimeoutError, http.client.HTTPException):
        raise WorkspaceClientError("outcome_unknown_lookup_required",
                                   details={"request_id": request_id}) from None
    finally:
        connection.close()
    if result_status < 200 or result_status >= 300:
        raise _error_from_response(result_status, result)
    try:
        _validate_refresh_result(result, request_id=request_id,
                                 expected_workspace_id=expected_workspace_id,
                                 expected_run_id=expected_run_id, allow_history=False)
    except WorkspaceClientError:
        # A syntactically valid 2xx response is not proof that the mutation was
        # applied.  Preserve the request id so the caller can reconcile it
        # without issuing a blind second provisioning request.
        raise WorkspaceClientError(
            "outcome_unknown_lookup_required",
            details={"request_id": request_id},
        ) from None
    return result


__all__ = ["WorkspaceClientError", "request", "refresh", "refresh_status",
           "provision_message"]
