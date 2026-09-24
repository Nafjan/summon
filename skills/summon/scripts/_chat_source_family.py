"""Private source-family authority for governed chat continuations.

Conversation rooms do not have background-job records.  This module provides a
small private, authenticated seal that gives a room/participant lineage its own
identity and nonce, then links explicitly revalidated source-turn qualifications
to that lineage.  It is local control metadata only; no provider is contacted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path

import _chat_launch_qualification as _qualification


SCHEMA = "summon.chat-source-family/v1"
_DOMAIN = b"summon-chat-source-family/v1:"
MIGRATION_AUTH_SCHEMA = "summon.chat-source-family-migration-authority/v1"
_MIGRATION_DOMAIN = b"summon-chat-source-family-migration-authority/v1:"
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 256 * 1024
_MAX_QUALIFICATIONS = 128
_LOCK_TIMEOUT_SECONDS = 15.0
_PROCESS_LOCK = threading.RLock()


class SourceFamilyError(ValueError):
    """Typed fail-closed source-family error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _auth(nonce: str, body: Mapping[str, object]) -> str:
    return hmac.new(nonce.encode("utf-8"), _DOMAIN + _canonical(body),
                    hashlib.sha256).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def migration_packet_digest(packet: Mapping[str, object]) -> str:
    """Digest packet content without its separate migration authority."""
    if not isinstance(packet, Mapping):
        raise SourceFamilyError("chat_migration_invalid", "migration packet is not an object")
    body = {key: value for key, value in packet.items() if key != "authority"}
    return _digest(body)


def issue_migration_authority(packet: Mapping[str, object], *, key: str) -> dict[str, object]:
    """Create a test/adapter-side packet authority.

    Production callers must invoke this only from a reviewed adapter-side
    producer holding the separate operator key.  The key is intentionally not
    part of the packet and is never returned by the public CLI; this helper
    signs the supplied packet content but does not make that content trusted.
    """
    if not isinstance(key, str) or len(key) < 32:
        raise SourceFamilyError("chat_migration_authority_unavailable",
                                "migration authority key is unavailable")
    body = {
        "schema": MIGRATION_AUTH_SCHEMA,
        "operation": "legacy_source_family",
        "session_id": packet.get("session_id"),
        "participant": packet.get("participant"),
        "turn_id": packet.get("turn_id"),
        "source_family_id": (packet.get("source_family", {}) or {}).get("source_family_id")
            if isinstance(packet.get("source_family"), Mapping) else None,
        "packet_sha256": migration_packet_digest(packet),
    }
    auth = hmac.new(key.encode("utf-8"), _MIGRATION_DOMAIN + _canonical(body),
                    hashlib.sha256).hexdigest()
    return dict(body, auth=auth)


