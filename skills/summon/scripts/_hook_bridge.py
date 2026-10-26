"""Zero-socket, HMAC-authenticated subprocess hook bridge for Summon.

Implements ``summon.hook-bridge/v1``:
- Opt-in / capability-gated (``--in-flight-hooks`` or ``SUMMON_IN_FLIGHT_HOOKS=off|auto|on``).
- Host-only secret isolation: ``<job_root>/.secrets/<job_id>.secret`` is stored outside
  the worker's ``--cwd`` and ``--read-root``s and binds ``hook_state.json`` and
  hook settings files via HMAC-SHA256.
- Synchronous ``PreToolUse`` write-tool denial when ``RuntimeControl`` verified state is
  ``constrained``:
  - Only strict read-only tools (``Read``, ``Glob``, ``Grep``, ``LS``, ``WebFetch``,
    ``WebSearch``, ``TodoRead``) return ``{}`` (passing through to normal CLI permission
    and read-root boundaries without forcing ``allow``).
  - All write, shell, delegation, unknown, or ``mcp__*`` tools return
    ``permissionDecision: "deny"`` synchronously.
- In-flight ``steer`` delivery (``queued`` -> ``emitted``):
  - Undelivered ``steer`` commands and ``steering`` breaker transitions emit
    ``hookSpecificOutput.additionalContext`` on ``PreToolUse`` / ``PostToolUse`` and
    commit ``emitted`` in atomic HMAC-sealed ``<job_root>/<job_id>.hook_state.json``.
  - At-most-once ``emitted`` semantics: if the CLI crashes after ``hook_state.json``
    is committed before consuming stdout, the steer remains in the control log for
    governed resume.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import _jobs
import _job_control


HOOK_BRIDGE_SCHEMA = "summon.hook-bridge/v1"

STRICT_READ_ONLY_TOOLS = frozenset({
    "Read",
    "Glob",
    "Grep",
    "LS",
    "TodoRead",
})

DENIED_WHEN_CONSTRAINED_TOOLS = frozenset({
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "Task",
    "Agent",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
})

_UNSAFE_CMD_CHARS_RE = re.compile(r'["$`\n\r;|&<>]')


class HookBridgeError(ValueError):
    """Raised when hook bridge state or secret verification fails."""


def resolve_hook_mode(cli_flag: bool = False, *, env: dict[str, str] | None = None) -> str:
    """Resolve effective in-flight hook mode: ``off`` | ``auto`` | ``on``."""
    environ = env if env is not None else os.environ
    if cli_flag:
        return "on"
    raw = str(environ.get("SUMMON_IN_FLIGHT_HOOKS", "off")).strip().lower()
    if raw in {"on", "1", "true", "yes"}:
        return "on"
    if raw == "auto":
        return "auto"
    return "off"


def hook_paths(job_root: str, job_id: str) -> dict[str, str]:
    """Return canonical paths for a job's hook bridge files."""
    if not _jobs.valid_job_id(job_id):
        raise HookBridgeError(f"invalid job_id for hook bridge: {job_id!r}")
    root = os.path.abspath(_jobs.resolve_jobs_dir(job_root))
    secrets_dir = os.path.join(root, ".secrets")
    return {
        "job_root": root,
        "secrets_dir": secrets_dir,
        "secret_file": os.path.join(secrets_dir, f"{job_id}.secret"),
        "state_file": os.path.join(root, f"{job_id}.hook_state.json"),
        "lock_file": os.path.join(root, f"{job_id}.hook_state.lock"),
        "settings_file": os.path.join(secrets_dir, f"{job_id}.claude_hooks.json"),
    }


def _unlink_with_retry(path: str) -> None:
    for attempt in range(5):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))


