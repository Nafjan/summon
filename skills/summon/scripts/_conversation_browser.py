"""Browser handoff for the local conversation atlas and bounded turn controls."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from _conversation_ui import (MAX_SURFACE_RECORD_BYTES, SURFACE_RECORD, TOKEN_RE,
                              _pid_is_alive, _surface_binding_digest,
                              read_surface_record)
from _conversation import _root_path
from _spawn import popen_flags


class ConversationBrowserError(Exception):
    """A local conversation surface could not be safely opened."""


class _NoRedirect(HTTPRedirectHandler):
    """Keep bearer-bearing loopback probes on the exact endpoint.

    ``urllib.request.urlopen`` follows redirects by default and reuses the
    request headers.  A compromised local listener could therefore redirect a
    readiness probe to another loopback port and receive the surface token.
    Surface reachability is a proof of the exact authenticated endpoint, not a
    navigation operation, so any redirect is a failed probe.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect())
_LAUNCH_LOCK = ".summon-conversation-ui.launch.lock"
_LAUNCH_LOCK_STALE_SECONDS = 30.0


@contextmanager
def _launch_guard(root: str, timeout: float):
    """Serialize cross-process surface starts without trusting a stale marker."""
    path = Path(root) / _LAUNCH_LOCK
    deadline = time.monotonic() + max(0.5, timeout)
    token = secrets.token_hex(16)
    fd = None
    try:
        while fd is None:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                payload = json.dumps({"pid": os.getpid(), "token": token,
                                      "started_at": time.time()},
                                     sort_keys=True, separators=(",", ":")).encode("ascii")
                os.write(fd, payload)
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            except FileExistsError:
                try:
                    stat = path.stat()
                    marker = json.loads(path.read_text(encoding="ascii"))
                    owner = marker.get("pid") if isinstance(marker, dict) else None
                    if (time.time() - stat.st_mtime > _LAUNCH_LOCK_STALE_SECONDS
                            and not _pid_is_alive(owner)
                            and path.stat().st_mtime_ns == stat.st_mtime_ns):
                        path.unlink()
                        continue
                except (OSError, ValueError, TypeError, UnicodeError):
                    pass
                if time.monotonic() >= deadline:
                    raise ConversationBrowserError("conversation surface start is busy")
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            finally:
                try:
                    marker = json.loads(path.read_text(encoding="ascii"))
                    if (isinstance(marker, dict) and marker.get("pid") == os.getpid()
                            and marker.get("token") == token):
                        path.unlink()
                except (OSError, ValueError, TypeError, UnicodeError):
                    pass


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


