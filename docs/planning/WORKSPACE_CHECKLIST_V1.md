# Summon workspace implementation checklist — v1

Status: planning draft for seven-seat advisory review. No implementation or release approval.

## Product decision and scope

Build one local workspace for tasks, agents, messages, reviews and decisions. Preserve the ordinary dispatcher, local swarm coordinator, council review kernel and fixed-option deliberation kernel. Introduce a facade and explicit adapters; do not replace the working runtime wholesale. A proposed next minor delivers reliability closure and a narrow workspace preview. Later phases earn independent qualification.

This checklist describes intended work, not completed capabilities. Every unchecked item requires implementation or verification. A source-backed existing feature still needs compatibility coverage when integrated. Raw review outputs and execution receipts are private; only independently assessed findings belong in public planning documents.

## Baseline evidence

| Fact | Repository evidence |
| --- | --- |
| Chat addressed append/read exists; automatic turn-context inclusion is missing | `skills/summon/scripts/_conversation.py:append_agent_message`, `skills/summon/scripts/_conversation_runtime.py:_context_prompt` |
| Chat resume allowlist differs from governed capability registry | `skills/summon/scripts/_conversation_runtime.py:_SUPPORTED_RESUME_CLIS`, `skills/summon/scripts/_resume_capabilities.py` |
| Only Claude subprocess is certified by the governed resume registry | `skills/summon/scripts/_resume_capabilities.py` |
| Swarm claims/leases exist; dependency readiness does not | `skills/summon/scripts/_swarm_coordinator.py:_materialize_tasks`, `_claim_common` |
| Swarm journal append shares a hard byte cap with control/terminal writes | `skills/summon/scripts/_swarm_coordinator.py:_append`, `_apply_cancel_request` |
| Prompt-file input still reaches generic CLI backends through argv | `skills/summon/scripts/_builder.py:argv_length_error` |
| Portable results are experimental and cover dispatch/jobs only | `skills/summon/scripts/_portable_result.py` |
| Local usage refresh is consented, narrowly version-bound and advisory | `skills/summon/scripts/_usage_live.py`, `_usage_runner.py` |
| Existing fixed test/gate registry governs releases | `tools/release_manifest.py`, `tools/release_gates.py`, `.github/workflows/ci.yml` |

Current public baseline is 3.4.0. Pending Astra/Flash changes must remain intact. Existing rooms/swarm remain preview; provider-inert tests are not live-provider certification. No app-native session bridge is established merely because the current host exposes a messaging tool.

## Work-item contract and sequencing

Each item must receive an implementation owner, dependencies, exact acceptance evidence and migration impact before work starts. Roles below are recommended ownership functions, not permission grants. Record blocked/unknown evidence explicitly. Keep execution success separate from review verdict, cleanup status and spend certainty.

Dependency spine: P0 contracts → P1 two-worker vertical slice → P2 supervision → P3 dependency swarm/native qualification. P4 discovery/UI design can proceed alongside P1; integrated UI qualification follows the relevant runtime slice. P5 is a per-capability evidence gate throughout, not permission to enable every provider at once.

## P0 — reliability, provenance and capacity

Owner: runtime and trust maintainers. Gate: existing behavior preserved; no new authority or provider claims inferred from fixtures.

