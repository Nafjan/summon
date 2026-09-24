# R01 bounded contract acceptance

Reviewed: 2026-09-05. Verdict: CONDITIONAL. The authorized pure contract slice is accepted after independent source and fixture review. The broader regression gate is not green; R01 as a whole and all release gates remain open.

## Implemented slice

- `skills/summon/scripts/_resume_capabilities.py`: additive exact v2 declarations, strict validation, deterministic serialization and registry-scope consistency checks. The existing v1 catalog remains the sole editable capability catalog.
- `skills/summon/scripts/test_resume_capabilities.py`: 29 provider-inert tests covering both versions, malformed and forged rows, consistency, deterministic bytes and registry purity.
- `docs/SUMMON_RESUME_CAPABILITIES.md`: compatibility, evidence and future admission boundaries.

No runtime consumer, routing rule, resume allowlist or launch permission changed. Source-only v2 qualification is explicitly unqualified. Executable binding, external CLI version, provider receipts and expiry observations are unavailable, not fabricated. Matching registry metadata proves consistency only. Every row retains `launch_permission: not_granted`.

The review rejected an initial draft that accepted boolean/float generation equivalents and integer evidence booleans, raised on malformed membership inputs, and omitted semantic fields from its digest. The corrected implementation rejects those inputs and covers all semantic row fields. These are corrected draft defects, not claims about the released product.

## Independent evidence

| Check | Result and limit |
| --- | --- |
| Original v1 comparison | All 13 canonical rows and tested unknown/wrong-direction cases match the original snapshot. This does not assert v1 byte serialization stability. |
| Malformed v2 checks | 211 independently constructed cases rejected without unexpected exceptions or acceptance. |
| Digest reconstruction | Independently reconstructed all 26 canonical operation rows; canonical manifest digest matches. No private digest values are published. |
| Full affected regression run | `test_resume_capabilities.py`, `test_job_continuation.py`, `test_job_control.py`, `test_job_resume.py`: **180 passed, 2 failed**, with no exclusions. Relevant source files stayed unchanged throughout. |
| Baseline failure comparison | Original snapshot targeted run: **1 passed, 1 failed**; reproduced concurrent initial resume refusal with `ControlBusyError`. |
| Current targeted rerun | The same two failing tests: **2 passed**. A rerun does not erase the full-run failures. |
| Source preservation | Only the authorized capability module and its test differ from original tracked-file fingerprints. Pre-existing tracked roster changes and four pending new roster files remain preserved. |
| Whitespace check | `git diff --check` passed. |

Tests used bytecode/cache suppression, disabled external pytest plugin autoload and disabled Summon telemetry. They did not contact providers. No installation, commit or release occurred.

## Outstanding reliability evidence

1. `skills/summon/scripts/test_job_resume.py::test_concurrent_initial_resume_returns_one_successor_without_record_error` failed with `ControlBusyError` on both the full current run and an original-snapshot targeted run. Classify as a reproduced baseline intermittent failure. Root cause and the distinction between lock timing and a correctness defect remain unresolved.
2. `skills/summon/scripts/test_job_control.py::test_eof_finalization_timeout_still_reaps_child` returned `startup_timeout` instead of `finalization_timeout` in the full run. Original and current targeted runs passed. Classify as intermittent and unresolved, not proven fixed or conclusively attributed to the baseline.
3. The developer separately reported a Windows cleanup timing exclusion in its own control run. That exclusion is not used to declare the independent suite green. Investigate timing under controlled load before changing thresholds or excluding tests.

These failures do not show a v2 consumer regression: existing consumers still use unchanged v1 behavior. They remain explicit reliability/release evidence gaps and must not disappear from the P0 backlog.

## Next handoff

Preserve this patch as a reviewable, uncommitted slice. Do not mark the whole R01 checklist item complete. Authenticated observation, finite expiry validation, admission-time revalidation, continuation binding and compatibility migration remain unimplemented. Runtime activation belongs to a separately authorized R02 change.

Before proposing activation, provide a concrete R02 design covering all three chat allowlist decision sites, compatible authenticated Claude continuation, explicit no-contact refusal and non-launching fork for affected preview lanes, and ownership/budget/exact-model evidence. Keep ordinary dispatch unchanged by resume policy. Carry the two independent regression failures into that handoff and obtain a clean, reproducible applicable gate before any release claim.

R13 reserved control capacity and F13/F17/F18 remain P1 prerequisites. Dependency scheduling and native attachment remain P3. No provider qualification, live steering, message delivery, installation, commit or release is authorized by this acceptance record.
