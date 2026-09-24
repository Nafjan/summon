# Project lead status

Release execution, 2026-09-18 (owner authorized): the evidence-backed 3.5.0
workspace-preview candidate entered deliberate release execution. The canonical
version was reconciled to 3.5.0 across the four contract sources (plugin.json,
dispatcher, telemetry, MCP server) and the unique migration marker; the
changelog entry is dated 3.5.0 - 2026-09-18; the release-contract test's
live-version pin moved with the release while the historical 3.4.0 rollback
fixture pins remain untouched. The candidate was committed locally on
`codex/gemini-flash-3.8`; push, PR, and publication remain separate reserved
actions. U03/U07 native device observations remain outstanding release
evidence; the workspace-preview manifest boundary remains the machine gate for
the committed candidate.

Strict QA checkpoint, 2026-09-18: a fresh quiet-host aggregate capture from the
frozen current tree (evidence retained outside the repository, explicit
Chromium) completed all 27 fixed suites with 5,488 passed case outcomes, zero
failures and errors, and exactly the three reviewed Windows policy skips. All
eight gates pass except the intentional `live_provider` block for missing
authoritative evidence. Windows platform qualification is available, including
a combined-run browser-security pass (204/204) that closes the earlier
shared-host sensitivity, plus the complete 44-file rendered collection,
Windows silent-launch (27/27), roster/model identity (15+58) and
telemetry/privacy (46) evidence in the same source-bound packet. Both release
manifest profiles refuse the evidence solely because the tree is not a
committed clean candidate: `evidence was not produced from a clean Git tree`.
This is diagnostic workspace-preview evidence, not release acceptance; the
day's first capture attempt was correctly discarded after a mid-run source
edit tripped the structured-outcome binding, and one escaped guarded child
from that attempt remains cleanup-uncertain (kill denied, near-idle, no
contention effect). U03/U07 device evidence and the reserved final L06/L09/L12
actions remain open.

Strict QA checkpoint, 2026-09-14: the corrected aggregate was rerun with the
explicit Chromium executable binding and completed all 27 fixed suites. It
recorded 4,920 passed cases, exactly 3 reviewed-policy skips, and zero
failures or errors. Windows qualification is available; accessibility,
browser-security, fake-lifecycle, managed-install, migration/rollback,
model-identity and telemetry/privacy gates pass. `live_provider` remains
blocked because no authoritative provider receipt is available. The packet is
diagnostic rather than release evidence because the source tree is dirty and
the source version remains 3.4.0. Both stable and workspace-preview manifest
checks therefore still refuse source-bound acceptance. U03/U07 device evidence
and final L06/L09/L12 candidate actions remain open. After this checkpoint,
the source-focused release/profile suite passed 151/151 with external pytest
plugin autoload disabled, and RB-01 through RB-03 passed 19/19.
The current phase-0 silent-launch partition also passes 27/27, and the exact
roster/model identity checks pass 15/15 plus 58 unittest cases.
Nested chat/dispatcher/conversation runtime launch checks pass 43/43 with
external pytest plugin autoload disabled.
The retained U08 writer-loss/recovery case also passes 1/1 in 49.04 seconds
with the explicit Chromium binding and no skips or errors; it remains
provider-inert passive recovery evidence and does not close U03/U07 device work.
The full current `workspace_ui` registry passes 330/330 with explicit
Chromium; this closes the host-sensitive rendered/UI recheck only, not native
assistive-technology or mobile-keyboard acceptance.
The current browser-security and accessibility gates pass 204/204 and
159/159 respectively; telemetry/privacy passes 46/46. These provider-free
reruns do not close the clean-candidate or device-only requirements.

Strict QA/profile checkpoint, 2026-09-13: RB-01 through RB-03 remain closed on
the current source; the exact projection/recovery acceptance passes 19/19 with
no skips. The release-profile reconciliation is now explicit and tested:
`stable` requires every fixed gate, while opt-in `workspace-preview` permits
only the redacted `live_provider_evidence_missing` boundary and still requires
all other suites, gates, rendered/platform, privacy, migration and managed-
install checks. A fresh aggregate capture of this source recorded 4,913
passed cases, 3 reviewed Windows skips, 2 failures and 5 errors; its only
failures were shared-host browser/UI startup cases. The exact affected tests
pass in isolated runs with the runner's explicit browser binding (rendered
accessibility/zoom 2/2, workspace details 16/16, browser-security failure
recheck 203/204, and the failed conversation startup case 1/1 alone), so the
aggregate remains blocked and no partial result is promoted. The provider-free
preview profile is not a waiver for aggregate QA, clean source, device-only
U03/U07 evidence or L06/L09/L12 publication conditions.

Strict QA checkpoint, 2026-09-13: the release candidate remains blocked. The
provider-free partitions and focused gates have been rerun independently under
the current source: phase0 launch 27/27, phase0/phase1 1452/1454 with exactly
the two reviewed Windows POSIX-only skips, the workspace demo 24/24,
accessibility 159/159, browser security 204/204, and the release/privacy
contract checks 141/141. The intentional live-provider gate remains blocked by
policy. The required aggregate `tools/release_gates.py` capture then timed out
before producing a complete source-bound evidence packet; an earlier aggregate
attempt also showed host-contention failures that did not reproduce in isolated
runs. These results are evidence for investigation, not a release pass. The
source version remains 3.4.0, and no version bump, commit, publication, or
3.5.0 claim is permitted until one quiet-host aggregate capture completes with
zero unexplained failures, reviewed skips only, complete rendered artifacts,
privacy checks, and the live-provider block recorded.

Latest actual capture checkpoint: complete fixed workspace_recovery passes
72/72 with zero failures/skips/deselections, independently verified exact case
inventory, canonical output binding, source/Git stability and owned cleanup.
Full core captures timed out at 600 and 1,200 seconds; their 812/826 console
success markers are not accepted passing-case evidence. One bounded demo
profile passes, but audit attribution and four timing anomalies prevent a
causal performance conclusion. UI preparation is unactivated. See
L09_CANONICAL_CAPTURE_CHECKPOINT.md for scopes and retained limits. Readiness
remains 58/63 (92%); U03, U07, L06, L09 and L12 remain blocking. All actual
captures/diagnostics at this checkpoint are terminal; no new full run is
implied. The conductor reports a separate owner-directed review round.

Lead update, 2026-09-13 (publication retry correction): the Windows workspace
layout now retries only transient permission denial during the no-replace
publication call, with the destination-race and post-publication uncertainty
contracts unchanged. The focused layout contract passes 21/21 and the full
workspace UI fixture passes 59/59 after the correction. Browser security passes
204/204 and the repeated accessibility capture passes 159/159. A fresh full
provider-free capture still times out in the large workspace-core partition at
the configured 900-second child bound, so no full release evidence or 3.5.0
claim is made. The fixed release registry now gives the 24-case conductor demo
its own `workspace_demo` outcome while keeping every case required; the
remaining action is a fresh capture against that expanded registry.

The full capture also exposed two exact Windows platform skips. The banner
source path is corrected and passes in isolation; the remaining POSIX-only FIFO
and process-group cases now have exact `sys_platform=win32` review entries,
while all missing-tool and permission skips remain blocking.

Earlier lead update, 2026-09-13: the release-runner compatibility seam is now verified
by the focused release/Windows tests. The rendered delivery producer emits the
manifest's canonical hyphenated state-card filenames, and the current
`workspace_ui` partition passes 327/327 with all 44 required rendered files
collected. One normal-budget full capture reached the combined gates but kept
browser security blocked after a safe Windows `publish_permission` refusal in
the shared temporary hierarchy; the affected cases pass in isolation. This is
host-sensitive evidence, not a release pass. The ACL helper itself passed 20
isolated fresh-root probes, so no permission relaxation or helper rewrite is
warranted. U03/U07 AT/mobile evidence and
L06/L09/L12 final-candidate actions remain open.

Conductor recovery succeeded. All seven runtime corrections, five CI corrections
and the regression-retention increment are integrated. All eighteen selected
files and 335 runtime/test/configuration files independently match the reviewed
composition after newline normalization. R03/R10 are closed again; current
readiness is 58/63 requirements (92%, rounded). Remaining blockers are U03, U07,
L06, L09 and L12. L09's parameter-ID gap is now corrected: 248 literal-ID additions
across eighty tests preserve all non-ID AST and existing IDs. All 151 selected
files match the candidate independently collected through the existing identity
hook: 3,298 distinct cases and eleven complete mappings, zero identity/collection
problems, guards or source drift, and zero executed tests. The other 256 compared
runtime/test/configuration files remain unchanged. Actual canonical runtime and
native-device evidence are still required. The attempted core capture and
accepted recovery evidence are recorded in the newer checkpoint above.

Latest finishing work: all seven runtime corrections are privately composed and
independently pass 162 focused cases; the five CI corrections independently pass
138 cases with one known Git-HEAD-dependent case deliberately deselected. Separate
source review accepts the CI and snapshot changes. Desktop encrypted recovery and
capability polling have retained passing ordinary-host/browser evidence. See
FINAL_VALIDATION_TRIAGE.md and CI_EVIDENCE_ACCEPTANCE.md for exact scopes, earlier
failures and remaining limits. Shared integration and fixed-test retention now
match that accepted packet. Bounded recovery of three earlier completions found
unsupported completion acknowledgments, omitted by the app retrieval API; none
identified changed files, evidence, an active handle or a blocker. This is a
retrieval discrepancy, not proof of an execution failure. The subsequent bounded
native Astra recovery produced the independently checked integration above.

Current checkpoint, 2026-09-12: the authoritative disposition table records
58 verified and 5 blocking requirements out of 63 (92%, rounded). R03 and R10
were reopened and then closed by verified correction and canonical retention;
see FINAL_VALIDATION_TRIAGE.md. The denominator is unchanged. U08 recovery
is now closed after exact test/fixed/CI retention; U07 CSS/text retention also
matches after newline normalization. L02/E12
schema and roster acceptance are now closed after final metadata/test retention;
U03/U07 and L06/L09/L12 remain blocking. U01/U02/U04
task navigation and delivery presentation are now independently accepted with
exact integrated runtime/test and fixed/CI retention. R16/L08
documentation reconciliation is now accepted; final candidate gates remain. F17 is
accepted; earlier instructions naming it as unfinished are historical. The
conductor has integrated E12 replacement binding and the complete accepted format
inventory. Final structural mapping and rendered UI/release acceptance remain.

Native 200%/400% browser zoom now independently passes in 13.762 seconds,
without product edits or reduced thresholds. Full-size synthetic images, fixed
native bounds and all ten checked controls are retained; the conductor owns
test/fixed/CI integration. See U07_NATIVE_ZOOM_ACCEPTANCE.md. Actual AT/mobile
evidence remains open. The CI evidence review confirms the raw-byte digest and
strict qualification corrections are present. Independent execution passed 127
cases in a private copy; one Git-dependent case passed separately against the
checkout under read-only controls. CI_EVIDENCE_ACCEPTANCE.md records the remaining
Ubuntu diagnostic, native-zoom integration, numeric inventory, finite upload and
test-module cleanup corrections. No release gate was waived.

The replacement runtime is integrated and matches the accepted packet; test
logic and fixed-gate/CI retention match. The actual sealed-task approved-alias
remap and public parser-to-dispatch cases independently pass in 3.529 seconds,
with zero skips/guard violations, unchanged owned source and an empty home.
The unchanged alias passes before the replacement refuses. Remaining E12
in-flight/model/effort evidence is being reconciled in a bounded private lane.

All seven accepted public documents are integrated with exact text retention.
The synthetic README plan and create/open/inspect examples are independently
validated without server/provider launch. R16_L08_DOCUMENTATION_ACCEPTANCE.md
records closure and preserves final privacy/publication gates.

The fleet launch-evidence repair is integrated and independently retained: its
test exactly matches the accepted packet and runtime AST matches, with only
comment omissions. The earlier 69 independent cases remain valid; the conductor's
integration worker reports 138 focused cases and 23 registry checks passing.
Fixed-command/CI retention is verified. Full audit evidence and authority/fencing
remain intact; final structural metadata integration is pending.

The task-identity repair now has two actual-host regressions that fail before
and pass after. The lead's corrected v3 browser replay reaches selected-task
typed details, separately scoped artifact bodies and terminal guidance. It still
returns two failures in 38.907 seconds: Escape/focus recovery during a deferred
body request, and a fixture expecting not-running where the source correctly
reports unknown owner state. A bounded helper owns focus repair. After correcting
the owner fixture, the lead independently passed the terminal-guidance and
pending-action authentication-expiry/same-key recovery case in 59.623 seconds,
with zero skips or guard violations, unchanged owned source, empty synthetic
home and no bytecode. The focal-artifact case now independently passes after the
minimal focus repair in 12.486 seconds with the same controls. It verifies typed
navigation, separate body scope, Escape/opener return, late-response rejection
and changed-task filtering. Both cases and the runtime repairs are accepted for
conductor integration; full UI requirement closure remains distinct.

The complementary U04 seven-state browser case independently passes in 43.884
seconds with no skips/guard violations, unchanged owned source and an empty
synthetic home. Its seven individual state-card screenshots are retained; this
completes the bounded twelve-state worker presentation packet for integration.
No actual worker/provider receipt qualification or full accessibility is implied.

The final E12 test-only packet independently passes three cases in 2.254 seconds,
zero skips/guard violations/source drift/home files/bytecode. It verifies actual
post-start definition drift and passing unchanged controls around model/effort
changes. The conductor has the packet for final retention. The bounded U07 lane
now measures desktop-browser contrast, reflow and reduced motion; actual
assistive technology and mobile keyboards remain distinct unverified gates.

The two final E12 test files are now integrated and exactly match the
independently tested bytes, with fixed/CI retention. Only final structural
metadata integration remains before E12 closure. The U07 CSS repair and one
rendered case independently pass in 15.634 seconds with zero skips/guard
violations/source drift/home files/bytecode. Two minimal rules improve form
borders and 320-pixel goal/task reflow; keyboard, native focus pixels, drawer
and reduced motion pass at four CSS geometries. Integration is assigned;
actual zoom, AT and native-mobile evidence still block full U07.

Inventory restoration is independently retained: all eight test files are byte
identical to the accepted packet; all 184 metadata rows and top-level statements
match, with harmless statement ordering/whitespace differences. The conductor
reports 203 integrated passes and 23 release-registry passes. Final E12/fleet
structural reconciliation is accepted for integration: 185 rows/719 references,
zero literal omissions. The lead's nine-file run passed 210 cases; two failed
only because its owned copy omitted the public skill fixture. Adding that
unchanged fixture made both exact cases pass in 4.076 seconds. No assertions or
runtime source changed; prior failures remain recorded. Retain the ninth test
file and the accepted fleet fixture dependency before final L02 closure.

