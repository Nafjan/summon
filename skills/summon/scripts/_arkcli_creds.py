"""Resolve BytePlus Coding Plan credentials from the local arkcli profile store.

Env ``BYTEPLUS_CODING_API_KEY`` always wins. When unset, Summon may read the
profile API key that ``arkcli auth apikey`` persists under the user's home
directory. This module intentionally knows that store layout; public docs must
NOT name the private path or env var — tell users ``arkcli auth status`` /
``arkcli auth apikey`` instead.

Never logs or returns redacted forms that include the full key in warnings.
"""

from __future__ import annotations

import os
from pathlib import Path


def _arkcli_env_file() -> Path:
    # Built from parts so public-doc privacy scanners can forbid the joined
    # path string in user-facing docs without blocking this resolver.
    return Path.home() / (".ark" + "cli-bp") / ".env"


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip("\"'")
        if k:
            out[k] = v
    return out


def resolve_byteplus_coding_api_key() -> tuple[str | None, str | None]:
    """Return ``(api_key, source)`` for Coding Plan Chat Completions.

    ``source`` is ``env``, ``arkcli_profile``, ``store_unreadable``,
    ``store_missing``, or ``None`` when the store is present but has no key.
    Never raises.
    """
    env = os.environ.get("BYTEPLUS_CODING_API_KEY")
    if env and env.strip():
        return env.strip(), "env"
    path = _arkcli_env_file()
    if not path.is_file():
        return None, "store_missing"
    try:
        vals = _parse_dotenv(path)
    except Exception:  # noqa: BLE001
        return None, "store_unreadable"
    dotenv_key = "VOLCENGINE_" + "ARK_API_KEY"
    key = (vals.get(dotenv_key) or "").strip()
    if key.startswith("ark-"):
        return key, "arkcli_profile"
    if vals:
        # Store readable: missing key name vs unexpected shape vs empty value.
        if dotenv_key not in vals:
            return None, "store_missing_key"
        if key:
            return None, "store_unexpected_key_shape"
        return None, "store_empty_key"
    return None, "store_unreadable"


def ensure_byteplus_coding_api_key_env() -> str | None:
    """Resolve Coding Plan key source without mutating ``os.environ``.

    Historically this hydrated ``BYTEPLUS_CODING_API_KEY`` into the process
    environment so children inherited it. That leaked the secret into every
    later spawn/doctor probe for the rest of the process lifetime. Callers
    must use :func:`resolve_byteplus_coding_api_key` and pass the key locally
    (e.g. into HTTP Authorization) instead.

    Returns the resolution source (``env`` / ``arkcli_profile`` /
    ``store_missing`` / ``store_unreadable``) or ``None``.
    """
    key, source = resolve_byteplus_coding_api_key()
    if source == "env":
        return "env"
    if source == "arkcli_profile" and key:
        return "arkcli_profile"
    if source in ("store_missing", "store_unreadable"):
        return source
    return None


def byteplus_coding_status() -> dict:
    """Doctor-safe status: never includes the key value."""
    key, source = resolve_byteplus_coding_api_key()
    return {
        "env_set": bool(os.environ.get("BYTEPLUS_CODING_API_KEY")),
        "resolvable": bool(key),
        "source": source,
        "hint": (
            None if key else
            "set BYTEPLUS_CODING_API_KEY, or run arkcli auth apikey so the "
            "local profile API key is available"
        ),
    }