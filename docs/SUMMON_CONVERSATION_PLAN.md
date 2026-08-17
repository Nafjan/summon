# Summon conversation rooms

Status: durable local room journal, authenticated loopback atlas, and an explicit live
agent-turn seam are implemented. The first safety/readability slice now also uses a
cross-process cursor compare-and-swap before launch, emits bounded redacted human previews,
and preserves the triggering prompt when identity drift creates a fork. Live turns are
opt-in per room/participant and use the ordinary Summon dispatcher; they are not a new
authority kernel. Council/deliberate control remains separate and model output is still
context, never a ballot or approval. Cancellation now records a durable
`turn_cancel_requested` command that a separate runtime can observe; owner leases,
authenticated cursor streaming, and bounded reconnect are implemented. Durable
addressed agent messages are now available through a bounded local inbox; a durable
swarm coordinator remains a separate follow-up slice.

Summon should have one conversation substrate shared by ordinary brainstorming,
interactive councils, and governed deliberations. The substrate is a durable,
redacted event stream—not a second scheduler and not a replacement for the
deliberation journal.

## What a conversation is

Each room has a stable `session_id` and records:

- project identity (a bounded project label plus a hash of the canonical project root);
- initiator identity (`codex`, `claude-code`, `cursor`, terminal, or another host);
- the initiating Summon agent and its catalog-backed role/name/version display;
- participants, seat roles, session/thread handles, and exact served-model evidence;
- mode: `chat`, `council`, or `deliberate`;
- a monotonic cursor and append-only events.

The browser groups rooms first by project, then by initiating host/agent. A user
can switch between rooms without losing the current cursor or confusing a Codex
conversation with a Claude Code or Cursor conversation.

## Session continuation

An agent message is a turn in the same room, not an automatic new chat. A
provider session is resumed only when the stored provider, profile/account
evidence, model target, prompt contract, and permission boundary still match.
If any compatibility check fails, Summon creates an explicit fork and records
the reason; it never silently starts a replacement conversation or retries paid
work. A fork keeps the parent room and cursor visible.

The room runtime writes `turn_started` durably before launching a provider subprocess,
using the observed journal cursor as a compare-and-swap so a second process cannot claim
the same turn after the scan. It then appends a bounded public-redacted `message_posted`
and `turn_finished` record. Human messages retain native text only in the local journal
and expose a bounded redacted preview in public/browser projections. If continuation
identity drifts, the child fork carries the triggering prompt as a new human context event
instead of dropping it.
Each participant has one active turn at a time, while different participants may run in
parallel in the same room. A compatible provider resume handle is reused only when
provider/profile/account/model/prompt/permission/project/agent-definition evidence still
matches. Any mismatch creates an explicit child fork and does not launch the provider. A
crashed or killed process leaves an unmatched `turn_started` boundary; the next request
refuses it rather than guessing whether the provider spent money. The browser keeps one
runtime per surface and preserves independent active-participant state during refreshes,
so cancellation remains available for the selected live turn. Browser/CLI chat turns
are clamped to `read-only` by default; a caller may deliberately construct a runtime
with a lower-risk explicit ceiling, but participant frontmatter can never widen it.
The local runtime also fences process identity with a birth token; Windows uses a
`KILL_ON_JOB_CLOSE` Job Object and POSIX uses a dedicated process group so a dispatcher
that exits while a descendant still owns its pipes is reaped before `turn_finished`.
On Windows, if the shared Job Object cannot be attached (for example, a host forbids
nested jobs), the turn fails closed instead of claiming unconditional descendant
cleanup. Cross-owner takeover still cannot transfer a native process handle, so the
birth-token/taskkill path is conservative and remains a provider-specific GA gate.
Every `.chat-runtime` lease component (root, session, and participant) is checked
before and after creation; symlinks, junctions, and non-directory replacements are
rejected so owner locks and process records cannot be redirected outside the room.
Cross-process takeover cannot transfer a native process handle, so an expired-owner
cleanup still requires a matching birth token and reports uncertainty rather than
terminating an unverified PID.

