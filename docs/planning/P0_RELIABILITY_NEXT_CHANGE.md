# P0 reliability: next-change proposal

Status: source-backed proposal and DEV-P0-01 implementation record, 2026-09-05. The owner delegated implementation details and directed Fable approval; see LEADERSHIP_AUTHORITY.md. Fable approved the bounded provider-inert fixture/contract slice. Runtime behavior and the accepted R01 contract remain unchanged. No full-suite green or release claim is made.

DEV-P0-01 candidate result: the four affected provider-inert suites now pass
189/189 with no exclusions under bytecode/cache suppression, external pytest
plugin autoload disabled and `SUMMON_TELEMETRY=0`. The earlier 180-pass/two-
failure run and its original-snapshot comparison remain retained as baseline
evidence; they are not erased by this successor run. Lead/Fable review and the
separate Windows launcher-silence issue remain open.

## Resume contention finding

The concurrent initial-resume failure has a reproducible mechanism under slow preparation, not merely an unexplained intermittent observation:

- `skills/summon/scripts/_job_control.py`: `_exclusive_control_lock` allows five seconds to acquire a lock, then raises `ControlBusyError`.
- `skills/summon/scripts/_job_resume.py`: `successor_launch_lock` uses that same lock; `reserve_request` separately serializes source-ledger and control access.
- `skills/summon/scripts/_background.py`: `run_jobs_query` holds the successor launch lock across `spawn_background`, which copies and hashes the immutable script bundle, writes preparation evidence and commits launch state. The immutable bundle is provenance protection and must be preserved.
- `skills/summon/scripts/test_job_resume.py`: `test_concurrent_initial_resume_returns_one_successor_without_record_error` expects both concurrent callers to return success. It does not bound preparation below the lock wait or specify the slow-preparation contention outcome.

An in-memory measurement of the unchanged test passed: winner launch-lock hold 1,883 ms; competing wait 1,859 ms. A second experiment inserted a 5,200 ms delay before bundle copying, without changing repository files or contacting a provider. The competing caller raised `ControlBusyError` after 5,029 ms; the winner held the launch lock for 7,421 ms. The existing test failed as expected under that injected condition. These measurements establish the mechanism and timing sensitivity; they do not prove the exact cause of every earlier natural failure, duplicate launch, lost authority or provider contact. The failed test stops before its later single-launch assertions, so those assertions cannot be claimed as passing evidence from this experiment.

The original-snapshot failure remains independent evidence that this issue predates the additive R01 contract. A bounded busy refusal is safer than allowing a competing launch. The open product question is how the caller learns that preparation is still in progress and safely continues the same request.

A subsequent in-memory provider-inert experiment checked the invariants that the original failing assertion had not reached. All seven checks passed: one initial success and one busy refusal; same-request retry succeeds; exactly one mocked process launch before and after retry; different-input reuse refuses; one ledger claim; both successful responses identify the same successor; and its claim remains spawned. Real filesystem preparation and OS locking were exercised, with a 5,200 ms injected preparation delay and a mocked provider process launch. This verifies that recovery path for the constructed scenario, not arbitrary crash recovery or live-provider safety.

## Finalization finding

`skills/summon/scripts/test_job_control.py::test_eof_finalization_timeout_still_reaps_child` starts a real subprocess, gives it a 1,000 ms first-event budget and a 2,000 ms overall budget, then expects EOF finalization to time out after 75 ms. It does not synchronize on a trusted startup event before starting the driver.

`skills/summon/scripts/_executor.py::_drive_process_loop` checks liveness expiry before reading the next queue entry. `skills/summon/scripts/_liveness.py::_expire_at` reports startup expiry when no trusted event has been observed within the startup budget. Thus the fixture can reach a different valid timeout stage before its intended EOF assertion. This is a source-supported explanation, not proof that host scheduling caused the observed failure. Actual reader/parser defects remain possible until isolated evidence rules them out.

The neighboring EOF-grace test also combines a semantic kill-start assertion with a six-second total cleanup ceiling. Preserve bounded cleanup testing, but distinguish deadline semantics from operating-system process-tree teardown latency. Do not remove the ceiling or label an excluded test as passing.

## Proposed bounded changes

