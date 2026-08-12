"""Deterministic primitives for bounded, headless deliberation.

This module deliberately has no executor or CLI integration.  Its durability port is
an owner-bound callback.  Production integration must bind it like this::

    append = lambda event: _rundir.journal_append(run_dir, event, owner=owner)
    current = lambda: _rundir.owner_still_current(owner)
    engine = DeliberationEngine(..., generation=owner.generation,
                                durable_append=append, owner_is_current=current)

``journal_append`` fsyncs before returning, so :class:`AttemptLedger` can guarantee
that ``attempt_started`` is durable before the adapter launch port is entered.  A real
executor adapter is still required to revalidate the participant snapshot at its
actual before-spawn boundary and to disable retry/fallback/gate/repair paths.  This
module exposes that seam; it does not claim those integrations exist yet.

Model-authored prose and status fields are never control evidence.  Only executor
evidence and a ballot validated against an immutable attempt binding can affect state.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping, Protocol, Sequence


SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FRACTION_RE = re.compile(r"^([1-9][0-9]*)/([1-9][0-9]*)$")


class DeliberationError(Exception):
    """Base error for invalid policy, state, or adapter behavior."""


class DuplicateAttemptError(DeliberationError):
    """An attempt id was committed or consumed more than once."""


class OwnershipLostError(DeliberationError):
    """The injected owner fence reports that this scheduler is deposed."""


class SnapshotDriftError(DeliberationError):
    """The adapter's pre-commit snapshot revalidation failed."""


