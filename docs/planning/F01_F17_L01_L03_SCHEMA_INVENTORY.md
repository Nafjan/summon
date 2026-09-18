# F01/F17/L01-L03 schema and legacy-reader inventory

## Current L01 entry compatibility disposition

L01 is VERIFIED-FOR-3.5, 2026-09-08, after source/test/retention reconciliation.
The lead compared the tracked baseline's CLI command-family set with current
source: all 26 entries remain and only `workspace` is added. The reviewed parser
diff retains existing branches and adds workspace, council checkpoint and explicit
job/chat revalidation translations. Existing flat-argv and subcommand golden
fixtures remain retained. This comparison uses the tracked source baseline;
it does not invent a locally unavailable release tag or claim a new test run.

| Finite compatibility boundary | Source and behavioral evidence |
| --- | --- |
| Command families and aliases | `_cli.SUBCOMMANDS`: dispatch/run; list/agents/ls; models; doctor; onboard; manifest; council; deliberate; chat; swarm; jobs; agent; role; telemetry; usage; bug-report; auth; fleet; result; version; help/--help/-h. `test_phase1_compatibility.py` retains flat-argument identity and the rewrite corpus; `test_discovery.py` covers subcommand translation. |
| Canonical skill | `skills/summon/SKILL.md`, canonical dispatcher and public-flag documentation fixture. |
| Council companion | `skills/council/SKILL.md` retains its canonical Summon references. `tests/test_install.py::test_council_companion_is_installed_and_removed_safely` and `test_council_companion_never_clobbers_foreign_skill` cover owned lifecycle and foreign preservation. |
| Deliberate companion | `skills/deliberate/SKILL.md` retains its canonical entry and clarifies gated runtime behavior. The analogous deliberate install/remove and foreign-preservation tests remain. These three are all repository skill entries. |
| Companion upgrade | `tests/test_migration_gate.py::test_isolated_upgrade_keeps_owned_manifest_and_companions` checks both companions under the owned manifest. No installation was performed during this reconciliation. |
| Legacy run IDs and bytes | Retained council-reader, historical deliberation-reader, historical job-claim and conversation compatibility acceptance preserve declared supported historical identities. `test_workspace_entry.py::test_legacy_host_reopen_preserves_scope_identity_without_schema_upgrade` preserves the v1 host identity. |
| Additive workspace labels | Workspace parser/entry fixtures cover additive routing, explicit preview/provider-free output and noninteractive listener refusal. New authenticated mutation CLI behavior remains F17. |

Unqualified names above are under `skills/summon/scripts/`. All named regression
files are retained in fixed release commands. The existing machine inventory's
four legacy boundary rows remain a partial metadata view; this finite companion
crosswalk completes the L01 preservation assessment without pretending that all
formats or producer/consumer migrations are complete. L02, F17 and final candidate
source-bound validation remain separate requirements. Historical incomplete-L01
statements below are superseded by this disposition.

Status: provider-free source inventory, updated 2026-09-08. This document records the
current working-tree contracts and reader behavior. It is not release approval,
managed-install evidence, or proof that the remaining migration rows are
complete.

## 2026-09-08 format-level audit and correction

Historical claim correction, 2026-09-08: all 18 retained reader/identity cases
now pass independently in 4.18 seconds. The lead inspected strict generation
typing, None-or-bool provider contact, and finite non-bool child creation time.
The current-source copy covered 230 files with no shared drift, guarded fixtures
were cleaned and no live handles remain. This supersedes the earlier 15-pass /
3-failure checkpoint below. The original 18-case test and exact format mapping
are now verified retained in canonical compatibility acceptance and CI, with
the current format-reference validator passing in 0.84 seconds. Historical field-set
fixtures still do not establish recovered historical producer equivalence.

Encrypted numeric-version integration is accepted, 2026-09-08. The shared test
matches the independently passed sixteen-case packet after line-ending
normalization. Canonical workspace UI, browser-security and accessibility
commands retain it. Payload mappings cite both supported and unsupported
version cases; the AAD mapping cites real AES-GCM cases without assigning that
evidence to the storage-slot format. Stale numeric-version gaps are replaced
with actual browser-storage, host-key and historical-producer limits. The
current-manifest-reference validator passes in 0.56 seconds with three watched
files unchanged. Earlier pending-integration statements below are superseded
at this bounded scope; remaining L02 formats and reader requirements stay open.

Current checkpoint: the separate format tool and its tests are integrated;
its 52 format/structural records are metadata, not complete migration evidence.
The current gap report contains 114 source/literal pairs across 79 distinct
schema names, including two isolated G02 candidate names. An uncovered pair
means a missing inventory mapping, not a demonstrated broken format. The
named-entity inventory and format inventory have different coverage scopes;
neither validator substitutes for reader/producer behavior.

