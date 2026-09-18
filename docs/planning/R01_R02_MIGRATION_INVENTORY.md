# R01/R02 resume and steering migration inventory

Status: bounded provider-free inventory and pre-contact comparison slice,
2026-09-06. The machine-checked inventory now includes the continuation and
jobs projection paths listed below. The current migration slice also proves
that historical v1 capability/refusal rows remain readable without becoming
current launch authority, and that a legacy session-wide process record can be
inspected without creating a participant-scoped runtime directory. This
document does not certify a provider, grant a launch, or complete the R02
runtime migration.

## Current acceptance checkpoint

R01 is VERIFIED-FOR-3.5 at the provider-free preview scope. The exact approved
queued-version and candidate-fork packets are now integrated and referenced by
CI and the fixed release command. All six files across the R01/R02/R12 packets
match their approved text, and the lead independently ran the three integrated
entry tests: 12 passed in 18.34 seconds. R02 still needs public capability/
changelog reconciliation and the newly introduced historical-v1 explicit fork
correction recorded in L02_L04_REMAINING_ACCEPTANCE.md. New current-v2 candidate
fork acceptance does not establish that legacy-v1 migration path.

Source reconciliation, 2026-09-08: the conversation runtime now consumes the
versioned registry and no longer contains the duplicate backend resume allowlist.
Exact v1 compatibility remains intentional; it is not current launch authority.
Existing synthetic launch-chain, background-publication and public chat migration
fixtures cover complementary producer/consumer boundaries. Their local evidence
does not certify an installed provider, and no universal live re-probe is required
for the preview. The older matrix wording about pending allowlist removal is stale.

R01-JOB-ORDER-01 is corrected. The lead reviewed the source-bound admission lock
shared by qualification revocation and validation-through-durable-claim admission;
lock order is source then ledger. The retained gate/main concurrent barrier and
before/after controls independently pass all six cases in 3.76 seconds against
unchanged lock sources. Revocation completed before admission wins; revocation
after admission is not retroactive cancellation. CI and the fixed release command
retain the exact regression. Authentication and historical formats remain intact.

Candidate-lane acceptance independently passes four cases in 1.75 seconds against
matching conversation, registry and family sources. Actual runtime entry refuses
Cursor, OpenCode and ZCode candidates and the unsupported AGY control with zero
attempts and unchanged parent files. Explicit fork preserves history and prompt
without starting a child turn. These are public Python runtime methods with
synthetic unqualified history, not CLI/browser or live-provider certification.
Canonical retention is assigned. The final source/material, replay and historical
compatibility crosswalk still governs whole R01/R02 closure.

Final R01 queued-version acceptance independently passes four cases in 3.02
seconds with ten current job/executor/qualification/emitter sources matching.
After reservation, changed registry generation, adapter version or externally
reported version reaches the real executor fence with executable bytes unchanged.
All three refuse before the CLI body, preserving exact authenticated ledger bytes,
pending claim and contact None. The supported control reaches the synthetic CLI.
Actual public output and stdout emitters preserve typed no-contact refusal and
exclude private observations and false pre-dispatch scope. The lead also inspected
main's governed executor-to-emitter path; the fixture calls emitters in process,
not a separately spawned public dispatcher. Historical qualification is synthetic.
Canonical retention is assigned; no new runtime defect was found.
R02 behavior is supported pending candidate-fork retention and the public preview
migration announcement. Supported historical formats are explicitly scoped;
unspecified additional historical shapes are not an invented release gate.
The two old R01 contract-regression failures are superseded by the controlled
fixtures and final approval in P0_IMPLEMENTATION_REVIEW.md, not live defects.

The following table records earlier boundary-by-boundary evidence. The current
acceptance checkpoint above supersedes its older remaining-work descriptions;
an older defect description does not mean that defect remains present. R02's
explicit current remainder is the public notice and historical-v1 fork path.

