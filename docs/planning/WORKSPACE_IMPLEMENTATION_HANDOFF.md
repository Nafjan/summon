# Workspace implementation handoff

Planning scope only. Read the latest checklist identified by this directory's README and the review disposition log before implementation. Unchecked requirements are not shipped features, and advisory review is not release authority.

## Product direction

Build one workspace containing tasks, agents, messages, artifacts, reviews and decisions. Keep the existing dispatcher and provider-neutral local coordinator. Keep council recommendations and fixed-option deliberation as separate typed modes with separate authority. Redesign atlas into a task-first shell; do not replace working runtime kernels wholesale.

Existing-session messaging is an adapter capability, not a universal property of an installed CLI. Distinguish known-session continuation, queued follow-up, live steering, observation and attachment. Investigate Claude Code, Claude Desktop, Codex CLI, Codex app and Antigravity separately. A host-embedded tool is not proof that an external Summon process can invoke it. Unsupported integrations receive explicit manual handoff.

## First implementation sequence

1. Review the pending Astra/Flash source and roster work as its own change. Preserve its existing organization and exact-model requirements. Run the corresponding regressions and deliberate source review before any commit or release claim.
2. Write the versioned capability and delivery contracts, transition fixtures, authority boundaries, private-storage layout and capacity arithmetic. Inventory strict schema consumers and compatibility behavior before changing producers.
3. Reconcile governed continuation with the shared capability registry. Keep historical preview rooms readable; announce demoted routes and offer a non-launching fork path. Do not silently promote candidate adapters.
4. Implement one crash-safe, recipient-scoped message path over the existing journal ownership boundary. Bind selected complete messages to an immutable attempt input. Provide authenticated send, inspection and disposition operations. Establish the two-simulated-worker acceptance slice before real-provider claims.
5. Add the minimal workspace shell and rendered accessibility/reconnect tests. Expose delivery, contact certainty, owner, stream and turn states separately. Keep passive versus supervised operation visible.
6. Qualify any real provider operation separately, then integrate long-horizon supervision, dependency scale and the first feasible native-session adapter as independently gated changes.

Split each step into small reviewable changes with one owner, dependencies, affected schema versions, acceptance evidence, migration impact and rollback behavior. Do not combine roster edits, journal migration, all native adapters and a new UI into one commit.

## Five acceptance journeys

### Agent messaging another agent

A task admits two authenticated worker instances and their scoped principals. One worker addresses a message to the other. Durable admission returns a message ID and queued delivery state, without launching a provider. On the recipient's next authorized turn, whole-message selection freezes inclusion IDs and the compiled input. The sender cannot claim provider receipt or acknowledgement merely because the task later finishes. Restart, duplicate IDs, stale recipients and malicious instructions must preserve this distinction.

### Supervisor steering long work

The operator inspects a task's liveness, objective progress, partial result and deadline. A steer command records bounded context for the next supported boundary; it is labelled queued unless a qualified adapter proves live delivery. Extend, cancel, pause, resume and fork are separate typed commands. An interrupted attempt retains unknown spend and its reservation until authoritative reconciliation. A successor needs its own grant and verified fencing of conflicting predecessor effects.

### More than ten agents

A versioned dependency graph admits sixteen simulated workers under measured capacity limits. Competing claims, upstream failures, retries, cancelled work, stale artifact revisions and restart produce deterministic outcomes. Default practical concurrency starts at four active workers with per-boundary limits; sixteen is opt-in after qualification. Dependency failure can close an execution run honestly without inventing downstream success or clearing uncertain attempts. A v1-only worker cannot receive an unsupported graph claim.

### Council review

The operator chooses advisory reviewers and the review scope. Positions, dissent, cross-examination and synthesis appear under a Review entity linked to the task. Human context can be added between explicit rounds. Four personas using one model are displayed as such. Recommendations cannot change permissions, authorize publication or become ballots.

### Fixed-option deliberation

The operator explicitly creates a Decision with fixed options, seats, quorum and bounded attempts/deadline. Each valid ballot remains tied to its invocation and evidence. Chat messages, imported prose and council findings are context only. Missing model evidence, provider failure, interrupted attempts and missing quorum preserve fail-closed outcomes. Live activation and application of any decision remain separately governed.

## Defer or retire

