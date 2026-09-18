"""Foreground-owned provider-free workspace entry points.

The product entry deliberately has a narrow boundary:

* ``workspace open`` reopens a named persisted workspace only when a durable,
  compatible operator scope is present. It starts no worker, provider, daemon,
  or implicit resume.
* ``workspace inspect`` is a read-only source-backed projection.
* ``workspace create --plan FILE`` is the bounded, provider-free creation path
  for a strict user-authored plan. It creates only dormant operator-message
  destinations; it never selects providers or launches/resumes work.
* ``workspace demo create`` remains an explicit qualification fixture and is
  visibly labelled synthetic; it is not a general product task planner.

Browser bootstrap material is shown only through an interactive TTY on stderr,
never in JSON or the URL.
"""
from __future__ import annotations

import hashlib
import base64
import copy
import hmac
import json
import os
import re
import stat
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _workspace_commands import (ACTIONS, COMMAND_POLICY_SCHEMA,
                                 OperatorCommandAdapter, OperatorDraftAdapter,
                                 OperatorDispositionAdapter,
                                 OperatorLinkedReplacementAdapter,
                                 OperatorMessageAdapter)
from _workspace_content import ContentRef
from _context_target import _final_open_path
from _workspace_demo import ConductorDemo
from _fleet_approval import (_regular_single_link, _reject_reparse_ancestors,
                             _secure_file, _secure_private_root, _verify_private)
from _rundir import OwnershipLostError, validate_run_id
from _swarm_coordinator import SwarmCoordinatorError, SwarmCorruptError
from _workspace_runtime import WorkspaceRuntime, WorkspaceRuntimeError
from _workspace_runtime import goal_plan as _runtime_goal_plan
from _workspace_layout import _publish_new
from _workspace_plan import (PLAN_FILE_NAME, PLAN_HOST_SCHEMA, PLAN_SCHEMA,
                             PLAN_V2_SCHEMA,
                             PROJECT_BINDING_SCHEMA, ROSTER_BINDING_SCHEMA,
                             SCOPE_TTL_MS,
                             WorkspacePlanError, compile_plan, compile_plan_v2,
                             compiled_plan_digest,
                             read_plan_file)
import _workspace_protocol as _workspace_protocol
from _workspace_ui import WorkspaceSurface, WorkspaceUIError
from _workspace_details import (SOURCE_KINDS, WorkspaceDetailAdapter,
                                WorkspaceDetailError)
import _workspace_canonical
from _workspace_client import (WorkspaceClientError,
                               provision_message as workspace_provision_message,
                               refresh as workspace_refresh,
                               refresh_status as workspace_refresh_status,
                               request as workspace_request)


WORKSPACE_ID = "workspace"
OPERATOR_ID = "local-operator"
OPERATOR_TARGET = "operator-target"
OPERATOR_INSTANCE = "operator-receiver"
DEFAULT_OBJECTIVE = (
    "Keep a bounded workspace visible while tasks, messages, and evidence are inspected."
)
HOST_CONFIG_NAME = "workspace-host.json"
HOST_CONFIG_SCHEMA = "summon.workspace-host/v1"
COMMAND_POLICY_MAX_BYTES = 8192
COMMAND_REFRESH_TOKEN_MAX_BYTES = 256
COMMAND_REFRESH_HISTORY_MAX = 8