| Required path | Current evidence | Remaining acceptance |
| --- | --- | --- |
| Job qualification and revocation | A queued unchanged qualification is refused after separate revocation; the durable provider claim remains pending. | Complete host observation/material provenance and interrupted-boundary matrix. |
| Legacy job revalidation | Supported synthetic history gains a linked binding; forged/revoked packets refuse before changing stored bytes. | Full compatibility and interrupted-publication coverage; installed evidence issuance stays separately gated. |
| Public job entry | Three retained tests exercise the actual dispatcher with child launch forbidden, covering supported, forged and revoked evidence. | These prove synthetic packet consumption, not installed-provider qualification. |
| Chat continuation | Six independently executed synthetic real-runtime cases prove explicit qualification enables continuation, the next turn does not self-qualify, and forged/expired/revoked/wrong-turn/changed-observation packets refuse. Guard/revocation ordering and missing-history refusal are independently corrected. | Full host observation/material provenance, explicit compatibility coverage and interruption matrix. |
| Public chat migration | Five independent actual-dispatcher cases prove supported synthetic history without a family sidecar acquires one and then resumes through the real runtime; forged migration/qualification, wrong key and expired qualification refuse without changing room files. Four public input-refusal cases and four interrupted-publication recovery cases pass. | This covers a synthetic compatible v1 history, not all historical shapes, live issuer qualification or operator trust-key provisioning. |
| Registry/material migration | Content identity and actual vendor version are separate; registry declarations remain inert. | Complete trusted producer/consumer and resolver-owned material qualification, distinct gate/main bindings, expiry/revocation and replay checks. |
| Release disposition | Seven independently retained job-path cases pass across the focused and public-CLI packets. | R01/R02 remain BLOCKING; component test counts do not qualify the complete migration. |

### Detailed acceptance history

The interrupted-publication correction is independently verified:
`test_chat_migration_interruption_acceptance.py` now passes all four cases in
1.82 seconds (Python 3.12). Same-packet recovery works before qualification,
between qualification and family publication, after family publication before
acknowledgement, and after an intervening refused resume. Source review confirms
continuations read existing authority instead of seeding a competing family;
same-packet reconciliation validates the current family, separate migration
authority, qualification, revocation and linked content before returning an
already-applied non-launching result. This closes the two reproduced recovery
failures. Competing changed-packet and fuller interruption/compatibility matrices
remain distinct requirements; no power-loss certification is claimed.

Public chat journey independently accepted at its bounded synthetic scope:
`test_chat_public_migration_journey_acceptance.py` passes five cases in 2.11
seconds (Python 3.12). Actual `dispatcher.main` consumes the packet with Popen
forbidden and preserves original journal bytes. The supported case then resumes
through actual `ConversationRuntime` and its fake dispatcher. Forged migration
authentication, a wrong independent operator key, forged qualification and
expired qualification refuse with unchanged stored directories/files/bytes.
Public output excludes the synthetic source path, operator key, family nonce
and qualification authentication. The fixture represents compatible current v1
history with its family sidecar removed, not every historical version or shape.

Public input corrections are independently verified: the four retained
`test_chat_revalidation_input_acceptance.py` cases pass together with the three
updated `test_chat_guard_boundary_acceptance.py` cases (7 passed in 0.49
seconds). The new migration key is now included in retained environment
acceptance. These close the reproduced malformed-input and key-isolation gaps;
the earlier failing checkpoints below describe history, not current defects.

Current public chat input checkpoint, 2026-09-08:
`test_chat_revalidation_input_acceptance.py` passes four cases in 0.12 seconds:
missing, malformed JSON, invalid UTF-8 and oversized packets all return a
bounded path/content-free refusal before creating room state. Only synthetic
paths and input were used.

The migration-key scrub correction is independently verified by direct
executor environment checks: inherited key, ordinary override and explicit
capability override all remove `SUMMON_CHAT_MIGRATION_KEY`, preserving an
unrelated synthetic setting. The earlier key-isolation finding below is closed
at this scope; the newly added key still needs retained release-test coverage.

In-progress legacy-migration boundary finding, 2026-09-08: the newly introduced
`SUMMON_CHAT_MIGRATION_KEY` independently authenticates the initial packet but
was not yet included in `_spawn.py`'s provider capability scrub list. A synthetic
direct `_executor._merge_env(None)` check returned true for key presence in the
resulting provider environment. Only the boolean was emitted; no real key or
provider was used. The conductor owns adding the key to scrubbing and retained
inherited/override acceptance before this new migration path can be accepted.
Previous credential-isolation passes covered the earlier key set and do not
certify this subsequently added capability.