def validate_migration_authority(packet: Mapping[str, object], *, key: str | None = None,
                                 expected_session: str, expected_participant: str,
                                 expected_turn_id: str, expected_family_id: str) -> None:
    """Verify the separate operator/adapter authority for legacy publication."""
    if key is None:
        key = os.environ.get("SUMMON_CHAT_MIGRATION_KEY")
    if not isinstance(key, str) or len(key) < 32:
        raise SourceFamilyError("chat_migration_authority_unavailable",
                                "legacy migration authority is unavailable")
    authority = packet.get("authority") if isinstance(packet, Mapping) else None
    if not isinstance(authority, Mapping):
        raise SourceFamilyError("chat_migration_authority_missing",
                                "legacy migration authority is missing")
    required = {"schema", "operation", "session_id", "participant", "turn_id",
                "source_family_id", "packet_sha256", "auth"}
    if set(authority) != required or authority.get("schema") != MIGRATION_AUTH_SCHEMA \
            or authority.get("operation") != "legacy_source_family":
        raise SourceFamilyError("chat_migration_authority_invalid",
                                "legacy migration authority is invalid")
    if (authority.get("session_id") != expected_session
            or authority.get("participant") != expected_participant
            or authority.get("turn_id") != expected_turn_id
            or authority.get("source_family_id") != expected_family_id
            or authority.get("packet_sha256") != migration_packet_digest(packet)):
        raise SourceFamilyError("chat_migration_authority_mismatch",
                                "legacy migration authority does not match the source")
    auth = authority.get("auth")
    if not isinstance(auth, str) or not _SHA_RE.fullmatch(auth):
        raise SourceFamilyError("chat_migration_authority_invalid",
                                "legacy migration authority authentication is invalid")
    body = {key: authority[key] for key in required if key != "auth"}
    expected = hmac.new(key.encode("utf-8"), _MIGRATION_DOMAIN + _canonical(body),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(auth, expected):
        raise SourceFamilyError("chat_migration_authority_auth_failed",
                                "legacy migration authority authentication failed")


def source_family_id(*, session_id: str, participant: str,
                     project_root_sha256: str, identity_sha256: str) -> str:
    """Derive the independently expected lineage identifier."""
    if (not isinstance(session_id, str) or not session_id
            or not isinstance(participant, str) or not participant
            or not _SHA_RE.fullmatch(str(project_root_sha256))
            or not _SHA_RE.fullmatch(str(identity_sha256))):
        raise SourceFamilyError("chat_source_family_invalid", "source-family identity inputs are invalid")
    return hashlib.sha256(_canonical({
        "session_id": session_id, "participant": participant,
        "project_root_sha256": project_root_sha256,
        "identity_sha256": identity_sha256,
    })).hexdigest()[:32]


def family_path(owner_dir: str | os.PathLike[str]) -> str:
    return str(Path(owner_dir) / "chat-source-family.json")


def qualification_path(owner_dir: str | os.PathLike[str], turn_id: str) -> str:
    if (not isinstance(turn_id, str) or not turn_id
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in turn_id)):
        raise SourceFamilyError("chat_source_turn_invalid", "source turn id is invalid")
    return str(Path(owner_dir) / f"chat-qualification-{turn_id}.json")