The first slice is available from the CLI:

```text
summon chat open SESSION_ID --project-id PROJECT --project-root DIR \
  --initiator-host codex --initiator-agent sol
summon chat post SESSION_ID --message "context for the room"
summon chat turn SESSION_ID AGENT --message "ask the participant"
summon chat cancel SESSION_ID AGENT
summon chat message SESSION_ID FROM_AGENT TO_AGENT --message "context for the peer"
summon chat inbox SESSION_ID AGENT [--chat-after CURSOR]
summon chat recover SESSION_ID AGENT --chat-confirm
summon chat fork SESSION_ID AGENT --message "continue this in a new lineage"
summon chat show SESSION_ID
summon chat list
summon chat open SESSION_ID --conversation-dir DIR --chat-browser auto
```

These commands use the local `.agents/conversations` journal (or
`--conversation-dir`). `open`, `post`, `show`, `list`, `message`, and `inbox` make zero
provider calls. `turn` is the explicit exception: it launches the named roster agent
only after the durable start fence; `cancel` appends a durable typed command and
targets the active worker when one is present, so another runtime can request
cancellation without an in-memory handle. `message` appends an addressed context event
from one admitted participant to another participant or the human; `inbox` reads the
local native message/context stream after a cursor for an agent process. These messages
cannot approve, deny, cancel, vote, or launch. `recover` requires an explicit human
attestation, closes an unmatched turn as indeterminate, and never retries; `fork` creates
a new context lineage with no provider contact. The human message is context-only; it is
never an approve, deny, cancel, or ballot command. CLI turns wait for the provider process
to finish so the parent cannot exit before `turn_finished` is journaled; the browser keeps
one runtime per surface for interactive cancellation.

## Modes

### `chat`

Open brainstorming with selected participants. There is no quorum and no ballot;
the human can post a message at any point. Responses are useful context, not
authorization. A chat can be promoted only by an explicit human action into a
fresh council or deliberate setup.

### `council`

An interactive council adds bounded rounds to the conversation room:

1. independent positions;
2. optional cross-examination or challenge questions;
3. a chaired synthesis and dissent summary;
4. a human-readable candidate-option proposal, if one emerges.

The human may chime in between rounds. The candidate proposal is still not a
ballot. Promotion to `deliberate` requires the human to fix option IDs, named
seats, quorum, rounds, physical-attempt budget, absolute deadline, and approval
policy in a new receipt.

The provider-inert journal now supports bounded round-start, position, cross-exam,
and chair-synthesis events plus a context-only recommendation artifact. The
promotion helper requires an explicit human confirmation and returns setup data;
it does not create a receipt, cast a ballot, or contact a provider.

The loopback conversation surface is available from `_conversation_ui.py` for a private
conversation root. It groups rooms by full project-root digest and initiating host/agent,
uses a bearer token in the URL fragment plus an authorization header for API requests,
and supports public-redacted room reads, bounded cursor `/events` reads, authenticated
`/stream` SSE frames, typed human-context posts, and an explicit `turns`/`cancel`
endpoint. The “Ask a roster agent” control shows participant role/name/
version, records the durable turn lifecycle, and explains that output is context only.
It is a separate surface from the deliberation ledger and has no ballot or policy
authority. Start the atlas with `chat open ... --cwd PROJECT` when agent turns
need to run against a project; a bare `--serve ROOT` invocation remains useful for
observation but cannot safely bind rooms from multiple unknown project roots.
The private surface handoff record stores SHA-256 digests for the requested `--cwd`
and explicit `--agents-dir` (never their absolute paths). A live atlas is reused only
when those bindings match; a legacy record without the digests is readable for an
unbound observer, but a bound caller receives an explicit mismatch instead of
silently attaching to another project's runtime.

For a local preview, keep the conversation root private and run:

```text
python skills/summon/scripts/_conversation_ui.py --serve <conversation-root>
```

