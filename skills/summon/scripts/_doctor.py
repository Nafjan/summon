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
_PROBE_TIMEOUT = 25   # opt-in eligibility probe: a minimal real call, short leash

# Known "the binary runs but the ACCOUNT/CLIENT cannot actually dispatch"
# signatures. A --version probe passes for ALL of these; only a real call (or this
# classifier over a failed dispatch's output) reveals them. Each maps to a concrete
# migration path so the user is never left with a bare error. Matched lower-cased.
_INELIGIBILITY_SIGNS = (
    ("ineligibletiererror", "gemini",
     "this Gemini client/account tier can no longer use Gemini Code Assist for "
     "individuals; use the metered API (set GEMINI_API_KEY) or the agy (Antigravity) "
     "backend instead"),
    ("no longer supported for gemini code assist", "gemini",
     "Gemini Code Assist for individuals is unavailable for this client; use "
     "GEMINI_API_KEY (metered) or the agy (Antigravity) backend"),
    ("this client is no longer supported", "gemini",
     "this Gemini client version/tier is no longer accepted; update the CLI, use "
     "GEMINI_API_KEY (metered), or switch to the agy (Antigravity) backend"),
)


def classify_ineligibility(text):
    """Detect a known account/client-eligibility failure in probe or dispatch
    output. Returns ``{eligible: False, backend, reason, guidance}`` or None when
    no known ineligibility signature is present. Pure + side-effect-free so it can
    run over a --doctor probe, a dispatch's output_tail, or a manual paste."""
    if not text or not isinstance(text, str):
        return None
    low = text.lower()
    for sign, backend, guidance in _INELIGIBILITY_SIGNS:
        if sign in low:
            return {"eligible": False, "backend": backend,
                    "reason": sign, "guidance": guidance}
    return None


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
        # Tiered eligibility (the field feedback): binary_ok is knowable cheaply;
        # auth_ok / account_eligible / model_access_verified are NOT (a passing
        # --version is not eligibility), so they stay None ("unverified") until the
        # opt-in live probe fills them. Being honest here is the whole point -- the
        # incident was a --version-OK Gemini that failed the first real dispatch.
        entry: dict = {"found": bool(path), "path": path, "binary_ok": bool(path),
                       "auth_ok": None, "account_eligible": None,
                       "model_access_verified": None}
        if path:
            entry["version"] = versions[name]
            entry["verified"] = versions[name] is not None
        else:
            entry["install"] = _BACKENDS[name]["install"]
        entry["auth_hint"] = _BACKENDS[name]["auth"]
        out[name] = entry
    return out


def _default_probe_runner(name: str, path: str) -> str | None:
    """A minimal real one-shot to reveal eligibility, reusing the dispatcher's own
    invocation builder so backend flags/permissions are correct. Returns the
    combined result/error text, or None if a cheap probe is not defined for this
    backend. Best-effort and fail-soft: any error becomes text for the classifier,
    never an exception."""
    try:
        from _builder import AgentInvocation
        from _executor import execute_agent
    except Exception:  # noqa: BLE001 - probing is best-effort
        return None
    try:
        inv = AgentInvocation(cli=name, prompt="ping", cwd=os.getcwd(),
                              system_context="", permission="read-only")
        resp = execute_agent(inv, timeout_ms=_PROBE_TIMEOUT * 1000)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    # Feed the classifier the richest failure text available.
    return " ".join(str(resp.get(k) or "") for k in ("error", "output_tail", "result"))


def _probe_eligibility(backends: dict, runner=None) -> None:
    """Opt-in: run a minimal live call per FOUND+verified backend and fill the
    eligibility tiers. A known ineligibility signature marks account_eligible
    False with guidance; a clean run marks the tiers True; an unclassifiable
    failure leaves them None (still unverified) but records the probe error."""
    runner = runner or _default_probe_runner
    for name, b in backends.items():
        if not (b.get("found") and b.get("verified")):
            continue
        text = runner(name, b.get("path"))
        b["probe_ran"] = True
        if text is None:
            b["probe_note"] = "no cheap eligibility probe defined for this backend"
            continue
        verdict = classify_ineligibility(text)
        if verdict is not None:
            b["auth_ok"] = b["account_eligible"] = False
            b["model_access_verified"] = False
            b["ineligible_reason"] = verdict["reason"]
            b["guidance"] = verdict["guidance"]
        elif "error" in text.lower() or "not found" in text.lower():
            b["probe_note"] = f"probe did not confirm eligibility: {text[:160]}"
        else:
            b["auth_ok"] = b["account_eligible"] = b["model_access_verified"] = True


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


def doctor(agents_dir: str | None = None, cwd: str | None = None,
           probe: bool = False, probe_runner=None) -> dict:
    backends = _check_backends()
    if probe:
        _probe_eligibility(backends, probe_runner)
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
    # Usable = on PATH + --version-verified, MINUS any the probe CONFIRMED
    # ineligible (account_eligible is False). Without a probe, eligibility is
    # merely UNVERIFIED (None) -- not disqualifying, since the backend may well
    # work; the render says so honestly rather than over-promising "[OK] ready".
    usable = [n for n, b in backends.items()
              if b["found"] and b.get("verified") and b.get("account_eligible") is not False]
    if "agy" in usable and not (
        report["agy_extras"].get("platform_ok")
        and report["agy_extras"].get("wrapper_found")
        and report["agy_extras"].get("pty_modules") in (True, None)
    ):
        usable.remove("agy")
    report["eligibility_probed"] = bool(probe)
    report["usable_backends"] = usable
    report["ineligible_backends"] = [n for n, b in backends.items()
                                     if b.get("account_eligible") is False]
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
            # Eligibility overlay for an otherwise-OK backend: a passing --version
            # is NOT eligibility. Confirmed-ineligible -> [!!] + migration guidance;
            # probe-verified -> [OK] eligible; unprobed -> [~?] eligibility unverified.
            if mark == "[OK]":
                if b.get("account_eligible") is False:
                    mark, detail = "[!!]", f"INELIGIBLE - {b.get('guidance', 'account/client not eligible')}"
                elif b.get("account_eligible") is True:
                    detail = f"{ver} - eligibility verified  ({b['path']})"
                else:
                    mark = "[~?]"
                    detail = f"{ver} - installed; eligibility unverified  ({b['path']})"
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
    ]
    if report.get("ineligible_backends"):
        lines.append(f"ineligible     : {', '.join(report['ineligible_backends'])} "
                     "(binary runs but the account/client can't dispatch - see guidance above)")
    if not report.get("eligibility_probed"):
        lines.append("note     : account eligibility is UNVERIFIED - a passing --version does "
                     "not prove a real dispatch will work; run `doctor --probe` to test a "
                     "minimal live call per backend (costs a tiny dispatch)")
    lines.append("verdict: " + ("[OK] ready - dispatch with --agent/--prompt/--cwd"
                                 if report["ok"] else
                                 "[!!] no usable backend - install and authenticate at least one CLI above"))
    return "\n".join(lines)
