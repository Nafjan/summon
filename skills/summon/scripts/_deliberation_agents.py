"""Provider-inert custom-agent package discovery for deliberation.

Packages are read-only definitions at ``<root>/<slug>/agent.md``.  Workspace
discovery uses ``.agents/agents``; a global root participates only when the
caller supplies it explicitly.  This module never resolves an executable,
imports a provider, creates a worktree, or launches a process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


SCHEMA_VERSION = 1
MAX_AGENT_BYTES = 64 * 1024
MAX_FRONTMATTER_LINES = 64
MAX_SCALAR_CHARS = 4096
MAX_SKILLS = 32
_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DIRECTIVE = re.compile(
    r"(?im)^\s*(?:run-agent|executable|command|args|permission|permission_ceiling|"
    r"authority_class|consent_class|worktree_mode|cli|transport)\s*:")
_FIELDS = frozenset({
    "schema_version", "name", "description", "role", "model", "cli",
    "transport", "permission_ceiling", "authority_class", "skills",
    "consent_class", "worktree_mode",
})
_PERMISSIONS = ("read-only", "safe-edit", "yolo")
_CLIS = frozenset({"claude", "codex", "cursor-agent", "gemini", "agy", "kimi"})
_AUTHORITY = {
    "contained": ("none", "none"),
    "workspace-write": ("none", "required"),
    "full-bypass": ("full-authority", "required"),
}


class AgentManifestError(ValueError):
    """A custom-agent package is malformed, ambiguous, or unsafe."""


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AgentManifestError("agent definition is not canonical JSON") from exc


def _scalar(raw: str) -> str:
    value = raw.strip()
    if ((value.startswith("'") and not value.endswith("'"))
            or (value.startswith('"') and not value.endswith('"'))
            or (value.endswith("'") and not value.startswith("'"))
            or (value.endswith('"') and not value.startswith('"'))):
        raise AgentManifestError("agent frontmatter contains an unmatched quote")
    if (len(value) >= 2 and value[0] == value[-1]
            and value[0] in {"'", '"'}):
        value = value[1:-1]
    if (not value or len(value) > MAX_SCALAR_CHARS or "\x00" in value
            or value.startswith(("|", ">", "&", "*", "!", "{"))):
        raise AgentManifestError("agent frontmatter scalar is invalid")
    return value


def _skills(raw: str) -> tuple[str, ...]:
    value = raw.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise AgentManifestError("skills must be an inline identifier list")
    inner = value[1:-1].strip()
    values = [] if not inner else [_scalar(part) for part in inner.split(",")]
    if (len(values) > MAX_SKILLS or len(set(values)) != len(values)
            or any(not _ID.fullmatch(item) for item in values)):
        raise AgentManifestError("skills contain invalid or duplicate identifiers")
    return tuple(values)


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentManifestError("agent.md requires frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1)
                   if line.strip() == "---")
    except StopIteration as exc:
        raise AgentManifestError("agent.md frontmatter is unterminated") from exc
    if end - 1 > MAX_FRONTMATTER_LINES:
        raise AgentManifestError("agent frontmatter has too many lines")
    result: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() or ":" not in line:
            raise AgentManifestError("agent frontmatter must be flat key-value YAML")
        key, raw = line.split(":", 1)
        key = key.strip()
        if key not in _FIELDS:
            raise AgentManifestError("agent frontmatter contains an unknown field")
        if key in result:
            raise AgentManifestError("agent frontmatter contains a duplicate field")
        result[key] = _skills(raw) if key == "skills" else _scalar(raw)
    missing = _FIELDS - set(result)
    if missing:
        raise AgentManifestError("agent frontmatter is missing required fields")
    body = "\n".join(lines[end + 1:])
    if not body.strip():
        raise AgentManifestError("agent body must be non-empty untrusted text")
    if _DIRECTIVE.search(body):
        raise AgentManifestError("agent body contains an authority directive")
    return result, body


def _safe_path(path: Path) -> tuple[Path, str]:
    if path.name != "agent.md" or not _ID.fullmatch(path.parent.name):
        raise AgentManifestError("agent package path is invalid")
    try:
        if _is_reparse_point(path) or _is_reparse_point(path.parent):
            raise AgentManifestError("agent package symlinks are refused")
        resolved = path.resolve(strict=True)
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise AgentManifestError("agent package cannot be resolved") from exc
    if resolved.parent != parent or not resolved.is_file():
        raise AgentManifestError("agent package escaped its directory")
    return resolved, path.parent.name


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points.

    ``Path.is_symlink`` does not report directory junctions on Windows.  A
    junction is an equally unsafe discovery boundary because its resolved
    files can live outside the selected workspace/global root.
    """
    try:
        isjunction = getattr(os.path, "isjunction", None)
        if path.is_symlink() or bool(isjunction(path) if isjunction else False):
            return True
        # Python 3.10/3.11 do not expose os.path.isjunction, but Windows
        # still reports FILE_ATTRIBUTE_REPARSE_POINT through stat().
        attributes = getattr(path.stat(follow_symlinks=False),
                             "st_file_attributes", 0)
        return bool(attributes & 0x400)
    except (OSError, ValueError):
        return True


