"""Authenticated, provider-inert fleet dispatch reservations.

This module owns no process, provider, network, or credential-launch capability.  It
turns one active M3b1 fleet approval into bounded, locally durable, single-use launch
claims. The caller must still connect the claim transitions to
``ProviderLaunchControl``.

Public receipts are evidence only. Authority comes from reopening the private
approval store, dispatch ledger, separate monotonic anchor, and authenticated
initialization marker under the shared store lock. Partial deletion and torn writes
fail closed while at least one dispatch-history artifact survives. As in
``_fleet_approval``, the local OS account is the trust boundary: this does not defend
against that owner deliberately deleting every authenticated dispatch-history
artifact, or rolling back or deleting its key or approval state.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import _decision
import _evidence
import _fleet
import _fleet_activation
import _fleet_approval
import _fleet_compile
import _jobs
from _backend_policy import (
    PERMISSION_ORDER, effective_permission, permission_enforcement,
)


LEDGER_SCHEMA = "summon.fleet-dispatch-ledger/v1"
ANCHOR_SCHEMA = "summon.fleet-dispatch-anchor/v1"
CLAIM_SCHEMA = "summon.fleet-dispatch-claim/v1"
PUBLIC_SCHEMA = "summon.fleet-dispatch/v1"
ACTIVATION_BINDING_SCHEMA = "summon.fleet-activation-binding/v1"
LAUNCH_EVIDENCE_SCHEMA = "summon.fleet-launch-evidence/v1"
INITIALIZATION_SCHEMA = "summon.fleet-dispatch-initialization/v1"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_ANCHOR_BYTES = 4096
MAX_ANCHOR_PARSE_ITEMS = 64
MAX_INITIALIZATION_BYTES = 2048
MAX_PUBLIC_RECEIPT_BYTES = 64 * 1024
MAX_CLAIMS = 4096
MAX_PARSE_ITEMS = 180_000
MAX_GENERATION = (1 << 63) - 1

PHASES = frozenset({
    "reserved", "cancelled_pre_spawn", "provider_launch_claimed",
    "spawn_failed", "spawned", "reaped", "indeterminate", "terminal",
})
ACTIVE_PHASES = frozenset({
    "reserved", "provider_launch_claimed", "spawned", "reaped", "indeterminate",
})
BILLING_CLASSES = frozenset({"none", "subscription", "credit", "payg"})
DATA_PROOFS = {
    "public": "public_prompt_verified",
    "local_sanitized": "operator_attested",
}

_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LANE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}$")
_PUBLIC_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SECRET = re.compile(
    r"(?i)(?:sk-(?:or-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|token|secret)[=:][^\s]{8,})")

_LAUNCH_EVIDENCE_FIELDS = frozenset({
    "schema", "backend", "transport", "command_sha256", "argv_sha256",
    "cwd_sha256", "env_names_sha256", "env_sha256",
})


class FleetDispatchError(_evidence.EvidenceError):
    """Typed fail-closed dispatch-ledger error."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class Reservation:
    """In-process convenience handle; the authenticated ledger remains authority."""

    approval_id: str
    claim_id: str
    request_id: str
    request_sha256: str
    decision: dict
    route: dict


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unsigned(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "mac"}


def _ledger_key(master: bytes, store_id: str, approval_id: str) -> bytes:
    domain = (
        b"summon.fleet-dispatch-ledger/v1\0"
        + store_id.encode("ascii") + b"\0" + approval_id.encode("ascii")
    )
    return hmac.new(master, domain, hashlib.sha256).digest()


def _anchor_key(master: bytes, store_id: str, approval_id: str) -> bytes:
    domain = (
        b"summon.fleet-dispatch-anchor/v1\0"
        + store_id.encode("ascii") + b"\0" + approval_id.encode("ascii")
    )
    return hmac.new(master, domain, hashlib.sha256).digest()


def _initialization_key(master: bytes, store_id: str, approval_id: str) -> bytes:
    domain = (
        b"summon.fleet-dispatch-initialization/v1\0"
        + store_id.encode("ascii") + b"\0" + approval_id.encode("ascii")
    )
    return hmac.new(master, domain, hashlib.sha256).digest()


def _mac(key: bytes, value: dict) -> str:
    return hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def ledger_root() -> str:
    """Private sibling directory; it never appears in public projections."""
    return os.path.join(
        os.path.dirname(os.path.abspath(_fleet_approval.store_path())),
        "fleet-dispatch",
    )


def ledger_path(approval_id: str) -> str:
    if not _SHA256.fullmatch(str(approval_id or "")):
        raise FleetDispatchError(
            "fleet_dispatch_approval_invalid", "fleet dispatch approval id is malformed")
    return os.path.join(ledger_root(), approval_id + ".json")


def anchor_root() -> str:
    """Separate private root so ledger-directory loss cannot erase its anchor."""
    return os.path.join(
        os.path.dirname(os.path.abspath(_fleet_approval.store_path())),
        "fleet-dispatch-anchors",
    )


def anchor_path(approval_id: str) -> str:
    if not _SHA256.fullmatch(str(approval_id or "")):
        raise FleetDispatchError(
            "fleet_dispatch_approval_invalid", "fleet dispatch approval id is malformed")
    return os.path.join(anchor_root(), approval_id + ".json")


def initialization_root() -> str:
    """Approval-store companion root, independent of both dispatch roots."""
    return os.path.join(
        os.path.dirname(os.path.abspath(_fleet_approval.store_path())),
        "fleet-dispatch-initialized",
    )


def initialization_path(approval_id: str) -> str:
    if not _SHA256.fullmatch(str(approval_id or "")):
        raise FleetDispatchError(
            "fleet_dispatch_approval_invalid", "fleet dispatch approval id is malformed")
    return os.path.join(initialization_root(), approval_id + ".json")


def _public_identifier(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise FleetDispatchError(
            "fleet_dispatch_request_invalid", f"{field} must be a bounded identifier")
    return value


def _public_text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not _PUBLIC_TEXT.fullmatch(value)
            or value.startswith(("/", "\\\\", "file:"))
            or re.match(r"^[A-Za-z]:[\\/]", value)
            or ".." in value or _SECRET.search(value)):
        raise FleetDispatchError(
            "fleet_dispatch_request_invalid", f"{field} must be bounded public text")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FleetDispatchError(
            "fleet_dispatch_request_invalid", f"{field} must be a SHA-256 digest")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", f"{field} timestamp is malformed")
    try:
        parsed = _dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", f"{field} timestamp is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != _dt.timedelta(0):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", f"{field} timestamp is malformed")
    canonical = parsed.astimezone(_dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", f"{field} timestamp is not canonical")
    return value


def _now_text() -> str:
    return _fleet_approval._timestamp(_fleet_approval._now())


def _normalize_data_boundary(value: Any, *, declared: str,
                             prompt_sha256: str) -> dict:
    if declared == "unspecified":
        raise FleetDispatchError(
            "fleet_dispatch_data_boundary_unsatisfied",
            "fleet dispatch refuses an unspecified data boundary")
    if declared == "private_local":
        raise FleetDispatchError(
            "fleet_dispatch_data_boundary_unsatisfied",
            "private-local dispatch requires a future authenticated non-egress backend proof")
    expected = DATA_PROOFS.get(declared)
    if (not isinstance(value, dict)
            or set(value) != {"boundary", "proof", "evidence_sha256"}
            or value.get("boundary") != declared
            or value.get("proof") != expected
            or not _SHA256.fullmatch(str(value.get("evidence_sha256", "")))):
        raise FleetDispatchError(
            "fleet_dispatch_data_boundary_unsatisfied",
            "fleet dispatch data-boundary evidence is incomplete or mismatched")
    # Neither a public scan nor an operator attestation may be replayed onto a
    # different prompt. Private-local is refused above until non-egress proof exists.
    if value["evidence_sha256"] != prompt_sha256:
        raise FleetDispatchError(
            "fleet_dispatch_data_boundary_unsatisfied",
            "data-boundary evidence must bind the exact prompt")
    return dict(value)


def _normalize_billing(value: Any, seats: set[str]) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != seats
            or not all(isinstance(seat, str) and billing in BILLING_CLASSES
                       for seat, billing in value.items())):
        raise FleetDispatchError(
            "fleet_dispatch_billing_invalid",
            "fleet dispatch requires one typed billing class for every lane seat")
    return {seat: value[seat] for seat in sorted(value)}


def _active_approval(approval_id: str, key: bytes | None,
                     now: _dt.datetime) -> tuple[dict, dict]:
    if key is None:
        raise FleetDispatchError(
            "fleet_dispatch_approval_unavailable", "fleet approval key is unavailable")
    store = _fleet_approval._read_store(key, now)
    if store is None or approval_id not in store["approvals"]:
        raise FleetDispatchError(
            "fleet_dispatch_approval_unavailable", "fleet approval does not exist")
    now = _fleet_approval._monotonic_now(store, now)
    state = _fleet_approval._state(approval_id, store, now)
    if state != "active":
        raise FleetDispatchError(
            "fleet_dispatch_approval_inactive",
            f"fleet approval is {state}; no provider launch may be claimed")
    approval = store["approvals"][approval_id]
    if approval.get("operation") != "fleet_dispatch":
        raise FleetDispatchError(
            "fleet_dispatch_approval_mismatch", "fleet approval operation is unsupported")
    return store, approval


def _approved_context(*, fleet: dict, plan: dict, catalog: list[dict],
                      lane_name: str, cwd: str,
                      approval: dict) -> tuple[dict, dict, list[dict]]:
    _payload, lane = _fleet_approval._lane(plan, lane_name)
    bindings = _fleet_approval._bindings(fleet, plan, lane)
    authority = _fleet_approval._authority(lane)
    normalized_catalog = _fleet_compile.validate_catalog(catalog)
    if _fleet_compile.catalog_digest(normalized_catalog) != bindings["catalog_sha256"]:
        raise FleetDispatchError(
            "fleet_dispatch_catalog_mismatch",
            "current fleet catalog does not match the approved plan")
    if _fleet.project_digest(cwd) != bindings["project_sha256"]:
        raise FleetDispatchError(
            "fleet_dispatch_project_mismatch",
            "current project root does not match the approved plan")
    if approval.get("bindings") != bindings or approval.get("authority") != authority:
        raise FleetDispatchError(
            "fleet_dispatch_approval_mismatch",
            "fleet approval does not bind the current fleet, plan, project, catalog, lane, and authority")
    return bindings, authority, normalized_catalog


