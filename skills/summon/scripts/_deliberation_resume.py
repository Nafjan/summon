"""Provider-inert reconciliation for durable deliberation prefixes.

This module owns exactly one run-directory lease per call and may append only
deterministic recovery transitions already justified by the immutable receipt
and validated journal.  It never reads the command inbox and has no scheduler,
executor, process, transport, or network surface.
"""

from __future__ import annotations

import os
import time
from typing import Mapping

import _deliberation_recovery as _recovery
import _deliberation_replay as _replay
import _deliberation_store as _store
import _rundir
from _deliberation import (DeliberationEngine, DeliberationError,
                           OwnershipLostError)


_TERMINAL = frozenset({
    "DECIDED", "UNRESOLVED", "REJECTED", "CANCELLED", "TIMED_OUT",
    "ATTEMPT_BUDGET_EXHAUSTED", "FAILED",
})


class _NoProviderAdapter:
    """Tripwire proving deterministic restoration cannot reach a provider."""

    @staticmethod
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("provider method reached during inert reconciliation")

    prepare = _forbidden
    revalidate = _forbidden
    launch = _forbidden
    cleanup = _forbidden


def _result(status: str, run_id: str, *, generation: int | None = None,
            checkpoint=None, error_kind: str | None = None,
            repaired: bool = False) -> dict:
    """Return one bounded receipt containing no path or provider material."""
    value = {
        "mode": "deliberation-resume-reconciliation",
        "schema_version": 1,
        "status": status,
        "run_id": run_id,
        "generation": generation,
        "provider_calls": 0,
        "journal_repaired": bool(repaired),
        "decision_status": None,
        "termination_reason": None,
        "decision_option": None,
        "checkpoint_digest": None,
    }
    if checkpoint is not None:
        value.update({
            "decision_status": checkpoint.status.lower(),
            "termination_reason": checkpoint.termination_reason,
            "decision_option": checkpoint.decision_option,
            "checkpoint_digest": checkpoint.digest,
        })
    if error_kind is not None:
        value["error_kind"] = error_kind
    return value


def _tagged_checkpoint(receipt: Mapping[str, object], run_dir: str,
                       current_generation: int):
    tagged, torn = _rundir.journal_read_tagged(run_dir)
    if torn:
        raise _replay.ReplayError("journal remains torn after repair")
    return tagged, _replay.replay_checkpoint(
        receipt, tagged, current_generation)