- [ ] R01 Establish one capability registry keyed by backend, transport, adapter/version and operation. Distinguish unsupported, candidate, qualified and revoked. Test all callers against the same values.
- [ ] R02 Reconcile chat and governed resume behavior without silently elevating preview paths. Test unsupported AGY and candidate Codex/Cursor paths, profile drift and explicit forks.
- [ ] R03 Preserve authoritative served-model rules for requested/targeted/served identity. Test missing, mismatch, mixed auxiliary-model usage and forged child-written metadata.
- [ ] R04 Normalize terminal outcomes for auth, missing tools, rate limit, empty output, interrupted work and unknown contact. Preserve partial work, backend exit and non-authoritative diagnostics.
- [ ] R05 Re-run existing Kimi EOF/finalization/auth/ACP-fallback regressions; reopen only a newly reproduced defect. No automatic auth repair or second provider attempt.
- [ ] R06 Re-run OpenCode zero-token, missing-finish, retired-route, rate-limit and external-CWD regressions. Keep denied filesystem scope denied.
- [ ] R07 Re-run AGY terminal enum, timeout propagation, missing-executable and headless launch regressions. Do not accept prose as evidence of successful execution.
- [ ] R08 Qualify Windows ACL/helper refusal and nested Job Object behavior on supported hosts. Fail closed on unenforceable permission/ownership; never widen permission as recovery.
- [ ] R09 Preserve immutable background prompt/scripts bundles and read-root serialization. Test concurrent installer/source changes and launch/terminal identity mismatch.
- [ ] R10 Preserve typed terminal envelopes for structural refusals, parser exits and exceptions. Hard-kill cases without a terminal receipt remain stale/indeterminate; never manufacture provider certainty.
- [ ] R11 Make timeout units explicit in all new UI/CLI examples. Preserve legacy millisecond parsing and short polls; test serialization, suffixes, overflow and late extension.
- [ ] R12 Implement a backend-specific prompt-transport capability: stdin/native file/API body/bounded argv. Test Unicode, newlines, quotes, exact-byte binding and OS limits. No silent truncation or unapproved transport fallback.
- [ ] R13 Reserve capacity before accepting work for terminal, cancellation, owner-loss and recovery records. Test full journal, failed fsync, disk full and control delivery without corrupting existing truth.
- [ ] R14 Introduce bounded checksummed journal segments/checkpoints with explicit recovery rules; preserve audit history and deduplication across segments. Test torn tails, corruption and replay limits.
- [ ] R15 Keep explicit Claude/Codex profiles isolated and bound to request/resume identity. Test credential rotation, profile mismatch, background propagation and no automatic account switching.
- [ ] R16 Reconcile README, product plan, public/engineering changelogs and readiness labels. Mark implemented, preview, provider-inert, live-gated and retired consistently.

Signals: typed refusal/terminal categories, identity evidence state, journal occupancy, control-write failures, cleanup verified/clean, unknown contact/spend. Store no message bodies in telemetry.

## P1 — durable task and message fabric

Owner: workspace/protocol maintainer. Gate: one task and two authenticated worker instances exchange bounded context across restart without changing authority.

- [ ] F01 Define versioned Workspace, Task, Attempt, Principal, WorkerInstance, Message, Delivery, Command, Artifact, Review and Decision schemas with strict validation and canonical identifiers.
- [ ] F02 Keep roster seat, worker instance, provider session and human principal distinct. Bind workers to project, task scope, epoch and effective authority.
- [ ] F03 Assign one authoritative writer/store to each entity; build workspace indexes as rebuildable projections over existing journals. No second scheduler or dual authority log.
- [ ] F04 Add durable outbox/inbox transactions or equivalent crash-safe binding. Test interruption before/after append, projection, receipt and acknowledgement.
- [ ] F05 Admit addressed messages only to authorized participants/supervisor within allowed task/project boundaries. Agent prose cannot add recipients or broaden the data boundary.
- [ ] F06 Add stable message IDs, content binding, correlation/reply/causation IDs and per-stream monotonic sequence. Do not claim universal wall-clock order.
- [ ] F07 Define delivery states accepted, queued, included_in_attempt, acknowledged, rejected, expired and cancelled; bind inclusion to exact attempt/context digest.
- [ ] F08 Specify acknowledgement levels: local durable admission, adapter receipt, provider submission. None proves model understanding, compliance or completion.
- [ ] F09 Make delivery retries idempotent; conflicting reuse is rejected. Never treat an ambiguous provider submission as safe to resubmit automatically.
- [ ] F10 Fence stale senders by authenticated worker epoch, not display name. Test revoked principals, replaced workers, stale owners and replayed credentials.
- [ ] F11 Incorporate addressed inbox events into fresh and resumed turns through the context compiler. Persist selection/high-water marks and preserve messages arriving during launch for the next boundary.
- [ ] F12 Default delivery to the next authorized turn/resume boundary. Expose live delivery only through a qualified adapter with an explicit receipt; queued context does not itself authorize spend.
- [ ] F13 Define cancellation/expiry before submission and after inclusion. Retraction cannot erase previously submitted context; cancelling execution is a separate typed action.
- [ ] F14 Prevent human and agent messages from silently altering permissions, accounts, models, spend, routing or policy. Test malicious instructions through all message/context paths.
- [ ] F15 Bound per-message size, inbox depth, per-sender rate, retained context and subscriptions. Apply backpressure with explicit refusal; preserve control capacity.
- [ ] F16 Define task and attempt state machines independently, including partial, blocked, failed, cancelled and indeterminate. No aggregate task success while required attempts/cleanup remain unresolved.
- [ ] F17 Provide provider-inert CLI inspection and task/message API projections. Link legacy IDs rather than rewriting old records.
- [ ] F18 Build a two-worker fake vertical fixture covering send/reply, restart, duplicate delivery, stale sender, cancelled queued message, authority injection and verified context inclusion.

