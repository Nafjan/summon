# Summon workspace implementation checklist — v2

Status: revised planning baseline following round 1 (six usable reports; Kimi incomplete). No implementation or release approval. See WORKSPACE_REVIEW_LOG.md for independent dispositions.

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

- [ ] R01 Establish one operation-specific capability registry, with explicit producer/consumer migration to a versioned v2 schema. Preserve the exact resume-capabilities/v1 validator and legacy projections until callers migrate. Key by backend/transport/adapter/version/operation; discovery is candidate evidence, not qualification. Test v1/v2 rejection, unsupported operations and revocation at submission.
- [ ] R02 Make the registry authoritative for governed launches; remove the duplicate chat backend allowlist. Existing preview histories stay readable. Unsupported/candidate resume refuses with a typed reason and an explicit non-launching fork/rebind option; it does not auto-launch or certify Codex/Cursor/OpenCode/ZCode/AGY. Version/migration tests cover existing rooms and preserve compatible certified Claude continuation.
- [ ] R03 Preserve authoritative served-model rules for requested/targeted/served identity. Test missing, mismatch, mixed auxiliary-model usage and forged child-written metadata.
- [ ] R04 Normalize terminal outcomes for auth, missing tools, rate limit, empty output, interrupted work and unknown contact. Preserve partial work, backend exit and non-authoritative diagnostics.
- [ ] R05 Re-run existing Kimi EOF/finalization/auth/ACP-fallback regressions; reopen only a newly reproduced defect. No automatic auth repair or second provider attempt.
- [ ] R06 Re-run OpenCode zero-token, missing-finish, retired-route, rate-limit and external-CWD regressions. Keep denied filesystem scope denied.
- [ ] R07 Re-run AGY terminal enum, timeout propagation, missing-executable and headless launch regressions. Do not accept prose as evidence of successful execution.
- [ ] R08 Qualify Windows ACL/helper refusal and nested Job Object behavior on supported hosts. Fail closed on unenforceable permission/ownership; never widen permission as recovery.
- [ ] R09 Preserve immutable background prompt/scripts bundles and read-root serialization. Test concurrent installer/source changes and launch/terminal identity mismatch.
- [ ] R10 Preserve typed terminal envelopes for structural refusals, parser exits and exceptions. Hard-kill cases without a terminal receipt remain stale/indeterminate; never manufacture provider certainty.
- [ ] R11 Make timeout units explicit in all new UI/CLI examples. Preserve legacy millisecond parsing and short polls; test serialization, suffixes, overflow and late extension.
- [ ] R12 Publish fresh/resume operation-specific transport and payload budgets: stdin/native file/API body/bounded argv. Validate the complete serialized invocation in UTF-16 units or encoded bytes, including overhead. Whole required messages must fit or remain queued/refused before contact; never auto-truncate or silently change transport. Test Unicode, newlines, quotes, BOM policy, exact bytes, boundary sizes and a 64 KiB payload on a capable transport.
- [ ] R13 Derive a numeric admission reserve from maximum active attempts, bounded terminal/cancel/owner-loss/recovery record sizes and permitted control counts. Ordinary append admission must preserve that reserve; reserve it before accepting work. Test journal soft/hard cap, concurrent cancellation, terminal close, fsync error and physical disk full. If persistence fails, report non-durable/unknown outcome, stop new launches and use only verified owned-process cleanup; do not claim a cancellation record exists.
- [ ] R14 Extend existing checksummed generation journals with size-triggered checkpoint/rollover and explicit hot/cold/total-retention budgets; segmentation alone does not reduce the aggregate cap. Preserve deduplication and audit facts. Corrupt non-tail authority data blocks execution; verified-prefix inspection is advisory. Test rollover, compaction, torn tails, corrupt middle records and bounded replay without silently skipping evidence.
- [ ] R15 Keep explicit Claude/Codex profiles isolated and bound to request/resume identity. Test credential rotation, profile mismatch, background propagation and no automatic account switching.
- [ ] R16 Reconcile README, product plan, public/engineering changelogs and readiness labels. Mark implemented, preview, provider-inert, live-gated and retired consistently.

Signals: typed refusal/terminal categories, identity evidence state, journal occupancy, control-write failures, cleanup verified/clean, unknown contact/spend. Store no message bodies in telemetry.

## P1 — durable task and message fabric

