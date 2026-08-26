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
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


# The opt-in config is not an event.  Keep its original schema independent of
# the spool contract so enabling telemetry never migrates a user's preference.
CONFIG_SCHEMA_VERSION = 1
LEGACY_EVENT_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 2
# Keep the telemetry contract tied to the dispatcher release without importing
# ``run_subagent`` (which would introduce a module cycle).  Release bumps must
# update this alongside the dispatcher ``__version__``.
SUMMON_VERSION = "3.2.1"
# Backward-compatible name for callers that used the old event constant.  It
# refers to event records, never the persisted opt-in configuration.
SCHEMA_VERSION = EVENT_SCHEMA_VERSION
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
_REPORT_MARKER = "<!-- Generated locally by summon. Review before sharing. -->"
_REPORT_FOOTER = ("To submit it, review this file, then run `summon bug-report "
                 "--submit-github --from REVIEWED_REPORT.md`.")
_SALT_RE = re.compile(r"^[0-9a-f]{64}$")
_EPHEMERAL_SALT: str | None = None
_FAILURE_CLASSES = {
    "success", "blocked", "timeout", "cancelled", "authentication", "quota",
    "permission", "transport", "missing_cli", "invalid_input", "report_contract", "worktree",
    "backend", "dispatch", "partial", "unknown",
}
_EVENT_KIND = "operation_terminal"
_TERMINAL_KINDS = {"operation"}
_OPERATIONS = {
    "dispatch", "chat_turn", "council", "deliberate", "manifest",
    "resume", "swarm", "doctor", "onboarding",
}
_COHORTS = {"test", "review", "preflight", "orchestration", "legacy_unknown"}
_STATUSES = {"success", "error", "blocked", "partial", "cancelled", "unknown"}
# A structural refusal did not execute a provider turn.  Keep this state on
# ``execution_status`` only; the terminal operation status remains ``blocked``.
_EXECUTION_STATUSES = _STATUSES | {"not_run"}
_ATTEMPT_STATUSES = {"not_run", "completed", "unknown"}
_SERVED_MODEL_EVIDENCE = {"reported", "inferred", "absent", "unknown"}
_MODEL_SOURCES = {
    "cli", "frontmatter", "invocation", "ambient_config", "legacy_args",
    "profile_default", "provider_default", "unknown",
}
_REPORT_ERROR_CODES = {"missing", "incomplete", "invalid", "parse_error", "unknown"}
_PUBLIC_REPORT_KEYS = {
    "schema", "source", "source_trust", "event_kind", "terminal_kind", "operation",
    "cohort", "validation_outcome", "status", "execution_status", "failure_class",
    "backend", "transport", "permission", "model_requested", "model_targeted",
    "model_resolved", "model_served", "model_requested_class", "model_served_class",
    "model_source", "model_selector_source", "served_model_evidence", "model_mismatch",
    "model_match", "named_model_verified",
    "report_expected", "report_ok", "report_error_code", "result_usable",
    "provider_contacted", "auth_stage", "auth_outcome", "interactive_required",
    "remediation_code", "auth_lifecycle_evidence", "exit_code", "elapsed_ms", "attempts",
    "attempt_status",
    "timeout_stage", "billing_source", "summon_version", "warning_count", "artifacts",
    "event_id", "operation_id", "parent_operation_id", "attempt_id", "provider_turn_id",
    "event_sequence", "cohort_provenance_digest", "provider_adapter_revision",
    "evidence_registry_revision", "error_sha256", "prompt_sha256", "scripts_sha256",
    "platform", "workspace", "recorded_at", "raw_backend_exit_code",
    "normalized_exit_code",
}
# Reports are generated from the complete public projection.  Requiring that
# shape prevents a hand-written file containing only ``source_trust`` (or a
# small forged subset) from being accepted as a reviewed report.  ``artifacts``
# and ``warning_count`` are genuinely optional when no artifacts/warnings were
# recorded, so they remain outside the required set.
_PUBLIC_REPORT_REQUIRED_KEYS = _PUBLIC_REPORT_KEYS - {"artifacts", "warning_count"}
_AUTH_STAGES = {"preflight", "call", "refresh", "login", "retry", "handoff", "terminal", "unknown"}
_AUTH_OUTCOMES = {
    "not_needed", "refreshed", "login_started", "login_required", "recovered",
    "failed", "declined", "cancelled", "unknown",
}
_AUTH_EVIDENCE = {"reported", "absent"}
_REMEDIATION_CODES = {
    "none", "provider_login_required", "provider_refresh_failed",
    "provider_auth_cancelled", "provider_quota_wait", "provider_cli_missing",
    "provider_config_required", "unknown",
}
_TIMEOUT_STAGES = {
    "queue", "preflight", "backend_execution", "stream", "report_validation",
    "council_overall", "owner_shutdown", "unknown",
}
_TIMEOUT_STAGE_ALIASES = {
    "backend-execution": "backend_execution",
    "backend_execution": "backend_execution",
    "council-setup": "preflight",
    "council_setup": "preflight",
    "council_overall": "council_overall",
    "session/prompt": "preflight",
}
# Raw provider deployment labels are not evidence-safe: slash/colon forms can
# carry tenant, account, URL, or private deployment identifiers.  The future
# evidence registry may re-introduce approved canonical aliases; this slice
# stores only the conservative public identifier grammar.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_MODEL_FORBIDDEN_RE = re.compile(
    r"(?i)(?:bearer|basic|secret|token|password|api[_-]?key|auth|private|tenant|"
    r"account|customer|acct|deployment|personal)")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REQUIRED_EVENT_FIELDS = {
    "schema", "event_id", "recorded_at", "source", "event_kind",
    "terminal_kind", "operation", "cohort", "operation_id",
    "parent_operation_id", "attempt_id", "provider_turn_id", "event_sequence",
    "validation_outcome", "cohort_provenance_digest", "provider_adapter_revision",
    "evidence_registry_revision", "status", "failure_class",
    "served_model_evidence", "model_mismatch", "model_match", "named_model_verified",
    "report_expected", "report_ok", "report_error_code", "summon_version",
    "auth_lifecycle_evidence", "attempt_status",
}
_VALIDATION_OUTCOMES = {
    "valid", "invalid_status", "invalid_cohort", "invalid_model_evidence",
    "invalid_revision", "invalid_auth",
}


