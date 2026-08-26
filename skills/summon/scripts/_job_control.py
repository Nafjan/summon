"""Durable single-machine controls for adaptive background jobs.

Commands are private local operator state.  They are never copied into public
telemetry or release evidence.  A command queues intent; it does not claim a
provider accepted steering or that a cancellation was instantaneous.
"""

from __future__ import annotations

import hashlib
import json
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


@contextmanager
def _exclusive_control_lock(path: str):
    """Serialize read-modify-replace command appends across local processes."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
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
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ValueError("job control is busy; retry the command")
                time.sleep(0.025)
        yield
    finally:
        try:
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
            existing = {"schema": "summon.job-control/v1", "job_id": job_id,
                        "nonce": record["nonce"], "generation": 0, "commands": []}
        if (existing.get("schema") != "summon.job-control/v1"
                or existing.get("job_id") != job_id
                or existing.get("nonce") != record["nonce"]
                or not isinstance(existing.get("commands"), list)):
            raise ValueError("job control identity mismatch")
        commands = existing["commands"]
        if len(commands) >= MAX_COMMANDS:
            raise ValueError("job control command limit reached")
        generation = existing.get("generation", 0) + 1
        command = {"generation": generation, "action": action,
                   "queued_at": time.time()}
        if action == "extend":
            command["duration_ms"] = duration_ms
        if action == "steer":
            command["message"] = message
            command["message_sha256"] = hashlib.sha256(
                message.encode("utf-8")).hexdigest()
        commands.append(command)
        existing["generation"] = generation
        _jobs._atomic_write_json(path, existing)
    return {"status": "queued", "job_id": job_id, "action": action,
            "generation": generation,
            "steering_mode": ("queued_for_resume" if action == "steer" else None),
            "provider_contacted": False}


def control_summary(root: str, job_id: str) -> dict | None:
    data, state = _jobs._read(control_path(root, job_id))
    if state == _jobs._CORRUPT:
        return {"state": "corrupt"}
    if not isinstance(data, dict):
        return None
    commands = data.get("commands") if isinstance(data.get("commands"), list) else []
    return {
        "state": "queued" if commands else "empty",
        "generation": data.get("generation"),
        "counts": {action: sum(item.get("action") == action for item in commands)
                   for action in ("extend", "cancel", "steer")},
        "steering_mode": "queued_for_resume",
    }


class RuntimeControl:
    """Child-side reader for durable job commands and liveness publication."""

    def __init__(self, *, path: str, heartbeat: str, job_id: str, nonce: str,
                 checkpoint_ms: int, max_runtime_ms: int,
                 clock=time.monotonic):
        self.path = path
        self.heartbeat = heartbeat
        self.job_id = job_id
        self.nonce = nonce
        self.checkpoint_ms = checkpoint_ms
        self.max_runtime_ms = max_runtime_ms
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + checkpoint_ms / 1000
        self.hard_deadline = self.started + max_runtime_ms / 1000
        self.generation = 0
        self.cancel_requested = False
        self.attention_required = False
        self.auto_extensions = 0
        self.extension_ms = 0
        self.steers: list[dict] = []
        self._last_publish = 0.0

    @classmethod
    def from_environment(cls, timeout_ms: int):
        if os.environ.get("SUMMON_ADAPTIVE_TIMEOUT") != "1":
            return None
        path = os.environ.get("SUMMON_JOB_CONTROL_FILE")
        heartbeat = os.environ.get("SUMMON_JOB_HEARTBEAT_FILE")
        job_id = os.environ.get("SUMMON_JOB_ID")
        nonce = os.environ.get("SUMMON_JOB_NONCE")
        if not all(isinstance(item, str) and item for item in
                   (path, heartbeat, job_id, nonce)):
            return None
        try:
            maximum = int(os.environ.get("SUMMON_MAX_RUNTIME_MS", timeout_ms))
        except ValueError:
            maximum = timeout_ms
        maximum = max(timeout_ms, min(MAX_EXTENSION_MS, maximum))
        return cls(path=path, heartbeat=heartbeat, job_id=job_id, nonce=nonce,
                   checkpoint_ms=timeout_ms, max_runtime_ms=maximum)

    def refresh(self) -> int:
        """Read new commands; return newly authorized extension milliseconds."""
        data, state = _jobs._read(self.path)
        if state == _jobs._CORRUPT:
            self.cancel_requested = True
            return 0
        if not isinstance(data, dict):
            return 0
        if data.get("job_id") != self.job_id or data.get("nonce") != self.nonce:
            self.cancel_requested = True
            return 0
        added = 0
        commands = data.get("commands")
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
            self.cancel_requested = True
            return 0
        for command in commands:
            generation = command.get("generation") if isinstance(command, dict) else None
            if not isinstance(generation, int) or generation <= self.generation:
                continue
            self.generation = generation
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
            "schema": "summon.job-heartbeat/v1", "job_id": self.job_id,
            "nonce": self.nonce,
            "observed_at": time.time(), "liveness": liveness,
            "adaptive": True, "attention_required": self.attention_required,
            "auto_extensions": self.auto_extensions,
            "extension_ms": self.extension_ms,
            "cancel_requested": self.cancel_requested,
            "steering": {"queued": len(self.steers),
                         "mode": "queued_for_resume"},
        }
        _jobs._atomic_write_json(self.heartbeat, payload)

    def projection(self) -> dict:
        return {"enabled": True, "attention_required": self.attention_required,
                "auto_extensions": self.auto_extensions,
                "extension_ms": self.extension_ms,
                "hard_runtime_ms": self.max_runtime_ms,
                "cancel_requested": self.cancel_requested,
                "steering": {"queued": len(self.steers),
                             "mode": "queued_for_resume",
                             "message_sha256": [item.get("message_sha256")
                                                for item in self.steers]}}
