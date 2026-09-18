# 3.5.0 implementation handoff

Updated 2026-09-14. This is an execution handoff for the narrow workspace preview, not a replacement for WORKSPACE_CHECKLIST_V3.md or release approval. Released baseline remains 3.4.0; 3.5.0 is provisional. General implementation and bounded technical review are authorized. Installation, credentials, commits and publication remain separately restricted.

## Current strict-QA handoff, 2026-09-18

The finish-line evidence packet is complete against the frozen current source.
One canonical quiet-host capture (`tools/release_gates.py` with explicit
Chromium; evidence retained outside the repository) completed all 27 fixed
suites: 5,488 passed case outcomes, zero failures/errors, exactly the three
reviewed policy skips, all eight gates at pass except the intentional
`live_provider_evidence_missing` block, Windows platform qualification, and
the complete 44-file rendered collection. The first capture attempt that day
was correctly discarded: a mid-run source edit violated the capture freeze and
the structured-outcome binding refused it; that failure is retained as
positive evidence that the binding control works. One escaped guarded child
from the aborted attempt could not be terminated (access denied) and is
retained as cleanup-uncertain evidence; it caused no contention failures.

Changelog reconciliation is integrated: the Unreleased section now also
carries the taskless supervisor inbox, the council between-round human-context
checkpoint and the Windows silent-launch handling, matching
EXPECTED_NEXT_RELEASE.md; the changelog-binding discovery tests pass under the
new text. The L06 pre-staging privacy scan found no real private paths,
credentials, receipts, telemetry contents or machine fingerprints in the
candidate tree, planning documents included; the final staged-candidate scan
remains reserved.

Both release manifest profiles refuse this evidence with
`evidence was not produced from a clean Git tree` — the exact reserved
boundary. Remaining blockers are unchanged: U03/U07 native device observations
(operator-supplied screen reader and mobile keyboard, fixtures prepared per
U03_U07_DEVICE_ACCEPTANCE.md) and the reserved L06 final scan / L09
final-candidate retention / L12 clean-commit plus publication chain. No
commit, install, version bump or publication occurred.

Closure advisory review, 2026-09-18: one bounded provider-seat review of a
sanitized packet copy (read-only clamped, no candidate access) returned
SUPPORT with no blocking findings: the changelog additions match their feature
sources, the workspace-preview manifest boundary is correctly enforced, and no
document overclaims readiness. Its single follow-up — explicitly verify the
escaped guarded child from the discarded first capture is gone before the
final clean-commit capture — is now resolved: the process exited on its own
and its absence was verified the same day. Advisory verdicts are context, not
release approval. The Kimi K3 and AGY Flash-persona seats were unavailable
(their CLI backends are not installed on this host) and were recorded as an
external unblock rather than repaired.

## Release execution checkpoint, 2026-09-18

The owner authorized deliberate release execution after the closure review.
The canonical version advanced to 3.5.0 across all four contract sources and
the unique migration marker; the changelog entry is dated 3.5.0 - 2026-09-18;
`tests/test_release_contract.py` moved its live-version pin with the release
(its fixtures exercise the marker mechanics, and the historical 3.4.0 pins in
`tests/test_l04_composed_rollback.py` remain the rollback baseline). The
committed candidate then requires a fresh clean-source canonical capture and
the `workspace-preview` manifest check with `--expected-version 3.5.0`. Push,
PR, tag, and publication remain separately reserved; U03/U07 device evidence
remains outstanding release evidence independent of every machine profile.

## Prior strict-QA handoff, 2026-09-14

The corrected aggregate, run with an explicit Chromium executable, completed
all 27 fixed suites with 4,920 passed cases, 3 reviewed-policy skips and zero
failures/errors. Windows qualification and every fixed gate except the
intentional missing-live-evidence block are green. Treat this as diagnostic
evidence only: the worktree is dirty, the source still declares 3.4.0, and
both stable and workspace-preview manifest checks require a clean source-bound
candidate. U03/U07 device acceptance and L06/L09/L12 final-candidate actions
remain reserved follow-up work. The current-source focused release/profile
suite passes 151/151 with external pytest plugin autoload disabled, and the
RB-01 through RB-03 rebuild/recovery suite passes 19/19.
The current phase-0 silent-launch partition passes 27/27, and the roster/model
identity checks pass 15/15 plus 58 unittest cases.
Nested chat/dispatcher/conversation runtime launch checks pass 43/43 with
external pytest plugin autoload disabled.
The full current `workspace_ui` registry passes 330/330 with explicit
Chromium; this closes the host-sensitive rendered/UI recheck only, not native
assistive-technology or mobile-keyboard acceptance.
The current browser-security and accessibility gates pass 204/204 and
159/159 respectively; telemetry/privacy passes 46/46. These provider-free
reruns do not close the clean-candidate or device-only requirements.

## Current execution order, 2026-09-12

**Historical wording note (2026-09-13):** any earlier 37-verified/26-blocking,
59%, or stale Ubuntu all-nonlive instructions below are historical checkpoints.
The authoritative current disposition is 58 verified and 5 blocking in
`RELEASE35_COVERAGE_MATRIX.md`.

The designated conductor has integrated VR-01 through VR-07, CE-01 through CE-05
and all four new runtime regression files with fixed/CI retention. All eighteen
selected files and 335 runtime/test/configuration files independently match the
accepted composition after newline normalization. Retained private executions
are 162 runtime passes, 138 CI passes with one explicit Git-HEAD deselection and
20 registry passes; no new full candidate invocation is implied. Corrected
VR-03/VR-04 v2 is retained; the first private version remains rejected.
Evidence and limits are in FINAL_VALIDATION_TRIAGE.md and CI_EVIDENCE_ACCEPTANCE.md.

L09's source-literal parameter-ID correction is integrated: all 151 selected
files match the independently collected candidate, with all 3,298 distinct cases
mapped across eleven fixed selections and no identity problems. Non-ID logic,
existing IDs and collector/policy are unchanged; zero test bodies ran in this
identity check. Next capture the complete integrated candidate with per-case outcomes,
rendered artifacts, actual toolchain and explicit skip/refusal accounting. Do not
replace required facilities or reserved clean-commit evidence with a green
scoped run. Preserve the integrated fixes, current planning and roster changes.

The disposition table in RELEASE35_COVERAGE_MATRIX.md controls acceptance:
58 requirements verified, 5 blocking (92%, rounded). R03/R10 were reopened by
independent reproductions and are now closed by tested correction and verified
canonical retention. U03/U07, L06/L09/L12 remain blocking. U08 is accepted after exact
rendered-test/fixed/CI retention. L02/E12 are accepted after
final metadata/test and fixed/CI retention. U01/U02/U04 are accepted
after independent rendered evidence and exact runtime/test/fixed/CI retention;
see U01_U02_U04_RENDERED_ACCEPTANCE.md. R16/L08 are accepted after
exact seven-document retention and source/example checks. F17 and L07 are accepted.
Telemetry producer/timeout corrections and test retention are complete. Earlier
instructions below naming these items as unfinished are superseded.

1. Preserve accepted L02/E12: the final 185-row inventory and all nine fixed/CI
   metadata files are integrated. The combined metadata command passed 212
   cases; the restored reader assertion passed its one affected case. Exact E12
   test bytes and equivalent metadata/test semantics are independently retained.
   Do not repeat accepted packet discovery or unchanged full partitions.
2. Complete U03/U07. Both accepted rendered packets are now integrated and
   retained; U08 is closed at preview scope. U07's page matches accepted text
   after newline normalization, and both tests match exact accepted bytes.
   Actual browser zoom and AT/mobile keyboard evidence remain distinct from
   headless/emulated geometry. Primary-source and installed-code inspection found
   native page zoom in full Chromium's settings. The corrected independent
   persistent-profile case passes at 200%/400% in 13.762 seconds, retaining
   fixed native bounds, full-size screenshots and raw CSS control/viewport
   rectangles with the original tolerance. The failed packet is preserved.
   The exact test and fixed/CI evidence wiring described in
   U07_NATIVE_ZOOM_ACCEPTANCE.md are now integrated and independently retained.
   AT/mobile evidence remains unverified;
   native zoom cannot replace those requirements or justify personal-profile use.
3. Preserve accepted public documentation and capability labels, freeze the
   candidate, then collect the final privacy/rendered/platform/release evidence
   (L06/L09/L12). Report reserved commit/install/publication actions separately.

2026-09-13 L09 capture correction: the rendered delivery producer now maps
snake_case protocol states to the manifest's canonical hyphenated state-card
filenames, and the release-registry test asserts the complete required set.
The lead ran the current `workspace_ui` partition at 327/327 and the
accessibility gate's rendered collection matched all 44 required files. The
accessibility gate remains blocked only for unverified assistive-technology and
native mobile-keyboard evidence; this correction does not promote U03/U07 or
L09/L12 and does not qualify a dirty-tree candidate.

2026-09-13 release-runner partition correction: the 24-case
`test_workspace_demo.py` conductor journey now has its own required
`workspace_demo` outcome. `workspace_core` retains the other workspace files,
so the fixed registry preserves every test while avoiding the prior 900-second
combined child timeout. A fresh full capture is still required; this registry
change does not waive failures, skips, source drift, dirty-tree state or live
provider gates.

The same current-source capture reproduced a safe Windows
`publish_permission` refusal in the combined browser-security partition under
the host's shared temporary hierarchy. The affected tests pass in isolation;
retain this as host-sensitive evidence and require a dedicated private-root or
CI rerun before treating the combined gate as green.

The native read-only documentation and deliberation-context audits are complete.
The two bounded native metadata lanes are complete. Their corrected v2 packets
are independently accepted with the cumulative baseline; the conductor alone
integrates and retains all eight metadata test files in fixed gates and CI.
Shared runtime behavior, roster, providers and private state were outside those
helper grants. Do not rerun unchanged accepted checks or silently relax scope.
Two new finite private-copy grants cover already identified documentation
corrections and the reproduced fleet launch-evidence compatibility regression.
They return additive patches for lead review and conductor integration; they
cannot change shared source, invoke providers or qualify live fleet behavior.
The fleet repair requires a guarded real-module synthetic regression and must
preserve audit digests, explicit evidence shapes and authority/owner fencing.
The documentation grant is now complete and accepted for conductor integration:
three documents, source-checked claims, and independently validated synthetic
plan plus three public command parses. No server or provider example was run.
A third private-copy lane owns one canonical ordinary-host rendered acceptance
packet for U01/U02/U04/U08, using installed headless Chromium and synthetic
sources only. It may return test-only changes and sanitized private rendered
evidence after at most two guarded attempts. Actual assistive-technology and
mobile-keyboard acceptance remain distinct; unavailable facilities cannot pass.
That rendered lane delivered v3 fixtures with explicit focal-task selection.
The minimal task-ID repair passes two actual-host before/after regressions and
is accepted for conductor integration. Lead browser replay reaches canonical
selected-task body scope and independently passes terminal guidance plus pending
authentication-expiry/same-key recovery. A new reproduced focus defect during a
deferred body read has a private repair lane; the lead verifies before integration.
A separate bounded rendered lane covers remaining normative delivery states.
Detail-item namespaces and all authority/body scopes remain unchanged.
The bounded Windows reporting-probe helper is complete: one intercepted
collector-to-stdlib test confirmed hidden startup flags at the child-creation
edge; no child ran and no visibility defect was established. No patch is needed
from that evidence, and no unrelated process-flash investigation is authorized.

