"""Pure, provider-inert resume and steering capability registry.

This module describes what Summon is allowed to *claim* before any backend is
launched.  It is intentionally not an adapter registry: it performs no PATH,
profile, environment, filesystem, or provider inspection.  A caller must use
the exact capability object to decide whether a persisted resume handle may be
considered; a plausible session-looking string is never capability evidence.

``certified`` means the lane is approved for governed resume by this registry,
not that a particular handle has been authenticated.  ``candidate`` means the
argv and/or stream seam exists but a provider-specific receipt gate is still
required.  ``unsupported`` is fail-closed.
"""

from __future__ import annotations


SCHEMA = "summon.resume-capabilities/v1"
STEERING_MODE = "queued_for_resume"

RESUME_CERTIFIED = "certified"
RESUME_CANDIDATE = "candidate"
RESUME_UNSUPPORTED = "unsupported"

_CAPABILITY_FIELDS = frozenset({
    "schema", "backend", "transport", "resume_state", "resume_reason",
    "steering_mode", "live_steering_acknowledged",
})

# Every entry is declarative, bounded, and safe to project.  In particular it
# contains no session handle, profile location, account information, or prompt.
_CAPABILITIES = {
    ("claude", "subprocess"): (
        RESUME_CERTIFIED, "claude_subprocess_governed_lane"),
    ("codex", "subprocess"): (
        RESUME_CANDIDATE, "provider_receipt_required"),
    ("cursor-agent", "subprocess"): (
        RESUME_CANDIDATE, "provider_receipt_required"),
    ("opencode", "subprocess"): (
        RESUME_CANDIDATE, "provider_receipt_required"),
    # ZCode emits a syntactically validated sess_ handle in terminal JSON and
    # accepts it through its headless resume seam, but an installed end-to-end
    # smoke has not yet certified continuity across versions.
    ("zcode", "subprocess"): (
        RESUME_CANDIDATE, "installed_resume_smoke_required"),
    ("agy", "subprocess"): (
        RESUME_UNSUPPORTED, "agy_profile_continuity_unreliable"),
    ("gemini", "subprocess"): (
        RESUME_UNSUPPORTED, "stable_session_resume_unavailable"),
    ("kimi", "subprocess"): (
        RESUME_UNSUPPORTED, "stable_session_id_unavailable"),
    ("cursor-agent", "acp"): (
        RESUME_UNSUPPORTED, "acp_session_namespace_not_resumable"),
    ("gemini", "acp"): (
        RESUME_UNSUPPORTED, "acp_session_namespace_not_resumable"),
    ("kimi", "acp"): (
        RESUME_UNSUPPORTED, "acp_session_namespace_not_resumable"),
    ("openai-compat", "api"): (
        RESUME_UNSUPPORTED, "stateless_api_transport"),
    ("arkcli", "api"): (
        RESUME_UNSUPPORTED, "response_id_not_captured"),
}


def _known(value: object, allowed: frozenset[str]) -> str:
    """Return a bounded canonical identifier, else the non-sensitive sentinel."""
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    return normalized if normalized in allowed else "unknown"


_BACKENDS = frozenset(backend for backend, _transport in _CAPABILITIES)
_TRANSPORTS = frozenset(transport for _backend, transport in _CAPABILITIES)


def resume_capability(cli: object, transport: object) -> dict[str, object]:
    """Return the exact public capability object for one backend transport.

    Unknown values never echo caller-controlled text.  They resolve to a fixed
    unsupported row, which makes this safe for status/preflight projections.
    """
    backend = _known(cli, _BACKENDS)
    transport_name = _known(transport, _TRANSPORTS)
    state, reason = _CAPABILITIES.get(
        (backend, transport_name),
        (RESUME_UNSUPPORTED, "unknown_backend_or_transport"),
    )
    return {
        "schema": SCHEMA,
        "backend": backend,
        "transport": transport_name,
        "resume_state": state,
        "resume_reason": reason,
        "steering_mode": STEERING_MODE,
        "live_steering_acknowledged": False,
    }


def governed_resume_supported(cli: object, transport: object) -> bool:
    """Whether this exact backend transport is certified for governed resume."""
    return resume_capability(cli, transport)["resume_state"] == RESUME_CERTIFIED


def is_exact_capability(value: object) -> bool:
    """Validate the fixed public schema without accepting additive fields."""
    if not isinstance(value, dict) or set(value) != _CAPABILITY_FIELDS:
        return False
    expected = resume_capability(value.get("backend"), value.get("transport"))
    return value == expected


__all__ = [
    "SCHEMA", "STEERING_MODE", "RESUME_CERTIFIED", "RESUME_CANDIDATE",
    "RESUME_UNSUPPORTED", "resume_capability", "governed_resume_supported",
    "is_exact_capability",
]
