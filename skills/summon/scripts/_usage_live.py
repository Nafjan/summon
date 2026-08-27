"""Provider-inert foundation for explicitly authorized live usage refresh.

The module contains no process, socket, or HTTP execution surface.  A caller
must supply a runner and explicitly consent to an account-usage read.  The
runner receives a bounded, one-attempt Codex app-server plan; raw output is
bounded and redacted before JSON parsing.  Only a normalized advisory is
persisted in an authenticated, owner-only private sidecar.

This is intentionally separate from :mod:`_usage`, whose unsigned operator
import contract remains unchanged.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

import _evidence
import _fleet_approval
from _job_control import ControlBusyError, _exclusive_control_lock
from _jobs import _atomic_write_json


PUBLIC_SCHEMA = "summon.usage-live-public/v1"
STORE_SCHEMA = "summon.usage-live-private/v1"
ANCHOR_SCHEMA = "summon.usage-live-anchor/v1"
CHECKPOINT_SCHEMA = "summon.usage-live-checkpoint/v1"
PLAN_SCHEMA = "summon.usage-live-runner-plan/v1"
CODEX_SCHEMA_CLI_VERSION = "codex-cli 0.147.0"
CODEX_SCHEMA_FIXTURE_SHA256 = (
    "327afd2b30f585d41a9b92b2959b509db2b3bf3f9b258084d38201ff95e09850")

MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 8 * 1024
COMMAND_TIMEOUT_MS = 10_000
TOTAL_TIMEOUT_MS = 25_000
MAX_JSON_ITEMS = 4096
MAX_JSON_DEPTH = 64
MAX_STORE_BYTES = 1024 * 1024
MAX_GENERATION = (1 << 63) - 1
DEFAULT_TTL_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 300

CAPABILITIES = {
    "codex": {
        "provider": "codex", "state": "fixture_supported",
        "mechanism": "app_server_jsonrpc", "live_enabled": True,
        "tested_cli_version": CODEX_SCHEMA_CLI_VERSION,
        "fixture_sha256": CODEX_SCHEMA_FIXTURE_SHA256,
    },
    "arkcli": {
        "provider": "arkcli", "state": "schema_unverified",
        "mechanism": None, "live_enabled": False,
    },
    "claude": {
        "provider": "claude", "state": "unsupported",
        "mechanism": None, "live_enabled": False,
    },
    "kimi": {
        "provider": "kimi", "state": "unsupported",
        "mechanism": None, "live_enabled": False,
    },
    "agy": {
        # AGY 1.1.11+ documents provider-free print-mode usage commands, but
        # Summon has not yet frozen and reviewed a version-pinned JSON fixture.
        "provider": "agy", "state": "schema_unverified",
        "mechanism": None, "live_enabled": False,
    },
}

_REDACTED_KEYS = (
    "access_token", "accessToken", "refresh_token", "refreshToken", "token",
    "api_key", "apiKey", "authorization", "password", "secret",
    "name",
)
_SENSITIVE_FIELD = re.compile(
    r'("(?:' + "|".join(re.escape(value) for value in _REDACTED_KEYS)
    + r')"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}")
_ACCOUNT_IDENTITY_FIELD = re.compile(
    r'("(?:accountId|account_id|email|id)"\s*:\s*)"((?:[^"\\]|\\.)*)"',
    re.IGNORECASE)
_STORE_PROCESS_LOCK = threading.RLock()


class UsageLiveError(_evidence.EvidenceError):
    """A typed, public-safe live-usage error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def capabilities() -> list[dict]:
    """Return a fresh public capability registry."""
    return [dict(CAPABILITIES[name]) for name in sorted(CAPABILITIES)]


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in CAPABILITIES:
        raise UsageLiveError("provider_unsupported", "usage provider is unsupported")
    return value


def _base_projection(provider: str) -> dict:
    capability = CAPABILITIES[provider]
    return {
        "schema": PUBLIC_SCHEMA,
        "provider": provider,
        "capability": capability["state"],
        "advisory_only": True,
        "routing_changed": False,
        "provider_contacted": False,
        "attempts": 0,
        "execution_status": "not_run",
    }


