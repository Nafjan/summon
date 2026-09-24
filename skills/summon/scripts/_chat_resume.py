"""Provider-inert chat continuation refusal contract.

This small module owns the exact public refusal shape so the runtime, journal
projection, CLI, and browser cannot drift on reason codes or JSON types.  It
does not inspect a provider, profile, executable, workspace, or session.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from _resume_capabilities import is_exact_capability


SCHEMA = "summon.chat-resume-refusal/v1"
REASON_CODES = frozenset({
    "resume_candidate", "resume_unsupported", "continuation_identity_missing",
    "continuation_identity_incompatible", "resume_capability_changed",
    "resume_launch_observation_missing", "resume_launch_qualification_missing",
    "resume_launch_qualification_invalid", "resume_launch_qualification_expired",
    "resume_launch_qualification_revoked",
})
REFUSAL_KEYS = frozenset({
    "schema", "status", "reason_code", "capability", "execution_status",
    "attempt_status", "attempts", "provider_contacted", "contact_scope",
    "action_offer",
})
_CAPABILITY_KEYS = frozenset({
    "schema", "backend", "transport", "resume_state", "resume_reason",
    "steering_mode", "live_steering_acknowledged",
})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DISPLAY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+@#-]{0,127}$")


def is_stored_capability(value: object) -> bool:
    """Validate historical v1 shape without consulting today's registry."""
    if not isinstance(value, Mapping) or set(value) != _CAPABILITY_KEYS:
        return False
    if value.get("schema") != "summon.resume-capabilities/v1":
        return False
    if (not isinstance(value.get("backend"), str)
            or not _ID_RE.fullmatch(value["backend"])
            or not isinstance(value.get("transport"), str)
            or not _ID_RE.fullmatch(value["transport"])
            or not isinstance(value.get("resume_state"), str)
            or value.get("resume_state") not in {"certified", "candidate", "unsupported"}
            or not isinstance(value.get("resume_reason"), str)
            or not _DISPLAY_RE.fullmatch(value["resume_reason"])
            or value.get("steering_mode") != "queued_for_resume"
            or type(value.get("live_steering_acknowledged")) is not bool
            or value.get("live_steering_acknowledged") is not False):
        return False
    return True


def build(reason_code: str, capability: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact refusal object for a current canonical capability row."""
    if (not isinstance(reason_code, str) or reason_code not in REASON_CODES
            or not isinstance(capability, Mapping)
            or not is_exact_capability(dict(capability))):
        raise ValueError("invalid chat resume refusal")
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "reason_code": reason_code,
        "capability": dict(capability),
        "execution_status": "not_run",
        "attempt_status": "not_run",
        "attempts": 0,
        "provider_contacted": False,
        "contact_scope": "this_invocation_before_dispatch",
        "action_offer": {"kind": "fork", "automatic": False},
    }


def is_valid(value: object, *, historical: bool = True) -> bool:
    """Validate exact keys/types; historical rows need not match today's registry."""
    if not isinstance(value, Mapping) or set(value) != REFUSAL_KEYS:
        return False
    if (value.get("schema") != SCHEMA or value.get("status") != "blocked"
            or not isinstance(value.get("reason_code"), str)
            or value.get("reason_code") not in REASON_CODES
            or value.get("execution_status") != "not_run"
            or value.get("attempt_status") != "not_run"
            or type(value.get("attempts")) is not int or value.get("attempts") != 0
            or type(value.get("provider_contacted")) is not bool
            or value.get("provider_contacted") is not False
            or value.get("contact_scope") != "this_invocation_before_dispatch"):
        return False
    action = value.get("action_offer")
    if (not isinstance(action, Mapping) or set(action) != {"kind", "automatic"}
            or not isinstance(action.get("kind"), str)
            or action.get("kind") != "fork"
            or type(action.get("automatic")) is not bool
            or action.get("automatic") is not False):
        return False
    capability = value.get("capability")
    return (is_stored_capability(capability)
            if historical else is_exact_capability(dict(capability))
            if isinstance(capability, Mapping) else False)


__all__ = ["SCHEMA", "REASON_CODES", "REFUSAL_KEYS", "build",
           "is_stored_capability", "is_valid"]