def ensure_surface(root: str, *, timeout: float = 5.0,
                   cwd: str | None = None, agents_dir: str | None = None,
                   timeout_ms: int = 600_000) -> dict[str, object]:
    try:
        # Fence every parent component before the launch lock is created.  A
        # symlinked child check alone still lets ``alias/rooms`` redirect the
        # lock write through a symlinked parent.
        canonical_root = _root_path(root, create=False)
    except Exception as exc:  # noqa: BLE001 - public browser boundary
        raise ConversationBrowserError("conversation root may not contain symlinks") from exc
    root = str(canonical_root)
    # The child is launched with the scripts directory as its working
    # directory for deterministic imports.  Canonicalize caller bindings now
    # so relative `--cwd`/`--agents-dir` values resolve identically in the
    # parent, the sidecar digest, and the child runtime.
    try:
        binding_cwd = (str(Path(cwd).expanduser().resolve()) if cwd is not None else None)
        binding_agents_dir = (
            str(Path(agents_dir).expanduser().resolve()) if agents_dir is not None else None
        )
    except (OSError, RuntimeError) as exc:
        raise ConversationBrowserError("surface binding path could not be resolved") from exc
    with _launch_guard(root, timeout):
        # Reuse is deliberately inside the same cross-process guard as start.
        # A fast-path probe outside the guard can race a concurrent close/start
        # and return a sidecar that has already been replaced.
        existing = read_surface_record(root)
        if existing is not None and _wait_surface_reachable(existing, timeout=timeout):
            _require_surface_binding(existing, cwd=binding_cwd, agents_dir=binding_agents_dir)
            return {"url": existing["url"], "reused": True, "pid": existing["pid"]}
        if existing is not None:
            # A shaped record with a live PID but an unreachable endpoint is
            # not safe to overwrite: the old server may be transiently busy,
            # and replacing it would create two untracked atlases.  Require
            # explicit process shutdown/recovery instead of guessing.
            if _pid_is_alive(existing.get("pid")):
                raise ConversationBrowserError(
                    "conversation surface is live but its endpoint is unavailable")
            _discard_stale_record(root, existing)
        script = str(Path(__file__).with_name("_conversation_ui.py"))
        # We hold _launch_guard across child startup.  The child must not
        # reacquire that same file lock or it would deadlock before publishing
        # its authenticated sidecar.
        command = [sys.executable, script, "--serve", root, "--timeout", str(int(timeout_ms)),
                   "--launch-lock-held"]
        if binding_cwd:
            command.extend(["--cwd", binding_cwd])
        if binding_agents_dir:
            command.extend(["--agents-dir", binding_agents_dir])
        try:
            # The atlas is a handoff from a short-lived CLI/IDE command to a
            # local observer.  Detach the server itself from that caller so a
            # terminal/agent session ending does not take the browser down.
            # Its authenticated sidecar remains the lifetime record, and an
            # explicit surface close still shuts it down.
            process = subprocess.Popen(command, shell=False,
                                       cwd=str(Path(__file__).parent), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, **popen_flags(detached=True))
        except OSError as exc:
            raise ConversationBrowserError("conversation surface could not be started") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = read_surface_record(root)
            if (record is not None and record.get("pid") == process.pid
                    and _surface_reachable(record)):
                try:
                    _require_surface_binding(record, cwd=binding_cwd,
                                             agents_dir=binding_agents_dir)
                except ConversationBrowserError:
                    # A child that published a record for a different binding
                    # is not a successful handoff.  Tear it down before
                    # returning the explicit mismatch rather than leaving a
                    # live, untracked browser process behind.
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


def _require_surface_binding(record: dict[str, object], *,
                             cwd: str | None, agents_dir: str | None) -> None:
    """Refuse silent atlas reuse across project or explicit roster bindings.

    Pre-binding sidecars remain readable for callers that do not provide a
    binding.  Once a caller supplies ``cwd`` or ``agents_dir``, however, an
    old/missing digest is an explicit mismatch rather than permission to
    launch a second server against the same atlas root.
    """
    if cwd is not None:
        expected = _surface_binding_digest(cwd)
        if record.get("cwd_sha256") != expected:
            raise ConversationBrowserError(
                "conversation surface is bound to a different project")
    if agents_dir is not None:
        expected = _surface_binding_digest(agents_dir)
        if record.get("agents_dir_sha256") != expected:
            raise ConversationBrowserError(
                "conversation surface is bound to a different agent roster")


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
        with _NO_REDIRECT_OPENER.open(request, timeout=0.35) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - stale/fake records fail closed
        return False


def _wait_surface_reachable(record: dict[str, object], *, timeout: float) -> bool:
    """Wait briefly for a freshly published sidecar's server thread to bind.

    ``ConversationSurface.start`` writes its record immediately after starting
    ``serve_forever``. A same-process caller can therefore observe a live PID
    during the small scheduling window before the first authenticated request
    succeeds. Wait only while that PID remains live; a genuinely dead or
    unreachable record still fails closed and is never silently replaced.
    """
    try:
        budget = max(0.05, min(float(timeout), 5.0))
    except (TypeError, ValueError, OverflowError):
        budget = 0.5
    deadline = time.monotonic() + budget
    while True:
        if _surface_reachable(record):
            return True
        if not _pid_is_alive(record.get("pid")) or time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


__all__ = ["ConversationBrowserError", "ensure_surface", "open_url"]
