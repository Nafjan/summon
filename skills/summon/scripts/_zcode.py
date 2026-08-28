"""Provider-inert Z.AI ZCode CLI discovery and result parsing.

ZCode ships its command line interface inside the desktop application on some
platforms.  This module deliberately knows only how to locate that local CLI
and parse its documented ``--json`` result.  It never reads ZCode
configuration, credentials, account identifiers, or usage data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path


_BUNDLE_RELATIVE = ("resources", "glm", "zcode.cjs")
_MAX_JSON_OUTPUT = 1_048_576
_SESSION_RE = re.compile(r"^sess_[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class ZCodeTarget:
    """A directly executable ZCode CLI target.

    ``source`` is intentionally a coarse install class, not a path.  Callers
    can expose it in diagnostics without publishing a machine-specific route.
    """

    command: str
    prefix_args: tuple[str, ...]
    source: str


def _is_file(path: str | os.PathLike[str] | None) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _target(path: str, source: str, *, which=shutil.which) -> ZCodeTarget | None:
    """Return a launch target without ever shelling out through a command shim."""
    if not _is_file(path):
        return None
    suffix = Path(path).suffix.lower()
    if suffix in {".js", ".cjs", ".mjs"}:
        node = which("node")
        if not node:
            return None
        return ZCodeTarget(command=node, prefix_args=(str(path),), source=source)
    # A .cmd wrapper reparses arguments on Windows.  Summon keeps prompt text
    # off argv, but a direct executable/bundle is still required for a truthful
    # headless integration; discovery continues to the bundled CLI instead.
    if suffix in {".cmd", ".bat", ".ps1"}:
        return None
    return ZCodeTarget(command=str(path), prefix_args=(), source=source)


def _registry_install_locations() -> list[str]:
    """Read Windows uninstall records for ZCode without touching user config."""
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore[attr-defined]
    except ImportError:
        return []
    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    locations: list[str] = []
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, name) as item:
                            display, _ = winreg.QueryValueEx(item, "DisplayName")
                            if not isinstance(display, str) or "zcode" not in display.casefold():
                                continue
                            try:
                                location, _ = winreg.QueryValueEx(item, "InstallLocation")
                            except OSError:
                                location = None
                            if isinstance(location, str) and location.strip():
                                locations.append(location.strip())
                                continue
                            # InstallLocation is optional for desktop apps.
                            # DisplayIcon/UninstallString are treated only as
                            # local file hints; no command text is ever run.
                            for value_name in ("DisplayIcon", "UninstallString"):
                                try:
                                    raw, _ = winreg.QueryValueEx(item, value_name)
                                except OSError:
                                    continue
                                if not isinstance(raw, str) or not raw.strip():
                                    continue
                                candidate = raw.strip().strip('"')
                                # DisplayIcon may append `,0`; UninstallString
                                # may include arguments. Keep only a quoted or
                                # executable-looking local prefix and validate
                                # its parent by the bundle file below.
                                candidate = candidate.split(",", 1)[0].strip('" ')
                                lowered = candidate.casefold()
                                marker = lowered.find(".exe")
                                if marker >= 0:
                                    candidate = candidate[:marker + 4]
                                if candidate.casefold().endswith(".exe"):
                                    locations.append(str(Path(candidate).parent))
                                    break
                    except OSError:
                        continue
        except OSError:
            continue
    return locations


def _bundle_candidates() -> list[tuple[str, str]]:
    """Return reviewed app-bundle locations, with no private-config lookup."""
    candidates: list[tuple[str, str]] = []
    if os.name == "nt":
        for root in _registry_install_locations():
            candidates.append((str(Path(root).joinpath(*_BUNDLE_RELATIVE)), "windows_registry"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append((str(Path(local, "Programs", "ZCode").joinpath(*_BUNDLE_RELATIVE)),
                               "windows_bundle"))
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append((str(Path(root, "ZCode").joinpath(*_BUNDLE_RELATIVE)),
                                   "windows_bundle"))
    elif os.name == "posix":
        if os.sys.platform == "darwin":
            candidates.extend([
                ("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs", "macos_bundle"),
                (str(Path.home() / "Applications" / "ZCode.app" / "Contents" / "Resources" /
                     "glm" / "zcode.cjs"), "macos_bundle"),
            ])
    return candidates


def resolve_zcode_cli(*, environ: dict[str, str] | None = None,
                      which=shutil.which) -> ZCodeTarget | None:
    """Resolve ZCode: explicit ``ZCODE_CLI``, PATH, registry, then bundles.

    An explicit environment override is useful for portable/CI installations.
    It is interpreted as a local file path only; no shell command parsing is
    performed.
    """
    env = os.environ if environ is None else environ
    override = env.get("ZCODE_CLI")
    if isinstance(override, str) and override.strip():
        return _target(os.path.expandvars(os.path.expanduser(override.strip())), "env", which=which)
    path_hit = which("zcode") or (which("zcode.exe") if os.name == "nt" else None)
    target = _target(path_hit, "path", which=which) if path_hit else None
    if target is not None:
        return target
    for candidate, source in _bundle_candidates():
        target = _target(candidate, source, which=which)
        if target is not None:
            return target
    return None


def zcode_version(target: ZCodeTarget, *, timeout: int = 15) -> str | None:
    """Best-effort local binary-version check; it makes no model request."""
    try:
        from _spawn import run_flags
        result = subprocess.run(
            [target.command, *target.prefix_args, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, **run_flags())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines()
             if line.strip()]
    return lines[0][:120] if lines else None


def parse_zcode_json_output(raw: str) -> dict | None:
    """Parse ZCode's single JSON result after a possible stdout banner.

    The CLI currently emits one document at completion but a dependency can
    print a non-JSON banner first.  We accept only a bounded object beginning
    at the first opening brace and never treat arbitrary prose as structured
    success.
    """
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > _MAX_JSON_OUTPUT:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(raw[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_text(value: object, maximum: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:maximum] if text else None


def _usage(value: object) -> dict | None:
    """Normalize bounded token counters without retaining provider metadata."""
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "input"),
        "output_tokens": ("output_tokens", "outputTokens", "output"),
        "total_tokens": ("total_tokens", "totalTokens", "total"),
        "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens", "cache_read"),
    }
    normalized: dict[str, int] = {}
    for target, names in aliases.items():
        raw = next((value[name] for name in names if name in value), None)
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= (1 << 63) - 1:
            normalized[target] = raw
    return normalized or None


def zcode_result_fields(value: dict | None) -> dict:
    """Extract a verified minimal terminal ZCode result shape.

    A random JSON object or a banner-adjacent diagnostic is not terminal
    completion evidence. Current ZCode JSON output is accepted only when it
    carries a nonempty bounded response and a stable ``sess_`` identifier.
    Usage is optional telemetry, never a completion substitute.
    """
    if not isinstance(value, dict):
        return {"response": "", "session_id": None, "usage": None,
                "context_window": None, "parse_ok": False}
    response = value.get("response")
    if (not isinstance(response, str) or not response.strip()
            or len(response.encode("utf-8", errors="replace")) > _MAX_JSON_OUTPUT):
        return {"response": "", "session_id": None, "usage": None,
                "context_window": None, "parse_ok": False}
    session = _bounded_text(value.get("sessionId"), 256)
    if session is None or not _SESSION_RE.fullmatch(session):
        return {"response": "", "session_id": None, "usage": None,
                "context_window": None, "parse_ok": False}
    usage = _usage(value.get("usage"))
    projection = value.get("projection") if isinstance(value.get("projection"), dict) else {}
    context_window = projection.get("contextWindow")
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window < 0:
        context_window = None
    return {
        "response": response,
        "session_id": session,
        "usage": usage,
        "context_window": context_window,
        "parse_ok": True,
    }
