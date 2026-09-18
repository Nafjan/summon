"""Real public-path conductor simulation. No capability override or journal injection."""
from pathlib import Path
import copy
import json
import subprocess
import sys
import tempfile
import unittest
import threading
from unittest import mock

from _workspace_demo import ConductorDemo, canonical
from _workspace_runtime import WorkspaceRuntimeError
from _workspace_content import ContentRef
from _workspace_transport import OwnedSupervisorConsumer
from _spawn import run_flags
import _workspace_state as projection
import _submission_accounting
import _swarm_coordinator as swarm_module
from _swarm_coordinator import SwarmBudgetRefusal
from _rundir import encode_journal_record


class ConductorDemoTests(unittest.TestCase):
    def test_v2_economics_timestamp_width_uses_finite_encoded_allowance(self):
        """The capacity proof pads a measured exemplar without changing the writer."""
        record = {"event": "timestamp-width-probe", "generation": 1,
                  "message_id": "timestamp-width-probe"}
        timestamps = (
            0.0,
            1_788_922_513.1234567,
            9_999_999_999_999.0,
            -9_999_999_999_999.0,
        )
        widths = []
        for timestamp in timestamps:
            payload = encode_journal_record(record, timestamp=timestamp)
            parsed = json.loads(payload[:-1].decode("utf-8"))
            width = len(json.dumps(parsed["ts"], ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")))
            widths.append(width)
            self.assertLessEqual(width, 32)
        self.assertEqual(
            swarm_module._economics_timestamp_padding(swarm_module._BOUND_TIMESTAMP),
            17)
        self.assertEqual(max(widths), 18)
        with self.assertRaises(ValueError):
            swarm_module._economics_timestamp_padding(float("inf"))
        with self.assertRaises(ValueError):
            swarm_module._economics_timestamp_padding("not-a-number")

    def test_v2_economics_path_derives_policy_bound_reservation_and_settles(self):
        with tempfile.TemporaryDirectory(prefix="summon-v2-economics-demo-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                demo.prepare(economics=True)
                grant = demo.grant("main-1", "worker-v2")[0]
                demo.launch("main-1", "worker-v2", grant)
                entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-v2", grant, [entry])
                reservation = turn["event"]["payload"]["economics_reservation"]
                self.assertEqual(reservation["status"], "reserved")
                self.assertEqual(reservation["payload_sha256"], turn["event"]["payload"]["selection"]["context_sha256"])
                self.assertNotEqual(reservation["material_sha256"], reservation["payload_sha256"])
                settled = demo.settle_economics(turn)
                self.assertEqual(settled["status"], "settled")
                state = demo.state()
                stored = state["admissions"][turn["event"]["operation_key"]]["economics_settlement"]
                self.assertEqual(stored["submission_state"], "not_submitted")
                self.assertFalse(stored["accounting"]["unknown_spend"])
                with self.assertRaises((ValueError, RuntimeError)):
                    demo.runtime._coordinator.close()
            finally:
                demo.cleanup()

    def test_v2_economics_capacity_bound_covers_admitted_shapes_and_replay(self):
        """The admission reserve must dominate every finite accepted outcome."""
        cases = (
            ("not-submitted", "not_submitted", "cancelled", False),
            ("submitted-reported", "submitted", "success", True),
            ("submitted-missing", "submitted", "cancelled", False),
            ("indeterminate", "indeterminate", "cancelled", False),
        )
        for label, submission_state, result_status, with_usage in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                    prefix="summon-v2-economics-capacity-") as temporary:
                demo = ConductorDemo(Path(temporary) / "workspaces", run_id=label)
                try:
                    demo.prepare(economics=True)
                    grant = demo.grant("main-1", "worker-v2")[0]
                    demo.register_worker("main-1", "worker-v2", grant)
                    entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                    turn = demo.admit("main-1", "worker-v2", grant, [entry])
                    admission_id = turn["event"]["operation_key"]
                    reservation = turn["event"]["payload"]["economics_reservation"]
                    selection = turn["event"]["payload"]["selection"]
                    claim = demo.state()["claims"][selection["claim_id"]]
                    estimate = _submission_accounting.estimate_payload(
                        [("context", turn["context"].decode("utf-8"))],
                        boundary="workspace-turn")
                    estimate["represented_bytes"] = 16 * 1024 * 1024
                    estimate["estimated_tokens"] = 4 * 1024 * 1024
                    usage = ({name: 4_194_303.9999999995
                              for name in _submission_accounting.METRICS}
                             if with_usage else None)
                    envelope = {
                        "provider_contacted": submission_state != "not_submitted",
                    }
                    if usage is not None:
                        envelope.update({
                            "usage": usage,
                            "usage_observation": {
                                "source": "x" * 80,
                                "scope": "last_step_snapshot",
                            },
                        })
                    accounting = _submission_accounting.private_record(
                        attempt_id=reservation["attempt_id"], attempt_kind="\x01" * 64,
                        attempt_ordinal=selection["attempt"], parent_attempt_id="a" * 32,
                        request_sha256=selection["request_sha256"], estimate=estimate,
                        envelope=envelope, submission_state=submission_state)
                    accounting["reported"]["provenance"] = "\x01" * 80
                    accounting["reported"]["observation_scope"] = "last_step_snapshot"
                    if with_usage:
                        accounting["reported"]["field_state"] = {
                            name: "reported" for name in _submission_accounting.METRICS
                        }
                        accounting["reported"]["completeness"] = "complete"
                    else:
                        accounting["reported"]["field_state"] = {
                            name: "malformed" for name in _submission_accounting.METRICS
                        }
                        accounting["reported"]["completeness"] = "malformed"
                    accounting["contact"]["local_process_created"] = False
                    accounting["contact"]["evidence"] = "launch_boundary_only"
                    coordinator = demo.runtime._coordinator
                    bound = coordinator._economics_settlement_capacity_bound(
                        reservation, claim, selection, admission_id)
                    self.assertLessEqual(bound, reservation["max_settlement_bytes"])
                    before = coordinator._strict_snapshot()[2]
                    settled = demo.runtime.settle_turn_economics(
                        admission_id, accounting_record=accounting,
                        submission_state=submission_state,
                        result_status=result_status)
                    self.assertEqual(settled["status"], "settled")
                    after = coordinator._strict_snapshot()[2]
                    appended = sum(len(after[name]) - len(before.get(name, b""))
                                   for name in after)
                    self.assertGreater(appended, 0)
                    self.assertLessEqual(appended, reservation["max_settlement_bytes"])
                    self.assertLessEqual(appended, bound)
                    duplicate = demo.runtime.settle_turn_economics(
                        admission_id, accounting_record=accounting,
                        submission_state=submission_state,
                        result_status=result_status)
                    self.assertTrue(duplicate["duplicate"])
                    self.assertEqual(after, coordinator._strict_snapshot()[2])
                finally:
                    demo.cleanup()

    def test_v2_economics_rejects_oversized_usage_without_releasing_reservation(self):
        """Invalid usage stays private/uncertain and cannot consume the fence."""
        with tempfile.TemporaryDirectory(prefix="summon-v2-economics-refusal-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces", run_id="oversized-usage")
            try:
                demo.prepare(economics=True)
                grant = demo.grant("main-1", "worker-v2")[0]
                demo.register_worker("main-1", "worker-v2", grant)
                entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-v2", grant, [entry])
                admission_id = turn["event"]["operation_key"]
                reservation = turn["event"]["payload"]["economics_reservation"]
                selection = turn["event"]["payload"]["selection"]
                estimate = _submission_accounting.estimate_payload(
                    [("context", turn["context"].decode("utf-8"))],
                    boundary="workspace-turn")
                accounting = _submission_accounting.private_record(
                    attempt_id=reservation["attempt_id"], attempt_kind="initial",
                    attempt_ordinal=selection["attempt"], parent_attempt_id=None,
                    request_sha256=selection["request_sha256"], estimate=estimate,
                    envelope={"provider_contacted": True}, submission_state="submitted")
                self.assertTrue(accounting["unknown_spend"])
                coordinator = demo.runtime._coordinator
                before = coordinator._strict_snapshot()[2]
                for mutation in (
                        lambda value: value["reported"]["metrics"].update(
                            input_tokens=4_194_304.5),
                        lambda value: value["reported"].update(
                            provenance="\x01" * 81)):
                    invalid = copy.deepcopy(accounting)
                    mutation(invalid)
                    with self.assertRaises(SwarmBudgetRefusal):
                        demo.runtime.settle_turn_economics(
                            admission_id, accounting_record=invalid,
                            submission_state="submitted", result_status="success")
                    after = coordinator._strict_snapshot()[2]
                    self.assertEqual(after, before)
                    state = demo.state()
                    self.assertEqual(
                        state["admissions"][admission_id]["economics_reservation"]["status"],
                        "reserved")
                    self.assertNotIn("economics_settlement", state["admissions"][admission_id])
            finally:
                demo.cleanup()

    def test_v2_economics_rejects_contradictory_metric_field_state(self):
        """Numeric usage cannot be relabeled absent or malformed."""
        with tempfile.TemporaryDirectory(prefix="summon-v2-economics-field-state-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces", run_id="field-state")
            try:
                demo.prepare(economics=True)
                grant = demo.grant("main-1", "worker-v2")[0]
                demo.register_worker("main-1", "worker-v2", grant)
                entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-v2", grant, [entry])
                admission_id = turn["event"]["operation_key"]
                reservation = turn["event"]["payload"]["economics_reservation"]
                selection = turn["event"]["payload"]["selection"]
                estimate = _submission_accounting.estimate_payload(
                    [("context", turn["context"].decode("utf-8"))],
                    boundary="workspace-turn")
                accounting = _submission_accounting.private_record(
                    attempt_id=reservation["attempt_id"], attempt_kind="initial",
                    attempt_ordinal=selection["attempt"], parent_attempt_id=None,
                    request_sha256=selection["request_sha256"], estimate=estimate,
                    envelope={
                        "provider_contacted": True,
                        "usage": {"total_tokens": 4},
                        "usage_observation": {
                            "scope": "attempt_total", "source": "synthetic"},
                    }, submission_state="submitted")
                coordinator = demo.runtime._coordinator
                before = coordinator._strict_snapshot()[2]
                for field_state in ("absent", "malformed"):
                    invalid = copy.deepcopy(accounting)
                    invalid["reported"]["field_state"]["total_tokens"] = field_state
                    with self.assertRaises(SwarmBudgetRefusal):
                        demo.runtime.settle_turn_economics(
                            admission_id, accounting_record=invalid,
                            submission_state="submitted", result_status="success")
                    self.assertEqual(coordinator._strict_snapshot()[2], before)
                    self.assertEqual(
                        demo.state()["admissions"][admission_id]
                        ["economics_reservation"]["status"], "reserved")
            finally:
                demo.cleanup()

    def test_v2_economics_accepts_partial_usage_with_malformed_overall_state(self):
        """A valid reported field remains usable when other fields are malformed."""
        with tempfile.TemporaryDirectory(prefix="summon-v2-economics-partial-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces", run_id="partial-state")
            try:
                demo.prepare(economics=True)
                grant = demo.grant("main-1", "worker-v2")[0]
                demo.register_worker("main-1", "worker-v2", grant)
                entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-v2", grant, [entry])
                admission_id = turn["event"]["operation_key"]
                reservation = turn["event"]["payload"]["economics_reservation"]
                selection = turn["event"]["payload"]["selection"]
                estimate = _submission_accounting.estimate_payload(
                    [("context", turn["context"].decode("utf-8"))],
                    boundary="workspace-turn")
                accounting = _submission_accounting.private_record(
                    attempt_id=reservation["attempt_id"], attempt_kind="initial",
                    attempt_ordinal=selection["attempt"], parent_attempt_id=None,
                    request_sha256=selection["request_sha256"], estimate=estimate,
                    envelope={
                        "provider_contacted": True,
                        "usage": {"total_tokens": 4},
                        "usage_observation": {
                            "scope": "attempt_total", "source": "synthetic"},
                    }, submission_state="submitted")
                accounting["reported"]["field_state"]["input_tokens"] = "malformed"
                accounting["reported"]["completeness"] = "malformed"
                settled = demo.runtime.settle_turn_economics(
                    admission_id, accounting_record=accounting,
                    submission_state="submitted", result_status="success")
                self.assertEqual(settled["status"], "settled")
                summary = _submission_accounting.public_summary([accounting])
                self.assertEqual(
                    summary["reported"]["total_tokens"]["known_subtotal"], 4)
                self.assertIsNone(
                    summary["reported"]["input_tokens"]["known_subtotal"])
            finally:
                demo.cleanup()

    def test_v2_economics_covers_full_turn_settlement_reopen_and_indeterminate_hold(self):
        """Exercise the real provider-free journey, not only its append seam."""
        with tempfile.TemporaryDirectory(prefix="summon-v2-economics-journey-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces", run_id="submitted-run")
            try:
                demo.prepare(economics=True)
                grant = demo.grant("main-1", "worker-v2")[0]
                demo.register_worker("main-1", "worker-v2", grant)
                entry = demo.message("main-1", "worker-v2", grant, 1, "[2,3]")
                turn = demo.admit("main-1", "worker-v2", grant, [entry])
                demo.launch("main-1", "worker-v2", grant,
                            budget_admission=turn["budget_admission"],
                            planned_work=turn["planned_work"])
                demo.start(turn)
                demo.receive(turn)
                settled = demo.settle_economics(turn, submission_state="submitted",
                                                result_status="success")
                self.assertEqual(settled["status"], "settled")
                demo.complete(turn)
                demo.assess("main-1", final=True)
                reopened = demo.reopen()
                self.assertEqual(reopened["status"], "inspected")
                self.assertEqual(demo.state()["tasks"]["main-1"]["terminal"], "completed")
                coordinator_status = demo.runtime._coordinator.status()
                public_economics = coordinator_status["workspace"]["economics"]
                self.assertTrue(public_economics["enabled"])
                self.assertEqual(public_economics["accounting"]["schema"],
                                 "summon.submission-summary/v1")
                from _workspace_view import ViewScope, project_workspace
                public = project_workspace(
                    demo.state()["workspace"], coordinator_status,
                    scope=ViewScope(demo.workspace_id, demo.run_id, b"v" * 32))
                self.assertEqual(public["economics"]["accounting"]["schema"],
                                 "summon.submission-summary/v1")
                public_text = json.dumps(public, sort_keys=True)
                self.assertNotIn("material_sha256", public_text)
                self.assertNotIn("request_sha256", public_text)
                stored = demo.state()["admissions"][turn["event"]["operation_key"]]["economics_settlement"]
                self.assertEqual(stored["submission_state"], "submitted")
                self.assertTrue(stored["accounting"]["unknown_spend"])
            finally:
                demo.cleanup()

            indeterminate = ConductorDemo(Path(temporary) / "workspaces", run_id="indeterminate-run")
            try:
                indeterminate.prepare(economics=True)
                grant = indeterminate.grant("main-1", "worker-v2")[0]
                indeterminate.register_worker("main-1", "worker-v2", grant)
                entry = indeterminate.message("main-1", "worker-v2", grant, 1, "[5,8]")
                turn = indeterminate.admit("main-1", "worker-v2", grant, [entry])
                indeterminate.launch("main-1", "worker-v2", grant,
                                    budget_admission=turn["budget_admission"],
                                    planned_work=turn["planned_work"])
                indeterminate.start(turn)
                settled = indeterminate.settle_economics(
                    turn, submission_state="indeterminate", result_status="timeout")
                self.assertEqual(settled["status"], "settled")
                reopened = indeterminate.reopen()
                self.assertEqual(reopened["status"], "inspected")
                stored = indeterminate.state()["admissions"][turn["event"]["operation_key"]]["economics_settlement"]
                self.assertEqual(stored["submission_state"], "indeterminate")
                self.assertTrue(stored["accounting"]["unknown_spend"])
                with self.assertRaises((ValueError, RuntimeError)):
                    indeterminate.runtime._coordinator.close()
            finally:
                indeterminate.cleanup()

    def test_new_host_same_task_recovery_uses_declared_budget_explicit_confirmation_and_fresh_attempt(self):
        for boundary in ("queued", "included_in_attempt", "submitted"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix="summon-task-process-recovery-") as temporary:
                demo = ConductorDemo(Path(temporary) / "workspaces")
                try:
                    demo.prepare(max_attempts=2)
                    grant = demo.grant("main-1", "worker-b-original")[0]
                    worker = demo.launch("main-1", "worker-b-original", grant)
                    entry = demo.message("main-1", "worker-b-original", grant, 1, "[2,3]")
                    turn = None
                    if boundary != "queued":
                        turn = demo.admit("main-1", "worker-b-original", grant, [entry])
                    if boundary == "submitted":
                        demo.start(turn)
                        frame = worker.receive()
                        self.assertEqual(frame["kind"], "frame_received")
                        source = demo.source({"kind": "actual_precrash_full_frame_receipt", "frame": frame})
                        receipt = demo.evidence(source, "main-1", "precrash-submitted-receipt", "adapter_receipt",
                            entry["delivery_id"], "submitted", "adapter_receipt")
                        demo.advance(entry["delivery_id"], "submitted",
                            {"adapter_receipt": receipt, "submission_boundary": "full_frame_receipt"},
                            {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"})
                    parent = copy.deepcopy(demo.state()["workspace"]["deliveries"][entry["delivery_id"]])
                    binding = copy.deepcopy(worker.channel.binding)
                    demo.cleanup()
                    self.assertTrue(worker.channel.revoked)
                    observation = {"kind": "fixture-observed-task-recovery", "provenance": "parent_test_harness_observation",
                        "qualification": "simulated", "binding": binding, "delivery_id": parent["delivery_id"],
                        "interrupted_state": parent["state"], "certainty": parent["certainty"],
                        "claim_id": turn["response"]["claim_id"] if turn else None,
                        "child_exit_observed": worker.process.poll() is not None, "exit_code": worker.process.returncode,
                        "channel_revoked": worker.channel.revoked,
                        "pipes_closed": all(pipe is None or pipe.closed for pipe in (worker.process.stdin, worker.process.stdout))}
                    proof = demo.evidence(demo.source(observation), "main-1", "persisted-task-harness-observation", "observation",
                        parent["delivery_id"], "held_for_recovery", "hold_observation")
                    before = demo.state()
                    request = {"parent_delivery_id": parent["delivery_id"], "recovery_reference": proof}
                    command = [sys.executable, "-B", str(Path(__file__).with_name("_workspace_demo.py")),
                        "--runs-root", str(demo.root), "--run-id", demo.run_id, "--recover-task-existing"]
                    completed = subprocess.run(command, input=canonical(request), stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, timeout=90, **run_flags())
                    result = json.loads(completed.stdout)
                    self.assertEqual(completed.returncode, 0, repr(result))
                    self.assertEqual(result["qualification"], "simulated_same_task_process_restart")
                    self.assertEqual(result["owned_worker_instances"], 1)
                    self.assertEqual(result["successor_writes"], 1)
                    self.assertEqual(result["attempt"], 1 if boundary == "queued" else 2)
                    self.assertEqual(result["result"], {"count": 2, "sum": 5, "sum_squares": 13})
                    self.assertEqual(result["inherited_uncertainty"], ["cleanup", "spend"] if boundary == "submitted" else [])
                    self.assertIs(result["uncertain_spend"], boundary != "queued")
                    self.assertFalse(result["run_closed"])
                    after = demo.state()
                    self.assertEqual(after["max_attempts"], 2)
                    self.assertEqual(set(after["tasks"]), set(before["tasks"]))
                    self.assertEqual(after["workspace"]["messages"], before["workspace"]["messages"])
                    for key in ("goal", "goal_history", "active_priority", "lanes"):
                        self.assertEqual(after["workspace"][key], before["workspace"][key])
                    task = after["tasks"]["main-1"]
                    self.assertEqual(task["terminal"], "completed")
                    self.assertFalse(task["retry_authorized"])
                    self.assertEqual(len(task["attempts"]), 1 if boundary == "queued" else 2)
                    if turn:
                        self.assertEqual(after["claims"][turn["response"]["claim_id"]]["status"], "indeterminate")
                    disposed = after["workspace"]["deliveries"][parent["delivery_id"]]
                    self.assertEqual(disposed, dict(parent, state="dead_lettered", reason="explicit_fixture_recovery", ack_level=None))
                    child = after["workspace"]["deliveries"]["task-recovery-successor"]
                    self.assertEqual(child["parent_delivery_id"], parent["delivery_id"])
                    self.assertEqual(child["message_id"], parent["message_id"])
                    self.assertEqual(child["selection"]["task_id"], "main-1")
                    self.assertEqual(child["selection"]["selected_message_ids"], [parent["message_id"]])
                    self.assertEqual(child["certainty"], {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"})
                    self.assertEqual(sum(item.get("parent_delivery_id") == parent["delivery_id"]
                                         for item in after["workspace"]["deliveries"].values()), 1)
                    with self.assertRaises((ValueError, RuntimeError)):
                        demo.runtime._coordinator.close()
                    self.assertFalse(self.restart_inspection(demo)["run_closed"])
                    self.assertLessEqual(len(list(Path(demo.runtime._content_store()._root).glob("blob-*"))), 32)
                finally:
                    demo.cleanup()

    def test_two_worker_crash_fork_collision_identity_matrix_has_one_successor(self):
        """Retain one end-to-end provider-free crash/fork/collision gate.

        The main worker crashes after its durable launch intent while a second
        worker has an independently admitted turn that is held by the runtime
        collision boundary.  A fresh host may create exactly one explicitly
        confirmed successor for the crashed task; it must not reattach the old
        worker, duplicate the forked delivery, or consume the held sibling.
        """
        with tempfile.TemporaryDirectory(prefix="summon-two-worker-matrix-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                demo.prepare(max_attempts=2)
                grant_a = demo.grant("main-1", "worker-a-original")[0]
                grant_b = demo.grant("side-1", "worker-b-sibling")[0]
                worker_a = demo.launch("main-1", "worker-a-original", grant_a)
                worker_b = demo.launch("side-1", "worker-b-sibling", grant_b)
                entry_a = demo.message("main-1", "worker-a-original", grant_a, 1, "[2,3]")
                turn_a = demo.admit("main-1", "worker-a-original", grant_a, [entry_a])
                demo.start(turn_a)
                entry_b = demo.message("side-1", "worker-b-sibling", grant_b, 1, "[4,5]",
                                       source_task="main-1")
                turn_b = demo.admit("side-1", "worker-b-sibling", grant_b, [entry_b])
                demo.start(turn_b)

                # Exercise the real runtime transport-observation boundary on
                # the sibling while the main worker is still live.  Replaying
                # the same authenticated observation must be an idempotent
                # readback, never a second child or attempt.
                sibling_before = copy.deepcopy(
                    demo.state()["workspace"]["deliveries"][entry_b["delivery_id"]])
                uncertainty = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
                source = demo.source({"kind": "two-worker-collision-observation",
                                      "delivery_id": entry_b["delivery_id"],
                                      "attempt_id": turn_b["response"]["claim_id"],
                                      "qualification": "simulated-adapter-boundary"})
                observation = demo.evidence(source, "side-1", "two-worker-collision", "observation",
                                            entry_b["delivery_id"], "held_for_recovery", "hold_observation")
                held = copy.deepcopy(sibling_before)
                held.update(state="held_for_recovery", reason="native_turn_collision",
                            certainty=uncertainty)
                collision = demo.event(
                    "workspace_delivery_advanced",
                    {"delivery": held, "evidence": {"hold_observation": observation},
                     "supported_ack_levels": []},
                    "two-worker-collision")
                demo.permit(collision)
                first_collision = demo.runtime.record_transport_observation(
                    collision, kind="native_turn_collision", evidence=observation)
                second_collision = demo.runtime.record_transport_observation(
                    collision, kind="native_turn_collision", evidence=observation)
                self.assertEqual(first_collision["status"], "recorded")
                self.assertEqual(second_collision["status"], "already_recorded")
                self.assertEqual(second_collision["revision"], first_collision["revision"])

                # Kill only the main child after its durable submission intent.
                # The sibling remains an independently owned process until the
                # parent host is torn down, proving the recovery path does not
                # infer or reuse another worker's identity.
                original_binding = copy.deepcopy(worker_a.channel.binding)
                worker_a.process.kill()
                worker_a.process.wait(timeout=5)
                worker_a.close()
                parent = copy.deepcopy(demo.state()["workspace"]["deliveries"][entry_a["delivery_id"]])
                self.assertEqual(parent["state"], "submission_started")
                sibling_binding = copy.deepcopy(worker_b.channel.binding)
                self.assertNotEqual(original_binding["instance_id"], sibling_binding["instance_id"])

                demo.cleanup()
                observation = {"kind": "fixture-observed-task-recovery",
                    "provenance": "parent_test_harness_observation", "qualification": "simulated",
                    "binding": original_binding, "delivery_id": parent["delivery_id"],
                    "interrupted_state": parent["state"], "certainty": parent["certainty"],
                    "claim_id": turn_a["response"]["claim_id"],
                    "child_exit_observed": worker_a.process.poll() is not None,
                    "exit_code": worker_a.process.returncode,
                    "channel_revoked": worker_a.channel.revoked,
                    "pipes_closed": all(pipe is None or pipe.closed
                                         for pipe in (worker_a.process.stdin, worker_a.process.stdout))}
                proof = demo.evidence(demo.source(observation), "main-1",
                                      "two-worker-recovery-observation", "observation",
                                      parent["delivery_id"], "held_for_recovery", "hold_observation")
                before = demo.state()
                request = {"parent_delivery_id": parent["delivery_id"],
                           "recovery_reference": proof}
                command = [sys.executable, "-B", str(Path(__file__).with_name("_workspace_demo.py")),
                           "--runs-root", str(demo.root), "--run-id", demo.run_id,
                           "--recover-task-existing"]
                completed = subprocess.run(command, input=canonical(request), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, timeout=90, **run_flags())
                result = json.loads(completed.stdout)
                self.assertEqual(completed.returncode, 0, repr(result))
                self.assertEqual(result["owned_worker_instances"], 1)
                self.assertEqual(result["successor_writes"], 1)
                self.assertEqual(result["attempt"], 2)
                self.assertFalse(result["run_closed"])

                after = demo.state()
                disposed = after["workspace"]["deliveries"][parent["delivery_id"]]
                self.assertEqual(disposed["state"], "dead_lettered")
                children = [item for item in after["workspace"]["deliveries"].values()
                            if item.get("parent_delivery_id") == parent["delivery_id"]]
                self.assertEqual(len(children), 1)
                child = children[0]
                self.assertEqual(child["recipient"]["instance_id"], "worker-b-replacement")
                self.assertNotEqual(child["recipient"]["instance_id"], original_binding["instance_id"])
                self.assertEqual(child["recipient"]["epoch"], 1)
                self.assertEqual(after["tasks"]["side-1"]["attempts"],
                                 before["tasks"]["side-1"]["attempts"])
                sibling = after["workspace"]["deliveries"][entry_b["delivery_id"]]
                self.assertEqual(sibling["state"], "held_for_recovery")
                self.assertEqual(sibling["reason"], "native_turn_collision")
                self.assertEqual(sum(item.get("parent_delivery_id") == entry_b["delivery_id"]
                                     for item in after["workspace"]["deliveries"].values()), 0)
                self.assertEqual(after["workspace"]["messages"][entry_a["message_id"]],
                                 before["workspace"]["messages"][entry_a["message_id"]])
            finally:
                demo.cleanup()

    def test_new_python_host_recovers_endpoint_from_persisted_harness_source_only(self):
        with tempfile.TemporaryDirectory(prefix="summon-endpoint-process-recovery-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                setup = demo.supervisor_message_setup()
                plan = demo.supervisor_offer_setup(setup)
                old = setup["consumer"]
                binding = old.observe_consumer()["binding"]
                demo.runtime.expose_supervisor_offer(old, plan["token"])
                old.receive_receipt()  # Deliberately not persisted before host loss.
                parent = copy.deepcopy(demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]])
                demo.cleanup()
                observation = {"kind": "fixture-observed-endpoint-recovery", "provenance": "parent_test_harness_observation",
                    "qualification": "simulated", "binding": binding, "delivery_id": parent["delivery_id"],
                    "interrupted_state": parent["state"], "retained_exposure": parent["exposure"],
                    "child_exit_observed": old.process.poll() is not None, "exit_code": old.process.returncode,
                    "channel_revoked": old.channel.revoked,
                    "pipes_closed": all(pipe is None or pipe.closed for pipe in (old.process.stdin, old.process.stdout))}
                reference = demo.evidence(demo.source(observation), None, "persisted-parent-harness-observation", "observation",
                    parent["delivery_id"], "held_for_recovery", "hold_observation", endpoint="supervisor-inbox")
                request = {"parent_delivery_id": parent["delivery_id"], "recovery_reference": reference}
                before = demo.state()
                command = [sys.executable, "-B", str(Path(__file__).with_name("_workspace_demo.py")),
                           "--runs-root", str(demo.root), "--run-id", demo.run_id, "--recover-supervisor-existing"]
                # Only bounded IDs/ref enter stdin: no runtime, handles, secret
                # pipe key, opaque receipt token or in-memory authorization set.
                bad = copy.deepcopy(request)
                bad["recovery_reference"]["sha256"] = "0" * 64
                refused = subprocess.run(command, input=canonical(bad), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, timeout=45, **run_flags())
                self.assertEqual(refused.returncode, 2, "invalid recovery source was not refused")
                refusal = json.loads(refused.stdout)
                self.assertEqual(refusal["owned_consumer_instances"], 0)
                self.assertEqual(demo.state()["workspace"], before["workspace"])
                recovered = subprocess.run(command, input=canonical(request), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, timeout=60, **run_flags())
                self.assertEqual(recovered.returncode, 0, "new fixed recovery host refused")
                outcome = json.loads(recovered.stdout)
                self.assertEqual(outcome, {"status": "success", "qualification": "simulated_endpoint_process_restart",
                    "cleanup_source_provenance": "parent_test_harness_observation", "owned_worker_instances": 0,
                    "owned_consumer_instances": 1, "successor_writes": 1, "parent_unchanged": True,
                    "task_state_unchanged": True, "inherited_exposure": "unknown", "possible_duplicate": True,
                    "run_closed": False})
                after = demo.state()
                held = after["workspace"]["inbox_deliveries"][parent["delivery_id"]]
                expected_parent = dict(parent, state="held_for_recovery", reason="owner_restart")
                self.assertEqual(held, expected_parent)
                child = after["workspace"]["inbox_deliveries"]["supervisor-successor"]
                self.assertEqual(child["prior_offer_id"], plan["offer_id"])
                self.assertEqual(child["recipient"]["epoch"], 2)
                self.assertEqual(child["inherited_exposure"], "unknown")
                for key in ("tasks", "claims", "workers"):
                    self.assertEqual(before[key], after[key])
                for key in ("messages", "goal", "goal_history", "active_priority", "lanes", "deliveries"):
                    self.assertEqual(before["workspace"][key], after["workspace"][key])
                self.assertFalse(self.restart_inspection(demo)["run_closed"])
                self.assertLessEqual(len(list(Path(demo.runtime._content_store()._root).glob("blob-*"))), 32)
            finally:
                demo.cleanup()

    def test_actual_endpoint_restart_links_fresh_consumer_without_erasing_parent_or_replaying_old_offer(self):
        for boundary in ("queued_before_offer", "receipt_observed_before_ack_append"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(prefix="summon-endpoint-recovery-") as temporary:
                demo = ConductorDemo(Path(temporary) / "workspaces")
                try:
                    setup = demo.supervisor_message_setup()
                    old_plan = old_receipt = None
                    if boundary == "receipt_observed_before_ack_append":
                        old_plan = demo.supervisor_offer_setup(setup)
                        self.assertEqual(demo.runtime.expose_supervisor_offer(setup["consumer"], old_plan["token"]).outcome, "full_write")
                        old_receipt = setup["consumer"].receive_receipt()
                        # Authenticated volatile receipt is deliberately NOT
                        # registered or acknowledged before losing the host.
                        self.assertEqual(demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]["state"], "offered")
                    before = demo.state()
                    original_message = copy.deepcopy(before["workspace"]["messages"][
                        before["workspace"]["inbox_deliveries"][setup["delivery_id"]]["message_id"]])
                    successor = demo.recover_supervisor_endpoint(setup)
                    recovered = demo.state()
                    child = recovered["workspace"]["inbox_deliveries"][successor["delivery_id"]]
                    parent = successor["parent"]
                    unknown = boundary == "receipt_observed_before_ack_append"
                    self.assertEqual(parent["state"], "held_for_recovery")
                    self.assertEqual(parent["exposure"], "unknown" if unknown else "not_exposed")
                    self.assertEqual(child["parent_delivery_id"], setup["delivery_id"])
                    self.assertEqual(child["recipient"]["epoch"], 2)
                    self.assertEqual(child["inherited_exposure"], "unknown" if unknown else None)
                    self.assertIs(child["possible_duplicate"], unknown)
                    self.assertEqual(child["prior_offer_id"], old_plan["offer_id"] if unknown else None)
                    self.assertNotEqual(child["grant_ref"], parent["grant_ref"])
                    self.assertEqual(successor["consumer"].channel._send_sequence, 0)
                    self.assertTrue(setup["consumer"].channel.revoked)
                    self.assertIsNotNone(setup["consumer"].process.poll())
                    self.assertTrue(all(worker.channel.revoked for worker in demo.workers.values()))
                    repeated = demo.runtime.supervisor_inbox_command(demo.inbox_commands["link-recovery-successor"],
                        consumer=successor["consumer"], evidence=successor["link_evidence"])
                    self.assertEqual(repeated, successor["link_response"])
                    with self.assertRaises((ValueError, RuntimeError)):
                        demo.inbox_command("link", "refuse-second-recovery-successor", consumer=successor["consumer"],
                            parent_delivery_id=parent["delivery_id"], new_delivery_id="second-successor",
                            evidence=successor["link_evidence"])
                    if old_plan is not None:
                        with self.assertRaises((ValueError, RuntimeError)):
                            demo.runtime.expose_supervisor_offer(setup["consumer"], old_plan["token"])
                        with self.assertRaises((ValueError, RuntimeError)):
                            successor["consumer"].observe_receipt(old_receipt)
                    self.assertEqual(demo.state()["workspace"], recovered["workspace"])
                    new_plan = demo.supervisor_offer_setup(successor, suffix="-successor")
                    self.assertEqual(demo.runtime.expose_supervisor_offer(successor["consumer"], new_plan["token"]).outcome, "full_write")
                    receipt = successor["consumer"].receive_receipt()
                    demo.supervisor_acknowledge(successor, new_plan, receipt, suffix="-successor")
                    after = demo.state()
                    final = after["workspace"]["inbox_deliveries"][successor["delivery_id"]]
                    self.assertEqual((final["state"], final["exposure"]), ("acknowledged", "consumer_received"))
                    self.assertEqual(final["inherited_exposure"], "unknown" if unknown else None)
                    self.assertIs(final["possible_duplicate"], unknown)
                    self.assertEqual(successor["consumer"].channel._send_sequence, 1)
                    self.assertEqual(after["workspace"]["inbox_deliveries"][parent["delivery_id"]], parent)
                    self.assertEqual(after["workspace"]["messages"][original_message["message_id"]], original_message)
                    self.assertEqual(sum(item.get("parent_delivery_id") == parent["delivery_id"]
                                         for item in after["workspace"]["inbox_deliveries"].values()), 1)
                    for key in ("tasks", "claims", "workers"):
                        self.assertEqual(after[key], before[key])
                    for key in ("goal", "goal_history", "active_priority", "lanes", "deliveries"):
                        self.assertEqual(after["workspace"][key], before["workspace"][key])
                    demo.cleanup()
                    self.assertFalse(self.restart_inspection(demo)["run_closed"])
                    self.assertLessEqual(len(list(Path(demo.runtime._content_store()._root).glob("blob-*"))), 32)
                finally:
                    demo.cleanup()

    def test_actual_worker_to_taskless_supervisor_receipt_preserves_execution_authority(self):
        with tempfile.TemporaryDirectory(prefix="summon-supervisor-inbox-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                setup = demo.supervisor_message_setup()
                before = demo.state()
                self.assertEqual(set(before["tasks"]), {"main-1", "main-2", "side-1"})
                self.assertEqual(len(before["workers"]), 1)
                delivery = before["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((delivery["state"], delivery["exposure"]), ("queued", "not_exposed"))
                message = before["workspace"]["messages"][delivery["message_id"]]
                descriptor = message["content"]
                self.assertEqual(demo.runtime._content_store().read(ContentRef(
                    descriptor["ref"], descriptor["sha256"], descriptor["utf8_bytes"])), b"[8]")
                self.assertEqual(message["sender"], {"instance_id": "worker-a-1", "epoch": 1})
                plan = demo.supervisor_offer_setup(setup)
                offered = demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((offered["state"], offered["exposure"]), ("offered", "unknown"))
                write = demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"])
                self.assertEqual(write.outcome, "full_write")
                self.assertEqual(demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]["state"], "offered")
                token = setup["consumer"].receive_receipt()
                result = demo.supervisor_acknowledge(setup, plan, token)
                after = demo.state()
                acknowledged = after["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((acknowledged["state"], acknowledged["exposure"]), ("acknowledged", "consumer_received"))
                self.assertEqual(result["consumer_kind"], "owned_fixed_supervisor_consumer/v1")
                self.assertEqual(result["qualification"], "simulated")
                for key in ("tasks", "claims", "workers"):
                    self.assertEqual(before[key], after[key])
                for key in ("goal", "goal_history", "active_priority", "lanes", "deliveries"):
                    self.assertEqual(before["workspace"][key], after["workspace"][key])
                self.assertEqual(demo.results["main-1"]["verified"]["result"]["sum"], 8)
                for registered in after["workspace"]["evidence"].values():
                    self.assertIsInstance(demo.resolve_source(registered), bytes)
                demo.cleanup()
                restarted = self.restart_inspection(demo)
                self.assertFalse(restarted["run_closed"])
                demo.reopen()
                self.assertEqual(demo.runtime._supervisor_consumers, {})
                self.assertEqual(demo.state()["workspace"]["inbox_deliveries"], after["workspace"]["inbox_deliveries"])
                for registered in demo.state()["workspace"]["evidence"].values():
                    self.assertIsInstance(demo.resolve_source(registered), bytes)
            finally:
                demo.cleanup()

    def test_same_supervisor_offer_token_concurrently_exposes_once_and_keeps_actual_receipt(self):
        with tempfile.TemporaryDirectory(prefix="summon-supervisor-exposure-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            entered, release = threading.Event(), threading.Event()
            thread = None
            try:
                setup = demo.supervisor_message_setup()
                plan = demo.supervisor_offer_setup(setup)
                original_load = demo.runtime._coordinator._load
                writes, outcomes, failures = [], [], []
                original_send = setup["consumer"].send_offer
                def paused_load():
                    state = original_load()
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("fixture_interleaving_expired")
                    return state
                def counted_send(*args, **kwargs):
                    writes.append(True)
                    return original_send(*args, **kwargs)
                def expose():
                    try:
                        outcomes.append(demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"]))
                    except Exception as error:
                        failures.append(type(error).__name__)
                with mock.patch.object(demo.runtime._coordinator, "_load", side_effect=paused_load), \
                        mock.patch.object(setup["consumer"], "send_offer", side_effect=counted_send):
                    thread = threading.Thread(target=expose)
                    thread.start()
                    self.assertTrue(entered.wait(3))
                    with self.assertRaisesRegex(WorkspaceRuntimeError, "consumer_exposure_in_progress"):
                        demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"])
                    release.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(writes, [True])
                self.assertEqual([value.outcome for value in outcomes], ["full_write"])
                token = setup["consumer"].receive_receipt()
                demo.supervisor_acknowledge(setup, plan, token)
                with self.assertRaisesRegex(WorkspaceRuntimeError, "durable_unexposed_local_offer_required"):
                    demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"])
                self.assertEqual(setup["consumer"].channel._send_sequence, 1)
            finally:
                release.set()
                if thread is not None:
                    thread.join(5)
                demo.cleanup()

    def test_durable_supervisor_revocation_before_exposure_refuses_owned_write(self):
        with tempfile.TemporaryDirectory(prefix="summon-supervisor-revocation-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                setup = demo.supervisor_message_setup()
                plan = demo.supervisor_offer_setup(setup)
                proof = demo.evidence(demo.source({"kind": "explicit-host-consumer-revocation",
                    "binding": setup["consumer"].observe_consumer()["binding"]}), None,
                    "supervisor-revocation", "fence", endpoint="supervisor-inbox")
                demo.inbox_command("revoke", "revoke-inbox", evidence={"owner_revocation": proof})
                with mock.patch.object(setup["consumer"], "send_offer", wraps=setup["consumer"].send_offer) as send:
                    with self.assertRaisesRegex(WorkspaceRuntimeError, "current_durable_offer_required"):
                        demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"])
                    send.assert_not_called()
                delivery = demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((delivery["state"], delivery["exposure"]), ("offered", "unknown"))
                self.assertEqual(setup["consumer"].channel._send_sequence, 0)
            finally:
                demo.cleanup()

    def test_expired_actual_supervisor_receipt_refuses_and_same_name_cannot_reattach(self):
        with tempfile.TemporaryDirectory(prefix="summon-supervisor-stale-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                setup = demo.supervisor_message_setup()
                plan = demo.supervisor_offer_setup(setup)
                demo.runtime.expose_supervisor_offer(setup["consumer"], plan["token"])
                token = setup["consumer"].receive_receipt()
                endpoint = demo.state()["workspace"]["supervisor_endpoints"]["supervisor-inbox"]
                with mock.patch.object(demo.runtime._coordinator, "clock", return_value=endpoint["lease_expires_at_ms"] / 1000 + 1):
                    with self.assertRaisesRegex((ValueError, RuntimeError), "consumer lease expired"):
                        demo.supervisor_acknowledge(setup, plan, token)
                delivery = demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((delivery["state"], delivery["exposure"]), ("offered", "unknown"))
                hold_source = demo.source({"kind": "expired-owned-consumer-receipt-observation",
                    "binding": setup["consumer"].observe_consumer()["binding"], "offer_id": plan["offer_id"],
                    "observed_at_ms": endpoint["lease_expires_at_ms"] + 1000, "stale_receipt_refused": True})
                hold_proof = demo.evidence(hold_source, None, "supervisor-expired-receipt-hold", "observation",
                    setup["delivery_id"], "held_for_recovery", "hold_observation", endpoint="supervisor-inbox")
                demo.inbox_command("hold", "hold-stale-inbox-receipt", delivery_id=setup["delivery_id"],
                    reason="consumer_lease_expired", evidence={"hold_observation": hold_proof})
                delivery = demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]]
                self.assertEqual((delivery["state"], delivery["exposure"]), ("held_for_recovery", "unknown"))
                binding = setup["consumer"].observe_consumer()["binding"]
                demo.cleanup()
                demo.reopen()
                with OwnedSupervisorConsumer(binding) as replacement:
                    demo.permit({"operation": "install_supervisor_consumer", "binding": binding})
                    with self.assertRaisesRegex(WorkspaceRuntimeError, "consumer_reattachment_not_supported"):
                        demo.runtime.install_supervisor_consumer(replacement)
                    with self.assertRaises((ValueError, RuntimeError)):
                        replacement.observe_receipt(token)
                self.assertEqual(demo.runtime._supervisor_consumers, {})
                self.assertEqual(demo.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]], delivery)
            finally:
                demo.cleanup()

    def test_supervisor_offer_budget_refuses_rate_stream_and_oversize_before_write(self):
        """The offered inbox item has its own zero-slot budget boundary."""
        with tempfile.TemporaryDirectory(prefix="summon-supervisor-budget-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                setup = demo.supervisor_message_setup()
                plan = demo.supervisor_offer_setup(setup)
                coordinator = demo.runtime._coordinator
                original_snapshot = coordinator.workspace_budget_snapshot
                offered = plan["token"]
                for mode in ("rate", "stream", "oversize"):
                    sent = []

                    def saturated(*args, _mode=mode, **kwargs):
                        snapshot = original_snapshot(*args, **kwargs)
                        item = next(item for item in snapshot["pending_messages"]
                                     if item.get("source") == "inbox")
                        if _mode == "rate":
                            snapshot["rate_events"] = [{"at_ms": snapshot["now_ms"],
                                                         "messages": 32, "bytes": 32}]
                        elif _mode == "stream":
                            snapshot["stream_usage"] = {
                                item["stream_id"]: {"messages": 32, "bytes": 32}}
                        else:
                            # Keep the snapshot structurally valid and tighten
                            # the policy in this branch so the real oversize
                            # rule is exercised rather than schema rejection.
                            item["bytes"] = max(2, item["bytes"])
                        return snapshot

                    policy_override = mock.patch.object(coordinator,
                                                         "consume_supervisor_offer_budget",
                                                         wraps=coordinator.consume_supervisor_offer_budget)
                    if mode == "oversize":
                        original_consume = coordinator.consume_supervisor_offer_budget

                        def tight_consume(**kwargs):
                            policy_value = dict(kwargs["policy"])
                            policy_value["max_message_bytes"] = 1
                            return original_consume(**{**kwargs, "policy": policy_value})

                        policy_override = mock.patch.object(coordinator,
                                                            "consume_supervisor_offer_budget",
                                                            side_effect=tight_consume)
                    with mock.patch.object(coordinator, "workspace_budget_snapshot",
                                           side_effect=saturated), policy_override, \
                            mock.patch.object(setup["consumer"], "send_offer",
                                               side_effect=lambda *a, **k: sent.append(True)):
                        with self.assertRaisesRegex(WorkspaceRuntimeError,
                                                     "supervisor_offer_budget_refused"):
                            demo.runtime.expose_supervisor_offer(setup["consumer"], offered)
                    self.assertEqual(sent, [], mode)
                    self.assertEqual(demo.state().get("supervisor_budget_admissions", {}), {}, mode)
            finally:
                demo.cleanup()

    def test_worker_logical_retry_after_lost_result_uses_new_frame_and_no_fresh_grant_or_append(self):
        with tempfile.TemporaryDirectory(prefix="summon-worker-retry-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            original_clock = None
            try:
                turn, sender, recipient, scope = demo.worker_message_setup(fixed_send_count=2)
                installed = sender._send_handler
                observations, outcomes, committed = [], [], []
                original_clock = demo.runtime._coordinator.clock
                def lose_first_result(worker, token):
                    observations.append(worker.observed_send(token))
                    outcome = installed(worker, token)  # Always invoke the actual authoritative path.
                    outcomes.append(outcome)
                    self.assertEqual(outcome["outcome"], "queued")
                    snapshot = demo.state()["workspace"]
                    if len(outcomes) == 1:
                        committed.append(snapshot)
                        demo.runtime._worker_ingress[id(worker)]["resolve_grant"] = lambda *_: self.fail("duplicate requested fresh grant")
                        demo.runtime._coordinator.clock = lambda: (scope["expires_at_ms"] + 1000) / 1000
                        # Model loss of the host's committed result, not loss of
                        # authenticated wire bytes: a correlated unknown result
                        # permits this fixture's single explicit logical retry.
                        return {"outcome": "indeterminate", "reason": "commit_uncertain", "result": None}
                    self.assertEqual(snapshot, committed[0])
                    demo.runtime._coordinator.clock = original_clock
                    return outcome
                sender._send_handler = lose_first_result
                demo.start(turn)
                demo.receive(turn)
                self.assertEqual(len(outcomes), 2)
                self.assertEqual(outcomes[0], outcomes[1])
                self.assertEqual([item["request_sequence"] for item in observations], [4, 5])
                self.assertNotEqual(observations[0]["request_frame_sha256"], observations[1]["request_frame_sha256"])
                self.assertEqual(observations[0]["request"], observations[1]["request"])
                self.assertEqual(len(demo.state()["workspace"]["send_operations"]), 1)
                self.assertEqual(recipient.channel._send_sequence, 0)
                sender.close()
                before = demo.state()["workspace"]
                refused = demo.runtime._receive_worker_send(sender, observations[1])
                self.assertEqual(refused["outcome"], "refused")
                self.assertEqual(demo.state()["workspace"], before)
            finally:
                if original_clock is not None:
                    demo.runtime._coordinator.clock = original_clock
                demo.cleanup()

    def test_actual_worker_forbidden_route_and_cancelled_source_do_not_publish_or_deliver(self):
        for variant in ("route", "cancelled", "revoked"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(prefix="summon-worker-refusal-") as temporary:
                demo = ConductorDemo(Path(temporary) / "workspaces")
                try:
                    turn, sender, recipient, _scope = demo.worker_message_setup(
                        destination_route="forbidden-route" if variant == "route" else None)
                    demo.start(turn)
                    if variant == "cancelled":
                        demo.runtime._coordinator.cancel("main-1", reason="operator_requested")
                    before_blobs = set(Path(demo.runtime._content_store()._root).glob("blob-*"))
                    installed = sender._send_handler
                    def observe_refusal(worker, token):
                        if variant == "revoked":
                            # Revoke the actual currently observed principal,
                            # not a copied token or a fabricated sender dict.
                            demo.runtime.revoke_worker_ingress(worker)
                        outcome = installed(worker, token)
                        self.assertNotEqual(outcome["outcome"], "queued")
                        self.assertFalse(demo.state()["workspace"]["send_operations"])
                        self.assertEqual(set(Path(demo.runtime._content_store()._root).glob("blob-*")), before_blobs)
                        return outcome
                    sender._send_handler = observe_refusal
                    if variant == "revoked":
                        with self.assertRaises(ValueError):
                            demo.receive(turn)
                        self.assertTrue(sender.channel.revoked)
                    else:
                        demo.receive(turn)
                    self.assertEqual(recipient.channel._send_sequence, 0)
                    self.assertFalse(demo.state()["tasks"]["side-1"]["attempts"])
                    self.assertFalse(demo.state()["workspace"]["send_operations"])
                finally:
                    demo.cleanup()

    def test_actual_worker_authors_context_and_recipient_computes_only_in_separate_authorized_turn(self):
        with tempfile.TemporaryDirectory(prefix="summon-worker-message-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                outcome = demo.worker_message_journey()
                self.assertTrue(outcome["queued_before_recipient_turn"])
                self.assertEqual(outcome["message"]["sender"], {"instance_id": "worker-a-1", "epoch": 1})
                self.assertEqual(demo.results["side-1"]["verified"]["result"], {"count": 1, "sum": 8, "sum_squares": 64})
                self.assertFalse(outcome["response"]["execution_authorized"])
                state = demo.state()
                self.assertEqual(len(state["workspace"]["send_operations"]), 1)
                self.assertEqual(state["tasks"]["main-1"]["terminal"], "completed")
                self.assertEqual(state["tasks"]["side-1"]["terminal"], "completed")
                self.assertEqual(state["tasks"]["main-2"]["attempts"], [])
                self.assertFalse(self.restart_inspection(demo)["run_closed"])
                # The source worker is closed at verified completion before the
                # separately authorized recipient turn is launched.
                self.assertEqual(demo.max_live, 1)
                with self.assertRaisesRegex(ValueError, "unsupported_workspace_event"):
                    demo.runtime.record_event(demo.event("workspace_message_sent", {}, "forged-send"))
            finally:
                demo.cleanup()

    def restart_inspection(self, demo):
        completed = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("_workspace_demo.py")),
            "--runs-root", str(demo.root), "--run-id", demo.run_id, "--inspect-existing"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **run_flags())
        self.assertEqual(completed.returncode, 0, "read-only restart refused")
        output = json.loads(completed.stdout)
        self.assertEqual(output["owned_worker_instances"], 0)
        self.assertEqual(output["evidence_count"], len(demo.state()["workspace"]["evidence"]))
        return output

    def queued_worker(self, demo):
        demo.prepare()
        grant = demo.grant("main-1", "worker-a-1")[0]
        worker = demo.launch("main-1", "worker-a-1", grant)
        entries = [demo.message("main-1", "worker-a-1", grant, 1, "[2,3]")]
        return grant, worker, entries

    def test_public_demo_computes_two_iterations_and_closes_only_after_observed_effect_resolution(self):
        with tempfile.TemporaryDirectory(prefix="summon-conductor-demo-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                demo.run()
                self.assertEqual(demo.iterations, 2)
                self.assertEqual(demo.max_live, 2)
                self.assertEqual(demo.total_workers, 3)
                self.assertTrue(demo.run_closed)
                state = demo.state()
                self.assertTrue(all(task["terminal"] == "completed" for task in state["tasks"].values()))
                first = state["workspace"]["assessments"]["main-1-assessment"]
                self.assertEqual(first["observed_task_status"]["side-1"], "claimed")
                self.assertEqual(state["workspace"]["active_priority"], "main-2")
                self.assertEqual(demo.results["main-2"]["verified"]["result"], {"count": 2, "sum": 15, "sum_squares": 113})
                self.assertTrue(all(item["state"] == "acknowledged" for item in state["workspace"]["deliveries"].values()))
                self.assertTrue(all(item["certainty"] == {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}
                                    for item in state["workspace"]["deliveries"].values()))
                effects = [item for item in state["workspace"]["evidence"].values()
                           if item.get("settlement_for", {}).get("target_state") == "effects_resolved"]
                self.assertEqual(len(effects), 8)
                blobs = list(Path(demo.runtime._content_store()._root).glob("blob-*"))
                self.assertLessEqual(len(blobs), 32)
                before = demo.state()["workspace"]
                restarted = self.restart_inspection(demo)
                self.assertTrue(restarted["run_closed"])
                self.assertEqual(restarted["assessment_count"], 2)
                self.assertEqual(demo.state()["workspace"], before)
            finally:
                demo.cleanup()

    def test_duplicate_admission_is_readback_and_changed_stale_or_wrong_requests_do_not_contact_worker(self):
        with tempfile.TemporaryDirectory(prefix="summon-conductor-refusal-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                grant, worker, entries = self.queued_worker(demo)
                turn = demo.admit("main-1", "worker-a-1", grant, entries)
                before, sequence = demo.state(), worker.channel._send_sequence
                duplicate = demo.runtime.admit_turn("worker-a-1", turn["event"],
                    resolve_grant=lambda *_: self.fail("duplicate requested fresh authority"), lease_ms=300000)
                self.assertEqual(duplicate, turn["response"])
                for mutation in ("changed_duplicate", "stale_revision", "wrong_recipient"):
                    with self.subTest(mutation=mutation):
                        event = copy.deepcopy(turn["event"])
                        if mutation == "changed_duplicate":
                            event["payload"]["selection"]["request_sha256"] = "f" * 64
                        else:
                            event["operation_key"] = mutation
                            if mutation == "wrong_recipient":
                                event["expected_revision"] = before["workspace"]["revision"]
                                event["payload"]["recipient"]["instance_id"] = "other-instance"
                        demo.permit({"operation": "admit_turn", "worker_id": "worker-a-1", "admission_event": event,
                                     "context_format": "summon.workspace.context/v1"})
                        with self.assertRaises((ValueError, RuntimeError)):
                            demo.runtime.admit_turn("worker-a-1", event, resolve_grant=demo.resolve_grant, lease_ms=300000)
                        self.assertEqual(worker.channel._send_sequence, sequence)
                        self.assertEqual(demo.state()["workspace"], before["workspace"])
                        self.assertEqual(demo.state()["claims"], before["claims"])
            finally:
                demo.cleanup()

    def test_relevant_hold_after_decision_refuses_next_claim_but_side_only_hold_allows_main_work(self):
        for affected in ("main-2", "side-1"):
            with self.subTest(affected=affected), tempfile.TemporaryDirectory(prefix="summon-conductor-hold-") as temporary:
                demo = ConductorDemo(Path(temporary) / "workspaces")
                try:
                    side = demo.first_iteration()
                    self.assertEqual(demo.state()["workspace"]["active_priority"], "main-2")
                    demo.hold(affected)
                    if affected == "main-2":
                        with self.assertRaisesRegex(RuntimeError, "trusted grant resolver failed"):
                            demo.next_turn(side)
                        self.assertNotIn("main-2-claim", demo.state()["claims"])
                        # The admission refusal precedes child creation, so no
                        # main-2 channel may exist to carry a send.
                        self.assertNotIn("main-2", demo.workers)
                    else:
                        following = demo.next_turn(side)
                        demo.start(following)
                        demo.receive(following)
                        self.assertEqual(demo.results["main-2"]["verified"]["result"]["sum"], 15)
                    self.assertEqual(len(projection.unresolved_holds(demo.state()["workspace"], affected)), 1)
                    self.assertEqual(demo.state()["tasks"]["main-1"]["terminal"], "completed")
                    self.assertFalse(demo.run_closed)
                    demo.cleanup()  # Revoke old channels before the fresh reader process.
                    partial = demo.state()["workspace"]
                    restarted = self.restart_inspection(demo)
                    self.assertEqual(restarted["assessment_count"], 2)
                    self.assertFalse(restarted["run_closed"])
                    self.assertEqual(demo.state()["workspace"], partial)
                    self.assertTrue(all(worker.channel.revoked for worker in demo.workers.values()))
                finally:
                    demo.cleanup()

    def test_actual_child_exit_after_durable_intent_preserves_uncertainty_across_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="summon-conductor-crash-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                grant, worker, entries = self.queued_worker(demo)
                turn = demo.admit("main-1", "worker-a-1", grant, entries)
                demo.start(turn)
                self.assertEqual(turn["write_outcome"], "full_write")
                # Actual owned child exits before the supervisor consumes any
                # response; even a buffered response is not an observation.
                worker.process.kill()
                worker.process.wait(timeout=5)
                worker.close()
                before = demo.state()["workspace"]
                delivery = before["deliveries"][entries[0]["delivery_id"]]
                self.assertEqual(delivery["state"], "submission_started")
                self.assertEqual(delivery["certainty"], {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
                with self.assertRaises((ValueError, RuntimeError)):
                    demo.complete(turn)
                with self.assertRaises((ValueError, RuntimeError)):
                    worker.observed_effects()
                self.assertFalse(self.restart_inspection(demo)["run_closed"])
                self.assertEqual(demo.state()["workspace"], before)
                with self.assertRaises((ValueError, RuntimeError)):
                    demo.runtime._coordinator.close()
                self.assertTrue(worker.channel.revoked)
            finally:
                demo.cleanup()

    def test_old_recipient_queue_cannot_be_rebound_to_replacement_channel_or_fresh_grant(self):
        with tempfile.TemporaryDirectory(prefix="summon-conductor-recipient-") as temporary:
            demo = ConductorDemo(Path(temporary) / "workspaces")
            try:
                _old_grant, worker, entries = self.queued_worker(demo)
                worker.close()
                self.assertTrue(worker.channel.revoked)
                self.restart_inspection(demo)
                old_delivery = copy.deepcopy(demo.state()["workspace"]["deliveries"][entries[0]["delivery_id"]])
                source = demo.source({"kind": "fixed-demo-grant", "workspace_id": demo.workspace_id, "run_id": demo.run_id,
                    "task_id": "main-1", "goal_revision": 1, "recipient": {"instance_id": "worker-a-replacement", "epoch": 1},
                    "revoked": False, "scope": "fixed-local-computation", "max_attempts": 1})
                grant = demo.evidence(source, "main-1", "replacement-grant", "grant")
                replacement = demo.launch("main-1", "worker-a-replacement", grant)
                with self.assertRaises((ValueError, RuntimeError)):
                    demo.admit("main-1", "worker-a-replacement", grant, entries)
                self.assertEqual(replacement.channel._send_sequence, 0)
                self.assertEqual(demo.state()["workspace"]["deliveries"][entries[0]["delivery_id"]], old_delivery)
                self.assertEqual(old_delivery["state"], "queued")
                self.assertFalse(demo.state()["claims"])
            finally:
                demo.cleanup()


if __name__ == "__main__":
    unittest.main()
