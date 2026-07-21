"""Install-drift detection: enumerate every summon install on this machine, hash each
with the SAME primitive the dispatch receipt uses (``_receipt.scripts_sha256``), and flag
divergence. Powers ``doctor``'s installs section and install.py's post-install convergence
check.

Motivated by the field incident where a host ran an ancient ``run_subagent.py`` (no
``summon`` receipt at all) while the other copies were current -- silent drift that took
a manual hash hunt to diagnose. With this, any envelope's ``summon.scripts_sha256`` can be
matched against every install on the box, and ``doctor`` says which copy is stale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from _receipt import scripts_sha256

# Canonical host roots (dir name under HOME) that summon installs into. Public so
# install.py's convergence check and the tests share ONE list. MUST match install.py's
# HOSTS keys -- test_installs_hosts_match_installer guards against drift between the
# installer and this detector.
HOST_DIRS = {"claude": ".claude", "codex": ".codex", "cursor": ".cursor",
             "gemini": ".gemini", "copilot": ".copilot"}
_MANIFEST = ".summon-install.json"


def _resolved(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return os.path.abspath(path)


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
    """A copy's dispatcher version, read straight from its ``run_subagent.py``
    ``__version__ = "x.y.z"`` line -- NOT imported (a stale copy might not even import
    under the current Python) and NOT from the manifest (which does not record it). None
    if the line is absent. Scans only the file head; never raises."""
    try:
        with open(os.path.join(scripts_dir, "run_subagent.py"), encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 300:
                    break
                s = line.strip()
                if s.startswith("__version__") and "=" in s:
                    rhs = s.split("=", 1)[1]
                    for ch in ("\"", "'"):
                        if ch in rhs:
                            return rhs.split(ch)[1]
                    return None
    except OSError:
        pass
    return None


def _probe(label: str, scripts_dir: str) -> dict:
    """One install record for a ``.../skills/summon/scripts`` directory. Absent copies
    are reported (present=False) rather than dropped, so ``doctor`` can show what is NOT
    installed. Hashing/manifest reads never raise."""
    present = os.path.isdir(scripts_dir)
    rec = {"label": label, "scripts_dir": scripts_dir, "present": present,
           "running": False, "sha256": None, "version": None, "installed_at": None}
    if present:
        try:
            rec["sha256"] = scripts_sha256(scripts_dir)
        except OSError:
            rec["sha256"] = None
        rec["version"] = _read_version(scripts_dir)
        rec["installed_at"] = _read_installed_at(os.path.dirname(scripts_dir))
    return rec


def enumerate_installs(running_scripts_dir: str | None = None,
                       home: str | None = None) -> list:
    """Every summon install we can locate: the five host copies, the ``~/.agents``
    third-party-clone location from the incident, and the RUNNING copy. If the running
    copy resolves to one of the enumerated locations it is TAGGED there (``running:
    True``), not listed twice; if it lives elsewhere (a repo/worktree checkout) it is
    appended as its own record. ``home`` is injectable for tests."""
    home = home or os.path.expanduser("~")
    records = [_probe(name, os.path.join(home, d, "skills", "summon", "scripts"))
               for name, d in HOST_DIRS.items()]
    records.append(_probe("agents",
                          os.path.join(home, ".agents", "skills", "summon", "scripts")))
    if running_scripts_dir:
        run_key = _resolved(running_scripts_dir)
        for r in records:
            if r["present"] and _resolved(r["scripts_dir"]) == run_key:
                r["running"] = True
                break
        else:
            rec = _probe("running", running_scripts_dir)
            rec["running"] = True
            records.append(rec)
    return records


def drift_report(records: list, reference_sha: str | None = None) -> dict:
    """Classify drift across enumerated records. The reference is the RUNNING copy's hash
    (the code that actually answered) unless one is passed explicitly. Present installs
    whose hash differs from the reference are DRIFTED. With no reference (e.g. the running
    copy could not be hashed) nothing is called drifted -- we never cry drift we can't
    anchor. Returns {reference_sha, converged, present, drifted}."""
    if reference_sha is None:
        run = next((r for r in records if r.get("running") and r.get("sha256")), None)
        reference_sha = run["sha256"] if run else None
    present = [r for r in records if r["present"] and r["sha256"]]
    drifted = [r for r in present if reference_sha and r["sha256"] != reference_sha]
    return {"reference_sha": reference_sha,
            "converged": bool(reference_sha) and not drifted,
            "present": present, "drifted": drifted}
