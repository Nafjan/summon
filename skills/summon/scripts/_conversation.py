"""Provider-inert conversation rooms for Summon.

This module is intentionally smaller than both the council runner and the
deliberation kernel.  It records a bounded, append-only conversation context
that those surfaces may display, but it never launches a provider and never
changes a ballot or a deliberation state.  The native journal keeps local
operator text; the public projection is an explicit allowlist and digest
boundary.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
MAX_EVENTS = 4096
MAX_EVENT_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 12_000
MAX_SUMMARY_CHARS = 2_000
MAX_PARTICIPANTS = 16
MAX_ROOMS = 512
MAX_CROSS_EXAMS = 64
MAX_RECOMMENDATION_ITEMS = 16
MAX_REASON_CHARS = 512
MAX_ROOM_FILES = MAX_ROOMS * 4
APPEND_LOCK_STALE_SECONDS = 30.0

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DISPLAY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+@#-]{0,127}$")
_PATHISH_RE = re.compile(r"(?:^[A-Za-z]:[\\/]|^\\\\|^/|\b[A-Za-z]:[\\/])")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODES = frozenset({"chat", "council", "deliberate"})
_ACTOR_KINDS = frozenset({"human", "agent", "system"})
_EVENTS = frozenset({
    "session_created", "message_posted", "human_message", "turn_started",
    "turn_finished", "turn_cancel_requested", "council_round_started", "position_submitted",
    "cross_exam", "chair_synthesis", "deliberation_policy_bound",
    "ballot_accepted", "state_transition", "cleanup_receipt", "fork_created",
    "agent_message",
})
_CONTROL_EVENTS = frozenset({"ballot_accepted", "state_transition", "cleanup_receipt"})


class ConversationError(ValueError):
    """A malformed, conflicting, or unsafe conversation-room operation."""


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConversationError(f"invalid {label}")
    text = value
    if not _ID_RE.fullmatch(text):
        raise ConversationError(f"invalid {label}")
    return text


def _safe_display(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ConversationError(f"invalid {label}")
    if not text:
        if required:
            raise ConversationError(f"missing {label}")
        return None
    if not _DISPLAY_RE.fullmatch(text):
        raise ConversationError(f"invalid {label}")
    if _PATHISH_RE.search(text):
        raise ConversationError(f"invalid {label}")
    return text


def _sha256(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ConversationError(f"invalid {label}")
    text = value.strip()
    if required and not text:
        raise ConversationError(f"missing {label}")
    if len(text) > MAX_SUMMARY_CHARS:
        raise ConversationError(f"{label} is too long")
    return text


def _redact_text(value: str) -> str:
    """Keep ordinary prose useful while removing obvious local secret/path data."""
    # Paths with spaces are deliberately consumed through a conservative
    # punctuation boundary.  Losing a little prose is preferable to leaking a
    # credential-bearing workspace path into a public room/recommendation.
    text = re.sub(r"(?i)\b[A-Za-z]:[\\/][^\r\n,;]*", "[path]", value)
    text = re.sub(r"(?i)\\\\[^\r\n,;]*", "[path]", text)
    text = re.sub(r"(?<![\w])/(?:[^\r\n,;]*)", "[path]", text)
    text = re.sub(r"(?<![\w])(?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.-]+", "[path]", text)
    text = re.sub(r"(?i)(?:token|password|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
                  "[redacted]", text)
    # Common provider/token shapes are sensitive even when a pasted log does
    # not label them.  Keep the patterns conservative so ordinary prose and
    # model names remain readable while bearer/API material cannot enter a
    # public room projection.
    text = re.sub(r"(?i)\b(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b",
                  "[redacted]", text)
    # Generic HTTP credentials do not always use a provider-specific prefix.
    # Redact the complete Authorization/Bearer/Basic token before it can reach
    # a room projection, while leaving ordinary prose such as "bearer token"
    # readable when no credential-shaped value follows it.
    text = re.sub(
        r"(?i)(?:\bauthorization\s*:\s*)?\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}",
        "[redacted]", text)
    text = re.sub(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+", "[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                  "[redacted]", text)
    # Also hide standalone fixture/credential labels that commonly appear in
    # pasted logs (for example ``SECRET_TOKEN``) even when no ``key=value``
    # separator is present.  This keeps bounded chat previews useful without
    # turning the public projection into a raw provider transcript.
    text = re.sub(r"\b(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)[A-Z0-9_-]{3,}\b",
                  "[redacted]", text, flags=re.IGNORECASE)
    return text


def _snapshot(value: Any) -> Any:
    """Copy untrusted mappings before validation to close accessor TOCTOU seams."""
    try:
        return copy.deepcopy(value)
    except Exception as exc:  # noqa: BLE001 - public boundary
        raise ConversationError("conversation value is not snapshot-safe") from exc


def _bounded_items(values: Iterable[Any], limit: int, label: str) -> list[Any]:
    """Materialize an iterable without allowing an unbounded caller to DoS us."""
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ConversationError(f"invalid {label}") from exc
    result: list[Any] = []
    for item in iterator:
        if len(result) >= limit:
            raise ConversationError(f"too many {label}")
        result.append(item)
    return result


def _redacted_bounded_text(value: Any, label: str) -> str:
    return _redact_text(_bounded_text(value, label))


def _project_participant(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise ConversationError("invalid participant")
    item = _snapshot(dict(value))
    agent = _safe_id(item.get("agent") or item.get("id"), "participant agent")
    out = {"agent": agent}
    for key in ("role", "name", "version"):
        display = _safe_display(item.get(key), f"participant {key}")
        if display is not None:
            out[key] = display
    return out


def _public_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the safe event payload; never pass arbitrary journal fields through."""
    data = _snapshot(dict(payload))
    out: dict[str, Any] = {}
    if event_type == "human_message":
        message_id = _safe_id(data.get("message_id"), "message id")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            raise ConversationError("human message must be non-empty and bounded")
        # Keep native text for provider context, but give the browser a bounded,
        # redacted preview so a room is actually readable. The preview is never
        # the source of truth and never contains raw native text, credentials,
        # or local paths.
        preview = _redact_text(text)[:MAX_SUMMARY_CHARS]
        return {"message_id": message_id, "text_sha256": _text_sha256(text),
                "text_chars": len(text),
                "preview": preview, "summary": preview}
    if event_type == "message_posted":
        message_id = _safe_id(data.get("message_id"), "message id")
        summary = data.get("summary")
        if not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS:
            raise ConversationError("message summary is missing or too long")
        return {"message_id": message_id, "summary": _redact_text(summary)}
    if event_type == "agent_message":
        message_id = _safe_id(data.get("message_id"), "message id")
        sender = _safe_id(data.get("sender"), "message sender")
        recipient = _safe_id(data.get("recipient"), "message recipient")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            raise ConversationError("agent message must be non-empty and bounded")
        preview = _redact_text(text)[:MAX_SUMMARY_CHARS]
        return {"message_id": message_id, "sender": sender, "recipient": recipient,
                "text_sha256": _text_sha256(text), "text_chars": len(text),
                "preview": preview, "summary": preview}
    if event_type == "session_created":
        out["project_id"] = _safe_id(data.get("project_id"), "project id")
        out["initiator_host"] = _safe_display(data.get("initiator_host"), "initiator host", required=True)
        out["initiator_agent"] = _safe_id(data.get("initiator_agent"), "initiator agent")
        if data.get("mode") not in _MODES:
            raise ConversationError("invalid conversation mode")
        out["mode"] = data["mode"]
        participants = data.get("participants", [])
        if not isinstance(participants, list) or len(participants) > MAX_PARTICIPANTS:
            raise ConversationError("participants are invalid")
        projected = [_project_participant(p) for p in participants]
        ids = [item["agent"] for item in projected]
        if len(ids) != len(set(ids)):
            raise ConversationError("duplicate conversation participant")
        out["participants"] = projected
        return out
    if event_type in {"turn_started", "turn_finished", "turn_cancel_requested",
                      "position_submitted", "cross_exam",
                      "chair_synthesis", "council_round_started", "deliberation_policy_bound"}:
        keys = ("turn_id", "round", "participant", "asker", "target", "command_id",
                "role", "name", "version",
                "option_ids", "recommended_option", "evidence_sha256", "summary")
        if event_type in {"turn_started", "turn_finished"}:
            keys += ("status", "provider", "model_target", "model_served", "permission",
                     "transport", "prompt_sha256", "result_sha256", "prompt_chars", "resumed")
        for key in keys:
            value = data.get(key)
            if key.endswith("_sha256"):
                if value is not None and (not isinstance(value, str) or not _SHA256_RE.fullmatch(value)):
                    raise ConversationError(f"invalid {key}")
                if value is not None:
                    out[key] = value
            elif key == "summary":
                if value is not None and (not isinstance(value, str) or len(value) > MAX_SUMMARY_CHARS):
                    raise ConversationError("summary is too long")
                if value is not None:
                    out[key] = _redact_text(value)
            elif key == "round":
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 10):
                    raise ConversationError("invalid round")
            elif key == "prompt_chars":
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                          or value < 0 or value > MAX_MESSAGE_CHARS * 3):
                    raise ConversationError("invalid prompt length")
                if value is not None:
                    out[key] = value
            elif key == "resumed":
                if value is not None and not isinstance(value, bool):
                    raise ConversationError("invalid resumed flag")
                if value is not None:
                    out[key] = value
            elif value is not None:
                if key in {"turn_id", "participant", "asker", "target", "command_id"}:
                    out[key] = _safe_id(value, key)
                elif key in {"role", "name", "version", "provider", "model_target", "model_served",
                             "permission", "transport", "status"}:
                    out[key] = _safe_display(value, key)
                elif key == "option_ids":
                    if not isinstance(value, list) or len(value) > 32:
                        raise ConversationError("invalid option ids")
                    out[key] = [_safe_id(item, "option id") for item in value]
                elif key == "recommended_option":
                    out[key] = _safe_id(value, key)
                else:
                    out[key] = value
        return out
    if event_type == "fork_created":
        out["parent_session_id"] = _safe_id(data.get("parent_session_id"), "parent session id")
        out["child_session_id"] = _safe_id(data.get("child_session_id"), "child session id")
        reason = _safe_display(data.get("reason"), "fork reason", required=True)
        out["reason"] = _redact_text(reason)
        return out
    if event_type in _CONTROL_EVENTS:
        # Conversation views can display control facts, but the deliberation
        # kernel remains the only authority.  Validate only safe fingerprints
        # and identifiers; do not allow prose or arbitrary model fields through.
        for key in ("option_id", "decision_id", "command_id", "reason"):
            value = data.get(key)
            if value is not None:
                out[key] = _safe_id(value, key) if key.endswith("_id") else _safe_display(value, key)
        for key in ("request_digest", "evidence_sha256", "command_batch_sha256"):
            value = data.get(key)
            if value is not None:
                if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                    raise ConversationError(f"invalid {key}")
                out[key] = value
        return out
    return out


