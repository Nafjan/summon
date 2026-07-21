"""Install-drift detection: enumerate every summon install on this machine, hash each
with the SAME primitive the dispatch receipt uses (``_receipt.scripts_sha256``), and flag
divergence. Powers ``doctor``'s installs section and install.py's post-install convergence
check.

Motivated by the field incident where a host ran an ancient ``run_subagent.py`` (no
``summon`` receipt at all) while the other copies were current: silent drift that took a
manual hash hunt to diagnose. With this, any envelope's ``summon.scripts_sha256`` can be
matched against every install on the box, and ``doctor`` says which copy is stale.

Every read here is BOUNDED and fail-soft: a foreign or corrupt copy (bad encoding, an
oversize or non-regular ``*.py``, a permission error) is classified, never crashes the
doctor or the installer, and never exhausts memory.
"""

from __future__ import annotations

import ast
import json
import os
import stat as _stat
from pathlib import Path

from _receipt import scripts_sha256

# Canonical host roots (dir name under HOME) that summon installs into. Public so
# install.py's convergence check and the tests share ONE list. MUST match install.py's
# HOSTS keys -- test_installs_hosts_match_installer guards against drift between the
# installer and this detector.
HOST_DIRS = {"claude": ".claude", "codex": ".codex", "cursor": ".cursor",
             "gemini": ".gemini", "copilot": ".copilot"}
_MANIFEST = ".summon-install.json"
# Per-file ceiling for hashing/parsing an install we do NOT own (drift enumeration of
# other copies). Real production modules are well under 200 KB; this only bounds a
# foreign/compromised copy so it cannot exhaust memory during doctor or install.
_ENUM_MAX_BYTES = 4_000_000


