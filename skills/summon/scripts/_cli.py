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


_MAX_TIMEOUT_MS = 7 * 24 * 60 * 60 * 1000   # 7 days; see parse_timeout


class Milliseconds(int):
    """A parsed --timeout, carrying whether it was written as a BARE sub-second number.

    argparse runs before the mode is known, and the same flag serves two very different
    jobs: a DISPATCH budget, where a bare `300` (meaning 0.3s) can only kill every agent
    instantly, and a `jobs wait` POLL, where "give up after 300ms" is a perfectly sensible
    non-blocking check. So the parser records the fact and the dispatch path decides --
    rejecting it globally broke `jobs wait --timeout 300`, which is legitimate.
    """

    bare_sub_second = False

    def __str__(self) -> str:
        """Serialize WITH the unit, always.

        `--background` and council forward this into a child's argv. Emitting a bare number
        threw away `bare_sub_second`, so an explicit `300ms` the parent accepted reached the
        child as "300" and was refused there as a units mistake -- summon contradicting its
        own promise across a process boundary (certification round 3). `300ms` round-trips
        to itself; there is no ambiguity left to lose.
        """
        return "%dms" % int(self)


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
    # A BARE value under one second is a units mistake, not a budget. No backend starts,
    # authenticates and answers in under a second, so `--timeout 300` meaning 0.3s can only
    # kill the dispatch instantly -- which is exactly what it did to a four-member council
    # in the field (2026-07-27): every seat killed after ~1s, no work performed. An explicit
    # `300ms` is still accepted, because someone writing the unit means it.
    _bare_sub_second = (not s.endswith(("ms", "s", "m"))) and ms < 1000
    # A finite but absurd value ('1e308') survived the checks above and then blew up far
    # downstream as an OverflowError inside threading.Event().wait() -- a traceback instead of a
    # dispatch. Nothing legitimate waits on a sub-agent for over a week, so cap it here where the
    # error is an argparse message the caller can act on.
    if ms > _MAX_TIMEOUT_MS:
        raise argparse.ArgumentTypeError(
            f"invalid --timeout {value!r}: exceeds the {_MAX_TIMEOUT_MS} ms (7 day) maximum")
    out = Milliseconds(max(1, int(round(ms))))
    out.bare_sub_second = _bare_sub_second
    return out


