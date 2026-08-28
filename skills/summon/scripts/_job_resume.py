"""Authenticated, at-most-once background continuation claims.

This module is deliberately provider-inert.  It turns one authenticated terminal
continuation source into one durable successor reservation and exposes the CAS
hooks used later by the background child.  Session handles and steering text stay
in private source/prompt files; public projections contain only opaque identities,
phases, and digests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import _jobs
from _job_continuation import (ContinuationError, read_private_source,
                               result_binding_sha256)
from _job_control import (_exclusive_control_lock,
                          authenticated_steering_commands)


LEDGER_SCHEMA = "summon.job-resume-ledger/v1"
CLAIM_SCHEMA = "summon.job-resume-claim/v1"
PUBLIC_SCHEMA = "summon.job-resume/v1"
MAX_LEDGER_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 256 * 1024
MAX_CLAIMS = 1
PROMPT_CONTRACT = "summon.resume-prompt/v1"
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_KIND_RE = re.compile(r"^[a-z0-9_]{1,96}$")
_CLAIM_KEYS = {
    "generation", "request_id", "request_sha256", "claim_id",
    "successor_job_id", "prompt_contract", "prompt_sha256",
    "steering_generations", "steering_sha256", "control_generation",
    "control_sha256", "permission", "gate_with", "allow_credit",
    "allow_payg", "timeout_ms", "gate_timeout_ms", "max_runtime_ms", "parent_phase",
    "gate_phase", "provider_phase", "provider_contacted",
    "successor_record_sha256", "bundle_sha256", "claim_sha256", "pid",
    "gate_pid", "provider_pid", "gate_decision_sha256", "terminal_sha256",
    "terminalization_error_kind",
    "created_at", "updated_at",
}


class ResumeError(ValueError):
    """Typed, provider-inert governed-resume refusal."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Reservation:
    root: str
    source_job_id: str
    request_id: str
    claim_id: str
    successor_job_id: str
    request_sha256: str
    prompt: str
    prompt_sha256: str
    source: dict
    steering: tuple[dict, ...]
    effective_permission: str
    gate_with: str | None
    allow_credit: bool
    allow_payg: bool
    timeout_ms: int
    gate_timeout_ms: int
    max_runtime_ms: int
    created_at: float


@dataclass(frozen=True)
class ChildContext:
    root: str
    source_job_id: str
    successor_job_id: str
    claim_id: str
    claim_sha256: str
    source: dict
    prompt: str
    resume_handle: str
    effective_permission: str
    gate_with: str | None
    gate_timeout_ms: int
    allow_credit: bool
    allow_payg: bool
    timeout_ms: int
    max_runtime_ms: int


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise ResumeError("resume_claim_invalid", "resume data is not canonical JSON") from exc


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _auth(nonce: str, domain: str, value: dict) -> str:
    return hmac.new(nonce.encode("utf-8"),
                    domain.encode("ascii") + b":" + _canonical(value),
                    hashlib.sha256).hexdigest()


def _is_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def ledger_path(root: str, source_job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, source_job_id))
    return os.path.join(base, f"{source_job_id}.resume-ledger.json")


def claim_path(root: str, successor_job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, successor_job_id))
    return os.path.join(base, f"{successor_job_id}.resume-claim.json")


def prompt_path(root: str, successor_job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, successor_job_id))
    return os.path.join(base, f"{successor_job_id}.resume-prompt.txt")


def successor_launch_lock(root: str, successor_job_id: str):
    """Serialize preparation/launch decisions for one deterministic successor.

    Identical resume callers reserve the same successor.  This lock closes the
    window between reading its authenticated claim and durably committing the
    launch, so the losing caller observes the winner's state instead of trying
    to create the same launch record again.
    """
    if not _jobs.valid_job_id(successor_job_id):
        raise ResumeError("invalid_job_id", "successor job id is invalid")
    base = os.path.dirname(_jobs.record_path(root, successor_job_id))
    path = os.path.join(base, f"{successor_job_id}.resume-launch.lock")
    return _exclusive_control_lock(path)


def _source_record(root: str, job_id: str) -> tuple[dict, str]:
    record, state = _read_strict_json(_jobs.record_path(root, job_id))
    if state != _jobs._OK or not isinstance(record, dict):
        raise ResumeError("source_job_untrusted", "source launch record is unavailable")
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise ResumeError("source_job_untrusted", "source launch record has no identity")
    return record, nonce


def _read_strict_json(path: str, *, max_bytes: int = MAX_LEDGER_BYTES):
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
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None, _jobs._CORRUPT
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
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


def _read_authenticated(path: str, nonce: str, domain: str, *, missing_ok: bool = False):
    value, state = _read_strict_json(path)
    if state == _jobs._MISSING and missing_ok:
        return None
    if state != _jobs._OK or not isinstance(value, dict):
        raise ResumeError("resume_claim_untrusted", "authenticated resume state is unavailable")
    auth = value.get("auth")
    body = {key: item for key, item in value.items() if key != "auth"}
    try:
        expected = _auth(nonce, domain, body)
    except ResumeError:
        raise
    if not _is_sha(auth) or not hmac.compare_digest(auth, expected):
        raise ResumeError("resume_claim_untrusted", "authenticated resume state did not verify")
    return value


def _write_authenticated(path: str, nonce: str, domain: str, body: dict) -> dict:
    value = dict(body)
    value["auth"] = _auth(nonce, domain, body)
    if len(_canonical(value)) > MAX_LEDGER_BYTES:
        raise ResumeError("resume_claim_oversized", "authenticated resume state is oversized")
    _jobs._atomic_write_json(path, value)
    return value


def _ledger_domain(source_job_id: str) -> str:
    return f"summon-job-resume-ledger/v1:{source_job_id}"


def _claim_domain(source_job_id: str, successor_job_id: str) -> str:
    return f"summon-job-resume-claim/v1:{source_job_id}:{successor_job_id}"


