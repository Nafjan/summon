"""Real-base public admission/refusal tests and pure replay integration.

No fixture enables a fake successful admission contract. Actual owned-process
journeys and failure boundaries are exercised separately in the demo tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from _fleet_approval import _secure_private_root
from _swarm_coordinator import SwarmCoordinator, SwarmCorruptError
from _workspace_content import ContentStore, prepare_content
from _workspace_content import ContentError
from _workspace_layout import create_workspace_layout
import _workspace_runtime as runtime
from test_workspace_protocol import goal, lane


def plan():
    lanes = []
    for name in ("main-1", "side-1", "main-2"):
        item = lane()
        item.update(task_id=name, lane_id=name,
                    role="investigation" if name == "side-1" else "main")
        lanes.append(item)
    return runtime.goal_plan(goal(), lanes, operation_prefix="definition")


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-workspace-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name) / "runs"
        _secure_private_root(str(self.runs))
        self.layout = create_workspace_layout(self.runs, "run")
        self.content_root = Path(self.layout.inner_run_directory) / "workspace-content"
        self.authorize = Mock(return_value=False)
        self.resolve = Mock(side_effect=AssertionError("no unqualified evidence access"))
        self.runtime = runtime.WorkspaceRuntime(
            self.runs, "run", "workspace",
            authorize=self.authorize, resolve_evidence=self.resolve, clock=lambda: 1000.0)

    def base(self):
        return SwarmCoordinator.create(
            self.layout.inner_runs_root, "run", project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64, tasks=plan()["tasks"],
            max_attempts=1, clock=lambda: 1000.0)

    def snapshot(self):
        return {str(p.relative_to(self.temp.name)): p.read_bytes()
                for p in Path(self.temp.name).rglob("*") if p.is_file()}

    def assert_refused(self, function, *args, **kwargs):
        before = self.snapshot()
        unavailable = SwarmCoordinator.workspace_admission_contract()
        unavailable.update(event_classes=[], atomic_claim_selection=False)
        # Explicit unavailable-capability negative fixture only; never enable
        # fictional support to make a success test pass.
        with patch.object(SwarmCoordinator, "workspace_admission_contract", return_value=unavailable):
            with self.assertRaises(runtime.WorkspaceRuntimeError) as caught:
                function(*args, **kwargs)
        self.assertEqual(caught.exception.kind, "workspace_admission_unqualified")
        self.assertEqual(self.snapshot(), before)
        self.authorize.assert_not_called()
        self.resolve.assert_not_called()

    def test_real_public_prepare_uses_actual_enabled_base_contract(self):
        self.authorize.return_value = True
        result = self.runtime.prepare(plan(), project_root_sha256="a" * 64,
                                      roster_definition_sha256="b" * 64)
        self.assertEqual(result["status"], "inspected")
        self.assertEqual(len(result["projection"]["lanes"]), 3)
        self.assertEqual(self.runtime._coordinator.status()["status"], "prepared")

    def test_runtime_requires_the_budget_contract_shape_without_authorizing_work(self):
        before = self.snapshot()
        contract = SwarmCoordinator.workspace_admission_contract()
        contract["budget_preflight"] = {
            "policy_schema": "summon.workspace.admission-policy/v1",
            "snapshot_schema": "summon.workspace.admission-snapshot/v1",
            "request_schema": "summon.workspace.admission-request/v1",
            "result_schema": "summon.workspace.admission-result/v1",
            "authoritative_inputs_required": False,
            "durable_execution_authorization": False,
        }
        with patch.object(SwarmCoordinator, "workspace_admission_contract",
                          return_value=contract):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError,
                                        "workspace_admission_unqualified"):
                self.runtime.prepare(
                    plan(), project_root_sha256="a" * 64,
                    roster_definition_sha256="b" * 64)
        self.assertEqual(self.snapshot(), before)
        self.authorize.assert_not_called()
        self.resolve.assert_not_called()

    def test_generic_event_api_refuses_all_authenticated_inbox_composites_before_authority_or_mutation(self):
        before = self.snapshot()
        for event_class in sorted(runtime.SUPERVISOR_EVENTS | {"workspace_message_sent", "workspace_operator_message_sent"}):
            with self.subTest(event_class=event_class):
                with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "unsupported_workspace_event"):
                    self.runtime.record_event({"event": event_class, "workspace_id": "workspace", "run_id": "run"})
        self.authorize.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_prepare_attempt_budget_is_declared_bounded_and_independently_authorized(self):
        original = plan()
        lanes = [copy.deepcopy(event["payload"]["lane"]) for event in original["events"][2:]]
        for item in lanes:
            item["budget"]["max_attempts"] = 2
        two = runtime.goal_plan(original["events"][1]["payload"]["goal"], lanes, operation_prefix="definition")
        before = self.snapshot()
        for value in (True, 0, 3, runtime.MAX_ATTEMPTS + 1):
            with self.subTest(max_attempts=value), self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "prepared_attempt_budget_invalid"):
                self.runtime.prepare(two, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64, max_attempts=value)
        self.authorize.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "command_not_authorized"):
            self.runtime.prepare(two, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64, max_attempts=2)
        self.assertEqual(self.authorize.call_args.args[0], {"operation": "prepare", "plan": two, "max_attempts": 2})
        self.assertEqual(self.snapshot(), before)
        self.authorize.return_value = True
        self.runtime.prepare(two, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64, max_attempts=2)
        state, _ = self.runtime._coordinator._load()
        self.assertEqual(state["max_attempts"], 2)
        self.assertTrue(all(item["budget"]["max_attempts"] == 2 for item in state["workspace"]["lanes"].values()))
        with self.assertRaises((ValueError, RuntimeError)):
            self.runtime.prepare(two, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64, max_attempts=2)
        self.assertEqual(self.runtime._coordinator._load()[0]["max_attempts"], 2)

    def test_goal_plan_has_distinct_tasks_without_execution_authority(self):
        prepared = plan()
        self.assertEqual(prepared["status"], "proposed")
        self.assertFalse(prepared["execution_authorized"])
        self.assertEqual([task["task_id"] for task in prepared["tasks"]], ["main-1", "side-1", "main-2"])
        self.assertEqual(len(prepared["events"]), 5)
        self.assertEqual(prepared["events"][0]["event"], "workspace_feature")
        self.assertEqual(len(set(event["operation_key"] for event in prepared["events"])), 5)

    def test_goal_plan_rejects_duplicate_lane_tasks(self):
        with self.assertRaises(runtime.WorkspaceRuntimeError):
            runtime.goal_plan(goal(), [lane(), lane()], operation_prefix="definition")

    def test_empty_published_layout_inspection_is_nonmutating(self):
        before = self.snapshot()
        self.assertEqual(self.runtime.inspect()["status"], "unprepared")
        self.assertEqual(self.snapshot(), before)

    def test_real_legacy_inner_run_is_not_fabricated_as_workspace(self):
        self.base()
        before = self.snapshot()
        self.assertEqual(self.runtime.inspect()["status"], "workspace_unprepared")
        self.assertEqual(self.snapshot(), before)

    def test_prepare_refuses_before_base_create_without_r13_hook(self):
        self.assert_refused(self.runtime.prepare, plan(),
                            project_root_sha256="a" * 64, roster_definition_sha256="b" * 64)

    def test_metadata_event_refuses_before_mutation_without_r13_hook(self):
        self.base()
        self.assert_refused(self.runtime.record_event, plan()["events"][0])

    def test_context_send_refuses_before_blob_write_without_r13_hook(self):
        self.base()
        self.assert_refused(self.runtime.send_context, {"event": "workspace_message_admitted"}, "body")
        self.assertFalse(self.content_root.exists())

    def test_turn_refuses_before_claim_without_r13_hook(self):
        coordinator = self.base()
        grant = Mock(side_effect=AssertionError("unqualified grant resolution"))
        self.assert_refused(self.runtime.admit_turn, "worker-a", {"task_id": "main-1"}, resolve_grant=grant)
        grant.assert_not_called()
        self.assertEqual(coordinator.status()["tasks"][0]["attempts"], 0)

    def test_private_content_missing_returns_honest_nondurable_hold(self):
        self.base()
        self.runtime._provision_content_after_create()
        ref = prepare_content("body")
        result = self.runtime.inspect_context(ref)
        self.assertEqual(result, {"status": "held", "reason": "content_missing",
                                  "diagnostic_kind": "content_missing", "durable": False})
        self.runtime._content_store().put(ref, "body", require_namespace_durable=False)
        self.assertEqual(self.runtime.inspect_context(ref), {"status": "verified", "bytes": b"body"})

    def test_content_lifecycle_is_after_real_base_creation_inside_inner_run(self):
        self.assertFalse(self.content_root.exists())
        base = self.base()  # would reject a prematurely provisioned directory
        self.runtime._provision_content_after_create()
        self.assertEqual(self.content_root.parent, Path(base.run_dir))
        self.assertTrue(self.content_root.is_dir())
        reopened = runtime.WorkspaceRuntime(self.runs, "run", "workspace",
            authorize=self.authorize, resolve_evidence=self.resolve)
        self.assertEqual(reopened._content_store()._root, str(self.content_root))
        self.assertEqual(base.status()["status"], "prepared")

    def test_external_content_root_is_refused(self):
        foreign = Path(self.temp.name) / "external-content"
        _secure_private_root(str(foreign))
        with self.assertRaises(runtime.WorkspaceRuntimeError) as caught:
            runtime.WorkspaceRuntime(self.runs, "run", "workspace", content=ContentStore(foreign),
                authorize=self.authorize, resolve_evidence=self.resolve)
        self.assertEqual(caught.exception.kind, "content_root_scope_mismatch")

    def test_new_provisions_scaffold_without_claiming_admitted_workspace(self):
        root = Path(self.temp.name) / "new-private-workspaces"
        created = runtime.WorkspaceRuntime.new(root, "new-run", "new-workspace",
            authorize=self.authorize, resolve_evidence=self.resolve)
        self.assertEqual(created.inspect()["status"], "unprepared")
        self.assertFalse(Path(created._content_root).exists())
        self.authorize.assert_not_called()

    def test_new_refuses_foreign_private_root_without_modifying_it(self):
        foreign = Path(self.temp.name) / "foreign"
        _secure_private_root(str(foreign))
        (foreign / "room-history").write_bytes(b"existing")
        before = self.snapshot()
        with self.assertRaises(runtime.WorkspaceRuntimeError):
            runtime.WorkspaceRuntime.new(foreign, "run", "workspace",
                authorize=self.authorize, resolve_evidence=self.resolve)
        self.assertEqual(self.snapshot(), before)

    def test_new_does_not_repair_unprivate_existing_root(self):
        with patch.object(runtime, "_verify_private", side_effect=ValueError("unsafe")), \
                patch.object(runtime, "_secure_private_root") as secure:
            with self.assertRaises(runtime.WorkspaceRuntimeError):
                runtime.WorkspaceRuntime.new(self.runs, "another", "workspace",
                    authorize=self.authorize, resolve_evidence=self.resolve)
            secure.assert_not_called()

    def test_content_failures_keep_missing_mismatch_and_availability_separate(self):
        self.base()
        self.runtime._provision_content_after_create()
        ref = prepare_content("body")
        mappings = {"content_missing": "content_missing", "content_mismatch": "content_mismatch",
                    "invalid_utf8": "content_mismatch", "store_limit": "content_unavailable",
                    "inventory_unavailable": "content_unavailable", "content_unreadable": "content_unavailable",
                    "private_root_invalid": "content_unavailable"}
        for kind, expected in mappings.items():
            with self.subTest(kind=kind), patch.object(self.runtime._content_store(), "read", side_effect=ContentError(kind)):
                result = self.runtime.inspect_context(ref)
                self.assertEqual(result["reason"], expected)
                self.assertEqual(result["diagnostic_kind"], kind)
                self.assertFalse(result["durable"])

    def test_transport_mapping_requires_receipts_and_never_claims_durability(self):
        for kind, expected in (("full_write", "submission_started"),
                               ("authenticated_frame_received", "submitted"),
                               ("authenticated_acknowledged", "acknowledged"),
                               ("partial_or_unknown", "held_for_recovery"), ("zero_write", "not_submitted")):
            result = runtime.transport_observation_plan(kind)
            self.assertEqual(result["candidate_state"], expected)
            self.assertFalse(result["durable"])
            self.assertFalse(result["evidence_verified"])
            self.assertTrue(result["requires"])
        self.assertEqual(runtime.transport_observation_plan("authenticated_acknowledged")["ack_level"], "recipient_received")
        self.assertEqual(runtime.transport_observation_plan("partial_or_unknown")["reason"], "contact_uncertain")

    def test_native_turn_collision_uses_runtime_admission_boundary(self):
        """A collision is admitted by the runtime, not by a reducer-only fixture."""
        from _workspace_demo import ConductorDemo

        with tempfile.TemporaryDirectory(prefix="summon-native-collision-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                demo.prepare()
                grant, _grant_source = demo.grant("main-1", "worker-a-1")
                demo.launch("main-1", "worker-a-1", grant)
                entry = demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-a-1", grant, [entry])
                demo.start(turn)
                before = demo.state()
                delivery = copy.deepcopy(before["workspace"]["deliveries"][entry["delivery_id"]])
                uncertainty = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
                source = demo.source({"kind": "native-turn-collision",
                                      "delivery_id": entry["delivery_id"],
                                      "attempt_id": turn["response"]["claim_id"],
                                      "qualification": "simulated-adapter-boundary"})
                observation = demo.evidence(
                    source, "main-1", "native-turn-collision", "observation",
                    entry["delivery_id"], "held_for_recovery", "hold_observation")
                delivery.update(state="held_for_recovery", reason="native_turn_collision",
                                certainty=uncertainty)
                event = demo.event(
                    "workspace_delivery_advanced",
                    {"delivery": delivery, "evidence": {"hold_observation": observation},
                     "supported_ack_levels": []},
                    "native-turn-collision")
                demo.permit(event)
                result = demo.runtime.record_transport_observation(
                    event, kind="native_turn_collision", evidence=observation)
                self.assertEqual(result["status"], "recorded")
                after = demo.state()
                held = after["workspace"]["deliveries"][entry["delivery_id"]]
                self.assertEqual(held["state"], "held_for_recovery")
                self.assertEqual(held["reason"], "native_turn_collision")
                self.assertEqual(held["certainty"], uncertainty)
                self.assertEqual(after["tasks"], before["tasks"])
                self.assertEqual(after["claims"], before["claims"])
                self.assertEqual(after["workers"], before["workers"])
                repeated = demo.runtime.record_transport_observation(
                    event, kind="native_turn_collision", evidence=observation)
                self.assertEqual(repeated["status"], "already_recorded")
                self.assertEqual(repeated["revision"], result["revision"])
            finally:
                demo.cleanup()

    def test_subclass_does_not_override_central_mutation_or_append(self):
        for name in ("_mutation", "_append", "_append_with_state", "_append_initial", "_acquire", "_load", "_state"):
            self.assertNotIn(name, runtime._WorkspaceCoordinator.__dict__)
        self.assertIs(runtime._WorkspaceCoordinator._append, SwarmCoordinator._append)
        self.assertIs(runtime._WorkspaceCoordinator._append_with_state, SwarmCoordinator._append_with_state)

    def test_in_memory_extension_replays_against_actual_base_task_records(self):
        # Only the base preparation below is durable. Workspace wrappers remain
        # in memory: this proves reducer wiring, NOT successful runtime admission.
        base = self.base()
        records, torn = base._read_records()
        self.assertFalse(torn)
        before = self.snapshot()
        wrappers = [{"event": runtime.CONTAINER, "workspace_event": event,
                     "observed_at_ms": 1000000, "generation": 1, "ts": 1000.0}
                    for event in plan()["events"]]
        result = self.runtime._coordinator._state(records + wrappers)
        self.assertEqual(result["workspace"]["goal"]["goal_id"], "goal")
        self.assertEqual(set(result["workspace"]["lanes"]), {"main-1", "side-1", "main-2"})
        self.assertEqual(self.snapshot(), before)

    def test_in_memory_wrapper_scope_and_status_snapshot_injection_refuse(self):
        base = self.base()
        records, _ = base._read_records()
        wrapper = {"event": runtime.CONTAINER, "workspace_event": plan()["events"][0],
                   "observed_at_ms": 1000000}
        with self.assertRaises(SwarmCorruptError):
            self.runtime._coordinator._state(records + [{**wrapper, "task_status": "completed"}])
        wrong_run = copy.deepcopy(wrapper)
        wrong_run["workspace_event"]["run_id"] = "another"
        with self.assertRaises(SwarmCorruptError):
            self.runtime._coordinator._state(records + [wrong_run])

    def test_task_view_tracks_actual_claim_and_terminal_without_writer_generation(self):
        base = self.base()
        base.register_worker("worker-a", worker_instance_id="instance-a")
        claim = base.claim("worker-a", "main-1", request_sha256="d" * 64)
        state, _ = base._load()
        view = runtime._task_view(state, run_id="run", now_ms=1000000)
        task = next(task for task in view["tasks"] if task["task_id"] == "main-1")
        self.assertEqual(task["status"], "claimed")
        self.assertEqual(task["active_claim"]["lease_generation"], claim["lease_generation"])
        self.assertEqual(task["attempts"], 1)
        base.complete("worker-a", task_id="main-1", claim_id=claim["claim_id"],
                      attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                      request_sha256="d" * 64, envelope_sha256="e" * 64)
        state, _ = base._load()
        view = runtime._task_view(state, run_id="run", now_ms=1000000)
        task = next(task for task in view["tasks"] if task["task_id"] == "main-1")
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["active_claim"])

    def test_historical_selection_replays_after_expiry_while_live_view_expires(self):
        # Real durable coordinator claim; workspace events remain explicitly
        # synthetic history, not a mock-enabled successful admission path.
        from test_workspace_state import Fixture
        from test_workspace_protocol import message, delivery, evidence_for, ref
        base = self.base()
        base.register_worker("worker-b", worker_instance_id="worker-b")
        claim = base.claim("worker-b", "main-2", request_sha256="d" * 64)
        records, torn = base._read_records()
        self.assertFalse(torn)
        fixture = Fixture()
        fixture.view = base.status()  # actual live claim state, not a claimed status string
        fixture.register(ref("grant"), category="grant")
        fixture.add("workspace_message_admitted", {"message": message(), "delivery": delivery()})
        fixture.register(ref("durable_admission"), delivery_id="delivery")
        fixture.add("workspace_delivery_advanced", {"delivery": delivery("queued"),
                    "evidence": evidence_for("queued"), "supported_ack_levels": []})
        for name in ("selection_record", "whole_message_fit"):
            fixture.register(ref(name), delivery_id="delivery")
        selected = delivery("included_in_attempt")
        selected["selection"].update(claim_id=claim["claim_id"], attempt=claim["attempt"],
                                     owner_generation=claim["lease_generation"], request_sha256="d" * 64)
        fixture.add("workspace_delivery_advanced", {"delivery": selected,
                    "evidence": evidence_for("included_in_attempt"), "supported_ack_levels": []})
        wrappers = [{"event": runtime.CONTAINER, "workspace_event": event,
                     "observed_at_ms": 1000000, "generation": 1, "ts": 1000.0}
                    for event, _view in fixture.steps]
        history = records + wrappers
        before = self.runtime._coordinator._state(history)["workspace"]
        self.runtime._coordinator.clock = lambda: claim["lease_expires_at_ms"] / 1000 + 1
        after = self.runtime._coordinator._state(history)["workspace"]
        self.assertEqual(before, after)
        self.assertEqual(after["deliveries"]["delivery"]["state"], "included_in_attempt")
        # Supply the already-proven synthetic replay to the live projection
        # boundary; the real journal has no admitted workspace events yet.
        with patch.object(self.runtime._coordinator, "_load",
                          side_effect=lambda: (self.runtime._coordinator._state(history), False)):
            inspected = self.runtime.inspect()
        lane_view = next(item for item in inspected["projection"]["lanes"] if item["task_id"] == "main-2")
        self.assertEqual(lane_view["status"], "expired")


    def _real_turn_fixture(self):
        """Real base wrapper preparation; public facade capability stays off."""
        from test_workspace_state import Fixture
        from test_workspace_protocol import message, delivery
        from _workspace_state import ATOMIC_TURN_EVENT
        self.base().register_worker("worker-b", worker_instance_id="worker-b")
        self.runtime._provision_content_after_create()
        fixture = Fixture()
        proof_bytes = b"private synthetic proof"
        def proof(name):
            return {"id": name, "sha256": hashlib.sha256(proof_bytes).hexdigest()}
        fixture.register(proof("grant"), category="grant")
        entries = []
        for index, body in enumerate(("[3,-2,7]", "[4,4,-1]"), 1):
            descriptor = prepare_content(body)
            self.runtime._content_store().put(descriptor, body, require_namespace_durable=False)
            msg = message()
            msg.update(message_id=f"message-{index}", sequence=index, grant_ref=proof("grant"),
                       content={"ref": descriptor.reference, "sha256": descriptor.sha256,
                                "utf8_bytes": descriptor.payload_bytes})
            item = delivery()
            item.update(delivery_id=f"delivery-{index}", message_id=msg["message_id"], grant_ref=proof("grant"))
            fixture.add("workspace_message_admitted", {"message": msg, "delivery": item})
            fixture.register(proof(f"queued-{index}"), delivery_id=item["delivery_id"])
            item["state"] = "queued"
            fixture.add("workspace_delivery_advanced", {"delivery": item,
                "evidence": {"durable_admission": proof(f"queued-{index}")}, "supported_ack_levels": []})
            entries.append({"message_id": msg["message_id"], "delivery_id": item["delivery_id"],
                            "content_sha256": descriptor.sha256, "content_utf8": body})
        evidence_by_delivery = {}
        for index in (1, 2):
            evidence = {"current_grant": proof("grant")}
            for name, category in (("recipient_owner_fence", "fence"), ("capacity_reservation", "fence"),
                                   ("physical_attempt_reservation", "fence"), ("selection_record", "event"),
                                   ("whole_message_fit", "event")):
                reference = proof(f"{name}-{index}")
                evidence[name] = reference
                fixture.register(reference, category=category, delivery_id=f"delivery-{index}")
            evidence_by_delivery[f"delivery-{index}"] = evidence
        coordinator = self.runtime._coordinator
        for event, _view in fixture.steps:
            with coordinator._mutation() as (owner, state, _torn):
                coordinator._append_with_state(owner, {"event": "workspace_event",
                    "workspace_event": event, "observed_at_ms": 1000000}, state=state)
        selection = {"task_id": "main-2", "claim_id": "fresh-claim", "attempt": 1,
                     "owner_generation": 1, "request_sha256": "d" * 64, "context_sha256": "0" * 64,
                     "selected_message_ids": [entry["message_id"] for entry in entries]}
        compiled = runtime.compile_turn_context(selection, entries)
        selection["context_sha256"] = hashlib.sha256(compiled).hexdigest()
        claim = {"task_id": "main-2", "claim_id": "fresh-claim", "worker_id": "worker-b",
                 "attempt": 1, "lease_generation": 1, "lease_expires_at_ms": 1,
                 "request_sha256": "d" * 64, "status": "active", "cancel_requested": False,
                 "cancel_acknowledged": False, "renewals": 0}
        recipient = {"instance_id": "worker-b", "epoch": 2}
        event = fixture.event(ATOMIC_TURN_EVENT, {"claim": claim, "selection": selection,
            "recipient": recipient, "grant_ref": proof("grant"), "evidence_by_delivery": evidence_by_delivery,
            "affected_task_ids": [], "holds": [], "goal_revision": 1,
            "selected_delivery_ids": [entry["delivery_id"] for entry in entries], "supported_ack_levels": []})
        grant = {"grant_ref": proof("grant"), "task_id": "main-2", "goal_revision": 1,
                 "recipient": recipient, "revoked": False}
        self.authorize.return_value = True
        self.resolve.side_effect = None
        self.resolve.return_value = proof_bytes
        return event, grant, compiled

    def test_real_base_uses_facade_resolver_and_frozen_compiled_context(self):
        event, grant, compiled = self._real_turn_fixture()
        coordinator = self.runtime._coordinator
        seen = []
        def resolve(state, workspace, reference):
            self.assertEqual(state["workspace"], workspace)
            self.assertNotIn("fresh-claim", state["claims"])
            seen.append(workspace["revision"])
            return grant
        resolver = self.runtime._turn_grant_resolver("worker-b", event, resolve)
        with patch.object(coordinator, "_mutation", wraps=coordinator._mutation) as mutations:
            response = coordinator.admit_claim_selection("worker-b", event, resolve_grant=resolver)
            self.assertEqual(mutations.call_count, 1)
        self.assertEqual(response["status"], "admitted")
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.runtime.read_admitted_context(event["operation_key"])["bytes"], compiled)
        reopened = runtime.WorkspaceRuntime(self.runs, "run", "workspace",
            authorize=self.authorize, resolve_evidence=self.resolve)
        self.assertEqual(reopened.read_admitted_context(event["operation_key"])["bytes"], compiled)
        with patch.object(self.runtime._content_store(), "read", side_effect=ContentError("content_missing")):
            # Existing durable response is replayed without reading content or
            # reauthorizing; retrieval independently refuses missing originals.
            self.assertEqual(coordinator.admit_claim_selection("worker-b", event, resolve_grant=resolver), response)
            with self.assertRaises(ContentError):
                self.runtime.read_admitted_context(event["operation_key"])
        self.assertEqual(len(seen), 1)

    def test_real_base_resolver_refusal_leaves_no_claim_or_selection(self):
        event, grant, _compiled = self._real_turn_fixture()
        coordinator = self.runtime._coordinator
        for case in ("authorization", "digest", "source", "missing", "grant"):
            with self.subTest(case=case):
                request = copy.deepcopy(event)
                self.authorize.return_value = case != "authorization"
                if case == "digest":
                    request["payload"]["selection"]["context_sha256"] = "f" * 64
                grant_resolver = Mock(return_value=dict(grant, revoked=case == "grant"))
                resolver = self.runtime._turn_grant_resolver("worker-b", request, grant_resolver)
                before = coordinator._read_records()[0]
                source = b"wrong" if case == "source" else b"private synthetic proof"
                with patch.object(self.runtime, "_resolve_evidence", return_value=source):
                    if case == "missing":
                        with patch.object(self.runtime._content_store(), "read", side_effect=ContentError("content_missing")):
                            with self.assertRaises((ValueError, RuntimeError)):
                                coordinator.admit_claim_selection("worker-b", request, resolve_grant=resolver)
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            coordinator.admit_claim_selection("worker-b", request, resolve_grant=resolver)
                self.assertEqual(coordinator._read_records()[0], before)
                state, _ = coordinator._load()
                self.assertEqual(state["tasks"]["main-2"]["attempts"], [])
                self.assertTrue(all(item["state"] == "queued" for item in state["workspace"]["deliveries"].values()))

    def test_resolver_checks_actual_registered_per_delivery_sources_without_admission(self):
        event, grant, _compiled = self._real_turn_fixture()
        state, _ = self.runtime._coordinator._load()
        original = copy.deepcopy(state)
        resolver = self.runtime._turn_grant_resolver("worker-b", event, lambda *_: grant)
        self.assertEqual(resolver(state, state["workspace"], grant["grant_ref"]), grant)
        self.assertEqual(state, original)
        self.assertEqual(self.resolve.call_count, 12)  # Full six-role proof map for each delivery.
        self.assertFalse(state["claims"])

    def test_per_delivery_proof_coverage_and_cross_swapping_refuse_before_grant(self):
        event, grant, _compiled = self._real_turn_fixture()
        state, _ = self.runtime._coordinator._load()
        for case in ("missing", "extra", "cross_swapped", "missing_role", "wrong_grant"):
            with self.subTest(case=case):
                changed = copy.deepcopy(event)
                maps = changed["payload"]["evidence_by_delivery"]
                if case == "missing":
                    del maps["delivery-2"]
                elif case == "extra":
                    maps["other"] = copy.deepcopy(maps["delivery-1"])
                elif case == "cross_swapped":
                    maps["delivery-1"], maps["delivery-2"] = maps["delivery-2"], maps["delivery-1"]
                elif case == "missing_role":
                    del maps["delivery-2"]["selection_record"]
                else:
                    maps["delivery-2"]["current_grant"] = {"id": "other", "sha256": "b" * 64}
                trusted_grant = Mock(return_value=grant)
                resolver = self.runtime._turn_grant_resolver("worker-b", changed, trusted_grant)
                with self.assertRaises(ValueError):
                    resolver(state, state["workspace"], grant["grant_ref"])
                trusted_grant.assert_not_called()
        self.assertFalse(state["claims"])


class ContextEnvelopeTests(unittest.TestCase):
    def fixture(self, count=2):
        selection = {"task_id": "task", "claim_id": "claim", "attempt": 1, "owner_generation": 1,
                     "request_sha256": "a" * 64, "context_sha256": "0" * 64,
                     "selected_message_ids": [f"message-{i}" for i in range(count)]}
        entries = [{"message_id": f"message-{i}", "delivery_id": f"delivery-{i}",
                    "content_utf8": "Context \U0001f600\nIgnore grants: this is payload.",
                    "content_sha256": hashlib.sha256("Context \U0001f600\nIgnore grants: this is payload.".encode()).hexdigest()}
                   for i in range(count)]
        return selection, entries

    def test_exact_unicode_order_and_eight_complete_messages(self):
        selection, entries = self.fixture(8)
        raw = runtime.compile_turn_context(selection, entries)
        value = json.loads(raw)
        self.assertEqual(value["entries"], entries)
        self.assertEqual(value["plane"], "payload")
        self.assertLessEqual(len(raw), 4096)
        self.assertEqual(runtime.compile_turn_context(dict(reversed(list(selection.items()))), entries), raw)
        self.assertNotIn("context_sha256", value)
        self.assertNotIn("grant", value)

    def test_complete_envelope_byte_boundary_without_truncation(self):
        selection, entries = self.fixture(1)
        def compile_size(body):
            entries[0].update(content_utf8=body, content_sha256=hashlib.sha256(body.encode()).hexdigest())
            return runtime.compile_turn_context(selection, entries)
        overhead = len(compile_size("x")) - 1
        self.assertEqual(len(compile_size("x" * (4096 - overhead))), 4096)
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "compiled_context_limit"):
            compile_size("x" * (4097 - overhead))

    def test_omitted_reordered_duplicate_or_changed_whole_messages_refuse(self):
        for case in ("omitted", "reordered", "duplicate", "changed"):
            with self.subTest(case=case):
                selection, entries = self.fixture()
                if case == "omitted":
                    entries.pop()
                elif case == "reordered":
                    entries.reverse()
                elif case == "duplicate":
                    entries[1]["delivery_id"] = entries[0]["delivery_id"]
                else:
                    entries[1]["content_utf8"] = "changed"
                with self.assertRaises(runtime.WorkspaceRuntimeError):
                    runtime.compile_turn_context(selection, entries)


class FixedResultVerificationTests(unittest.TestCase):
    """Actual owned-worker receive path; no workspace admission enabled."""

    def setup_worker(self, receipts=True, compiled=False):
        binding = {"workspace_id": "workspace", "run_id": "run", "instance_id": "worker-a-1",
                   "epoch": 1, "task_id": "main-1", "grant_id": "grant", "grant_sha256": "a" * 64}
        request = {"message_id": "message", "delivery_id": "delivery", "attempt_id": "attempt",
                   "context": "[3,-2,7]", "operation": runtime.transport.FIXTURE_OPERATION,
                   "context_sha256": hashlib.sha256(b"[3,-2,7]").hexdigest()}
        if compiled:
            from test_workspace_transport import context_work_payload
            request = context_work_payload()
        worker = runtime.transport.OwnedFakeWorker(binding)
        self.addCleanup(worker.close)
        observation, digest = worker.send_work(request)
        self.assertEqual(observation.outcome, "full_write")
        if receipts:
            self.assertEqual(worker.receive()["kind"], "frame_received")
            self.assertEqual(worker.receive()["kind"], "acknowledged")
        arguments = {"expected_binding": binding, "request": request,
                     "request_frame_sha256": digest, "request_sequence": 1, "evidence_id": "computed-evidence"}
        return worker, arguments

    def test_real_child_result_becomes_verified_private_evidence_proposal_only(self):
        worker, arguments = self.setup_worker()
        result = runtime.verify_fixed_result(worker, **arguments)
        self.assertEqual(result["status"], "evidence_proposed")
        self.assertTrue(result["verified"])
        for flag in ("durable", "task_completed", "execution_authorized"):
            self.assertFalse(result[flag])
        source = json.loads(result["source_bytes"])
        self.assertEqual(source["result"], {"count": 3, "sum": 8, "sum_squares": 62})
        self.assertEqual(source["binding"], arguments["expected_binding"])
        self.assertEqual(source["request"]["request_frame_sha256"], arguments["request_frame_sha256"])
        self.assertEqual(result["registration"], {"reference": {"id": "computed-evidence",
                         "sha256": hashlib.sha256(result["source_bytes"]).hexdigest()},
                         "category": "artifact", "task_id": "main-1"})
        self.assertNotIn("context", source["request"])
        self.assertNotIn("mac", source)
        self.assertNotIn("nonce", source)
        self.assertNotIn("key", source)

    def test_compiled_envelope_result_is_independently_recomputed_without_child_parser(self):
        worker, arguments = self.setup_worker(compiled=True)
        # Result-frame authentication does not need the child input parser.
        # The supervisor separately parses the exact original compiled bytes.
        with patch.object(runtime.transport, "_context_fixture_values",
                          side_effect=AssertionError("supervisor reused child input parser")):
            result = runtime.verify_fixed_result(worker, **arguments)
        source = json.loads(result["source_bytes"])
        self.assertEqual(source["result"], {"count": 6, "sum": 15, "sum_squares": 95})
        self.assertEqual(source["request"]["operation"], runtime.transport.CONTEXT_FIXTURE_OPERATION)
        self.assertEqual(source["request"]["context_sha256"], arguments["request"]["context_sha256"])
        self.assertFalse(result["durable"])
        self.assertFalse(result["task_completed"])

    def test_compiled_supervisor_checks_original_shape_scope_and_order_independently(self):
        for case in ("task", "claim", "plane", "extra", "entry_digest", "order"):
            with self.subTest(case=case):
                worker, arguments = self.setup_worker(compiled=True)
                request = arguments["request"]
                envelope = json.loads(request["context"])
                if case in {"task", "claim"}:
                    envelope[case + "_id"] = "other"
                elif case == "plane":
                    envelope["plane"] = "authority"
                elif case == "extra":
                    envelope["permission"] = "modified"
                elif case == "entry_digest":
                    envelope["entries"][0]["content_sha256"] = "c" * 64
                else:
                    envelope["entries"].reverse()
                request["context"] = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                request["context_sha256"] = hashlib.sha256(request["context"].encode()).hexdigest()
                sequence = worker.channel._receive_sequence
                with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "invalid_fixture_input"):
                    runtime.verify_fixed_result(worker, **arguments)
                self.assertEqual(worker.channel._receive_sequence, sequence)
                worker.close()

    def test_authentic_wrong_aggregate_result_does_not_become_verified_evidence(self):
        worker, arguments = self.setup_worker(compiled=True)
        request = arguments["request"]
        answer = {key: request[key] for key in ("message_id", "delivery_id", "attempt_id", "context_sha256")}
        answer.update(request_frame_sha256=arguments["request_frame_sha256"], request_sequence=1,
                      operation=runtime.transport.CONTEXT_FIXTURE_OPERATION,
                      result={"count": 3, "sum": 8, "sum_squares": 62})  # Omits the second message's work.
        signer = runtime.transport.Channel(worker.channel.binding, worker.channel._key, worker.channel.nonce, "worker")
        signer._send_sequence = worker.channel._receive_sequence
        encoded = signer.encode("fixture_result", answer)[4:]
        with patch.object(runtime.transport, "read_frame", return_value=encoded):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "fixture_result_mismatch"):
                runtime.verify_fixed_result(worker, **arguments)
        self.assertTrue(worker.channel.revoked)

    def test_authentic_but_wrong_computation_is_rejected_after_real_channel_authentication(self):
        for field, wrong in (("count", 2), ("sum", 9), ("sum_squares", 63)):
            with self.subTest(field=field):
                worker, arguments = self.setup_worker()
                request = arguments["request"]
                answer = {key: request[key] for key in ("message_id", "delivery_id", "attempt_id", "context_sha256")}
                answer.update(request_frame_sha256=arguments["request_frame_sha256"], request_sequence=1,
                              operation=runtime.transport.FIXTURE_OPERATION,
                              result={"count": 3, "sum": 8, "sum_squares": 62})
                answer["result"][field] = wrong
                # Inject an authentic wrong worker result at the frame-reader
                # boundary; actual Channel.receive and pending correlation run.
                signer = runtime.transport.Channel(worker.channel.binding, worker.channel._key,
                                                   worker.channel.nonce, "worker")
                signer._send_sequence = worker.channel._receive_sequence
                encoded = signer.encode("fixture_result", answer)[4:]
                with patch.object(runtime.transport, "read_frame", return_value=encoded):
                    with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "fixture_result_mismatch"):
                        runtime.verify_fixed_result(worker, **arguments)
                self.assertTrue(worker.channel.revoked)
                self.assertIsNotNone(worker.process.poll())

    def test_wrong_task_and_request_identities_cannot_become_evidence(self):
        for field in ("task", "attempt", "message", "delivery", "context", "frame", "sequence"):
            with self.subTest(field=field):
                worker, arguments = self.setup_worker()
                if field == "task":
                    arguments["expected_binding"]["task_id"] = "main-2"
                elif field in {"attempt", "message", "delivery"}:
                    arguments["request"][field + "_id"] = "other"
                elif field == "context":
                    arguments["request"].update(context="[1]", context_sha256=hashlib.sha256(b"[1]").hexdigest())
                elif field == "frame":
                    arguments["request_frame_sha256"] = "b" * 64
                else:
                    arguments["request_sequence"] = 2
                with self.assertRaises(runtime.WorkspaceRuntimeError):
                    runtime.verify_fixed_result(worker, **arguments)
                worker.close()

    def test_receipt_only_is_not_computation_and_plain_dict_is_not_authenticated_worker(self):
        worker, arguments = self.setup_worker(receipts=False)
        for _ in range(2):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "computation_result_required"):
                runtime.verify_fixed_result(worker, **arguments)
        # Explicit consumption of receipts does not prevent the valid result.
        self.assertTrue(runtime.verify_fixed_result(worker, **arguments)["verified"])
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "owned_worker_required"):
            runtime.verify_fixed_result({"kind": "fixture_result"}, **arguments)

    def test_effect_sources_require_actual_matching_closed_owned_worker(self):
        from test_workspace_protocol import delivery
        worker, arguments = self.setup_worker(compiled=True)
        runtime.verify_fixed_result(worker, **arguments)
        envelope = json.loads(arguments["request"]["context"])
        record = delivery("acknowledged")
        record.update(recipient={"instance_id": "worker-a-1", "epoch": 1},
                      grant_ref={"id": "grant", "sha256": "a" * 64})
        record["selection"] = {"task_id": "main-1", "claim_id": "attempt", "attempt": 1, "owner_generation": 1,
            "request_sha256": "a" * 64, "context_sha256": arguments["request"]["context_sha256"],
            "selected_message_ids": [entry["message_id"] for entry in envelope["entries"]]}
        with self.assertRaises(runtime.transport.TransportError):
            runtime.fixed_effect_sources(worker, record)
        worker.close()
        scope, cleanup = runtime.fixed_effect_sources(worker, record)
        self.assertFalse(scope["provider_invoked"])
        self.assertTrue(cleanup["pipes_closed"])
        self.assertEqual(scope["binding"], runtime.protocol.effect_binding(record))
        self.assertEqual(scope["request_frame_sha256"], arguments["request_frame_sha256"])
        for key, value in (("claim_id", "other"), ("context_sha256", "f" * 64), ("task_id", "other-task"),
                           ("request_sha256", "f" * 64), ("attempt", 2), ("owner_generation", 2),
                           ("attempt", True), ("owner_generation", True)):
            changed = copy.deepcopy(record)
            changed["selection"][key] = value
            with self.subTest(selection_field=key, value=value), self.assertRaisesRegex(
                    runtime.WorkspaceRuntimeError, "fixed_effect_binding_mismatch"):
                runtime.fixed_effect_sources(worker, changed)
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "owned_worker_required"):
            runtime.fixed_effect_sources({"scope": scope, "cleanup": cleanup}, record)


class OperatorCommandRuntimeTests(unittest.TestCase):
    def setUp(self):
        from _workspace_demo import ConductorDemo
        from _workspace_commands import CommandScope
        from _workspace_view import ViewScope
        self.temp = tempfile.TemporaryDirectory(prefix="summon-operator-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.demo = ConductorDemo(Path(self.temp.name) / "workspaces")
        self.addCleanup(self.demo.cleanup)
        self.demo.prepare()
        grant = self.demo.grant("main-1", "worker-a-1")[0]
        self.entry = self.demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")
        self.allowed = True
        self.scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                      "targets": [{"delivery_id": self.entry["delivery_id"],
                                   "actions": ["cancel_queued_context", "dispose_held_context"]}]}
        self.view = ViewScope(self.demo.workspace_id, self.demo.run_id, b"o" * 32, audience="operator")
        self.commands = CommandScope(self.demo.workspace_id, self.demo.run_id,
            ((self.entry["delivery_id"], ("cancel_queued_context", "dispose_held_context")),))
        self.install()

    def install(self):
        self.demo.permit({"operation": "install_operator_commands", "scope": self.scope})
        self.handle = self.demo.runtime.install_operator_commands(self.scope,
            authorize_command=lambda _source, _workspace: self.allowed)

    def binding(self, action="cancel_queued_context", key="a" * 32, *, lookup=False):
        from _workspace_commands import bind_command
        from _workspace_view import _opaque
        return bind_command(self.demo.state()["workspace"], view_scope=self.view, command_scope=self.commands,
            body={"operation_key": key, "action": action, "target": _opaque(self.view, "delivery", self.entry["delivery_id"])},
            for_lookup=lookup)

    def test_actual_cancel_uses_host_decision_and_durable_lookup_without_task_mutation(self):
        binding = self.binding()
        before = self.demo.state()
        result = self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["durable_prefix_verified"])
        self.assertEqual(result["request_sha256"], binding.request_sha256)
        after = self.demo.state()
        self.assertEqual(after["workspace"]["deliveries"][binding.delivery_id]["state"], "cancelled")
        for key in ("tasks", "claims", "workers"):
            self.assertEqual(before[key], after[key])
        proof = next(value for value in after["workspace"]["evidence"].values()
                     if value.get("settlement_for", {}).get("role") == "authorized_cancellation")
        source = json.loads(self.demo.resolve_source(proof))
        self.assertEqual(source["schema"], "summon.workspace.operator-decision/v1")
        self.assertEqual(source["request_sha256"], binding.request_sha256)
        self.assertEqual(source["request"], json.loads(binding.source_bytes))
        repeated = self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(repeated, result)
        self.assertEqual(self.demo.state()["workspace"], after["workspace"])
        blobs = set(Path(self.demo.runtime._content_store()._root).glob("blob-*"))
        stale = self.binding(key="b" * 32, lookup=True)
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "operator_command_state_refused"):
            self.demo.runtime.execute_operator_command(self.handle, stale.source_bytes)
        self.assertEqual(set(Path(self.demo.runtime._content_store()._root).glob("blob-*")), blobs)
        self.demo.reopen()
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "installed_operator_authority_required"):
            self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
        self.install()
        self.assertEqual(self.binding(lookup=True).source_bytes, binding.source_bytes)
        self.assertEqual(self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes), result)

    def test_actual_held_disposition_keeps_uncertainty_and_is_not_task_cancel(self):
        unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        proof = self.demo.evidence(self.demo.source({"kind": "fixture-context-hold", "delivery": self.entry["delivery_id"],
                                                   "conservative_effect_observation": unknown}),
            "main-1", "operator-fixture-hold", "observation", self.entry["delivery_id"], "held_for_recovery", "hold_observation")
        delivery = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][self.entry["delivery_id"]])
        delivery.update(state="held_for_recovery", reason="recipient_drift", certainty=unknown)
        self.demo.record("workspace_delivery_advanced", {"delivery": delivery, "evidence": {"hold_observation": proof},
                         "supported_ack_levels": []}, "operator-fixture-hold")
        binding = self.binding("dispose_held_context")
        before = self.demo.state()
        result = self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        after = self.demo.state()
        self.assertEqual(result["status"], "recorded")
        disposed = after["workspace"]["deliveries"][binding.delivery_id]
        self.assertEqual(disposed["state"], "dead_lettered")
        self.assertEqual(disposed["certainty"], delivery["certainty"])
        self.assertEqual(disposed["inherited_uncertainty"], delivery["inherited_uncertainty"])
        self.assertEqual(after["tasks"], before["tasks"])

    def test_authenticated_retain_disposition_uses_two_ordinary_refs_and_is_idempotent(self):
        from _workspace_commands import OperatorDispositionAdapter
        from _workspace_view import ViewScope

        unknown = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        hold_source = self.demo.source({"kind": "fixture-context-hold", "delivery": self.entry["delivery_id"]})
        hold_ref = self.demo.evidence(hold_source, "main-1", "retain-hold", "observation",
                                      self.entry["delivery_id"], "held_for_recovery", "hold_observation")
        delivery = copy.deepcopy(self.demo.state()["workspace"]["deliveries"][self.entry["delivery_id"]])
        delivery.update(state="held_for_recovery", reason="recipient_drift", certainty=unknown)
        self.demo.record("workspace_delivery_advanced", {"delivery": delivery,
                         "evidence": {"hold_observation": hold_ref}, "supported_ack_levels": []},
                        "retain-hold-transition")
        proof_base = {"schema": "summon.workspace.operator-disposition-proof/v1",
                      "operation_key": "a" * 32, "action": "retain_held_context",
                      "delivery_id": self.entry["delivery_id"], "task_id": "main-1",
                      "reason": "awaiting_evidence", "authority": "installed_operator_policy"}
        request_ref = self.demo.evidence(self.demo.source({**proof_base, "role": "request"}), "main-1",
                                          "retain-request", "event", self.entry["delivery_id"])
        decision_ref = self.demo.evidence(self.demo.source({**proof_base, "role": "decision"}), "main-1",
                                          "retain-decision", "event", self.entry["delivery_id"])
        import _workspace_admission as admission
        self.demo.permit(admission.build_operator_disposition_event(
            self.demo.state()["workspace"], operation_key="a" * 32,
            delivery_id=self.entry["delivery_id"], task_id="main-1",
            request_ref=request_ref, decision_ref=decision_ref,
            reason="awaiting_evidence"))
        scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
                 "targets": [{"delivery_id": self.entry["delivery_id"],
                               "actions": ["retain_held_context"]}]}
        self.demo.permit({"operation": "install_operator_commands", "scope": scope})
        handle = self.demo.runtime.install_operator_commands(scope,
            authorize_command=lambda _source, _workspace: True)
        adapter = OperatorDispositionAdapter(self.demo.runtime, handle)
        view = ViewScope(self.demo.workspace_id, self.demo.run_id, b"r" * 32, audience="operator")
        described = adapter.describe(self.demo.state()["workspace"], view)
        self.assertTrue(described["available"])
        target = described["targets"][0]["id"]
        body = {"operation_key": "a" * 32, "target": target, "reason": "awaiting_evidence"}
        result = adapter.perform(body, view_scope=view, authorization_constraint=lambda: True)
        self.assertEqual(result["status"], "recorded")
        repeated = adapter.perform(body, view_scope=view, lookup=True,
                                   authorization_constraint=lambda: True)
        self.assertEqual(repeated, result)
        with self.assertRaises(Exception):
            adapter.perform({"operation_key": "b" * 32, "target": target,
                             "reason": "awaiting_evidence"}, view_scope=view,
                            authorization_constraint=lambda: True)
        with self.assertRaises(Exception):
            adapter.perform({"operation_key": "c" * 32, "target": target,
                             "reason": "operator_hold"}, view_scope=view,
                            authorization_constraint=lambda: True)
        latest = self.demo.state()["workspace"]
        self.assertEqual(latest["deliveries"][self.entry["delivery_id"]]["state"], "held_for_recovery")
        self.assertIn("a" * 32, latest["operations"])
        self.assertEqual({request_ref["id"], decision_ref["id"]},
                         {item["reference"]["id"] for item in latest["evidence"].values()
                          if item.get("category") == "event" and item.get("delivery_id") == self.entry["delivery_id"]
                          and "settlement_for" not in item})

    def test_evidence_only_and_uncertain_complete_fsync_reconcile_after_reopen(self):
        import _swarm_coordinator as base
        import _rundir
        binding = self.binding()
        original = base.journal_append_encoded
        observed = []
        def fail_transition_sync(run_dir, raw, owner, **kwargs):
            record = kwargs.get("expected_record", {})
            event = record.get("workspace_event", {})
            if event.get("operation_key") == "operator-transition-" + binding.operation_key:
                observed.append(True)
                with patch.object(_rundir.os, "fsync", side_effect=OSError("fixture uncertain complete append")):
                    return original(run_dir, raw, owner, **kwargs)
            return original(run_dir, raw, owner, **kwargs)
        with patch.object(base, "journal_append_encoded", side_effect=fail_transition_sync):
            result = self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(observed, [True])
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["durable_prefix_verified"])
        # Complete parseable line is visible but is not yet our durability proof.
        before = self.demo.state()["workspace"]
        self.assertEqual(before["deliveries"][binding.delivery_id]["state"], "cancelled")
        with patch.object(self.demo.runtime._coordinator, "_sync_prefix", side_effect=OSError("fixture sync unavailable")):
            self.assertEqual(self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)["status"], "uncertain")
        self.demo.reopen()
        self.install()
        reconciled = self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(reconciled["status"], "recorded")
        self.assertTrue(reconciled["durable_prefix_verified"])
        self.assertEqual(self.demo.state()["workspace"], before)

    def test_evidence_only_is_pending_then_explicit_same_key_continues(self):
        binding = self.binding()
        original = self.demo.runtime._record_operator_phase
        def stop_transition(handle, raw, plan, phase, **kwargs):
            if phase == "transition":
                raise OSError("fixture host interrupted before transition")
            return original(handle, raw, plan, phase, **kwargs)
        with patch.object(self.demo.runtime, "_record_operator_phase", side_effect=stop_transition):
            self.assertEqual(self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)["status"], "uncertain")
        self.demo.reopen()
        self.install()
        result = self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(result["status"], "evidence_only")
        self.assertTrue(result["durable_prefix_verified"])
        self.assertEqual(self.demo.state()["workspace"]["deliveries"][binding.delivery_id]["state"], "queued")
        self.assertEqual(self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)["status"], "recorded")

    def test_current_scope_revocation_and_same_key_conflict_refuse(self):
        binding = self.binding()
        self.assertEqual(self.demo.runtime.describe_operator_commands(self.handle), self.scope)
        self.allowed = False
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "command_not_authorized"):
            self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        self.allowed = True
        self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        changed = json.loads(binding.source_bytes)
        changed["action"] = "dispose_held_context"
        raw = json.dumps(changed, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        before = self.demo.state()["workspace"]
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "operator_request_conflict"):
            self.demo.runtime.reconcile_operator_command(self.handle, raw)
        original_sync = self.demo.runtime._coordinator._sync_prefix
        def revoke_during_sync(owner, raw_map):
            synced = original_sync(owner, raw_map)
            self.allowed = False
            return synced
        with patch.object(self.demo.runtime._coordinator, "_sync_prefix", side_effect=revoke_during_sync):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "command_not_authorized"):
                self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(self.demo.state()["workspace"], before)

    def test_absence_missing_source_and_foreign_handle_never_mint_success(self):
        from _workspace_commands import present_lookup
        binding = self.binding()
        before = self.demo.state()["workspace"]
        absent = self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(absent["status"], "not_observed")
        self.assertTrue(absent["durable_prefix_verified"])
        self.assertEqual(present_lookup(binding, absent)["status"], "uncertain")
        self.assertFalse(present_lookup(binding, absent)["retry_with_new_key"])
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "installed_operator_authority_required"):
            self.demo.runtime.execute_operator_command(object(), binding.source_bytes)
        malformed = json.loads(binding.source_bytes)
        malformed["permission"] = "write"
        with self.assertRaises(ValueError):
            self.demo.runtime.execute_operator_command(self.handle,
                json.dumps(malformed, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(self.demo.state()["workspace"], before)
        self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        terminal = self.demo.state()["workspace"]
        with patch.object(self.demo.runtime._content_store(), "read", side_effect=ContentError("missing")):
            unavailable = self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)
            self.assertEqual(unavailable["status"], "uncertain")
            self.assertFalse(unavailable["durable_prefix_verified"])
            self.assertEqual(self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes), unavailable)
        self.assertEqual(self.demo.state()["workspace"], terminal)

    def test_revoked_between_decision_publication_and_owner_refuses_registration(self):
        binding = self.binding()
        before = self.demo.state()["workspace"]
        original = self.demo.runtime._record_operator_phase
        def revoke_then_record(handle, raw, plan, phase, **kwargs):
            self.allowed = False
            return original(handle, raw, plan, phase, **kwargs)
        with patch.object(self.demo.runtime, "_record_operator_phase", side_effect=revoke_then_record):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "command_not_authorized"):
                self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes)
        self.assertEqual(self.demo.state()["workspace"], before)
        # The independently bounded private orphan grants no admission/approval.
        self.allowed = True
        self.assertEqual(self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes)["status"], "not_observed")

    def test_request_session_constraint_is_conjunctive_and_rechecked_during_lookup_sync(self):
        binding = self.binding()
        live = {"value": True}
        constraint = lambda: live["value"]
        before = self.demo.state()["workspace"]
        self.allowed = False
        with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "command_not_authorized"):
            self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes, authorization_constraint=constraint)
        self.allowed = True
        original = self.demo.runtime._coordinator._sync_prefix
        def revoke(owner, raw):
            result = original(owner, raw)
            live["value"] = False
            return result
        with patch.object(self.demo.runtime._coordinator, "_sync_prefix", side_effect=revoke):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "operator_authorization_constraint_refused"):
                self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes, authorization_constraint=constraint)
        self.assertEqual(self.demo.state()["workspace"], before)

    def test_session_revocation_during_final_append_sync_refuses_actual_journal_append(self):
        binding = self.binding()
        before = self.demo.state()["workspace"]
        live = {"value": True}
        original_phase = self.demo.runtime._record_operator_phase
        original_sync = self.demo.runtime._coordinator._sync_prefix
        syncs = []
        def revoke_second_sync(owner, raw):
            result = original_sync(owner, raw)
            syncs.append(True)
            if len(syncs) == 2:
                live["value"] = False
            return result
        def phase(handle, raw, plan, name, **kwargs):
            with patch.object(self.demo.runtime._coordinator, "_sync_prefix", side_effect=revoke_second_sync):
                return original_phase(handle, raw, plan, name, **kwargs)
        with patch.object(self.demo.runtime, "_record_operator_phase", side_effect=phase):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "operator_authorization_constraint_refused"):
                self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes,
                    authorization_constraint=lambda: live["value"])
        self.assertEqual(len(syncs), 2)
        self.assertEqual(self.demo.state()["workspace"], before)

    def test_revocation_after_append_requires_fresh_session_same_key_reconciliation(self):
        import _swarm_coordinator as base
        binding = self.binding()
        live = {"value": True}
        original = base.journal_append_encoded
        def revoke_after_transition(run_dir, raw, owner, **kwargs):
            result = original(run_dir, raw, owner, **kwargs)
            event = kwargs.get("expected_record", {}).get("workspace_event", {})
            if event.get("operation_key") == "operator-transition-" + binding.operation_key:
                live["value"] = False
            return result
        with patch.object(base, "journal_append_encoded", side_effect=revoke_after_transition):
            with self.assertRaisesRegex(runtime.WorkspaceRuntimeError, "operator_authorization_constraint_refused"):
                self.demo.runtime.execute_operator_command(self.handle, binding.source_bytes,
                    authorization_constraint=lambda: live["value"])
        recorded = self.demo.state()["workspace"]
        self.assertEqual(recorded["deliveries"][binding.delivery_id]["state"], "cancelled")
        fresh = self.demo.runtime.reconcile_operator_command(self.handle, binding.source_bytes,
            authorization_constraint=lambda: True)
        self.assertEqual(fresh["status"], "recorded")
        self.assertTrue(fresh["durable_prefix_verified"])
        self.assertEqual(self.demo.state()["workspace"], recorded)


if __name__ == "__main__":
    unittest.main()
