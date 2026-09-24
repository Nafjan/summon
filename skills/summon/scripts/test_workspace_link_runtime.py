"""Provider-free trusted linked-admission and reservation-boundary tests."""
from __future__ import annotations

import copy
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _swarm_coordinator as base
import _workspace_admission as admission
import _workspace_runtime as runtime
from _rundir import OwnerHeldError
from _workspace_demo import ConductorDemo


class TestLinkedRuntime:
    def setup_method(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-linked-runtime-")
        self.demo = ConductorDemo(Path(self.temp.name) / "workspaces")
        self.demo.prepare()
        # Freeze the local coordinator clock so the capacity boundary and
        # owner-frozen journal bytes are reproducible on every platform.
        self.demo.runtime._coordinator.clock = lambda: 1700000000.123
        grant = self.demo.grant("main-1", "worker-a-1")[0]
        self.entry = self.demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")
        self._hold_parent()

    def teardown_method(self):
        self.demo.cleanup()
        self.temp.cleanup()

    def _hold_parent(self):
        parent_id = self.entry["delivery_id"]
        observation = self.demo.source({
            "kind": "linked-hold-observation", "delivery_id": parent_id,
            "certainty": {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"},
        })
        hold_ref = self.demo.evidence(
            observation, "main-1", "linked-hold-observation", "observation", parent_id,
            "held_for_recovery", "hold_observation")
        held = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][parent_id])
        held.update(state="held_for_recovery", reason="recipient_drift", ack_level=None,
                    certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
        self.demo.record("workspace_delivery_advanced", {
            "delivery": held, "evidence": {"hold_observation": hold_ref},
            "supported_ack_levels": []}, "linked-hold-transition")

    def _authority(self, *, recipient_instance="worker-b-1"):
        parent_id = self.entry["delivery_id"]
        task_id = "main-1"
        recipient = {"instance_id": recipient_instance, "epoch": 1}
        sources = {}

        def add(role, category):
            value = {
                "schema": "summon.workspace.linked-replacement-authority/v1",
                "role": role, "parent_delivery_id": parent_id, "task_id": task_id,
                "recipient": copy.deepcopy(recipient), "generation": 3,
                "expires_at_ms": 1000, "revoked": False,
            }
            source = self.demo.source(value)
            sources[role] = self.demo.evidence(
                source, task_id, "linked-" + role, category, parent_id)

        add("authenticated_disposition", "event")
        add("remaining_authority", "grant")
        add("fresh_attempt_grant", "grant")
        add("physical_fence", "fence")
        add("recipient_transfer_authority", "grant")
        return {
            "parent_delivery_id": parent_id, "parent_target": "opaque-parent",
            "recipient_target": "opaque-recipient", "task_id": task_id,
            "recipient": recipient, "generation": 3, "now_ms": 10,
            "sources": {
                role: {**{"reference": reference},
                       "parent_delivery_id": parent_id, "task_id": task_id,
                       "recipient": copy.deepcopy(recipient), "generation": 3,
                       "expires_at_ms": 1000, "revoked": False}
                for role, reference in sources.items()
            },
        }

    def _install(self, authority):
        proposal = {"schema": "summon.workspace.linked-replacement-proposal/v1",
                    "action": "propose_linked_replacement", "operation_key": "a" * 32,
                    "parent_target": "opaque-parent", "recipient_target": "opaque-recipient"}
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"parent_target": "opaque-parent",
                               "recipient_targets": ["opaque-recipient"]}]}
        self.demo.permit({"operation": "install_operator_linked_replacements", "scope": scope})
        handle = self.demo.runtime.install_operator_linked_replacements(
            scope, authorize_link=lambda _request, _workspace: True,
            resolve_link=lambda _parent, _recipient, _workspace: copy.deepcopy(authority))
        self.demo.permit({"operation": "execute_operator_linked_replacement", "proposal": proposal})
        return handle, proposal

    def test_trusted_sources_queue_one_child_and_same_key_reconciles(self):
        authority = self._authority()
        handle, proposal = self._install(authority)
        parent_before = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][self.entry["delivery_id"]])
        result = self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        assert result["status"] == "recorded"
        current = self.demo.state()["workspace"]
        child = current["deliveries"][result["delivery_id"]]
        assert child["state"] == "queued"
        assert child["parent_delivery_id"] == self.entry["delivery_id"]
        assert child["inherited_uncertainty"] == ["cleanup", "contact", "spend"]
        assert current["deliveries"][self.entry["delivery_id"]] == parent_before
        repeated = self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        assert repeated["status"] == "already_recorded"
        assert self.demo.state()["workspace"]["deliveries"] == current["deliveries"]
        later = self.demo.source({"kind": "unrelated-later-observation", "scope": "same-workspace"})
        self.demo.evidence(later, "side-1", "unrelated-later", "observation")
        looked_up = self.demo.runtime.reconcile_operator_linked_replacement(handle, proposal)
        assert looked_up["status"] == "already_recorded"

    def test_capacity_refusal_keeps_parent_and_child_projection_unchanged(self):
        authority = self._authority()
        handle, proposal = self._install(authority)
        full_before = self.demo.state()
        before = copy.deepcopy(full_before["workspace"])
        _tagged, _torn, raw_map = self.demo.runtime._coordinator._strict_snapshot()
        used = sum(len(value) for value in raw_map.values())
        with patch.object(base, "MAX_JOURNAL_BYTES", used + 1):
            with pytest.raises(runtime.WorkspaceRuntimeError, match="linked_replacement_capacity_refused"):
                self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        after = self.demo.state()["workspace"]
        assert after == before

    def test_authority_expiry_race_refuses_at_owner_append_boundary(self):
        authority = self._authority()
        calls = {"count": 0}
        def resolve(parent_target, recipient_target, workspace):
            calls["count"] += 1
            value = copy.deepcopy(authority)
            if calls["count"] >= 3:
                value["now_ms"] = 1000
                value["sources"]["physical_fence"]["expires_at_ms"] = 999
            return value
        proposal = {"schema": "summon.workspace.linked-replacement-proposal/v1",
                    "action": "propose_linked_replacement", "operation_key": "c" * 32,
                    "parent_target": "opaque-parent", "recipient_target": "opaque-recipient"}
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"parent_target": "opaque-parent",
                               "recipient_targets": ["opaque-recipient"]}]}
        self.demo.permit({"operation": "install_operator_linked_replacements", "scope": scope})
        handle = self.demo.runtime.install_operator_linked_replacements(
            scope, authorize_link=lambda _request, _workspace: True, resolve_link=resolve)
        self.demo.permit({"operation": "execute_operator_linked_replacement", "proposal": proposal})
        full_before = self.demo.state()
        before = copy.deepcopy(full_before["workspace"])
        with pytest.raises(runtime.WorkspaceRuntimeError, match="authority_invalid"):
            self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        assert calls["count"] >= 3
        assert self.demo.state()["workspace"] == before

    def test_simultaneous_competing_owners_have_one_successor_and_honest_loser(self):
        """The owner fence admits at most one linked successor for a parent.

        Both proposals are independently authorized before the race.  The
        resolver barrier makes the two callers observe the same pre-append
        authority, so a successful result from both callers would indicate a
        lost owner fence rather than a test-ordering artifact.
        """
        authority = self._authority()
        proposal_a = {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
                      "action": admission.LINKED_REPLACEMENT_ACTION,
                      "operation_key": "e" * 32,
                      "parent_target": "opaque-parent",
                      "recipient_target": "opaque-recipient"}
        proposal_b = {**proposal_a, "operation_key": "f" * 32}
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"parent_target": "opaque-parent",
                               "recipient_targets": ["opaque-recipient"]}]}
        self.demo.permit({"operation": "install_operator_linked_replacements", "scope": scope})
        parent_before = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][self.entry["delivery_id"]])
        first_resolution = threading.Barrier(2)
        seen = set()
        seen_lock = threading.Lock()

        def resolve(parent_target, recipient_target, workspace):
            with seen_lock:
                marker = threading.get_ident()
                first = marker not in seen
                seen.add(marker)
            if first:
                first_resolution.wait(timeout=5)
            return copy.deepcopy(authority)

        handle = self.demo.runtime.install_operator_linked_replacements(
            scope, authorize_link=lambda _request, _workspace: True,
            resolve_link=resolve)
        for proposal in (proposal_a, proposal_b):
            self.demo.permit({"operation": "execute_operator_linked_replacement",
                              "proposal": proposal})

        def run(proposal):
            try:
                return ("recorded", self.demo.runtime.execute_operator_linked_replacement(
                    handle, proposal))
            except runtime.WorkspaceRuntimeError as error:
                return ("error", error.kind)
            except OwnerHeldError:
                # The coordinator's single-owner lease is itself an honest
                # loser outcome: a competing writer cannot enter the append
                # boundary while the winner owns the run.
                return ("error", "owner_held")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (proposal_a, proposal_b)))
        recorded = [value for kind, value in results if kind == "recorded"]
        errors = [value for kind, value in results if kind == "error"]
        assert len(recorded) == 1, results
        assert len(errors) == 1, results
        assert errors[0] in {
            "journal_snapshot_changed", "linked_replacement_operation_conflict",
            "linked_replacement_authority_changed", "linked_replacement_append_uncertain",
            "owner_held",
        }, results

        current = self.demo.state()["workspace"]
        parent_id = self.entry["delivery_id"]
        children = [delivery for delivery in current["deliveries"].values()
                    if delivery.get("parent_delivery_id") == parent_id]
        assert len(children) == 1
        assert children[0]["delivery_id"] == recorded[0]["delivery_id"]
        assert current["deliveries"][parent_id] == parent_before
        assert children[0]["inherited_uncertainty"] == ["cleanup", "contact", "spend"]

    def test_handle_revocation_during_final_resolution_refuses_before_append(self):
        authority = self._authority()
        calls = {"count": 0, "handle": None}
        def resolve(parent_target, recipient_target, workspace):
            calls["count"] += 1
            if calls["count"] >= 3 and calls["handle"] is not None:
                self.demo.runtime.revoke_operator_linked_replacements(calls["handle"])
            return copy.deepcopy(authority)
        proposal = {"schema": "summon.workspace.linked-replacement-proposal/v1",
                    "action": "propose_linked_replacement", "operation_key": "d" * 32,
                    "parent_target": "opaque-parent", "recipient_target": "opaque-recipient"}
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"parent_target": "opaque-parent",
                               "recipient_targets": ["opaque-recipient"]}]}
        self.demo.permit({"operation": "install_operator_linked_replacements", "scope": scope})
        handle = self.demo.runtime.install_operator_linked_replacements(
            scope, authorize_link=lambda _request, _workspace: True, resolve_link=resolve)
        calls["handle"] = handle
        self.demo.permit({"operation": "execute_operator_linked_replacement", "proposal": proposal})
        before = copy.deepcopy(self.demo.state()["workspace"])
        with pytest.raises(runtime.WorkspaceRuntimeError, match="installed_operator_authority_required"):
            self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        assert calls["count"] >= 3
        assert self.demo.state()["workspace"] == before

    def test_missing_or_mismatched_source_bytes_refuse_before_append(self):
        authority = self._authority()
        handle, proposal = self._install(authority)
        bad_role = authority["sources"]["physical_fence"]
        registered = self.demo.state()["workspace"]["evidence"][bad_role["reference"]["id"]]
        raw = self.demo.resolve_source(registered)
        assert b'"role":"physical_fence"' in raw
        tampered = raw.replace(b'"revoked":false', b'"revoked":true')
        original_resolve = self.demo.runtime._resolve_evidence
        def tampered_resolve(value):
            return tampered if value["reference"] == registered["reference"] else original_resolve(value)
        self.demo.runtime._resolve_evidence = tampered_resolve
        before = copy.deepcopy(self.demo.state()["workspace"])
        with pytest.raises(runtime.WorkspaceRuntimeError):
            self.demo.runtime.execute_operator_linked_replacement(handle, proposal)
        assert self.demo.state()["workspace"] == before

    def test_retain_keeps_recovery_reservation_when_capacity_is_exhausted(self):
        parent_id = self.entry["delivery_id"]
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"delivery_id": parent_id,
                               "actions": ["retain_held_context"]}]}
        self.demo.permit({"operation": "install_operator_commands", "scope": scope})
        handle = self.demo.runtime.install_operator_commands(
            scope, authorize_command=lambda _request, _workspace: True)
        operation_key = "b" * 32
        proof_refs = {}
        for role in ("request", "decision"):
            source = self.demo.source({
                "schema": admission.OPERATOR_DISPOSITION_PROOF_SCHEMA,
                "role": role, "operation_key": operation_key,
                "action": admission.OPERATOR_DISPOSITION_ACTION,
                "delivery_id": parent_id, "task_id": "main-1",
                "reason": "awaiting_evidence", "authority": "installed_operator_policy",
            })
            proof_refs[role] = self.demo.evidence(
                source, "main-1", "retain-" + role, "event", parent_id)
        event = admission.build_operator_disposition_event(
            self.demo.state()["workspace"], operation_key=operation_key,
            delivery_id=parent_id, task_id="main-1",
            request_ref=proof_refs["request"], decision_ref=proof_refs["decision"],
            reason="awaiting_evidence")
        self.demo.permit(event)
        full_before = self.demo.state()
        before = copy.deepcopy(full_before["workspace"])
        _tagged, _torn, raw_map = self.demo.runtime._coordinator._strict_snapshot()
        used = sum(len(value) for value in raw_map.values())
        request = {"operation_key": operation_key, "delivery_id": parent_id,
                   "task_id": "main-1", "reason": "awaiting_evidence"}
        plan = self.demo.runtime._operator_disposition_request(
            handle, request, before)
        now = int(self.demo.runtime._coordinator.clock() * 1000)
        wrapper = {"event": "workspace_event", "workspace_event": plan["event"],
                   "observed_at_ms": now}
        reserve = self.demo.runtime._coordinator._reserve_after(full_before, wrapper)
        # Set the limit from the exact frozen bytes produced by each owner
        # attempt. Owner generations can advance after a refused append, so a
        # precomputed generation/length would be flaky across isolated runs.
        captured = []
        original_encode = base.encode_journal_record
        def lower_capture(record, *args, **kwargs):
            raw = original_encode(record, *args, **kwargs)
            if (isinstance(record, dict) and record.get("event") == "workspace_event"
                    and isinstance(record.get("workspace_event"), dict)
                    and record["workspace_event"].get("operation_key") == plan["event"]["operation_key"]):
                captured.append(len(raw))
                base.MAX_JOURNAL_BYTES = used + len(raw) + reserve - 1
            return raw
        default_capacity = base.MAX_JOURNAL_BYTES
        with patch.object(base, "MAX_JOURNAL_BYTES", default_capacity):
            with patch.object(base, "encode_journal_record", side_effect=lower_capture):
                result = self.demo.runtime.execute_operator_disposition(handle, request)
        assert result["status"] == "uncertain"
        assert captured
        assert self.demo.state()["workspace"] == before
        exact_capture = []
        def exact_encode(record, *args, **kwargs):
            raw = original_encode(record, *args, **kwargs)
            if (isinstance(record, dict) and record.get("event") == "workspace_event"
                    and isinstance(record.get("workspace_event"), dict)
                    and record["workspace_event"].get("operation_key") == plan["event"]["operation_key"]):
                exact_capture.append(len(raw))
                base.MAX_JOURNAL_BYTES = used + len(raw) + reserve
            return raw
        with patch.object(base, "MAX_JOURNAL_BYTES", default_capacity):
            with patch.object(base, "encode_journal_record", side_effect=exact_encode):
                result = self.demo.runtime.execute_operator_disposition(handle, request)
        assert result["status"] == "recorded"
        assert exact_capture
        assert reserve > 0
        full_after = self.demo.state()
        after = full_after["workspace"]
        assert after["deliveries"][parent_id] == before["deliveries"][parent_id]
        assert self.demo.runtime._coordinator._workspace_reserve(after) == \
            self.demo.runtime._coordinator._workspace_reserve(before)

    def test_linked_child_advances_then_typed_retain_dispose_and_lookup_preserve_lineage(self):
        """A linked child can use the typed held-context controls safely."""
        authority = self._authority()
        link_handle, proposal = self._install(authority)
        linked = self.demo.runtime.execute_operator_linked_replacement(link_handle, proposal)
        child_id = linked["delivery_id"]
        before_parent = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][self.entry["delivery_id"]])
        child_before = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][child_id])

        unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        hold_source = self.demo.source({"kind": "linked-child-hold", "delivery_id": child_id,
                                        "certainty": unknown})
        hold_ref = self.demo.evidence(hold_source, "main-1", "linked-child-hold", "observation",
                                      child_id, "held_for_recovery", "hold_observation")
        held = copy.deepcopy(child_before)
        held.update(state="held_for_recovery", reason="recipient_drift", ack_level=None,
                    certainty=unknown)
        hold_event = self.demo.event("workspace_delivery_advanced", {
            "delivery": held, "evidence": {"hold_observation": hold_ref},
            "supported_ack_levels": []}, "linked-child-hold-transition")
        self.demo.permit(hold_event)
        self.demo.runtime.record_event(hold_event)

        retain_key = "1" * 32
        proof_refs = {}
        for role in ("request", "decision"):
            proof = {"schema": admission.OPERATOR_DISPOSITION_PROOF_SCHEMA,
                     "role": role, "operation_key": retain_key,
                     "action": admission.OPERATOR_DISPOSITION_ACTION,
                     "delivery_id": child_id, "task_id": "main-1",
                     "reason": "awaiting_evidence", "authority": "installed_operator_policy"}
            proof_refs[role] = self.demo.evidence(
                self.demo.source(proof), "main-1", "linked-child-retain-" + role,
                "event", child_id)
        retain_event = admission.build_operator_disposition_event(
            self.demo.state()["workspace"], operation_key=retain_key,
            delivery_id=child_id, task_id="main-1",
            request_ref=proof_refs["request"], decision_ref=proof_refs["decision"],
            reason="awaiting_evidence")
        self.demo.permit(retain_event)

        command_scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                         "targets": [{"delivery_id": child_id,
                                       "actions": ["retain_held_context", "dispose_held_context"]}]}
        self.demo.permit({"operation": "install_operator_commands", "scope": command_scope})
        command_handle = self.demo.runtime.install_operator_commands(
            command_scope, authorize_command=lambda _request, _workspace: True)
        retained = self.demo.runtime.execute_operator_disposition(
            command_handle, {"operation_key": retain_key, "delivery_id": child_id,
                             "task_id": "main-1", "reason": "awaiting_evidence"})
        assert retained["status"] == "recorded"
        looked_up_retain = self.demo.runtime.reconcile_operator_disposition(
            command_handle, {"operation_key": retain_key, "delivery_id": child_id,
                             "task_id": "main-1", "reason": "awaiting_evidence"})
        assert looked_up_retain["status"] == "recorded"

        from _workspace_commands import CommandScope, bind_command
        from _workspace_view import ViewScope, _opaque
        view = ViewScope(self.demo.workspace_id, self.demo.run_id, b"l" * 32, audience="operator")
        commands = CommandScope(self.demo.workspace_id, self.demo.run_id,
                                ((child_id, ("retain_held_context", "dispose_held_context")),))
        dispose_body = {"operation_key": "2" * 32, "action": "dispose_held_context",
                        "target": _opaque(view, "delivery", child_id)}
        binding = bind_command(self.demo.state()["workspace"], view_scope=view,
                               command_scope=commands, body=dispose_body)
        disposed = self.demo.runtime.execute_operator_command(command_handle, binding.source_bytes)
        assert disposed["status"] == "recorded"
        looked_up_dispose = self.demo.runtime.reconcile_operator_command(
            command_handle, binding.source_bytes)
        assert looked_up_dispose["status"] == "recorded"
        linked_lookup = self.demo.runtime.reconcile_operator_linked_replacement(
            link_handle, proposal)
        assert linked_lookup["status"] == "already_recorded"
        retained_after_dispose = self.demo.runtime.reconcile_operator_disposition(
            command_handle, {"operation_key": retain_key, "delivery_id": child_id,
                             "task_id": "main-1", "reason": "awaiting_evidence"})
        assert retained_after_dispose["status"] == "recorded"

        final = self.demo.state()["workspace"]
        assert final["deliveries"][self.entry["delivery_id"]] == before_parent
        child = final["deliveries"][child_id]
        assert child["state"] == "dead_lettered"
        assert child["parent_delivery_id"] == child_before["parent_delivery_id"]
        assert child["message_id"] == child_before["message_id"]
        assert child["inherited_uncertainty"] == ["cleanup", "contact", "spend"]
        assert child["certainty"] == unknown

    def test_reserved_recovery_append_remains_possible_after_retain(self):
        """A retained hold still admits its separately authorized recovery."""
        parent_id = self.entry["delivery_id"]
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"delivery_id": parent_id,
                               "actions": ["retain_held_context"]}]}
        self.demo.permit({"operation": "install_operator_commands", "scope": scope})
        handle = self.demo.runtime.install_operator_commands(
            scope, authorize_command=lambda _request, _workspace: True)
        operation_key = "e" * 32
        proof_refs = {}
        for role in ("request", "decision"):
            source = self.demo.source({
                "schema": admission.OPERATOR_DISPOSITION_PROOF_SCHEMA,
                "role": role, "operation_key": operation_key,
                "action": admission.OPERATOR_DISPOSITION_ACTION,
                "delivery_id": parent_id, "task_id": "main-1",
                "reason": "awaiting_evidence", "authority": "installed_operator_policy",
            })
            proof_refs[role] = self.demo.evidence(
                source, "main-1", "recovery-retain-" + role, "event", parent_id)
        retain_event = admission.build_operator_disposition_event(
            self.demo.state()["workspace"], operation_key=operation_key,
            delivery_id=parent_id, task_id="main-1",
            request_ref=proof_refs["request"], decision_ref=proof_refs["decision"],
            reason="awaiting_evidence")
        self.demo.permit(retain_event)
        retain_request = {"operation_key": operation_key, "delivery_id": parent_id,
                          "task_id": "main-1", "reason": "awaiting_evidence"}
        assert self.demo.runtime.execute_operator_disposition(handle, retain_request)["status"] == "recorded"

        recovery_source = self.demo.source({
            "schema": "summon.workspace.recovery-disposition/v1",
            "role": "authenticated_disposition", "delivery_id": parent_id,
            "task_id": "main-1", "decision": "dead_letter_parent_after_retain",
        })
        recovery_ref = self.demo.evidence(
            recovery_source, "main-1", "recovery-authenticated-disposition", "event",
            parent_id, "dead_lettered", "authenticated_disposition")
        before_full = self.demo.state()
        parent = copy.deepcopy(before_full["workspace"]["deliveries"][parent_id])
        parent.update(state="dead_lettered", reason="explicit_fixture_recovery", ack_level=None)
        event = self.demo.event("workspace_delivery_advanced", {
            "delivery": parent, "evidence": {"authenticated_disposition": recovery_ref},
            "supported_ack_levels": []}, "recovery-after-retain")
        self.demo.permit(event)
        wrapper = {"event": "workspace_event", "workspace_event": event,
                   "observed_at_ms": int(self.demo.runtime._coordinator.clock() * 1000)}
        reserve = self.demo.runtime._coordinator._reserve_after(before_full, wrapper)
        _, _, raw_map = self.demo.runtime._coordinator._strict_snapshot()
        used = sum(len(value) for value in raw_map.values())
        original_encode = base.encode_journal_record
        lower_capture = []
        def capture_lower(record, *args, **kwargs):
            raw = original_encode(record, *args, **kwargs)
            if (isinstance(record, dict) and record.get("event") == "workspace_event"
                    and isinstance(record.get("workspace_event"), dict)
                    and record["workspace_event"].get("operation_key") == event["operation_key"]):
                lower_capture.append(len(raw))
                base.MAX_JOURNAL_BYTES = used + len(raw) + reserve - 1
            return raw
        default_capacity = base.MAX_JOURNAL_BYTES
        with patch.object(base, "MAX_JOURNAL_BYTES", default_capacity):
            with patch.object(base, "encode_journal_record", side_effect=capture_lower):
                with pytest.raises(base.SwarmCoordinatorError):
                    self.demo.runtime.record_event(event)
        assert lower_capture and reserve > 0
        exact_capture = []
        def capture_exact(record, *args, **kwargs):
            raw = original_encode(record, *args, **kwargs)
            if (isinstance(record, dict) and record.get("event") == "workspace_event"
                    and isinstance(record.get("workspace_event"), dict)
                    and record["workspace_event"].get("operation_key") == event["operation_key"]):
                exact_capture.append(len(raw))
                base.MAX_JOURNAL_BYTES = used + len(raw) + reserve
            return raw
        with patch.object(base, "MAX_JOURNAL_BYTES", default_capacity):
            with patch.object(base, "encode_journal_record", side_effect=capture_exact):
                assert self.demo.runtime.record_event(event)["status"] == "recorded"
        assert exact_capture
        after = self.demo.state()["workspace"]
        assert after["deliveries"][parent_id]["state"] == "dead_lettered"
        assert after["deliveries"][parent_id]["certainty"] == before_full["workspace"]["deliveries"][parent_id]["certainty"]
        assert self.demo.runtime._coordinator._workspace_reserve(after) > 0


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
