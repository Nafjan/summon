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

import hashlib
import json

SCHEMA = "summon.resume-capabilities/v1"
SCHEMA_V2 = "summon.resume-capabilities/v2"
LAUNCH_SCOPE_SCHEMA = "summon.resume-launch-scope/v1"
STEERING_MODE = "queued_for_resume"

# v2 is a contract for eligibility evidence, not a launch grant.  The pure
# registry never inspects a CLI, profile, environment, session handle, or
# provider.  Runtime adapters bind these facts later, under the R02 admission
# gate.  Keep this scope explicit so a matching registry digest cannot be
# mistaken for authenticated qualification.
V2_OPERATIONS = frozenset({"resume", "steer"})
V2_LAUNCH_PERMISSION = "not_granted"
V2_ADAPTER_VERSION_SCOPE = "summon-executor/3.4.0"
V2_EXTERNAL_CLI_VERSION_SCOPE = "not_declared"

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

_V2_CAPABILITY_FIELDS = frozenset({
    "schema", "registry_generation", "registry_digest", "operation",
    "backend", "transport", "adapter", "adapter_version_scope",
    "external_cli_version_scope",
    "resume_state", "resume_reason", "steering_mode", "handle_kind",
    "evidence_requirements", "qualification", "launch_permission",
})
_V2_EVIDENCE_FIELDS = frozenset({
    "exact_identity", "owner_fence", "fresh_handle", "provider_receipt",
})
_V2_EXPIRY_FIELDS = frozenset({"state", "expires_at"})
_V2_QUALIFICATION_FIELDS = frozenset({
    "provenance", "status", "executable_binding", "provider_receipt",
    "external_cli_version", "expiry",
})
_V2_QUALIFICATION = {
    # These are declarations about what is still unavailable at the pure
    # registry layer.  They are deliberately not provider observations.
    "provenance": "source_registry_only",
    "status": "unqualified",
    "executable_binding": "unavailable",
    "provider_receipt": "unavailable",
    "external_cli_version": "unavailable",
    "expiry": {"state": "unavailable", "expires_at": None},
}
_V2_ADAPTERS = {
    "subprocess": "summon-cli-subprocess",
    "acp": "summon-acp",
    "api": "summon-api",
}

_LAUNCH_SCOPE_FIELDS = frozenset({
    "schema", "registry_generation", "registry_digest", "adapter",
    "adapter_version", "external_cli_version",
})
_LAUNCH_SCOPE_REASONS = frozenset({
    "scope_match", "capability_malformed", "observation_malformed",
    "registry_scope_stale", "adapter_mismatch", "adapter_version_mismatch",
    "external_cli_version_mismatch",
})


def _v2_handle_kind(backend: str, transport: str, state: str) -> str:
    if state == RESUME_UNSUPPORTED:
        return "none"
    if backend == "agy":
        return "profile_continuation"
    return "provider_session"


def _v2_registry_material() -> list[dict[str, object]]:
    """Return canonical source declarations used for the v2 registry digest."""
    rows = []
    for (backend, transport), (state, reason) in sorted(_CAPABILITIES.items()):
        for operation in sorted(V2_OPERATIONS):
            rows.append({
                "schema": SCHEMA_V2,
                "operation": operation,
                "backend": backend,
                "transport": transport,
                "resume_state": state,
                "resume_reason": reason,
                "steering_mode": ("queued_for_resume"
                                  if state in {RESUME_CERTIFIED, RESUME_CANDIDATE}
                                  else "unsupported"),
                "handle_kind": _v2_handle_kind(backend, transport, state),
                "adapter": _V2_ADAPTERS[transport],
                "adapter_version_scope": V2_ADAPTER_VERSION_SCOPE,
                "external_cli_version_scope": V2_EXTERNAL_CLI_VERSION_SCOPE,
                "evidence_requirements": {
                    field: state != RESUME_UNSUPPORTED
                    for field in _V2_EVIDENCE_FIELDS
                },
                "qualification": {
                    **_V2_QUALIFICATION,
                    "expiry": dict(_V2_QUALIFICATION["expiry"]),
                },
                "launch_permission": V2_LAUNCH_PERMISSION,
            })
    return rows


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


REGISTRY_GENERATION = 1
REGISTRY_DIGEST = "sha256:" + hashlib.sha256(
    _canonical_bytes({"generation": REGISTRY_GENERATION,
                      "rows": _v2_registry_material()})
).hexdigest()


