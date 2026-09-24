"""Private parent/dispatcher handoff for chat provider-bound observations.

The conversation parent starts a dispatcher process before the dispatcher
resolves the provider executable.  This file is the small authenticated bridge
between those processes: the child records the fresh observation it measured
immediately before provider Popen, and the parent can bind that observation to
the room turn without exposing paths, prompts, credentials, or bearer handles.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext


SCHEMA = "summon.chat-launch-guard/v1"
SCHEMA_V2 = "summon.chat-launch-guard/v2"
_MAX_BYTES = 32 * 1024
_MAX_FRESH_SECONDS = 30 * 60


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _auth(token: str, body: Mapping[str, object], schema: str = SCHEMA) -> str:
    domain = (b"summon-chat-launch-guard/v2:"
              if schema == SCHEMA_V2 else b"summon-chat-launch-guard/v1:")
    return hmac.new(token.encode("utf-8"),
                    domain + _canonical(dict(body)),
                    hashlib.sha256).hexdigest()


def _write(path: str, value: dict[str, object]) -> None:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise ValueError("chat launch guard is oversized")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read(path: str, token: str, *, schema: str = SCHEMA) -> dict[str, object]:
    if not isinstance(path, str) or not path or not isinstance(token, str) or not token:
        raise ValueError("chat launch guard is unavailable")
    with open(path, "rb") as handle:
        raw = handle.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("chat launch guard is oversized")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError("chat launch guard schema is invalid")
    auth = value.get("auth")
    body = {key: item for key, item in value.items() if key != "auth"}
    if not isinstance(auth, str) or not hmac.compare_digest(auth, _auth(token, body, schema)):
        raise ValueError("chat launch guard authentication failed")
    return value


@contextmanager
def _lock(path: str):
    lock_path = path + ".lock"
    token = f"{os.getpid()}-{time.time_ns()}"
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def create(path: str, token: str, *, expected: Mapping[str, object] | None,
           backend: str, transport: str, turn_id: str,
           owner_generation: int, owner_nonce: str,
           attempt_id: str,
           qualification: Mapping[str, object] | None = None,
           qualification_required: bool = False,
           source_family_id: str | None = None,
           qualification_source_turn_id: str | None = None,
           qualification_token: str | None = None,
           source_family_path: str | None = None) -> None:
    # An expected binding denotes a continuation rather than a fresh turn.
    # Continuations require the separate source-family qualification; callers
    # cannot downgrade that requirement by omitting the flag.
    qualification_required = bool(qualification_required or expected is not None)
    body: dict[str, object] = {
        "schema": SCHEMA, "phase": "pending", "backend": backend,
        "transport": transport, "expected": dict(expected) if expected else None,
        "turn_id": turn_id, "owner_generation": owner_generation,
        "owner_nonce": owner_nonce,
        "attempt_id": attempt_id,
        "qualification": dict(qualification) if qualification else None,
        "qualification_required": bool(qualification_required),
        "source_family_id": source_family_id,
        "qualification_source_turn_id": qualification_source_turn_id,
        "qualification_token_sha256": (
            hashlib.sha256(qualification_token.encode("utf-8")).hexdigest()
            if isinstance(qualification_token, str) and qualification_token else None),
        "source_family_path": source_family_path,
        "created_at": time.time(),
    }
    _write(path, {**body, "auth": _auth(token, body)})


def create_v2(path: str, token: str, *, expected: Mapping[str, object] | None,
              session_id: str, participant: str, policy: Mapping[str, object],
              payload_sha256: str, context_selection_sha256: str,
              backend: str, transport: str, turn_id: str,
              owner_generation: int, owner_nonce: str, attempt_id: str,
              qualification: Mapping[str, object] | None = None,
              qualification_required: bool = False,
              source_family_id: str | None = None,
              qualification_source_turn_id: str | None = None,
              qualification_token: str | None = None,
              source_family_path: str | None = None) -> None:
    """Create an authenticated v2 guard bound to the exact chat payload.

    The v1 guard remains readable for historical rooms.  New chat turns use
    this additive shape so a valid executable observation alone cannot be
    mistaken for accounting evidence for a different turn or payload.
    """
    if (not isinstance(session_id, str) or not session_id
            or not isinstance(participant, str) or not participant
            or not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
            or not isinstance(context_selection_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", context_selection_sha256)):
        raise ValueError("chat launch guard v2 identity is invalid")
    try:
        import _context_policy
        checked_policy = _context_policy.validate(dict(policy))
    except (ImportError, TypeError, ValueError) as exc:
        raise ValueError("chat launch guard v2 policy is invalid") from exc
    qualification_required = bool(qualification_required or expected is not None)
    body: dict[str, object] = {
        "schema": SCHEMA_V2, "phase": "pending", "backend": backend,
        "transport": transport, "expected": dict(expected) if expected else None,
        "session_id": session_id, "participant": participant, "turn_id": turn_id,
        "owner_generation": owner_generation, "owner_nonce": owner_nonce,
        "attempt_id": attempt_id, "policy": checked_policy,
        "policy_sha256": hashlib.sha256(_canonical(checked_policy)).hexdigest(),
        "payload_sha256": payload_sha256,
        "context_selection_sha256": context_selection_sha256,
        "qualification": dict(qualification) if qualification else None,
        "qualification_required": bool(qualification_required),
        "source_family_id": source_family_id,
        "qualification_source_turn_id": qualification_source_turn_id,
        "qualification_token_sha256": (
            hashlib.sha256(qualification_token.encode("utf-8")).hexdigest()
            if isinstance(qualification_token, str) and qualification_token else None),
        "source_family_path": source_family_path,
        "created_at": time.time(),
    }
    _write(path, {**body, "auth": _auth(token, body, SCHEMA_V2)})


def _fresh_projection(evidence: object, *, backend: str, transport: str) -> dict[str, object]:
    from _launch_binding import binding_projection, valid_observation
    if not isinstance(evidence, Mapping):
        raise ValueError("chat launch observation is missing")
    observation = evidence.get("launch_observation")
    if not valid_observation(observation):
        raise ValueError("chat launch observation is invalid")
    if observation.get("backend") != backend or observation.get("transport") != transport:
        raise ValueError("chat launch observation route changed")
    from _resume_capabilities import resume_capability_v2
    row = resume_capability_v2("resume", backend, transport)
    expected_scope = {
        "registry_generation": row.get("registry_generation"),
        "registry_digest": row.get("registry_digest"),
        "adapter": row.get("adapter"),
        "adapter_version": row.get("adapter_version_scope"),
        "external_cli_version_scope": row.get("external_cli_version_scope"),
    }
    if any(observation.get(key) != value for key, value in expected_scope.items()):
        raise ValueError("chat launch observation scope changed")
    executable_sha = observation.get("executable_sha256")
    if (not isinstance(executable_sha, str)
            or observation.get("executable_content_revision") != f"sha256:{executable_sha}"):
        raise ValueError("chat launch observation lacks executable content identity")
    projected = binding_projection(observation)
    if projected is None:
        raise ValueError("chat launch observation projection is unavailable")
    return projected


def before_launch(path: str, token: str, evidence: object, *, backend: str,
                  transport: str, attempt_id: str,
                  qualification_token: str | None = None,
                  source_family_path: str | None = None) -> None:
    _before_launch(path, token, evidence, backend=backend, transport=transport,
                   attempt_id=attempt_id, qualification_token=qualification_token,
                   source_family_path=source_family_path, schema=SCHEMA)


def before_launch_v2(path: str, token: str, evidence: object, *, backend: str,
                     transport: str, attempt_id: str,
                     qualification_token: str | None = None,
                     source_family_path: str | None = None) -> None:
    _before_launch(path, token, evidence, backend=backend, transport=transport,
                   attempt_id=attempt_id, qualification_token=qualification_token,
                   source_family_path=source_family_path, schema=SCHEMA_V2)


def _before_launch(path: str, token: str, evidence: object, *, backend: str,
                   transport: str, attempt_id: str,
                   qualification_token: str | None = None,
                   source_family_path: str | None = None,
                   schema: str = SCHEMA) -> None:
    with _lock(path):
        current = _read(path, token, schema=schema)
        family_path = None
        family_boundary = nullcontext()
        if current.get("phase") != "pending":
            raise ValueError("chat launch guard was already consumed")
        if current.get("backend") != backend or current.get("transport") != transport:
            raise ValueError("chat launch guard route changed")
        if current.get("attempt_id") != attempt_id:
            raise ValueError("chat launch guard attempt changed")
        if schema == SCHEMA_V2:
            if (not isinstance(current.get("session_id"), str)
                    or not isinstance(current.get("participant"), str)
                    or not isinstance(current.get("policy"), Mapping)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("policy_sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("payload_sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("context_selection_sha256", "")))):
                raise ValueError("chat launch guard v2 accounting binding is invalid")
            try:
                import _context_policy
                policy = _context_policy.validate(dict(current["policy"]))
            except (ImportError, TypeError, ValueError) as exc:
                raise ValueError("chat launch guard v2 policy is invalid") from exc
            if hashlib.sha256(_canonical(policy)).hexdigest() != current.get("policy_sha256"):
                raise ValueError("chat launch guard v2 policy digest changed")
            # The parent binds the final prompt bytes and physical attempt;
            # the child must present those same digests from the invocation it
            # is actually about to launch. Route/observation evidence alone is
            # not enough because a caller could reuse a valid observation for
            # a different payload or attempt.
            payload_digest = evidence.get("dispatch_payload_sha256") if isinstance(evidence, Mapping) else None
            attempt_digest = evidence.get("attempt_id_sha256") if isinstance(evidence, Mapping) else None
            if (not isinstance(payload_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_digest)
                    or payload_digest != current.get("payload_sha256")
                    or not isinstance(attempt_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", attempt_digest)
                    or attempt_digest != hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()):
                raise ValueError("chat launch guard v2 invocation binding is invalid")
        owner_nonce = current.get("owner_nonce")
        owner_generation = current.get("owner_generation")
        if (not isinstance(owner_nonce, str) or not owner_nonce
                or not isinstance(owner_generation, int)
                or isinstance(owner_generation, bool) or owner_generation < 1):
            raise ValueError("chat launch guard owner fence is invalid")
        # Re-read the live owner fence immediately before the provider
        # boundary. The parent already checked this before Popen, but the
        # child must not rely on a stale parent-side check after a takeover.
        from _rundir import read_owner, _effective_expiry
        owner_dir = os.path.dirname(os.path.abspath(path))
        owner = read_owner(owner_dir)
        if (not isinstance(owner, Mapping)
                or owner.get("nonce") != owner_nonce
                or owner.get("generation") != owner_generation
                or _effective_expiry(owner_dir, dict(owner)) <= time.time()):
            raise ValueError("chat launch owner fence is no longer current")
        created = current.get("created_at")
        if (not isinstance(created, (int, float)) or isinstance(created, bool)
                or time.time() - float(created) > _MAX_FRESH_SECONDS):
            raise ValueError("chat launch guard is stale")
        projected = _fresh_projection(evidence, backend=backend, transport=transport)
        observed_at_ns = (evidence.get("launch_observation", {}).get("observed_at_ns")
                          if isinstance(evidence, Mapping)
                          and isinstance(evidence.get("launch_observation"), Mapping)
                          else None)
        now_ns = time.time_ns()
        if (not isinstance(observed_at_ns, int) or isinstance(observed_at_ns, bool)
                or observed_at_ns <= 0 or observed_at_ns > now_ns
                or now_ns - observed_at_ns > _MAX_FRESH_SECONDS * 1_000_000_000):
            raise ValueError("chat launch observation is stale")
        expected = current.get("expected")
        if expected is not None and projected != expected:
            raise ValueError("chat launch observation does not match the bound continuation")
        if current.get("qualification_required") is True:
            qualification = current.get("qualification")
            presented = (evidence.get("launch_qualification")
                         if isinstance(evidence, Mapping) else None)
            from _chat_launch_qualification import valid as valid_qualification
            if not isinstance(qualification, Mapping) or not isinstance(presented, Mapping):
                raise ValueError("chat launch qualification is missing")
            expected_source = current.get("source_family_id")
            expected_turn = current.get("qualification_source_turn_id")
            if (not isinstance(expected_source, str) or not re.fullmatch(r"[a-f0-9]{32}", expected_source)
                    or not isinstance(expected_turn, str) or not expected_turn):
                raise ValueError("chat launch qualification authority is invalid")
            if (not isinstance(qualification_token, str) or not qualification_token
                    or current.get("qualification_token_sha256") != hashlib.sha256(
                        qualification_token.encode("utf-8")).hexdigest()):
                raise ValueError("chat launch qualification token is invalid")
            if qualification != presented or not valid_qualification(
                    qualification, token=qualification_token,
                    observation=evidence.get("launch_observation"),
                    source_family_id=expected_source, turn_id=expected_turn):
                raise ValueError("chat launch qualification is invalid")
            family_path = source_family_path or current.get("source_family_path")
            if not isinstance(family_path, str) or not family_path:
                raise ValueError("chat launch qualification authority is unavailable")
            import _chat_source_family
            family_boundary = _chat_source_family.lock(family_path)
        # Keep the current revocation read and the guard's durable phase
        # transition in one ordered critical section. A revoke that races this
        # boundary therefore either wins before consumption (and blocks launch)
        # or is published after the guard is durably observed.
        with family_boundary:
            if current.get("qualification_required") is True:
                try:
                    family = _chat_source_family.read(
                        family_path, expected_id=expected_source)
                    if str(qualification.get("revocation_id")) in family.get("revocations", []):
                        raise ValueError("chat launch qualification is revoked")
                except _chat_source_family.SourceFamilyError as exc:
                    raise ValueError("chat launch qualification authority is invalid") from exc
            body = dict(current)
            body.update({"phase": "observed", "observation": projected,
                         "observed_at": time.time()})
            body.pop("auth", None)
            _write(path, {**body, "auth": _auth(token, body, schema)})


def read(path: str, token: str) -> dict[str, object] | None:
    try:
        value = _read(path, token)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    observation = value.get("observation")
    return dict(observation) if value.get("phase") == "observed" and isinstance(observation, dict) else None


def read_v2(path: str, token: str) -> dict[str, object] | None:
    """Authenticate and return the v2 binding plus its observed projection."""
    try:
        value = _read(path, token, schema=SCHEMA_V2)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    observation = value.get("observation")
    if value.get("phase") != "observed" or not isinstance(observation, dict):
        return None
    return {
        "observation": dict(observation),
        "session_id": value.get("session_id"),
        "participant": value.get("participant"),
        "turn_id": value.get("turn_id"),
        "attempt_id": value.get("attempt_id"),
        "policy": dict(value["policy"]) if isinstance(value.get("policy"), Mapping) else None,
        "payload_sha256": value.get("payload_sha256"),
        "context_selection_sha256": value.get("context_selection_sha256"),
    }


def read_qualification(path: str, token: str) -> dict[str, object] | None:
    """Return the committed qualification only after authenticating the guard."""
    try:
        value = _read(path, token)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    qualification = value.get("qualification")
    return (dict(qualification) if isinstance(qualification, Mapping) else None)


__all__ = ["SCHEMA", "SCHEMA_V2", "create", "create_v2", "before_launch",
           "before_launch_v2", "read", "read_v2", "read_qualification"]