class WorkspaceEntryError(ValueError):
    """Safe, stable command-facing failure; no paths or source data."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"workspace entry refused ({kind})")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_private_token(path: str) -> str:
    """Read one private control token without exposing its value or path."""
    if type(path) is not str or not path:
        raise WorkspaceEntryError("command_refresh_token_required")
    descriptor = None
    try:
        _reject_reparse_ancestors(path)
        expected = _regular_single_link(path, "workspace command refresh token")
        _verify_private(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        raw = os.read(descriptor, COMMAND_REFRESH_TOKEN_MAX_BYTES + 1)
        after = os.fstat(descriptor)
        current = _regular_single_link(path, "workspace command refresh token")
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                or len(raw) > COMMAND_REFRESH_TOKEN_MAX_BYTES):
            raise ValueError
        token = raw.strip()
        if not token or len(token) > 128 or any(byte < 0x21 or byte > 0x7e for byte in token):
            raise ValueError
        return token.decode("ascii")
    except WorkspaceEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WorkspaceEntryError("command_refresh_token_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_private_token(path: str, token: str) -> None:
    """Create a refresh-token file without replacing an existing user file."""
    if type(path) is not str or not path or type(token) is not str:
        raise WorkspaceEntryError("command_refresh_token_required")
    descriptor = None
    try:
        _reject_reparse_ancestors(path)
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            raise ValueError
        raw = token.encode("ascii")
        if not 1 <= len(raw) <= COMMAND_REFRESH_TOKEN_MAX_BYTES:
            raise ValueError
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o600)
        # Establish and verify privacy while the exclusive new file is empty.
        # A failed protection check must never leave token bytes behind.
        _secure_file(path)
        _verify_private(path, directory=False)
        if os.write(descriptor, raw) != len(raw):
            raise OSError
        os.fsync(descriptor)
        _secure_file(path)
    except FileExistsError:
        raise WorkspaceEntryError("command_refresh_token_exists") from None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WorkspaceEntryError("command_refresh_token_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _replace_private_bytes(path: str, raw: bytes) -> bytes:
    """Atomically replace an already configured private policy file.

    This is an owner-only control-plane operation.  It refuses links and
    identity races, writes through a same-directory temporary file, and
    returns the prior bytes so a failed in-memory refresh can be rolled back.
    Browser/session credentials never reach this helper.
    """
    if type(path) is not str or not path or type(raw) is not bytes:
        raise WorkspaceEntryError("command_policy_unavailable")
    temporary = None
    try:
        _reject_reparse_ancestors(path)
        expected = _regular_single_link(path, "workspace command policy")
        _verify_private(path, directory=False)
        previous = Path(path).read_bytes()
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent) or len(raw) > COMMAND_POLICY_MAX_BYTES:
            raise ValueError
        descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".summon-policy-", suffix=".tmp")
        with os.fdopen(descriptor, "wb") as handle:
            # Harden the newly-created file while it is still empty.  Keep
            # this inside the descriptor-owning context so a failed ACL/link
            # check closes the descriptor before the cleanup below unlinks it.
            # A parent directory may carry inherited permissions; writing
            # policy bytes before securing the temporary would expose them.
            _secure_file(temporary)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        current = _regular_single_link(path, "workspace command policy")
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(current) != identity(expected):
            raise WorkspaceEntryError("command_policy_changed")
        os.replace(temporary, path)
        temporary = None
        _secure_file(path)
        return previous
    except WorkspaceEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WorkspaceEntryError("command_policy_unavailable") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _public_error(exc: BaseException) -> str:
    if isinstance(exc, SwarmCorruptError):
        return "journal_recovery_required"
    if isinstance(exc, OwnershipLostError):
        return "ownership_lost"
    if isinstance(exc, SwarmCoordinatorError):
        return "coordinator_unavailable"
    kind = getattr(exc, "kind", None)
    return kind if isinstance(kind, str) and kind else "workspace_entry_failed"


def _objective(value: Any) -> str:
    if value is None:
        return DEFAULT_OBJECTIVE
    if type(value) is not str or not 1 <= len(value) <= 512:
        raise WorkspaceEntryError("invalid_objective")
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
        raise WorkspaceEntryError("invalid_objective")
    return value


def _port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise WorkspaceEntryError("explicit_port_required")
    return value


def _read_command_policy(path: str, *, workspace: dict, workspace_id: str,
                         run_id: str) -> dict:
    """Read an explicit private command policy; never infer authority from membership."""
    if type(path) is not str or not path:
        raise WorkspaceEntryError("command_policy_required")
    try:
        expected = _regular_single_link(path, "workspace command policy")
        _verify_private(path, directory=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            raw = os.read(descriptor, COMMAND_POLICY_MAX_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = _regular_single_link(path, "workspace command policy")
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                or identity(before) != identity(after) or identity(after) != identity(current)
                or len(raw) > COMMAND_POLICY_MAX_BYTES or not raw.endswith(b"\n")):
            raise ValueError
        value = json.loads(raw[:-1].decode("utf-8"))
    except WorkspaceEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WorkspaceEntryError("command_policy_unavailable") from None
    required = {"schema", "workspace_id", "run_id", "generation", "expires_at_ms",
                "max_active_deliveries", "targets"}
    if (not isinstance(value, dict) or set(value) != required
            or value.get("schema") != COMMAND_POLICY_SCHEMA
            or value.get("workspace_id") != workspace_id
            or value.get("run_id") != run_id
            or type(value.get("generation")) is not int or not 1 <= value["generation"] <= _workspace_protocol.MAX_INT
            or type(value.get("expires_at_ms")) is not int
            or value["expires_at_ms"] <= int(time.time() * 1000)
            or type(value.get("max_active_deliveries")) is not int
            or not 1 <= value["max_active_deliveries"] <= 128
            or type(value.get("targets")) is not list
            or not 1 <= len(value["targets"]) <= 128):
        raise WorkspaceEntryError("command_policy_invalid")
    deliveries = workspace.get("deliveries")
    if not isinstance(deliveries, dict):
        raise WorkspaceEntryError("command_policy_invalid")
    seen = set()
    normalized = []
    for item in value["targets"]:
        if (not isinstance(item, dict) or set(item) != {"delivery_id", "actions"}
                or type(item["delivery_id"]) is not str
                or type(item["actions"]) is not list
                or not 1 <= len(item["actions"]) <= len(ACTIONS)
                or item["delivery_id"] in seen
                or item["delivery_id"] not in deliveries
                or any(type(action) is not str or action not in ACTIONS
                       for action in item["actions"])
                or len(set(item["actions"])) != len(item["actions"])):
            raise WorkspaceEntryError("command_policy_invalid")
        try:
            _workspace_protocol._id(item["delivery_id"])
        except (ValueError, TypeError):
            raise WorkspaceEntryError("command_policy_invalid") from None
        seen.add(item["delivery_id"])
        normalized.append({"delivery_id": item["delivery_id"],
                           "actions": list(item["actions"])})
    if len(normalized) > value["max_active_deliveries"]:
        raise WorkspaceEntryError("command_policy_invalid")
    return {**value, "targets": normalized}


def _command_scope(policy: dict, *, workspace_id: str, run_id: str) -> dict:
    return {"workspace_id": workspace_id, "run_id": run_id,
            "targets": copy.deepcopy(policy["targets"])}


def _host_config_path(runtime: WorkspaceRuntime) -> str:
    path = os.path.join(runtime._coordinator.run_dir, HOST_CONFIG_NAME)
    _reject_reparse_ancestors(path)
    return path


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: Any, expected: int) -> bytes:
    if type(value) is not str or not 1 <= len(value) <= 128 or not value.isascii():
        raise WorkspaceEntryError("host_config_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError):
        raise WorkspaceEntryError("host_config_invalid") from None
    if len(raw) != expected:
        raise WorkspaceEntryError("host_config_invalid")
    return raw


def _host_config_value(workspace_id: str, run_id: str, presentation_key: bytes,
                       retention_secret: bytes, key_epoch: str) -> dict:
    return {
        "schema": HOST_CONFIG_SCHEMA,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "operator_id": OPERATOR_ID,
        "presentation_key_b64": _b64(presentation_key),
        "retention_secret_b64": _b64(retention_secret),
        "key_epoch": key_epoch,
    }


def _write_host_config(runtime: WorkspaceRuntime, value: dict) -> None:
    path = _host_config_path(runtime)
    parent = os.path.dirname(path)
    _secure_private_root(parent)
    raw = _canonical(value) + b"\n"
    if len(raw) > 4096 or os.path.lexists(path):
        raise WorkspaceEntryError("host_config_exists")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _secure_file(path)
    except (OSError, ValueError):
        raise WorkspaceEntryError("host_config_write_failed") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_plan_file(runtime: WorkspaceRuntime, raw: bytes) -> None:
    path = os.path.join(runtime._coordinator.run_dir, PLAN_FILE_NAME)
    try:
        _reject_reparse_ancestors(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                if stream.write(raw) != len(raw):
                    raise OSError("short plan write")
                stream.flush()
                os.fsync(stream.fileno())
            _secure_file(path)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        raise WorkspaceEntryError("plan_copy_write_failed") from None


def _plan_host_config_value(workspace_id: str, run_id: str, presentation_key: bytes,
                            retention_secret: bytes, key_epoch: str,
                            raw_sha256: str, compiled_sha256: str,
                            operator_expiry_ms: int, bindings: dict,
                            plan_schema: str = PLAN_SCHEMA) -> dict:
    return {
        "schema": PLAN_HOST_SCHEMA,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "operator_id": OPERATOR_ID,
        "presentation_key_b64": _b64(presentation_key),
        "retention_secret_b64": _b64(retention_secret),
        "key_epoch": key_epoch,
        "plan_schema": plan_schema,
        "plan_file": PLAN_FILE_NAME,
        "plan_sha256": raw_sha256,
        "plan_compiled_sha256": compiled_sha256,
        "operator_expiry_ms": operator_expiry_ms,
        "project_binding_schema": PROJECT_BINDING_SCHEMA,
        "project_binding_kind": bindings["project_kind"],
        "project_binding_sha256": bindings["project_sha256"],
        "roster_binding_schema": ROSTER_BINDING_SCHEMA,
        "roster_binding_kind": bindings["roster_kind"],
        "roster_binding_sha256": bindings["roster_sha256"],
        "origin": "user-plan-file",
    }


def _write_plan_evidence(runtime: WorkspaceRuntime, compiled: dict, allowed: set[str]) -> None:
    """Store aggregate bytes once, then register exact semantic refs."""
    store = runtime._content_store()
    stored: set[str] = set()
    for source in compiled["sources"]:
        raw = source["raw"]
        digest = hashlib.sha256(raw).hexdigest()
        base = "blob-" + digest[:32]
        if digest not in stored:
            store.put(ContentRef(base, digest, len(raw)), raw, require_namespace_durable=False)
            stored.add(digest)
        current, torn = runtime._coordinator._load()
        if torn or not isinstance(current.get("workspace"), dict):
            raise WorkspaceEntryError("journal_recovery_required")
        reference = source["reference"]
        if not reference["id"].startswith(base + "."):
            raise WorkspaceEntryError("plan_source_reference_invalid")
        event = {
            "event": "workspace_evidence_registered",
            # Evidence registration is an ordinary workspace event.  Keep it
            # on the same protocol as the prepared goal so a v2 plan cannot
            # accidentally append a v1 event into a v2 journal.
            "protocol": compiled["goal"]["protocol"],
            "workspace_id": compiled["workspace_id"],
            "run_id": compiled["run_id"],
            "operation_key": "plan-evidence-" + hashlib.sha256(
                reference["id"].encode("ascii")).hexdigest()[:32],
            "expected_revision": current["workspace"]["revision"],
            "payload": {"reference": reference, "category": source["category"],
                        "task_id": source["task_id"]},
        }
        allowed.add(_digest(event))
        runtime.record_event(event)


def create_workspace_from_plan(runs_root: str, run_id: str, plan_file: str) -> dict:
    """Create one plan workspace using private staging and no-replace publish."""
    try:
        value, raw, raw_sha256 = read_plan_file(plan_file)
    except WorkspacePlanError as exc:
        raise WorkspaceEntryError(exc.kind) from None
    try:
        try:
            run_id = validate_run_id(run_id)
        except (TypeError, ValueError):
            raise WorkspaceEntryError("workspace_run_id_invalid") from None
        _reject_reparse_ancestors(runs_root)
        if os.path.lexists(runs_root):
            _verify_private(runs_root, directory=True)
        else:
            _secure_private_root(runs_root)
        final_outer = os.path.join(os.fspath(runs_root), run_id)
        if os.path.lexists(final_outer):
            raise WorkspaceEntryError("run_exists")
        compiler = compile_plan_v2 if isinstance(value, dict) and value.get("schema") == PLAN_V2_SCHEMA else compile_plan
        compiled = compiler(value, raw_sha256=raw_sha256, workspace_id=WORKSPACE_ID,
                            run_id=run_id, now_ms=int(time.time() * 1000))
        stage_root = os.path.join(
            os.fspath(runs_root), ".workspace-plan-stage-" +
            hashlib.sha256(run_id.encode("ascii")).hexdigest()[:24])
        if os.path.lexists(stage_root):
            raise WorkspaceEntryError("create_recovery_required")
        # Reserve the deterministic stage name atomically.  A concurrent
        # creator must observe an explicit recovery collision before either
        # caller can write into the same stage or publish a mixed journal.
        try:
            os.mkdir(stage_root)
        except FileExistsError:
            raise WorkspaceEntryError("create_recovery_required") from None
        _secure_private_root(stage_root)
        published = False
        publication_started = False
        try:
            allowed: set[str] = set()
            holder = {}
            def resolve(payload):
                return _content_resolver(holder["runtime"])(payload)
            runtime = WorkspaceRuntime.new(stage_root, run_id, WORKSPACE_ID,
                                           authorize=lambda command, _current: _digest(command) in allowed,
                                           resolve_evidence=resolve)
            holder["runtime"] = runtime
            bindings = compiled["bindings"]
            prepared = _runtime_goal_plan(compiled["goal"], compiled["lanes"],
                                          operation_prefix="plan-create",
                                          economics=compiled.get("economics"))
            allowed.add(_digest({"operation": "prepare", "plan": prepared}))
            for event in prepared["events"]:
                allowed.add(_digest(event))
            runtime.prepare(prepared, project_root_sha256=bindings["project_sha256"],
                            roster_definition_sha256=bindings["roster_sha256"])
            # Plan recipients are durable destinations, not workers. Keeping
            # them out of the coordinator worker registry prevents a queued
            # operator message from silently granting claim/lease/artifact or
            # terminal authority. A future adapter must explicitly register a
            # real worker before it can mutate the run.
            _write_plan_evidence(runtime, compiled, allowed)
            _write_plan_file(runtime, raw)
            presentation_key = secrets.token_bytes(32)
            retention_secret = secrets.token_bytes(32)
            key_epoch = secrets.token_urlsafe(12)
            _write_host_config(runtime, _plan_host_config_value(
                WORKSPACE_ID, run_id, presentation_key, retention_secret,
                key_epoch, raw_sha256, compiled_plan_digest(compiled),
                compiled["operator_expiry_ms"], bindings,
                plan_schema=compiled["schema"]))
            runtime.inspect()
            stage_outer = os.path.join(stage_root, run_id)
            if os.path.lexists(final_outer):
                raise WorkspaceEntryError("run_exists")
            publication_started = True
            _publish_new(stage_outer, final_outer)
            published = True
            # Reopen through the published layout and verify the source-backed
            # projection before claiming success.
            reopened = WorkspaceRuntime(runs_root, run_id, WORKSPACE_ID,
                                        authorize=lambda _command, _current: True,
                                        resolve_evidence=lambda payload: _content_resolver(reopened)(payload))
            config = _read_host_config(reopened)
            _verify_plan_host_files(reopened, config)
            state, torn = reopened._coordinator._load()
            if torn or not isinstance(state.get("workspace"), dict):
                raise WorkspaceEntryError("journal_recovery_required")
            workspace = state["workspace"]
            resolve = lambda payload: _content_resolver(reopened)(payload)
            for registered in workspace.get("evidence", {}).values():
                raw_source = resolve(registered)
                if hashlib.sha256(raw_source).hexdigest() != registered["reference"]["sha256"]:
                    raise WorkspaceEntryError("plan_source_mismatch")
                source_value, _metadata = _json_source_with_meta(resolve, registered)
                if not isinstance(source_value, dict):
                    raise WorkspaceEntryError("plan_source_unavailable")
                if (registered.get("category") == "grant"
                        and source_value.get("schema") == "summon.workspace.operator-message-grant/v1"):
                    _grant_resolver(resolve, workspace_id=WORKSPACE_ID, run_id=run_id)(
                        state, workspace, registered["reference"])
            scope = _scope_from_workspace(workspace, resolve,
                                          workspace_id=WORKSPACE_ID, run_id=run_id)
            if len(scope["targets"]) != len(compiled["operator_targets"]):
                raise WorkspaceEntryError("operator_scope_invalid")
            reopened.inspect()
            try:
                os.rmdir(stage_root)
            except OSError:
                raise WorkspaceEntryError("create_cleanup_uncertain") from None
            return {"status": "created", "mode": "create", "preview": True, "run_id": run_id,
                    "workspace_id": WORKSPACE_ID,
                    "task_count": len(compiled["lanes"]),
                    "operator_target_count": len(compiled["operator_targets"]),
                    "provider_calls": 0, "workers_started": 0, "mutation": "created"}
        except WorkspaceEntryError as exc:
            if published:
                if exc.kind == "create_cleanup_uncertain":
                    raise
                raise WorkspaceEntryError("create_published_uncertain") from None
            if publication_started:
                raise WorkspaceEntryError("create_published_uncertain") from None
            raise WorkspaceEntryError("create_recovery_required") from None
        except (WorkspaceRuntimeError, SwarmCoordinatorError, OwnershipLostError, OSError, ValueError):
            if published or publication_started:
                raise WorkspaceEntryError("create_published_uncertain") from None
            raise WorkspaceEntryError("create_recovery_required") from None
    except WorkspaceEntryError:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkspaceEntryError("create_recovery_required") from None


def _read_host_config(runtime: WorkspaceRuntime) -> dict:
    path = _host_config_path(runtime)
    descriptor = None
    try:
        expected = _regular_single_link(path, "workspace host config")
        _verify_private(path, directory=False)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        final = _final_open_path(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                or final is None or os.path.normcase(final) != os.path.normcase(os.path.realpath(path))):
            raise WorkspaceEntryError("host_config_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(4097)
        after = os.fstat(descriptor)
        current = _regular_single_link(path, "workspace host config")
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(current)
                or expected.st_size != len(raw)
                or len(raw) > 4096 or not raw.endswith(b"\n")):
            raise WorkspaceEntryError("host_config_changed")
        value = json.loads(raw[:-1].decode("utf-8"))
        v1 = {"schema", "workspace_id", "run_id", "operator_id",
              "presentation_key_b64", "retention_secret_b64", "key_epoch"}
        v2 = v1 | {"plan_schema", "plan_file", "plan_sha256",
                   "plan_compiled_sha256", "operator_expiry_ms",
                   "project_binding_schema", "project_binding_kind",
                   "project_binding_sha256", "roster_binding_schema",
                   "roster_binding_kind", "roster_binding_sha256", "origin"}
        valid = (isinstance(value, dict)
                 and ((value.get("schema") == HOST_CONFIG_SCHEMA and set(value) == v1)
                      or (value.get("schema") == PLAN_HOST_SCHEMA and set(value) == v2))
                 and _canonical(value) + b"\n" == raw
                 and value.get("schema") in {HOST_CONFIG_SCHEMA, PLAN_HOST_SCHEMA}
                 and value.get("operator_id") == OPERATOR_ID)
        if valid and value.get("schema") == PLAN_HOST_SCHEMA:
            valid = (value.get("plan_schema") in {PLAN_SCHEMA, PLAN_V2_SCHEMA}
                     and value.get("plan_file") == PLAN_FILE_NAME
                     and value.get("origin") == "user-plan-file"
                     and type(value.get("plan_sha256")) is str
                     and re.fullmatch(r"[a-f0-9]{64}", value["plan_sha256"]) is not None
                     and type(value.get("plan_compiled_sha256")) is str
                     and re.fullmatch(r"[a-f0-9]{64}", value["plan_compiled_sha256"]) is not None
                     and type(value.get("operator_expiry_ms")) is int
                     and value["operator_expiry_ms"] >= SCOPE_TTL_MS
                     and value.get("project_binding_schema") == PROJECT_BINDING_SCHEMA
                     and value.get("project_binding_kind") == "unbound"
                     and value.get("roster_binding_schema") == ROSTER_BINDING_SCHEMA
                     and value.get("roster_binding_kind") == "dormant-operator-message-destinations"
                     and all(re.fullmatch(r"[a-f0-9]{64}", value.get(key, ""))
                             is not None for key in ("project_binding_sha256",
                                                      "roster_binding_sha256")))
        if not valid:
            raise WorkspaceEntryError("host_config_invalid")
        return value
    except WorkspaceEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise WorkspaceEntryError("host_config_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _decode_host_config(value: dict, *, workspace_id: str, run_id: str):
    if value.get("workspace_id") != workspace_id or value.get("run_id") != run_id:
        raise WorkspaceEntryError("host_config_scope_mismatch")
    epoch = value.get("key_epoch")
    if (type(epoch) is not str or not 1 <= len(epoch) <= 64
            or not epoch.isascii() or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for char in epoch)):
        raise WorkspaceEntryError("host_config_invalid")
    return (_unb64(value["presentation_key_b64"], 32),
            _unb64(value["retention_secret_b64"], 32), epoch)


def _verify_plan_host_files(runtime: WorkspaceRuntime, config: dict) -> None:
    if config.get("schema") != PLAN_HOST_SCHEMA:
        return
    try:
        value, raw, digest = read_plan_file(os.path.join(runtime._coordinator.run_dir,
                                                         config["plan_file"]))
    except WorkspacePlanError:
        raise WorkspaceEntryError("plan_source_unavailable") from None
    if digest != config["plan_sha256"] or value.get("schema") != config["plan_schema"]:
        raise WorkspaceEntryError("plan_source_mismatch")
    expiry = config.get("operator_expiry_ms")
    if type(expiry) is not int or expiry < SCOPE_TTL_MS:
        raise WorkspaceEntryError("plan_source_mismatch")
    try:
        compiler = compile_plan_v2 if value.get("schema") == PLAN_V2_SCHEMA else compile_plan
        compiled = compiler(value, raw_sha256=digest,
                            workspace_id=runtime.workspace_id,
                            run_id=runtime._coordinator.run_id,
                            now_ms=expiry - SCOPE_TTL_MS)
        bindings = compiled["bindings"]
        if (compiled["operator_expiry_ms"] != expiry
                or compiled_plan_digest(compiled) != config.get("plan_compiled_sha256")
                or config.get("project_binding_schema") != PROJECT_BINDING_SCHEMA
                or config.get("project_binding_kind") != bindings["project_kind"]
                or config.get("project_binding_sha256") != bindings["project_sha256"]
                or config.get("roster_binding_schema") != ROSTER_BINDING_SCHEMA
                or config.get("roster_binding_kind") != bindings["roster_kind"]
                or config.get("roster_binding_sha256") != bindings["roster_sha256"]):
            raise WorkspaceEntryError("plan_source_mismatch")
        state, torn = runtime._coordinator._load()
        if torn or not isinstance(state.get("workspace"), dict):
            raise WorkspaceEntryError("journal_recovery_required")
        workspace = state["workspace"]
        if (state.get("project_root_sha256") != bindings["project_sha256"]
                or state.get("roster_definition_sha256") != bindings["roster_sha256"]):
            raise WorkspaceEntryError("plan_binding_mismatch")
        expected_goal = compiled["goal"]
        if workspace.get("goal") != expected_goal:
            raise WorkspaceEntryError("plan_projection_mismatch")
        expected_lanes = {lane["task_id"]: lane for lane in compiled["lanes"]}
        actual_lanes = workspace.get("lanes")
        if not isinstance(actual_lanes, dict) or set(actual_lanes) != set(expected_lanes):
            raise WorkspaceEntryError("plan_projection_mismatch")
        lane_fields = {"protocol", "workspace_id", "run_id", "kind", "goal_id",
                       "goal_revision", "task_id", "lane_id", "request_sha256",
                       "role", "outcome", "scope", "authority_ref", "budget",
                       "criterion_ids", "return_condition", "escalation_trigger",
                       "depends_on"}
        for task_id, expected in expected_lanes.items():
            actual = actual_lanes[task_id]
            if {key: actual.get(key) for key in lane_fields} != {key: expected.get(key) for key in lane_fields}:
                raise WorkspaceEntryError("plan_projection_mismatch")
        expected_sources = {(item["reference"]["id"], item["reference"]["sha256"],
                            item["category"], item["task_id"]) for item in compiled["sources"]}
        evidence = workspace.get("evidence")
        if not isinstance(evidence, dict):
            raise WorkspaceEntryError("plan_projection_mismatch")
        actual_sources = set()
        for item in evidence.values():
            reference = item.get("reference") if isinstance(item, dict) else None
            if not isinstance(reference, dict):
                raise WorkspaceEntryError("plan_projection_mismatch")
            actual_sources.add((reference.get("id"), reference.get("sha256"),
                                item.get("category"), item.get("task_id")))
        # Later operator sends and reconciliation may append additional,
        # non-plan evidence.  The immutable plan binding requires every
        # compiled source to remain present with the same identity/digest; it
        # must not reject legitimate post-create lifecycle evidence.
        if not expected_sources <= actual_sources:
            raise WorkspaceEntryError("plan_projection_mismatch")
    except WorkspaceEntryError:
        raise
    except (WorkspaceRuntimeError, SwarmCoordinatorError, OSError, TypeError, ValueError):
        raise WorkspaceEntryError("plan_source_mismatch") from None


def _content_resolver(runtime: WorkspaceRuntime):
    """Build a source resolver bound to this runtime's private content root."""
    def resolve(payload):
        try:
            reference = payload["reference"]
            if (isinstance(reference, dict) and isinstance(reference.get("id"), str)
                    and reference["id"].startswith("admission-")):
                return runtime.read_send_admission_evidence(payload)
            content_store = runtime._content_store()
            # Evidence references may carry a bounded semantic suffix (for
            # example ``blob-<id>.operator-scope``); the immutable blob store
            # address is the base id only.
            blob_id = reference["id"].split(".", 1)[0]
            path = content_store._path(blob_id)
            size = os.stat(path, follow_symlinks=False).st_size
            return content_store.read(ContentRef(blob_id, reference["sha256"], size))
        except (KeyError, OSError, TypeError, ValueError):
            raise WorkspaceEntryError("source_unreadable") from None
    return resolve


