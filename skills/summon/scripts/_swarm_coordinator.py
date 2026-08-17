"""Durable, provider-neutral coordinator for ``summon.swarm/v1``.

The wire module deliberately stops at framing.  This module is the next
boundary: it gives a local coordinator an owner lease, a checksummed journal,
claim/attempt fencing, cancellation, and explicit uncertain-spend recovery.
It never launches a provider and never dereferences worker-supplied artifacts.

Every mutating operation acquires the run owner for the shortest possible
critical section.  A worker therefore holds a *claim* lease, not the
coordinator's filesystem lock.  A successor can serialize the next operation,
but it cannot complete, publish, or cancel a claim whose worker, attempt,
request digest, or lease generation does not match the durable record.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from _rundir import (JournalCorruptError, Owner, acquire_owner, journal_append,
                     journal_read_tagged, journal_repair, release_owner,
                     run_path, validate_run_id)
from _swarm_protocol import (PROTOCOL, SwarmProtocolError, encode_frame,
                             frame_sha256, make_frame, parse_frame)


MAX_TASKS = 1024
MAX_ATTEMPTS = 32
MAX_LEASE_MS = 24 * 60 * 60 * 1000
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_REASON_CHARS = 512
MAX_STATUS_TASKS = 1024
MAX_STATUS_MESSAGES = 4096
MAX_STATUS_ARTIFACTS = 4096
MAX_JOURNAL_BYTES = 8 * 1024 * 1024

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SwarmCoordinatorError(RuntimeError):
    """Base class for coordinator admission/state errors."""


class SwarmNotFoundError(SwarmCoordinatorError):
    pass


class SwarmCorruptError(SwarmCoordinatorError):
    pass


class SwarmConflictError(SwarmCoordinatorError):
    pass


class SwarmIndeterminateError(SwarmConflictError):
    """A previous physical attempt may have spent and needs human policy."""


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _positive_int(value: Any, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SwarmProtocolError(f"invalid {label}")
    if maximum is not None and value > maximum:
        raise SwarmProtocolError(f"invalid {label}")
    return value


def _bounded_reason(value: Any, label: str = "reason") -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value or len(value) > MAX_REASON_CHARS:
        raise SwarmProtocolError(f"invalid {label}")
    if any(c in value for c in "\x00\r\n"):
        raise SwarmProtocolError(f"invalid {label}")
    # The coordinator's public journal is not a raw provider-output channel.
    # Keep useful bounded prose while applying the same conservative path and
    # credential boundary as the conversation atlas. Secret-bearing output
    # belongs in a provider's private receipt.
    text = re.sub(r"(?i)\b[A-Z]:[\\/][^\r\n,;]*", "[path]", value)
    text = re.sub(r"(?<![\w])/(?:[^\r\n,;]*)", "[path]", text)
    text = re.sub(r"(?<![\w])(?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.-]+", "[path]", text)
    text = re.sub(r"(?i)(?:token|password|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
                  "[redacted]", text)
    text = re.sub(r"(?i)\b(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b",
                  "[redacted]", text)
    text = re.sub(r"(?i)(?:\bauthorization\s*:\s*)?\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}",
                  "[redacted]", text)
    text = re.sub(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+", "[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                  "[redacted]", text)
    text = re.sub(r"\b(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)[A-Z0-9_-]{3,}\b",
                  "[redacted]", text, flags=re.IGNORECASE)
    return text


def _now_ms(clock) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SwarmCoordinatorError("coordinator clock returned a non-number")
    if not (value == value and abs(value) < 10**13):
        raise SwarmCoordinatorError("coordinator clock is invalid")
    result = int(value * 1000)
    if result < 1:
        raise SwarmCoordinatorError("coordinator clock is before the Unix epoch")
    return result


def _message_id(prefix: str = "msg") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _materialize_tasks(tasks: Iterable[Mapping[str, Any]], *, max_attempts: int) -> list[dict[str, Any]]:
    if isinstance(tasks, (str, bytes, Mapping)):
        raise SwarmProtocolError("tasks must be an iterable of task objects")
    materialized: list[dict[str, Any]] = []
    seen: set[str] = set()
    iterator = iter(tasks)
    for _ in range(MAX_TASKS + 1):
        try:
            item = next(iterator)
        except StopIteration:
            break
        if not isinstance(item, Mapping):
            raise SwarmProtocolError("task must be an object")
        if set(item) - {"task_id", "request_sha256"}:
            raise SwarmProtocolError("unknown task field")
        task_id = _id(item.get("task_id"), "task id")
        request_sha = _digest(item.get("request_sha256"), "request digest")
        if task_id in seen:
            raise SwarmProtocolError("duplicate task id")
        seen.add(task_id)
        materialized.append({"task_id": task_id, "request_sha256": request_sha})
    else:
        raise SwarmProtocolError("too many tasks")
    if not materialized:
        raise SwarmProtocolError("swarm must contain at least one task")
    if max_attempts < 1 or max_attempts > MAX_ATTEMPTS:
        raise SwarmProtocolError("invalid max attempts")
    return materialized


class SwarmCoordinator:
    """A local durable coordinator for one swarm run.

    ``runs_root`` is a private operator-owned directory.  The coordinator
    stores digests, bounded previews, and lifecycle facts only; prompts,
    provider output, credentials, and artifact bytes are never written here.
    """

    def __init__(self, runs_root: str | os.PathLike[str], run_id: str, *,
                 lease_sec: float = 30.0, clock=None):
        self.runs_root = str(runs_root)
        self.run_id = validate_run_id(run_id)
        self.run_dir = run_path(self.runs_root, self.run_id)
        if not isinstance(lease_sec, (int, float)) or isinstance(lease_sec, bool):
            raise ValueError("lease_sec must be numeric")
        if not (0.5 <= float(lease_sec) <= 3600.0):
            raise ValueError("lease_sec is outside the coordinator bounds")
        self.lease_sec = float(lease_sec)
        self.clock = clock or time.time

    # --- construction and durable IO -------------------------------------

    @classmethod
    def create(cls, runs_root: str | os.PathLike[str], run_id: str, *,
               project_root_sha256: str, roster_definition_sha256: str,
               tasks: Iterable[Mapping[str, Any]], max_attempts: int = 1,
               lease_sec: float = 30.0, clock=None) -> "SwarmCoordinator":
        coordinator = cls(runs_root, run_id, lease_sec=lease_sec, clock=clock)
        _digest(project_root_sha256, "project root digest")
        _digest(roster_definition_sha256, "roster definition digest")
        task_list = _materialize_tasks(tasks, max_attempts=max_attempts)
        owner = coordinator._acquire()
        try:
            records, torn = coordinator._read_records()
            if records or torn:
                raise SwarmConflictError("swarm run already exists")
            # Refuse foreign files in a run directory.  Owner/generation files
            # may be left by a crashed, empty acquisition and are harmless.
            try:
                names = set(os.listdir(coordinator.run_dir))
            except OSError as exc:
                raise SwarmCoordinatorError("cannot inspect swarm run directory") from exc
            allowed = {"owner.lock", "generation.txt"}
            allowed.update(name for name in names if name.startswith("lease-") and name.endswith(".json"))
            allowed.update(name for name in names if name.startswith("journal-g") and name.endswith(".jsonl"))
            if names - allowed:
                raise SwarmConflictError("swarm run directory contains foreign files")
            coordinator._append(owner, {
                "event": "swarm_prepared",
                "schema": 1,
                "run_id": coordinator.run_id,
                "project_root_sha256": project_root_sha256,
                "roster_definition_sha256": roster_definition_sha256,
                "max_attempts": max_attempts,
                "tasks": task_list,
                "message_id": _message_id("prepare"),
            })
        finally:
            release_owner(owner)
        return coordinator

    def _acquire(self) -> Owner:
        try:
            self._fence_run_files()
            owner = acquire_owner(self.run_dir, self.lease_sec)
            self._fence_run_files()
            journal_repair(self.run_dir, owner)
            return owner
        except (OSError, ValueError) as exc:
            raise SwarmCoordinatorError("cannot acquire swarm coordinator owner") from exc

    def _fence_run_files(self) -> None:
        """Reject link-like coordinator files before any open/replace.

        ``run_path`` fences the run directory itself, but a hostile or
        partially-recovered run can still contain a symlinked journal segment,
        lease, lock, or generation file.  Those names are opened by the
        shared run-dir helper, so fail closed before it gets a chance to
        follow one outside the coordinator root.
        """
        root = Path(self.run_dir)
        if not root.exists():
            return
        try:
            children = list(root.iterdir())
        except OSError as exc:
            raise SwarmCoordinatorError("cannot inspect swarm run directory") from exc
        for child in children:
            name = child.name
            managed = (
                name in {"owner.lock", "generation.txt"}
                or (name.startswith("lease-") and name.endswith(".json"))
                or (name.startswith("journal-g") and name.endswith(".jsonl"))
            )
            if not managed:
                continue
            try:
                attrs = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
                junction = bool(getattr(child, "is_junction", lambda: False)())
                if child.is_symlink() or junction or bool(attrs & 0x400):
                    raise SwarmCoordinatorError(f"swarm managed file may not be a link: {name}")
                if not child.is_file():
                    raise SwarmCoordinatorError(f"swarm managed path is not a file: {name}")
            except FileNotFoundError:
                # A concurrent cleanup/recovery can remove a stale sidecar;
                # the next fenced scan will decide whether it is safe.
                continue
            except OSError as exc:
                raise SwarmCoordinatorError(f"cannot inspect swarm managed file: {name}") from exc

    def _read_records(self) -> tuple[list[dict[str, Any]], bool]:
        try:
            tagged, torn = journal_read_tagged(self.run_dir)
        except FileNotFoundError:
            return [], False
        except JournalCorruptError as exc:
            raise SwarmCorruptError(str(exc)) from exc
        records: list[dict[str, Any]] = []
        for generation, record in tagged:
            if not isinstance(record, dict) or record.get("generation") != generation:
                raise SwarmCorruptError("swarm journal generation mismatch")
            records.append(record)
        return records, torn

    def _state(self, records: list[dict[str, Any]], *, allow_empty: bool = False) -> dict[str, Any]:
        if not records:
            if allow_empty:
                return {"prepared": False, "tasks": {}, "claims": {}, "workers": {},
                        "messages": [], "artifacts": [], "seen": {}, "torn_tail": False}
            raise SwarmNotFoundError(f"swarm run {self.run_id!r} is not initialized")
        prepared = [record for record in records if record.get("event") == "swarm_prepared"]
        if len(prepared) != 1 or records[0].get("event") != "swarm_prepared":
            raise SwarmCorruptError("swarm journal has no unique leading preparation")
        first = prepared[0]
        try:
            max_attempts = _positive_int(first["max_attempts"], "max attempts", MAX_ATTEMPTS)
            project = _digest(first["project_root_sha256"], "project root digest")
            roster = _digest(first["roster_definition_sha256"], "roster definition digest")
            tasks = _materialize_tasks(first["tasks"], max_attempts=max_attempts)
        except (KeyError, TypeError, ValueError, SwarmProtocolError) as exc:
            raise SwarmCorruptError("invalid swarm preparation record") from exc
        state: dict[str, Any] = {
            "prepared": True,
            "run_id": self.run_id,
            "project_root_sha256": project,
            "roster_definition_sha256": roster,
            "max_attempts": max_attempts,
            "tasks": {
                task["task_id"]: {**task, "attempts": [], "terminal": None,
                                  "retry_authorized": False, "uncertain_spend": False}
                for task in tasks
            },
            "claims": {},
            "workers": {},
            "messages": [],
            "artifacts": [],
            "seen": {},
            "closed": False,
        }
        for record in records:
            event = record.get("event")
            if not isinstance(event, str):
                raise SwarmCorruptError("swarm journal record has no event")
            message_id = record.get("message_id")
            if message_id is not None:
                _id(message_id, "journal message id")
                frame_hash = record.get("frame_sha256")
                if frame_hash is not None:
                    _digest(frame_hash, "journal frame digest")
                state["seen"][message_id] = {
                    "frame_sha256": frame_hash,
                    "response": record.get("response"),
                }
            if event in {"swarm_prepared", "journal_repaired"}:
                if event == "swarm_prepared" and state.get("closed"):
                    raise SwarmCorruptError("swarm preparation appears after close")
                continue
            if state.get("closed"):
                raise SwarmCorruptError("swarm journal contains an event after close")
            try:
                self._apply_record(state, record)
            except (KeyError, TypeError, ValueError, SwarmProtocolError) as exc:
                raise SwarmCorruptError(f"invalid swarm journal event {event!r}") from exc
        return state

    @staticmethod
    def _apply_record(state: dict[str, Any], record: Mapping[str, Any]) -> None:
        event = record.get("event")
        task_id = record.get("task_id")
        if event == "worker_registered":
            worker_id = _id(record.get("worker_id"), "worker id")
            state["workers"][worker_id] = {
                "worker_id": worker_id,
                "worker_instance_id": _id(record.get("worker_instance_id"), "worker instance id"),
                "project_root_sha256": _digest(record.get("project_root_sha256"), "project root digest"),
                "roster_definition_sha256": _digest(record.get("roster_definition_sha256"), "roster digest"),
                "permission_ceiling": record.get("permission_ceiling"),
            }
            return
        if event in {"claim_granted", "claim_reclaimed"}:
            task = state["tasks"].get(task_id)
            if task is None:
                raise ValueError("claim references unknown task")
            claim_id = _id(record.get("claim_id"), "claim id")
            if claim_id in state["claims"]:
                raise ValueError("duplicate claim id")
            claim = {
                "task_id": task_id,
                "claim_id": claim_id,
                "worker_id": _id(record.get("worker_id"), "worker id"),
                "attempt": _positive_int(record.get("attempt"), "attempt", MAX_ATTEMPTS),
                "lease_generation": _positive_int(record.get("lease_generation"), "lease generation"),
                "lease_expires_at_ms": _positive_int(record.get("lease_expires_at_ms"), "lease expiry"),
                "request_sha256": _digest(record.get("request_sha256"), "request digest"),
                "status": "active",
                "cancel_requested": False,
            }
            if claim["request_sha256"] != task["request_sha256"]:
                raise ValueError("claim request differs from task")
            state["claims"][claim_id] = claim
            task["attempts"].append(claim_id)
            task["retry_authorized"] = False
            return
        if event == "claim_renewed":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("renew references unknown claim")
            if claim["status"] != "active":
                raise ValueError("renew references terminal claim")
            claim["lease_expires_at_ms"] = _positive_int(record.get("lease_expires_at_ms"), "lease expiry")
            return
        if event in {"claim_released", "claim_indeterminate", "task_completed", "task_failed",
                     "task_cancelled", "task_blocked"}:
            claim_id = record.get("claim_id")
            claim = state["claims"].get(_id(claim_id, "claim id")) if claim_id is not None else None
            if claim is None:
                raise ValueError("terminal event references unknown claim")
            status = {
                "claim_released": "released",
                "claim_indeterminate": "indeterminate",
                "task_completed": "completed",
                "task_failed": "failed",
                "task_cancelled": "cancelled",
                "task_blocked": "blocked",
            }[event]
            claim["status"] = status
            if event in {"task_completed", "task_failed", "task_cancelled", "task_blocked"}:
                state["tasks"][claim["task_id"]]["terminal"] = status
            if event == "claim_indeterminate":
                state["tasks"][claim["task_id"]]["terminal"] = "indeterminate"
                state["tasks"][claim["task_id"]]["uncertain_spend"] = True
            if event == "task_blocked":
                state["tasks"][claim["task_id"]]["uncertain_spend"] = True
            return
        if event == "retry_authorized":
            task = state["tasks"].get(task_id)
            if task is None:
                raise ValueError("retry authorization references unknown task")
            task["retry_authorized"] = True
            task["terminal"] = None
            return
        if event == "cancel_requested":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("cancel references unknown claim")
            claim["cancel_requested"] = True
            return
        if event == "cancel_acknowledged":
            claim = state["claims"].get(_id(record.get("claim_id"), "claim id"))
            if claim is None:
                raise ValueError("cancel ack references unknown claim")
            return
        if event == "message_posted":
            state["messages"].append({
                "message_id": _id(record.get("message_id"), "message id"),
                "from": _id(record.get("from"), "message sender"),
                "to": record.get("to"),
                "content_sha256": _digest(record.get("content_sha256"), "content digest"),
                "content_chars": _positive_int(record.get("content_chars"), "content length"),
                "preview": _bounded_reason(record.get("preview"), "message preview"),
                "reply_to": record.get("reply_to"),
            })
            return
        if event == "artifact_published":
            state["artifacts"].append({
                key: record[key] for key in
                ("artifact_id", "task_id", "claim_id", "attempt", "lease_generation",
                 "request_sha256", "sha256", "bytes", "media_type", "relative_path", "opaque_uri")
                if key in record
            })
            return
        if event == "run_closed":
            state["closed"] = True
            return
        raise ValueError("unknown swarm event")

    def _load(self, *, allow_empty: bool = False) -> tuple[dict[str, Any], bool]:
        records, torn = self._read_records()
        state = self._state(records, allow_empty=allow_empty)
        state["torn_tail"] = torn
        return state, torn

    def _append(self, owner: Owner, record: Mapping[str, Any]) -> None:
        self._fence_run_files()
        payload = dict(record)
        payload["generation"] = owner.generation
        # Keep the coordinator's public journal bounded.  The journal is the
        # source of truth, so bounding only status/events projections would
        # still permit an unbounded disk and replay/memory cost.
        stamped = {**payload, "ts": time.time()}
        serialized = json.dumps(stamped, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()
        line_bytes = len(json.dumps({**stamped, "sha256": digest},
                                    sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")) + 1
        used = 0
        try:
            for path in Path(self.run_dir).glob("journal-g*.jsonl"):
                used += path.stat().st_size
        except OSError as exc:
            raise SwarmCoordinatorError("cannot inspect swarm journal size") from exc
        if used + line_bytes > MAX_JOURNAL_BYTES:
            raise SwarmCoordinatorError("swarm journal limit reached")
        journal_append(self.run_dir, payload, owner)

    @contextlib.contextmanager
    def _mutation(self):
        owner = self._acquire()
        try:
            state, torn = self._load()
            yield owner, state, torn
        finally:
            release_owner(owner)

    def _check_frame(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_frame(encode_frame(frame))
        except (SwarmProtocolError, TypeError, ValueError) as exc:
            raise SwarmCoordinatorError(f"invalid swarm frame: {exc}") from exc
        if parsed["run_id"] != self.run_id:
            raise SwarmCoordinatorError("frame run id does not match coordinator")
        now = _now_ms(self.clock)
        sent = parsed["sent_at_ms"]
        if abs(now - sent) > MAX_CLOCK_SKEW_MS:
            raise SwarmCoordinatorError("frame timestamp is outside the replay window")
        return parsed

    @staticmethod
    def _response(status: str, **values: Any) -> dict[str, Any]:
        return {"status": status, **values}

    def _idempotent(self, state: dict[str, Any], frame: Mapping[str, Any]) -> dict[str, Any] | None:
        message_id = frame["message_id"]
        existing = state["seen"].get(message_id)
        if existing is None:
            return None
        if existing.get("frame_sha256") != frame_sha256(frame):
            raise SwarmConflictError("message id was reused for a different frame")
        response = existing.get("response")
        if not isinstance(response, dict):
            raise SwarmCorruptError("journaled idempotency response is invalid")
        return dict(response)

    def _remembered_append(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any],
                           response: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
        event_name = event.get("event")
        if event_name == "message_posted" and len(state["messages"]) >= MAX_STATUS_MESSAGES:
            raise SwarmCoordinatorError("swarm message limit reached")
        if event_name == "artifact_published" and len(state["artifacts"]) >= MAX_STATUS_ARTIFACTS:
            raise SwarmCoordinatorError("swarm artifact limit reached")
        record = dict(event)
        record.update({"message_id": frame["message_id"],
                       "frame_sha256": frame_sha256(frame),
                       "response": response})
        self._append(owner, record)
        return response

    # --- protocol operations ---------------------------------------------

    def apply_frame(self, frame: Mapping[str, Any], *, worker_id: str = "coordinator") -> dict[str, Any]:
        """Apply one validated wire frame with durable idempotency.

        ``worker_id`` is the authenticated connection identity supplied by a
        future stdio/IDE adapter; it is deliberately not trusted from arbitrary
        frame prose.  The current local API uses the same value explicitly.
        """
        worker_id = _id(worker_id, "worker id")
        parsed = self._check_frame(frame)
        frame_type = parsed["type"]
        if frame_type == "hello":
            return {"status": "ok", "frame": make_frame(
                "hello_ack", run_id=self.run_id,
                message_id=_message_id("ack"), sent_at_ms=_now_ms(self.clock),
                payload={"selected_version": PROTOCOL,
                         "coordinator_id": f"coord-{self.run_id}",
                         "capabilities": ["claim", "renew", "message", "artifact", "cancel"],
                         "permission_ceiling": "read-only"})}
        if frame_type == "poll":
            return self.status()
        if frame_type == "shutdown":
            return self.close()
        with self._mutation() as (owner, state, _torn):
            duplicate = self._idempotent(state, parsed)
            if duplicate is not None:
                return duplicate
            if state.get("closed"):
                raise SwarmConflictError("swarm is closed")
            payload = parsed["payload"]
            if frame_type == "register_worker":
                if payload["worker_id"] != worker_id:
                    raise SwarmCoordinatorError("registered worker does not match connection")
                if payload["project_root_sha256"] != state["project_root_sha256"]:
                    raise SwarmCoordinatorError("worker project binding differs from run")
                if payload["roster_definition_sha256"] != state["roster_definition_sha256"]:
                    raise SwarmCoordinatorError("worker roster binding differs from run")
                existing = state["workers"].get(worker_id)
                if existing and existing["worker_instance_id"] != payload["worker_instance_id"]:
                    raise SwarmConflictError("worker id is already bound to another instance")
                response = self._response("registered", worker_id=worker_id)
                return self._remembered_append(owner, state, parsed, response, {
                    "event": "worker_registered", **{k: payload[k] for k in (
                        "worker_id", "worker_instance_id", "project_root_sha256",
                        "roster_definition_sha256", "permission_ceiling")}})
            # The coordinator itself is the authenticated sender for operator
            # cancellation.  It is not a worker registration and must not be
            # smuggled into the worker registry merely to cancel a claim.
            if frame_type == "cancel_requested" and worker_id == "coordinator":
                return self._apply_cancel_request(owner, state, parsed)
            if worker_id not in state["workers"]:
                raise SwarmCoordinatorError("worker must register before mutating the run")
            if frame_type in {"claim", "claim_requested", "task_claimed"}:
                return self._apply_claim(owner, state, parsed, worker_id)
            if frame_type in {"renew", "lease_renewed"}:
                return self._apply_renew(owner, state, parsed, worker_id)
            if frame_type in {"send_message", "message_posted"}:
                return self._apply_message(owner, state, parsed, worker_id)
            if frame_type in {"publish_artifact", "artifact_published"}:
                return self._apply_artifact(owner, state, parsed, worker_id)
            if frame_type in {"complete", "task_completed", "fail", "task_failed", "task_blocked"}:
                return self._apply_terminal(owner, state, parsed, worker_id)
            if frame_type == "ack_cancel":
                return self._apply_cancel_ack(owner, state, parsed, worker_id)
            if frame_type in {"cancelled", "indeterminate"}:
                return self._apply_cancel_outcome(owner, state, parsed, worker_id)
            if frame_type == "claim_released":
                return self._apply_release(owner, state, parsed, worker_id)
            raise SwarmCoordinatorError(f"frame type {frame_type!r} is not accepted here")

    def _claim_common(self, state: dict[str, Any], payload: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        task_id = _id(payload.get("task_id"), "task id")
        task = state["tasks"].get(task_id)
        if task is None:
            raise SwarmCoordinatorError("unknown task")
        request_sha = _digest(payload.get("request_sha256"), "request digest")
        if request_sha != task["request_sha256"]:
            raise SwarmConflictError("request digest does not match the task")
        active = [state["claims"][claim_id] for claim_id in task["attempts"]
                  if state["claims"][claim_id]["status"] == "active"]
        now = _now_ms(self.clock)
        if active:
            if any(claim["lease_expires_at_ms"] > now for claim in active):
                raise SwarmConflictError("task already has a live claim")
            raise SwarmIndeterminateError("expired claim may have spent; resolve it explicitly before retry")
        if task["terminal"] in {"completed", "cancelled", "blocked"}:
            raise SwarmConflictError(f"task is already {task['terminal']}")
        if task["terminal"] == "indeterminate" and not task["retry_authorized"]:
            raise SwarmIndeterminateError("uncertain spend requires explicit retry authorization")
        attempt = _positive_int(payload.get("attempt"), "attempt", MAX_ATTEMPTS)
        expected_attempt = len(task["attempts"]) + 1
        if attempt != expected_attempt or attempt > state["max_attempts"]:
            raise SwarmConflictError("claim attempt does not match the durable task state")
        lease_generation = _positive_int(payload.get("lease_generation"), "lease generation")
        previous_generations = [state["claims"][claim_id]["lease_generation"] for claim_id in task["attempts"]]
        expected_generation = (max(previous_generations) + 1) if previous_generations else 1
        if lease_generation != expected_generation:
            raise SwarmConflictError("claim lease generation does not match the durable task state")
        expires = _positive_int(payload.get("lease_expires_at_ms"), "lease expiry")
        if expires <= now or expires > now + MAX_LEASE_MS:
            raise SwarmConflictError("claim lease expiry is outside the allowed window")
        return {"task_id": task_id, "task": task, "request_sha256": request_sha,
                "attempt": attempt, "lease_generation": lease_generation,
                "lease_expires_at_ms": expires, "worker_id": worker_id}

    def _apply_claim(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        info = self._claim_common(state, payload, worker_id)
        claim_id = _id(payload.get("claim_id"), "claim id")
        if claim_id in state["claims"]:
            raise SwarmConflictError("claim id already exists")
        response = self._response("claimed", task_id=info["task_id"], claim_id=claim_id,
                                  attempt=info["attempt"], lease_generation=info["lease_generation"],
                                  lease_expires_at_ms=info["lease_expires_at_ms"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_granted", "task_id": info["task_id"], "claim_id": claim_id,
            "worker_id": worker_id, "attempt": info["attempt"],
            "lease_generation": info["lease_generation"],
            "lease_expires_at_ms": info["lease_expires_at_ms"],
            "request_sha256": info["request_sha256"]})

    def _claim_for_update(self, state: dict[str, Any], payload: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim_id = _id(payload.get("claim_id"), "claim id")
        claim = state["claims"].get(claim_id)
        if claim is None:
            raise SwarmCoordinatorError("unknown claim")
        if claim["worker_id"] != worker_id:
            raise SwarmConflictError("claim belongs to another worker")
        if claim["status"] != "active":
            raise SwarmConflictError("claim is no longer active")
        if claim["lease_expires_at_ms"] <= _now_ms(self.clock):
            raise SwarmIndeterminateError("claim lease expired; resolve uncertain spend explicitly")
        if _positive_int(payload.get("lease_generation"), "lease generation") != claim["lease_generation"]:
            raise SwarmConflictError("claim lease generation is stale")
        return claim

    def _apply_renew(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        if claim.get("cancel_requested"):
            raise SwarmConflictError("claim cancellation is pending")
        expires = _positive_int(payload.get("lease_expires_at_ms"), "lease expiry")
        now = _now_ms(self.clock)
        if expires <= now or expires > now + MAX_LEASE_MS:
            raise SwarmConflictError("renewed lease expiry is outside the allowed window")
        response = self._response("renewed", claim_id=claim["claim_id"],
                                  lease_generation=claim["lease_generation"],
                                  lease_expires_at_ms=expires)
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_renewed", "claim_id": claim["claim_id"],
            "worker_id": worker_id, "lease_generation": claim["lease_generation"],
            "lease_expires_at_ms": expires})

    def _apply_message(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        if payload.get("from") != worker_id:
            raise SwarmConflictError("message sender does not match connection")
        target = payload.get("to")
        if target not in {"coordinator", "human"} and target not in state["workers"]:
            raise SwarmCoordinatorError("message recipient is not registered")
        response = self._response("posted", message_id=frame["message_id"], to=target)
        event = {"event": "message_posted", "from": worker_id, "to": target,
                 "content_sha256": payload["content_sha256"],
                 "content_chars": payload["content_chars"],
                 "preview": _bounded_reason(payload["preview"], "message preview")}
        if payload.get("reply_to") is not None:
            event["reply_to"] = payload["reply_to"]
        return self._remembered_append(owner, state, frame, response, event)

    def _apply_artifact(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        for key in ("task_id", "attempt", "request_sha256"):
            if payload.get(key) != claim[key] and not (key == "task_id" and payload.get(key) == claim["task_id"]):
                raise SwarmConflictError("artifact claim fence does not match")
        if payload.get("attempt") != claim["attempt"] or payload.get("request_sha256") != claim["request_sha256"]:
            raise SwarmConflictError("artifact claim fence does not match")
        artifact = {key: payload[key] for key in (
            "artifact_id", "task_id", "claim_id", "attempt", "lease_generation",
            "request_sha256", "sha256", "bytes", "media_type")}
        if "relative_path" in payload:
            artifact["relative_path"] = payload["relative_path"]
        if "opaque_uri" in payload:
            artifact["opaque_uri"] = payload["opaque_uri"]
        response = self._response("artifact_published", artifact_id=payload["artifact_id"])
        return self._remembered_append(owner, state, frame, response,
                                       {"event": "artifact_published", **artifact})

    def _apply_terminal(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        payload = frame["payload"]
        claim = self._claim_for_update(state, payload, worker_id)
        if payload.get("task_id") != claim["task_id"] or payload.get("attempt") != claim["attempt"]:
            raise SwarmConflictError("terminal claim fence does not match")
        if payload.get("request_sha256") != claim["request_sha256"]:
            raise SwarmConflictError("terminal request fence does not match")
        if frame["type"] in {"complete", "task_completed"}:
            if claim["cancel_requested"]:
                raise SwarmConflictError("claim has a pending cancellation")
            event, status = "task_completed", "completed"
        elif frame["type"] in {"fail", "task_failed"}:
            event, status = "task_failed", "failed"
        else:
            event, status = "task_blocked", "blocked"
        event_data: dict[str, Any] = {"event": event, "task_id": claim["task_id"],
                                      "claim_id": claim["claim_id"], "worker_id": worker_id,
                                      "attempt": claim["attempt"],
                                      "lease_generation": claim["lease_generation"],
                                      "request_sha256": claim["request_sha256"],
                                      "envelope_sha256": payload.get("envelope_sha256")}
        if payload.get("reason") is not None:
            event_data["reason"] = _bounded_reason(payload["reason"])
        response = self._response(status, task_id=claim["task_id"], claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, event_data)

    def _apply_cancel_request(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any]) -> dict[str, Any]:
        payload = frame["payload"]
        claim = state["claims"].get(_id(payload.get("claim_id"), "claim id"))
        if claim is None or claim["status"] != "active":
            raise SwarmConflictError("claim is not cancellable")
        if claim["lease_expires_at_ms"] <= _now_ms(self.clock):
            raise SwarmIndeterminateError("claim lease expired; resolve uncertain spend explicitly")
        if payload.get("lease_generation") != claim["lease_generation"]:
            raise SwarmConflictError("cancel lease generation is stale")
        response = self._response("cancel_requested", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "cancel_requested", "claim_id": claim["claim_id"],
            "lease_generation": claim["lease_generation"],
            "reason": _bounded_reason(payload.get("reason"))})

    def _apply_cancel_ack(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        if not claim["cancel_requested"]:
            raise SwarmConflictError("cancel has not been requested")
        response = self._response("cancel_acknowledged", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "cancel_acknowledged", "claim_id": claim["claim_id"],
            "worker_id": worker_id, "lease_generation": claim["lease_generation"]})

    def _apply_cancel_outcome(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        if not claim["cancel_requested"]:
            raise SwarmConflictError("cancel has not been requested")
        event = "task_cancelled" if frame["type"] == "cancelled" else "claim_indeterminate"
        status = "cancelled" if event == "task_cancelled" else "indeterminate"
        response = self._response(status, claim_id=claim["claim_id"], uncertain_spend=(status == "indeterminate"))
        return self._remembered_append(owner, state, frame, response, {
            "event": event, "task_id": claim["task_id"], "claim_id": claim["claim_id"],
            "worker_id": worker_id, "attempt": claim["attempt"],
            "lease_generation": claim["lease_generation"],
            "request_sha256": claim["request_sha256"],
            "reason": _bounded_reason(frame["payload"].get("reason"))})

    def _apply_release(self, owner: Owner, state: dict[str, Any], frame: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
        claim = self._claim_for_update(state, frame["payload"], worker_id)
        response = self._response("released", claim_id=claim["claim_id"])
        return self._remembered_append(owner, state, frame, response, {
            "event": "claim_released", "task_id": claim["task_id"],
            "claim_id": claim["claim_id"], "worker_id": worker_id,
            "attempt": claim["attempt"], "lease_generation": claim["lease_generation"],
            "request_sha256": claim["request_sha256"],
            "reason": _bounded_reason(frame["payload"].get("reason"))})

    # --- ergonomic local API ---------------------------------------------

    def register_worker(self, worker_id: str, *, worker_instance_id: str,
                        capabilities: list[str] | None = None,
                        permission_ceiling: str = "read-only") -> dict[str, Any]:
        state, _ = self._load()
        payload = {"worker_id": _id(worker_id, "worker id"),
                   "worker_instance_id": _id(worker_instance_id, "worker instance id"),
                   "project_root_sha256": state["project_root_sha256"],
                   "roster_definition_sha256": state["roster_definition_sha256"],
                   "capabilities": capabilities or [], "permission_ceiling": permission_ceiling}
        frame = make_frame("register_worker", run_id=self.run_id,
                           message_id=_message_id("register"), sent_at_ms=_now_ms(self.clock),
                           payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def claim(self, worker_id: str, task_id: str, *, request_sha256: str,
              lease_ms: int = 30_000, message_id: str | None = None) -> dict[str, Any]:
        state, _ = self._load()
        task = state["tasks"].get(_id(task_id, "task id"))
        if task is None:
            raise SwarmCoordinatorError("unknown task")
        attempt = len(task["attempts"]) + 1
        previous = [state["claims"][claim_id]["lease_generation"] for claim_id in task["attempts"]]
        generation = max(previous) + 1 if previous else 1
        now = _now_ms(self.clock)
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or not 1 <= lease_ms <= MAX_LEASE_MS:
            raise SwarmProtocolError("invalid lease duration")
        frame = make_frame("claim", run_id=self.run_id,
                           message_id=message_id or _message_id("claim"), sent_at_ms=now,
                           payload={"task_id": task_id, "claim_id": _message_id("claim"),
                                    "attempt": attempt, "lease_generation": generation,
                                    "lease_expires_at_ms": now + lease_ms,
                                    "request_sha256": request_sha256})
        return self.apply_frame(frame, worker_id=worker_id)

    def renew(self, worker_id: str, claim_id: str, lease_generation: int, *,
              lease_ms: int = 30_000, message_id: str | None = None) -> dict[str, Any]:
        now = _now_ms(self.clock)
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or not 1 <= lease_ms <= MAX_LEASE_MS:
            raise SwarmProtocolError("invalid lease duration")
        frame = make_frame("renew", run_id=self.run_id,
                           message_id=message_id or _message_id("renew"), sent_at_ms=now,
                           payload={"claim_id": claim_id, "lease_generation": lease_generation,
                                    "lease_expires_at_ms": now + lease_ms})
        return self.apply_frame(frame, worker_id=worker_id)

    def send_message(self, worker_id: str, recipient: str, content: str, *,
                     reply_to: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        if not isinstance(content, str) or not content or len(content) > 12_000:
            raise SwarmProtocolError("invalid message content")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        preview = _bounded_reason(content[:512], "message preview")
        payload: dict[str, Any] = {"from": worker_id, "to": recipient,
                                   "content_sha256": digest, "content_chars": len(content),
                                   "preview": preview}
        if reply_to is not None:
            payload["reply_to"] = reply_to
        frame = make_frame("send_message", run_id=self.run_id,
                           message_id=message_id or _message_id("message"),
                           sent_at_ms=_now_ms(self.clock), payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def publish_artifact(self, worker_id: str, *, task_id: str, claim_id: str,
                         attempt: int, lease_generation: int, request_sha256: str,
                         artifact_id: str, sha256: str, bytes_count: int,
                         media_type: str, relative_path: str | None = None,
                         opaque_uri: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"task_id": task_id, "claim_id": claim_id,
                                   "attempt": attempt, "lease_generation": lease_generation,
                                   "request_sha256": request_sha256, "artifact_id": artifact_id,
                                   "sha256": sha256, "bytes": bytes_count, "media_type": media_type}
        if relative_path is not None:
            payload["relative_path"] = relative_path
        if opaque_uri is not None:
            payload["opaque_uri"] = opaque_uri
        frame = make_frame("publish_artifact", run_id=self.run_id,
                           message_id=message_id or _message_id("artifact"),
                           sent_at_ms=_now_ms(self.clock), payload=payload)
        return self.apply_frame(frame, worker_id=worker_id)

    def complete(self, worker_id: str, *, task_id: str, claim_id: str, attempt: int,
                 lease_generation: int, request_sha256: str, envelope_sha256: str,
                 message_id: str | None = None) -> dict[str, Any]:
        frame = make_frame("complete", run_id=self.run_id,
                           message_id=message_id or _message_id("complete"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"task_id": task_id, "claim_id": claim_id,
                                    "attempt": attempt, "lease_generation": lease_generation,
                                    "request_sha256": request_sha256,
                                    "envelope_sha256": envelope_sha256})
        return self.apply_frame(frame, worker_id=worker_id)

    def fail(self, worker_id: str, *, task_id: str, claim_id: str, attempt: int,
             lease_generation: int, request_sha256: str, envelope_sha256: str,
             reason: str = "worker_failed", message_id: str | None = None) -> dict[str, Any]:
        frame = make_frame("fail", run_id=self.run_id,
                           message_id=message_id or _message_id("fail"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"task_id": task_id, "claim_id": claim_id,
                                    "attempt": attempt, "lease_generation": lease_generation,
                                    "request_sha256": request_sha256,
                                    "envelope_sha256": envelope_sha256, "reason": reason})
        return self.apply_frame(frame, worker_id=worker_id)

    def cancel(self, task_id: str, *, reason: str = "operator_requested",
               message_id: str | None = None) -> dict[str, Any]:
        state, _ = self._load()
        task = state["tasks"].get(_id(task_id, "task id"))
        if task is None:
            raise SwarmCoordinatorError("unknown task")
        active = [state["claims"][claim_id] for claim_id in task["attempts"]
                  if state["claims"][claim_id]["status"] == "active"]
        if not active:
            raise SwarmConflictError("task has no active claim")
        claim = active[-1]
        frame = make_frame("cancel_requested", run_id=self.run_id,
                           message_id=message_id or _message_id("cancel"),
                           sent_at_ms=_now_ms(self.clock),
                           payload={"claim_id": claim["claim_id"],
                                    "lease_generation": claim["lease_generation"],
                                    "reason": reason})
        return self.apply_frame(frame, worker_id="coordinator")

    def acknowledge_indeterminate(self, task_id: str, *, allow_retry: bool,
                                  reason: str, human_confirmed: bool = False) -> dict[str, Any]:
        if not isinstance(allow_retry, bool) or not isinstance(human_confirmed, bool):
            raise SwarmCoordinatorError("indeterminate recovery flags must be boolean")
        if not human_confirmed:
            raise SwarmCoordinatorError("indeterminate recovery requires human confirmation")
        task_id = _id(task_id, "task id")
        reason = _bounded_reason(reason)
        with self._mutation() as (owner, state, _torn):
            if state.get("closed"):
                raise SwarmConflictError("swarm is closed")
            task = state["tasks"].get(task_id)
            if task is None:
                raise SwarmCoordinatorError("unknown task")
            claims = [state["claims"][claim_id] for claim_id in task["attempts"]]
            uncertain = [claim for claim in claims if claim["status"] in {"active", "indeterminate"}]
            if not uncertain:
                raise SwarmConflictError("task has no indeterminate claim")
            latest = uncertain[-1]
            event = "retry_authorized" if allow_retry else "task_blocked"
            self._append(owner, {"event": "claim_indeterminate", "task_id": task_id,
                                 "claim_id": latest["claim_id"],
                                 "attempt": latest["attempt"],
                                 "lease_generation": latest["lease_generation"],
                                 "request_sha256": latest["request_sha256"],
                                 "reason": reason,
                                 "human_confirmed": True,
                                 "message_id": _message_id("recovery")})
            self._append(owner, {"event": event, "task_id": task_id,
                                 "claim_id": latest["claim_id"],
                                 "reason": reason,
                                 "human_confirmed": True,
                                 "message_id": _message_id("recovery")})
            if allow_retry:
                return self._response("retry_authorized", task_id=task_id,
                                      previous_claim_id=latest["claim_id"])
            return self._response("blocked", task_id=task_id, uncertain_spend=True)

    def close(self) -> dict[str, Any]:
        with self._mutation() as (owner, state, _torn):
            if state["closed"]:
                return self._response("closed", run_id=self.run_id)
            live = [claim for claim in state["claims"].values() if claim["status"] == "active"]
            if live:
                raise SwarmConflictError("cannot close a swarm with active claims")
            unfinished = [task for task in state["tasks"].values()
                          if task["terminal"] not in {"completed", "failed", "cancelled", "blocked"}]
            if unfinished:
                raise SwarmConflictError("cannot close a swarm with unfinished tasks")
            self._append(owner, {"event": "run_closed", "message_id": _message_id("close")})
            return self._response("closed", run_id=self.run_id)

    # --- read-only projections -------------------------------------------

    def status(self) -> dict[str, Any]:
        try:
            state, torn = self._load()
        except SwarmNotFoundError:
            return {"status": "missing", "run_id": self.run_id}
        now = _now_ms(self.clock)
        tasks = []
        uncertain = False
        for task in state["tasks"].values():
            claims = [state["claims"][claim_id] for claim_id in task["attempts"]]
            latest = claims[-1] if claims else None
            task_status = task["terminal"] or ("expired" if latest and latest["status"] == "active"
                                                and latest["lease_expires_at_ms"] <= now else
                                                "claimed" if latest and latest["status"] == "active" else "pending")
            if task_status in {"expired", "indeterminate"}:
                uncertain = True
            tasks.append({"task_id": task["task_id"],
                          "request_sha256": task["request_sha256"],
                          "status": task_status,
                          "attempts": len(claims),
                          "active_claim": ({"claim_id": latest["claim_id"],
                                             "worker_id": latest["worker_id"],
                                             "lease_generation": latest["lease_generation"],
                                             "lease_expires_at_ms": latest["lease_expires_at_ms"],
                                             "cancel_requested": latest["cancel_requested"]}
                                            if latest and latest["status"] == "active" else None)})
        if state["closed"]:
            status = "closed"
        elif any(task["status"] == "expired" for task in tasks):
            status = "uncertain_spend"
        elif tasks and all(task["status"] in {"completed", "failed", "cancelled", "blocked"} for task in tasks):
            status = "completed"
        elif any(task["status"] in {"claimed"} for task in tasks):
            status = "running"
        else:
            status = "prepared"
        return {"status": status, "run_id": self.run_id,
                "project_root_sha256": state["project_root_sha256"],
                "roster_definition_sha256": state["roster_definition_sha256"],
                "max_attempts": state["max_attempts"],
                "generation": max((record.get("generation", 0) for record in self._read_records()[0]), default=0),
                "tasks": tasks[:MAX_STATUS_TASKS],
                "worker_count": len(state["workers"]),
                "message_count": min(len(state["messages"]), MAX_STATUS_MESSAGES),
                "artifact_count": min(len(state["artifacts"]), MAX_STATUS_ARTIFACTS),
                "uncertain_spend": uncertain or any(
                    state["tasks"][task["task_id"]].get("uncertain_spend", False)
                    for task in tasks),
                "torn_tail": bool(torn)}

    def events(self) -> list[dict[str, Any]]:
        """Return the bounded public event projection; never raw worker text."""
        records, _torn = self._read_records()
        public: list[dict[str, Any]] = []
        for record in records:
            value = {key: record[key] for key in (
                "generation", "ts", "event", "message_id", "task_id", "claim_id",
                "worker_id", "attempt", "lease_generation", "lease_expires_at_ms",
                "request_sha256", "project_root_sha256", "roster_definition_sha256",
                "artifact_id", "sha256", "bytes", "media_type", "content_sha256",
                "content_chars", "preview", "to", "from", "status", "reason")
                     if key in record}
            public.append(value)
        return public


def run_command(args) -> int:
    """Run the provider-inert ``summon swarm`` management surface.

    The CLI intentionally exposes lifecycle coordination, not provider launch.
    Host adapters can use :class:`SwarmCoordinator` directly and must still
    prove their own process-tree, permission, and billing contracts.
    """
    action = getattr(args, "swarm_action", None)
    root = getattr(args, "swarm_dir", None)
    if not root:
        root = os.path.join(os.path.abspath(getattr(args, "cwd", None) or os.getcwd()),
                            ".agents", "swarm")
    run_id = getattr(args, "swarm_run_id", None)
    try:
        if not isinstance(action, str) or not action:
            raise SwarmCoordinatorError("swarm action is required")
        if not isinstance(run_id, str) or not run_id:
            raise SwarmCoordinatorError("swarm run id is required")
        if action == "create":
            task_file = getattr(args, "swarm_tasks", None)
            if not task_file:
                raise SwarmCoordinatorError("swarm create requires --swarm-tasks FILE")
            task_path = Path(task_file).expanduser()
            if task_path.stat().st_size > 2 * 1024 * 1024:
                raise SwarmCoordinatorError("swarm task file is too large")
            with task_path.open(encoding="utf-8") as handle:
                tasks = json.load(handle)
            if not isinstance(tasks, list):
                raise SwarmCoordinatorError("swarm task file must contain a JSON array")
            coordinator = SwarmCoordinator.create(
                root, run_id,
                project_root_sha256=getattr(args, "swarm_project_root_sha256", None),
                roster_definition_sha256=getattr(args, "swarm_roster_sha256", None),
                tasks=tasks,
                max_attempts=getattr(args, "swarm_max_attempts", None) or 1,
            )
            result = coordinator.status()
        else:
            coordinator = SwarmCoordinator(root, run_id)
            if action == "status":
                result = coordinator.status()
            elif action == "events":
                result = {"status": "ok", "run_id": run_id, "events": coordinator.events()}
            elif action == "register":
                result = coordinator.register_worker(
                    getattr(args, "swarm_worker", None),
                    worker_instance_id=getattr(args, "swarm_instance", None),
                    capabilities=[], permission_ceiling="read-only")
            elif action == "claim":
                result = coordinator.claim(
                    getattr(args, "swarm_worker", None), getattr(args, "swarm_task_id", None),
                    request_sha256=getattr(args, "swarm_request_sha256", None),
                    lease_ms=getattr(args, "swarm_lease_ms", None) or 30_000)
            elif action == "renew":
                result = coordinator.renew(
                    getattr(args, "swarm_worker", None), getattr(args, "swarm_claim_id", None),
                    getattr(args, "swarm_lease_generation", None),
                    lease_ms=getattr(args, "swarm_lease_ms", None) or 30_000)
            elif action == "cancel":
                result = coordinator.cancel(getattr(args, "swarm_task_id", None),
                                             reason=getattr(args, "swarm_reason", None) or "operator_requested")
            elif action == "close":
                result = coordinator.close()
            else:
                raise SwarmCoordinatorError(f"unknown swarm action {action!r}")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, SwarmCoordinatorError,
            SwarmProtocolError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error_kind": type(exc).__name__},
                         ensure_ascii=False))
        return 1


__all__ = ["SwarmCoordinator", "SwarmCoordinatorError", "SwarmNotFoundError",
           "SwarmCorruptError", "SwarmConflictError", "SwarmIndeterminateError",
           "run_command"]
