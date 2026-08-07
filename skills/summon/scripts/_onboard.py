"""``summon onboard`` — detect CLIs, print install hints, write merge-safe prefs.

Never auto-installs foreign CLIs. Never asks for or stores raw API secrets.
Prefs live in ``~/.agents/summon.json`` (same file as PAYG consent).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any


PREFS_NAME = "summon.json"

# Binary name → install hint (OS-aware where it matters). Auth is a separate hint.
_CLI_CATALOG = (
    {
        "id": "claude",
        "binaries": ("claude",),
        "subscription": "claude",
        "install": {
            "default": "npm install -g @anthropic-ai/claude-code",
        },
        "auth": "claude auth login",
    },
    {
        "id": "codex",
        "binaries": ("codex",),
        "subscription": "codex",
        "install": {
            "default": "npm install -g @openai/codex",
        },
        "auth": "codex login",
    },
    {
        "id": "cursor-agent",
        # Prefer the unambiguous Cursor CLI name — a bare `agent` on PATH is
        # often an unrelated tool and must not infer a Cursor subscription.
        "binaries": ("cursor-agent",),
        "subscription": "cursor",
        "install": {
            "default": "https://cursor.com/cli  (install Cursor CLI / cursor-agent)",
        },
        "auth": "cursor-agent login  (or set CLI_API_KEY)",
    },
    {
        "id": "gemini",
        "binaries": ("gemini",),
        "subscription": "gemini",
        "install": {
            "default": "npm install -g @google/gemini-cli",
        },
        "auth": "gemini  (first interactive run) or GEMINI_API_KEY",
    },
    {
        "id": "agy",
        "binaries": ("agy",),
        "subscription": "antigravity",
        "install": {
            "default": "Antigravity CLI — https://antigravity.google",
            "Windows": "Antigravity CLI — https://antigravity.google  (Windows ConPTY)",
        },
        "auth": "agy login",
    },
    {
        "id": "kimi",
        "binaries": ("kimi",),
        "subscription": "kimi",
        "install": {
            "default": "Kimi Code CLI — https://moonshotai.github.io/kimi-code/",
        },
        "auth": "kimi login",
    },
    {
        "id": "arkcli",
        "binaries": ("arkcli",),
        "subscription": "byteplus_coding_plan",
        "install": {
            "default": "npm install -g @byteplus/ark-cli",
        },
        "auth": "arkcli auth login",
    },
)

KNOWN_SUBSCRIPTIONS = (
    "cursor",
    "claude",
    "codex",
    "gemini",
    "antigravity",
    "byteplus_coding_plan",
    "kimi",
    "other_api",
)


def prefs_path() -> Path:
    return Path.home() / ".agents" / PREFS_NAME


def load_prefs() -> dict[str, Any]:
    path = prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_prefs(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge top-level keys; nested dicts for known sections merge one level."""
    out = dict(existing)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def save_prefs(prefs: dict[str, Any]) -> Path:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def _install_hint(entry: dict) -> str:
    system = platform.system()
    table = entry.get("install") or {}
    return table.get(system) or table.get("default") or "(see vendor docs)"


def detect_clis() -> list[dict[str, Any]]:
    rows = []
    for entry in _CLI_CATALOG:
        found_path = None
        found_bin = None
        for name in entry["binaries"]:
            p = shutil.which(name)
            if p:
                found_path, found_bin = p, name
                break
        rows.append({
            "id": entry["id"],
            "subscription": entry["subscription"],
            "found": bool(found_path),
            "binary": found_bin,
            # Portable label — never absolute machine paths in onboard JSON/text
            # (users paste this into issues; privacy scrub).
            "path": found_bin if found_path else None,
            "install": _install_hint(entry),
            "auth": entry["auth"],
        })
    return rows


def detect_credential_presence() -> dict[str, bool]:
    """Booleans only — never return secret values."""
    out = {
        "BYTEPLUS_CODING_API_KEY": bool(os.environ.get("BYTEPLUS_CODING_API_KEY")),
        "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
        "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
    }
    try:
        from _arkcli_creds import resolve_byteplus_coding_api_key
        key, source = resolve_byteplus_coding_api_key()
        out["byteplus_coding_resolvable"] = bool(key)
        out["byteplus_coding_source"] = source
    except Exception:  # noqa: BLE001
        out["byteplus_coding_resolvable"] = False
        out["byteplus_coding_source"] = None
    return out