The source-family runtime packet is independently verified at its stated
synthetic scope: the two selected test functions in
`test_chat_source_family_runtime_acceptance.py` pass six cases in 3.54 seconds
(Python 3.12). They exercise actual `ConversationRuntime` with the retained
fake dispatcher: explicit packet consumption preserves the journal, enables
one continuation, and does not automatically qualify the following turn.
Forged, expired, revoked, wrong-turn and changed-observation packets refuse
without an additional dispatcher invocation. These tests do not exercise a
public command or a legacy room without a source-family sidecar.

The missing-history refusal correction also passes independently:
`test_chat_revalidation_refusal_acceptance.py` passes its case in 0.26 seconds.
Revalidation now checks historical eligibility before authority access and
reads an existing family without creating directories. This closes the
reproduced refusal mutation. It does not implement initial authenticated
source-family migration for legacy rooms; that positive journey remains open.

The guard-consumption correction below is independently verified:
`test_chat_guard_boundary_acceptance.py` now passes all three cases in 0.46
seconds (Python 3.12). Source inspection confirms the family lock covers both
the current revocation read and durable guard write. The competing revocation
waits for that write and then completes; the two provider-environment checks
also pass. This closes the reproduced guard/revocation ordering defect, not
the full chat producer, public migration, or interruption matrix.

Independent refusal-ordering acceptance, 2026-09-08:
`test_chat_revalidation_refusal_acceptance.py` reproduces a failed migration
creating a source-family authority file and participant runtime directories
before rejecting a nonexistent historical turn (1 failed in 0.28 seconds,
Python 3.12). The original journal bytes remain unchanged, but the refused
operation changes the room's file set. All child-process creation is forbidden
in this synthetic test. Validate historical eligibility and the applicable
qualification authority before publishing migration sidecars; retain the
non-launching and failure-before-mutation requirements. The conductor owns the
correction and integration. This is separate from the guard-consumption race.

Independent durable-boundary acceptance, 2026-09-08:
`test_chat_guard_boundary_acceptance.py` currently reports 1 failed and 2 passed
in 0.17 seconds (Python 3.12, provider-free). A competing authenticated
revocation completes after the guard checks revocation but before it durably
records consumption. The source-family lock currently ends at the read; the
required ordering must extend through durable consumption. The two passing
cases verify the executor removes all current chat qualification capability
environment variables with both inherited and explicitly merged environments,
while preserving an unrelated synthetic provider setting. This finding does
not reopen the independently closed stale-writer cases below. R01/R02 remain
blocking; the conductor owns the correction and subsequent integration.

The subsequent serialization correction passes both retained source-family
stale-writer tests independently (2 passed in 0.12 seconds, Python 3.12).
Revoked qualification refuses without changing stored files, and a stale
qualification writer preserves the previously linked turn. This closes the
two reproduced lost-update cases at module scope; actual competing operations,
initialization contention, interruption, and guard/revocation linearization
remain required. No worker or provider ran.

Independent stale-writer checks in the new
`test_chat_source_family_acceptance.py` both fail (0.19 seconds, Python 3.12).
After a separately persisted revocation, writing a qualification with the older
family snapshot removes that revocation. Likewise, a second qualification
written from the same stale snapshot drops the first turn's retained link.
These are deterministic source-family lost updates, without any worker or
provider process. Serialize initialization, current-state validation, linking
and revocation using a consistent ownership/lock/CAS boundary; unique temporary
filenames alone do not preserve authority. The actual guard-consumption boundary
must participate in the required revocation ordering. The conductor owns the
new retained tests and the correction; R01/R02 remain blocking.

The next chat source revision replaces automatic issuance with explicit
authenticated qualification validation: `revalidate_chat_source` now requires
the qualification argument, and the fresh-turn automatic issuer is removed.
The focused existing guard suite reports 2 passed / 1 failed in 0.24 seconds;
its prior success fixture does not yet satisfy the new source-family authority
check. Adapt that fixture using the actual private source-family producer and
retain the negative cases. Do not relax qualification to preserve the old
fixture, or treat this changing-source component result as full chat acceptance.