@dataclass(frozen=True)
class FrozenAgent:
    """Canonical package snapshot; body/path remain internal-only data."""

    name: str
    description: str
    role: str
    model: str
    cli: str
    transport: str
    permission_ceiling: str
    authority_class: str
    skills: tuple[str, ...]
    consent_class: str
    worktree_mode: str
    definition_digest: str
    source_digest: str
    _body: str
    _path: str

    @property
    def body(self) -> str:
        return self._body

    @property
    def path(self) -> str:
        return self._path

    def as_dict(self, *, internal: bool = False) -> dict[str, object]:
        value = {
            "schema_version": SCHEMA_VERSION,
            "name_sha256": _digest_text(self.name),
            "description_sha256": _digest_text(self.description),
            "role_sha256": _digest_text(self.role),
            "model_sha256": _digest_text(self.model),
            "cli": self.cli, "transport": self.transport,
            "permission_ceiling": self.permission_ceiling,
            "authority_class": self.authority_class,
            "skills_sha256": [_digest_text(item) for item in self.skills],
            "consent_class": self.consent_class,
            "worktree_mode": self.worktree_mode,
            "definition_digest": self.definition_digest,
            "source_digest": self.source_digest,
        }
        if internal:
            value.update({"name": self.name, "description": self.description,
                          "role": self.role, "model": self.model,
                          "skills": list(self.skills), "body": self._body,
                          "path": self._path})
        return value

    def canonical_json(self, *, internal: bool = False) -> str:
        return _canonical(self.as_dict(internal=internal)).decode("utf-8")