def _decision_and_route(*, approval_id: str, bindings: dict, authority: dict,
                        lane: dict, catalog: list[dict], billing: dict[str, str],
                        data_boundary: dict) -> tuple[dict, dict]:
    by_seat = {entry["seat"]: entry for entry in catalog}
    requested_seats = {item["seat"] for item in lane["candidates"]}
    if set(by_seat).issuperset(requested_seats) is False:
        raise FleetDispatchError(
            "fleet_dispatch_catalog_mismatch", "approved lane references an unavailable seat")
    if set(billing) != requested_seats:
        raise FleetDispatchError(
            "fleet_dispatch_billing_invalid", "billing projection does not match the lane")
    constraints = lane["constraints"]
    provider_allowlist = set(constraints["provider_allowlist"])
    model_allowlist = set(constraints["model_allowlist"])
    required_capabilities = set(constraints["required_capabilities"])
    candidates = []
    for requested in lane["candidates"]:
        entry = by_seat[requested["seat"]]
        billing_class = billing[entry["seat"]]
        reasons = []
        if provider_allowlist and entry["provider"] not in provider_allowlist:
            reasons.append("provider_not_allowed")
        if entry["provider_evidence"] == "unknown":
            reasons.append("provider_identity_unknown")
        if model_allowlist and entry["model"] not in model_allowlist:
            reasons.append("model_not_allowed")
        if not required_capabilities.issubset(entry["capabilities"]):
            reasons.append("capability_missing")
        if PERMISSION_ORDER[entry["permission"]] > PERMISSION_ORDER[
                authority["permission_ceiling"]]:
            reasons.append("permission_ceiling_exceeded")
        derived_effective = effective_permission(entry["backend"], entry["permission"])
        derived_enforcement = permission_enforcement(
            entry["backend"], entry["permission"])
        if (entry["effective_permission"] != derived_effective
                or entry["enforcement"] != derived_enforcement):
            reasons.append("authority_projection_mismatch")
        if (derived_effective not in PERMISSION_ORDER
                or PERMISSION_ORDER[derived_effective]
                > PERMISSION_ORDER[authority["permission_ceiling"]]):
            reasons.append("effective_permission_ceiling_exceeded")
        if derived_enforcement != "enforced":
            reasons.append("permission_not_enforced")
        if entry["requires_spend"] != (billing_class != "none"):
            reasons.append("billing_projection_mismatch")
        elif billing_class != "none" and not authority["spend"][billing_class]:
            reasons.append("billing_class_not_authorized")
        candidates.append({
            "seat": entry["seat"], "backend": entry["backend"],
            "provider": entry["provider"], "model": entry["model"],
            "permission": entry["permission"], "priority": requested["priority"],
            "eligible": not reasons, "reasons": sorted(set(reasons)),
            "requires_spend": entry["requires_spend"], "gate_allowed": True,
            "data_boundary_satisfied": True, "requires_corrective": False,
            "requires_retry": False, "requires_fallback": False,
            "freshness": "unknown",
        })
    decision = _decision.decide(
        request={"agent": None, "lane": lane["name"], "model": None,
                 "source": "approved_lane"},
        candidates=candidates,
        constraints={
            "permission_ceiling": authority["permission_ceiling"],
            # Typed per-candidate billing reasons are authoritative here.  This
            # boolean prevents the older kernel from flattening the approved class.
            "spend_authorized": any(
                authority["spend"][field]
                for field in ("subscription", "credit", "payg")),
            "enforcement": "enforced", "unenforceable_authorized": False,
            "unknowns": [], "corrective_allowed": False,
            "retry_allowed": False, "fallback_allowed": False,
            "require_fresh": False,
        },
        evidence={"roster": bindings["catalog_sha256"],
                  "policy": bindings["lane_sha256"],
                  "project": bindings["project_sha256"],
                  "approval": approval_id},
    )
    payload = _evidence.verify(decision)
    seat = payload["resolution"]["seat"]
    if seat is None:
        raise FleetDispatchError(
            "fleet_dispatch_no_eligible_route",
            "approved lane has no eligible route under current authority")
    entry = by_seat[seat]
    route = {
        "seat": entry["seat"], "backend": entry["backend"],
        "provider": entry["provider"],
        "provider_evidence": entry["provider_evidence"],
        "model": entry["model"], "permission": entry["permission"],
        "effective_permission": entry["effective_permission"],
        "enforcement": entry["enforcement"],
        "billing_class": billing[seat],
    }
    if (payload["resolution"]["backend"] != route["backend"]
            or payload["resolution"]["provider"] != route["provider"]
            or payload["resolution"]["model_targeted"] != route["model"]):
        raise FleetDispatchError(
            "fleet_dispatch_decision_mismatch", "decision route differs from the catalog")
    del data_boundary  # its exact evidence is bound by the semantic request below
    return decision, route


def _semantic_request(*, bindings: dict, authority: dict, decision: dict,
                      prompt_sha256: str, billing: dict[str, str],
                      data_boundary: dict, route: dict) -> dict:
    return {
        "operation": "fleet_dispatch",
        "bindings": bindings,
        "authority": authority,
        "decision_sha256": decision["sha256"],
        "prompt_sha256": prompt_sha256,
        "billing": billing,
        "data_boundary": data_boundary,
        "route": route,
    }


def _validate_route(value: Any) -> dict:
    fields = {"seat", "backend", "provider", "provider_evidence", "model",
              "permission", "effective_permission", "enforcement", "billing_class"}
    if not isinstance(value, dict) or set(value) != fields:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch route is malformed")
    for field in ("seat", "backend", "provider"):
        _public_identifier(value.get(field), f"route.{field}")
    _public_text(value.get("model"), "route.model", nullable=True)
    if value.get("provider_evidence") not in {
            "backend_static", "named_registry", "model_selector_verified", "unknown"}:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch provider evidence is invalid")
    if value.get("permission") not in PERMISSION_ORDER:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch permission is invalid")
    if value.get("effective_permission") not in {
            "read-only", "safe-edit", "yolo", "unenforceable"}:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "effective permission is invalid")
    if value.get("enforcement") not in {"enforced", "unenforceable", "unknown"}:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "permission enforcement is invalid")
    if value.get("billing_class") not in BILLING_CLASSES:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch billing class is invalid")
    return value


def _validate_claim(claim: Any, ledger: dict) -> None:
    fields = {
        "schema", "ordinal", "claim_generation", "claim_id", "request_id",
        "request_sha256", "request", "decision", "route", "phase",
        "contact_slot_reserved", "billable_slot_reserved",
        "contact_slot_consumed", "billable_slot_consumed",
        "provider_contacted", "terminal_sha256", "created_at", "updated_at",
    }
    if (not isinstance(claim, dict) or set(claim) not in {frozenset(fields), frozenset(fields | {"activation"})}
            or claim.get("schema") != CLAIM_SCHEMA
            or not isinstance(claim.get("ordinal"), int)
            or isinstance(claim.get("ordinal"), bool)
            or not 1 <= claim["ordinal"] <= MAX_CLAIMS
            or not isinstance(claim.get("claim_generation"), int)
            or isinstance(claim.get("claim_generation"), bool)
            or not 1 <= claim["claim_generation"] <= MAX_GENERATION
            or not _ID.fullmatch(str(claim.get("claim_id", "")))
            or not _ID.fullmatch(str(claim.get("request_id", "")))
            or not _SHA256.fullmatch(str(claim.get("request_sha256", "")))
            or claim.get("phase") not in PHASES
            or not all(isinstance(claim.get(field), bool) for field in (
                "contact_slot_reserved", "billable_slot_reserved",
                "contact_slot_consumed", "billable_slot_consumed"))
            or claim.get("provider_contacted") not in {None, False, True}
            or (claim.get("terminal_sha256") is not None
                and not _SHA256.fullmatch(str(claim["terminal_sha256"])))):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch claim is malformed")
    _timestamp(claim.get("created_at"), "created_at")
    _timestamp(claim.get("updated_at"), "updated_at")
    request = claim.get("request")
    request_fields = {
            "operation", "bindings", "authority", "decision_sha256",
            "prompt_sha256", "billing", "data_boundary", "route"}
    activation = claim.get("activation")
    if activation is not None:
        request_fields.add("activation_sha256")
    if (not isinstance(request, dict) or set(request) != request_fields
            or request.get("operation") not in {"fleet_dispatch", "fleet_activation_dispatch"}
            or (activation is None) != (request.get("operation") == "fleet_dispatch")
            or request.get("bindings") != ledger["bindings"]
            or request.get("authority") != ledger["authority"]
            or not _SHA256.fullmatch(str(request.get("decision_sha256", "")))
            or not _SHA256.fullmatch(str(request.get("prompt_sha256", "")))
            or not isinstance(request.get("billing"), dict)
            or request.get("route") != claim.get("route")
            or claim["request_sha256"] != _digest(request)):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch request binding is invalid")
    if activation is not None:
        activation_fields = {
            "schema", "contract_sha256", "agent_definition_sha256",
            "request_identity_sha256", "private_binding_hmac", "billing_class",
            "attempt_policy",
        }
        final_launch_sha256 = activation.get("final_launch_sha256") if isinstance(
            activation, dict) else None
        activation_field_sets = {
            frozenset(activation_fields),
            frozenset(activation_fields | {"final_launch_sha256"}),
        }
        if (not isinstance(activation, dict) or set(activation) not in activation_field_sets
                or activation.get("schema") != ACTIVATION_BINDING_SCHEMA
                or not all(_SHA256.fullmatch(str(activation.get(field, "")))
                           for field in ("contract_sha256", "agent_definition_sha256",
                                         "request_identity_sha256", "private_binding_hmac"))
                or ("final_launch_sha256" in activation
                    and not _SHA256.fullmatch(str(final_launch_sha256)))
                or activation.get("billing_class") not in BILLING_CLASSES
                or activation.get("attempt_policy") != {
                    "foreground_subprocess_only": True, "max_attempts": 1,
                    "retry": False, "fallback": False,
                    "continuation": False, "background": False,
                }
                or request.get("activation_sha256") != activation["contract_sha256"]):
            raise FleetDispatchError(
                "fleet_dispatch_ledger_untrusted", "activation binding is malformed")
    data_boundary = request.get("data_boundary")
    _normalize_data_boundary(
        data_boundary, declared=ledger["authority"]["data_boundary"],
        prompt_sha256=request["prompt_sha256"])
    billing = request["billing"]
    if (not all(isinstance(key, str) and _PUBLIC_ID.fullmatch(key)
                and value in BILLING_CLASSES for key, value in billing.items())):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch billing binding is invalid")
    route = _validate_route(claim.get("route"))
    if (route["effective_permission"] != effective_permission(
            route["backend"], route["permission"])
            or route["enforcement"] != permission_enforcement(
                route["backend"], route["permission"])
            or route["effective_permission"] not in PERMISSION_ORDER
            or PERMISSION_ORDER[route["effective_permission"]]
            > PERMISSION_ORDER[ledger["authority"]["permission_ceiling"]]):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted",
            "dispatch route authority differs from backend policy")
    if billing.get(route["seat"]) != route["billing_class"]:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "selected billing class is inconsistent")
    decision = claim.get("decision")
    payload = _evidence.verify(decision)
    resolution = payload["resolution"]
    if (decision.get("sha256") != request["decision_sha256"]
            or resolution.get("seat") != route["seat"]
            or resolution.get("backend") != route["backend"]
            or resolution.get("provider") != route["provider"]
            or resolution.get("model_targeted") != route["model"]
            or payload["digests"] != {
                "roster": ledger["bindings"]["catalog_sha256"],
                "policy": ledger["bindings"]["lane_sha256"],
                "project": ledger["bindings"]["project_sha256"],
                "approval": ledger["approval_id"],
            }):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch decision binding is invalid")
    billable = route["billing_class"] in {"credit", "payg"}
    phase = claim["phase"]
    expected = {
        "reserved": (True, billable, False, False, False, False),
        "cancelled_pre_spawn": (False, False, False, False, False, False),
        "provider_launch_claimed": (False, False, True, billable, None, False),
        "spawn_failed": (False, False, True, billable, False, False),
        "spawned": (False, False, True, billable, True, False),
        "reaped": (False, False, True, billable, True, False),
    }
    actual = (
        claim["contact_slot_reserved"], claim["billable_slot_reserved"],
        claim["contact_slot_consumed"], claim["billable_slot_consumed"],
        claim["provider_contacted"], claim["terminal_sha256"] is not None,
    )
    if phase in expected and actual != expected[phase]:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch claim phase contradicts its counters")
    if phase == "indeterminate" and (
            actual[:4] != (False, False, True, billable)
            or claim["provider_contacted"] not in {None, True}
            or claim["terminal_sha256"] is not None):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "indeterminate claim is incoherent")
    if phase == "terminal" and (
            actual[:4] != (False, False, True, billable)
            or not isinstance(claim["provider_contacted"], bool)
            or claim["terminal_sha256"] is None):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "terminal claim is incoherent")
    if activation is not None:
        launch_committed = "final_launch_sha256" in activation
        if ((phase in {"reserved", "cancelled_pre_spawn"} and launch_committed)
                or (phase in {
                    "provider_launch_claimed", "spawn_failed", "spawned", "reaped",
                    "indeterminate", "terminal"} and not launch_committed)):
            raise FleetDispatchError(
                "fleet_dispatch_ledger_untrusted",
                "activation final-launch binding contradicts its claim phase")


