"""Per-CLI command argument construction.

Holds the static knowledge of how each backend CLI is invoked: base flags,
permission-level mapping, system-prompt injection mechanism. The dispatcher
:func:`build_invocation_args` returns ``(command, args, env_override)``.
"""

from __future__ import annotations

import json
import getpass
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace

from _loader import DEFAULT_PERMISSION
from _spawn import run_flags

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
    # "subprocess" (default: spawn the CLI one-shot) or "acp" (run the turn over
    # the Agent Client Protocol, where the backend supports it natively). The
    # executor dispatches on this; see _acpbackend.
    transport: str = "subprocess"
    model: str | None = None
    effort: str | None = None          # reasoning effort (claude only): low..max
    resume_id: str | None = None       # backend session/thread/chat id to resume
    resume_profile: str | None = None  # agy only: profile dir of the session to resume
    extra_args: tuple = ()             # arbitrary backend flags (agent `args:` frontmatter)
    base_url: str | None = None        # openai-compat only: resolved API base url
    api_key_env: str | None = None     # openai-compat only: env var holding the API key
    allow_payg: bool = False           # byteplus-coding: per-run consent for PAYG fallback
    # agy only: the account digest the REQUEST IDENTITY recorded, checked against the bytes
    # actually copied into the isolated profile so a swap in between cannot produce a result
    # stamped under the wrong account.
    agy_account_sha256: str | None = None
    # True when the request identity inspected the agy account (so a None digest above means
    # "absent when fingerprinted", a state to attest, not "legacy caller, nothing to check").
    agy_account_checked: bool = False
    # True when SUMMON imposed `permission` rather than the caller declaring it: a
    # --gate-with adjudicator, a --max-permission clamp, a contract-repair resume. Such a
    # tier is a privilege REDUCTION and cannot be waived by the unenforced-read-only opt-in,
    # which exists to waive a tier you chose for yourself. Carried on the invocation because
    # the executor is where the refusal happens and the distinction is invisible by then.
    permission_forced: bool = False


# Short report-contract nudge appended to RESUME prompts. On resume the session
# already holds the full agent definition, so we do NOT re-inject it (that saving
# is the whole point) — this one line just keeps the contract on follow-ups.
_RESUME_REMINDER = (
    "\n\n[Reminder] End your reply with the exact 'Final report' block from your "
    "agent definition (every field present). Include LEFT_BEHIND: none, or describe "
    "every resource you created and intentionally left for the caller to decide about."
)


_ENVIRONMENT_HANDOFF_CONTEXT = """## Environment handoff (required)
Before your final report, account for everything YOU created outside the requested
deliverables that still exists when you finish. This includes temporary files or paths,
generated processes, listeners or web servers, VMs, containers, images, volumes, networks,
and local or cloud service state. Do not stop or delete a resource merely to make this
report empty: unless the task explicitly directs cleanup, the caller decides what to keep.

Add `LEFT_BEHIND: none` to the Final report if nothing remains. Otherwise, use
`LEFT_BEHIND:` to name each remaining resource, its location or identifier, its current
state, why it remains, and the safe stop/removal action. Never include secret values,
credentials, or tokens in that field.
"""


def environment_handoff_context(system_context: str) -> str:
    """Append the universal, caller-owned environment handoff contract.

    It belongs in the system context rather than a backend-specific argv builder so every
    initial dispatch, including project-local agents, receives the same non-destructive
    obligation. Resumes get the short equivalent in ``_RESUME_REMINDER`` above because the
    original session already holds the complete context.
    """
    return f"{system_context.rstrip()}\n\n{_ENVIRONMENT_HANDOFF_CONTEXT}"


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

    if cli == "kimi":
        # Kimi Code has a native JSONL one-shot mode.  Do not add --auto,
        # --yolo, or --plan here: its current CLI rejects those with --prompt.
        return "kimi", ["--output-format", "stream-json", "--prompt", prompt]

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
        # agy HAS NO ENFORCEABLE READ-ONLY TIER. Measured over five canaries on
        # 2026-07-25/26: `--sandbox` restricts TERMINAL operations only; `--mode plan` does
        # not withhold the file tools; and withholding `--add-dir` only breaks RELATIVE
        # paths -- canary 5 had a declared-read-only agent read a secret and create a file
        # by ABSOLUTE path, both confirmed on disk. These flags are sent as defence in
        # depth and because a future agy may honour them, but summon relies on NONE of
        # them and no longer claims the tier: an agy read-only dispatch FAILS CLOSED
        # (see readonly_unenforceable_error).
        "read-only": ["--mode", "plan", "--sandbox"],
        "safe-edit": ["--dangerously-skip-permissions"],
        "yolo": ["--dangerously-skip-permissions"],
    },
    "kimi": {
        # Kimi's non-interactive --prompt mode auto-handles regular tool calls
        # and rejects --plan/--yolo/--auto.  Summon therefore permits only an
        # explicitly declared yolo invocation (enforced below); these remain
        # empty so the CLI contract stays valid.
        "read-only": [],
        "safe-edit": [],
        "yolo": [],
    },
}


# Authority ORDER, lowest first. Used only to clamp downward; nothing raises a tier.
_PERMISSION_ORDER = ("read-only", "safe-edit", "yolo")


def clamp_permission(declared: str, ceiling: str | None) -> str:
    """The lesser of the agent's declared permission and a caller-supplied ceiling.

    One-directional BY CONSTRUCTION: the result is never higher than `declared`, so a
    caller cannot use this to escalate an agent. That is the whole reason summon has no
    general ``--permission`` override -- an override would let any caller hand any agent
    full bypass, which is a larger hole than the one it would close.
    """
    if not ceiling:
        return declared
    try:
        return (declared if _PERMISSION_ORDER.index(declared)
                <= _PERMISSION_ORDER.index(ceiling) else ceiling)
    except ValueError:          # unknown tier: fail SAFE, keep the declared value
        return declared


def permission_flags(cli: str, permission: str) -> list:
    """Map permission level to CLI-specific flags. Fails fast on unknown values."""
    try:
        return list(_PERMISSION_MAPPING[cli][permission])
    except KeyError as e:
        raise ValueError(f"No permission mapping for cli={cli!r}, permission={permission!r}") from e


def effective_permission(cli: str, permission: str) -> str:
    """The tier the backend ACTUALLY enforces, which is not always the declared one.

    A security census built from declared `permission:` strings UNDERSTATES real capability
    on agy, where `safe-edit` maps to the same full bypass as `yolo` -- and a census that
    undercounts capability is worse than no census, because it is trusted (field report,
    2026-07-28). Every consumer that summarises capability across a roster must ask this,
    not the frontmatter.
    """
    if cli == "agy":
        if permission == "safe-edit":
            return "yolo"          # identical flags; the label is the only difference
        if permission == "read-only":
            return "unenforceable"  # refused at dispatch unless explicitly waived
    if cli == "kimi":
        if permission == "read-only":
            return "unenforceable"
        if permission == "safe-edit":
            return "yolo"
    return permission


