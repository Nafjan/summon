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
import hashlib
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
        # Exit-code-clarity fields inlined: this guard runs before the sibling
        # imports, so it cannot call finalize_exit_fields, but the contract holds.
        "backend_exit_code": 1,
        "dispatcher_status": "error",
        "normalization_reason": "interpreter older than Python 3.10; summon did not run",
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
import _executor  # noqa: E402
import _receipt  # noqa: E402
from _builder import AgentInvocation  # noqa: E402
from _executor import ENVELOPE_VERSION as _ENVELOPE_VERSION  # noqa: E402
from _executor import (agent_def_sha, content_sha,  # noqa: E402
                       envelope_answers_request, execute_agent, finalize_exit_fields,
                       is_terminal_success, request_fingerprint)
from _loader import bundled_roster_dir, get_agents_dir, list_agents, load_agent  # noqa: E402
from _resolver import discover_models, resolve_cli  # noqa: E402

__version__ = "0.11.2"  # summon dispatcher version (see CHANGELOG.md)

# When set (a --background child), the final JSON goes to this file (atomically,
# via .tmp + rename) instead of stdout, so the parent can poll for completion.
_JOB_FILE: str | None = None


def _endpoint_for_dispatch(identity: dict, agent_file: str, agents_dir: str) -> tuple:
    """The endpoint this dispatch will call: the SNAPSHOT the request identity already
    resolved, if it has one.

    Resolving a second time here let a providers.json edit in between send the work to one
    endpoint while the envelope recorded another -- and restoring the first then let the
    second's answer resume as its own. A named function rather than an inline branch so a
    test can exercise the choice: with the registry changed underneath, the snapshot must
    still win. Falls back to resolving only when the identity could not (an unresolvable
    endpoint is exactly the case where the error is the point).
    """
    snap = (identity or {}).get("_endpoint")
    if snap:
        return tuple(snap)
    return _compat_endpoint(agent_file, agents_dir)


def _compat_endpoint(agent_file: str, agents_dir: str) -> tuple:
    """Re-read an openai-compat agent's frontmatter to resolve its API endpoint.

    A named function, not an inline block, so a test can exercise THIS decoding rather than
    re-implement it: read as plain utf-8 (as this once was) a BOM hides the frontmatter and
    the dispatch dies with a misleading "needs a provider or base_url". utf-8-sig matches
    what load_agent already does for the same file.
    """
    from _apibackend import resolve_endpoint
    from _loader import parse_frontmatter
    with open(agent_file, encoding="utf-8-sig") as fh:
        fm, _ = parse_frontmatter(fh.read())
    return resolve_endpoint(fm, agents_dir)


def _write_error_out(out_path: str, env: dict) -> None:
    """Record a failure at the authoritative --out path WITHOUT ever destroying a stored
    success.

    The invariant, stated once rather than patched into a sequence: a terminal SUCCESS
    already at that path is never replaced by an error. It is archived first, and if
    archiving FAILS the error is not written at all -- a lost answer is worse than a failure
    that is only on stdout. Anything else there (an error, a partial, junk) is simply
    replaced, and an empty path is simply written.

    That invariant holds for a SINGLE writer, which is the documented model for a result
    path (see references/fan-out.md: one --results-dir belongs to one run). The read, the
    archive and the write are not one atomic step, so a second process writing a success
    into the same path between this read and this write can still lose it. Locking is not
    added because the shared-result-path case is already outside what summon supports; the
    limit is stated here rather than implied away.

    This exists because the naive version -- write the error, full stop -- destroyed
    completed work whenever the failure came before the resume block could preserve it, and
    the next version archived first but ignored the archive's own failure and overwrote
    anyway. Both were caught in review; hence the invariant up front.
    """
    from _executor import is_terminal_success
    from _manifest import _clear_out_file
    prior = None
    try:
        with open(out_path, encoding="utf-8") as fh:
            prior = json.load(fh)
    except (OSError, ValueError):
        prior = None
    if is_terminal_success(prior):
        if _clear_out_file(out_path, archive=True):
            return                      # could not preserve it -> do not overwrite it
    _write_out(out_path, env)


