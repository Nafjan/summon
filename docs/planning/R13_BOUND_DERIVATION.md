# R13 bound derivation: executable control-record proposal

Status: design-only, provider-inert, not approved constants.  No coordinator,
journal, or protocol runtime code was changed for this derivation.

Date: 2026-09-05

## Source limits used

The derivation uses the current `_swarm_protocol.py` and
`_swarm_coordinator.py` validators, then adds the proposed operation metadata
needed for idempotent settlement accounting.

| Field class | Bound used | Source or proposal |
| --- | ---: | --- |
| IDs (`task_id`, `claim_id`, worker, message, operation, recovery) | 128 UTF-8 characters | `_ID_RE` |
| SHA-256 digests | 64 ASCII characters | `_SHA256_RE` |
| Attempt | 1,024 | protocol validator; coordinator policy remains 32 |
| Generation, lease expiry, sent time, operation sequence | `2^63 - 1` | protocol positive-integer ceiling |
| Reason/preview/text fields | 512 characters | `_bounded_reason` / `_text` |
| Message content length | 12,000 characters | `MAX_MESSAGE_CHARS` |
| Artifact bytes | 64 MiB | `MAX_ARTIFACT_BYTES` |
| Capabilities/versions | 32 / 8 entries | protocol limits |
| Proposed operation metadata | `operation_id` (128), `operation_seq` (uint63), `obligation` (32), `durability` (24), and recovery fields (`recovery_id` 128, `recovery_state` 32) | R13 proposal |

The worst current reason encoding is not one byte per character.  The existing
validator permits U+0001; JSON escapes it to six ASCII bytes.  The probe uses
512 such characters (3,072 encoded bytes) to remain conservative.  The final
implementation may reject additional control characters, but it must derive a
new bound after that change rather than silently reuse this one.

Current journal serialization is `_rundir._journal_line`: canonical JSON,
SHA-256 over the stamped record, a `sha256` field, and one newline.  The probe
freezes `generation`, `ts`, and the proposed metadata before calculating this
line.  The production change must freeze the same bytes before both capacity
checking and writing; a readable line after a failed flush/fsync remains
uncertain, not durable proof.

## Derived encoded sizes

The following are lower-bound design results for the proposed metadata and all
current field maxima above.  They include the checksum and LF byte.  They are
not release constants until the implementation uses the same constructors and
the boundary tests pass on Windows and POSIX.

| Record constructor | Bytes including LF | Settlement class |
| --- | ---: | --- |
| `claim_granted` | 1,643 | admission record; not counted again in post-admission reserve |
| `claim_renewed` | 1,247 | routine renewal |
| `message_posted` | 4,545 | ordinary context/event record |
| `artifact_published` | 7,724 | artifact metadata record |
| `task_failed` with maximum reason | 4,671 | terminal settlement |
| `cancel_requested` with maximum reason | 4,077 | cancellation settlement |
| `cancel_acknowledged` | 1,142 | cancellation settlement |
| `task_cancelled` with maximum reason | 4,451 | terminal settlement |
| `claim_indeterminate` with maximum reason | 4,661 | uncertainty settlement |
| `claim_released` with maximum reason | 4,450 | release settlement |
| `worker_registered` | 1,319 | worker-control record |
| recovery uncertainty marker | 4,112 | recovery settlement |
| recovery authorization/decision | 4,122 | recovery settlement |
| `run_closed` | 623 | run-level close |

Under these assumptions, a proposed first lower bound is 4,671 bytes for a
control/settlement record, 7,724 bytes for an artifact metadata record, and
623 bytes for run close.  These values must not be promoted to constants until
the constructor, numeric timestamp policy, and operation metadata are frozen.
Artifacts are not settlement records, but their metadata still needs its own
ordinary-admission bound.

## Settlement-obligation state table

The reserve follows obligations, not merely the number of active workers.  It
must account for the maximum **reachable settlement path**, not just the next
record.  A candidate record is counted once in `E`; it is not also included in
the post-state path budget.

| State | Reachable path before any new ordinary grant | Outstanding reserve behavior |
| --- | --- | --- |
| `claimed` | normal terminal or release; cancellation may branch to the paths below | Retain the maximum reachable path, including cancellation |
| `cancel_requested` | one acknowledgement, then clean outcome **or** indeterminate outcome | The cancellation request is already the candidate; retain all later branches |
| `cancel_acknowledged` | one clean outcome **or** indeterminate outcome | Acknowledgement does not settle the claim |
| `indeterminate` | one recovery decision plus one recovery resolution | Original uncertainty remains reserved until resolution |
| `owner_loss_uncertain` | one uncertainty marker if absent, then the same recovery path | Owner changes cannot mint another emergency path |
| `recovery_authorized` | recovery resolution remains required; a new claim needs a separate ordinary reservation | Never refund the old uncertainty before reconciliation |
| `terminal` / `released` / `blocked` without uncertainty | no claim settlement records | The claim obligation is settled; run close remains global |

