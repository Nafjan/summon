"""Provider-inert, privacy-bounded portable terminal projections.

The dispatcher envelope is a private execution record.  It can contain prompts,
provider output, local paths, credentials-adjacent diagnostics, and session
handles.  This module derives a small *experimental* compatibility receipt from
that record.  It is intentionally independent of the dispatcher so consumers
cannot accidentally treat this object as execution authority.

Only a direct in-memory private envelope should be passed to :func:`project_dispatch`.
``validate_projection`` validates the public shape; it does not confer trust on a
deserialized projection.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


SCHEMA = "summon.portable-result/experimental-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+\-]{0,127}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ARTIFACT_MAX = 64
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_JSON_MAX_BYTES = 256 * 1024
_PRIVATE_JSON_MAX_BYTES = 8 * 1024 * 1024
_ARTIFACT_BYTES_MAX = 1 << 50
_MEDIA_TYPE = re.compile(r"^[a-z][a-z0-9.+-]{0,63}/[a-z0-9.+-]{1,127}$")

_STATUSES = {"success", "partial", "blocked", "error", "unknown"}
_EXECUTION = {"success", "partial", "blocked", "error", "not_run", "unknown"}
_ATTEMPTS = {"completed", "failed", "cancelled", "timed_out", "not_run", "unknown"}
_EVIDENCE = {"reported", "inferred", "absent"}
_VERDICTS = {"pass", "revise", "block", "conditional", "endorse", "unknown"}
_MUTATION = {"none", "observed", "unknown"}
_STABILITY = {"stable", "unstable", "unknown"}
_TRANSPORTS = {"subprocess", "api", "acp", "manifest", "council", "unknown"}
_SOURCE_SURFACES = {"dispatch", "job"}
_PRIVATE_TERMINAL_STATUSES = {"success", "partial", "blocked", "error"}


class PortableResultError(ValueError):
    """Raised when a portable projection fails its strict public contract."""


def public_error_message(error: BaseException) -> str:
    """Return a bounded path-free error for the public command projection."""
    if isinstance(error, PortableResultError):
        message = str(error)
        if (message and len(message) <= 240
                and "\x00" not in message and "\n" not in message and "\r" not in message):
            return message
    return "portable result filesystem operation failed"


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 of canonical JSON, rejecting non-finite values."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text(value: object, *, slug: bool = False, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 0x21 or ord(char) > 0x7e for char in value):
        return None
    pattern = _SLUG if slug else _IDENTIFIER
    return value if pattern.fullmatch(value) else None


def _sha256(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _is_bool_or_none(value: object) -> bool:
    return value is None or isinstance(value, bool)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and -(2 ** 31) <= value <= 2 ** 31 - 1:
        return value
    return None


def _enum(value: object, allowed: set[str], default: str | None = None) -> str | None:
    return value if isinstance(value, str) and value in allowed else default


def _root_identity(root: Path) -> tuple[int, int]:
    """Return replacement identity without mutable directory metadata.

    Creating the private temporary file and publishing its hard link are the
    writer's own directory mutations, so directory ctime cannot be used as a
    stability token.  Device/inode identity still detects replacement; while
    the temporary file exists the original non-empty directory cannot be
    deleted and have its inode reused.
    """
    value = root.stat()
    return (value.st_dev, value.st_ino)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_cleanup_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Stronger identity for deleting a temporary pathname we created."""
    return (_same_file(left, right)
            and left.st_size == right.st_size)


