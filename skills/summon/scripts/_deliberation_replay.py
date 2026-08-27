"""Fail-closed replay validation for the deliberation preview.

This module reconstructs only durable, schedule-bound facts.  It intentionally
does not open a run directory, import an executor, or decide anything from
model prose or projection files.  Callers obtain ``(segment_generation,
record)`` pairs from :func:`_rundir.journal_read_tagged` and pass the immutable
receipt separately.

The result is a checkpoint suitable for a future resume implementation.  This
slice does not resume or contact a provider: an unmatched physical start is
reported as uncertain spend and every restored attempt remains unlaunchable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_REPLAY_RECORDS = 10_000
MAX_REPLAY_BYTES = 4 * 1024 * 1024
MAX_SCHEDULE_ROUNDS = 10
MAX_SIGNED64 = (1 << 63) - 1
MAX_HUMAN_COMMANDS = 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+() -]{0,159}$")


class ReplayError(ValueError):
    """The durable record set cannot be reconstructed safely."""


@dataclass(frozen=True)
class ReplayTurn:
    decision_id: str
    seat_id: str
    turn_id: str
    turn_ordinal: int
    request_digest: str


@dataclass(frozen=True)
class ReplayAttempt:
    attempt_id: str
    generation: int
    decision_id: str
    seat_id: str
    turn_id: str
    turn_ordinal: int
    launch_spec_digest: str
    phase: str
    ballot_valid: bool | None = None

    @property
    def uncertain(self) -> bool:
        return self.phase != "finished"


@dataclass(frozen=True)
class ReplayBallot:
    decision_id: str
    seat_id: str
    turn_id: str
    attempt_id: str
    turn_ordinal: int
    decision: str
    option_id: str | None
    confidence: str | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayCommand:
    """One immutable, operator-authored command at a durable boundary."""

    command_id: str
    sequence: int
    action: str


@dataclass(frozen=True)
class ReplayCommandBatch:
    """One atomically journaled human-command boundary."""

    batch_id: str
    source_generation: int
    commands: tuple[ReplayCommand, ...]
    digest: str


@dataclass(frozen=True)
class ReplayCheckpoint:
    """Immutable, recomputed replay projection.

    ``candidate_option`` is derived from accepted ballots, never copied from a
    ``candidate_selected`` event.  ``attempts`` includes committed starts that
    have no durable finish; those entries consume budget and set
    ``uncertain_spend``.
    """

    receipt_sha256: str
    run_id: str
    decision_id: str
    policy_digest: str
    schedule_digest: str
    prior_generation: int
    status: str
    termination_reason: str | None
    candidate_option: str | None
    decision_option: str | None
    attempts: tuple[ReplayAttempt, ...]
    ballots: tuple[ReplayBallot, ...]
    pending_turn: ReplayTurn | None
    next_ordinal: int
    applied_commands: tuple[ReplayCommand, ...]
    pending_commands: tuple[ReplayCommand, ...]
    pending_command_batch: ReplayCommandBatch | None
    transcript_events: tuple[Mapping[str, object], ...]
    uncertain_spend: bool
    digest: str

    @property
    def applied_command_ids(self) -> tuple[str, ...]:
        return tuple(command.command_id for command in self.applied_commands)

    @property
    def applied_command_sequences(self) -> tuple[int, ...]:
        return tuple(command.sequence for command in self.applied_commands)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReplayError("replay value is not canonical JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReplayError(f"invalid replay {label}")
    return value


def _int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReplayError(f"invalid replay {label}")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReplayError(f"invalid replay {label}")
    return value


def _receipt_policy(receipt: Mapping[str, object]) -> tuple[tuple[str, ...], tuple[str, ...], object, int, bool]:
    seats = receipt.get("seat_ids")
    options = receipt.get("option_ids")
    if (not isinstance(seats, list) or not seats or
            any(not isinstance(item, str) for item in seats)):
        raise ReplayError("receipt seat_ids are missing or malformed")
    if (not isinstance(options, list) or len(options) < 2 or
            any(not isinstance(item, str) for item in options)):
        raise ReplayError("receipt option_ids are missing or malformed")
    seat_ids = tuple(_id(item, "seat id") for item in seats)
    option_ids = tuple(_id(item, "option id") for item in options)
    if len(set(seat_ids)) != len(seat_ids) or len(set(option_ids)) != len(option_ids):
        raise ReplayError("receipt ids are not unique")
    # The durable receipt schema has one spelling.  Accepting an alias here
    # while another restore boundary requires the canonical key would let a
    # replayed policy and a restored policy disagree.
    if "quorum_rule" not in receipt:
        raise ReplayError("receipt quorum_rule is missing")
    quorum = receipt.get("quorum_rule")
    if quorum is None:
        raise ReplayError("receipt quorum is missing")
    max_attempts = _int(receipt.get("max_attempts"), "max_attempts", minimum=1)
    if "require_human_approval" not in receipt:
        raise ReplayError("receipt require_human_approval is missing")
    approval = receipt.get("require_human_approval")
    if not isinstance(approval, bool):
        raise ReplayError("receipt approval policy is malformed")
    # Importing the pure kernel is safe and keeps quorum grammar identical to
    # live scheduling.  No provider/executor module is reachable from here.
    try:
        from _deliberation import resolve_quorum
        resolve_quorum(len(seat_ids), quorum)
    except (ImportError, ValueError, TypeError) as exc:
        raise ReplayError("receipt quorum is invalid") from exc
    return seat_ids, option_ids, quorum, max_attempts, approval


def _schedule_value(value: object, label: str, *, maximum: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or
            not 1 <= value <= maximum):
        raise ReplayError(f"receipt {label} is invalid")
    return value


def schedule_digest(rounds: object, deadline_unix_ms: object) -> str:
    """Return the immutable digest for a bounded absolute schedule.

    The schedule is deliberately separate from the deliberation policy digest:
    policy changes and schedule changes are distinct receipt mutations.  Keep
    the canonical object tiny and numeric so no prompt, path, or provider data
    can enter the checkpoint seal.
    """
    rounds = _schedule_value(rounds, "rounds", maximum=MAX_SCHEDULE_ROUNDS)
    deadline_unix_ms = _schedule_value(deadline_unix_ms, "deadline_unix_ms",
                                       maximum=MAX_SIGNED64)
    return _sha({"rounds": rounds, "deadline_unix_ms": deadline_unix_ms})


def receipt_schedule(receipt: Mapping[str, object]) -> tuple[int, int, str]:
    """Validate and return ``(rounds, deadline_unix_ms, schedule_digest)``."""
    if "rounds" not in receipt or "deadline_unix_ms" not in receipt:
        raise ReplayError("receipt schedule fields are missing")
    rounds = _schedule_value(receipt.get("rounds"), "rounds",
                             maximum=MAX_SCHEDULE_ROUNDS)
    deadline = _schedule_value(receipt.get("deadline_unix_ms"),
                               "deadline_unix_ms", maximum=MAX_SIGNED64)
    return rounds, deadline, schedule_digest(rounds, deadline)


def _receipt_metadata(receipt: Mapping[str, object]) -> None:
    """Validate immutable question/timestamp metadata before replaying turns."""
    question_sha = receipt.get("question_sha256")
    if not isinstance(question_sha, str) or not _SHA256_RE.fullmatch(question_sha):
        raise ReplayError("receipt question_sha256 is missing or malformed")
    if "created_at" in receipt:
        created_at = receipt.get("created_at")
        if (isinstance(created_at, bool) or
                not isinstance(created_at, (int, float)) or
                not math.isfinite(float(created_at)) or created_at < 0):
            raise ReplayError("receipt created_at is malformed")
    if "durable_context" in receipt:
        try:
            from _deliberation_context import parse_private_projection
            binding = parse_private_projection(receipt["durable_context"])
        except Exception as exc:
            raise ReplayError("receipt durable context binding is invalid") from exc
        if (binding.run_id != receipt.get("run_id")
                or binding.decision_id != receipt.get("decision_id")
                or binding.state == "stale_refused"
                or binding.routing_authority is not False):
            raise ReplayError("receipt durable context authority is invalid")


def _policy_digest(decision_id: str, seat_ids: tuple[str, ...],
                   option_ids: tuple[str, ...], quorum: object,
                   max_attempts: int, approval: bool) -> str:
    return _sha({
        "decision_id": decision_id, "seat_ids": list(seat_ids),
        "option_ids": list(option_ids), "quorum_rule": quorum,
        "max_attempts": max_attempts,
        "require_human_approval": approval,
    })


def _legal_transition(current: str, target: str) -> bool:
    terminal = {"DECIDED", "UNRESOLVED", "REJECTED", "CANCELLED", "TIMED_OUT",
                "ATTEMPT_BUDGET_EXHAUSTED", "FAILED"}
    legal = {
        "PREPARED": {"RUNNING", "CANCELLED", "TIMED_OUT", "FAILED"},
        "RUNNING": terminal | {"WAITING_HUMAN"},
        "WAITING_HUMAN": {"DECIDED", "REJECTED", "CANCELLED", "TIMED_OUT"},
    }
    return target in legal.get(current, set())


def _quorum_threshold(denominator: int, rule: object) -> int:
    try:
        from _deliberation import resolve_quorum
        return resolve_quorum(denominator, rule)
    except (ImportError, ValueError, TypeError) as exc:
        raise ReplayError("receipt quorum is invalid") from exc


_SAFE_REASONS = frozenset({
    "started", "deadline", "attempt_budget", "max_rounds", "cancelled",
    "snapshot_drift", "adapter_indeterminate", "adapter_error",
    "context_source_drift",
    "approval_required", "consensus", "human_cancel", "human_denied",
    "human_approved", "ownership_lost", "max_attempts", "timeout",
})


def _record_copy(record: Mapping[str, object]) -> dict:
    # Transcript exports are bounded and immutable.  Do not retain caller-owned
    # nested lists/dicts that could be changed after a checkpoint is returned.
    try:
        copied = json.loads(_canonical(record).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError("replay record is not canonical JSON") from exc
    if not isinstance(copied, dict):
        raise ReplayError("replay record is not an object")
    return copied


def _freeze(value: object) -> object:
    """Return a recursively immutable export value for the checkpoint."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


