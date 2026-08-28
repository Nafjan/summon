"""Platform flags for spawning a child process. ONE definition, every call site.

Summon spawns children from several places (the executor's backend dispatch, the
manifest/council child runner, the --background launcher). Each one previously
carried its own copy of the platform branch, and the copies drifted: on Windows a
console application spawned without ``CREATE_NO_WINDOW`` ALLOCATES A CONSOLE, so
an empty ``node.exe`` window appeared for every Codex dispatch.

Patching one site did not fix it, because the paths nest. A manifest or council run
spawns ``python.exe`` (the child dispatcher), which spawns ``node.exe`` (the vendor
CLI). If the outer python spawn lacks the flag it allocates the console, and the
inner Node process inherits it -- so the window appears even when the executor is
patched. Every link in the chain has to carry the flag, which is why it lives here
once instead of being re-derived per site.

POSIX has no console concept to suppress; there ``start_new_session`` is what
matters, so ``_kill_tree`` can signal the whole process group (a shim's grandchild
otherwise survives ``process.kill()`` and holds stdout open, defeating the timeout).
"""
from __future__ import annotations

import os
import subprocess


def _hidden_startupinfo():
    """Return a Windows startup descriptor that hides any GUI-capable child.

    ``CREATE_NO_WINDOW`` handles console applications, but it does not prevent a
    child that elects to create a GUI window (or launches one through a shell)
    from flashing a window. Summon is always a headless broker, so every child
    gets an explicit hidden startup state as well. Keep this in the shared
    helper: a single missed launch site would reintroduce the intermittent
    popup users see during nested AGY/shell work.
    """
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def popen_flags(*, detached: bool = False,
                join_parent_group: bool = False) -> dict:
    """Platform ``Popen`` kwargs for a spawned child.

    ``detached=False`` (the default, for every worker summon waits on): no console
    on Windows, own session on POSIX.

    ``join_parent_group=True`` is for a supervised POSIX shim child that must stay
    in the caller's process group so the outer timeout can terminate the complete
    tree. It has no effect on Windows, where job ownership is handled separately.

    ``detached=True`` (only the ``--background`` launcher): the child outlives this
    process, so it needs its own process GROUP as well. ``DETACHED_PROCESS`` already
    means "no console at all", and Windows documents ``CREATE_NO_WINDOW`` as IGNORED
    when combined with it -- so it is deliberately not added there rather than
    stacked on for symmetry.
    """
    if os.name != "nt":
        if join_parent_group:
            return {}
        return {"start_new_session": True}
    hidden = {"startupinfo": _hidden_startupinfo()}
    if detached:
        hidden["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
        return hidden
    hidden["creationflags"] = subprocess.CREATE_NO_WINDOW
    return hidden


_INTERNAL_PROVIDER_ENV_NAMES = frozenset({
    "SUMMON_ADAPTIVE_TIMEOUT",
    "SUMMON_MAX_RUNTIME_MS",
    "SUMMON_RESUME_CLAIM_FILE",
    "SUMMON_FRESH_CONSENT_ONLY",
    "SUMMON_CMD_LAUNCHER",
    "SUMMON_FLEET_APPROVAL_KEY",
    "SUMMON_FLEET_APPROVAL_STORE",
})


def scrub_provider_env(values: dict[str, str]) -> dict[str, str]:
    """Remove dispatcher control capabilities from a provider child.

    A detached background dispatcher needs these values itself, but the vendor
    CLI it launches must not inherit the outer job's authenticated control,
    heartbeat, provenance, or continuation channel. Ordinary provider settings
    remain byte-for-byte unchanged.
    """
    return {
        key: value for key, value in values.items()
        if key not in _INTERNAL_PROVIDER_ENV_NAMES
        and not key.startswith("SUMMON_JOB_")
    }


def run_flags() -> dict:
    """Flags for a short-lived UTILITY subprocess.run: git, taskkill, icacls, a version
    probe. These are console applications too, so on Windows they flash (or, when summon
    itself has no console, ALLOCATE) a window without CREATE_NO_WINDOW.

    POSIX gets nothing: unlike a spawned backend these are awaited inline and never need
    their own session, so start_new_session would be noise rather than protection.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW,
            "startupinfo": _hidden_startupinfo()}