- Defer a replacement supervisor/runtime, universal desktop attachment, arbitrary private child-session discovery and claims of universal mid-turn steering.
- Defer sixteen-worker live scale and multi-hour live claims until simulated and operation-specific qualification pass.
- Defer universal long-prompt transport claims; support declared bounded argv routes with pre-contact refusal while adding capable transports individually.
- Retire duplicate capability allowlists through versioned migration, confusing competing landing flows through the workspace facade, and outdated roster aliases through explicit tombstones.
- Preserve legacy commands, typed council/deliberation kernels, historical model identities and receipts. Retired Ox/stealth aliases must never relabel old records.

## Phase evidence and operational signals

These are proposed bounded local signals, subject to telemetry opt-in and retention controls. Operational authority journals remain separate. Do not collect message bodies, prompts, private paths, account labels, credentials or raw session handles in telemetry. A metric cannot replace a receipt or authorize a recovery action.

| Phase | Acceptance and release gate | Useful bounded signals |
| --- | --- | --- |
| P0 reliability/provenance | Affected regression suites, model-evidence refusals, terminal envelopes, capacity reserve and immutable launch tests | Refusal categories, missing/mismatched evidence counts, journal occupancy, control-write failures, unknown-contact and cleanup outcomes |
| P1 task/message fabric | Two authenticated simulated workers; crash-before/after-submission fixtures; full-message inclusion/exclusion; forged identity and involuntary fork; explicit preview gate set | Queue depth/age, boundary latency, duplicate suppression, held reasons, context inclusion/defer counts, projection lag |
| P2 supervision | Multi-hour simulated work, deadline/control races, partial preservation, successor grant/fence tests; separate evidence for each real operation | First-event/idle/finalization age, progress/stall class, control application latency, held reservations, retained resources |
| P3 swarm/adapters | Sixteen fake workers, dependency/claim/restart/version faults; measured memory/journal budgets; fake-host conformance before scoped native qualification | Active/pending counts, claim contention, readiness lag, cancellation latency, version refusals, recipient collisions, coordinator resource use excluding children |
| P4 onboarding/UI | Rendered focus/contrast/scroll/reconnect checks, first-task journey, passive-mode and auth-repair guidance; shipped UI must meet its declared gate | Reconnect attempts and terminal reasons, aggregate layout/anchor failures, focus-test failures, first-run completion stages without entered content |
| P5 live/release | Exact committed source, fixed gate-contract consumer consistency, privacy, rollback, managed-install convergence, per-operation live evidence and owner publication decision | Gate pass/fail/blocked/skipped counts, reviewed skip reasons, evidence freshness, capability drift and privacy-scan failures |

Establish latency and memory thresholds with a reproducible benchmark before publishing promises. Performance evidence must state concurrency, workload bounds and whether workers were simulated or real.

## Decisions under delegated leadership

The owner's subsequent direction delegates routine implementation decisions to the lead and Fable; see LEADERSHIP_AUTHORITY.md. The list below tracks decisions to resolve through that process, not renewed per-slice owner permission requests. Separate installation, credential, commit and release restrictions remain in effect.

- Confirm the narrow P0/P1 workspace preview, including its minimal UI, and name accountable runtime, governance, adapter, UI and release owners.
- Approve measured admission targets and retention policy. Four default active workers and opt-in sixteen-worker qualification are proposals, not capacity guarantees.
- Approve preview resume deprecations and communication before governed registry reconciliation changes behavior.
- Choose the first existing-session adapter only after a version-scoped positive or negative feasibility report.
- Define per-operation provider/contact/spend grants for future live qualification; reserve uncertainty conservatively. No automatic account rotation or billing fallback.

## Top risks and unknowns

1. Native interface reachability: documented continuation support may not permit attachment or safe concurrent submission from an external process.
2. External ownership: detach cannot stop another application's work. Missing authoritative quiescence can intentionally block conflicting successor mutation.
3. Contact and model evidence: successful-looking prose, inferred model identity and terminal process exit may still be insufficient for governed claims.
4. Storage and concurrency: bounded journals must retain room for cancellation/recovery, with crash-safe checkpoint membership and explicit ordering under simultaneous commands.
5. Scope and migration: stricter governed resume may demote preview behavior. A facade must not duplicate authority stores or weaken release gates to accelerate UI delivery.

## Required handoff from each implementation change

Report changed checklist IDs, actual source changes, test/gate evidence and its limitations, migrated schema consumers, retained resources, privacy review, unresolved decisions and the next bounded task. Preserve request/route/authority evidence and separate execution status from review verdict. Do not mark an item complete from model advice alone.

Public documentation contains only repository-relative references and sanitized synthetic examples. Private reports, execution receipts, account state, prompts and machine-specific evidence stay outside public source.
