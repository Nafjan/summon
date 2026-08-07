"""T3 Code harness detection (Summon-side only — no upstream T3 dependency).

T3 Code (https://t3.codes) is an agent control plane that drives CLIs you already
pay for (Claude Code, Codex, Cursor, …). It does not ship its own skill root;
it discovers Claude skills under ``~/.claude/skills`` and Codex skills under
``~/.codex/skills`` (and project ``.claude`` / ``.agents`` trees).

Summon "supports T3 Code" by installing into those provider skill roots and by
surfacing readiness in ``doctor`` / ``onboard``. This module never talks to the
T3 app process and never prints expanded home paths (portable labels only).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

# Providers T3 currently documents as driveable that also load file-based skills
# Summon installs into. (Grok Build / OpenCode are T3 providers but not Summon
# install hosts today.)
T3_SKILL_HOSTS = ("claude", "codex", "cursor")

# PATH binaries that prove a T3-driveable provider is available on this machine.
T3_PROVIDER_BINARIES = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("cursor", "cursor-agent"),
    ("grok", "grok"),
    ("opencode", "opencode"),
)

_T3_MARKER = ".t3"  # userdata/caches live here after a desktop/web install


def t3_home_marker(home: str | None = None) -> Path:
    """``~/.t3`` (or ``$HOME/.t3`` under a fake HOME in tests)."""
    root = Path(home) if home else Path.home()
    return root / _T3_MARKER


def t3_detected(home: str | None = None) -> bool:
    """True when the local T3 userdata directory exists."""
    return t3_home_marker(home).is_dir()


def _skill_present(host_root: Path) -> bool:
    return (host_root / "skills" / "summon" / "SKILL.md").is_file()


def skill_visibility(home: str | None = None) -> dict[str, bool]:
    """Whether Summon's skill is installed where T3's provider pickers look."""
    root = Path(home) if home else Path.home()
    return {
        "claude": _skill_present(root / ".claude"),
        "codex": _skill_present(root / ".codex"),
        "cursor": _skill_present(root / ".cursor"),
    }


def providers_on_path() -> dict[str, bool]:
    """Which T3-driveable provider CLIs are on PATH (portable; no paths)."""
    out: dict[str, bool] = {}
    for label, binary in T3_PROVIDER_BINARIES:
        found = bool(shutil.which(binary))
        if not found and os.name == "nt":
            found = bool(shutil.which(f"{binary}.cmd"))
        out[label] = found
    return out


def t3_status(home: str | None = None) -> dict[str, Any]:
    """Structured T3 readiness for ``doctor --json`` / onboard.

    Paths are never included — only portable labels and booleans.
    """
    detected = t3_detected(home)
    skills = skill_visibility(home)
    providers = providers_on_path()
    skill_hosts_ready = [h for h, ok in skills.items() if ok]
    # Ready = T3 present AND at least one skill host has Summon AND that
    # provider binary is on PATH (so a T3 session can actually invoke it).
    paired = (
        (skills.get("claude") and providers.get("claude"))
        or (skills.get("codex") and providers.get("codex"))
        or (skills.get("cursor") and providers.get("cursor"))
    )
    if not detected:
        hint = (
            "T3 Code not detected (~/.t3 absent). Install from https://t3.codes, "
            "then: python install.py --profile t3"
        )
    elif paired:
        hint = (
            "T3 Code + Summon skill + a driveable provider CLI are all present. "
            "In T3, open a Claude or Codex thread and run summon (doctor first). "
            "See skills/summon/references/t3-code.md"
        )
    elif skill_hosts_ready:
        missing = [h for h in skill_hosts_ready if not providers.get(h)]
        hint = (
            "T3 Code detected and Summon is installed, but the matching provider "
            f"CLI is not on PATH ({', '.join(missing) or 'unknown'}). Install/"
            "sign into that CLI, then reopen a T3 session. See "
            "skills/summon/references/t3-code.md"
        )
    else:
        hint = (
            "T3 Code detected. Install Summon into Claude/Codex/Cursor skill roots "
            "with: python install.py --profile t3   then open a Claude or Codex "
            "session in T3 and invoke $summon / ask for summon. See "
            "skills/summon/references/t3-code.md"
        )
    return {
        "detected": detected,
        "marker": f"~/{_T3_MARKER}",
        "skill_hosts": skills,
        "skill_hosts_with_summon": skill_hosts_ready,
        "providers_on_path": providers,
        "ready": bool(detected and paired),
        "profile_hosts": list(T3_SKILL_HOSTS),
        "hint": hint,
    }
