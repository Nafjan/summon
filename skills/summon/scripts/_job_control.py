"""Durable single-machine controls for adaptive background jobs.

Commands are private local operator state.  They are never copied into public
telemetry or release evidence.  A command queues intent; it does not claim a
provider accepted steering or that a cancellation was instantaneous.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import threading
import time
from contextlib import contextmanager

import _jobs


MAX_COMMANDS = 128
MAX_BREAKER_COMMANDS = 96
MAX_STEER_CHARS = 16_384
MAX_EXTENSION_MS = 7 * 24 * 60 * 60 * 1000
LOCK_WAIT_SECONDS = 5.0
MAX_CONTROL_BYTES = 512 * 1024


class ControlBusyError(ValueError):
    """The local control-file lock is held by another live operation."""


def control_path(root: str, job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.control.json")


def heartbeat_path(root: str, job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.heartbeat.json")


def source_admission_lock(root: str, job_id: str):
    """Serialize source-bound launch admission with qualification revocation.

    This lock is deliberately distinct from the job-control and resume-ledger
    locks.  Callers acquire it first, then any ledger lock, so a revocation
    that wins before admission is observed and a revocation that waits until
    after the durable CAS is not retroactive cancellation.
    """
    if not _jobs.valid_job_id(job_id):
        raise ValueError("source admission job id is invalid")
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return _exclusive_control_lock(
        os.path.join(base, f"{job_id}.resume-admission.lock"))


def _auth(nonce: str, domain: str, payload: dict) -> str:
    """Bind a local control payload to one job secret.

    This detects stale, cross-job, and accidentally/tampered records.  It is not
    a security boundary against a same-user process that can read the immutable
    launch record and therefore obtain the nonce.
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hmac.new(nonce.encode("utf-8"),
                    domain.encode("ascii") + b":" + body,
                    hashlib.sha256).hexdigest()


def heartbeat_auth(nonce: str, payload: dict) -> str:
    """Authenticate the complete v2 heartbeat body (excluding ``auth``)."""
    return _auth(nonce, "summon-heartbeat/v2", payload)


