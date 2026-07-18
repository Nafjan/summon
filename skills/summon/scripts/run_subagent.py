#!/usr/bin/env python3
"""run_subagent.py - Execute external CLI AIs as sub-agents.

Usage:
    scripts/run_subagent.py --agent <name> --prompt "<task>" --cwd <path>
    scripts/run_subagent.py --list

Supported CLIs: claude, cursor-agent, codex, gemini.

Environment:
    SUB_AGENTS_DIR: Override default agents directory ({cwd}/.agents/).
    CLI_API_KEY:    Forwarded as CURSOR_API_KEY to cursor-agent (env, never argv).

Implementation is split into sibling modules:
    _loader.py   - frontmatter parsing and agent discovery
    _resolver.py - CLI auto-detection
    _stream.py   - StreamProcessor (NDJSON parsing)
    _builder.py  - command/args construction per CLI
    _executor.py - subprocess driver and response shaping
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Fail early and clearly on an unsupported interpreter: summon targets Python
# 3.10+, and the sibling modules imported below use 3.10 syntax that would
# otherwise raise an opaque SyntaxError. This entry file stays parseable on
# 3.7-3.9 (the realistic "slightly old Python" range, thanks to `from __future__
# import annotations`) so this guard runs and emits a proper JSON envelope. EOL
# interpreters (2.x, 3.6) may still fail to parse first; that is out of scope.
if sys.version_info < (3, 10):
    _found = sys.version.split()[0]
    sys.stdout.write(json.dumps({
        "status": "error",
        "result": "",
        "exit_code": 1,
        "error": ("summon needs Python 3.10 or newer, but this interpreter is "
                  + _found + ". Install a newer Python (python.org or your package "
                  "manager), make it the `python` on your PATH, and retry. "
                  "Check with: python --version"),
        "setup": {"needs": "python>=3.10", "found": _found},
        "envelope": 1,
    }) + "\n")
    sys.exit(1)

# Ensure sibling modules import correctly when invoked via absolute path.
sys.path.insert(0, str(Path(__file__).parent))

from _builder import AgentInvocation  # noqa: E402
from _executor import ENVELOPE_VERSION as _ENVELOPE_VERSION  # noqa: E402
from _executor import execute_agent  # noqa: E402
from _loader import bundled_roster_dir, get_agents_dir, list_agents, load_agent  # noqa: E402
from _resolver import discover_models, resolve_cli  # noqa: E402

__version__ = "0.9.0"  # summon dispatcher version (see CHANGELOG.md)

# When set (a --background child), the final JSON goes to this file (atomically,
# via .tmp + rename) instead of stdout, so the parent can poll for completion.
_JOB_FILE: str | None = None


def _emit(obj: dict) -> None:
    """Write the response as JSON — to the job file (background) or stdout."""
    text = json.dumps(obj, ensure_ascii=False)
    if _JOB_FILE:
        tmp = _JOB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, _JOB_FILE)  # rename == atomic done-marker for the poller
    else:
        print(text)


def _print_error(error: str, exit_code: int = 1) -> None:
    _emit({"result": "", "exit_code": exit_code, "status": "error", "error": error})


def _preflight_backend(cli: str) -> dict | None:
    """Confirm the resolved backend is actually invocable before spawning it.

    A missing backend CLI becomes a clear setup message (the install and sign-in
    commands, plus which backends ARE ready so the agent can pivot) instead of a
    raw "command not found". Returns None when the CLI is on PATH -- real
    auth/runtime errors then surface in the normal envelope; for openai-compat,
    which has no binary and whose HTTP errors are already structured; or for an
    UNKNOWN backend name (e.g. a typo'd --cli), which is deferred to downstream
    validation to reject as unsupported rather than mislabel as "not installed".
    The `doctor` probe runs ONLY on the missing-backend path, so a normal
    dispatch pays just one PATH lookup.
    """
    if cli == "openai-compat" or shutil.which(cli):
        return None
    # Enrichment is best-effort: an incomplete install missing _doctor.py must
    # still yield a setup message, never an uncaught ImportError from this guard.
    try:
        from _doctor import _BACKENDS, doctor
    except Exception:  # noqa: BLE001
        _BACKENDS, doctor = {}, None
    # Only a real, supported backend earns "install it"; an unknown name defers to
    # downstream (build_invocation_args raises a proper "unknown backend" error).
    if _BACKENDS and cli not in _BACKENDS:
        return None
    hint = _BACKENDS.get(cli, {})
    usable = []
    if doctor is not None:
        try:
            usable = doctor(None, None).get("usable_backends", [])
        except Exception:  # noqa: BLE001 - a diagnostic must never mask the real failure
            usable = []
    msg = (f"The '{cli}' CLI isn't installed or isn't on your PATH, so this agent "
           f"can't run. Install it: {hint.get('install', 'see the vendor docs')}. "
           f"Then sign in: {hint.get('auth', 'log in to the CLI')}.")
    if usable:
        msg += (f" Backends ready right now: {', '.join(usable)} - or pick an agent on "
                "one of those (run the `list` command).")
    else:
        msg += " No backend is set up yet; run the `doctor` command for the full checklist."
    return {
        "status": "error",
        "result": "",
        "exit_code": 127,   # documented contract: 127 == CLI not found (SKILL.md)
        "error": msg,
        "cli": cli,
        "setup": {"backend": cli, "install": hint.get("install"),
                  "auth": hint.get("auth"), "usable_backends": usable},
        "warnings": [f"backend '{cli}' is not installed or not on PATH"],
        "envelope": _ENVELOPE_VERSION,
    }


_MEMORY_CAP = 8000  # chars; keeps the injected block well under agy's 28 KB argv guard


def _parse_timeout(value: str) -> int:
    """--timeout accepts bare milliseconds (backward compatible) or a human
    suffix: '90s', '10m', '600000ms'. Returns whole milliseconds (>= 1;
    fractional input rounds). Zero, negative, and non-finite durations are
    rejected here so they fail as argparse errors, not as instantly-killed
    agents or an OverflowError from the executor."""
    import math
    s = str(value).strip().lower()
    try:
        if s.endswith("ms"):
            ms = float(s[:-2])
        elif s.endswith("s"):
            ms = float(s[:-1]) * 1000
        elif s.endswith("m"):
            ms = float(s[:-1]) * 60_000
        else:
            ms = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --timeout {value!r}: use milliseconds or a suffix, e.g. 600000, 600s, 10m")
    if not math.isfinite(ms) or ms <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid --timeout {value!r}: must be a positive finite duration")
    return max(1, int(round(ms)))


def _apply_gemini_thinking(model: str, effort: str) -> str:
    """Map summon effort -> an agy Gemini thinking-mode suffix on the model name
    (agy's thinking is a model variant, not a flag). Strips any existing ``(...)``
    and applies the mapped level. NOTE: not every Gemini model has every level
    (e.g. 3.1 Pro has no Medium) — an unavailable variant will fail at agy, and the
    envelope's model.requested shows exactly what was asked so it's diagnosable."""
    suffix = {"low": "Low", "medium": "Medium", "high": "High",
              "xhigh": "High", "max": "High"}.get(effort)
    if not suffix:
        return model
    base = re.sub(r"\s*\([^)]*\)\s*$", "", model).strip()
    return f"{base} ({suffix})"


def _inject_memory(system_context: str, cwd: str) -> str:
    """Append {cwd}/.agents/memory.md to the agent's system context (capped)."""
    mem_path = os.path.join(cwd, ".agents", "memory.md")
    try:
        with open(mem_path, encoding="utf-8", errors="replace") as fh:
            mem = fh.read()
    except OSError:
        return system_context
    if not mem.strip():
        return system_context
    if len(mem) > _MEMORY_CAP:
        mem = mem[:_MEMORY_CAP] + "\n[memory truncated]"
    return f"{system_context}\n\n## Project memory (from .agents/memory.md)\n{mem}"


def _setup_worktree(cwd: str, name_arg: str, agent: str) -> dict:
    """Create an isolated git worktree so a (possibly parallel) editing agent
    can't collide with the main tree or other agents. Returns {path, branch}.
    Raises ValueError (surfaced as a clean JSON error) on any failure."""
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"--worktree requires a git repo; {cwd} is not inside one")
    repo = r.stdout.strip()
    # Auto-name includes a random suffix: two same-agent dispatches in the same
    # whole second would otherwise generate an identical name and one would fail
    # the "path already exists" guard below (a real collision under parallel fan-out).
    name = name_arg or f"{agent}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    # Reject path-traversal / dotfile names BEFORE building the path.
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name) or ".." in name:
        raise ValueError(f"invalid worktree name: {name!r} (letters/digits/._- , no '..', no leading dot)")
    branch = f"agents/{name}"
    wt = os.path.join(repo, ".claude", "worktrees", name)
    if os.path.exists(wt):
        raise ValueError(f"worktree path already exists: {wt}")
    # Don't clobber an existing branch: `-b` (not `-B`) fails if agents/<name>
    # already exists, so prior committed agent work is never force-reset away.
    if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{branch}"], capture_output=True, text=True).returncode == 0:
        raise ValueError(f"branch {branch} already exists; pick a different --worktree name "
                         "(its commits would otherwise be at risk)")
    r2 = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch, wt, "HEAD"],
                        capture_output=True, text=True)
    if r2.returncode != 0:
        raise ValueError(f"git worktree add failed: {(r2.stderr or r2.stdout).strip()}")
    # If --cwd was a SUBDIRECTORY of the repo, run inside the matching subdir of
    # the worktree to preserve the caller's intended working directory — BUT only
    # if it actually exists in the fresh checkout. An untracked/ignored/empty
    # subdir isn't checked out, so fall back to the worktree root rather than
    # handing the executor a nonexistent cwd (which would fail after we've already
    # created a persistent branch + worktree).
    rel = os.path.relpath(os.path.abspath(cwd), repo)
    sub = os.path.join(wt, rel)
    effective = sub if rel not in (".", "") and not rel.startswith("..") and os.path.isdir(sub) else wt
    return {"path": wt, "cwd": effective, "branch": branch}


