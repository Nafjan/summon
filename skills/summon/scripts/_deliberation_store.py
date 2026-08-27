"""Durable storage and local command handlers for headless deliberation.

The journal is authoritative.  Management readers never treat ``state.json``
as evidence.  The narrow fresh read-only lane may initialize a run with
:func:`initialize_run`, append through ``_rundir.journal_append``, and consume
the bounded command inbox; all broader approval/resume/provider paths remain
gated.  Status/replay are lock-free readers; a standalone cancel command only
queues a typed request and does not claim that it was applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping

import _rundir
import _deliberation_replay as _replay
import _deliberation_context as _context
import _deliberation_context_source as _context_source
import _model_catalog
from _deliberation import DeliberationError, HumanCommand, TERMINAL_STATES


SCHEMA_VERSION = 1
MAX_JOURNAL_RECORDS = 10_000
MAX_REPLAY_BYTES = 4 * 1024 * 1024
MAX_PENDING_COMMANDS = 1_024
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DeliberationStoreError(Exception):
    """A deliberation run cannot be read or safely changed."""


def runs_root(args, cwd: str) -> str:
    """Return the deliberation namespace below the existing run root."""
    base = (getattr(args, "run_dir", None) or getattr(args, "results_dir", None)
            or os.environ.get("SUMMON_RUNS_DIR")
            or os.path.join(cwd, ".agents", "runs"))
    return os.path.join(os.path.abspath(base), "deliberations")


def _runs_root_sha256(root: str) -> str:
    """Bind one-run authority to a resolved namespace without recording its path."""
    canonical = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _public_receipt(value: Mapping[str, object]) -> dict:
    """Project only bounded receipt facts needed by the local observer.

    The browser needs policy labels and agent transport labels to explain a
    decision, but it must never receive the raw frozen plan (paths, argv,
    profile names, prompts, or credentials).  Keep this allowlist in the
    store boundary so every observer gets the same redacted envelope.
    """
    keys = (
        "decision_id", "seat_ids", "option_ids", "quorum_rule", "max_attempts",
        "require_human_approval", "rounds", "deadline_unix_ms", "roster_digest",
    )
    projected = {key: value[key] for key in keys if key in value}
    plans = value.get("plan_identity_by_seat")
    catalog_display: dict[str, dict[str, object]] = {}
    if isinstance(plans, Mapping):
        safe_plans: dict[str, dict[str, object]] = {}
        for seat, plan in plans.items():
            if not isinstance(seat, str) or not isinstance(plan, Mapping):
                continue
            safe = {
                key: plan[key] for key in (
                    "cli", "transport", "effective_permission", "authority_class",
                    "custom_agent_definition_digest", "custom_agent_source_digest",
                ) if key in plan and isinstance(plan[key], (str, int, bool))
            }
            if safe:
                safe_plans[seat] = safe
            display_identity = _model_catalog.display_for_hash(
                plan.get("cli"), plan.get("model_sha256"))
            if display_identity is not None:
                catalog_display[seat] = display_identity
        if safe_plans:
            projected["plan_identity_by_seat"] = safe_plans
    # Model identity is derived only from the exact redacted plan hash and the
    # checked-in catalog.  Never trust a receipt-provided display row: otherwise
    # a forged receipt could make the browser claim that an arbitrary model was
    # frontier or served.  Service evidence is a separate hash-only field;
    # absence means "not verified", equality means exact, and a different hash
    # means mismatch.  The raw requested/served model strings never cross this
    # projection boundary.
    served_by_seat = value.get("model_served_sha256_by_seat")
    if not isinstance(served_by_seat, Mapping):
        served_by_seat = {}
    for seat, display_identity in list(catalog_display.items()):
        plan = plans.get(seat) if isinstance(plans, Mapping) else None
        requested_hash = plan.get("model_sha256") if isinstance(plan, Mapping) else None
        served_hash = served_by_seat.get(seat)
        display_identity = dict(display_identity)
        if isinstance(served_hash, str) and _SHA256_RE.fullmatch(served_hash):
            if served_hash == requested_hash:
                display_identity["availability"] = "served_exact"
                display_identity["served_exact"] = True
            else:
                display_identity["availability"] = "served_mismatch"
                display_identity["served_exact"] = False
        else:
            display_identity["availability"] = "catalog_listed"
            display_identity["served_exact"] = False
        catalog_display[seat] = display_identity
    if catalog_display:
        projected["model_display_by_seat"] = catalog_display
    if "durable_context" in value:
        try:
            binding = _context.parse_private_projection_readonly(value["durable_context"])
            projected["durable_context"] = _context.public_projection(binding)
        except _context.ContextFreshnessError as exc:
            raise DeliberationStoreError(
                "deliberation durable context projection is invalid") from exc
    return projected


def _replay_policy(receipt: Mapping[str, object]):
    """Construct the exact replay policy from a canonical receipt.

    Deliberation status/replay must not invent defaults.  Older/minimal
    preview receipts are therefore rejected at this boundary until they are
    migrated by a fresh run initializer.
    """
    required = ("decision_id", "seat_ids", "option_ids", "quorum_rule",
                "max_attempts", "require_human_approval")
    missing = [key for key in required if key not in receipt]
    if missing:
        raise DeliberationStoreError(
            "deliberation receipt is missing canonical replay fields: " +
            ", ".join(missing))
    try:
        # Reuse replay's strict JSON-shape validation.  Converting arbitrary
        # iterables with tuple(value) would turn malformed strings into valid-
        # looking seat/option sequences before the policy constructor sees
        # them.
        seat_ids, option_ids, quorum, max_attempts, approval = (
            _replay._receipt_policy(receipt))
        from _deliberation import DeliberationPolicy
        return DeliberationPolicy(
            receipt["decision_id"], seat_ids, option_ids, quorum,
            max_attempts, approval)
    except (KeyError, TypeError, ValueError, _replay.ReplayError) as exc:
        raise DeliberationStoreError("deliberation receipt policy is invalid") from exc


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
    # Freeze and validate the cross-process schedule before creating any run
    # directory.  Missing or out-of-range values must not become implicit
    # defaults that differ on resume.
    try:
        _replay.receipt_schedule(value)
        # Validate the complete immutable policy at the same preflight boundary.
        # A malformed receipt must fail before path creation or owner acquisition;
        # otherwise a later status read would discover an unusable half-run.
        _replay_policy(value)
        _replay._receipt_metadata(value)
    except _replay.ReplayError as exc:
        raise DeliberationStoreError("deliberation receipt is invalid") from exc
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


def _authoritative_checkpoint(path: str, receipt: Mapping[str, object]):
    """Read tagged journal bytes once and rebuild the replay checkpoint."""
    try:
        tagged, torn = _rundir.journal_read_tagged(path)
    except _rundir.JournalCorruptError as exc:
        raise DeliberationStoreError(f"deliberation journal is corrupt: {exc}") from exc
    if len(tagged) > MAX_JOURNAL_RECORDS:
        raise DeliberationStoreError(
            f"deliberation journal exceeds {MAX_JOURNAL_RECORDS} records")
    encoded = json.dumps([record for _generation, record in tagged],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPLAY_BYTES:
        raise DeliberationStoreError(
            f"deliberation journal exceeds {MAX_REPLAY_BYTES} replay bytes")
    generations = [generation for generation, _record in tagged]
    current = max(generations, default=0) + 1
    if current < 1:
        raise DeliberationStoreError("deliberation journal generation is invalid")
    policy = _replay_policy(receipt)
    try:
        checkpoint = _replay.replay_checkpoint(
            receipt, tagged, current, allow_legacy_context=True)
    except _replay.ReplayError as exc:
        raise DeliberationStoreError(
            "deliberation journal cannot be reconstructed from its receipt") from exc
    return tagged, torn, checkpoint, policy


def _journal_signature(path: str) -> tuple[tuple[str, int, int, int], ...]:
    """Cheap filesystem fingerprint used to detect same-generation appends."""
    values = []
    try:
        for entry in os.scandir(path):
            if not entry.name.startswith("journal-g") or not entry.name.endswith(".jsonl"):
                continue
            stat = entry.stat()
            values.append((entry.name, int(stat.st_size), int(stat.st_mtime_ns),
                           int(getattr(stat, "st_ino", 0))))
    except OSError as exc:
        raise DeliberationStoreError("cannot fingerprint deliberation journal") from exc
    return tuple(sorted(values))


def _project_checkpoint(checkpoint, records: list[dict]) -> dict:
    """Expose only replay-derived control facts plus bounded cleanup evidence."""
    cleanup = None
    for record in checkpoint.transcript_events:
        if record.get("event") == "cleanup_receipt":
            cleanup = {
                "verified": record.get("verified"),
                "clean": record.get("clean"),
            }
    attempts = checkpoint.attempts
    return {
        "state": checkpoint.status.lower(),
        "termination_reason": checkpoint.termination_reason,
        "candidate_option": checkpoint.candidate_option,
        "decision_option": checkpoint.decision_option,
        "physical_attempts": {
            "started": len(attempts),
            "finished": sum(attempt.phase == "finished" for attempt in attempts),
            "indeterminate": sum(attempt.phase != "finished" for attempt in attempts),
        },
        "uncertain_spend": checkpoint.uncertain_spend,
        "stale_attempts": [attempt.attempt_id for attempt in attempts
                           if attempt.phase != "finished"],
        "cleanup": cleanup,
        "replay_digest": checkpoint.digest,
        "schedule_digest": checkpoint.schedule_digest,
        "pending_journal_commands": len(checkpoint.pending_commands),
    }


def _public_transcript(checkpoint, records: list[dict]) -> list[dict]:
    """Return replay's allowlisted transcript, never raw journal records."""
    result = []
    if records and records[0].get("event") == "run_prepared":
        result.append(_replay._public_event(records[0]))
    result.extend(dict(record) for record in checkpoint.transcript_events)
    return result


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
        journal_before = _journal_signature(path)
        receipt = _receipt(path, run_id)
        tagged, torn, checkpoint, _policy = _authoritative_checkpoint(path, receipt)
        records = [record for _generation, record in tagged]
        _verify_receipt_binding(receipt, records)
        after = _rundir.read_owner(path)
        generation_after = _rundir._last_generation(path)
        journal_after = _journal_signature(path)
        # A lock-free reader must not return a receipt that changed while its
        # journal projection was being read.  Retry once; a persistent race is
        # reported as inconsistent rather than presented as a stable view.
        receipt_after = _receipt(path, run_id)
        receipt_consistent = (_rundir.content_sha256(dict(receipt)) ==
                              _rundir.content_sha256(dict(receipt_after)))
        consistent = ((before or {}).get("nonce") == (after or {}).get("nonce")
                      and (before or {}).get("generation") == (after or {}).get("generation")
                      and generation_before == generation_after
                      and journal_before == journal_after
                      and receipt_consistent)
        pending = _pending_commands(path)
        view = {
            "mode": "deliberation-status", "status": "success",
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "current_generation": generation_after,
            "owner": None if after is None else {
                "pid": after.get("pid"), "generation": after.get("generation"),
                "lease_expires": after.get("lease_expires"),
            },
            "receipt": _public_receipt(receipt),
            "projection": _project_checkpoint(checkpoint, records),
            "journal_records": len(records),
            "journal_torn_tail": torn,
            "recovery_required": torn,
            "consistent": consistent and not torn,
            "pending_commands": pending,
        }
        if torn:
            view["status"] = "blocked"
            view["error_kind"] = "torn_tail"
        elif not consistent:
            view["status"] = "blocked"
            view["error_kind"] = "unstable_read"
        if consistent:
            break
    return view


