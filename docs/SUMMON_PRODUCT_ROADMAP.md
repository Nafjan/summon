# Summon product roadmap

Status date: 2026-08-16
Current source version: `3.0.0` candidate
Readiness: the immutable `3.0.0` tag remains the public-preview baseline, while the
current candidate contains the authenticated chat/runtime and browser hardening described
below. The candidate has a clean source-bound schema-2 Claude Opus receipt and all named
machine gates pass; it is not yet a published GA tag. Additional provider receipts,
remote execution, and multi-user hosting remain separately gated.

This is the release-facing roadmap for turning the current, well-tested kernel into a dependable open product. It records what is actually shipped, what was tested, and the gates that must pass before we describe a feature as shipped, preview, or live-provider-enabled.

## Release snapshot

| Area | Current truth | Release posture |
| --- | --- | --- |
| Ordinary dispatch, council, ACP, telemetry | Implemented and locally exercised | Ready for continued public use |
| Cross-vendor roster lanes | Frontier/near-frontier labels are editorial catalog metadata; Claude Opus has a reviewed receipt, while Fable and Gemini availability still require saved receipts with exact `model.served` and profile evidence | Claude Opus route verified; other live lanes remain gated |
| Deliberation kernel, journal replay/recovery, scheduler | Focused `test_deliberation_*.py` suite; provider-inert | Ready as a fake/injected integration surface |
| Custom Agent manifests and roster binding | Strict parser, path fencing, consent/identity binding | Ready as provider-inert configuration |
| Live provider composition | Receipt/plan/owner/deadline fences exist; the current source-bound packet passes the schema-2 Claude Opus normal/cancel/deadline pilot | Claude Opus route is live-provider-gated; other routes remain provider-inert until independently reviewed |
| Browser conversation surface | Authenticated loopback, stale-record fencing, bounded requests, redaction, accessibility, visual evidence, and explicit cancellable roster-agent turns; richer multi-run lifecycle remains preview | Unreleased post-GA preview |
| Managed local installs | Eight managed host copies converge on the candidate's 3.0.0 source payload; unmanaged Cursor plugin drift is reported and intentionally untouched | Candidate gate passed; unmanaged drift is not installer-owned |
| Unmanaged Cursor local plugin | Older 2.2.0 content hash; not installer-owned | Update through its plugin mechanism only |

Telemetry is enabled for this workstation through `~/.agents/summon-telemetry.json`. “On” means bounded, allow-listed local JSONL diagnostics only; it does not transmit data. GitHub issue submission remains a separate explicit action.

## Summon 3.0 release contract

The release council agrees that 3.0 must be a product-quality boundary, not a
version-number rename. The 3.0.0 release is GA only because every fixed suite and
gate below is machine-recorded from a clean immutable commit.

### Must ship for 3.0

1. Preserve ordinary dispatch, manifest, council, ACP, envelope, and install
   compatibility, with a documented 2.x → 3.0 migration, rollback, and supported-host
   matrix.
2. Complete a fake-provider end-to-end lifecycle: durable-before-spawn, owner lease and
   takeover, deadline, cancellation, uncertain spend, cleanup/LEFT_BEHIND, torn-tail
   repair, replay/recovery, human-command lifecycle, and exactly-once launch accounting.
3. Make the local conversation atlas production-coherent: authenticated loopback,
   project/initiator grouping, human context, council rounds, explicit continuation/fork
   semantics, SSE cursor/reconnect, terminal and owner-loss states, no duplicate surfaces,
   bounded request handling, and strict redaction.
4. Harden both browser surfaces against stale/fake records, symlink/junction roots,
   unreachable endpoints, orphaned child processes, stale locks, and slow clients.
5. Ship interactive `/council` rounds and a bounded recommendation artifact with an
   explicit human promotion into a fresh `/deliberate` receipt. No policy or ballot may
   be inferred from prose.
6. Make Custom Agents minimally useful through a documented manifest workflow,
   `summon agents validate`, source/digest/effective-permission evidence, and prompt
   contribution only. A manifest never grants authority.
7. Complete browser accessibility and visual regression evidence: WCAG 2.2 AA baseline,
   keyboard/focus/live-region/reduced-motion behavior, desktop/tablet/mobile screenshots,
   and Impeccable-clean output.
8. Pass one independently gated live deliberation provider. The gate proves exact
   `model.served`, profile/backend/consent evidence, owner/deadline/process fencing,
   cancellation, kill-switch behavior, cleanup, and honest uncertain-spend handling with
    no hidden retry or fallback. Gemini 3.7 Flash High is a candidate route, not a
    current availability claim; additional providers must earn separate evidence and
    do not inherit any claim.