Under the provisional event sizes above, the finite no-new-grant paths are:

| Reachable path | Arithmetic | Bytes |
| --- | --- | ---: |
| normal terminal | `4,671` | 4,671 |
| normal release | `4,450` | 4,450 |
| clean cancellation | `4,077 + 1,142 + 4,451` | 9,670 |
| cancel → indeterminate → recovery decision → resolution | `4,077 + 1,142 + 4,661 + 4,122 + 4,122` | 18,124 |
| owner loss → recovery decision → resolution | `4,661 + 4,122 + 4,122` | 12,905 |

Therefore the provisional settlement path budget for an admitted claim is the
maximum reachable path, 18,124 bytes under these assumptions, before routine
renewal allowance and run-close headroom.  This is a design result, not an
approved constant.  A cancellation or owner-loss path that wants to retry must
stop at an explicit `new ordinary grant required` boundary: the new claim and
its own path budget are admitted separately.  Do not hide a new claim inside
the emergency reserve or promise an unbounded recovery chain.

The implementation must assign a stable `operation_id`/operation key to each
semantic obligation and reject conflicting replays.  Repeated recovery calls,
owner changes, and equivalent uncertainty observations must reuse the same
obligation and cannot append duplicate uncertainty markers.  A new attempt
requires a fresh bounded reservation; a configured task-attempt ceiling may
limit how many such reservations are allowed, but it is not a renewal budget.
`claim_released` is not legal as a way to clear unresolved uncertainty.

For a durable state `s`, define `V(s)` as the maximum sum of encoded event
bytes over every allowed settlement path that does not acquire new ordinary
capacity.  Every transition must satisfy:

```text
event_bytes + V(next_state) <= V(current_state)
```

unless it first acquires an explicit additional ordinary reservation.  The
transition table and path generator must prove this inequality for claim,
cancel, acknowledgement, outcome, owner-loss, recovery, terminal, and release
edges.  Routine renewals use their separate declared allowance below and may
not borrow emergency settlement reserve.

## Renewal policy

`MAX_ATTEMPTS = 32` is not a renewal budget.  The implementation must declare a
finite routine renewal policy before admission:

```text
N_routine = ceil(max_task_duration_ms / renewal_interval_ms)
```

Both values are policy inputs with explicit maxima.  If the resulting routine
renewal obligation cannot fit alongside settlement obligations, ordinary
admission stops.  Routine renewals cannot borrow the emergency settlement
reserve, and an exhausted routine budget must surface as a typed capacity or
liveness state rather than silently shortening the task or claiming completion.

## Reserve equation

For hard cap `H`, persisted bytes `U`, candidate record bytes `E`, settlement
path budget `V(s)` for the post-state, routine-renewal allowance `Q(s)`, run-
close bound `C`, and recovery headroom `R`:

```text
reserve_after = V(post_state) + Q(post_state) + C + R
admit iff U + E + reserve_after <= H
```

`V(post_state)` is the maximum reachable settlement path over all outstanding
obligations, not a sum of only their next records.  The candidate record `E`
is not double-counted in `reserve_after`.  A terminal or recovery record
reduces `V` only for the obligation it durably settles.  An indeterminate
result retains the original obligation until reconciliation.  A new claim or
recovery retry must acquire an explicit ordinary reservation before it can
increase the reachable set.

## Focused provider-inert proof plan

1. Generate every constructor at the bounds above and assert its exact UTF-8
   length, checksum, and LF byte.
2. Generate the finite reachable-state graph, enumerate every declared path,
   and assert `event_bytes + V(next) <= V(current)` for every transition.  A
   retry edge must terminate in `new ordinary grant required`, not recurse into
   an unbounded emergency path.
3. Repeat cap-minus-one, cap, and cap-plus-one admission on POSIX and Windows,
   with fixed and variable timestamp lengths, Unicode, and legacy CRLF/LF
   segments.
4. Exercise concurrent last-slot claims, cancel races, owner loss, recovery,
   identical operation replay, and conflicting replay.
5. Inject partial write, flush/fsync, and disk-full failures.  A readable line
   after an uncertain write must remain uncertain and must not be retried
   automatically.
6. Verify that unresolved obligations survive restart and that terminal/release
   transitions do not refund their reserve prematurely.

This document is the executable-bound proposal requested for R13 review.  It
does not authorize runtime implementation; Fable/lead approval is required
before changing `_swarm_coordinator.py` or `_rundir.py`.
