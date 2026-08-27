"""Provider-inert fleet activation contracts.

This module freezes the inputs that a future fleet launcher must authenticate before
contacting a provider.  It intentionally cannot launch, reserve, approve, or turn its
digest-sealed output into authority.  The private dispatch ledger remains the only place
where a later milestone may bind this evidence to a single-use claim.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
import os
import re
import tomllib
from typing import Any


SCHEMA = "summon.fleet-activation/v1"
MAX_CANONICAL_BYTES = 256 * 1024
MAX_ITEMS = 4096
MAX_DEPTH = 16
MAX_TEXT = 32 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_ID = re.compile(r"^[0-9a-f]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}$")

# These builders are currently pure.  Kimi, OpenCode, and AGY create profiles or
# otherwise mutate local state while preparing argv, so they remain ineligible until
# preparation is split into a pure plan and an explicit materialization step.
PURE_SUBPROCESS_BACKENDS = frozenset({"claude", "codex", "cursor-agent"})
_BILLING_SOURCES = frozenset({
    "codex_key_policy", "anthropic_key_present", "plan_dependent_model",
    "anthropic_key_absent", "cursor_key_policy", "gemini_key_policy",
    "unsupported_backend_policy", "custom_provider_configuration",
    "vertex_or_adc_configuration",
})

_PRIVATE_INVOCATION_FIELDS = frozenset({
    "prompt", "cwd", "system_context", "agent_file", "resume_id",
    "resume_profile", "extra_args", "base_url", "api_key_env",
    "agy_account_sha256", "profile", "profile_env", "profile_command",
    "openrouter_options", "worktree", "read_roots", "attempt_id",
    "parent_attempt_id",
})

_INVOCATION_FIELDS = frozenset({
    "cli", "prompt", "cwd", "system_context", "agent_file", "permission",
    "transport", "model", "model_source", "model_exact_required",
    "model_exact_source", "effort", "resume_id", "resume_profile", "extra_args",
    "base_url", "api_key_env", "allow_payg", "agy_account_sha256",
    "agy_account_checked", "permission_forced", "profile", "profile_env",
    "profile_command", "openrouter_options", "worktree", "isolated_lane",
    "allow_tool_credentials", "read_roots", "output_contract", "attempt_id",
    "attempt_kind", "attempt_ordinal", "parent_attempt_id",
})

_BINDING_HASHES = frozenset({
    "approval_id", "fleet_sha256", "lane_sha256", "catalog_sha256",
    "project_sha256", "prompt_sha256", "request_identity_sha256",
})


class FleetActivationError(ValueError):
    """Typed structural refusal from the provider-inert activation compiler."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bounded(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> Any:
    """Return a JSON-safe canonical copy while rejecting surprising object graphs."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_ITEMS or depth > MAX_DEPTH:
        raise FleetActivationError(
            "fleet_activation_invalid", "activation input exceeds structural bounds")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise FleetActivationError(
                "fleet_activation_invalid", "activation text exceeds bounds")
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth=depth + 1, counter=counter) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) and len(key) <= 256 for key in value):
            raise FleetActivationError(
                "fleet_activation_invalid", "activation mappings require bounded string keys")
        return {
            key: _bounded(value[key], depth=depth + 1, counter=counter)
            for key in sorted(value)
        }
    raise FleetActivationError(
        "fleet_activation_invalid", "activation input is not canonical JSON data")


def _private_shape(value: Any) -> dict:
    """Describe a private field without hashing or exposing its value."""
    if value is None:
        return {"present": False, "kind": "none", "items": 0}
    if isinstance(value, (list, tuple, dict)):
        return {"present": True, "kind": "collection", "items": len(value)}
    return {"present": True, "kind": "scalar", "items": 1}


def _invocation_projection(invocation: Any) -> dict:
    if not is_dataclass(invocation):
        raise FleetActivationError(
            "fleet_activation_invocation_invalid", "activation requires a dataclass invocation")
    actual = {field.name for field in fields(invocation)}
    if actual != _INVOCATION_FIELDS:
        raise FleetActivationError(
            "fleet_activation_invocation_invalid",
            "invocation schema changed; activation support must be reviewed explicitly")
    projection = {}
    for name in sorted(actual):
        value = getattr(invocation, name)
        projection[name] = (_private_shape(value) if name in _PRIVATE_INVOCATION_FIELDS
                            else _bounded(value))
    raw = _canonical(projection)
    if len(raw) > MAX_CANONICAL_BYTES:
        raise FleetActivationError(
            "fleet_activation_invocation_invalid", "invocation identity exceeds bounds")
    return projection


def invocation_structural_sha256(invocation: Any) -> str:
    """Hash the reviewed public structure; private values require a keyed binding."""
    return _sha(_invocation_projection(invocation))


def _codex_custom_provider_configured() -> bool | None:
    """Return True for a selected custom provider, None when config is unreadable."""
    path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    if not os.path.exists(path):
        return False
    try:
        if os.path.getsize(path) > MAX_CANONICAL_BYTES:
            return None
        with open(path, "rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    # An explicit selector can redirect both transport and billing. Even a familiar
    # label is not certified here because its registry definition may be overridden.
    return "model_provider" in value


def derive_billing_class(invocation: Any) -> dict:
    """Conservatively derive billing from backend policy and the local environment.

    This is a pre-launch projection, not provider attestation.  The future launcher must
    recompute it from the effective child environment immediately before its launch CAS.
    """
    cli = getattr(invocation, "cli", None)
    model = getattr(invocation, "model", None)
    extra_args = tuple(getattr(invocation, "extra_args", ()) or ())
    if cli == "codex":
        configured = _codex_custom_provider_configured()
        custom = bool(
            configured is not False
            or os.environ.get("CODEX_HOME")
            or os.environ.get("CODEX_MODEL_PROVIDER")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            # A named Codex profile has its own config-precedence chain and can
            # select a custom provider even when the base config is ordinary.
            # Slice A does not resolve/profile-bind those private bytes, so any
            # profile selector is conservatively ineligible.
            or any(str(value).lower() in {"-c", "--config", "-p", "--profile"}
                   or str(value).lower().startswith("--config=")
                   or str(value).lower().startswith("--profile=")
                   or str(value).lower().startswith("-p=")
                   or "model_provider" in str(value).lower()
                   or "model-provider" in str(value).lower()
                   or "base_url" in str(value).lower()
                   for value in extra_args))
        if custom:
            billing, source = "unknown", "custom_provider_configuration"
        else:
            metered = bool(os.environ.get("OPENAI_API_KEY")
                           and os.environ.get("SUBAGENTS_ALLOW_OPENAI_KEY") == "1")
            billing = "payg" if metered else "subscription"
            source = "codex_key_policy"
    elif cli == "claude":
        custom = bool(
            os.environ.get("CLAUDE_CODE_USE_BEDROCK")
            or os.environ.get("CLAUDE_CODE_USE_VERTEX")
            or os.environ.get("ANTHROPIC_BEDROCK_BASE_URL")
            or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or os.environ.get("ANTHROPIC_BASE_URL"))
        if custom:
            billing, source = "unknown", "custom_provider_configuration"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            billing, source = "payg", "anthropic_key_present"
        elif model == "claude-fable-5":
            billing, source = "unknown", "plan_dependent_model"
        else:
            billing, source = "subscription", "anthropic_key_absent"
    elif cli == "cursor-agent":
        custom = bool(os.environ.get("CURSOR_API_BASE_URL"))
        if custom:
            billing, source = "unknown", "custom_provider_configuration"
        else:
            billing = ("payg" if (os.environ.get("CLI_API_KEY")
                                   or os.environ.get("CURSOR_API_KEY"))
                       else "subscription")
            source = "cursor_key_policy"
    elif cli == "gemini":
        vertex = bool(
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"))
        if vertex:
            billing, source = "unknown", "vertex_or_adc_configuration"
        else:
            billing = "payg" if os.environ.get("GEMINI_API_KEY") else "unknown"
            source = "gemini_key_policy"
    else:
        billing, source = "unknown", "unsupported_backend_policy"
    credential_binding = billing == "payg"
    return {
        "class": billing,
        "source": source,
        "evidence": "derived_local_policy",
        "candidate_eligible": billing != "unknown",
        "credential_binding_required": credential_binding,
        "final_recheck_required": True,
    }


def _validate_attempt_policy(invocation: Any) -> None:
    if getattr(invocation, "transport", None) != "subprocess":
        raise FleetActivationError(
            "fleet_activation_transport_unsupported",
            "fleet activation currently permits only subprocess transport")
    if getattr(invocation, "cli", None) not in PURE_SUBPROCESS_BACKENDS:
        raise FleetActivationError(
            "fleet_activation_backend_unsupported",
            "backend preparation is not yet proven provider-inert")
    if (getattr(invocation, "resume_id", None) is not None
            or getattr(invocation, "resume_profile", None) is not None
            or getattr(invocation, "attempt_id", None) is not None
            or getattr(invocation, "parent_attempt_id", None) is not None
            or getattr(invocation, "attempt_kind", None) != "initial"
            or getattr(invocation, "attempt_ordinal", None) != 1):
        raise FleetActivationError(
            "fleet_activation_attempt_unsupported",
            "fleet activation requires one fresh initial foreground attempt")


def freeze_activation_candidate(*, bindings: dict, claim_id: str, seat: str,
                                agent_definition_sha256: str,
                                invocation: Any) -> dict:
    """Create non-authoritative evidence for one future foreground launch."""
    if (not isinstance(bindings, dict) or set(bindings) != _BINDING_HASHES
            or not all(isinstance(bindings.get(name), str)
                       and _SHA256.fullmatch(bindings[name])
                       for name in _BINDING_HASHES)):
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "activation bindings are malformed")
    if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id):
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "activation claim id is malformed")
    if not isinstance(seat, str) or not _IDENTIFIER.fullmatch(seat):
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "activation seat is malformed")
    if (not isinstance(agent_definition_sha256, str)
            or not _SHA256.fullmatch(agent_definition_sha256)):
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "agent definition digest is malformed")
    _validate_attempt_policy(invocation)
    projection = _invocation_projection(invocation)
    if _sha(getattr(invocation, "prompt", "")) != bindings["prompt_sha256"]:
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "prompt differs from its activation binding")
    billing = derive_billing_class(invocation)
    payload = {
        "authorization": "evidence_only",
        "activation_allowed": False,
        "candidate_eligible": billing["candidate_eligible"],
        "binding_state": "unbound",
        "bindings": {**bindings, "claim_id": claim_id},
        "agent": {"seat": seat, "definition_sha256": agent_definition_sha256},
        "invocation": {
            "structural_sha256": _sha(projection),
            "private_binding": "required",
            "backend": invocation.cli,
            "transport": invocation.transport,
            "permission": invocation.permission,
            "model_sha256": _sha(invocation.model),
        },
        "billing": billing,
        "attempt_policy": {
            "mode": "foreground", "max_physical_attempts": 1,
            "retry": False, "fallback": False, "contract_repair": False,
            "resume": False, "secondary_transport": False,
        },
        "final_launch": None,
    }
    contract = {"schema": SCHEMA, **payload}
    contract["sha256"] = hashlib.sha256(
        SCHEMA.encode("ascii") + b"\0" + _canonical(payload)).hexdigest()
    validate_activation_contract(contract)
    return contract


def validate_activation_contract(value: Any) -> dict:
    """Validate structure and digest; never convert evidence into authority."""
    try:
        normalized = _bounded(value)
        raw = _canonical(normalized)
    except (TypeError, ValueError, RecursionError, FleetActivationError) as exc:
        if isinstance(exc, FleetActivationError):
            raise
        raise FleetActivationError(
            "fleet_activation_invalid", "activation contract is malformed") from exc
    if len(raw) > MAX_CANONICAL_BYTES or not isinstance(normalized, dict):
        raise FleetActivationError(
            "fleet_activation_invalid", "activation contract exceeds bounds")
    fields_expected = {
        "schema", "sha256", "authorization", "activation_allowed",
        "candidate_eligible", "binding_state",
        "bindings", "agent", "invocation", "billing", "attempt_policy", "final_launch",
    }
    if set(normalized) != fields_expected or normalized.get("schema") != SCHEMA:
        raise FleetActivationError(
            "fleet_activation_invalid", "activation contract fields are malformed")
    payload = {key: item for key, item in normalized.items()
               if key not in {"schema", "sha256"}}
    expected = hashlib.sha256(
        SCHEMA.encode("ascii") + b"\0" + _canonical(payload)).hexdigest()
    if normalized.get("sha256") != expected:
        raise FleetActivationError(
            "fleet_activation_invalid", "activation contract digest is invalid")
    bindings = payload.get("bindings")
    if (payload.get("authorization") != "evidence_only"
            or payload.get("activation_allowed") is not False
            or payload.get("candidate_eligible") is not bool(
                payload.get("billing", {}).get("candidate_eligible"))
            or payload.get("binding_state") != "unbound"
            or payload.get("final_launch") is not None
            or not isinstance(bindings, dict)
            or set(bindings) != _BINDING_HASHES | {"claim_id"}
            or not all(_SHA256.fullmatch(str(bindings.get(name, "")))
                       for name in _BINDING_HASHES)
            or not _CLAIM_ID.fullmatch(str(bindings.get("claim_id", "")))):
        raise FleetActivationError(
            "fleet_activation_invalid", "activation authority boundary is malformed")
    policy = payload.get("attempt_policy")
    expected_policy = {
        "mode": "foreground", "max_physical_attempts": 1,
        "retry": False, "fallback": False, "contract_repair": False,
        "resume": False, "secondary_transport": False,
    }
    billing = payload.get("billing")
    if (policy != expected_policy or not isinstance(billing, dict)
            or set(billing) != {
                "class", "source", "evidence", "candidate_eligible",
                "credential_binding_required", "final_recheck_required"}
            or billing.get("class") not in {"subscription", "payg", "unknown"}
            or billing.get("source") not in _BILLING_SOURCES
            or billing.get("evidence") != "derived_local_policy"
            or billing.get("candidate_eligible") is not (
                billing.get("class") != "unknown")
            or billing.get("credential_binding_required") is not (
                billing.get("class") == "payg")
            or billing.get("final_recheck_required") is not True):
        raise FleetActivationError(
            "fleet_activation_invalid", "activation policy or billing evidence is malformed")
    for section, exact in (
            (payload.get("agent"), {"seat", "definition_sha256"}),
            (payload.get("invocation"), {
                "structural_sha256", "private_binding", "backend", "transport",
                "permission", "model_sha256"})):
        if not isinstance(section, dict) or set(section) != exact:
            raise FleetActivationError(
                "fleet_activation_invalid", "activation identity section is malformed")
    if (not _IDENTIFIER.fullmatch(str(payload["agent"].get("seat", "")))
            or not _SHA256.fullmatch(str(payload["agent"].get("definition_sha256", "")))
            or payload["invocation"].get("backend") not in PURE_SUBPROCESS_BACKENDS
            or payload["invocation"].get("transport") != "subprocess"
            or payload["invocation"].get("private_binding") != "required"
            or payload["invocation"].get("permission") not in {
                "read-only", "safe-edit", "yolo"}
            or not _SHA256.fullmatch(str(payload["invocation"].get("structural_sha256", "")))
            or not _SHA256.fullmatch(str(payload["invocation"].get("model_sha256", "")))):
        raise FleetActivationError(
            "fleet_activation_invalid", "activation identity values are malformed")
    return normalized


def matches_activation_candidate(contract: dict, *, agent_definition_sha256: str,
                                 current_request_identity_sha256: str,
                                 invocation: Any) -> bool:
    """Return whether Slice A can *certify* a match.

    Slice A deliberately cannot certify private invocation or credential equality: that
    requires the future keyed ledger binding.  The comparisons here still fail quickly on
    public structural, definition, billing, prompt, or fresh request-identity drift.
    """
    normalized = validate_activation_contract(contract)
    _validate_attempt_policy(invocation)
    if (not isinstance(current_request_identity_sha256, str)
            or not _SHA256.fullmatch(current_request_identity_sha256)):
        raise FleetActivationError(
            "fleet_activation_binding_invalid",
            "current request identity digest is malformed")
    structural_match = bool(
        normalized["agent"]["definition_sha256"] == agent_definition_sha256
        and normalized["bindings"]["request_identity_sha256"]
        == current_request_identity_sha256
        and normalized["invocation"]["structural_sha256"]
        == invocation_structural_sha256(invocation)
        and normalized["billing"] == derive_billing_class(invocation)
        and normalized["bindings"]["prompt_sha256"]
        == _sha(getattr(invocation, "prompt", ""))
    )
    # A plain digest cannot safely bind low-entropy private fields or credential values.
    # Until the private ledger supplies and verifies a keyed binding, certification is false.
    return bool(structural_match and normalized["invocation"]["private_binding"] == "verified")


__all__ = [
    "SCHEMA", "FleetActivationError", "PURE_SUBPROCESS_BACKENDS",
    "derive_billing_class", "invocation_structural_sha256",
    "freeze_activation_candidate", "validate_activation_contract",
    "matches_activation_candidate",
]
