"""Per-CLI command argument construction.

Holds the static knowledge of how each backend CLI is invoked: base flags,
permission-level mapping, system-prompt injection mechanism. The dispatcher
:func:`build_invocation_args` returns ``(command, args, env_override)``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace

from _loader import DEFAULT_PERMISSION

# cursor-agent exposes no machine-readable model list and no floating alias like
# claude's `opus`/`sonnet`, so its default is pinned here. This is the SINGLE
# bump-point when a newer Composer ships (also surfaced by --list-models).
CURSOR_DEFAULT_MODEL = "composer-2.5"


@dataclass(frozen=True)
class AgentInvocation:
    """A single sub-agent invocation request."""

    cli: str
    prompt: str
    cwd: str
    system_context: str = ""
    agent_file: str | None = None
    permission: str = DEFAULT_PERMISSION
    model: str | None = None
    effort: str | None = None          # reasoning effort (claude only): low..max
    resume_id: str | None = None       # backend session/thread/chat id to resume
    resume_profile: str | None = None  # agy only: profile dir of the session to resume
    extra_args: tuple = ()             # arbitrary backend flags (agent `args:` frontmatter)
    base_url: str | None = None        # openai-compat only: resolved API base url
    api_key_env: str | None = None     # openai-compat only: env var holding the API key


# Short report-contract nudge appended to RESUME prompts. On resume the session
# already holds the full agent definition, so we do NOT re-inject it (that saving
# is the whole point) — this one line just keeps the contract on follow-ups.
_RESUME_REMINDER = (
    "\n\n[Reminder] End your reply with the exact 'Final report' block from your "
    "agent definition (every field present)."
)


def _resume_prompt(inv: AgentInvocation) -> str:
    """Prompt for a resume run: the raw task + a short contract reminder only."""
    return inv.prompt + _RESUME_REMINDER


def build_command(cli: str, prompt: str) -> tuple[str, list]:
    """Build the base command + base args (no permission/system-prompt yet)."""
    if cli == "codex":
        return "codex", ["exec", "--json", "--skip-git-repo-check", prompt]

    if cli == "claude":
        return "claude", ["--output-format", "stream-json", "--verbose", "-p", prompt]

    if cli == "gemini":
        # --skip-trust is required for headless runs in untrusted folders;
        # passing --cwd is itself a trust statement, and Gemini otherwise
        # downgrades the approval mode to "default" (interactive prompts)
        # which deadlocks here.
        return "gemini", ["--skip-trust", "--output-format", "stream-json", "-p", prompt]

    if cli == "cursor-agent":
        # API key is forwarded via CURSOR_API_KEY env (in build_invocation_args),
        # never via argv — argv would expose the secret in `ps` output.
        return "cursor-agent", ["--model", CURSOR_DEFAULT_MODEL, "--output-format", "json", "-p", prompt]

    if cli == "agy":
        # Antigravity (Google) CLI. Native Go binary; plain-text output.
        return "agy", ["--print", prompt]

    raise ValueError(f"Unknown CLI: {cli}")


_PERMISSION_MAPPING = {
    "codex": {
        "read-only": ["-s", "read-only"],
        "safe-edit": ["-s", "workspace-write", "-c", "approval_policy=never"],
        "yolo": ["--dangerously-bypass-approvals-and-sandbox"],
    },
    "claude": {
        "read-only": ["--permission-mode", "plan"],
        "safe-edit": ["--permission-mode", "acceptEdits"],
        "yolo": ["--dangerously-skip-permissions"],
    },
    "gemini": {
        "read-only": ["--approval-mode", "plan"],
        "safe-edit": ["--approval-mode", "auto_edit"],
        "yolo": ["-y"],
    },
    "cursor-agent": {
        "read-only": ["--mode", "plan"],
        "safe-edit": ["--trust"],
        "yolo": ["-f", "--trust"],
    },
    "agy": {
        "read-only": ["--sandbox"],
        "safe-edit": ["--dangerously-skip-permissions"],
        "yolo": ["--dangerously-skip-permissions"],
    },
}


def permission_flags(cli: str, permission: str) -> list:
    """Map permission level to CLI-specific flags. Fails fast on unknown values."""
    try:
        return list(_PERMISSION_MAPPING[cli][permission])
    except KeyError as e:
        raise ValueError(f"No permission mapping for cli={cli!r}, permission={permission!r}") from e


def _concatenated_prompt(inv: AgentInvocation) -> str:
    """The prompt for CLIs with no native system-prompt slot: the agent's system
    context concatenated ahead of the user task."""
    return f"[System Context]\n{inv.system_context}\n\n[User Prompt]\n{inv.prompt}"


def _concatenated_args(
    inv: AgentInvocation, perm_flags: list, env: dict | None
) -> tuple[str, list, dict | None]:
    """Fallback: concatenate system context into the user prompt argument.

    Used when a CLI lacks a native system-prompt mechanism we can target.
    """
    command, base_args = build_command(inv.cli, _concatenated_prompt(inv))
    return command, perm_flags + base_args, env


def _build_claude_args(inv: AgentInvocation) -> tuple[str, list, dict | None]:
    perm = permission_flags(inv.cli, inv.permission)
    model_flag = ["--model", inv.model] if inv.model else []
    effort_flag = ["--effort", inv.effort] if inv.effort else []

    if inv.resume_id:
        # Resume: the session already carries the agent definition, so we don't
        # re-inject system context — but permission flags DO still apply per call
        # (a resumed editing agent must keep its --dangerously-skip-permissions,
        # or it hangs on an approval prompt). Just point at the session + new task.
        command, base_args = build_command(inv.cli, _resume_prompt(inv))
        return (command,
                perm + model_flag + effort_flag + list(inv.extra_args)
                + ["--resume", inv.resume_id] + base_args,
                None)

    system_prompt = (
        f"cwd: {inv.cwd}\n\n{inv.system_context}\n\n"
        "Reminder before responding: your final message MUST end with the exact "
        "'Final report' block from your agent definition above, with every field "
        "present. Do not skip it, even for tiny or trivial tasks."
    )
    command, base_args = build_command(inv.cli, inv.prompt)
    return (command,
            perm + model_flag + effort_flag + list(inv.extra_args)
            + ["--append-system-prompt", system_prompt] + base_args,
            None)


def _build_gemini_args(inv: AgentInvocation) -> tuple[str, list, dict | None]:
    if inv.resume_id:
        # gemini's --resume takes an index/"latest", not a stable UUID, and
        # --session-id only seeds NEW sessions, so we can't reliably resume a
        # specific prior conversation headlessly. Fail loudly rather than silently
        # starting a fresh (context-less) session.
        raise ValueError("resume is not supported for the gemini backend")
    perm = permission_flags(inv.cli, inv.permission)
    model_flag = ["--model", inv.model] if inv.model else []
    if inv.agent_file:
        command, base_args = build_command(inv.cli, inv.prompt)
        return command, perm + model_flag + list(inv.extra_args) + base_args, {"GEMINI_SYSTEM_MD": inv.agent_file}
    return _concatenated_args(inv, perm + model_flag + list(inv.extra_args), env=None)


# Which env var flips each CLI from subscription (login) to metered API billing.
_API_KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "cursor-agent": "CURSOR_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def infer_billing(cli: str) -> dict:
    """Best-effort ``{source, note}`` — does this run draw from the vendor
    SUBSCRIPTION (CLI login) or metered API credits (an API key in the env)?
    Reflects summon's own env handling (codex has OPENAI_API_KEY stripped by
    default). Advisory only; the vendor's billing is the source of truth."""
    if cli == "openai-compat":
        return {"source": "api", "note": "OpenAI-compatible endpoint (API key / credits)"}
    if cli == "agy":
        return {"source": "subscription", "note": "Google login (no API-key path)"}
    if cli == "codex":
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("SUBAGENTS_ALLOW_OPENAI_KEY") == "1":
            return {"source": "api", "note": "OPENAI_API_KEY present (billing guard opted out)"}
        return {"source": "subscription", "note": "ChatGPT login (OPENAI_API_KEY stripped)"}
    key = _API_KEY_ENV.get(cli)
    if key and os.environ.get(key):
        return {"source": "api", "note": f"{key} set in env"}
    if key:
        return {"source": "subscription", "note": f"CLI login (no {key})"}
    return {"source": "unknown", "note": ""}