The conductor owns shared integration; leadership reviews decisions and source-
backed acceptance. New features, analytics dashboards and additional advisory
review rounds are outside this finish-line packet. Return a concise list of
actual remaining failures or unexecuted gates after each completed packet.

Final-evidence source reconciliation: `tools/release_collect.py` already emits
per-case outcomes and redacted skip records; `tools/release_outcomes.py` binds
review decisions to source, node, reason and platform. The current skip policy
has no reviews. Do not create a second skip collector or treat platform skips
as automatic passes. Review only actual final-run skips with a specific
justification; missing required facilities stay unqualified. The remaining
capture gap is complete candidate-bound rendered/browser/toolchain evidence.

Final-publication distinction: the release manifest has an explicit
`stable` profile and an opt-in `workspace-preview` profile. Stable/public
publication still requires every fixed gate, including `live_provider`, to
pass. The provider-free preview profile accepts only the exact missing-live-
evidence block while requiring all other suites and gates, clean source,
rendered/platform evidence, privacy, migration and managed-install checks.
It is preview evidence, not publication approval; L12 still needs the
separately authorized release-owner decision and no live call is authorized by
this handoff. A blocked result or version label cannot select the preview
profile implicitly.
Historical Ubuntu wording (superseded 2026-09-13): an earlier audit described
an all-nonlive-pass assertion beside the Windows-only composed migration gate.
The current informational job retains incomplete producer diagnostics and
refuses qualification when Windows evidence is unavailable; it does not count
that evidence as an Ubuntu pass.

Final-gate read-only audit, 2026-09-12, adds this finite retention queue for the
conductor before candidate capture:

- Reconcile Ubuntu informational outcomes with required Windows rollback and
  OS-privacy facilities; unreviewed skips remain blocking.
- Declare compatible Playwright/Chromium prerequisites for the retained rendered
  tests. This is CI/environment preparation, not authority to install locally.
- Make the existing rendered-output and explicit-browser mechanisms reachable
  through the restrictive runner using bounded owned outputs and validated
  toolchain configuration; do not broadly forward the caller environment.
- Retain sanitized machine outcomes, adjacent gate artifacts, synthetic rendered
  images and actual toolchain/source binding. Runner-temporary files alone do
  not provide a retained release artifact. Reuse the existing collector.

Current draft review: an external output-directory argument alone cannot prove
rendered retention. Reject stale/reused artifacts, bind actual bounded artifact
names and content to the current source/commands/toolchain, and validate missing
or modified artifacts through the existing evidence consumers. Extension-only
upload patterns do not establish that files were generated by the current run.
Retain a qualified Windows machine bundle; Ubuntu informational evidence and
Windows console output cannot be combined into a fabricated passing bundle.
Any added evidence structure needs explicit version/reader inventory treatment.

Integrated CI packet review, 2026-09-12: fresh output-directory rejection,
bounded names and collection before temporary cleanup are now present. A
process/network-denied synthetic probe found a producer/reader digest mismatch
for CRLF JSON: the producer hashes raw rendered bytes while the reader applies
source-text newline normalization. Both new nested schema readers also accept
boolean `true` as version 1, and a single metadata file satisfies the rendered
bundle shape. The two production modules are now corrected: five independent
pure probes confirm matching raw CRLF digests, strict integer schema rejection,
incomplete-bundle refusal and inconsistent Windows-status rejection. Source
inspection confirms strict final checking requires qualified Windows evidence
and reconciles runtime/gate facts. Actual producer-to-file-reader regression
retention, missing/tampered required artifacts, Windows machine capture and the
two nested inventory mappings still require the completed integrated packet.
The bounded source-only review found missing test-local restoration around
nested collector runs; it did not establish that current bytes still reproduce
the historical order failure. The conductor's stopped partial lane reports 128
combined tests passing, but that report alone does not cover new seam regressions.
No full candidate run or additional provider activity is authorized by this
review, and no requirement count changes on these diagnostic findings.

`release_collect.py` diagnostic completion and `release_gates.py` capture
completion are not passing release decisions. Final `--require-clean` capture,
actual managed-copy facts, reviewed skip policy, exact public/export privacy
review and strict manifest check remain separate. No live evidence, managed
installation change, staging/commit or publication is authorized by this audit.

Completed source corrections: `workspace provision-message` now reaches its
implemented handler through the actual parser Namespace. Its independent
regression and the actual approved-alias remap against a sealed stored-task
continuation both pass; source/test retention is verified. Do not reimplement
these controls. Only the final test strengthening and metadata retention in
the execution order above remain for E12.

## Historical checkpoints through 2026-09-09

2026-09-09 restart checkpoint: F17 public policy provisioning/refresh remains
the conductor's active implementation target. Follow F17_PUBLIC_COMMAND_BRIEF.md
and the source-reviewed F17_RETAIN_LINK_CONTRACT.md. The latter includes native
independent corrections for source authenticity and advanced linked-child
history. Do not repeat accepted client/serialized-refresh component checks
unless relevant source changes. Complete public journeys before closing F17.
Refresh must preserve a caller-recoverable operation identity and return unknown
when its bounded status history no longer establishes an outcome; absence from
a one-entry cache is not evidence that a refresh never happened.

L02: current integrated metadata independently passed 125 tests; the additional
four-family authority patch and dependent inventory-v2 reader-contract patch
are accepted for integration. The combined owned artifact independently passed
167 tests with unchanged sources. Integrate in dependency order, retain the new
reader-contract test in CI and fixed release commands, and verify that combined
checkpoint once. Full L02 remains open for final F17 and other required mapping
reconciliation. Exact scope is in L02_L04_REMAINING_ACCEPTANCE.md. All completed
helper grants have ended; no provider review or managed installation occurred.

Historical checkpoint: 48 verified / 15 blocking out of 63 requirements (76%, rounded).
L04 is accepted after independent full rollback rehearsal, exact canonical
retention and Windows CI/fixed release-gate wiring verification. Final candidate
platform evidence remains L09/L12; no publication or managed installation is implied.
E07 is accepted at provider-inert preview scope, including independently passing
actual parent-chat accounting and fresh-runtime interruption recovery. Preserve
its canonical tests. F17 public command authority/CLI is now the next product
priority; later E07-open checkpoints below are historical.
The final disposition table in RELEASE35_COVERAGE_MATRIX.md controls status;
earlier partial evidence rows and historical checkpoints do not reopen accepted
requirements. R01, R02, R12, R15, F01, F02, G02, L01, R03 and R04 are verified for the preview. Preserve their
accepted evidence until relevant source changes or final candidate validation.

R15 profile continuity is accepted after seven independent guarded cases and
source/version/canonical-coverage review. The conductor is now closing E07's
remaining per-field usage consistency, parent-chat lifecycle fixture and finite
test/schema inventory requirements.
The lead independently accepts the guarded workspace economics journeys
(two passes in 43.20 seconds, no source drift) and verifies exact canonical
retention plus CI/release entries. Full E07 evidence reconciliation remains
lead-owned. Follow
E07_SUBMISSION_ACCOUNTING_BRIEF.md and R15_PROFILE_CONTINUITY_ACCEPTANCE.md.
Ordinary/background sealing, synthetic HTTP ordering and eight-case retry/grant
capacity acceptance are already retained; they are not new implementation work.

The approved R01 queued-version, R02 candidate refusal/fork and four-file R12
operator-body packets are now retained in CI and the fixed release command.
All six files match the reviewed text; the lead independently ran the integrated
entry tests, with 12 passing in 18.34 seconds. The exact crosswalk is in
`R01_R02_R12_CANONICAL_RETENTION.md`. R01/R12 are closed at the accepted preview
scope. R02 public changelog/capability reconciliation is now independently
reviewed and closed for the certified preview scope. Candidate providers remain
explicitly gated; this is not additional native/provider qualification.
The corrected 28-case migration and four-case chat-dispatcher packets are now
retained exactly in canonical source, CI and the fixed release command.
The lead verified both against the reviewed patches and previously
independently passed all 28 cases, including explicit legacy-v1 fork and
cancellation ordering, in 2.26 seconds on an owned source snapshot.
The conductor reports a combined 32-case pass after integration. E07 encoded
capacity source correction and guarded workspace journeys are accepted; whole E07 acceptance
and deterministic Windows setup-error closure remain outstanding.

R01's revocation race is corrected and its six-case regression retained. The
queued registry/adapter/external-version executor packet independently passes
four cases and includes actual public-emitter refusal checks; do not repeat it
unchanged. R01 is closed at preview scope after retention. After finite E07
closure, prioritize F17 ordinary host/CLI message and disposition controls:
these unlock the actual product loop and dependent UI acceptance. Follow
F17_PUBLIC_COMMAND_BRIEF.md and reuse the accepted sole-writer operations.
E12 explicit replacement binding and alias/model/effort journeys remain required
after that product-entry slice. Preserve R15's authenticated v2 expected-profile binding,
strict historical v1 refusal and final locked revalidation during identity work.
Do not widen historical schemas or inspect real accounts. UI/security, remaining migration, documentation and final
candidate evidence remain on the full matrix; none is silently deferred.

## Historical checkpoints

The following chronological record preserves earlier findings and decisions.
Instructions describing completed work are superseded by the current state above.

G02 is VERIFIED-FOR-3.5 at the provider-free council scope after exact canonical
transport test retention and source reconciliation. Progress is 39/63 (62%).
Do not repeat unchanged broad council tests. Integrate the approved F01 strict
18-case packet unchanged and retain exact fixture references; E07's replacement
must preserve accepted R12 intake, and its accounting/privacy review continues
independently. Remaining R12 adapter ceilings retain their existing scope.

F01 semantic mapping correction is now independently accepted: actual Artifact
publication and workspace Review/Decision mappings are separate from effects,
routing and deliberation. Two reference validators pass in 0.72 seconds with
four watched files unchanged. The approved four-case artifact packet is now
verified retained unchanged, including CI/release and exact method references;
the current schema-reference check passes in 0.08 seconds. The lead's isolated
helper is covering three direct plan/registration/command
strictness gaps. The R12 helper is testing positive exact decoded identity and
legacy external prompt-file compatibility through guarded synthetic children.
The conductor must explicitly restart stopped E07 correction work under the
existing bounded grant and return a frozen independently reviewable packet.

