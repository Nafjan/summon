# 3.5 preview acceptance boundary

Status: architect decision, 2026-09-08; bounded exact Fable consultation approved
the boundary and RB-01 through RB-03. Subsequent architect acceptance of F15
is recorded in F15_ARCHITECT_REVIEW.md. Historical blocking findings below
remain the audit trail; the accepted scope has not been narrowed.

The release promise in EXPECTED_NEXT_RELEASE.md is a provider-free workspace
preview. The F15 release disposition instead demands universal transport and
live-adapter evidence. Those requirements describe a later capability and
contradict the release promise. This decision makes the acceptance boundary
explicit; it does not mark any requirement verified or change the 63-row
progress denominator.

## Adopted decision

**Historical supersession note (2026-09-13):** paragraphs below that say F15
remains BLOCKING are retained as the earlier audit trail. They are superseded
by `F15_ARCHITECT_REVIEW.md`, which accepts F15 for the bounded provider-free
workspace preview; the universal transport and live-adapter requirement
remains later work.

## Final release-profile decision, 2026-09-13

The provider-free workspace preview and a live-provider release are two
explicit publication profiles, not one partially waived gate set:

- `stable` is the default and retains the existing GA contract. Every fixed
  suite and gate, including `live_provider`, must pass from a clean,
  source-bound candidate.
- `workspace-preview` is an opt-in contract for the reviewed 3.5 workspace
  slice. It requires every fixed suite and every other gate to pass, and it
  permits `live_provider=blocked` only when the machine artifact carries the
  exact `live_provider_evidence_missing` reason. Invalid evidence, a failed
  live gate, an unreviewed skip, a dirty tree, missing rendered evidence or
  failed managed-install convergence still blocks the profile.
- The profile must be selected explicitly in `tools/release_manifest.py` with
  `--profile workspace-preview`; a version label, model endorsement or a
  blocked result cannot select it implicitly. The resulting manifest is a
  provider-inert preview artifact and cannot be used as stable/public-release
  evidence.

This resolves the earlier wording conflict: preview implementation acceptance
does not silently waive the live gate, while the reviewed provider-free
workspace cut has a machine-checkable release boundary. No provider contact,
credential change, installation, commit or publication is implied.

Historical audit position: keep F15 BLOCKING until every transport/resource path exposed by the 3.5
workspace preview consumes the coordinator-bound admission decision before
creating an execution resource or sending required context. Enumerate those
paths from actual CLI, runtime and demo callers, including recovery paths.
An optional adapter with an ungated reachable path is insufficient.

Resource accounting is operation-specific. `OwnedSupervisorConsumer` is a
local delivery consumer, not an agent execution slot. Its child/channel still
requires bounded ownership, cleanup and resource accounting; sending context
through `expose_supervisor_offer` requires a bound message decision before the
write. Registration alone consumes no worker execution slot. These distinctions
do not exempt actual worker launches or recovery successors from admission.

Required evidence for each exposed path:

- Exact policy, request, source prefix, revision and recipient binding.
- Complete serialized payload accounting; required messages never truncate.
- Independent byte, message, rate, stream and execution-slot accounting.
- Control and recovery reserves survive saturation and oversized head holds.
- Stale, duplicate, mismatched or deferred admission refuses before contact.
- Restart and recovery preserve previous delivery and spend uncertainty.
- Existing dispatch behavior retains its affected compatibility regressions.

Universal external transports and live-provider qualification remain later
work, as the published release promise already states. They are not proven by
synthetic workers. Every supported route must be listed with its qualification
and authority; an unsupported route must be unavailable or explicitly refused.
Do not count a label alone as enforcement.

R12 still requires transport-boundary and exact-byte capable-path evidence for
the transports actually claimed by this release. This decision cannot relax
integrity, provenance, privacy, authority or recovery requirements.

## Closure process

The conductor's source-derived caller inventory was independently checked and
Fable approved the boundary alongside the stable rebuild closure packet. At
that historical checkpoint, the F15 blocking reason referred to the exposed
preview paths and remained
BLOCKING until the resulting acceptance tests passed independently. Record
any scope correction separately from implementation progress.

No commit, installation, account change, provider activation or publication is
authorized by this document. The broader reviewed checklist remains intact.

## Architect source check, 2026-09-08

`skills/summon/scripts/_workspace_entry.py:WorkspaceHost.start` prepares the
operator demo and registers a durable recipient without launching a worker.
Registration must not be counted as execution-slot consumption or provider
contact. The ordinary plan/create/open/inspect surface therefore needs its own
message-admission and lifecycle evidence, not a fabricated worker launch gate.