# --- Credit-only model guard (Fable) -----------------------------------------
# Some models are NOT covered by the vendor's flat subscription and bill account
# CREDIT (API-style) even through the subscription CLI. Fable (claude-fable-5)
# left the Claude Max subscription and is now credit-only. Mirroring the
# OPENAI_API_KEY guard, we DEFAULT to the latest subscription-covered model
# (Opus) and require an explicit opt-in to spend credit — so a `fable` dispatch
# never silently draws down credit. The API-key path (an openai-compat anthropic
# agent) is unaffected: that is metered by design.
_CREDIT_ONLY_MODELS = {"claude-fable-5"}
# The latest subscription-covered Opus, PINNED (not the `opus` alias). The alias
# currently LAGS — it resolves to claude-opus-4-7 while 4-8 is the latest — so a
# pin gives the actual latest (verified available on the CLI). Bump this line when
# a newer Opus ships. The credit-env strip still covers an `opus`-alias remap for
# any agent that uses the alias directly.
_OPUS_FALLBACK = "claude-opus-4-8"
# Flags that select a model — the guard scrubs credit-only values from any of
# these in an agent's `args:` passthrough (incl. --fallback-model, which Claude
# uses on primary-model overload).
_MODEL_FLAG_NAMES = ("--model", "-m", "--fallback-model")