9. Keep telemetry opt-in by default in clean installs, local-only, bounded, redacted,
   and covered by disable/clear/support-bundle tests.

### Explicitly deferred from 3.0

Remote hosting, cloud sync, multi-user networking, `0.0.0.0` binding, model-controlled
browser actions, dynamic/recursive deliberations, live session resume without stable
provider session IDs, multi-provider deliberation, retries/fallback inside deliberation,
custom-agent marketplaces, hosted chat/Chatpack dependencies, ads/Freebuff, and updates
to unmanaged Cursor plugins.

### Release decision rule

No open P0/P1 reliability, security, privacy, migration, or evidence contradiction may
remain. The release manifest is generated from a clean, immutable source tree and
requires the named suites and gates rather than accepting arbitrary caller-supplied
labels. The latest local fixed-registry run (not a GA claim) recorded: deliberation
335/335, resume 15/15, discovery 549/549, conversation 39/39, runtime 24/24, UI
19/19, install 30/30, model catalog 6/6, model routing 4/4, release contract 3/3,
release gates 7/7, release manifest 16/16, live-provider gate tests 3/3, swarm
protocol 12/12, and ACP 36/36. The registry is source-bound and clean; every named
machine gate passes, including the reviewed schema-2 Claude Opus receipt. The candidate
still needs the final immutable GA tag/publication and any additional provider receipts
we choose to advertise.

## Product direction

Summon has one open product surface. There is no tiered UI fork, hosted deliberation
service, or second scheduler. The dispatcher, `/council` and `/deliberate` companion skills,
durable journal, local browser observer, custom-agent provenance, and provider adapters
share the same versioned contracts. Technical modules may be installed separately, but
that is packaging—not a capability boundary.

Use these readiness words consistently:

- **implemented** — code and focused tests exist;
- **public preview** — users may invoke the local/provider-inert surface, but a stated
  lifecycle gate is still open;
- **provider-inert** — the path makes zero provider calls;
- **live-provider-gated** — one or more explicit owner/deadline/cleanup gates remain
  closed; never imply live spend is available.

The secondary `/council` and `/deliberate` skills are thin entry points into the same
dispatcher. They do not duplicate scripts or schemas. Use `/council` for open-ended
positions, cross-examination, and chaired synthesis; use `/deliberate` only for
fixed-option, quorum-controlled, receipt-bound decisions. A council can hand a
candidate option set to a human, but it cannot silently become a ballot or change a
deliberation receipt.

## Milestones toward a complete product

| Milestone | Outcome | Release label |
| --- | --- | --- |
| 2.2.x — release integrity | Repeatable suites, clean warnings, generated release manifest, converged managed installs, honest observer preview | public preview |
| 2.3 — durable local control | Real-journal browser fixture, reconnect/takeover/torn-tail/terminal/cleanup paths, owner/lease/action truth | public preview |
| 2.4 — governed agents | Custom Agent validation, migration diagnostics, receipt-bound prompt contributions, fake-provider end-to-end flow | implemented/provider-inert |
| 2.4b — council handoff | `/council` companion, bounded recommendation artifact, explicit human promotion into a fresh `/deliberate` receipt | public preview |
| 2.5 — one live-provider pilot | One explicitly gated provider with durable-before-spawn, cancellation, deadline, cleanup, kill switch, and takeover evidence | live-provider-gated |
| 2.6 — conversation rooms | Persistent chat substrate, authenticated loopback conversation atlas grouped by project and initiating host/agent, human messages, bounded roster-agent turns with explicit continuation/fork, and bounded council-round artifacts | public preview |
| 2.7 — daily browser workflow | Round-oriented ledger, filters/export, keyboard/mobile actions, terminal summaries, WCAG 2.2 AA and visual regression | public preview |
| 2.7b — swarm protocol contract | Versioned `summon.swarm/v1` stdio framing, worker/claim/message/artifact/cancel schemas, golden vectors, and fake-adapter conformance; no native IDE attachment yet | contract-only preview |
| 2.8 — product operations | Supported-version matrix, rollback/install manifest, stable machine schemas, local diagnostics, sanitized support bundle | gated; contract defined in [`docs/VERSIONING_AND_3.0.md`](VERSIONING_AND_3.0.md) |
| Later — provider expansion | Each transport earns its own independent evidence gate; no umbrella live-ready claim | live-provider-gated |

## P0 — release hygiene and reliability (candidate gate)

