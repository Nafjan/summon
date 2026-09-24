# Expected next release: 3.5.0 workspace preview

Planning draft, updated 2026-09-19. Comparison baseline: released Summon
3.4.0, dated 2026-09-02 in CHANGELOG.md. **3.5.0 is committed locally but
has not been published.** This document describes the intended narrow P0/P1
preview and its remaining gates; it is not a publication or readiness claim.

The release boundary is explicit: the default `stable` manifest profile still
requires every fixed gate, including live-provider evidence. The reviewed
provider-free workspace cut may use the opt-in `workspace-preview` profile,
which accepts only the exact missing-live-evidence block while requiring every
other suite, gate, privacy, rendered, platform, migration and installation
condition. That profile is preview evidence, not stable/public-release
approval, and cannot be inferred from a version label or a blocked result.

Strict QA update, 2026-09-14: the corrected Windows aggregate completed all 27
fixed suites with 4,920 passed cases, 3 reviewed-policy skips and zero
failures/errors. It remains diagnostic until a clean source-bound 3.5.0
candidate exists; the missing live receipt, U03/U07 device evidence and final
L06/L09/L12 actions are still release blockers.

Strict QA update, 2026-09-18: a fresh quiet-host capture from the frozen
current tree completed all 27 fixed suites with 5,488 passed case outcomes,
zero failures/errors and exactly the three reviewed-policy skips, including a
combined-run browser-security pass and the complete 44-file rendered
collection. It remains diagnostic until a clean source-bound 3.5.0 candidate
exists; the missing live receipt, U03/U07 device evidence and final L06/L09/L12
actions are still release blockers.

## Release promise

Expected changes from 3.4.0, once release gates pass:

- A local workspace preview that keeps a goal, tasks, messages and evidence in
  one task-focused view, with versioned plan creation and explicit simulated
  worker journeys.
- Durable addressed context, supervisor inboxes and honest delivery/recovery
  states, including browser composition and encrypted pending-work recovery.
- Governed continuation migration, explicit refusal/fork behavior and an
  advisory council checkpoint for between-round human context.
- Scoped per-attempt usage and explicit context policy, with accepted
  provider-inert workspace and parent-chat lifecycle evidence.
- Stronger capacity, transport and Windows silent-launch handling, plus clearer
  identity, lifetime and recovery labels.
- Astra and Flash roster updates, preserving exact-model gates and the separate
  Fable review lane.
- More useful local diagnostics for trusted results without exact-model proof
  and clearer timeout-stage classification, preserving all model-evidence gates.
  This bounded candidate addition is tracked in TELEMETRY35_FINISH_LINE.md.

This release does not promise universal external-session attachment or real
provider-worker delegation from a workspace plan. Detailed verified evidence
and the remaining gates follow; none of these draft notes claims publication.

Current product-entry checkpoint: F17 is accepted for the bounded provider-free
preview. Ordinary host and authenticated client paths support context requests,
scoped dispositions, retained holds, authorized linked replacements and same-key
recovery. Independent checks cover source authority, capacity, compatibility,
concurrent writers, original-operation lookup after child advancement, and real
Chromium lost-response recovery through the ordinary host. Fresh host reinstall
is verified separately. Messages do not grant authority or launch workers;
unknown effects and immutable parent lineage remain preserved. Terminal-guidance
and scoped task/artifact navigation are independently accepted with exact
source and fixed/CI retention; U08 owner-loss/recovery is also accepted.
R03/R10 corrections and regression retention are accepted. U03/U07 and final
candidate gates remain open. The release matrix records 58 verified requirements
and 5 blocking requirements (92% rounded),
not release readiness. Documentation reconciliation
is accepted in R16_L08_DOCUMENTATION_ACCEPTANCE.md. Windows ACL-policy privacy
acceptance is complete at the documented scope, with zero-skip execution and
fixed release/Windows CI retention; final candidate evidence remains required.
FINAL_VALIDATION_TRIAGE.md records the independently reproduced provenance,
heartbeat and capability-snapshot gaps, their accepted integration, and the
remaining candidate-evidence boundary. These are reliability corrections to
the planned preview; they are not new feature promises.

