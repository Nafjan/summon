"""Production fresh-dispatch bridge for the deliberation kernel.

The bridge deliberately wraps :func:`_executor.execute_agent` directly.  It
does not use the dispatcher's retry, gate, fallback, or report-repair helpers.
One instance represents one immutable seat invocation and creates one
single-use :class:`_executor.ProviderLaunchControl` per committed attempt.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Callable, Mapping

from _builder import AgentInvocation
from _deliberation import (AdapterResult, CleanupReceipt, DeliberationError,
                           DuplicateAttemptError, ExecutionEvidence,
                           LaunchSpec, LaunchToken, SnapshotDriftError,
                           TurnContext)
from _executor import ProviderLaunchControl, execute_agent


class FreshDispatchAdapter:
    """Bind one immutable invocation snapshot to one-attempt executor calls."""

    def __init__(
        self,
        invocation: AgentInvocation,
        *,
        snapshot_digest: str,
        current_snapshot_digest: Callable[[], str],
        owner_is_current: Callable[[], bool],
        timeout_ms: int,
        cancelled: Callable[[], bool] | None = None,
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
        self._invocation = invocation
        self._snapshot_digest = snapshot_digest
        self._current_snapshot_digest = current_snapshot_digest
        # Ownership is a required production invariant.  Do not silently
        # replace a missing callback with an always-true test hook: a future
        # scheduler wiring omission must fail at construction, not disable the
        # final takeover fence.
        self._owner_is_current = owner_is_current
        self._prompt_digest = hashlib.sha256(
            invocation.prompt.encode("utf-8", errors="surrogatepass")).hexdigest()
        self._timeout_ms = timeout_ms
        self._cancelled = cancelled or (lambda: False)
        self._parse_output = parse_output or self._parse_json_object
        self._executor = executor
        self._debug_dir = debug_dir
        self._lock = threading.Lock()
        self._prepared: dict[str, TurnContext] = {}
        self._launched: set[str] = set()
        self._live_handles: dict[int, object] = {}

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

    def prepare(self, context: TurnContext) -> LaunchSpec:
        """Build an immutable, prompt-free spec; no provider operation occurs."""
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
            "prompt_digest": self._prompt_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        model_digest = hashlib.sha256(
            (self._invocation.model or "default").encode("utf-8")).hexdigest()[:16]
        spec = LaunchSpec(
            transport=self._invocation.transport,
            command_identity=(f"{self._invocation.cli}:"
                              f"{self._invocation.transport}:{model_digest}"),
            sealed_payload=payload,
            snapshot_digest=self._snapshot_digest,
        )
        with self._lock:
            self._prepared[spec.digest] = context
        return spec

    def revalidate(self, spec: LaunchSpec, context: TurnContext) -> bool:
        with self._lock:
            prepared = self._prepared.get(spec.digest)
        return (prepared == context and context.request_digest == self._prompt_digest
                and spec.snapshot_digest == self._snapshot_digest
                and self._snapshot_current() and self._owner_is_current())

    def _on_spawn(self, handle: object) -> None:
        with self._lock:
            self._live_handles[id(handle)] = handle

    def _on_reap(self, handle: object) -> None:
        with self._lock:
            self._live_handles.pop(id(handle), None)

    def launch(self, spec: LaunchSpec, token: LaunchToken) -> AdapterResult:
        if token.binding.launch_spec_digest != spec.digest:
            raise DeliberationError("launch token does not bind this launch spec")
        attempt_id = token.binding.attempt_id
        with self._lock:
            if attempt_id in self._launched:
                raise DuplicateAttemptError(
                    f"attempt already entered the executor: {attempt_id}")
            self._launched.add(attempt_id)

        def _before_launch(_evidence: Mapping[str, object]) -> None:
            # This callback runs inside the executor immediately before the
            # irreversible Popen/open operation, closing the prepare->spawn TOCTOU.
            # This is the final fence, after the scheduler's durable commit and
            # immediately before provider contact.  It closes the takeover
            # window between AttemptLedger.launch_once() and this callback.
            with self._lock:
                context = self._prepared.get(spec.digest)
            if (context is None or not self._owner_is_current()
                    or not self.revalidate(spec, context)):
                raise SnapshotDriftError(
                    "owner or participant snapshot changed at provider launch boundary")

        control = ProviderLaunchControl(
            before_launch=_before_launch,
            on_spawn=self._on_spawn,
            on_reap=self._on_reap,
            cancelled=self._cancelled,
            allow_secondary=False,
        )
        response = self._executor(
            self._invocation,
            timeout_ms=self._timeout_ms,
            debug_dir=self._debug_dir,
            launch_control=control,
        )
        raw_exit = response.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        timed_out = bool(response.get("timeout")) or exit_code == 124
        # Summon intentionally terminates some stream CLIs after their terminal
        # event.  Those two SIGTERM spellings are executor-owned success exits.
        transport_ok = not timed_out and exit_code in (0, 143, -15)
        prose = response.get("result") if isinstance(response.get("result"), str) else ""
        structured = self._parse_output(prose)
        evidence = ExecutionEvidence(
            transport_ok=transport_ok,
            exit_code=exit_code,
            timed_out=timed_out,
            parser_valid=structured is not None,
        )
        return AdapterResult(evidence=evidence, structured_output=structured,
                             model_prose=prose)

    def cleanup(self) -> CleanupReceipt:
        """Kill only still-registered process objects; never act on a bare PID."""
        with self._lock:
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
        return CleanupReceipt(verified=not retained,
                              retained_resources=tuple(retained))
