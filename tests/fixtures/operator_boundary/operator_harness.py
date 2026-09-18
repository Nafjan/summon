"""Exact OperatorRuntimeHarness from test_workspace_operator_runtime.py; no pytest import."""
import copy
from pathlib import Path
import _workspace_admission as admission
from _workspace_demo import ConductorDemo
class OperatorRuntimeHarness:
    def __init__(self, tmp_path):
        self.demo = ConductorDemo(tmp_path / "workspaces")
        self.demo.prepare()
        self.grant, grant_value = self.demo.grant("main-1", "operator-receiver")
        self.demo.runtime._coordinator.register_worker("operator-receiver", worker_instance_id="operator-receiver")
        self.grant_value = {"grant_ref": self.grant, **{key: grant_value[key] for key in
                            ("task_id", "goal_revision", "recipient", "revoked")}}
        self.allowed = self.grant_live = True
        scope = {"revision": 1, "expires_at_ms": int(self.demo.runtime._coordinator.clock() * 1000) + 300000,
                 "revoked": False, "operation": "operator.message.send", "goal_id": "verify-conductor-loop",
                 "destination_task_id": "main-1", "recipient": {"instance_id": "operator-receiver", "epoch": 1},
                 "delivery_grant_ref": self.grant, "target": "operator-target"}
        source = self.demo.source({"schema": "summon.workspace.operator-scope/v1",
            "workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id, "operator_id": "local-operator", "scope": scope})
        scope_ref = self.demo.evidence(source, "main-1", "operator-scope", "grant")
        self.resolved = {"operator_id": "local-operator", "scope": scope | {"reference": scope_ref}}
        self.scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
            "operator_id": "local-operator", "targets": [{"target": "operator-target", "task_id": "main-1", "send_scope_ref": scope_ref}]}
        self.request = {"operation_key": "a" * 32, "target": "operator-target", "text": "[2,\n3]"}
        self.handle = self.install()

    def grant_resolver(self, current, workspace, reference):
        assert reference == self.grant and current["workspace"] == workspace
        return copy.deepcopy(self.grant_value) | {"revoked": not self.grant_live}

    def install(self):
        self.demo.permit({"operation": "install_operator_messages", "scope": self.scope})
        return self.demo.runtime.install_operator_messages(self.scope,
            authorize_message=lambda command, workspace: self.allowed, resolve_grant=self.grant_resolver)

    def permit(self, request, lookup):
        _, digest = admission.operator_request_identity(self.demo.state()["workspace"], self.resolved, request)
        command = {"operation": "operator_message.lookup" if lookup else "operator_message.send",
                   **{key: self.scope[key] for key in ("workspace_id", "run_id", "operator_id")},
                   "request": {"operation_key": request["operation_key"], "target": request["target"], "request_sha256": digest}}
        self.demo.permit(command)

    def send(self, *, request=None, constraint=None):
        request = self.request if request is None else request
        self.permit(request, False)
        return self.demo.runtime.send_operator_message(self.handle, request, authorization_constraint=constraint)

    def lookup(self):
        self.permit(self.request, True)
        return self.demo.runtime.reconcile_operator_message(self.handle, self.request)

    def journals(self):
        return {p.name: p.read_bytes() for p in Path(self.demo.runtime._coordinator.run_dir).glob("journal-g*.jsonl")}