def _json_source(resolve, registered):
    value, _meta = _json_source_with_meta(resolve, registered)
    return value


def _json_source_with_meta(resolve, registered):
    try:
        raw = resolve(registered)
        value = json.loads(raw.decode("utf-8"))
        if (isinstance(value, dict) and isinstance(value.get("entries"), list)
                and isinstance(value.get("schema"), str)
                and value["schema"].startswith("summon.workspace.plan-")):
            expected_fields = {"schema", "aggregate_id", "workspace_id", "run_id",
                               "entry_count", "chunk_index", "chunk_count", "entries"}
            if set(value) != expected_fields:
                raise ValueError("aggregate fields invalid")
            reference = registered.get("reference", {})
            identifier = reference.get("id") if isinstance(reference, dict) else None
            suffix = identifier.rsplit(".", 1)[-1] if isinstance(identifier, str) and "." in identifier else None
            if suffix is None:
                raise ValueError("aggregate reference missing entry suffix")
            if (type(value["aggregate_id"]) is not str or not value["aggregate_id"]
                    or type(value["workspace_id"]) is not str or type(value["run_id"]) is not str
                    or type(value["chunk_index"]) is not int or type(value["chunk_count"]) is not int
                    or not 0 <= value["chunk_index"] < value["chunk_count"]
                    or type(value["entry_count"]) is not int or not 1 <= value["entry_count"] <= 16
                    or not 1 <= value["chunk_count"] <= 16
                    or len(value["entries"]) == 0 or len(value["entries"]) > 16):
                raise ValueError("aggregate metadata invalid")
            if (not identifier.startswith("blob-" + hashlib.sha256(raw).hexdigest()[:32] + ".")
                    or not registered["reference"]["sha256"] == hashlib.sha256(raw).hexdigest()):
                raise ValueError("aggregate reference digest mismatch")
            entry_ids = [entry.get("entry_id") if isinstance(entry, dict) else None
                         for entry in value["entries"]]
            if (any(type(entry_id) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}\Z", entry_id)
                    for entry_id in entry_ids)
                    or len(set(entry_ids)) != len(entry_ids)):
                raise ValueError("aggregate entry ids invalid")
            matches = [entry for entry in value["entries"]
                       if isinstance(entry, dict) and entry.get("entry_id") == suffix]
            if len(matches) != 1 or not isinstance(matches[0].get("value"), dict):
                raise ValueError("aggregate entry not found")
            return matches[0]["value"], {
                "aggregate_id": value.get("aggregate_id"),
                "entry_count": value["entry_count"],
                "chunk_count": value["chunk_count"],
                "chunk_index": value.get("chunk_index"),
                "schema": value["schema"],
            }
        if not isinstance(value, dict):
            raise ValueError("source is not an object")
        return value, None
    except WorkspaceEntryError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OSError):
        raise WorkspaceEntryError("plan_source_unavailable") from None