def _validate_ledger(value: Any, key: bytes, approval_id: str) -> dict:
    fields = {"schema", "store_id", "approval_id", "approval_generation",
              "approval_sha256", "bindings", "authority", "generation",
              "next_ordinal", "totals", "claims", "mac"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("schema") != LEDGER_SCHEMA
            or value.get("approval_id") != approval_id
            or not _SHA256.fullmatch(str(value.get("store_id", "")))
            or not _SHA256.fullmatch(str(value.get("approval_sha256", "")))
            or not isinstance(value.get("approval_generation"), int)
            or isinstance(value.get("approval_generation"), bool)
            or not 1 <= value["approval_generation"] <= MAX_GENERATION
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or not 0 <= value["generation"] <= MAX_GENERATION
            or not isinstance(value.get("next_ordinal"), int)
            or isinstance(value.get("next_ordinal"), bool)
            or not 0 <= value["next_ordinal"] <= MAX_CLAIMS
            or not isinstance(value.get("claims"), dict)
            or len(value["claims"]) > MAX_CLAIMS
            or not isinstance(value.get("totals"), dict)
            or set(value["totals"]) != {
                "provider_contact_slots_consumed", "billable_slots_consumed"}
            or not all(isinstance(value["totals"].get(field), int)
                       and not isinstance(value["totals"].get(field), bool)
                       and value["totals"][field] >= 0 for field in value["totals"])):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch ledger shape is malformed")
    ledger_key = _ledger_key(key, value["store_id"], approval_id)
    if not hmac.compare_digest(str(value.get("mac", "")), _mac(ledger_key, _unsigned(value))):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch ledger authentication failed")
    try:
        authority = _fleet_approval._validate_authority(value.get("authority"))
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch authority is malformed") from exc
    bindings = value.get("bindings")
    if (not isinstance(bindings, dict) or set(bindings) != {
            "fleet_sha256", "plan_sha256", "project_sha256", "catalog_sha256",
            "lane", "lane_sha256"}):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch bindings are malformed")
    for field in ("fleet_sha256", "plan_sha256", "project_sha256",
                  "catalog_sha256", "lane_sha256"):
        _sha(bindings.get(field), f"bindings.{field}")
    _public_identifier(bindings.get("lane"), "bindings.lane")
    if authority != value["authority"]:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch authority is noncanonical")
    ordinals = []
    contacts = billable = reservations = billable_reservations = active = 0
    for claim_id, claim in value["claims"].items():
        if claim_id != str((claim or {}).get("claim_id", "")):
            raise FleetDispatchError(
                "fleet_dispatch_ledger_untrusted", "dispatch claim index is malformed")
        _validate_claim(claim, value)
        ordinals.append(claim["ordinal"])
        contacts += int(claim["contact_slot_consumed"])
        billable += int(claim["billable_slot_consumed"])
        reservations += int(claim["contact_slot_reserved"])
        billable_reservations += int(claim["billable_slot_reserved"])
        active += int(claim["phase"] in ACTIVE_PHASES)
    if (len(ordinals) != len(set(ordinals))
            or (ordinals and max(ordinals) > value["next_ordinal"])
            or value["next_ordinal"] < len(ordinals)
            or contacts != value["totals"]["provider_contact_slots_consumed"]
            or billable != value["totals"]["billable_slots_consumed"]):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch ledger totals are inconsistent")
    spend = authority["spend"]
    if (contacts + reservations > spend["max_provider_contacts"]
            or billable + billable_reservations > spend["max_billable_attempts"]
            or active > spend["max_parallel"]):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", "dispatch ledger exceeds approved ceilings")
    return value


def _ledger_retired(value: dict) -> bool:
    claims = value.get("claims", {})
    return bool(claims) and all(
        claim.get("phase") in {"cancelled_pre_spawn", "terminal"}
        for claim in claims.values())


def _validate_anchor(value: Any, key: bytes, store: dict,
                     approval_id: str) -> dict:
    fields = {
        "schema", "store_id", "approval_id", "approval_generation",
        "generation", "state", "ledger_sha256", "pending_generation",
        "pending_ledger_sha256", "retired", "pending_retired",
        "updated_at", "mac",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("schema") != ANCHOR_SCHEMA
            or value.get("store_id") != store.get("store_id")
            or value.get("approval_id") != approval_id
            or not isinstance(value.get("approval_generation"), int)
            or isinstance(value.get("approval_generation"), bool)
            or not 1 <= value["approval_generation"] <= MAX_GENERATION
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or not 0 <= value["generation"] <= MAX_GENERATION
            or value.get("state") not in {"clean", "prepared"}
            or (value.get("ledger_sha256") is not None
                and not _SHA256.fullmatch(str(value["ledger_sha256"])))
            or not isinstance(value.get("retired"), bool)):
        raise _evidence.EvidenceError("fleet dispatch anchor is malformed")
    approval = store.get("approvals", {}).get(approval_id)
    if (approval is not None
            and value["approval_generation"] != approval.get("generation")):
        raise _evidence.EvidenceError(
            "fleet dispatch anchor approval generation is inconsistent")
    _timestamp(value.get("updated_at"), "updated_at")
    if value["state"] == "clean":
        if (value.get("pending_generation") is not None
                or value.get("pending_ledger_sha256") is not None
                or value.get("pending_retired") is not None):
            raise _evidence.EvidenceError("clean fleet dispatch anchor has pending state")
    elif (not isinstance(value.get("pending_generation"), int)
          or isinstance(value.get("pending_generation"), bool)
          or value["generation"] >= MAX_GENERATION
          or value["pending_generation"] != value["generation"] + 1
          or not _SHA256.fullmatch(str(value.get("pending_ledger_sha256", "")))
          or not isinstance(value.get("pending_retired"), bool)):
        raise _evidence.EvidenceError("prepared fleet dispatch anchor is malformed")
    expected = _mac(
        _anchor_key(key, value["store_id"], approval_id), _unsigned(value))
    if not hmac.compare_digest(str(value.get("mac", "")), expected):
        raise _evidence.EvidenceError("fleet dispatch anchor authentication failed")
    return value


def _read_anchor(key: bytes, store: dict, approval_id: str, *,
                 missing_ok: bool) -> dict | None:
    path = anchor_path(approval_id)
    root = os.path.dirname(path)
    if not os.path.lexists(root):
        if missing_ok:
            return None
        raise _evidence.EvidenceError("fleet dispatch anchor is missing")
    _fleet_approval._reject_reparse_ancestors(root)
    _fleet_approval._verify_private(root, directory=True)
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise _evidence.EvidenceError("fleet dispatch anchor is missing")
    identity = _fleet_approval._regular_single_link(path, "dispatch anchor")
    _fleet_approval._verify_private(path, directory=False)
    if identity.st_size > MAX_ANCHOR_BYTES:
        raise _evidence.EvidenceError("fleet dispatch anchor is oversized")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet dispatch anchor could not be read") from exc
    value = _evidence.loads(
        raw, max_bytes=MAX_ANCHOR_BYTES, max_items=MAX_ANCHOR_PARSE_ITEMS)
    return _validate_anchor(value, key, store, approval_id)