def _claim_key(source_nonce: str, successor_nonce: str) -> str:
    return hmac.new(source_nonce.encode("utf-8"),
                    b"summon-job-resume-claim-key/v1:" +
                    successor_nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _record_binding(record: dict) -> str:
    """Bind immutable successor launch fields while excluding spawn metadata."""
    return _digest({key: record.get(key) for key in (
        "agent", "prompt_sha256", "cwd", "flags", "summon", "attempt_id",
        "resume_lineage")})


def _write_private_prompt(path: str, prompt: str) -> None:
    raw = prompt.encode("utf-8")
    if len(raw) > MAX_PROMPT_BYTES:
        raise ResumeError("resume_prompt_oversized", "resume prompt is oversized")
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.path.lexists(path):
        try:
            current = _read_private_prompt(path)
        except ResumeError:
            raise ResumeError("resume_prompt_conflict", "private resume prompt already exists")
        if not hmac.compare_digest(current.encode("utf-8"), raw):
            raise ResumeError("resume_prompt_conflict", "private resume prompt differs")
        return
    temp = path + "." + uuid.uuid4().hex + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _jobs._replace_with_retry(temp, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _read_private_prompt(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt" and os.path.islink(path):
        raise ResumeError("resume_prompt_untrusted", "private resume prompt is a symlink")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ResumeError("resume_prompt_untrusted", "private resume prompt is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROMPT_BYTES:
            raise ResumeError("resume_prompt_untrusted", "private resume prompt has invalid type or size")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PROMPT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_PROMPT_BYTES:
        raise ResumeError("resume_prompt_untrusted", "private resume prompt is oversized")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResumeError("resume_prompt_untrusted", "private resume prompt is not UTF-8") from exc
    if not value.strip():
        raise ResumeError("resume_prompt_untrusted", "private resume prompt is empty")
    return value


def _permission_meet(source: str, requested: str | None) -> str:
    from _builder import clamp_permission
    if requested is None:
        return source
    if requested not in {"read-only", "safe-edit", "yolo"}:
        raise ResumeError("resume_authority_invalid", "resume permission ceiling is invalid")
    return clamp_permission(source, requested)


def _bounded_duration(value, name: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < 1_000 or value > 7 * 24 * 60 * 60 * 1000):
        raise ResumeError("resume_timeout_invalid", f"{name} is outside the supported range")
    return value


def _compose_prompt(message: str | None, steering: list[dict]) -> str:
    parts = [
        "Continue the authenticated prior session and complete the original task. ",
        "Treat the following as new operator guidance, preserve prior constraints, ",
        "and finish with the required report contract.",
    ]
    for item in steering:
        parts.extend(("\n\nQueued steering:\n", item["message"]))
    if isinstance(message, str) and message.strip():
        parts.extend(("\n\nResume instruction:\n", message.strip()))
    prompt = "".join(parts)
    try:
        raw = prompt.encode("utf-8")
    except UnicodeError as exc:
        raise ResumeError("resume_prompt_invalid", "resume prompt must be UTF-8 text") from exc
    if len(raw) > MAX_PROMPT_BYTES:
        raise ResumeError("resume_prompt_oversized", "resume prompt is oversized")
    return prompt


def _semantic_request(*, source: dict, prompt_sha256: str,
                      message: str | None, steering: list[dict],
                      control_generation: int, control_sha256: str,
                      permission: str, gate_with: str | None,
                      allow_credit: bool, allow_payg: bool,
                      timeout_ms: int, gate_timeout_ms: int,
                      max_runtime_ms: int) -> dict:
    return {
        "source_result_binding_sha256": source["result_binding_sha256"],
        "source_sha256": _digest(source),
        "prompt_contract": PROMPT_CONTRACT,
        "prompt_sha256": prompt_sha256,
        "message_sha256": (hashlib.sha256(message.strip().encode("utf-8")).hexdigest()
                           if isinstance(message, str) and message.strip() else None),
        "steering": [{"generation": item["generation"],
                      "message_sha256": item["message_sha256"]}
                     for item in steering],
        "control_generation": control_generation,
        "control_sha256": control_sha256,
        "permission": permission,
        "gate_with": gate_with,
        "allow_credit": allow_credit,
        "allow_payg": allow_payg,
        "timeout_ms": timeout_ms,
        "gate_timeout_ms": gate_timeout_ms,
        "max_runtime_ms": max_runtime_ms,
    }


def _validate_ledger(value: dict, source_job_id: str, source: dict) -> None:
    expected = {"schema", "source_job_id", "source_attempt_id",
                "source_result_binding_sha256", "source_sha256",
                "generation", "claims", "auth"}
    if (set(value) != expected or value.get("schema") != LEDGER_SCHEMA
            or value.get("source_job_id") != source_job_id
            or value.get("source_attempt_id") != source.get("attempt_id")
            or value.get("source_result_binding_sha256") != source.get("result_binding_sha256")
            or value.get("source_sha256") != _digest(source)
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or not isinstance(value.get("claims"), list)
            or len(value["claims"]) > MAX_CLAIMS
            or value["generation"] != len(value["claims"])):
        raise ResumeError("resume_claim_untrusted", "resume ledger shape or source binding is invalid")
    for ordinal, claim in enumerate(value["claims"], 1):
        if (not isinstance(claim, dict) or set(claim) != _CLAIM_KEYS
                or claim.get("generation") != ordinal
                or not _is_id(claim.get("request_id"))
                or not _is_sha(claim.get("request_sha256"))
                or not _is_id(claim.get("claim_id"))
                or not _is_id(claim.get("successor_job_id"))
                or claim.get("prompt_contract") != PROMPT_CONTRACT
                or not _is_sha(claim.get("prompt_sha256"))
                or not isinstance(claim.get("steering_generations"), list)
                or not isinstance(claim.get("steering_sha256"), list)
                or len(claim["steering_generations"]) != len(claim["steering_sha256"])
                or not all(isinstance(item, int) and not isinstance(item, bool)
                           and item >= 1 for item in claim["steering_generations"])
                or claim["steering_generations"] != sorted(set(claim["steering_generations"]))
                or not all(_is_sha(item) for item in claim["steering_sha256"])
                or not isinstance(claim.get("control_generation"), int)
                or isinstance(claim.get("control_generation"), bool)
                or claim["control_generation"] < 0
                or not _is_sha(claim.get("control_sha256"))
                or any(item > claim["control_generation"]
                       for item in claim["steering_generations"])
                or claim.get("permission") not in {"read-only", "safe-edit", "yolo"}
                or not (claim.get("gate_with") is None
                        or isinstance(claim.get("gate_with"), str)
                        and 0 < len(claim["gate_with"]) <= 256)
                or not isinstance(claim.get("allow_credit"), bool)
                or not isinstance(claim.get("allow_payg"), bool)
                or not isinstance(claim.get("timeout_ms"), int)
                or isinstance(claim.get("timeout_ms"), bool)
                or not isinstance(claim.get("max_runtime_ms"), int)
                or isinstance(claim.get("max_runtime_ms"), bool)
                or not isinstance(claim.get("gate_timeout_ms"), int)
                or isinstance(claim.get("gate_timeout_ms"), bool)
                or not 1_000 <= claim["gate_timeout_ms"] <= 7 * 24 * 60 * 60 * 1000
                or not 1_000 <= claim["timeout_ms"] <= claim["max_runtime_ms"]
                or claim["max_runtime_ms"] > 7 * 24 * 60 * 60 * 1000
                or claim.get("parent_phase") not in {
                    "reserved", "successor_prepared", "child_launch_claimed",
                    "spawned", "spawn_failed", "indeterminate", "terminal"}
                or claim.get("gate_phase") not in {
                    "not_required", "pending", "launch_claimed", "spawned", "reaped",
                    "spawn_failed", "approved", "denied", "error", "indeterminate"}
                or claim.get("provider_phase") not in {
                    "pending", "launch_claimed", "spawned", "reaped",
                    "spawn_failed", "terminal", "indeterminate"}
                or claim.get("provider_contacted") not in {None, True, False}
                or any(item is not None and not _is_sha(item) for item in (
                    claim.get("successor_record_sha256"), claim.get("bundle_sha256"),
                    claim.get("claim_sha256"), claim.get("gate_decision_sha256"),
                    claim.get("terminal_sha256")))
                or (claim.get("terminalization_error_kind") is not None
                    and (not isinstance(claim.get("terminalization_error_kind"), str)
                         or not _ERROR_KIND_RE.fullmatch(
                             claim["terminalization_error_kind"])))
                or any(item is not None and (not isinstance(item, int)
                    or isinstance(item, bool) or item <= 0) for item in (
                    claim.get("pid"), claim.get("gate_pid"), claim.get("provider_pid")))
                or any(not isinstance(claim.get(key), (int, float))
                       or isinstance(claim.get(key), bool)
                       or not math.isfinite(claim[key]) for key in ("created_at", "updated_at"))):
            raise ResumeError("resume_claim_untrusted", "resume ledger claim is invalid")


def _reservation_from_claim(root: str, source: dict, claim: dict,
                            prompt: str, steering: list[dict]) -> Reservation:
    return Reservation(
        root=root, source_job_id=source["job_id"], request_id=claim["request_id"],
        claim_id=claim["claim_id"], successor_job_id=claim["successor_job_id"],
        request_sha256=claim["request_sha256"], prompt=prompt,
        prompt_sha256=claim["prompt_sha256"], source=source,
        steering=tuple(steering), effective_permission=claim["permission"],
        gate_with=claim["gate_with"], allow_credit=claim["allow_credit"],
        allow_payg=claim["allow_payg"], timeout_ms=claim["timeout_ms"],
        gate_timeout_ms=claim["gate_timeout_ms"],
        max_runtime_ms=claim["max_runtime_ms"], created_at=claim["created_at"])


def reserve_request(root: str, source_job_id: str, *, message: str | None = None,
                    request_id: str | None = None,
                    max_permission: str | None = None,
                    gate_with: str | None = None,
                    allow_credit: bool = False, allow_payg: bool = False,
                    timeout_ms: int = 600_000,
                    gate_timeout_ms: int | None = None,
                    max_runtime_ms: int = 24 * 60 * 60 * 1000) -> Reservation:
    """Reserve one idempotent successor without contacting a provider.

    The source-resume lock is always acquired before the source control lock.
    The latter is taken by ``authenticated_steering_commands`` while the former
    remains held, establishing the only multi-lock order used by this feature.
    """
    root = _jobs.resolve_jobs_dir(root)
    if not _jobs.valid_job_id(source_job_id):
        raise ResumeError("invalid_job_id", "source job id is invalid")
    if request_id is not None and not _is_id(request_id):
        raise ResumeError("invalid_request_id", "request id must be 32 lowercase hex characters")
    if not isinstance(allow_credit, bool) or not isinstance(allow_payg, bool):
        raise ResumeError("resume_consent_invalid", "resume spend consent must be explicit booleans")
    timeout_ms = _bounded_duration(timeout_ms, "resume timeout")
    gate_timeout_ms = _bounded_duration(
        timeout_ms if gate_timeout_ms is None else gate_timeout_ms,
        "resume gate timeout")
    max_runtime_ms = _bounded_duration(max_runtime_ms, "resume max runtime")
    if max_runtime_ms < timeout_ms:
        raise ResumeError("resume_timeout_invalid", "resume max runtime is shorter than its checkpoint")
    if gate_with is not None and (not isinstance(gate_with, str)
                                  or not gate_with.strip() or len(gate_with) > 256):
        raise ResumeError("resume_gate_invalid", "resume gate identity is invalid")
    path = ledger_path(root, source_job_id)
    with _exclusive_control_lock(path):
        try:
            source = read_private_source(root, source_job_id)
        except ContinuationError as exc:
            raise ResumeError(exc.kind, str(exc)) from exc
        if source["backend"]["cli"] != "claude" or source["backend"]["transport"] != "subprocess":
            raise ResumeError("resume_backend_unsupported", "only certified Claude subprocess sources can resume")
        if source["backend"].get("profile") is None:
            raise ResumeError("resume_profile_unverified", "governed resume currently requires a named Claude profile")
        permission = _permission_meet(source["authority"]["permission"], max_permission)
        prior_gate = source["gate"].get("agent")
        if prior_gate and gate_with not in {None, prior_gate}:
            raise ResumeError("resume_gate_weakened", "a source gate cannot be replaced during resume")
        effective_gate = prior_gate or gate_with
        record, nonce = _source_record(root, source_job_id)
        if record.get("attempt_id") != source.get("attempt_id"):
            raise ResumeError("source_attempt_mismatch", "source attempt identity changed")
        try:
            control = authenticated_steering_commands(root, source_job_id)
        except ValueError as exc:
            raise ResumeError("resume_steering_untrusted", str(exc)) from exc
        steering = control["steering"]
        prompt = _compose_prompt(message, steering)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        semantic = _semantic_request(
            source=source, prompt_sha256=prompt_sha,
            message=message, steering=steering,
            control_generation=control["generation"],
            control_sha256=control["control_sha256"],
            permission=permission, gate_with=effective_gate,
            allow_credit=allow_credit, allow_payg=allow_payg,
            timeout_ms=timeout_ms, gate_timeout_ms=gate_timeout_ms,
            max_runtime_ms=max_runtime_ms)
        request_sha = _digest(semantic)
        stable_id = request_id or request_sha[:32]
        ledger = _read_authenticated(path, nonce, _ledger_domain(source_job_id),
                                     missing_ok=True)
        if ledger is None:
            ledger = {
                "schema": LEDGER_SCHEMA, "source_job_id": source_job_id,
                "source_attempt_id": source["attempt_id"],
                "source_result_binding_sha256": source["result_binding_sha256"],
                "source_sha256": _digest(source), "generation": 0, "claims": [],
                "auth": "",
            }
        else:
            _validate_ledger(ledger, source_job_id, source)
        for existing in ledger["claims"]:
            if existing["request_id"] == stable_id:
                if existing["request_sha256"] != request_sha:
                    raise ResumeError("resume_claim_conflict", "request id is already bound to different resume inputs")
                if (existing["prompt_contract"] != PROMPT_CONTRACT
                        or existing["prompt_sha256"] != prompt_sha):
                    raise ResumeError("resume_prompt_conflict", "idempotent resume prompt differs")
                return _reservation_from_claim(root, source, existing, prompt, steering)
        # One source handle is a single-use authority. A chain continues from the
        # successor's newly reported handle, never by spending the same source twice.
        if ledger["claims"]:
            raise ResumeError("resume_source_consumed", "source continuation is already bound to a successor")
        if len(ledger["claims"]) >= MAX_CLAIMS:
            raise ResumeError("resume_claim_limit", "resume claim limit reached")
        claim = {
            "generation": ledger["generation"] + 1,
            "request_id": stable_id, "request_sha256": request_sha,
            "claim_id": uuid.uuid4().hex, "successor_job_id": _jobs.new_job_id(),
            "prompt_contract": PROMPT_CONTRACT,
            "prompt_sha256": prompt_sha,
            "steering_generations": [item["generation"] for item in steering],
            "steering_sha256": [item["message_sha256"] for item in steering],
            "control_generation": control["generation"],
            "control_sha256": control["control_sha256"],
            "permission": permission, "gate_with": effective_gate,
            "allow_credit": allow_credit, "allow_payg": allow_payg,
            "timeout_ms": timeout_ms, "max_runtime_ms": max_runtime_ms,
            "gate_timeout_ms": gate_timeout_ms,
            "parent_phase": "reserved",
            "gate_phase": "pending" if effective_gate else "not_required",
            "provider_phase": "pending", "provider_contacted": None,
            "successor_record_sha256": None, "bundle_sha256": None,
            "claim_sha256": None, "pid": None, "terminal_sha256": None,
            "terminalization_error_kind": None,
            "gate_pid": None, "provider_pid": None,
            "gate_decision_sha256": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        ledger["claims"].append(claim)
        ledger["generation"] = len(ledger["claims"])
        body = {key: value for key, value in ledger.items() if key != "auth"}
        _write_authenticated(path, nonce, _ledger_domain(source_job_id), body)
        return _reservation_from_claim(root, source, claim, prompt, steering)


def _mutate_claim(root: str, source_job_id: str, claim_id: str, mutator) -> dict:
    record, nonce = _source_record(root, source_job_id)
    del record
    path = ledger_path(root, source_job_id)
    with _exclusive_control_lock(path):
        source = read_private_source(root, source_job_id)
        ledger = _read_authenticated(path, nonce, _ledger_domain(source_job_id))
        _validate_ledger(ledger, source_job_id, source)
        matches = [item for item in ledger["claims"] if item.get("claim_id") == claim_id]
        if len(matches) != 1:
            raise ResumeError("resume_claim_missing", "resume claim is unavailable")
        mutator(matches[0])
        matches[0]["updated_at"] = time.time()
        _validate_ledger(ledger, source_job_id, source)
        body = {key: value for key, value in ledger.items() if key != "auth"}
        _write_authenticated(path, nonce, _ledger_domain(source_job_id), body)
        return dict(matches[0])


def get_claim(reservation: Reservation) -> dict:
    """Read and authenticate the exact claim reserved by this caller."""
    _record, nonce = _source_record(reservation.root, reservation.source_job_id)
    source = read_private_source(reservation.root, reservation.source_job_id)
    value = _read_authenticated(
        ledger_path(reservation.root, reservation.source_job_id), nonce,
        _ledger_domain(reservation.source_job_id))
    _validate_ledger(value, reservation.source_job_id, source)
    matches = [item for item in value["claims"]
               if item.get("claim_id") == reservation.claim_id]
    if len(matches) != 1:
        raise ResumeError("resume_claim_missing", "resume claim is unavailable")
    claim = matches[0]
    if (claim.get("request_id") != reservation.request_id
            or claim.get("request_sha256") != reservation.request_sha256
            or claim.get("successor_job_id") != reservation.successor_job_id):
        raise ResumeError("resume_claim_untrusted", "resume claim identity differs")
    if _digest(reservation.source) != _digest(source):
        raise ResumeError("resume_reservation_changed", "resume source snapshot differs")
    return dict(claim)


def claim_transition(root: str, source_job_id: str, claim_id: str, *,
                     field: str, expected: str, target: str,
                     updates: dict | None = None) -> dict:
    """Durably compare-and-swap one claim phase."""
    transitions = {
        "parent_phase": {
            ("reserved", "spawn_failed"): {"provider_contacted"},
            ("successor_prepared", "child_launch_claimed"): set(),
            ("spawn_failed", "child_launch_claimed"): {"provider_contacted"},
            ("child_launch_claimed", "spawned"): {"pid"},
            ("child_launch_claimed", "spawn_failed"): {"provider_contacted"},
            ("child_launch_claimed", "indeterminate"): set(),
            ("spawned", "indeterminate"): set(),
        },
        "gate_phase": {
            ("pending", "launch_claimed"): set(),
            ("launch_claimed", "spawned"): {"gate_pid"},
            ("launch_claimed", "indeterminate"): set(),
            ("spawned", "indeterminate"): set(),
        },
        "provider_phase": {
            ("pending", "launch_claimed"): set(),
            ("launch_claimed", "spawned"): {"provider_pid", "provider_contacted"},
            ("launch_claimed", "indeterminate"): set(),
            ("spawned", "indeterminate"): set(),
            ("spawned", "reaped"): set(),
        },
    }
    allowed = transitions.get(field, {}).get((expected, target))
    if allowed is None:
        raise ResumeError("resume_transition_invalid", "resume transition field is invalid")
    if set(updates or {}) != allowed:
        raise ResumeError("resume_transition_invalid", "resume transition update set is invalid")
    def mutate(claim):
        if claim.get(field) != expected:
            raise ResumeError("resume_launch_claimed", "resume transition was already consumed")
        claim[field] = target
        for key, value in (updates or {}).items():
            claim[key] = value
    return _mutate_claim(root, source_job_id, claim_id, mutate)


def prepare_successor(reservation: Reservation, *, successor_record: dict,
                      bundle_sha256: str) -> dict:
    """Publish the immutable private claim after the successor record exists."""
    if not isinstance(reservation, Reservation):
        raise ResumeError("resume_claim_invalid", "resume reservation is invalid")
    if not _is_sha(bundle_sha256):
        raise ResumeError("resume_bundle_invalid", "successor bundle digest is invalid")
    root = reservation.root
    ledger_claim = get_claim(reservation)
    steering_projection = [
        {"generation": item["generation"], "message_sha256": item["message_sha256"]}
        for item in reservation.steering
    ]
    # The dataclass is an in-process convenience, not authority. Every field
    # that can affect execution must exactly reproduce the authenticated ledger.
    if (hashlib.sha256(reservation.prompt.encode("utf-8")).hexdigest()
               != ledger_claim["prompt_sha256"]
            or reservation.prompt_sha256 != ledger_claim["prompt_sha256"]
            or reservation.effective_permission != ledger_claim["permission"]
            or reservation.gate_with != ledger_claim["gate_with"]
            or reservation.allow_credit != ledger_claim["allow_credit"]
            or reservation.allow_payg != ledger_claim["allow_payg"]
            or reservation.timeout_ms != ledger_claim["timeout_ms"]
            or reservation.gate_timeout_ms != ledger_claim["gate_timeout_ms"]
            or reservation.max_runtime_ms != ledger_claim["max_runtime_ms"]
            or steering_projection != [
                {"generation": generation, "message_sha256": digest}
                for generation, digest in zip(
                    ledger_claim["steering_generations"],
                    ledger_claim["steering_sha256"])]):
        raise ResumeError("resume_reservation_changed", "resume reservation differs from its authenticated ledger")
    source_record, source_nonce = _source_record(root, reservation.source_job_id)
    del source_record
    successor_nonce = successor_record.get("nonce")
    if (not isinstance(successor_nonce, str) or not successor_nonce
            or successor_record.get("attempt_id") != reservation.successor_job_id):
        raise ResumeError("resume_successor_untrusted", "successor launch identity is invalid")
    if ((successor_record.get("summon") or {}).get("scripts_sha256") != bundle_sha256
            or successor_record.get("prompt_sha256") != reservation.prompt_sha256):
        raise ResumeError("resume_successor_untrusted", "successor launch provenance differs")
    prompt_file = prompt_path(root, reservation.successor_job_id)
    _write_private_prompt(prompt_file, reservation.prompt)
    body = {
        "schema": CLAIM_SCHEMA,
        "source_job_id": reservation.source_job_id,
        "source_attempt_id": reservation.source["attempt_id"],
        "source_result_binding_sha256": reservation.source["result_binding_sha256"],
        "source_sha256": _digest(reservation.source),
        "request_id": reservation.request_id,
        "request_sha256": reservation.request_sha256,
        "claim_id": reservation.claim_id,
        "successor_job_id": reservation.successor_job_id,
        "successor_attempt_id": reservation.successor_job_id,
        "successor_record_sha256": _record_binding(successor_record),
        "bundle_sha256": bundle_sha256,
        "prompt_sha256": reservation.prompt_sha256,
        "steering": steering_projection,
        "authority": {
            "permission": ledger_claim["permission"],
            "source_permission": reservation.source["authority"]["permission"],
            "read_roots": list(reservation.source["authority"]["read_roots"]),
            "strict_agents_dir": reservation.source["authority"]["strict_agents_dir"],
            "enable_roles": reservation.source["authority"]["enable_roles"],
            "isolated_lane": reservation.source["authority"]["isolated_lane"],
            "allow_tool_credentials": reservation.source["authority"]["allow_tool_credentials"],
            "model_exact_required": True,
            "allow_text_only": reservation.source["authority"]["allow_text_only"],
            "require_tools": reservation.source["authority"]["require_tools"],
            "effort": reservation.source["authority"]["effort"],
        },
        "gate": {"agent": ledger_claim["gate_with"],
                 "timeout_ms": ledger_claim["gate_timeout_ms"]},
        "spend": {"allow_credit": ledger_claim["allow_credit"],
                  "allow_payg": ledger_claim["allow_payg"]},
        "timeout": {"checkpoint_ms": ledger_claim["timeout_ms"],
                    "max_runtime_ms": ledger_claim["max_runtime_ms"]},
        "created_at": reservation.created_at,
    }
    key = _claim_key(source_nonce, successor_nonce)
    value = dict(body, auth=_auth(
        key, _claim_domain(reservation.source_job_id,
                           reservation.successor_job_id), body))
    path = claim_path(root, reservation.successor_job_id)
    with _exclusive_control_lock(path):
        if os.path.lexists(path):
            existing = _read_authenticated(
                path, key, _claim_domain(reservation.source_job_id,
                                         reservation.successor_job_id))
            if existing != value:
                raise ResumeError("resume_claim_conflict", "immutable successor claim differs")
        else:
            _jobs._atomic_write_json(path, value)
    claim_sha = _digest(value)

    def mark_prepared(claim):
        if claim.get("parent_phase") == "successor_prepared":
            if (claim.get("claim_sha256") != claim_sha
                    or claim.get("successor_record_sha256") != body["successor_record_sha256"]
                    or claim.get("bundle_sha256") != bundle_sha256):
                raise ResumeError("resume_claim_conflict", "prepared successor identity differs")
            return
        if claim.get("parent_phase") != "reserved":
            raise ResumeError("resume_launch_claimed", "successor preparation is already consumed")
        claim.update({
            "parent_phase": "successor_prepared",
            "successor_record_sha256": body["successor_record_sha256"],
            "bundle_sha256": bundle_sha256,
            "claim_sha256": claim_sha,
        })

    claim = _mutate_claim(root, reservation.source_job_id,
                          reservation.claim_id, mark_prepared)
    return {"claim_file": path, "prompt_file": prompt_file,
            "claim_sha256": claim_sha, "claim": claim}


def load_child_context(job_file: str, claim_file: str | None = None) -> ChildContext | None:
    """Authenticate private child inputs using the canonical successor job path."""
    claim_file = claim_file or os.environ.get("SUMMON_RESUME_CLAIM_FILE")
    if not claim_file:
        return None
    if not isinstance(job_file, str) or not job_file:
        raise ResumeError("resume_successor_untrusted", "successor result path is unavailable")
    root = os.path.dirname(os.path.abspath(job_file))
    successor_job_id = Path(job_file).stem
    if (not _jobs.valid_job_id(successor_job_id)
            or os.path.abspath(job_file) != _jobs.result_path(root, successor_job_id)
            or os.path.abspath(claim_file) != claim_path(root, successor_job_id)):
        raise ResumeError("resume_successor_untrusted", "successor private path is not canonical")
    successor_record, successor_nonce = _source_record(root, successor_job_id)
    raw, state = _read_strict_json(claim_file)
    if state != _jobs._OK or not isinstance(raw, dict):
        raise ResumeError("resume_claim_untrusted", "successor claim is unavailable")
    source_job_id = raw.get("source_job_id")
    if not _jobs.valid_job_id(source_job_id):
        raise ResumeError("resume_claim_untrusted", "successor claim has invalid source")
    _source_launch, source_nonce = _source_record(root, source_job_id)
    key = _claim_key(source_nonce, successor_nonce)
    value = _read_authenticated(
        claim_file, key, _claim_domain(source_job_id, successor_job_id))
    required = {
        "schema", "source_job_id", "source_attempt_id",
        "source_result_binding_sha256", "source_sha256", "request_id",
        "request_sha256", "claim_id", "successor_job_id", "successor_attempt_id",
        "successor_record_sha256", "bundle_sha256", "prompt_sha256", "steering",
        "authority", "gate", "spend", "timeout", "created_at", "auth",
    }
    if (set(value) != required or value.get("schema") != CLAIM_SCHEMA
            or value.get("successor_job_id") != successor_job_id
            or value.get("successor_attempt_id") != successor_record.get("attempt_id")
            or value.get("successor_record_sha256") != _record_binding(successor_record)
            or value.get("bundle_sha256") !=
               ((successor_record.get("summon") or {}).get("scripts_sha256"))):
        raise ResumeError("resume_claim_untrusted", "successor claim binding is invalid")
    source = read_private_source(root, source_job_id)
    if (value.get("source_attempt_id") != source.get("attempt_id")
            or value.get("source_result_binding_sha256") != source.get("result_binding_sha256")
            or value.get("source_sha256") != _digest(source)):
        raise ResumeError("resume_source_changed", "successor source evidence changed")
    prompt = _read_private_prompt(prompt_path(root, successor_job_id))
    if (hashlib.sha256(prompt.encode("utf-8")).hexdigest() != value.get("prompt_sha256")
            or successor_record.get("prompt_sha256") != value.get("prompt_sha256")):
        raise ResumeError("resume_prompt_changed", "successor prompt binding changed")
    ledger = _read_authenticated(
        ledger_path(root, source_job_id), source_nonce, _ledger_domain(source_job_id))
    _validate_ledger(ledger, source_job_id, source)
    matches = [item for item in ledger["claims"] if item.get("claim_id") == value.get("claim_id")]
    if (len(matches) != 1 or matches[0].get("claim_sha256") != _digest(value)
            or matches[0].get("successor_job_id") != successor_job_id
            or matches[0].get("request_sha256") != value.get("request_sha256")):
        raise ResumeError("resume_claim_untrusted", "successor claim is not in its source ledger")
    ledger_claim = matches[0]
    authority = value.get("authority")
    gate = value.get("gate")
    spend = value.get("spend")
    timeout = value.get("timeout")
    expected_steering = [
        {"generation": generation, "message_sha256": digest}
        for generation, digest in zip(
            ledger_claim["steering_generations"], ledger_claim["steering_sha256"])]
    expected_authority = {
        "permission": ledger_claim["permission"],
        "source_permission": source["authority"]["permission"],
        "read_roots": list(source["authority"]["read_roots"]),
        "strict_agents_dir": source["authority"]["strict_agents_dir"],
        "enable_roles": source["authority"]["enable_roles"],
        "isolated_lane": source["authority"]["isolated_lane"],
        "allow_tool_credentials": source["authority"]["allow_tool_credentials"],
        "model_exact_required": True,
        "allow_text_only": source["authority"]["allow_text_only"],
        "require_tools": source["authority"]["require_tools"],
        "effort": source["authority"]["effort"],
    }
    if (value.get("request_id") != ledger_claim["request_id"]
            or value.get("request_sha256") != ledger_claim["request_sha256"]
            or value.get("prompt_sha256") != ledger_claim["prompt_sha256"]
            or value.get("steering") != expected_steering
            or authority != expected_authority
            or gate != {"agent": ledger_claim["gate_with"],
                        "timeout_ms": ledger_claim["gate_timeout_ms"]}
            or spend != {"allow_credit": ledger_claim["allow_credit"],
                         "allow_payg": ledger_claim["allow_payg"]}
            or timeout != {"checkpoint_ms": ledger_claim["timeout_ms"],
                           "max_runtime_ms": ledger_claim["max_runtime_ms"]}):
        raise ResumeError("resume_claim_untrusted", "successor policy differs from its ledger")
    handle = source.get("continuation", {}).get("handle")
    if not isinstance(handle, str) or not handle:
        raise ResumeError("resume_handle_unavailable", "source continuation handle is unavailable")
    return ChildContext(
        root=root, source_job_id=source_job_id, successor_job_id=successor_job_id,
        claim_id=value["claim_id"], claim_sha256=_digest(value), source=source,
        prompt=prompt, resume_handle=handle,
        effective_permission=authority.get("permission"), gate_with=gate.get("agent"),
        gate_timeout_ms=gate.get("timeout_ms"),
        allow_credit=spend.get("allow_credit") is True,
        allow_payg=spend.get("allow_payg") is True,
        timeout_ms=value["timeout"]["checkpoint_ms"],
        max_runtime_ms=value["timeout"]["max_runtime_ms"])


def validate_loaded_invocation(context: ChildContext, invocation, args, receipt: dict) -> None:
    """Bind the exact resolved child invocation before any gate/provider contact."""
    source = context.source
    agent_def = receipt.get("agent_def") if isinstance(receipt, dict) else None
    profile = receipt.get("profile") if isinstance(receipt, dict) else None
    expected_agent = source["agent"]
    role = (getattr(args, "_role_provenance", {}) or {}).get("role")
    expected_role = None
    if expected_agent["requested"] != expected_agent["resolved"]:
        expected_role = {
            "name": expected_agent["requested"],
            "resolved_agent": expected_agent["resolved"],
            "target_sha256": expected_agent["role_target_sha256"],
            "fingerprint": expected_agent["role_fingerprint"],
            "hash": expected_agent["role_approval_sha256"],
            "registry_sha256": expected_agent["role_registry_sha256"],
        }
    if (invocation.cli != "claude" or invocation.transport != "subprocess"
            or invocation.resume_id != context.resume_handle
            or invocation.cwd != source["workspace"]["path"]
            or invocation.profile != source["backend"]["profile"]
            or not isinstance(profile, dict)
            or profile.get("name") != source["backend"]["profile"]
            or profile.get("path_sha256") != source["backend"]["profile_path_sha256"]
            or profile.get("registry_sha256") != source["backend"]["profile_registry_sha256"]
            or profile.get("command_sha256") != source["backend"]["profile_command_sha256"]
            or invocation.model != source["model"]["targeted"]
            or invocation.permission != context.effective_permission
            or invocation.effort != source["authority"]["effort"]
            or invocation.agent_file != expected_agent["file"]
            or tuple(invocation.read_roots) != tuple(source["authority"]["read_roots"])
            or bool(invocation.isolated_lane) != source["authority"]["isolated_lane"]
            or bool(invocation.allow_tool_credentials) !=
               source["authority"]["allow_tool_credentials"]
            or invocation.model_exact_required is not True
            or not isinstance(agent_def, dict)
            or agent_def.get("sha256") != expected_agent["definition_sha256"]
            or getattr(args, "agent", None) != expected_agent["requested"]
            or getattr(args, "_resolved_agent", None) != expected_agent["resolved"]
            or role != expected_role
            or os.path.abspath(getattr(args, "agents_dir", "")) !=
               os.path.abspath(expected_agent["agents_dir"])
            or bool(getattr(args, "strict_agents_dir", False)) !=
               source["authority"]["strict_agents_dir"]
            or bool(getattr(args, "enable_roles", False)) !=
               source["authority"]["enable_roles"]
            or bool(getattr(args, "allow_credit", False)) != context.allow_credit
            or bool(getattr(args, "allow_payg", False)) != context.allow_payg
            or bool(getattr(args, "allow_text_only", False)) !=
               source["authority"]["allow_text_only"]
            or bool(getattr(args, "require_tools", False)) !=
               source["authority"]["require_tools"]
            or getattr(args, "gate_with", None) != context.gate_with
            or int(getattr(args, "gate_timeout", 0) or 0) != context.gate_timeout_ms
            or int(getattr(args, "timeout", 0) or 0) != context.timeout_ms
            or int(getattr(args, "max_runtime", 0) or 0) != context.max_runtime_ms
            or int(getattr(args, "retries", 0)) != 0
            or not bool(getattr(args, "no_contract_repair", False))
            or not bool(getattr(args, "no_acp_fallback", False))):
        raise ResumeError("resume_child_drift", "resolved successor differs from its authenticated claim")


def provider_launch_control(context: ChildContext, *, gate: bool = False):
    """Return a single-use executor boundary backed by durable CAS."""
    from _executor import ProviderLaunchControl
    field = "gate_phase" if gate else "provider_phase"

    def before_launch(_evidence):
        def mutate(claim):
            if claim.get("parent_phase") not in {"child_launch_claimed", "spawned"}:
                raise ResumeError("resume_parent_not_launched", "successor parent launch is not committed")
            if gate:
                if claim.get("gate_phase") != "pending":
                    raise ResumeError("resume_launch_claimed", "gate launch was already consumed")
                claim["gate_phase"] = "launch_claimed"
            else:
                if claim.get("gate_phase") not in {"not_required", "approved"}:
                    raise ResumeError("resume_gate_not_approved", "resume provider launch lacks gate approval")
                if claim.get("provider_phase") != "pending":
                    raise ResumeError("resume_launch_claimed", "provider launch was already consumed")
                claim["provider_phase"] = "launch_claimed"
        _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)

    def spawned(process):
        pid = getattr(process, "pid", None)
        def mutate(claim):
            if claim.get(field) != "launch_claimed":
                raise ResumeError("resume_launch_claimed", "provider launch phase changed")
            claim[field] = "spawned"
            if gate:
                claim["gate_pid"] = pid if isinstance(pid, int) else None
            else:
                claim["provider_pid"] = pid if isinstance(pid, int) else None
                claim["provider_contacted"] = True
        _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)

    def reaped(_process):
        def mutate(claim):
            if claim.get(field) == "spawned":
                claim[field] = "reaped"
        _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)

    def pre_spawn_failed(_error):
        def mutate(claim):
            if claim.get(field) != "launch_claimed":
                raise ResumeError(
                    "resume_launch_state_invalid",
                    "pre-spawn failure lacks a durable launch claim")
            claim[field] = "spawn_failed"
            if not gate:
                claim["provider_contacted"] = False
        _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)

    def indeterminate(_error):
        def mutate(claim):
            if claim.get(field) in {"launch_claimed", "spawned"}:
                claim[field] = "indeterminate"
        _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)

    return ProviderLaunchControl(before_launch=before_launch,
                                 on_spawn=spawned, on_reap=reaped,
                                 on_pre_spawn_failure=pre_spawn_failed,
                                 on_indeterminate=indeterminate,
                                 allow_secondary=False)


