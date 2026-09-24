"""Provider-free launch-bound host observations.

This module deliberately does not launch a provider or ask a CLI for account
status.  It records the facts available in the Summon process immediately
before ``Popen``: the resolved executable bytes, the current resume registry
scope, and a fresh opaque observation nonce.  A governed continuation may use
those facts as a private binding; a matching registry row alone never grants a
launch.

The executable-content token is an executable content revision, never a vendor
version. Most CLIs do not expose a safe, side-effect-free version API to a
running dispatcher. Hashing the exact executable and adapter-declared launch
materials closes replacement-after-reservation; an adapter with separately
qualified vendor-version evidence may provide it through ``external_cli_version``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Mapping


SCHEMA = "summon.resume-launch-observation/v1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
_PROJECTION_KEYS = frozenset({
    "schema", "backend", "transport", "executable_path_sha256",
    "executable_sha256", "executable_size", "executable_mtime_ns",
    "launch_material_sha256", "executable_content_revision",
    "external_cli_version", "registry_generation", "registry_digest",
    "adapter", "adapter_version", "external_cli_version_scope",
})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_digest(value: object) -> bool:
    return (isinstance(value, str)
            and (_SHA_RE.fullmatch(value)
                 or (value.startswith("sha256:")
                     and bool(_SHA_RE.fullmatch(value[7:])))))


def _resolve_executable(command: object) -> str | None:
    if not isinstance(command, str) or not command:
        return None
    candidate = os.path.abspath(command) if os.path.isabs(command) else shutil.which(command)
    if not candidate:
        return None
    try:
        candidate = os.path.realpath(candidate)
        if not os.path.isfile(candidate):
            return None
    except OSError:
        return None
    return candidate


def measure_executable(command: object) -> dict[str, object] | None:
    """Measure the exact executable file without invoking it.

    Paths are not returned to the durable/public projection.  The normalized
    path digest is retained only in the private observation so a PATH retarget
    cannot be mistaken for the same command text.
    """
    path = _resolve_executable(command)
    if path is None:
        return None
    try:
        info = os.stat(path, follow_symlinks=False)
        if not os.path.isfile(path) or info.st_size < 0 or info.st_size > _MAX_EXECUTABLE_BYTES:
            return None
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_EXECUTABLE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_EXECUTABLE_BYTES:
        return None
    normalized = os.path.normcase(os.path.realpath(path))
    digest = _sha_bytes(raw)
    return {
        "executable_path_sha256": _sha_bytes(normalized.encode("utf-8", errors="replace")),
        "executable_sha256": digest,
        "executable_size": len(raw),
        "executable_mtime_ns": int(getattr(info, "st_mtime_ns", 0)),
        "executable_content_revision": f"sha256:{digest}",
    }


def _bounded_file_material(path: str) -> dict[str, object] | None:
    """Measure one launch material without retaining its path.

    Node/Python shims frequently launch a provider through a script named in
    argv.  Measuring only the interpreter would let that script be replaced
    after reservation.  We inspect only explicitly referenced regular files;
    this is not a directory scan and never invokes a provider.
    """
    try:
        resolved = os.path.realpath(path)
        info = os.stat(resolved, follow_symlinks=False)
        if (not os.path.isfile(resolved) or info.st_size < 0
                or info.st_size > _MAX_EXECUTABLE_BYTES):
            return None
        with open(resolved, "rb") as handle:
            raw = handle.read(_MAX_EXECUTABLE_BYTES + 1)
    except (OSError, ValueError, TypeError):
        return None
    if len(raw) > _MAX_EXECUTABLE_BYTES:
        return None
    normalized = os.path.normcase(resolved)
    return {
        "path_sha256": _sha_bytes(normalized.encode("utf-8", errors="replace")),
        "content_sha256": _sha_bytes(raw),
        "size": len(raw),
        "mtime_ns": int(getattr(info, "st_mtime_ns", 0)),
    }


def measure_launch_material(command: object, args: list[object],
                            material_paths: list[str] | None = None) -> str | None:
    """Return an aggregate identity for the exact executable and argv scripts.

    The aggregate is deliberately path-free and stable under JSON canonical
    encoding. ``material_paths`` is an adapter-owned descriptor produced by
    the resolver. We never infer authority from arbitrary argv values: prompt,
    attachment, and data arguments are not read as files.
    """
    command_material = measure_executable(command)
    if command_material is None:
        return None
    records: list[dict[str, object]] = [{
        "role": "command",
        "path_sha256": command_material["executable_path_sha256"],
        "content_sha256": command_material["executable_sha256"],
        "size": command_material["executable_size"],
        "mtime_ns": command_material["executable_mtime_ns"],
    }]
    seen = {records[0]["path_sha256"]}
    for index, path in enumerate(material_paths or (), 1):
        if not isinstance(path, str) or not path:
            return None
        material = _bounded_file_material(path)
        if material is None:
            # A resolver-declared material is required. Silent omission would
            # turn a replaced/missing entry script into an apparently stable
            # launch identity.
            return None
        if material["path_sha256"] in seen:
            continue
        seen.add(material["path_sha256"])
        records.append({"role": f"argv:{index}", **material})
    return _sha_bytes(_canonical(records))


def registry_scope(backend: object, transport: object) -> dict[str, object]:
    """Build a fresh process-owned v2 scope; never copy a caller declaration."""
    from _resume_capabilities import resume_capability_v2
    row = resume_capability_v2("resume", backend, transport)
    return {
        "registry_generation": row.get("registry_generation"),
        "registry_digest": row.get("registry_digest"),
        "adapter": row.get("adapter"),
        "adapter_version": row.get("adapter_version_scope"),
        "external_cli_version_scope": row.get("external_cli_version_scope"),
    }


def observation(command: object, args: list[object], cwd: str,
                proc_env: Mapping[str, object] | None, *, backend: str,
                transport: str = "subprocess",
                external_cli_version: str | None = None,
                material_paths: list[str] | None = None) -> dict[str, object]:
    """Return bounded fresh facts for one exact launch boundary.

    Existing command/argv/cwd/environment digests remain in the fleet evidence
    object.  These additive fields are the host-binding portion consumed by the
    governed continuation controller.
    """
    effective_env = proc_env if proc_env is not None else os.environ
    normalized_cwd = os.path.normcase(os.path.realpath(cwd))
    resolved_command = str(command)
    resolved_argv = [resolved_command, *(str(arg) for arg in args)]
    env_names = sorted(str(name) for name in effective_env)
    env_projection = {str(name): str(effective_env[name]) for name in sorted(effective_env)}
    measured = measure_executable(command) or {}
    launch_material_sha256 = measure_launch_material(command, args, material_paths)
    scope = registry_scope(backend, transport)
    content_revision = measured.get("executable_content_revision")
    return {
        "schema": SCHEMA,
        "backend": str(backend),
        "transport": str(transport),
        "command_sha256": _sha_bytes(resolved_command.encode("utf-8", errors="replace")),
        "argv_sha256": _sha_bytes(_canonical(resolved_argv)),
        "cwd_sha256": _sha_bytes(_canonical(normalized_cwd)),
        "env_names_sha256": _sha_bytes(_canonical(env_names)),
        "env_sha256": _sha_bytes(_canonical(env_projection)),
        "executable_path_sha256": measured.get("executable_path_sha256"),
        "executable_sha256": measured.get("executable_sha256"),
        "executable_size": measured.get("executable_size"),
        "executable_mtime_ns": measured.get("executable_mtime_ns"),
        "launch_material_sha256": launch_material_sha256,
        # Content identity and optional vendor-version evidence are distinct.
        # Never synthesize the latter from the former.
        "executable_content_revision": content_revision,
        "external_cli_version": external_cli_version,
        "registry_generation": scope["registry_generation"],
        "registry_digest": scope["registry_digest"],
        "adapter": scope["adapter"],
        "adapter_version": scope["adapter_version"],
        "external_cli_version_scope": scope["external_cli_version_scope"],
        "observation_nonce": uuid.uuid4().hex,
        "observed_at_ns": time.time_ns(),
    }


def binding_projection(value: object) -> dict[str, object] | None:
    """Keep only non-sensitive immutable facts needed for later comparison."""
    if not isinstance(value, Mapping):
        return None
    keys = (
        "schema", "backend", "transport", "executable_path_sha256",
        "executable_sha256", "executable_size", "executable_mtime_ns",
        "launch_material_sha256",
        "executable_content_revision",
        "external_cli_version", "registry_generation", "registry_digest",
        "adapter", "adapter_version", "external_cli_version_scope",
    )
    result = {key: value.get(key) for key in keys}
    return result


def valid_projection(value: object) -> bool:
    """Validate the immutable path-free projection stored by a continuation."""
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_KEYS:
        return False
    if value.get("schema") != SCHEMA:
        return False
    if (not isinstance(value.get("backend"), str) or not value["backend"]
            or not isinstance(value.get("transport"), str) or not value["transport"]):
        return False
    for key in ("executable_path_sha256", "executable_sha256",
                "launch_material_sha256", "registry_digest"):
        if not _is_digest(value.get(key)):
            return False
    for key in ("executable_size", "executable_mtime_ns", "registry_generation"):
        minimum = 1 if key == "registry_generation" else 0
        if (not isinstance(value.get(key), int) or isinstance(value.get(key), bool)
                or value[key] < minimum):
            return False
    executable_sha = value.get("executable_sha256")
    revision = value.get("executable_content_revision")
    if (not isinstance(revision, str) or not _is_digest(revision)
            or revision != f"sha256:{executable_sha}"):
        return False
    external = value.get("external_cli_version")
    if external is not None and (not isinstance(external, str)
                                 or not external or len(external) > 256):
        return False
    return all(isinstance(value.get(key), str) and value[key]
               for key in ("adapter", "adapter_version", "external_cli_version_scope"))


def valid_observation(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        return False
    if not isinstance(value.get("backend"), str) or not value["backend"]:
        return False
    if not isinstance(value.get("transport"), str) or not value["transport"]:
        return False
    for key in ("command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256",
                "env_sha256", "executable_path_sha256", "executable_sha256",
                "launch_material_sha256", "registry_digest"):
        if not isinstance(value.get(key), str) or not _is_digest(value[key]):
            return False
    if not isinstance(value.get("executable_size"), int) or value["executable_size"] < 0:
        return False
    if not isinstance(value.get("executable_mtime_ns"), int) or value["executable_mtime_ns"] < 0:
        return False
    if not _is_digest(value.get("launch_material_sha256")):
        return False
    if not isinstance(value.get("registry_generation"), int) or value["registry_generation"] < 1:
        return False
    for key in ("adapter", "adapter_version", "external_cli_version_scope"):
        if not isinstance(value.get(key), str) or not value[key]:
            return False
    revision = value.get("executable_content_revision")
    if (not isinstance(revision, str) or not _is_digest(revision)
            or revision != f"sha256:{value.get('executable_sha256')}"):
        return False
    external = value.get("external_cli_version")
    if external is not None and (not isinstance(external, str) or not external or len(external) > 256):
        return False
    if not isinstance(value.get("observation_nonce"), str) or not _NONCE_RE.fullmatch(value["observation_nonce"]):
        return False
    if not isinstance(value.get("observed_at_ns"), int) or value["observed_at_ns"] <= 0:
        return False
    return True


__all__ = ["SCHEMA", "measure_executable", "measure_launch_material", "registry_scope", "observation",
           "binding_projection", "valid_projection", "valid_observation"]
