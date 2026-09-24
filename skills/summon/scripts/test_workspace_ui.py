"""Actual loopback server + real coordinator fixture; no providers or rendering."""
from __future__ import annotations

import copy
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_ui as ui
from _workspace_runtime import WorkspaceRuntime
from _workspace_view import ViewScope
from test_workspace_runtime import plan


@pytest.fixture
def surface(tmp_path):
    prepared = plan()
    def authorize(event, state):
        if event == {"operation": "prepare", "plan": prepared} and state is None:
            return True
        return (event.get("event") in {"workspace_feature", "workspace_goal_defined", "workspace_lane_defined"}
                and event.get("workspace_id") == "workspace" and event.get("run_id") == "run")
    def reject_source(_):
        raise AssertionError("no evidence source needed by metadata-only preparation")
    runtime = WorkspaceRuntime.new(tmp_path / "runs", "run", "workspace", authorize=authorize,
                                   resolve_evidence=reject_source, clock=lambda: 2000.0)
    runtime.prepare(prepared, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64)
    instance = ui.WorkspaceSurface(runtime._coordinator, "workspace")
    instance.start()
    yield instance
    instance.stop()


def files(surface):
    root = Path(surface.coordinator.runs_root)
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def request(surface, method="GET", target="/api/view", *, token=None, payload=None,
            headers=None, raw=None):
    parsed = urlsplit(surface.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    supplied = dict(headers or {})
    if token is not None:
        supplied["Authorization"] = "Bearer " + token
    body = raw
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        supplied.setdefault("Content-Type", "application/json")
    if method == "POST":
        supplied.setdefault("Origin", surface.url.rstrip("/"))
    try:
        connection.request(method, target, body=body, headers=supplied)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def login(surface):
    code = surface.bootstrap_code
    status, _headers, body = request(surface, "POST", "/api/bootstrap", payload={"code": code})
    assert status == 200
    parsed = json.loads(body)
    assert parsed["scope"] == ("workspace_operator" if (surface.operator_commands is not None
                                                           or surface.operator_messages is not None
                                                           or surface.operator_drafts is not None
                                                           or surface.operator_dispositions is not None
                                                           or getattr(surface, "operator_linked_replacements", None) is not None
                                                           or getattr(surface, "workspace_details", None) is not None) else "workspace_read")
    assert surface.bootstrap_code is None
    return parsed["bearer"], code


def no_material(body, materials):
    if any(value and value.encode("utf-8") in body for value in materials):
        pytest.fail("response or log exposed forbidden session/source material", pytrace=False)


@pytest.fixture
def operator_fixture(tmp_path):
    """Real runtime/journal/source; host capability, never a success callback."""
    from _workspace_demo import ConductorDemo
    from _workspace_commands import OperatorCommandAdapter
    demo = ConductorDemo(tmp_path / "operator-runs")
    demo.prepare()
    grant = demo.grant("main-1", "worker-a-1")[0]
    entry = demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")
    authority = {"allowed": True}
    scope = {"workspace_id": demo.workspace_id, "run_id": demo.run_id,
             "targets": [{"delivery_id": entry["delivery_id"],
                          "actions": ["cancel_queued_context", "dispose_held_context"]}]}
    surfaces = []
    def start(key=b"s" * 32, *, commands=True, port=0):
        demo.permit({"operation": "install_operator_commands", "scope": scope})
        handle = demo.runtime.install_operator_commands(scope,
            authorize_command=lambda _source, _workspace: authority["allowed"])
        adapter = OperatorCommandAdapter(demo.runtime, handle) if commands else None
        surface = ui.WorkspaceSurface(demo.runtime._coordinator, demo.workspace_id,
            operator_commands=adapter, presentation_key=key, port=port)
        surface.start()
        surfaces.append(surface)
        return surface
    surface = start()
    try:
        yield demo, entry, authority, surface, start
    finally:
        for instance in surfaces:
            instance.stop()
        demo.cleanup()


@pytest.fixture
def disposition_fixture(tmp_path):
    """Real authenticated retain route with trusted proof resolution."""
    from _workspace_admission import build_operator_disposition_event
    from _workspace_commands import OperatorDispositionAdapter
    from _workspace_demo import ConductorDemo
    demo = ConductorDemo(tmp_path / "disposition-runs")
    demo.prepare()
    grant = demo.grant("main-1", "worker-a-1")[0]
    entry = demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")
    unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
    hold_ref = demo.evidence(demo.source({"kind": "fixture-context-hold"}), "main-1",
                             "retain-hold", "observation", entry["delivery_id"],
                             "held_for_recovery", "hold_observation")
    delivery = copy.deepcopy(demo.state()["workspace"]["deliveries"][entry["delivery_id"]])
    delivery.update(state="held_for_recovery", reason="recipient_drift", certainty=unknown)
    demo.record("workspace_delivery_advanced", {"delivery": delivery,
                 "evidence": {"hold_observation": hold_ref}, "supported_ack_levels": []},
                "retain-hold-transition")
    proof_base = {"schema": "summon.workspace.operator-disposition-proof/v1",
                  "operation_key": "a" * 32, "action": "retain_held_context",
                  "delivery_id": entry["delivery_id"], "task_id": "main-1",
                  "reason": "awaiting_evidence", "authority": "installed_operator_policy"}
    request_ref = demo.evidence(demo.source({**proof_base, "role": "request"}), "main-1",
                                "retain-request", "event", entry["delivery_id"])
    decision_ref = demo.evidence(demo.source({**proof_base, "role": "decision"}), "main-1",
                                 "retain-decision", "event", entry["delivery_id"])
    operation_key = "a" * 32
    demo.permit(build_operator_disposition_event(
        demo.state()["workspace"], operation_key=operation_key,
        delivery_id=entry["delivery_id"], task_id="main-1",
        request_ref=request_ref, decision_ref=decision_ref, reason="awaiting_evidence"))
    scope = {"workspace_id": demo.workspace_id, "run_id": demo.run_id,
             "targets": [{"delivery_id": entry["delivery_id"],
                           "actions": ["retain_held_context"]}]}
    demo.permit({"operation": "install_operator_commands", "scope": scope})
    handle = demo.runtime.install_operator_commands(scope,
        authorize_command=lambda _source, _workspace: True)
    adapter = OperatorDispositionAdapter(demo.runtime, handle)
    surface = ui.WorkspaceSurface(demo.runtime._coordinator, demo.workspace_id,
                                  operator_dispositions=adapter, presentation_key=b"d" * 32)
    surface.start()
    try:
        yield demo, surface, entry, operation_key
    finally:
        surface.stop()
        demo.cleanup()


def test_authenticated_retain_route_records_and_resolves_same_key(disposition_fixture):
    demo, surface, entry, operation_key = disposition_fixture
    token, _ = login(surface)
    status, _, raw = request(surface, token=token)
    assert status == 200
    target = json.loads(raw)["operator_dispositions"]["targets"][0]["id"]
    body = {"operation_key": operation_key, "target": target, "reason": "awaiting_evidence"}
    status, _, raw = request(surface, "POST", "/api/dispositions", token=token, payload=body)
    assert status == 200
    assert json.loads(raw)["status"] == "recorded"
    status, _, raw = request(surface, "POST", "/api/dispositions/lookup", token=token, payload=body)
    assert status == 200
    assert json.loads(raw)["status"] == "recorded"
    latest = demo.state()["workspace"]["deliveries"][entry["delivery_id"]]
    assert latest["state"] == "held_for_recovery"


@pytest.fixture
def message_fixture(tmp_path):
    from test_workspace_operator_runtime import OperatorRuntimeHarness
    from _workspace_commands import OperatorDraftAdapter, OperatorMessageAdapter
    harness = OperatorRuntimeHarness(tmp_path / "message-runs")
    draft_scope = {"workspace_id": harness.demo.workspace_id, "run_id": harness.demo.run_id,
                   "operator_id": "local-operator",
                   "targets": [{"target": "operator-target", "task_id": "main-1"}]}
    harness.demo.permit({"operation": "install_operator_draft_retention", "scope": draft_scope})
    draft_handle = harness.demo.runtime.install_operator_draft_retention(
        draft_scope, authorize_retention=lambda _source, _workspace: harness.allowed,
        retention_secret=b"r" * 32, key_epoch="epoch-1")
    messages = OperatorMessageAdapter(harness.demo.runtime, harness.handle)
    drafts = OperatorDraftAdapter(harness.demo.runtime, draft_handle)
    surface = ui.WorkspaceSurface(harness.demo.runtime._coordinator, harness.demo.workspace_id,
                                  operator_messages=messages, operator_drafts=drafts,
                                  presentation_key=b"m" * 32)
    surface.start()
    try:
        yield harness, surface
    finally:
        surface.stop()
        harness.demo.cleanup()


def operator_body(surface, token, action="cancel_queued_context", key="c" * 32):
    status, _, raw = request(surface, token=token)
    assert status == 200
    view = json.loads(raw)
    capabilities = view["operator_commands"]
    assert capabilities["available"]
    target = next(target for target, actions in capabilities["actions_by_delivery"].items() if action in actions)
    return {"operation_key": key, "action": action, "target": target}


def message_body(surface, token, key="a" * 32, text="context"):
    status, _, raw = request(surface, token=token)
    assert status == 200
    view = json.loads(raw)
    assert view["operator_messages"]["available"]
    assert view["operator_drafts"]["available"]
    target = view["operator_messages"]["targets"][0]["id"]
    return {"operation_key": key, "target": target, "text": text}, view


def test_message_only_surface_advertises_operator_scope_and_key_endpoint(message_fixture):
    harness, surface = message_fixture
    token, _ = login(surface)
    body, view = message_body(surface, token)
    assert json.loads(request(surface, token=token)[2])["operator_drafts"]["operator_scope"]
    status, _, raw = request(surface, "POST", "/api/messages/draft-key", token=token,
                             payload={"target": body["target"], "operation_key": body["operation_key"], "state": "unsent"})
    assert status == 200
    result = json.loads(raw)
    assert set(result) == {"key_b64", "operator_scope", "key_epoch", "max_text_bytes", "max_record_bytes"}
    assert result["operator_scope"] == view["operator_drafts"]["operator_scope"]
    assert "local-operator" not in raw.decode("utf-8")


def test_message_changed_payload_is_409_without_second_admission(message_fixture):
    harness, surface = message_fixture
    token, _ = login(surface)
    body, _ = message_body(surface, token, text="first")
    harness.send(request={"operation_key": body["operation_key"], "target": "operator-target", "text": "first"})
    before = harness.demo.state()
    changed = body | {"text": "changed"}
    harness.permit({"operation_key": body["operation_key"], "target": "operator-target", "text": "changed"}, False)
    status, _, raw = request(surface, "POST", "/api/messages", token=token, payload=changed)
    assert status == 409 and json.loads(raw)["error"] == "message_request_conflict"
    after = harness.demo.state()
    assert len(after["workspace"]["messages"]) == len(before["workspace"]["messages"])
    assert len(after["workspace"]["deliveries"]) == len(before["workspace"]["deliveries"])


def test_public_http_send_owner_refresh_command_lookup_and_reopen(tmp_path):
    """Exercise the supported browser/control-plane journey end to end.

    The browser receives only opaque delivery handles.  The owner provisions a
    newer private policy file and uses the separate control token to refresh it;
    the browser then uses its authenticated command surface and can reconcile
    the same command after a fresh restart.
    """
    from _workspace_entry import WorkspaceHost, _canonical, _secure_file

    runs = tmp_path / "runs"
    def free_port():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        return port

    seed = WorkspaceHost(str(runs), "run-a", free_port(), mode="demo-create")
    seed.start()
    state, torn = seed.runtime._coordinator._load()
    assert not torn
    original_delivery = next(iter(state["workspace"]["deliveries"]))
    seed.stop()

    policy = tmp_path / "policy.json"
    token_file = tmp_path / "control-token"

    def write_policy(generation, delivery_ids):
        policy.write_bytes(_canonical({
            "schema": "summon.workspace-command-policy/v1",
            "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
            "expires_at_ms": int(time.time() * 1000) + 600000,
            "max_active_deliveries": 2,
            "targets": [{"delivery_id": item, "actions": ["cancel_queued_context"]}
                         for item in delivery_ids],
        }) + b"\n")
        _secure_file(str(policy))

    write_policy(1, [original_delivery])
    host = WorkspaceHost(str(runs), "run-a", free_port(), mode="open",
                         command_policy_file=str(policy),
                         command_refresh_token_file=str(token_file))
    host.start()
    command = {"operation_key": "e" * 32, "action": "cancel_queued_context"}
    try:
        token, _ = login(host.surface)
        message, _view = message_body(host.surface, token, key="f" * 32,
                                      text="public context")
        assert request(host.surface, "POST", "/api/messages", token=token,
                       payload=message)[0] == 200
        state, torn = host.runtime._coordinator._load()
        assert not torn
        new_delivery = next(item for item in state["workspace"]["deliveries"]
                             if item != original_delivery)
        control = token_file.read_text(encoding="utf-8")
        provision = request(
            host.surface, "POST", "/api/commands/provision-message",
            headers={"X-Summon-Command-Refresh": control},
            payload={"request_id": "a" * 32, "operation_key": "f" * 32,
                     "actions": ["cancel_queued_context"],
                     "workspace_id": "workspace", "run_id": "run-a"})
        assert provision[0] == 200
        refreshed = json.loads(provision[2])
        assert refreshed["generation"] == 2
        assert new_delivery not in json.dumps(refreshed)
        refresh_status = request(
            host.surface, "GET", "/api/commands/refresh/status?request_id=" + "a" * 32,
            headers={"X-Summon-Command-Refresh": control})
        assert refresh_status[0] == 200 and json.loads(refresh_status[2])["status"] == "refreshed"
        collision = request(
            host.surface, "POST", "/api/commands/provision-message",
            headers={"X-Summon-Command-Refresh": control},
            payload={"request_id": "a" * 32, "operation_key": "f" * 32,
                     "actions": ["cancel_queued_context", "dispose_held_context"],
                     "workspace_id": "workspace", "run_id": "run-a"})
        assert collision[0] == 409
        assert json.loads(collision[2])["error"] == "command_refresh_request_conflict"
        view_after = json.loads(request(host.surface, token=token)[2])
        target = next(target for target, actions
                      in view_after["operator_commands"]["actions_by_delivery"].items()
                      if "cancel_queued_context" in actions)
        command["target"] = target
        result = request(host.surface, "POST", "/api/commands", token=token,
                         payload=command)
        assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"
    finally:
        host.stop()

    reopened_token_file = tmp_path / "control-token-reopened"
    reopened = WorkspaceHost(str(runs), "run-a", free_port(), mode="open",
                             command_policy_file=str(policy),
                             command_refresh_token_file=str(reopened_token_file))
    reopened.start()
    try:
        fresh, _ = login(reopened.surface)
        lookup = request(reopened.surface, "POST", "/api/commands/lookup",
                         token=fresh, payload=command)
        assert lookup[0] == 200 and json.loads(lookup[2])["status"] == "recorded"
    finally:
        reopened.stop()


def test_message_and_draft_key_mid_request_session_revocation_is_401(message_fixture, monkeypatch):
    harness, surface = message_fixture
    token, _ = login(surface)
    body, _ = message_body(surface, token)
    calls = {"count": 0}
    def revoked(_token):
        calls["count"] += 1
        return calls["count"] < 1
    monkeypatch.setattr(surface, "_session_allows", revoked)
    status, _, raw = request(surface, "POST", "/api/messages", token=token, payload=body)
    assert status == 401 and json.loads(raw)["error"] == "session_expired"

    calls["count"] = 0
    status, _, raw = request(surface, "POST", "/api/messages/draft-key", token=token,
                             payload={"target": body["target"], "operation_key": body["operation_key"], "state": "unsent"})
    assert status == 401 and json.loads(raw)["error"] == "session_expired"


def test_real_http_operator_cancel_and_same_key_lost_response_recovery(operator_fixture, monkeypatch):
    demo, entry, _, surface, _ = operator_fixture
    token, code = login(surface)
    body = operator_body(surface, token)
    before = demo.state()
    original = ui._Handler._send
    observed = []
    def lose_response(handler, code, value, **kwargs):
        if handler.path == "/api/commands" and value.get("status") == "recorded":
            observed.append(value)
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return
        return original(handler, code, value, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(ui._Handler, "_send", lose_response)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            request(surface, "POST", "/api/commands", token=token, payload=body)
    assert len(observed) == 1 and observed[0]["status"] == "recorded"
    settled = demo.state()
    journal = {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")}
    # Actual lost terminal HTTP response; the same key acquires a new fenced
    # generation to reconcile, without adding another event or changing tasks.
    status, _, recovered = request(surface, "POST", "/api/commands/lookup", token=token, payload=body)
    assert status == 200 and json.loads(recovered) == observed[0]
    assert demo.state() == settled
    assert {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")} == journal
    after = demo.state()
    assert after["workspace"]["deliveries"][entry["delivery_id"]]["state"] == "cancelled"
    assert all(after[name] == before[name] for name in ("tasks", "workers", "claims"))
    no_material(recovered, (token, code, entry["delivery_id"], demo.workspace_id, "request_sha256"))
    status, _, exported = request(surface, target="/api/export", token=token)
    assert status == 200 and json.loads(exported)["operator_commands"] == {
        "available": False, "actions_by_delivery": {}, "reason": "read_only_export"}


def test_real_http_operator_scope_and_boundary_refusals_do_not_mutate(operator_fixture):
    demo, _, authority, surface, _ = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    before = files(surface)
    for kwargs, expected in [({"token": None}, 401),
            ({"token": token, "headers": {"Origin": "http://foreign.invalid"}}, 403),
            ({"token": token, "payload": dict(body, certainty={})}, 400),
            ({"token": token, "payload": dict(body, target="delivery_" + "x" * 32)}, 400)]:
        options = {"token": token, "payload": body, **kwargs}
        status, _, _ = request(surface, "POST", "/api/commands", **options)
        assert status == expected
        assert files(surface) == before
    authority["allowed"] = False
    status, _, _ = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert status == 403 and files(surface) == before
    status, _, raw = request(surface, token=token)
    assert status == 200 and not json.loads(raw)["operator_commands"]["available"]


def test_real_http_operator_restart_reinstalls_authority_but_not_old_bearer(operator_fixture):
    demo, _, _, surface, start = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    assert request(surface, "POST", "/api/commands", token=token, payload=body)[0] == 200
    same_port = urlsplit(surface.url).port
    original_url = surface.url
    surface.stop()
    demo.reopen()
    resumed = start(port=same_port)
    assert resumed.url == original_url
    assert request(resumed, token=token)[0] == 401
    fresh, _ = login(resumed)
    assert json.loads(request(resumed, "POST", "/api/commands/lookup", token=fresh, payload=body)[2])["status"] == "recorded"
    readonly = start(commands=False)
    readtoken, _ = login(readonly)
    assert request(readonly, "POST", "/api/commands/lookup", token=readtoken, payload=body)[0] == 405
    rotated = start(key=b"t" * 32)
    rotatedtoken, _ = login(rotated)
    result = request(rotated, "POST", "/api/commands/lookup", token=rotatedtoken, payload=body)
    assert result[0] == 400 and json.loads(result[2])["error"] == "command_target_refused"


def test_real_http_operator_evidence_only_restart_is_pending_then_same_key_finishes(operator_fixture, monkeypatch):
    demo, _, _, surface, start = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    original = demo.runtime._record_operator_phase
    def interrupt(handle, raw, plan, phase, **kwargs):
        if phase == "transition":
            raise OSError("synthetic interruption")
        return original(handle, raw, plan, phase, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(demo.runtime, "_record_operator_phase", interrupt)
        result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "uncertain"
    surface.stop()
    demo.reopen()
    resumed = start()
    fresh, _ = login(resumed)
    result = request(resumed, "POST", "/api/commands/lookup", token=fresh, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "pending"
    result = request(resumed, "POST", "/api/commands", token=fresh, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"


def test_real_http_held_disposition_retains_unknown_effects(operator_fixture):
    demo, entry, _, surface, _ = operator_fixture
    unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
    proof = demo.evidence(demo.source({"kind": "fixture-context-hold", "delivery": entry["delivery_id"],
                                      "conservative_effect_observation": unknown}), "main-1", "ui-fixture-hold",
        "observation", entry["delivery_id"], "held_for_recovery", "hold_observation")
    delivery = copy.deepcopy(demo.state()["workspace"]["deliveries"][entry["delivery_id"]])
    delivery.update(state="held_for_recovery", reason="recipient_drift", certainty=unknown)
    demo.record("workspace_delivery_advanced", {"delivery": delivery, "evidence": {"hold_observation": proof},
                "supported_ack_levels": []}, "ui-fixture-hold")
    token, _ = login(surface)
    body = operator_body(surface, token, "dispose_held_context")
    before = demo.state()
    result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"
    after = demo.state()
    assert after["workspace"]["deliveries"][entry["delivery_id"]]["certainty"] == unknown
    assert after["workspace"]["deliveries"][entry["delivery_id"]]["state"] == "dead_lettered"
    assert after["tasks"] == before["tasks"]


@pytest.mark.parametrize("revoke", [False, True], ids=['p001_case_001', 'p001_case_002'])
def test_operator_body_read_cannot_outlive_session_authority(operator_fixture, monkeypatch, revoke):
    _, _, _, surface, _ = operator_fixture
    token, _ = login(surface)
    payload = json.dumps(operator_body(surface, token)).encode()
    before = files(surface)
    entered = threading.Event()
    original = ui._Handler._body
    def observed_read(handler):
        entered.set()
        return original(handler)
    monkeypatch.setattr(ui._Handler, "_body", observed_read)
    address = urlsplit(surface.url)
    with socket.create_connection((address.hostname, address.port), timeout=5) as connection:
        header = (f"POST /api/commands HTTP/1.1\r\nHost: {address.netloc}\r\nOrigin: {surface.url.rstrip('/')}\r\n"
                  f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n")
        connection.sendall(header.encode() + payload[:1])
        assert entered.wait(2), "HTTP body reader was not reached"
        if revoke:
            surface.issue_bootstrap()
        else:
            with surface._lock:
                surface._session_deadline = time.monotonic() - 1
        connection.sendall(payload[1:])
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 401
        response.read()
    assert files(surface) == before


def test_real_http_full_line_failed_fsync_stays_uncertain_until_restarted_lookup(operator_fixture, monkeypatch):
    import _swarm_coordinator as base
    import _rundir
    demo, entry, _, surface, start = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    original = base.journal_append_encoded
    observed = []
    def fail_sync(run_dir, raw, owner, **kwargs):
        event = kwargs.get("expected_record", {}).get("workspace_event", {})
        if event.get("operation_key") == "operator-transition-" + body["operation_key"]:
            observed.append(True)
            with monkeypatch.context() as patcher:
                patcher.setattr(_rundir.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("synthetic sync uncertainty")))
                return original(run_dir, raw, owner, **kwargs)
        return original(run_dir, raw, owner, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(base, "journal_append_encoded", fail_sync)
        result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert observed == [True]
    assert result[0] == 200 and json.loads(result[2])["status"] == "uncertain"
    assert demo.state()["workspace"]["deliveries"][entry["delivery_id"]]["state"] == "cancelled"
    journal = {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")}
    surface.stop()
    demo.reopen()
    resumed = start()
    fresh, _ = login(resumed)
    result = request(resumed, "POST", "/api/commands/lookup", token=fresh, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"
    assert {name: raw for name, raw in files(resumed).items() if name.endswith(".jsonl")} == journal


def test_operator_session_revoked_during_final_sync_prevents_event_admission(operator_fixture, monkeypatch):
    demo, _, _, surface, _ = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    before = demo.state()
    journal = {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")}
    original_phase = demo.runtime._record_operator_phase
    original_sync = demo.runtime._coordinator._sync_prefix
    syncs = []
    def revoke(owner, raw):
        result = original_sync(owner, raw)
        syncs.append(True)
        if len(syncs) == 2:
            surface.issue_bootstrap()
        return result
    def phase(handle, raw, plan, name, **kwargs):
        with monkeypatch.context() as patcher:
            patcher.setattr(demo.runtime._coordinator, "_sync_prefix", revoke)
            return original_phase(handle, raw, plan, name, **kwargs)
    monkeypatch.setattr(demo.runtime, "_record_operator_phase", phase)
    result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 401 and len(syncs) == 2
    assert demo.state() == before
    assert {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")} == journal


def test_operator_session_revoked_after_actual_append_reconciles_with_fresh_session(operator_fixture, monkeypatch):
    import _swarm_coordinator as base
    demo, entry, _, surface, _ = operator_fixture
    token, _ = login(surface)
    body = operator_body(surface, token)
    original = base.journal_append_encoded
    def revoke(run_dir, raw, owner, **kwargs):
        result = original(run_dir, raw, owner, **kwargs)
        event = kwargs.get("expected_record", {}).get("workspace_event", {})
        if event.get("operation_key") == "operator-transition-" + body["operation_key"]:
            surface.issue_bootstrap()
        return result
    with monkeypatch.context() as patcher:
        patcher.setattr(base, "journal_append_encoded", revoke)
        result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 401
    assert demo.state()["workspace"]["deliveries"][entry["delivery_id"]]["state"] == "cancelled"
    fresh, _ = login(surface)
    result = request(surface, "POST", "/api/commands/lookup", token=fresh, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"


def test_explicit_loopback_port_refuses_occupied_address_without_fallback(surface):
    occupied = ui.WorkspaceSurface(surface.coordinator, surface.workspace_id, port=urlsplit(surface.url).port)
    with pytest.raises(ui.WorkspaceUIError, match="loopback_port_unavailable"):
        occupied.start()
    assert occupied._server is None and occupied.bootstrap_code is None
    for port in (-1, 65536, True, "1234"):
        with pytest.raises(ui.WorkspaceUIError, match="invalid_loopback_port"):
            ui.WorkspaceSurface(surface.coordinator, surface.workspace_id, port=port)


def test_operator_unknown_runtime_error_is_private_and_new_key_cannot_repeat_terminal_action(operator_fixture, monkeypatch):
    from _workspace_runtime import WorkspaceRuntimeError
    _, _, _, surface, _ = operator_fixture
    token, code = login(surface)
    body = operator_body(surface, token)
    runtime = surface.operator_commands._runtime
    def unknown(*_args, **_kwargs):
        raise WorkspaceRuntimeError("SYNTHETIC-PRIVATE-ERROR")
    with monkeypatch.context() as patcher:
        patcher.setattr(runtime, "execute_operator_command", unknown)
        result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 503 and json.loads(result[2])["error"] == "command_outcome_uncertain"
    no_material(result[2], (token, code, "SYNTHETIC-PRIVATE-ERROR"))
    result = request(surface, "POST", "/api/commands", token=token, payload=body)
    assert result[0] == 200 and json.loads(result[2])["status"] == "recorded"
    before = {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")}
    result = request(surface, "POST", "/api/commands", token=token,
                     payload=dict(body, operation_key="d" * 32))
    assert result[0] == 409 and json.loads(result[2])["error"] == "command_request_conflict"
    assert {name: raw for name, raw in files(surface).items() if name.endswith(".jsonl")} == before


def test_actual_authenticate_read_export_and_static_page_do_not_mutate(surface, capsys):
    before = files(surface)
    token, code = login(surface)
    for endpoint in ("/", "/api/view", "/api/export"):
        status, headers, body = request(surface, target=endpoint, token=token)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in headers
        no_material(body, (token, code, "a" * 64, "b" * 64, "scope-proof"))
        if endpoint != "/":
            value = json.loads(body)
            assert value["mode"]["mode"] == "passive" and len(value["tasks"]) == 3
            assert value["timeline"]["history_complete"]
            assert not any(control["available"] for control in value["controls"])
            assert value["goal"]["text_available"] == (endpoint == "/api/view")
            assert value["closure"]["closed"] is False
    assert files(surface) == before
    captured = capsys.readouterr()
    no_material((captured.out + captured.err).encode(), (token, code))


def test_bootstrap_is_single_use_has_no_discovery_endpoint_and_no_url_token(surface):
    before = files(surface)
    token, code = login(surface)
    status, _headers, body = request(surface, "POST", "/api/bootstrap", payload={"code": code})
    assert status == 401
    for target in ("/api/bootstrap", "/api/discovery", "/?code=synthetic", "/api/view?token=synthetic"):
        status, _headers, body = request(surface, target=target, token=token)
        assert status in {400, 404}
        no_material(body, (token, code))
    assert files(surface) == before


@pytest.mark.parametrize("headers", [{"Host": "localhost:1"}, {"Host": "external.invalid"},
                                      {"Origin": "http://external.invalid"}, {"Origin": "null"}], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_exact_host_and_origin_refuse_authenticated_reads(surface, headers):
    token, _code = login(surface)
    before = files(surface)
    status, _headers, _body = request(surface, token=token, headers=headers)
    assert status == 403
    assert files(surface) == before


def test_bootstrap_wrong_origin_cannot_consume_code(surface):
    code = surface.bootstrap_code
    status, _headers, _body = request(surface, "POST", "/api/bootstrap", payload={"code": code},
                                     headers={"Origin": "http://external.invalid"})
    assert status == 403
    token, _ = login(surface)
    assert request(surface, token=token)[0] == 200


def test_repeated_wrong_origin_body_refusal_is_received_without_consuming_code(surface):
    code = surface.bootstrap_code
    before = files(surface)
    for _ in range(20):
        status, _, body = request(surface, "POST", "/api/bootstrap", payload={"code": code},
                                  headers={"Origin": "http://external.invalid"})
        assert status == 403
        no_material(body, (code,))
    if surface.bootstrap_code != code:
        pytest.fail("Rejected origin consumed bootstrap", pytrace=False)
    assert files(surface) == before


def test_wrong_origin_missing_declared_body_has_bounded_refusal(surface):
    parsed = urlsplit(surface.url)
    before = files(surface)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as connection:
        connection.settimeout(2)
        request_bytes = (f"POST /api/bootstrap HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                         "Origin: http://external.invalid\r\nContent-Type: application/json\r\n"
                         "Content-Length: 4097\r\n\r\nx").encode("ascii")
        started = time.monotonic()
        connection.sendall(request_bytes)
        received = connection.recv(4096)
        assert received.startswith(b"HTTP/1.1 403")
        assert time.monotonic() - started < 1.5
    assert files(surface) == before


@pytest.mark.parametrize("token", [None, "forged", "wrong.scope.session"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003'])
def test_unauthenticated_and_forged_reads_return_no_workspace_data(surface, token):
    before = files(surface)
    status, _headers, body = request(surface, token=token)
    assert status == 401
    assert set(json.loads(body)) == {"error"}
    assert files(surface) == before


def test_expiry_and_explicit_local_renewal_revoke_session(surface):
    token, _ = login(surface)
    before = files(surface)
    surface._session_deadline = 0.0  # Deterministic expired-clock boundary; no sleep.
    status, _headers, body = request(surface, token=token)
    assert status == 401 and json.loads(body)["error"] == "session_expired"
    surface.issue_bootstrap()
    fresh, _ = login(surface)
    assert request(surface, token=token)[0] == 401
    assert request(surface, token=fresh)[0] == 200
    assert files(surface) == before


def test_stop_restart_invalidates_previous_session_without_stopping_work(surface):
    token, _ = login(surface)
    before = files(surface)
    surface.stop()
    assert surface.bootstrap_code is None
    surface.start()
    assert request(surface, token=token)[0] == 401
    new_token, _ = login(surface)
    assert request(surface, token=new_token)[0] == 200
    assert files(surface) == before


def test_authenticated_identity_endpoint_binds_public_client_target(surface):
    token, _ = login(surface)
    status, headers, body = request(surface, target="/api/identity", token=token)
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {
        "schema": "summon.workspace.identity/v1",
        "workspace_id": "workspace",
        "run_id": surface.coordinator.run_id,
    }
    assert request(surface, target="/api/identity?run=unexpected", token=token)[0] == 404


@pytest.mark.parametrize("method,target", [("POST", "/api/send"), ("POST", "/api/cancel"),
    ("POST", "/api/close"), ("POST", "/api/review"), ("PUT", "/api/bootstrap"),
    ("PATCH", "/api/view"), ("DELETE", "/api/view")], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005', 'p004_case_006', 'p004_case_007'])
def test_mutation_routes_remain_unimplemented_and_nonmutating(surface, method, target):
    token, _code = login(surface)
    before = files(surface)
    status, _headers, body = request(surface, method, target, token=token, payload={"instruction": "do not execute"})
    assert status == 405
    assert json.loads(body)["error"] == "read_only_surface"
    assert files(surface) == before


@pytest.mark.parametrize("raw,headers", [(b"{}", {"Content-Type": "text/plain"}),
    (b'{"code":"x","code":"y"}', {"Content-Type": "application/json"}),
    (b"x" * (ui.MAX_BODY + 1), {"Content-Type": "application/json"}),
    (b"[1]", {"Content-Type": "application/json"})], ids=["wrong-type", "duplicate-key", "too-large", "not-object"])
def test_bootstrap_body_and_json_bounds(surface, raw, headers):
    before = files(surface)
    status, _headers, _body = request(surface, "POST", "/api/bootstrap", raw=raw, headers=headers)
    assert status == 400
    assert files(surface) == before


def test_bootstrap_guess_and_expiry_bounds_are_explicit(surface):
    for _ in range(ui.MAX_BOOTSTRAP_FAILURES):
        assert request(surface, "POST", "/api/bootstrap", payload={"code": "wrong"})[0] == 401
    assert request(surface, "POST", "/api/bootstrap", payload={"code": surface.bootstrap_code})[0] == 401
    surface.issue_bootstrap()
    surface._bootstrap_deadline = 0.0
    assert request(surface, "POST", "/api/bootstrap", payload={"code": surface.bootstrap_code})[0] == 401


def test_actual_cursor_binding_and_stale_read_refusal(surface):
    token, _ = login(surface)
    before = files(surface)
    status, _, body = request(surface, target="/api/view?limit=2", token=token)
    assert status == 200
    first = json.loads(body)
    query = urlencode({"limit": 2, "cursor": first["timeline"]["next_cursor"], "snapshot": first["snapshot"]})
    assert request(surface, target="/api/view?" + query, token=token)[0] == 200
    assert request(surface, target="/api/export?" + query, token=token)[0] == 409
    assert request(surface, target="/api/view?cursor=forged", token=token)[0] == 409
    assert request(surface, target="/api/view?task=main-2", token=token)[0] == 400
    assert files(surface) == before
    surface.coordinator.register_worker("new-worker", worker_instance_id="new-worker")
    changed = files(surface)
    assert request(surface, target="/api/view?" + query, token=token)[0] == 409
    assert files(surface) == changed


def test_actual_anchored_read_after_mutation_and_scope_refusals_do_not_write(surface):
    token, _ = login(surface)
    first = json.loads(request(surface, target="/api/view?limit=2", token=token)[2])
    last_query = urlencode({"limit": 2, "cursor": first["timeline"]["next_cursor"]})
    last = json.loads(request(surface, target="/api/view?" + last_query, token=token)[2])
    anchor = last["timeline"]["items"][0]
    surface.coordinator.register_worker("anchor-worker", worker_instance_id="anchor-worker")
    before = files(surface)
    query = urlencode({"limit": 2, "anchor": anchor["anchor"]})
    code, _, body = request(surface, target="/api/view?" + query, token=token)
    assert code == 200
    newer = json.loads(body)
    assert newer["snapshot"] != last["snapshot"]
    assert newer["timeline"]["items"][0]["id"] == anchor["id"]
    assert request(surface, target="/api/export?" + query, token=token)[0] == 409
    assert request(surface, target="/api/view?anchor=unknown", token=token)[0] == 409
    assert request(surface, target="/api/view?anchor=unknown&cursor=unknown", token=token)[0] == 400
    assert request(surface, target="/api/view?anchor=unknown&anchor=duplicate", token=token)[0] == 400
    assert request(surface, target="/api/view?" + last_query, token=token)[0] == 409
    assert files(surface) == before


def test_actual_snapshot_projects_outer_journal_recorded_times(surface):
    result = surface.snapshot()
    items = result["timeline"]["items"]
    assert items and all(item["timestamp_reason"] == "journal_recorded"
                         and type(item["occurred_at"]) is float
                         for item in items)
    assert all(item["occurred_at"] >= 0 for item in items)


@pytest.mark.parametrize("settlement", ["receipt", "unknown_disposition"], ids=['p006_case_001', 'p006_case_002'])
def test_real_journal_inbox_http_read_retains_qualification_and_unknown_after_close(tmp_path, settlement):
    """Real core journal/HTTP; the harness is a synthetic trusted host, not a live consumer."""
    from test_workspace_inbox import InboxHarness
    harness = InboxHarness(tmp_path)
    coordinator = harness.coordinator
    delivery_id = harness.send()["delivery_id"]
    offer = harness.offer(delivery_id)
    if settlement == "receipt":
        harness.receipt(delivery_id, offer["offer_id"])
    else:
        proof = harness.register("http-hold", "observation", delivery_id=delivery_id,
                                 target="held_for_recovery", role="hold_observation")
        harness.command("hold", {"hold_observation": proof}, delivery_id=delivery_id, reason="consumer_lost")
        proof = harness.register("http-disposition", "event", delivery_id=delivery_id,
                                 target="dead_lettered", role="authenticated_disposition")
        harness.command("dispose", {"authenticated_disposition": proof}, delivery_id=delivery_id,
                        reason="operator_disposition")
    for task_id in ("main-1", "main-2", "side-1"):
        state = harness.state()
        if state["tasks"][task_id]["attempts"]:
            claim = state["claims"][state["tasks"][task_id]["attempts"][-1]]
        else:
            result = coordinator.claim("worker-a", task_id, request_sha256="d" * 64)
            claim = harness.state()["claims"][result["claim_id"]]
        coordinator.complete("worker-a", task_id=task_id, claim_id=claim["claim_id"], attempt=claim["attempt"],
                             lease_generation=claim["lease_generation"], request_sha256="d" * 64,
                             envelope_sha256="e" * 64)
    # All tasks finished, but the active endpoint still owes an explicit control
    # disposition. The workspace must retain its settlement state.
    waiting = ui.collect_snapshot(coordinator, scope=ViewScope("workspace", "run", b"s" * 32, "operator"))
    assert waiting["closure"]["state"] == "settlement_required"
    assert waiting["closure"]["ready"] is False
    assert waiting["inbox"]["summary"]["active_endpoints"] == 1
    harness.retire()
    coordinator.close()
    before = harness.journals()
    with ui.WorkspaceSurface(coordinator, "workspace") as instance:
        token, code = login(instance)
        for path in ("/api/view", "/api/export"):
            status, _, body = request(instance, target=path, token=token)
            assert status == 200
            result = json.loads(body)
            assert result["closure"]["closed"] is True
            assert result["mode"]["mode"] == "passive"
            assert result["inbox"]["summary"]["total"] == 1
            card = result["inbox"]["deliveries"][0]
            if settlement == "receipt":
                assert card["receipt"] == {"level": "supervisor_context_received", "qualification": "simulated"}
            else:
                assert card["label"] == "Disposed with unknown exposure" and card["exposure"] == "unknown"
                assert result["inbox"]["summary"]["disposed_with_unknown_exposure"] == 1
            no_material(body, (token, code, "consumer-1", "receiver-1", offer["offer_id"], "a" * 64))
    assert harness.journals() == before


def test_real_snapshot_adapter_retries_one_actual_prefix_change(surface, monkeypatch):
    original = surface.coordinator.status
    calls = []
    def changed_once():
        result = original()
        calls.append(True)
        if len(calls) == 1:
            surface.coordinator.register_worker("race-worker", worker_instance_id="race-worker")
        return result
    monkeypatch.setattr(surface.coordinator, "status", changed_once)
    result = surface.snapshot()
    assert result["schema"] == "summon.workspace.view/v1" and len(calls) == 2


def test_repeated_actual_prefix_change_refuses_after_two_attempts(surface, monkeypatch):
    original = surface.coordinator.status
    calls = []
    def changing():
        result = original()
        calls.append(True)
        name = "race-" + str(len(calls))
        surface.coordinator.register_worker(name, worker_instance_id=name)
        return result
    monkeypatch.setattr(surface.coordinator, "status", changing)
    with pytest.raises(ui.WorkspaceUIError, match="snapshot_changed"):
        surface.snapshot()
    assert len(calls) == 2


def test_cross_workspace_scope_and_incomplete_status_are_refused(surface, monkeypatch):
    before = files(surface)
    with pytest.raises(ValueError, match="scope_refused"):
        ui.collect_snapshot(surface.coordinator, scope=ViewScope("other", "run", b"x" * 32))
    original = surface.coordinator.status
    def truncated():
        result = original()
        result["tasks"] = result["tasks"][:-1]
        return result
    monkeypatch.setattr(surface.coordinator, "status", truncated)
    with pytest.raises(ui.WorkspaceUIError, match="incomplete_snapshot"):
        surface.snapshot()
    assert files(surface) == before


def test_canonical_error_reasons_use_typed_http_classes():
    expected = {
        "canonical_scope_refused": 403,
        "canonical_body_scope_invalid": 403,
        "canonical_snapshot_unavailable": 503,
        "canonical_detail_unavailable": 503,
        "canonical_binding_unavailable": 503,
        "canonical_scope_empty": 503,
    }
    for reason, code in expected.items():
        sent = []
        handler = SimpleNamespace(_send=lambda status, body: sent.append((status, body)))
        ui._Handler._refused(handler, ui.CanonicalDetailError(reason))
        assert sent == [(code, {"error": reason})]

    sent = []
    handler = SimpleNamespace(_send=lambda status, body: sent.append((status, body)))
    ui._Handler._refused(handler, ui.WorkspaceDetailError("detail_scope_required"))
    assert sent == [(403, {"error": "detail_scope_required"})]


def test_client_admission_is_bounded_before_starting_request_threads(surface):
    slots = surface._server._slots
    acquired = 0
    try:
        for _ in range(ui.MAX_CLIENTS):
            assert slots.acquire(blocking=False)
            acquired += 1
        assert request(surface, target="/")[0] == 503
    finally:
        for _ in range(acquired):
            slots.release()
    assert request(surface, target="/")[0] == 200


def test_actual_idle_clients_exhaust_bounded_acceptance_without_extra_request_thread(surface):
    address = ("127.0.0.1", urlsplit(surface.url).port)
    clients = []
    try:
        for _ in range(ui.MAX_CLIENTS):
            clients.append(socket.create_connection(address, timeout=3))
        assert request(surface, target="/")[0] == 503
    finally:
        for client in clients:
            client.close()


def test_actual_incomplete_post_body_has_bounded_timeout(surface):
    parsed = urlsplit(surface.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    before = files(surface)
    started = time.monotonic()
    try:
        connection.putrequest("POST", "/api/bootstrap")
        connection.putheader("Origin", surface.url.rstrip("/"))
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "64")
        connection.endheaders(b"{")
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        assert time.monotonic() - started < 5
    finally:
        connection.close()
    assert files(surface) == before


def test_actual_slow_drip_headers_cannot_extend_total_request_lifetime(surface):
    address = ("127.0.0.1", urlsplit(surface.url).port)
    before = files(surface)
    client = socket.create_connection(address, timeout=2)
    client.settimeout(0.05)
    started = time.monotonic()
    closed = False
    try:
        client.sendall(b"GET /api/view HTTP/1.1\r\nHost: ")
        while time.monotonic() - started < ui.REQUEST_LIFETIME + 1.5:
            try:
                client.sendall(b"x")
                time.sleep(0.35)  # Always below the ordinary idle timeout.
                try:
                    if client.recv(1) == b"":
                        closed = True
                        break
                except socket.timeout:
                    pass
            except (ConnectionError, OSError):
                closed = True
                break
        assert closed, "slow input retained an admitted client past its total lifetime"
    finally:
        client.close()
    assert files(surface) == before


def test_duplicate_host_and_authorization_headers_are_refused(surface):
    token, _ = login(surface)
    parsed = urlsplit(surface.url)
    for duplicate in ("Host", "Authorization"):
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.putrequest("GET", "/api/view", skip_host=True)
            connection.putheader("Host", parsed.netloc)
            connection.putheader("Authorization", "Bearer " + token)
            connection.putheader(duplicate, parsed.netloc if duplicate == "Host" else "Bearer " + token)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == (403 if duplicate == "Host" else 401)
            no_material(response.read(), (token,))
        finally:
            connection.close()


def test_unrelated_surface_cannot_accept_this_workspace_session(surface):
    token, _ = login(surface)
    other = ui.WorkspaceSurface(surface.coordinator, "other-workspace")
    other.start()
    try:
        assert request(other, token=token)[0] == 401
        unrelated_token, _ = login(other)
        assert request(other, token=unrelated_token)[0] == 403
    finally:
        other.stop()


def run_operator_review_fixture(lifetime_seconds=1800):
    """Finite actual HTTP/runtime manual fixture, no provider/worker launch.

    renew/expire/restart/lose/stop controls belong only to this synthetic helper.
    restart reopens the canonical history with independent host installation at
    the same origin. lose drops one real recorded response, never fabricates one.
    """
    import queue
    import tempfile
    from _workspace_demo import ConductorDemo
    from _workspace_commands import OperatorCommandAdapter
    if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 1800:
        raise ValueError("bounded fixture lifetime required")
    controls = queue.Queue()
    def receive():
        for line in sys.stdin:
            controls.put(line.strip())
    with tempfile.TemporaryDirectory(prefix="summon-operator-review-") as directory:
        demo = ConductorDemo(Path(directory) / "runs")
        demo.prepare()
        grant = demo.grant("main-1", "worker-a-1")[0]
        entries = [demo.message("main-1", "worker-a-1", grant, index, "[2,3]") for index in (1, 2, 3)]
        held = entries[1]["delivery_id"]
        unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        proof = demo.evidence(demo.source({"kind": "fixture-context-hold", "delivery": held,
            "conservative_effect_observation": unknown}), "main-1", "rendered-hold", "observation",
            held, "held_for_recovery", "hold_observation")
        delivery = copy.deepcopy(demo.state()["workspace"]["deliveries"][held])
        delivery.update(state="held_for_recovery", reason="recipient_drift", certainty=unknown)
        demo.record("workspace_delivery_advanced", {"delivery": delivery, "evidence": {"hold_observation": proof},
            "supported_ack_levels": []}, "rendered-hold")
        scope = {"workspace_id": demo.workspace_id, "run_id": demo.run_id, "targets": [
            {"delivery_id": entry["delivery_id"], "actions": ["cancel_queued_context", "dispose_held_context"]}
            for entry in entries]}
        private_key = b"synthetic-rendered-operator-key---"
        def start(port=0):
            demo.permit({"operation": "install_operator_commands", "scope": scope})
            handle = demo.runtime.install_operator_commands(scope, authorize_command=lambda _source, _workspace: True)
            instance = ui.WorkspaceSurface(demo.runtime._coordinator, demo.workspace_id,
                operator_commands=OperatorCommandAdapter(demo.runtime, handle), presentation_key=private_key, port=port)
            instance.start()
            instance.bootstrap_code = "synthetic-workspace-review"
            return instance
        instance = start()
        original_send = ui._Handler._send
        lose_next = {"value": False}
        def send(handler, code, value, **kwargs):
            if lose_next["value"] and handler.path == "/api/commands" and value.get("status") == "recorded":
                lose_next["value"] = False
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
                print(json.dumps({"status": "recorded_response_dropped"}), flush=True)
                return
            return original_send(handler, code, value, **kwargs)
        ui._Handler._send = send
        threading.Thread(target=receive, daemon=True, name="synthetic-operator-control").start()
        print(json.dumps({"status": "ready", "url": instance.url, "lifetime_seconds": lifetime_seconds}), flush=True)
        deadline = time.monotonic() + lifetime_seconds
        try:
            while time.monotonic() < deadline:
                try:
                    command = controls.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if command == "stop":
                    break
                try:
                    if command == "restart":
                        port = urlsplit(instance.url).port
                        instance.stop()
                        demo.reopen()
                        instance = start(port)
                    elif command == "renew":
                        instance.issue_bootstrap()
                        instance.bootstrap_code = "synthetic-workspace-review"
                    elif command == "expire":
                        with instance._lock:
                            instance._session_deadline = 0.0
                    elif command == "lose":
                        lose_next["value"] = True
                    else:
                        print(json.dumps({"status": "control_refused"}), flush=True)
                        continue
                    print(json.dumps({"status": "ok", "control": command}), flush=True)
                except Exception:
                    print(json.dumps({"status": "control_refused"}), flush=True)
        finally:
            ui._Handler._send = original_send
            instance.stop()
            demo.cleanup()
        print(json.dumps({"status": "stopped"}), flush=True)


def run_inbox_review_fixture(lifetime_seconds=1800):
    """Explicit finite rendered fixture with real journals and synthetic core facts.

    Controls: renew, expire, stop. Fixed code is synthetic manual-review data,
    never production authentication qualification. No values or paths printed.
    """
    import queue
    import tempfile
    import threading
    from test_workspace_inbox import InboxHarness
    from test_workspace_worker_send import request as send_request
    if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 1800:
        raise ValueError("bounded fixture lifetime required")
    controls = queue.Queue()
    def receive():
        for line in sys.stdin:
            controls.put(line.strip())
    with tempfile.TemporaryDirectory(prefix="summon-inbox-review-") as directory:
        harness = InboxHarness(Path(directory))
        first = harness.send()["delivery_id"]
        harness.offer(first)
        proof = harness.register("review-hold", "observation", delivery_id=first,
                                 target="held_for_recovery", role="hold_observation")
        harness.command("hold", {"hold_observation": proof}, delivery_id=first, reason="consumer_lost")
        proof = harness.register("review-disposition", "event", delivery_id=first,
                                 target="dead_lettered", role="authenticated_disposition")
        harness.command("dispose", {"authenticated_disposition": proof}, delivery_id=first, reason="operator_disposition")
        second = harness.send(send_request() | {"operation_key": "second-review-message"})["delivery_id"]
        offer = harness.offer(second)
        harness.receipt(second, offer["offer_id"])
        harness.retire()
        for task_id in ("main-1", "main-2", "side-1"):
            state = harness.state()
            if not state["tasks"][task_id]["attempts"]:
                harness.coordinator.claim("worker-a", task_id, request_sha256="d" * 64)
                state = harness.state()
            claim = state["claims"][state["tasks"][task_id]["attempts"][-1]]
            harness.coordinator.complete("worker-a", task_id=task_id, claim_id=claim["claim_id"],
                                         attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                                         request_sha256="d" * 64, envelope_sha256="e" * 64)
        harness.coordinator.close()
        with ui.WorkspaceSurface(harness.coordinator, "workspace") as instance:
            instance.bootstrap_code = "synthetic-workspace-review"
            threading.Thread(target=receive, daemon=True, name="synthetic-inbox-control").start()
            print(json.dumps({"status": "ready", "url": instance.url, "lifetime_seconds": lifetime_seconds}), flush=True)
            deadline = time.monotonic() + lifetime_seconds
            while time.monotonic() < deadline:
                try:
                    command = controls.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if command == "stop":
                    break
                if command == "renew":
                    instance.issue_bootstrap()
                    instance.bootstrap_code = "synthetic-workspace-review"
                elif command == "expire":
                    instance._session_deadline = 0.0
                else:
                    print(json.dumps({"status": "control_refused"}), flush=True)
                    continue
                print(json.dumps({"status": "ok", "control": command}), flush=True)
            print(json.dumps({"status": "stopped"}), flush=True)


def run_review_fixture(lifetime_seconds=1800, *, reuse_directory=None):
    """Explicit manual rendered fixture; synthetic fixed code is NOT auth QA.

    Stdin commands: update, renew, expire, stop. Output contains only control
    acknowledgements and the loopback URL. No bootstrap/session token, receipt,
    source bytes or temporary path is printed. Nothing runs on module import.
    """
    import hashlib
    import queue
    import tempfile
    import threading
    from contextlib import nullcontext
    from _workspace_runtime import goal_plan
    from test_workspace_protocol import goal, lane

    if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 1800:
        raise ValueError("bounded fixture lifetime required")
    definition = goal()
    definition["objective"] = "Keep the main goal visible while a side investigation runs. Review the evidence, then select the next useful task."
    lanes = []
    for task_id, role, outcome in (
        ("main-1", "main", "Complete the first goal milestone"),
        ("side-1", "investigation", "Investigate one bounded side issue"),
        ("main-2", "main", "Integrate findings and continue the goal"),
    ):
        item = lane()
        item.update(task_id=task_id, lane_id=task_id, role=role, outcome=outcome)
        lanes.append(item)
    prepared = goal_plan(definition, lanes, operation_prefix="definition")
    sources = {}
    def authorize(event, state):
        if event == {"operation": "prepare", "plan": prepared} and state is None:
            return True
        if event in prepared["events"]:
            return True
        payload = event.get("payload", {})
        reference = payload.get("reference", {})
        raw = sources.get(reference.get("id"))
        return (event.get("event") == "workspace_evidence_registered" and event.get("workspace_id") == "workspace"
                and event.get("run_id") == "run" and payload.get("task_id") == "main-2"
                and payload.get("category") == "event" and type(raw) is bytes
                and reference.get("sha256") == hashlib.sha256(raw).hexdigest())
    def resolve(payload):
        return sources[payload["reference"]["id"]]
    commands = queue.Queue()
    def receive():
        for line in sys.stdin:
            commands.put(line.strip())
    # Explicit host-only fixture reuse preserves canonical journal history; it
    # never accepts a browser path or substitutes a fabricated presentation.
    context = (tempfile.TemporaryDirectory(prefix="summon-ui-review-")
               if reuse_directory is None else nullcontext(reuse_directory))
    with context as directory:
        constructor = WorkspaceRuntime.new if reuse_directory is None else WorkspaceRuntime
        runtime = constructor(Path(directory) / "runs", "run", "workspace",
            authorize=authorize, resolve_evidence=resolve, clock=lambda: 2000.0)
        if reuse_directory is None:
            runtime.prepare(prepared, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64)
            count = 0
        else:
            state, torn = runtime._coordinator._load()
            if torn:
                raise ValueError("reusable fixture history is torn")
            count = max((int(key.removeprefix("synthetic-observation-"))
                         for key in state["workspace"]["evidence"]
                         if key.startswith("synthetic-observation-")), default=0)
        def update():
            nonlocal count
            count += 1
            identifier = "synthetic-observation-" + str(count)
            raw = json.dumps({"kind": "synthetic_review_fixture", "sequence": count}, sort_keys=True).encode()
            sources[identifier] = raw
            state = runtime._coordinator._load()[0]["workspace"]
            event = {"event": "workspace_evidence_registered", "protocol": "summon.workspace/v1",
                "workspace_id": "workspace", "run_id": "run", "operation_key": "review-event-" + str(count),
                "expected_revision": state["revision"], "payload": {
                    "reference": {"id": identifier, "sha256": hashlib.sha256(raw).hexdigest()},
                    "task_id": "main-2", "category": "event"}}
            runtime.record_event(event)
        coordinator = runtime._coordinator
        if reuse_directory is None:
            for _ in range(205):
                update()
            coordinator.register_worker("review-worker", worker_instance_id="review-worker")
            claim = coordinator.claim("review-worker", "main-1", request_sha256="d" * 64)
            coordinator.complete("review-worker", task_id="main-1", claim_id=claim["claim_id"], attempt=claim["attempt"],
                lease_generation=claim["lease_generation"], request_sha256="d" * 64,
                envelope_sha256="e" * 64)
            coordinator.claim("review-worker", "side-1", request_sha256="d" * 64)
        with ui.WorkspaceSurface(coordinator, "workspace") as instance:
            instance.bootstrap_code = "synthetic-workspace-review"
            threading.Thread(target=receive, daemon=True, name="synthetic-review-control").start()
            print(json.dumps({"status": "ready", "url": instance.url, "lifetime_seconds": lifetime_seconds}), flush=True)
            deadline = time.monotonic() + lifetime_seconds
            while time.monotonic() < deadline:
                try:
                    command = commands.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if command == "stop":
                    break
                try:
                    if command == "update":
                        update()
                    elif command == "renew":
                        instance.issue_bootstrap()
                        instance.bootstrap_code = "synthetic-workspace-review"
                    elif command == "expire":
                        instance._session_deadline = 0.0
                    else:
                        print(json.dumps({"status": "unknown_control"}), flush=True)
                        continue
                    print(json.dumps({"status": "ok", "control": command}), flush=True)
                except Exception:
                    print(json.dumps({"status": "control_refused"}), flush=True)
            print(json.dumps({"status": "stopped"}), flush=True)
