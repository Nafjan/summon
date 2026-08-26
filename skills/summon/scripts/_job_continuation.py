"""Private authenticated continuation sources for governed background resume.

This module does not launch providers and does not expose a resume command.  It
seals the minimum private authority needed by a later claim/launch layer and
validates it against the immutable source record, terminal result, and workspace
directory object.  Public callers receive only ``public_projection``; session
handles, local paths, profiles, prompts, and steering text never leave the private
job directory.
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
from pathlib import Path

import _jobs
from _resume_capabilities import resume_capability


SCHEMA = "summon.job-continuation-source/v1"
PUBLIC_SCHEMA = "summon.job-continuation/v1"
MAX_PRIVATE_BYTES = 512 * 1024
MAX_JOB_BYTES = 512 * 1024
MAX_HANDLE_CHARS = 4_096

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_DIGEST_RE = re.compile(r"^[0-9a-f]{32}$")
_ROLE_APPROVAL_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_TOP_FIELDS = {
    "schema", "job_id", "attempt_id", "launch_sha256", "request_sha256",
    "prompt_sha256", "scripts_sha256", "result_binding_sha256", "agent", "backend", "model",
    "authority", "spend", "gate", "workspace", "continuation", "terminal",
    "created_at", "auth",
}
_AGENT_FIELDS = {
    "name", "requested", "resolved", "definition_sha256", "source", "file",
    "agents_dir", "role_fingerprint", "role_approval_sha256",
    "role_target_sha256", "role_registry_sha256",
}
_BACKEND_FIELDS = {
    "cli", "transport", "profile", "profile_path_sha256",
    "profile_registry_sha256", "profile_command_sha256", "driver",
    "backend_type", "served_via",
}
_MODEL_FIELDS = {"requested", "targeted", "served", "evidence", "verified"}
_AUTHORITY_FIELDS = {
    "permission", "permission_forced", "permission_ceiling", "read_roots",
    "strict_agents_dir", "enable_roles", "isolated_lane",
    "allow_tool_credentials", "model_exact_required", "model_exact_source",
    "effort", "extra_args", "no_contract_repair",
}
_SPEND_FIELDS = {"billing_source", "source_allow_credit", "source_allow_payg"}
_GATE_FIELDS = {"agent", "decision_sha256"}
_WORKSPACE_FIELDS = {
    "path", "path_sha256", "device", "inode", "reparse_point", "kind",
}
_CONTINUATION_FIELDS = {"kind", "handle"}
_TERMINAL_FIELDS = {
    "status", "execution_status", "provider_contacted", "report_ok", "attempts",
}


class ContinuationError(ValueError):
    """Typed fail-closed continuation-source error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise ContinuationError("invalid_private_source", "continuation data is not canonical JSON") from exc


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _auth(nonce: str, body: dict) -> str:
    return hmac.new(nonce.encode("utf-8"),
                    b"summon-job-continuation-source/v1:" + _canonical(body),
                    hashlib.sha256).hexdigest()


def continuation_path(root: str, job_id: str) -> str:
    base = os.path.dirname(_jobs.record_path(root, job_id))
    return os.path.join(base, f"{job_id}.continuation.json")


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _is_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _is_profile_digest(value) -> bool:
    return isinstance(value, str) and bool(_PROFILE_DIGEST_RE.fullmatch(value))


def _bounded_text(value, *, nullable: bool = False, maximum: int = 4_096) -> bool:
    if nullable and value is None:
        return True
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not _CONTROL_RE.search(value))