New user direction is recorded in SUBSCRIPTION_FIRST_ROUTING.md. Current usage
evidence remains advisory and live refresh coverage is narrowly version-bound;
universal quota tracking and subscription-first automatic routing are a follow-on
increment with explicit policy, freshness, eligibility and spend boundaries.
The existing 3.5.0 denominator and finish-line scope remain unchanged.

Local diagnostic review produced two bounded candidate corrections, specified
in TELEMETRY35_FINISH_LINE.md. The producer and timeout corrections are integrated
and independently pass 34 synthetic tests, including identity-forgery and audit
controls, in an owned copy; new producer-test canonical and CI retention are verified. No real
diagnostic records or local usage statistics belong in repository artifacts.

Remaining work is grouped into UI acceptance, compatibility/roster acceptance,
inventory retention, documentation, and final candidate evidence. Finish existing gates;
do not start another feature or advisory-review cycle. Release progress is
unchanged until the relevant requirement is independently closed. Installation,
commit and publication remain reserved actions, and 3.5.0 is not released.

Latest acceptance: the recovered E12 replacement-binding packet independently
passes 36 guarded cases and is queued for conductor integration. The corrected
in-flight detail-body expiry path passes five current page/Node cases. All 15
integrated Windows OS-privacy cases pass, including policy hardening failure
cleanup; unchanged tested source and fixed-gate/Windows-CI retention now close
L07 at the documented ACL-policy scope. Final candidate evidence remains L09/L12. The four
previously accepted stable-authority, reader-contract-v2, context and fleet
inventory patches have been recovered and handed to the conductor in dependency
order. Their combined owned-copy replay passes all 178 cases without skips,
failures or errors in 50.822 seconds, with unchanged source and no bytecode;
private patch-copy line endings were normalized before applying. The first
replay's incomplete copied fixture and shell-probing test reporter were corrected
without runtime changes. The two corrected remainder packets and a stale
literal-gap assertion correction now pass the full eight-file metadata suite:
203 passed in 41.674 seconds, no skips/failures, unchanged shared/owned source,
empty synthetic home and no bytecode. The combined 184 rows cover all discovered
source/literal pairs while preserving historical/runtime evidence gaps. L02
still requires canonical integration, all-test retention and the E12 optional
replacement structural mapping. Ten ordinary-host detail cases independently
pass in 58.789 seconds. The subsequent canonical reader consistency correction
and truthful deliberation outcomes independently pass eight actual-host cases
in 33.900 seconds with the same isolation/unchanged-source controls. Retain
accepted evidence; do not repeat discovery or unchanged tests.

Conductor recovery: several native Luna-Max turns ended with empty assistant
messages and no requested packet changes. The lead requested a bounded Sol-High
recovery run in the same designated conductor task, preserving all authority and
integration boundaries. This is a temporary routing-preference exception, not
served-model certification. The recovery packet is the canonical external-reader
consistency correction and its actual-host regressions; it must leave a sanitized
checkpoint in the implementation handoff for independent acceptance.
The recovery response exposed an earlier implementation hold still being
carried forward. The lead explicitly forwarded the user's later resume and
finish-line instructions, preserving reserved actions. The conductor is active
again and delivered the independently accepted canonical-reader packet. E12
integration and the existing alias-to-stored-task boundary are its next packet;
no new alias feature or provider qualification is implied.

## Historical checkpoints

2026-09-12 integration recovery: the conductor applied the two inventory
remainder packets without their accepted reader/context/fleet dependencies,
leaving 159 rows and three missing test files. Its eight-file run did not
collect; the two new suites returned 18 passes and seven failures. The lead
prepared one additive patch from that exact partial state to the complete
independently accepted 184-row/203-pass artifact. The conductor is authorized
to restore those supplied bytes; no architecture decision or provider action
is missing. Fixed-gate/CI wiring now names all eight files. Integration and
E12/fleet structural reconciliation remain pending; do not count failed or
uncollected runs as acceptance.

2026-09-09 L04 is VERIFIED-FOR-3.5. The lead independently passed all three
owned rollback branches in 105.117 seconds, verified exact test retention and
Windows CI/full-history/Node wiring, and passed the registry-consistency test.
The fixed release gate includes composed rollback; prerequisite logs omit
machine paths. Progress is 48 verified / 15 blocking of 63 requirements
(76%, rounded). F17 remains the primary product priority; final candidate
platform evidence and reserved release actions remain L09/L12. Earlier 75%
and L04-pending checkpoints below are historical.

2026-09-09 E07 is VERIFIED-FOR-3.5 at provider-inert preview scope. The lead
independently passed the actual parent-chat lifecycle in 6.53 seconds with all
252 watched source files unchanged, and accepted the current metadata correction
with 73 tests in 1.40 seconds. Combined prior accounting/context/workspace evidence
and canonical retention close E07. Progress is now 47 verified / 16 blocking of
63 requirements (75%, rounded). F17 public command authority/CLI is the next
product priority. L04 independently passed the complete ordinary/Kimi-override/
shared-junction owned rollback rehearsal in 105.117 seconds; exact test retention
and Windows CI/release wiring remain pending. No release,
provider or account qualification is claimed.

2026-09-09 parent-chat/E07 checkpoint: the updated native owned-child fixture
passes the focused lifecycle test and the adjacent chat partition, with
temporary-only environment roots, audit-bound process/file/network edges,
physical turn/attempt accounting checks, durable cancellation, and fresh
runtime recovery. The broader E07 and metadata partition also passes. These
are provider-free bounded results; E07 remains open pending independent
acceptance and the finite encoded-capacity correction. No provider/account,
installation, commit, or release qualification is implied.

2026-09-09 R15 is VERIFIED-FOR-3.5. The lead independently passed seven guarded
cases in two focused runs (five in 3.948 seconds; two in 1.541 seconds), both
without watched source drift. Durable v2 profile binding, exact v1 readability
and refusal, locked final checks, typed/public boundaries, canonical wiring and
version/recovery documentation are reviewed. Current progress is 46 verified /
17 blocking of 63 (73%). No new provider/account or release qualification.

2026-09-09 R15 initial candidate remains REVISE despite conductor-reported
three guarded passes and 111 adjacent resume/continuation cases. Source review
found that the expected profile state was sampled too late, at child load, and
missing registry evidence skipped the new check. The conductor is correcting
durable authenticated source/reservation binding and adding pre-load/missing
evidence coverage. Do not treat those narrower passes as R15 closure.
The parent-chat test-only packet is frozen with two desired failures at an
intentional command-admission boundary; no provider body ran or live child
remains. It is fixture-design evidence, not a production regression. Readiness
remains 45/63 (71%), with 18 blocking requirements.

2026-09-09 independent E07 follow-up found remaining encoded-bound gaps:
false can exceed null, independently admitted malformed metric states exceed
the probe's reported/complete states, and fractional timestamps can exceed the
numeric-magnitude exemplar's encoded width. These are source-derived findings,
not executed counterexamples. Frozen settlement bytes still enforce capacity
fail-closed; the risk is refusal after admission. The conductor owns correction
and focused counterexample tests, including oversized-metric refusal preserving
reservation and uncertainty. Current progress remains 45/63 (71%).

2026-09-09 R02 is VERIFIED-FOR-3.5 at the certified preview scope: previously
independently passing migration/refusal/fork evidence is retained exactly in
canonical tests and release checks, and public migration/capability/changelog
wording is reconciled. Candidate-provider qualification is explicitly gated,
not silently waived. Current progress is 45 verified / 18 blocking of 63 (71%).
E07 escaped-string/numeric-width correction remains under independent review;
the interrupted combined run and Windows setup concerns remain non-green.

2026-09-09 E07 capacity source review remains non-green: the finite validator
accepts arbitrary bounded-length attempt/provenance strings and finite floats,
but the reservation probe and four-outcome test use ASCII strings and integer
metrics. JSON escaping and float representation can exceed those example byte
widths. This is a source-backed proof gap; a new runtime counterexample has not
yet been executed by the lead. The conductor is assigned actual-encoder boundary
tests and a dominating bound or justified contract restriction. The interrupted
combined test run remains distinct from subsequently reported passing partitions.

2026-09-09 R02 public documentation now describes historical-v1 read-only
inspection, explicit v2 fork, certified versus candidate continuation and queued
steering. The lead reviewed the relevant README, Unreleased changelog, capability
and migration passages. R02 remains open pending clarification of the capability
document's remaining observation-authentication obligation for the certified
preview lane. The conductor is assigned the bounded E07 capacity, settlement,
replay, uncertainty and public-summary acceptance slice; no broad rerun or
provider qualification is implied. Progress remains 44/63 (70%).

2026-09-09 canonical retention gap resolved: the lead independently compared
the newly integrated migration and chat-dispatcher files against the approved
patches. All 318 migration lines match. After the conductor restored two
assertion-message strings, all 277 dispatcher lines also independently match.
Both entries are present in CI and the fixed phase0_phase1 release command.
The conductor reports 32 combined packet passes and 30 release-contract passes;
those counts are conductor evidence, not a new independent lead test run.
R02 public documentation reconciliation and broader E07 acceptance remain open.
Progress remains 44/63 (70%). This supersedes the retention gap below.

2026-09-09 independent retention correction: the approved 28-case migration
and four-case actual chat-dispatcher packets remain absent under their reviewed
canonical filenames, with no matching CI or release-manifest entries. The
conductor's report that packets were retained establishes preservation only;
canonical integration remains pending unless an exact alternative crosswalk is
provided. R02/E07 remain open and progress remains 44/63 (70%). The latest
reported broad run had 4095 passes, four skips, three failures and two setup
errors. Focused passes do not make that run green; the two Windows setup errors
still lack retained individual node IDs and deterministic combined evidence.

2026-09-09 documentation/metadata repairs independently verified: the actual
parser exposes 207 public flags and every one appears in the skill documentation.
The format-inventory suite passes all 68 tests in 0.99 seconds. Current
conversation/process producer constants are v2; this verifies current references,
not every historical reader mapping. The conductor reports a broader run with
4095 passed, four skipped and three warnings but remaining errors; it is not
a clean canonical release run. Exact failure/error counts and relative node IDs
are requested for the handoff, without private traces or evidence contents.

2026-09-09 broad-run checkpoint: the conductor reports a prior canonical
Phase 0/1 run with 1211 passed, two skipped, 40 failures and six errors. It is
failed evidence, not a release pass. Reported targeted repairs cover invocation
field compatibility, optional adapter keyword forwarding, historical refusal
before lock-file creation and the loopback view fixture. The lead source-checked
the first three: guarded resume still revalidates inside the source admission
lock before CAS. Targeted pass counts are conductor reports; the canonical
rerun remains pending and is not duplicated by the lead.

2026-09-08 current execution checkpoint: progress is 44/63 (70%), with
19 blocking requirements. R01 and R12 are now VERIFIED-FOR-3.5: all six files
from the three approved packets match their reviewed text and are retained in
CI/fixed release coverage. The lead independently ran the integrated entries:
12 passed in 18.34 seconds. R02 still needs public documentation reconciliation
and canonical retention of the corrected historical-v1 migration packet.

2026-09-09 the unchanged 28-case migration packet independently passes all
28 cases in 2.26 seconds on an owned current-source snapshot. Cancellation
ordering and the explicit historical-v1 fork are corrected. The affected R01/R02
packets also pass eight cases in 3.89 seconds after the executor/conversation
changes. E07 storage settlement still needs validated bound outcomes, duplicate
return, sufficient encoded capacity and actual public-v2 wiring. A bounded
actual dispatcher/guard acceptance packet is assigned for the new chat attempt
binding; passing direct guard tests alone do not establish that producer path.

The actual chat dispatcher packet is now independently approved: four cases
pass in 2.01 seconds, including real executor/guard/owned-body positive and
prompt/attempt/replay refusals. It needs canonical retention. Workspace E07
still needs the end-to-end turn journey: pure reproduction rejects the standard
estimator output while accepting contradictory estimate/contact controls.
The lead assigned a complete persisted-policy-through-public-summary acceptance
path, including actual encoded capacity and immutable outcome binding.

The latest pure accounting rerun accepts matching standard producer output,
refuses substituted payload output, and retains submitted/unknown-usage outcomes.
The new v2 demo currently proves admission and a no-submission settlement only.
Actual work/result or interruption, reopen/public summary and prelaunch capacity
remain the finite E07 acceptance target. Reported Windows-sensitive individual
reruns remain intermittent observations, not newly accepted reliability proof.

E07 now has workspace admission reservations and chat/
room v2 producers. Independent review found reservations lost from capacity
accounting on subsequent appends and missing comparison of actual dispatcher
payload to the authenticated chat guard. A guarded pure-state reserve comparison
reproduced the former; the latter is source-confirmed. The conductor has finite
corrections, plus cancellation version ordering and explicit legacy-v1 fork.
E07 accounting, L02 migration, R15 identity and F17 public commands remain
concrete implementation work; final UI/privacy/release requirements stay open.

Retention audit corrected a misleading crosswalk that named older tests as the
three approved packets. The lead supplied self-contained packet locations;
the conductor integrated the actual files. `R01_R02_R12_CANONICAL_RETENTION.md`
now records verified retention and the remaining R02 public-document obligations.

The revised L02 packet independently reports 23 passed / 5 failed in 2.65 seconds:
four cancellation no-mutation failures and one historical-v1 fork refusal. The
strict current/historical reader and recovery controls pass. E07's subsequent
reserve accumulator fixes following-append accounting, but source review rejects
releasing that reserve at prelaunch budget consumption rather than durable
accounting settlement. Both finite corrections remain assigned to the conductor.

## Earlier checkpoints

2026-09-08 L01 is VERIFIED-FOR-3.5 after finite entry/companion/legacy-ID
reconciliation. All 26 tracked-baseline command-family entries remain, workspace
is additive, and companion lifecycle/upgrade and historical-reader fixtures are
retained. This is source/evidence reconciliation, not a new install or test run.
Progress is now 42 verified / 21 blocking out of 63 (67%). F17 public commands,
R15 profile continuity, E07 accounting and final candidate gates remain open.

2026-09-08 F02 is VERIFIED-FOR-3.5 after current source/test/retention
reconciliation against the reviewed trusted-host authentication model and prior
independent ingress/operator acceptance. No new test run is claimed. Progress
is now 41 verified / 22 blocking out of 63 (65%). F17 public host/CLI disposition
wiring is a concrete remaining product gap, and R15 profile-state rotation is
independently reproduced. E07 remains the conductor's active implementation.

