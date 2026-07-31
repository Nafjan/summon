#!/usr/bin/env python3
"""Cross-platform stream-json proxy for `agy` one-shot dispatches.

agy renders terminal animation by default and historically required a PTY scraper
on Windows. Since recent versions expose `--output-format stream-json`, we can now
consume a stable line-stream of JSON events directly and forward it to summon.

The wrapper keeps the launch arguments in a single place (the spawned `agy`
invocation), injects the stream format when absent, and forwards stdout+stderr
as-is so summon can parse terminal events with its existing `StreamProcessor`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from _spawn import popen_flags

def _find_agy() -> str | None:
    p = shutil.which("agy")
    if p and os.path.isfile(p):
        return p
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = os.path.join(local, "agy", "bin", "agy.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _has_output_format_flag(args: list[str]) -> bool:
    """True when args already choose output-format explicitly."""
    for a in args:
        # Prompt content starts after --print and must never be interpreted
        # as option syntax. A prompt can legitimately include `--output-format`
        # and should not suppress injection.
        if a == "--print":
            break
        if a == "--output-format":
            return True
        if a.startswith("--output-format="):
            return True
        # Defensive for any future shorthand that takes a single next token
        if a in {"--output", "-of"}:  # undocumented/defensive
            return True
    return False


def _strip_boundary_flags(args: list[str]) -> list[str]:
    """Drop flags that can increase workspace scope or bypass read-only controls.

    The proxy is a boundary utility; callers must not pass boundary flags through
    this layer. Strip only direct agy flags (and their values) to avoid prompt
    text being accidentally rewritten.
    """
    boundary_flags = {
        "--add-dir", "--mode", "--sandbox",
        "--conversation", "--continue", "--continue=", "--agent", "--agent=", "--project",
        "--project=", "--new-project", "--new-project=", "--log-file", "--log-file=",
        "--dangerously-skip-permissions", "--yolo", "--resume", "--continue-id", "--output-file"
    }
    boundary_prefixes = {"--add-dir=", "--mode=", "--sandbox="}
    result: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--print":
            # Prompt text follows --print and is never parsed as a CLI flag.
            result.append(a)
            result.extend(args[i + 1:])
            break
        if a in {
            "--add-dir", "--mode", "--sandbox", "--conversation",
            "--agent", "--project", "--log-file",
        }:
            if i + 1 < len(args):
                i += 2
            else:
                i += 1
            continue
        if a in {
            "--dangerously-skip-permissions", "--yolo", "--resume", "--continue-id",
            "--continue", "--new-project", "--output-file"
        }:
            i += 1
            continue
        if a in boundary_prefixes:
            i += 1
            continue
        if any(a.startswith(pfx) for pfx in boundary_prefixes):
            i += 1
            continue
        result.append(a)
        i += 1
    return result


def _build_agy_cmd(args: list[str]) -> list[str]:
    agy = _find_agy()
    if not agy:
        sys.stderr.write("agy executable not found\n")
        raise SystemExit(127)
    if os.environ.get("AGY_STREAM_PROXY_ALLOW_BOUNDARY") == "1":
        cmd = [agy]
        if not _has_output_format_flag(args):
            cmd.extend(["--output-format", "stream-json"])
        cmd.extend(args)
        return cmd
    args = _strip_boundary_flags(args)
    cmd = [agy]
    if not _has_output_format_flag(args):
        cmd.extend(["--output-format", "stream-json"])
    cmd.extend(args)
    return cmd


def _flush_stdout(proc: subprocess.Popen) -> int:
    try:
        if proc.stdout is None:
            return proc.wait()
        for line in iter(proc.stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
    except OSError:
        # If the stream drops while the process is exiting, rely on the process
        # exit code for the envelope.
        pass
    return proc.wait()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = list(sys.argv[1:])
    if not args:
        sys.stderr.write("usage: agy_stream_proxy.py <agy args>\n")
        return 2

    cmd = _build_agy_cmd(args)
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.setdefault("TERM", "xterm-256color")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_flags(),
            env=env,
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"spawn failed: {type(e).__name__}: {e}\n")
        return 1

    try:
        code = _flush_stdout(proc)
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return 130
    return int(code if code is not None else 0)


if __name__ == "__main__":
    raise SystemExit(main())