def roster_permission_lint(agents: list) -> list:
    """Roster-wide tier/backend mismatches, so they surface BEFORE a dispatch.

    summon refuses an unenforceable tier correctly, but only when the agent is dispatched.
    A roster maintained as a controlled artifact can therefore sit for months holding
    definitions whose declared intent the backend can never honour -- two of them named
    `reviewer` in the report that prompted this. Per-dispatch refusal is right and arrives
    too late for whoever maintains the roster.

    Each entry: {agent, cli, declared, effective, severity, note}.
    """
    out = []
    for a in agents or []:
        name = a.get("name")
        cli = a.get("run_agent") or a.get("cli")
        declared = a.get("permission")
        if not cli or not declared:
            continue
        eff = effective_permission(cli, declared)
        if eff == declared:
            continue
        if eff == "unenforceable":
            out.append({
                "agent": name, "cli": cli, "declared": declared, "effective": eff,
                "severity": "error",
                "note": (f"{cli} cannot enforce '{declared}'; this definition is refused at "
                         f"dispatch unless the operator explicitly waives the tier. The "
                         f"roster currently misrepresents what this agent is allowed to do."),
            })
        else:
            out.append({
                "agent": name, "cli": cli, "declared": declared, "effective": eff,
                "severity": "warning",
                "note": (f"on {cli}, '{declared}' runs with the SAME full bypass as "
                         f"'{eff}'. A capability census reading the declared string "
                         f"understates this agent -- report the effective tier."),
            })
    return out


def agy_permission_warning(cli: str, permission: str) -> str | None:
    """The agy safe-edit surprise, surfaced per dispatch: agy has no
    workspace-write tier, so 'safe-edit' maps to the same full bypass as
    'yolo'. One shared helper so the real envelope and --dry-run emit the
    identical warning (and emit it exactly once each)."""
    if cli == "agy" and permission == "safe-edit":
        return ("agy has no workspace-write tier: permission 'safe-edit' runs with a "
                "FULL permission bypass (--dangerously-skip-permissions), identical to "
                "'yolo'. Constrain the agent by instruction and only point it at repos "
                "you trust.")
    return None


# agy is a multi-step agent (it plans, calls tools, then answers), so it routinely needs
# minutes where a single-shot backend needs seconds. Measured 2026-07-25: canaries at 420s
# completed; a nested dispatch given 180s timed out mid-work and reported exit 124. That
# failure reads as "agy is broken" rather than "the budget was short", which is why it is
# worth a warning rather than silence.
#
# The number is 420s because that is the SMALLEST budget an agy dispatch has actually been
# observed to complete under. It was 300s, which no measurement supported -- 180s failed and
# 420s worked, and picking the midpoint dressed an interpolation up as evidence. Erring high
# only costs an advisory line; erring low is the silence that lets a short clock masquerade
# as a broken backend. Raise the threshold only by lowering a MEASURED completion.
_AGY_MIN_ADVISED_TIMEOUT_MS = 420_000


# Flags that MOVE THE PERMISSION BOUNDARY, per backend. Derived from each backend's own
# permission mapping plus its documented bypass switches, because an agent definition's
# `args:` are appended AFTER the flags summon computes -- so a later flag wins.
#
# This started as an agy-only list, which was fixing the instance instead of the class:
# cross-vendor review reproduced the same escalation on every other backend, e.g. a
# read-only Claude definition producing `--permission-mode plan --dangerously-skip-permissions`
# while the envelope still reported `permission: read-only`. `--max-permission` and
# `--gate-with` already drop extra_args wholesale for exactly this reason; a DIRECTLY
# declared tier did not, which left the permission mapping advisory against the roster.
_BOUNDARY_FLAGS = {
    "claude": ("--permission-mode", "--dangerously-skip-permissions"),
    "codex": ("-s", "--sandbox", "--dangerously-bypass-approvals-and-sandbox",
              "--full-auto", "--yolo"),
    "cursor-agent": ("--mode", "--trust", "-f", "--force"),
    "gemini": ("--approval-mode", "-y", "--yolo"),
    "agy": ("--add-dir", "--mode", "--sandbox", "--dangerously-skip-permissions", "--yolo"),
    "kimi": ("--auto", "--yolo", "--plan", "--session", "-S", "--continue", "-c",
             "--agent", "--agent-file", "--add-dir", "--skills-dir"),
}
# Flags that consume the NEXT token as their value; dropping the flag must drop the value
# too, or the bare value becomes a stray positional argument.
_BOUNDARY_TAKES_VALUE = {"--permission-mode", "-s", "--sandbox", "--mode", "--approval-mode",
                         "--add-dir", "--session", "-S", "--agent", "--agent-file", "--skills-dir"}
# codex configures approval policy through `-c key=value`, so the KEY decides, not the flag.
_CODEX_CONFIG_KEYS = ("approval_policy", "sandbox_mode", "sandbox_permissions")


def strip_boundary_flags(cli: str, extra_args) -> list:
    """Remove agent-supplied flags that would move the permission boundary summon set.

    Not a general sanitiser: it drops exactly the flags that change the tier (plus their
    values) and leaves every other passthrough argument alone. The point is to keep the
    declared tier honest, not to police what an agent author may configure.

    KNOWN LIMITATION, resolved toward safety: a token is judged by its spelling, not its
    position, so `--log-file --mode` (where `--mode` is the log FILENAME) loses both tokens.
    Dropping a legitimate value is recoverable and loud; leaving an agent able to grant
    itself a full bypass is neither.
    """
    flags = _BOUNDARY_FLAGS.get(cli)
    if not flags:
        return list(extra_args)
    out, skip = [], False
    it = list(extra_args)
    for i, a in enumerate(it):
        if skip:
            skip = False
            continue
        base = a.split("=", 1)[0]
        # Go-style single-dash long options: agy's parser accepts `-add-dir=...`, and
        # matching only the double-dash spelling was a sanitiser the target walked past.
        if base.startswith("-") and not base.startswith("--") and len(base) > 2:
            base = "-" + base
        if a == "--":
            # A terminator stops the backend's parser before summon's own trailing flags,
            # which on agy 1.1.7 dropped it into interactive behaviour until the timeout.
            continue
        if base in flags:
            skip = ("=" not in a) and base in _BOUNDARY_TAKES_VALUE
            continue
        if cli == "codex" and base == "-c":
            # `-c` is codex's GENERAL config flag -- summon itself uses
            # `-c model_reasoning_effort=high` for --effort -- so it cannot be stripped
            # wholesale. Only a boundary KEY matters. Dropping every `-c` broke an existing
            # test that asserts ordinary config reaches argv, which is the test doing its
            # job: a sanitiser that eats legitimate configuration is a bug, not caution.
            nxt = it[i + 1] if i + 1 < len(it) else ""
            if any(str(nxt).startswith(k + "=") for k in _CODEX_CONFIG_KEYS):
                skip = True
                continue
        if cli == "codex" and "=" in a and any(
                a.startswith(k + "=") for k in _CODEX_CONFIG_KEYS):
            continue                        # a bare `approval_policy=never` value token
        out.append(a)
    return out


def _strip_agy_boundary_flags(extra_args) -> list:
    """Back-compat shim: the agy-specific entry point now delegates to the shared one."""
    return strip_boundary_flags("agy", extra_args)


_UNENFORCED_RO_OPT_IN = "SUMMON_ALLOW_UNENFORCED_READONLY"


