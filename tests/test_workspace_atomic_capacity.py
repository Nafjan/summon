"""Private journaled-fixture acceptance of composite workspace admission.

Public workspace event classes remain disabled. Preparation and disposition use
the existing private fixture wrapper through the REAL coordinator append seam;
no admission/reserve/append success is mocked. This does not qualify a public
workspace adapter, provider execution or power-loss durability.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "summon" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _rundir as rd
import _swarm_coordinator as swarm
import _workspace_state as workspace_state
from _swarm_protocol import make_frame
from _workspace_runtime import _WorkspaceCoordinator, _task_view
from test_workspace_admission import admission_fixture
from test_workspace_protocol import delivery, evidence_for


STAMP = 2_000_000_000.25
REQUEST = "d" * 64


class AtomicCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-atomic-capacity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        timer = mock.patch.object(swarm.time, "time", return_value=STAMP)
        timer.start()
        self.addCleanup(timer.stop)

    def journals(self, coordinator):
        return {p.name: p.read_bytes() for p in Path(coordinator.run_dir).glob("journal-g*.jsonl")}

    def used(self, coordinator):
        return sum(map(len, self.journals(coordinator).values()))

    def records(self, coordinator):
        return [json.loads(line) for raw in self.journals(coordinator).values() for line in raw.splitlines()]

    def reserve(self, coordinator):
        return coordinator._reserve_after(coordinator._load()[0], {"event": "message_posted"})

    def append_fixture_event(self, coordinator, event):
        with coordinator._mutation() as (owner, state, _torn):
            workspace_state.apply_event(
                state.get("workspace"), event,
                _task_view(state, run_id=coordinator.run_id, now_ms=int(STAMP * 1000)))
            coordinator._append_with_state(
                owner, {"event": "workspace_event", "workspace_event": copy.deepcopy(event),
                        "observed_at_ms": int(STAMP * 1000)}, state=state)

    def prepared(self, *, future_no_contact_proof=True):
        fixture, event, _unused, grant = admission_fixture()
        for name, category in (("recipient-fence", "fence"), ("capacity", "fence"),
                               ("physical", "fence"), ("selection-record", "event"),
                               ("whole-message-fit", "event")):
            fixture.register({"id": name, "sha256": "a" * 64},
                             category=category, delivery_id="delivery")
        # Known no-contact fixture evidence is journaled before selection.
        # It is synthetic reducer evidence, not a provider-authored receipt.
        no_contact_proof = fixture.proofs("not_submitted") if future_no_contact_proof else None
        coordinator = _WorkspaceCoordinator.create(
            self.root / "baseline", "run", project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64,
            tasks=[{"task_id": task, "request_sha256": REQUEST}
                   for task in ("main-1", "side-1", "main-2")],
            max_attempts=2, clock=lambda: STAMP)
        coordinator.register_worker("worker-b", worker_instance_id="worker-b")
        for seeded, _view in fixture.steps:
            self.append_fixture_event(coordinator, seeded)
        event["operation_key"] = "atomic-capacity"
        event["expected_revision"] = coordinator._load()[0]["workspace"]["revision"]
        event["payload"]["claim"].update({
            "claim_id": "fresh-claim", "worker_id": "worker-b", "lease_generation": 1,
            "lease_expires_at_ms": int(STAMP * 1000) + 10_000,
            "request_sha256": REQUEST, "attempt": 1})
        event["payload"]["selection"].update({
            "claim_id": "fresh-claim", "owner_generation": 1,
            "request_sha256": REQUEST, "attempt": 1})
        event["payload"]["supported_ack_levels"] = []
        event["payload"]["evidence_by_delivery"]["delivery"].update({
            key: {"id": name, "sha256": "a" * 64}
            for key, name in (("recipient_owner_fence", "recipient-fence"),
                              ("capacity_reservation", "capacity"),
                              ("physical_attempt_reservation", "physical"),
                              ("selection_record", "selection-record"),
                              ("whole_message_fit", "whole-message-fit"))})
        return coordinator, event, grant, no_contact_proof

    def clone(self, coordinator, label):
        destination = self.root / label
        shutil.copytree(coordinator.runs_root, destination)
        return _WorkspaceCoordinator(destination, coordinator.run_id, clock=lambda: STAMP)

    def admit(self, coordinator, event, grant):
        return coordinator.admit_claim_selection(
            "worker-b", copy.deepcopy(event),
            resolve_grant=lambda _state, _workspace, _ref: copy.deepcopy(grant),
            lease_ms=10_000)

    def calibrated(self, baseline, event, grant):
        calibration = self.clone(baseline, "calibration")
        self.assertEqual(self.admit(calibration, event, grant)["status"], "admitted")
        added = self.used(calibration) - self.used(baseline)
        composite = [record for record in self.records(calibration)
                     if record["event"] == "workspace_turn_admitted"]
        self.assertEqual(len(composite), 1)
        data = copy.deepcopy(composite[0])
        data.pop("sha256")
        self.assertEqual(added, len(rd.encode_journal_record(data, timestamp=data["ts"])))
        # Post-replay state contains the real new claim and delivery. Measuring
        # its reserve avoids repeating the composite-event projection logic.
        required = self.used(calibration) + self.reserve(calibration)
        return calibration, required

    def workspace_event(self, coordinator, kind, payload, key):
        state, torn = coordinator._load()
        self.assertFalse(torn)
        return {"event": kind, "protocol": "summon.workspace/v1",
                "workspace_id": "workspace", "run_id": "run",
                "operation_key": key, "expected_revision": state["workspace"]["revision"],
                "payload": copy.deepcopy(payload)}

    def append_measured(self, coordinator, event, cap):
        before = self.used(coordinator)
        count = len(self.records(coordinator))
        self.append_fixture_event(coordinator, event)
        records = self.records(coordinator)
        self.assertEqual(len(records), count + 1)
        written = next(record for record in records
                       if record.get("workspace_event", {}).get("operation_key") == event["operation_key"])
        encoded = copy.deepcopy(written)
        encoded.pop("sha256")
        self.assertEqual(self.used(coordinator) - before,
                         len(rd.encode_journal_record(encoded, timestamp=encoded["ts"])))
        self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)

    def obtained_evidence(self, coordinator, target, cap, admission):
        """Journal synthetic proof when obtained, using one explicit reserve slot.

        These locally constructed records exercise the reducer contract; they
        are not receipts from a provider or evidence of actual provider contact.
        Admission-time grant/fences already exist; future launch/receipt/hold
        facts must each be persisted now, after the real composite admission.
        """
        proof = evidence_for(target)
        admitted = admission["payload"]["evidence_by_delivery"]["delivery"]
        for role, reference in list(proof.items()):
            if type(reference) is not dict:
                continue
            if role in admitted:
                proof[role] = copy.deepcopy(admitted[role])
                continue
            reference = {"id": "obtained-" + target + "-" + role.replace("_", "-"),
                         "sha256": "e" * 64}
            proof[role] = reference
            state, _ = coordinator._load()
            self.assertNotIn(reference["id"], state["workspace"]["evidence"])
            category = {"adapter_receipt": "adapter_receipt", "ack_receipt": "adapter_receipt",
                        "no_contact_receipt": "adapter_receipt", "cleanup_evidence": "fence",
                        "hold_observation": "observation"}.get(role, "event")
            event = self.workspace_event(coordinator, "workspace_evidence_registered", {
                "reference": reference, "category": category, "task_id": "main-2",
                "delivery_id": "delivery", "settlement_for": {
                    "delivery_id": "delivery", "target_state": target, "role": role}},
                "register-" + reference["id"])
            self.append_measured(coordinator, event, cap)
        return proof

    def advance_obtained(self, coordinator, target, cap, admission, *, clean_no_contact=False):
        proof = self.obtained_evidence(coordinator, target, cap, admission)
        state, _ = coordinator._load()
        after = copy.deepcopy(state["workspace"]["deliveries"]["delivery"])
        template = delivery(target)
        after.update({key: template[key] for key in ("state", "reason", "ack_level")})
        if target in {"submission_started", "submitted", "not_submitted"}:
            after["certainty"] = copy.deepcopy(template["certainty"])
        if clean_no_contact:
            self.assertEqual(target, "not_submitted")
            after["certainty"] = {"contact": "none", "spend": "not_incurred", "cleanup": "complete"}
        event = self.workspace_event(coordinator, "workspace_delivery_advanced", {
            "delivery": after, "evidence": proof, "supported_ack_levels": ["recipient_received"]},
            "advance-" + target)
        self.append_measured(coordinator, event, cap)

    def complete_claim(self, coordinator, task_id, claim_id):
        state, _ = coordinator._load()
        claim = state["claims"][claim_id]
        coordinator.complete("worker-b", task_id=task_id, claim_id=claim_id,
                             attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                             request_sha256=REQUEST, envelope_sha256="f" * 64,
                             message_id="complete-" + task_id)

    def finish_other_tasks(self, coordinator):
        for task_id in ("main-1", "side-1"):
            claimed = coordinator.claim("worker-b", task_id, request_sha256=REQUEST,
                                        lease_ms=10_000, message_id="claim-" + task_id)
            self.complete_claim(coordinator, task_id, claimed["claim_id"])

    def assert_replay_delivery(self, coordinator, expected_state):
        reopened = _WorkspaceCoordinator(coordinator.runs_root, "run", clock=lambda: STAMP)
        state, torn = reopened._load()
        self.assertFalse(torn)
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], expected_state)
        self.assertEqual(self.journals(reopened), self.journals(coordinator))
        self.assertEqual(sum(record["event"] == "workspace_turn_admitted"
                             for record in self.records(reopened)), 1)
        return state

    def test_exact_cap_full_submission_with_new_evidence_and_acknowledgement(self):
        baseline, event, grant, _proof = self.prepared(future_no_contact_proof=False)
        _calibration, cap = self.calibrated(baseline, event, grant)
        coordinator = self.clone(baseline, "acknowledgement")
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            self.admit(coordinator, event, grant)
            self.assertEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            for target in ("submission_started", "submitted", "acknowledged"):
                self.advance_obtained(coordinator, target, cap, event)
                self.assert_replay_delivery(coordinator, target)
            self.complete_claim(coordinator, "main-2", "fresh-claim")
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
        state = self.assert_replay_delivery(coordinator, "acknowledged")
        self.assertEqual(state["tasks"]["main-2"]["terminal"], "completed")
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["certainty"],
                         {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"})
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["ack_level"], "recipient_received")

    def test_exact_cap_only_valid_unique_settlement_evidence_consumes_reserve(self):
        baseline, event, grant, _proof = self.prepared(future_no_contact_proof=False)
        _calibration, cap = self.calibrated(baseline, event, grant)
        coordinator = self.clone(baseline, "proof-credit")
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            self.admit(coordinator, event, grant)
            self.assertEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            payload = {"reference": {"id": "unrelated-proof", "sha256": "e" * 64},
                       "category": "event", "task_id": "main-2", "delivery_id": "delivery"}
            ordinary = self.workspace_event(coordinator, "workspace_evidence_registered",
                                            payload, "ordinary-proof")
            before = self.journals(coordinator)
            reserve_before = self.reserve(coordinator)
            with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                self.append_fixture_event(coordinator, ordinary)
            self.assertEqual(self.journals(coordinator), before)
            self.assertEqual(self.reserve(coordinator), reserve_before)
            invalid = copy.deepcopy(payload)
            invalid["settlement_for"] = {"delivery_id": "delivery", "target_state": "submission_started",
                                          "role": "ack_receipt"}
            invalid_event = self.workspace_event(coordinator, "workspace_evidence_registered",
                                                 invalid, "invalid-role")
            with self.assertRaises((workspace_state.WorkspaceStateError, swarm.SwarmCoordinatorError)):
                self.append_fixture_event(coordinator, invalid_event)
            self.assertEqual(self.journals(coordinator), before)
            self.assertEqual(self.reserve(coordinator), reserve_before)
            proof = self.obtained_evidence(coordinator, "submission_started", cap, event)
            reserve_after = self.reserve(coordinator)
            self.assertLess(reserve_after, reserve_before)
            registered = coordinator._load()[0]["workspace"]["evidence"][proof["durable_launch_intent"]["id"]]
            conflict = copy.deepcopy(registered)
            conflict["reference"] = {"id": "second-launch-proof", "sha256": "b" * 64}
            conflict_event = self.workspace_event(coordinator, "workspace_evidence_registered",
                                                  conflict, "duplicate-slot")
            before = self.journals(coordinator)
            with self.assertRaises((workspace_state.WorkspaceStateError, swarm.SwarmCoordinatorError)):
                self.append_fixture_event(coordinator, conflict_event)
            self.assertEqual(self.journals(coordinator), before)
            self.assertEqual(self.reserve(coordinator), reserve_after)
            # Exact operation replay may be refused or append a replay record
            # through this private fixture seam; it must never mint more credit.
            original = next(record["workspace_event"] for record in self.records(coordinator)
                            if record.get("workspace_event", {}).get("operation_key") ==
                            "register-" + proof["durable_launch_intent"]["id"])
            try:
                self.append_fixture_event(coordinator, original)
            except (workspace_state.WorkspaceStateError, swarm.SwarmCoordinatorError):
                pass
            self.assertEqual(self.reserve(coordinator), reserve_after)
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
        self.assert_replay_delivery(coordinator, "included_in_attempt")

    def test_exact_cap_interrupted_delivery_has_new_hold_and_explicit_disposition(self):
        baseline, event, grant, _proof = self.prepared(future_no_contact_proof=False)
        _calibration, cap = self.calibrated(baseline, event, grant)
        coordinator = self.clone(baseline, "held")
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            self.admit(coordinator, event, grant)
            self.assertEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            for target in ("submission_started", "held_for_recovery", "dead_lettered"):
                self.advance_obtained(coordinator, target, cap, event)
                self.assert_replay_delivery(coordinator, target)
            coordinator.acknowledge_indeterminate("main-2", allow_retry=False,
                                                  reason="synthetic interruption", human_confirmed=True)
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
        state = self.assert_replay_delivery(coordinator, "dead_lettered")
        self.assertEqual(state["tasks"]["main-2"]["terminal"], "blocked")
        self.assertTrue(state["tasks"]["main-2"]["uncertain_spend"])
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["certainty"],
                         {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})

    def test_terminal_tasks_cannot_close_unresolved_delivery_then_clean_disposition_closes(self):
        baseline, event, grant, _proof = self.prepared(future_no_contact_proof=False)
        # Finish unrelated work before calibration: no later claim admission
        # borrows the selected delivery's reserved settlement tail.
        self.finish_other_tasks(baseline)
        _calibration, cap = self.calibrated(baseline, event, grant)
        coordinator = self.clone(baseline, "close-guard")
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            self.admit(coordinator, event, grant)
            self.assertEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            self.complete_claim(coordinator, "main-2", "fresh-claim")
            state, _ = coordinator._load()
            self.assertTrue(all(task["terminal"] == "completed" for task in state["tasks"].values()))
            self.assertFalse(any(claim["status"] == "active" for claim in state["claims"].values()))
            before = self.journals(coordinator)
            with self.assertRaisesRegex(swarm.SwarmConflictError, "delivery|deliveries|workspace"):
                coordinator.close()
            self.assertEqual(self.journals(coordinator), before)
            self.assertFalse(coordinator._load()[0]["closed"])
            self.advance_obtained(coordinator, "not_submitted", cap, event, clean_no_contact=True)
            self.assertEqual(coordinator.close()["status"], "closed")
            self.assertLessEqual(self.used(coordinator), cap)
        state = self.assert_replay_delivery(coordinator, "not_submitted")
        self.assertTrue(state["closed"])
        self.assertEqual(sum(record["event"] == "run_closed" for record in self.records(coordinator)), 1)

    def test_exact_composite_boundary_includes_claim_and_has_no_half_admission(self):
        baseline, event, grant, _proof = self.prepared()
        calibration, required = self.calibrated(baseline, event, grant)
        for delta in (-1, 0, 1):
            with self.subTest(cap_delta=delta):
                coordinator = self.clone(baseline, "boundary-" + str(delta))
                before = self.journals(coordinator)
                with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", required + delta):
                    if delta == -1:
                        with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                            self.admit(coordinator, event, grant)
                        self.assertEqual(self.journals(coordinator), before)
                        state, torn = coordinator._load()
                        self.assertFalse(torn)
                        self.assertEqual(state["tasks"]["main-2"]["attempts"], [])
                        self.assertNotIn("fresh-claim", state["claims"])
                        self.assertNotIn("atomic-capacity", state.get("admissions", {}))
                        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], "queued")
                    else:
                        self.admit(coordinator, event, grant)
                        self.assertEqual(self.journals(coordinator), self.journals(calibration))
                        self.assertEqual(self.used(coordinator) + self.reserve(coordinator), required)
                        reopened = _WorkspaceCoordinator(coordinator.runs_root, "run", clock=lambda: STAMP)
                        state, torn = reopened._load()
                        self.assertFalse(torn)
                        self.assertEqual(state["tasks"]["main-2"]["attempts"], ["fresh-claim"])
                        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], "included_in_attempt")

    def test_same_cap_preserves_claim_cancellation_and_delivery_disposition(self):
        baseline, event, grant, proof = self.prepared()
        _calibration, required = self.calibrated(baseline, event, grant)
        coordinator = self.clone(baseline, "settle")
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", required):
            self.admit(coordinator, event, grant)
            before = self.journals(coordinator)
            with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                coordinator.send_message("worker-b", "coordinator", "ordinary", message_id="ordinary-refused")
            self.assertEqual(self.journals(coordinator), before)
            coordinator.cancel("main-2", reason="\x01" * 512, message_id="cancel-request")
            for kind, identifier, reason in (("ack_cancel", "cancel-ack", False),
                                             ("cancelled", "cancel-outcome", True)):
                payload = {"claim_id": "fresh-claim", "lease_generation": 1}
                if reason:
                    payload["reason"] = "\x01" * 512
                frame = make_frame(kind, run_id="run", message_id=identifier,
                                   sent_at_ms=int(STAMP * 1000), payload=payload)
                coordinator.apply_frame(frame, worker_id="worker-b")
                self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), required)
            state, _ = coordinator._load()
            self.assertEqual(state["tasks"]["main-2"]["terminal"], "cancelled")
            self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], "included_in_attempt")
            # Claim completion cannot refund an unresolved delivery obligation.
            without_workspace = copy.deepcopy(state)
            without_workspace.pop("workspace")
            ordinary_only = coordinator._reserve_after(without_workspace, {"event": "message_posted"})
            self.assertGreater(self.reserve(coordinator), ordinary_only,
                               "unresolved workspace delivery has no settlement reserve")
            after = copy.deepcopy(state["workspace"]["deliveries"]["delivery"])
            template = delivery("not_submitted")
            after.update({key: template[key] for key in ("state", "reason", "certainty", "ack_level")})
            # This fixture launched nothing and has a registered cleanup fence;
            # settle the complete no-contact disposition, not the template's
            # deliberately unfinished cleanup default.
            after["certainty"] = {"contact": "none", "spend": "not_incurred", "cleanup": "complete"}
            disposition = {"event": "workspace_delivery_advanced", "protocol": event["protocol"],
                           "workspace_id": event["workspace_id"], "run_id": "run",
                           "operation_key": "delivery-disposition",
                           "expected_revision": state["workspace"]["revision"],
                           "payload": {"delivery": after, "evidence": proof, "supported_ack_levels": []}}
            self.append_fixture_event(coordinator, disposition)
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), required)
        reopened = _WorkspaceCoordinator(coordinator.runs_root, "run", clock=lambda: STAMP)
        state, torn = reopened._load()
        self.assertFalse(torn)
        self.assertEqual(state["tasks"]["main-2"]["terminal"], "cancelled")
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["state"], "not_submitted")
        self.assertEqual(state["workspace"]["deliveries"]["delivery"]["certainty"],
                         {"contact": "none", "spend": "not_incurred", "cleanup": "complete"})
        events = [record["event"] for record in self.records(reopened)]
        self.assertEqual(events.count("workspace_turn_admitted"), 1)
        self.assertEqual(events.count("claim_granted"), 0)
        self.assertEqual(events.count("task_cancelled"), 1)


if __name__ == "__main__":
    unittest.main()