def parse_quorum(value: str) -> int | str:
    """Keep council's integer form while accepting deliberation all/fractions."""
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    parts = text.split("/", 1)
    if (text == "all" or (len(parts) == 2 and all(part.isdigit() for part in parts)
                          and all(int(part) > 0 for part in parts))):
        return text
    raise argparse.ArgumentTypeError(
        "quorum must be an integer, 'all', or a positive fraction such as 2/3")


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
                 "retries", "job_file", "strict_agents_dir", "enable_roles"},
    # Operation-level rows: a fresh council, a resume, and a read-only status
    # each consume a DIFFERENT set (v3.1). Changing members/rounds/question on a
    # resume would be a new run, so they are rejected there; status takes only
    # its id + where to look.
    "council": {"council", "question", "question_file", "members", "chairman",
                "rounds", "cwd", "agents_dir", "timeout", "out", "run_dir", "results_dir",
                "job_file", "quorum", "chairman_fallback", "member_timeout",
                "chair_timeout", "overall_timeout", "min_successful", "strict_agents_dir",
                "enable_roles"},
    # A resume may change how the SAME run's stages are gated/timed (quorum,
    # fallback, per-stage timeouts) without changing its identity; question,
    # members, chairman, and rounds still come from the receipt.
    "council-resume": {"council", "resume_run", "cwd", "agents_dir", "timeout",
                       "out", "run_dir", "results_dir", "job_file",
                       "quorum", "chairman_fallback", "member_timeout", "chair_timeout",
                       "overall_timeout", "min_successful", "strict_agents_dir", "enable_roles"},
    # Status takes ONLY its id, where to look, and the output format -- it never
    # dispatches, so it has no working directory (use --run-dir to point it).
    "council-status": {"council_status", "run_dir", "json", "job_file"},
    "deliberation": {"deliberate", "question", "question_file", "seats", "options",
                     "quorum", "rounds", "max_attempts", "deadline", "cwd",
                     "agents_dir", "run_dir", "results_dir", "strict_agents_dir",
                     "enable_roles", "require_human_approval", "text_only_consent",
                     "full_authority_consent", "json", "job_file"},
    "deliberation-resume": {"deliberate_resume", "run_dir", "results_dir", "cwd",
                            "retry_indeterminate", "json", "job_file"},
    "deliberation-open": {"deliberate_open", "run_dir", "results_dir", "cwd",
                          "browser", "json", "job_file"},
    "deliberation-recover": {"deliberate_recover", "run_dir", "results_dir", "cwd",
                             "json", "job_file"},
    "deliberation-status": {"deliberate_status", "run_dir", "results_dir", "cwd",
                            "json", "job_file"},
    "deliberation-replay": {"deliberate_replay", "run_dir", "results_dir", "cwd",
                            "json", "job_file"},
    "deliberation-cancel": {"deliberate_cancel", "run_dir", "results_dir", "cwd",
                            "command_id", "json", "job_file"},
    "chat": {"chat_action", "chat_session", "chat_message", "chat_participant",
              "chat_timeout", "chat_participants", "chat_project_id",
              "chat_project_root", "chat_initiator_host", "chat_initiator_agent",
              "chat_mode", "chat_browser", "chat_confirm", "chat_reason",
              "chat_to", "chat_after",
              "conversation_dir", "agents_dir",
              "strict_agents_dir", "json", "cwd", "job_file"},
    "swarm": {"swarm_action", "swarm_run_id", "swarm_dir", "swarm_tasks",
               "swarm_project_root_sha256", "swarm_roster_sha256", "swarm_max_attempts",
               "swarm_worker", "swarm_instance", "swarm_task_id", "swarm_request_sha256",
               "swarm_lease_ms", "swarm_claim_id", "swarm_lease_generation",
               "swarm_reason", "json", "job_file", "cwd"},
    # jobs read commands: registry query only.
    "jobs-list": {"jobs_list", "job_dir", "json", "job_file"},
    "jobs-status": {"jobs_status", "job_dir", "json", "job_file"},
    "jobs-wait": {"jobs_wait", "job_dir", "timeout", "job_file"},
    # Diagnostics are local management commands. They never dispatch an agent;
    # bug-report submission is an explicit, user-authenticated gh invocation.
    "telemetry": {"telemetry_enable", "telemetry_disable", "telemetry_status",
                   "telemetry_clear", "json", "job_file"},
    "bug-report": {"bug_report", "bug_report_from", "bug_report_output",
                    "bug_report_submit", "github_repo", "bug_title",
                    "bug_description", "json", "job_file"},
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
    "deliberation": ("a fresh deliberation takes only its immutable question, seats, "
                     "options, policy, consent, and run-location flags."),
    "deliberation-resume": ("resume takes the run id and may explicitly authorize "
                            "retrying an indeterminate paid attempt."),
    "deliberation-open": ("open takes only the run id, run location, output format, "
                          "and browser target; use 'link' for scripts or SSH."),
    "deliberation-recover": ("recover completes only deterministic, journal-proven "
                              "crash boundaries and performs zero provider calls."),
    "deliberation-status": ("status is read-only and accepts only the run id, run "
                            "location, and output format."),
    "deliberation-replay": ("replay is read-only and accepts only the run id, run "
                            "location, and output format."),
    "deliberation-cancel": ("cancel queues one typed command; --command-id is an "
                            "optional idempotency key."),
    "chat": ("chat is a local room with explicit human context and bounded agent turns. "
             "`turn` launches one selected roster agent after a durable turn_started "
             "event and resumes its provider session only when identity evidence matches; "
             "drift creates a visible fork. It never changes a ballot. `chat open "
             "--chat-browser auto|builtin|ide|system|link` starts or reuses the atlas."),
    "jobs-list": ("jobs list is read-only: it takes only --job-dir and --json."),
    "jobs-status": ("jobs status is read-only: it takes only the job id, --job-dir, "
                    "and --json."),
    "jobs-wait": ("jobs wait is read-only: it takes only the job id, --job-dir, "
                  "and --timeout."),
    "telemetry": ("telemetry is local-only and opt-in: it writes bounded, sanitized "
                  "JSONL evidence and never phones home."),
    "bug-report": ("bug-report writes a sanitized local report; review it before the "
                   "explicit --submit-github action."),
}
FLAG_NAMES = {"sets": "--set"}  # dests whose flag spelling isn't dest.replace('_','-')
TOKEN_DESTS = {"set": "sets", "from": "bug_report_from",
               # ergonomic names used only by the `chat` subcommand
               "project-id": "chat_project_id", "project-root": "chat_project_root",
               "initiator-host": "chat_initiator_host", "initiator-agent": "chat_initiator_agent",
               "message": "chat_message", "mode": "chat_mode", "participant": "chat_participant",
               "participants": "chat_participants", "to": "chat_to",
               "after": "chat_after"}   # reverse mapping


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
    if getattr(args, "deliberate_status", None):
        return "deliberation-status"
    if getattr(args, "deliberate_recover", None):
        return "deliberation-recover"
    if getattr(args, "deliberate_replay", None):
        return "deliberation-replay"
    if getattr(args, "deliberate_cancel", None):
        return "deliberation-cancel"
    if getattr(args, "deliberate_resume", None):
        return "deliberation-resume"
    if getattr(args, "deliberate_open", None):
        return "deliberation-open"
    if getattr(args, "chat_action", None):
        return "chat"
    if getattr(args, "deliberate", False):
        return "deliberation"
    if args.council:
        return "council-resume" if getattr(args, "resume_run", None) else "council"
    if any(getattr(args, name, False) for name in
           ("telemetry_enable", "telemetry_disable", "telemetry_status", "telemetry_clear")):
        return "telemetry"
    if getattr(args, "bug_report", False):
        return "bug-report"
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
    label = {"council-resume": "council resume", "council-status": "council status",
             "deliberation-resume": "deliberate resume",
             "deliberation-recover": "deliberate recover",
             "deliberation-status": "deliberate status",
             "deliberation-replay": "deliberate replay",
             "deliberation-cancel": "deliberate cancel",
             "deliberation-open": "deliberate open",
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
               "onboard", "manifest", "council", "deliberate", "agent", "jobs", "version",
               "chat", "swarm", "role", "telemetry", "bug-report", "help", "--help", "-h"}

