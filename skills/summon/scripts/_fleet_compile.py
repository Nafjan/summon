"""Pure compiler for Summon's provider-inert fleet configuration.

This module deliberately cannot discover a roster, select a route, launch a
process, read credentials, or contact a provider.  Callers pass an already
sanitized declarative catalog.  The compiler only validates cross-references
and emits a sealed candidate-and-constraint plan.  Selection and approval are
outside this provider-inert Phase 1 slice.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from _backend_policy import CAPABILITIES
from _evidence import EvidenceError, seal, verify


FLEET_SCHEMA = "summon.fleet/v1"
PLAN_SCHEMA = "summon.fleet-plan/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LANE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_PUBLIC_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SECRET = re.compile(
    r"(?i)(?:sk-(?:or-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|token|secret)[=:][^\s]{8,})")
_CATALOG_FIELDS = {
    "seat", "backend", "provider", "model", "permission", "capabilities",
    "provider_evidence", "effective_permission", "enforcement",
    "requires_spend", "source",
}


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceError(f"{field} must be a bounded public identifier")
    return value


def is_agent_identifier(value: Any) -> bool:
    """Match the existing public agent-name contract without raising."""
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _lane_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _LANE_ID.fullmatch(value):
        raise EvidenceError(f"{field} must be a bounded lowercase lane identifier")
    return value


def _public_text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not _PUBLIC_TEXT.fullmatch(value)
            or value.startswith(("/", "\\\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value) or ".." in value
            or _SECRET.search(value)):
        raise EvidenceError(f"{field} must be bounded public text")
    return value


def validate_catalog(catalog: Any) -> list[dict]:
    """Return a normalized safe catalog; reject executable/private metadata."""
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= 512:
        raise EvidenceError("fleet catalog requires 1..512 entries")
    normalized = []
    seats = []
    for index, item in enumerate(catalog):
        if not isinstance(item, dict) or set(item) != _CATALOG_FIELDS:
            raise EvidenceError("fleet catalog entry has malformed fields")
        seat = _identifier(item.get("seat"), f"catalog[{index}].seat")
        backend = _identifier(item.get("backend"), f"catalog[{index}].backend")
        provider = _identifier(item.get("provider"), f"catalog[{index}].provider")
        provider_evidence = item.get("provider_evidence")
        if provider_evidence not in {
                "backend_static", "named_registry", "model_selector_verified",
                "unknown"}:
            raise EvidenceError("fleet catalog provider evidence is invalid")
        model = _public_text(item.get("model"), f"catalog[{index}].model", nullable=True)
        permission = item.get("permission")
        if permission not in {"read-only", "safe-edit", "yolo"}:
            raise EvidenceError("fleet catalog permission is invalid")
        effective = item.get("effective_permission")
        if effective not in {"read-only", "safe-edit", "yolo", "unenforceable"}:
            raise EvidenceError("fleet catalog effective permission is invalid")
        capabilities = item.get("capabilities")
        if (not isinstance(capabilities, list) or len(capabilities) > 64
                or not all(isinstance(value, str) and _CAPABILITY.fullmatch(value)
                           and value in CAPABILITIES for value in capabilities)
                or capabilities != sorted(set(capabilities))):
            raise EvidenceError("fleet catalog capabilities must be canonical")
        enforcement = item.get("enforcement")
        if enforcement not in {"enforced", "unenforceable", "unknown"}:
            raise EvidenceError("fleet catalog enforcement is invalid")
        requires_spend = item.get("requires_spend")
        if not isinstance(requires_spend, bool):
            raise EvidenceError("fleet catalog requires_spend must be boolean")
        source = _identifier(item.get("source"), f"catalog[{index}].source")
        seats.append(seat)
        normalized.append({
            "seat": seat,
            "backend": backend,
            "provider": provider,
            "provider_evidence": provider_evidence,
            "model": model,
            "permission": permission,
            "effective_permission": effective,
            "capabilities": list(capabilities),
            "enforcement": enforcement,
            "requires_spend": requires_spend,
            "source": source,
        })
    if len(seats) != len(set(seats)) or len(seats) != len({seat.casefold() for seat in seats}):
        raise EvidenceError("fleet catalog seats must be unique under portable case folding")
    normalized.sort(key=lambda item: item["seat"])
    return normalized


def catalog_digest(catalog: Any) -> str:
    """Hash exactly the normalized declarative catalog consumed by a plan."""
    normalized = validate_catalog(catalog)
    raw = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_draft(*, lane: str, seats: list[str], permission_ceiling: str,
                provider_allowlist: list[str] | None = None,
                model_allowlist: list[str] | None = None,
                required_capabilities: list[str] | None = None,
                data_boundary: str = "local_sanitized",
                corrective: dict | None = None,
                spend: dict | None = None) -> dict:
    """Create one canonical draft fleet document without selecting a route."""
    _lane_identifier(lane, "lane")
    if not isinstance(seats, list) or not 1 <= len(seats) <= 128:
        raise EvidenceError("fleet proposal requires 1..128 seats")
    candidates = []
    for priority, seat in enumerate(seats):
        candidates.append({"seat": _identifier(seat, "seat"), "priority": priority})
    if (len({item["seat"] for item in candidates}) != len(candidates)
            or len({item["seat"].casefold() for item in candidates}) != len(candidates)):
        raise EvidenceError("fleet proposal seats must be unique")
    candidates.sort(key=lambda item: (item["priority"], item["seat"]))
    requested_capabilities = sorted(set(required_capabilities or []))
    unknown_capabilities = sorted(set(requested_capabilities) - CAPABILITIES)
    if unknown_capabilities:
        raise EvidenceError(
            "fleet proposal names unsupported capabilities: "
            + ", ".join(unknown_capabilities))
    payload = {
        "draft": True,
        "lanes": [{
            "name": lane,
            "candidates": candidates,
            "constraints": {
                "provider_allowlist": sorted(set(provider_allowlist or [])),
                "model_allowlist": sorted(set(model_allowlist or [])),
                "required_capabilities": requested_capabilities,
                "permission_ceiling": permission_ceiling,
                "data_boundary": data_boundary,
                "corrective": dict(corrective or {
                    "contract_repair": False,
                    "retry": False,
                    "fallback": False,
                    "continuation": False,
                }),
                "spend": dict(spend or {
                    "subscription": False,
                    "credit": False,
                    "payg": False,
                    "max_provider_contacts": 1,
                    "max_billable_attempts": 1,
                    "max_parallel": 1,
                }),
            },
        }],
    }
    return seal(FLEET_SCHEMA, payload)


def compile_fleet(*, fleet: dict, catalog: list[dict], project_sha256: str,
                  catalog_sha256: str) -> dict:
    """Compile cross-references into a sealed plan that cannot authorize work."""
    payload = verify(fleet)
    if fleet.get("schema") != FLEET_SCHEMA:
        raise EvidenceError("fleet document uses an unsupported schema")
    normalized_catalog = validate_catalog(catalog)
    by_seat = {item["seat"]: item for item in normalized_catalog}
    if not _SHA256.fullmatch(str(project_sha256)):
        raise EvidenceError("project_sha256 must be a SHA-256 digest")
    if not _SHA256.fullmatch(str(catalog_sha256)):
        raise EvidenceError("catalog_sha256 must be a SHA-256 digest")
    if catalog_sha256 != catalog_digest(normalized_catalog):
        raise EvidenceError("catalog_sha256 does not bind the compiled catalog")
    lane_names = {lane["name"] for lane in payload["lanes"]}
    seats_casefold = {seat.casefold(): seat for seat in by_seat}
    collisions = sorted(
        lane for lane in lane_names if lane.casefold() in seats_casefold)
    if collisions:
        raise EvidenceError(
            "fleet lane names collide with roster seats: " + ", ".join(collisions))
    missing = sorted({candidate["seat"]
                      for lane in payload["lanes"]
                      for candidate in lane["candidates"]
                      if candidate["seat"] not in by_seat})
    if missing:
        raise EvidenceError(
            "fleet references unknown roster seats: " + ", ".join(missing))
    unsupported_capabilities = sorted({
        capability
        for lane in payload["lanes"]
        for capability in lane["constraints"]["required_capabilities"]
        if capability not in CAPABILITIES
    })
    if unsupported_capabilities:
        raise EvidenceError(
            "fleet names unsupported capabilities: "
            + ", ".join(unsupported_capabilities))
    plan_payload = {
        "provider_contacted": False,
        "authorization": "advisory_only",
        "fleet_sha256": fleet["sha256"],
        "project_sha256": project_sha256,
        "catalog_sha256": catalog_sha256,
        "lanes": payload["lanes"],
    }
    return seal(PLAN_SCHEMA, plan_payload)


def verify_plan_for_fleet(plan: dict, fleet: dict) -> dict:
    """Verify both seals and their exact candidate/constraint relationship."""
    plan_payload = verify(plan)
    fleet_payload = verify(fleet)
    if plan.get("schema") != PLAN_SCHEMA or fleet.get("schema") != FLEET_SCHEMA:
        raise EvidenceError("fleet or plan uses an unsupported schema")
    if (plan_payload["fleet_sha256"] != fleet["sha256"]
            or plan_payload["lanes"] != fleet_payload["lanes"]):
        raise EvidenceError("fleet plan does not match the fleet document")
    return plan_payload
