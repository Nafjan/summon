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

from _receipt import scripts_sha256, _read_regular_bounded

# Canonical host roots (dir name under HOME) that summon installs into. Public so
# install.py's convergence check and the tests share ONE list. MUST match install.py's
# HOSTS keys -- test_installs_hosts_match_installer guards against drift between the
# installer and this detector.
HOST_DIRS = {"claude": ".claude", "codex": ".codex", "cursor": ".cursor",
             "gemini": ".gemini", "kimi": ".kimi-code", "copilot": ".copilot",
             # Antigravity's skills live per-profile UNDER ~/.gemini, so these are nested
             # relative paths rather than a top-level dot-dir (joined the same way).
             "antigravity": os.path.join(".gemini", "antigravity"),
             "antigravity-cli": os.path.join(".gemini", "antigravity-cli"),
             "antigravity-ide": os.path.join(".gemini", "antigravity-ide")}
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


def _host_root(name: str, relative: str, home: str) -> str:
    """Resolve a host root, honoring Kimi's portable data-home override."""
    if name == "kimi":
        return os.environ.get("KIMI_CODE_HOME") or os.path.join(home, relative)
    return os.path.join(home, relative)


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
        raw, note = _read_regular_bounded(path, _ENUM_MAX_BYTES)   # non-blocking; FIFO-safe
        if note is not None or raw is None:      # non-regular (FIFO/device) or oversize
            return None
        if len(raw) > _ENUM_MAX_BYTES:           # grew past the bound on the handle
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
        elif isinstance(node, ast.AnnAssign):    # __version__: str = "x"
            if isinstance(node.target, ast.Name) and node.target.id == "__version__":
                v = node.value                   # may be None (a bare annotation)
                found = (v.value if v is not None and isinstance(v, ast.Constant)
                         and isinstance(v.value, str) else None)
        elif isinstance(node, ast.AugAssign):    # __version__ += "x": no longer a known literal
            if isinstance(node.target, ast.Name) and node.target.id == "__version__":
                found = None
    return found   # last module-level assignment wins; a computed/mutated value -> None


_SKILL_MD_MAX = 200_000   # a SKILL.md is small text; bound a foreign/corrupt one
_MAX_SKILLS_SCAN = 10_000  # cap entries scanned in a skills dir (bound a hostile/huge one)
# Agent Plugin installs (agent-plugins.org) use the same skills/summon/ tree as this repo.
# Cursor loads local dev copies from ~/.cursor/plugins/local/<name>/ and marketplace
# copies under ~/.cursor/plugins/cache/ — distinct from ~/.cursor/skills/summon that
# install.py targets.
_AGENT_PLUGIN_LOCAL_ROOT = os.path.join(".cursor", "plugins", "local", "summon")
_AGENT_PLUGIN_CACHE_ROOT = os.path.join(".cursor", "plugins", "cache")
_AGENT_PLUGIN_SCRIPTS_TAIL = os.path.join("skills", "summon", "scripts")
_MAX_PLUGIN_CACHE_DIRS = 500   # bound a hostile/huge cache tree
_MAX_PLUGIN_CACHE_DEPTH = 5    # cache/<id>/…/summon — shallow enough for known layouts


def _skill_md_name(skill_dir: str):
    """The frontmatter ``name:`` of a skill dir's SKILL.md, or None. Bounded + fail-soft:
    a missing / oversize / non-regular / unreadable file yields None, never a raise. Only a
    CLOSED leading ``---`` frontmatter block is honored, and only a TOP-LEVEL (column-0)
    ``name:`` key -- so a body ``name:``, a nested ``metadata: {name: ...}``, or an unclosed
    block never mints a false name (matching how a host identifies a skill by its declared
    name). Tolerates a UTF-8 BOM, a quoted value, and a trailing ``# inline comment``."""
    md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(md):
        return None
    try:
        raw, _note = _read_regular_bounded(md, _SKILL_MD_MAX)
    except OSError:
        return None
    if raw is None:                      # oversize / non-regular: never .decode() on None
        return None
    text = raw.decode("utf-8-sig", "replace")   # utf-8-sig strips a leading BOM
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":   # opening fence at column 0 (no leading space)
        return None
    name, seen_name, closed = None, False, False
    for ln in lines[1:]:
        if ln.rstrip() == "---":         # closing fence, column 0 (an indented `---` is NOT one)
            closed = True
            break
        if ln[:1] in (" ", "\t"):        # indented -> a NESTED key, not the top-level name
            continue
        s = ln.strip()
        if s.startswith("name:"):        # CASE-SENSITIVE key (YAML keys are); `Name:` is not it
            after = s[5:]
            if after[:1] not in (" ", "\t", ""):      # require `name: value` / `name:` -- `name:summon`
                continue                              # is a plain scalar, not a mapping key
            if seen_name:                # a SECOND top-level `name:` KEY (even an empty one) ->
                return None              # ambiguous frontmatter, reject (track KEY, not value)
            seen_name = True
            val = after.strip()
            if not val:                               # `name:` with no value -> no name here
                continue
            if val[:1] in ('"', "'"):                 # quoted: take the quoted span
                end = val.find(val[0], 1)
                if end < 0:                           # UNTERMINATED quote -> malformed, reject lookup
                    return None
                raw_rest = val[end + 1:]              # what follows the close quote
                if raw_rest and not raw_rest[:1].isspace():   # glued junk / a `#` with no leading
                    return None                               # space is not a YAML comment -> reject
                rest = raw_rest.strip()
                if rest and not rest.startswith("#"):         # non-comment trailing content -> reject
                    return None
                val = val[1:end]
            else:                                     # bare: a ' #' begins an inline comment
                val = val.split(" #", 1)[0].strip()
            name = val
    return name if closed else None      # an UNCLOSED block is not valid frontmatter