Chat producer design review, 2026-09-08: the in-progress
`revalidate_chat_source` accepts observation metadata and calls
`_chat_material_qualification` to sign a new qualification using the local
source-family nonce. Fresh-turn persistence follows the same automatic issuance
path. This does not yet implement the required observation/qualification trust
separation: route/material allowlisting and supplied version/hash fields are
not independent qualifying evidence. The conductor must preserve inert fresh
observation capture and make explicit revalidation consume authenticated
qualification from a trusted producer, with source-family and current-policy
checks before publication. This is a source review of an unfinished candidate,
not a claimed successful exploit or a completed public-path test.

Latest retained correction and public-entry checks, 2026-09-08: all four
qualification/revalidation cases now pass independently (3.47 seconds),
including refusal of revoked qualification before migration mutation. The new
`test_jobs_revalidation_cli_acceptance.py` additionally passes three cases
through the real `run_subagent.main` dispatcher: supported synthetic evidence,
forged authentication, and separate revocation. The public command preserves
original continuation bytes, refuses without changing the job file set or
contents, and does not expose the synthetic private root or authentication.
Its Popen boundary is forbidden; no child or provider runs. This CLI packet
passed in 0.67 seconds under Python 3.12. It verifies consuming a synthetic
authenticated evidence packet, not qualification issuance for an installed
provider. The revoked-before-write case is now enforced in `revalidate_source`;
the existing qualification and binding sidecars remain byte-stable on refusal.
Full chat producer/consumer integration, observation provenance, material
qualification and interruption cases remain required for R01/R02.

The paragraphs below retain earlier independent observations for auditability;
the current checkpoint above supersedes their intermediate failure states.

Retained independent acceptance now lives in
`test_resume_revalidation_acceptance.py`: supported synthetic legacy
revalidation reaches the matching later validator and rejects a changed
version; forged authentication refuses without adding or changing sidecar
bytes. Those two cases pass. The third case fails because an already revoked
qualification is accepted by revalidation instead of refusing before mutation.
Combined with the conductor's retained queued provider-boundary revocation
test, the focused result is 3 passed / 1 failed in 0.80 seconds under Python
3.12. Thus the preceding forged-auth ordering defect is corrected, while
revalidation still needs the same independently current revocation check used
by launch. No child or provider ran in this acceptance packet. The conductor
owns subsequent integration of the new test file; public command, chat and
interruption acceptance remain separate.

The linked-binding correction passes the subsequent positive synthetic check:
`revalidate_source` now publishes the binding, preserves original continuation
bytes, and the later launch validator accepts the specifically matching
qualification/observation. A negative check still fails the no-mutation
boundary: replacing only qualification authentication with a forged value
returns `launch_qualification_auth_failed` after writing the missing binding.
Authenticate the complete proposed qualification and current authority before
publishing any migration sidecar. Retain both cases and interruption/partial
publication behavior in the full public journey. Neither independent check
created a reservation, process, or provider contact.

Independent legacy journey check, 2026-09-08: with an authenticated synthetic
v1 source lacking a launch-binding sidecar, current `revalidate_source` returns
`status=revalidated` and preserves the original continuation bytes, but leaves
`read_launch_binding` null. The later real launch validator then refuses with
`resume_launch_observation_stale`. Thus the helper has not completed the
promised linked-seal migration. This check created no reservation, child or
provider contact. Finish the explicit compatibility decision, linked private
seal and supported public producer/consumer journey; publishing qualification
alone cannot honestly report completed revalidation.

Subsequent independent revocation check, 2026-09-08: the job launch validator
now re-reads current qualification and a separate authenticated revocation
record. With a synthetic signed qualification, the real
`_job_resume._validate_launch_observation` first accepts the matching observation,
then returns `resume_launch_qualification_revoked` after separate revocation.
The queued qualification and its private file bytes remain unchanged. This
closes the reproduced cached-only validator defect at component scope. The
check did not consume a durable claim or reach Popen; full queued continuation,
chat revocation, qualification issuance and legacy migration remain required.

Latest independent check, 2026-09-08: the chat guard accepts a continuation
whose expected binding matches a synthetic fresh observation even when the
vendor version is null and no runtime qualification exists. The actual
`_chat_launch_guard.before_launch` consumed the private guard to `observed` under
a current temporary owner; no provider or child was launched. This is a concrete
qualification-enforcement defect, not merely missing test evidence.

