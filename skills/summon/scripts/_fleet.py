"""Provider-inert fleet control-plane helpers.

File and roster reads live here, outside the pure compiler.  This module emits
only redacted structural projections.  Explanations compare candidates with
declared constraints but deliberately never select a route.  It has no
executor/provider imports or launch seam.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from _backend_policy import (
    PERMISSION_ORDER,
    backend_capabilities,
    effective_permission,
    permission_enforcement,
    provider_contact_required,
    route_provider,
)
import _evidence
import _fleet_compile
from _job_continuation import ContinuationError, capture_workspace


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def project_digest(cwd: str) -> str:
    """Bind the current directory object without publishing its local path."""
    try:
        snapshot = capture_workspace(cwd)
    except ContinuationError as exc:
        raise _evidence.EvidenceError(
            f"fleet project identity is unavailable ({exc.kind})") from exc
    public_identity = {
        "path_sha256": snapshot["path_sha256"],
        "device": snapshot["device"],
        "inode": snapshot["inode"],
        "reparse_point": snapshot["reparse_point"],
        "kind": snapshot["kind"],
    }
    return _canonical_digest(public_identity)


def catalog_snapshot(agents: list[dict]) -> tuple[list[dict], str, list[str]]:
    """Reduce a live roster listing to bounded declarative routing metadata."""
    catalog = []
    unavailable = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        seat = agent.get("name")
        backend = agent.get("run_agent")
        permission = agent.get("permission")
        lifecycle = agent.get("lifecycle", "active")
        if not isinstance(seat, str):
            continue
        if (not isinstance(backend, str)
                or permission not in {"read-only", "safe-edit", "yolo"}
                or lifecycle != "active"):
            if _fleet_compile.is_agent_identifier(seat):
                unavailable.append(seat)
            continue
        model = agent.get("model") if isinstance(agent.get("model"), str) else None
        provider, provider_evidence = route_provider(
            backend, agent.get("provider"), model,
            agent.get("provider_endpoint_mode", "none"))
        if provider_evidence == "mismatch":
            unavailable.append(seat)
            continue
        capabilities = backend_capabilities(backend, permission)
        enforcement = permission_enforcement(backend, permission)
        source = agent.get("source") if agent.get("source") in {
            "project", "bundled", "pack", "explicit", "env"} else "unknown"
        entry = {
            "seat": seat,
            "backend": backend,
            "provider": provider,
            "provider_evidence": provider_evidence,
            "model": model,
            "permission": permission,
            "effective_permission": effective_permission(backend, permission),
            "capabilities": capabilities,
            "enforcement": enforcement,
            "requires_spend": provider_contact_required(backend),
            "source": source,
        }
        try:
            catalog.extend(_fleet_compile.validate_catalog([entry]))
        except _evidence.EvidenceError:
            if _fleet_compile.is_agent_identifier(seat):
                unavailable.append(seat)
    normalized = _fleet_compile.validate_catalog(catalog)
    return normalized, _fleet_compile.catalog_digest(normalized), sorted(set(unavailable))


def load_fleet(path: str) -> dict:
    """Load one bounded sealed fleet document without exposing its local path."""
    try:
        with Path(path).open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > 1 << 20:
                raise _evidence.EvidenceError("fleet document exceeds 1048576 bytes")
            raw = handle.read((1 << 20) + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > 1 << 20:
            raise _evidence.EvidenceError("fleet document exceeds 1048576 bytes")
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise _evidence.EvidenceError("fleet document changed while it was read")
    except OSError as exc:
        raise _evidence.EvidenceError("fleet document could not be read") from exc
    value = _evidence.loads(raw, max_bytes=1 << 20)
    if not isinstance(value, dict):
        raise _evidence.EvidenceError("fleet document must be an object")
    _evidence.verify(value)
    if value.get("schema") != _fleet_compile.FLEET_SCHEMA:
        raise _evidence.EvidenceError("fleet document uses an unsupported schema")
    return value


def compile_document(*, fleet: dict, agents: list[dict], cwd: str) -> tuple[dict, list[dict]]:
    catalog, catalog_sha256, unavailable = catalog_snapshot(agents)
    fleet_payload = _evidence.verify(fleet)
    requested = {candidate["seat"]
                 for lane in fleet_payload["lanes"]
                 for candidate in lane["candidates"]}
    invalid_requested = sorted(requested.intersection(unavailable))
    if invalid_requested:
        raise _evidence.EvidenceError(
            "fleet references roster seats without runnable definitions: "
            + ", ".join(invalid_requested))
    plan = _fleet_compile.compile_fleet(
        fleet=fleet,
        catalog=catalog,
        project_sha256=project_digest(cwd),
        catalog_sha256=catalog_sha256,
    )
    return plan, catalog


def proposal(*, lane: str, seats: list[str], agents: list[dict], cwd: str,
             permission_ceiling: str = "read-only",
             provider_allowlist: list[str] | None = None,
             model_allowlist: list[str] | None = None,
             required_capabilities: list[str] | None = None,
             data_boundary: str = "local_sanitized",
             corrective: dict | None = None,
             spend: dict | None = None) -> tuple[dict, dict, dict]:
    fleet = _fleet_compile.build_draft(
        lane=lane,
        seats=seats,
        permission_ceiling=permission_ceiling,
        provider_allowlist=provider_allowlist,
        model_allowlist=model_allowlist,
        required_capabilities=required_capabilities,
        data_boundary=data_boundary,
        corrective=corrective,
        spend=spend,
    )
    plan, _ = compile_document(fleet=fleet, agents=agents, cwd=cwd)
    return report("propose", fleet, plan), fleet, plan


def _lane_projection(lane: dict) -> dict:
    constraints = lane["constraints"]
    return {
        "name": lane["name"],
        "candidate_count": len(lane["candidates"]),
        "candidates": [
            {"seat": candidate["seat"], "priority": candidate["priority"]}
            for candidate in lane["candidates"]
        ],
        "constraints": {
            "provider_allowlist": list(constraints["provider_allowlist"]),
            "model_allowlist": list(constraints["model_allowlist"]),
            "required_capabilities": list(constraints["required_capabilities"]),
            "permission_ceiling": constraints["permission_ceiling"],
            "data_boundary": constraints["data_boundary"],
            "corrective": dict(constraints["corrective"]),
            "spend": dict(constraints["spend"]),
        },
    }


def inspect(fleet: dict) -> dict:
    """Project every approval-relevant draft field without consulting a roster."""
    payload = _evidence.verify(fleet)
    if fleet.get("schema") != _fleet_compile.FLEET_SCHEMA:
        raise _evidence.EvidenceError("fleet document uses an unsupported schema")
    return {
        "status": "success",
        "action": "inspect",
        "provider_contacted": False,
        "authorization": "advisory_only",
        "fleet": {"schema": fleet["schema"], "sha256": fleet["sha256"]},
        "lanes": [_lane_projection(lane) for lane in payload["lanes"]],
        "approval": {"state": "recording_available_not_activated"},
    }


def report(action: str, fleet: dict, plan: dict) -> dict:
    payload = _fleet_compile.verify_plan_for_fleet(plan, fleet)
    return {
        "status": "success",
        "action": action,
        "provider_contacted": False,
        "authorization": "advisory_only",
        "fleet": {"schema": fleet["schema"], "sha256": fleet["sha256"]},
        "plan": {"schema": plan["schema"], "sha256": plan["sha256"]},
        "project_sha256": payload["project_sha256"],
        "catalog_sha256": payload["catalog_sha256"],
        "lanes": [_lane_projection(lane) for lane in payload["lanes"]],
        "approval": {"state": "recording_available_not_activated"},
    }


def explain(*, fleet: dict, plan: dict, catalog: list[dict], lane_name: str) -> dict:
    payload = _fleet_compile.verify_plan_for_fleet(plan, fleet)
    lanes = [lane for lane in payload["lanes"] if lane["name"] == lane_name]
    if len(lanes) != 1:
        raise _evidence.EvidenceError("fleet explain requires one existing lane")
    lane = lanes[0]
    normalized_catalog = _fleet_compile.validate_catalog(catalog)
    if _canonical_digest(normalized_catalog) != payload["catalog_sha256"]:
        raise _evidence.EvidenceError("fleet explanation catalog does not match the plan")
    by_seat = {entry["seat"]: entry for entry in normalized_catalog}
    constraints = lane["constraints"]
    provider_allowlist = set(constraints["provider_allowlist"])
    model_allowlist = set(constraints["model_allowlist"])
    required_capabilities = set(constraints["required_capabilities"])
    candidates = []
    for requested in lane["candidates"]:
        entry = by_seat[requested["seat"]]
        reasons = []
        if provider_allowlist and entry["provider"] not in provider_allowlist:
            reasons.append("provider_not_allowed")
        if entry["provider_evidence"] == "unknown":
            reasons.append("provider_identity_unknown")
        if model_allowlist and entry["model"] not in model_allowlist:
            reasons.append("model_not_allowed")
        if not required_capabilities.issubset(entry["capabilities"]):
            reasons.append("capability_missing")
        if (PERMISSION_ORDER[entry["permission"]]
                > PERMISSION_ORDER[constraints["permission_ceiling"]]):
            reasons.append("permission_ceiling_exceeded")
        if entry["enforcement"] != "enforced":
            reasons.append("permission_not_enforced")
        if entry["requires_spend"] and not any(
                constraints["spend"][field]
                for field in ("subscription", "credit", "payg")):
            reasons.append("provider_contact_not_authorized")
        if constraints["data_boundary"] == "unspecified":
            reasons.append("data_boundary_unspecified")
        candidates.append({
            "seat": entry["seat"],
            "backend": entry["backend"],
            "provider": entry["provider"],
            "provider_evidence": entry["provider_evidence"],
            "model": entry["model"],
            "permission": entry["permission"],
            "effective_permission": entry["effective_permission"],
            "priority": requested["priority"],
            "matches_declared_constraints": not reasons,
            "dispatch_eligible": None,
            "reasons": sorted(set(reasons)),
            "requires_spend": entry["requires_spend"],
            "source": entry["source"],
            "enforcement": entry["enforcement"],
            "capabilities": list(entry["capabilities"]),
        })
    return {
        "status": "success",
        "action": "explain",
        "provider_contacted": False,
        "authorization": "advisory_only",
        "summary": (f"No seat selected: lane {lane_name!r} is a draft and has no "
                    "approved dispatch authority."),
        "fleet": {"schema": fleet["schema"], "sha256": fleet["sha256"]},
        "plan": {"schema": plan["schema"], "sha256": plan["sha256"]},
        "project_sha256": payload["project_sha256"],
        "catalog_sha256": payload["catalog_sha256"],
        "lane": _lane_projection(lane),
        "candidates": candidates,
        "selection": {
            "seat": None,
            "status": "not_authorized",
            "reason": "approval_recorded_not_activated",
        },
        "unknowns": [
            "approval_consumption", "billing_source", "capability_freshness",
            "data_boundary_evidence", "gate",
        ],
        "approval": {"state": "recording_available_not_activated"},
    }


def same_output_target(left: str, right: str) -> bool:
    """Compare explicit local paths without requiring either target to exist."""
    try:
        left_path = os.path.normcase(os.path.realpath(os.path.abspath(left)))
        right_path = os.path.normcase(os.path.realpath(os.path.abspath(right)))
    except (OSError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError("fleet output target could not be normalized") from exc
    return left_path == right_path


def preflight_json_output(path: str) -> None:
    """Reject an occupied or unsafe explicit output before durable mutation."""
    try:
        destination = Path(path)
        if destination.is_symlink():
            raise _evidence.EvidenceError("fleet output refuses a symbolic-link target")
        if destination.exists():
            raise _evidence.EvidenceError("fleet output already exists")
        parent = destination.parent.resolve()
        if parent.exists() and not parent.is_dir():
            raise _evidence.EvidenceError("fleet output parent must be a directory")
    except _evidence.EvidenceError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError("fleet output target could not be inspected") from exc


def write_json(path: str, value: dict) -> None:
    """Atomically create an explicit fleet artifact without clobbering a file."""
    temp_path = None
    try:
        destination = Path(path)
        parent = destination.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        capture_workspace(str(parent))
        if destination.is_symlink():
            raise _evidence.EvidenceError("fleet output refuses a symbolic-link target")
        if destination.exists():
            raise _evidence.EvidenceError("fleet output already exists")
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                   + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(parent), prefix=".summon-fleet-",
                suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-directory hard link publishes the completed bytes atomically
        # and fails if another writer won the final name. Unlike replace, it
        # can never destroy an unrelated dispatch envelope or fleet draft.
        os.link(temp_path, destination)
        os.unlink(temp_path)
        temp_path = None
    except FileExistsError as exc:
        raise _evidence.EvidenceError("fleet output already exists") from exc
    except _evidence.EvidenceError:
        raise
    except (OSError, ContinuationError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError("fleet output could not be written atomically") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
