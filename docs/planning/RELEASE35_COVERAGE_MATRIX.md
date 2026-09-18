# 3.5.0 minimum-slice coverage matrix

Updated 2026-09-14. This matrix maps the minimum preview cut in
`WORKSPACE_CHECKLIST_V3.md` to current provider-free evidence. The tables below
use `Status` as an evidence state: a broad item is never marked `VERIFIED`
solely because a narrower fixture passes. `PARTIAL` means the bounded local
slice works but the evidence has an unqualified portion; `OPEN` means required
evidence is missing. The authoritative release decision is the separate
**3.5 disposition** table below, whose values are `VERIFIED-FOR-3.5`,
`BLOCKING`, or `DEFERRED-FROM-3.5`. Post-3.5/live remainder is recorded there
instead of being hidden inside a green status.

**2026-09-14 strict aggregate update:** the corrected Windows capture used an
explicit Chromium executable and completed all 27 fixed suites with 4,920
passed cases, 3 reviewed-policy skips, and zero failures or errors. Windows
qualification is available and every fixed gate passes except the intentional
`live_provider` block for missing authoritative evidence. This is the
strongest provider-free diagnostic packet, but it is not release evidence:
the source tree remains dirty, the source version remains 3.4.0, and U03/U07
device evidence plus L06/L09/L12 final-candidate actions remain open. The
current `workspace_ui` registry independently passes 330/330 with explicit
Chromium; this does not replace native assistive-technology or mobile-keyboard
evidence.
The current browser-security and accessibility gates independently pass
204/204 and 159/159, and telemetry/privacy passes 46/46. These provider-free
reruns do not replace native assistive-technology/mobile-keyboard or final
candidate evidence.

**2026-09-18 strict aggregate update:** a fresh quiet-host capture from the
frozen dirty tree completed all 27 fixed suites with 5,488 passed case
outcomes, zero failures/errors and exactly the three reviewed-policy skips.
Windows qualification is available with a combined-run browser-security pass
(204/204); all 44 required rendered artifacts are bound in the same
source-bound packet. Both manifest profiles refuse solely on the uncommitted
tree. U03/U07 device evidence and the reserved L06/L09/L12 final-candidate
actions remain blocking; this packet is diagnostic workspace-preview evidence.

**2026-09-13 evidence update:** the rendered delivery producer now emits the
manifest's canonical hyphenated state-card filenames. The release contract
asserts the 44-file set; a current `workspace_ui` run passed 327/327 and the
accessibility collector matched 44/44 files. This closes the filename/reader
mismatch only. Actual assistive-technology and native mobile-keyboard evidence,
final candidate binding, and reserved L06/L09/L12 release actions remain open.
The first full current-source browser-security capture also reproduced the
safe Windows `publish_permission` refusal under this host's shared temporary
hierarchy; the affected cases pass in isolation, so no green combined-gate
claim is made until a dedicated private-root/CI rerun succeeds.

**2026-09-13 release-profile decision:** the default `stable` manifest profile
requires every fixed gate, including `live_provider`, to pass. The explicit
`workspace-preview` profile is the reviewed provider-inert boundary: it may
accept only `live_provider=blocked` with the machine reason
`live_provider_evidence_missing`, while every other suite/gate and all clean
source, rendered, platform, privacy, migration and managed-install checks
remain mandatory. This is a labeled preview artifact, not stable/public
release evidence.

**2026-09-13 aggregate QA update:** the first current-source capture after the
profile correction recorded 4,913 passed cases, 3 reviewed Windows skips, 2
failures and 5 errors. The failures were confined to shared-host browser/UI
startup cases; exact rechecks pass in isolation, but this does not qualify the
aggregate. The packet remains diagnostic and L09/L12 stay blocking.