USAGE = """summon — cross-vendor sub-agents for any AI CLI

Usage: summon <command> [options]

Commands:
  dispatch  --agent NAME --prompt "…" --cwd DIR   run an agent (the default action)
  list                                            list available agents
  agents validate [--cwd DIR] [--agents-dir D]   validate custom agent manifests
  models    [--cli BACKEND]                       what each backend can run now
  doctor    [--json] [--probe]                    check backends / setup health
  onboard   [--subscriptions …] [--reset] [--json] detect CLIs; write merge-safe prefs
  manifest  FILE [--concurrency …] [--results-dir D]   run a batch swarm
  council   --question "…" [--members …] [--rounds 2]  decide by consensus
  deliberate --question "…" --seats A,B --options X,Y  bounded agent deliberation
  deliberate status|replay|cancel|recover RUN_ID       inspect/control/recover a run
  deliberate open RUN_ID [--browser auto|builtin|ide|system|link]  open its local ledger
  deliberate resume RUN_ID [--retry-indeterminate]     resume with spend consent
  chat open SESSION_ID [--mode chat|council|deliberate]  create/reuse a local room
  chat post SESSION_ID --message "…"                    add a typed human context message
  chat turn SESSION_ID AGENT --message "…"              run one resumable agent turn
  chat cancel SESSION_ID AGENT                           cancel that active turn
  chat show SESSION_ID | chat list                      inspect rooms
  swarm create RUN_ID --swarm-tasks FILE               create a durable local coordinator
  swarm status|events RUN_ID                            inspect coordinator state/events
  swarm register RUN_ID WORKER INSTANCE                bind a worker to the run
  swarm claim RUN_ID TASK_ID WORKER --request-sha256 H  claim one task lease
  swarm renew RUN_ID CLAIM_ID WORKER --lease-generation N
  swarm cancel RUN_ID TASK_ID                           queue typed cancellation
  swarm close RUN_ID                                    close after terminal tasks
  agent new NAME [--set k=v …]                    scaffold an agent definition
  agent set NAME  --set k=v …                     retune an agent's frontmatter
  role propose ALIAS TARGET                        propose a private global role alias
  role approve ALIAS                              activate a proposed role alias
  role list|resolve ALIAS                         inspect approved/proposed aliases
  jobs list|status [ID] [--job-dir D] [--json]      inspect background jobs
  jobs wait ID [--job-dir D] [--timeout T]          wait for one background job
  telemetry enable|disable|status|clear [--json]  manage opt-in local diagnostics
  bug-report [--from FILE] [--output FILE] [--json] create a sanitized report
             [--bug-title TEXT] [--bug-description TEXT]
             --submit-github --from REVIEWED.md [--github-repo OWNER/REPO]
  version                                         print version

Legacy flat flags still work: `summon --agent NAME --prompt … --cwd …`,
`summon --list`, `summon --manifest FILE`, etc. Run `summon --help` for the complete
flat option list, or `summon telemetry --help` / `summon bug-report --help` for their command-specific forms. Full docs: SKILL.md.
"""


COMMAND_USAGE = {
    "chat": """summon chat open SESSION_ID [--project-id ID --project-root DIR --participants A,B]
summon chat post SESSION_ID --message TEXT
summon chat turn SESSION_ID AGENT --message TEXT [--chat-timeout 10m]
summon chat cancel SESSION_ID AGENT
summon chat message SESSION_ID FROM_AGENT TO_AGENT --message TEXT
summon chat inbox SESSION_ID AGENT [--chat-after CURSOR]
summon chat recover SESSION_ID AGENT --chat-confirm
summon chat fork SESSION_ID AGENT --message TEXT
summon chat show SESSION_ID | summon chat list
summon chat open SESSION_ID --chat-browser auto|builtin|ide|system|link

Open a local room, add human context, or start one bounded roster-agent turn.
The turn is durably started before provider launch; compatible provider sessions
resume, while identity drift creates an explicit fork. Chat output is context only:
it cannot approve, vote, launch, or change a deliberate run.
""",
    "swarm": """summon swarm create RUN_ID --swarm-tasks TASKS.json
                     --swarm-project-root-sha256 HEX --swarm-roster-sha256 HEX
summon swarm status|events RUN_ID
summon swarm register RUN_ID WORKER INSTANCE
summon swarm claim RUN_ID TASK_ID WORKER --request-sha256 HEX
summon swarm renew RUN_ID CLAIM_ID WORKER --lease-generation N
summon swarm cancel RUN_ID TASK_ID
summon swarm close RUN_ID

This is a local, provider-neutral coordinator. It durably fences claims,
leases, cancellation, artifacts, and uncertain spend; it never launches a
provider or attaches to an IDE-native swarm by itself.
""",
    "agents": """summon agents validate [--cwd DIR] [--agents-dir DIR] [--json]

Validate workspace `.agents/agents/<slug>/agent.md` manifests and an optional explicit
global agents root. This is provider-inert and returns only redacted identity,
authority, and digest evidence.
""",
    "telemetry": """summon telemetry enable|disable|status|clear [--json]

Manage opt-in local diagnostics. `enable`/`disable` persist the choice; `status` reports
the bounded JSONL spool; `clear` removes captured events without disabling collection.
The `SUMMON_TELEMETRY` environment override is non-persistent and inherited by Summon
children. No telemetry command dispatches an agent or makes a network call.
""",
    "bug-report": """summon bug-report [--from SOURCE] [--output REPORT.md] [--json]
                     [--bug-title TEXT] [--bug-description TEXT]
summon bug-report --submit-github --from REVIEWED.md
                     [--github-repo OWNER/REPO] [--bug-title TEXT] [--json]

Generate a sanitized local Markdown report from the latest event or SOURCE (envelope,
telemetry JSONL, debug directory). Review the existing REPORT.md, then submit that exact
file in the separate `--submit-github` form; submission never regenerates it.
""",
}