2026-09-08 the last R01 queued-version packet independently passes four cases
in 3.02 seconds against matching job/executor/qualification/emitter sources.
Actual post-reservation version changes refuse without changing ledger bytes or
claim uncertainty; public emitters preserve the correct refusal. R01/R02/R12
behavior is accepted at preview scope pending assigned canonical retention and
the R02 migration notice. E07 has active context-policy/conversation-economics
implementation; the lead supplied early schema-roundtrip and authenticated-
handoff feedback. R15 selected-profile rotation has a bounded synthetic review.
Progress remains 40/63 until the whole-row retention obligations are verified.

2026-09-08 current checkpoint: the jobs revocation correction independently passes
six cases in 3.76 seconds and its exact regression is retained in CI/release.
Candidate-lane refusal and explicit nonlaunching fork independently pass four
cases in 1.75 seconds; canonical retention is assigned. All four approved exact
adapter transport files match their retained packet and canonical references.
The conductor has resumed E07 workspace/conversation context-policy and submission
accounting. Final R12 operator-body and R01/R02 requirement crosswalk review remain
active. Whole-requirement progress remains 40/63 (63%); no release is claimed.

2026-09-08 the corrected R12 exact-boundary packet independently passes 15
cases in 9.131 seconds with 112 stable runtime files and six current transport
sources matching. E07 transient/capacity acceptance independently passes eight
cases in 1.85 seconds with six current sources matching. Both test-only packets
are approved for exact retention. The conductor remains on the jobs revocation
ordering fix; whole R12/E07 and the full 3.5.0 release remain incomplete.

2026-09-08 approved terminal/HTTP/grant and E12 turn/fork tests are verified in
source, CI and the release manifest. The lead independently passed all four
current grant/consent/manual-retry cases in 0.68 seconds, closing the reproduced
global fallback-allowance defect at that scope. Focused optional-transient/exact-cap
controls remain active. The conductor is now implementing the jobs revocation
ordering correction; broader E07 conversation/workspace work remains required.

2026-09-08 approved E07 terminal/HTTP and E12 turn/fork test files are now present;
the lead verified their functional contents match the approved packets (only
trailing blank lines differ). Canonical CI/release references remain pending.
The jobs launch-order review reproduced revocation-before-admission being ignored
for gate and main paths; two desired cases fail and four ordering controls pass.
A shared-lock-compatible concurrent regression is assigned, and the conductor
has the scoped authority correction ahead of new workspace feature work.

2026-09-08 E12 has bounded actual runtime acceptance: the lead reviewed and
passed four guarded turn-refusal/fork cases in 1.22 seconds with relevant source
matching. Synthetic history remains explicitly unqualified; no provider proof is
invented. Exact test retention is assigned. Replacement-name binding and remaining
task/model/effort journeys stay open; E12_ROSTER_ACCEPTANCE.md records the crosswalk.
The conductor's ACP newline correction is in source; exact byte tests are active.

2026-09-08 R12 adapter packet has implemented limits but remains REVISE after
independent exact-write review. A Windows ACP probe accepted an 89-byte ceiling
then wrote 90 bytes through the actual text-wrapper newline policy. The conductor
owns the correction; a native helper is preparing guarded final-byte boundary
tests. Existing three oversized-refusal controls are insufficient for full R12.
A separate native helper covers E12 actual conversation refusal/fork evidence.
No whole requirement has newly closed; progress remains 40 verified / 23 blocking.

2026-09-08 E07 evidence-capacity review found one additional candidate defect:
the real producer and retry dispatcher exhaust a global one-fallback allowance
on an already-authorized second outer attempt. Three consent integration controls
pass; the desired four-request recovery case fails. A portable guarded regression
is assigned to the conductor with a bounded-capacity correction. This does not
enable a new provider or billing path. Conversation/workspace source crosswalk is
delegated read-only while the conductor implements remaining transport limits.

2026-09-08 E07 public/version correction independently passes 11 guarded cases
in 1.04 seconds. Authenticated terminal sealing now passes the unchanged
five-case independent regression in 5.093 seconds with 112 runtime files stable
and zero guard hits; canonical test retention is assigned. Possible-record
coverage is independently corrected. Actual HTTP prelaunch accounting passes two
lead-run cases in 0.31 seconds with 36 current dependencies matching the reviewed
copy; exact retention is assigned. Standing-consent evidence capacity still needs
producer/adapter integration acceptance. The conductor
continues remaining R12 adapter limits while bounded review proceeds. Whole E07
remains open; requirement progress stays 40/63 (63%).

2026-09-08 F01 is VERIFIED-FOR-3.5 after coherent source/behavior/retention
review of all eleven required entities. The approved eighteen-case strict test
is retained unchanged, all 27 dependencies remain stable, CI/release/exact
inventory references are present, and the final reference check passes in 0.06
seconds. Artifact and Review/Decision acceptance remain retained. Progress is
now 40 verified / 23 blocking out of 63 (63%), superseding earlier checkpoints.
Other migration, projection, identity qualification and adapter gates remain.

2026-09-08 G02 is VERIFIED-FOR-3.5. Exact four-file council process test retention
and CI/release references are verified; the unguarded supplemental test is gone.
Checkpoint source matches the accepted 22-case protocol baseline and only the
separately reviewed writer/dispatch functions changed in council. Seven current
guarded actual-process cases complete the decoded-text boundary. Progress is
now 39 verified / 24 blocking out of 63 (62%). This replaces earlier 38/63
checkpoints below. Other R12 transports and live qualification remain open.

2026-09-08 E07 correction delta is held for a confirmed cross-lane regression:
its accounting patch also removes the accepted council exact-text intake.
The lead verified those hunks and directed the conductor to regenerate a
scoped replacement; it has not been approved for shared application. Independent
accounting/privacy/version review continues on owned copies. This demonstrates
why accounting-only passes and apply-check success cannot authorize integration.

2026-09-08 council decoded-text/process correction is independently approved:
seven cases pass in 5.921 seconds with reviewed pre-import audit fences and
unchanged functional assertions. All 112 copied runtime files stayed stable;
owned fixtures/children are cleaned. Exact four-file canonical integration is
assigned to the conductor. Together with the approved F01 strict packet, this
leaves concrete retention checks before reconsidering the two requirement rows.
E07's frozen correction is now under split independent review of publication/
version boundaries and accounting/physical-request semantics. Neither a frozen
patch nor reported focused counts constitutes acceptance.

2026-09-08 F01 strict-entity test-only integration is approved: the lead reviewed
the complete packet and independently passed 18 cases in 8.38 seconds, covering
plan schema/task identity, worker registration/incarnation and actual durable
command source boundaries. All 27 dependencies match, source drift and guard
hits are zero, and owned fixtures are cleaned. No runtime defect was found.
The conductor must retain the approved file and exact canonical fixture
references before the final coherent F01 closure check. No percentage promotion
is made from an isolated packet alone.

2026-09-08 approved artifact acceptance integration is verified: the complete
four-case guarded test matches the reviewed packet after line-ending/trailing
whitespace normalization, all nine runtime dependencies remain unchanged, and
CI/release/inventory references retain it. The current schema-reference check
passes in 0.08 seconds. The temporary substitute test was replaced; this closes
the bounded artifact identity/reopen/rebuild packet. Three direct F01 strictness
checks remain under isolated acceptance; no whole-row percentage change yet.

2026-09-08 F01 semantic mappings are independently accepted: Artifact now maps
actual swarm publication/reconstruction; workspace assessments and next-lane
decisions have distinct rows, preserving routing/deliberation mappings. Two
current schema/format reference validators pass in 0.72 seconds with four
watched files stable. Canonical retention of the approved artifact packet and
three directly audited strict-entity tests remain. E07's stopped worker has an
explicit restart direction under its existing bounded correction grant; a new
live handle is not yet verified. R12 positive decoded-text acceptance is running.

2026-09-08 artifact identity correction is independently approved for test
integration: all four public publication/replay/reopen/rebuild cases pass with
zero failures, skips or guard hits. All nine runtime dependencies match the
reviewed packet, shared source did not drift, and owned fixtures were cleaned.
The conductor must retain the portable acceptance test and correct the actual
Artifact and workspace Review/Decision inventory mappings before F01 closure.

2026-09-08 historical claim scalar correction independently accepted on shared
source: the lead inspected all three checks and passed the retained 18-case
guarded acceptance packet in 4.18 seconds against a fresh current-source copy.
All 230 copied source files remained stable; fixtures were cleaned and no live
handles remain. Boolean generation, numeric provider contact and object-valued
child creation time now refuse. Canonical test/inventory retention is verified:
the original 18-case file differs only by a trailing blank line, CI and release
commands retain it, and the current format-reference validator passes in 0.84
seconds. This bounded packet is closed. Historical producer equivalence is not
claimed. F01 mapping and R12 exact decoded-text correction are next; E07 remains
subject to the retained accounting and publication-privacy findings.

2026-09-08 current correction evidence: a three-case owned-record probe confirms
the frozen E07 background writer mutates future and boolean schema versions;
the supported v2 control also updates. Process/network guard hits are zero,
source is unchanged and temporary records are cleaned. E07-VERSION-01 is now
part of the existing accounting correction, alongside the retained semantic
failures and secondary-request boundary.

The conductor added council prompt-file transport, but its new long-prompt test
stubs the child runner. Independent actual-process acceptance is running in one
isolated test-only lane, including bounded refusal and cleanup. No R12 closure
is inferred from mocked child delivery or passing protocol-only tests.

2026-09-08 G02 shared protocol/public-entry integration is independently
accepted: all four runtime files match the reviewed snapshot correction and
the current 22-case guarded suite passes in 14.14 seconds with no source drift
or guard hits. Canonical compatibility commands and both checkpoint/context
format mappings are retained. A separate R12 source review identifies the
intermediate long-prompt argv boundary; the real long-context child journey is
still required before release. See `R12_TRANSPORT_CLOSURE.md`.

The Review/Decision test-only integration matches the approved packet after
line-ending normalization and is retained in the canonical workspace-core
suite. Two current inventory-reference validators pass in 0.71 seconds.
Its machine entity row still maps routing/deliberation rather than workspace
assessment/next-lane schemas; the already-assigned F01 mapping correction
remains open. Adding a workspace fixture does not fix that semantic mapping.

Frozen E07 source still contains the three previously reproduced accounting
semantic defects, contradicting the reported closure. The conductor has a
bounded restart grant to correct these and the second-HTTP-submission seam.
Keep the patch isolated. Whole-row progress remains 38 verified / 25 blocking
out of 63; the source matrix count was independently checked this turn.

2026-09-08 Review/Decision acceptance approved for integration: the lead's
independent guarded run passes 33 cases (12 new, 21 existing) in 1.13 seconds.
Strict version/authority refusals, immutable identities, exact replay, scoped
evidence and persistent holds are covered without changing runtime semantics.
The producer is synthetic event construction; actual conductor admission and
persisted reopen remain separate evidence requirements. Integrate this packet
with the conductor's F01 mapping/artifact work; whole F01 stays blocking.

2026-09-08 encrypted-version integration accepted: shared test matches the
approved sixteen-case packet after line-ending normalization. Canonical
workspace UI, browser-security and accessibility commands retain it; the gate
runner derives those commands from the release manifest. Corrected format
mapping separates storage slot, outer payload and AAD evidence and records
real remaining limits. The current-manifest-reference test passes in 0.56
seconds with three watched files unchanged. No browser runtime changes or
whole-L02 claim accompany this test-only integration.

2026-09-08 G02 snapshot correction is independently approved for integration:
21 focused tests pass; eight actual guarded workers verify nine predicates.
Replacement after owner-held validation preserves the changed external file
but downstream consumes only the original checked content. Historical carry,
pre-validation refusal and fresh/continuation deadline controls still pass.
No reviewed source drift or live handles remain. Whole G02 still needs the
actual public-main lifecycle and integrated fixed-suite retention. The conductor
may integrate the corrected packet and proceed to historical scalar validation
and artifact identity; E07 remains with its existing worker. This supersedes
the open snapshot-race status below without promoting the whole release row.

2026-09-08 latest independent checkpoint supersedes the earlier G02 failures
below: actual old-producer/old-reader/new-reader dispatch counts are 5/0/0;
pre-validation body and attempt/contact mutations refuse without dispatch;
fresh and continuation clocks both retain 90 seconds after ten seconds of
setup. The frozen 19-case focused suite passes. One remaining race is reproduced:
replacing round-one result and report summary after under-owner validation
reaches all three later stages. G02 stays isolated until continuation consumes
the same validated snapshot. No G02 closure is claimed.

Encrypted pending-state acceptance is independently approved for integration:
16 actual page-script/WebCrypto cases pass in 1.16 seconds, with zero guard
hits, all sixteen owned Node children exited and runtime fixtures cleaned.
Three inspected dependencies match shared source. Unsupported numeric record
and AAD versions preserve ciphertext and operation identity without submission;
explicit reauthentication recovers compatible work. This is test-only evidence,
not rendered-browser, host-key or historical-producer qualification. Whole-row
progress remains 38/63 (60%); the conductor owns fixed-suite integration.

2026-09-08 migration review produced three concrete correction packets. G02's
revised continuation clock passes the actual registry control, but ordinary
historical carry repeats all five stages, changed r1 evidence is accepted, and
fresh pause still records a deadline excluding setup time. Historical job-claim
acceptance independently reproduces 15 passes / 3 malformed-field failures.
Artifact characterization proves that duplicate new-frame publication is
accepted by ordinary reopen but refused by strict reconstruction. All findings
have scoped conductor handoffs; root helpers completed with no live handles.
The conductor reports its separate support check complete and E07 worker active.
The F01 crosswalk now identifies eleven required entities and the specific
mapping/acceptance work, without making unrelated L02 formats prerequisites.
No whole row is newly verified; progress remains 38/63 (60%).

2026-09-08 browser integration independently verified: 104 shared-source tests
pass in 3.42 seconds with eight watched files unchanged. This includes 36 page
and actual-script cases plus 68 format-inventory cases. The new acceptance file
matches the approved packet; existing page tests differ only by line endings.
The page has two inspected integration differences: equivalent listener order
and reporting the retained current revision for an unchanged snapshot.
Canonical workspace UI/browser-security/accessibility commands retain the new
test, and CI executes the canonical release registry. Full valid-v1 shape and
rendered-browser qualification remain open. No whole requirement is promoted.
G02 corrections remain isolated; E07 implementation may proceed independently.

2026-09-08 G02 independent review changes the next action to correction, not
integration. The public command path failed actual parser/mode checks, and a
synthetic ten-second roster-validation delay left a 100-second watchdog when
the original deadline had only 90 seconds remaining. The conductor reports a
CLI correction; ownership/carry review remains active. Findings and acceptance
criteria are in `G02_CHECKPOINT_ACCEPTANCE_REVIEW.md`. Browser correction
integration can proceed independently. Progress remains 38/63 (60%); no whole
requirement or release gate is newly accepted from these candidate results.

