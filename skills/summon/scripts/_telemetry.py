"""Opt-in local diagnostics and explicit GitHub bug-report generation.

Telemetry is deliberately a local spool, not a phone-home service.  A user must
enable it (``summon telemetry enable`` or ``SUMMON_TELEMETRY=1``) before a
dispatch writes an event.  Events are a bounded, allow-listed projection of a
dispatch envelope: prompts, results, raw output, absolute paths, credentials,
and environment values never enter the spool.

``bug-report`` turns one event or an envelope supplied with ``--from`` into a
Markdown file.  GitHub submission is a separate explicit action and delegates
authentication to the user's ``gh`` installation; this module never reads a
token or makes an HTTP request itself.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_EVENT_BYTES = 16 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_EVENT_FILE_BYTES = 2 * 1024 * 1024
_MAX_TEXT = 600
_MAX_WARNINGS = 8
_MAX_EVENTS_SCAN = 10_000
_TRUE = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "off", "disable", "disabled"}
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+")
_REPORT_HEADER = "# Summon diagnostic report"
_FAILURE_CLASSES = {
    "success", "blocked", "timeout", "cancelled", "authentication", "quota",
    "transport", "missing_cli", "invalid_input", "report_contract", "worktree",
    "backend", "dispatch", "partial", "unknown",
}


def _config_path() -> Path:
    override = os.environ.get("SUMMON_TELEMETRY_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".agents" / "summon-telemetry.json"


def _events_path() -> Path:
    override = os.environ.get("SUMMON_TELEMETRY_FILE")
    return Path(override).expanduser() if override else Path.home() / ".agents" / "summon-telemetry.jsonl"


def _reports_dir() -> Path:
    override = os.environ.get("SUMMON_REPORTS_DIR")
    return Path(override).expanduser() if override else Path.home() / ".agents" / "summon-reports"


def _parse_bool(value: object) -> bool | None:
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _read_config() -> dict:
    path = _config_path()
    try:
        size = path.stat().st_size
        if size > _MAX_CONFIG_BYTES:
            return {}
        with path.open("rb") as fh:
            raw = fh.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def enabled() -> bool:
    """Return the effective local telemetry setting.

    The environment override is intentionally process-local and wins over the
    persisted setting, so a caller can disable collection for one sensitive
    invocation without rewriting the user's preference.
    """
    override = os.environ.get("SUMMON_TELEMETRY")
    if override is not None:
        parsed = _parse_bool(override)
        return bool(parsed) if parsed is not None else False
    return bool(_read_config().get("enabled") is True)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".summon-telemetry-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(value)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def set_enabled(value: bool) -> dict:
    """Persist the user's opt-in choice without storing a path or identity."""
    data = {"schema": SCHEMA_VERSION, "enabled": bool(value)}
    _atomic_write(_config_path(), json.dumps(data, sort_keys=True).encode("utf-8"))
    return status()