Historical scalar correction is independently accepted, 2026-09-08: root's
current-source guarded run passes all 18 retained cases in 4.18 seconds without
230-file drift or live handles. Original 18-case test, canonical compatibility
command, CI and exact format fixture references are now verified retained;
the current metadata validator passes in 0.84 seconds. Artifact identity also
passes independent four-case public publication/reopen/rebuild acceptance with
nine matching dependencies, zero guard hits and zero shared drift. Integrate
its portable test and canonical coverage, then correct actual F01 mappings.
Do not repeat the unchanged historical failure investigation. E07 and R12
exact internal-text correction remain separate active work.

2026-09-08 current closure queue: G02 protocol/public-main integration passes
independent 22-case acceptance with canonical suite and format retention. Its
remaining real long-context child boundary is specified under R12 in
`R12_TRANSPORT_CLOSURE.md`; preserve the accepted protocol correction.
Review/Decision tests are integrated, while the semantic machine mappings and
artifact producer correction remain. E07's handed-off source still contains
the retained semantic failures; use the new bounded correction grant and the
updated brief, including each permitted HTTP submission's durable boundary.
Historical scalar validation and its test retention are complete at the bounded
scope above; earlier queue entries below are historical checkpoints.

Encrypted-version test integration is complete and independently accepted:
the shared file matches the approved packet after line-ending normalization;
canonical commands retain it and corrected format references validate. No
further integration or unchanged broad test run is needed for this packet.

G02 correction approval, 2026-09-08: integrate the independently reviewed
validated-snapshot implementation. Twenty-one focused cases and eight actual
guarded worker invocations pass; original content is consumed after external
post-validation replacement, with old-history carry and both clocks preserved.
Retain one actual public-main pause/context/continue journey and wrong-mode
refusal before whole-row closure. Root inspected implementation and independent
result predicates; no further unchanged broad-suite review is requested.
Proceed with historical scalar validation and artifact identity in the existing
queue; E07 implementation remains independent.

Latest checkpoint, 2026-09-08: G02 compatibility, pre-validation evidence and
both deadline controls now pass independently. Correct G02-SNAPSHOT-01 next:
consume the exact validated round-one snapshots instead of rereading stage files
after validation. The retained reproduction proves replacement reaches later
prompts. Keep this correction isolated pending independent review.

The encrypted numeric-version test packet is independently approved for
integration: 16 actual page-script/WebCrypto cases pass in 1.16 seconds with
zero guard hits and no live children. Integrate the test-only file and retain
it in the canonical schema/browser-security acceptance commands and inventory.
No runtime change or repeat of the prior 104-test browser batch is needed.
This closes a bounded evidence gap, not full L02 or rendered-browser acceptance.
Historical-claim and artifact producer fixes remain next; E07 stays independent.

Current correction queue, 2026-09-08: G02 compatibility/full-r1-evidence/fresh
deadline corrections first; strict historical job-claim scalar validation next;
artifact-ID publication refusal and corrected F01 mappings after that. Reusable
isolated regression packets are handed to the conductor. E07 accounting has
a conductor-reported active isolated worker and stays independent. Browser
integration remains accepted; no repeat batch needed. Consult
`G02_CHECKPOINT_ACCEPTANCE_REVIEW.md`, `F01_NAMED_ENTITY_ACCEPTANCE.md`, and
the historical-claim checkpoint in the schema inventory for exact closure.

2026-09-08 browser integration is independently accepted at the reviewed schema
and response/recovery scope: 104 shared tests pass in 3.42 seconds without
watched-source drift, and canonical release-command/CI retention is verified.
Do not repeat that batch absent relevant changes. Correct isolated G02 findings
and begin the independently scoped E07 ordinary/background/retry accounting
packet under the new leadership grant. Shared integration remains serialized.
The E07 contract must cover actual submissions rather than stopping at a pure
normalizer; its first bounded worker checkpoint may return unfinished gaps.

Current actions, 2026-09-08: complete the approved three-file browser schema
correction with its new acceptance file and fixed-suite/CI/inventory retention.
Keep G02 isolated: independent review reproduced public parser failures and an
original-deadline extension through pre-admission setup. See
`G02_CHECKPOINT_ACCEPTANCE_REVIEW.md` for stable findings and exact acceptance
requirements. The conductor reports the CLI correction; core review continues.
After these corrections, implement E07 submission accounting from the approved
brief. Do not restart the completed 94-test packet or expand provider scope.
Older integration requests below are chronological history, not duplicate work.

2026-09-08 integration checkpoint completed at independent scope: the three
approved packets pass 94 shared-source tests plus 20 subtests in 10.34 seconds;
fixed phase0/schema registry and CI retention are present. Do not repeat those
tests or re-integrate the packets absent relevant drift. The conductor returns
to isolated G02, including under-owner revalidation and resume-bypass refusal.
The new view-schema failure has its own isolated page correction lane. E07's
submission-contract design preserves ephemeral foreground storage behavior;
per-attempt terminal accounting must not silently create persistent jobs.

Immediate conductor checkpoint: integrate the three independently approved
packets (background publication acceptance, OpenCode missing counters and the
format inventory tool) with fixed-suite/CI retention and focused verification.
They touch separate source/test boundaries and must not wait for completion of
the larger isolated G02 candidate. Preserve all shared council/roster edits.
The final format packet passes 73 independent tests in 1.30 seconds and reports
its remaining gaps honestly; the E07 packet passes 18 tests plus 20 subtests.
No whole requirement or release percentage changes until its complete bounded
acceptance criteria are met. After integration, resume the actual G02 failing
test and complete its public pause/submit/continue journey.

2026-09-08 additional approved integration packet: OpenCode token/cache parsing
now distinguishes missing/invalid counters from explicit zero. The isolated
two-file source review and 18-test/20-subtest independent execution pass; shared
baselines and patch applicability match. Integrate at the next safe checkpoint
alongside the already-approved background publication tests, then run focused
shared-source acceptance. Keep G02's incomplete candidate separate. Broader E07
work follows `E07_SUBMISSION_ACCOUNTING_BRIEF.md`; the small parser correction
does not close that requirement. The format-level inventory tool is in an
independent isolated native lane, with no shared runtime writes.

2026-09-08 current next action: the public-emitter corrections are integrated
and independently accepted (five actual-main cases, following the 53-case
candidate gate). The actual detached-background publication test-only packet
is now independently accepted: three passes in 8.12 seconds. The conductor
should integrate it, retain it in fixed phase0/CI commands and run the three
shared-source cases. Historical reproduced emitter failures below are closed
at this bounded scope; do not redo their investigation. Remaining chat and
governed-resume journeys still need evidence.

In parallel, G02 has conditional architectural approval for an isolated council
implementation and acceptance packet. Use the existing `--overall-timeout`,
persist the original absolute deadline and budgets, keep an immutable versioned
council checkpoint with separately admitted idempotent context, and distinguish
checkpoint generation from subsequent writer ownership generation. Preserve
common partial-envelope semantics and actual prior provider/spend facts. No
deliberation kernel edits, deadline resets, implicit rounds or authority changes.
The architect reviews the patch and legacy plus pause/submit/continue evidence
before the conductor integrates it. Release progress remains 60% (38/63).

2026-09-08 next integration checkpoint: the frozen UI/council/history/docs/
registry batch passes 140 independent focused tests. The new public-emitter
acceptance changes the next action from broad investigation to two demonstrated
corrections: accept the canonical executor route at continuation intake without
changing historical private schema, and redact private launch evidence from
`--out` while retaining it for private sealing. Actual argv/main/executor/emitter
tests reproduce two failures and a passing model-mismatch refusal control.
The native helper owns only the isolated two-runtime-file correction plus
directly affected tests; conductor integration follows architect review.
Do not repeat the completed batch or whole release captures. Background/chat
producer journeys remain explicit follow-up after this publication seam works.

2026-09-08 current integration handoff: L01 preview labeling and L02 ordinary
swarm history validation are integrated. The conductor reports 119 passing
swarm regressions; the architect independently verifies all 42 boundary cases
and the three preview cases. L09 custom-harness accounting, full CI registry
equality and strict gate/suite record consumers are integrated. The conductor
reports 121 passing release tests; independent Python 3.13 privacy/outcome
execution passes 86 cases. Whole L01/L02/L09 and release readiness remain open.

Next critical path: connect the actual synthetic executable/version/material
observation producer to governed job gate/main and chat continuation consumers.
Use the current source: provider_launch_control now validates observations; older
notes below about ignored evidence describe historical findings. Prove distinct
gate/main claims, foreground/background handoff, supported fake-child launch and
pre-launch refusal of missing/substituted/forged/replayed evidence and changed
material/version/adapter generation. Preserve cancellation/deadline fencing,
lineage and uncertainty. Reuse accepted tests and correct only missing integration;
do not rerun accepted RB/F15/L03 work without a relevant change. No installed
provider probes, new spend, authentication, installation, commits or release.

Complete this and other planned nonreserved implementation before the final
whole-candidate source freeze/capture. Focused collector and integration tests
are sufficient for intermediate packets; historical source-bound captures do
not become current evidence when the source changes.

2026-09-08 latest independent packet: the corrected real Windows descendant
cleanup plus transport-refusal tests pass (5 cases, 8.23 seconds). The subsequent
adapter-owned fresh/resume attachment, refusal and budget packet passes
16 cases in 23.63 seconds. These replace the earlier fixture-start failure as
current evidence, without qualifying installed providers or all R01/R02/R12.
The R12 documentation must distinguish whole-argv size measurement from its
caller-prompt digest; attachment digests cover the exact built attachment.

L09's 25-minute isolated implementation stage has completed. The conductor
received the 12-file candidate and known release-registry drift, with conditional
integration direction in `L09_SKIP_EVIDENCE_CONTRACT.md`. Root reproduced and
isolated the single newer-pytest test assumption failure: real built-in subtests
are captured even without an external plugin. Correct that test, integrate
without dropping newer fixed commands, and qualify parameter identities and
the affected real release consumers. Empty skip policy grants no exclusions.
Historical checkpoint (superseded 2026-09-13): release progress was then
37 verified / 26 blocking (59%). The authoritative current disposition is
58 verified / 5 blocking in RELEASE35_COVERAGE_MATRIX.md.

The next migration audit is a bounded read-only native assignment. Preliminary
root-confirmed findings: workspace create/inspect outputs and CLI help need
explicit preview labeling, and schema inventory coverage is currently at file
granularity with a `/vN` literal scanner. Already-mapped files can contain
unclassified schema variants; numeric/experimental versions need explicit
classification. Do not turn this into new schemas or redo accepted L03.