def command_usage(command: str | None = None) -> str:
    """Return specific help for the two management commands."""
    return COMMAND_USAGE.get(command or "", USAGE)


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
    # Management commands have a compact, command-specific help block. Other
    # subcommands retain the general usage because their flags are the full flat parser.
    if any(a in ("--help", "-h") for a in rest):
        return argv, f"help:{head}" if head in COMMAND_USAGE else "help"
    if head in ("dispatch", "run"):
        return rest, None
    if head == "agents" and rest and rest[0] == "validate":
        return ["--validate-agents", *rest[1:]], None
    if head in ("list", "agents", "ls"):
        return ["--list", *rest], None
    if head == "models":
        return ["--list-models", *rest], None
    if head == "doctor":
        return ["--doctor", *rest], None
    if head == "onboard":
        return ["--onboard", *rest], None
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
    if head == "deliberate":
        if rest and rest[0] in ("resume", "status", "replay", "cancel", "recover", "open"):
            action = rest[0]
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, f"error: 'deliberate {action}' needs a run id"
            flag = {
                "resume": "--deliberate-resume",
                "recover": "--deliberate-recover",
                "status": "--deliberate-status",
                "replay": "--deliberate-replay",
                "cancel": "--deliberate-cancel",
                "open": "--deliberate-open",
            }[action]
            return [flag, rest[1], *rest[2:]], None
        return ["--deliberate", *rest], None
    if head == "swarm":
        if not rest:
            return argv, "help:swarm"
        action = rest[0]
        if action not in ("create", "status", "events", "register", "claim", "renew", "cancel", "close"):
            return argv, f"error: unknown 'swarm' action {action!r} (use create/status/events/register/claim/renew/cancel/close)"
        if len(rest) < 2 or rest[1].startswith("-"):
            return argv, f"error: 'swarm {action}' needs a run id"
        translated = ["--swarm-action", action, "--swarm-run-id", rest[1]]
        if action == "register":
            if len(rest) < 4 or rest[2].startswith("-") or rest[3].startswith("-"):
                return argv, "error: 'swarm register' needs worker and instance ids"
            translated += ["--swarm-worker", rest[2], "--swarm-instance", rest[3], *rest[4:]]
        elif action == "claim":
            if len(rest) < 4 or rest[2].startswith("-") or rest[3].startswith("-"):
                return argv, "error: 'swarm claim' needs task and worker ids"
            translated += ["--swarm-task-id", rest[2], "--swarm-worker", rest[3], *rest[4:]]
        elif action == "renew":
            if len(rest) < 4 or rest[2].startswith("-") or rest[3].startswith("-"):
                return argv, "error: 'swarm renew' needs claim and worker ids"
            translated += ["--swarm-claim-id", rest[2], "--swarm-worker", rest[3], *rest[4:]]
        elif action == "cancel":
            if len(rest) < 3 or rest[2].startswith("-"):
                return argv, "error: 'swarm cancel' needs a task id"
            translated += ["--swarm-task-id", rest[2], *rest[3:]]
        else:
            translated += rest[2:]
        return translated, None
    if head == "chat":
        if not rest:
            return argv, "help:chat"
        action = rest[0]
        if action not in ("open", "post", "show", "list", "turn", "cancel", "message", "inbox", "recover", "fork"):
            return argv, f"error: unknown 'chat' action {action!r} (use open/post/show/list/turn/cancel/message/inbox/recover/fork)"
        if action == "list":
            return ["--chat-action", "list", *rest[1:]], None
        if len(rest) < 2 or rest[1].startswith("-"):
            return argv, f"error: 'chat {action}' needs a session id"
        translated = ["--chat-action", action, "--chat-session", rest[1]]
        if action in ("turn", "cancel", "inbox", "recover", "fork"):
            if len(rest) < 3 or rest[2].startswith("-"):
                return argv, f"error: 'chat {action}' needs a participant id"
            translated += ["--chat-participant", rest[2], *rest[3:]]
        elif action == "message":
            if len(rest) < 4 or rest[2].startswith("-") or rest[3].startswith("-"):
                return argv, "error: 'chat message' needs sender and recipient ids"
            translated += ["--chat-participant", rest[2], "--chat-to", rest[3], *rest[4:]]
        else:
            translated += rest[2:]
        # `--timeout` is a long-standing dispatch/jobs flag.  It must not be
        # globally remapped in TOKEN_DESTS because the mode matrix needs to
        # distinguish it from chat's bounded turn timeout.  Translate it only
        # inside the chat subcommand so jobs/manifest/council keep their legacy
        # meaning and validation.
        translated = [
            ("--chat-timeout" + token[len("--timeout"):])
            if token == "--timeout" or token.startswith("--timeout=") else token
            if token != "--after" and not token.startswith("--after=") else
            ("--chat-after" + token[len("--after"):])
            for token in translated
        ]
        return translated, None
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
    if head == "role":
        if not rest:
            return argv, "help"
        action = rest[0]
        if action == "propose":
            if len(rest) < 3 or rest[1].startswith("-") or rest[2].startswith("-"):
                return argv, "error: 'role propose' needs an alias and target agent"
            return ["--role-propose", rest[1], rest[2], *rest[3:]], None
        if action == "approve":
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, "error: 'role approve' needs a role name"
            return ["--role-approve", rest[1], *rest[2:]], None
        if action == "list":
            return ["--role-list", *rest[1:]], None
        if action == "resolve":
            if len(rest) < 2 or rest[1].startswith("-"):
                return argv, "error: 'role resolve' needs a role name"
            return ["--role-resolve", rest[1], *rest[2:]], None
        return argv, f"error: unknown 'role' action {action!r} (use propose/approve/list/resolve)"
    if head == "telemetry":
        if not rest or rest[0] not in ("enable", "disable", "status", "clear"):
            return argv, "error: 'telemetry' needs enable/disable/status/clear"
        flag = "--telemetry-" + rest[0]
        return [flag, *rest[1:]], None
    if head == "bug-report":
        return ["--bug-report", *rest], None
    return argv, None