Owner: workspace/protocol maintainer. Gate: one task and two locally authenticated simulated worker instances exchange bounded context across restart without changing authority. Live capability claims require separate L10 qualification.

- [ ] F01 Define versioned Workspace, Task, Attempt, Principal, WorkerInstance, Message, Delivery, Command, Artifact, Review and Decision schemas with strict validation and canonical identifiers.
- [ ] F02 Keep roster seat, worker instance, provider session and human principal distinct. Bind workers to project, task scope, epoch and effective authority.
- [ ] F03 Assign one authoritative writer/store to each entity; build workspace indexes as rebuildable projections over existing journals. No second scheduler or dual authority log.
- [ ] F04 Add durable outbox/inbox transactions or equivalent crash-safe binding. Test interruption before/after append, projection, receipt and acknowledgement.
- [ ] F05 Admit addressed messages only to authorized participants/supervisor within allowed task/project boundaries. Agent prose cannot add recipients or broaden the data boundary.
- [ ] F06 Add stable message/content IDs, correlation/reply/causation IDs, per-stream sequence, sender epoch and recipient binding. Addressing is instance-bound by default; logical-task continuation requires explicit grant and auditable rebinding. No universal wall-clock order or silent successor retargeting.
- [ ] F07 Define a versioned delivery transition table with accepted, queued, included_in_attempt, submission_started, submitted, acknowledged, not_submitted, held_for_recovery, rejected, expired, cancelled and dead_lettered. Separate logical messages from per-attempt deliveries and adapter acknowledgement level. Exercise every legal/illegal transition; the normative table below governs failure and re-entry.
- [ ] F08 Record acknowledgement level separately: durable admission, adapter receipt and provider-submission evidence, each bound to delivery/attempt/content. A terminal attempt cannot fabricate an acknowledgement; no level proves model understanding, compliance or objective completion.
- [ ] F09 Make local delivery operations idempotent and reject conflicting ID reuse. Only affirmative no-contact evidence permits safe re-arming within remaining authority. Post-contact or unknown submission remains held_for_recovery; any new provider attempt needs its own grant, physical fence and linked delivery record.
- [ ] F10 Validate sender and recipient epochs plus upstream grant/revocation lineage at admission and submission. Replaced recipients hold instance-bound messages for explicit disposition; never silently drop or retarget them. Bound grant depth and forbid recursive delegation/cross-workspace messaging in the first preview. Test stale owners, revoked principals, replaced workers and replayed grants.
- [ ] F11 Make fresh and resumed turn-context selection recipient-scoped. Fit complete messages before freezing selected IDs, compiled digest and launch record; assert actual child input matches. Use escaped typed context as untrusted payload, not policy. Track explicit delivered/deferred sets so no high-water mark skips omitted messages; later arrivals remain queued. Proven pre-contact failure can re-arm; ambiguous contact cannot.
- [ ] F12 Default delivery to the next authorized turn/resume boundary. Expose live delivery only through a qualified adapter with an explicit receipt; queued context does not itself authorize spend.
- [ ] F13 Define queued cancellation/expiry and explicit dead-letter disposition without erasing submission/spend uncertainty. On any fork, retain parent attempt bindings; hold pending deliveries or explicitly transfer eligible unsubmitted context under new delivery IDs and correlation. Retraction cannot erase submitted context. No forced expiry merely to make migration/fork look terminal.
- [ ] F14 Prevent human and agent messages from silently altering permissions, accounts, models, spend, routing or policy. Test malicious instructions through all message/context paths.
- [ ] F15 Coordinate bounded message bytes/count/rate, context capacity, execution slots and provider-contact budgets through the existing owner/admission boundary; do not equate an inbox message with a billable execution slot. Reserve control capacity. Whole-message selection preserves order; oversize head messages get explicit hold/refusal or authorized artifact handling, not silent starvation or truncation.
- [ ] F16 Define task and attempt state machines independently, including partial, blocked, failed, cancelled and indeterminate. No aggregate task success while required attempts/cleanup remain unresolved.
- [ ] F17 Provide provider-inert CLI inspection and task/message API projections. Link legacy IDs rather than rewriting old records.
- [ ] F18 Build a two-locally-authenticated simulated-worker vertical fixture covering recipient inclusion/exclusion, resumed new messages, restart, duplicate delivery, sender/recipient drift, capacity, cancelled queues, authority injection and crashes around submission. This qualifies protocol behavior only; real provider messaging requires L10 evidence.

