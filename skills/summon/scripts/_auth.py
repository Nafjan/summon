"""Explicit provider-auth recovery for Summon.

Authentication repair is deliberately separate from dispatch. A failed provider
call produces a safe repair plan, but Summon never launches a login flow or
retries a potentially billable request unless the caller explicitly runs
``summon auth repair BACKEND --allow-auth-repair``. Vendor login commands may
open a browser and still require the human to approve the flow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

from _spawn import popen_flags


def _plan(backend: str) -> dict[str, Any] | None:
    from _doctor import auth_repair_plan
    return auth_repair_plan(backend)


def _portable_executable(backend: str) -> str | None:
    """Resolve only the executable name; never return the path in a report."""
    return shutil.which(backend)


def _command_for(backend: str, plan: dict[str, Any]) -> list[str]:
    argv = [str(value) for value in plan.get("argv") or []]
    if not argv:
        raise ValueError(f"no auth command is registered for {backend!r}")
    if backend == "zcode" and argv[0] == "zcode":
        # ZCode commonly ships only as a desktop-bundled Node CLI, so PATH can
        # be empty even though doctor and dispatch can use it. Reuse the same
        # direct-target resolver; never route a registry value through a shell.
        from _zcode import resolve_zcode_cli
        target = resolve_zcode_cli()
        if target is None:
            raise FileNotFoundError("zcode is not installed or discoverable")
        return [target.command, *target.prefix_args, *argv[1:]]
    executable = _portable_executable(argv[0])
    if not executable:
        raise FileNotFoundError(f"{backend} is not installed or not on PATH")
    # npm shims cannot be executed directly with shell=False on Windows. Keep
    # the command allowlisted and route only the shim through cmd.exe; no user
    # input is interpolated into a shell command.
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", executable, *argv[1:]]
    return [executable, *argv[1:]]


def _repair(backend: str, *, allow: bool, timeout_s: float = 300.0) -> dict[str, Any]:
    plan = _plan(backend)
    if plan is None:
        return {
            "status": "error", "error_kind": "unknown_backend",
            "backend": backend, "message": f"no auth repair plan for {backend!r}",
        }
    if not allow:
        return {
            "status": "blocked", "error_kind": "authentication_repair_not_authorized",
            "backend": backend, "auth": plan,
            "message": (f"authentication repair for {backend} was not started; run "
                         f"`{plan['command']}` yourself or explicitly authorize "
                         f"`{plan['authorized_command']}`"),
        }
    if not plan.get("supports_autonomous"):
        return {
            "status": "blocked", "error_kind": "authentication_repair_requires_user",
            "backend": backend, "auth": plan,
            "message": (plan.get("note") or
                         f"run `{plan['command']}` in an interactive terminal, then retry"),
        }
    try:
        command = _command_for(backend, plan)
    except (OSError, ValueError) as exc:
        return {
            "status": "error", "error_kind": "backend_not_installed",
            "backend": backend, "auth": plan,
            "message": str(exc),
        }
    started = time.monotonic()
    try:
        # Keep vendor output off the JSON protocol. stderr remains inherited so
        # a browser fallback URL or a vendor's human-facing instruction is still
        # visible in a real terminal; Summon never captures or stores it.
        process = subprocess.Popen(
            command, stdin=None, stdout=subprocess.DEVNULL, stderr=None,
            shell=False, **popen_flags(),
        )
        try:
            code = process.wait(timeout=max(1.0, float(timeout_s)))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            return {
                "status": "error", "error_kind": "authentication_repair_timeout",
                "backend": backend, "auth": plan,
                "message": (f"`{plan['command']}` did not finish within the bounded "
                             f"{int(timeout_s)}s login window; finish it manually, then retry"),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
    except OSError as exc:
        return {
            "status": "error", "error_kind": "authentication_repair_failed",
            "backend": backend, "auth": plan,
            "message": f"could not start `{plan['command']}`: {type(exc).__name__}",
        }
    if code == 0:
        return {
            "status": "success", "backend": backend, "auth": plan,
            "message": (f"`{plan['command']}` exited successfully. If the browser asked "
                         "for approval, complete it before retrying the original dispatch."),
            "retry_required": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "status": "error", "error_kind": "authentication_repair_failed",
        "backend": backend, "auth": plan,
        "message": (f"`{plan['command']}` exited with code {code}; no credentials were "
                     "captured. Complete the vendor login manually, then retry."),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _status(*, backend: str | None = None, probe: bool = False) -> dict[str, Any]:
    from _doctor import doctor
    report = doctor(probe=probe)
    names = [backend] if backend else list(report.get("backends") or {})
    rows = []
    for name in names:
        item = (report.get("backends") or {}).get(name)
        plan = _plan(name)
        if item is None:
            rows.append({"backend": name, "status": "unknown", "auth": plan})
            continue
        if item.get("auth_ok") is True:
            state = "authenticated"
        elif item.get("auth_ok") is False:
            state = "authentication_failed"
        elif not item.get("found"):
            state = "not_installed"
        else:
            state = "unverified"
        rows.append({
            "backend": name, "status": state,
            "found": bool(item.get("found")),
            "verified": bool(item.get("verified")),
            "auth": plan,
        })
    return {
        "status": "success", "probe_ran": bool(probe), "backends": rows,
        "note": ("Authentication is unverified until `--probe` performs a minimal live "
                 "check. Login commands may open a browser and still require approval."),
    }


def run_auth_action(action: str, *, backend: str | None = None,
                    allow: bool = False, probe: bool = False,
                    timeout_s: float = 300.0) -> dict[str, Any]:
    """Run a local auth-management action without dispatching a model."""
    if action == "status":
        return _status(backend=backend, probe=probe)
    if action == "repair":
        if not backend:
            return {"status": "error", "error_kind": "missing_backend",
                    "message": "auth repair requires a backend (for example `kimi`)"}
        return _repair(backend, allow=allow, timeout_s=timeout_s)
    return {"status": "error", "error_kind": "unknown_auth_action",
            "message": f"unknown auth action {action!r}"}
