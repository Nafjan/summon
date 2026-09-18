# F01 named-entity acceptance

2026-09-08. Status: VERIFIED-FOR-3.5 at the current provider-free entity scope.
The requirement in `WORKSPACE_CHECKLIST_V3.md`
names eleven entities. The existing inventory tool exposes thirteen names,
including supplementary resume and turn-budget mappings. Those counts describe
different scopes. Neither a passing inventory validator nor the existence of
a schema literal establishes complete producer/reader acceptance.

The following is a source-inspected crosswalk, not a new claim that all tests
were executed. Source paths below are relative to `skills/summon/scripts/`.

| Required entity | Actual representation and identity | Writer / reader / inspected evidence |
| --- | --- | --- |
| Workspace | `summon.workspace/v1`, workspace/run identity and feature marker | `_workspace_protocol.py` validators; `_workspace_state.py` reducer; `test_workspace_state.py` feature-marker and old-reader refusal cases. |
| Task | Versioned plan task plus task/lane binding | `_workspace_plan.py`, `_workspace_protocol.py`; plan/compiler and state readers; strict plan and authority-field tests. |
| Attempt | Embedded task/claim/attempt/owner/request/context identity | `_workspace_protocol.py`, `_swarm_protocol.py`; coordinator/state admission and claim-selection tests. |
| Principal | Scoped local operator and host configuration identity | `_workspace_protocol.py`, `_workspace_entry.py`; exact host v1/v2 readers and legacy identity-preservation tests. No universal identity-verification claim. |
| WorkerInstance | Registered instance plus endpoint epoch/incarnation | `_swarm_protocol.py`, `_workspace_protocol.py`, `_swarm_coordinator.py`; registration binding and conflict tests. |
| Message | Separate strict worker/operator message variants | `_workspace_protocol.py`, `_workspace_admission.py`; sole-writer admission, immutable content/digest/recipient and operation replay tests. |
| Delivery | Versioned delivery record with independent state and certainty | `_workspace_protocol.py`, `_workspace_state.py`; lifecycle, immutable facts and uncertainty-retention tests. |
| Command | Supervisor inbox and local-operator command variants | `_workspace_admission.py`, `_workspace_commands.py`; current-authority validation and durable command observation tests. |
| Artifact | Swarm artifact publication frame, claim binding and artifact ID | `_swarm_protocol.py`, `_swarm_coordinator.py`; containment, claim fences and reopen tests. Identity-reuse/reconstruction consistency is under dedicated acceptance. |
| Review | Strict task-linked workspace assessment, identified by `assessment_id` | `_workspace_protocol.py` `_assessment`; `_workspace_state.py` unique assessment storage; `_workspace_view.py` detached assessment projection and non-approval tests. |
| Decision | Unique assessment-bound workspace next-lane decision | `_workspace_state.py` `next_lane_decision`; `_workspace_view.py` separate decision projection; dangling/stale/duplicate target tests. |

## Architectural disposition

The existing assessment is the preview Review representation: it binds the
goal revision, task, lane, criterion results, evidence, disposition and limits.
The requirement calls for canonical identifiers; it does not require renaming
`assessment_id` to `review_id` or adding another authority store. Preserve the
existing strict immutable schema and context-only authority.

Workspace next-lane decisions are separately identified orchestration records.
They do not become council approvals or fixed-option deliberation ballots.
The routing schema in `_decision.py` and deliberation context bindings do not
by themselves establish the required workspace Review/Decision entities.
Council recommendations and fixed-option deliberation retain their own typed
authority and compatibility requirements.

The corrected Artifact inventory binding now names actual swarm publication
and reconstruction; effects observations remain a separate row. Workspace
Review/Decision bindings now name workspace protocol/state/view representations,
while routing and deliberation mappings remain separate. The lead inspected
these mappings and passed two current schema/format reference validators in
0.72 seconds with four watched files unchanged. This accepts the semantic
mapping correction, not all producer/reader behavior.

## Required closure work

Final F01 closure, 2026-09-08: the eleven-row source crosswalk, corrected machine
mappings, existing strict producer/reader controls, accepted Artifact and
Review/Decision packets, and the 18-case strict entity packet jointly satisfy
the explicit schema/validation/canonical-identifier requirement. The lead
verified the final strict test matches the independently passed packet, all
27 source/fixture dependencies remain unchanged, and CI/release/exact inventory
references retain it. The current schema-reference validator passes in 0.06
seconds. Artifact's four-case canonical retention is separately verified above.
No current entity implementation gap remains established by this audit.
This does not close unrelated format migration (L02), live principal or adapter
qualification, or all CLI/API projection requirements (F17). Earlier pending
integration statements below are historical checkpoints.

