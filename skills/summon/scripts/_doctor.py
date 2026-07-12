"""Environment health checks for the dispatcher (``--doctor``).

Answers a new user's first questions: "which backends can I actually use on
this machine, and how do I finish setting up the rest?" Read-only and
fail-soft - never raises, never mutates anything, works (and is useful) even
on a machine with zero backends installed.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_VERSION_TIMEOUT = 10

# Install/auth hints are static strings shown to humans; they never execute.
_BACKENDS = {
    "claude": {
        "install": "npm install -g @anthropic-ai/claude-code",
        "auth": "claude auth login",
    },
    "codex": {
        "install": "npm install -g @openai/codex",
        "auth": "codex login  (ChatGPT subscription; a stray OPENAI_API_KEY is stripped by default)",
    },
    "cursor-agent": {
        "install": "https://cursor.com/cli",
        "auth": "cursor-agent login  (or CLI_API_KEY env, forwarded as CURSOR_API_KEY)",
    },
    "gemini": {
        "install": "npm install -g @google/gemini-cli",
        "auth": "gemini  (first interactive run) or GEMINI_API_KEY",
    },
    "agy": {
        "install": "Antigravity CLI (https://antigravity.google)",
        "auth": "agy login  - Windows-only out of the box (ConPTY wrapper); "
                "POSIX needs AGY_PTY_WRAPPER (see docs)",
    },
}


def _probe_version(path: str) -> str | None:
    """First line of ``<cli> --version`` (static arg), or None. On Windows a
    .cmd/.bat shim cannot be exec'd directly - route through cmd.exe."""
    cmd = [path, "--version"]
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", path, "--version"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_VERSION_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        # A binary that errors on --version is not a working backend, no matter
        # what it prints (impostor/broken-install guard).
        return None
    # Combine BOTH streams: some CLIs print the version to stderr, and a
    # whitespace-only stdout must not mask a real version on stderr.
    combined = f"{r.stdout or ''}\n{r.stderr or ''}"
    lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    return lines[0][:120] if lines else None


def _check_backends() -> dict:
    """Probe all backends CONCURRENTLY (a hostile/hung PATH entry costs one
    timeout, not five in sequence). A CLI that is on PATH but fails its
    --version probe is reported found-but-unverified and NOT counted usable —
    a random binary shadowing a backend name must not read as ready."""
    names = list(_BACKENDS)
    paths = {n: shutil.which(n) for n in names}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        versions = dict(zip(names, pool.map(
            lambda n: _probe_version(paths[n]) if paths[n] else None, names)))
    out: dict = {}
    for name in names:
        path = paths[name]
        entry: dict = {"found": bool(path), "path": path}
        if path:
            entry["version"] = versions[name]
            entry["verified"] = versions[name] is not None
        else:
            entry["install"] = _BACKENDS[name]["install"]
        entry["auth_hint"] = _BACKENDS[name]["auth"]
        out[name] = entry
    return out


def _check_agy_extras(backends: dict) -> dict:
    """agy needs more than the CLI: a PTY wrapper + an interpreter with
    pywinpty+pyte (bundled wrapper), or a user-supplied wrapper on POSIX."""
    from _builder import _agy_python, _has_pty_modules, _agy_wrapper
    entry: dict = {"platform_ok": os.name == "nt" or bool(os.environ.get("AGY_PTY_WRAPPER"))}
    try:
        wrapper = _agy_wrapper()
        entry["wrapper"] = wrapper
        entry["wrapper_found"] = os.path.isfile(wrapper)
    except ValueError as e:
        entry["wrapper"] = None
        entry["wrapper_found"] = False
        entry["note"] = str(e)
        return entry
    if os.environ.get("AGY_PTY_WRAPPER"):
        entry["python"] = os.environ.get("AGY_PTY_PYTHON") or sys.executable
        entry["pty_modules"] = None  # custom wrapper: deps unknown by design
    else:
        py = _agy_python()
        entry["python"] = py
        entry["pty_modules"] = _has_pty_modules(py)
        if not entry["pty_modules"]:
            entry["note"] = f"pip install pywinpty pyte  (into {py})"
    return entry