def _scope_from_workspace(workspace: dict, resolve, *, workspace_id: str, run_id: str) -> dict:
    """Find the complete exact durable operator scope; never synthesize one."""
    expired_or_revoked = False
    targets = []
    aggregate_groups = {}
    for registered in workspace.get("evidence", {}).values():
        if registered.get("category") != "grant":
            continue
        source, metadata = _json_source_with_meta(resolve, registered)
        if not (isinstance(source, dict)
                and source.get("schema") == "summon.workspace.operator-scope/v1"
                and source.get("workspace_id") == workspace_id
                and source.get("run_id") == run_id
                and source.get("operator_id") == OPERATOR_ID
                and isinstance(source.get("scope"), dict)):
            # An aggregate scope entry that cannot be decoded is a broken
            # workspace, not permission to accept a partial target set.
            if metadata is not None and metadata.get("schema") == "summon.workspace.plan-scopes/v1":
                raise WorkspaceEntryError("operator_scope_invalid")
            continue
        scope = source["scope"]
        task_id = scope.get("destination_task_id")
        grant_ref = scope.get("delivery_grant_ref")
        target = scope.get("target")
        if (not isinstance(task_id, str) or not isinstance(grant_ref, dict)
                or set(grant_ref) != {"id", "sha256"}
                or not isinstance(target, str)
                or type(scope.get("expires_at_ms")) is not int
                or scope.get("expires_at_ms") <= int(time.time() * 1000)
                or scope.get("revoked") is not False):
            expired_or_revoked = True
            continue
        if metadata is None:
            targets.append({"target": target, "task_id": task_id,
                            "recipient_instance_id": scope["recipient"]["instance_id"],
                            "send_scope_ref": registered["reference"]})
            continue
        key = (metadata.get("aggregate_id"), metadata.get("schema"))
        group = aggregate_groups.setdefault(key, {"expected": metadata["entry_count"],
                                                   "chunks": metadata["chunk_count"],
                                                   "chunk_indexes": set(), "targets": []})
        group["chunk_indexes"].add(metadata.get("chunk_index"))
        group["targets"].append({"target": target, "task_id": task_id,
                                  "recipient_instance_id": scope["recipient"]["instance_id"],
                                  "send_scope_ref": registered["reference"]})
    if aggregate_groups and targets:
        raise WorkspaceEntryError("operator_scope_invalid")
    if aggregate_groups:
        valid_groups = []
        for group in aggregate_groups.values():
            if (len(group["targets"]) != group["expected"]
                    or len(group["chunk_indexes"]) != group["chunks"]
                    or len({item["target"] for item in group["targets"]}) != group["expected"]
                    or len({item["task_id"] for item in group["targets"]}) != group["expected"]):
                raise WorkspaceEntryError("operator_scope_invalid")
            valid_groups.append(group["targets"])
        if len(valid_groups) != 1:
            raise WorkspaceEntryError("operator_scope_invalid")
        targets.extend(valid_groups[0])
    if targets and not expired_or_revoked:
        return {"workspace_id": workspace_id, "run_id": run_id,
                "operator_id": OPERATOR_ID, "targets": targets}
    raise WorkspaceEntryError("operator_scope_unavailable" if expired_or_revoked
                              else "operator_scope_missing")


def _grant_resolver(resolve, *, workspace_id=WORKSPACE_ID, run_id=None):
    """Resolve the durable grant shape without assuming a demo class."""
    def resolve_grant(_coordinator, workspace, reference):
        registered = workspace.get("evidence", {}).get(reference["id"])
        if not isinstance(registered, dict) or registered.get("category") != "grant":
            raise WorkspaceRuntimeError("execution_grant_missing")
        if registered.get("reference") != reference:
            raise WorkspaceRuntimeError("execution_grant_reference_mismatch")
        source = _json_source(resolve, registered)
        if not isinstance(source, dict):
            raise WorkspaceRuntimeError("execution_grant_source_unverified")
        # The synthetic demo/reopen contract predates user plans and uses a
        # deliberately distinct fixed grant shape.  Preserve that fixture
        # compatibility without allowing it to satisfy the plan grant schema.
        if source.get("kind") == "fixed-demo-grant":
            required_demo = {"kind", "workspace_id", "run_id", "task_id",
                             "goal_revision", "recipient", "revoked", "scope",
                             "max_attempts"}
            if (set(source) != required_demo
                    or source["workspace_id"] != workspace_id
                    or (run_id is not None and source["run_id"] != run_id)
                    or source["scope"] != "fixed-local-computation"
                    or source["revoked"] is not False
                    or source["max_attempts"] != 1):
                raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
            return {"grant_ref": reference, "task_id": source["task_id"],
                    "goal_revision": source["goal_revision"],
                    "recipient": source["recipient"], "revoked": source["revoked"]}
        required = {"schema", "workspace_id", "run_id", "task_id", "goal_revision",
                    "recipient", "revoked", "operation", "execution_authorized"}
        if set(source) != required or source.get("schema") != "summon.workspace.operator-message-grant/v1":
            raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
        if (source["workspace_id"] != workspace_id
                or (run_id is not None and source["run_id"] != run_id)):
            raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
        if source["operation"] != "operator.message.receive" or source["execution_authorized"] is not False:
            raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
        if source["revoked"] is not False:
            raise WorkspaceRuntimeError("execution_grant_scope_mismatch")
        return {"grant_ref": reference, "task_id": source["task_id"],
                "goal_revision": source["goal_revision"], "recipient": source["recipient"],
                "revoked": source["revoked"]}
    return resolve_grant


def _demo_scope(demo: ConductorDemo) -> dict:
    """Create one explicit fixture-only operator scope for demo qualification."""
    grant, _ = demo.grant("main-1", OPERATOR_INSTANCE, suffix="operator-message-grant")
    scope = {
        "revision": 1,
        "expires_at_ms": int(demo.runtime._coordinator.clock() * 1000) + 300000,
        "revoked": False,
        "operation": "operator.message.send",
        "goal_id": "verify-conductor-loop",
        "destination_task_id": "main-1",
        "recipient": {"instance_id": OPERATOR_INSTANCE, "epoch": 1},
        "delivery_grant_ref": grant,
        "target": OPERATOR_TARGET,
    }
    source = demo.source({
        "schema": "summon.workspace.operator-scope/v1",
        "workspace_id": demo.workspace_id,
        "run_id": demo.run_id,
        "operator_id": OPERATOR_ID,
        "scope": scope,
    })
    scope_ref = demo.evidence(source, "main-1", "operator-message-scope", "grant")
    return {
        "workspace_id": demo.workspace_id,
        "run_id": demo.run_id,
        "operator_id": OPERATOR_ID,
        "targets": [{
            "target": OPERATOR_TARGET,
            "task_id": "main-1",
            "recipient_instance_id": OPERATOR_INSTANCE,
            "send_scope_ref": scope_ref,
        }],
    }