def _dir_is_summon_owned(d: str) -> bool:
    """True iff ``d`` carries a valid summon install manifest. Used to tell OUR transient
    ``summon.staging-*`` artifacts (skip) from a foreign dir merely wearing that name (flag)."""
    try:
        with open(os.path.join(d, _MANIFEST), encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and data.get("installed_by") == "summon"
    except (OSError, ValueError):
        return False


def duplicate_summon_skills(skills_dir: str, skill_name: str = "summon") -> tuple:
    """Return ``(dirs, truncated)``. ``dirs`` = sibling dirs under ``skills_dir`` (other than the
    canonical ``skill_name`` dir) whose SKILL.md declares ``name: <skill_name>`` -- dirs a host
    would load as a SECOND skill of the same name (a stale ``summon.pre-refresh-*`` backup, a
    hand-copied dupe): what makes a host show TWO 'summon' entries. Keyed on the DECLARED name,
    so the intentional ``sub-agents`` alias (name: sub-agents) is NOT flagged. The canonical entry
    is skipped by NAME (``os.path.normcase`` -- a case-variant ``SUMMON`` on Windows is the same
    entry; on case-sensitive POSIX it is a distinct one and IS flagged). A DIFFERENTLY-named
    symlink pointing at the canonical dir is a SECOND entry the host loads, so it is flagged. OUR
    OWN transient ``summon.staging-*`` artifacts are skipped ONLY when they are a real directory
    (not a symlink) carrying a summon manifest -- a FOREIGN or symlinked one wearing that name is
    still flagged. ``truncated`` is True iff the scan was INCOMPLETE -- it hit ``_MAX_SKILLS_SCAN`` OR a
    read error on an EXISTING dir left siblings unscanned (convergence is then unverified; callers
    treat it as blocking, not clean). An ABSENT skills dir is NOT truncated (the host is simply not
    installed). ``dirs`` are the LEXICAL sibling paths (never resolved), so the guidance points at
    the entry the host loads -- removing a duplicate SYMLINK, never its target. Fail-soft; sorted."""
    try:
        mode = os.stat(skills_dir).st_mode           # distinguish ABSENT from INACCESSIBLE:
    except FileNotFoundError:
        return [], False                             # truly absent -> nothing, NOT incomplete
    except OSError:
        return [], True                              # exists but unstatable -> INCOMPLETE, block converged
    if not _stat.S_ISDIR(mode):
        return [], False                             # a file where a dir would be -> no skills here
    skill_lc = os.path.normcase(skill_name)
    out, truncated = [], False
    try:
        with os.scandir(skills_dir) as it:          # streaming, never materializes a huge dir
            for i, entry in enumerate(it):
                if i >= _MAX_SKILLS_SCAN:
                    truncated = True                 # unscanned tail -> convergence unverified
                    break
                # Skip the canonical entry by NAME (case-insensitive on Windows via normcase), NOT
                # by resolved path: a DIFFERENTLY-named symlink pointing at the canonical dir is a
                # SECOND skill the host loads, and must still be flagged.
                if os.path.normcase(entry.name) == skill_lc:
                    continue
                d = os.path.join(skills_dir, entry.name)   # LEXICAL path -- the entry, not its target
                if (entry.name.startswith("summon.staging-") and not entry.is_symlink()
                        and _dir_is_summon_owned(d)):
                    continue                         # OUR transient artifact (verified real dir + ours);
                    #                                  a SYMLINK wearing that name is NOT exempted
                try:
                    if not entry.is_dir():           # follows symlinks: a symlink-to-dir counts
                        continue
                except OSError:
                    truncated = True                 # entry uninspectable -> could BE the dupe; incomplete
                    continue
                if _skill_md_name(d) == skill_name:
                    out.append(d)                    # report the LEXICAL sibling, never _canonical(d)
    except OSError:
        truncated = True                             # the dir EXISTS but a read failed -> incomplete
        return sorted(out), truncated
    return sorted(out), truncated


def _is_summon_plugin_scripts(scripts_dir: str) -> bool:
    """True when ``scripts_dir`` looks like a summon dispatcher (has run_subagent.py)."""
    try:
        return os.path.isfile(os.path.join(scripts_dir, "run_subagent.py"))
    except OSError:
        return False


def _discover_agent_plugin_installs(home: str) -> list[tuple[str, str]]:
    """Known Agent Plugin install roots -> ``(label, scripts_dir)``. Only PRESENT
    installs are returned. Fail-soft; never raises."""
    found: list[tuple[str, str]] = []
    local_root = os.path.join(home, _AGENT_PLUGIN_LOCAL_ROOT)
    local_scripts = os.path.join(local_root, _AGENT_PLUGIN_SCRIPTS_TAIL)
    try:
        if os.path.isdir(local_root) and _is_summon_plugin_scripts(local_scripts):
            found.append(("cursor-plugin-local", local_scripts))
    except OSError:
        pass
    # Marketplace / cached Agent Plugin copies: bounded shallow scan for a ``summon`` dir
    # whose layout matches this repo (…/summon/skills/summon/scripts/run_subagent.py).
    cache_root = os.path.join(home, _AGENT_PLUGIN_CACHE_ROOT)
    try:
        if not os.path.isdir(cache_root):
            return found
        queue: list[tuple[str, int]] = [(cache_root, 0)]
        scanned = 0
        seen_labels: set[str] = set()
        while queue and scanned < _MAX_PLUGIN_CACHE_DIRS:
            parent, depth = queue.pop(0)
            try:
                with os.scandir(parent) as it:
                    for entry in it:
                        if scanned >= _MAX_PLUGIN_CACHE_DIRS:
                            break
                        scanned += 1
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        d = entry.path
                        if os.path.normcase(entry.name) == "summon":
                            scripts = os.path.join(d, _AGENT_PLUGIN_SCRIPTS_TAIL)
                            if _is_summon_plugin_scripts(scripts):
                                rel = os.path.relpath(d, cache_root).replace("\\", "/")
                                label = f"cursor-plugin-cache:{rel}"
                                if label not in seen_labels:
                                    seen_labels.add(label)
                                    found.append((label, scripts))
                        if depth < _MAX_PLUGIN_CACHE_DEPTH:
                            queue.append((d, depth + 1))
            except OSError:
                continue
    except OSError:
        pass
    return found


def _probe(label: str, scripts_dir: str, managed: bool) -> dict:
    """One install record for a ``.../skills/summon/scripts`` directory. Absent copies are
    reported (present=False) rather than dropped, so ``doctor`` can show what is NOT
    installed. A present copy that cannot be hashed (e.g. permission denied on the dir)
    keeps ``sha256=None`` -- classified UNKNOWN downstream, never silently 'converged'.
    Hashing/manifest/version reads never raise."""
    present = os.path.isdir(scripts_dir)
    rec = {"label": label, "scripts_dir": scripts_dir, "present": present,
           "managed": managed, "running": False, "sha256": None, "version": None,
           "installed_at": None, "duplicates": [], "duplicates_truncated": False}
    if present:
        try:
            rec["sha256"] = scripts_sha256(scripts_dir, max_bytes=_ENUM_MAX_BYTES)
        except OSError:
            rec["sha256"] = None
        rec["version"] = _read_version(scripts_dir)
        rec["installed_at"] = _read_installed_at(os.path.dirname(scripts_dir))
    # Sibling dirs the HOST would load as a SECOND 'summon' skill (a stale pre-refresh backup, a
    # hand-copied dupe) -- invisible to the hash check, which only inspects the canonical path.
    # Computed even when the canonical is ABSENT: a host can carry a summon.pre-refresh-* copy
    # with NO canonical `summon`, which still loads as a summon skill and must block converged.
    # scripts_dir is <host>/skills/summon/scripts, so two dirnames up is the host's skills dir.
    rec["duplicates"], rec["duplicates_truncated"] = duplicate_summon_skills(
        os.path.dirname(os.path.dirname(scripts_dir)))
    return rec


def enumerate_installs(running_scripts_dir: str | None = None,
                       home: str | None = None,
                       project_dir: str | None = None) -> list:
    """Every summon install we can locate: the five host copies (``managed``), the
    ``~/.agents`` third-party-clone location from the incident, and the RUNNING copy.

    Present copies that are the SAME physical directory (symlink aliases) are COLLAPSED
    into one record with merged labels (e.g. ``claude+codex``), so a shared copy is neither
    double-counted nor mis-tagged. The running copy is matched by canonical path: if it is
    one of the enumerated copies it is TAGGED there (``running: True``); if it lives
    elsewhere (a repo/worktree checkout) it is appended as its own record. ``home`` is
    injectable for tests."""
    home = home or os.path.expanduser("~")
    raw = [_probe(name, os.path.join(_host_root(name, d, home), "skills", "summon", "scripts"), managed=True)
           for name, d in HOST_DIRS.items()]
    raw.append(_probe("agents",
                      os.path.join(home, ".agents", "skills", "summon", "scripts"),
                      managed=False))
    # A PROJECT-LOCAL copy (`<project>/.agents/skills/summon`) is a real, used layout --
    # a project can carry its own roster and a vendored dispatcher. install.py never
    # touches it (it targets host roots), so nothing refreshes it and it rots silently:
    # exactly how a copy reached v0.9.0 code behind a hand-edited version string, and how
    # a stale copy kept the Windows console-window bug after the hosts were fixed. It is
    # unmanaged (no ownership manifest), so it is REPORTED, never written.
    if project_dir:
        # BOTH conventional project locations. `.agents/skills/summon` is summon's own
        # layout; `.claude/skills/summon` is where `npx skills add` puts it by default, and
        # that one was invisible to enumeration -- a stale copy there left drift reporting
        # `converged: true` while the host ran old code, which is the exact failure this
        # module exists to prevent.
        for _label, _rel in (("project", (".agents", "skills", "summon", "scripts")),
                             ("project-claude", (".claude", "skills", "summon", "scripts"))):
            raw.append(_probe(_label, os.path.join(project_dir, *_rel), managed=False))
    for label, scripts_dir in _discover_agent_plugin_installs(home):
        raw.append(_probe(label, scripts_dir, managed=False))
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
            # MERGE duplicates: two hosts sharing one physical summon (symlink alias) may still
            # have DIFFERENT skills dirs, so a duplicate visible only from the later host's dir
            # must not be dropped on collapse (else converged is falsely True).
            first["duplicates"] = sorted(set(first.get("duplicates", []))
                                         | set(r.get("duplicates", [])))
            # an incomplete scan on EITHER collapsed record leaves convergence unverified
            first["duplicates_truncated"] = bool(first.get("duplicates_truncated")
                                                 or r.get("duplicates_truncated"))
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
    cry drift we cannot anchor. ``converged`` requires a reference, NO drift, no unknown
    copy (an uncheckable copy is never reported as 'all match'), AND no host carrying a
    duplicate 'summon' skill (a stale backup dir loaded beside the canonical copy). Returns
    {reference_sha, converged, present, hashed, drifted, unknown, duplicates}."""
    if reference_sha is None:
        run = next((r for r in records if r.get("running") and r.get("sha256")), None)
        reference_sha = run["sha256"] if run else None
    present = [r for r in records if r["present"]]
    hashed = [r for r in present if r["sha256"]]
    unknown = [r for r in present if not r["sha256"]]
    drifted = [r for r in hashed if reference_sha and r["sha256"] != reference_sha]
    # Duplicate 'summon' skills a host loads beside its canonical copy. Hash-convergence says
    # NOTHING about these: a host can be byte-identical on the canonical path yet still show TWO
    # summon entries (the field symptom), so any duplicate blocks `converged`. A truncated scan
    # (a skills dir too large to fully scan) leaves duplicates UNVERIFIED, so it blocks too --
    # tracked separately from real duplicate paths so consumers never render it as a deletable dir.
    duplicates = [{"label": r["label"], "dirs": r["duplicates"]}
                  for r in records if r.get("duplicates")]
    truncated = [r["label"] for r in records if r.get("duplicates_truncated")]
    return {"reference_sha": reference_sha,
            "converged": (bool(reference_sha) and not drifted and not unknown
                          and not duplicates and not truncated),
            "present": present, "hashed": hashed, "drifted": drifted,
            "unknown": unknown, "duplicates": duplicates, "scan_truncated": truncated}