def mark_gate_terminal(context: ChildContext, *, approved: bool,
                       decision_sha256: str | None = None) -> dict:
    if not isinstance(approved, bool) or not _is_sha(decision_sha256):
        raise ResumeError("resume_gate_state_invalid", "gate terminal evidence is invalid")
    target = "approved" if approved else "denied"
    def mutate(claim):
        if claim.get("gate_phase") not in {"spawned", "reaped", "spawn_failed"}:
            raise ResumeError("resume_gate_state_invalid", "gate did not reach a launch state")
        claim["gate_phase"] = target
        claim["gate_decision_sha256"] = decision_sha256
    return _mutate_claim(context.root, context.source_job_id, context.claim_id, mutate)


def mark_terminal(context: ChildContext, result: dict) -> dict:
    """Bind the successor result to the claim after atomic envelope publication."""
    return _mark_terminal_identified(
        context.root, context.source_job_id, context.successor_job_id,
        context.claim_id, context.claim_sha256, result)


def authenticated_lineage(job_file: str) -> dict:
    """Recover public lineage from the authenticated ledger and launch record."""
    if not isinstance(job_file, str) or not job_file:
        raise ResumeError("resume_successor_untrusted", "successor result path is unavailable")
    root = os.path.dirname(os.path.abspath(job_file))
    successor_job_id = Path(job_file).stem
    if (not _jobs.valid_job_id(successor_job_id)
            or os.path.abspath(job_file) != _jobs.result_path(root, successor_job_id)):
        raise ResumeError("resume_successor_untrusted", "successor result path is not canonical")
    record, state = _read_strict_json(_jobs.record_path(root, successor_job_id))
    lineage = record.get("resume_lineage") if isinstance(record, dict) else None
    if state != _jobs._OK or not isinstance(lineage, dict):
        raise ResumeError("resume_successor_untrusted", "successor launch record is unavailable")
    source_job_id = lineage.get("source_job_id")
    claim_id = lineage.get("claim_id")
    if not _jobs.valid_job_id(source_job_id) or not _is_id(claim_id):
        raise ResumeError("resume_successor_untrusted", "successor lineage identity is invalid")
    _source_record_value, nonce = _source_record(root, source_job_id)
    source = read_private_source(root, source_job_id)
    ledger = _read_authenticated(
        ledger_path(root, source_job_id), nonce, _ledger_domain(source_job_id))
    _validate_ledger(ledger, source_job_id, source)
    matches = [item for item in ledger["claims"] if item.get("claim_id") == claim_id]
    if len(matches) != 1:
        raise ResumeError("resume_claim_missing", "successor lineage claim is unavailable")
    claim = matches[0]
    if (claim.get("successor_job_id") != successor_job_id
            or claim.get("request_id") != lineage.get("request_id")
            or claim.get("request_sha256") != lineage.get("request_sha256")
            or claim.get("successor_record_sha256") != _record_binding(record)
            or not _is_sha(claim.get("claim_sha256"))):
        raise ResumeError("resume_claim_untrusted", "successor lineage differs from its ledger")
    return {
        "kind": "governed_resume", "source_job_id": source_job_id,
        "claim_id": claim_id, "claim_sha256": claim["claim_sha256"],
    }