def _write_anchor(value: dict, key: bytes, store: dict) -> dict:
    body = _unsigned(value)
    authenticated = {
        **body,
        "mac": _mac(_anchor_key(
            key, body["store_id"], body["approval_id"]), body),
    }
    _validate_anchor(authenticated, key, store, body["approval_id"])
    serialized = json.dumps(
        authenticated, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(serialized) > MAX_ANCHOR_BYTES:
        raise _evidence.EvidenceError("fleet dispatch anchor is oversized")
    reparsed = _evidence.loads(
        serialized, max_bytes=MAX_ANCHOR_BYTES,
        max_items=MAX_ANCHOR_PARSE_ITEMS)
    _validate_anchor(reparsed, key, store, body["approval_id"])
    _fleet_approval._secure_private_root(anchor_root())
    try:
        _write_bytes_atomic(anchor_path(body["approval_id"]), serialized)
    except _evidence.EvidenceError:
        raise
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet dispatch anchor could not be written atomically") from exc
    return authenticated


def _reconcile_dispatch_anchor(store: dict, key: bytes, approval_id: str,
                               ledger_sha256: str | None, *,
                               ledger_retired: bool) -> dict:
    if (ledger_sha256 is not None
            and not _SHA256.fullmatch(str(ledger_sha256))):
        raise _evidence.EvidenceError("fleet dispatch ledger digest is malformed")
    if not isinstance(ledger_retired, bool):
        raise _evidence.EvidenceError("fleet dispatch retirement state is malformed")
    anchor = _read_anchor(key, store, approval_id, missing_ok=True)
    if anchor is None:
        if ledger_sha256 is not None:
            raise _evidence.EvidenceError(
                "unanchored fleet dispatch ledger cannot be adopted")
        approval = store.get("approvals", {}).get(approval_id)
        if approval is None:
            raise _evidence.EvidenceError(
                "fleet dispatch anchor is unavailable for a retired approval")
        anchor = {
            "schema": ANCHOR_SCHEMA, "store_id": store["store_id"],
            "approval_id": approval_id,
            "approval_generation": approval["generation"],
            "generation": 0, "state": "clean",
            "ledger_sha256": None, "pending_generation": None,
            "pending_ledger_sha256": None, "retired": ledger_retired,
            "pending_retired": None, "updated_at": _now_text(), "mac": "",
        }
        return _write_anchor(anchor, key, store)
    if anchor["state"] == "clean":
        if (ledger_sha256 != anchor["ledger_sha256"]
                or ledger_retired != anchor["retired"]):
            raise _evidence.EvidenceError(
                "fleet dispatch ledger replay or deletion detected")
        return anchor
    if (ledger_sha256 != anchor["pending_ledger_sha256"]
            or ledger_retired != anchor["pending_retired"]):
        raise _evidence.EvidenceError(
            "fleet dispatch prepared anchor cannot reconcile its ledger")
    anchor["ledger_sha256"] = anchor["pending_ledger_sha256"]
    anchor["retired"] = anchor["pending_retired"]
    anchor["generation"] = anchor["pending_generation"]
    anchor["state"] = "clean"
    anchor["pending_generation"] = None
    anchor["pending_ledger_sha256"] = None
    anchor["pending_retired"] = None
    anchor["updated_at"] = _now_text()
    return _write_anchor(anchor, key, store)


def _prepare_dispatch_anchor(store: dict, key: bytes, approval_id: str,
                             target_ledger_sha256: str, *,
                             target_retired: bool) -> int:
    if (not _SHA256.fullmatch(str(target_ledger_sha256))
            or not isinstance(target_retired, bool)):
        raise _evidence.EvidenceError("fleet dispatch anchor target is malformed")
    anchor = _read_anchor(key, store, approval_id, missing_ok=False)
    if anchor["state"] != "clean":
        raise _evidence.EvidenceError("fleet dispatch anchor is not ready")
    if anchor["generation"] >= MAX_GENERATION:
        raise _evidence.EvidenceError("fleet dispatch anchor generation is exhausted")
    if target_ledger_sha256 == anchor["ledger_sha256"]:
        raise _evidence.EvidenceError("fleet dispatch anchor target did not change")
    pending = anchor["generation"] + 1
    anchor["state"] = "prepared"
    anchor["pending_generation"] = pending
    anchor["pending_ledger_sha256"] = target_ledger_sha256
    anchor["pending_retired"] = target_retired
    anchor["updated_at"] = _now_text()
    _write_anchor(anchor, key, store)
    return pending


def _commit_dispatch_anchor(store: dict, key: bytes, approval_id: str,
                            pending_generation: int, ledger_sha256: str, *,
                            ledger_retired: bool) -> dict:
    anchor = _read_anchor(key, store, approval_id, missing_ok=False)
    if (anchor["state"] != "prepared"
            or anchor["pending_generation"] != pending_generation
            or anchor["pending_ledger_sha256"] != ledger_sha256
            or anchor["pending_retired"] != ledger_retired):
        raise _evidence.EvidenceError("fleet dispatch anchor commit is inconsistent")
    return _reconcile_dispatch_anchor(
        store, key, approval_id, ledger_sha256,
        ledger_retired=ledger_retired)


def _read_ledger_file(path: str, key: bytes,
                      approval_id: str, field: str) -> tuple[dict, bytes]:
    root = os.path.dirname(path)
    _fleet_approval._reject_reparse_ancestors(root)
    _fleet_approval._verify_private(root, directory=True)
    identity = _fleet_approval._regular_single_link(path, field)
    _fleet_approval._verify_private(path, directory=False)
    if identity.st_size > MAX_LEDGER_BYTES:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", f"{field} is oversized")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_io_failed", f"{field} could not be read") from exc
    try:
        value = _evidence.loads(
            raw, max_bytes=MAX_LEDGER_BYTES, max_items=MAX_PARSE_ITEMS)
        return _validate_ledger(value, key, approval_id), raw
    except FleetDispatchError:
        raise
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", str(exc)) from exc


def _promote_staged_ledger(staged: str, path: str) -> None:
    _fleet_approval._regular_single_link(staged, "staged dispatch ledger")
    _fleet_approval._verify_private(staged, directory=False)
    _jobs._replace_with_retry(staged, path)
    _fleet_approval._secure_file(path)
    if os.name != "nt":
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _recover_prepared_ledger(path: str, key: bytes,
                             approval_id: str, store: dict) -> None:
    anchor = _read_anchor(key, store, approval_id, missing_ok=True)
    if anchor is None or anchor.get("state") != "prepared":
        return
    staged = path + ".pending"
    if not os.path.lexists(staged):
        return
    value, raw = _read_ledger_file(
        staged, key, approval_id, "staged dispatch ledger")
    if (value["store_id"] != store.get("store_id")
            or hashlib.sha256(raw).hexdigest()
            != anchor.get("pending_ledger_sha256")
            or _ledger_retired(value) != anchor.get("pending_retired")):
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted",
            "staged dispatch ledger differs from its prepared anchor")
    try:
        _promote_staged_ledger(staged, path)
    except OSError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_io_failed",
            "staged dispatch ledger could not be promoted") from exc


def _read_initialization_marker(
        key: bytes, store: dict, approval_id: str) -> bool:
    path = initialization_path(approval_id)
    root = os.path.dirname(path)
    if not os.path.lexists(path):
        return False
    _fleet_approval._reject_reparse_ancestors(root)
    _fleet_approval._verify_private(root, directory=True)
    identity = _fleet_approval._regular_single_link(
        path, "dispatch initialization marker")
    _fleet_approval._verify_private(path, directory=False)
    if identity.st_size > MAX_INITIALIZATION_BYTES:
        raise _evidence.EvidenceError("fleet dispatch initialization marker is oversized")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet dispatch initialization marker could not be read") from exc
    value = _evidence.loads(
        raw, max_bytes=MAX_INITIALIZATION_BYTES, max_items=32)
    approval = store.get("approvals", {}).get(approval_id)
    fields = {"schema", "store_id", "approval_id", "approval_generation", "mac"}
    if (approval is None or not isinstance(value, dict) or set(value) != fields
            or value.get("schema") != INITIALIZATION_SCHEMA
            or value.get("store_id") != store.get("store_id")
            or value.get("approval_id") != approval_id
            or value.get("approval_generation") != approval.get("generation")):
        raise _evidence.EvidenceError(
            "fleet dispatch initialization marker is malformed")
    expected = _mac(
        _initialization_key(key, value["store_id"], approval_id), _unsigned(value))
    if not hmac.compare_digest(str(value.get("mac", "")), expected):
        raise _evidence.EvidenceError(
            "fleet dispatch initialization marker authentication failed")
    return True


def _write_initialization_marker(
        key: bytes, store: dict, approval_id: str) -> None:
    if _read_initialization_marker(key, store, approval_id):
        return
    approval = store.get("approvals", {}).get(approval_id)
    if approval is None:
        raise _evidence.EvidenceError(
            "fleet approval is unavailable for dispatch initialization")
    body = {
        "schema": INITIALIZATION_SCHEMA, "store_id": store["store_id"],
        "approval_id": approval_id,
        "approval_generation": approval["generation"],
    }
    authenticated = {
        **body,
        "mac": _mac(_initialization_key(
            key, store["store_id"], approval_id), body),
    }
    serialized = json.dumps(
        authenticated, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(serialized) > MAX_INITIALIZATION_BYTES:
        raise _evidence.EvidenceError(
            "fleet dispatch initialization marker is oversized")
    _fleet_approval._secure_private_root(initialization_root())
    _write_bytes_atomic(initialization_path(approval_id), serialized)
    if not _read_initialization_marker(key, store, approval_id):
        raise _evidence.EvidenceError(
            "fleet dispatch initialization marker could not be verified")


def _read_ledger(path: str, key: bytes, approval_id: str, store: dict, *,
                 missing_ok: bool) -> dict | None:
    try:
        _recover_prepared_ledger(path, key, approval_id, store)
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_replay", str(exc)) from exc
    ledger_exists = os.path.lexists(path)
    anchor_exists = os.path.lexists(anchor_path(approval_id))
    try:
        initialized = _read_initialization_marker(key, store, approval_id)
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_replay", str(exc)) from exc
    if not ledger_exists:
        if initialized and not anchor_exists:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_replay",
                "authenticated dispatch history exists but both dispatch roots are missing")
        if not initialized and not anchor_exists:
            if not missing_ok:
                raise FleetDispatchError(
                    "fleet_dispatch_claim_missing", "dispatch ledger does not exist")
            try:
                # Commit the durable marker first. A crash before anchor creation
                # deliberately becomes ambiguous history and fails closed later.
                _write_initialization_marker(key, store, approval_id)
            except _evidence.EvidenceError as exc:
                raise FleetDispatchError(
                    "fleet_dispatch_anchor_failed",
                    "dispatch first-use marker could not be recorded") from exc
        try:
            _reconcile_dispatch_anchor(
                store, key, approval_id, None, ledger_retired=False)
        except _evidence.EvidenceError as exc:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_replay", str(exc)) from exc
        if not initialized and anchor_exists:
            try:
                # Upgrade a legacy clean genesis anchor only after its exact
                # empty state reconciles. Commit the durable marker before any
                # caller can create a new ledger from that legacy state.
                _write_initialization_marker(key, store, approval_id)
            except _evidence.EvidenceError as exc:
                raise FleetDispatchError(
                    "fleet_dispatch_anchor_failed",
                    "legacy dispatch genesis could not be durably marked") from exc
        if missing_ok:
            return None
        raise FleetDispatchError(
            "fleet_dispatch_claim_missing", "dispatch ledger does not exist")
    value, raw = _read_ledger_file(path, key, approval_id, "dispatch ledger")
    try:
        if value["store_id"] != store.get("store_id"):
            raise FleetDispatchError(
                "fleet_dispatch_ledger_untrusted",
                "dispatch ledger belongs to another approval store")
        try:
            _reconcile_dispatch_anchor(
                store, key, approval_id, hashlib.sha256(raw).hexdigest(),
                ledger_retired=_ledger_retired(value))
        except _evidence.EvidenceError as exc:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_replay", str(exc)) from exc
        if not initialized:
            try:
                # Safely adopt a legacy authenticated ledger only after its
                # independent anchor has reconciled the exact bytes.
                _write_initialization_marker(key, store, approval_id)
            except _evidence.EvidenceError as exc:
                raise FleetDispatchError(
                    "fleet_dispatch_anchor_failed",
                    "legacy dispatch history could not be durably marked") from exc
        return value
    except FleetDispatchError:
        raise
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_untrusted", str(exc)) from exc


