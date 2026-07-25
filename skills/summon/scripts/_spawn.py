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


def popen_flags(*, detached: bool = False) -> dict:
    """Platform ``Popen`` kwargs for a spawned child.

    ``detached=False`` (the default, for every worker summon waits on): no console
    on Windows, own session on POSIX.

    ``detached=True`` (only the ``--background`` launcher): the child outlives this
    process, so it needs its own process GROUP as well. ``DETACHED_PROCESS`` already
    means "no console at all", and Windows documents ``CREATE_NO_WINDOW`` as IGNORED
    when combined with it -- so it is deliberately not added there rather than
    stacked on for symmetry.
    """
    if os.name != "nt":
        return {"start_new_session": True}
    if detached:
        return {"creationflags": (subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP)}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}