Signals: bounded queue depth/age, delivery-boundary latency, duplicate suppression, stale rejection, context inclusion counts, projection lag and recovery outcomes.

## P2 — long-horizon supervision

Owner: job/runtime maintainer. Gate: honest long-duration state and intervention, with no duplicate spend on recovery.

- [ ] H01 Present first-event, idle, finalization and hard deadlines separately with explicit units and supported maximum runtime.
- [ ] H02 Separate authenticated heartbeat/process liveness from useful objective progress. Repeated stream events, ping-only loops and model assertions cannot justify unlimited extension.
- [ ] H03 Support task-specific optional milestone watchdog policy, distinct from stream idle, using validated artifacts/checks or other declared milestones. Heuristic progress stays labelled. A milestone timeout follows an explicitly configured hold/cancel policy; research tasks are not required to create files or tests. Hard budgets always apply.
- [ ] H04 Unify inspect, extend, cancel, steer, pause, resume and fork as typed capability-checked commands with queued/durable/applied/rejected/blocked outcomes.
- [ ] H05 Preserve generation-bound live extension and hard caps; test command races at the deadline, clock jumps and expired owners.
- [ ] H06 Keep steering queued unless an adapter proves live support. Preserve order, expiry and exactly-once context inclusion across a successor.
- [ ] H07 Define pause as stop-admitting-work plus supported safe-boundary completion/checkpoint. Unsupported active pause stays unavailable; do not freeze arbitrary processes or claim provider spend stopped.
- [ ] H08 Preserve bounded partial results separately from authoritative terminal results, including truncation and evidence limitations.
- [ ] H09 Classify resources as verified Summon-owned children versus externally owned sessions. Birth-token/process-group/Job Object teardown applies only to owned children. External timeout/cancel invokes a qualified scoped host operation or holds/detaches with uncertainty; never OS-kill the external app. Require physical predecessor fencing before successor mutation; worktree separation alone does not stop spend or shared side effects.
- [ ] H10 Define resource handoff for workspaces, artifacts, processes and locks; require explicit ownership transfer and report retained resources.
- [ ] H11 Create one idempotent successor only from eligible authenticated state and a newly recorded explicit grant bound to its request, route, permission and contact/spend ceilings. No age-threshold waiver or ambient account/PAYG fallback. Remaining task budgets do not reset; expansion requires a separate typed owner decision. Unknown prior contact/cleanup blocks automatic retry.
- [ ] H12 Fork creates a new lineage with explicit context selection, no inherited approval and a complete parent-delivery disposition. Identity-driven runtime forks follow the same non-launching policy: no provider contact until an explicit new grant, no lost pending inbox, no duplication of parent submitted context.
- [ ] H13 Test multi-hour simulated workloads and one separately approved real qualification lane, including stalled streams, parent death, cancellation and recovery.
- [ ] H14 Distinguish shutting the browser, stopping its server, stopping the supervisor and cancelling provider work. Each action shows its actual lifetime effects.

Signals: progress age, stall category, applied control generation, successor idempotency, cancellation latency, resource retention and unknown-spend outcomes.

## P3 — dependency swarms and existing-session interoperability

Owner: coordinator and adapter maintainers. Gate: sixteen fake workers pass conformance; native/live claims require separate evidence.

