"""Command-line surface: the argparse spec, the git-style subcommand front-end,
and the fan-out mode flag matrix.

Split out of run_subagent.py so the entry point keeps only the dispatch flow.
Everything here is pure translation/validation of the command line: it reads argv
and a parsed namespace, and builds the parser; it never touches dispatch state.
``build_parser`` takes the version + envelope-schema version as injected params
so this module has no dependency back on the entry point.
"""

from __future__ import annotations

import argparse
import math


def parse_timeout(value: str) -> int:
    """--timeout accepts bare milliseconds (backward compatible) or a human
    suffix: '90s', '10m', '600000ms'. Returns whole milliseconds (>= 1;
    fractional input rounds). Zero, negative, and non-finite durations are
    rejected here so they fail as argparse errors, not as instantly-killed
    agents or an OverflowError from the executor."""
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


# --- Fan-out mode flag matrix --------------------------------------------------
# The flags each fan-out mode actually CONSUMES. --manifest and --council branch
# out of main() before most dispatch flags are read, so anything outside these
# sets used to be SILENTLY IGNORED -- field case: a council run passed --out
# expecting an artifact and never got one. A flag that would be dropped is now
# rejected loudly BEFORE any paid dispatch. Whitelist, not blacklist: a flag
# added to the parser later is rejected-by-default in these modes until a mode
# explicitly supports it.
MODE_FLAGS = {
    "manifest": {"manifest", "concurrency", "results_dir", "cwd", "agents_dir",
                 "retries", "job_file"},
    # Operation-level rows: a fresh council, a resume, and a read-only status
    # each consume a DIFFERENT set (v3.1). Changing members/rounds/question on a
    # resume would be a new run, so they are rejected there; status takes only
    # its id + where to look.
    "council": {"council", "question", "question_file", "members", "chairman",
                "rounds", "cwd", "agents_dir", "timeout", "out", "run_dir", "job_file",
                "quorum", "chairman_fallback", "member_timeout", "chair_timeout"},
    # A resume may change how the SAME run's stages are gated/timed (quorum,
    # fallback, per-stage timeouts) without changing its identity; question,
    # members, chairman, and rounds still come from the receipt.
    "council-resume": {"council", "resume_run", "cwd", "agents_dir", "timeout",
                       "out", "run_dir", "job_file",
                       "quorum", "chairman_fallback", "member_timeout", "chair_timeout"},
    # Status takes ONLY its id, where to look, and the output format -- it never
    # dispatches, so it has no working directory (use --run-dir to point it).
    "council-status": {"council_status", "run_dir", "json", "job_file"},
    # jobs read commands: registry query only.
    "jobs-list": {"jobs_list", "job_dir", "json", "job_file"},
    "jobs-status": {"jobs_status", "job_dir", "json", "job_file"},
    "jobs-wait": {"jobs_wait", "job_dir", "timeout", "job_file"},
}
MODE_HINTS = {
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
    "jobs-list": ("jobs list is read-only: it takes only --job-dir and --json."),
    "jobs-status": ("jobs status is read-only: it takes only the job id, --job-dir, "
                    "and --json."),
    "jobs-wait": ("jobs wait is read-only: it takes only the job id, --job-dir, "
                  "and --timeout."),
}
FLAG_NAMES = {"sets": "--set"}  # dests whose flag spelling isn't dest.replace('_','-')
TOKEN_DESTS = {"set": "sets"}   # the reverse mapping, for raw-argv presence detection


def fanout_mode(args: argparse.Namespace) -> str | None:
    """Which fixed-flag mode this invocation is, for the whitelist below."""
    if args.manifest:
        return "manifest"
    if getattr(args, "jobs_list", None):
        return "jobs-list"
    if getattr(args, "jobs_status", None):
        return "jobs-status"
    if getattr(args, "jobs_wait", None):
        return "jobs-wait"
    if getattr(args, "council_status", None):
        return "council-status"
    if args.council:
        return "council-resume" if getattr(args, "resume_run", None) else "council"
    return None


def unsupported_mode_flags(argv: list, args: argparse.Namespace) -> str | None:
    """Error text when a fan-out mode received flags it does not consume, else
    None. Presence is detected from the RAW (post-subcommand-rewrite) argv, not
    by comparing parsed values to defaults -- a value equal to its default
    (e.g. ``--timeout 600000``) is still an explicit flag and still rejected."""
    mode = fanout_mode(args)
    if mode is None:
        return None
    allowed = MODE_FLAGS[mode]
    present = set()
    for tok in argv:
        if tok.startswith("--"):
            name = tok[2:].split("=", 1)[0]
            present.add(TOKEN_DESTS.get(name, name.replace("-", "_")))
    offending = sorted(
        FLAG_NAMES.get(dest, "--" + dest.replace("_", "-"))
        for dest in vars(args)
        if dest not in allowed and dest in present
    )
    if not offending:
        return None
    label = {"council-resume": "council resume", "council-status": "council status"
             }.get(mode, f"--{mode}")
    return (f"{label} does not support {', '.join(offending)}: these flags would "
            f"have been silently ignored, so they are rejected instead. "
            f"{MODE_HINTS[mode]}")