The executable qualification fixture in
`skills/summon/scripts/_workspace_demo.py:main` can run the conductor loop or
same-task recovery. Its `ConductorDemo.launch` constructs `OwnedFakeWorker`
without a budget adapter, and `admit` follows launch. These are reachable
synthetic execution paths, even though they are not live-provider operations.
They must not be used as F15 end-to-end admission proof in their current form.

`skills/summon/scripts/_workspace_transport.py:OwnedFakeWorker` invokes
`BoundTransportAdmission.before_spawn` only when an adapter is supplied. The
legacy constructor still accepts an omitted adapter. The inventory must cover
this optionality, the actual fixed-work selection order, and recovery callers;
making a constructor argument mandatory without arranging those dependencies
would not establish correct admission.

## Review handoff, 2026-09-08

The conductor independently confirmed the caller inventory. Its alternative
of excluding worker/admission qualification from the preview is not adopted:
the promised simulated conductor and recovery journeys remain acceptance
requirements. At this historical review checkpoint, F15 stayed BLOCKING. The
completed Fable consultation identified
the integration requirements below; inspected-source advice does not replace
proof of the promised journeys.

The parallel rebuild correction packet now passes independent verification:
18 focused tests and the six-file affected partition of 194 tests plus 12
subtests on Python 3.12, with external plugin autoload and telemetry disabled.
Source hashes matched the conductor handoff after verification. RB-01 maps
projection serialization overflow to a static coordinator error; RB-02 rejects
boolean/float journal generations; RB-03 checks rich multi-segment lifecycle
and recovery parity with preserved uncertainty. Fable closure review is
complete with APPROVE; these results do not promote L03 or F15.

## Required integration closure

Fable's inspected-source findings F15-A through F15-F are accepted as work
items, with implementation correctness still subject to source and test proof:

- Register and queue messages before planning the exact complete turn. Under
  the coordinator owner boundary, evaluate pre-claim capacity and atomically
  persist the bound reservation with claim/selection. Before spawn, consume
  that exact durable reservation once, without requesting a second slot or
  changing selection. Persist launch intent before child/channel creation.
- Bind ordered multiple-message contexts, refusing reordered or missing entries.
- Derive budget state from coordinator/runtime facts instead of trusting caller
  declarations of active slots, pending messages or stream usage.
- Persist admission digests and bound revision/prefix in atomic turn provenance.
- Cover main, next-turn, worker-message, supervisor-message and task-recovery
  journeys, plus the supervisor consumer distinction above.
- Prove failure and concurrency behavior across consume/spawn/admit: no orphan
  process or unaccounted reservation, and no erasure of uncertain effects.

The architect refined Fable's suggested ordering after source inspection:
`admit_claim_selection` already serializes claim and selection without spawning.
Reusing this durable boundary is preferable to spawn followed by immediate
admission, which leaves a cross-instance capacity race. The read-only adapter's
per-instance lock is insufficient: an independent provider-free reproduction
using two coordinator objects for one journal accepted the same decision twice.
No worker or provider was started in that reproduction. Regression closure must
also cover separate requests competing for the last slot and restart after
durable launch intent. Unknown launch outcome must remain held; a generic
new-admission evaluator must not treat already-included messages as fresh work.

The single review returned a parsed terminal report with exact Fable root
served-model evidence. Its auxiliary model list was not treated as independent
review approval. Fable inspected source; the architect independently ran the
tests. The gate-wiring appendix below was added after the reviewed document
snapshot and is separate architect evidence, not part of Fable's reviewed text.

## Integrated release evidence follow-up

The earlier source inspection found rebuild and transport-budget tests absent
from fixed release commands. Current `tools/release_manifest.py` now includes
`test_swarm_projection_rebuild.py` in `swarm_coordinator`, and
`test_workspace_transport_budget.py` plus
`test_workspace_reservation_acceptance.py` in `workspace_core`. Independent
registry/manifest verification passed 33 tests. This proves wiring, not a
complete current-source run of these gates. In particular, the reported broad
workspace partition excluded `test_workspace_demo.py`; full simulated journey
evidence remains required. Schema inventory membership alone is not migration
or execution evidence.

## Supervisor delivery integration review

Independent review of the implementation in progress found that classifying
an actual supervisor offer solely as an empty-selection control request charges
journal capacity but adds no message count or bytes to rate and stream usage.
A provider-free evaluator reproduction accepted that request with both rate
and stream usage already at their limits. This is valid for control bookkeeping
but insufficient for message delivery. The conductor must bind authoritative
inbox message facts and charge delivery limits while requesting zero execution
slots. Do not relabel inbox deliveries as fresh worker messages. Current
endpoint ownership, lease and grant checks must also hold at durable consumption.
At that historical review checkpoint, F15 remained blocking until these
boundaries and the full journeys were verified.