def readonly_unenforceable_error(cli: str, permission: str, *,
                                 forced: bool = False) -> str | None:
    """Refuse a dispatch whose declared permission tier the backend cannot enforce.

    summon FAILS CLOSED here for the same reason --gate-with does: a permission tier is a
    promise to the caller, and a promise the backend will not keep is worse than no
    promise, because it is acted on. agy and kimi are such backends today: agy
    (opt-in waivable) cannot contain even read-only, and kimi (never waivable)
    cannot enforce anything below yolo in non-interactive mode.

    Measured across five canaries (2026-07-25/26). `--sandbox` restricts TERMINAL
    operations only. `--mode plan` does not withhold the file tools. Withholding
    `--add-dir` -- which summon shipped briefly as a containment fix -- only breaks
    RELATIVE paths: canary 5 gave a DECLARED read-only agy agent two absolute paths, and it
    read a secret file back verbatim and created a new one, both confirmed on disk.

    The opt-in exists because refusing outright would strand anyone who knowingly wants
    agy's reasoning on a throwaway checkout. It makes the tier ADVISORY, not enforced, and
    says so.
    """
    if cli == "kimi":
        if permission == "read-only":
            return ("kimi's non-interactive --prompt mode cannot be combined with its "
                    "read-only plan mode, so summon refuses this dispatch rather than "
                    "claim a boundary Kimi cannot apply. Use an enforcing backend for "
                    "reviews, or a deliberately declared kimi yolo agent in a trusted "
                    "worktree for full-authority work.")
        if permission == "safe-edit":
            return ("kimi's non-interactive --prompt mode auto-handles tool calls but has "
                    "no workspace-write sandbox. Declare permission: yolo explicitly for "
                    "a trusted, isolated worktree; summon refuses the misleading safe-edit "
                    "label.")
        return None
    if cli != "agy" or permission != "read-only":
        return None
    if forced:
        # A tier SUMMON imposed -- a --gate-with adjudicator, a --max-permission clamp, a
        # contract-repair resume -- is a privilege REDUCTION, and an ambient environment
        # variable must not lift it. The opt-in waives a tier the CALLER declared for their
        # own dispatch; it is not a global "read-only is optional" switch.
        #
        # This is the concrete hazard cross-vendor review demonstrated: the variable is
        # inherited by every backend, background, manifest and council child, so setting it
        # for one dispatch silently authorized advisory-only gates and clamped members
        # underneath it. A gate that can be waived by the environment it runs in is not a
        # gate.
        return ("agy cannot enforce read-only, and this dispatch was FORCED to read-only by "
                "summon (a gate, a --max-permission clamp, or a contract-repair resume). "
                + _UNENFORCED_RO_OPT_IN + " does not apply here: it waives a tier you chose, "
                "not one summon imposed to reduce privilege. Use a backend that enforces "
                "read-only for this role.")
    if os.environ.get(_UNENFORCED_RO_OPT_IN) == "1":
        return None
    return ("agy cannot enforce the read-only tier, so summon refuses this dispatch rather "
            "than imply a boundary that does not exist. Measured: a declared read-only agy "
            "agent read a secret file and created another by ABSOLUTE path, with --sandbox "
            "and --mode plan both in force and the workspace withheld. agy at any tier can "
            "read and write anything your user account can.\n"
            "Choose deliberately:\n"
            "  - use a backend that enforces read-only (claude, codex, cursor-agent); or\n"
            "  - declare safe-edit if write access is acceptable (on agy that IS a full "
            "bypass -- point it only at a repo you can afford to have written to); or\n"
            "  - set " + _UNENFORCED_RO_OPT_IN + "=1 to dispatch anyway, accepting that "
            "read-only is ADVISORY for agy and enforced by nothing.")


def agy_readonly_workspace_warning(cli: str, permission: str) -> str | None:
    """Warn on the OPT-IN path that read-only is advisory only.

    Reached only when the caller set the opt-in, so the dispatch proceeds -- but it must
    not proceed quietly. An earlier version of this warning claimed agy "cannot read your
    repository", which canary 5 disproved: it read a secret by absolute path.
    """
    if cli != "agy" or permission != "read-only":
        return None
    if os.environ.get(_UNENFORCED_RO_OPT_IN) != "1":
        return None
    return ("read-only is ADVISORY for agy and enforced by nothing: it can read and write "
            "any path your user account can, whatever this tier says. You set "
            + _UNENFORCED_RO_OPT_IN + "=1, so summon dispatched anyway. Treat the result as "
            "having had full filesystem access.")


def agy_timeout_warning(cli: str, timeout_ms: int | None) -> str | None:
    """Warn when an agy dispatch is given a budget it will probably overrun."""
    if cli != "agy" or not timeout_ms or timeout_ms >= _AGY_MIN_ADVISED_TIMEOUT_MS:
        return None
    return (f"agy was given {int(timeout_ms / 1000)}s. It is a MULTI-STEP agent and "
            f"routinely needs longer: a measured dispatch at 180s timed out mid-work, and "
            f"{int(_AGY_MIN_ADVISED_TIMEOUT_MS / 1000)}s is the smallest budget one has been "
            f"observed to COMPLETE under. A short clock reports exit 124, which reads as a "
            f"broken backend rather than a budget you set. Raise --timeout, or expect a "
            f"partial.")


# Windows caps an entire command line at 32767 UTF-16 code units (CreateProcess,
# including the terminating null), measured on the SERIALISED line -- subprocess quotes and
# escapes arguments, so the raw character sum is not the number that matters. POSIX instead
# caps each SINGLE argument at MAX_ARG_STRLEN = 131072 BYTES on Linux, and caps arguments
# plus environment together at ARG_MAX. Measured 2026-07-25 on Windows: a 20k-char prompt
# dispatched fine, 31k and 34k both failed -- and CreateProcess reports the overflow as
# ERROR_FILE_NOT_FOUND, which Python raises as FileNotFoundError, which summon reported as
# "CLI not found: ...node.EXE". That sends you to debug an install that was never broken.
# A margin is left for the exe path and the flags, which are part of the same budget.
_ARGV_TOTAL_LIMIT_NT = 32767
_ARGV_SINGLE_LIMIT_POSIX = 131072


def _utf8(s: str) -> bytes:
    """Encode for measurement, never raising.

    Arguments can carry lone surrogates -- from a prompt decoded with surrogateescape, or a
    filesystem path on either platform. `surrogateescape` handles the low half but raises on
    a lone HIGH surrogate, so measurement fell over on input it was supposed to describe.
    """
    try:
        return s.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return s.encode("utf-8", "surrogatepass")