@dataclass
class WorkspaceHost:
    """One foreground-owned local workspace host."""

    runs_root: str
    run_id: str
    port: int
    mode: str = "open"
    objective: str = DEFAULT_OBJECTIVE
    workspace_id: str = WORKSPACE_ID
    command_policy_file: str | None = None
    command_refresh_token_file: str | None = None

    def __post_init__(self):
        self.runs_root = os.fspath(self.runs_root)
        self.port = _port(self.port)
        if self.mode not in {"open", "demo-create"}:
            raise WorkspaceEntryError("invalid_mode")
        self.objective = _objective(self.objective)
        if self.command_policy_file is not None and (
                type(self.command_policy_file) is not str or not self.command_policy_file):
            raise WorkspaceEntryError("command_policy_required")
        if self.command_refresh_token_file is not None and (
                type(self.command_refresh_token_file) is not str
                or not self.command_refresh_token_file):
            raise WorkspaceEntryError("command_refresh_token_required")
        if self.command_refresh_token_file is not None and self.command_policy_file is None:
            raise WorkspaceEntryError("command_refresh_unavailable")
        self.demo: ConductorDemo | None = None
        self.runtime: WorkspaceRuntime | None = None
        self.surface: WorkspaceSurface | None = None
        self._authority = {"allowed": True}
        self._allowed_commands: set[str] = set()
        self._presentation_key: bytes | None = None
        self._retention_secret: bytes | None = None
        self._key_epoch: str | None = None
        self._message_handle = None
        self._draft_handle = None
        self._command_handle = None
        self._linked_handle = None
        self._linked_scope: dict | None = None
        self._workspace_details: WorkspaceDetailAdapter | None = None
        self._command_policy: dict | None = None
        self._command_policy_generation = 0
        self._command_policy_lock = threading.RLock()
        self._command_refresh_token: str | None = None
        self._command_refresh_history: dict[str, dict] = {}
        self._command_refresh_history_order: list[str] = []
        self._command_provision_bindings: dict[str, tuple[str, tuple[str, ...], str, str]] = {}
        self._started = False

    def _authorize(self, command, current, *, policy_generation=None):
        with self._command_policy_lock:
            policy = copy.deepcopy(self._command_policy)
            authority_allowed = self._authority["allowed"]
        if command is None and policy is not None:
            return (authority_allowed
                    and int(time.time() * 1000) < policy["expires_at_ms"]
                    and (policy_generation is None
                         or policy_generation == policy["generation"]))
        operation = command.get("operation") if isinstance(command, dict) else None
        if operation in {"operator_message.send", "operator_message.lookup"}:
            return authority_allowed
        if isinstance(command, dict) and command.get("event") == "workspace_operator_disposition_recorded":
            if policy is None or not authority_allowed:
                return False
            if int(time.time() * 1000) >= policy["expires_at_ms"]:
                return False
            payload = command.get("payload")
            if (not isinstance(payload, dict)
                    or payload.get("action") != "retain_held_context"
                    or not isinstance(payload.get("delivery_id"), str)):
                return False
            return any(item["delivery_id"] == payload["delivery_id"]
                       and "retain_held_context" in item["actions"]
                       for item in policy["targets"])
        if isinstance(command, dict) and command.get("event") == "workspace_evidence_registered":
            payload = command.get("payload")
            reference = payload.get("reference") if isinstance(payload, dict) else None
            reference_id = reference.get("id") if isinstance(reference, dict) else None
            if (isinstance(reference_id, str) and reference_id.startswith("blob-")
                    and ".retain-" in reference_id):
                if policy is None or not authority_allowed:
                    return False
                if int(time.time() * 1000) >= policy["expires_at_ms"]:
                    return False
                if (payload.get("category") != "event"
                        or not isinstance(payload.get("delivery_id"), str)
                        or not isinstance(payload.get("task_id"), str)):
                    return False
                return any(item["delivery_id"] == payload["delivery_id"]
                           and "retain_held_context" in item["actions"]
                           for item in policy["targets"])
        if isinstance(command, dict) and command.get("schema") == "summon.workspace.operator-command-request/v1":
            if policy is None or not authority_allowed:
                return False
            if int(time.time() * 1000) >= policy["expires_at_ms"]:
                return False
            if (policy_generation is not None
                    and policy_generation != policy["generation"]):
                return False
            delivery = command.get("delivery")
            if not isinstance(delivery, dict):
                return False
            target = delivery.get("delivery_id")
            return any(item["delivery_id"] == target and command.get("action") in item["actions"]
                       for item in policy["targets"])
        if isinstance(command, dict) and command.get("schema") == "summon.workspace.operator-disposition-request/v1":
            if policy is None or not authority_allowed:
                return False
            if int(time.time() * 1000) >= policy["expires_at_ms"]:
                return False
            if (policy_generation is not None
                    and policy_generation != policy["generation"]):
                return False
            target = command.get("delivery_id")
            return (command.get("action") == "retain_held_context"
                    and any(item["delivery_id"] == target
                            and "retain_held_context" in item["actions"]
                            for item in policy["targets"]))
        if isinstance(command, dict) and command.get("operation") == "execute_operator_linked_replacement":
            scope = copy.deepcopy(self._linked_scope)
            proposal = command.get("proposal")
            if not authority_allowed or not isinstance(scope, dict) or not isinstance(proposal, dict):
                return False
            if (proposal.get("schema") != "summon.workspace.linked-replacement-proposal/v1"
                    or proposal.get("action") != "propose_linked_replacement"):
                return False
            return any(item.get("parent_target") == proposal.get("parent_target")
                       and proposal.get("recipient_target") in item.get("recipient_targets", [])
                       for item in scope.get("targets", []) if isinstance(item, dict))
        if self.demo is not None:
            return self.demo._base_authorize(command, current)
        return _digest(command) in self._allowed_commands

    def _record_install_command(self, command):
        if self.demo is not None:
            self.demo.permit(command)
        else:
            self._allowed_commands.add(_digest(command))

    def _make_open_runtime(self):
        holder = {}

        def resolve(payload):
            return _content_resolver(holder["runtime"])(payload)

        runtime = WorkspaceRuntime(
            self.runs_root, self.run_id, self.workspace_id,
            authorize=self._authorize, resolve_evidence=resolve,
        )
        holder["runtime"] = runtime
        return runtime, resolve

    def _install_authority(self, scope, *, grant_resolver, command_policy=None):
        runtime = self.runtime
        assert runtime is not None
        install = {"operation": "install_operator_messages", "scope": scope}
        self._record_install_command(install)
        message_handle = runtime.install_operator_messages(
            scope,
            authorize_message=lambda _command, _workspace: self._authority["allowed"],
            resolve_grant=grant_resolver,
        )
        retention_scope = {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "operator_id": OPERATOR_ID,
            "targets": [{"target": item["target"], "task_id": item["task_id"]}
                        for item in scope["targets"]],
        }
        retention = {"operation": "install_operator_draft_retention", "scope": retention_scope}
        self._record_install_command(retention)
        draft_handle = runtime.install_operator_draft_retention(
            retention_scope,
            authorize_retention=lambda _command, _workspace: self._authority["allowed"],
            retention_secret=self._retention_secret,
            key_epoch=self._key_epoch,
        )
        command_handle = None
        if command_policy is not None:
            command_scope = _command_scope(command_policy, workspace_id=self.workspace_id,
                                            run_id=self.run_id)
            install_command = {"operation": "install_operator_commands", "scope": command_scope}
            self._record_install_command(install_command)
            generation = command_policy["generation"]
            command_handle = runtime.install_operator_commands(
                command_scope,
                authorize_command=lambda command, workspace, generation=generation:
                    self._authorize(command, workspace, policy_generation=generation),
            )
        self._message_handle, self._draft_handle, self._command_handle = (
            message_handle, draft_handle, command_handle)
        with self._command_policy_lock:
            self._command_policy = copy.deepcopy(command_policy)
            self._command_policy_generation = command_policy["generation"] if command_policy else 0
        return message_handle, draft_handle, command_handle

    def install_operator_linked_replacements(self, scope, *, authorize_link,
                                             resolve_link):
        """Install a bounded linked-replacement adapter on the live host.

        This is a trusted host seam, not a CLI command: the caller supplies
        the already-authorized source resolver and the finite opaque target
        scope.  The browser receives only the resulting presentation handles.
        The authority is memory-only and must be reinstalled after restart.
        """
        if (not self._started or self.runtime is None or self.surface is None
                or not callable(authorize_link) or not callable(resolve_link)):
            raise WorkspaceEntryError("linked_scope_required")
        install = {"operation": "install_operator_linked_replacements",
                   "scope": copy.deepcopy(scope)}
        self._record_install_command(install)
        try:
            handle = self.runtime.install_operator_linked_replacements(
                scope, authorize_link=authorize_link, resolve_link=resolve_link)
            adapter = OperatorLinkedReplacementAdapter(self.runtime, handle)
        except (WorkspaceRuntimeError, ValueError, TypeError) as error:
            raise WorkspaceEntryError(_public_error(error)) from None
        prior = self._linked_handle
        if prior is not None:
            try:
                self.runtime.revoke_operator_linked_replacements(prior)
            except WorkspaceRuntimeError:
                pass
        self._linked_handle = handle
        self._linked_scope = copy.deepcopy(scope)
        self.surface.operator_linked_replacements = adapter
        return {"status": "installed", "target_count": len(scope.get("targets", [])),
                "provider_calls": 0}

    def revoke_operator_linked_replacements(self):
        if self.runtime is None:
            raise WorkspaceEntryError("linked_scope_required")
        handle = self._linked_handle
        if handle is None:
            raise WorkspaceEntryError("linked_scope_required")
        try:
            self.runtime.revoke_operator_linked_replacements(handle)
        except WorkspaceRuntimeError:
            raise WorkspaceEntryError("linked_scope_required") from None
        self._linked_handle = None
        self._linked_scope = None
        if self.surface is not None:
            self.surface.operator_linked_replacements = None

    def install_workspace_details(self, scope, *, resolve=None, readers=None,
                                  body_scope=None, resolve_body=None, body_readers=None):
        """Install finite typed review/decision metadata and optional body scope.

        This trusted host seam never grants a browser capability.  The adapter
        receives only the host's pre-bound records and is revoked with the
        host; body access requires a separately supplied body scope/resolver.
        """
        if (not self._started or self.runtime is None or self.surface is None
                or (resolve is None and readers is None)):
            raise WorkspaceEntryError("detail_scope_required")
        if readers is not None:
            if (type(readers) is not dict or not readers
                    or any(type(kind) is not str or kind not in SOURCE_KINDS
                           or not callable(reader)
                           for kind, reader in readers.items())):
                raise WorkspaceEntryError("detail_reader_invalid")
            typed_readers = dict(readers)
            def resolve(binding):
                reader = typed_readers.get(binding["source_kind"])
                if reader is None:
                    raise WorkspaceDetailError("detail_type_refused")
                return reader(binding)
        if not callable(resolve):
            raise WorkspaceEntryError("detail_reader_invalid")
        if body_readers is not None:
            if (type(body_readers) is not dict or not body_readers
                    or any(type(kind) is not str or kind not in SOURCE_KINDS
                           or not callable(reader)
                           for kind, reader in body_readers.items())):
                raise WorkspaceEntryError("detail_reader_invalid")
            typed_body_readers = dict(body_readers)
            def resolve_body(binding):
                reader = typed_body_readers.get(binding["source_kind"])
                if reader is None:
                    raise WorkspaceDetailError("detail_type_refused")
                return reader(binding)
        if resolve_body is not None and not callable(resolve_body):
            raise WorkspaceEntryError("detail_reader_invalid")
        try:
            adapter = WorkspaceDetailAdapter(
                self.runtime._coordinator, self.workspace_id, scope,
                resolve=resolve, presentation_key=self._presentation_key,
                body_scope=body_scope, resolve_body=resolve_body)
        except WorkspaceDetailError as error:
            raise WorkspaceEntryError(str(error)) from None
        prior = self._workspace_details
        if prior is not None:
            prior.revoke()
        self._workspace_details = adapter
        self.surface.workspace_details = adapter
        return {"status": "installed", "target_count": len(scope.get("targets", [])),
                "body_scope": body_scope is not None, "provider_calls": 0}

    def install_canonical_workspace_details(self, *, council_sources=None,
                                            deliberation_sources=None,
                                            body_source_keys=None):
        """Bind the UI to the host's canonical workspace/council journals.

        The caller supplies only finite host-owned descriptors for external
        council/deliberation roots.  The resulting browser scope contains
        opaque source handles; no source path or receipt is placed in it.
        Reinstallation is required after restart, just like other trusted
        workspace capabilities.
        """
        if not self._started or self.runtime is None or self.surface is None:
            raise WorkspaceEntryError("detail_scope_required")
        try:
            state, torn = self.runtime._coordinator._load()
            workspace = state.get("workspace") if isinstance(state, dict) else None
            if torn or not isinstance(workspace, dict):
                raise WorkspaceEntryError("detail_snapshot_unavailable")
            scope, body_scope, resolve, resolve_body = _workspace_canonical.build(
                self.runtime, self.workspace_id, self.run_id,
                council_sources=council_sources,
                deliberation_sources=deliberation_sources,
                body_source_keys=body_source_keys)
            installed = self.install_workspace_details(
                scope, resolve=resolve, body_scope=body_scope,
                resolve_body=resolve_body)
            installed["canonical"] = True
            installed["workspace_sources"] = sum(
                item.get("source_kind") in {"workspace_assessment", "workspace_decision",
                                               "workspace_evidence"}
                for item in scope["targets"])
            installed["external_sources"] = sum(
                item.get("source_kind") in {"council_review", "deliberation_decision",
                                               "deliberation_pending"}
                for item in scope["targets"])
            installed["provider_calls"] = 0
            return installed
        except _workspace_canonical.CanonicalDetailError as error:
            raise WorkspaceEntryError(str(error)) from None

    def revoke_workspace_details(self):
        if self._workspace_details is None:
            raise WorkspaceEntryError("detail_scope_required")
        self._workspace_details.revoke()
        self._workspace_details = None
        if self.surface is not None:
            self.surface.workspace_details = None

    def refresh_command_policy(self, path: str, *, request_id: str | None = None) -> dict:
        """Explicitly replace the finite command scope with a newer generation."""
        with self._command_policy_lock:
            # Policy validation, handle installation, adapter construction and
            # retirement form one serialized transaction.  In particular, do
            # not validate generation N, release the lock, and let a newer
            # generation commit before N installs its handle; that would let
            # an older refresh overwrite the current authority.
            if not self._started or self.runtime is None or self.surface is None:
                raise WorkspaceEntryError("command_policy_host_unavailable")
            if request_id is None:
                request_id = secrets.token_hex(16)
            if (type(request_id) is not str or not 16 <= len(request_id) <= 128
                    or not request_id.isascii()
                    or any(char not in "0123456789abcdef" for char in request_id)):
                raise WorkspaceEntryError("command_refresh_request_invalid")
            if request_id in self._command_provision_bindings:
                raise WorkspaceEntryError("command_refresh_request_conflict")
            prior = self._command_refresh_history.get(request_id)
            if prior is not None:
                return copy.deepcopy(prior)
            if len(self._command_refresh_history) >= COMMAND_REFRESH_HISTORY_MAX:
                # Never evict a request key: an evicted old key could later be
                # reused as a fresh mutation after the policy file changed.
                raise WorkspaceEntryError("command_refresh_history_full")
            state, torn = self.runtime._coordinator._load()
            if torn or not isinstance(state.get("workspace"), dict):
                raise WorkspaceEntryError("workspace_unprepared")
            policy = _read_command_policy(path, workspace=state["workspace"],
                                           workspace_id=self.workspace_id, run_id=self.run_id)
            if (self._command_policy is not None
                    and policy["generation"] <= self._command_policy_generation):
                raise WorkspaceEntryError("command_policy_generation_stale")
            scope = _command_scope(policy, workspace_id=self.workspace_id, run_id=self.run_id)
            self._record_install_command({"operation": "install_operator_commands", "scope": scope})
            generation = policy["generation"]
            previous = self._command_handle
            previous_policy = copy.deepcopy(self._command_policy)
            previous_generation = self._command_policy_generation
            previous_adapter = self.surface.operator_commands
            handle = None
            # Install the new policy before adapter construction: the runtime
            # immediately invokes its authorization callback while the adapter
            # binds the newly installed handle. Nothing is exposed until the
            # adapter has been fully constructed.
            try:
                handle = self.runtime.install_operator_commands(
                    scope,
                    authorize_command=lambda command, workspace, generation=generation:
                        self._authorize(command, workspace, policy_generation=generation),
                )
                self._command_policy = copy.deepcopy(policy)
                self._command_policy_generation = generation
                adapter = OperatorCommandAdapter(self.runtime, handle)
                disposition_adapter = OperatorDispositionAdapter(self.runtime, handle)
                if previous is not None:
                    self.runtime.revoke_operator_commands(previous)
                self.surface.operator_commands = adapter
                self.surface.operator_dispositions = disposition_adapter
                self._command_handle = handle
            except Exception:
                if handle is not None:
                    try:
                        self.runtime.revoke_operator_commands(handle)
                    except WorkspaceRuntimeError:
                        pass
                self._command_policy = previous_policy
                self._command_policy_generation = previous_generation
                self.surface.operator_commands = previous_adapter
                self.surface.operator_dispositions = (OperatorDispositionAdapter(self.runtime, previous)
                                                       if previous is not None else None)
                self._command_handle = previous
                raise WorkspaceEntryError("command_policy_refresh_failed") from None
            result = {"schema": "summon.workspace.command-refresh/v1",
                      "status": "refreshed", "workspace_id": self.workspace_id,
                      "run_id": self.run_id, "generation": generation,
                      "target_count": len(policy["targets"]), "provider_calls": 0,
                      "request_id": request_id}
            self._command_refresh_history[request_id] = copy.deepcopy(result)
            self._command_refresh_history_order.append(request_id)
            return result

    def provision_command_policy_for_operator_message(self, operation_key: str, *,
                                                       actions=("cancel_queued_context",),
                                                       request_id: str | None = None) -> dict:
        """Owner-only bridge from a sent message operation to command scope.

        The browser sees only an opaque delivery handle.  This method resolves
        the operation against the current, coherent journal prefix and writes a
        bounded private policy before using the normal refresh transaction.  It
        never accepts a public delivery ID, sender prose or a browser bearer as
        authority, and the result contains only the ordinary refresh projection.
        """
        if (not self._started or self.runtime is None or self.surface is None
                or self.command_policy_file is None):
            raise WorkspaceEntryError("command_refresh_unavailable")
        if (type(operation_key) is not str
                or re.fullmatch(r"[a-f0-9]{32}", operation_key) is None):
            raise WorkspaceEntryError("command_refresh_request_invalid")
        if (type(actions) not in (tuple, list) or not 1 <= len(actions) <= len(ACTIONS)
                or any(type(action) is not str or action not in ACTIONS for action in actions)
                or len(set(actions)) != len(actions)):
            raise WorkspaceEntryError("command_policy_invalid")
        if request_id is None:
            request_id = operation_key
        if (type(request_id) is not str or not 16 <= len(request_id) <= 128
                or not request_id.isascii()
                or any(char not in "0123456789abcdef" for char in request_id)):
            raise WorkspaceEntryError("command_refresh_request_invalid")
        binding = (operation_key, tuple(actions), self.workspace_id, self.run_id)
        with self._command_policy_lock:
            prior_binding = self._command_provision_bindings.get(request_id)
            if prior_binding is not None:
                if prior_binding != binding:
                    raise WorkspaceEntryError("command_refresh_request_conflict")
                prior = self._command_refresh_history.get(request_id)
                if prior is not None:
                    return copy.deepcopy(prior)
                raise WorkspaceEntryError("command_refresh_request_conflict")
            if request_id in self._command_refresh_history:
                raise WorkspaceEntryError("command_refresh_request_conflict")
            if len(self._command_refresh_history) >= COMMAND_REFRESH_HISTORY_MAX:
                raise WorkspaceEntryError("command_refresh_history_full")
            prior = self._command_refresh_history.get(request_id)
            if prior is not None:
                return copy.deepcopy(prior)
            if self._command_policy is None:
                raise WorkspaceEntryError("command_refresh_unavailable")
            try:
                state, torn, before = self.runtime._coordinator._load_with_snapshot()
                records, later_torn, after = self.runtime._coordinator._read_records_snapshot()
            except (OSError, ValueError, TypeError, KeyError):
                raise WorkspaceEntryError("workspace_snapshot_unavailable") from None
            if (torn or later_torn or before != after
                    or not isinstance(state.get("workspace"), dict)):
                raise WorkspaceEntryError("workspace_snapshot_unavailable")
            import _workspace_admission as admission
            matches = [record.get("workspace_event") for record in records
                       if isinstance(record, dict)
                       and isinstance(record.get("workspace_event"), dict)
                       and record["workspace_event"].get("event") == "workspace_operator_message_sent"
                       and isinstance(record["workspace_event"].get("payload"), dict)
                       and isinstance(record["workspace_event"]["payload"].get("request"), dict)
                       and record["workspace_event"]["payload"]["request"].get("operation_key") == operation_key]
            if len(matches) != 1 or matches[0].get("event") != "workspace_operator_message_sent":
                raise WorkspaceEntryError("command_target_refused")
            try:
                event = admission.canonical_operator_send_event(matches[0])
                response = admission.operator_send_response(event)
            except (ValueError, TypeError, KeyError):
                raise WorkspaceEntryError("command_target_refused") from None
            delivery_id = response.get("delivery_id")
            workspace = state["workspace"]
            event_operation_key = event["operation_key"]
            if (workspace.get("send_operations", {}).get(event_operation_key) != {
                    "request_sha256": event["payload"]["request"]["request_sha256"],
                    "response": response}):
                raise WorkspaceEntryError("command_target_refused")
            delivery = workspace.get("deliveries", {}).get(delivery_id)
            message = (workspace.get("messages", {}).get(delivery.get("message_id"))
                       if isinstance(delivery, dict) else None)
            if (not isinstance(delivery, dict) or "parent_delivery_id" in delivery
                    or not isinstance(message, dict)
                    or message.get("kind") != "operator_message"
                    or message.get("destination_task_id") not in workspace.get("lanes", {})):
                raise WorkspaceEntryError("command_target_refused")
            policy = copy.deepcopy(self._command_policy)
            generation = policy["generation"] + 1
            if generation > _workspace_protocol.MAX_INT:
                raise WorkspaceEntryError("command_policy_generation_stale")
            targets = {item["delivery_id"]: item for item in policy["targets"]}
            targets[delivery_id] = {"delivery_id": delivery_id, "actions": list(actions)}
            policy["generation"] = generation
            policy["targets"] = list(targets.values())
            raw = _canonical(policy) + b"\n"
            previous = _replace_private_bytes(self.command_policy_file, raw)
            try:
                result = self.refresh_command_policy(self.command_policy_file, request_id=request_id)
                self._command_provision_bindings[request_id] = binding
                return result
            except Exception:
                try:
                    _replace_private_bytes(self.command_policy_file, previous)
                except Exception:
                    pass
                raise

    def refresh_command_policy_with_token(self, token: str, *, request_id: str | None = None) -> dict:
        """Refresh from the explicitly configured policy using separate control auth."""
        if (self._command_refresh_token is None or type(token) is not str
                or not hmac.compare_digest(token, self._command_refresh_token)):
            raise WorkspaceEntryError("command_refresh_authentication_required")
        if self.command_policy_file is None:
            raise WorkspaceEntryError("command_refresh_unavailable")
        return self.refresh_command_policy(self.command_policy_file, request_id=request_id)

    def provision_command_policy_for_operator_message_with_token(
            self, token: str, operation_key: str, actions, request_id: str) -> dict:
        """Provision a sent operator message through the owner-only control token."""
        if (self._command_refresh_token is None or type(token) is not str
                or not hmac.compare_digest(token, self._command_refresh_token)):
            raise WorkspaceEntryError("command_refresh_authentication_required")
        if self.command_policy_file is None:
            raise WorkspaceEntryError("command_refresh_unavailable")
        return self.provision_command_policy_for_operator_message(
            operation_key, actions=actions, request_id=request_id)

    def refresh_command_policy_status(self, token: str, request_id: str) -> dict:
        """Return only bounded control-plane state for a lost refresh response."""
        if (self._command_refresh_token is None or type(token) is not str
                or not hmac.compare_digest(token, self._command_refresh_token)):
            raise WorkspaceEntryError("command_refresh_authentication_required")
        if self.command_policy_file is None:
            raise WorkspaceEntryError("command_refresh_unavailable")
        if (type(request_id) is not str or not 16 <= len(request_id) <= 128
                or not request_id.isascii()
                or any(char not in "0123456789abcdef" for char in request_id)):
            raise WorkspaceEntryError("command_refresh_request_invalid")
        with self._command_policy_lock:
            policy = copy.deepcopy(self._command_policy)
            prior = copy.deepcopy(self._command_refresh_history.get(request_id))
        if policy is None:
            raise WorkspaceEntryError("command_refresh_unavailable")
        if prior is not None:
            return prior
        return {"schema": "summon.workspace.command-refresh-status/v1",
                "status": "history_not_retained", "workspace_id": self.workspace_id,
                "run_id": self.run_id, "generation": policy["generation"],
                "target_count": len(policy["targets"]), "provider_calls": 0,
                "request_id": request_id}

    def start(self) -> dict:
        if self._started:
            raise WorkspaceEntryError("host_already_started")
        try:
            # Fail before provisioning/adopting a run when the requested port
            # is already occupied. The final bind below remains authoritative
            # because this preflight is necessarily racy.
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", self.port))
            except OSError:
                raise WorkspaceEntryError("loopback_port_unavailable") from None
            finally:
                probe.close()
            if self.mode == "demo-create":
                demo = ConductorDemo(
                    self.runs_root, self.run_id, workspace_id=self.workspace_id,
                    objective=self.objective,
                )
                demo._base_authorize = demo.authorize
                demo.authorize = self._authorize
                self.demo = demo
                demo.prepare()
                # The fixture's operator scope targets a durable receiver
                # identity so the first real operator send can be queued and
                # reconciled after reopen.  Registration is not a worker
                # launch: no process, provider, or resumed task is started.
                demo.runtime._coordinator.register_worker(
                    OPERATOR_INSTANCE,
                    worker_instance_id=OPERATOR_INSTANCE,
                    capabilities=["operator-message"],
                    permission_ceiling="read-only",
                )
                self.runtime = demo.runtime
                self._presentation_key = secrets.token_bytes(32)
                self._retention_secret = secrets.token_bytes(32)
                self._key_epoch = secrets.token_urlsafe(12)
                _write_host_config(self.runtime, _host_config_value(
                    self.workspace_id, self.run_id, self._presentation_key,
                    self._retention_secret, self._key_epoch))
                scope = _demo_scope(demo)
                # Seed one explicit queued destination so the demo can expose
                # the typed disposition adapter as well as the message adapter.
                command_grant, _ = demo.grant(
                    "main-1", OPERATOR_INSTANCE, suffix="operator-command-grant")
                demo.message("main-1", OPERATOR_INSTANCE, command_grant, 1,
                             "provider-free operator command fixture")
                state, torn = demo.runtime._coordinator._load()
                if torn or not isinstance(state.get("workspace"), dict):
                    raise WorkspaceEntryError("workspace_unprepared")
                deliveries = state["workspace"].get("deliveries", {})
                command_policy = {
                    "schema": COMMAND_POLICY_SCHEMA,
                    "workspace_id": self.workspace_id,
                    "run_id": self.run_id,
                    "generation": 1,
                    "expires_at_ms": int(demo.runtime._coordinator.clock() * 1000) + 300000,
                    "max_active_deliveries": 128,
                    "targets": [{"delivery_id": delivery_id,
                                 "actions": ["cancel_queued_context", "dispose_held_context"]}
                                for delivery_id in deliveries],
                }
                grant_resolver = demo.resolve_grant
            else:
                self.runtime, resolve = self._make_open_runtime()
                config = _read_host_config(self.runtime)
                (self._presentation_key, self._retention_secret,
                 self._key_epoch) = _decode_host_config(
                    config, workspace_id=self.workspace_id, run_id=self.run_id)
                _verify_plan_host_files(self.runtime, config)
                state, torn = self.runtime._coordinator._load()
                if torn:
                    raise WorkspaceEntryError("journal_recovery_required")
                workspace = state.get("workspace")
                if not isinstance(workspace, dict):
                    raise WorkspaceEntryError("workspace_unprepared")
                scope = _scope_from_workspace(
                    workspace, resolve, workspace_id=self.workspace_id, run_id=self.run_id)
                grant_resolver = _grant_resolver(resolve, workspace_id=self.workspace_id,
                                                 run_id=self.run_id)
                command_policy = (_read_command_policy(
                    self.command_policy_file, workspace=workspace,
                    workspace_id=self.workspace_id, run_id=self.run_id)
                    if self.command_policy_file is not None else None)
            message_handle, draft_handle, command_handle = self._install_authority(
                scope, grant_resolver=grant_resolver, command_policy=command_policy)
            if self.command_policy_file is not None:
                self._command_refresh_token = secrets.token_urlsafe(32)
                if self.command_refresh_token_file is not None:
                    _write_private_token(self.command_refresh_token_file,
                                         self._command_refresh_token)
            self.surface = WorkspaceSurface(
                self.runtime._coordinator,
                self.workspace_id,
                operator_commands=(OperatorCommandAdapter(self.runtime, command_handle)
                                   if command_handle is not None else None),
                operator_dispositions=(OperatorDispositionAdapter(self.runtime, command_handle)
                                       if command_handle is not None else None),
                operator_messages=OperatorMessageAdapter(self.runtime, message_handle),
                operator_drafts=OperatorDraftAdapter(self.runtime, draft_handle),
                command_policy_refresh=(self.refresh_command_policy_with_token
                                        if self.command_policy_file is not None else None),
                command_policy_refresh_status=(self.refresh_command_policy_status
                                               if self.command_policy_file is not None else None),
                command_policy_provision=(self.provision_command_policy_for_operator_message_with_token
                                          if self.command_policy_file is not None else None),
                presentation_key=self._presentation_key,
                port=self.port,
            )
            url = self.surface.start()
            self._started = True
            ready = {
                "status": "ready",
                "preview": True,
                "mode": self.mode,
                "qualification": "provider_inert_scripted_demo" if self.mode == "demo-create" else "provider_inert_reopen",
                "run_id": self.run_id,
                "workspace_id": self.workspace_id,
                "url": url,
                # Private handoff only; run_command deliberately removes it.
                "bootstrap_code": self.surface.bootstrap_code,
                "provider_calls": 0,
                "workers_started": 0,
                "workers_resumed": 0,
                "operator_commands": "available" if command_handle is not None else "unavailable",
                "lifecycle": "foreground_owned",
                "stop": "Ctrl+C",
            }
            if self.command_policy_file is not None:
                ready["command_refresh_token"] = self._command_refresh_token
            return ready
        except (WorkspaceEntryError, WorkspaceRuntimeError, WorkspaceUIError,
                SwarmCoordinatorError, OwnershipLostError, ValueError, OSError):
            self.stop()
            raise WorkspaceEntryError(_public_error(sys.exc_info()[1])) from None

    def stop(self):
        self._authority["allowed"] = False
        errors = []
        if self.surface is not None:
            try:
                self.surface.stop()
            except (WorkspaceUIError, OSError):
                errors.append("surface_cleanup_uncertain")
        self.surface = None
        if self.demo is not None:
            try:
                self.demo.cleanup()
            except (ValueError, RuntimeError, OSError):
                errors.append("worker_cleanup_uncertain")
        self._message_handle = self._draft_handle = self._command_handle = None
        self._linked_handle = None
        self._linked_scope = None
        if self._workspace_details is not None:
            self._workspace_details.revoke()
        self._workspace_details = None
        with self._command_policy_lock:
            self._command_policy = None
            self._command_policy_generation = 0
        self._command_refresh_token = None
        self._command_refresh_history.clear()
        self._command_refresh_history_order.clear()
        self._command_provision_bindings.clear()
        self._presentation_key = self._retention_secret = self._key_epoch = None
        self._started = False
        if errors:
            raise WorkspaceEntryError("cleanup_uncertain")

    def inspect(self) -> dict:
        """Generic, read-only inspection; does not use the demo class."""
        try:
            runtime, resolve = self._make_open_runtime()
            _verify_plan_host_files(runtime, _read_host_config(runtime))
            state, torn = runtime._coordinator._load()
            if torn or not isinstance(state.get("workspace"), dict):
                raise WorkspaceEntryError("existing_run_not_readable")
            workspace = state["workspace"]
            if workspace.get("workspace_id") != self.workspace_id:
                raise WorkspaceEntryError("workspace_scope_mismatch")
            # Re-read every registered source to keep the projection source-backed,
            # while returning counts only (no paths, refs, bodies, or prompts).
            for registered in workspace.get("evidence", {}).values():
                resolve(registered)
            return {
                "status": "success",
                "preview": True,
                "mode": "inspect",
                "run_id": self.run_id,
                "workspace_id": self.workspace_id,
                "task_count": len(workspace.get("lanes", {})),
                "evidence_count": len(workspace.get("evidence", {})),
                "delivery_count": len(workspace.get("deliveries", {})),
                "closed": bool(state.get("closed")),
                "provider_calls": 0,
                "workers_resumed": 0,
                "mutation": "none",
            }
        except WorkspaceEntryError:
            raise
        except (SwarmCoordinatorError, OwnershipLostError,
                ValueError, RuntimeError, OSError, KeyError, TypeError):
            raise WorkspaceEntryError("existing_run_not_readable") from None