def preflight(provider: str, *, allow_account_usage_read: bool = False,
              dry_run: bool = False) -> dict:
    """Provider-inert consent and capability preflight."""
    provider = _provider(provider)
    result = _base_projection(provider)
    state = CAPABILITIES[provider]["state"]
    if state == "schema_unverified":
        return {**result, "status": "blocked", "error_kind": "schema_unverified"}
    if state != "fixture_supported":
        return {**result, "status": "blocked", "error_kind": "usage_refresh_unsupported"}
    if allow_account_usage_read is not True:
        return {**result, "status": "blocked", "error_kind": "consent_required"}
    return {
        **result, "status": "success", "error_kind": None,
        "eligible": True, "dry_run": bool(dry_run),
    }


def codex_request_plan(*, allow_account_usage_read: bool = False,
                       dry_run: bool = False) -> dict:
    """Return the exact bounded request plan; never execute it."""
    check = preflight(
        "codex", allow_account_usage_read=allow_account_usage_read,
        dry_run=dry_run)
    if check["status"] != "success":
        return check
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "summon-usage", "version": "1"},
            "capabilities": {},
        }},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/read", "params": {
            "refreshToken": False,
        }},
        {"jsonrpc": "2.0", "id": 3, "method": "account/rateLimits/read", "params": {}},
    )
    return {
        "schema": PLAN_SCHEMA,
        "provider": "codex",
        "transport": "app_server_jsonrpc",
        "argv": ["codex", "app-server"],
        "required_cli_version": CODEX_SCHEMA_CLI_VERSION,
        "schema_fixture_sha256": CODEX_SCHEMA_FIXTURE_SHA256,
        "requests": [dict(item) for item in requests],
        "command_timeout_ms": COMMAND_TIMEOUT_MS,
        "total_timeout_ms": TOTAL_TIMEOUT_MS,
        "max_stdout_bytes": MAX_STDOUT_BYTES,
        "max_stderr_bytes": MAX_STDERR_BYTES,
        "max_attempts": 1,
        "retry_allowed": False,
        "provider_contact_expected": True,
        "dry_run": bool(dry_run),
    }


def _store_path(path: str | None = None) -> str:
    raw = path or os.environ.get("SUMMON_USAGE_LIVE_STORE") or os.path.join(
        os.path.expanduser("~"), ".agents", "summon", "usage-live.json")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        raise UsageLiveError("private_store_invalid", "usage live store must be absolute")
    return os.path.abspath(expanded)


def _key_path(path: str) -> str:
    return path + ".key"


def _anchor_path(path: str) -> str:
    return path + ".anchor"


def _checkpoint_root() -> str:
    raw = os.environ.get("SUMMON_USAGE_LIVE_CHECKPOINT_ROOT") or os.path.join(
        os.path.expanduser("~"), ".agents", "summon-usage-live-checkpoints")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        raise UsageLiveError(
            "private_store_invalid", "usage live checkpoint root must be absolute")
    return os.path.abspath(expanded)


def _store_path_digest(path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _checkpoint_path(path: str) -> str:
    return os.path.join(_checkpoint_root(), _store_path_digest(path) + ".json")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UsageLiveError("private_store_invalid", "usage value is not canonical") from exc


def _domain_key(master: bytes, domain: bytes) -> bytes:
    return hmac.new(master, b"summon.usage-live/v1\0" + domain,
                    hashlib.sha256).digest()


def _mac(master: bytes, domain: bytes, value: Mapping[str, Any]) -> str:
    return hmac.new(_domain_key(master, domain), _canonical(dict(value)),
                    hashlib.sha256).hexdigest()


def _secure_root_for(path: str) -> None:
    _fleet_approval._secure_private_root(os.path.dirname(path))


def _load_key(path: str, *, create: bool) -> bytes | None:
    selected = _key_path(path)
    if not os.path.exists(selected):
        if not create:
            return None
        _secure_root_for(path)
        body = {"schema": "summon.usage-live-key/v1", "key": secrets.token_hex(32)}
        _atomic_write_json(selected, body)
        _fleet_approval._secure_file(selected)
    _fleet_approval._regular_single_link(selected, "usage live key")
    _fleet_approval._verify_private(selected, directory=False)
    try:
        if os.path.getsize(selected) > 1024:
            raise ValueError
        raw = json.loads(Path(selected).read_text(encoding="utf-8"))
        key = bytes.fromhex(raw["key"])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise UsageLiveError("private_store_invalid", "usage live key is invalid") from exc
    if raw.get("schema") != "summon.usage-live-key/v1" or len(key) != 32:
        raise UsageLiveError("private_store_invalid", "usage live key is invalid")
    return key


def _key_binding(path: str, key: bytes) -> dict | None:
    """Return the authenticated store checkpoint carried by the key record."""
    selected = _key_path(path)
    if not os.path.exists(selected):
        return None
    try:
        value = json.loads(Path(selected).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UsageLiveError("private_store_invalid", "usage live key is invalid") from exc
    binding = value.get("store_binding") if isinstance(value, dict) else None
    if binding is None:
        return None
    if (not isinstance(binding, dict)
            or set(binding) != {"store_id", "generation", "store_mac"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("store_id", "")))
            or not isinstance(binding.get("generation"), int)
            or isinstance(binding.get("generation"), bool)
            or not 0 <= binding["generation"] <= MAX_GENERATION
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("store_mac", "")))):
        raise UsageLiveError("private_store_invalid", "usage live key binding is invalid")
    expected = _mac(key, b"key-binding", binding)
    if not hmac.compare_digest(str(value.get("store_binding_mac", "")), expected):
        raise UsageLiveError(
            "private_store_tampered", "usage live key binding failed authentication")
    return binding