1. **Keep the ordinary dispatcher suite deterministic.** The two early-exit tests now use a dispatch barrier and the broad run is `549/549`; keep the repetition check in CI. Treat a timeout or missing stage as a test failure, never as a silent pass.
2. **Close installer asset drift.** Keep `examples/` in the owned payload and manifest (now fixed); add a source-vs-install manifest check to CI. Never update archival worktrees or unmanaged plugin copies.
3. **Remove resource warnings.** ✅ Closed the bounded Git-reader pipes in `_receipt.py`; the
   fixed release registry (currently 335 deliberation, 15 resume, 39 conversation, 24 runtime,
   19 UI, 549 discovery, 30 install, 36 ACP, 6 catalog, 4 routing, 16 release-manifest,
   7 release-gates, 12 swarm, 3 live-provider-gate, and 3 release-contract tests) is now warning-clean rather than
   merely assertion-clean.
4. **Publish one release manifest.** `python tools/release_gates.py` runs the fixed test and
   gate registries and emits source/Git-bound output digests plus one artifact per gate;
   `python tools/release_manifest.py --evidence-file ...` binds that evidence to the full
   release and verification tree. `--check` remains the GA gate: it fails on a dirty tree,
   invalid owned payloads, missing install convergence, or any named gate that is not
   machine-recorded as `pass`. A missing live-provider receipt is therefore visible as
   `blocked`, not silently treated as `not_run` or inferred from a CLI being installed.
5. **Make browser handoffs discoverable.** Document both `deliberate open RUN_ID` and
   `chat open SESSION_ID --chat-browser ...`, keep one local surface per run, and
   preserve `--browser link`/`--chat-browser link` for non-interactive callers. These
   public observer paths remain separate from live-provider activation.
6. **Keep model routing explicit.** The catalog records the current editorial order—Fable,
   Sol, Opus, Kimi as clear frontier; Grok 4.6, Gemini Flash 3.7, GLM 5.2, DeepSeek V4
   Flash, and DeepSeek V4 Pro as near-frontier/high-value. Pin `researcher` to
   `gemini-3.7-flash-high` as the fast independent evidence lane; keep Fable behind its
   named `fable-fallback` profile and require `model.served`/profile evidence in every
   release smoke. Tooltips may show role/name/version and the editorial lane, but labels
   never imply availability: exact `model.served` evidence remains separate and no lane
   silently falls back to another model.

Exit gate: ✅ broad dispatcher suite is repeatable; install/doctor reports converged managed
copies; no unexplained warnings; no dirty or foreign tree is overwritten. The final tag and
publication remain an explicit release-owner action after this candidate review.

## P1 — make the deliberation product complete

### Shared conversation rooms

Summon now has one conversation substrate for ordinary brainstorming, interactive
`/council`, and the discussion view around `/deliberate`. Room reads and human context
remain provider-inert; an explicitly requested roster-agent turn uses a bounded live
runtime. A room has a stable session ID, project identity, initiating
host/agent, participants, mode, monotonic cursor, and an append-only redacted
event stream. The browser groups rooms by project and then by initiator so a
Codex-started conversation cannot be confused with a Claude Code or Cursor
conversation. The authenticated loopback conversation atlas groups rooms by
project and initiating host/agent; `chat open --chat-browser` starts or reuses
one surface without opening duplicate tabs. See [`SUMMON_CONVERSATION_PLAN.md`](SUMMON_CONVERSATION_PLAN.md)
for the event and continuation contract.

Continuation resumes the same provider session only when provider/profile/account,
model, prompt contract, permission, project, and agent-definition evidence still match.
Otherwise Summon creates an explicit fork and records why; it never silently opens a new
chat or retries paid work. Different participants may run in parallel, but each
participant has one active turn and an unmatched start blocks the next turn. The larger
two-project/three-initiator lifecycle fixture and provider-specific live evidence gates
remain open work. Local OS-process cancellation now has birth-token fencing plus
descendant cleanup coverage; cross-process takeover remains conservative when a native
process handle cannot be transferred.

Interactive council rounds may include independent positions, cross-examination,
chair synthesis, and human messages between rounds. A council recommendation
remains context, not a ballot. Promotion to deliberate is an explicit human
boundary that fixes options, seats, quorum, rounds, attempt budget, deadline,
and approval policy in a fresh receipt.

### Durable run truth

- Add a real loopback integration fixture backed by `initialize_run`, tagged journal records, torn-tail repair, owner takeover, cleanup failure/`LEFT_BEHIND`, uncertain spend, terminal decisions, and queued human commands.
- Exercise status, replay, SSE, and POST against that same run. Assert zero provider calls for recovery paths.
- Finish the browser security contract: session-token exchange, restart/owner-loss invalidation, monotonic command IDs, cursor replay/gap detection, reconnect behavior, and disconnect-safe SSE writes.
- Make the UI show owner/lease health, terminal option and reason, cleanup `verified` **and** `clean`, uncertain spend, deadline countdown, stale/reconnected state, and an explicit local/redacted disclosure.