- [ ] S01 Define graph storage/wire v2 before adding depends_on. Preserve strict swarm/v1 readers. Validate cycles, self/missing edges and bound total edges, fan-in/out and readiness traversal; initial proposal is at most 16 prerequisites per task, subject to measurement.
- [ ] S02 Gate claims on dependency success and accepted artifact revision. Separate reversible dependency_pending/blocked_by_dependency from terminal task refusal. Reconcile readiness deterministically from immutable graph and upstream evidence after explicit upstream retry; never reopen already completed downstream effects silently.
- [ ] S03 Bridge manifests into coordinator claims without competing launch ownership. Bind attempts to the same permission, project, roster and spend decisions as ordinary dispatch.
- [ ] S04 Define failure-class retry and total physical-attempt/contact budgets. Lease expiry is uncertain, not retry authority. Require predecessor physical fencing and unchanged/reduced effect scope before a successor; parallel isolated work still needs explicit spend/side-effect authority. Test upstream recovery and downstream readiness after restart.
- [ ] S05 Enforce global and per-backend/account concurrency plus bounded pending queues and fairness. Start at four active workers/two per boundary; opt-in up to sixteen after qualification.
- [ ] S06 Treat 1,024 tasks and sixteen workers as separately measured admission targets, not proof that every task/attempt/message combination fits 8 MiB. Publish per-record/worst-case capacity arithmetic and distinguish hot, archived and total limits; reject admission when reserves cannot fit. R13 and R14 capacity policy precede scale qualification.
- [ ] S07 Verify artifact content on consumption, permission and claim generation. Do not dereference arbitrary worker URLs or absolute paths automatically.
- [ ] S08 Run sixteen fake workers with dependencies, competing claims, lease expiry, duplicate completion, cancellation, host restart and stale artifacts. Measure coordinator separately from provider children.
- [ ] S09 Version negotiation covers graph storage/wire and adapter capabilities; v1-only graph claims refuse unless a separately qualified coordinator-owned translation proves readiness and all fences. Declare external-process/local-IPC/host-embedded reachability, operation-specific transport, busy/admission protocol and cleanup ownership. Do not guess a private bridge.
- [ ] X01 Distinguish Summon-owned sessions, explicitly attached external sessions and read-only observations. Attaching never silently transfers process ownership or grants mutation authority.
- [ ] X02 SessionDescriptor binds opaque private handle and generation, provider/host/adapter version, workspace/worktree scope, principal/account/model evidence, granted operations, reachability boundary, expiry and per-operation payload transport. Public projections omit handles, private root/account fingerprints and raw paths.
- [ ] X03 Require explicit session selection and scoped attachment consent; preview destination, data boundary, model/account evidence and allowed operations before any message submission.
- [ ] X04 Bind selected session identity against replacement, reuse, app restart, account/model change and stale catalog entries. Wrong-recipient delivery must fail closed.
- [ ] X05 Define busy_state unknown/idle/busy and atomic host turn-generation/CAS admission or equivalent. A stale read of idle is not permission to submit. Native human activity or recipient drift produces collision_hold; absent safe host admission, automatic submission is unsupported. Queued follow-ups have bounded TTL and an explicit on-demand or owned-supervisor driver.
- [ ] X06 Bind scoped attachment grant and native human precedence. Concurrent native activity holds stale queued work. Detach revokes Summon access and polling without killing external processes or claiming their cancellation; scoped cancel requires a supported authorized adapter receipt.
- [ ] X07 Investigate Claude Code's supported continuation/messaging interfaces using official CLI/API contracts and source where available. Qualify compatible known-session continuation first; do not assume live injection.
- [ ] X08 Investigate Claude Desktop independently. CLI session support does not imply Desktop chat access. If no supported interface exists, provide a manual handoff and label attachment unsupported.
- [ ] X09 Investigate Codex CLI independently from the Codex app. Governed CLI continuation remains candidate until identity/profile and interrupted-turn evidence pass.
- [ ] X10 Investigate external-process reachability of Codex app task messaging through a documented/authorized interface. Host-embedded callable tools alone are insufficient. Record qualified/unsupported/inconclusive outcome, exact capability/version scope and requalification triggers; provide manual handoff when external reachability is absent.
- [ ] X11 Investigate Antigravity using supported interfaces and versioned fixtures. Reconcile unreliable profile continuity before claiming attachment/resume. Do not scrape private session databases.
- [ ] X12 Derive the per-operation discovery/read/follow-up/live-steering/resume/cancel matrix from R01, never a parallel registry. Version drift expires qualification; capability withdrawal stops new submissions. Security revocation invokes a scoped owned/external in-flight response, never an unconditional continue rule.
- [ ] X13 Account for Hermes/Nous and native swarm systems as distinct host, model-provider and credential routes. A credential bridge is not a native-session adapter.
- [ ] X14 Provide copy/export manual handoff when no adapter is available. Require explicit content selection and redacted preview; never silently send via UI automation.
- [ ] X15 Test fake-host attach/detach/reconnect: spoofed endpoint, unsupported/host-only reachability, lost acknowledgement, duplicate submission, wrong account/project/worktree, session replacement, native-turn collision and grant revocation. Assert no OS termination targets an external host.
- [ ] X16 Qualify the first feasible native adapter with normal/busy/cancel/interrupted/reconnect evidence. Every investigation X07-X16 records positive, negative or inconclusive outcome, evidence limits, version scope, expiry and requalification trigger. Desktop research is not a P0/P1 preview release dependency.