def _bind_key_to_store(path: str, key: bytes, store: dict) -> None:
    binding = {
        "store_id": store["store_id"], "generation": store["generation"],
        "store_mac": store["mac"],
    }
    record = {
        "schema": "summon.usage-live-key/v1", "key": key.hex(),
        "store_binding": binding,
        "store_binding_mac": _mac(key, b"key-binding", binding),
    }
    _atomic_write_json(_key_path(path), record)
    _fleet_approval._secure_file(_key_path(path))


def _json_file(path: str, field: str) -> dict:
    _fleet_approval._regular_single_link(path, field)
    _fleet_approval._verify_private(path, directory=False)
    try:
        if os.path.getsize(path) > MAX_STORE_BYTES:
            raise ValueError
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UsageLiveError("private_store_invalid", f"{field} is invalid") from exc
    if not isinstance(value, dict):
        raise UsageLiveError("private_store_invalid", f"{field} is invalid")
    return value


def _unsigned(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "mac"}


def _validate_store(value: dict, key: bytes) -> dict:
    body = _unsigned(value)
    if (body.get("schema") != STORE_SCHEMA
            or not isinstance(body.get("store_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", body["store_id"])
            or not isinstance(body.get("generation"), int)
            or isinstance(body.get("generation"), bool)
            or not 0 <= body["generation"] <= MAX_GENERATION
            or not isinstance(body.get("observations"), dict)
            or set(body["observations"]) - set(CAPABILITIES)
            or set(body) != {
                "schema", "store_id", "generation", "last_seen_at", "observations",
            }
            or not hmac.compare_digest(str(value.get("mac", "")),
                                       _mac(key, b"store", body))):
        raise UsageLiveError("private_store_tampered", "usage live store failed authentication")
    _parse_time(body.get("last_seen_at"), "last_seen_at")
    return value


def _validate_anchor(value: dict, key: bytes, store: dict) -> None:
    body = _unsigned(value)
    expected_fields = {"schema", "store_id", "generation", "store_mac"}
    if (set(body) != expected_fields or body.get("schema") != ANCHOR_SCHEMA
            or body.get("store_id") != store.get("store_id")
            or body.get("generation") != store.get("generation")
            or body.get("store_mac") != store.get("mac")
            or not hmac.compare_digest(str(value.get("mac", "")),
                                       _mac(key, b"anchor", body))):
        raise UsageLiveError(
            "private_store_rollback_or_torn_write",
            "usage live store and monotonic anchor disagree")


def _validate_checkpoint(value: dict, key: bytes, store: dict,
                         path: str) -> None:
    body = _unsigned(value)
    expected_fields = {
        "schema", "store_path_sha256", "store_id", "generation", "store_mac",
    }
    if (set(body) != expected_fields
            or body.get("schema") != CHECKPOINT_SCHEMA
            or body.get("store_path_sha256") != _store_path_digest(path)
            or body.get("store_id") != store.get("store_id")
            or body.get("generation") != store.get("generation")
            or body.get("store_mac") != store.get("mac")
            or not hmac.compare_digest(str(value.get("mac", "")),
                                       _mac(key, b"checkpoint", body))):
        raise UsageLiveError(
            "private_store_rollback_or_torn_write",
            "usage live independent checkpoint disagrees with the store")


def _write_checkpoint(path: str, store: dict, key: bytes) -> None:
    root = _checkpoint_root()
    _fleet_approval._secure_private_root(root)
    body = {
        "schema": CHECKPOINT_SCHEMA,
        "store_path_sha256": _store_path_digest(path),
        "store_id": store["store_id"],
        "generation": store["generation"],
        "store_mac": store["mac"],
    }
    checkpoint = {**body, "mac": _mac(key, b"checkpoint", body)}
    selected = _checkpoint_path(path)
    _atomic_write_json(selected, checkpoint)
    _fleet_approval._secure_file(selected)


def _stored_time(store: dict) -> _dt.datetime:
    return _parse_time(store["last_seen_at"], "last_seen_at")


def _read_store(path: str, key: bytes | None, *, now: _dt.datetime) -> dict | None:
    store_exists = os.path.exists(path)
    anchor_exists = os.path.exists(_anchor_path(path))
    checkpoint_exists = os.path.exists(_checkpoint_path(path))
    if not store_exists and not anchor_exists:
        if checkpoint_exists:
            raise UsageLiveError(
                "private_store_rollback_or_torn_write",
                "usage live initialized store is missing")
        if key is not None and _key_binding(path, key) is not None:
            raise UsageLiveError(
                "private_store_rollback_or_torn_write",
                "usage live initialized store is missing")
        return None
    if key is None or store_exists != anchor_exists or not checkpoint_exists:
        raise UsageLiveError(
            "private_store_rollback_or_torn_write",
            "usage live store is incomplete")
    _secure_root_for(path)
    store = _validate_store(_json_file(path, "usage live store"), key)
    anchor = _json_file(_anchor_path(path), "usage live anchor")
    _validate_anchor(anchor, key, store)
    _fleet_approval._secure_private_root(_checkpoint_root())
    checkpoint = _json_file(_checkpoint_path(path), "usage live checkpoint")
    _validate_checkpoint(checkpoint, key, store, path)
    binding = _key_binding(path, key)
    if binding is not None and binding != {
            "store_id": store["store_id"], "generation": store["generation"],
            "store_mac": store["mac"]}:
        raise UsageLiveError(
            "private_store_rollback_or_torn_write",
            "usage live key checkpoint and store disagree")
    if _stored_time(store) > now + _dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise UsageLiveError(
            "private_store_clock_rollback",
            "usage live clock moved behind its authenticated checkpoint")
    return store


def _write_store(path: str, value: dict, key: bytes) -> dict:
    body = _unsigned(value)
    authenticated = {**body, "mac": _mac(key, b"store", body)}
    anchor_body = {
        "schema": ANCHOR_SCHEMA, "store_id": body["store_id"],
        "generation": body["generation"], "store_mac": authenticated["mac"],
    }
    anchor = {**anchor_body, "mac": _mac(key, b"anchor", anchor_body)}
    if len(_canonical(authenticated)) > MAX_STORE_BYTES:
        raise UsageLiveError("private_store_full", "usage live store is full")
    # Each replacement is atomic.  A crash between replacements is detectable
    # and fails closed rather than accepting a rolled-back observation.
    _atomic_write_json(path, authenticated)
    _fleet_approval._secure_file(path)
    _atomic_write_json(_anchor_path(path), anchor)
    _fleet_approval._secure_file(_anchor_path(path))
    # The key record is a third authenticated checkpoint for the co-located
    # store pair. A crash during this multi-file commit fails closed on read.
    _bind_key_to_store(path, key, authenticated)
    # This checkpoint lives under an independently configured private root.
    # It detects replay or deletion of the co-located key/store/anchor set.
    # A local account owner can still deliberately delete every private root;
    # defending against that requires an OS-backed external trust anchor.
    _write_checkpoint(path, authenticated, key)
    return authenticated


def _advance_last_seen(path: str, store: dict | None, key: bytes | None,
                       now: _dt.datetime) -> dict | None:
    if store is None:
        return None
    assert key is not None
    previous = _stored_time(store)
    if now <= previous:
        return store
    if store["generation"] >= MAX_GENERATION:
        raise UsageLiveError("private_store_full", "usage live generation is exhausted")
    updated = {
        **_unsigned(store),
        "generation": store["generation"] + 1,
        "last_seen_at": _format_time(now),
    }
    return _write_store(path, updated, key)


@contextmanager
def _locked_store(path: str):
    _secure_root_for(path)
    with _STORE_PROCESS_LOCK:
        try:
            with _exclusive_control_lock(path + ".lock"):
                yield
        except ControlBusyError as exc:
            raise UsageLiveError("private_store_busy", "usage live store is busy") from exc


def _parse_time(value: Any, field: str) -> _dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid")
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid")
    return parsed.astimezone(_dt.timezone.utc)


def _format_time(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: str | None) -> _dt.datetime:
    return _parse_time(value, "now") if value is not None else _dt.datetime.now(_dt.timezone.utc)


def _redact_wire(value: bytes | str, identity_key: bytes) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UsageLiveError("provider_response_invalid", "provider output is not UTF-8") from exc
    elif isinstance(value, str):
        text = value
    else:
        raise UsageLiveError("runner_contract_invalid", "runner output has an invalid type")
    _reject_escaped_json_keys(text)
    # Stable account identity is needed only to detect an account switch. Turn
    # candidate identity strings into a keyed marker *before* JSON parsing so
    # plaintext email/account identifiers never enter the parsed object.
    def replace_identity(match: re.Match[str]) -> str:
        try:
            # Decode the JSON string scalar so semantically identical wire
            # spellings (for example ``a`` and ``\u0061``) share one private
            # account scope. The plaintext scalar exists only long enough to
            # compute the keyed marker and never enters the parsed response.
            identity = json.loads('"' + match.group(2) + '"')
            if not isinstance(identity, str):
                raise ValueError("identity is not text")
            identity_bytes = identity.encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, json.JSONDecodeError) as exc:
            raise UsageLiveError(
                "provider_response_invalid",
                "provider account identity is invalid") from exc
        marker = hmac.new(
            _domain_key(identity_key, b"wire-account-identity"),
            identity_bytes, hashlib.sha256).hexdigest()
        return match.group(1) + '"hmac:' + marker + '"'

    text = _ACCOUNT_IDENTITY_FIELD.sub(replace_identity, text)
    text = _SENSITIVE_FIELD.sub(lambda match: match.group(1) + '"<redacted>"', text)
    return _BEARER.sub("Bearer <redacted>", text)


