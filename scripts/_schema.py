"""Structured-output support (``--json-schema``).

Two pieces every swarm orchestrator otherwise reinvents:
- :func:`extract_json` — find the agent's final JSON value in free-form text
  (exact ``raw_decode`` scanning, not an outermost-braces heuristic).
- :func:`validate` — a dependency-free validator for the JSON-Schema subset
  that swarm outputs actually use. NOT full JSON Schema; unsupported keywords
  are ignored deliberately (documented in SKILL.md).
"""

from __future__ import annotations

import json
import re

# Schema keywords the validator understands. Anything else is ignored — better
# a documented subset than a silent half-implementation of the full spec.
SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "items", "enum", "const",
    "additionalProperties", "minItems", "maxItems", "minLength", "maxLength",
    "minimum", "maximum", "pattern",
})

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def extract_json(text: str):
    """Return ``(value, None)`` for the LAST complete top-level JSON object or
    array in ``text``, or ``(None, reason)`` when none parses.

    Scans forward with ``JSONDecoder.raw_decode`` and jumps over each parsed
    span, so nested values inside an already-consumed object are never
    mistaken for the final answer, and code fences / surrounding prose are
    ignored naturally.
    """
    if not text:
        return None, "empty text"
    dec = json.JSONDecoder()
    last = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            try:
                obj, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            last = obj
            i = end
        else:
            i += 1
    if last is None:
        return None, "no complete JSON object/array found in the agent's result"
    return last, None


def validate(instance, schema: dict, path: str = "$") -> list:
    """Validate ``instance`` against the supported schema subset.

    Returns a list of human-readable error strings (empty = valid). Never
    raises on a weird schema — a malformed schema node yields an error string
    so the caller sees the problem instead of a crash.
    """
    errors: list = []
    if not isinstance(schema, dict):
        return [f"{path}: schema node is not an object"]

    stype = schema.get("type")
    if stype is not None:
        types = stype if isinstance(stype, list) else [stype]
        checks = [_TYPE_CHECKS.get(t) for t in types]
        if any(c is None for c in checks):
            errors.append(f"{path}: schema has unknown type {stype!r}")
        elif not any(c(instance) for c in checks):
            errors.append(f"{path}: expected type {stype}, got {type(instance).__name__}")
            return errors  # type mismatch makes deeper checks meaningless

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list):
            errors.append(f"{path}: schema enum is not a list")
        elif instance not in enum:
            errors.append(f"{path}: {instance!r} not in enum {enum!r}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: string longer than maxLength {schema['maxLength']}")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], instance):
                    errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
            except re.error as e:
                errors.append(f"{path}: schema pattern invalid ({e})")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            errors.append(f"{path}: schema properties is not an object")
            props = {}
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required property {req!r}")
        for key, sub in props.items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: unexpected properties {sorted(extra)!r}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, item in enumerate(instance):
                errors += validate(item, items, f"{path}[{idx}]")

    return errors


def attach_parsed(response: dict, schema: dict) -> None:
    """Extract + validate the agent's final JSON against ``schema`` and attach
    ``parsed`` / ``parse_ok`` / ``parse_errors`` to the envelope in place."""
    value, why = extract_json(response.get("result") or "")
    if value is None:
        response["parsed"] = None
        response["parse_ok"] = False
        response["parse_errors"] = [why]
        return
    errs = validate(value, schema)
    response["parsed"] = value
    response["parse_ok"] = not errs
    response["parse_errors"] = errs


def correction_prompt(schema: dict, errors: list) -> str:
    """The one-shot corrective follow-up sent on an invalid payload."""
    return (
        "Your previous reply's JSON did not satisfy the required schema. "
        f"Validation errors: {'; '.join(errors[:10])}\n\n"
        "Reply with ONLY the corrected JSON value (no prose before it) that "
        f"satisfies this JSON schema:\n{json.dumps(schema)}\n\n"
        "Then end with your exact 'Final report' block as usual."
    )