## P0 reliability and provenance

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| R01 | PARTIAL | Fresh provider-free resume/context/profile partition passed 640 tests and 12 subtests across `test_resume_capabilities.py`, `test_chat_resume.py`, phase-zero/compatibility/context suites; a fresh schema/compatibility partition passed 507 tests and 17 subtests on 2026-09-06; the additive v2 launch-scope comparison and stale-generation/adapter-version fixtures now bring the focused resume-capability suite to 37 passed, including refusal of exact-but-explicitly-unsupported capabilities as `capability_unsupported`. Full producer/consumer v2 migration and per-adapter qualification remain open. | Do not claim universal continuation qualification. |
| R02 | PARTIAL | Provider-free migration evidence now covers historical v1 refusal readability without current launch authority, legacy room-ID open/list without duplicate creation, legacy session-wide process-record inspection without creating a participant scope, and pre-contact stale/unsupported capability refusal. The focused packet passed 14 targeted tests; the changed resume/conversation/schema partition passed 111 tests plus 2 subtests. Full duplicate-allowlist removal, authenticated host observations and all backend migration evidence remain open. | Existing rooms remain readable; unsupported or stale continuation stays gated. |
| R03 | PARTIAL | `test_evidence_kernel.py`, `test_portable_result.py`, and model-proof fixtures cover exact, mismatch, inferred, absent, and forged evidence. Complete provider/adapter coverage is not established. | Named-model claims remain fail-closed and provider-specific. |
| R04 | PARTIAL | `test_portable_result.py`, `test_evidence_kernel.py`, and job-control tests cover normalized structural outcomes. Live-provider and every backend-specific terminal category remain unqualified. | Partial/unknown outcomes remain advisory; no live reliability claim. |
| R09 | VERIFIED | `test_background_read_roots.py` covers read-root/prompt/script preservation, immutable per-job snapshots, launch/terminal identity mismatch, an active install lock, and an installer starting during lease creation. `test_job_resume.py` covers successor bundle identity. | Verified for the provider-free preview; no claim about an unqualified live provider. |
| R10 | PARTIAL | Earlier structural refusal, parser, exception, zero-attempt, and raw/normalized-exit evidence remains retained. VR-05's observational heartbeat correction and real-driver terminal/control regressions are now integrated with verified fixed/CI retention. | Accepted for the bounded provider-free preview; hard-kill behavior and live-provider cleanup remain indeterminate or separately qualified by design. |
| R11 | VERIFIED | Timeout parser/serialization tests and explicit-unit CLI/docs cover suffixes, legacy parsing, overflow, and extension semantics. | Examples may use explicit units; no live long-horizon guarantee. |
| R12 | PARTIAL | Fresh 640-test provider-free partition covers context compilation, freshness/source, prompt-file/newline handling, and compatibility. `test_zcode.py` now passes 42 tests/3 subtests, including an exact UTF-8 64 KiB-plus private-attachment transport. `_transport_budget.py` and its executor seam add explicit operation/content binding, strict capability-shape and declared-platform binding, full serialized argv accounting (UTF-16 on Windows, bytes plus environment on POSIX), whole-message refusal, and a 64 KiB-plus private-attachment fixture; the focused transport/ZCode/executor gate passed 57 tests/3 subtests before this incremental guard. The dedicated synthetic transport partition adds 108 passing cases. The structured seam is opt-in to caller-declared capabilities, so universal transport selection and every backend boundary remain open. | Oversize or uncertain payloads must queue/refuse; never truncate. |
| R13 | VERIFIED | `test_workspace_atomic_capacity.py`, `test_workspace_effect_capacity.py`, `test_r13_capacity_acceptance.py`, and `test_r13_rundir_append.py` cover bounded reserve/replay, saturation, synthetic ENOSPC, fsync uncertainty, interrupted/poisoned owners, and a bounded two-process claim race. | Capacity is bounded provider-free preview evidence; no power-loss or unrestricted operational maximum is claimed. |
| R15 | PARTIAL | Provider-free account/activation coverage now includes an activation-time recheck that explicit Claude/Codex profile homes are dedicated directories outside the dispatch tree. Synthetic profile homes in the dispatch-ledger fixtures are now outside the task root, and a regression proves that rotating the selected Claude profile home after reservation refuses before capacity consumption or provider contact. The focused activation, dispatch, and account-profile files passed 51, 55, and 19 cases respectively (125 combined); existing coverage also includes named account homes, detached propagation, registry-change identity binding, credential/profile-state rotation, authenticated bootstrap/expiry/revocation, and secret/path redaction. Full hostile same-user isolation, multi-account rotation/background recovery across every claimed backend, and live adapter qualification remain open. | No automatic account switching or fallback claim. |
| R16 | PARTIAL | README, public/engineering changelog, planning docs, and capability labels have been reconciled in this candidate. Final committed-source/privacy/release-owner review remains open. | No release claim until the final source-bound gate passes. |

