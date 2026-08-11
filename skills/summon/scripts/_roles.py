"""Fail-closed, user-global role aliases.

Roles are deliberately separate from project rosters.  An approved role is only a
symbolic name for an existing agent definition; it cannot carry frontmatter, a path,
permission, or backend overrides.  The active registry is private to the operator and
is never copied into a receipt.  A small pending registry implements the explicit
propose -> approve -> write lifecycle without activating a proposal early.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SCHEMA_VERSION = 1
DEFAULT_ROLES_FILE = os.path.join(os.path.expanduser("~"), ".claude", "summon",
                                  "roles.json")


def roles_path() -> str:
    """Return the private role registry path; an env override keeps tests hermetic."""
    raw = os.environ.get("SUMMON_ROLES_FILE") or DEFAULT_ROLES_FILE
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def pending_roles_path() -> str:
    """The unactivated proposal store beside the active registry."""
    raw = os.environ.get("SUMMON_ROLES_PENDING_FILE")
    return (os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
            if raw else roles_path() + ".pending")


def validate_role_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or not ROLE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"invalid role name {name!r}; use letters, digits, '.', '_' or '-' "
            "and do not provide a path")
    return value


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_role(alias: str, target: str, target_sha256: str) -> str:
    return json.dumps({"alias": alias, "maps_to": target,
                       "target_sha256": target_sha256}, sort_keys=True,
                      separators=(",", ":"))


def _role_fingerprint(alias: str, target: str, target_sha256: str) -> str:
    return hashlib.sha256(_canonical_role(alias, target, target_sha256).encode("utf-8")).hexdigest()


def _role_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True).encode("utf-8")).hexdigest()


def _expected_role_hash(entry: dict) -> str | None:
    """Return the hash approval wrote, excluding the self-referential ``hash`` key."""
    if not isinstance(entry, dict) or not isinstance(entry.get("hash"), str):
        return None
    unsigned = {k: v for k, v in entry.items() if k != "hash"}
    return "sha256:" + _role_hash(unsigned)


def _read_document(path: str, kind: str, *, missing_ok: bool) -> tuple[dict, str | None]:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return {"schema_version": SCHEMA_VERSION,
                    "roles" if kind == "roles" else "proposals": {}}, None
        raise ValueError(f"{kind} registry is unavailable; create it with summon role propose")
    except OSError as exc:
        raise ValueError(f"{kind} registry cannot be read") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{kind} registry is not valid UTF-8 JSON") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{kind} registry root must be a JSON object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{kind} registry schema_version must be {SCHEMA_VERSION}")
    key = "roles" if kind == "roles" else "proposals"
    if not isinstance(doc.get(key), dict):
        raise ValueError(f"{kind} registry must contain an object named {key!r}")
    return doc, hashlib.sha256(raw).hexdigest()


def _write_document(path: str, doc: dict) -> None:
    """Atomically publish a private JSON registry, never exposing a partial document."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    data = (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".summon-roles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _definition_snapshot(alias: str, target: str, cwd: str, agents_dir: str | None,
                         active: dict, *, strict_agents_dir: bool = False) -> tuple[str, str]:
    """Load an existing target and return ``(resolved_file, sha256)``.

    Target aliases are rejected before loading, so a role can never chain through
    another role.  The loader remains the authority for malformed definitions and the
    returned hash is the exact byte snapshot that approval binds to.
    """
    from _loader import get_agents_dir, load_agent_snapshot, validate_agent_name

    validate_agent_name(target)
    if target in active:
        raise ValueError(f"role {alias!r} maps to another role {target!r}; alias chaining is disabled")
    roster = get_agents_dir(agents_dir, cwd)
    try:
        tup, _, sha = load_agent_snapshot(roster, target,
                                          strict_agents_dir=strict_agents_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"role {alias!r} target {target!r} is not a valid agent definition") from exc
    if not tup or not sha:
        raise ValueError(f"role {alias!r} target {target!r} is not a valid agent definition")
    return str(tup[3]), sha