def argv_length_error(cli: str, command: str, args: list, env=None) -> str | None:
    """Reject an over-long command line BEFORE spawning, with the real reason.

    The prompt is passed via argv by every CLI backend, so a large prompt (a diff, a
    packet, a pasted file) can exceed the OS limit. --prompt-file does NOT avoid this: it
    is a quoting and encoding convenience, and the content still reaches the backend on
    the command line.
    """
    if os.name == "nt":
        # Measure what CreateProcess ACTUALLY receives, not a character sum of the parts.
        # subprocess serialises argv with list2cmdline, which adds quotes around anything
        # containing a space and DOUBLES embedded backslashes before a quote -- so a raw sum
        # undercounts badly. Cross-vendor review measured `\\"` * 10000 at 20010 by the old
        # count and 40011 UTF-16 units once serialised; both passed preflight and then failed
        # CreateProcess as WinError 206, which is the exact misdiagnosis this check exists to
        # prevent. Windows counts UTF-16 code units, so a non-BMP character costs TWO, and
        # the terminating NUL counts inside the limit.
        line = subprocess.list2cmdline([command, *args])
        # surrogatepass: a prompt decoded from an odd byte stream can carry a LONE
        # SURROGATE, and a plain .encode() raises UnicodeEncodeError -- turning a preflight
        # meant to produce a clear message into an uncaught crash on the dispatch path.
        # Measuring is best-effort; refusing to measure must never be fatal.
        total = len(line.encode("utf-16-le", "surrogatepass")) // 2 + 1   # +1 for the NUL
        if total > _ARGV_TOTAL_LIMIT_NT:
            return (f"the assembled command line is {total} characters, over the Windows "
                    f"limit of {_ARGV_TOTAL_LIMIT_NT}. The prompt reaches {cli} through "
                    f"argv, so a large prompt overflows it -- and Windows reports that "
                    f"overflow as a MISSING FILE, which is why this used to surface as "
                    f"'CLI not found'. --prompt-file does not help: it is a quoting "
                    f"convenience and the content still goes on the command line. Shorten "
                    f"the prompt, or write the material to a file under --cwd and ask the "
                    f"agent to READ it (a repo-capable backend will).")
        return None
    # BYTES, not characters: execve counts encoded bytes, so 70k accented characters is
    # 140k bytes and sailed past a character comparison (measured under WSL, E2BIG).
    blobs = [_utf8(a) for a in args]
    longest = max((len(b) for b in blobs), default=0)
    # >=, not >: MAX_ARG_STRLEN COUNTS THE TERMINATING NUL, so 131072 bytes of payload is
    # already one over. Measured under WSL: 131071 spawned, 131072 failed with E2BIG.
    if longest >= _ARGV_SINGLE_LIMIT_POSIX:
        return (f"a single argument is {longest} bytes, over the {_ARGV_SINGLE_LIMIT_POSIX} "
                f"per-argument limit. The prompt reaches {cli} through argv. Shorten it, or "
                f"write the material to a file under --cwd and ask the agent to READ it.")
    # And the TOTAL, which counts the environment too: many medium arguments overflow
    # ARG_MAX without any single one being close to the per-argument cap (measured: 25 x
    # 100000 bytes passed preflight, then E2BIG). Read the real limit rather than assume;
    # fall back to the POSIX minimum guarantee if sysconf is unavailable.
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError, AttributeError):
        arg_max = 2 ** 21
    # BYTES of the environment that will ACTUALLY be passed, not characters of this
    # process's. Both were wrong: character counting under-measured multibyte values (22
    # values of 50k non-ASCII chars passed preflight, then /bin/true failed with E2BIG),
    # and reading os.environ measured a different environment from the one _merge_env hands
    # to Popen.
    _env = os.environ if env is None else env
    env_bytes = sum(len(_utf8(k)) + len(_utf8(v)) + 2 for k, v in _env.items())
    total = len(command.encode("utf-8", "surrogateescape")) + sum(len(b) + 1 for b in blobs)
    # A margin: the kernel also stores pointers and the auxiliary vector in this budget, and
    # the exact overhead is not knowable from here.
    if total + env_bytes > arg_max - 4096:
        return (f"the arguments and environment total {total + env_bytes} bytes, over this "
                f"system's ARG_MAX of {arg_max}. The prompt reaches {cli} through argv. "
                f"Shorten it, or write the material to a file under --cwd and ask the agent "
                f"to READ it.")
    return None


_FROZEN_BACKENDS = {
    "gemini": (
        "the `gemini` CLI backend is FROZEN: Google has stopped updating and supporting it, "
        "and Gemini Code Assist for individuals now rejects it outright "
        "(`IneligibleTierError: This client is no longer supported`). summon still dispatches "
        "to it so existing setups do not break mid-flight, but it is not being developed and "
        "will not be fixed if the vendor changes it again.\n"
        "For Gemini models use one of:\n"
        "  - `agy` (Antigravity), the supported Gemini path and summon's default for Gemini "
        "work; or\n"
        "  - `openai-compat` with a GEMINI_API_KEY, which bills the metered API and has no "
        "subscription-tier eligibility to lose.\n"
        "Run `doctor --probe` to see whether this account is still eligible at all."),
}


def frozen_backend_warning(cli: str) -> str | None:
    """A backend summon still supports but no longer recommends, and why.

    Frozen, not removed: breaking a working setup because the vendor stopped caring is the
    caller's decision to make, not summon's. But the status has to be visible ON THE
    DISPATCH -- a field report (2026-07-27) had an operator burn two dispatches on a backend
    whose vendor had already cut them off, because nothing said so until they ran a probe
    they had no reason to think they needed.
    """
    return _FROZEN_BACKENDS.get(cli)


def cursor_premium_agreement_warning(cli: str, model: str | None) -> str | None:
    """Cursor serves Fable only after a one-time data-handling agreement.

    The agreement is accepted in Cursor's own UI, per user -- summon can neither accept it
    on your behalf nor see whether you already have. Saying so at dispatch beats letting
    the run fail with a vendor error the caller then has to go and decode (reported
    2026-07-27 by an operator who had already accepted it, and wanted others warned).
    """
    if cli != "cursor-agent" or (model or "") not in _PREMIUM_MODELS:
        return None
    return (f"cursor serves {model} only after a ONE-TIME data-handling agreement accepted "
            f"in the Cursor UI. summon cannot accept it for you or check whether you have; "
            f"if this dispatch fails on a vendor policy error, that is the likely cause.")


def advisory_warnings(cli: str, permission: str, timeout_ms: int | None,
                      model: str | None = None,
                      extra_args: list | tuple = ()) -> list:
    """Every advisory warning a dispatch should carry, in ONE place.

    The real envelope and --dry-run each assembled this list themselves and had already
    drifted: --dry-run emitted the permission warning but neither the timeout nor the
    read-only-workspace one. That is backwards -- preflight is exactly where a short clock
    or an unreadable workspace is still free to fix. A guard test asserts the two paths
    stay identical.
    """
    notice_model = _selected_premium_model(cli, model, extra_args)
    return [w for w in (frozen_backend_warning(cli),
                        premium_model_warning(notice_model, cli),
                        cursor_premium_agreement_warning(cli, notice_model),
                        agy_permission_warning(cli, permission),
                        agy_readonly_workspace_warning(cli, permission),
                        agy_timeout_warning(cli, timeout_ms)) if w]


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
                perm + model_flag + effort_flag
                + strip_boundary_flags(inv.cli, inv.extra_args)
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
            perm + model_flag + effort_flag
            + strip_boundary_flags(inv.cli, inv.extra_args)
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
        return (command,
                perm + model_flag + strip_boundary_flags(inv.cli, inv.extra_args)
                + base_args,
                {"GEMINI_SYSTEM_MD": inv.agent_file})
    return _concatenated_args(
        inv, perm + model_flag + strip_boundary_flags(inv.cli, inv.extra_args), env=None)