2026-09-08 browser schema correction independently accepted for conductor
integration: 36 cases pass in 2.43 seconds (23 new actual-script cases and
13 existing page tests). Missing/future/malformed schema and unreadable success
responses refuse before current/pending views or recovery records change.
Compatibility pause preserves ciphertext and explicit retry restores valid v1
operation. Review also caught and corrected a new 503-response ordering issue;
JSON/non-JSON transient errors now retain bounded automatic reconnect behavior
without parsing or exposing their bodies. Full valid-v1 projection-shape and
rendered-browser qualification remain unproved. No shared correction is claimed
until conductor integration and fixed-suite retention complete.

2026-09-08 three-packet integration independently verified: 94 tests plus
20 subtests pass against shared source in 10.34 seconds. Four files exactly
match their reviewed candidates; the background test differs only by a leading
blank line. Fixed phase0/schema commands and CI now retain both new test files.
This completes the scoped background/OpenCode/format-tool integration queue.

A new actual browser stale-schema blocker is independently reproduced: four
missing/future view-schema cases fail and two supported controls pass in
0.98 seconds. Awaiting-confirmation operations can lose their recovery storage
and encrypted draft when that invalid view is installed. A bounded isolated
page correction is active; no correction is accepted yet. Existing U14 tests
remain evidence for their valid-view presentation scope, while full L02/UI
compatibility remains blocking. Release progress stays 38/63 (60%).

2026-09-08 format inventory candidate independently accepted: 73 metadata/schema
tests pass in 1.30 seconds after final source review. The new tool maps 52
audited variants and explicitly reports 114 uncovered source/literal pairs,
including two candidate G02 pairs, 15 manual formats and five missing producer
definitions. It preserves the existing entity inventory API. This corrects
coverage visibility; it does not close the full migration requirements.

Current integration queue has three independently approved packets: actual
background publication tests, OpenCode missing-counter correction and the
format inventory tool/tests. The conductor owns integration, fixed-suite
retention and focused shared-source acceptance. Native helpers are complete
with no live handles. G02 remains a separate incomplete isolated candidate.

2026-09-08 E07 parser candidate independently accepted: full two-file review
and focused execution pass 18 tests plus 20 subtests in 1.22 seconds. OpenCode
`tokens`/cache snapshots retain reported valid values and explicit zero without
inventing missing counters, merging steps or inferring totals. Existing terminal
classification and raw usage fallback remain unchanged. Both shared baselines
match the reviewed packet and patch applicability passes. Conductor integration
is pending. The acceptance run uses explicit credential-fenced fixtures; an
earlier helper batch lacked that proof and is not the acceptance basis.

E07's broader gap is now an implementation brief, not merely a request for more
coverage: `E07_SUBMISSION_ACCOUNTING_BRIEF.md` defines physical-attempt
attribution, estimate scope, unknown usage/contact and explicit task context
policy. The separate format inventory correction is underway in an isolated
native lane. G02 remains incomplete in its isolated conductor candidate; shared
partial edits are preserved. No whole requirement has been promoted and the
matrix remains 38 verified / 25 blocking / 63 (60%).

2026-09-08 actual background publication packet independently accepted: three
tests pass in 8.12 seconds through the public detached parent, frozen child
bundle and actual terminal/private publication. The restricted local PATH shim
uses real profile admission; exact-model mismatch and background `--out`
refusals remain intact. No private launch marker or known launch digests appear
in checked public outputs. Conductor integration and fixed-suite retention are
pending. Missing version evidence remains unqualified; this does not close
governed resume, chat producer journeys or the whole R01/R02 requirements.
The matrix remains 38 verified / 25 blocking / 63 (60%).

2026-09-08 emitter correction is integrated and independently verified. All four
source/test files exactly match the reviewed packet; fixed-suite retention is
confirmed. The five actual-main publication cases pass against shared source in
5.42 seconds, supplementing the earlier 53-case independent candidate and the
conductor's 53-case integration results. Canonical route intake and public
`--out` privacy defects are closed at this scope. Real background parent/child
publication and remaining chat producer journeys still require their evidence;
ordinary missing version observations remain unqualified.

2026-09-08 release disposition update: U14 is VERIFIED-FOR-3.5 for the already
adopted minimal workspace preview. Integrated tests, native rendering and both
architect/conductor route inspection establish the passive shell, explicit
foreground host, sanitized snapshot owner and absence of an implemented queue
evaluator/background supervisor. P2 supervision and full U07/U08/L09 gates remain
open. `U14_OWNER_PRESENTATION_ACCEPTANCE.md` records the requirement-by-requirement
basis. The fixed 63-row matrix now has 38 verified and 25 blocking rows (60%).

The two emitter corrections pass 53 independent isolated tests in 4.43 seconds
and are approved for conductor integration. This does not yet claim shared-source
correction, whole R01/R02 acceptance, installed qualification or a new release.

2026-09-08 frozen integration batch independently accepted at scoped level:
140 tests pass in 85.87 seconds on Python 3.12 with external pytest plugins,
telemetry, bytecode and cache disabled. This combines the integrated owner UI,
preview entry, council identity/generation guards, historical deliberation
public readers and the real-observer gate/main control seam. Reviewed production
and fixture differences are accounted for; fixed release commands retain the
new tests and release-artifact privacy tests. The conductor separately reports
135 focused passes, ten lifecycle passes and 35 manifest/gate passes.

Next critical packet has two independently reproduced failures in actual public
dispatch publication: canonical executor `backend_type=cli_agent` is rejected by
the continuation writer's `cli` check, and foreground `--out` serializes the
private launch marker before terminal emission strips it. The real argv/main/
executor/emitter fixture reports two failures and one passing model-mismatch
control independently in 2.51 seconds. All inputs are synthetic. An isolated
native correction is in testing; no shared runtime correction or release
acceptance is claimed yet. Preserve strict route/model checks, historical
private-sidecar schema and private evidence until sealing. The five-item batch
is complete; do not reopen it or delay this concrete publication correction.

2026-09-08 integrated reliability/evidence batch: the conductor integrated the
L01 preview labels, L02 ordinary swarm history guards, real discovery/install/ACP
collector callbacks, CI registry correction, transport wording and strict release
artifact consumers. Independent shared-source checks pass all three preview
cases, all 42 swarm boundary cases, both discovery skip/leak cases and the full
CI registry equality case. The conductor's combined swarm regression reports
119 passes; its integrated release privacy/outcome/manifest/gate partition
reports 121 passes. Independent Python 3.13 privacy/outcome execution passes
86 cases in 4.33 seconds. These are scoped integration results, not whole L01,
L02, L09 or release acceptance.

The copied-record privacy correction rejects arbitrary diagnostic/unknown
fields in both gate artifacts and suite wrappers at intake and final checking,
including recomputed-digest attempts. Valid producer shapes, blocked outcomes,
historical sanitized previews and projection equality remain tested. Native
review/implementation helpers completed their bounded grants and have stopped;
no provider or cross-vendor approval is claimed for this packet.

The earlier 436/436 workspace and 15/15 outcome captures were independently
read and validated. Both are internally valid historical checkpoints and no
longer match the integrated source. Do not recapture the whole release after
each edit. The conductor now owns the actual synthetic observation-producer
bridge into governed job/chat continuation. Batch planning updates before the
eventual final candidate freeze. Release progress remains 37 verified and 26
blocking of 63 (59%); reserved installation, commit and publication actions
remain separate.

2026-09-08 L01 labeling candidate independently reviewed: three actual CLI/entry
checks pass in 6.25 seconds against the isolated final patch. Workspace help
explicitly says preview; create/inspect/ready/lifecycle output adds `preview: true`
while preserving original fields and authority. Inspect remains byte-preserving;
the foreground test starts/stops the local host and keeps bootstrap material
out of public output. The conductor has conditional integration approval for
the four-file packet and owns fixed-registry retention. These are isolated
candidate results until integrated; broader L01 historical compatibility and
L02 schema migration remain blocking.

The same three final-patch preview checks also pass independently under
Python 3.13 (4.86 seconds), matching one configured release Python version.
This supplements the Python 3.12 result; it does not establish the full
Windows/Ubuntu or Python 3.10/3.13 release matrix.

2026-09-08 L02 defect reproduced: ordinary swarm status and worker registration
accept a checksummed future preparation schema or mismatched preparation run ID.
The retained `test_swarm_migration_boundary_acceptance.py` reports 6 failures
and 2 passes in 0.96 seconds on Python 3.12. Registration accepts both complete
and torn invalid histories; unknown-event controls correctly refuse before
ownership and repair. The defect is separate from the accepted L03 reconstruction
preflight. The conductor owns a shared ordinary-reader validation correction,
preserving supported recovery and checking again under ownership. L02 was
already blocking; this converts uncertainty into a concrete failure rather
than changing the release denominator or reopening L03.

2026-09-08 independent verification update: the corrected real Windows
descendant-cleanup acceptance and transport-refusal file pass together
(5 tests, 8.23 seconds). Successful Job Object attachment, positive child start,
delayed leak detection and host-platform rejection are verified at their bounded
synthetic scope; whole R01/R02/R12 acceptance remains open. The L09 helper
completed its 25-minute isolated implementation stage and packaged a 12-file
candidate. Independent Python 3.12 / pytest 9.1.1 execution returned 27 passed
and one test failure: the test incorrectly assumes absent external subtests
plugin means absent fixture, although pytest now supplies it internally. Actual
capture retained the skipped subtest and redacted its reason. Correct this
feature-detection test, preserve the latest registry during integration, and
qualify explicit parameter identities before L09 acceptance. No release gate
is promoted by this checkpoint; current progress remains 37/63 (59%).

2026-09-08 execution update: the bounded L09 implementation helper is now
confirmed running in an isolated source copy; the conductor retains integration
ownership. Parsed fixed commands include both observer files and workspace
reconstruction acceptance. A preliminary scan of 76 README/changelog/docs
Markdown files found no Windows user-directory, local project-drive or Unix
user-home path patterns. This is a narrow preflight, not L06 final privacy,
credential/receipt detection or staged-candidate acceptance. U14 still needs
visible owner state and honest route-specific queue-evaluation guidance.

2026-09-08 current release disposition: **37 verified and 26 blocking of 63
(59%, rounded)**. L03 is independently accepted after 19 current reconstruction
tests, including actual workspace journal/history/uncertainty preservation.
`L03_ARCHITECT_REVIEW.md` records the exact requirement and limits. The combined
launch-observer gate now passes 11 cases, including post-callback cancellation
and deadline refusal before spawn. The subsequent typed policy-refusal regression
also independently passes (1 case, 10.72 seconds); the conductor's combined
12-case result is supported by matching handoff source identities. Remaining
producer resource/isolation and governed integration gates stay open. Earlier percentages below are
historical checkpoints. The designated conductor remains active and owns
integration and the bounded parallel L09 scheduling grant.

2026-09-08 producer review checkpoint: the executor-connected observation
producer is present but not accepted. Independent focused execution returned
5 passed and 1 failed; deterministic review also reproduced version suffix
collapse and old-version/new-executable binding. LO-01 through LO-05 in
`RELEASE35_IMPLEMENTATION_HANDOFF.md` are assigned to the conductor for closure.
A bounded native read-only helper completed the L09 skip-evidence review;
`L09_SKIP_EVIDENCE_CONTRACT.md` records the queued implementation direction,
custom-harness accounting gap and required independent review. Task status
observations timed out, so no restart or terminal-state inference was made;
the correction and follow-on handoffs were successfully delivered. Overall
release progress remains 36 verified and 27 blocking of 63 (57%).

2026-09-08 current implementation focus: complete R01/R02 trusted launch
observation/material production and the remaining compatibility/interruption
matrix, then the next requirements in the conductor handoff. Independent
acceptance now covers public job and chat revalidation, supported synthetic
legacy chat migration through later continuation, authority/input refusals,
durable revocation ordering and four interrupted-publication recovery cases.
The UI offline-hint defect is corrected, with six page-script cases and a
rendered synthetic shutdown/preserved-view check. These are bounded synthetic
and local-browser proofs; they do not certify installed/live providers.
`R01_R02_MIGRATION_INVENTORY.md` and `U08_BROWSER_ACCEPTANCE.md` distinguish
current acceptance from intermediate defects. Fixed release commands now cover
all 16 unique retained acceptance files, including reconnect acceptance.
Existing registry/command/CI consistency tests pass, but full
candidate-bound release evidence is still outstanding. F15 stays frozen.
Release disposition remains 36 verified and 27 blocking of 63 (57%).

2026-09-08 final F15 delta accepted: explicit recovery-delivery selection is
verified by the full demo (17 tests plus 13 subtests), affected partition
(86 tests), and corrected distinguishable-parent lineage node (1 test).
`F15_ARCHITECT_REVIEW.md` records exact terminal evidence and limits. F15 is
frozen; the conductor proceeds with R01/R02 capability migration. Progress
remains 36 verified and 27 blocking of 63 (57%).

2026-09-08 acceptance update: F15 is VERIFIED-FOR-3.5 at the bounded
provider-free preview scope. `F15_ARCHITECT_REVIEW.md` records source review,
current full-demo/core/coordinator gate evidence and limitations. The release
disposition is now 36 verified and 27 blocking of 63 (57%); L09 and the other
release requirements remain open. R01/R02 capability migration is next.
The checkpoint narrative below records the preceding integration history.

2026-09-08 F15 integration checkpoint: independent provider-free checks on
stable six-module source now prove first durable reservation consumption,
clean reload by a fresh coordinator, typed duplicate refusal, reconstruction
of consumed state and launch-intent provenance with later records retained,
pre-launch cancellation/lease-expiry refusal without a launch intent, and
sequential refusal of a different task when the only execution slot is already
reserved. These checks spawned no worker and contacted no provider. They are
bounded checks, not simultaneous multi-process contention or full journey
qualification. These checks are now retained in
`test_workspace_reservation_acceptance.py`. Its expanded 10-case suite passed
independently in 39.71 seconds, adding missing/revoked/rotated-grant refusal,
fresh Python process reload, and same-process thread contention followed by
explicit post-contention capacity refusal. This does not prove simultaneous
separate-process contention or a complete worker journey.

The fixed release commands now include the rebuild and transport-admission
test files and the new reservation acceptance file. The architect independently
passed 33 release-registry/manifest tests in 30.92 seconds. That verifies registry
behavior, not execution of every registered command. The conductor reports a
108-test affected partition and 48 acceptance/schema/release checks; these
overlapping counts are not additive release evidence. The complete demo now
has verified terminal evidence: 17 tests and 13 subtests passed in 717.30 seconds.
The earlier full run exposed a recovery projection defect: a preserved parent
delivery hid its pending successor because both shared a logical message ID.
Current projection prefers the pending successor without rewriting the parent.
Independent synthetic checks confirm parent/successor insertion-order parity;
the conductor's real endpoint and same-task recovery checks also passed.
Two stale assertions were corrected to match refusal before worker creation
and sequential authorized message handoff. Separate two-worker concurrency,
overlapping side work and two-iteration assertions remain required and passed
in the full demo. See `F15_CURRENT_EVIDENCE.md` for the conductor checkpoint.
F15, L09 and release counts remain unchanged while the affected fixed gates
are rerun against current source; older green subsets do not qualify this tree.