def build_parser(version: str, envelope_version) -> argparse.ArgumentParser:
    """The full flat-flag argparse spec. ``version``/``envelope_version`` are
    injected (the entry point owns them) so this module never imports it back."""
    # allow_abbrev=False: argparse's prefix matching accepted `--mod opus` for --model, but
    # unsupported_mode_flags() scans the RAW argv by literal flag name, so an abbreviated
    # flag slipped past the "rejected, never silently dropped" fan-out matrix and was then
    # dropped anyway -- exactly the failure that matrix exists to prevent. Abbreviations are
    # also ambiguous as flags are added. Spell flags out.
    parser = argparse.ArgumentParser(description="Execute external CLI AIs as sub-agents",
                                     allow_abbrev=False)
    parser.add_argument("--version", action="version",
                        version=f"summon {version} (envelope schema v{envelope_version})")
    role_group = parser.add_mutually_exclusive_group()
    role_group.add_argument("--role-propose", nargs=2, metavar=("ALIAS", "TARGET"),
                             help="Propose a private global role alias; approval is required")
    role_group.add_argument("--role-approve", metavar="ALIAS",
                             help="Activate a previously proposed private role alias")
    role_group.add_argument("--role-list", action="store_true",
                             help="List private global role aliases and proposals")
    role_group.add_argument("--role-resolve", metavar="ALIAS",
                             help="Resolve and validate one approved private role alias")
    parser.add_argument("--enable-roles", dest="enable_roles", action="store_true",
                        help="Opt into approved user-global role aliases for this dispatch; "
                             "disabled by default and never changes an exact agent match")
    parser.add_argument("--list", action="store_true", help="List available agents")
    parser.add_argument("--validate-agents", dest="validate_agents", action="store_true",
                        help="Validate provider-inert custom-agent manifests under the workspace")
    parser.add_argument("--list-models", dest="list_models", action="store_true",
                        help="Report invocable models per backend (live where the CLI exposes it; "
                             "filter with --cli)")
    parser.add_argument("--onboard", action="store_true",
                        help="Detect CLIs, print install hints, write merge-safe prefs to "
                             "~/.agents/summon.json (never stores API secrets)")
    parser.add_argument("--subscriptions", dest="subscriptions", default=None,
                        help="With --onboard: comma list of active plans "
                             "(cursor,claude,codex,gemini,antigravity,byteplus_coding_plan,"
                             "kimi,other_api)")
    parser.add_argument("--reset", dest="onboard_reset", action="store_true",
                        help="With --onboard: replace onboard section instead of merging")
    parser.add_argument("--no-write", dest="onboard_no_write", action="store_true",
                        help="With --onboard: detect only; do not write prefs")
    telemetry_group = parser.add_mutually_exclusive_group()
    telemetry_group.add_argument("--telemetry-enable", dest="telemetry_enable",
                                 action="store_true", help="Enable bounded local diagnostics")
    telemetry_group.add_argument("--telemetry-disable", dest="telemetry_disable",
                                 action="store_true", help="Disable local diagnostics")
    telemetry_group.add_argument("--telemetry-status", dest="telemetry_status",
                                 action="store_true", help="Show local diagnostics status")
    telemetry_group.add_argument("--telemetry-clear", dest="telemetry_clear",
                                 action="store_true", help="Delete captured local diagnostics")
    parser.add_argument("--bug-report", dest="bug_report", action="store_true",
                        help="Create a sanitized local bug report from the latest event or --from")
    parser.add_argument("--from", dest="bug_report_from", metavar="FILE",
                        help="Bug-report source: envelope, telemetry JSONL, debug directory, or reviewed Markdown when submitting")
    parser.add_argument("--output", dest="bug_report_output", metavar="FILE",
                        help="Bug-report output path (default ~/.agents/summon-reports)")
    parser.add_argument("--submit-github", dest="bug_report_submit", action="store_true",
                        help="Submit an existing reviewed Markdown report from --from through authenticated gh")
    parser.add_argument("--github-repo", dest="github_repo", default="Nafjan/summon",
                        metavar="OWNER/REPO", help="Repository for --submit-github")
    parser.add_argument("--bug-title", dest="bug_title",
                        help="Title for the generated or submitted issue")
    parser.add_argument("--bug-description", dest="bug_description",
                        help="Short sanitized description for the report")
    parser.add_argument("--transient-retries", dest="transient_retries", action="store_true",
                        help="Enable one conservative retry on transient network/5xx/"
                             "timeout errors (also SUMMON_TRANSIENT_RETRIES=1). Never retries "
                             "ambiguous billable write failures")
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
                        help="With --new-agent/--set-agent: run-agent, model, permission, args, profile")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON where supported by the selected command")
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
    parser.add_argument("--strict-agents-dir", dest="strict_agents_dir", action="store_true",
                        help="Fail closed when an agent is absent from the selected roster; "
                             "do not fall back to bundled or plugin definitions")
    parser.add_argument(
        "--timeout", type=parse_timeout, default=600000,
        help="Timeout: bare ms, or with suffix — 600s, 10m (default: 600000 ms = 10m)"
    )
    parser.add_argument("--cli", help="Force specific CLI (claude, cursor-agent, codex, gemini)")
    parser.add_argument("--model", help="Override the agent's frontmatter model for this call")
    parser.add_argument("--profile", help="Select a named private backend profile for this call; "
                        "the name is resolved from ~/.agents/summon-profiles.json and never a path")
    parser.add_argument(
        "--effort",
        help="Reasoning/thinking: low|medium|high|xhigh|max (or none). "
             "Honored by claude+codex (default high); agy Gemini only when "
             "explicit (model suffix); ignored elsewhere. See references/effort.md",
    )
    parser.add_argument("--resume", dest="resume", help="Backend session/thread/chat id to resume")
    parser.add_argument("--resume-profile", help="agy only: profile dir of the session being resumed")
    parser.add_argument("--transport", choices=["subprocess", "acp"], default=None,
                        help="Force the dispatch transport. 'acp' runs the turn over the "
                             "Agent Client Protocol (native support: gemini, kimi, "
                             "cursor-agent). Overrides the agent's `transport:` frontmatter.")
    parser.add_argument("--no-acp-fallback", dest="no_acp_fallback", action="store_true",
                        help="Disable the automatic ACP recovery attempt (and oversized-prompt "
                             "ACP routing) when the subprocess transport fails")
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
    parser.add_argument("--max-permission", dest="max_permission",
                        choices=["read-only", "safe-edit"],
                        help="CLAMP the dispatch to at most this permission tier. It can "
                             "only REDUCE authority, never raise it: an agent declaring "
                             "read-only stays read-only even under --max-permission "
                             "safe-edit. Also drops the agent's `args:` passthrough, which "
                             "could otherwise carry a permission-override flag that "
                             "defeats the clamp. Deliberately NOT a general --permission "
                             "override: one of those would let any caller escalate any "
                             "agent, which is worse than the problem it solves")
    parser.add_argument("--gate-with", dest="gate_with", metavar="AGENT",
                        help="Require AGENT to approve this dispatch before it runs. The gate "
                             "is forced read-only and adjudicates the request (agent, prompt, "
                             "permission, cwd). FAILS CLOSED: anything but an explicit APPROVE "
                             "blocks the dispatch; UNCERTAIN sets requires_human_review")
    # `type=parse_timeout` was MISSING: the help promised "same grammar as --timeout" and
    # the flag parsed nothing, so `--gate-timeout 600s` reached the gate as the raw STRING
    # "600s" via `timeout = args.gate_timeout or args.timeout` (certification round 3 found
    # the guard bypass; the missing parser underneath it is the actual defect).
    parser.add_argument("--gate-timeout", dest="gate_timeout", default=None,
                        type=parse_timeout,
                        help="Timeout for the --gate-with dispatch (same grammar as --timeout; "
                             "defaults to --timeout)")
    parser.add_argument("--allow-credit", dest="allow_credit", action="store_true",
                        help="Authorize spending ACCOUNT CREDIT on a credit-only model "
                             "(Fable) for this one dispatch — flag form of "
                             "SUMMON_ALLOW_CREDIT=1. Single dispatch only: rejected for "
                             "--manifest/--council (set the env var deliberately for "
                             "fan-out spend)")
    parser.add_argument("--allow-payg", dest="allow_payg", action="store_true",
                        help="Authorize BytePlus PAYG (/api/v3) fallback if the Coding "
                             "Plan endpoint fails with quota/plan-limit/unsupported-model. "
                             "Single dispatch only: rejected for --manifest/--council "
                             "(set SUMMON_ALLOW_BYTEPLUS_PAYG=1 or ~/.agents/summon.json "
                             "for fan-out)")
    parser.add_argument("--allow-text-only", dest="allow_text_only", action="store_true",
                        help="Authorize a text-seat dispatch (openai-compat / arkcli +chat: "
                             "no FS/tools) for this one call. Flag form of "
                             "SUMMON_ALLOW_TEXT_ONLY=1; agent frontmatter "
                             "capability: text-only also opts in (still warns). "
                             "Single dispatch only: rejected for --manifest/--council "
                             "(set the env var deliberately for fan-out). Never auto-retry "
                             "a text_seat block with this flag")
    parser.add_argument("--require-tools", dest="require_tools", action="store_true",
                        help="Refuse text seats even when --allow-text-only / "
                             "capability: text-only / SUMMON_ALLOW_TEXT_ONLY=1 is set. "
                             "Flag form of SUMMON_REQUIRE_TOOLS=1")
    parser.add_argument("--json-schema", dest="json_schema",
                        help="Validate the agent's final JSON against this schema file; attach "
                             "parsed/parse_ok; one corrective retry via resume on mismatch")
    parser.add_argument("--artifact", dest="artifacts", action="append", default=[],
                        metavar="FILE",
                        help="Record loose-file provenance for an input under --cwd "
                             "(repeatable): path, bytes, sha256, and page metadata where "
                             "available; re-check after dispatch and mark changed baselines "
                             "suspect")
    parser.add_argument("--no-contract-repair", dest="no_contract_repair", action="store_true",
                        help="Disable the automatic ONE-shot corrective resume that fixes a "
                             "malformed report contract on a suspect success (status=success but "
                             "report_ok=false). Off by default; set this to save the extra call")
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
                        help="With --manifest: per-job envelope dir (default {cwd}/.agents/results). "
                             "With --council: alias for --run-dir (same precedence, above "
                             "SUMMON_RUNS_DIR)")
    parser.add_argument("--council", action="store_true",
                        help="Decide by consensus: dispatch --question to diverse members, "
                             "then a chairman synthesizes. See SKILL.md")
    parser.add_argument("--question", help="With --council: the decision/question to deliberate")
    parser.add_argument("--question-file", dest="question_file",
                        help="With --council: read the question from a file")
    parser.add_argument("--deliberate", action="store_true",
                        help="Run a bounded headless deliberation (separate from council)")
    parser.add_argument("--deliberate-resume", dest="deliberate_resume", metavar="RUN_ID",
                        help="Resume a deliberation run by id")
    parser.add_argument("--deliberate-recover", dest="deliberate_recover", metavar="RUN_ID",
                        help="Recover deterministic journal boundaries without provider calls")
    parser.add_argument("--deliberate-status", dest="deliberate_status", metavar="RUN_ID",
                        help="Read a deliberation run's journal-derived status")
    parser.add_argument("--deliberate-replay", dest="deliberate_replay", metavar="RUN_ID",
                        help="Replay a deliberation run's bounded checksummed journal")
    parser.add_argument("--deliberate-cancel", dest="deliberate_cancel", metavar="RUN_ID",
                        help="Queue a typed cancel command for a deliberation run")
    parser.add_argument("--deliberate-open", dest="deliberate_open", metavar="RUN_ID",
                        help="Open/reuse the authenticated local deliberation ledger")
    parser.add_argument("--chat-action", dest="chat_action",
                        choices=("open", "post", "show", "list", "turn", "cancel", "message", "inbox", "recover", "fork"),
                        help="Conversation room action; turn launches one bounded roster agent; "
                             "recover/fork never retry a provider")
    parser.add_argument("--chat-session", dest="chat_session", metavar="SESSION_ID",
                        help="Conversation room session id")
    parser.add_argument("--chat-participant", "--participant", dest="chat_participant", metavar="AGENT",
                        help="With chat turn/cancel/message/inbox: participant or sender roster agent id")
    parser.add_argument("--chat-to", dest="chat_to", metavar="AGENT",
                        help="With chat message: recipient roster agent id or human")
    parser.add_argument("--chat-after", dest="chat_after", type=int, default=0, metavar="CURSOR",
                        help="With chat inbox: return addressed messages after this cursor")
    parser.add_argument("--chat-participants", "--participants", dest="chat_participants",
                        help="With chat open: comma-separated participant roster ids")
    parser.add_argument("--chat-timeout", dest="chat_timeout", type=parse_timeout,
                        help="With chat turn: per-turn provider timeout")
    parser.add_argument("--chat-message", "--message", dest="chat_message",
                        help="Typed human context message for a conversation room")
    parser.add_argument("--chat-confirm", dest="chat_confirm", action="store_true",
                        help="With chat recover: explicitly attest that the unmatched turn was reviewed; "
                             "never retries the provider")
    parser.add_argument("--chat-reason", dest="chat_reason",
                        help="With chat fork: bounded human-readable reason for the new lineage")
    parser.add_argument("--chat-project-id", "--project-id", dest="chat_project_id",
                        help="Bounded project label for a new room")
    parser.add_argument("--chat-project-root", "--project-root", dest="chat_project_root",
                        help="Project root used only to bind a redacted project digest")
    parser.add_argument("--chat-initiator-host", "--initiator-host", dest="chat_initiator_host",
                        help="Initiating host label (codex, claude-code, cursor, terminal)")
    parser.add_argument("--chat-initiator-agent", "--initiator-agent", dest="chat_initiator_agent",
                        help="Initiating Summon agent id")
    parser.add_argument("--chat-mode", "--mode", dest="chat_mode", choices=("chat", "council", "deliberate"),
                        default="chat", help="Conversation room mode")
    parser.add_argument("--conversation-dir", dest="conversation_dir",
                        help="Root for provider-inert conversation room journals")
    parser.add_argument("--chat-browser", dest="chat_browser",
                        choices=("auto", "builtin", "ide", "system", "link"),
                        help="With chat open: reuse the local atlas in an IDE/browser, or return a link")
    parser.add_argument("--swarm-action", dest="swarm_action",
                        choices=("create", "status", "events", "register", "claim", "renew", "cancel", "close"),
                        help="Local provider-neutral swarm coordinator action")
    parser.add_argument("--swarm-run-id", dest="swarm_run_id", metavar="RUN_ID",
                        help="Swarm coordinator run id")
    parser.add_argument("--swarm-dir", dest="swarm_dir",
                        help="Private root containing durable swarm runs")
    parser.add_argument("--swarm-tasks", dest="swarm_tasks",
                        help="JSON task array for swarm create")
    parser.add_argument("--swarm-project-root-sha256", dest="swarm_project_root_sha256",
                        help="Receipt-bound project root digest for swarm create")
    parser.add_argument("--swarm-roster-sha256", dest="swarm_roster_sha256",
                        help="Receipt-bound roster definition digest for swarm create")
    parser.add_argument("--swarm-max-attempts", dest="swarm_max_attempts", type=int,
                        help="Maximum physical attempts per swarm task")
    parser.add_argument("--swarm-worker", dest="swarm_worker", metavar="WORKER",
                        help="Authenticated swarm worker id")
    parser.add_argument("--swarm-instance", dest="swarm_instance", metavar="INSTANCE",
                        help="Worker instance id for swarm registration")
    parser.add_argument("--swarm-task-id", dest="swarm_task_id", metavar="TASK_ID",
                        help="Swarm task id")
    parser.add_argument("--swarm-request-sha256", dest="swarm_request_sha256",
                        help="Receipt-bound task request digest")
    parser.add_argument("--swarm-lease-ms", dest="swarm_lease_ms", type=int,
                        help="Swarm claim/renew lease duration in milliseconds")
    parser.add_argument("--swarm-claim-id", dest="swarm_claim_id", metavar="CLAIM_ID",
                        help="Swarm claim id")
    parser.add_argument("--swarm-lease-generation", dest="swarm_lease_generation", type=int,
                        help="Swarm claim lease generation")
    parser.add_argument("--swarm-reason", dest="swarm_reason",
                        help="Bounded reason for swarm cancellation")
    parser.add_argument("--browser", choices=("auto", "builtin", "ide", "system", "link"),
                        default="auto",
                        help="With --deliberate-open: built-in/IDE bridge, system browser, or link")
    parser.add_argument("--seats",
                        help="With --deliberate: comma-separated immutable seat agent ids")
    parser.add_argument("--options",
                        help="With --deliberate: comma-separated immutable decision options")
    parser.add_argument("--max-attempts", dest="max_attempts", type=int,
                        help="With --deliberate: hard physical provider-launch budget")
    parser.add_argument("--deadline", type=parse_timeout,
                        help="With --deliberate: absolute run duration from start")
    parser.add_argument("--require-human-approval", dest="require_human_approval",
                        action="store_true",
                        help="With --deliberate: require typed approval after consensus")
    parser.add_argument("--retry-indeterminate", dest="retry_indeterminate",
                        action="store_true",
                        help="With deliberate resume: explicitly allow retry after uncertain spend")
    parser.add_argument("--command-id", dest="command_id",
                        help="With deliberate cancel: optional idempotency key")
    parser.add_argument("--text-only-consent", dest="text_only_consent", action="append",
                        default=[], metavar="SEAT",
                        help="With --deliberate: receipt-bound consent for one text-only seat")
    parser.add_argument("--full-authority-consent", dest="full_authority_consent",
                        action="append", default=[], metavar="SEAT",
                        help="With --deliberate: explicit consent for one full-authority seat")
    parser.add_argument("--members", help="With --council: comma-separated member agents "
                                          "(default: a vendor-diverse set)")
    parser.add_argument("--chairman", help="With --council: the synthesizer agent "
                        "(default: architect, which is Opus 5; pass fable explicitly for the "
                        "escalation tier)")
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
    parser.add_argument("--quorum", type=parse_quorum, metavar="N|all|FRACTION",
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
    parser.add_argument("--overall-timeout", dest="overall_timeout", type=parse_timeout,
                        help="With --council: a HARD wall-clock budget for the DELIBERATION "
                             "(every member and chairman dispatch, plus setup). On breach summon "
                             "process-tree-kills in-flight members and emits a PARTIAL council "
                             "envelope (status=partial) BEFORE the host's own timeout can kill it; "
                             "once spent it launches no further dispatch (queued members and the "
                             "fallback chairman are excluded). Per-stage timeouts still apply within "
                             "it. The parent's own final envelope serialization (no child running) "
                             "is not counted")
    parser.add_argument("--min-successful-members", dest="min_successful", type=int,
                        help="With --council: EARLY-EXIT threshold. Once this many members SUCCEED "
                             "in the final round, summon stops waiting for the stragglers "
                             "(process-tree-killing the in-flight ones and excluding the queued "
                             "ones) and chairs the surviving quorum immediately -- a pre-deadline "
                             "exit for 'we have enough, go now'. Must be >= --quorum (if set) and "
                             "<= the member count; chairs with council_state=early_exit and exits 0")
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