Signals: bounded queue depth/age, delivery-boundary latency, duplicate suppression, stale rejection, context inclusion counts, projection lag and recovery outcomes.

## P2 — long-horizon supervision

Owner: job/runtime maintainer. Gate: honest long-duration state and intervention, with no duplicate spend on recovery.

- [ ] H01 Present first-event, idle, finalization and hard deadlines separately with explicit units and supported maximum runtime.
- [ ] H02 Separate authenticated heartbeat/process liveness from useful objective progress. Repeated stream events, ping-only loops and model assertions cannot justify unlimited extension.
- [ ] H03 Define trusted progress milestones using validated artifacts, completed dependency work and verified checks; label heuristic progress as heuristic.
- [ ] H04 Unify inspect, extend, cancel, steer, pause, resume and fork as typed capability-checked commands with queued/durable/applied/rejected/blocked outcomes.
- [ ] H05 Preserve generation-bound live extension and hard caps; test command races at the deadline, clock jumps and expired owners.
- [ ] H06 Keep steering queued unless an adapter proves live support. Preserve order, expiry and exactly-once context inclusion across a successor.
- [ ] H07 Define pause as stop-admitting-work plus supported safe-boundary completion/checkpoint. Unsupported active pause stays unavailable; do not freeze arbitrary processes or claim provider spend stopped.
- [ ] H08 Preserve bounded partial results separately from authoritative terminal results, including truncation and evidence limitations.
- [ ] H09 Maintain leases, owner epochs, process birth-token checks and whole-process-tree cleanup. Test PID reuse, orphan pipes, host restart and unverifiable takeover.
- [ ] H10 Define resource handoff for workspaces, artifacts, processes and locks; require explicit ownership transfer and report retained resources.
- [ ] H11 Create one idempotent successor only from eligible authenticated state, with unchanged/decreased authority and fresh spend consent. Do not replay uncertain contact.
- [ ] H12 Make fork a new lineage with explicit context selection and no inherited approval. Preserve original attempt and provider identity evidence.
- [ ] H13 Test multi-hour simulated workloads and one separately approved real qualification lane, including stalled streams, parent death, cancellation and recovery.
- [ ] H14 Distinguish shutting the browser, stopping its server, stopping the supervisor and cancelling provider work. Each action shows its actual lifetime effects.

Signals: progress age, stall category, applied control generation, successor idempotency, cancellation latency, resource retention and unknown-spend outcomes.

## P3 — dependency swarms and existing-session interoperability

Owner: coordinator and adapter maintainers. Gate: sixteen fake workers pass conformance; native/live claims require separate evidence.

