"""Strict, provider-free context-policy values.

The policy is a small typed value, not a prompt instruction.  It selects a
context compiler for future admissions and is therefore validated before any
workspace or chat owner is consumed.  Historical workspace/v1 data is handled
by :func:`derived_legacy` and is never presented as an operator-selected
policy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping


SCHEMA = "summon.context-policy/v1"
MODES = frozenset({"off", "safe"})
IMPLEMENTATIONS = {
    "off": "whole-message-v1",
    "safe": "typed-context-compiler-v1",
}
ORIGINS = frozenset({"operator", "derived-compatibility"})
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")


class ContextPolicyError(ValueError):
    """Malformed or unsupported policy; no provider or workspace is touched."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"context policy refused ({kind})")


def _fail(kind: str) -> None:
    raise ContextPolicyError(kind)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("policy_json")


def validate(value: Any, *, previous: Mapping[str, Any] | None = None,
             supported_modes: set[str] | frozenset[str] = MODES) -> dict[str, Any]:
    """Validate and detach one policy.

    ``previous`` is optional and is used only for monotonic revision checks.
    Replaying the exact same policy is idempotent; a changed policy must move
    to a strictly newer revision.  No caller-controlled prose is accepted.
    """
    if type(value) is not dict:
        _fail("policy_shape")
    required = {"schema", "policy_id", "mode", "revision", "implementation", "origin"}
    if set(value) != required:
        _fail("policy_fields")
    if value["schema"] != SCHEMA:
        _fail("policy_schema")
    policy_id = value["policy_id"]
    if type(policy_id) is not str or not _ID.fullmatch(policy_id):
        _fail("policy_id")
    mode = value["mode"]
    if type(mode) is not str or mode not in set(supported_modes) or mode not in MODES:
        _fail("policy_mode_unsupported")
    origin = value["origin"]
    if type(origin) is not str or origin not in ORIGINS:
        _fail("policy_origin")
    if origin == "derived-compatibility":
        _fail("derived_policy_requires_compatibility_helper")
    revision = value["revision"]
    if type(revision) is not int or isinstance(revision, bool) or not 1 <= revision <= 2**31 - 1:
        _fail("policy_revision")
    implementation = value["implementation"]
    if implementation != IMPLEMENTATIONS[mode]:
        _fail("policy_implementation_mismatch")
    checked = copy.deepcopy(value)
    _canonical(checked)
    if previous is not None:
        old = validate(previous, supported_modes=supported_modes)
        if checked != old and revision <= old["revision"]:
            _fail("policy_revision_not_monotonic")
    return checked


def make(mode: str, *, revision: int = 1, policy_id: str | None = None) -> dict[str, Any]:
    """Create an operator-selected policy through the same strict validator."""
    if policy_id is None:
        policy_id = f"context-{mode}"
    value = {"schema": SCHEMA, "policy_id": policy_id, "mode": mode,
             "revision": revision, "implementation": IMPLEMENTATIONS.get(mode),
             "origin": "operator"}
    return validate(value)


def derived_legacy() -> dict[str, Any]:
    """Describe historical v1 behavior without claiming operator selection."""
    return {
        "schema": SCHEMA,
        "policy_id": "legacy-v1",
        "mode": "off",
        "revision": 0,
        "implementation": IMPLEMENTATIONS["off"],
        "origin": "derived-compatibility",
    }


def digest(value: Mapping[str, Any]) -> str:
    """Return a policy identity digest after validating an operator policy."""
    checked = validate(dict(value))
    return hashlib.sha256(_canonical(checked)).hexdigest()


def public(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only non-sensitive policy facts for status surfaces."""
    row = dict(value)
    if row.get("origin") == "derived-compatibility":
        expected = derived_legacy()
        if row != expected:
            _fail("derived_policy_invalid")
        return {"schema": SCHEMA, "mode": "off", "revision": 0,
                "origin": "derived-compatibility"}
    checked = validate(row)
    return {"schema": SCHEMA, "mode": checked["mode"],
            "revision": checked["revision"], "origin": "operator"}


def validate_public(value: Any) -> dict[str, Any]:
    """Validate the intentionally smaller public policy projection."""
    if type(value) is not dict or set(value) != {"schema", "mode", "revision", "origin"}:
        _fail("public_policy_fields")
    if value["schema"] != SCHEMA or value["mode"] not in MODES:
        _fail("public_policy_shape")
    if type(value["revision"]) is not int or isinstance(value["revision"], bool) or value["revision"] < 0:
        _fail("public_policy_revision")
    if value["origin"] not in ORIGINS:
        _fail("public_policy_origin")
    if value["origin"] == "derived-compatibility":
        if value != {"schema": SCHEMA, "mode": "off", "revision": 0,
                     "origin": "derived-compatibility"}:
            _fail("public_policy_derived")
    elif value["revision"] < 1:
        _fail("public_policy_revision")
    return copy.deepcopy(value)


__all__ = ["SCHEMA", "MODES", "IMPLEMENTATIONS", "ContextPolicyError",
           "validate", "make", "derived_legacy", "digest", "public", "validate_public"]