Rollback qualification checkpoint: the current-to-3.4.0-source-to-current
rehearsal passes in owned synthetic homes across ordinary, Kimi-override and
shared/junction installation layouts. It preserves pending encrypted work,
approval state and uncertainty. Windows CI and the fixed migration gate retain
the rehearsal. L04 is verified for this preview scope; final candidate platform,
source and publication evidence remain required. This is not a real managed
installation change or proof that 3.5.0 has shipped.

Encrypted recovery acceptance, 2026-09-08: sixteen actual page-script/WebCrypto
cases independently verify supported pending-record recovery, refusal of
incompatible record and authenticated-data versions, exact ciphertext and
operation retention, and compatible recovery after explicit reauthentication.
This test-only packet is integrated with canonical suite retention and validated
format references. It does not establish real
browser storage, host-key or historical-producer compatibility by itself.

Verified browser recovery checkpoint, 2026-09-08: incompatible workspace-view
responses preserve the last confirmed view and encrypted pending work, block
unsafe replacement actions, and expose an explicit retry. Transient server
errors keep bounded automatic reconnect behavior. The integrated page and
format-inventory packet passes 104 focused tests; complete field-shape and
rendered accessibility qualification remain open.

Submission accounting is being completed for the release: distinguish scoped
input estimates, provider-reported tokens/cache and unknown values for each
attempt, including retries and interrupted background work. Ordinary foreground
calls keep their existing ephemeral storage behavior. Explicit task context
policy and workspace settlement/reopen now have accepted provider-free journey
evidence. Contradictory usage-field handling has been corrected; independent
checks confirm that public summaries exclude malformed numeric metrics while
retaining valid reported metrics in a partially malformed record. Parent-chat
completion/interruption and fresh-runtime recovery now pass independently, as
do the corrected metadata checks. The parent-chat and accounting/context test
modules are retained in CI and the release command. E07 is accepted for the
provider-inert preview; token reports cannot clear uncertain spend or imply new
authority. This is a verified candidate feature, not a shipped release claim.

Migration compatibility is verified for the existing certified preview lane:
historical v1 rooms remain readable, explicit forks create separate v2 lineage,
and candidate continuation remains gated. This does not certify new providers
or native-session attachment. Selected-profile continuity is now independently
accepted at the preview scope: seven guarded synthetic cases cover durable v2
profile-state binding, rotation and missing-evidence refusal, legacy v1 refusal,
claim binding and public redaction. This does not certify another account or
provider. Fresh invocation is the supported recovery path; a future revalidation
migration is not claimed as implemented.

Verified publication and owner-presentation checkpoint, 2026-09-08: ordinary
dispatch continuation intake now accepts the executor's canonical CLI route
while preserving the historical private sidecar format. Public `--out` excludes
private launch observations without losing them before private sealing. Five
actual-main synthetic publication cases pass against integrated source. The
workspace clearly distinguishes a passive page, its explicit foreground server
and the last confirmed owner snapshot; connection loss and session expiry do
not imply worker completion. U14 is accepted for the existing minimal preview.
Full recovery, accessibility, migration and final release gates remain open.

Council protocol and public-entry acceptance is integrated: a caller may pause a
two-round advisory council after the first round, submit bounded human context,
and explicitly continue under the original deadline, models, permissions and
budgets. Context remains advisory. This does not add deliberation ballots or
automatic approval. Twenty-two guarded cases pass independently, including the
actual public-main lifecycle. Seven additional guarded actual-process controls
now verify exact admitted context through the intermediate child-dispatch hop,
and their exact canonical retention is confirmed. G02 is verified for the
provider-free preview; final adapter-specific transport limits and live provider
qualification remain separate gates.

Verified local compatibility/evidence checkpoint, 2026-09-08: workspace help
and result envelopes explicitly identify the preview. Ordinary swarm readers
refuse unsupported preparation schemas, mismatched run identities and wholly
torn preparation before repair, while supported recovery remains tested.
Release collection now records actual custom-harness skips; strict consumers
reject unknown/private artifact and suite-wrapper fields instead of trusting a
recomputed checksum. These scoped fixes are integrated and independently tested;
full migration, rendered UI, platform and final release gates remain open.

