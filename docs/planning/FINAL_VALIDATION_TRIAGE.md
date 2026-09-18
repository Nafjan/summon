# Final validation triage

2026-09-12 architect disposition: CONDITIONAL. All seven runtime corrections,
five CI corrections and regression-retention changes are now integrated.
All eighteen selected files and 335 runtime/test/configuration files match the
independently reviewed composition after newline normalization. Retained scoped
evidence is 162 runtime passes, 138 CI passes with one explicit Git-HEAD-dependent
deselection, and 20 registry passes; these are the earlier private executions,
not a new integrated suite invocation. Canonical retention closes R03/R10 again.
Coverage is 58 verified and 5 blocking out of 63 (92%, rounded): U03, U07, L06,
L09 and L12. The earlier reproductions and failed packets remain evidence below.

L09's parameter-ID correction is now integrated. Independent AST comparison
preserves every non-ID test operation and all existing IDs; actual guarded
collection maps 3,298 distinct cases across eleven fixed selections with zero
identity/collection problems, guards or source drift. All 151 selected files
match that collected candidate. No test bodies ran, release outcome was finished,
or platform qualification was claimed by the collection-only check. Complete
canonical runtime capture and native-device observations remain required.

## Evidence that changes acceptance

- VR-06: an actual normal executor-stamping call, with a synthetic registered
  backend, retains `kimi_model_evidence_conflict` but restores the targeted
  identity as inferred when output tokens are present and exact mode is off.
  Exact mode still blocks; zero-output conflict stays absent; ordinary inference
  remains a passing control. This is contradictory provenance, not a bypass of
  the exact-model gate. Reopen R03 until conflict remains withheld through final
  stamping and the producer-to-envelope path has a retained regression.
- VR-05: two guarded driver confirmation cases exercised the real reader thread,
  stream parser, liveness tracker, RuntimeControl, driver loop, enrichment and
  final forced publication using an in-memory process. The unchanged control
  returns its completed result. Injecting an OSError only at final observational
  publication suppresses that return after terminal processing. Both confirmation
  assertions passed in 0.18 seconds; they prove the defect, not its repair.
  Reopen R10. Outer launch propagation is source-backed, not an executed provider
  or physical process-cleanup claim.
- VR-07: the production workspace projection accepts changed message/detail/body
  capabilities while retaining the same snapshot and accepting the old expected
  snapshot. The unchanged control is stable. The browser polls by snapshot ID,
  so it can miss these changes. This remains within already-blocking U03/L09.

The projection and executor-stamping probe completed in 1.916 seconds with no
provider, socket or child execution, guard violation, source change, home data or
bytecode. The separate driver confirmation likewise created no child or socket
and retained unchanged source. Exact bindings and synthetic evidence stay private.

## Finite correction queue

The conductor remains the sole shared integrator. Keep executor changes under
one owner and serialize shared test/CI changes. These are fixes to existing
behavior, not authorization for new providers, native adapters or P2 expansion.

| ID | Classification and concrete acceptance |
| --- | --- |
| VR-01 | Source-confirmed: a late attempt inherits an already-expired soft deadline despite remaining hard budget. Give an authorized attempt a usable observation interval within the immutable job hard end. Verify late retry/repair, hard-expired refusal, cancellation and replayed extensions; do not reset aggregate time authority. |
| VR-02 | Source-confirmed: wait_job never refreshes an OK prepared record with no PID. Verify later PID publication/death is observed promptly, while result/nonce precedence and caller deadline remain correct. |
| VR-03 | Source-confirmed: a 129-character fork reason passes UI validation but fails the journal after child initialization. Validate the complete journal-compatible reason before any mutation. Test 128/129/256/512 boundaries on ordinary/historical/bound paths and unchanged room inventory on refusal. |
| VR-04 | Source-confirmed: claim helper retries under the same message ID generate a different frame and conflict. No second claim is committed. Verify identical semantic retries, including after reopen, return the original result; changed task/worker/lease scope still conflicts. |
| VR-05 | Reproduced final driver failure: observational heartbeat I/O can suppress a completed result; the midstream consequence is source-confirmed. Preserve terminal response/accounting and ownership cleanup, expose heartbeat unavailability separately, and keep durable control authentication/launch failures strict. |
| VR-06 | Reproduced stamping failure: preserve known Kimi identity conflict through final non-exact stamping. Retain exact-mode refusal, ordinary inference and missing-output controls. Never promote child observations into authoritative served evidence. |
| VR-07 | Reproduced projection failure: include returned message/detail capability inputs in snapshot identity. Verify availability, recipient, detail revision/body-scope changes, stale expected-snapshot refusal and an ordinary browser poll without focus/anchor loss. |