def _build_kimi_args(inv: AgentInvocation) -> tuple[str, list, dict | None]:
    """Build Kimi Code's native JSONL one-shot invocation.

    Kimi 0.31's ``--prompt`` runner is deliberately not combined with session,
    agent, skills, or permission flags: the CLI rejects several combinations and
    they could silently replace Summon's declared authority boundary.  The child
    receives a per-call ``KIMI_CODE_HOME`` built below, with only its own auth and
    configuration material; notably no inherited MCP configuration or sessions.
    """
    if inv.resume_id:
        raise ValueError("resume is not supported for the kimi backend yet: its JSONL output does not provide a stable session id")
    model_flag = ["--model", inv.model] if inv.model else []
    profile = _ensure_kimi_profile()
    command, base_args = build_command(inv.cli, _concatenated_prompt(inv))
    return (command, model_flag + strip_boundary_flags(inv.cli, inv.extra_args) + base_args,
            {"KIMI_CODE_HOME": profile, "USERPROFILE": profile, "HOME": profile})


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
    if cli == "arkcli":
        return {"source": "subscription",
                "note": "arkcli +chat via profile store (dual-wire with openai-compat HTTP)"}
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


# --- Credit-only and plan-dependent model billing ----------------------------
# Keep the credit-only guard ready for a future model that is unconditionally
# billed to account credit through a subscription CLI. No current model meets
# that definition. Fable is different: Max/premium seats may use it for up to
# 50% of their regular weekly limit, while Pro/standard seats use usage credits
# from the first token. summon cannot inspect the seat type or remaining limit, so it runs
# the requested model and reports the billing source as unknown unless an API-key
# route indicates metered API billing.
_CREDIT_ONLY_MODELS: set[str] = set()
_PLAN_DEPENDENT_BILLING_MODELS = {"claude-fable-5"}
# Premium models deserve a pre-dispatch billing notice even when summon cannot
# determine which part of the vendor plan will pay for the run.
_PREMIUM_MODELS = {
    "claude-fable-5": (
        "plan-dependent billing: Max/premium seats may use Fable for up to 50% of "
        "their regular weekly limit at no extra cost, while Pro/standard seats use "
        "usage credits from the start; after that limit, eligible plans may continue "
        "on usage credits"
    ),
}
# The latest subscription-covered Opus, PINNED (not the `opus` alias). The alias
# LAGS BADLY — re-verified 2026-07-25: `--model opus` still served claude-opus-4-7,
# two releases behind claude-opus-5 — so a pin is what actually gets the latest
# (claude-opus-5 verified served on subscription billing via a live dispatch).
# Bump this line when a newer Opus ships. The credit-env strip still covers an
# `opus`-alias remap for any agent that uses the alias directly.
_OPUS_FALLBACK = "claude-opus-5"
# Flags that select a model — the guard scrubs credit-only values from any of
# these in an agent's `args:` passthrough (incl. --fallback-model, which Claude
# uses on primary-model overload).
_MODEL_FLAG_NAMES = ("--model", "-m", "--fallback-model")


def _selected_model_candidates(cli: str, model: str | None,
                               extra_args: list | tuple = ()) -> tuple:
    """Return models that can actually run after backend-specific argv precedence."""
    # Cursor appends its authoritative --model after passthrough args, so an
    # earlier selector cannot override the invocation model.
    if cli == "cursor-agent":
        return (model,) if model else ()
    # Claude appends passthrough args after the invocation model. The last
    # primary selector wins; its fallback is an independent candidate.
    if cli != "claude":
        return (model,) if model else ()
    primary, fallback = model, None
    ea = extra_args or []
    i = 0
    while i < len(ea):
        a = ea[i]
        key, value = None, None
        if a in _MODEL_FLAG_NAMES and i + 1 < len(ea):
            key, value = a, ea[i + 1]
            i += 2
        else:
            if "=" in a:
                maybe_key, maybe_value = a.split("=", 1)
                if maybe_key in _MODEL_FLAG_NAMES:
                    key, value = maybe_key, maybe_value
            i += 1
        if key in ("--model", "-m"):
            primary = value
        elif key == "--fallback-model":
            fallback = value
    return tuple(x for x in (primary, fallback) if x)


def _selected_premium_model(cli: str, model: str | None,
                            extra_args: list | tuple = ()) -> str | None:
    """Return a premium model that can run after backend argv precedence."""
    for candidate in _selected_model_candidates(cli, model, extra_args):
        if candidate in _PREMIUM_MODELS:
            return candidate
    return None


def credit_spend_allowed() -> bool:
    """The operator opted in to spending account credit on a credit-only model.

    `_CREDIT_ONLY_MODELS` is empty today. Fable is handled separately because its
    billing depends on the Claude plan and remaining allowance. The opt-in is still
    read and honoured so existing scripts keep working; it simply has nothing to
    authorize right now. Keep the machinery ready for the next credit-only model.
    """
    return (os.environ.get("SUMMON_ALLOW_FABLE") == "1"
            or os.environ.get("SUMMON_ALLOW_CREDIT") == "1")


def premium_model_warning(model: str | None, cli: str) -> str | None:
    """Pre-dispatch notice for a premium model with non-uniform billing."""
    if cli not in ("claude", "cursor-agent"):
        return None
    note = _PREMIUM_MODELS.get(model or "")
    if not note:
        return None
    if cli == "cursor-agent":
        return (f"{model} is a premium model with provider-specific billing and limits. "
                f"summon cannot inspect your Cursor plan and does not substitute the "
                f"requested model; check Cursor usage settings.")
    return (f"{model} has {note}. summon cannot inspect your plan or remaining allowance "
            f"and does not substitute the requested model; check Claude usage settings.")


def resolve_billing_model(model: str | None, cli: str) -> tuple[str | None, str | None]:
    """The MODEL half of the credit-only guard. Returns ``(effective_model,
    fallback_note)`` — a credit-only model on the ``claude`` CLI falls back to
    Opus unless credit spend is authorized (None note when unchanged/authorized)."""
    if cli == "claude" and model in _CREDIT_ONLY_MODELS and not credit_spend_allowed():
        return _OPUS_FALLBACK, (
            f"{model} is not covered by the Claude subscription (it bills account credit); "
            f"summon fell back to Opus. To run it on credit set SUMMON_ALLOW_CREDIT=1, or "
            f"use an ANTHROPIC_API_KEY (openai-compat) agent.")
    return model, None


def selects_credit_only(model: str | None, extra_args: list) -> bool:
    """Would this dispatch run a credit-only model, considering BOTH the model
    field AND a --model/-m/--fallback-model selector in ``args:``? Used for
    accurate billing/warning telemetry."""
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


def selects_plan_dependent_billing(model: str | None, extra_args: list | tuple,
                                   cli: str = "claude") -> bool:
    """Does the dispatch select a model whose billing depends on account state?"""
    return any(candidate in _PLAN_DEPENDENT_BILLING_MODELS
               for candidate in _selected_model_candidates(cli, model, extra_args))