def resume_capability_v2(operation: object, cli: object,
                         transport: object) -> dict[str, object]:
    """Return the exact pure v2 capability contract for one operation.

    ``launch_permission`` is intentionally fixed to ``not_granted``.  A
    caller-supplied generation/digest can be compared with
    :func:`matches_registry_scope`, but that consistency check is not
    authentication or provider qualification.
    """
    normalized_operation = (operation.strip().lower()
                            if isinstance(operation, str) else "unknown")
    backend = _known(cli, _BACKENDS)
    transport_name = _known(transport, _TRANSPORTS)
    declaration = _CAPABILITIES.get((backend, transport_name))
    if normalized_operation not in V2_OPERATIONS:
        state, reason = RESUME_UNSUPPORTED, "unknown_operation"
        normalized_operation = "unknown"
    elif declaration is None:
        state, reason = RESUME_UNSUPPORTED, "unknown_backend_or_transport"
    else:
        state, reason = declaration
    adapter = _V2_ADAPTERS.get(transport_name, "unknown")
    steering_mode = ("queued_for_resume"
                     if state in {RESUME_CERTIFIED, RESUME_CANDIDATE}
                     else "unsupported")
    evidence = {
        "exact_identity": state != RESUME_UNSUPPORTED,
        "owner_fence": state != RESUME_UNSUPPORTED,
        "fresh_handle": state != RESUME_UNSUPPORTED,
        "provider_receipt": state != RESUME_UNSUPPORTED,
    }
    qualification = {
        **_V2_QUALIFICATION,
        "expiry": dict(_V2_QUALIFICATION["expiry"]),
    }
    return {
        "schema": SCHEMA_V2,
        "registry_generation": REGISTRY_GENERATION,
        "registry_digest": REGISTRY_DIGEST,
        "operation": normalized_operation,
        "backend": backend,
        "transport": transport_name,
        "adapter": adapter,
        "adapter_version_scope": V2_ADAPTER_VERSION_SCOPE if adapter != "unknown" else "none",
        "external_cli_version_scope": (V2_EXTERNAL_CLI_VERSION_SCOPE
                                       if adapter != "unknown" else "none"),
        "resume_state": state,
        "resume_reason": reason,
        "steering_mode": steering_mode,
        "handle_kind": _v2_handle_kind(backend, transport_name, state),
        "evidence_requirements": evidence,
        "qualification": qualification,
        "launch_permission": V2_LAUNCH_PERMISSION,
    }


def _is_exact_v2_shape(value: dict[str, object]) -> bool:
    """Reject malformed primitive/nested values before registry comparison."""
    if type(value.get("schema")) is not str:
        return False
    if value["schema"] != SCHEMA_V2:
        return False
    if type(value.get("registry_generation")) is not int:
        return False
    if type(value.get("registry_digest")) is not str:
        return False
    scalar_fields = {
        "operation", "backend", "transport", "adapter",
        "adapter_version_scope", "external_cli_version_scope",
        "resume_state", "resume_reason", "steering_mode", "handle_kind",
        "launch_permission",
    }
    if any(type(value.get(field)) is not str for field in scalar_fields):
        return False
    evidence = value.get("evidence_requirements")
    if not isinstance(evidence, dict) or set(evidence) != _V2_EVIDENCE_FIELDS:
        return False
    if any(type(evidence[field]) is not bool for field in _V2_EVIDENCE_FIELDS):
        return False
    qualification = value.get("qualification")
    if (not isinstance(qualification, dict)
            or set(qualification) != _V2_QUALIFICATION_FIELDS):
        return False
    for field in _V2_QUALIFICATION_FIELDS - {"expiry"}:
        if type(qualification[field]) is not str:
            return False
    expiry = qualification["expiry"]
    if not isinstance(expiry, dict) or set(expiry) != _V2_EXPIRY_FIELDS:
        return False
    if type(expiry["state"]) is not str or expiry["expires_at"] is not None:
        return False
    return True


def is_exact_capability_v2(value: object) -> bool:
    """Validate one v2 row against the canonical pure registry."""
    if not isinstance(value, dict) or set(value) != _V2_CAPABILITY_FIELDS:
        return False
    if not _is_exact_v2_shape(value):
        return False
    if value.get("operation") not in V2_OPERATIONS:
        return False
    if (value.get("backend"), value.get("transport")) not in _CAPABILITIES:
        return False
    expected = resume_capability_v2(value.get("operation"), value.get("backend"),
                                    value.get("transport"))
    return value == expected


