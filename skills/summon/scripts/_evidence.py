"""Canonical, provider-inert evidence primitives for Summon.

The module deliberately imports no executor, process, network, installer, or
provider code. Every digest binds the schema identifier into its preimage.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from _backend_policy import CAPABILITIES


KNOWN_SCHEMAS = {
    "summon.evidence/v1",
    "summon.evidence/v2",
    "summon.decision/v2",
    "summon.fleet/v1",
    "summon.fleet-plan/v1",
    "summon.liveness/v1",
}
MAX_DEPTH = 24
MAX_ITEMS = 4096
MAX_STRING_BYTES = 1 << 20
MAX_INTEGER = (1 << 63) - 1
_PUBLIC_SECRET = re.compile(
    r"(?i)(?:sk-(?:or-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(?:api[_-]?key|token|secret)[=:][^\s]{8,})")
_RULE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_FLEET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LANE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _public_string(value: Any, field: str, *, nullable: bool = False,
                   maximum: int = 256) -> str | None:
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise EvidenceError(f"{field} must be a bounded public string")
    _reject_private_public_text(value)
    return value


def _rule(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _RULE.fullmatch(value):
        raise EvidenceError(f"{field} must be a typed rule identifier")
    return value


class EvidenceError(ValueError):
    """Evidence is malformed, unbounded, or uses an unknown schema."""


def _reject_constant(token: str) -> None:
    raise EvidenceError(f"non-finite JSON number: {token}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_fleet_lanes(lanes: Any) -> None:
    """Validate the declarative, deliberately non-routing fleet lane shape."""
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= 64:
        raise EvidenceError("fleet requires 1..64 lanes")
    lane_names = []
    for lane_index, lane in enumerate(lanes):
        if not isinstance(lane, dict) or set(lane) != {
                "name", "candidates", "constraints"}:
            raise EvidenceError("fleet lane has malformed fields")
        lane_name = _public_string(lane.get("name"), f"lanes[{lane_index}].name")
        if not _LANE_ID.fullmatch(lane_name):
            raise EvidenceError("fleet lane name must be a typed identifier")
        lane_names.append(lane_name)
        candidates = lane.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 128:
            raise EvidenceError("fleet lane requires 1..128 candidates")
        seats = []
        canonical_candidates = []
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict) or set(candidate) != {"seat", "priority"}:
                raise EvidenceError("fleet candidate has malformed fields")
            seat = _public_string(
                candidate.get("seat"),
                f"lanes[{lane_index}].candidates[{candidate_index}].seat")
            if not _FLEET_ID.fullmatch(seat):
                raise EvidenceError("fleet candidate seat must be a typed identifier")
            priority = candidate.get("priority")
            if (not isinstance(priority, int) or isinstance(priority, bool)
                    or not -1_000_000 <= priority <= 1_000_000):
                raise EvidenceError("fleet candidate priority must be a bounded integer")
            seats.append(seat)
            canonical_candidates.append((priority, seat))
        if len(seats) != len(set(seats)) or len(seats) != len({seat.casefold() for seat in seats}):
            raise EvidenceError("fleet candidate seats must be unique within a lane")
        if canonical_candidates != sorted(canonical_candidates):
            raise EvidenceError("fleet candidates must be sorted by priority then seat")

        constraints = lane.get("constraints")
        if not isinstance(constraints, dict) or set(constraints) != {
                "provider_allowlist", "model_allowlist", "required_capabilities",
                "permission_ceiling", "data_boundary", "corrective", "spend"}:
            raise EvidenceError("fleet constraints have malformed fields")
        for field in ("provider_allowlist", "model_allowlist", "required_capabilities"):
            values = constraints.get(field)
            if not isinstance(values, list) or len(values) > 64:
                raise EvidenceError(f"fleet {field} must be canonical identifiers")
            for item in values:
                _public_string(item, f"fleet {field}")
                valid = (_RULE.fullmatch(item) if field == "required_capabilities"
                         else (_FLEET_ID.fullmatch(item)
                               if field == "provider_allowlist" else True))
                if field == "required_capabilities" and item not in CAPABILITIES:
                    valid = False
                if not valid:
                    raise EvidenceError(f"fleet {field} must be canonical identifiers")
            if values != sorted(set(values)):
                raise EvidenceError(f"fleet {field} must be canonical identifiers")
        if constraints.get("permission_ceiling") not in {
                "read-only", "safe-edit", "yolo"}:
            raise EvidenceError("fleet permission ceiling is invalid")
        if constraints.get("data_boundary") not in {
                "unspecified", "public", "local_sanitized", "private_local"}:
            raise EvidenceError("fleet data boundary is invalid")
        corrective = constraints.get("corrective")
        if (not isinstance(corrective, dict) or set(corrective) != {
                "contract_repair", "retry", "fallback", "continuation"}
                or not all(isinstance(value, bool) for value in corrective.values())):
            raise EvidenceError("fleet corrective policy is malformed")
        spend = constraints.get("spend")
        if (not isinstance(spend, dict) or set(spend) != {
                "subscription", "credit", "payg", "max_provider_contacts",
                "max_billable_attempts", "max_parallel"}
                or not all(isinstance(spend.get(field), bool)
                           for field in ("subscription", "credit", "payg"))):
            raise EvidenceError("fleet spend policy is malformed")
        contacts = spend.get("max_provider_contacts")
        billable = spend.get("max_billable_attempts")
        parallel = spend.get("max_parallel")
        if (not isinstance(contacts, int) or isinstance(contacts, bool)
                or not 0 <= contacts <= 128
                or not isinstance(billable, int) or isinstance(billable, bool)
                or not 0 <= billable <= contacts
                or not isinstance(parallel, int) or isinstance(parallel, bool)
                or not 1 <= parallel <= 32
                or parallel > max(1, contacts)):
            raise EvidenceError("fleet spend/contact ceilings are invalid")
    if lane_names != sorted(lane_names) or len(lane_names) != len(set(lane_names)):
        raise EvidenceError("fleet lanes must be unique and sorted by name")


def _validate_fleet_document(value: dict) -> None:
    if set(value) != {"draft", "lanes"} or value.get("draft") is not True:
        raise EvidenceError("summon.fleet/v1 must be an explicit draft")
    _validate_fleet_lanes(value.get("lanes"))
    _reject_private_public_text(value)


def _validate_fleet_plan(value: dict) -> None:
    if set(value) != {
            "provider_contacted", "authorization", "fleet_sha256",
            "project_sha256", "catalog_sha256", "lanes"}:
        raise EvidenceError("summon.fleet-plan/v1 has malformed fields")
    if value.get("provider_contacted") is not False:
        raise EvidenceError("fleet plan must be provider-inert")
    if value.get("authorization") != "advisory_only":
        raise EvidenceError("fleet plan cannot authorize dispatch")
    for field in ("fleet_sha256", "project_sha256", "catalog_sha256"):
        digest_value = value.get(field)
        if (not isinstance(digest_value, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest_value)):
            raise EvidenceError(f"fleet plan {field} must be a SHA-256 digest")
    _validate_fleet_lanes(value.get("lanes"))
    _reject_private_public_text(value)


def loads(data: str | bytes, *, max_bytes: int = 1 << 20,
          max_items: int = MAX_ITEMS) -> Any:
    """Parse bounded UTF-8 JSON with duplicate/non-finite rejection."""
    if (not isinstance(max_items, int) or isinstance(max_items, bool)
            or max_items < 1):
        raise EvidenceError("evidence item limit must be a positive integer")
    if isinstance(data, bytes):
        raw = data
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeError as exc:
            raise EvidenceError("evidence must be valid UTF-8") from exc
    elif isinstance(data, str):
        text = data.lstrip("\ufeff")
        try:
            raw = text.encode("utf-8")
        except UnicodeError as exc:
            raise EvidenceError("evidence must be valid UTF-8") from exc
    else:
        raise EvidenceError("evidence must be JSON text or bytes")
    if len(raw) > max_bytes:
        raise EvidenceError(f"evidence exceeds {max_bytes} bytes")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EvidenceError("evidence must be valid JSON") from exc
    validate(value, max_items=max_items)
    return value


def validate(value: Any, *, depth: int = 0, counter: list[int] | None = None,
             max_items: int = MAX_ITEMS) -> None:
    """Validate a JSON-compatible value against global resource bounds."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > max_items:
        raise EvidenceError(f"evidence exceeds {max_items} values")
    if depth > MAX_DEPTH:
        raise EvidenceError(f"evidence exceeds depth {MAX_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_INTEGER:
            raise EvidenceError("integer is outside the supported range")
        return
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_INTEGER:
            raise EvidenceError("number is non-finite or outside the supported range")
        return
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeError as exc:
            raise EvidenceError("strings must contain valid Unicode scalar values") from exc
        if len(encoded) > MAX_STRING_BYTES:
            raise EvidenceError(f"string exceeds {MAX_STRING_BYTES} bytes")
        return
    if isinstance(value, list):
        for item in value:
            validate(item, depth=depth + 1, counter=counter,
                     max_items=max_items)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError("object keys must be strings")
            validate(key, depth=depth + 1, counter=counter,
                     max_items=max_items)
            validate(item, depth=depth + 1, counter=counter,
                     max_items=max_items)
        return
    raise EvidenceError(f"unsupported evidence type: {type(value).__name__}")


def _validate_schema_value(schema: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise EvidenceError(f"{schema} evidence must be an object")
    if schema == "summon.evidence/v2":
        if set(value) != {"kind", "value"}:
            raise EvidenceError(
                "summon.evidence/v2 requires exactly kind and value")
        _rule(value.get("kind"), "evidence.kind")
        _reject_private_public_text(value)
    elif schema == "summon.fleet/v1":
        _validate_fleet_document(value)
    elif schema == "summon.fleet-plan/v1":
        _validate_fleet_plan(value)
    elif schema == "summon.decision/v2":
        required = {"provider_contacted", "request", "resolution", "authority",
                    "candidates", "unknowns", "digests"}
        missing = sorted(required - set(value))
        if missing:
            raise EvidenceError(
                "summon.decision/v2 is missing fields: " + ", ".join(missing))
        if value.get("provider_contacted") is not False:
            raise EvidenceError("decision evidence must be provider-inert")
        if not isinstance(value.get("request"), dict) \
                or not isinstance(value.get("resolution"), dict) \
                or not isinstance(value.get("authority"), dict) \
                or not isinstance(value.get("candidates"), list) \
                or not isinstance(value.get("unknowns"), list) \
                or not isinstance(value.get("digests"), dict):
            raise EvidenceError("decision evidence has malformed composites")
        optional_top = {"usage"}
        if not required.issubset(value) or set(value) - required - optional_top:
            raise EvidenceError("decision evidence contains unknown fields")
        request = value["request"]
        resolution = value["resolution"]
        authority = value["authority"]
        digests = value["digests"]
        if set(request) - {"agent", "lane", "model", "provider", "resolved_agent"}:
            raise EvidenceError("decision request contains unknown fields")
        if ({"seat", "backend", "provider", "model_targeted", "winning_rule"}
                - set(resolution) or set(resolution) - {
                    "seat", "backend", "provider", "model_targeted", "winning_rule",
                    "source", "precedence"}):
            raise EvidenceError("decision resolution has malformed fields")
        if ({"permission_ceiling", "spend_authorized", "enforcement",
             "unenforceable_authorized", "corrective_allowed", "retry_allowed",
             "fallback_allowed", "require_fresh"}
                - set(authority) or set(authority) - {
                    "permission_ceiling", "spend_authorized", "enforcement",
                    "unenforceable_authorized",
                    "corrective_allowed", "retry_allowed", "fallback_allowed",
                    "require_fresh",
                    "effective_permission", "strict_roster", "credit", "payg"}):
            raise EvidenceError("decision authority has malformed fields")
        if set(digests) != {"roster", "policy", "project", "approval"}:
            raise EvidenceError("decision digests have malformed fields")
        if bool(request.get("agent")) == bool(request.get("lane")):
            raise EvidenceError("decision request must name exactly one agent or lane")
        if request.get("lane") and request.get("resolved_agent"):
            raise EvidenceError("decision lane request cannot pre-resolve an agent")
        for field in ("agent", "lane", "model", "provider", "resolved_agent"):
            item = request.get(field)
            _public_string(item, f"request.{field}", nullable=True)
        for field in ("seat", "backend", "provider", "model_targeted"):
            item = resolution.get(field)
            _public_string(item, f"resolution.{field}", nullable=True)
        _rule(resolution.get("winning_rule"), "resolution.winning_rule")
        if resolution["winning_rule"] not in {
                "no_eligible_candidate", "approved_role_resolved",
                "exact_agent_preserved", "approved_lane_priority"}:
            raise EvidenceError("decision winning rule is unknown")
        if "source" in resolution:
            if (not isinstance(resolution["source"], str)
                    or resolution["source"] not in {
                        "explicit_agent", "approved_role", "approved_lane"}):
                raise EvidenceError("decision resolution source is invalid")
        if "precedence" in resolution:
            precedence = resolution["precedence"]
            canonical_precedence = [
                "exact_agent", "approved_role", "approved_lane"]
            if (not isinstance(precedence, list) or not precedence
                    or len(precedence) > 16
                    or not all(isinstance(item, str)
                               and item in {"exact_agent", "approved_role",
                                            "approved_lane"}
                               for item in precedence)
                    or precedence != canonical_precedence[:len(precedence)]):
                raise EvidenceError("decision precedence is invalid")
        if not 1 <= len(value["candidates"]) <= 128:
            raise EvidenceError("decision requires 1..128 candidates")
        seats = []
        for candidate in value["candidates"]:
            if not isinstance(candidate, dict) or set(candidate) != {
                    "seat", "backend", "provider", "model", "permission", "eligible",
                    "losing_rules", "priority", "declared_eligible",
                    "declared_reasons", "requires_spend", "gate_allowed",
                    "data_boundary_satisfied", "requires_corrective",
                    "requires_retry", "requires_fallback", "freshness",
                    "preflight_rules"}:
                raise EvidenceError("decision candidate has malformed fields")
            _public_string(candidate.get("seat"), "candidate.seat")
            seats.append(candidate["seat"])
            _public_string(candidate.get("backend"), "candidate.backend", nullable=True)
            _public_string(candidate.get("provider"), "candidate.provider", nullable=True)
            _public_string(candidate.get("model"), "candidate.model", nullable=True)
            if (candidate.get("permission") not in
                    {"read-only", "safe-edit", "yolo"}
                    or not isinstance(candidate.get("eligible"), bool)
                    or not isinstance(candidate.get("priority"), int)
                    or isinstance(candidate.get("priority"), bool)
                    or not -1_000_000 <= candidate.get("priority") <= 1_000_000
                or not isinstance(candidate.get("losing_rules"), list)
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in candidate["losing_rules"])
                or not isinstance(candidate.get("declared_eligible"), bool)
                or not isinstance(candidate.get("declared_reasons"), list)
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in candidate["declared_reasons"])
                or not isinstance(candidate.get("preflight_rules"), list)
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in candidate["preflight_rules"])
                or not all(isinstance(candidate.get(field), bool) for field in (
                    "requires_spend", "gate_allowed", "data_boundary_satisfied",
                    "requires_corrective", "requires_retry", "requires_fallback"))
                or candidate.get("freshness") not in {"fresh", "stale", "unknown"}):
                raise EvidenceError("decision candidate has invalid values")
            if candidate["losing_rules"] != sorted(set(candidate["losing_rules"])):
                raise EvidenceError(
                    "decision candidate losing rules are not canonical")
            if candidate["declared_reasons"] != sorted(
                    set(candidate["declared_reasons"])):
                raise EvidenceError(
                    "decision candidate declared reasons are not canonical")
            if candidate["preflight_rules"] != sorted(
                    set(candidate["preflight_rules"])):
                raise EvidenceError(
                    "decision candidate preflight rules are not canonical")
            if candidate["eligible"] == bool(candidate["losing_rules"]):
                raise EvidenceError("decision candidate eligibility contradicts losing rules")
        if len(seats) != len(set(seats)):
            raise EvidenceError("decision candidate seats must be unique")
        if (not isinstance(value["unknowns"], list)
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in value["unknowns"])):
            raise EvidenceError("decision unknowns have invalid values")
        if value["unknowns"] != sorted(set(value["unknowns"])):
            raise EvidenceError("decision unknowns are not canonical")
        for item in digests.values():
            if item is not None and (not isinstance(item, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", item)):
                raise EvidenceError("decision digest reference is invalid")
        if (not isinstance(authority.get("spend_authorized"), bool)
                or not isinstance(authority.get("unenforceable_authorized"), bool)
                or not all(isinstance(authority.get(field), bool) for field in (
                    "corrective_allowed", "retry_allowed", "fallback_allowed",
                    "require_fresh"))
                or (authority.get("permission_ceiling") is not None
                    and authority.get("permission_ceiling") not in
                    {"read-only", "safe-edit", "yolo"})
                or authority.get("enforcement") not in
                {"enforced", "unenforceable", "unknown"}):
            raise EvidenceError("decision authority has invalid values")
        if "effective_permission" in authority \
                and (not isinstance(authority["effective_permission"], str)
                     or authority["effective_permission"] not in {
                        "read-only", "safe-edit", "yolo"}):
            raise EvidenceError("decision effective permission is invalid")
        if "strict_roster" in authority and not isinstance(authority["strict_roster"], bool):
            raise EvidenceError("decision strict_roster must be boolean")
        for field in ("credit", "payg"):
            if field not in authority:
                continue
            item = authority[field]
            allowed_sources = ({"none", "dispatch_flag", "environment"}
                               if field == "credit" else
                               {"none", "dispatch_flag", "operator_configuration"})
            if (not isinstance(item, dict) or set(item) != {"authorized", "source"}
                    or not isinstance(item.get("authorized"), bool)
                    or item.get("source") not in allowed_sources
                    or item["authorized"] != (item["source"] != "none")):
                raise EvidenceError(f"decision authority.{field} is malformed")
        projected_spend = [authority[field]["authorized"]
                           for field in ("credit", "payg")
                           if field in authority]
        if any(projected_spend) and not authority["spend_authorized"]:
            raise EvidenceError(
                "decision spend projection contradicts authorization")
        if len(projected_spend) == 2 \
                and authority["spend_authorized"] != any(projected_spend):
            raise EvidenceError(
                "decision spend projection contradicts authorization")
        selected = resolution.get("seat")
        source = resolution.get("source")
        resolved_agent = request.get("resolved_agent")
        if source == "approved_lane" and not request.get("lane"):
            raise EvidenceError(
                "decision resolution source contradicts request shape")
        if source in {"explicit_agent", "approved_role"} and not request.get("agent"):
            raise EvidenceError(
                "decision resolution source contradicts request shape")
        if (source == "approved_role"
                and (not resolved_agent or resolved_agent == request.get("agent"))):
            raise EvidenceError(
                "approved role source lacks distinct resolved agent")
        if source is None:
            raise EvidenceError("decision resolution source is required")
        expected_rule = ({
            "explicit_agent": "exact_agent_preserved",
            "approved_role": "approved_role_resolved",
            "approved_lane": "approved_lane_priority",
        }[source])
        expected_seat = resolved_agent or request.get("agent")
        exact_model = request.get("model")
        exact_provider = request.get("provider")
        permission_order = {"read-only": 0, "safe-edit": 1, "yolo": 2}
        permission_ceiling = authority.get("permission_ceiling")
        enforcement = authority["enforcement"]
        unenforceable_authorized = authority["unenforceable_authorized"]
        for candidate in value["candidates"]:
            losing = candidate["losing_rules"]
            expected_losing = set(candidate["declared_reasons"])
            if not candidate["declared_eligible"] and not expected_losing:
                expected_losing.add("candidate_ineligible")
            bindings = []
            if request.get("agent") and expected_seat is not None:
                bindings.append(("agent_mismatch",
                                 candidate["seat"] == expected_seat))
            if exact_model is not None:
                bindings.append(("exact_model_mismatch",
                                 candidate["model"] == exact_model))
            if exact_provider is not None:
                bindings.append(("exact_provider_mismatch",
                                 candidate["provider"] == exact_provider))
            for rule, matches in bindings:
                if not matches:
                    expected_losing.add(rule)
                has_rule = rule in losing
                if matches and has_rule:
                    raise EvidenceError(
                        "decision candidate contradicts exact request binding")
                if not matches and not has_rule:
                    raise EvidenceError(
                        "decision candidate ignores exact request binding")
            if candidate["requires_spend"] and not authority["spend_authorized"]:
                expected_losing.add("paid_route_not_authorized")
            if not candidate["gate_allowed"]:
                expected_losing.add("gate_denied")
            if not candidate["data_boundary_satisfied"]:
                expected_losing.add("data_boundary_unsatisfied")
            for required_field, allowed_field, rule in (
                    ("requires_corrective", "corrective_allowed",
                     "corrective_not_allowed"),
                    ("requires_retry", "retry_allowed", "retry_not_allowed"),
                    ("requires_fallback", "fallback_allowed",
                     "fallback_not_allowed")):
                if candidate[required_field] and not authority[allowed_field]:
                    expected_losing.add(rule)
            if authority["require_fresh"] and candidate["freshness"] != "fresh":
                expected_losing.add("freshness_required")
            ceiling_exceeded = (permission_ceiling is not None
                                and permission_order[candidate["permission"]]
                                > permission_order[permission_ceiling])
            if ceiling_exceeded:
                expected_losing.add("permission_ceiling_exceeded")
            has_ceiling_rule = "permission_ceiling_exceeded" in losing
            if ceiling_exceeded != has_ceiling_rule:
                raise EvidenceError(
                    "decision candidate contradicts permission ceiling")
            if ceiling_exceeded and candidate["eligible"]:
                raise EvidenceError(
                    "decision candidate ignores permission ceiling")
            if "permission_order_unknown" in losing:
                raise EvidenceError(
                    "decision candidate has impossible permission order")
            enforcement_rule = (
                f"permission_{enforcement}"
                if enforcement != "enforced"
                and not (enforcement == "unenforceable"
                         and unenforceable_authorized)
                else None)
            if enforcement_rule is not None:
                expected_losing.add(enforcement_rule)
            for rule in ("permission_unknown", "permission_unenforceable"):
                expected = rule == enforcement_rule
                present = rule in losing
                if expected != present:
                    raise EvidenceError(
                        "decision candidate contradicts permission enforcement")
            if enforcement_rule is not None and candidate["eligible"]:
                raise EvidenceError(
                    "decision candidate ignores permission enforcement")
            expected_losing.update(candidate["preflight_rules"])
            if losing != sorted(expected_losing):
                raise EvidenceError(
                    "decision candidate losing rules contradict visible inputs")
            if candidate["eligible"] != (not expected_losing):
                raise EvidenceError(
                    "decision candidate eligibility contradicts visible inputs")
        if selected is None:
            if resolution["winning_rule"] != "no_eligible_candidate":
                raise EvidenceError(
                    "empty decision resolution contradicts winning rule")
            if any(resolution.get(field) is not None
                   for field in ("backend", "provider", "model_targeted")):
                raise EvidenceError("empty decision resolution contains route identity")
            if any(candidate["eligible"] for candidate in value["candidates"]):
                raise EvidenceError("empty decision resolution ignores eligible candidate")
        else:
            if resolution["winning_rule"] != expected_rule:
                raise EvidenceError(
                    "decision resolution contradicts winning rule")
            winners = [candidate for candidate in value["candidates"]
                       if candidate["seat"] == selected and candidate["eligible"]]
            if len(winners) != 1:
                raise EvidenceError("decision resolution does not name one eligible candidate")
            winner = winners[0]
            if (resolution.get("backend") != winner["backend"]
                    or resolution.get("provider") != winner["provider"]
                    or resolution.get("model_targeted") != winner["model"]):
                raise EvidenceError("decision resolution identity contradicts candidate")
            expected = min(
                (candidate for candidate in value["candidates"]
                 if candidate["eligible"]),
                key=lambda candidate: (candidate["priority"], candidate["seat"]))
            if selected != expected["seat"]:
                raise EvidenceError(
                    "decision resolution does not name the deterministic winner")
            if ("effective_permission" in authority
                    and authority["effective_permission"] != winner["permission"]):
                raise EvidenceError(
                    "decision effective permission contradicts selected candidate")
        if "usage" in value:
            _validate_decision_usage(value["usage"])
        _reject_private_public_text(value)
    elif schema == "summon.liveness/v1":
        required = {"phase", "elapsed_ms", "expired", "counts"}
        optional = {"first_trusted_event_ms", "last_meaningful_event_ms",
                    "last_trusted_kind", "last_activity_kind"}
        if (not required.issubset(value) or set(value) - required - optional
                or not isinstance(value.get("phase"), str)
                or value.get("phase") not in {
                    "startup", "generation", "reconnect", "finalization",
                    "terminal", "cancelled", "timed_out"}
                or not isinstance(value.get("elapsed_ms"), int)
                or isinstance(value.get("elapsed_ms"), bool)
                or value.get("elapsed_ms") < 0
                or (value.get("expired") is not None
                    and not isinstance(value.get("expired"), str))
                or (value.get("expired") is not None
                    and value.get("expired") not in {
                        "overall_timeout", "startup_timeout",
                        "generation_idle_timeout", "finalization_timeout"})
                or not isinstance(value.get("counts"), dict)
                or any(value.get(field) is not None
                       and not isinstance(value.get(field), str)
                       for field in ("last_trusted_kind", "last_activity_kind"))
                or any(value.get(field) is not None
                       and (not isinstance(value.get(field), int)
                            or isinstance(value.get(field), bool)
                            or value.get(field) < 0)
                       for field in ("first_trusted_event_ms",
                                     "last_meaningful_event_ms"))
                or set(value.get("counts", {})) != {
                    "trusted", "meaningful", "ignored", "untrusted", "duplicates",
                    "reordered", "reconnects", "tools"}
                or value.get("last_trusted_kind") not in {None} | {
                    "transport_started", "stream_event", "output_text", "tool_activity",
                    "reconnect", "finalizing", "terminal", "cancelled"}
                or value.get("last_activity_kind") not in {None, "tool", "generation"}
                or not all(isinstance(item, int) and not isinstance(item, bool)
                           and item >= 0 for item in value["counts"].values())):
            raise EvidenceError("liveness evidence has malformed composites")


def _reject_private_public_text(value: Any) -> None:
    """Reject obvious paths and credentials from public decision evidence."""
    if isinstance(value, str):
        if (".." in value or re.search(
                r"(?i)(?:^|[\s('\"=\[,;:])[A-Z]:[\\/]", value)
                or re.search(
                    r"(?i)(?:file:\s*(?://)?[/\\]|"
                    r"(?:^|[\s('\"=\[])(?://[^/\s]|/[^/\s]|\\\\[^\\\s]))",
                    value)
                or _PUBLIC_SECRET.search(value)):
            raise EvidenceError("public evidence contains path-like or secret text")
    elif isinstance(value, list):
        for item in value:
            _reject_private_public_text(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_private_public_text(key)
            _reject_private_public_text(item)


def _validate_decision_usage(value: Any) -> None:
    if not isinstance(value, dict):
        raise EvidenceError("decision usage must be an object")
    state = value.get("state")
    common = {"state", "reason"}
    if state == "not_consulted":
        allowed = common
    elif state == "invalid":
        allowed = common | {"observations_considered"}
    elif state == "advisory_only":
        allowed = common | {"observations_considered", "freshness", "dimensions",
                            "comparability"}
    else:
        raise EvidenceError("decision usage state is invalid")
    if set(value) != allowed:
        raise EvidenceError("decision usage fields are invalid")
    _rule(value.get("reason"), "usage.reason")
    if "observations_considered" in value and (
            not isinstance(value["observations_considered"], int)
            or isinstance(value["observations_considered"], bool)
            or not 0 <= value["observations_considered"] <= 4096):
        raise EvidenceError("decision usage observation count is invalid")
    if state == "advisory_only":
        freshness = value.get("freshness")
        if (not isinstance(freshness, dict) or set(freshness) != {"fresh", "stale"}
                or not all(isinstance(item, int) and not isinstance(item, bool)
                           and item >= 0 for item in freshness.values())):
            raise EvidenceError("decision usage freshness is invalid")
        dimensions = value.get("dimensions")
        if (not isinstance(dimensions, list) or len(dimensions) > 32
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in dimensions)
                or dimensions != sorted(set(dimensions))):
            raise EvidenceError("decision usage dimensions are invalid")
        observations = value["observations_considered"]
        if sum(freshness.values()) != observations:
            raise EvidenceError(
                "decision usage freshness contradicts observation count")
        if len(dimensions) > observations:
            raise EvidenceError(
                "decision usage dimensions contradict observation count")
        if value.get("comparability") != "unverified_semantics":
            raise EvidenceError("decision usage comparability is invalid")
    elif isinstance(value, dict):
        for item in value.values():
            _reject_private_public_text(item)


def _reject_canonical_floats(value: Any) -> None:
    if isinstance(value, float):
        raise EvidenceError(
            "canonical evidence uses integers or decimal strings, not binary floats")
    if isinstance(value, list):
        for item in value:
            _reject_canonical_floats(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_canonical_floats(item)


def canonical_bytes(schema: str, value: Any) -> bytes:
    """Return canonical JSON bytes with a schema-bound preimage."""
    if schema not in KNOWN_SCHEMAS:
        raise EvidenceError(f"unknown evidence schema: {schema}")
    validate(value)
    _validate_schema_value(schema, value)
    _reject_canonical_floats(value)
    preimage = {"schema_version": schema, "value": value}
    return json.dumps(preimage, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(schema: str, value: Any) -> str:
    return hashlib.sha256(canonical_bytes(schema, value)).hexdigest()


def seal(schema: str, value: dict) -> dict:
    """Return a copy with its canonical digest; reject pre-existing ambiguity."""
    if not isinstance(value, dict):
        raise EvidenceError("sealed evidence must be an object")
    if "sha256" in value or "schema" in value or "schema_version" in value:
        raise EvidenceError("unsealed evidence must not contain seal fields")
    result = dict(value)
    result["schema"] = schema
    result["sha256"] = digest(schema, value)
    return result


def verify(sealed: dict) -> dict:
    """Verify and return the visible payload of a sealed evidence object."""
    if not isinstance(sealed, dict):
        raise EvidenceError("sealed evidence must be an object")
    schema = sealed.get("schema")
    claimed = sealed.get("sha256")
    if schema not in KNOWN_SCHEMAS or not isinstance(claimed, str) \
            or len(claimed) != 64:
        raise EvidenceError("sealed evidence has invalid seal fields")
    payload = {key: value for key, value in sealed.items()
               if key not in {"schema", "sha256"}}
    if digest(schema, payload) != claimed:
        raise EvidenceError("sealed evidence digest mismatch")
    return payload
