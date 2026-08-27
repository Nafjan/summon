"""Production fresh-dispatch bridge for the deliberation kernel.

The bridge deliberately wraps :func:`_executor.execute_agent` directly.  It
does not use the dispatcher's retry, gate, fallback, or report-repair helpers.
One instance represents one immutable seat execution identity; a context factory
may select one prompt-specific invocation per prepared turn. Each committed
attempt gets one single-use :class:`_executor.ProviderLaunchControl`.
"""

from __future__ import annotations

import hashlib
import json
import copy
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from _builder import AgentInvocation
from _deliberation import (AdapterResult, CleanupReceipt, DeliberationError,
                           DuplicateAttemptError, ExecutionEvidence,
                           LaunchSpec, LaunchToken, SnapshotDriftError,
                           TurnContext)
from _executor import ProviderLaunchControl, execute_agent


@dataclass(frozen=True)
class _PreparedInvocation:
    """The exact invocation selected before a durable attempt commit."""

    context: TurnContext
    invocation: AgentInvocation
    prompt_digest: str


@dataclass(frozen=True)
class _TrackedResource:
    path: str
    kind: str
    runs_root: str
    marker_name: str
    marker_value: str
    identity: tuple[int, int]


def _profile_runs_root(kind: str) -> str | None:
    """Return the only disposable profile root controlled by a builder kind."""
    if kind == "agy-profile":
        base = os.environ.get("AGY_HEADLESS_PROFILE") or os.path.join(
            os.path.expanduser("~"), ".agents", "state", "agy-headless-profile")
    elif kind == "kimi-profile":
        base = os.environ.get("KIMI_HEADLESS_PROFILE") or os.path.join(
            tempfile.gettempdir(), "summon-kimi-headless-profile")
    else:
        return None
    return os.path.realpath(os.path.join(base, "runs"))


