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


_EXTRACT_MAX = 2_000_000    # chars scanned; agent JSON payloads are far smaller
_EXTRACT_MAX_ATTEMPTS = 5000  # failed decode attempts before giving up
_OPENER = re.compile(r"[{\[]")


def extract_json(text: str):
    """Return ``(value, None)`` for the LAST complete top-level JSON object or
    array in ``text``, or ``(None, reason)`` when none parses.

    Finds each ``{``/``[`` with a single regex scan and attempts ``raw_decode``
    there, jumping past whatever it decodes. Two guards keep pathological input
    cheap (a 1 MB string of ``{`` otherwise costs 1 M raise/catch cycles — the
    expensive part is the exceptions, not the scan): the input is capped to
    ``_EXTRACT_MAX`` chars from the END (where the answer is), and FAILED decode
    attempts are capped at ``_EXTRACT_MAX_ATTEMPTS`` — real agent output has a
    handful of top-level openers, never thousands.
    """
    if not text:
        return None, "empty text"
    if len(text) > _EXTRACT_MAX:
        text = text[-_EXTRACT_MAX:]
    dec = json.JSONDecoder()
    last = None
    fails = 0
    i, n = 0, len(text)
    while i < n:
        m = _OPENER.search(text, i)
        if not m:
            break
        i = m.start()
        try:
            obj, end = dec.raw_decode(text, i)
            last = obj
            i = end
        except ValueError:
            fails += 1
            if fails > _EXTRACT_MAX_ATTEMPTS:
                if last is not None:
                    break  # already have a candidate; stop burning cycles
                return None, ("no JSON object/array decoded in the first "
                              f"{_EXTRACT_MAX_ATTEMPTS} openers (input looks malformed)")
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
        if not all(isinstance(t, str) for t in types):
            # A non-string type member (e.g. {"type": [{}]}) is a malformed
            # schema. Report it — never let it reach _TYPE_CHECKS.get (which
            # would raise TypeError on an unhashable dict/list member).
            errors.append(f"{path}: schema type members must be strings, got {stype!r}")
        else:
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

    # Numeric-bound helpers: a malformed schema (e.g. minLength: "3") must yield
    # a validation error, never a TypeError out of this function.
    def _num(key):
        if key not in schema:
            return None, False
        v = schema[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"{path}: schema {key} is not a number ({v!r})")
            return None, True
        return v, False

    if isinstance(instance, str):
        lo, bad = _num("minLength")
        if lo is not None and len(instance) < lo:
            errors.append(f"{path}: string shorter than minLength {lo}")
        hi, bad = _num("maxLength")
        if hi is not None and len(instance) > hi:
            errors.append(f"{path}: string longer than maxLength {hi}")
        if "pattern" in schema:
            pat = schema["pattern"]
            if not isinstance(pat, str):
                errors.append(f"{path}: schema pattern is not a string")
            else:
                try:
                    if not re.search(pat, instance):
                        errors.append(f"{path}: does not match pattern {pat!r}")
                except re.error as e:
                    errors.append(f"{path}: schema pattern invalid ({e})")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        lo, _ = _num("minimum")
        if lo is not None and instance < lo:
            errors.append(f"{path}: {instance} < minimum {lo}")
        hi, _ = _num("maximum")
        if hi is not None and instance > hi:
            errors.append(f"{path}: {instance} > maximum {hi}")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            errors.append(f"{path}: schema properties is not an object")
            props = {}
        required = schema.get("required", [])
        if not isinstance(required, list):
            errors.append(f"{path}: schema required is not a list")
            required = []
        for req in required:
            if not isinstance(req, str):
                # {"required": [[]]} — a non-string member would raise TypeError
                # on `req not in instance` (unhashable). Report, don't crash.
                errors.append(f"{path}: schema required members must be strings, got {req!r}")
            elif req not in instance:
                errors.append(f"{path}: missing required property {req!r}")
        for key, sub in props.items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: unexpected properties {sorted(extra)!r}")

    if isinstance(instance, list):
        lo, _ = _num("minItems")
        if lo is not None and len(instance) < lo:
            errors.append(f"{path}: fewer than minItems {lo}")
        hi, _ = _num("maxItems")
        if hi is not None and len(instance) > hi:
            errors.append(f"{path}: more than maxItems {hi}")
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, item in enumerate(instance):
                errors += validate(item, items, f"{path}[{idx}]")

    return errors


def unsupported_keywords(schema, path: str = "$") -> list:
    """Walk ``schema`` and collect keywords the validator does NOT enforce, so a
    caller isn't misled by ``parse_ok: true`` on a schema using ``oneOf`` /
    ``$ref`` / ``format`` / etc. that were silently ignored. Returns ``[(path,
    keyword), ...]``."""
    found: list = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k not in SUPPORTED_KEYWORDS:
                found.append((path, k))
            if k == "properties" and isinstance(v, dict):
                for pk, pv in v.items():
                    found += unsupported_keywords(pv, f"{path}.{pk}")
            elif k == "items":
                found += unsupported_keywords(v, f"{path}[]")
    return found


def attach_parsed(response: dict, schema: dict) -> None:
    """Extract + validate the agent's final JSON against ``schema`` and attach
    ``parsed`` / ``parse_ok`` / ``parse_errors`` to the envelope in place.

    Any schema keyword outside :data:`SUPPORTED_KEYWORDS` is surfaced in
    ``parse_warnings`` (it was NOT enforced) — never silently ignored.
    """
    warns = [f"{p}: unsupported schema keyword {k!r} (not enforced)"
             for p, k in unsupported_keywords(schema)]
    if warns:
        response["parse_warnings"] = warns
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