def _reject_escaped_json_keys(text: str) -> None:
    """Reject escaped object keys before any provider JSON is materialized.

    The redaction allowlist intentionally operates on raw wire spelling. JSON
    escapes in keys could otherwise turn an unrecognized raw key into ``email``
    or ``accessToken`` only after parsing. Rejecting every escaped key keeps the
    confidentiality boundary simple and fail-closed; values may still contain
    ordinary JSON escapes and are validated after decoding.
    """
    index = 0
    length = len(text)
    while index < length:
        if text[index] != '"':
            index += 1
            continue
        index += 1
        escaped_key = False
        while index < length:
            character = text[index]
            if character == "\\":
                escaped_key = True
                index += 2
                continue
            if character == '"':
                index += 1
                cursor = index
                while cursor < length and text[cursor] in " \t\r\n":
                    cursor += 1
                if cursor < length and text[cursor] == ":" and escaped_key:
                    raise UsageLiveError(
                        "provider_response_invalid",
                        "provider output uses an escaped object key")
                break
            index += 1


def _validate_decoded_json(value: Any, *, depth: int = 0,
                           array_items: list[int] | None = None) -> None:
    """Bound arrays/depth and reject strings that cannot be UTF-8 encoded."""
    if array_items is None:
        array_items = [0]
    if depth > MAX_JSON_DEPTH:
        raise UsageLiveError("provider_response_invalid", "provider output is too deep")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise UsageLiveError(
                "provider_response_invalid", "provider output contains invalid text") from exc
        return
    if isinstance(value, list):
        array_items[0] += len(value)
        if array_items[0] > MAX_JSON_ITEMS:
            raise UsageLiveError("provider_response_invalid", "provider output is too complex")
        for item in value:
            _validate_decoded_json(item, depth=depth + 1, array_items=array_items)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_decoded_json(key, depth=depth + 1, array_items=array_items)
            _validate_decoded_json(item, depth=depth + 1, array_items=array_items)


