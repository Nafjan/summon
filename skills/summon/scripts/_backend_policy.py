"""Pure, shared backend authority facts.

This module is the single source for static permission/capability facts used by
the command builder and provider-inert control planes.  It imports no provider,
process, network, credential, or filesystem code.
"""

from __future__ import annotations


PERMISSION_ORDER = {"read-only": 0, "safe-edit": 1, "yolo": 2}
KNOWN_BACKENDS = frozenset({
    "agy", "arkcli", "claude", "codex", "cursor-agent", "gemini", "kimi",
    "openai-compat", "opencode",
})
TOOLFUL_BACKENDS = frozenset({
    "agy", "claude", "codex", "cursor-agent", "gemini", "kimi", "opencode",
})
TEXT_ONLY_BACKENDS = frozenset({"arkcli", "openai-compat"})
CAPABILITIES = frozenset({"filesystem", "read_only_enforced", "text", "tools"})

# ``provider`` names the account/endpoint authority that receives and bills the
# request, not a model vendor guessed from a model id. Multi-provider gateways
# must declare it in agent frontmatter; singleton backends have one reviewed
# default. Keeping this beside the permission facts prevents control planes
# from inventing contradictory provider vocabularies.
_PROVIDER_BY_BACKEND = {
    "agy": "google",
    "arkcli": "byteplus",
    "claude": "anthropic",
    "codex": "openai",
    "cursor-agent": "cursor",
    "gemini": "google",
    "kimi": "moonshot",
}
MULTI_PROVIDER_BACKENDS = frozenset({"openai-compat", "opencode"})


def permission_enforcement(cli: str, permission: str) -> str:
    """Classify the real backend/tier boundary used by decision evidence."""
    if cli not in KNOWN_BACKENDS:
        return "unknown"
    if permission == "yolo":
        return "enforced"  # no narrower boundary is being promised
    if cli in TEXT_ONLY_BACKENDS:
        # These request transports expose no local filesystem or shell tools.
        return "enforced"
    if cli in {"agy", "kimi"}:
        return "unenforceable"
    return "enforced"


def effective_permission(cli: str, permission: str) -> str:
    """Return the authority a backend actually receives for a declared tier."""
    if cli == "agy":
        if permission == "safe-edit":
            return "yolo"
        if permission == "read-only":
            return "unenforceable"
    if cli == "kimi":
        if permission == "read-only":
            return "unenforceable"
        if permission == "safe-edit":
            return "yolo"
    return permission


def backend_capabilities(cli: str, permission: str) -> list[str]:
    """Return reviewed static capabilities, or an empty list for an unknown CLI."""
    if cli not in KNOWN_BACKENDS:
        return []
    values = {"text"}
    if cli in TOOLFUL_BACKENDS:
        values.update({"filesystem", "tools"})
    if permission == "read-only" and permission_enforcement(cli, permission) == "enforced":
        values.add("read_only_enforced")
    return sorted(values)


def provider_contact_required(cli: str) -> bool:
    """Conservatively classify every roster backend as provider-contacting.

    An unknown future backend must not become a zero-cost route merely because
    this static table has not learned its name yet.
    """
    return isinstance(cli, str) and bool(cli)


def route_provider(cli: str, declared_provider: str | None,
                   model: str | None, endpoint_mode: str = "none") -> tuple[str, str]:
    """Bind a provider declaration to the mechanism that executes the route.

    Returns ``(provider, evidence)``.  Model prefixes are never provider proof
    by themselves.  For OpenCode they are used only to verify an explicit
    declaration against the selector OpenCode executes.  OpenAI-compatible
    named-provider keys preserve exact case because the registry lookup does.
    An inline endpoint has no publishable provider identity without a separate
    endpoint attestation, so it remains unknown even when it carries a label.
    """
    declared = (declared_provider.strip()
                if isinstance(declared_provider, str) else "")
    if declared.casefold() == "unknown":
        return "unknown", "unknown"
    if cli == "opencode":
        if not declared or not isinstance(model, str) or "/" not in model:
            return "unknown", "unknown"
        selector_provider = model.split("/", 1)[0]
        if selector_provider != declared:
            return "unknown", "mismatch"
        return declared, "model_selector_verified"
    if cli == "openai-compat":
        if endpoint_mode == "named_provider" and declared:
            return declared, "named_registry"
        return "unknown", "unknown"

    expected = _PROVIDER_BY_BACKEND.get(cli)
    if expected is None:
        return "unknown", "unknown"
    if declared and declared.casefold() != expected.casefold():
        return "unknown", "mismatch"
    return expected, "backend_static"
