"""Best-effort local Nous/Hermes credential lookup.

The Nous key is intentionally read only from an explicitly named Hermes profile
file (or an operator-supplied override). The value is returned only to the
immediate provider call; this module never logs, serializes, or exposes the key
in a receipt.
"""

from __future__ import annotations

import os
from pathlib import Path


_MAX_ENV_BYTES = 256 * 1024


def _candidate_paths() -> list[Path]:
    """Return deterministic local profile candidates without scanning the home tree."""
    out: list[Path] = []
    for name in ("SUMMON_NOUS_ENV", "HERMES_NOUS_ENV"):
        value = os.environ.get(name)
        if value:
            out.append(Path(value).expanduser())
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        out.append(Path(local_app_data) / "hermes" / "profiles" / "main" / "nous.env")
    home = Path.home()
    out.append(home / "AppData" / "Local" / "hermes" / "profiles" / "main" / "nous.env")
    out.append(home / ".config" / "hermes" / "profiles" / "main" / "nous.env")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in out:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _parse_key(path: Path) -> str | None:
    """Read only ``NOUS_API_KEY`` from a bounded dotenv-style file."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ENV_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if separator != "=" or name.strip() != "NOUS_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = value.strip()
        return value or None
    return None


def resolve_nous_api_key() -> tuple[str | None, str | None]:
    """Return ``(key, source_tag)`` without returning a profile path."""
    for path in _candidate_paths():
        key = _parse_key(path)
        if key:
            return key, "hermes_profile"
    return None, None
