# P0 implementation review

Updated: 2026-09-05. DEV-P0-01 has passed independent lead verification against FABLE_P0_APPROVAL.md. Verdict: APPROVE for this bounded test/contract slice, confirmed by final FABLE-WIN-02 milestone review with the Windows correction. This does not complete R01/R02 or establish release readiness.

The final independent run passed all 189 tests in 56.76 seconds, with no exclusions: capability contract 29, continuation 43, job control 60, and job resume 57. Bytecode, pytest cache, external plugin autoload and telemetry were disabled. Fingerprints of all script modules and the contract document remained unchanged throughout the run. The accepted R01 snapshot is unchanged. The only differences from the original tracked-tree snapshot are the accepted R01 files, these two P0 test files and the separately authorized Windows release-tool edits now in progress.

## Findings and disposition

| ID | Evidence | Required closure | State |
| --- | --- | --- | --- |
| P0-RV1 | Initial driver coverage omitted startup and idle expiry | Ordered-queue, injected-clock driver cases now assert both classifications and cleanup; finalization and terminal precedence remain covered. | Closed; independent suite passed |
| P0-RV2 | Normal concurrent case was initially replaced by forced busy | Separate normal case retains real OS locking and reconciles both callers to one successor. Its real immutable bundle is prepared before contention to isolate lock semantics from copy duration; the busy case covers gated preparation and retry. | Closed; independent suite passed |
| P0-RV3 | Report initially lacked separate timing aggregates | Independent measured kill start: 1313 ms; total cleanup: 1937 ms. Both hard assertions passed, including the unchanged six-second cleanup bound. These are single-run observations, not latency guarantees. | Closed |
| P0-RV4 | Initial readiness helper generated invalid Python | Valid multiline child code is compiled before spawn; failed readiness cleans up the child. The helper now uses shared silent-launch flags. | Closed; independent suite passed |
| P0-RV5 | Initial implicit-ID documentation/test was inaccurate | Identical implicit input is idempotent; changed implicit input is source consumed; changed explicit input conflicts. | Closed; source and tests verified |

## Review approach

The lead checked the real error-emitter test and controlled ledger-byte window, invocation-versus-claim scope, forbidden successor fields on the error, one bundle/claim/launch, same-request recovery and dead-winner refusal. Production lock wait and runtime modules remain unchanged. The formerly failing named tests now pass under controlled fixtures; this does not prove every historical field failure has been diagnosed.

For final verification, inspect the complete diff and documentation, run the four affected suites with telemetry and external plugin autoload disabled, bind results to unchanged source during the run, and check pre-existing roster edits and the accepted R01 slice are preserved. Report any failures without exclusions. Passing tests do not complete R01/R02 or establish release readiness.