def _directory_reparse(st) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def capture_workspace(path: str) -> dict:
    """Capture the exact directory object used by a continuation source."""
    if not isinstance(path, str) or not path:
        raise ContinuationError("workspace_unavailable", "workspace path is missing")
    absolute = os.path.abspath(path)
    try:
        lst = os.lstat(absolute)
    except OSError as exc:
        raise ContinuationError("workspace_unavailable", "workspace cannot be inspected") from exc
    reparse = os.path.islink(absolute) or _directory_reparse(lst)
    if reparse:
        raise ContinuationError("workspace_reparse_point", "workspace root is a reparse point")
    resolved = os.path.realpath(absolute)
    try:
        current = os.stat(resolved)
    except OSError as exc:
        raise ContinuationError("workspace_unavailable", "workspace target cannot be inspected") from exc
    if not stat.S_ISDIR(current.st_mode):
        raise ContinuationError("workspace_unavailable", "workspace is not a directory")
    device = int(current.st_dev)
    inode = int(current.st_ino)
    if device < 0 or inode <= 0:
        raise ContinuationError(
            "workspace_identity_unavailable",
            "filesystem did not expose a stable directory identity")
    normalized = os.path.normcase(resolved) if os.name == "nt" else resolved
    return {
        "path": resolved,
        "path_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "device": device,
        "inode": inode,
        "reparse_point": False,
        "kind": "directory_object",
    }


def workspace_continuity(snapshot: dict) -> tuple[bool, str]:
    """Re-inspect a private snapshot without leaking its path in the result."""
    try:
        current = capture_workspace(snapshot.get("path"))
    except ContinuationError as exc:
        return False, exc.kind
    expected = {key: snapshot.get(key) for key in _WORKSPACE_FIELDS}
    return ((current == expected),
            "same_directory_object" if current == expected else "workspace_identity_changed")


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContinuationError("invalid_private_source", "duplicate JSON key")
        result[key] = value
    return result


def _read_strict(path: str, *, max_bytes: int = MAX_PRIVATE_BYTES,
                 missing_kind: str = "continuation_source_missing",
                 invalid_kind: str = "invalid_private_source") -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt" and os.path.islink(path):
        raise ContinuationError(invalid_kind, "authenticated JSON input is a symlink")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ContinuationError(missing_kind, "authenticated JSON input is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ContinuationError(invalid_kind, "authenticated JSON input has invalid size or type")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise ContinuationError(invalid_kind, "authenticated JSON input is oversized")
    try:
        def strict_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ContinuationError(invalid_kind, "duplicate JSON key")
                result[key] = item
            return result
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=strict_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ContinuationError(invalid_kind, "non-finite JSON number")))
    except ContinuationError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ContinuationError(invalid_kind, "authenticated JSON input is malformed") from exc
    if not isinstance(value, dict):
        raise ContinuationError(invalid_kind, "authenticated JSON input must be an object")
    return value