def _parse_json_lines(stdout: bytes | str, identity_key: bytes) -> dict[int, dict]:
    redacted = _redact_wire(stdout, identity_key)
    counter = [0]

    def check_depth(line: str) -> None:
        depth = 0
        quoted = False
        escaped = False
        for char in line:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise UsageLiveError(
                        "provider_response_invalid", "provider output is too deeply nested")
            elif char in "]}":
                depth -= 1

    def pairs(items):
        counter[0] += len(items)
        if counter[0] > MAX_JSON_ITEMS:
            raise UsageLiveError("provider_response_invalid", "provider output is too complex")
        result = {}
        for key, item in items:
            if key in result:
                raise UsageLiveError("provider_response_invalid", "provider output has duplicate fields")
            result[key] = item
        return result

    responses: dict[int, dict] = {}
    try:
        for line in redacted.splitlines():
            if not line.strip():
                continue
            check_depth(line)
            item = json.loads(line, object_pairs_hook=pairs,
                              parse_constant=lambda _v: (_ for _ in ()).throw(
                                  UsageLiveError("provider_response_invalid",
                                                  "provider output has non-finite numbers")))
            _validate_decoded_json(item)
            raw_id = item.get("id") if isinstance(item, dict) else None
            if type(raw_id) is int and raw_id in {1, 2, 3}:
                response_id = raw_id
                if response_id in responses:
                    raise UsageLiveError(
                        "provider_response_invalid",
                        "provider output has a duplicate response id")
                responses[response_id] = item
    except UsageLiveError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
        raise UsageLiveError("provider_response_invalid", "provider output is invalid JSON") from exc
    if set(responses) != {1, 2, 3}:
        raise UsageLiveError("provider_response_incomplete", "provider output is incomplete")
    return responses