def _bounded(value: object, allowed: set[str], default: str | None = None) -> str | None:
    """Return a fixed vocabulary value, never an arbitrary telemetry string."""
    if isinstance(value, str) and value in allowed:
        return value
    return default


def _operation(value: object, default: str = "dispatch") -> str:
    return _bounded(value, _OPERATIONS, default) or default


def _cohort(value: object, default: str = "test", *, allow_legacy: bool = False) -> str:
    # ``production_like`` is intentionally not accepted here.  Only the later
    # signed-provenance integration may grant that cohort; ordinary callers and
    # environment values must not be able to promote traffic into a GA lane.
    allowed = _COHORTS if allow_legacy else _COHORTS - {"legacy_unknown"}
    return _bounded(value, allowed, default) or default


def _timeout_stage(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown"
    canonical = _TIMEOUT_STAGE_ALIASES.get(value.strip().lower(), value.strip().lower())
    return _bounded(canonical, _TIMEOUT_STAGES, "unknown")


class TelemetryOperation:
    """Opaque, local-only operation identity for schema-2 event projection.

    This foundation only emits an operation terminal through ``record``.  The
    context still owns sequence allocation now, so retry/turn instrumentation
    can be added later without changing identifiers or field names.
    """

    __slots__ = ("_operation", "_operation_id", "_parent_operation_id", "_cohort",
                 "_sequence", "_terminal_closed")

    def __setattr__(self, name: str, value: object) -> None:
        # The context is created locally; callers may observe it but must not
        # retag an operation or reopen a terminal by ordinary attribute writes.
        if hasattr(self, name):
            raise AttributeError("TelemetryOperation is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("TelemetryOperation is immutable")

    def __init__(self, operation: object = "dispatch", *, parent_operation_id: object = None,
                 cohort: object = "test") -> None:
        self._operation = _operation(operation)
        self._operation_id = uuid.uuid4().hex
        self._parent_operation_id = _safe_opaque_id(parent_operation_id)
        self._cohort = _cohort(cohort)
        self._sequence = 0
        self._terminal_closed = False

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def parent_operation_id(self) -> str | None:
        return self._parent_operation_id

    @property
    def cohort(self) -> str:
        return self._cohort

    @property
    def terminal_closed(self) -> bool:
        return self._terminal_closed

    def next_sequence(self) -> int:
        if (not isinstance(self._sequence, int) or isinstance(self._sequence, bool)
                or self._sequence < 0 or self._sequence > 2_147_483_647):
            object.__setattr__(self, "_sequence", 0)
        value = self._sequence
        object.__setattr__(self, "_sequence", value + 1)
        return value

    def close_terminal(self) -> bool:
        if self._terminal_closed:
            return False
        object.__setattr__(self, "_terminal_closed", True)
        return True


class _TrustedModelEvidence:
    """Private type marker: only an internal producer may assert mismatch."""

    __slots__ = ("served_model_evidence", "model_mismatch")

    def __init__(self, served_model_evidence: str, model_mismatch: bool | None) -> None:
        self.served_model_evidence = served_model_evidence
        self.model_mismatch = model_mismatch


_EVIDENCE_CAPABILITY = object()


class _TrustedAuthLifecycle:
    """Private marker for lifecycle fields emitted by the executor."""

    __slots__ = ("auth_stage", "auth_outcome", "interactive_required", "remediation_code")

    def __init__(self, auth_stage: str | None, auth_outcome: str | None,
                 interactive_required: bool | None, remediation_code: str | None) -> None:
        self.auth_stage = auth_stage
        self.auth_outcome = auth_outcome
        self.interactive_required = interactive_required
        self.remediation_code = remediation_code


_AUTH_CAPABILITY = object()


def _trusted_model_evidence(served_model_evidence: object,
                            model_mismatch: object = None, *,
                            _capability: object = None) -> _TrustedModelEvidence:
    """Construct the private evidence marker for a future receipt parser.

    This is intentionally separate from user/provider envelopes.  Projection
    never treats envelope booleans or labels as evidence of a served model.
    """
    # Only an adapter/parser in this module can mint the marker.  Envelope
    # fields remain untrusted and are never promoted by the projector.
    if _capability is not _EVIDENCE_CAPABILITY:
        return _TrustedModelEvidence("absent", None)
    evidence = _bounded(served_model_evidence, _SERVED_MODEL_EVIDENCE, "absent") or "absent"
    return _TrustedModelEvidence(evidence, _bool_or_none(model_mismatch))


def _trusted_auth_lifecycle(auth_stage: object, auth_outcome: object,
                            interactive_required: object, remediation_code: object, *,
                            _capability: object = None) -> _TrustedAuthLifecycle | None:
    """Construct an executor-only auth lifecycle marker."""
    if _capability is not _AUTH_CAPABILITY:
        return None
    values = (
        _bounded(auth_stage, _AUTH_STAGES),
        _bounded(auth_outcome, _AUTH_OUTCOMES),
        _bool_or_none(interactive_required),
        _bounded(remediation_code, _REMEDIATION_CODES),
    )
    if all(value is None for value in values):
        return None
    return _TrustedAuthLifecycle(*values)


def _safe_opaque_id(value: object) -> str | None:
    """Accept only a locally shaped opaque id; never sanitize arbitrary text into one."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    return None


def new_operation_context(operation: object = "dispatch", *, parent_operation_id: object = None,
                          cohort: object = "test") -> TelemetryOperation:
    return TelemetryOperation(operation, parent_operation_id=parent_operation_id, cohort=cohort)


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
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
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


def _correlation_salt(*, ensure: bool = False) -> bytes | None:
    """Return the local correlation salt without exposing it to events.

    A persisted salt is created only for an opted-in configuration.  An
    environment-only opt-in receives a process-local salt, so a temporary
    diagnostic override never mutates the user's preference file.
    """
    global _EPHEMERAL_SALT
    data = _read_config()
    raw = data.get("salt")
    if isinstance(raw, str) and _SALT_RE.fullmatch(raw):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    if not ensure:
        if _EPHEMERAL_SALT and _SALT_RE.fullmatch(_EPHEMERAL_SALT):
            return bytes.fromhex(_EPHEMERAL_SALT)
        return None
    salt = secrets.token_hex(32)
    if data.get("enabled") is True:
        updated = {"schema": CONFIG_SCHEMA_VERSION, "enabled": True, "salt": salt}
        try:
            _atomic_write(_config_path(), json.dumps(updated, sort_keys=True).encode("utf-8"))
            return bytes.fromhex(salt)
        except OSError:
            pass
    _EPHEMERAL_SALT = salt
    return bytes.fromhex(salt)


def set_enabled(value: bool) -> dict:
    """Persist opt-in and rotate the local correlation salt on boundaries."""
    global _EPHEMERAL_SALT
    _EPHEMERAL_SALT = None
    data = {"schema": CONFIG_SCHEMA_VERSION, "enabled": bool(value)}
    if value:
        data["salt"] = secrets.token_hex(32)
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
    return {"schema": CONFIG_SCHEMA_VERSION, "enabled": enabled(),
            "event_file": _display_path(path), "event_count": count,
            "event_bytes": size}


def clear_events() -> dict:
    """Delete the local spool and rotate its correlation salt."""
    global _EPHEMERAL_SALT
    path = _events_path()
    removed = False
    with _spool_lock(path) as locked:
        if locked:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
    _EPHEMERAL_SALT = None
    config = _read_config()
    if config.get("enabled") is True:
        config["schema"] = CONFIG_SCHEMA_VERSION
        config["salt"] = secrets.token_hex(32)
        try:
            _atomic_write(_config_path(), json.dumps(config, sort_keys=True).encode("utf-8"))
        except OSError:
            pass
    elif enabled():
        _correlation_salt(ensure=True)
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


def _safe_model_id(value: object) -> str | None:
    """Permit only conservative public model metadata, never deployments/paths."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (not _MODEL_ID_RE.fullmatch(text) or "://" in text or "\\" in text
            or ".." in text or _MODEL_FORBIDDEN_RE.search(text)):
        return None
    return text


def _revision(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _REVISION_RE.fullmatch(value):
        return value
    return "unknown"


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
    payload = str(value).encode("utf-8", "replace")
    salt = _correlation_salt()
    if salt:
        return hmac.new(salt, payload, hashlib.sha256).hexdigest()
    # Compatibility for report construction while telemetry is disabled.  A
    # persisted/opted-in record always calls ``_correlation_salt(ensure=True)``
    # from ``record`` before projection.
    return hashlib.sha256(payload).hexdigest()


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
    if "permission_unsupported" in text or "cannot enforce" in text:
        return "permission"
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


def event_from_envelope(envelope: dict, *, source: str = "dispatch",
                        operation_context: TelemetryOperation | None = None,
                        event_kind: str = "operation_terminal",
                        terminal_kind: str | None = "operation",
                        cohort: str | None = None) -> dict | None:
    """Project an envelope into the schema-2 local telemetry contract.

    ``record`` remains the compatibility entrypoint and supplies an operation
    terminal.  Future lifecycle emitters may pass their own context and a
    different event kind without duplicating the privacy allow-list.
    """
    if not isinstance(envelope, dict):
        return None
    # This first writer only emits operation terminals.  Attempt/turn events
    # will arrive with their own lifecycle implementation, not as a permissive
    # optional argument accidentally used by an arbitrary caller.
    if event_kind != _EVENT_KIND or terminal_kind not in _TERMINAL_KINDS:
        return None
    if not isinstance(operation_context, TelemetryOperation):
        # The public envelope is not a cohort/operation authority.  Dedicated
        # orchestration code can pass a local context; ordinary records remain
        # dispatch/test until a verified provenance integration exists.
        operation_context = new_operation_context("dispatch", cohort="test")
    # Invalid input before the first valid terminal is retained as a diagnostic
    # record, but once a valid terminal closes the operation no later projection
    # (valid or invalid) may mint another operation-terminal event.
    if operation_context.terminal_closed:
        return None
    model = envelope.get("model") if isinstance(envelope.get("model"), dict) else {}
    summon = envelope.get("summon") if isinstance(envelope.get("summon"), dict) else {}
    timeout = envelope.get("timeout") if isinstance(envelope.get("timeout"), dict) else {}
    billing = envelope.get("billing") if isinstance(envelope.get("billing"), dict) else {}
    raw_execution_status = _bounded(envelope.get("execution_status"), _EXECUTION_STATUSES)
    raw_attempt_status = _bounded(envelope.get("attempt_status"), _ATTEMPT_STATUSES)
    structural_not_run = (
        raw_execution_status == "not_run" or raw_attempt_status == "not_run")
    if structural_not_run:
        # ``execution_status:not_run`` is an explicit proof that the provider
        # turn never started.  Re-project the complete public contract from
        # that fact instead of trusting contradictory compatibility fields
        # such as attempts=1, provider_contacted=true, or model_match=true.
        execution_status = "not_run"
        attempt_status = "not_run"
        attempts = 0
        provider_contacted = False
    else:
        execution_status = raw_execution_status
        attempts = _int(envelope.get("attempts"), 0, 100)
        provider_contacted = _bool_or_none(envelope.get("provider_contacted"))
        attempt_status = raw_attempt_status
        if attempt_status is None and (
                (attempts is not None and attempts > 0)
                or provider_contacted is True):
            # The executor has not historically stamped a success-only
            # attempt status.  ``completed`` means the attempt terminalized;
            # it does not imply a successful report or trusted model proof.
            attempt_status = "completed"
    requested_model = _safe_model_id(model.get("requested"))
    targeted_model = _safe_model_id(model.get("targeted"))
    resolved_model = _safe_model_id(model.get("resolved"))
    evidence = envelope.get("_telemetry_model_evidence")
    if structural_not_run:
        served_model = None
        served_evidence, mismatch = "absent", None
    elif isinstance(evidence, _TrustedModelEvidence):
        served_model = _safe_model_id(model.get("served"))
        served_evidence = evidence.served_model_evidence
        mismatch = evidence.model_mismatch
    else:
        served_model = _safe_model_id(model.get("served"))
        served_evidence, mismatch = "absent", None
    if served_evidence == "reported" and served_model is None:
        served_evidence, mismatch = "absent", None
    # A public projection must not manufacture a named-model claim from the
    # caller's envelope fields.  Only the executor-created trusted marker can
    # assert provider-reported identity, and even then all three identities
    # must be present and exactly equal.  Inferred/absent evidence is explicitly
    # unknown (null), never a false named-model vote.
    if (served_evidence == "reported" and mismatch is not None
            and requested_model and targeted_model and served_model):
        model_match = (
            requested_model == targeted_model == served_model
            and mismatch is False)
    else:
        model_match = None
    named_model_verified = model_match is True
    auth_marker = envelope.get("_telemetry_auth_lifecycle")
    if isinstance(auth_marker, _TrustedAuthLifecycle):
        auth_stage = auth_marker.auth_stage
        auth_outcome = auth_marker.auth_outcome
        interactive_required = auth_marker.interactive_required
        remediation_code = auth_marker.remediation_code
        auth_lifecycle_evidence = "reported"
    else:
        auth_stage = _bounded(envelope.get("auth_stage"), _AUTH_STAGES)
        auth_outcome = _bounded(envelope.get("auth_outcome"), _AUTH_OUTCOMES)
        interactive_required = _bool_or_none(envelope.get("interactive_required"))
        remediation_code = _bounded(envelope.get("remediation_code"), _REMEDIATION_CODES)
        auth_lifecycle_evidence = "absent"
    raw_status = envelope.get("status")
    status = _bounded(raw_status, _STATUSES, "unknown") or "unknown"
    validation_outcome = "valid" if status != "unknown" or raw_status == "unknown" else "invalid_status"
    raw_cohort = cohort
    if (raw_cohort == "production_like"
            or getattr(operation_context, "cohort", None) == "production_like"):
        # This slice has no signed provenance verifier.  Never allow an
        # envelope, context, or public argument to promote a record.
        validation_outcome = "invalid_cohort"
    raw_evidence = envelope.get("served_model_evidence")
    if (validation_outcome == "valid" and raw_evidence is not None
            and not isinstance(evidence, _TrustedModelEvidence)):
        validation_outcome = "invalid_model_evidence"
    raw_revision = envelope.get("provider_adapter_revision")
    raw_registry_revision = envelope.get("evidence_registry_revision")
    if validation_outcome == "valid" and (raw_revision is not None or raw_registry_revision is not None):
        validation_outcome = "invalid_revision"
    # Auth lifecycle values are bounded, but silently converting a forged value
    # to null would make an otherwise valid terminal look trustworthy.  Only an
    # executor-created marker may assert a lifecycle result; direct or disk
    # envelopes remain diagnostic-only.
    if validation_outcome == "valid":
        raw_auth = tuple(envelope.get(key) for key in (
            "auth_stage", "auth_outcome", "interactive_required", "remediation_code"))
        if auth_marker is None and any(value is not None for value in raw_auth):
            validation_outcome = "invalid_auth"
        elif isinstance(auth_marker, _TrustedAuthLifecycle):
            marker_values = (auth_stage, auth_outcome, interactive_required, remediation_code)
            if any(raw is not None and raw != marked
                   for raw, marked in zip(raw_auth, marker_values)):
                validation_outcome = "invalid_auth"
    # Invalid projections are visible diagnostic records, not a terminal state:
    # a later valid projection for this context must still be able to close it.
    if validation_outcome == "valid" and not operation_context.close_terminal():
        return None
    failure = (_safe_id(envelope.get("failure_class"), 24)
               if isinstance(envelope.get("failure_class"), str)
               and envelope.get("failure_class") in _FAILURE_CLASSES
               else _failure_class(envelope))
    event = {
        "schema": EVENT_SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "recorded_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "source": _safe_id(source, 32) or "dispatch",
        **({"source_trust": "unverified"} if source == "bug-report" else {}),
        # Required schema-2 grammar.  Nulls are retained deliberately: a
        # terminal that did not touch a provider is different from one whose
        # provider evidence was lost, and omission would collapse that fact.
        "event_kind": _EVENT_KIND,
        "terminal_kind": "operation",
        "operation": _operation(operation_context.operation),
        "cohort": _cohort(cohort if cohort is not None else operation_context.cohort),
        "operation_id": _safe_opaque_id(operation_context.operation_id) or uuid.uuid4().hex,
        "parent_operation_id": _safe_opaque_id(operation_context.parent_operation_id),
        # The executor mints a UUID-shaped physical-attempt identity.  Preserve
        # only that bounded opaque value; a caller-supplied arbitrary string is
        # not allowed to enter the local event contract.
        "attempt_id": _safe_opaque_id(envelope.get("attempt_id")),
        "provider_turn_id": None,
        "event_sequence": operation_context.next_sequence(),
        "validation_outcome": validation_outcome,
        "cohort_provenance_digest": None,
        # Revision bindings are accepted only from the future verified
        # provenance/evidence registry.  Raw envelope strings are not proof.
        "provider_adapter_revision": None,
        "evidence_registry_revision": None,
        "status": status,
        "execution_status": execution_status,
        "failure_class": failure,
        "error_sha256": _digest(envelope.get("error")),
        "backend": _bounded(envelope.get("cli"), {
            "claude", "codex", "cursor-agent", "gemini", "agy", "kimi", "arkcli",
            "openai-compat", "summon"}),
        "transport": _bounded(envelope.get("transport"), {
            "subprocess", "acp", "council", "manifest"}),
        "model_requested": requested_model,
        "model_targeted": targeted_model,
        "model_resolved": resolved_model,
        "model_served": served_model,
        # Model classes are reserved for the later evidence registry.  Keeping
        # them null is more truthful than guessing a provider/model family.
        "model_requested_class": None,
        "model_served_class": None,
        "model_source": _bounded(model.get("request_source"), _MODEL_SOURCES),
        "model_selector_source": _bounded(model.get("selector_source"), _MODEL_SOURCES),
        "served_model_evidence": served_evidence,
        "model_mismatch": mismatch,
        "model_match": model_match,
        "named_model_verified": named_model_verified,
        "permission": _bounded(envelope.get("permission"), {"read-only", "safe-edit", "yolo"}),
        "report_expected": None,
        "report_ok": None,
        "report_error_code": None,
        "result_usable": _bool_or_none(envelope.get("result_usable")),
        "provider_contacted": provider_contacted,
        "auth_stage": auth_stage,
        "auth_outcome": auth_outcome,
        "interactive_required": interactive_required,
        "remediation_code": remediation_code,
        "auth_lifecycle_evidence": auth_lifecycle_evidence,
        "exit_code": _int(envelope.get("exit_code"), -255, 255),
        "raw_backend_exit_code": _int(
            envelope.get("raw_backend_exit_code",
                         envelope.get("backend_exit_code", envelope.get("exit_code"))),
            -255, 255),
        "normalized_exit_code": _int(envelope.get("normalized_exit_code"), -255, 255),
        "elapsed_ms": _int(envelope.get("elapsed_ms"), 0, 7 * 24 * 60 * 60 * 1000),
        "attempts": attempts,
        "attempt_status": attempt_status,
        "timeout_stage": _timeout_stage(timeout.get("stage")),
        "billing_source": _bounded(billing.get("source"), {
            "api", "credit", "subscription", "unknown"}),
        # The version is a local runtime receipt, not caller-supplied envelope
        # metadata.  Use the module's canonical release value so a disk-loaded
        # or hand-built envelope cannot mint a misleading version.
        "summon_version": SUMMON_VERSION,
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
    # The dispatch and resume contracts promise a report block.  Fan-out
    # summaries (council/manifest) intentionally do not have one.  Defaults are
    # applied here rather than trusting caller-provided envelope labels.
    op = _operation(operation_context.operation)
    if op in {"council", "manifest"}:
        event["report_expected"] = False
        event["report_ok"] = None
        event["report_error_code"] = None
    else:
        raw_ok = envelope.get("report_ok")
        # Dispatch/resume always have a report contract.  A caller cannot
        # downgrade that obligation by setting report_expected=false.
        event["report_expected"] = True
        event["report_ok"] = _bool_or_none(raw_ok)
        raw_code = _bounded(envelope.get("report_error_code"), _REPORT_ERROR_CODES)
        if event["report_ok"] is True:
            # A successful report has no error code; discard a contradictory
            # caller value rather than making a valid record ambiguous.
            event["report_error_code"] = None
        elif event["report_ok"] is False:
            event["report_error_code"] = raw_code or "invalid"
        else:
            event["report_error_code"] = raw_code or "missing"
    # Required schema-2 fields intentionally retain null values.  Optional
    # legacy fields remain sparse to keep the local spool bounded.
    return event


def _spool_projection(event: dict) -> dict:
    """Drop absent optional values before JSONL persistence.

    Required schema fields retain explicit nulls for contract stability, while
    optional nulls (notably ``prompt_sha256``) must not create misleading
    privacy markers or waste the bounded spool.  The returned object is only
    for local storage; callers still receive the complete in-memory event.
    """
    return {
        key: value for key, value in event.items()
        if value is not None or key in _REQUIRED_EVENT_FIELDS
    }


def _valid_public_schema2_event(item: dict) -> bool:
    """Validate user-writable spool input before making a shareable view."""
    if item.get("schema") != EVENT_SCHEMA_VERSION:
        return False
    if item.get("event_kind") != _EVENT_KIND or item.get("terminal_kind") != "operation":
        return False
    if not isinstance(item.get("status"), str) or item.get("status") not in _STATUSES:
        return False
    execution_status = item.get("execution_status")
    if (execution_status is not None
            and (not isinstance(execution_status, str)
                 or execution_status not in _EXECUTION_STATUSES)):
        return False
    attempt_status = item.get("attempt_status")
    if (attempt_status is not None
            and (not isinstance(attempt_status, str)
                 or attempt_status not in _ATTEMPT_STATUSES)):
        return False
    if item.get("summon_version") != SUMMON_VERSION:
        return False
    validation_outcome = item.get("validation_outcome")
    if (not isinstance(validation_outcome, str)
            or validation_outcome not in _VALIDATION_OUTCOMES):
        return False
    timeout_stage = item.get("timeout_stage")
    if timeout_stage is not None and (
            not isinstance(timeout_stage, str) or timeout_stage not in _TIMEOUT_STAGES):
        return False
    artifacts = item.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, dict) or set(artifacts) - {"count", "changed", "stable"}:
            return False
        count = artifacts.get("count")
        if (count is not None and (not isinstance(count, int) or isinstance(count, bool)
                                  or count < 0 or count > 100_000)):
            return False
        for key in ("changed", "stable"):
            value = artifacts.get(key)
            if value is not None and not isinstance(value, bool):
                return False
    return True


def _sanitize_event(item: dict, *, source: str = "bug-report") -> dict | None:
    """Re-project an event loaded from disk before putting it in a report.

    The local spool is user-writable, so a hand-edited JSONL line must not be
    trusted merely because it carries our schema and event markers.  Reusing
    the envelope allow-list keeps ``--from`` safe even for a forged event.
    """
    if not isinstance(item, dict) or item.get("schema") not in (
            LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION):
        return None
    if item.get("schema") == EVENT_SCHEMA_VERSION and not _valid_public_schema2_event(item):
        return None
    model = {"requested": item.get("model_requested"),
             "targeted": item.get("model_targeted"),
             "resolved": item.get("model_resolved"),
             "served": item.get("model_served"),
             "request_source": item.get("model_source"),
             "selector_source": item.get("model_selector_source")}
    proxy = {
        "status": item.get("status"), "execution_status": item.get("execution_status"),
        "failure_class": item.get("failure_class"), "error": None,
        "error_hint": None, "cli": item.get("backend"),
        "transport": item.get("transport"), "model": model,
        "permission": item.get("permission"),
        "report_expected": item.get("report_expected"), "report_ok": item.get("report_ok"),
        "report_error_code": item.get("report_error_code"),
        "result_usable": item.get("result_usable"),
        "provider_contacted": item.get("provider_contacted"),
        # A disk line is user-editable and cannot mint provider evidence.  The
        # sanitized view deliberately drops these fields before re-projecting.
        # Auth lifecycle labels are also untrusted when reloaded from a
        # user-editable spool.  The report boundary does not mint recovery
        # claims or expose provider login state.
        "exit_code": item.get("exit_code"),
        "raw_backend_exit_code": item.get("raw_backend_exit_code"),
        "normalized_exit_code": item.get("normalized_exit_code"),
        "elapsed_ms": item.get("elapsed_ms"),
        "attempts": item.get("attempts"),
        "attempt_status": item.get("attempt_status"),
        "timeout": {"stage": item.get("timeout_stage")},
        "billing": {"source": item.get("billing_source")},
        "summon": {"version": item.get("summon_version"),
                   "scripts_sha256": item.get("scripts_sha256")},
        "prompt_sha256": item.get("prompt_sha256"),
        "workspace_evidence": item.get("workspace"),
        "warnings": item.get("warnings"),
        "warning_count": item.get("warning_count"),
    }
    legacy = item.get("schema") == LEGACY_EVENT_SCHEMA_VERSION
    event = event_from_envelope(
        proxy,
        source=source,
        cohort="legacy_unknown" if legacy else item.get("cohort"),
    )
    if event is None:
        return None
    # Imported evidence is deliberately re-projected with no trusted identity
    # and a current local timestamp. A hand-edited event must not impersonate
    # a native dispatch.
    event["source"] = source
    event["source_trust"] = "unverified"
    event["error_sha256"] = _hash(item.get("error_sha256"))
    # Model/deployment labels can contain private tenant or account metadata.
    # Until the evidence registry supplies approved public classes, retain
    # those identifiers only in the local spool and omit them from reports.
    for key in ("model_requested", "model_targeted", "model_resolved", "model_served",
                "model_source", "model_selector_source"):
        event[key] = None
    # Public bug reports intentionally redact every model identifier and carry no
    # signed evidence-registry binding. They therefore cannot certify a named model,
    # even when the private local event did. Keep the private spool authoritative.
    event["served_model_evidence"] = "absent"
    event["model_mismatch"] = None
    event["model_match"] = None
    event["named_model_verified"] = False
    for key, allowed in {
        "backend": {"claude", "codex", "cursor-agent", "gemini", "agy", "kimi",
                     "arkcli", "openai-compat", "summon"},
        "transport": {"subprocess", "acp", "council", "manifest"},
        "permission": {"read-only", "safe-edit", "yolo"},
    }.items():
        if event.get(key) not in allowed:
            event[key] = None
    # Bug reports are shareable views.  A spool is user-writable, so neither
    # its correlation ids nor adapter/provenance bindings are public evidence.
    for key in ("event_id", "operation_id", "parent_operation_id", "attempt_id",
                "provider_turn_id", "event_sequence", "cohort_provenance_digest",
                "provider_adapter_revision", "evidence_registry_revision", "recorded_at"):
        event[key] = None
    event["auth_lifecycle_evidence"] = None
    # Publishable bug reports must not carry local correlation material,
    # machine metadata, workspace snapshots, or deterministic fingerprints.
    for key in ("error_sha256", "prompt_sha256", "scripts_sha256", "platform",
                "workspace"):
        event[key] = None
    if legacy:
        # A schema-1 record has no event grammar or verified evidence state.
        # Keep its schema dimension instead of re-labelling it as a native v2
        # event, and make every unavailable v2 field explicitly null.
        event.update({
            "schema": LEGACY_EVENT_SCHEMA_VERSION,
            "event_kind": None, "terminal_kind": None, "operation": None,
            "cohort": "legacy_unknown", "validation_outcome": None,
            "summon_version": None,
            "report_expected": None, "report_ok": None, "report_error_code": None,
            "model_targeted": None, "model_resolved": None,
            "model_requested_class": None, "model_served_class": None,
            "model_source": None, "model_selector_source": None,
            "served_model_evidence": None, "model_mismatch": None,
            "model_match": None, "named_model_verified": False,
            "attempt_status": None,
            "auth_stage": None, "auth_outcome": None,
            "interactive_required": None, "remediation_code": None,
            "auth_lifecycle_evidence": None,
        })
    if isinstance(item.get("artifacts"), dict):
        artifacts = item["artifacts"]
        event["artifacts"] = {
            "count": _int(artifacts.get("count"), 0, 100_000),
            "changed": _bool_or_none(artifacts.get("changed")),
            "stable": _bool_or_none(artifacts.get("stable")),
        }
        event["artifacts"] = {k: v for k, v in event["artifacts"].items() if v is not None}
    return event


def record(envelope: dict, *, operation_context: TelemetryOperation | None = None,
           operation: str | None = None) -> dict | None:
    """Append one safe event when effective telemetry is enabled.

    Any I/O failure is deliberately swallowed.  Diagnostics must never change
    the dispatch result or turn a successful run into an error.
    """
    if not enabled():
        return None
    _correlation_salt(ensure=True)
    if operation_context is None and operation is not None:
        operation_context = new_operation_context(operation)
    event = event_from_envelope(envelope, operation_context=operation_context)
    if event is None:
        return None
    persisted = _spool_projection(event)
    raw = json.dumps(persisted, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > _MAX_EVENT_BYTES:
        persisted["warnings"] = ["telemetry event exceeded its bound and was reduced"]
        for key in ("workspace", "artifacts", "platform", "warnings"):
            if key != "warnings":
                persisted.pop(key, None)
        raw = json.dumps(persisted, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
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
        except (ValueError, RecursionError):
            continue
        if (isinstance(item, dict)
                and item.get("schema") in (LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION)):
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
    except (OSError, ValueError, TypeError, RecursionError):
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
                except (ValueError, RecursionError):
                    item = None
            else:
                events = _parse_event_lines(raw)
                item = events[-1] if events else None
        return item if isinstance(item, dict) else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None


def make_report(source: dict, *, title: str | None = None,
                description: str | None = None) -> tuple[str, dict]:
    is_event = (isinstance(source, dict)
                and source.get("schema") in (LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION)
                and source.get("event_id") and source.get("recorded_at"))
    if is_event:
        event = _sanitize_event(source)
    else:
        projected = event_from_envelope(source, source="bug-report")
        event = _sanitize_event(projected) if projected is not None else None
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
             "This report contains bounded diagnostic metadata only. Automatically collected prompt/result text, agent results, raw output, credentials, absolute machine paths, local identifiers, machine metadata, and deterministic fingerprints were omitted. Review any manually supplied description before sharing.", "",
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


def _validate_public_evidence(evidence: object) -> None:
    """Reject edits that turn a reviewed report back into a raw envelope.

    This is deliberately a shape validator, rather than a best-effort parser.
    A report is submitted to a public tracker only after this function accepts
    it, so missing fields, wrong JSON types, and unbounded enum values must all
    fail closed without raising incidental ``TypeError`` exceptions.
    """
    if not isinstance(evidence, dict):
        raise ValueError("reviewed report evidence must be a JSON object")
    unknown = set(evidence) - _PUBLIC_REPORT_KEYS
    if unknown:
        raise ValueError("reviewed report contains unknown evidence fields")
    missing = _PUBLIC_REPORT_REQUIRED_KEYS - set(evidence)
    if missing:
        raise ValueError("reviewed report is missing generated evidence fields")

    def _enum_or_none(key: str, allowed: set[str]) -> None:
        value = evidence.get(key)
        if value is not None and (not isinstance(value, str) or value not in allowed):
            raise ValueError("reviewed report contains an invalid evidence value")

    def _bool_or_none_public(key: str) -> None:
        value = evidence.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError("reviewed report contains an invalid boolean")

    def _int_or_none(key: str, minimum: int, maximum: int) -> None:
        value = evidence.get(key)
        if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
                or value < minimum or value > maximum):
            raise ValueError("reviewed report contains an invalid integer")

    if evidence.get("source_trust") != "unverified":
        raise ValueError("reviewed report evidence is not a Summon sanitized projection")
    if evidence.get("source") != "bug-report":
        raise ValueError("reviewed report evidence has an invalid source")
    schema = evidence.get("schema")
    if (not isinstance(schema, int) or isinstance(schema, bool)
            or schema not in (LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION)):
        raise ValueError("reviewed report contains an unsupported schema")

    _enum_or_none("status", _STATUSES)
    _enum_or_none("execution_status", _EXECUTION_STATUSES)
    _enum_or_none("failure_class", _FAILURE_CLASSES)
    _enum_or_none("backend", {"claude", "codex", "cursor-agent", "gemini", "agy", "kimi",
                               "arkcli", "openai-compat", "summon"})
    _enum_or_none("transport", {"subprocess", "acp", "council", "manifest"})
    _enum_or_none("permission", {"read-only", "safe-edit", "yolo"})
    _enum_or_none("timeout_stage", _TIMEOUT_STAGES)
    _enum_or_none("billing_source", {"api", "credit", "subscription", "unknown"})

    if schema == EVENT_SCHEMA_VERSION:
        if evidence.get("event_kind") != _EVENT_KIND:
            raise ValueError("reviewed report contains an invalid event kind")
        if evidence.get("terminal_kind") != "operation":
            raise ValueError("reviewed report contains an invalid terminal kind")
        _enum_or_none("operation", _OPERATIONS)
        _enum_or_none("cohort", _COHORTS - {"legacy_unknown"})
        _enum_or_none("validation_outcome", _VALIDATION_OUTCOMES)
        if evidence.get("summon_version") != SUMMON_VERSION:
            raise ValueError("reviewed report contains a non-current Summon version")
        _enum_or_none("served_model_evidence", _SERVED_MODEL_EVIDENCE)
        _enum_or_none("report_error_code", _REPORT_ERROR_CODES)
        if not isinstance(evidence.get("report_expected"), bool):
            raise ValueError("reviewed report contains an invalid report contract")
        _bool_or_none_public("report_ok")
    else:
        if evidence.get("cohort") != "legacy_unknown":
            raise ValueError("legacy report has an invalid cohort")
        for key in ("event_kind", "terminal_kind", "operation",
                    "validation_outcome", "served_model_evidence", "summon_version",
                    "report_expected", "report_ok", "report_error_code"):
            if evidence.get(key) is not None:
                raise ValueError("legacy report contains a schema-2 field")

    _bool_or_none_public("result_usable")
    _bool_or_none_public("provider_contacted")
    _bool_or_none_public("model_mismatch")
    _bool_or_none_public("model_match")
    if not isinstance(evidence.get("named_model_verified"), bool):
        raise ValueError("reviewed report contains an invalid named-model proof")
    # The public report is user-editable, so shape validation must also enforce
    # the relationship between these fields. A forged ``true`` bit with null
    # served identity/evidence must never pass merely because each JSON value is
    # individually well-typed. ``false`` is meaningful only for a reported
    # mismatch; absent/inferred evidence is represented by ``null``.
    model_match = evidence.get("model_match")
    named_verified = evidence.get("named_model_verified")
    served_evidence = evidence.get("served_model_evidence")
    expected_public_evidence = (
        "absent" if schema == EVENT_SCHEMA_VERSION else None)
    if (served_evidence != expected_public_evidence
            or evidence.get("model_mismatch") is not None
            or model_match is not None or named_verified is not False):
        raise ValueError(
            "public reports redact model identity and cannot certify a named model")
    _int_or_none("exit_code", -255, 255)
    _int_or_none("raw_backend_exit_code", -255, 255)
    _int_or_none("normalized_exit_code", -255, 255)
    _int_or_none("elapsed_ms", 0, 7 * 24 * 60 * 60 * 1000)
    _int_or_none("attempts", 0, 100)
    _enum_or_none("attempt_status", _ATTEMPT_STATUSES)
    if (evidence.get("execution_status") == "not_run"
            or evidence.get("attempt_status") == "not_run"):
        if (evidence.get("attempts") != 0
                or evidence.get("attempt_status") != "not_run"
                or evidence.get("execution_status") != "not_run"
                or evidence.get("provider_contacted") is not False
                or evidence.get("served_model_evidence") != "absent"
                or evidence.get("model_match") is not None
                or evidence.get("named_model_verified") is not False):
            raise ValueError("not-run reports must prove zero attempts and no provider contact")
    if "warning_count" in evidence:
        _int_or_none("warning_count", 0, _MAX_WARNINGS)

    for key in ("model_requested", "model_targeted", "model_resolved", "model_served",
                "model_requested_class", "model_served_class", "model_source",
                "model_selector_source", "auth_stage", "auth_outcome",
                "interactive_required", "remediation_code", "auth_lifecycle_evidence"):
        # The current public projection intentionally carries no raw model or
        # auth lifecycle values until their registries/capabilities are bound.
        if evidence.get(key) is not None:
            raise ValueError("reviewed report contains unverified private evidence")

    for key in ("event_id", "operation_id", "parent_operation_id", "attempt_id",
                "provider_turn_id", "event_sequence", "cohort_provenance_digest",
                "provider_adapter_revision", "evidence_registry_revision", "error_sha256",
                "prompt_sha256", "scripts_sha256", "platform", "workspace", "recorded_at"):
        if evidence.get(key) is not None:
            raise ValueError("reviewed report contains local correlation metadata")

    artifacts = evidence.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, dict) or set(artifacts) - {"count", "changed", "stable"}:
            raise ValueError("reviewed report contains invalid artifact metadata")
        count = artifacts.get("count")
        if count is not None and (
                not isinstance(count, int) or isinstance(count, bool)
                or count < 0 or count > 100_000):
            raise ValueError("reviewed report contains an invalid artifact count")
        for key in ("changed", "stable"):
            value = artifacts.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError("reviewed report contains invalid artifact state")


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
    # Parse either platform newline convention; the submitted bytes remain
    # exactly the reviewed file after validation.
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ValueError("reviewed report is not valid UTF-8") from exc
    if not text.startswith(_REPORT_HEADER) or _REPORT_MARKER not in text:
        raise ValueError("--submit-github requires a reviewed Summon Markdown report from --output")
    evidence_marker = "## Sanitized evidence\n\n```json\n"
    if (text.count(evidence_marker) != 1 or text.count(_REPORT_FOOTER) != 1
            or not text.rstrip().endswith(_REPORT_FOOTER)):
        raise ValueError("reviewed report is missing the sanitized evidence block")
    evidence_text = text.split(evidence_marker, 1)[1].split("\n```", 1)[0]
    try:
        evidence = json.loads(evidence_text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("reviewed report contains invalid sanitized evidence") from exc
    _validate_public_evidence(evidence)
    forbidden = {"prompt", "result", "output", "cwd", "agent_def", "auth",
                 "error", "error_hint", "private_field"}
    if forbidden.intersection(evidence):
        raise ValueError("reviewed report contains a forbidden unsanitized field")
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
        if (item.get("schema") in (LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION) and item.get("event_id")
                and item.get("recorded_at")):
            event = _sanitize_event(item)
            if event is not None:
                return event
        projected = event_from_envelope(item, source="bug-report")
        event = _sanitize_event(projected) if projected is not None else None
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