def _exact_fields(value, fields) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _valid_source_shape(value: dict) -> bool:
    if not _exact_fields(value, _TOP_FIELDS):
        return False
    if value.get("schema") != SCHEMA or not _is_id(value.get("job_id")) \
            or not _is_id(value.get("attempt_id")):
        return False
    if not all(_is_sha(value.get(key)) for key in (
            "launch_sha256", "request_sha256", "prompt_sha256", "scripts_sha256",
            "result_binding_sha256", "auth")):
        return False
    created = value.get("created_at")
    if (not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(created)):
        return False
    agent = value.get("agent")
    if not _exact_fields(agent, _AGENT_FIELDS):
        return False
    if (not _bounded_text(agent.get("name"), maximum=256)
            or not _bounded_text(agent.get("requested"), maximum=256)
            or not _bounded_text(agent.get("resolved"), maximum=256)
            or not _is_sha(agent.get("definition_sha256"))
            or not _bounded_text(agent.get("source"), maximum=64)
            or not _bounded_text(agent.get("file"), maximum=32_768)
            or not _bounded_text(agent.get("agents_dir"), maximum=32_768)
            or not (agent.get("role_fingerprint") is None
                    or _is_sha(agent.get("role_fingerprint")))
            or not (agent.get("role_approval_sha256") is None
                    or (isinstance(agent.get("role_approval_sha256"), str)
                        and _ROLE_APPROVAL_RE.fullmatch(agent["role_approval_sha256"])))
            or not (agent.get("role_target_sha256") is None
                    or _is_sha(agent.get("role_target_sha256")))
            or not (agent.get("role_registry_sha256") is None
                    or _is_sha(agent.get("role_registry_sha256")))):
        return False
    backend = value.get("backend")
    if (not _exact_fields(backend, _BACKEND_FIELDS)
            or not _bounded_text(backend.get("cli"), maximum=64)
            or not _bounded_text(backend.get("transport"), maximum=64)
            or not _bounded_text(backend.get("profile"), nullable=True, maximum=256)
            or backend.get("driver") != "cli"
            or backend.get("backend_type") != "cli"
            or backend.get("served_via") != "cli_agent"
            or not all(item is None or _is_profile_digest(item) for item in (
                backend.get("profile_path_sha256"),
                backend.get("profile_registry_sha256"),
                backend.get("profile_command_sha256")))):
        return False
    model = value.get("model")
    if (not _exact_fields(model, _MODEL_FIELDS)
            or not all(_bounded_text(model.get(key), maximum=512)
                       for key in ("requested", "targeted", "served"))
            or model.get("evidence") != "reported" or model.get("verified") is not True):
        return False
    authority = value.get("authority")
    if not _exact_fields(authority, _AUTHORITY_FIELDS):
        return False
    if (not _bounded_text(authority.get("permission"), maximum=32)
            or not _bounded_text(authority.get("permission_ceiling"), nullable=True, maximum=32)
            or not isinstance(authority.get("read_roots"), list)
            or not all(_bounded_text(item, maximum=32_768) for item in authority["read_roots"])
            or not isinstance(authority.get("extra_args"), list)
            or not all(_bounded_text(item, maximum=8_192) for item in authority["extra_args"])
            or not _bounded_text(authority.get("model_exact_source"), nullable=True, maximum=128)
            or not _bounded_text(authority.get("effort"), nullable=True, maximum=32)):
        return False
    for key in ("permission_forced", "strict_agents_dir", "enable_roles",
                "isolated_lane", "allow_tool_credentials", "model_exact_required",
                "no_contract_repair"):
        if not isinstance(authority.get(key), bool):
            return False
    spend = value.get("spend")
    if (not _exact_fields(spend, _SPEND_FIELDS)
            or not _bounded_text(spend.get("billing_source"), maximum=64)
            or not isinstance(spend.get("source_allow_credit"), bool)
            or not isinstance(spend.get("source_allow_payg"), bool)):
        return False
    gate = value.get("gate")
    if (not _exact_fields(gate, _GATE_FIELDS)
            or not _bounded_text(gate.get("agent"), nullable=True, maximum=256)
            or not (_is_sha(gate.get("decision_sha256"))
                    or gate.get("decision_sha256") is None)):
        return False
    workspace = value.get("workspace")
    if (not _exact_fields(workspace, _WORKSPACE_FIELDS)
            or not _bounded_text(workspace.get("path"), maximum=32_768)
            or not _is_sha(workspace.get("path_sha256"))
            or not isinstance(workspace.get("device"), int)
            or isinstance(workspace.get("device"), bool)
            or not isinstance(workspace.get("inode"), int)
            or isinstance(workspace.get("inode"), bool)
            or workspace.get("reparse_point") is not False
            or workspace.get("kind") != "directory_object"):
        return False
    continuation = value.get("continuation")
    if (not _exact_fields(continuation, _CONTINUATION_FIELDS)
            or continuation.get("kind") != "claude_session"
            or not _bounded_text(continuation.get("handle"), maximum=MAX_HANDLE_CHARS)):
        return False
    terminal = value.get("terminal")
    if (not _exact_fields(terminal, _TERMINAL_FIELDS)
            or not _bounded_text(terminal.get("status"), maximum=64)
            or not _bounded_text(terminal.get("execution_status"), maximum=64)
            or terminal.get("provider_contacted") is not True
            or not isinstance(terminal.get("report_ok"), bool)
            or terminal.get("attempts") != 1):
        return False
    return True


def _terminal_model(result: dict) -> tuple[str, str, str] | None:
    model = result.get("model")
    if not isinstance(model, dict):
        return None
    requested = model.get("requested")
    targeted = model.get("targeted")
    # A continuation handle is authority, so legacy ``resolved`` inference is
    # insufficient. Only an explicit provider-reported served identity qualifies.
    served = model.get("served")
    if (not all(isinstance(item, str) and item for item in (requested, targeted, served))
            or not (requested == targeted == served)
            or result.get("served_model_evidence") != "reported"
            or result.get("model_match") is not True
            or result.get("named_model_verified") is not True):
        return None
    return requested, targeted, served


