"""Provider-inert fleet activation contracts.

This module freezes the inputs that a future fleet launcher must authenticate before
contacting a provider.  It intentionally cannot launch, reserve, approve, or turn its
digest-sealed output into authority.  The private dispatch ledger remains the only place
where a later milestone may bind this evidence to a single-use claim.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

try:  # Python 3.11+ standard library; optional compatibility package on 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - selected by the interpreter version
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


SCHEMA = "summon.fleet-activation/v1"
MAX_CANONICAL_BYTES = 256 * 1024
MAX_ITEMS = 4096
MAX_DEPTH = 16
MAX_TEXT = 32 * 1024
MAX_PRIVATE_FILE_BYTES = 256 * 1024

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
    "api_key_fingerprint",
    "agy_account_sha256", "profile", "profile_env", "profile_command",
    "openrouter_options", "worktree", "read_roots", "attempt_id",
    "parent_attempt_id",
})

_INVOCATION_FIELDS = frozenset({
    "cli", "prompt", "cwd", "system_context", "agent_file", "permission",
    "transport", "model", "model_source", "model_exact_required",
    "model_exact_source", "effort", "resume_id", "resume_profile", "extra_args",
    "base_url", "api_key_env", "api_key_fingerprint", "allow_payg", "agy_account_sha256",
    "agy_account_checked", "permission_forced", "profile", "profile_env",
    "profile_command", "profile_auth_mode", "openrouter_options", "worktree", "isolated_lane",
    "allow_tool_credentials", "read_roots", "output_contract", "attempt_id",
    "attempt_kind", "attempt_ordinal", "parent_attempt_id",
})

_BINDING_HASHES = frozenset({
    "approval_id", "fleet_sha256", "plan_sha256", "lane_sha256", "catalog_sha256",
    "project_sha256", "prompt_sha256", "request_identity_sha256",
})

_SENSITIVE_ENV_EXACT = frozenset({
    "CLI_API_KEY", "CODEX_HOME", "CODEX_MODEL_PROVIDER",
    "SUBAGENTS_ALLOW_OPENAI_KEY", "SUMMON_ALLOW_CREDIT", "SUMMON_ALLOW_PAYG",
    "SUMMON_FRESH_CONSENT_ONLY",
})
_SENSITIVE_ENV_PREFIXES = (
    "ANTHROPIC_", "OPENAI_", "CURSOR_", "CLAUDE_CODE_", "GOOGLE_",
    "GEMINI_", "AWS_", "KIMI_", "AGY_", "ARK_", "OPENROUTER_", "NOUS_",
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")

_CLAUDE_PRIVATE_FILES = (
    ".credentials.json", "account.json", "auth.json", "credentials.json",
    "settings.json", "settings.local.json",
)
_CODEX_PRIVATE_FILES = ("auth.json", "config.json", "config.toml")


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


def _profile_environment(invocation: Any) -> dict[str, str]:
    """Project the environment the pure backend builder actually gives its child."""
    effective = dict(os.environ)
    profile_env = getattr(invocation, "profile_env", None)
    # Both named-profile builders forward the selected home. Login-account
    # mode additionally removes ambient credentials before applying that home.
    if getattr(invocation, "profile_auth_mode", "profile") == "login":
        from _profiles import account_launch_policy
        _, profile_env = account_launch_policy(invocation)
    if getattr(invocation, "cli", None) in ("claude", "codex") and profile_env is not None:
        if (not isinstance(profile_env, dict)
                or not all(isinstance(key, str) and _ENV_NAME.fullmatch(key)
                           and (value is None or isinstance(value, str))
                           for key, value in profile_env.items())):
            raise FleetActivationError(
                "fleet_activation_invocation_invalid",
                "profile environment must contain bounded environment names and text")
        for key, value in profile_env.items():
            if value is None:
                effective.pop(key, None)
            else:
                effective[key] = value
    return effective


def _codex_custom_provider_configured(environment: dict[str, str]) -> bool | None:
    """Return True for a selected custom provider, None when config is unreadable."""
    configured_root = environment.get("CODEX_HOME")
    root = (os.path.expandvars(os.path.expanduser(configured_root))
            if configured_root else os.path.join(os.path.expanduser("~"), ".codex"))
    path = os.path.join(root, "config.toml")
    if not os.path.exists(path):
        return False
    # A Python 3.10 standalone install may not have the optional ``tomli``
    # compatibility package.  Do not guess that an unreadable config is safe:
    # unknown makes the candidate ineligible without crashing the dispatcher.
    if tomllib is None:
        return None
    try:
        if os.path.getsize(path) > MAX_CANONICAL_BYTES:
            return None
        with open(path, "rb") as handle:
            value = tomllib.load(handle)
    except (OSError, ValueError, TypeError):
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
    environment = _profile_environment(invocation)
    if cli == "codex":
        configured = _codex_custom_provider_configured(environment)
        custom = bool(
            configured is not False
            or environment.get("CODEX_HOME")
            or environment.get("CODEX_MODEL_PROVIDER")
            or environment.get("OPENAI_BASE_URL")
            or environment.get("OPENAI_API_BASE")
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
            metered = bool(environment.get("OPENAI_API_KEY")
                           and environment.get("SUBAGENTS_ALLOW_OPENAI_KEY") == "1")
            billing = "payg" if metered else "subscription"
            source = "codex_key_policy"
    elif cli == "claude":
        settings_environment = _claude_settings_environment(invocation, environment)
        if settings_environment is None:
            return {
                "class": "unknown", "source": "custom_provider_configuration",
                "evidence": "derived_local_policy", "candidate_eligible": False,
                "credential_binding_required": False, "final_recheck_required": True,
            }
        effective = {**environment, **settings_environment}
        custom = bool(
            effective.get("CLAUDE_CODE_USE_BEDROCK")
            or effective.get("CLAUDE_CODE_USE_VERTEX")
            or effective.get("ANTHROPIC_BEDROCK_BASE_URL")
            or effective.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or effective.get("ANTHROPIC_BASE_URL"))
        if custom:
            billing, source = "unknown", "custom_provider_configuration"
        elif effective.get("ANTHROPIC_API_KEY"):
            billing, source = "payg", "anthropic_key_present"
        elif model in {"claude-fable-5", "claude-fable-5-1"}:
            billing, source = "unknown", "plan_dependent_model"
        else:
            billing, source = "subscription", "anthropic_key_absent"
    elif cli == "cursor-agent":
        custom = bool(environment.get("CURSOR_API_BASE_URL"))
        if custom:
            billing, source = "unknown", "custom_provider_configuration"
        else:
            # A key is bindable. Cursor's signed-in subscription account is not
            # yet exposed through a provider-inert, content-addressable identity,
            # so absence of a key must not certify an account or subscription.
            billing = ("payg" if (environment.get("CLI_API_KEY")
                                   or environment.get("CURSOR_API_KEY"))
                       else "unknown")
            source = "cursor_key_policy"
    elif cli == "gemini":
        vertex = bool(
            environment.get("GOOGLE_APPLICATION_CREDENTIALS")
            or environment.get("GOOGLE_CLOUD_PROJECT")
            or environment.get("GOOGLE_GENAI_USE_VERTEXAI"))
        if vertex:
            billing, source = "unknown", "vertex_or_adc_configuration"
        else:
            billing = "payg" if environment.get("GEMINI_API_KEY") else "unknown"
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


def validate_private_binding_inputs(
        contract: dict, *, agent_definition_sha256: str,
        current_request_identity_sha256: str, invocation: Any) -> dict:
    """Validate all non-secret Slice A evidence before a private ledger binding."""
    normalized = validate_activation_contract(contract)
    _validate_attempt_policy(invocation)
    if (not isinstance(agent_definition_sha256, str)
            or not _SHA256.fullmatch(agent_definition_sha256)
            or not isinstance(current_request_identity_sha256, str)
            or not _SHA256.fullmatch(current_request_identity_sha256)):
        raise FleetActivationError(
            "fleet_activation_binding_invalid", "current identity digest is malformed")
    if normalized["candidate_eligible"] is not True:
        raise FleetActivationError(
            "fleet_activation_billing_unknown",
            "unknown billing cannot become an authoritative activation candidate")
    if (normalized["agent"]["definition_sha256"] != agent_definition_sha256
            or normalized["bindings"]["request_identity_sha256"]
            != current_request_identity_sha256
            or normalized["invocation"]["structural_sha256"]
            != invocation_structural_sha256(invocation)
            or normalized["billing"] != derive_billing_class(invocation)
            or normalized["bindings"]["prompt_sha256"]
            != _sha(getattr(invocation, "prompt", ""))):
        raise FleetActivationError(
            "fleet_activation_binding_changed",
            "definition, request, invocation, prompt, or billing evidence changed")
    return normalized


def _sensitive_environment_projection(
        invocation: Any, environment: dict[str, str]) -> dict:
    selected = {}
    for key, value in environment.items():
        if key in _SENSITIVE_ENV_EXACT or key.startswith(_SENSITIVE_ENV_PREFIXES):
            if (not isinstance(key, str) or len(key) > 256
                    or not isinstance(value, str) or len(value) > MAX_TEXT):
                raise FleetActivationError(
                    "fleet_activation_private_binding_invalid",
                    "sensitive environment exceeds private binding bounds")
            selected[key] = value
    api_key_env = getattr(invocation, "api_key_env", None)
    if api_key_env is not None:
        if not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env):
            raise FleetActivationError(
                "fleet_activation_private_binding_invalid",
                "API-key environment name is malformed")
        if api_key_env in environment:
            value = environment[api_key_env]
            if len(value) > MAX_TEXT:
                raise FleetActivationError(
                    "fleet_activation_private_binding_invalid",
                    "API-key environment value exceeds private binding bounds")
            selected[api_key_env] = value
    if len(selected) > 256:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            "too many sensitive environment values for private binding")
    return {key: selected[key] for key in sorted(selected)}


def _reject_private_link(path: str, field: str, *, directory: bool) -> os.stat_result:
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} cannot be inspected for private binding") from exc
    attrs = getattr(value, "st_file_attributes", 0)
    if stat.S_ISLNK(value.st_mode) or bool(
            attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} refuses links and reparse points")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode) or (not directory and getattr(value, "st_nlink", 1) != 1):
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} must be a private {'directory' if directory else 'single-link regular file'}")
    return value


def _reject_private_ancestors(path: str, field: str) -> None:
    current = Path(os.path.abspath(path))
    for component in reversed([current, *current.parents]):
        if not os.path.lexists(component):
            continue
        value = os.lstat(component)
        attrs = getattr(value, "st_file_attributes", 0)
        if stat.S_ISLNK(value.st_mode) or bool(
                attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise FleetActivationError(
                "fleet_activation_private_binding_invalid",
                f"{field} refuses a linked or reparse-point ancestor")


def _private_file_bytes(path: str, field: str) -> bytes | None:
    if not os.path.lexists(path):
        return None
    _reject_private_ancestors(os.path.dirname(path), field)
    identity = _reject_private_link(path, field, directory=False)
    if identity.st_size > MAX_PRIVATE_FILE_BYTES:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} exceeds private binding bounds")
    try:
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(MAX_PRIVATE_FILE_BYTES + 1)
            closed = os.fstat(handle.fileno())
    except OSError as exc:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} cannot be read for private binding") from exc
    if (len(raw) > MAX_PRIVATE_FILE_BYTES
            or (identity.st_dev, identity.st_ino, identity.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (closed.st_dev, closed.st_ino, closed.st_size, closed.st_mtime_ns)):
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            f"{field} changed while it was privately bound")
    return raw


def _private_file_sha256(path: str, field: str) -> str | None:
    raw = _private_file_bytes(path, field)
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _private_directory_projection(
        root: str, names: tuple[str, ...], field: str) -> dict | None:
    expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
    if not os.path.lexists(expanded):
        return None
    _reject_private_ancestors(os.path.dirname(expanded), field)
    _reject_private_link(expanded, field, directory=True)
    return {
        name: _private_file_sha256(os.path.join(expanded, name), f"{field}/{name}")
        for name in names
    }


def _profile_private_projection(
        invocation: Any, environment: dict[str, str]) -> dict:
    cli = getattr(invocation, "cli", None)
    if cli == "claude":
        root = environment.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude")
        project_root = os.path.join(
            os.path.abspath(getattr(invocation, "cwd", "")), ".claude")
        return {
            "backend": "claude",
            "profile": _private_directory_projection(
                root, _CLAUDE_PRIVATE_FILES, "Claude profile"),
            # Named Claude profiles retain Claude's project/local settings
            # sources. Bind those bytes as well as the named user profile so a
            # project cannot change credentials or endpoint selection between
            # reservation and the final launch recheck.
            "project": _private_directory_projection(
                project_root, ("settings.json", "settings.local.json"),
                "Claude project settings"),
            "account": _private_file_sha256(
                os.path.join(os.path.expanduser("~"), ".claude.json"),
                "Claude account configuration"),
        }
    if cli == "codex":
        configured_root = environment.get("CODEX_HOME")
        root = (os.path.expandvars(os.path.expanduser(configured_root))
                if configured_root else os.path.join(os.path.expanduser("~"), ".codex"))
        return {
            "backend": "codex",
            "profile": _private_directory_projection(
                root, _CODEX_PRIVATE_FILES, "Codex profile"),
            "account": None,
        }
    return {"backend": cli, "profile": None, "account": None}


def _claude_settings_environment(
        invocation: Any, environment: dict[str, str]) -> dict[str, str] | None:
    """Return enabled named-profile/project settings env, or fail closed.

    Default Claude seats disable ambient setting sources in ``_builder``. Named
    profiles deliberately keep them, so activation must apply and later bind the
    user, project, and local project files that can alter credentials or routing.
    """
    if (getattr(invocation, "cli", None) != "claude"
            or getattr(invocation, "profile_env", None) is None):
        return {}
    root = environment.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
    project = os.path.join(
        os.path.abspath(getattr(invocation, "cwd", "")), ".claude")
    result: dict[str, str] = {}
    sources = (
        (expanded, "settings.json", "Claude profile/settings.json"),
        (expanded, "settings.local.json", "Claude profile/settings.local.json"),
        (project, "settings.json", "Claude project settings/settings.json"),
        (project, "settings.local.json",
         "Claude project settings/settings.local.json"),
    )
    if getattr(invocation, "profile_auth_mode", "profile") == "login":
        sources = sources[:1]  # matches --setting-sources user
    for directory, name, field in sources:
        raw = _private_file_bytes(
            os.path.join(directory, name), field)
        if raw is None:
            continue
        try:
            value = _bounded(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, FleetActivationError):
            return None
        if not isinstance(value, dict):
            return None
        # Executable credential helpers or cloud refresh hooks can change the
        # effective account dynamically and cannot be certified provider-inertly.
        if any(value.get(key) is not None for key in (
                "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")):
            return None
        configured = value.get("env", {})
        if (not isinstance(configured, dict)
                or not all(_ENV_NAME.fullmatch(str(key))
                           and (item is None or isinstance(item, str))
                           for key, item in configured.items())):
            return None
        for key, item in configured.items():
            if item is None:
                result.pop(key, None)
            else:
                result[key] = item
    return result


def private_binding_hmac(
        invocation: Any, *, master_key: bytes, approval_id: str, claim_id: str,
        contract_sha256: str, agent_definition_sha256: str,
        current_request_identity_sha256: str) -> str:
    """HMAC every private invocation/account/environment input; expose no values."""
    if not isinstance(master_key, bytes) or len(master_key) < 32:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid", "private approval key is invalid")
    for value, pattern in (
            (approval_id, _SHA256), (claim_id, _CLAIM_ID),
            (contract_sha256, _SHA256), (agent_definition_sha256, _SHA256),
            (current_request_identity_sha256, _SHA256)):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise FleetActivationError(
                "fleet_activation_private_binding_invalid",
                "private activation identity is malformed")
    environment = _profile_environment(invocation)
    projection = {
        "contract_sha256": contract_sha256,
        "agent_definition_sha256": agent_definition_sha256,
        "request_identity_sha256": current_request_identity_sha256,
        "invocation": {
            name: _bounded(getattr(invocation, name))
            for name in sorted(_INVOCATION_FIELDS)
        },
        "sensitive_environment": _sensitive_environment_projection(
            invocation, environment),
        "profile_private": _profile_private_projection(invocation, environment),
    }
    raw = _canonical(projection)
    if len(raw) > MAX_CANONICAL_BYTES:
        raise FleetActivationError(
            "fleet_activation_private_binding_invalid",
            "private activation binding exceeds bounds")
    domain = (b"summon.fleet-activation-private/v1\0"
              + approval_id.encode("ascii") + b"\0" + claim_id.encode("ascii"))
    derived = hmac.new(master_key, domain, hashlib.sha256).digest()
    return hmac.new(derived, raw, hashlib.sha256).hexdigest()


__all__ = [
    "SCHEMA", "FleetActivationError", "PURE_SUBPROCESS_BACKENDS",
    "derive_billing_class", "invocation_structural_sha256",
    "freeze_activation_candidate", "validate_activation_contract",
    "matches_activation_candidate", "validate_private_binding_inputs",
    "private_binding_hmac",
]