_TRANSCRIPT_FIELDS = {
    "run_prepared": ("event", "schema_version", "generation", "run_id",
                      "receipt_sha256"),
    "state_transition": ("event", "schema_version", "generation", "from", "to",
                          "reason", "decision_option", "recovery_kind",
                          "command_batch_sha256", "command_source_generation"),
    "turn_prepared": ("event", "schema_version", "generation", "decision_id",
                       "seat_id", "turn_id", "turn_ordinal", "request_digest"),
    "attempt_started": ("event", "schema_version", "generation", "attempt_id",
                         "decision_id", "seat_id", "turn_id", "turn_ordinal",
                         "launch_spec_sha256"),
    "attempt_finished": ("event", "schema_version", "generation", "attempt_id",
                          "launch_spec_sha256", "transport_ok", "exit_code",
                          "timed_out", "parser_valid", "ballot_valid"),
    "attempt_model_identity": ("event", "schema_version", "generation",
                                "attempt_id", "model_served", "model_targeted"),
    "ballot_accepted": ("event", "schema_version", "generation", "attempt_id",
                         "seat_id", "turn_id", "turn_ordinal", "decision",
                         "option_id"),
    "ballot_inert": ("event", "schema_version", "generation", "attempt_id",
                     "seat_id", "turn_id"),
    "human_command": ("event", "schema_version", "generation", "command_id",
                       "sequence", "action"),
    "human_command_batch": ("event", "schema_version", "generation", "batch_id",
                            "source_generation", "commands",
                            "command_batch_sha256"),
    "cleanup_receipt": ("event", "schema_version", "generation", "verified", "clean"),
    "advisory_left_behind": ("event", "schema_version", "generation"),
    "journal_repaired": ("event", "schema_version", "generation",
                          "repaired_generation"),
}


