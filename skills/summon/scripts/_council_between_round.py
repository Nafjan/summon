"""Provider-inert council between-round checkpoint and context admission.

This module owns only the council-specific sidecar contract. It deliberately
does not import a provider or the shared ownership policy beyond the atomic
JSON primitive supplied by _rundir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import _rundir


CHECKPOINT_SCHEMA = "summon.council-between-round/v1"
CONTEXT_SCHEMA = "summon.council-between-round-context/v1"
CHECKPOINT_FILE = "council-between-round-v1.json"
CONTEXT_FILE = "council-between-round-context-v1.json"
MAX_CONTEXT_BYTES = 64 * 1024
MAX_ENTRIES = 64
MAX_ENTRY_CHARS = 4096
_KEY_RE = re.compile(r"^[0-9a-f]{32,128}$")


class CouncilBetweenRoundError(ValueError):
    """The sidecar is missing, stale, malformed, or conflicting."""


def _sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read(path: str) -> dict | None:
    value = _rundir.read_json(path)
    return value if isinstance(value, dict) else None


def checkpoint_path(run_dir: str) -> str:
    return os.path.join(os.path.abspath(run_dir), CHECKPOINT_FILE)


def context_path(run_dir: str) -> str:
    return os.path.join(os.path.abspath(run_dir), CONTEXT_FILE)


def _with_digest(payload: dict) -> dict:
    result = dict(payload)
    # Rewriting a checkpoint must hash the canonical body, not the previous
    # digest.  Without removing the old value first, any legitimate update
    # (for example, a deadline revalidation) produces a self-inconsistent
    # checkpoint that cannot be admitted on the next command.
    result.pop("checkpoint_sha256", None)
    result["checkpoint_sha256"] = _sha(result)
    return result


def _verify_checkpoint(value: dict, *, run_id: str) -> dict:
    if value.get("schema") != CHECKPOINT_SCHEMA or value.get("run_id") != run_id:
        raise CouncilBetweenRoundError("invalid council between-round checkpoint")
    digest = value.get("checkpoint_sha256")
    body = dict(value)
    body.pop("checkpoint_sha256", None)
    if not isinstance(digest, str) or digest != _sha(body):
        raise CouncilBetweenRoundError("council between-round checkpoint digest mismatch")
    required = (
        "source_receipt_sha256", "source_generation", "completed_round",
        "next_round", "original_deadline_unix_ms", "member_timeout_ms",
        "chair_timeout_ms", "members", "chairman", "attempt_ledger",
        "uncertain_spend", "member_definition_sha256",
        "execution_context_sha256", "between_round_context_sha256",
        "round_one_evidence_sha256",
    )
    if any(key not in value for key in required):
        raise CouncilBetweenRoundError("council between-round checkpoint is incomplete")
    if (not isinstance(value["source_receipt_sha256"], str)
            or not isinstance(value["source_generation"], int)
            or not isinstance(value["completed_round"], int)
            or not isinstance(value["next_round"], int)
            or value["next_round"] != value["completed_round"] + 1
            or not isinstance(value["original_deadline_unix_ms"], int)
            or value["original_deadline_unix_ms"] <= 0
            or not isinstance(value["member_timeout_ms"], int)
            or value["member_timeout_ms"] <= 0
            or not isinstance(value["chair_timeout_ms"], int)
            or value["chair_timeout_ms"] <= 0
            or not isinstance(value["members"], list)
            or not isinstance(value["chairman"], str)
            or not isinstance(value["attempt_ledger"], dict)
            or not isinstance(value["member_definition_sha256"], dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in value["member_definition_sha256"].items())
            or not isinstance(value["execution_context_sha256"], str)
            or not isinstance(value["between_round_context_sha256"], str)
            or not isinstance(value["round_one_evidence_sha256"], dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in value["round_one_evidence_sha256"].items())
            or not isinstance(value["uncertain_spend"], bool)):
        raise CouncilBetweenRoundError("invalid council between-round checkpoint fields")
    return value


def read_checkpoint(run_dir: str, run_id: str) -> dict:
    value = _read(checkpoint_path(run_dir))
    if value is None:
        raise CouncilBetweenRoundError("council between-round checkpoint is missing")
    return _verify_checkpoint(value, run_id=run_id)


def write_checkpoint(run_dir: str, payload: dict) -> dict:
    if payload.get("schema") != CHECKPOINT_SCHEMA:
        raise CouncilBetweenRoundError("invalid council checkpoint schema")
    result = _with_digest(payload)
    _rundir.atomic_write_json(checkpoint_path(run_dir), result)
    return result


def _validate_context(value: dict, *, checkpoint: dict,
                     operation_key: str | None = None,
                     persisted: bool = False) -> dict:
    allowed = {"schema", "run_id", "checkpoint_sha256", "checkpoint_generation",
               "operation_key", "entries"}
    if persisted:
        allowed |= {"submitted_at_unix_ms", "writer_generation"}
    if set(value) != allowed or value.get("schema") != CONTEXT_SCHEMA:
        raise CouncilBetweenRoundError("invalid council context packet")
    if value.get("run_id") != checkpoint["run_id"]:
        raise CouncilBetweenRoundError("council context run identity mismatch")
    if value.get("checkpoint_sha256") != checkpoint["checkpoint_sha256"]:
        raise CouncilBetweenRoundError("council context checkpoint mismatch")
    if value.get("checkpoint_generation") != checkpoint["source_generation"]:
        raise CouncilBetweenRoundError("council context generation mismatch")
    key = value.get("operation_key")
    if (not isinstance(key, str) or not _KEY_RE.fullmatch(key)
            or operation_key is not None and key != operation_key):
        raise CouncilBetweenRoundError("council context operation key is invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise CouncilBetweenRoundError("council context entries are invalid")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"kind", "author", "text"}:
            raise CouncilBetweenRoundError("council context entry shape is invalid")
        if (not isinstance(entry["kind"], str) or not entry["kind"]
                or len(entry["kind"]) > 64
                or not isinstance(entry["author"], str) or not entry["author"]
                or len(entry["author"]) > 128
                or not isinstance(entry["text"], str) or not entry["text"].strip()
                or len(entry["text"]) > MAX_ENTRY_CHARS):
            raise CouncilBetweenRoundError("council context entry exceeds bounds")
        normalized.append({
            "kind": entry["kind"], "author": entry["author"],
            "text": entry["text"],
        })
    result = dict(value)
    result["entries"] = normalized
    return result


def read_context(run_dir: str, checkpoint: dict) -> dict | None:
    value = _read(context_path(run_dir))
    if value is None:
        return None
    return _validate_context(value, checkpoint=checkpoint, persisted=True)


def load_context_file(path: str, *, checkpoint: dict,
                      operation_key: str | None = None) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CouncilBetweenRoundError(f"cannot read council context: {exc}") from exc
    if len(raw) > MAX_CONTEXT_BYTES:
        raise CouncilBetweenRoundError("council context exceeds the byte bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouncilBetweenRoundError("council context must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CouncilBetweenRoundError("council context must be an object")
    return _validate_context(value, checkpoint=checkpoint,
                             operation_key=operation_key)


def submit_context(run_dir: str, checkpoint: dict, candidate: dict,
                   *, writer_generation: int) -> dict:
    """Atomically admit one context; identical replay is idempotent."""
    existing = read_context(run_dir, checkpoint)
    if existing is not None:
        if (existing["operation_key"] == candidate["operation_key"]
                and _sha(existing["entries"]) == _sha(candidate["entries"])):
            return existing
        raise CouncilBetweenRoundError("conflicting council context already submitted")
    stored = dict(candidate)
    stored["submitted_at_unix_ms"] = int(time.time() * 1000)
    stored["writer_generation"] = writer_generation
    # The submission record is separate from the immutable checkpoint. Its
    # writer generation therefore cannot invalidate the source checkpoint.
    _rundir.atomic_write_json(context_path(run_dir), stored)
    return stored


def public_projection(run_dir: str, checkpoint: dict) -> dict:
    context = read_context(run_dir, checkpoint)
    return {
        "schema": CHECKPOINT_SCHEMA,
        "run_id": checkpoint["run_id"],
        "status": "context_submitted" if context else "awaiting_context",
        "source_generation": checkpoint["source_generation"],
        "next_round": checkpoint["next_round"],
        "context_sha256": (_sha(context["entries"]) if context else None),
        "context_entry_count": (len(context["entries"]) if context else 0),
        "provider_contacted": False,
        "uncertain_spend": checkpoint["uncertain_spend"],
    }
