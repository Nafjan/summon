"""Private, named backend-profile selection.

Profiles deliberately live outside a repository.  An agent definition may name a
profile, but it never contains the profile's path or credentials.  The operator's
private registry maps that name to one narrowly-allowed vendor config directory and
the dispatcher passes the corresponding environment variable to the child only.

This first public slice supports the config-directory variables whose semantics are
well-defined by the bundled backends.  More vendors can be added here once their
profile boundary is measured; arbitrary environment injection is intentionally not
supported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".agents", "summon-profiles.json")

# A profile can redirect a backend's on-disk config/auth home, but cannot inject an
# arbitrary secret-bearing environment variable.  AGY is intentionally absent: summon
# owns its fresh per-call profile and must not be pointed at a roaming auth directory.
PROFILE_ENV_VARS = {
    "claude": "CLAUDE_CONFIG_DIR",
}


def validate_profile_name(name: str) -> str:
    """Validate a registry key, refusing paths and shell metacharacters."""
    value = str(name or "").strip()
    if not value or not PROFILE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"invalid profile name {name!r}; use letters, digits, '.', '_' or '-' "
            "and do not provide a path")
    return value


def registry_path() -> str:
    """Return the private registry path, with a test/operator override."""
    raw = os.environ.get("SUMMON_PROFILES_FILE") or DEFAULT_REGISTRY
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def _read_registry(path: str) -> tuple[dict, str]:
    """Read one registry snapshot and return ``(document, content_sha256)``.

    A missing registry is not an error when no profile was requested; callers only
    invoke this function for an explicit profile.  JSON is used instead of YAML so
    the format stays stdlib-only and cannot execute constructors.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(
            "profile registry is unavailable; create the private registry at "
            f"{path!r} or remove the profile selection") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"profile registry is not valid UTF-8 JSON: {path!r}") from exc
    if not isinstance(document, dict):
        raise ValueError("profile registry root must be a JSON object")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("profile registry must contain an object named 'profiles'")
    return document, hashlib.sha256(raw).hexdigest()


def _inside_dispatch_tree(path: str, cwd: str | None) -> bool:
    """Return whether ``path`` is inside ``cwd`` on the host filesystem.

    Windows paths are case-insensitive.  Comparing the raw ``commonpath`` string
    with an unnormalised cwd let ``C:\\TASK`` and ``C:\\task`` disagree and
    admitted a profile located in the dispatch tree.  ``realpath`` also closes
    the symlink/junction spelling of the same containment check.
    """
    if not cwd:
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        root = os.path.normcase(os.path.realpath(os.path.abspath(cwd)))
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        # Different Windows drives have no common path and therefore cannot be
        # an in-tree profile.
        return False


def _profile_dir(raw: object, cwd: str | None) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("profile config_dir must be a non-empty string")
    # The registry is operator-owned, but paths still fail closed: no relative path,
    # no NUL/newline, and no profile hidden inside the task tree where project content
    # could replace auth/config files.
    value = os.path.expandvars(os.path.expanduser(raw.strip()))
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("profile config_dir contains a control character")
    if not os.path.isabs(value):
        raise ValueError("profile config_dir must be an absolute path")
    try:
        resolved = str(Path(value).resolve(strict=True))
    except OSError as exc:
        raise ValueError("profile config_dir does not exist") from exc
    if not os.path.isdir(resolved):
        raise ValueError("profile config_dir is not a directory")
    if _inside_dispatch_tree(resolved, cwd):
        raise ValueError("profile config_dir must be outside the dispatch cwd")
    return resolved, hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def _profile_command(raw: object, cli: str, cwd: str | None) -> tuple[str | None, str | None]:
    """Resolve an optional pinned executable without allowing command injection."""
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("profile command must be a non-empty absolute path")
    value = os.path.expandvars(os.path.expanduser(raw.strip()))
    if "\x00" in value or "\r" in value or "\n" in value or not os.path.isabs(value):
        raise ValueError("profile command must be an absolute path without control characters")
    try:
        resolved = str(Path(value).resolve(strict=True))
    except OSError as exc:
        raise ValueError("profile command does not exist") from exc
    if not os.path.isfile(resolved):
        raise ValueError("profile command is not a file")
    # Popen receives the path directly (no shell), so Windows batch shims are not
    # portable executable pins.  Require the real binary (or an extensionless
    # POSIX executable) and let the caller keep vendor shims on PATH if desired.
    allowed = {cli.lower(), f"{cli.lower()}.exe"}
    if os.path.basename(resolved).lower() not in allowed:
        raise ValueError(f"profile command must be the {cli} executable")
    if _inside_dispatch_tree(resolved, cwd):
        raise ValueError("profile command must be outside the dispatch cwd")
    return resolved, hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def resolve_profile(name: str | None, cli: str, cwd: str | None = None) -> dict | None:
    """Resolve a named profile for ``cli``.

    Returns a private selection record with ``env`` for the child and a path digest
    suitable for request identity/receipts.  The absolute path stays out of the record
    consumed by envelope serializers; only the builder receives it as an env value.
    """
    if not name:
        return None
    profile_name = validate_profile_name(name)
    env_var = PROFILE_ENV_VARS.get(cli)
    if not env_var:
        raise ValueError(
            f"profiles are not supported for backend {cli!r}; use its native isolation")
    document, registry_sha = _read_registry(registry_path())
    entry = document["profiles"].get(profile_name)
    if not isinstance(entry, dict):
        raise ValueError(f"profile {profile_name!r} is not defined in the private registry")
    entry_cli = entry.get("cli", entry.get("backend", cli))
    if not isinstance(entry_cli, str) or entry_cli != cli:
        raise ValueError(
            f"profile {profile_name!r} targets {entry_cli!r}, not the resolved backend {cli!r}")
    path, path_sha = _profile_dir(entry.get("config_dir"), cwd)
    command, command_sha = _profile_command(entry.get("command"), cli, cwd)
    models = entry.get("models")
    if models is not None:
        if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
            raise ValueError(f"profile {profile_name!r} models must be a list of strings")
    return {
        "name": profile_name,
        "cli": cli,
        "env_var": env_var,
        "env": {env_var: path},
        "path_sha256": path_sha,
        "command": command,
        "command_sha256": command_sha,
        "registry_sha256": registry_sha[:32],
        "models": tuple(models or ()),
    }


def validate_model(profile: dict | None, model: str | None) -> None:
    """Reject a profile explicitly restricted to models it can authenticate."""
    if not profile or not profile.get("models") or not model:
        return
    if model not in profile["models"]:
        raise ValueError(
            f"profile {profile['name']!r} is restricted to its configured models; "
            f"{model!r} is not allowed")
