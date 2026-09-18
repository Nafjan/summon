"""Authenticated, provider-free qualification for a future launch.

Launch observations describe what the host measured immediately before a
process boundary.  They are not, by themselves, permission to resume a
provider session.  This module stores the separate, short-lived qualification
record that binds an observation to an explicitly reviewed adapter/version and
material contract.  The record is private and authenticated with the source
job's nonce; it is never emitted in public receipts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from collections.abc import Mapping

import _jobs


SCHEMA = "summon.launch-qualification/v1"
_DOMAIN = b"summon-launch-qualification/v1:"
_REVOCATION_DOMAIN = b"summon-launch-qualification-revocation/v1:"
REVOCATION_SCHEMA = "summon.launch-qualification-revocation/v1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_BYTES = 256 * 1024
_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
_FIELDS = frozenset({
    "schema", "source_job_id", "source_attempt_id", "operation", "backend",
    "transport", "registry_generation", "registry_digest", "adapter",
    "adapter_version", "external_cli_version", "material_contract",
    "executable_sha256", "launch_material_sha256", "status", "expires_at",
    "revocation_id", "auth",
})


class QualificationError(ValueError):
    """Typed fail-closed qualification error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def material_contract_for(*, backend: object, transport: object,
                          adapter: object, adapter_version: object) -> str | None:
    """Return the resolver-owned contract for a supported launch adapter.

    This is intentionally an allowlist, not a hash of caller-provided text.
    Unknown adapters must obtain a separately reviewed policy before they can
    issue a continuation qualification.
    """
    if (backend, transport, adapter, adapter_version) == (
            "claude", "subprocess", "summon-cli-subprocess", "summon-executor/3.4.0"):
        return "summon-claude-subprocess-material/v1"
    return None


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _digest(value: object) -> bool:
    return isinstance(value, str) and (
        bool(_SHA_RE.fullmatch(value))
        or (value.startswith("sha256:") and bool(_SHA_RE.fullmatch(value[7:]))))


def _id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _auth(nonce: str, body: Mapping[str, object]) -> str:
    return hmac.new(nonce.encode("utf-8"), _DOMAIN + _canonical(body),
                    hashlib.sha256).hexdigest()


def revocation_id_for(value: Mapping[str, object]) -> str:
    """Derive revocation identity from the current qualification policy."""
    fields = {
        key: value.get(key) for key in (
            "operation", "backend", "transport", "registry_generation",
            "registry_digest", "adapter", "adapter_version",
            "external_cli_version", "material_contract",
            "executable_sha256", "launch_material_sha256")
    }
    return hashlib.sha256(_canonical(fields)).hexdigest()


def qualification_path(root: str, job_id: str) -> str:
    if not _id(job_id):
        raise QualificationError("invalid_job_id", "qualification job id is invalid")
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.launch-qualification.json")


def revocation_path(root: str, job_id: str) -> str:
    if not _id(job_id):
        raise QualificationError("invalid_job_id", "qualification job id is invalid")
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.launch-qualification-revocation.json")


def _strict_read(path: str) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt" and os.path.islink(path):
        raise QualificationError("launch_qualification_untrusted", "qualification is a symlink")
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QualificationError("launch_qualification_untrusted", "qualification is unavailable") from exc
    try:
        info = os.fstat(fd)
        if info.st_size > _MAX_BYTES:
            raise QualificationError("launch_qualification_oversized", "qualification is oversized")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise QualificationError("launch_qualification_oversized", "qualification is oversized")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise QualificationError("launch_qualification_untrusted", "qualification is not valid JSON") from exc
    if not isinstance(value, dict):
        raise QualificationError("launch_qualification_untrusted", "qualification is not an object")
    return value