## P1 task and message fabric

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| F01 | VERIFIED | All eleven required entities have source-inspected versioned representations, strict validation and canonical identifiers. Corrected machine mappings distinguish actual Artifact and workspace Review/Decision records. Independent acceptance includes the existing protocol/reader controls, four artifact publication/reopen/rebuild cases, twelve Review/Decision cases plus twenty-one existing controls, and eighteen strict plan/worker/command cases. Exact canonical retention and unchanged dependencies are verified; see F01_NAMED_ENTITY_ACCEPTANCE.md. | Current provider-free preview entity scope. Other format migration, live authentication and CLI/API projection gates remain separate. |
| F02 | PARTIAL | Provider-free approval, worker-send, and workspace-runtime gates cover authenticated grants, epochs, source verification, worker instance/lease/grant scope, stale-source refusal, bootstrap/expiry/revocation, dormant-destination checks, and the activation-time dedicated-profile-home recheck. The latest profile-binding regression proves that rotating the selected Claude account home after reservation is refused before provider contact; the activation and dispatch files passed 51 and 55 cases, and the account-profile file passed 19 cases. The bounded worker-message checkpoint passed 41 worker-send, 51 owned-worker runtime, and 108 synthetic transport cases. The runtime contract validates the extended observational budget-preflight shape without granting execution authority, and the base worker-send capability still reports `authenticated_worker_ingress=false`. Full hostile same-user worker isolation, durable worker bootstrap/recovery, and every backend adapter remain open. | Never treat a name, PID, or catalog seat as authentication. |
| F03 | VERIFIED | Sole-writer state/journal tests and operator admission/replay tests preserve writer authority and concurrent ordering in the bounded local slice. | Safe for the simulated/local preview; multi-process production qualification remains separate. |
| F04 | VERIFIED | Inbox/recovery, atomic publication, restart, and operator reconciliation tests cover interruption around append/projection/receipt boundaries. With a local-system temporary root, the ordinary inbox partition passed 23 cases, the separate quota/capacity partition passed 5 cases, and inbox recovery passed 5 cases (33/33 scoped cases). The default project-volume partition exceeded its observation window, so the qualified result remains host/filesystem-performance sensitive. | Provider-free crash recovery is accepted for the bounded tested preview scope; deployments should use an appropriate local temporary root and must not infer uniform performance across volumes. |
| F05 | VERIFIED | Admission tests reject unauthorized recipients, scope drift, stale grants, and agent-authored recipient expansion. | Message routing cannot widen the data boundary. |
| F06 | VERIFIED | Stable IDs, correlation/causation, sender/recipient epochs, and recipient-instance bindings are exercised by operator and workspace tests. | Successor rebinding remains explicit and audited. |
| F07 | VERIFIED | Delivery transition and held/rejected/cancelled/expired paths are covered by operator runtime, reconciliation, and UI tests. | Contact certainty stays separate from delivery state. |
| F08 | VERIFIED | Durable admission, adapter receipt, and provider-submission evidence are represented separately in projections and tests. | A terminal result cannot fabricate acknowledgement or model understanding. |
| F09 | VERIFIED | Idempotent send/reconcile and conflicting-ID/refusal tests pass without duplicate journal writes. | Automatic re-arm remains limited to proven no-contact cases. |
| F10 | VERIFIED | Epoch, revocation, replaced-recipient, stale-owner, and replayed-grant cases are covered by admission/runtime tests. | No silent retargeting or recursive delegation. |
| F11 | VERIFIED | Recipient-scoped context selection, complete-message fitting, explicit included/deferred sets, and digest checks are covered by context/runtime tests. | Required messages cannot be silently skipped or truncated. |
| F12 | VERIFIED | `test_workspace_operator_runtime.py`, `test_workspace_operator_send.py`, and `test_chat_resume.py` cover queued next-turn/resume delivery, explicit lookup, and bounded recovery. Live delivery remains an explicitly absent adapter capability. | Verified for the provider-free preview; no live-delivery or spend authorization is implied. |
| F13 | VERIFIED | Cancellation, expiry, dead-letter, fork lineage, parent-delivery disposition, and uncertainty preservation are covered by workspace tests. | Retraction cannot erase submitted or uncertain work. |
| F14 | VERIFIED | Authority non-escalation and malicious-message tests cover permission/account/model/spend/routing/policy boundaries. | Agent prose remains context, never policy. |
| F15 | PARTIAL | Fresh workspace admission/capacity partition passed 114 tests and 11 subtests, and the provider-free `test_workspace_budget.py` passed 9 tests for explicit rate windows, oversized-head hold/refusal with independent streams, control/recovery reserves, stream prefixes/budgets, and deferred execution slots. The coordinator now exposes an observational preflight plus a bound admission-decision envelope carrying validated policy/snapshot/request facts, canonical request/facts digests, the current workspace revision, and the journal-prefix digest. Detached facts still refuse with typed `budget_binding_required`; exact-bound mismatches refuse before trusted source/content callbacks or journal mutation. The 2026-09-06 bounded cross-cutting selection passed 188/188 budget/integration/transport/runtime cases; one eight-case demo partition passed 8/8, while its remaining eight cases were stopped at the bounded window and are not counted. The opt-in `BoundTransportAdmission` now consumes the exact bound decision immediately before the fixed worker's channel/Popen boundary, binds one selected message/execution slot plus binding/frame fixed at issuance (not live claim/grant authentication), refuses multi-entry contexts for its one-message contract, and refuses stale/mismatch/oversize/rate/stream/reserve/deferred/duplicate cases before contact; focused adapter coverage passed 17/17 and the related partition passed 179/179. The accepted receipt is retained, public refusal causes are redacted, and the legacy constructor remains explicitly unqualified. Universal transport accounting, recovery-path integration, and live adapter qualification remain open. | Capacity claims stay qualitative/bounded; detached preflight cannot authorize execution. |
| F16 | VERIFIED | Independent task/attempt states, blocked/failed/indeterminate outcomes, dependency handling, and unresolved cleanup are covered by runtime tests. | Aggregate success cannot hide unresolved work. |
| F17 | VERIFIED | Ordinary host and authenticated CLI/API send/disposition/link paths, strict compatibility, source-bound authority, capacity recovery, same-key reconciliation, concurrent writer contention and advanced-child history have independent focused evidence recorded in F17_PUBLIC_COMMAND_BRIEF.md. Real Chromium ordinary-host submission recovers a dropped response through reload/reauth; fresh-host reinstall is tested separately. | Bounded provider-free preview; no live provider, independent-process recovery or implicit retry claim. |
| F18 | VERIFIED | `test_native_turn_collision_uses_runtime_admission_boundary` and the retained `test_two_worker_crash_fork_collision_identity_matrix_has_one_successor` exercise the runtime collision boundary, two independently owned workers, an actual child crash after durable launch intent, explicit same-task recovery/fork lineage, epoch/instance identity separation, idempotent replay, and exactly one successor with no duplicate sibling child. | Protocol qualification is provider-free only; real messaging remains gated. |

## Minimum P4 user-facing shell

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| U01 | PARTIAL | Workspace UI/page/view projections and authenticated entry tests cover scoped task surfaces. Full atlas migration/origin/CSRF/accessibility design remains open. | Legacy surfaces remain; no claim of complete atlas replacement. |
| U02 | PARTIAL | Task/goal/message/evidence projections, canonical actual-host detail reads and a scoped rendered journey exist. The release runner retains sanitized JSON outcomes and skip records outside the checkout. The complete canonical one-focal-area rendered shell and exact browser/toolchain evidence still need candidate-bound retention. | Keep current surfaces clearly labelled preview. |
| U03 | PARTIAL | Focused page/HTTP/UI tests passed 66 cases and the browser/conversation/deliberation slice passed 50 cases, covering stable focus, scroll, reflow, reload, reconnect, authority-loss and notifications for the bounded journey. The workspace-layout diagnostics partition passed 19/19 with bounded stage/category errors and no path/errno leakage; the previously flaky real HTTP UI fixture passed 1/1 in isolation. Historical five-file `browser_security` evidence passed 116/116 on a dedicated local-system root; the current expanded gate remains host-sensitive and is not promoted. Full long-journal, mobile-keyboard and broader rendered evidence remain open. | No broad accessibility/layout claim yet. |
| U04 | PARTIAL | Delivery badges/projections and operator UI tests cover several queued/held/rejected/cancelled states. Complete normative state presentation and contact/spend UX remain open. | Never infer acknowledgement or token cost from completion. |
| U07 | PARTIAL | Keyboard/focus/reflow checks exist in the provider-free page/UI evidence, the layout diagnostic partition passed 19/19 without exposing host details, and historical five-file `browser_security` evidence passed 116/116 on a dedicated local-system root. Measured WCAG 2.2 AA, assistive technology, native mobile keyboards, and full toolchain evidence remain open. | Accessibility gate remains open. |
| U08 | PARTIAL | Reload, owner-loss, uncertainty, reconnect, and bounded notification cases are tested. Full stale/indeterminate recovery and measured backoff matrix remain open. | UI timeouts cannot declare worker death or clear spend uncertainty. |
| U14 | PARTIAL | The minimal preview shell is accepted after integrated page/view/entry tests, native owner/freshness rendering and source-derived route enumeration; see U14_OWNER_PRESENTATION_ACCEPTANCE.md. Real P2 supervised-background controls remain later work. | No hidden daemon, implied queue evaluator or automatic lease retry. |

