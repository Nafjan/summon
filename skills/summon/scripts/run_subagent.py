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

import _background  # noqa: E402
import _cli  # noqa: E402
import _receipt  # noqa: E402
from _builder import AgentInvocation  # noqa: E402
from _executor import ENVELOPE_VERSION as _ENVELOPE_VERSION  # noqa: E402
from _executor import execute_agent, finalize_exit_fields, is_terminal_success  # noqa: E402
from _loader import bundled_roster_dir, get_agents_dir, list_agents, load_agent  # noqa: E402
from _resolver import discover_models, resolve_cli  # noqa: E402

__version__ = "0.9.0"  # summon dispatcher version (see CHANGELOG.md)

# When set (a --background child), the final JSON goes to this file (atomically,
# via .tmp + rename) instead of stdout, so the parent can poll for completion.
_JOB_FILE: str | None = None


def _stamp_job(env: dict) -> dict:
    """Stamp a background child's result envelope with its job identity so a
    result at a job's path can be authenticated against the launch record. Fires
    ONLY when the internal ``--job-file`` is present -- that flag is how the
    parent spawns a background child, so a NORMAL foreground run (which never
    passes it) cannot carry a `job_nonce` from a stray SUMMON_JOB_* env var. A
    caller that deliberately passes the internal flag AND sets SUMMON_JOB_NONCE
    can hand-stamp one, but on a single-user machine that is self-inflicted: the
    nonce is best-effort integrity against stale/mismatched result files, not a
    security boundary."""
    if _resolve_job_file() is None:
        return env
    nonce = os.environ.get("SUMMON_JOB_NONCE")
    if nonce:
        env["job_nonce"] = nonce
    # Fill prompt_sha256 on paths that lack a full receipt (the crash writer);
    # a normal envelope already carries the receipt-computed hash, kept as-is.
    if env.get("prompt_sha256") is None:
        _ph = os.environ.get("SUMMON_JOB_PROMPT_SHA")
        if _ph:
            env["prompt_sha256"] = _ph
    return env


def _emit(obj: dict) -> None:
    """Write the response as JSON — to the job file (background) or stdout."""
    # Single emission point: guarantee the exit-code-clarity fields on EVERY
    # dispatch-shaped envelope, including the pre-dispatch validation/preflight
    # paths that never reach the executor's _stamp. Idempotent + no-op on query
    # envelopes (list/doctor/version have no exit_code).
    finalize_exit_fields(obj)
    _stamp_job(obj)
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


# --- Background dispatch + jobs queries (moved to _background.py) ---------------
# child_argv/spawn_background/run_jobs_query/render_jobs live in _background.py.
# _child_argv is a CALL re-export (a test calls it); spawn_background uses the
# _background.child_argv directly, so this binding is not a patch-through seam.
# _spawn_background stays a thin wrapper that injects THIS entry script's path
# (the child re-execs run_subagent.py, not _background.py) and the summon receipt,
# so nothing in _background imports the hub back.
_child_argv = _background.child_argv


def _spawn_background(args: argparse.Namespace) -> dict:
    """Dispatch detached. See _background.spawn_background; the entry-script path
    and summon receipt are injected here."""
    return _background.spawn_background(
        args, os.path.abspath(__file__), _receipt_base()["summon"])


# --- Provenance receipt --------------------------------------------------------
# Three divergent installed copies of this dispatcher (one hand-patched) all
# self-reported "0.9.0" while their scripts differed, making envelopes
# unattributable. Every dispatch envelope now carries the dispatcher's identity,
# the agent definition actually loaded, and the root prompt hash, so drift is
# diagnosable from any single envelope. Paths are absolute local-operator data
# (documented in SKILL.md); no prompt text or secrets, hashes only.

# Bodies live in _receipt.py. _receipt_agent/_receipt_prompt/_git_head are CALL
# re-exports for the tests (which invoke run_subagent._receipt_*); main() calls
# _receipt.* directly, so these are not patch-through seams. _receipt_base is the
# one real wrapper: it binds THIS entry script's path + version so the receipt's
# `script`/`version` name run_subagent.py, not _receipt.py (a sibling).
_receipt_agent = _receipt.receipt_agent
_receipt_prompt = _receipt.receipt_prompt
_git_head = _receipt.git_head