That migration audit is now complete. Current inventory has 14 conceptual rows
and 13 named entities; earlier counts of 12 named entities are historical.
The architect's ordinary swarm acceptance reproduces future-schema/foreign-run
acceptance in six cases; both unknown-event controls pass. The conductor owns
`test_swarm_migration_boundary_acceptance.py` and the correction. Preserve
supported recovery and repeat compatibility validation under ownership.

For the next inventory packet, enumerate format/version contracts rather than
treating a mapped source file as complete. Prioritize these producer/reader pairs:

- Plan grants/scopes in `_workspace_plan.py` to strict `_workspace_entry.py`
  readers, including host/project/recipient/task binding refusals.
- Admission/pipe schemas in `_workspace_transport.py` to its admission,
  supervisor-channel and bootstrap readers; reuse existing transport fixtures.
- Continuation source/binding and launch observation formats to jobs/resume
  consumers; preserve history, uncertainty and separately qualified provenance.
- `_workspace_layout.py` to its resolver/validator; existing layout and migration
  refusal tests are evidence to register, not missing implementations.
- Numeric conversation and deliberation formats plus legacy unversioned council
  receipts to their replay/status/restore readers, with frozen historical cases.
- Generic evidence v1/v2 and experimental portable results to their strict
  verifiers/consumers; reuse privacy and uncertainty tests.
- Browser pending-context/message and encrypted-record versions to restore/
  decryption readers; unknown formats must preserve unresolved drafts.

Record current, legacy-read-only, refused or deferred disposition explicitly.
Expand legacy-reader mapping to council/deliberate companion entrypoints and
durable run readers. Presence of a symbol is not behavioral acceptance.
Do not require deferred graph execution or universal native-session attachment.
A separate isolated helper now owns only L01 CLI preview labeling, with a
15-minute grant; conductor integration remains serialized.

L09 implementation now has one confirmed native helper in an isolated source
copy under the recorded grant. The architect brokers dispatch and independent
review; the conductor remains sole integrator. Do not run a duplicate L09 lane.
The current fixed registry includes both observer test files and the new
workspace reconstruction case; the architect checked the parsed argv entries.

U14 source triage for the next UI packet: the page persistently displays Passive
and distinguishes closing the browser from stopping the server. However,
`_workspace_view` supplies `owner_state` while `_workspace_page` displays only
the mode label, and the queue guidance says only "explicitly authorized
invocation" without naming an implemented evaluation action or explaining
that evaluation is unavailable for the current route. Complete this operator
journey with honest route-specific guidance and visible sanitized owner state.
Do not invent a worker-delivery command or imply that `workspace open` starts
provider work. Supervised status needs current owner evidence; foreground
hosting alone is not supervision. Keep U14 blocking until behavior and rendered
guidance match the actual exposed preview routes.

Latest accepted checkpoints, 2026-09-08: L03 is VERIFIED-FOR-3.5 after 19
current reconstruction cases, including actual workspace history/uncertainty
preservation; see `L03_ARCHITECT_REVIEW.md`. The observer correction passed
11 combined independent cases in 46.76 seconds. A subsequent specific-before-base
exception fix preserves typed policy refusals; its new regression independently
passed in 10.72 seconds. The conductor reports the combined 12-case suite green,
and all four handoff file identities matched the architect's current source check.
These supersede the intermediate failures below. Actual bounded-output/descendant
cleanup, isolation and governed jobs/chat producer integration remain open.
Four subsequent pure/mocked resource tests independently pass in 0.15 seconds:
environment filtering, bounded buffer overflow, parent-exit job-handle closure
and Job Object termination precedence. This accepts the callback wiring only.
Actual oversized/stalled executable and descendant-cleanup journeys, including
failed job assignment, remain required; the shared Job Object helper explicitly
documents its post-Popen assignment race and fail-soft behavior.

R01/R02 producer draft reviewed 2026-09-08: `_launch_observer.py` now supplies
an executor-connected Claude version/material producer. The earlier missing
producer description is superseded; implementation is present but not accepted.
Independent execution of `test_launch_observer.py` returned 5 passed and 1 failed
in 36.69 seconds: material replacement after the gate reports
`launch_version_output_untrusted` instead of the expected
`launch_observation_changed`. This is a moving-worktree checkpoint, not candidate
certification. The conductor owns correction of these review findings:

- LO-01: preserve the full vendor version. A pure-function check confirmed
  prerelease and release forms currently collapse to the same trailing-format
  version, which could falsely satisfy a version-bound qualification.
- LO-02: bound captured version output while reading it, and own cleanup of
  timed-out/descendant processes. The current output limit runs after unbounded
  `subprocess.run` capture; source inspection alone does not prove cleanup.
- LO-03: establish and describe the actual probe isolation boundary. A synthetic
  environment check confirms alternative-provider credential keys and home
  lookup remain available. Removing selected environment keys does not prove
  that arbitrary executable code cannot contact a provider.
- LO-04: bind the producer's measured executable/material to the observation
  returned by the executor. A deterministic synthetic replacement between
  producer return and `observation()` confirmed that old version evidence can
  currently accompany replacement executable bytes. No executable ran in this
  check; the temporary synthetic material was removed.
- LO-05: avoid leaving cancellation, deadline or current launch authority stale
  across the second version process after durable consumption. Verify actual
  pre/post-consumption behavior and identity immediately before the main child;
  retain uncertain state where the outcome cannot be established.

Subsequent independent checkpoint: `test_launch_observer_acceptance.py`
returned 3 passed and 2 failed in 22.23 seconds. The two supported version-suffix
cases and the producer-return replacement refusal now pass, closing the
reproduced LO-01 and LO-04 cases at this scope. Cancellation and deadline
invalidation inside the durable callback still reached the process-launch path
and returned `provider_contacted=True`; those LO-05 cases remain blocking.
The retained test additionally tracks the actual `on_spawn` callback; that
assertion-only refinement awaits the correction rerun. The conductor now owns
this new acceptance file and must add it, with `test_launch_observer.py`, to
the fixed release commands. Process-tree, resource, isolation and the complete
governed runtime/qualification matrix remain required; this is not whole R01/R02
acceptance.

Explicitly qualify supported native/entry-script material layouts; an embedded
marker is not producer authentication. Retain actual builder/executor positive
and refusal journeys, distinct gate/main binding and ordinary-dispatch behavior.
Do not copy versions from qualification packets or infer them from content hashes.
Installed/live qualification remains a separate gate; this implementation and
independent acceptance campaign uses synthetic local producers only.

U08 correction independently accepted, 2026-09-08: all six retained page-script
cases now pass, including repeated-failure backoff, timer/announcement coalescing
and recovery reset; the rendered synthetic workspace opens, selects a task, and
preserves confirmed state after controlled local-server shutdown.
`U08_BROWSER_ACCEPTANCE.md` records evidence and remaining rendered gates.
The older offline-suppression findings below are now closed at this scope.

L09 integration checkpoint, 2026-09-08: a fresh parsed-argument audit of
`REQUIRED_COMMANDS` and `REQUIRED_GATE_COMMANDS` finds all 16 unique retained
acceptance files in fixed commands. The list below is the retained integration
checklist; all paths are under `skills/summon/scripts`:

- `test_launch_binding.py`
- `test_launch_qualification.py`
- `test_chat_launch_guard.py`
- `test_chat_launch_guard_acceptance.py`
- `test_resume_revalidation_acceptance.py`
- `test_jobs_revalidation_cli_acceptance.py`
- `test_workspace_loopback_acceptance.py`
- `test_roster_drift_acceptance.py` (added by the subsequent E12 review)
- `test_chat_source_family_acceptance.py` (retained stale-writer authority checks)
- `test_chat_guard_boundary_acceptance.py` (durable revocation ordering and provider environment isolation; added by subsequent independent acceptance)
- `test_chat_revalidation_refusal_acceptance.py` (failed migration must not publish source-family authority)
- `test_chat_source_family_runtime_acceptance.py` (actual runtime with synthetic dispatcher; six selected cases independently pass)
- `test_chat_revalidation_input_acceptance.py` (public malformed/missing packet input privacy and typed refusal)
- `test_chat_legacy_migration_acceptance.py` (public legacy-family packet consumer)
- `test_chat_public_migration_journey_acceptance.py` (public migration through later continuation, immutable history and four authority refusal cases)
- `test_chat_migration_interruption_acceptance.py` (retry/reconciliation before and after durable publication, including intervening refused resume)

The architect checked the parsed command arguments, not just source mentions.
The initial 16/17 tally counted a duplicate interruption-test inventory row;
the corrected unique count is now 16/16. Five
selected existing registry/command/CI consistency tests independently pass
(5 passed, 28 deselected in 19.18 seconds). They verify registry equality,
fixed/hermetic commands, existing test paths, CI partition agreement and exact
machine-evidence commands, not execution of the complete release candidate.
Wire the qualification/migration cases into the relevant fixed runtime/recovery
command and the loopback cases into the UI command when integrating these
packets. Keep registry/runner/manifest consumers consistent and retain actual
results for the final candidate. Add upcoming chat producer/consumer acceptance
to that same integration inventory. Do not treat an unchanged green gate that
never selects these tests as evidence for the corrected behavior. Existing
required gate names and versioned contracts remain unchanged unless a separately
justified contract change is needed.

2026-09-08 F15 acceptance supersedes item 1 below: the architect approved the
provider-free integration in `F15_ARCHITECT_REVIEW.md` after current full-demo,
workspace-core and affected coordinator gate verification. Proceed with R01/R02
in item 3. Preserve the remaining migration, UI, privacy and release scope.

2026-09-08 execution order supersedes historical next-step descriptions:

1. The conductor is implementing F15-A through F15-F: derive and reserve real
   budget state, bind complete ordered context, persist admission provenance,
   and gate every promised simulated worker/message/recovery journey. Include
   cross-object concurrent consumption and failed spawn/admit cleanup. Wire
   rebuild and transport-admission tests into fixed release gates (L09).
   RELEASE35_SCOPE_DECISION.md is adopted; no row was promoted.
2. Independently verify actual journey call sites and failure cases when the
   packet is stable, then complete capability/migration work. RB-01 through
   RB-03 have independent 18/194+12 test evidence and bounded exact Fable
   inspected-source approval; repeat only on relevant source changes.
