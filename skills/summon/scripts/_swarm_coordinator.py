"""Durable, provider-neutral coordinator for ``summon.swarm/v1``.

The wire module deliberately stops at framing.  This module is the next
boundary: it gives a local coordinator an owner lease, a checksummed journal,
claim/attempt fencing, cancellation, and explicit uncertain-spend recovery.
It never launches a provider and never dereferences worker-supplied artifacts.

Every mutating operation acquires the run owner for the shortest possible
critical section.  A worker therefore holds a *claim* lease, not the
coordinator's filesystem lock.  A successor can serialize the next operation,
but it cannot complete, publish, or cancel a claim whose worker, attempt,
request digest, or lease generation does not match the durable record.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from _rundir import (JournalCorruptError, Owner, acquire_owner,
                     encode_journal_record, journal_append_encoded,
                     journal_repair, owner_still_current, release_owner,
                     run_path, validate_run_id)
from _swarm_protocol import (PROTOCOL, SwarmProtocolError, encode_frame,
                             frame_sha256, make_frame, parse_frame)
from _workspace_protocol import (MAX_RECORD_BYTES as _WORKSPACE_MAX_RECORD_BYTES,
                                 MIN_ECONOMICS_SETTLEMENT_BYTES)
import _submission_accounting


MAX_TASKS = 1024
MAX_ATTEMPTS = 32
MAX_LEASE_MS = 24 * 60 * 60 * 1000
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_REASON_CHARS = 512
MAX_STATUS_TASKS = 1024
MAX_STATUS_MESSAGES = 4096
MAX_STATUS_ARTIFACTS = 4096
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_RECORDS = 100_000
MAX_PROJECTION_BYTES = 32 * 1024 * 1024
ROUTINE_RENEWAL_INTERVAL_MS = 30_000
MAX_ROUTINE_DURATION_MS = 60 * 60 * 1000
MAX_ROUTINE_RENEWALS = (MAX_ROUTINE_DURATION_MS + ROUTINE_RENEWAL_INTERVAL_MS - 1) // ROUTINE_RENEWAL_INTERVAL_MS
MAX_GENERATION = 2**63 - 1
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_WORKSPACE_TURN_EVENT = "workspace_turn_admitted"
_WORKSPACE_TURN_BUDGET_CONSUMED = "workspace_turn_budget_consumed"
_WORKSPACE_TURN_ECONOMICS_SETTLED = "workspace_turn_economics_settled"
_ECONOMICS_SETTLEMENT_SCHEMA = "summon.workspace.turn-economics-settlement/v1"
_ACCOUNTING_SCHEMA = "summon.submission-accounting/v1"
_ECONOMICS_SCOPE = "summon_visible_adapter_input"
_ECONOMICS_BOUNDARY = "workspace-turn"
_ECONOMICS_OUTSIDE_SCOPE = (
    "provider_or_cli_added_system_context",
    "provider_wrapper_or_tool_context",
)
_ECONOMICS_MAX_REPRESENTED_BYTES = 16 * 1024 * 1024
_ECONOMICS_MAX_ESTIMATED_TOKENS = 4 * 1024 * 1024
_ECONOMICS_MAX_METRIC = 4 * 1024 * 1024
_ECONOMICS_MAX_METRIC_JSON_CHARS = 18
_ECONOMICS_MAX_TIMESTAMP_JSON_BYTES = 32
_ECONOMICS_MAX_ATTEMPT_KIND_CHARS = 64
_ECONOMICS_MAX_PROVENANCE_CHARS = 80
_ECONOMICS_OBSERVATION_SCOPES = frozenset({
    "unestablished", "attempt_total", "last_step_snapshot",
})
_ECONOMICS_CONTACT_EVIDENCE = frozenset({
    "adapter_report", "launch_boundary_only", "no_contact", "unavailable",
})
_ECONOMICS_BOUND_CONTROL_STRING = "\x01"
_ECONOMICS_BOUND_ATTEMPT_PARENT = "a" * 32
_ECONOMICS_BOUND_METRIC = 4_194_303.9999999995
_SUPERVISOR_OFFER_BUDGET_CONSUMED = "supervisor_offer_budget_consumed"
_WORKSPACE_WRAPPER_EVENT = "workspace_event"
_BUDGET_ADMISSION_SCHEMA = "summon.workspace.turn-budget-admission/v1"
_SUPERVISOR_BUDGET_SCHEMA = "summon.workspace.supervisor-offer-budget/v1"
_WORKSPACE_EVENT_CLASSES = frozenset({
    "workspace_feature", "workspace_goal_defined", "workspace_lane_defined",
    "workspace_evidence_registered", "workspace_assessed", "workspace_next_lane_selected",
    "workspace_message_admitted", "workspace_delivery_advanced", "workspace_delivery_linked",
    "workspace_effects_resolved", "workspace_operator_disposition_recorded",
})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECTION_REBUILD_SCHEMA = "summon.swarm.projection-rebuild/v1"


# Capacity is admitted against the exact frozen journal bytes, not an
# approximation based on a second serialization.  These bounds are generated
# from the same encoder used by the live append path.  The values are
# intentionally conservative and finite: a run can never reserve an
# unbounded recovery/renewal tail merely because it remains open.
_BOUND_ID = "x" * 128
_BOUND_SHA = "f" * 64
# JSON's canonical encoder escapes each control code point as six ASCII bytes.
# U+0001 is accepted by the bounded journal reason contract, so it is the
# correct worst-case shape for byte admission (not a printable 512-byte stub).
_BOUND_REASON = "\x01" * MAX_REASON_CHARS
# The E07 settlement bound adds an explicit 32-byte finite-binary64 timestamp
# allowance over this deterministic journal-bound probe; other event bounds
# continue to use this stable synthetic timestamp unchanged.
_BOUND_TIMESTAMP = 9_999_999_999_999.0
_BOUND_INTEGER = 2**63 - 1
_BOUND_RESPONSE = {
    "status": "s" * 32,
    "run_id": _BOUND_ID,
    "task_id": _BOUND_ID,
    "claim_id": _BOUND_ID,
    "worker_id": _BOUND_ID,
    "previous_claim_id": _BOUND_ID,
    "message_id": _BOUND_ID,
    "artifact_id": _BOUND_ID,
    "to": _BOUND_ID,
    "lease_generation": _BOUND_INTEGER,
    "attempt": MAX_ATTEMPTS,
    "lease_expires_at_ms": _BOUND_INTEGER,
    "uncertain_spend": True,
}


def _bound_record(event: str) -> dict[str, Any]:
    """Build a finite, over-approximating record for ``event``.

    This is deliberately kept next to the coordinator's record constructors;
    changing a constructor's field shape without changing its bound is a
    review-visible capacity bug rather than a silent disk-overrun.
    """
    record: dict[str, Any] = {
        "event": event,
        "generation": MAX_GENERATION,
        "message_id": _BOUND_ID,
        "frame_sha256": _BOUND_SHA,
        "response": dict(_BOUND_RESPONSE),
    }
    if event == "swarm_prepared":
        record.update({
            "schema": 1,
            "run_id": _BOUND_ID,
            "project_root_sha256": _BOUND_SHA,
            "roster_definition_sha256": _BOUND_SHA,
            "max_attempts": MAX_ATTEMPTS,
            "tasks": [{"task_id": _BOUND_ID, "request_sha256": _BOUND_SHA}
                      for _ in range(MAX_TASKS)],
        })
    elif event == "worker_registered":
        record.update({"worker_id": _BOUND_ID, "worker_instance_id": _BOUND_ID,
                       "project_root_sha256": _BOUND_SHA,
                       "roster_definition_sha256": _BOUND_SHA,
                       "permission_ceiling": "safe-edit"})
    elif event in {"claim_granted", "claim_reclaimed"}:
        record.update({"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                       "worker_id": _BOUND_ID, "attempt": MAX_ATTEMPTS,
                       "lease_generation": _BOUND_INTEGER,
                       "lease_expires_at_ms": _BOUND_INTEGER,
                       "request_sha256": _BOUND_SHA})
    elif event == "claim_renewed":
        record.update({"claim_id": _BOUND_ID, "worker_id": _BOUND_ID,
                       "lease_generation": _BOUND_INTEGER,
                       "lease_expires_at_ms": _BOUND_INTEGER,
                       "renewal_number": MAX_ROUTINE_RENEWALS})
    elif event in {"claim_released", "claim_indeterminate", "task_completed",
                   "task_failed", "task_cancelled", "task_blocked"}:
        record.update({"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                       "worker_id": _BOUND_ID, "attempt": MAX_ATTEMPTS,
                       "lease_generation": _BOUND_INTEGER,
                       "request_sha256": _BOUND_SHA,
                       "envelope_sha256": _BOUND_SHA,
                       "reason": _BOUND_REASON, "human_confirmed": True})
    elif event == "retry_authorized":
        record.update({"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                       "reason": _BOUND_REASON, "human_confirmed": True})
    elif event in {"cancel_requested", "cancel_acknowledged"}:
        record.update({"claim_id": _BOUND_ID, "worker_id": _BOUND_ID,
                       "lease_generation": _BOUND_INTEGER,
                       "reason": _BOUND_REASON})
    elif event == "message_posted":
        record.update({"from": _BOUND_ID, "to": _BOUND_ID,
                       "content_sha256": _BOUND_SHA, "content_chars": 12_000,
                       "preview": _BOUND_REASON, "reply_to": _BOUND_ID})
    elif event == "artifact_published":
        record.update({"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                       "attempt": MAX_ATTEMPTS, "lease_generation": _BOUND_INTEGER,
                       "request_sha256": _BOUND_SHA, "artifact_id": _BOUND_ID,
                       "artifact_sha256": _BOUND_SHA,
                       "bytes": MAX_ARTIFACT_BYTES, "media_type": _BOUND_REASON,
                       "relative_path": _BOUND_REASON})
    elif event == "journal_repaired":
        record.update({"repaired_generation": MAX_GENERATION})
    elif event == "run_closed":
        pass
    elif event == _WORKSPACE_TURN_EVENT:
        # The nested workspace envelope is already bounded by the workspace
        # protocol.  This record bound deliberately includes the complete
        # persisted wrapper, not only the coordinator claim fields.
        record.update({
            "admission_id": _BOUND_ID,
            "task_id": _BOUND_ID,
            "claim_id": _BOUND_ID,
            "worker_id": _BOUND_ID,
            "attempt": MAX_ATTEMPTS,
            "lease_generation": _BOUND_INTEGER,
            "lease_expires_at_ms": _BOUND_INTEGER,
            "request_sha256": _BOUND_SHA,
            "workspace_event_sha256": _BOUND_SHA,
            "workspace_event": {
                "event": _WORKSPACE_TURN_EVENT,
                "protocol": "summon.workspace/v1",
                "workspace_id": _BOUND_ID,
                "run_id": _BOUND_ID,
                "operation_key": _BOUND_ID,
                "expected_revision": _BOUND_INTEGER,
                "payload": {
                    "claim": {"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                               "worker_id": _BOUND_ID, "attempt": MAX_ATTEMPTS,
                               "lease_generation": _BOUND_INTEGER,
                               "lease_expires_at_ms": _BOUND_INTEGER,
                               "request_sha256": _BOUND_SHA, "status": "active",
                               "cancel_requested": False, "cancel_acknowledged": False,
                               "renewals": 0},
                    "selection": {"task_id": _BOUND_ID, "claim_id": _BOUND_ID,
                                  "attempt": MAX_ATTEMPTS, "owner_generation": _BOUND_INTEGER,
                                  "request_sha256": _BOUND_SHA, "context_sha256": _BOUND_SHA,
                                  "selected_message_ids": [_BOUND_ID] * 8},
                    "recipient": {"instance_id": _BOUND_ID, "epoch": _BOUND_INTEGER},
                    "grant_ref": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                    "evidence_by_delivery": {str(index).ljust(128, "x"): {
                        "current_grant": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "recipient_owner_fence": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "capacity_reservation": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "physical_attempt_reservation": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "selection_record": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "whole_message_fit": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                        "durable_launch_intent": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                    } for index in range(8)}, "affected_task_ids": [_BOUND_ID] * 16,
                    "holds": [], "goal_revision": _BOUND_INTEGER,
                    "selected_delivery_ids": [_BOUND_ID] * 8,
                    "supported_ack_levels": [],
                    "budget_admission": {
                        "schema": _BUDGET_ADMISSION_SCHEMA, "status": "reserved",
                        "policy": {"schema": "summon.workspace.admission-policy/v1",
                                   "rate_window_ms": _BOUND_INTEGER, "rate_limit": _BOUND_INTEGER,
                                   "rate_bytes_limit": _BOUND_INTEGER, "max_message_bytes": _BOUND_INTEGER,
                                   "journal_capacity_bytes": _BOUND_INTEGER, "control_reserve_bytes": _BOUND_INTEGER,
                                   "recovery_reserve_bytes": _BOUND_INTEGER, "execution_slot_limit": _BOUND_INTEGER,
                                   "stream_limits": {str(index).ljust(128, "x"): {
                                       "message_limit": _BOUND_INTEGER, "byte_limit": _BOUND_INTEGER}
                                       for index in range(32)},
                                   "oversize_head_action": "refuse"},
                        "snapshot": {"schema": "summon.workspace.admission-snapshot/v1",
                                     "now_ms": _BOUND_INTEGER, "journal_used_bytes": _BOUND_INTEGER,
                                     "active_execution_slots": _BOUND_INTEGER, "rate_events": [],
                                     "pending_messages": [{"message_id": _BOUND_ID, "stream_id": _BOUND_ID,
                                                            "sequence": _BOUND_INTEGER, "bytes": _BOUND_INTEGER,
                                                            "state": "queued"} for _ in range(8)],
                                     "stream_usage": {str(index).ljust(128, "x"): {
                                         "messages": _BOUND_INTEGER, "bytes": _BOUND_INTEGER}
                                         for index in range(32)}},
                        "request": {"schema": "summon.workspace.admission-request/v1",
                                    "selected_message_ids": [_BOUND_ID] * 8,
                                    "journal_bytes": _BOUND_INTEGER, "execution_slots_requested": 1,
                                    "kind": "message"},
                        "decision": {"schema": "summon.workspace.admission-decision/v1",
                                     "result": {}, "result_sha256": _BOUND_SHA,
                                     "request_identity_sha256": _BOUND_SHA,
                                     "facts_identity_sha256": _BOUND_SHA,
                                     "journal_revision": _BOUND_INTEGER,
                                     "journal_prefix_sha256": _BOUND_SHA},
                        "request_identity_sha256": _BOUND_SHA, "facts_identity_sha256": _BOUND_SHA,
                        "result_sha256": _BOUND_SHA, "journal_revision": _BOUND_INTEGER,
                        "journal_prefix_sha256": _BOUND_SHA,
                    },
                },
            },
        })
    elif event == _WORKSPACE_TURN_BUDGET_CONSUMED:
        record.update({
            "admission_id": _BOUND_ID, "claim_id": _BOUND_ID,
            "budget_admission_schema": _BUDGET_ADMISSION_SCHEMA,
            "request_identity_sha256": _BOUND_SHA, "facts_identity_sha256": _BOUND_SHA,
            "result_sha256": _BOUND_SHA, "journal_revision": _BOUND_INTEGER,
            "journal_prefix_sha256": _BOUND_SHA, "binding_sha256": _BOUND_SHA,
            "planned_work_sha256": _BOUND_SHA,
        })
    elif event == _WORKSPACE_TURN_ECONOMICS_SETTLED:
        record.update({
            "admission_id": _BOUND_ID, "claim_id": _BOUND_ID,
            "result_sha256": _BOUND_SHA,
            "submission_state": "indeterminate", "result_status": "timeout",
        })
    elif event == _SUPERVISOR_OFFER_BUDGET_CONSUMED:
        record.update({
            "admission_id": _BOUND_ID, "delivery_id": _BOUND_ID,
            "offer_id": _BOUND_ID, "offer_payload_sha256": _BOUND_SHA,
            "binding_sha256": _BOUND_SHA,
            "budget_admission": {
                "schema": _SUPERVISOR_BUDGET_SCHEMA, "status": "consumed",
                "policy": {"schema": "summon.workspace.admission-policy/v1",
                           "rate_window_ms": _BOUND_INTEGER, "rate_limit": _BOUND_INTEGER,
                           "rate_bytes_limit": _BOUND_INTEGER, "max_message_bytes": _BOUND_INTEGER,
                           "journal_capacity_bytes": _BOUND_INTEGER, "control_reserve_bytes": _BOUND_INTEGER,
                           "recovery_reserve_bytes": _BOUND_INTEGER, "execution_slot_limit": _BOUND_INTEGER,
                           "stream_limits": {str(index).ljust(128, "x"): {
                               "message_limit": _BOUND_INTEGER, "byte_limit": _BOUND_INTEGER}
                               for index in range(32)}, "oversize_head_action": "refuse"},
                "snapshot": {"schema": "summon.workspace.admission-snapshot/v1",
                             "now_ms": _BOUND_INTEGER, "journal_used_bytes": _BOUND_INTEGER,
                             "active_execution_slots": _BOUND_INTEGER, "rate_events": [],
                             "pending_messages": [{"message_id": _BOUND_ID, "stream_id": _BOUND_ID,
                                                   "sequence": 1, "bytes": _BOUND_INTEGER,
                                                   "state": "offered", "source": "inbox"}], "stream_usage": {}},
                "request": {"schema": "summon.workspace.admission-request/v1",
                            "selected_message_ids": [_BOUND_ID], "journal_bytes": _BOUND_INTEGER,
                            "execution_slots_requested": 0, "kind": "message"},
                "decision": {"schema": "summon.workspace.admission-decision/v1",
                             "result": {}, "result_sha256": _BOUND_SHA,
                             "request_identity_sha256": _BOUND_SHA,
                             "facts_identity_sha256": _BOUND_SHA,
                             "journal_revision": _BOUND_INTEGER,
                             "journal_prefix_sha256": _BOUND_SHA},
                "request_identity_sha256": _BOUND_SHA,
                "facts_identity_sha256": _BOUND_SHA,
                "result_sha256": _BOUND_SHA,
                "journal_revision": _BOUND_INTEGER,
                "journal_prefix_sha256": _BOUND_SHA,
            },
        })
    elif event == _WORKSPACE_WRAPPER_EVENT:
        # Ordinary workspace events are stored inside the coordinator journal
        # by the runtime facade.  The nested protocol bounds the event body;
        # the extra wrapper/generation/checksum overhead is covered by the
        # explicit finite wrapper bound below.
        record.update({
            "workspace_event": {
                "event": "workspace_delivery_advanced",
                "protocol": "summon.workspace/v1",
                "workspace_id": _BOUND_ID,
                "run_id": _BOUND_ID,
                "operation_key": _BOUND_ID,
                "expected_revision": _BOUND_INTEGER,
                "payload": {"delivery": {}, "evidence": {},
                             "supported_ack_levels": []},
            },
            "observed_at_ms": _BOUND_INTEGER,
        })
    else:
        raise ValueError(f"unknown bounded journal event {event!r}")
    return record


_EVENT_BOUND_BYTES = {
    event: len(encode_journal_record(_bound_record(event), timestamp=_BOUND_TIMESTAMP))
    for event in (
        "swarm_prepared", "worker_registered", "claim_granted", "claim_reclaimed",
        "claim_renewed", "claim_released", "claim_indeterminate", "task_completed",
        "task_failed", "task_cancelled", "task_blocked", "retry_authorized",
        "cancel_requested", "cancel_acknowledged", "message_posted",
        "artifact_published", "journal_repaired", "run_closed",
        _WORKSPACE_TURN_EVENT, _WORKSPACE_TURN_BUDGET_CONSUMED,
        _WORKSPACE_TURN_ECONOMICS_SETTLED,
        _SUPERVISOR_OFFER_BUDGET_CONSUMED, _WORKSPACE_WRAPPER_EVENT)
}

_BOUND_CLAIM_RENEWED = _EVENT_BOUND_BYTES["claim_renewed"]
_BOUND_CANCEL_REQUESTED = _EVENT_BOUND_BYTES["cancel_requested"]
_BOUND_CANCEL_ACKNOWLEDGED = _EVENT_BOUND_BYTES["cancel_acknowledged"]
_BOUND_TASK_CANCELLED = _EVENT_BOUND_BYTES["task_cancelled"]
_BOUND_CLAIM_INDETERMINATE = _EVENT_BOUND_BYTES["claim_indeterminate"]
_BOUND_RETRY_AUTHORIZED = _EVENT_BOUND_BYTES["retry_authorized"]
_BOUND_TASK_BLOCKED = _EVENT_BOUND_BYTES["task_blocked"]
_BOUND_TASK_COMPLETED = _EVENT_BOUND_BYTES["task_completed"]
_BOUND_TASK_FAILED = _EVENT_BOUND_BYTES["task_failed"]
_BOUND_CLAIM_RELEASED = _EVENT_BOUND_BYTES["claim_released"]
_RECOVERY_HEADROOM = _BOUND_CLAIM_INDETERMINATE + max(
    _BOUND_RETRY_AUTHORIZED, _BOUND_TASK_BLOCKED)
_RUN_CLOSE_BOUND = _EVENT_BOUND_BYTES["run_closed"]
# ``MAX_RECORD_BYTES`` bounds the canonical nested workspace envelope.  A
# wrapper adds only fixed metadata, generation, timestamp, and checksum; keep
# a generous finite margin so every ordinary wrapper append remains covered.
_BOUND_WORKSPACE_WRAPPER = _WORKSPACE_MAX_RECORD_BYTES + 8 * 1024
_EVENT_BOUND_BYTES[_WORKSPACE_WRAPPER_EVENT] = _BOUND_WORKSPACE_WRAPPER
_EVENT_BOUND_BYTES[_WORKSPACE_TURN_EVENT] = _BOUND_WORKSPACE_WRAPPER
_EVENT_BOUND_BYTES[_WORKSPACE_TURN_BUDGET_CONSUMED] = len(
    encode_journal_record(_bound_record(_WORKSPACE_TURN_BUDGET_CONSUMED), timestamp=_BOUND_TIMESTAMP))
_EVENT_BOUND_BYTES[_SUPERVISOR_OFFER_BUDGET_CONSUMED] = len(
    encode_journal_record(_bound_record(_SUPERVISOR_OFFER_BUDGET_CONSUMED), timestamp=_BOUND_TIMESTAMP))


def _workspace_evidence_record_bound() -> int:
    """Maximum encoded full wrapper across strict task/inbox/control slot shapes."""
    normal = {"reference": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
              "category": "adapter_receipt", "task_id": _BOUND_ID, "delivery_id": _BOUND_ID,
              "settlement_for": {"delivery_id": _BOUND_ID, "target_state": "included_in_attempt",
                                 "role": "physical_attempt_reservation"}}
    inbox = {"reference": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
             "category": "adapter_receipt", "endpoint_id": _BOUND_ID, "delivery_id": _BOUND_ID,
             "settlement_for": {"delivery_id": _BOUND_ID, "target_state": "held_for_recovery",
                                "role": "authorized_cancellation"}}
    endpoint = {"reference": {"id": _BOUND_ID, "sha256": _BOUND_SHA},
                "category": "fence", "endpoint_id": _BOUND_ID,
                "settlement_for": {"endpoint_id": _BOUND_ID, "epoch": _BOUND_INTEGER,
                                   "target_state": "retired", "role": "owner_revocation"}}
    # Enum lengths use their maximum independently: this safely covers every
    # supported role/category combination, while subject/slot shapes remain exact.
    return max(len(encode_journal_record({
        "event": _WORKSPACE_WRAPPER_EVENT, "generation": MAX_GENERATION,
        "observed_at_ms": _BOUND_INTEGER,
        "workspace_event": {"event": "workspace_evidence_registered", "protocol": "summon.workspace/v1",
                            "workspace_id": _BOUND_ID, "run_id": _BOUND_ID, "operation_key": _BOUND_ID,
                            "expected_revision": _BOUND_INTEGER, "payload": payload},
    }, timestamp=_BOUND_TIMESTAMP)) for payload in (normal, inbox, endpoint))


_BOUND_WORKSPACE_EVIDENCE = _workspace_evidence_record_bound()


class SwarmCoordinatorError(RuntimeError):
    """Base class for coordinator admission/state errors."""


class SwarmNotFoundError(SwarmCoordinatorError):
    pass


class SwarmCorruptError(SwarmCoordinatorError):
    pass


class SwarmConflictError(SwarmCoordinatorError):
    pass


class SwarmIndeterminateError(SwarmConflictError):
    """A previous physical attempt may have spent and needs human policy."""


class SwarmBudgetRefusal(SwarmCoordinatorError):
    """A detached workspace-budget preflight refused or lacked its facts.

    This is intentionally separate from journal/capacity errors.  The budget
    evaluator is a provider-free preflight seam: it never mutates the
    coordinator and its result is not a durable execution authorization.
    """

    def __init__(self, reason: str, *, result: Mapping[str, Any] | None = None):
        self.reason = reason
        self.result = copy.deepcopy(dict(result)) if isinstance(result, Mapping) else None
        super().__init__(f"workspace budget refused ({reason})")


def _validate_budget_admission(value: Mapping[str, Any], *, selected_message_ids: list[str],
                               journal_revision: int, journal_prefix_sha256: str | None,
                               status: str = "reserved") -> dict[str, Any]:
    """Validate a durable, pre-claim budget reservation.

    The complete fact set is journaled with the atomic turn. Replay validates
    the original decision instead of recomputing against the post-claim
    ``included_in_attempt`` projection.
    """
    import _workspace_budget as budget

    required = {"schema", "status", "policy", "snapshot", "request", "decision",
                "request_identity_sha256", "facts_identity_sha256", "result_sha256",
                "journal_revision", "journal_prefix_sha256"}
    if type(value) is not dict or set(value) != required:
        raise SwarmCorruptError("invalid durable budget admission")
    if value["schema"] != _BUDGET_ADMISSION_SCHEMA or value["status"] != status:
        raise SwarmCorruptError("invalid durable budget admission status")
    try:
        policy = budget.validate_policy(value["policy"])
        snapshot = budget.validate_snapshot(value["snapshot"])
        request = budget.validate_request(value["request"])
        if request["kind"] != "message" or request["execution_slots_requested"] != 1:
            raise budget.WorkspaceBudgetError("turn budget request is not one message slot")
        if request["selected_message_ids"] != list(selected_message_ids):
            raise budget.WorkspaceBudgetError("turn budget selection differs")
        decision = copy.deepcopy(value["decision"])
        if value["journal_revision"] != journal_revision:
            raise budget.WorkspaceBudgetError("turn budget revision differs")
        stored_prefix = value["journal_prefix_sha256"]
        budget._validate_binding_digest(stored_prefix, "turn budget prefix digest")
        if journal_prefix_sha256 is not None and stored_prefix != journal_prefix_sha256:
            raise budget.WorkspaceBudgetError("turn budget prefix differs")
        prefix_for_binding = stored_prefix if journal_prefix_sha256 is None else journal_prefix_sha256
        consumed = budget.consume_bound_decision(
            decision, policy=policy, snapshot=snapshot, request=request,
            journal_revision=journal_revision,
            journal_prefix_sha256=prefix_for_binding)
        if consumed["status"] != "accepted" or consumed["execution_status"] != "available":
            raise budget.WorkspaceBudgetError("turn budget is not executable")
        if value["request_identity_sha256"] != budget.request_identity_sha256(request):
            raise budget.WorkspaceBudgetError("turn budget request digest differs")
        if value["facts_identity_sha256"] != budget.facts_identity_sha256(policy, snapshot, request):
            raise budget.WorkspaceBudgetError("turn budget facts digest differs")
        if value["result_sha256"] != budget.result_sha256(consumed):
            raise budget.WorkspaceBudgetError("turn budget result digest differs")
        return {
            "schema": _BUDGET_ADMISSION_SCHEMA, "status": status,
            "policy": policy, "snapshot": snapshot, "request": request,
            "decision": decision,
            "request_identity_sha256": value["request_identity_sha256"],
            "facts_identity_sha256": value["facts_identity_sha256"],
            "result_sha256": value["result_sha256"],
            "journal_revision": journal_revision,
            "journal_prefix_sha256": stored_prefix,
        }
    except (budget.WorkspaceBudgetError, KeyError, TypeError, ValueError):
        raise SwarmCorruptError("invalid durable budget admission") from None


def _validate_supervisor_budget_admission(value: Mapping[str, Any], *,
                                          journal_revision: int,
                                          journal_prefix_sha256: str | None,
                                          selected_message_id: str,
                                          status: str = "consumed") -> dict[str, Any]:
    """Validate the non-execution budget consumed by one supervisor offer.

    A supervisor consumer is not an execution slot, so this uses a typed
    zero-slot ``message`` request bound to the actual inbox message.  The decision is still bound
    to the exact pre-append journal prefix and is replayed without trusting a
    caller-provided snapshot.
    """
    import _workspace_budget as budget

    required = {"schema", "status", "policy", "snapshot", "request", "decision",
                "request_identity_sha256", "facts_identity_sha256", "result_sha256",
                "journal_revision", "journal_prefix_sha256"}
    if type(value) is not dict or set(value) != required:
        raise SwarmCorruptError("invalid supervisor offer budget admission")
    if value["schema"] != _SUPERVISOR_BUDGET_SCHEMA or value["status"] != status:
        raise SwarmCorruptError("invalid supervisor offer budget status")
    try:
        policy = budget.validate_policy(value["policy"])
        snapshot = budget.validate_snapshot(value["snapshot"])
        request = budget.validate_request(value["request"])
        if (request["kind"] != "message"
                or request["selected_message_ids"] != [selected_message_id]
                or request["execution_slots_requested"] != 0):
            raise budget.WorkspaceBudgetError("supervisor offer must be one zero-slot message request")
        if value["journal_revision"] != journal_revision:
            raise budget.WorkspaceBudgetError("supervisor offer budget revision differs")
        stored_prefix = value["journal_prefix_sha256"]
        budget._validate_binding_digest(stored_prefix, "supervisor offer budget prefix digest")
        if journal_prefix_sha256 is not None and stored_prefix != journal_prefix_sha256:
            raise budget.WorkspaceBudgetError("supervisor offer budget prefix differs")
        prefix_for_binding = stored_prefix if journal_prefix_sha256 is None else journal_prefix_sha256
        consumed = budget.consume_bound_decision(
            value["decision"], policy=policy, snapshot=snapshot, request=request,
            journal_revision=journal_revision, journal_prefix_sha256=prefix_for_binding)
        if consumed["status"] != "accepted" or consumed["execution_status"] != "not_requested":
            raise budget.WorkspaceBudgetError("supervisor offer budget is not available")
        if value["request_identity_sha256"] != budget.request_identity_sha256(request):
            raise budget.WorkspaceBudgetError("supervisor offer request digest differs")
        if value["facts_identity_sha256"] != budget.facts_identity_sha256(policy, snapshot, request):
            raise budget.WorkspaceBudgetError("supervisor offer facts digest differs")
        if value["result_sha256"] != budget.result_sha256(consumed):
            raise budget.WorkspaceBudgetError("supervisor offer result digest differs")
        return {
            "schema": _SUPERVISOR_BUDGET_SCHEMA, "status": status,
            "policy": policy, "snapshot": snapshot, "request": request,
            "decision": copy.deepcopy(value["decision"]),
            "request_identity_sha256": value["request_identity_sha256"],
            "facts_identity_sha256": value["facts_identity_sha256"],
            "result_sha256": value["result_sha256"],
            "journal_revision": journal_revision,
            "journal_prefix_sha256": stored_prefix,
        }
    except (budget.WorkspaceBudgetError, KeyError, TypeError, ValueError):
        raise SwarmCorruptError("invalid supervisor offer budget admission") from None


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _canonical_json(value: Any, label: str, *, maximum: int = MAX_JOURNAL_BYTES) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SwarmProtocolError(f"invalid {label} serialization") from exc
    if len(encoded) > maximum:
        raise SwarmProtocolError(f"{label} exceeds byte bound")
    return encoded


def _economics_metric_json_width(value: int | float) -> int | None:
    """Return canonical JSON width for a finite accounting metric."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError):
        return None
    return len(encoded)


