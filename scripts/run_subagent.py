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
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Ensure sibling modules import correctly when invoked via absolute path.
sys.path.insert(0, str(Path(__file__).parent))

from _builder import AgentInvocation  # noqa: E402
from _executor import ENVELOPE_VERSION as _ENVELOPE_VERSION  # noqa: E402
from _executor import execute_agent  # noqa: E402
from _loader import get_agents_dir, list_agents, load_agent  # noqa: E402
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
    out = ["--agent", args.agent, "--prompt", args.prompt, "--cwd", args.cwd]
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

    args = parser.parse_args(argv)

    global _JOB_FILE
    _JOB_FILE = args.job_file

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

    if args.resume and args.worktree is not None:
        _print_error("--resume and --worktree are incompatible: a session lives in the "
                     "original project dir, not a fresh worktree")
        sys.exit(1)

    # --dry-run is a SINGLE-dispatch preview only. Combining it with modes that
    # fan out or detach would otherwise slip past the dry-run exit and run real
    # work (a detached --background child never even inherits --dry-run). Refuse
    # loudly instead of silently executing.
    if args.dry_run and (args.background or args.manifest or args.council):
        _print_error("--dry-run cannot be combined with --background, --manifest, or "
                     "--council (it previews one resolved dispatch and never executes)")
        sys.exit(1)

    # --background and --out are two DIFFERENT completion contracts: background
    # signals done via its own result_file (job handle), while --out means
    # "write the envelope here, skip if it already exists". Mixing them is
    # ambiguous (skip returns a cached envelope with no job handle; a pre-dispatch
    # error never creates --out). For fan-out with per-job result files, use
    # --manifest. Reject the combination rather than pick a surprising winner.
    if args.background and args.out:
        _print_error("--background and --out are incompatible: background reports "
                     "completion via its own result_file; --out is the (manifest) "
                     "result-file mechanism. Use --manifest for fan-out with result files.")
        sys.exit(1)

    # --manifest: batch fan-out. Delegates to _manifest and exits.
    if args.manifest:
        from _manifest import run_manifest
        sys.exit(run_manifest(args))

    # --council: consensus deliberation. Delegates to _council and exits.
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

    # --json-schema: fail fast on an unloadable schema BEFORE paying for a run.
    schema = None
    if args.json_schema:
        try:
            with open(args.json_schema, encoding="utf-8") as fh:
                schema = json.load(fh)
            if not isinstance(schema, dict):
                raise ValueError("schema root must be a JSON object")
        except (OSError, ValueError) as e:
            _print_error(f"--json-schema: cannot load {args.json_schema}: {e}")
            sys.exit(1)

    # --background: hand off to a detached copy of ourselves and return at once.
    if args.background and not args.list:
        if not (args.agent and args.prompt and args.cwd):
            _print_error("--background requires --agent, --prompt, and --cwd")
            sys.exit(1)
        print(json.dumps(_spawn_background(args), ensure_ascii=False))
        sys.exit(0)

    if args.list:
        agents_dir = get_agents_dir(args.agents_dir, args.cwd)
        agents = list_agents(agents_dir)
        print(json.dumps({"agents": agents, "agents_dir": agents_dir}, ensure_ascii=False))
        sys.exit(0)

    # Validate required args for execution
    if not args.agent:
        _print_error("--agent is required")
        sys.exit(1)
    if not args.prompt:
        _print_error("--prompt is required")
        sys.exit(1)
    if not args.cwd:
        _print_error("--cwd is required")
        sys.exit(1)
    if not os.path.isabs(args.cwd):
        _print_error("cwd must be an absolute path")
        sys.exit(1)
    if not os.path.isdir(args.cwd):
        _print_error(f"cwd does not exist: {args.cwd}")
        sys.exit(1)

    agents_dir = get_agents_dir(args.agents_dir, args.cwd)

    try:
        run_agent_cli, system_context, _, agent_file, permission, model, extra_args = load_agent(
            agents_dir, args.agent
        )
    except (FileNotFoundError, ValueError) as e:
        _print_error(str(e))
        sys.exit(1)

    # Shared project memory: inject {cwd}/.agents/memory.md (standing conventions,
    # constraints, durable decisions) so callers don't re-explain project context
    # every prompt. Read from the ORIGINAL cwd (before any worktree rewrite).
    # Skipped on resume — the session already carries it.
    if not args.resume:
        system_context = _inject_memory(system_context, args.cwd)

    # --worktree: run the agent in an isolated git worktree instead of the cwd.
    # NEVER created under --dry-run (dry-run is mutation-free by contract).
    worktree_info = None
    if args.worktree is not None and not args.dry_run:
        try:
            worktree_info = _setup_worktree(args.cwd, args.worktree, args.agent)
        except ValueError as e:
            _print_error(str(e))
            sys.exit(1)
        args.cwd = worktree_info["cwd"]

    try:
        cli = args.cli or resolve_cli(run_agent_cli)
    except ValueError as e:
        _print_error(f"agent {args.agent!r}: {e}")
        sys.exit(1)

    # --effort is a claude-only knob; warn instead of silently dropping it so a
    # user doesn't believe a non-claude run was tuned when it wasn't.
    if args.effort and cli != "claude":
        print(f"note: --effort is only honored by the claude backend; ignored for {cli}",
              file=sys.stderr)

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
            _print_error(f"openai-compat agent {args.agent!r}: {e}")
            sys.exit(1)

    invocation = AgentInvocation(
        cli=cli,
        prompt=args.prompt,
        cwd=args.cwd,
        system_context=system_context,
        agent_file=agent_file,
        permission=permission,
        model=args.model or model,       # dispatch-time override wins over frontmatter
        effort=args.effort,
        resume_id=args.resume,
        resume_profile=args.resume_profile,
        extra_args=tuple(extra_args),
        base_url=base_url,
        api_key_env=api_key_env,
    )

    if args.dry_run:
        _emit(_dry_run_view(invocation, args, agents_dir))
        sys.exit(0)

    # Catch ValueError from build_invocation_args / permission_flags / TOML
    # escaping so unknown --cli values or unsafe agent paths surface as JSON
    # errors rather than tracebacks. All other CLI-side failures are already
    # shaped into the response by execute_agent.
    try:
        result = _dispatch_with_retries(invocation, args)
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)

    # --json-schema: structured-output contract with ONE corrective retry.
    if schema is not None and result.get("status") == "success":
        result = _apply_schema(result, schema, invocation, args)

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
                          apply_credit_guard, infer_billing, credit_spend_allowed,
                          selects_credit_only)
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