Supervisor context exposure is now being integrated with a durable pre-write
budget decision. Independent review reproduced two intermediate contract gaps:
empty-selection control accounting omitted delivery message costs, and an
offered inbox message could receive a positive execution-slot decision. The
current evaluator distinguishes inbox sources and refuses positive-slot or
mixed inbox/task requests; all 10 tests in `test_workspace_budget.py` passed
independently under Python 3.12 in 0.52 seconds. Source now also checks current
endpoint owner, lease, grant and content inside coordinator consumption.
The affected `test_workspace_budget_integration.py` and
`test_workspace_transport_budget.py` files also passed independently: 30 tests
in 105.00 seconds under Python 3.12, with plugin autoload and telemetry disabled.
These bounded findings do not establish full supervisor integration acceptance.
Retained rate/stream/oversize, stale-authority, duplicate/restart and actual
worker/supervisor/recovery journey evidence remains required. No release row
is promoted on the basis of this evaluator-only verification.

Current checkpoint, 2026-09-08: work resumed under the owner's instruction.
The conductor completed RB-01 through RB-03; the architect independently
verified 18 focused rebuild tests and 194 affected tests plus 12 subtests,
with source matching the handoff. The single bounded exact Fable review
approved RB-01 through RB-03 and the preview boundary on inspected source.
RELEASE35_SCOPE_DECISION.md records the adopted correction to F15's
preview/live boundary and the verified ungated synthetic demo/recovery
callers. Those promised journeys remain in scope. The conductor now owns
ordered multi-message admission, derived budget state, durable provenance,
all preview launch/recovery integrations and the missing fixed-gate wiring.
Independent failure/concurrency verification remains required. Release disposition stays
35 verified and 28 blocking out of 63 before the F15 acceptance recorded above.
Earlier checkpoints below retain their dated evidence and may describe
superseded implementation gaps.

Updated: 2026-09-06. The owner authorized the general direction/goals and designated the lead as product lead and main architect, with Fable via Summon as the principal consultant and skeptical second reviewer. The lead retains decision ownership. Earlier per-slice authorization blockers below are historical and superseded by LEADERSHIP_AUTHORITY.md. The completed P0 fixture/contract slice passed independent verification: 189 tests, no exclusions or source drift during the run; all five lead findings are closed. DEV-WIN-01 also passed independent verification: final 47 Windows/release/spawn tests, no skips or source drift, after a recorded cold-cache fixture correction. FABLE-WIN-02 approved the completed bounded milestone with no blocking findings. FABLE_WINDOWS_APPROVAL.md records six non-blocking follow-ups. The R01 additive contract remains accepted within its bounded scope. Hourly monitoring remains configured.

The current R15/F02 provider-free checkpoint found source-backed coverage for named-account isolation, profile-state rotation, authenticated approval bootstrap/expiry/revocation, worker instance/epoch/lease/grant scope, stale-source refusal, and an activation-time recheck that explicit Claude/Codex profile homes are dedicated directories outside the dispatch tree. The follow-up corrected synthetic profile homes to stay outside each dispatch tree and added a regression proving that rotating the selected Claude profile home after reservation refuses before capacity consumption or provider contact; the activation, dispatch, and account-profile focused files passed 51, 55, and 19 cases respectively (125 combined). Earlier account/fleet/runtime/transport and adjacent fleet/approval/workspace/conversation evidence remains recorded below. The runtime validates the extended workspace admission contract, including the observational budget-preflight shape, without treating it as execution authority. This remains provider-free evidence: hostile same-user isolation, every backend's multi-account rotation, durable worker bootstrap/recovery, and live adapter qualification remain open. R15 and F02 remain PARTIAL/BLOCKING. No provider, install, commit, release, or credential action occurred.

The bounded E12 roster increment adds a provider-free fail-closed check for explicit effort pins: routes without a reviewed effort transport now refuse before provider contact with typed `effort_unsupported`, `provider_contacted=false`, `result_usable=false`, and `retryable=false`; ambient/default effort remains best-effort. The focused model-routing suite passed 14/14. E12 remains PARTIAL/BLOCKING because full rename/removal/alias-remap and in-flight rebind/fork drift coverage, plus authoritative served-model qualification, remain open.

The bounded E12 provenance follow-up revalidated the retired Ox selector tombstones and separate GLM successor, editorial-catalog versus served-model separation, tri-state exact-model proof, and unsupported explicit effort in 16 focused tests plus 3 subtests; the selected discovery identity/effort subset passed 7 tests. A new provider-free resume regression covers model and effort drift and refuses before successor launch (4 focused resume-drift cases passed). No catalog/routing source change was needed. E12 remains PARTIAL/BLOCKING because complete rename/removal/alias-remap, broader in-flight rebind/fork coverage, and authoritative served-model qualification remain open. No provider, credential, install, commit, or release action occurred.

The bounded E07 usage increment makes the advisory projection fail closed for live observations that lack successful execution/provider-contact evidence or a valid account-scope proof. Invalid live observations are rejected before they can affect advisory counts; the focused provider-free usage suite passed 51/51. E07 remains PARTIAL/BLOCKING while measured-versus-estimated semantics, cache/opt-out coverage and live qualification remain open.

The provider-free `browser_security` gate is now terminally green for its fixed five-file scope: 116/116 passed in about 97 seconds with plugin autoload disabled, a fresh local-system temporary root, and no skips, errors, failures or `publish_permission` classification. Node v25.2.1 is available; the earlier focused page/HTTP/UI slice passed 66 cases and the browser/conversation/deliberation slice passed 50 cases. U01-U14 and L09 remain partial or blocking where their broader rendered, accessibility, reconnect and lifetime evidence is still open; this gate does not make a rendered release or accessibility claim.

The bounded U02/L09 artifact review found no current source-bound seam that captures and retains a real rendered Workspace shell with browser/toolchain and machine-readable skip evidence. `tools/release_gates.py` can safely retain sanitized JSON gate artifacts outside the checkout and rejects symlinked or in-tree outputs, but the fixed `browser_security` command exercises source/HTTP/Node fixtures and emits no screenshot, browser-engine metadata, or reviewed skip inventory. No screenshot or synthetic render artifact was created, and U02/L09 remain BLOCKING until an external-browser capture producer and its source/gate binding are available.

The bounded workspace-layout diagnostics increment is verified provider-free: `test_workspace_layout.py` passed 19/19, including safe stage/category subreasons for staging, ACL, manifest, fsync, validation, publication, and post-publication resolution failures; injected exception text, errno and paths remain absent from public errors, and published/unknown versus not-published outcomes remain fail-closed. The previously flaky real HTTP UI fixture passed 1/1 in isolation. The final combined browser-security gate also passed 116/116 with no `publish_permission` classification; broader UI/lifetime release rows remain open for their separate rendered, accessibility and supervision requirements.

A bounded first-failure browser diagnostic previously collected 114 passing cases before a host-sensitive layout-publication refusal; a separate fresh-root probe created eight layouts successfully. The final fixed browser-security gate passed 116/116 on a fresh local-system root without that refusal, so the earlier event is retained as historical host/load evidence rather than a current combined-gate failure. The Windows silent-launch harness also passed its generated-AGY and generated-discovery structural checks (2 passed, 1 platform-appropriate skip); the executed AGY descendant now uses the shared spawn policy.

Inbox timing diagnosis and qualified rerun: the inbox lifecycle cases have no child process, sleep, or network wait; they perform repeated journal/lock/fsync work. On a local-system temporary root, the ordinary inbox partition passed 23 cases, the separate quota/capacity partition passed 5 cases, and inbox recovery passed 5 cases (33/33 scoped cases). The default project-volume run exceeded the bounded observation window, so the qualified result remains host/filesystem-performance sensitive and does not claim every volume is equally fast.

### Restart-safe checkpoint, 2026-09-06

The bounded F15 provider-free budget slice is source-complete for this checkpoint but remains `PARTIAL/BLOCKING`: `_workspace_budget.py` defines explicit versioned policy, snapshot, request and result contracts for rate windows, whole-message oversized-head handling, independent stream ordering/budgets, control/recovery reserves and separate execution-slot deferral. `test_workspace_budget.py` passed 9 tests. The adjacent admission/worker/inbox partition passed 104 tests in 544.16 seconds; the protocol/state/release partition passed 469 tests in 0.79 seconds; AST validation covered 206 Python files with 0 failures; the targeted privacy scan found 0 hits; and `git diff --check` passed apart from the pre-existing unrelated swarm CRLF warning. The manifest and coverage/status documents were updated. No provider, install, commit, release or credential action occurred.

The exact current F15 source identities remain bound to the private handoff/evidence packet rather than this public planning document. Astra's current source review is still in progress; its latest finding is that R01/R02 migration must next map every v1 producer/consumer, define the canonical v2-at-launch comparison, preserve v1 history projections, and add stale-generation/adapter-version refusal before contact. This checkpoint is not a release or acceptance decision.

The provider-free worker-message checkpoint adds 41 worker-send, 51 owned-worker runtime, and 108 synthetic transport cases with no provider contact or source edits. These cases confirm atomic admission/replay, owned-worker result verification, and transport/authentication refusal behavior; the base capability still explicitly reports `authenticated_worker_ingress=false`. F02 and R12 therefore remain partial/blocking rather than claiming production worker ingress or universal transport support.

The bounded F15 increment now adds a versioned admission-decision envelope bound to validated policy/snapshot/request facts, a canonical request/facts digest, the current workspace revision, and the current journal-prefix digest. Detached facts still refuse with typed `budget_binding_required`; live admission consumes a decision only when every binding matches exactly, otherwise it refuses before trusted source/content callbacks or journal mutation. The focused budget/integration gate passed 17 tests, the related worker-send gate passed 41 tests, and the adapter-bound stale-facts/revision/prefix plus control/recovery-reserve partition passed 12 tests. Inspection found that the owned-pipe workspace transport and standalone transport-budget module do not yet consume coordinator budget decisions; no adapter was invented. F15 remains `PARTIAL/BLOCKING`: universal transport accounting, cross-resource integration, and live adapter qualification remain open.

F15/R12 seam review, 2026-09-06: the bounded provider-free selection passed 188/188 budget, integration, transport-budget, owned-transport, and runtime tests in 60.12 seconds. An eight-case owned-worker demo partition passed 8/8 in 138.22 seconds; the remaining eight demo cases were stopped at the bounded window without a terminal summary and are not counted as passing. No runtime adapter was added: `ConductorDemo.launch()` constructs `OwnedFakeWorker` (and starts its fixed child) before `WorkspaceRuntime.admit_turn()`, while the existing coordinator decision is bound to workspace policy/snapshot/request/revision/journal-prefix facts and currently gates coordinator message admission only. There is no source contract that binds that decision to the worker constructor or exact selected transport request; adding a heuristic gate would falsely claim pre-contact enforcement. F15 and R12 therefore remain `PARTIAL/BLOCKING`; the missing seam is an explicit coordinator-issued transport-admission adapter that consumes the exact bound decision before `Popen`/pipe bootstrap. No provider, install, commit, release, or credential action occurred.

F15/R12 adapter increment, 2026-09-06: an opt-in `BoundTransportAdmission` now consumes the coordinator's exact bound decision immediately before `OwnedFakeWorker` channel/Popen creation. It binds one selected message, an explicit execution slot, the worker binding as fixed issuance identity (not independent live claim/grant/instance authentication), and the complete fixed-work frame; multi-entry compiled contexts refuse because this adapter's budget request is one-message. Stale policy/snapshot/request/journal-prefix facts, reserve/rate/stream/oversize refusals, deferred slots, payload/binding mismatches, and duplicate consumption all refuse before child or pipe contact. The accepted receipt is retained for later observation, and translated public errors drop internal causes. The legacy constructor remains explicitly unqualified when no adapter is supplied. Focused adapter coverage passed 17/17; the related transport/budget/runtime partition passed 179/179 in 56.29 seconds; compilation, privacy scan, and `git diff --check` passed. This is one provider-free fixed-worker adapter, not universal transport or live-provider qualification; F15/R12 remain `PARTIAL/BLOCKING` for broader adapters, recovery paths, and live evidence. No provider, install, commit, release, or credential action occurred.

R12 now has a bounded provider-free transport fact in `_transport_budget.py`. An explicitly supplied operation capability measures the exact serialized argv line (Windows UTF-16 units or POSIX argument/environment bytes) or a private attachment, binds the result to the attempt/content digest, and returns a whole-message refusal before spawn when the declared route cannot carry it. The executor consumes this seam only when the caller supplies the authoritative capability; legacy routes retain the existing argv guard rather than receiving inferred limits. The focused transport/ZCode/executor gate passed 57 tests plus 3 subtests, and the dedicated transport budget tests passed 7 cases. This does not certify universal transport support or every backend boundary.

R01/R02 now have a bounded provider-free migration inventory in `docs/planning/R01_R02_MIGRATION_INVENTORY.md`. `_resume_capabilities.py` adds `compare_launch_scope()` as a pure pre-contact consistency check with no launch or provider authority, and `_conversation_runtime.py` invokes it before resumed dispatch. The focused resume/chat gate passed 39 tests, including exact scope match, stale registry generation, adapter-version, external-version and malformed-input refusal; the full conversation-runtime file passed 35 tests, including refusal before a second provider call. v1 capability/history projections remain unchanged. Authenticated observations, duplicate allowlist removal and backend qualification remain open R02 work.

The bounded migration follow-up now refuses exact-but-explicitly-unsupported continuation capabilities as `capability_unsupported` before contact instead of reporting a scope match; `launch_permission` remains `not_granted` and v1 projections remain unchanged. The focused resume-capability suite passed 37/37. R01/R02/F01/F17/L01-L03 remain PARTIAL/BLOCKING for full producer/consumer migration, stale-reader and index-rebuild evidence, legacy-ID coverage, and adapter qualification.

The R02/F17 compatibility packet adds provider-free evidence for the remaining
current seams: historical v1 refusal rows remain readable but cannot be
reconstructed as current launch authority; an existing legacy room ID can be
opened/listed without relaunch or duplicate room creation; and legacy
session-wide process records are inspected without creating a participant
scoped runtime directory. The focused migration packet passed 14 targeted
tests, and the changed resume/conversation/schema partition passed 111 tests
plus 2 subtests in 16.43 seconds. No provider was contacted and no live
authority was granted. R02/F17 stay PARTIAL/BLOCKING because authenticated
host observations, complete duplicate-allowlist removal, full stale-reader
migration and adapter qualification remain open.