def _economics_timestamp_padding(value: int | float) -> int:
    """Reserve conservative width for any finite binary64 timestamp."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError("economics timestamp probe must be finite numeric")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("economics timestamp probe is not JSON numeric") from exc
    if len(encoded) > _ECONOMICS_MAX_TIMESTAMP_JSON_BYTES:
        raise ValueError("economics timestamp probe exceeds finite width")
    return _ECONOMICS_MAX_TIMESTAMP_JSON_BYTES - len(encoded)


def _accounting_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value, "submission accounting",
                                          maximum=MIN_ECONOMICS_SETTLEMENT_BYTES * 8)).hexdigest()


def _validate_economics_accounting(value: Mapping[str, Any], *, claim: Mapping[str, Any],
                                   selection: Mapping[str, Any], reservation: Mapping[str, Any],
                                   owner_generation: int, submission_state: str,
                                   result_status: str) -> tuple[dict[str, Any], str]:
    """Validate the complete private accounting outcome before releasing a fence.

    A terminal digest by itself is not an accounting result.  The immutable
    record must bind the physical attempt, request/material identities, exact
    selected context, bounded estimate, usage provenance, and uncertainty
    flags to the live claim and reservation.  This remains provider-neutral;
    it never infers provider spend from a process launch.
    """
    if type(value) is not dict or set(value) != {
            "schema", "attempt", "identity", "estimate", "reported", "contact",
            "submission_state", "uncertain_spend", "unknown_spend"}:
        raise SwarmBudgetRefusal("economics_accounting_required")
    if value["schema"] != _ACCOUNTING_SCHEMA:
        raise SwarmBudgetRefusal("economics_accounting_invalid")
    attempt = value["attempt"]
    if (type(attempt) is not dict
            or set(attempt) != {"id", "kind", "ordinal", "parent_id"}
            or not re.fullmatch(r"[a-f0-9]{32}", str(attempt.get("id", "")))
            or type(attempt.get("kind")) is not str
            or not 1 <= len(attempt["kind"]) <= _ECONOMICS_MAX_ATTEMPT_KIND_CHARS
            or type(attempt.get("ordinal")) is not int
            or not 1 <= attempt["ordinal"] <= 1024
            or (attempt.get("parent_id") is not None
                and not re.fullmatch(r"[a-f0-9]{32}", str(attempt["parent_id"])) )):
        raise SwarmBudgetRefusal("economics_accounting_invalid")
    if attempt["id"] != reservation.get("attempt_id"):
        raise SwarmBudgetRefusal("economics_attempt_binding_required")
    if attempt["ordinal"] != selection.get("attempt"):
        raise SwarmBudgetRefusal("economics_attempt_binding_required")
    identity = value["identity"]
    if (type(identity) is not dict or set(identity) != {"request_sha256", "material_sha256"}
            or identity.get("request_sha256") != claim.get("request_sha256")
            or identity.get("material_sha256") != reservation.get("material_sha256")):
        raise SwarmBudgetRefusal("economics_identity_binding_required")
    _digest(identity["request_sha256"], "accounting request digest")
    _digest(identity["material_sha256"], "accounting material digest")
    estimate = value["estimate"]
    if (type(estimate) is not dict
            or set(estimate) != {"method", "represented_bytes", "estimated_tokens", "scope",
                                 "boundary", "complete_provider_input", "outside_scope", "material_sha256"}
            or estimate.get("method") != "utf8_bytes_divided_by_4_ceiling"
            or type(estimate.get("represented_bytes")) is not int
            or not 0 <= estimate["represented_bytes"] <= _ECONOMICS_MAX_REPRESENTED_BYTES
            or type(estimate.get("estimated_tokens")) is not int
            or not 0 <= estimate["estimated_tokens"] <= _ECONOMICS_MAX_ESTIMATED_TOKENS
            or estimate.get("scope") != _ECONOMICS_SCOPE
            or estimate.get("boundary") != _ECONOMICS_BOUNDARY
            or estimate.get("complete_provider_input") is not False
            or type(estimate.get("outside_scope")) is not list
            or estimate.get("outside_scope") != list(_ECONOMICS_OUTSIDE_SCOPE)
            or estimate.get("material_sha256") != identity["material_sha256"]):
        raise SwarmBudgetRefusal("economics_estimate_invalid")
    if estimate["estimated_tokens"] != (estimate["represented_bytes"] + 3) // 4:
        raise SwarmBudgetRefusal("economics_estimate_invalid")
    reported = value["reported"]
    if (type(reported) is not dict
            or set(reported) != {"metrics", "field_state", "provenance", "observation_scope", "completeness"}
            or type(reported.get("metrics")) is not dict
            or set(reported["metrics"]) != {"input_tokens", "output_tokens", "total_tokens",
                                              "reasoning_tokens", "cache_read_tokens", "cache_write_tokens"}
            or type(reported.get("field_state")) is not dict
            or set(reported["field_state"]) != set(reported["metrics"])
            or type(reported.get("provenance")) is not str
            or len(reported.get("provenance", "")) > _ECONOMICS_MAX_PROVENANCE_CHARS
            or reported.get("observation_scope") not in _ECONOMICS_OBSERVATION_SCOPES
            or reported.get("completeness") not in {"missing", "partial", "complete", "malformed"}
            or any((item is not None and (isinstance(item, bool)
                                          or not isinstance(item, (int, float))
                                          or not math.isfinite(item)
                                          or not 0 <= item <= _ECONOMICS_MAX_METRIC
                                          or (_economics_metric_json_width(item) is None)
                                              or (_economics_metric_json_width(item)
                                              > _ECONOMICS_MAX_METRIC_JSON_CHARS)))
                   for item in reported["metrics"].values())
            or any(item not in {"absent", "reported", "malformed"}
                   for item in reported["field_state"].values())
            or any((reported["field_state"][name] == "reported"
                    and reported["metrics"][name] is None)
                   or (reported["field_state"][name] in {"absent", "malformed"}
                       and reported["metrics"][name] is not None)
                   for name in reported["metrics"])):
        raise SwarmBudgetRefusal("economics_usage_invalid")
    contact = value["contact"]
    if (type(contact) is not dict
            or set(contact) != {"local_process_created", "possible_submission", "evidence"}
            or (contact["local_process_created"] is not None
                and type(contact["local_process_created"]) is not bool)
            or (contact["possible_submission"] is not None
                and type(contact["possible_submission"]) is not bool)
            or contact.get("evidence") not in _ECONOMICS_CONTACT_EVIDENCE):
        raise SwarmBudgetRefusal("economics_contact_invalid")
    if value["submission_state"] != submission_state:
        raise SwarmBudgetRefusal("economics_submission_binding_required")
    if submission_state not in {"submitted", "not_submitted", "indeterminate"}:
        raise SwarmBudgetRefusal("invalid economics submission state")
    if result_status not in {"success", "partial", "error", "cancelled", "timeout"}:
        raise SwarmBudgetRefusal("invalid economics result status")
    if type(value["uncertain_spend"]) is not bool or type(value["unknown_spend"]) is not bool:
        raise SwarmBudgetRefusal("economics_uncertainty_invalid")
    possible_submission = contact["possible_submission"]
    if submission_state == "not_submitted":
        if possible_submission is not False or value["uncertain_spend"] or value["unknown_spend"]:
            raise SwarmBudgetRefusal("economics_submission_consistency_required")
    elif submission_state == "submitted":
        # A confirmed submission may still have unknown usage/spend when the
        # provider returned no usage record.  Submission evidence and usage
        # provenance are separate facts; retain the uncertainty.
        if possible_submission is not True:
            raise SwarmBudgetRefusal("economics_submission_consistency_required")
    elif not value["unknown_spend"] or possible_submission not in {None, True}:
        raise SwarmBudgetRefusal("economics_submission_consistency_required")
    # Owner generation and selected context are recorded in the settlement
    # wrapper; the accounting record itself remains the standard private
    # submission-accounting schema and is not extended with host authority.
    if (type(owner_generation) is not int
            or not 1 <= owner_generation <= MAX_GENERATION):
        raise SwarmBudgetRefusal("economics_owner_binding_required")
    digest = _accounting_digest(value)
    wrapper = {"schema": _ECONOMICS_SETTLEMENT_SCHEMA, "accounting": value,
               "accounting_sha256": digest, "submission_state": submission_state,
               "result_status": result_status, "owner_generation": owner_generation,
               "attempt_id": attempt["id"],
               # The reservation binds the raw selected context.  The
               # accounting identity binds the role-framed estimator material;
               # these are intentionally distinct identities.
               "payload_sha256": reservation.get("payload_sha256"),
               "material_sha256": reservation.get("material_sha256"),
               "selection_sha256": reservation.get("selection_sha256"),
               "policy_sha256": reservation.get("policy_sha256")}
    if len(_canonical_json(wrapper, "economics settlement wrapper")) > reservation["max_settlement_bytes"]:
        raise SwarmBudgetRefusal("economics_settlement_capacity_insufficient")
    return copy.deepcopy(value), digest


def _positive_int(value: Any, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SwarmProtocolError(f"invalid {label}")
    if maximum is not None and value > maximum:
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _validate_economics_config(value: Any) -> dict[str, Any]:
    """Validate the persisted v2 economics fence independently of callers."""
    if type(value) is not dict or set(value) != {
            "schema", "feature", "enabled", "max_records", "max_settlement_bytes"}:
        raise SwarmProtocolError("invalid economics feature fence")
    if (value["schema"] != "summon.workspace.economics/v1"
            or value["feature"] != "submission-accounting-v1"
            or value["enabled"] is not True
            or type(value["max_records"]) is not int
            or not 1 <= value["max_records"] <= 64
            or type(value["max_settlement_bytes"]) is not int
            or not MIN_ECONOMICS_SETTLEMENT_BYTES <= value["max_settlement_bytes"] <= 65536):
        raise SwarmProtocolError("invalid economics feature fence")
    return copy.deepcopy(value)


def _bounded_reason(value: Any, label: str = "reason") -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value or len(value) > MAX_REASON_CHARS:
        raise SwarmProtocolError(f"invalid {label}")
    if any(c in value for c in "\x00\r\n"):
        raise SwarmProtocolError(f"invalid {label}")
    # The coordinator's public journal is not a raw provider-output channel.
    # Keep useful bounded prose while applying the same conservative path and
    # credential boundary as the conversation atlas. Secret-bearing output
    # belongs in a provider's private receipt.
    text = re.sub(r"(?i)\b[A-Z]:[\\/][^\r\n,;]*", "[path]", value)
    text = re.sub(r"(?<![\w])/(?:[^\r\n,;]*)", "[path]", text)
    text = re.sub(r"(?<![\w])(?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.-]+", "[path]", text)
    text = re.sub(r"(?i)(?:token|password|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
                  "[redacted]", text)
    text = re.sub(r"(?i)\b(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b",
                  "[redacted]", text)
    text = re.sub(r"(?i)(?:\bauthorization\s*:\s*)?\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}",
                  "[redacted]", text)
    text = re.sub(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+", "[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                  "[redacted]", text)
    text = re.sub(r"\b(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)[A-Z0-9_-]{3,}\b",
                  "[redacted]", text, flags=re.IGNORECASE)
    return text


def _now_ms(clock) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SwarmCoordinatorError("coordinator clock returned a non-number")
    if not (value == value and abs(value) < 10**13):
        raise SwarmCoordinatorError("coordinator clock is invalid")
    result = int(value * 1000)
    if result < 1:
        raise SwarmCoordinatorError("coordinator clock is before the Unix epoch")
    return result


def _message_id(prefix: str = "msg") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _materialize_tasks(tasks: Iterable[Mapping[str, Any]], *, max_attempts: int) -> list[dict[str, Any]]:
    if isinstance(tasks, (str, bytes, Mapping)):
        raise SwarmProtocolError("tasks must be an iterable of task objects")
    materialized: list[dict[str, Any]] = []
    seen: set[str] = set()
    iterator = iter(tasks)
    for _ in range(MAX_TASKS + 1):
        try:
            item = next(iterator)
        except StopIteration:
            break
        if not isinstance(item, Mapping):
            raise SwarmProtocolError("task must be an object")
        if set(item) - {"task_id", "request_sha256"}:
            raise SwarmProtocolError("unknown task field")
        task_id = _id(item.get("task_id"), "task id")
        request_sha = _digest(item.get("request_sha256"), "request digest")
        if task_id in seen:
            raise SwarmProtocolError("duplicate task id")
        seen.add(task_id)
        materialized.append({"task_id": task_id, "request_sha256": request_sha})
    else:
        raise SwarmProtocolError("too many tasks")
    if not materialized:
        raise SwarmProtocolError("swarm must contain at least one task")
    if max_attempts < 1 or max_attempts > MAX_ATTEMPTS:
        raise SwarmProtocolError("invalid max attempts")
    return materialized


class SwarmCoordinator:
    """A local durable coordinator for one swarm run.

    ``runs_root`` is a private operator-owned directory.  The coordinator
    stores digests, bounded previews, and lifecycle facts only; prompts,
    provider output, credentials, and artifact bytes are never written here.
    """

    def __init__(self, runs_root: str | os.PathLike[str], run_id: str, *,
                 lease_sec: float = 30.0, clock=None):
        self.runs_root = str(runs_root)
        self.run_id = validate_run_id(run_id)
        self.run_dir = run_path(self.runs_root, self.run_id)
        if not isinstance(lease_sec, (int, float)) or isinstance(lease_sec, bool):
            raise ValueError("lease_sec must be numeric")
        if not (0.5 <= float(lease_sec) <= 3600.0):
            raise ValueError("lease_sec is outside the coordinator bounds")
        self.lease_sec = float(lease_sec)
        self.clock = clock or time.time
        # Transport-budget consumption is a pre-bootstrap reservation.  Keep
        # the identity set behind a process-local lock so two independently
        # issued adapters cannot both consume the same bound decision.  The
        # journal/claim remains the durable execution reservation; this guard
        # only closes the race between adapter objects before a child exists.
        self._budget_consume_lock = threading.Lock()
        self._budget_consumed: set[tuple[str, str, int, str]] = set()

    # --- construction and durable IO -------------------------------------

    @classmethod
    def create(cls, runs_root: str | os.PathLike[str], run_id: str, *,
               project_root_sha256: str, roster_definition_sha256: str,
               tasks: Iterable[Mapping[str, Any]], max_attempts: int = 1,
               lease_sec: float = 30.0, clock=None,
               economics: Mapping[str, Any] | None = None) -> "SwarmCoordinator":
        coordinator = cls(runs_root, run_id, lease_sec=lease_sec, clock=clock)
        _digest(project_root_sha256, "project root digest")
        _digest(roster_definition_sha256, "roster definition digest")
        checked_economics = (_validate_economics_config(economics)
                             if economics is not None else None)
        task_list = _materialize_tasks(tasks, max_attempts=max_attempts)
        owner = coordinator._acquire(allow_empty=True)
        try:
            records, torn = coordinator._read_records()
            if records or torn:
                raise SwarmConflictError("swarm run already exists")
            # Refuse foreign files in a run directory.  Owner/generation files
            # may be left by a crashed, empty acquisition and are harmless.
            try:
                names = set(os.listdir(coordinator.run_dir))
            except OSError as exc:
                raise SwarmCoordinatorError("cannot inspect swarm run directory") from exc
            allowed = {"owner.lock", "generation.txt"}
            allowed.update(name for name in names if name.startswith("lease-") and name.endswith(".json"))
            allowed.update(name for name in names if name.startswith("journal-g") and name.endswith(".jsonl"))
            if names - allowed:
                raise SwarmConflictError("swarm run directory contains foreign files")
            prepared = {
                "event": "swarm_prepared",
                "schema": 1,
                "run_id": coordinator.run_id,
                "project_root_sha256": project_root_sha256,
                "roster_definition_sha256": roster_definition_sha256,
                "max_attempts": max_attempts,
                "tasks": task_list,
                "message_id": _message_id("prepare"),
            }
            if checked_economics is not None:
                prepared["economics"] = checked_economics
            coordinator._append_initial(owner, prepared)
        finally:
            release_owner(owner)
        return coordinator

    def _acquire(self, *, repair: bool = True, allow_empty: bool = False) -> Owner:
        owner = None
        try:
            # Ownership advances durable generation state. Refuse unsupported
            # history before crossing that boundary, including direct mutators.
            initial, _torn, _raw = self._load_with_snapshot(allow_empty=allow_empty)
            # Creation alone may begin empty; observed history cannot disappear.
            allow_empty = allow_empty and not initial.get("prepared")
            owner = acquire_owner(self.run_dir, self.lease_sec)
            self._fence_run_files()
            # Reconcile the predecessor's exact prefix before repair and again
            # after repair.  A successful lock acquisition is not permission
            # to treat an unreadable/disappearing journal as an empty run.
            state, torn, _raw = self._load_with_snapshot(allow_empty=allow_empty)
            allow_empty = allow_empty and not state.get("prepared")
            if torn and not repair:
                raise SwarmCorruptError("reconciliation refuses a torn journal prefix")
            if repair:
                if not owner_still_current(owner):
                    raise SwarmCorruptError("swarm owner changed before journal repair")
                journal_repair(self.run_dir, owner)
            self._fence_run_files()
            _state, torn, _raw = self._load_with_snapshot(allow_empty=allow_empty)
            if torn:
                raise SwarmCorruptError("swarm journal torn tail was not repaired")
            return owner
        except BaseException as exc:  # noqa: BLE001 - release only our owner
            if owner is not None:
                release_owner(owner)
            if isinstance(exc, (SwarmCoordinatorError,)):
                raise
            if isinstance(exc, (OSError, ValueError, JournalCorruptError)):
                raise SwarmCoordinatorError("cannot acquire swarm coordinator owner") from exc
            raise

    def _fence_run_files(self) -> None:
        """Reject link-like coordinator files before any open/replace.

        ``run_path`` fences the run directory itself, but a hostile or
        partially-recovered run can still contain a symlinked journal segment,
        lease, lock, or generation file.  Those names are opened by the
        shared run-dir helper, so fail closed before it gets a chance to
        follow one outside the coordinator root.
        """
        root = Path(self.run_dir)
        if not root.exists():
            return
        try:
            children = list(root.iterdir())
        except OSError as exc:
            raise SwarmCoordinatorError("cannot inspect swarm run directory") from exc
        for child in children:
            name = child.name
            managed = (
                name in {"owner.lock", "generation.txt"}
                or (name.startswith("lease-") and name.endswith(".json"))
                or (name.startswith("journal-g") and name.endswith(".jsonl"))
            )
            if not managed:
                continue
            try:
                attrs = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
                junction = bool(getattr(child, "is_junction", lambda: False)())
                if child.is_symlink() or junction or bool(attrs & 0x400):
                    raise SwarmCoordinatorError(f"swarm managed file may not be a link: {name}")
                if not child.is_file():
                    raise SwarmCoordinatorError(f"swarm managed path is not a file: {name}")
            except FileNotFoundError:
                # A concurrent cleanup/recovery can remove a stale sidecar;
                # the next fenced scan will decide whether it is safe.
                continue
            except OSError as exc:
                raise SwarmCoordinatorError(f"cannot inspect swarm managed file: {name}") from exc

    @staticmethod
    def _strict_read_segment(path: Path, generation: int) -> tuple[list[dict[str, Any]], bool, bytes]:
        """Read one journal segment without converting IO failure to history.

        The shared legacy reader intentionally treats an unavailable file as
        empty for older recovery callers.  The coordinator cannot do that:
        ``[]`` is an authoritative statement that no spend/event prefix
        exists.  This reader snapshots bytes, keeps CRLF/LF compatibility, and
        marks only a malformed final line as repairable torn tail.
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise SwarmCorruptError("journal prefix disappeared during read") from exc
        except OSError as exc:
            raise SwarmCoordinatorError("cannot read swarm journal prefix") from exc
        if not raw:
            return [], False, raw
        lines = raw.splitlines()
        # ``bytes.splitlines`` drops a final CR/LF delimiter.  A journal line
        # written by the owned binary seam always ends in LF; historical CRLF
        # files remain accepted.  A final unterminated valid JSON record is
        # still valid legacy history, matching _read_segment semantics.
        records: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            try:
                text = line.decode("utf-8")
                data = json.loads(text)
                if not isinstance(data, dict) or not isinstance(data.get("sha256"), str):
                    raise ValueError("journal record is missing its checksum")
                claimed = data.pop("sha256")
                serialized = json.dumps(data, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False)
                valid = hashlib.sha256(serialized.encode("utf-8")).hexdigest() == claimed
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                valid = False
                data = None
            if valid:
                declared_generation = data.get("generation")
                if (type(declared_generation) is not int
                        or declared_generation != generation):
                    raise SwarmCorruptError(
                        f"journal-g{generation}.jsonl contains a generation mismatch")
                records.append(data)
            elif index == len(lines) - 1:
                return records, True, raw
            else:
                raise SwarmCorruptError(
                    f"{path.name} line {index + 1} failed its checksum")
        return records, False, raw

    @staticmethod
    def _prefix_fingerprint(raw_map: Mapping[str, bytes]) -> tuple[tuple[str, int, str], ...]:
        return tuple(sorted((name, len(raw), hashlib.sha256(raw).hexdigest())
                            for name, raw in raw_map.items()))

    @classmethod
    def _prefix_digest(cls, raw_map: Mapping[str, bytes]) -> str:
        return hashlib.sha256(_canonical_json(
            cls._prefix_fingerprint(raw_map), "journal prefix fingerprint")).hexdigest()

    def _strict_snapshot_once(self) -> tuple[list[tuple[int, dict[str, Any]]], bool, dict[str, bytes]]:
        root = Path(self.run_dir)
        try:
            root.stat()
        except FileNotFoundError:
            return [], False, {}
        except OSError as exc:
            raise SwarmCoordinatorError("cannot inspect swarm journal prefix") from exc
        if not root.is_dir():
            raise SwarmCoordinatorError("swarm run path is not a directory")
        try:
            names = sorted(name for name in os.listdir(root)
                           if re.fullmatch(r"journal-g\d+\.jsonl", name))
        except OSError as exc:
            raise SwarmCoordinatorError("cannot list swarm journal prefix") from exc
        tagged: list[tuple[int, dict[str, Any]]] = []
        raw_map: dict[str, bytes] = {}
        segments: list[tuple[int, str, list[dict[str, Any]], bool, bytes]] = []
        declared_bytes = 0
        observed_bytes = 0
        observed_records = 0
        for name in names:
            match = re.fullmatch(r"journal-g(\d+)\.jsonl", name)
            assert match is not None
            generation = int(match.group(1))
            if not 1 <= generation <= MAX_GENERATION:
                raise SwarmCorruptError("journal generation is outside the bounded range")
            path = root / name
            try:
                segment_size = int(path.stat(follow_symlinks=False).st_size)
            except OSError as exc:
                raise SwarmCoordinatorError("cannot inspect swarm journal prefix") from exc
            if segment_size < 0 or declared_bytes + segment_size > MAX_JOURNAL_BYTES:
                raise SwarmCorruptError("swarm journal prefix exceeds the byte bound")
            declared_bytes += segment_size
            records, segment_torn, raw = self._strict_read_segment(path, generation)
            observed_bytes += len(raw)
            if observed_bytes > MAX_JOURNAL_BYTES:
                raise SwarmCorruptError("swarm journal prefix exceeds the byte bound")
            observed_records += len(records)
            if observed_records > MAX_JOURNAL_RECORDS:
                raise SwarmCorruptError("swarm journal prefix exceeds the record bound")
            segments.append((generation, name, records, segment_torn, raw))
            raw_map[name] = raw
        nonempty = [(generation, name) for generation, name, _records, _torn, raw in segments
                    if raw]
        newest = max((generation for generation, _ in nonempty), default=None)
        torn_segments = [(generation, name) for generation, name, _records, segment_torn, _raw in segments
                         if segment_torn]
        for generation, name in torn_segments:
            if newest != generation:
                raise SwarmCorruptError(
                    f"{name} has a torn tail below the newest generation")
        torn = bool(torn_segments)
        for generation, name, records, _segment_torn, _raw in sorted(segments):
            for record in records:
                tagged.append((generation, record))
        return tagged, torn, raw_map

    def _strict_snapshot(self) -> tuple[list[tuple[int, dict[str, Any]]], bool, dict[str, bytes]]:
        self._fence_run_files()
        first = self._strict_snapshot_once()
        self._fence_run_files()
        second = self._strict_snapshot_once()
        if first[2] != second[2]:
            raise SwarmCorruptError("swarm journal prefix unstable")
        self._fence_run_files()
        return second

    def _sync_prefix(self, owner: Owner, raw_map: Mapping[str, bytes]) -> dict[str, bytes]:
        """Synchronize and revalidate the exact prefix before a mutation.

        Two identical reads prove a stable observation, not durable storage.
        Reopening each current segment with a non-mutating read/write handle,
        checking byte identity/length, and fsyncing it closes that distinction
        before an idempotent response or a new admission is acknowledged.
        """
        if not owner_still_current(owner):
            raise SwarmCorruptError("swarm owner changed before prefix sync")
        expected_names = set(raw_map)
        synced_identities: dict[str, tuple[int, int, int, int]] = {}
        for name, expected in sorted(raw_map.items()):
            path = Path(self.run_dir) / name
            try:
                with path.open("r+b") as handle:
                    before_stat = os.fstat(handle.fileno())
                    path_stat = path.stat()
                    if (before_stat.st_size != len(expected)
                            or path_stat.st_size != len(expected)
                            or (before_stat.st_ino and path_stat.st_ino
                                and before_stat.st_ino != path_stat.st_ino)
                            or (before_stat.st_dev and path_stat.st_dev
                                and before_stat.st_dev != path_stat.st_dev)):
                        raise SwarmCorruptError("swarm journal handle identity changed before sync")
                    observed = handle.read()
                    if len(observed) != len(expected) or observed != expected:
                        raise SwarmCorruptError("swarm journal prefix changed before sync")
                    if not owner_still_current(owner):
                        raise SwarmCorruptError("swarm owner changed during prefix sync")
                    handle.flush()
                    os.fsync(handle.fileno())
                    after_stat = os.fstat(handle.fileno())
                    path_stat_after = path.stat()
                    if (after_stat.st_size != len(expected)
                            or (before_stat.st_ino and after_stat.st_ino
                                and before_stat.st_ino != after_stat.st_ino)
                            or (before_stat.st_dev and after_stat.st_dev
                                and before_stat.st_dev != after_stat.st_dev)
                            or (path_stat_after.st_size != len(expected)
                                or (before_stat.st_ino and path_stat_after.st_ino
                                    and before_stat.st_ino != path_stat_after.st_ino)
                                or (before_stat.st_dev and path_stat_after.st_dev
                                    and before_stat.st_dev != path_stat_after.st_dev))):
                        raise SwarmCorruptError("swarm journal handle identity changed after sync")
                    synced_identities[name] = (
                        int(after_stat.st_dev), int(after_stat.st_ino),
                        int(after_stat.st_size), int(getattr(after_stat, "st_mtime_ns", 0)))
                    handle.seek(0)
                    after = handle.read()
                    if len(after) != len(expected) or after != expected:
                        raise SwarmCorruptError("swarm journal prefix changed after sync")
            except SwarmCorruptError:
                raise
            except FileNotFoundError as exc:
                raise SwarmCorruptError("swarm journal prefix disappeared during sync") from exc
            except OSError as exc:
                raise SwarmCoordinatorError("cannot synchronize swarm journal prefix") from exc
        if not owner_still_current(owner):
            raise SwarmCorruptError("swarm owner changed after prefix sync")
        _tagged, _torn, final_map = self._strict_snapshot()
        if set(final_map) != expected_names or final_map != dict(raw_map):
            raise SwarmCorruptError("swarm journal prefix changed during sync")
        for name, identity in synced_identities.items():
            try:
                final_stat = (Path(self.run_dir) / name).stat()
            except OSError as exc:
                raise SwarmCorruptError("swarm journal path disappeared after sync") from exc
            final_identity = (
                int(final_stat.st_dev), int(final_stat.st_ino),
                int(final_stat.st_size), int(getattr(final_stat, "st_mtime_ns", 0)))
            if final_identity != identity:
                raise SwarmCorruptError("swarm journal path identity changed during sync")
        return final_map

    def _read_records_snapshot(self) -> tuple[list[dict[str, Any]], bool, dict[str, bytes]]:
        tagged, torn, raw = self._strict_snapshot()
        records: list[dict[str, Any]] = []
        for generation, record in tagged:
            if not isinstance(record, dict) or record.get("generation") != generation:
                raise SwarmCorruptError("swarm journal generation mismatch")
            records.append(record)
        if torn and not records:
            raise SwarmCorruptError("swarm journal has no complete preparation")
        return records, torn, raw

    def _read_records(self) -> tuple[list[dict[str, Any]], bool]:
        records, torn, _raw = self._read_records_snapshot()
        return records, torn

    def _state(self, records: list[dict[str, Any]], *, allow_empty: bool = False) -> dict[str, Any]:
        if not records:
            if allow_empty:
                return {"prepared": False, "tasks": {}, "claims": {}, "workers": {},
                        "messages": [], "artifacts": [], "admissions": {},
                        "supervisor_budget_admissions": {},
                        "seen": {}, "economics": None, "torn_tail": False}
            raise SwarmNotFoundError(f"swarm run {self.run_id!r} is not initialized")
        prepared = [record for record in records if record.get("event") == "swarm_prepared"]
        if len(prepared) != 1 or records[0].get("event") != "swarm_prepared":
            raise SwarmCorruptError("swarm journal has no unique leading preparation")
        first = prepared[0]
        _validate_preparation_identity(first, self.run_id)
        try:
            max_attempts = _positive_int(first["max_attempts"], "max attempts", MAX_ATTEMPTS)
            project = _digest(first["project_root_sha256"], "project root digest")
            roster = _digest(first["roster_definition_sha256"], "roster definition digest")
            tasks = _materialize_tasks(first["tasks"], max_attempts=max_attempts)
            economics = (_validate_economics_config(first["economics"])
                         if "economics" in first else None)
        except (KeyError, TypeError, ValueError, SwarmProtocolError) as exc:
            raise SwarmCorruptError("invalid swarm preparation record") from exc
        state: dict[str, Any] = {
            "prepared": True,
            "run_id": self.run_id,
            "project_root_sha256": project,
            "roster_definition_sha256": roster,
            "max_attempts": max_attempts,
            "tasks": {
                task["task_id"]: {**task, "attempts": [], "terminal": None,
                                  "retry_authorized": False, "uncertain_spend": False}
                for task in tasks
            },
            "claims": {},
            "workers": {},
            "messages": [],
            "artifacts": [],
            "admissions": {},
            "supervisor_budget_admissions": {},
            "seen": {},
            "closed": False,
            "economics": economics,
        }
        for record in records:
            event = record.get("event")
            if not isinstance(event, str):
                raise SwarmCorruptError("swarm journal record has no event")
            message_id = record.get("message_id")
            if message_id is not None:
                _id(message_id, "journal message id")
                frame_hash = record.get("frame_sha256")
                if frame_hash is not None:
                    _digest(frame_hash, "journal frame digest")
                state["seen"][message_id] = {
                    "frame_sha256": frame_hash,
                    "response": record.get("response"),
                }
            if event in {"swarm_prepared", "journal_repaired"}:
                if event == "swarm_prepared" and state.get("closed"):
                    raise SwarmCorruptError("swarm preparation appears after close")
                continue
            if state.get("closed"):
                raise SwarmCorruptError("swarm journal contains an event after close")
            try:
                self._apply_record(state, record)
            except (KeyError, TypeError, ValueError, SwarmProtocolError) as exc:
                # Event names come from journal bytes. Keep the public error
                # stable and bounded instead of reflecting journal-controlled
                # text or retaining it through exception chaining.
                raise SwarmCorruptError("invalid swarm journal event") from None
        return state

    @staticmethod
    def _apply_record(state: dict[str, Any], record: Mapping[str, Any]) -> None:
        event = record.get("event")
        task_id = record.get("task_id")
        if event == _WORKSPACE_TURN_BUDGET_CONSUMED:
            allowed = {"event", "generation", "message_id", "frame_sha256", "response", "ts",
                       "admission_id", "claim_id", "request_identity_sha256",
                       "facts_identity_sha256", "result_sha256", "journal_revision",
                       "journal_prefix_sha256", "binding_sha256", "planned_work_sha256"}
            required = {"admission_id", "claim_id", "request_identity_sha256",
                        "facts_identity_sha256", "result_sha256", "journal_revision",
                        "journal_prefix_sha256", "binding_sha256", "planned_work_sha256"}
            if set(record) - allowed or not required <= set(record):
                raise ValueError("budget consumption record is malformed")
            admission_id = _id(record["admission_id"], "admission id")
            admission = state.setdefault("admissions", {}).get(admission_id)
            if not isinstance(admission, dict) or not isinstance(admission.get("budget_admission"), dict):
                raise ValueError("budget consumption references no reservation")
            budget_admission = admission["budget_admission"]
            if budget_admission.get("status") != "reserved":
                raise ValueError("budget reservation was already consumed")
            if (admission.get("claim_id") != record["claim_id"]
                    or budget_admission.get("request_identity_sha256") != record["request_identity_sha256"]
                    or budget_admission.get("facts_identity_sha256") != record["facts_identity_sha256"]
                    or budget_admission.get("result_sha256") != record["result_sha256"]
                    or budget_admission.get("journal_revision") != record["journal_revision"]
                    or budget_admission.get("journal_prefix_sha256") != record["journal_prefix_sha256"]):
                raise ValueError("budget consumption reservation mismatch")
            _digest(record["binding_sha256"], "budget binding digest")
            _digest(record["planned_work_sha256"], "budget planned-work digest")
            budget_admission["status"] = "consumed"
            admission["launch_intent"] = {
                "message_id": record.get("message_id"),
                "binding_sha256": record["binding_sha256"],
                "planned_work_sha256": record["planned_work_sha256"],
                "journal_revision": record["journal_revision"],
                "journal_prefix_sha256": record["journal_prefix_sha256"],
            }
            return
        if event == _WORKSPACE_TURN_ECONOMICS_SETTLED:
            allowed = {"event", "generation", "message_id", "frame_sha256", "response", "ts",
                       "admission_id", "claim_id", "result_sha256", "submission_state",
                       "result_status", "accounting", "accounting_sha256", "owner_generation",
                       "attempt_id", "payload_sha256", "selection_sha256", "policy_sha256"}
            required = {"admission_id", "claim_id", "result_sha256", "submission_state",
                        "result_status", "accounting", "accounting_sha256", "owner_generation",
                        "attempt_id", "payload_sha256", "selection_sha256", "policy_sha256"}
            if set(record) - allowed or not required <= set(record):
                raise ValueError("economics settlement record is malformed")
            admission_id = _id(record["admission_id"], "economics admission id")
            admission = state.setdefault("admissions", {}).get(admission_id)
            if not isinstance(admission, dict):
                raise ValueError("economics settlement references no admission")
            reservation = admission.get("economics_reservation")
            if (not isinstance(reservation, dict)
                    or reservation.get("status") != "reserved"):
                raise ValueError("economics reservation was already settled")
            if admission.get("claim_id") != record["claim_id"]:
                raise ValueError("economics settlement claim differs")
            claim = state.get("claims", {}).get(record["claim_id"])
            workspace_event = admission.get("workspace_event")
            payload = (workspace_event.get("payload")
                       if isinstance(workspace_event, Mapping) else None)
            selection = payload.get("selection") if isinstance(payload, Mapping) else None
            if not isinstance(claim, Mapping) or not isinstance(selection, Mapping):
                raise ValueError("economics settlement source is missing")
            try:
                accounting, accounting_sha = _validate_economics_accounting(
                    record["accounting"], claim=claim, selection=selection,
                    reservation=reservation, owner_generation=record["owner_generation"],
                    submission_state=record["submission_state"], result_status=record["result_status"])
                _digest(record["accounting_sha256"], "accounting digest")
                _digest(record["result_sha256"], "economics result digest")
                if (type(record["attempt_id"]) is not str
                        or not re.fullmatch(r"[a-f0-9]{32}", record["attempt_id"])):
                    raise SwarmProtocolError("invalid economics attempt id")
                _digest(record["payload_sha256"], "economics payload digest")
                _digest(record["selection_sha256"], "economics selection digest")
                _digest(record["policy_sha256"], "economics policy digest")
            except (SwarmProtocolError, SwarmBudgetRefusal) as exc:
                raise ValueError("economics settlement accounting is invalid") from exc
            if (accounting_sha != record["accounting_sha256"]
                    or accounting_sha != record["result_sha256"]
                    or record["owner_generation"] != claim.get("lease_generation")
                    or record["attempt_id"] != reservation.get("attempt_id")
                    or record["payload_sha256"] != reservation.get("payload_sha256")
                    or record["selection_sha256"] != reservation.get("selection_sha256")
                     or record["policy_sha256"] != reservation.get("policy_sha256")
                     or record["payload_sha256"] != selection.get("context_sha256")):
                raise ValueError("economics settlement binding differs")
            reservation["status"] = "settled"
            admission["economics_settlement"] = {
                "result_sha256": record["result_sha256"],
                "accounting_sha256": record["accounting_sha256"],
                "accounting": copy.deepcopy(accounting),
                "owner_generation": record["owner_generation"],
                "attempt_id": record["attempt_id"],
                "payload_sha256": record["payload_sha256"],
                "selection_sha256": record["selection_sha256"],
                "policy_sha256": record["policy_sha256"],
                "submission_state": record["submission_state"],
                "result_status": record["result_status"],
            }
            return
        if event == _SUPERVISOR_OFFER_BUDGET_CONSUMED:
            allowed = {"event", "generation", "message_id", "frame_sha256", "response", "ts",
                       "admission_id", "delivery_id", "offer_id", "offer_payload_sha256",
                       "binding_sha256", "budget_admission"}
            required = {"admission_id", "delivery_id", "offer_id", "offer_payload_sha256",
                        "binding_sha256", "budget_admission"}
            if set(record) - allowed or not required <= set(record):
                raise ValueError("supervisor offer budget record is malformed")
            admission_id = _id(record["admission_id"], "supervisor offer admission id")
            admissions = state.setdefault("supervisor_budget_admissions", {})
            if admission_id in admissions:
                raise ValueError("supervisor offer budget was already consumed")
            delivery_id = _id(record["delivery_id"], "supervisor offer delivery id")
            offer_id = _id(record["offer_id"], "supervisor offer id")
            _digest(record["offer_payload_sha256"], "supervisor offer payload digest")
            _digest(record["binding_sha256"], "supervisor offer binding digest")
            workspace = state.get("workspace")
            delivery = workspace.get("inbox_deliveries", {}).get(delivery_id) if isinstance(workspace, Mapping) else None
            if (not isinstance(delivery, Mapping) or delivery.get("state") != "offered"
                    or not isinstance(delivery.get("offer"), Mapping)
                    or delivery["offer"].get("offer_id") != offer_id):
                raise ValueError("supervisor offer is not currently offered")
            fingerprint = state.get("_prefix_fingerprint")
            prefix_digest = None
            if isinstance(fingerprint, (tuple, list)):
                prefix_digest = hashlib.sha256(
                    _canonical_json(fingerprint, "journal prefix fingerprint")).hexdigest()
            checked_budget = _validate_supervisor_budget_admission(
                record["budget_admission"],
                journal_revision=workspace["revision"],
                journal_prefix_sha256=prefix_digest,
                selected_message_id=delivery.get("message_id"))
            admissions[admission_id] = {
                "admission_id": admission_id, "delivery_id": delivery_id,
                "offer_id": offer_id,
                "offer_payload_sha256": record["offer_payload_sha256"],
                "binding_sha256": record["binding_sha256"],
                "budget_admission": checked_budget,
            }
            return
        if event == _WORKSPACE_WRAPPER_EVENT:
            if (set(record) - {"event", "workspace_event", "observed_at_ms", "generation", "ts"}
                    or not {"event", "workspace_event", "observed_at_ms"} <= set(record)):
                raise SwarmCorruptError("invalid workspace container")
            observed = record["observed_at_ms"]
            if type(observed) is not int or not 1 <= observed <= _BOUND_INTEGER:
                raise SwarmCorruptError("invalid workspace observation time")
            nested = record["workspace_event"]
            if type(nested) is not dict or nested.get("run_id") != state["run_id"]:
                raise SwarmCorruptError("workspace run mismatch")
            if nested.get("event") == _WORKSPACE_TURN_EVENT:
                raise SwarmCorruptError("atomic turn requires the composite coordinator record")
            state["workspace"] = SwarmCoordinator._workspace_after_record(state, record)
            return
        if event == _WORKSPACE_TURN_EVENT:
            # Replay the composite as one state transition.  The nested
            # workspace envelope is validated structurally here; the caller
            # performed the detached grant/hold/selection checks before the
            # append, and the stored digest prevents a later projection from
            # silently substituting a different envelope.
            import _workspace_admission as admission

            admission_id = _id(record.get("admission_id"), "admission id")
            workspace_event = record.get("workspace_event")
            if not isinstance(workspace_event, Mapping):
                raise ValueError("composite admission has no workspace event")
            event_bytes = _canonical_json(workspace_event, "workspace event", maximum=MAX_JOURNAL_BYTES)
            if _digest(record.get("workspace_event_sha256"), "workspace event digest") != hashlib.sha256(event_bytes).hexdigest():
                raise ValueError("workspace event digest mismatch")
            checked_event = admission._event_envelope(workspace_event)
            if checked_event["event"] != admission.ATOMIC_TURN_EVENT:
                raise ValueError("composite admission has wrong workspace event")
            if checked_event["run_id"] != state.get("run_id") or checked_event["operation_key"] != admission_id:
                raise ValueError("composite admission scope or operation mismatch")
            payload = checked_event["payload"]
            admission._object(payload, {"claim", "selection", "recipient", "grant_ref", "evidence_by_delivery",
                                        "affected_task_ids", "holds", "goal_revision",
                                        "selected_delivery_ids", "supported_ack_levels"},
                               {"budget_admission", "economics_reservation"})
            try:
                admission.protocol._attempt(payload["selection"])
                admission.protocol._worker(payload["recipient"])
            except (admission.protocol.WorkspaceProtocolError, AttributeError) as exc:
                raise ValueError("composite selection or recipient is malformed") from exc
            admission._canonical_reference(payload["grant_ref"], "grant reference")
            if type(payload["evidence_by_delivery"]) is not dict:
                raise ValueError("composite evidence is malformed")
            economics_reservation = None
            if "economics_reservation" in payload:
                try:
                    economics_reservation = admission.economics_reservation(
                        payload["economics_reservation"],
                        claim_id=payload["claim"]["claim_id"],
                        selection=payload["selection"])
                except admission.WorkspaceAdmissionError as exc:
                    raise ValueError("composite economics reservation is malformed") from exc
            existing_admission = state.setdefault("admissions", {}).get(admission_id)
            if existing_admission is not None:
                if (existing_admission.get("workspace_event_sha256") != record["workspace_event_sha256"]
                        or existing_admission.get("claim_id") != payload["claim"].get("claim_id")):
                    raise ValueError("admission id was reused for a different turn")
                return
            claim, prospective = admission._prospective_claim(payload.get("claim"), state)
            for key in ("task_id", "claim_id", "worker_id", "attempt", "lease_generation",
                        "lease_expires_at_ms", "request_sha256"):
                if record.get(key) != claim[key]:
                    raise ValueError("composite claim fence mismatch")
            if claim["claim_id"] in state["claims"]:
                raise ValueError("duplicate claim id")
            task = state["tasks"].get(claim["task_id"])
            if task is None:
                raise ValueError("claim references unknown task")
            workspace = state.get("workspace")
            if workspace is None:
                raise ValueError("composite admission requires authoritative workspace state")
            budget_admission = None
            if "budget_admission" in payload:
                fingerprint = state.get("_prefix_fingerprint")
                prefix_digest = None
                if isinstance(fingerprint, (tuple, list)):
                    prefix_digest = hashlib.sha256(
                        _canonical_json(fingerprint, "journal prefix fingerprint")).hexdigest()
                budget_admission = _validate_budget_admission(
                    payload["budget_admission"],
                    selected_message_ids=list(payload["selection"]["selected_message_ids"]),
                    journal_revision=workspace["revision"],
                    journal_prefix_sha256=prefix_digest)
            import _workspace_state as workspace_projection
            following_workspace = workspace_projection.apply_event(
                workspace, checked_event,
                admission._coordinator_view(prospective, state["run_id"]))
            state["claims"][claim["claim_id"]] = {
                "task_id": claim["task_id"], "claim_id": claim["claim_id"],
                "worker_id": claim["worker_id"], "attempt": claim["attempt"],
                "lease_generation": claim["lease_generation"],
                "lease_expires_at_ms": claim["lease_expires_at_ms"],
                "request_sha256": claim["request_sha256"], "status": "active",
                "cancel_requested": False, "cancel_acknowledged": False, "renewals": 0,
            }
            task["attempts"].append(claim["claim_id"])
            task["retry_authorized"] = False
            state["workspace"] = following_workspace
            state["admissions"][admission_id] = {
                "admission_id": admission_id,
                "claim_id": claim["claim_id"],
                "task_id": claim["task_id"],
                "request_identity_sha256": record.get("request_identity_sha256"),
                "workspace_event_sha256": record["workspace_event_sha256"],
                "message_id": record.get("message_id"),
                "response": copy.deepcopy(record.get("response")),
                "workspace_event": copy.deepcopy(dict(checked_event)),
            }
            if budget_admission is not None:
                state["admissions"][admission_id]["budget_admission"] = budget_admission
            if economics_reservation is not None:
                state["admissions"][admission_id]["economics_reservation"] = economics_reservation
            return
        if event == "worker_registered":
            worker_id = _id(record.get("worker_id"), "worker id")
            state["workers"][worker_id] = {
                "worker_id": worker_id,
                "worker_instance_id": _id(record.get("worker_instance_id"), "worker instance id"),
                "project_root_sha256": _digest(record.get("project_root_sha256"), "project root digest"),
                "roster_definition_sha256": _digest(record.get("roster_definition_sha256"), "roster digest"),
                "permission_ceiling": record.get("permission_ceiling"),
            }
            return
        if event in {"claim_granted", "claim_reclaimed", _WORKSPACE_TURN_EVENT}:
            task = state["tasks"].get(task_id)
            if task is None:
                raise ValueError("claim references unknown task")
            claim_id = _id(record.get("claim_id"), "claim id")
            if claim_id in state["claims"]:
                raise ValueError("duplicate claim id")
            claim = {
                "task_id": task_id,
                "claim_id": claim_id,
                "worker_id": _id(record.get("worker_id"), "worker id"),
                "attempt": _positive_int(record.get("attempt"), "attempt", MAX_ATTEMPTS),
                "lease_generation": _positive_int(record.get("lease_generation"), "lease generation",
                                                  MAX_GENERATION),
                "lease_expires_at_ms": _positive_int(record.get("lease_expires_at_ms"), "lease expiry"),
                "request_sha256": _digest(record.get("request_sha256"), "request digest"),
                "status": "active",
                "cancel_requested": False,
                "cancel_acknowledged": False,
                "renewals": 0,
            }
            if claim["request_sha256"] != task["request_sha256"]:
                raise ValueError("claim request differs from task")
            state["claims"][claim_id] = claim
            task["attempts"].append(claim_id)
            task["retry_authorized"] = False
            return
        if event == "claim_renewed":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("renew references unknown claim")
            if claim["status"] != "active":
                raise ValueError("renew references terminal claim")
            renewal_number = record.get("renewal_number", claim["renewals"] + 1)
            if (_positive_int(renewal_number, "renewal number", MAX_ROUTINE_RENEWALS)
                    != claim["renewals"] + 1):
                raise ValueError("renewal sequence is not contiguous")
            claim["renewals"] = renewal_number
            claim["lease_expires_at_ms"] = _positive_int(record.get("lease_expires_at_ms"), "lease expiry")
            return
        if event in {"claim_released", "claim_indeterminate", "task_completed", "task_failed",
                     "task_cancelled", "task_blocked"}:
            claim_id = record.get("claim_id")
            claim = state["claims"].get(_id(claim_id, "claim id")) if claim_id is not None else None
            if claim is None:
                raise ValueError("terminal event references unknown claim")
            status = {
                "claim_released": "released",
                "claim_indeterminate": "indeterminate",
                "task_completed": "completed",
                "task_failed": "failed",
                "task_cancelled": "cancelled",
                "task_blocked": "blocked",
            }[event]
            claim["status"] = status
            if event in {"task_completed", "task_failed", "task_cancelled", "task_blocked"}:
                state["tasks"][claim["task_id"]]["terminal"] = status
            if event == "claim_indeterminate":
                state["tasks"][claim["task_id"]]["terminal"] = "indeterminate"
                state["tasks"][claim["task_id"]]["uncertain_spend"] = True
            if event == "task_blocked":
                state["tasks"][claim["task_id"]]["uncertain_spend"] = True
            return
        if event == "retry_authorized":
            task = state["tasks"].get(task_id)
            if task is None:
                raise ValueError("retry authorization references unknown task")
            task["retry_authorized"] = True
            task["terminal"] = None
            return
        if event == "cancel_requested":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("cancel references unknown claim")
            claim["cancel_requested"] = True
            return
        if event == "cancel_acknowledged":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("cancel ack references unknown claim")
            if not claim["cancel_requested"]:
                raise ValueError("cancel ack precedes cancel request")
            if claim["cancel_acknowledged"]:
                raise ValueError("duplicate cancel ack")
            claim["cancel_acknowledged"] = True
            return
        if event == "message_posted":
            state["messages"].append({
                "message_id": _id(record.get("message_id"), "message id"),
                "from": _id(record.get("from"), "message sender"),
                "to": record.get("to"),
                "content_sha256": _digest(record.get("content_sha256"), "content digest"),
                "content_chars": _positive_int(record.get("content_chars"), "content length"),
                "preview": _bounded_reason(record.get("preview"), "message preview"),
                "reply_to": record.get("reply_to"),
            })
            return
        if event == "artifact_published":
            artifact = {
                key: record[key] for key in
                ("artifact_id", "task_id", "claim_id", "attempt", "lease_generation",
                 "request_sha256", "bytes", "media_type", "relative_path", "opaque_uri")
                if key in record
            }
            # The journal reserves ``sha256`` for its line checksum.  Artifact
            # content digests are namespaced on disk and restored to the
            # historical public projection here.
            if "artifact_sha256" in record:
                artifact["sha256"] = record["artifact_sha256"]
            elif "sha256" in record:
                # Accept only a pre-existing in-memory/legacy projection; the
                # strict journal reader cannot produce an ambiguous old line.
                artifact["sha256"] = record["sha256"]
            state["artifacts"].append(artifact)
            return
        if event == "run_closed":
            if SwarmCoordinator._workspace_reserve(state.get("workspace")):
                raise SwarmConflictError("cannot close with unresolved workspace delivery obligations")
            economics_bytes, economics_records = SwarmCoordinator._economics_reservation_totals(
                state, record)
            if economics_bytes or economics_records:
                raise SwarmConflictError("cannot close with unresolved economics reservations")
            state["closed"] = True
            return
        raise ValueError("unknown swarm event")

    def _load(self, *, allow_empty: bool = False) -> tuple[dict[str, Any], bool]:
        state, torn, _raw = self._load_with_snapshot(allow_empty=allow_empty)
        return state, torn

    def _load_with_snapshot(self, *, allow_empty: bool = False) -> tuple[dict[str, Any], bool, dict[str, bytes]]:
        records, torn, raw = self._read_records_snapshot()
        state = self._state(records, allow_empty=allow_empty)
        state["torn_tail"] = torn
        state["_prefix_fingerprint"] = self._prefix_fingerprint(raw)
        return state, torn, raw

    @staticmethod
    def workspace_admission_contract(_coordinator=None) -> dict[str, Any]:
        """Describe the centrally enforced workspace extension boundary.

        Ordinary workspace wrappers and atomic claim/selection share this base
        writer's canonical replay, exact frozen append and finite settlement
        reserve. This is a trusted-host integration contract, not provider or
        worker authentication, execution permission, or release certification.
        """
        return {
            "schema": "summon.workspace-admission/v1",
            "protocol": "summon.workspace/v1",
            "event_container": "workspace_event",
            "event_classes": sorted(_WORKSPACE_EVENT_CLASSES),
            "reconciliation": True,
            "control_reserve": True,
            "max_event_bytes": _BOUND_WORKSPACE_WRAPPER,
            "atomic_claim_selection": True,
            "budget_preflight": {
                "policy_schema": "summon.workspace.admission-policy/v1",
                "snapshot_schema": "summon.workspace.admission-snapshot/v1",
                "request_schema": "summon.workspace.admission-request/v1",
                "result_schema": "summon.workspace.admission-result/v1",
                "authoritative_inputs_required": True,
                "durable_execution_authorization": False,
            },
        }

    @staticmethod
    def workspace_worker_send_contract() -> dict[str, Any]:
        """Separate trusted-host base seam; no installed/authenticated adapter claim."""
        return {"schema": "summon.workspace.worker-send-base/v1",
                "event_class": "workspace_message_sent", "atomic_message_queue": True,
                "derived_admission_evidence": True, "authenticated_worker_ingress": False,
                "max_operations": 32, "max_content_bytes": 4096}

    @staticmethod
    def workspace_operator_send_contract() -> dict[str, Any]:
        """Dedicated local-host seam; installing browser authority is separate."""
        return {"schema": "summon.workspace.operator-send-base/v1",
                "event_class": "workspace_operator_message_sent", "atomic_message_queue": True,
                "exact_operation_lookup": True, "derived_admission_evidence": True,
                "authenticated_browser_ingress": False, "max_operations": 32,
                "max_content_bytes": 2048, "max_request_bytes": 4096}

    @staticmethod
    def supervisor_inbox_contract() -> dict[str, Any]:
        """Pure/core capability only; actual installed consumer is runtime-owned."""
        import _workspace_admission as admission
        return {"schema": "summon.workspace.supervisor-inbox-base/v1", "event_classes": sorted(admission.INBOX_EVENTS),
                "atomic_message_queue": True, "control_reserve": True, "consumer_authentication": False,
                "max_endpoints": 1, "max_messages": 32, "max_deliveries": 128, "max_content_bytes": 4096}

    @staticmethod
    def _obligation_bound(claim: Mapping[str, Any], task: Mapping[str, Any]) -> int:
        status = claim.get("status")
        if status == "active":
            # Reserve the complete reachable settlement path, not merely one
            # next terminal record.  The branches are mutually exclusive in
            # normal operation, but the sum is the deliberate finite safety
            # margin for cancellation, uncertainty, and explicit recovery.
            return (
                (0 if claim.get("cancel_requested") else _BOUND_CANCEL_REQUESTED)
                + (0 if claim.get("cancel_acknowledged") else _BOUND_CANCEL_ACKNOWLEDGED)
                + _BOUND_TASK_CANCELLED + _BOUND_CLAIM_INDETERMINATE
                + max(_BOUND_RETRY_AUTHORIZED, _BOUND_TASK_BLOCKED)
                + _BOUND_TASK_COMPLETED + _BOUND_TASK_FAILED + _BOUND_CLAIM_RELEASED
            )
        if status == "indeterminate" and not task.get("retry_authorized"):
            return max(_BOUND_RETRY_AUTHORIZED, _BOUND_TASK_BLOCKED)
        return 0

    @staticmethod
    def _workspace_after_record(state: Mapping[str, Any], record: Mapping[str, Any]):
        """Use canonical replay for candidate capacity, never a parallel lifecycle."""
        if record.get("event") not in {_WORKSPACE_TURN_EVENT, _WORKSPACE_WRAPPER_EVENT}:
            return copy.deepcopy(state.get("workspace"))
        import _workspace_admission as admission
        import _workspace_state as projection
        coordinator = state
        if record["workspace_event"].get("event") == admission.ATOMIC_SEND_EVENT:
            admission.validate_worker_send(
                record["workspace_event"], coordinator_state=state,
                workspace_state=state.get("workspace"), now_ms=record.get("observed_at_ms"))
        if record["workspace_event"].get("event") == admission.SUPERVISOR_SEND_EVENT:
            admission.validate_supervisor_send(record["workspace_event"], coordinator_state=state,
                workspace_state=state.get("workspace"), now_ms=record.get("observed_at_ms"))
        elif record["workspace_event"].get("event") == admission.OPERATOR_SEND_EVENT:
            admission.validate_operator_send(record["workspace_event"], coordinator_state=state,
                workspace_state=state.get("workspace"), now_ms=record.get("observed_at_ms"))
        elif record["workspace_event"].get("event") in admission.INBOX_EVENTS:
            admission.validate_inbox_command_event(record["workspace_event"], coordinator_state=state,
                workspace_state=state.get("workspace"), now_ms=record.get("observed_at_ms"))
        if record["event"] == _WORKSPACE_TURN_EVENT:
            _claim, coordinator = admission._prospective_claim(
                record["workspace_event"]["payload"]["claim"], state)
        return projection.apply_event(
            state.get("workspace"), record["workspace_event"],
            admission._coordinator_view(coordinator, state["run_id"],
                                        now_ms=record.get("observed_at_ms")))

    @staticmethod
    def _workspace_reserve(workspace: Mapping[str, Any] | None) -> int:
        """Reserve every remaining finite transition and actual proof slot.

        A nonterminal delivery also retains one uncertainty tail: a terminal
        disposition cannot erase unknown contact/spend/cleanup facts. That tail
        is released only by a legal clean disposition, never by task completion.
        """
        if not isinstance(workspace, Mapping):
            return 0
        deliveries = workspace.get("deliveries")
        if not isinstance(deliveries, Mapping):
            return 0
        import _workspace_admission as admission
        import _workspace_state as projection

        transition_count, evidence_count = projection.settlement_record_counts(workspace)
        if len(workspace["evidence"]) + evidence_count > projection.MAX_EVIDENCE:
            raise SwarmCoordinatorError("workspace evidence capacity cannot preserve settlement slots")
        if len(workspace["operations"]) + transition_count + evidence_count > projection.MAX_EVENTS:
            raise SwarmCoordinatorError("workspace event capacity cannot preserve settlement slots")
        total = transition_count * _BOUND_WORKSPACE_WRAPPER + evidence_count * _BOUND_WORKSPACE_EVIDENCE
        for delivery_id, delivery in deliveries.items():
            if not isinstance(delivery, Mapping):
                raise SwarmCorruptError("workspace delivery projection is malformed")
            try:
                obligations = admission.outstanding_obligations(
                    delivery.get("state"), delivery_id,
                    certainty=delivery.get("certainty"),
                    inherited_uncertainty=delivery.get("inherited_uncertainty", ()))
            except Exception as exc:  # noqa: BLE001 - capacity must fail closed
                raise SwarmCorruptError("workspace delivery obligation is malformed") from exc
            if obligations:
                total += _BOUND_WORKSPACE_WRAPPER
        return total

    @staticmethod
    def _economics_reservation_totals(
            state: Mapping[str, Any], record: Mapping[str, Any] | None = None) -> tuple[int, int]:
        """Return outstanding economics bytes and journal-slot reservations.

        Reservations are durable admission obligations.  They must survive an
        ordinary append and replay, and remain held until the typed budget
        consumption record settles the corresponding admission.  The optional
        candidate record is included because append admission checks the exact
        frozen record before it is written.  A candidate that is itself the
        settlement record is treated as consumed rather than double-counted.
        """
        admissions = state.get("admissions", {})
        if not isinstance(admissions, Mapping):
            raise SwarmCorruptError("economics admission projection is malformed")
        total_bytes = 0
        total_records = 0
        seen: set[str] = set()

        def add(admission_id: object, reservation: object) -> None:
            nonlocal total_bytes, total_records
            if not isinstance(admission_id, str):
                raise SwarmCorruptError("economics admission id is malformed")
            if not isinstance(reservation, Mapping):
                raise SwarmCorruptError("economics reservation projection is malformed")
            status = reservation.get("status")
            if status not in {"reserved", "consumed", "settled"}:
                raise SwarmCorruptError("economics reservation status is malformed")
            if status != "reserved":
                return
            max_bytes = reservation.get("max_settlement_bytes")
            max_records = reservation.get("max_records")
            if (type(max_bytes) is not int
                    or not MIN_ECONOMICS_SETTLEMENT_BYTES <= max_bytes <= 65_536
                    or type(max_records) is not int or not 1 <= max_records <= 64):
                raise SwarmCorruptError("economics reservation bounds are malformed")
            total_bytes += max_bytes
            total_records += max_records
            if total_bytes > MAX_JOURNAL_BYTES or total_records > MAX_JOURNAL_RECORDS:
                raise SwarmCoordinatorError("economics reservation exceeds journal capacity")
            seen.add(admission_id)

        for admission_id, admission in admissions.items():
            if not isinstance(admission, Mapping):
                raise SwarmCorruptError("economics admission is malformed")
            reservation = admission.get("economics_reservation")
            if reservation is not None:
                add(admission_id, reservation)

        if isinstance(record, Mapping):
            event = record.get("event")
            if event == _WORKSPACE_TURN_EVENT:
                workspace_event = record.get("workspace_event")
                payload = (workspace_event.get("payload")
                           if isinstance(workspace_event, Mapping) else None)
                reservation = (payload.get("economics_reservation")
                               if isinstance(payload, Mapping) else None)
                operation_id = (workspace_event.get("operation_key")
                                if isinstance(workspace_event, Mapping) else None)
                if isinstance(reservation, Mapping) and isinstance(operation_id, str) \
                        and operation_id not in seen:
                    add(operation_id, reservation)
            elif event == _WORKSPACE_TURN_BUDGET_CONSUMED:
                # Budget consumption is only a launch intent.  It does not
                # settle the physical attempt, so keep the reservation held.
                pass
            elif event == _WORKSPACE_TURN_ECONOMICS_SETTLED:
                # The reducer marks this admission settled on the projected
                # copy. Exclude it from the pre-append state reservation.
                admission_id = record.get("admission_id")
                admission = admissions.get(admission_id) if isinstance(admission_id, str) else None
                reservation = (admission.get("economics_reservation")
                               if isinstance(admission, Mapping) else None)
                if isinstance(admission_id, str) and isinstance(reservation, Mapping):
                    max_bytes = reservation.get("max_settlement_bytes")
                    max_records = reservation.get("max_records")
                    if (reservation.get("status") == "reserved"
                            and type(max_bytes) is int and type(max_records) is int):
                        total_bytes -= max_bytes
                        total_records -= max_records
                        seen.discard(admission_id)
        return max(0, total_bytes), max(0, total_records)

    def _reserve_after(self, state: dict[str, Any], record: Mapping[str, Any]) -> int:
        """Return bounded bytes required after admitting ``record``.

        The candidate itself is intentionally absent from this sum.  Its
        exact frozen length is the separate ``E`` term in ``U + E + reserve``.
        """
        claims = {claim_id: dict(claim)
                  for claim_id, claim in state.get("claims", {}).items()}
        tasks = {task_id: dict(task)
                 for task_id, task in state.get("tasks", {}).items()}
        event = record.get("event")
        claim_id = record.get("claim_id")
        task_id = record.get("task_id")
        if event in {"claim_granted", "claim_reclaimed"}:
            if isinstance(claim_id, str) and isinstance(task_id, str):
                claims[claim_id] = {
                    "claim_id": claim_id, "task_id": task_id, "status": "active",
                    "cancel_requested": False, "cancel_acknowledged": False,
                    "renewals": 0,
                }
        elif event == _WORKSPACE_TURN_EVENT:
            # Composite admission creates the claim and selected workspace
            # deliveries in one journal record.  Capacity must project the
            # fresh claim before calculating its settlement/renewal tail;
            # otherwise the append can consume the bytes needed to finish the
            # admitted turn.  The nested event has already passed the trusted
            # admission validator on the live path, so malformed direct calls
            # simply leave the existing state unchanged and fail at append.
            workspace_event = record.get("workspace_event")
            payload = (workspace_event.get("payload")
                       if isinstance(workspace_event, Mapping) else None)
            candidate = payload.get("claim") if isinstance(payload, Mapping) else None
            if isinstance(candidate, Mapping):
                candidate_claim_id = candidate.get("claim_id")
                candidate_task_id = candidate.get("task_id")
                if isinstance(candidate_claim_id, str) and isinstance(candidate_task_id, str):
                    claims[candidate_claim_id] = {
                        "claim_id": candidate_claim_id,
                        "task_id": candidate_task_id,
                        "status": "active",
                        "cancel_requested": False,
                        "cancel_acknowledged": False,
                        "renewals": 0,
                    }
        claim = claims.get(claim_id) if isinstance(claim_id, str) else None
        if event == "claim_renewed" and claim is not None:
            claim["renewals"] = min(MAX_ROUTINE_RENEWALS,
                                     int(claim.get("renewals", 0)) + 1)
        elif event == "cancel_requested" and claim is not None:
            claim["cancel_requested"] = True
        elif event == "cancel_acknowledged" and claim is not None:
            claim["cancel_acknowledged"] = True
        elif event in {"claim_released", "claim_indeterminate", "task_completed",
                       "task_failed", "task_cancelled", "task_blocked"} and claim is not None:
            claim["status"] = {
                "claim_released": "released", "claim_indeterminate": "indeterminate",
                "task_completed": "completed", "task_failed": "failed",
                "task_cancelled": "cancelled", "task_blocked": "blocked",
            }[event]
            if event in {"task_completed", "task_failed", "task_cancelled", "task_blocked",
                         "claim_indeterminate"}:
                task = tasks.setdefault(claim.get("task_id"), {})
                task["terminal"] = "indeterminate" if event == "claim_indeterminate" else claim["status"]
                if event in {"claim_indeterminate", "task_blocked"}:
                    task["uncertain_spend"] = True
        elif event == "retry_authorized" and isinstance(task_id, str):
            task = tasks.setdefault(task_id, {})
            task["retry_authorized"] = True
            task["terminal"] = None
        elif event == "run_closed":
            if self._workspace_reserve(state.get("workspace")):
                raise SwarmConflictError("cannot close with unresolved workspace delivery obligations")
            economics_bytes, economics_records = self._economics_reservation_totals(state, record)
            if economics_bytes or economics_records:
                raise SwarmConflictError("cannot close with unresolved economics reservations")
            return 0

        workspace_after = self._workspace_after_record(state, record)
        workspace_reserve = self._workspace_reserve(workspace_after)

        obligation = 0
        renewal_reserve = 0
        for claim_value in claims.values():
            task = tasks.get(claim_value.get("task_id"), {})
            obligation += self._obligation_bound(claim_value, task)
            if claim_value.get("status") == "active":
                remaining = max(0, MAX_ROUTINE_RENEWALS - int(claim_value.get("renewals", 0)))
                renewal_reserve += remaining * _BOUND_CLAIM_RENEWED
        close_reserve = 0 if state.get("closed") or event == "run_closed" else _RUN_CLOSE_BOUND
        economics_reserve, _economics_records = self._economics_reservation_totals(state, record)
        return (obligation + renewal_reserve + workspace_reserve + economics_reserve
                + close_reserve + _RECOVERY_HEADROOM)

    def _append(self, owner: Owner, record: Mapping[str, Any]) -> None:
        raise SwarmCoordinatorError(
            "direct coordinator append requires an admitted state snapshot")

    def _append_initial(self, owner: Owner, record: Mapping[str, Any]) -> None:
        """Append only the one creation record before a state exists.

        All later mutations must pass the loaded state through
        ``_append_with_state``.  Keeping this exceptional path named prevents
        workspace/future facades from accidentally opting out of reserve
        admission by calling the historical private ``_append`` seam.
        """
        self._append_with_state(owner, record, state=None, creation=True)

    def _append_with_state(self, owner: Owner, record: Mapping[str, Any], *,
                           state: dict[str, Any] | None, creation: bool = False,
                           before_append: Callable | None = None,
                           frozen: bytes | None = None) -> tuple[tuple[str, int, str], ...]:
        # Optional installed-host authorization conjunct, never a record field
        # or JSON capability. It must be nonblocking and perform no mutation,
        # source I/O or nested coordinator call. Existing callers are unchanged.
        if before_append is not None and (not callable(before_append) or type(state) is not dict):
            raise SwarmProtocolError("trusted final append constraint requires admitted state")
        self._fence_run_files()
        if not isinstance(owner.generation, int) or not 1 <= owner.generation <= MAX_GENERATION:
            raise SwarmCoordinatorError("owner generation is outside the bounded range")
        payload = dict(record)
        payload["generation"] = owner.generation
        if payload.get("event") == "artifact_published" and "sha256" in payload:
            payload["artifact_sha256"] = payload.pop("sha256")
        if state is not None and payload.get("event") in {
                _WORKSPACE_WRAPPER_EVENT, _WORKSPACE_TURN_EVENT, "run_closed"}:
            if state.get("closed"):
                raise SwarmConflictError("cannot append workspace state after close")
            # BASE validation is mandatory even if a subclass or private caller
            # skipped its preflight. Reserve and replay share this same reducer.
            SwarmCoordinator._apply_record(copy.deepcopy(state), payload)
        # Freeze once and admit against those exact bytes.  This prevents a
        # timestamp/serialization race between capacity accounting and the
        # owned write seam.  Settlement callers may pass the already checked
        # candidate so the capacity proof and durable append consume one byte
        # string rather than two independently stamped records.
        if frozen is None:
            frozen = encode_journal_record(payload, timestamp=time.time())
        elif type(frozen) is not bytes or not frozen.endswith(b"\n"):
            raise SwarmProtocolError("frozen journal record is invalid")
        _tagged, _torn, raw_map = self._strict_snapshot()
        if state is None and not creation:
            raise SwarmCoordinatorError(
                "coordinator append requires an admitted state snapshot")
        if creation:
            if payload.get("event") != "swarm_prepared" or _tagged or _torn or any(raw_map.values()):
                raise SwarmConflictError("creation append requires an empty swarm journal")
        elif state is not None:
            expected = state.get("_prefix_fingerprint")
            if expected is None or expected != self._prefix_fingerprint(raw_map):
                raise SwarmCorruptError("mutation state is stale relative to journal prefix")
        synced = self._sync_prefix(owner, raw_map)
        if (state is not None
                and state.get("_prefix_fingerprint") != self._prefix_fingerprint(synced)):
            raise SwarmCorruptError("mutation state changed during prefix sync")
        used = sum(len(raw) for raw in raw_map.values())
        reserve = self._reserve_after(state, payload) if state is not None else 0
        if used + len(frozen) + reserve > MAX_JOURNAL_BYTES:
            if state is not None and reserve:
                raise SwarmCoordinatorError(
                    "swarm journal capacity degraded: settlement reserve cannot be preserved")
            raise SwarmCoordinatorError("swarm journal limit reached")
        if state is not None:
            _economics_bytes, economics_records = self._economics_reservation_totals(state, payload)
            if len(_tagged) + 1 + economics_records > MAX_JOURNAL_RECORDS:
                raise SwarmCoordinatorError(
                    "swarm journal capacity degraded: economics record reserve cannot be preserved")
        expected_after = dict(raw_map)
        segment_name = f"journal-g{owner.generation}.jsonl"
        expected_after[segment_name] = expected_after.get(segment_name, b"") + frozen
        if before_append is not None and before_append(copy.deepcopy(state)) is not True:
            raise SwarmProtocolError("final append constraint refused")
        journal_append_encoded(self.run_dir, frozen, owner, expected_record=payload)
        _tagged_after, torn_after, raw_after = self._strict_snapshot()
        if torn_after:
            raise SwarmCorruptError("journal became torn after durable append")
        if raw_after != expected_after:
            raise SwarmCorruptError("journal prefix changed during append")
        return self._prefix_fingerprint(raw_after)

    @contextlib.contextmanager
    def _mutation(self, *, repair: bool = True):
        owner = self._acquire() if repair else self._acquire(repair=False)
        try:
            state, torn, raw_map = self._load_with_snapshot()
            if torn:
                raise SwarmCorruptError("swarm journal requires repair before mutation")
            synced = self._sync_prefix(owner, raw_map)
            if state.get("_prefix_fingerprint") != self._prefix_fingerprint(synced):
                raise SwarmCorruptError("loaded swarm state is not bound to synchronized prefix")
            yield owner, state, torn
        finally:
            release_owner(owner)

    def _check_frame(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_frame(encode_frame(frame))
        except (SwarmProtocolError, TypeError, ValueError) as exc:
            raise SwarmCoordinatorError(f"invalid swarm frame: {exc}") from exc
        if parsed["run_id"] != self.run_id:
            raise SwarmCoordinatorError("frame run id does not match coordinator")
        now = _now_ms(self.clock)
        sent = parsed["sent_at_ms"]
        if abs(now - sent) > MAX_CLOCK_SKEW_MS:
            raise SwarmCoordinatorError("frame timestamp is outside the replay window")
        return parsed

    @staticmethod
    def _response(status: str, **values: Any) -> dict[str, Any]:
        return {"status": status, **values}

    def _economics_settlement_capacity_bound(self, reservation: Mapping[str, Any],
                                             claim: Mapping[str, Any],
                                             selection: Mapping[str, Any],
                                             admission_id: str) -> int:
        """Bound every accepted terminal accounting shape before admission.

        A reservation is an admission-time obligation, not a promise that a
        later append will somehow fit.  Build the largest provider-neutral
        standard outcomes using the same private-record validator and journal
        encoder as settlement.  The actual settlement still rechecks its
        frozen bytes; this earlier bound prevents work from starting with an
        undersized fence.
        """
        if not isinstance(reservation, Mapping) or not isinstance(claim, Mapping):
            raise SwarmBudgetRefusal("economics_binding_required")
        estimate = {
            "method": "utf8_bytes_divided_by_4_ceiling",
            "represented_bytes": _ECONOMICS_MAX_REPRESENTED_BYTES,
            "estimated_tokens": _ECONOMICS_MAX_ESTIMATED_TOKENS,
            "scope": _ECONOMICS_SCOPE,
            "boundary": _ECONOMICS_BOUNDARY,
            "complete_provider_input": False,
            "outside_scope": list(_ECONOMICS_OUTSIDE_SCOPE),
            "material_sha256": reservation.get("material_sha256"),
        }
        usage = {name: _ECONOMICS_BOUND_METRIC
                 for name in _submission_accounting.METRICS}
        cases = (
            ("not_submitted", "cancelled", {
                "provider_contacted": False, "usage": usage,
                "usage_observation": {"source": "x" * _ECONOMICS_MAX_PROVENANCE_CHARS,
                                       "scope": "last_step_snapshot"},
            }),
            ("submitted", "success", {
                "provider_contacted": True, "usage": usage,
                "usage_observation": {"source": "x" * _ECONOMICS_MAX_PROVENANCE_CHARS,
                                       "scope": "last_step_snapshot"},
            }),
            ("submitted", "cancelled", {"provider_contacted": True}),
            ("indeterminate", "cancelled", {"provider_contacted": True}),
        )
        # The validator's own wrapper check is deliberately bypassed only for
        # this measurement; the real reservation is compared to the resulting
        # encoded record below.
        probe_reservation = dict(reservation)
        probe_reservation["max_settlement_bytes"] = 65_536
        timestamp_padding = _economics_timestamp_padding(_BOUND_TIMESTAMP)
        bound = 0
        for submission_state, result_status, envelope in cases:
            accounting = _submission_accounting.private_record(
                attempt_id=str(reservation.get("attempt_id")),
                attempt_kind=_ECONOMICS_BOUND_CONTROL_STRING * _ECONOMICS_MAX_ATTEMPT_KIND_CHARS,
                attempt_ordinal=selection["attempt"],
                parent_attempt_id=_ECONOMICS_BOUND_ATTEMPT_PARENT,
                request_sha256=claim["request_sha256"],
                estimate=estimate, envelope=envelope,
                submission_state=submission_state)
            # These values are all admitted by the finite producer contract.
            # Keep the probe at the largest accepted representation even when
            # a real provider omits usage or reports only a launch boundary.
            accounting["reported"]["provenance"] = (
                _ECONOMICS_BOUND_CONTROL_STRING * _ECONOMICS_MAX_PROVENANCE_CHARS)
            accounting["reported"]["observation_scope"] = "last_step_snapshot"
            if envelope.get("usage") is None:
                accounting["reported"]["field_state"] = {
                    name: "malformed" for name in _submission_accounting.METRICS
                }
                accounting["reported"]["completeness"] = "malformed"
            else:
                # Numeric maxima are only legal for reported fields.  Keep a
                # separate malformed/unknown probe below for the no-usage
                # shape so the capacity bound covers both states without
                # admitting contradictory per-field records.
                accounting["reported"]["field_state"] = {
                    name: "reported" for name in _submission_accounting.METRICS
                }
                accounting["reported"]["completeness"] = "complete"
            accounting["contact"]["local_process_created"] = False
            accounting["contact"]["evidence"] = "launch_boundary_only"
            if envelope.get("usage") is None:
                accounting["reported"]["metrics"] = {
                    name: None for name in _submission_accounting.METRICS
                }
            checked, accounting_sha = _validate_economics_accounting(
                accounting, claim=claim, selection=selection,
                reservation=probe_reservation,
                owner_generation=MAX_GENERATION,
                submission_state=submission_state, result_status=result_status)
            frame = make_frame(
                "claim", run_id=self.run_id,
                message_id="economics-" + ("x" * 32),
                sent_at_ms=_now_ms(self.clock),
                payload={"task_id": claim["task_id"], "claim_id": claim["claim_id"],
                         "attempt": claim["attempt"],
                         "lease_generation": claim["lease_generation"],
                         "lease_expires_at_ms": claim["lease_expires_at_ms"],
                         "request_sha256": claim["request_sha256"]})
            response = self._response("settled", admission_id=admission_id,
                                      claim_id=claim["claim_id"], provider_contacted=False,
                                      state_mutated=True)
            record = {
                "event": _WORKSPACE_TURN_ECONOMICS_SETTLED,
                "admission_id": admission_id, "claim_id": claim["claim_id"],
                "result_sha256": accounting_sha, "accounting_sha256": accounting_sha,
                "accounting": checked, "owner_generation": MAX_GENERATION,
                "attempt_id": reservation["attempt_id"],
                "payload_sha256": reservation["payload_sha256"],
                "selection_sha256": reservation["selection_sha256"],
                "policy_sha256": reservation["policy_sha256"],
                "submission_state": submission_state, "result_status": result_status,
            }
            candidate = dict(record, generation=MAX_GENERATION,
                             message_id=frame["message_id"],
                             frame_sha256=frame_sha256(frame), response=response)
            bound = max(
                bound,
                len(encode_journal_record(candidate, timestamp=_BOUND_TIMESTAMP))
                + timestamp_padding,
            )
        return bound

    def _idempotent(self, state: dict[str, Any], frame: Mapping[str, Any]) -> dict[str, Any] | None:
        message_id = frame["message_id"]
        existing = state["seen"].get(message_id)
        if existing is None:
            return None
        if existing.get("frame_sha256") != frame_sha256(frame):
            raise SwarmConflictError("message id was reused for a different frame")
        response = existing.get("response")
        if not isinstance(response, dict):
            raise SwarmCorruptError("journaled idempotency response is invalid")
        return dict(response)

    @staticmethod
    def workspace_budget_preflight(*, policy: Mapping[str, Any],
                                   snapshot: Mapping[str, Any],
                                   request: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate explicit detached budget facts without coordinator I/O.

        This is an observational seam only.  The returned decision is not
        bound to a live message, source grant, journal prefix, or execution
        slot and therefore cannot authorize an admission by itself.
        """
        import _workspace_budget as budget
        try:
            result = budget.evaluate(policy, snapshot, request)
        except budget.WorkspaceBudgetError as exc:
            raise SwarmBudgetRefusal("budget_input_invalid") from exc
        return {"phase": "observational", **copy.deepcopy(result),
                "result_sha256": budget.result_sha256(result)}

    def workspace_budget_snapshot(self, *, state=None, raw_map=None, records=None,
                                  now_ms: int | None = None,
                                  exclude_message_id: str | None = None,
                                  preferred_delivery_ids: set[str] | None = None) -> dict[str, Any]:
        """Derive admission facts from the coordinator's authoritative state.

        Callers may still use detached snapshots in the low-level budget
        module, but production workspace admission should obtain its facts here
        so a caller cannot substitute a stale active-slot or message view.
        """
        import _workspace_budget as budget

        if state is None or raw_map is None:
            state, torn, raw_map = self._load_with_snapshot()
        else:
            torn = bool(state.get("torn_tail"))
        if torn:
            raise SwarmBudgetRefusal("budget_binding_required")
        workspace = state.get("workspace")
        if not isinstance(workspace, Mapping):
            raise SwarmBudgetRefusal("budget_binding_required")
        deliveries = workspace.get("deliveries")
        inbox_deliveries = workspace.get("inbox_deliveries")
        messages = workspace.get("messages")
        if isinstance(deliveries, Mapping) and isinstance(messages, Mapping):
            inbox_deliveries = inbox_deliveries if isinstance(inbox_deliveries, Mapping) else {}
        else:
            raise SwarmBudgetRefusal("budget_binding_required")
        if records is None:
            records, records_torn, records_raw = self._read_records_snapshot()
            if records_torn or records_raw != raw_map:
                raise SwarmBudgetRefusal("budget_binding_required")
        if not isinstance(records, list):
            raise SwarmBudgetRefusal("budget_binding_required")
        if preferred_delivery_ids is not None and (
                type(preferred_delivery_ids) is not set
                or any(type(item) is not str for item in preferred_delivery_ids)):
            raise SwarmBudgetRefusal("budget_binding_required")
        captured_now = _now_ms(self.clock) if now_ms is None else now_ms
        if type(captured_now) is not int or captured_now < 1:
            raise SwarmBudgetRefusal("budget_binding_required")
        by_message = {}
        inbox_message_ids = {
            item.get("message_id") for item in inbox_deliveries.values()
            if isinstance(item, Mapping) and isinstance(item.get("message_id"), str)
        }
        pending_states = {"queued", "held", "offered", "included_in_attempt"}

        def delivery_priority(delivery):
            """Prefer the current recoverable delivery for a message id.

            Recovery successors intentionally retain the parent's message id.
            A terminal/held parent must not hide a newly queued or offered
            child from the authoritative budget projection.
            """
            state_name = delivery.get("state") if isinstance(delivery, Mapping) else None
            return 1 if state_name in pending_states else 0

        for delivery in (*deliveries.values(), *inbox_deliveries.values()):
            if not isinstance(delivery, Mapping):
                continue
            message_id = delivery.get("message_id")
            if isinstance(message_id, str):
                prior = by_message.get(message_id)
                preferred = delivery.get("delivery_id") in (preferred_delivery_ids or set())
                prior_preferred = (isinstance(prior, Mapping)
                                   and prior.get("delivery_id") in (preferred_delivery_ids or set()))
                if (prior is None or preferred and not prior_preferred
                        or (preferred == prior_preferred
                            and delivery_priority(delivery) > delivery_priority(prior))):
                    by_message[message_id] = delivery
        pending = []
        usage = {}
        for message_id, message in messages.items():
            delivery = by_message.get(message_id)
            if not isinstance(message, Mapping) or not isinstance(delivery, Mapping):
                continue
            content = message.get("content")
            stream_id = message.get("stream_id")
            sequence = message.get("sequence")
            if (not isinstance(content, Mapping) or not isinstance(stream_id, str)
                    or type(sequence) is not int):
                raise SwarmBudgetRefusal("budget_binding_required")
            bytes_count = content.get("utf8_bytes")
            if type(bytes_count) is not int or bytes_count < 1:
                raise SwarmBudgetRefusal("budget_binding_required")
            state_name = delivery.get("state")
            if state_name in {"queued", "held", "offered", "included_in_attempt"}:
                item = {"message_id": message_id, "stream_id": stream_id,
                        "sequence": sequence, "bytes": bytes_count,
                        "state": state_name}
                if message_id in inbox_message_ids:
                    item["source"] = "inbox"
                pending.append(item)
            else:
                item = usage.setdefault(stream_id, {"messages": 0, "bytes": 0})
                item["messages"] += 1
                item["bytes"] += bytes_count
        rate_events = []
        for record in records:
            nested = record.get("workspace_event") if isinstance(record, Mapping) else None
            if not isinstance(nested, Mapping):
                continue
            kind = nested.get("event")
            payload = nested.get("payload")
            message = payload.get("message") if isinstance(payload, Mapping) else None
            content = message.get("content") if isinstance(message, Mapping) else None
            observed = record.get("observed_at_ms")
            if (kind not in {"workspace_message_admitted", "workspace_message_sent",
                             "workspace_operator_message_sent", "workspace_supervisor_message_sent"}
                    or type(observed) is not int or not isinstance(content, Mapping)
                    or type(content.get("utf8_bytes")) is not int):
                continue
            if exclude_message_id is not None and isinstance(message, Mapping) and message.get("message_id") == exclude_message_id:
                continue
            rate_events.append({"at_ms": observed, "messages": 1,
                                "bytes": content["utf8_bytes"]})
        snapshot = {
            "schema": budget.SNAPSHOT_SCHEMA,
            "now_ms": captured_now,
            "journal_used_bytes": sum(len(raw) for raw in raw_map.values()),
            "active_execution_slots": sum(
                1 for claim in state.get("claims", {}).values()
                if isinstance(claim, Mapping) and claim.get("status") == "active"),
            "rate_events": rate_events,
            "pending_messages": pending,
            "stream_usage": usage,
        }
        try:
            return budget.validate_snapshot(snapshot)
        except budget.WorkspaceBudgetError as exc:
            raise SwarmBudgetRefusal("budget_binding_required") from exc

    def workspace_budget_decision(self, *, policy: Mapping[str, Any],
                                  snapshot: Mapping[str, Any] | None = None,
                                  request: Mapping[str, Any],
                                  require_authoritative: bool = False) -> dict[str, Any]:
        """Create a budget decision bound to this coordinator's current prefix."""
        import _workspace_budget as budget

        state, torn, raw_map = self._load_with_snapshot()
        if torn:
            raise SwarmBudgetRefusal("budget_binding_required")
        workspace = state.get("workspace")
        if not isinstance(workspace, Mapping) or type(workspace.get("revision")) is not int:
            raise SwarmBudgetRefusal("budget_binding_required")
        authoritative = None
        if isinstance(workspace, Mapping):
            try:
                authoritative = self.workspace_budget_snapshot(state=state, raw_map=raw_map)
            except SwarmBudgetRefusal:
                if require_authoritative:
                    raise
                authoritative = None
        # A real workspace never accepts a caller-supplied view that disagrees
        # with the coordinator.  Detached snapshots remain supported for the
        # provider-free unit fixtures that have no workspace projection.
        if snapshot is None:
            if authoritative is None:
                raise SwarmBudgetRefusal("budget_binding_required")
            snapshot = authoritative
        elif require_authoritative:
            if authoritative is None or dict(snapshot) != authoritative:
                raise SwarmBudgetRefusal("budget_binding_required")
        result = self.workspace_budget_preflight(policy=policy, snapshot=snapshot, request=request)
        decision = budget.bind_decision(
            policy=policy, snapshot=snapshot, request=request,
            result={key: value for key, value in result.items() if key not in {"phase", "result_sha256"}},
            journal_revision=workspace["revision"],
            journal_prefix_sha256=self._prefix_digest(raw_map),
        )
        return {"phase": "bound", **decision}

    def consume_supervisor_offer_budget(self, *, admission_id: str,
                                        delivery_id: str, offer_id: str,
                                        offer_payload: Mapping[str, Any],
                                        binding: Mapping[str, Any],
                                        policy: Mapping[str, Any],
                                        request: Mapping[str, Any]) -> dict[str, Any]:
        """Durably consume one control/message budget before offer I/O.

        ``OwnedSupervisorConsumer`` does not consume an execution slot, but a
        physical offer still spends bounded journal/control capacity and may
        become uncertain once the pipe write starts.  This event is therefore
        persisted under the coordinator owner before ``send_offer``.  A fresh
        host sees the consumed record and refuses a duplicate exposure.
        """
        import _workspace_budget as budget

        admission_id = _id(admission_id, "supervisor offer admission id")
        delivery_id = _id(delivery_id, "supervisor offer delivery id")
        offer_id = _id(offer_id, "supervisor offer id")
        if not isinstance(offer_payload, Mapping) or not isinstance(binding, Mapping):
            raise SwarmBudgetRefusal("budget_binding_required")
        try:
            payload_sha = hashlib.sha256(
                _canonical_json(dict(offer_payload), "supervisor offer payload",
                                maximum=MAX_JOURNAL_BYTES)).hexdigest()
            binding_sha = hashlib.sha256(
                _canonical_json(dict(binding), "supervisor offer binding",
                                maximum=MAX_JOURNAL_BYTES)).hexdigest()
            checked_policy = budget.validate_policy(policy)
            checked_request = budget.validate_request(request)
            if (checked_request["kind"] != "message"
                    or len(checked_request["selected_message_ids"]) != 1
                    or checked_request["execution_slots_requested"] != 0
                    or checked_request["journal_bytes"] < 1):
                raise budget.WorkspaceBudgetError("supervisor offer needs a nonempty zero-slot control request")
        except (budget.WorkspaceBudgetError, TypeError, ValueError):
            raise SwarmBudgetRefusal("budget_binding_required") from None
        with self._mutation() as (owner, state, _torn):
            existing = state.setdefault("supervisor_budget_admissions", {}).get(admission_id)
            if existing is not None:
                if (existing.get("delivery_id") != delivery_id
                        or existing.get("offer_id") != offer_id
                        or existing.get("offer_payload_sha256") != payload_sha
                        or existing.get("binding_sha256") != binding_sha):
                    raise SwarmConflictError("supervisor offer budget operation key was reused")
                raise SwarmBudgetRefusal("budget_binding_required")
            workspace = state.get("workspace")
            delivery = (workspace.get("inbox_deliveries", {}).get(delivery_id)
                        if isinstance(workspace, Mapping) else None)
            offer = delivery.get("offer") if isinstance(delivery, Mapping) else None
            endpoint = (workspace.get("supervisor_endpoints", {}).get(binding.get("endpoint_id"))
                        if isinstance(workspace, Mapping) else None)
            message = (workspace.get("messages", {}).get(delivery.get("message_id"))
                       if isinstance(workspace, Mapping) and isinstance(delivery, Mapping) else None)
            content = message.get("content") if isinstance(message, Mapping) else None
            now_ms = _now_ms(self.clock)
            if (not isinstance(delivery, Mapping) or delivery.get("state") != "offered"
                    or not isinstance(offer, Mapping) or offer.get("offer_id") != offer_id
                    or offer.get("consumer") != dict(binding)
                    or offer.get("message_id") != checked_request["selected_message_ids"][0]
                    or not isinstance(endpoint, Mapping)
                    or endpoint.get("status") != "active"
                    or endpoint.get("owner") != {"instance_id": binding.get("owner_instance_id"),
                                                   "epoch": binding.get("epoch")}
                    or endpoint.get("receiver_grant_ref") != delivery.get("grant_ref")
                    or endpoint.get("lease_expires_at_ms", 0) <= now_ms
                    or delivery.get("recipient") != {
                        "kind": "supervisor_inbox", "endpoint_id": binding.get("endpoint_id"),
                        "owner_instance_id": binding.get("owner_instance_id"),
                        "epoch": binding.get("epoch")}
                    or not isinstance(content, Mapping)
                    or offer.get("content_sha256") != content.get("sha256")
                    or offer.get("content_utf8_bytes") != content.get("utf8_bytes")):
                raise SwarmBudgetRefusal("budget_binding_required")
            records, torn, raw_map = self._read_records_snapshot()
            if (torn or state.get("_prefix_fingerprint") != self._prefix_fingerprint(raw_map)):
                raise SwarmBudgetRefusal("budget_binding_required")
            try:
                snapshot = self.workspace_budget_snapshot(
                    state=state, raw_map=raw_map, records=records,
                    exclude_message_id=delivery["message_id"],
                    preferred_delivery_ids={delivery_id})
                result = budget.evaluate(checked_policy, snapshot, checked_request)
                if result["status"] != "accepted" or result["execution_status"] != "not_requested":
                    raise SwarmBudgetRefusal("budget_decision_blocked", result=result)
                prefix_digest = self._prefix_digest(raw_map)
                decision = budget.bind_decision(
                    policy=checked_policy, snapshot=snapshot, request=checked_request,
                    result=result, journal_revision=workspace["revision"],
                    journal_prefix_sha256=prefix_digest)
                budget_admission = {
                    "schema": _SUPERVISOR_BUDGET_SCHEMA, "status": "consumed",
                    "policy": checked_policy, "snapshot": snapshot,
                    "request": checked_request, "decision": decision,
                    "request_identity_sha256": budget.request_identity_sha256(checked_request),
                    "facts_identity_sha256": budget.facts_identity_sha256(
                        checked_policy, snapshot, checked_request),
                    "result_sha256": budget.result_sha256(result),
                    "journal_revision": workspace["revision"],
                    "journal_prefix_sha256": prefix_digest,
                }
            except SwarmBudgetRefusal:
                raise
            except (budget.WorkspaceBudgetError, KeyError, TypeError, ValueError):
                raise SwarmBudgetRefusal("budget_binding_required") from None
            frame = make_frame(
                "claim", run_id=self.run_id, message_id=_message_id("supervisor-budget"),
                sent_at_ms=_now_ms(self.clock),
                payload={"task_id": "supervisor", "claim_id": "supervisor-" + admission_id[:96],
                         "attempt": 1, "lease_generation": 1,
                         "lease_expires_at_ms": _now_ms(self.clock) + 1,
                         "request_sha256": payload_sha})
            response = self._response("consumed", admission_id=admission_id,
                                      delivery_id=delivery_id, offer_id=offer_id,
                                      provider_contacted=False, state_mutated=True)
            record = {
                "event": _SUPERVISOR_OFFER_BUDGET_CONSUMED,
                "admission_id": admission_id, "delivery_id": delivery_id,
                "offer_id": offer_id, "offer_payload_sha256": payload_sha,
                "binding_sha256": binding_sha,
                "budget_admission": budget_admission,
            }
            self._remembered_append(owner, state, frame, response, record)
            return {"status": "accepted", "admission_id": admission_id,
                    "delivery_id": delivery_id, "offer_id": offer_id,
                    "provider_contacted": False, "state_mutated": True,
                    "budget_admission": copy.deepcopy(budget_admission)}

    def consume_workspace_turn_budget(self, admission_id: str, *, decision: Mapping[str, Any],
                                      policy: Mapping[str, Any], snapshot: Mapping[str, Any],
                                      request: Mapping[str, Any], binding: Mapping[str, Any],
                                      planned_work: Mapping[str, Any],
                                      resolve_grant: Callable | None = None) -> dict[str, Any]:
        """Consume one persisted turn reservation before child creation.

        Consumption is a journaled launch intent.  A second coordinator or a
        restarted process therefore observes ``consumed`` and refuses before
        any channel/process exists.  Unknown physical outcomes remain
        consumed and must be recovered explicitly; this method never refunds.
        """
        import _workspace_budget as budget
        import _workspace_transport as transport

        admission_id = _id(admission_id, "admission id")
        transport._binding(binding)
        transport._payload("fixture_work", planned_work)
        with self._mutation() as (owner, state, _torn):
            admission = state.get("admissions", {}).get(admission_id)
            if not isinstance(admission, Mapping):
                raise SwarmBudgetRefusal("budget_binding_required")
            stored = admission.get("budget_admission")
            if not isinstance(stored, Mapping) or stored.get("status") != "reserved":
                raise SwarmBudgetRefusal("budget_binding_required")
            selected_ids = list(admission["workspace_event"]["payload"]["selection"]["selected_message_ids"])
            checked_stored = _validate_budget_admission(
                stored, selected_message_ids=selected_ids,
                journal_revision=stored.get("journal_revision"), journal_prefix_sha256=None)
            try:
                if (budget.validate_policy(policy) != checked_stored["policy"]
                        or budget.validate_snapshot(snapshot) != checked_stored["snapshot"]
                        or budget.validate_request(request) != checked_stored["request"]):
                    raise budget.WorkspaceBudgetError("budget facts differ")
                supplied_decision = copy.deepcopy(dict(decision))
                if supplied_decision.get("phase") == "bound":
                    supplied_decision.pop("phase")
                if supplied_decision != checked_stored["decision"]:
                    raise budget.WorkspaceBudgetError("budget decision differs")
            except (budget.WorkspaceBudgetError, TypeError, ValueError):
                raise SwarmBudgetRefusal("budget_binding_required") from None
            event = admission["workspace_event"]
            payload = event["payload"]
            claim_id = admission["claim_id"]
            current_claim = state.get("claims", {}).get(claim_id)
            worker_state = state.get("workers", {}).get(payload["claim"]["worker_id"])
            if (not isinstance(current_claim, Mapping)
                    or current_claim.get("status") != "active"
                    or current_claim.get("cancel_requested") is not False
                    or current_claim.get("lease_expires_at_ms", 0) <= _now_ms(self.clock)
                    or not isinstance(worker_state, Mapping)
                    or worker_state.get("worker_instance_id") != payload["recipient"]["instance_id"]):
                raise SwarmBudgetRefusal("budget_binding_required")
            for delivery_id in payload["selected_delivery_ids"]:
                delivery = state["workspace"]["deliveries"].get(delivery_id)
                if (not isinstance(delivery, Mapping)
                        or delivery.get("state") != "included_in_attempt"
                        or delivery.get("selection") != payload["selection"]):
                    raise SwarmBudgetRefusal("budget_binding_required")
            # A durable turn admission is qualified only by a trusted current
            # grant resolver.  The detached adapter path remains explicitly
            # unqualified, but a missing resolver here must never turn a stale
            # or revoked grant into a launch authorization.
            if not callable(resolve_grant):
                raise SwarmBudgetRefusal("budget_binding_required")
            try:
                current_grant = resolve_grant(
                    copy.deepcopy(state), copy.deepcopy(state["workspace"]),
                    copy.deepcopy(payload["grant_ref"]))
            except Exception:
                raise SwarmBudgetRefusal("budget_binding_required") from None
            if (not isinstance(current_grant, Mapping)
                    or current_grant.get("revoked") is not False
                    or current_grant.get("recipient") != payload["recipient"]):
                raise SwarmBudgetRefusal("budget_binding_required")
            expected_binding = {
                "workspace_id": event["workspace_id"],
                "run_id": self.run_id, "instance_id": payload["recipient"]["instance_id"],
                "epoch": payload["recipient"]["epoch"], "task_id": payload["claim"]["task_id"],
                "grant_id": payload["grant_ref"]["id"], "grant_sha256": payload["grant_ref"]["sha256"],
            }
            if dict(binding) != expected_binding:
                raise SwarmBudgetRefusal("budget_binding_required")
            if (planned_work.get("attempt_id") != payload["claim"]["claim_id"]
                    or planned_work.get("message_id") != selected_ids[0]
                    or planned_work.get("context_sha256") != payload["selection"]["context_sha256"]):
                raise SwarmBudgetRefusal("budget_binding_required")
            binding_sha = hashlib.sha256(_canonical_json(dict(binding), "transport binding")).hexdigest()
            planned_sha = hashlib.sha256(_canonical_json(dict(planned_work), "planned work")).hexdigest()
            now = _now_ms(self.clock)
            frame = make_frame("claim", run_id=self.run_id,
                               message_id=_message_id("budget"), sent_at_ms=now,
                               payload={"task_id": payload["claim"]["task_id"],
                                        "claim_id": admission["claim_id"],
                                        "attempt": payload["claim"]["attempt"],
                                        "lease_generation": payload["claim"]["lease_generation"],
                                        "lease_expires_at_ms": current_claim["lease_expires_at_ms"],
                                        "request_sha256": payload["claim"]["request_sha256"]})
            response = self._response("consumed", admission_id=admission_id,
                                      claim_id=admission["claim_id"])
            record = {
                "event": _WORKSPACE_TURN_BUDGET_CONSUMED,
                "admission_id": admission_id, "claim_id": admission["claim_id"],
                "request_identity_sha256": checked_stored["request_identity_sha256"],
                "facts_identity_sha256": checked_stored["facts_identity_sha256"],
                "result_sha256": checked_stored["result_sha256"],
                "journal_revision": checked_stored["journal_revision"],
                "journal_prefix_sha256": checked_stored["journal_prefix_sha256"],
                "binding_sha256": binding_sha, "planned_work_sha256": planned_sha,
            }
            self._remembered_append(owner, state, frame, response, record)
            return {
                "schema": transport.TRANSPORT_ADMISSION_SCHEMA,
                "result": copy.deepcopy(checked_stored["decision"]["result"]),
                "journal_revision": checked_stored["journal_revision"],
                "journal_prefix_sha256": checked_stored["journal_prefix_sha256"],
                "provider_contacted": False, "state_mutated": True,
            }

    def settle_workspace_turn_economics(self, admission_id: str, *,
                                        accounting_record: Mapping[str, Any],
                                        submission_state: str,
                                        result_status: str,
                                        result_sha256: str | None = None) -> dict[str, Any]:
        """Release one turn's storage reservation after a typed result.

        Budget consumption is only a pre-launch intent and deliberately does
        not release the economics fence. This separate sole-writer record
        binds the final private economics/result digest and outcome state,
        including an explicit not_submitted result when no remote request was
        accepted. It never infers provider spend from a process launch.
        """
        admission_id = _id(admission_id, "admission id")
        if not isinstance(accounting_record, Mapping):
            raise SwarmBudgetRefusal("economics_accounting_required")
        with self._mutation() as (owner, state, _torn):
            admission = state.get("admissions", {}).get(admission_id)
            if not isinstance(admission, Mapping):
                raise SwarmBudgetRefusal("economics_binding_required")
            reservation = admission.get("economics_reservation")
            if not isinstance(reservation, Mapping):
                raise SwarmBudgetRefusal("economics_binding_required")
            claim_id = _id(admission.get("claim_id"), "claim id")
            claim = state.get("claims", {}).get(claim_id)
            if not isinstance(claim, Mapping):
                raise SwarmBudgetRefusal("economics_binding_required")
            workspace_event = admission.get("workspace_event")
            payload = (workspace_event.get("payload")
                       if isinstance(workspace_event, Mapping) else None)
            selection = payload.get("selection") if isinstance(payload, Mapping) else None
            if not isinstance(selection, Mapping):
                raise SwarmBudgetRefusal("economics_binding_required")
            if reservation.get("status") == "settled":
                stored = admission.get("economics_settlement")
                if (not isinstance(stored, Mapping)
                        or stored.get("submission_state") != submission_state
                        or stored.get("result_status") != result_status):
                    raise SwarmConflictError("economics settlement differs")
                accounting, accounting_sha = _validate_economics_accounting(
                    dict(accounting_record), claim=claim, selection=selection,
                    reservation=reservation, owner_generation=claim.get("lease_generation"),
                    submission_state=stored["submission_state"], result_status=stored["result_status"])
                if result_sha256 is not None and result_sha256 != accounting_sha:
                    raise SwarmConflictError("economics result digest differs")
                if accounting_sha != stored.get("accounting_sha256"):
                    raise SwarmConflictError("economics settlement differs")
                return {
                    "schema": _ECONOMICS_SETTLEMENT_SCHEMA,
                    "status": "settled", "admission_id": admission_id,
                    "claim_id": claim_id, "submission_state": submission_state,
                    "result_status": result_status, "result_sha256": accounting_sha,
                    "accounting_sha256": accounting_sha, "duplicate": True,
                    "provider_contacted": False, "state_mutated": False,
                }
            accounting, accounting_sha = _validate_economics_accounting(
                dict(accounting_record), claim=claim, selection=selection,
                reservation=reservation, owner_generation=claim.get("lease_generation"),
                submission_state=submission_state, result_status=result_status)
            if result_sha256 is not None and result_sha256 != accounting_sha:
                raise SwarmConflictError("economics result digest differs")
            if reservation.get("status") != "reserved":
                raise SwarmBudgetRefusal("economics_binding_required")
            frame = make_frame(
                "claim", run_id=self.run_id, message_id=_message_id("economics"),
                sent_at_ms=_now_ms(self.clock),
                payload={"task_id": claim["task_id"], "claim_id": claim_id,
                         "attempt": claim["attempt"],
                         "lease_generation": claim["lease_generation"],
                         "lease_expires_at_ms": claim["lease_expires_at_ms"],
                         "request_sha256": claim["request_sha256"]})
            response = self._response("settled", admission_id=admission_id,
                                      claim_id=claim_id, provider_contacted=False,
                                      state_mutated=True)
            record = {
                "event": _WORKSPACE_TURN_ECONOMICS_SETTLED,
                "admission_id": admission_id, "claim_id": claim_id,
                "result_sha256": accounting_sha,
                "accounting_sha256": accounting_sha,
                "accounting": accounting,
                "owner_generation": claim["lease_generation"],
                "attempt_id": reservation["attempt_id"],
                "payload_sha256": reservation["payload_sha256"],
                "selection_sha256": reservation["selection_sha256"],
                "policy_sha256": reservation["policy_sha256"],
                "submission_state": submission_state,
                "result_status": result_status,
            }
            # The reservation covers the actual frozen journal settlement
            # record, not only the nested accounting wrapper.  Check the exact
            # encoded candidate before the sole-writer append so capacity cannot
            # be released for a record that would not fit its reservation.
            candidate = dict(record)
            candidate.update({"generation": owner.generation,
                              "message_id": frame["message_id"],
                              "frame_sha256": frame_sha256(frame),
                              "response": response})
            try:
                encoded_candidate = encode_journal_record(candidate, timestamp=time.time())
            except (OSError, TypeError, ValueError, SwarmProtocolError) as exc:
                raise SwarmBudgetRefusal("economics_settlement_capacity_insufficient") from exc
            if len(encoded_candidate) > reservation["max_settlement_bytes"]:
                raise SwarmBudgetRefusal("economics_settlement_capacity_insufficient")
            self._remembered_append(owner, state, frame, response, record,
                                    frozen=encoded_candidate)
            return {
                "schema": _ECONOMICS_SETTLEMENT_SCHEMA,
                "status": "settled", "admission_id": admission_id,
                "claim_id": claim_id, "submission_state": submission_state,
                "result_status": result_status, "result_sha256": accounting_sha,
                "accounting_sha256": accounting_sha, "duplicate": False,
                "provider_contacted": False,
                "state_mutated": True,
            }

    def workspace_transport_admission(self, *, decision: Mapping[str, Any],
                                      policy: Mapping[str, Any],
                                      snapshot: Mapping[str, Any] | None = None,
                                      request: Mapping[str, Any],
                                      binding: Mapping[str, Any],
                                      planned_work: Mapping[str, Any],
                                      turn_admission_id: str | None = None,
                                      resolve_grant: Callable | None = None):
        """Create the one explicit pre-bootstrap transport admission gate.

        The returned adapter re-reads this coordinator's current journal state
        immediately before an owned worker is created.  It is deliberately
        opt-in; legacy ``OwnedFakeWorker`` construction remains unqualified.
        """
        import _workspace_transport as transport

        if snapshot is None:
            snapshot = self.workspace_budget_snapshot()

        def consume(*, decision, policy, snapshot, request, binding=None, planned_work=None):
            import _workspace_budget as budget
            if turn_admission_id is not None:
                if binding is None or planned_work is None:
                    raise SwarmBudgetRefusal("budget_binding_required")
                return self.consume_workspace_turn_budget(
                    turn_admission_id, decision=decision, policy=policy,
                    snapshot=snapshot, request=request, binding=binding,
                    planned_work=planned_work, resolve_grant=resolve_grant)
            state, torn, raw_map = self._load_with_snapshot()
            workspace = state.get("workspace")
            if (torn or not isinstance(workspace, Mapping)
                    or type(workspace.get("revision")) is not int):
                raise SwarmBudgetRefusal("budget_binding_required")
            try:
                current_prefix = self._prefix_digest(raw_map)
                result = budget.consume_bound_decision(
                    decision, policy=policy, snapshot=snapshot, request=request,
                    journal_revision=workspace["revision"],
                    journal_prefix_sha256=current_prefix)
                reservation_key = (decision["facts_identity_sha256"],
                                   decision["request_identity_sha256"],
                                   workspace["revision"], current_prefix)
                with self._budget_consume_lock:
                    if reservation_key in self._budget_consumed:
                        raise SwarmBudgetRefusal("budget_binding_required")
                    self._budget_consumed.add(reservation_key)
            except budget.WorkspaceBudgetError as exc:
                raise SwarmBudgetRefusal("budget_binding_required") from exc
            return {
                "schema": transport.TRANSPORT_ADMISSION_SCHEMA,
                "result": copy.deepcopy(result),
                "journal_revision": workspace["revision"],
                "journal_prefix_sha256": self._prefix_digest(raw_map),
                "provider_contacted": False,
                "state_mutated": False,
            }

        return transport.BoundTransportAdmission(
            decision=decision, policy=policy, snapshot=snapshot, request=request,
            binding=binding, planned_work=planned_work, consume=consume,
            turn_admission_id=turn_admission_id)

    def _budget_preflight(self, *, policy: Mapping[str, Any] | None,
                          snapshot: Mapping[str, Any] | None,
                          request: Mapping[str, Any] | None,
                          decision: Mapping[str, Any] | None = None,
                          required: bool = False) -> None:
        """Consume only a decision bound to the exact live coordinator state.

        Existing callers remain unchanged when no budget inputs are supplied.
        A complete detached policy/snapshot/request is intentionally not
        treated as a live admission gate: the current seam has no canonical
        binding between its selected messages and the checked request/state.
        Only ``workspace_budget_decision`` establishes that identity.
        """
        supplied = tuple(value is not None for value in (policy, snapshot, request))
        if decision is not None:
            if not all(supplied):
                raise SwarmBudgetRefusal("budget_binding_required")
            import _workspace_budget as budget
            try:
                state, torn, raw_map = self._load_with_snapshot()
                workspace = state.get("workspace")
                if (torn or not isinstance(workspace, Mapping)
                        or type(workspace.get("revision")) is not int):
                    raise budget.WorkspaceBudgetError("current coordinator state is not bindable")
                result = budget.consume_bound_decision(
                    decision, policy=policy, snapshot=snapshot, request=request,
                    journal_revision=workspace["revision"],
                    journal_prefix_sha256=self._prefix_digest(raw_map))
            except (budget.WorkspaceBudgetError, SwarmCoordinatorError) as exc:
                raise SwarmBudgetRefusal("budget_binding_required") from exc
            if result["status"] != "accepted":
                raise SwarmBudgetRefusal("budget_decision_blocked", result=result)
            return {"phase": "bound", **copy.deepcopy(result)}
        if not any(supplied):
            if required:
                raise SwarmBudgetRefusal("budget_policy_required")
            return None
        if not all(supplied):
            raise SwarmBudgetRefusal("budget_policy_required")
        raise SwarmBudgetRefusal("budget_binding_required")

    def _remembered_append(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any],
                           response: dict[str, Any], event: Mapping[str, Any], *,
                           frozen: bytes | None = None) -> dict[str, Any]:
        event_name = event.get("event")
        if event_name == "message_posted" and len(state["messages"]) >= MAX_STATUS_MESSAGES:
            raise SwarmCoordinatorError("swarm message limit reached")
        if event_name == "artifact_published" and len(state["artifacts"]) >= MAX_STATUS_ARTIFACTS:
            raise SwarmCoordinatorError("swarm artifact limit reached")
        record = dict(event)
        record.update({"message_id": frame["message_id"],
                       "frame_sha256": frame_sha256(frame),
                       "response": response})
        projected = copy.deepcopy(state)
        self._apply_record(projected, record)
        projected["seen"][frame["message_id"]] = {
            "frame_sha256": frame_sha256(frame), "response": response,
        }
        fingerprint = self._append_with_state(owner, record, state=state, frozen=frozen)
        projected["_prefix_fingerprint"] = fingerprint
        state.clear()
        state.update(projected)
        return response

    # --- protocol operations ---------------------------------------------

    def apply_frame(self, frame: Mapping[str, Any], *, worker_id: str = "coordinator") -> dict[str, Any]:
        """Apply one validated wire frame with durable idempotency.

        ``worker_id`` is the authenticated connection identity supplied by a
        future stdio/IDE adapter; it is deliberately not trusted from arbitrary
        frame prose.  The current local API uses the same value explicitly.
        """
        worker_id = _id(worker_id, "worker id")
        parsed = self._check_frame(frame)
        frame_type = parsed["type"]
        if frame_type == "hello":
            return {"status": "ok", "frame": make_frame(
                "hello_ack", run_id=self.run_id,
                message_id=_message_id("ack"), sent_at_ms=_now_ms(self.clock),
                payload={"selected_version": PROTOCOL,
                         "coordinator_id": f"coord-{self.run_id}",
                         "capabilities": ["claim", "renew", "message", "artifact", "cancel"],
                         "permission_ceiling": "read-only"})}
        if frame_type == "poll":
            return self.status()
        if frame_type == "shutdown":
            return self.close()
        with self._mutation() as (owner, state, _torn):
            duplicate = self._idempotent(state, parsed)
            if duplicate is not None:
                return duplicate
            if state.get("closed"):
                raise SwarmConflictError("swarm is closed")
            payload = parsed["payload"]
            if frame_type == "register_worker":
                if payload["worker_id"] != worker_id:
                    raise SwarmCoordinatorError("registered worker does not match connection")
                if payload["project_root_sha256"] != state["project_root_sha256"]:
                    raise SwarmCoordinatorError("worker project binding differs from run")
                if payload["roster_definition_sha256"] != state["roster_definition_sha256"]:
                    raise SwarmCoordinatorError("worker roster binding differs from run")
                existing = state["workers"].get(worker_id)
                if existing and existing["worker_instance_id"] != payload["worker_instance_id"]:
                    raise SwarmConflictError("worker id is already bound to another instance")
                response = self._response("registered", worker_id=worker_id)
                return self._remembered_append(owner, state, parsed, response, {
                    "event": "worker_registered", **{k: payload[k] for k in (
                        "worker_id", "worker_instance_id", "project_root_sha256",
                        "roster_definition_sha256", "permission_ceiling")}})
            # The coordinator itself is the authenticated sender for operator
            # cancellation.  It is not a worker registration and must not be
            # smuggled into the worker registry merely to cancel a claim.
            if frame_type == "cancel_requested" and worker_id == "coordinator":
                return self._apply_cancel_request(owner, state, parsed)
            if worker_id not in state["workers"]:
                raise SwarmCoordinatorError("worker must register before mutating the run")
            if frame_type in {"claim", "claim_requested", "task_claimed"}:
                return self._apply_claim(owner, state, parsed, worker_id)
            if frame_type in {"renew", "lease_renewed"}:
                return self._apply_renew(owner, state, parsed, worker_id)
            if frame_type in {"send_message", "message_posted"}:
                return self._apply_message(owner, state, parsed, worker_id)
            if frame_type in {"publish_artifact", "artifact_published"}:
                return self._apply_artifact(owner, state, parsed, worker_id)
            if frame_type in {"complete", "task_completed", "fail", "task_failed", "task_blocked"}:
                return self._apply_terminal(owner, state, parsed, worker_id)
            if frame_type == "ack_cancel":
                return self._apply_cancel_ack(owner, state, parsed, worker_id)
            if frame_type in {"cancelled", "indeterminate"}:
                return self._apply_cancel_outcome(owner, state, parsed, worker_id)
            if frame_type == "claim_released":
                return self._apply_release(owner, state, parsed, worker_id)
            raise SwarmCoordinatorError(f"frame type {frame_type!r} is not accepted here")

    def _claim_common(self, state: dict[str, Any], payload: Mapping[str, Any], worker_id: str,
                      *, now_ms: int | None = None) -> dict[str, Any]:
        task_id = _id(payload.get("task_id"), "task id")
        task = state["tasks"].get(task_id)
        if task is None:
            raise SwarmCoordinatorError("unknown task")
        request_sha = _digest(payload.get("request_sha256"), "request digest")
        if request_sha != task["request_sha256"]:
            raise SwarmConflictError("request digest does not match the task")
        active = [state["claims"][claim_id] for claim_id in task["attempts"]
                  if state["claims"][claim_id]["status"] == "active"]
        now = _now_ms(self.clock) if now_ms is None else _positive_int(now_ms, "admission time")
        if active:
            if any(claim["lease_expires_at_ms"] > now for claim in active):
                raise SwarmConflictError("task already has a live claim")
            raise SwarmIndeterminateError("expired claim may have spent; resolve it explicitly before retry")
        if task["terminal"] in {"completed", "cancelled", "blocked"}:
            raise SwarmConflictError(f"task is already {task['terminal']}")
        if task["terminal"] == "indeterminate" and not task["retry_authorized"]:
            raise SwarmIndeterminateError("uncertain spend requires explicit retry authorization")
        attempt = _positive_int(payload.get("attempt"), "attempt", MAX_ATTEMPTS)
        expected_attempt = len(task["attempts"]) + 1
        if attempt != expected_attempt or attempt > state["max_attempts"]:
            raise SwarmConflictError("claim attempt does not match the durable task state")
        lease_generation = _positive_int(payload.get("lease_generation"), "lease generation",
                                         MAX_GENERATION)
        previous_generations = [state["claims"][claim_id]["lease_generation"] for claim_id in task["attempts"]]
        expected_generation = (max(previous_generations) + 1) if previous_generations else 1
        if lease_generation != expected_generation:
            raise SwarmConflictError("claim lease generation does not match the durable task state")
        expires = _positive_int(payload.get("lease_expires_at_ms"), "lease expiry")
        if expires <= now or expires > now + MAX_LEASE_MS:
            raise SwarmConflictError("claim lease expiry is outside the allowed window")
        return {"task_id": task_id, "task": task, "request_sha256": request_sha,
                "attempt": attempt, "lease_generation": lease_generation,
                "lease_expires_at_ms": expires, "worker_id": worker_id}

    def _apply_claim(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        info = self._claim_common(state, payload, worker_id)
        claim_id = _id(payload.get("claim_id"), "claim id")
        if claim_id in state["claims"]:
            raise SwarmConflictError("claim id already exists")
        response = self._response("claimed", task_id=info["task_id"], claim_id=claim_id,
                                  attempt=info["attempt"], lease_generation=info["lease_generation"],
                                  lease_expires_at_ms=info["lease_expires_at_ms"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_granted", "task_id": info["task_id"], "claim_id": claim_id,
            "worker_id": worker_id, "attempt": info["attempt"],
            "lease_generation": info["lease_generation"],
            "lease_expires_at_ms": info["lease_expires_at_ms"],
            "request_sha256": info["request_sha256"]})

    def _claim_for_update(self, state: dict[str, Any], payload: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim_id = _id(payload.get("claim_id"), "claim id")
        claim = state["claims"].get(claim_id)
        if claim is None:
            raise SwarmCoordinatorError("unknown claim")
        if claim["worker_id"] != worker_id:
            raise SwarmConflictError("claim belongs to another worker")
        if claim["status"] != "active":
            raise SwarmConflictError("claim is no longer active")
        if claim["lease_expires_at_ms"] <= _now_ms(self.clock):
            raise SwarmIndeterminateError("claim lease expired; resolve uncertain spend explicitly")
        if _positive_int(payload.get("lease_generation"), "lease generation",
                         MAX_GENERATION) != claim["lease_generation"]:
            raise SwarmConflictError("claim lease generation is stale")
        return claim

    def _apply_renew(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        if claim.get("cancel_requested"):
            raise SwarmConflictError("claim cancellation is pending")
        if claim.get("renewals", 0) >= MAX_ROUTINE_RENEWALS:
            raise SwarmConflictError("routine renewal budget is exhausted")
        expires = _positive_int(payload.get("lease_expires_at_ms"), "lease expiry")
        now = _now_ms(self.clock)
        if expires <= now or expires > now + MAX_LEASE_MS:
            raise SwarmConflictError("renewed lease expiry is outside the allowed window")
        renewal_number = claim.get("renewals", 0) + 1
        response = self._response("renewed", claim_id=claim["claim_id"],
                                  lease_generation=claim["lease_generation"],
                                  lease_expires_at_ms=expires,
                                  renewal_number=renewal_number)
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_renewed", "claim_id": claim["claim_id"],
            "worker_id": worker_id, "lease_generation": claim["lease_generation"],
            "lease_expires_at_ms": expires, "renewal_number": renewal_number})

    def _apply_message(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        if payload.get("from") != worker_id:
            raise SwarmConflictError("message sender does not match connection")
        target = payload.get("to")
        if target not in {"coordinator", "human"} and target not in state["workers"]:
            raise SwarmCoordinatorError("message recipient is not registered")
        response = self._response("posted", message_id=frame["message_id"], to=target)
        event = {"event": "message_posted", "from": worker_id, "to": target,
                 "content_sha256": payload["content_sha256"],
                 "content_chars": payload["content_chars"],
                 "preview": _bounded_reason(payload["preview"], "message preview")}
        if payload.get("reply_to") is not None:
            event["reply_to"] = payload["reply_to"]
        return self._remembered_append(owner, state, frame, response, event)

    def _apply_artifact(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        for key in ("task_id", "attempt", "request_sha256"):
            if payload.get(key) != claim[key] and not (key == "task_id" and payload.get(key) == claim["task_id"]):
                raise SwarmConflictError("artifact claim fence does not match")
        if payload.get("attempt") != claim["attempt"] or payload.get("request_sha256") != claim["request_sha256"]:
            raise SwarmConflictError("artifact claim fence does not match")
        # Artifact IDs are immutable publication identities.  A repeated
        # frame with the same message ID is handled by the idempotency guard
        # before this method; a new frame reusing an existing artifact ID must
        # refuse before journal append, even when its metadata is identical.
        if any(item.get("artifact_id") == payload.get("artifact_id")
               for item in state.get("artifacts", ())):
            raise SwarmConflictError("artifact id was already published")
        artifact = {key: payload[key] for key in (
            "artifact_id", "task_id", "claim_id", "attempt", "lease_generation",
            "request_sha256", "sha256", "bytes", "media_type")}
        if "relative_path" in payload:
            artifact["relative_path"] = payload["relative_path"]
        if "opaque_uri" in payload:
            artifact["opaque_uri"] = payload["opaque_uri"]
        response = self._response("artifact_published", artifact_id=payload["artifact_id"])
        return self._remembered_append(owner, state, frame, response,
                                       {"event": "artifact_published", **artifact})

    def _apply_terminal(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        if payload.get("task_id") != claim["task_id"] or payload.get("attempt") != claim["attempt"]:
            raise SwarmConflictError("terminal claim fence does not match")
        if payload.get("request_sha256") != claim["request_sha256"]:
            raise SwarmConflictError("terminal request fence does not match")
        if frame["type"] in {"complete", "task_completed"}:
            if claim["cancel_requested"]:
                raise SwarmConflictError("claim has a pending cancellation")
            event, status = "task_completed", "completed"
        elif frame["type"] in {"fail", "task_failed"}:
            event, status = "task_failed", "failed"
        else:
            event, status = "task_blocked", "blocked"
        event_data: dict[str, Any] = {"event": event, "task_id": claim["task_id"],
                                      "claim_id": claim["claim_id"], "worker_id": worker_id,
                                      "attempt": claim["attempt"],
                                      "lease_generation": claim["lease_generation"],
                                      "request_sha256": claim["request_sha256"],
                                      "envelope_sha256": payload.get("envelope_sha256")}
        if payload.get("reason") is not None:
            event_data["reason"] = _bounded_reason(payload["reason"])
        response = self._response(status, task_id=claim["task_id"], claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, event_data)

    def _apply_cancel_request(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any]) -> dict[str, Any]:
        payload = frame["payload"]
        claim = state["claims"].get(_id(payload.get("claim_id"), "claim id"))
        if claim is None or claim["status"] != "active":
            raise SwarmConflictError("claim is not cancellable")
        if claim["lease_expires_at_ms"] <= _now_ms(self.clock):
            raise SwarmIndeterminateError("claim lease expired; resolve uncertain spend explicitly")
        if payload.get("lease_generation") != claim["lease_generation"]:
            raise SwarmConflictError("cancel lease generation is stale")
        response = self._response("cancel_requested", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "cancel_requested", "claim_id": claim["claim_id"],
            "lease_generation": claim["lease_generation"],
            "reason": _bounded_reason(payload.get("reason"))})

    def _apply_cancel_ack(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        if not claim["cancel_requested"]:
            raise SwarmConflictError("cancel has not been requested")
        if claim.get("cancel_acknowledged"):
            raise SwarmConflictError("cancel has already been acknowledged")
        response = self._response("cancel_acknowledged", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "cancel_acknowledged", "claim_id": claim["claim_id"],
            "worker_id": worker_id, "lease_generation": claim["lease_generation"]})

    def _apply_cancel_outcome(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        if not claim["cancel_requested"]:
            raise SwarmConflictError("cancel has not been requested")
        event = "task_cancelled" if frame["type"] == "cancelled" else "claim_indeterminate"
        status = "cancelled" if event == "task_cancelled" else "indeterminate"
        response = self._response(status, claim_id=claim["claim_id"], uncertain_spend=(status == "indeterminate"))
        return self._remembered_append(owner, state, frame, response, {
            "event": event, "task_id": claim["task_id"], "claim_id": claim["claim_id"],
            "worker_id": worker_id, "attempt": claim["attempt"],
            "lease_generation": claim["lease_generation"],
            "request_sha256": claim["request_sha256"],
            "reason": _bounded_reason(frame["payload"].get("reason"))})

    def _apply_release(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        response = self._response("released", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_released", "task_id": claim["task_id"],
            "claim_id": claim["claim_id"], "worker_id": worker_id,
            "attempt": claim["attempt"], "lease_generation": claim["lease_generation"],
            "request_sha256": claim["request_sha256"],
            "reason": _bounded_reason(frame["payload"].get("reason"))})

    # --- ergonomic local API ---------------------------------------------

    def register_worker(self, worker_id: str, *, worker_instance_id: str,
                        capabilities: list[str] | None = None,
                        permission_ceiling: str = "read-only") -> dict[str, Any]:
        state, _ = self._load()
        payload = {"worker_id": _id(worker_id, "worker id"),
                   "worker_instance_id": _id(worker_instance_id, "worker instance id"),
                   "project_root_sha256": state["project_root_sha256"],
                   "roster_definition_sha256": state["roster_definition_sha256"],
                   "capabilities": capabilities or [], "permission_ceiling": permission_ceiling}
        frame = make_frame("register_worker", run_id=self.run_id,
                           message_id=_message_id("register"), sent_at_ms=_now_ms(self.clock),
                           payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def claim(self, worker_id: str, task_id: str, *, request_sha256: str,
              lease_ms: int = 30_000, message_id: str | None = None) -> dict[str, Any]:
        worker_id = _id(worker_id, "worker id")
        task_id = _id(task_id, "task id")
        request_sha256 = _digest(request_sha256, "request digest")
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or not 1 <= lease_ms <= MAX_LEASE_MS:
            raise SwarmProtocolError("invalid lease duration")
        frame_id = _id(message_id or _message_id("claim"), "message id")
        # Preserve pre-owner refusals: acquiring an owner can repair a torn
        # journal and advances durable generation even if no claim is appended.
        initial, _ = self._load()
        if task_id not in initial["tasks"]:
            raise SwarmCoordinatorError("unknown task")
        # Construct and match under the same durable owner as the append. A
        # helper retry must not mint a fresh claim ID before checking history.
        with self._mutation() as (owner, state, _torn):
            existing = state["seen"].get(frame_id)
            if existing is not None:
                original = existing.get("response")
                if not isinstance(original, dict) or original.get("status") != "claimed":
                    raise SwarmConflictError("message id was reused for a different frame")
                claim = state["claims"].get(original.get("claim_id"))
                if claim is None or claim.get("worker_id") != worker_id:
                    raise SwarmConflictError("claim retry worker does not match original owner")
                # Response fields retain the ORIGINAL lease even after renew.
                # Reconstruct sent_at from the caller's duration so a changed
                # lease also fails the existing exact frame digest check.
                frame = make_frame("claim", run_id=self.run_id,
                    message_id=frame_id,
                    sent_at_ms=original["lease_expires_at_ms"] - lease_ms,
                    payload={"task_id": task_id, "claim_id": original["claim_id"],
                             "attempt": original["attempt"],
                             "lease_generation": original["lease_generation"],
                             "lease_expires_at_ms": original["lease_expires_at_ms"],
                             "request_sha256": request_sha256})
                return self._idempotent(state, self._check_frame(frame))
            task = state["tasks"].get(task_id)
            if task is None:
                raise SwarmCoordinatorError("unknown task")
            if state.get("closed"):
                raise SwarmConflictError("swarm is closed")
            if worker_id not in state["workers"]:
                raise SwarmCoordinatorError("worker must register before mutating the run")
            previous = [state["claims"][claim_id]["lease_generation"] for claim_id in task["attempts"]]
            now = _now_ms(self.clock)
            frame = make_frame("claim", run_id=self.run_id,
                message_id=frame_id, sent_at_ms=now,
                payload={"task_id": task_id, "claim_id": _message_id("claim"),
                         "attempt": len(task["attempts"]) + 1,
                         "lease_generation": max(previous) + 1 if previous else 1,
                         "lease_expires_at_ms": now + lease_ms,
                         "request_sha256": request_sha256})
            return self._apply_claim(owner, state, self._check_frame(frame), worker_id)

    def admit_operator_message(self, request: Mapping[str, Any], *,
                               resolve_operator: Callable, prepare_content: Callable,
                               budget_policy: Mapping[str, Any] | None = None,
                               budget_snapshot: Mapping[str, Any] | None = None,
                               budget_request: Mapping[str, Any] | None = None,
                               budget_decision: Mapping[str, Any] | None = None,
                               require_budget: bool = False) -> dict[str, Any]:
        """Queue exact operator context; installed host callbacks supply authority.

        No worker, provider or browser authentication is created here. Content
        is published once outside ownership. The resolver must perform no I/O,
        nested mutation or worker traffic and must check current host/session
        authority every time it is called, including the final append boundary.
        """
        return self._operator_message_operation(request, resolve_operator=resolve_operator,
                                                prepare_content=prepare_content, lookup=False,
                                                budget_policy=budget_policy,
                                                budget_snapshot=budget_snapshot,
                                                budget_request=budget_request,
                                                budget_decision=budget_decision,
                                                require_budget=require_budget)

    def reconcile_operator_message(self, request: Mapping[str, Any], *,
                                   resolve_operator: Callable) -> dict[str, Any]:
        """Observe one exact operator send after owned prefix sync, never retry it."""
        return self._operator_message_operation(request, resolve_operator=resolve_operator,
                                                prepare_content=None, lookup=True)

    def _operator_message_operation(self, request, *, resolve_operator, prepare_content, lookup,
                                    budget_policy=None, budget_snapshot=None,
                                    budget_request=None, budget_decision=None, require_budget=False):
        import _workspace_admission as admission

        checked = admission.operator_request(request)
        if not callable(resolve_operator) or (not lookup and not callable(prepare_content)):
            raise SwarmProtocolError("trusted operator source/content integration required")
        budget_result = self._budget_preflight(
            policy=budget_policy, snapshot=budget_snapshot, request=budget_request,
            decision=budget_decision,
            required=require_budget)

        def resolve(current):
            workspace = current.get("workspace")
            if type(workspace) is not dict or workspace.get("run_id") != self.run_id:
                raise SwarmProtocolError("operator workspace scope invalid")
            supplied = resolve_operator(copy.deepcopy(current), copy.deepcopy(workspace), copy.deepcopy(checked))
            return admission.validate_operator_scope(supplied, checked, coordinator_state=current,
                workspace_state=workspace, now_ms=_now_ms(self.clock))

        def identity(current, resolved):
            return admission.operator_request_identity(current["workspace"], resolved, checked)

        def prior(current, operation, digest):
            previous = current["workspace"].get("send_operations", {}).get(operation)
            if previous is None:
                return None
            if previous.get("request_sha256") != digest:
                raise SwarmConflictError("operator send operation key was reused")
            if type(previous.get("response")) is not dict:
                raise SwarmCorruptError("operator send response is malformed")
            return copy.deepcopy(previous["response"])

        preflight, torn = self._load()
        if torn:
            raise SwarmCorruptError("operator send refuses a torn journal prefix")
        observed = resolve(preflight)
        operation, digest = identity(preflight, observed)
        already = prior(preflight, operation, digest)
        descriptor = None
        if not lookup and already is None:
            if preflight.get("closed"):
                raise SwarmConflictError("cannot send after workspace close")
            raw = checked["text"].encode("utf-8")
            content_id = "blob-" + hashlib.sha256(raw).hexdigest()[:32]
            predicted = {"ref": content_id, "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}
            candidate = admission.build_operator_send_event(checked, observed, predicted, preflight["workspace"])
            admission.validate_operator_send(candidate, coordinator_state=preflight,
                workspace_state=preflight["workspace"], now_ms=_now_ms(self.clock))
            descriptor = prepare_content(copy.deepcopy(checked), content_id)
            admission._object(descriptor, {"ref", "sha256", "utf8_bytes"})
            if descriptor != predicted or type(descriptor["utf8_bytes"]) is not int:
                raise SwarmProtocolError("operator prepared content binding differs")
            descriptor = copy.deepcopy(descriptor)

        # Neither sends nor lookup silently repair an uncertain tail. An orphan
        # content blob is not admission and may be reconciled independently.
        with self._mutation(repair=False) as (owner, current, _torn):
            final = resolve(current)
            if identity(current, final) != (operation, digest) or final != observed:
                raise SwarmConflictError("operator binding changed during admission")
            response = prior(current, operation, digest)
            if lookup or response is not None:
                records, torn, raw_map = self._read_records_snapshot()
                fingerprint = current["_prefix_fingerprint"]
                if torn or self._prefix_fingerprint(raw_map) != fingerprint:
                    raise SwarmCorruptError("operator send lookup prefix differs")
                matches = [record for record in records if type(record.get("workspace_event")) is dict
                           and record["workspace_event"].get("operation_key") == operation]
                if len(matches) != (1 if response is not None else 0):
                    raise SwarmCorruptError("operator send lookup disagrees with journal")
                if matches:
                    retained = admission.canonical_operator_send_event(matches[0]["workspace_event"])
                    if (matches[0].get("event") != _WORKSPACE_WRAPPER_EVENT
                            or retained["payload"]["request"]["request_sha256"] != digest
                            or admission.operator_send_response(retained) != response):
                        raise SwarmCorruptError("operator send lookup binding differs")
                _tagged, final_torn, final_raw = self._strict_snapshot()
                if final_torn or self._prefix_fingerprint(final_raw) != fingerprint or not owner_still_current(owner):
                    raise SwarmCorruptError("operator send journal changed during lookup")
                if resolve(current) != final:
                    raise SwarmConflictError("operator authority changed during lookup")
                if response is not None:
                    result = response | ({"durable_prefix_verified": True} if lookup else {})
                    if budget_result is not None:
                        result["budget_preflight"] = copy.deepcopy(budget_result)
                    return result
                return {"status": "not_observed", "operation_key": checked["operation_key"],
                        "request_sha256": digest, "revision": current["workspace"]["revision"],
                        "durable_prefix_verified": True, "execution_authorized": False}
            if descriptor is None:
                raise SwarmCorruptError("previous operator send disappeared")
            if current.get("closed"):
                raise SwarmConflictError("cannot send after workspace close")
            now = _now_ms(self.clock)
            event = admission.build_operator_send_event(checked, final, descriptor, current["workspace"])
            admission.validate_operator_send(event, coordinator_state=current,
                workspace_state=current["workspace"], now_ms=now)

            def final_constraint(snapshot):
                return resolve(snapshot) == final

            self._append_with_state(owner, {"event": _WORKSPACE_WRAPPER_EVENT,
                "workspace_event": event, "observed_at_ms": now}, state=current, before_append=final_constraint)
            # A revoked session may lose its response after a real append. A
            # fresh independently authorized session must use exact-key lookup.
            if resolve(current) != final:
                raise SwarmConflictError("operator authority changed after append")
            result = admission.operator_send_response(event)
            if budget_result is not None:
                result["budget_preflight"] = copy.deepcopy(budget_result)
            return result

    def admit_worker_message(self, request: Mapping[str, Any], *,
                             resolve_source: Callable,
                             prepare_content: Callable,
                             budget_policy: Mapping[str, Any] | None = None,
                             budget_snapshot: Mapping[str, Any] | None = None,
                             budget_request: Mapping[str, Any] | None = None,
                             budget_decision: Mapping[str, Any] | None = None,
                             require_budget: bool = False) -> dict[str, Any]:
        return self._admit_message(request, resolve_source=resolve_source, prepare_content=prepare_content,
                                   budget_policy=budget_policy, budget_snapshot=budget_snapshot,
                                   budget_request=budget_request, budget_decision=budget_decision,
                                   require_budget=require_budget)

    def admit_supervisor_message(self, request: Mapping[str, Any], *,
                                 resolve_source: Callable, prepare_content: Callable,
                                 budget_policy: Mapping[str, Any] | None = None,
                                 budget_snapshot: Mapping[str, Any] | None = None,
                                 budget_request: Mapping[str, Any] | None = None,
                                 budget_decision: Mapping[str, Any] | None = None,
                                 require_budget: bool = False) -> dict[str, Any]:
        return self._admit_message(request, resolve_source=resolve_source, prepare_content=prepare_content,
                                   supervisor=True, budget_policy=budget_policy,
                                   budget_snapshot=budget_snapshot, budget_request=budget_request,
                                   budget_decision=budget_decision,
                                   require_budget=require_budget)

    def _admit_message(self, request: Mapping[str, Any], *, resolve_source: Callable,
                       prepare_content: Callable, supervisor=False,
                       budget_policy: Mapping[str, Any] | None = None,
                       budget_snapshot: Mapping[str, Any] | None = None,
                       budget_request: Mapping[str, Any] | None = None,
                       budget_decision: Mapping[str, Any] | None = None,
                       require_budget: bool = False) -> dict[str, Any]:
        """Commit one worker-origin intent through the existing sole writer.

        The trusted runtime derives its source from an actual owned channel.
        JSON is not authentication. Source resolution runs at most twice:
        read-only preflight, then final same-state admission. Content publication
        runs once outside the owner mutation. Callbacks must not perform pipe,
        provider/network I/O or nested coordinator mutations under ownership.
        """
        import _workspace_admission as admission
        build = admission.build_supervisor_send_event if supervisor else admission.build_send_event
        validate = admission.validate_supervisor_send if supervisor else admission.validate_worker_send

        checked = admission.send_request(request)
        if not callable(resolve_source) or not callable(prepare_content):
            raise SwarmCoordinatorError("trusted worker source/content integration required")
        budget_result = self._budget_preflight(
            policy=budget_policy, snapshot=budget_snapshot, request=budget_request,
            decision=budget_decision,
            required=require_budget)

        def resolve(current):
            workspace = current.get("workspace")
            if not isinstance(workspace, dict) or workspace.get("run_id") != self.run_id:
                raise SwarmCoordinatorError("authoritative workspace state required")
            try:
                resolved = resolve_source(copy.deepcopy(current), copy.deepcopy(workspace), copy.deepcopy(checked))
                admission._object(resolved, {"binding", "claim", "send_scope"})
                admission._canonical_event_bytes(resolved)
                binding = admission.send_binding(resolved["binding"])
                if binding["run_id"] != self.run_id or binding["workspace_id"] != workspace["workspace_id"]:
                    raise SwarmCoordinatorError("observed worker scope differs")
                return copy.deepcopy(resolved)
            except Exception as exc:
                raise SwarmCoordinatorError("trusted worker source resolution failed") from exc

        def prior(workspace, operation, digest):
            previous = workspace.get("send_operations", {}).get(operation)
            if previous is None:
                return None
            if previous.get("request_sha256") != digest:
                raise SwarmConflictError("send operation key was reused")
            if type(previous.get("response")) is not dict:
                raise SwarmCorruptError("send operation response is malformed")
            return copy.deepcopy(previous["response"])

        # Preflight has no owner and is not authority for the final commit.
        preflight, torn = self._load()
        if torn:
            raise SwarmCorruptError("workspace send requires a complete journal prefix")
        observed = resolve(preflight)
        operation, digest = admission.send_request_identity(observed["binding"], checked)
        already = prior(preflight["workspace"], operation, digest)
        descriptor = None
        if already is None:
            raw = checked["content"].encode("utf-8")
            # ContentStore names are blob-<32 hex>. A collision must refuse,
            # never overwrite; the trusted publisher verifies the full digest.
            content_id = "blob-" + hashlib.sha256(raw).hexdigest()[:32]
            predicted = {"ref": content_id, "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}
            preflight_event = build(checked, observed, predicted, preflight["workspace"])
            # Reject already-known invalid routes/claims/capacity shapes before
            # publication. This read-only snapshot never authorizes the append;
            # final source resolution and the same validator run under owner.
            validate(preflight_event, coordinator_state=preflight,
                                             workspace_state=preflight["workspace"], now_ms=_now_ms(self.clock))
            try:
                descriptor = prepare_content(copy.deepcopy(checked), content_id)
                admission._object(descriptor, {"ref", "sha256", "utf8_bytes"})
                if (descriptor["ref"] != content_id or descriptor["sha256"] != hashlib.sha256(raw).hexdigest()
                        or type(descriptor["utf8_bytes"]) is not int or descriptor["utf8_bytes"] != len(raw)):
                    raise SwarmCoordinatorError("prepared content binding differs")
                descriptor = copy.deepcopy(descriptor)
            except Exception as exc:
                raise SwarmCoordinatorError("worker message content preparation failed") from exc

        with self._mutation() as (owner, state, _torn):
            final = resolve(state)
            if final["binding"] != observed["binding"]:
                raise SwarmConflictError("worker channel binding changed during admission")
            response = prior(state["workspace"], operation, digest)
            if response is not None:
                if budget_result is not None:
                    response["budget_preflight"] = copy.deepcopy(budget_result)
                return response
            if descriptor is None:
                raise SwarmCorruptError("committed send operation disappeared")
            if state.get("closed"):
                raise SwarmConflictError("cannot send after workspace close")
            now = _now_ms(self.clock)
            event = build(checked, final, descriptor, state["workspace"])
            event = validate(event, coordinator_state=state,
                                                    workspace_state=state["workspace"], now_ms=now)
            record = {"event": _WORKSPACE_WRAPPER_EVENT, "workspace_event": event, "observed_at_ms": now}
            self._append_with_state(owner, record, state=state)
            response = admission.send_response(event)
            if budget_result is not None:
                response["budget_preflight"] = copy.deepcopy(budget_result)
            return response

    def supervisor_inbox_command(self, command: Mapping[str, Any], *, resolve_consumer: Callable) -> dict[str, Any]:
        """One sole-writer command; callback is installed trusted host code, not JSON authority.

        It runs once, has no pipe/network/file waits or nested mutation, and
        must derive the live consumer (or independently authorize recovery).
        The core checks current binding even before historical live readback.
        """
        import _workspace_admission as admission
        checked = admission.inbox_command(command)
        if not callable(resolve_consumer):
            raise SwarmCoordinatorError("trusted consumer integration required")
        with self._mutation() as (owner, state, _torn):
            workspace = state.get("workspace")
            if not isinstance(workspace, dict):
                raise SwarmCoordinatorError("workspace required")
            resolved = resolve_consumer(copy.deepcopy(state), copy.deepcopy(workspace), copy.deepcopy(checked))
            resolved = admission.inbox_resolution(resolved)
            if checked["action"] in admission.INBOX_LIVE_ACTIONS - {"activate"}:
                endpoint = workspace.get("supervisor_endpoints", {}).get(checked["endpoint_id"])
                if endpoint is None or resolved["lease_expires_at_ms"] != endpoint["lease_expires_at_ms"]:
                    raise SwarmConflictError("observed consumer lease differs from current endpoint")
            previous = workspace.get("inbox_operations", {}).get(checked["operation_key"])
            if previous is not None:
                if previous["request_sha256"] != admission.inbox_command_identity(checked):
                    raise SwarmConflictError("inbox operation key reused")
                if checked["action"] in admission.INBOX_LIVE_ACTIONS:
                    endpoint = workspace.get("supervisor_endpoints", {}).get(checked["endpoint_id"])
                    if endpoint is None or resolved["consumer"] != previous["consumer"]:
                        raise SwarmConflictError("historical inbox consumer binding differs")
                    admission.require_current_consumer(endpoint, resolved["consumer"], _now_ms(self.clock))
                return copy.deepcopy(previous["response"])
            if state.get("closed"):
                raise SwarmConflictError("cannot mutate closed inbox")
            now = _now_ms(self.clock)
            event = admission.build_inbox_command_event(checked, resolved, workspace)
            admission.validate_inbox_command_event(event, coordinator_state=state, workspace_state=workspace, now_ms=now)
            self._append_with_state(owner, {"event": _WORKSPACE_WRAPPER_EVENT, "workspace_event": event,
                                           "observed_at_ms": now}, state=state)
            return admission.inbox_response(event)

    def reconcile_operator_command(self, request: Mapping[str, Any], *,
                                   authorize_command: Callable, inspect_command: Callable) -> dict[str, Any]:
        """Synchronize and inspect exactly two scoped operator records, without append.

        Callbacks are installed trusted-host code, never JSON capabilities.
        Authorization runs at most twice (preflight and under ownership);
        inspection once, with at most two detached canonical event envelopes.
        They must perform no blocking I/O, worker traffic, nested mutation or
        source publication. The runtime independently binds observed host
        decision sources and current authorization; this core proves only the
        synchronized journal observation. Storage uncertainty raises rather
        than returning a false absence or successful admission.
        """
        import _workspace_admission as admission
        import _workspace_state as projection
        admission._object(request, {"workspace_id", "run_id", "operation_key", "request_sha256",
                                    "evidence_operation_key", "transition_operation_key"})
        admission._canonical_event_bytes(request)
        query = copy.deepcopy(dict(request))
        for key in ("workspace_id", "run_id"):
            admission._id(query[key], key)
        logical = query["operation_key"]
        if (type(logical) is not str or not re.fullmatch(r"[a-f0-9]{32}", logical)
                or query["evidence_operation_key"] != "operator-evidence-" + logical
                or query["transition_operation_key"] != "operator-transition-" + logical):
            raise SwarmProtocolError("invalid operator reconciliation keys")
        admission._digest(query["request_sha256"], "operator request digest")
        if query["run_id"] != self.run_id or not callable(authorize_command) or not callable(inspect_command):
            raise SwarmProtocolError("operator reconciliation integration or scope invalid")

        def authorize(state):
            workspace = state.get("workspace")
            if (type(workspace) is not dict
                    or any(workspace.get(key) != query[key] for key in ("workspace_id", "run_id"))
                    or len(workspace.get("operations", {})) > projection.MAX_EVENTS):
                raise SwarmProtocolError("operator reconciliation workspace mismatch")
            if authorize_command(copy.deepcopy(state), copy.deepcopy(workspace), copy.deepcopy(query)) is not True:
                raise SwarmProtocolError("operator command not authorized")
            return workspace

        preflight, torn = self._load()
        if torn:
            raise SwarmCorruptError("reconciliation refuses a torn journal prefix")
        authorize(preflight)
        # Lookup must not silently repair/truncate an uncertain command tail.
        with self._mutation(repair=False) as (owner, state, _torn):
            workspace = authorize(state)
            records, torn, raw = self._read_records_snapshot()
            fingerprint = state["_prefix_fingerprint"]
            if torn or self._prefix_fingerprint(raw) != fingerprint:
                raise SwarmCorruptError("operator lookup prefix differs from synchronized state")
            expected = {query["evidence_operation_key"]: "workspace_evidence_registered",
                        query["transition_operation_key"]: "workspace_delivery_advanced"}
            found = {}
            for record in records:
                nested = record.get("workspace_event")
                if type(nested) is not dict or nested.get("operation_key") not in expected:
                    continue
                key = nested["operation_key"]
                if key in found:
                    raise SwarmCorruptError("duplicate operator operation in journal")
                checked = admission._event_envelope(nested)
                if (record.get("event") != _WORKSPACE_WRAPPER_EVENT or checked["event"] != expected[key]
                        or any(checked[scope] != query[scope] for scope in ("workspace_id", "run_id"))):
                    raise SwarmProtocolError("operator operation key conflicts with retained event")
                found[key] = checked
            has_evidence = query["evidence_operation_key"] in found
            has_transition = query["transition_operation_key"] in found
            if has_transition and not has_evidence:
                raise SwarmProtocolError("operator transition has no matching evidence operation")
            expected_status = "recorded" if has_transition else "evidence_only" if has_evidence else "not_observed"
            observation = inspect_command(copy.deepcopy(state), copy.deepcopy(workspace), copy.deepcopy(query), copy.deepcopy(found))
            admission._object(observation, {"status", "request_sha256", "revision"})
            if (observation["status"] != expected_status
                    or observation["request_sha256"] != query["request_sha256"]
                    or type(observation["revision"]) is not int or observation["revision"] != workspace["revision"]):
                raise SwarmProtocolError("operator inspection conflicts with synchronized observation")
            # Do not certify an old snapshot if a callback or outside writer
            # changed the owned prefix while the semantic checks were running.
            _tagged, final_torn, final_raw = self._strict_snapshot()
            if final_torn or self._prefix_fingerprint(final_raw) != fingerprint or not owner_still_current(owner):
                raise SwarmCorruptError("operator journal changed before reconciliation response")
            return {"status": expected_status, "request_sha256": query["request_sha256"],
                    "durable_prefix_verified": True, "revision": workspace["revision"]}

    def admit_claim_selection(self, worker_id: str, admission_event: Mapping[str, Any], *,
                              resolve_grant: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
                              lease_ms: int = 30_000,
                              message_id: str | None = None,
                              budget_policy: Mapping[str, Any] | None = None,
                              budget_request: Mapping[str, Any] | None = None,
                              economics_reservation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Atomically admit one fresh claim and its workspace selection.

        This is the provider-free base hook for the trusted workspace facade.
        It reads the journal-bound workspace projection under the owner lease,
        asks only the trusted private grant resolver for current grant state,
        prepares a detached prospective claim, validates the complete
        workspace grant/hold/selection against that post-claim view, then
        persists one composite journal record through the normal reconciliation
        and reserve boundary.  It captures admission time once and does not
        require a durable launch-intent proof until the later pipe boundary.
        It never launches a worker or opens a pipe.
        """
        import _workspace_admission as admission

        worker_id = _id(worker_id, "worker id")
        if not isinstance(admission_event, Mapping):
            raise SwarmCoordinatorError("workspace admission event is required")
        if not callable(resolve_grant):
            raise SwarmCoordinatorError("trusted grant resolver is required")
        if (budget_policy is None) != (budget_request is None):
            raise SwarmBudgetRefusal("budget_policy_required")
        if (not isinstance(lease_ms, int) or isinstance(lease_ms, bool)
                or not 1 <= lease_ms <= MAX_LEASE_MS):
            raise SwarmProtocolError("invalid lease duration")
        admission_id = _id(admission_event.get("operation_key"), "admission operation key")
        frame_id = _id(message_id or admission_id, "admission message id")
        with self._mutation() as (owner, state, _torn):
            raw_checked = admission._event_envelope(admission_event)
            raw_payload = raw_checked["payload"]
            if economics_reservation is not None:
                if not isinstance(economics_reservation, Mapping):
                    raise SwarmProtocolError("economics reservation must be an object")
                raw_payload["economics_reservation"] = copy.deepcopy(dict(economics_reservation))
            raw_candidate = raw_payload.get("claim")
            if not isinstance(raw_candidate, Mapping):
                raise SwarmCoordinatorError("workspace admission claim is required")
            stable_request = copy.deepcopy(raw_checked)
            stable_request["payload"] = copy.deepcopy(raw_payload)
            stable_request["payload"]["claim"] = copy.deepcopy(dict(raw_candidate))
            # Lease expiry is a transaction-local fact, not caller-supplied
            # request identity.  Normalize it out for duplicate comparison.
            stable_request["payload"]["claim"]["lease_expires_at_ms"] = 1
            request_identity = hashlib.sha256(
                admission._canonical_event_bytes(stable_request)).hexdigest()
            existing = state.get("admissions", {}).get(admission_id)
            if existing is not None:
                if (existing.get("request_identity_sha256") != request_identity
                        or existing.get("claim_id") != raw_candidate.get("claim_id")):
                    raise SwarmConflictError("admission operation key was reused")
            now = _now_ms(self.clock)
            if existing is not None:
                response = existing.get("response")
                if not isinstance(response, dict):
                    raise SwarmCorruptError("admission idempotency response is invalid")
                return copy.deepcopy(response)
            workspace_state = state.get("workspace")
            if not isinstance(workspace_state, Mapping):
                raise SwarmCoordinatorError("authoritative workspace state is required")
            candidate = copy.deepcopy(dict(raw_candidate))
            candidate["lease_expires_at_ms"] = now + lease_ms
            normalized_event = copy.deepcopy(raw_checked)
            normalized_event["payload"] = copy.deepcopy(raw_payload)
            normalized_event["payload"]["claim"] = candidate
            if economics_reservation is not None:
                normalized_event["payload"]["economics_reservation"] = copy.deepcopy(dict(economics_reservation))
            grant_ref = normalized_event["payload"].get("grant_ref")
            try:
                grant_state = resolve_grant(
                    copy.deepcopy(state), copy.deepcopy(workspace_state),
                    copy.deepcopy(grant_ref))
            except Exception as exc:
                raise SwarmCoordinatorError("trusted grant resolver failed") from exc
            if not isinstance(grant_state, Mapping):
                raise SwarmCoordinatorError("trusted grant resolver returned no grant")
            checked = admission.validate_atomic_claim_selection(
                normalized_event, coordinator_state=state,
                workspace_state=workspace_state, grant_state=grant_state)
            payload = checked["payload"]
            economics_reservation = payload.get("economics_reservation")
            if economics_reservation is not None:
                # Prove the configured fence is large enough for every
                # accepted terminal outcome before a worker can be launched.
                required_settlement_bytes = self._economics_settlement_capacity_bound(
                    economics_reservation, payload["claim"], payload["selection"], admission_id)
                if economics_reservation["max_settlement_bytes"] < required_settlement_bytes:
                    raise SwarmBudgetRefusal("economics_settlement_capacity_insufficient")
            budget_admission = None
            if budget_policy is not None:
                import _workspace_budget as budget
                records, records_torn, raw_map = self._read_records_snapshot()
                if (records_torn
                        or state.get("_prefix_fingerprint") != self._prefix_fingerprint(raw_map)):
                    raise SwarmCorruptError("budget state is stale relative to journal prefix")
                try:
                    checked_policy = budget.validate_policy(budget_policy)
                    checked_request = budget.validate_request(budget_request)
                    selected_ids = list(payload["selection"]["selected_message_ids"])
                    if (checked_request["kind"] != "message"
                            or checked_request["execution_slots_requested"] != 1
                            or checked_request["selected_message_ids"] != selected_ids):
                        raise budget.WorkspaceBudgetError("budget request is not bound to selection")
                    snapshot = self.workspace_budget_snapshot(
                        state=state, raw_map=raw_map, records=records, now_ms=now,
                        preferred_delivery_ids=set(payload["selected_delivery_ids"]))
                    result = budget.evaluate(checked_policy, snapshot, checked_request)
                    if result["status"] != "accepted" or result["execution_status"] != "available":
                        raise SwarmBudgetRefusal("budget_decision_blocked", result=result)
                    prefix_digest = self._prefix_digest(raw_map)
                    decision = budget.bind_decision(
                        policy=checked_policy, snapshot=snapshot, request=checked_request,
                        result=result, journal_revision=workspace_state["revision"],
                        journal_prefix_sha256=prefix_digest)
                    budget_admission = {
                        "schema": _BUDGET_ADMISSION_SCHEMA, "status": "reserved",
                        "policy": checked_policy, "snapshot": snapshot,
                        "request": checked_request, "decision": decision,
                        "request_identity_sha256": budget.request_identity_sha256(checked_request),
                        "facts_identity_sha256": budget.facts_identity_sha256(
                            checked_policy, snapshot, checked_request),
                        "result_sha256": budget.result_sha256(result),
                        "journal_revision": workspace_state["revision"],
                        "journal_prefix_sha256": prefix_digest,
                    }
                except budget.WorkspaceBudgetError as exc:
                    raise SwarmBudgetRefusal("budget_binding_required") from exc
                payload["budget_admission"] = copy.deepcopy(budget_admission)
                checked["payload"] = payload
            candidate = payload["claim"]
            if candidate["worker_id"] != worker_id:
                raise SwarmConflictError("claim worker does not match authenticated worker")
            claim_payload = {
                "task_id": candidate["task_id"],
                "claim_id": candidate["claim_id"],
                "attempt": candidate["attempt"],
                "lease_generation": candidate["lease_generation"],
                "lease_expires_at_ms": now + lease_ms,
                "request_sha256": candidate["request_sha256"],
            }
            # Reuse the coordinator's existing claim admission rules, including
            # clock/lease bounds, without writing its standalone claim event.
            info = self._claim_common(state, claim_payload, worker_id, now_ms=now)
            if candidate["claim_id"] in state["claims"]:
                raise SwarmConflictError("claim id already exists")
            event_bytes = admission._canonical_event_bytes(checked)
            event_digest = hashlib.sha256(event_bytes).hexdigest()
            frame = make_frame(
                "claim", run_id=self.run_id, message_id=frame_id,
                sent_at_ms=now, payload=claim_payload)
            existing_seen = state.get("seen", {}).get(frame_id)
            if existing_seen is not None:
                if existing_seen.get("frame_sha256") != frame_sha256(frame):
                    raise SwarmConflictError("admission message id was reused")
                raise SwarmConflictError("admission message id already has a different operation")
            response = self._response(
                "admitted", task_id=info["task_id"], claim_id=candidate["claim_id"],
                attempt=info["attempt"], lease_generation=info["lease_generation"],
                lease_expires_at_ms=info["lease_expires_at_ms"],
                workspace_event_sha256=event_digest)
            if budget_admission is not None:
                response["budget_admission"] = copy.deepcopy(budget_admission)
            record = {
                "event": _WORKSPACE_TURN_EVENT,
                "admission_id": admission_id,
                "task_id": info["task_id"], "claim_id": candidate["claim_id"],
                "worker_id": worker_id, "attempt": info["attempt"],
                "lease_generation": info["lease_generation"],
                "lease_expires_at_ms": info["lease_expires_at_ms"],
                "request_sha256": info["request_sha256"],
                "request_identity_sha256": request_identity,
                "workspace_event_sha256": event_digest,
                "workspace_event": copy.deepcopy(checked),
            }
            return self._remembered_append(owner, state, frame, response, record)

    def renew(self, worker_id: str, claim_id: str, lease_generation: int, *,
              lease_ms: int = 30_000, message_id: str | None = None) -> dict[str, Any]:
        now = _now_ms(self.clock)
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or not 1 <= lease_ms <= MAX_LEASE_MS:
            raise SwarmProtocolError("invalid lease duration")
        frame = make_frame("renew", run_id=self.run_id,
                           message_id=message_id or _message_id("renew"), sent_at_ms=now,
                           payload={"claim_id": claim_id, "lease_generation": lease_generation,
                                    "lease_expires_at_ms": now + lease_ms})
        return self.apply_frame(frame, worker_id=worker_id)

    def send_message(self, worker_id: str, recipient: str, content: str, *,
                     reply_to: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        if not isinstance(content, str) or not content or len(content) > 12_000:
            raise SwarmProtocolError("invalid message content")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        preview = _bounded_reason(content[:512], "message preview")
        payload: dict[str, Any] = {"from": worker_id, "to": recipient,
                                   "content_sha256": digest, "content_chars": len(content),
                                   "preview": preview}
        if reply_to is not None:
            payload["reply_to"] = reply_to
        frame = make_frame("send_message", run_id=self.run_id,
                           message_id=message_id or _message_id("message"),
                           sent_at_ms=_now_ms(self.clock), payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def publish_artifact(self, worker_id: str, *, task_id: str, claim_id: str,
                         attempt: int, lease_generation: int, request_sha256: str,
                         artifact_id: str, sha256: str, bytes_count: int,
                         media_type: str, relative_path: str | None = None,
                         opaque_uri: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"task_id": task_id, "claim_id": claim_id,
                                   "attempt": attempt, "lease_generation": lease_generation,
                                   "request_sha256": request_sha256, "artifact_id": artifact_id,
                                   "sha256": sha256, "bytes": bytes_count, "media_type": media_type}
        if relative_path is not None:
            payload["relative_path"] = relative_path
        if opaque_uri is not None:
            payload["opaque_uri"] = opaque_uri
        frame = make_frame("publish_artifact", run_id=self.run_id,
                           message_id=message_id or _message_id("artifact"),
                           sent_at_ms=_now_ms(self.clock), payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def complete(self, worker_id: str, *, task_id: str, claim_id: str, attempt: int,
                 lease_generation: int, request_sha256: str, envelope_sha256: str,
                 message_id: str | None = None) -> dict[str, Any]:
        frame = make_frame("complete", run_id=self.run_id,
                           message_id=message_id or _message_id("complete"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"task_id": task_id, "claim_id": claim_id,
                                    "attempt": attempt, "lease_generation": lease_generation,
                                    "request_sha256": request_sha256,
                                    "envelope_sha256": envelope_sha256})
        return self.apply_frame(frame, worker_id=worker_id)

    def fail(self, worker_id: str, *, task_id: str, claim_id: str, attempt: int,
             lease_generation: int, request_sha256: str, envelope_sha256: str,
             reason: str = "worker_failed", message_id: str | None = None) -> dict[str, Any]:
        frame = make_frame("fail", run_id=self.run_id,
                           message_id=message_id or _message_id("fail"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"task_id": task_id, "claim_id": claim_id,
                                    "attempt": attempt, "lease_generation": lease_generation,
                                    "request_sha256": request_sha256,
                                    "envelope_sha256": envelope_sha256, "reason": reason})
        return self.apply_frame(frame, worker_id=worker_id)

    def cancel(self, task_id: str, *, reason: str = "operator_requested",
               message_id: str | None = None) -> dict[str, Any]:
        state, _ = self._load()
        task = state["tasks"].get(_id(task_id, "task id"))
        if task is None:
            raise SwarmCoordinatorError("unknown task")
        active = [state["claims"][claim_id] for claim_id in task["attempts"]
                  if state["claims"][claim_id]["status"] == "active"]
        if not active:
            raise SwarmConflictError("task has no active claim")
        claim = active[-1]
        frame = make_frame("cancel_requested", run_id=self.run_id,
                           message_id=message_id or _message_id("cancel"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"claim_id": claim["claim_id"],
                                    "lease_generation": claim["lease_generation"],
                                    "reason": reason})
        return self.apply_frame(frame, worker_id="coordinator")

    def acknowledge_indeterminate(self, task_id: str, *, allow_retry: bool,
                                  reason: str, human_confirmed: bool = False) -> dict[str, Any]:
        if not isinstance(allow_retry, bool) or not isinstance(human_confirmed, bool):
            raise SwarmCoordinatorError("indeterminate recovery flags must be boolean")
        if not human_confirmed:
            raise SwarmCoordinatorError("indeterminate recovery requires human confirmation")
        task_id = _id(task_id, "task id")
        reason = _bounded_reason(reason)
        with self._mutation() as (owner, state, _torn):
            if state.get("closed"):
                raise SwarmConflictError("swarm is closed")
            task = state["tasks"].get(task_id)
            if task is None:
                raise SwarmCoordinatorError("unknown task")
            claims = [state["claims"][claim_id] for claim_id in task["attempts"]]
            uncertain = [claim for claim in claims if claim["status"] in {"active", "indeterminate"}]
            if not uncertain:
                raise SwarmConflictError("task has no indeterminate claim")
            latest = uncertain[-1]
            event = "retry_authorized" if allow_retry else "task_blocked"
            # A recovery decision is not permission to append the same
            # uncertainty marker repeatedly.  Once the claim is indeterminate
            # the durable prefix already records that possible spend; only the
            # explicit human decision is new evidence.
            if latest["status"] == "indeterminate":
                if (allow_retry and task.get("retry_authorized")) or (
                        not allow_retry and task.get("terminal") == "blocked"):
                    return self._response(
                        "retry_authorized" if allow_retry else "blocked",
                        task_id=task_id,
                        **({} if allow_retry else {"uncertain_spend": True}))
            else:
                indeterminate_record = {
                    "event": "claim_indeterminate", "task_id": task_id,
                    "claim_id": latest["claim_id"],
                    "attempt": latest["attempt"],
                    "lease_generation": latest["lease_generation"],
                    "request_sha256": latest["request_sha256"],
                    "reason": reason,
                    "human_confirmed": True,
                    "message_id": _message_id("recovery")}
                projected = copy.deepcopy(state)
                self._apply_record(projected, indeterminate_record)
                fingerprint = self._append_with_state(owner, indeterminate_record, state=state)
                projected["_prefix_fingerprint"] = fingerprint
                state.clear()
                state.update(projected)
                latest = state["claims"][latest["claim_id"]]
            decision_record = {"event": event, "task_id": task_id,
                               "claim_id": latest["claim_id"],
                               "reason": reason,
                               "human_confirmed": True,
                               "message_id": _message_id("recovery")}
            projected = copy.deepcopy(state)
            self._apply_record(projected, decision_record)
            fingerprint = self._append_with_state(owner, decision_record, state=state)
            projected["_prefix_fingerprint"] = fingerprint
            state.clear()
            state.update(projected)
            if allow_retry:
                return self._response("retry_authorized", task_id=task_id,
                                      previous_claim_id=latest["claim_id"])
            return self._response("blocked", task_id=task_id, uncertain_spend=True)

    def close(self) -> dict[str, Any]:
        with self._mutation() as (owner, state, _torn):
            if state["closed"]:
                return self._response("closed", run_id=self.run_id)
            live = [claim for claim in state["claims"].values() if claim["status"] == "active"]
            if live:
                raise SwarmConflictError("cannot close a swarm with active claims")
            unfinished = [task for task in state["tasks"].values()
                          if task["terminal"] not in {"completed", "failed", "cancelled", "blocked"}]
            if unfinished:
                raise SwarmConflictError("cannot close a swarm with unfinished tasks")
            if self._workspace_reserve(state.get("workspace")):
                raise SwarmConflictError("cannot close with unresolved workspace delivery obligations")
            self._append_with_state(owner, {"event": "run_closed", "message_id": _message_id("close")},
                                    state=state)
            return self._response("closed", run_id=self.run_id)

    # --- read-only projections -------------------------------------------

    def status(self) -> dict[str, Any]:
        try:
            state, torn = self._load()
        except SwarmNotFoundError:
            return {"status": "missing", "run_id": self.run_id}
        now = _now_ms(self.clock)
        tasks = []
        uncertain = False
        for task in state["tasks"].values():
            claims = [state["claims"][claim_id] for claim_id in task["attempts"]]
            latest = claims[-1] if claims else None
            task_status = task["terminal"] or ("expired" if latest and latest["status"] == "active"
                                                and latest["lease_expires_at_ms"] <= now else
                                                "claimed" if latest and latest["status"] == "active" else "pending")
            if task_status in {"expired", "indeterminate"}:
                uncertain = True
            tasks.append({"task_id": task["task_id"],
                          "request_sha256": task["request_sha256"],
                          "status": task_status,
                          "attempts": len(claims),
                          "active_claim": ({"claim_id": latest["claim_id"],
                                             "worker_id": latest["worker_id"],
                                             "lease_generation": latest["lease_generation"],
                                             "lease_expires_at_ms": latest["lease_expires_at_ms"],
                                             "cancel_requested": latest["cancel_requested"]}
                                            if latest and latest["status"] == "active" else None)})
        workspace_report = None
        workspace = state.get("workspace")
        if workspace is not None:
            import _workspace_admission as admission
            import _workspace_state as projection
            inbox = projection.inbox_summary(workspace)
            unsettled, effects = 0, set()
            for delivery_id, delivery in workspace["deliveries"].items():
                if admission.outstanding_obligations(
                        delivery["state"], delivery_id, certainty=delivery["certainty"],
                        inherited_uncertainty=delivery.get("inherited_uncertainty", ())):
                    unsettled += 1
                effects.update(key for key, value in delivery["certainty"].items() if value == "unknown")
                effects.update(delivery.get("inherited_uncertainty", ()))
            uncertain = uncertain or "spend" in effects
            workspace_report = {
                "delivery_count": len(workspace["deliveries"]),
                "unsettled_delivery_count": unsettled,
                "uncertain_effects": sorted(effects),
                "closure_ready": (not state["closed"] and not torn and not unsettled and inbox["closure_ready"]
                                  and all(task["status"] in {"completed", "failed", "cancelled", "blocked"}
                                          for task in tasks)
                                  and not any(claim["status"] == "active" for claim in state["claims"].values())),
            }
            if workspace.get("supervisor_endpoints") or workspace.get("inbox_deliveries"):
                workspace_report["inbox"] = inbox
            resolutions = workspace.get("effect_resolutions", {})
            if resolutions:
                workspace_report["effect_resolution"] = {
                    "count": len(resolutions), "resolution_kind": "fixed_worker_fixture/v1",
                    "qualification": "simulated"}
            if state.get("economics") is not None:
                outstanding_bytes, outstanding_records = self._economics_reservation_totals(state)
                accounting_records = [
                    item["economics_settlement"]["accounting"]
                    for item in state.get("admissions", {}).values()
                    if isinstance(item, Mapping)
                    and isinstance(item.get("economics_settlement"), Mapping)
                    and isinstance(item["economics_settlement"].get("accounting"), Mapping)
                ]
                workspace_report["economics"] = {
                    "enabled": True,
                    "reservation": {
                        "outstanding_bytes": outstanding_bytes,
                        "outstanding_records": outstanding_records,
                    },
                    "settled_count": len(accounting_records),
                    # This projector intentionally drops attempt IDs, hashes,
                    # prompt material, provider handles and raw usage detail.
                    "accounting": _submission_accounting.public_summary(accounting_records),
                }
        if state["closed"]:
            status = "closed"
        elif any(task["status"] == "expired" for task in tasks):
            status = "uncertain_spend"
        elif tasks and all(task["status"] in {"completed", "failed", "cancelled", "blocked"} for task in tasks):
            status = "settlement_required" if workspace_report and (
                workspace_report["unsettled_delivery_count"] or not workspace_report.get("inbox", {}).get("closure_ready", True)) else "completed"
        elif any(task["status"] in {"claimed"} for task in tasks):
            status = "running"
        else:
            status = "prepared"
        result = {"status": status, "run_id": self.run_id,
                "project_root_sha256": state["project_root_sha256"],
                "roster_definition_sha256": state["roster_definition_sha256"],
                "max_attempts": state["max_attempts"],
                "generation": max((record.get("generation", 0) for record in self._read_records()[0]), default=0),
                "tasks": tasks[:MAX_STATUS_TASKS],
                "worker_count": len(state["workers"]),
                "message_count": min(len(state["messages"]), MAX_STATUS_MESSAGES),
                "artifact_count": min(len(state["artifacts"]), MAX_STATUS_ARTIFACTS),
                "uncertain_spend": uncertain or any(
                    state["tasks"][task["task_id"]].get("uncertain_spend", False)
                    for task in tasks),
                "torn_tail": bool(torn)}
        if workspace_report is not None:
            result["workspace"] = workspace_report
        return result

    def events(self) -> list[dict[str, Any]]:
        """Return the bounded public event projection; never raw worker text."""
        records, _torn = self._read_records()
        self._state(records, allow_empty=True)
        public: list[dict[str, Any]] = []
        for record in records:
            projected = dict(record)
            if (projected.get("event") == "artifact_published"
                    and "artifact_sha256" in projected):
                projected["sha256"] = projected.pop("artifact_sha256")
            value = {key: projected[key] for key in (
                "generation", "ts", "event", "message_id", "task_id", "claim_id",
                "worker_id", "attempt", "lease_generation", "lease_expires_at_ms",
                "request_sha256", "project_root_sha256", "roster_definition_sha256",
                "artifact_id", "sha256", "bytes", "media_type", "content_sha256",
                "content_chars", "preview", "to", "from", "status", "reason",
                "admission_id", "workspace_event_sha256")
                     if key in projected}
            public.append(value)
        return public

def _validate_preparation_identity(first: Mapping[str, Any], run_id: str) -> None:
    """Shared exact schema/identity boundary for ordinary and rebuild readers."""
    if type(first.get("schema")) is not int or first["schema"] != 1:
        raise SwarmCorruptError("unsupported swarm preparation schema")
    try:
        prepared_run_id = validate_run_id(first.get("run_id"))
    except (TypeError, ValueError):
        raise SwarmCorruptError("invalid swarm preparation identity") from None
    if prepared_run_id != run_id:
        raise SwarmCorruptError("swarm preparation identity differs from run directory")
    if "economics" in first:
        try:
            _validate_economics_config(first["economics"])
        except (KeyError, TypeError, ValueError, SwarmProtocolError):
            raise SwarmCorruptError("invalid persisted economics fence") from None


def _rebuild_identity_preflight(records: list[dict[str, Any]], run_id: str) -> None:
    """Reject ambiguous identities before the canonical reducer is invoked."""
    if not records:
        raise SwarmNotFoundError("swarm journal is not initialized")
    prepared = [record for record in records
                if record.get("event") == "swarm_prepared"]
    if len(prepared) != 1 or records[0] is not prepared[0]:
        raise SwarmCorruptError("swarm journal has no unique leading preparation")
    first = prepared[0]
    _validate_preparation_identity(first, run_id)

    seen_journal_messages: set[str] = set()
    seen_claim_creations: set[str] = set()
    seen_artifacts: set[str] = set()
    seen_admissions: set[str] = set()
    seen_workspace_operations: set[str] = set()

    def unique(value: Any, seen: set[str], label: str) -> None:
        if value is None:
            return
        try:
            identity = _id(value, label)
        except SwarmProtocolError:
            raise SwarmCorruptError("swarm journal contains an invalid identity") from None
        if identity in seen:
            raise SwarmCorruptError("swarm journal contains a duplicate identity")
        seen.add(identity)

    for record in records:
        unique(record.get("message_id"), seen_journal_messages, "journal message id")
        event = record.get("event")
        if event in {"claim_granted", "claim_reclaimed", _WORKSPACE_TURN_EVENT}:
            unique(record.get("claim_id"), seen_claim_creations, "claim id")
        if event == "artifact_published":
            unique(record.get("artifact_id"), seen_artifacts, "artifact id")
        if event == _WORKSPACE_TURN_EVENT:
            unique(record.get("admission_id"), seen_admissions, "admission id")
        if event in {_WORKSPACE_TURN_EVENT, _WORKSPACE_WRAPPER_EVENT}:
            nested = record.get("workspace_event")
            if isinstance(nested, Mapping):
                unique(nested.get("operation_key"), seen_workspace_operations,
                       "workspace operation id")


def _swarm_v1_compatibility(raw_map: Mapping[str, bytes], *,
                            allow_legacy: bool) -> dict[str, Any]:
    """Describe only the two historical swarm encodings accepted by source."""
    features: list[str] = []
    for raw in raw_map.values():
        if re.search(rb"\r(?!\n)", raw):
            raise SwarmCorruptError("unsupported swarm journal line encoding")
        if b"\r\n" in raw and "crlf_segments" not in features:
            features.append("crlf_segments")
        if raw and not raw.endswith(b"\n") \
                and "unterminated_valid_final_line" not in features:
            features.append("unterminated_valid_final_line")
    if features and not allow_legacy:
        raise SwarmCorruptError("legacy swarm journal encoding is disabled")
    return {
        "schema": "summon.swarm/v1",
        "mode": "legacy_v1_encoding" if features else "current_v1_encoding",
        "features": features,
        "launch_authority": False,
    }


def rebuild_projection_from_journal(
        run_dir: str | os.PathLike[str], *, expected_run_id: str | None = None,
        expected_project_root_sha256: str | None = None,
        allow_legacy: bool = True, require_complete: bool = True) -> dict[str, Any]:
    """Rebuild one detached private swarm projection from stable journal bytes.

    This seam is deliberately read-only and swarm-specific. It does not acquire
    an owner, repair or synchronize a prefix, publish an index, import an
    executor, contact a provider, or grant launch authority. A caller that later
    persists an index needs a separate owner-gated, source-digest-bound API.
    """
    if type(allow_legacy) is not bool or type(require_complete) is not bool:
        raise SwarmCoordinatorError("invalid swarm projection rebuild option")
    if not require_complete:
        raise SwarmCoordinatorError("incomplete swarm projection rebuild is unsupported")
    try:
        supplied = os.fspath(run_dir)
    except TypeError:
        raise SwarmCoordinatorError("invalid swarm run directory") from None
    if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
        raise SwarmCoordinatorError("invalid swarm run directory")
    raw_path = Path(supplied).expanduser()
    run_id = raw_path.name
    try:
        validate_run_id(run_id)
        if expected_run_id is not None:
            validate_run_id(expected_run_id)
    except (TypeError, ValueError):
        raise SwarmCoordinatorError("invalid expected swarm identity") from None
    if expected_run_id is not None and expected_run_id != run_id:
        raise SwarmCorruptError("expected swarm identity differs from run directory")
    if expected_project_root_sha256 is not None:
        try:
            _digest(expected_project_root_sha256, "expected project root digest")
        except SwarmProtocolError:
            raise SwarmCoordinatorError("invalid expected project identity") from None

    try:
        coordinator = SwarmCoordinator(str(raw_path.parent), run_id)
        if Path(coordinator.run_dir) != raw_path.resolve():
            raise SwarmCorruptError("swarm run directory identity is ambiguous")
        tagged, torn, raw_map = coordinator._strict_snapshot()
    except SwarmCoordinatorError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SwarmCoordinatorError("cannot read swarm journal for projection rebuild") from None
    if torn:
        raise SwarmCorruptError("swarm projection rebuild refuses a torn journal")
    if len(tagged) > MAX_JOURNAL_RECORDS:
        raise SwarmCorruptError("swarm journal prefix exceeds the record bound")
    if sum(len(raw) for raw in raw_map.values()) > MAX_JOURNAL_BYTES:
        raise SwarmCorruptError("swarm journal prefix exceeds the byte bound")

    records = [record for _generation, record in tagged]
    _rebuild_identity_preflight(records, run_id)
    compatibility = _swarm_v1_compatibility(raw_map, allow_legacy=allow_legacy)
    try:
        projection = coordinator._state(records)
    except SwarmCoordinatorError:
        raise
    except (KeyError, TypeError, ValueError, SwarmProtocolError):
        raise SwarmCorruptError("invalid swarm journal projection") from None
    if (expected_project_root_sha256 is not None
            and projection.get("project_root_sha256") != expected_project_root_sha256):
        raise SwarmCorruptError("expected project identity differs from swarm preparation")

    detached = copy.deepcopy(projection)
    segments = []
    for name, raw in raw_map.items():
        match = re.fullmatch(r"journal-g(\d+)\.jsonl", name)
        if match is None:
            raise SwarmCorruptError("invalid swarm journal segment identity")
        segments.append({
            "name": name,
            "generation": int(match.group(1)),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    segments.sort(key=lambda item: (item["generation"], item["name"]))
    try:
        projection_bytes = _canonical_json(
            detached, "swarm projection", maximum=MAX_PROJECTION_BYTES)
    except SwarmProtocolError:
        # The projection is an operator-facing digest, not a place to expose
        # serializer details or attacker-controlled field names.  Keep the
        # public corruption class stable when the bounded projection cannot
        # be encoded.
        raise SwarmCorruptError(
            "swarm projection cannot be encoded within the byte bound") from None
    return {
        "schema": PROJECTION_REBUILD_SCHEMA,
        "visibility": "private_operator",
        "run_id": run_id,
        "project_root_sha256": detached["project_root_sha256"],
        "source": {
            "segments": segments,
            "prefix_sha256": coordinator._prefix_digest(raw_map),
            "record_count": len(records),
            "last_generation": max((generation for generation, _record in tagged),
                                   default=0),
            "workspace_revision": (detached.get("workspace") or {}).get("revision"),
            "complete": True,
            "torn_tail": False,
            "stable": True,
        },
        "compatibility": compatibility,
        "projection_sha256": hashlib.sha256(projection_bytes).hexdigest(),
        "projection": detached,
        "launch_authority": False,
    }


def run_command(args) -> int:
    """Run the provider-inert ``summon swarm`` management surface.

    The CLI intentionally exposes lifecycle coordination, not provider launch.
    Host adapters can use :class:`SwarmCoordinator` directly and must still
    prove their own process-tree, permission, and billing contracts.
    """
    action = getattr(args, "swarm_action", None)
    root = getattr(args, "swarm_dir", None)
    if not root:
        root = os.path.join(os.path.abspath(getattr(args, "cwd", None) or os.getcwd()),
                            ".agents", "swarm")
    run_id = getattr(args, "swarm_run_id", None)
    try:
        if not isinstance(action, str) or not action:
            raise SwarmCoordinatorError("swarm action is required")
        if not isinstance(run_id, str) or not run_id:
            raise SwarmCoordinatorError("swarm run id is required")
        if action == "create":
            task_file = getattr(args, "swarm_tasks", None)
            if not task_file:
                raise SwarmCoordinatorError("swarm create requires --swarm-tasks FILE")
            task_path = Path(task_file).expanduser()
            if task_path.stat().st_size > 2 * 1024 * 1024:
                raise SwarmCoordinatorError("swarm task file is too large")
            with task_path.open(encoding="utf-8") as handle:
                tasks = json.load(handle)
            if not isinstance(tasks, list):
                raise SwarmCoordinatorError("swarm task file must contain a JSON array")
            coordinator = SwarmCoordinator.create(
                root, run_id,
                project_root_sha256=getattr(args, "swarm_project_root_sha256", None),
                roster_definition_sha256=getattr(args, "swarm_roster_sha256", None),
                tasks=tasks,
                max_attempts=getattr(args, "swarm_max_attempts", None) or 1,
            )
            result = coordinator.status()
        else:
            coordinator = SwarmCoordinator(root, run_id)
            if action == "status":
                result = coordinator.status()
            elif action == "events":
                result = {"status": "ok", "run_id": run_id, "events": coordinator.events()}
            elif action == "register":
                result = coordinator.register_worker(
                    getattr(args, "swarm_worker", None),
                    worker_instance_id=getattr(args, "swarm_instance", None),
                    capabilities=[], permission_ceiling="read-only")
            elif action == "claim":
                result = coordinator.claim(
                    getattr(args, "swarm_worker", None), getattr(args, "swarm_task_id", None),
                    request_sha256=getattr(args, "swarm_request_sha256", None),
                    lease_ms=getattr(args, "swarm_lease_ms", None) or 30_000)
            elif action == "renew":
                result = coordinator.renew(
                    getattr(args, "swarm_worker", None), getattr(args, "swarm_claim_id", None),
                    getattr(args, "swarm_lease_generation", None),
                    lease_ms=getattr(args, "swarm_lease_ms", None) or 30_000)
            elif action == "cancel":
                result = coordinator.cancel(getattr(args, "swarm_task_id", None),
                                             reason=getattr(args, "swarm_reason", None) or "operator_requested")
            elif action == "close":
                result = coordinator.close()
            else:
                raise SwarmCoordinatorError(f"unknown swarm action {action!r}")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, SwarmCoordinatorError,
            SwarmProtocolError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error_kind": type(exc).__name__},
                         ensure_ascii=False))
        return 1


__all__ = ["SwarmCoordinator", "SwarmCoordinatorError", "SwarmNotFoundError",
           "SwarmCorruptError", "SwarmConflictError", "SwarmIndeterminateError",
           "rebuild_projection_from_journal", "run_command"]