def _check_agents_dir(agents_dir: str | None, cwd: str | None) -> dict:
    from _loader import bundled_roster_dir, get_agents_dir, list_agents
    try:
        resolved = get_agents_dir(agents_dir, cwd or os.getcwd())
        agents = list_agents(resolved)
        found = os.path.isdir(resolved)
        entry = {"path": resolved, "found": found,
                 "agent_count": len(agents),
                 "agents": sorted(a.get("name", "?") for a in agents)[:50]}
        # list_agents falls back to the skill's bundled starter roster, so a
        # fresh install lists agents even when the project dir is absent. Say so
        # explicitly rather than emitting a contradictory found:false + count>0.
        bundled = bundled_roster_dir()
        if not found and bundled and agents:
            entry["note"] = (f"project roster {resolved} not present — dispatching "
                             f"the skill's bundled starter roster ({bundled})")
        return entry
    except Exception as e:  # noqa: BLE001 - doctor never raises
        return {"path": agents_dir, "found": False, "agent_count": 0,
                "note": f"{type(e).__name__}: {e}"}


def doctor(agents_dir: str | None = None, cwd: str | None = None) -> dict:
    backends = _check_backends()
    report = {
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "backends": backends,
        "agy_extras": _check_agy_extras(backends),
        "agents_dir": _check_agents_dir(agents_dir, cwd),
        "git": {"found": bool(shutil.which("git"))},
        "billing_guard": {
            "openai_api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
            "guard_active": os.environ.get("SUBAGENTS_ALLOW_OPENAI_KEY") != "1",
            "note": "codex children get OPENAI_API_KEY stripped (subscription billing) "
                    "unless SUBAGENTS_ALLOW_OPENAI_KEY=1",
        },
    }
    usable = [n for n, b in backends.items() if b["found"] and b.get("verified")]
    if "agy" in usable and not (
        report["agy_extras"].get("platform_ok")
        and report["agy_extras"].get("wrapper_found")
        and report["agy_extras"].get("pty_modules") in (True, None)
    ):
        usable.remove("agy")
    report["usable_backends"] = usable
    report["ok"] = bool(usable)
    return report


def render(report: dict) -> str:
    """Human-readable summary. ASCII-only markers - Windows consoles default
    to cp1252 and must never crash the doctor."""
    lines = [
        f"platform : {report['platform']}   python {report['python']}",
        f"git      : {'[OK]' if report['git']['found'] else '[--] not found (needed for --worktree)'}",
        "",
        "backends:",
    ]
    for name, b in report["backends"].items():
        if b["found"]:
            ver = b.get("version") or "version unknown"
            mark, detail = "[OK]", f"{ver}  ({b['path']})"
            if not b.get("verified"):
                mark, detail = "[!!]", (f"on PATH but --version probe failed "
                                        f"({b['path']}) - broken install or impostor binary")
            if name == "agy" and b.get("verified"):
                ex = report["agy_extras"]
                if not ex.get("platform_ok"):
                    mark, detail = "[!!]", "CLI found but backend needs Windows or AGY_PTY_WRAPPER"
                elif not ex.get("wrapper_found"):
                    mark, detail = "[!!]", f"CLI found but PTY wrapper missing ({ex.get('wrapper')})"
                elif ex.get("pty_modules") is False:
                    mark, detail = "[!!]", f"CLI found but: {ex.get('note')}"
        else:
            mark, detail = "[--]", f"not installed  ->  {b['install']}"
        lines.append(f"  {mark} {name:<13} {detail}")
        lines.append(f"       auth: {b['auth_hint']}")
    ad = report["agents_dir"]
    lines += [
        "",
        f"agents   : {'[OK]' if ad['found'] else '[--]'} {ad.get('path')}  "
        f"({ad.get('agent_count', 0)} agent definitions)",
    ]
    bg = report["billing_guard"]
    if bg["openai_api_key_present"]:
        lines.append("billing  : OPENAI_API_KEY is set - "
                     + ("guard ACTIVE (stripped for codex children)" if bg["guard_active"]
                        else "guard DISABLED (SUBAGENTS_ALLOW_OPENAI_KEY=1)"))
    lines += [
        "",
        f"usable backends: {', '.join(report['usable_backends']) or 'NONE'}",
        "verdict: " + ("[OK] ready - dispatch with --agent/--prompt/--cwd"
                       if report["ok"] else
                       "[!!] no usable backend - install and authenticate at least one CLI above"),
    ]
    return "\n".join(lines)
