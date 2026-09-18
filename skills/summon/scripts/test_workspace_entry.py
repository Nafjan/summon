import json
import copy
import socket
import subprocess
import sys
import concurrent.futures
import hashlib
import os
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import _cli
import _context_policy
import _workspace_entry
import _swarm_coordinator as swarm
from _workspace_plan import ECONOMICS_FEATURE, ECONOMICS_SCHEMA, PLAN_V2_SCHEMA
from _spawn import run_flags
from _workspace_entry import (WorkspaceEntryError, WorkspaceHost, _content_resolver,
                               _run_foreground, create_workspace_from_plan, run_command)
from _workspace_runtime import WorkspaceRuntime
from _workspace_view import ViewScope, _opaque


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _args(**values):
    defaults = {
        "workspace_action": None,
        "workspace_run_id": None,
        "workspace_runs_root": None,
        "workspace_port": None,
        "workspace_objective": None,
        "workspace_plan_file": None,
        "workspace_command_policy": None,
        "workspace_host_url": None,
        "workspace_control_token_file": None,
        "workspace_request_id": None,
        "workspace_token_file": None,
        "workspace_request_file": None,
        "workspace_request_kind": None,
        "workspace_lookup": False,
        "workspace_operation_key": None,
        "workspace_actions": None,
        "workspace_interactive": False,
        "json": False,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_workspace_subcommand_boundary_and_demo_translation():
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "open", "run-a", "--runs-root", "C:/runs", "--port", "43121"])
    assert error is None
    assert rewritten == [
        "--workspace-action", "open", "--workspace-run-id", "run-a",
        "--workspace-runs-root", "C:/runs", "--workspace-port", "43121",
    ]