@contextmanager
def _hook_state_lock(lock_file: str, timeout_sec: float = 5.0):
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + max(0.2, timeout_sec)
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            stale = False
            try:
                with open(lock_file, encoding="ascii") as fh:
                    holder = fh.read().strip()
                holder_pid = int(holder.split(":", 1)[0])
                if _jobs._pid_liveness(holder_pid) == "dead":
                    stale = True
            except (OSError, ValueError):
                try:
                    if time.time() - os.path.getmtime(lock_file) > 15.0:
                        stale = True
                except OSError:
                    pass
            if stale:
                dead_tmp = f"{lock_file}.dead.{os.getpid()}.{uuid.uuid4().hex[:8]}"
                try:
                    os.replace(lock_file, dead_tmp)
                    _unlink_with_retry(dead_tmp)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise HookBridgeError("timed out waiting for hook_state.lock")
            time.sleep(0.01)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        yield
    finally:
        try:
            owned = False
            with open(lock_file, encoding="ascii") as fh:
                owned = fh.read().strip() == token
            if owned:
                _unlink_with_retry(lock_file)
        except OSError:
            pass


def _ensure_secrets_dir(secrets_dir: str) -> None:
    os.makedirs(secrets_dir, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(secrets_dir, 0o700)
        except OSError:
            pass


def _load_or_create_secret(secret_file: str) -> bytes:
    if os.path.islink(secret_file):
        raise HookBridgeError("hook secret file cannot be a symlink")
    if os.path.lexists(secret_file):
        st = os.lstat(secret_file)
        if not stat.S_ISREG(st.st_mode):
            raise HookBridgeError("hook secret file is not a regular file")
        raw = Path(secret_file).read_bytes().strip()
        if len(raw) >= 32:
            return raw
        raise HookBridgeError("hook secret file is truncated")
    _ensure_secrets_dir(os.path.dirname(secret_file))
    token = secrets.token_hex(32).encode("ascii")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(secret_file), prefix=".hook-sec-", suffix=".tmp")
    try:
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        _jobs._replace_with_retry(tmp, secret_file)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise HookBridgeError(f"cannot write hook secret file: {exc}") from exc
    return token


def _compute_mac(secret: bytes, payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "mac"}
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hmac.new(secret, b"summon.hook-bridge/v1:" + canonical, hashlib.sha256).hexdigest()


def _initial_hook_state(job_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "schema": HOOK_BRIDGE_SCHEMA,
        "job_id": job_id,
        "generation": 1,
        "created_at": now,
        "updated_at": now,
        "high_water_control_generation": 0,
        "sticky_cancel_requested": False,
        "emitted_generations": [],
        "emitted_commands": [],
        "denied_tool_events": 0,
        "last_event": None,
        "semantics_note": (
            "Steering delivery is recorded as 'emitted' before hook stdout returns; "
            "if the CLI crashes before consuming stdout, the steer remains in "
            "control.json for governed resume."
        ),
    }


def _read_sealed_hook_state(state_file: str, secret: bytes, job_id: str) -> dict[str, Any] | None:
    if not os.path.lexists(state_file):
        return None
    obj, st = _jobs._read(state_file)
    if st != _jobs._OK or not isinstance(obj, dict):
        raise HookBridgeError(f"hook_state.json is unreadable ({st})")
    if obj.get("schema") != HOOK_BRIDGE_SCHEMA or obj.get("job_id") != job_id:
        raise HookBridgeError("hook_state.json schema or job_id mismatch")
    mac = obj.get("mac")
    if not isinstance(mac, str) or len(mac) != 64:
        raise HookBridgeError("hook_state.json MAC is missing")
    expected = _compute_mac(secret, obj)
    if not hmac.compare_digest(mac, expected):
        raise HookBridgeError("hook_state.json HMAC verification failed")
    return obj


def _write_sealed_hook_state(state_file: str, secret: bytes, state: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in state.items() if k != "mac"}
    payload["updated_at"] = time.time()
    payload["mac"] = _compute_mac(secret, payload)
    _jobs._atomic_write_json(state_file, payload)
    return payload