def _run_foreground(host: WorkspaceHost, *, output, secret_output, interactive: bool) -> int:
    if not interactive:
        return 2
    attempted = False
    started = False
    result = 0
    terminal = {"lifecycle": "foreground_owned", "preview": True, "provider_calls": 0,
                "workers_started": 0, "workers_resumed": 0}
    try:
        # Starting the host is an owned lifecycle boundary.  It may have
        # provisioned a runtime, lock, or listener before an exception (or a
        # Ctrl+C) reaches this wrapper, so cleanup is required even before the
        # ready response has been returned.
        attempted = True
        ready = host.start()
        started = True
        public = {key: value for key, value in ready.items() if key != "bootstrap_code"}
        public.pop("command_refresh_token", None)
        output(json.dumps(public, sort_keys=True), flush=True)
        secret_output(
            "Summon workspace bootstrap code (one-time, keep private): "
            + ready["bootstrap_code"], flush=True)
        if ready.get("command_refresh_token") is not None:
            if host.command_refresh_token_file is not None:
                secret_output(
                    "Summon workspace command refresh token written to its protected file",
                    flush=True)
            else:
                secret_output(
                    "Summon workspace command refresh token (keep private): "
                    + ready["command_refresh_token"], flush=True)
        while True:
            time.sleep(0.25)
    except KeyboardInterrupt:
        if started:
            result = 0
            terminal["status"] = "stopped"
        else:
            result = 2
            terminal.update(status="error", cleanup="pending", error="interrupted")
    except (WorkspaceEntryError, WorkspaceUIError, WorkspaceRuntimeError,
            SwarmCoordinatorError, OwnershipLostError, OSError, ValueError) as exc:
        result = 2
        terminal.update(status="error", cleanup="pending", error=_public_error(exc))
    finally:
        if attempted:
            try:
                host.stop()
            except WorkspaceEntryError:
                result = 2
                terminal.update(status="error", cleanup="uncertain", error="cleanup_uncertain")
            else:
                terminal.setdefault("status", "stopped" if started else "error")
                terminal["cleanup"] = "complete"
        if attempted:
            try:
                output(json.dumps(terminal, sort_keys=True), flush=True)
            except Exception:
                # A closed/captured stdout must not hide the cleanup result;
                # make one best-effort stderr attempt and keep exit 2.
                result = 2
                try:
                    secret_output(json.dumps({"status": "error", "error": "output_unavailable"}), flush=True)
                except Exception:
                    pass
    return result