def _request_identity(args) -> dict:
    """Adapter: the dispatcher's RAW inputs, handed to the one shared identity builder.

    Deliberately does no derivation of its own -- every hash and every environment-backed
    field is computed inside build_request_identity, so this view and the manifest parent's
    (_manifest._job_identity) cannot drift apart by having different derived fields.
    """
    return _executor.build_request_identity(
        agent=args.agent, prompt=args.prompt, cwd=args.cwd, agents_dir=args.agents_dir,
        cli=args.cli, model=args.model, effort=args.effort, json_schema=args.json_schema,
        resume=args.resume, resume_profile=getattr(args, "resume_profile", None),
        worktree=args.worktree, allow_credit=getattr(args, "allow_credit", False))


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
    # Primary emission point: guarantee the exit-code-clarity fields on EVERY
    # dispatch-shaped envelope routed here, including the pre-dispatch validation/
    # preflight paths that never reach the executor's _stamp. (The two paths that
    # can't route through here -- the pre-import Python-version guard and the
    # last-resort crash handler -- inline the fields themselves.) Idempotent +
    # no-op on query envelopes (list/doctor/version have no exit_code).
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


def _inject_memory(system_context: str, cwd: str, raw: bytes | None = None) -> str:
    """Append {cwd}/.agents/memory.md to the agent's system context (capped).

    `raw` lets the caller pass the BYTES it already hashed, so the text injected here is the
    text that was attested. Reading the file again would leave the window where memory A is
    fingerprinted and memory B is what the agent actually runs under.
    """
    mem_path = os.path.join(cwd, ".agents", "memory.md")
    if raw is not None:
        mem = raw.decode("utf-8", errors="replace")
    else:
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
        print(json.dumps({
            "models": discover_models(args.cli),
            "note": "model lists are static/config- or live-endpoint-derived and do NOT verify "
                    "ACCOUNT eligibility; a listed backend can still fail a real dispatch "
                    "(e.g. an ineligible client tier). Run `doctor --probe` to confirm eligibility.",
        }, ensure_ascii=False))
        sys.exit(0)

    if args.doctor:
        from _doctor import doctor, render  # local import: keeps dispatch path lean
        report = doctor(args.agents_dir, args.cwd, probe=getattr(args, "probe", False))
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
        # --out is the AUTHORITATIVE result path, so a pre-dispatch failure has to land
        # there too. Emitting only to stdout left that path EMPTY after a refused stale
        # success was archived -- the old answer correctly gone, but the new failure
        # recorded nowhere a consumer of the file would look.
        # Not under --background (which never owns --out; writing there while REJECTING the
        # combination would be self-contradictory, and it littered a file into the caller's
        # cwd) and not under --dry-run (which promises to touch nothing).
        if (getattr(args, "out", None) and not getattr(args, "dry_run", False)
                and not getattr(args, "background", False)):
            try:
                _write_error_out(args.out, env)
            except Exception:  # noqa: BLE001 — reporting the original error matters more
                pass
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

    # --out resume behavior: a pre-existing SUCCESS envelope means this job is
    # already done — emit it (marked skipped) and exit without dispatching. A
    # prior error/blocked/partial envelope is NOT terminal: re-running retries
    # it (matches the manifest's resume semantics — failures get another shot).
    # A SUSPECT success (status=success but report_ok=false -> suspect=true) is
    # NOT terminal either: skipping it would strand a semantically-useful but
    # unparseable envelope, forcing a manual delete/rename to re-run. Re-dispatch
    # it instead (consistent with summon's existing "suspect => re-dispatch" stance).
    #
    # The skip ALSO has to verify the prior envelope answers THIS request. Keyed by path
    # alone it returned a stale answer as a fresh one: edit a manifest job's prompt but keep
    # its id and the job is `skipped` with the OLD result, and on Windows two case-different
    # job ids share one result file. So compare the receipt prompt hash and the agent -- a
    # mismatch re-dispatches. This runs AFTER --prompt-file resolution because the hash is
    # only final once the prompt is. An envelope written before 0.10.2 carries no
    # prompt_sha256; it is still honored (resume keeps its value) but says so in `warnings`,
    # because "unknown" must not be reported as "verified".
    # The request fingerprint joins the receipt too, so EVERY envelope this dispatch writes
    # carries what it answered -- that is what a later resume compares against.
    _identity = _request_identity(args)
    request_sha = request_fingerprint(**_identity)
    receipt["request_sha256"] = request_sha
    if args.out and os.path.isfile(args.out) and not args.dry_run:
        try:
            with open(args.out, encoding="utf-8") as fh:
                prior = json.load(fh)
        except (OSError, ValueError):
            prior = None
        if is_terminal_success(prior):
            _reusable, _note = envelope_answers_request(
                prior, request_sha, receipt.get("prompt_sha256"), args.agent, _identity)
            # A BARE --worktree auto-names a FRESH tree on every invocation, so no stored
            # result was produced in the tree this run will use. A fingerprint cannot express
            # that (it has to be deterministic to be comparable), so it is decided here.
            if args.worktree == "":
                _reusable = False
            if not _reusable:
                print(f"[out] {args.out} holds a success for a DIFFERENT request; "
                      "re-dispatching instead of skipping", file=sys.stderr, flush=True)
                # ARCHIVE it now, exactly as the manifest does. Leaving it in place meant a
                # pre-dispatch failure (a missing agent, a bad schema) exited with an error
                # while the stale success was still sitting at the authoritative path, ready
                # to be read as this run's result by anything that looks there.
                from _manifest import _clear_out_file
                _clear_err = _clear_out_file(args.out, archive=True)
                if _clear_err:
                    _die(_clear_err)
            else:
                if _note:
                    _pw = prior.get("warnings")
                    _pw = _pw if isinstance(_pw, list) else ([] if _pw is None else [str(_pw)])
                    prior["warnings"] = _pw + [_note]
                prior["skipped"] = True
                _emit(prior)
                sys.exit(0)

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
            # Read the BYTES once, hash THEM, then parse from the same buffer -- the
            # identity fingerprinted this file earlier, and re-reading it here would leave
            # the window (including A -> B -> A) in which one schema is fingerprinted and a
            # different one actually validates the result.
            _schema_raw = Path(args.json_schema).read_bytes()
            schema = json.loads(_schema_raw.decode("utf-8"))
            if not isinstance(schema, dict):
                raise ValueError("schema root must be a JSON object")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            _die(f"--json-schema: cannot load {args.json_schema}: {e}")
        # Compared unconditionally, so a schema that was ABSENT at fingerprint time but
        # exists by now is caught too -- it would otherwise validate the result while
        # contributing nothing to the identity that names it.
        _sch_expected = _identity.get("json_schema_sha256")
        _sch_actual = hashlib.sha256(_schema_raw).hexdigest()
        if True:
            if _sch_actual != _sch_expected:
                _die(f"--json-schema {args.json_schema} changed between fingerprinting and "
                     f"dispatch ({_sch_expected} -> {_sch_actual}); re-run rather than "
                     "validate against a contract the envelope does not name")

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

    # The identity hashed this definition BEFORE dispatch; this is the copy that will
    # actually be executed. If it changed in between, the run would use one definition's
    # model, permission and system context while being stamped with another's request hash
    # -- and restoring the first would then let the second's answer resume as its own. Same
    # attestation shape as the agy account profile and the openai-compat endpoint snapshot.
    # UNCONDITIONAL (not only when a hash was recorded): a definition that did NOT resolve
    # when the identity was built but DOES by dispatch would otherwise run under an identity
    # naming none of it. agent_file is set (load_agent succeeded), so the actual hash is
    # real; the expected side is None exactly when the identity found no definition, and
    # None != a real hash is the mismatch we want. Only a pre-0.10.2 caller (no agent field
    # in its identity at all) is exempt.
    from _loader import last_parsed_sha
    _def_expected = _identity.get("agent_def_sha256")
    if _identity.get("agent") is not None:
        # The bytes load_agent PARSED, not a fresh read of the path: re-reading made this
        # ABA-vulnerable, since a file changed to B and restored to A around the parse
        # matched on the re-read while B was what got loaded.
        _def_actual = last_parsed_sha(agent_file) or _executor.content_sha(agent_file)
        if _def_actual != _def_expected:
            _die(f"agent definition {args.agent!r} changed between fingerprinting and "
                 f"dispatch ({_def_expected} -> {_def_actual}); re-run rather than record "
                 "a result under a definition that was not used")

    receipt.update(_receipt.receipt_agent(args, agent_file))

    # Shared project memory: inject {cwd}/.agents/memory.md (standing conventions,
    # constraints, durable decisions) so callers don't re-explain project context
    # every prompt. Read from the ORIGINAL cwd (before any worktree rewrite).
    # Skipped on resume — the session already carries it.
    if not args.resume:
        # Same attestation as the definition and the schema: the identity hashed
        # .agents/memory.md earlier, and it is read again here to build the system context,
        # so a change in between would inject instructions the envelope does not name.
        # Read ONCE, here: hash these bytes and inject THESE bytes. Attesting one read and
        # injecting another left the very window this check exists to close. The comparison
        # covers ABSENT too -- a memory file that did not exist at fingerprint time but does
        # by now would otherwise be used while contributing nothing to the identity.
        _mem_path = os.path.join(args.cwd, ".agents", "memory.md")
        try:
            _mem_raw = Path(_mem_path).read_bytes()
        except OSError:
            _mem_raw = None
        _mem_actual = hashlib.sha256(_mem_raw).hexdigest() if _mem_raw is not None else None
        _mem_expected = _identity.get("memory_sha256")
        if _mem_actual != _mem_expected:
            _die("project memory (.agents/memory.md) changed between fingerprinting and "
                 f"dispatch ({_mem_expected} -> {_mem_actual}); re-run rather than record a "
                 "result under instructions the envelope does not name")
        system_context = _inject_memory(system_context, args.cwd, _mem_raw)

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
            # --out is AUTHORITATIVE, and this path bypassed _die() (which writes there), so
            # a preflight failure after a refused stale success was archived left that path
            # absent entirely -- the old answer correctly gone, the new failure nowhere a
            # reader of the file would find it.
            if args.out and not args.background:
                try:
                    _write_out(args.out, setup_error)
                except Exception:  # noqa: BLE001 — reporting the error matters more
                    pass
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
        try:
            base_url, api_key_env = _endpoint_for_dispatch(_identity, agent_file, agents_dir)
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
        # the account digest the identity recorded, so the dispatch can verify the profile
        # it builds carries the SAME bytes
        agy_account_sha256=_identity.get("agy_account_sha256"),
        agy_account_checked=bool(_identity.get("_agy_account_checked")),
        api_key_env=api_key_env,
    )

    if args.dry_run:
        _emit(_dry_run_view(invocation, args, agents_dir))
        sys.exit(0)

    # --gate-with: another agent must APPROVE this dispatch before it runs. Placed
    # BEFORE git_head_before and the dispatch itself so a refused request never
    # touches the tree. Fails closed inside _gate.decide.
    if getattr(args, "gate_with", None):
        _gate_decision = _run_gate(args, agents_dir, invocation)
        if not _gate_decision.get("approved"):
            from _gate import blocked_envelope
            _env = blocked_envelope(_gate_decision, agent=args.agent, cli=cli)
            _env["request_sha256"] = receipt.get("request_sha256")
            finalize_exit_fields(_env)
            if args.out:
                _write_out(args.out, _env)
            _emit(_env)
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
    if getattr(args, "gate_with", None):
        result["gate"] = _gate_decision

    # --json-schema: structured-output contract with ONE corrective retry.
    if schema is not None and result.get("status") == "success":
        result = _apply_schema(result, schema, invocation, args)

    # Report-contract auto-repair: a suspect success (status=success but the report
    # block is malformed -> report_ok false) gets ONE constrained corrective resume,
    # unless disabled. Cheaper and more targeted than the caller re-dispatching the
    # whole task. Runs after --json-schema so a still-suspect envelope gets one shot.
    if not getattr(args, "no_contract_repair", False):
        result = _apply_contract_repair(result, invocation, args)

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


