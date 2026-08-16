"""Receipt-bound one-attempt subprocess integration for deliberation.

This module is an explicit integration seam, not a CLI entry point.  It is
constructed only from a held run-directory owner, the owner's immutable
receipt, and a frozen roster.  The caller must opt into this API directly;
ordinary dispatch, the deliberation CLI, and resume remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

import _rundir
import _deliberation_replay as _replay
import _executor
from _deliberation import (DeliberationError, DeliberationPolicy,
                           _receipt_binding)
from _deliberation_adapter import FreshDispatchAdapter, SeatMultiplexAdapter
from _deliberation_invocation import build_invocation_plans
from _deliberation_roster import FrozenRoster, WorktreeProof
from _deliberation_scheduler import DeliberationScheduler
from _deliberation_phase_a import bind_schedule


class LiveDeliberationError(DeliberationError):
    """The receipt-bound live integration gate refused to construct a run."""


MAX_LIVE_QUESTION_BYTES = 16 * 1024
MAX_LIVE_TIMEOUT_MS = 3_600_000
MAX_LIVE_WALL_CLOCK_SKEW_MS = 120_000
_PROVIDER_INTEGRATION = {"name": "live-deliberation", "version": 1}
_FALSE_EXECUTION_FIELDS = (
    "retries", "acp_fallback", "gates", "report_repair", "allow_payg",
    "allow_secondary",
)
_PLAN_FIELDS = frozenset({
    "snapshot_digest", "cli", "transport", "effective_permission",
    "authority_class", "model_sha256", "profile_name_sha256",
    "extra_args_sha256", "cwd_sha256", "worktree_path_sha256",
    "worktree_head_sha256", "custom_agent_definition_digest",
    "custom_agent_source_digest", "output_contract",
})
_ACTIVATION_LOCK = threading.Lock()
_ACTIVATED_OWNERS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class LiveBinding:
    receipt_sha256: str
    roster_digest: str
    question_sha256: str
    schedule_digest: str
    rounds: int
    deadline_unix_ms: int
    deadline_clock: float


def _canonical(value: Mapping[str, object]) -> tuple[dict[str, object], str]:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        frozen = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LiveDeliberationError("live receipt is not canonical JSON") from exc
    if not isinstance(frozen, dict):
        raise LiveDeliberationError("live receipt must be an object")
    return frozen, hashlib.sha256(raw).hexdigest()


def _owner_receipt(owner: _rundir.Owner, supplied: Mapping[str, object]) -> tuple[dict, str]:
    if not isinstance(owner, _rundir.Owner):
        raise LiveDeliberationError("live integration requires a held run owner")
    if not os.path.isdir(owner.run_dir) or not _rundir.owner_still_current(owner):
        raise LiveDeliberationError("live integration owner is not current")
    try:
        disk = _rundir.read_json(os.path.join(owner.run_dir, "receipt.json"))
        if not isinstance(disk, dict):
            raise LiveDeliberationError("live run has no immutable receipt")
        disk_snapshot, disk_sha = _canonical(disk)
        supplied_snapshot, supplied_sha = _canonical(supplied)
        run_id = disk_snapshot.get("run_id")
        if not isinstance(run_id, str):
            raise LiveDeliberationError("live receipt run id is invalid")
        _rundir.validate_run_id(run_id)
        if os.path.basename(os.path.normpath(owner.run_dir)) != run_id:
            raise LiveDeliberationError("live receipt is not bound to the owner path")
        if disk_snapshot != supplied_snapshot or disk_sha != supplied_sha:
            raise LiveDeliberationError("live receipt differs from owner receipt")
        tagged, torn = _rundir.journal_read_tagged(owner.run_dir)
        if torn:
            raise LiveDeliberationError("live journal has a torn tail")
        prepared = [record for segment_generation, record in tagged
                    if record.get("event") == "run_prepared"
                    and record.get("generation") == segment_generation
                    and record.get("run_id") == run_id
                    and record.get("receipt_sha256") == disk_sha]
        if len(prepared) != 1:
            raise LiveDeliberationError(
                "live journal lacks exactly one receipt-bound run preparation")
        try:
            checkpoint = _replay.replay_checkpoint(
                disk_snapshot, tagged, owner.generation + 1)
        except _replay.ReplayError as exc:
            raise LiveDeliberationError(
                "live journal is not a valid prepared prefix") from exc
        if (checkpoint.status != "PREPARED" or checkpoint.next_ordinal != 0
                or checkpoint.attempts or checkpoint.ballots
                or checkpoint.pending_turn is not None
                or checkpoint.pending_commands
                or checkpoint.pending_command_batch is not None):
            raise LiveDeliberationError(
                "live integration requires an unstarted prepared journal")
        return disk_snapshot, disk_sha
    except LiveDeliberationError:
        raise
    except (_rundir.JournalCorruptError, OSError, TypeError, ValueError,
            UnicodeError) as exc:
        raise LiveDeliberationError("live receipt or journal could not be verified") from exc


def _owner_receipt_still_matches(owner: _rundir.Owner, receipt_sha256: str) -> bool:
    """Recheck the immutable receipt without exposing its contents."""
    try:
        if not _rundir.owner_still_current(owner):
            return False
        value = _rundir.read_json(os.path.join(owner.run_dir, "receipt.json"))
        if not isinstance(value, dict):
            return False
        _snapshot, observed = _canonical(value)
        return observed == receipt_sha256
    except (OSError, TypeError, ValueError, UnicodeError):
        return False


def _owner_current_with_lease(owner: _rundir.Owner) -> bool:
    """Final owner fence includes lease expiry, not merely lock-byte identity."""
    try:
        data = _rundir.read_owner(owner.run_dir)
        return (bool(data) and data.get("nonce") == owner.nonce
                and _rundir.owner_still_current(owner)
                and time.time() < _rundir._effective_expiry(owner.run_dir, data))
    except Exception:
        return False


def _validate_time_authority(owner: _rundir.Owner, binding: LiveBinding,
                             *, timeout_ms: int, physical_attempts: int,
                             unix_now_ms: int) -> None:
    if (isinstance(unix_now_ms, bool) or not isinstance(unix_now_ms, int)
            or unix_now_ms < 1):
        raise LiveDeliberationError("live wall-clock authority is invalid")
    observed_now_ms = int(time.time() * 1000)
    if abs(unix_now_ms - observed_now_ms) > MAX_LIVE_WALL_CLOCK_SKEW_MS:
        raise LiveDeliberationError("live wall-clock authority is stale")
    try:
        data = _rundir.read_owner(owner.run_dir)
        if not data or data.get("nonce") != owner.nonce:
            raise LiveDeliberationError("live owner lease is unavailable")
        lease_ms = int(_rundir._effective_expiry(owner.run_dir, data) * 1000)
    except LiveDeliberationError:
        raise
    except Exception as exc:
        raise LiveDeliberationError("live owner lease could not be verified") from exc
    total_budget_ms = timeout_ms * physical_attempts
    if (binding.deadline_unix_ms - unix_now_ms < total_budget_ms
            or lease_ms - int(time.time() * 1000) < total_budget_ms):
        raise LiveDeliberationError(
            "live one-round timeout budget exceeds the receipt deadline or owner lease")


def _sha(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _expected_plan_identity(roster: FrozenRoster, plans: Mapping[str, object]) -> dict:
    result = {}
    for seat in roster.seats:
        plan = plans.get(seat.seat_id)
        if plan is None:
            raise LiveDeliberationError("live roster plan is incomplete")
        public = plan.as_dict()
        result[seat.seat_id] = {
            "snapshot_digest": seat.snapshot_digest,
            "cli": seat.cli, "transport": seat.transport,
            "effective_permission": seat.effective_permission,
            "authority_class": seat.authority_class,
            "model_sha256": _sha(seat.model),
            "profile_name_sha256": _sha(seat.profile_name),
            "extra_args_sha256": public["extra_args_sha256"],
            "cwd_sha256": _sha(os.path.abspath(plan.cwd)),
            "worktree_path_sha256": seat.worktree_path_sha256,
            "worktree_head_sha256": seat.worktree_head_sha256,
            "custom_agent_definition_digest": seat.custom_agent_definition_digest,
            "custom_agent_source_digest": seat.custom_agent_source_digest,
            "output_contract": "deliberation",
        }
    return result


def _validate_provider_contract(snapshot: Mapping[str, object], *,
                                roster: FrozenRoster,
                                plans: Mapping[str, object], timeout_ms: int) -> None:
    if snapshot.get("provider_integration") != _PROVIDER_INTEGRATION:
        raise LiveDeliberationError("live provider integration identity is invalid")
    contract = snapshot.get("execution_contract")
    expected_contract = {"timeout_ms": timeout_ms,
                         **{key: False for key in _FALSE_EXECUTION_FIELDS}}
    if not isinstance(contract, dict) or contract != expected_contract:
        raise LiveDeliberationError("live execution contract is invalid")
    identities = snapshot.get("plan_identity_by_seat")
    expected = _expected_plan_identity(roster, plans)
    if (not isinstance(identities, dict) or set(identities) != set(expected)
            or identities != expected
            or any(not isinstance(value, dict) or set(value) != _PLAN_FIELDS
                   for value in identities.values())):
        raise LiveDeliberationError("live invocation plan identity is invalid")
    roster_public = roster.as_dict(native=False)
    expected_consent = {
        "full_authority": roster_public["full_authority_consent_sha256"],
        "text_only": roster_public["text_only_consent_sha256"],
    }
    consent = snapshot.get("consent_hashes")
    if (not isinstance(consent, dict)
            or set(consent) != {"full_authority", "text_only"}
            or consent != expected_consent):
        raise LiveDeliberationError("live consent evidence is invalid")


def bind_live_receipt(*, owner: _rundir.Owner, receipt: Mapping[str, object],
                      policy: DeliberationPolicy, question: str,
                      roster: FrozenRoster, plans: Mapping[str, object],
                      timeout_ms: int, clock_now: float,
                      unix_now_ms: int) -> LiveBinding:
    """Validate the owner receipt, roster identity, policy and schedule once."""
    snapshot, receipt_sha = _owner_receipt(owner, receipt)
    if (not isinstance(question, str) or not question.strip()
            or len(question.encode("utf-8", errors="surrogatepass"))
            > MAX_LIVE_QUESTION_BYTES):
        raise LiveDeliberationError("live question must be non-empty text")
    if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= MAX_LIVE_TIMEOUT_MS):
        raise LiveDeliberationError("live timeout is outside the supported bound")
    if (isinstance(unix_now_ms, bool) or not isinstance(unix_now_ms, int)
            or unix_now_ms < 1
            or abs(unix_now_ms - int(time.time() * 1000))
            > MAX_LIVE_WALL_CLOCK_SKEW_MS):
        raise LiveDeliberationError("live wall-clock authority is invalid")
    question_sha = hashlib.sha256(question.encode("utf-8",
                                                   errors="surrogatepass")).hexdigest()
    if snapshot.get("question_sha256") != question_sha:
        raise LiveDeliberationError("live question differs from the receipt")
    if snapshot.get("roster_digest") != roster.roster_digest:
        raise LiveDeliberationError("live roster differs from the receipt")
    if tuple(snapshot.get("seat_ids", ())) != tuple(seat.seat_id for seat in roster.seats):
        raise LiveDeliberationError("live seat order differs from the receipt")
    _validate_provider_contract(snapshot, roster=roster, plans=plans,
                                timeout_ms=timeout_ms)
    try:
        _receipt_binding(snapshot, policy)
        schedule = bind_schedule(snapshot, policy, clock_now=clock_now,
                                 unix_now_ms=unix_now_ms)
    except (DeliberationError, _replay.ReplayError, ValueError, TypeError) as exc:
        raise LiveDeliberationError("live receipt policy or schedule is invalid") from exc
    return LiveBinding(receipt_sha, roster.roster_digest, question_sha,
                       schedule.schedule_digest, schedule.rounds,
                       schedule.deadline_unix_ms, schedule.deadline_clock)


def build_live_scheduler(*, owner: _rundir.Owner,
                         receipt: Mapping[str, object],
                         policy: DeliberationPolicy, question: str,
                         roster: FrozenRoster, timeout_ms: int,
                         clock: Callable[[], float], unix_now_ms: int,
                         worktree_proofs: Mapping[str, WorktreeProof] | None = None,
                         cancel_requested: Callable[[], bool] | None = None,
                         deadline_clock: float | None = None,
                         _executor_for_tests=None) -> DeliberationScheduler:
    """Build one owner-bound scheduler using fresh subprocess adapters.

    The underscore-prefixed executor override is a private test seam. Production
    always uses the exact existing controlled ``execute_agent`` path.
    No retry, ACP fallback, gate, report repair, or openai-compatible HTTP
    path is enabled by this constructor.
    """
    if not callable(clock):
        raise TypeError("clock must be callable")
    if cancel_requested is not None and not callable(cancel_requested):
        raise TypeError("cancel_requested must be callable")
    if not roster.revalidate():
        raise LiveDeliberationError("live roster evidence is stale")
    cwd = roster.root_cwd
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        raise LiveDeliberationError("live roster has no valid execution scope")
    try:
        plans = build_invocation_plans(
            roster, decision_id=policy.decision_id, cwd=cwd,
            worktree_proofs=worktree_proofs)
    except Exception as exc:  # no mutable roster details cross the boundary
        raise LiveDeliberationError("live invocation planning was refused") from exc
    binding = bind_live_receipt(
        owner=owner, receipt=receipt, policy=policy, question=question,
        roster=roster, plans=plans, timeout_ms=timeout_ms,
        clock_now=clock(), unix_now_ms=unix_now_ms)
    _validate_time_authority(owner, binding, timeout_ms=timeout_ms,
                             physical_attempts=len(policy.seat_ids),
                             unix_now_ms=unix_now_ms)
    if deadline_clock is not None and deadline_clock != binding.deadline_clock:
        raise LiveDeliberationError("caller deadline differs from receipt authority")
    if binding.rounds != 1 or policy.max_attempts != len(policy.seat_ids):
        raise LiveDeliberationError(
            "phase-b permits exactly one physical attempt per seat")
    if any(seat.cli == "kimi" for seat in roster.seats):
        raise LiveDeliberationError(
            "kimi live deliberation is disabled until source credentials are receipt-bound")

    external_cancel = cancel_requested or (lambda: False)
    owner_current = lambda: _owner_current_with_lease(owner)
    chosen_executor = (_executor.execute_agent if _executor_for_tests is None
                       else _executor_for_tests)
    if not callable(chosen_executor):
        raise TypeError("_executor_for_tests must be callable")

    def controlled_execute(invocation, **kwargs):
        try:
            response = chosen_executor(invocation, **kwargs)
        except BaseException:
            raise
        if (isinstance(response, Mapping)
                and (response.get("timeout") is True
                     or response.get("exit_code") == 124)):
            raise LiveDeliberationError("controlled provider attempt timed out")
        return response

    holder: dict[str, DeliberationScheduler] = {}
    deadline_latched = [False]

    def wall_deadline_reached() -> bool:
        """Use the receipt's Unix deadline as the final provider fence.

        ``clock`` remains injectable for deterministic kernel tests, but it is
        not trusted for paid-contact authorization.  The launch control gets
        this wall-clock check immediately before Popen/ACP contact.
        """
        if (clock() >= binding.deadline_clock
                or int(time.time() * 1000) >= binding.deadline_unix_ms):
            deadline_latched[0] = True
        return deadline_latched[0]

    def authoritative_clock() -> float:
        """Project wall-clock expiry into the kernel's deterministic clock."""
        return (binding.deadline_clock if wall_deadline_reached() else clock())

    def shared_cancel() -> bool:
        scheduler = holder.get("scheduler")
        # Deadline is a separate launch-control predicate.  Treating it as
        # user cancellation would mislabel an ordinary expiry as CANCELLED;
        # the executor reports a bounded timeout and the kernel records
        # TIMED_OUT instead.
        return (bool(external_cancel())
                or bool(scheduler is not None
                        and scheduler._cancel_event.is_set()))

    children: dict[str, FreshDispatchAdapter] = {}
    for seat in roster.seats:
        plan = plans.get(seat.seat_id)
        if plan is None:
            raise LiveDeliberationError("live roster plan is incomplete")
        template = plan.template
        expected_snapshot = seat.snapshot_digest

        def current_snapshot(*, expected=expected_snapshot,
                             receipt_sha= binding.receipt_sha256) -> str:
            try:
                return (expected if roster.revalidate()
                        and _owner_receipt_still_matches(owner, receipt_sha)
                        and owner_current()
                        else "0" * 64)
            except Exception:
                return "0" * 64

        def invocation_for_context(context, *, selected=plan):
            scheduler = holder.get("scheduler")
            if scheduler is None:
                raise LiveDeliberationError("live scheduler prompt authority is not ready")
            return selected.for_context(context, scheduler.prompt_for(context))

        children[seat.seat_id] = FreshDispatchAdapter(
            template, snapshot_digest=expected_snapshot,
            current_snapshot_digest=current_snapshot,
            owner_is_current=owner_current,
            timeout_ms=timeout_ms, generation=owner.generation,
            invocation_for_context=invocation_for_context,
            cancelled=shared_cancel, deadline_reached=wall_deadline_reached,
            executor=controlled_execute)

    adapter = SeatMultiplexAdapter(children)
    durable = lambda event: _rundir.journal_append(owner.run_dir, event, owner=owner)
    scheduler = DeliberationScheduler(
        question=question, policy=policy, seat_resolver={
            seat.seat_id: seat.seat_definition for seat in roster.seats},
        adapter=adapter, generation=owner.generation,
        durable_append=durable,
        owner_is_current=owner_current,
        deadline=binding.deadline_clock,
        clock=authoritative_clock, rounds=binding.rounds,
        cancel_requested=shared_cancel, live_provider=False)
    activation = (os.path.realpath(owner.run_dir), owner.nonce)
    with _ACTIVATION_LOCK:
        if activation in _ACTIVATED_OWNERS:
            raise LiveDeliberationError(
                "live owner already has an activated scheduler")
        _ACTIVATED_OWNERS.add(activation)
    holder["scheduler"] = scheduler
    return scheduler