def _root_path(root: str | os.PathLike[str], *, create: bool = True) -> Path:
    raw = Path(root).expanduser()
    # Inspect the supplied path before resolve(); resolving first would turn a
    # junction/symlink into an apparently ordinary directory.
    for candidate in (raw, *raw.parents):
        try:
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise ConversationError("conversation root may not contain symlinks")
        except OSError as exc:
            raise ConversationError("conversation root cannot be inspected") from exc
    path = raw
    try:
        path = path.resolve()
    except OSError as exc:
        raise ConversationError("conversation root cannot be resolved") from exc
    if path.exists() and (path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())):
        raise ConversationError("conversation root may not be a symlink")
    if path.exists() and not path.is_dir():
        raise ConversationError("conversation root is not a directory")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise ConversationError("conversation root does not exist")
    return path


@dataclass(frozen=True)
class ContinuationDecision:
    action: str
    reason: str
    parent_session_id: str

    def as_dict(self) -> dict[str, str]:
        return {"action": self.action, "reason": self.reason,
                "parent_session_id": self.parent_session_id}


@dataclass(frozen=True)
class ConversationRoom:
    session_id: str
    project_id: str
    project_root_sha256: str
    initiator_host: str
    initiator_agent: str
    mode: str
    participants: tuple[dict[str, str], ...]
    created_at_ms: int
    cursor: int

    def as_dict(self, *, native: bool = False) -> dict[str, Any]:
        # Native views still intentionally expose only the project hash, never
        # the original cwd.  The tuple is copied to prevent mutation leaks.
        return {"session_id": self.session_id, "project_id": self.project_id,
                "project_root_sha256": self.project_root_sha256,
                "initiator_host": self.initiator_host,
                "initiator_agent": self.initiator_agent, "mode": self.mode,
                "participants": [dict(item) for item in self.participants],
                "created_at_ms": self.created_at_ms, "cursor": self.cursor,
                "redaction": "local-native-allowlist" if native else "public-redacted"}