3. R01/R02 still needs current launch-bound evidence. Source inspection on
   the current implementation is governed by
   `R02_LAUNCH_QUALIFICATION_DECISION.md`: registry declarations, fresh host
   observations and authenticated runtime qualification are distinct; explicit
   legacy revalidation is non-launching and preserves original history.
   Historical source inspection on
   this date confirms `_conversation_runtime._launch_scope_observation`
   translates stored registry declarations, without observing an external
   executable. `_job_continuation.read_private_source` authenticates historical
   launch/result bindings and checks v1 certification, without the v2 scope
   comparison. Preserve these protections and legacy readers while completing
   current launch binding. Inventory membership does not prove migration or
   authenticate a host. Live routes remain separately qualified; no probe or
   provider activation is authorized by this handoff.
   The existing `_job_resume.provider_launch_control` is the concrete successor
   enforcement seam: its `before_launch(_evidence)` currently ignores the
   executor evidence while consuming the durable launch claim. The executor's
   `_subprocess_launch_evidence` binds command text, argv, cwd and environment;
   its command digest is not a digest of executable bytes or a measured CLI
   version. Preserve that private evidence and durable CAS, but do not promote
   either to host/version qualification. Bind fresh trusted observations to the
   exact successor before consuming launch authority, and apply equivalent
   pre-contact enforcement to conversation resume. Keep historical records
   readable without treating historical certification as current authority.
   Acceptance must cover command/version replacement after reservation, stale
   adapter/registry generation, missing or forged host observations, and both
   foreground/background consumers. Refusals must occur before child/provider
   launch and preserve lineage, unknown effects and historical model identity.
   Use synthetic injected host observations for this provider-free campaign;
   they qualify the integration contract, not an installed provider route.
   Boundary clarification from current source: conversation runtime launches
   the dispatcher, which later resolves and launches the provider. Its
   pre-dispatch registry check alone cannot close the replacement window.
   Carry authenticated continuation binding through the child and enforce it
   again at the executor's actual provider boundary. Background gate and main
   provider controls are distinct launches and must retain distinct bindings.
   Never accept a caller-supplied `trusted` boolean or a copy of the registry
   declaration as a host observation. Test missing, substituted and replayed
   observations across the real producer/consumer chain with fake executables
   or injected observations; no installed-provider probe is required.
4. Complete retained UI/reconnect/accessibility, supported-host migration,
   documentation/roster/privacy and integrated candidate gates. Schema
   inventory tests explicitly report unmapped sources; their green result
   cannot establish full producer/consumer coverage. Close each matrix row
   with requirement-matched evidence before changing the progress count.

Historical checkpoint (superseded 2026-09-13): the disposition was then 37
verified and 26 blocking of 63 (59%, rounded). Installation, commit and
publication actions remain reserved and separate from the release-ready source
handoff; the current disposition is recorded in RELEASE35_COVERAGE_MATRIX.md.

R12 queued source finding, 2026-09-08: independent provider-free execution of
the real `_executor.execute_agent` entry with Gemini/subprocess and an explicit
1,000-unit argv budget for a 4,000-character prompt attempted the recursive ACP
fallback after budget refusal. The recursive call was intercepted and Popen
forbidden; no process or provider was contacted. The fallback condition excludes
private attachments but still permits rejected explicit argv capabilities.
The existing `test_declared_budget_blocks_before_executor_spawn` covers Codex,
so it does not exercise this native-ACP branch. In the R12 transport packet,
make explicit structured budget refusal and malformed capability fail closed
without selecting a secondary transport. Preserve ordinary legacy dispatch's
separate fallback behavior. Retain ACP-capable caller regressions and the
64 KiB actual-transport/whole-message tests required by R12; this one fix alone
does not close that gate. Do not interrupt the F15 freeze or R01/R02 sequence.

Retained R12 acceptance now exists in `test_transport_refusal_acceptance.py`:
2 failed and 1 passed in 0.30 seconds, exit 1. Both an explicit oversized argv
budget and a malformed capability selected ACP; the recursive route was
intercepted and Popen forbidden. The legacy OS-limit fallback without a
structured capability passed its compatibility control. The conductor owns
this file after handoff and should correct the branch at the next safe executor
edit boundary, then add it to fixed commands. Full capable-path/64 KiB evidence
remains separate; this failure is not a provider attempt or quota event.

Subsequent R12 correction: `test_transport_budget.py` plus the refusal acceptance
now independently pass 11 cases in 0.17 seconds. The explicit capability branch
no longer selects ACP; legacy fallback retains its separate behavior.
`test_attachment_transport_acceptance.py` additionally passes four actual
fake-child cases in 22.24 seconds: fresh/resume payloads above 64 KiB with
Unicode, mixed newlines, quotes and BOM content arrive byte-exact at the declared
limit; exceeding that limit by one byte refuses before receiver contact, and
attachments are removed. Real attachment ACL/write/cleanup runs in this fixture.
Its initial failure was a missing platform field in the test, not a product bug.
This does not qualify installed ZCode, permissions or governed continuity.
The conductor owns this new test after handoff. Published operation-specific
budgets and runtime binding of measurement platform to the actual local launch
remain to be verified before whole R12 acceptance; the executor currently uses
the supplied capability platform as its measurement platform.

G02 source clarification, 2026-09-08: this is an implementation gap, not only
a missing acceptance run. `_council.run_council` checkpoints `round1_complete`
and immediately builds the second-round prompt from the original question and
member positions. The council companion skill describes no between-round
human-context operation. Finish this requirement in the governance packet after
the current R01/R02 migration; do not interrupt that repair or silently defer G02.

Architect scope gate: G02 belongs to advisory council. Its optional pause,
context submission and explicit next-round continuation must use council
receipts and ownership, with a separately versioned context/checkpoint record.
Do not add `deliberate continue`, activate two-round live deliberation, or edit
the fixed-option state/scheduler/replay/live kernels for this requirement.
Those are separately gated features. A preliminary deliberation-targeted
proposal was rejected before implementation; retain its useful idempotence,
privacy and frozen-deadline constraints only in the corrected council design.
The conductor must return the corrected bounded command/state/touchpoint
proposal before implementation, preserving default automatic council rounds.

The required behavior is an explicit bounded checkpoint at which the human may
submit context for the next authorized round. Preserve ordinary automatic council
invocation compatibility by making the checkpoint behavior explicit. Bind submitted
context to the council run, checkpoint generation and next round; order it and
freeze its inclusion before launch. Context may inform positions but cannot
change seats, models, permissions, physical attempts, launch budgets, deadline,
routing, quorum or approval policy. Waiting must not silently renew the original
deadline. Expired or uncertain work remains refused/held until a separately
authorized action; a late message cannot restart a round. Advisory council output
must never become a deliberation ballot through this path.

Acceptance must drive the real checkpoint/resume producer and consumer using
simulated children: context reaches only the intended next round, budgets and
deadline remain identical, duplicates do not consume another attempt, competing
or stale submissions refuse without mutation, and interruption preserves both
context identity and launch uncertainty. Exercise hostile prose requesting new
seats/spend/ballots and prove it stays data. Preserve old receipts and the default
automatic-round CLI regression. No live council run is authorized for this gate.

E12 independent roster checkpoint, 2026-09-08: the new
`test_roster_drift_acceptance.py` passes three synthetic cases (2.79 seconds,
Python 3.12): rename, removal, and externally replaced approved alias mapping.
The original frozen roster remains unchanged and refuses revalidation; a
separately requested freeze produces distinct valid evidence. Alias replacement
is simulated using a separately approved fixture registry, not an assertion
that the shipped role CLI can overwrite an approved alias. No provider or task
grant is created. Retain these cases in the fixed roster/runtime release gate;
actual in-flight task rebind/fork and submission-boundary coverage still need
their complete evidence before promoting E12. The conductor owns subsequent
integration of this new independent test file.

F17 public-command review, 2026-09-08: the workspace parser and entry currently
expose `create`, `open`, `inspect`, and explicit `demo-create`. Inspection is
provider-inert, but this is not the full command surface required by F17.
The HTTP message/send-and-lookup routes exist in `_workspace_ui.py`; the
operator command adapter in `_workspace_commands.py` currently declares
`cancel_queued_context` and `dispose_held_context`. Complete the public CLI/API
coverage of authenticated message send and reconciliation, retaining a hold,
eligible cancellation, reason-bound dead-letter disposition, and proposal of
a separately linked delivery under fresh authority. Reuse the typed runtime
methods and sole writer; an inspection handle or local process identity cannot
become mutation authority. Preserve request identity, stale-generation refusal,
noninteractive behavior, original IDs and uncertain outcomes across both entry
points. Source review establishes the current surface; parser probes launch no
provider and are not end-to-end command acceptance. Keep this in the existing
public-interface packet after the current qualification repair.

F01/L02 schema review, 2026-09-08: the inventory enumerates the required named
entities, but validation currently proves source/schema/fixture presence only.
Its dynamic unmapped-source report includes launch qualification/observation,
job continuation/control, fleet approval/dispatch, context compilation, usage,
layout/view, and browser recovery records. Classify each at the final source
freeze as producer, reader, projection, browser-private state, or intentionally
unchanged legacy contract, with its real validator and compatibility fixture.
Do not invent a new schema solely because a source appears in this report, or
claim reader/migration behavior from a matching literal. The newly changing
qualification schemas belong to the current R01/R02 inventory update.

U08 retained independent acceptance now lives in
`skills/summon/scripts/test_workspace_loopback_acceptance.py`. It executes the
actual complete page script, including its bootstrap submit handler, with a
synthetic DOM/network/timer host. On the current source, two cases fail: an
offline browser hint suppresses the initial view after successful local
authentication, and suppresses the request/backoff path for an unavailable
local server. Three controls pass: hidden-page scheduling, authentication
expiry, and absent authentication. The focused result is 2 failed / 3 passed
in 0.58 seconds under Python 3.12, with no providers or persistent host.
The conductor owns subsequent adaptation and the UI fix. Preserve the failing
behavioral requirements; rendered geometry remains a separate acceptance gate.

U08 loopback reconnect finding, 2026-09-08: `_workspace_page.py` returns from
`poll()` and suppresses `schedule()` when `navigator.onLine` is false. Independent
Node execution of the actual polling function, with authenticated synthetic state
and a successful request stub, performed one view request/render with the flag
true and neither with it false. The local loopback surface must still attempt an
explicit refresh and first authenticated snapshot under that condition; a browser
connectivity hint cannot establish that this local server is unavailable. Preserve
bounded retry/backoff, hidden-page behavior and session-expiry refusal. Retain
tests for offline-reported/reachable loopback and genuinely unavailable servers.

A temporary real Chrome review rendered the login page but did not reach the
workspace after submission. The browser's connectivity flag was not available
through the DOM-only review tool, so that stall's cause remains unproven; it is
not evidence of a passed login or reconnect journey. The owned tab and temporary
server were closed, with terminal cleanup confirmation and no workers/providers.
Complete this correction and rendered acceptance in the UI packet after R01/R02.