The bounded F01/F17/L01-L03 source inventory is now machine-checked by `tools/schema_inventory.py`. It binds the named workspace/task/attempt/principal/worker/message/delivery/command/artifact/review/decision/resume contracts to repository-relative producers, readers, and regression fixtures. `tests/test_schema_inventory.py` passed 3 tests, and the focused schema/release/migration/entry partition passed 81 tests in 151.78 seconds. This closes the inventory's source-binding gap but does not promote the rows: full legacy CLI/projection migration, every stale-generation path, and authenticated adapter observations remain open.

## Current next action

P1-HUMAN-BROWSER-01 is APPROVED at bounded provider-free preview scope. The accepted journey now includes the source/runtime/HTTP/Node contract and a real rendered browser pass: responsive 390-pixel layout, keyboard composition, encrypted no-plaintext draft retention, full reload and fresh authentication without auto-send, unreadable-record freezing, same-key reconciliation after a deliberately lost result, and exactly one resulting message event. The final revocation case found one real presentation defect: host message-authority loss was labeled as storage failure. The conductor corrected the mapping; the browser now preserves the encrypted operation, shows authority unavailable, makes no journal write, and does not restore host authority through browser reauthentication alone. The architect independently inspected the correction and passed all 12 page tests. Foundation and integration partitions passed 639 cases/58 subtests and 328 cases/13 subtests respectively. This accepts the human-context preview journey, not 3.5.0 release readiness. The standalone UI-suite terminal receipt was lost and remains a later release-gate rerun. Memory-only opt-out and text-free discard remain deferred because a status-only tombstone must not hide unknown work or permit replacement over an uncertain send.

Current disposition, 2026-09-06: Summon improvements remains the conductor; this task remains architect/project lead. The bounded operator-message kernel, rendered human-context journey and foreground workspace entry are accepted at provider-free preview scope. The accepted entry uses the real parser/dispatcher: `workspace open` reopens only a compatible persisted scope, `workspace inspect` is read-only, and `workspace demo create` is an explicitly synthetic five-minute qualification fixture. The real lifecycle queued one bounded operator message exactly once, reopened the retained state without resending, became read-only when the fixture scope expired, emitted truthful terminal cleanup and left no owned listener or process. Foundation passed 639 tests plus 58 subtests; integration passed 328 tests plus 13 subtests; the final focused entry/page gate passed 35 tests. Fable's conditional review findings were closed with startup-interrupt ownership, typed error redaction, terminal envelopes, silent child-launch coverage and explicit help text. This accepts the entry seam, not 3.5.0 release readiness or live-provider/native-session capability.

### Astra closure direction, 2026-09-06

Astra's architecture checkpoint keeps the next milestone scoped to the bounded provider-free Workspace preview. The closure order is: (1) R01/R02/F01/F17/L01-L03 schema, legacy-reader and migration inventory; (2) R12/F15 transport, 64 KiB, oversize-head, rate, control-reserve and stream-admission evidence; (3) R15/F02 named-account isolation, rotation, bootstrap and worker-scope evidence; (4) U01-U14/G02/L09 retained rendered, accessibility, reconnect and lifetime artifacts; (5) E07/E12 usage and roster-drift evidence; and (6) L04/L06-L08/L12 rollback, privacy, documentation, managed-copy, clean-source and owner gates. Each row remains explicitly PASS, PARTIAL or OPEN in `RELEASE35_COVERAGE_MATRIX.md`; no required row is silently deferred. The latest provider-free evidence adds migration 5/5, workspace runtime 50 tests/58 subtests, clean public-doc privacy results and no generated artifacts, but does not promote any broad row or establish release approval. Fable remains the final adversarial reviewer and the owner retains release authority.

G02 bounded review, 2026-09-06: the existing provider-free conversation, deliberation scheduler, restore/replay, and durable-context freshness partitions passed 168 tests in 6.10 seconds. They confirm fixed policy/seat/schedule bindings, context-only council recommendations, receipt-bound ballots, and stale/cross-run refusal. The source still has no integrated operator-message admission path between live multi-round deliberation rounds: conversation messages are journal context, while the scheduler receives one fixed durable context and only approval/cancel commands. Prompt overflow remains bounded by transcript elision, not a fail-closed multi-round context budget contract. G02 therefore remains PARTIAL/BLOCKING for the 3.5.0 preview; no provider, install, or release action was taken.

R01/F01/L02 bounded schema packet, 2026-09-06: `tools/schema_inventory.py` now binds the required workspace, task, attempt, principal, worker, message, delivery, command, artifact, review, decision, and resume names to source producers/readers, versioned schema literals, and focused fixtures. It also source-binds the flat CLI translator, dispatcher entry, skill documentation boundary, and legacy job reader as additive compatibility readers that never create launch authority. The inventory audit found 38 production files containing versioned `summon.*` literals; 22 remain explicitly unmapped migration sources across fleet, usage, liveness, transport, context, jobs, workspace UI/layout/view, and dispatcher projections. The provider-free schema/migration/workspace/swarm/resume/conversation/job/compatibility run passed 840 tests in 211.39 seconds. Existing projection/journal tests preserve authoritative bytes and reject unsupported readers before mutation, but no standalone journal-preserving index/projection rebuild seam exists, so R01/F01/L01/L02 remain PARTIAL/BLOCKING and L03 remains BLOCKING. No provider, auth, install, commit, release, or credential action occurred.

R12 received one bounded implementation increment: ZCode's private attachment transport now preserves exact UTF-8/newline bytes on Windows, and a 64 KiB-plus Unicode/multiline fixture proves the payload remains off argv and is cleaned up. The focused ZCode gate passed 42 tests plus 3 subtests; the related Windows/context partition passed 46 tests. This does not certify generic argv, fixed-pipe, ACP, or universal transport selection. F15 now has a separate versioned provider-free budget fixture: `test_workspace_budget.py` passed 9 tests covering explicit rate windows, oversized-head hold/refusal with independent streams, exact control/recovery reserve boundaries, stream prefix/budget refusal, and separate execution-slot deferral. This is still only structural evidence; coordinator integration and universal transport admission remain open.

The versioned provider-free `workspace create --plan FILE` contract is implemented in the working tree and is accepted by the architect as a bounded milestone. It creates a useful non-synthetic workspace from bounded goal, acceptance-criteria, task, dependency and operator-message-destination data while granting no provider, account, model, spend, permission, command, resume or worker-launch authority. Plan destinations remain outside the coordinator worker registry; a separate adapter registration is required before any claim, lease, message, artifact or terminal mutation. It uses deterministic private staging/no-replace publication, stores a private copied plan with a versioned host binding, and recompiles/verifies the copied plan against the durable goal, lanes and source references on reopen. The architect retains priorities, planning/review documents, independent acceptance and Fable consultation. The requirement-level status matrix is in `docs/planning/RELEASE35_COVERAGE_MATRIX.md`; broad items remain `PARTIAL` or `OPEN` where evidence is bounded. Full 3.5.0 release gates, managed-install evidence, retained UI evidence, roster review, migration/troubleshooting reconciliation and publication remain open.

Provider-free implementation evidence for this checkpoint is recorded in the release handoff: plan/protocol/entry 435 passed; coordinator/admission/runtime/state/protocol 215 passed plus 75 subtests; page/UI/view/conversation/deliberation/chat 188 passed plus 8 subtests; Windows silent-launch 13 passed; and release-manifest/gate tests 32 passed. A focused dormant-destination authority test passed 5 cases, including unchanged journal/state refusal and explicit registration. These counts are not release approval and do not include a named-model or live-provider endorsement.

P1-HUMAN-RUNTIME-01 is ACCEPTED at bounded local/provider-free scope. Actual private ContentStore publication, source-bound host installation, ephemeral handles, restart lookup, conjunctive host/session/grant checks, outside-writer source I/O and unknown-preserving append errors are exercised. A fixed receiver consumes only the selected complete multiline operator context and returns independently verified aggregates with one write. The integrated focused gate now passes 94 tests with the separately classified idle-client case deselected, and the architect's seven-case closure check passed without source drift. This is simulated authority/execution plus HTTP/Node contract evidence; it is not rendered-browser, provider or release qualification.

REC-02 ACCEPTED, bounded simulated same-task process recovery: the lead independently passed 110 JUnit cases in 136.91 seconds with no failures/errors/skips and all 28 source-packet files unchanged. Source review confirms queued/included/submitted boundaries, an immutable predeclared attempt limit, explicit synthetic host retry confirmation, a fresh process and recipient, one successor write, unchanged original message/task identity and retained indeterminate claims and spend/cleanup uncertainty. Run closure remains refused where effects are unresolved. Cleanup provenance is the parent test harness; this does not establish abrupt-crash, power-loss or live-provider recovery. At acceptance only the unfinished admission helper differed from that packet; the subsequent local operator replay changes below also remain outside the recovery packet qualification. Recheck the combined candidate after integration.

Human-message work is at a bounded implementation checkpoint, not complete. After the helper stoppage the lead took local ownership of operator composite replay and obligation classification. The combined gate passed 538 JUnit cases in 48.59 seconds, no failures/errors/skips or workspace-source changes during the run, including 14 new operator cases. One composite creates its explicit operator message, queued delivery, journal-derived admission proof and stable lookup response; same-key conflict, sequential ordering, legacy-event refusal and structural capacity no-half-admission are tested. Task/goal/decision authority stays unchanged. This is pure replay acceptance, not actual writer admission, authentication, reachable journal capacity or browser qualification. The subsequent dedicated sole-writer admission/lookup increment passed a combined 242-case gate in 137.12 seconds with no failures/errors/skips or watched-source changes. It includes 20 actual operator-journal cases plus replay, worker-send, operator reconciliation and coordinator regressions. Exact-cap admission retains cancellation capacity; uncertain complete-line writes require owned synchronization before lookup success. Revocation is checked after final sync and after append, and lookup never silently repairs or resends. The host/runtime and browser-message candidate now includes bounded encrypted draft recovery, first-edit/stale-seal guards, same-key lookup and truthful unresolved outcomes; its focused provider-free gate is 93 passed with one separate idle-client timing test deselected. This remains synthetic/provider-free evidence; independent review, broader runtime coverage, rendered qualification and executable entry remain open. The earlier 409-test existing-contract gate is superseded for these changed helper files. The encrypted-draft architecture is adopted with conditions in FABLE_DRAFT_DESIGN_REVIEW.md.

### Earlier checkpoints (historical)

RENDERED OPERATOR JOURNEY ACCEPTED, bounded fixture scope: keyboard cancellation recorded one delivery disposition while all tasks stayed pending with zero attempts and focus returned to the timeline. A second actual disposition response was deliberately dropped; the page showed uncertainty, preserved its exact pending operation across full reload and graceful same-origin server restart, required fresh authentication, and reconciled through explicit Check outcome. Revision stayed 21 with two distinct operator transitions and no duplicate; disposed contact/spend/cleanup remained unknown. At 390x844 the timeline measured 226.19 pixels; at 320x640 with expanded criteria it remained 160 pixels with keyboard scrolling/focus and no horizontal document overflow. Review tab closed and viewport reset. The replacement owned fixture stopped gracefully with exit zero; its predecessor required targeted cleanup after its stdin-control launch mistake. Actual history was preserved throughout. No live server remains. This does not complete human composition, all UI accessibility, native/provider qualification or executable launch/reopen.

REC-01 ACCEPTED for explicit simulated endpoint process recovery: the lead independently passed two tests plus two subtests in 60.38 seconds from a preserved 22-file source packet, unchanged throughout; its 21 shared files exactly matched the accepted UI baseline. Source inspection and execution verify a fresh Python supervisor reconstructing only persisted state and a precisely bound parent-test-harness cleanup observation, rejecting a bad reference before any consumer, independently authorizing/installing a fresh consumer, and making one successor write/receipt while preserving parent/message/task history and inherited uncertainty. Cleanup evidence remains explicitly parent-harness provenance, not independently rediscovered provider/process truth. The two-boundary same-process test remains distinct. Abrupt host crash/power-loss and full ordinary recovery are not certified by this milestone.

Current implementation: runtime is testing ordinary queued/included/submitted same-task recovery with declared attempt budgets; core now implements the accepted explicit local-operator message and atomic queued admission contract. UI takes the corresponding message union, composer and bounded private draft/reload design after the accepted disposition shell. The executable preview entry handoff selects a foreground-owned create/open/inspect lifecycle and explicit owner stop; existing conversation bearer-fragment/persisted-bearer helpers must not be reused directly. Exact public syntax and final parser/launcher integration remain future implementation gates, not current capabilities.

OPERATOR HTTP/PAGE INCREMENT ACCEPTED at source/test scope: the lead independently passed 172 tests in 73.91 seconds, no failures/errors/skips and 35 recursively identified source/test dependencies unchanged. The actual routes cover lost responses, same-origin restart with independently reinstalled authority, failed fsync, expiry/revocation during body/final synchronization, post-append reconciliation, typed error mapping, exclusive Windows port binding and bounded pending-operation storage. This closes UI-OP-01/02 at the tested HTTP/script boundary; rendered qualification is in progress. The lead used keyboard cancellation against the actual fixture and observed one delivery cancelled, all tasks still pending with zero attempts and focus restored to the timeline. The fixture's manual control channel lacked interactive stdin; its owner is preserving and reopening that existing history before the remaining lost-response/reload checks. That harness launch issue is not a product HTTP failure.

FINAL AUTHORIZATION/RECOVERY-CORE INCREMENT ACCEPTED: the lead independently passed 113 tests plus 53 subtests in 285.77 seconds, no failures/errors/skips and 33 recursively identified source/test dependencies unchanged. The gate covers full runtime, operator reconciliation, inbox and new inbox-recovery modules plus four affected legacy prefix/repair cases. Source review confirms the trusted conjunctive session/host check after the final blocking append synchronization; revocation before admission refuses, while revocation after a real append requires fresh-session same-key lookup. This closes the runtime/core portion of UI-OP-01; actual HTTP/browser handling remains open. SI02-02 same-endpoint cross-sender fencing and SI02-03 actual measured link-capacity refusal/remaining settlement passed. Unrelated-endpoint nonblocking is a pure scope guard under the existing single-endpoint product bound, not multi-endpoint runtime qualification. Actual supervisor process recovery and ordinary task retry remain open.

All gate freezes are released. Runtime resumes fresh-process endpoint recovery and then declared-budget same-task retries. UI applies a typed exception-kind mapping correction found by its real HTTP tests and completes its command/reload gate before rendered review. Core prepares the bounded human-message admission design independently, without destabilizing the recovery implementation. P1_TASK_SHELL_BRIEF.md retains the full human-composition and executable launch/reopen requirements; a concurrent documentation replacement briefly removed these sections and the lead restored their exact acceptance text.