def _result_binding(result: dict) -> str:
    """Digest the producer-owned receipt projection used by continuation."""
    keys = (
        "attempt_id", "request_sha256", "prompt_sha256", "summon", "agent",
        "agent_requested", "agent_resolved", "role", "agent_def", "cli",
        "backend", "backend_type", "provider", "transport", "profile", "model",
        "served", "served_via",
        "served_model_evidence", "named_model_verified", "resume", "billing",
        "gate", "status", "execution_status", "provider_contacted", "report_ok",
        "attempts", "permission", "permission_forced", "read_allowlist", "effort",
    )
    return _digest({key: result.get(key) for key in keys})


def result_binding_sha256(result: dict) -> str:
    """Return the sidecar-compatible digest for one exact terminal snapshot."""
    if not isinstance(result, dict):
        raise ContinuationError("source_job_untrusted", "terminal snapshot is unavailable")
    return _result_binding(result)


def _unavailable(cli, transport, reason: str) -> dict:
    capability = resume_capability(cli, transport)
    return {
        "schema": PUBLIC_SCHEMA,
        "available": False,
        "resume_state": capability["resume_state"],
        "resume_reason": reason,
        "backend": capability["backend"],
        "transport": capability["transport"],
        "steering_mode": capability["steering_mode"],
        "live_steering_acknowledged": False,
    }


def public_projection(source: dict) -> dict:
    capability = resume_capability(source["backend"]["cli"],
                                   source["backend"]["transport"])
    if capability["resume_state"] != "certified":
        return _unavailable(source["backend"]["cli"],
                            source["backend"]["transport"],
                            capability["resume_reason"])
    return {
        "schema": PUBLIC_SCHEMA,
        "available": True,
        "resume_state": capability["resume_state"],
        "resume_reason": "private_source_authenticated",
        "backend": capability["backend"],
        "transport": capability["transport"],
        "steering_mode": capability["steering_mode"],
        "live_steering_acknowledged": False,
    }


