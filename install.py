#!/usr/bin/env python3
"""Install the summon skill into every AI-CLI host on this machine.

    python install.py                 # detect hosts, install skill + starter agents
    python install.py --dry-run       # show what would happen, touch nothing
    python install.py --hosts claude,codex
    python install.py --no-agents     # skill only, skip the starter agent roster
    python install.py --uninstall     # remove ONLY copies summon installed

What it does:
  1. Copies SKILL.md + scripts/ + references/ into <host>/skills/summon/ for each
     detected host (~/.claude, ~/.codex, ~/.cursor, ~/.gemini, ~/.copilot), writing
     an ownership manifest (.summon-install.json) into each copy.
  2. Copies the starter agent roster into ~/.agents/ with EXCLUSIVE creation —
     an agent file you already have is never touched, even under races.
  3. Prints next steps (run --doctor).

Safety model (every destructive operation is ownership-gated):
  - A directory counts as summon-owned ONLY if its manifest parses as JSON and
    says {"installed_by": "summon"}. Corrupt or foreign manifests fail closed.
  - Refuses to replace or remove anything it does not own — including stale
    staging/backup artifacts: those are only cleaned when they carry a valid
    manifest of their own.
  - Refreshes are staged in a unique temp dir and swapped in by rename; the old
    tree is kept as an owned backup until the swap succeeds, and restored on
    failure (or on the next run after a crash). A per-host lock file prevents
    concurrent installers from fighting over the swap.

Exit codes: 0 = all requested work done; 2 = one or more hosts refused/failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

# Host roots that support file-based skills. A host is "detected" when its root
# dir exists (i.e. the CLI has run at least once on this machine).
HOSTS = {
    "claude": os.path.join(HOME, ".claude"),
    "codex": os.path.join(HOME, ".codex"),
    "cursor": os.path.join(HOME, ".cursor"),
    "gemini": os.path.join(HOME, ".gemini"),
    "copilot": os.path.join(HOME, ".copilot"),
}

SKILL_PAYLOAD = ["SKILL.md", "scripts", "references"]
MANIFEST = ".summon-install.json"
LOCK_STALE_SEC = 600
AGENTS_DIR = os.path.join(HOME, ".agents")


def detect_hosts() -> list:
    return [name for name, root in HOSTS.items() if os.path.isdir(root)]


def _owned(path: str) -> bool:
    """True ONLY for a directory whose manifest parses to a dict that identifies
    summon. Anything else — missing, unreadable, corrupt, foreign, or valid JSON
    of the wrong shape (e.g. []) — fails closed."""
    try:
        with open(os.path.join(path, MANIFEST), encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and data.get("installed_by") == "summon"
    except (OSError, ValueError):
        return False


def _write_manifest(dst: str, files: list) -> None:
    with open(os.path.join(dst, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump({"installed_by": "summon", "installed_at": int(time.time()),
                   "files": sorted(files)}, fh, indent=1)


def _lock_owned(lock: str) -> bool:
    """A lock we may break must itself carry a validated summon marker —
    a random user file that happens to share the name is never deleted."""
    try:
        with open(lock, encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and data.get("installed_by") == "summon"
    except (OSError, ValueError):
        return False


def _acquire_lock(parent: str) -> str | None:
    """O_EXCL lock file so two installers (or an installer and an uninstaller)
    can't race the same host. A stale lock (> LOCK_STALE_SEC) is broken once,
    and ONLY if it is summon's own marker with an unchanged mtime — an unowned
    or freshly-replaced file is left alone."""
    lock = os.path.join(parent, "summon.install.lock")
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"installed_by": "summon", "pid": os.getpid()}, fh)
            return lock
        except FileExistsError:
            try:
                observed_mtime = os.path.getmtime(lock)
            except OSError:
                continue  # vanished under us -> retry the O_EXCL create
            if time.time() - observed_mtime > LOCK_STALE_SEC and _lock_owned(lock):
                try:
                    # Re-check mtime right before unlink: if another process
                    # just replaced the lock, its file is NOT ours to break.
                    if os.path.getmtime(lock) == observed_mtime:
                        os.unlink(lock)
                except OSError:
                    pass
                continue
            return None
        except OSError:
            return None
    return None


def _build_tree(dst: str) -> list:
    """Copy the payload into dst; return the manifest file list."""
    files = []
    for item in SKILL_PAYLOAD:
        src = os.path.join(HERE, item)
        out = os.path.join(dst, item)
        if os.path.isdir(src):
            shutil.copytree(src, out,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for root, _, fnames in os.walk(out):
                files += [os.path.relpath(os.path.join(root, f), dst) for f in fnames]
        elif os.path.isfile(src):
            shutil.copy2(src, out)
            files.append(item)
    return files


def install_skill(host: str, dry: bool) -> tuple:
    """Returns (message, ok). Staged install: build in a unique temp dir, keep
    the old tree as an owned backup, swap by rename, restore on failure."""
    dest = os.path.join(HOSTS[host], "skills", "summon")
    parent = os.path.dirname(dest)
    backup = dest + ".previous"

    if dry:
        # Mutation-free by contract: report what WOULD happen, including a
        # pending crash recovery, without renaming or creating anything.
        if not os.path.isdir(dest) and os.path.isdir(backup) and _owned(backup):
            return (f"[dry] would restore crashed backup then refresh -> {dest}", True)
        if os.path.isdir(dest) and not _owned(dest):
            return (f"[!!]  {dest} exists but was NOT installed by summon "
                    f"(no valid {MANIFEST}); refusing to replace it - move it aside first", False)
        verb = "refresh" if os.path.isdir(dest) else "install"
        return (f"[dry] would {verb} skill -> {dest}", True)

    os.makedirs(parent, exist_ok=True)
    lock = _acquire_lock(parent)
    if lock is None:
        return (f"[!!]  {host}: another summon install appears to be running "
                f"(lock: {os.path.join(parent, 'summon.install.lock')}); retry shortly", False)

    staging = None
    try:
        # Crash recovery (under the lock): a previous run may have moved the
        # good tree aside and died before the swap. Put it back first.
        if not os.path.isdir(dest) and os.path.isdir(backup) and _owned(backup):
            try:
                os.rename(backup, dest)
            except OSError:
                pass

        if os.path.isdir(dest) and not _owned(dest):
            return (f"[!!]  {dest} exists but was NOT installed by summon "
                    f"(no valid {MANIFEST}); refusing to replace it - move it aside first", False)

        # Clean OUR stale artifacts only — anything without a valid summon
        # manifest is not ours to delete, no matter what it is named.
        for name in os.listdir(parent):
            p = os.path.join(parent, name)
            if name.startswith("summon.staging-") and os.path.isdir(p) and _owned(p):
                shutil.rmtree(p, ignore_errors=True)
        if os.path.isdir(backup) and _owned(backup):
            shutil.rmtree(backup, ignore_errors=True)

        staging = tempfile.mkdtemp(prefix="summon.staging-", dir=parent)
        # Manifest goes in FIRST: even a half-copied staging dir is then
        # recognizably ours, so a crashed run's leftovers can be reaped later.
        _write_manifest(staging, [])
        files = _build_tree(staging)
        _write_manifest(staging, files)

        if os.path.isdir(dest):
            os.rename(dest, backup)   # owned (checked above); becomes rollback copy
        os.rename(staging, dest)
        staging = None
        if os.path.isdir(backup) and _owned(backup):
            shutil.rmtree(backup, ignore_errors=True)
        return (f"[ok]  skill installed -> {dest}", True)
    except OSError as e:
        # Roll back: if the swap half-happened, restore the owned backup.
        if not os.path.isdir(dest) and os.path.isdir(backup) and _owned(backup):
            try:
                os.rename(backup, dest)
            except OSError:
                pass
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)  # mkdtemp'd by us this run
        return (f"[!!]  {host}: install failed ({e}); previous copy left in place", False)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def uninstall_skill(host: str, dry: bool) -> tuple:
    dest = os.path.join(HOSTS[host], "skills", "summon")
    if not os.path.isdir(dest):
        return (f"[--]  nothing at {dest}", True)
    if not _owned(dest):
        return (f"[!!]  {dest} has no valid {MANIFEST} - summon did not install it; "
                f"refusing to delete", False)
    if dry:
        return (f"[dry] would remove {dest}", True)
    # Same lock as install: an uninstall must not race a concurrent installer
    # (which could otherwise recreate the tree mid-removal, or vice versa).
    lock = _acquire_lock(os.path.dirname(dest))
    if lock is None:
        return (f"[!!]  {host}: another summon install/uninstall appears to be "
                f"running; retry shortly", False)
    try:
        if not _owned(dest):  # re-check under the lock
            return (f"[!!]  {dest} changed while acquiring the lock; refusing", False)
        shutil.rmtree(dest)
        return (f"[ok]  removed {dest}", True)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def install_agents(dry: bool) -> list:
    """Copy starter agents into ~/.agents with O_EXCL creation - an existing
    file is never opened, truncated, or replaced (race-safe, not just checked).
    A failed copy removes its partial file so the next run can retry it."""
    src_dir = os.path.join(HERE, "agents")
    out = []
    if not os.path.isdir(src_dir):
        return ["[--]  no bundled agents/ dir; skipping"]
    if not dry:
        os.makedirs(AGENTS_DIR, exist_ok=True)
    added = skipped = failed = 0
    for f in sorted(os.listdir(src_dir)):
        if not f.endswith(".md"):
            continue
        dst = os.path.join(AGENTS_DIR, f)
        if dry:
            skipped += os.path.exists(dst)
            added += not os.path.exists(dst)
            continue
        try:
            fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            skipped += 1
            continue
        except OSError:
            failed += 1
            continue
        try:
            with os.fdopen(fd, "wb") as fout, open(os.path.join(src_dir, f), "rb") as fin:
                shutil.copyfileobj(fin, fout)
            added += 1
        except OSError:
            # Never leave a partial agent behind - it would read as user-owned
            # and block this starter agent forever.
            try:
                os.unlink(dst)
            except OSError:
                pass
            failed += 1
    verb = "[dry] would add" if dry else "[ok]  added"
    line = (f"{verb} {added} starter agents -> {AGENTS_DIR} "
            f"({skipped} already present, left untouched)")
    if failed:
        line += f"  [!!] {failed} failed to copy (partials removed; re-run to retry)"
    out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hosts", help="comma-separated subset (default: all detected)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-agents", action="store_true", help="skip the starter agent roster")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
        unknown = [h for h in hosts if h not in HOSTS]
        if unknown:
            print(f"unknown host(s): {', '.join(unknown)}  (valid: {', '.join(HOSTS)})")
            return 2
    else:
        hosts = detect_hosts()
        if not hosts:
            print("No AI-CLI host dirs found (~/.claude, ~/.codex, ~/.cursor, ~/.gemini, "
                  "~/.copilot).\nInstall and run at least one CLI first, or pass --hosts "
                  "explicitly.")
            return 2

    print(f"hosts: {', '.join(hosts)}\n")
    all_ok = True
    for h in hosts:
        msg, ok = (uninstall_skill(h, args.dry_run) if args.uninstall
                   else install_skill(h, args.dry_run))
        all_ok &= ok
        print(msg)

    if not args.uninstall and not args.no_agents:
        for line in install_agents(args.dry_run):
            print(line)

    if not args.uninstall:
        shim = os.path.join(HERE, "summon.py")
        print(f"\nNext: check your setup ->  python \"{shim}\" --doctor")
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