def infer_dispatch_billing(cli: str, model: str | None = None,
                           extra_args: list | tuple = ()) -> dict:
    """Best-effort billing telemetry for the fully selected dispatch."""
    if cli == "claude" and selects_plan_dependent_billing(model, extra_args, cli):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"source": "api",
                    "note": "ANTHROPIC_API_KEY present "
                            "(predicted metered API billing; vendor authentication "
                            "remains authoritative)"}
        return {
            "source": "unknown",
            "note": (
                "Fable 5 billing is plan-dependent: included for up to 50% of regular "
                "weekly usage on Max/premium seats; usage credits on Pro/standard "
                "seats or after that limit. summon cannot inspect the plan or "
                "remaining usage"
            ),
        }
    return infer_billing(cli)


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
                        "(it could have spent account credit without opt-in)")
        if not model:
            # ...and PUT THE FALLBACK BACK. Scrubbing alone left the request with no model
            # at all, so the vendor's own default ran -- which prevents the unauthorized
            # credit spend but is not the documented behaviour ("substitute a credit-only
            # model with Opus"), and silently answers on a model nobody chose.
            model = _OPUS_FALLBACK
            warnings.append(f"summon pinned {_OPUS_FALLBACK} in its place, so this run does "
                            "not fall through to the backend's own default model")
    env = _credit_env_override()
    if env:
        warnings.append(f"summon stripped env var(s) {sorted(env)} that remap a model alias "
                        "to a credit-only model for this run")
    if inv.resume_id and selects_credit_only(inv.model, inv.extra_args):
        warnings.append("resuming a claude session keeps its ORIGINAL model — summon cannot "
                        "re-pin it to Opus or prove the original session's billing source")
    if model != inv.model or args is not inv.extra_args:
        inv = replace(inv, model=model, extra_args=args)
    return inv, env, warnings


def env_override_for(cli: str, allow_credit: bool = False) -> dict | None:
    """The environment delta summon applies to a child for `cli`, insofar as it depends only
    on the ENVIRONMENT (not on the resolved invocation).

    ONE definition, used both by the arg builders below and by the request identity. The
    identity previously hashed variables by vendor PREFIX, which is not the same thing as
    what the child receives: summon forwards `CLI_API_KEY` as `CURSOR_API_KEY` and strips
    `OPENAI_API_KEY` unless `SUBAGENTS_ALLOW_OPENAI_KEY=1`. So changing the Cursor key, or
    toggling whether Codex sees an API key at all, changed the child's effective auth while
    the fingerprint stayed equal -- and changing a STRIPPED `OPENAI_API_KEY` invalidated
    results whose execution was identical. Deriving both from this keeps them in step.
    """
    if cli == "claude" and (allow_credit or credit_spend_allowed()):
        # AUTHORIZED (by the flag OR by SUMMON_ALLOW_CREDIT/SUMMON_ALLOW_FABLE): the guard
        # strips nothing, so neither does the hashed view. Using only the flag left the env
        # var authorization path hashing the stripped environment while the authorized child
        # received the credit-only remap.
        return None
    if cli == "claude":
        # The credit guard STRIPS any ANTHROPIC_*MODEL* var naming a credit-only model, so
        # the child never sees it -- and the identity must not hash a value the child does
        # not get, or an unset-vs-set pair that dispatch treats identically forces a rerun.
        # (Only the env-derived half lives here; the model substitution depends on the
        # resolved invocation and is covered by the identity's own `model` field.)
        return _credit_env_override() or None
    if cli == "codex":
        return _codex_env_override()
    if cli == "cursor-agent":
        # Forwarded via env (not argv) to keep the secret out of `ps` output.
        api_key = os.environ.get("CLI_API_KEY")
        return {"CURSOR_API_KEY": api_key} if api_key else None
    return None


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
    env = env_override_for("codex")
    head = (perm + model_flag + effort_flag
            + strip_boundary_flags(inv.cli, inv.extra_args))
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
    env_override = env_override_for("cursor-agent")
    model = inv.model or CURSOR_DEFAULT_MODEL
    if inv.resume_id:
        return "cursor-agent", perm + strip_boundary_flags(inv.cli, inv.extra_args) + [
            "--model", model, "--resume", inv.resume_id, "--output-format", "json",
            "-p", _resume_prompt(inv)], env_override
    return "cursor-agent", perm + strip_boundary_flags(inv.cli, inv.extra_args) + [
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
_AGY_MODEL_ALIASES = {
    # Common user-provided aliases that should map to agy's display model names.
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "claude-opus-4.6-thinking": "Claude Opus 4.6 (Thinking)",
    "claude opus 4 6 thinking": "Claude Opus 4.6 (Thinking)",
    "claude opus 4.6 thinking": "Claude Opus 4.6 (Thinking)",
    "claude-opus-4.6-thinking": "Claude Opus 4.6 (Thinking)",
    "claude-sonnet-4-6-thinking": "Claude Sonnet 4.6 (Thinking)",
    "claude sonnet 4 6 thinking": "Claude Sonnet 4.6 (Thinking)",
    "claude sonnet 4.6 thinking": "Claude Sonnet 4.6 (Thinking)",
    "claude-sonnet-4.6-thinking": "Claude Sonnet 4.6 (Thinking)",
    "gpt oss 120b medium": "GPT-OSS 120B (Medium)",
    "gpt-oss 120b medium": "GPT-OSS 120B (Medium)",
    "gpt_oss_120b_medium": "GPT-OSS 120B (Medium)",
    "gptoss120bmedium": "GPT-OSS 120B (Medium)",
    "gemini 3 6 flash high": "Gemini 3.6 Flash (High)",
    "gemini 3 6 flash medium": "Gemini 3.6 Flash (Medium)",
    "gemini 3 6 flash low": "Gemini 3.6 Flash (Low)",
    "gemini 3 5 flash high": "Gemini 3.5 Flash (High)",
    "gemini 3 5 flash medium": "Gemini 3.5 Flash (Medium)",
    "gemini 3 5 flash low": "Gemini 3.5 Flash (Low)",
    "gemini 3 1 pro high": "Gemini 3.1 Pro (High)",
    "gemini 3 1 pro low": "Gemini 3.1 Pro (Low)",
}
_AGY_MAX_PROMPT = 28000  # one argv token; stay under Windows CreateProcess ~32 KB


def _normalize_agy_model(model: str | None) -> str | None:
    """Convert common aliases into canonical agy model display names."""
    if not model:
        return None
    trimmed = model.strip()
    if not trimmed:
        return trimmed
    key = re.sub(r"[^a-z0-9]+", " ", trimmed.lower()).strip()
    return _AGY_MODEL_ALIASES.get(key, trimmed)


def _reject_oversized_agy_prompt(prompt: str) -> None:
    """Raise if the assembled agy prompt cannot be passed as one argv token."""
    if len(prompt) > _AGY_MAX_PROMPT:
        raise ValueError(
            f"agy prompt is {len(prompt)} chars (> {_AGY_MAX_PROMPT}); it is passed as one "
            "Windows argv token and would risk CreateProcess truncation. Shorten the "
            "agent definition or task prompt, or write the material to a file under --cwd "
            "and ask the agent to READ it.")
_AGY_RUN_TTL_SEC = 900   # don't clean run dirs younger than this (may be in use)


def _has_pty_modules(python: str) -> bool:
    """Can this interpreter import the bundled wrapper's deps (pywinpty + pyte)?
    Quick probe subprocess; fail-soft False on any error."""
    try:
        r = subprocess.run([python, "-c", "import pyte, winpty"],
                           capture_output=True, timeout=8, stdin=subprocess.DEVNULL, **run_flags())
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


def _agy_wrapper_is_stream(wrapper: str | None = None) -> bool:
    """True when the resolved wrapper can emit `agy` stream-json events."""
    if wrapper is None:
        wrapper = _agy_wrapper()
    return os.path.basename(wrapper).lower() == "agy_stream_proxy.py"


def _agy_wrapper() -> str:
    """Path to the agy wrapper script.

    The default is the built-in stream-json proxy (`agy_stream_proxy.py`), which
    works cross-platform. A custom wrapper remains supported via
    ``$AGY_PTY_WRAPPER`` for operators that require alternate routing/observability.
    The error is raised here — before any profile work — so dispatch failures are
    reported before credentials are copied.
    """
    override = os.environ.get("AGY_PTY_WRAPPER")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    here_wrapper = os.path.join(here, "agy_stream_proxy.py")
    if os.path.isfile(here_wrapper):  # bundled beside the scripts (public installs)
        return here_wrapper
    legacy = os.path.join(here, "agy_pty_pyte.py")
    if os.path.isfile(legacy):
        return legacy
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
    # USERNAME can describe the interactive desktop account while this process
    # is running under an IDE sandbox/service identity.  ACLing a child profile
    # to that ambient name then locks the actual child out of its own tokens.
    # Ask Windows for the effective principal first; fall back only when that
    # diagnostic is unavailable (e.g. a constrained test environment).
    user = ""
    try:
        current = subprocess.run(["whoami"], capture_output=True, text=True,
                                 timeout=5, **run_flags())
        if current.returncode == 0:
            user = (current.stdout or "").strip().splitlines()[0].strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    user = user or os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            user = ""
    if not user:
        raise ValueError("agy profile: cannot determine current user for ACL lockdown")
    user = user.strip()
    if "\\" in user:
        # DOMAIN\user is a valid icacls principal; trim whitespace that can
        # bleed in from wrapper hosts.
        user = user.replace(" ", "")
    if not user or any(ch in user for ch in (";", "<", ">", "|", "&", "^", "\x00", "\"")):
        raise ValueError(f"agy profile: suspicious user principal for ACL lockdown: {user!r}")
    fails = []
    # Pass 1: strip inherited ACEs and give the owner an EFFECTIVE full-control ACE
    # on every dir AND file. NOTE: an (OI)(CI) ACE applied to a *file* is
    # inherit-only and leaves the file with an empty DACL, so files need PLAIN ":F".
    r = subprocess.run(
        ["icacls", prof, "/inheritance:r", "/grant:r", f"{user}:F", "/T", "/C"],
        capture_output=True, text=True, **run_flags())
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
            ["icacls", d, "/grant:r", f"{user}:(OI)(CI)F"], capture_output=True,
            text=True, **run_flags())
        if r.returncode != 0:
            fails.append((r.stderr or r.stdout).strip())
    if fails:
        raise ValueError(f"agy profile: failed to secure token ACLs on {prof}: " + " | ".join(fails))