def _child_argv(args: argparse.Namespace, result_file: str) -> list:
    """Reconstruct the child argv from PARSED args (not by filtering sys.argv,
    which would wrongly drop a token that is another option's *value*). Drops
    --background, adds --job-file."""
    # A file-sourced prompt is re-passed AS THE FILE (not the loaded text): the
    # child re-reads it, keeping the detached argv small and mojibake-free.
    if getattr(args, "prompt_file", None):
        out = ["--agent", args.agent, "--prompt-file", args.prompt_file, "--cwd", args.cwd]
    else:
        out = ["--agent", args.agent, "--prompt", args.prompt, "--cwd", args.cwd]
    if getattr(args, "allow_credit", False):
        out += ["--allow-credit"]
    if args.agents_dir:
        out += ["--agents-dir", args.agents_dir]
    if args.timeout:
        out += ["--timeout", str(args.timeout)]
    for flag, val in (("--cli", args.cli), ("--model", args.model), ("--effort", args.effort),
                      ("--resume", args.resume), ("--resume-profile", args.resume_profile),
                      ("--out", args.out), ("--json-schema", args.json_schema),
                      ("--debug-dir", args.debug_dir)):
        if val:
            out += [flag, val]
    if args.retries:
        out += ["--retries", str(args.retries)]
    if args.worktree is not None:
        out += [f"--worktree={args.worktree}"]  # =form is unambiguous for the bare case
    return out + ["--job-file", result_file]


