"""Owner-bound recovery for an already-journaled human command batch.

This is deliberately a small, provider-inert boundary.  It completes the
durable command transaction when a process died after appending the EOF
an atomic ``human_command_batch`` record but before appending its state transition.  It
does not read an inbox, construct an engine, invoke a scheduler, or contact a
provider.  The held :class:`_rundir.Owner` is the only source of the run
directory, generation, ownership fence, and journal append capability.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Mapping

import _deliberation_replay as replay
import _rundir
from _deliberation import DeliberationPolicy, SCHEMA_VERSION


class RecoveryError(ValueError):
    """The owner-bound journal cannot be recovered safely."""


@dataclass(frozen=True)
class CommandRecoveryReceipt:
    """Bounded, non-sensitive result of one recovery attempt."""

    status: str
    run_id: str
    generation: int
    command_ids: tuple[str, ...] = ()
    target_state: str | None = None
    reason: str | None = None
    command_batch_sha256: str | None = None
    appended: bool = False


_RECOVERY_AUDIT_SUFFIX = frozenset({
    "cleanup_receipt", "advisory_left_behind", "journal_repaired",
})
_MAX_RECOVERY_AUDIT_SUFFIX = 64


def _canonical_snapshot(value: object, label: str) -> tuple[dict, str]:
    if not isinstance(value, Mapping):
        raise RecoveryError(f"{label} must be an object")
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        snapshot = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RecoveryError(f"{label} is not canonical JSON") from exc
    if not isinstance(snapshot, dict):
        raise RecoveryError(f"{label} is not an object")
    return snapshot, hashlib.sha256(raw).hexdigest()


def _policy_fields(policy: DeliberationPolicy) -> dict:
    if not isinstance(policy, DeliberationPolicy):
        raise RecoveryError("recovery requires a DeliberationPolicy")
    return {
        "decision_id": policy.decision_id,
        "seat_ids": list(policy.seat_ids),
        "option_ids": list(policy.option_ids),
        "quorum_rule": policy.quorum_rule,
        "max_attempts": policy.max_attempts,
        "require_human_approval": policy.require_human_approval,
    }


def _validate_receipts(receipt: Mapping[str, object], disk: Mapping[str, object],
                       policy: DeliberationPolicy) -> tuple[dict, str, str]:
    supplied, supplied_sha = _canonical_snapshot(receipt, "supplied receipt")
    stored, stored_sha = _canonical_snapshot(disk, "owner receipt")
    if supplied_sha != stored_sha or supplied != stored:
        raise RecoveryError("supplied receipt differs from the owner receipt")
    expected = _policy_fields(policy)
    if (stored.get("mode") != "deliberation" or
            stored.get("schema_version") != SCHEMA_VERSION or
            any(stored.get(key) != value for key, value in expected.items())):
        raise RecoveryError("receipt policy does not match the configured policy")
    run_id = stored.get("run_id")
    if (not isinstance(run_id, str) or not run_id or
            "/" in run_id or "\\" in run_id or run_id in {".", ".."}):
        raise RecoveryError("receipt run id is malformed")
    return stored, stored_sha, run_id


def _partition_records(owner: _rundir.Owner) -> tuple[list[tuple[int, dict]],
                                                         list[tuple[int, dict]]]:
    """Return ``(prior, current_material)`` after fencing the current segment."""
    try:
        tagged, torn = _rundir.journal_read_tagged(owner.run_dir)
    except (_rundir.JournalCorruptError, OSError) as exc:
        raise RecoveryError("owner journal is not readable") from exc
    if torn:
        raise RecoveryError("owner journal has an unrepaired torn tail")
    prior: list[tuple[int, dict]] = []
    current: list[tuple[int, dict]] = []
    for generation, record in tagged:
        if generation < owner.generation:
            prior.append((generation, record))
        elif generation == owner.generation:
            current.append((generation, record))
        else:
            raise RecoveryError("owner journal contains a future generation")
    if not prior:
        # The initial owner has no immutable predecessor to replay.  Recovery
        # is a takeover operation; it must never reinterpret its own live
        # segment as an already-validated prior prefix.
        raise RecoveryError("owner journal has no prior deliberation prefix")
    if current and current[0][1].get("event") == "journal_repaired":
        repair = current.pop(0)[1]
        repaired_generation = max(generation for generation, _ in prior)
        if (repair.get("generation") != owner.generation or
                repair.get("repaired_generation") != repaired_generation):
            raise RecoveryError("owner journal repair prelude is invalid")
    if len(current) > _MAX_RECOVERY_AUDIT_SUFFIX + 1:
        raise RecoveryError("owner journal recovery audit suffix is too large")
    if current:
        # A current generation may contain one durable recovery transition and
        # only the bounded terminal audit records that ordinary cleanup emits.
        # Any other control material would make a retry ambiguous.
        if (current[0][1].get("event") != "state_transition" or
                current[0][1].get("recovery_kind") != "human_command_eof" or
                any(record.get("event") not in _RECOVERY_AUDIT_SUFFIX
                    for _generation, record in current[1:])):
            raise RecoveryError("owner journal has unsupported current material")
    if current and current[0][1].get("generation") != owner.generation:
        raise RecoveryError("owner recovery generation is invalid")
    return prior, current


def _legacy_command_batch_from_records(records: list[tuple[int, dict]], index: int) -> tuple[replay.ReplayCommand, ...]:
    """Read a completed legacy command prefix for compatibility validation."""
    values: list[replay.ReplayCommand] = []
    cursor = index - 1
    while cursor >= 0 and records[cursor][1].get("event") == "human_command":
        record = records[cursor][1]
        command_id = record.get("command_id")
        sequence = record.get("sequence")
        action = record.get("action")
        if (not isinstance(command_id, str) or not isinstance(sequence, int)
                or isinstance(sequence, bool) or action not in {"cancel", "approve", "deny"}):
            raise RecoveryError("recovery command batch is malformed")
        values.append(replay.ReplayCommand(command_id, sequence, action))
        cursor -= 1
    values.reverse()
    if not values:
        raise RecoveryError("recovery transition has no preceding command batch")
    return tuple(values)


def _sealed_batch_before(records: list[tuple[int, dict]], index: int) -> replay.ReplayCommandBatch | None:
    cursor = index - 1
    # A successor can repair the predecessor's torn tail and then itself die
    # before appending the recovery transition.  Canonical replay validates
    # each repair record's generation and first-in-segment placement; skip
    # those inert audits while locating the still-pending sealed boundary.
    while cursor >= 0 and records[cursor][1].get("event") == "journal_repaired":
        cursor -= 1
    if cursor < 0 or records[cursor][1].get("event") != "human_command_batch":
        return None
    generation, record = records[cursor]
    try:
        return replay._sealed_command_batch(record, generation)
    except replay.ReplayError as exc:
        raise RecoveryError("durable command batch is malformed") from exc


def _already_recovered(prior: list[tuple[int, dict]], checkpoint: replay.ReplayCheckpoint,
                       run_id: str, generation: int) -> CommandRecoveryReceipt | None:
    """Recognize a validated recovery transition from this or a prior owner."""
    if not prior:
        return None
    recovery_indices = [index for index, (_generation, record) in enumerate(prior)
                        if (record.get("event") == "state_transition" and
                            record.get("recovery_kind") == "human_command_eof")]
    if not recovery_indices:
        return None
    recovery_index = recovery_indices[-1]
    last = prior[recovery_index][1]
    suffix = prior[recovery_index + 1:]
    if len(suffix) > _MAX_RECOVERY_AUDIT_SUFFIX or any(
            record.get("event") not in _RECOVERY_AUDIT_SUFFIX
            for _generation, record in suffix):
        return None
    sealed = _sealed_batch_before(prior, recovery_index)
    batch = (sealed.commands if sealed is not None else
             _legacy_command_batch_from_records(prior, recovery_index))
    digest = replay.command_batch_sha256(batch)
    if last.get("command_batch_sha256") != digest:
        raise RecoveryError("durable recovery batch hash is inconsistent")
    if (checkpoint.status != last.get("to") or
            checkpoint.termination_reason != last.get("reason") or
            checkpoint.decision_option != last.get("decision_option")):
        raise RecoveryError("durable recovery projection is inconsistent")
    return CommandRecoveryReceipt(
        status="already_recovered", run_id=run_id, generation=generation,
        command_ids=tuple(command.command_id for command in batch),
        target_state=last.get("to"), reason=last.get("reason"),
        command_batch_sha256=digest, appended=False)


def _validate_current_recovery(prior: list[tuple[int, dict]], current: list[tuple[int, dict]],
                               receipt: Mapping[str, object], owner_generation: int,
                               run_id: str) -> CommandRecoveryReceipt | None:
    """Validate a recovery transition written by the current owner."""
    if not current:
        return None
    recovery = current[0][1]
    if recovery.get("event") != "state_transition":
        raise RecoveryError("owner current material is not a recovery transition")
    # Replay is the authoritative validator.  Validate the durable current
    # segment as an immutable predecessor of a hypothetical successor while
    # preserving every record's actual generation.
    try:
        validation = replay.replay_checkpoint(
            receipt, prior + current, owner_generation + 1)
    except replay.ReplayError as exc:
        raise RecoveryError("current recovery transition is not valid") from exc
    if validation.pending_commands:
        raise RecoveryError("recovery transition did not consume its command batch")
    sealed = _sealed_batch_before(prior, len(prior))
    batch = (sealed.commands if sealed is not None else
             _legacy_command_batch_from_records(prior, len(prior)))
    digest = replay.command_batch_sha256(batch)
    if recovery.get("command_batch_sha256") != digest:
        raise RecoveryError("current recovery batch hash differs from the command batch")
    return CommandRecoveryReceipt(
        status="already_recovered", run_id=run_id, generation=owner_generation,
        command_ids=tuple(command.command_id for command in batch),
        target_state=recovery.get("to"), reason=recovery.get("reason"),
        command_batch_sha256=digest, appended=False)


def recover_pending_human_commands(*, owner: _rundir.Owner,
                                   receipt: Mapping[str, object],
                                   policy: DeliberationPolicy) -> CommandRecoveryReceipt:
    """Complete one durable EOF human-command boundary, or report its status.

    This operation never reevaluates a deadline.  Command admission already
    happened before the journaled ``human_command_batch`` record was written; this
    function only makes the matching state transition durable and idempotent.
    """
    if not isinstance(owner, _rundir.Owner):
        raise RecoveryError("recovery requires a held rundir Owner")
    supplied, _ = _canonical_snapshot(receipt, "supplied receipt")
    disk = _rundir.read_json(os.path.join(owner.run_dir, "receipt.json"))
    if not isinstance(disk, dict):
        raise RecoveryError("owner run has no valid receipt")
    stored, _receipt_sha, run_id = _validate_receipts(supplied, disk, policy)
    if not _rundir.owner_still_current(owner):
        return CommandRecoveryReceipt("ownership_lost", run_id, owner.generation)
    prior, current = _partition_records(owner)
    # This replay call validates the entire immutable prior prefix, including
    # policy, receipt hash, command syntax/sequence, and state precedence.
    try:
        checkpoint = replay.replay_checkpoint(stored, prior, owner.generation)
    except replay.ReplayError as exc:
        raise RecoveryError("owner journal cannot be reconstructed") from exc
    current_result = _validate_current_recovery(
        prior, current, stored, owner.generation, run_id)
    if current_result is not None:
        return current_result
    previous_result = _already_recovered(prior, checkpoint, run_id, owner.generation)
    if previous_result is not None:
        return previous_result
    sealed = checkpoint.pending_command_batch
    if sealed is None and checkpoint.pending_commands:
        raise RecoveryError(
            "legacy unsealed EOF human commands are not recoverable")
    pending = () if sealed is None else tuple(sealed.commands)
    if not pending:
        raise RecoveryError("owner journal has no pending human command batch")
    minimum = min(command.sequence for command in pending)
    boundary = {command.action for command in pending if command.sequence == minimum}
    chosen = ("cancel" if "cancel" in boundary else
              "deny" if "deny" in boundary else "approve")
    if checkpoint.status in {"PREPARED", "RUNNING"}:
        if chosen != "cancel":
            raise RecoveryError("approval or denial is invalid before WAITING_HUMAN")
        target, reason, decision = "CANCELLED", "human_cancel", None
    elif checkpoint.status == "WAITING_HUMAN":
        target, reason = {
            "cancel": ("CANCELLED", "human_cancel"),
            "deny": ("REJECTED", "human_denied"),
            "approve": ("DECIDED", "human_approved"),
        }[chosen]
        decision = checkpoint.candidate_option if chosen == "approve" else None
        if chosen == "approve" and decision is None:
            raise RecoveryError("approval command has no recomputed candidate")
    else:
        raise RecoveryError("human command batch is not recoverable in this state")
    batch_digest = replay.command_batch_sha256(pending)
    if not _rundir.owner_still_current(owner):
        return CommandRecoveryReceipt(
            "ownership_lost", run_id, owner.generation,
            tuple(command.command_id for command in pending), target, reason,
            batch_digest, False)
    event = {
        "event": "state_transition", "schema_version": SCHEMA_VERSION,
        "generation": owner.generation, "from": checkpoint.status,
        "to": target, "reason": reason, "decision_option": decision,
        "recovery_kind": "human_command_eof",
        "command_batch_sha256": batch_digest,
        "command_source_generation": sealed.source_generation,
    }
    try:
        _rundir.journal_append(owner.run_dir, event, owner=owner)
    except _rundir.OwnershipLostError:
        return CommandRecoveryReceipt(
            "ownership_lost", run_id, owner.generation,
            tuple(command.command_id for command in pending), target, reason,
            batch_digest, False)
    return CommandRecoveryReceipt(
        "recovered", run_id, owner.generation,
        tuple(command.command_id for command in pending), target, reason,
        batch_digest, True)
