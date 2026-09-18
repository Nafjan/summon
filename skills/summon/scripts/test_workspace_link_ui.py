from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_ui as ui
from _workspace_commands import OperatorLinkedReplacementAdapter
from _workspace_entry import WorkspaceHost
import _workspace_admission as admission
from test_workspace_link_runtime import TestLinkedRuntime
from test_workspace_entry import _free_port
from test_workspace_ui import login, request


class TestLinkedPublicSurface(TestLinkedRuntime):
    def test_authenticated_link_route_is_opaque_and_reopens_same_key(self):
        authority = self._authority()
        handle, proposal = self._install(authority)
        adapter = OperatorLinkedReplacementAdapter(self.demo.runtime, handle)
        surface = ui.WorkspaceSurface(self.demo.runtime._coordinator, self.demo.workspace_id,
                                      operator_linked_replacements=adapter,
                                      presentation_key=b"l" * 32)
        surface.start()
        try:
            token, _ = login(surface)
            status, _, raw = request(surface, token=token)
            assert status == 200
            view = json.loads(raw)
            cap = view["operator_linked_replacements"]
            assert cap["available"] is True
            assert len(cap["targets"]) == 1
            parent = cap["targets"][0]["parent_target"]
            recipient = cap["targets"][0]["recipient_target"]
            assert "opaque-parent" not in raw.decode("utf-8")
            assert "opaque-recipient" not in raw.decode("utf-8")
            body = {"operation_key": proposal["operation_key"],
                    "parent_target": parent, "recipient_target": recipient}
            initial = copy.deepcopy(self.demo.state()["workspace"])
            parent_id = self.entry["delivery_id"]
            parent_before = copy.deepcopy(initial["deliveries"][parent_id])
            status, _, raw = request(surface, "POST", "/api/linked-replacements",
                                     token=token, payload=body)
            assert status == 200, raw.decode()
            result = json.loads(raw)
            assert result["schema"] == "summon.workspace.linked-replacement-result/v1"
            assert result["status"] == "recorded"
            assert result["child_delivery"]
            assert result["parent_target"] == parent
            assert result["recipient_target"] == recipient
            recorded = self.demo.state()["workspace"]
            assert len(recorded["deliveries"]) == len(initial["deliveries"]) + 1
            assert recorded["deliveries"][parent_id] == parent_before
            before = copy.deepcopy(self.demo.state()["workspace"]["deliveries"])
            competing = {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
                          "action": admission.LINKED_REPLACEMENT_ACTION,
                          "operation_key": "d" * 32,
                          "parent_target": "opaque-parent",
                          "recipient_target": "opaque-recipient"}
            self.demo.permit({"operation": "execute_operator_linked_replacement",
                              "proposal": competing})
            status, _, raw = request(
                surface, "POST", "/api/linked-replacements", token=token,
                payload={**body, "operation_key": "d" * 32})
            assert status == 503, raw.decode()
            assert self.demo.state()["workspace"]["deliveries"] == before
            surface.stop()
            surface.start()
            token, _ = login(surface)
            status, _, raw = request(surface, "POST", "/api/linked-replacements/lookup",
                                     token=token, payload=body)
            assert status == 200, raw.decode()
            assert json.loads(raw)["status"] == "already_recorded"
            assert self.demo.state()["workspace"]["deliveries"] == before
            assert len(self.demo.state()["workspace"]["deliveries"]) == len(initial["deliveries"]) + 1
        finally:
            surface.stop()

    def test_link_route_rejects_forged_target_without_mutation(self):
        authority = self._authority()
        handle, _proposal = self._install(authority)
        adapter = OperatorLinkedReplacementAdapter(self.demo.runtime, handle)
        surface = ui.WorkspaceSurface(self.demo.runtime._coordinator, self.demo.workspace_id,
                                      operator_linked_replacements=adapter,
                                      presentation_key=b"m" * 32)
        surface.start()
        try:
            token, _ = login(surface)
            before = copy.deepcopy(self.demo.state()["workspace"])
            status, _, _ = request(surface, "POST", "/api/linked-replacements", token=token,
                                   payload={"operation_key": "b" * 32,
                                            "parent_target": "forged",
                                            "recipient_target": "forged"})
            assert status in {400, 403, 409}
            assert self.demo.state()["workspace"] == before
        finally:
            surface.stop()

    def test_ordinary_host_reinstalls_link_authority_after_runtime_reopen(self, tmp_path):
        host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
        host.start()
        try:
            demo = host.demo
            state = demo.state()["workspace"]
            parent_id = next(iter(state["deliveries"]))
            hold_source = demo.source({"kind": "entry-linked-hold", "delivery_id": parent_id})
            hold_ref = demo.evidence(hold_source, "main-1", "entry-linked-hold",
                                      "observation", parent_id, "held_for_recovery", "hold_observation")
            held = copy.deepcopy(state["deliveries"][parent_id])
            held.update(state="held_for_recovery", reason="recipient_drift", ack_level=None,
                        certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
            event = demo.event("workspace_delivery_advanced", {
                "delivery": held, "evidence": {"hold_observation": hold_ref},
                "supported_ack_levels": []}, "entry-linked-hold-transition")
            host._record_install_command(event)
            demo.runtime.record_event(event)
            parent = demo.state()["workspace"]["deliveries"][parent_id]
            recipient = copy.deepcopy(parent["recipient"])
            sources = {}
            for role, category in (("authenticated_disposition", "event"),
                                   ("remaining_authority", "grant"),
                                   ("fresh_attempt_grant", "grant"),
                                   ("physical_fence", "fence")):
                material = {"schema": "summon.workspace.linked-replacement-authority/v1",
                            "role": role, "parent_delivery_id": parent_id,
                            "task_id": "main-1", "recipient": recipient,
                            "generation": 3, "expires_at_ms": 10_000_000,
                            "revoked": False}
                source = demo.source(material)
                sources[role] = demo.evidence(source, "main-1", "entry-linked-" + role,
                                              category, parent_id)
            authority = {"parent_delivery_id": parent_id, "parent_target": "opaque-parent",
                         "recipient_target": "opaque-recipient", "task_id": "main-1",
                         "recipient": recipient, "generation": 3, "now_ms": 1,
                         "sources": {role: {"reference": ref,
                             "parent_delivery_id": parent_id, "task_id": "main-1",
                             "recipient": copy.deepcopy(recipient), "generation": 3,
                             "expires_at_ms": 10_000_000, "revoked": False}
                             for role, ref in sources.items()}}
            scope = {"workspace_id": host.workspace_id, "run_id": host.run_id,
                     "targets": [{"parent_target": "opaque-parent",
                                   "recipient_targets": ["opaque-recipient"]}]}
            proposal = {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
                        "action": admission.LINKED_REPLACEMENT_ACTION,
                        "operation_key": "c" * 32,
                        "parent_target": "opaque-parent", "recipient_target": "opaque-recipient"}
            installed = host.install_operator_linked_replacements(
                scope, authorize_link=lambda _request, _workspace: True,
                resolve_link=lambda _parent, _recipient, _workspace: copy.deepcopy(authority))
            assert installed["provider_calls"] == 0
            token, _ = login(host.surface)
            view = json.loads(request(host.surface, token=token)[2])
            target = view["operator_linked_replacements"]["targets"][0]
            body = {"operation_key": "c" * 32,
                    "parent_target": target["parent_target"],
                    "recipient_target": target["recipient_target"]}
            status, _, raw = request(host.surface, "POST", "/api/linked-replacements",
                                     token=token, payload=body)
            assert status == 200, raw.decode()
            assert json.loads(raw)["status"] == "recorded"
        finally:
            host.stop()

        reopened = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="open")
        reopened.start()
        try:
            token, _ = login(reopened.surface)
            before_install = json.loads(request(reopened.surface, token=token)[2])
            assert before_install["operator_linked_replacements"] == {
                "available": False, "targets": [], "reason": "linked_scope_required"}
            reopened.install_operator_linked_replacements(
                scope, authorize_link=lambda _request, _workspace: True,
                resolve_link=lambda _parent, _recipient, _workspace: copy.deepcopy(authority))
            view = json.loads(request(reopened.surface, token=token)[2])
            target = view["operator_linked_replacements"]["targets"][0]
            body = {"operation_key": "c" * 32,
                    "parent_target": target["parent_target"],
                    "recipient_target": target["recipient_target"]}
            status, _, raw = request(reopened.surface, "POST", "/api/linked-replacements/lookup",
                                     token=token, payload=body)
            assert status == 200, raw.decode()
            assert json.loads(raw)["status"] == "already_recorded"
        finally:
            reopened.stop()
