#!/usr/bin/env python3
"""Machine-checkable version and migration contract for a Summon release.

The release version is intentionally read from the plugin metadata and then
cross-checked against every shipped executable companion.  A release evidence
file must bind this result to the source tree; a hand-edited version string in
one launcher must never be enough to claim a release.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
_VERSION_FILES = {
    "plugin": Path("plugin.json"),
    "dispatcher": Path("skills/summon/scripts/run_subagent.py"),
    "telemetry": Path("skills/summon/scripts/_telemetry.py"),
    "mcp_server": Path("skills/summon/scripts/mcp_server.py"),
}


def _module_string_assignment(path: Path, name: str) -> str | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None
    value: str | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                value = node.value.value if isinstance(node.value.value, str) else None
    return value


def version_facts(root: Path = ROOT) -> dict[str, object]:
    """Return canonical versions and deterministic mismatch diagnostics."""
    versions: dict[str, str | None] = {}
    try:
        value = json.loads((root / _VERSION_FILES["plugin"]).read_text(encoding="utf-8"))
        versions["plugin"] = value.get("version") if isinstance(value, Mapping) else None
    except (OSError, ValueError, TypeError, UnicodeError):
        versions["plugin"] = None
    versions["dispatcher"] = _module_string_assignment(
        root / _VERSION_FILES["dispatcher"], "__version__"
    )
    versions["telemetry"] = _module_string_assignment(
        root / _VERSION_FILES["telemetry"], "SUMMON_VERSION"
    )
    versions["mcp_server"] = _module_string_assignment(
        root / _VERSION_FILES["mcp_server"], "SERVER_VERSION"
    )
    canonical = versions.get("plugin")
    errors: list[str] = []
    if not isinstance(canonical, str) or not _SEMVER.fullmatch(canonical):
        errors.append("plugin version is missing or not semantic versioning")
        canonical = None
    for name, version in versions.items():
        if version is None:
            errors.append(f"{name} version is missing")
        elif canonical is not None and version != canonical:
            errors.append(f"{name} version {version!r} differs from {canonical!r}")
    return {
        "canonical": canonical,
        "versions": dict(sorted(versions.items())),
        "converged": not errors,
        "errors": errors,
    }


def assert_version_contract(root: Path = ROOT) -> dict[str, object]:
    facts = version_facts(root)
    if not facts["converged"]:
        raise ValueError("; ".join(facts["errors"]))
    return facts


def migration_contract(root: Path = ROOT) -> dict[str, object]:
    """Read the required 2.x -> 3.x migration contract without executing it."""
    path = root / "docs" / "VERSIONING_AND_3.0.md"
    required = (
        "2.x -> 3.0",
        "## Compatibility boundary",
        "## Upgrade procedure",
        "## Rollback procedure",
        "## Release decision",
        "rollback",
        "envelope: 1",
        "telemetry",
        "managed installs",
    )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"path": path.relative_to(root).as_posix(), "present": False,
                "complete": False, "missing": list(required)}
    missing = [phrase for phrase in required if phrase not in text]
    return {"path": path.relative_to(root).as_posix(), "present": True,
            "complete": not missing, "missing": missing}


def release_contract(root: Path = ROOT) -> dict[str, object]:
    versions = version_facts(root)
    migration = migration_contract(root)
    return {
        "schema": 1,
        "version": versions,
        "migration": migration,
        "ready": bool(versions["converged"] and migration["complete"]),
    }


if __name__ == "__main__":
    print(json.dumps(release_contract(), ensure_ascii=False, indent=2, sort_keys=True))
