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

## Product Purpose

Summon coordinates heterogeneous AI CLIs behind one structured dispatcher. The
deliberation feature lets several bounded participants exchange auditable turns, produce
structured ballots, and reach a fixed-policy outcome with optional human approval. The
local view should make the live state, evidence, pending human actions, and retained
resources understandable without becoming a chat-first product.

## Positioning

Summon's distinctive mechanism is governed cross-vendor work: the dispatcher, not model
prose, owns scheduling, permissions, physical launch budgets, durable replay, cleanup,
and the final control decision. A participant's identity or claimed verdict is evidence,
not authority.

## Operating Context

Runs are initiated by a calling agent or terminal and may involve Claude, Codex, Cursor,
Kimi, Antigravity, Gemini-compatible, or OpenAI-compatible backends. The control surface
is loopback-only, offline, and optional. A single scheduler owner writes a checksummed,
generation-fenced journal; local browser handlers may queue typed commands but never
launch providers or mutate policy. Users may inspect a run during execution, review a
finished run, replay durable events, approve or deny a human gate, cancel, and see
LEFT_BEHIND/environment-handoff details.

## Capabilities and Constraints

- Existing council and dispatch modes remain behaviorally compatible.
- Deliberation supports 2-10 fixed seats, bounded turns, immutable options, fixed quorum,
  physical launch limits, absolute deadlines, deterministic terminal precedence, and
  structured ballots.
- Core runtime remains Python stdlib-only and cross-platform, including Windows hidden
  process launches.
- The first UI slice is a provider-inert observer over the redacted store; it queues only
  typed cancel while owner-bound command recovery, store resume, and richer human-command
  lifecycle gates remain pending.
- No remote hosting, multi-user networking, `0.0.0.0` binding, model-rendered HTML, or
  model-controlled browser actions are allowed in the first release.
- Backend identities, prompts, credentials, private paths, and raw provider output must
  remain redacted from public exports and telemetry.

## Brand Commitments

The product name is Summon. The interface should feel precise, calm, trustworthy, and
operational rather than like a generic chat client. Beautiful UI is an inspiration
reference only; authoritative OSS/permissive licensing and provenance are not established
for its source, so no source or copied component is part of this product.

## Evidence on Hand

- `README.md` documents the dispatcher, council, manifest, cross-vendor, and local-model
  workflows.
- `docs/DELIBERATION_PRO_PLAN.md` is the current deliberation contract and safety plan.
- The provider-inert kernel, scheduler, roster, invocation, replay, and restore modules
  and their focused tests are in `skills/summon/scripts/`.
- No existing browser surface, design system, product screenshots, or production UI
  assets are present in this repository.

## Product Principles

1. Show the durable truth, not model theater.
2. Keep human control explicit, local, and reversible where possible.
3. Make uncertainty, cleanup, and retained resources visible without alarmism.
4. Preserve privacy and offline operation by default.
5. Let the task and state lead the interface; visual character must never obscure control.

## Accessibility & Inclusion

The local surface must support keyboard navigation, visible focus, semantic headings and
live-region updates, reduced motion, sufficient contrast, responsive layouts, and clear
text alternatives for status/color/icon treatments. Exact conformance target remains an
open decision; use WCAG 2.2 AA as the delegated baseline until the user specifies another.