## Council and deliberation preservation

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| G01 | VERIFIED | Council/review and deliberation UI/protocol tests keep review artifacts separate from decisions. | Advisory positions cannot authorize release. |
| G02 | VERIFIED | Explicit advisory council pause/context-submit/continue is accepted through the real public main lifecycle: 22 guarded protocol cases plus seven independently passed actual-process transport controls. Original deadlines, seats, launch budgets, definitions and authority remain bound; validated snapshots prevent post-check replacement from changing later context. The exact four-file transport packet is retained in CI/release coverage. | Qualified for the provider-free council preview. Human context does not become a deliberation ballot, permission change or approval; live provider transport remains separately gated. |
| G03 | VERIFIED | Deliberation setup tests bind option IDs, seats, quorum, rounds, attempts, deadlines, and approval policy. | Fixed-option setup remains explicit. |
| G04 | VERIFIED | Evidence-kernel and deliberation tests accept only receipt-bound ballots. | Messages, summaries, and self-identification are not votes. |
| G05 | VERIFIED | Durable-before-contact, replay/recovery, terminal precedence, and uncertain-spend tests pass provider-free. | Recovery remains fail-closed. |
| G06 | VERIFIED | `test_evidence_kernel.py`, deliberation CLI/UI tests, and workspace authority tests verify that live-provider activation, writable seats, approval application, resume, and alternate transports remain individually gated. Full live qualification is intentionally absent. | Verified as a non-escalation rule for the preview; live capability still requires separate evidence. |
| G07 | PARTIAL | Fable remains a separate escalation lane. The 2026-09-08 bounded review has authoritative exact root served-model evidence and approves RB-01 through RB-03 plus the preview scope boundary; it does not approve the entire current candidate or publication. | Human/release-owner authority remains primary. |
| G08 | VERIFIED | `test_cross_kernel_context_receipt_and_generation_attacks_are_inert`, `test_cross_kernel_hostile_context_variants_never_promote_or_launch`, and `test_missing_quorum_cannot_restore_candidate_or_emit_transition` cover ballot-shaped chat/council context, copied receipts, option/quorum mutations, stale generations, hostile transport fields, and partial/missing quorum without adapter launch, journal mutation, or automatic ballot promotion. | No automatic ballot promotion. |

## Release, privacy, and migration gates

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| L01 | PARTIAL | The bounded schema packet source-binds the flat CLI translator, dispatcher entry, skill documentation boundary, and legacy job reader to compatibility fixtures; the 840-test provider-free run passed. Full companion-skill/legacy-run inventory and every stale-generation path remain open. | Workspace remains explicitly preview-labelled. |
| L02 | PARTIAL | Versioned workspace/projection namespaces and strict validators exist. Named-entity mappings and format-level migration inventory have different scopes; neither validator proves compatibility by itself. The current bounded reader acceptances and remaining format gaps are recorded in F01_F17_L01_L03_SCHEMA_INVENTORY.md. Historical producer equivalence and remaining stale-reader fixtures stay open. | Unsupported readers must refuse before mutation. |
| L03 | VERIFIED | Current standalone and actual workspace reconstruction acceptance passes 19 cases, preserving journal/opaque sidecar bytes, declared identity and uncertain spend without mutation or launch. L03_ARCHITECT_REVIEW.md maps the exact requirement and limits; prior RB acceptance remains intact. | Legacy reader migration and candidate-bound release evidence remain separate open gates. |
| L04 | PARTIAL | `test_workspace_lease_and_incompatible_reader_preserve_state` now covers active execution-lease refusal for refresh/uninstall, explicit provider-free settlement, incompatible-reader refusal before mutation, and durable workspace/job/room/usage/telemetry preservation. Encrypted draft/UI and broader host rollback evidence remain open. | Blocks release readiness. |
| L05 | VERIFIED | Telemetry is optional/local, disabled by default in the bounded contract, and operational audit state is separate. | No telemetry claim beyond the tested local scope. |
| L06 | PARTIAL | Public-document scans found no real private paths, credentials, or client data. Final exact-source/export scan remains required after staging. | Blocks final publication until rerun on committed candidate. |
| L07 | PARTIAL | Private evidence separation and scope-bound tests exist. Full OS-user ACL/reparse/export qualification remains open. | No broader multi-user/private-workspace claim. |
| L08 | PARTIAL | README, changelogs, planning, capability, and troubleshooting edits are underway and stale wording is being corrected. Final consistency review remains open. | Blocks release documentation sign-off. |
| L09 | PARTIAL | Release manifest/gate tests and focused UI/platform checks have accepted evidence; CI and fixed registries retain workspace partitions. Current `release_collect.py` produces source-bound per-case outcomes and redacted skip records, and `release_outcomes.py` requires explicit matching skip review. The current review policy is empty; no skipped result is implicitly accepted. Complete candidate-bound rendered/browser/toolchain evidence, reviewed actual skip outcomes, integrated fixed gates and stale-consumer contract retention remain required. | Blocks the integrated release gate until the remaining release evidence is closed. |
| L11 | VERIFIED | Astra/Flash source and focused catalog/roster suite pass 15 tests, including Astra `max` and `xhigh`. Live availability and served identity remain explicitly unqualified. | Final candidate stability and documentation still required. |
| L12 | OPEN | Requires clean committed source, source-bound evidence, migration/rollback, managed-install convergence, privacy pass, and release-owner publication decision. | Explicit release blocker. |

## Applicable usage, context, and roster invariants