R15 acceptance reuse, 2026-09-08: retain the existing account-profile fixtures
for isolated child environments, credential-state digest changes and detached
profile selection. They are useful component evidence, not full resumed-child
rotation qualification. `_executor.build_request_identity` includes
`profile_state_sha256`; `_job_resume.validate_loaded_invocation` currently
compares profile name/path/registry/command fields without that explicit state
field. Trace the authenticated request identity through reservation and actual
child launch before deciding whether this is covered indirectly. Acceptance
must rotate a synthetic selected profile after reservation and prove refusal
or explicit revalidation before contact, with no fallback to another account.
Cover Claude's supported continuation and Codex's fresh/background selection
plus candidate-resume refusal; do not fabricate a certified Codex resume path.

Owner correction, 2026-09-06: Summon improvements is the conductor. The architect/project lead retains planning, architecture, independent acceptance and Fable consultation. The bounded operator-message source/runtime/HTTP/Node/rendered-browser journey and the foreground workspace entry are architect-approved at provider-free preview scope. The accepted entry uses the real parser, PTY-owned server, browser authentication, persisted reopen, scope-expiry demotion and truthful cleanup. The former local helpers remain stopped on usage limits; preserve their work and do not blindly retry or switch accounts.

Accepted evidence is scoped, overlapping and not additive:

- Actual scripted conductor: two iterations with useful bounded side work and independently checked results, simulated workers only.
- Actual authenticated simulated worker-to-worker and taskless supervisor delivery, ordering, receipt and recovery boundaries.
- Operator UI/API: 172 tests plus a bounded rendered keyboard/lost-response/reload/restart journey. Exact pending identity survives; explicit lookup avoids duplicate disposition. Unknown effects remain unknown.
- REC-01: fresh Python supervisor endpoint recovery, two tests plus two subtests, 22 unchanged packet files. Parent-harness cleanup provenance remains explicit.
- REC-02: full runtime suite plus actual queued/included/submitted same-task process-recovery node, 110 JUnit cases in 136.91 seconds, zero failures/errors/skips, 28 unchanged packet files. A predeclared two-attempt budget and explicit synthetic host confirmation permit the bounded successor; earlier indeterminate claims and uncertainty remain. This is not abrupt-crash, power-loss or live-provider qualification.

At REC-02 acceptance its packet differed from current source only in `_workspace_admission.py`. The lead has subsequently changed admission classification and state replay for operator sends and added `test_workspace_operator_send.py`. The combined pure/worker-send regression gate passed 538 JUnit cases in 48.59 seconds, including 14 new operator cases, with no failures/errors/skips or changes during the run. Requalify the complete candidate after runtime integration; the earlier recovery packet is not a moving-tree release gate.

## Accepted kernel and next product outcome

The typed human-send kernel, rendered journey, foreground entry and versioned
provider-free plan creation are accepted at bounded preview scope. The next
product outcome is integrated 3.5 closure: retained UI evidence, affected
capability/migration regressions, roster/documentation/privacy reconciliation
and release-owner review. Keep the plan authority-inert: it may describe
bounded goal, acceptance criteria, tasks, dependencies and operator-message
destinations, but it cannot select providers/accounts/models, authorize spend
or commands, change permissions/policy, resume sessions or launch workers.

### Current implementation checkpoint: versioned plan creation

The provider-free `workspace create --plan FILE` slice is now implemented in
`_workspace_plan.py` and `_workspace_entry.py`. It accepts only the strict
`summon.workspace.plan/v1` schema, rejects unknown/duplicate fields and
dependency cycles, bounds task/criteria/target/context sizes, and compiles
message-only authority records. Creation uses a deterministic private staging
directory and no-replace publication; a pre-publication failure leaves an
explicit recovery marker and never claims success. The copied plan and a
versioned host configuration are private, content-bound records.

Reopen and inspect re-read the copied plan, recompile it with the host-bound
expiry, verify the compiled-plan digest, compare the durable goal/lane records
and require every compiled source reference/digest to remain present. They do
not resume workers, providers, accounts, models or sessions. The operator
retention scope preserves each target identity (including the 16-target
capacity case), while legacy synthetic demo grants remain isolated to the
fixture schema.

Provider-free evidence for this checkpoint: plan/protocol/entry `435 passed`;
coordinator/admission/runtime/state/protocol `215 passed, 75 subtests`;
page/UI/view/conversation/deliberation/chat `188 passed, 8 subtests`;
Windows silent-launch `13 passed`; and release-manifest/gate tests `32 passed`.
The focused dormant-destination authority matrix passed `5` cases, including
unchanged journal/state refusal and explicit registration. The architect
independently accepted the bounded plan milestone after rerunning focused plan/
authority and Windows checks plus the 16-target create/open/send/reconcile
journey with source stability. These are implementation gates, not 3.5.0
release approval. Retained UI evidence, pending roster review, migration and
troubleshooting reconciliation, managed-copy convergence evidence and the
separate release-owner decision remain open.

The requirement-level status matrix is maintained in
`docs/planning/RELEASE35_COVERAGE_MATRIX.md`. It deliberately marks broad
requirements `PARTIAL` or `OPEN` where the current fixtures cover only a
bounded slice; aggregate test totals must not be used as a substitute.

1. Core: the lead completed and tested pure operator composite replay and a journal-derived operation/proof index in `_workspace_state.py`, plus admission obligation classification. Its one detached transition produces a message, queued delivery, proof and response; it preserves strict ordinary-worker admission. The lead has now added dedicated `admit_operator_message` and `reconcile_operator_message` methods through the existing sole writer. The combined gate passed 242 cases in 137.12 seconds, including 20 actual operator-journal cases, with no failures/errors/skips or watched-source changes. Content publication precedes ownership, exact scope is rechecked after final synchronization and after append, and lookup synchronizes without repair or resend. The earlier raw-event limitation is retained as historical context; the current typed operator path is the supported provider-free route. Full integrated release evidence remains mandatory.
2. Runtime accepted at bounded local/provider-free scope: `install_operator_messages`, `describe_operator_messages`, `revoke_operator_messages`, `send_operator_message` and `reconcile_operator_message`, plus the versioned base contract and operator admission-source resolver. Tests cover real private content, verified grant sources outside ownership, current authority, restart reinstallation/lookup and a fixed receiver consuming the exact selected multiline message. Live-provider and native-session qualification remain separate; the rendered provider-free journey is accepted.
3. Consumers now compile context, resolve grants and project the explicit operator union. Worker ingress remains strict; the implementation never invents a source task or worker identity. The fixed receiver consumes the exact selected complete bytes in the provider-free runtime tests. Remaining work is release evidence and live/native qualification, not a missing operator consumer.
4. Browser contract and rendered behavior are accepted at bounded provider-free scope: the scoped composer and draft-key route keep goal and priority visible, label the sender local operator, render text as text, coalesce edits and clear only after matched durable queue evidence. The authenticated rendered journey has been exercised; the retained integrated release receipt remains a separate gate.
5. Draft persistence is accepted at bounded provider-free scope: the implementation uses a separate retention secret, HMAC-bound opaque operator scope, actual stable operator binding, frozen pending sends, fresh nonces/authenticated associated data and exact storage/wire bounds. It never stores plaintext, bearer material or reusable keys in browser storage, does not auto-send after restore, and preserves unresolved records on cleanup/auth uncertainty. Memory-only opt-out and ciphertext discard remain deferred follow-up decisions. Full release evidence and supported-host migration qualification remain open.

Wire bounds remain `{operation_key,target,text}`, 32 lowercase hexadecimal key characters, at most 2048 UTF-8 text bytes and 4096 encoded request bytes. Host fixes sender, stream, ordering, recipient and grant binding. Shared capacities remain finite; measure reachable reserve rather than claiming the structural maximum as operational capacity.

The rendered human-send gate is accepted at bounded provider-free preview scope. It covers keyboard composition, exact Unicode/multiline bytes, per-tab encrypted draft persistence across full reload, fresh authentication without automatic send, same-key lookup after a lost result, visibly frozen unreadable records, stable scrolling/focus, narrow viewport/reflow and explicit host-authority loss. The revocation pass found and corrected one presentation defect; reauthentication alone cannot restore revoked host authority, and the journal count remains unchanged. The standalone HTTP/UI suite needs a retained terminal receipt in the later integrated release gate.

## Runtime and presentation integration findings

The dedicated coordinator methods accept trusted callbacks, not browser credentials. The implemented runtime installs bounded operator/recipient authority independently, verifies actual registered source bytes outside the writer callback, and conjunctively rechecks current host/session authority during send and lookup. The content callback uses the real private ContentStore in the bounded integration path; provider contact, native-session attachment and full release qualification remain outside this evidence.

`read_send_admission_evidence` now verifies the operator domain against its retained event and registration. The selected-context compiler consumed a complete operator message in the actual fixed receiver test; combined integration remains subject to the conductor's broader verification. Existing operator disposition currently restricts message kind to ordinary worker messages; review its supported typed scope explicitly before extending it.

The earlier review finding that `_workspace_view.py` rejected `operator_message` is superseded by the current source: the view now validates and projects the explicit operator variant without inventing a source task, and the local-operator label remains distinct from model identity. Preserve strict legacy and supervisor validation; no presentation exception or permissive fallback is acceptable.

## Accepted: supported workspace entry

The thin executable facade now runs through the actual command parser and dispatcher. It uses foreground-owned demo-create/open/inspect with explicit owner stop and cleanup. Opening history never resumes workers. It independently reinstalls operation authority, rotates bootstrap/session secrets and preserves only bounded private launch configuration and presentation identity needed for restart.

The accepted gate did not reuse conversation bearer fragments or persisted bearer URLs, add PID-based remote stop or treat read authentication as lifecycle authority. An occupied explicit loopback port refuses without fallback. The real parser/launcher/server/browser/reopen/expiry/cleanup lifecycle passed without provider or worker contact.

## Follow-up: versioned user-plan hardening

Continue hardening the implemented `workspace create --plan FILE` path against
source/journal tampering, staged-run recovery, Windows reparse/ACL behavior,
and public error/privacy regressions. Keep `workspace demo create` visibly
synthetic and qualification-only. Do not infer provider, model, account,
spend, permission, command, resume or worker-launch authority from a plan
file. The integrated candidate still needs independent review and the normal
managed-copy, documentation, and release gates.

After the human and entry journeys work, close the remaining recovery/collision/fork and rendered accessibility matrix from the normative checklist; run affected legacy dispatch, continuation, council and deliberation compatibility. Review pending Astra/Flash changes separately. Bind Fable's final skeptical review to the exact integrated candidate and authoritative served identity. Reconcile README, expected/release/engineering changelogs, migration and troubleshooting; verify public privacy and rollback/install plans before the separately authorized release steps.

## Leadership checkpoints