def test_workspace_provision_message_public_spelling_reaches_parser_and_dispatch(
        monkeypatch, capsys):
    operation_key = "a" * 32
    argv = [
        "workspace", "provision-message", "run-a", "--url", "http://127.0.0.1:43121/",
        "--control-token-file", "C:/private/control.token", "--operation-key", operation_key,
        "--actions", "cancel_queued_context", "--lookup",
    ]
    rewritten, mode = _cli.rewrite_subcommand(argv)
    assert mode is None
    parsed = _cli.build_parser("3.5.0", 1).parse_args(rewritten)
    assert parsed.workspace_action == "provision-message"
    assert parsed.workspace_run_id == "run-a"
    assert parsed.workspace_operation_key == operation_key
    assert parsed.workspace_actions == "cancel_queued_context"
    assert parsed.workspace_lookup is True

    called = {}

    def provision(**kwargs):
        called.update(kwargs)
        return {"status": "reconciled", "provider_calls": 0}

    monkeypatch.setattr(_workspace_entry, "workspace_provision_message", provision)
    result = run_command(parsed)
    assert result == 0
    assert called == {
        "url": "http://127.0.0.1:43121/",
        "control_token_file": "C:/private/control.token",
        "operation_key": operation_key,
        "actions": ("cancel_queued_context",),
        "request_id": operation_key,
        "expected_workspace_id": "workspace",
        "expected_run_id": "run-a",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "reconciled"


def _versioned_plan(task_count=1):
    tasks = []
    targets = []
    for index in range(task_count):
        task_id = f"task-{index:02d}"
        tasks.append({
            "task_id": task_id,
            "role": "main" if index == 0 else "review",
            "outcome": "Return a bounded provider-free result.",
            "scope": "Inspect only the staged workspace state.",
            "depends_on": [] if index == 0 else ["task-00"],
            "criterion_ids": ["evidence"],
            "return_condition": "Return a complete bounded report.",
            "escalation_trigger": "A source or authority check fails.",
            "limits": {"max_duration_ms": 300000, "max_attempts": 1, "max_context_bytes": 4096},
        })
        targets.append({"target": f"target-{index:02d}", "task_id": task_id})
    return {
        "schema": "summon.workspace.plan/v1",
        "goal": {
            "goal_id": "plan-goal",
            "objective": "Create a bounded provider-free task workspace.",
            "criteria": [{"criterion_id": "evidence", "description": "Evidence exists.",
                           "evidence_requirement": "Inspect the durable source records."}],
            "constraints": ["No providers."],
            "active_priority": "task-00",
            "unresolved_decisions": [],
        },
        "tasks": tasks,
        "operator_message_targets": targets,
    }


def test_v2_plan_uses_the_economics_runtime_without_downgrading_to_v1(tmp_path):
    value = _versioned_plan(1)
    value["schema"] = PLAN_V2_SCHEMA
    value["tasks"][0]["context_policy"] = _context_policy.make(
        "off", policy_id="task-context")
    value["economics"] = {
        "schema": ECONOMICS_SCHEMA, "feature": ECONOMICS_FEATURE,
        "enabled": True, "max_records": 8, "max_settlement_bytes": 4096,
    }
    plan_file = tmp_path / "plan-v2.json"
    plan_file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    result = create_workspace_from_plan(str(tmp_path / "runs"), "plan-v2", str(plan_file))
    assert result["status"] == "created"
    assert result["provider_calls"] == 0
    state, torn = WorkspaceRuntime(
        str(tmp_path / "runs"), "plan-v2", "workspace",
        authorize=lambda _command, _current: True,
        resolve_evidence=lambda _payload: b"")._coordinator._load()
    assert not torn
    assert state["economics"] == value["economics"]
    host = WorkspaceHost(str(tmp_path / "runs"), "plan-v2", _free_port(), mode="open")
    ready = host.start()
    try:
        assert ready["qualification"] == "provider_inert_reopen"
        config = json.loads((Path(host.runtime._coordinator.run_dir) / "workspace-host.json").read_text())
        assert config["plan_schema"] == PLAN_V2_SCHEMA
    finally:
        host.stop()


def test_versioned_plan_create_is_atomic_and_reopens(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    result = create_workspace_from_plan(str(runs), "plan-run", str(plan_file))
    assert result == {
        "mode": "create", "mutation": "created", "operator_target_count": 1, "preview": True,
             "provider_calls": 0, "run_id": "plan-run", "status": "created",
             "task_count": 1, "workers_started": 0, "workspace_id": "workspace",
    }
    with pytest.raises(WorkspaceEntryError, match="run_exists"):
        create_workspace_from_plan(str(runs), "plan-run", str(plan_file))
    host = WorkspaceHost(str(runs), "plan-run", _free_port(), mode="open")
    ready = host.start()
    try:
        assert ready["preview"] is True
        assert ready["qualification"] == "provider_inert_reopen"
        state, torn = host.runtime._coordinator._load()
        assert not torn
        assert state["workers"] == {}, "plan destinations must not become worker authority"
        assert len(state["project_root_sha256"]) == len(state["roster_definition_sha256"]) == 64
        assert state["project_root_sha256"] != hashlib.sha256(plan_file.read_bytes()).hexdigest()
        assert state["roster_definition_sha256"] != hashlib.sha256(plan_file.read_bytes()).hexdigest()
        request = {"operation_key": "c" * 32, "target": "target-00", "text": "hello plan"}
        queued = host.runtime.send_operator_message(host._message_handle, request)
        assert queued["status"] == "queued"
        recipient = host.runtime._operator_send_handles[host._message_handle]["targets"]["target-00"]["recipient_instance_id"]
        with pytest.raises(swarm.SwarmCoordinatorError, match="worker must register"):
            host.runtime._coordinator.claim(recipient, "task-00",
                                             request_sha256=state["tasks"]["task-00"]["request_sha256"])
        with pytest.raises(swarm.SwarmCoordinatorError, match="worker must register"):
            host.runtime._coordinator.send_message(recipient, "coordinator", "not authority")
        assert host.runtime._coordinator._load()[0]["workers"] == {}
    finally:
        host.stop()


@pytest.mark.parametrize(
    "operation",
    ["claim", "renew", "complete", "publish_artifact", "send_message"],
    ids=["claim", "renew", "complete", "publish_artifact", "send_message"],
)
def test_plan_destination_has_no_worker_mutation_authority(tmp_path, operation):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    create_workspace_from_plan(str(runs), "destination-run", str(plan_file))
    host = WorkspaceHost(str(runs), "destination-run", _free_port(), mode="open")
    host.start()
    try:
        coordinator = host.runtime._coordinator
        state, torn = coordinator._load()
        assert not torn and state["workers"] == {}
        recipient = host.runtime._operator_send_handles[host._message_handle]["targets"]["target-00"]["recipient_instance_id"]
        request_sha = state["tasks"]["task-00"]["request_sha256"]
        before_records = {path.name: path.read_bytes() for path in Path(coordinator.run_dir).glob("journal-g*.jsonl")}
        before_state = coordinator._load()[0]
        with pytest.raises(swarm.SwarmCoordinatorError, match="worker must register"):
            if operation == "claim":
                coordinator.claim(recipient, "task-00", request_sha256=request_sha)
            elif operation == "renew":
                coordinator.renew(recipient, "claim-1", 1)
            elif operation == "complete":
                coordinator.complete(recipient, task_id="task-00", claim_id="claim-1", attempt=1,
                                     lease_generation=1, request_sha256=request_sha,
                                     envelope_sha256="0" * 64)
            elif operation == "publish_artifact":
                coordinator.publish_artifact(recipient, task_id="task-00", claim_id="claim-1",
                                             attempt=1, lease_generation=1, request_sha256=request_sha,
                                             artifact_id="artifact-1", sha256="0" * 64,
                                             bytes_count=1, media_type="text/plain",
                                             relative_path="report.txt")
            else:
                coordinator.send_message(recipient, "coordinator", "not authority")
        assert {path.name: path.read_bytes() for path in Path(coordinator.run_dir).glob("journal-g*.jsonl")} == before_records
        assert coordinator._load()[0] == before_state
        assert coordinator.register_worker(recipient, worker_instance_id=recipient)["status"] == "registered"
        assert coordinator._load()[0]["workers"][recipient]["worker_instance_id"] == recipient
    finally:
        host.stop()


def test_public_run_command_creates_plan_without_server(tmp_path, capsys):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    result = run_command(_args(
        workspace_action="create", workspace_run_id="cli-plan-run",
        workspace_runs_root=str(runs), workspace_plan_file=str(plan_file),
    ))
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created"
    assert payload["provider_calls"] == payload["workers_started"] == 0
    assert payload["run_id"] == "cli-plan-run"
    assert not any(runs.rglob("workspace-host.sock"))


def test_plan_create_failure_leaves_deterministic_recovery_stage(monkeypatch, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    monkeypatch.setattr(_workspace_entry, "_write_plan_file",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            WorkspaceEntryError("injected_pre_publish")))
    with pytest.raises(WorkspaceEntryError, match="create_recovery_required"):
        create_workspace_from_plan(str(runs), "recovery-run", str(plan_file))
    stage = runs / (".workspace-plan-stage-" +
                    hashlib.sha256(b"recovery-run").hexdigest()[:24])
    assert stage.is_dir()
    assert not (runs / "recovery-run").exists()
    with pytest.raises(WorkspaceEntryError, match="create_recovery_required"):
        create_workspace_from_plan(str(runs), "recovery-run", str(plan_file))
    assert len(list(runs.glob(".workspace-plan-stage-*"))) == 1


def test_plan_create_refuses_destination_collision_without_overwrite(monkeypatch, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    original = _workspace_entry._publish_new
    def collide(source, destination):
        Path(destination).mkdir(parents=True, exist_ok=True)
        original(source, destination)
    monkeypatch.setattr(_workspace_entry, "_publish_new", collide)
    with pytest.raises(WorkspaceEntryError, match="create_published_uncertain"):
        create_workspace_from_plan(str(runs), "collision-run", str(plan_file))
    assert (runs / "collision-run").is_dir()
    assert len(list(runs.glob(".workspace-plan-stage-*"))) == 1


def test_plan_create_concurrent_collision_has_one_owner(monkeypatch, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    start = threading.Barrier(2)
    def create_once():
        start.wait(timeout=10)
        try:
            return ("created", create_workspace_from_plan(
                str(runs), "concurrent-run", str(plan_file)))
        except WorkspaceEntryError as exc:
            return ("error", exc.kind)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: create_once(), (1, 2)))
    assert [kind for kind, _value in results].count("created") == 1, results
    assert [kind for kind, _value in results].count("error") == 1, results
    assert (runs / "concurrent-run").is_dir()
    assert len(list(runs.glob(".workspace-plan-stage-*"))) <= 1
    assert WorkspaceHost(str(runs), "concurrent-run", _free_port(), mode="open").inspect()["status"] == "success"


def test_plan_create_post_publish_verification_failure_is_uncertain(monkeypatch, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    monkeypatch.setattr(_workspace_entry, "_verify_plan_host_files",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            WorkspaceEntryError("injected_post_publish")))
    with pytest.raises(WorkspaceEntryError, match="create_published_uncertain"):
        create_workspace_from_plan(str(runs), "verify-run", str(plan_file))
    assert (runs / "verify-run").is_dir()


def test_plan_create_cleanup_failure_leaves_reopenable_published_run(monkeypatch, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    original_rmdir = _workspace_entry.os.rmdir
    def fail_stage_cleanup(path):
        if ".workspace-plan-stage-" in os.fspath(path):
            raise OSError("injected cleanup")
        return original_rmdir(path)
    monkeypatch.setattr(_workspace_entry.os, "rmdir", fail_stage_cleanup)
    with pytest.raises(WorkspaceEntryError, match="create_cleanup_uncertain"):
        create_workspace_from_plan(str(runs), "cleanup-run", str(plan_file))
    assert (runs / "cleanup-run").is_dir()
    assert WorkspaceHost(str(runs), "cleanup-run", _free_port(), mode="open").inspect()["status"] == "success"


def test_sixteen_task_plan_reopens_all_targets_and_reconciles_edges(tmp_path):
    plan_file = tmp_path / "plan-16.json"
    plan_file.write_text(json.dumps(_versioned_plan(16), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    result = create_workspace_from_plan(str(runs), "plan-run-16", str(plan_file))
    assert result["task_count"] == result["operator_target_count"] == 16
    host = WorkspaceHost(str(runs), "plan-run-16", _free_port(), mode="open")
    host.start()
    requests = []
    try:
        for target, marker in (("target-00", "first"), ("target-15", "last")):
            request = {"operation_key": ("d" if marker == "first" else "e") * 32,
                       "target": target, "text": marker}
            requests.append(request)
            assert host.runtime.send_operator_message(host._message_handle, request)["status"] == "queued"
    finally:
        host.stop()
    reopened = WorkspaceHost(str(runs), "plan-run-16", _free_port(), mode="open")
    reopened.start()
    try:
        for request in requests:
            observed = reopened.runtime.reconcile_operator_message(reopened._message_handle, request)
            assert observed["status"] in {"queued", "already_recorded"}
    finally:
        reopened.stop()
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "demo", "create", "run-a", "--runs-root", "C:/runs",
        "--port", "43121", "--objective", "fixture"])
    assert error is None and rewritten[1] == "demo-create"
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "create", "run-a", "--plan", "plan.json", "--runs-root", "C:/runs"])
    assert error is None
    assert rewritten == [
        "--workspace-action", "create", "--workspace-run-id", "run-a",
        "--workspace-plan-file", "plan.json", "--workspace-runs-root", "C:/runs",
    ]
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "refresh", "run-a", "--url", "http://127.0.0.1:43121/",
        "--control-token-file", "C:/private/refresh.token", "--request-id", "a" * 32])
    assert error is None
    assert rewritten == [
        "--workspace-action", "refresh", "--workspace-run-id", "run-a",
        "--workspace-host-url", "http://127.0.0.1:43121/",
        "--workspace-control-token-file", "C:/private/refresh.token",
        "--workspace-request-id", "a" * 32,
    ]
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "refresh-status", "run-a", "--url", "http://127.0.0.1:43121/",
        "--control-token-file", "C:/private/refresh.token", "--request-id", "a" * 32])
    assert error is None and rewritten[1] == "refresh-status"


