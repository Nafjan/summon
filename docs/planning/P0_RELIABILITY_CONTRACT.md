# P0 runtime-reliability contract

Status: implementation candidate for DEV-P0-01. This document records the
provider-inert contract tested by the two affected suites. It does not grant a
provider, account, workspace, credential, spend, resume, or steering
permission.

## Scope

This change hardens the test contract around:

- startup, generation-idle, EOF finalization, and trusted-terminal liveness;
- real child ownership and bounded cleanup;
- authenticated resume contention, idempotency, and uncertain recovery; and
- truthful structural-refusal output.

Runtime modules, shared lock budgets, launch serialization, immutable bundle
preparation, roster entries, and the R01 capability registry are unchanged.

## Resume contention

The successor launch lock remains a bounded, fail-closed serialization point.
Preparation may hold it while the immutable bundle, launch record, and
claim-bound evidence are written. A competing caller therefore receives the
existing compatible `ControlBusyError` refusal rather than a new launch.

The same explicit request identifier and identical semantic inputs may be
inspected or retried. The retry reconciles the original successor and cannot
create a second provider process. Reusing an explicit request identifier with
different inputs returns `resume_claim_conflict`. A changed input with no
explicit identifier after the source has already been claimed returns
`resume_source_consumed`; an unchanged implicit request derives the same stable
identifier and remains idempotent.

The test contract covers both normal concurrent completion (both callers
reconcile one successor) and slow-preparation contention (one caller completes,
one caller receives the bounded busy refusal). Both cases assert one claim and
one provider launch; the slow case additionally snapshots the ledger while
the winner remains inside the controlled preparation barrier.

The busy refusal is an invocation error, not a claim-scoped attempt receipt.
The real CLI emitter returns exit code 1 and two distinct JSON records can be
observed under contention: the winner's background handle and the loser's
structural error envelope. The error envelope may state `attempts: 0`,
`attempt_status: not_run`, `execution_status: not_run`, and
`provider_contacted: false` because that refused invocation did not launch. It
contains no `job_id`, resume lineage, continuation, or model identity. The
`provider_contacted` field is intentionally scope-local to the refused
invocation; it must not be read as a claim about the concurrent winner. The
winner's claim and contact state are queried separately.

The internal callback receives the exact bounded message
`governed resume refused (ControlBusyError)`. The real `_print_error` emitter
serializes the same refusal with `exit_code: 1`,
`raw_backend_exit_code: 1`, `normalized_exit_code: 1`,
`dispatcher_status: error`, `served_model_evidence: absent`,
`model_match: null`, and `named_model_verified: false`. This is an invocation
error record; it is not a winner-phase or provider-contact receipt.

An uncertain owner, partial preparation, post-spawn ambiguity, or dead winner
is never converted into a fresh launch. Recovery remains explicitly blocked
until authenticated state proves that a safe transition is possible.

An additive `pending` projection is intentionally deferred. A top-level
pending response cannot safely assert zero attempts or no provider contact
while another caller may have crossed reservation or process creation.

## Liveness and process ownership

The deterministic fixtures use an injected monotonic clock and an ordered
reader queue at both the tracker and `_drive_process_loop` seams. They assert
the stages independently:

1. no trusted event before the first-event deadline is `startup_timeout`;
2. no meaningful progress after a trusted event is `generation_idle_timeout`;
3. EOF starts its own bounded `finalization_timeout` grace; and
4. a trusted terminal event remains authoritative after later deadline checks.

The real-process fixtures publish an out-of-band readiness marker before the
driver starts its first-event budget. A release marker then controls whether
the child emits a trusted event or remains silent. This keeps interpreter
startup out of the semantic startup deadline while retaining a separate real
silent-startup/reaping test.

Kill-start timing and total cleanup timing are separate hard assertions and are
recorded as private aggregate `kill_start_ms` and `total_cleanup_ms` test
properties. The six-second cleanup ceiling remains unchanged. A slow operating-system teardown
must not be relabelled as a liveness-stage failure, and a cleanup test must not
be excluded to produce a green release claim.

## Privacy and evidence

Fixtures use synthetic IDs, prompts, paths, and receipt-shaped values only.
Public documentation reports fields and outcomes, never local paths,
credentials, prompts, provider receipts, session handles, or account data.
Telemetry is disabled for the provider-inert test commands.

## Acceptance

The affected provider-inert suites must run with bytecode/cache generation and
external pytest plugin autoload disabled. Results must report deterministic
fixtures and real-process fixtures separately, retain the historical
contention/finalization test names, and disclose any failure or baseline
comparison. A passing test gate does not certify a provider or make R01,
R02, or a release complete.

## Deferred work

Runtime admission, authenticated external executable/version observations,
expiry enforcement, v2 continuation migration, live steering, automatic
message injection, usage-aware routing, and an operation-scoped pending schema
require separate reviewed changes.
