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


KNOWN_SCHEMAS = {
    "summon.evidence/v1",
    "summon.decision/v1",
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


def loads(data: str | bytes, *, max_bytes: int = 1 << 20) -> Any:
    """Parse bounded UTF-8 JSON with duplicate/non-finite rejection."""
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
    validate(value)
    return value


def validate(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    """Validate a JSON-compatible value against global resource bounds."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_ITEMS:
        raise EvidenceError(f"evidence exceeds {MAX_ITEMS} values")
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
            validate(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError("object keys must be strings")
            validate(key, depth=depth + 1, counter=counter)
            validate(item, depth=depth + 1, counter=counter)
        return
    raise EvidenceError(f"unsupported evidence type: {type(value).__name__}")


def _validate_schema_value(schema: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise EvidenceError(f"{schema} evidence must be an object")
    if schema == "summon.decision/v1":
        required = {"provider_contacted", "request", "resolution", "authority",
                    "candidates", "unknowns", "digests"}
        missing = sorted(required - set(value))
        if missing:
            raise EvidenceError(
                "summon.decision/v1 is missing fields: " + ", ".join(missing))
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
        if ({"permission_ceiling", "spend_authorized", "enforcement"}
                - set(authority) or set(authority) - {
                    "permission_ceiling", "spend_authorized", "enforcement",
                    "effective_permission", "strict_roster", "credit", "payg"}):
            raise EvidenceError("decision authority has malformed fields")
        if set(digests) != {"roster", "policy", "project", "approval"}:
            raise EvidenceError("decision digests have malformed fields")
        if bool(request.get("agent")) == bool(request.get("lane")):
            raise EvidenceError("decision request must name exactly one agent or lane")
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
            if (not isinstance(precedence, list) or not precedence
                    or len(precedence) > 16
                    or not all(isinstance(item, str)
                               and item in {"exact_agent", "approved_role",
                                            "approved_lane"}
                               for item in precedence)):
                raise EvidenceError("decision precedence is invalid")
        if not 1 <= len(value["candidates"]) <= 128:
            raise EvidenceError("decision requires 1..128 candidates")
        seats = []
        for candidate in value["candidates"]:
            if not isinstance(candidate, dict) or set(candidate) != {
                    "seat", "backend", "provider", "model", "permission", "eligible",
                    "losing_rules", "priority"}:
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
                               for item in candidate["losing_rules"])):
                raise EvidenceError("decision candidate has invalid values")
            if candidate["eligible"] == bool(candidate["losing_rules"]):
                raise EvidenceError("decision candidate eligibility contradicts losing rules")
        if len(seats) != len(set(seats)):
            raise EvidenceError("decision candidate seats must be unique")
        if (not isinstance(value["unknowns"], list)
                or not all(isinstance(item, str) and _RULE.fullmatch(item)
                           for item in value["unknowns"])):
            raise EvidenceError("decision unknowns have invalid values")
        for item in digests.values():
            if item is not None and (not isinstance(item, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", item)):
                raise EvidenceError("decision digest reference is invalid")
        if (not isinstance(authority.get("spend_authorized"), bool)
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
            if (not isinstance(item, dict) or set(item) != {"authorized", "source"}
                    or not isinstance(item.get("authorized"), bool)):
                raise EvidenceError(f"decision authority.{field} is malformed")
            _public_string(item.get("source"), f"authority.{field}.source")
        selected = resolution.get("seat")
        if selected is None:
            if any(resolution.get(field) is not None
                   for field in ("backend", "provider", "model_targeted")):
                raise EvidenceError("empty decision resolution contains route identity")
            if any(candidate["eligible"] for candidate in value["candidates"]):
                raise EvidenceError("empty decision resolution ignores eligible candidate")
        else:
            winners = [candidate for candidate in value["candidates"]
                       if candidate["seat"] == selected and candidate["eligible"]]
            if len(winners) != 1:
                raise EvidenceError("decision resolution does not name one eligible candidate")
            winner = winners[0]
            if (resolution.get("backend") != winner["backend"]
                    or resolution.get("provider") != winner["provider"]
                    or resolution.get("model_targeted") != winner["model"]):
                raise EvidenceError("decision resolution identity contradicts candidate")
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
        if (".." in value or re.search(r"(?i)(?:^|\s)[A-Z]:[\\/]", value)
                or value.startswith(("/", "\\\\"))
                or _PUBLIC_SECRET.search(value)):
            raise EvidenceError("public evidence contains path-like or secret text")
    elif isinstance(value, list):
        for item in value:
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
                           for item in dimensions)):
            raise EvidenceError("decision usage dimensions are invalid")
        _rule(value.get("comparability"), "usage.comparability")
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