def _mark_terminal_identified(root: str, source_job_id: str,
                              successor_job_id: str, claim_id: str,
                              claim_sha256: str, result: dict) -> dict:
    result_file = _jobs.result_path(root, successor_job_id)
    stored, result_state = _read_strict_json(result_file)
    record, record_state = _read_strict_json(
        _jobs.record_path(root, successor_job_id))
    state, trusted = _jobs._classify(record, record_state, stored, result_state)
    contacted = isinstance(stored, dict) and stored.get("provider_contacted") is True
    attempt_bound = (isinstance(stored, dict)
                     and (stored.get("attempt_id") == successor_job_id
                          or (not contacted and stored.get("attempts") == 0
                              and stored.get("attempt_id") is None)))
    if (not trusted or state in {"corrupt", "unverified", "identity_mismatch"}
            or not isinstance(stored, dict) or _digest(stored) != _digest(result)
            or not attempt_bound
            or stored.get("job_nonce") != record.get("nonce")
            or stored.get("lineage") != {
                "kind": "governed_resume",
                "source_job_id": source_job_id,
                "claim_id": claim_id,
                "claim_sha256": claim_sha256,
            }):
        raise ResumeError("resume_terminal_untrusted", "canonical successor result did not verify")
    terminal_sha = _digest(stored)
    def mutate(claim):
        if claim.get("parent_phase") == "terminal":
            if claim.get("terminal_sha256") != terminal_sha:
                raise ResumeError("resume_terminal_untrusted", "terminal successor identity differs")
            if (claim.get("provider_phase") != "indeterminate"
                    and claim.get("provider_contacted") is not contacted):
                raise ResumeError("resume_terminal_untrusted", "terminal provider identity differs")
            return
        if claim.get("parent_phase") not in {
                "child_launch_claimed", "spawned", "indeterminate"}:
            raise ResumeError("resume_terminal_state_invalid", "successor launch state is invalid")
        provider_indeterminate = claim.get("provider_phase") == "indeterminate"
        if contacted and claim.get("provider_phase") not in {
                "spawned", "reaped", "indeterminate"}:
            raise ResumeError("resume_terminal_state_invalid", "provider contact lacks a durable launch claim")
        if not contacted and claim.get("provider_phase") not in {
                "pending", "spawn_failed", "indeterminate"}:
            raise ResumeError("resume_terminal_state_invalid", "no-contact result conflicts with provider state")
        claim["parent_phase"] = "terminal"
        if not provider_indeterminate:
            claim["provider_phase"] = "terminal"
            claim["provider_contacted"] = contacted
        else:
            # A canonical result proves that the child terminalized; it does not
            # prove whether an ambiguous Popen crossed the provider boundary.
            # Preserve unknown spend provenance and fail closed on any retry.
            claim["provider_contacted"] = None
        claim["terminal_sha256"] = terminal_sha
        claim["terminalization_error_kind"] = None
    return _mutate_claim(root, source_job_id, claim_id, mutate)