def test_noninteractive_entry_refuses_before_creating_a_listener(tmp_path, capsys):
    result = run_command(_args(
        workspace_action="demo-create", workspace_run_id="run-a",
        workspace_runs_root=str(tmp_path / "runs"), workspace_port=43122,
    ))
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "interactive_bootstrap_required", "status": "error"}
    assert not (tmp_path / "runs").exists()


def test_objective_is_rejected_outside_explicit_demo_mode(tmp_path, capsys):
    result = run_command(_args(
        workspace_action="open", workspace_run_id="run-a",
        workspace_runs_root=str(tmp_path / "runs"), workspace_port=43122,
        workspace_objective="must not be accepted", workspace_interactive=True,
    ))
    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"] == "objective_only_demo"


def test_demo_create_is_explicit_and_open_reinstalls_without_workers(tmp_path):
    port = _free_port()
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", port, mode="demo-create")
    ready = host.start()
    try:
        assert ready["preview"] is True
        assert ready["qualification"] == "provider_inert_scripted_demo"
        assert ready["provider_calls"] == 0
        assert ready["workers_started"] == ready["workers_resumed"] == 0
        assert ready["bootstrap_code"] not in ready["url"]
    finally:
        host.stop()

    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    reopened_ready = reopened.start()
    try:
        assert reopened_ready["preview"] is True
        assert reopened_ready["qualification"] == "provider_inert_reopen"
        assert reopened_ready["workers_resumed"] == 0
        assert reopened_ready["bootstrap_code"] != ready["bootstrap_code"]
    finally:
        reopened.stop()