def _validate_shape(value: object, *, now: float | None = None,
                    require_qualified: bool = False) -> bool:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        return False
    if value.get("schema") != SCHEMA:
        return False
    if not _id(value.get("source_job_id")) or not _id(value.get("source_attempt_id")):
        return False
    for key in ("operation", "backend", "transport", "adapter", "adapter_version",
                "external_cli_version", "material_contract"):
        item = value.get(key)
        if not isinstance(item, str) or not item or len(item) > 512:
            return False
    external = value["external_cli_version"].strip().lower()
    if external in {"not_declared", "unavailable", "unknown", "none"}:
        return False
    if not isinstance(value.get("registry_generation"), int) or isinstance(value["registry_generation"], bool) \
            or value["registry_generation"] < 1:
        return False
    if not _digest(value.get("registry_digest")) or not _sha(value.get("executable_sha256")) \
            or not _sha(value.get("launch_material_sha256")) or not _sha(value.get("revocation_id")):
        return False
    if value.get("revocation_id") != revocation_id_for(value):
        return False
    if material_contract_for(
            backend=value.get("backend"), transport=value.get("transport"),
            adapter=value.get("adapter"), adapter_version=value.get("adapter_version")) \
            != value.get("material_contract"):
        return False
    expires = value.get("expires_at")
    if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
            or not math.isfinite(expires)):
        return False
    if require_qualified and value.get("status") != "qualified":
        return False
    if not isinstance(value.get("status"), str) or not value["status"]:
        return False
    if now is not None and (expires <= now or expires > now + _MAX_TTL_SECONDS + 1):
        return False
    return _sha(value.get("auth"))


def issue(*, source_job_id: str, source_attempt_id: str, operation: str,
          backend: str, transport: str, registry_generation: int,
          registry_digest: str, adapter: str, adapter_version: str,
          external_cli_version: str, material_contract: str,
          executable_sha256: str, launch_material_sha256: str,
          expires_at: float, revocation_id: str,
          source_nonce: str, status: str = "qualified") -> dict:
    """Build an authenticated record; the caller must write it privately.

    This is an explicit qualification seam for a reviewed, provider-free
    revalidation operation.  It deliberately rejects registry placeholders and
    does not infer a vendor version from executable content.
    """
    body = {
        "schema": SCHEMA, "source_job_id": source_job_id,
        "source_attempt_id": source_attempt_id, "operation": operation,
        "backend": backend, "transport": transport,
        "registry_generation": registry_generation, "registry_digest": registry_digest,
        "adapter": adapter, "adapter_version": adapter_version,
        "external_cli_version": external_cli_version,
        "material_contract": material_contract,
        "executable_sha256": executable_sha256,
        "launch_material_sha256": launch_material_sha256,
        "status": status, "expires_at": expires_at,
        "revocation_id": revocation_id,
    }
    value = dict(body, auth=_auth(source_nonce, body))
    if not isinstance(source_nonce, str) or not source_nonce:
        raise QualificationError("launch_qualification_untrusted", "source nonce is unavailable")
    if not _validate_shape(value, now=time.time(), require_qualified=True):
        raise QualificationError("launch_qualification_invalid", "qualification fields are invalid")
    return value


def write(root: str, job_id: str, value: Mapping[str, object]) -> dict:
    """Write an already-authenticated qualification for an authenticated source.

    This function never signs caller-supplied metadata.  A revalidation
    producer must first call :func:`issue` with the source nonce and pass its
    resulting record unchanged.
    """
    record_path = _jobs.record_path(root, job_id)
    record = _strict_read(record_path)
    if not isinstance(record, dict) or not isinstance(record.get("nonce"), str):
        raise QualificationError("source_job_untrusted", "source record is unavailable")
    if not isinstance(value, Mapping):
        raise QualificationError("launch_qualification_invalid", "qualification is not an object")
    candidate = dict(value)
    body = {key: item for key, item in candidate.items() if key != "auth"}
    if not _validate_shape(candidate, now=time.time(), require_qualified=True):
        raise QualificationError("launch_qualification_invalid", "qualification fields are invalid")
    if body.get("source_job_id") != job_id or body.get("source_attempt_id") != record.get("attempt_id"):
        raise QualificationError("launch_qualification_identity", "qualification source identity differs")
    if not hmac.compare_digest(str(candidate.get("auth", "")), _auth(record["nonce"], body)):
        raise QualificationError("launch_qualification_auth_failed", "qualification authentication failed")
    _jobs._atomic_write_json(qualification_path(root, job_id), candidate)
    return candidate


