# R15 selected-profile continuity

Status: VERIFIED-FOR-3.5, 2026-09-09. The revised v2-source candidate independently
passes all five guarded cases in 3.948 seconds, exit zero, without watched
script/fixture drift. Exact version dispatch and separate authentication domains
preserve strict v1 shape. Durable expected state, pre-load and post-load rotation,
unchanged control and missing historical binding now have bounded acceptance.
The lead also independently passed two unavailable-state/claim-binding/public
redaction cases in 1.541 seconds with no source drift. Typed refusal taxonomy,
canonical CI/release inclusion, separate v1/v2 format mapping and fresh-invocation
recovery wording are source-reviewed. The conductor reports 73 inventory and
111 adjacent resume/continuation tests passing. This closes the provider-inert
preview requirement without certifying real accounts or candidate providers.

Historical initial reproduction: the lead independently reproduced profile rotation
passing governed Claude resume admission: one unchanged control passes and one
desired refusal fails in 1.754 seconds. Eight relevant current profile, source,
claim, qualification, executor and emitter modules match the reviewed copy.
No provider process or real account state was used; guards report zero hits.

## Reproduced boundary

2026-09-09 helper follow-up reproduces rotation after actual loaded-child
validation succeeds: unchanged control passes, desired final refusal fails
(two cases in 1.545 seconds; zero guard hits). The actual resolver sees changed
selected state, but the launch-control writer still commits a claim and changes
the authenticated ledger. Contact remains unknown; no provider or alternate
profile is used. This is helper evidence pending independent lead packet review,
and confirms that an earlier validation-only fix would be insufficient.

The lead reviewed the completed incremental test source and ordering against
the helper handoff. It retains real loaded validation, selected-state resolution,
final launch control, typed refusal and byte-preservation assertions. The exact
complete or incremental source-only packet has been supplied to the conductor;
use one form only. Execution results remain helper evidence until independently
rerun; the original pre-validation case remains retained.

The fixture creates one selected synthetic Claude profile, authenticates a
continuation source and reserves its successor. Changing only that profile's
credential file changes the actual resolved state digest and original request
fingerprint. Name, path, registry and command identities remain unchanged.
Actual child validation accepts the changed selection; actual provider launch
control commits `launch_claimed`. Contact remains unknown because the fixture
does not execute a provider. This is a real admission defect, not proof that a
provider consumed a changed account.

The original request identity includes profile state. The dispatcher profile
receipt, strict continuation backend shape and loaded-child comparisons omit
that field. The successor's semantic request digest and executable/material
qualification do not restore this missing binding. Relevant source is in
`skills/summon/scripts/_profiles.py`, `_executor.py`, `run_subagent.py`,
`_job_continuation.py` and `_job_resume.py`.

## Architectural decision

2026-09-09 revised source now binds expected state durably and refuses missing
evidence before child load. Acceptance still requires an explicit version
boundary: the observed candidate accepts both old and extended backend fields
under source/v1. Preserve exact historical validation and introduce a distinct
authenticated format for new profile-state fields; historical records must not
be relabeled. Current source progress is not yet final implementation acceptance.

2026-09-09 lead source review rejects the initial in-memory-only candidate:
sampling current state at child load cannot detect rotation since source or
reservation creation. Returning no state after resolver errors and skipping
validation on missing state also violates fail-closed continuity. Production
compatibility exceptions for synthetic callers are not acceptable. Preserve
the final locked check, but derive its expected state from authenticated durable
source/reservation evidence; unsupported historical data stays readable and
requires explicit fresh qualification before launch. Add pre-load rotation and
missing-evidence controls without weakening earlier desired refusals.

Bind selected profile state through a coherent additive authenticated contract,
source, reservation and current launch validation. Do not silently widen exact
historical validators or relabel an old request as newly qualified. Historical
readability remains; missing state evidence is unavailable, not a match.

Changed state must refuse before durable launch admission. Explicitly authorized
revalidation may create a linked fresh binding after checking current evidence;
it must preserve original bytes and claims. A fresh invocation is also explicit.
Neither path changes the selected account automatically, refreshes credentials,
clears uncertain spend or authorizes provider contact merely by recording state.

The final admission check must cover rotation after loaded-child validation as
well as after reservation. Keep profile state/digests private; public status
needs only a bounded reason and supported recovery action. Named Codex resume
remains a candidate/refused path; its fresh and background selection coverage
must not be presented as certified continuation.

## Acceptance and integration

The approved reproduction contains an owned outer runner, preconsumer audit
fence, synthetic claim fixture and two-case worker. Actual source authentication,
profile resolution, qualification and claim writers remain intact. Executable
identity is measured from an owned inert file that is never executed. Synthetic
historical model/version facts do not qualify an installed provider.

The conductor owns correction and test integration after E07, coordinated with
E12 identity work. Retain the unchanged and rotated scenarios; adapt fixture
production only where the new explicit schema requires it, never to accept the
defect. Add rotation after child validation, supported explicit revalidation,
missing historical evidence, typed public refusal and no alternate-account
selection at the corrected consumer boundary. A final-fence refusal may use the
typed provider-control exception; tests must not demand only an earlier
validation exception. Preserve pending claims and prior contact uncertainty.

Reuse existing account selection/scrubbing/privacy, detached Claude publication
and Codex fresh/background/refusal evidence. Fleet rotation tests cover a
different authority path. No blanket provider matrix or real credential probe
is needed for this correction. No install, commit or release is authorized here.
