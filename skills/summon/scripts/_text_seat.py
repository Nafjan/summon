"""Text-seat honesty: refuse chat-only backends unless the caller opts in.

``openai-compat`` and Summon's ``arkcli`` path are single-shot text seats (no
FS / tool loop). Hosts historically dispatched repo work there; models either
BLOCKED or invented. This module classifies those seats, builds a machine-
readable recovery package, and decides block vs allow.

Opt-in (any one): ``--allow-text-only``, ``SUMMON_ALLOW_TEXT_ONLY=1``, or agent
frontmatter ``capability: text-only``. Opt-in still emits a loud warning every
time (anti rubber-stamp). ``--require-tools`` / ``SUMMON_REQUIRE_TOOLS=1``
refuses even with opt-in.

Never auto-reroutes and never auto-retries with the flag — hosts must get
fresh consent before a new dispatch.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

# Seats that cannot enforce a tool/FS loop under Summon today.
TEXT_SEAT_CLIS = frozenset({"openai-compat", "arkcli"})

# Toolful CLIs we may suggest (presence-checked). Not text seats.
_TOOLFUL_CLIS = (
    ("agy", "toolful agent loop with repo FS"),
    ("claude", "toolful CLI with enforceable permission tiers"),
    ("codex", "toolful CLI with workspace sandbox"),
    ("cursor-agent", "toolful IDE agent loop"),
    ("kimi", "toolful CLI (yolo-only in prompt mode)"),
    ("gemini", "toolful CLI (frozen/ineligible for many accounts)"),
)

BLOCKED_REASON = "text_seat_no_tools"
POLICY = "text_seat"

_WARNING = (
    "TEXT SEAT: this backend has no filesystem or tool loop "
    "(openai-compat / arkcli +chat). Opt-in accepted for this dispatch; "
    "paste needed context into the prompt. Do not treat ambient "
    "SUMMON_ALLOW_TEXT_ONLY=1 as a standing waiver for toolful work. "
    "For repo edits, re-dispatch to a toolful CLI "
    "(see text_seat.suggested_reroutes)."
)

_HINT = (
    "Re-dispatch with --allow-text-only if one-shot text is enough "
    "(paste needed files into the prompt); otherwise use a toolful CLI from "
    "suggested_reroutes. Do not auto-retry with the flag."
)


def is_text_seat(cli: str | None) -> bool:
    return (cli or "").strip().lower() in TEXT_SEAT_CLIS


def _capability_values(raw: Any) -> list[str]:
    """Normalize frontmatter ``capability`` to a list of lowercase strings."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip().lower() for x in raw if str(x).strip()]
    text = str(raw).strip().lower()
    if not text:
        return []
    # YAML may leave a single scalar; also accept comma-separated.
    if "," in text:
        return [p.strip() for p in text.split(",") if p.strip()]
    return [text]