Verified transport checkpoint, 2026-09-08: native ZCode subprocess dispatch now
derives an 8 MiB private-attachment budget from its adapter, for both fresh and
resumed turns. Independent synthetic-child tests verify exact payload bytes
above 64 KiB, refusal above the declared limit, attachment cleanup and explicit
host-platform mismatch refusal (16 focused tests). This qualifies local
transport behavior, not an installed ZCode session or provider. The corrected
Windows version-probe descendant fixture also independently verifies owned
cleanup; it does not establish the source of unrelated workstation popups.

Verified chat migration checkpoint, 2026-09-08: `chat revalidate` consumes a
separately authenticated private packet without launching a worker or rewriting
the original journal. Independent synthetic tests cover supported legacy-family
migration followed by continuation, invalid authority/qualification refusals,
and bounded malformed-input errors. Live evidence issuance and operator trust
provisioning remain gated; this does not enable arbitrary native-session attach.

Verified UI correction, 2026-09-08: the workspace can open and keep retrying a
reachable local server despite a browser offline hint. Losing the local server
preserves the last confirmed task view and reports connection uncertainty.
The scoped tests pass; full rendered recovery/accessibility gates remain open.

Help a conductor keep a goal in focus, inspect a durable task/message model, and choose the next iteration from one local workspace. This is an additive provider-free workspace protocol preview: existing dispatch, chat, council and deliberation entry points remain, and their permission and decision boundaries stay explicit. It does not yet bridge a plan to a real provider worker or attach to an external session.

The target includes a durable task/message model, explicit scoped context controls, a task-focused browser surface, and truthful interruption/recovery states. The release demonstrates these provider-free protocol journeys with explicit simulated fixtures; it does not promise real agent delegation, a worker attach bridge, live-model autonomy, or external-session attachment. Those require later, separate adapter qualification.

Current verified scope includes real local cancellation/disposition controls, same-key durable reconciliation, simulated agent messaging, fresh-process endpoint recovery and bounded same-task recovery at queued/included/submitted boundaries. The human-authored message journey passes its bounded provider-free source/runtime/HTTP/Node and rendered-browser gates, including encrypted reload recovery, unreadable-record freezing, explicit authority-loss presentation and same-key reconciliation to exactly one message event. The supported foreground entry also passes its real parser, interactive server, authenticated browser, persisted reopen, expiry and truthful cleanup journey. The versioned user-supplied plan and ordinary non-synthetic `workspace create --plan FILE` preview are implemented provider-free; worker/provider/session execution, a retained standalone UI-suite receipt, full release regressions, roster review and release/privacy reconciliation remain blockers. The Windows idle-client saturation test passes alone but remains classified as host-load-sensitive until the release gate runs under a quiet host. Detailed scopes and overlapping counts belong in PROJECT_LEAD_STATUS.md, and the next implementation order is in RELEASE35_IMPLEMENTATION_HANDOFF.md. No aggregate percentage or release date is implied.

## Expected user-visible changes versus 3.4.0

Required migration announcement for the candidate: governed continuation now
uses the versioned capability registry and authenticated current qualification.
Claude retains its eligible governed path when evidence can be revalidated;
Codex, Cursor, OpenCode and ZCode candidate lanes cannot resume merely because
old chat history contains a handle. Unsupported or unqualified continuation
returns a typed no-contact refusal. Users may explicitly fork while preserving
history and the new prompt; creating that fork starts no provider or child turn.
Steering remains queued for a later resume boundary. Ordinary fresh dispatch
and historical record readability are preserved; no automatic native-session
attachment, model certification or account change is implied. The conductor
must retain this announcement in the public Unreleased changelog and reconcile
the capability document before release.

