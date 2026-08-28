"""Side-effect-free participant resolution for deliberation runs.

This module is deliberately a *planning* boundary.  It resolves the immutable
agent-definition evidence that a future live deliberation run will need, but it
does not build a provider invocation, create a worktree or profile, contact a
backend, append a journal event, or spawn a process.  Keeping this phase small
lets the live runtime reuse the existing dispatch security code without making
the CLI's mutable roster part of the scheduler state machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from _builder import BACKEND_CLIS, clamp_permission, effective_permission
from _deliberation_agents import AgentManifestError, FrozenAgent, resolve_agent as resolve_custom_agent
from _model_catalog import display_for as _model_display_for
from _deliberation_scheduler import SeatDefinition
from _loader import (AgentLifecycleError, PERMISSION_VALUES, get_agents_dir,
                     load_agent_snapshot, require_dispatchable_lifecycle)
from _resolver import resolve_cli
from _roles import resolve_for_dispatch


SCHEMA_VERSION = 1
_SHA256_HEX = frozenset("0123456789abcdef")
_TEXT_SEAT_CLIS = frozenset({"openai-compat", "arkcli"})
_ACP_CLIS = frozenset({"cursor-agent", "gemini", "kimi"})
_KNOWN_TRANSPORTS = frozenset({"subprocess", "acp"})
_ENV_PREFIXES = {
    "claude": ("ANTHROPIC_",),
    "codex": ("OPENAI_", "CODEX_"),
    "cursor-agent": ("CURSOR_",),
    "gemini": ("GEMINI_", "GOOGLE_"),
    "agy": ("GEMINI_", "GOOGLE_", "AGY_"),
    "kimi": ("KIMI_",),
}


class RosterResolutionError(ValueError):
    """The immutable participant contract cannot be satisfied."""


def _bounded_text(value: object, name: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RosterResolutionError(f"{name} must be bounded non-empty text")
    return value.strip()


def _sha256_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256()
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
            return digest.hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RosterResolutionError("roster evidence could not be read") from exc


def _content_digest(path: str) -> str | None:
    return _sha256_file(path)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _redacted_id(value: object) -> str | None:
    """Stable public identifier without exposing an operator's naming scheme."""
    if value is None:
        return None
    return _digest_text(str(value))