The process prints one authenticated loopback URL. Reuse that URL while the process is
running; the fragment token is never sent to the server and must not be copied into a
prompt, ticket, report, or telemetry event. `summon chat open --chat-browser auto`
now starts or reuses this atlas and prefers an explicit IDE bridge, then a built-in
browser harness, then the system browser. Use `--chat-browser link` in CI/SSH to
return the URL without launching a tab. Room reads and human posts remain
provider-inert; the explicit Ask-a-roster-agent control is the only provider-backed
exception and requires the surface's `--cwd`/roster binding. The surface close path
durably cancels and joins locally owned turns before shutting down the HTTP server;
cross-process cancellation remains bounded by the recorded owner/process lease.

### `deliberate`

The governed decision room may display the discussion and human messages, but
its control plane remains separate: only receipt-bound ballots and typed human
commands can change the decision state. Model prose, chat messages, and chair
synthesis cannot approve, deny, lower the denominator, or authorize a launch.

## Event contract

The initial allowlist is:

`session_created`, `message_posted`, `human_message`, `agent_message`, `turn_started`,
`turn_finished`, `turn_cancel_requested`, `council_round_started`, `position_submitted`, `cross_exam`,
`chair_synthesis`, `deliberation_policy_bound`, `ballot_accepted`,
`state_transition`, `cleanup_receipt`, and bounded `fork_created`.

Every event carries `session_id`, `cursor`, `generation`, `actor_kind`, and a
redacted payload digest. Public projections may expose role/name/version,
bounded prose summaries, option IDs, and evidence fingerprints; they must omit
prompts, argv, cwd, profiles, credentials, raw model output, and private paths.

Human messages are explicitly typed and attributed to the initiator. They are
never silently treated as approve/deny/cancel commands. `agent_message` is
addressed to one admitted participant or `human`; the native journal retains the
message for the recipient's local inbox while public projections expose only a
redacted preview and digest. Commands use the existing durable command protocol and
remain auditable.

## UI shape

The browser room now uses a clean-room, Chatpack-informed messenger arrangement. Chatpack
is inspiration only; Summon does not ship its Node packages, copy its source, or add a
runtime dependency. The Python standard-library server, append-only journal, bearer
fragment/header authentication, and public-redaction boundary remain authoritative.

The atlas provides:

- a searchable project → initiator → room rail with mode and cursor counts;
- a persistent room header with connection, mode, cursor, and role/name/version
  participant chips;
- a grouped messenger timeline: operator context bubbles, agent context bubbles,
  and compact lifecycle/evidence chips for council or deliberate records;
- an explicit composer split between “Post context” and “Ask a roster agent”; context
  is never a control command, while turns are durably fenced and visibly cancellable;
- a redacted evidence drawer for root fingerprints, initiator, public cursor, and
  authority boundaries; private prompts, paths, credentials, and native output never
  enter the page;
- incremental cursor rendering with duplicate suppression, gap refetch, reconnect,
  and bounded polling fallback; an unread marker is held in memory only;
- a responsive mobile rail and docked composer with keyboard focus, named controls,
  reduced-motion behavior, and no forced autoscroll while the operator is reading;
  the composer stays below the thread on phones so it never hides the latest event.

Council and deliberate keep their authority distinction: council positions, cross-exams,
and chair synthesis are context artifacts; deliberate policy, ballots, state transitions,
and cleanup remain kernel receipts. The browser never infers a vote or approval from prose.

## Delivery gates

1. Durable session journal, strict event projection, and the before/after-provider turn
   fence with explicit continuation/fork behavior.
2. Real loopback browser fixture with two projects and three initiators, cursor
   replay, reconnect, fork, human message, and redaction tests.
3. Interactive council rounds and explicit human promotion into deliberate.
4. Deliberation room integration without changing kernel state authority.
5. Only then consider a provider-specific continuation gate, one vendor at a
   time, with no fallback or automatic retry.