- [ ] S01 Add explicit dependency edges with cycle/self-edge/missing-node validation and deterministic readiness. Do not describe current claim-only tasks as an existing DAG scheduler.
- [ ] S02 Gate claims on dependency success and artifact acceptance; define blocked/cancelled/partial upstream propagation and fan-in behavior.
- [ ] S03 Bridge manifests into coordinator claims without competing launch ownership. Bind attempts to the same permission, project, roster and spend decisions as ordinary dispatch.
- [ ] S04 Define retries per failure class, total attempt budget and explicit uncertainty handling. Lease expiry alone never authorizes duplicate execution.
- [ ] S05 Enforce global and per-backend/account concurrency plus bounded pending queues and fairness. Start at four active workers/two per boundary; opt-in up to sixteen after qualification.
- [ ] S06 Keep 1,024-task admission and existing payload limits until measured evidence justifies change. Qualify journal rollover, replay, memory and cancellation under saturation.
- [ ] S07 Verify artifact content on consumption, permission and claim generation. Do not dereference arbitrary worker URLs or absolute paths automatically.
- [ ] S08 Run sixteen fake workers with dependencies, competing claims, lease expiry, duplicate completion, cancellation, host restart and stale artifacts. Measure coordinator separately from provider children.
- [ ] S09 Publish a version-negotiated adapter conformance boundary, preserving strict existing swarm/v1 readers. Capabilities include discovery, observation, follow-up, queued/live steering, resume, interrupt, cancellation and cleanup ownership.
- [ ] X01 Distinguish Summon-owned sessions, explicitly attached external sessions and read-only observations. Attaching never silently transfers process ownership or grants mutation authority.
- [ ] X02 Define a minimal SessionDescriptor: provider/host/adapter version, opaque private handle, permitted operations, identity-evidence state and freshness. Keep handles/account bindings out of public exports.
- [ ] X03 Require explicit session selection and scoped attachment consent; preview destination, data boundary, model/account evidence and allowed operations before any message submission.
- [ ] X04 Bind selected session identity against replacement, reuse, app restart, account/model change and stale catalog entries. Wrong-recipient delivery must fail closed.
- [ ] X05 Negotiate busy-session behavior: queue follow-up, reject, or documented live delivery. Never interpret queue acceptance as turn completion or control acknowledgement.
- [ ] X06 Define coexistence with a human/native supervisor: locks, concurrent writes, operator revocation and detach. Detach revokes Summon access without killing an externally owned session.
- [ ] X07 Investigate Claude Code's supported continuation/messaging interfaces using official CLI/API contracts and source where available. Qualify compatible known-session continuation first; do not assume live injection.
- [ ] X08 Investigate Claude Desktop independently. CLI session support does not imply Desktop chat access. If no supported interface exists, provide a manual handoff and label attachment unsupported.
- [ ] X09 Investigate Codex CLI independently from the Codex app. Governed CLI continuation remains candidate until identity/profile and interrupted-turn evidence pass.
- [ ] X10 Investigate whether Codex app task messaging is available to an external Summon process. Host-local callable tools are not a public integration guarantee; prototype only against a documented/authorized interface.
- [ ] X11 Investigate Antigravity using supported interfaces and versioned fixtures. Reconcile unreliable profile continuity before claiming attachment/resume. Do not scrape private session databases.
- [ ] X12 Qualify Cursor, OpenCode and ZCode individually. Maintain a matrix of discovery/read/follow-up/live steering/resume/cancel support rather than a single misleading supported badge.
- [ ] X13 Account for Hermes/Nous and native swarm systems as distinct host, model-provider and credential routes. A credential bridge is not a native-session adapter.
- [ ] X14 Provide copy/export manual handoff when no adapter is available. Require explicit content selection and redacted preview; never silently send via UI automation.
- [ ] X15 Test attach/detach/reconnect against a fake host: spoofed endpoint, unsupported version, lost acknowledgement, duplicate submission, wrong account, session replacement and revoked access.
- [ ] X16 Qualify the first native adapter with a real normal/busy/cancel/interrupted/reconnect matrix and no private-child-state assumptions. Choose it by verified feasibility, not brand preference.

Targets to measure, not promises: 16 workers; bounded replay at 1,024 tasks; provisional coordinator memory budget 256 MiB excluding child processes. Establish actual latency/resource budgets from a reproducible benchmark before publishing them.

## P4 — onboarding, user experience and accessibility

Owner: product/UI and discovery maintainers. Gate: first-run and task-control journeys pass rendered browser and keyboard testing.

