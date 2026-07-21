"""Provenance receipt: the identity summon stamps onto every dispatch envelope.

Split out of run_subagent.py (the entry point) so the hashing/identity logic
lives on its own. The entry script injects its OWN path and version into
``receipt_base`` (via the zero-arg ``_receipt_base`` wrapper there), so the
receipt's ``script``/``version`` always name run_subagent.py, never THIS module
(a sibling in the same scripts dir).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
from pathlib import Path

from _loader import bundled_roster_dir


def scripts_sha256(scripts_dir, max_bytes=None) -> str:
    """One SHA-256 over EVERY production module in ``scripts_dir`` (all ``*.py``
    except test_discovery.py, which never executes at dispatch time). Length-prefixed
    framing (``len(name)|name|len(data)|data``, names sorted) so (name, content)
    boundaries are unambiguous and drift in ANY sibling (incl. agy_pty_pyte.py), not
    just the entry file, is detectable.

    SINGLE source of truth for install identity: the dispatch receipt and the
    install-drift detector both hash the same way, so a hash difference is always a
    REAL code divergence, never an algorithm mismatch. A missing/unreadable module
    hashes as empty content (kept diagnosable, never raises).

    ``max_bytes`` bounds the work per file so hashing a FOREIGN copy (drift enumeration
    of OTHER installs) cannot exhaust memory: a non-regular file (a symlink to a device
    or FIFO) is hashed by a marker instead of read, and a file larger than ``max_bytes``
    is hashed by a name+size marker without reading its content. The dispatch receipt
    passes ``max_bytes=None`` (EXACT legacy behavior, hashing the operator's OWN trusted
    install); every real, small, regular module hashes identically under either, so drift
    detection still matches the receipt for any genuine copy."""
    here = Path(scripts_dir)
    h = hashlib.sha256()
    for name in sorted(p.name for p in here.glob("*.py") if p.name != "test_discovery.py"):
        target = here / name
        try:
            if max_bytes is not None and not target.is_file():
                data = b"\x00__nonfile__"          # symlink to a device/dir/FIFO: never read
            elif max_bytes is not None and target.stat().st_size > max_bytes:
                data = b"\x00__oversize__:" + str(target.stat().st_size).encode("ascii")
            else:
                data = target.read_bytes()
        except OSError:
            data = b""
        nb = name.encode("utf-8")
        h.update(len(nb).to_bytes(8, "big"))
        h.update(nb)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def receipt_base(entry_path: str, version: str) -> dict:
    """summon identity: available before ANY validation, so even a missing-agent
    or unknown-backend error names the install that produced it. ``entry_path``
    and ``version`` are injected by the entry script so ``script``/``version``
    name run_subagent.py; ``here`` is that file's directory (this module is a
    sibling, so the scripts dir is the same either way)."""
    here = Path(entry_path).resolve().parent
    return {"summon": {"version": version,
                       "script": str(Path(entry_path).resolve()),
                       "scripts_sha256": scripts_sha256(here)}}


def receipt_agent(args: argparse.Namespace, agent_file: str) -> dict:
    """Agent-definition provenance. ``agents_dir`` records the ABSOLUTE roster
    directory the definition was ACTUALLY loaded from (a bundled-fallback hit
    must not record the project dir that failed the lookup)."""
    try:
        fsha = hashlib.sha256(Path(agent_file).read_bytes()).hexdigest()
    except OSError:
        fsha = None  # hashed as read-back; a vanished file stays diagnosable
    served_dir = str(Path(agent_file).resolve().parent)
    _bundled = bundled_roster_dir()
    if _bundled and Path(served_dir) == Path(_bundled).resolve():
        source = "bundled"
    elif args.agents_dir:
        source = "explicit"
    elif os.environ.get("SUB_AGENTS_DIR"):
        source = "env"
    else:
        source = "project"
    return {"agent_def": {"file": agent_file, "sha256": fsha,
                          "agents_dir": served_dir, "source": source}}


def receipt_prompt(prompt: str | None) -> dict:
    if prompt is None:
        return {}
    return {"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


def git_head(cwd: str) -> str | None:
    """HEAD of the EFFECTIVE dispatch cwd, captured BEFORE the agent runs (input
    provenance -- hence `git_head_before` in the envelope; an editing agent may
    commit during the run). Best-effort: None outside a repo or without git.
    Uses the shared ``subprocess`` module object so a test patching it is seen."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=2,
                           stdin=subprocess.DEVNULL)
        head = (r.stdout or "").strip()
        return head if r.returncode == 0 and head else None
    except (OSError, subprocess.SubprocessError):
        return None