def propose(alias: str, target: str, *, cwd: str, agents_dir: str | None = None) -> dict:
    """Validate and persist an inactive proposal; no active role is changed."""
    alias = validate_role_name(alias)
    target = validate_role_name(target)
    active, _ = _read_document(roles_path(), "roles", missing_ok=True)
    roles = active["roles"]
    from _loader import (bundled_roster_dir, discover_agent_packs, get_agents_dir,
                         _agent_file_present)
    roster = get_agents_dir(agents_dir, cwd)
    # An exact definition always wins at dispatch, so accepting the same name as a role
    # would create an unreachable/ambiguous entry rather than a useful alias.
    exact = _agent_file_present(roster, alias)
    if not exact:
        bundled = bundled_roster_dir()
        exact = bool(bundled and _agent_file_present(bundled, alias))
    if not exact:
        for pack in discover_agent_packs():
            if _agent_file_present(pack.get("path"), alias):
                exact = True
                break
    if exact:
        raise ValueError(f"role {alias!r} collides with an existing agent name")
    if alias in roles:
        raise ValueError(f"role {alias!r} is already approved")
    proposals, _ = _read_document(pending_roles_path(), "proposals", missing_ok=True)
    if alias in proposals:
        raise ValueError(f"role {alias!r} already has a pending proposal")
    _, target_sha = _definition_snapshot(alias, target, cwd, agents_dir, roles)
    proposal = {
        "maps_to": target,
        "target_sha256": target_sha,
        "proposed_at": _now(),
        "fingerprint": _role_fingerprint(alias, target, target_sha),
    }
    proposals["proposals"][alias] = proposal
    _write_document(pending_roles_path(), proposals)
    return {"status": "success", "role": alias, "maps_to": target,
            "target_sha256": target_sha, "fingerprint": proposal["fingerprint"],
            "state": "proposed"}


def approve(alias: str, *, cwd: str, agents_dir: str | None = None) -> dict:
    """Activate one pending proposal after revalidating its target bytes."""
    alias = validate_role_name(alias)
    active, _ = _read_document(roles_path(), "roles", missing_ok=True)
    pending, _ = _read_document(pending_roles_path(), "proposals", missing_ok=False)
    proposal = pending["proposals"].get(alias)
    if not isinstance(proposal, dict):
        raise ValueError(f"role {alias!r} has no pending proposal")
    target = proposal.get("maps_to")
    target_sha = proposal.get("target_sha256")
    fingerprint = proposal.get("fingerprint")
    if not (isinstance(target, str) and isinstance(target_sha, str)
            and isinstance(fingerprint, str)):
        raise ValueError(f"role {alias!r} proposal is malformed; re-propose it")
    if fingerprint != _role_fingerprint(alias, target, target_sha):
        raise ValueError(f"role {alias!r} proposal fingerprint mismatch; re-propose it")
    if alias in active["roles"]:
        raise ValueError(f"role {alias!r} is already approved")
    _, actual_sha = _definition_snapshot(alias, target, cwd, agents_dir, active["roles"])
    if actual_sha != target_sha:
        raise ValueError(f"role {alias!r} target changed after proposal; re-propose it")
    entry = dict(proposal)
    entry["approved_at"] = _now()
    entry["approved_by"] = "user"
    # Hash the unsigned approval record.  The ``hash`` field is intentionally
    # self-excluded so dispatch can validate it later without a circular value.
    entry["hash"] = _expected_role_hash({**entry, "hash": "placeholder"})
    active["roles"][alias] = entry
    _write_document(roles_path(), active)
    # If this cleanup fails the active role remains valid and the pending proposal is
    # harmlessly visible as a duplicate; never roll back a successfully approved role.
    pending["proposals"].pop(alias, None)
    try:
        _write_document(pending_roles_path(), pending)
    except OSError:
        pass
    return {"status": "success", "role": alias, "maps_to": target,
            "hash": entry["hash"], "fingerprint": fingerprint, "state": "approved"}


def list_roles() -> dict:
    active, active_sha = _read_document(roles_path(), "roles", missing_ok=True)
    pending, _ = _read_document(pending_roles_path(), "proposals", missing_ok=True)
    rows = []
    for name, entry in sorted(active["roles"].items()):
        if isinstance(entry, dict):
            rows.append({"role": name, "maps_to": entry.get("maps_to"),
                         "state": "approved", "hash": entry.get("hash"),
                         "fingerprint": entry.get("fingerprint")})
    for name, entry in sorted(pending["proposals"].items()):
        if name not in active["roles"] and isinstance(entry, dict):
            rows.append({"role": name, "maps_to": entry.get("maps_to"),
                         "state": "proposed", "fingerprint": entry.get("fingerprint")})
    return {"status": "success", "schema_version": SCHEMA_VERSION, "roles": rows,
            "registry_sha256": active_sha}