def reconcile_run(root: str, run_id: str, *, lease_sec: float = 600.0) -> dict:
    """Reconcile one deterministic crash prefix without contacting a provider."""
    owner = None
    repaired = False
    try:
        try:
            _rundir.validate_run_id(run_id)
        except (TypeError, ValueError):
            # Do not echo a path-like invalid identifier into a public/native
            # envelope. Valid run ids are safe bounded identifiers; rejected
            # input may be an absolute private path supplied by a caller.
            return _result("blocked", "<invalid>",
                           error_kind="invalid_run_id")
        if not isinstance(root, str) or not root:
            return _result("blocked", run_id, error_kind="invalid_root")
        try:
            path = _store.run_dir(root, run_id)
        except (TypeError, ValueError):
            return _result("blocked", run_id, error_kind="invalid_root")
        if not os.path.isdir(path):
            return _result("blocked", run_id, error_kind="unknown_run")
        if (isinstance(lease_sec, bool) or not isinstance(lease_sec, (int, float))
                or not 0 < float(lease_sec) < 86_400):
            return _result("blocked", run_id, error_kind="invalid_lease")

        # Recovery is an authority-mutating path. Authenticate current receipt
        # metadata before acquiring an owner or repairing/appending a journal.
        # Historical v1 context bindings are intentionally readable only by
        # status/replay and must fail here without changing the run directory.
        try:
            preflight_receipt = _store._receipt(path, run_id)
            _store._replay_policy(preflight_receipt)
            _replay._receipt_metadata(preflight_receipt)
        except (_store.DeliberationStoreError, _replay.ReplayError):
            return _result("blocked", run_id, error_kind="receipt_invalid")

        owner = _rundir.acquire_owner(path, float(lease_sec))
        # Repair must precede every journal read in this coordinator.  The
        # existing helper repairs only the newest eligible torn tail and
        # fences its audit record to this exact owner generation.
        repaired = _rundir.journal_repair(path, owner)
        receipt = _store._receipt(path, run_id)
        policy = _store._replay_policy(receipt)
        tagged, checkpoint = _tagged_checkpoint(
            receipt, path, owner.generation + 1)

        if checkpoint.uncertain_spend:
            return _result("blocked", run_id, generation=owner.generation,
                           checkpoint=checkpoint,
                           error_kind="uncertain_spend", repaired=repaired)
        if checkpoint.status in _TERMINAL:
            return _result("already_terminal", run_id,
                           generation=owner.generation,
                           checkpoint=checkpoint, repaired=repaired)
        if checkpoint.pending_commands and checkpoint.pending_command_batch is None:
            return _result("blocked", run_id, generation=owner.generation,
                           checkpoint=checkpoint,
                           error_kind="legacy_unsealed_commands", repaired=repaired)

        if checkpoint.pending_command_batch is not None:
            recovered = _recovery.recover_pending_human_commands(
                owner=owner, receipt=receipt, policy=policy)
            if recovered.status == "ownership_lost":
                return _result("blocked", run_id, generation=owner.generation,
                               checkpoint=checkpoint,
                               error_kind="ownership_lost", repaired=repaired)
            _tagged, reconciled = _tagged_checkpoint(
                receipt, path, owner.generation + 1)
            status = ("already_recovered" if recovered.status == "already_recovered"
                      else "recovered")
            return _result(status, run_id, generation=owner.generation,
                           checkpoint=reconciled, repaired=repaired)

        if checkpoint.status == "WAITING_HUMAN":
            return _result("waiting_human", run_id,
                           generation=owner.generation,
                           checkpoint=checkpoint, repaired=repaired)

        if (checkpoint.status == "RUNNING" and
                checkpoint.candidate_option is not None and
                checkpoint.pending_turn is None):
            # Public restore independently binds the checkpoint to this owner,
            # receipt, and predecessor journal.  Pass the predecessor-only
            # seal it expects; a current journal_repaired audit remains inert.
            prior = [(generation, record) for generation, record in tagged
                     if generation < owner.generation]
            prior_checkpoint = _replay.replay_checkpoint(
                receipt, prior, owner.generation)
            DeliberationEngine.restore(
                prior_checkpoint, policy, _NoProviderAdapter(), owner=owner,
                run_dir=path, deadline=0.0, clock=time.monotonic,
                receipt=receipt)
            _tagged, reconciled = _tagged_checkpoint(
                receipt, path, owner.generation + 1)
            if reconciled.status == "DECIDED":
                status = "reconciled_terminal"
            elif reconciled.status == "WAITING_HUMAN":
                status = "waiting_human"
            else:
                return _result("blocked", run_id,
                               generation=owner.generation,
                               checkpoint=reconciled,
                               error_kind="reconciliation_incomplete",
                               repaired=repaired)
            return _result(status, run_id, generation=owner.generation,
                           checkpoint=reconciled, repaired=repaired)

        error_kind = ("pending_turn" if checkpoint.pending_turn is not None
                      else "no_deterministic_recovery")
        return _result("blocked", run_id, generation=owner.generation,
                       checkpoint=checkpoint, error_kind=error_kind,
                       repaired=repaired)
    except _rundir.OwnerHeldError:
        return _result("blocked", run_id, error_kind="owner_held")
    except _rundir.OwnerLockForeignError:
        return _result("blocked", run_id, error_kind="owner_lock_invalid")
    except (OwnershipLostError, _rundir.OwnershipLostError):
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind="ownership_lost", repaired=repaired)
    except _recovery.RecoveryError as exc:
        kind = ("legacy_unsealed_commands" if "legacy unsealed" in str(exc)
                else "recovery_invalid")
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind=kind, repaired=repaired)
    except (_rundir.JournalCorruptError, _replay.ReplayError):
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind="journal_invalid", repaired=repaired)
    except _store.DeliberationStoreError:
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind="receipt_invalid", repaired=repaired)
    except DeliberationError:
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind="restore_invalid", repaired=repaired)
    except OSError:
        return _result("blocked", run_id,
                       generation=None if owner is None else owner.generation,
                       error_kind="io_error", repaired=repaired)
    finally:
        if owner is not None:
            _rundir.release_owner(owner)