| ID | Status | Source/test evidence | Release consequence |
| --- | --- | --- | --- |
| E03 | PARTIAL | Provider/account usage boundaries are represented in phase-one usage tests and docs. No live usage query was performed for this closure. | Usage refresh cannot trigger auth, routing, retry, or spend. |
| E04 | VERIFIED | Provider-inert selection/permission tests preserve explicit model/account/spend pins and reject silent fallback. | Recommendations remain advisory. |
| E05 | VERIFIED | Typed context compilation, deduplication, bounded transcript, and artifact-elision tests pass. | Optimization may not change authority or required content. |
| E06 | VERIFIED | Evidence/provenance/mutation records remain separate from summaries in projection and privacy tests. | Audit facts cannot be stripped for token savings. |
| E07 | PARTIAL | Live usage observations now require successful execution/provider-contact evidence and a valid account-scope proof before affecting the advisory projection; invalid observations fail closed. The focused provider-free usage suite passed 51/51. Complete measured-vs-estimated/cache/opt-out coverage and live qualification remain open. | Unknown contact/usage stays unknown. |
| E08 | VERIFIED | Catalog, requested/targeted/served identity, and evidence freshness are separate in model/catalog tests. | Editorial catalog data is never served proof. |
| E09 | VERIFIED | Astra, Fable, Flash 3.8, Kimi, Sol, Terra, and Luna entries are distinct and documented; the 15-test roster/catalog suite covers Astra `max` and `xhigh`, and no live claims are made. | Live availability remains outside provider-free evidence. |
| E12 | PARTIAL | Fresh catalog/routing/fleet coverage includes a 14/14 model-routing check that explicit effort pins fail closed before provider contact with typed `effort_unsupported`, `provider_contacted=false`, `result_usable=false`, and `retryable=false`; ambient/default effort remains best-effort. The follow-up revalidated retired Ox selector tombstones and the separate GLM successor, editorial versus served identity separation, tri-state exact-model proof, and unsupported explicit effort in 16 focused tests plus 3 subtests; the selected discovery identity/effort subset passed 7 tests. A new resume regression covers model and effort drift and refuses before successor launch (4 focused cases). Complete rename/removal/alias-remap, broader in-flight rebind/fork drift coverage, and authoritative served-model qualification remain open. | In-flight tasks require explicit rebind/fork on drift. |

## Explicitly outside this preview

R05–R08 are existing-provider/host regression gates that must pass for any
affected route; they are not silently considered complete by this matrix.
P2 supervision, P3 dependency swarms/native adapters, live provider claims,
and long-horizon GA remain deferred unless separately qualified and authorized.

## Authoritative 3.5 disposition

This is the release-scoped view of the minimum cut. `BLOCKING` means the
candidate cannot be accepted for 3.5 until the listed evidence is complete.
`VERIFIED-FOR-3.5` means the bounded provider-free requirement is satisfied;
any later/live remainder is intentionally not part of this release. No
mandatory row is silently treated as deferred.