def record_terminalization_failure(job_file: str, result: dict,
                                   error_kind: str) -> dict:
    """Durably expose a failed result-to-claim seal without rewriting the result.

    The canonical terminal envelope remains immutable.  This records only a
    bounded typed reason plus its digest in the authenticated source ledger, so a
    later ``jobs resume`` query cannot mistake the child exit for a clean close.
    """
    lineage = authenticated_lineage(job_file)
    root = os.path.dirname(os.path.abspath(job_file))
    successor_job_id = Path(job_file).stem
    stored, result_state = _read_strict_json(job_file)
    record, record_state = _read_strict_json(
        _jobs.record_path(root, successor_job_id))
    state, trusted = _jobs._classify(record, record_state, stored, result_state)
    if (not trusted or state in {"corrupt", "unverified", "identity_mismatch"}
            or not isinstance(stored, dict) or _digest(stored) != _digest(result)):
        raise ResumeError("resume_terminal_untrusted",
                          "canonical successor result did not verify")
    kind = error_kind if isinstance(error_kind, str) else "resume_terminalization_failed"
    kind = kind.strip().lower().replace("-", "_")
    if not _ERROR_KIND_RE.fullmatch(kind):
        kind = "resume_terminalization_failed"
    terminal_sha = _digest(stored)

    def mutate(claim):
        if claim.get("parent_phase") == "terminal":
            return
        if claim.get("parent_phase") not in {
                "child_launch_claimed", "spawned", "indeterminate"}:
            raise ResumeError("resume_terminal_state_invalid",
                              "successor launch state is invalid")
        claim["parent_phase"] = "indeterminate"
        claim["terminal_sha256"] = terminal_sha
        claim["terminalization_error_kind"] = kind

    return _mutate_claim(
        root, lineage["source_job_id"], lineage["claim_id"], mutate)


