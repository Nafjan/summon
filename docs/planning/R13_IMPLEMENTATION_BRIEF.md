# R13 implementation brief: durable task/message journal capacity

Status: interface direction reviewed by Fable; the lead authorizes the bounded
implementation increments below. No completed runtime or release is claimed.

Date: 2026-09-05

## Scope

R13 makes the first durable task/message slice honest under journal pressure.
An admitted claim must be able to record its bounded cancellation, terminal,
owner-loss, and recovery outcome without consuming a control reserve that an
ordinary message or progress event can exhaust.

This is a capacity and durability contract, not a promise that every queued
task can finish.  The current coordinator still has `MAX_TASKS = 1024`,
`MAX_ATTEMPTS = 32`, and an eight-MiB aggregate journal cap.  Those values are
not evidence of a qualified 16-worker or long-horizon configuration.

## Invariants

1. A record is admitted and accounted for using the exact bytes that will be
   written: canonical UTF-8 JSON, checksum, timestamp, and one explicit LF
   byte.  The capacity check must not reconstruct a different string later.
2. A record is durable only after the owned write and required flush/fsync
   complete successfully.  A readable or checksummed line after an fsync or
   disk error is not proof of durability.  Reconciliation must establish
   durability under the current owner or retain a persistent uncertainty
   state; it must never retry automatically after an unknown write.
3. Legacy schema-1 CRLF/LF segments remain readable.  New records use the
   frozen LF byte representation.
4. An obligation remains reserved through `indeterminate` and recovery.  A
   worker exiting, a claim release, or a readable partial line does not free
   settlement capacity until the required terminal/recovery record is durable.
5. Duplicate operation keys with identical payloads are replay-safe.  A
   conflicting replay is rejected without consuming another reserve slot.
6. Admission, cancellation, terminalization, and recovery retain the existing
   owner/generation/checksum fences.

## Minimal internal interfaces

The implementation should add narrow internal helpers rather than a second
coordinator:

- `_rundir.encode_journal_record(record) -> bytes`: freeze timestamp,
  canonical JSON, checksum, and LF once.
- `_rundir.journal_append(...) -> JournalAppendResult`: distinguish durable,
  rejected-before-write, and durability-uncertain outcomes.
- `_swarm_coordinator._record_bytes(record)`: use the exact encoded bytes for
  admission and the append operation.
- `_swarm_coordinator._capacity_snapshot()`: expose used bytes, hard cap,
  ordinary-admission ceiling, outstanding obligations, and uncertainty without
  exposing message bodies or local paths.
- A typed per-obligation accounting helper that is shared by claim, cancel,
  terminal, owner-loss, and recovery transitions.

No public receipt should claim a durable cancellation or terminal event when
the append result is uncertain.

## Bound derivation

Do not set `CONTROL_RECORD_MAX` from a convenient round number.  First freeze
the maximum serialized lengths for identifiers, operation/idempotency keys,
request and artifact digests, timestamps, lease/generation integers, bounded
reasons, and all control metadata.  Then generate every control record at
those limits, encode it with the production serializer, and take the largest
byte length including checksum and LF.

The current provider-inert sizing probe observed these bytes under synthetic
limits: cancel request 3,841; cancel acknowledgement 906; worker terminal
4,435; cancel-indeterminate 4,245; recovery-indeterminate 3,840;
recovery-authorization 3,701; run close 305.  These values show that a
universal four-KiB allowance is unsafe.  They are evidence for the derivation,
not the final bound.

`MAX_ATTEMPTS = 32` is not a renewal budget.  Derive the maximum routine
renewals from an explicit lease duration and renewal cadence.  Keep routine
renewals separate from emergency settlement records so a long-running task is
not accidentally capped at an undocumented wall-clock duration.  Define the
honest behavior when the routine-renewal budget or ordinary journal budget is
exhausted: stop new admission, preserve the lease/uncertainty state, and do
not fabricate completion.

## Reserve formula

Let:

- `H` be the hard aggregate byte cap (currently eight MiB);
- `U` be bytes already persisted in all journal generations;
- `E` be the exact byte length of the new record being considered;
- `O` be the set of outstanding settlement obligations after that record is
  applied;
- `B(o)` be the exact bounded byte budget for obligation `o`;
- `C` be the run-close bound; and
- `R` be separately derived recovery headroom for owner loss and uncertain
  durability.

The post-admission reserve is:

```text
reserve_after = sum(B(o) for o in O) + C + R
admit iff U + E + reserve_after <= H
```

The candidate record `E` is counted once.  It must not also be counted as a
second copy in `reserve_after`.  A successful durable terminal/recovery event
removes only its settled obligation.  An indeterminate outcome retains the
obligation until recovery is durably authorized and recorded.

The default active-worker setting remains four.  Sixteen concurrent workers
are a separate qualification target.  Capacity is calculated from actual
obligations, not active worker count alone; a small number of indeterminate
claims can require more reserve than many ordinary active claims.

## Old-run behavior

For an existing schema-1 run, do not rewrite history or pretend that a reserve
was retroactively guaranteed.  If the current used bytes cannot satisfy the
new reserve, report `capacity_degraded`, refuse new ordinary admission, and
continue to expose existing claims and uncertainty.  Never raise the hard cap
silently and never discard old segments to make room.

## Lead resolution of durability/replay advice

The independent storage review verified that the coordinator acquires/releases
an owner for each mutation. A normal acknowledged record therefore becomes a
superseded segment tail. Supersession alone is not a failed-write signal.
Re-appending an existing claim event also fails the reducer's duplicate-claim
check; re-appending message/artifact events duplicates effects. The lead rejects
blanket tail demotion and re-recording an outcome with its original event type.

Distinguish current durable persistence, the original caller acknowledgement,
and external/provider effects. Successful synchronization of an exact verified
prefix can establish the first; it does not reconstruct the other two.

Before inherited bytes justify an idempotent durable response, new admission or
reserve refund, the coordinator must validate and synchronize the exact prefix
it will replay under current ownership. Check segment identity/length/content
and the owner fence around verification; bytes outside that prefix are not
verified. Unreadable, unstable, torn or conflicting input refuses the affected
mutation. A post-error marker or a volatile flag alone is insufficient because
the storage medium can also prevent that marker from persisting.

An uncertain append poisons that ownership instance for further writes and
does not return an idempotent success or free obligations. Successors use the
reopening rule above rather than trusting the volatile flag. Reconciled complete
events replay once; no duplicate append, automatic redispatch or fabricated
provider certainty. Strict reconciliation reads must not turn a read error into
empty history. Normal legacy generations remain valid and prospectively gain
the reserve checks; no separate format marker is introduced merely for R13.

## Authorized implementation increments

1. Runtime maintainer implements the exact stamped/checksummed UTF-8+LF encoder
   and owned append seam in `_rundir.py`, plus focused journal tests. Preserve
   existing `journal_append` callers: non-success must still raise a compatible
   exception, never return a structured failure that an old caller can ignore.
   Provide bounded typed phase/record-identity failure information. Write exactly
   the bytes admitted; retain legacy CRLF/LF reading and owner fencing. This
   increment alone does not resolve coordinator replay or complete R13.
2. After the lead verifies that delta, integrate strict coordinator prefix
   reconciliation and finite-path settlement accounting with the existing
   mutation boundary. Use the derived reachable-path invariant, prospective
   degraded legacy behavior, separate routine-renewal allowance and explicit
   P1 event classification. Preserve unknown provider/cleanup facts independently
   from journal durability. Do not enable P1 execution before this gate passes.

No install, credential change, commit, release, provider invocation or unrelated
runtime refactor is included. Additional implementation questions stay with the
assigned specialist lane; escalate a contradiction that invalidates these
invariants rather than silently weakening them.

## Required acceptance evidence

Before implementation is considered complete, run provider-inert tests for:

1. Exact `H - 1`, `H`, and `H + 1` behavior using persisted bytes on POSIX and
   Windows, with fixed and variable timestamps, Unicode expansion, and mixed
   legacy newline segments.
2. Ordinary message/progress admission stopping at the soft boundary while an
   already admitted claim can still record bounded cancel, acknowledgement,
   terminal, and recovery outcomes.
3. Concurrent last-slot claims, cancellation races, owner loss, recovery, and
   generation fencing.
4. Identical idempotent replays, conflicting replays, and operation-key bounds.
5. Partial write, flush/fsync failure, disk-full, and uncertain-durability
   reconciliation.  No case may automatically retry an unknown write.
6. Old schema-1 read compatibility and explicit `capacity_degraded` behavior.

Acceptance requires exact byte accounting, no double counting, no reserve leak
after an unresolved claim, and no durable-success claim after an uncertain
write.  R13 remains open until these tests and an independent technical review
pass.