def serialize_capability_v2(value: object) -> bytes:
    """Serialize an exact v2 row with deterministic UTF-8 JSON bytes."""
    if not is_exact_capability_v2(value):
        raise ValueError("capability is not an exact v2 registry row")
    return _canonical_bytes(value)


def matches_registry_scope(value: object, observation: object) -> bool:
    """Compare an observation to a row; this is consistency, not authority.

    Runtime code must authenticate the observation and apply the separate
    launch/owner/model/spend gates.  A caller cannot turn this boolean into a
    provider permission or a per-delivery acknowledgement.
    """
    if not is_exact_capability_v2(value) or not isinstance(observation, dict):
        return False
    scope_fields = frozenset({
        "schema", "registry_generation", "registry_digest", "adapter",
        "adapter_version_scope", "external_cli_version_scope",
    })
    if set(observation) != scope_fields:
        return False
    if (type(observation["schema"]) is not str
            or type(observation["registry_generation"]) is not int
            or type(observation["registry_digest"]) is not str
            or any(type(observation[field]) is not str for field in scope_fields
                   - {"registry_generation"})):
        return False
    expected = {
        "schema": value["schema"],
        "registry_generation": value["registry_generation"],
        "registry_digest": value["registry_digest"],
        "adapter": value["adapter"],
        "adapter_version_scope": value["adapter_version_scope"],
        "external_cli_version_scope": value["external_cli_version_scope"],
    }
    return observation == expected


def compare_launch_scope(capability: object, observation: object) -> dict[str, object]:
    """Compare v2 launch facts before any continuation contact.

    A ``match`` is only a consistency result. It never grants launch,
    authentication, model, account, spend, or provider authority. The caller
    must retain the v1 historical projection until R02 migration is complete
    and apply separate owner/model/spend gates.
    """
    result = {
        "schema": LAUNCH_SCOPE_SCHEMA,
        "status": "refused",
        "reason": "capability_malformed",
        "contact_allowed": False,
        "provider_contacted": False,
        "launch_permission": V2_LAUNCH_PERMISSION,
    }
    if not is_exact_capability_v2(capability):
        return result
    # An exact registry row can still be explicitly unsupported.  Treating its
    # matching scope as a successful comparison would let legacy callers
    # mistake a known-unavailable route for a continuation candidate.
    if capability.get("resume_state") == RESUME_UNSUPPORTED:
        result["reason"] = "capability_unsupported"
        return result
    if not isinstance(observation, dict) or set(observation) != _LAUNCH_SCOPE_FIELDS:
        result["reason"] = "observation_malformed"
        return result
    if (observation.get("schema") != LAUNCH_SCOPE_SCHEMA
            or type(observation.get("registry_generation")) is not int
            or type(observation.get("registry_digest")) is not str
            or type(observation.get("adapter")) is not str
            or type(observation.get("adapter_version")) is not str
            or type(observation.get("external_cli_version")) is not str):
        result["reason"] = "observation_malformed"
        return result
    if (observation["registry_generation"] != capability["registry_generation"]
            or observation["registry_digest"] != REGISTRY_DIGEST):
        result["reason"] = "registry_scope_stale"
        return result
    if observation["adapter"] != capability["adapter"]:
        result["reason"] = "adapter_mismatch"
        return result
    if observation["adapter_version"] != capability["adapter_version_scope"]:
        result["reason"] = "adapter_version_mismatch"
        return result
    if observation["external_cli_version"] != capability["external_cli_version_scope"]:
        result["reason"] = "external_cli_version_mismatch"
        return result
    result["status"] = "match"
    result["reason"] = "scope_match"
    return result


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
    "SCHEMA", "SCHEMA_V2", "LAUNCH_SCOPE_SCHEMA", "STEERING_MODE", "V2_OPERATIONS",
    "V2_LAUNCH_PERMISSION", "V2_ADAPTER_VERSION_SCOPE",
    "V2_EXTERNAL_CLI_VERSION_SCOPE", "REGISTRY_GENERATION",
    "REGISTRY_DIGEST", "RESUME_CERTIFIED", "RESUME_CANDIDATE",
    "RESUME_UNSUPPORTED", "resume_capability", "resume_capability_v2",
    "governed_resume_supported", "is_exact_capability", "is_exact_capability_v2",
    "serialize_capability_v2", "matches_registry_scope", "compare_launch_scope",
]