def _attest_agy_profile(profile: str, expected: str | None,
                        checked: bool = False) -> None:
    """Refuse to dispatch unless the profile carries the account that was fingerprinted.

    The identity digests an account before dispatch; this checks what the child will
    ACTUALLY run under -- the freshly copied profile, or the resumed one. If they differ the
    account changed in between and the run would answer as one account while being stamped
    as another.

    `checked` distinguishes "the identity inspected the account" from a pre-0.10.2 caller
    that recorded nothing. When it did inspect, `expected is None` is a positive claim ("no
    account files then"), so an account that has since APPEARED (None -> a real digest) is a
    mismatch and is refused. Only an un-inspecting legacy caller is waved through.
    """
    if not checked and not expected:
        return
    actual = agy_profile_account_sha(profile)
    if actual != expected:
        raise ValueError(
            "agy account files changed between fingerprinting and dispatch "
            f"({expected} -> {actual}); re-run rather than record a result under the "
            "wrong account")


def agy_profile_account_sha(profile: str) -> str | None:
    """Digest of the account files as COPIED into `profile`.

    The identity digests the SOURCE files before dispatch; this digests what actually landed
    in the profile the child will run under. Comparing them closes the window in which the
    source could be swapped between the two -- otherwise one account's bytes could be
    dispatched and stamped with another's fingerprint. Same shape as the endpoint snapshot:
    describe what was used, not what was seen earlier.
    """
    import hashlib as _h
    h, seen_any = _h.sha256(b"summon-agy-account-v1"), False
    for fn in sorted(_AGY_AUTH_FILES):
        try:
            with open(os.path.join(profile, ".gemini", fn), "rb") as fh:
                sha = _h.sha256(fh.read()).hexdigest()
            seen_any = True
        except OSError:
            sha = ""
        h.update(b"\0" + fn.encode("utf-8") + b"\0" + sha.encode("utf-8"))
    return h.hexdigest()[:32] if seen_any else None


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


_KIMI_RUN_TTL_SEC = 24 * 3600


