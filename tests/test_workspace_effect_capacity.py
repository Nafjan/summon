"""Exact-capacity fixture proof for explicit fixed-worker effect resolution.

Reuses the frozen atomic-capacity harness without changing its assertions. These
are synthetic source references admitted through the actual sole writer; actual
owned-child source provenance is tested separately by the runtime/demo lane.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_workspace_atomic_capacity as fixture
import _workspace_protocol as protocol
import _workspace_state as projection
import _swarm_coordinator as swarm


class EffectCapacityTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.AtomicCapacityTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def _acknowledged(self):
        h = self.h
        baseline, event, grant, _unused = h.prepared(future_no_contact_proof=False)
        h.finish_other_tasks(baseline)
        _calibration, cap = h.calibrated(baseline, event, grant)
        coordinator = h.clone(baseline, "effect-resolution")
        queued = coordinator._load()[0]["workspace"]["deliveries"]["delivery"]
        self.assertIn("effects_resolved", projection.delivery_settlement_targets(queued))
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            h.admit(coordinator, event, grant)
            self.assertEqual(h.used(coordinator) + h.reserve(coordinator), cap)
            for target in ("submission_started", "submitted", "acknowledged"):
                h.advance_obtained(coordinator, target, cap, event)
            h.complete_claim(coordinator, "main-2", "fresh-claim")
        return coordinator, cap

    def _register_resolution_sources(self, coordinator, cap):
        h = self.h
        refs = {}
        for role, category in protocol.EFFECT_EVIDENCE_CATEGORIES.items():
            reference = {"id": "obtained-" + role.replace("_", "-"), "sha256": "e" * 64}
            refs[role] = reference
            before = h.reserve(coordinator)
            h.append_measured(coordinator, h.workspace_event(coordinator, "workspace_evidence_registered", {
                "reference": reference, "category": category, "task_id": "main-2", "delivery_id": "delivery",
                "settlement_for": {"delivery_id": "delivery", "target_state": "effects_resolved", "role": role}},
                "register-" + reference["id"]), cap)
            self.assertEqual(before - h.reserve(coordinator), swarm._BOUND_WORKSPACE_EVIDENCE)
        return refs

    def _resolution(self, coordinator, evidence, key="resolve-fixed-effects"):
        after = copy.deepcopy(coordinator._load()[0]["workspace"]["deliveries"]["delivery"])
        after["certainty"] = {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}
        return self.h.workspace_event(coordinator, "workspace_effects_resolved", {
            "delivery": after, "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
            "qualification": "simulated", "evidence": evidence}, key)

    def test_same_cap_late_resolution_sources_event_and_close_preserve_receipts(self):
        h = self.h
        coordinator, cap = self._acknowledged()
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            acknowledged = copy.deepcopy(coordinator._load()[0]["workspace"]["deliveries"]["delivery"])
            self.assertEqual(coordinator.status()["status"], "settlement_required")
            with self.assertRaises(swarm.SwarmConflictError):
                coordinator.close()
            refs = self._register_resolution_sources(coordinator, cap)
            before_resolve = h.reserve(coordinator)
            event = self._resolution(coordinator, refs)
            h.append_measured(coordinator, event, cap)
            # The resolution wrapper and the distinct uncertainty tail both
            # disappear only on this explicit, structurally proven operation.
            self.assertEqual(before_resolve - h.reserve(coordinator), 2 * swarm._BOUND_WORKSPACE_WRAPPER)
            state = h.assert_replay_delivery(coordinator, "acknowledged")
            resolved = state["workspace"]["deliveries"]["delivery"]
            self.assertEqual({k: v for k, v in resolved.items() if k != "certainty"},
                             {k: v for k, v in acknowledged.items() if k != "certainty"})
            self.assertEqual(resolved["certainty"], {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"})
            self.assertEqual(state["workspace"]["effect_resolutions"]["delivery"]["qualification"], "simulated")
            report = coordinator.status()
            self.assertFalse(report["uncertain_spend"])
            self.assertTrue(report["workspace"]["closure_ready"])
            self.assertEqual(report["workspace"]["effect_resolution"]["qualification"], "simulated")
            reserve = h.reserve(coordinator)
            h.append_fixture_event(coordinator, event)
            self.assertEqual(h.reserve(coordinator), reserve)
            self.assertEqual(len(coordinator._load()[0]["workspace"]["effect_resolutions"]), 1)
            self.assertEqual(coordinator.close()["status"], "closed")
            self.assertLessEqual(h.used(coordinator), cap)
        reopened = swarm.SwarmCoordinator(coordinator.runs_root, "run", clock=lambda: fixture.STAMP)
        self.assertEqual(reopened.status()["status"], "closed")
        prior_states = [item.get("workspace_event", {}).get("payload", {}).get("delivery", {}).get("state")
                        for item in h.records(reopened)]
        self.assertIn("submitted", prior_states)
        self.assertIn("acknowledged", prior_states)

    def test_bad_slot_and_resolution_bindings_refuse_without_journal_or_credit_change(self):
        h = self.h
        coordinator, cap = self._acknowledged()
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            for mutation in ("category", "task", "delivery"):
                with self.subTest(registration=mutation):
                    payload = {"reference": {"id": "bad-source", "sha256": "e" * 64},
                        "category": "observation", "task_id": "main-2", "delivery_id": "delivery",
                        "settlement_for": {"delivery_id": "delivery", "target_state": "effects_resolved",
                                           "role": "fixed_execution_scope"}}
                    payload[{"category": "category", "task": "task_id", "delivery": "delivery_id"}[mutation]] = (
                        "artifact" if mutation == "category" else "side-1" if mutation == "task" else "missing")
                    before, reserve = h.journals(coordinator), h.reserve(coordinator)
                    with self.assertRaises((projection.WorkspaceStateError, swarm.SwarmCoordinatorError)):
                        h.append_fixture_event(coordinator, h.workspace_event(coordinator,
                            "workspace_evidence_registered", payload, "bad-registration-" + mutation))
                    self.assertEqual(h.journals(coordinator), before)
                    self.assertEqual(h.reserve(coordinator), reserve)
            refs = self._register_resolution_sources(coordinator, cap)
            event = self._resolution(coordinator, refs)
            for mutation in ("unregistered", "swapped", "selection", "qualification", "contact"):
                with self.subTest(resolution=mutation):
                    bad = copy.deepcopy(event)
                    payload = bad["payload"]
                    if mutation == "unregistered":
                        payload["evidence"]["fixed_execution_scope"] = {"id": "missing", "sha256": "e" * 64}
                    elif mutation == "swapped":
                        payload["evidence"] = {"fixed_execution_scope": refs["owned_child_cleanup"],
                                               "owned_child_cleanup": refs["fixed_execution_scope"]}
                    elif mutation == "selection":
                        payload["delivery"]["selection"]["context_sha256"] = "f" * 64
                    elif mutation == "qualification":
                        payload["qualification"] = "provider_verified"
                    else:
                        payload["delivery"]["certainty"]["contact"] = "none"
                    before, reserve = h.journals(coordinator), h.reserve(coordinator)
                    with self.assertRaises((projection.WorkspaceStateError, swarm.SwarmCoordinatorError)):
                        h.append_fixture_event(coordinator, bad)
                    self.assertEqual(h.journals(coordinator), before)
                    self.assertEqual(h.reserve(coordinator), reserve)


if __name__ == "__main__":
    unittest.main()
