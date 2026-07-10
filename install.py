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

Safety model:
  - Refuses to replace or remove a skills/summon dir that has no ownership
    manifest (i.e. anything summon did not itself install).
  - Refreshes are staged: the new tree is built beside the old one and swapped
    in with rename, so a failed copy never leaves a half-updated skill. Stale
    files from prior versions are gone after the swap (true refresh).

Exit codes: 0 = all requested work done; 2 = one or more hosts refused/failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
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
AGENTS_DIR = os.path.join(HOME, ".agents")


def detect_hosts() -> list:
    return [name for name, root in HOSTS.items() if os.path.isdir(root)]


def _owned(dest: str) -> bool:
    """True only for a directory summon itself installed (manifest present)."""
    return os.path.isfile(os.path.join(dest, MANIFEST))


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
    """Returns (message, ok). Staged install: build beside, swap in, keep the
    old tree until the new one is fully in place."""
    dest = os.path.join(HOSTS[host], "skills", "summon")
    if os.path.isdir(dest) and not _owned(dest):
        return (f"[!!]  {dest} exists but was NOT installed by summon "
                f"(no {MANIFEST}); refusing to replace it - move it aside first", False)
    if dry:
        verb = "refresh" if os.path.isdir(dest) else "install"
        return (f"[dry] would {verb} skill -> {dest}", True)

    staging = dest + ".staging"
    backup = dest + ".previous"
    for leftover in (staging, backup):  # from an interrupted earlier run
        shutil.rmtree(leftover, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.makedirs(staging)
        files = _build_tree(staging)
        with open(os.path.join(staging, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump({"installed_by": "summon", "installed_at": int(time.time()),
                       "files": sorted(files)}, fh, indent=1)
        if os.path.isdir(dest):
            os.rename(dest, backup)  # backup slot was cleared above on both OSes
        os.rename(staging, dest)
        shutil.rmtree(backup, ignore_errors=True)
        return (f"[ok]  skill installed -> {dest}", True)
    except OSError as e:
        # Roll back: restore the previous tree if we had moved it aside.
        if not os.path.isdir(dest) and os.path.isdir(backup):
            try:
                os.rename(backup, dest)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        return (f"[!!]  {host}: install failed ({e}); previous copy left in place", False)


def uninstall_skill(host: str, dry: bool) -> tuple:
    dest = os.path.join(HOSTS[host], "skills", "summon")
    if not os.path.isdir(dest):
        return (f"[--]  nothing at {dest}", True)
    if not _owned(dest):
        return (f"[!!]  {dest} has no {MANIFEST} - summon did not install it; "
                f"refusing to delete", False)
    if dry:
        return (f"[dry] would remove {dest}", True)
    shutil.rmtree(dest)
    return (f"[ok]  removed {dest}", True)


def install_agents(dry: bool) -> list:
    """Copy starter agents into ~/.agents with O_EXCL creation - an existing
    file is never opened, truncated, or replaced (race-safe, not just checked)."""
    src_dir = os.path.join(HERE, "agents")
    out = []
    if not os.path.isdir(src_dir):
        return ["[--]  no bundled agents/ dir; skipping"]
    if not dry:
        os.makedirs(AGENTS_DIR, exist_ok=True)
    added = skipped = 0
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
        with os.fdopen(fd, "wb") as fout, open(os.path.join(src_dir, f), "rb") as fin:
            shutil.copyfileobj(fin, fout)
        added += 1
    verb = "[dry] would add" if dry else "[ok]  added"
    out.append(f"{verb} {added} starter agents -> {AGENTS_DIR} "
               f"({skipped} already present, left untouched)")
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