def _result(value: dict, response_id: int) -> dict:
    item = value[response_id]
    if "error" in item:
        error = item.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        authentication = (
            code in {401, 403, -32001}
            or any(token in message.lower() for token in (
                "auth", "unauthorized", "login", "token", "credential"))
        )
        raise UsageLiveError(
            "authentication_failed" if authentication else "provider_request_failed",
            "Codex app-server request failed")
    result = item.get("result")
    if not isinstance(result, dict):
        raise UsageLiveError("provider_response_invalid", "Codex app-server result is invalid")
    return result


def _account_identity(account_result: dict) -> str:
    account = account_result.get("account", account_result)
    if not isinstance(account, dict):
        raise UsageLiveError("account_identity_absent", "Codex account identity is absent")
    for key in ("accountId", "account_id", "id", "email"):
        value = account.get(key)
        if (isinstance(value, str) and re.fullmatch(r"hmac:[0-9a-f]{64}", value)):
            return value
    raise UsageLiveError("account_identity_absent", "Codex account identity is absent")


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid") from exc
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise UsageLiveError("provider_response_invalid", f"{field} is invalid")
    return number


def _primary_bucket(result: dict) -> tuple[float | None, Any]:
    limits = result.get("rateLimits", result)
    if not isinstance(limits, dict):
        raise UsageLiveError("provider_response_invalid", "Codex rate limits are invalid")
    top = limits.get("primary")
    # Current app-server fixtures expose the per-limit map as a sibling of
    # ``rateLimits``. Accept the earlier nested fixture shape as input, but do
    # not let it hide a sibling map if both are present.
    by_id = result.get("rateLimitsByLimitId", limits.get("rateLimitsByLimitId"))
    codex_limits = by_id.get("codex") if isinstance(by_id, dict) else None
    nested = codex_limits.get("primary") if isinstance(codex_limits, dict) else None

    def used(bucket):
        if not isinstance(bucket, dict):
            return None
        return _number(bucket.get("usedPercent"), "usedPercent")

    top_used = used(top)
    nested_used = used(nested)
    if top_used is not None and nested_used is not None and top_used != nested_used:
        return None, None
    chosen = top if top_used is not None else nested
    chosen_used = top_used if top_used is not None else nested_used
    if chosen_used is None or not isinstance(chosen, dict):
        raise UsageLiveError("provider_response_invalid", "Codex primary rate limit is absent")
    return chosen_used, chosen.get("resetsAt", chosen.get("resetAt"))


def _reset_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return _format_time(parsed)
    if isinstance(value, str):
        try:
            return _format_time(_parse_time(value, "reset_at"))
        except UsageLiveError:
            return None
    return None