Browser schema identity and bounded recovery behavior are now independently
accepted in shared source: 36 page/actual-script cases plus 68 format-inventory
cases pass in 3.42 seconds. The request boundary refuses incompatible success
responses before view installation or recovery mutation; transient HTTP errors
retain bounded reconnect behavior. Full valid-v1 field-shape and rendered
qualification remain separate. Historical job-claim malformed scalar corrections
remain open. Encrypted pending-state numeric-version acceptance is independently
approved for integration as described below.
The older pending-tool and untested-browser statements below describe the
initial audit checkpoint and are superseded at this bounded scope.

The existing validator succeeds but its file-level coverage masks omitted
formats in already-mapped producers, including job continuation/source/binding,
embedded workspace authority and chat-family records. Numeric conversation
records, browser draft versions and unversioned job envelopes also fall outside
the literal scanner. The current bounded scan found 43 production files with
versioned schema literals excluding the changing G02 candidate. The earlier
38-file count below is historical, not current completeness evidence.

A separate isolated tool packet now records format and structural variants,
producer/readers, authority classification and source/fixture references. It
must expose missing source/format pairs inside mapped files and keep manual
numeric/unversioned coverage explicit. Validation proves references remain
present, not that all formats or runtime behaviors are qualified. Job/launch/
chat/workspace paths were inspected in detail; fleet, usage, context and model/
role registries remain discovery-only in this audit.

| Behavioral follow-up | Evidence status | Required next check |
| --- | --- | --- |
| Historical job-resume claim field sets in `_job_resume.py` | Explicit compatibility branches found; a dedicated exact-shape fixture was not located. | Exercise each supported historical claim shape through its actual reader, preserving identities and refusing unauthorized launch. |
| Browser `summon.workspace.view/v1` reader | Missing/future-schema recovery loss was reproduced and corrected at the common request boundary; integrated 36-case acceptance passes. | Retain the accepted cases; full valid-v1 field-shape and rendered acceptance remain open. |
| Pending-message ciphertext `v:1` and AAD `version:1` | Sixteen actual page-script/WebCrypto cases independently pass; test-only integration approved. | Retain in canonical schema/browser-security commands and inventory; rendered storage, host keys and historical-producer equivalence remain separate. |

Encrypted numeric-version acceptance, 2026-09-08: the lead inspected the complete
test and guarded runner and independently passed all 16 cases in 1.16 seconds.
Actual numeric-v1 producer records recover unsent, sending and uncertain states.
Seven missing/future/wrong-type outer versions refuse with exact ciphertext and
operation identity retained; five real AES-GCM AAD controls exercise supported
and incompatible versions. Explicit reauthentication recovers compatible work.
No automatic message submission or lookup occurs, and unreadable work blocks
replacement. Three inspected dependencies match shared source; all sixteen
owned Node children exited, fixtures were cleaned and guard hits were zero.
This evidence does not qualify rendering, actual browser storage, host key
derivation, full view shape or historical producer equivalence. Integration and
fixed-suite retention remain the conductor's next action.

Historical claim acceptance, 2026-09-08: a new isolated 18-case packet exercises
the explicit historical ledger/child field sets and modern controls through
actual authenticated readers. The lead inspected the complete test and guard
wrapper and independently reproduced 15 passes / 3 failures in 3.63 seconds.
Historical identity/bytes and public privacy are preserved in passing cases;
historical missing qualification refuses before launch consumption or mutation.
Three authenticated malformed fields are accepted: claim `generation=True`,
claim `provider_contacted=1`, and child `created_at` as an object. These are
strict-shape validation gaps, not an authentication bypass. Correct the two
reader functions while preserving valid historical/current shapes; retain all
18 cases in the release suite. The fixtures derive supported historical shapes
from synthetic current records and do not certify unavailable old producers.

Accepted council receipt/generation, historical deliberation public-reader and
L03 rebuild evidence remain retention-only. New G02 records are candidate-only.
No runtime tests ran in this read-only audit; implementation and independent
acceptance of the inventory tool remain pending.

## 2026-09-08 companion-reader checkpoint

The council reader correction is integrated and independently accepted. The
reader now checks council mode/run identity before projection or resume, checks
the receipt again under ownership, and uses bounded before/after generation
observations. Sixteen added cases retain supported unversioned receipts and
exercise initial refusal, replacement races and generation instability. They
were independently executed with the historical-reader cases in the accepted
140-case integration batch. Both files are retained in fixed phase0 and CI
commands. The previously reproduced reader gaps are historical and closed at
this scope; incomplete G02 implementation is a separate candidate change.