def _digest_mapping(values: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(values), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze_nested(value: object) -> object:
    """Deep-freeze receipt evidence so nested caller containers cannot drift."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_nested(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_nested(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_nested(item) for item in value), key=repr))
    return value


def _git_head(path: str) -> str | None:
    """Read a worktree's detached/ref HEAD without invoking git or a shell."""
    marker = Path(path) / ".git"
    try:
        # A deliberation seat must be a linked disposable worktree, not the
        # caller's primary checkout and not an arbitrary directory containing a
        # forged .git/HEAD file.
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
        if not text.lower().startswith("gitdir:"):
            return None
        target = text.split(":", 1)[1].strip()
        gitdir = (marker.parent / target).resolve()
        if not gitdir.is_dir():
            return None
        back = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
        if not back:
            return None
        back_target = (back.split(":", 1)[1].strip()
                       if back.lower().startswith("gitdir:") else back)
        if Path(back_target).resolve() != marker.resolve():
            return None
        common_file = gitdir / "commondir"
        if not common_file.is_file():
            return None
        common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve()
        if (not common.is_dir() or not (common / "config").is_file()
                or not (common / "objects").is_dir()
                or not (common / "refs").is_dir()):
            return None
        worktrees = (common / "worktrees").resolve()
        if gitdir.parent.resolve() != worktrees:
            return None
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            if not ref or any(part in ("", ".", "..") for part in ref.split("/")):
                return None
            ref_path = common / ref
            if ref_path.is_file():
                head = ref_path.read_text(encoding="ascii").strip()
            else:
                packed = common / "packed-refs"
                if not packed.is_file():
                    return None
                prefix = ref
                found = None
                for line in packed.read_text(encoding="ascii").splitlines():
                    if line and not line.startswith("#") and not line.startswith("^"):
                        parts = line.split(" ", 1)
                        if len(parts) == 2 and parts[1] == prefix:
                            found = parts[0]
                            break
                head = found
        if not isinstance(head, str) or len(head) != 40:
            return None
        int(head, 16)
        return head.lower()
    except (OSError, UnicodeError, ValueError):
        return None


def _profile_content_digest(profile: Mapping[str, object] | None) -> str | None:
    """Hash profile file names, sizes and contents without exposing credentials."""
    if not profile:
        return None
    env = profile.get("env")
    if not isinstance(env, Mapping):
        return None
    root = next((value for value in env.values() if isinstance(value, str)), None)
    if not root:
        return None
    root_path = Path(root)
    try:
        root_path = root_path.resolve(strict=True)
    except OSError as exc:
        raise RosterResolutionError("profile config directory disappeared") from exc
    if not root_path.is_dir():
        raise RosterResolutionError("profile config directory is not a directory")
    digest = hashlib.sha256(b"summon-profile-content-v1")
    count = 0
    total_bytes = 0
    for current, dirs, files in os.walk(root_path, followlinks=False):
        dirs.sort()
        files.sort()
        # A directory symlink can change the credential tree without changing
        # the visible names returned by a non-following walk.  Refuse it rather
        # than pretending the profile contents are frozen.
        if any((Path(current) / name).is_symlink() for name in dirs + files):
            raise RosterResolutionError("profile contains symlinked content")
        for name in files:
            path = Path(current) / name
            try:
                relative = path.relative_to(root_path).as_posix()
                size = path.stat().st_size
                child = _sha256_file(str(path))
            except (OSError, ValueError) as exc:
                raise RosterResolutionError("profile content could not be attested") from exc
            if child is None:
                raise RosterResolutionError("profile content disappeared during attestation")
            count += 1
            if count > 4096:
                raise RosterResolutionError("profile contains too many files")
            total_bytes += size
            if total_bytes > 64 * 1024 * 1024:
                raise RosterResolutionError("profile contents exceed attestation bound")
            digest.update(relative.encode("utf-8") + b"\0" + str(size).encode("ascii")
                          + b"\0" + child.encode("ascii") + b"\0")
    return digest.hexdigest()


def _capabilities(raw: object) -> tuple[str, ...]:
    """Normalize the optional frontmatter capability field without rereading it."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        values = [str(value).strip().lower() for value in raw if str(value).strip()]
    else:
        text = str(raw).strip().lower()
        values = [part.strip() for part in text.split(",") if part.strip()]
    if len(values) > 32 or any(len(value) > 256 for value in values):
        raise RosterResolutionError("agent capabilities are too large")
    return tuple(dict.fromkeys(values))


def _validate_seat_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise RosterResolutionError("seat ids must be bounded identifiers")
    if not value[0].isalnum() or any(not (char.isalnum() or char in "._:-")
                                     for char in value):
        raise RosterResolutionError(f"invalid seat id {value!r}")
    return value


def _validate_sha(value: object, name: str, *, allow_none: bool = True,
                  allow_short: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    lengths = (32, 64) if allow_short else (64,)
    if (not isinstance(value, str) or len(value) not in lengths
            or any(ch not in _SHA256_HEX for ch in value)):
        raise RosterResolutionError(f"{name} must be a lowercase sha256")
    return value


def _backend_env_digest(cli: str) -> str | None:
    prefixes = _ENV_PREFIXES.get(cli)
    if not prefixes:
        return None
    values = {key: value for key, value in os.environ.items()
              if key.startswith(prefixes)}
    if not values:
        return None
    return _digest_mapping(values)


def _agy_account_digest() -> str | None:
    """Hash only the auth files agy's isolated profile copies.

    This mirrors the builder's account identity without importing the executor
    or touching the provider.  The file names are public implementation facts;
    their contents never leave the local digest.
    """
    try:
        from _builder import _AGY_AUTH_FILES
    except Exception as exc:  # pragma: no cover - import failure is fail closed
        raise RosterResolutionError("agy auth evidence is unavailable") from exc
    root = os.path.join(os.path.expanduser("~"), ".gemini")
    digest = hashlib.sha256(b"summon-agy-account-v1")
    seen = False
    for name in sorted(_AGY_AUTH_FILES):
        child = os.path.join(root, name)
        child_sha = _sha256_file(child)
        if child_sha:
            seen = True
        digest.update(b"\0" + name.encode("utf-8") + b"\0" + (child_sha or "").encode("ascii"))
    return digest.hexdigest()[:32] if seen else None


@dataclass(frozen=True)
class SeatRequest:
    """Operator-supplied seat identity and display role."""

    seat_id: str
    agent: str
    role: str = "participant"
    persona: str = ""
    capabilities: tuple[str, ...] = ()
    custom_agent: str | None = None

    def __post_init__(self) -> None:
        _validate_seat_id(self.seat_id)
        _bounded_text(self.agent, "agent")
        _bounded_text(self.role, "role")
        if not isinstance(self.persona, str) or len(self.persona) > 4096:
            raise RosterResolutionError("persona must be bounded text")
        values = tuple(self.capabilities)
        if len(values) > 32 or any(not isinstance(value, str) or not value or len(value) > 256
                                   for value in values):
            raise RosterResolutionError("seat capabilities are invalid")
        object.__setattr__(self, "capabilities", tuple(dict.fromkeys(values)))
        if self.custom_agent is not None:
            _bounded_text(self.custom_agent, "custom_agent", limit=64)


@dataclass(frozen=True)
class WorktreeProof:
    """Receipt-bound proof supplied by a later lifecycle phase.

    This class does not create or mutate the path.  A live runtime must create a
    seat-specific disposable worktree, then construct this proof and revalidate
    it before calling :func:`freeze_roster` again.
    """

    path: str
    head_sha256: str
    isolated: bool = False
    disposable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not os.path.isabs(self.path):
            raise RosterResolutionError("worktree proof path must be absolute")
        if not os.path.isdir(self.path):
            raise RosterResolutionError("worktree proof path is not a directory")
        _validate_sha(self.head_sha256, "worktree head_sha256", allow_none=False)
        if not self.isolated or not self.disposable:
            raise RosterResolutionError(
                "worktree proof must be both isolated and disposable")
        actual = _git_head(self.path)
        if actual is None or _digest_text(actual) != self.head_sha256:
            raise RosterResolutionError(
                "worktree proof is not a verified git worktree at the expected HEAD")

    @property
    def path_sha256(self) -> str:
        return _digest_text(os.path.normcase(os.path.realpath(self.path)))

    def revalidate(self) -> bool:
        """Recheck path identity, git membership, HEAD and lifecycle flags."""
        if not self.isolated or not self.disposable or not os.path.isdir(self.path):
            return False
        actual = _git_head(self.path)
        return actual is not None and _digest_text(actual) == self.head_sha256


@dataclass(frozen=True)
class FrozenSeat:
    """Public, redaction-safe identity for one frozen participant."""

    seat_id: str
    requested_agent: str
    resolved_agent: str
    definition_sha256: str
    definition_source: str
    role_provenance: Mapping[str, object]
    cli: str
    transport: str
    declared_permission: str
    effective_permission: str
    model: str | None
    effort: str | None
    extra_args: tuple[str, ...]
    profile_name: str | None
    profile_path_sha256: str | None
    profile_registry_sha256: str | None
    profile_command_sha256: str | None
    profile_content_sha256: str | None
    memory_sha256: str | None
    agy_account_sha256: str | None
    authority_class: str
    snapshot_digest: str
    seat_definition: SeatDefinition
    executable_sha256: str | None = None
    backend_env_sha256: str | None = None
    worktree_path_sha256: str | None = None
    worktree_head_sha256: str | None = None
    worktree_required: bool = False
    agy_account_checked: bool = False
    custom_agent_definition_digest: str | None = None
    custom_agent_source_digest: str | None = None
    custom_agent_identity: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _validate_seat_id(self.seat_id)
        for name in ("requested_agent", "resolved_agent", "cli", "transport",
                     "declared_permission", "effective_permission", "authority_class",
                     "snapshot_digest"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise RosterResolutionError(f"frozen seat {name} is invalid")
        if self.transport not in _KNOWN_TRANSPORTS:
            raise RosterResolutionError("frozen seat transport is invalid")
        if self.declared_permission not in PERMISSION_VALUES:
            raise RosterResolutionError("frozen seat permission is invalid")
        if self.model is not None and (not isinstance(self.model, str) or len(self.model) > 512):
            raise RosterResolutionError("frozen seat model is invalid")
        if self.effort is not None and (not isinstance(self.effort, str) or len(self.effort) > 32):
            raise RosterResolutionError("frozen seat effort is invalid")
        for name in ("definition_sha256", "snapshot_digest", "memory_sha256",
                     "profile_path_sha256", "profile_registry_sha256",
                     "profile_command_sha256", "agy_account_sha256",
                     "profile_content_sha256",
                     "executable_sha256", "backend_env_sha256",
                     "worktree_path_sha256", "worktree_head_sha256"):
            _validate_sha(getattr(self, name), name,
                          allow_short=name in {"profile_path_sha256",
                                               "profile_registry_sha256",
                                               "profile_command_sha256",
                                               "agy_account_sha256"})
        object.__setattr__(self, "role_provenance",
                           _freeze_nested(self.role_provenance or {}))
        for name in ("custom_agent_definition_digest", "custom_agent_source_digest"):
            _validate_sha(getattr(self, name), name)
        identity = self.custom_agent_identity
        if identity is not None:
            if not isinstance(identity, Mapping):
                raise RosterResolutionError("custom agent identity is invalid")
            object.__setattr__(self, "custom_agent_identity",
                               _freeze_nested(identity))

    def as_dict(self, *, native: bool = False) -> dict[str, object]:
        """Return receipt evidence; private source paths are native-only."""
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "seat_id_sha256": _redacted_id(self.seat_id),
            "requested_agent_sha256": _redacted_id(self.requested_agent),
            "resolved_agent_sha256": _redacted_id(self.resolved_agent),
            "definition_sha256": self.definition_sha256,
            "cli": self.cli,
            "transport": self.transport,
            "declared_permission": self.declared_permission,
            "effective_permission": self.effective_permission,
            "model_sha256": _redacted_id(self.model),
            "effort": self.effort,
            # Frontmatter args may contain a private path or token-like value.
            # Keep the exact tuple only in the native local receipt; redacted
            # exports carry its digest and never echo the argument bytes.
            "extra_args_sha256": _digest_text("\0".join(self.extra_args)),
            "profile_name_sha256": _redacted_id(self.profile_name),
            "profile_path_sha256": self.profile_path_sha256,
            "profile_registry_sha256": self.profile_registry_sha256,
            "profile_command_sha256": self.profile_command_sha256,
            "memory_sha256": self.memory_sha256,
            "agy_account_sha256": self.agy_account_sha256,
            "authority_class": self.authority_class,
            "snapshot_digest": self.snapshot_digest,
            "role_provenance_sha256": _digest_mapping(self.role_provenance),
            "seat_definition": {
                "seat_id_sha256": _redacted_id(self.seat_definition.seat_id),
                "role_sha256": _digest_text(self.seat_definition.role),
                "persona_sha256": _digest_text(self.seat_definition.persona),
                "capabilities_sha256": _digest_text("\0".join(self.seat_definition.capabilities)),
            },
            "profile_content_sha256": self.profile_content_sha256,
            "executable_sha256": self.executable_sha256,
            "backend_env_sha256": self.backend_env_sha256,
            "worktree_path_sha256": self.worktree_path_sha256,
            "worktree_head_sha256": self.worktree_head_sha256,
            "worktree_required": self.worktree_required,
            "agy_account_checked": self.agy_account_checked,
            "custom_agent_definition_digest": self.custom_agent_definition_digest,
            "custom_agent_source_digest": self.custom_agent_source_digest,
            "custom_agent_identity": (dict(self.custom_agent_identity)
                                      if self.custom_agent_identity is not None else None),
        }
        if native:
            result["seat_id"] = self.seat_id
            result["requested_agent"] = self.requested_agent
            result["resolved_agent"] = self.resolved_agent
            result["model"] = self.model
            result["profile_name"] = self.profile_name
            result["role_provenance"] = dict(self.role_provenance)
            result["seat_definition"] = {
                "seat_id": self.seat_definition.seat_id,
                "role": self.seat_definition.role,
                "persona": self.seat_definition.persona,
                "capabilities": list(self.seat_definition.capabilities),
            }
            result["extra_args"] = list(self.extra_args)
            result["definition_source"] = self.definition_source
        return result


@dataclass(frozen=True)
class FrozenRoster:
    """Immutable roster snapshot plus a side-effect-free revalidation hook."""

    seats: tuple[FrozenSeat, ...]
    roster_digest: str
    full_authority_consent: tuple[str, ...] = ()
    text_only_consent: tuple[str, ...] = ()
    _requests: tuple[SeatRequest, ...] = field(default=(), repr=False, compare=False)
    _kwargs: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)
    _runtime: Mapping[str, Mapping[str, object]] = field(default_factory=dict,
                                                       repr=False, compare=False)
    root_cwd: str = field(default="", repr=True, compare=True)

    def __post_init__(self) -> None:
        if not self.seats:
            raise RosterResolutionError("deliberation roster must contain a seat")
        if len({seat.seat_id for seat in self.seats}) != len(self.seats):
            raise RosterResolutionError("deliberation seat ids must be unique")
        _validate_sha(self.roster_digest, "roster_digest", allow_none=False)
        object.__setattr__(self, "full_authority_consent",
                           tuple(sorted(set(self.full_authority_consent))))
        object.__setattr__(self, "text_only_consent",
                           tuple(sorted(set(self.text_only_consent))))
        object.__setattr__(self, "_kwargs", MappingProxyType(dict(self._kwargs)))
        object.__setattr__(self, "_runtime", MappingProxyType({
            key: MappingProxyType(dict(value))
            for key, value in dict(self._runtime).items()
        }))
        if self.root_cwd:
            if not isinstance(self.root_cwd, str) or not os.path.isabs(self.root_cwd):
                raise RosterResolutionError("frozen roster cwd must be absolute")
            object.__setattr__(self, "root_cwd", os.path.abspath(self.root_cwd))

    def seat(self, seat_id: str) -> FrozenSeat:
        for seat in self.seats:
            if seat.seat_id == seat_id:
                return seat
        raise KeyError(seat_id)

    def runtime_for(self, seat_id: str) -> Mapping[str, object]:
        """Return private invocation material for an internal runtime phase.

        The result is intentionally absent from :meth:`as_dict`; callers must
        not serialize it into journals, browser views, or public bug reports.
        """
        try:
            return self._runtime[seat_id]
        except KeyError:
            raise KeyError(seat_id) from None

    def as_dict(self, *, native: bool = False) -> dict[str, object]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "roster_digest": self.roster_digest,
            "root_cwd_sha256": _redacted_id(self.root_cwd),
            "full_authority_consent_sha256": [_redacted_id(value)
                                               for value in self.full_authority_consent],
            "text_only_consent_sha256": [_redacted_id(value)
                                         for value in self.text_only_consent],
            "seats": [seat.as_dict(native=native) for seat in self.seats],
        }
        if not native:
            # Editorial model labels are presentation-only metadata.  They are
            # derived from exact backend/model pairs in the catalog; unknown
            # models remain hash-only and never become guessed identities.
            display = {}
            for seat in self.seats:
                identity = _model_display_for(seat.cli, seat.model)
                if identity is not None:
                    display[seat.seat_id] = identity
            if display:
                result["model_display_by_seat"] = display
        if native:
            result["full_authority_consent"] = list(self.full_authority_consent)
            result["text_only_consent"] = list(self.text_only_consent)
        return result

    def revalidate(self) -> bool:
        """Re-resolve the original request and compare the complete digest."""
        try:
            current = freeze_roster(self._requests, **dict(self._kwargs))
        except (RosterResolutionError, OSError, ValueError):
            return False
        if current.roster_digest != self.roster_digest:
            return False
        if current.root_cwd != self.root_cwd:
            return False
        if (current.full_authority_consent != self.full_authority_consent
                or current.text_only_consent != self.text_only_consent):
            return False
        # Do not trust the stored digest alone: a caller can construct a
        # dataclasses.replace copy with forged seat evidence while retaining
        # the original roster_digest.  Re-resolved seat projections must also
        # equal the frozen projections byte-for-byte.
        return tuple(_canonical_seat(seat) for seat in current.seats) == tuple(
            _canonical_seat(seat) for seat in self.seats)


def _normalise_requests(requests: Iterable[SeatRequest | Mapping[str, object]]) -> tuple[SeatRequest, ...]:
    result = []
    for item in requests:
        if isinstance(item, SeatRequest):
            result.append(item)
            continue
        if not isinstance(item, Mapping):
            raise RosterResolutionError("each deliberation seat must be a SeatRequest or object")
        try:
            result.append(SeatRequest(
                seat_id=item["seat_id"], agent=item["agent"],
                role=item.get("role", "participant"), persona=item.get("persona", ""),
                capabilities=tuple(item.get("capabilities", ())),
                custom_agent=item.get("custom_agent"),
            ))
        except KeyError as exc:
            raise RosterResolutionError("seat request needs seat_id and agent") from exc
    if not result or len(result) > 10:
        raise RosterResolutionError("deliberation roster needs 1..10 seats")
    if len({item.seat_id for item in result}) != len(result):
        raise RosterResolutionError("deliberation seat ids must be unique")
    return tuple(result)


def _worktree_for(seat_id: str, proofs: Mapping[str, WorktreeProof]) -> WorktreeProof | None:
    value = proofs.get(seat_id)
    if value is not None and not isinstance(value, WorktreeProof):
        raise RosterResolutionError(f"worktree proof for {seat_id!r} is invalid")
    if value is not None and not value.revalidate():
        raise RosterResolutionError(f"worktree proof for {seat_id!r} is stale")
    return value


def _authority(cli: str, declared: str, transport: str) -> tuple[str, str, bool]:
    effective = effective_permission(cli, declared)
    if cli in _TEXT_SEAT_CLIS:
        return effective, "text-only", False
    if effective == "unenforceable":
        return effective, "unenforceable", True
    if transport == "acp":
        return effective, "full-bypass", True
    if effective == "yolo":
        return effective, "full-bypass", True
    if effective == "safe-edit":
        return effective, "writable", True
    return effective, "enforceable", False


def _canonical_seat(seat: FrozenSeat) -> dict[str, object]:
    value = seat.as_dict(native=False)
    # Consent names are deliberately not part of the per-seat identity.  They
    # are attached to the run receipt and cannot mutate this seat's evidence.
    return value


def _custom_identity(agent: FrozenAgent | None) -> Mapping[str, object] | None:
    """Return a deeply immutable redacted projection of a manifest."""
    if agent is None:
        return None
    value = agent.as_dict(internal=False)
    value["skills_sha256"] = tuple(value["skills_sha256"])
    return MappingProxyType(value)


def freeze_roster(
    requests: Iterable[SeatRequest | Mapping[str, object]],
    *,
    cwd: str,
    agents_dir: str | None = None,
    role_enabled: bool = True,
    strict_agents_dir: bool = False,
    full_authority_consent: Iterable[str] = (),
    text_only_consent: Iterable[str] = (),
    worktree_proofs: Mapping[str, WorktreeProof] | None = None,
    cli_overrides: Mapping[str, str] | None = None,
    model_overrides: Mapping[str, str] | None = None,
    permission_ceilings: Mapping[str, str] | None = None,
    transport_overrides: Mapping[str, str] | None = None,
    profile_overrides: Mapping[str, str] | None = None,
    effort_overrides: Mapping[str, str] | None = None,
    custom_agents_root: str | os.PathLike[str] | None = None,
) -> FrozenRoster:
    """Resolve and freeze seats without any provider or filesystem mutation.

    ``worktree_proofs`` are evidence from a future lifecycle phase.  This
    function never creates them; writable/full-bypass seats fail closed when a
    proof is absent.  Consent is explicit and bound to seat ids, never read from
    ambient environment variables.
    """
    requests_tuple = _normalise_requests(requests)
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise RosterResolutionError("cwd must be an existing absolute directory")
    if custom_agents_root is not None:
        try:
            custom_agents_root = os.fspath(custom_agents_root)
        except TypeError as exc:
            raise RosterResolutionError("custom agent root must be path-like") from exc
        if not os.path.isabs(custom_agents_root):
            raise RosterResolutionError("custom agent root must be absolute")
        custom_agents_root = os.path.abspath(custom_agents_root)
    roster_dir = get_agents_dir(agents_dir, cwd)
    full = frozenset(_validate_seat_id(value) for value in full_authority_consent)
    text = frozenset(_validate_seat_id(value) for value in text_only_consent)
    requested_ids = frozenset(request.seat_id for request in requests_tuple)
    if not full <= requested_ids or not text <= requested_ids:
        raise RosterResolutionError("consent names must identify seats in this roster")
    proofs = dict(worktree_proofs or {})
    if any(seat_id not in requested_ids for seat_id in proofs):
        raise RosterResolutionError("worktree proof names an unknown seat")
    cli_overrides = dict(cli_overrides or {})
    model_overrides = dict(model_overrides or {})
    permission_ceilings = dict(permission_ceilings or {})
    transport_overrides = dict(transport_overrides or {})
    profile_overrides = dict(profile_overrides or {})
    effort_overrides = dict(effort_overrides or {})
    overrides = (cli_overrides, model_overrides, permission_ceilings,
                 transport_overrides, profile_overrides, effort_overrides)
    if any(set(mapping) - requested_ids for mapping in overrides):
        raise RosterResolutionError("an override names an unknown seat")
    for seat_id, ceiling in permission_ceilings.items():
        if ceiling not in PERMISSION_VALUES:
            raise RosterResolutionError(
                f"permission ceiling for {seat_id!r} is invalid")

    frozen: list[FrozenSeat] = []
    private_runtime: dict[str, Mapping[str, object]] = {}
    for request in requests_tuple:
        custom_agent: FrozenAgent | None = None
        if request.custom_agent is not None:
            try:
                custom_agent = resolve_custom_agent(
                    request.custom_agent, cwd, custom_agents_root)
            except (AgentManifestError, OSError, TypeError, ValueError) as exc:
                raise RosterResolutionError("custom agent could not be resolved") from exc
        role_info = resolve_for_dispatch(
            request.agent, cwd=cwd, agents_dir=agents_dir,
            enabled=role_enabled, strict_agents_dir=strict_agents_dir)
        resolved_agent = role_info.get("resolved")
        if not isinstance(resolved_agent, str) or not resolved_agent:
            raise RosterResolutionError("role resolution returned no agent")
        loaded, frontmatter, definition_sha = load_agent_snapshot(
            roster_dir, resolved_agent, strict_agents_dir=strict_agents_dir)
        if not loaded or not frontmatter or not definition_sha:
            raise RosterResolutionError(f"agent {resolved_agent!r} did not resolve")
        try:
            require_dispatchable_lifecycle(frontmatter, resolved_agent)
        except AgentLifecycleError as exc:
            raise RosterResolutionError(str(exc)) from exc
        role_detail = role_info.get("role")
        if isinstance(role_detail, Mapping):
            target_sha = role_detail.get("target_sha256")
            if target_sha != definition_sha:
                raise RosterResolutionError("approved role target changed during resolution")
            role_provenance = dict(role_detail)
        else:
            role_provenance = {}
        (run_agent_cli, system_context, _description, definition_source,
         declared, model, extra_args, effort) = loaded
        cli = cli_overrides.get(request.seat_id) or resolve_cli(run_agent_cli)
        if not isinstance(cli, str) or not cli:
            raise RosterResolutionError("agent did not resolve a backend CLI")
        if cli not in BACKEND_CLIS:
            raise RosterResolutionError(f"unknown deliberation backend {cli!r}")
        if cli_overrides.get(request.seat_id) and cli != cli_overrides[request.seat_id]:
            raise RosterResolutionError("invalid CLI override")
        model = model_overrides.get(request.seat_id, model)
        effort = effort_overrides.get(request.seat_id, effort)
        declared = clamp_permission(declared, permission_ceilings.get(request.seat_id))
        if declared not in PERMISSION_VALUES:
            raise RosterResolutionError("agent permission is invalid")
        raw_transport = transport_overrides.get(request.seat_id, frontmatter.get("transport", "subprocess"))
        transport = str(raw_transport).strip().lower()
        if transport not in _KNOWN_TRANSPORTS:
            raise RosterResolutionError(f"invalid deliberation transport {transport!r}")
        if transport == "acp" and cli not in _ACP_CLIS:
            raise RosterResolutionError(f"ACP is not supported for backend {cli!r}")
        profile_name = profile_overrides.get(request.seat_id) or frontmatter.get("profile")
        profile_selection = None
        if profile_name:
            try:
                from _profiles import resolve_profile, validate_model
                profile_selection = resolve_profile(profile_name, cli, cwd)
                validate_model(profile_selection, model)
            except (ValueError, TypeError) as exc:
                raise RosterResolutionError("private profile could not be resolved") from exc
        profile_content_sha = _profile_content_digest(profile_selection)
        memory_sha = _content_digest(os.path.join(cwd, ".agents", "memory.md"))
        agy_sha = _agy_account_digest() if cli == "agy" else None
        executable = (profile_selection or {}).get("command") if profile_selection else None
        if executable is None and cli not in _TEXT_SEAT_CLIS:
            executable = shutil.which(cli) or (shutil.which(f"{cli}.cmd") if os.name == "nt" else None)
        executable_sha = _sha256_file(executable)
        # Missing PATH executables are represented as unknown evidence here.  A
        # later live activation must preflight and refuse before creating the
        # run; the resolver itself remains useful on CI and offline hosts.
        effective, authority, needs_worktree = _authority(cli, declared, transport)
        if custom_agent is not None:
            expected = {
                "cli": cli,
                "transport": transport,
                "permission_ceiling": declared,
                "model": model,
            }
            actual = {
                "cli": custom_agent.cli,
                "transport": custom_agent.transport,
                "permission_ceiling": custom_agent.permission_ceiling,
                "model": custom_agent.model,
            }
            if actual != expected:
                raise RosterResolutionError(
                    "custom agent identity does not match the resolved roster seat")
            # The manifest is immutable evidence, never a source of authority.
            # Require its complete authority tuple to agree with the legacy
            # resolver instead of permitting it to relax a consent/worktree gate.
            authority_contract = {
                "enforceable": ("contained", "none", "none"),
                "writable": ("workspace-write", "none", "required"),
                "full-bypass": ("full-bypass", "full-authority", "required"),
            }.get(authority)
            if authority_contract != (custom_agent.authority_class,
                                      custom_agent.consent_class,
                                      custom_agent.worktree_mode):
                raise RosterResolutionError(
                    "custom agent authority does not match the resolved roster seat")
        if effective == "unenforceable":
            raise RosterResolutionError(
                f"{cli} cannot enforce declared permission {declared!r}; seat refused")
        if authority == "text-only":
            if request.seat_id not in text:
                raise RosterResolutionError(
                    f"text-only seat {request.seat_id!r} requires explicit text_only_consent")
            raise RosterResolutionError(
                "text-only deliberation seats are disabled until a cancellable adapter exists")
        proof = _worktree_for(request.seat_id, proofs)
        if needs_worktree and proof is None:
            raise RosterResolutionError(
                f"seat {request.seat_id!r} requires an isolated disposable worktree proof")
        if authority == "full-bypass" and request.seat_id not in full:
            raise RosterResolutionError(
                f"seat {request.seat_id!r} requires receipt-bound full_authority_consent")
        seat_definition = SeatDefinition(
            request.seat_id, request.role, tuple(request.capabilities or _capabilities(frontmatter.get("capability"))),
            request.persona)
        role_provenance = MappingProxyType(role_provenance)
        backend_env_sha = _backend_env_digest(cli)
        custom_identity = _custom_identity(custom_agent)
        snapshot_values = {
            "seat": request.seat_id,
            "requested_agent": request.agent,
            "resolved_agent": resolved_agent,
            "definition_sha256": definition_sha,
            "role_provenance": dict(role_provenance),
            "cli": cli,
            "transport": transport,
            "declared_permission": declared,
            "effective_permission": effective,
            "model": model,
            "effort": effort,
            "extra_args": list(extra_args),
            "profile_name": profile_selection.get("name") if profile_selection else None,
            "profile_path_sha256": profile_selection.get("path_sha256") if profile_selection else None,
            "profile_registry_sha256": profile_selection.get("registry_sha256") if profile_selection else None,
            "profile_command_sha256": profile_selection.get("command_sha256") if profile_selection else None,
            "profile_content_sha256": profile_content_sha,
            "memory_sha256": memory_sha,
            "agy_account_sha256": agy_sha,
            "authority_class": authority,
            "seat_definition": {
                "seat_id": seat_definition.seat_id,
                "role": seat_definition.role,
                "persona": seat_definition.persona,
                "capabilities": list(seat_definition.capabilities),
            },
            "executable_sha256": executable_sha,
            "backend_env_sha256": backend_env_sha,
            "worktree_path_sha256": proof.path_sha256 if proof else None,
            "worktree_head_sha256": proof.head_sha256 if proof else None,
            "worktree_required": needs_worktree,
            "agy_account_checked": cli == "agy",
            "custom_agent_definition_digest": (
                custom_agent.definition_digest if custom_agent else None),
            "custom_agent_source_digest": (
                custom_agent.source_digest if custom_agent else None),
            "custom_agent_identity": dict(custom_identity) if custom_identity else None,
        }
        snapshot_digest = _digest_mapping(snapshot_values)
        frozen.append(FrozenSeat(
            seat_id=request.seat_id, requested_agent=request.agent,
            resolved_agent=resolved_agent, definition_sha256=definition_sha,
            definition_source=str(definition_source), role_provenance=role_provenance,
            cli=cli, transport=transport, declared_permission=declared,
            effective_permission=effective, model=model, effort=effort,
            extra_args=tuple(extra_args), profile_name=(profile_selection or {}).get("name"),
            profile_path_sha256=(profile_selection or {}).get("path_sha256"),
            profile_registry_sha256=(profile_selection or {}).get("registry_sha256"),
            profile_command_sha256=(profile_selection or {}).get("command_sha256"),
            profile_content_sha256=profile_content_sha,
            memory_sha256=memory_sha, agy_account_sha256=agy_sha,
            authority_class=authority, snapshot_digest=snapshot_digest,
            seat_definition=seat_definition, executable_sha256=executable_sha,
            backend_env_sha256=backend_env_sha,
            worktree_path_sha256=proof.path_sha256 if proof else None,
            worktree_head_sha256=proof.head_sha256 if proof else None,
            worktree_required=needs_worktree, agy_account_checked=(cli == "agy"),
            custom_agent_definition_digest=(
                custom_agent.definition_digest if custom_agent else None),
            custom_agent_source_digest=(
                custom_agent.source_digest if custom_agent else None),
            custom_agent_identity=custom_identity,
        ))
        profile_env = ((profile_selection or {}).get("env") or {})
        private_runtime[request.seat_id] = MappingProxyType({
            "definition_body": system_context,
            "definition_source": str(definition_source),
            "profile_env": MappingProxyType(dict(profile_env)),
            "profile_command": ((profile_selection or {}).get("command")),
            "extra_args": tuple(extra_args),
            "custom_agent_definition_body": (
                custom_agent.body if custom_agent is not None else None),
            "custom_agent_definition_digest": (
                custom_agent.definition_digest if custom_agent else None),
            "custom_agent_source_digest": (
                custom_agent.source_digest if custom_agent else None),
            "custom_agent_identity": custom_identity,
        })
    roster_digest = _digest_mapping({"schema_version": SCHEMA_VERSION,
                                     "root_cwd_sha256": _digest_text(os.path.abspath(cwd)),
                                     "seats": [_canonical_seat(seat) for seat in frozen]})
    kwargs = {
        "cwd": cwd, "agents_dir": agents_dir, "role_enabled": role_enabled,
        "strict_agents_dir": strict_agents_dir,
        "full_authority_consent": tuple(full), "text_only_consent": tuple(text),
        "worktree_proofs": MappingProxyType(dict(proofs)),
        "cli_overrides": MappingProxyType(dict(cli_overrides)),
        "model_overrides": MappingProxyType(dict(model_overrides)),
        "permission_ceilings": MappingProxyType(dict(permission_ceilings)),
        "transport_overrides": MappingProxyType(dict(transport_overrides)),
        "profile_overrides": MappingProxyType(dict(profile_overrides)),
        "effort_overrides": MappingProxyType(dict(effort_overrides)),
        "custom_agents_root": custom_agents_root,
    }
    return FrozenRoster(tuple(frozen), roster_digest, tuple(sorted(full)), tuple(sorted(text)),
                        requests_tuple, kwargs, private_runtime, os.path.abspath(cwd))
