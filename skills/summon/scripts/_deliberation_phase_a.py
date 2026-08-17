"""Provider-inert Phase-A composition for deliberation.

This module deliberately has no executor, subprocess, socket, HTTP, PATH, profile
discovery, or CLI surface.  It composes the existing scheduler with an explicitly
injected fake contact port so durable-boundary and crash-prefix behavior can be
proved before any vendor is made reachable.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from _deliberation import (AdapterResult, CleanupReceipt,
                           DeliberationError, DeliberationPolicy,
                           DuplicateAttemptError, LaunchSpec, LaunchToken,
                           SnapshotDriftError, TurnContext, _receipt_binding,
                           _schedule_binding)
from _deliberation_scheduler import (DeliberationScheduler, SeatDefinition,
                                     SeatResolver)


class PhaseAError(DeliberationError):
    """The provider-inert authorization boundary refused an operation."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class PhaseASchedule:
    receipt_sha256: str
    schedule_digest: str
    rounds: int
    deadline_unix_ms: int
    deadline_clock: float


@dataclass(frozen=True)
class FakeContact:
    """Prompt-free identity passed to the injected fake process port."""

    attempt_id: str
    launch_spec_digest: str
    request_digest: str


@dataclass(frozen=True)
class _Resource:
    resource_id: str
    identity_digest: str
    current_identity: Callable[[], str]


def bind_schedule(receipt: Mapping[str, object], policy: DeliberationPolicy, *,
                  clock_now: float, unix_now_ms: int) -> PhaseASchedule:
    """Canonicalize one receipt and project its absolute deadline exactly once."""
    try:
        raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        frozen = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PhaseAError("phase-a receipt is not canonical JSON") from exc
    if not isinstance(frozen, dict):
        raise PhaseAError("phase-a receipt must be an object")
    receipt_sha, _policy_sha = _receipt_binding(frozen, policy)
    rounds, deadline_unix_ms, schedule_sha = _schedule_binding(frozen)
    if (isinstance(unix_now_ms, bool) or not isinstance(unix_now_ms, int)
            or abs(unix_now_ms) > 9_223_372_036_854_775_807
            or isinstance(clock_now, bool) or not isinstance(clock_now, (int, float))
            or not math.isfinite(float(clock_now))):
        raise PhaseAError("phase-a clocks are invalid")
    try:
        deadline_clock = float(clock_now) + (deadline_unix_ms - unix_now_ms) / 1000.0
    except (OverflowError, ValueError):
        raise PhaseAError("phase-a deadline projection is invalid") from None
    if not math.isfinite(deadline_clock):
        raise PhaseAError("phase-a deadline projection is invalid")
    return PhaseASchedule(receipt_sha, schedule_sha, rounds,
                          deadline_unix_ms, deadline_clock)


