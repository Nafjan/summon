"""Safe browser handoff for the authenticated deliberation surface.

This module is deliberately small and provider-inert.  It owns the difference
between a durable loopback surface and a browser process: one live surface per
run, an explicit IDE bridge when configured, and a system-browser fallback.
URLs are validated as local authenticated surface URLs before they are handed
to either launcher.
"""

from __future__ import annotations

import os
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import _deliberation_store as _store
import _deliberation_ui as _ui
import _rundir
from _spawn import popen_flags


class BrowserOpenError(Exception):
    """A browser handoff was refused or could not be started."""


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
BRIDGE_ENV = (
    "SUMMON_BROWSER_BRIDGE",
    "CODEX_BROWSER_BRIDGE",
    "VSCODE_BROWSER_BRIDGE",
    "CURSOR_BROWSER_BRIDGE",
    "ANTIGRAVITY_BROWSER_BRIDGE",
)
OPEN_LOCK = ".summon-deliberation-ui.open"
OPEN_LOCK_STALE_SECONDS = 30.0
OPEN_WAIT_SECONDS = 5.0
MAX_SURFACE_RECORD_BYTES = 16 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _validate_url(url: str) -> dict[str, object]:
    if not isinstance(url, str) or len(url) > 512:
        raise BrowserOpenError("surface URL is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.query):
        raise BrowserOpenError("surface URL is not a loopback URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserOpenError("surface URL has an invalid port") from exc
    if not port or not (1 <= port <= 65535):
        raise BrowserOpenError("surface URL has an invalid port")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "runs" or not RUN_ID_RE.fullmatch(parts[1]):
        raise BrowserOpenError("surface URL has an invalid run route")
    if not parsed.fragment.startswith("token="):
        raise BrowserOpenError("surface URL has no bearer token")
    token = unquote(parsed.fragment[6:])
    if not TOKEN_RE.fullmatch(token):
        raise BrowserOpenError("surface URL has an invalid bearer token")
    return {"run_id": parts[1], "token": token, "port": port}


def _bridge_executable() -> str | None:
    for name in BRIDGE_ENV:
        value = os.environ.get(name)
        if not value:
            continue
        # An IDE bridge is one executable, not a shell command.  This prevents
        # an environment value such as ``bridge && curl ...`` from becoming a
        # command injection surface.
        if "\x00" in value or "\r" in value or "\n" in value:
            raise BrowserOpenError(f"{name} contains control characters")
        executable = value if os.path.isfile(value) else shutil.which(value)
        if not executable:
            raise BrowserOpenError(f"{name} does not name an executable")
        return os.path.abspath(executable)
    return None


def _builtin_browser_executable() -> str | None:
    """Find Codex/IDE's optional Browser Harness without making it required."""
    backends = {part.strip().lower() for part in
                os.environ.get("BROWSER_USE_AVAILABLE_BACKENDS", "").split(",")}
    if "iab" not in backends:
        return None
    return shutil.which("browser-harness")


def _open_builtin(url: str, executable: str, *, details: dict[str, object]) -> dict[str, object]:
    """Ask the local Browser Harness to reuse a matching tab or open one.

    The harness receives a short Python program on stdin rather than a shell
    command.  The URL has already passed the strict loopback/token validator and
    is encoded as a Python string literal, so it cannot add executable code.
    """
    target = json.dumps(url, ensure_ascii=True)
    script = f'''target = {target}
base = target.split("#", 1)[0]
found = False
for tab in list_tabs(include_chrome=False):
    if str(tab.get("url", "")).split("#", 1)[0] == base:
        switch_tab(tab)
        found = True
        break
if not found:
    new_tab(target)
print(page_info())
'''
    try:
        process = subprocess.Popen(
            [executable], shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_flags())
        process.communicate(script.encode("utf-8"), timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()  # type: ignore[has-type]
        except Exception:  # noqa: BLE001 - best-effort child cleanup
            pass
        raise BrowserOpenError("built-in IDE browser could not be started") from exc
    if process.returncode not in (0, None):
        raise BrowserOpenError("built-in IDE browser refused the link")
    return {"url": url, "target": "builtin", "opened": True, "reused": True,
            "run_id": details["run_id"]}


def open_url(url: str, *, mode: str = "auto") -> dict[str, object]:
    """Open a validated local surface URL.

    ``auto`` prefers an explicit IDE bridge, then the optional integrated
    Browser Harness, and otherwise asks the system browser to reuse an existing
    window (``new=0``).  ``link`` is deterministic and never launches anything,
    which is useful for SSH/CI and scripts.
    """
    details = _validate_url(url)
    if mode not in {"auto", "builtin", "ide", "system", "link"}:
        raise BrowserOpenError("browser mode must be auto, builtin, ide, system, or link")
    if mode == "link":
        return {"url": url, "target": "link", "opened": False, "reused": False,
                "run_id": details["run_id"]}
    bridge = _bridge_executable() if mode in {"auto", "ide"} else None
    if mode == "ide" and not bridge:
        raise BrowserOpenError(
            "no IDE browser bridge is configured; set SUMMON_BROWSER_BRIDGE "
            "(or use --browser system/link)")
    if bridge:
        try:
            subprocess.Popen([bridge, url], shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **popen_flags())
        except OSError as exc:
            raise BrowserOpenError("IDE browser bridge could not be started") from exc
        return {"url": url, "target": "ide", "opened": True, "reused": True,
                "run_id": details["run_id"]}
    builtin = _builtin_browser_executable() if mode in {"auto", "builtin"} else None
    if mode == "builtin" and not builtin:
        raise BrowserOpenError(
            "the built-in IDE browser is unavailable; retry with --browser system/link")
    if builtin:
        return _open_builtin(url, builtin, details=details)
    try:
        opened = bool(webbrowser.open(url, new=0, autoraise=True))
    except Exception as exc:  # noqa: BLE001 - browser plugins are third-party
        raise BrowserOpenError("system browser could not be started") from exc
    if not opened:
        raise BrowserOpenError(
            "system browser did not accept the link; retry with --browser link")
    return {"url": url, "target": "system", "opened": True, "reused": True,
            "run_id": details["run_id"]}


def _lock_path(root: str, run_id: str) -> str:
    return os.path.join(_store.run_dir(root, run_id), OPEN_LOCK)


def _surface_reachable(record: dict[str, object]) -> bool:
    """Prove a shaped record belongs to a responding authenticated surface."""
    url = record.get("url")
    token = record.get("token")
    run_id = record.get("run_id")
    if not isinstance(url, str) or not isinstance(token, str) or not isinstance(run_id, str):
        return False
    try:
        details = _validate_url(url)
        endpoint = f"http://127.0.0.1:{details['port']}/api/v1/runs/{run_id}/snapshot"
        request = Request(endpoint, headers={
            "Host": f"127.0.0.1:{details['port']}",
            "Authorization": "Bearer " + token,
        })
        # A local observer must not follow a redirect with a bearer header.
        opener = build_opener(_NoRedirect)
        with opener.open(request, timeout=0.35) as response:
            return response.status == 200
    except (OSError, ValueError, HTTPError, URLError):
        return False


def _discard_stale_record(root: str, run_id: str, record: dict[str, object] | None) -> None:
    """Remove only the exact shaped record that failed its endpoint probe."""
    if not isinstance(record, dict):
        return
    pid, token = record.get("pid"), record.get("token")
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(token, str) or not TOKEN_RE.fullmatch(token)):
        return
    try:
        path = Path(_ui._surface_record_path(root, run_id))
        if path.is_symlink() or path.stat().st_size > MAX_SURFACE_RECORD_BYTES:
            return
        current = _rundir.read_json(str(path))
        if (isinstance(current, dict) and current.get("run_id") == run_id
                and current.get("pid") == pid and current.get("token") == token):
            path.unlink()
    except (OSError, ValueError):
        return


def _claim_open_lock(path: str) -> str | None:
    token = secrets.token_hex(16)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            observed = os.stat(path)
            if time.time() - observed.st_mtime > OPEN_LOCK_STALE_SECONDS:
                data = _rundir.read_json(path)
                if (isinstance(data, dict) and isinstance(data.get("token"), str)
                        and data.get("pid") is not None
                        and not _ui._pid_is_alive(data.get("pid"))
                        and os.stat(path).st_mtime_ns == observed.st_mtime_ns):
                    os.unlink(path)
                    return _claim_open_lock(path)
        except (OSError, ValueError):
            pass
        return None
    except OSError:
        return None
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        json.dump({"pid": os.getpid(), "token": token, "started_at": time.time()}, fh)
    return token


def _release_open_lock(path: str, token: str | None) -> None:
    if not isinstance(token, str):
        return
    try:
        data = _rundir.read_json(path)
        if not isinstance(data, dict) or data.get("token") != token:
            return
        os.unlink(path)
    except (OSError, ValueError):
        return


def _wait_for_surface(root: str, run_id: str, process: subprocess.Popen | None = None) -> dict[str, object]:
    deadline = time.monotonic() + OPEN_WAIT_SECONDS
    while time.monotonic() < deadline:
        record = _ui.read_surface_record(root, run_id)
        if (record is not None
                and (process is None or record.get("pid") == process.pid)
                and _surface_reachable(record)):
            return record
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.05)
    raise BrowserOpenError("deliberation surface did not become ready")


def ensure_surface(root: str, run_id: str) -> dict[str, object]:
    """Return a stable surface URL, starting one foreground child if needed."""
    try:
        _store.inspect_run(root, run_id)
    except Exception as exc:  # noqa: BLE001 - convert store details at boundary
        raise BrowserOpenError("deliberation run is not available") from exc
    existing = _ui.read_surface_record(root, run_id)
    if existing is not None and _surface_reachable(existing):
        return {"url": existing["url"], "run_id": run_id, "reused": True,
                "pid": existing["pid"]}
    lock = _lock_path(root, run_id)
    claimed = _claim_open_lock(lock)
    if claimed is None:
        existing = _wait_for_surface(root, run_id)
        return {"url": existing["url"], "run_id": run_id, "reused": True,
                "pid": existing["pid"]}
    process = None
    try:
        existing = _ui.read_surface_record(root, run_id)
        if existing is not None and _surface_reachable(existing):
            return {"url": existing["url"], "run_id": run_id, "reused": True,
                    "pid": existing["pid"]}
        if existing is not None and _ui._pid_is_alive(existing.get("pid")):
            raise BrowserOpenError(
                "deliberation surface is live but its endpoint is unavailable")
        _discard_stale_record(root, run_id, existing)
        script = str(Path(__file__).with_name("_deliberation_ui.py"))
        try:
            process = subprocess.Popen(
                [sys.executable, script, "--serve", root, run_id], shell=False,
                cwd=str(Path(__file__).parent), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **popen_flags())
        except OSError as exc:
            raise BrowserOpenError("could not start the local deliberation surface") from exc
        return _wait_for_surface(root, run_id, process)
    except BrowserOpenError:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        raise
    finally:
        try:
            _release_open_lock(lock, claimed)
        except OSError:
            pass


__all__ = ["BrowserOpenError", "ensure_surface", "open_url"]
