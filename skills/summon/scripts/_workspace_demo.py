"""Scripted, provider-inert conductor demo against real public admission gates.

No journal injection, provider, native attachment, automatic planner or release
qualification. Source evidence stays in the bounded private content store.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from types import SimpleNamespace

from _workspace_content import ContentRef, prepare_content
from _workspace_runtime import WorkspaceRuntime, WorkspaceRuntimeError, compile_turn_context, fixed_effect_sources, goal_plan, verify_fixed_result
from _workspace_transport import OwnedFakeWorker, OwnedSupervisorConsumer, CONTEXT_FIXTURE_OPERATION
import _workspace_protocol as protocol
import _workspace_state as projection
import _submission_accounting
import _context_policy


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


class _RegisteredWorkerView:
    """Registration-only view used before a destination turn is admitted."""
    def __init__(self):
        self.channel = SimpleNamespace(_send_sequence=0, revoked=False)

    def close(self):
        self.channel.revoked = True


class ConductorDemo:
    """Trusted supervisor for this fixed script, never a worker-facing API."""

    def __init__(self, runs_root, run_id="conductor-demo", *, workspace_id="conductor-workspace",
                 objective="Complete two evidence-checked computation iterations while a bounded side lane remains visible."):
        self.root, self.run_id = runs_root, run_id
        self.workspace_id = workspace_id
        self.objective = objective
        self.runtime = None
        self.permitted = set()
        self.workers = {}
        self.consumers = []
        self.inbox_commands = {}
        self.total_workers = self.max_live = self.iterations = 0
        self.requests, self.results = {}, {}
        self.turns = {}
        self._bindings = {}
        self.phase = "prepare"
        self.run_closed = False
        self.economics_enabled = False
    def permit(self, command):
        self.permitted.add(digest(canonical(command)))

    def authorize(self, command, _current):
        # Script-created exact commands only. Worker payloads cannot authorize
        # themselves or change task scope, permissions, route or spend.
        return digest(canonical(command)) in self.permitted

    def resolve_source(self, payload):
        reference = payload["reference"]
        if reference["id"].startswith("admission-"):
            return self.runtime.read_send_admission_evidence(payload)
        match = re.fullmatch(r"(blob-[a-f0-9]{32})(?:\.[a-z0-9_-]+)?", reference["id"])
        if match is None:
            raise WorkspaceRuntimeError("demo_source_reference_invalid")
        store = self.runtime._content_store()
        # Length discovery is not acceptance: ContentStore reopens with all
        # private-root/file checks and verifies exact length/digest afterwards.
        size = os.stat(store._path(match.group(1)), follow_symlinks=False).st_size
        return store.read(ContentRef(match.group(1), reference["sha256"], size))

    def source(self, value):
        raw = value if type(value) is bytes else canonical(value)
        descriptor = prepare_content(raw)
        self.runtime._content_store().put(descriptor, raw, require_namespace_durable=False)
        return {"id": descriptor.reference, "sha256": descriptor.sha256}

    def state(self):
        state, torn = self.runtime._coordinator._load()
        if torn:
            raise WorkspaceRuntimeError("journal_recovery_required")
        return state

    def reopen(self):
        """Read durable state and evidence without reconstructing any worker."""
        self.runtime = WorkspaceRuntime(self.root, self.run_id, self.workspace_id,
            authorize=self.authorize, resolve_evidence=self.resolve_source)
        return self.runtime.inspect()

    def event(self, kind, payload, operation):
        current = self.state()["workspace"]
        version = protocol.PROTOCOL_V2 if self.economics_enabled else protocol.PROTOCOL
        return {"event": kind, "protocol": version, "workspace_id": self.workspace_id,
                "run_id": self.run_id, "operation_key": operation,
                "expected_revision": current["revision"], "payload": payload}

    def record(self, kind, payload, operation, *, resolve_grant=None, fixed_worker=None):
        event = self.event(kind, payload, operation)
        self.permit(event)
        return self.runtime.record_event(event, resolve_grant=resolve_grant, fixed_worker=fixed_worker)

    def evidence(self, source, task, suffix, category, delivery=None, target=None, role=None, *, endpoint=None):
        ref_suffix = suffix if len(source["id"]) + 1 + len(suffix) <= 128 else "proof-" + digest(suffix.encode("utf-8"))[:40]
        reference = {"id": source["id"] + "." + ref_suffix, "sha256": source["sha256"]}
        if (task is None) == (endpoint is None):
            raise WorkspaceRuntimeError("demo_evidence_subject_required")
        payload = {"reference": reference, "category": category,
                   "task_id" if endpoint is None else "endpoint_id": task if endpoint is None else endpoint}
        if delivery is not None:
            payload["delivery_id"] = delivery
        if target is not None:
            payload["settlement_for"] = {"delivery_id": delivery, "target_state": target, "role": role}
        self.record("workspace_evidence_registered", payload, "register-" + suffix)
        return reference

    def prepare(self, *, max_attempts=1, economics=False):
        self.economics_enabled = bool(economics)
        self.runtime = WorkspaceRuntime.new(self.root, self.run_id, self.workspace_id,
            authorize=self.authorize, resolve_evidence=self.resolve_source)
        version = protocol.PROTOCOL_V2 if self.economics_enabled else protocol.PROTOCOL
        base = {"protocol": version, "workspace_id": self.workspace_id, "run_id": self.run_id}
        economics_fence = {
            "schema": "summon.workspace.economics/v1",
            "feature": "submission-accounting-v1", "enabled": True,
            "max_records": 8, "max_settlement_bytes": 8192,
        } if self.economics_enabled else None
        goal = dict(base, kind="goal", goal_id="verify-conductor-loop", revision=1,
            objective=self.objective,
            criteria=[{"criterion_id": "checked-computation", "description": "Child computation matches independent verification",
                       "evidence_requirement": "Verified result source and explicit supervisor assessment"}],
            constraints=["Fixed local integer data only", "No provider or native session", "At most two live owned workers"],
            active_priority="main-1", unresolved_decisions=[])
        lanes = []
        for task in ("main-1", "side-1", "main-2"):
            self.requests[task] = digest(canonical({"task": task, "operation": CONTEXT_FIXTURE_OPERATION, "scope": "fixed-demo"}))
            lanes.append(dict(base, kind="lane", goal_id=goal["goal_id"], goal_revision=1,
                task_id=task, lane_id=task, request_sha256=self.requests[task],
                role="investigation" if task == "side-1" else "main", outcome="Return checked integer aggregates",
                scope="Bounded fixed fake-child computation", authority_ref={"id": "script-scope", "sha256": digest(b"fixed-demo")},
                budget={"max_duration_ms": 300000, "max_attempts": max_attempts, "max_context_bytes": 4096},
                criterion_ids=["checked-computation"], return_condition="Return one independently verified result and stop",
                escalation_trigger="Mismatch, uncertain contact, missing evidence or scope conflict",
                **({"context_policy": _context_policy.make("off", policy_id=task + "-context")}
                   if self.economics_enabled else {})))
        plan = goal_plan(goal, lanes, operation_prefix="prepare", economics=economics_fence)
        self.permit({"operation": "prepare", "plan": plan, **({"max_attempts": max_attempts} if max_attempts != 1 else {})})
        for event in plan["events"]:
            self.permit(event)
        return self.runtime.prepare(plan, project_root_sha256=digest(b"synthetic-project"),
                                    roster_definition_sha256=digest(b"fixed-workers"), max_attempts=max_attempts)

    def grant(self, task, instance, *, suffix=None):
        value = {"kind": "fixed-demo-grant", "workspace_id": self.workspace_id, "run_id": self.run_id,
                 "task_id": task, "goal_revision": 1, "recipient": {"instance_id": instance, "epoch": 1},
                 "revoked": False, "scope": "fixed-local-computation", "max_attempts": 1}
        source = self.source(value)
        reference = self.evidence(source, task, suffix or task + "-grant", "grant")
        return reference, value

    def resolve_grant(self, coordinator, workspace, reference):
        registered = workspace["evidence"].get(reference["id"])
        if registered is None or registered["category"] != "grant":
            raise WorkspaceRuntimeError("demo_grant_missing")
        source = json.loads(self.resolve_source(registered))
        if (source["kind"] != "fixed-demo-grant" or source["workspace_id"] != self.workspace_id
                or source["run_id"] != self.run_id or source["scope"] != "fixed-local-computation"):
            raise WorkspaceRuntimeError("demo_grant_scope")
        return {"grant_ref": reference, **{key: source[key] for key in ("task_id", "goal_revision", "recipient", "revoked")}}

    def register_worker(self, task, instance, grant):
        """Register a durable worker identity without starting its child."""
        binding = {"workspace_id": self.workspace_id, "run_id": self.run_id,
                   "instance_id": instance, "epoch": 1, "task_id": task,
                   "grant_id": grant["id"], "grant_sha256": grant["sha256"]}
        self.runtime._coordinator.register_worker(
            instance, worker_instance_id=instance,
            capabilities=["fixed-computation"], permission_ceiling="read-only")
        self._bindings[task] = binding
        return binding

    def _budget_spec(self, task, entries, compiled, attempt_id=None):
        """Build the pre-claim policy/request and exact planned payload."""
        import _workspace_budget as budget
        workspace = self.state()["workspace"]
        streams = {message["stream_id"] for message in workspace["messages"].values()}
        policy = {
            "schema": budget.POLICY_SCHEMA, "rate_window_ms": 60_000,
            "rate_limit": 32, "rate_bytes_limit": 256 * 1024,
            "max_message_bytes": 4096, "journal_capacity_bytes": 8 * 1024 * 1024,
            "control_reserve_bytes": 4096, "recovery_reserve_bytes": 4096,
            "execution_slot_limit": 3,
            "stream_limits": {stream: {"message_limit": 32, "byte_limit": 256 * 1024}
                              for stream in sorted(streams) or ["main-1-to-main-1"]},
            "oversize_head_action": "refuse",
        }
        request = {
            "schema": budget.REQUEST_SCHEMA,
            "selected_message_ids": [entry["message_id"] for entry in entries],
            "journal_bytes": len(compiled), "execution_slots_requested": 1,
            "kind": "message",
        }
        planned = {"message_id": entries[0]["message_id"],
                   "delivery_id": entries[0]["delivery_id"],
                   "attempt_id": attempt_id or self.state()["tasks"][task]["attempts"][-1],
                   "context": compiled.decode("utf-8"),
                   "context_sha256": digest(compiled),
                   "operation": CONTEXT_FIXTURE_OPERATION}
        return policy, request, planned

    def _budget_gate_from_turn(self, turn, binding):
        """Build a gate from the reservation persisted by atomic admission."""
        admission = turn["response"].get("budget_admission")
        if not isinstance(admission, dict):
            raise WorkspaceRuntimeError("demo_budget_reservation_missing")
        return self.runtime._coordinator.workspace_transport_admission(
            decision=admission["decision"], policy=admission["policy"],
            snapshot=admission["snapshot"], request=admission["request"],
            binding=binding, planned_work=turn["planned_work"],
            turn_admission_id=turn["event"]["operation_key"],
            resolve_grant=self.resolve_grant)

    def launch(self, task, instance, grant, *, fixed_send=None, fixed_send_count=1,
               budget_admission=None, planned_work=None):
        # Separate immutable channel for each task, never task rebinding.
        binding = self._bindings.get(task) or self.register_worker(task, instance, grant)
        worker = OwnedFakeWorker(binding, fixed_send=fixed_send, fixed_send_count=fixed_send_count,
                                 budget_admission=budget_admission, planned_work=planned_work)
        self.workers[task] = worker
        self.total_workers += 1
        live = sum(worker.process.poll() is None for worker in self.workers.values())
        self.max_live = max(self.max_live, live)
        if live > 2:
            raise WorkspaceRuntimeError("demo_worker_bound")
        return worker

    def message(self, task, instance, grant, index, body, source_task="side-1"):
        identifier, delivery_id = task + "-message-" + str(index), task + "-delivery-" + str(index)
        descriptor = prepare_content(body)
        base = {"protocol": (protocol.PROTOCOL_V2 if self.economics_enabled else protocol.PROTOCOL),
                "workspace_id": self.workspace_id, "run_id": self.run_id}
        stream_id = source_task + "-to-" + task
        previous_stream = self.state()["workspace"]["streams"].get(stream_id)
        message = dict(base, kind="message", message_id=identifier, goal_id="verify-conductor-loop",
            source_task_id=source_task, destination_task_id=task, sender={"instance_id": "supervisor-stimulus", "epoch": 1},
            recipient={"instance_id": instance, "epoch": 1}, grant_ref=grant,
            stream_id=stream_id, sequence=1 if previous_stream is None else previous_stream["sequence"] + 1,
            content={"ref": descriptor.reference, "sha256": descriptor.sha256, "utf8_bytes": descriptor.payload_bytes})
        delivery = dict(base, kind="delivery", delivery_id=delivery_id, message_id=identifier, recipient=message["recipient"],
            grant_ref=grant, state="accepted", reason=None, selection=None,
            certainty={"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"},
            inherited_uncertainty=[], ack_level=None)
        event = self.event("workspace_message_admitted", {"message": message, "delivery": delivery}, "send-" + identifier)
        self.permit(event)
        self.runtime.send_context(event, body)
        observed = self.source({"kind": "accepted-message-observation", "message_id": identifier,
                                "delivery_id": delivery_id, "workspace_revision": self.state()["workspace"]["revision"]})
        proof = self.evidence(observed, task, delivery_id + "-queued", "event", delivery_id, "queued", "durable_admission")
        self.advance(delivery_id, "queued", {"durable_admission": proof})
        return {"message_id": identifier, "delivery_id": delivery_id, "content_utf8": body, "content_sha256": descriptor.sha256}

    def advance(self, delivery_id, target, evidence, certainty=None):
        delivery = copy.deepcopy(self.state()["workspace"]["deliveries"][delivery_id])
        delivery.update(state=target, reason=None, ack_level="recipient_received" if target == "acknowledged" else None)
        if certainty is not None:
            delivery["certainty"] = certainty
        return self.record("workspace_delivery_advanced", {"delivery": delivery, "evidence": evidence,
                           "supported_ack_levels": ["recipient_received"]}, delivery_id + "-" + target,
                           resolve_grant=self.resolve_grant if target == "submission_started" else None)

    def admit(self, task, instance, grant, entries, *, attempt=1):
        worker = self.workers.get(task)
        binding = worker.channel.binding if worker is not None else (
            self._bindings.get(task) or self.register_worker(task, instance, grant))
        current_state = self.state()
        generation = max((current_state["claims"][key]["lease_generation"]
                          for key in current_state["tasks"][task]["attempts"]), default=0) + 1
        selection = {"task_id": task, "claim_id": task + "-claim" + ("-" + str(attempt) if attempt != 1 else ""),
                     "attempt": attempt, "owner_generation": generation,
                     "request_sha256": self.requests[task], "context_sha256": "0" * 64,
                     "selected_message_ids": [entry["message_id"] for entry in entries]}
        compiled = compile_turn_context(selection, entries)
        selection["context_sha256"] = digest(compiled)
        preparation = self.source({"kind": "observed-fixed-turn-preparation", "binding": binding,
            "ready": worker._ready if worker is not None else False,
            "owned_child_alive": worker is not None and worker.process.poll() is None,
            "scope": "fixed-local-computation", "selection": selection,
            "selected_delivery_ids": [entry["delivery_id"] for entry in entries],
            "compiled_bytes": len(compiled), "supervisor_attempt_budget": 1,
            "base_admission_required": True})
        maps = {}
        for entry in entries:
            delivery_id = entry["delivery_id"]
            proofs = {"current_grant": grant}
            for role in ("recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation",
                         "selection_record", "whole_message_fit"):
                target = "included_in_attempt" if role in {"selection_record", "whole_message_fit"} else None
                proofs[role] = self.evidence(preparation, task, delivery_id + "-" + role,
                    "event" if target else "fence", delivery_id, target, role if target else None)
            maps[delivery_id] = proofs
        claim = {"task_id": task, "claim_id": selection["claim_id"], "worker_id": instance,
                 "attempt": attempt, "lease_generation": generation, "lease_expires_at_ms": 1,
                 "request_sha256": self.requests[task], "status": "active",
                 "cancel_requested": False, "cancel_acknowledged": False, "renewals": 0}
        current = self.state()["workspace"]
        known_holds = {hold["assessment_id"]: hold for lane in current["lanes"]
                       for hold in projection.unresolved_holds(current, lane)}
        affected = sorted({item for hold in known_holds.values() for item in hold["affected_task_ids"]})
        event = self.event(projection.ATOMIC_TURN_EVENT, {"claim": claim, "selection": selection,
            "recipient": {"instance_id": instance, "epoch": 1}, "grant_ref": grant,
            "evidence_by_delivery": maps, "affected_task_ids": affected, "holds": list(known_holds.values()), "goal_revision": 1,
            "selected_delivery_ids": [entry["delivery_id"] for entry in entries], "supported_ack_levels": ["recipient_received"]},
            task + "-admit" + ("-" + str(attempt) if attempt != 1 else ""))
        if self.economics_enabled:
            event["payload"]["economics_reservation"] = self.runtime._v2_economics_reservation(event, None)
        self.permit({"operation": "admit_turn", "worker_id": instance, "admission_event": event,
                     "context_format": "summon.workspace.context/v1"})
        budget_policy, budget_request, planned = self._budget_spec(
            task, entries, compiled, selection["claim_id"])
        response = self.runtime.admit_turn(
            instance, event, resolve_grant=self.resolve_grant, lease_ms=300000,
            budget_policy=budget_policy, budget_request=budget_request)
        frozen = self.runtime.read_admitted_context(event["operation_key"])
        if frozen["bytes"] != compiled:
            raise WorkspaceRuntimeError("demo_frozen_context_mismatch")
        turn = {"task": task, "instance": instance, "grant": grant, "entries": entries,
                 "event": event, "response": response, "context": compiled,
                 "planned_work": planned}
        turn["budget_admission"] = self._budget_gate_from_turn(turn, binding)
        self.turns[task] = turn
        return turn

    def settle_economics(self, turn, *, submission_state="not_submitted", result_status="error"):
        """Produce and settle the provider-free v2 accounting outcome."""
        if not self.economics_enabled:
            raise WorkspaceRuntimeError("economics_not_enabled")
        estimate = _submission_accounting.estimate_payload(
            [("context", turn["context"].decode("utf-8"))],
            boundary="workspace-turn")
        envelope = {"provider_contacted": submission_state == "submitted"}
        accounting = _submission_accounting.private_record(
            attempt_id=turn["event"]["payload"]["economics_reservation"]["attempt_id"],
            attempt_kind="initial", attempt_ordinal=turn["event"]["payload"]["selection"]["attempt"],
            parent_attempt_id=None,
            request_sha256=turn["event"]["payload"]["selection"]["request_sha256"],
            estimate=estimate, envelope=envelope, submission_state=submission_state)
        return self.runtime.settle_turn_economics(
            turn["event"]["operation_key"], accounting_record=accounting,
            submission_state=submission_state, result_status=result_status)

    def start(self, turn):
        task, worker = turn["task"], self.workers[turn["task"]]
        observation = self.source({"kind": "fixed-submission-intent", "claim_id": turn["response"]["claim_id"],
            "context_sha256": digest(turn["context"]), "binding": worker.channel.binding,
            "owned_child_alive": worker.process.poll() is None, "provider_route": None})
        for entry in turn["entries"]:
            identifier = entry["delivery_id"]
            proofs = {"current_grant": turn["grant"]}
            for role in ("recipient_owner_fence", "capacity_reservation", "physical_attempt_reservation", "durable_launch_intent"):
                proofs[role] = self.evidence(observation, task, identifier + "-start-" + role,
                    "event" if role == "durable_launch_intent" else "fence", identifier, "submission_started", role)
            self.advance(identifier, "submission_started", proofs,
                         {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
        first = turn["entries"][0]
        payload = {"message_id": first["message_id"], "delivery_id": first["delivery_id"],
            "attempt_id": turn["response"]["claim_id"], "context": turn["context"].decode("utf-8"),
            "context_sha256": digest(turn["context"]), "operation": CONTEXT_FIXTURE_OPERATION}
        written, frame_digest = worker.send_work(payload)
        turn.update(request=payload, request_frame_sha256=frame_digest,
                    request_sequence=worker.channel._send_sequence, write_outcome=written.outcome)
        if written.outcome != "full_write":
            raise WorkspaceRuntimeError("demo_contact_uncertain")
        # Full write is deliberately not registered as submitted.

    def receive(self, turn):
        task, worker = turn["task"], self.workers[turn["task"]]
        receipts = []
        for expected_kind in ("frame_received", "acknowledged"):
            frame = worker.receive()
            if frame["kind"] != expected_kind:
                raise WorkspaceRuntimeError("demo_receipt_order")
            receipts.append({"kind": frame["kind"], "binding": frame["binding"], "payload": frame["payload"]})
        result = verify_fixed_result(worker, expected_binding=worker.channel.binding, request=turn["request"],
            request_frame_sha256=turn["request_frame_sha256"], request_sequence=turn["request_sequence"], evidence_id=task + "-result")
        verified = json.loads(result["source_bytes"])
        # All observations already occurred. One immutable multi-fact source
        # supports separately scoped journal registrations within the32-blob
        # bound; none of these receipts or results is prefilled.
        source = self.source({"kind": "observed-fixed-turn-output", "receipts": receipts,
            "verified_result": verified, "selected_delivery_ids": turn["event"]["payload"]["selected_delivery_ids"]})
        for kind, target, role in (("frame_received", "submitted", "adapter_receipt"),
                                   ("acknowledged", "acknowledged", "ack_receipt")):
            for entry in turn["entries"]:
                identifier = entry["delivery_id"]
                reference = self.evidence(source, task, identifier + "-" + role, "adapter_receipt", identifier, target, role)
                proof = {role: reference, **({"submission_boundary": "full_frame_receipt"} if target == "submitted"
                                             else {"ack_level": "recipient_received"})}
                certainty = {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"} if target == "submitted" else None
                self.advance(identifier, target, proof, certainty)
        evidence = self.evidence(source, task, task + "-result", "artifact")
        self.results[task] = {"reference": evidence, "source": source, "verified": verified}
        return evidence

    def complete(self, turn):
        task, worker = turn["task"], self.workers[turn["task"]]
        if task not in self.results:
            raise WorkspaceRuntimeError("demo_unverified_completion")
        worker.close()
        # Verified per-delivery cleanup sources below replace the old generic
        # cleanup blob, preserving facts within the independent32-blob bound.
        for entry in turn["entries"]:
            delivery = self.state()["workspace"]["deliveries"][entry["delivery_id"]]
            observations = fixed_effect_sources(worker, delivery)
            evidence = {}
            for role, observed in zip(("fixed_execution_scope", "owned_child_cleanup"), observations):
                source = self.source(observed)
                evidence[role] = self.evidence(source, task, entry["delivery_id"] + "-" + role,
                    "observation" if role == "fixed_execution_scope" else "fence",
                    entry["delivery_id"], "effects_resolved", role)
            after = copy.deepcopy(delivery)
            after["certainty"] = {"contact": "occurred", "spend": "not_incurred", "cleanup": "complete"}
            self.record("workspace_effects_resolved", {"delivery": after, "resolution_kind": protocol.EFFECT_RESOLUTION_KIND,
                "qualification": "simulated", "evidence": evidence}, entry["delivery_id"] + "-resolve-effects", fixed_worker=worker)
        response = turn["response"]
        self.runtime._coordinator.complete(turn["instance"], task_id=task, claim_id=response["claim_id"],
            attempt=response["attempt"], lease_generation=response["lease_generation"], request_sha256=self.requests[task],
            envelope_sha256=self.results[task]["source"]["sha256"], message_id=task + "-complete")

    def assess(self, task, final=False):
        reference = self.results[task]["reference"]
        assessment = {"protocol": (protocol.PROTOCOL_V2 if self.economics_enabled else protocol.PROTOCOL), "kind": "assessment", "workspace_id": self.workspace_id,
            "run_id": self.run_id, "assessment_id": task + "-assessment", "goal_id": "verify-conductor-loop",
            "goal_revision": 1, "task_id": task, "lane_id": task, "supervisor_id": "scripted-conductor",
            "criterion_results": [{"criterion_id": "checked-computation", "result": "met", "evidence_ids": [reference["id"]]}],
            "evidence": [reference], "limitations": ["Fixed provider-inert simulation; delivery settlement is separate"],
            "handoff": "Finish explicit delivery dispositions before closing the run",
            "disposition": "continue_main", "next_decision": "criteria_satisfied" if final else "continue",
            "reason": "Independent supervisor recomputation matched every selected message"}
        self.record("workspace_assessed", {"assessment": assessment}, task + "-assess")
        self.iterations += 1

    def first_iteration(self):
        """Run useful main work and persist its decision while side work is live."""
        self.prepare()
        self.phase = "iteration-one"
        grants = {task: self.grant(task, instance)[0] for task, instance in (("main-1", "worker-a-1"), ("side-1", "worker-b-1"))}
        self.register_worker("main-1", "worker-a-1", grants["main-1"])
        self.register_worker("side-1", "worker-b-1", grants["side-1"])
        main = self.admit("main-1", "worker-a-1", grants["main-1"],
            [self.message("main-1", "worker-a-1", grants["main-1"], 1, "[3,-2,7]")])
        self.launch("main-1", "worker-a-1", grants["main-1"],
                    budget_admission=main["budget_admission"], planned_work=main["planned_work"])
        side = self.admit("side-1", "worker-b-1", grants["side-1"],
            [self.message("side-1", "worker-b-1", grants["side-1"], 1, "[4,4,-1]", source_task="main-1")])
        self.launch("side-1", "worker-b-1", grants["side-1"],
                    budget_admission=side["budget_admission"], planned_work=side["planned_work"])
        self.start(side)
        self.start(main)
        self.receive(main)
        self.complete(main)
        self.assess("main-1")  # Actual main evidence while side claim is active.
        self.record("workspace_next_lane_selected", {"decision_id": "iteration-two-choice", "goal_revision": 1,
            "assessment_id": "main-1-assessment", "from_task_id": "main-1", "to_task_id": "main-2"}, "choose-main-2")
        self.phase = "iteration-two"
        return side

    def next_turn(self, side):
        """Prepare a fresh, explicitly granted successor; never reopen main-1."""
        self.receive(side)
        grant = self.grant("main-2", "worker-a-2")[0]  # Fresh grant, not implied by the decision.
        # Supervisor mediates verified worker results; no fabricated worker
        # message-authoring capability is attributed to the fixed receipt API.
        main_value = self.results["main-1"]["verified"]["result"]["sum"]
        side_value = self.results["side-1"]["verified"]["result"]["sum"]
        entries = [self.message("main-2", "worker-a-2", grant, 1, json.dumps([main_value]), source_task="main-1"),
                   self.message("main-2", "worker-a-2", grant, 2, json.dumps([side_value]), source_task="side-1")]
        following = self.admit("main-2", "worker-a-2", grant, entries)
        self.launch("main-2", "worker-a-2", grant,
                    budget_admission=following["budget_admission"], planned_work=following["planned_work"])
        return following

    def hold(self, affected_task):
        """Explicit supervisor fixture concern, backed by the observed task state.

        This deliberately introduced issue tests scoped authority; it does not
        imply the fixed child discovered a real defect or issued an instruction.
        """
        observed = self.source({"kind": "supervisor-fixture-concern", "affected_task_id": affected_task,
            "observed_task": self.state()["tasks"][affected_task],
            "reason": "Explicit fixture inspection remains required for this task"})
        reference = self.evidence(observed, "side-1", "fixture-hold-evidence", "observation")
        assessment = {"protocol": (protocol.PROTOCOL_V2 if self.economics_enabled else protocol.PROTOCOL), "kind": "assessment", "workspace_id": self.workspace_id,
            "run_id": self.run_id, "assessment_id": "fixture-hold", "goal_id": "verify-conductor-loop",
            "goal_revision": 1, "task_id": "side-1", "lane_id": "side-1", "supervisor_id": "scripted-conductor",
            "criterion_results": [{"criterion_id": "checked-computation", "result": "unknown", "evidence_ids": [reference["id"]]}],
            "evidence": [reference], "limitations": ["Explicit synthetic concern; no worker-discovered defect"],
            "handoff": "Keep this task held pending an explicit supported disposition",
            "disposition": "hold_affected_lane", "affected_task_ids": [affected_task], "next_decision": "blocked",
            "reason": "Supervisor requires a bounded follow-up inspection of the affected task"}
        return self.record("workspace_assessed", {"assessment": assessment}, "fixture-hold")

    def run(self):
        side = self.first_iteration()
        following = self.next_turn(side)
        self.start(following)
        self.receive(following)
        self.complete(following)
        self.assess("main-2", final=True)
        self.complete(side)
        self.phase = "delivery-settlement"
        # Close only after actual fixed-launcher/cleanup effect resolution.
        self.runtime._coordinator.close()
        self.run_closed = True
        self.phase = "closed"

    def inbox_command(self, action, operation, *, consumer=None, evidence=None, offer_token=None, receipt_token=None, **fields):
        command = {"operation_key": operation, "endpoint_id": "supervisor-inbox",
                   "expected_revision": self.state()["workspace"]["revision"], "action": action, **fields}
        evidence = evidence or {}
        binding = consumer.observe_consumer()["binding"] if consumer is not None else None
        self.permit({"operation": "supervisor_inbox_command", "command": command,
                     "evidence": evidence, "consumer_binding": binding})
        self.inbox_commands[operation] = copy.deepcopy(command)
        return self.runtime.supervisor_inbox_command(command, consumer=consumer, evidence=evidence,
            offer_token=offer_token, receipt_token=receipt_token)

    def supervisor_message_setup(self):
        """Actual A-authored context to a taskless, independently owned consumer."""
        self.prepare()
        self.inbox_command("define", "define-inbox")
        grant_source = {"kind": "supervisor_receiver_grant", "workspace_id": self.workspace_id,
            "run_id": self.run_id, "endpoint_id": "supervisor-inbox", "owner_instance_id": "supervisor-consumer-1",
            "epoch": 1, "lease_expires_at_ms": int(self.runtime._coordinator.clock() * 1000) + 300000,
            "permission": "supervisor_context_receive", "revoked": False}
        receiver_grant = self.evidence(self.source(grant_source), None, "supervisor-receiver-grant", "grant",
                                       endpoint="supervisor-inbox")
        binding = {key: grant_source[key] for key in ("workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch")}
        binding.update(receiver_grant_id=receiver_grant["id"], receiver_grant_sha256=receiver_grant["sha256"])
        consumer = OwnedSupervisorConsumer(binding)
        self.consumers.append(consumer)
        self.permit({"operation": "install_supervisor_consumer", "binding": binding})
        self.runtime.install_supervisor_consumer(consumer)
        owner_fence = self.evidence(self.source({"kind": "installed-owned-consumer-observation",
            **consumer.observe_consumer()}), None, "supervisor-owner-fence", "fence", endpoint="supervisor-inbox")
        self.inbox_command("activate", "activate-inbox", consumer=consumer, receiving_grant_ref=receiver_grant,
                           evidence={"receiver_grant": receiver_grant, "owner_fence": owner_fence})
        source_grant = self.grant("main-1", "worker-a-1")[0]
        scope = {"revision": 1, "expires_at_ms": grant_source["lease_expires_at_ms"], "revoked": False,
            "operation": "message.send", "goal_id": "verify-conductor-loop", "source_task_id": "main-1",
            "sender": {"instance_id": "worker-a-1", "epoch": 1}, "channel_grant_ref": source_grant,
            "recipient": {"kind": "supervisor_inbox", "endpoint_id": "supervisor-inbox",
                          "owner_instance_id": "supervisor-consumer-1", "epoch": 1}, "receiver_grant_ref": receiver_grant}
        scope_ref = self.evidence(self.source(scope), "main-1", "a-to-supervisor-send-scope", "grant")
        route = {"schema": "summon.workspace.message-send/v1", "operation_key": "a-authored-supervisor-sum",
                 "destination_route": scope_ref["id"], "observed_grant_revision": 1}
        self.register_worker("main-1", "worker-a-1", source_grant)
        turn = self.admit("main-1", "worker-a-1", source_grant,
                         [self.message("main-1", "worker-a-1", source_grant, 1, "[3,-2,7]")])
        sender = self.launch("main-1", "worker-a-1", source_grant, fixed_send=route,
                             budget_admission=turn["budget_admission"],
                             planned_work=turn["planned_work"])
        self.permit({"operation": "install_supervisor_ingress", "binding": sender.channel.binding,
                     "claim_id": turn["response"]["claim_id"], "send_scope_ref": scope_ref})
        self.runtime.install_supervisor_ingress(sender, claim_id=turn["response"]["claim_id"],
                                               send_scope_ref=scope_ref, resolve_grant=self.resolve_grant)
        self.start(turn)
        self.receive(turn)
        wire = sender._last_send_result
        if wire is None or wire["response"]["outcome"] != "queued":
            raise WorkspaceRuntimeError("demo_supervisor_message_not_queued")
        response = wire["response"]["result"]
        delivery = self.state()["workspace"]["inbox_deliveries"][response["delivery_id"]]
        return {"turn": turn, "sender": sender, "consumer": consumer, "receiver_grant": receiver_grant,
                "delivery_id": delivery["delivery_id"], "response": response}

    def supervisor_offer_setup(self, setup, *, suffix=""):
        consumer, delivery_id = setup["consumer"], setup["delivery_id"]
        plan = self.runtime.prepare_supervisor_offer(consumer, delivery_id)
        intent = self.evidence(self.source(plan["source_bytes"]), None, "supervisor-offer-intent" + suffix, "event",
                               delivery_id, "offered", "offer_intent", endpoint="supervisor-inbox")
        result = self.inbox_command("offer", "offer-inbox-context" + suffix, consumer=consumer, delivery_id=delivery_id,
            evidence={"receiver_grant": setup["receiver_grant"], "offer_intent": intent}, offer_token=plan["token"])
        return {**plan, "result": result}

    def supervisor_acknowledge(self, setup, plan, receipt_token, *, suffix=""):
        source = self.runtime.supervisor_receipt_source(setup["consumer"], receipt_token)
        receipt = self.evidence(self.source(source), None, "supervisor-consumer-receipt" + suffix, "adapter_receipt",
            setup["delivery_id"], "acknowledged", "consumer_receipt", endpoint="supervisor-inbox")
        return self.inbox_command("receipt", "receive-inbox-context" + suffix, consumer=setup["consumer"],
            delivery_id=setup["delivery_id"], offer_id=plan["offer_id"], evidence={"consumer_receipt": receipt},
            receipt_token=receipt_token)

    def recover_supervisor_endpoint(self, setup):
        """Explicit fixture-host restart and separately granted endpoint successor.

        This preserves logical message/parent and uncertain exposure. It grants
        no task retry, provider spend, model understanding or automatic resend.
        The harness observes actual old-child exit/pipes; names are never used
        to reattach an old consumer in the fresh runtime.
        """
        old = setup["consumer"]
        old_binding = old.observe_consumer()["binding"]
        before = copy.deepcopy(self.state()["workspace"]["inbox_deliveries"][setup["delivery_id"]])
        self.cleanup()
        cleanup = {"kind": "fixture-observed-endpoint-recovery", "provenance": "parent_test_harness_observation",
            "qualification": "simulated", "binding": old_binding,
            "delivery_id": setup["delivery_id"], "interrupted_state": before["state"],
            "retained_exposure": before["exposure"], "child_exit_observed": old.process.poll() is not None,
            "exit_code": old.process.returncode, "channel_revoked": old.channel.revoked,
            "pipes_closed": all(pipe is None or pipe.closed for pipe in (old.process.stdin, old.process.stdout))}
        if not all(cleanup[key] for key in ("child_exit_observed", "channel_revoked", "pipes_closed")):
            raise WorkspaceRuntimeError("demo_old_consumer_cleanup_unknown")
        self.reopen()
        if self.runtime._supervisor_consumers or self.runtime._worker_ingress:
            raise WorkspaceRuntimeError("demo_restart_reconstructed_connection")
        source = self.source(cleanup)
        hold = self.evidence(source, None, "recovery-parent-hold", "observation", setup["delivery_id"],
                             "held_for_recovery", "hold_observation", endpoint="supervisor-inbox")
        return self._recover_supervisor_from_observation(setup["delivery_id"], source, hold)

    def recover_supervisor_from_persisted_observation(self, request):
        """Fixed recovery host reconstructs no prior runtime/handle authority.

        The private source is an explicit trusted parent-test-harness cleanup
        observation, not independently rediscovered process or provider truth.
        This fresh fixture host authorizes each exact recovery command itself.
        """
        if (type(request) is not dict or set(request) != {"parent_delivery_id", "recovery_reference"}
                or self.runtime is not None or self.workers or self.consumers or self.permitted):
            raise WorkspaceRuntimeError("fresh_fixture_recovery_host_required")
        self.reopen()
        state = self.state()["workspace"]
        reference = request["recovery_reference"]
        protocol._reference(reference)
        registered = state["evidence"].get(reference["id"])
        if (registered is None or registered["reference"] != reference or registered["category"] != "observation"
                or registered.get("endpoint_id") != "supervisor-inbox"
                or registered.get("delivery_id") != request["parent_delivery_id"]
                or registered.get("settlement_for") != {"delivery_id": request["parent_delivery_id"],
                    "target_state": "held_for_recovery", "role": "hold_observation"}):
            raise WorkspaceRuntimeError("registered_fixture_recovery_source_required")
        observed = json.loads(self.resolve_source(registered))
        keys = {"kind", "provenance", "qualification", "binding", "delivery_id", "interrupted_state", "retained_exposure",
                "child_exit_observed", "exit_code", "channel_revoked", "pipes_closed"}
        parent = state["inbox_deliveries"][request["parent_delivery_id"]]
        endpoint = state["supervisor_endpoints"]["supervisor-inbox"]
        if (set(observed) != keys or observed["kind"] != "fixture-observed-endpoint-recovery"
                or observed["provenance"] != "parent_test_harness_observation" or observed["qualification"] != "simulated"
                or observed["delivery_id"] != parent["delivery_id"] or observed["interrupted_state"] != parent["state"]
                or observed["retained_exposure"] != parent["exposure"]
                or any(observed[key] is not True for key in ("child_exit_observed", "channel_revoked", "pipes_closed"))
                or type(observed["exit_code"]) is not int
                or observed["binding"] != {"workspace_id": self.workspace_id, "run_id": self.run_id,
                    "endpoint_id": "supervisor-inbox", "owner_instance_id": endpoint["owner"]["instance_id"],
                    "epoch": endpoint["owner"]["epoch"], "receiver_grant_id": endpoint["receiver_grant_ref"]["id"],
                    "receiver_grant_sha256": endpoint["receiver_grant_ref"]["sha256"]}):
            raise WorkspaceRuntimeError("fixture_recovery_observation_mismatch")
        source = {"id": reference["id"].split(".", 1)[0], "sha256": reference["sha256"]}
        return self._recover_supervisor_from_observation(parent["delivery_id"], source, reference)

    def _recover_supervisor_from_observation(self, delivery_id, source, hold):
        before = copy.deepcopy(self.state()["workspace"]["inbox_deliveries"][delivery_id])
        self.inbox_command("hold", "hold-recovery-parent", delivery_id=delivery_id,
            reason="owner_restart", evidence={"hold_observation": hold})
        parent = copy.deepcopy(self.state()["workspace"]["inbox_deliveries"][delivery_id])
        revoke = self.evidence(source, None, "recovery-old-owner-revocation", "fence", endpoint="supervisor-inbox")
        self.inbox_command("revoke", "revoke-recovery-owner", evidence={"owner_revocation": revoke})
        grant_source = {"kind": "supervisor_receiver_grant", "workspace_id": self.workspace_id,
            "run_id": self.run_id, "endpoint_id": "supervisor-inbox", "owner_instance_id": "supervisor-consumer-2",
            "epoch": 2, "lease_expires_at_ms": int(self.runtime._coordinator.clock() * 1000) + 300000,
            "permission": "supervisor_context_receive", "revoked": False}
        grant = self.evidence(self.source(grant_source), None, "recovery-receiver-grant", "grant", endpoint="supervisor-inbox")
        binding = {key: grant_source[key] for key in ("workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch")}
        binding.update(receiver_grant_id=grant["id"], receiver_grant_sha256=grant["sha256"])
        consumer = OwnedSupervisorConsumer(binding)
        self.consumers.append(consumer)
        self.permit({"operation": "install_supervisor_consumer", "binding": binding})
        self.runtime.install_supervisor_consumer(consumer)
        fence = self.evidence(source, None, "recovery-physical-fence", "fence", endpoint="supervisor-inbox")
        owner = self.evidence(self.source({"kind": "fresh-owned-consumer-observation", **consumer.observe_consumer()}),
                              None, "recovery-new-owner-fence", "fence", endpoint="supervisor-inbox")
        self.inbox_command("activate", "activate-recovery-owner", consumer=consumer, receiving_grant_ref=grant,
            evidence={"receiver_grant": grant, "owner_fence": owner, "recovery_fence": fence})
        linked = self.inbox_command("link", "link-recovery-successor", consumer=consumer,
            parent_delivery_id=parent["delivery_id"], new_delivery_id="supervisor-successor",
            evidence={"fresh_receiving_grant": grant, "recovery_fence": fence})
        return {"consumer": consumer, "receiver_grant": grant, "delivery_id": linked["delivery_id"],
                "parent": parent, "parent_before_hold": before, "link_response": linked,
                "link_evidence": {"fresh_receiving_grant": grant, "recovery_fence": fence}}

    def recover_task_from_persisted_observation(self, request):
        """Fixed new-host same-task recovery, never production consent inference.

        This explicitly authorized fixture scenario confirms a local retry using
        the existing human-confirmation contract. The immutable run/each lane
        already permits two attempts; messages or results cannot raise that cap.
        Parent evidence, indeterminate claim history and inherited uncertainty
        remain visible even when the fresh fixed computation verifies.  A
        durable ``submission_started`` parent is also recoverable here: the
        explicit harness observation proves the child exited before a receipt,
        so the new host must first hold/dispose the uncertain parent rather
        than treating the launch intent as a successful submission.
        """
        if (type(request) is not dict or set(request) != {"parent_delivery_id", "recovery_reference"}
                or self.runtime is not None or self.workers or self.consumers or self.permitted):
            raise WorkspaceRuntimeError("fresh_fixture_recovery_host_required")
        self.reopen()
        state = self.state()
        workspace = state["workspace"]
        parent_id = request["parent_delivery_id"]
        parent = copy.deepcopy(workspace["deliveries"][parent_id])
        message = workspace["messages"][parent["message_id"]]
        task = message["destination_task_id"]
        if (task != "main-1" or state["max_attempts"] != 2 or workspace["lanes"][task]["budget"]["max_attempts"] != 2
                or workspace["lanes"][task]["scope"] != "Bounded fixed fake-child computation"
                or "No provider or native session" not in workspace["goal"]["constraints"]
                or parent["state"] not in {"queued", "included_in_attempt", "submission_started", "submitted"}
                or "parent_delivery_id" in parent):
            raise WorkspaceRuntimeError("fixed_task_recovery_scope_required")
        reference = request["recovery_reference"]
        protocol._reference(reference)
        registered = workspace["evidence"].get(reference["id"])
        if (registered is None or registered["reference"] != reference or registered["category"] != "observation"
                or registered.get("task_id") != task or registered.get("delivery_id") != parent_id
                or registered.get("settlement_for") != {"delivery_id": parent_id,
                    "target_state": "held_for_recovery", "role": "hold_observation"}):
            raise WorkspaceRuntimeError("registered_fixture_recovery_source_required")
        observed = json.loads(self.resolve_source(registered))
        fields = {"kind", "provenance", "qualification", "binding", "delivery_id", "interrupted_state", "certainty",
                  "claim_id", "child_exit_observed", "exit_code", "channel_revoked", "pipes_closed"}
        if (set(observed) != fields or observed["kind"] != "fixture-observed-task-recovery"
                or observed["provenance"] != "parent_test_harness_observation" or observed["qualification"] != "simulated"
                or observed["delivery_id"] != parent_id or observed["interrupted_state"] != parent["state"]
                or observed["certainty"] != parent["certainty"]
                or any(observed[key] is not True for key in ("child_exit_observed", "channel_revoked", "pipes_closed"))
                or type(observed["exit_code"]) is not int
                or observed["binding"] != {"workspace_id": self.workspace_id, "run_id": self.run_id, "task_id": task,
                    "instance_id": parent["recipient"]["instance_id"], "epoch": parent["recipient"]["epoch"],
                    "grant_id": parent["grant_ref"]["id"], "grant_sha256": parent["grant_ref"]["sha256"]}
                or observed["claim_id"] != (parent["selection"]["claim_id"] if parent["selection"] else None)):
            raise WorkspaceRuntimeError("fixture_recovery_observation_mismatch")
        self.requests = {name: lane["request_sha256"] for name, lane in workspace["lanes"].items()}
        source = {"id": reference["id"].split(".", 1)[0], "sha256": reference["sha256"]}
        held = copy.deepcopy(parent)
        held.update(state="held_for_recovery", reason="recipient_drift", ack_level=None)
        self.record("workspace_delivery_advanced", {"delivery": held, "evidence": {"hold_observation": reference},
                    "supported_ack_levels": []}, "task-recovery-hold")
        disposition_source = self.source({"kind": "explicit-fixture-host-recovery-decision", "qualification": "simulated",
            "task_id": task, "parent_delivery_id": parent_id, "claim_id": observed["claim_id"],
            "declared_attempt_limit": 2, "fixed_operation": CONTEXT_FIXTURE_OPERATION,
            "decision": "dispose_parent_and_authorize_one_fixed_successor",
            "confirmation_provenance": "explicit_test_scenario_authorization"})
        disposition = self.evidence(disposition_source, task, "task-recovery-disposition", "event", parent_id,
                                    "dead_lettered", "authenticated_disposition")
        disposed = copy.deepcopy(held)
        disposed.update(state="dead_lettered", reason="explicit_fixture_recovery")
        self.record("workspace_delivery_advanced", {"delivery": disposed,
            "evidence": {"authenticated_disposition": disposition}, "supported_ack_levels": []}, "task-recovery-dispose")
        if observed["claim_id"] is not None:
            confirmation = self.evidence(disposition_source, task, "task-recovery-confirmation", "grant")
            command = {"operation": "fixture_host_confirm_retry", "task_id": task,
                       "claim_id": observed["claim_id"], "confirmation": confirmation}
            self.permit(command)
            if self.authorize(command, self.state()["workspace"]) is not True:
                raise WorkspaceRuntimeError("fixture_retry_confirmation_required")
            # Explicit synthetic fixture owner confirmation, never worker/model
            # prose and never a claim of real-provider human authorization.
            self.runtime._coordinator.acknowledge_indeterminate(task, allow_retry=True,
                reason="explicit_fixed_fixture_recovery", human_confirmed=True)
        grant = self.grant(task, "worker-b-replacement", suffix="task-recovery-fresh-grant")[0]
        remaining = self.evidence(disposition_source, task, "task-recovery-remaining-authority", "grant")
        transfer = self.evidence(disposition_source, task, "task-recovery-recipient-transfer", "grant")
        fence = self.evidence(source, task, "task-recovery-physical-fence", "fence")
        child = copy.deepcopy(disposed)
        child.update(delivery_id="task-recovery-successor", parent_delivery_id=parent_id,
            recipient={"instance_id": "worker-b-replacement", "epoch": 1}, grant_ref=grant,
            state="queued", reason=None, selection=None, ack_level=None,
            certainty={"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"},
            inherited_uncertainty=sorted(set(parent["inherited_uncertainty"]) |
                                         {key for key, value in parent["certainty"].items() if value == "unknown"}))
        evidence = {"authenticated_disposition": disposition, "remaining_authority": remaining,
                    "fresh_attempt_grant": grant, "physical_fence": fence, "recipient_transfer_authority": transfer}
        link = self.event("workspace_delivery_linked", {"delivery": child, "evidence": evidence}, "task-recovery-link")
        self.permit(link)
        self.runtime.record_event(link)
        linked = self.state()
        self.runtime.record_event(link)  # Exact duplicate is readback, not a second child.
        if self.state()["workspace"] != linked["workspace"]:
            raise WorkspaceRuntimeError("fixture_duplicate_link_mutated")
        self.register_worker(task, "worker-b-replacement", grant)
        content = message["content"]
        raw = self.runtime._content_store().read(ContentRef(content["ref"], content["sha256"], content["utf8_bytes"]))
        entry = {"message_id": message["message_id"], "delivery_id": child["delivery_id"],
                 "content_utf8": raw.decode("utf-8"), "content_sha256": content["sha256"]}
        attempt = len(self.state()["tasks"][task]["attempts"]) + 1
        turn = self.admit(task, "worker-b-replacement", grant, [entry], attempt=attempt)
        worker = self.launch(task, "worker-b-replacement", grant,
                             budget_admission=turn["budget_admission"],
                             planned_work=turn["planned_work"])
        self.start(turn)
        self.receive(turn)
        worker.close()
        response = turn["response"]
        self.runtime._coordinator.complete("worker-b-replacement", task_id=task, claim_id=response["claim_id"],
            attempt=response["attempt"], lease_generation=response["lease_generation"], request_sha256=self.requests[task],
            envelope_sha256=self.results[task]["source"]["sha256"], message_id="task-recovery-complete")
        after = self.state()
        if (after["workspace"]["deliveries"][parent_id] != disposed
                or after["workspace"]["messages"][message["message_id"]] != message
                or worker.channel._send_sequence != 1):
            raise WorkspaceRuntimeError("fixture_task_recovery_invariant")
        # Linked/history effects are deliberately not resolved by the standalone
        # fixed execution resolution shortcut. Completion is not a clean close.
        return {"task_id": task, "parent": disposed, "child": after["workspace"]["deliveries"][child["delivery_id"]],
                "attempt": attempt, "result": self.results[task]["verified"]["result"],
                "uncertain_spend": after["tasks"][task]["uncertain_spend"], "successor_writes": 1}

    def worker_message_setup(self, *, fixed_send_count=1, destination_route=None):
        """Prepare the fixed A-to-B route and source turn, without task payloads."""
        self.prepare()
        source_grant = self.grant("main-1", "worker-a-1")[0]
        destination_grant = self.grant("side-1", "worker-b-1")[0]
        scope = {"revision": 1, "expires_at_ms": int(self.runtime._coordinator.clock() * 1000) + 300000,
            "revoked": False, "operation": "message.send", "goal_id": "verify-conductor-loop", "source_task_id": "main-1",
            "sender": {"instance_id": "worker-a-1", "epoch": 1}, "channel_grant_ref": source_grant,
            "destination_task_id": "side-1", "recipient": {"instance_id": "worker-b-1", "epoch": 1},
            "delivery_grant_ref": destination_grant}
        scope_ref = self.evidence(self.source(scope), "main-1", "a-to-b-send-scope", "grant")
        route = {"schema": "summon.workspace.message-send/v1", "operation_key": "a-authored-sum",
                 "destination_route": scope_ref["id"] if destination_route is None else destination_route, "observed_grant_revision": 1}
        self.register_worker("main-1", "worker-a-1", source_grant)
        self.register_worker("side-1", "worker-b-1", destination_grant)
        turn = self.admit("main-1", "worker-a-1", source_grant,
            [self.message("main-1", "worker-a-1", source_grant, 1, "[3,-2,7]")])
        sender = self.launch("main-1", "worker-a-1", source_grant, fixed_send=route,
                             fixed_send_count=fixed_send_count,
                             budget_admission=turn["budget_admission"],
                             planned_work=turn["planned_work"])
        recipient = _RegisteredWorkerView()
        self.permit({"operation": "install_worker_ingress", "binding": sender.channel.binding,
                     "claim_id": turn["response"]["claim_id"], "send_scope_ref": scope_ref})
        self.runtime.install_worker_ingress(sender, claim_id=turn["response"]["claim_id"],
                                           send_scope_ref=scope_ref, resolve_grant=self.resolve_grant)
        return turn, sender, recipient, scope

    def worker_message_journey(self):
        """Actual A-authored context, followed by separately authorized B work.

        This bounded journey leaves main-2 pending and does not claim a complete
        goal or a taskless supervisor inbox. Normal conductor coverage is separate.
        """
        turn, sender, recipient, scope = self.worker_message_setup()
        self.start(turn)
        self.receive(turn)  # Same pipe reader admits the child's authored request.
        wire = sender._last_send_result
        if wire is None or wire["response"]["outcome"] != "queued" or wire["write_outcome"] != "full_write":
            raise WorkspaceRuntimeError("demo_worker_message_not_queued")
        response = wire["response"]["result"]
        state = self.state()
        message = state["workspace"]["messages"][response["message_id"]]
        delivery = state["workspace"]["deliveries"][response["delivery_id"]]
        if (message["sender"] != scope["sender"] or message["recipient"] != scope["recipient"]
                or delivery["state"] != "queued" or state["tasks"]["side-1"]["attempts"]
                or recipient.channel._send_sequence != 0):
            raise WorkspaceRuntimeError("demo_message_launched_or_rebound")
        descriptor = message["content"]
        raw = self.runtime._content_store().read(ContentRef(descriptor["ref"], descriptor["sha256"], descriptor["utf8_bytes"]))
        protocol.validate_content(message, raw)
        if raw != canonical([self.results["main-1"]["verified"]["result"]["sum"]]):
            raise WorkspaceRuntimeError("demo_authored_result_mismatch")
        self.complete(turn)
        following = self.admit("side-1", "worker-b-1", scope["delivery_grant_ref"],
            [{"message_id": message["message_id"], "delivery_id": delivery["delivery_id"],
              "content_utf8": raw.decode("utf-8"), "content_sha256": descriptor["sha256"]}])
        recipient = self.launch("side-1", "worker-b-1", scope["delivery_grant_ref"],
                                budget_admission=following["budget_admission"],
                                planned_work=following["planned_work"])
        self.start(following)
        self.receive(following)
        self.complete(following)
        self.phase = "worker-message-verified"
        return {"queued_before_recipient_turn": True, "response": response, "message": message}

    def summary(self, status, reason=None):
        return {"status": status, "phase": self.phase, "reason": reason,
                "qualification": "provider_inert_scripted_demo", "iterations": self.iterations,
                "owned_worker_instances": self.total_workers, "max_live_workers": self.max_live,
                "provider_calls": 0, "run_closed": self.run_closed}

    def cleanup(self):
        for worker in self.workers.values():
            worker.close()
        for consumer in self.consumers:
            consumer.close()


def run_demo(runs_root, run_id="conductor-demo"):
    demo = ConductorDemo(runs_root, run_id)
    try:
        demo.run()
        result = demo.summary("success")
    except (ValueError, RuntimeError, OSError) as exc:
        # Static diagnostic only; never emit exceptions containing source data.
        result = demo.summary("blocked" if not demo.total_workers else "partial",
                              getattr(exc, "kind", type(exc).__name__))
    try:
        demo.cleanup()
    except (ValueError, RuntimeError, OSError):
        result.update(status="partial", reason="cleanup_uncertain")
    return result


def run_task_recovery(runs_root, run_id, request):
    demo = ConductorDemo(runs_root, run_id)
    try:
        recovered = demo.recover_task_from_persisted_observation(request)
        result = {"status": "success", "qualification": "simulated_same_task_process_restart",
            "cleanup_source_provenance": "parent_test_harness_observation",
            "retry_confirmation_provenance": "explicit_test_scenario_authorization",
            "owned_worker_instances": demo.total_workers, "successor_writes": recovered["successor_writes"],
            "attempt": recovered["attempt"], "result": recovered["result"],
            "inherited_uncertainty": recovered["child"]["inherited_uncertainty"],
            "uncertain_spend": recovered["uncertain_spend"], "parent_unchanged_after_disposition": True,
            "run_closed": False}
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        result = {"status": "partial", "reason": "fixture_task_recovery_refused",
                  "error_kind": getattr(error, "kind", type(error).__name__), "owned_worker_instances": demo.total_workers}
    try:
        demo.cleanup()
    except (ValueError, RuntimeError, OSError):
        result.update(status="partial", reason="fixture_worker_cleanup_uncertain")
    return result


def run_endpoint_recovery(runs_root, run_id, request):
    """New fixed fixture host; only persisted context and scoped source cross in."""
    demo = ConductorDemo(runs_root, run_id)
    try:
        successor = demo.recover_supervisor_from_persisted_observation(request)
        before = demo.state()
        plan = demo.supervisor_offer_setup(successor, suffix="-successor")
        written = demo.runtime.expose_supervisor_offer(successor["consumer"], plan["token"])
        if written.outcome != "full_write":
            raise WorkspaceRuntimeError("demo_contact_uncertain")
        token = successor["consumer"].receive_receipt()
        demo.supervisor_acknowledge(successor, plan, token, suffix="-successor")
        after = demo.state()
        child = after["workspace"]["inbox_deliveries"][successor["delivery_id"]]
        if (after["workspace"]["inbox_deliveries"][successor["parent"]["delivery_id"]] != successor["parent"]
                or any(before[key] != after[key] for key in ("tasks", "claims", "workers"))
                or successor["consumer"].channel._send_sequence != 1
                or child["state"] != "acknowledged" or child["exposure"] != "consumer_received"):
            raise WorkspaceRuntimeError("demo_recovery_invariant")
        result = {"status": "success", "qualification": "simulated_endpoint_process_restart",
            "cleanup_source_provenance": "parent_test_harness_observation",
            "owned_worker_instances": 0, "owned_consumer_instances": len(demo.consumers),
            "successor_writes": 1, "parent_unchanged": True, "task_state_unchanged": True,
            "inherited_exposure": child["inherited_exposure"], "possible_duplicate": child["possible_duplicate"],
            "run_closed": False}
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        result = {"status": "partial", "reason": "fixture_endpoint_recovery_refused",
                  "owned_worker_instances": 0, "owned_consumer_instances": len(demo.consumers)}
    try:
        demo.cleanup()
    except (ValueError, RuntimeError, OSError):
        result.update(status="partial", reason="fixture_consumer_cleanup_uncertain")
    return result


def main():
    parser = argparse.ArgumentParser(description="Run the gated, fixed local conductor demo")
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--run-id", default="conductor-demo")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect-existing", action="store_true",
                        help="Read an existing private run without launching or resuming workers")
    mode.add_argument("--recover-supervisor-existing", action="store_true",
                      help="Fixed simulated endpoint recovery using a bounded registered harness observation on stdin")
    mode.add_argument("--recover-task-existing", action="store_true",
                      help="Fixed same-task recovery with a predeclared attempt budget and explicit fixture-host confirmation")
    args = parser.parse_args()
    if args.recover_supervisor_existing or args.recover_task_existing:
        try:
            raw = sys.stdin.buffer.read(4097)
            request = json.loads(raw.decode("utf-8"))
            if not 1 <= len(raw) <= 4096 or canonical(request) != raw:
                raise ValueError("invalid bounded fixture request")
            recover = run_task_recovery if args.recover_task_existing else run_endpoint_recovery
            result = recover(args.runs_root, args.run_id, request)
        except (ValueError, UnicodeError):
            result = {"status": "partial", "reason": "fixture_recovery_request_invalid", "owned_worker_instances": 0}
    elif args.inspect_existing:
        demo = ConductorDemo(args.runs_root, args.run_id)
        try:
            demo.reopen()
            state = demo.state()
            # Re-read registered sources to prove process restart preserves
            # evidence. Output only synthetic counts, never references/bodies.
            for registered in state["workspace"]["evidence"].values():
                demo.resolve_source(registered)
            result = {"status": "success", "qualification": "provider_inert_read_only_restart",
                      "owned_worker_instances": demo.total_workers, "run_closed": state["closed"],
                      "evidence_count": len(state["workspace"]["evidence"]),
                      "assessment_count": len(state["workspace"]["assessments"]),
                      "delivery_count": len(state["workspace"]["deliveries"])}
        except (ValueError, RuntimeError, OSError):
            result = {"status": "partial", "reason": "existing_run_not_readable", "owned_worker_instances": 0}
    else:
        result = run_demo(args.runs_root, args.run_id)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
