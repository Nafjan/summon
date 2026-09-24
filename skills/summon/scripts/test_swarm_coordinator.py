from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _swarm_coordinator import (SwarmConflictError, SwarmCoordinator,
                                SwarmCoordinatorError, SwarmCorruptError,
                                SwarmIndeterminateError, SwarmBudgetRefusal)
import _swarm_coordinator as swarm_module
import _rundir as rundir
from _swarm_protocol import PROTOCOL, SwarmProtocolError, make_frame
from _spawn import run_flags
import _workspace_admission as workspace_admission
import _submission_accounting as submission_accounting
from test_workspace_admission import admission_fixture
from test_workspace_protocol import delivery as workspace_delivery, ref as workspace_ref
import _workspace_protocol as workspace_protocol
import _workspace_state as workspace_state_module
from _workspace_runtime import _WorkspaceCoordinator, _task_view


class _Clock:
    def __init__(self, value: float = 2_000_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class SwarmCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-swarm-")
        self.clock = _Clock()
        self.request = hashlib.sha256(b"task-a").hexdigest()
        self.project = "a" * 64
        self.roster = "b" * 64
        self.coordinator = SwarmCoordinator.create(
            self.temp.name, "swarm-1", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[{"task_id": "task-a", "request_sha256": self.request}],
            max_attempts=2, clock=self.clock)
        self.coordinator.register_worker("worker-a", worker_instance_id="instance-a",
                                        capabilities=["message", "cancel"])
        self.coordinator.register_worker("worker-b", worker_instance_id="instance-b",
                                        capabilities=["message", "cancel"])

    def tearDown(self):
        self.temp.cleanup()

    def test_prepared_status_is_redacted_and_bound(self):
        status = self.coordinator.status()
        self.assertEqual(status["status"], "prepared")
        self.assertEqual(status["project_root_sha256"], self.project)
        self.assertEqual(status["roster_definition_sha256"], self.roster)
        self.assertEqual(status["worker_count"], 2)
        self.assertEqual(status["tasks"][0]["status"], "pending")
        self.assertFalse(status["uncertain_spend"])

    def test_v2_economics_fence_is_persisted_and_replayed(self):
        root = tempfile.TemporaryDirectory(prefix="summon-economics-config-")
        self.addCleanup(root.cleanup)
        economics = {
            "schema": "summon.workspace.economics/v1",
            "feature": "submission-accounting-v1",
            "enabled": True, "max_records": 8,
            "max_settlement_bytes": 4096,
        }
        coordinator = SwarmCoordinator.create(
            root.name, "economics-config", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[{"task_id": "task-a", "request_sha256": self.request}],
            economics=economics, clock=self.clock)
        state, torn = coordinator._load()
        self.assertFalse(torn)
        self.assertEqual(state["economics"], economics)
        reopened = SwarmCoordinator(root.name, "economics-config", clock=self.clock)
        self.assertEqual(reopened._load()[0]["economics"], economics)

    def test_workspace_admission_contract_exposes_only_implemented_base_events(self):
        contract = SwarmCoordinator.workspace_admission_contract()
        self.assertEqual(contract["schema"], "summon.workspace-admission/v1")
        self.assertEqual(set(contract["event_classes"]), swarm_module._WORKSPACE_EVENT_CLASSES)
        self.assertTrue(contract["reconciliation"])
        self.assertTrue(contract["control_reserve"])
        self.assertTrue(contract["atomic_claim_selection"])
        self.assertGreaterEqual(contract["max_event_bytes"], 48 * 1024)

    def _fresh_atomic_admission(self, coordinator, *, operation_key="atomic-1"):
        fixture, event, _unused, grant = admission_fixture()
        claim = event["payload"]["claim"]
        selection = event["payload"]["selection"]
        now = int(self.clock.value * 1000)
        claim.update({"claim_id": "fresh-claim", "worker_id": "worker-b",
                      "lease_generation": 1, "lease_expires_at_ms": now + 10_000,
                      "request_sha256": self.request, "attempt": 1})
        selection.update({"claim_id": "fresh-claim", "owner_generation": 1,
                          "request_sha256": self.request, "attempt": 1})
        event["operation_key"] = operation_key
        current_state, _ = coordinator._load()
        event["expected_revision"] = current_state["workspace"]["revision"]
        event["payload"]["supported_ack_levels"] = []
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "selection_record": {"id": "selection-record", "sha256": "a" * 64},
            "whole_message_fit": {"id": "whole-message-fit", "sha256": "a" * 64},
        })
        event["payload"]["claim"] = claim
        event["payload"]["selection"] = selection
        return fixture, event, grant

    def _workspace_coordinator(self, root, request, fixture):
        coordinator = _WorkspaceCoordinator.create(
            root, "run", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[{"task_id": task_id, "request_sha256": request}
                   for task_id in ("main-1", "side-1", "main-2")],
            max_attempts=2, clock=self.clock)
        coordinator.register_worker("worker-b", worker_instance_id="worker-b")
        # Seed the authoritative workspace through the same wrapper record
        # used by the runtime facade; no direct state injection is allowed.
        for event, _view in fixture.steps:
            with coordinator._mutation() as (owner, state, _torn):
                view = _task_view(state, run_id="run", now_ms=int(self.clock.value * 1000))
                workspace_state_module.apply_event(state.get("workspace"), event, view)
                coordinator._append_with_state(
                    owner,
                    {"event": "workspace_event", "workspace_event": copy.deepcopy(event),
                     "observed_at_ms": int(self.clock.value * 1000)},
                    state=state)
        return coordinator

    def test_atomic_admission_claims_unclaimed_task_and_replays_as_one_record(self):
        root = tempfile.TemporaryDirectory(prefix="summon-atomic-")
        self.addCleanup(root.cleanup)
        request = "d" * 64
        fixture, _unused_event, _unused_coordinator, _unused_grant = admission_fixture()
        fixture.register({"id": "recipient-fence", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "capacity", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "physical", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "selection-record", "sha256": "a" * 64}, delivery_id="delivery")
        fixture.register({"id": "whole-message-fit", "sha256": "a" * 64}, delivery_id="delivery")
        coordinator = self._workspace_coordinator(root.name, request, fixture)
        event, grant = self._fresh_atomic_admission(coordinator)[1:]
        event["payload"]["claim"]["request_sha256"] = request
        event["payload"]["selection"]["request_sha256"] = request
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "recipient_owner_fence": {"id": "recipient-fence", "sha256": "a" * 64},
            "capacity_reservation": {"id": "capacity", "sha256": "a" * 64},
            "physical_attempt_reservation": {"id": "physical", "sha256": "a" * 64},
        })
        def resolve_grant(_state, _workspace, _ref):
            # The transaction's captured admission time must remain the
            # lease fence even if grant resolution itself takes time.
            self.clock.value += 1
            return grant

        result = coordinator.admit_claim_selection(
            "worker-b", event, resolve_grant=resolve_grant, lease_ms=10_000)
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(next(item for item in coordinator.status()["tasks"]
                              if item["task_id"] == "main-2")["status"], "claimed")
        composite = [item for item in coordinator.events()
                     if item.get("event") == "workspace_turn_admitted"]
        self.assertEqual(len(composite), 1)
        reopened = _WorkspaceCoordinator(root.name, "run", clock=self.clock)
        state, _ = reopened._load()
        self.assertIn("atomic-1", state["admissions"])
        self.assertEqual(state["tasks"]["main-2"]["attempts"], ["fresh-claim"])
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], "included_in_attempt")
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["selection"], event["payload"]["selection"])
        self.assertEqual(reopened.admit_claim_selection(
            "worker-b", event,
            resolve_grant=lambda _state, _workspace, _ref: grant,
            lease_ms=10_000), result)
        self.assertEqual(len([item for item in reopened.events()
                              if item.get("event") == "workspace_turn_admitted"]), 1)
        reopened.complete(
            "worker-b", task_id="main-2", claim_id="fresh-claim", attempt=1,
            lease_generation=1, request_sha256=request, envelope_sha256="c" * 64)
        self.clock.value += 120
        self.assertEqual(reopened.admit_claim_selection(
            "worker-b", event,
            resolve_grant=lambda *_args: (_ for _ in ()).throw(AssertionError("resolver re-run")),
            lease_ms=10_000), result)

    def test_atomic_admission_persists_and_reserves_economics_fence(self):
        root = tempfile.TemporaryDirectory(prefix="summon-atomic-economics-")
        self.addCleanup(root.cleanup)
        request = "d" * 64
        fixture, _unused_event, _unused_coordinator, grant = admission_fixture()
        for evidence_id, category in (("recipient-fence", "fence"),
                                      ("capacity", "fence"),
                                      ("physical", "fence"),
                                      ("selection-record", "event"),
                                      ("whole-message-fit", "event")):
            fixture.register({"id": evidence_id, "sha256": "a" * 64},
                             category=category, delivery_id="delivery")
        coordinator = self._workspace_coordinator(root.name, request, fixture)
        event = self._fresh_atomic_admission(coordinator)[1]
        event["payload"]["claim"]["request_sha256"] = request
        event["payload"]["selection"]["request_sha256"] = request
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "recipient_owner_fence": {"id": "recipient-fence", "sha256": "a" * 64},
            "capacity_reservation": {"id": "capacity", "sha256": "a" * 64},
            "physical_attempt_reservation": {"id": "physical", "sha256": "a" * 64},
        })
        selection = event["payload"]["selection"]
        estimate = submission_accounting.estimate_payload(
            [("user", "hello")], boundary="workspace-turn")
        selection_sha256 = hashlib.sha256(workspace_admission._canonical_event_bytes({
            "task_id": selection["task_id"],
            "attempt": selection["attempt"],
            "request_sha256": selection["request_sha256"],
            "context_sha256": selection["context_sha256"],
            "selected_message_ids": selection["selected_message_ids"],
        })).hexdigest()
        reservation = {
            "schema": workspace_admission.ECONOMICS_RESERVATION_SCHEMA,
            "feature": workspace_admission.ECONOMICS_FEATURE,
            "status": "reserved", "max_records": 1,
            "max_settlement_bytes": 4096,
            "claim_id": event["payload"]["claim"]["claim_id"],
            "selection_sha256": selection_sha256,
            "attempt_id": "1" * 32,
            "payload_sha256": selection["context_sha256"],
            "material_sha256": estimate["material_sha256"],
            "policy_sha256": "b" * 64,
        }
        before, _ = coordinator._load()
        without = coordinator._reserve_after(
            before, {"event": "workspace_turn_admitted",
                     "workspace_event": copy.deepcopy(event)})
        with_reservation = copy.deepcopy(event)
        with_reservation["payload"]["economics_reservation"] = copy.deepcopy(reservation)
        with_reserve = coordinator._reserve_after(
            before, {"event": "workspace_turn_admitted",
                     "workspace_event": with_reservation})
        self.assertEqual(with_reserve - without, 4096)

        result = coordinator.admit_claim_selection(
            "worker-b", event, resolve_grant=lambda *_args: grant,
            lease_ms=10_000, economics_reservation=reservation)
        admission_id = event["operation_key"]
        state, _ = coordinator._load()
        self.assertEqual(
            state["admissions"][admission_id]["economics_reservation"], reservation)
        reopened = _WorkspaceCoordinator(root.name, "run", clock=self.clock)
        rebuilt, _ = reopened._load()
        self.assertEqual(
            rebuilt["admissions"][admission_id]["economics_reservation"], reservation)
        # The reservation is durable, not merely a candidate-append term: an
        # ordinary subsequent append must retain the same bounded economics
        # bytes/record slots after replay.
        durable_with = reopened._reserve_after(rebuilt, {"event": "message_posted"})
        unreserved = copy.deepcopy(rebuilt)
        unreserved["admissions"][admission_id].pop("economics_reservation")
        durable_without = reopened._reserve_after(unreserved, {"event": "message_posted"})
        self.assertEqual(durable_with - durable_without, 4096)
        # Close remains fail-closed until a typed settlement marks the
        # economics reservation consumed.  Use a settled synthetic workspace
        # projection so this assertion isolates the economics obligation.
        close_state = copy.deepcopy(rebuilt)
        close_state["workspace"] = None
        for claim in close_state["claims"].values():
            claim["status"] = "released"
        for task in close_state["tasks"].values():
            task["terminal"] = "failed"
        with self.assertRaises(SwarmConflictError):
            reopened._apply_record(close_state, {"event": "run_closed"})
        # The estimator's role-framed material identity is deliberately
        # distinct from the raw selected-context digest in the reservation.
        accounting = submission_accounting.private_record(
            attempt_id="1" * 32, attempt_kind="initial", attempt_ordinal=1,
            parent_attempt_id=None, request_sha256=request, estimate=estimate,
            envelope={"provider_contacted": False}, submission_state="not_submitted")
        submitted_unknown = submission_accounting.private_record(
            attempt_id="1" * 32, attempt_kind="initial", attempt_ordinal=1,
            parent_attempt_id=None, request_sha256=request, estimate=estimate,
            envelope={"provider_contacted": True}, submission_state="submitted")
        claim = reopened._load()[0]["claims"][event["payload"]["claim"]["claim_id"]]
        checked, _ = swarm_module._validate_economics_accounting(
            submitted_unknown, claim=claim, selection=selection,
            reservation=reservation, owner_generation=1,
            submission_state="submitted", result_status="error")
        self.assertTrue(checked["unknown_spend"])
        settled = reopened.settle_workspace_turn_economics(
            admission_id, accounting_record=accounting,
            submission_state="not_submitted", result_status="error")
        self.assertEqual(settled["status"], "settled")
        duplicate = reopened.settle_workspace_turn_economics(
            admission_id, accounting_record=accounting,
            submission_state="not_submitted", result_status="error")
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaises(SwarmConflictError):
            reopened.settle_workspace_turn_economics(
                admission_id, accounting_record=accounting,
                submission_state="not_submitted", result_status="error",
                result_sha256="f" * 64)
        forged_estimate = copy.deepcopy(accounting)
        forged_estimate["estimate"]["estimated_tokens"] = 0
        with self.assertRaises(SwarmBudgetRefusal):
            reopened.settle_workspace_turn_economics(
                admission_id, accounting_record=forged_estimate,
                submission_state="not_submitted", result_status="error")
        forged_contact = copy.deepcopy(accounting)
        forged_contact["contact"]["possible_submission"] = True
        with self.assertRaises(SwarmBudgetRefusal):
            reopened.settle_workspace_turn_economics(
                admission_id, accounting_record=forged_contact,
                submission_state="not_submitted", result_status="error")
        with self.assertRaises(SwarmConflictError):
            reopened.settle_workspace_turn_economics(
                admission_id, accounting_record=accounting,
                submission_state="indeterminate", result_status="error")
        settled_state, _ = reopened._load()
        close_state = copy.deepcopy(settled_state)
        close_state["workspace"] = None
        for claim in close_state["claims"].values():
            claim["status"] = "released"
        for task in close_state["tasks"].values():
            task["terminal"] = "failed"
        reopened._apply_record(close_state, {"event": "run_closed"})
        self.assertTrue(close_state["closed"])
        self.assertEqual(reopened.admit_claim_selection(
            "worker-b", event, resolve_grant=lambda *_args: grant,
            lease_ms=10_000, economics_reservation=reservation), result)

    def test_terminal_tasks_report_unsettled_workspace_effects_truthfully(self):
        root = tempfile.TemporaryDirectory(prefix="summon-workspace-status-")
        self.addCleanup(root.cleanup)
        fixture, event, _unused, grant = admission_fixture()
        request = "d" * 64
        coordinator = self._workspace_coordinator(root.name, request, fixture)
        current, _ = coordinator._load()
        event["expected_revision"] = current["workspace"]["revision"]
        event["payload"]["claim"].update(claim_id="fresh-claim", lease_generation=1)
        event["payload"]["selection"].update(claim_id="fresh-claim", owner_generation=1)
        coordinator.admit_claim_selection("worker-b", event, resolve_grant=lambda *_args: grant)

        def append(kind, payload, operation):
            with coordinator._mutation() as (owner, state, _torn):
                nested = {"event": kind, "protocol": workspace_protocol.PROTOCOL,
                          "workspace_id": "workspace", "run_id": "run", "operation_key": operation,
                          "expected_revision": state["workspace"]["revision"], "payload": payload}
                coordinator._append_with_state(owner, {"event": "workspace_event", "workspace_event": nested,
                    "observed_at_ms": int(self.clock.value * 1000)}, state=state)

        append("workspace_evidence_registered", {
            "reference": workspace_ref("observed-intent"), "category": "event", "task_id": "main-2",
            "delivery_id": "delivery", "settlement_for": {"delivery_id": "delivery",
                "target_state": "submission_started", "role": "durable_launch_intent"}}, "register-intent")
        current, _ = coordinator._load()
        after = copy.deepcopy(current["workspace"]["deliveries"]["delivery"])
        template = workspace_delivery("submission_started")
        after.update({key: template[key] for key in ("state", "reason", "certainty", "ack_level")})
        evidence = {key: value for key, value in event["payload"]["evidence_by_delivery"]["delivery"].items()
                    if key not in {"selection_record", "whole_message_fit"}}
        evidence["durable_launch_intent"] = workspace_ref("observed-intent")
        append("workspace_delivery_advanced", {"delivery": after, "evidence": evidence,
                                               "supported_ack_levels": []}, "start-submission")
        coordinator.complete("worker-b", task_id="main-2", claim_id="fresh-claim", attempt=1,
            lease_generation=1, request_sha256=request, envelope_sha256="c" * 64)
        for task_id in ("main-1", "side-1"):
            claim = coordinator.claim("worker-b", task_id, request_sha256=request)
            coordinator.complete("worker-b", task_id=task_id, claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=request, envelope_sha256="c" * 64)
        # Reopen through BASE replay as well as the facade subclass. Receipt and
        # task terminal facts remain independent after a supervisor restart.
        report = SwarmCoordinator(root.name, "run", clock=self.clock).status()
        self.assertTrue(all(task["status"] == "completed" for task in report["tasks"]))
        self.assertEqual(report["status"], "settlement_required")
        self.assertTrue(report["uncertain_spend"])
        self.assertEqual(report["workspace"], {"delivery_count": 1, "unsettled_delivery_count": 1,
            "uncertain_effects": ["cleanup", "contact", "spend"], "closure_ready": False})

    def test_atomic_admission_reserves_the_new_claim_settlement_tail(self):
        root = tempfile.TemporaryDirectory(prefix="summon-atomic-capacity-")
        self.addCleanup(root.cleanup)
        request = "d" * 64
        fixture, _unused_event, _unused_coordinator, grant = admission_fixture()
        for evidence_id, category in (("recipient-fence", "fence"),
                                      ("capacity", "fence"),
                                      ("physical", "fence"),
                                      ("selection-record", "event"),
                                      ("whole-message-fit", "event")):
            fixture.register({"id": evidence_id, "sha256": "a" * 64},
                             category=category, delivery_id="delivery")
        coordinator = self._workspace_coordinator(root.name, request, fixture)
        event = self._fresh_atomic_admission(coordinator)[1]
        event["payload"]["claim"]["request_sha256"] = request
        event["payload"]["selection"]["request_sha256"] = request
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "recipient_owner_fence": {"id": "recipient-fence", "sha256": "a" * 64},
            "capacity_reservation": {"id": "capacity", "sha256": "a" * 64},
            "physical_attempt_reservation": {"id": "physical", "sha256": "a" * 64},
        })
        before, _ = coordinator._load()
        ordinary = coordinator._reserve_after(
            before, {"event": "claim_granted", "task_id": "main-2",
                     "claim_id": "fresh-claim"})
        composite = coordinator._reserve_after(
            before, {"event": "workspace_turn_admitted",
                     "workspace_event": copy.deepcopy(event)})
        # Atomic inclusion consumes the queued selection branch budget. Compare
        # claim tails separately instead of requiring delivery reserve to grow.
        following_workspace = coordinator._workspace_after_record(before, {
            "event": "workspace_turn_admitted", "workspace_event": copy.deepcopy(event)})
        self.assertEqual(composite - coordinator._workspace_reserve(following_workspace),
                         ordinary - coordinator._workspace_reserve(before["workspace"]))
        coordinator.admit_claim_selection(
            "worker-b", event, resolve_grant=lambda *_args: grant, lease_ms=10_000)
        after, _ = coordinator._load()
        self.assertEqual(coordinator._reserve_after(after, {"event": "message_posted"}), composite)

    def test_atomic_admission_refusal_or_append_failure_leaves_no_claim(self):
        root = tempfile.TemporaryDirectory(prefix="summon-atomic-fail-")
        self.addCleanup(root.cleanup)
        request = "d" * 64
        fixture, _unused_event, _unused_coordinator, _unused_grant = admission_fixture()
        fixture.register({"id": "recipient-fence", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "capacity", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "physical", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "selection-record", "sha256": "a" * 64}, delivery_id="delivery")
        fixture.register({"id": "whole-message-fit", "sha256": "a" * 64}, delivery_id="delivery")
        coordinator = self._workspace_coordinator(root.name, request, fixture)
        event, grant = self._fresh_atomic_admission(coordinator)[1:]
        event["payload"]["claim"]["request_sha256"] = request
        event["payload"]["selection"]["request_sha256"] = request
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "recipient_owner_fence": {"id": "recipient-fence", "sha256": "a" * 64},
            "capacity_reservation": {"id": "capacity", "sha256": "a" * 64},
            "physical_attempt_reservation": {"id": "physical", "sha256": "a" * 64},
        })
        event["payload"]["selection"]["selected_message_ids"] = ["missing"]
        with self.assertRaises(ValueError):
            coordinator.admit_claim_selection(
                "worker-b", event,
                resolve_grant=lambda _state, _workspace, _ref: grant,
                lease_ms=10_000)
        self.assertEqual(next(item for item in coordinator.status()["tasks"]
                              if item["task_id"] == "main-2")["status"], "pending")
        fixture, _unused_event, _unused_coordinator, _unused_grant = admission_fixture()
        fixture.register({"id": "recipient-fence", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "capacity", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "physical", "sha256": "a" * 64}, category="fence", delivery_id="delivery")
        fixture.register({"id": "selection-record", "sha256": "a" * 64}, delivery_id="delivery")
        fixture.register({"id": "whole-message-fit", "sha256": "a" * 64}, delivery_id="delivery")
        event, grant = self._fresh_atomic_admission(coordinator, operation_key="atomic-2")[1:]
        event["payload"]["claim"]["request_sha256"] = request
        event["payload"]["selection"]["request_sha256"] = request
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            "recipient_owner_fence": {"id": "recipient-fence", "sha256": "a" * 64},
            "capacity_reservation": {"id": "capacity", "sha256": "a" * 64},
            "physical_attempt_reservation": {"id": "physical", "sha256": "a" * 64},
        })
        with mock.patch.object(coordinator, "_append_with_state",
                               side_effect=SwarmCoordinatorError("append failed")):
            with self.assertRaises(SwarmCoordinatorError):
                coordinator.admit_claim_selection(
                    "worker-b", event,
                    resolve_grant=lambda _state, _workspace, _ref: grant,
                    lease_ms=10_000)
        self.assertEqual(next(item for item in coordinator.status()["tasks"]
                              if item["task_id"] == "main-2")["status"], "pending")
        self.assertNotIn("workspace_turn_admitted", [item["event"] for item in coordinator.events()])

    def test_direct_append_seam_cannot_bypass_state_admission(self):
        owner = swarm_module.acquire_owner(self.coordinator.run_dir, 30.0)
        try:
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator._append(owner, {"event": "message_posted"})
        finally:
            swarm_module.release_owner(owner)

    def test_creation_append_has_explicit_empty_preparation_guard(self):
        owner = swarm_module.acquire_owner(self.coordinator.run_dir, 30.0)
        try:
            with self.assertRaises(SwarmConflictError):
                self.coordinator._append_initial(owner, {"event": "message_posted"})
        finally:
            swarm_module.release_owner(owner)

    def test_only_one_live_claim_and_terminal_fence(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.assertEqual(claim["status"], "claimed")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.complete(
                "worker-b", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, envelope_sha256="c" * 64)
        done = self.coordinator.complete(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, envelope_sha256="c" * 64)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.coordinator.status()["status"], "completed")

    def test_two_coordinator_instances_share_one_durable_claim(self):
        # Separate coordinator objects model separate worker processes.  Each
        # operation must reacquire the run owner and observe the same journal;
        # a process-local active map is not sufficient protection.
        other = SwarmCoordinator(self.temp.name, "swarm-1", clock=self.clock)
        other.register_worker("worker-c", worker_instance_id="instance-c",
                              capabilities=["message"])
        first = other.claim("worker-c", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        done = self.coordinator.complete(
            "worker-c", task_id="task-a", claim_id=first["claim_id"],
            attempt=first["attempt"], lease_generation=first["lease_generation"],
            request_sha256=self.request, envelope_sha256="f" * 64)
        self.assertEqual(done["status"], "completed")

    def test_duplicate_frame_is_idempotent_and_replay_conflict_is_rejected(self):
        claim_id = "claim-fixed"
        frame = make_frame(
            "claim", run_id="swarm-1", message_id="claim-message",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"task_id": "task-a", "claim_id": claim_id, "attempt": 1,
                     "lease_generation": 1,
                     "lease_expires_at_ms": int(self.clock.value * 1000) + 10_000,
                     "request_sha256": self.request})
        first = self.coordinator.apply_frame(frame, worker_id="worker-a")
        second = self.coordinator.apply_frame(frame, worker_id="worker-a")
        self.assertEqual(first, second)
        altered = dict(frame)
        altered["payload"] = {**frame["payload"], "request_sha256": "d" * 64}
        with self.assertRaises((SwarmCoordinatorError, SwarmProtocolError)):
            self.coordinator.apply_frame(altered, worker_id="worker-a")

    def test_duplicate_frame_after_reopen_has_one_durable_event(self):
        frame = make_frame(
            "send_message", run_id="swarm-1", message_id="reopen-message",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"from": "worker-a", "to": "worker-b",
                     "content_sha256": hashlib.sha256(b"once").hexdigest(),
                     "content_chars": 4, "preview": "once"})
        self.assertEqual(self.coordinator.apply_frame(frame, worker_id="worker-a")["status"],
                         "posted")
        before = len([event for event in self.coordinator.events()
                      if event.get("message_id") == "reopen-message"])
        reopened = SwarmCoordinator(self.temp.name, "swarm-1", clock=self.clock)
        self.assertEqual(reopened.apply_frame(frame, worker_id="worker-a")["status"], "posted")
        after = len([event for event in reopened.events()
                     if event.get("message_id") == "reopen-message"])
        self.assertEqual(before, 1)
        self.assertEqual(after, 1)

    def test_prefix_sync_failure_blocks_replay_without_duplicate_append(self):
        frame = make_frame(
            "send_message", run_id="swarm-1", message_id="sync-message",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"from": "worker-a", "to": "worker-b",
                     "content_sha256": hashlib.sha256(b"sync").hexdigest(),
                     "content_chars": 4, "preview": "sync"})
        self.assertEqual(self.coordinator.apply_frame(frame, worker_id="worker-a")["status"],
                         "posted")
        reopened = SwarmCoordinator(self.temp.name, "swarm-1", clock=self.clock)
        with mock.patch.object(swarm_module.os, "fsync",
                               side_effect=OSError("simulated prefix fsync failure")):
            with self.assertRaises(SwarmCoordinatorError):
                reopened.apply_frame(frame, worker_id="worker-a")
        self.assertEqual(reopened.apply_frame(frame, worker_id="worker-a")["status"], "posted")
        self.assertEqual(len([event for event in reopened.events()
                              if event.get("message_id") == "sync-message"]), 1)

    def test_event_inserted_between_state_load_and_prefix_sync_refuses(self):
        original = self.coordinator._sync_prefix
        injected = {"done": False}

        def inject(owner, raw_map):
            if not injected["done"]:
                injected["done"] = True
                payload = swarm_module.encode_journal_record(
                    {"event": "journal_repaired", "generation": owner.generation,
                     "repaired_generation": owner.generation,
                     "message_id": "injected-repair"}, timestamp=2_000_000_000.0)
                path = Path(self.coordinator.run_dir) / f"journal-g{owner.generation}.jsonl"
                with path.open("ab") as handle:
                    handle.write(payload)
            return original(owner, raw_map)

        with mock.patch.object(self.coordinator, "_sync_prefix", side_effect=inject):
            with self.assertRaises(SwarmCorruptError):
                self.coordinator.send_message("worker-a", "worker-b", "must refuse")
        self.assertNotIn("must refuse", json_text(self.coordinator.events()))

    def test_prefix_path_replacement_with_identical_bytes_refuses(self):
        successor = rundir.acquire_owner(self.coordinator.run_dir, 30.0)
        try:
            rundir.journal_append(
                self.coordinator.run_dir,
                {"event": "journal_repaired", "generation": successor.generation,
                 "repaired_generation": 1, "message_id": "second-segment"},
                successor)
        finally:
            rundir.release_owner(successor)
        _tagged, _torn, raw_map = self.coordinator._strict_snapshot()
        owner = rundir.acquire_owner(self.coordinator.run_dir, 30.0)
        first_segment = Path(self.coordinator.run_dir) / "journal-g1.jsonl"
        original_fsync = swarm_module.os.fsync
        calls = {"count": 0}

        def replace_after_second_segment(handle):
            calls["count"] += 1
            result = original_fsync(handle)
            if calls["count"] == 2:
                original_bytes = first_segment.read_bytes()
                replacement = first_segment.with_suffix(".replacement")
                replacement.write_bytes(original_bytes)
                replacement.replace(first_segment)
            return result

        try:
            with mock.patch.object(swarm_module.os, "fsync",
                                   side_effect=replace_after_second_segment):
                with self.assertRaises(SwarmCorruptError):
                    self.coordinator._sync_prefix(owner, raw_map)
        finally:
            rundir.release_owner(owner)

    def test_renewal_extends_only_the_current_worker_claim(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        renewed = self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"],
                                         lease_ms=50_000)
        self.assertEqual(renewed["status"], "renewed")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.renew("worker-b", claim["claim_id"], claim["lease_generation"])
        with self.assertRaises(SwarmConflictError):
            self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"] + 1)

    def test_routine_renewal_budget_is_finite(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        with mock.patch.object(swarm_module, "MAX_ROUTINE_RENEWALS", 1):
            self.assertEqual(
                self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"])["status"],
                "renewed")
            with self.assertRaises(SwarmConflictError):
                self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"])

    def test_expired_claim_requires_explicit_uncertain_spend_policy(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        self.assertEqual(self.coordinator.status()["status"], "uncertain_spend")
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        blocked = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=False, reason=r"provider may have touched C:\private\repo",
            human_confirmed=True)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["uncertain_spend"])
        status = self.coordinator.status()
        self.assertEqual(status["tasks"][0]["status"], "blocked")
        self.assertTrue(status["uncertain_spend"])

    def test_expired_claim_can_retry_only_after_human_authorization(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.acknowledge_indeterminate(
                "task-a", allow_retry=True, reason="reviewed", human_confirmed=False)
        retry = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=True, reason="reviewed and explicitly re-authorized",
            human_confirmed=True)
        self.assertEqual(retry["status"], "retry_authorized")
        claim2 = self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        self.assertEqual(claim2["attempt"], 2)
        self.assertEqual(claim2["lease_generation"], claim["lease_generation"] + 1)

    def test_indeterminate_marker_is_not_appended_twice(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        first = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=True, reason="first decision", human_confirmed=True)
        self.assertEqual(first["status"], "retry_authorized")
        before = len([event for event in self.coordinator.events()
                      if event.get("event") == "claim_indeterminate"])
        second = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=True, reason="duplicate decision", human_confirmed=True)
        after = len([event for event in self.coordinator.events()
                     if event.get("event") == "claim_indeterminate"])
        self.assertEqual(second["status"], "retry_authorized")
        self.assertEqual(before, 1)
        self.assertEqual(after, 1)

    def test_expired_claim_rejects_stale_worker_and_operator_mutations(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.complete(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, envelope_sha256="c" * 64)
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.cancel("task-a")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.acknowledge_indeterminate(
                "task-a", allow_retry="false", reason="reviewed", human_confirmed="false")
        self.assertEqual(self.coordinator.status()["status"], "uncertain_spend")

    def test_indeterminate_recovery_requires_real_booleans(self):
        self.coordinator.claim("worker-a", "task-a", request_sha256=self.request, lease_ms=1_000)
        self.clock.value += 2
        for allow_retry, human_confirmed in (("false", True), (False, "true"), (1, True)):
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.acknowledge_indeterminate(
                    "task-a", allow_retry=allow_retry, reason="reviewed",
                    human_confirmed=human_confirmed)

    def test_closed_run_is_immutable(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.coordinator.fail(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, envelope_sha256="e" * 64)
        self.coordinator.close()
        with self.assertRaises(SwarmConflictError):
            self.coordinator.register_worker("worker-c", worker_instance_id="instance-c")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.send_message("worker-a", "worker-b", "late message")

    def test_message_admission_is_bounded_not_only_status_projection(self):
        with mock.patch.object(swarm_module, "MAX_JOURNAL_BYTES", 1):
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.send_message("worker-a", "worker-b", "journal blocked")
        with mock.patch.object(swarm_module, "MAX_STATUS_MESSAGES", 1):
            self.coordinator.send_message("worker-a", "worker-b", "first")
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.send_message("worker-a", "worker-b", "second")

    def test_active_claim_reserves_full_settlement_and_renewal_tail(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        _tagged, _torn, raw_map = self.coordinator._strict_snapshot()
        used = sum(len(raw) for raw in raw_map.values())
        reserve = self.coordinator._reserve_after(
            self.coordinator._load()[0],
            {"event": "message_posted"})
        self.assertGreater(reserve, swarm_module._RUN_CLOSE_BOUND)
        with mock.patch.object(
                swarm_module, "MAX_JOURNAL_BYTES", used + reserve - 1):
            with self.assertRaisesRegex(SwarmCoordinatorError, "capacity degraded"):
                self.coordinator.send_message("worker-a", "worker-b", "reserve me")

        # A terminal settlement is admitted using the post-state obligation,
        # not by pretending the active claim only needs one arbitrary record.
        state, _ = self.coordinator._load()
        terminal_reserve = self.coordinator._reserve_after(
            state, {"event": "task_failed", "claim_id": claim["claim_id"],
                    "task_id": "task-a"})
        self.assertLess(terminal_reserve, reserve)
        with mock.patch.object(
                swarm_module, "MAX_JOURNAL_BYTES",
                used + swarm_module._EVENT_BOUND_BYTES["task_failed"] + terminal_reserve):
            settled = self.coordinator.fail(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, envelope_sha256="e" * 64)
        self.assertEqual(settled["status"], "failed")

    def test_settlement_reserve_consumes_each_control_edge(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        state, _ = self.coordinator._load()
        before = self.coordinator._reserve_after(
            state, {"event": "message_posted"})
        after_request = self.coordinator._reserve_after(
            state, {"event": "cancel_requested", "claim_id": claim["claim_id"]})
        self.assertLess(after_request, before)
        self.coordinator.cancel("task-a")
        state, _ = self.coordinator._load()
        after_ack = self.coordinator._reserve_after(
            state, {"event": "cancel_acknowledged", "claim_id": claim["claim_id"]})
        self.assertLess(after_ack, after_request)

    def test_strict_read_failure_never_becomes_empty_history(self):
        journal = next(Path(self.coordinator.run_dir).glob("journal-g*.jsonl"))
        with mock.patch.object(Path, "read_bytes",
                               side_effect=PermissionError("simulated journal read failure")):
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.status()

    def test_strict_read_rejects_unstable_prefix(self):
        journal = next(Path(self.coordinator.run_dir).glob("journal-g*.jsonl"))
        first = journal.read_bytes()
        changed = first.replace(b'"ts":', b'"ts":', 1)
        # A distinct valid prefix makes the two-read stability check observable
        # without manufacturing an invalid checksum line.
        records = json.loads(first.splitlines()[0].decode("utf-8"))
        records.pop("sha256")
        records["ts"] = records.get("ts", 0) + 1
        serialized = json.dumps(records, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
        records["sha256"] = hashlib.sha256(serialized).hexdigest()
        changed = (json.dumps(records, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False) + "\n").encode("utf-8")
        with mock.patch.object(Path, "read_bytes", side_effect=[first, changed]):
            with self.assertRaises(SwarmCorruptError):
                self.coordinator.status()

    def test_managed_journal_symlink_is_rejected_before_write(self):
        target = Path(self.temp.name) / "outside-journal.jsonl"
        target.write_text("outside", encoding="utf-8")
        link = Path(self.coordinator.run_dir) / "journal-g99.jsonl"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.send_message("worker-a", "worker-b", "must not follow link")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_cancel_requires_ack_and_terminal_outcome(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        requested = self.coordinator.cancel("task-a", reason="operator requested")
        self.assertEqual(requested["status"], "cancel_requested")
        ack = make_frame("ack_cancel", run_id="swarm-1", message_id="cancel-ack",
                         sent_at_ms=int(self.clock.value * 1000),
                         payload={"claim_id": claim["claim_id"],
                                  "lease_generation": claim["lease_generation"]})
        self.assertEqual(self.coordinator.apply_frame(ack, worker_id="worker-a")["status"],
                         "cancel_acknowledged")
        cancelled = make_frame("cancelled", run_id="swarm-1", message_id="cancelled",
                               sent_at_ms=int(self.clock.value * 1000),
                               payload={"claim_id": claim["claim_id"],
                                        "lease_generation": claim["lease_generation"],
                                        "reason": "stopped"})
        self.assertEqual(self.coordinator.apply_frame(cancelled, worker_id="worker-a")["status"],
                         "cancelled")
        self.assertEqual(self.coordinator.status()["tasks"][0]["status"], "cancelled")

    def test_duplicate_cancel_ack_with_new_message_id_refuses_before_append(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.coordinator.cancel("task-a")
        ack_payload = {"claim_id": claim["claim_id"],
                       "lease_generation": claim["lease_generation"]}
        first = make_frame("ack_cancel", run_id="swarm-1", message_id="ack-one",
                           sent_at_ms=int(self.clock.value * 1000), payload=ack_payload)
        self.assertEqual(self.coordinator.apply_frame(first, worker_id="worker-a")["status"],
                         "cancel_acknowledged")
        event_count = len(self.coordinator.events())
        second = make_frame("ack_cancel", run_id="swarm-1", message_id="ack-two",
                            sent_at_ms=int(self.clock.value * 1000), payload=ack_payload)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.apply_frame(second, worker_id="worker-a")
        self.assertEqual(len(self.coordinator.events()), event_count)

    def test_message_is_context_only_and_publicly_redacted(self):
        sent = self.coordinator.send_message("worker-a", "worker-b", r"look at C:\private\repo")
        self.assertEqual(sent["status"], "posted")
        event = [e for e in self.coordinator.events() if e.get("event") == "message_posted"][-1]
        self.assertNotIn(r"C:\private\repo", json_text(event))
        self.assertEqual(event["to"], "worker-b")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.send_message("worker-a", "not-registered", "no")

    def test_message_preview_redacts_paths_and_credentials(self):
        samples = [
            r"C:\private folder\repo",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "SECRET_TOKEN=do-not-publish",
            "cookie: session=do-not-publish",
        ]
        for index, sample in enumerate(samples):
            self.coordinator.send_message("worker-a", "worker-b", sample,
                                          message_id=f"redaction-{index}")
        text = json_text(self.coordinator.events())
        for sample in samples:
            self.assertNotIn(sample, text)
        self.assertNotIn("do-not-publish", text)

    def test_artifact_is_claim_fenced_and_never_dereferenced(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        artifact = self.coordinator.publish_artifact(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, artifact_id="artifact-a", sha256="d" * 64,
            bytes_count=12, media_type="text/plain", relative_path="out.txt")
        self.assertEqual(artifact["status"], "artifact_published")
        reopened = swarm_module.SwarmCoordinator(self.temp.name, "swarm-1", clock=self.clock)
        reopened_state, _ = reopened._load()
        self.assertEqual(reopened_state["artifacts"][0]["sha256"], "d" * 64)
        artifact_event = next(
            event for event in reopened.events() if event.get("event") == "artifact_published"
        )
        self.assertEqual(artifact_event["sha256"], "d" * 64)
        self.assertNotIn("artifact_sha256", artifact_event)
        with self.assertRaises(SwarmProtocolError):
            self.coordinator.publish_artifact(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, artifact_id="artifact-b", sha256="d" * 64,
                bytes_count=1, media_type="text/plain", relative_path=r"..\secret.txt")

    def test_new_frame_reusing_artifact_id_refuses_before_append(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.coordinator.publish_artifact(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, artifact_id="artifact-reused", sha256="d" * 64,
            bytes_count=12, media_type="text/plain", relative_path="out.txt",
            message_id="artifact-first")
        event_count = len(self.coordinator.events())
        with self.assertRaises(SwarmConflictError):
            self.coordinator.publish_artifact(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, artifact_id="artifact-reused", sha256="e" * 64,
                bytes_count=13, media_type="text/plain", relative_path="out-2.txt",
                message_id="artifact-second")
        self.assertEqual(len(self.coordinator.events()), event_count)

    def test_foreign_frame_run_and_timestamp_are_refused(self):
        frame = make_frame("poll", run_id="other-run", message_id="poll-1",
                           sent_at_ms=int(self.clock.value * 1000))
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.apply_frame(frame)
        stale = make_frame("poll", run_id="swarm-1", message_id="poll-2",
                           sent_at_ms=int(self.clock.value * 1000) - 600_001)
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.apply_frame(stale)

    def test_close_requires_no_live_claim_and_is_idempotent(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.close()
        self.coordinator.fail("worker-a", task_id="task-a", claim_id=claim["claim_id"],
                              attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                              request_sha256=self.request, envelope_sha256="e" * 64)
        self.assertEqual(self.coordinator.close()["status"], "closed")
        self.assertEqual(self.coordinator.close()["status"], "closed")

    def test_torn_tail_is_repaired_on_next_mutation(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        journal = max(Path(self.coordinator.run_dir).glob("journal-g*.jsonl"),
                      key=lambda path: int(path.stem.removeprefix("journal-g")))
        with journal.open("ab") as handle:
            handle.write(b'{"torn":')
        self.assertTrue(self.coordinator.status()["torn_tail"])
        self.coordinator.fail("worker-a", task_id="task-a", claim_id=claim["claim_id"],
                              attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                              request_sha256=self.request, envelope_sha256="e" * 64)
        self.assertFalse(self.coordinator.status()["torn_tail"])

    def test_legacy_crlf_journal_prefix_remains_readable(self):
        for journal in Path(self.coordinator.run_dir).glob("journal-g*.jsonl"):
            journal.write_bytes(journal.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.coordinator.status()["status"], "prepared")

    def test_cli_create_and_status_are_provider_inert(self):
        task_file = Path(self.temp.name) / "tasks.json"
        task_file.write_text(json.dumps([{"task_id": "task-a", "request_sha256": self.request}]),
                             encoding="utf-8")
        command = [sys.executable, str(HERE / "run_subagent.py")]
        create = subprocess.run(
            command + ["swarm", "create", "cli-run", "--swarm-dir", self.temp.name,
                       "--swarm-tasks", str(task_file),
                       "--swarm-project-root-sha256", self.project,
                       "--swarm-roster-sha256", self.roster, "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False,
            **run_flags())
        self.assertEqual(create.returncode, 0, create.stderr)
        self.assertEqual(json.loads(create.stdout)["status"], "prepared")
        status = subprocess.run(
            command + ["swarm", "status", "cli-run", "--swarm-dir", self.temp.name, "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False,
            **run_flags())
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["run_id"], "cli-run")


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