1. Add deterministic driver fixtures with an injected clock and ordered trusted startup/EOF events. Assert startup, generation-idle and finalization stages independently; retain trusted-terminal precedence and child-reaping assertions. Existing fake-clock/queue patterns in `test_job_control.py` provide an implementation starting point.
2. Preserve a real subprocess integration test that establishes its intended startup precondition explicitly. Keep a separate real startup-timeout test and an independently bounded process-cleanup check. Fixture readiness must not silently disable production deadlines.
3. Add event-controlled resume contention tests for normal completion, preparation exceeding the lock budget, and retry after the winner finishes. Verify the same request resolves to the same authenticated successor, exactly one launch occurs, and different-input reuse of the request ID still refuses. Exercise real OS locking as well as deterministic orchestration.
4. Specify the public slow-contention result before editing runtime behavior. Recommended semantics: bounded refusal or an explicitly pending result bound to the existing request, with a clear safe retry/inspect instruction. Preserve old error compatibility if adding a typed projection. A busy caller must not claim that the winning attempt has made no provider contact; attempt scope and uncertainty remain explicit.
5. Add owner-death, partial-preparation and post-spawn uncertainty cases. A busy response cannot authorize retrying a launch, reallocating the source handle, changing permission, dropping provenance, or substituting a provider. Revalidate the authenticated claim before any later action.

Do not solve the observed failure by increasing the shared lock timeout alone, removing launch serialization, moving authority-sensitive writes out of the protected transition without a reviewed replacement, stripping the immutable bundle, excluding the failing test, or weakening its single-successor assertions. A redesigned critical section would require a separate crash-recovery design and broader review.

## Acceptance and signals

| Gate | Evidence required |
| --- | --- |
| Deadline classification | Deterministic startup/idle/finalization boundary tests, including queued-event and trusted-terminal precedence, pass without host-speed assumptions. |
| Real process ownership | Real subprocess tests prove child reaping and bounded cleanup; phase timing and total cleanup timing are reported separately. |
| Resume idempotency | Normal and slow contention, same-request retry, conflicting input and interrupted preparation preserve one authenticated successor and at most one launch. |
| Fail-closed recovery | Owner loss and uncertain launch/spend cannot silently return to a launchable state. |
| Regression | Complete applicable capability, continuation, control and resume suites pass without hidden exclusions. Report deterministic and real-process results separately. |
| Scope and privacy | Pending roster changes and R01 patch remain preserved; no provider call, installation, commit or release. |

For future diagnostics, use bounded aggregate lock-wait, lock-hold, preparation, reader-readiness, EOF-to-reap and cleanup durations plus outcome enums. Do not emit lock paths, request identifiers, session handles, prompt contents, credentials or receipt bodies. This proposal does not enable or read production telemetry.

## Owner decision and handoff

Recommended next implementation scope is the deterministic regression fixtures and explicit contention/retry documentation below, before R02 activation. Test changes outside the R01 contract require new authorization. The present authorization covers the pure capability contract only.

The developer returned a read-only plan. Its fixture and ownership-test direction is accepted. Its proposed additive pending projection is rejected for this slice: unqualified `attempts: 0`, `execution_status: not_run` and `provider_contacted: false` do not describe a winning caller that may still be running or may already have crossed the launch boundary. A later caveat about requiring evidence does not repair unscoped fields. The lock refusal alone proves neither the winner's phase nor absence of provider contact.

Lead disposition: preserve the current `ControlBusyError` compatibility and bounded wait. Document explicit inspection and retry of the same request with unchanged inputs; authenticate/reconcile the existing successor instead of starting another launch. Do not automatically retry. Defer an opt-in pending schema to the P1 operation-versus-attempt contract, where observation freshness, attempt scope and uncertainty can be explicit. This is a sequencing choice within the full workspace roadmap, not removal of its durable operation-state requirements.

Concrete proposed edit boundary:

- `skills/summon/scripts/test_job_control.py`: deterministic stage-boundary cases, readiness-controlled real subprocess cases and separate bounded cleanup evidence. Preserve startup expiry and trusted-terminal precedence coverage.
- `skills/summon/scripts/test_job_resume.py`: deterministic contention cases plus real OS-lock coverage, slow preparation, same-request retry and conflicting-input refusal. Retain one-launch/one-successor checks and existing preparation-fault, owner-loss and uncertain-launch coverage.
- A dedicated runtime-reliability contract document describing the existing busy outcome, its limited evidence and manual same-request recovery, plus planning evidence updates.

No runtime modules, public response schema, shared timeout, launch lock, immutable bundle, R01 contract, roster files or general release documents should change in this proposed slice. If tests expose a production defect requiring such a change, report the concrete evidence and a separate bounded runtime proposal; do not silently widen this authorization request.

Do not promise both simultaneous callers always succeed within a fixed lock budget. Carry the independent 180-pass/2-failure result, original-snapshot comparison and injected slow-preparation evidence forward; none is a new release gate pass. The next owner decision is whether to authorize this test-and-contract-documentation slice. Provider calls, installation, commit, release and R02 activation remain outside it.