def _display_path(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
        return "~" + os.sep + str(relative)
    except (OSError, ValueError):
        return str(path)


def status() -> dict:
    path = _events_path()
    count = 0
    size = 0
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _MAX_SOURCE_BYTES:
                fh.seek(size - _MAX_SOURCE_BYTES)
            for _ in fh:
                count += 1
                if count >= _MAX_EVENTS_SCAN:
                    break
    except OSError:
        pass
    return {"schema": SCHEMA_VERSION, "enabled": enabled(),
            "event_file": _display_path(path), "event_count": count,
            "event_bytes": size}


def clear_events() -> dict:
    """Delete only the local telemetry spool; persisted opt-in remains unchanged."""
    path = _events_path()
    removed = False
    with _spool_lock(path) as locked:
        if locked:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
    return {**status(), "cleared": removed}


def _clean_text(value: object, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    # Secret-shaped material is removed before any other projection.  The
    # patterns are intentionally conservative; omitting a fragment is safer
    # than publishing a credential-shaped token in a report.
    text = re.sub(r"(?i)(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic|token)?\s*[^\s,;]+",
                  "authorization: <redacted>", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "<redacted>", text)
    # Handle quoted and whitespace-containing assignments without attempting
    # to preserve a value that might be a credential or private prose.
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret|prompt|query|input|result|output|content|message)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\r\n,;]+)",
                  r"\1=<redacted>", text)
    text = re.sub(r"(?i)\b(?:sk|rk|pk|ark)-[A-Za-z0-9_-]{8,}", "<redacted>", text)
    text = re.sub(r"(?i)\b(?:github_pat_[A-Za-z0-9_]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
                  r"npm_[A-Za-z0-9_]{8,}|pypi-[A-Za-z0-9_-]{8,}|"
                  r"xox[baprs]-[A-Za-z0-9-]{8,}|AIza[0-9A-Za-z_-]{20,})\b", "<redacted>", text)
    text = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "<redacted>", text)
    # JWTs are three dot-separated base64url segments and are commonly echoed
    # without a label.  Keep no part of the token in a report.
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                  "<redacted>", text)
    text = re.sub(r"(?i)https?://[^\s\"'<>]+", "<url>", text)
    # Absolute paths are useful in a private envelope but are not appropriate
    # telemetry or public issue content.  Keep the diagnostic sentence, remove
    # the machine-specific location.
    text = re.sub(r"(?i)(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\{1,2})[^<>\"'\r\n,;]+", "<path>", text)
    text = re.sub(r"(?<![A-Za-z0-9_])/(?!/)[^<>\"'\r\n,;]+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:max(0, limit - 14)] + " [truncated]"
    return text or None


def _safe_id(value: object, limit: int = 160) -> str | None:
    text = _clean_text(value, limit)
    if not text:
        return None
    # IDs and model names are metadata, not free-form output.  Drop control
    # punctuation that could become Markdown or JSONL structure.
    return re.sub(r"[^A-Za-z0-9_.:/@+() -]", "", text)[:limit] or None


def _int(value: object, minimum: int = 0, maximum: int = 2_147_483_647) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(minimum, min(maximum, result))


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _hash(value: object) -> str | None:
    text = str(value) if value is not None else ""
    return text if _HASH_RE.fullmatch(text) else None


def _digest(value: object) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _failure_class(envelope: dict) -> str:
    status = str(envelope.get("status") or "").lower()
    kind = str(envelope.get("error_kind") or "").lower()
    stage = str((envelope.get("timeout") or {}).get("stage") or "").lower()
    text = " ".join((kind, stage, str(envelope.get("error") or "").lower()))
    if status == "success":
        return "success"
    if status == "blocked":
        return "blocked"
    if status == "partial":
        return "partial"
    if "timeout" in text or stage:
        return "timeout"
    if any(word in text for word in ("auth", "login", "unauthorized", "forbidden")):
        return "authentication"
    if any(word in text for word in ("quota", "rate limit", "429", "credit")):
        return "quota"
    if any(word in text for word in ("transport", "connection", "network", "acp")):
        return "transport"
    if any(word in text for word in ("not found", "command not found", "cli not")):
        return "missing_cli"
    if any(word in text for word in ("prompt", "argument", "invalid", "required")):
        return "invalid_input"
    if any(word in text for word in ("report", "contract", "parse")):
        return "report_contract"
    if "worktree" in text:
        return "worktree"
    return "backend" if status == "error" else "unknown"


@contextmanager
def _spool_lock(path: Path):
    """Small cross-process lock for append/trim/clear on the local spool."""
    lock = Path(str(path) + ".lock")
    deadline = time.monotonic() + 5.0
    acquired = False
    owner_token = uuid.uuid4().hex
    while not acquired:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, owner_token.encode("ascii"))
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            acquired = True
        except FileExistsError:
            try:
                stat = lock.stat()
                if time.time() - stat.st_mtime > 30:
                    # Re-read the same owner marker before reclaiming.  The
                    # comparison closes the stale-owner race where a paused
                    # holder wakes after another process has taken its lock.
                    with lock.open("rb") as fh:
                        marker = fh.read(64)
                    if lock.stat().st_mtime_ns == stat.st_mtime_ns:
                        try:
                            if marker == b"" or len(marker) == 32:
                                lock.unlink()
                                continue
                        except FileNotFoundError:
                            continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with lock.open("rb") as fh:
                    marker = fh.read(64)
                if marker == owner_token.encode("ascii"):
                    lock.unlink()
            except OSError:
                pass