The preceding job consumer also validated the qualification copied into its child
context without consulting independently current revocation authority. The
qualification module checks a record-derived digest, and its test mutates that
digest; neither establishes revocation of an unchanged queued record. The
material-contract policy comparison and public non-launching legacy-revalidation
journey remain incomplete. The conductor's reported 111-test focused pass and
595-test broader pass do not close these requirements. Resolve these predicates
before another broad acceptance campaign; retain the existing passing evidence
as component-level history.

Implementation checkpoint, 2026-09-08: `_launch_qualification.py` now carries a
separate authenticated qualification sidecar, preserves the canonical
`sha256:` registry digest form, checks an allowlisted resolver-owned material
contract, and supports a separately authenticated revocation record. The job
consumer re-reads the qualification and revocation authority at the actual
provider boundary; it no longer trusts only the cached reservation copy.
`revalidate_source()` is a provider-free, non-launching source migration seam
that leaves the historical record/result bytes unchanged. The chat guard now has
a versioned source-family qualification wrapper and an explicit required path;
ordinary chat remains observation-only until a trusted chat qualification is
actually issued and persisted. No chat turn is being represented as a job.

Provider-free tests after this checkpoint: qualification/guard/job-resume
focused slice `22 passed in 4.95 seconds` (including the real revalidation CLI
acceptance); the preceding complete continuation/qualification/chat/resume
slice reached `111 passed` before the final revocation guard; fleet and evidence
partition `595 passed, 1 skipped`; account/roster/backend/usage partition `913
passed, 1 skipped`; deliberation/swarm/chat partition `520 passed`. These are
source tests only; no provider/auth call was made. R01/R02 remain blocked from
release until the chat source-family producer/consumer is persisted end-to-end,
and the observation/material/interruption matrix reaches the actual claim
boundary. The public non-launching revalidation command is now exposed and
provider-free tested.

2026-09-08: R01/R02 remain blocking. The architect inspected the conductor's
terminal command results, separately from its narrative progress reports:

`R02_LAUNCH_QUALIFICATION_DECISION.md` now fixes the outstanding qualification
and explicit legacy-revalidation policy. Its implementation and acceptance
requirements govern the remaining runtime work below.

| Partition | Terminal evidence | Acceptance limit |
| --- | --- | --- |
| Launch binding, chat guard, retained guard acceptance, conversation runtime, job resume | 108 passed in 76.88 seconds | Compatible fixture paths pass; full replacement/replay, legacy revalidation and version qualification still require review. |
| Fleet, operator workflow, phase-zero contracts, evidence kernel, portable results, job control, resume capability | 434 passed, 1 skipped in 96.24 seconds | The suites overlap other evidence and are not additive requirement coverage. |
| Migration, release gates, schema inventory, Windows silent launch | 35 passed in 48.07 seconds | Gate wiring and bounded migration checks do not establish a release-ready source tree. |

The POSIX FIFO-swap node in `test_portable_result.py` is explicitly skipped on
Windows. A separate targeted `-rs` check confirmed that classification (one
skip, 0.13 seconds); this is not evidence that the POSIX case passed. The wider
provider-free account/context/deliberation partition subsequently completed:
955 passed and one skipped in 484.51 seconds. The architect verified its terminal
exit status; its skip classification still needs the packet's retained report.

Known contract work remains: keep content identity separate from vendor-version
evidence, complete adapter-owned material qualification, and prove the full
foreground/background child-boundary mutation and legacy-revalidation matrix.
Passing existing compatibility tests cannot waive those requirements. The next
field-separation revision passes independent synthetic-file checks: an unobserved
vendor version stays null, two supplied fixture versions remain distinct while
content identity stays stable, observation/projection readers agree, and the
misleading measured-version field is absent. No executable was launched. This
closes that reproduced representation defect; the conversation declaration-to-
observation translation and generic content-based qualification predicates still
need the explicit adapter-policy decision and enforcement.

The subsequent focused run on the changing contract reported 109 passed and
two failed in 227.46 seconds: conversation scope refusal before provider contact,
and expired-owner cancellation of a recorded orphan tree. Earlier green subsets
do not qualify this revision. The conductor is removing the content-only fallback
and implementing the separate authenticated qualification record. Preserve the
orphan regression's real initial launch and expiry/cleanup subject while updating
its qualification fixture; setup refusal is not process-tree cleanup evidence.