def _run_gate(args, agents_dir, gated_inv) -> dict:
    """Dispatch the --gate-with agent to adjudicate this request. Returns a gate
    decision dict (see _gate.decide); FAILS CLOSED on every failure path.

    The gate is forced to ``read-only`` REGARDLESS of what its own definition
    declares. A gate that could write would be a privilege-escalation path: the
    caller could name a yolo profile as its own approver and gain write access
    through the approval step itself.
    """
    from _builder import AgentInvocation
    from _gate import decide, gate_prompt
    from _loader import load_agent

    try:
        tup = load_agent(agents_dir, args.gate_with)
    except Exception as e:  # noqa: BLE001 — an unusable gate must REFUSE, not pass
        return decide(None, args.gate_with) | {
            "reason": f"gate agent {args.gate_with!r} could not be loaded: {e}"}

    gate_cli, gate_ctx, _desc, gate_file, _perm, gate_model, gate_args, gate_effort = tup
    try:
        from _resolver import resolve_cli
        gate_cli = resolve_cli(gate_cli)
    except Exception:  # noqa: BLE001
        pass
    if gate_cli == args.cli and gate_cli is not None:
        print(f"note: --gate-with {args.gate_with!r} resolves to the same backend "
              f"({gate_cli}) as the gated dispatch; a same-vendor gate shares the "
              f"caller's blind spots", file=sys.stderr)

    prompt = gate_prompt(agent=args.agent, prompt=args.prompt, cwd=args.cwd,
                         permission=gated_inv.permission, cli=gated_inv.cli,
                         model=gated_inv.model)
    gate_inv = AgentInvocation(
        cli=gate_cli, prompt=prompt, cwd=args.cwd, system_context=gate_ctx,
        agent_file=gate_file,
        permission="read-only",   # FORCED: never inherit the gate definition's tier
        model=gate_model, effort=gate_effort, extra_args=tuple(gate_args or ()),
    )
    timeout = args.gate_timeout or args.timeout
    if isinstance(timeout, str):
        from _cli import parse_timeout
        timeout = parse_timeout(timeout)
    try:
        resp = execute_agent(gate_inv, timeout_ms=timeout, debug_dir=args.debug_dir)
    except Exception as e:  # noqa: BLE001 — a crashed gate REFUSES
        return decide(None, args.gate_with) | {
            "reason": f"gate dispatch failed: {type(e).__name__}: {e}"}
    return decide(resp, args.gate_with)


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
    from dataclasses import replace as _replace

    from _schema import attach_parsed, correction_prompt
    attach_parsed(result, schema)
    if result["parse_ok"]:
        return result
    resume = result.get("resume") or {}
    sid, profile = resume.get("session_id"), resume.get("profile")
    if not sid and not profile:
        return result  # no resume lane (e.g. gemini) — return the verdict as-is
    # CLONE the original invocation and override only the retry-specific fields, so every
    # other field -- including the agy attestation fields, and anything added later -- rides
    # along. Reconstructing by hand silently dropped whatever the constructor call omitted.
    retry_inv = _replace(
        invocation,
        prompt=correction_prompt(schema, result.get("parse_errors") or []),
        system_context="",  # resume: session already holds the definition
        resume_id=sid or "latest",
        resume_profile=profile or invocation.resume_profile,
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
        # additional work, not a reset) so cost accounting stays honest, and fold
        # the ORIGINAL call's spend into the returned envelope (the first call was
        # paid for too -- otherwise a schema repair silently under-reports spend).
        retry["attempts"] = result.get("attempts", 1) + retry.get("attempts", 1)
        _aggregate_spend(retry, result)
        return retry
    # Rejected: keep the original, but the failed corrective call was still spent.
    result["attempts"] = result.get("attempts", 1) + retry.get("attempts", 1)
    _aggregate_spend(result, retry)
    return result


_CONTRACT_REPAIR_PROMPT = (
    "Your previous reply was accepted but did NOT include a valid report contract, "
    "so it is flagged as unverifiable. Re-emit your report, ending with EXACTLY this "
    "block (all four fields, each on its own line):\n\n"
    "STATUS: DONE | PARTIAL | BLOCKED\n"
    "SUMMARY: <one line>\n"
    "FOLLOW-UP: <the next step, or 'none'>\n"
    "HANDOFF: <what the next agent needs, or 'none'>\n\n"
    "IMPORTANT: STATUS is the EXECUTION status of the task (use DONE if you "
    "finished), NOT your decision or verdict. If your decision was a verdict such as "
    "APPROVE, BLOCK, or a recommendation, put it in SUMMARY (and in FINDINGS if you "
    "use them) -- never in STATUS. Keep all of your analysis; just make sure the "
    "exact block above is present."
)


def _aggregate_spend(result: dict, retry: dict) -> None:
    """Fold the corrective call's cost/usage into ``result`` so total spend stays
    honest whether or not the retry is accepted (the call happened either way)."""
    rc, tc = result.get("cost_usd"), retry.get("cost_usd")
    if isinstance(rc, (int, float)) or isinstance(tc, (int, float)):
        result["cost_usd"] = (rc or 0) + (tc or 0)
    ru, tu = result.get("usage"), retry.get("usage")
    if isinstance(ru, dict) or isinstance(tu, dict):
        merged = dict(ru or {})
        for k, v in (tu or {}).items():
            if isinstance(v, (int, float)) and isinstance(merged.get(k), (int, float)):
                merged[k] += v
            else:
                merged.setdefault(k, v)
        result["usage"] = merged


def _apply_contract_repair(result: dict, invocation, args) -> dict:
    """A dispatch that SUCCEEDED but whose report contract is malformed (report_ok
    false -> suspect) gets ONE constrained corrective resume that re-emits the exact
    contract, teaching STATUS = execution state (not a decision verdict).

    The original envelope is PRESERVED: on accept, only the STRUCTURED contract
    (report/report_ok/status) is overlaid; the original `result` text is kept
    verbatim (so its full analysis survives a terse re-emit), and the corrected
    block is stored in `repaired_report_text`. Schema output, warnings, and model
    survive; spend/attempts aggregate across BOTH calls whether the retry is
    accepted or rejected. The corrective resume runs READ-ONLY with no inherited
    extra_args (a formatting re-emit needs no write/tool authority and must not
    repeat the task's side effects). No resume lane -> no-op."""
    if not (result.get("status") == "success" and not result.get("report_ok")):
        return result
    resume = result.get("resume") or {}
    sid, profile = resume.get("session_id"), resume.get("profile")
    if not sid and not profile:
        return result  # no resume lane (e.g. gemini) — leave the suspect verdict as-is
    from dataclasses import replace as _replace
    # CLONE, then override the retry-specific fields. permission and extra_args are
    # DELIBERATELY overridden (see below); every OTHER field -- agy attestation included --
    # is inherited, so a new field can never be silently dropped from this path again.
    retry_inv = _replace(
        invocation,
        prompt=_CONTRACT_REPAIR_PROMPT,
        system_context="",  # resume: session already holds the definition
        permission="read-only",   # formatting re-emit: no write/yolo/tool authority, no side effects
        resume_id=sid or "latest",
        resume_profile=profile or invocation.resume_profile,
        extra_args=(),   # DROP extra_args: a resume keeps the session's model, and a
                         # stray permission-override flag (--dangerously-bypass...,
                         # --permission-mode bypassPermissions) would defeat read-only.
    )
    try:
        retry = execute_agent(retry_inv, timeout_ms=args.timeout, debug_dir=args.debug_dir,
                              max_tool_output_bytes=getattr(args, "max_tool_output_bytes", None))
    except ValueError:
        return result  # resume unsupported on this backend: keep the first verdict, no call made
    # A corrective call was spent EITHER WAY -> account for attempts + spend honestly.
    result["attempts"] = result.get("attempts", 1) + retry.get("attempts", 1)
    _aggregate_spend(result, retry)
    # Accept a retry that produced a VALID contract and did not error/time out. A
    # truthful DONE **or** PARTIAL/BLOCKED (report_ok true) is better than the
    # original suspect success; only an errored/timed-out or still-malformed retry
    # is rejected.
    if retry.get("report_ok") and retry.get("status") in ("success", "partial", "blocked"):
        # Overlay ONLY the structured contract; KEEP the original `result` text so
        # the agent's full analysis is never lost to a terse corrective re-emit.
        # `report`/`report_ok` are the authoritative parsed view.
        result["report"] = retry.get("report")
        result["report_ok"] = True
        result["status"] = retry.get("status")    # DONE->success; PARTIAL/BLOCKED reflected
        result.pop("suspect", None)
        result["contract_repaired"] = True
        result["repaired_report_text"] = retry.get("result")   # the corrected block, for reference
        if retry.get("resume"):
            result["resume"] = retry["resume"]     # latest session id for follow-ups
        if retry.get("warnings"):
            result["warnings"] = (result.get("warnings") or []) + retry["warnings"]
    else:
        result["contract_repair_attempted"] = True   # a call was spent; it did not improve
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


def _crash_envelope(e: BaseException) -> dict:
    """The last-resort crash envelope. The exit-code-clarity fields are inlined
    (not via finalize_exit_fields) so a crash in the executor import path can't
    sink the net. A module-level function so the shape stays testable."""
    return {"result": "", "status": "error", "exit_code": 1,
            "error": f"uncaught {type(e).__name__}: {e}",
            "backend_exit_code": 1, "dispatcher_status": "error",
            "normalization_reason": f"uncaught {type(e).__name__} before completion"}


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # intentional exits (validation, normal completion) pass through
    except BaseException as e:  # noqa: BLE001 — last-resort net so a bg job never orphans
        err = _crash_envelope(e)
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
