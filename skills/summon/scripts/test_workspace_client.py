"""Provider-free tests for the noninteractive loopback workspace client."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_client as client


class _Response:
    def __init__(self, status, value):
        self.status = status
        self._raw = json.dumps(value, separators=(",", ":")).encode("utf-8")

    def read(self, _limit):
        return self._raw


class _Connection:
    responses = []
    calls = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        pass


def _files(tmp_path, payload):
    token = tmp_path / "token"
    request = tmp_path / "request"
    token.write_text("t" * 32, encoding="ascii")
    request.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    return token, request


def _identity():
    return _Response(200, {"schema": "summon.workspace.identity/v1",
                           "workspace_id": "workspace", "run_id": "run"})


def _command():
    return _Response(200, {"operation_key": "a" * 32, "action": "cancel_queued_context",
                           "status": "pending", "target_state": None, "revision": 1,
                           "retry_with_new_key": False, "task_or_provider_action": False})


def _disposition():
    return _Response(200, {"schema": "summon.workspace.operator-disposition-result/v1",
                           "operation_key": "a" * 32, "action": "retain_held_context",
                           "target": "delivery_x", "task_id": "task_x",
                           "reason": "awaiting_evidence", "status": "recorded", "revision": 1,
                           "retry_with_new_key": False, "execution_authorized": False})


def _linked(*, parent="linked-parent_x", recipient="linked-recipient_y",
            status="recorded", child="delivery_" + "z" * 32):
    return _Response(200, {"schema": "summon.workspace.linked-replacement-result/v1",
                           "status": status, "operation_key": "a" * 32,
                           "parent_target": parent, "recipient_target": recipient,
                           "child_delivery": child, "revision": 1,
                           "execution_authorized": False, "retry_with_new_key": False})


def _refresh(value, *, request_id="c" * 32, status="refreshed"):
    schema = ("summon.workspace.command-refresh/v1" if status == "refreshed"
              else "summon.workspace.command-refresh-status/v1")
    return _Response(200, {"schema": schema, "status": status,
                           "workspace_id": "workspace", "run_id": "run",
                           "generation": 2, "target_count": 1,
                           "provider_calls": 0, "request_id": request_id})


def test_client_preflights_identity_and_validates_command(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "action": "cancel_queued_context", "target": "delivery_x"})
    _Connection.responses = [_identity(), _command()]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                            request_file=str(request), kind="command", expected_run_id="run")
    assert result["status"] == "pending"
    assert [call[0:2] for call in _Connection.calls] == [("GET", "/api/identity"), ("POST", "/api/commands")]


def test_client_validates_authenticated_retain_disposition(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "target": "delivery_x", "reason": "awaiting_evidence"})
    _Connection.responses = [_identity(), _disposition()]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                            request_file=str(request), kind="disposition", expected_run_id="run")
    assert result["status"] == "recorded"
    assert [call[0:2] for call in _Connection.calls] == [("GET", "/api/identity"), ("POST", "/api/dispositions")]


def test_client_validates_linked_replacement_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "parent_target": "linked-parent_x",
                                       "recipient_target": "linked-recipient_y"})
    _Connection.responses = [_identity(), _linked()]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                            request_file=str(request), kind="linked", expected_run_id="run")
    assert result["status"] == "recorded"
    assert [call[0:2] for call in _Connection.calls] == [("GET", "/api/identity"),
                                                          ("POST", "/api/linked-replacements")]


@pytest.mark.parametrize("response", [
    _linked(parent="wrong-parent"),
    _linked(recipient="wrong-recipient"),
    _linked(child="forged-child"),
])
def test_client_rejects_mismatched_linked_2xx_as_lookup_required(monkeypatch, tmp_path, response):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "parent_target": "linked-parent_x",
                                       "recipient_target": "linked-recipient_y"})
    _Connection.responses = [_identity(), response]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    with pytest.raises(client.WorkspaceClientError) as raised:
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="linked", expected_run_id="run")
    assert raised.value.kind == "outcome_unknown_lookup_required"


def test_client_rejects_malformed_url_and_nan(monkeypatch, tmp_path):
    with pytest.raises(client.WorkspaceClientError, match="host_url_invalid"):
        client._endpoint("http://127.0.0.1:bad")
    token = tmp_path / "token"
    request = tmp_path / "request"
    token.write_text("t" * 32, encoding="ascii")
    request.write_text('{"value": NaN}', encoding="utf-8")
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    with pytest.raises(client.WorkspaceClientError, match="request_invalid"):
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="command")


def test_client_does_not_retry_unknown_post_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "action": "cancel_queued_context", "target": "delivery_x"})
    _Connection.responses = [_identity(), http.client.RemoteDisconnected("closed")]
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    with pytest.raises(client.WorkspaceClientError, match="outcome_unknown_lookup_required"):
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="command", expected_run_id="run")


def test_client_binds_response_to_operation_and_rejects_invalid_success(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "action": "cancel_queued_context", "target": "delivery_x"})
    wrong = _Response(200, {"operation_key": "b" * 32, "action": "cancel_queued_context",
                            "status": "pending", "target_state": None, "revision": 1,
                            "retry_with_new_key": False, "task_or_provider_action": False})
    _Connection.responses = [_identity(), wrong]
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    with pytest.raises(client.WorkspaceClientError, match="outcome_unknown_lookup_required"):
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="command", expected_run_id="run")

    invalid = _Response(200, {"operation_key": "a" * 32, "action": "cancel_queued_context",
                              "status": "recorded", "target_state": "cancelled", "revision": 1,
                              "retry_with_new_key": False, "task_or_provider_action": True})
    _Connection.responses = [_identity(), invalid]
    with pytest.raises(client.WorkspaceClientError, match="outcome_unknown_lookup_required"):
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="command", expected_run_id="run")


def test_client_treats_malformed_post_response_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token, request = _files(tmp_path, {"operation_key": "a" * 32,
                                       "action": "cancel_queued_context", "target": "delivery_x"})

    class _Malformed(_Connection):
        def getresponse(self):
            if len(self.calls) == 1:
                return _identity()
            response = _Response(200, {})
            response.read = lambda _limit: b"not-json"
            return response

    _Malformed.responses = []
    _Malformed.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Malformed)
    with pytest.raises(client.WorkspaceClientError, match="outcome_unknown_lookup_required"):
        client.request(url="http://127.0.0.1:43121/", token_file=str(token),
                       request_file=str(request), kind="command", expected_run_id="run")


def test_refresh_uses_separate_control_header_and_reconciles_by_request_id(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(client.secrets, "token_hex", lambda _size: "c" * 32)
    token = tmp_path / "control-token"
    token.write_text("r" * 32, encoding="ascii")
    _Connection.responses = [_refresh({}, request_id="c" * 32, status="history_not_retained"),
                             _refresh({}, request_id="c" * 32)]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.refresh(url="http://127.0.0.1:43121/",
                            control_token_file=str(token), expected_run_id="run")
    assert result["status"] == "refreshed"
    assert [call[0:2] for call in _Connection.calls] == [
        ("GET", "/api/commands/refresh/status?request_id=" + "c" * 32),
        ("POST", "/api/commands/refresh"),
    ]
    method, path, body, headers = _Connection.calls[1]
    assert headers["X-Summon-Command-Refresh"] == "r" * 32
    assert "Authorization" not in headers
    assert json.loads(body) == {"request_id": "c" * 32,
                                "workspace_id": "workspace", "run_id": "run"}


def test_refresh_lost_response_returns_unknown_when_control_status_did_not_apply(
        monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(client.secrets, "token_hex", lambda _size: "d" * 32)
    token = tmp_path / "control-token"
    token.write_text("r" * 32, encoding="ascii")
    _Connection.responses = [_refresh({}, request_id="d" * 32, status="history_not_retained"),
                             http.client.RemoteDisconnected("closed"),
                             _refresh({}, request_id="d" * 32, status="history_not_retained")]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    with pytest.raises(client.WorkspaceClientError, match="outcome_unknown_lookup_required"):
        client.refresh(url="http://127.0.0.1:43121/",
                       control_token_file=str(token), expected_run_id="run")
    assert len(_Connection.calls) == 3


def test_refresh_status_is_read_only_and_accepts_history_not_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token = tmp_path / "control-token"
    token.write_text("r" * 32, encoding="ascii")
    _Connection.responses = [_refresh({}, request_id="e" * 32,
                                      status="history_not_retained")]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.refresh_status(url="http://127.0.0.1:43121/",
                                   control_token_file=str(token),
                                   request_id="e" * 32, expected_run_id="run")
    assert result["status"] == "history_not_retained"
    assert [call[0:2] for call in _Connection.calls] == [
        ("GET", "/api/commands/refresh/status?request_id=" + "e" * 32)]
    assert "Content-Length" not in _Connection.calls[0][3]


def test_provision_message_uses_control_header_and_exact_operation_binding(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token = tmp_path / "control-token"
    token.write_text("r" * 32, encoding="ascii")
    _Connection.responses = [_refresh({}, request_id="f" * 32)]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    result = client.provision_message(
        url="http://127.0.0.1:43121/", control_token_file=str(token),
        operation_key="e" * 32, actions=["cancel_queued_context"],
        request_id="f" * 32, expected_run_id="run")
    assert result["status"] == "refreshed"
    method, path, body, headers = _Connection.calls[0]
    assert (method, path) == ("POST", "/api/commands/provision-message")
    assert headers["X-Summon-Command-Refresh"] == "r" * 32
    assert "Authorization" not in headers
    assert json.loads(body) == {
        "request_id": "f" * 32, "operation_key": "e" * 32,
        "actions": ["cancel_queued_context"],
        "workspace_id": "workspace", "run_id": "run"}


def test_provision_message_keeps_request_id_when_2xx_projection_is_invalid(
        monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_verify_private", lambda *_args, **_kwargs: None)
    token = tmp_path / "control-token"
    token.write_text("r" * 32, encoding="ascii")
    # The host returned JSON and a 2xx status, but the projection is bound to a
    # different workspace.  The client must not treat that as success or retry
    # blindly; the same request id is required for reconciliation.
    invalid = _Response(200, {
        "schema": "summon.workspace.command-refresh/v1",
        "status": "refreshed", "workspace_id": "other-workspace",
        "run_id": "run", "generation": 2, "target_count": 1,
        "provider_calls": 0, "request_id": "f" * 32})
    _Connection.responses = [invalid]
    _Connection.calls = []
    monkeypatch.setattr(client.http.client, "HTTPConnection", _Connection)
    with pytest.raises(client.WorkspaceClientError) as raised:
        client.provision_message(
            url="http://127.0.0.1:43121/", control_token_file=str(token),
            operation_key="e" * 32, actions=["cancel_queued_context"],
            request_id="f" * 32, expected_workspace_id="workspace",
            expected_run_id="run")
    assert raised.value.kind == "outcome_unknown_lookup_required"
    assert raised.value.details == {"request_id": "f" * 32}