def _kimi_cleanup_old_runs(runs_dir: str) -> None:
    """Best-effort expiry for isolated Kimi profiles.

    Kimi retains credentials and may write sessions below ``KIMI_CODE_HOME``;
    profiles are never reused, and only old directories owned by this profile
    root are considered here.  Failure to remove one is safe and silent.

    The TTL must exceed the longest plausible dispatch (the default timeout is
    10 minutes but ``--timeout`` accepts hours): a previous 900s TTL let a
    second dispatch delete a concurrent run's live home mid-flight.  A run
    lasting longer than 24h can still lose its home to a concurrent sweep --
    accepted residual, since every run gets a fresh directory and correctness
    never depends on this cleanup.
    """
    cutoff = time.time() - _KIMI_RUN_TTL_SEC
    try:
        names = os.listdir(runs_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(runs_dir, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _ensure_kimi_profile() -> str:
    """Make a fresh, ACL-locked Kimi home with no inherited MCP/session state.

    The only copied material is the local Kimi configuration and credentials
    necessary for the installed CLI to authenticate.  ``mcp.json``, sessions,
    logs, history, and user skills intentionally remain outside the child home.
    """
    source = os.environ.get("KIMI_CODE_HOME") or os.path.join(os.path.expanduser("~"), ".kimi-code")
    config = os.path.join(source, "config.toml")
    credentials = os.path.join(source, "credentials")
    device_id = os.path.join(source, "device_id")
    if not os.path.isfile(config) and not os.path.isdir(credentials):
        raise ValueError("kimi profile: no Kimi configuration or credentials found; run `kimi login` first")
    # A desktop agent may run under a sandbox identity that cannot traverse a
    # profile directory another IDE created beneath ~/.agents.  The per-call
    # Kimi profile is deliberately disposable, so the current user's temp root
    # is both safer for cross-host operation and avoids a stale ACL becoming a
    # global outage.  Every child directory is still owner-ACL-locked below.
    base = os.environ.get("KIMI_HEADLESS_PROFILE") or os.path.join(
        tempfile.gettempdir(), "summon-kimi-headless-profile")
    runs = os.path.join(base, "runs")
    try:
        os.makedirs(runs, exist_ok=True)
        _kimi_cleanup_old_runs(runs)
        profile = tempfile.mkdtemp(prefix="run-", dir=runs)
        if os.path.isfile(config):
            shutil.copy2(config, os.path.join(profile, "config.toml"))
        if os.path.isdir(credentials):
            shutil.copytree(credentials, os.path.join(profile, "credentials"))
        # Kimi's managed OAuth client binds requests to this installation id;
        # without it a copied credential can authenticate locally yet fail the
        # provider handshake as a generic connection error.
        if os.path.isfile(device_id):
            shutil.copy2(device_id, os.path.join(profile, "device_id"))
        _agy_lock_down(profile)
        return profile
    except ValueError:
        if 'profile' in locals():
            shutil.rmtree(profile, ignore_errors=True)
        raise
    except OSError as e:
        if 'profile' in locals():
            shutil.rmtree(profile, ignore_errors=True)
        raise ValueError(f"kimi profile: build failed: {type(e).__name__}: {e}") from e


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
    # names ("Claude Opus 4.6 (Thinking)") or short aliases
    # ("claude-opus-4-6-thinking"). Default if unset is "Gemini 3.5 Flash
    # (Medium)". See `agy models`.
    model = _normalize_agy_model(inv.model)
    model_flag = ["--model", model] if model else []

    if inv.resume_id:
        # Resume: reuse the SAME profile (its conversation DB holds the session)
        # and continue the most-recent conversation. No fresh profile, no scrub —
        # this is the opt-in exception to per-call isolation.
        profile = _resume_agy_profile(inv.resume_profile)
        _attest_agy_profile(profile, getattr(inv, "agy_account_sha256", None),
                            getattr(inv, "agy_account_checked", False))
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
        # CHECK BEFORE BUILDING. The guard below used to run after _ensure_agy_profile, so a
        # prompt that was never going to dispatch still created a profile directory and
        # copied OAuth material into it before raising -- orphaning credentials for a run
        # that never happened. Same rule as the unenforceable-tier refusal: fail before side
        # effects, not after them.
        _reject_oversized_agy_prompt(prompt)
        profile = _ensure_agy_profile(inv.cwd, deadline_sec)
        _attest_agy_profile(profile, getattr(inv, "agy_account_sha256", None),
                            getattr(inv, "agy_account_checked", False))
        cont = []

    # the resume branch above builds no profile, so checking it here costs nothing
    _reject_oversized_agy_prompt(prompt)

    # Launch the wrapper, NOT agy directly. Arg order matters: agy's --print
    # consumes the NEXT token as the prompt, so flags (perm, --continue, --model)
    # precede it.
    # --add-dir puts the caller's --cwd INTO agy's workspace, which is what makes agy
    # usable for repo-grounded work: without it, relative paths resolve against a scratch
    # dir inside the isolated profile.
    #
    # It is passed at EVERY tier that can dispatch. Withholding it at read-only was tried
    # and reverted: it bought no containment at all, because agy reaches any absolute path
    # regardless (canary 5, 2026-07-26 -- a DECLARED read-only agent read a secret file and
    # created another, both by absolute path, both confirmed on disk). All it did was break
    # relative-path work while leaving a false impression of a boundary. Read-only agy now
    # fails closed instead, so the only dispatches reaching here already have write
    # authority, or an explicit and informed opt-in.
    add_dir = ["--add-dir", inv.cwd] if inv.cwd else []
    # An agent definition's own `args:` are appended AFTER the permission flags, so a
    # frontmatter `args: ["--add-dir", "/repo"]` or a second --mode silently rewrites the
    # tier summon just computed. --max-permission and --gate-with already drop extra_args
    # wholesale for exactly this reason; a DIRECTLY declared tier did not, which left the
    # permission mapping advisory against the roster. Drop only the flags that move the
    # boundary, so ordinary passthrough args keep working.
    extra = _strip_agy_boundary_flags(inv.extra_args)
    args = [wrapper, *perm, *add_dir, *extra, *cont, *model_flag, "--print", prompt]
    env = {
        "USERPROFILE": profile,
        "HOME": profile,
        # The real request deadline (agy was previously pinned to 300s here so a
        # longer --timeout was truncated). Kept as a string the wrapper float()s.
        "AGY_PTY_DEADLINE": repr(deadline_sec),
        "AGY_PTY_QUIET": os.environ.get("AGY_PTY_QUIET", "20"),
    }
    if _agy_wrapper_is_stream(wrapper):
        # The stream wrapper strips boundary flags to reduce accidental bypass risk when
        # used directly; summon already performs shared boundary stripping itself, so keep
        # our own scoped boundary flags intact by signalling explicit passthrough.
        env["AGY_STREAM_PROXY_ALLOW_BOUNDARY"] = "1"
    return _agy_python(), args, env


# --- Backend registry --------------------------------------------------------
# The ONE place that knows every backend and how it runs. Two kinds:
#   "subprocess" — build() returns (command, args, env_override) for the executor
#                  to spawn (claude/codex/cursor/gemini/agy).
#   "api"        — the executor calls the backend's own request function instead
#                  of spawning a process (openai-compat: an HTTP call).
# An optional "acp" key on a subprocess backend registers NATIVE Agent Client
# Protocol support: {"call": fn} invoked when inv.transport == "acp" (fallback,
# oversized prompts, or explicit opt-in — see _acpbackend).
# Adding a backend = add ONE entry here (+ its build/call fn). ``side_effects``
# flags a build that mutates the filesystem (agy creates a per-call profile), so
# callers like --dry-run know not to invoke build() as a pure preview.
# See references/adding-a-backend.md.


def _api_call(inv: AgentInvocation, timeout_ms: int) -> dict:
    from _apibackend import call as _call   # lazy: keep _builder import-light
    return _call(inv, timeout_ms)


def _acp_call(inv: AgentInvocation, timeout_ms: int) -> dict:
    from _acpbackend import call as _call    # lazy: keep _builder import-light
    return _call(inv, timeout_ms)


def _arkcli_call(inv: AgentInvocation, timeout_ms: int) -> dict:
    from _arkcli_backend import call as _call  # lazy: keep _builder import-light
    return _call(inv, timeout_ms)


BACKENDS: dict = {
    "claude":       {"kind": "subprocess", "build": _build_claude_args},
    "codex":        {"kind": "subprocess", "build": _build_codex_args},
    # The "acp" key marks NATIVE Agent Client Protocol support and provides the
    # call the executor uses when inv.transport == "acp". The backend keeps its
    # subprocess kind/build as the primary transport; ACP is the alternate.
    "cursor-agent": {"kind": "subprocess", "build": _build_cursor_args,
                     "acp": {"call": _acp_call}},
    "gemini":       {"kind": "subprocess", "build": _build_gemini_args,
                     "acp": {"call": _acp_call}},
    "kimi":         {"kind": "subprocess", "build": _build_kimi_args, "side_effects": True,
                     "acp": {"call": _acp_call}},
    "agy":          {"kind": "subprocess", "build": _build_agy_args, "side_effects": True},
    "arkcli":       {"kind": "api", "call": _arkcli_call},
    "openai-compat": {"kind": "api", "call": _api_call},
}
BACKEND_CLIS = tuple(BACKENDS)

# Back-compat alias (was the subprocess-only dispatch table).
_BUILDERS = {k: v["build"] for k, v in BACKENDS.items() if v["kind"] == "subprocess"}


def backend_kind(cli: str) -> str | None:
    """'subprocess' | 'api' | None (unknown backend)."""
    b = BACKENDS.get(cli)
    return b["kind"] if b else None


def supports_acp(cli: str) -> bool:
    """True when the backend has a NATIVE ACP entry point (the executor can
    dispatch inv.transport == "acp" to it)."""
    b = BACKENDS.get(cli)
    return bool(b and b.get("acp"))


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
