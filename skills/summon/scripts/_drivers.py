"""Thin provider-driver SPI for Summon 2.0 (T3-inspired, still headless).

Permission translation (Summon → native), with honest fallbacks:

| Summon       | Claude/Codex-ish         | ACP (cursor/gemini)      | openai-compat |
|--------------|--------------------------|--------------------------|---------------|
| read-only    | plan/read / ask          | approval-required        | N/A (no tools)|
| safe-edit    | default / workspace-write| auto-accept-edits        | N/A           |
| full / yolo  | bypassPermissions / danger| full-access             | N/A           |

If a provider cannot honor a tier, Summon must refuse or degrade loudly —
never claim enforcement it does not have.

Drivers registered here are the advisory/registry surface for doctor + MCP +
future routing. Primary dispatch still goes through ``execute_agent`` /
``_apibackend.call`` / ``_arkcli_backend.call``; ``enrich_envelope_from_cli``
stamps additive fields.

``arkcli`` is dual-wired with ``openai-compat``: HTTP Coding Plan seats stay on
openai-compat; ``run-agent: arkcli`` uses ``arkcli +chat`` with profile-store
auth (api-kind entry whose call is a thin subprocess, not an HTTP agent loop).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any


BACKEND_TYPE_CLI = "cli_agent"
BACKEND_TYPE_CHAT = "chat_completions"
BACKEND_TYPE_ARKCLI = "arkcli_chat"
BACKEND_TYPE_ACP = "cli_agent"

PERMISSION_MAP = {
    "read-only": {
        "claude": "plan",
        "codex": "read-only",
        "cursor-agent": "approval-required",
        "agy": "refused-unless-opt-in",
        "openai-compat": "n/a",
        "arkcli": "n/a",
    },
    "safe-edit": {
        "claude": "default",
        "codex": "workspace-write",
        "cursor-agent": "auto-accept-edits",
        "agy": "dangerously-skip-permissions+add-dir",
        "openai-compat": "n/a",
        "arkcli": "n/a",
    },
    "full": {
        "claude": "bypassPermissions",
        "codex": "danger-full-access",
        "cursor-agent": "full-access",
        "agy": "dangerously-skip-permissions",
        "openai-compat": "n/a",
        "arkcli": "n/a",
    },
}


@dataclass(frozen=True)
class Driver:
    name: str
    kind: str  # subprocess | api | acp


DRIVERS: dict[str, Driver] = {
    "cli": Driver("cli", "subprocess"),
    "openai-compat": Driver("openai-compat", "api"),
    "arkcli": Driver("arkcli", "api"),
    "acp": Driver("acp", "acp"),
}


def get_driver(name: str) -> Driver:
    if name not in DRIVERS:
        raise KeyError(f"unknown provider driver {name!r}")
    return DRIVERS[name]


def list_drivers() -> list[str]:
    return sorted(DRIVERS)


def resolve_model(driver: str, model_id: str | None) -> str | None:
    """Identity resolve for now; drivers may specialize later."""
    return model_id


def probe(driver: str) -> dict[str, Any]:
    """Cheap health snapshot — never raises. Always includes ``ok`` bool."""
    if driver == "openai-compat":
        return {"driver": driver, "ok": True, "found": True, "kind": "api",
                "note": "HTTP Chat Completions; needs provider base_url + key"}
    if driver == "arkcli":
        found = bool(shutil.which("arkcli") or (
            os.name == "nt" and shutil.which("arkcli.cmd")))
        return {"driver": driver, "ok": found, "found": found,
                "path": "arkcli" if found else None, "kind": "api",
                "auth": "arkcli auth login",
                "note": "arkcli +chat via profile store (dual-wire with openai-compat HTTP)"}
    if driver == "acp":
        return {"driver": driver, "ok": True, "found": True, "kind": "acp",
                "note": "native ACP for gemini/kimi/cursor-agent when supported"}
    if driver == "cli":
        return {"driver": driver, "ok": True, "found": True, "kind": "subprocess",
                "note": "argv backends: claude/codex/cursor-agent/gemini/kimi/agy"}
    return {"driver": driver, "ok": False, "found": False,
            "error": "unknown driver"}


def cancel(_job_id: str) -> dict:
    """One-shot dispatches have nothing to cancel."""
    return {"cancelled": False, "note": "one-shot drivers; cancel is a no-op"}


def stamp_driver_fields(resp: dict, *, driver: str, backend_type: str,
                        served_via: str | None = None) -> dict:
    """Add additive envelope fields without removing existing keys.

    Sets ``provider.driver``, ``backend_type``, and ``served.via`` (plus
    ``served_via`` sibling for lenient consumers).
    """
    if not isinstance(resp, dict):
        return resp
    via = served_via or backend_type
    resp["backend_type"] = backend_type
    resp["served_via"] = via
    served = resp.get("served")
    if not isinstance(served, dict):
        served = {}
        resp["served"] = served
    served["via"] = via
    prov = resp.get("provider")
    if not isinstance(prov, dict):
        prov = {}
        resp["provider"] = prov
    prov["driver"] = driver
    return resp


def _driver_for_cli(cli: str) -> tuple[str, str, str]:
    """Return ``(driver_name, backend_type, served_via)`` for a backend cli."""
    if cli == "openai-compat":
        return "openai-compat", BACKEND_TYPE_CHAT, "chat_completions"
    if cli == "arkcli":
        return "arkcli", BACKEND_TYPE_ARKCLI, "arkcli_chat"
    return "cli", BACKEND_TYPE_CLI, "cli_agent"


def enrich_envelope_from_cli(resp: dict, cli: str,
                             transport: str = "subprocess") -> dict:
    """Stamp SPI fields onto a finished envelope. Fail-soft."""
    if not isinstance(resp, dict):
        return resp
    driver, backend_type, via = _driver_for_cli(cli)
    if transport == "acp":
        driver, via = "acp", "acp"
        backend_type = BACKEND_TYPE_ACP
    return stamp_driver_fields(resp, driver=driver, backend_type=backend_type,
                               served_via=via)


def dispatch_via_driver(driver: str, inv, timeout_ms: int) -> dict:
    """Optional explicit driver dispatch (MCP / experiments)."""
    if driver == "openai-compat":
        from _apibackend import call
        return enrich_envelope_from_cli(call(inv, timeout_ms), "openai-compat")
    if driver == "arkcli":
        from _arkcli_backend import call as ark_call
        return enrich_envelope_from_cli(ark_call(inv, timeout_ms), "arkcli")
    if driver in ("cli", "acp"):
        from _executor import execute_agent
        return execute_agent(inv, timeout_ms=timeout_ms)
    return {"status": "error", "exit_code": 2, "cli": driver,
            "error": f"unknown provider driver {driver!r}", "result": ""}