def _atomic_write(path: str, value: Mapping[str, object]) -> None:
    raw = _canonical(value)
    if len(raw) > _MAX_BYTES:
        raise SourceFamilyError("chat_source_family_oversized", "source-family record is oversized")
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    # Include a per-write nonce.  A process can legitimately have concurrent
    # runtime threads on Windows; sharing one ``pid.tmp`` path lets one writer
    # replace or unlink the other writer's open file and produces WinError 32.
    temp = Path(f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise SourceFamilyError("chat_source_family_write_failed", "source-family record cannot be written") from exc
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


@contextmanager
def _family_lock(path: str):
    """Serialize family read/modify/write operations across threads/processes."""
    lock_path = f"{path}.lock"
    with _PROCESS_LOCK:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        acquired = False
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        try:
            # Keep one byte available for the Windows byte-range lock.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while time.monotonic() < deadline:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (OSError, BlockingIOError):
                    time.sleep(0.01)
            if not acquired:
                raise SourceFamilyError("chat_source_family_busy", "source-family record is busy")
            yield
        finally:
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def lock(path: str):
    """Expose the family critical section to a guard boundary.

    Callers must use this only around a current-state read and its durable
    consumption/publication.  The lock is intentionally not a general-purpose
    authority token.
    """
    return _family_lock(path)


def _read_json(path: str) -> dict | None:
    try:
        if os.path.islink(path):
            raise SourceFamilyError("chat_source_family_untrusted", "source-family record is a symlink")
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SourceFamilyError("chat_source_family_untrusted", "source-family record is unavailable") from exc
    if len(raw) > _MAX_BYTES:
        raise SourceFamilyError("chat_source_family_oversized", "source-family record is oversized")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise SourceFamilyError("chat_source_family_untrusted", "source-family record is malformed") from exc
    if not isinstance(value, dict):
        raise SourceFamilyError("chat_source_family_untrusted", "source-family record is not an object")
    return value


def _valid_shape(value: object) -> bool:
    required = {
        "schema", "source_family_id", "session_id", "participant",
        "project_root_sha256", "identity_sha256", "nonce", "qualifications",
        "revocations", "created_at", "auth",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        return False
    if value.get("schema") != SCHEMA or not _ID_RE.fullmatch(str(value.get("source_family_id", ""))):
        return False
    for key in ("session_id", "participant", "nonce"):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 256:
            return False
    if (not _SHA_RE.fullmatch(str(value.get("project_root_sha256", "")))
            or not _SHA_RE.fullmatch(str(value.get("identity_sha256", "")))
            or not isinstance(value.get("auth"), str)
            or not _SHA_RE.fullmatch(value["auth"])):
        return False
    for key in ("qualifications", "revocations"):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > _MAX_QUALIFICATIONS:
            return False
        if key == "revocations":
            if any(not isinstance(item, str) or not _SHA_RE.fullmatch(item) for item in items):
                return False
        else:
            for item in items:
                if (not isinstance(item, Mapping)
                        or set(item) != {"turn_id", "qualification_sha256", "observation_sha256"}
                        or not isinstance(item.get("turn_id"), str)
                        or not _SHA_RE.fullmatch(str(item.get("qualification_sha256", "")))
                        or not _SHA_RE.fullmatch(str(item.get("observation_sha256", "")))):
                    return False
    return True


def read(path: str, *, expected_id: str | None = None,
         expected_session: str | None = None,
         expected_participant: str | None = None,
         expected_project_root_sha256: str | None = None,
         expected_identity_sha256: str | None = None) -> dict:
    value = _read_json(path)
    return validate_record(
        value, expected_id=expected_id, expected_session=expected_session,
        expected_participant=expected_participant,
        expected_project_root_sha256=expected_project_root_sha256,
        expected_identity_sha256=expected_identity_sha256)


def validate_record(value: object, *, expected_id: str | None = None,
                    expected_session: str | None = None,
                    expected_participant: str | None = None,
                    expected_project_root_sha256: str | None = None,
                    expected_identity_sha256: str | None = None) -> dict:
    """Validate an authenticated family supplied by a trusted packet.

    This is deliberately separate from ``read``: legacy migration must be
    able to authenticate a sealed family before its first private sidecar is
    published.  The caller still has to authenticate the migration packet by
    a separate operator/adapter authority; the family MAC alone is not that
    authority.
    """
    if not _valid_shape(value):
        raise SourceFamilyError("chat_source_family_untrusted", "source-family record is invalid")
    value = dict(value)
    body = {key: item for key, item in value.items() if key != "auth"}
    if not hmac.compare_digest(str(value.get("auth", "")), _auth(value["nonce"], body)):
        raise SourceFamilyError("chat_source_family_auth_failed", "source-family authentication failed")
    checks = {
        "source_family_id": expected_id, "session_id": expected_session,
        "participant": expected_participant, "project_root_sha256": expected_project_root_sha256,
        "identity_sha256": expected_identity_sha256,
    }
    if any(expected is not None and value.get(key) != expected for key, expected in checks.items()):
        raise SourceFamilyError("chat_source_family_mismatch", "source-family identity differs")
    return value


def publish(path: str, value: Mapping[str, object], *, expected_id: str,
            expected_session: str, expected_participant: str,
            expected_project_root_sha256: str,
            expected_identity_sha256: str) -> dict:
    """Publish one authenticated family without replacing an existing seal.

    ``publish`` is only for the explicit legacy migration packet.  It refuses
    a family that already carries links/revocations so a caller cannot import
    an unreviewed history into a new room authority.  The packet authority is
    checked by the runtime before this function is called.
    """
    with _family_lock(path):
        existing = _read_json(path)
        if existing is not None:
            return read(path, expected_id=expected_id,
                        expected_session=expected_session,
                        expected_participant=expected_participant,
                        expected_project_root_sha256=expected_project_root_sha256,
                        expected_identity_sha256=expected_identity_sha256)
        family = validate_record(
            value, expected_id=expected_id, expected_session=expected_session,
            expected_participant=expected_participant,
            expected_project_root_sha256=expected_project_root_sha256,
            expected_identity_sha256=expected_identity_sha256)
        if family.get("qualifications") or family.get("revocations"):
            raise SourceFamilyError("chat_source_family_migration_invalid",
                                    "legacy migration family is not unused")
        _atomic_write(path, family)
        return read(path, expected_id=expected_id,
                    expected_session=expected_session,
                    expected_participant=expected_participant,
                    expected_project_root_sha256=expected_project_root_sha256,
                    expected_identity_sha256=expected_identity_sha256)


def publish_qualification(path: str, family: Mapping[str, object],
                          qualification: Mapping[str, object],
                          observation: Mapping[str, object], *,
                          expected_id: str, expected_session: str,
                          expected_participant: str,
                          expected_project_root_sha256: str,
                          expected_identity_sha256: str) -> dict:
    """Recoverably publish a legacy family and its first qualification.

    The qualification file is written before the family link.  If the process
    stops between those writes, the orphaned qualification is not trusted by
    any reader and the same authenticated packet can safely complete the link
    on retry.  A family that already exists is never replaced.
    """
    from _launch_binding import binding_projection
    if not isinstance(family, Mapping) or not isinstance(qualification, Mapping):
        raise SourceFamilyError("chat_qualification_invalid", "qualification is not an object")
    with _family_lock(path):
        existing = _read_json(path)
        if existing is None:
            current = validate_record(
                family, expected_id=expected_id, expected_session=expected_session,
                expected_participant=expected_participant,
                expected_project_root_sha256=expected_project_root_sha256,
                expected_identity_sha256=expected_identity_sha256)
            if current.get("qualifications") or current.get("revocations"):
                raise SourceFamilyError("chat_source_family_migration_invalid",
                                        "legacy migration family is not unused")
        else:
            current = read(path, expected_id=expected_id,
                           expected_session=expected_session,
                           expected_participant=expected_participant,
                           expected_project_root_sha256=expected_project_root_sha256,
                           expected_identity_sha256=expected_identity_sha256)
        projected = binding_projection(observation)
        turn_id = qualification.get("turn_id")
        if projected is None or qualification.get("observation") != projected:
            raise SourceFamilyError("chat_qualification_invalid", "qualification observation differs")
        if qualification.get("source_family_id") != current.get("source_family_id"):
            raise SourceFamilyError("chat_source_family_mismatch", "qualification family differs")
        if not _qualification.valid(qualification, token=current["nonce"],
                                    observation=projected,
                                    source_family_id=current["source_family_id"],
                                    turn_id=turn_id):
            raise SourceFamilyError("chat_qualification_auth_failed", "qualification authentication failed")
        if qualification.get("revocation_id") in current.get("revocations", []):
            raise SourceFamilyError("chat_launch_qualification_revoked", "chat qualification is revoked")
        qual_path = qualification_path(str(Path(path).parent), str(turn_id))
        existing_qualification = _read_json(qual_path)
        if existing_qualification is not None and existing_qualification != qualification:
            raise SourceFamilyError("chat_qualification_conflict", "source-turn qualification already differs")
        if existing_qualification is None:
            _atomic_write(qual_path, qualification)
        qual_sha = _digest(qualification)
        obs_sha = _digest(projected)
        links = [dict(item) for item in current.get("qualifications", [])]
        expected = {"turn_id": turn_id, "qualification_sha256": qual_sha,
                    "observation_sha256": obs_sha}
        prior = next((item for item in links if item.get("turn_id") == turn_id), None)
        if prior is not None and prior != expected:
            raise SourceFamilyError("chat_qualification_conflict", "source-turn link already differs")
        if prior is None:
            links.append(expected)
            links.sort(key=lambda item: item["turn_id"])
            body = {key: item for key, item in current.items() if key != "auth"}
            body["qualifications"] = links
            body["auth"] = _auth(current["nonce"], body)
            _atomic_write(path, body)
        return {"qualification_sha256": qual_sha, "observation_sha256": obs_sha,
                "source_family_id": current["source_family_id"], "turn_id": turn_id}


def ensure(path: str, *, source_family_id_value: str, session_id: str,
           participant: str, project_root_sha256: str,
           identity_sha256: str) -> dict:
    """Create or authenticate one stable family seal without overwriting it."""
    with _family_lock(path):
        existing = _read_json(path)
        if existing is not None:
            return read(path, expected_id=source_family_id_value,
                        expected_session=session_id, expected_participant=participant,
                        expected_project_root_sha256=project_root_sha256,
                        expected_identity_sha256=identity_sha256)
        if source_family_id_value != source_family_id(
                session_id=session_id, participant=participant,
                project_root_sha256=project_root_sha256,
                identity_sha256=identity_sha256):
            raise SourceFamilyError("chat_source_family_mismatch", "source-family id is not independently derived")
        body = {
            "schema": SCHEMA, "source_family_id": source_family_id_value,
            "session_id": session_id, "participant": participant,
            "project_root_sha256": project_root_sha256,
            "identity_sha256": identity_sha256, "nonce": os.urandom(32).hex(),
            "qualifications": [], "revocations": [], "created_at": time.time(),
        }
        value = dict(body, auth=_auth(body["nonce"], body))
        _atomic_write(path, value)
        return read(path, expected_id=source_family_id_value,
                    expected_session=session_id, expected_participant=participant,
                    expected_project_root_sha256=project_root_sha256,
                    expected_identity_sha256=identity_sha256)


def write_qualification(path: str, family: Mapping[str, object], qualification: Mapping[str, object],
                        observation: Mapping[str, object]) -> dict:
    """Persist one already-authenticated qualification and link it atomically."""
    from _launch_binding import binding_projection
    if not isinstance(family, Mapping) or not isinstance(qualification, Mapping):
        raise SourceFamilyError("chat_qualification_invalid", "qualification is not an object")
    turn_id = qualification.get("turn_id")
    if qualification.get("source_family_id") != family.get("source_family_id"):
        raise SourceFamilyError("chat_source_family_mismatch", "qualification family differs")
    projected = binding_projection(observation)
    if projected is None or qualification.get("observation") != projected:
        raise SourceFamilyError("chat_qualification_invalid", "qualification observation differs")
    if not _qualification.valid(
            qualification, token=family["nonce"],
            observation=projected,
            source_family_id=family["source_family_id"],
            turn_id=turn_id):
        raise SourceFamilyError("chat_qualification_auth_failed", "qualification authentication failed")
    family_path_value = str(path)
    qual_path = qualification_path(str(Path(family_path_value).parent), str(turn_id))
    with _family_lock(family_path_value):
        # Always operate on the current authenticated family. A cached caller
        # snapshot must not be able to overwrite a revocation or an unrelated
        # turn link that was published after it was read.
        current_family = read(family_path_value,
                              expected_id=family.get("source_family_id"))
        if (current_family.get("nonce") != family.get("nonce")
                or current_family.get("auth") != family.get("auth")):
            raise SourceFamilyError("chat_source_family_stale", "source-family authority is stale")
        if qualification.get("revocation_id") in current_family.get("revocations", []):
            raise SourceFamilyError("chat_launch_qualification_revoked", "chat qualification is revoked")
        qual_sha = _digest(qualification)
        obs_sha = _digest(projected)
        existing = _read_json(qual_path)
        if existing is not None:
            if existing != qualification:
                raise SourceFamilyError("chat_qualification_conflict", "source-turn qualification already differs")
        else:
            _atomic_write(qual_path, qualification)
        body = {key: item for key, item in current_family.items() if key != "auth"}
        links = [dict(item) for item in current_family.get("qualifications", [])]
        current = next((item for item in links if item.get("turn_id") == turn_id), None)
        expected = {"turn_id": turn_id, "qualification_sha256": qual_sha,
                    "observation_sha256": obs_sha}
        if current is not None and current != expected:
            raise SourceFamilyError("chat_qualification_conflict", "source-turn link already differs")
        if current is None:
            links.append(expected)
            links.sort(key=lambda item: item["turn_id"])
            body["qualifications"] = links
            body["auth"] = _auth(current_family["nonce"], body)
            _atomic_write(family_path_value, body)
        return {"qualification_sha256": qual_sha, "observation_sha256": obs_sha,
                "source_family_id": current_family["source_family_id"], "turn_id": turn_id}


def read_qualification(path: str, family: Mapping[str, object], turn_id: str) -> tuple[dict, dict]:
    with _family_lock(path):
        current = read(path, expected_id=family.get("source_family_id"))
        if (current.get("nonce") != family.get("nonce")
                or current.get("auth") != family.get("auth")):
            raise SourceFamilyError("chat_source_family_stale", "source-family authority is stale")
        value = _read_json(qualification_path(str(Path(path).parent), turn_id))
        if value is None:
            raise SourceFamilyError("chat_launch_qualification_missing", "chat qualification is missing")
        if not _qualification.valid(value, token=current["nonce"],
                                   source_family_id=current["source_family_id"], turn_id=turn_id):
            raise SourceFamilyError("chat_launch_qualification_invalid", "chat qualification is invalid")
        link = next((item for item in current.get("qualifications", [])
                     if item.get("turn_id") == turn_id), None)
        from _launch_binding import binding_projection
        observation = value.get("observation")
        if not isinstance(link, Mapping) or link.get("qualification_sha256") != _digest(value):
            raise SourceFamilyError("chat_launch_qualification_invalid", "chat qualification is not linked")
        if not isinstance(observation, Mapping) or link.get("observation_sha256") != _digest(observation):
            raise SourceFamilyError("chat_launch_qualification_invalid", "chat qualification observation is not linked")
        if binding_projection(observation) != observation:
            raise SourceFamilyError("chat_launch_qualification_invalid", "chat qualification observation is malformed")
        return dict(value), dict(observation)


def revoke(path: str, family: Mapping[str, object], revocation_id: str) -> None:
    if not _SHA_RE.fullmatch(str(revocation_id)):
        raise SourceFamilyError("chat_launch_qualification_invalid", "revocation identity is invalid")
    with _family_lock(path):
        current = read(path, expected_id=family.get("source_family_id"))
        if (current.get("nonce") != family.get("nonce")
                or current.get("auth") != family.get("auth")):
            raise SourceFamilyError("chat_source_family_stale", "source-family authority is stale")
        if revocation_id in current["revocations"]:
            return
        body = {key: item for key, item in current.items() if key != "auth"}
        body["revocations"] = sorted([*current["revocations"], revocation_id])
        body["auth"] = _auth(current["nonce"], body)
        _atomic_write(path, body)


def is_revoked(path: str, family: Mapping[str, object], revocation_id: str) -> bool:
    with _family_lock(path):
        current = read(path, expected_id=family.get("source_family_id"))
        # This read is the revocation linearization point used by the launch
        # guard: revocations published before it are honored; later revokes do
        # not retroactively cancel an already-consumed provider boundary.
        return revocation_id in current["revocations"]


__all__ = ["SCHEMA", "MIGRATION_AUTH_SCHEMA", "SourceFamilyError", "source_family_id",
           "family_path", "lock", "qualification_path", "ensure", "read",
           "validate_record", "publish", "publish_qualification", "write_qualification", "read_qualification",
           "revoke", "is_revoked", "migration_packet_digest",
           "issue_migration_authority", "validate_migration_authority"]