def write_private_source(job_file: str, result: dict, invocation, args) -> dict:
    """Write a source sidecar after one trusted executor result, provider-inertly."""
    capability = resume_capability(invocation.cli, invocation.transport)
    if capability["resume_state"] != "certified":
        return _unavailable(invocation.cli, invocation.transport,
                            capability["resume_reason"])
    if invocation.cli != "claude" or invocation.transport != "subprocess":
        return _unavailable(invocation.cli, invocation.transport,
                            "governed_backend_not_certified")
    if invocation.resume_id:
        return _unavailable(invocation.cli, invocation.transport,
                            "ungoverned_resumed_source")
    if result.get("provider_contacted") is not True or result.get("attempts") != 1:
        return _unavailable(invocation.cli, invocation.transport,
                            "single_contact_source_required")
    if len(_canonical(result)) > MAX_JOB_BYTES:
        return _unavailable(invocation.cli, invocation.transport,
                            "source_receipt_oversized")
    models = _terminal_model(result)
    if models is None:
        return _unavailable(invocation.cli, invocation.transport,
                            "reported_exact_model_required")
    resume = result.get("resume")
    if not isinstance(resume, dict) or resume.get("cli") != invocation.cli:
        return _unavailable(invocation.cli, invocation.transport,
                            "continuation_route_mismatch")
    if result.get("cli") != invocation.cli:
        return _unavailable(invocation.cli, invocation.transport,
                            "continuation_route_mismatch")
    for key, expected in (("backend", invocation.cli),
                          ("transport", invocation.transport)):
        if result.get(key) is not None and result.get(key) != expected:
            return _unavailable(invocation.cli, invocation.transport,
                                "continuation_route_mismatch")
    provider = result.get("provider")
    served = result.get("served")
    if (result.get("backend_type") != "cli"
            or result.get("served_via") != "cli_agent"
            or not isinstance(provider, dict) or provider.get("driver") != "cli"
            or set(provider) != {"driver"}
            or not isinstance(served, dict) or served.get("via") != "cli_agent"
            or set(served) != {"via"}):
        return _unavailable(invocation.cli, invocation.transport,
                            "continuation_route_mismatch")
    handle = resume.get("session_id") if isinstance(resume, dict) else None
    if not _bounded_text(handle, maximum=MAX_HANDLE_CHARS):
        return _unavailable(invocation.cli, invocation.transport,
                            "reported_session_handle_required")
    root = os.path.dirname(os.path.abspath(job_file))
    job_id = Path(job_file).stem
    if not _jobs.valid_job_id(job_id) or os.path.abspath(job_file) != _jobs.result_path(root, job_id):
        raise ContinuationError("invalid_job_identity", "job result path is not canonical")
    record = _read_strict(
        _jobs.record_path(root, job_id), max_bytes=MAX_JOB_BYTES,
        missing_kind="source_record_untrusted", invalid_kind="source_record_untrusted")
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise ContinuationError("source_record_untrusted", "source launch record has no identity")
    attempt_id = result.get("attempt_id")
    if not _is_id(attempt_id) or attempt_id != record.get("attempt_id"):
        raise ContinuationError("source_attempt_mismatch", "source attempt identity does not match")
    scripts_sha = ((result.get("summon") or {}).get("scripts_sha256")
                   if isinstance(result.get("summon"), dict) else None)
    record_scripts = ((record.get("summon") or {}).get("scripts_sha256")
                      if isinstance(record.get("summon"), dict) else None)
    if not _is_sha(scripts_sha) or scripts_sha != record_scripts:
        raise ContinuationError("source_scripts_mismatch", "source executable identity does not match")
    request_sha = result.get("request_sha256")
    prompt_sha = result.get("prompt_sha256")
    agent_def = result.get("agent_def")
    if (not _is_sha(request_sha) or not _is_sha(prompt_sha)
            or not isinstance(agent_def, dict) or not _is_sha(agent_def.get("sha256"))):
        return _unavailable(invocation.cli, invocation.transport,
                            "source_provenance_incomplete")
    if prompt_sha != record.get("prompt_sha256"):
        raise ContinuationError("source_prompt_mismatch",
                                "source prompt identity does not match launch record")
    requested_agent = str(getattr(args, "agent", ""))
    resolved_agent = str(getattr(args, "_resolved_agent", None) or requested_agent)
    if record.get("agent") != requested_agent:
        raise ContinuationError("source_agent_mismatch",
                                "source agent identity does not match launch record")
    role = (getattr(args, "_role_provenance", {}) or {}).get("role")
    role = role if isinstance(role, dict) else {}
    profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
    if requested_agent != resolved_agent:
        if (not role
                or result.get("agent_requested") != requested_agent
                or result.get("agent_resolved") != resolved_agent
                or result.get("role") != role
                or role.get("name") != requested_agent
                or role.get("resolved_agent") != resolved_agent
                or role.get("target_sha256") != agent_def.get("sha256")
                or not _is_sha(role.get("fingerprint"))
                or not (isinstance(role.get("hash"), str)
                        and _ROLE_APPROVAL_RE.fullmatch(role["hash"]))
                or not _is_sha(role.get("target_sha256"))
                or not _is_sha(role.get("registry_sha256"))):
            return _unavailable(invocation.cli, invocation.transport,
                                "approved_role_provenance_required")
    elif role:
        return _unavailable(invocation.cli, invocation.transport,
                            "approved_role_provenance_mismatch")
    if invocation.profile:
        profile_path = next(iter((invocation.profile_env or {}).values()), None)
        expected_path_sha = (hashlib.sha256(str(profile_path).encode("utf-8")).hexdigest()[:32]
                             if profile_path else None)
        expected_command_sha = (
            hashlib.sha256(str(invocation.profile_command).encode("utf-8")).hexdigest()[:32]
            if invocation.profile_command else None)
        if (profile.get("name") != invocation.profile
                or profile.get("cli") != invocation.cli
                or not _is_profile_digest(profile.get("path_sha256"))
                or not _is_profile_digest(profile.get("registry_sha256"))
                or profile.get("path_sha256") != expected_path_sha
                or profile.get("command_sha256") != expected_command_sha):
            return _unavailable(invocation.cli, invocation.transport,
                                "profile_provenance_required")
    elif profile:
        return _unavailable(invocation.cli, invocation.transport,
                            "profile_provenance_mismatch")
    billing = result.get("billing") if isinstance(result.get("billing"), dict) else {}
    gate_decision = result.get("gate")
    gate_digest = _digest(gate_decision) if isinstance(gate_decision, dict) else None
    body = {
        "schema": SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "launch_sha256": _digest({key: value for key, value in record.items()
                                  if key != "nonce"}),
        "request_sha256": request_sha,
        "prompt_sha256": prompt_sha,
        "scripts_sha256": scripts_sha,
        "result_binding_sha256": _result_binding(result),
        "agent": {
            "name": resolved_agent,
            "requested": requested_agent,
            "resolved": resolved_agent,
            "definition_sha256": agent_def["sha256"],
            "source": str(agent_def.get("source") or "unknown"),
            "file": str(invocation.agent_file or ""),
            "agents_dir": str(getattr(args, "agents_dir", None)
                              or os.path.dirname(invocation.agent_file or "")),
            "role_fingerprint": role.get("fingerprint"),
            "role_approval_sha256": role.get("hash"),
            "role_target_sha256": role.get("target_sha256"),
            "role_registry_sha256": role.get("registry_sha256"),
        },
        "backend": {
            "cli": invocation.cli, "transport": invocation.transport,
            "profile": invocation.profile,
            "profile_path_sha256": profile.get("path_sha256"),
            "profile_registry_sha256": profile.get("registry_sha256"),
            "profile_command_sha256": profile.get("command_sha256"),
            "driver": "cli", "backend_type": "cli", "served_via": "cli_agent",
        },
        "model": {"requested": models[0], "targeted": models[1],
                  "served": models[2], "evidence": "reported", "verified": True},
        "authority": {
            "permission": invocation.permission,
            "permission_forced": bool(invocation.permission_forced),
            "permission_ceiling": getattr(args, "max_permission", None),
            "read_roots": list(invocation.read_roots),
            "strict_agents_dir": bool(getattr(args, "strict_agents_dir", False)),
            "enable_roles": bool(getattr(args, "enable_roles", False)),
            "isolated_lane": bool(invocation.isolated_lane),
            "allow_tool_credentials": bool(invocation.allow_tool_credentials),
            "model_exact_required": bool(invocation.model_exact_required),
            "model_exact_source": invocation.model_exact_source,
            "effort": invocation.effort,
            "extra_args": list(invocation.extra_args),
            "no_contract_repair": bool(getattr(args, "no_contract_repair", False)),
        },
        "spend": {
            "billing_source": str(billing.get("source") or "unknown"),
            "source_allow_credit": bool(getattr(args, "allow_credit", False)),
            "source_allow_payg": bool(getattr(args, "allow_payg", False)),
        },
        "gate": {"agent": getattr(args, "gate_with", None),
                 "decision_sha256": gate_digest},
        "workspace": capture_workspace(invocation.cwd),
        "continuation": {"kind": "claude_session", "handle": handle},
        "terminal": {
            "status": str(result.get("status") or "unknown"),
            "execution_status": str(result.get("execution_status") or result.get("status")
                                    or "unknown"),
            "provider_contacted": True,
            "report_ok": bool(result.get("report_ok", False)),
            "attempts": 1,
        },
        "created_at": time.time(),
    }
    body["auth"] = _auth(nonce, body)
    if not _valid_source_shape(body):
        raise ContinuationError("invalid_private_source", "generated continuation source is invalid")
    path = continuation_path(root, job_id)
    # Serialise the existence check, comparison, and atomic publication. Without
    # this lock two terminal writers could both observe absence and each return
    # success while the later replace silently changed the authority handle.
    from _job_control import _exclusive_control_lock
    with _exclusive_control_lock(path):
        if os.path.lexists(path):
            existing = _read_strict(path)
            existing_body = {key: value for key, value in existing.items() if key != "auth"}
            if (not _valid_source_shape(existing)
                    or not hmac.compare_digest(existing.get("auth", ""),
                                               _auth(nonce, existing_body))):
                raise ContinuationError("continuation_source_conflict",
                                        "existing continuation source is not authentic")
            stable_existing = {key: value for key, value in existing.items()
                               if key not in {"auth", "created_at"}}
            stable_new = {key: value for key, value in body.items()
                          if key not in {"auth", "created_at"}}
            if stable_existing != stable_new:
                raise ContinuationError("continuation_source_conflict",
                                        "a different continuation source already exists")
            return public_projection(existing)
        _jobs._atomic_write_json(path, body)
    return public_projection(body)