def mark_terminal_for_job(job_file: str, result: dict) -> dict:
    """Terminalize any governed child, including authenticated pre-provider refusals."""
    lineage = authenticated_lineage(job_file)
    root = os.path.dirname(os.path.abspath(job_file))
    return _mark_terminal_identified(
        root, lineage["source_job_id"], Path(job_file).stem,
        lineage["claim_id"], lineage["claim_sha256"], result)


def reconcile_terminal(reservation: Reservation) -> dict:
    """Provider-inertly repair a claim from its immutable canonical result."""
    claim = get_claim(reservation)
    result_file = _jobs.result_path(reservation.root, reservation.successor_job_id)
    result, state = _read_strict_json(result_file)
    if state != _jobs._OK or not isinstance(result, dict):
        raise ResumeError("resume_terminal_unavailable", "successor terminal result is unavailable")
    lineage = authenticated_lineage(result_file)
    if (lineage.get("source_job_id") != reservation.source_job_id
            or lineage.get("claim_id") != claim.get("claim_id")):
        raise ResumeError("resume_claim_untrusted", "successor terminal claim is unavailable")
    return mark_terminal_for_job(result_file, result)


def governed_source_allowed(context: ChildContext, invocation, result: dict) -> bool:
    """Authorize successor continuation sealing without accepting raw --resume."""
    if not isinstance(context, ChildContext):
        return False
    _record, nonce = _source_record(context.root, context.source_job_id)
    source = read_private_source(context.root, context.source_job_id)
    ledger = _read_authenticated(
        ledger_path(context.root, context.source_job_id), nonce,
        _ledger_domain(context.source_job_id))
    _validate_ledger(ledger, context.source_job_id, source)
    matches = [item for item in ledger["claims"]
               if item.get("claim_id") == context.claim_id]
    if len(matches) != 1:
        return False
    claim = matches[0]
    return bool(
        claim.get("claim_sha256") == context.claim_sha256
        and claim.get("successor_job_id") == context.successor_job_id
        and claim.get("provider_phase") in {"spawned", "reaped"}
        and claim.get("provider_contacted") is True
        and getattr(invocation, "attempt_id", None) == context.successor_job_id
        and getattr(invocation, "resume_id", None) == context.resume_handle
        and result.get("attempt_id") == context.successor_job_id
        and result.get("provider_contacted") is True
        and result.get("attempts") == 1)


