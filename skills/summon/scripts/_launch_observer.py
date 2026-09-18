"""Trusted, provider-free launch observations for governed subprocess routes.

The ordinary launch evidence path intentionally records only the executable
content identity.  Governed continuation lanes need one additional fact: a
vendor-reported CLI version measured at the same boundary as the executable
and its entry materials.  This module owns that measurement for the first
qualified route, Claude over the subprocess transport.

This is not an authentication or model probe.  The version command is run
with provider credentials removed and must complete without contacting a
provider.  Ambiguous wrappers and unrecognised output fail closed; callers
must never turn a caller-supplied version string into an observation.
"""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping

from _launch_binding import measure_executable, measure_launch_material
from _spawn import popen_flags, run_flags


CLAUDE_BACKEND = "claude"
SUBPROCESS_TRANSPORT = "subprocess"
ENTRYPOINT_MARKER = "SUMMON_CLAUDE_ENTRYPOINT_V1"
MAX_VERSION_OUTPUT = 4096
VERSION_TIMEOUT_SECONDS = 3.0

_INTERPRETERS = frozenset({
    "cmd", "cmd.exe", "node", "node.exe", "python", "python.exe",
    "py", "py.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})
_WRAPPERS = frozenset({".cmd", ".bat", ".ps1", ".psm1", ".sh", ".py"})
_MARKER_LINE = re.compile(
    r"^\s*(?:rem|#|//)\s+SUMMON_CLAUDE_ENTRYPOINT_V1\s*$",
    re.IGNORECASE,
)
_VERSION_TOKEN = r"\d+\.\d+\.\d+(?:[-+._][A-Za-z0-9.-]+)?"
_VERSION_LINE = re.compile(
    r"^(?:claude(?:\s+code)?\s+v?"
    rf"(?P<leading>{_VERSION_TOKEN})|"
    rf"v?(?P<trailing>{_VERSION_TOKEN})\s+"
    r"\(Claude\s+Code\))$",
    re.IGNORECASE,
)


class LaunchObservationError(ValueError):
    """A typed, provider-free failure to produce a trusted observation."""

    def __init__(self, kind: str, message: str = "launch observation unavailable"):
        super().__init__(message)
        self.kind = kind


def _resolved_file(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = os.path.realpath(os.path.abspath(value))
    try:
        if not os.path.isfile(candidate):
            return None
    except OSError:
        return None
    return candidate


def _command_path(command: object) -> str | None:
    if not isinstance(command, str) or not command:
        return None
    candidate = command if os.path.isabs(command) else shutil.which(command)
    return _resolved_file(candidate)


def _script_marker(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(16 * 1024)
    except OSError:
        return False
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive for exotic codecs
        return False
    # The marker is a format discriminator, not a free-form substring.  A small
    # conventional batch prologue is allowed before it; a random comment later
    # in an opaque wrapper cannot qualify that wrapper as an entry script.
    prologue = {"@echo off", "echo off", "setlocal", "setlocal enableextensions",
                "setlocal enabledelayedexpansion"}
    for line in text.splitlines():
        normalized = line.strip().lower()
        if not normalized:
            continue
        if normalized in prologue:
            continue
        return bool(_MARKER_LINE.fullmatch(line))
    return False


def _entry_script(command_path: str, args: list[object]) -> str | None:
    """Resolve an explicitly marked interpreter/entry-script layout.

    The marker is deliberately required.  A generic ``cmd /c`` or ``node``
    wrapper is not enough to establish what the governed Claude route is.
    """
    basename = os.path.basename(command_path).lower()
    if basename not in _INTERPRETERS:
        return None
    text_args = [item for item in args if isinstance(item, str)]
    if basename in {"cmd", "cmd.exe"}:
        indexes = [index for index, item in enumerate(text_args)
                   if item.lower() == "/c"]
        candidates = [text_args[index + 1] for index in indexes
                      if index + 1 < len(text_args)]
    elif basename in {"node", "node.exe", "python", "python.exe", "py", "py.exe"}:
        candidates = [text_args[0]] if text_args else []
    else:
        candidates = []
    for candidate in candidates:
        path = _resolved_file(candidate)
        if path and os.path.splitext(path)[1].lower() in _WRAPPERS and _script_marker(path):
            return path
        if path and os.path.splitext(path)[1].lower() in {".js", ".cjs", ".mjs"} \
                and _script_marker(path):
            return path
    return None


def resolve_claude_layout(command: object, args: list[object]) -> dict[str, object]:
    """Resolve one allowlisted Claude launch layout without invoking it."""
    command_path = _command_path(command)
    if command_path is None:
        raise LaunchObservationError("launch_executable_unavailable")
    basename = os.path.basename(command_path).lower()
    suffix = os.path.splitext(command_path)[1].lower()
    if basename in {"cmd", "cmd.exe", "node", "node.exe", "python", "python.exe",
                    "py", "py.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        script = _entry_script(command_path, args)
        if script is None:
            raise LaunchObservationError("launch_resolver_unsupported")
        return {
            "layout": "marked_entry_script",
            "command": command_path,
            "version_argv": [command_path, *(
                ["/d", "/s", "/c", script] if basename in {"cmd", "cmd.exe"}
                else [script]), "--version"],
            "material_paths": [script],
        }
    if suffix in _WRAPPERS:
        raise LaunchObservationError("launch_resolver_unsupported")
    if suffix != ".exe" and os.name == "nt":
        raise LaunchObservationError("launch_resolver_unsupported")
    return {
        "layout": "native",
        "command": command_path,
        "version_argv": [command_path, "--version"],
        "material_paths": [],
    }


def _version_environment(proc_env: Mapping[str, object] | None,
                         isolated_home: str) -> dict[str, str]:
    source = proc_env if proc_env is not None else os.environ
    env = {str(key): str(value) for key, value in source.items()}
    # A version query must not inherit first-party, cloud, or custom endpoint
    # credentials.  Keep PATH/SystemRoot and other launch plumbing intact, but
    # give the process an empty home/config root so it cannot discover a vendor
    # profile by ambient path.
    sensitive_prefixes = (
        "ANTHROPIC_", "AWS_", "AZURE_", "GOOGLE_", "OPENAI_", "OPENROUTER_",
        "KIMI_", "MOONSHOT_", "BYTEPLUS_", "ARK_", "AGY_", "CLAUDE_",
    )
    sensitive_exact = {
        "GOOGLE_APPLICATION_CREDENTIALS", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE", "AZURE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_CACHE_HOME",
    }
    for key in tuple(env):
        upper = key.upper()
        if upper in sensitive_exact or upper.startswith(sensitive_prefixes):
            env.pop(key, None)
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    env["APPDATA"] = isolated_home
    env["LOCALAPPDATA"] = isolated_home
    env["XDG_CONFIG_HOME"] = isolated_home
    env["XDG_DATA_HOME"] = isolated_home
    env["XDG_CACHE_HOME"] = isolated_home
    env["SUMMON_VERSION_PROBE"] = "1"
    return env


def _parse_version(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text.encode("utf-8", errors="replace")) > MAX_VERSION_OUTPUT:
        raise LaunchObservationError("launch_version_output_invalid")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) != 1:
        raise LaunchObservationError("launch_version_output_invalid")
    match = _VERSION_LINE.fullmatch(lines[0])
    if match is None:
        raise LaunchObservationError("launch_version_output_untrusted")
    # Normalize the two supported vendor formats to a stable, bounded value.
    version = match.group("leading") or match.group("trailing")
    return f"claude/{version}"


def _terminate_owned(process: subprocess.Popen) -> None:
    """Terminate only the process tree owned by this version probe."""
    # Prefer the shared Windows Job Object when available.  Unlike taskkill's
    # parent/child walk, it still reaches descendants after the version-probe
    # leader has already exited while a grandchild keeps stdout open.
    try:
        from _jobobj import terminate as _job_terminate
        if _job_terminate(process):
            return
    except Exception:  # noqa: BLE001 - taskkill/killpg remains the fallback
        pass
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=2.0, **run_flags())
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _bounded_output(process: subprocess.Popen) -> tuple[bytes, bool]:
    output = bytearray()
    overflow = False
    stream = process.stdout
    if stream is None:
        return b"", False
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        if len(output) + len(chunk) > MAX_VERSION_OUTPUT:
            overflow = True
            break
        output.extend(chunk)
    return bytes(output), overflow


def _run_version(layout: Mapping[str, object], cwd: str,
                 proc_env: Mapping[str, object] | None) -> str:
    argv = [str(item) for item in layout["version_argv"]]
    isolated_home = tempfile.mkdtemp(prefix="summon-version-probe-")
    process = None
    reader = None
    output = b""
    overflow = False
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=_version_environment(proc_env, isolated_home),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, **popen_flags())
        try:
            from _jobobj import attach as _job_attach
            _job_attach(process)
        except Exception:  # noqa: BLE001 - teardown fallback remains valid
            pass
        result_box: list[tuple[bytes, bool]] = []
        reader = threading.Thread(
            target=lambda: result_box.append(_bounded_output(process)), daemon=True)
        reader.start()
        reader.join(VERSION_TIMEOUT_SECONDS)
        if reader.is_alive():
            _terminate_owned(process)
            reader.join(2.0)
            raise LaunchObservationError("launch_version_timeout")
        output, overflow = result_box[0] if result_box else (b"", False)
        if overflow:
            _terminate_owned(process)
            raise LaunchObservationError("launch_version_output_oversized")
        try:
            return_code = process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            _terminate_owned(process)
            raise LaunchObservationError("launch_version_timeout") from exc
        if return_code != 0:
            raise LaunchObservationError("launch_version_probe_failed")
        return _parse_version(output.decode("utf-8", errors="replace"))
    except LaunchObservationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LaunchObservationError("launch_version_probe_failed") from exc
    finally:
        if process is not None and process.poll() is None:
            _terminate_owned(process)
        elif process is not None:
            # The leader can exit before a descendant closes the pipe.  Closing
            # the job handle still tears down that descendant when attached.
            try:
                from _jobobj import close as _job_close
                _job_close(process)
            except Exception:  # noqa: BLE001 - no cleanup error may mask result
                pass
        shutil.rmtree(isolated_home, ignore_errors=True)


def observe_claude_launch(command: object, args: list[object], cwd: str,
                          proc_env: Mapping[str, object] | None) -> dict[str, object]:
    """Measure one governed Claude route immediately before a launch.

    The executable/material identity is measured both before and after the
    local ``--version`` invocation.  A replacement while the version command
    runs is therefore refused rather than being attached to the wrong version.
    """
    layout = resolve_claude_layout(command, args)
    before_executable = measure_executable(layout["command"])
    before_material = measure_launch_material(
        layout["command"], args, list(layout["material_paths"]))
    if before_executable is None or before_material is None:
        raise LaunchObservationError("launch_material_unavailable")
    version = _run_version(layout, cwd, proc_env)
    after_executable = measure_executable(layout["command"])
    after_material = measure_launch_material(
        layout["command"], args, list(layout["material_paths"]))
    if (after_executable is None or after_material is None
            or after_executable.get("executable_sha256")
            != before_executable.get("executable_sha256")
            or after_material != before_material):
        raise LaunchObservationError("launch_material_changed")
    return {
        "external_cli_version": version,
        "material_paths": list(layout["material_paths"]),
        "layout": layout["layout"],
        "executable": after_executable,
        "launch_material_sha256": after_material,
    }


def claude_launch_identity(command: object, args: list[object]) -> dict[str, object]:
    """Re-measure only executable/material identity immediately before Popen."""
    layout = resolve_claude_layout(command, args)
    executable = measure_executable(layout["command"])
    material = measure_launch_material(
        layout["command"], args, list(layout["material_paths"]))
    if executable is None or material is None:
        raise LaunchObservationError("launch_material_unavailable")
    return {
        "executable_sha256": executable.get("executable_sha256"),
        "launch_material_sha256": material,
        "material_paths": list(layout["material_paths"]),
        "layout": layout["layout"],
    }


__all__ = ["CLAUDE_BACKEND", "SUBPROCESS_TRANSPORT", "ENTRYPOINT_MARKER",
           "LaunchObservationError", "resolve_claude_layout",
           "observe_claude_launch", "claude_launch_identity"]
