"""Browser handoff for the provider-inert conversation atlas."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from _conversation_ui import MAX_SURFACE_RECORD_BYTES, SURFACE_RECORD, TOKEN_RE, read_surface_record
from _spawn import popen_flags


class ConversationBrowserError(Exception):
    """A local conversation surface could not be safely opened."""


def _validate_url(url: str) -> dict[str, object]:
    if not isinstance(url, str) or len(url) > 512:
        raise ConversationBrowserError("surface URL is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.path != "/"):
        raise ConversationBrowserError("surface URL is not a conversation loopback URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConversationBrowserError("surface URL has an invalid port") from exc
    if not port or not (1 <= port <= 65535) or not parsed.fragment.startswith("token="):
        raise ConversationBrowserError("surface URL has no valid bearer token")
    token = parsed.fragment[6:]
    if not TOKEN_RE.fullmatch(token):
        raise ConversationBrowserError("surface URL has an invalid bearer token")
    return {"token": token, "port": port}


def _bridge_executable() -> str | None:
    for name in ("SUMMON_BROWSER_BRIDGE", "CODEX_BROWSER_BRIDGE",
                 "VSCODE_BROWSER_BRIDGE", "CURSOR_BROWSER_BRIDGE",
                 "ANTIGRAVITY_BROWSER_BRIDGE"):
        value = os.environ.get(name)
        if not value:
            continue
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ConversationBrowserError("browser bridge contains control characters")
        executable = value if os.path.isfile(value) else shutil.which(value)
        if not executable:
            raise ConversationBrowserError("browser bridge is unavailable")
        return os.path.abspath(executable)
    return None


def _builtin_executable() -> str | None:
    backends = {item.strip().lower() for item in
                os.environ.get("BROWSER_USE_AVAILABLE_BACKENDS", "").split(",")}
    return shutil.which("browser-harness") if "iab" in backends else None


def open_url(url: str, *, mode: str = "auto") -> dict[str, object]:
    _validate_url(url)
    if mode not in {"auto", "builtin", "ide", "system", "link"}:
        raise ConversationBrowserError("browser mode is invalid")
    if mode == "link":
        return {"url": url, "target": "link", "opened": False, "reused": False}
    bridge = _bridge_executable() if mode in {"auto", "ide"} else None
    if mode == "ide" and not bridge:
        raise ConversationBrowserError("no IDE browser bridge is configured")
    if bridge:
        try:
            subprocess.Popen([bridge, url], shell=False, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **popen_flags())
        except OSError as exc:
            raise ConversationBrowserError("IDE browser bridge could not be started") from exc
        return {"url": url, "target": "ide", "opened": True, "reused": True}
    builtin = _builtin_executable() if mode in {"auto", "builtin"} else None
    if mode == "builtin" and not builtin:
        raise ConversationBrowserError("built-in IDE browser is unavailable")
    if builtin:
        target = json.dumps(url, ensure_ascii=True)
        script = f"target = {target}\nnew_tab(target)\nprint(page_info())\n"
        try:
            process = subprocess.Popen([builtin], shell=False, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       **popen_flags())
            process.communicate(script.encode("utf-8"), timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            try: process.kill()
            except Exception: pass
            raise ConversationBrowserError("built-in IDE browser could not be started") from exc
        if process.returncode not in (0, None):
            raise ConversationBrowserError("built-in IDE browser refused the link")
        return {"url": url, "target": "builtin", "opened": True, "reused": True}
    try:
        opened = bool(webbrowser.open(url, new=0, autoraise=True))
    except Exception as exc:
        raise ConversationBrowserError("system browser could not be started") from exc
    if not opened:
        raise ConversationBrowserError("system browser did not accept the link")
    return {"url": url, "target": "system", "opened": True, "reused": True}


def ensure_surface(root: str, *, timeout: float = 5.0) -> dict[str, object]:
    existing = read_surface_record(root)
    if existing is not None and _surface_reachable(existing):
        return {"url": existing["url"], "reused": True, "pid": existing["pid"]}
    if existing is not None:
        _discard_stale_record(root, existing)
    script = str(Path(__file__).with_name("_conversation_ui.py"))
    try:
        process = subprocess.Popen([sys.executable, script, "--serve", root], shell=False,
                                   cwd=str(Path(__file__).parent), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **popen_flags())
    except OSError as exc:
        raise ConversationBrowserError("conversation surface could not be started") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = read_surface_record(root)
        if (record is not None and record.get("pid") == process.pid
                and _surface_reachable(record)):
            # The server is intentionally detached from this short-lived CLI
            # process. Mark the readiness handoff complete so CPython does not
            # emit a false ResourceWarning when the Popen wrapper is collected;
            # the child owns its loopback surface and is tracked by its record.
            process.returncode = 0
            return {"url": record["url"], "reused": False, "pid": record["pid"]}
        if process.poll() is not None:
            break
        time.sleep(0.05)
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise ConversationBrowserError("conversation surface did not become ready")


def _discard_stale_record(root: str, expected: dict[str, object]) -> None:
    """Remove only the exact shaped record that failed its local endpoint probe."""
    try:
        path = Path(root) / SURFACE_RECORD
        if path.is_symlink() or path.stat().st_size > MAX_SURFACE_RECORD_BYTES:
            return
        value = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("pid") == expected.get("pid")
                and value.get("token") == expected.get("token")):
            path.unlink()
    except (OSError, ValueError):
        return


def _surface_reachable(record: dict[str, object]) -> bool:
    """Prove a shaped surface record points at the authenticated local server."""
    url, token = record.get("url"), record.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        return False
    try:
        parsed = _validate_url(url)
        endpoint = f"http://127.0.0.1:{parsed['port']}/api/v1/rooms"
        request = Request(endpoint, headers={
            "Host": f"127.0.0.1:{parsed['port']}",
            "Authorization": "Bearer " + token,
        })
        with urlopen(request, timeout=0.35) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - stale/fake records fail closed
        return False


__all__ = ["ConversationBrowserError", "ensure_surface", "open_url"]