def render_detection(clis: list[dict[str, Any]], creds: dict[str, Any]) -> str:
    lines = [
        "summon onboard — detected backends",
        f"platform: {platform.system()} {platform.release()}",
        "",
    ]
    for row in clis:
        if row["found"]:
            lines.append(f"  [OK] {row['id']:14} {row['path']}")
        else:
            lines.append(f"  [--] {row['id']:14} not found")
            lines.append(f"       install: {row['install']}")
            lines.append(f"       auth:    {row['auth']}")
    lines.append("")
    lines.append("credential presence (values never shown):")
    for k in ("BYTEPLUS_CODING_API_KEY", "byteplus_coding_resolvable",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY"):
        if k in creds:
            lines.append(f"  {k}: {creds[k]}")
    if creds.get("byteplus_coding_source"):
        lines.append(f"  byteplus_coding_source: {creds['byteplus_coding_source']}")
    lines.append("")
    lines.append("Summon never auto-installs CLIs and never stores API secrets in prefs.")
    return "\n".join(lines)


def parse_subscription_list(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    parts = [p.strip().lower().replace("-", "_") for p in str(raw).split(",")]
    out = []
    aliases = {
        "byteplus": "byteplus_coding_plan",
        "coding_plan": "byteplus_coding_plan",
        "agy": "antigravity",
        "chatgpt": "codex",
    }
    for p in parts:
        if not p:
            continue
        p = aliases.get(p, p)
        if p in KNOWN_SUBSCRIPTIONS and p not in out:
            out.append(p)
    return out


def build_onboard_prefs(
    subscriptions: list[str],
    *,
    reset: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build prefs for onboard write.

    Always loads the existing prefs file first so sibling keys (e.g.
    ``allow_byteplus_payg``) survive. ``reset=True`` replaces only the
    ``onboard`` section — never the whole document.
    """
    existing = load_prefs()
    onboard_blob: dict[str, Any] = {
        "completed": True,
        "subscriptions": subscriptions,
        "clis_detected": [r["id"] for r in detect_clis() if r["found"]],
    }
    if reset:
        out = dict(existing)
        out["onboard"] = onboard_blob
        if extra:
            for k, v in extra.items():
                if k == "onboard" and isinstance(v, dict):
                    out["onboard"] = v
                else:
                    out[k] = v
        return out
    updates: dict[str, Any] = {"onboard": onboard_blob}
    if extra:
        updates.update(extra)
    return merge_prefs(existing, updates)


def run_onboard(
    *,
    subscriptions: list[str] | None = None,
    reset: bool = False,
    write: bool = True,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Run onboard detection; optionally write prefs.

    Non-interactive by default: pass ``--subscriptions`` (comma list) to record
    active plans. Without it, detection still runs and prefs record detected CLIs.
    """
    clis = detect_clis()
    creds = detect_credential_presence()
    subs = list(subscriptions or [])
    if not subs:
        # Infer soft defaults from what is already on PATH — never invent secrets.
        for row in clis:
            if row["found"] and row["subscription"] not in subs:
                if row["subscription"] in KNOWN_SUBSCRIPTIONS:
                    subs.append(row["subscription"])
    prefs = build_onboard_prefs(subs, reset=reset)
    path = None
    if write:
        path = str(save_prefs(prefs))
    report = {
        "ok": True,
        "clis": clis,
        "credentials": creds,
        "subscriptions": subs,
        "prefs_path": path or str(prefs_path()),
        "prefs": prefs if write else prefs,
        "known_subscriptions": list(KNOWN_SUBSCRIPTIONS),
        "note": ("Pass --subscriptions cursor,claude,byteplus_coding_plan to record "
                 "active plans. Secrets are never written."),
    }
    if not json_mode:
        report["text"] = render_detection(clis, creds)
        if write:
            report["text"] += f"\n\nWrote prefs: {path}\nSubscriptions recorded: {', '.join(subs) or '(none)'}\n"
    return report