| ID | 3.5 disposition | Post-3.5/live remainder or blocking reason |
| --- | --- | --- |
| R01 | VERIFIED-FOR-3.5 | Versioned registry, supported historical producers/readers, authenticated qualification/revocation and queued registry/adapter/external-version refusal are accepted at the provider-free preview scope. The exact queued-version packet is retained in CI/fixed release coverage and independently passes on the integrated source. Installed-provider qualification remains separately gated; R15 profile-state and E07 accounting corrections remain distinct. |
| R02 | VERIFIED-FOR-3.5 | Current-v2 candidate/unsupported refusal and explicit fork packet is retained and independently passes. The corrected 28-case migration packet independently passes, including historical-v1 explicit fork and cancellation ordering; exact canonical retention with CI/release inclusion is verified. README, Unreleased changelog, capability and migration documentation now reflect accepted behavior and explicitly distinguish gated candidate-provider qualification. Closed for the certified preview scope only; no new provider/native continuation certification. See L02_L04_REMAINING_ACCEPTANCE.md. |
| R03 | VERIFIED-FOR-3.5 | VR-06 now preserves explicit Kimi evidence conflict through final non-exact stamping, with exact refusal, zero-output and ordinary-inference controls. The integrated runtime, regression and fixed/CI retention match the independently reviewed and tested composition. Provider-specific live certification remains separately gated; see FINAL_VALIDATION_TRIAGE.md. |
| R04 | VERIFIED-FOR-3.5 | Live backend-specific terminal qualification remains separately gated. |
| R09 | VERIFIED-FOR-3.5 | Managed-copy convergence remains an L12 release gate. |
| R10 | VERIFIED-FOR-3.5 | VR-05 preserves completed terminal results after observational heartbeat OSError, with midstream/final real-driver regressions and strict durable-control/heartbeat-auth controls. Integrated runtime, regression and fixed/CI retention match the independently tested composition. Hard-kill and live-provider cleanup qualification remain distinct; see FINAL_VALIDATION_TRIAGE.md. |
| R11 | VERIFIED-FOR-3.5 | No live long-horizon runtime guarantee is implied. |
| R12 | VERIFIED-FOR-3.5 | Complete local transport crosswalk, capable 64 KiB attachment, exact-byte adapter/council boundaries and corrected authenticated 4096/+1 operator-body packet are retained in CI/fixed release coverage. The integrated operator packet independently passes. See R12_TRANSPORT_CLOSURE.md for fresh/resume, Windows/POSIX, ACP late-refusal and provider-neutral claim limits. |
| R13 | VERIFIED-FOR-3.5 | Provider-free reserve/replay, synthetic disk-full/fsync/interruption, and bounded Windows process-race evidence pass; power-loss behavior remains outside the preview claim. |
| R15 | VERIFIED-FOR-3.5 | Authenticated v2 source binds selected profile state and claim identity; exact v1 remains readable and refuses unsupported named-profile continuation. Lead independently passed five rotation/legacy/control cases plus two unavailable-state/public-redaction cases with no source drift. Final locked revalidation, canonical CI/release entries, version mappings and supported fresh-invocation recovery wording are reviewed. No new account/provider certification; existing candidate Codex refusal remains. See R15_PROFILE_CONTINUITY_ACCEPTANCE.md. |
| R16 | VERIFIED-FOR-3.5 | Seven integrated documents exactly retain the source-reviewed preview scope, truthful capability/model/usage labels and strict final publication boundary. Current planning actions and historical migration notice are reconciled. See R16_L08_DOCUMENTATION_ACCEPTANCE.md; final candidate privacy/release evidence remains separate. |
| F01 | VERIFIED-FOR-3.5 | Eleven source-backed entity contracts and corrected mappings are accepted with strict identity/version/authority refusal and supported producer/reader evidence. The final eighteen-case packet is independently passed and retained unchanged in CI/release/inventory; all 27 dependencies remain unchanged. Other format migration remains L02; see F01_NAMED_ENTITY_ACCEPTANCE.md. |
| F02 | VERIFIED-FOR-3.5 | Current issued-channel/bootstrap, scope, replay, rotation/revocation and restart semantics reconcile with retained actual worker/handler fixtures and prior independent ingress/operator acceptance. The reviewed model trusts OS user/launcher/supervisor and does not claim arbitrary hostile same-user isolation. See F02_AUTHENTICATION_ACCEPTANCE.md. F17 public wiring and L07 OS privacy remain separate. |
| F03 | VERIFIED-FOR-3.5 | Multi-process production qualification remains later work. |
| F04 | VERIFIED-FOR-3.5 | Provider/native crash qualification remains later work. |
| F05 | VERIFIED-FOR-3.5 | No live provider recipient is implied. |
| F06 | VERIFIED-FOR-3.5 | Explicit successor rebinding remains required. |
| F07 | VERIFIED-FOR-3.5 | Adapter acknowledgement remains separate from model understanding. |
| F08 | VERIFIED-FOR-3.5 | Provider submission evidence remains separately qualified. |
| F09 | VERIFIED-FOR-3.5 | Automatic re-arm remains limited to proven no-contact cases. |
| F10 | VERIFIED-FOR-3.5 | Recursive delegation and cross-workspace messaging remain excluded. |
| F11 | VERIFIED-FOR-3.5 | Live transport limits remain separately qualified. |
| F12 | VERIFIED-FOR-3.5 | No live-delivery adapter or spend authorization is included. |
| F13 | VERIFIED-FOR-3.5 | Provider-side retraction is not claimed. |
| F14 | VERIFIED-FOR-3.5 | Messages remain context, never policy. |
| F15 | VERIFIED-FOR-3.5 | Architect acceptance in F15_ARCHITECT_REVIEW.md: durable coordinator-derived pre-spawn admission, ordered context, zero-slot supervisor delivery, current-authority checks and full simulated messaging/recovery/parallel journeys. Current demo passed 17 tests plus 13 subtests; core partition passed 793 plus 69 subtests; affected coordinator/release partition passed 123. Universal external/live qualification remains later. |
| F16 | VERIFIED-FOR-3.5 | Aggregate success remains conservative. |
| F17 | VERIFIED-FOR-3.5 | Accepted consolidated retain/link contract packet and independently verified final concurrent-writer, ordinary-host Chromium and post-disposal original-operation lookup gates. Preserve explicit source authority, immutable lineage, unknown effects and same-key recovery. See F17_PUBLIC_COMMAND_BRIEF.md for evidence and limits. |
| F18 | VERIFIED-FOR-3.5 | Retained two-worker crash/fork/collision/identity matrix passes provider-free; live provider messaging remains separately gated. |
| U01 | VERIFIED-FOR-3.5 | Independently accepted ordinary-host typed navigation, separate body scope and negative authorization cases exactly match integrated source. Fixed/CI rendered retention is verified; legacy surfaces remain. See U01_U02_U04_RENDERED_ACCEPTANCE.md. Remaining U07 accessibility still blocks release. |
| U02 | VERIFIED-FOR-3.5 | Canonical task identity, one-focal-task typed details, separate artifact body scope, task filtering and drawer focus/late-response handling independently pass in real Chromium; exact source/test retention and rendered artifacts are verified. See U01_U02_U04_RENDERED_ACCEPTANCE.md. |
| U03 | BLOCKING | Rendered 210-event journal pagination and pending/explicit-refresh anchoring pass with zero measured offset change; see U08_BROWSER_ACCEPTANCE.md. Complete mobile-keyboard, broader reconnect/jitter and candidate-bound rendered evidence. |
| U04 | VERIFIED-FOR-3.5 | All twelve normative worker-delivery states have independently accepted real-browser evidence and exact fixed/CI retention. Separate contact/spend/ack/turn/model facts, finite reasons, scoped actions and bound journal time remain honest. Synthetic receipts do not qualify physical provider delivery. See U01_U02_U04_RENDERED_ACCEPTANCE.md. |
| U07 | BLOCKING | Measured contrast/reflow, native focus, reduced motion and drawer keyboard evidence are accepted. Native 200%/400% zoom independently passes with fixed bounds and full-size images; its exact test/fixed/CI retention and 44-file rendered collection now match the integrated release contract. See U07_NATIVE_ZOOM_ACCEPTANCE.md. Complete actual assistive-technology announcements and native mobile-keyboard evidence; no complete WCAG conformance claim is made. |
| U08 | VERIFIED-FOR-3.5 | Independently accepted actual writer-generation loss, stale same-key uncertainty, natural jittered backoff/coalescing and explicit recovery are retained as an exact rendered test in fixed/CI commands. Existing 210-event anchoring, modal focus, expiry and page-script cap/hidden controls remain accepted. Current page matches accepted U07 source after newline normalization. See U08_BROWSER_ACCEPTANCE.md; active P2 supervisor handoff and AT/mobile qualification remain separate. |
| U14 | VERIFIED-FOR-3.5 | Integrated passive shell, sanitized snapshot owner, foreground lifetime/no-hidden-daemon checks and native rendering satisfy the existing minimal preview cut. Queue evaluation is explicitly unavailable on this surface. Real P2 background supervision remains separate; see U14_OWNER_PRESENTATION_ACCEPTANCE.md. |
| G01 | VERIFIED-FOR-3.5 | Advisory council artifacts remain separate from decisions. |
| G02 | VERIFIED-FOR-3.5 | Public council pause/context-submit/continue, fixed authority/budgets and validated snapshots pass 22 independent protocol cases; seven guarded actual-process controls preserve admitted decoded context and external compatibility. Exact CI/release retention is verified. Checkpoint source is unchanged from accepted protocol source; only the separately accepted prompt writer/dispatch symbols changed. Other R12 adapters and live-provider qualification remain separate. See G02_CHECKPOINT_ACCEPTANCE_REVIEW.md. |
| G03 | VERIFIED-FOR-3.5 | Fixed-option setup remains explicit. |
| G04 | VERIFIED-FOR-3.5 | Only receipt-bound ballots count. |
| G05 | VERIFIED-FOR-3.5 | Durable-before-contact and recovery rules are provider-free verified. |
| G06 | VERIFIED-FOR-3.5 | Live activation, writable seats, resume, and alternate transports remain gated. |
| G07 | VERIFIED-FOR-3.5 | Bounded Fable technical advice does not confer whole-candidate or publication approval; human release authority remains primary. |
| G08 | VERIFIED-FOR-3.5 | Cross-kernel hostile-input, copied-authority, stale-generation, and missing-quorum fixtures pass provider-free; live activation remains separately gated. |
| L01 | VERIFIED-FOR-3.5 | All 26 tracked-baseline command-family entries remain; workspace is additive. Current parser diff, three repository skill entries, retained companion install/upgrade/foreign-preservation fixtures and accepted historical ID readers reconcile with the preview requirement. See the finite current crosswalk in F01_F17_L01_L03_SCHEMA_INVENTORY.md. F17 new commands and L02 full format migration remain separate. |
| L02 | VERIFIED-FOR-3.5 | Final 185-row/719-reference inventory, optional E12 replacement history and exact fleet 8/10/11 shapes are independently accepted and retained with all nine fixed/CI metadata files. The integrated nine-file command passes 212 cases; the restored unchanged reader assertion passes its focused case. Metadata AST/content retention and preserved evidence gaps are verified. See L02_L04_REMAINING_ACCEPTANCE.md; literal coverage is not universal runtime/historical qualification. |
| L03 | VERIFIED-FOR-3.5 | Read-only canonical and actual workspace reconstruction passes 19 cases; history, identity and uncertainty remain intact. See L03_ARCHITECT_REVIEW.md. |
| L04 | VERIFIED-FOR-3.5 | The lead independently passed the composed ordinary/Kimi-override/shared-junction rollback rehearsal in 105.117 seconds and verified exact canonical test retention. Windows CI has full history, Node and explicit prerequisites; the fixed migration_rollback registry includes both migration suites. Registry consistency independently passes and prerequisite logs omit machine paths. Final candidate/platform artifacts remain L09/L12, not publication qualification. |
| L05 | VERIFIED-FOR-3.5 | Telemetry remains optional/local and disabled by default. |
| L06 | BLOCKING | Repeat the exact public/export/privacy scan on the final staged candidate. |
| L07 | VERIFIED-FOR-3.5 | All 15 integrated Windows ACL-policy, inheritance, creation/reopen, actual junction/hardlink, export and failure-cleanup cases independently pass with zero skips. Tested source is unchanged; fixed browser_security and explicit Windows CI retention are verified. ACL-policy evidence does not claim separate-principal login, encryption or administrator isolation; missing facilities remain unqualified. See L07_OS_PRIVACY_ACCEPTANCE.md. Final candidate evidence remains L09/L12. |
| L08 | VERIFIED-FOR-3.5 | README, public/engineering changelogs, operator/troubleshooting guidance, capability and migration docs match the accepted preview scope. Exact seven-document retention and independent synthetic plan/public parser evidence are verified; released history and 3.4 markers remain. See R16_L08_DOCUMENTATION_ACCEPTANCE.md. |
| L09 | BLOCKING | Retain workspace core/recovery/UI machine evidence, rendered artifacts, skip inventory, and stale-consumer contract evidence. |
| L11 | VERIFIED-FOR-3.5 | Catalog/live-availability distinction remains explicit. |
| L12 | BLOCKING | Requires clean committed source, source-bound evidence, managed-copy convergence, privacy, rollback, and owner publication decision. |
| E03 | VERIFIED-FOR-3.5 | Live provider/account usage reads remain separately consented and unqualified. |
| E04 | VERIFIED-FOR-3.5 | Usage advice cannot switch account/provider/model/billing class. |
| E05 | VERIFIED-FOR-3.5 | Safe context compilation and bounded elision preserve bindings. |
| E06 | VERIFIED-FOR-3.5 | Audit/provenance/mutation facts are retained. |
| E07 | VERIFIED-FOR-3.5 | Provider-inert submission accounting and explicit context policy are accepted with the retained foreground/background/HTTP and workspace evidence, corrected metric/metadata boundaries, and independently passing actual parent-chat lifecycle (1 test, 6.53s; 252 source files unchanged). Estimates, reported usage/cache, opt-out, required-message boundaries and unknown spend remain distinct. Parent test and accounting/policy modules are retained in CI/release commands. See E07_SUBMISSION_ACCOUNTING_BRIEF.md; live-provider qualification and final candidate gates remain separate. |
| E08 | VERIFIED-FOR-3.5 | Editorial catalog and live evidence remain separate. |
| E09 | VERIFIED-FOR-3.5 | Astra/Flash catalog source/tests pass; live availability remains unqualified. |
| E12 | VERIFIED-FOR-3.5 | Actual refusal/explicit replacement forks, sealed-task approved-alias remap, post-durable-start definition drift and model/effort controls independently pass and are retained. The final two test files exactly match accepted bytes, and final L02 replacement/fleet mapping is retained. Historical identity and nonlaunching fork boundaries remain intact. See E12_ROSTER_ACCEPTANCE.md; installed served-model qualification remains separate. |