class FreshDispatchAdapter:
    """Bind immutable seat execution identity to one-attempt executor calls.

    ``invocation_for_context`` is the scheduler hook for multi-turn runs.  It
    may change only the prompt: CLI, transport, model, permission, profile,
    cwd, and every other execution field remain bound to the constructor's
    invocation.  The factory is called once by :meth:`prepare`; the exact
    resulting invocation is retained through commit and launch, so a mutable
    transcript cannot cause a second prompt to be sent after the durable
    attempt boundary.
    """

    def __init__(
        self,
        invocation: AgentInvocation,
        *,
        snapshot_digest: str,
        current_snapshot_digest: Callable[[], str],
        owner_is_current: Callable[[], bool],
        timeout_ms: int,
        generation: int,
        invocation_for_context: Callable[[TurnContext], AgentInvocation] | None = None,
        cancelled: Callable[[], bool] | None = None,
        deadline_reached: Callable[[], bool] | None = None,
        prelaunch_revalidate: Callable[[], None] | None = None,
        parse_output: Callable[[str], Mapping[str, object] | None] | None = None,
        executor: Callable[..., dict] = execute_agent,
        debug_dir: str | None = None,
    ) -> None:
        if not isinstance(invocation, AgentInvocation):
            raise TypeError("invocation must be an AgentInvocation")
        # The stdlib urllib transport cannot be interrupted once opener.open()
        # has contacted a provider.  A deliberation seat must be cancellable
        # and cleanup-honest, so keep this backend out of the production bridge
        # until a bounded, cancellable transport is available.  The lower-level
        # launch-control seam remains covered for future transport work.
        if invocation.cli == "openai-compat":
            raise ValueError(
                "openai-compat deliberation seats are disabled until provider "
                "cancellation is bounded")
        if (not isinstance(snapshot_digest, str) or len(snapshot_digest) != 64 or
                any(ch not in "0123456789abcdef" for ch in snapshot_digest)):
            raise ValueError("snapshot_digest must be a lowercase sha256")
        if not callable(current_snapshot_digest):
            raise TypeError("current_snapshot_digest must be callable")
        if not callable(owner_is_current):
            raise TypeError("owner_is_current must be callable")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive integer")
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 1):
            raise ValueError("generation must be a positive integer")
        try:
            # AgentInvocation is frozen only at the outer dataclass boundary;
            # profile_env is a mutable mapping. Keep a private deep copy so
            # caller mutations cannot alter the physical launch after prepare.
            self._invocation = copy.deepcopy(invocation)
        except Exception as exc:  # noqa: BLE001 - fail closed without details
            raise TypeError("invocation must be safely copyable") from exc
        self._invocation_identity = replace(self._invocation, prompt="")
        self._generation = generation
        self._invocation_for_context = invocation_for_context
        if (invocation_for_context is not None
                and not callable(invocation_for_context)):
            raise TypeError("invocation_for_context must be callable")
        self._snapshot_digest = snapshot_digest
        self._current_snapshot_digest = current_snapshot_digest
        # Ownership is a required production invariant.  Do not silently
        # replace a missing callback with an always-true test hook: a future
        # scheduler wiring omission must fail at construction, not disable the
        # final takeover fence.
        self._owner_is_current = owner_is_current
        self._timeout_ms = timeout_ms
        self._cancelled = cancelled or (lambda: False)
        if deadline_reached is not None and not callable(deadline_reached):
            raise TypeError("deadline_reached must be callable")
        self._deadline_reached = deadline_reached or (lambda: False)
        if prelaunch_revalidate is not None and not callable(prelaunch_revalidate):
            raise TypeError("prelaunch_revalidate must be callable")
        self._prelaunch_revalidate = prelaunch_revalidate or (lambda: None)
        self._parse_output = parse_output or self._parse_json_object
        self._executor = executor
        self._debug_dir = debug_dir
        self._lock = threading.Lock()
        self._prepared: dict[str, _PreparedInvocation] = {}
        self._launched: set[str] = set()
        self._consumed_specs: set[str] = set()
        self._live_handles: dict[int, object] = {}
        self._resources: dict[str, _TrackedResource] = {}
        self._cleanup_started = False

    @staticmethod
    def _parse_json_object(text: str) -> Mapping[str, object] | None:
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _snapshot_current(self) -> bool:
        try:
            return self._current_snapshot_digest() == self._snapshot_digest
        except Exception:
            return False

    def _invocation_for(self, context: TurnContext) -> AgentInvocation:
        """Resolve one immutable invocation for ``context`` without launching."""
        try:
            invocation = (self._invocation_for_context(context)
                          if self._invocation_for_context is not None
                          else self._invocation)
        except Exception as exc:  # noqa: BLE001 - do not leak factory details
            raise SnapshotDriftError(
                "invocation factory failed before attempt commitment") from exc
        if not isinstance(invocation, AgentInvocation):
            raise SnapshotDriftError(
                "invocation factory did not return an AgentInvocation")
        try:
            invocation = copy.deepcopy(invocation)
        except Exception as exc:  # noqa: BLE001 - fail closed without details
            raise SnapshotDriftError(
                "invocation factory result is not safely copyable") from exc
        if replace(invocation, prompt="") != self._invocation_identity:
            raise SnapshotDriftError(
                "invocation factory changed immutable execution identity")
        if invocation.cli == "openai-compat":
            raise ValueError(
                "openai-compat deliberation seats are disabled until provider "
                "cancellation is bounded")
        return invocation

    def prepare(self, context: TurnContext) -> LaunchSpec:
        """Build an immutable, prompt-free spec; no provider operation occurs."""
        invocation = self._invocation_for(context)
        prompt_digest = hashlib.sha256(
            invocation.prompt.encode("utf-8", errors="surrogatepass")).hexdigest()
        payload = json.dumps({
            "schema_version": 1,
            "decision_id": context.decision_id,
            "seat_id": context.seat_id,
            "turn_id": context.turn_id,
            "turn_ordinal": context.turn_ordinal,
            "request_digest": context.request_digest,
            # Bind the immutable context to the exact prompt carried by this
            # invocation.  A context hash by itself is not evidence that the
            # executor will send this invocation's prompt.
            "prompt_digest": prompt_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        model_digest = hashlib.sha256(
            (invocation.model or "default").encode("utf-8")).hexdigest()[:16]
        spec = LaunchSpec(
            transport=invocation.transport,
            command_identity=(f"{invocation.cli}:"
                              f"{invocation.transport}:{model_digest}"),
            sealed_payload=payload,
            snapshot_digest=self._snapshot_digest,
        )
        with self._lock:
            self._prepared[spec.digest] = _PreparedInvocation(
                context=context, invocation=invocation,
                prompt_digest=prompt_digest)
        return spec

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        with self._lock:
            prepared = self._prepared.get(spec.digest)
        return (prepared is not None and prepared.context == context
                and context.request_digest == prepared.prompt_digest
                and spec.snapshot_digest == self._snapshot_digest
                and self._snapshot_current() and self._owner_is_current())

    def _on_spawn(self, handle: object) -> None:
        with self._lock:
            self._live_handles[id(handle)] = handle

    def _on_reap(self, handle: object) -> None:
        with self._lock:
            self._live_handles.pop(id(handle), None)

    def _on_resource(self, resource: object, kind: str) -> None:
        """Capture a builder-created disposable profile without journaling its path."""
        with self._lock:
            if self._cleanup_started:
                raise ValueError("controlled provider resource arrived after cleanup")
        if not isinstance(resource, str) or not os.path.isabs(resource):
            raise ValueError("controlled provider resource is not an absolute path")
        runs_root = _profile_runs_root(kind)
        real = os.path.realpath(resource)
        if (runs_root is None or os.path.islink(resource)
                or not os.path.isdir(real)
                or os.path.normcase(os.path.dirname(real)) != os.path.normcase(runs_root)
                or not os.path.basename(real).startswith("run-")):
            # Never accept a caller-selected profile or an arbitrary path as a
            # deletion capability.  The error contains no path or credential.
            raise ValueError("controlled provider resource is not a disposable profile")
        marker_value = secrets.token_hex(24)
        marker_name = ".summon-resource-marker-" + secrets.token_hex(12)
        marker_path = os.path.join(real, marker_name)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(marker_path, flags, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as marker:
                    marker.write(marker_value)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            st = os.stat(real, follow_symlinks=False)
        except Exception as exc:  # noqa: BLE001 - never grant an unmarked delete
            try:
                os.unlink(marker_path)
            except OSError:
                pass
            raise ValueError("controlled provider resource marker could not be created") from exc
        tracked = _TrackedResource(real, kind, runs_root, marker_name,
                                   marker_value, (int(st.st_dev), int(st.st_ino)))
        with self._lock:
            if self._cleanup_started:
                try:
                    os.unlink(marker_path)
                except OSError:
                    pass
                raise ValueError("controlled provider resource arrived after cleanup")
            existing = self._resources.get(real)
            if existing is not None and existing != tracked:
                try:
                    os.unlink(marker_path)
                except OSError:
                    pass
                raise ValueError("controlled provider resource was rebound")
            self._resources[real] = tracked

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        if token.binding.launch_spec_digest != spec.digest:
            raise DeliberationError("launch token does not bind this launch spec")
        with self._lock:
            prepared = self._prepared.get(spec.digest)
        if prepared is None:
            raise DeliberationError("launch spec was not prepared by this adapter")
        binding = token.binding
        context = prepared.context
        if (binding.generation != self._generation
                or binding.decision_id != context.decision_id
                or binding.seat_id != context.seat_id
                or binding.turn_id != context.turn_id
                or binding.turn_ordinal != context.turn_ordinal):
            raise DeliberationError(
                "launch token does not bind the prepared decision turn")
        attempt_id = token.binding.attempt_id
        with self._lock:
            if (attempt_id in self._launched
                    or spec.digest in self._consumed_specs):
                raise DuplicateAttemptError(
                    f"launch spec or attempt already consumed: {attempt_id}")
            self._launched.add(attempt_id)
            # A retry must prepare a fresh spec. Consume this spec before any
            # provider boundary, including a pre-provider refusal.
            self._consumed_specs.add(spec.digest)

        def _before_launch(_evidence: Mapping[str, object]) -> None:
            # This callback runs inside the executor immediately before the
            # irreversible Popen/open operation, closing the prepare->spawn TOCTOU.
            # This is the final fence, after the scheduler's durable commit and
            # immediately before provider contact.  It closes the takeover
            # window between AttemptLedger.launch_once() and this callback.
            with self._lock:
                current = self._prepared.get(spec.digest)
                cleanup_started = self._cleanup_started
            if (current is None or cleanup_started
                    or not self._owner_is_current()
                    or not self.revalidate(spec, current.context)):
                raise SnapshotDriftError(
                    "owner or participant snapshot changed at provider launch boundary")
            self._prelaunch_revalidate()

        control = ProviderLaunchControl(
            before_launch=_before_launch,
            on_spawn=self._on_spawn,
            on_reap=self._on_reap,
            on_resource=self._on_resource,
            cancelled=self._cancelled,
            deadline_reached=self._deadline_reached,
            allow_secondary=False,
        )
        response = self._executor(
            prepared.invocation,
            timeout_ms=self._timeout_ms,
            debug_dir=self._debug_dir,
            launch_control=control,
        )
        raw_exit = response.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        timed_out = bool(response.get("timeout")) or exit_code == 124
        # Summon intentionally terminates some stream CLIs after their terminal
        # event.  Those two SIGTERM spellings are executor-owned success exits.
        # Some controlled CLI wrappers (notably Antigravity/agy on Windows)
        # return a non-zero raw process code after emitting a clean terminal
        # event.  The executor preserves that raw code but normalizes the
        # durable response to status=success.  Treat that explicit normalized
        # status as transport success; raw non-zero without it remains a
        # transport failure and cannot reach ballot acceptance.
        normalized_success = response.get("status") == "success"
        transport_ok = (not timed_out and
                        (exit_code in (0, 143, -15) or normalized_success))
        prose = response.get("result") if isinstance(response.get("result"), str) else ""
        structured = self._parse_output(prose)
        model_served = None
        model_targeted = None
        model = response.get("model")
        if isinstance(model, Mapping):
            model_served = model.get("served")
            model_targeted = model.get("targeted")
        elif isinstance(model, str):
            model_served = model
        safe_model = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+() -]{0,159}$")
        if not isinstance(model_served, str) or safe_model.fullmatch(model_served) is None:
            model_served = None
        if not isinstance(model_targeted, str) or safe_model.fullmatch(model_targeted) is None:
            model_targeted = None
        evidence = ExecutionEvidence(
            transport_ok=transport_ok,
            exit_code=exit_code,
            timed_out=timed_out,
            parser_valid=structured is not None,
            model_served=model_served,
            model_targeted=model_targeted,
            error_kind=(response.get("error_kind")
                        if response.get("error_kind") == "context_source_drift"
                        else None),
        )
        return AdapterResult(evidence=evidence, structured_output=structured,
                             model_prose=prose)

    def cleanup(self) -> CleanupReceipt:
        """Kill/reap handles and remove only validated disposable profiles."""
        with self._lock:
            self._cleanup_started = True
            handles = list(self._live_handles.values())
        retained: list[str] = []
        for handle in handles:
            if isinstance(handle, subprocess.Popen):
                try:
                    from _executor import _kill_tree
                    _kill_tree(handle)
                    handle.wait(timeout=3.0)
                except Exception:
                    retained.append("registered-process:cleanup-unverified")
                finally:
                    self._on_reap(handle)
            else:
                # HTTP request objects have no portable cancellation handle.
                # The executor normally unregisters them in a finally block;
                # if one remains, report rather than claim cleanup.
                retained.append("registered-provider-operation:cleanup-unverified")
        with self._lock:
            resources = list(self._resources.values())
        for resource in resources:
            real = os.path.realpath(resource.path)
            safe = (not os.path.islink(resource.path)
                    and os.path.normcase(os.path.dirname(real))
                    == os.path.normcase(resource.runs_root)
                    and os.path.basename(real).startswith("run-"))
            marker_path = os.path.join(real, resource.marker_name)
            if safe:
                try:
                    st = os.stat(real, follow_symlinks=False)
                    identity_ok = ((int(st.st_dev), int(st.st_ino))
                                   == resource.identity)
                    with open(marker_path, "r", encoding="ascii") as marker:
                        marker_ok = (not os.path.islink(marker_path)
                                     and marker.read() == resource.marker_value)
                    safe = identity_ok and marker_ok
                except (OSError, UnicodeError):
                    safe = False
            if not safe:
                retained.append(f"{resource.kind}:cleanup-unverified")
                continue
            try:
                if os.path.exists(real):
                    shutil.rmtree(real)
                if os.path.lexists(real):
                    raise OSError("profile remains")
            except Exception:  # noqa: BLE001 - receipt must remain honest
                retained.append(f"{resource.kind}:cleanup-unverified")
            else:
                with self._lock:
                    self._resources.pop(resource.path, None)
        return CleanupReceipt(verified=not retained,
                              retained_resources=tuple(retained))


class SeatMultiplexAdapter:
    """Route one deliberation adapter port across immutable seat adapters.

    The scheduler owns one :class:`FreshDispatchAdapter` per seat.  A prepared
    launch spec is the routing capability: after ``prepare`` the multiplexer
    never chooses a child from caller-supplied context, only from the exact
    spec digest it recorded.  This prevents a cross-seat context substitution
    from reaching the wrong provider and lets each child retain its own launch
    and cleanup fences.
    """

    def __init__(self, children: Mapping[str, FreshDispatchAdapter]) -> None:
        if not isinstance(children, Mapping):
            raise TypeError("children must be a seat-to-adapter mapping")
        copied = dict(children)
        if not copied:
            raise ValueError("at least one seat adapter is required")
        for seat_id, child in copied.items():
            if not isinstance(seat_id, str) or not seat_id:
                raise ValueError("seat adapter ids must be non-empty strings")
            if not isinstance(child, FreshDispatchAdapter):
                raise TypeError("seat adapter values must be FreshDispatchAdapter instances")
        # Keep the caller's dictionary from changing routing after construction.
        self._children = MappingProxyType(copied)
        self._lock = threading.Lock()
        self._prepared: dict[str, FreshDispatchAdapter] = {}

    def prepare(self, context: TurnContext) -> LaunchSpec:
        """Prepare through the seat named by ``context`` and bind its digest."""
        child = self._children.get(context.seat_id)
        if child is None:
            raise DeliberationError(
                f"no deliberation adapter is configured for seat {context.seat_id!r}")
        spec = child.prepare(context)
        with self._lock:
            previous = self._prepared.get(spec.digest)
            if previous is not None and previous is not child:
                raise DeliberationError(
                    "launch spec digest is already bound to another seat adapter")
            self._prepared[spec.digest] = child
        return spec

    def _child_for_spec(self, spec: LaunchSpec) -> FreshDispatchAdapter | None:
        with self._lock:
            return self._prepared.get(spec.digest)

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        """Revalidate only through the child bound to ``spec.digest``."""
        child = self._child_for_spec(spec)
        if child is None:
            return False
        return child.revalidate(spec, context)

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        """Launch only through the child bound to ``spec.digest``."""
        child = self._child_for_spec(spec)
        if child is None:
            raise DeliberationError("launch spec was not prepared by this adapter")
        return child.launch(spec, token)

    def cleanup(self) -> CleanupReceipt:
        """Clean every child and report any unverified result or failure."""
        verified = True
        retained: list[str] = []
        unverified_pids: list[int] = []
        # Sorting makes the aggregate deterministic and keeps receipts stable
        # across equivalent mapping construction orders.
        for seat_id, child in sorted(self._children.items()):
            try:
                receipt = child.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not hide a leak
                verified = False
                retained.append(f"seat:{seat_id}:cleanup-unverified")
                continue
            if not isinstance(receipt, CleanupReceipt):
                verified = False
                retained.append(f"seat:{seat_id}:cleanup-invalid-receipt")
                continue
            verified = verified and receipt.verified
            retained.extend(receipt.retained_resources)
            unverified_pids.extend(receipt.unverified_pids)
        return CleanupReceipt(
            verified=verified,
            retained_resources=tuple(retained),
            unverified_pids=tuple(unverified_pids),
        )