- [ ] U01 Redesign the existing atlas into a task-first workspace; reuse loopback auth, cursor reconnect, fixed viewport and feed scroller. Keep agents and decision views discoverable.
- [ ] U02 Make one task detail the focal area: objective, active work, messages, artifacts, evidence, controls and decisions. Avoid three competing chat/council/deliberation landing flows.
- [ ] U03 Keep stable scroll ownership and composer geometry under notices, streams, reconnect, long context and mobile keyboard changes. Use bounded incremental rendering, initially about 200 visible items.
- [ ] U04 Show owner, stream, turn, delivery and model-verification states separately. Use plain boundary-specific labels rather than a generic live/connected badge.
- [ ] U05 Provide readable prompts/context with explicit inclusion/omission and public/private projection labels. Preserve keyboard focus and scroll anchor when new events arrive.
- [ ] U06 Make notifications nonintrusive: reserved placement, unread badges and actionable failures; no focus theft, layout shifts or repetitive unchanged-state alerts.
- [ ] U07 Test keyboard-only operation, accessible names, contrast, reduced motion, focus return, zoom/reflow, desktop/tablet/mobile and screen-reader announcements.
- [ ] U08 Test real rendered geometry, bounded long-journal behavior, reconnect gaps, stale owner, token invalidation and unavailable controls. Source-string assertions alone are insufficient.
- [ ] U09 Unify first-run discovery across installed CLIs/IDEs, profiles and routes. Include OpenCode/ZCode; distinguish discovered executable, auth state, declared subscription, entitlement and served-model evidence.
- [ ] U10 Keep initial discovery local and non-mutating. Provider/account queries require explicit scope/consent; credential presence is not entitlement or usable credit.
- [ ] U11 Give provider-specific auth-repair guidance for Claude, Codex, Kimi, ArkCLI, OpenCode, ZCode and others. Login completion is not proof of model availability; retry remains explicit.
- [ ] U12 Explain timeout units, adaptive checkpoints, server lifetime, background jobs, queued steering, supported resume and local telemetry with one complete walkthrough.
- [ ] U13 Add an existing-session connection journey showing supported operations and delivery evidence, plus an honest manual handoff fallback.
- [ ] U14 Keep normal dispatch usable without a persistent server. Workspace observation must not make a daemon mandatory for every command.

## Usage, context economics and roster

Owner: evidence and provider maintainers. Gate: recommendations remain advisory and audit facts survive optimization.

- [ ] E01 Normalize usage observation/retrieval timestamps, TTL, units, account scope, provider, billing class and provenance. Distinguish unknown, stale, unavailable and zero.
- [ ] E02 Keep allowance, rate limit, API balance and credit separate. Do not turn them into an unsupported common balance or cost score.
- [ ] E03 Require explicit provider/account read consent and reviewed versioned fixtures. No auth repair, model dispatch, routing change or retry as a side effect of usage refresh.
- [ ] E04 Prevent usage advice from switching account/provider/model/billing class or overriding exact pins and explicit spend ceilings.
- [ ] E05 Reuse typed safe context compilation for deduplication, bounded transcripts and tool/artifact elision. Freeze source bindings and reverify external references at consumption.
- [ ] E06 Preserve audit facts, provenance, permission/spend boundaries, immutable evidence and mutation records byte-for-byte or through a provably equivalent retained reference. Model summaries cannot replace authority.
- [ ] E07 Keep per-task off/opt-out controls and explain token estimates versus measured usage. Never silently drop undelivered messages to fit a context budget.
- [ ] E08 Preserve editorial model catalog separately from live observations and runtime capabilities. Show requested, targeted, served and evidence source/freshness distinctly.
- [ ] E09 Preserve Astra exact gpt-6-astra/high with explicit xhigh/max; Fable 5.1 independent escalation; Flash 3.8 advisory personas; Kimi K3, Sol, Terra and Luna as distinct entries. Do not infer catalog changes from host defaults.
- [ ] E10 Label GLM/Z.ai, ArkCLI, ZCode, OpenCode, Hermes/Nous and OpenRouter by host/backend/provider/model/billing route. Qualify each route independently.
- [ ] E11 Add explicit refresh, successor selection, deprecation and retirement UX. Historical Ox/stealth receipts retain original identities and tombstones; no automatic replacement or relabeling.
- [ ] E12 Add roster-drift tests for renamed, removed, alias-remapped and unverified models plus unsupported effort levels. Freeze definitions for in-flight tasks and require explicit rebind/fork on drift.

## Council and deliberation preservation

Owner: governance maintainer. Gate: workspace integration cannot change existing authority semantics.

