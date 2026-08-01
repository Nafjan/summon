#!/usr/bin/env python3
"""run_subagent.py - Execute external CLI AIs as sub-agents.

Usage:
    scripts/run_subagent.py --agent <name> --prompt "<task>" --cwd <path>
    scripts/run_subagent.py --list

Supported CLIs: claude, cursor-agent, codex, gemini, kimi, agy.

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
from _builder import clamp_permission as _clamp  # noqa: E402
from _executor import ENVELOPE_VERSION as _ENVELOPE_VERSION  # noqa: E402
from _executor import (agent_def_sha, content_sha,  # noqa: E402
                       envelope_answers_request, execute_agent, finalize_exit_fields,
                       is_terminal_success, request_fingerprint)
from _loader import bundled_roster_dir, get_agents_dir, list_agents, load_agent  # noqa: E402
from _resolver import discover_models, resolve_cli  # noqa: E402

__version__ = "1.0.0"  # summon dispatcher version (see CHANGELOG.md)

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


def _transport_for_dispatch(agent_file: str | None, cli_arg: str | None) -> str:
    """Resolve the dispatch transport: --transport flag > agent `transport:`
    frontmatter > "subprocess". Raises ValueError on an unknown value.

    Re-reads the frontmatter like _compat_endpoint (utf-8-sig, so a BOM cannot
    hide the field) instead of widening load_agent's return tuple, which has
    many callers."""
    fm_transport = None
    if agent_file:
        from _loader import parse_frontmatter
        with open(agent_file, encoding="utf-8-sig") as fh:
            fm, _ = parse_frontmatter(fh.read())
        raw = fm.get("transport")
        if raw is not None:
            fm_transport = str(raw).strip().lower()
    transport = (cli_arg or fm_transport or "subprocess").lower()
    if transport not in ("subprocess", "acp"):
        raise ValueError(f"invalid transport {transport!r}: use 'subprocess' or 'acp'")
    return transport


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
        worktree=args.worktree, allow_credit=getattr(args, "allow_credit", False),
        gate_with=getattr(args, "gate_with", None),
        max_permission=getattr(args, "max_permission", None),
        artifacts=getattr(args, "artifacts", None))


def _complete_artifact_provenance(env: dict, args, before: dict | None) -> dict:
    """Attach the post-dispatch stability check for an opt-in artifact baseline."""
    if not before:
        return env
    from _artifacts import build_manifest, changed_paths
    after, error = build_manifest(getattr(args, "artifacts", None), args.cwd)
    evidence = dict(before)
    stable = bool(after and not error and after.get("sha256") == before.get("sha256"))
    evidence["stable_during_dispatch"] = stable
    evidence["after_sha256"] = after.get("sha256") if after else None
    # If the after-read failed, the changed set is UNKNOWN rather than "every
    # input changed". Keep the result suspect, but do not manufacture a change
    # claim that the failed read cannot support.
    evidence["changed"] = (
        None if after is None
        else changed_paths(before, after) if not stable
        else []
    )
    if error:
        evidence["after_error"] = error
    env["artifacts"] = evidence
    if not stable:
        if error:
            warning = (
                "artifact provenance could not be verified after dispatch (%s); "
                "the review does not describe one verified stable loose-file baseline"
                % error)
        else:
            changed = ", ".join(evidence["changed"]) or "the named baseline"
            warning = (
                "artifact provenance changed during dispatch (%s); the review does not "
                "describe one stable loose-file baseline" % changed)
        env.setdefault("warnings", []).append(warning)
        # Keep the executor outcome honest while preventing --out/manifest from
        # treating the review as terminal evidence for an unstable corpus.
        if env.get("status") == "success":
            env["suspect"] = True
    return env


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
    from _spawn import run_flags
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, **run_flags())
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
                       f"refs/heads/{branch}"], capture_output=True, text=True, **run_flags()).returncode == 0:
        raise ValueError(f"branch {branch} already exists; pick a different --worktree name "
                         "(its commits would otherwise be at risk)")
    r2 = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch, wt, "HEAD"],
                        capture_output=True, text=True, **run_flags())
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
    _head = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                           capture_output=True, text=True, **run_flags())
    # Cleanup fails closed when this evidence is unavailable: without the exact
    # commit the new branch began at, a later clean status cannot distinguish
    # "untouched" from "the gate/misbehaving peer made a commit".
    base_head = _head.stdout.strip() if _head.returncode == 0 else None
    # `repo` so a later teardown can run `git -C <repo> worktree remove`: git rejects
    # a bare path outside the repository context, which is how the first attempt at
    # denial cleanup silently failed.
    return {"path": wt, "cwd": effective, "branch": branch, "repo": repo,
            "base_head": base_head}


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
    if args.worktree is not None and getattr(args, "artifacts", None):
        _die("--artifact and --worktree are incompatible: loose files are measured under "
             "the original --cwd but an isolated worktree may not contain them")

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
    _artifact_manifest = _identity.get("_artifact_manifest")
    if _artifact_manifest:
        # Before-only on refusal/preflight paths; a completed dispatch replaces this
        # with a before/after stability record below.
        receipt["artifacts"] = dict(_artifact_manifest,
                                    stable_during_dispatch=None,
                                    after_sha256=None)
    if _identity.get("_artifact_error"):
        _die("--artifact: " + str(_identity["_artifact_error"]))
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
        # Roster-level tier lint. Per-dispatch refusal is correct but arrives too late for
        # anyone maintaining a roster as a controlled artifact: a definition whose declared
        # tier its backend cannot enforce sits unnoticed until someone dispatches it (field
        # report, 2026-07-28 -- two of the offenders there were named reviewers).
        from _builder import roster_permission_lint
        _out = {"agents": agents, "agents_dir": agents_dir}
        _lint = roster_permission_lint(agents)
        if _lint:
            _out["roster_warnings"] = _lint
        print(json.dumps(_out, ensure_ascii=False))
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
    # Units validation runs BEFORE any side effect: `--worktree` creates a branch
    # and a checkout below, and a units error used to leave both behind
    # (certification round 3). Nothing to clean up if nothing was created.
    # `--gate-timeout` reaches the backend through _run_gate -> execute_agent, which does
    # NOT pass back through this function, so it bypassed the guard entirely and a gate
    # could be handed a 300ms budget (certification round 3). Same rule, both flags.
    for _flag, _val in (("--timeout", args.timeout),
                        ("--gate-timeout", getattr(args, "gate_timeout", None))):
        if getattr(_val, "bare_sub_second", False):
            _n = int(_val)
            _die(f"{_flag} {_n} means {_n} MILLISECONDS, which no dispatch can complete in "
                 f"-- every agent would be killed almost immediately. Did you mean {_n}s? "
                 f"Bare values are milliseconds for backward compatibility (600000 == 10m); "
                 f"write {_n}ms explicitly if you really want it.")

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

    # Transport: --transport flag > agent `transport:` frontmatter > subprocess.
    # ACP requires NATIVE backend support; asking for it anywhere else is a
    # configuration error, not a dispatch failure.
    try:
        transport = _transport_for_dispatch(agent_file, getattr(args, "transport", None))
    except ValueError as e:
        _die(str(e))
    if transport == "acp":
        from _builder import supports_acp as _supports_acp
        if not _supports_acp(cli):
            _die(f"backend {cli!r} has no acp transport "
                 f"(native ACP: gemini, kimi, cursor-agent)")
    # The kill switch is process-wide: the executor's oversized-prompt ACP
    # routing reads the env var and never sees args, so the flag writes it here.
    if getattr(args, "no_acp_fallback", False):
        os.environ["SUMMON_ACP_FALLBACK"] = "0"

    # A bare sub-second --timeout is a units mistake on a DISPATCH: bare values are
    # milliseconds for backward compatibility, so `--timeout 300` means 0.3s and kills every
    # agent almost immediately. A four-member council lost a whole run to this (field
    # report, 2026-07-27). Checked HERE rather than in the parser, because `jobs wait
    # --timeout 300` is a legitimate non-blocking poll and must keep working.
    if getattr(args.timeout, "bare_sub_second", False):
        _ms = int(args.timeout)
        _die(f"--timeout {_ms} means {_ms} MILLISECONDS, which no dispatch can complete in "
             f"-- every agent would be killed almost immediately. Did you mean {_ms}s? Bare "
             f"values are milliseconds for backward compatibility (600000 == 10m); write "
             f"{_ms}ms explicitly if you really want it.")

    invocation = AgentInvocation(
        cli=cli,
        prompt=args.prompt,
        cwd=args.cwd,
        system_context=system_context,
        agent_file=agent_file,
        # --max-permission CLAMPS downward only (see _builder.clamp_permission): an agent
        # declaring read-only stays read-only. extra_args are dropped alongside it because
        # an agent's `args:` is appended AFTER the permission flags and could otherwise
        # carry --dangerously-skip-permissions and defeat the clamp -- the same hole that
        # made a gate's own args a privilege-escalation path.
        permission=_clamp(permission, getattr(args, "max_permission", None)),
        # A CLAMP is summon reducing privilege on the caller's instruction, so like the gate
        # it must not be waivable by an ambient opt-in. Marked forced only when the clamp
        # actually bit -- a request that was already at or below the ceiling was not forced
        # anywhere, and calling it forced would refuse dispatches the caller chose freely.
        permission_forced=bool(
            getattr(args, "max_permission", None)
            and _clamp(permission, args.max_permission) != permission),
        model=final_model,               # incl. agy Gemini thinking-mode suffix
        effort=effort,                    # --effort > frontmatter > env > default(high)
        resume_id=args.resume,
        resume_profile=args.resume_profile,
        extra_args=(() if getattr(args, "max_permission", None)
                    else tuple(extra_args)),
        base_url=base_url,
        transport=transport,             # --transport > frontmatter > subprocess
        # the account digest the identity recorded, so the dispatch can verify the profile
        # it builds carries the SAME bytes
        agy_account_sha256=_identity.get("agy_account_sha256"),
        agy_account_checked=bool(_identity.get("_agy_account_checked")),
        api_key_env=api_key_env,
    )

    if args.dry_run:
        _emit(_dry_run_view(invocation, args, agents_dir, agent_file,
                            artifact_manifest=_artifact_manifest))
        sys.exit(0)

    # --gate-with: another agent must APPROVE this dispatch before it runs. Placed
    # BEFORE git_head_before and the dispatch itself so a refused request never
    # touches the tree. Fails closed inside _gate.decide.
    if getattr(args, "gate_with", None):
        # A gate authorises ONE execution. --retries would otherwise run the task
        # again (up to N times) on a single approval, which for a side-effecting
        # task is materially more than what was approved. Each attempt re-gates.
        _gate_decision = _run_gate(args, agents_dir, invocation)
        if not _gate_decision.get("approved"):
            from _gate import blocked_envelope
            _env = _enrich_denial(
                blocked_envelope(_gate_decision, agent=args.agent, cli=cli),
                receipt, invocation)
            # A DENIED dispatch must not leave a branch and checkout behind. --worktree runs
            # ~90 lines BEFORE the gate, so a DENY still created `.claude/worktrees/<name>`
            # and `refs/heads/agents/<name>`. The gate exists to authorise side effects, and
            # one had already happened; removing it is the honest completion of the refusal.
            if worktree_info:
                _cleanup = _remove_worktree(worktree_info)
                _env["worktree_removed"] = _cleanup["worktree_removed"]
                _env["worktree_cleanup"] = _cleanup
                if _cleanup["preserved"]:
                    _env["worktree_preserved"] = True
                    _env.setdefault("warnings", []).append(
                        "the gate denied this dispatch, but summon preserved its --worktree "
                        "because removing it could lose work (%s). Inspect %s and branch %s"
                        % (_cleanup["reason"], _cleanup["path"], _cleanup["branch"]))
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
        result = _dispatch_with_retries(invocation, args, agents_dir)
    except ValueError as e:
        _die(str(e))
    if getattr(args, "gate_with", None) and "gate" not in result:
        # Only stamp the INITIAL approval when the dispatch did not already carry a
        # gate decision. _dispatch_with_retries attaches its own when a RETRY gate
        # refuses, and overwriting that with the initial approval produced an
        # envelope that read `blocked` while recording gate.approved=true -- false
        # evidence, which is precisely what the gate exists to make impossible.
        result["gate"] = _gate_decision

    # --json-schema: structured-output contract with ONE corrective retry.
    if schema is not None and result.get("status") == "success":
        result = _apply_schema(result, schema, invocation, args, agents_dir)

    # Report-contract auto-repair: a suspect success (status=success but the report
    # block is malformed -> report_ok false) gets ONE constrained corrective resume,
    # unless disabled. Cheaper and more targeted than the caller re-dispatching the
    # whole task. Runs after --json-schema so a still-suspect envelope gets one shot.
    if not getattr(args, "no_contract_repair", False):
        result = _apply_contract_repair(result, invocation, args, agents_dir)

    # Receipt LAST, from main()-scope values: a schema-correction retry replaces
    # the envelope, and this keeps prompt_sha256 bound to the ROOT prompt (the
    # correction prompt must never restamp it).
    result.update(receipt)
    _complete_artifact_provenance(result, args, _artifact_manifest)
    # An explicit --agents-dir that fell through to the BUNDLED roster is an intent
    # violation: the caller named a directory and got something else. `--agents-dir` selects
    # which directory is SEARCHED; only `agent_def.source` proves provenance -- and a real
    # governance control was written on the other belief (field report, 2026-07-28). Warn on
    # the real dispatch, not just --dry-run, because the dispatch is what gets audited.
    try:
        from _loader import explicit_dir_fallback_warning
        _fw = explicit_dir_fallback_warning(args.agents_dir, agent_file)
        if _fw and _fw not in (result.get("warnings") or []):
            result.setdefault("warnings", []).append(_fw)
    except Exception:  # noqa: BLE001 - provenance advisory must never break a dispatch
        pass

    if worktree_info:
        result["worktree"] = worktree_info
    if args.out:
        _write_out(args.out, result)
    _emit(result)
    sys.exit(0 if result["status"] == "success" else 1)


def _dry_run_view(invocation, args, agents_dir: str,
                  agent_file: str | None = None,
                  artifact_manifest: dict | None = None) -> dict:
    """The fully resolved dispatch, without executing. For agy the per-call
    profile is NOT built (that copies OAuth tokens = a mutation); the wrapper
    path is shown instead."""
    from _builder import (BACKENDS, backend_kind, build_invocation_args,
                          permission_flags as _pf, _PERMISSION_MAPPING, _agy_wrapper,
                          advisory_warnings, apply_credit_guard, infer_dispatch_billing,
                          credit_spend_allowed, selects_credit_only)
    _guarded, _, _guard_warnings = apply_credit_guard(invocation)
    _eff_model = _guarded.model
    # Predict the billing source so preflight can reveal a charge (mirrors _stamp).
    _bill = infer_dispatch_billing(invocation.cli, invocation.model,
                                   invocation.extra_args)
    if invocation.cli == "claude":
        if invocation.resume_id:
            _bill = {
                "source": "unknown",
                "note": "resumed Claude session keeps its original model; billing "
                        "cannot be inferred before terminal model evidence",
            }
        elif (selects_credit_only(invocation.model, invocation.extra_args)
              and credit_spend_allowed()):
            _bill = {"source": "api" if os.environ.get("ANTHROPIC_API_KEY") else "credit",
                     "note": "credit-only model authorized"}
    view = {
        "dry_run": True,
        "agent": args.agent,
        "cli": invocation.cli,
        "cwd": invocation.cwd,
        "agents_dir": agents_dir,
        "model_requested": invocation.model,
        "model_effective": _eff_model,  # after any credit-only-model fallback
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
    if artifact_manifest:
        view["artifacts"] = dict(artifact_manifest,
                                 stable_during_dispatch=None,
                                 after_sha256=None)
    for _w in _guard_warnings:  # credit-only guard actions surfaced in the preview
        view.setdefault("warnings", []).append(_w)
    # A dispatch that will be REFUSED must say so in preflight. Surfaced as `would_refuse`
    # plus `error` rather than a warning, because it is not advice: the run does not happen.
    from _builder import readonly_unenforceable_error as _refuse
    _ro = _refuse(invocation.cli, invocation.permission,
                  forced=getattr(invocation, "permission_forced", False))
    if _ro:
        # Its OWN key, not `error`. In this view `error` means "the preview could not be
        # built" (e.g. no agy wrapper on this OS) and is set later by the backend branches,
        # which clobbered the refusal on Linux. They are different facts and both are worth
        # reporting: the dispatch would be refused, AND the preview is partial.
        view["would_refuse"] = True
        view["refusal"] = _ro
    # Same helper as the real envelope, so preflight shows exactly what the run would
    # warn about -- a short agy clock and a withheld read-only workspace are both things
    # you want to learn BEFORE paying, which is the whole point of --dry-run.
    for _w in advisory_warnings(invocation.cli, invocation.permission, args.timeout,
                                invocation.model, invocation.extra_args):
        view.setdefault("warnings", []).append(_w)
    # An explicit --agents-dir that silently fell through to the bundled roster is an
    # INTENT violation, not a convenience: a governance control was written on the belief
    # that --agents-dir guaranteed provenance (field report, 2026-07-28).
    try:
        from _loader import explicit_dir_fallback_warning
        _fw = explicit_dir_fallback_warning(args.agents_dir, agent_file)
        if _fw:
            view.setdefault("warnings", []).append(_fw)
    except Exception:  # noqa: BLE001 - a preflight view must always render
        pass
    # PROVENANCE PARITY: dry-run reported `agents_dir` (which directory was searched) but
    # not `agent_def` (where the definition actually came from). Those differ in precisely
    # the case worth catching, and dry-run's whole purpose is catching things before paying.
    try:
        from _receipt import receipt_agent
        view.update(receipt_agent(args, agent_file))
    except Exception:  # noqa: BLE001
        pass
    if invocation.transport == "acp":
        # ACP dispatches build no argv (the executor hands the turn to
        # _acpbackend.call); render the transport, not a command line.
        view["command"] = f"{invocation.cli} <acp>"
        view["transport"] = "acp"
    elif backend_kind(invocation.cli) == "api":
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

    # EVERY field comes from the invocation actually being gated, not from args. Taking
    # prompt/cwd from args meant a re-gate (retry, schema correction, contract repair)
    # adjudicated the ORIGINAL task while a DIFFERENT request was dispatched -- the gate
    # approved something nobody was about to run. args is the first dispatch's shape; the
    # invocation is what is about to happen.
    prompt = gate_prompt(agent=args.agent, prompt=gated_inv.prompt, cwd=gated_inv.cwd,
                         permission=gated_inv.permission, cli=gated_inv.cli,
                         model=gated_inv.model)
    gate_inv = AgentInvocation(
        cli=gate_cli, prompt=prompt, cwd=gated_inv.cwd, system_context=gate_ctx,
        agent_file=gate_file,
        permission="read-only",   # FORCED: never inherit the gate definition's tier
        permission_forced=True,   # so the opt-in cannot turn the adjudicator advisory
        model=gate_model, effort=gate_effort,
        # extra_args are DELIBERATELY DROPPED. build_invocation_args appends an
        # agent's `args:` AFTER the permission flags, so a gate definition carrying
        # --dangerously-skip-permissions (claude), --sandbox danger-full-access
        # (codex) or -f (cursor) would defeat the forced read-only tier above and
        # turn the approval step into the escalation path it exists to prevent.
        # A gate adjudicates a request; it is not a configurable dispatch.
        extra_args=(),
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
    # Attach the gate's OWN definition hash. `agent_def` is normally added by main() via
    # _receipt, and _run_gate calls execute_agent directly -- so without this the field the
    # docs advertise as gate evidence ("which definition adjudicated this") was structurally
    # always None. A live gate run is what exposed it: the verdict was right and the
    # provenance was empty.
    try:
        from _loader import last_parsed_sha
        resp.setdefault("agent_def", {"file": gate_file,
                                      "sha256": last_parsed_sha(gate_file)})
    except Exception:  # noqa: BLE001 — evidence is best-effort; never fail the gate on it
        pass
    return decide(resp, args.gate_with)


def _enrich_denial(env: dict, receipt, invocation) -> dict:
    """Put a gate denial's own provenance on the envelope. Used by EVERY denial site.

    A refusal is exactly when a caller needs to know WHICH request and WHICH definition were
    refused, and all of it is resolved before the gate runs. Only fields the receipt ACTUALLY
    holds are copied -- an earlier version copied `cwd` and `agents_dir` from a receipt that
    never stored them, so the denial advertised keys that were silently absent.

    `permission_flags` is deliberately NOT set: those flags describe an execution that did
    not happen, and reporting the target backend's sandbox flags for a refused dispatch
    states something untrue. The TIER is a property of the request, so it is reported.
    """
    try:
        for key in ("request_sha256", "summon", "agent_def", "prompt_sha256",
                    "git_head_before", "artifacts"):
            if receipt and receipt.get(key) is not None:
                env[key] = receipt[key]
        if invocation is not None:
            env["permission"] = invocation.permission
            env["cwd"] = invocation.cwd
    except Exception:  # noqa: BLE001 - evidence is best-effort, never fatal
        pass
    return env


def _remove_worktree(info) -> dict:
    """Safely clean a worktree summon created, preserving any work that appeared.

    Used when a gate DENIES a dispatch: `--worktree` runs before the gate, so a refusal
    would otherwise leave a real branch and checkout behind -- a side effect from a request
    that was never authorised. A concurrent writer or misbehaving gate can touch the tree
    while approval runs. Deletion is allowed only when BOTH the index/worktree are clean
    and HEAD still equals the commit captured at creation. Every ambiguous case preserves
    the checkout and branch.
    """
    result = {
        "worktree_removed": False, "branch_removed": False, "preserved": True,
        "reason": "worktree cleanup lacked creation metadata",
        "path": None, "branch": None,
    }
    if not info:
        return result
    # The worktree ROOT, not `cwd`. `_setup_worktree` sets `cwd` to the SUBDIRECTORY
    # mirroring the caller's original --cwd, so when --cwd was a repo subdirectory `cwd`
    # points INSIDE the checkout and `git worktree remove` fails on it -- the denial then
    # left both the checkout and the branch behind (certification round 3).
    path = info.get("path") or info.get("cwd")
    branch = info.get("branch")
    result["branch"] = branch
    if not path:
        return result
    from _spawn import run_flags
    repo = info.get("repo")
    # `git -C <repo>`: git refuses `worktree remove` on a bare path outside the repository
    # context ("fatal: ... is not a working tree"), so the first version of this removed
    # nothing. It did report that honestly -- the return value is a real existence check,
    # which is how the test caught it -- but an accurate report of a broken teardown still
    # leaves the checkout on disk. Normalise separators too: the stored path mixes forward
    # and back slashes on Windows.
    target = os.path.normpath(str(path))
    result["path"] = target
    base = ["git"] + (["-C", str(repo)] if repo else [])
    if not repo or not branch or not info.get("base_head"):
        return result
    # The helper receives only internal metadata, but keep the destructive target
    # bounded to the directory _setup_worktree owns.
    try:
        owned_root = os.path.realpath(os.path.abspath(
            os.path.join(str(repo), ".claude", "worktrees")))
        resolved_target = os.path.realpath(os.path.abspath(target))
        if os.path.commonpath((owned_root, resolved_target)) != owned_root:
            result["reason"] = "worktree path is outside summon's owned worktree directory"
            return result
    except ValueError:
        result["reason"] = "worktree path is outside summon's owned worktree directory"
        return result

    try:
        status = subprocess.run(["git", "-C", target, "status", "--porcelain=v1",
                                 "--untracked-files=all"], capture_output=True, text=True,
                                timeout=30, **run_flags())
        head = subprocess.run(["git", "-C", target, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30, **run_flags())
    except Exception as e:  # noqa: BLE001
        result["reason"] = "could not verify worktree identity: %s: %s" % (
            type(e).__name__, e)
        return result
    if status.returncode != 0 or head.returncode != 0:
        result["reason"] = "could not verify worktree status/HEAD"
        return result
    if status.stdout.strip():
        result["reason"] = "uncommitted changes or untracked files appeared"
        return result
    if head.stdout.strip() != info["base_head"]:
        result["reason"] = "branch HEAD advanced after worktree creation"
        return result

    # No force flags. If work appears after the checks, git's own clean-worktree
    # guard refuses removal; if a commit races in, branch -d refuses the unmerged
    # commit. The two independent guards preserve work across either window.
    try:
        removed = subprocess.run(base + ["worktree", "remove", target],
                                 capture_output=True, text=True, timeout=30, **run_flags())
    except Exception as e:  # noqa: BLE001
        result["reason"] = "worktree removal failed: %s: %s" % (type(e).__name__, e)
        return result
    result["worktree_removed"] = not os.path.exists(target)
    if removed.returncode != 0 or not result["worktree_removed"]:
        result["reason"] = ("git refused worktree removal: "
                            + (removed.stderr or removed.stdout or "unknown error").strip())
        return result

    try:
        deleted = subprocess.run(base + ["branch", "-d", str(branch)],
                                 capture_output=True, text=True, timeout=30, **run_flags())
        exists = subprocess.run(base + ["rev-parse", "--verify", "--quiet",
                                        "refs/heads/" + str(branch)],
                                capture_output=True, timeout=30, **run_flags())
        result["branch_removed"] = exists.returncode != 0
        if deleted.returncode != 0 or not result["branch_removed"]:
            result["reason"] = ("checkout removed but branch preserved: "
                                + (deleted.stderr or deleted.stdout or "git refused deletion").strip())
            return result
    except Exception as e:  # noqa: BLE001
        result["reason"] = "checkout removed but branch cleanup failed: %s: %s" % (
            type(e).__name__, e)
        return result
    result["preserved"] = False
    result["reason"] = "pristine checkout and unchanged branch removed"
    return result


def _regate_or_none(args, agents_dir, invocation):
    """Re-run the gate for a RETRY attempt. Returns the decision if refused (so the
    caller stops), or None when approved. A gate authorises one execution."""
    if not getattr(args, "gate_with", None):
        return None
    dec = _run_gate(args, agents_dir, invocation)
    return None if dec.get("approved") else dec


def _is_agy_scrape_loss(result: dict) -> bool:
    """True when agy returned nothing at all after a run that actually executed.

    Some legacy setups still use a terminal scrape path where a dropped frame yields
    an empty envelope after a full wall-clock. That is a LOST RESULT, not a refusal,
    and it is the one failure a blind retry genuinely fixes.
    Deliberately narrow: agy only, empty only, error only -- a retry that fires on a real
    refusal just spends money twice.
    """
    # The backend must have ACTUALLY RUN. Without this, every empty agy error qualified:
    # a missing pywinpty, an oversized argv, an unenforceable-tier refusal, a policy decline
    # -- all preflight or structural failures that a retry cannot fix, each costing a real
    # dispatch to re-learn. `backend_exit_code` is set only once a child was spawned and
    # driven, so it is the evidence that separates "the run happened and the scrape lost it"
    # from "the run never started".
    if result.get("backend_exit_code") is None:
        return False
    reason = (result.get("normalization_reason") or "").lower()
    return (result.get("cli") == "agy"
            and result.get("status") == "error"
            and "empty terminal scrape" in reason
            and not (result.get("result") or "").strip()
            and not (result.get("error") or "").strip().lower().startswith(
                ("agy backend:", "agy prompt is", "agy cannot enforce")))


def _acp_fallback_enabled(args) -> bool:
    """Kill switch: --no-acp-fallback, or SUMMON_ACP_FALLBACK=0 (which the flag
    also sets so the executor's oversized-prompt routing sees it)."""
    return (os.environ.get("SUMMON_ACP_FALLBACK") != "0"
            and not getattr(args, "no_acp_fallback", False))


def _acp_fallback_worthy(result: dict) -> bool:
    """True only for failure classes a TRANSPORT change can fix (premortem T3).

    Deliberately narrow, same philosophy as _is_agy_scrape_loss: a fallback that
    fires on a structural failure spends a real paid dispatch to fail the same
    way over a different wire. IN: timeouts, stream-sniffing/output-shape
    losses, mid-stream pipe failures, backend-internal errors. OUT:
    CLI-not-found (ACP spawns the same binary), auth/eligibility (a credential
    problem, not a transport problem), structural refusals that never reached a
    backend, argv-length (the executor routes those over ACP itself)."""
    if result.get("status") not in ("error", "partial"):
        return False
    reason = (result.get("normalization_reason") or "").lower()
    if result.get("exit_code") == 124 or "timed out" in reason:
        return True
    err = (result.get("error") or "").lower()
    if "over the windows limit" in err or "a single argument is" in err:
        return False
    if result.get("exit_code") == 127 or "cli not found" in err:
        return False
    if "cannot enforce" in err:
        return False
    # Auth/eligibility signatures, shared with --doctor's classifier so a newly
    # catalogued signature narrows fallback automatically. The result tail is
    # included because an auth failure sometimes surfaces only in output text.
    try:
        from _doctor import classify_ineligibility
        text = " ".join([err, reason, (result.get("result") or "")[:500]])
        if classify_ineligibility(text, backend=result.get("cli")):
            return False
    except Exception:  # noqa: BLE001 — a broken classifier must not block recovery
        pass
    return True


def _dispatch_with_retries(invocation, args, agents_dir=None) -> dict:
    """execute_agent with --retries: exponential backoff on error/partial only
    (blocked won't improve by retrying — its cause is structural).

    Under --gate-with, EACH retry is re-gated. A gate authorises one execution; N
    attempts of a side-effecting task on a single approval is materially more than
    what was approved. A refusal mid-retry stops the loop and returns the blocked
    envelope rather than the last failure."""
    attempt = 0
    _auto_scrape_retry = False
    _prev_result: dict = {}
    while True:
        result = execute_agent(invocation, timeout_ms=args.timeout, debug_dir=args.debug_dir,
                               max_tool_output_bytes=getattr(args, "max_tool_output_bytes", None))
        attempt += 1
        # A scrape-loss on agy gets ONE free retry even at --retries 0. Every other
        # backend fails LOUDLY (a pipe closes, an exit code arrives); a screen-scraped one
        # fails EMPTY, so the operator has to know to opt into retries for the single
        # backend where output loss is structural rather than exceptional. Field report
        # (2026-07-27): agy succeeded at 55s and failed empty at 47s on comparable prompts.
        # `attempts` and the warning below keep it honest -- this spends a real dispatch.
        _budget = max(0, args.retries)
        if _budget == 0 and attempt == 1 and _is_agy_scrape_loss(result):
            _budget = 1
            _auto_scrape_retry = True
        if attempt > 1:
            # Money: a retry REPLACED the previous attempt's cost/usage instead of adding to
            # it, so two attempts at $0.40 and $0.60 reported $0.60. `attempts` said 2 while
            # the bill said one. _aggregate_spend already exists for exactly this.
            _aggregate_spend(result, _prev_result)
        _prev_result = dict(result)
        if result.get("status") not in ("error", "partial") or attempt > _budget:
            if _auto_scrape_retry:
                # On the RETURNED envelope, not the discarded first attempt. Appending it
                # inside the loop lost the warning the moment the retry replaced that dict,
                # so a recovered run reported success with no trace of the extra dispatch
                # it had spent.
                result.setdefault("warnings", []).append(
                    "agy returned an empty terminal scrape, so summon retried once "
                    "automatically: on this backend an empty result is usually a lost frame "
                    "rather than an agent that declined. This spent an extra dispatch (see "
                    "`attempts`). Use --retries to control it.")
            break
        refused = _regate_or_none(args, agents_dir, invocation)
        if refused is not None:
            from _gate import blocked_envelope
            result = _enrich_denial(
                blocked_envelope(refused, agent=getattr(args, "agent", None),
                                 cli=invocation.cli),
                getattr(args, "_receipt", None), invocation)
            result["attempts"] = attempt
            return result
        time.sleep(min(30, 2 ** attempt))
    result["attempts"] = attempt

    # ACP fallback: ONE recovery attempt over the Agent Client Protocol when the
    # subprocess path failed in a way a transport change can fix (premortem T3
    # predicate keeps it narrow). Re-gated like any retry; spend and attempts
    # stay honest whichever envelope is returned; the `fallback` field records
    # the attempt either way (and doubles as Phase-2 scoping telemetry, E1).
    from _builder import supports_acp as _supports_acp
    if (result.get("status") in ("error", "partial")
            and invocation.transport == "subprocess"
            and invocation.permission == "yolo"  # ACP refuses sub-yolo tiers
            and _supports_acp(invocation.cli)
            and _acp_fallback_enabled(args)
            and _acp_fallback_worthy(result)):
        refused = _regate_or_none(args, agents_dir, invocation)
        if refused is not None:
            from _gate import blocked_envelope
            denied = _enrich_denial(
                blocked_envelope(refused, agent=getattr(args, "agent", None),
                                 cli=invocation.cli),
                getattr(args, "_receipt", None), invocation)
            # The primary attempt's spend happened whether or not the gate
            # allows the recovery; folding it in keeps accounting honest.
            _aggregate_spend(denied, result)
            denied["attempts"] = attempt
            return denied
        from dataclasses import replace as _replace
        fb = execute_agent(_replace(invocation, transport="acp"),
                           timeout_ms=args.timeout, debug_dir=args.debug_dir,
                           max_tool_output_bytes=getattr(args, "max_tool_output_bytes", None))
        if fb.get("status") == "success":
            # Recovered: return the ACP envelope with the primary failure folded
            # in (spend + provenance), never silently.
            _aggregate_spend(fb, result)
            fb["attempts"] = attempt + 1
            fb["fallback"] = {
                "from": "subprocess", "to": "acp",
                "reason": result.get("error") or result.get("normalization_reason"),
                "primary_status": result.get("status"),
            }
            fb.setdefault("warnings", []).append(
                "the subprocess transport failed, so summon recovered this run over "
                "ACP (see `fallback`). This spent an extra dispatch (see `attempts`). "
                "Use --no-acp-fallback to control it.")
            return fb
        # Recovery failed too: keep the ORIGINAL envelope (richer primary-path
        # diagnostics), but the spent fallback attempt is recorded and billed.
        _aggregate_spend(result, fb)
        result["attempts"] = attempt + 1
        result["fallback"] = {"to": "acp", "status": fb.get("status"),
                              "error": fb.get("error")}
    return result


def _apply_schema(result: dict, schema: dict, invocation, args, agents_dir=None) -> dict:
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
    # The schema correction re-dispatches with the ORIGINAL permission (retry_inv does
    # not override it), so under --gate-with it is a SECOND write-capable execution. A
    # gate authorises one. Re-gate it, exactly as _dispatch_with_retries does; a refusal
    # keeps the original result -- which was itself approved and completed -- rather than
    # performing an unapproved edit.
    _refused = _regate_or_none(args, agents_dir, retry_inv)
    if _refused is not None:
        # NOT result["gate"]: that field holds the approval which authorised the run that
        # already happened, and overwriting it would misreport completed work as denied.
        # The refusal of the CORRECTION is its own fact.
        result["gate_correction_refused"] = _refused
        return result
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
        # The retry is a DIFFERENT envelope, so gate evidence attached to the original
        # would simply vanish here -- a gated dispatch reporting no gate at all.
        if "gate" in result and "gate" not in retry:
            retry["gate"] = result["gate"]
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


_EXIT_TUPLE = ("exit_code", "backend_exit_code", "dispatcher_status",
               "normalization_reason")


def _push_exit_history(result: dict, retry: dict) -> None:
    """Publish the retry's exit telemetry, keeping every superseded attempt in order.

    `original_exit` was singular and therefore wrong as soon as more than one retry could
    run: an accepted SCHEMA correction replaces the root envelope before contract repair
    ever looks at it, so the "original" it preserved was the schema retry, not attempt 1 --
    while the changelog claimed attempt 1. An append-only list cannot drift that way, and
    `attempts` already tells the caller how many runs there were.

    `original_exit`/`original_exit_code` are kept as aliases for the MOST RECENT superseded
    attempt, because they are already documented; `exit_history` is the truthful record.
    """
    from _executor import finalize_exit_fields as _fin

    superseded = {k: result.get(k) for k in _EXIT_TUPLE if k in result}
    if superseded:
        result.setdefault("exit_history", []).append(superseded)
        result["original_exit"] = superseded            # most recent, not "attempt 1"
        result["original_exit_code"] = superseded.get("exit_code")
    for k in _EXIT_TUPLE:
        result.pop(k, None)
    result["exit_code"] = retry.get("exit_code")
    for k in ("backend_exit_code", "dispatcher_status", "normalization_reason"):
        if retry.get(k) is not None:
            result[k] = retry[k]
    # ADOPT the retry's own reason where it has one; recompute only as a fallback, since a
    # sentence the retry wrote about itself beats one we infer.
    _fin(result)


def _repair_permission(invocation) -> tuple:
    """The tier a corrective resume should run at, and a warning when it is not read-only.

    The repair normally forces `read-only`: it is a formatting re-emit and must not gain
    authority. Forcing it on a backend that cannot ENFORCE read-only was a first attempt at
    this and it was wrong twice over. It refused the resume outright -- discarding the
    refusal, so the envelope claimed `contract_repair_attempted: true` and `attempts: 2` for
    a call that never reached a backend -- and it bought nothing, because the repair RESUMES
    THE SESSION THAT ALREADY RAN. That session held the task's authority; a tier on the
    resume cannot retroactively contain it, and on agy the tier is unenforceable regardless.

    So: read-only where it means something, the original tier where it does not, and say
    which. Pretending is the one option that is never right.
    """
    from _builder import readonly_unenforceable_error
    if readonly_unenforceable_error(invocation.cli, "read-only", forced=True) is None:
        return "read-only", True, None
    return invocation.permission, False, (
        "the corrective resume ran at %r, not read-only: %s cannot enforce read-only, and "
        "the resume continues the session that already held this authority. It re-emits the "
        "report contract and asks for no new work." % (invocation.permission, invocation.cli))


def _apply_contract_repair(result: dict, invocation, args, agents_dir=None) -> dict:
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
    _perm, _forced, _perm_warning = _repair_permission(invocation)
    # NOT appended yet: it says the resume RAN at this tier, and the gate below may deny it.
    # Emitting it here produced flatly contradictory telemetry -- "the corrective resume ran
    # at safe-edit" sitting next to "DENIED by the gate, so it was not run". A warning about
    # an execution belongs after the execution.
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
        permission=_perm,         # read-only where the backend enforces it (see above)
        permission_forced=_forced,
        resume_id=sid or "latest",
        resume_profile=profile or invocation.resume_profile,
        extra_args=(),   # DROP extra_args: a resume keeps the session's model, and a
                         # stray permission-override flag (--dangerously-bypass...,
                         # --permission-mode bypassPermissions) would defeat read-only.
    )
    # A gate authorises ONE execution. Retries re-gate; schema correction re-gates; this
    # path did not -- and round 3 made that expensive by giving the repair the task's own
    # tier on backends that cannot enforce read-only. On agy that is
    # --dangerously-skip-permissions, so a gated agy task with a malformed report bought a
    # second FULL-AUTHORITY run nobody approved. The repair prompt is instruction, not
    # containment.
    _repair_refused = _regate_or_none(args, agents_dir, retry_inv)
    if _repair_refused is not None:
        # Its own field, not result["gate"]: that holds the approval for the run that
        # already completed, and overwriting it would misreport approved work as denied.
        result["gate_repair_refused"] = _repair_refused
        result.setdefault("warnings", []).append(
            "the report contract is malformed and the corrective resume was DENIED by the "
            "gate, so it was not run; the original result stands as returned")
        return result
    if _perm_warning:                     # approved: the resume is about to actually run
        result.setdefault("warnings", []).append(_perm_warning)
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
        # If the ORIGINAL text was empty, "keep the original verbatim" preserves nothing:
        # the envelope then reports success with an empty `result`, and the only content
        # lives in a field most callers never read. Observed for real on this repo's own
        # review dispatch -- the findings existed, and a caller branching on status and
        # reading `result` would have seen an empty string and no error.
        if not (result.get("result") or "").strip():
            result["result"] = retry.get("result") or ""
            result["result_from_repair"] = True
        # The accepted outcome came from the RETRY, so the WHOLE exit tuple must describe
        # that run. Round 7 rewrote status and exit_code and stopped there, leaving
        # backend_exit_code, dispatcher_status and normalization_reason describing attempt 1
        # -- so the envelope still contradicted itself, just in fields I had not looked at,
        # and the changelog's "the contradiction is gone" was false. finalize_exit_fields()
        # cannot repair them afterwards because it uses setdefault: already-set values win.
        #
        # Preserve the complete original tuple (it is real evidence about attempt 1), then
        # adopt the retry's, recomputing the reason from the new pair rather than copying a
        # sentence written about the old one.
        if retry.get("exit_code") is not None:
            # UNCONDITIONAL once the retry is accepted. Gating on "did exit_code or status
            # change?" left attempt 1's normalization_reason in place whenever the two runs
            # happened to share an exit code -- two clean successes with DIFFERENT reasons
            # kept the wrong sentence and recorded no history at all. If the retry's answer
            # is the one being published, its telemetry is too.
            _push_exit_history(result, retry)
        if retry.get("resume"):
            result["resume"] = retry["resume"]     # latest session id for follow-ups
        if retry.get("warnings"):
            # DEDUPE, order-preserving. The corrective resume repeats the original
            # dispatch's conditions -- same backend, same tier, same clock -- so it emits
            # the same advisory warnings, and concatenating produced every one of them
            # twice. A caller counting warnings, or a human reading them, sees a doubled
            # list describing one condition.
            _merged = (result.get("warnings") or []) + retry["warnings"]
            _seen, result["warnings"] = set(), []
            for _w in _merged:
                if _w not in _seen:
                    _seen.add(_w)
                    result["warnings"].append(_w)
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