def _canonical(path: str) -> str:
    """A comparison key that is stable across symlink aliases AND case-insensitive
    filesystems: resolve symlinks (realpath) then normcase (lowercases on Windows). So
    ``~/.claude`` and ``~/.codex`` symlinked to one physical copy collapse to one key,
    and a Windows path never mismatches itself on case alone. Never raises."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _read_installed_at(install_dir: str):
    """``installed_at`` epoch from a copy's ``.summon-install.json`` (one dir ABOVE
    scripts/); None if absent, unreadable, corrupt, or foreign. Never raises."""
    try:
        with open(os.path.join(install_dir, _MANIFEST), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("installed_by") == "summon":
            return data.get("installed_at")
    except (OSError, ValueError):
        pass
    return None


def _read_version(scripts_dir: str):
    """A copy's dispatcher version, parsed from its ``run_subagent.py`` module-level
    ``__version__ = "x.y.z"`` assignment via AST -- NOT a line scan (which would mistake a
    ``__version__`` inside a docstring, or a trailing ``# "comment"``, for the value), NOT
    imported (a stale copy might not import under the current Python), NOT from the manifest
    (which omits it). None if absent, computed (non-literal), or unparseable.

    TRULY never raises: a non-regular/oversize/non-UTF-8/syntactically-broken file, or even a
    pathological deeply-nested expression (RecursionError from ast), all yield None. The file
    is opened ONCE and fstat'd on the HANDLE, then read at most ``_ENUM_MAX_BYTES`` -- an
    oversize file is REJECTED (not parsed from a truncated prefix, which could return a wrong
    or stale value). Non-UTF-8 is strict (yields None), never silently replaced. When multiple
    module-level ``__version__`` assignments exist, the LAST wins (Python's own semantics)."""
    path = os.path.join(scripts_dir, "run_subagent.py")
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not _stat.S_ISREG(st.st_mode) or st.st_size > _ENUM_MAX_BYTES:
                return None                      # non-regular (device/FIFO) or oversize
            raw = fh.read(_ENUM_MAX_BYTES + 1)
        if len(raw) > _ENUM_MAX_BYTES:           # grew past the bound between fstat and read
            return None
        tree = ast.parse(raw.decode("utf-8"))    # strict: non-UTF-8 -> UnicodeDecodeError
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
        return None
    found = None
    for node in tree.body:   # MODULE level only -- ignore nested/conditional assigns
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__version__":
                    v = node.value
                    found = (v.value if isinstance(v, ast.Constant)
                             and isinstance(v.value, str) else None)
    return found   # last module-level assignment wins; a computed value -> None (not garbage)


def _probe(label: str, scripts_dir: str, managed: bool) -> dict:
    """One install record for a ``.../skills/summon/scripts`` directory. Absent copies are
    reported (present=False) rather than dropped, so ``doctor`` can show what is NOT
    installed. A present copy that cannot be hashed (e.g. permission denied on the dir)
    keeps ``sha256=None`` -- classified UNKNOWN downstream, never silently 'converged'.
    Hashing/manifest/version reads never raise."""
    present = os.path.isdir(scripts_dir)
    rec = {"label": label, "scripts_dir": scripts_dir, "present": present,
           "managed": managed, "running": False,
           "sha256": None, "version": None, "installed_at": None}
    if present:
        try:
            rec["sha256"] = scripts_sha256(scripts_dir, max_bytes=_ENUM_MAX_BYTES)
        except OSError:
            rec["sha256"] = None
        rec["version"] = _read_version(scripts_dir)
        rec["installed_at"] = _read_installed_at(os.path.dirname(scripts_dir))
    return rec


def enumerate_installs(running_scripts_dir: str | None = None,
                       home: str | None = None) -> list:
    """Every summon install we can locate: the five host copies (``managed``), the
    ``~/.agents`` third-party-clone location from the incident, and the RUNNING copy.

    Present copies that are the SAME physical directory (symlink aliases) are COLLAPSED
    into one record with merged labels (e.g. ``claude+codex``), so a shared copy is neither
    double-counted nor mis-tagged. The running copy is matched by canonical path: if it is
    one of the enumerated copies it is TAGGED there (``running: True``); if it lives
    elsewhere (a repo/worktree checkout) it is appended as its own record. ``home`` is
    injectable for tests."""
    home = home or os.path.expanduser("~")
    raw = [_probe(name, os.path.join(home, d, "skills", "summon", "scripts"), managed=True)
           for name, d in HOST_DIRS.items()]
    raw.append(_probe("agents",
                      os.path.join(home, ".agents", "skills", "summon", "scripts"),
                      managed=False))
    records: list = []
    by_key: dict = {}
    for r in raw:
        if not r["present"]:
            records.append(r)          # absent: no physical path to collapse on
            continue
        key = _canonical(r["scripts_dir"])
        if key in by_key:
            first = by_key[key]
            first["label"] = first["label"] + "+" + r["label"]
            first["managed"] = first["managed"] or r["managed"]
        else:
            by_key[key] = r
            records.append(r)
    if running_scripts_dir:
        run_key = _canonical(running_scripts_dir)
        for r in records:
            if r["present"] and _canonical(r["scripts_dir"]) == run_key:
                r["running"] = True
                break
        else:
            rec = _probe("running", running_scripts_dir, managed=False)
            rec["running"] = True
            records.append(rec)
    return records


def drift_report(records: list, reference_sha: str | None = None) -> dict:
    """Classify drift across enumerated records. The reference is the RUNNING copy's hash
    (the code that actually answered) unless one is passed explicitly.

    A PRESENT copy is HASHED (comparable), or UNKNOWN when its hash could not be computed
    (a permission error, a foreign non-regular ``*.py``). ``drifted`` = hashed copies whose
    hash differs from the reference. With no reference nothing is called drifted -- we never
    cry drift we cannot anchor. ``converged`` requires a reference, NO drift, AND no unknown
    copy (an uncheckable copy is never reported as 'all match'). Returns
    {reference_sha, converged, present, hashed, drifted, unknown}."""
    if reference_sha is None:
        run = next((r for r in records if r.get("running") and r.get("sha256")), None)
        reference_sha = run["sha256"] if run else None
    present = [r for r in records if r["present"]]
    hashed = [r for r in present if r["sha256"]]
    unknown = [r for r in present if not r["sha256"]]
    drifted = [r for r in hashed if reference_sha and r["sha256"] != reference_sha]
    return {"reference_sha": reference_sha,
            "converged": bool(reference_sha) and not drifted and not unknown,
            "present": present, "hashed": hashed, "drifted": drifted, "unknown": unknown}