Targets to measure, not promises: 16 workers; bounded replay at 1,024 tasks; provisional coordinator memory budget 256 MiB excluding child processes. Establish actual latency/resource budgets from a reproducible benchmark before publishing them.

## P4 — onboarding, user experience and accessibility

Owner: product/UI and discovery maintainers. Gate: first-run and task-control journeys pass rendered browser and keyboard testing.

- [ ] U01 Redesign atlas as one task-first navigation shell over scoped projections of existing kernels. Choose routes for task timeline/review/decision views with independent typed authorization; do not merge authority tokens, use raw cross-origin iframe tokens or expose session capabilities in history/links.
- [ ] U02 Make one task detail the focal area: objective, active work, messages, artifacts, evidence, controls and decisions. Avoid three competing chat/council/deliberation landing flows.
- [ ] U03 Keep stable scroll ownership and composer geometry under notices, streams, reconnect, long context and mobile keyboard changes. Use bounded incremental rendering, initially about 200 visible items.
- [ ] U04 Specify per-message boundary badges, reason and timestamps for queued-next-turn, included, submitted, held, rejected and cancelled states. Keep owner/stream/turn/model evidence separate. Never infer acknowledgement or exact per-message token cost from generic completion.
- [ ] U05 Provide a bounded per-attempt context inspector listing included/deferred/elided message/artifact IDs and reasons, category estimates, source bindings and limits. Mark estimates/unknown ceilings. Public views remain redacted; private detail/artifact access requires explicit scope, not a raw unreviewed link.
- [ ] U06 Make notifications nonintrusive: reserved placement, unread badges and actionable failures; no focus theft, layout shifts or repetitive unchanged-state alerts.
- [ ] U07 Use measurable WCAG 2.2 AA-oriented acceptance: keyboard/focus containment and return, accessible names, measured text/non-text contrast, zoom/reflow, reduced motion and assistive-technology announcements. Coalesce bursts and test real desktop/mobile keyboard geometry without prescribing an unmeasured virtualizer trick.
- [ ] U08 Rendered tests cover long-journal anchor preservation, modal focus, owner loss and honest stale/indeterminate recovery. Implement bounded jittered backoff plus terminal authentication-expiry guidance; stop/reduce background retries when disconnected and coalesce announcements. UI timeout alone cannot declare worker death or clear uncertain spend.
- [ ] U09 Unify local discovery including OpenCode/ZCode and an empty-workspace first-task walkthrough. Separate executable discovery, unverified auth, operator-declared subscription, authorized account observations and served-model evidence. Do not infer authenticated/usable state from credential presence.
- [ ] U10 Keep initial discovery local and non-mutating. Provider/account queries require explicit scope/consent; credential presence is not entitlement or usable credit.
- [ ] U11 Give provider-specific auth-repair guidance for Claude, Codex, Kimi, ArkCLI, OpenCode, ZCode and others. Login completion is not proof of model availability; retry remains explicit.
- [ ] U12 Explain timeout units, adaptive checkpoints, server lifetime, background jobs, queued steering, supported resume and local telemetry with one complete walkthrough.
- [ ] U13 Add an existing-session connection journey showing supported operations and delivery evidence, plus an honest manual handoff fallback.
- [ ] U14 Keep on-demand CLI mode daemonless: messages remain durable and delivery/readiness are evaluated on the next explicitly authorized invocation. An explicitly started foreground/background supervisor may drive approved queued work; record its owner/lifetime and display passive versus supervised mode. No hidden daemon startup or automatic lease retry.

## Usage, context economics and roster

Owner: evidence and provider maintainers. Gate: recommendations remain advisory and audit facts survive optimization.