# --- Subcommand front-end -----------------------------------------------------
# summon presents git-style subcommands (dispatch/manifest/council/doctor/models/
# agent/list/version) that translate to the underlying flat flags. The flat form
# still works unchanged (legacy compat) — anything starting with '-' skips the
# rewrite. This keeps one battle-tested parser + all logic while giving a clean,
# discoverable command surface.
SUBCOMMANDS = {"dispatch", "run", "list", "agents", "ls", "models", "doctor",
               "manifest", "council", "agent", "jobs", "version", "help", "--help", "-h"}

USAGE = """summon — cross-vendor sub-agents for any AI CLI

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
  jobs list|status|wait [ID] [--job-dir D] [--json]   inspect background jobs
  version                                         print version

Legacy flat flags still work: `summon --agent NAME --prompt … --cwd …`,
`summon --list`, `summon --manifest FILE`, etc. Run any command with --help for
its options. Full docs: SKILL.md.
"""


def rewrite_subcommand(argv: list) -> tuple:
    """Translate a leading subcommand into equivalent flat flags. Returns
    ``(argv, mode)`` where mode is 'help' (print usage, exit 0), a string
    'error: …' (print error, exit 2), or None. Legacy flat invocations (argv
    starts with '-') pass through untouched."""
    if not argv:
        return argv, "help"
    head = argv[0]
    if head.startswith("-") or head not in SUBCOMMANDS:
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
    if head == "jobs":
        if not rest:
            return argv, "help"       # bare `summon jobs` -> usage, not a silent list
        if rest[0] == "list":
            return ["--jobs-list", *rest[1:]], None
        if rest[0] in ("status", "wait"):
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, f"error: 'jobs {rest[0]}' needs a job id"
            flag = "--jobs-status" if rest[0] == "status" else "--jobs-wait"
            return [flag, rest[1], *rest[2:]], None
        return argv, f"error: unknown 'jobs' action {rest[0]!r} (use list/status/wait)"
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


def build_parser(version: str, envelope_version) -> argparse.ArgumentParser:
    """The full flat-flag argparse spec. ``version``/``envelope_version`` are
    injected (the entry point owns them) so this module never imports it back."""
    parser = argparse.ArgumentParser(description="Execute external CLI AIs as sub-agents")
    parser.add_argument("--version", action="version",
                        version=f"summon {version} (envelope schema v{envelope_version})")
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
    parser.add_argument("--probe", action="store_true",
                        help="With --doctor: run a minimal LIVE call per backend to verify "
                             "account/client eligibility (catches e.g. Gemini IneligibleTierError "
                             "that a --version check misses). Costs a tiny dispatch per backend")
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
        "--timeout", type=parse_timeout, default=600000,
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
    parser.add_argument("--max-tool-output-bytes", dest="max_tool_output_bytes",
                        type=int, default=None,
                        help="Elision threshold for the output_tail: a base64/binary "
                             "run this many bytes or longer is replaced by a bounded "
                             "[payload omitted: type, N bytes, sha256 ...] marker "
                             "(data: URIs are always elided; --debug-dir keeps the "
                             "full transcript). Default ~2048")
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
    parser.add_argument("--quorum", type=int, metavar="N",
                        help="With --council: synthesize only if at least N members "
                             "succeeded (2..member-count); below N the chairman is skipped. "
                             "Never changes the top-level status, only synthesis")
    parser.add_argument("--chairman-fallback", dest="chairman_fallback", metavar="AGENT",
                        help="With --council: a fallback synthesizer to run once if the "
                             "primary chairman ends non-success")
    parser.add_argument("--member-timeout", dest="member_timeout", type=parse_timeout,
                        help="With --council: per-member stage timeout (default: --timeout)")
    parser.add_argument("--chair-timeout", dest="chair_timeout", type=parse_timeout,
                        help="With --council: chairman (and fallback) stage timeout "
                             "(default: --timeout)")
    parser.add_argument("--job-dir", dest="job_dir",
                        help="Root for --background job records/results "
                             "(default {tempdir}/subagents_jobs; env SUMMON_JOBS_DIR)")
    parser.add_argument("--jobs-list", dest="jobs_list", action="store_true",
                        help="List background jobs in the job dir (read-only; add --json)")
    parser.add_argument("--jobs-status", dest="jobs_status", metavar="JOB_ID",
                        help="Print one background job's record + result (read-only)")
    parser.add_argument("--jobs-wait", dest="jobs_wait", metavar="JOB_ID",
                        help="Wait for a background job's result (read-only poll; --timeout)")
    return parser