def _receipt_base() -> dict:
    """summon identity, bound to THIS entry script. See _receipt.receipt_base."""
    return _receipt.receipt_base(os.path.abspath(__file__), __version__)


# --- Command-line surface (moved to _cli.py) -----------------------------------
# The argparse spec, the git-style subcommand front-end, and the fan-out mode
# flag matrix live in _cli.py. These are CALL re-exports: they keep the historical
# names the tests and main() invoke (the parser is built via _cli.build_parser in
# main()). They are not patch-through seams -- internal callers use the _cli.*
# functions directly, so reassigning e.g. run_subagent._parse_timeout no longer
# affects internals (an incidental co-location property nothing relied on).
_parse_timeout = _cli.parse_timeout
_rewrite_subcommand = _cli.rewrite_subcommand
_unsupported_mode_flags = _cli.unsupported_mode_flags
_USAGE = _cli.USAGE


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
    argv, mode = _cli.rewrite_subcommand(sys.argv[1:])
    if mode == "help":
        print(_cli.USAGE)
        sys.exit(0)
    if mode and mode.startswith("error:"):
        _print_error(mode[len("error:"):].strip())
        sys.exit(2)

    parser = _cli.build_parser(__version__, _ENVELOPE_VERSION)
    args = parser.parse_args(argv)

    global _JOB_FILE
    _JOB_FILE = args.job_file

    # Fan-out modes consume a fixed flag set; anything else present in argv is
    # rejected FIRST -- before the query handlers below, so `--manifest --doctor`
    # can't run doctor while silently dropping the manifest (see _cli.MODE_FLAGS).
    _bad_mode_flags = _cli.unsupported_mode_flags(argv, args)
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

    # jobs list/status/wait: read-only registry queries; no dispatch. Answer and
    # exit before any agent/prompt/cwd validation.
    if args.jobs_list or args.jobs_status or args.jobs_wait:
        sys.exit(_background.run_jobs_query(args, _print_error))

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
    # A SUSPECT success (status=success but report_ok=false -> suspect=true) is
    # NOT terminal either: skipping it would strand a semantically-useful but
    # unparseable envelope, forcing a manual delete/rename to re-run. Re-dispatch
    # it instead (consistent with summon's existing "suspect => re-dispatch" stance).
    if args.out and os.path.isfile(args.out) and not args.dry_run:
        try:
            with open(args.out, encoding="utf-8") as fh:
                prior = json.load(fh)
        except (OSError, ValueError):
            prior = None
        if is_terminal_success(prior):
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
    receipt.update(_receipt.receipt_prompt(args.prompt))

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
    receipt["git_head_before"] = _receipt.git_head(args.cwd)

    agents_dir = get_agents_dir(args.agents_dir, args.cwd)

    try:
        run_agent_cli, system_context, _, agent_file, permission, model, extra_args, effort_fm = load_agent(
            agents_dir, args.agent
        )
    except (FileNotFoundError, ValueError) as e:
        _die(str(e))

    receipt.update(_receipt.receipt_agent(args, agent_file))

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
    receipt["git_head_before"] = _receipt.git_head(args.cwd)

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
        result = execute_agent(invocation, timeout_ms=args.timeout, debug_dir=args.debug_dir,
                               max_tool_output_bytes=getattr(args, "max_tool_output_bytes", None))
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
        retry = execute_agent(retry_inv, timeout_ms=args.timeout, debug_dir=args.debug_dir,
                              max_tool_output_bytes=getattr(args, "max_tool_output_bytes", None))
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
                _stamp_job(err)   # even a crash envelope carries its job identity
                with open(jf + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump(err, fh, ensure_ascii=False)
                os.replace(jf + ".tmp", jf)
            except OSError:
                pass
        else:
            print(json.dumps(err, ensure_ascii=False))
        sys.exit(1)