def read(root: str, job_id: str) -> dict | None:
    record = _strict_read(_jobs.record_path(root, job_id))
    if record is None or not isinstance(record.get("nonce"), str):
        raise QualificationError("source_job_untrusted", "source record is unavailable")
    value = _strict_read(qualification_path(root, job_id))
    if value is None:
        return None
    if not _validate_shape(value):
        raise QualificationError("launch_qualification_untrusted", "qualification fields are invalid")
    body = {key: item for key, item in value.items() if key != "auth"}
    if (body.get("source_job_id") != job_id
            or body.get("source_attempt_id") != record.get("attempt_id")
            or not hmac.compare_digest(str(value.get("auth")), _auth(record["nonce"], body))):
        raise QualificationError("launch_qualification_auth_failed", "qualification authentication failed")
    return dict(value)


def revoke(root: str, job_id: str, revocation_id: str) -> dict:
    """Record trusted revocation without editing the signed qualification."""
    from _job_control import source_admission_lock
    with source_admission_lock(root, job_id):
        record = _strict_read(_jobs.record_path(root, job_id))
        if record is None or not isinstance(record.get("nonce"), str):
            raise QualificationError("source_job_untrusted", "source record is unavailable")
        if not _sha(revocation_id):
            raise QualificationError("launch_qualification_invalid", "revocation identity is invalid")
        body = {"schema": REVOCATION_SCHEMA, "job_id": job_id,
                "revocation_id": revocation_id, "revoked_at": time.time()}
        value = dict(body, auth=hmac.new(
            record["nonce"].encode("utf-8"), _REVOCATION_DOMAIN + _canonical(body),
            hashlib.sha256).hexdigest())
        _jobs._atomic_write_json(revocation_path(root, job_id), value)
        return value


def is_revoked(root: str, job_id: str, revocation_id: str) -> bool:
    record = _strict_read(_jobs.record_path(root, job_id))
    if record is None or not isinstance(record.get("nonce"), str):
        raise QualificationError("source_job_untrusted", "source record is unavailable")
    value = _strict_read(revocation_path(root, job_id))
    if value is None:
        return False
    body = {key: item for key, item in value.items() if key != "auth"}
    revoked_at = value.get("revoked_at")
    if (set(value) != {"schema", "job_id", "revocation_id", "revoked_at", "auth"}
            or value.get("schema") != REVOCATION_SCHEMA
            or value.get("job_id") != job_id
            or value.get("revocation_id") != revocation_id
            or not isinstance(revoked_at, (int, float)) or isinstance(revoked_at, bool)
            or not math.isfinite(revoked_at)
            or not hmac.compare_digest(str(value.get("auth", "")), hmac.new(
                record["nonce"].encode("utf-8"), _REVOCATION_DOMAIN + _canonical(body),
                hashlib.sha256).hexdigest())):
        raise QualificationError("launch_qualification_untrusted", "revocation record is invalid")
    return True


def validate(value: object, observation: Mapping[str, object], *, operation: str,
             backend: str, transport: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not _validate_shape(value, now=now, require_qualified=True):
        return False
    if not isinstance(observation, Mapping):
        return False
    if (value.get("operation") != operation or value.get("backend") != backend
            or value.get("transport") != transport):
        return False
    for key in ("registry_generation", "registry_digest", "adapter", "adapter_version",
                "external_cli_version", "executable_sha256", "launch_material_sha256"):
        if observation.get(key) != value.get(key):
            return False
    return True


__all__ = ["SCHEMA", "REVOCATION_SCHEMA", "QualificationError", "qualification_path",
           "revocation_path", "material_contract_for", "issue", "write", "read",
           "revoke", "is_revoked", "validate", "revocation_id_for"]