def replay_run(root: str, run_id: str) -> dict:
    """Return a bounded, checksum-verified native-local journal replay."""
    _rundir.validate_run_id(run_id)
    path = run_dir(root, run_id)
    if not os.path.isdir(path):
        raise DeliberationStoreError(f"unknown deliberation run {run_id!r} under {root}")
    # Deliberation replay and status must consume one authoritative read; do
    # not call inspect_run and then reread a different generation.
    for _attempt in range(2):
        before = _rundir.read_owner(path)
        generation_before = _rundir._last_generation(path)
        journal_before = _journal_signature(path)
        receipt = _receipt(path, run_id)
        tagged, torn, checkpoint, _policy = _authoritative_checkpoint(path, receipt)
        records = [record for _generation, record in tagged]
        _verify_receipt_binding(receipt, records)
        after = _rundir.read_owner(path)
        generation_after = _rundir._last_generation(path)
        journal_after = _journal_signature(path)
        receipt_after = _receipt(path, run_id)
        consistent = ((before or {}).get("nonce") == (after or {}).get("nonce")
                      and (before or {}).get("generation") == (after or {}).get("generation")
                      and generation_before == generation_after
                      and journal_before == journal_after
                      and _rundir.content_sha256(dict(receipt)) ==
                      _rundir.content_sha256(dict(receipt_after)) and not torn)
        if consistent or _attempt == 1:
            break
    return {
        "mode": "deliberation-replay",
        "status": "success" if (not torn and consistent) else "blocked",
        "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "consistent": consistent, "journal_torn_tail": torn,
        "recovery_required": torn,
        **({"error_kind": "torn_tail"} if torn else
           ({"error_kind": "unstable_read"} if not consistent else {})),
        "projection": _project_checkpoint(checkpoint, records),
        "records": _public_transcript(checkpoint, records),
        "checkpoint_digest": checkpoint.digest,
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
    # Cancellation creates new authority-bearing command bytes. Historical v1
    # durable context is readable by status/replay only, so authenticate current
    # receipt metadata before even creating the commands directory.
    try:
        path = run_dir(root, run_id)
        receipt = _receipt(path, run_id)
        _replay_policy(receipt)
        _replay._receipt_metadata(receipt)
    except (_replay.ReplayError, DeliberationStoreError, ValueError) as exc:
        raise DeliberationStoreError(
            "deliberation receipt cannot authorize a command") from exc
    status = inspect_run(root, run_id)
    if status.get("recovery_required") or not status.get("consistent"):
        raise DeliberationStoreError(
            "run journal requires repair or a stable read before queuing a command")
    state = status["projection"]["state"].upper()
    if state in {item.value for item in TERMINAL_STATES}:
        raise DeliberationStoreError(f"run {run_id!r} is already terminal ({state})")
    command_id = command_id or ("cmd-" + uuid.uuid4().hex)
    if not _COMMAND_ID_RE.fullmatch(command_id) or ".." in command_id:
        raise DeliberationStoreError("invalid command id")
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
        # Validate operation identifiers before calling helpers whose detailed
        # ValueError includes the raw input.  CLI envelopes are public/native
        # output and must never echo a path-like invalid run id.
        for attr in ("deliberate_open", "deliberate_recover", "deliberate_status",
                     "deliberate_replay", "deliberate_cancel", "deliberate_resume"):
            candidate = getattr(args, attr, None)
            if candidate is not None:
                try:
                    _rundir.validate_run_id(candidate)
                except (TypeError, ValueError):
                    return _error(attr.removeprefix("deliberate_"),
                                  "invalid run id", kind="validation")
        if getattr(args, "deliberate_open", None):
            from _deliberation_browser import BrowserOpenError, ensure_surface, open_url
            try:
                surface = ensure_surface(root, args.deliberate_open)
                browser = open_url(surface["url"],
                                   mode=getattr(args, "browser", "auto") or "auto")
            except BrowserOpenError as exc:
                return _error("open", str(exc), kind="browser_unavailable")
            result = {
                "mode": "deliberation-open", "status": "success",
                "schema_version": SCHEMA_VERSION, "run_id": args.deliberate_open,
                "url": surface["url"], "surface_reused": bool(surface.get("reused")),
                "browser": browser,
            }
            _emit(result, json_mode=bool(args.json))
            return 0
        if getattr(args, "deliberate_recover", None):
            from _deliberation_resume import reconcile_run
            result = reconcile_run(root, args.deliberate_recover)
            _emit(result, json_mode=bool(args.json))
            return 0 if result.get("status") not in {"blocked", "error"} else 1
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
                "deliberation resume is not publicly activated until the one-attempt "
                "adapter is wired through the owner-bound durable coordinator and passes "
                "resume/cancel/cleanup gates; no provider was called",
                kind="integration_pending", status="blocked")
        if getattr(args, "deliberate", False):
            _validate_fresh_args(args)
            return _run_fresh_live(args, root, cwd)
    except DeliberationError as exc:
        return _error("run", str(exc), kind="integration_pending", status="blocked")
    except (DeliberationStoreError, ValueError, OSError) as exc:
        message = str(exc)
        # Management errors may include the configured run root (for example,
        # an unknown run). Keep the structured diagnostic useful without
        # exporting a caller's absolute project path.
        message = message.replace(os.path.abspath(root), "<runs-root>")
        return _error("command", message)
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
    if args.rounds < 1 or args.rounds > _replay.MAX_SCHEDULE_ROUNDS:
        raise ValueError(
            f"--rounds must be an integer in 1..{_replay.MAX_SCHEDULE_ROUNDS}")
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
    if (getattr(args, "context_observation_file", None)
            or getattr(args, "accept_stale_file", None)) and not getattr(args, "context_file", None):
        raise ValueError(
            "--context-observation-file/--accept-stale-file require --context-file")