def _workspace_summary(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = {"coverage": _safe_id(value.get("coverage"), 24),
           "child_commit": _bool_or_none(value.get("child_commit")),
           "mutation": _bool_or_none(value.get("mutation")),
           "read_only_violation": _bool_or_none(value.get("read_only_violation")),
           "attribution": _safe_id(value.get("attribution"), 24)}
    for key in ("before", "after"):
        snap = value.get(key)
        if isinstance(snap, dict):
            out[key] = {"staged_count": len(snap.get("staged") or []) if isinstance(snap.get("staged"), list) else None,
                        "unstaged_count": len(snap.get("unstaged") or []) if isinstance(snap.get("unstaged"), list) else None,
                        "renamed_count": len(snap.get("renamed") or []) if isinstance(snap.get("renamed"), list) else None,
                        "untracked_count": len(snap.get("untracked") or []) if isinstance(snap.get("untracked"), list) else None}
    return out


def event_from_envelope(envelope: dict, *, source: str = "dispatch") -> dict | None:
    """Project an envelope into a public-safe, bounded telemetry event."""
    if not isinstance(envelope, dict) or "status" not in envelope:
        return None
    model = envelope.get("model") if isinstance(envelope.get("model"), dict) else {}
    summon = envelope.get("summon") if isinstance(envelope.get("summon"), dict) else {}
    timeout = envelope.get("timeout") if isinstance(envelope.get("timeout"), dict) else {}
    billing = envelope.get("billing") if isinstance(envelope.get("billing"), dict) else {}
    event = {
        "schema": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "recorded_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "source": _safe_id(source, 32) or "dispatch",
        **({"source_trust": "unverified"} if source == "bug-report" else {}),
        "status": _safe_id(envelope.get("status"), 24) or "unknown",
        "execution_status": _safe_id(envelope.get("execution_status"), 24),
        "failure_class": (_safe_id(envelope.get("failure_class"), 24)
                          if isinstance(envelope.get("failure_class"), str)
                          and envelope.get("failure_class") in _FAILURE_CLASSES
                          else _failure_class(envelope)),
        "error_sha256": _digest(envelope.get("error")),
        "backend": _safe_id(envelope.get("cli"), 40),
        "transport": _safe_id(envelope.get("transport"), 24),
        "model_requested": _safe_id(model.get("requested")),
        "model_served": _safe_id(model.get("served")),
        "permission": _safe_id(envelope.get("permission"), 32),
        "report_ok": _bool_or_none(envelope.get("report_ok")),
        "exit_code": _int(envelope.get("exit_code"), -255, 255),
        "elapsed_ms": _int(envelope.get("elapsed_ms"), 0, 7 * 24 * 60 * 60 * 1000),
        "attempts": _int(envelope.get("attempts"), 0, 100),
        "timeout_stage": _safe_id(timeout.get("stage"), 48),
        "billing_source": _safe_id(billing.get("source"), 32),
        "summon_version": _safe_id(summon.get("version"), 40),
        "scripts_sha256": _hash(summon.get("scripts_sha256")),
        "prompt_sha256": _hash(envelope.get("prompt_sha256")),
        "workspace": _workspace_summary(envelope.get("workspace_evidence")),
        "platform": {"os": os.name, "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                      "system": platform.system() or None},
    }
    warnings = envelope.get("warnings")
    if isinstance(warnings, list):
        event["warning_count"] = min(len(warnings), _MAX_WARNINGS)
    elif isinstance(envelope.get("warning_count"), int):
        event["warning_count"] = max(0, min(envelope["warning_count"], _MAX_WARNINGS))
    artifacts = envelope.get("artifacts")
    if isinstance(artifacts, dict):
        files = artifacts.get("files")
        event["artifacts"] = {"count": len(files) if isinstance(files, list) else None,
                              "changed": _bool_or_none(artifacts.get("changed")),
                              "stable": _bool_or_none(artifacts.get("stable_during_dispatch"))}
    return {key: value for key, value in event.items() if value is not None}


def _sanitize_event(item: dict, *, source: str = "bug-report") -> dict | None:
    """Re-project an event loaded from disk before putting it in a report.

    The local spool is user-writable, so a hand-edited JSONL line must not be
    trusted merely because it carries our schema and event markers.  Reusing
    the envelope allow-list keeps ``--from`` safe even for a forged event.
    """
    if not isinstance(item, dict) or item.get("schema") != SCHEMA_VERSION:
        return None
    model = {"requested": item.get("model_requested"),
             "served": item.get("model_served")}
    proxy = {
        "status": item.get("status"), "execution_status": item.get("execution_status"),
        "failure_class": item.get("failure_class"), "error": None,
        "error_hint": None, "cli": item.get("backend"),
        "transport": item.get("transport"), "model": model,
        "permission": item.get("permission"), "report_ok": item.get("report_ok"),
        "exit_code": item.get("exit_code"), "elapsed_ms": item.get("elapsed_ms"),
        "attempts": item.get("attempts"), "timeout": {"stage": item.get("timeout_stage")},
        "billing": {"source": item.get("billing_source")},
        "summon": {"version": item.get("summon_version"),
                   "scripts_sha256": item.get("scripts_sha256")},
        "prompt_sha256": item.get("prompt_sha256"),
        "workspace_evidence": item.get("workspace"),
        "warnings": item.get("warnings"),
        "warning_count": item.get("warning_count"),
    }
    event = event_from_envelope(proxy, source=source)
    if event is None:
        return None
    # Imported evidence is deliberately assigned a fresh identity and current
    # timestamp. A hand-edited event must not impersonate a native dispatch.
    event["source"] = source
    event["source_trust"] = "unverified"
    event["error_sha256"] = _hash(item.get("error_sha256"))
    event = {key: value for key, value in event.items() if value is not None}
    if isinstance(item.get("artifacts"), dict):
        artifacts = item["artifacts"]
        event["artifacts"] = {
            "count": _int(artifacts.get("count"), 0, 100_000),
            "changed": _bool_or_none(artifacts.get("changed")),
            "stable": _bool_or_none(artifacts.get("stable")),
        }
        event["artifacts"] = {k: v for k, v in event["artifacts"].items() if v is not None}
    return {key: value for key, value in event.items() if value is not None}


def record(envelope: dict) -> dict | None:
    """Append one safe event when effective telemetry is enabled.

    Any I/O failure is deliberately swallowed.  Diagnostics must never change
    the dispatch result or turn a successful run into an error.
    """
    if not enabled():
        return None
    event = event_from_envelope(envelope)
    if event is None:
        return None
    raw = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > _MAX_EVENT_BYTES:
        event["warnings"] = ["telemetry event exceeded its bound and was reduced"]
        event.pop("workspace", None)
        event.pop("artifacts", None)
        raw = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > _MAX_EVENT_BYTES:
        return None
    path = _events_path()
    with _spool_lock(path) as locked:
        if not locked:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                return None
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(path), flags, 0o600)
            try:
                # Regular-file writes are normally complete, but looping preserves
                # the JSONL record if a platform returns a short write.
                offset = 0
                while offset < len(raw):
                    offset += os.write(fd, raw[offset:])
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _trim_event_file(path)
            return event
        except OSError:
            return None


def _parse_event_lines(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines()[-_MAX_EVENTS_SCAN:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA_VERSION:
            out.append(item)
    return out


def _trim_event_file(path: Path) -> None:
    """Keep the local spool bounded without making dispatch depend on it."""
    try:
        if path.stat().st_size <= _MAX_EVENT_FILE_BYTES:
            return
        with path.open("rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - _MAX_SOURCE_BYTES))
            raw = fh.read(_MAX_SOURCE_BYTES)
        events = _parse_event_lines(raw)
        kept = []
        total = 0
        for event in reversed(events):
            line = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
            if kept and total + len(line) > _MAX_EVENT_FILE_BYTES:
                break
            kept.append(line)
            total += len(line)
        if kept:
            _atomic_write(path, b"".join(reversed(kept)))
        else:
            # A malformed oversized spool is not allowed to grow forever.
            _atomic_write(path, b"")
    except (OSError, ValueError, TypeError):
        # A full or malformed spool must never turn a dispatch into an error.
        return


def latest_event() -> dict | None:
    try:
        path = _events_path()
        with path.open("rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - _MAX_SOURCE_BYTES))
            raw = fh.read(_MAX_SOURCE_BYTES)
    except OSError:
        return None
    parsed = _parse_event_lines(raw)
    return parsed[-1] if parsed else None


def _load_json_source(path: Path) -> dict | None:
    try:
        if path.is_dir():
            candidates = []
            for name in ("envelope.json", "result.json", "response.json"):
                candidate = path / name
                if candidate.is_file():
                    candidates.append(candidate)
                    break
            if not candidates:
                candidates = sorted(path.glob("*.log"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                return None
            path = candidates[-1]
        size = path.stat().st_size
        if size > _MAX_SOURCE_BYTES and path.suffix.lower() != ".log":
            raise ValueError("source is larger than the 2 MiB diagnostic bound")
        with path.open("rb") as fh:
            if size > _MAX_SOURCE_BYTES:
                fh.seek(max(0, size - _MAX_SOURCE_BYTES))
            raw = fh.read(_MAX_SOURCE_BYTES)
        try:
            item = json.loads(raw.decode("utf-8"))
        except ValueError:
            text = raw.decode("utf-8", errors="replace")
            markers = list(re.finditer(r"(?m)^# final envelope\r?\n", text))
            if markers:
                try:
                    item = json.loads(text[markers[-1].end():].strip())
                except ValueError:
                    item = None
            else:
                events = _parse_event_lines(raw)
                item = events[-1] if events else None
        return item if isinstance(item, dict) else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def make_report(source: dict, *, title: str | None = None,
                description: str | None = None) -> tuple[str, dict]:
    is_event = (isinstance(source, dict) and source.get("schema") == SCHEMA_VERSION
                and source.get("event_id") and source.get("recorded_at"))
    event = _sanitize_event(source) if is_event else event_from_envelope(source, source="bug-report")
    if event is None:
        raise ValueError("source does not contain a dispatch envelope or telemetry event")
    issue_title = _clean_text(title, 120) or (
        "Summon diagnostic: " + (_safe_id(event.get("failure_class"), 60) or "unknown"))
    user_description = _clean_text(description, 1200) if description else None
    def _markdown_payload(value: str) -> str:
        # Backend text is untrusted. Keep it inside the evidence fence even if
        # it contains a Markdown fence of its own.
        return value.replace("```", "\\u0060\\u0060\\u0060")
    lines = ["# Summon diagnostic report", "", "<!-- Generated locally by summon. Review before sharing. -->", "",
             "## Summary", "", issue_title, ""]
    if user_description:
        lines += ["## What happened", "", "```text", _markdown_payload(user_description), "```", ""]
    lines += ["## Sanitized evidence", "", "```json",
              _markdown_payload(json.dumps(event, ensure_ascii=True, indent=2, sort_keys=True)), "```", "",
              "## Privacy", "",
              "This report contains bounded diagnostic metadata only. Automatically collected prompt/result text, agent results, raw output, credentials, and absolute machine paths were omitted. Deterministic SHA-256 fingerprints of prompt/error values may remain for correlation; they are not plaintext, but can correlate or reveal low-entropy values. Review any manually supplied description and these fingerprints before sharing.", "",
              "To submit it, review this file, then run `summon bug-report --submit-github --from REVIEWED_REPORT.md`.", ""]
    return "\n".join(lines), {"title": issue_title, "event": event}


def write_report(source: dict, *, output: str | None = None,
                 title: str | None = None, description: str | None = None) -> dict:
    text, meta = make_report(source, title=title, description=description)
    if output:
        path = Path(output).expanduser()
    else:
        _reports_dir().mkdir(parents=True, exist_ok=True)
        path = _reports_dir() / f"summon-bug-{meta['event'].get('event_id', uuid.uuid4().hex[:12])}.md"
    _atomic_write(path, text.encode("utf-8"))
    return {"ok": True, "report": _display_path(path), "title": meta["title"],
            "event_id": meta["event"].get("event_id"), "submitted": False,
            "submission_url": None}


def submit_github(report_path: str, *, repo: str = "Nafjan/summon",
                  title: str | None = None, validated_report: dict | None = None) -> dict:
    """Submit an existing reviewed report through authenticated ``gh``."""
    if not _REPO_RE.fullmatch(repo or ""):
        raise ValueError("--github-repo must be OWNER/REPOSITORY")
    meta = validated_report or validate_report_file(report_path)
    raw = meta.get("_content") if isinstance(meta, dict) else None
    if not isinstance(raw, bytes):
        # Do not trust a caller-provided metadata dictionary unless it carries
        # the bytes that were actually validated.
        meta = validate_report_file(report_path)
        raw = meta["_content"]
    path = Path(meta["path"])
    gh = shutil.which("gh")
    if not gh:
        raise FileNotFoundError("GitHub CLI 'gh' is not installed; review the report and submit it manually")
    issue_title = _clean_text(title, 120) or meta["title"]
    snapshot = None
    try:
        fd, snapshot = tempfile.mkstemp(prefix=".summon-reviewed-", suffix=".md")
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(snapshot, 0o600)
        except OSError:
            pass
        args = [gh, "issue", "create", "--repo", repo,
                "--title", issue_title, "--body-file", snapshot]
    except OSError as exc:
        if snapshot:
            try:
                os.unlink(snapshot)
            except OSError:
                pass
        raise RuntimeError("GitHub issue submission could not stage the reviewed report") from exc
    try:
        from _spawn import run_flags
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, timeout=90,
                              **run_flags())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub issue submission timed out; the local report is preserved") from exc
    except OSError as exc:
        raise RuntimeError("GitHub issue submission could not start; the local report is preserved") from exc
    finally:
        if snapshot:
            try:
                os.unlink(snapshot)
            except OSError:
                pass
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError("GitHub issue submission failed; the local report is preserved")
    match = _ISSUE_URL_RE.search(output)
    return {"ok": True, "submitted": True, "submission_url": match.group(0) if match else None}


def validate_report_file(path: str) -> dict:
    """Validate and describe an already-reviewed Markdown report."""
    report = Path(path).expanduser().resolve()
    try:
        size = report.stat().st_size
        if size > _MAX_SOURCE_BYTES:
            raise ValueError("reviewed bug report is larger than the 2 MiB diagnostic bound")
        with report.open("rb") as fh:
            raw = fh.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise FileNotFoundError(f"reviewed bug report does not exist: {path}") from exc
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ValueError("reviewed bug report is larger than the 2 MiB diagnostic bound")
    text = raw.decode("utf-8")
    if not text.startswith(_REPORT_HEADER):
        raise ValueError("--submit-github requires a reviewed Summon Markdown report from --output")
    title = "Summon diagnostic report"
    marker = "## Summary\n"
    if marker in text:
        candidate = next((line.strip() for line in text.split(marker, 1)[1].splitlines()
                          if line.strip()), "")
        title = _clean_text(candidate, 120) or title
    return {"ok": True, "report": _display_path(report), "path": str(report), "title": title,
            "_content": raw,
            "event_id": None, "submitted": False, "submission_url": None}


def source_event(path: str | None = None) -> dict:
    if path:
        item = _load_json_source(Path(path).expanduser())
        if item is None:
            raise ValueError("--from must point to a JSON envelope, JSONL telemetry file, or debug directory")
        if (item.get("schema") == SCHEMA_VERSION and item.get("event_id")
                and item.get("recorded_at")):
            event = _sanitize_event(item)
            if event is not None:
                return event
        event = event_from_envelope(item, source="bug-report")
        if event is None:
            raise ValueError("--from does not contain a dispatch envelope")
        return event
    event = latest_event()
    if event is None:
        raise ValueError("no local telemetry event found; enable telemetry or pass --from ENVELOPE")
    sanitized = _sanitize_event(event)
    if sanitized is None:
        raise ValueError("latest local telemetry event is malformed")
    return sanitized
