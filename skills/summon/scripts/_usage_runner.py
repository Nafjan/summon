"""Bounded execution boundary for explicitly consented usage refreshes.

Only the reviewed Codex app-server plan is accepted.  Raw output is retained in
memory only long enough for :mod:`_usage_live` to redact and normalize it; this
module never logs diagnostics, retries, logs in, or launches a model turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

import _usage_live
from _spawn import popen_flags


class UsageRunnerError(ValueError):
    pass


def _command(executable: str, *args: str) -> list[str]:
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        rendered = subprocess.list2cmdline([executable, *args])
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", rendered]
    return [executable, *args]


def _validate_plan(plan: Any) -> dict:
    if not isinstance(plan, dict):
        raise UsageRunnerError("usage runner plan must be an object")
    expected = _usage_live.codex_request_plan(
        allow_account_usage_read=True, dry_run=False)
    if plan != expected:
        raise UsageRunnerError("usage runner accepts only the reviewed Codex plan")
    methods = [item.get("method") for item in plan["requests"]]
    if methods != ["initialize", "initialized", "account/read",
                   "account/rateLimits/read"]:
        raise UsageRunnerError("usage runner method sequence is invalid")
    return plan


def _drain(stream, limit: int, sink: bytearray, overflow: threading.Event) -> None:
    try:
        read_chunk = getattr(stream, "read1", None)
        if read_chunk is None:
            read_chunk = stream.read
        while True:
            # BufferedReader.read(n) may wait for n bytes or EOF, which hides
            # short JSONL responses from the liveness loop. read1() returns the
            # bytes currently available while preserving the hard allocation cap.
            chunk = read_chunk(4096)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            remaining = max(0, limit - len(sink))
            if remaining:
                sink.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
    except (OSError, ValueError):
        overflow.set()


def _kill_owned(process: subprocess.Popen) -> None:
    from _executor import _kill_tree
    _kill_tree(process)


def _attach_owned(process: subprocess.Popen) -> bool:
    from _jobobj import attach
    return attach(process)


def _close_owned(process: subprocess.Popen) -> bool:
    from _jobobj import close
    return close(process)


def _execute(args: list[str], *, input_bytes: bytes, timeout_ms: int,
             max_stdout: int, max_stderr: int,
             command_timeout_ms: int | None = None,
             progress_probe: Callable[[bytes], int] | None = None,
             completion_target: int | None = None,
             popen_factory: Callable[..., Any] = subprocess.Popen,
             monotonic: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             kill_tree: Callable[[Any], None] = _kill_owned,
             attach_job: Callable[[Any], bool] = _attach_owned,
             close_job: Callable[[Any], bool] = _close_owned) -> dict:
    started = monotonic()
    process = popen_factory(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, **popen_flags())
    try:
        # Match the main backend executor: on Windows a kill-on-close Job Object
        # keeps descendants reachable even if the CLI leader exits first.  A
        # failed attachment is soft; _kill_owned retains the taskkill fallback.
        attach_job(process)
    except Exception:  # noqa: BLE001 - teardown plumbing must fail closed later
        pass
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    threads = [
        threading.Thread(target=_drain, args=(process.stdout, max_stdout, stdout, overflow),
                         daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, max_stderr, stderr, overflow),
                         daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        if process.stdin is None:
            raise OSError("usage runner stdin is unavailable")
        process.stdin.write(input_bytes)
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass

    deadline = started + timeout_ms / 1000.0
    progress = 0
    progress_at = started
    completed = False
    error_kind = None
    while process.poll() is None:
        if overflow.is_set():
            error_kind = "backend_execution_failed"
            kill_tree(process)
            break
        current = monotonic()
        if progress_probe is not None:
            try:
                observed = progress_probe(bytes(stdout))
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                error_kind = "backend_execution_failed"
                kill_tree(process)
                break
            if observed > progress:
                progress = observed
                progress_at = current
            if completion_target is not None and observed >= completion_target:
                # The reviewed response set is complete. The stdio app-server
                # need not define EOF shutdown behavior for this one-shot read;
                # reap only this owned tree and normalize the completed exchange.
                completed = True
                kill_tree(process)
                break
        if (command_timeout_ms is not None
                and current >= progress_at + command_timeout_ms / 1000.0):
            error_kind = "usage_refresh_command_timeout"
            kill_tree(process)
            break
        if current >= deadline:
            error_kind = "usage_refresh_timeout"
            kill_tree(process)
            break
        sleep(0.01)
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        kill_tree(process)
    try:
        # Close before joining drains so a descendant that inherited a pipe is
        # terminated and cannot hold the reader thread past the bounded join.
        close_job(process)
    except Exception:  # noqa: BLE001 - live drains/reap state are checked below
        pass
    for thread in threads:
        thread.join(timeout=1)
    elapsed_ms = max(0, int((monotonic() - started) * 1000))
    exit_code = process.poll()
    teardown_failed = not isinstance(exit_code, int) or any(
        thread.is_alive() for thread in threads)
    if overflow.is_set():
        # A drain may discover buffered excess only after the response set was
        # complete. Output bounds always outrank logical completion.
        error_kind = "backend_execution_failed"
        exit_code = 1
    elif teardown_failed:
        # Never certify completion while the owned process or a pipe drain is
        # still live after bounded teardown.
        error_kind = "backend_execution_failed"
        exit_code = 1
    elif completed:
        exit_code = 0
    elif error_kind in {"usage_refresh_timeout", "usage_refresh_command_timeout"}:
        exit_code = 124
    elif not isinstance(exit_code, int):
        exit_code = 1
    return {
        "stdout": bytes(stdout), "stderr": bytes(stderr),
        "exit_code": exit_code, "elapsed_ms": min(elapsed_ms, timeout_ms),
        # Writing a request to a local child is not proof that the account
        # endpoint was reached. run_plan upgrades this only after bounded
        # response records for both reviewed account methods are present.
        "provider_contacted": None,
        "error_kind": error_kind,
    }


def _version(executable: str, **kwargs) -> str | None:
    result = _execute(
        _command(executable, "--version"), input_bytes=b"", timeout_ms=5_000,
        max_stdout=512, max_stderr=512, **kwargs)
    if result["exit_code"] != 0:
        return None
    combined = result["stdout"] + b"\n" + result["stderr"]
    lines = [line.strip() for line in combined.decode("utf-8", errors="replace").splitlines()
             if line.strip()]
    return lines[0][:120] if lines else None


def _contact_from_stdout(raw: bytes) -> bool | None:
    results = set()
    errors = set()
    try:
        for line in raw.splitlines():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("id") in (2, 3):
                if "result" in value:
                    results.add(value["id"])
                elif "error" in value:
                    errors.add(value["id"])
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    # A local JSON-RPC server can generate an error without crossing an account
    # boundary. Only two successful account-method results prove contact.
    return True if results == {2, 3} and not errors else None


def _response_progress(raw: bytes) -> int:
    """Count unique reviewed response ids; notifications and prose do not renew liveness."""
    ids = set()
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if (isinstance(value, dict) and value.get("id") in (1, 2, 3)
                and ("result" in value or "error" in value)):
            ids.add(value["id"])
    return len(ids)


def run_plan(plan: dict, *, which: Callable[[str], str | None] = shutil.which,
             popen_factory: Callable[..., Any] = subprocess.Popen,
             monotonic: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             kill_tree: Callable[[Any], None] = _kill_owned) -> dict:
    """Execute one exact Codex usage plan and return the bounded runner contract."""
    plan = _validate_plan(plan)
    executable = which("codex")
    if not executable:
        return {
            "stdout": b"", "stderr": b"", "exit_code": 1, "elapsed_ms": 0,
            "provider_contacted": False, "cli_version": None,
            "error_kind": "backend_execution_failed",
        }
    injected = {
        "popen_factory": popen_factory, "monotonic": monotonic,
        "sleep": sleep, "kill_tree": kill_tree,
    }
    cli_version = _version(executable, **injected)
    if cli_version != _usage_live.CODEX_SCHEMA_CLI_VERSION:
        return {
            "stdout": b"", "stderr": b"", "exit_code": 1, "elapsed_ms": 0,
            "provider_contacted": False, "cli_version": cli_version,
            "error_kind": "backend_execution_failed",
        }
    payload = b"".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n"
        for item in plan["requests"])
    result = _execute(
        _command(executable, "app-server"), input_bytes=payload,
        timeout_ms=plan["total_timeout_ms"],
        command_timeout_ms=plan["command_timeout_ms"],
        progress_probe=_response_progress,
        completion_target=3,
        max_stdout=plan["max_stdout_bytes"],
        max_stderr=plan["max_stderr_bytes"], **injected)
    result["cli_version"] = cli_version
    result["provider_contacted"] = _contact_from_stdout(result["stdout"])
    return result


__all__ = ["UsageRunnerError", "run_plan"]