Source references: `skills/summon/scripts/_job_control.py`, `_jobs.py`,
`_executor.py`, `_conversation.py`, `_conversation_runtime.py`,
`_conversation_ui.py`, `_swarm_coordinator.py`, `_workspace_view.py` and
`_workspace_page.py`. Existing test seams are recorded in the private triage
report; no source-only counterexample is counted as executed acceptance.

CE-01 through CE-05 in CI_EVIDENCE_ACCEPTANCE.md remain the CI integration
packet. The AGY test's omitted restoration of ambient `AGY_PTY_QUIET` is a
test-local hygiene correction that can share that packet. It does not justify
changing runtime environment policy or another provider review round.

## Independently checked correction packet

The complete private VR-01 through VR-07 composition now independently passes
162 focused cases in one run: 7.165 seconds test runtime and 8.735 seconds outer
runtime, with zero failures, skips, guard violations, source changes,
synthetic-home files or bytecode. Ten Python files differ from shared source:
six corrected runtime modules and four new regression files. This supersedes
the narrower execution totals below for the composed packet, without discarding
their evidence. Shared integration and canonical/CI retention remain required.

The final private retention increment now also passes all 20 release-registry
cases independently in 1.765 seconds (5.171 seconds outer runtime), with no
guards, children, source changes, leaked test aliases, home files or bytecode.
It places the four regressions in the existing related fixed suites and CI,
without adding gates, schemas or rendered artifacts. Apply that incremental
metadata patch after the accepted CE packet. The assembled handoff contains all
runtime corrections, the CI packet and this retention increment; do not replace
the whole shared tree with its private source copy or overwrite unrelated edits.

VR-03 validates final journal-compatible reasons before any child creation.
VR-04 recovers the original claim response within the existing replay window,
including after reopen or renewal, without another claim or lease extension.
Changed task/worker/request/lease inputs, out-of-window requests and physical
owner contention remain refusals. The lead rejected an initial private version
that could acquire ownership and repair a torn journal before rejecting a
malformed digest or unknown task. The accepted v2 restores those pre-owner
checks; actual torn-journal tests preserve all run bytes and owner/control files
on refusal, while a subsequent valid claim still performs normal repair.

VR-07 independently passes 115 projection cases and is included in the 162-case
composition. Its retained desktop browser run took 30.210 seconds (32.464
seconds outer runtime): natural polling detected a host-installed capability
change, retained the displayed view until explicit refresh, and preserved the
active-element ID, serialized reading anchor and scroll position (zero delta).
Task entities and journal bytes stayed unchanged. Separate read-only review
confirmed source, executed-harness and runtime bindings. This does not claim
DOM object identity, screen-reader speech or native mobile behavior.

The private VR-05/VR-06 candidate now passes nine independent cases in 0.423
seconds (0.871 seconds outer runtime), with no failures, skips, guard violations,
source changes, synthetic-home files or bytecode. The lead reviewed its complete
patch and tests before execution. Only `_executor.py`, `_job_control.py` and the
new `test_executor_observation_regressions.py` differ from current shared Python
source. The shared integrator has the bound packet; R03/R10 remain blocking
until source/test retention is verified.

Heartbeat handling catches only OSError from observational file publication;
authentication and durable control remain outside that catch. Midstream and
final write failures preserve the result and accounting, expose only a latched
boolean, and retain the successful control and strict-error cases. Kimi tests
exercise the actual conflict producer using an in-memory stream and synthetic
wire usage records through final executor stamping. Non-exact and zero-output
conflicts remain absent, exact mode refuses, and ordinary inference stays
inferred. These fixtures do not establish native process or live-provider proof.