def credit_spend_allowed() -> bool:
    """The operator opted in to spending account credit on a credit-only model."""
    return (os.environ.get("SUMMON_ALLOW_FABLE") == "1"
            or os.environ.get("SUMMON_ALLOW_CREDIT") == "1")


def resolve_billing_model(model: str | None, cli: str) -> tuple[str | None, str | None]:
    """The MODEL half of the credit-only guard. Returns ``(effective_model,
    fallback_note)`` — a credit-only model on the ``claude`` CLI falls back to
    Opus unless credit spend is authorized (None note when unchanged/authorized)."""
    if cli == "claude" and model in _CREDIT_ONLY_MODELS and not credit_spend_allowed():
        return _OPUS_FALLBACK, (
            f"{model} is no longer covered by the Claude Max subscription (it bills "
            f"account credit); summon fell back to Opus. To run it on credit set "
            f"SUMMON_ALLOW_FABLE=1, or use an ANTHROPIC_API_KEY (openai-compat) agent.")
    return model, None


def selects_credit_only(model: str | None, extra_args: list) -> bool:
    """Would this dispatch run a credit-only model, considering BOTH the model
    field AND a --model/-m/--fallback-model selector in ``args:``? Used for
    accurate billing/warning telemetry (Fable can be picked either way)."""
    if model in _CREDIT_ONLY_MODELS:
        return True
    ea = extra_args or []
    for i, a in enumerate(ea):
        if a in _MODEL_FLAG_NAMES and i + 1 < len(ea) and ea[i + 1] in _CREDIT_ONLY_MODELS:
            return True
        if "=" in a:
            k, v = a.split("=", 1)
            if k in _MODEL_FLAG_NAMES and v in _CREDIT_ONLY_MODELS:
                return True
    return False


def _scrub_credit_args(extra_args: list) -> tuple[list, bool]:
    """Drop credit-only model selections from an agent's `args:` passthrough
    (``--model``/``-m``/``--fallback-model`` in flag-value or ``flag=value``
    form). Returns ``(args, scrubbed?)``; the original list is returned unchanged
    when nothing matched."""
    if not extra_args:
        return extra_args, False
    out, scrubbed, i, n = [], False, 0, len(extra_args)
    while i < n:
        a = extra_args[i]
        if a in _MODEL_FLAG_NAMES and i + 1 < n and extra_args[i + 1] in _CREDIT_ONLY_MODELS:
            scrubbed = True
            i += 2
            continue
        if "=" in a:
            k, v = a.split("=", 1)
            if k in _MODEL_FLAG_NAMES and v in _CREDIT_ONLY_MODELS:
                scrubbed = True
                i += 1
                continue
        out.append(a)
        i += 1
    return (out, True) if scrubbed else (extra_args, False)


def _credit_env_override() -> dict:
    """Env keys to STRIP (value ``None``) from the claude child: any ANTHROPIC_*
    model-selection var whose value is a credit-only model, so an alias like
    ``opus`` can't be silently remapped to Fable (e.g. ANTHROPIC_DEFAULT_OPUS_MODEL,
    ANTHROPIC_MODEL)."""
    return {k: None for k, v in os.environ.items()
            if k.startswith("ANTHROPIC_") and "MODEL" in k and v in _CREDIT_ONLY_MODELS}


