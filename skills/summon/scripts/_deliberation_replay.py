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
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_REPLAY_RECORDS = 10_000
MAX_REPLAY_BYTES = 4 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    prior_generation: int
    status: str
    termination_reason: str | None
    candidate_option: str | None
    decision_option: str | None
    attempts: tuple[ReplayAttempt, ...]
    ballots: tuple[ReplayBallot, ...]
    pending_turn: ReplayTurn | None
    next_ordinal: int
    applied_command_ids: tuple[str, ...]
    applied_command_sequences: tuple[int, ...]
    transcript_events: tuple[Mapping[str, object], ...]
    uncertain_spend: bool
    digest: str


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
    quorum = receipt.get("quorum_rule", receipt.get("quorum"))
    if quorum is None:
        raise ReplayError("receipt quorum is missing")
    max_attempts = _int(receipt.get("max_attempts"), "max_attempts", minimum=1)
    approval = receipt.get("require_human_approval", False)
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
    "state_transition": ("event", "schema_version", "generation", "from", "to",
                          "reason", "decision_option"),
    "turn_prepared": ("event", "schema_version", "generation", "decision_id",
                       "seat_id", "turn_id", "turn_ordinal", "request_digest"),
    "attempt_started": ("event", "schema_version", "generation", "attempt_id",
                         "decision_id", "seat_id", "turn_id", "turn_ordinal",
                         "launch_spec_sha256"),
    "attempt_finished": ("event", "schema_version", "generation", "attempt_id",
                          "launch_spec_sha256", "transport_ok", "exit_code",
                          "timed_out", "parser_valid", "ballot_valid"),
    "ballot_accepted": ("event", "schema_version", "generation", "attempt_id",
                         "seat_id", "turn_id", "turn_ordinal", "decision",
                         "option_id"),
    "ballot_inert": ("event", "schema_version", "generation", "attempt_id",
                     "seat_id", "turn_id"),
    "human_command": ("event", "schema_version", "generation", "command_id",
                       "sequence", "action"),
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
    if receipt.get("mode") != "deliberation" or receipt.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError("receipt mode/schema is invalid")
    run_id = _id(receipt.get("run_id"), "run id")
    decision_id = _id(receipt.get("decision_id"), "decision id")
    seat_ids, option_ids, quorum, max_attempts, approval = _receipt_policy(receipt)
    receipt_sha256 = _sha(receipt)
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
    command_ids: list[str] = []
    command_sequences: list[int] = []
    command_actions: list[tuple[int, str]] = []
    transcript: list[Mapping[str, object]] = []

    def current_candidate() -> str | None:
        counts = {option: 0 for option in option_ids}
        for ballot in latest_ballot.values():
            if ballot.decision == "vote" and ballot.option_id is not None:
                counts[ballot.option_id] += 1
        winners = [option for option, count in counts.items()
                   if count >= _quorum_threshold(len(seat_ids), quorum)]
        return winners[0] if len(winners) == 1 else None

    for generation, record in records[1:]:
        event = record["event"]
        if status in {"DECIDED", "UNRESOLVED", "REJECTED", "CANCELLED", "TIMED_OUT",
                      "ATTEMPT_BUDGET_EXHAUSTED", "FAILED"} and event not in {
                          "cleanup_receipt", "advisory_left_behind", "journal_repaired"
                      }:
            raise ReplayError("durable material appeared after terminal state")
        if event == "state_transition":
            source, target = record.get("from"), record.get("to")
            if source != status or not isinstance(target, str) or not _legal_transition(status, target):
                raise ReplayError("illegal deliberation state transition")
            actions = [action for _sequence, action in command_actions]
            if status == "WAITING_HUMAN":
                if target == "TIMED_OUT" and not actions:
                    pass
                elif target == "CANCELLED" and not actions and record.get("reason") == "cancelled":
                    pass
                elif not actions:
                    raise ReplayError("human-gated transition has no durable command")
                else:
                    minimum = min(sequence for sequence, _action in command_actions)
                    at_boundary = [action for sequence, action in command_actions
                                   if sequence == minimum]
                    chosen = ("cancel" if "cancel" in at_boundary else
                              "deny" if "deny" in at_boundary else "approve")
                    expected = {"cancel": "CANCELLED", "approve": "DECIDED",
                                "deny": "REJECTED"}[chosen]
                    if target != expected:
                        raise ReplayError("human command precedence was not honored")
                    command_actions.clear()
            elif "cancel" in actions:
                minimum = min(sequence for sequence, _action in command_actions)
                if target != "CANCELLED" or any(
                        action != "cancel" for sequence, action in command_actions
                        if sequence == minimum):
                    raise ReplayError("queued human cancellation was not honored")
                command_actions.clear()
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
            reason = record.get("reason")
            if reason not in _SAFE_REASONS:
                raise ReplayError("state transition reason is not a safe enum")
            termination_reason = reason
            proposed = record.get("decision_option")
            if proposed is not None:
                if proposed not in option_ids:
                    raise ReplayError("state transition decision is not an option")
                decision_option = proposed
            transcript.append(_public_event(record))
            continue
        if event == "turn_prepared":
            if status != "RUNNING" or any(action == "cancel" for _, action in command_actions):
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
            if status != "RUNNING" or any(action == "cancel" for _, action in command_actions):
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
            if (not isinstance(transport_ok, bool) or not isinstance(timed_out, bool)
                    or not isinstance(parser_valid, bool)):
                raise ReplayError("attempt executor evidence is malformed")
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
            command_id = _id(record.get("command_id"), "command id")
            sequence = _int(record.get("sequence"), "command sequence", minimum=1)
            action = record.get("action")
            if action not in {"cancel", "approve", "deny"}:
                raise ReplayError("human command action is malformed")
            if action in {"approve", "deny"} and status != "WAITING_HUMAN":
                raise ReplayError("approval command was issued outside WAITING_HUMAN")
            if command_id in command_ids or (command_sequences and sequence < command_sequences[-1]):
                raise ReplayError("human command sequence or id was replayed")
            command_ids.append(command_id)
            command_sequences.append(sequence)
            command_actions.append((sequence, action))
            transcript.append(_public_event(record))
            continue
        if event in {"attempt_finished", "ballot_inert", "cleanup_receipt",
                     "advisory_left_behind", "journal_repaired"}:
            # These records are audit material.  Their control-bearing fields
            # were already validated above or have no effect on state.
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
        if not any(attempt.turn_id == value.turn_id and
                   attempt.turn_ordinal == value.turn_ordinal and
                   attempt.phase == "finished"
                   for attempt in attempts.values()):
            pending = value
    uncertain = any(attempt.uncertain for attempt in attempts.values())
    next_ordinal = max(ordinals, default=-1) + 1
    digest_payload = {
        "receipt_sha256": receipt_sha256, "run_id": run_id,
        "prior_generation": last_generation, "status": status,
        "termination_reason": termination_reason, "candidate_option": candidate,
        "decision_option": decision_option, "attempts": [attempt.__dict__ for attempt in attempts.values()],
        "ballots": [ballot.__dict__ for ballot in accepted],
        "pending_turn": None if pending is None else pending.__dict__,
        "next_ordinal": next_ordinal, "commands": command_ids,
        "command_sequences": command_sequences, "uncertain_spend": uncertain,
    }
    checkpoint_digest = _sha(digest_payload)
    return ReplayCheckpoint(
        receipt_sha256, run_id, decision_id, last_generation, status,
        termination_reason, candidate, decision_option,
        tuple(attempts.values()), tuple(accepted), pending, next_ordinal,
        tuple(command_ids), tuple(command_sequences),
        tuple(_freeze(record) for record in transcript),
        uncertain, checkpoint_digest,
    )