class PhaseAAdapter:
    """Single-attempt fake contact adapter with production-shaped fences."""

    def __init__(self, *, snapshot_digest: str, profile_revision_digest: str,
                 current_snapshot_digest: Callable[[], str],
                 current_profile_revision_digest: Callable[[], str],
                 owner_is_current: Callable[[], bool], generation: int, deadline: float,
                 clock: Callable[[], float],
                 fake_contact: Callable[[FakeContact], AdapterResult],
                 fake_cleanup: Callable[[str, str], bool] | None = None) -> None:
        for name, value in (("snapshot", snapshot_digest),
                            ("profile revision", profile_revision_digest)):
            if (not isinstance(value, str) or len(value) != 64
                    or any(ch not in "0123456789abcdef" for ch in value)):
                raise ValueError(f"{name} digest must be a lowercase sha256")
        for name, callback in (
                ("snapshot", current_snapshot_digest),
                ("profile revision", current_profile_revision_digest),
                ("owner", owner_is_current), ("clock", clock),
                ("fake contact", fake_contact)):
            if not callable(callback):
                raise TypeError(f"{name} callback must be callable")
        if not isinstance(deadline, (int, float)) or not math.isfinite(float(deadline)):
            raise ValueError("deadline must be finite")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("generation must be positive")
        self._snapshot = snapshot_digest
        self._profile_revision = profile_revision_digest
        self._current_snapshot = current_snapshot_digest
        self._current_profile = current_profile_revision_digest
        self._owner = owner_is_current
        self._generation = generation
        self._deadline = float(deadline)
        self._clock = clock
        self._contact = fake_contact
        self._cleanup = fake_cleanup or (lambda _resource_id, _identity: True)
        self._lock = threading.Lock()
        self._cleanup_condition = threading.Condition(self._lock)
        self._prepared: dict[str, TurnContext] = {}
        self._attempts: dict[str, str] = {}
        self._attempt_specs: dict[str, str] = {}
        self._consumed_specs: set[str] = set()
        self._resources: dict[str, _Resource] = {}
        self._quarantine: set[str] = set()
        self._closed = False
        self._cleanup_in_progress = False
        self._cleanup_result: CleanupReceipt | None = None

    def _fresh(self) -> bool:
        try:
            return (not self._closed and self._owner()
                    and self._clock() < self._deadline
                    and self._current_snapshot() == self._snapshot
                    and self._current_profile() == self._profile_revision)
        except Exception:
            return False

    def prepare(self, context: TurnContext) -> LaunchSpec:
        if not self._fresh():
            raise SnapshotDriftError("phase-a authorization evidence is stale or expired")
        payload = json.dumps({
            "schema_version": 1, "decision_id": context.decision_id,
            "seat_id": context.seat_id, "turn_id": context.turn_id,
            "turn_ordinal": context.turn_ordinal,
            "request_digest": context.request_digest,
            "profile_revision_digest": self._profile_revision,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        spec = LaunchSpec("phase-a-fake", "phase-a:injected-fake", payload,
                          self._snapshot)
        with self._lock:
            if self._closed:
                raise PhaseAError("phase-a adapter is closed")
            self._prepared[spec.digest] = context
        return spec

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        with self._lock:
            prepared = self._prepared.get(spec.digest)
        return prepared == context and spec.snapshot_digest == self._snapshot and self._fresh()

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        binding = token.binding
        with self._lock:
            context = self._prepared.get(spec.digest)
            if context is None or binding.launch_spec_digest != spec.digest:
                raise PhaseAError("phase-a token is not bound to a prepared contact")
            if (binding.attempt_id in self._attempts
                    or spec.digest in self._consumed_specs):
                raise DuplicateAttemptError(
                    "phase-a attempt or launch spec was already delivered")
            if binding.generation != self._generation:
                raise PhaseAError("phase-a attempt generation is stale")
            if (binding.decision_id, binding.seat_id, binding.turn_id,
                    binding.turn_ordinal) != (context.decision_id, context.seat_id,
                                               context.turn_id, context.turn_ordinal):
                raise PhaseAError("phase-a attempt binding differs from its context")
            # A prepared spec is one physical-delivery capability.  Consume it
            # after immutable binding validation but before every final fence,
            # including refusal, so forged input cannot burn the capability and
            # a valid caller cannot relabel it under another attempt id.
            self._consumed_specs.add(spec.digest)
            # Consume before the last fence: a refused or ambiguous delivery can
            # never be retried under the same durable attempt.
            self._attempts[binding.attempt_id] = "spawn_not_possible"
            self._attempt_specs[binding.attempt_id] = spec.digest
        if not self.revalidate(spec, context):
            raise SnapshotDriftError("phase-a evidence changed immediately before contact")
        with self._lock:
            self._attempts[binding.attempt_id] = "contact_possible"
        try:
            result = self._contact(FakeContact(
                binding.attempt_id, spec.digest, context.request_digest))
        except BaseException:
            with self._lock:
                self._attempts[binding.attempt_id] = "uncertain_spend"
            raise
        if not isinstance(result, AdapterResult):
            with self._lock:
                self._attempts[binding.attempt_id] = "uncertain_spend"
            raise PhaseAError("fake contact returned an invalid result")
        with self._lock:
            # The kernel still owns the durable attempt_finished append.  Do
            # not call an observed result "committed" until observe_durable()
            # sees that owner-bound callback return successfully.
            self._attempts[binding.attempt_id] = "result_available"
        return result

    def observe_durable(self, event: Mapping[str, object]) -> None:
        """Advance lifecycle only after the caller's durable append returned."""
        if not isinstance(event, Mapping) or event.get("event") != "attempt_finished":
            return
        attempt_id = event.get("attempt_id")
        spec_digest = event.get("launch_spec_sha256")
        with self._lock:
            if (not isinstance(attempt_id, str)
                    or self._attempts.get(attempt_id) != "result_available"):
                raise PhaseAError("durable result has no matching available result")
            if event.get("generation") != self._generation:
                raise PhaseAError("durable result generation is stale")
            if self._attempt_specs.get(attempt_id) != spec_digest:
                raise PhaseAError("durable result differs from the consumed launch spec")
            self._attempts[attempt_id] = "result_committed"

    def register_resource(self, resource_id: str, identity_digest: str,
                          current_identity: Callable[[], str]) -> None:
        if (not isinstance(resource_id, str) or not _SAFE_ID.fullmatch(resource_id)
                or not callable(current_identity)):
            raise ValueError("fake resource identity is invalid")
        if (not isinstance(identity_digest, str) or len(identity_digest) != 64
                or any(ch not in "0123456789abcdef" for ch in identity_digest)):
            raise ValueError("fake resource digest is invalid")
        with self._lock:
            if self._closed or resource_id in self._resources:
                raise PhaseAError("fake resource registration is closed or duplicate")
            self._resources[resource_id] = _Resource(
                resource_id, identity_digest, current_identity)

    def cleanup(self) -> CleanupReceipt:
        with self._cleanup_condition:
            if self._cleanup_result is not None:
                return self._cleanup_result
            if self._cleanup_in_progress:
                while self._cleanup_result is None:
                    self._cleanup_condition.wait()
                return self._cleanup_result
            resources = tuple(self._resources.values())
            self._closed = True
            self._cleanup_in_progress = True
        result: CleanupReceipt | None = None
        try:
            retained: list[str] = []
            for resource in resources:
                try:
                    same = resource.current_identity() == resource.identity_digest
                except Exception:
                    same = False
                if not same:
                    self._quarantine.add(resource.resource_id)
                    retained.append(f"quarantine:{resource.resource_id}")
                    continue
                try:
                    removed = self._cleanup(resource.resource_id, resource.identity_digest) is True
                except Exception:
                    removed = False
                if not removed:
                    self._quarantine.add(resource.resource_id)
                    retained.append(f"quarantine:{resource.resource_id}")
            result = CleanupReceipt(not retained, tuple(sorted(retained)))
        except Exception:
            result = CleanupReceipt(False, ("quarantine:cleanup-error",))
        finally:
            with self._cleanup_condition:
                self._cleanup_result = result or CleanupReceipt(
                    False, ("quarantine:cleanup-error",))
                self._cleanup_in_progress = False
                self._cleanup_condition.notify_all()
        return self._cleanup_result

    @property
    def lifecycle(self) -> Mapping[str, str]:
        with self._lock:
            values = dict(self._attempts)
            if self._closed:
                values["_adapter"] = ("quarantine/closed" if self._quarantine
                                      else "cleanup_verified/closed")
        return MappingProxyType(values)


def build_scheduler(*, receipt: Mapping[str, object], policy: DeliberationPolicy,
                    question: str, seat_resolver: SeatResolver | Mapping[str, SeatDefinition],
                    adapter: PhaseAAdapter, generation: int,
                    durable_append: Callable[[dict], None],
                    owner_is_current: Callable[[], bool], clock: Callable[[], float],
                    unix_now_ms: int, cancel_requested: Callable[[], bool] | None = None
                    ) -> DeliberationScheduler:
    """Compose a fake-only scheduler from receipt-bound schedule authority."""
    now = clock()
    schedule = bind_schedule(receipt, policy, clock_now=now,
                             unix_now_ms=unix_now_ms)
    if (adapter._deadline != schedule.deadline_clock
            or adapter._generation != generation
            or adapter._clock is not clock
            or adapter._owner is not owner_is_current):
        raise PhaseAError("phase-a adapter is not bound to scheduler authority")
    def append_and_observe(event: dict) -> None:
        durable_append(event)
        adapter.observe_durable(event)

    return DeliberationScheduler(
        question=question, policy=policy, seat_resolver=seat_resolver,
        adapter=adapter, generation=generation, durable_append=append_and_observe,
        owner_is_current=owner_is_current, deadline=schedule.deadline_clock,
        clock=clock, rounds=schedule.rounds,
        cancel_requested=cancel_requested, live_provider=False)