New integration findings are assigned, with all three lanes active. UI-OP-01: session authentication before a potentially slow body and after mutation leaves a revocation window; UI/runtime/core are adding a trusted conjunctive per-request constraint through the final owned append boundary and a real delayed-body/final-sync refusal test. UI-OP-02: fresh ephemeral ports and page-memory-only pending commands do not establish browser recovery across server restart/reload. The lead approved explicit host-selected loopback port with no fallback, plus one strictly bounded session-storage disposition descriptor containing only scoped opaque identity, operation key, action and target; secrets/source/text remain excluded, and restoration requires authenticated scope matching and same-key lookup. Both are moving-source findings, not accepted fixes.

REC-01: the first actual endpoint recovery candidate closes real children and reopens the runtime, but the recovery itself stays in the same host process. Its later subprocess only inspects history. The runtime owner must add a fresh-process recovery execution using persisted state/evidence and independently installed fixture authority before claiming full supervisor process-restart recovery. Ordinary same-task successor attempts remain required afterward; inbox recovery does not replace them. The outdated brief example that retargeted a linked message to a fresh task has been corrected.

The required human context composition journey now has a concrete handoff in P1_TASK_SHELL_BRIEF.md. Source confirms ordinary messages currently require a worker sender/source task, so this needs an explicit local-operator message/admission representation, not a dummy worker or proxy ingress. Exact complete content, atomic queued admission, independent host authority, zero launch on send, durable same-key recovery and private bounded draft behavior are acceptance requirements. This follows active disposition integration and stays within the promised preview.

OPERATOR RUNTIME/RECONCILIATION ACCEPTED, bounded local scope: the lead independently passed 74 tests plus 53 subtests in 91.94 seconds, with no failures/errors/skips and 31 recursively identified source/test dependencies unchanged. Source review confirms independently installed/rechecked host authority, exact private decision-source binding, ordinary queued-context cancellation and held-context disposition without task cancellation, same-key restart lookup, evidence-only pending state, and complete-line uncertain fsync requiring successful owned synchronization before a recorded result. Missing source, revoked authority and torn/corrupt prefixes cannot mint durable success. The first 29-case core run passed but included runtime source drift; this combined frozen gate supersedes it. UI HTTP/browser integration and human composition remain open. Core/runtime freezes are released and both owners restarted on actual linked-successor recovery F18-B/C.

Operator integration is advancing: the lead inspected the new public sole-owner reconciliation seam and started independent verification of its uncertain-write/restart and no-repair guarantees. Runtime execution tests are implementation-owner reports until independently checked. The lead approved a host-supplied private presentation key for command-enabled workspace restarts: opaque targets remain workspace/run-bound, authentication rotates independently, and command authority must be independently reinstalled. Missing or rotated keys leave pending commands unresolved; they never trigger retargeting or a fresh-key retry. Read-only surfaces retain fresh-key defaults. Active HTTP controls and human context composition remain required.

FABLE-SUPERVISOR-02 completed with scoped APPROVE. Root verified successful execution, a valid report, authoritative matching root model identity and eight unchanged packet files. FABLE_WORKSPACE_DIRECTION_REVIEW.md records SI02-01 through SI02-05. Concrete next checks are a reachable real-journal link-capacity/settlement test and replacement-epoch fencing across all senders at the same endpoint without blocking unrelated endpoints. These are assigned to the active recovery owners. The reviewer did not accept moving operator controls, UI, native/live operations or release readiness.

NORMAL CONDUCTOR ACCEPTED, bounded simulated scope: the lead independently passed 121 tests and 45 subtests after the explicit envelope-identity provenance correction, with no failures/errors/skips or drift across ten source/test files. The actual public path creates one goal, uses three immutable fixed children across two logical workers with at most two live, verifies two main computations and assessments while the side claim remains active, admits a fresh successor grant, registers eight actual effect sources, preserves all receipt/contact history and closes within 32 content blobs. This is script-driven and supervisor-mediated; it does not certify autonomous live-model planning or worker-origin messaging.

BOUNDED FAULT/RESTART ACCEPTED: the lead independently passed the actual demo suite, 5 tests and 5 subtests, with no failures/errors/skips or drift across ten frozen source/test modules. Tested cases include scoped holds, actual owned-child interruption, durable refusal/duplicate boundaries and a fresh read-only inspection process. This does not complete the full F18 recovery/fork/collision matrix or establish power-loss durability.

CORE SEND COMPOSITE ACCEPTED: 141 runtime-independent tests passed (41 worker-send, 26 admission, 74 state); the lead inspected the final pre-publication validation, locked recheck and separate receiver-turn capacity/settlement path. All 180 tests in the broader invocation passed, but its 39 coordinator cases are advisory because their runtime dependencies were being edited. Eleven fingerprinted core/test modules were unchanged. This qualifies the structural composite, not authenticated process ingress.

AUTHENTICATED WORKER-TO-WORKER INCREMENT ACCEPTED: the lead independently passed the full frozen runtime/transport/demo suite, 141 tests and 53 subtests, with no failures/errors/skips or drift across twelve modules. A computes and authors context; B has no claim or task-frame contact until separate admission. Tests cover historical logical retry through a fresh authenticated frame, forbidden/cancelled/revoked sources, actual result computation, private-source replay, generic-event bypass refusal and the earlier conductor/fault journeys. This remains simulated, not live-provider or native-session qualification.

AUTHENTICATED WORKER-TO-SUPERVISOR INCREMENT ACCEPTED: the lead independently passed the runtime/transport/demo suite, 158 tests plus 61 subtests, with no failures/errors/skips and 38 recursively identified local source/test dependencies unchanged. An actual worker authors `[8]`, the taskless consumer receives its exact content through durable offer and authenticated receipt, and task/claim/worker/goal/priority/ordinary-effect state stays unchanged. The gate includes one-write concurrent exposure, durable revocation before exposure, refusal of an authentic expired receipt with an evidence-backed hold, separate-process source inspection, refusal of same-name reattachment, and generic authenticated-composite bypass refusals. Qualification is simulated context receipt, not model understanding, approval, native attachment or provider execution.

FROZEN INTEGRATION GATE COMPLETE: 903 tests plus 61 subtests passed across runtime/transport/demo, read-only UI and core/coordinator groups, with no failures/errors/skips and all 38 recursively identified local source/test dependencies unchanged throughout. The core group independently passed 591 cases, including the previously reported 552 and 39 coordinator regressions. This is bounded simulated workspace acceptance, not the full preview release gate. An immutable eight-file supervisor source packet was preserved against this exact tested baseline for Fable's next focused review; that review has not yet been dispatched.

Next ownership is active and all freezes are released: coordinator/runtime owners implement the public authenticated command-reconciliation seam to unblock the UI's actual controls, then actual linked-successor recovery under F18-B/C. All three owners were resumed with concrete handoffs. FABLE_WORKSPACE_DIRECTION_REVIEW.md retains SI-01 through SI-07, the additive direction and conservative late-receipt decision. Active typed controls, full F18 coverage and final rendered/release gates remain required.

SUPERVISOR TRANSPORT ACCEPTED, simulated scope: the lead independently passed all 108 transport tests in 6.16 seconds, with no failures/errors/skips and 21 recursively identified local source/test import dependencies unchanged. Actual owned-child tests establish exact UTF-8 receipt correlation, bounded offer/write behavior, cross-domain/epoch/replay refusals and the two concurrency corrections. This accepts transport only; durable inbox admission, current runtime activation and the actual worker-to-supervisor journey remain open.

Integration findings: **INBOX-R01** is closed in the accepted simulated runtime/transport scope: actual same-token concurrent exposure produces one write and an authenticated receipt while the competing caller refuses; prior durable revocation produces zero writes. **INBOX-R02** is closed within the accepted transport gate: reader exclusion spans frame matching, token publication and pending-state clearance; a caller paused after actual frame read cannot admit a competing reader, and the original receipt remains usable. Coordinator ownership is not held during pipe I/O; later logical revocation does not establish physical non-exposure.

Source-review conditions closed within the integrated scope: **INBOX-R03** retains two revision numbers for active endpoint revocation/retirement. The lead's core gate verified actual journal progression to active revision 62, revoked 63, unchanged-journal refusal of another activation, and legal retirement at 64. **UI-INBOX-R01** uses shared proof-free lineage validation to reject erased inherited exposure, substituted prior offers and invalid parents after child progress; both its five focused cases and full UI/core groups passed independently. These findings did not establish that a previously accepted canonical run emitted false state.

Rendered inbox increment verified against a finite actual-journal synthetic fixture: a closed workspace retained two inbox deliveries, including one disposed with unknown exposure and one simulated fixed-consumer receipt. The page kept retired endpoint ownership unverified and message bodies unavailable. Task filtering removed unrelated cards while preserving an explicitly workspace-wide summary. Measured timeline heights were 214 pixels at 1280x720, 291 at 390x844, and 160 at 320x640 with expanded criteria; no document horizontal overflow occurred. Keyboard scrolling retained timeline focus and reached the uncertainty warning. The review tab was closed, viewport restored and the owned fixture stopped gracefully. This does not qualify active controls, complete accessibility/reconnect behavior, live providers or native adapters.

Capacity evidence scope is explicit: the combined 127-task-plus-one-inbox map guard is a pure quota test, not proof that a particular journal history can reach that count under all other limits. The attempted long successor history also encountered the one-successor rule and the independent 256-evidence bound. Those limits remain intact. The lead's core gate paired structural combined-map refusal with real-journal evidence/reserve refusal followed by usable late receipt, retirement and closure. No operational 128-delivery capacity claim follows.

The lead mapped remaining F18 requirements into eight concrete integration cases in P1_VERTICAL_SLICE_BRIEF.md. Existing recipient-replacement refusal and pure linked-delivery replay do not establish actual post-restart recovery. The next recovery increment follows supervisor ingress and must retain immutable parent disposition, a separately authorized linked successor and inherited uncertainty. Separately, active operator controls need a public authenticated reconciliation/lookup seam: read-only history cannot certify durable success after uncertain fsync. UI POST remains disabled until that seam and its restart/lost-response tests exist; independent presentation work continues.

The ordinary operator-command binding/presentation helper independently passed 21 tests with no failures/errors/skips and seven dependencies unchanged. This accepts finite opaque-target request binding, immutable same-key lookup reconstruction and conservative pending/uncertain presentation only. It does not establish installed host authorization, durable reconciliation, active HTTP controls or human context composition; those remain required integration work.

READ-ONLY UI/INBOX INCREMENT ACCEPTED: the lead independently passed the four view/UI/page/command modules, 154 tests in 47.69 seconds, no failures/errors/skips and all 38 integrated-gate dependencies unchanged. This includes actual-journal inbox HTTP reads, lineage consistency, post-close unknown-exposure presentation, and bounded Windows wrong-origin body refusal without consuming the bootstrap code. The earlier implementation-lane run with moving core dependencies is superseded for this bounded scope. Active operator controls and the full preview UI/release requirements remain open.

## Earlier implementation checkpoints

The following records are historical. Current acceptance and ownership above supersede their pending-state wording; overlapping test counts are not additive.

The implementation lane reports the actual normal conductor path passed 121 tests and 37 subtests with stable source, including clean close and eight actual effect-source registrations within the existing content bound. Root acceptance is pending a small explicit envelope-identity provenance tightening and independent rerun. Fault scenarios and worker-origin messaging are still separate open journeys. The ingress source handoff confirmed the brief's atomic send requirement and the need for one bounded pipe demultiplexer; detailed composite design is proceeding without editing the frozen demo dependencies.

Current effect-capacity checkpoint: the lead independently passed 102 tests and 8 subtests with no failures/errors/skips or drift across nine source/test files. Separate resolution and evidence slots are reserved before launch; the new exact-cap fixture registers the late sources, resolves, handles duplicate identity and closes under the same cap while preserving acknowledgement/contact history. Invalid bindings leave bytes and reserve unchanged. This accepts the synthetic source-reference/journal increment; runtime verification of actual child facts and the normal conductor/fault gate remain pending. The implementation owner's broader 147-test/11-subtest report is advisory and not additive to this root gate.

The coordinator lane retains an approved design for read-only authenticated worker-origin ingress and a typed CLI/API handoff, but the current runtime deliberately exposes only the trusted-host/base capability (`authenticated_worker_ingress=false`). The accepted preview uses synthetic and owned-worker fixtures; an installed authenticated consumer, taskless supervisor endpoint, hostile same-user isolation and every backend adapter remain open. P1_VERTICAL_SLICE_BRIEF.md tracks this distinction alongside the remaining restart/fork/collision and UI gates. The presentation lane reports a first passing pure suite; independent review and rendered shell work remain open.

Latest accepted increment: the real base activation and additive obligation/status reporting passed the lead's independent gate: 144 tests and 3 subtests, no failures/errors/skips or drift across eight inspected source/test files. The base now advertises centrally enforced ordinary workspace events and atomic admission. Terminal tasks with unresolved deliveries report settlement_required rather than implying workspace closure. This is bounded local admission/status acceptance, not the completed conductor or release. The separate effect-resolution integration is now proceeding after this freeze; its actual-child and exact-cap clean-close gates remain open.

The effect-resolution pure protocol suite separately passed 302 tests under the lead's independent rerun with unchanged validator/test sources. This proves strict record and observation-shape rules only; actual issuer provenance, owned-child observations, persistence and capacity remain integration gates. The coordinator owner now owns a separate new effect-capacity acceptance file; the previous six-method capacity file stays frozen and the presentation lane continues UI work.

The demo's first real public execution reached second-iteration messaging and correctly refused a mixed-source stream. The demo owner is correcting the stream construction without weakening the kernel. A separate presentation lane now owns only new view/projection tests under P1_TASK_SHELL_BRIEF.md, advancing the required UI in parallel with backend integration. These are implementation reports, not yet independently accepted demo/UI outcomes.

Current verified checkpoint: the full atomic-capacity fixture independently passed 6 tests and 3 subtests with no failures, errors, skips or source drift. This covers consumable late-evidence slots through acknowledgement, interrupted hold/dead-letter preservation, invalid/duplicate slot credit, exact admission boundaries and close refusal until a clean delivery disposition. The fixed-worker compiled-context bridge separately passed 91 tests and 20 subtests. Counts describe distinct bounded scopes and are not a combined product qualification.

Next product action: complete the actual public-API conductor demonstration and its explicit effect-resolution path. Source review found that acknowledged deliveries correctly retain unknown spend/cleanup, so acknowledgement alone cannot support normal close. The lead approved a separate typed fixed-fixture effect-resolution operation with trusted launch and supervisor cleanup observations, complete scope binding and capacity reserved before launch. Protocol, coordinator and runtime work have distinct owners. Provider effects and inherited-uncertainty recovery remain gated. Earlier checkpoints below are historical and superseded where these facts differ.