VR-01/VR-02 now have a separate private correction. A fresh composed copy of
that packet and VR-05/VR-06 independently passed all 24 focused cases in 8.765
seconds (23.547 seconds outer runtime), with zero failures, skips, guard
violations, source changes, synthetic-home files or bytecode. Actual RuntimeControl
clock/checkpoint cases cover late initial/retry/repair attempts, hard-end bounds,
authenticated extension replay and cancellation. Canonical prepared/PID/result
transitions cover late process publication, death/result races, wrong nonce/source
identity and the caller deadline. Time and process-liveness inputs are synthetic;
no provider launch or physical process identity is qualified by these tests.

## Items not promoted to release blockers

- Known unsupported steering currently retains a queued note; execution still
  refuses unsupported resume. Availability wording is a follow-on contract task.
- Ordinary-v2 child lineage differs from historical/bound lineage, but this
  review found no authority failure. Preserve compatibility pending a separately
  specified typed-child-lineage change.
- Chat replacement CLI polish and the canonical descriptor dead branch remain
  follow-ups; current canonical fallback preserves the validated body scope.
- The drawer Escape claim lacks a reachable ordinary-user counterexample.
  Existing single-dialog keyboard evidence is not waived or replaced.
- A single intercepted Windows fallback probe confirms a shell request after
  WMI failure, but installed Python adds hidden-window startup flags itself.
  No child/window was launched or observed. This does not identify the cause of
  field flashes. Process-free platform facts remain bounded future hardening.
- Git-less copy failures, concurrent socket exhaustion and proposed broadening
  of accepted Unicode or diagnostic codes are not accepted product defects.

## Device and release boundary

A private executable device fixture and nine-case observation template now exist.
Its data-only smoke passed in 1.827 seconds for three synthetic tasks, typed
assessment/decision records and one separately scoped body. Actual speech and
native keyboard behavior remain unexecuted.
The encrypted pending-message boundary is now prepared. Its single guarded
runtime smoke passed in 1.668 seconds: a real append became uncertain after
session-constraint loss, and an exact lookup returned the original queued
message without changing the journal. The lead inspected the executed script,
receipt and result; the retained script matches the executed bytes. This does
not exercise HTTP, browser encryption or a native device; disposition recovery
is not a substitute.

The subsequent ordinary-host/HTTP/desktop-browser composition passed in 36.900
seconds (38.785 seconds outer runtime). One real append returned uncertainty
after fixture-induced session expiry. Reload and reauthentication preserved the
same serialized encrypted record; ordinary WebCrypto restored the same body,
key and recipient. One explicit lookup confirmed one queued message, with no
automatic resend or journal change. The lead inspected the corrected runner
before execution and its retained result, exact executed bytes and cleanup
evidence afterward. The earlier unclassified harness failure remains preserved;
the passing retry does not retrospectively establish its cause. No product/CSP
or application-state change was used to force acceptance. These are full
headless desktop Chromium observations, not actual AT/mobile evidence. All nine
native observations remain NOT RUN.

The app retrieval API omitted the latest conductor completions. A bounded
read-only recovery of three actual final messages found only unsupported
completion acknowledgments, without changed files, evidence, an active handle,
blocker or requested decision. This establishes a retrieval discrepancy; it
does not establish an execution failure or shared integration. The bounded
private runtime, CI and retention packets are prepared and independently
checked; the conductor remains the sole shared integrator. No shared
implementation ownership has been transferred and coverage is unchanged.
The subsequent bounded native Astra recovery produced an actual shared VR-07
integration. Both runtime and regression files independently match the reviewed
composition after newline normalization. This qualifies that integration only;
the remaining five reviewed patches are assigned to the same conductor under
a finite follow-on grant. Native device availability remains unknown.

No provider validation lacking authoritative identity is counted as a named-model
approval. Do not repeat failed or unchanged model reviews merely to obtain votes.
Final source/privacy/platform, clean committed candidate, managed installation
and publication gates remain separately required and reserved as applicable.