def _provider_contacted(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    contacted = value.get("provider_contacted")
    if contacted is not None and not isinstance(contacted, bool):
        raise UsageLiveError(
            "runner_contract_invalid", "runner provider contact state is invalid")
    return contacted


def _wire_size(value: bytes | str) -> int:
    if isinstance(value, bytes):
        return len(value)
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise UsageLiveError(
            "runner_contract_invalid", "runner output is not valid UTF-8") from exc


def _normalize_runner_result(
        value: Any) -> tuple[bytes | str, bytes | str, int, int, bool | None]:
    if not isinstance(value, dict):
        raise UsageLiveError("runner_contract_invalid", "runner result must be an object")
    if value.get("cli_version") != CODEX_SCHEMA_CLI_VERSION:
        raise UsageLiveError(
            "schema_version_mismatch",
            "Codex CLI version differs from the reviewed usage schema fixture")
    stdout = value.get("stdout", b"")
    stderr = value.get("stderr", b"")
    if not isinstance(stdout, (bytes, str)) or not isinstance(stderr, (bytes, str)):
        raise UsageLiveError("runner_contract_invalid", "runner output has an invalid type")
    stdout_size = _wire_size(stdout)
    stderr_size = _wire_size(stderr)
    if stdout_size > MAX_STDOUT_BYTES:
        raise UsageLiveError("stdout_limit_exceeded", "provider stdout exceeded its bound")
    if stderr_size > MAX_STDERR_BYTES:
        raise UsageLiveError("stderr_limit_exceeded", "provider stderr exceeded its bound")
    exit_code = value.get("exit_code")
    elapsed_ms = value.get("elapsed_ms")
    if (isinstance(exit_code, bool) or not isinstance(exit_code, int)
            or isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int)
            or elapsed_ms < 0):
        raise UsageLiveError("runner_contract_invalid", "runner status is invalid")
    if elapsed_ms > TOTAL_TIMEOUT_MS:
        raise UsageLiveError("usage_refresh_timeout", "usage refresh exceeded its total bound")
    if exit_code != 0:
        # Stderr is deliberately not classified or returned: it can contain
        # account information, paths, credentials, or raw provider text.
        reported = value.get("error_kind")
        kind = reported if reported in {
            "authentication_failed", "usage_refresh_timeout",
            "backend_execution_failed",
        } else "backend_execution_failed"
        raise UsageLiveError(kind, "Codex usage refresh failed")
    return stdout, stderr, exit_code, elapsed_ms, _provider_contacted(value)