def run_command(args) -> int:
    """Dispatcher entry used by ``run_subagent.py``; provider-inert."""
    action = getattr(args, "workspace_action", None)
    run_id = getattr(args, "workspace_run_id", None)
    runs_root = getattr(args, "workspace_runs_root", None)
    port = getattr(args, "workspace_port", None)
    objective = getattr(args, "workspace_objective", None)
    plan_file = getattr(args, "workspace_plan_file", None)
    command_policy_file = getattr(args, "workspace_command_policy", None)
    host_url = getattr(args, "workspace_host_url", None)
    control_token_file = getattr(args, "workspace_control_token_file", None)
    refresh_request_id = getattr(args, "workspace_request_id", None)
    provision_operation_key = getattr(args, "workspace_operation_key", None)
    provision_actions = getattr(args, "workspace_actions", None)
    token_file = getattr(args, "workspace_token_file", None)
    request_file = getattr(args, "workspace_request_file", None)
    request_kind = getattr(args, "workspace_request_kind", None)
    lookup = bool(getattr(args, "workspace_lookup", False))
    if action not in {"create", "open", "inspect", "demo-create", "request",
                      "refresh", "refresh-status", "provision-message"}:
        error = "workspace_action_required"
        print(json.dumps({"status": "error", "error": error}, sort_keys=True))
        return 2
    if type(run_id) is not str or not run_id:
        print(json.dumps({"status": "error", "error": "workspace_run_id_required"}, sort_keys=True))
        return 2
    if action not in {"refresh", "refresh-status", "provision-message"} and (type(runs_root) is not str or not runs_root):
        print(json.dumps({"status": "error", "error": "workspace_runs_root_required"}, sort_keys=True))
        return 2
    try:
        if action == "refresh":
            if (type(host_url) is not str or not host_url
                    or type(control_token_file) is not str or not control_token_file):
                raise WorkspaceEntryError("workspace_refresh_options_required")
            if getattr(args, "workspace_interactive", False):
                raise WorkspaceEntryError("workspace_request_noninteractive_only")
            result = workspace_refresh(url=host_url,
                                       control_token_file=control_token_file,
                                       expected_run_id=run_id,
                                       request_id=refresh_request_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "refresh-status":
            if (type(host_url) is not str or not host_url
                    or type(control_token_file) is not str or not control_token_file
                    or type(refresh_request_id) is not str or not refresh_request_id):
                raise WorkspaceEntryError("workspace_refresh_status_options_required")
            if getattr(args, "workspace_interactive", False):
                raise WorkspaceEntryError("workspace_request_noninteractive_only")
            result = workspace_refresh_status(
                url=host_url, control_token_file=control_token_file,
                request_id=refresh_request_id, expected_run_id=run_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "provision-message":
            if (type(host_url) is not str or not host_url
                    or type(control_token_file) is not str or not control_token_file
                    or type(provision_operation_key) is not str or not provision_operation_key
                    or type(provision_actions) is not str or not provision_actions):
                raise WorkspaceEntryError("workspace_provision_options_required")
            if getattr(args, "workspace_interactive", False):
                raise WorkspaceEntryError("workspace_request_noninteractive_only")
            actions = tuple(provision_actions.split(","))
            result = workspace_provision_message(
                url=host_url, control_token_file=control_token_file,
                operation_key=provision_operation_key, actions=actions,
                request_id=refresh_request_id or provision_operation_key,
                expected_workspace_id="workspace", expected_run_id=run_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "request":
            if any(value is None for value in (host_url, token_file, request_file, request_kind)):
                raise WorkspaceEntryError("workspace_request_options_required")
            if getattr(args, "workspace_interactive", False):
                raise WorkspaceEntryError("workspace_request_noninteractive_only")
            result = workspace_request(url=host_url, token_file=token_file,
                                       request_file=request_file,
                                       kind=request_kind, lookup=lookup,
                                       expected_run_id=run_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "create":
            if type(plan_file) is not str or not plan_file:
                raise WorkspaceEntryError("workspace_plan_file_required")
            if (port is not None or objective is not None or command_policy_file is not None
                    or getattr(args, "workspace_interactive", False)):
                raise WorkspaceEntryError("create_options_not_supported")
            result = create_workspace_from_plan(runs_root, run_id, plan_file)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "inspect":
            if port is not None or objective is not None or command_policy_file is not None:
                raise WorkspaceEntryError("inspect_options_not_supported")
            result = WorkspaceHost(runs_root, run_id, 1, mode="open").inspect()
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "open" and objective is not None:
            raise WorkspaceEntryError("objective_only_demo")
        if not getattr(args, "workspace_interactive", False):
            # run_subagent sets this only for a real TTY.  The parser's --json
            # form must never create an inaccessible listener or print a secret.
            print(json.dumps({"status": "error", "error": "interactive_bootstrap_required"}, sort_keys=True))
            return 2
        host = WorkspaceHost(runs_root, run_id, _port(port), mode=action,
                             objective=objective, command_policy_file=command_policy_file,
                             command_refresh_token_file=control_token_file)
        return _run_foreground(
            host, output=print,
            secret_output=lambda text, **kw: print(text, file=sys.stderr, **kw),
            interactive=True,
        )
    except WorkspaceClientError as exc:
        if exc.kind == "outcome_unknown_lookup_required":
            payload = {"status": "unknown", "error": exc.kind,
                       "lookup_required": True, "retry_with_new_key": False}
            if isinstance(getattr(exc, "details", None), dict):
                request_id = exc.details.get("request_id")
                if isinstance(request_id, str):
                    payload["request_id"] = request_id
            print(json.dumps(payload, sort_keys=True))
        else:
            print(json.dumps({"status": "error", "error": exc.kind}, sort_keys=True))
        return 2
    except WorkspaceEntryError as exc:
        print(json.dumps({"status": "error", "error": exc.kind}, sort_keys=True))
        return 2


__all__ = ["WorkspaceEntryError", "WorkspaceHost", "run_command"]
