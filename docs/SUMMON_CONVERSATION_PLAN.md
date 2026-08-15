# Summon conversation rooms

Status: provider-inert journal and authenticated loopback conversation atlas implemented;
interactive provider continuation remains gated. This is not a claim that live provider
chat is enabled.

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

The first provider-inert slice must support continuation entirely from the local
journal. Live provider resume remains behind the existing owner, deadline,
cleanup, and provider gates.

The first slice is available from the CLI:

```text
summon chat open SESSION_ID --project-id PROJECT --project-root DIR \
  --initiator-host codex --initiator-agent sol
summon chat post SESSION_ID --message "context for the room"
summon chat show SESSION_ID
summon chat list
summon chat open SESSION_ID --conversation-dir DIR --chat-browser auto
```

These commands use the local `.agents/conversations` journal (or
`--conversation-dir`), expose only the public event projection, and make zero
provider calls. The human message is context-only; it is never an approve,
deny, cancel, or ballot command.

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
and supports only public-redacted room reads and typed human-context posts. It is a
separate surface from the deliberation ledger and has no provider, ballot, or policy
authority.

For a local preview, keep the conversation root private and run:

```text
python skills/summon/scripts/_conversation_ui.py --serve <conversation-root>
```

The process prints one authenticated loopback URL. Reuse that URL while the process is
running; the fragment token is never sent to the server and must not be copied into a
prompt, ticket, report, or telemetry event. `summon chat open --chat-browser auto`
now starts or reuses this atlas and prefers an explicit IDE bridge, then a built-in
browser harness, then the system browser. Use `--chat-browser link` in CI/SSH to
return the URL without launching a tab. The surface remains provider-inert.

### `deliberate`

The governed decision room may display the discussion and human messages, but
its control plane remains separate: only receipt-bound ballots and typed human
commands can change the decision state. Model prose, chat messages, and chair
synthesis cannot approve, deny, lower the denominator, or authorize a launch.

## Event contract

The initial allowlist is:

`session_created`, `message_posted`, `human_message`, `turn_started`,
`turn_finished`, `council_round_started`, `position_submitted`, `cross_exam`,
`chair_synthesis`, `deliberation_policy_bound`, `ballot_accepted`,
`state_transition`, `cleanup_receipt`, and bounded `fork_created`.

Every event carries `session_id`, `cursor`, `generation`, `actor_kind`, and a
redacted payload digest. Public projections may expose role/name/version,
bounded prose summaries, option IDs, and evidence fingerprints; they must omit
prompts, argv, cwd, profiles, credentials, raw model output, and private paths.

Human messages are explicitly typed and attributed to the initiator. They are
never silently treated as approve/deny/cancel commands. Commands use the
existing durable command protocol and remain auditable.

## UI shape

The browser room should provide:

- a project/initiator conversation switcher;
- a persistent room header with mode, owner/connection, cursor, and continuation
  status;
- grouped round cards for council and deliberate, with participant role/name/
  version tooltips;
- a human message composer with clear “context only” versus “typed command”
  affordances;
- a fork/continue action that explains compatibility evidence before proceeding;
- progressive disclosure for evidence and raw technical details;
- terminal closure showing decision, dissent, cleanup, and the redacted export
  boundary.

## Delivery gates

1. Provider-inert session journal and strict event projection.
2. Real loopback browser fixture with two projects and three initiators, cursor
   replay, reconnect, fork, human message, and redaction tests.
3. Interactive council rounds and explicit human promotion into deliberate.
4. Deliberation room integration without changing kernel state authority.
5. Only then consider a provider-specific continuation gate, one vendor at a
   time, with no fallback or automatic retry.