def _public_event(record: Mapping[str, object]) -> dict:
    """Project one known event without copying arbitrary nested/private data."""
    event = record.get("event")
    fields = _TRANSCRIPT_FIELDS.get(event)
    if fields is None:
        raise ReplayError("cannot export unknown replay event")
    return {key: record[key] for key in fields if key in record}


def command_batch_sha256(commands: Iterable[ReplayCommand]) -> str:
    """Hash the exact ordered command batch at a durable EOF boundary.

    Only the stable command identity, sequence, and action participate.  The
    helper is shared by replay and the owner-bound recovery writer so a
    recovery transition cannot describe a different batch than the one it
    follows.  It intentionally accepts the typed replay command rather than
    arbitrary mappings.
    """
    values = []
    for command in commands:
        if not isinstance(command, ReplayCommand):
            raise ReplayError("command batch contains an invalid command")
        values.append({"command_id": command.command_id,
                       "sequence": command.sequence,
                       "action": command.action})
    if not values:
        raise ReplayError("command batch is empty")
    return _sha(values)


def _sealed_command_batch(record: Mapping[str, object],
                          generation: int) -> ReplayCommandBatch:
    """Validate one all-or-nothing command-batch journal record."""
    batch_id = _id(record.get("batch_id"), "command batch id")
    source_generation = _int(record.get("source_generation"),
                             "command source generation", minimum=1)
    if source_generation != generation:
        raise ReplayError("command batch crossed an owner generation")
    raw_commands = record.get("commands")
    if (not isinstance(raw_commands, list) or not raw_commands or
            len(raw_commands) > MAX_HUMAN_COMMANDS):
        raise ReplayError("command batch commands are missing or unbounded")
    commands = []
    ids: set[str] = set()
    for value in raw_commands:
        if not isinstance(value, Mapping):
            raise ReplayError("command batch entry is malformed")
        if set(value) != {"command_id", "sequence", "action"}:
            raise ReplayError("command batch entry has unsupported fields")
        command_id = _id(value.get("command_id"), "command id")
        sequence = _int(value.get("sequence"), "command sequence", minimum=1)
        action = value.get("action")
        if action not in {"cancel", "approve", "deny"}:
            raise ReplayError("human command action is malformed")
        if command_id in ids:
            raise ReplayError("human command id was replayed")
        ids.add(command_id)
        commands.append(ReplayCommand(command_id, sequence, action))
    sequences = sorted({command.sequence for command in commands})
    if sequences[-1] - sequences[0] + 1 != len(sequences):
        raise ReplayError("human command sequence is not contiguous")
    digest = _sha256(record.get("command_batch_sha256"),
                     "command batch digest")
    if digest != command_batch_sha256(commands):
        raise ReplayError("command batch digest does not match its commands")
    if batch_id != "batch-" + digest[:32]:
        raise ReplayError("command batch id does not match its digest")
    return ReplayCommandBatch(batch_id, source_generation,
                              tuple(commands), digest)