def apply_credit_guard(inv) -> tuple:
    """Full credit-only guard for a claude dispatch. Returns
    ``(guarded_inv, env_override, warnings)``. No-op for non-claude backends or
    when credit spend is authorized. Otherwise: substitute a credit-only model
    with Opus, scrub credit-only model flags from `args:`, strip ANTHROPIC_* env
    aliases that remap to one, and warn that a resume can't be re-pinned."""
    warnings: list = []
    if inv.cli != "claude" or credit_spend_allowed():
        return inv, {}, warnings
    model, note = resolve_billing_model(inv.model, inv.cli)
    if note:
        warnings.append(note)
    args, scrubbed = _scrub_credit_args(inv.extra_args)
    if scrubbed:
        warnings.append("summon stripped a credit-only model flag from this agent's `args:` "
                        "(it would have run Fable on account credit without opt-in)")
    env = _credit_env_override()
    if env:
        warnings.append(f"summon stripped env var(s) {sorted(env)} that remap a model alias "
                        "to a credit-only model for this run")
    if inv.resume_id and selects_credit_only(inv.model, inv.extra_args):
        warnings.append("resuming a claude session keeps its ORIGINAL model — summon cannot "
                        "re-pin it to Opus, so this Fable session bills account credit")
    if model != inv.model or args is not inv.extra_args:
        inv = replace(inv, model=model, extra_args=args)
    return inv, env, warnings


def _codex_env_override() -> dict | None:
    """Strip OPENAI_API_KEY from the child env so codex uses ChatGPT-subscription
    auth, never metered API billing (a stray key would silently flip delegations
    to paid API). Value None = "remove key" (see execute_agent's merge). Opt out
    with SUBAGENTS_ALLOW_OPENAI_KEY=1."""
    if os.environ.get("SUBAGENTS_ALLOW_OPENAI_KEY") == "1":
        return None
    return {"OPENAI_API_KEY": None}


def _build_codex_args(inv: AgentInvocation) -> tuple[str, list, dict | None]:
    perm = permission_flags(inv.cli, inv.permission)
    model_flag = ["-m", inv.model] if inv.model else []
    # Reasoning effort -> codex config override. gpt supports low|medium|high, so
    # clamp claude's xhigh/max down to high. Global `-c` flags precede the subcommand.
    effort_flag = []
    if inv.effort:
        _e = "high" if inv.effort in ("xhigh", "max") else inv.effort
        effort_flag = ["-c", f"model_reasoning_effort={_e}"]
    env = _codex_env_override()
    head = perm + model_flag + effort_flag + list(inv.extra_args)
    if inv.resume_id:
        # `codex exec resume <id>`: the thread holds the agent definition, so send
        # only the task + reminder (no [System Context] prefix). Permission/model
        # flags are global codex flags and still precede the subcommand.
        return "codex", head + [
            "exec", "resume", inv.resume_id, "--json", "--skip-git-repo-check",
            _resume_prompt(inv)], env
    command, base_args = build_command(inv.cli, _concatenated_prompt(inv))
    return command, head + base_args, env


def _build_cursor_args(inv: AgentInvocation) -> tuple[str, list, dict | None]:
    perm = permission_flags(inv.cli, inv.permission)
    # Forward CLI_API_KEY (skill contract) as CURSOR_API_KEY (cursor's native
    # env). Passing via env keeps the secret out of `ps` output.
    api_key = os.environ.get("CLI_API_KEY")
    env_override = {"CURSOR_API_KEY": api_key} if api_key else None
    model = inv.model or CURSOR_DEFAULT_MODEL
    if inv.resume_id:
        return "cursor-agent", perm + list(inv.extra_args) + [
            "--model", model, "--resume", inv.resume_id, "--output-format", "json",
            "-p", _resume_prompt(inv)], env_override
    return "cursor-agent", perm + list(inv.extra_args) + [
        "--model", model, "--output-format", "json", "-p", _concatenated_prompt(inv)], env_override


# --- Antigravity (agy) headless one-shot support -------------------------------
# agy has no working non-interactive pipe mode: --print renders only to a TTY,
# so a piped stdout captures nothing. We launch it under a ConPTY+pyte wrapper
# (captures the TTY-only "drip" output as clean text) inside a FRESH, token-locked,
# PER-INVOCATION profile (no MCP servers, no inherited memory) so agy behaves as a
# deterministic one-shot instead of a roaming, memory-carrying interactive agent.
# See agy_pty_pyte.py.
#
# Each call gets its OWN throwaway profile dir, so (a) no prior-session state can
# leak in (isolation holds) and (b) concurrent agy sub-agents never collide on
# trust scope or the conversation DB (concurrency-safe). agy leaves a short-lived
# sidecar that holds its conversation SQLite DB open for ~1-3 min after the main
# process exits, so we NEVER reuse or scrub a profile in place — old run dirs are
# cleaned best-effort on a later call once that sidecar has released them.