def test_demo_host_installs_typed_operator_command_adapter(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    ready = host.start()
    try:
        assert ready["operator_commands"] == "available"
        assert host.surface.operator_commands is not None
        state, torn = host.runtime._coordinator._load()
        assert not torn
        view_scope = ViewScope(host.workspace_id, host.run_id, host.surface._key,
                               audience="operator", allow_text=True)
        described = host.surface.operator_commands.describe(state["workspace"], view_scope)
        assert described["available"] is True
        assert any("cancel_queued_context" in actions
                   for actions in described["actions_by_delivery"].values())
    finally:
        host.stop()


def test_demo_host_command_adapter_executes_one_authenticated_disposition(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        state, torn = host.runtime._coordinator._load()
        assert not torn
        view_scope = ViewScope(host.workspace_id, host.run_id, host.surface._key,
                               audience="operator", allow_text=True)
        described = host.surface.operator_commands.describe(state["workspace"], view_scope)
        target = next(target for target, actions in described["actions_by_delivery"].items()
                      if "cancel_queued_context" in actions)
        result = host.surface.perform_command({
            "operation_key": "d" * 32,
            "action": "cancel_queued_context",
            "target": target,
        })
        assert result["status"] == "recorded"
        latest, torn = host.runtime._coordinator._load()
        assert not torn
        assert any(item["state"] == "cancelled"
                   for item in latest["workspace"]["deliveries"].values())
    finally:
        host.stop()


def test_noninteractive_workspace_request_uses_private_files_and_existing_host(tmp_path, capsys):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    ready = host.start()
    try:
        view_scope = ViewScope(host.workspace_id, host.run_id, host.surface._key,
                               audience="operator", allow_text=True)
        state, torn = host.runtime._coordinator._load()
        assert not torn
        described = host.surface.operator_commands.describe(state["workspace"], view_scope)
        target = next(target for target, actions in described["actions_by_delivery"].items()
                      if "cancel_queued_context" in actions)
        token_path = tmp_path / "session.token"
        token_path.write_text(host.surface._exchange(ready["bootstrap_code"]), encoding="ascii")
        _workspace_entry._secure_file(str(token_path))
        request_path = tmp_path / "command.json"
        request_path.write_bytes(_workspace_entry._canonical({
            "operation_key": "e" * 32,
            "action": "cancel_queued_context",
            "target": target,
        }) + b"\n")
        _workspace_entry._secure_file(str(request_path))
        result = run_command(_args(
            workspace_action="request", workspace_run_id="run-a",
            workspace_runs_root=str(tmp_path / "runs"),
            workspace_host_url=ready["url"], workspace_token_file=str(token_path),
            workspace_request_file=str(request_path), workspace_request_kind="command"))
        assert result == 0
        assert json.loads(capsys.readouterr().out)["status"] == "recorded"
    finally:
        host.stop()


def test_open_accepts_only_explicit_private_command_policy(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    state, torn = host.runtime._coordinator._load()
    assert not torn
    delivery_id = next(iter(state["workspace"]["deliveries"]))
    host.stop()
    policy_path = tmp_path / "command-policy.json"
    policy_path.write_bytes(_workspace_entry._canonical({
        "schema": "summon.workspace-command-policy/v1",
        "workspace_id": "workspace", "run_id": "run-a", "generation": 1,
        "expires_at_ms": int(time.time() * 1000) + 600000,
        "max_active_deliveries": 1,
        "targets": [{"delivery_id": delivery_id, "actions": ["cancel_queued_context"]}],
    }) + b"\n")
    _workspace_entry._secure_file(str(policy_path))
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open",
                             command_policy_file=str(policy_path))
    ready = reopened.start()
    try:
        assert ready["operator_commands"] == "available"
        scope = reopened.surface.operator_commands._installed_scope()
        assert scope.targets == ((delivery_id, ("cancel_queued_context",)),)
    finally:
        reopened.stop()


def test_open_host_mints_typed_retain_proofs_and_records_disposition(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        demo = host.demo
        state, torn = host.runtime._coordinator._load()
        assert not torn
        delivery_id = next(iter(state["workspace"]["deliveries"]))
        source = demo.source({"kind": "fixture-context-hold", "delivery": delivery_id})
        hold_ref = {"id": source["id"] + ".hold-observation", "sha256": source["sha256"]}
        evidence_event = demo.event("workspace_evidence_registered", {
            "reference": hold_ref, "category": "observation", "task_id": "main-1",
            "delivery_id": delivery_id,
            "settlement_for": {"delivery_id": delivery_id,
                                "target_state": "held_for_recovery", "role": "hold_observation"}},
            "host-retain-hold-observation")
        host._record_install_command(evidence_event)
        demo.runtime.record_event(evidence_event)
        delivery = copy.deepcopy(demo.state()["workspace"]["deliveries"][delivery_id])
        certainty = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        delivery.update(state="held_for_recovery", reason="recipient_drift", ack_level=None,
                        certainty=certainty)
        hold_event = demo.event("workspace_delivery_advanced", {
            "delivery": delivery, "evidence": {"hold_observation": hold_ref},
            "supported_ack_levels": []}, "host-retain-hold-transition")
        host._record_install_command(hold_event)
        demo.runtime.record_event(hold_event)
    finally:
        host.stop()

    policy_path = tmp_path / "retain-policy.json"
    policy_path.write_bytes(_workspace_entry._canonical({
        "schema": "summon.workspace-command-policy/v1",
        "workspace_id": "workspace", "run_id": "run-a", "generation": 1,
        "expires_at_ms": int(time.time() * 1000) + 600000,
        "max_active_deliveries": 1,
        "targets": [{"delivery_id": delivery_id, "actions": ["retain_held_context"]}],
    }) + b"\n")
    _workspace_entry._secure_file(str(policy_path))
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open",
                             command_policy_file=str(policy_path))
    reopened.start()
    try:
        from test_workspace_ui import login, request
        token, _ = login(reopened.surface)
        status, _, raw = request(reopened.surface, token=token)
        assert status == 200, raw.decode()
        target = json.loads(raw)["operator_dispositions"]["targets"][0]["id"]
        status, _, raw = request(reopened.surface, "POST", "/api/dispositions",
                                 token=token, payload={
                                     "operation_key": "a" * 32,
                                     "target": target, "reason": "awaiting_evidence"})
        assert status == 200 and json.loads(raw)["status"] == "recorded"
        status, _, raw = request(reopened.surface, "POST", "/api/dispositions",
                                 token=token, payload={
                                     "operation_key": "b" * 32,
                                     "target": target, "reason": "operator_hold"})
        assert status == 200 and json.loads(raw)["status"] == "recorded"
        status, _, raw = request(reopened.surface, "POST", "/api/dispositions/lookup",
                                 token=token, payload={
                                     "operation_key": "b" * 32,
                                     "target": target, "reason": "operator_hold"})
        assert status == 200 and json.loads(raw)["status"] == "recorded"
        latest, torn = reopened.runtime._coordinator._load()
        assert not torn
        assert latest["workspace"]["deliveries"][delivery_id]["state"] == "held_for_recovery"
        typed = [item for item in latest["workspace"]["evidence"].values()
                 if item.get("category") == "event" and item.get("delivery_id") == delivery_id
                 and "settlement_for" not in item]
        assert len(typed) == 4
        roles = set()
        for item in typed:
            raw = reopened.runtime._resolve_evidence(copy.deepcopy(item))
            parsed = json.loads(raw.decode("utf-8"))
            roles.add((parsed["operation_key"], parsed["role"]))
        assert roles == {("a" * 32, "request"), ("a" * 32, "decision"),
                         ("b" * 32, "request"), ("b" * 32, "decision")}
    finally:
        reopened.stop()

    reopened_again = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open",
                                   command_policy_file=str(policy_path))
    reopened_again.start()
    try:
        from test_workspace_ui import login, request
        token, _ = login(reopened_again.surface)
        status, _, raw = request(reopened_again.surface, token=token)
        assert status == 200, raw.decode()
        target = json.loads(raw)["operator_dispositions"]["targets"][0]["id"]
        status, _, raw = request(reopened_again.surface, "POST", "/api/dispositions/lookup",
                                 token=token, payload={"operation_key": "b" * 32,
                                                      "target": target, "reason": "operator_hold"})
        assert status == 200 and json.loads(raw)["status"] == "recorded"
    finally:
        reopened_again.stop()


def test_command_policy_refresh_binds_new_generation_before_adapter_and_rolls_back(tmp_path, monkeypatch):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        state, torn = host.runtime._coordinator._load()
        assert not torn
        delivery_id = next(iter(state["workspace"]["deliveries"]))
        def write_policy(path, generation):
            path.write_bytes(_workspace_entry._canonical({
                "schema": "summon.workspace-command-policy/v1",
                "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
                "expires_at_ms": int(time.time() * 1000) + 600000,
                "max_active_deliveries": 1,
                "targets": [{"delivery_id": delivery_id, "actions": ["cancel_queued_context"]}],
            }) + b"\n")
            _workspace_entry._secure_file(str(path))
        policy_v2 = tmp_path / "policy-v2.json"
        write_policy(policy_v2, 2)
        refreshed = host.refresh_command_policy(str(policy_v2))
        assert refreshed["generation"] == 2
        assert host._command_policy_generation == 2
        assert host.surface.operator_commands._installed_scope().targets == (
            (delivery_id, ("cancel_queued_context",)),)
        with pytest.raises(WorkspaceEntryError, match="generation_stale"):
            host.refresh_command_policy(str(policy_v2))

        policy_v3 = tmp_path / "policy-v3.json"
        write_policy(policy_v3, 3)
        previous_adapter = host.surface.operator_commands
        class FailingAdapter:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("injected")
        monkeypatch.setattr(_workspace_entry, "OperatorCommandAdapter", FailingAdapter)
        with pytest.raises(WorkspaceEntryError, match="refresh_failed"):
            host.refresh_command_policy(str(policy_v3))
        assert host._command_policy_generation == 2
        assert host.surface.operator_commands is previous_adapter
    finally:
        host.stop()


def test_noninteractive_refresh_reloads_policy_with_private_control_token(tmp_path, capsys):
    runs = tmp_path / "runs"
    seed = WorkspaceHost(str(runs), "run-a", _free_port(), mode="demo-create")
    seed.start()
    state, torn = seed.runtime._coordinator._load()
    assert not torn
    delivery_id = next(iter(state["workspace"]["deliveries"]))
    seed.stop()

    def write_policy(path, generation):
        path.write_bytes(_workspace_entry._canonical({
            "schema": "summon.workspace-command-policy/v1",
            "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
            "expires_at_ms": int(time.time() * 1000) + 600000,
            "max_active_deliveries": 1,
            "targets": [{"delivery_id": delivery_id,
                         "actions": ["cancel_queued_context"]}],
        }) + b"\n")
        _workspace_entry._secure_file(str(path))

    policy = tmp_path / "policy.json"
    write_policy(policy, 1)
    token = tmp_path / "refresh.token"
    host = WorkspaceHost(str(runs), "run-a", _free_port(), mode="open",
                         command_policy_file=str(policy),
                         command_refresh_token_file=str(token))
    ready = host.start()
    try:
        assert ready["command_refresh_token"]
        assert token.read_text(encoding="ascii") == ready["command_refresh_token"]
        write_policy(policy, 2)
        result = run_command(_args(
            workspace_action="refresh", workspace_run_id="run-a",
            workspace_host_url=ready["url"],
            workspace_control_token_file=str(token)))
        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "refreshed"
        assert payload["generation"] == 2
        assert payload["provider_calls"] == 0
        assert host._command_policy_generation == 2
        result = run_command(_args(
            workspace_action="refresh-status", workspace_run_id="run-a",
            workspace_host_url=ready["url"],
            workspace_control_token_file=str(token),
            workspace_request_id=payload["request_id"]))
        assert result == 0
        status = json.loads(capsys.readouterr().out)
        assert status["status"] == "refreshed"
        assert host._command_policy_generation == 2
    finally:
        host.stop()


def test_refresh_request_ids_are_bounded_and_old_ids_never_reapply(tmp_path, monkeypatch):
    monkeypatch.setattr(_workspace_entry, "COMMAND_REFRESH_HISTORY_MAX", 2)
    runs = tmp_path / "runs"
    seed = WorkspaceHost(str(runs), "run-a", _free_port(), mode="demo-create")
    seed.start()
    state, torn = seed.runtime._coordinator._load()
    assert not torn
    delivery_id = next(iter(state["workspace"]["deliveries"]))
    seed.stop()

    def write_policy(path, generation, action="cancel_queued_context"):
        path.write_bytes(_workspace_entry._canonical({
            "schema": "summon.workspace-command-policy/v1",
            "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
            "expires_at_ms": int(time.time() * 1000) + 600000,
            "max_active_deliveries": 1,
            "targets": [{"delivery_id": delivery_id, "actions": [action]}],
        }) + b"\n")
        _workspace_entry._secure_file(str(path))

    policy = tmp_path / "policy.json"
    policy_b = tmp_path / "policy-b.json"
    policy_c = tmp_path / "policy-c.json"
    write_policy(policy, 1)
    write_policy(policy_b, 2, "dispose_held_context")
    write_policy(policy_c, 3)
    host = WorkspaceHost(str(runs), "run-a", _free_port(), mode="open",
                         command_policy_file=str(policy))
    host.start()
    try:
        result_a = host.refresh_command_policy(str(policy_b), request_id="a" * 32)
        result_b = host.refresh_command_policy(str(policy_c), request_id="b" * 32)
        assert result_a["generation"] == 2 and result_b["generation"] == 3
        before = host._command_policy_generation
        assert host.refresh_command_policy_status(host._command_refresh_token or "", "a" * 32)["status"] == "refreshed"
        assert host.refresh_command_policy_status(host._command_refresh_token or "", "b" * 32)["status"] == "refreshed"
        # Reusing A after the policy file changed is idempotent history, never
        # a second installation or a way to apply policy C under an old key.
        write_policy(policy, 4, "cancel_queued_context")
        assert host.refresh_command_policy(str(policy), request_id="a" * 32) == result_a
        assert host._command_policy_generation == before
        with pytest.raises(WorkspaceEntryError, match="history_full"):
            host.refresh_command_policy(str(policy), request_id="c" * 32)
        assert host._command_policy_generation == before
    finally:
        host.stop()


def test_send_then_explicit_policy_refresh_exposes_disposition_and_reopen_lookup(tmp_path):
    runs = tmp_path / "runs"
    seed = WorkspaceHost(str(runs), "run-a", _free_port(), mode="demo-create")
    seed.start()
    state, torn = seed.runtime._coordinator._load()
    assert not torn
    original_delivery = next(iter(state["workspace"]["deliveries"]))
    seed.stop()

    def write_policy(path, generation, delivery_ids):
        path.write_bytes(_workspace_entry._canonical({
            "schema": "summon.workspace-command-policy/v1",
            "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
            "expires_at_ms": int(time.time() * 1000) + 600000,
            "max_active_deliveries": 2,
            "targets": [{"delivery_id": item, "actions": ["cancel_queued_context"]}
                         for item in delivery_ids],
        }) + b"\n")
        _workspace_entry._secure_file(str(path))

    policy = tmp_path / "policy.json"
    write_policy(policy, 1, [original_delivery])
    host = WorkspaceHost(str(runs), "run-a", _free_port(), mode="open",
                         command_policy_file=str(policy))
    host.start()
    try:
        sent = host.runtime.send_operator_message(
            host._message_handle,
            {"operation_key": "f" * 32, "target": "operator-target", "text": "new context"})
        assert sent["status"] == "queued"
        state, torn = host.runtime._coordinator._load()
        assert not torn
        new_delivery = next(item for item in state["workspace"]["deliveries"]
                            if item != original_delivery)
        write_policy(policy, 2, [original_delivery, new_delivery])
        refreshed = host.refresh_command_policy(str(policy), request_id="f" * 32)
        assert refreshed["generation"] == 2
        scope = host.surface.operator_commands._installed_scope()
        assert {item[0] for item in scope.targets} == {original_delivery, new_delivery}
        view_scope = ViewScope(host.workspace_id, host.run_id, host.surface._key,
                               audience="operator", allow_text=True)
        described = host.surface.operator_commands.describe(state["workspace"], view_scope)
        target = _opaque(view_scope, "delivery", new_delivery)
        assert described["actions_by_delivery"][target] == ["cancel_queued_context"]
        command = {"operation_key": "e" * 32,
                   "action": "cancel_queued_context", "target": target}
        assert host.surface.perform_command(command)["status"] == "recorded"
        state, torn = host.runtime._coordinator._load()
        assert not torn and state["workspace"]["deliveries"][new_delivery]["state"] == "cancelled"
    finally:
        host.stop()
    reopened = WorkspaceHost(str(runs), "run-a", _free_port(), mode="open",
                             command_policy_file=str(policy))
    reopened.start()
    try:
        view_scope = ViewScope(reopened.workspace_id, reopened.run_id,
                               reopened.surface._key, audience="operator", allow_text=True)
        target = _opaque(view_scope, "delivery", new_delivery)
        lookup = reopened.surface.perform_command(
            {"operation_key": "e" * 32, "action": "cancel_queued_context", "target": target},
            lookup=True)
        assert lookup["status"] == "recorded"
    finally:
        reopened.stop()


def test_command_policy_refresh_serializes_competing_generations(tmp_path, monkeypatch):
    """A slower older refresh cannot overwrite a newer committed generation."""
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        state, torn = host.runtime._coordinator._load()
        assert not torn
        delivery_id = next(iter(state["workspace"]["deliveries"]))

        def write_policy(path, generation, action):
            path.write_bytes(_workspace_entry._canonical({
                "schema": "summon.workspace-command-policy/v1",
                "workspace_id": "workspace", "run_id": "run-a", "generation": generation,
                "expires_at_ms": int(time.time() * 1000) + 600000,
                "max_active_deliveries": 1,
                "targets": [{"delivery_id": delivery_id, "actions": [action]}],
            }) + b"\n")
            _workspace_entry._secure_file(str(path))

        policy_v2 = tmp_path / "policy-v2.json"
        policy_v3 = tmp_path / "policy-v3.json"
        write_policy(policy_v2, 2, "cancel_queued_context")
        write_policy(policy_v3, 3, "dispose_held_context")

        entered_v2 = threading.Event()
        release_v2 = threading.Event()
        original_install = host.runtime.install_operator_commands

        def gated_install(scope, *args, **kwargs):
            if scope["targets"][0]["actions"] == ["cancel_queued_context"]:
                entered_v2.set()
                assert release_v2.wait(5), "older refresh did not receive release"
            return original_install(scope, *args, **kwargs)

        monkeypatch.setattr(host.runtime, "install_operator_commands", gated_install)
        results = {}

        def refresh(label, path):
            try:
                results[label] = host.refresh_command_policy(str(path))
            except Exception as exc:  # assert in the owner thread below
                results[label] = exc

        older = threading.Thread(target=refresh, args=("v2", policy_v2), daemon=True)
        newer = threading.Thread(target=refresh, args=("v3", policy_v3), daemon=True)
        older.start()
        assert entered_v2.wait(5), "older refresh did not enter its install transaction"
        newer.start()
        time.sleep(0.05)
        assert newer.is_alive(), "newer refresh bypassed the policy transaction lock"
        release_v2.set()
        older.join(5)
        newer.join(5)
        assert not older.is_alive() and not newer.is_alive()
        assert not isinstance(results.get("v2"), Exception), results.get("v2")
        assert not isinstance(results.get("v3"), Exception), results.get("v3")
        assert host._command_policy_generation == 3
        assert host.surface.operator_commands._installed_scope().targets == (
            (delivery_id, ("dispose_held_context",)),)
    finally:
        host.stop()


def test_workspace_command_policy_alias_is_private_and_explicit():
    rewritten, error = _cli.rewrite_subcommand([
        "workspace", "open", "run-a", "--runs-root", "C:/runs", "--port", "43121",
        "--command-policy", "C:/private/policy.json"])
    assert error is None
    assert rewritten[-2:] == ["--workspace-command-policy", "C:/private/policy.json"]


def test_open_reuses_private_host_config_for_draft_recovery(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    ready = host.start()
    request = {"target": "operator-target", "operation_key": "a" * 32, "state": "unsent"}
    first_key = host.runtime.derive_operator_draft_key(host._draft_handle, request)["key_b64"]
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    config_bytes = config_path.read_bytes()
    host.stop()

    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    reopened_ready = reopened.start()
    try:
        second_key = reopened.runtime.derive_operator_draft_key(reopened._draft_handle, request)["key_b64"]
        assert second_key == first_key
        assert config_path.read_bytes() == config_bytes
        assert reopened_ready["bootstrap_code"] not in reopened_ready["url"]
    finally:
        reopened.stop()


def test_legacy_host_reopen_preserves_scope_identity_without_schema_upgrade(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    assert config["schema"] == _workspace_entry.HOST_CONFIG_SCHEMA
    assert config["workspace_id"] == "workspace"
    assert config["run_id"] == "run-a"
    assert config["operator_id"] == _workspace_entry.OPERATOR_ID
    host.stop()

    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    try:
        ready = reopened.start()
        assert ready["workspace_id"] == "workspace"
        assert ready["run_id"] == "run-a"
        assert Path(reopened.runtime._coordinator.run_dir, "workspace-host.json").read_bytes() == config_bytes
    finally:
        reopened.stop()


def test_open_refuses_malformed_host_config_without_rotating_or_starting(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    host.stop()
    config_path.write_bytes(b"{}\n")
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    with pytest.raises(WorkspaceEntryError, match="host_config_invalid"):
        reopened.start()
    assert not reopened._started


def test_host_config_rejects_cross_version_field_sets(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    host.stop()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["plan_schema"] = "summon.workspace.plan/v1"
    config_path.write_bytes(_workspace_entry._canonical(config) + b"\n")
    with pytest.raises(WorkspaceEntryError, match="host_config_invalid"):
        WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open").start()

    # A plan-schema label with the v1 field set is equally invalid; the
    # schema label and exact field set are a single versioned contract.
    config = json.loads(_workspace_entry._canonical(_workspace_entry._host_config_value(
        "workspace", "run-a", b"p" * 32, b"r" * 32, "epoch")) .decode("utf-8"))
    config["schema"] = _workspace_entry.PLAN_HOST_SCHEMA
    config_path.write_bytes(_workspace_entry._canonical(config) + b"\n")
    with pytest.raises(WorkspaceEntryError, match="host_config_invalid"):
        WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open").start()


def test_plan_copy_binding_is_verified_on_reopen(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    create_workspace_from_plan(str(runs), "plan-run", str(plan_file))
    run_dir = next(runs.joinpath("plan-run").rglob("workspace-host.json")).parent
    copied_plan = run_dir / "workspace-plan.json"
    value = json.loads(copied_plan.read_text(encoding="utf-8"))
    value["goal"]["objective"] = "changed after publish"
    copied_plan.write_bytes(_workspace_entry._canonical(value) + b"\n")
    with pytest.raises(WorkspaceEntryError, match="plan_source_mismatch"):
        WorkspaceHost(str(runs), "plan-run", _free_port(), mode="open").start()


def test_plan_binding_config_tamper_refuses_reopen(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_versioned_plan(1), ensure_ascii=False), encoding="utf-8")
    runs = tmp_path / "runs"
    create_workspace_from_plan(str(runs), "binding-run", str(plan_file))
    config_path = next(runs.joinpath("binding-run").rglob("workspace-host.json"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["roster_binding_kind"] = "unbound"
    config_path.write_bytes(_workspace_entry._canonical(config) + b"\n")
    with pytest.raises(WorkspaceEntryError, match="host_config_invalid"):
        WorkspaceHost(str(runs), "binding-run", _free_port(), mode="open").start()


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "host_config_unavailable"),
    ("replaced", "host_config_invalid"),
], ids=["missing", "replaced"])
def test_open_refuses_missing_or_replaced_host_config(tmp_path, mutation, expected):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    host.stop()
    if mutation == "missing":
        config_path.unlink()
    else:
        config_path.write_bytes(b"{\"schema\":\"summon.workspace-host/v1\"}\n")
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    with pytest.raises(WorkspaceEntryError, match=expected):
        reopened.start()
    assert not reopened._started


def test_open_refuses_expired_operator_scope(monkeypatch, tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    host.stop()
    monkeypatch.setattr(_workspace_entry.time, "time", lambda: 4_000_000_000)
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    with pytest.raises(WorkspaceEntryError, match="operator_scope_unavailable"):
        reopened.start()
    assert not reopened._started


def test_inspect_is_generic_bounded_and_does_not_start_server(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    running = host.start()
    host.stop()
    inspected = WorkspaceHost(str(tmp_path / "runs"), "run-a", 1, mode="open").inspect()
    assert inspected["preview"] is True
    assert inspected["mode"] == "inspect"
    assert inspected["mutation"] == "none"
    assert inspected["provider_calls"] == 0
    assert inspected["workers_resumed"] == 0
    assert running["url"].startswith("http://127.0.0.1:")


def test_explicit_port_refuses_occupied_port_without_fallback(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", port, mode="demo-create")
    with pytest.raises(WorkspaceEntryError, match="loopback_port_unavailable"):
        host.start()
    sock.close()
    assert not host._started


def test_inspection_resolver_uses_runtime_admission_evidence_path():
    class Runtime:
        def read_send_admission_evidence(self, payload):
            assert payload["reference"]["id"] == "admission-a"
            return b"verified-admission"

    assert _content_resolver(Runtime())({"reference": {"id": "admission-a"}}) == b"verified-admission"


def test_foreground_output_failure_still_stops_owned_host():
    class Host:
        def __init__(self):
            self.started = self.stopped = False

        def start(self):
            self.started = True
            return {"status": "ready", "bootstrap_code": "private"}

        def stop(self):
            self.stopped = True

    host = Host()
    def fail_output(*_args, **_kwargs):
        raise OSError("captured stream closed")
    assert _run_foreground(host, output=fail_output, secret_output=fail_output, interactive=True) == 2
    assert host.started and host.stopped


def test_foreground_interrupt_emits_stopped_terminal_envelope(monkeypatch):
    class Host:
        def __init__(self):
            self.stopped = False

        def start(self):
            return {"status": "ready", "bootstrap_code": "private"}

        def stop(self):
            self.stopped = True

    lines = []
    monkeypatch.setattr(_workspace_entry.time, "sleep",
                        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    host = Host()
    assert _run_foreground(host, output=lambda text, **_: lines.append(text),
                           secret_output=lambda *_args, **_kwargs: None,
                           interactive=True) == 0
    assert host.stopped
    terminal = json.loads(lines[-1])
    assert terminal["status"] == "stopped"
    assert terminal["cleanup"] == "complete"
    assert terminal["provider_calls"] == 0


def test_foreground_cleanup_failure_emits_typed_terminal_envelope(monkeypatch):
    class Host:
        def start(self):
            return {"status": "ready", "bootstrap_code": "private"}

        def stop(self):
            raise WorkspaceEntryError("cleanup_uncertain")

    lines = []
    monkeypatch.setattr(_workspace_entry.time, "sleep",
                        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert _run_foreground(Host(), output=lambda text, **_: lines.append(text),
                           secret_output=lambda *_args, **_kwargs: None,
                           interactive=True) == 2
    terminal = json.loads(lines[-1])
    assert terminal == {
        "cleanup": "uncertain", "error": "cleanup_uncertain",
        "lifecycle": "foreground_owned", "preview": True, "provider_calls": 0,
        "status": "error", "workers_resumed": 0, "workers_started": 0,
    }


def test_foreground_ready_stdout_never_contains_bootstrap_but_stderr_does(monkeypatch):
    class Host:
        def start(self):
            return {"status": "ready", "bootstrap_code": "private-bootstrap"}

        def stop(self):
            pass

    stdout, stderr = [], []
    monkeypatch.setattr(_workspace_entry.time, "sleep",
                        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert _run_foreground(Host(), output=lambda text, **_: stdout.append(text),
                           secret_output=lambda text, **_: stderr.append(text),
                           interactive=True) == 0
    assert all("private-bootstrap" not in line for line in stdout)
    assert any("private-bootstrap" in line for line in stderr)
    assert json.loads(stdout[0])["status"] == "ready"
    assert json.loads(stdout[-1])["status"] == "stopped"


def test_foreground_start_interrupt_is_owned_and_emits_safe_terminal_error():
    class Host:
        def __init__(self):
            self.stop_calls = 0

        def start(self):
            raise KeyboardInterrupt()

        def stop(self):
            self.stop_calls += 1

    lines = []
    host = Host()
    assert _run_foreground(host, output=lambda text, **_: lines.append(text),
                           secret_output=lambda *_args, **_kwargs: None,
                           interactive=True) == 2
    assert host.stop_calls == 1
    terminal = json.loads(lines[-1])
    assert terminal["status"] == "error"
    assert terminal["error"] == "interrupted"
    assert terminal["cleanup"] == "complete"
    assert "bootstrap_code" not in terminal


def test_foreground_start_failure_emits_terminal_stdout_after_cleanup():
    class Host:
        def __init__(self):
            self.stop_calls = 0

        def start(self):
            raise WorkspaceEntryError("host_config_invalid")

        def stop(self):
            self.stop_calls += 1

    lines = []
    host = Host()
    assert _run_foreground(host, output=lambda text, **_: lines.append(text),
                           secret_output=lambda *_args, **_kwargs: None,
                           interactive=True) == 2
    assert host.stop_calls == 1
    assert json.loads(lines[-1]) == {
        "cleanup": "complete", "error": "host_config_invalid",
        "lifecycle": "foreground_owned", "preview": True, "provider_calls": 0,
        "status": "error", "workers_resumed": 0, "workers_started": 0,
    }


@pytest.mark.parametrize(("error_type", "expected"), [
    (_workspace_entry.SwarmCoordinatorError, "coordinator_unavailable"),
    (_workspace_entry.OwnershipLostError, "ownership_lost"),
], ids=["coordinator", "ownership"])
def test_foreground_start_maps_typed_runtime_failures_without_leaking_details(error_type, expected):
    class Host:
        def __init__(self):
            self.stop_calls = 0

        def start(self):
            raise error_type("private path and coordinator details")

        def stop(self):
            self.stop_calls += 1

    lines = []
    host = Host()
    assert _run_foreground(host, output=lambda text, **_: lines.append(text),
                           secret_output=lambda *_args, **_kwargs: None,
                           interactive=True) == 2
    assert host.stop_calls == 1
    terminal = json.loads(lines[-1])
    assert terminal == {
        "cleanup": "complete", "error": expected,
        "lifecycle": "foreground_owned", "preview": True, "provider_calls": 0,
        "status": "error", "workers_resumed": 0, "workers_started": 0,
    }
    assert all("private path" not in line for line in lines)


def test_open_refuses_torn_journal_and_mismatched_host_config(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    config_path = Path(host.runtime._coordinator.run_dir) / "workspace-host.json"
    run_dir = Path(host.runtime._coordinator.run_dir)
    journal = next(run_dir.glob("journal-g*.jsonl"))
    host.stop()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["run_id"] = "other-run"
    config_path.write_bytes(_workspace_entry._canonical(config) + b"\n")
    with pytest.raises(WorkspaceEntryError, match="host_config_scope_mismatch"):
        WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open").start()
    config["run_id"] = "run-a"
    config_path.write_bytes(_workspace_entry._canonical(config) + b"\n")
    with journal.open("ab") as stream:
        stream.write(b"{torn")
    with pytest.raises(WorkspaceEntryError, match="journal_recovery_required"):
        WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open").start()


def test_reopened_host_can_send_and_reconcile_same_operator_message(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    request = {"operation_key": "b" * 32, "target": "operator-target", "text": "hello after reopen"}
    first = host.runtime.send_operator_message(host._message_handle, request)
    assert first["status"] == "queued"
    host.stop()
    reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
    reopened.start()
    try:
        second = reopened.runtime.reconcile_operator_message(reopened._message_handle, request)
        assert second["status"] == "queued"
        state, torn = reopened.runtime._coordinator._load()
        assert not torn
        # Demo-create seeds one explicit queued disposition target so the
        # public host can expose its typed command adapter; this test adds one
        # additional operator message after reopen.
        assert len(state["workspace"]["messages"]) == 2
        assert len(state["workspace"]["send_operations"]) == 1
    finally:
        reopened.stop()


def test_stop_revokes_host_authority_and_piped_cli_refuses_before_listener(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    host.stop()
    assert host._authority["allowed"] is False
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "workspace", "demo", "create", "run-a",
         "--runs-root", str(tmp_path / "piped-runs"), "--port", "43123"],
        cwd=str(script.parent), capture_output=True, text=True, timeout=20,
        **run_flags(),
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error": "interactive_bootstrap_required", "status": "error"
    }
    assert not (tmp_path / "piped-runs").exists()