def legacy_heartbeat_auth(nonce: str, job_id: str) -> str:
    """Read-only compatibility for the unreleased v1 draft."""
    return hmac.new(nonce.encode("utf-8"),
                    f"summon-heartbeat:{job_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def public_heartbeat(value: dict, integrity: str) -> dict | None:
    """Return a typed, allowlisted heartbeat projection.

    Authentication proves which worker wrote a heartbeat; it does not make
    arbitrary nested strings safe to publish. Unknown fields are omitted.
    """
    if not isinstance(value, dict) or value.get("schema") not in {
            "summon.job-heartbeat/v1", "summon.job-heartbeat/v2"}:
        return None
    out = {"schema": value["schema"], "integrity": integrity}
    for key in ("job_id", "attempt_id"):
        item = value.get(key)
        if isinstance(item, str) and _jobs.valid_job_id(item):
            out[key] = item
    for key in ("attempt_ordinal", "job_elapsed_ms", "auto_extensions",
                "operator_extensions", "extension_ms", "control_generation"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 2 ** 63 - 1:
            out[key] = item
    for key in ("job_started_at", "job_hard_deadline_at", "observed_at"):
        item = value.get(key)
        if (isinstance(item, (int, float)) and not isinstance(item, bool)
                and math.isfinite(item) and item >= 0):
            out[key] = item
    for key in ("adaptive", "attention_required", "cancel_requested", "control_untrusted"):
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    for key, allowed in {
        "attempt_kind": {"initial", "transient_retry", "acp_fallback",
                         "schema_correction", "contract_repair"},
        "state": {"running", "repairing", "complete", "error", "cancelled"},
        "control_scope": {"current_attempt_replayed_from_job_origin"},
    }.items():
        if value.get(key) in allowed:
            out[key] = value[key]
    steering = value.get("steering")
    if isinstance(steering, dict):
        queued = steering.get("queued")
        mode = steering.get("mode")
        safe_steering = {}
        if isinstance(queued, int) and not isinstance(queued, bool) and 0 <= queued <= MAX_COMMANDS:
            safe_steering["queued"] = queued
        if mode == "queued_for_resume":
            safe_steering["mode"] = mode
        if safe_steering:
            out["steering"] = safe_steering
    live = value.get("liveness")
    if isinstance(live, dict):
        safe_live = {}
        if live.get("schema") == "summon.liveness/v1":
            safe_live["schema"] = live["schema"]
        phase = live.get("phase")
        if isinstance(phase, str) and phase in {"startup", "generation", "reconnect", "finalization",
                                                "terminal", "cancelled", "timed_out"}:
            safe_live["phase"] = phase
        for key in ("elapsed_ms", "first_trusted_event_ms", "last_meaningful_event_ms"):
            item = live.get(key)
            if item is None or (isinstance(item, int) and not isinstance(item, bool)
                                and 0 <= item <= 2 ** 63 - 1):
                safe_live[key] = item
        expired = live.get("expired")
        if expired is None or (isinstance(expired, str) and expired in {
                "startup_timeout", "overall_timeout",
                "generation_idle_timeout", "finalization_timeout"}):
            safe_live["expired"] = expired
        trusted_kind = live.get("last_trusted_kind")
        if trusted_kind is None or (isinstance(trusted_kind, str) and trusted_kind in {
                "transport_started", "stream_event", "output_text", "tool_activity",
                "reconnect", "finalizing", "terminal", "cancelled"}):
            safe_live["last_trusted_kind"] = trusted_kind
        activity_kind = live.get("last_activity_kind")
        if activity_kind is None or (isinstance(activity_kind, str) and activity_kind in {"tool", "generation"}):
            safe_live["last_activity_kind"] = activity_kind
        counts = live.get("counts")
        if isinstance(counts, dict):
            safe_counts = {}
            for key in ("trusted", "meaningful", "ignored", "untrusted", "duplicates",
                        "reordered", "reconnects", "tools"):
                item = counts.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 2 ** 63 - 1:
                    safe_counts[key] = item
            if safe_counts:
                safe_live["counts"] = safe_counts
        if safe_live:
            out["liveness"] = safe_live
    try:
        from _progress_judge import sanitize_record
        progress = sanitize_record(value.get("progress"))
    except Exception:  # noqa: BLE001 - optional advisory field
        progress = None
    if progress is not None:
        out["progress"] = progress
    try:
        from _breaker import sanitize_record as _sanitize_breaker
        breaker = _sanitize_breaker(value.get("breaker"))
    except Exception:  # noqa: BLE001 - optional advisory field
        breaker = None
    if breaker is not None:
        out["breaker"] = breaker
    try:
        from _context_handoff import sanitize_record as _sanitize_handoff
        context_handoff = _sanitize_handoff(value.get("context_handoff"))
    except Exception:  # noqa: BLE001 - optional advisory field
        context_handoff = None
    if context_handoff is not None:
        out["context_handoff"] = context_handoff
    usage_metrics = _sanitize_usage_metrics(value.get("usage_metrics"))
    if usage_metrics is not None:
        out["usage_metrics"] = usage_metrics
    context_occupancy = _sanitize_context_occupancy(value.get("context_occupancy"))
    if context_occupancy is not None:
        out["context_occupancy"] = context_occupancy
    return out


def _sanitize_usage_metrics(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("schema") != "summon.usage-metrics/v1":
        return None
    out: dict = {"schema": "summon.usage-metrics/v1"}
    for key in ("work_tokens", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens"):
        item = value.get(key)
        if item is None or (isinstance(item, int) and not isinstance(item, bool)
                            and 0 <= item <= 2 ** 63 - 1):
            out[key] = item
        else:
            return None
    semantics = value.get("input_semantics")
    if isinstance(semantics, str) and semantics in {"includes_cache", "excludes_cache", "unknown"}:
        out["input_semantics"] = semantics
    scope = value.get("observation_scope")
    if scope is None or (isinstance(scope, str) and scope in {
        "attempt_total",
        "last_step_snapshot",
        "unestablished",
        "last_turn",
        "accumulated",
        "unknown",
        "single_request",
    }):
        out["observation_scope"] = scope
    evidence = value.get("evidence")
    if isinstance(evidence, str) and evidence in {
        "absent",
        "incomplete",
        "derived",
        "inconsistent_cache_fields",
        "derived_no_cache_fields",
        "ambiguous_cache_semantics",
        "partial",
        "reported",
    }:
        out["evidence"] = evidence
    return out


def _sanitize_context_occupancy(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("schema") != "summon.context-occupancy/v1":
        return None
    out: dict = {"schema": "summon.context-occupancy/v1"}
    for key in ("occupied_tokens", "window_tokens"):
        item = value.get(key)
        if item is None or (isinstance(item, int) and not isinstance(item, bool)
                            and 0 <= item <= 2 ** 63 - 1):
            out[key] = item
        else:
            return None
    ratio = value.get("ratio")
    if ratio is None or (isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
                         and not (isinstance(ratio, int) and not (0 <= ratio <= 10))
                         and math.isfinite(float(ratio)) and 0.0 <= float(ratio) <= 10.0):
        out["ratio"] = float(ratio) if ratio is not None else None
    else:
        return None
    over_window = value.get("over_window")
    if over_window is None or isinstance(over_window, bool):
        out["over_window"] = over_window
    semantics = value.get("input_semantics")
    if isinstance(semantics, str) and semantics in {"includes_cache", "excludes_cache", "unknown"}:
        out["input_semantics"] = semantics
    evidence = value.get("evidence")
    if isinstance(evidence, str) and evidence in {
        "absent",
        "incomplete",
        "ambiguous_cache_semantics",
        "occupied_without_window",
        "reported",
        "window_only",
        "occupied_only",
    }:
        out["evidence"] = evidence
    return out


def command_auth(nonce: str, job_id: str, command: dict) -> str:
    """Authenticate one append-only v2 control command."""
    return _auth(nonce, f"summon-job-control/v2:{job_id}", command)


def _seal_path(path: str) -> str:
    return path + ".seal"


def _write_control_seal(path: str, job_id: str, nonce: str, *,
                        generation: int, digests: dict[int, str],
                        constrained: bool, breaker_stopped: bool,
                        cancel_requested: bool, breaker_level: str,
                        breaker_transitions: int) -> bool:
    """Persist an authenticated high-water mark so rollback is detected across restarts."""
    if generation < 1:
        return False
    ordered = {str(k): digests[k] for k in sorted(digests)
               if isinstance(k, int) and 1 <= k <= generation and _is_sha256(digests[k])}
    if len(ordered) != generation:
        return False
    body = {
        "kind": "control_seal_v1",
        "job_id": job_id,
        "generation": generation,
        "digests": ordered,
        "constrained": bool(constrained),
        "breaker_stopped": bool(breaker_stopped),
        "cancel_requested": bool(cancel_requested),
        "breaker_level": str(breaker_level),
        "breaker_transitions": int(breaker_transitions),
    }
    payload = dict(body)
    payload["auth"] = _auth(nonce, f"summon-job-control-seal/v1:{job_id}", body)
    try:
        _write_control_json(_seal_path(path), payload)
        return True
    except OSError:
        return False


def _load_control_seal(path: str, job_id: str, nonce: str) -> tuple[dict | None, str]:
    """Return ``(seal_dict, state)`` where state is ``_OK``, ``_MISSING``, or ``_CORRUPT``."""
    data, state = _read_control_json(_seal_path(path))
    if state != _jobs._OK:
        return None, state
    if not isinstance(data, dict):
        return None, _jobs._CORRUPT
    expected_keys = {
        "kind", "job_id", "generation", "digests", "constrained",
        "breaker_stopped", "cancel_requested", "breaker_level",
        "breaker_transitions", "auth",
    }
    if set(data) != expected_keys or data.get("kind") != "control_seal_v1":
        return None, _jobs._CORRUPT
    if data.get("job_id") != job_id or not _is_sha256(data.get("auth")):
        return None, _jobs._CORRUPT
    gen = data.get("generation")
    digests = data.get("digests")
    if (not isinstance(gen, int) or isinstance(gen, bool)
            or not (1 <= gen <= MAX_COMMANDS)
            or not isinstance(digests, dict) or len(digests) != gen):
        return None, _jobs._CORRUPT
    parsed_digests: dict[int, str] = {}
    for idx in range(1, gen + 1):
        val = digests.get(str(idx))
        if not _is_sha256(val):
            return None, _jobs._CORRUPT
        parsed_digests[idx] = val
    for bkey in ("constrained", "breaker_stopped", "cancel_requested"):
        if not isinstance(data.get(bkey), bool):
            return None, _jobs._CORRUPT
    transitions = data.get("breaker_transitions")
    if not isinstance(transitions, int) or isinstance(transitions, bool) or transitions < 0:
        return None, _jobs._CORRUPT
    if not isinstance(data.get("breaker_level"), str):
        return None, _jobs._CORRUPT
    body = {k: v for k, v in data.items() if k != "auth"}
    try:
        expected_auth = _auth(nonce, f"summon-job-control-seal/v1:{job_id}", body)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None, _jobs._CORRUPT
    if not hmac.compare_digest(data["auth"], expected_auth):
        return None, _jobs._CORRUPT
    out = dict(body)
    out["parsed_digests"] = parsed_digests
    return out, _jobs._OK


def _is_finite_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and value == value.lower()
            and all(char in "0123456789abcdef" for char in value))


def _canonical_json(value) -> bytes:
    """Canonical bytes for authenticated control data; never admits NaN/Inf."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write_control_json(path: str, value: dict) -> None:
    """Reject non-JSON numeric values and oversized payloads before the shared atomic writer runs."""
    _canonical_json(value)
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(serialized) > MAX_CONTROL_BYTES:
        raise ValueError("job control command limit reached")
    _jobs._atomic_write_json(path, value)


def _read_control_json(path: str):
    """Bounded strict JSON read for authenticated private control inputs."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt" and os.path.islink(path):
        return None, _jobs._CORRUPT
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, _jobs._MISSING
    except OSError:
        return None, _jobs._CORRUPT
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONTROL_BYTES:
            return None, _jobs._CORRUPT
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CONTROL_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONTROL_BYTES:
        return None, _jobs._CORRUPT
    try:
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _item: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, _jobs._CORRUPT
    return (value, _jobs._OK) if isinstance(value, dict) else (None, _jobs._CORRUPT)


def _valid_command_log(commands: list, generation: int) -> bool:
    """The v2 log is append-only: generations are exactly 1..N."""
    return (isinstance(generation, int) and not isinstance(generation, bool)
            and generation == len(commands)
            and all(isinstance(item, dict)
                    and isinstance(item.get("generation"), int)
                    and not isinstance(item.get("generation"), bool)
                    and item.get("generation") == index
                    for index, item in enumerate(commands, 1)))


def _valid_command_shape(command: dict, *, authenticated: bool) -> bool:
    """Validate one exact v1/v2 command shape before it is counted or applied."""
    if not isinstance(command, dict):
        return False
    action = command.get("action")
    expected = {"generation", "action", "queued_at"}
    if action == "extend":
        expected.add("duration_ms")
    elif action == "steer":
        expected.update({"message", "message_sha256"})
    elif action == "breaker":
        if not authenticated:
            return False
        expected.update({"from_level", "to_level", "signals", "mode"})
        if command.get("to_level") == "stopped":
            expected.add("allow_stop")
        if "message" in command or "message_sha256" in command:
            if command.get("to_level") != "steering":
                return False
            expected.update({"message", "message_sha256"})
    elif action != "cancel":
        return False
    if authenticated:
        expected.add("auth")
    if set(command) != expected:
        return False
    generation = command.get("generation")
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or generation < 1 or not _is_finite_number(command.get("queued_at"))):
        return False
    if action == "extend":
        duration = command.get("duration_ms")
        if (not isinstance(duration, int) or isinstance(duration, bool)
                or not 1 <= duration <= MAX_EXTENSION_MS):
            return False
    elif action == "steer":
        message = command.get("message")
        if (not isinstance(message, str) or not message.strip()
                or len(message) > MAX_STEER_CHARS):
            return False
        try:
            expected_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        except UnicodeError:
            return False
        claimed_digest = command.get("message_sha256")
        if (not _is_sha256(claimed_digest)
                or not hmac.compare_digest(claimed_digest, expected_digest)):
            return False
    elif action == "breaker":
        try:
            from _breaker import (LEVELS as _BREAKER_LEVELS,
                                  TRIGGER_SIGNALS as _TRIGGER_SIGNALS,
                                  fixed_steering_message as _fixed_steer,
                                  normalize_signals as _norm_signals)
        except Exception:
            return False
        from_level = command.get("from_level")
        to_level = command.get("to_level")
        mode = command.get("mode")
        signals = command.get("signals")
        if (from_level not in _BREAKER_LEVELS
                or to_level not in _BREAKER_LEVELS
                or from_level == "stopped"
                or from_level == to_level
                or mode not in {"shadow", "enforce"}
                or not isinstance(signals, list)
                or not (1 <= len(signals) <= 8)
                or signals != _norm_signals(signals)):
            return False
        from_idx = _BREAKER_LEVELS.index(from_level)
        to_idx = _BREAKER_LEVELS.index(to_level)
        if abs(to_idx - from_idx) != 1:
            return False
        escalating = to_idx > from_idx
        if escalating:
            if not all(s in _TRIGGER_SIGNALS for s in signals):
                return False
            if to_level == "steering" and "message" not in command:
                return False
            if to_level == "stopped" and command.get("allow_stop") is not True:
                return False
        else:
            if signals != ["clean_checkpoint"] or "message" in command:
                return False
        if "message" in command:
            message = command.get("message")
            expected_msg = _fixed_steer(signals)
            if not isinstance(message, str) or message != expected_msg:
                return False
            try:
                expected_digest = hashlib.sha256(
                    message.encode("utf-8")).hexdigest()
            except UnicodeError:
                return False
            claimed_digest = command.get("message_sha256")
            if (not _is_sha256(claimed_digest)
                    or not hmac.compare_digest(claimed_digest, expected_digest)):
                return False
    return not authenticated or _is_sha256(command.get("auth"))


def _validated_commands(data: dict, job_id: str, nonce: str, *,
                        allow_stop: bool | None = None):
    """Return ``(commands, integrity)`` or ``(None, 'untrusted')``.

    V2 is accepted only after exact schema, generation, command shape, and HMAC
    validation. V1 remains readable for the immediately preceding draft, but is
    explicitly labelled unverified because it authenticated only the whole file
    with a duplicated nonce rather than each command.
    """
    if not isinstance(data, dict) or not isinstance(nonce, str) or not nonce:
        return None, "untrusted"
    schema = data.get("schema")
    if schema == "summon.job-control/v2":
        if set(data) != {"schema", "job_id", "generation", "commands"}:
            return None, "untrusted"
        authenticated = True
        integrity = "authenticated"
    elif schema == "summon.job-control/v1":
        if set(data) != {"schema", "job_id", "nonce", "generation", "commands"}:
            return None, "untrusted"
        if data.get("nonce") != nonce:
            return None, "untrusted"
        authenticated = False
        integrity = "legacy_unverified"
    else:
        return None, "untrusted"
    commands = data.get("commands")
    generation = data.get("generation")
    if (data.get("job_id") != job_id or not isinstance(commands, list)
            or len(commands) > MAX_COMMANDS
            or not _valid_command_log(commands, generation)):
        return None, "untrusted"
    if allow_stop is None:
        try:
            from _breaker import allow_stop_from_environment
            effective_allow_stop = allow_stop_from_environment()
        except Exception:
            effective_allow_stop = False
    else:
        effective_allow_stop = bool(allow_stop)
    expected_breaker_from = "healthy"
    for command in commands:
        if not _valid_command_shape(command, authenticated=authenticated):
            return None, "untrusted"
        if command.get("action") == "breaker":
            if command.get("from_level") != expected_breaker_from:
                return None, "untrusted"
            if command.get("to_level") == "stopped" and not effective_allow_stop:
                return None, "untrusted"
            expected_breaker_from = command["to_level"]
        if authenticated:
            body = {key: value for key, value in command.items() if key != "auth"}
            try:
                expected_auth = command_auth(nonce, job_id, body)
            except (TypeError, ValueError, OverflowError, UnicodeError):
                return None, "untrusted"
            if not hmac.compare_digest(command["auth"], expected_auth):
                return None, "untrusted"
    return commands, integrity


def _invalid_summary(integrity: str) -> dict:
    return {
        "state": integrity,
        "integrity": integrity,
        "generation": None,
        "counts": {action: 0 for action in ("extend", "cancel", "steer")},
        "steering_mode": "queued_for_resume",
    }


@contextmanager
def _exclusive_control_lock(path: str):
    """Serialize read-modify-replace command appends across local processes."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
    acquired = False
    try:
        # Lock byte zero directly, including on a newly-created empty file.
        # Seeding the file before acquiring the OS lock creates a Windows race:
        # concurrent first callers can both observe length zero and one can fail
        # its write/flush with PermissionError before serialization begins.
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
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
                if time.monotonic() >= deadline:
                    raise ControlBusyError("job control is busy; retry the command")
                time.sleep(0.025)
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _record(root: str, job_id: str) -> dict:
    record, state = _jobs._read(_jobs.record_path(root, job_id))
    if state != _jobs._OK or not isinstance(record, dict):
        raise ValueError(f"job {job_id!r} has no readable launch record")
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise ValueError("job launch record has no control identity")
    result, result_state = _jobs._read(_jobs.result_path(root, job_id))
    if result_state == _jobs._CORRUPT:
        raise ValueError("job result is malformed; control refused")
    if isinstance(result, dict):
        raise ValueError(
            "job is already terminal; use 'jobs resume JOB_ID --message TEXT' "
            "to continue it as a governed new attempt")
    return record


def queue_command(root: str, job_id: str, action: str, *,
                  duration_ms: int | None = None,
                  message: str | None = None) -> dict:
    """Append one authenticated operator command atomically."""
    if action not in {"extend", "cancel", "steer"}:
        raise ValueError("job control action must be extend, cancel, or steer")
    if action == "extend":
        if (not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
                or not 1 <= duration_ms <= MAX_EXTENSION_MS):
            raise ValueError("extension must be a bounded positive duration")
    elif duration_ms is not None:
        raise ValueError("duration is valid only for extend")
    if action == "steer":
        if (not isinstance(message, str) or not message.strip()
                or len(message) > MAX_STEER_CHARS):
            raise ValueError("steering message must be non-empty and bounded")
    elif message is not None:
        raise ValueError("message is valid only for steer")

    path = control_path(root, job_id)
    with _exclusive_control_lock(path):
        record = _record(root, job_id)
        existing, state = _jobs._read(path)
        if state == _jobs._CORRUPT:
            raise ValueError("job control file is malformed")
        if existing is None:
            existing = {"schema": "summon.job-control/v2", "job_id": job_id,
                        "generation": 0, "commands": []}
        elif existing.get("schema") == "summon.job-control/v1":
            # Never rewrite a live legacy log in place. Older frozen readers
            # hash the complete command, so adding v2 authentication to an
            # already-observed command changes its replay identity and can make
            # the running job distrust all later control. A fresh attempt gets
            # a v2 log; this legacy job must finish under its original contract.
            commands, integrity = _validated_commands(
                existing, job_id, record["nonce"])
            if commands is None or integrity != "legacy_unverified":
                raise ValueError("job control identity mismatch")
            raise ValueError(
                "legacy job control cannot be upgraded while its job is live; "
                "wait for it to finish or start a fresh attempt")
        commands, integrity = _validated_commands(
            existing, job_id, record["nonce"])
        if commands is None or integrity != "authenticated":
            raise ValueError("job control identity mismatch")
        if len(commands) >= MAX_COMMANDS:
            raise ValueError("job control command limit reached")
        generation = existing.get("generation", 0) + 1
        queued_at = time.time()
        if not _is_finite_number(queued_at):
            raise ValueError("system clock produced a non-finite control timestamp")
        command = {"generation": generation, "action": action,
                   "queued_at": queued_at}
        if action == "extend":
            command["duration_ms"] = duration_ms
        if action == "steer":
            command["message"] = message
            try:
                command["message_sha256"] = hashlib.sha256(
                    message.encode("utf-8")).hexdigest()
            except UnicodeError as exc:
                raise ValueError("steering message must be valid UTF-8 text") from exc
        command["auth"] = command_auth(record["nonce"], job_id, command)
        commands.append(command)
        existing["generation"] = generation
        _write_control_json(path, existing)
    return {"status": "queued", "job_id": job_id, "action": action,
            "generation": generation,
            "steering_mode": ("queued_for_resume" if action == "steer" else None),
            "provider_contacted": False}


def _append_breaker_command(path: str, job_id: str, nonce: str,
                            decision: dict, *, wall_clock=None,
                            _rollback_on_breaker_limit: bool = True) -> dict | None:
    """Append one authenticated breaker transition to a v2 control log."""
    if not isinstance(decision, dict) or not decision.get("transitioned"):
        return None
    from _breaker import LEVELS as _BREAKER_LEVELS, fixed_steering_message, normalize_signals
    try:
        from_level = decision.get("from_level")
        to_level = decision.get("to_level")
        mode = decision.get("mode")
        signals = normalize_signals(decision.get("signals"))
        if (from_level not in _BREAKER_LEVELS or to_level not in _BREAKER_LEVELS
                or from_level == "stopped"
                or from_level == to_level or mode not in {"shadow", "enforce"}
                or not signals):
            raise ValueError("breaker transition decision is malformed")
        from_idx = _BREAKER_LEVELS.index(from_level)
        to_idx = _BREAKER_LEVELS.index(to_level)
        if abs(to_idx - from_idx) != 1:
            raise ValueError("breaker transition must be single-step adjacent")
        escalating = to_idx > from_idx
        allow_stop = bool(decision.get("allow_stop"))
        if to_level == "stopped" and not allow_stop:
            raise ValueError("breaker stop transition requires allow_stop=True")
        now_fn = wall_clock or time.time
        with _exclusive_control_lock(path):
            existing, state = _jobs._read(path)
            if state == _jobs._CORRUPT:
                raise ValueError("job control file is malformed")
            if existing is None:
                existing = {"schema": "summon.job-control/v2", "job_id": job_id,
                            "generation": 0, "commands": []}
            elif existing.get("schema") == "summon.job-control/v1":
                raise ValueError(
                    "legacy job control cannot be upgraded while its job is live")
            commands, integrity = _validated_commands(
                existing, job_id, nonce, allow_stop=allow_stop or None)
            if commands is None or integrity != "authenticated":
                raise ValueError("job control identity mismatch")
            if len(commands) >= MAX_COMMANDS:
                raise ValueError("job control command limit reached")
            breaker_count = sum(
                1 for item in commands
                if isinstance(item, dict) and item.get("action") == "breaker"
            )
            if breaker_count >= MAX_BREAKER_COMMANDS:
                raise ValueError("job control breaker command limit reached")
            generation = existing.get("generation", 0) + 1
            queued_at = now_fn()
            if not _is_finite_number(queued_at):
                raise ValueError("system clock produced a non-finite control timestamp")
            command: dict = {
                "generation": generation,
                "action": "breaker",
                "queued_at": queued_at,
                "from_level": from_level,
                "to_level": to_level,
                "signals": signals[:8],
                "mode": mode,
            }
            if to_level == "stopped":
                command["allow_stop"] = True
            if to_level == "steering" and escalating:
                msg = fixed_steering_message(signals[:8])
                command["message"] = msg
                command["message_sha256"] = hashlib.sha256(
                    msg.encode("utf-8")).hexdigest()
            command["auth"] = command_auth(nonce, job_id, command)
            if not _valid_command_shape(command, authenticated=True):
                raise ValueError("breaker transition command failed validation")
            commands.append(command)
            existing["generation"] = generation
            checked, checked_integrity = _validated_commands(
                existing, job_id, nonce, allow_stop=allow_stop or None)
            if checked is None or checked_integrity != "authenticated":
                raise ValueError("breaker transition command conflicts with control log state")
            _write_control_json(path, existing)
    except Exception as exc:
        if (
            not _rollback_on_breaker_limit
            and isinstance(exc, ValueError)
            and str(exc) == "job control breaker command limit reached"
        ):
            raise
        rollback_fn = getattr(decision, "_rollback", None)
        if callable(rollback_fn):
            rollback_fn()
        raise
    return {
        "status": "queued",
        "job_id": job_id,
        "action": "breaker",
        "generation": generation,
        "from_level": from_level,
        "to_level": to_level,
        "mode": mode,
        "provider_contacted": False,
    }


def queue_breaker_transition(root: str, job_id: str, decision: dict, *,
                             wall_clock=None) -> dict | None:
    """Append one authenticated breaker transition for a live job in ``root``."""
    if not isinstance(decision, dict) or not decision.get("transitioned"):
        return None
    record = _record(root, job_id)
    return _append_breaker_command(
        control_path(root, job_id), job_id, record["nonce"], decision,
        wall_clock=wall_clock)


def _append_handoff_steer_command(path: str, job_id: str, nonce: str, *,
                                  wall_clock=None) -> dict | None:
    """Append the deterministic context-handoff steering directive at most once."""
    from _context_handoff import HANDOFF_STEERING_PROMPT
    now_fn = wall_clock or time.time
    with _exclusive_control_lock(path):
        existing, state = _jobs._read(path)
        if state == _jobs._CORRUPT:
            raise ValueError("job control file is malformed")
        if existing is None:
            existing = {"schema": "summon.job-control/v2", "job_id": job_id,
                        "generation": 0, "commands": []}
        elif existing.get("schema") == "summon.job-control/v1":
            raise ValueError(
                "legacy job control cannot be upgraded while its job is live")
        commands, integrity = _validated_commands(existing, job_id, nonce)
        if commands is None or integrity != "authenticated":
            raise ValueError("job control identity mismatch")
        for prior in commands:
            if (isinstance(prior, dict) and prior.get("action") == "steer"
                    and prior.get("message") == HANDOFF_STEERING_PROMPT):
                return None
        if len(commands) >= MAX_COMMANDS:
            raise ValueError("job control command limit reached")
        generation = existing.get("generation", 0) + 1
        queued_at = now_fn()
        if not _is_finite_number(queued_at):
            raise ValueError("system clock produced a non-finite control timestamp")
        command: dict = {
            "generation": generation,
            "action": "steer",
            "queued_at": queued_at,
            "message": HANDOFF_STEERING_PROMPT,
            "message_sha256": hashlib.sha256(
                HANDOFF_STEERING_PROMPT.encode("utf-8")).hexdigest(),
        }
        command["auth"] = command_auth(nonce, job_id, command)
        if not _valid_command_shape(command, authenticated=True):
            raise ValueError("context handoff steering command failed validation")
        commands.append(command)
        existing["generation"] = generation
        _write_control_json(path, existing)
    return {
        "status": "queued",
        "job_id": job_id,
        "action": "steer",
        "generation": generation,
        "steering_mode": "queued_for_resume",
        "provider_contacted": False,
    }


def control_summary(root: str, job_id: str) -> dict | None:
    data, state = _jobs._read(control_path(root, job_id))
    if state == _jobs._CORRUPT:
        return _invalid_summary("corrupt")
    if not isinstance(data, dict):
        return None
    record, record_state = _jobs._read(_jobs.record_path(root, job_id))
    nonce = record.get("nonce") if isinstance(record, dict) else None
    if record_state != _jobs._OK or not isinstance(nonce, str) or not nonce:
        return _invalid_summary("untrusted")
    try:
        commands, integrity = _validated_commands(data, job_id, nonce)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return _invalid_summary("untrusted")
    if commands is None:
        return _invalid_summary("untrusted")
    if integrity != "authenticated":
        # V1 remains readable by an in-flight legacy child, but it is never
        # rewritten in place and status must not promote nonce-bound
        # observations into an authenticated queued-command claim.
        return _invalid_summary(integrity)
    counts = {action: sum(item.get("action") == action for item in commands)
              for action in ("extend", "cancel", "steer")}
    summary = {
        "state": "queued" if any(counts.values()) else "empty",
        "integrity": integrity,
        "generation": data.get("generation"),
        "counts": counts,
        "steering_mode": "queued_for_resume",
    }
    breaker_cmds = [item for item in commands if item.get("action") == "breaker"]
    if breaker_cmds:
        last_b = breaker_cmds[-1]
        summary["breaker"] = {
            "level": last_b["to_level"],
            "from_level": last_b["from_level"],
            "to_level": last_b["to_level"],
            "mode": last_b["mode"],
            "signals": list(last_b["signals"]),
            "transitions": len(breaker_cmds),
            "last_generation": last_b["generation"],
        }
    return summary


def authenticated_steering_commands(root: str, job_id: str) -> dict:
    """Return a private, authenticated snapshot of the complete control log.

    This is an internal continuation input, never a public status projection.
    The control lock is held while reading so a concurrent append is either
    wholly before or wholly after the snapshot.  Callers that also own a resume
    claim ledger must acquire the ledger lock first; no control path acquires a
    resume-ledger lock, which keeps the lock order acyclic.
    """
    record, record_state = _jobs._read(_jobs.record_path(root, job_id))
    nonce = record.get("nonce") if isinstance(record, dict) else None
    if record_state != _jobs._OK or not isinstance(nonce, str) or not nonce:
        raise ValueError("job launch record has no authenticated control identity")
    path = control_path(root, job_id)
    with _exclusive_control_lock(path):
        data, state = _read_control_json(path)
        if state == _jobs._MISSING:
            return {"generation": 0,
                    "control_sha256": hashlib.sha256(b"").hexdigest(),
                    "steering": []}
        if state == _jobs._CORRUPT or not isinstance(data, dict):
            raise ValueError("job control file is malformed")
        commands, integrity = _validated_commands(data, job_id, nonce)
        if commands is None or integrity != "authenticated":
            raise ValueError("job control identity mismatch")
        try:
            control_sha = hashlib.sha256(_canonical_json(data)).hexdigest()
        except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise ValueError("job control file is malformed") from exc
        return {"generation": data["generation"],
                "control_sha256": control_sha,
                "steering": [
            {
                "generation": command["generation"],
                "message": command["message"],
                "message_sha256": command["message_sha256"],
            }
            for command in commands
            if command.get("action") == "steer"
            or (command.get("action") == "breaker"
                and command.get("mode") == "enforce"
                and command.get("to_level") == "steering"
                and isinstance(command.get("message"), str))
        ]}


def environment_job_budget(timeout_ms: int, *, wall_clock=None) -> dict:
    """Return the immutable background-job budget without touching providers.

    The job-origin wall clock is shared by initial, retry, and repair attempts.
    A missing/invalid origin remains non-expired for legacy foreground callers;
    the child runtime will then anchor itself when it starts.
    """
    now_fn = wall_clock or time.time
    try:
        maximum = int(os.environ.get("SUMMON_MAX_RUNTIME_MS", timeout_ms))
    except ValueError:
        maximum = timeout_ms
    maximum = max(timeout_ms, min(MAX_EXTENSION_MS, maximum))
    try:
        job_started_at = float(os.environ.get("SUMMON_JOB_STARTED_AT", ""))
    except ValueError:
        job_started_at = None
    if (not isinstance(job_started_at, (int, float))
            or not math.isfinite(job_started_at)):
        job_started_at = None
    deadline_at = (job_started_at + maximum / 1000
                   if job_started_at is not None else None)
    remaining_ms = (max(0, int((deadline_at - now_fn()) * 1000))
                    if deadline_at is not None else None)
    return {
        "job_started_at": job_started_at,
        "max_runtime_ms": maximum,
        "deadline_at": deadline_at,
        "remaining_ms": remaining_ms,
        "expired": deadline_at is not None and remaining_ms == 0,
    }


class RuntimeControl:
    """Child-side reader for durable job commands and liveness publication."""

    def __init__(self, *, path: str, heartbeat: str, job_id: str, nonce: str,
                 checkpoint_ms: int, max_runtime_ms: int,
                 attempt_id: str | None = None, attempt_kind: str = "initial",
                 attempt_ordinal: int = 1, job_started_at: float | None = None,
                 clock=time.monotonic, wall_clock=time.time):
        self.path = path
        self.heartbeat = heartbeat
        self.job_id = job_id
        self.nonce = nonce
        self.checkpoint_ms = checkpoint_ms
        self.max_runtime_ms = max_runtime_ms
        self.clock = clock
        self.wall_clock = wall_clock
        self.job_started_at = (float(job_started_at)
                               if isinstance(job_started_at, (int, float))
                               else wall_clock())
        elapsed = max(0.0, wall_clock() - self.job_started_at)
        admitted = clock()
        self.started = admitted - elapsed
        self.hard_deadline = self.started + max_runtime_ms / 1000
        # Each admitted attempt gets an observation interval, while the job's
        # original hard budget (and authenticated extension replay) still binds.
        self.deadline = min(admitted + checkpoint_ms / 1000, self.hard_deadline)
        self.attempt_id = attempt_id or job_id
        self.attempt_kind = attempt_kind
        self.attempt_ordinal = attempt_ordinal
        self.generation = 0
        self._command_digests: dict[int, str] = {}
        self.cancel_requested = False
        self.control_untrusted = False
        self.attention_required = False
        self.auto_extensions = 0
        self.operator_extensions = 0
        self.operator_extension_ms = 0
        self.extension_ms = 0
        self.steers: list[dict] = []
        self._last_publish = 0.0
        self._lock = threading.RLock()
        # Latched for the attempt: a later heartbeat cannot restore a missed
        # observation. This is diagnostics, never command/launch authority.
        self.heartbeat_unavailable = False
        self._last_refresh = float("-inf")
        # Latest advisory progress judgement (Phase 2); published, never authority.
        self.progress: dict | None = None
        # Graduated circuit breaker state (Phase 3).
        self.breaker: dict | None = None
        self.breaker_level: str = "healthy"
        self.breaker_transitions: int = 0
        self.constrained: bool = False
        self.breaker_stopped: bool = False
        # Context-aware handoff state (Phase 5).
        self.context_handoff: dict | None = None
        self.handoff_triggered: bool = False
        # Live advisory usage/context snapshots (Phase 7).
        self.usage_metrics: dict | None = None
        self.context_occupancy: dict | None = None
        self._sealed_generation: int = 0
        self._sealed_breaker_transitions: int = 0
        self._load_prior_high_water()

    def _salvage_authenticated_control_state(self) -> None:
        """Preserve enforced containment/cancellation from an authenticated control log even when seal/heartbeat is corrupt."""
        data, state = _jobs._read(self.path)
        if state != _jobs._OK or not isinstance(data, dict):
            return
        try:
            commands, integrity = _validated_commands(data, self.job_id, self.nonce)
        except (TypeError, ValueError, OverflowError, UnicodeError):
            return
        if commands is None or integrity != "authenticated":
            return
        declared_gen = data.get("generation", 0)
        if isinstance(declared_gen, int) and not isinstance(declared_gen, bool):
            self.generation = max(self.generation, declared_gen)
        breaker_count = 0
        log_constrained = False
        log_stopped = False
        log_level = "healthy"
        for command in commands:
            action = command.get("action")
            if action == "cancel":
                self.cancel_requested = True
            elif action == "steer" and isinstance(command.get("message"), str):
                try:
                    from _context_handoff import HANDOFF_STEERING_PROMPT
                    if command["message"] == HANDOFF_STEERING_PROMPT:
                        self.handoff_triggered = True
                except Exception:  # noqa: BLE001
                    pass
            elif action == "breaker":
                breaker_count += 1
                to_level = command.get("to_level")
                mode = command.get("mode")
                if isinstance(to_level, str):
                    log_level = to_level
                    if mode == "enforce":
                        if to_level == "constrained":
                            log_constrained = True
                        elif to_level == "stopped":
                            log_stopped = True
                        elif to_level == "healthy":
                            log_constrained = False
        if log_constrained:
            self.constrained = True
        if log_stopped:
            self.breaker_stopped = True
        from _breaker import LEVELS as _BREAKER_LEVELS
        if (isinstance(declared_gen, int) and not isinstance(declared_gen, bool)
                and declared_gen >= self._sealed_generation
                and log_level in _BREAKER_LEVELS):
            self.breaker_level = log_level
        elif (log_level in _BREAKER_LEVELS and self.breaker_level in _BREAKER_LEVELS
              and _BREAKER_LEVELS.index(log_level) > _BREAKER_LEVELS.index(self.breaker_level)):
            self.breaker_level = log_level
        self.breaker_transitions = max(
            self.breaker_transitions, self._sealed_breaker_transitions, breaker_count)

    def _load_prior_high_water(self) -> None:
        """Restore committed high-water marks so control rollback across restarts fails closed."""
        seal, seal_state = _load_control_seal(self.path, self.job_id, self.nonce)
        if seal_state == _jobs._CORRUPT:
            self.control_untrusted = True
        elif seal_state == _jobs._OK and isinstance(seal, dict):
            self._sealed_generation = int(seal["generation"])
            self._command_digests.update(seal["parsed_digests"])
            if seal.get("constrained"):
                self.constrained = True
            if seal.get("breaker_stopped"):
                self.breaker_stopped = True
            if seal.get("cancel_requested"):
                self.cancel_requested = True
            if isinstance(seal.get("breaker_level"), str):
                self.breaker_level = seal["breaker_level"]
            if isinstance(seal.get("breaker_transitions"), int):
                self.breaker_transitions = seal["breaker_transitions"]
                self._sealed_breaker_transitions = seal["breaker_transitions"]
        # Cross-check authenticated prior heartbeat if available.
        hb, hb_state = _read_control_json(self.heartbeat)
        if hb_state == _jobs._CORRUPT:
            self.control_untrusted = True
        elif hb_state == _jobs._OK and isinstance(hb, dict):
            if hb.get("schema") == "summon.job-heartbeat/v2":
                hb_auth = hb.get("auth")
                hb_body = {k: v for k, v in hb.items() if k != "auth"}
                try:
                    expected_hb_auth = heartbeat_auth(self.nonce, hb_body)
                except (TypeError, ValueError, OverflowError, UnicodeError):
                    expected_hb_auth = None
                hb_verified = False
                if (_is_sha256(hb_auth) and expected_hb_auth is not None
                        and hb.get("job_id") == self.job_id):
                    try:
                        hb_verified = hmac.compare_digest(hb_auth, expected_hb_auth)
                    except (TypeError, ValueError):
                        hb_verified = False
                if hb_verified:
                    hb_gen = hb.get("control_generation")
                    if isinstance(hb_gen, int) and not isinstance(hb_gen, bool) and hb_gen > self._sealed_generation:
                        self._sealed_generation = hb_gen
                    if hb.get("cancel_requested") is True:
                        self.cancel_requested = True
                    if hb.get("control_untrusted") is True:
                        self.control_untrusted = True
                    hb_breaker = hb.get("breaker")
                    if isinstance(hb_breaker, dict):
                        hb_trans = hb_breaker.get("transitions")
                        if (isinstance(hb_trans, int) and not isinstance(hb_trans, bool)
                                and hb_trans > self.breaker_transitions):
                            self.breaker_transitions = hb_trans
                            self._sealed_breaker_transitions = hb_trans
                        if hb_breaker.get("enforced") is True:
                            b_level = hb_breaker.get("level")
                            if b_level == "constrained":
                                self.constrained = True
                            elif b_level == "stopped":
                                self.breaker_stopped = True
                            from _breaker import LEVELS as _BREAKER_LEVELS
                            if (isinstance(b_level, str) and b_level in _BREAKER_LEVELS
                                    and self.breaker_level in _BREAKER_LEVELS
                                    and _BREAKER_LEVELS.index(b_level) > _BREAKER_LEVELS.index(self.breaker_level)):
                                self.breaker_level = b_level
                    hb_handoff = hb.get("context_handoff")
                    if isinstance(hb_handoff, dict) and hb_handoff.get("triggered") is True:
                        self.handoff_triggered = True
                else:
                    self.control_untrusted = True
        if self.control_untrusted:
            self._salvage_authenticated_control_state()

    def checkpoint_due(self) -> bool:
        """Whether ``checkpoint`` would act now (lets callers measure lazily)."""
        now = self.clock()
        return self.deadline <= now < self.hard_deadline

    def ms_until_checkpoint(self) -> int:
        """Milliseconds until the soft checkpoint deadline (0 when due/past)."""
        return max(0, int((self.deadline - self.clock()) * 1000))

    @classmethod
    def from_environment(cls, timeout_ms: int, *, attempt_id: str | None = None,
                         attempt_kind: str = "initial",
                         attempt_ordinal: int = 1):
        if os.environ.get("SUMMON_ADAPTIVE_TIMEOUT") != "1":
            return None
        path = os.environ.get("SUMMON_JOB_CONTROL_FILE")
        heartbeat = os.environ.get("SUMMON_JOB_HEARTBEAT_FILE")
        job_id = os.environ.get("SUMMON_JOB_ID")
        nonce = os.environ.get("SUMMON_JOB_NONCE")
        if not all(isinstance(item, str) and item for item in
                   (path, heartbeat, job_id, nonce)):
            return None
        budget = environment_job_budget(timeout_ms)
        maximum = int(budget["max_runtime_ms"])
        job_started_at = budget["job_started_at"]
        return cls(path=path, heartbeat=heartbeat, job_id=job_id, nonce=nonce,
                   checkpoint_ms=timeout_ms, max_runtime_ms=maximum,
                   attempt_id=attempt_id, attempt_kind=attempt_kind,
                   attempt_ordinal=attempt_ordinal,
                   job_started_at=job_started_at)

    def refresh(self, *, force: bool = False) -> int:
        """Read new commands; return newly authorized extension milliseconds."""
        with self._lock:
            return self._refresh_locked(force=force)

    def _refresh_locked(self, *, force: bool = False) -> int:
        if self.control_untrusted:
            return 0
        now = self.clock()
        if not force and now - self._last_refresh < 0.25:
            return 0
        self._last_refresh = now
        data, state = _jobs._read(self.path)
        if state == _jobs._CORRUPT:
            self.control_untrusted = True
            return 0
        min_expected_generation = max(self.generation, self._sealed_generation)
        if not isinstance(data, dict):
            if min_expected_generation > 0:
                self.control_untrusted = True
            return 0
        try:
            commands, _integrity = _validated_commands(data, self.job_id, self.nonce)
        except (TypeError, ValueError, OverflowError, UnicodeError):
            self.control_untrusted = True
            return 0
        if commands is None:
            self.control_untrusted = True
            return 0
        declared_generation = data["generation"]
        if declared_generation < min_expected_generation:
            # A deleted/recreated or truncated control file must not make a
            # previously observed cancel/extend/steer disappear.
            self.control_untrusted = True
            return 0
        pending: list[tuple[dict, str]] = []
        for command in commands:
            generation = command["generation"]
            try:
                command_digest = hashlib.sha256(_canonical_json(command)).hexdigest()
            except (TypeError, ValueError, OverflowError, UnicodeError):
                self.control_untrusted = True
                return 0
            known_digest = self._command_digests.get(generation)
            if known_digest is not None and known_digest != command_digest:
                self.control_untrusted = True
                return 0
            if generation <= self.generation:
                continue
            pending.append((command, command_digest))

        # Apply nothing until the complete log has passed validation. An invalid
        # tail must not make an earlier cancel or extension take effect.
        added = 0
        for command, command_digest in pending:
            generation = command["generation"]
            action = command.get("action")
            if action == "cancel":
                self.cancel_requested = True
            elif action == "extend":
                duration = command.get("duration_ms")
                if isinstance(duration, int) and not isinstance(duration, bool) \
                        and 1 <= duration <= MAX_EXTENSION_MS:
                    # Process the authenticated append-only log in generation
                    # order. An extension is live only when it was queued by the
                    # operator before the then-current hard deadline. An earlier
                    # valid extension may therefore make a later command
                    # eligible, but a late command cannot resurrect an expired
                    # job.
                    hard_deadline_at = (
                        self.job_started_at + self.max_runtime_ms / 1000)
                    if command["queued_at"] <= hard_deadline_at:
                        remaining_capacity = max(
                            0, MAX_EXTENSION_MS - self.max_runtime_ms)
                        applied = min(duration, remaining_capacity)
                        if applied:
                            added += applied
                            self.max_runtime_ms += applied
                            self.hard_deadline += applied / 1000
                            self.deadline += applied / 1000
                            self.operator_extensions += 1
                            self.operator_extension_ms += applied
            elif action == "steer" and isinstance(command.get("message"), str):
                message = command["message"]
                if 0 < len(message) <= MAX_STEER_CHARS:
                    self.steers.append({"generation": generation,
                                        "message": message,
                                        "message_sha256": command.get("message_sha256")})
                    try:
                        from _context_handoff import HANDOFF_STEERING_PROMPT
                        if message == HANDOFF_STEERING_PROMPT:
                            self.handoff_triggered = True
                    except Exception:  # noqa: BLE001
                        pass
            elif action == "breaker":
                to_level = command.get("to_level")
                mode = command.get("mode")
                if isinstance(to_level, str):
                    self.breaker_level = to_level
                    if mode == "enforce":
                        if to_level == "steering" and isinstance(command.get("message"), str):
                            self.steers.append({
                                "generation": generation,
                                "message": command["message"],
                                "message_sha256": command.get("message_sha256"),
                            })
                        elif to_level == "constrained":
                            self.constrained = True
                        elif to_level == "stopped":
                            self.breaker_stopped = True
                        elif to_level == "healthy":
                            self.constrained = False
            self.generation = generation
            self._command_digests[generation] = command_digest
        total_breaker_cmds = sum(
            1 for cmd in commands
            if isinstance(cmd, dict) and cmd.get("action") == "breaker"
        )
        self.breaker_transitions = max(self._sealed_breaker_transitions, total_breaker_cmds)
        if self.generation > self._sealed_generation:
            if _write_control_seal(
                self.path, self.job_id, self.nonce,
                generation=self.generation,
                digests=self._command_digests,
                constrained=self.constrained,
                breaker_stopped=self.breaker_stopped,
                cancel_requested=self.cancel_requested,
                breaker_level=self.breaker_level,
                breaker_transitions=self.breaker_transitions,
            ):
                self._sealed_generation = self.generation
                self._sealed_breaker_transitions = self.breaker_transitions
        if added:
            self.extension_ms += added
            self.attention_required = False
            return added
        return 0

    def checkpoint(self, *, active: bool) -> int:
        """At the soft deadline, extend active work or open an attention grace."""
        with self._lock:
            now = self.clock()
            if now < self.deadline or now >= self.hard_deadline:
                return 0
            remaining = max(0, int((self.hard_deadline - self.deadline) * 1000))
            if remaining <= 0:
                return 0
            if active:
                extension = min(self.checkpoint_ms, remaining)
                self.auto_extensions += 1
                if not (
                    self.handoff_triggered
                    and isinstance(self.context_handoff, dict)
                    and self.context_handoff.get("mode") == "enforce"
                ):
                    self.attention_required = False
            else:
                if self.attention_required:
                    return 0
                extension = min(max(60_000, self.checkpoint_ms // 2), remaining)
                self.attention_required = True
            self.deadline += extension / 1000
            self.extension_ms += extension
            return extension

    def record_breaker(self, decision: dict) -> dict | None:
        """Persist a breaker transition into the authenticated control log and update state."""
        from _breaker import sanitize_record as _sanitize_breaker
        with self._lock:
            sanitized = _sanitize_breaker(decision)
            if sanitized is None:
                return None
            if decision.get("transitioned") and not self.control_untrusted:
                try:
                    queued = _append_breaker_command(
                        self.path, self.job_id, self.nonce, decision,
                        wall_clock=self.wall_clock,
                        _rollback_on_breaker_limit=(decision.get("mode") != "shadow"))
                except ValueError as exc:
                    if (str(exc) == "job control breaker command limit reached"
                            and decision.get("mode") == "shadow"):
                        queued = None
                    else:
                        rollback_fn = getattr(decision, "_rollback", None)
                        if callable(rollback_fn):
                            rollback_fn()
                        return None
                except Exception:  # noqa: BLE001 - a corrupt/legacy control log cannot be appended
                    rollback_fn = getattr(decision, "_rollback", None)
                    if callable(rollback_fn):
                        rollback_fn()
                    return None
                if queued is not None:
                    commit_fn = getattr(decision, "_commit", None)
                    if callable(commit_fn):
                        commit_fn()
                    self.breaker = sanitized
                    self._refresh_locked(force=True)
                    return queued
            commit_fn = getattr(decision, "_commit", None)
            if callable(commit_fn):
                commit_fn()
            self.breaker = sanitized
            self.breaker_level = str(sanitized.get("level") or self.breaker_level)
            if sanitized.get("enforced"):
                if self.breaker_level == "constrained":
                    self.constrained = True
                elif self.breaker_level == "stopped":
                    self.breaker_stopped = True
                elif self.breaker_level == "healthy":
                    self.constrained = False
            return None

    def record_context_handoff(self, decision: dict) -> dict | None:
        """Persist an enforced context-handoff steering directive and update state."""
        from _context_handoff import sanitize_record as _sanitize_handoff
        with self._lock:
            sanitized = _sanitize_handoff(decision)
            if sanitized is None:
                return None
            if (decision.get("newly_triggered") and decision.get("enforced")
                    and not self.control_untrusted):
                try:
                    queued = _append_handoff_steer_command(
                        self.path, self.job_id, self.nonce,
                        wall_clock=self.wall_clock)
                except Exception:  # noqa: BLE001
                    rollback_fn = getattr(decision, "_rollback", None)
                    if callable(rollback_fn):
                        rollback_fn()
                    return None
                commit_fn = getattr(decision, "_commit", None)
                if callable(commit_fn):
                    commit_fn()
                self.context_handoff = sanitized
                self.handoff_triggered = True
                self.attention_required = True
                self._refresh_locked(force=True)
                return queued
            commit_fn = getattr(decision, "_commit", None)
            if callable(commit_fn):
                commit_fn()
            self.context_handoff = sanitized
            if sanitized.get("triggered"):
                self.handoff_triggered = True
                if sanitized.get("mode") == "enforce":
                    self.attention_required = True
            if sanitized.get("enforced"):
                self.attention_required = True
            return None

    def expired(self) -> bool:
        now = self.clock()
        return now >= self.deadline or now >= self.hard_deadline

    def publish(self, liveness: dict, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_publish < 1.0:
            return
        self._last_publish = now
        payload = {
            "schema": "summon.job-heartbeat/v2", "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "attempt_kind": self.attempt_kind,
            "attempt_ordinal": self.attempt_ordinal,
            "state": ("repairing" if self.attempt_kind in {
                "schema_correction", "contract_repair"} else "running"),
            "job_started_at": self.job_started_at,
            "job_elapsed_ms": max(
                0, int((self.wall_clock() - self.job_started_at) * 1000)),
            "job_hard_deadline_at": (
                self.job_started_at + self.max_runtime_ms / 1000),
            "observed_at": time.time(), "liveness": liveness,
            "adaptive": True, "attention_required": self.attention_required,
            "auto_extensions": self.auto_extensions,
            "operator_extensions": self.operator_extensions,
            "extension_ms": self.extension_ms,
            "control_generation": self.generation,
            "control_scope": "current_attempt_replayed_from_job_origin",
            "cancel_requested": self.cancel_requested,
            "control_untrusted": self.control_untrusted,
            "steering": {"queued": len(self.steers),
                         "mode": "queued_for_resume"},
        }
        if self.progress is not None:
            payload["progress"] = self.progress
        if self.breaker is not None:
            payload["breaker"] = self.breaker
        if self.context_handoff is not None:
            payload["context_handoff"] = self.context_handoff
        if self.usage_metrics is not None:
            payload["usage_metrics"] = self.usage_metrics
        if self.context_occupancy is not None:
            payload["context_occupancy"] = self.context_occupancy
        payload["auth"] = heartbeat_auth(self.nonce, payload)
        try:
            _write_control_json(self.heartbeat, payload)
        except OSError:
            # Only the observational file write is fail-soft. Authentication,
            # durable commands and provider launch fences remain strict.
            self.heartbeat_unavailable = True
        mission_id = os.environ.get("SUMMON_MISSION_ID")
        if mission_id and self.usage_metrics is not None:
            try:
                from _mission_budget import record_heartbeat_usage
                job_dir = os.environ.get("SUMMON_JOB_DIR") or os.environ.get("SUMMON_JOBS_DIR")
                eff_job_id = os.environ.get("SUMMON_MISSION_JOB_ID") or self.job_id
                record_heartbeat_usage(
                    mission_id,
                    job_id=eff_job_id,
                    attempt_id=self.attempt_id,
                    usage_metrics=self.usage_metrics,
                    attempt_kind=self.attempt_kind,
                    job_dir=job_dir,
                )
            except Exception:
                pass

    def projection(self) -> dict:
        out = {"enabled": True, "attention_required": self.attention_required,
               "heartbeat_unavailable": self.heartbeat_unavailable,
               "auto_extensions": self.auto_extensions,
               "operator_extensions": self.operator_extensions,
               "extension_ms": self.extension_ms,
               "hard_runtime_ms": self.max_runtime_ms,
               "control_generation": self.generation,
               "cancel_requested": self.cancel_requested,
               "control_untrusted": self.control_untrusted,
               "attempt_id": self.attempt_id,
               "attempt_kind": self.attempt_kind,
               "attempt_ordinal": self.attempt_ordinal,
               "steering": {"queued": len(self.steers),
                            "mode": "queued_for_resume"}}
        if self.breaker is not None:
            out["breaker"] = self.breaker
        if self.context_handoff is not None:
            out["context_handoff"] = self.context_handoff
        return out
