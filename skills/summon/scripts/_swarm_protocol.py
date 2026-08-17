"""Versioned, provider-neutral wire contract for external Summon workers.

This module intentionally stops at framing and validation.  It does not claim
ownership of a task, launch a provider, or turn a worker message into policy.
The durable coordinator and host adapters are later layers that must use this
contract rather than writing directly into a conversation room journal.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


PROTOCOL = "summon.swarm/v1"
MAX_FRAME_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 48 * 1024
MAX_STRING_CHARS = 512
MAX_MESSAGE_CHARS = 12_000
MAX_CAPABILITIES = 32
MAX_VERSIONS = 8
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERMISSIONS = frozenset({"read-only", "safe-edit"})
# Opaque artifact locators are metadata for a future adapter; this framing
# layer never dereferences them.  Keep the scheme set explicit so a consumer
# cannot accidentally turn a worker-supplied ``file:``, ``javascript:``, or
# custom credential-bearing URI into a fetch target.
_ARTIFACT_URI_SCHEMES = frozenset({"artifact", "https", "http", "ipfs", "s3", "gs"})
_FRAME_TYPES = frozenset({
    "hello", "hello_ack", "register_worker", "poll", "claim", "renew",
    "send_message", "publish_artifact", "complete", "fail", "ack_cancel",
    "shutdown", "claim_requested", "task_claimed", "lease_renewed",
    "claim_released", "message_posted", "artifact_published",
    "cancel_requested", "cancelled", "indeterminate", "task_completed",
    "task_failed", "task_blocked",
})
_FRAME_KEYS = frozenset({"protocol", "run_id", "message_id", "type", "sent_at_ms", "payload"})


class SwarmProtocolError(ValueError):
    """A malformed or unsupported external-worker frame."""


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _text(value: Any, label: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_CHARS:
        raise SwarmProtocolError(f"invalid {label}")
    if any(char in value for char in "\x00\r\n"):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _positive_int(value: Any, label: str, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _list(value: Any, label: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _require(payload: Mapping[str, Any], *keys: str) -> None:
    for key in keys:
        if key not in payload:
            raise SwarmProtocolError(f"missing payload field: {key}")


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise SwarmProtocolError("unknown state-changing payload field")


def _validate_payload(frame_type: str, payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping):
        raise SwarmProtocolError("frame payload must be an object")
    if frame_type == "hello":
        _require(payload, "adapter_id", "host_instance_nonce", "versions",
                 "capabilities", "max_message_bytes", "max_artifact_bytes",
                 "cancellation_supported", "permission_ceiling")
        _reject_unknown(payload, {"adapter_id", "host_instance_nonce", "versions",
                                   "capabilities", "max_message_bytes",
                                   "max_artifact_bytes", "cancellation_supported",
                                   "permission_ceiling"})
        _id(payload["adapter_id"], "adapter id")
        _id(payload["host_instance_nonce"], "host instance nonce")
        versions = _list(payload["versions"], "protocol versions", MAX_VERSIONS)
        if PROTOCOL not in versions or any(not isinstance(item, str) for item in versions):
            raise SwarmProtocolError("protocol versions must include summon.swarm/v1")
        capabilities = _list(payload["capabilities"], "capabilities", MAX_CAPABILITIES)
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in capabilities):
            raise SwarmProtocolError("invalid capability")
        _positive_int(payload["max_message_bytes"], "max message bytes", MAX_MESSAGE_CHARS)
        _positive_int(payload["max_artifact_bytes"], "max artifact bytes", MAX_ARTIFACT_BYTES)
        if not isinstance(payload["cancellation_supported"], bool):
            raise SwarmProtocolError("invalid cancellation capability")
        if payload["permission_ceiling"] not in _PERMISSIONS:
            raise SwarmProtocolError("invalid permission ceiling")
    elif frame_type == "hello_ack":
        _require(payload, "selected_version", "coordinator_id", "capabilities",
                 "permission_ceiling")
        _reject_unknown(payload, {"selected_version", "coordinator_id", "capabilities",
                                   "permission_ceiling"})
        if payload["selected_version"] != PROTOCOL:
            raise SwarmProtocolError("unsupported selected protocol")
        _id(payload["coordinator_id"], "coordinator id")
        capabilities = _list(payload["capabilities"], "capabilities", MAX_CAPABILITIES)
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in capabilities):
            raise SwarmProtocolError("invalid capability")
        if payload["permission_ceiling"] not in _PERMISSIONS:
            raise SwarmProtocolError("invalid permission ceiling")
    elif frame_type == "register_worker":
        _require(payload, "worker_id", "worker_instance_id", "project_root_sha256",
                 "roster_definition_sha256", "capabilities", "permission_ceiling")
        _reject_unknown(payload, {"worker_id", "worker_instance_id", "project_root_sha256",
                                   "roster_definition_sha256", "capabilities",
                                   "permission_ceiling"})
        _id(payload["worker_id"], "worker id")
        _id(payload["worker_instance_id"], "worker instance id")
        _digest(payload["project_root_sha256"], "project root digest")
        _digest(payload["roster_definition_sha256"], "roster definition digest")
        capabilities = _list(payload["capabilities"], "capabilities", MAX_CAPABILITIES)
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in capabilities):
            raise SwarmProtocolError("invalid capability")
        if payload["permission_ceiling"] not in _PERMISSIONS:
            raise SwarmProtocolError("invalid permission ceiling")
    elif frame_type in {"claim", "claim_requested", "task_claimed"}:
        _require(payload, "task_id", "claim_id", "attempt", "lease_generation",
                 "lease_expires_at_ms", "request_sha256")
        _reject_unknown(payload, {"task_id", "claim_id", "attempt", "lease_generation",
                                   "lease_expires_at_ms", "request_sha256"})
        _id(payload["task_id"], "task id")
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["attempt"], "attempt", 1024)
        _positive_int(payload["lease_generation"], "lease generation")
        _positive_int(payload["lease_expires_at_ms"], "lease expiry")
        _digest(payload["request_sha256"], "request digest")
    elif frame_type in {"renew", "lease_renewed", "ack_cancel"}:
        _require(payload, "claim_id", "lease_generation")
        _reject_unknown(payload, {"claim_id", "lease_generation", "lease_expires_at_ms"})
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["lease_generation"], "lease generation")
        if "lease_expires_at_ms" in payload:
            _positive_int(payload["lease_expires_at_ms"], "lease expiry")
    elif frame_type in {"send_message", "message_posted"}:
        _require(payload, "from", "to", "content_sha256", "content_chars", "preview")
        _reject_unknown(payload, {"from", "to", "content_sha256", "content_chars",
                                   "preview", "reply_to", "content"})
        _id(payload["from"], "message sender")
        target = payload["to"]
        if not isinstance(target, str) or not (target in {"coordinator", "human"} or _ID_RE.fullmatch(target)):
            raise SwarmProtocolError("invalid message recipient")
        _digest(payload["content_sha256"], "content digest")
        _positive_int(payload["content_chars"], "content length", MAX_MESSAGE_CHARS)
        _text(payload["preview"], "message preview")
        if "reply_to" in payload and payload["reply_to"] is not None:
            _id(payload["reply_to"], "reply-to message id")
        if "content" in payload:
            _text(payload["content"], "message content")
    elif frame_type in {"publish_artifact", "artifact_published"}:
        # An artifact is a stateful claim side effect, not a free-floating
        # attachment.  Bind it to the same task/attempt/request fence as its
        # completion so a stale worker cannot publish into a successor claim.
        _require(payload, "task_id", "claim_id", "attempt", "lease_generation",
                 "request_sha256", "artifact_id", "sha256", "bytes", "media_type")
        _reject_unknown(payload, {"task_id", "claim_id", "attempt", "lease_generation",
                                   "request_sha256", "artifact_id", "sha256", "bytes",
                                   "media_type", "relative_path", "opaque_uri"})
        _id(payload["task_id"], "task id")
        _id(payload["artifact_id"], "artifact id")
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["attempt"], "attempt", 1024)
        _positive_int(payload["lease_generation"], "lease generation")
        _digest(payload["request_sha256"], "request digest")
        _digest(payload["sha256"], "artifact digest")
        _positive_int(payload["bytes"], "artifact bytes", MAX_ARTIFACT_BYTES)
        _text(payload["media_type"], "artifact media type")
        if "relative_path" in payload and "opaque_uri" in payload:
            raise SwarmProtocolError("artifact must use one locator")
        if "relative_path" in payload:
            path = _text(payload["relative_path"], "artifact relative path")
            if (path is not None and
                    (path.startswith(("/", "\\")) or "\\" in path
                     or re.match(r"^[A-Za-z]:", path)
                     or any(part in {"", ".", ".."} for part in path.split("/")))):
                raise SwarmProtocolError("artifact path must be relative and contained")
        elif "opaque_uri" in payload:
            uri = _text(payload["opaque_uri"], "artifact opaque uri")
            parsed = urlsplit(uri or "")
            if (not parsed.scheme
                    or parsed.scheme.lower() not in _ARTIFACT_URI_SCHEMES
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or any(char in (uri or "") for char in "\\\r\n\x00")):
                raise SwarmProtocolError("artifact opaque uri is invalid")
        else:
            raise SwarmProtocolError("artifact needs relative_path or opaque_uri")
    elif frame_type in {"complete", "task_completed", "fail", "task_failed", "task_blocked"}:
        _require(payload, "task_id", "claim_id", "attempt", "lease_generation",
                 "request_sha256", "envelope_sha256")
        _reject_unknown(payload, {"task_id", "claim_id", "attempt", "lease_generation",
                                   "request_sha256", "envelope_sha256", "reason"})
        _id(payload["task_id"], "task id")
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["attempt"], "attempt", 1024)
        _positive_int(payload["lease_generation"], "lease generation")
        _digest(payload["request_sha256"], "request digest")
        _digest(payload["envelope_sha256"], "envelope digest")
        if "reason" in payload:
            _text(payload["reason"], "completion reason")
    elif frame_type in {"cancel_requested", "cancelled", "indeterminate"}:
        _require(payload, "claim_id", "lease_generation")
        _reject_unknown(payload, {"claim_id", "lease_generation", "reason"})
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["lease_generation"], "lease generation")
        if "reason" in payload:
            _text(payload["reason"], "cancellation reason")
    elif frame_type == "claim_released":
        _require(payload, "task_id", "claim_id", "attempt", "lease_generation",
                 "request_sha256")
        _reject_unknown(payload, {"task_id", "claim_id", "attempt", "lease_generation",
                                   "request_sha256"})
        _id(payload["task_id"], "task id")
        _id(payload["claim_id"], "claim id")
        _positive_int(payload["attempt"], "attempt", 1024)
        _positive_int(payload["lease_generation"], "lease generation")
        _digest(payload["request_sha256"], "request digest")
    elif frame_type in {"poll", "shutdown"}:
        if payload:
            raise SwarmProtocolError(f"{frame_type} payload must be empty")


def make_frame(frame_type: str, *, run_id: str, message_id: str,
               sent_at_ms: int, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if frame_type not in _FRAME_TYPES:
        raise SwarmProtocolError("unsupported swarm frame type")
    _id(run_id, "run id")
    _id(message_id, "message id")
    _positive_int(sent_at_ms, "sent timestamp")
    if payload is not None and not isinstance(payload, Mapping):
        raise SwarmProtocolError("frame payload must be an object")
    body = dict(payload or {})
    _validate_payload(frame_type, body)
    frame = {"protocol": PROTOCOL, "run_id": run_id, "message_id": message_id,
             "type": frame_type, "sent_at_ms": sent_at_ms, "payload": body}
    encoded = json.dumps(frame, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES or len(json.dumps(body, ensure_ascii=False,
                                                        separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise SwarmProtocolError("swarm frame is too large")
    return frame


def encode_frame(frame: Mapping[str, Any]) -> bytes:
    parsed = parse_frame(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
    return (json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def parse_frame(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        if len(line) > MAX_FRAME_BYTES:
            raise SwarmProtocolError("swarm frame is too large")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SwarmProtocolError("swarm frame is not UTF-8") from exc
    elif isinstance(line, str):
        if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
            raise SwarmProtocolError("swarm frame is too large")
        text = line
    else:
        raise SwarmProtocolError("swarm frame must be text or UTF-8 bytes")
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SwarmProtocolError("swarm frame is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise SwarmProtocolError("unsupported swarm protocol")
    if set(value) - _FRAME_KEYS:
        raise SwarmProtocolError("unknown swarm frame field")
    frame_type = value.get("type")
    if frame_type not in _FRAME_TYPES:
        raise SwarmProtocolError("unsupported swarm frame type")
    _id(value.get("run_id"), "run id")
    _id(value.get("message_id"), "message id")
    _positive_int(value.get("sent_at_ms"), "sent timestamp")
    payload = value.get("payload")
    _validate_payload(frame_type, payload)
    encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded_payload) > MAX_PAYLOAD_BYTES:
        raise SwarmProtocolError("swarm payload is too large")
    return value


def frame_sha256(frame: Mapping[str, Any]) -> str:
    parsed = parse_frame(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
    canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["PROTOCOL", "MAX_FRAME_BYTES", "MAX_PAYLOAD_BYTES", "SwarmProtocolError",
           "make_frame", "parse_frame", "encode_frame", "frame_sha256"]