def resolve(alias: str, *, cwd: str, agents_dir: str | None = None) -> dict:
    """Validate and report one approved role's current target identity."""
    alias = validate_role_name(alias)
    active, active_sha = _read_document(roles_path(), "roles", missing_ok=False)
    entry = active["roles"].get(alias)
    if not isinstance(entry, dict):
        raise ValueError(f"role {alias!r} is not approved")
    target = entry.get("maps_to")
    target_sha = entry.get("target_sha256")
    if not isinstance(target, str) or not isinstance(target_sha, str):
        raise ValueError(f"role {alias!r} is malformed; re-propose it")
    if entry.get("hash") != _expected_role_hash(entry):
        raise ValueError(f"role {alias!r} hash mismatch; re-propose and approve it")
    _, actual_sha = _definition_snapshot(alias, target, cwd, agents_dir, active["roles"])
    if actual_sha != target_sha:
        raise ValueError(f"role {alias!r} target changed after approval; re-propose and approve it")
    return {"status": "success", "role": alias, "resolved_agent": target,
            "target_sha256": target_sha, "hash": entry.get("hash"),
            "fingerprint": entry.get("fingerprint"),
            "registry_sha256": active_sha}


def resolve_for_dispatch(agent_name: str, *, cwd: str, agents_dir: str | None,
                         enabled: bool, strict_agents_dir: bool = False) -> dict:
    """Resolve one dispatch name, returning provenance without changing the caller's args."""
    from _loader import (bundled_roster_dir, discover_agent_packs, get_agents_dir,
                         _agent_file_present)

    if not enabled:
        return {"requested": agent_name, "resolved": agent_name, "role": None}
    validate_role_name(agent_name)
    roster = get_agents_dir(agents_dir, cwd)
    # Exact files always win. This also preserves malformed-file behavior: dispatch will
    # report the loader's real parse error instead of allowing a role to mask it.
    if _agent_file_present(roster, agent_name):
        return {"requested": agent_name, "resolved": agent_name, "role": None}
    bundled = bundled_roster_dir()
    if bundled and _agent_file_present(bundled, agent_name):
        return {"requested": agent_name, "resolved": agent_name, "role": None}
    for pack in discover_agent_packs():
        if _agent_file_present(pack.get("path"), agent_name):
            return {"requested": agent_name, "resolved": agent_name, "role": None}

    active, registry_sha = _read_document(roles_path(), "roles", missing_ok=True)
    entry = active["roles"].get(agent_name)
    if not isinstance(entry, dict):
        return {"requested": agent_name, "resolved": agent_name, "role": None}
    target = entry.get("maps_to")
    target_sha = entry.get("target_sha256")
    fingerprint = entry.get("fingerprint")
    if not (isinstance(target, str) and isinstance(target_sha, str)
            and isinstance(fingerprint, str)):
        raise ValueError(f"role {agent_name!r} is malformed; re-propose and approve it")
    if entry.get("hash") != _expected_role_hash(entry):
        raise ValueError(f"role {agent_name!r} hash mismatch; re-propose and approve it")
    if target == agent_name or target in active["roles"]:
        raise ValueError(f"role {agent_name!r} chains through another role; alias chaining is disabled")
    # strict_agents_dir is deliberately applied AFTER alias resolution.
    _, actual_sha = _definition_snapshot(
        agent_name, target, cwd, agents_dir, active["roles"],
        strict_agents_dir=strict_agents_dir)
    if actual_sha != target_sha:
        raise ValueError(f"role {agent_name!r} target changed after approval; re-propose and approve it")
    if fingerprint != _role_fingerprint(agent_name, target, target_sha):
        raise ValueError(f"role {agent_name!r} fingerprint mismatch; re-propose and approve it")
    return {"requested": agent_name, "resolved": target,
            "role": {"name": agent_name, "resolved_agent": target,
                     "target_sha256": target_sha, "fingerprint": fingerprint,
                     "hash": entry.get("hash"),
                     "registry_sha256": registry_sha}}