- [ ] E01 Normalize usage observation/retrieval timestamps, TTL, units, account scope, provider, billing class and provenance. Distinguish unknown, stale, unavailable and zero.
- [ ] E02 Keep allowance, rate limit, API balance and credit separate. Do not turn them into an unsupported common balance or cost score.
- [ ] E03 Require explicit provider/account read consent and reviewed versioned fixtures. No auth repair, model dispatch, routing change or retry as a side effect of usage refresh.
- [ ] E04 Prevent usage advice from switching account/provider/model/billing class or overriding exact pins and explicit spend ceilings.
- [ ] E05 Reuse typed safe context compilation for deduplication, bounded transcripts and tool/artifact elision. Freeze source bindings and reverify external references at consumption.
- [ ] E06 Preserve audit facts, provenance, permission/spend boundaries, immutable evidence and mutation records byte-for-byte or through a provably equivalent retained reference. Model summaries cannot replace authority.
- [ ] E07 Keep per-task off/opt-out controls and token estimates distinct from measured usage. Logical-message deduplication is not free provider context: record estimates per actual submission, reported usage/caching separately and unknown contact as unknown. Freeze retained audit facts; never silently omit pending required messages.
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
- [ ] L02 Inventory and version capability, graph/wire, workspace, delivery, checkpoint and evidence schemas before new producers ship. Use a separate workspace namespace; exact legacy validators remain intact. Preserve/quarantine pending states with original IDs/digests and uncertainty, never guess terminal states. Unsupported readers refuse before repair/mutation.
- [ ] L03 Rebuild indexes without rewriting old journals, approvals or receipts; preserve historical identity uncertainty and avoid fabricated worker authentication.
- [ ] L04 Test rollback with active jobs, pending messages, approval generations, usage state and telemetry preferences. Quiesce/transfer ownership explicitly before incompatible runtime replacement.
- [ ] L05 Keep optional local telemetry bounded and disabled by default; preserve operational audit state independently. Explain clear versus disable and retention controls.
- [ ] L06 Scan exact public files, examples, release notes and exports for private paths, names, accounts, tokens, session handles, raw prompts/reports, receipts, telemetry contents and machine-specific fingerprints.
- [ ] L07 Keep receipt/evidence storage outside public source. Treat hashes of low-entropy private values as potentially identifying; public projections must be purpose-limited.
- [ ] L08 Update README, public and engineering changelogs, operator guide, troubleshooting, capability matrix and migration notes to match actual release scope.
- [ ] L09 Extend the fixed gate registry with behavioral and rendered evidence, retaining existing HTTP/security and source checks under accurately described scope. Record browser/engine/toolchain versions. Require machine-readable skip reasons and a reviewed allowlist; a required rendered gate cannot pass because all cases skipped. Version any gate-contract change explicitly.
- [ ] L10 Independently qualify normal/cancel/deadline/interrupt behavior for each claimed provider/adapter operation. Fixtures do not prove current live availability or served model.
- [ ] L11 Review and test pending Astra/Flash changes separately before any deliberate commit; preserve existing user edits and private/custom installation state.
- [ ] L12 Require clean committed source, source-bound test/gate evidence, migration/rollback pass, managed-install convergence, privacy pass and explicit release-owner publication decision. No release claim from this checklist or model approval.

## Owner decisions

- [ ] D01 Approve the P0/P1 protocol-preview cut below, without mandatory P2/P3 live/native work or a replacement runtime. Native/desktop feasibility remains separately scheduled research.
- [ ] D02 Confirm local-only scope, four default active workers and opt-in sixteen-worker qualification.
- [ ] D03 Select the first existing-session adapter only after feasibility evidence; Claude Code continuation and Codex app reachability are initial investigations, not guarantees.
- [ ] D04 Approve separate provider/contact/spend envelopes for future live qualification; no automatic account rotation or PAYG fallback.
- [ ] D05 Assign accountable owners and effort estimates after checklist review; split oversized items into independently testable changes.
- [ ] D06 Adopt a release support/deprecation policy and explicit storage/privacy retention rules before workspace GA.


## Normative release cut and execution modes

The next-minor candidate is a **protocol/workspace preview**, not native-session or long-horizon GA. Its minimum vertical slice is R01-R04, R09-R13, R15-R16, F01-F18, a minimal U01/U02/U04/U08/U14 shell and applicable G/L compatibility/privacy gates. Existing-regression items R05-R08 must pass for any affected shipping route. R14's capacity design must be explicit before the preview admits work; full size-triggered archival implementation and S-scale qualification may follow under strict existing bounds. U07 accessibility applies to the shipped UI, not just a later redesign. All implementation owners/estimates are assigned before development; role owners above are responsibility functions.