def _write_bytes_atomic(path: str, serialized: bytes) -> None:
    root = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=root, prefix=".summon-fleet-dispatch-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        _fleet_approval._secure_file(temporary)
        _jobs._replace_with_retry(temporary, path)
        _fleet_approval._secure_file(path)
        if os.name != "nt":
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_ledger(path: str, value: dict, key: bytes, store: dict) -> dict:
    body = _unsigned(value)
    ledger_key = _ledger_key(key, body["store_id"], body["approval_id"])
    authenticated = {**body, "mac": _mac(ledger_key, body)}
    _validate_ledger(authenticated, key, body["approval_id"])
    serialized = json.dumps(
        authenticated, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(serialized) > MAX_LEDGER_BYTES:
        raise FleetDispatchError(
            "fleet_dispatch_ledger_full", "dispatch ledger is full")
    reparsed = _evidence.loads(
        serialized, max_bytes=MAX_LEDGER_BYTES, max_items=MAX_PARSE_ITEMS)
    _validate_ledger(reparsed, key, body["approval_id"])
    target_sha256 = hashlib.sha256(serialized).hexdigest()
    target_retired = _ledger_retired(authenticated)
    staged = path + ".pending"
    try:
        _write_bytes_atomic(staged, serialized)
    except OSError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_io_failed",
            "dispatch ledger could not be staged durably") from exc
    try:
        pending_generation = _prepare_dispatch_anchor(
            store, key, body["approval_id"], target_sha256,
            target_retired=target_retired)
    except BaseException as exc:
        # The authenticated store replacement may have committed before a
        # subsequent ACL/fsync check reported failure. Retain the exact staged
        # target so a prepared anchor can recover it on the next locked read.
        if not isinstance(exc, _evidence.EvidenceError):
            raise
        raise FleetDispatchError(
            "fleet_dispatch_anchor_failed", str(exc)) from exc
    try:
        _promote_staged_ledger(staged, path)
    except (OSError, _evidence.EvidenceError) as exc:
        raise FleetDispatchError(
            "fleet_dispatch_io_failed", "dispatch ledger could not be written atomically") from exc
    try:
        _commit_dispatch_anchor(
            store, key, body["approval_id"], pending_generation,
            target_sha256, ledger_retired=target_retired)
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_anchor_failed",
            "dispatch ledger was written but its anchor remains prepared") from exc
    return authenticated


def _reservation(claim: dict) -> Reservation:
    return Reservation(
        approval_id=claim["request"]["decision_sha256"] and
                    _evidence.verify(claim["decision"])["digests"]["approval"],
        claim_id=claim["claim_id"], request_id=claim["request_id"],
        request_sha256=claim["request_sha256"],
        decision=json.loads(json.dumps(claim["decision"])),
        route=json.loads(json.dumps(claim["route"])),
    )


def reserve_dispatch(*, approval_id: str, fleet: dict, plan: dict,
                     catalog: list[dict], lane_name: str, cwd: str,
                     prompt_sha256: str,
                     billing_by_seat: dict[str, str], data_boundary: dict,
                     request_id: str | None = None) -> Reservation:
    """Reserve capacity for one deterministic, provider-inert lane decision."""
    _sha(prompt_sha256, "prompt_sha256")
    if request_id is not None and not _ID.fullmatch(str(request_id)):
        raise FleetDispatchError(
            "fleet_dispatch_request_invalid", "request id must be 32 lowercase hex characters")
    path = ledger_path(approval_id)
    now = _fleet_approval._now()
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        store, approval = _active_approval(approval_id, key, now)
        bindings, authority, normalized_catalog = _approved_context(
            fleet=fleet, plan=plan, catalog=catalog,
            lane_name=lane_name, cwd=cwd, approval=approval)
        _payload, lane = _fleet_approval._lane(plan, lane_name)
        seats = {item["seat"] for item in lane["candidates"]}
        billing = _normalize_billing(billing_by_seat, seats)
        boundary = _normalize_data_boundary(
            data_boundary, declared=authority["data_boundary"],
            prompt_sha256=prompt_sha256)
        decision, route = _decision_and_route(
            approval_id=approval_id, bindings=bindings, authority=authority,
            lane=lane, catalog=normalized_catalog, billing=billing,
            data_boundary=boundary)
        request = _semantic_request(
            bindings=bindings, authority=authority, decision=decision,
            prompt_sha256=prompt_sha256, billing=billing,
            data_boundary=boundary, route=route)
        request_sha256 = _digest(request)
        stable_id = request_id or request_sha256[:32]
        root = os.path.dirname(path)
        _fleet_approval._secure_private_root(root)
        ledger = _read_ledger(
            path, key, approval_id, store, missing_ok=True)
        approval_sha = _digest(approval)
        if ledger is None:
            ledger = {
                "schema": LEDGER_SCHEMA, "store_id": store["store_id"],
                "approval_id": approval_id,
                "approval_generation": approval["generation"],
                "approval_sha256": approval_sha, "bindings": bindings,
                "authority": authority, "generation": 0, "next_ordinal": 0,
                "totals": {"provider_contact_slots_consumed": 0,
                           "billable_slots_consumed": 0},
                "claims": {}, "mac": "",
            }
        elif (ledger["store_id"] != store["store_id"]
              or ledger["approval_generation"] != approval["generation"]
              or ledger["approval_sha256"] != approval_sha
              or ledger["bindings"] != bindings or ledger["authority"] != authority):
            raise FleetDispatchError(
                "fleet_dispatch_approval_mismatch",
                "dispatch ledger belongs to different approval authority")
        for existing in ledger["claims"].values():
            if existing["request_id"] == stable_id:
                if existing["request_sha256"] != request_sha256:
                    raise FleetDispatchError(
                        "fleet_dispatch_request_conflict",
                        "request id is already bound to different dispatch inputs")
                return _reservation(existing)
            if existing["request_sha256"] == request_sha256:
                return _reservation(existing)
        if len(ledger["claims"]) >= MAX_CLAIMS:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_full", "dispatch claim limit reached")
        spend = authority["spend"]
        contacts = ledger["totals"]["provider_contact_slots_consumed"] + sum(
            int(item["contact_slot_reserved"]) for item in ledger["claims"].values())
        billable = ledger["totals"]["billable_slots_consumed"] + sum(
            int(item["billable_slot_reserved"]) for item in ledger["claims"].values())
        active = sum(
            int(item["phase"] in ACTIVE_PHASES) for item in ledger["claims"].values())
        route_billable = route["billing_class"] in {"credit", "payg"}
        if contacts >= spend["max_provider_contacts"]:
            raise FleetDispatchError(
                "fleet_dispatch_contact_ceiling", "approved provider-contact ceiling is exhausted")
        if route_billable and billable >= spend["max_billable_attempts"]:
            raise FleetDispatchError(
                "fleet_dispatch_billable_ceiling", "approved billable-attempt ceiling is exhausted")
        if active >= spend["max_parallel"]:
            raise FleetDispatchError(
                "fleet_dispatch_parallel_ceiling", "approved parallel ceiling is exhausted")
        if ledger["generation"] >= MAX_GENERATION or ledger["next_ordinal"] >= MAX_CLAIMS:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_full", "dispatch generation limit reached")
        claim_id = os.urandom(16).hex()
        created = _now_text()
        claim = {
            "schema": CLAIM_SCHEMA, "ordinal": ledger["next_ordinal"] + 1,
            "claim_generation": 1, "claim_id": claim_id,
            "request_id": stable_id, "request_sha256": request_sha256,
            "request": request, "decision": decision, "route": route,
            "phase": "reserved", "contact_slot_reserved": True,
            "billable_slot_reserved": route_billable,
            "contact_slot_consumed": False, "billable_slot_consumed": False,
            "provider_contacted": False, "terminal_sha256": None,
            "created_at": created, "updated_at": created,
        }
        ledger["claims"][claim_id] = claim
        ledger["next_ordinal"] += 1
        ledger["generation"] += 1
        _write_ledger(path, ledger, key, store)
        return _reservation(claim)


def _activation_attempt_policy() -> dict:
    return {
        "foreground_subprocess_only": True, "max_attempts": 1,
        "retry": False, "fallback": False,
        "continuation": False, "background": False,
    }


def _validate_launch_evidence(value: Any, *, invocation: Any,
                              route: dict) -> dict:
    """Validate the private, final builder projection for one launch CAS.

    The executor computes these digests from the concrete command, argv, cwd, and
    environment-name set immediately before it asks for a launch claim.  The ledger
    records only the digest of this compact projection: no command, path, argv, or
    environment value can reach a public receipt.
    """
    if (not isinstance(value, dict) or set(value) != _LAUNCH_EVIDENCE_FIELDS
            or value.get("schema") != LAUNCH_EVIDENCE_SCHEMA):
        raise FleetDispatchError(
            "fleet_activation_launch_evidence_invalid",
            "final launch evidence has an invalid schema")
    backend = value.get("backend")
    transport = value.get("transport")
    if (not isinstance(backend, str) or not _PUBLIC_ID.fullmatch(backend)
            or not isinstance(transport, str) or not _PUBLIC_ID.fullmatch(transport)
            or backend != getattr(invocation, "cli", None)
            or backend != route.get("backend")
            or transport != "subprocess"
            or transport != getattr(invocation, "transport", None)):
        raise FleetDispatchError(
            "fleet_activation_launch_evidence_invalid",
            "final launch evidence differs from the resolved backend or transport")
    for field in (
            "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256",
            "env_sha256"):
        try:
            _sha(value.get(field), f"launch_evidence.{field}")
        except FleetDispatchError as exc:
            raise FleetDispatchError(
                "fleet_activation_launch_evidence_invalid",
                "final launch evidence has an invalid digest") from exc
    expected_cwd_sha256 = _digest(os.path.normcase(os.path.realpath(
        str(getattr(invocation, "cwd", "")))))
    if not hmac.compare_digest(value["cwd_sha256"], expected_cwd_sha256):
        raise FleetDispatchError(
            "fleet_activation_launch_evidence_invalid",
            "final launch evidence differs from the frozen working directory")
    return {field: value[field] for field in sorted(_LAUNCH_EVIDENCE_FIELDS)}


def _activation_request(*, bindings: dict, authority: dict, decision: dict,
                        prompt_sha256: str, billing: dict[str, str],
                        data_boundary: dict, route: dict,
                        activation_sha256: str) -> dict:
    request = _semantic_request(
        bindings=bindings, authority=authority, decision=decision,
        prompt_sha256=prompt_sha256, billing=billing,
        data_boundary=data_boundary, route=route)
    request["operation"] = "fleet_activation_dispatch"
    request["activation_sha256"] = activation_sha256
    return request