Use evidence that changes a product decision. A completed helper or passing isolated contract is not the complete human journey. Keep one owner per changing seam; record handoff context and verify a lane actually starts before calling it active. Do not spend another cycle rediscovering the unavailable former developer task or attributing unrelated workstation popups.

Next acceptance is the integrated 3.5 closure packet against one stable source
candidate. Universal external-session attachment, live autonomous long-horizon
loops, native swarm adapters and 10+ worker qualification remain later
milestones; council and deliberation authority remain explicit throughout.

HANDOFF: the conductor owns hardening and independent acceptance of the implemented
versioned `workspace create --plan FILE` slice. The plan is descriptive input,
not authority. Its exact bytes and identity are bound to a private copied plan,
host-bound expiry, compiled digest, durable goal/lane projection and source
references; it creates only dormant context-queue destinations and is
reopenable/inspectable without launching or resuming workers/providers. Preserve
uncertainty and immutable historical identities. Report implemented, preview,
simulated, gated and released states separately; keep the original goal active
until its full requirements are satisfied.

Plan-created destinations are not coordinator workers. Creation leaves the
worker registry empty; an operator message may queue against a source-verified
destination grant, while claim, renewal, worker-send, artifact, completion and
other worker mutations fail until a separate adapter explicitly registers a
worker instance.

## Confirmed fleet evidence compatibility regression

2026-09-12 correction accepted for integration: independent guarded real-module
replay passes 69 cases in 7.51 seconds with zero skips/failures, unchanged
shared/owned source, empty synthetic home and no bytecode. The v2 packet accepts
explicit historical eight-field, current ten-field and current eleven-field
observation shapes; partial/unknown/null/superseded shapes refuse. It validates
raw prompt and audit digests, strict observation/shared bindings and preserves
all accepted evidence in the private launch digest. Existing authority, CAS,
capacity, cancellation/deadline and public-redaction controls pass. The current
ten-field callback is the reachable repair; observation compatibility does not
qualify a live fleet route. The conductor must integrate the runtime/test packet,
retain its source-literal test IDs in fixed gates/CI and reconcile fleet metadata.

An isolated reproduction executes unchanged AST-selected production validator
and callback methods. The base eight-field launch evidence is accepted; adding
the executor's valid `dispatch_payload_sha256` and `attempt_id_sha256` fields
causes `fleet_activation_launch_evidence_invalid`. The control and runtime
callbacks preserve both fields. Independent replay passed three cases in 0.037s.
This is component reproduction plus source-traced reachability after authority
gates, not end-to-end provider qualification: the durable claim API was replaced
with a synthetic bridge and neither executor launch nor private stores ran.

The conductor must reconcile this compatibility failure before declaring the
affected fleet path qualified. Use explicit evidence shapes, validate audit
digests and prompt binding, preserve all accepted evidence in the launch digest,
and retain unknown-field refusal, authority checks and owner/CAS fencing.
Account for optional launch-observation evidence explicitly. Require a real-module
synthetic regression for the repair; the reproduction is not repair acceptance.
This finding does not authorize provider calls or broaden the 3.5 preview scope.

### Conductor current packet

- Outcome: DONE for the canonical external-reader consistency slice and finite
  deliberation outcome labels. External detail records now reuse the single
  validated council receipt or deliberation status payload, including decision
  bodies, so returned content cannot come from a later validation generation;
  terminal projection states retain truthful decision labels (`DECIDED`,
  `UNRESOLVED`, `CANCELLED`, and the finite remaining terminal outcomes).
- Changed files: `skills/summon/scripts/_workspace_canonical.py`,
  `skills/summon/scripts/test_workspace_details.py`, and this handoff.
- Acceptance evidence: `python -m pytest
  skills/summon/scripts/test_workspace_details.py -q -k
  "canonical_council_http_body or canonical_deliberation_http_body"` -> 2
  passed, 11 deselected; `python -m pytest
  skills/summon/scripts/test_workspace_details.py -q -k
  "terminal_outcomes"` -> 1 passed, 13 deselected; `python -m pytest
  skills/summon/scripts/test_workspace_details.py -q` -> 14 passed, 0
  skipped; `python -m py_compile
  skills/summon/scripts/_workspace_canonical.py
  skills/summon/scripts/test_workspace_details.py` -> exit 0.
- Unresolved findings: none in this packet; provider/authentication paths were
  not exercised.
- Next packet: Astra performs the independent integrated review and preserves
  the existing public schema, opaque identities, and body-scope boundaries.
- Resources left behind: no persistent artifacts or external resources.

Workspace UI/rendered finish-line checkpoint (2026-09-12):

- Outcome: DONE for the accepted task/detail presentation and bounded rendered
  UI packet. Task shell and typed detail targets now share the canonical task
  presentation identity while detail-item handles remain in their separate
  namespace. Pending authorized-body requests move focus to the existing Close
  control before disabling the request button, preserving Escape dismissal,
  generation fencing, and duplicate-request prevention.
- Changed paths: `skills/summon/scripts/_workspace_details.py`,
  `skills/summon/scripts/test_workspace_details.py`,
  `skills/summon/scripts/_workspace_page.py`,
  `skills/summon/scripts/test_workspace_page.py`,
  `skills/summon/scripts/test_workspace_canonical_rendered.py`,
  `skills/summon/scripts/test_workspace_delivery_rendered.py`,
  `tools/release_manifest.py`, `.github/workflows/ci.yml`, and this handoff.
- Acceptance evidence: the focused source/HTTP/Node partition (`test_workspace_details.py`
  and `test_workspace_page.py`) passed `35 passed in 42.46s`; the two rendered
  Chromium fixtures passed `3 passed in 57.90s` with no skips; affected modules
  and `tools/release_manifest.py` compiled successfully; `tests.test_release_manifest`
  passed `23 tests in 24.393s`.
- Rendered boundary: fixtures use synthetic provider-free protocol records,
  loopback-only routing, and an owned headless browser. They do not qualify
  provider execution, real worker execution, assistive technology, mobile
  keyboards, or a release. Browser/toolchain unavailability remains a failing
  prerequisite for these rendered tests, not a silently promoted pass.
- No provider, authentication, account, spend, install, Git, release, or
  external-network action was exercised. No persistent process or external
  resource was left behind.
- Unresolved findings: the broader integrated UI/release gate and Astra's
  independent review remain open.

Fleet launch-evidence compatibility checkpoint (2026-09-12):

- Outcome: DONE for the bounded provider-inert fleet evidence packet. The
  reachable enriched callback now accepts the historical eight-field launch
  projection, the current ten-field projection, and the compatibility
  observation-bearing eleven-field projection. It preserves prompt and
  attempt binding checks, strict observation/shared-binding validation,
  unknown-field refusal, single-use/CAS/deadline/cancel fences, full-object
  private digesting, capacity accounting, and public redaction. The
  observation-bearing shape remains compatibility-only and does not qualify a
  live provider launch.
- Changed paths: `skills/summon/scripts/_fleet_dispatch.py`,
  `skills/summon/scripts/test_phase1_fleet_launch_evidence.py`,
  `tools/release_manifest.py`, `.github/workflows/ci.yml`, and this handoff.
- Acceptance evidence: focused fleet dispatch/runtime/evidence tests ->
  `138 passed in 15.55s` (exit 0); focused module compilation -> exit 0.
  The new evidence regression is retained in the fixed `phase0_phase1`
  manifest and CI fleet coverage.
- Boundary evidence: no provider, authentication, account, spend, install,
  Git, release, or external-resource action was exercised. No live launch
  qualification is claimed.
- Resources left behind: no external/provider resources or persistent
  artifacts.

E12 replacement-binding integration addendum (2026-09-12):

- Outcome: DONE for the accepted replacement-binding implementation and the
  provider-inert approved-role target-drift journey. Replacement bindings keep
  the recorded permission ceiling and definition digest, preserve parent bytes,
  and authorize no provider execution; the roles-enabled dispatcher refuses an
  approved alias whose stored target definition has drifted before launch.
- Changed files: `skills/summon/scripts/_conversation.py`,
  `skills/summon/scripts/_conversation_runtime.py`,
  `skills/summon/scripts/test_e12_turn_fork_acceptance.py`,
  `skills/summon/scripts/test_roster_drift_acceptance.py`, and this handoff.
- Acceptance evidence: `python -m pytest -q
  skills/summon/scripts/test_e12_turn_fork_acceptance.py` -> 36 passed in
  25.82s, 0 skipped; `python -m pytest -q
  skills/summon/scripts/test_roster_drift_acceptance.py` -> 4 passed in
  4.21s, 0 skipped; `python -m pytest -q
  skills/summon/scripts/test_job_continuation.py -k role_alias` -> 1 passed,
  47 deselected, 0 skipped, in 0.77s; `python -m pytest -q
  skills/summon/scripts/test_model_routing.py` -> 14 passed in 0.84s, 0
  skipped; `python -m py_compile skills/summon/scripts/_conversation.py
  skills/summon/scripts/_conversation_runtime.py
  skills/summon/scripts/test_e12_turn_fork_acceptance.py
  skills/summon/scripts/test_roster_drift_acceptance.py
  skills/summon/scripts/run_subagent.py` -> exit 0.
- Unresolved findings: none in this bounded packet; provider, authentication,
  account, and spend paths were not exercised. The full integrated release
  matrix and independent Astra review remain open.
- Next packet: Astra performs the independent integrated review, retaining
  exact-model/model-effort refusal behavior and the existing alias/schema
  boundaries.
- Resources left behind: no persistent artifacts or external resources.

L02 metadata v2 remainder integration checkpoint (2026-09-12):

- Outcome: DONE for the accepted metadata v2 remainder and prerequisite
  reader-contract, stable-authority, context, and fleet layers. The source
  inventory preserves distinct deliberation input/prompt shapes, authority
  versus evidence boundaries, the detached private swarm projection, and
  retained evidence-gap semantics. The fixed release/CI `schema_inventory`
  command retains `tests/test_schema_inventory.py` plus all seven
  `tests/test_format_inventory*.py` files.
- Changed paths: `tools/format_inventory.py`, `tests/test_format_inventory.py`,
  `tests/test_format_inventory_l02_acceptance.py`,
  `tests/test_format_inventory_reader_contract.py`,
  `tests/test_format_inventory_context.py`,
  `tests/test_format_inventory_fleet.py`,
  `tests/test_format_inventory_nonworkspace.py`,
  `tests/test_format_inventory_workspace.py`, `tools/release_manifest.py`,
  `.github/workflows/ci.yml`, and this handoff.