- [ ] G01 Model Review separately from Decision. Council positions, challenges, synthesis and dissent remain advisory artifacts.
- [ ] G02 Allow human context between explicit council rounds without changing launch budgets or promoting a recommendation automatically.
- [ ] G03 Require explicit fresh deliberation setup fixing option IDs, seats, quorum denominator, rounds, physical attempts, deadline and approval policy.
- [ ] G04 Count only validated receipt-bound ballots; messages, summaries, copied recommendations and model self-identification are not ballots or approvals.
- [ ] G05 Preserve durable-before-contact attempt records, deterministic terminal precedence, replay/recovery and uncertain-spend rules.
- [ ] G06 Keep live-provider activation, writable seats, approval application, resume and alternate transports individually gated. Workspace UI actions must not widen these capabilities.
- [ ] G07 Keep Fable's independent final-review lane distinct from human/release-owner authority. Seven advisory reviewers do not authorize a release by majority vote.
- [ ] G08 Test malicious chat-to-ballot promotion, changed options/quorum, copied receipts, stale generations, disputed model identity and missing quorum.

## P5 — privacy, migration and release qualification

Owner: release and privacy maintainers. Gate: exact committed source, machine-recorded required checks and deliberate owner approval.

- [ ] L01 Preserve all existing CLI names/companion skills and legacy run IDs; introduce workspace commands additively with explicit preview labels.
- [ ] L02 Define versioned migration and downgrade behavior before new writes. Older strict readers must reject unsupported schema safely, never mutate it.
- [ ] L03 Rebuild indexes without rewriting old journals, approvals or receipts; preserve historical identity uncertainty and avoid fabricated worker authentication.
- [ ] L04 Test rollback with active jobs, pending messages, approval generations, usage state and telemetry preferences. Quiesce/transfer ownership explicitly before incompatible runtime replacement.
- [ ] L05 Keep optional local telemetry bounded and disabled by default; preserve operational audit state independently. Explain clear versus disable and retention controls.
- [ ] L06 Scan exact public files, examples, release notes and exports for private paths, names, accounts, tokens, session handles, raw prompts/reports, receipts, telemetry contents and machine-specific fingerprints.
- [ ] L07 Keep receipt/evidence storage outside public source. Treat hashes of low-entropy private values as potentially identifying; public projections must be purpose-limited.
- [ ] L08 Update README, public and engineering changelogs, operator guide, troubleshooting, capability matrix and migration notes to match actual release scope.
- [ ] L09 Extend the fixed CI/release registry for new behavioral tests and rendered UI evidence. Test on supported Windows/POSIX configurations; document legitimate skips.
- [ ] L10 Independently qualify normal/cancel/deadline/interrupt behavior for each claimed provider/adapter operation. Fixtures do not prove current live availability or served model.
- [ ] L11 Review and test pending Astra/Flash changes separately before any deliberate commit; preserve existing user edits and private/custom installation state.
- [ ] L12 Require clean committed source, source-bound test/gate evidence, migration/rollback pass, managed-install convergence, privacy pass and explicit release-owner publication decision. No release claim from this checklist or model approval.

## Owner decisions

- [ ] D01 Approve a narrow next-minor workspace preview rather than a replacement runtime.
- [ ] D02 Confirm local-only scope, four default active workers and opt-in sixteen-worker qualification.
- [ ] D03 Select the first existing-session adapter only after feasibility evidence; Claude Code continuation and Codex app reachability are initial investigations, not guarantees.
- [ ] D04 Approve separate provider/contact/spend envelopes for future live qualification; no automatic account rotation or PAYG fallback.
- [ ] D05 Assign accountable owners and effort estimates after checklist review; split oversized items into independently testable changes.
- [ ] D06 Adopt a release support/deprecation policy and explicit storage/privacy retention rules before workspace GA.

## Review protocol

Seven requested seats: Gemini Flash 3.8 product persona, interoperability persona, reliability persona and UX persona; Kimi completeness/context reviewer; GLM protocol/economics reviewer; Opus architecture/release reviewer. Personas are not independent-model votes. A target without authoritative served-model evidence remains explicitly unverified; findings require source or engineering verification.

Round 1 reviews v1. The lead records accepted/rejected/deferred/needs-evidence findings with stable IDs and produces v2. Round 2 receives v2, the disposition ledger and each seat's prior HANDOFF. Produce v3 and run a third full round if unresolved material checklist issues justify it. Stop at convergence or a documented external blocker; never claim unanimity from missing, failed or unverifiable seats. No automatic retries for unknown contact/auth/quota failures.

Exit criteria for planning: every material accepted finding maps to a work item; exclusions/unknowns are explicit; phase gates are observable; authority and migration invariants survive; unresolved issues have an owner decision or evidence task. Implementation and release gates remain future work.