The new separate qualification issuer has an independently reproduced initial
format mismatch: canonical `registry_scope` returns a prefixed digest, while
`_launch_qualification.issue` currently rejects that unchanged value as invalid.
The check used otherwise valid synthetic fields and launched no executable.
Fix the shared producer/consumer contract without ad hoc caller normalization.
The completed qualification chain must additionally enforce its material contract,
current revocation state and qualifying authority/evidence; signing caller-supplied
metadata does not establish any of those facts.

The issuer format correction passes an independent recheck: the canonical
prefixed digest survives unchanged, while missing and placeholder vendor versions
refuse. This is a synthetic issuer check, not issuance-authority or full consumer
acceptance. The current qualification schema/storage seam is job-specific; chat
integration must retain an explicit source-family binding or authenticated wrapper
without inventing a historical job or relabeling a chat turn to satisfy job fields.

The three initial qualification tests pass independently under Python 3.12
(0.25 seconds). A separate synthetic write check confirms that forged
authentication refuses before sidecar publication, closing the writer's earlier
re-signing behavior. These establish record mechanics only. The current
revocation test changes a digest inside the candidate record; that is not proof
of revocation through independently current authority. Retain the original
signed record, revoke its authority separately, and refuse its already-queued
continuation at the actual provider boundary. Material-contract validation must
likewise compare against the trusted resolver/policy, not just a self-consistent
digest of qualification fields.

## Canonical facts

`skills/summon/scripts/_resume_capabilities.py` is the source of the exact
resume-capabilities/v1 rows and the additive resume-capabilities/v2 rows. The
v2 registry carries a generation, digest, operation, adapter, adapter-version
scope, external-CLI-version scope, qualification facts, and an explicit
`launch_permission: not_granted` value. `compare_launch_scope()` is a pure
pre-contact consistency check. A match is not authentication or permission;
every result has `contact_allowed: false` and `provider_contacted: false`.

## Producer/consumer inventory

| Source | Current role | Migration treatment |
| --- | --- | --- |
| `_resume_capabilities.py` | Produces v1 public rows and v2 canonical declarations | Remains the single editable authority; v2 comparison is additive. |
| `_chat_resume.py` | Validates stored v1 refusal/history projections | Keep the exact v1 reader and legacy room readability; historical rows are readable but cannot be rebuilt as current launch authority. |
| `_job_continuation.py` | Produces and validates private/public continuation facts; participates in the source-bound resume inventory | Preserve v1 bindings; add v2 scope checks only at the reviewed pre-contact boundary. |
| `_jobs.py` | Projects current capability/status facts; consumes the continuation capability before public projection | Continue v1-compatible public projections; do not infer launch from v2 equality. |
| `_job_resume.py` | Reserves the authenticated successor and consumes durable gate/provider launch claims | Bind the exact current host observation before consuming each launch claim. The current `provider_launch_control` callback ignores executor evidence; durable CAS alone does not verify the executable or version. |
| `run_subagent.py` | Loads the background child context, verifies invocation drift, and passes separate gate/main controls to execution | Carry the authenticated capability and host binding through the actual child path. Preserve gate/main separation and reject missing or substituted binding before provider contact. Ordinary dispatch remains outside this continuation migration. |
| `_executor.py` | Resolves the actual command and supplies launch evidence immediately before provider Popen | Existing command/argv/cwd/environment digests are private launch evidence, not executable-byte or measured-version evidence. Consume a fresh trusted observation at this boundary; do not infer qualification from command-text equality. |
| `_conversation_runtime.py` | Stores v1 capability alongside a v2 scope for current runtime decisions | The v1 field remains the historical projection. R02 compares the v2 scope immediately before launch and refuses stale generation/adapter version. Legacy process-record lookup is read-only and falls back to the old session-wide directory without creating a duplicate participant scope. |
| `test_resume_capabilities.py` | Pure v1/v2 contract and forgery fixtures | Includes exact v2 scope match, stale registry, adapter-version, external-version and malformed-input refusal tests. |
| `test_job_continuation.py`, `test_job_control.py` | Continuation-source and jobs-status consumer fixtures | Bind the inventory to private-source sealing and public status projection without rewriting v1 history. |
| `test_chat_resume.py`, `test_conversation.py`, `test_conversation_runtime.py` | Historical v1/room/process compatibility fixtures | Prove legacy readability, additive public projection, no duplicate room/runtime scope, and non-contacting stale/refusal boundaries. |