def public_projection(claim: dict) -> dict:
    """Allowlisted status; never includes text, paths, handles, or private profile data."""
    return {
        "schema": PUBLIC_SCHEMA,
        "request_id": claim.get("request_id"),
        "claim_id": claim.get("claim_id"),
        "successor_job_id": claim.get("successor_job_id"),
        "parent_phase": claim.get("parent_phase"),
        "gate_phase": claim.get("gate_phase"),
        "provider_phase": claim.get("provider_phase"),
        "provider_contacted": claim.get("provider_contacted"),
        "terminal_result_present": _is_sha(claim.get("terminal_sha256")),
        "terminalization_error_kind": claim.get("terminalization_error_kind"),
        "consumed_steering_generations": list(claim.get("steering_generations") or []),
        "recovery_required": claim.get("parent_phase") == "indeterminate"
                            or claim.get("gate_phase") == "indeterminate"
                            or claim.get("provider_phase") == "indeterminate",
    }


__all__ = [
    "LEDGER_SCHEMA", "CLAIM_SCHEMA", "PUBLIC_SCHEMA", "ResumeError",
    "Reservation", "ChildContext", "ledger_path", "claim_path", "prompt_path",
    "reserve_request", "prepare_successor", "load_child_context",
    "get_claim",
    "validate_loaded_invocation", "provider_launch_control",
    "mark_gate_terminal", "mark_terminal", "mark_terminal_for_job",
    "record_terminalization_failure",
    "authenticated_lineage", "reconcile_terminal", "claim_transition",
    "governed_source_allowed",
    "public_projection",
]