Continuation implementation checkpoint, 2026-09-08: the candidate includes
`summon jobs revalidate JOB_ID --evidence-file FILE` and
`summon chat revalidate SESSION_ID PARTICIPANT --evidence-file FILE`. Both
consume private authenticated observation/qualification packets without
launching a child or provider and preserve historical records. Legacy chat
rooms without a family sidecar additionally require the separately held
`SUMMON_CHAT_MIGRATION_KEY`; the public packet cannot self-authorize that
authority. Retained synthetic tests cover successful migration and continuation,
forged/expired/wrong-key refusal, and malformed-input privacy. This is not an
installed-provider qualification issuer, universal resume support, or live
trust-key provisioning; those remain separate release gates.

| Area | In 3.4.0 | Intended change in the next release | Current readiness |
| --- | --- | --- | --- |
| Goal-directed work | Dispatch, jobs, chat, council and local swarm provide separate building blocks; the host conductor coordinates them. | A provider-free workspace preview organizes a goal, acceptance criteria, tasks, attempts, messages and evidence for inspection and explicit simulated protocol work. The conductor retains decisions and completion judgment. | The plan/create/open/inspect and simulated protocol journeys are verified at provider-free scope. Real provider-worker delegation, result import, and autonomous live-model planning remain outside this preview. |
| Agent messaging | Addressed room inboxes exist; that does not prove inclusion in a provider turn or universal session attachment. | Bind each selected complete message to the intended recipient and simulated attempt; preserve ordering, duplicate identity, delivery state and recovery disposition across restart. Show queued, included, submitted, acknowledged and held states truthfully. | Provider-free simulated worker/local-operator protocol journeys are independently verified. No public worker attach, real provider submission, result import, or native-session bridge is included; those remain separately gated. |
| Task-focused UI | Existing preview atlas/chat and separate review/decision surfaces. | Add a minimal task-first shell with the goal, active work, messages, evidence and explicit typed controls in one focal area. Require stable scroll/composer behavior, accessible interaction and clear reconnect/delivery states. | The local operator controls, human-context composer and supported launch/reopen command have passed bounded real-browser journeys for keyboard flow, responsive layout, reload/authentication, encrypted recovery, uncertainty, revocation and same-key reconciliation. Full release accessibility/toolchain evidence remains open. |
| Continuation and migration | Preview continuation eligibility is not consistently governed by one operation-specific capability boundary. | Recheck trusted qualification and launch bindings before continuation; refuse missing, unsupported or changed evidence with a clear reason. Keep history/drafts intact, provide explicit non-launching job/chat revalidation where the adapter supports legacy migration, and retain the explicit fresh-conversation/fork action. | The certified preview lane has accepted guard, ownership, environment, migration, candidate-refusal/fork and queued-version evidence with exact canonical retention. Missing selected-profile legacy state remains unsupported. Installed/live qualification and operator trust provisioning remain separate. |
| Interruption and capacity | Local coordinator ownership, leases and recovery already exist. | Strengthen exact-byte journal writes and successor reconciliation; retain enough journal space to cancel, settle or hold admitted work. Prevent incomplete claim/message admission and preserve uncertainty after interrupted writes or contact. | R13 core accepted in a bounded Windows/provider-inert scope. Atomic-capacity fixtures independently cover late evidence through acknowledgement, interrupted delivery and close refusal. Explicit effect resolution is independently verified for owned simulated workers; provider effects and full operational preview qualification remain gated. |
| Windows process visibility | Hidden-launch mechanisms exist, with gaps in some owned tooling/test launch paths. | Apply shared silent-launch flags to corrected owned paths and add regression guards, including generated test children. Publish tested caller guidance. | Bounded source corrections verified. This is not a claim that every observed workstation popup is fixed, nor an installed update. |
| Submission accounting | Existing usage evidence and context estimates do not establish complete per-attempt attribution. | Show scoped estimates and reported token/cache values with missing coverage for every attempt; retain uncertainty across retries/interruption and make context policy explicit. | E07 provider-free accounting, conversation/workspace policy and actual parent-chat lifecycle evidence are accepted and retained. Final candidate and live qualification remain separate. A process launch does not establish confirmed provider submission or complete cost. |
| Model roster | Fable 5.1, existing Codex seats and other routes are already available as catalog entries. | Add exact-pinned GPT-6 Astra, default high with explicit xhigh/max support; add the advisory flash-reviewer seat and Gemini Flash 3.8 roster updates. Correct AGY Gemini slug effort selection. Keep Fable as the separate skeptical escalation/review lane. | Pending source/roster changes exist and must pass their separate final review and release gates. Catalog entries do not certify live availability or served identity. |
| Guidance and clarity | Existing operator, profile, usage and context features are documented across surfaces. | Add practical conductor guidance for bounded delegation, stopping conditions, evidence-based integration and avoiding unproductive investigation loops. Clarify requested effort versus generated CLI configuration/provider evidence, preview continuation, local lifetime, delivery and recovery boundaries. | Conductor and effort guidance implemented; onboarding, troubleshooting and release-document reconciliation remain planned. |