def read_private_source(root: str, job_id: str) -> dict:
    """Authenticate and bind a source to its launch, terminal result, and workspace."""
    record = _read_strict(
        _jobs.record_path(root, job_id), max_bytes=MAX_JOB_BYTES,
        missing_kind="source_job_untrusted", invalid_kind="source_job_untrusted")
    result = _read_strict(
        _jobs.result_path(root, job_id), max_bytes=MAX_JOB_BYTES,
        missing_kind="source_job_untrusted", invalid_kind="source_job_untrusted")
    record_state = result_state = _jobs._OK
    state, trusted = _jobs._classify(record, record_state, result, result_state)
    if not trusted or state in {"corrupt", "unverified", "identity_mismatch"}:
        raise ContinuationError("source_job_untrusted", "source terminal result is not trusted")
    source = _read_strict(continuation_path(root, job_id))
    if not _valid_source_shape(source):
        raise ContinuationError("invalid_private_source", "continuation source schema is invalid")
    capability = resume_capability(source["backend"]["cli"],
                                   source["backend"]["transport"])
    if capability["resume_state"] != "certified":
        raise ContinuationError("resume_capability_revoked",
                                "source backend is no longer certified for governed resume")
    nonce = record.get("nonce")
    body = {key: value for key, value in source.items() if key != "auth"}
    if not isinstance(nonce, str) or not hmac.compare_digest(source["auth"], _auth(nonce, body)):
        raise ContinuationError("continuation_auth_failed", "continuation source authentication failed")
    if source["job_id"] != job_id or source["attempt_id"] != record.get("attempt_id"):
        raise ContinuationError("source_attempt_mismatch", "continuation source belongs to another attempt")
    if result.get("attempt_id") != source["attempt_id"]:
        raise ContinuationError("source_attempt_mismatch", "terminal attempt identity changed")
    launch_digest = _digest({key: value for key, value in record.items() if key != "nonce"})
    if source["launch_sha256"] != launch_digest:
        raise ContinuationError("source_launch_changed", "source launch record changed")
    if (source["request_sha256"] != result.get("request_sha256")
            or source["prompt_sha256"] != result.get("prompt_sha256")
            or source["prompt_sha256"] != record.get("prompt_sha256")):
        raise ContinuationError("source_request_changed", "source request identity changed")
    expected_terminal = {
        "status": str(result.get("status") or "unknown"),
        "execution_status": str(result.get("execution_status") or result.get("status")
                                or "unknown"),
        "provider_contacted": result.get("provider_contacted"),
        "report_ok": bool(result.get("report_ok", False)),
        "attempts": result.get("attempts"),
    }
    if source["terminal"] != expected_terminal:
        raise ContinuationError("source_terminal_changed",
                                "source terminal evidence changed")
    if source["result_binding_sha256"] != _result_binding(result):
        raise ContinuationError("source_result_changed",
                                "source terminal receipt binding changed")
    models = _terminal_model(result)
    if models is None or tuple(source["model"][key]
                               for key in ("requested", "targeted", "served")) != models:
        raise ContinuationError("source_model_changed", "source model evidence changed")
    resume = result.get("resume")
    if (not isinstance(resume, dict)
            or source["continuation"]["handle"] != resume.get("session_id")):
        raise ContinuationError("source_handle_changed", "source continuation handle changed")
    continuous, reason = workspace_continuity(source["workspace"])
    if not continuous:
        raise ContinuationError(reason, "source workspace continuity could not be proven")
    return source


__all__ = [
    "SCHEMA", "PUBLIC_SCHEMA", "ContinuationError", "capture_workspace",
    "workspace_continuity", "continuation_path", "public_projection",
    "write_private_source", "read_private_source", "result_binding_sha256",
]