def capability_text_only(agent_file: str | None) -> bool:
    """True when agent frontmatter declares ``capability: text-only``."""
    if not agent_file:
        return False
    try:
        from _loader import parse_frontmatter
        with open(agent_file, encoding="utf-8-sig") as fh:
            fm, _ = parse_frontmatter(fh.read())
        if not isinstance(fm, dict):
            return False
        return "text-only" in _capability_values(fm.get("capability"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError, ImportError):
        # Soft: a unreadable/malformed definition must not crash dispatch; treat
        # as no capability opt-in. Do not swallow programming errors (AttributeError…).
        return False


def require_tools(*, flag: bool = False) -> bool:
    return bool(flag) or os.environ.get("SUMMON_REQUIRE_TOOLS") == "1"


def text_only_opted_in(*, flag: bool = False, agent_file: str | None = None,
                       capability: bool | None = None) -> bool:
    """Caller accepted a text-only conversion for this dispatch."""
    if flag or os.environ.get("SUMMON_ALLOW_TEXT_ONLY") == "1":
        return True
    if capability is not None:
        return bool(capability)
    return capability_text_only(agent_file)


def suggested_reroutes() -> list[dict[str, str]]:
    """Installed toolful CLIs only (PATH presence; no live probes)."""
    out: list[dict[str, str]] = []
    for cli, reason in _TOOLFUL_CLIS:
        found = bool(shutil.which(cli))
        if not found and os.name == "nt":
            found = bool(shutil.which(f"{cli}.cmd"))
        if found:
            out.append({"cli": cli, "reason": reason})
    return out


def text_seat_info(
    *,
    cli: str,
    allowed: bool,
    would_block: bool,
    capability: bool = False,
) -> dict[str, Any]:
    """Additive ``text_seat`` object for dry-run and live envelopes."""
    return {
        "policy": POLICY,
        "no_tools": True,
        "no_filesystem": True,
        "allowed": bool(allowed),
        "would_block": bool(would_block),
        "opt_in": [
            "--allow-text-only",
            "SUMMON_ALLOW_TEXT_ONLY=1",
            "capability: text-only",
        ],
        "capability_text_only": bool(capability),
        "suggested_reroutes": suggested_reroutes(),
        "hint": _HINT,
    }


def evaluate_text_seat(
    *,
    cli: str,
    allow_text_only: bool = False,
    require_tools_flag: bool = False,
    agent_file: str | None = None,
) -> dict[str, Any] | None:
    """Return a decision dict for text seats, or None when the CLI is toolful.

    Keys: ``allowed``, ``would_block``, ``text_seat``, ``warning`` (when allowed).
    """
    if not is_text_seat(cli):
        return None
    cap = capability_text_only(agent_file)
    hard = require_tools(flag=require_tools_flag)
    opted = text_only_opted_in(
        flag=allow_text_only, agent_file=agent_file, capability=cap)
    allowed = bool(opted) and not hard
    would_block = not allowed
    info = text_seat_info(
        cli=cli, allowed=allowed, would_block=would_block, capability=cap)
    if hard:
        info["require_tools"] = True
    out: dict[str, Any] = {
        "allowed": allowed,
        "would_block": would_block,
        "text_seat": info,
    }
    if allowed:
        out["warning"] = _WARNING
    return out


def fanout_allows_text_seat() -> bool:
    """Council/manifest may use text seats only with deliberate env opt-in.

    ``capability: text-only`` and ``--allow-text-only`` authorize a *single*
    dispatch. Fan-out must set ``SUMMON_ALLOW_TEXT_ONLY=1`` so a house chat
    agent cannot silently join a council/manifest without that consent.
    ``SUMMON_REQUIRE_TOOLS=1`` refuses fan-out text seats even when allow is set.
    """
    if require_tools():
        return False
    return os.environ.get("SUMMON_ALLOW_TEXT_ONLY") == "1"


def fanout_text_seat_refusal(agent: str, cli: str) -> str:
    """Human-readable council/manifest refusal for a text-seat agent."""
    return (
        f"council/manifest refuses text-seat agent {agent!r} (cli={cli}): "
        f"no filesystem/tools. Set SUMMON_ALLOW_TEXT_ONLY=1 to allow pure-text "
        f"fan-out deliberately; capability: text-only / --allow-text-only alone "
        f"are single-dispatch only. Prefer a toolful CLI for repo deliberation."
    )


def blocked_envelope(*, agent: str, cli: str, text_seat: dict) -> dict:
    """Pre-dispatch refusal: structural block, never billed.

    ``exit_code`` is 0 to match gate denials (``_gate.blocked_envelope``): the
    dispatch was prevented, not attempted-and-broken. Hosts must branch on
    ``status: blocked`` / ``blocked_reason``, not on exit code alone.
    """
    return {
        "result": "",
        "status": "blocked",
        "exit_code": 0,
        "cli": cli,
        "agent": agent,
        "error": None,
        "blocked_reason": BLOCKED_REASON,
        "text_seat": text_seat,
        "warnings": [
            "refused text-seat dispatch (no FS/tools). "
            "Opt in with --allow-text-only for one-shot text, or use a toolful CLI. "
            "Do not auto-retry with --allow-text-only."
        ],
    }