def _fresh_question(args) -> str:
    """Read the fresh question once, without echoing its bytes in errors."""
    if args.question is not None and args.question_file is not None:
        raise ValueError("give --question or --question-file, not both")
    question = (args.question or "").strip()
    if args.question_file is not None:
        try:
            question = Path(args.question_file).read_text(
                encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("cannot read --question-file") from exc
    if not question:
        raise ValueError("deliberate needs --question or --question-file")
    return question


def _read_bounded_context_file(path: str, label: str) -> bytes:
    """Read one private context input once without following a symlink."""
    if not isinstance(path, str) or not path:
        raise ValueError(f"cannot read {label}")
    if os.name == "nt" and path.replace("/", "\\").startswith("\\\\"):
        raise ValueError(f"{label} must be a local file")
    target = Path(path)
    try:
        before = target.lstat()
        if (target.is_symlink() or not target.is_file()
                or before.st_size > _context.MAX_INPUT_BYTES):
            raise ValueError(f"{label} is not a bounded regular file")
        with target.open("rb") as handle:
            raw = handle.read(_context.MAX_INPUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"cannot read {label}") from exc
    if (len(raw) > _context.MAX_INPUT_BYTES
            or before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or before.st_size != after.st_size):
        raise ValueError(f"{label} changed during readback")
    return raw


def _read_cancel_command(path: str) -> dict | None:
    """Return one safe queued cancel command, or ``None``.

    The inbox is an untrusted cross-process boundary.  The live runner only
    consumes the exact bounded shape written by :func:`queue_cancel`; it never
    treats arbitrary files as a cancellation signal.
    """
    directory = _commands_dir(path)
    try:
        if os.path.islink(directory):
            raise DeliberationStoreError(
                "commands inbox is a symbolic link; refusing it")
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DeliberationStoreError("cannot inspect commands inbox") from exc
    for name in names[:MAX_PENDING_COMMANDS]:
        command_id = name[:-5]
        if not _COMMAND_ID_RE.fullmatch(command_id) or ".." in command_id:
            continue
        target = os.path.join(directory, name)
        try:
            if os.path.islink(target):
                continue
            value = _rundir.read_json(target)
        except OSError:
            continue
        if (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
                and value.get("run_id") == os.path.basename(os.path.normpath(path))
                and value.get("action") == "cancel"
                and value.get("command_id") == command_id
                and value.get("actor") == "human"):
            return value
    return None


def _run_fresh_live(args, root: str, cwd: str) -> int:
    """Run the deliberately narrow public live lane.

    This lane is intentionally stricter than the general deliberation parser:
    it only admits explicit quorum, one round, one physical attempt per seat,
    read-only enforceable seats, and no human-approval pause.  The durable
    receipt is created before the first provider launch and the owner is held
    until the scheduler has cleaned up.  Resume/approval remain separate gates.
    """
    question = _fresh_question(args)
    seats = _csv(getattr(args, "seats", None), "seats")
    if getattr(args, "quorum", None) is None:
        raise ValueError("live deliberation requires an explicit --quorum")
    if getattr(args, "rounds", 1) != 1:
        raise DeliberationError(
            "live deliberation currently permits exactly one round")
    if bool(getattr(args, "require_human_approval", False)):
        raise DeliberationError(
            "human approval is not activated in the fresh live lane; use the observer only")
    if (getattr(args, "text_only_consent", ())
            or getattr(args, "full_authority_consent", ())):
        raise DeliberationError(
            "authority-consent seats are not admitted by the read-only live lane")
    max_attempts = getattr(args, "max_attempts", None)
    if max_attempts != len(seats):
        raise DeliberationError(
            "live deliberation requires --max-attempts equal to the seat count")
    duration_ms = getattr(args, "deadline", None)
    if (isinstance(duration_ms, bool) or not isinstance(duration_ms, int)
            or duration_ms < len(seats)):
        raise ValueError("live deliberation deadline is too short for its seats")

    from _deliberation import DeliberationPolicy
    from _deliberation_invocation import build_invocation_plans
    from _deliberation_live import (MAX_LIVE_TIMEOUT_MS, LiveDeliberationError,
                                    _expected_plan_identity, build_live_scheduler)
    from _deliberation_roster import SeatRequest, freeze_roster

    permission_ceilings = {seat_id: "read-only" for seat_id in seats}
    roster = freeze_roster(
        tuple(SeatRequest(seat_id, seat_id, role="participant")
              for seat_id in seats),
        cwd=cwd,
        agents_dir=getattr(args, "agents_dir", None),
        role_enabled=bool(getattr(args, "enable_roles", False)),
        strict_agents_dir=bool(getattr(args, "strict_agents_dir", False)),
        permission_ceilings=permission_ceilings,
    )
    unsupported = []
    for seat in roster.seats:
        if seat.transport != "subprocess":
            unsupported.append(f"{seat.seat_id}: transport {seat.transport}")
        elif seat.cli in {"kimi", "openai-compat", "arkcli"}:
            unsupported.append(f"{seat.seat_id}: backend {seat.cli}")
        elif (seat.effective_permission != "read-only"
              or seat.authority_class != "enforceable"):
            unsupported.append(f"{seat.seat_id}: permission {seat.effective_permission}")
        elif not seat.executable_sha256:
            unsupported.append(f"{seat.seat_id}: executable evidence unavailable")
    if unsupported:
        raise DeliberationError(
            "live read-only lane could not verify its seats: " + "; ".join(unsupported))
    # A stale-context authority is deliberately one-run. Its fully validated
    # identity must therefore be selected before plan construction; otherwise
    # random internal ids would make the public acceptance path unreachable.
    parsed_context = None
    stale_intent = None
    if getattr(args, "context_file", None):
        parsed_context = _context.parse_context_packet(
            _read_bounded_context_file(args.context_file, "--context-file"))
        if getattr(args, "accept_stale_file", None):
            stale_intent = _read_bounded_context_file(
                args.accept_stale_file, "--accept-stale-file")
    if stale_intent is not None:
        run_id, decision_id = _context.acceptance_identity(stale_intent)
    else:
        run_id = "deliberation-" + uuid.uuid4().hex
        decision_id = "decision-" + uuid.uuid4().hex
    # The decision id is part of every prompt and plan identity. Resolve it
    # before building the plans so the receipt and invocation agree.
    plans = build_invocation_plans(
        roster, decision_id=decision_id, cwd=cwd, ballot_only=True)
    policy = DeliberationPolicy(
        decision_id, tuple(seats), _csv(getattr(args, "options", None), "options"),
        getattr(args, "quorum"), max_attempts, False)
    now_ms = int(time.time() * 1000)
    deadline_unix_ms = now_ms + int(duration_ms)
    timeout_ms = max(1, int(duration_ms) // len(seats))
    if timeout_ms > MAX_LIVE_TIMEOUT_MS:
        raise DeliberationError(
            "live deliberation per-seat timeout exceeds the supported bound")
    lease_sec = max(600.0, duration_ms / 1000.0 + 30.0)
    context_binding = None
    manifest_path = getattr(args, "context_observation_file", None)
    if parsed_context is not None:
        observation = _context_source.observe_source(
            parsed_context, cwd=cwd, unix_now_ms=now_ms,
            manifest_path=manifest_path)
        context_binding = _context.bind_context(
            parsed_context, observation, run_id=run_id,
            decision_id=decision_id, unix_now_ms=now_ms,
            runs_root_sha256=_runs_root_sha256(root),
            accept_stale=stale_intent)
        if context_binding.state == "stale_refused":
            raise DeliberationError(
                "durable context is stale and lacks valid one-run acceptance")
    public_roster = roster.as_dict(native=False)
    receipt = {
        "mode": "deliberation", "schema_version": SCHEMA_VERSION,
        "run_id": run_id, "decision_id": decision_id,
        "question_sha256": hashlib.sha256(
            question.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "seat_ids": list(seats), "option_ids": list(policy.option_ids),
        "quorum_rule": policy.quorum_rule, "max_attempts": max_attempts,
        "require_human_approval": False, "rounds": 1,
        "deadline_unix_ms": deadline_unix_ms, "created_at": time.time(),
        "roster_digest": roster.roster_digest,
        "provider_integration": {"name": "live-deliberation", "version": 1},
        "execution_contract": {
            "timeout_ms": timeout_ms, "retries": False,
            "acp_fallback": False, "gates": False,
            "report_repair": False, "allow_payg": False,
            "allow_secondary": False,
        },
        "plan_identity_by_seat": _expected_plan_identity(roster, plans),
        "consent_hashes": {
            "full_authority": public_roster["full_authority_consent_sha256"],
            "text_only": public_roster["text_only_consent_sha256"],
        },
        # Native-only recovery material.  It is deliberately excluded by
        # _public_receipt; it is required to re-resolve the same roster later.
        "execution_scope": {"cwd": cwd,
                             "agents_dir": getattr(args, "agents_dir", None)},
        "seat_requests": [{"seat_id": seat_id, "agent": seat_id,
                           "role": "participant"} for seat_id in seats],
        "permission_ceilings": permission_ceilings,
    }
    if context_binding is not None:
        receipt["durable_context"] = _context.private_projection(context_binding)
    # Prove the largest non-transcript prompt fits before creating a run or
    # giving any adapter a chance to contact a provider. Runtime transcript
    # projection then trims to the exact remaining byte budget.
    from _deliberation_scheduler import validate_prompt_admission
    validate_prompt_admission(
        question=question, policy=policy,
        seats={seat.seat_id: seat.seat_definition for seat in roster.seats},
        durable_context=context_binding)
    path, owner = initialize_run(root, receipt, lease_sec=lease_sec)
    scheduler = None
    stop_watch = __import__("threading").Event()
    consumed_command: dict | None = None
    command_lock = threading.Lock()
    pending_command: dict | None = None
    watcher_error: list[BaseException] = []

    def take_cancel_command() -> HumanCommand | None:
        nonlocal pending_command
        with command_lock:
            command = pending_command
            pending_command = None
        if command is None:
            return None
        return HumanCommand(sequence=1, action="cancel",
                            command_id=command["command_id"])

    def watch_cancel() -> None:
        nonlocal consumed_command, pending_command
        while not stop_watch.wait(0.05):
            try:
                command = _read_cancel_command(path)
                if command is None or consumed_command is not None:
                    continue
                # Hold the command while the provider unwinds.  The engine
                # appends it only after attempt_finished and immediately before
                # the CANCELLED transition, which keeps replay boundary-atomic.
                with command_lock:
                    consumed_command = command
                    pending_command = command
                scheduler.cancel()
                return
            except BaseException as exc:  # fail closed without killing the owner
                watcher_error.append(exc)
                return

    watcher = threading.Thread(target=watch_cancel,
                               name="summon-deliberation-cancel", daemon=True)
    try:
        if (context_binding is not None
                and context_binding.runs_root_sha256 != _runs_root_sha256(root)):
            raise DeliberationStoreError(
                "durable context run namespace changed before provider launch")
        scheduler = build_live_scheduler(
            owner=owner, receipt=receipt, policy=policy, question=question,
            roster=roster, timeout_ms=timeout_ms,
            clock=time.monotonic, unix_now_ms=now_ms,
            cancel_command=take_cancel_command,
            durable_context=context_binding,
            context_observer=(
                (lambda: _context_source.observe_source(
                    parsed_context, cwd=cwd,
                    unix_now_ms=int(time.time() * 1000),
                    manifest_path=manifest_path))
                if parsed_context is not None else None),
            context_namespace_observer=(
                (lambda: _runs_root_sha256(root))
                if parsed_context is not None else None))
        watcher.start()
        report = scheduler.run()
        if watcher_error:
            raise DeliberationStoreError("live cancel channel failed closed")
        result = report.as_dict()
        result.update({"run_id": run_id, "receipt": _public_receipt(receipt),
                       "cancel": ("applied" if consumed_command
                                  and result.get("state") == "CANCELLED" else "none")})
        if consumed_command and result.get("state") == "CANCELLED":
            try:
                os.unlink(os.path.join(path, "commands",
                                       consumed_command["command_id"] + ".json"))
            except OSError:
                pass
        _emit(result, json_mode=bool(args.json))
        return 0 if result.get("status") == "success" else 1
    except LiveDeliberationError:
        raise
    finally:
        stop_watch.set()
        if watcher.ident is not None:
            watcher.join(timeout=1.0)
        _rundir.release_owner(owner)
