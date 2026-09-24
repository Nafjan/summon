# Telemetry findings and the 3.5.0 finish line

Architecture decision, 2026-09-12. This is a bounded addition to the existing
workspace-preview release plan, not a new analytics product or release claim.
Private diagnostic records and local usage statistics are deliberately absent.

Current source checkpoint: both runtime corrections are integrated and the lead
independently passed 34 synthetic producer/audit/gate tests, including timeout
aliases, in a fresh owned copy. The test child could not launch processes or
open sockets; its synthetic home remained empty, with no shared-source drift
or bytecode. The lead also verified the new producer test in the canonical
`telemetry_privacy` command and ordinary CI list. This bounded correction and
retention packet is accepted; final candidate evidence remains required.

## Include in the candidate

1. Preserve valid diagnostics from a fresh executor result whose served-model
   evidence is inferred or absent. The internal producer may attest that absence;
   it cannot turn it into reported evidence or exact-model verification. Caller
   fields, serialized results and unknown evidence values remain untrusted.
   Source: `_executor.trusted_telemetry_model_evidence`, `run_subagent._emit`,
   `_telemetry` and `_telemetry_audit` in `skills/summon/scripts`.
2. Normalize the executor's finite timeout reasons into the existing diagnostic
   stage vocabulary. Generation inactivity belongs to `stream`; startup,
   overall/adaptive deadlines and provider-process finalization belong to
   `backend_execution`. Unsupported values remain `unknown`. Runtime deadlines,
   cancellation, partial-result handling and retry authority do not change.
   Source: `_liveness._expire_at`, `_executor` and `_telemetry._timeout_stage`.
3. Explain execution success separately from report-contract acceptance. Useful
   output with a failed report contract must not be presented as accepted work.
   Preserve the existing separate fields and uncertainty rather than rewriting
   historical outcomes or treating every missing report as provider failure.
   Source: `_executor` report finalization and `_telemetry` report projection.

## Acceptance and integration

- Reproduce the trusted inferred/absent producer defect through the actual
  emission path; retain negative controls for serialized/caller evidence,
  malformed evidence, reported mismatch and unsupported production claims.
- Verify finite timeout aliases through projection and validation, including
  unknown values. Do not add raw error strings or a new unrestricted field.
- Keep model proof, audit binding, optional collection and privacy behavior
  unchanged. Synthetic tests must not read or modify real diagnostic storage.
- The conductor integrates additive patches, retains the focused regressions
  in the release command and reconciles public telemetry/changelog wording.
- These corrections do not close the remaining UI, migration, roster, privacy
  or final source-bound release gates. Use `RELEASE35_COVERAGE_MATRIX.md` for
  acceptance status; test counts alone are not release progress.

## Interpretation limits and follow-up scope

Bounded rolling diagnostics are not lifetime usage. Test cohorts, preflight
refusals, provider-contact attempts and audit-accepted records have different
denominators. Do not silently reclassify historical test records as production
or rank providers from a selected sample of available model evidence.

Repeated prompt fingerprints can represent intentional council questions,
iterations or retries. They do not establish wasted work. Missing usage metrics
are unknown, not zero. Terminal-only records do not establish current liveness.

Defer new analytics dashboards and broad instrumentation. The next diagnostic
design should provide privacy-bounded parent/task/round correlation and explicit
retry/review triggers, then combine those with existing scoped usage evidence.
That would help distinguish progress toward a goal from repeated review loops.
Any new collection requires a versioned contract, provenance, retention limits
and opt-out behavior; it cannot change provider, account, model, policy or spend.

Finish-line order remains: integrate the small diagnostic corrections, close
the current task-detail/session-expiry/recovery defects, retain the outstanding
acceptance evidence, then run the final candidate gates. Additional features
and more advisory review rounds are not prerequisites for this release.