Verified two-message checkpoint: the real base transaction with the facade's same-state verification callback passed independently, including two separately bound proof maps, exact compiled bytes, restart reconstruction and durable duplicate response. One focused test passed with no source drift. Preparation is still through the private fixture and the public facade feature gate remains closed; this is not yet the executable conductor demonstration. Complete lifecycle capacity now requires bounded `settlement_for` evidence slots so newly obtained required evidence can consume its reserved space, and a close guard for unresolved deliveries. The accepted design keeps each slot tied to one delivery, reachable target transition, required role and verified source; it does not grant execution authority or permit untagged evidence to consume settlement capacity.

Current ownership: following the external developer's final owned-source handoff and two authoritative task-not-found responses, the lead explicitly assigned the next coordinator/admission/state changes to the local protocol implementation lane. The capacity specialist owns only the new atomic-capacity acceptance file; the facade owner retains runtime/tests. This replaces the earlier unconfirmed external schema assignment below. First close the real two-message case using per-delivery proofs, then make reserved lifecycle/evidence events consumable and refuse run closure with unresolved deliveries. The prior passing cancellation shortcut does not establish these guarantees. No whole-workspace or release acceptance is claimed.

Latest integration checkpoint: the corrected atomic-capacity fixture independently passed 2 tests and 3 subtests with no failures/errors/skips/source drift. It covers exact admission boundaries, no half admission, same-cap claim cancellation and explicit not-submitted delivery disposition. Complete submission/acknowledgement/hold evidence paths still require reserve review. The facade's real two-message case exposes a separate shared-proof-map mismatch. Lead decision: use bounded `evidence_by_delivery` keyed by exactly the selected delivery IDs, retaining every proof's category/task/delivery binding. The facade owner is updating its owned files; delivery of the matching base-owner instruction is unconfirmed because the app task connector timed out and then reported the developer task unavailable. Do not infer that shared-source ownership is free or restart a task solely from the timeout. Independent work continues; the goal is active.

Current checkpoint: R13's two increments are accepted for the Windows provider-inert coordinator scope. Independent gates passed 69 core/layout cases and 17 capacity/race cases; these overlap other historical checks and are not additive. The protocol/state delta passed 301 cases, and the runtime facade passed 28 cases as a deliberately gated helper. These results do not establish an operational conductor loop.

The fixed-worker transport extension independently passed 59 tests with no failures/errors/skips/source drift. Child processes now perform one bounded deterministic computation and return a separately authenticated result; acknowledgement alone remains receipt evidence. The fixture uses three task-bound instances across two logical workers, with at most two live and the side result pending while main output arrives. This is synthetic transport/work evidence, not yet a durable conductor run or proof that a live model remains productively occupied.

Supervisor result verification independently passed 25 tests and 17 subtests with no failures/errors/skips/drift. It consumes the actual owned-worker result, verifies request/task/attempt bindings and independently recomputes the result, refusing authentic-but-wrong calculations. Returned private evidence bytes and registration proposal remain nondurable; no task completion or execution authority is inferred. The remaining integration must persist that evidence and apply explicit task/delivery transitions through the coordinator.

The developer now owns actual atomic claim/selection integration through the existing sole writer. The moving source now prepares a fresh prospective claim and reuses canonical selection validation; early source review is still REVISE, not acceptance. Current corrections are to derive workspace/grant authority from the same reconciled transaction, apply canonical selected-delivery effects during composite replay, establish lease time inside admission, and return an identical committed operation before checking subsequently changed task lifecycle. The current fixed-clock fixture with an external workspace snapshot does not prove those guarantees. Claim/selection and later submission intent remain distinct phases. The facade owner is preparing the two-worker demo integration while this necessary base change proceeds; no second writer or parallel authority store is authorized.

The next product acceptance is one goal, two actual simulated workers, three tasks and two evidence-driven iterations, with useful main work continuing during a bounded side investigation. A critical finding holds only affected dependent admission; ordinary messages never authorize new work. No additional abstract review round is needed before implementing this transaction. Fable challenges the integrated milestone after the focused evidence exists. The paragraphs below retain earlier checkpoints and are superseded by this current disposition where they differ.

Atomic seam checkpoint: the independent gate passed 178 tests and 32 subtests, with no failures/errors/skips/source drift. Source review nevertheless found that `_reserve_after` projects new ordinary claims but omits `workspace_turn_admitted`. A fresh-state in-memory comparison measured 305743 bytes of reserve for the ordinary claim versus 12604 for the composite, leaving 293139 bytes of claim reserve unaccounted for before delivery obligations. The developer owns the correction; a separate acceptance lane owns an actual atomic-capacity boundary fixture. A tautological duplicate-selection check in atomic replay is also returned for correction. The combined feature remains REVISE; old ordinary-claim capacity evidence does not certify the new event.

The owner's latest product direction makes goal-directed multi-agent loop engineering central. CONDUCTOR_LOOP_DIRECTION.md defines the conductor's focus, bounded specialist lanes, evidence-based integration and explicit checkpoints. The workspace fabric is the supporting mechanism. The first demo must make goal -> delegated work -> evidence -> next decision visible, rather than stop at message transport.

Current work: P1's isolated pure-contract module is implemented and independently passed 221 tests with no failures, skips or drift. The revised vertical brief requires two conductor iterations across one goal and three lane tasks; Fable seconded that direction. The developer is implementing R13's exact-byte owned append seam before coordinator reconciliation/reserve integration. Its arithmetic now covers complete reachable settlement paths. The storage specialist corrected a proposed recovery mechanism that would duplicate reducer effects. W06's delegated source patch passed the lead's updated 13-test Windows/structural guard; external popup attribution remains separate and open. Source skill guidance now promotes bounded conductor delegation and goal focus without claiming runtime enforcement.

R02 Stage A is accepted at the revision inspected by FABLE-WORKSPACE-01. The third-turn owner-generation failure and explicit-lineage correction passed independent verification: 63 tests and two subtests, no failures, skips or source drift. FABLE_WORKSPACE_DIRECTION_REVIEW.md records the closure and P1/R13 integration conditions. Earlier 225/87-test and rendered evidence retain their original scope; overlapping counts are not additive. Authenticated qualification remains Stage B and full R01/R02 stay open.

Parallel implementation lanes have returned the pure event replay/projection and bounded private content helpers. The lead is independently checking those candidates with the exact-byte journal seam and affected consumers. Integration must preserve the coordinator as sole task authority, provision the content lock before concurrent publication, and label Windows content durability as process-restart preview only. Namespace/power-loss qualification remains gated. The selected outer-layout guard against old writers is still an implementation dependency, not a proven feature.

The combined independent check completed with 383 tests, one failure, no errors/skips or source drift. All pure protocol/replay/content tests passed. Artifact publication failed the new journal checksum validation because its artifact digest occupies the journal checksum field. The developer owns the bounded compatibility correction; R13 increment one is not yet accepted. Separate helpers own the linked-delivery grant-category regression and content initialization precondition while the lead retains the conductor demonstration as the next product outcome.

The corrected scoped gate passed 349 JUnit cases without failures, errors, skips or drift. Replay (45 tests) and content (24 tests) are accepted as preliminary helpers; neither establishes runtime integration. The proposed journal checksum-key change still needs old-writer compatibility correction before acceptance. Parallel implementation now covers actual owned-pipe fake-worker transport and the versioned outer-layout guard. Both directly support the pending conductor demonstration.

Latest disposition: the journal format experiment was replaced by a namespaced artifact digest inside the unchanged checksum format. Final journal/swarm/recovery verification passed 60 JUnit cases without failures/skips/drift; increment one is accepted and the developer is implementing actual R13 coordinator reconciliation/reserve. The layout helper also independently passed 14 Windows tests and is accepted within its published-address/process-restart scope. Transport is implemented but awaits bounded write-deadline correction. The conductor demonstration remains the integration milestone; these helper acceptances do not complete it.

The owner corrected the lead's excessive focus on Windows diagnostics. Goal-directed multi-agent loops remain the objective; the workspace supports them. WINDOWS_POPUP_FOLLOWUP.md retains the separate open issue, and further external tracing is deferred. Necessary reliability work stays in bounded specialist lanes. No workstation-resolution claim is justified. Installation, credential changes, commits and releases remain separately restricted.

The sections below retain the chronological record. Their old statements about pending authorization or inactive implementation describe earlier checkpoints, not current blockers.

## Current baseline

The reviewed planning baseline is WORKSPACE_CHECKLIST_V3.md. Three advisory review rounds are complete. Runtime implementation and release gates remain unchecked. Preserve pending Astra/Flash changes as a separate reviewable slice.

Leadership has established an hourly follow-up, assessed the developer's first brief against source, and reviewed its bounded R01 specification. These checks produced concrete scope and contract corrections; they did not authorize implementation.

## Developer brief assessment

The developer task, Summon improvements, returned a read-only first-change brief. Its overall sequence is aligned, with these corrections sent back for incorporation:

| Correction | Required treatment |
| --- | --- |
| R13 omitted from prerequisite list | Reserve bounded journal/control capacity before P1 admits work |
| F13/F17/F18 omitted from named P1 sequence | Include delivery/fork disposition, authenticated send/disposition operations and the explicit two-worker gate |
| Dependency readiness mentioned as P1 | Keep S01/S02 graph work in P3, outside the first preview |
| Preview deprecation notice posed as optional | D06 already requires notice and a non-launching fork path; approve the concrete affected-lane policy |
| Broad P0 list called the smallest change | Split into reviewable slices; begin with R01 versioned contracts and consumer compatibility, then R02 behavioral migration |

## Current source evidence

| Source | Verified fact | First-change consequence |
| --- | --- | --- |
| `skills/summon/scripts/_resume_capabilities.py` | Pure declarative registry; exact seven-field v1 schema; only Claude subprocess is registry-certified | Preserve purity and strict v1 behavior while designing v2; certification is not authentication of an individual session |
| `skills/summon/scripts/_job_continuation.py` | Registry used for public projection, private source eligibility and submission revalidation; private-source path also has explicit Claude/subprocess restriction | Inventory both registry consumers and route-specific guards; changing one table must not silently certify a new path |
| `skills/summon/scripts/_jobs.py` | Independently validates exact job-continuation/v1 fields before projecting capability state | Capability schema migration and continuation projection schema migration are distinct contracts |
| `skills/summon/scripts/_conversation_runtime.py` | Duplicate resume allowlist controls three decision sites | R02 must cover all sites, old-room behavior and explicit non-launching fork outcomes |
| `skills/summon/scripts/test_resume_capabilities.py` | Initial v1 suite passed 16 tests; corrected additive contract suite now passes 29 | Establishes pure schema/row/negative-input behavior only; does not qualify provider continuity or message delivery |

The initial registry regression run disabled bytecode, pytest cache and external plugin autoload. No provider calls were made. Broader suites were subsequently run for authorized implementation acceptance; their results and failures are recorded in R01_CONTRACT_ACCEPTANCE.md.

## Next bounded handoff

Owner for the requested specification: developer task Summon improvements. Accountable implementation owners remain D05 decisions.

The developer returned the R01 specification. The lead reviewed it and recorded the bounded proposal in R01_CHANGE_DECISION.md: explicit version scope, authenticated registry evidence, capability/acknowledgement separation, pure evaluation, compatibility projections and preserved Claude continuation. A current-source matrix identifies Codex/Cursor/AGY preview resume demotions separately from ordinary dispatch.

Authorized next change: the developer may implement only the pure R01 contract, directly necessary provider-inert fixtures and contract documentation, preserving current runtime behavior and pending roster changes. No provider calls, installs, commits or releases are included. The explicit authorization is the user's affirmative response to the bounded implementation question; it is not inferred from monitoring or model review.

Developer response before authorization: decision reviewed with no material contradiction found. The lead dispatched the authorized implementation, rejected concrete validation/digest gaps in its first draft, and independently reviewed the corrected patch. The full affected regression run was 180 passed and 2 failed, without exclusions; targeted reruns passed, and one failure reproduced against the original snapshot. The contract slice is conditionally accepted, not a full green regression or release gate. R01 as a whole remains unchecked; R02 runtime migration remains outside this slice.

Historical blocked audit: the prior unanswered authorization persisted across three goal turns and the goal was marked blocked. The user's subsequent explicit authorization resolves that blocker for this bounded slice only.

Pre-implementation observation: original source fingerprints were unchanged. Two earlier task-status reads timed out; no work was restarted on that basis. Future source changes must be classified as this authorized slice versus preserved pre-existing work, not rejected merely because implementation changes the original fingerprint.

The full target remains the reviewed workspace roadmap; this first slice does not replace its later supervision, scale, adapter or UI requirements. R16/L08 public-product-document edits remain outside the currently authorized contract-documentation slice.

Readiness review progress: READINESS_CORRECTIONS.md records eight focused R16/L08 findings, including inconsistent historical tag labels, obsolete current-suite counts, chat maturity/glossary conflicts, inbox-versus-turn wording and unclear governed-resume limits. The current manifest names 19 required suite groups and eight gates; that is a registry inventory, not fresh passing evidence. Public product documents and roster/runtime files remain unchanged.

## Monitoring rules

Post-acceptance investigation: P0_RELIABILITY_NEXT_CHANGE.md records a successful measured resume test and a provider-inert, in-memory slow-preparation experiment. The latter reproduced a five-second launch-lock refusal while the winner remained in preparation. Finalization fixture analysis identifies an unsynchronized startup precondition, without claiming the natural failure's root cause is conclusively established. No runtime or test files changed. The next proposal separates deterministic deadline tests, real process cleanup and explicit contention/retry semantics; it remains outside the authorized R01 implementation scope.

P0 proposal review: the developer's read-only plan is complete. The lead accepted its fixture/ownership direction and rejected unscoped pending fields that could falsely claim no provider contact while the winner is active. A separate provider-inert recovery probe passed all seven single-successor, one-launch, same-request retry and conflicting-input checks. The proposed next slice now preserves runtime busy behavior and changes only the two regression files plus dedicated contract/planning documentation. Authorization for that concrete slice is pending; no runtime activation is implied.

Check the developer's live task status and material source/test changes. Send coordination messages only for actionable deltas. Keep messages, advisory reports, model evidence, spend authorization and release authority separate. Keep user notifications quiet on unchanged state. Do not repeatedly request the same brief or restart work after a mere observation timeout.

This status document records authorization; it cannot expand it. Current implementation and bounded Fable consultations follow the explicit owner delegation in LEADERSHIP_AUTHORITY.md. Installation, credential changes, commit and release retain their separate restrictions. Planning updates contain only verified deltas and repository-relative public references.
