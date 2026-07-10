#!/usr/bin/env python3
"""Install the summon skill into every AI-CLI host on this machine.

    python install.py                 # detect hosts, install skill + starter agents
    python install.py --dry-run       # show what would happen, touch nothing
    python install.py --hosts claude,codex
    python install.py --no-agents     # skill only, skip the starter agent roster
    python install.py --uninstall     # remove installed copies (never touches agents)

What it does:
  1. Copies SKILL.md + scripts/ + references/ into <host>/skills/summon/ for each
     detected host (~/.claude, ~/.codex, ~/.cursor, ~/.gemini, ~/.copilot).
  2. Copies the starter agent roster into ~/.agents/ (skipping any name that
     already exists there — your own agents are never overwritten).
  3. Prints next steps (run --doctor).

Idempotent: re-running refreshes the skill copies in place.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

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
AGENTS_DIR = os.path.join(HOME, ".agents")


def detect_hosts() -> list:
    return [name for name, root in HOSTS.items() if os.path.isdir(root)]


def install_skill(host: str, dry: bool) -> str:
    dest = os.path.join(HOSTS[host], "skills", "summon")
    if dry:
        return f"[dry] would install skill -> {dest}"
    os.makedirs(dest, exist_ok=True)
    for item in SKILL_PAYLOAD:
        src = os.path.join(HERE, item)
        dst = os.path.join(dest, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
    return f"[ok]  skill installed -> {dest}"


def uninstall_skill(host: str, dry: bool) -> str:
    dest = os.path.join(HOSTS[host], "skills", "summon")
    if not os.path.isdir(dest):
        return f"[--]  nothing at {dest}"
    if dry:
        return f"[dry] would remove {dest}"
    shutil.rmtree(dest)
    return f"[ok]  removed {dest}"


def install_agents(dry: bool) -> list:
    """Copy starter agents into ~/.agents WITHOUT overwriting existing names."""
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
        if os.path.exists(dst):
            skipped += 1
            continue
        if not dry:
            shutil.copy2(os.path.join(src_dir, f), dst)
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
            return 1
    else:
        hosts = detect_hosts()
        if not hosts:
            print("No AI-CLI host dirs found (~/.claude, ~/.codex, ~/.cursor, ~/.gemini, "
                  "~/.copilot).\nInstall and run at least one CLI first, or pass --hosts "
                  "explicitly.")
            return 1

    print(f"hosts: {', '.join(hosts)}\n")
    for h in hosts:
        print(uninstall_skill(h, args.dry_run) if args.uninstall
              else install_skill(h, args.dry_run))

    if not args.uninstall and not args.no_agents:
        for line in install_agents(args.dry_run):
            print(line)

    if not args.uninstall:
        shim = os.path.join(HERE, "summon.py")
        print(f"\nNext: check your setup ->  python \"{shim}\" --doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