# The MINIMAL set copied from the real agy config into the fresh per-call
# profile so agy can run headless without prompting: OAuth creds + account,
# install id + integrity (agy refuses to start otherwise), and the two
# operational files it needs to skip interactive gates — `state.json`
# (onboarding/first-run flags) and `trustedFolders.json` (workspace trust, so
# agy doesn't block on a "trust this folder?" prompt). What is deliberately NOT
# copied: conversation history / the SQLite DB, MCP server config, and any
# roaming memory — so the isolation claim is "no inherited conversation or MCP
# state", not "an empty $HOME".
_AGY_AUTH_FILES = (
    "oauth_creds.json", "google_accounts.json", "installation_id",
    "state.json", "trustedFolders.json", "extension_integrity.json",
)
_AGY_MAX_PROMPT = 28000  # one argv token; stay under Windows CreateProcess ~32 KB
_AGY_RUN_TTL_SEC = 900   # don't clean run dirs younger than this (may be in use)


def _has_pty_modules(python: str) -> bool:
    """Can this interpreter import the bundled wrapper's deps (pywinpty + pyte)?
    Quick probe subprocess; fail-soft False on any error."""
    try:
        r = subprocess.run([python, "-c", "import pyte, winpty"],
                           capture_output=True, timeout=8, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _agy_python() -> str:
    """An interpreter that can run the agy PTY wrapper.

    $AGY_PTY_PYTHON always wins. With a CUSTOM wrapper ($AGY_PTY_WRAPPER) we
    trust the caller's environment and use the current interpreter. For the
    bundled ConPTY wrapper, PROBE candidates for pywinpty+pyte (current
    interpreter first, then well-known installs, then PATH) instead of assuming
    a hardcoded path. If none has the modules, fall back to the current
    interpreter — the wrapper then exits 127 with a clear install message that
    the executor surfaces as a CLI error.
    """
    env = os.environ.get("AGY_PTY_PYTHON")
    if env and os.path.isfile(env):
        return env
    if os.environ.get("AGY_PTY_WRAPPER"):
        return sys.executable
    candidates = [sys.executable, r"C:\python313\python.exe", r"C:\python312\python.exe",
                  shutil.which("python"), shutil.which("py")]
    for c in candidates:
        if c and os.path.isfile(c) and _has_pty_modules(c):
            return c
    return sys.executable


def _agy_wrapper() -> str:
    """Path to the PTY wrapper script. The bundled wrapper is ConPTY (Windows).

    On POSIX there is no bundled wrapper yet: fail fast with a clear message
    (set $AGY_PTY_WRAPPER to a PTY-capture script to bring your own). Checked
    here — the first agy-specific step — so the error precedes profile setup.
    """
    override = os.environ.get("AGY_PTY_WRAPPER")
    if override:
        return override
    if os.name != "nt":
        raise ValueError(
            "agy backend: the bundled ConPTY wrapper is Windows-only. On this OS set "
            "AGY_PTY_WRAPPER to a PTY-capture wrapper for `agy --print` (agy has no "
            "working pipe mode), or use another backend (claude/codex/cursor/gemini).")
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy_pty_pyte.py")
    if os.path.isfile(here):  # bundled beside the scripts (public installs)
        return here
    return os.path.join(os.path.expanduser("~"), ".agents", "scripts", "agy_pty_pyte.py")


def _agy_cleanup_old_runs(runs_dir: str, deadline_sec: float | None = None) -> None:
    """Best-effort removal of prior per-invocation profiles.

    Each profile carries a ``.summon_expiry`` timestamp (its OWN deadline +
    sidecar margin); a dir is reaped only once past its own expiry, so a SHORT
    call's cleanup can't delete a concurrent LONG call's still-valid profile.
    Dirs without a marker (legacy/partial) fall back to mtime + a TTL sized by
    this call's deadline. Correctness never depends on this — every run uses a
    brand-new dir regardless of whether old ones were cleaned.
    """
    try:
        names = os.listdir(runs_dir)
    except OSError:
        return
    if deadline_sec is None:
        try:
            deadline_sec = float(os.environ.get("AGY_PTY_DEADLINE", "300"))
        except ValueError:
            deadline_sec = 300.0
    ttl = max(_AGY_RUN_TTL_SEC, deadline_sec * 2 + 300)
    now = time.time()
    cutoff = now - ttl
    for name in names:
        p = os.path.join(runs_dir, name)
        try:
            if not os.path.isdir(p):
                continue
            # Honor the dir's OWN expiry marker if present (concurrency-safe).
            try:
                with open(os.path.join(p, ".summon_expiry"), encoding="utf-8") as _fh:
                    if now < float(_fh.read().strip()):
                        continue  # still within its own validity window
            except (OSError, ValueError):
                pass
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _agy_lock_down(prof: str) -> None:
    """Restrict a fresh per-invocation profile (holds copied OAuth tokens) to the
    current user only.

    Windows: strips inherited ACEs and grants owner-only full control (icacls).
    POSIX: chmod 700 dirs / 600 files — the 700 root blocks all other-user
    traversal, so files agy writes later are unreachable regardless of umask.
    Fails closed on both: if permissions cannot be applied we raise rather than
    run agy with readable tokens.
    """
    if os.name != "nt":
        try:
            os.chmod(prof, 0o700)
            for root, dnames, fnames in os.walk(prof):
                for d in dnames:
                    os.chmod(os.path.join(root, d), 0o700)
                for f in fnames:
                    os.chmod(os.path.join(root, f), 0o600)
        except OSError as e:
            raise ValueError(f"agy profile: failed to secure token permissions on {prof}: {e}") from e
        return
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        raise ValueError("agy profile: cannot determine current user for ACL lockdown")
    fails = []
    # Pass 1: strip inherited ACEs and give the owner an EFFECTIVE full-control ACE
    # on every dir AND file. NOTE: an (OI)(CI) ACE applied to a *file* is
    # inherit-only and leaves the file with an empty DACL, so files need PLAIN ":F".
    r = subprocess.run(
        ["icacls", prof, "/inheritance:r", "/grant:r", f"{user}:F", "/T", "/C"],
        capture_output=True, text=True)
    if r.returncode != 0:
        fails.append((r.stderr or r.stdout).strip())
    # Pass 2: make EVERY directory's ACE inheritable (not just the root) so files
    # agy writes later anywhere in the profile — e.g. a refreshed token, or new
    # conversation/brain files — are owner-only too, not SYSTEM/Administrators.
    dirs = [prof]
    for root, dnames, _files in os.walk(prof):
        dirs.extend(os.path.join(root, d) for d in dnames)
    for d in dirs:
        r = subprocess.run(
            ["icacls", d, "/grant:r", f"{user}:(OI)(CI)F"], capture_output=True, text=True)
        if r.returncode != 0:
            fails.append((r.stderr or r.stdout).strip())
    if fails:
        raise ValueError(f"agy profile: failed to secure token ACLs on {prof}: " + " | ".join(fails))


def _ensure_agy_profile(cwd: str, deadline_sec: float = 300.0) -> str:
    """Create a FRESH, token-locked, isolated agy home dir for ONE invocation.

    Copies only the auth needed to reach the model (fresh from ~/.gemini each
    time, so any upstream re-auth is picked up) plus minimal settings with no
    mcpServers and trust scoped to ``cwd``. No MCP, no inherited brain/
    conversations -> a clean, deterministic one-shot. Returns the new profile
    dir (passed to agy as USERPROFILE/HOME).
    """
    base = os.environ.get("AGY_HEADLESS_PROFILE") or os.path.join(
        os.path.expanduser("~"), ".agents", "state", "agy-headless-profile")
    real = os.path.join(os.path.expanduser("~"), ".gemini")
    runs = os.path.join(base, "runs")
    os.makedirs(runs, exist_ok=True)
    _agy_cleanup_old_runs(runs, deadline_sec)

    # mkdtemp gives an ATOMICALLY-unique dir (no <pid>-<ms> collision when two
    # same-process calls land in the same millisecond).
    prof = tempfile.mkdtemp(prefix="run-", dir=runs)
    # Self-describe when THIS profile becomes safe to reap (own deadline + sidecar
    # margin). A concurrent SHORT call's cleanup reads this and leaves a long
    # call's still-valid profile alone — the reaping no longer depends on the
    # cleaner's own deadline.
    try:
        with open(os.path.join(prof, ".summon_expiry"), "w", encoding="utf-8") as _fh:
            _fh.write(repr(time.time() + deadline_sec * 2 + 300))
    except OSError:
        pass
    try:
        g = os.path.join(prof, ".gemini")
        acli = os.path.join(g, "antigravity-cli")
        os.makedirs(acli, exist_ok=True)
        # Lock the EMPTY skeleton FIRST: every dir gets an inheritable owner-only
        # ACE, so the tokens copied next inherit owner-only with no window where a
        # secret sits on disk under default/inherited ACLs (TOCTOU-safe).
        _agy_lock_down(prof)

        for fn in _AGY_AUTH_FILES:
            src = os.path.join(real, fn)
            if os.path.isfile(src):
                dst = os.path.join(g, fn)
                shutil.copy2(src, dst)
                if os.name != "nt":
                    # copy2 preserves SOURCE modes; re-tighten so a 0644 source
                    # can't yield a world-readable token copy (dirs are already
                    # 0700, this makes the file contract explicit too).
                    os.chmod(dst, 0o600)
            elif fn == "oauth_creds.json":  # required -> fail closed
                raise ValueError(f"agy profile: required auth file missing: {src}")

        # Windows agy expects backslash paths in trustedWorkspaces; POSIX must
        # keep forward slashes (a blanket replace would corrupt /tmp/x -> \tmp\x).
        trusted = cwd.replace("/", "\\") if os.name == "nt" else cwd
        with open(os.path.join(g, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)
        with open(os.path.join(acli, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "toolPermission": "always-proceed",
                "trustedWorkspaces": [trusted],
                "mcpServers": {},
            }, fh, indent=2)
        if os.name != "nt":
            os.chmod(os.path.join(g, "settings.json"), 0o600)
            os.chmod(os.path.join(acli, "settings.json"), 0o600)
    except ValueError:
        shutil.rmtree(prof, ignore_errors=True)  # never leave a partial profile
        raise
    except OSError as e:
        # Convert raw FS/icacls errors to ValueError so the broker returns a
        # clean JSON error instead of crashing (run_subagent catches ValueError).
        shutil.rmtree(prof, ignore_errors=True)
        raise ValueError(f"agy profile: build failed: {type(e).__name__}: {e}") from e
    return prof


def _resume_agy_profile(profile: str | None) -> str:
    """Validate + refresh a profile dir being resumed. Extends its mtime so the
    TTL cleanup won't reap it mid-use. Fails closed if it's gone (its short-lived
    sidecar and conversation DB may already have been cleaned).

    CAVEAT: agy leaves a sidecar holding the conversation SQLite DB open for ~1-3
    min after a run. A resume dispatched inside that window can occasionally hit
    'database is locked'. The dispatcher surfaces that as a normal error — the
    caller should retry after a short delay (verified working in practice)."""
    if not profile or not os.path.isdir(os.path.join(profile, ".gemini")):
        raise ValueError(
            "agy resume: profile dir missing or expired "
            f"({profile!r}); start a fresh run instead of resuming.")
    try:
        os.utime(profile, None)  # bump mtime -> TTL cleanup leaves it alone
    except OSError:
        pass
    return profile


def _build_agy_args(inv: AgentInvocation, timeout_ms: int | None = None
                    ) -> tuple[str, list, dict | None]:
    # Effective wrapper deadline in seconds: the real request if provided (don't
    # floor to int — keep sub-second precision), else the env/default. Used both
    # for the wrapper (AGY_PTY_DEADLINE) and for sizing the profile-TTL cleanup.
    if timeout_ms:
        deadline_sec = max(1.0, timeout_ms / 1000)
    else:
        try:
            deadline_sec = float(os.environ.get("AGY_PTY_DEADLINE", "300"))
        except ValueError:
            deadline_sec = 300.0
    wrapper = _agy_wrapper()  # FIRST: fails fast on POSIX before any profile is built
    perm = permission_flags(inv.cli, inv.permission)  # --dangerously-skip-permissions
    # Optional model pin from agent frontmatter (`model:`). agy accepts display
    # names ("Claude Opus 4.6 (Thinking)") or slugs ("gemini-3.1-pro"); default
    # if unset is "Gemini 3.5 Flash (Medium)". See `agy models`.
    model_flag = ["--model", inv.model] if inv.model else []

    if inv.resume_id:
        # Resume: reuse the SAME profile (its conversation DB holds the session)
        # and continue the most-recent conversation. No fresh profile, no scrub —
        # this is the opt-in exception to per-call isolation.
        profile = _resume_agy_profile(inv.resume_profile)
        prompt = _resume_prompt(inv)
        cont = ["--continue"]
    else:
        prompt = (
            f"[System Context]\n{inv.system_context}\n\n"
            f"[User Prompt]\n{inv.prompt}\n\n"
            "[Reminder] Your final message MUST end with the exact 'Final report' "
            "block from your agent definition above, with every field present "
            "(use \"none\" where it does not apply). Do not skip it, even for tiny tasks."
        )
        profile = _ensure_agy_profile(inv.cwd, deadline_sec)
        cont = []

    if len(prompt) > _AGY_MAX_PROMPT:
        raise ValueError(
            f"agy prompt is {len(prompt)} chars (> {_AGY_MAX_PROMPT}); it is passed as one "
            "Windows argv token and would risk CreateProcess truncation. Shorten the "
            "agent definition or task prompt.")

    # Launch the wrapper, NOT agy directly. Arg order matters: agy's --print
    # consumes the NEXT token as the prompt, so flags (perm, --continue, --model)
    # precede it.
    args = [wrapper, *perm, *inv.extra_args, *cont, *model_flag, "--print", prompt]
    env = {
        "USERPROFILE": profile,
        "HOME": profile,
        # The real request deadline (agy was previously pinned to 300s here so a
        # longer --timeout was truncated). Kept as a string the wrapper float()s.
        "AGY_PTY_DEADLINE": repr(deadline_sec),
        "AGY_PTY_QUIET": os.environ.get("AGY_PTY_QUIET", "20"),
    }
    return _agy_python(), args, env


# --- Backend registry --------------------------------------------------------
# The ONE place that knows every backend and how it runs. Two kinds:
#   "subprocess" — build() returns (command, args, env_override) for the executor
#                  to spawn (claude/codex/cursor/gemini/agy).
#   "api"        — the executor calls the backend's own request function instead
#                  of spawning a process (openai-compat: an HTTP call).
# Adding a backend = add ONE entry here (+ its build/call fn). ``side_effects``
# flags a build that mutates the filesystem (agy creates a per-call profile), so
# callers like --dry-run know not to invoke build() as a pure preview.
# See references/adding-a-backend.md.


def _api_call(inv: AgentInvocation, timeout_ms: int) -> dict:
    from _apibackend import call as _call   # lazy: keep _builder import-light
    return _call(inv, timeout_ms)


BACKENDS: dict = {
    "claude":       {"kind": "subprocess", "build": _build_claude_args},
    "codex":        {"kind": "subprocess", "build": _build_codex_args},
    "cursor-agent": {"kind": "subprocess", "build": _build_cursor_args},
    "gemini":       {"kind": "subprocess", "build": _build_gemini_args},
    "agy":          {"kind": "subprocess", "build": _build_agy_args, "side_effects": True},
    "openai-compat": {"kind": "api", "call": _api_call},
}
BACKEND_CLIS = tuple(BACKENDS)

# Back-compat alias (was the subprocess-only dispatch table).
_BUILDERS = {k: v["build"] for k, v in BACKENDS.items() if v["kind"] == "subprocess"}


def backend_kind(cli: str) -> str | None:
    """'subprocess' | 'api' | None (unknown backend)."""
    b = BACKENDS.get(cli)
    return b["kind"] if b else None


def build_invocation_args(inv: AgentInvocation, timeout_ms: int | None = None
                          ) -> tuple[str, list, dict | None]:
    """Dispatch to a SUBPROCESS backend's argument builder.

    Returns ``(command, args, env_override_or_None)``. Raises ValueError for an
    unknown backend or an api-kind backend (which has no argv — the executor
    calls it directly; see ``backend_kind``). ``timeout_ms`` is threaded to agy
    so its wrapper deadline AND its profile-TTL cleanup (which runs at build
    time) reflect the real request; the other builders don't need it.
    """
    b = BACKENDS.get(inv.cli)
    if b is None:
        raise ValueError(f"Unknown backend: {inv.cli}")
    if b["kind"] != "subprocess":
        raise ValueError(f"backend {inv.cli!r} is {b['kind']}-kind; no argv to build")
    # Credit-only guard (Fable): substitute the model, scrub credit-only model
    # flags from `args:`, and strip ANTHROPIC_* alias remaps HERE so real dispatch
    # and --dry-run enforce it identically. The executor surfaces the notes/billing.
    inv, credit_env, _ = apply_credit_guard(inv)
    if inv.cli == "agy":
        cmd, args, env = _build_agy_args(inv, timeout_ms)
    else:
        cmd, args, env = b["build"](inv)
    if credit_env:
        env = {**(env or {}), **credit_env}
    return cmd, args, env