## Required R02 launch sequence

1. Reconstruct the exact v2 capability from the canonical registry.
2. Obtain a bounded, authenticated host observation for registry generation,
   digest, adapter identity and adapter/external CLI versions.
3. Run `compare_launch_scope()` before any continuation contact.
4. Refuse with a typed, non-contacting result on malformed, stale or mismatched
   facts; never silently fall back to a different backend or version.
5. Apply separate owner, session, account, model, permission, spend and
   served-evidence gates.
6. Preserve the v1 capability/history projection and original continuation
   identity for compatible legacy rooms; do not rewrite old records.

## Current boundary

This slice proves pure schema and pre-contact comparison behavior plus the
provider-free historical room/process compatibility seam. It does not
authenticate host observations, remove every legacy chat allowlist, qualify
Codex/Cursor/OpenCode/ZCode/AGY, or certify live steering. Those remain
explicit R02/release work items.

## Final-boundary acceptance matrix

Source rechecked 2026-09-08. The conversation path starts a dispatcher before
that child resolves its provider executable. Checking only the parent process
or its registry does not close the provider replacement window. The following
cases are required implementation evidence, not claims of passing tests.

| Injected change or event | Required observation and result |
| --- | --- |
| Executable or external version changes after reservation | Reject at the actual provider boundary before Popen; a stable command string is insufficient. |
| Registry generation or adapter version changes while queued | Reject the stale binding without automatically selecting another route or rewriting the original declaration. |
| Host observation is missing, malformed, copied from registry, or caller-forged | Reject before launch. A supplied `trusted` flag or matching public digest is not authentication. |
| Observation belongs to another successor, owner, or launch | Reject substitution and replay; gate approval cannot be reused as the main provider's launch binding. |
| Same valid continuation is submitted twice | Only the durable claim winner can reach the child boundary; the loser cannot spawn or erase the winner's uncertainty. |
| Failure before claim consumption versus failure after consumption | Preserve proved no-contact refusal separately from indeterminate launch effects; do not reset an uncertain claim to retryable. |
| Dispatcher starts, but provider-boundary validation refuses | Report that precise boundary. Do not emit the existing `this_invocation_before_dispatch` refusal shape for an already-started dispatcher. |
| Historical v1 source, room, or refusal is inspected | Preserve readability and historical model identity; inspection grants no current continuation authority and creates no replacement room. |

Exercise foreground conversation and background governed-job paths through
their real producer/consumer chain, with fake executables or injected trusted
observations. Include the required gate path when present. A direct unit call
to `compare_launch_scope` cannot qualify the child handoff. No installed-provider
probe, new account/auth operation, or live route qualification is authorized.

## Launch-material identity decision

Architect review of the first implementation, 2026-09-08: executable content
identity and externally observed CLI version are separate evidence. A content
digest must not be relabeled as a measured vendor version when version evidence
is absent. Any adapter that qualifies a content-bound version identity must do
so explicitly within its own reviewed contract; there is no generic fallback.
The observation must describe the exact executor-resolved launch, including
the adapter's required script/package materials when an interpreter or shim
is the launched executable. Holding `node`, Python or a shell constant while
replacing its provider script must not preserve a valid provider binding.
Tests must exercise this replacement and resolution drift through the actual
child boundary. Structural validation remains distinct from authenticated
production and durable consumption; a well-shaped observation alone grants
neither eligibility nor launch authority.

Strict-envelope compatibility check, 2026-09-08: the first integration added
`launch_observation` to `summon.fleet-launch-evidence/v1`. Independent execution
of the pure fleet consumer rejected that object because its v1 field set is
exact; the otherwise identical legacy projection passed. No child or provider
was launched. The resume observation must therefore travel separately or via
an explicit versioned adapter, preserving the existing fleet envelope and its
strict validator. The conductor owns this in-progress correction. Affected
fleet and deliberation consumers belong in the migration regression packet;
passing resume-only tests cannot establish compatibility.

