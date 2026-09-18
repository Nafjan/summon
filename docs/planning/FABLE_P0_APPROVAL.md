# Fable P0 implementation approval

Date: 2026-09-05. Verdict: APPROVE, subject to the closure conditions below. This approves implementation of the corrected reliability proposal; it is not acceptance of an untested patch or a release approval.

The owner delegated implementation details to the project lead and designated Fable for technical approval. Two bounded read-only reviews completed successfully. Both carried authoritative reported served-model evidence for `claude-fable-5-1`; the lead checked that evidence before counting the approvals. Raw reports and receipts remain private. No automatic retry, model substitution or account rotation occurred. An initial unavailable-profile preflight made no provider call; the reviews used the seat's default configured route.

The second review was necessary: the lead found that the first review's F-01 condition conflated the test callback with the actual CLI error emitter. Fable inspected the real boundary, withdrew that wording and approved the corrected condition. Model approval remains subject to source verification.

## Approved scope

`docs/planning/P0_RELIABILITY_NEXT_CHANGE.md`, including its corrected lead disposition, with changes confined to:

- `skills/summon/scripts/test_job_control.py`
- `skills/summon/scripts/test_job_resume.py`
- One dedicated runtime-reliability contract document
- Planning and evidence records

Keep runtime modules, public response schema, lock timeout, launch serialization, immutable bundle, R01 contract, pending roster edits and release documents unchanged. If a test exposes a necessary runtime correction, return the concrete finding to the lead and Fable. No routine owner permission request is needed. Installation, credentials, commit and release remain separate boundaries.

## Binding closure conditions

These review labels use a hyphen and are distinct from workspace checklist IDs F01 through F18.

| Condition | Required closure |
| --- | --- |
| F-01: actual error boundary | Preserve internal callback text `governed resume refused (ControlBusyError)` and return code 1. Use the real dispatcher `_print_error` in a contention boundary test, with foreground globals restored and telemetry disabled. Combined capture has the winner's JSON handle and the refused invocation's JSON error. Error fields include status error, exit code 1, zero attempts, not-run attempt/execution states, false provider contact, null served/resolved model and absent model evidence, with normalized exit fields. It has no job_id, resume, lineage or continuation. Documentation must explicitly distinguish invocation scope from the winner's claim scope. |
| F-02: both conflict branches | Explicit request ID plus changed inputs refuses as `resume_claim_conflict`; omitted ID plus changed inputs refuses as `resume_source_consumed`. Retry guidance preserves the identical command and same explicit request ID. |
| F-03: deterministic deadlines | Inject a clock and ordered queue for startup, generation idle, EOF finalization and trusted-terminal precedence. Account for direct monotonic calls after EOF. Report a concrete finding if runtime clock changes prove necessary. |
| F-04: real process readiness | Use out-of-band readiness and a release barrier so finalization testing does not accidentally test interpreter startup. Preserve production budgets. A separate silent real subprocess must expire at startup and be reaped. A persistent readiness-controlled failure is a runtime finding, not justification to weaken the fixture. |
| F-05: separate cleanup evidence | Separate kill-start semantics from total cleanup bounds into hard-failing tests. Keep the current numeric cleanup ceiling. Report the two millisecond measurements separately. |
| F-06: real locking | At least one contention test exercises the OS lock file. A fixture may shorten the wait constant; assert the production constant remains 5.0 seconds. |
| F-07: no losing-caller effects | Hold the winner at a signalled preparation barrier. Compare ledger bytes after the signal and after the loser's refusal, before releasing the winner. This excludes legitimate winner transitions from the no-mutation window. Verify one claim, one bundle, one mocked launch before and after retry, and the same successor. A dead-winner retry returns blocked/indeterminate without another launch. Unexpected idempotent reservation rewrites are findings, not grounds to weaken assertions. |
| F-08: privacy | Synthetic fixture IDs and captured synthetic output are permitted. Public documentation excludes real paths, IDs, handles, prompts, receipt bodies and account details. |
| F-09: truthful regression report | Run all four affected capability/continuation/control/resume suites with telemetry and external pytest plugin autoload disabled. Report commands, flags, counts, deterministic/real-process results and both original failures' observed current outcomes. No exclusions or inferred green status. A passing run is regression evidence, not a release gate by itself. |

The implementer must map each condition to a named test or documentation section. The lead independently reviews the actual diff and reruns applicable gates before seeking milestone acceptance.

## Scope ambiguity carried to P1

`provider_contacted` currently has invocation scope in a generic error envelope and claim scope in a resume report. A refused invocation's zero-attempt fields do not establish the winner's phase or contact state. The error has no successor handle or resume projection; a new pending projection attaching those fields to a successor would assert different facts and remains deferred.

Documentation makes this overlap explicit; it does not resolve the schema ambiguity. Carry an explicit operation/attempt scope design into the P1 F01/F03 contract work and the R02 planning checkpoint. Future scope markers require reviewed runtime changes.

## Evidence limits and handoff

Fable inspected source but ran no tests. The earlier timing probes and 180-pass/2-failure result were reported evidence, not reviewer-executed tests. The lead independently confirmed the real emitter's JSON/error fields without provider contact. Single-successor probes used a mocked provider process and do not establish live process retention or arbitrary crash safety.

DEV-P0-01 implements this scope. LEAD-P0-01 verifies it and preserves the original failure evidence. After verification, prepare the R02 design packet covering all three chat allowlist sites, authenticated Claude compatibility, typed refusal/non-launching fork, and operation-versus-attempt state. Runtime activation remains subject to Fable review within the owner's delegated direction.

The second review reported a private harness decision note as its only leftover artifact; no product files or live processes were reported changed. Keep that note private.