class ConversationJournal:
    """A small JSONL room journal with cursor/idempotency checks."""

    def __init__(self, path: str | os.PathLike[str], header: Mapping[str, Any],
                 records: Iterable[Mapping[str, Any]] = (), *, allow_empty: bool = False):
        self.path = Path(path)
        self._header = _snapshot(dict(header))
        self._records = [_snapshot(dict(item)) for item in records]
        self._validate_header(self._header)
        self._validate_records(self._records, self._header, allow_empty=allow_empty)

    @classmethod
    def create(cls, root: str | os.PathLike[str], *, session_id: str,
               project_id: str, project_root: str | os.PathLike[str],
               initiator_host: str, initiator_agent: str, mode: str = "chat",
               participants: Iterable[Mapping[str, Any]] = ()) -> "ConversationJournal":
        session = _safe_id(session_id, "session id")
        project = _safe_id(project_id, "project id")
        host = _safe_display(initiator_host, "initiator host", required=True)
        agent = _safe_id(initiator_agent, "initiator agent")
        if mode not in _MODES:
            raise ConversationError("invalid conversation mode")
        try:
            project_path = Path(project_root).expanduser().resolve()
        except OSError as exc:
            raise ConversationError("project root cannot be resolved") from exc
        if not project_path.is_dir():
            raise ConversationError("project root does not exist")
        participant_list = [_project_participant(item) for item in
                            _bounded_items(participants, MAX_PARTICIPANTS, "participants")]
        participant_ids = [item["agent"] for item in participant_list]
        if len(participant_ids) != len(set(participant_ids)):
            raise ConversationError("duplicate conversation participant")
        root_path = _root_path(root)
        path = root_path / f"{session}.jsonl"
        if path.exists():
            raise ConversationError("conversation session already exists")
        header = {"record": "conversation_room", "schema_version": SCHEMA_VERSION,
                  "session_id": session, "project_id": project,
                  "project_root_sha256": _sha256(str(project_path)),
                  "initiator_host": host, "initiator_agent": agent, "mode": mode,
                  "participants": participant_list,
                  "created_at_ms": int(time.time() * 1000), "cursor": 0}
        journal = cls(path, header, allow_empty=True)
        journal._write_new()
        journal.append("session_created", "system", "summon", {
            "project_id": project, "initiator_host": host, "initiator_agent": agent,
            "mode": mode, "participants": participant_list,
        }, expected_cursor=0, event_id="session-created")
        return journal

    @classmethod
    def open(cls, root: str | os.PathLike[str], session_id: str) -> "ConversationJournal":
        session = _safe_id(session_id, "session id")
        root_path = _root_path(root, create=False)
        path = root_path / f"{session}.jsonl"
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ConversationError("conversation journal may not be a symlink")
        if not path.is_file():
            raise ConversationError("conversation session not found")
        try:
            lines = None
            for attempt in range(3):
                before = path.stat()
                if before.st_size > MAX_JOURNAL_BYTES:
                    raise ConversationError("conversation journal is too large")
                candidate = path.read_bytes().splitlines()
                after = path.stat()
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    lines = candidate
                    break
                if attempt < 2:
                    # A live turn may append exactly one fsynced line between
                    # the two fingerprints. Retry briefly, but never accept a
                    # persistently changing or replaced journal.
                    time.sleep(0.005)
            if lines is None:
                raise ConversationError("conversation journal changed during read")
        except OSError as exc:
            raise ConversationError("conversation session cannot be read") from exc
        if not lines or len(lines) > MAX_EVENTS + 1:
            raise ConversationError("conversation journal is empty or too large")
        if any(len(line) > MAX_EVENT_BYTES for line in lines):
            raise ConversationError("conversation event is too large")
        try:
            header = json.loads(lines[0].decode("utf-8"))
            records = [json.loads(line.decode("utf-8")) for line in lines[1:]]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationError("conversation journal is malformed") from exc
        if header.get("session_id") != session:
            raise ConversationError("conversation filename does not match session id")
        return cls(path, header, records)

    @staticmethod
    def _validate_header(header: Mapping[str, Any]) -> None:
        if header.get("record") != "conversation_room" or header.get("schema_version") != SCHEMA_VERSION:
            raise ConversationError("unsupported conversation room schema")
        _safe_id(header.get("session_id"), "session id")
        _safe_id(header.get("project_id"), "project id")
        if not _SHA256_RE.fullmatch(str(header.get("project_root_sha256", ""))):
            raise ConversationError("invalid project root digest")
        _safe_display(header.get("initiator_host"), "initiator host", required=True)
        _safe_id(header.get("initiator_agent"), "initiator agent")
        if header.get("mode") not in _MODES:
            raise ConversationError("invalid conversation mode")
        participants = header.get("participants", [])
        if not isinstance(participants, list) or len(participants) > MAX_PARTICIPANTS:
            raise ConversationError("invalid participants")
        for item in participants:
            _project_participant(item)
        if not isinstance(header.get("created_at_ms"), int) or isinstance(header.get("created_at_ms"), bool):
            raise ConversationError("invalid creation timestamp")
        if header.get("cursor") != 0:
            raise ConversationError("invalid room cursor")

    @staticmethod
    def _validate_records(records: list[Mapping[str, Any]], header: Mapping[str, Any], *, allow_empty: bool = False) -> None:
        session_id = header["session_id"]
        expected = 1
        prior_generation = 1
        seen_ids: set[str] = set()
        if not records and allow_empty:
            return
        if not records or records[0].get("event") != "session_created":
            raise ConversationError("conversation journal has no session_created event")
        for record in records:
            if record.get("record") != "conversation_event" or record.get("schema_version") != SCHEMA_VERSION:
                raise ConversationError("invalid conversation event record")
            if record.get("cursor") != expected:
                raise ConversationError("conversation cursor is not contiguous")
            if record.get("session_id") != session_id:
                raise ConversationError("conversation event belongs to another room")
            generation = record.get("generation")
            if (not isinstance(generation, int) or isinstance(generation, bool)
                    or generation < prior_generation):
                raise ConversationError("conversation generation is invalid")
            prior_generation = generation
            event_id = _safe_id(record.get("event_id"), "event id")
            if event_id in seen_ids:
                raise ConversationError("duplicate conversation event id")
            seen_ids.add(event_id)
            if record.get("event") not in _EVENTS:
                raise ConversationError("unsupported conversation event")
            if record.get("actor_kind") not in _ACTOR_KINDS:
                raise ConversationError("invalid actor kind")
            _safe_id(record.get("actor_id"), "actor id")
            if not _SHA256_RE.fullmatch(str(record.get("payload_sha256", ""))):
                raise ConversationError("invalid payload digest")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ConversationError("invalid conversation payload")
            if _sha256(payload) != record.get("payload_sha256"):
                raise ConversationError("conversation payload digest mismatch")
            if record["event"] == "session_created":
                expected_header = {"project_id": header["project_id"],
                                   "initiator_host": header["initiator_host"],
                                   "initiator_agent": header["initiator_agent"],
                                   "mode": header["mode"]}
                if any(payload.get(key) != value for key, value in expected_header.items()):
                    raise ConversationError("session_created payload disagrees with room header")
            _public_payload(str(record["event"]), payload)
            expected += 1

    def _write_new(self) -> None:
        data = (json.dumps(self._header, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError as exc:
            raise ConversationError("conversation session already exists") from exc
        except OSError as exc:
            raise ConversationError("conversation journal cannot be created") from exc

    @contextmanager
    def _append_lock(self):
        """Claim a short-lived cross-process append lock without a dependency.

        A crashed writer must not brick a room forever.  The marker binds a
        stale-lock reclaim to the original PID/token and an unchanged inode
        timestamp, so a late owner cannot delete a replacement lock.
        """
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + 2.0
        fd = None
        token = secrets.token_hex(16)

        def pid_alive(pid: object) -> bool:
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                return False
            try:
                os.kill(pid, 0)
            except PermissionError:
                return True
            except (OSError, ProcessLookupError):
                return False
            return True

        try:
            while fd is None:
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    marker = json.dumps({"pid": os.getpid(), "token": token,
                                         "started_at": time.time()},
                                        sort_keys=True, separators=(",", ":")).encode("ascii")
                    os.write(fd, marker)
                    try:
                        os.fsync(fd)
                    except OSError:
                        pass
                except FileExistsError:
                    try:
                        observed = lock.stat()
                        if time.time() - observed.st_mtime > APPEND_LOCK_STALE_SECONDS:
                            raw = lock.read_bytes()
                            marker = json.loads(raw.decode("ascii"))
                            owner_pid = marker.get("pid") if isinstance(marker, dict) else None
                            owner_token = marker.get("token") if isinstance(marker, dict) else None
                            if (isinstance(owner_token, str) and len(owner_token) == 32
                                    and not pid_alive(owner_pid)
                                    and lock.stat().st_mtime_ns == observed.st_mtime_ns):
                                try:
                                    lock.unlink()
                                    continue
                                except FileNotFoundError:
                                    continue
                    except (OSError, UnicodeError, ValueError, TypeError):
                        pass
                    if time.monotonic() >= deadline:
                        raise ConversationError("conversation journal is busy")
                    time.sleep(0.01)
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                finally:
                    try:
                        os.unlink(lock)
                    except FileNotFoundError:
                        pass

    def _refresh_records(self) -> None:
        # ``create`` writes the header before the first session_created event.
        # During that one initialization window the journal is intentionally
        # header-only; do not reject it as a forged/truncated room before the
        # append that establishes the required first event.
        try:
            if self.path.is_symlink() or (hasattr(self.path, "is_junction") and self.path.is_junction()):
                raise ConversationError("conversation journal may not be a symlink")
            stat_before = self.path.stat()
            if stat_before.st_size > MAX_JOURNAL_BYTES:
                raise ConversationError("conversation journal is too large")
            if not self._records:
                with open(self.path, "rb") as fh:
                    if fh.read(2).count(b"\n") <= 1:
                        return
        except OSError as exc:
            raise ConversationError("conversation journal cannot be refreshed") from exc
        current = ConversationJournal.open(self.path.parent, self._header["session_id"])
        if current._header != self._header:
            raise ConversationError("conversation room header changed")
        self._records = current._records

    @property
    def room(self) -> ConversationRoom:
        if self._records:
            self._refresh_records()
        h = self._header
        return ConversationRoom(
            session_id=h["session_id"], project_id=h["project_id"],
            project_root_sha256=h["project_root_sha256"],
            initiator_host=h["initiator_host"], initiator_agent=h["initiator_agent"],
            mode=h["mode"], participants=tuple(dict(x) for x in h["participants"]),
            created_at_ms=h["created_at_ms"], cursor=len(self._records),
        )

    def append(self, event: str, actor_kind: str, actor_id: str,
               payload: Mapping[str, Any], *, expected_cursor: int | None = None,
               event_id: str | None = None, generation: int | None = None) -> dict[str, Any]:
        if event not in _EVENTS:
            raise ConversationError("unsupported conversation event")
        if actor_kind not in _ACTOR_KINDS:
            raise ConversationError("invalid actor kind")
        actor = _safe_id(actor_id, "actor id")
        if (generation is not None
                and (not isinstance(generation, int) or isinstance(generation, bool) or generation < 1)):
            raise ConversationError("invalid event generation")
        item = _snapshot(dict(payload))
        # Control facts may be *displayed* when a deliberate kernel projects
        # them into a room, but this generic conversation writer is never an
        # authority bridge.  It therefore rejects them outright; a future
        # integration must import a verified kernel projection through a
        # dedicated, receipt-bound seam rather than letting chat append ballots.
        if event in _CONTROL_EVENTS:
            raise ConversationError("control events must come from a verified kernel projection")
        public = _public_payload(event, item)
        eid = _safe_id(event_id or uuid.uuid4().hex, "event id")
        with self._append_lock():
            self._refresh_records()
            if generation is None:
                generation = max(
                    [int(record.get("generation", 1)) for record in self._records
                     if isinstance(record.get("generation", 1), int)] or [1]
                )
            if (not isinstance(generation, int) or isinstance(generation, bool)
                    or generation < 1):
                raise ConversationError("invalid event generation")
            if any(record.get("event_id") == eid for record in self._records):
                existing = next(record for record in self._records if record.get("event_id") == eid)
                if (existing.get("payload_sha256") != _sha256(item)
                        or existing.get("event") != event
                        or existing.get("actor_kind") != actor_kind
                        or existing.get("actor_id") != actor):
                    raise ConversationError("event id was reused with different payload")
                return self._public_record(existing)
            if expected_cursor is not None and expected_cursor != len(self._records):
                raise ConversationError("conversation cursor conflict")
            if self._records and generation < int(self._records[-1].get("generation", 1)):
                raise ConversationError("conversation generation regressed")
            if len(self._records) >= MAX_EVENTS:
                raise ConversationError("conversation room event limit reached")
            record = {"record": "conversation_event", "schema_version": SCHEMA_VERSION,
                      "session_id": self.room.session_id, "cursor": len(self._records) + 1,
                      "generation": generation, "event_id": eid, "event": event,
                      "actor_kind": actor_kind, "actor_id": actor,
                      "payload_sha256": _sha256(item), "payload": item}
            encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_EVENT_BYTES:
                raise ConversationError("conversation event is too large")
            try:
                with open(self.path, "ab") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise ConversationError("conversation event cannot be appended") from exc
            self._records.append(record)
            return self._public_record(record)

    @staticmethod
    def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        payload = _public_payload(str(record["event"]), record["payload"])
        return {"session_id": record["session_id"], "cursor": record["cursor"],
                "generation": record["generation"], "event_id": record["event_id"],
                "event": record["event"], "actor_kind": record["actor_kind"],
                "actor_id": record["actor_id"], "payload_sha256": record["payload_sha256"],
                "payload": payload}

    def events(self, *, native: bool = False, after_cursor: int = 0) -> list[dict[str, Any]]:
        if not isinstance(after_cursor, int) or isinstance(after_cursor, bool) or after_cursor < 0:
            raise ConversationError("invalid event cursor")
        if self._records:
            self._refresh_records()
        if native:
            return [copy.deepcopy(record) for record in self._records if record["cursor"] > after_cursor]
        return [self._public_record(record) for record in self._records if record["cursor"] > after_cursor]

    def as_dict(self, *, native: bool = False, after_cursor: int = 0) -> dict[str, Any]:
        return {"room": self.room.as_dict(native=native),
                "events": self.events(native=native, after_cursor=after_cursor),
                "cursor": len(self._records), "redaction": "native-local" if native else "public-redacted"}

    def append_human_message(self, text: str, *, actor_id: str = "human",
                             message_id: str | None = None, expected_cursor: int | None = None) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ConversationError("human message must be text")
        mid = message_id or uuid.uuid4().hex
        return self.append("human_message", "human", actor_id,
                           {"message_id": mid, "text": text},
                           expected_cursor=expected_cursor, event_id=f"message-{mid}")

    def append_agent_message(self, sender: str, recipient: str, text: str, *,
                             message_id: str | None = None,
                             expected_cursor: int | None = None) -> dict[str, Any]:
        """Append a durable context-only message addressed to one room member.

        This is a delivery record, not a control channel: recipient text cannot
        approve, cancel, vote, or launch anything.  Native text stays in the
        private journal; public/browser projections expose only a redacted
        preview and digest.
        """
        sender_id = _safe_id(sender, "message sender")
        recipient_id = _safe_id(recipient, "message recipient")
        participant_ids = {item["agent"] for item in self._header.get("participants", [])}
        allowed = participant_ids | {"human"}
        if sender_id not in participant_ids:
            raise ConversationError("message sender is not a room participant")
        if recipient_id not in allowed:
            raise ConversationError("message recipient is not a room participant")
        if not isinstance(text, str) or not text.strip():
            raise ConversationError("agent message must be non-empty text")
        mid = message_id or uuid.uuid4().hex
        return self.append("agent_message", "agent", sender_id,
                           {"message_id": mid, "sender": sender_id,
                            "recipient": recipient_id, "text": text},
                           expected_cursor=expected_cursor,
                           event_id=f"agent-message-{mid}")

    def agent_inbox(self, recipient: str, *, after_cursor: int = 0,
                    native: bool = True) -> list[dict[str, Any]]:
        """Read addressed messages plus operator context for one participant.

        ``native=True`` is intentionally local-only for an agent caller; HTTP
        and browser surfaces use the normal public-redacted ``events`` path.
        """
        recipient_id = _safe_id(recipient, "message recipient")
        records = self.events(native=native, after_cursor=after_cursor)
        result = []
        for record in records:
            event = record.get("event")
            payload = record.get("payload", {})
            if event == "agent_message" and payload.get("recipient") == recipient_id:
                result.append(record)
            elif event == "human_message" and recipient_id in {
                    item.get("agent") for item in self._header.get("participants", [])}:
                result.append(record)
        return result

    def append_council_round(self, round_number: int,
                             positions: Iterable[Mapping[str, Any]], *,
                             cross_exams: Iterable[Mapping[str, Any]] = (),
                             synthesis: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Append one bounded, provider-agnostic council round.

        The caller supplies already-produced summaries; this method does not
        dispatch members or interpret a recommendation as a ballot. Human
        messages can be appended between calls, so a UI can let the operator
        chime in without changing council authority.
        """
        if self.room.mode != "council":
            raise ConversationError("council rounds require a council room")
        if not isinstance(round_number, int) or isinstance(round_number, bool) or not 1 <= round_number <= 10:
            raise ConversationError("invalid council round")
        position_list = [_snapshot(dict(item)) for item in
                         _bounded_items(positions, MAX_PARTICIPANTS, "council positions")]
        if not position_list or len(position_list) > MAX_PARTICIPANTS:
            raise ConversationError("council positions are missing or too many")
        results = [self.append("council_round_started", "system", "summon",
                               {"round": round_number},
                               event_id=f"council-round-{round_number}")]
        for item in position_list:
            participant = _safe_id(item.get("participant"), "participant")
            payload: dict[str, Any] = {"round": round_number, "participant": participant,
                                       "summary": _bounded_text(item.get("summary"), "position summary")}
            for key in ("role", "name", "version"):
                if item.get(key) is not None:
                    payload[key] = _safe_display(item[key], key)
            if item.get("evidence_sha256") is not None:
                payload["evidence_sha256"] = item["evidence_sha256"]
            results.append(self.append("position_submitted", "agent", participant, payload,
                                       event_id=f"council-round-{round_number}-position-{participant}"))
        for index, item in enumerate(_bounded_items(cross_exams, MAX_CROSS_EXAMS, "cross-exams")):
            item = _snapshot(dict(item))
            asker = _safe_id(item.get("asker"), "asker")
            target = _safe_id(item.get("target"), "target")
            results.append(self.append("cross_exam", "agent", asker,
                                       {"round": round_number, "asker": asker, "target": target,
                                        "summary": _bounded_text(item.get("summary"), "cross-exam summary")},
                                       event_id=f"council-round-{round_number}-cross-{index}"))
        if synthesis is not None:
            synthesis = _snapshot(dict(synthesis))
            payload = {"round": round_number,
                       "summary": _bounded_text(synthesis.get("summary"), "chair synthesis")}
            if synthesis.get("recommended_option") is not None:
                payload["recommended_option"] = _safe_id(synthesis["recommended_option"], "recommended option")
            if synthesis.get("evidence_sha256") is not None:
                payload["evidence_sha256"] = synthesis["evidence_sha256"]
            results.append(self.append("chair_synthesis", "agent", "chair", payload,
                                       event_id=f"council-round-{round_number}-synthesis"))
        return results


def council_recommendation(*, session_id: str, source_cursor: int,
                           option_ids: Iterable[str], recommended_option: str | None,
                           tradeoffs: Iterable[str] = (), dissent: Iterable[str] = ()) -> dict[str, Any]:
    """Create a bounded context-only artifact from a council synthesis."""
    session = _safe_id(session_id, "session id")
    if not isinstance(source_cursor, int) or isinstance(source_cursor, bool) or source_cursor < 1:
        raise ConversationError("invalid recommendation cursor")
    options = [_safe_id(item, "option id") for item in
               _bounded_items(option_ids, MAX_RECOMMENDATION_ITEMS, "option ids")]
    if len(options) < 2 or len(options) != len(set(options)):
        raise ConversationError("a recommendation needs at least two unique options")
    if recommended_option is not None:
        recommended_option = _safe_id(recommended_option, "recommended option")
        if recommended_option not in options:
            raise ConversationError("recommended option is not in the option set")
    tradeoff_list = [_redacted_bounded_text(item, "trade-off") for item in
                     _bounded_items(tradeoffs, MAX_RECOMMENDATION_ITEMS, "trade-offs")]
    dissent_list = [_redacted_bounded_text(item, "dissent") for item in
                    _bounded_items(dissent, MAX_RECOMMENDATION_ITEMS, "dissent")]
    artifact = {"kind": "council_recommendation", "authority": "context-only",
                "session_id": session, "source_cursor": source_cursor,
                "option_ids": options, "recommended_option": recommended_option,
                "tradeoffs": tradeoff_list, "dissent": dissent_list}
    artifact["artifact_sha256"] = _sha256(artifact)
    return artifact


def promote_recommendation(artifact: Mapping[str, Any], *, human_confirmed: bool,
                           decision_id: str, seat_ids: Iterable[str],
                           option_ids: Iterable[str], quorum: int | str,
                           rounds: int, max_attempts: int,
                           deadline_unix_ms: int,
                           require_human_approval: bool) -> dict[str, Any]:
    """Return an explicit deliberate setup; never create a ballot or launch."""
    if human_confirmed is not True:
        raise ConversationError("council promotion requires explicit human confirmation")
    source = _snapshot(dict(artifact))
    if source.get("kind") != "council_recommendation" or source.get("authority") != "context-only":
        raise ConversationError("invalid council recommendation")
    digest = source.get("artifact_sha256")
    copy_without_digest = dict(source)
    copy_without_digest.pop("artifact_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(copy_without_digest):
        raise ConversationError("council recommendation digest mismatch")
    decision = _safe_id(decision_id, "decision id")
    seats = [_safe_id(item, "seat id") for item in
             _bounded_items(seat_ids, 10, "seat ids")]
    options = [_safe_id(item, "option id") for item in
               _bounded_items(option_ids, MAX_RECOMMENDATION_ITEMS, "option ids")]
    if len(seats) < 2 or len(seats) > 10 or len(seats) != len(set(seats)):
        raise ConversationError("deliberation needs 2-10 unique seats")
    if len(options) < 2 or len(options) != len(set(options)):
        raise ConversationError("deliberation needs at least two unique options")
    if not isinstance(quorum, (int, str)) or isinstance(quorum, bool):
        raise ConversationError("invalid deliberation quorum")
    if isinstance(quorum, int) and not 1 <= quorum <= len(seats):
        raise ConversationError("invalid deliberation quorum")
    if isinstance(quorum, str) and quorum != "all" and not re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", quorum):
        raise ConversationError("invalid deliberation quorum")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
        raise ConversationError("invalid deliberation rounds")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 1024:
        raise ConversationError("invalid deliberation attempt budget")
    if (not isinstance(deadline_unix_ms, int) or isinstance(deadline_unix_ms, bool)
            or deadline_unix_ms <= 0):
        raise ConversationError("invalid deliberation deadline")
    if not isinstance(require_human_approval, bool):
        raise ConversationError("human approval policy must be boolean")
    return {"mode": "deliberate", "authority": "human-confirmed-setup",
            "source_recommendation_sha256": digest, "source_session_id": source["session_id"],
            "decision_id": decision, "seat_ids": seats, "option_ids": options,
            "quorum": quorum, "rounds": rounds, "max_attempts": max_attempts,
            "deadline_unix_ms": deadline_unix_ms,
            "require_human_approval": require_human_approval,
            "provider_calls": 0}


def continuation_decision(parent_session_id: str, existing: Mapping[str, Any],
                          requested: Mapping[str, Any]) -> ContinuationDecision:
    """Decide whether a provider session can continue or must fork."""
    parent = _safe_id(parent_session_id, "parent session id")
    left = _snapshot(dict(existing))
    right = _snapshot(dict(requested))
    fields = ("provider", "profile_digest", "profile_revision_sha256",
              "account_evidence_sha256", "model_target", "model_served_sha256",
              "prompt_contract_sha256", "permission", "owner_generation")
    digest_fields = {"profile_digest", "profile_revision_sha256", "account_evidence_sha256",
                     "model_served_sha256", "prompt_contract_sha256"}
    display_fields = {"provider", "model_target", "permission"}
    mismatches = []
    for label, value in (("existing", left), ("requested", right)):
        for key in value:
            if (key not in fields and
                    any(token in str(key).lower() for token in ("account", "profile", "credential"))):
                key_text = str(key)
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_text):
                    safe_key = key_text
                else:
                    safe_key = "key-" + _sha256(key_text)[:16]
                mismatches.append(f"unsupported_{label}_{safe_key}")
    for field in fields:
        if field not in left or field not in right:
            mismatches.append(f"missing_{field}")
        elif field in digest_fields and (not isinstance(left[field], str)
                                         or not _SHA256_RE.fullmatch(left[field])
                                         or not isinstance(right[field], str)
                                         or not _SHA256_RE.fullmatch(right[field])):
            mismatches.append(f"invalid_{field}")
        elif field in display_fields and (not isinstance(left[field], str)
                                          or not _DISPLAY_RE.fullmatch(left[field])
                                          or not isinstance(right[field], str)
                                          or not _DISPLAY_RE.fullmatch(right[field])):
            mismatches.append(f"invalid_{field}")
        elif field == "owner_generation":
            if (not isinstance(left[field], int) or isinstance(left[field], bool) or left[field] < 1
                    or not isinstance(right[field], int) or isinstance(right[field], bool)
                    or right[field] < 1):
                mismatches.append("invalid_owner_generation")
            elif right[field] < left[field]:
                # Owner generations are fencing evidence, not a provider
                # session identity. A normal next owner may continue a
                # completed provider session, but a stale process must never
                # present an older generation as current.
                mismatches.append("owner_generation_regressed")
        elif left[field] != right[field]:
            mismatches.append(field)
    if mismatches:
        reason = "incompatible:" + ",".join(mismatches[:32])
        return ContinuationDecision("fork", reason[:MAX_REASON_CHARS], parent)
    return ContinuationDecision("continue", "compatible_provider_session", parent)


def group_rooms(rooms: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group public room summaries by project, then initiating host/agent."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    count = 0
    for raw in rooms:
        count += 1
        if count > MAX_ROOMS:
            raise ConversationError("too many conversation rooms")
        item = _snapshot(dict(raw))
        project = _safe_id(item.get("project_id"), "project id")
        root_digest = item.get("project_root_sha256")
        if not isinstance(root_digest, str) or not _SHA256_RE.fullmatch(root_digest):
            raise ConversationError("invalid project root digest")
        host = _safe_display(item.get("initiator_host"), "initiator host", required=True)
        agent = _safe_id(item.get("initiator_agent"), "initiator agent")
        mode = item.get("mode")
        if mode not in _MODES:
            raise ConversationError("invalid conversation mode")
        cursor = item.get("cursor", 0)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ConversationError("invalid room cursor")
        key = f"{host}/{agent}"
        project_key = f"{project}@{root_digest}"
        public = {"session_id": _safe_id(item.get("session_id"), "session id"),
                  "project_id": project, "project_root_sha256": root_digest,
                  "initiator_host": host,
                  "initiator_agent": agent,
                  "mode": mode, "cursor": cursor}
        grouped.setdefault(project_key, {}).setdefault(key, []).append(public)
    return grouped


def list_rooms(root: str | os.PathLike[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Read all valid rooms and return the safe project/initiator index."""
    root_path = _root_path(root, create=False)
    rooms: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(root_path.glob("*.jsonl")):
        scanned += 1
        if scanned > MAX_ROOM_FILES:
            raise ConversationError("too many conversation journal files")
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            continue
        try:
            journal = ConversationJournal.open(root_path, path.stem)
        except ConversationError:
            # A malformed room is not silently represented as a healthy room.
            # Callers can inspect it by opening the exact id and receive the
            # structured error; the index remains safe and bounded.
            continue
        rooms.append(journal.room.as_dict())
        if len(rooms) > MAX_ROOMS:
            raise ConversationError("too many conversation rooms")
    return group_rooms(rooms)


def run_command(args: Any) -> int:
    """CLI handler for local rooms and explicitly requested live agent turns.

    Room reads, human context, and addressed agent messages remain provider-inert.
    ``turn`` and ``cancel`` are the only operations that cross into the runtime
    seam; the runtime owns the durable-before-provider fence and continuation/fork
    decision.  Message delivery is context-only and cannot mutate authority.
    """
    action = getattr(args, "chat_action", None)
    root = getattr(args, "conversation_dir", None) or os.path.join(
        getattr(args, "cwd", None) or os.getcwd(), ".agents", "conversations")
    # Resolve this before the existing-room branch too.  Browser handoff is
    # allowed for an already-created room, and the previous lazy assignment
    # raised UnboundLocalError when --cwd was omitted on that path.
    project_root = (getattr(args, "chat_project_root", None)
                    or getattr(args, "cwd", None) or os.getcwd())
    try:
        if action == "open":
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            try:
                journal = ConversationJournal.open(root, session)
                result = {"status": "opened", **journal.as_dict()}
            except ConversationError as exc:
                if not any(token in str(exc) for token in ("not found", "root does not exist")):
                    raise
                raw_participants = getattr(args, "chat_participants", None)
                participants = []
                if raw_participants:
                    if not isinstance(raw_participants, str):
                        raise ConversationError("chat participants are invalid")
                    names = [item.strip() for item in raw_participants.split(",") if item.strip()]
                    if not names:
                        raise ConversationError("chat participants are invalid")
                    participants = [{"agent": _safe_id(item, "participant agent")} for item in names]
                journal = ConversationJournal.create(
                    root, session_id=session,
                    project_id=getattr(args, "chat_project_id", None) or "default",
                    project_root=project_root,
                    initiator_host=getattr(args, "chat_initiator_host", None) or "terminal",
                    initiator_agent=getattr(args, "chat_initiator_agent", None) or "human",
                    mode=getattr(args, "chat_mode", None) or "chat",
                    participants=participants,
                )
                result = {"status": "created", **journal.as_dict()}
            browser_mode = getattr(args, "chat_browser", None)
            if browser_mode:
                try:
                    from _conversation_browser import ensure_surface, open_url
                    surface = ensure_surface(
                        root,
                        cwd=getattr(args, "cwd", None) or project_root,
                        agents_dir=getattr(args, "agents_dir", None),
                    )
                    result["browser"] = open_url(str(surface["url"]), mode=browser_mode)
                except Exception as exc:  # bounded public CLI error, no provider path
                    from _conversation_browser import ConversationBrowserError
                    if isinstance(exc, ConversationBrowserError):
                        raise ConversationError("conversation browser handoff refused") from exc
                    raise
        elif action == "post":
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            journal = ConversationJournal.open(root, session)
            event = journal.append_human_message(
                getattr(args, "chat_message", None) or "",
                actor_id=getattr(args, "chat_initiator_agent", None) or "human",
            )
            result = {"status": "posted", "room": journal.room.as_dict(), "event": event}
        elif action == "show":
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            result = {"status": "ok", **ConversationJournal.open(root, session).as_dict()}
        elif action == "list":
            result = {"status": "ok", "rooms": list_rooms(root)}
        elif action == "turn":
            from _conversation_runtime import ConversationRuntime
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            participant = _safe_id(getattr(args, "chat_participant", None), "participant")
            prompt = getattr(args, "chat_message", None)
            if not isinstance(prompt, str) or not prompt.strip():
                raise ConversationError("chat turn requires --message")
            runtime = ConversationRuntime(
                root,
                cwd=getattr(args, "cwd", None) or os.getcwd(),
                agents_dir=getattr(args, "agents_dir", None),
                timeout_ms=int(getattr(args, "chat_timeout", None) or getattr(args, "timeout", 600_000)),
                strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)
                                       or getattr(args, "agents_dir", None)),
            )
            result = runtime.start_turn(
                session, participant, prompt,
                timeout_ms=(int(getattr(args, "chat_timeout", None))
                            if getattr(args, "chat_timeout", None) is not None else None),
                # A CLI process must stay alive until the worker has journaled
                # its finish event.  The browser uses the runtime object
                # directly for asynchronous turns; a detached CLI thread would
                # otherwise die with the parent process.
                wait=True,
            )
        elif action == "cancel":
            from _conversation_runtime import ConversationRuntime
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            participant = _safe_id(getattr(args, "chat_participant", None), "participant")
            runtime = ConversationRuntime(
                root,
                cwd=getattr(args, "cwd", None) or os.getcwd(),
                agents_dir=getattr(args, "agents_dir", None),
                timeout_ms=int(getattr(args, "chat_timeout", None) or getattr(args, "timeout", 600_000)),
                strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)
                                       or getattr(args, "agents_dir", None)),
            )
            result = runtime.cancel_turn(session, participant)
        elif action == "message":
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            sender = _safe_id(getattr(args, "chat_participant", None), "message sender")
            recipient = _safe_id(getattr(args, "chat_to", None), "message recipient")
            message = getattr(args, "chat_message", None)
            if not isinstance(message, str) or not message.strip():
                raise ConversationError("chat message requires --message")
            journal = ConversationJournal.open(root, session)
            event = journal.append_agent_message(sender, recipient, message)
            result = {"status": "sent", "room": journal.room.as_dict(), "event": event}
        elif action == "inbox":
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            recipient = _safe_id(getattr(args, "chat_participant", None), "message recipient")
            after = getattr(args, "chat_after", 0)
            journal = ConversationJournal.open(root, session)
            events = journal.agent_inbox(recipient, after_cursor=after, native=True)
            result = {"status": "ok", "session_id": session,
                      "recipient": recipient, "after_cursor": after,
                      "events": events, "cursor": journal.room.cursor,
                      "delivery": "local-native"}
        elif action == "recover":
            from _conversation_runtime import ConversationRuntime
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            participant = _safe_id(getattr(args, "chat_participant", None), "participant")
            runtime = ConversationRuntime(
                root,
                cwd=getattr(args, "cwd", None) or os.getcwd(),
                agents_dir=getattr(args, "agents_dir", None),
                timeout_ms=int(getattr(args, "chat_timeout", None) or getattr(args, "timeout", 600_000)),
                strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)
                                       or getattr(args, "agents_dir", None)),
            )
            result = runtime.recover_turn(
                session, participant, confirm=bool(getattr(args, "chat_confirm", False)))
        elif action == "fork":
            from _conversation_runtime import ConversationRuntime
            session = _safe_id(getattr(args, "chat_session", None), "session id")
            participant = _safe_id(getattr(args, "chat_participant", None), "participant")
            runtime = ConversationRuntime(
                root,
                cwd=getattr(args, "cwd", None) or os.getcwd(),
                agents_dir=getattr(args, "agents_dir", None),
                timeout_ms=int(getattr(args, "chat_timeout", None) or getattr(args, "timeout", 600_000)),
                strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)
                                       or getattr(args, "agents_dir", None)),
            )
            result = runtime.fork_turn(
                session, participant, getattr(args, "chat_message", None) or "",
                reason=getattr(args, "chat_reason", None) or "manual fork")
        else:
            raise ConversationError("chat action is required")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ConversationError, OSError) as exc:
        # Keep CLI management failures bounded; do not echo filesystem paths or
        # user message bodies into a public error envelope.
        print(json.dumps({"status": "blocked", "error_kind": "conversation_invalid",
                          "error": str(exc)[:240]}, ensure_ascii=False))
        return 1


__all__ = ["ConversationError", "ConversationJournal", "ConversationRoom",
           "ContinuationDecision", "continuation_decision", "group_rooms",
           "council_recommendation", "promote_recommendation",
           "list_rooms", "run_command", "MAX_EVENTS", "MAX_EVENT_BYTES"]