The historical deliberation public-reader gap now has independent behavioral
evidence: two external acceptance cases pass against current shared source in
45.94 seconds on Python 3.12, with external pytest plugins, telemetry, bytecode
and cache disabled. Real `inspect_run` and `replay_run` consume a synthetic
historical journal/receipt pair and preserve run/decision and packet/binding
identities, `legacy_read_only: true`, `routing_authority: false`, private-entry
elision and recursive durable bytes. Current initialization remains strict;
the test reconstructs a historical record rather than permitting a new legacy
authority producer. The process exited successfully; both cases subsequently
passed in the integrated batch and are retained in the fixed release suite.
Whole L01/L02 remain incomplete.

## Canonical entity and contract map

| Entity or boundary | Canonical source and schema | Main writer/reader | Compatibility rule |
| --- | --- | --- | --- |
| Workspace event stream | `_workspace_protocol.py`, `summon.workspace/v1` | `_workspace_state.py` reducer and `_workspace_runtime.py` | The feature marker must be first. Unknown protocol/event versions refuse before projection mutation. |
| Workspace feature marker | `_workspace_state.py`, `workspace_feature` version `1` | `apply_event()` | A missing, reordered, or future feature marker is refused; no workspace is fabricated from an old inner run. |
| Goal, task, lane, attempt | `_workspace_plan.py` `summon.workspace.plan/v1`; embedded protocol records | plan compiler, coordinator, state projection | Task and attempt IDs remain embedded in immutable bindings. There is no alternate legacy task schema. Rebinding requires a new explicit grant/fork. |
| Host scope and principal | `_workspace_entry.py` `summon.workspace-host/v1` and `summon.workspace-host/v2` | host config reader, plan-host verifier | v1 is retained for the existing host/reopen path; v2 adds source and roster bindings. Exact field sets are required and configs are never silently upgraded. |
| Operator grant/scope | `_workspace_plan.py` and `_workspace_entry.py`, `summon.workspace.operator-message-grant/v1` and `summon.workspace.operator-scope/v1` | host scope compiler and runtime | Scope is workspace/run/task-bound, expires, and is checked again at the final append boundary. IDs and source digests are immutable. |
| Worker message | `_workspace_admission.py`, `summon.workspace.message-send/v1` and `summon.workspace.worker-send-base/v1` | coordinator worker-send admission | Source, claim, recipient, epoch, operation key, and content digest must match. Stale or legacy event shapes refuse before publication. |
| Operator message | `_workspace_admission.py`, `summon.workspace.operator-send-base/v1` | coordinator operator admission/reconciliation | The same operation key is read back idempotently; a changed request conflicts and never creates a second delivery. |
| Delivery and settlement | `_workspace_protocol.py` delivery records and `_workspace_state.py` | state reducer, runtime, inbox/recovery | Delivery transitions retain contact/spend/cleanup uncertainty and require typed evidence. Retraction cannot erase submitted or uncertain work. |
| Inbox/command | `_workspace_admission.py`, `summon.workspace.operator-command-request/v1`, `summon.workspace.supervisor-inbox-base/v1` | coordinator inbox command path | Commands require a current consumer binding or an explicit recovery authorization; stale consumers cannot be rebound implicitly. |
| Content/context transport | `_workspace_transport.py`, `summon.workspace.pipe/v1`, `summon.workspace.context/v1`, `summon.workspace.message-send-result/v1` | owned transport and worker receiver | Complete UTF-8 payloads are bound by digest and byte count. Unsupported transport observations refuse; no provider is contacted. |
| Artifact/effect evidence | `_workspace_protocol.py`, `summon.workspace.effects-observation/v1` | runtime/effect resolver and projection | Evidence is source-bound and qualification-labeled. Worker prose cannot substitute for host observation. |
| Review/decision authority | `_decision.py`, `summon.decision/v2`; deliberation context schemas | council/deliberation kernels | Review messages remain advisory. Only receipt-bound, option-bound ballots and explicit quorum rules affect deliberation state. |
| Conversation room/event | `_conversation.py`, record schema version `1` | room journal/UI/runtime | Room and event records require the exact supported version; unsupported versions refuse. |
| Resume capability | `_resume_capabilities.py`, v1/v2 plus `summon.resume-launch-scope/v1` | chat/job continuation and conversation runtime | v1 remains a historical projection. v2 scope is compared before contact and before launch; mismatch is a typed refusal. |

## Legacy readers and identity preservation

1. **Host configuration.** The host reader accepts exactly the v1 field set or
   the v2 plan-host field set. A v1 host config preserves `workspace_id`,
   `run_id`, `operator_id`, `key_epoch`, and both private key identities; it is
   not rewritten as v2. A v2 config must revalidate its copied plan and project
   and roster bindings before reopen.