- Acceptance evidence: `python -m py_compile tools/format_inventory.py` plus
  the resolved `tests/test_format_inventory*.py` paths -> exit 0; the exact
  fixed eight-file provider-inert command -> `203 passed in 72.35s` (exit 0).
  A source-only
  validation check reports `184` rows, `0` unmapped literals, detached list
  `['swarm-projection-rebuild']`, and `155` retained evidence-gap rows.
  `python -m pytest -q tests/test_release_manifest.py` -> `23 passed in
  67.80s` (exit 0), confirming fixed manifest/CI retention.
- Contract boundaries: lexical completeness is not universal runtime
  qualification; documented historical/runtime gaps remain explicit. No
  provider, authentication, account, install, Git, staging, release, or
  external-resource action was exercised, and no private path or packet
  location is recorded here.
- Resources left behind: no external/provider resources or persistent
  artifacts; the repository changes remain for Astra's integrated review.

CLI/parser, durable-alias, and release-docs finishline checkpoint (2026-09-12):

- Outcome: DONE for this bounded packet. The approved durable alias source is
  sealed through the real proposal/approval, private-source, reserve/prepare,
  and child-context path; an unchanged approved binding passes the loaded-child
  validator, while a separately approved replacement registry refuses the
  remapped child before any launch seam. Historical private source/result bytes
  remain unchanged, and a separately requested fresh resolver sees the new
  target without resume or ballot fields.
- Changed paths: `skills/summon/scripts/_cli.py`,
  `skills/summon/scripts/_builder.py`,
  `skills/summon/scripts/run_subagent.py`,
  `skills/summon/scripts/summon.cmd`,
  `skills/summon/scripts/test_roster_drift_acceptance.py`,
  `skills/summon/scripts/test_workspace_entry.py`, `README.md`,
  `docs/PROTOCOL.md`, `docs/PHASE1_OPERATOR_GUIDE.md`, `CHANGELOG.md`,
  `docs/ENGINEERING_CHANGELOG.md`, `docs/PHASE1_MIGRATION_ROLLBACK.md`,
  `docs/SUMMON_RESUME_CAPABILITIES.md`, and this handoff.
- Acceptance evidence: `python -m pytest -q
  skills/summon/scripts/test_roster_drift_acceptance.py` -> 5 passed in
  4.83s; `python -m pytest -q skills/summon/scripts/test_workspace_entry.py`
  -> 53 passed in 134.55s; the exact parser/dispatch regression -> 2 passed
  in 0.66s; `test_phase1_operator_guide_only_documents_live_commands_and_flags`
  -> 1 passed in 1.13s; `python -m pytest -q tests/test_release_contract.py`
  -> 7 passed in 0.87s; focused `py_compile` -> exit 0; README's three
  provider-free workspace examples -> parsed by the production parser; comment
  behavior source checks -> passed.
- Boundary evidence: no provider, authentication, account, spend, install,
  Git, or external-network action was exercised. The full workspace-entry
  suite uses only its existing local provider-inert fixtures; no process/provider
  dispatch was launched by this packet, and no persistent server/resource was
  left behind.
- Unresolved findings: none in this bounded packet. Astra's independent
  integrated review and any broader release gate remain open.
- Resources left behind: no persistent artifacts or external resources.

E12 test-strengthening closure checkpoint (2026-09-12):

- Outcome: DONE for the accepted test-only packet. The durable-start drift
  fixture now proves two unchanged pre-start identity reads, a single post-start
  definition change, frozen start identity/history, and no launch guard or child
  process. The model and effort resume fixtures each prove an unchanged control,
  isolate exactly one mutated field, preserve the pending claim, and leave all
  files byte-stable.
- Changed paths: `skills/summon/scripts/test_conversation_runtime.py` and
  `skills/summon/scripts/test_job_resume.py` only.
- Acceptance evidence: the three focused cases passed; the bounded
  conversation/runtime, job-resume, and roster-drift partition passed `106
  passed, 4 subtests passed`; focused `py_compile` and `git diff --check` both
  passed. Both test files remain retained by the fixed release/CI commands.
- Boundary evidence: no runtime/provider/auth/install/Git/release/network action
  was exercised and no external resource was left behind.

Final L02 metadata reconciliation checkpoint (2026-09-12):

- Outcome: DONE for the accepted metadata-only packet. The conversation v2
  participant-replacement structure is inventoried as optional durable history;
  it is not launch authority. Fleet launch evidence retains the historical
  eight-field shape and records the current ten-field and observation-bearing
  eleven-field compatibility shapes. The eleven-field observation remains
  compatibility evidence, not live qualification.
- Changed paths: `tools/format_inventory.py`,
  `tests/test_format_inventory_fleet.py`,
  `tests/test_format_inventory_final_structures.py`,
  `tools/release_manifest.py`, `.github/workflows/ci.yml`, and this handoff.
- Acceptance evidence: the exact fixed nine-file provider-inert metadata
  command -> `212 passed in 35.94s` (exit 0); source inventory -> `185` rows,
  `719` references, `0` unmapped literals, detached list
  `['swarm-projection-rebuild']`; `python -m pytest -q
  tests/test_release_manifest.py` -> `23 passed in 25.25s` (exit 0);
  focused `py_compile` -> exit 0; `git diff --check` -> exit 0; the explicit
  untracked-file trailing-whitespace scan -> exit 0.
- Contract boundaries: the metadata validator checks source-bound references
  and preserves explicit evidence gaps; it does not execute runtime/provider
  fixtures, establish live transport, authenticate a provider, or promote the
  observation-compatible fleet envelope to qualification. No provider,
  authentication, account, install, Git mutation, release, or network action
  was exercised.
- Resources left behind: no external/provider resources or persistent test
  processes; changed files remain for the parent agent's integrated review.

U07/U08 rendered accessibility and recovery checkpoint (2026-09-12):

- Outcome: DONE for the accepted rendered-test packet. U07 adds the reviewed
  control-border contrast and narrow-screen one-column reflow correction. U08
  adds the reviewed preview journal-owner-loss/recovery journey; it does not
  claim supervisor lease/process handoff.
- Changed paths: `skills/summon/scripts/_workspace_page.py`,
  `skills/summon/scripts/test_workspace_accessibility_rendered.py`,
  `skills/summon/scripts/test_workspace_recovery_rendered.py`,
  `tools/release_manifest.py`, `.github/workflows/ci.yml`, and this handoff.
- Acceptance evidence: the focused U07 rendered test passed `1 passed in
  13.44s`; the focused U08 rendered test passed `1 passed in 40.04s`; the
  related page/static suite passed `78 passed in 88.32s`; release-manifest
  tests passed `23 passed in 24.55s`; affected-module `py_compile` and
  `git diff --check` passed.
- Verification limits: reduced CSS viewports are reflow checks, not browser
  zoom, assistive-technology speech, or native mobile-keyboard evidence. U07
  actual zoom/AT/mobile keyboard remain unverified. U08 is preview journal
  owner loss with passive/unknown effects, not a P2 supervisor handoff.
- Boundary evidence: tests used synthetic loopback fixtures only. No provider,
  authentication, account, spend, installation, Git mutation, release, or
  external-network action was exercised; no persistent process or external
  resource was left behind.

Release-evidence and CI truthfulness checkpoint (2026-09-12):

- Scope: provider-inert release tooling and workflow wiring only. The release
  runner now emits a fixed platform-scoped qualification record: non-Windows
  evidence labels Windows rollback/OS-privacy boundaries as `unavailable` and
  `incomplete`; it never converts those facts into a pass. `live_provider`
  remains an independent hard blocker.
- Hermetic UI evidence: rendered checks share one runner-owned external
  evidence directory. The two UI environment variables are never inherited
  from ambient state; an explicit browser executable is forwarded only after
  regular-file validation. No credentials, provider output, arbitrary paths or
  network settings are admitted to the child environment.
- CI: rendered checks now declare Playwright and Chromium prerequisites. The
  release-evidence job retains only source-bound JSON gate/manifest outcomes
  and synthetic rendered JSON/PNG evidence through a bounded artifact allowlist;
  raw logs, receipts, prompts and account data are not uploaded.
- Validation: `tests.test_release_gates`, `tests.test_release_manifest`, and
  `tests.test_release_outcomes` passed `56` tests; affected modules compiled and
  `git diff --check` passed. This was a bounded local check; no full release
  evidence run, provider/auth call, dependency installation, Git mutation or
  release was performed.
- Remaining gates: Windows qualification, live-provider evidence, and the
  broader integrated candidate/review matrix remain open. CI must continue to
  fail closed when the platform-scoped record, rendered tooling, or sanitized
  artifact set is missing or invalid.

Bounded release-evidence implementation follow-up (2026-09-12):

- Structural correction: `_platform_qualification` now returns from its own
  function, and rendered evidence is collected before a run-owned temporary
  directory is released. Empty rendered output remains invalid at manifest
  intake; no evidence is promoted by directory existence alone.
- Validation: `python -m py_compile` passed; `tests/test_release_gates.py` plus
  `tests/test_release_manifest.py` passed `42`; `tests/test_release_outcomes.py`
  passed `17`; `tests/test_release_gate_artifact_privacy.py` passed `69` with
  three pre-existing pytest-asyncio configuration warnings. CI YAML parsing,
  required prerequisite/artifact source assertions, and `git diff --check`
  passed.
- Scope and limits: these were bounded provider-inert checks only. The four
  test files pass when run as isolated bounded partitions; a single combined
  pytest invocation exposes existing cross-module fixture-order failures in
  the artifact-privacy partition and is not claimed as a clean integrated
  suite. Windows qualification, live-provider evidence, and a retained
  Windows candidate bundle remain open. No provider/auth/install/dependency
  installation, Git mutation, release, or network action was performed; no
  persistent process or external resource was left behind.

Public-docs truthfulness/privacy checkpoint (2026-09-12):

- Scope: bounded public wording only. The model snapshot now distinguishes
  editorial targets and snapshot observations from current service proof; direct
  OpenAI-compatible routing is documented as a supported `/chat/completions`
  subset; billing/usage text states that plan, credit, invoice, quota, and
  local no-cost behavior remain endpoint/account facts; and the retired Ox
  selector is clearly historical while the GLM route is an optional documented
  successor rather than a permanence or pricing promise.
- Historical labeling: the roadmap's prior v3.3.0 suite/gate and convergence
  notes are explicitly historical and do not certify the current working tree or
  provider lanes. The release owner still reruns current source-bound and live
  gates before publication.
- Changed paths: `skills/summon/SKILL.md`,
  `skills/summon/references/models.md`,
  `skills/summon/references/backends.md`, `README.md`,
  `docs/PROTOCOL.md`, `docs/SUMMON_PRODUCT_ROADMAP.md`, and this handoff.
- Boundary: no private paths, account names, credentials, prompts, receipts,
  telemetry contents, machine fingerprints, provider/auth/install/network/Git
  action, or legal/license text was added or changed. The worktree remains under
  the parent agent's integrated review; this packet does not authorize commit or
  release.