def _activation_inputs(*, approval_id: str, fleet: dict, plan: dict,
                       catalog: list[dict], lane_name: str, cwd: str,
                       data_boundary: dict, invocation: Any,
                       activation_candidate: dict,
                       agent_definition_sha256: str,
                       current_request_identity_sha256: str,
                       approval: dict) -> tuple[dict, dict, dict, dict, dict, dict]:
    try:
        candidate = _fleet_activation.validate_private_binding_inputs(
            activation_candidate,
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256,
            invocation=invocation)
    except _fleet_activation.FleetActivationError as exc:
        raise FleetDispatchError(exc.kind, str(exc)) from exc
    bindings, authority, normalized_catalog = _approved_context(
        fleet=fleet, plan=plan, catalog=catalog,
        lane_name=lane_name, cwd=cwd, approval=approval)
    _payload, lane = _fleet_approval._lane(plan, lane_name)
    if len(lane["candidates"]) != 1:
        raise FleetDispatchError(
            "fleet_activation_lane_ambiguous",
            "authoritative activation currently requires a single-candidate lane")
    seat = lane["candidates"][0]["seat"]
    expected_bindings = {**{key: value for key, value in bindings.items() if key != "lane"},
                         "approval_id": approval_id,
                         "prompt_sha256": _fleet_activation._sha(invocation.prompt),
                         "request_identity_sha256": current_request_identity_sha256}
    if (candidate["bindings"] != {**expected_bindings,
                                  "claim_id": candidate["bindings"]["claim_id"]}
            or candidate["agent"]["seat"] != seat):
        raise FleetDispatchError(
            "fleet_activation_context_changed",
            "activation candidate differs from the approved lane or bindings")
    billing_class = candidate["billing"]["class"]
    billing = {seat: billing_class}
    boundary = _normalize_data_boundary(
        data_boundary, declared=authority["data_boundary"],
        prompt_sha256=expected_bindings["prompt_sha256"])
    decision, route = _decision_and_route(
        approval_id=approval_id, bindings=bindings, authority=authority,
        lane=lane, catalog=normalized_catalog, billing=billing,
        data_boundary=boundary)
    if (route["seat"] != seat or route["backend"] != invocation.cli
            or route["model"] != invocation.model
            or route["permission"] != invocation.permission):
        raise FleetDispatchError(
            "fleet_activation_route_changed",
            "resolved lane route differs from the frozen invocation")
    request = _activation_request(
        bindings=bindings, authority=authority, decision=decision,
        prompt_sha256=expected_bindings["prompt_sha256"], billing=billing,
        data_boundary=boundary, route=route,
        activation_sha256=candidate["sha256"])
    return candidate, bindings, authority, decision, route, request


def reserve_activation_dispatch(*, approval_id: str, fleet: dict, plan: dict,
                                catalog: list[dict], lane_name: str, cwd: str,
                                data_boundary: dict, invocation: Any,
                                activation_candidate: dict,
                                agent_definition_sha256: str,
                                current_request_identity_sha256: str,
                                request_id: str | None = None) -> Reservation:
    """Privately bind one frozen Slice A candidate; never launch or consume it."""
    if request_id is not None and not _ID.fullmatch(str(request_id)):
        raise FleetDispatchError(
            "fleet_dispatch_request_invalid", "request id must be 32 lowercase hex characters")
    path = ledger_path(approval_id)
    now = _fleet_approval._now()
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        store, approval = _active_approval(approval_id, key, now)
        candidate, bindings, authority, decision, route, request = _activation_inputs(
            approval_id=approval_id, fleet=fleet, plan=plan, catalog=catalog,
            lane_name=lane_name, cwd=cwd, data_boundary=data_boundary,
            invocation=invocation, activation_candidate=activation_candidate,
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256,
            approval=approval)
        claim_id = candidate["bindings"]["claim_id"]
        request_sha256 = _digest(request)
        stable_id = request_id or claim_id
        activation = {
            "schema": ACTIVATION_BINDING_SCHEMA,
            "contract_sha256": candidate["sha256"],
            "agent_definition_sha256": agent_definition_sha256,
            "request_identity_sha256": current_request_identity_sha256,
            "private_binding_hmac": _fleet_activation.private_binding_hmac(
                invocation, master_key=key, approval_id=approval_id,
                claim_id=claim_id, contract_sha256=candidate["sha256"],
                agent_definition_sha256=agent_definition_sha256,
                current_request_identity_sha256=current_request_identity_sha256),
            "billing_class": candidate["billing"]["class"],
            "attempt_policy": _activation_attempt_policy(),
        }
        root = os.path.dirname(path)
        _fleet_approval._secure_private_root(root)
        ledger = _read_ledger(path, key, approval_id, store, missing_ok=True)
        approval_sha = _digest(approval)
        if ledger is None:
            ledger = {
                "schema": LEDGER_SCHEMA, "store_id": store["store_id"],
                "approval_id": approval_id,
                "approval_generation": approval["generation"],
                "approval_sha256": approval_sha, "bindings": bindings,
                "authority": authority, "generation": 0, "next_ordinal": 0,
                "totals": {"provider_contact_slots_consumed": 0,
                           "billable_slots_consumed": 0},
                "claims": {}, "mac": "",
            }
        elif (ledger["store_id"] != store["store_id"]
              or ledger["approval_generation"] != approval["generation"]
              or ledger["approval_sha256"] != approval_sha
              or ledger["bindings"] != bindings or ledger["authority"] != authority):
            raise FleetDispatchError(
                "fleet_dispatch_approval_mismatch",
                "dispatch ledger belongs to different approval authority")
        for existing in ledger["claims"].values():
            if existing["request_id"] == stable_id or existing["request_sha256"] == request_sha256:
                if (existing["request_sha256"] != request_sha256
                        or existing.get("activation") != activation):
                    raise FleetDispatchError(
                        "fleet_dispatch_request_conflict",
                        "activation request is already bound to different private inputs")
                return _reservation(existing)
        if claim_id in ledger["claims"]:
            raise FleetDispatchError(
                "fleet_dispatch_request_conflict", "activation claim id is already occupied")
        if len(ledger["claims"]) >= MAX_CLAIMS:
            raise FleetDispatchError("fleet_dispatch_ledger_full", "dispatch claim limit reached")
        spend = authority["spend"]
        contacts = ledger["totals"]["provider_contact_slots_consumed"] + sum(
            int(item["contact_slot_reserved"]) for item in ledger["claims"].values())
        bills = ledger["totals"]["billable_slots_consumed"] + sum(
            int(item["billable_slot_reserved"]) for item in ledger["claims"].values())
        active = sum(int(item["phase"] in ACTIVE_PHASES)
                     for item in ledger["claims"].values())
        route_billable = route["billing_class"] in {"credit", "payg"}
        if contacts >= spend["max_provider_contacts"]:
            raise FleetDispatchError("fleet_dispatch_contact_ceiling", "approved provider-contact ceiling is exhausted")
        if route_billable and bills >= spend["max_billable_attempts"]:
            raise FleetDispatchError("fleet_dispatch_billable_ceiling", "approved billable-attempt ceiling is exhausted")
        if active >= spend["max_parallel"]:
            raise FleetDispatchError("fleet_dispatch_parallel_ceiling", "approved parallel ceiling is exhausted")
        if ledger["generation"] >= MAX_GENERATION or ledger["next_ordinal"] >= MAX_CLAIMS:
            raise FleetDispatchError("fleet_dispatch_ledger_full", "dispatch generation limit reached")
        created = _now_text()
        claim = {
            "schema": CLAIM_SCHEMA, "ordinal": ledger["next_ordinal"] + 1,
            "claim_generation": 1, "claim_id": claim_id,
            "request_id": stable_id, "request_sha256": request_sha256,
            "request": request, "decision": decision, "route": route,
            "activation": activation, "phase": "reserved",
            "contact_slot_reserved": True,
            "billable_slot_reserved": route_billable,
            "contact_slot_consumed": False, "billable_slot_consumed": False,
            "provider_contacted": False, "terminal_sha256": None,
            "created_at": created, "updated_at": created,
        }
        ledger["claims"][claim_id] = claim
        ledger["next_ordinal"] += 1
        ledger["generation"] += 1
        _write_ledger(path, ledger, key, store)
        return _reservation(claim)


def _revalidate_launch_context(*, ledger: dict, claim: dict, approval: dict,
                               store: dict, fleet: dict, plan: dict,
                               catalog: list[dict], lane_name: str, cwd: str) -> None:
    bindings, authority, normalized_catalog = _approved_context(
        fleet=fleet, plan=plan, catalog=catalog,
        lane_name=lane_name, cwd=cwd, approval=approval)
    if (ledger["store_id"] != store["store_id"]
            or ledger["approval_generation"] != approval["generation"]
            or ledger["approval_sha256"] != _digest(approval)
            or ledger["bindings"] != bindings
            or ledger["authority"] != authority):
        raise FleetDispatchError(
            "fleet_dispatch_context_changed",
            "current approval context differs from the reserved dispatch")
    _payload, lane = _fleet_approval._lane(plan, lane_name)
    request = claim["request"]
    decision, route = _decision_and_route(
        approval_id=ledger["approval_id"], bindings=bindings,
        authority=authority, lane=lane, catalog=normalized_catalog,
        billing=request["billing"], data_boundary=request["data_boundary"])
    expected_request = _semantic_request(
        bindings=bindings, authority=authority, decision=decision,
        prompt_sha256=request["prompt_sha256"], billing=request["billing"],
        data_boundary=request["data_boundary"], route=route)
    if (claim["decision"] != decision or claim["route"] != route
            or request != expected_request
            or claim["request_sha256"] != _digest(expected_request)):
        raise FleetDispatchError(
            "fleet_dispatch_context_changed",
            "current lane decision differs from the reserved dispatch")


def preflight_activation_dispatch(
        reservation: Reservation, *, fleet: dict, plan: dict,
        catalog: list[dict], lane_name: str, cwd: str, data_boundary: dict,
        invocation: Any, activation_candidate: dict,
        agent_definition_sha256: str,
        current_request_identity_sha256: str) -> dict:
    """Revalidate a private activation reservation without consuming any slot.

    This is evidence only. A future launcher must repeat this validation while
    holding the same lock that consumes the slot and freezes final argv/env;
    returning ``ready`` here is never a transferable launch capability.
    """
    if not isinstance(reservation, Reservation):
        raise FleetDispatchError(
            "fleet_dispatch_claim_invalid", "dispatch reservation is invalid")
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        store, approval = _active_approval(
            reservation.approval_id, key, _fleet_approval._now())
        ledger = _read_ledger(
            ledger_path(reservation.approval_id), key,
            reservation.approval_id, store, missing_ok=False)
        claim = ledger["claims"].get(reservation.claim_id)
        if (claim is None or claim["request_id"] != reservation.request_id
                or claim["request_sha256"] != reservation.request_sha256
                or claim["decision"] != reservation.decision
                or claim["route"] != reservation.route
                or claim.get("activation") is None):
            raise FleetDispatchError(
                "fleet_dispatch_claim_untrusted", "activation reservation differs from its ledger")
        if claim["phase"] != "reserved":
            raise FleetDispatchError(
                "fleet_dispatch_cas_conflict", "activation reservation is no longer reserved")
        candidate, bindings, authority, decision, route, request = _activation_inputs(
            approval_id=reservation.approval_id, fleet=fleet, plan=plan,
            catalog=catalog, lane_name=lane_name, cwd=cwd,
            data_boundary=data_boundary, invocation=invocation,
            activation_candidate=activation_candidate,
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256,
            approval=approval)
        expected_hmac = _fleet_activation.private_binding_hmac(
            invocation, master_key=key, approval_id=reservation.approval_id,
            claim_id=reservation.claim_id, contract_sha256=candidate["sha256"],
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256)
        activation = claim["activation"]
        if (ledger["bindings"] != bindings or ledger["authority"] != authority
                or claim["decision"] != decision or claim["route"] != route
                or claim["request"] != request
                or activation["contract_sha256"] != candidate["sha256"]
                or activation["agent_definition_sha256"] != agent_definition_sha256
                or activation["request_identity_sha256"] != current_request_identity_sha256
                or not hmac.compare_digest(
                    activation["private_binding_hmac"], expected_hmac)):
            raise FleetDispatchError(
                "fleet_activation_context_changed",
                "current activation inputs differ from the private reservation")
        return {
            "ready": True, "provider_contacted": False,
            "contract_sha256": activation["contract_sha256"],
            "billing_class": activation["billing_class"],
            "attempt_policy": dict(activation["attempt_policy"]),
        }