def read_hook_state(job_root: str, job_id: str) -> dict[str, Any] | None:
    """Read and verify the HMAC-sealed ``hook_state.json`` for ``job_id``."""
    paths = hook_paths(job_root, job_id)
    if not os.path.lexists(paths["state_file"]):
        return None
    with _hook_state_lock(paths["lock_file"]):
        secret = _load_or_create_secret(paths["secret_file"])
        return _read_sealed_hook_state(paths["state_file"], secret, job_id)


def _assert_secrets_outside_worker_roots(
    secrets_dir: str,
    worker_cwd: str | None = None,
    read_roots: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Verify the hook secrets directory is outside the worker's cwd and read_roots."""
    secrets_real = os.path.normcase(os.path.realpath(os.path.abspath(secrets_dir)))
    candidate_roots: list[str] = []
    if isinstance(worker_cwd, str) and worker_cwd.strip():
        candidate_roots.append(worker_cwd)
    for r in read_roots or ():
        if isinstance(r, str) and r.strip():
            candidate_roots.append(r)
    for root in candidate_roots:
        root_real = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        try:
            common = os.path.normcase(os.path.commonpath([secrets_real, root_real]))
        except ValueError:
            # Different drives on Windows -> disjoint
            continue
        if common in (secrets_real, root_real):
            raise HookBridgeError(
                "hook secrets directory must be outside worker cwd and read_roots"
            )


def prepare_claude_hook_settings(
    job_root: str,
    job_id: str,
    *,
    script_path: str | None = None,
    python_exe: str | None = None,
    worker_cwd: str | None = None,
    read_roots: list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Create the private Claude Code hook settings file and initialize ``hook_state.json``."""
    paths = hook_paths(job_root, job_id)
    _assert_secrets_outside_worker_roots(
        paths["secrets_dir"], worker_cwd=worker_cwd, read_roots=read_roots
    )
    py = python_exe or sys.executable
    bridge_script = os.path.abspath(script_path or __file__)
    for label, val in (
        ("python_exe", py),
        ("script_path", bridge_script),
        ("job_root", paths["job_root"]),
        ("job_id", job_id),
    ):
        if _UNSAFE_CMD_CHARS_RE.search(str(val)):
            raise HookBridgeError(
                f"unsafe shell character in hook {label}: {val!r}"
            )

    _ensure_secrets_dir(paths["secrets_dir"])
    with _hook_state_lock(paths["lock_file"]):
        secret = _load_or_create_secret(paths["secret_file"])
        state = _read_sealed_hook_state(paths["state_file"], secret, job_id)
        if state is None:
            state = _initial_hook_state(job_id)
            _write_sealed_hook_state(paths["state_file"], secret, state)

    def _cmd_for(event_name: str) -> str:
        if os.name != "nt":
            return " ".join(
                shlex.quote(part)
                for part in (
                    py,
                    bridge_script,
                    "--job-root",
                    paths["job_root"],
                    "--job-id",
                    job_id,
                    "--event",
                    event_name,
                )
            )
        return (
            f'"{py}" "{bridge_script}" '
            f'--job-root "{paths["job_root"]}" '
            f'--job-id "{job_id}" '
            f'--event "{event_name}"'
        )

    settings_payload = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd_for("PreToolUse"),
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd_for("PostToolUse"),
                        }
                    ],
                }
            ],
        }
    }
    _jobs._atomic_write_json(paths["settings_file"], settings_payload)
    return paths


def is_tool_allowed_when_constrained(tool_name: str | None) -> bool:
    """Return True ONLY for strict read-only tools; deny write, shell, unknown, and ``mcp__*``."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return False
    normalized = tool_name.strip()
    if normalized.lower().startswith("mcp__"):
        return False
    if normalized in DENIED_WHEN_CONSTRAINED_TOOLS:
        return False
    return normalized in STRICT_READ_ONLY_TOOLS


def _verified_job_control_snapshot(job_root: str, job_id: str) -> dict[str, Any]:
    """Read authenticated control state (constrained, stopped, cancel, steers) for ``job_id``."""
    rec, st = _jobs._read(_jobs.record_path(job_root, job_id))
    if st != _jobs._OK or not isinstance(rec, dict):
        raise HookBridgeError(f"job {job_id!r} launch record is unavailable")
    nonce = rec.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise HookBridgeError(f"job {job_id!r} launch record has no control nonce")

    ctrl_path = _job_control.control_path(job_root, job_id)
    if not os.path.lexists(ctrl_path):
        return {
            "constrained": False,
            "stopped": False,
            "cancel_requested": False,
            "max_generation": 0,
            "steers": [],
        }

    data, ctrl_state = _jobs._read(ctrl_path)
    if ctrl_state != _jobs._OK or not isinstance(data, dict):
        raise HookBridgeError("job control file is malformed")

    commands, integrity = _job_control._validated_commands(data, job_id, nonce)
    if commands is None or integrity != "authenticated":
        raise HookBridgeError("job control file failed authentication")

    breaker_level = "healthy"
    cancel_requested = False
    max_generation = 0
    steers: list[dict[str, Any]] = []

    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        gen = int(cmd.get("generation") or 0)
        if gen > max_generation:
            max_generation = gen
        action = cmd.get("action")
        if action == "cancel":
            cancel_requested = True
        elif action == "steer" and isinstance(cmd.get("message"), str):
            steers.append({
                "generation": gen,
                "message": cmd["message"],
                "message_sha256": cmd.get("message_sha256"),
                "source": "operator_steer",
            })
        elif action == "breaker":
            to_level = cmd.get("to_level")
            mode = cmd.get("mode")
            if mode == "enforce" and isinstance(to_level, str) and to_level:
                breaker_level = to_level
                if to_level == "steering" and isinstance(cmd.get("message"), str):
                    steers.append({
                        "generation": gen,
                        "message": cmd["message"],
                        "message_sha256": cmd.get("message_sha256"),
                        "source": "breaker_steering",
                    })

    return {
        "constrained": breaker_level == "constrained",
        "stopped": breaker_level == "stopped",
        "cancel_requested": cancel_requested,
        "max_generation": max_generation,
        "steers": steers,
    }


def evaluate_hook_event(
    job_root: str,
    job_id: str,
    *,
    event_name: str,
    hook_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a ``PreToolUse`` or ``PostToolUse`` event against verified control state.

    Returns the JSON object to write to hook stdout:
    - When ``constrained`` on ``PreToolUse``:
      - If ``tool_name`` is in ``STRICT_READ_ONLY_TOOLS``, does NOT return ``"allow"``
        (returns ``{}`` or only ``additionalContext`` so normal CLI permission and
        read-root checks still apply!).
      - Otherwise returns ``permissionDecision: "deny"`` synchronously.
    - When undelivered ``steer`` commands exist, attaches ``additionalContext`` and
      atomically records ``status: "emitted"`` in ``hook_state.json``.
    """
    if event_name not in {"PreToolUse", "PostToolUse"}:
        return {}
    payload = hook_input if isinstance(hook_input, dict) else {}
    tool_name = payload.get("tool_name") or payload.get("toolName")
    paths = hook_paths(job_root, job_id)

    try:
        ctrl = _verified_job_control_snapshot(paths["job_root"], job_id)
    except HookBridgeError as exc:
        # Fail closed on PreToolUse when control log or launch record is tampered/corrupt
        if event_name == "PreToolUse":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Summon control verification failed ({exc}); tool execution denied.",
                }
            }
        return {}

    with _hook_state_lock(paths["lock_file"]):
        secret = _load_or_create_secret(paths["secret_file"])
        state = _read_sealed_hook_state(paths["state_file"], secret, job_id)
        if state is None:
            state = _initial_hook_state(job_id)

        high_water = int(state.get("high_water_control_generation") or 0)
        ctrl_max_gen = int(ctrl.get("max_generation") or 0)
        rollback_detected = ctrl_max_gen < high_water
        if ctrl_max_gen > high_water:
            state["high_water_control_generation"] = ctrl_max_gen

        if ctrl["cancel_requested"]:
            state["sticky_cancel_requested"] = True
        effective_cancel = bool(ctrl["cancel_requested"] or state.get("sticky_cancel_requested"))

        emitted_gens = set(
            int(g) for g in (state.get("emitted_generations") or [])
            if isinstance(g, int) and not isinstance(g, bool)
        )
        pending_steers = [
            s for s in ctrl["steers"]
            if isinstance(s.get("generation"), int)
            and s["generation"] not in emitted_gens
            and isinstance(s.get("message"), str)
            and s["message"].strip()
        ]

        deny_reason: str | None = None
        if event_name == "PreToolUse":
            if rollback_detected:
                deny_reason = (
                    "Summon control log rollback or deletion detected; tool execution denied."
                )
            elif effective_cancel or ctrl["stopped"]:
                deny_reason = (
                    "Summon job cancellation/stop is active; tool execution denied."
                )
            elif ctrl["constrained"] and not is_tool_allowed_when_constrained(tool_name):
                deny_reason = (
                    f"Summon circuit breaker is in 'constrained' (read-only containment) mode; "
                    f"tool {tool_name!r} is denied. Only strict read-only inspection tools "
                    f"({', '.join(sorted(STRICT_READ_ONLY_TOOLS))}) are permitted."
                )

        additional_context: str | None = None
        if pending_steers:
            messages = [s["message"].strip() for s in pending_steers]
            additional_context = (
                "[Summon Operator Steering]\n" + "\n\n".join(messages)
            )
            now = time.time()
            emitted_list = list(state.get("emitted_commands") or [])
            for s in pending_steers:
                gen = int(s["generation"])
                emitted_gens.add(gen)
                emitted_list.append({
                    "generation": gen,
                    "status": "emitted",
                    "source": s.get("source", "operator_steer"),
                    "message_sha256": s.get("message_sha256"),
                    "emitted_at": now,
                    "hook_event": event_name,
                })
            state["emitted_generations"] = sorted(emitted_gens)
            state["emitted_commands"] = emitted_list[-128:]

        if deny_reason is not None:
            state["denied_tool_events"] = int(state.get("denied_tool_events") or 0) + 1

        state["last_event"] = {
            "hook_event": event_name,
            "tool_name": str(tool_name) if tool_name else None,
            "decision": "deny" if deny_reason else "passthrough",
            "steers_emitted": len(pending_steers),
            "at": time.time(),
        }
        state["generation"] = int(state.get("generation", 1)) + 1
        _write_sealed_hook_state(paths["state_file"], secret, state)

    if deny_reason is not None:
        out_hook: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_reason,
        }
        if additional_context:
            out_hook["additionalContext"] = additional_context
        return {"hookSpecificOutput": out_hook}

    if additional_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            }
        }

    # IMPORTANT (Astra P1 #5): Return {} rather than permissionDecision="allow"
    # so normal CLI permission flags and --add-dir read-root boundaries still apply.
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summon subprocess hook bridge")
    parser.add_argument("--job-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--event", required=True, choices=("PreToolUse", "PostToolUse"))
    args = parser.parse_args(argv)

    raw_stdin = ""
    try:
        if not sys.stdin.isatty():
            raw_stdin = sys.stdin.read(256 * 1024)
    except OSError:
        raw_stdin = ""

    hook_input: dict[str, Any] = {}
    if raw_stdin.strip():
        try:
            parsed = json.loads(raw_stdin)
            if isinstance(parsed, dict):
                hook_input = parsed
        except (ValueError, TypeError):
            hook_input = {}

    try:
        response = evaluate_hook_event(
            args.job_root,
            args.job_id,
            event_name=args.event,
            hook_input=hook_input,
        )
    except Exception as exc:  # noqa: BLE001
        if args.event == "PreToolUse":
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Summon hook bridge error ({type(exc).__name__})",
                }
            }
        else:
            response = {}

    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