def _is_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _bounded_bytes(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _ARTIFACT_BYTES_MAX else None


def _media_type(value: object) -> str | None:
    return value if isinstance(value, str) and _MEDIA_TYPE.fullmatch(value) else None


def _stability(value: object) -> str:
    if value is True or value == "stable":
        return "stable"
    if value is False or value == "unstable":
        return "unstable"
    return "unknown"


def _mutation(value: object) -> str:
    if value is True:
        return "observed"
    if value is False:
        return "none"
    return "unknown"


def _artifact_entries(envelope: dict, repo_root: os.PathLike[str] | str) -> tuple[dict, ...]:
    # Keep the argument in the experimental API so a future schema can add
    # handle-authenticated locators without changing the projector signature.
    del repo_root
    source = envelope.get("artifacts")
    files = source.get("files") if isinstance(source, dict) else None
    if not isinstance(files, list):
        files = []
    result: list[dict] = []
    for item in files:
        if isinstance(item, str):
            digest = None
        elif isinstance(item, dict):
            digest = _sha256(item.get("sha256"))
            size = _bounded_bytes(item.get("bytes", item.get("size_bytes")))
            media_type = _media_type(item.get("media_type"))
        else:
            continue
        if isinstance(item, str):
            size, media_type = None, None
        # Experimental-1 deliberately exports no path locator.  A pathname is
        # mutable after projection and proving nested containment against
        # concurrent ancestor replacement requires platform-specific,
        # handle-relative traversal.  A supplied content digest is the only
        # portable artifact identity; bounded metadata may accompany it.
        if digest is not None:
            entry: dict[str, object] = {"sha256": digest}
            if size is not None:
                entry["bytes"] = size
            if media_type is not None:
                entry["media_type"] = media_type
            result.append(entry)
            if len(result) >= _ARTIFACT_MAX:
                break
    return tuple(result)


def _model_projection(envelope: dict, no_contact: bool) -> dict:
    raw = envelope.get("model") if isinstance(envelope.get("model"), dict) else {}
    requested = _text(raw.get("requested"))
    targeted = _text(raw.get("targeted"))
    served = _text(raw.get("served"))
    evidence = _enum(envelope.get("served_model_evidence"), _EVIDENCE, "absent")
    if no_contact:
        requested, targeted, served, evidence = requested, targeted, None, "absent"
    complete_reported = (evidence == "reported" and requested is not None
                         and targeted is not None and served is not None)
    exact = complete_reported and requested == targeted == served
    return {
        "requested": requested,
        "targeted": targeted,
        "served": served,
        "served_model_evidence": evidence,
        "model_match": True if exact else (False if complete_reported else None),
        "named_model_verified": exact,
    }


def project_dispatch(envelope: dict, repo_root: os.PathLike[str] | str,
                     *, source_sha256: str, source_surface: str = "dispatch",
                     source_binding_sha256: str | None = None) -> dict:
    """Derive an allowlisted portable receipt from one private dispatch envelope.

    All claims of native hosting, model matching, provider contact, summaries,
    session identifiers, or artifact paths are ignored or recomputed.  The
    returned receipt describes an external compatibility relationship only.
    """
    if not isinstance(envelope, dict):
        raise PortableResultError("private dispatch envelope must be an object")
    if envelope.get("status") not in _PRIVATE_TERMINAL_STATUSES:
        raise PortableResultError(
            "portable projection requires a terminal private dispatch status")
    status = _enum(envelope.get("status"), _STATUSES, "unknown")
    execution = _enum(envelope.get("execution_status"), _EXECUTION, "unknown")
    attempts_raw = envelope.get("attempts")
    attempts = (attempts_raw if isinstance(attempts_raw, int)
                and not isinstance(attempts_raw, bool) and 0 <= attempts_raw <= 100
                else None)
    contact = _bool_or_none(envelope.get("provider_contacted"))
    attempt_status = _enum(envelope.get("attempt_status"), _ATTEMPTS, "unknown")
    no_contact = execution == "not_run" or attempt_status == "not_run" or contact is False or attempts == 0
    if no_contact:
        attempts, attempt_status, execution, contact = 0, "not_run", "not_run", False
        if status in {"success", "partial"}:
            status = "blocked"
    # Missing attempt evidence remains unknown.  A terminal status or an
    # explicit contact bit is not proof of a count and must not manufacture one.
    model = _model_projection(envelope, no_contact)
    source_summon = envelope.get("summon") if isinstance(envelope.get("summon"), dict) else {}
    source_artifacts = envelope.get("artifacts") if isinstance(envelope.get("artifacts"), dict) else {}
    workspace = envelope.get("workspace_evidence") if isinstance(envelope.get("workspace_evidence"), dict) else {}
    if no_contact:
        source_artifacts, workspace = {}, {}
    retry = "none"
    if not no_contact:
        if envelope.get("fallback_used") is True:
            retry = "fallback"
        elif (isinstance(envelope.get("attempts"), int)
              and not isinstance(envelope.get("attempts"), bool)
              and envelope["attempts"] > 1):
            retry = "retried"
    receipt_sha256 = _sha256(source_sha256)
    if receipt_sha256 is None:
        raise PortableResultError(
            "source_sha256 must bind the exact private receipt bytes")
    if source_surface not in _SOURCE_SURFACES:
        raise PortableResultError("unsupported portable result source surface")
    binding_sha256 = _sha256(source_binding_sha256)
    if ((source_surface == "job") != (binding_sha256 is not None)):
        raise PortableResultError(
            "trusted job projection requires an exact private record digest")
    core = {
        "schema": SCHEMA,
        "source": {
            "surface": source_surface,
            "source_schema": _text(envelope.get("schema"), limit=128) or "summon.dispatch-envelope/unknown",
            "receipt_sha256": receipt_sha256,
            "binding_sha256": binding_sha256,
            "relationship": "external", "native_adapter_attested": False,
        },
        "outcome": {
            "status": status,
            "execution_status": execution,
            "attempts": attempts,
            "attempt_status": attempt_status,
            "report_ok": False if no_contact else _bool_or_none(envelope.get("report_ok")),
            "result_usable": False if no_contact else _bool_or_none(envelope.get("result_usable")),
            "verdict": None if no_contact else _enum(envelope.get("verdict"), _VERDICTS),
            "error_kind": _text(envelope.get("error_kind"), slug=True, limit=64),
            "raw_backend_exit_code": _int_or_none(envelope.get("raw_backend_exit_code", envelope.get("backend_exit_code"))),
            "normalized_exit_code": _int_or_none(envelope.get("normalized_exit_code", envelope.get("exit_code"))),
        },
        "contact": {"provider_contacted": contact, "retry_or_fallback": retry},
        "model": model,
        "artifacts": {
            "workspace_mutation": _mutation(workspace.get("mutation")),
            "artifact_stability": _stability(source_artifacts.get("stable_during_dispatch")),
            "bytes": _bounded_bytes(source_artifacts.get("bytes", source_artifacts.get("size_bytes"))),
            "files": [] if no_contact else list(_artifact_entries(envelope, repo_root)),
        },
        "attestation": {
            "agent": _text(envelope.get("agent"), slug=True, limit=64),
            "provider": _text(envelope.get("provider"), slug=True, limit=64),
            "backend": _text(envelope.get("cli"), slug=True, limit=64),
            "transport": _enum(envelope.get("transport"), _TRANSPORTS, "unknown"),
        },
        "integrity": {
            "summon_version": _text(source_summon.get("version"), limit=64),
            "scripts_sha256": _sha256(source_summon.get("scripts_sha256")),
        },
    }
    core["integrity"]["projection_sha256"] = canonical_digest(core)
    validate_projection(core)
    return core


def _strict_keys(value: object, keys: set[str], location: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise PortableResultError(f"invalid {location} keys")
    return value


def validate_projection(value: object) -> None:
    """Strictly validate a public receipt and all cross-field invariants."""
    root = _strict_keys(value, {"schema", "source", "outcome", "contact", "model", "artifacts", "attestation", "integrity"}, "root")
    if root["schema"] != SCHEMA:
        raise PortableResultError("unknown portable result schema")
    source = _strict_keys(root["source"], {"surface", "source_schema", "receipt_sha256", "binding_sha256", "relationship", "native_adapter_attested"}, "source")
    if (source.get("surface") not in _SOURCE_SURFACES or source.get("relationship") != "external"
            or source.get("native_adapter_attested") is not False
            or _text(source.get("source_schema"), limit=128) is None
            or _sha256(source.get("receipt_sha256")) is None):
        raise PortableResultError("portable relationship must remain unattested external")
    if ((source["surface"] == "job") != (_sha256(source.get("binding_sha256")) is not None)):
        raise PortableResultError("invalid portable source binding")
    outcome = _strict_keys(root["outcome"], {"status", "execution_status", "attempts", "attempt_status", "report_ok", "result_usable", "verdict", "error_kind", "raw_backend_exit_code", "normalized_exit_code"}, "outcome")
    if outcome["status"] not in _STATUSES or outcome["execution_status"] not in _EXECUTION or outcome["attempt_status"] not in _ATTEMPTS:
        raise PortableResultError("invalid outcome state")
    if (outcome["attempts"] is not None
            and (not isinstance(outcome["attempts"], int)
                 or isinstance(outcome["attempts"], bool)
                 or not 0 <= outcome["attempts"] <= 100)):
        raise PortableResultError("invalid attempt count")
    if (not _is_bool_or_none(outcome["report_ok"])
            or not _is_bool_or_none(outcome["result_usable"])
            or outcome["verdict"] not in _VERDICTS | {None}):
        raise PortableResultError("invalid report state")
    if outcome["error_kind"] is not None and _text(outcome["error_kind"], slug=True, limit=64) is None:
        raise PortableResultError("invalid error kind")
    for key in ("raw_backend_exit_code", "normalized_exit_code"):
        if outcome[key] is not None and _int_or_none(outcome[key]) is None:
            raise PortableResultError("invalid exit code")
    contact = _strict_keys(root["contact"], {"provider_contacted", "retry_or_fallback"}, "contact")
    if (not _is_bool_or_none(contact["provider_contacted"])
            or contact["retry_or_fallback"] not in {"none", "retried", "fallback"}):
        raise PortableResultError("invalid contact state")
    model = _strict_keys(root["model"], {"requested", "targeted", "served", "served_model_evidence", "model_match", "named_model_verified"}, "model")
    for key in ("requested", "targeted", "served"):
        if model[key] is not None and _text(model[key]) is None:
            raise PortableResultError("invalid model identifier")
    if (model["served_model_evidence"] not in _EVIDENCE
            or not _is_bool_or_none(model["model_match"])
            or not isinstance(model["named_model_verified"], bool)):
        raise PortableResultError("invalid model proof")
    exact = (model["served_model_evidence"] == "reported" and model["requested"] is not None
             and model["requested"] == model["targeted"] == model["served"])
    expected_match = True if exact else (False if model["served_model_evidence"] == "reported"
                                         and model["requested"] is not None and model["targeted"] is not None
                                         and model["served"] is not None else None)
    if model["model_match"] is not expected_match or model["named_model_verified"] != exact:
        raise PortableResultError("forged or inconsistent model proof")
    no_contact = contact["provider_contacted"] is False or outcome["execution_status"] == "not_run" or outcome["attempt_status"] == "not_run" or outcome["attempts"] == 0
    if no_contact and (outcome["attempts"] != 0 or outcome["execution_status"] != "not_run" or outcome["attempt_status"] != "not_run" or contact["provider_contacted"] is not False or contact["retry_or_fallback"] != "none" or outcome["status"] in {"success", "partial"} or outcome["report_ok"] is True or outcome["result_usable"] is True or outcome["verdict"] is not None or model["served"] is not None or model["served_model_evidence"] != "absent" or model["model_match"] is not None or model["named_model_verified"]):
        raise PortableResultError("structural no-contact invariant violated")
    artifacts = _strict_keys(root["artifacts"], {"workspace_mutation", "artifact_stability", "bytes", "files"}, "artifacts")
    if (artifacts["workspace_mutation"] not in _MUTATION or artifacts["artifact_stability"] not in _STABILITY
            or artifacts["bytes"] is not None and _bounded_bytes(artifacts["bytes"]) is None
            or not isinstance(artifacts["files"], list) or len(artifacts["files"]) > _ARTIFACT_MAX):
        raise PortableResultError("invalid artifact summary")
    for entry in artifacts["files"]:
        allowed_entries = ({"sha256"}, {"sha256", "bytes"},
                           {"sha256", "media_type"},
                           {"sha256", "bytes", "media_type"})
        if not isinstance(entry, dict) or set(entry) not in allowed_entries:
            raise PortableResultError("invalid artifact entry")
        if "sha256" in entry and _sha256(entry["sha256"]) is None:
            raise PortableResultError("invalid artifact digest")
        if "bytes" in entry and _bounded_bytes(entry["bytes"]) is None:
            raise PortableResultError("invalid artifact bytes")
        if "media_type" in entry and _media_type(entry["media_type"]) is None:
            raise PortableResultError("invalid artifact media type")
    if no_contact and (artifacts["workspace_mutation"] != "unknown"
                       or artifacts["artifact_stability"] != "unknown"
                       or artifacts["bytes"] is not None or artifacts["files"]):
        raise PortableResultError("structural no-contact artifact invariant violated")
    attestation = _strict_keys(root["attestation"], {"agent", "provider", "backend", "transport"}, "attestation")
    for key in ("agent", "provider", "backend"):
        if attestation[key] is not None and _text(attestation[key], slug=True, limit=64) is None:
            raise PortableResultError("invalid attestation identifier")
    if attestation["transport"] not in _TRANSPORTS:
        raise PortableResultError("invalid transport")
    integrity = _strict_keys(root["integrity"], {"summon_version", "scripts_sha256", "projection_sha256"}, "integrity")
    if integrity["summon_version"] is not None and _text(integrity["summon_version"], limit=64) is None or integrity["scripts_sha256"] is not None and _sha256(integrity["scripts_sha256"]) is None or _sha256(integrity["projection_sha256"]) is None:
        raise PortableResultError("invalid integrity receipt")
    unsigned = json.loads(json.dumps(root))
    unsigned["integrity"].pop("projection_sha256")
    if integrity["projection_sha256"] != canonical_digest(unsigned):
        raise PortableResultError("projection digest mismatch")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise PortableResultError("duplicate JSON key")
        out[key] = value
    return out


def load_projection_bytes(data: bytes, *, max_bytes: int = _JSON_MAX_BYTES) -> dict:
    """Load one strict, bounded portable receipt from UTF-8 JSON bytes."""
    if (not isinstance(data, bytes) or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 1024 * 1024
            or not data or len(data) > max_bytes):
        raise PortableResultError("invalid projection byte size")
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _constant: (_ for _ in ()).throw(
                PortableResultError("non-finite JSON constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableResultError("invalid projection JSON") from exc
    validate_projection(value)
    return value


def load_private_envelope_bytes(
        data: bytes, *, max_bytes: int = _PRIVATE_JSON_MAX_BYTES) -> dict:
    """Load one bounded private envelope without projecting untrusted fields.

    The returned object remains private and must be passed directly to an
    allowlisting projector.  Duplicate keys, non-finite numbers, malformed
    UTF-8, top-level non-objects, and oversized inputs fail closed.
    """
    if (not isinstance(data, bytes) or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 16 * 1024 * 1024
            or not data or len(data) > max_bytes):
        raise PortableResultError("invalid private envelope byte size")
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _constant: (_ for _ in ()).throw(
                PortableResultError("non-finite JSON constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableResultError("invalid private envelope JSON") from exc
    if not isinstance(value, dict):
        raise PortableResultError("private envelope must be an object")
    return value


def read_regular_file_bytes(
        source: os.PathLike[str] | str, *, max_bytes: int = _PRIVATE_JSON_MAX_BYTES) -> bytes:
    """Read one existing non-link regular file through a stable descriptor."""
    if (not isinstance(max_bytes, int) or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= 16 * 1024 * 1024):
        raise PortableResultError("invalid input byte limit")
    path = Path(source)
    try:
        before = path.lstat()
        if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
            raise PortableResultError("portable result input must be a regular non-link file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name != "nt":
            # If a regular path is swapped for a FIFO between lstat and open,
            # the descriptor must not block before fstat rejects it.
            flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not _same_file(before, opened) or opened.st_size < 1 or opened.st_size > max_bytes:
                raise PortableResultError("portable result input changed or exceeds its byte limit")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        if (len(data) < 1 or len(data) > max_bytes or not _same_file(opened, after)
                or opened.st_size != after.st_size or len(data) != after.st_size
                or _is_reparse(path)):
            raise PortableResultError("portable result input changed during read")
        return data
    except PortableResultError:
        raise
    except OSError as exc:
        raise PortableResultError("portable result input is unavailable") from exc


def consume_reference(projection: dict) -> dict:
    """Return a bounded, authority-free reference-consumer acknowledgement."""
    validate_projection(projection)
    return {
        "schema": "summon.portable-consumer/experimental-1",
        "status": "accepted",
        "authority_granted": False,
        "source_surface": projection["source"]["surface"],
        "source_receipt_sha256": projection["source"]["receipt_sha256"],
        "projection_sha256": projection["integrity"]["projection_sha256"],
        "execution_status": projection["outcome"]["execution_status"],
        "result_usable": projection["outcome"]["result_usable"],
        "named_model_verified": projection["model"]["named_model_verified"],
        "artifact_count": len(projection["artifacts"]["files"]),
    }


def require_current_job_binding(record: object, result: object) -> None:
    """Require the current immutable-background-bundle provenance shape.

    The legacy registry intentionally treats a matching nonce as readable trust.
    Portable provenance is stricter: it requires the frozen scripts and prompt
    identities emitted by current Summon background launches.
    """
    if not isinstance(record, dict) or not isinstance(result, dict):
        raise PortableResultError("job projection needs private record and result objects")
    summon = record.get("summon")
    result_summon = result.get("summon")
    bundle = summon.get("background_bundle") if isinstance(summon, dict) else None
    scripts = _sha256(summon.get("scripts_sha256")) if isinstance(summon, dict) else None
    prompt = _sha256(record.get("prompt_sha256"))
    if (scripts is None or prompt is None or not isinstance(bundle, dict)
            or bundle.get("kind") != "immutable_per_job_snapshot"
            or _sha256(bundle.get("scripts_sha256")) != scripts
            or _sha256(bundle.get("prompt_sha256")) != prompt
            or not isinstance(result_summon, dict)
            or _sha256(result_summon.get("scripts_sha256")) != scripts
            or _sha256(result.get("prompt_sha256")) != prompt):
        raise PortableResultError(
            "job projection requires a current immutable scripts/prompt bundle")


def _safe_destination_parent(destination: Path) -> Path:
    parent = destination.parent
    try:
        if _is_reparse(parent) or not parent.is_dir():
            raise PortableResultError("projection destination parent is unsafe")
        # Reject link/junction ancestry; a parent replacement after this check
        # is detected by the identity check around the final hard-link commit.
        current = parent
        while current != current.parent:
            if _is_reparse(current):
                raise PortableResultError("projection destination ancestry is unsafe")
            current = current.parent
    except OSError as exc:
        raise PortableResultError("projection destination parent is unavailable") from exc
    return parent


def _descriptor_sha256(descriptor: int, expected_size: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise PortableResultError("projection file truncated during verification")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise PortableResultError("projection file grew during verification")
    return digest.hexdigest()


def write_projection_file(destination: os.PathLike[str] | str, projection: dict) -> None:
    """Exclusively publish canonical JSON with an atomic no-overwrite commit.

    The final pathname is created through ``link`` from a private, exclusive
    temporary file. Existing files, symbolic links and unsafe parent paths are
    rejected. An ambiguous link error re-authenticates and removes only the
    exact target inode created from our retained temporary descriptor.
    """
    validate_projection(projection)
    # Bind relative input to the caller's current directory once.  Every later
    # check and commit uses this absolute name rather than re-resolving cwd.
    target = Path(os.path.abspath(os.fspath(destination)))
    if target.name in {"", ".", ".."} or target.is_symlink() or target.exists():
        raise PortableResultError("projection destination already exists or is unsafe")
    parent = _safe_destination_parent(target)
    before = _root_identity(parent)
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    descriptor = None
    temp_name = None
    temp_stat = None
    temp_cleanup_stat = None
    linked = False
    linked_stat = None
    published_ok = False
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".summon-portable-", suffix=".tmp", dir=parent)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise PortableResultError("projection temporary write made no progress")
            offset += written
        os.fsync(descriptor)
        temp_stat = os.fstat(descriptor)
        temp_cleanup_stat = temp_stat
        if (not stat.S_ISREG(temp_stat.st_mode) or temp_stat.st_size != len(payload)
                or _descriptor_sha256(descriptor, len(payload)) != payload_sha256):
            raise PortableResultError("projection temporary file is invalid")
        if _root_identity(parent) != before or target.exists() or _is_reparse(parent):
            raise PortableResultError("projection destination changed during write")
        try:
            if os.link in os.supports_follow_symlinks:
                os.link(temp_name, target, follow_symlinks=False)
            else:
                os.link(temp_name, target)
            linked = True
            linked_stat = target.lstat()
        except FileExistsError as exc:
            raise PortableResultError("projection destination already exists") from exc
        except OSError as exc:
            # Some filesystems can report an error after creating the hard
            # link. Authenticate that exact inode so finally can remove it;
            # never infer ownership from the destination pathname alone.
            try:
                maybe_linked = target.lstat()
                if (_same_file(temp_stat, maybe_linked)
                        and maybe_linked.st_size == len(payload)):
                    linked, linked_stat = True, maybe_linked
            except OSError:
                pass
            raise PortableResultError("atomic projection publish unsupported") from exc
        target_stat = target.lstat()
        if (_root_identity(parent) != before or _is_reparse(parent)
                or _is_reparse(target) or not _same_file(temp_stat, target_stat)
                or not _same_file(linked_stat, target_stat)
                or target_stat.st_size != len(payload)):
            raise PortableResultError("projection destination changed during commit")
        target_fd = os.open(
            target, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        try:
            if (not _same_file(target_stat, os.fstat(target_fd))
                    or _descriptor_sha256(target_fd, len(payload)) != payload_sha256):
                raise PortableResultError("projection content changed during commit")
        finally:
            os.close(target_fd)
        published_ok = True
    finally:
        if descriptor is not None:
            if temp_cleanup_stat is None:
                try:
                    temp_cleanup_stat = os.fstat(descriptor)
                except OSError:
                    pass
            os.close(descriptor)
        # On any exceptional path, remove only the exact inode created by our
        # link call.  Never unlink a concurrently substituted destination.
        if linked and linked_stat is not None and not published_ok:
            try:
                published = target.lstat()
                if _same_file(linked_stat, published):
                    os.unlink(target)
            except OSError:
                pass
        if temp_name is not None:
            try:
                current_temp = Path(temp_name).lstat()
                # Clean only the exact inode and metadata state retained through
                # our descriptor. A substituted pathname fails this comparison;
                # deterministic unsupported-link failures do not litter.
                if (temp_cleanup_stat is not None
                        and _same_cleanup_identity(temp_cleanup_stat, current_temp)):
                    os.unlink(temp_name)
            except (FileNotFoundError, OSError):
                pass


__all__ = ["SCHEMA", "PortableResultError", "canonical_digest", "project_dispatch",
           "validate_projection", "load_projection_bytes",
           "load_private_envelope_bytes", "read_regular_file_bytes", "consume_reference",
           "require_current_job_binding",
           "write_projection_file", "public_error_message"]