## Draft release notes, to publish only after the gates pass

### Added

- A provider-free local workspace preview for inspecting a goal, tasks, attempts, messages and evidence, with a minimal task-first interface.
- An explicitly driven simulated protocol demonstration. It does not delegate real provider work, attach an external worker, import provider results, or certify autonomous live-model performance.
- Durable addressed-message selection and delivery tracking, with immutable attempt binding, explicit acknowledgements and recovery dispositions.
- A taskless supervisor inbox, so a worker can report to its conductor without creating a dummy task. Messages carry context; they do not grant permissions, approve work or cast votes.
- Scoped context controls with durable operation lookup, so an interrupted browser response cannot silently cause a second command. Human context composition, the foreground executable entry, and versioned `workspace create --plan FILE` have passed bounded provider-free rendered/entry qualification. Plan creation is an accepted provider-free preview; the integrated release gate remains before this can be advertised as a released feature.
- An explicit between-round human-context checkpoint for advisory council, preserving the original execution limits and requiring an explicit continuation. Protocol, public lifecycle and exact intermediate child transport acceptance are integrated; downstream adapter limits still apply.
- GPT-6 Astra and Gemini Flash 3.8 roster updates, including a separate advisory Flash review seat. Exact model claims continue to require authoritative served-model evidence.

### Improved

- Goal-focused orchestration guidance: helpers own bounded detail work; the conductor retains priorities, integration decisions and the completion criteria.
- Journal durability, cancellation/recovery capacity and atomic task/message admission, including fail-closed treatment of uncertain writes and interrupted work.
- Silent launches on the corrected Summon-owned Windows tooling/test paths, with regression coverage and caller guidance.
- Delivery, model/effort and continuation labels; preview migration and troubleshooting documentation.
- Correct canonical CLI continuation intake and private launch-observation exclusion from public dispatch output, while retaining the private evidence needed for continuation checks.
- Browser reading stability: responsive task/timeline layout, keyboard focus preservation, bounded refresh coalescing and anchored history updates. Full reconnect and active-control behavior remain release gates.
- Preserve encrypted pending work and the last confirmed view when the browser receives an unsupported workspace-view response; keep explicit retry separate from transient reconnect handling.
- Per-attempt usage completeness and context-policy reporting, preserving unknown spend and ordinary foreground storage behavior. E07 is accepted at the provider-inert preview scope; final candidate and live-provider qualification remain separate.

### Changed

- Saved-session continuation is checked as a specific capability with separately authenticated qualification and current launch evidence. Unsupported, unqualified or incompatible continuation refuses before contact and preserves history/draft. Explicit revalidation records current evidence without launching work or rewriting historical facts; it is available only where the adapter's legacy-compatibility policy permits it. The separate fresh-conversation/fork action remains explicit. R02 migration is accepted for the existing certified preview lane; additional provider/native qualification and final candidate evidence remain gated.
- Council remains advisory review; fixed-option deliberation retains its separate receipt, quorum and ballot rules. The workspace presents these typed modes without merging their authority.

## Compatibility and exclusions

