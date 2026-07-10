#!/usr/bin/env python3
"""ConPTY + pyte wrapper for agy (Google Antigravity CLI) headless one-shots.

agy's --print mode renders its answer with a terminal "drip" (typewriter)
animation using cursor-movement escapes that only fire on a real TTY. Piping
stdout yields nothing. This wrapper:

  1. Spawns agy under a Windows ConPTY (pywinpty) so it takes the TTY path.
  2. Feeds the raw byte stream into a `pyte` HistoryScreen (an in-memory VT100
     emulator with scrollback) so cursor moves / repaints / erases resolve to
     the true final text, and long outputs that scroll off the visible window
     are still recovered from history.
  3. Once agy exits (EOF) — or output goes quiet, or a deadline hits — it dumps
     the full scrollback + visible screen as clean UTF-8 text to its own stdout.

Completion is primarily EOF (a real one-shot exits). The quiet-period and
deadline are backstops for the case where agy keeps the PTY open after the
answer is on screen. Exit codes let the caller judge trust: 0 = clean EOF
(agy's own exit status), 2 = forced stop (deadline/quiet -> the capture may be
partial or an auth/error screen, so the caller treats it as failure), 1 =
nothing captured.

NOTE: this must run under an interpreter that has BOTH `pywinpty` and `pyte`
installed (e.g. C:\\python313\\python.exe on this machine).

Usage: python agy_pty_pyte.py [--debug] <agy_args...>
Env knobs: AGY_PTY_DEADLINE (s, def 240), AGY_PTY_QUIET (s, def 20),
           AGY_PTY_ROWS (visible rows, def 50), AGY_PTY_COLS (def 400),
           AGY_PTY_HISTORY (scrollback lines, def 8000).
"""
from __future__ import annotations

import os
import re
import sys
import time
import shutil
import threading

try:
    import winpty  # pywinpty
except ImportError:
    sys.stderr.write("pywinpty not installed in this interpreter.\n")
    sys.exit(127)

try:
    import pyte
except ImportError:
    sys.stderr.write("pyte not installed in this interpreter.\n")
    sys.exit(127)

DEADLINE_S = float(os.environ.get("AGY_PTY_DEADLINE", "240"))
QUIET_S = float(os.environ.get("AGY_PTY_QUIET", "20"))
ROWS = int(os.environ.get("AGY_PTY_ROWS", "50"))
COLS = int(os.environ.get("AGY_PTY_COLS", "1000"))
HISTORY = int(os.environ.get("AGY_PTY_HISTORY", "8000"))


def _find_agy() -> str | None:
    p = shutil.which("agy")
    if p and os.path.isfile(p):
        return p
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cand = os.path.join(la, "agy", "bin", "agy.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _render_row(row, columns: int) -> str:
    """Render one pyte buffer/history row (a {x: Char} mapping) to a string."""
    try:
        return "".join(row[x].data for x in range(columns)).rstrip()
    except Exception:
        return ""


def _dump(screen) -> str:
    """Full text = scrolled-off history (top) + current visible screen."""
    lines: list = []
    try:
        for row in list(screen.history.top):
            lines.append(_render_row(row, screen.columns))
    except Exception:
        pass
    for s in screen.display:
        lines.append(s.rstrip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = sys.argv[1:]
    debug = False
    if args and args[0] == "--debug":
        debug = True
        args = args[1:]
    if not args:
        sys.stderr.write("usage: agy_pty_pyte.py [--debug] <agy_args...>\n")
        return 2

    agy = _find_agy()
    if not agy:
        sys.stderr.write("agy.exe not found.\n")
        return 127

    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm-256color"

    try:
        proc = winpty.PtyProcess.spawn([agy, *args], dimensions=(ROWS, COLS), env=env)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"spawn failed: {type(e).__name__}: {e}\n")
        return 1

    raw = bytearray()
    last_data = [time.time()]
    last_content = [0.0]  # time we last saw real (non-escape) alphanumeric output
    done = threading.Event()

    # Strip ALL escape sequences (CSI, OSC, other) so terminal-setup and any
    # title spam don't register as "real content" for the quiet-stop heuristic.
    _ESC_RE = re.compile(rb"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
    _ALNUM_RE = re.compile(rb"[A-Za-z0-9]")

    def reader():
        while True:
            try:
                chunk = proc.read(8192)  # returns str
            except EOFError:
                break
            except Exception:
                break
            if not chunk:
                break
            b = chunk.encode("utf-8", "replace")
            raw.extend(b)
            now = time.time()
            last_data[0] = now
            if _ALNUM_RE.search(_ESC_RE.sub(b"", b)):
                last_content[0] = now
        done.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    start = time.time()
    stop_reason = "eof"
    while not done.is_set():
        now = time.time()
        if now - start > DEADLINE_S:
            stop_reason = "deadline"
            break
        if last_content[0] and (now - last_content[0]) > QUIET_S:
            stop_reason = "content-quiet"
            break
        time.sleep(0.5)

    try:
        proc.terminate(force=True)
    except Exception:
        pass
    # Drain the reader before rendering: terminate() closes the PTY, so the
    # reader hits EOF and sets `done`. Joining guarantees every byte is in
    # `raw` before we feed pyte — otherwise a forced stop could render a
    # truncated answer while the reader is still appending tail bytes.
    done.wait(timeout=5)
    reader_thread.join(timeout=5)

    screen = pyte.HistoryScreen(COLS, ROWS, history=HISTORY, ratio=0.5)
    stream = pyte.ByteStream(screen)
    try:
        stream.feed(bytes(raw))
    except Exception as e:  # noqa: BLE001
        if debug:
            sys.stderr.write(f"[pyte feed error: {e}]\n")

    text = _dump(screen)

    if debug:
        sys.stderr.write(
            f"[stop={stop_reason} captured={len(raw)}b lines={text.count(chr(10)) + 1} "
            f"exit={proc.exitstatus}]\n"
        )
        sys.stderr.write("[--- RAW REPR (first 1000) ---]\n")
        sys.stderr.write(repr(bytes(raw)[:1000]) + "\n[--- END RAW ---]\n")

    sys.stdout.write(text + "\n")
    sys.stdout.flush()

    # Exit-code semantics let the caller distinguish a clean one-shot from a
    # partial/suspect capture (an auth prompt, an error screen, or a deadline/
    # quiet cutoff while agy was still working):
    #   1  -> nothing captured (hard failure)
    #   2  -> forced stop (deadline/quiet): agy did NOT exit on its own, so the
    #         text may be partial or not the real answer -> caller treats as error
    #   else clean EOF -> propagate agy's own exit status (0 = success)
    if not text.strip():
        return 1
    if stop_reason != "eof":
        return 2
    return proc.exitstatus if proc.exitstatus is not None else 0


if __name__ == "__main__":
    sys.exit(main())