def load_agent(path: str | os.PathLike[str]) -> FrozenAgent:
    """Read and freeze exactly one ``agent.md`` without side effects."""
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("agent path must be path-like")
    resolved, slug = _safe_path(Path(path))
    try:
        size = resolved.stat().st_size
        if size < 1 or size > MAX_AGENT_BYTES:
            raise AgentManifestError("agent.md exceeds the file-size bound")
        # O_NOFOLLOW closes the final-component swap where agent.md becomes a
        # symlink after _safe_path.  Platforms without the flag still retain
        # the pre/post path and inode checks below.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(resolved, flags)
        try:
            opened = os.fstat(fd)
            chunks = []
            remaining = MAX_AGENT_BYTES + 1
            while remaining:
                block = os.read(fd, min(64 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        if len(raw) != size:
            raise AgentManifestError("agent.md changed while being read")
        after = resolved.stat(follow_symlinks=False)
        if ((opened.st_dev, opened.st_ino, opened.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise AgentManifestError("agent.md identity changed while being read")
        text = raw.decode("utf-8")
    except AgentManifestError:
        raise
    except (OSError, UnicodeError) as exc:
        raise AgentManifestError("agent.md could not be read as UTF-8") from exc
    fields, body = _frontmatter(text)
    try:
        schema = int(str(fields["schema_version"]))
    except (TypeError, ValueError) as exc:
        raise AgentManifestError("agent schema_version must be integer 1") from exc
    if schema != SCHEMA_VERSION or str(fields["schema_version"]) != "1":
        raise AgentManifestError("unsupported agent schema_version")
    name = str(fields["name"])
    if not _ID.fullmatch(name) or name != slug:
        raise AgentManifestError("agent name must equal its safe package slug")
    for key in ("role", "model"):
        if not _ID.fullmatch(str(fields[key])):
            raise AgentManifestError(f"agent {key} is not a safe identifier")
    description = str(fields["description"])
    if len(description) > 512 or not description.strip():
        raise AgentManifestError("agent description is invalid")
    cli, transport = str(fields["cli"]), str(fields["transport"])
    permission = str(fields["permission_ceiling"])
    authority = str(fields["authority_class"])
    consent, worktree = str(fields["consent_class"]), str(fields["worktree_mode"])
    if cli not in _CLIS or transport != "subprocess":
        raise AgentManifestError("agent CLI or transport is not allowlisted")
    if permission not in _PERMISSIONS:
        raise AgentManifestError("agent permission ceiling is invalid")
    if authority not in _AUTHORITY or _AUTHORITY[authority] != (consent, worktree):
        raise AgentManifestError("agent authority, consent, and worktree contract is unsafe")
    canonical_definition = {
        **{key: fields[key] for key in sorted(_FIELDS) if key != "schema_version"},
        "schema_version": SCHEMA_VERSION, "body": body,
    }
    return FrozenAgent(
        name, description, str(fields["role"]), str(fields["model"]), cli,
        transport, permission, authority, tuple(fields["skills"]), consent,
        worktree, _digest_text(_canonical(canonical_definition).decode("utf-8")),
        hashlib.sha256(raw).hexdigest(), body, str(resolved))


def _discover_root(root: Path, *, allow_legacy_flat: bool = False) -> list[FrozenAgent]:
    try:
        if not root.exists():
            return []
        if _is_reparse_point(root) or not root.is_dir():
            raise AgentManifestError("agent discovery root is unsafe")
        root_resolved = root.resolve(strict=True)
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise AgentManifestError("agent discovery root could not be read") from exc
    result = []
    for child in children:
        if _is_reparse_point(child):
            raise AgentManifestError(
                f"agent discovery contains an unsafe package ({child.name})")
        # Older global rosters store role definitions as direct ``*.md`` files
        # rather than modern package directories. They are reported separately
        # and are not treated as modern manifests. Workspace discovery remains
        # strict by default.
        if allow_legacy_flat and child.is_file():
            # The explicit global root is also the home of legacy flat role
            # files and operational documents. They are reported separately;
            # only modern package directories are parsed here.
            continue
        if allow_legacy_flat and child.is_dir():
            if (not _ID.fullmatch(child.name)
                    or not (child / "agent.md").is_file()):
                continue
        if not child.is_dir() or not _ID.fullmatch(child.name):
            raise AgentManifestError(
                f"agent discovery contains an invalid package entry ({child.name}); "
                "expected <agent-slug>/agent.md")
        try:
            child_resolved = child.resolve(strict=True)
            root_key = os.path.normcase(os.path.abspath(str(root_resolved)))
            child_key = os.path.normcase(os.path.abspath(str(child_resolved)))
            if os.path.commonpath((root_key, child_key)) != root_key:
                raise AgentManifestError(
                    f"agent package escaped the discovery root ({child.name})")
        except (OSError, ValueError) as exc:
            raise AgentManifestError(
                f"agent package cannot be contained ({child.name})") from exc
        try:
            result.append(load_agent(child / "agent.md"))
        except AgentManifestError as exc:
            raise AgentManifestError(
                f"agent manifest {child.name}/agent.md is invalid: {exc}") from exc
    return result


def legacy_flat_roster(global_root: str | os.PathLike[str] | None
                       ) -> tuple[str, ...]:
    """Return names of direct legacy ``*.md`` roster files.

    This is metadata-only: file contents are never read or returned. The
    legacy format is reported separately from modern packaged agents so
    validation cannot silently claim that it validated the old format.
    """
    if global_root is None:
        return ()
    if not isinstance(global_root, (str, os.PathLike)):
        raise TypeError("global_root must be path-like")
    root = Path(global_root)
    try:
        if not root.exists():
            return ()
        if _is_reparse_point(root) or not root.is_dir():
            raise AgentManifestError("agent discovery root is unsafe")
        return tuple(sorted(
            child.name for child in root.iterdir()
            if not _is_reparse_point(child)
            and child.is_file()
            and child.suffix.lower() == ".md"
        ))
    except OSError as exc:
        raise AgentManifestError("agent discovery root could not be read") from exc


def discover_agents(workspace_root: str | os.PathLike[str],
                    global_root: str | os.PathLike[str] | None = None,
                    *, allow_legacy_flat: bool = False
                    ) -> Mapping[str, FrozenAgent]:
    """Discover workspace packages, plus an explicitly supplied global root."""
    if not isinstance(workspace_root, (str, os.PathLike)):
        raise TypeError("workspace_root must be path-like")
    workspace = Path(workspace_root)
    try:
        if workspace.is_symlink() or not workspace.resolve(strict=True).is_dir():
            raise AgentManifestError("workspace root is unsafe")
    except OSError as exc:
        raise AgentManifestError("workspace root cannot be resolved") from exc
    agents = _discover_root(workspace / ".agents" / "agents")
    if global_root is not None:
        if not isinstance(global_root, (str, os.PathLike)):
            raise TypeError("global_root must be path-like")
        agents.extend(_discover_root(Path(global_root),
                                     allow_legacy_flat=allow_legacy_flat))
    result: dict[str, FrozenAgent] = {}
    for agent in agents:
        if agent.name in result:
            raise AgentManifestError("duplicate custom-agent name")
        result[agent.name] = agent
    return MappingProxyType(dict(sorted(result.items())))


def resolve_agent(name: str, workspace_root: str | os.PathLike[str],
                  global_root: str | os.PathLike[str] | None = None) -> FrozenAgent:
    if not isinstance(name, str) or not _ID.fullmatch(name):
        raise AgentManifestError("agent name is unsafe")
    agents = discover_agents(workspace_root, global_root)
    try:
        return agents[name]
    except KeyError as exc:
        raise AgentManifestError("custom agent was not found") from exc


def clamp_permission(parent: str, declared: str) -> str:
    """Return the least-authoritative valid tier; invalid tiers fail closed."""
    if parent not in _PERMISSIONS or declared not in _PERMISSIONS:
        raise AgentManifestError("permission tier is invalid")
    return _PERMISSIONS[min(_PERMISSIONS.index(parent),
                            _PERMISSIONS.index(declared))]