- Preserve existing CLI/skill entry points and readable historical room/run records. Do not rewrite historical receipts or relabel retired Ox/stealth identities.
- The new workspace uses a versioned layout and explicit preview boundaries; do not silently migrate or resume old work.
- Claude's existing governed subprocess continuation remains subject to its current checks. Candidate Codex, Cursor, OpenCode, ZCode, AGY/Gemini/Kimi continuation routes do not become certified merely because this version ships.
- Universal messaging into already-running Claude Code, Claude Desktop, Codex or Antigravity sessions is **not included**. Each external-session operation needs a verified adapter and its own qualification.
- True universal mid-turn steering, autonomous multi-hour supervision, dependency-graph execution, 10+/16-worker qualification and native swarm adapters remain later milestones.
- Full first-run subscription/account discovery, broad credential-repair automation, live credit queries, automatic model/account selection and the complete atlas redesign are not bundled into this narrow preview. Existing supported profile/usage behavior remains; no silent account, model or billing changes are added.
- Existing dispatch, manifests, council, ACP, adaptive jobs, usage/context/fleet scaffolding, portable results, chat, local swarm and provider-inert deliberation are not being reannounced as new features. In particular, Fable 5.1 and named Codex account homes already shipped in 3.4.0.

## What must be true before this becomes a release changelog

1. The actual coordinator-backed conductor and addressed-message journeys work, including restart, duplicates, stale identities, scoped holds, whole-context binding and truthful final delivery/attempt states.
2. Admission reserves the complete remaining task and delivery settlement paths at measured capacity boundaries. Preserve the independently accepted R13 reserve/recovery tests in the final candidate; the earlier reproduced reserve failure is historical, not a reason to restart its investigation.
3. The shipped task-first UI passes its rendered accessibility, scroll/focus, reconnect, identity and privacy checks. A console-only synthetic demo does not satisfy that UI gate.
   The bounded executable entry includes `summon workspace create RUN_ID --plan FILE --runs-root DIR` for provider-free versioned-plan creation, plus `workspace open` and `workspace inspect`; the explicit `workspace demo create` fixture remains qualification-only. Its parser, server, bootstrap handoff, reopen and foreground stop path are accepted at provider-free preview scope. Plan creation does not launch workers/providers/sessions or grant mutation authority; the complete candidate still needs the integrated release gate.
4. Capability migration, ordinary dispatch and typed council/deliberation compatibility pass the required affected regressions. Provider-inert evidence never substitutes for qualification of a claimed live provider operation.
5. Pending roster changes are independently reviewed; README, changelogs, migration and troubleshooting text agree with the actual supported capabilities. Public artifacts pass privacy review.
6. Required checks, migration/rollback and managed-install convergence are bound to the deliberately reviewed and committed release source, followed by the separate release-owner publication decision. No installation, commit or release is authorized by this document.

If the minimum preview cannot satisfy these gates, revise the target release plan explicitly. Do not silently call the completed helper subset the promised workspace release.

## Source and planning basis

- CHANGELOG.md: released 3.4.0 baseline and existing Unreleased roster entries.
- WORKSPACE_CHECKLIST_V3.md: normative preview cut, recovery table and later-phase exclusions.
- CONDUCTOR_LOOP_DIRECTION.md and P1_VERTICAL_SLICE_BRIEF.md: the actual goal-directed demonstration.
- PROJECT_LEAD_STATUS.md and LEADERSHIP_AUTHORITY.md: current verified scope and active implementation gaps.
- R02_PREVIEW_MIGRATION_NOTICE.md: affected continuation behavior and compatibility.
- WINDOWS_IMPLEMENTATION_REVIEW.md and W06_DISCOVERY_HANDOFF.md: bounded Windows corrections and remaining uncertainty.
- skills/summon/references/orchestration.md and skills/summon/references/effort.md: implemented host guidance and evidence distinctions (repository-root-relative references).

HANDOFF: maintain this expected changelog when scope or verified readiness changes. Keep the existing release changelog authoritative for what has actually shipped. Publish only items whose declared release scope has passed its gates; retain clear preview and simulated/live distinctions.
