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
import time
from contextlib import contextmanager

import _jobs


MAX_COMMANDS = 128
MAX_STEER_CHARS = 16_384
MAX_EXTENSION_MS = 7 * 24 * 60 * 60 * 1000
LOCK_WAIT_SECONDS = 5.0


def control_path(root: str, job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.control.json")


def heartbeat_path(root: str, job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.heartbeat.json")


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


def command_auth(nonce: str, job_id: str, command: dict) -> str:
    """Authenticate one append-only v2 control command."""
    return _auth(nonce, f"summon-job-control/v2:{job_id}", command)


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
    """Reject non-JSON numeric values before the shared atomic writer runs."""
    _canonical_json(value)
    _jobs._atomic_write_json(path, value)


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
    return not authenticated or _is_sha256(command.get("auth"))


def _validated_commands(data: dict, job_id: str, nonce: str):
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
    for command in commands:
        if not _valid_command_shape(command, authenticated=authenticated):
            return None, "untrusted"
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
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
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
                    raise ValueError("job control is busy; retry the command")
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
        raise ValueError("job is already terminal; resume it as a new attempt")
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
    return {
        "state": "queued" if commands else "empty",
        "integrity": integrity,
        "generation": data.get("generation"),
        "counts": {action: sum(item.get("action") == action for item in commands)
                   for action in ("extend", "cancel", "steer")},
        "steering_mode": "queued_for_resume",
    }


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
        self.started = clock() - elapsed
        self.deadline = self.started + checkpoint_ms / 1000
        self.hard_deadline = self.started + max_runtime_ms / 1000
        self.attempt_id = attempt_id or job_id
        self.attempt_kind = attempt_kind
        self.attempt_ordinal = attempt_ordinal
        self.generation = 0
        self._command_digests: dict[int, str] = {}
        self.cancel_requested = False
        self.control_untrusted = False
        self.attention_required = False
        self.auto_extensions = 0
        self.extension_ms = 0
        self.steers: list[dict] = []
        self._last_publish = 0.0
        self._last_refresh = float("-inf")

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
        if not isinstance(data, dict):
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
        if declared_generation < self.generation:
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
            if generation <= self.generation:
                if self._command_digests.get(generation) != command_digest:
                    self.control_untrusted = True
                    return 0
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
                    added += duration
            elif action == "steer" and isinstance(command.get("message"), str):
                message = command["message"]
                if 0 < len(message) <= MAX_STEER_CHARS:
                    self.steers.append({"generation": generation,
                                        "message": message,
                                        "message_sha256": command.get("message_sha256")})
            self.generation = generation
            self._command_digests[generation] = command_digest
        if added:
            allowed = max(0, int((self.hard_deadline - self.deadline) * 1000))
            applied = min(added, allowed)
            self.deadline += applied / 1000
            self.extension_ms += applied
            if applied:
                self.attention_required = False
            return applied
        return 0

    def checkpoint(self, *, active: bool) -> int:
        """At the soft deadline, extend active work or open an attention grace."""
        now = self.clock()
        if now < self.deadline or now >= self.hard_deadline:
            return 0
        remaining = max(0, int((self.hard_deadline - self.deadline) * 1000))
        if remaining <= 0:
            return 0
        if active:
            extension = min(self.checkpoint_ms, remaining)
            self.auto_extensions += 1
            self.attention_required = False
        else:
            if self.attention_required:
                return 0
            extension = min(max(60_000, self.checkpoint_ms // 2), remaining)
            self.attention_required = True
        self.deadline += extension / 1000
        self.extension_ms += extension
        return extension

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
            "extension_ms": self.extension_ms,
            "control_scope": "current_attempt_replayed_from_job_origin",
            "cancel_requested": self.cancel_requested,
            "control_untrusted": self.control_untrusted,
            "steering": {"queued": len(self.steers),
                         "mode": "queued_for_resume"},
        }
        payload["auth"] = heartbeat_auth(self.nonce, payload)
        _write_control_json(self.heartbeat, payload)

    def projection(self) -> dict:
        return {"enabled": True, "attention_required": self.attention_required,
                "auto_extensions": self.auto_extensions,
                "extension_ms": self.extension_ms,
                "hard_runtime_ms": self.max_runtime_ms,
                "cancel_requested": self.cancel_requested,
                "control_untrusted": self.control_untrusted,
                "attempt_id": self.attempt_id,
                "attempt_kind": self.attempt_kind,
                "attempt_ordinal": self.attempt_ordinal,
                "steering": {"queued": len(self.steers),
                             "mode": "queued_for_resume"}}