## Partial/open owner and next-evidence register

Every non-verified row above has an owner and a concrete next evidence item.
These are release-gate assignments, not permission to contact providers or
modify managed installations.

| ID | Owner | Next evidence required |
| --- | --- | --- |
| R01 | Runtime/provenance maintainer | Accepted at preview scope; preserve exact retained migration, qualification/revocation and queued-version packets through final candidate validation. No universal installed-adapter certification is implied. |
| R02 | Runtime/provenance maintainer | Accepted preview migration, historical compatibility and explicit non-launching fork evidence are retained; preserve them through final candidate validation. Additional provider qualification remains separate. |
| R03 | Trust/provenance maintainer | Preserve accepted VR-06 conflict stamping, exact-model refusal and the retained fixture matrix. Installed model certification remains separate. |
| R04 | Runtime/provenance maintainer | Accepted preview terminal classifications; preserve the retained auth/rate/empty/interrupted/missing-tool fixture matrix and final source-bound run. Historical reports are not current defects. |
| R10 | Runtime/provenance maintainer | Preserve accepted VR-05 midstream/final observational publication handling, strict durable-control failures and terminal accounting. Actual provider/process cleanup qualification remains separate. |
| R12 | Transport maintainer | Accepted at the documented local transport scope; preserve retained UTF-16/encoded-byte, exact-text, 64 KiB-capable and operator-body packets through final candidate validation. |
| R13 | State/capacity maintainer | Accepted bounded Windows/provider-free durability and reserve evidence is retained; preserve final source binding without claiming power-loss qualification. |
| R15 | Profile/account maintainer | Selected-profile continuity correction and rotation/unavailable-state/public-redaction controls are accepted. Preserve v2 binding and unsupported legacy refusal; never inspect or switch actual accounts for this gate. |
| R16 | Documentation/release maintainer | Accepted current preview reconciliation; preserve its source-bound labels and final candidate privacy requirements. |
| F01 | Workspace/protocol maintainer | Preview entity requirement accepted. Preserve exact producer/reader fixtures and schema mappings through final candidate freeze; broader format migration remains L02. |
| F02 | Workspace/security maintainer | Accepted at the issued-connection/trusted-host preview scope. Preserve current channel/runtime/handler fixtures and final source-bound evidence; do not silently expand this to arbitrary hostile same-user OS isolation. |
| F15 | Workspace/capacity maintainer | Accepted simulated coordinator admission/messaging/recovery matrix is retained; preserve exact source binding. External/live adapters remain later work. |
| F17 | Workspace/protocol maintainer | Accepted at bounded preview scope; retain current host/CLI, source-authority, capacity, compatibility and rendered recovery evidence through final candidate validation. |
| F18 | Workspace/QA maintainer | Preserve the accepted complete two-worker crash/fork/collision/identity matrix through final candidate validation. |
| U01 | UI/security maintainer | Retain task-shell origin/CSRF/session-scope/accessibility design and rendered evidence while legacy surfaces remain readable. |
| U02 | UI/product maintainer | Retain the one-focal-task rendered shell with objective, active work, messages, evidence, controls, and decisions. |
| U03 | UI/accessibility maintainer | Run long-journal anchor, mobile-keyboard, reconnect, and notification-jitter evidence. |
| U04 | UI/protocol maintainer | Retain every normative delivery badge/state and separate contact/spend certainty in the rendered surface. |
| U07 | Accessibility maintainer | Run measured WCAG 2.2 AA, assistive-technology, zoom/reflow, reduced-motion, and keyboard evidence. |
| U08 | UI/recovery maintainer | Retain stale/indeterminate recovery, owner-loss, bounded backoff, and authentication-expiry evidence. |
| U14 | CLI/supervision maintainer | Retain passive-versus-supervised lifetime, explicit owner, and no-hidden-daemon evidence. |
| G02 | Governance maintainer | Preview requirement accepted; retain the exact protocol/process fixtures and source bindings through final candidate freeze. Live-provider qualification remains separate. |
| G07 | Governance/release maintainer | Retain the separate Fable lane contract and human release-owner decision path; do not substitute model approval. |
| G08 | Governance/security maintainer | Preserve accepted hostile-input, copied-authority, stale-generation and missing-quorum fixtures through final candidate validation. |
| L01 | Compatibility maintainer | Accepted finite CLI/three-skill/legacy-ID preservation crosswalk; retain existing compatibility and companion fixtures through the final source-bound release run. New F17 mutations do not become accepted by this closure. |
| L02 | Migration maintainer | Freeze the capability/graph/wire/workspace/delivery/evidence schema inventory and stale-reader refusal fixtures. |
| L03 | Migration maintainer | Accepted; retain test_workspace_rebuild_acceptance.py in fixed commands and final L09 candidate verification. No new index writer is required. |
| L04 | Release/migration maintainer | Preserve accepted composed historical rollback and exact Windows CI/fixed-gate retention; final candidate/platform artifacts remain L09/L12. |
| L06 | Privacy maintainer | Repeat exact public-source, export, receipt, prompt, telemetry, and fingerprint scan on the final staged candidate. |
| L07 | Privacy/security maintainer | Retain OS-user ACL, reparse-path, inheritance, reopen, and export protection evidence. |
| L08 | Documentation/release maintainer | Accepted seven-document scope and retained history; preserve accuracy through final candidate validation. |
| L09 | Release-gate maintainer | Retain rendered/toolchain versions, machine-readable skip inventory, and stale-consumer contract evidence. |
| L12 | Release owner/release maintainer | Produce clean committed-source, source-bound evidence, migration/rollback, managed-copy, privacy, and owner-publication receipts. |
| E03 | Usage/provider maintainer | Add reviewed provider/account usage-read fixtures with explicit consent and no side effects; do not call providers for this closure. |
| E07 | Context/usage maintainer | Preserve accepted accounting, context-policy and parent-chat lifecycle evidence through final candidate validation; estimates and unknown spend remain distinct. |
| E12 | Roster/provenance maintainer | Complete the sealed stored-task alias-remap refusal journey and reconcile retained in-flight/model/effort coverage; replacement-name binding is already integrated. |

## Closure rule

The candidate is not release-ready while any required row is `BLOCKING` or
while L12 remains open. Every `PARTIAL`/`OPEN` evidence row must still have an
owner and next evidence in the register; that assignment does not make it
release-ready. The next closure packet must attach exact source-bound test
receipts and mark each disposition deliberately. Aggregate pass counts alone
are insufficient.