def _mutate(reservation: Reservation, *, expected: str, target: str,
            require_active_approval: bool, terminal_sha256: str | None = None,
            provider_contacted: bool | None = None,
            live_context: tuple[dict, dict, list[dict], str, str] | None = None) -> dict:
    if not isinstance(reservation, Reservation):
        raise FleetDispatchError(
            "fleet_dispatch_claim_invalid", "dispatch reservation is invalid")
    path = ledger_path(reservation.approval_id)
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        if key is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable", "fleet approval key is unavailable")
        active_context = None
        if require_active_approval:
            active_context = _active_approval(
                reservation.approval_id, key, _fleet_approval._now())
            store = active_context[0]
        else:
            store = _fleet_approval._read_store(key, _fleet_approval._now())
            if store is None:
                raise FleetDispatchError(
                    "fleet_dispatch_approval_unavailable",
                    "fleet approval store is unavailable")
        ledger = _read_ledger(
            path, key, reservation.approval_id, store, missing_ok=False)
        claim = ledger["claims"].get(reservation.claim_id)
        if (claim is None or claim["request_id"] != reservation.request_id
                or claim["request_sha256"] != reservation.request_sha256
                or claim["decision"] != reservation.decision
                or claim["route"] != reservation.route):
            raise FleetDispatchError(
                "fleet_dispatch_claim_untrusted", "dispatch reservation differs from its ledger")
        if require_active_approval:
            if live_context is None:
                raise FleetDispatchError(
                    "fleet_dispatch_context_required",
                    "provider launch requires the current fleet, plan, catalog, and lane")
            fleet, plan, catalog, lane_name, cwd = live_context
            store, approval = active_context
            _revalidate_launch_context(
                ledger=ledger, claim=claim, approval=approval, store=store,
                fleet=fleet, plan=plan, catalog=catalog,
                lane_name=lane_name, cwd=cwd)
        if claim["phase"] != expected:
            raise FleetDispatchError(
                "fleet_dispatch_cas_conflict",
                f"dispatch claim is {claim['phase']}, not expected {expected}")
        if ledger["generation"] >= MAX_GENERATION or claim["claim_generation"] >= MAX_GENERATION:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_full", "dispatch generation limit reached")
        if target == "cancelled_pre_spawn":
            claim["contact_slot_reserved"] = False
            claim["billable_slot_reserved"] = False
        elif target == "provider_launch_claimed":
            spend = ledger["authority"]["spend"]
            billable = claim["route"]["billing_class"] in {"credit", "payg"}
            contacts_without_self = ledger["totals"]["provider_contact_slots_consumed"]
            bills_without_self = ledger["totals"]["billable_slots_consumed"]
            if contacts_without_self >= spend["max_provider_contacts"]:
                raise FleetDispatchError(
                    "fleet_dispatch_contact_ceiling", "provider-contact ceiling changed or is exhausted")
            if billable and bills_without_self >= spend["max_billable_attempts"]:
                raise FleetDispatchError(
                    "fleet_dispatch_billable_ceiling", "billable-attempt ceiling changed or is exhausted")
            claim["contact_slot_reserved"] = False
            claim["billable_slot_reserved"] = False
            claim["contact_slot_consumed"] = True
            claim["billable_slot_consumed"] = billable
            claim["provider_contacted"] = None
            ledger["totals"]["provider_contact_slots_consumed"] += 1
            ledger["totals"]["billable_slots_consumed"] += int(billable)
        elif target == "spawn_failed":
            claim["provider_contacted"] = False
        elif target in {"spawned", "reaped"}:
            claim["provider_contacted"] = True
        elif target == "indeterminate":
            if provider_contacted not in {None, True}:
                raise FleetDispatchError(
                    "fleet_dispatch_transition_invalid",
                    "indeterminate contact evidence must be true or unknown")
            claim["provider_contacted"] = provider_contacted
        elif target == "terminal":
            _sha(terminal_sha256, "terminal_sha256")
            if not isinstance(provider_contacted, bool):
                raise FleetDispatchError(
                    "fleet_dispatch_transition_invalid",
                    "terminal contact evidence must be an explicit boolean")
            claim["terminal_sha256"] = terminal_sha256
            claim["provider_contacted"] = provider_contacted
        else:
            raise FleetDispatchError(
                "fleet_dispatch_transition_invalid", "dispatch transition target is invalid")
        claim["phase"] = target
        claim["claim_generation"] += 1
        claim["updated_at"] = _now_text()
        ledger["generation"] += 1
        authenticated = _write_ledger(path, ledger, key, store)
        return json.loads(json.dumps(authenticated["claims"][reservation.claim_id]))


def cancel_pre_spawn(reservation: Reservation) -> dict:
    return _mutate(
        reservation, expected="reserved", target="cancelled_pre_spawn",
        require_active_approval=False)


def claim_provider_launch(reservation: Reservation, *, fleet: dict, plan: dict,
                          catalog: list[dict], lane_name: str, cwd: str) -> dict:
    """Consume the reserved contact/billable slots immediately before launch."""
    return _mutate(
        reservation, expected="reserved", target="provider_launch_claimed",
        require_active_approval=True,
        live_context=(fleet, plan, catalog, lane_name, cwd))


def claim_activation_provider_launch(
        reservation: Reservation, *, fleet: dict, plan: dict,
        catalog: list[dict], lane_name: str, cwd: str, data_boundary: dict,
        invocation: Any, activation_candidate: dict,
        agent_definition_sha256: str,
        current_request_identity_sha256: str,
        launch_evidence: dict) -> dict:
    """Atomically bind a final launch projection and consume one activation slot.

    This intentionally contains no process-launch capability.  A future executor must
    construct ``launch_evidence`` from its concrete command, argv, cwd, and child
    environment immediately before this call, then launch only that exact plan after
    the durable ``provider_launch_claimed`` result.  A provider has not been contacted
    when this function returns.
    """
    if not isinstance(reservation, Reservation):
        raise FleetDispatchError(
            "fleet_dispatch_claim_invalid", "dispatch reservation is invalid")
    path = ledger_path(reservation.approval_id)
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        if key is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable", "fleet approval key is unavailable")
        store, approval = _active_approval(
            reservation.approval_id, key, _fleet_approval._now())
        ledger = _read_ledger(
            path, key, reservation.approval_id, store, missing_ok=False)
        claim = ledger["claims"].get(reservation.claim_id)
        if (claim is None or claim["request_id"] != reservation.request_id
                or claim["request_sha256"] != reservation.request_sha256
                or claim["decision"] != reservation.decision
                or claim["route"] != reservation.route
                or claim.get("activation") is None):
            raise FleetDispatchError(
                "fleet_dispatch_claim_untrusted", "activation reservation differs from its ledger")
        if claim["phase"] != "reserved":
            raise FleetDispatchError(
                "fleet_dispatch_cas_conflict", "activation reservation is no longer reserved")

        candidate, bindings, authority, decision, route, request = _activation_inputs(
            approval_id=reservation.approval_id, fleet=fleet, plan=plan,
            catalog=catalog, lane_name=lane_name, cwd=cwd,
            data_boundary=data_boundary, invocation=invocation,
            activation_candidate=activation_candidate,
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256,
            approval=approval)
        expected_hmac = _fleet_activation.private_binding_hmac(
            invocation, master_key=key, approval_id=reservation.approval_id,
            claim_id=reservation.claim_id, contract_sha256=candidate["sha256"],
            agent_definition_sha256=agent_definition_sha256,
            current_request_identity_sha256=current_request_identity_sha256)
        activation = claim["activation"]
        if (ledger["bindings"] != bindings or ledger["authority"] != authority
                or claim["decision"] != decision or claim["route"] != route
                or claim["request"] != request
                or activation.get("contract_sha256") != candidate["sha256"]
                or activation.get("agent_definition_sha256") != agent_definition_sha256
                or activation.get("request_identity_sha256")
                != current_request_identity_sha256
                or activation.get("billing_class") != candidate["billing"]["class"]
                or activation.get("billing_class") != route["billing_class"]
                or "final_launch_sha256" in activation
                or not hmac.compare_digest(
                    str(activation.get("private_binding_hmac", "")), expected_hmac)):
            raise FleetDispatchError(
                "fleet_activation_context_changed",
                "current activation inputs differ from the private reservation")
        final_launch = _validate_launch_evidence(
            launch_evidence, invocation=invocation, route=route)

        if ledger["generation"] >= MAX_GENERATION or claim["claim_generation"] >= MAX_GENERATION:
            raise FleetDispatchError(
                "fleet_dispatch_ledger_full", "dispatch generation limit reached")
        spend = ledger["authority"]["spend"]
        billable = claim["route"]["billing_class"] in {"credit", "payg"}
        if ledger["totals"]["provider_contact_slots_consumed"] >= spend["max_provider_contacts"]:
            raise FleetDispatchError(
                "fleet_dispatch_contact_ceiling", "provider-contact ceiling changed or is exhausted")
        if (billable and ledger["totals"]["billable_slots_consumed"]
                >= spend["max_billable_attempts"]):
            raise FleetDispatchError(
                "fleet_dispatch_billable_ceiling", "billable-attempt ceiling changed or is exhausted")

        claim["activation"] = {
            **activation,
            "final_launch_sha256": _digest(final_launch),
        }
        claim["contact_slot_reserved"] = False
        claim["billable_slot_reserved"] = False
        claim["contact_slot_consumed"] = True
        claim["billable_slot_consumed"] = billable
        claim["provider_contacted"] = None
        claim["phase"] = "provider_launch_claimed"
        claim["claim_generation"] += 1
        claim["updated_at"] = _now_text()
        ledger["totals"]["provider_contact_slots_consumed"] += 1
        ledger["totals"]["billable_slots_consumed"] += int(billable)
        ledger["generation"] += 1
        authenticated = _write_ledger(path, ledger, key, store)
        return json.loads(json.dumps(authenticated["claims"][reservation.claim_id]))


def mark_spawn_failed(reservation: Reservation) -> dict:
    return _mutate(
        reservation, expected="provider_launch_claimed", target="spawn_failed",
        require_active_approval=False)


def mark_spawned(reservation: Reservation) -> dict:
    return _mutate(
        reservation, expected="provider_launch_claimed", target="spawned",
        require_active_approval=False)


def mark_reaped(reservation: Reservation) -> dict:
    return _mutate(
        reservation, expected="spawned", target="reaped",
        require_active_approval=False)


def mark_indeterminate(reservation: Reservation, *, expected_phase: str,
                       provider_contacted: bool | None) -> dict:
    if expected_phase not in {"provider_launch_claimed", "spawned", "reaped"}:
        raise FleetDispatchError(
            "fleet_dispatch_transition_invalid", "indeterminate source phase is invalid")
    if expected_phase in {"spawned", "reaped"} and provider_contacted is not True:
        raise FleetDispatchError(
            "fleet_dispatch_transition_invalid",
            "post-spawn indeterminate state must preserve known provider contact")
    return _mutate(
        reservation, expected=expected_phase, target="indeterminate",
        require_active_approval=False, provider_contacted=provider_contacted)