### Journal and command semantics

- Keep every control decision durable-before-provider-contact.
- Make the human action lifecycle visible: queued → durable → applied/rejected/blocked.
- Preserve bounded event projections and never expose prompts, argv, credentials, profile paths, or raw provider output.

Exit gate: a real browser can observe and cancel a journal-derived run through reconnect, takeover, torn-tail, terminal, and cleanup scenarios with no contradictory status and no secret in HTTP, SSE, DOM, or telemetry.

### Council → deliberate handoff

- Keep `/council` open-ended: independent positions, optional cross-examination, and
  a chaired synthesis remain its control model.
- Add a bounded, redacted recommendation artifact that may list candidate option IDs,
  trade-offs, dissent, and provenance without claiming a ballot or approval.
- Require a human to select/confirm the option set and provide seats, quorum, rounds,
  attempt budget, deadline, and approval policy before creating a fresh `/deliberate`
  receipt. The handoff must never reuse hidden council state or auto-spend.

Exit gate: a council recommendation can seed a deliberate setup screen/command while
every policy field is visible, editable, receipt-bound, and independently auditable.

## P2 — activate one live provider safely

Enable one provider/transport at a time behind an explicit feature gate. For each provider:

1. derive policy, schedule, roster, plan identity, consent, worktree, profile evidence, and timeout from the immutable receipt;
2. prove the final owner/deadline/snapshot fence immediately before contact;
3. record exactly one physical attempt before spawn and classify timeout/cancel/uncertain spend honestly;
4. register every credential-bearing temporary profile/resource before contact and verify identity on cleanup;
5. run a disposable-worktree, owner-loss, cancellation, deadline-crossing, process-tree, and replacement-path matrix;
6. ship a kill switch and a no-network/no-provider fake mode in CI.

Kimi, ACP secondary paths, retries, fallback, repair, and full-bypass authority remain disabled until their own evidence and cleanup gates pass. No “live-ready” label is valid from unit tests alone.

## P3 — turn Custom Agents into a user feature

The current manifest work is deliberately provider-inert. The next step is to make a custom agent useful without weakening governance:

- expose a documented workspace `.agents/agents/<slug>/agent.md` workflow and the
  provider-inert `summon agents validate` command;
- resolve role/body/skills into a prompt contribution while keeping authority, model, transport, cwd, consent, and worktree fields receipt-bound;
- show source, definition digest, and effective permission in local/native evidence while redacting bodies and paths from public views;
- add migration/duplicate diagnostics around the validated manifest source;
- add one fake provider end-to-end path before any live provider activation.

This is where Antigravity-style custom agents become useful to Summon: a named role can be versioned, reviewed, and reused across hosts, while Summon remains the governance layer that decides what that role may actually do. A manifest is a role contract, not an authority grant.

## P4 — make the browser surface a daily tool

- Replace generic event JSON with round-grouped turns: participant role, executor facts, ballot, evidence, and expandable bounded detail.
- Preserve open disclosures and focus across incremental updates; add latest-event jump, filters, keyboard shortcuts, and a sticky mobile action shelf.
- Add explicit terminal closure: decision, confidence/evidence summary, cleanup receipt, retained-resource handoff, and export boundary.
- Meet WCAG AA for small labels, status text, focus, live regions, and error recovery.
- Add visual regression screenshots at desktop, tablet, and narrow mobile widths; keep Impeccable detector output clean and review screenshots in the repository’s release evidence directory.

## P5 — operations, packaging, and trust

- Add a supported-version matrix for Python and host CLIs, with a signed/hashed install manifest and rollback instructions.
- Provide `doctor`, `status`, `replay`, `recover`, and telemetry commands with stable machine-readable schemas and useful exit codes.
- Add bounded local metrics (latency, attempts, cleanup, drift) with explicit opt-in and a clear privacy policy; never turn local opt-in into silent network collection.
- Add a support bundle/bug-report workflow that is sanitized by construction and requires explicit review before upload.
- Document billing boundaries, provider terms, unsupported transports, and what “uncertain spend” means to an operator.

## Definition of complete

Summon is a complete product when a new operator can install it on a supported host, validate the environment, define or select a governed agent, run a real deliberation in a disposable workspace, observe the durable journal in the browser, cancel or approve with an auditable command, recover after a crash or takeover without duplicate paid work, and export a redacted report—while CI proves the same invariants with fake providers and each live provider has an explicit, independently reviewed gate.

For routes without their own receipt, use the kernel, fake/injected adapters, ordinary
dispatch, council, ACP fixtures, local telemetry, and browser preview confidently;
keep those live-deliberation and provider-integration claims explicitly gated.