def _scope_hmac(key: bytes, provider: str, identity: str) -> str:
    return hmac.new(_domain_key(key, b"account-scope"),
                    (provider + "\0" + identity).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _public_observation(observation: dict, *, now: _dt.datetime,
                        refresh_status: str = "success",
                        error_kind: str | None = None) -> dict:
    retrieved = _parse_time(observation["retrieved_at"], "retrieved_at")
    if retrieved > now + _dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise UsageLiveError(
            "private_store_clock_rollback",
            "usage live clock moved behind the observation time")
    expires = retrieved + _dt.timedelta(seconds=observation["ttl_seconds"])
    result = {
        **_base_projection(observation["provider"]),
        "status": "success" if refresh_status == "success" else "partial",
        "execution_status": "success" if refresh_status == "success" else "error",
        "attempts": 1,
        "provider_contacted": observation.get("provider_contacted"),
        "refresh_status": refresh_status,
        "error_kind": error_kind,
        "dimension": "rate_limit",
        "support": observation["support"],
        "account_scope_hmac": observation["account_scope_hmac"],
        "retrieved_at": observation["retrieved_at"],
        # Freshness describes the age of the successfully observed local CLI
        # result. It is deliberately independent of provider_contacted, whose
        # tri-state records whether the injected runner crossed that boundary.
        "freshness_basis": "local_command_observation",
        "freshness": "fresh" if now <= expires else "stale",
        "expires_at": _format_time(expires),
    }
    if observation.get("remaining") is not None:
        result["remaining"] = dict(observation["remaining"])
    if observation.get("reset_at") is not None:
        result["reset_at"] = observation["reset_at"]
    return result


def _cached_failure(path: str, provider: str, now: _dt.datetime,
                    kind: str, *, provider_contacted: bool | None = None) -> dict:
    with _locked_store(path):
        key = _load_key(path, create=False)
        store = _read_store(path, key, now=now)
        store = _advance_last_seen(path, store, key, now)
        observation = (store or {}).get("observations", {}).get(provider)
    if isinstance(observation, dict):
        projected = _public_observation(
            observation, now=now, refresh_status="error", error_kind=kind)
        projected["provider_contacted"] = provider_contacted
        projected["cache_preserved"] = True
        return projected
    return {
        **_base_projection(provider), "status": "error",
        "execution_status": "error", "attempts": 1,
        "provider_contacted": provider_contacted, "refresh_status": "error",
        "error_kind": kind, "cache_preserved": False,
    }


def refresh_codex(*, allow_account_usage_read: bool = False,
                  dry_run: bool = False,
                  runner: Callable[[dict], dict] | None = None,
                  store_file: str | None = None,
                  now: str | None = None) -> dict:
    """Run one explicitly authorized refresh through an injected runner.

    No retry, fallback, login, repair, reset-credit operation, model dispatch,
    or built-in process execution exists in this function.
    """
    plan = codex_request_plan(
        allow_account_usage_read=allow_account_usage_read, dry_run=dry_run)
    if plan.get("schema") != PLAN_SCHEMA:
        return plan
    if dry_run:
        return {
            **_base_projection("codex"), "status": "success",
            "error_kind": None, "dry_run": True, "eligible": True,
            "plan": plan,
        }
    if runner is None or not callable(runner):
        return {
            **_base_projection("codex"), "status": "blocked",
            "error_kind": "runner_required",
        }
    checked_at = _now(now)
    path = _store_path(store_file)
    with _locked_store(path):
        wire_key = _load_key(path, create=True)
        assert wire_key is not None
        existing = _read_store(path, wire_key, now=checked_at)
        _advance_last_seen(path, existing, wire_key, checked_at)
    contacted: bool | None = None
    try:
        try:
            raw = runner(plan)
        except Exception:
            # Runner failure cannot establish whether its child crossed the
            # provider boundary. Do not guess that it was an auth failure.
            return _cached_failure(
                path, "codex", checked_at, "runner_failed",
                provider_contacted=None)
        contacted = _provider_contacted(raw)
        stdout, _stderr, _exit, _elapsed, _contacted = _normalize_runner_result(raw)
        responses = _parse_json_lines(stdout, wire_key)
        _result(responses, 1)
        account_identity = _account_identity(_result(responses, 2))
        used_percent, reset = _primary_bucket(_result(responses, 3))
    except UsageLiveError as exc:
        return _cached_failure(
            path, "codex", checked_at, exc.kind,
            provider_contacted=contacted)

    with _locked_store(path):
        key = _load_key(path, create=True)
        assert key is not None
        if not hmac.compare_digest(key, wire_key):
            raise UsageLiveError(
                "private_store_key_changed", "usage live key changed during refresh")
        store = _read_store(path, key, now=checked_at)
        if store is None:
            store = {
                "schema": STORE_SCHEMA, "store_id": secrets.token_hex(32),
                "generation": 0, "last_seen_at": _format_time(checked_at),
                "observations": {},
            }
        if store["generation"] >= MAX_GENERATION:
            raise UsageLiveError("private_store_full", "usage live generation is exhausted")
        scope = _scope_hmac(key, "codex", account_identity)
        prior = store["observations"].get("codex")
        observation = {
            "provider": "codex", "support": "supported" if used_percent is not None else "unknown",
            "account_scope_hmac": scope, "retrieved_at": _format_time(checked_at),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
            "provider_contacted": contacted,
        }
        if used_percent is not None:
            observation["remaining"] = {
                "value": round(100.0 - used_percent, 6), "unit": "percent",
            }
        rendered_reset = _reset_at(reset)
        if rendered_reset is not None:
            observation["reset_at"] = rendered_reset
        store = {
            **_unsigned(store), "generation": store["generation"] + 1,
            "last_seen_at": _format_time(max(_stored_time(store), checked_at)),
            "observations": {**store["observations"], "codex": observation},
        }
        _write_store(path, store, key)
    public = _public_observation(observation, now=checked_at)
    public["account_changed"] = bool(
        isinstance(prior, dict) and prior.get("account_scope_hmac") != scope)
    public["cache_preserved"] = False
    return public


def status(*, store_file: str | None = None, now: str | None = None) -> dict:
    """Read the authenticated private cache without contacting a provider."""
    checked_at = _now(now)
    path = _store_path(store_file)
    with _locked_store(path):
        key = _load_key(path, create=False)
        store = _read_store(path, key, now=checked_at)
        store = _advance_last_seen(path, store, key, checked_at)
        values = list((store or {}).get("observations", {}).values())
    return {
        "schema": PUBLIC_SCHEMA, "status": "success",
        "provider_contacted": False, "advisory_only": True,
        "routing_changed": False,
        "observations": [_public_observation(item, now=checked_at) for item in values],
        "capabilities": capabilities(),
    }


__all__ = [
    "PUBLIC_SCHEMA", "STORE_SCHEMA", "PLAN_SCHEMA", "CAPABILITIES",
    "CODEX_SCHEMA_CLI_VERSION", "CODEX_SCHEMA_FIXTURE_SHA256",
    "MAX_STDOUT_BYTES", "MAX_STDERR_BYTES", "COMMAND_TIMEOUT_MS",
    "TOTAL_TIMEOUT_MS", "UsageLiveError", "capabilities", "preflight",
    "codex_request_plan", "refresh_codex", "status",
]