Current integration state: the semantic mapping correction is accepted. The
approved four-case artifact test is now verified retained verbatim apart from
line endings/trailing whitespace, with all nine runtime dependencies unchanged.
CI, the canonical release suite and exact inventory method references retain
it; the current schema-reference validator passes in 0.08 seconds. The three
direct strict-entity gaps below now pass independent acceptance: all 18 cases
pass in 8.38 seconds, including supported producers and public durable readers,
with unchanged authoritative state/nonempty journal bytes on refusal. All 27
source/fixture dependencies match and guard hits/source drift are zero. The
complete portable test is approved for exact canonical integration. Earlier
statements that the machine bindings still point only to effects or deliberation
are superseded. No unchanged broad inventory run is required.

Remaining-entity source audit, 2026-09-08: all eleven representations and
identifiers are present; no new runtime defect was established. The inspected
tests lack direct negative controls for missing/future task-plan schema and
duplicate task IDs, malformed worker registration and same-worker/different-
instance registration across reopen, and missing/future operator-command
source schema at the request reader. One bounded isolated test-only packet is
assigned for these gaps, retaining supported controls and unchanged state on
refusal. Twenty-one inspected source/test files remained stable; the audit ran
no tests. These are evidence gaps, not reasons to introduce additional schemas.

Artifact correction checkpoint, 2026-09-08: the lead reviewed the complete
portable test and guard and independently passed all four cases against a
fresh current-source copy. Identical/conflicting metadata under a new frame
refuses before append; original frame replay before and after reopen retains
one publication; a fresh ID still succeeds; ordinary and strict reconstructed
artifact records agree. Journal bytes remain unchanged on refusal and replay.
Nine runtime dependencies match the reviewed packet, shared drift and guard
hits are zero, and owned fixtures are cleaned with no live handles. This
supersedes the defect characterization below. Canonical test retention and
actual Artifact/Review/Decision machine mappings are now verified retained as
recorded above.

Test-only integration is accepted, 2026-09-08: the shared file matches the
approved packet after line-ending normalization and canonical workspace-core
commands retain it. Current inventory-reference validators pass. The machine
Review/Decision row still names routing/deliberation producers; adding the new
workspace fixture does not correct those schema/producer/consumer mappings.
That previously assigned semantic correction and artifact acceptance remain.

Review/Decision behavioral checkpoint, 2026-09-08: twelve new test-only cases
and twenty-one existing controls independently pass in 1.13 seconds. The lead
inspected the complete patch, guarded runner and actual reducer identity rules;
six inspected dependencies match shared source. Missing/future protocols and
unknown/approval fields refuse without state mutation. Fresh-operation reuse
of existing Review/Decision IDs refuses, while exact-event replay is retained.
Existing controls cover scoped evidence, stale/dangling decisions, detached
projections and holds that later positive assessments cannot release. Guard
hits are zero, runtime fixtures are cleaned and no live handles remain.

The test-only packet is approved for conductor integration with the corrected
mapping/artifact work. It exercises synthetic event construction through strict
validation, reducer and projection. Actual conductor admission, journal reopen
and historical producer equivalence were not exercised. Two handcrafted corrupt
view states remain defensive observations: no supported persisted replay path
to them was found, so they are not new release blockers or runtime changes.
Neither this bounded evidence nor inventory metadata alone closes F01.

1. Correct the machine inventory's Artifact and Review/Decision mappings;
   expand task, worker incarnation and command variant references where needed.
   Preserve the inventory tool's existing API and distinguish supplementary
   names in documentation rather than removing valid mappings for counting.
2. Retain supported producer-to-reader identity checks and unknown-version,
   unknown-field and conflicting-identity refusals for the corrected mappings.
   Reuse existing behavioral evidence when it covers the actual requirement;
   do not invent historical versions of newly introduced records.
3. Resolve the now-reproduced artifact publication/reconstruction discrepancy.
   Three isolated characterization cases pass in 1.340 seconds: exact-frame
   replay retains one event and rebuilds, while identical or conflicting
   artifact bodies under a new frame produce duplicate IDs that reopen accepts
   and strict reconstruction refuses. These passes prove the defect, not a fix.
   Root inspected the complete test and source. Preserve reconstruction and
   existing frame-replay behavior. A new frame must refuse a previously used
   artifact ID before durable append, even for identical metadata; a new
   artifact/version requires a fresh ID. Do not deduplicate old journals.
4. Independently verify the coherent shared packet and retain it in the release
   suite before changing F01's disposition. Unrelated fleet/usage mappings stay
   in L02; actual shared protocol and historical-reader dependencies remain.

The conductor may implement the artifact correction within existing scope and
return independent desired-behavior acceptance. Adapt the isolated packet's
fixture/guard roots for repository execution before integration: temporary
storage must be explicitly owned, not inferred from a source directory's
ancestor. Retain one-publication reopen/rebuild and unchanged-journal controls.
Runtime acceptance is limited to the explicitly verified identity boundary
above. No whole-F01 closure, provider qualification, release percentage
promotion or publication is claimed by this document.