The subsequent opt-in correction passes a narrow independent recheck: default
executor evidence contains no `launch_observation` and passes the actual strict
fleet v1 consumer. The existing fleet launch-evidence privacy/binding test also
passed under Python 3.12 (one test, 0.76 seconds). This closes the reproduced
default-envelope regression; it does not qualify the unfinished resume chain
or substitute for the final affected-consumer regression packet.

The job-resume partition subsequently passed 59 tests in 91.75 seconds before
the next material-measurement change. Independent synthetic execution of that
new change found that a path-looking `--prompt` value was opened and counted
as provider launch material. Changing the referenced text file changed the
launch-material digest. This is not an acceptable resolver contract: prompt
arguments and attachments are invocation data, not authority to discover
provider files. Require explicit adapter-owned material descriptors from the
actual resolver, with separate stable provider identity and invocation-content
bindings. Missing or unreadable required materials must refuse, not disappear
from the measured set. The conductor owns this correction and its path-looking
prompt, entry-script replacement and qualified-package dependency regressions.

The subsequent explicit `material_paths` measurement seam passes two independent
no-provider checks: a path-looking prompt argument never calls the material
reader, and a missing declared file refuses. This closes the reproduced data
argument read at that layer. It does not prove the resolver declares every
required material, distinguish an unsupported wrapper from a complete native
executable, or qualify the unfinished conversation/resume integration.

Independent check of the new `_chat_launch_guard.py`, 2026-09-08: a synthetic
authenticated guard accepted an otherwise structurally valid observation with
`observed_at_ns=1`, then accepted that same observation a second time. The check
called the real `create` and `before_launch` functions using a temporary fixture;
it launched no child and contacted no provider. Authentication of the guard file
does not establish freshness or single consumption. Require authenticated
turn/owner/attempt binding, bounded freshness and a durable single-use transition,
including competing callers and honest uncertainty after consumption. This is an
open integration defect assigned to the conductor, not release acceptance.

The revised conversation scope translator also returned `scope_match` for a
synthetic object containing only public registry fields. Its pure result still
correctly said `launch_permission=not_granted`; the metadata comparison cannot
serve as evidence that the host measured or authenticated a provider launch.
The completed parent/child chain must retain that distinction and verify the
actual provider boundary rather than promote this isolated comparison.

The next guard revision independently refuses a sequential duplicate, but still
accepts `observed_at_ns=1`: it bounds the guard file's age rather than the host
observation's age. Current owner/turn authority must also be revalidated at the
child boundary; storing those fields alone is not a live ownership fence.

A synthetic environment-only check of `_executor._merge_env(None)` also retained
all three new chat-launch guard fields for the provider environment, including
the authentication token. No real token or provider was involved. The central
`_spawn.scrub_provider_env` must remove dispatcher guard capabilities before
provider Popen, with a regression through the actual executor seam. Retaining
them in the dispatcher and stripping them from its provider child are separate
requirements. These findings remain part of the conductor's R01/R02 packet.

The following scrubber correction is independently verified: all five declared
chat guard fields are removed by `_executor._merge_env` both with and without
overrides; the dispatcher environment and an unrelated preference remain intact.
The existing executor ownership environment selection also passed four tests
(four unrelated tests deselected) under Python 3.12 in 0.30 seconds, with plugin
autoload and telemetry disabled. These checks used synthetic values and no
provider contact. They close the reproduced environment inheritance defect at
that seam, while final child-chain, freshness and owner-fencing acceptance remain
open. The earlier finding above is retained as the migration audit trail.

The next freshness/owner revision passes nine independent retained cases in
`test_chat_launch_guard_acceptance.py` (Python 3.12, 4.06 seconds, plugin autoload
and telemetry disabled). The tests use real temporary owner records and
synthetic observations: fresh consumption; stale/future observations; wrong
attempt; missing/replaced/expired owner; duplicate consumption; and two threads
competing for one guard. Refused cases preserve the pending phase. No provider
or child is launched. This verifies those bounded guard properties, not
cross-process contention, observation authenticity/production, turn-state
revalidation, vendor-version qualification, or the complete conversation and
governed-job migration. Keep the fixture's synthetic executable-content-revision field
distinct from approval of the still-open version-identity design.