def _spawn_background(args: argparse.Namespace) -> dict:
    """Re-exec this script detached, streaming its result to a job file. Returns
    the immediate {status, job_id, pid, result_file} handle (pid lets the poller
    tell 'still running' from 'died')."""
    jobs_dir = os.path.join(tempfile.gettempdir(), "subagents_jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    result_file = os.path.join(jobs_dir, f"{job_id}.json")
    cmd = [sys.executable, os.path.abspath(__file__), *_child_argv(args, result_file)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return {"status": "background", "job_id": job_id, "pid": proc.pid, "result_file": result_file}


# --- Provenance receipt --------------------------------------------------------
# Three divergent installed copies of this dispatcher (one hand-patched) all
# self-reported "0.9.0" while their scripts differed, making envelopes
# unattributable. Every dispatch envelope now carries the dispatcher's identity,
# the agent definition actually loaded, and the root prompt hash, so drift is
# diagnosable from any single envelope. Paths are absolute local-operator data
# (documented in SKILL.md); no prompt text or secrets, hashes only.

def _receipt_base() -> dict:
    """summon identity: available before ANY validation, so even a missing-agent
    or unknown-backend error names the install that produced it."""
    import hashlib
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    # One SHA over EVERY production module (incl. agy_pty_pyte.py -- drift lives
    # in siblings, not just the entry file). Length-prefixed framing so
    # (name, content) boundaries are unambiguous. test_discovery.py is excluded:
    # it never executes at dispatch time.
    for name in sorted(p.name for p in here.glob("*.py") if p.name != "test_discovery.py"):
        try:
            data = (here / name).read_bytes()
        except OSError:
            data = b""
        nb = name.encode("utf-8")
        h.update(len(nb).to_bytes(8, "big"))
        h.update(nb)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return {"summon": {"version": __version__,
                       "script": str(Path(__file__).resolve()),
                       "scripts_sha256": h.hexdigest()}}


def _receipt_agent(args: argparse.Namespace, agent_file: str) -> dict:
    """Agent-definition provenance. ``agents_dir`` records the ABSOLUTE roster
    directory the definition was ACTUALLY loaded from (a bundled-fallback hit
    must not record the project dir that failed the lookup)."""
    import hashlib
    try:
        fsha = hashlib.sha256(Path(agent_file).read_bytes()).hexdigest()
    except OSError:
        fsha = None  # hashed as read-back; a vanished file stays diagnosable
    served_dir = str(Path(agent_file).resolve().parent)
    _bundled = bundled_roster_dir()
    if _bundled and Path(served_dir) == Path(_bundled).resolve():
        source = "bundled"
    elif args.agents_dir:
        source = "explicit"
    elif os.environ.get("SUB_AGENTS_DIR"):
        source = "env"
    else:
        source = "project"
    return {"agent_def": {"file": agent_file, "sha256": fsha,
                          "agents_dir": served_dir, "source": source}}


def _receipt_prompt(prompt: str | None) -> dict:
    import hashlib
    if prompt is None:
        return {}
    return {"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


def _git_head(cwd: str) -> str | None:
    """HEAD of the EFFECTIVE dispatch cwd, captured BEFORE the agent runs (input
    provenance -- hence `git_head_before` in the envelope; an editing agent may
    commit during the run). Best-effort: None outside a repo or without git."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=2,
                           stdin=subprocess.DEVNULL)
        head = (r.stdout or "").strip()
        return head if r.returncode == 0 and head else None
    except (OSError, subprocess.SubprocessError):
        return None


# --- Fan-out mode flag matrix --------------------------------------------------
# The flags each fan-out mode actually CONSUMES. --manifest and --council branch
# out of main() before most dispatch flags are read, so anything outside these
# sets used to be SILENTLY IGNORED -- field case: a council run passed --out
# expecting an artifact and never got one. A flag that would be dropped is now
# rejected loudly BEFORE any paid dispatch. Whitelist, not blacklist: a flag
# added to the parser later is rejected-by-default in these modes until a mode
# explicitly supports it.
_MODE_FLAGS = {
    "manifest": {"manifest", "concurrency", "results_dir", "cwd", "agents_dir",
                 "retries", "job_file"},
    # Operation-level rows: a fresh council, a resume, and a read-only status
    # each consume a DIFFERENT set (v3.1). Changing members/rounds/question on a
    # resume would be a new run, so they are rejected there; status takes only
    # its id + where to look.
    "council": {"council", "question", "question_file", "members", "chairman",
                "rounds", "cwd", "agents_dir", "timeout", "out", "run_dir", "job_file"},
    "council-resume": {"council", "resume_run", "cwd", "agents_dir", "timeout",
                       "out", "run_dir", "job_file"},
    # Status takes ONLY its id, where to look, and the output format -- it never
    # dispatches, so it has no working directory (use --run-dir to point it).
    "council-status": {"council_status", "run_dir", "json", "job_file"},
}
_MODE_HINTS = {
    "manifest": ("Put per-job settings (model, effort, timeout, json_schema, "
                 "debug_dir, prompt/prompt_file) in the manifest's jobs/defaults; "
                 "per-job envelopes land under --results-dir."),
    "council": ("--out IS supported (the council envelope, checkpointed at each "
                "phase); member model/effort/permission come from each member "
                "agent's own definition."),
    "council-resume": ("a resume re-runs the SAME run: question, members, chairman, "
                       "and rounds come from the run's receipt.json, so they cannot "
                       "be changed here -- start a fresh council to change them."),
    "council-status": ("status is read-only: it takes only the run id, --run-dir, "
                       "and --json."),
}
_FLAG_NAMES = {"sets": "--set"}  # dests whose flag spelling isn't dest.replace('_','-')
_TOKEN_DESTS = {"set": "sets"}   # the reverse mapping, for raw-argv presence detection


def _fanout_mode(args: argparse.Namespace) -> str | None:
    """Which fixed-flag mode this invocation is, for the whitelist below."""
    if args.manifest:
        return "manifest"
    if getattr(args, "council_status", None):
        return "council-status"
    if args.council:
        return "council-resume" if getattr(args, "resume_run", None) else "council"
    return None


def _unsupported_mode_flags(argv: list, args: argparse.Namespace) -> str | None:
    """Error text when a fan-out mode received flags it does not consume, else
    None. Presence is detected from the RAW (post-subcommand-rewrite) argv, not
    by comparing parsed values to defaults -- a value equal to its default
    (e.g. ``--timeout 600000``) is still an explicit flag and still rejected."""
    mode = _fanout_mode(args)
    if mode is None:
        return None
    allowed = _MODE_FLAGS[mode]
    present = set()
    for tok in argv:
        if tok.startswith("--"):
            name = tok[2:].split("=", 1)[0]
            present.add(_TOKEN_DESTS.get(name, name.replace("-", "_")))
    offending = sorted(
        _FLAG_NAMES.get(dest, "--" + dest.replace("_", "-"))
        for dest in vars(args)
        if dest not in allowed and dest in present
    )
    if not offending:
        return None
    label = {"council-resume": "council resume", "council-status": "council status"
             }.get(mode, f"--{mode}")
    return (f"{label} does not support {', '.join(offending)}: these flags would "
            f"have been silently ignored, so they are rejected instead. "
            f"{_MODE_HINTS[mode]}")


# --- Subcommand front-end -----------------------------------------------------
# summon presents git-style subcommands (dispatch/manifest/council/doctor/models/
# agent/list/version) that translate to the underlying flat flags. The flat form
# still works unchanged (legacy compat) — anything starting with '-' skips the
# rewrite. This keeps one battle-tested parser + all logic while giving a clean,
# discoverable command surface.
_SUBCOMMANDS = {"dispatch", "run", "list", "agents", "ls", "models", "doctor",
                "manifest", "council", "agent", "version", "help", "--help", "-h"}

_USAGE = """summon — cross-vendor sub-agents for any AI CLI

Usage: summon <command> [options]

Commands:
  dispatch  --agent NAME --prompt "…" --cwd DIR   run an agent (the default action)
  list                                            list available agents
  models    [--cli BACKEND]                       what each backend can run now
  doctor    [--json]                              check backends / setup health
  manifest  FILE [--concurrency …] [--results-dir D]   run a batch swarm
  council   --question "…" [--members …] [--rounds 2]  decide by consensus
  agent new NAME [--set k=v …]                    scaffold an agent definition
  agent set NAME  --set k=v …                     retune an agent's frontmatter
  version                                         print version

Legacy flat flags still work: `summon --agent NAME --prompt … --cwd …`,
`summon --list`, `summon --manifest FILE`, etc. Run any command with --help for
its options. Full docs: SKILL.md.
"""


def _rewrite_subcommand(argv: list) -> tuple:
    """Translate a leading subcommand into equivalent flat flags. Returns
    ``(argv, mode)`` where mode is 'help' (print usage, exit 0), a string
    'error: …' (print error, exit 2), or None. Legacy flat invocations (argv
    starts with '-') pass through untouched."""
    if not argv:
        return argv, "help"
    head = argv[0]
    if head.startswith("-") or head not in _SUBCOMMANDS:
        return argv, None  # legacy flat (or a stray token the flat parser reports)
    if head in ("help", "--help", "-h"):
        return argv, "help"
    rest = argv[1:]
    # `<subcommand> --help/-h`: the argv-rewrite facade has no per-command parser,
    # so show the general usage rather than argparse erroring on a missing positional.
    if any(a in ("--help", "-h") for a in rest):
        return argv, "help"
    if head in ("dispatch", "run"):
        return rest, None
    if head in ("list", "agents", "ls"):
        return ["--list", *rest], None
    if head == "models":
        return ["--list-models", *rest], None
    if head == "doctor":
        return ["--doctor", *rest], None
    if head == "council":
        # `council resume <id>` and `council status <id>` are nested actions;
        # a bare `council …` stays the fresh-run form.
        if rest and rest[0] == "resume":
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, "error: 'council resume' needs a run id"
            return ["--council", "--resume-run", rest[1], *rest[2:]], None
        if rest and rest[0] == "status":
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, "error: 'council status' needs a run id"
            # NO --council: status dispatches on --council-status alone (and its
            # whitelist would reject a stray --council).
            return ["--council-status", rest[1], *rest[2:]], None
        return ["--council", *rest], None
    if head == "version":
        return ["--version", *rest], None
    if head == "manifest":            # first positional is the manifest file
        return (["--manifest", *rest], None)
    if head == "agent":
        if not rest:
            return argv, "help"       # `summon agent` -> usage
        if rest[0] not in ("new", "set"):
            # an invalid action (e.g. `agent delete`) is an ERROR, not success —
            # automation must not read exit 0 for a bogus command.
            return argv, f"error: unknown 'agent' action {rest[0]!r} (use 'new' or 'set')"
        flag = "--new-agent" if rest[0] == "new" else "--set-agent"
        return ([flag, *rest[1:]], None)
    return argv, None


def main() -> None:
    # Windows consoles default to cp1252; sub-agent results often contain
    # non-ASCII (arrows, em-dashes, emoji). Emit UTF-8 so json.dumps never
    # raises UnicodeEncodeError on stdout.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Subcommand front-end: translate `summon <command> …` to flat flags; `summon`
    # / `summon help` prints usage. Legacy flat invocations pass through.
    argv, mode = _rewrite_subcommand(sys.argv[1:])
    if mode == "help":
        print(_USAGE)
        sys.exit(0)
    if mode and mode.startswith("error:"):
        _print_error(mode[len("error:"):].strip())
        sys.exit(2)

    parser = argparse.ArgumentParser(description="Execute external CLI AIs as sub-agents")
    parser.add_argument("--version", action="version",
                        version=f"summon {__version__} (envelope schema v{_ENVELOPE_VERSION})")
    parser.add_argument("--list", action="store_true", help="List available agents")
    parser.add_argument("--list-models", dest="list_models", action="store_true",
                        help="Report invocable models per backend (live where the CLI exposes it; "
                             "filter with --cli)")
    parser.add_argument("--doctor", action="store_true",
                        help="Check backend CLIs, agy wrapper deps, agents dir, and git; "
                             "human-readable (add --json for machines)")
    parser.add_argument("--new-agent", dest="new_agent", metavar="NAME",
                        help="Scaffold a new agent definition (house template: report "
                             "contract + untrusted-content guard); customize with --set")
    parser.add_argument("--set-agent", dest="set_agent", metavar="NAME",
                        help="Edit an existing agent's frontmatter via --set KEY=VALUE "
                             "(KEY= removes); body untouched")
    parser.add_argument("--set", dest="sets", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="With --new-agent/--set-agent: run-agent, model, permission, args")
    parser.add_argument("--json", action="store_true",
                        help="With --doctor: emit machine-readable JSON instead of the table")
    parser.add_argument("--agent", help="Agent definition name")
    parser.add_argument("--prompt", help="Task prompt")
    parser.add_argument("--prompt-file", dest="prompt_file",
                        help="Read the task prompt from FILE (UTF-8; BOM tolerated). "
                             "Mutually exclusive with --prompt. Ergonomics for long/"
                             "quoted prompts -- backends still receive the prompt via "
                             "argv, so backend argv limits (e.g. agy ~28k chars) apply")
    parser.add_argument("--cwd", help="Working directory (absolute path)")
    parser.add_argument("--agents-dir", help="Directory containing agent definitions")
    parser.add_argument(
        "--timeout", type=_parse_timeout, default=600000,
        help="Timeout: bare ms, or with suffix — 600s, 10m (default: 600000 ms = 10m)"
    )
    parser.add_argument("--cli", help="Force specific CLI (claude, cursor-agent, codex, gemini)")
    parser.add_argument("--model", help="Override the agent's frontmatter model for this call")
    parser.add_argument("--effort", help="Reasoning effort (claude): low|medium|high|xhigh|max")
    parser.add_argument("--resume", dest="resume", help="Backend session/thread/chat id to resume")
    parser.add_argument("--resume-profile", help="agy only: profile dir of the session being resumed")
    parser.add_argument("--worktree", nargs="?", const="", default=None,
                        help="Run in an isolated git worktree (optional name; auto-named if bare)")
    parser.add_argument("--background", action="store_true",
                        help="Dispatch detached; return a job handle immediately")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Print the fully resolved dispatch (command, model, permission "
                             "flags, cwd) WITHOUT executing anything")
    parser.add_argument("--out", help="Write the envelope atomically to FILE; if FILE already "
                                      "holds a valid envelope, skip the run (swarm resume)")
    parser.add_argument("--retries", type=int, default=0,
                        help="Re-dispatch up to N times on error/partial, exponential backoff")
    parser.add_argument("--allow-credit", dest="allow_credit", action="store_true",
                        help="Authorize spending ACCOUNT CREDIT on a credit-only model "
                             "(Fable) for this one dispatch — flag form of "
                             "SUMMON_ALLOW_CREDIT=1. Single dispatch only: rejected for "
                             "--manifest/--council (set the env var deliberately for "
                             "fan-out spend)")
    parser.add_argument("--json-schema", dest="json_schema",
                        help="Validate the agent's final JSON against this schema file; attach "
                             "parsed/parse_ok; one corrective retry via resume on mismatch")
    parser.add_argument("--debug-dir", dest="debug_dir",
                        help="Dump per-run argv + raw output + envelope into this dir")
    parser.add_argument("--job-file", dest="job_file", help=argparse.SUPPRESS)  # internal
    parser.add_argument("--manifest", help="Run a batch of jobs from a JSON manifest (see SKILL.md)")
    parser.add_argument("--concurrency", help="With --manifest: per-backend caps, e.g. agy=2,codex=3,default=3")
    parser.add_argument("--results-dir", dest="results_dir",
                        help="With --manifest: envelope dir (default {cwd}/.agents/results)")
    parser.add_argument("--council", action="store_true",
                        help="Decide by consensus: dispatch --question to diverse members, "
                             "then a chairman synthesizes. See SKILL.md")
    parser.add_argument("--question", help="With --council: the decision/question to deliberate")
    parser.add_argument("--question-file", dest="question_file",
                        help="With --council: read the question from a file")
    parser.add_argument("--members", help="With --council: comma-separated member agents "
                                          "(default: a vendor-diverse set)")
    parser.add_argument("--chairman", help="With --council: the synthesizer agent (default: fable)")
    parser.add_argument("--rounds", type=int, default=1,
                        help="With --council: 1 (independent) or 2 (adds cross-examination)")
    parser.add_argument("--run-dir", dest="run_dir",
                        help="With --council: root for the durable run directory "
                             "(default {cwd}/.agents/runs; env SUMMON_RUNS_DIR)")
    parser.add_argument("--resume-run", dest="resume_run", metavar="RUN_ID",
                        help="Resume a council run by id: re-run only missing/failed/"
                             "changed stages (question/members come from its receipt)")
    parser.add_argument("--council-status", dest="council_status", metavar="RUN_ID",
                        help="Print a council run's durable state (read-only; add --json)")

    args = parser.parse_args(argv)

    global _JOB_FILE
    _JOB_FILE = args.job_file

    # Fan-out modes consume a fixed flag set; anything else present in argv is
    # rejected FIRST -- before the query handlers below, so `--manifest --doctor`
    # can't run doctor while silently dropping the manifest (see _MODE_FLAGS).
    _bad_mode_flags = _unsupported_mode_flags(argv, args)
    if _bad_mode_flags:
        _print_error(_bad_mode_flags)
        sys.exit(1)

    # --list-models / --doctor: pure discovery queries. Need no agent/prompt/cwd —
    # answer and exit before any of those are validated.
    if args.list_models:
        print(json.dumps({"models": discover_models(args.cli)}, ensure_ascii=False))
        sys.exit(0)

    if args.doctor:
        from _doctor import doctor, render  # local import: keeps dispatch path lean
        report = doctor(args.agents_dir, args.cwd)
        print(json.dumps(report, ensure_ascii=False) if args.json else render(report))
        sys.exit(0 if report["ok"] else 1)

    # --new-agent / --set-agent: local roster management, no dispatch involved.
    if args.new_agent or args.set_agent:
        if args.new_agent and args.set_agent:
            _print_error("--new-agent and --set-agent are mutually exclusive; run one at a time")
            sys.exit(1)
        from _roster import new_agent, parse_sets, set_agent
        try:
            sets = parse_sets(args.sets)
            roster_dir = get_agents_dir(args.agents_dir, args.cwd)
            # The skill's bundled starter roster is READ-ONLY. Refuse to scaffold
            # or mutate an agent inside it (whether reached by default resolution
            # or an explicit --agents-dir / $SUB_AGENTS_DIR pointed at the installed
            # skill's agents/): that would corrupt the installed skill and desync
            # its ownership manifest. Writable rosters only.
            _bundled = bundled_roster_dir()
            if _bundled and Path(roster_dir).resolve() == Path(_bundled).resolve():
                _print_error(
                    f"refusing to modify the skill's bundled starter roster ({roster_dir}); "
                    "use a project .agents/ dir or point --agents-dir at a writable location")
                sys.exit(1)
            if args.new_agent:
                info = new_agent(roster_dir, args.new_agent, sets)
                info["status"] = "success"
                info["note"] = ("scaffolded from the house template - edit the body "
                                "(purpose, Role, rubric) before first dispatch")
            else:
                info = set_agent(roster_dir, args.set_agent, sets)
                info["status"] = "success"
            print(json.dumps(info, ensure_ascii=False))
            sys.exit(0)
        except FileExistsError:
            _print_error(f"agent {args.new_agent!r} already exists; use --set-agent to modify it")
            sys.exit(1)
        except (ValueError, FileNotFoundError, OSError) as e:
            _print_error(str(e))
            sys.exit(1)

    # Provenance receipt, built PROGRESSIVELY: summon identity from here on, so
    # EVERY single-dispatch-path envelope (combo rejections, prompt-file errors,
    # missing agent, invalid effort, bad endpoint, preflight, real results)
    # names the install that produced it. The prompt hash joins as soon as the
    # prompt is resolved; agent identity after load; git HEAD once the cwd is
    # validated. Cheap (one hash over the scripts dir), so computing it even for
    # runs that turn out to be fan-out modes is fine.
    receipt: dict = _receipt_base()

    def _die(msg: str, exit_code: int = 1) -> None:
        env = {"result": "", "exit_code": exit_code, "status": "error", "error": msg}
        env.update(receipt)
        _emit(env)
        sys.exit(exit_code)

    if args.resume and args.worktree is not None:
        _die("--resume and --worktree are incompatible: a session lives in the "
             "original project dir, not a fresh worktree")

    # --dry-run is a SINGLE-dispatch preview only. Combining it with modes that
    # fan out or detach would otherwise slip past the dry-run exit and run real
    # work (a detached --background child never even inherits --dry-run). Refuse
    # loudly instead of silently executing.
    if args.dry_run and (args.background or args.manifest or args.council):
        _die("--dry-run cannot be combined with --background, --manifest, or "
             "--council (it previews one resolved dispatch and never executes)")

    # --background and --out are two DIFFERENT completion contracts: background
    # signals done via its own result_file (job handle), while --out means
    # "write the envelope here, skip if it already exists". Mixing them is
    # ambiguous (skip returns a cached envelope with no job handle; a pre-dispatch
    # error never creates --out). For fan-out with per-job result files, use
    # --manifest. Reject the combination rather than pick a surprising winner.
    if args.background and args.out:
        _die("--background and --out are incompatible: background reports "
             "completion via its own result_file; --out is the (manifest) "
             "result-file mechanism. Use --manifest for fan-out with result files.")

    # --manifest: batch fan-out. Delegates to _manifest and exits.
    if args.manifest:
        from _manifest import run_manifest
        sys.exit(run_manifest(args))

    # council status: read-only durable-state view. No dispatch, no lock.
    if args.council_status:
        from _council import run_council_status
        sys.exit(run_council_status(args))

    # --council: consensus deliberation (fresh run or --resume-run). Delegates
    # to _council and exits.
    if args.council:
        from _council import run_council
        sys.exit(run_council(args))

    # --out resume behavior: a pre-existing SUCCESS envelope means this job is
    # already done — emit it (marked skipped) and exit without dispatching. A
    # prior error/blocked/partial envelope is NOT terminal: re-running retries
    # it (matches the manifest's resume semantics — failures get another shot).
    if args.out and os.path.isfile(args.out) and not args.dry_run:
        try:
            with open(args.out, encoding="utf-8") as fh:
                prior = json.load(fh)
        except (OSError, ValueError):
            prior = None
        if isinstance(prior, dict) and prior.get("status") == "success":
            prior["skipped"] = True
            _emit(prior)
            sys.exit(0)

    # --prompt-file: resolve to a prompt BEFORE the background handler (its
    # validation needs args.prompt). utf-8-sig strips a BOM; strict decoding so
    # mojibake fails loudly instead of reaching a paid model. NOTE: this is
    # quoting/encoding ergonomics, not argv-limit relief -- builders still pass
    # the prompt as one argv token (agy's ~28k guard still applies). Presence
    # checks (is not None) on BOTH sides, not truthiness: --prompt "" plus
    # --prompt-file, and --prompt plus --prompt-file "", are each two competing
    # inputs (and an empty filename then fails the open loudly, never silently).
    if args.prompt is not None and args.prompt_file is not None:
        _die("give --prompt or --prompt-file, not both")
    if args.prompt_file is not None:
        try:
            with open(args.prompt_file, encoding="utf-8-sig") as fh:
                args.prompt = fh.read()
        except (OSError, UnicodeDecodeError, ValueError) as e:
            _die(f"cannot read --prompt-file {args.prompt_file}: {e}")
        if not args.prompt.strip():
            _die(f"--prompt-file {args.prompt_file} is empty")

    # Root-prompt hash joins the receipt HERE, as soon as the prompt is final,
    # so even a missing-agent error downstream carries it.
    receipt.update(_receipt_prompt(args.prompt))

    # --allow-credit: per-dispatch credit authorization. Env form of the same
    # switch, set process-local so the credit guard and any --background child
    # (env-inherited AND argv-propagated) see it. Fan-out modes never reach
    # here -- the mode-flag matrix rejects the flag for them.
    if args.allow_credit:
        os.environ["SUMMON_ALLOW_CREDIT"] = "1"

    # --json-schema: fail fast on an unloadable schema BEFORE paying for a run.
    schema = None
    if args.json_schema:
        try:
            with open(args.json_schema, encoding="utf-8") as fh:
                schema = json.load(fh)
            if not isinstance(schema, dict):
                raise ValueError("schema root must be a JSON object")
        except (OSError, ValueError) as e:
            _die(f"--json-schema: cannot load {args.json_schema}: {e}")

    # --background: hand off to a detached copy of ourselves and return at once.
    if args.background and not args.list:
        if not (args.agent and args.prompt and args.cwd):
            _die("--background requires --agent, --prompt, and --cwd")
        print(json.dumps(_spawn_background(args), ensure_ascii=False))
        sys.exit(0)

    if args.list:
        agents_dir = get_agents_dir(args.agents_dir, args.cwd)
        agents = list_agents(agents_dir)
        print(json.dumps({"agents": agents, "agents_dir": agents_dir}, ensure_ascii=False))
        sys.exit(0)

    # Validate required args for execution
    if not args.agent:
        _die("--agent is required")
    if not args.prompt:
        _die("--prompt is required")
    if not args.cwd:
        _die("--cwd is required")
    if not os.path.isabs(args.cwd):
        _die("cwd must be an absolute path")
    if not os.path.isdir(args.cwd):
        _die(f"cwd does not exist: {args.cwd}")

    # Input provenance: HEAD of the (validated) cwd. Recomputed after a
    # --worktree rewrite so the dispatched value names the effective tree;
    # pre-worktree failures (incl. preflight) carry the original cwd's HEAD.
    receipt["git_head_before"] = _git_head(args.cwd)

    agents_dir = get_agents_dir(args.agents_dir, args.cwd)

    try:
        run_agent_cli, system_context, _, agent_file, permission, model, extra_args, effort_fm = load_agent(
            agents_dir, args.agent
        )
    except (FileNotFoundError, ValueError) as e:
        _die(str(e))

    receipt.update(_receipt_agent(args, agent_file))

    # Shared project memory: inject {cwd}/.agents/memory.md (standing conventions,
    # constraints, durable decisions) so callers don't re-explain project context
    # every prompt. Read from the ORIGINAL cwd (before any worktree rewrite).
    # Skipped on resume — the session already carries it.
    if not args.resume:
        system_context = _inject_memory(system_context, args.cwd)

    try:
        cli = args.cli or resolve_cli(run_agent_cli)
    except ValueError as e:
        _die(f"agent {args.agent!r}: {e}")

    # Pre-flight the backend BEFORE any side effects (e.g. creating a worktree):
    # a missing CLI becomes a clear setup message (install + sign-in + what IS
    # ready) instead of a raw spawn failure, so a first-time user, or an agent
    # that skipped `doctor`, is told exactly what to do. Skipped under --dry-run,
    # which must preview a dispatch even when the backend isn't installed yet.
    if not args.dry_run:
        setup_error = _preflight_backend(cli)
        if setup_error is not None:
            setup_error.update(receipt)   # provenance even on the no-backend path
            _emit(setup_error)
            sys.exit(setup_error.get("exit_code", 1))

    # --worktree: run the agent in an isolated git worktree instead of the cwd.
    # NEVER created under --dry-run (dry-run is mutation-free by contract).
    worktree_info = None
    if args.worktree is not None and not args.dry_run:
        try:
            worktree_info = _setup_worktree(args.cwd, args.worktree, args.agent)
        except ValueError as e:
            _die(str(e))
        args.cwd = worktree_info["cwd"]

    # Reasoning-effort precedence: --effort > agent `effort:` frontmatter >
    # SUMMON_DEFAULT_EFFORT env > the built-in default (high — summon delegates the
    # hard problems, so it defaults to deep reasoning). `none`/`default`/`off` = the
    # backend's own default. claude/codex take an effort flag; agy encodes thinking
    # in the model NAME (Gemini Low/Medium/High); others don't have the knob.
    _EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
    final_model = args.model or model       # dispatch-time override wins over frontmatter
    effort = (args.effort or effort_fm or os.environ.get("SUMMON_DEFAULT_EFFORT") or "high")
    if effort in ("none", "default", "off"):
        effort = None
    elif effort not in _EFFORT_LEVELS:
        _die(f"invalid effort {effort!r}: use one of {', '.join(_EFFORT_LEVELS)} "
             "(or none/default to use the backend's own default)")
    _explicit_effort = bool(args.effort or effort_fm)
    if cli == "agy":
        # agy has no --effort flag; thinking is the model-name suffix. Apply an
        # EXPLICIT effort to a Gemini model; the global default never rewrites an
        # agy model (respects the variant chosen in `model:`).
        if effort and _explicit_effort and final_model and final_model.strip().lower().startswith("gemini"):
            final_model = _apply_gemini_thinking(final_model, effort)
        elif _explicit_effort:
            print("note: agy thinking is a model-name suffix (e.g. 'Gemini 3.1 Pro (High)'); "
                  "--effort maps only Gemini agy models — set it in `model:` / see --list-models",
                  file=sys.stderr)
        effort = None
    elif effort and cli not in ("claude", "codex"):
        if _explicit_effort:
            print(f"note: effort is only honored by claude/codex/agy; ignored for {cli}",
                  file=sys.stderr)
        effort = None

    # openai-compat: resolve the API endpoint (provider -> base_url/api_key_env)
    # from the agent's frontmatter now, while we still have the agents dir.
    base_url = api_key_env = None
    if cli == "openai-compat":
        from _apibackend import resolve_endpoint
        from _loader import parse_frontmatter
        try:
            with open(agent_file, encoding="utf-8") as fh:
                fm, _ = parse_frontmatter(fh.read())
            base_url, api_key_env = resolve_endpoint(fm, agents_dir)
        except (OSError, ValueError) as e:
            _die(f"openai-compat agent {args.agent!r}: {e}")

    invocation = AgentInvocation(
        cli=cli,
        prompt=args.prompt,
        cwd=args.cwd,
        system_context=system_context,
        agent_file=agent_file,
        permission=permission,
        model=final_model,               # incl. agy Gemini thinking-mode suffix
        effort=effort,                    # --effort > frontmatter > env > default(high)
        resume_id=args.resume,
        resume_profile=args.resume_profile,
        extra_args=tuple(extra_args),
        base_url=base_url,
        api_key_env=api_key_env,
    )

    if args.dry_run:
        _emit(_dry_run_view(invocation, args, agents_dir))
        sys.exit(0)

    # Effective-tree provenance: recompute HEAD after any worktree rewrite,
    # BEFORE the agent can commit anything.
    receipt["git_head_before"] = _git_head(args.cwd)

    # Catch ValueError from build_invocation_args / permission_flags / TOML
    # escaping so unknown --cli values or unsafe agent paths surface as JSON
    # errors rather than tracebacks. All other CLI-side failures are already
    # shaped into the response by execute_agent.
    try:
        result = _dispatch_with_retries(invocation, args)
    except ValueError as e:
        _die(str(e))

    # --json-schema: structured-output contract with ONE corrective retry.
    if schema is not None and result.get("status") == "success":
        result = _apply_schema(result, schema, invocation, args)

    # Receipt LAST, from main()-scope values: a schema-correction retry replaces
    # the envelope, and this keeps prompt_sha256 bound to the ROOT prompt (the
    # correction prompt must never restamp it).
    result.update(receipt)

    if worktree_info:
        result["worktree"] = worktree_info
    if args.out:
        _write_out(args.out, result)
    _emit(result)
    sys.exit(0 if result["status"] == "success" else 1)


def _dry_run_view(invocation, args, agents_dir: str) -> dict:
    """The fully resolved dispatch, without executing. For agy the per-call
    profile is NOT built (that copies OAuth tokens = a mutation); the wrapper
    path is shown instead."""
    from _builder import (BACKENDS, backend_kind, build_invocation_args,
                          permission_flags as _pf, _PERMISSION_MAPPING, _agy_wrapper,
                          agy_permission_warning, apply_credit_guard, infer_billing,
                          credit_spend_allowed, selects_credit_only)
    _guarded, _, _guard_warnings = apply_credit_guard(invocation)
    _eff_model = _guarded.model
    # Predict the billing source so preflight can reveal a charge (mirrors _stamp).
    _bill = infer_billing(invocation.cli)
    if invocation.cli == "claude" and selects_credit_only(invocation.model, invocation.extra_args):
        if credit_spend_allowed():
            _bill = {"source": "api" if os.environ.get("ANTHROPIC_API_KEY") else "credit",
                     "note": "credit-only model (Fable) authorized"}
        elif invocation.resume_id:
            _bill = {"source": "unknown", "note": "resume keeps the session's original model"}
    view = {
        "dry_run": True,
        "agent": args.agent,
        "cli": invocation.cli,
        "cwd": invocation.cwd,
        "agents_dir": agents_dir,
        "model_requested": invocation.model,
        "model_effective": _eff_model,  # after the credit-only (Fable) fallback
        "billing_predicted": _bill,     # subscription / credit / api / unknown
        "permission": invocation.permission,
        # openai-compat (and any future non-sandbox backend) has no permission
        # mapping — report None instead of raising.
        "permission_flags": (_pf(invocation.cli, invocation.permission)
                             if invocation.cli in _PERMISSION_MAPPING else None),
        "extra_args": list(invocation.extra_args),
        "timeout_ms": args.timeout,
        "worktree": ("would create" if args.worktree is not None else None),
        "system_context_chars": len(invocation.system_context),
    }
    for _w in _guard_warnings:  # credit-only guard actions surfaced in the preview
        view.setdefault("warnings", []).append(_w)
    _pw = agy_permission_warning(invocation.cli, invocation.permission)
    if _pw:  # same helper as the real envelope -> identical warning, exactly once
        view.setdefault("warnings", []).append(_pw)
    if backend_kind(invocation.cli) == "api":
        view["command"] = f"POST ({invocation.cli})"
        view["base_url"] = invocation.base_url
        view["endpoint"] = (invocation.base_url or "?") + "/chat/completions"
        view["api_key_env"] = invocation.api_key_env
        view["api_key_present"] = bool(invocation.api_key_env and os.environ.get(invocation.api_key_env))
        view["billing"] = {"source": "api"}
    elif BACKENDS.get(invocation.cli, {}).get("side_effects"):
        # A side-effecting build (agy builds a per-call profile) must NOT run
        # under --dry-run. Generic for any such backend; agy adds wrapper detail.
        view["note"] = ("this backend's build has filesystem side-effects and is NOT "
                        "invoked in --dry-run; the real dispatch performs them")
        if invocation.cli == "agy":
            try:
                view["command"] = "python <wrapper>"
                view["wrapper"] = _agy_wrapper()
            except ValueError as e:
                view["error"] = str(e)
    else:
        try:
            cmd, argv, env = build_invocation_args(invocation)
            view["command"] = cmd
            view["args"] = [a if len(a) <= 400 else a[:400] + f"...[+{len(a)-400} chars]" for a in argv]
            view["env_overrides"] = sorted(env) if env else []
        except ValueError as e:
            view["error"] = str(e)
    return view


def _dispatch_with_retries(invocation, args) -> dict:
    """execute_agent with --retries: exponential backoff on error/partial only
    (blocked won't improve by retrying — its cause is structural)."""
    attempt = 0
    while True:
        result = execute_agent(invocation, timeout_ms=args.timeout, debug_dir=args.debug_dir)
        attempt += 1
        if result.get("status") not in ("error", "partial") or attempt > max(0, args.retries):
            break
        time.sleep(min(30, 2 ** attempt))
    result["attempts"] = attempt
    return result


def _apply_schema(result: dict, schema: dict, invocation, args) -> dict:
    """Validate the agent's final JSON; on mismatch, ONE corrective follow-up
    through --resume (claude/codex/cursor via session_id; agy via profile)."""
    from _schema import attach_parsed, correction_prompt
    from _builder import AgentInvocation as _AI
    attach_parsed(result, schema)
    if result["parse_ok"]:
        return result
    resume = result.get("resume") or {}
    sid, profile = resume.get("session_id"), resume.get("profile")
    if not sid and not profile:
        return result  # no resume lane (e.g. gemini) — return the verdict as-is
    retry_inv = _AI(
        cli=invocation.cli,
        prompt=correction_prompt(schema, result.get("parse_errors") or []),
        cwd=invocation.cwd,
        system_context="",  # resume: session already holds the definition
        agent_file=invocation.agent_file,
        permission=invocation.permission,
        model=invocation.model,
        effort=invocation.effort,
        resume_id=sid or "latest",
        resume_profile=profile,
        extra_args=invocation.extra_args,
    )
    try:
        retry = execute_agent(retry_inv, timeout_ms=args.timeout, debug_dir=args.debug_dir)
    except ValueError:
        return result  # resume unsupported on this backend: keep the first verdict
    retry["parse_retry"] = True
    attach_parsed(retry, schema)
    # Only accept the retry if it STRICTLY improved things: the corrective run
    # both completed successfully AND now satisfies the schema. A retry that
    # errored, timed out, or is still schema-invalid must never replace the
    # original successful (if invalid) envelope.
    if retry.get("status") == "success" and retry.get("parse_ok"):
        # Preserve the total dispatch count across the correction (the retry is
        # additional work, not a reset) so cost accounting stays honest.
        retry["attempts"] = result.get("attempts", 1) + retry.get("attempts", 1)
        return retry
    return result


def _write_out(path: str, result: dict) -> None:
    """Atomic envelope write: a present file is a COMPLETE file, which is what
    makes --out usable as a swarm's skip-if-done marker. The temp file is
    per-process-unique (mkstemp) so two processes writing the same --out never
    clobber each other's partial temp; the final rename is atomic."""
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".summon-out-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        result["out_error"] = f"failed to write --out {path}: {e}"


def _resolve_job_file() -> str | None:
    """The job file, even if main() crashed before setting _JOB_FILE."""
    if _JOB_FILE:
        return _JOB_FILE
    argv = sys.argv
    if "--job-file" in argv:
        i = argv.index("--job-file")
        if i + 1 < len(argv):
            return argv[i + 1]
    for a in argv:
        if a.startswith("--job-file="):
            return a.split("=", 1)[1]
    return None


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # intentional exits (validation, normal completion) pass through
    except BaseException as e:  # noqa: BLE001 — last-resort net so a bg job never orphans
        err = {"result": "", "status": "error", "exit_code": 1,
               "error": f"uncaught {type(e).__name__}: {e}"}
        jf = _resolve_job_file()
        if jf:
            try:
                with open(jf + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump(err, fh, ensure_ascii=False)
                os.replace(jf + ".tmp", jf)
            except OSError:
                pass
        else:
            print(json.dumps(err, ensure_ascii=False))
        sys.exit(1)
