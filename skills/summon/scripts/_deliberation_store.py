"""Durable storage and local command handlers for headless deliberation.

The journal is authoritative.  This module never treats ``state.json`` as
evidence and never launches a provider.  A future scheduler may initialize a
run with :func:`initialize_run`, append through ``_rundir.journal_append``, and
consume the bounded command inbox.  Status/replay are lock-free readers;
cancel only queues a typed command and does not claim that it was applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Mapping

import _rundir
from _deliberation import RunState, TERMINAL_STATES, summarize_attempt_events


SCHEMA_VERSION = 1
MAX_JOURNAL_RECORDS = 10_000
MAX_REPLAY_BYTES = 4 * 1024 * 1024
MAX_PENDING_COMMANDS = 1_024
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DeliberationStoreError(Exception):
    """A deliberation run cannot be read or safely changed."""


def runs_root(args, cwd: str) -> str:
    """Return the deliberation namespace below the existing run root."""
    base = (getattr(args, "run_dir", None) or getattr(args, "results_dir", None)
            or os.environ.get("SUMMON_RUNS_DIR")
            or os.path.join(cwd, ".agents", "runs"))
    return os.path.join(os.path.abspath(base), "deliberations")


def run_dir(root: str, run_id: str) -> str:
    return _rundir.run_path(root, run_id)


def _receipt(path: str, run_id: str) -> dict:
    value = _rundir.read_json(os.path.join(path, "receipt.json"))
    if (not value or value.get("mode") != "deliberation"
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("run_id") != run_id):
        raise DeliberationStoreError(
            f"run {run_id!r} has no valid deliberation receipt.json")
    return value


def _write_projection(path: str, generation: int, projection: dict) -> None:
    """Write only a generation-fenced cache; readers never trust it."""
    _rundir.atomic_write_json(
        _rundir.stage_path(path, generation, "state"), projection)


def initialize_run(root: str, receipt: Mapping[str, object], *,
                   lease_sec: float = 600.0) -> tuple[str, _rundir.Owner]:
    """Create and own a PREPARED run from an already frozen receipt.

    The caller must supply the immutable participant/policy snapshot.  This
    function does not resolve mutable agent definitions and does not dispatch.
    The returned owner remains held so the scheduler can append safely.
    """
    value = dict(receipt)
    run_id = value.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("deliberation receipt needs run_id")
    _rundir.validate_run_id(run_id)
    if value.get("mode") != "deliberation" or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid deliberation receipt mode/schema")
    path = run_dir(root, run_id)
    if os.path.exists(path):
        raise FileExistsError(f"deliberation run already exists: {run_id}")
    owner = _rundir.acquire_owner(path, lease_sec)
    try:
        _rundir.atomic_write_json(os.path.join(path, "receipt.json"), value)
        _rundir.journal_append(path, {
            "event": "run_prepared", "schema_version": SCHEMA_VERSION,
            "generation": owner.generation, "run_id": run_id,
            "receipt_sha256": _rundir.content_sha256(value),
        }, owner=owner)
        projection = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "generation": owner.generation, "decision_status": "prepared",
            "termination_reason": None,
        }
        _write_projection(path, owner.generation, projection)
        return path, owner
    except BaseException:
        _rundir.release_owner(owner)
        raise


def _project(records: list[dict]) -> dict:
    state = RunState.PREPARED.value
    reason = decision = candidate = None
    cleanup = None
    for record in records:
        event = record.get("event")
        if event == "state_transition" and isinstance(record.get("to"), str):
            state = record["to"]
            reason = record.get("reason")
            if record.get("decision_option") is not None:
                decision = record.get("decision_option")
        elif event == "candidate_selected":
            candidate = record.get("option_id")
        elif event == "cleanup_receipt":
            cleanup = {
                "verified": bool(record.get("verified")),
                "clean": bool(record.get("clean")),
                "retained_resources": list(record.get("retained_resources") or []),
            }
    attempts = summarize_attempt_events(records)
    return {
        "state": str(state).lower(), "termination_reason": reason,
        "candidate_option": candidate, "decision_option": decision,
        "physical_attempts": {
            "started": attempts["started"], "finished": attempts["finished"],
            "indeterminate": attempts["indeterminate"],
        },
        "uncertain_spend": attempts["uncertain_spend"],
        "stale_attempts": list(attempts["stale_attempts"]),
        "cleanup": cleanup,
    }


def _read_records(path: str) -> tuple[list[dict], bool]:
    records, torn = _rundir.journal_read(path)
    if len(records) > MAX_JOURNAL_RECORDS:
        raise DeliberationStoreError(
            f"deliberation journal exceeds {MAX_JOURNAL_RECORDS} records")
    encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPLAY_BYTES:
        raise DeliberationStoreError(
            f"deliberation journal exceeds {MAX_REPLAY_BYTES} replay bytes")
    return records, torn


def _verify_receipt_binding(receipt: Mapping[str, object], records: list[dict]) -> None:
    """Ensure the mutable receipt file still matches its durable preparation hash.

    ``receipt.json`` carries the immutable question, seats, options, and policy.
    Reading it without checking the hash would let an operator or stale writer
    change those fields while status/replay still reported a consistent run.
    """
    prepared = next((record for record in records
                     if record.get("event") == "run_prepared"), None)
    expected = prepared.get("receipt_sha256") if prepared else None
    actual = _rundir.content_sha256(dict(receipt))
    if not isinstance(expected, str) or expected != actual:
        raise DeliberationStoreError(
            "deliberation receipt changed after durable preparation; refusing status/replay")


def inspect_run(root: str, run_id: str) -> dict:
    """Read a generation-stable, journal-derived status view."""
    try:
        _rundir.validate_run_id(run_id)
        path = run_dir(root, run_id)
    except ValueError as exc:
        raise DeliberationStoreError(str(exc)) from exc
    if not os.path.isdir(path):
        raise DeliberationStoreError(f"unknown deliberation run {run_id!r} under {root}")
    view: dict = {}
    for _ in range(2):
        before = _rundir.read_owner(path)
        generation_before = _rundir._last_generation(path)
        receipt = _receipt(path, run_id)
        try:
            records, torn = _read_records(path)
        except _rundir.JournalCorruptError as exc:
            raise DeliberationStoreError(f"run {run_id}: {exc}") from exc
        _verify_receipt_binding(receipt, records)
        after = _rundir.read_owner(path)
        generation_after = _rundir._last_generation(path)
        # A lock-free reader must not return a receipt that changed while its
        # journal projection was being read.  Retry once; a persistent race is
        # reported as inconsistent rather than presented as a stable view.
        receipt_after = _receipt(path, run_id)
        receipt_consistent = (_rundir.content_sha256(dict(receipt)) ==
                              _rundir.content_sha256(dict(receipt_after)))
        consistent = ((before or {}).get("nonce") == (after or {}).get("nonce")
                      and (before or {}).get("generation") == (after or {}).get("generation")
                      and generation_before == generation_after
                      and receipt_consistent)
        pending = _pending_commands(path)
        view = {
            "mode": "deliberation-status", "status": "success",
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "run_dir": path, "current_generation": generation_after,
            "owner": None if after is None else {
                "pid": after.get("pid"), "generation": after.get("generation"),
                "lease_expires": after.get("lease_expires"),
            },
            "receipt": {
                "decision_id": receipt.get("decision_id"),
                "seat_ids": list(receipt.get("seat_ids") or []),
                "option_ids": list(receipt.get("option_ids") or []),
                "created_at": receipt_after.get("created_at"),
            },
            "projection": _project(records), "journal_records": len(records),
            "journal_torn_tail": torn, "consistent": consistent,
            "pending_commands": pending,
        }
        if consistent:
            break
    return view


def replay_run(root: str, run_id: str) -> dict:
    """Return a bounded, checksum-verified native-local journal replay."""
    status = inspect_run(root, run_id)
    records, torn = _read_records(status["run_dir"])
    return {
        "mode": "deliberation-replay", "status": "success",
        "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "consistent": status["consistent"], "journal_torn_tail": torn,
        "projection": _project(records), "records": records,
    }


def _commands_dir(path: str) -> str:
    return os.path.join(path, "commands")


def _pending_commands(path: str) -> int:
    directory = _commands_dir(path)
    try:
        if os.path.islink(directory):
            raise DeliberationStoreError("commands inbox is a symbolic link; refusing it")
        return sum(1 for name in os.listdir(directory) if name.endswith(".json"))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise DeliberationStoreError(f"cannot inspect commands inbox: {exc}") from exc


def queue_cancel(root: str, run_id: str, command_id: str | None = None) -> dict:
    """Queue one typed cancel command with exclusive creation.

    This is not an acknowledgement that cancellation happened.  Only the
    scheduler owner may append ``human_command`` and advance durable state.
    """
    status = inspect_run(root, run_id)
    state = status["projection"]["state"].upper()
    if state in {item.value for item in TERMINAL_STATES}:
        raise DeliberationStoreError(f"run {run_id!r} is already terminal ({state})")
    command_id = command_id or ("cmd-" + uuid.uuid4().hex)
    if not _COMMAND_ID_RE.fullmatch(command_id) or ".." in command_id:
        raise DeliberationStoreError("invalid command id")
    path = status["run_dir"]
    directory = _commands_dir(path)
    if os.path.lexists(directory) and os.path.islink(directory):
        raise DeliberationStoreError("commands inbox is a symbolic link; refusing it")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, command_id + ".json")
    enqueue_lock = os.path.join(directory, ".enqueue.lock")
    body = {
        "schema_version": SCHEMA_VERSION, "command_id": command_id,
        "run_id": run_id, "action": "cancel", "actor": "human",
        "queued_at": time.time(),
    }
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        lock_fd = os.open(enqueue_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DeliberationStoreError("deliberation command inbox is busy; retry") from exc
    try:
        os.close(lock_fd)
        if _pending_commands(path) >= MAX_PENDING_COMMANDS:
            raise DeliberationStoreError("deliberation command inbox is full")
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise DeliberationStoreError(f"command id already exists: {command_id}") from exc
    finally:
        try:
            os.unlink(enqueue_lock)
        except OSError:
            pass
    return {
        "mode": "deliberation-cancel", "status": "success", "run_id": run_id,
        "command_id": command_id, "command_status": "queued",
        "applied": False,
        "note": "queued for the scheduler owner; status confirms when cancellation is durable",
    }


def _emit(value: dict, *, json_mode: bool = True) -> None:
    # Deliberation management output is structured even without --json; this
    # avoids an unstable human renderer becoming part of the P1 contract.
    print(json.dumps(value, ensure_ascii=False,
                     indent=None if json_mode else 2))


def _error(operation: str, message: str, *, kind: str = "validation",
           status: str = "error") -> int:
    _emit({"mode": "deliberation-" + operation, "status": status,
           "error_kind": kind, "error": message})
    return 1


def run_command(args) -> int:
    """Handle deliberation commands before ordinary agent validation."""
    cwd = os.path.abspath(getattr(args, "cwd", None) or os.getcwd())
    root = runs_root(args, cwd)
    try:
        if getattr(args, "deliberate_status", None):
            _emit(inspect_run(root, args.deliberate_status), json_mode=bool(args.json))
            return 0
        if getattr(args, "deliberate_replay", None):
            _emit(replay_run(root, args.deliberate_replay), json_mode=bool(args.json))
            return 0
        if getattr(args, "deliberate_cancel", None):
            _emit(queue_cancel(root, args.deliberate_cancel,
                               getattr(args, "command_id", None)),
                  json_mode=bool(args.json))
            return 0
        if getattr(args, "deliberate_resume", None):
            status = inspect_run(root, args.deliberate_resume)
            uncertain = bool(status["projection"]["uncertain_spend"])
            if uncertain and not getattr(args, "retry_indeterminate", False):
                return _error(
                    "resume",
                    "run has an indeterminate physical attempt and uncertain spend; "
                    "resume performs zero calls unless --retry-indeterminate is explicit",
                    kind="uncertain_spend", status="blocked")
            return _error(
                "resume",
                "deliberation resume is not available until the one-attempt executor "
                "adapter and before-spawn fence are integrated; no provider was called",
                kind="integration_pending", status="blocked")
        if getattr(args, "deliberate", False):
            _validate_fresh_args(args)
            return _error(
                "run",
                "deliberation execution is not available until the one-attempt executor "
                "adapter and before-spawn fence are integrated; no run was created and "
                "no provider was called",
                kind="integration_pending", status="blocked")
    except (DeliberationStoreError, ValueError, OSError) as exc:
        return _error("command", str(exc))
    return _error("command", "no deliberation operation selected")


def _csv(value: str | None, label: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in (value or "").split(",") if part.strip())
    if not items:
        raise ValueError(f"--{label} is required")
    if len(set(items)) != len(items):
        raise ValueError(f"--{label} values must be unique")
    return items


def _validate_fresh_args(args) -> None:
    if args.question is not None and args.question_file is not None:
        raise ValueError("give --question or --question-file, not both")
    question = (args.question or "").strip()
    if args.question_file is not None:
        try:
            question = Path(args.question_file).read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read --question-file: {exc}") from exc
    if not question:
        raise ValueError("deliberate needs --question or --question-file")
    seats = _csv(getattr(args, "seats", None), "seats")
    options = _csv(getattr(args, "options", None), "options")
    if not 2 <= len(seats) <= 10:
        raise ValueError("--seats requires 2-10 unique seats")
    if len(options) < 2:
        raise ValueError("--options requires at least two unique options")
    identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    for label, values in (("seats", seats), ("options", options)):
        invalid = [value for value in values if not identifier.fullmatch(value)]
        if invalid:
            raise ValueError(f"--{label} contains invalid id {invalid[0]!r}")
    text_consents = tuple(getattr(args, "text_only_consent", ()) or ())
    authority_consents = tuple(getattr(args, "full_authority_consent", ()) or ())
    for label, values in (("text-only-consent", text_consents),
                          ("full-authority-consent", authority_consents)):
        if len(set(values)) != len(values):
            raise ValueError(f"--{label} seats must be unique")
        unknown = [value for value in values if value not in seats]
        if unknown:
            raise ValueError(f"--{label} names unknown seat {unknown[0]!r}")
    overlap = set(text_consents) & set(authority_consents)
    if overlap:
        raise ValueError(
            f"a seat cannot have both text-only and full-authority consent: "
            f"{sorted(overlap)[0]!r}")
    if args.rounds < 1:
        raise ValueError("--rounds must be positive")
    if args.quorum is not None:
        from _deliberation import resolve_quorum
        try:
            resolve_quorum(len(seats), args.quorum)
        except ValueError as exc:
            raise ValueError(f"invalid deliberation --quorum: {exc}") from exc
    if getattr(args, "max_attempts", None) is None:
        raise ValueError("--max-attempts is required")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    if getattr(args, "deadline", None) is None:
        raise ValueError("--deadline is required")