P2 supervision, dependency graph execution, sixteen-worker scale and native/desktop adapters are not implied by the preview. Any optional real-provider demonstration needs its own L10/D04 grant and evidence; otherwise all public examples say simulated. Desktop feasibility cannot block the P0/P1 cut. Keep ordinary CLI invocation on-demand; supervised background delivery needs an explicitly started owner process and a recorded automation grant.

| Slice | Blocking predecessors | Evidence at completion |
| --- | --- | --- |
| Capability reconciliation | R01 schema migration, R02 legacy-room policy | Exact old/new validator fixtures and no silent candidate promotion |
| Message vertical slice | R03/R09/R10/R12/R13, F01-F17 | F18 fake process-boundary/crash fixture; no live claim |
| Supervisor integration | F18, H01-H12 contracts, R13 reserve | H13/H14 owned/external failure and lifetime tests |
| Dependency scale | S01/S02 versioning, S03/S04 ownership, R13/R14 budgets | S08 mixed-version sixteen-worker synthetic benchmark |
| Existing-session adapter | R01, X01-X06 grants/ownership, operation transport | X15 fake conformance then separately authorized X16/L10 live evidence |
| Release | Applicable completed slice plus G/L gates | Committed-source-bound manifest, privacy/migration/skip inventory, owner approval |

## Normative delivery recovery table

Logical Message IDs remain immutable. Each inclusion/submission is a separate Delivery bound to an Attempt. Acknowledgement level and evidence are fields, not inference from task success. A scalar cursor is insufficient when messages are held/deferred: persist explicit sets or a cursor plus authenticated holes.

| Transition | Required evidence and consequence |
| --- | --- |
| accepted → queued | Durable admitted message and recipient/grant binding; no spend authorized by this transition |
| queued → included_in_attempt | Whole-message fit and immutable selected IDs/context digest/attempt binding recorded before launch |
| included_in_attempt → submission_started | Current grant, recipient/owner fence, capacity and physical-attempt reservation; durable launch intent before external side effect |
| submission_started → submitted | Authoritative adapter evidence for its documented submission boundary; absence remains unknown |
| submitted → acknowledged | A separately observed supported acknowledgement bound to this delivery; no model-understanding claim |
| included_in_attempt/submission_started → not_submitted | Affirmative proof no provider contact and no side effects; retain immutable attempt binding |
| not_submitted → separately linked queued delivery | Explicit disposition within remaining authority creates a new delivery record for the same logical message; no in-place rewind or automatic provider launch |
| included/submission_started/submitted → held_for_recovery | Contact/recipient/native-turn/cleanup uncertainty or incompatible fork; retain original binding and refuse automatic resubmission |
| queued → rejected/expired/cancelled | Typed validated refusal, TTL expiry or authorized cancellation before submission; preserve history |
| held_for_recovery → dead_lettered | Explicit authenticated disposition; terminal delivery bookkeeping does not resolve unknown spend or change the attempt |
| held/dead-lettered → new linked delivery | Explicit disposition plus a fresh attempt grant and physical fence; prior delivery history stays immutable |
| any terminal delivery → implicit requeue | Forbidden; no terminal rewrite or acknowledgement manufactured from attempt completion |

Forks retain submitted parent deliveries and hold unsubmitted instance-bound ones until explicit retain/transfer/cancel disposition. A user-authorized logical-task addressing policy may transfer eligible queued context with a new recipient binding; it cannot replay submitted effects. Revocation stops new submission at the next boundary; already external effects remain uncertain until a supported cancellation/status receipt resolves them.

## Review protocol

Seven requested seats: Gemini Flash 3.8 product persona, interoperability persona, reliability persona and UX persona; Kimi completeness/context reviewer; GLM protocol/economics reviewer; Opus architecture/release reviewer. Personas are not independent-model votes. A target without authoritative served-model evidence remains explicitly unverified; findings require source or engineering verification.

Round 1 reviews v1. The lead records accepted/rejected/deferred/needs-evidence findings with stable IDs and produces v2. Round 2 receives v2, the disposition ledger and each seat's prior HANDOFF. Produce v3 and run a third full round if unresolved material checklist issues justify it. Stop at convergence or a documented external blocker; never claim unanimity from missing, failed or unverifiable seats. No automatic retries for unknown contact/auth/quota failures.

Exit criteria for planning: every material accepted finding maps to a work item; exclusions/unknowns are explicit; phase gates are observable; authority and migration invariants survive; unresolved issues have an owner decision or evidence task. Implementation and release gates remain future work.