def replay_checkpoint(receipt: Mapping[str, object],
                      tagged_records: Iterable[tuple[int, Mapping[str, object]]],
                      current_owner_generation: int) -> ReplayCheckpoint:
    """Validate and reconstruct a durable deliberation checkpoint.

    ``current_owner_generation`` is the generation being acquired for resume;
    all prior records must therefore be strictly older.  This prevents a stale
    owner from being treated as the current writer.  The function has no file
    or process side effects.
    """
    if not isinstance(receipt, Mapping):
        raise ReplayError("receipt must be an object")
    # Snapshot potentially stateful Mapping implementations once.  All policy
    # parsing and the preparation hash must observe identical receipt bytes.
    receipt = _record_copy(receipt)
    if receipt.get("mode") != "deliberation" or receipt.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError("receipt mode/schema is invalid")
    run_id = _id(receipt.get("run_id"), "run id")
    decision_id = _id(receipt.get("decision_id"), "decision id")
    seat_ids, option_ids, quorum, max_attempts, approval = _receipt_policy(receipt)
    rounds, deadline_unix_ms, schedule_digest_value = receipt_schedule(receipt)
    _receipt_metadata(receipt)
    receipt_sha256 = _sha(receipt)
    policy_digest = _policy_digest(decision_id, seat_ids, option_ids, quorum,
                                   max_attempts, approval)
    if not _SHA256_RE.fullmatch(receipt_sha256):  # defensive, keeps type explicit
        raise ReplayError("receipt hash could not be computed")
    current_generation = _int(current_owner_generation, "owner generation", minimum=1)

    pairs = list(tagged_records)
    if len(pairs) > MAX_REPLAY_RECORDS:
        raise ReplayError("replay journal exceeds record bound")
    if any(not isinstance(pair, tuple) or len(pair) != 2 for pair in pairs):
        raise ReplayError("replay records lack segment provenance")
    raw_size = sum(len(_canonical(record)) for _, record in pairs)
    if raw_size > MAX_REPLAY_BYTES:
        raise ReplayError("replay journal exceeds byte bound")

    records: list[tuple[int, dict]] = []
    last_generation = 0
    for segment_generation, original in pairs:
        generation = _int(segment_generation, "segment generation", minimum=1)
        if generation >= current_generation:
            raise ReplayError("journal record belongs to the current or a newer owner")
        if generation < last_generation:
            raise ReplayError("journal generations are out of order")
        if not isinstance(original, Mapping):
            raise ReplayError("replay record is not an object")
        record = _record_copy(original)
        if (record.get("schema_version") != SCHEMA_VERSION and
                not (record.get("event") == "journal_repaired" and
                     "schema_version" not in record)):
            raise ReplayError("replay record schema is invalid")
        declared = record.get("generation")
        if isinstance(declared, bool) or not isinstance(declared, int) or declared != generation:
            raise ReplayError("replay record generation does not match its segment")
        event = record.get("event")
        if not isinstance(event, str) or len(event) > 64:
            raise ReplayError("replay event is malformed")
        records.append((generation, record))
        last_generation = generation
    if not records or records[0][1].get("event") != "run_prepared":
        raise ReplayError("replay must begin with run_prepared")

    prepared_count = sum(record.get("event") == "run_prepared" for _, record in records)
    if prepared_count != 1:
        raise ReplayError("replay requires exactly one run_prepared record")
    prepared = records[0][1]
    if (prepared.get("run_id") != run_id or
            prepared.get("receipt_sha256") != receipt_sha256):
        raise ReplayError("run_prepared is not bound to the receipt")

    status = "PREPARED"
    termination_reason: str | None = None
    decision_option: str | None = None
    turns: dict[tuple[str, str], ReplayTurn] = {}
    ordinals: dict[int, tuple[str, str]] = {}
    attempts: dict[str, ReplayAttempt] = {}
    finished: set[str] = set()
    accepted: list[ReplayBallot] = []
    ballot_keys: set[str] = set()
    latest_ballot_ordinal: dict[str, int] = {}
    latest_ballot: dict[str, ReplayBallot] = {}
    expected_turn_ordinal = 0
    pending_turn_key: tuple[str, str] | None = None
    command_ids: set[str] = set()
    applied_commands: list[ReplayCommand] = []
    command_batch: list[ReplayCommand] = []
    sealed_command_batch: ReplayCommandBatch | None = None
    command_batch_kind: str | None = None
    last_command_sequence = 0
    transcript: list[Mapping[str, object]] = []

    def current_candidate() -> str | None:
        counts = {option: 0 for option in option_ids}
        for ballot in latest_ballot.values():
            if ballot.decision == "vote" and ballot.option_id is not None:
                counts[ballot.option_id] += 1
        winners = [option for option, count in counts.items()
                   if count >= _quorum_threshold(len(seat_ids), quorum)]
        return winners[0] if len(winners) == 1 else None

    for record_index, (generation, record) in enumerate(records[1:], start=1):
        event = record["event"]
        if command_batch and event not in {
                "human_command", "state_transition", "journal_repaired"}:
            raise ReplayError(
                "human command batch was not immediately consumed by a transition")
        # Once a validated ballot produces a unique quorum candidate, the
        # only legal next durable material is the derived state transition.
        # Accepting another turn/attempt/ballot here would let a crash prefix
        # carry contradictory pending work after recovery.
        if (status == "RUNNING" and current_candidate() is not None and
                event not in {"state_transition", "candidate_selected",
                              "journal_repaired"}):
            raise ReplayError("consensus candidate was not immediately transitioned")
        if status in {"DECIDED", "UNRESOLVED", "REJECTED", "CANCELLED", "TIMED_OUT",
                      "ATTEMPT_BUDGET_EXHAUSTED", "FAILED"} and event not in {
                          "cleanup_receipt", "advisory_left_behind", "journal_repaired"
                      }:
            raise ReplayError("durable material appeared after terminal state")
        if event == "state_transition":
            source, target = record.get("from"), record.get("to")
            if source != status or not isinstance(target, str) or not _legal_transition(status, target):
                raise ReplayError("illegal deliberation state transition")
            reason = record.get("reason")
            expected_reasons = {
                ("PREPARED", "RUNNING"): {"started"},
                ("PREPARED", "CANCELLED"): {"cancelled", "human_cancel"},
                ("PREPARED", "TIMED_OUT"): {"deadline"},
                ("PREPARED", "FAILED"): {"snapshot_drift", "ownership_lost"},
                ("RUNNING", "WAITING_HUMAN"): {"approval_required"},
                ("RUNNING", "DECIDED"): {"consensus"},
                ("RUNNING", "UNRESOLVED"): {"max_rounds"},
                ("RUNNING", "CANCELLED"): {"cancelled", "human_cancel"},
                ("RUNNING", "TIMED_OUT"): {"deadline"},
                ("RUNNING", "ATTEMPT_BUDGET_EXHAUSTED"): {"attempt_budget"},
                ("RUNNING", "FAILED"): {"snapshot_drift", "adapter_indeterminate",
                                          "adapter_error", "context_source_drift",
                                          "ownership_lost"},
                ("WAITING_HUMAN", "DECIDED"): {"human_approved"},
                ("WAITING_HUMAN", "REJECTED"): {"human_denied"},
                ("WAITING_HUMAN", "CANCELLED"): {"cancelled", "human_cancel"},
                ("WAITING_HUMAN", "TIMED_OUT"): {"deadline"},
            }
            if reason not in expected_reasons.get((source, target), set()):
                raise ReplayError("state transition reason does not match its boundary")
            proposed = record.get("decision_option")
            if target == "DECIDED":
                if proposed not in option_ids:
                    raise ReplayError("decided transition lacks an immutable option")
            elif proposed is not None:
                raise ReplayError("non-decision transition carries a decision option")
            has_recovery_kind = "recovery_kind" in record
            has_batch_digest = "command_batch_sha256" in record
            if has_recovery_kind != has_batch_digest:
                raise ReplayError("recovery transition metadata is incomplete")
            if not has_recovery_kind and "command_source_generation" in record:
                raise ReplayError("non-recovery transition carries command metadata")
            recovery_batch_digest = None
            if has_recovery_kind:
                if record.get("recovery_kind") != "human_command_eof":
                    raise ReplayError("recovery transition kind is invalid")
                if not command_batch:
                    raise ReplayError("recovery transition has no command batch")
                recovery_batch_digest = _sha256(
                    record.get("command_batch_sha256"),
                    "command batch digest")
                if recovery_batch_digest != command_batch_sha256(command_batch):
                    raise ReplayError("recovery transition batch does not match commands")
                source_generation = record.get("command_source_generation")
                if sealed_command_batch is not None:
                    if source_generation != sealed_command_batch.source_generation:
                        raise ReplayError("recovery transition command generation differs")
                elif source_generation is not None:
                    raise ReplayError("legacy recovery has unexpected generation metadata")
            if (source == "PREPARED" and target == "CANCELLED"
                    and reason == "human_cancel" and not has_recovery_kind):
                raise ReplayError("prepared human cancellation requires recovery metadata")
            actions = [command.action for command in command_batch]
            if status == "WAITING_HUMAN":
                if target == "TIMED_OUT" and not actions:
                    pass
                elif target == "CANCELLED" and not actions and record.get("reason") == "cancelled":
                    pass
                elif not actions:
                    raise ReplayError("human-gated transition has no durable command")
                else:
                    minimum = min(command.sequence for command in command_batch)
                    at_boundary = [command.action for command in command_batch
                                   if command.sequence == minimum]
                    chosen = ("cancel" if "cancel" in at_boundary else
                              "deny" if "deny" in at_boundary else "approve")
                    expected = {"cancel": "CANCELLED", "approve": "DECIDED",
                                "deny": "REJECTED"}[chosen]
                    expected_reason = {"cancel": "human_cancel",
                                       "approve": "human_approved",
                                       "deny": "human_denied"}[chosen]
                    if target != expected or record.get("reason") != expected_reason:
                        raise ReplayError("human command precedence was not honored")
                    applied_commands.extend(command_batch)
                    command_batch.clear()
                    sealed_command_batch = None
                    command_batch_kind = None
            elif "cancel" in actions:
                minimum = min(command.sequence for command in command_batch)
                if (target != "CANCELLED" or record.get("reason") != "human_cancel"
                        or any(command.action != "cancel" for command in command_batch
                               if command.sequence == minimum)):
                    raise ReplayError("queued human cancellation was not honored")
                applied_commands.extend(command_batch)
                command_batch.clear()
                sealed_command_batch = None
                command_batch_kind = None
            elif command_batch:
                raise ReplayError("human command batch cannot authorize this transition")
            elif status == "RUNNING" and target == "WAITING_HUMAN":
                if not approval or current_candidate() is None:
                    raise ReplayError("approval gate opened without policy and consensus")
            elif status == "RUNNING" and target == "DECIDED":
                if approval or current_candidate() is None:
                    raise ReplayError("decision bypassed approval or consensus")
            if (target in {"UNRESOLVED", "ATTEMPT_BUDGET_EXHAUSTED"}
                    and current_candidate() is not None):
                raise ReplayError("a valid consensus was discarded at a terminal boundary")
            status = target
            if reason not in _SAFE_REASONS:
                raise ReplayError("state transition reason is not a safe enum")
            termination_reason = reason
            if proposed is not None:
                if proposed not in option_ids:
                    raise ReplayError("state transition decision is not an option")
                decision_option = proposed
            transcript.append(_public_event(record))
            continue
        if event == "turn_prepared":
            if status != "RUNNING" or command_batch:
                raise ReplayError("turn material appeared outside a running, uncancelled state")
            if any(key in record for key in ("prompt", "question", "cwd", "path",
                                              "argv", "env", "private_prompt")):
                raise ReplayError("turn journal contains private launch material")
            d = _id(record.get("decision_id"), "turn decision id")
            seat = _id(record.get("seat_id"), "turn seat id")
            turn = _id(record.get("turn_id"), "turn id")
            ordinal = _int(record.get("turn_ordinal"), "turn ordinal")
            digest = _sha256(record.get("request_digest"), "request digest")
            if d != decision_id or seat not in seat_ids:
                raise ReplayError("turn is outside the immutable receipt")
            if ordinal >= rounds * len(seat_ids):
                raise ReplayError("turn ordinal is outside the immutable schedule")
            if seat != seat_ids[ordinal % len(seat_ids)]:
                raise ReplayError("turn seat does not match the immutable schedule")
            if ordinal != expected_turn_ordinal:
                raise ReplayError("turn ordinals must be contiguous from zero")
            if pending_turn_key is not None:
                raise ReplayError("a new turn was prepared while an earlier turn was unresolved")
            key = (seat, turn)
            value = ReplayTurn(d, seat, turn, ordinal, digest)
            if key in turns or ordinal in ordinals:
                raise ReplayError("turn identity or ordinal was reused")
            turns[key] = value
            ordinals[ordinal] = key
            expected_turn_ordinal += 1
            pending_turn_key = key
            transcript.append(_public_event(record))
            continue
        if event == "attempt_started":
            if status != "RUNNING" or command_batch:
                raise ReplayError("attempt material appeared outside a running, uncancelled state")
            attempt_id = _id(record.get("attempt_id"), "attempt id")
            if attempt_id in attempts:
                raise ReplayError("attempt id was reused")
            d = _id(record.get("decision_id"), "attempt decision id")
            seat = _id(record.get("seat_id"), "attempt seat id")
            turn = _id(record.get("turn_id"), "attempt turn id")
            ordinal = _int(record.get("turn_ordinal"), "attempt ordinal")
            spec = _sha256(record.get("launch_spec_sha256"), "launch spec digest")
            if d != decision_id or (seat, turn) not in turns or turns[(seat, turn)].turn_ordinal != ordinal:
                raise ReplayError("attempt is not bound to a prepared turn")
            if any(attempt.turn_id == turn and attempt.phase != "finished"
                   for attempt in attempts.values()):
                raise ReplayError("attempt was replayed for an unresolved turn")
            if any(attempt.turn_id == turn for attempt in attempts.values()):
                raise ReplayError("turn has more than one physical attempt")
            attempts[attempt_id] = ReplayAttempt(attempt_id, generation, d, seat, turn,
                                                 ordinal, spec, "indeterminate")
            transcript.append(_public_event(record))
            continue
        if event == "attempt_finished":
            if status != "RUNNING":
                raise ReplayError("attempt finish appeared outside a running state")
            attempt_id = _id(record.get("attempt_id"), "finished attempt id")
            prior = attempts.get(attempt_id)
            spec = _sha256(record.get("launch_spec_sha256"), "finished spec digest")
            if prior is None or prior.phase == "finished" or prior.generation != generation or prior.launch_spec_digest != spec:
                raise ReplayError("attempt finish does not match its durable start")
            ballot_valid = record.get("ballot_valid")
            if not isinstance(ballot_valid, bool):
                raise ReplayError("attempt finish ballot_valid is malformed")
            transport_ok = record.get("transport_ok")
            timed_out = record.get("timed_out")
            parser_valid = record.get("parser_valid")
            exit_code = record.get("exit_code")
            if (not isinstance(transport_ok, bool) or not isinstance(timed_out, bool)
                    or not isinstance(parser_valid, bool)):
                raise ReplayError("attempt executor evidence is malformed")
            if (exit_code is not None and
                    (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                     or not -(1 << 31) <= exit_code <= (1 << 31) - 1)):
                raise ReplayError("attempt exit_code is malformed")
            if ballot_valid and (not transport_ok or timed_out or not parser_valid):
                raise ReplayError("invalid executor evidence cannot validate a ballot")
            attempts[attempt_id] = ReplayAttempt(prior.attempt_id, prior.generation,
                                                 prior.decision_id, prior.seat_id,
                                                 prior.turn_id, prior.turn_ordinal,
                                                 prior.launch_spec_digest, "finished",
                                                 ballot_valid)
            if pending_turn_key == (prior.seat_id, prior.turn_id):
                pending_turn_key = None
            finished.add(attempt_id)
            transcript.append(_public_event(record))
            continue
        if event == "attempt_model_identity":
            attempt_id = _id(record.get("attempt_id"), "model identity attempt id")
            if attempt_id not in attempts:
                raise ReplayError("model identity lacks a matching attempt")
            for key in ("model_served", "model_targeted"):
                value = record.get(key)
                if value is not None and (not isinstance(value, str)
                                          or _MODEL_RE.fullmatch(value) is None):
                    raise ReplayError("attempt model identity is malformed")
            transcript.append(_public_event(record))
            continue
        if event == "ballot_accepted":
            if status != "RUNNING":
                raise ReplayError("ballot appeared outside a running state")
            attempt_id = _id(record.get("attempt_id"), "ballot attempt id")
            prior = attempts.get(attempt_id)
            if (prior is None or prior.phase != "finished" or
                    prior.generation != generation or prior.ballot_valid is not True):
                raise ReplayError("ballot lacks a matching valid finished attempt")
            if (record.get("seat_id") != prior.seat_id or
                    record.get("turn_id") != prior.turn_id or
                    record.get("turn_ordinal") != prior.turn_ordinal):
                raise ReplayError("ballot binding differs from its attempt")
            if ("decision_id" in record and record.get("decision_id") != decision_id):
                raise ReplayError("ballot decision id differs from its receipt")
            decision = record.get("decision")
            option = record.get("option_id")
            if decision == "vote":
                if option not in option_ids:
                    raise ReplayError("ballot option is not in the receipt")
            elif decision in {"abstain", "undecided"}:
                if option is not None:
                    raise ReplayError("non-vote ballot has an option")
            else:
                raise ReplayError("ballot decision is malformed")
            key = f"{prior.seat_id}:{prior.turn_ordinal}:{attempt_id}"
            if key in ballot_keys or (prior.seat_id in latest_ballot_ordinal and
                                      prior.turn_ordinal <= latest_ballot_ordinal[prior.seat_id]):
                raise ReplayError("duplicate or regressed ballot")
            latest_ballot_ordinal[prior.seat_id] = prior.turn_ordinal
            ballot_keys.add(key)
            ballot = ReplayBallot(decision_id, prior.seat_id, prior.turn_id,
                                  attempt_id, prior.turn_ordinal, decision, option)
            accepted.append(ballot)
            latest_ballot[prior.seat_id] = ballot
            transcript.append(_public_event(record))
            continue
        if event == "human_command":
            if command_batch_kind == "sealed":
                raise ReplayError("legacy command cannot extend a sealed command batch")
            command_id = _id(record.get("command_id"), "command id")
            sequence = _int(record.get("sequence"), "command sequence", minimum=1)
            action = record.get("action")
            if action not in {"cancel", "approve", "deny"}:
                raise ReplayError("human command action is malformed")
            if action in {"approve", "deny"} and status != "WAITING_HUMAN":
                raise ReplayError("approval command was issued outside WAITING_HUMAN")
            if command_id in command_ids:
                raise ReplayError("human command id was replayed")
            if ((not command_batch and sequence != last_command_sequence + 1)
                    or (command_batch and sequence not in {
                        last_command_sequence, last_command_sequence + 1})):
                raise ReplayError("human command sequence is not contiguous")
            command = ReplayCommand(command_id, sequence, action)
            command_ids.add(command_id)
            command_batch.append(command)
            command_batch_kind = "legacy"
            last_command_sequence = sequence
            transcript.append(_public_event(record))
            continue
        if event == "human_command_batch":
            if command_batch:
                raise ReplayError("command batch cannot extend another command boundary")
            sealed = _sealed_command_batch(record, generation)
            if any(command.command_id in command_ids for command in sealed.commands):
                raise ReplayError("human command id was replayed")
            if any(command.action in {"approve", "deny"}
                   for command in sealed.commands) and status != "WAITING_HUMAN":
                raise ReplayError("approval command was issued outside WAITING_HUMAN")
            command_ids.update(command.command_id for command in sealed.commands)
            command_batch.extend(sealed.commands)
            sealed_command_batch = sealed
            command_batch_kind = "sealed"
            last_command_sequence = max(command.sequence for command in sealed.commands)
            transcript.append(_public_event(record))
            continue
        if event in {"attempt_finished", "ballot_inert", "cleanup_receipt",
                     "advisory_left_behind", "journal_repaired"}:
            # These records are audit material.  Their control-bearing fields
            # were already validated above or have no effect on state.
            if event == "cleanup_receipt":
                if (not isinstance(record.get("verified"), bool) or
                        not isinstance(record.get("clean"), bool)):
                    raise ReplayError("cleanup receipt booleans are malformed")
                retained = record.get("retained_resources", [])
                if (not isinstance(retained, list) or len(retained) > 64 or
                        any(not isinstance(item, str) or len(item) > 256
                            for item in retained)):
                    raise ReplayError("cleanup receipt resources are malformed")
            elif event == "advisory_left_behind" and "items" in record:
                items = record.get("items")
                if (not isinstance(items, list) or len(items) > 32 or
                        any(not isinstance(item, str) or len(item) > 256
                            for item in items)):
                    raise ReplayError("advisory resources are malformed")
            elif event == "ballot_inert":
                attempt_id = _id(record.get("attempt_id"), "inert attempt id")
                seat_id = _id(record.get("seat_id"), "inert seat id")
                turn_id = _id(record.get("turn_id"), "inert turn id")
                prior = attempts.get(attempt_id)
                if (seat_id not in seat_ids or prior is None or
                        prior.phase != "finished" or prior.seat_id != seat_id or
                        prior.turn_id != turn_id):
                    raise ReplayError("inert ballot is not bound to a finished attempt")
            elif event == "journal_repaired":
                repaired_generation = record.get("repaired_generation")
                first_in_segment = (record_index == 1 or
                                    records[record_index - 1][0] != generation)
                prior_generations = [prior_generation for prior_generation, _ in
                                     records[:record_index]
                                     if prior_generation < generation]
                newest_prior = max(prior_generations, default=0)
                if (not first_in_segment or isinstance(repaired_generation, bool) or
                        not isinstance(repaired_generation, int) or
                        not 1 <= repaired_generation < generation or
                        repaired_generation != newest_prior):
                    raise ReplayError("journal repair generation is malformed")
            transcript.append(_public_event(record))
            continue
        if event == "candidate_selected":
            # A projection/candidate event is intentionally inert.  Its value
            # is not copied into the checkpoint.
            continue
        # Unknown events must not silently alter a future resume.  A small set
        # of explicitly inert audit records may be added with a schema review;
        # until then fail closed.
        raise ReplayError("unknown replay event")

    latest_ballots: dict[str, ReplayBallot] = {}
    for ballot in accepted:
        latest_ballots[ballot.seat_id] = ballot
    counts = {option: 0 for option in option_ids}
    for ballot in latest_ballots.values():
        if ballot.decision == "vote" and ballot.option_id is not None:
            counts[ballot.option_id] += 1
    try:
        from _deliberation import resolve_quorum
        threshold = resolve_quorum(len(seat_ids), quorum)
    except (ImportError, ValueError, TypeError) as exc:
        raise ReplayError("receipt quorum is invalid") from exc
    winners = [option for option, count in counts.items() if count >= threshold]
    candidate = winners[0] if len(winners) == 1 else None
    if status in {"DECIDED", "WAITING_HUMAN"} and candidate is None:
        raise ReplayError("terminal projection has no recomputed consensus candidate")
    if status == "WAITING_HUMAN" and not approval:
        raise ReplayError("waiting-human state is not enabled by the receipt")
    if status == "DECIDED" and decision_option != candidate:
        raise ReplayError("decision projection disagrees with recomputed candidate")
    if len(attempts) > max_attempts:
        raise ReplayError("durable attempts exceed the receipt budget")
    pending = None
    for value in turns.values():
        # Only a durably prepared turn with no physical start is resumable.
        # An unmatched start is uncertain spend, never a pending relaunch.
        if not any(attempt.turn_id == value.turn_id and
                   attempt.turn_ordinal == value.turn_ordinal
                   for attempt in attempts.values()):
            pending = value
    uncertain = any(attempt.uncertain for attempt in attempts.values())
    next_ordinal = max(ordinals, default=-1) + 1
    if next_ordinal > rounds * len(seat_ids):
        raise ReplayError("replay consumed more turns than the immutable schedule")
    digest_payload = {
        "receipt_sha256": receipt_sha256, "run_id": run_id,
        "policy_digest": policy_digest,
        "schedule_digest": schedule_digest_value,
        "prior_generation": last_generation, "status": status,
        "termination_reason": termination_reason, "candidate_option": candidate,
        "decision_option": decision_option, "attempts": [attempt.__dict__ for attempt in attempts.values()],
        "ballots": [ballot.__dict__ for ballot in accepted],
        "pending_turn": None if pending is None else pending.__dict__,
        "next_ordinal": next_ordinal,
        "applied_commands": [command.__dict__ for command in applied_commands],
        "pending_commands": [command.__dict__ for command in command_batch],
        "pending_command_batch": (None if sealed_command_batch is None else {
            "batch_id": sealed_command_batch.batch_id,
            "source_generation": sealed_command_batch.source_generation,
            "commands": [command.__dict__ for command in sealed_command_batch.commands],
            "digest": sealed_command_batch.digest,
        }),
        "transcript_events": transcript,
        "uncertain_spend": uncertain,
    }
    checkpoint_digest = _sha(digest_payload)
    return ReplayCheckpoint(
        receipt_sha256, run_id, decision_id, policy_digest,
        schedule_digest_value,
        last_generation, status,
        termination_reason, candidate, decision_option,
        tuple(attempts.values()), tuple(accepted), pending, next_ordinal,
        tuple(applied_commands), tuple(command_batch),
        sealed_command_batch,
        tuple(_freeze(record) for record in transcript),
        uncertain, checkpoint_digest,
    )