def mark_terminal(reservation: Reservation, *, expected_phase: str,
                  terminal_sha256: str, provider_contacted: bool) -> dict:
    if expected_phase not in {"reaped", "spawn_failed"}:
        raise FleetDispatchError(
            "fleet_dispatch_transition_invalid",
            "only a proven reap or pre-spawn failure may become terminal; "
            "indeterminate claims require a future authenticated reconciliation")
    if expected_phase == "reaped" and provider_contacted is not True:
        raise FleetDispatchError(
            "fleet_dispatch_transition_invalid", "reaped provider contact cannot be denied")
    if expected_phase == "spawn_failed" and provider_contacted is not False:
        raise FleetDispatchError(
            "fleet_dispatch_transition_invalid", "proven spawn failure cannot claim contact")
    return _mutate(
        reservation, expected=expected_phase, target="terminal",
        require_active_approval=False, terminal_sha256=terminal_sha256,
        provider_contacted=provider_contacted)


def get_claim(reservation: Reservation) -> dict:
    if not isinstance(reservation, Reservation):
        raise FleetDispatchError(
            "fleet_dispatch_claim_invalid", "dispatch reservation is invalid")
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        if key is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable", "fleet approval key is unavailable")
        store = _fleet_approval._read_store(key, _fleet_approval._now())
        if store is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable",
                "fleet approval store is unavailable")
        ledger = _read_ledger(
            ledger_path(reservation.approval_id), key,
            reservation.approval_id, store, missing_ok=False)
        claim = ledger["claims"].get(reservation.claim_id)
        if (claim is None or claim["request_id"] != reservation.request_id
                or claim["request_sha256"] != reservation.request_sha256
                or claim["decision"] != reservation.decision
                or claim["route"] != reservation.route):
            raise FleetDispatchError(
                "fleet_dispatch_claim_untrusted", "dispatch reservation differs from its ledger")
        return json.loads(json.dumps(claim))


def _public_payload(ledger: dict, claim: dict) -> dict:
    route = claim["route"]
    payload = {
        "status": "success",
        "action": "fleet_dispatch_claim",
        "authorization": "evidence_only",
        "approval": {
            "approval_id": ledger["approval_id"],
            "generation": ledger["approval_generation"],
        },
        "bindings": dict(ledger["bindings"]),
        "decision": {
            "schema": claim["decision"]["schema"],
            "sha256": claim["decision"]["sha256"],
        },
        "resolution": {
            "seat": route["seat"], "backend": route["backend"],
            "provider": route["provider"], "model": route["model"],
            "permission": route["permission"],
            "effective_permission": route["effective_permission"],
            "enforcement": route["enforcement"],
            "billing_class": route["billing_class"],
            "data_boundary_proof": claim["request"]["data_boundary"]["proof"],
        },
        "claim": {
            "claim_id": claim["claim_id"], "phase": claim["phase"],
            "contact_slot_reserved": claim["contact_slot_reserved"],
            "billable_slot_reserved": claim["billable_slot_reserved"],
            "contact_slot_consumed": claim["contact_slot_consumed"],
            "billable_slot_consumed": claim["billable_slot_consumed"],
            "provider_contacted": claim["provider_contacted"],
        },
    }
    if claim.get("activation") is not None:
        activation = claim["activation"]
        payload["activation"] = {
            "contract_sha256": activation["contract_sha256"],
            "billing_class": activation["billing_class"],
            "attempt_policy": dict(activation["attempt_policy"]),
        }
    return payload


def _validate_public_payload_before_hash(payload: dict) -> None:
    try:
        _evidence.validate(payload)
        _evidence._reject_private_public_text(payload)
        serialized = _canonical(payload)
    except (TypeError, ValueError, RecursionError, _evidence.EvidenceError) as exc:
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid",
            "public dispatch receipt contains invalid public text or exceeds bounds") from exc
    if len(serialized) > MAX_PUBLIC_RECEIPT_BYTES:
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt is oversized")


def public_receipt(reservation: Reservation) -> dict:
    """Return a redacted digest-sealed receipt; never a dispatch capability."""
    if not isinstance(reservation, Reservation):
        raise FleetDispatchError(
            "fleet_dispatch_claim_invalid", "dispatch reservation is invalid")
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        if key is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable", "fleet approval key is unavailable")
        store = _fleet_approval._read_store(key, _fleet_approval._now())
        if store is None:
            raise FleetDispatchError(
                "fleet_dispatch_approval_unavailable",
                "fleet approval store is unavailable")
        ledger = _read_ledger(
            ledger_path(reservation.approval_id), key,
            reservation.approval_id, store, missing_ok=False)
        claim = ledger["claims"].get(reservation.claim_id)
        if claim is None:
            raise FleetDispatchError(
                "fleet_dispatch_claim_missing", "dispatch claim is unavailable")
        payload = _public_payload(ledger, claim)
        _validate_public_payload_before_hash(payload)
    receipt = {"schema": PUBLIC_SCHEMA, **payload}
    receipt["sha256"] = hashlib.sha256(
        PUBLIC_SCHEMA.encode("ascii") + b"\0" + _canonical(payload)).hexdigest()
    return receipt


def verify_public_receipt(value: Any) -> dict:
    fields = {"schema", "sha256", "status", "action", "authorization",
              "approval", "bindings", "decision", "resolution", "claim"}
    if (not isinstance(value, dict)
            or set(value) not in {frozenset(fields), frozenset(fields | {"activation"})}
            or value.get("schema") != PUBLIC_SCHEMA):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt is malformed")
    payload = {key: item for key, item in value.items() if key not in {"schema", "sha256"}}
    _validate_public_payload_before_hash(payload)
    expected = hashlib.sha256(
        PUBLIC_SCHEMA.encode("ascii") + b"\0" + _canonical(payload)).hexdigest()
    if not hmac.compare_digest(str(value.get("sha256", "")), expected):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt digest is invalid")
    # Exact projection validation is obtained from its bounded field vocabulary.
    bindings = payload.get("bindings")
    resolution = payload.get("resolution")
    claim = payload.get("claim")
    activation = payload.get("activation")
    if (payload.get("status") != "success"
            or payload.get("action") != "fleet_dispatch_claim"
            or payload.get("authorization") != "evidence_only"
            or not isinstance(payload.get("approval"), dict)
            or set(payload["approval"]) != {"approval_id", "generation"}
            or not _SHA256.fullmatch(str(payload["approval"].get("approval_id", "")))
            or not isinstance(payload["approval"].get("generation"), int)
            or isinstance(payload["approval"].get("generation"), bool)
            or not isinstance(bindings, dict)
            or set(bindings) != {
                "fleet_sha256", "plan_sha256", "project_sha256", "catalog_sha256",
                "lane", "lane_sha256"}
            or not all(_SHA256.fullmatch(str(bindings.get(field, "")))
                       for field in ("fleet_sha256", "plan_sha256", "project_sha256",
                                     "catalog_sha256", "lane_sha256"))
            or not _LANE.fullmatch(str(bindings.get("lane", "")))
            or not isinstance(payload.get("decision"), dict)
            or set(payload["decision"]) != {"schema", "sha256"}
            or payload["decision"].get("schema") != _decision.SCHEMA
            or not _SHA256.fullmatch(str(payload["decision"].get("sha256", "")))
            or not isinstance(resolution, dict)
            or set(resolution) != {
                "seat", "backend", "provider", "model", "permission",
                "effective_permission", "enforcement", "billing_class",
                "data_boundary_proof"}
            or not isinstance(claim, dict)
            or set(claim) != {
                "claim_id", "phase", "contact_slot_reserved",
                "billable_slot_reserved", "contact_slot_consumed",
                "billable_slot_consumed", "provider_contacted"}):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt fields are invalid")
    if activation is not None and (
            not isinstance(activation, dict)
            or set(activation) != {"contract_sha256", "billing_class", "attempt_policy"}
            or not _SHA256.fullmatch(str(activation.get("contract_sha256", "")))
            or activation.get("billing_class") not in BILLING_CLASSES
            or activation.get("attempt_policy") != _activation_attempt_policy()
            or activation.get("billing_class") != resolution.get("billing_class")):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public activation projection is invalid")
    for field in ("seat", "backend", "provider"):
        _public_identifier(resolution.get(field), f"resolution.{field}")
    _public_text(resolution.get("model"), "resolution.model", nullable=True)
    if (resolution.get("permission") not in PERMISSION_ORDER
            or resolution.get("effective_permission") not in {
                "read-only", "safe-edit", "yolo", "unenforceable"}
            or resolution.get("enforcement") not in {
                "enforced", "unenforceable", "unknown"}
            or resolution.get("billing_class") not in BILLING_CLASSES
            or resolution.get("data_boundary_proof") not in set(DATA_PROOFS.values())
            or claim.get("phase") not in PHASES
            or not _ID.fullmatch(str(claim.get("claim_id", "")))
            or claim.get("provider_contacted") not in {None, False, True}
            or not all(isinstance(claim.get(field), bool) for field in (
                "contact_slot_reserved", "billable_slot_reserved",
                "contact_slot_consumed", "billable_slot_consumed"))):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt claim is invalid")
    billable = resolution["billing_class"] in {"credit", "payg"}
    phase = claim["phase"]
    actual = (
        claim["contact_slot_reserved"], claim["billable_slot_reserved"],
        claim["contact_slot_consumed"], claim["billable_slot_consumed"],
        claim["provider_contacted"],
    )
    coherent = {
        "reserved": (True, billable, False, False, False),
        "cancelled_pre_spawn": (False, False, False, False, False),
        "provider_launch_claimed": (False, False, True, billable, None),
        "spawn_failed": (False, False, True, billable, False),
        "spawned": (False, False, True, billable, True),
        "reaped": (False, False, True, billable, True),
    }
    if (phase in coherent and actual != coherent[phase]) or (
            phase == "indeterminate"
            and (actual[:4] != (False, False, True, billable)
                 or claim["provider_contacted"] not in {None, True})) or (
            phase == "terminal"
            and (actual[:4] != (False, False, True, billable)
                 or not isinstance(claim["provider_contacted"], bool))):
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt state is incoherent")
    try:
        _evidence._reject_private_public_text(payload)
    except _evidence.EvidenceError as exc:
        raise FleetDispatchError(
            "fleet_dispatch_receipt_invalid", "public dispatch receipt contains private text") from exc
    return payload


__all__ = [
    "LEDGER_SCHEMA", "CLAIM_SCHEMA", "PUBLIC_SCHEMA", "LAUNCH_EVIDENCE_SCHEMA",
    "FleetDispatchError",
    "Reservation", "reserve_dispatch", "reserve_activation_dispatch",
    "preflight_activation_dispatch", "cancel_pre_spawn",
    "claim_provider_launch", "claim_activation_provider_launch",
    "mark_spawn_failed", "mark_spawned",
    "mark_reaped", "mark_indeterminate", "mark_terminal", "get_claim",
    "public_receipt", "verify_public_receipt", "ledger_path",
]
