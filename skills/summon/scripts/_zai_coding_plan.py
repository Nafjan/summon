"""Bounded credential discovery for Z.AI Coding Plan's text endpoint.

The optional direct Z.AI Coding Plan seat is an OpenAI-compatible *text* seat.
Its key is resolved only for that immediate HTTPS request. This module does not
invoke ``chelper``, import its configuration wholesale, mutate an environment,
or serialize a credential/path into a receipt.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path


_MAX_CONFIG_BYTES = 64 * 1024
# A comment starts only after whitespace.  API keys may legally contain '#',
# so treating every hash as a comment silently truncates credentials.
_SCALAR = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)(?:\s+#.*)?\s*$")
_KEY_NAMES = frozenset({"api_key"})
_PLAN_NAMES = frozenset({"plan"})


def _helper_config_path(environ: dict[str, str] | None = None) -> Path:
    """Return one expected helper config path without scanning a home tree."""
    env = environ if environ is not None else os.environ
    override = (env.get("ZAI_CODING_HELPER_CONFIG") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chelper" / "config.yaml"


def _safe_regular_file(path: Path) -> bool:
    """Accept only a bounded, non-symlink regular file."""
    try:
        # Reject a linked helper directory as well as a linked final file.
        # Do not scan the home tree: only walk the deterministic path upward
        # to the resolved home boundary.
        if path.is_symlink() or path.parent.is_symlink():
            return False
        current = path.parent
        home = Path.home().resolve()
        while current != home and current.parent != current:
            if current.is_symlink():
                return False
            current = current.parent
        file_stat = path.stat()
    except OSError:
        return False
    return path.is_file() and 0 < file_stat.st_size <= _MAX_CONFIG_BYTES


def _unquote_scalar(value: str) -> str | None:
    """Accept a conservative plain/single/double-quoted YAML scalar only."""
    value = value.strip()
    if not value or value[0] in "[{:&*!|>" or value.startswith("-"):
        return None
    if value[0] in "\"'":
        if len(value) < 2 or value[-1] != value[0]:
            return None
        value = value[1:-1]
    if "\x00" in value or "\n" in value or "\r" in value:
        return None
    return value.strip() or None


def _read_helper_scalars(path: Path) -> dict[str, str] | None:
    """Read two needed top-level scalar values, never arbitrary YAML."""
    if not _safe_regular_file(path):
        return None
    try:
        # O_NOFOLLOW (where the OS supports it) closes the check/read swap for
        # a final-path symlink. fstat validates the opened descriptor instead
        # of trusting the earlier pathname metadata.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        before = path.stat()
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_size <= 0 or info.st_size > _MAX_CONFIG_BYTES):
                return None
            # Refuse a rename/replacement or size change between pathname
            # inspection and descriptor read. This is not a general file lock;
            # it is a bounded fail-closed guard for a credential source.
            if (before.st_size != info.st_size
                    or before.st_dev != info.st_dev
                    or before.st_ino != info.st_ino):
                return None
            text = handle.read(_MAX_CONFIG_BYTES + 1)
            if len(text.encode("utf-8", errors="replace")) > _MAX_CONFIG_BYTES:
                return None
    except (OSError, UnicodeError):
        return None
    values: dict[str, str] = {}
    saw_key = saw_plan = False
    for raw in text.splitlines():
        # Nested mappings, sequences, tags, and anchors are intentionally out.
        if raw[:1].isspace() or raw.lstrip().startswith(("#", "-", "!", "&", "*")):
            continue
        match = _SCALAR.fullmatch(raw)
        if not match:
            continue
        name, raw_value = match.groups()
        if name not in _KEY_NAMES | _PLAN_NAMES:
            continue
        scalar = _unquote_scalar(raw_value)
        if scalar is not None:
            # Different aliases must not create a last-wins credential or
            # plan selection. An ambiguous helper file is not a credential
            # source for Summon.
            if name in _KEY_NAMES:
                if saw_key:
                    return None
                saw_key = True
            else:
                if saw_plan:
                    return None
                saw_plan = True
            values[name] = scalar
    return values or None


def resolve_zai_coding_api_key(*, environ: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Return ``(key, source)`` without exposing a path or account identity.

    The explicit environment variable wins. The helper fallback requires a
    coding-plan scalar so a generic helper profile cannot be silently reused.
    """
    env = environ if environ is not None else os.environ
    explicit = (env.get("ZAI_CODING_API_KEY") or "").strip()
    if explicit:
        return explicit, "env"
    values = _read_helper_scalars(_helper_config_path(env))
    if not values:
        return None, None
    plan = next((values[name] for name in _PLAN_NAMES if name in values), "")
    normalized_plan = plan.casefold().replace("-", "_")
    # The global coding endpoint is reviewed only for this explicit helper
    # plan. Generic or regional profiles must not be silently repurposed.
    if normalized_plan != "glm_coding_plan_global":
        return None, "helper_not_coding_plan"
    key = next((values[name] for name in _KEY_NAMES if name in values), "")
    return (key or None), ("coding_helper_config" if key else "helper_key_missing")


def zai_coding_plan_status(*, environ: dict[str, str] | None = None) -> dict:
    """Doctor-safe availability summary; no key/path is returned."""
    key, source = resolve_zai_coding_api_key(environ=environ)
    return {
        "env_set": bool((environ or os.environ).get("ZAI_CODING_API_KEY")),
        "resolvable": bool(key),
        "source": source,
        "hint": None if key else "set ZAI_CODING_API_KEY or configure the official Coding Plan helper",
    }
