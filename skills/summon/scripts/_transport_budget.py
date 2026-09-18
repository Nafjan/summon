"""Provider-free, operation-bound transport-size admission facts.

This module measures the exact serialized subprocess invocation before a child
is contacted.  It deliberately does not choose a fallback or write a receipt;
the caller must refuse or hold the complete operation when the declared route
cannot carry it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Any, Mapping


SCHEMA = "summon.transport-budget/v1"
DEFAULT_ZCODE_ATTACHMENT_BYTES = 8 * 1024 * 1024
DEFAULT_STRUCTURED_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_SYSTEM_FILE_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 16 * 1024
_CAPABILITY_FIELDS = frozenset({
    "kind", "platform", "max_utf16_units", "max_single_argument_bytes",
    "max_env_utf16_units", "max_total_bytes", "max_attachment_bytes",
    "max_serialized_bytes",
})


class TransportBudgetError(ValueError):
    """Malformed operation or capability facts."""


def _fail(message: str) -> None:
    raise TransportBudgetError(message)


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        _fail(f"invalid {label}")
    return value


def _bytes(value: Any, label: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if type(value) is str:
        try:
            return value.encode("utf-8", "surrogatepass")
        except UnicodeEncodeError as exc:
            raise TransportBudgetError(f"invalid {label}") from exc
    _fail(f"invalid {label}")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _argv_measurement(command: str, args: list[str], env: Mapping[str, Any] | None,
                      *, platform: str, capability: Mapping[str, Any]) -> dict[str, int]:
    if platform == "nt":
        line = subprocess.list2cmdline([command, *args])
        units = len(line.encode("utf-16-le", "surrogatepass")) // 2 + 1
        limit = capability.get("max_utf16_units")
        if type(limit) is not int or limit < 1:
            _fail("argv capability requires max_utf16_units")
        measurement = {"serialized_utf16_units": units,
                       "limit_utf16_units": limit}
        env_limit = capability.get("max_env_utf16_units")
        if env_limit is not None:
            if type(env_limit) is not int or env_limit < 1:
                _fail("argv capability requires max_env_utf16_units")
            env_block = "" if env is None else "\0".join(
                f"{key}={value}" for key, value in env.items()) + "\0\0"
            env_units = len(env_block.encode("utf-16-le", "surrogatepass")) // 2
            measurement.update({"serialized_env_utf16_units": env_units,
                                "limit_env_utf16_units": env_limit})
        return measurement
    if platform != "posix":
        _fail("unsupported transport platform")
    max_argument = capability.get("max_single_argument_bytes")
    max_total = capability.get("max_total_bytes")
    if type(max_argument) is not int or max_argument < 1 or type(max_total) is not int or max_total < 1:
        _fail("argv capability requires explicit POSIX byte limits")
    encoded_args = [_bytes(item, "argv item") for item in [command, *args]]
    env_items = [] if env is None else [
        (_bytes(key, "environment key"), _bytes(value, "environment value"))
        for key, value in env.items()
    ]
    # Match _builder.argv_length_error: MAX_ARG_STRLEN counts the terminating
    # NUL in the admission limit, but the measured payload is the argument
    # bytes themselves. Environment entries carry ``=`` plus a terminating NUL
    # and the command name is not counted as an argument NUL here.
    longest = max((len(item) for item in encoded_args), default=0)
    total = (len(encoded_args[0])
             + sum(len(item) + 1 for item in encoded_args[1:])
             + sum(len(key) + len(value) + 2
                   for key, value in env_items) if env_items
             else len(encoded_args[0])
             + sum(len(item) + 1 for item in encoded_args[1:]))
    return {"max_argument_bytes": longest, "limit_single_argument_bytes": max_argument,
            "serialized_arg_env_bytes": total, "limit_total_bytes": max_total}


def evaluate_invocation(*, operation_id: str, content: bytes | str,
                        command: str, args: list[str] | tuple[str, ...],
                        env: Mapping[str, Any] | None, platform: str,
                        capability: Mapping[str, Any],
                        attachment_bytes: int | None = None) -> dict[str, Any]:
    """Return a deterministic accepted/blocked fact for one exact operation.

    ``operation_id`` and the digest of the supplied content bind the result to
    one attempt.  The result never authorizes provider contact or mutation.
    """
    if type(operation_id) is not str or not _ID.fullmatch(operation_id):
        _fail("invalid operation id")
    if type(command) is not str or not command:
        _fail("invalid command")
    if type(args) not in (list, tuple) or any(type(item) is not str for item in args):
        _fail("invalid argv")
    if type(capability) is not dict:
        _fail("transport capability is required")
    if set(capability) - _CAPABILITY_FIELDS:
        _fail("transport capability contains unknown fields")
    kind = capability.get("kind")
    if kind not in {"argv", "private_attachment", "system_file"}:
        _fail("unsupported transport capability")
    declared_platform = capability.get("platform")
    if declared_platform is not None:
        if declared_platform not in {"nt", "posix"} or declared_platform != platform:
            _fail("transport capability platform does not match invocation")
    if kind == "argv" and any(field in capability for field in
                               ("max_attachment_bytes", "max_serialized_bytes")):
        _fail("argv capability contains attachment limits")
    if kind in {"private_attachment", "system_file"} and any(
            field in capability for field in
            ("max_utf16_units", "max_single_argument_bytes", "max_total_bytes",
             "max_serialized_bytes")):
        _fail("attachment capability contains argv limits")
    content_bytes = _bytes(content, "content")
    if len(content_bytes) > 16 * 1024 * 1024:
        _fail("content exceeds transport fact bound")
    measurement = _argv_measurement(command, list(args), env, platform=platform,
                                     capability=capability) if kind == "argv" else {}
    status, reason = "accepted", "none"
    if kind == "argv":
        if platform == "nt" and measurement["serialized_utf16_units"] > measurement["limit_utf16_units"]:
            status, reason = "blocked", "serialized_invocation_too_large"
        elif (platform == "nt"
              and measurement.get("serialized_env_utf16_units", 0)
              > measurement.get("limit_env_utf16_units", 2**63 - 1)):
            status, reason = "blocked", "serialized_environment_too_large"
        elif platform == "posix" and (
                measurement["max_argument_bytes"] >= measurement["limit_single_argument_bytes"]
                or measurement["serialized_arg_env_bytes"] > measurement["limit_total_bytes"]):
            status, reason = "blocked", "serialized_invocation_too_large"
    else:
        if type(attachment_bytes) is not int or attachment_bytes < 0:
            _fail(f"{kind} capability requires attachment_bytes")
        max_attachment = capability.get("max_attachment_bytes")
        if type(max_attachment) is not int or max_attachment < 1:
            _fail(f"{kind} capability requires max_attachment_bytes")
        measurement = {"attachment_bytes": attachment_bytes,
                       "limit_attachment_bytes": max_attachment}
        if attachment_bytes > max_attachment:
            status, reason = "blocked", (
                "system_file_too_large" if kind == "system_file"
                else "private_attachment_too_large")
    result = {
        "schema": SCHEMA,
        "operation_id": operation_id,
        "content_sha256": _digest(content_bytes),
        "content_bytes": len(content_bytes),
        "capability": {"kind": kind, "platform": platform},
        "status": status,
        "reason": reason,
        "measurement": measurement,
        "whole_message": True,
        "provider_contacted": False,
        "state_mutated": False,
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_RESULT_BYTES:
        _fail("transport result exceeds bound")
    return result


def evaluate_serialized_payload(*, operation_id: str, payload: bytes | str,
                                capability: Mapping[str, Any],
                                boundary: str) -> dict[str, Any]:
    """Measure one complete structured request before its final write.

    ACP JSON-RPC frames and OpenAI-compatible HTTP bodies do not travel in an
    argv token, so the subprocess argv seam cannot prove their size.  This
    helper binds the exact UTF-8 bytes that the adapter is about to write to a
    local operational ceiling.  The ceiling is deliberately a Summon safety
    bound, not a provider context-window or billing claim.
    """
    if type(operation_id) is not str or not _ID.fullmatch(operation_id):
        _fail("invalid operation id")
    if type(boundary) is not str or not boundary or len(boundary) > 128:
        _fail("invalid serialized boundary")
    if type(capability) is not dict:
        _fail("transport capability is required")
    if set(capability) - _CAPABILITY_FIELDS:
        _fail("transport capability contains unknown fields")
    if capability.get("kind") != "serialized":
        _fail("serialized payload requires a serialized capability")
    platform = capability.get("platform")
    if platform is not None and platform not in {"nt", "posix"}:
        _fail("serialized capability has unsupported platform")
    limit = capability.get("max_serialized_bytes")
    if type(limit) is not int or limit < 1:
        _fail("serialized capability requires max_serialized_bytes")
    content_bytes = _bytes(payload, "serialized payload")
    if len(content_bytes) > 16 * 1024 * 1024:
        _fail("serialized payload exceeds transport fact bound")
    result = {
        "schema": SCHEMA,
        "operation_id": operation_id,
        "content_sha256": _digest(content_bytes),
        "content_bytes": len(content_bytes),
        "boundary": boundary,
        "capability": {"kind": "serialized", "platform": platform},
        "status": "accepted" if len(content_bytes) <= limit else "blocked",
        "reason": ("none" if len(content_bytes) <= limit
                   else "serialized_payload_too_large"),
        "measurement": {"serialized_bytes": len(content_bytes),
                         "limit_serialized_bytes": limit},
        "whole_message": True,
        "provider_contacted": False,
        "state_mutated": False,
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_RESULT_BYTES:
        _fail("transport result exceeds bound")
    return result


def result_sha256(result: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(dict(result), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise TransportBudgetError("invalid transport result") from exc
    return hashlib.sha256(encoded).hexdigest()