class RunState(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    DECIDED = "DECIDED"
    UNRESOLVED = "UNRESOLVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    FAILED = "FAILED"


class NextAction(str, Enum):
    START = "START"
    LAUNCH = "LAUNCH"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    DONE = "DONE"


TERMINAL_STATES = frozenset({
    RunState.DECIDED, RunState.UNRESOLVED, RunState.REJECTED,
    RunState.CANCELLED, RunState.TIMED_OUT,
    RunState.ATTEMPT_BUDGET_EXHAUSTED, RunState.FAILED,
})


def _valid_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def resolve_quorum(denominator: int, rule: int | str) -> int:
    """Resolve an integer, ``all``, or fractional rule with deterministic ceil."""
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 1:
        raise ValueError("quorum denominator must be a positive integer")
    if isinstance(rule, bool):
        raise ValueError("boolean quorum is invalid")
    if isinstance(rule, int):
        threshold = rule
    elif rule == "all":
        threshold = denominator
    elif isinstance(rule, str):
        match = _FRACTION_RE.fullmatch(rule)
        if not match:
            raise ValueError("quorum must be an integer, 'all', or a positive fraction")
        numerator, divisor = (int(part) for part in match.groups())
        # Integer ceiling avoids float rounding/overflow and is deterministic on
        # every supported Python build.
        threshold = (numerator * denominator + divisor - 1) // divisor
    else:
        raise ValueError("quorum must be an integer, 'all', or a fraction string")
    if not 1 <= threshold <= denominator:
        raise ValueError("resolved quorum must be within the fixed denominator")
    return threshold


@dataclass(frozen=True)
class DeliberationPolicy:
    decision_id: str
    seat_ids: tuple[str, ...]
    option_ids: tuple[str, ...]
    quorum_rule: int | str
    max_attempts: int
    require_human_approval: bool = False

    def __post_init__(self) -> None:
        _valid_id(self.decision_id, "decision id")
        if not 2 <= len(self.seat_ids) <= 10:
            raise ValueError("deliberation requires 2-10 seats")
        if len(set(self.seat_ids)) != len(self.seat_ids):
            raise ValueError("seat ids must be unique")
        for seat_id in self.seat_ids:
            _valid_id(seat_id, "seat id")
        if len(self.option_ids) < 2 or len(set(self.option_ids)) != len(self.option_ids):
            raise ValueError("decision options must contain at least two unique ids")
        for option_id in self.option_ids:
            _valid_id(option_id, "option id")
        if (isinstance(self.max_attempts, bool) or
                not isinstance(self.max_attempts, int) or self.max_attempts < 1):
            raise ValueError("max_attempts must be a positive integer")
        if not isinstance(self.require_human_approval, bool):
            raise ValueError("require_human_approval must be boolean")
        resolve_quorum(len(self.seat_ids), self.quorum_rule)

    @property
    def denominator(self) -> int:
        return len(self.seat_ids)

    @property
    def threshold(self) -> int:
        return resolve_quorum(self.denominator, self.quorum_rule)


@dataclass(frozen=True)
class TurnContext:
    decision_id: str
    seat_id: str
    turn_id: str
    turn_ordinal: int
    request_digest: str

    def __post_init__(self) -> None:
        _valid_id(self.decision_id, "decision id")
        _valid_id(self.seat_id, "seat id")
        _valid_id(self.turn_id, "turn id")
        if isinstance(self.turn_ordinal, bool) or not isinstance(self.turn_ordinal, int) or self.turn_ordinal < 0:
            raise ValueError("turn ordinal must be a non-negative integer")
        if not isinstance(self.request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.request_digest):
            raise ValueError("request_digest must be a lowercase sha256")


@dataclass(frozen=True)
class LaunchSpec:
    """Immutable, exact adapter input; only its digest/identity is journaled."""

    transport: str
    command_identity: str
    sealed_payload: bytes
    snapshot_digest: str

    def __post_init__(self) -> None:
        _valid_id(self.transport, "transport")
        if (not isinstance(self.command_identity, str) or
                not 1 <= len(self.command_identity.encode("utf-8")) <= 256):
            raise ValueError("command_identity must be bounded")
        if not isinstance(self.sealed_payload, bytes):
            raise ValueError("sealed_payload must be immutable bytes")
        if len(self.sealed_payload) > 1_048_576:
            raise ValueError("sealed_payload exceeds the P1 bound")
        if not isinstance(self.snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_digest):
            raise ValueError("snapshot_digest must be a lowercase sha256")

    @property
    def digest(self) -> str:
        identity = {
            "schema_version": SCHEMA_VERSION,
            "transport": self.transport,
            "command_identity": self.command_identity,
            "payload_sha256": hashlib.sha256(self.sealed_payload).hexdigest(),
            "snapshot_digest": self.snapshot_digest,
        }
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AttemptBinding:
    attempt_id: str
    generation: int
    decision_id: str
    seat_id: str
    turn_id: str
    turn_ordinal: int
    launch_spec_digest: str

    def __post_init__(self) -> None:
        _valid_id(self.attempt_id, "attempt id")
        _valid_id(self.decision_id, "decision id")
        _valid_id(self.seat_id, "seat id")
        _valid_id(self.turn_id, "turn id")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("generation must be positive")
        if isinstance(self.turn_ordinal, bool) or not isinstance(self.turn_ordinal, int) or self.turn_ordinal < 0:
            raise ValueError("turn ordinal must be non-negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.launch_spec_digest):
            raise ValueError("launch spec digest must be a lowercase sha256")


@dataclass(frozen=True)
class LaunchToken:
    """Single-use capability returned only after durable attempt commitment."""

    binding: AttemptBinding


@dataclass(frozen=True)
class ExecutionEvidence:
    """Executor-owned facts; no normalized/model-authored status is accepted."""

    transport_ok: bool
    exit_code: int | None
    timed_out: bool = False
    parser_valid: bool = True


@dataclass(frozen=True)
class AdapterResult:
    evidence: ExecutionEvidence
    structured_output: Mapping[str, object] | None = None
    model_prose: str = ""  # retained as data only; never inspected by the engine


@dataclass(frozen=True)
class CleanupReceipt:
    verified: bool
    retained_resources: tuple[str, ...] = ()
    unverified_pids: tuple[int, ...] = ()

    @property
    def clean(self) -> bool:
        return self.verified and not self.retained_resources and not self.unverified_pids


class DeliberationAdapter(Protocol):
    """Fresh one-attempt adapter port for the later real executor integration.

    ``revalidate`` is a pre-commit fail-closed check.  The real adapter must also
    revalidate inside ``launch`` immediately before its one and only physical spawn;
    it must not implement retries, ACP fallback, gates, or report repair.
    """

    def prepare(self, context: TurnContext) -> LaunchSpec: ...
    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool: ...
    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult: ...
    def cleanup(self) -> CleanupReceipt: ...


@dataclass
class _AttemptEntry:
    # Restored physical attempts are accounting evidence, never capabilities.
    # Only commit() may create a launch token.
    token: LaunchToken | None
    phase: str


def _field(value: object, name: str, default: object = None) -> object:
    """Read one field from a replay dataclass or a mapping without importing it."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


# Keep restore's public state projection on the same bounded vocabulary as
# journal replay.  A restored reason is eventually observable in envelopes, so
# accepting arbitrary text here would turn the restore boundary into a private
# path/secret exfiltration channel.
_SAFE_TERMINATION_REASONS = frozenset({
    "started", "deadline", "attempt_budget", "max_rounds", "cancelled",
    "snapshot_drift", "adapter_indeterminate", "adapter_error",
    "approval_required", "consensus", "human_cancel", "human_denied",
    "human_approved", "ownership_lost", "max_attempts", "timeout",
})


def _restore_digest(checkpoint: object) -> str:
    """Recompute the replay checkpoint seal without importing the replay module."""
    def object_fields(value: object, names: Sequence[str]) -> dict:
        return {name: _field(value, name) for name in names}

    attempts = tuple(_field(checkpoint, "attempts", ()) or ())
    ballots = tuple(_field(checkpoint, "ballots", ()) or ())
    pending = _field(checkpoint, "pending_turn")
    payload = {
        "receipt_sha256": _field(checkpoint, "receipt_sha256"),
        "run_id": _field(checkpoint, "run_id"),
        "prior_generation": _field(checkpoint, "prior_generation"),
        "status": _field(checkpoint, "status"),
        "termination_reason": _field(checkpoint, "termination_reason"),
        "candidate_option": _field(checkpoint, "candidate_option"),
        "decision_option": _field(checkpoint, "decision_option"),
        "attempts": [object_fields(item, (
            "attempt_id", "generation", "decision_id", "seat_id", "turn_id",
            "turn_ordinal", "launch_spec_digest", "phase", "ballot_valid"))
                     for item in attempts],
        "ballots": [object_fields(item, (
            "decision_id", "seat_id", "turn_id", "attempt_id", "turn_ordinal",
            "decision", "option_id", "confidence", "evidence_refs"))
                    for item in ballots],
        "pending_turn": (None if pending is None else object_fields(
            pending, ("decision_id", "seat_id", "turn_id", "turn_ordinal",
                      "request_digest"))),
        "next_ordinal": _field(checkpoint, "next_ordinal"),
        "commands": list(_field(checkpoint, "applied_command_ids", ()) or ()),
        "command_sequences": list(_field(checkpoint, "applied_command_sequences", ()) or ()),
        "uncertain_spend": _field(checkpoint, "uncertain_spend"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class AttemptLedger:
    """Thread-safe durable-before-launch and single-use attempt accounting."""

    def __init__(self, generation: int, durable_append: Callable[[dict], None],
                 owner_is_current: Callable[[], bool] = lambda: True,
                 *, restored_entries: Iterable[object] = ()) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        self.generation = generation
        self._append = durable_append
        self._owner_is_current = owner_is_current
        self._entries: dict[str, _AttemptEntry] = {}
        self._lock = threading.Lock()
        for restored in restored_entries:
            try:
                phase = _field(restored, "phase")
                if phase not in {"finished", "indeterminate"}:
                    raise ValueError("restored attempt phase is invalid")
                binding = AttemptBinding(
                    _field(restored, "attempt_id"),
                    _field(restored, "generation"),
                    _field(restored, "decision_id"),
                    _field(restored, "seat_id"),
                    _field(restored, "turn_id"),
                    _field(restored, "turn_ordinal"),
                    _field(restored, "launch_spec_digest",
                           _field(restored, "launch_spec_sha256")),
                )
            except (TypeError, ValueError) as exc:
                raise DeliberationError("invalid restored attempt") from exc
            if binding.generation >= generation:
                raise DeliberationError(
                    "restored attempt must belong to an earlier owner generation")
            if binding.attempt_id in self._entries:
                raise DuplicateAttemptError(
                    f"restored attempt already exists: {binding.attempt_id}")
            # Deliberately discard the reconstructed binding after validation:
            # retaining it must never accidentally become launch authority.
            self._entries[binding.attempt_id] = _AttemptEntry(None, phase)

    def commit(self, spec: LaunchSpec, binding: AttemptBinding) -> LaunchToken:
        if binding.generation != self.generation:
            raise DeliberationError("attempt generation does not match owner generation")
        if binding.launch_spec_digest != spec.digest:
            raise DeliberationError("attempt is not bound to the exact launch spec")
        token = LaunchToken(binding)
        with self._lock:
            if binding.attempt_id in self._entries:
                raise DuplicateAttemptError(f"attempt already exists: {binding.attempt_id}")
            if not self._owner_is_current():
                raise OwnershipLostError("ownership lost before attempt commitment")
            self._entries[binding.attempt_id] = _AttemptEntry(token, "committing")
            try:
                self._append({
                    "event": "attempt_started", "schema_version": SCHEMA_VERSION,
                    "generation": binding.generation,
                    "attempt_id": binding.attempt_id,
                    "decision_id": binding.decision_id,
                    "seat_id": binding.seat_id, "turn_id": binding.turn_id,
                    "turn_ordinal": binding.turn_ordinal,
                    "launch_spec_sha256": binding.launch_spec_digest,
                    "transport": spec.transport,
                    # The adapter's display identity may still be operator-private.
                    # Persist only a stable digest at this boundary.
                    "command_identity_sha256": hashlib.sha256(
                        spec.command_identity.encode("utf-8")).hexdigest(),
                })
            except BaseException:
                del self._entries[binding.attempt_id]
                raise
            self._entries[binding.attempt_id].phase = "committed"
        return token

    def claim_launch(self, token: LaunchToken) -> None:
        """Atomically consume a token; concurrent/duplicate calls cannot spawn."""
        attempt_id = token.binding.attempt_id
        with self._lock:
            entry = self._entries.get(attempt_id)
            if entry is None or entry.token is not token or entry.phase != "committed":
                raise DuplicateAttemptError(f"attempt token is not launchable: {attempt_id}")
            entry.phase = "launching"

    def launch_once(self, spec: LaunchSpec, token: LaunchToken,
                    launcher: Callable[[LaunchSpec, LaunchToken], AdapterResult]) -> AdapterResult:
        """Consume ``token`` and enter the launch port at most once.

        This is intentionally the only method used by the scheduler to call an
        adapter.  Spec substitution, duplicate calls, and concurrent calls all
        fail before ``launcher`` is entered.
        """
        if spec.digest != token.binding.launch_spec_digest:
            raise DeliberationError("launch spec differs from committed spec")
        self.claim_launch(token)
        if not self._owner_is_current():
            self.mark_indeterminate(token)
            raise OwnershipLostError("ownership lost after commitment; launch refused")
        return launcher(spec, token)

    def finish(self, token: LaunchToken, evidence: ExecutionEvidence,
               ballot_valid: bool) -> None:
        attempt_id = token.binding.attempt_id
        with self._lock:
            entry = self._entries.get(attempt_id)
            if entry is None or entry.token is not token or entry.phase != "launching":
                raise DuplicateAttemptError(f"attempt is not in flight: {attempt_id}")
            if not self._owner_is_current():
                entry.phase = "indeterminate"
                raise OwnershipLostError("ownership lost before attempt finish")
            self._append({
                "event": "attempt_finished", "schema_version": SCHEMA_VERSION,
                "generation": token.binding.generation,
                "attempt_id": attempt_id,
                "launch_spec_sha256": token.binding.launch_spec_digest,
                "transport_ok": evidence.transport_ok,
                "exit_code": evidence.exit_code,
                "timed_out": evidence.timed_out,
                "parser_valid": evidence.parser_valid,
                "ballot_valid": ballot_valid,
            })
            entry.phase = "finished"

    def mark_indeterminate(self, token: LaunchToken) -> None:
        with self._lock:
            entry = self._entries.get(token.binding.attempt_id)
            if entry is not None and entry.token is token and entry.phase != "finished":
                entry.phase = "indeterminate"

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            phases = [entry.phase for entry in self._entries.values()]
        return {
            "started": len(phases),
            "finished": phases.count("finished"),
            "indeterminate": sum(phase in {"committed", "launching", "indeterminate"}
                                 for phase in phases),
        }

    @property
    def uncertain_spend(self) -> bool:
        return self.counts["indeterminate"] > 0


def summarize_attempt_events(events: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Conservatively project physical attempts from generation-fenced events.

    A durable start without a matching finish is indeterminate and may have spent.
    Conflicting reuse of an attempt id fails closed.  No PID action is attempted.
    """
    started: dict[str, tuple[object, ...]] = {}
    finished: set[str] = set()
    for event in events:
        kind, attempt_id = event.get("event"), event.get("attempt_id")
        if kind not in {"attempt_started", "attempt_finished"} or not isinstance(attempt_id, str):
            continue
        binding = (event.get("generation"), event.get("launch_spec_sha256"))
        if kind == "attempt_started":
            if attempt_id in started and started[attempt_id] != binding:
                raise DeliberationError("attempt id has conflicting durable bindings")
            started[attempt_id] = binding
        elif attempt_id in started and started[attempt_id] == binding:
            finished.add(attempt_id)
    indeterminate = sorted(set(started) - finished)
    return {
        "started": len(started), "finished": len(finished),
        "indeterminate": len(indeterminate), "uncertain_spend": bool(indeterminate),
        "stale_attempts": tuple({"attempt_id": item, "action": "report_only"}
                                for item in indeterminate),
    }


@dataclass(frozen=True)
class Ballot:
    schema_version: int
    decision_id: str
    seat_id: str
    turn_id: str
    attempt_id: str
    decision: str
    option_id: str | None
    confidence: str
    evidence_refs: tuple[str, ...] = ()


def validate_ballot(payload: object, binding: AttemptBinding,
                    policy: DeliberationPolicy) -> Ballot | None:
    """Return a schedule-bound ballot, or ``None`` for inert model data."""
    if not isinstance(payload, Mapping):
        return None
    if (payload.get("schema_version") != SCHEMA_VERSION or
            payload.get("decision_id") != binding.decision_id or
            payload.get("seat_id") != binding.seat_id or
            payload.get("turn_id") != binding.turn_id or
            payload.get("attempt_id") != binding.attempt_id):
        return None
    decision, option_id = payload.get("decision"), payload.get("option_id")
    if decision == "vote":
        if option_id not in policy.option_ids:
            return None
    elif decision in {"abstain", "undecided"}:
        if option_id is not None:
            return None
    else:
        return None
    confidence = payload.get("confidence")
    if confidence not in {"low", "medium", "high"}:
        return None
    refs = payload.get("evidence_refs", [])
    if (not isinstance(refs, list) or len(refs) > 32 or
            any(not isinstance(ref, str) or len(ref.encode("utf-8")) > 128 for ref in refs)):
        return None
    return Ballot(SCHEMA_VERSION, binding.decision_id, binding.seat_id,
                  binding.turn_id, binding.attempt_id, decision, option_id,
                  confidence, tuple(refs))


class BallotBook:
    """Latest valid scheduled ballot per fixed seat; duplicates are inert."""

    def __init__(self, policy: DeliberationPolicy,
                 *, restored_ballots: Iterable[object] = ()) -> None:
        self.policy = policy
        self._latest: dict[str, tuple[int, Ballot]] = {}
        for restored in restored_ballots:
            try:
                ordinal = _field(restored, "turn_ordinal")
                if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                        or ordinal < 0):
                    raise ValueError("restored ballot ordinal is invalid")
                decision_id = _field(restored, "decision_id")
                seat_id = _field(restored, "seat_id")
                decision = _field(restored, "decision")
                option_id = _field(restored, "option_id")
                confidence = _field(restored, "confidence", None) or "low"
                raw_refs = _field(restored, "evidence_refs", ())
                if raw_refs is None:
                    raw_refs = ()
                if (not isinstance(raw_refs, (tuple, list)) or len(raw_refs) > 32
                        or any(not isinstance(ref, str) or
                               len(ref.encode("utf-8")) > 128 for ref in raw_refs)):
                    raise ValueError("restored ballot evidence refs are invalid")
                evidence_refs = tuple(raw_refs)
                ballot = Ballot(
                    SCHEMA_VERSION, decision_id, seat_id,
                    _field(restored, "turn_id"),
                    _field(restored, "attempt_id"), decision, option_id,
                    confidence, evidence_refs,
                )
                if decision_id != policy.decision_id:
                    raise ValueError("restored ballot decision differs from policy")
                if decision == "vote":
                    if option_id not in policy.option_ids:
                        raise ValueError("restored ballot option is invalid")
                elif decision in {"abstain", "undecided"}:
                    if option_id is not None:
                        raise ValueError("restored non-vote ballot has an option")
                else:
                    raise ValueError("restored ballot decision is invalid")
                if confidence not in {"low", "medium", "high"}:
                    raise ValueError("restored ballot confidence is invalid")
                _valid_id(ballot.seat_id, "seat id")
                _valid_id(ballot.turn_id, "turn id")
                _valid_id(ballot.attempt_id, "attempt id")
            except (TypeError, ValueError) as exc:
                raise DeliberationError("invalid restored ballot") from exc
            if not self.record(ballot, ordinal):
                raise DeliberationError("restored ballot is duplicate or out of order")

    def record(self, ballot: Ballot, turn_ordinal: int) -> bool:
        if ballot.seat_id not in self.policy.seat_ids:
            return False
        current = self._latest.get(ballot.seat_id)
        if current is not None and turn_ordinal <= current[0]:
            return False
        self._latest[ballot.seat_id] = (turn_ordinal, ballot)
        return True

    def tally(self) -> dict[str, int]:
        counts = {option_id: 0 for option_id in self.policy.option_ids}
        for _, ballot in self._latest.values():
            if ballot.decision == "vote" and ballot.option_id is not None:
                counts[ballot.option_id] += 1
        return counts

    def candidate(self) -> str | None:
        winners = [option_id for option_id, count in self.tally().items()
                   if count >= self.policy.threshold]
        return winners[0] if len(winners) == 1 else None

    @property
    def valid_count(self) -> int:
        return len(self._latest)


@dataclass(frozen=True)
class HumanCommand:
    sequence: int
    action: str
    command_id: str | None = None


@dataclass
class DeliberationState:
    generation: int
    status: RunState = RunState.PREPARED
    candidate_option: str | None = None
    decision_option: str | None = None
    termination_reason: str | None = None
    uncertain_spend: bool = False
    advisory_left_behind: list[str] = field(default_factory=list)
    cleanup_verified: bool = False
    retained_resources: list[str] = field(default_factory=list)


class DeliberationEngine:
    """Small synchronous scheduler kernel for one physical attempt at a time."""

    def __init__(self, policy: DeliberationPolicy, adapter: DeliberationAdapter,
                 generation: int, durable_append: Callable[[dict], None],
                 *, deadline: float, clock: Callable[[], float],
                 owner_is_current: Callable[[], bool] = lambda: True,
                 cancel_requested: Callable[[], bool] = lambda: False) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        self.policy, self.adapter, self.deadline, self.clock = policy, adapter, deadline, clock
        self._append_callback = durable_append
        self._owner_is_current = owner_is_current
        self._cancel_requested = cancel_requested
        self.state = DeliberationState(generation=generation)
        self.attempts = AttemptLedger(generation, durable_append, owner_is_current)
        self.ballots = BallotBook(policy)
        # Resume wiring is deliberately outside this slice.  A restored pending
        # turn is inert data until a future scheduler verifies its prompt digest.
        self.pending_turn: TurnContext | None = None
        self.next_ordinal = 0

    @classmethod
    def restore(cls, checkpoint: object, policy: DeliberationPolicy,
                adapter: DeliberationAdapter, generation: int,
                durable_append: Callable[[dict], None], *, deadline: float,
                clock: Callable[[], float],
                owner_is_current: Callable[[], bool],
                cancel_requested: Callable[[], bool] = lambda: False) -> "DeliberationEngine":
        """Restore validated accounting/state without creating launch authority.

        ``checkpoint`` is intentionally duck typed so this pure kernel does not
        import the replay module.  Restoration performs no adapter or journal
        calls and does not resume the pending turn.
        """
        if not callable(owner_is_current):
            raise TypeError("restore requires an owner callback")
        try:
            current = bool(owner_is_current())
        except Exception as exc:
            raise OwnershipLostError("ownership could not be verified for restore") from exc
        if not current:
            raise OwnershipLostError("ownership lost before restore")
        try:
            status = RunState(_field(checkpoint, "status"))
        except (TypeError, ValueError) as exc:
            raise DeliberationError("checkpoint state is invalid") from exc
        checkpoint_digest = _field(checkpoint, "digest")
        if (not isinstance(checkpoint_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_digest)
                or _restore_digest(checkpoint) != checkpoint_digest):
            raise DeliberationError("checkpoint digest is missing or invalid")
        if status in TERMINAL_STATES:
            raise DeliberationError("terminal checkpoint cannot be restored")
        prior_generation = _field(checkpoint, "prior_generation")
        if (isinstance(prior_generation, bool) or not isinstance(prior_generation, int)
                or prior_generation < 1 or prior_generation >= generation):
            raise DeliberationError("checkpoint generation is not older than the owner")
        if _field(checkpoint, "decision_id") != policy.decision_id:
            raise DeliberationError("checkpoint decision differs from policy")
        attempts = tuple(_field(checkpoint, "attempts", ()) or ())
        ballots = tuple(_field(checkpoint, "ballots", ()) or ())
        next_ordinal = _field(checkpoint, "next_ordinal", 0)
        if (isinstance(next_ordinal, bool) or not isinstance(next_ordinal, int)
                or next_ordinal < 0):
            raise DeliberationError("checkpoint next ordinal is invalid")
        if len(attempts) > policy.max_attempts:
            raise DeliberationError("checkpoint attempts exceed the policy budget")

        termination_reason = _field(checkpoint, "termination_reason")
        if status == RunState.PREPARED:
            if (termination_reason is not None and
                    termination_reason not in _SAFE_TERMINATION_REASONS):
                raise DeliberationError("checkpoint termination reason is not a safe enum")
        elif termination_reason not in _SAFE_TERMINATION_REASONS:
            raise DeliberationError("checkpoint termination reason is not a safe enum")

        attempt_by_id: dict[str, object] = {}
        attempt_by_turn: dict[tuple[str, str], object] = {}
        attempt_ordinals: set[int] = set()
        for restored in attempts:
            attempt_id = _field(restored, "attempt_id")
            if (not isinstance(attempt_id, str) or not _ID_RE.fullmatch(attempt_id)
                    or attempt_id in attempt_by_id):
                raise DeliberationError("checkpoint attempt id is invalid or duplicated")
            attempt_by_id[attempt_id] = restored
            restored_generation = _field(restored, "generation")
            if (isinstance(restored_generation, bool)
                    or not isinstance(restored_generation, int)
                    or not 1 <= restored_generation <= prior_generation):
                raise DeliberationError("checkpoint attempt generation is invalid")
            if _field(restored, "decision_id") != policy.decision_id:
                raise DeliberationError("checkpoint attempt decision differs from policy")
            seat_id = _field(restored, "seat_id")
            turn_id = _field(restored, "turn_id")
            ordinal = _field(restored, "turn_ordinal")
            if seat_id not in policy.seat_ids or not isinstance(turn_id, str):
                raise DeliberationError("checkpoint attempt is outside the policy")
            if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                    or ordinal < 0 or ordinal >= next_ordinal):
                raise DeliberationError("checkpoint attempt ordinal is invalid")
            if ordinal in attempt_ordinals:
                raise DeliberationError("checkpoint reuses a schedule ordinal")
            attempt_ordinals.add(ordinal)
            phase = _field(restored, "phase")
            if phase not in {"finished", "indeterminate"}:
                raise DeliberationError("checkpoint attempt phase is invalid")
            turn_key = (seat_id, turn_id)
            if turn_key in attempt_by_turn:
                raise DeliberationError("checkpoint has multiple attempts for one turn")
            attempt_by_turn[turn_key] = restored

        ballot_attempt_ids: set[str] = set()
        for restored in ballots:
            attempt_id = _field(restored, "attempt_id")
            attempt = attempt_by_id.get(attempt_id)
            if attempt is None or attempt_id in ballot_attempt_ids:
                raise DeliberationError("checkpoint ballot has no unique matching attempt")
            if _field(attempt, "phase") != "finished" or _field(attempt, "ballot_valid") is not True:
                raise DeliberationError("checkpoint ballot is not backed by a valid finished attempt")
            for name in ("decision_id", "seat_id", "turn_id", "turn_ordinal"):
                if _field(restored, name) != _field(attempt, name):
                    raise DeliberationError("checkpoint ballot binding differs from its attempt")
            ballot_attempt_ids.add(attempt_id)

        pending_value = _field(checkpoint, "pending_turn")
        pending_key: tuple[str, str] | None = None
        pending_ordinal: int | None = None
        if pending_value is not None:
            pending_key = (_field(pending_value, "seat_id"),
                           _field(pending_value, "turn_id"))
            if pending_key in attempt_by_turn:
                raise DeliberationError("checkpoint pending turn already has a physical attempt")
            pending_ordinal = _field(pending_value, "turn_ordinal")
            if (isinstance(pending_ordinal, bool) or not isinstance(pending_ordinal, int)
                    or pending_ordinal < 0 or pending_ordinal + 1 != next_ordinal):
                raise DeliberationError("checkpoint pending turn is not the next schedule slot")

        # Replay schedules exactly one physical attempt per prepared turn and
        # permits at most one unattempted turn at the tail.  Requiring this
        # shape prevents a forged checkpoint from skipping a turn merely by
        # increasing next_ordinal or by attaching a distant pending turn.
        expected_attempt_ordinals = set(range(len(attempts)))
        if attempt_ordinals != expected_attempt_ordinals:
            raise DeliberationError("checkpoint attempt ordinals are not contiguous")
        if pending_ordinal is None:
            if next_ordinal != len(attempts):
                raise DeliberationError("checkpoint next ordinal skips an attempt slot")
        elif (pending_ordinal != len(attempts) or
              next_ordinal != len(attempts) + 1):
            raise DeliberationError("checkpoint pending ordinal skips an attempt slot")
        for restored in ballots:
            if _field(restored, "turn_ordinal") >= next_ordinal:
                raise DeliberationError("checkpoint ballot ordinal is beyond the schedule")

        claimed_decision = _field(checkpoint, "decision_option")
        if claimed_decision is not None and claimed_decision not in policy.option_ids:
            raise DeliberationError("checkpoint decision is outside policy options")
        claimed_candidate = _field(checkpoint, "candidate_option")
        if status == RunState.PREPARED and (claimed_candidate is not None or claimed_decision is not None):
            raise DeliberationError("prepared checkpoint has a decision")
        if status == RunState.PREPARED and (attempts or ballots or pending_value is not None):
            raise DeliberationError("prepared checkpoint has durable execution material")
        if status == RunState.RUNNING and (claimed_candidate is not None or claimed_decision is not None):
            raise DeliberationError("running checkpoint has a decision")
        if status == RunState.WAITING_HUMAN:
            if not policy.require_human_approval or claimed_candidate is None or claimed_decision is not None:
                raise DeliberationError("waiting-human checkpoint violates approval policy")
        engine = cls(policy, adapter, generation, durable_append,
                     deadline=deadline, clock=clock,
                     owner_is_current=owner_is_current,
                     cancel_requested=cancel_requested)
        engine.attempts = AttemptLedger(
            generation, durable_append, owner_is_current,
            restored_entries=attempts)
        engine.ballots = BallotBook(policy, restored_ballots=ballots)
        candidate = engine.ballots.candidate()
        if claimed_candidate != candidate:
            raise DeliberationError("checkpoint candidate differs from restored ballots")
        if status == RunState.WAITING_HUMAN and candidate is None:
            raise DeliberationError("waiting-human checkpoint has no candidate")
        claimed_uncertain = _field(checkpoint, "uncertain_spend")
        if not isinstance(claimed_uncertain, bool):
            raise DeliberationError("checkpoint uncertain-spend flag is invalid")
        if claimed_uncertain != engine.attempts.uncertain_spend:
            raise DeliberationError("checkpoint uncertain-spend flag differs from attempts")
        engine.state = DeliberationState(
            generation=generation, status=status,
            candidate_option=candidate,
            decision_option=_field(checkpoint, "decision_option"),
            termination_reason=termination_reason,
            uncertain_spend=claimed_uncertain,
        )
        pending = pending_value
        if pending is not None:
            try:
                engine.pending_turn = TurnContext(
                    _field(pending, "decision_id"), _field(pending, "seat_id"),
                    _field(pending, "turn_id"), _field(pending, "turn_ordinal"),
                    _field(pending, "request_digest"),
                )
            except (TypeError, ValueError) as exc:
                raise DeliberationError("checkpoint pending turn is invalid") from exc
            if (engine.pending_turn.decision_id != policy.decision_id or
                engine.pending_turn.seat_id not in policy.seat_ids):
                raise DeliberationError("checkpoint pending turn is outside policy")
        if (engine.pending_turn is not None and
                (status != RunState.RUNNING or
                 engine.pending_turn.turn_ordinal + 1 != next_ordinal)):
            raise DeliberationError("checkpoint next ordinal does not follow pending turn")
        engine.next_ordinal = next_ordinal
        return engine

    def _append(self, event: dict) -> None:
        if not self._owner_is_current():
            raise OwnershipLostError("ownership lost before state journal append")
        self._append_callback({**event, "generation": self.state.generation})

    def _transition(self, status: RunState, reason: str,
                    *, decision_option: str | None = None) -> None:
        if self.state.status in TERMINAL_STATES:
            raise DeliberationError("terminal state cannot transition")
        legal = {
            RunState.PREPARED: {RunState.RUNNING, RunState.CANCELLED, RunState.TIMED_OUT, RunState.FAILED},
            RunState.RUNNING: TERMINAL_STATES | {RunState.WAITING_HUMAN},
            RunState.WAITING_HUMAN: {RunState.DECIDED, RunState.REJECTED,
                                     RunState.CANCELLED, RunState.TIMED_OUT},
        }
        if status not in legal.get(self.state.status, set()):
            raise DeliberationError(f"illegal transition {self.state.status} -> {status}")
        self._append({"event": "state_transition", "from": self.state.status.value,
                      "to": status.value, "reason": reason,
                      "decision_option": decision_option})
        self.state.status = status
        self.state.termination_reason = reason
        if decision_option is not None:
            self.state.decision_option = decision_option

    def start(self) -> None:
        if self.state.status != RunState.PREPARED:
            raise DeliberationError("run is not prepared")
        if self.clock() >= self.deadline:
            self._transition(RunState.TIMED_OUT, "deadline")
        else:
            self._transition(RunState.RUNNING, "started")

    def next_action(self) -> NextAction:
        if self.state.status == RunState.PREPARED:
            return NextAction.START
        if self.state.status == RunState.WAITING_HUMAN:
            if self.clock() >= self.deadline:
                self._transition(RunState.TIMED_OUT, "deadline")
                return NextAction.DONE
            return NextAction.WAIT_FOR_HUMAN
        if self.state.status in TERMINAL_STATES:
            return NextAction.DONE
        if self.clock() >= self.deadline:
            self._transition(RunState.TIMED_OUT, "deadline")
            return NextAction.DONE
        if self.attempts.counts["started"] >= self.policy.max_attempts:
            self._transition(RunState.ATTEMPT_BUDGET_EXHAUSTED, "attempt_budget")
            return NextAction.DONE
        return NextAction.LAUNCH

    def finish_schedule(self, reason: str = "max_rounds") -> None:
        """Durably close a non-consensus schedule that has no turns left.

        The scheduler, rather than a caller's report projection, owns this
        terminal transition.  Leaving a bounded run in ``RUNNING`` would make
        a completed process look resumable even though its fixed schedule was
        exhausted.
        """
        if self.state.status == RunState.RUNNING:
            self._transition(RunState.UNRESOLVED, reason)
        elif self.state.status not in TERMINAL_STATES and self.state.status != RunState.WAITING_HUMAN:
            raise DeliberationError("schedule cannot be finished from its current state")

    def run_turn(self, context: TurnContext, attempt_id: str) -> AdapterResult | None:
        if self.next_action() != NextAction.LAUNCH:
            return None
        if self._cancel_requested_safely():
            self._transition(RunState.CANCELLED, "cancelled")
            return None
        if context.decision_id != self.policy.decision_id or context.seat_id not in self.policy.seat_ids:
            raise DeliberationError("turn is outside the immutable schedule")
        spec = self.adapter.prepare(context)
        if not self.adapter.revalidate(spec, context):
            self._transition(RunState.FAILED, "snapshot_drift")
            raise SnapshotDriftError("participant snapshot changed before commitment")
        binding = AttemptBinding(attempt_id, self.state.generation, context.decision_id,
                                 context.seat_id, context.turn_id,
                                 context.turn_ordinal, spec.digest)
        token = self.attempts.commit(spec, binding)
        try:
            result = self.attempts.launch_once(spec, token, self.adapter.launch)
        except BaseException:
            self.attempts.mark_indeterminate(token)
            self.state.uncertain_spend = True
            if self.state.status not in TERMINAL_STATES:
                try:
                    self._transition(RunState.FAILED, "adapter_indeterminate")
                except OwnershipLostError:
                    self.state.status = RunState.FAILED
                    self.state.termination_reason = "ownership_lost"
            raise

        # Capture cancellation at the physical-result boundary.  A callback
        # may become true inside the provider call; that cancellation must win
        # over a model ballot that has not yet been durably accepted.
        cancel_before_finish = self._cancel_requested_safely()
        ballot = None
        if (not cancel_before_finish and result.evidence.parser_valid and
                isinstance(result.structured_output, Mapping)):
            ballot = validate_ballot(result.structured_output.get("ballot"), binding, self.policy)
        try:
            self.attempts.finish(token, result.evidence, ballot is not None)
        except BaseException:
            self.attempts.mark_indeterminate(token)
            self.state.uncertain_spend = True
            raise

        self._record_advisory_left_behind(result.structured_output)

        if cancel_before_finish or self._cancel_requested_safely():
            self._transition(RunState.CANCELLED, "cancelled")
            return result

        # Absolute deadline is a safety boundary.  A result returned after it is
        # recorded for audit, but cannot become a decision candidate.
        if result.evidence.timed_out or self.clock() >= self.deadline:
            self._transition(RunState.TIMED_OUT, "deadline")
            return result
        if not result.evidence.transport_ok:
            self._transition(RunState.FAILED, "adapter_error")
            return result

        if ballot is not None and self.ballots.record(ballot, context.turn_ordinal):
            self._append({"event": "ballot_accepted", "attempt_id": attempt_id,
                          "seat_id": ballot.seat_id, "turn_id": ballot.turn_id,
                          "turn_ordinal": context.turn_ordinal,
                          "decision": ballot.decision, "option_id": ballot.option_id})
        elif result.structured_output is not None:
            self._append({"event": "ballot_inert", "attempt_id": attempt_id,
                          "seat_id": binding.seat_id, "turn_id": binding.turn_id})

        # Result ingestion and candidate validation deliberately precede the
        # attempt-budget check: consensus on the final attempt is preserved.
        candidate = self.ballots.candidate()
        if candidate is not None:
            self.state.candidate_option = candidate
            if self.policy.require_human_approval:
                self._transition(RunState.WAITING_HUMAN, "approval_required")
            else:
                self._transition(RunState.DECIDED, "consensus", decision_option=candidate)
        elif self.attempts.counts["started"] >= self.policy.max_attempts:
            self._transition(RunState.ATTEMPT_BUDGET_EXHAUSTED, "attempt_budget")
        return result

    def _cancel_requested_safely(self) -> bool:
        try:
            return bool(self._cancel_requested())
        except Exception:
            # An unavailable cancellation channel must not grant the model a
            # decision.  Fail closed at this boundary.
            return True

    def _record_advisory_left_behind(self, output: Mapping[str, object] | None) -> None:
        if not isinstance(output, Mapping):
            return
        value = output.get("left_behind")
        if not isinstance(value, list):
            return
        advisory = [item[:256] for item in value[:32] if isinstance(item, str)]
        if advisory:
            self.state.advisory_left_behind.extend(advisory)
            self._append({"event": "advisory_left_behind", "items": advisory,
                          "verified": False, "source": "model_output"})

    def apply_human_commands(self, commands: Sequence[HumanCommand]) -> None:
        if self.state.status != RunState.WAITING_HUMAN:
            raise DeliberationError("run is not waiting for human approval")
        if self.clock() >= self.deadline:
            self._transition(RunState.TIMED_OUT, "deadline")
            return
        valid = [cmd for cmd in commands if cmd.action in {"cancel", "approve", "deny"}
                 and isinstance(cmd.sequence, int) and not isinstance(cmd.sequence, bool)]
        if not valid:
            return
        for index, command in enumerate(valid):
            command_id = command.command_id
            if command_id is None:
                command_id = f"cmd-{command.sequence}-{index}"
            if not isinstance(command_id, str) or not _ID_RE.fullmatch(command_id):
                raise DeliberationError("human command id is invalid")
            self._append({"event": "human_command", "command_id": command_id,
                          "sequence": command.sequence, "action": command.action})
        first_sequence = min(cmd.sequence for cmd in valid)
        actions = {cmd.action for cmd in valid if cmd.sequence == first_sequence}
        if "cancel" in actions:
            self._transition(RunState.CANCELLED, "human_cancel")
        elif "deny" in actions:
            self._transition(RunState.REJECTED, "human_denied")
        elif "approve" in actions:
            self._transition(RunState.DECIDED, "human_approved",
                             decision_option=self.state.candidate_option)

    def cancel(self) -> None:
        if self.state.status in TERMINAL_STATES:
            return
        self._transition(RunState.CANCELLED, "cancelled")

    def cleanup(self) -> CleanupReceipt:
        receipt = self.adapter.cleanup()
        # Bare PIDs are never used as kill authority.  The adapter must provide a
        # verified reuse-safe handle; otherwise they remain report-only evidence.
        self.state.cleanup_verified = receipt.clean
        retained = list(receipt.retained_resources)
        retained.extend(f"pid:{pid}:report_only" for pid in receipt.unverified_pids)
        self.state.retained_resources = retained
        self._append({"event": "cleanup_receipt", "verified": receipt.verified,
                      "clean": receipt.clean, "retained_resources": retained})
        return receipt


