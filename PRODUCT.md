# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

delegated: dependency-free static HTML/CSS/JavaScript served by the existing
stdlib-only Python runtime; no frontend framework or hosted service is required.

## Users

Primary users are developers and AI operators who run Summon from an IDE, CLI, or
desktop agent and need to observe and guide a multi-agent deliberation while it is
running. They may be coding, researching, or making a design/architecture decision and
need the control surface to remain unobtrusive beside their working application.

## Product purpose

Summon coordinates heterogeneous AI CLIs behind one structured dispatcher. The
deliberation feature lets several bounded participants exchange auditable turns, produce
structured ballots, and reach a fixed-policy outcome with optional human approval. A
shared conversation room supports provider-inert brainstorming/human context plus an
explicit bounded roster-agent turn path, session continuation, and interactive council
rounds; it must keep chat context separate from the deliberation control plane. The local
view should make live state, evidence,
pending human actions, and retained resources understandable without turning prose into
authority.

## Positioning

Summon's distinctive mechanism is governed cross-vendor work: the dispatcher, not model
prose, owns scheduling, permissions, physical launch budgets, durable replay, cleanup,
and the final control decision. A participant's identity or claimed verdict is evidence,
not authority.

## Operating context

Runs are initiated by a calling agent or terminal and may involve Claude, Codex, Cursor,
Kimi, Antigravity, Gemini-compatible, or OpenAI-compatible backends. The control surface
is loopback-only, offline, and user-invoked. A single scheduler owner writes a checksummed,
generation-fenced journal. The browser can append human context, start a bounded roster-agent
chat turn when the caller supplies a project and roster binding, and queue typed deliberation
commands. It cannot change deliberation policy, approve a ballot, or activate a deliberation
provider. Users may inspect a run during execution, review a finished run, replay durable
events, queue typed cancellation, and see `LEFT_BEHIND` or environment-handoff details.
Approve, deny, and richer human-command application remain separately gated capabilities.

## Capabilities and constraints

- Existing council and dispatch modes remain behaviorally compatible.
- Conversation rooms are a shared, project- and initiator-grouped substrate for chat,
  interactive council, and deliberation discussion views; continuation requires explicit
  compatibility evidence or an auditable fork.
- Deliberation supports 2-10 fixed seats, bounded turns, immutable options, fixed quorum,
  physical launch limits, absolute deadlines, deterministic terminal precedence, and
  structured ballots.
- Core runtime remains Python stdlib-only and cross-platform, including Windows hidden
  process launches.
- The local UI is a redacted observer and an explicit live-chat surface. It can append human
  context and, when the caller supplies a project/roster binding, start one durable agent turn
  through the dispatcher with a pre-launch journal fence, continuation identity checks, and
  process-tree cancellation. Chat turns are context work, not policy authority: they cannot
  approve a deliberation, change its quorum/options, or silently activate a provider. The
  deliberation control plane remains separately gated and provider-inert where its live
  integration is not explicitly enabled. This is open product functionality, not a premium tier.
- No remote hosting, multi-user networking, `0.0.0.0` binding, model-rendered HTML, or
  model-controlled browser actions are allowed in the first release.
- Backend identities, prompts, credentials, private paths, and raw provider output must
  remain redacted from public exports and telemetry.

## Brand commitments

The product name is Summon. The interface should feel precise, calm, trustworthy, and
operational rather than like a generic chat client. [Beautiful UI's published license page](https://www.beautifului.dev/license)
states an MIT license; Summon borrows its compact AI-workbench rhythm, card anatomy, and
status language while keeping an independent, dependency-free implementation. The
conversation atlas now includes a clearly attributed visual adaptation of Beautiful
UI’s centered canvas, striped surround, dashed hairlines, and compact card rhythm.
It remains a local rewrite: no remote package, runtime, font, or hosted asset is
loaded. The full MIT notice is in `THIRD_PARTY_NOTICES.md`; any future source-level
reuse must retain that notice.

## Evidence on hand

- `README.md` documents the dispatcher, council, manifest, cross-vendor, and local-model
  workflows.
- `docs/DELIBERATION_PRODUCT_PLAN.md` is the current deliberation contract and safety plan.
- The provider-inert kernel, scheduler, roster, invocation, replay, and restore modules
  and their focused tests are in `skills/summon/scripts/`.
- A stdlib loopback observer/browser handoff and durable chat-turn runtime are present. The
  chat runtime is deliberately scoped to a persistent local surface: a CLI turn waits for its
  child to finish, while the browser keeps one runtime for interactive turns and cancellation.
  Full deliberation provider activation, richer human-command application, and complete
  real-journal lifecycle coverage remain separately gated preview work.

## Product principles

1. Show the durable truth, not model theater.
2. Keep human control explicit, local, and reversible where possible.
3. Make uncertainty, cleanup, and retained resources visible without alarmism.
4. Preserve privacy and offline operation by default.
5. Let the task and state lead the interface; visual character must never obscure control.

## Accessibility and inclusion

The local surface must support keyboard navigation, visible focus, semantic headings and
live-region updates, reduced motion, sufficient contrast, responsive layouts, and clear
text alternatives for status, color, and icon treatments. WCAG 2.2 AA is the working
baseline. Summon does not claim independent accessibility certification.
