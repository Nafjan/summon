"""Single-attempt runtime bridge for approved fleet lanes.

The fleet compiler and approval store describe authority; this module is the
small bridge that spends exactly one approved attempt at the executor's real
provider boundary. It deliberately supports only the first proven slice:
foreground, single-candidate, pure subprocess routes with no secondary attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any

import _fleet
import _fleet_activation
import _fleet_approval
import _fleet_dispatch
import _loader
from _executor import ProviderLaunchControl


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _result_projection(result: dict) -> dict:
    """Return a bounded terminal identity without copying result text."""
    model = result.get("model") if isinstance(result.get("model"), dict) else {}
    text = result.get("result")
    return {
        "schema": "summon.fleet-terminal-projection/v1",
        "status": result.get("status"),
        "execution_status": result.get("execution_status"),
        "raw_backend_exit_code": result.get("raw_backend_exit_code"),
        "normalized_exit_code": result.get("normalized_exit_code"),
        "provider_contacted": result.get("provider_contacted"),
        "error_kind": result.get("error_kind"),
        "report_ok": result.get("report_ok"),
        "model": {
            "requested": model.get("requested"),
            "targeted": model.get("targeted"),
            "served": model.get("served"),
            "served_model_evidence": result.get("served_model_evidence"),
        },
        "result_sha256": hashlib.sha256(
            (text if isinstance(text, str) else "").encode(
                "utf-8", errors="replace")).hexdigest(),
    }


@dataclass
class FleetLaunchRuntime:
    """Own one reservation and its durable executor callbacks."""

    reservation: _fleet_dispatch.Reservation
    fleet: dict
    plan: dict
    catalog: list[dict]
    lane_name: str
    cwd: str
    data_boundary: dict
    invocation: Any
    activation_candidate: dict
    agent_definition_sha256: str
    request_identity_sha256: str
    fleet_path: str
    agents_dir: str
    strict_agents_dir: bool

    def _current_context(self) -> tuple[dict, dict, list[dict]]:
        fleet = _fleet.load_fleet(self.fleet_path)
        agents = _loader.list_agents(self.agents_dir)
        if self.strict_agents_dir:
            agents = [item for item in agents if item.get("source") == "project"]
        if not agents:
            raise _fleet_dispatch.FleetDispatchError(
                "fleet_dispatch_catalog_mismatch",
                "live roster contains no eligible declarative agent seats")
        plan, catalog = _fleet.compile_document(
            fleet=fleet, agents=agents, cwd=self.cwd)
        return fleet, plan, catalog

    def _claim(self, evidence: dict) -> None:
        fleet, plan, catalog = self._current_context()
        _fleet_dispatch.claim_activation_provider_launch(
            self.reservation,
            fleet=fleet,
            plan=plan,
            catalog=catalog,
            lane_name=self.lane_name,
            cwd=self.cwd,
            data_boundary=self.data_boundary,
            invocation=self.invocation,
            activation_candidate=self.activation_candidate,
            agent_definition_sha256=self.agent_definition_sha256,
            current_request_identity_sha256=self.request_identity_sha256,
            launch_evidence=evidence,
        )

    def _spawned(self, _handle: object) -> None:
        _fleet_dispatch.mark_spawned(self.reservation)

    def _reaped(self, _handle: object) -> None:
        _fleet_dispatch.mark_reaped(self.reservation)

    def _spawn_failed(self, _error: BaseException) -> None:
        _fleet_dispatch.mark_spawn_failed(self.reservation)

    def _indeterminate(self, _error: BaseException) -> None:
        claim = _fleet_dispatch.get_claim(self.reservation)
        phase = claim["phase"]
        if phase in {"provider_launch_claimed", "spawned", "reaped"}:
            _fleet_dispatch.mark_indeterminate(
                self.reservation,
                expected_phase=phase,
                provider_contacted=(True if phase in {"spawned", "reaped"} else None),
            )

    def control(self) -> ProviderLaunchControl:
        return ProviderLaunchControl(
            before_launch=self._claim,
            on_spawn=self._spawned,
            on_reap=self._reaped,
            on_pre_spawn_failure=self._spawn_failed,
            on_indeterminate=self._indeterminate,
            allow_secondary=False,
        )

    def finalize(self, result: dict) -> dict:
        """Reconcile one executor return and attach its public claim receipt."""
        claim = _fleet_dispatch.get_claim(self.reservation)
        phase = claim["phase"]
        if phase == "reserved":
            _fleet_dispatch.cancel_pre_spawn(self.reservation)
        elif phase == "spawned":
            # The executor normally reports reap before returning. A callback
            # interruption can leave the durable record one transition behind;
            # the returned child is already reaped, so reconcile exactly once.
            _fleet_dispatch.mark_reaped(self.reservation)
            phase = "reaped"
        if phase in {"reaped", "spawn_failed"}:
            _fleet_dispatch.mark_terminal(
                self.reservation,
                expected_phase=phase,
                terminal_sha256=_canonical_sha256(_result_projection(result)),
                provider_contacted=(phase == "reaped"),
            )
        elif phase == "provider_launch_claimed":
            _fleet_dispatch.mark_indeterminate(
                self.reservation, expected_phase=phase, provider_contacted=None)

        claim = _fleet_dispatch.get_claim(self.reservation)
        if claim["phase"] == "indeterminate":
            result.update({
                "status": "error",
                "execution_status": "error",
                "error_kind": "fleet_dispatch_indeterminate",
                "error": "fleet launch state is indeterminate; the result is not usable",
                "retryable": False,
                "result_usable": False,
                "provider_contacted": claim.get("provider_contacted"),
            })
        else:
            # The authenticated claim is authoritative when an exceptional
            # dispatcher path supplied stale or guessed contact evidence.
            result["provider_contacted"] = claim.get("provider_contacted")
        result["fleet_dispatch"] = _fleet_dispatch.public_receipt(self.reservation)
        return result

    def failure(self, error: BaseException) -> dict:
        """Return a fail-closed envelope even when durable reconciliation fails.

        Once a reservation exists, callers must never fall back to a generic
        pre-dispatch error that asserts zero provider contact.  This method first
        attempts the normal reconciliation path, then derives the strongest
        available contact/attempt state from the authenticated claim.  If the
        ledger itself cannot be read, contact remains explicitly indeterminate.
        """
        result = {
            "status": "error",
            "execution_status": "error",
            "result": "",
            "provider_contacted": None,
            "raw_backend_exit_code": None,
            "normalized_exit_code": 1,
            "exit_code": 1,
            "retryable": False,
            "result_usable": False,
            "error": str(error),
            "error_kind": getattr(error, "kind", "fleet_activation_failed"),
            "served_model_evidence": "absent",
            "model_match": None,
            "named_model_verified": False,
        }
        try:
            return self.finalize(result)
        except Exception as reconcile_error:  # preserve the original failure too
            result["error_kind"] = "fleet_dispatch_reconciliation_failed"
            result["reconciliation_error_kind"] = getattr(
                reconcile_error, "kind", type(reconcile_error).__name__)

        claim = None
        try:
            claim = _fleet_dispatch.get_claim(self.reservation)
            phase = claim["phase"]
            if phase in {"provider_launch_claimed", "spawned", "reaped"}:
                try:
                    _fleet_dispatch.mark_indeterminate(
                        self.reservation,
                        expected_phase=phase,
                        provider_contacted=(
                            True if phase in {"spawned", "reaped"} else None),
                    )
                    claim = _fleet_dispatch.get_claim(self.reservation)
                except Exception:
                    # The authenticated state we already read is still stronger
                    # than inventing a provider-free result.
                    pass
        except Exception:
            claim = None

        if claim is None:
            # A reservation existed, but neither launch nor non-launch can now
            # be authenticated.  Count the uncertain physical attempt instead
            # of falsely projecting a zero-attempt refusal.
            result.update({
                "attempts": 1,
                "attempt_status": "indeterminate",
                "provider_contacted": None,
            })
            return result

        phase = claim.get("phase")
        contacted = claim.get("provider_contacted")
        no_attempt = phase in {
            "reserved", "cancelled_pre_spawn", "spawn_failed",
        } and contacted is False
        result.update({
            "attempts": 0 if no_attempt else 1,
            "attempt_status": "not_run" if no_attempt else "indeterminate",
            "execution_status": "not_run" if no_attempt else "error",
            "provider_contacted": contacted,
        })
        try:
            result["fleet_dispatch"] = _fleet_dispatch.public_receipt(
                self.reservation)
        except Exception:
            pass
        return result


def _load_context(*, fleet_path: str, agents_dir: str, cwd: str,
                  strict_agents_dir: bool) -> tuple[dict, dict, list[dict]]:
    fleet = _fleet.load_fleet(fleet_path)
    agents = _loader.list_agents(agents_dir)
    if strict_agents_dir:
        agents = [item for item in agents if item.get("source") == "project"]
    if not agents:
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_dispatch_catalog_mismatch",
            "live roster contains no eligible declarative agent seats")
    plan, catalog = _fleet.compile_document(fleet=fleet, agents=agents, cwd=cwd)
    return fleet, plan, catalog


def preview(*, approval_id: str, fleet_path: str, agents_dir: str,
            strict_agents_dir: bool, lane_name: str, cwd: str,
            data_proof: str, invocation: Any,
            agent_definition_sha256: str,
            request_identity_sha256: str) -> dict:
    """Verify live-lane inputs without reserving capacity or contacting a provider."""
    fleet, plan, catalog = _load_context(
        fleet_path=fleet_path, agents_dir=agents_dir, cwd=cwd,
        strict_agents_dir=strict_agents_dir)
    del catalog
    payload, lane = _fleet_approval._lane(plan, lane_name)
    del payload
    if len(lane["candidates"]) != 1:
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_activation_lane_ambiguous",
            "live fleet dispatch currently requires exactly one lane candidate")
    seat = lane["candidates"][0]["seat"]
    bindings = _fleet_approval._bindings(fleet, plan, lane)
    authority = _fleet_approval._authority(lane)
    approval = _fleet_approval.inspect(approval_id)
    public_approval = approval["approval"]
    if (public_approval.get("state") != "active"
            or approval.get("authority") != authority
            or any(public_approval.get(key) != value
                   for key, value in bindings.items())):
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_dispatch_approval_mismatch",
            "fleet approval does not bind the current fleet, lane, project, and roster")
    activation_bindings = {
        "approval_id": approval_id,
        **{key: value for key, value in bindings.items() if key != "lane"},
        "prompt_sha256": _fleet_activation._sha(invocation.prompt),
        "request_identity_sha256": request_identity_sha256,
    }
    candidate = _fleet_activation.freeze_activation_candidate(
        bindings=activation_bindings,
        claim_id="0" * 32,
        seat=seat,
        agent_definition_sha256=agent_definition_sha256,
        invocation=invocation,
    )
    if not candidate.get("candidate_eligible"):
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_activation_billing_unverified",
            "live fleet dispatch requires a provider-inert billing classification")
    boundary = _fleet_dispatch._normalize_data_boundary(
        {"boundary": authority["data_boundary"], "proof": data_proof,
         "evidence_sha256": activation_bindings["prompt_sha256"]},
        declared=authority["data_boundary"],
        prompt_sha256=activation_bindings["prompt_sha256"])
    del boundary
    return {
        "status": "success",
        "authorization": "approved_lane",
        "provider_contacted": False,
        "approval": {
            "approval_id": approval_id,
            "generation": public_approval["generation"],
        },
        "lane": lane_name,
        "seat": seat,
        "activation_candidate": {
            "eligible": True,
            "binding_state": "preview_only_not_reserved",
        },
        "billing": {
            "class": candidate["billing"]["class"],
            "evidence": candidate["billing"]["evidence"],
        },
        "attempt_policy": dict(candidate["attempt_policy"]),
    }


def prepare(*, approval_id: str, fleet_path: str, agents_dir: str,
            strict_agents_dir: bool, lane_name: str, cwd: str,
            data_proof: str, invocation: Any,
            agent_definition_sha256: str,
            request_identity_sha256: str) -> FleetLaunchRuntime:
    """Freeze, reserve, and preflight one approved lane attempt provider-inertly."""
    fleet, plan, catalog = _load_context(
        fleet_path=fleet_path, agents_dir=agents_dir, cwd=cwd,
        strict_agents_dir=strict_agents_dir)
    payload, lane = _fleet_approval._lane(plan, lane_name)
    del payload
    if len(lane["candidates"]) != 1:
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_activation_lane_ambiguous",
            "live fleet dispatch currently requires exactly one lane candidate")
    seat = lane["candidates"][0]["seat"]
    approved_bindings = _fleet_approval._bindings(fleet, plan, lane)
    bindings = {
        "approval_id": approval_id,
        **{key: value for key, value in approved_bindings.items() if key != "lane"},
        "prompt_sha256": _fleet_activation._sha(invocation.prompt),
        "request_identity_sha256": request_identity_sha256,
    }
    claim_id = os.urandom(16).hex()
    candidate = _fleet_activation.freeze_activation_candidate(
        bindings=bindings,
        claim_id=claim_id,
        seat=seat,
        agent_definition_sha256=agent_definition_sha256,
        invocation=invocation,
    )
    if not candidate.get("candidate_eligible"):
        raise _fleet_dispatch.FleetDispatchError(
            "fleet_activation_billing_unverified",
            "live fleet dispatch requires a provider-inert billing classification")
    boundary = {
        "boundary": lane["constraints"]["data_boundary"],
        "proof": data_proof,
        "evidence_sha256": bindings["prompt_sha256"],
    }
    reservation = _fleet_dispatch.reserve_activation_dispatch(
        approval_id=approval_id,
        fleet=fleet,
        plan=plan,
        catalog=catalog,
        lane_name=lane_name,
        cwd=cwd,
        data_boundary=boundary,
        invocation=invocation,
        activation_candidate=candidate,
        agent_definition_sha256=agent_definition_sha256,
        current_request_identity_sha256=request_identity_sha256,
    )
    return FleetLaunchRuntime(
        reservation=reservation,
        fleet=fleet,
        plan=plan,
        catalog=catalog,
        lane_name=lane_name,
        cwd=cwd,
        data_boundary=boundary,
        invocation=invocation,
        activation_candidate=candidate,
        agent_definition_sha256=agent_definition_sha256,
        request_identity_sha256=request_identity_sha256,
        fleet_path=fleet_path,
        agents_dir=agents_dir,
        strict_agents_dir=bool(strict_agents_dir),
    )


__all__ = ["FleetLaunchRuntime", "prepare", "preview"]