class ScriptedAdapter:
    """Deterministic fake adapter; ``spawn_count`` means launch-port entries."""

    def __init__(self, outcomes: Sequence[AdapterResult | BaseException],
                 *, revalidate: Callable[[LaunchSpec, TurnContext], bool] | None = None,
                 before_spawn: Callable[[LaunchSpec, LaunchToken], None] | None = None,
                 cleanup_receipt: CleanupReceipt = CleanupReceipt(True)) -> None:
        self._outcomes = list(outcomes)
        self._revalidate = revalidate or (lambda spec, context: True)
        self._before_spawn = before_spawn
        self._cleanup_receipt = cleanup_receipt
        self._lock = threading.Lock()
        self.prepare_count = 0
        self.spawn_count = 0

    def prepare(self, context: TurnContext) -> LaunchSpec:
        with self._lock:
            self.prepare_count += 1
        payload = json.dumps({"decision_id": context.decision_id,
                              "seat_id": context.seat_id,
                              "turn_id": context.turn_id,
                              "request_digest": context.request_digest},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        return LaunchSpec("scripted", f"scripted:{context.seat_id}", payload,
                          hashlib.sha256(context.seat_id.encode("utf-8")).hexdigest())

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        return bool(self._revalidate(spec, context))

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        if self._before_spawn is not None:
            self._before_spawn(spec, token)
        with self._lock:
            self.spawn_count += 1
            if not self._outcomes:
                raise DeliberationError("script has no remaining outcome")
            outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def cleanup(self) -> CleanupReceipt:
        return self._cleanup_receipt
