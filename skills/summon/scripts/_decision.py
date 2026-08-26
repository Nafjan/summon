"""Pure decision kernel for Summon routing explanations.

This module has no dispatch capability. It evaluates already-resolved candidates
and constraints, producing an immutable, provider-inert explanation receipt.
"""

from __future__ import annotations

import re
from typing import Any

from _evidence import EvidenceError, digest


SCHEMA = "summon.decision/v1"
ENFORCEMENT = {"enforced", "unenforceable", "unknown"}
PERMISSION_ORDER = {"read-only": 0, "safe-edit": 1, "yolo": 2}
MAX_PRIORITY = 1_000_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}$")
_RULE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SECRET = re.compile(
    r"(?i)(?:sk-(?:or-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|token|secret)[=:][^\s]{8,})")


def _exact_keys(value: dict, allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvidenceError(f"{field} contains unknown fields: {', '.join(unknown)}")


def _text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or value.startswith(("/", "\\\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value)
            or ".." in value or _SECRET.search(value)):
        raise EvidenceError(f"{field} must be a non-empty bounded string")
    return value


def _slug_text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    text = _text(value, field, nullable=nullable)
    if text is not None and not _SLUG.fullmatch(text):
        raise EvidenceError(f"{field} must be a bounded public identifier")
    return text


def decide(*, request: dict, candidates: list[dict], constraints: dict,
           evidence: dict | None = None) -> dict:
    """Choose one eligible candidate without executing or contacting anything."""
    if not isinstance(request, dict) or not isinstance(constraints, dict):
        raise EvidenceError("request and constraints must be objects")
    _exact_keys(request, {"agent", "lane", "model", "resolved_agent",
                          "provider", "source"}, "request")
    _exact_keys(constraints, {"permission_ceiling", "spend_authorized",
                              "enforcement", "unknowns", "corrective_allowed",
                              "retry_allowed", "fallback_allowed", "require_fresh"},
                "constraints")
    for required in ("corrective_allowed", "retry_allowed", "fallback_allowed"):
        if required not in constraints:
            raise EvidenceError(f"constraints.{required} is required")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 128:
        raise EvidenceError("decision requires 1..128 candidates")
    agent = _slug_text(request.get("agent"), "request.agent", nullable=True)
    lane = _slug_text(request.get("lane"), "request.lane", nullable=True)
    if bool(agent) == bool(lane):
        raise EvidenceError("request must name exactly one agent or lane")
    exact_model = _text(request.get("model"), "request.model", nullable=True)
    resolved_agent = _slug_text(
        request.get("resolved_agent"), "request.resolved_agent", nullable=True)
    expected_seat = resolved_agent or agent
    requested_provider = _slug_text(
        request.get("provider"), "request.provider", nullable=True)
    resolution_source = request.get("source", "explicit_agent")
    if resolution_source not in {"explicit_agent", "approved_role", "approved_lane"}:
        raise EvidenceError("request.source is invalid")

    permission_ceiling = _slug_text(
        constraints.get("permission_ceiling"), "permission_ceiling", nullable=True)
    if permission_ceiling is not None and permission_ceiling not in PERMISSION_ORDER:
        raise EvidenceError("permission_ceiling is invalid")
    spend_authorized = constraints.get("spend_authorized")
    if not isinstance(spend_authorized, bool):
        raise EvidenceError("spend_authorized must be boolean")
    enforcement = constraints.get("enforcement", "unknown")
    if enforcement not in ENFORCEMENT:
        raise EvidenceError("enforcement must be enforced, unenforceable, or unknown")
    unknowns = constraints.get("unknowns") or []
    if (not isinstance(unknowns, list) or len(unknowns) > 128
            or not all(isinstance(item, str) and _RULE.fullmatch(item)
                       for item in unknowns)):
        raise EvidenceError("constraints.unknowns must be bounded strings")

    normalized = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise EvidenceError("candidate must be an object")
        _exact_keys(candidate, {"seat", "backend", "provider", "model", "permission",
                                "priority", "eligible", "reasons", "requires_spend",
                                "gate_allowed", "data_boundary_satisfied",
                                "requires_corrective", "requires_retry",
                                "requires_fallback", "freshness"},
                    f"candidates[{index}]")
        seat = _slug_text(candidate.get("seat"), f"candidates[{index}].seat")
        backend = _slug_text(candidate.get("backend"),
                        f"candidates[{index}].backend", nullable=True)
        model = _text(candidate.get("model"), f"candidates[{index}].model", nullable=True)
        permission = _slug_text(candidate.get("permission"),
                           f"candidates[{index}].permission")
        if permission not in PERMISSION_ORDER:
            raise EvidenceError("candidate.permission is invalid")
        priority = candidate.get("priority", index)
        if (not isinstance(priority, int) or isinstance(priority, bool)
                or not -MAX_PRIORITY <= priority <= MAX_PRIORITY):
            raise EvidenceError("candidate.priority must be a bounded integer")
        eligible = candidate.get("eligible", True)
        if not isinstance(eligible, bool):
            raise EvidenceError("candidate.eligible must be boolean")
        reasons = candidate.get("reasons") or []
        if (not isinstance(reasons, list) or len(reasons) > 64
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in reasons)):
            raise EvidenceError("candidate reasons must be bounded strings")
        losing = list(reasons)
        if not eligible and not losing:
            losing.append("candidate_ineligible")
        if losing:
            eligible = False
        if agent and seat != expected_seat:
            eligible = False
            losing.append("agent_mismatch")
        provider = _slug_text(candidate.get("provider"),
                         f"candidates[{index}].provider", nullable=True)
        if requested_provider and provider != requested_provider:
            eligible = False
            losing.append("exact_provider_mismatch")
        if exact_model and model != exact_model:
            eligible = False
            losing.append("exact_model_mismatch")
        requires_spend = candidate.get("requires_spend", False)
        if not isinstance(requires_spend, bool):
            raise EvidenceError("candidate.requires_spend must be boolean")
        if requires_spend and not spend_authorized:
            eligible = False
            losing.append("paid_route_not_authorized")
        for field, rule in (("gate_allowed", "gate_denied"),
                            ("data_boundary_satisfied", "data_boundary_unsatisfied")):
            value = candidate.get(field)
            if not isinstance(value, bool):
                raise EvidenceError(f"candidate.{field} must be an explicit boolean")
            if not value:
                eligible = False
                losing.append(rule)
        for required_field, allowed_field, rule in (
                ("requires_corrective", "corrective_allowed", "corrective_not_allowed"),
                ("requires_retry", "retry_allowed", "retry_not_allowed"),
                ("requires_fallback", "fallback_allowed", "fallback_not_allowed")):
            required = candidate.get(required_field, False)
            allowed = constraints.get(allowed_field)
            if not isinstance(required, bool) or not isinstance(allowed, bool):
                raise EvidenceError(
                    f"{required_field} and constraints.{allowed_field} must be boolean")
            if required and not allowed:
                eligible = False
                losing.append(rule)
        require_fresh = constraints.get("require_fresh", False)
        if not isinstance(require_fresh, bool):
            raise EvidenceError("constraints.require_fresh must be boolean")
        freshness = candidate.get("freshness", "unknown")
        if freshness not in {"fresh", "stale", "unknown"}:
            raise EvidenceError("candidate.freshness must be fresh, stale, or unknown")
        if require_fresh and freshness != "fresh":
            eligible = False
            losing.append("freshness_required")
        if permission_ceiling is not None:
            if permission not in PERMISSION_ORDER or permission_ceiling not in PERMISSION_ORDER:
                eligible = False
                losing.append("permission_order_unknown")
            elif PERMISSION_ORDER[permission] > PERMISSION_ORDER[permission_ceiling]:
                eligible = False
                losing.append("permission_ceiling_exceeded")
            if enforcement != "enforced":
                eligible = False
                losing.append(f"permission_{enforcement}")
        normalized.append({
            "seat": seat, "backend": backend, "provider": provider, "model": model,
            "permission": permission, "eligible": eligible,
            "losing_rules": sorted(set(losing)),
            "priority": priority,
        })
    eligible = [item for item in normalized if item["eligible"]]
    seats = [item["seat"] for item in normalized]
    if len(seats) != len(set(seats)):
        raise EvidenceError("candidate seats must be unique")
    eligible.sort(key=lambda item: (item["priority"], item["seat"]))
    winner = eligible[0] if eligible else None
    if winner is None:
        winning_rule = "no_eligible_candidate"
    elif resolution_source == "approved_role":
        winning_rule = "approved_role_resolved"
    elif agent:
        winning_rule = "exact_agent_preserved"
    else:
        winning_rule = "approved_lane_priority"

    body = {
        "provider_contacted": False,
        "request": ({"agent": agent, "lane": lane, "model": exact_model}
                    | ({"provider": requested_provider}
                       if requested_provider else {})
                    | ({"resolved_agent": resolved_agent}
                       if resolved_agent and resolved_agent != agent else {})),
        "resolution": ({
            "seat": winner["seat"], "backend": winner["backend"],
            "provider": winner["provider"], "model_targeted": winner["model"],
            "winning_rule": winning_rule,
        } if winner else {"seat": None, "backend": None,
                          "provider": None, "model_targeted": None,
                          "winning_rule": winning_rule}),
        "authority": {
            "permission_ceiling": permission_ceiling,
            "spend_authorized": spend_authorized,
            "enforcement": enforcement,
        },
        "candidates": normalized,
        "unknowns": sorted(set(unknowns)),
        "digests": _evidence_digests(evidence),
    }
    return {"schema": SCHEMA, **body, "decision_sha256": digest(SCHEMA, body)}


def _evidence_digests(evidence: dict | None) -> dict:
    value = dict(evidence or {})
    if set(value) - {"roster", "policy", "project", "approval"}:
        raise EvidenceError("decision evidence contains unknown digest fields")
    result = {}
    for key in ("roster", "policy", "project", "approval"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str)
                                 or not re.fullmatch(r"[0-9a-f]{64}", item)):
            raise EvidenceError(f"evidence.{key} must be a SHA-256 digest or null")
        result[key] = item
    return result