2. **Deliberation context.** `_deliberation_context.py` can reproduce the v1
   binding only through the explicit read-only legacy path. The v1 result keeps
   packet, source revision, run, decision, entry, and observation identities,
   and always has `routing_authority=false`. Normal current binding parsing does
   not accept a legacy object.
3. **Resume capabilities.** v1 records remain available for historical
   projections. Launch decisions use v2 plus `compare_launch_scope()`; a stale
   registry generation, adapter version, external CLI version, or malformed
   scope refuses before any provider contact.
4. **Conversation and swarm history.** Conversation journals require schema
   version 1. The swarm reader retains valid historical segment bytes, supports
   CRLF/LF compatibility, and rejects an ambiguous or torn prefix rather than
   repairing it implicitly. A legacy inner run is reported as unprepared rather
   than being fabricated into a workspace.
5. **IDs and replay.** Operation keys are idempotency identities, not display
   labels. Replays preserve the original goal/task/message/delivery/decision/
   turn/attempt IDs and reject a conflicting reuse. A replacement consumer or
   worker receives a new epoch/instance and explicit lineage; it cannot inherit
   the old authority by matching a name.

## Evidence currently present

- `test_workspace_entry.py` covers exact host field sets, plan binding checks,
  missing/replaced config refusal, and v1 host reopen/draft-key retention.
- `test_workspace_plan.py`, `test_workspace_protocol.py`, and
  `test_workspace_state.py` cover versioned plan/protocol records, stale
  revisions, duplicate operation keys, immutable IDs, and unsupported events.
- `test_deliberation_replay.py` covers read-only v1 context replay and refuses
  to treat it as current authority.
- `test_resume_capabilities.py`, `test_chat_resume.py`, and
  `test_conversation_runtime.py` cover v1 history plus v2 pre-contact scope
  refusal; the combined gate now passes 74 tests.

## 2026-09-06 bounded schema packet

The source-bound inventory now has an explicit machine-checked binding for the
required entity vocabulary: workspace, task, attempt, principal, worker,
message, delivery, command, artifact, review, decision, and resume. Each name
resolves to one or more conceptual inventory rows with non-empty producers,
readers, versioned schema literals, and focused fixtures. The inventory also
records the flat CLI translator, dispatcher entry, skill documentation entry,
and legacy job reader as compatibility boundaries. These entries are additive
and never create launch authority.

The inventory audit scans production Python sources for versioned `summon.*`
schema literals. It found 38 schema-bearing files; 22 remain intentionally
unmapped to a named entity row, primarily fleet, usage, liveness, transport
budget, context compilation, job-control/resume, workspace UI/layout/view, and
the dispatcher projection. This is now an explicit audit result rather than an
implicit completeness claim. The uncovered files are migration work, not a
release assertion.

Provider-free validation passed 840 tests in 211.39 seconds across the schema
inventory, migration gate, workspace plan/protocol/state/runtime, swarm
protocol/coordinator, resume/chat/conversation compatibility, job continuation
and control, and phase-zero/legacy-compatibility suites. No provider, auth,
install, credential, commit, or release action was performed.

The working tree now contains a bounded, read-only, swarm-only projection
rebuild seam. It uses the strict coordinator snapshot and canonical reducer,
binds its detached output to the exact segment bytes and prefix digest, and
refuses torn, unstable, corrupt, mismatched, duplicated, or unsupported
history without repairing or publishing an index. Focused coverage maps the
conditional Fable findings to provider-free tests, including zero-write and
Windows reparse checks. There is still no durable index publisher or migration
runner, and full producer/consumer and legacy-projection coverage remains open;
F01/F17 remain PARTIAL/BLOCKING. L03's bounded preview rebuild requirement was
subsequently independently accepted and is VERIFIED-FOR-3.5 in the disposition
matrix; do not reopen that completed scope merely because broader inventory
work remains.

## Remaining migration work

This inventory is a partial source map; it does not establish complete format
coverage or promote F01/F17/L01/L02. L03 retains its accepted bounded scope:

- No single machine-checked manifest yet enumerates every producer and consumer
  across the legacy CLI, workspace, room, job, fleet, usage, review, and
  deliberation projections.
- Full legacy CLI/projection migration and every noninteractive stale-generation
  path remain open.
- Authenticated host observations, duplicate-allowlist removal, and complete
  adapter revalidation remain R01/R02 work.
- No reader may silently translate a legacy record into launch authority. Future
  migration fixtures must prove refusal-before-mutation and exact identity
  preservation for each newly enumerated consumer.
