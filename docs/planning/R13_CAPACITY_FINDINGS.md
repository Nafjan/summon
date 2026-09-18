# Journal capacity prerequisite for the message fabric

Updated: 2026-09-05. Source investigation and provider-inert probes only. R13 remains open; no coordinator or journal source was changed. This work runs alongside DEV-R02-01 and does not expand its implementation scope.

## Verified findings

| Finding | Source and independent evidence | Consequence |
| --- | --- | --- |
| No control reserve | `_swarm_coordinator.py:_append` applies the same aggregate eight-MiB limit to every event. `_apply_cancel_request`, cancellation acknowledgement/outcome and terminal completion all use that path. A synthetic one-claim run with the cap set to its current used bytes refused cancellation, left journal bytes unchanged and correctly left cancel_requested false. | Ordinary message/progress admission can consume capacity needed to record control/terminal events. Reserve must be enforced before accepting work; a refused cancel must not be presented as durable. |
| Windows byte undercount | `_append` estimates UTF-8 JSON plus one newline byte. `_rundir.py:journal_append` writes text with platform newline translation. At a synthetic exact boundary with a fixed timestamp, the same cancellation record was estimated at 473 bytes but occupied 474 bytes on Windows. The append succeeded one byte above that synthetic cap. | Check and write the same encoded bytes, including newline policy. Do not rely on a separately reconstructed character/newline estimate. |
| Separate timestamps | Coordinator estimation and journal writing each call time.time separately. Source proves they can serialize different timestamp representations; this probe fixed the timestamp deliberately. | Freeze the stamped, checksummed serialization once. Variable timestamp-length impact has not yet been measured and is not claimed as another reproduced failure. |

The probes used an isolated synthetic coordinator and in-memory cap/time substitutions. They made no provider calls, touched no real workspace journal, and left coordinator source unchanged. Counts and outcomes above are sanitized; private fixture records remain outside source.

## Control record sizing investigation

A separate provider-inert encoding probe used the current _journal_line serializer, 128-character identifiers, 64-character digests, attempt 32, 19-digit generation/lease values, a finite float timestamp and LF. The currently permitted U+0001 reason character expands to six JSON bytes; 512 such characters occupy 3,072 bytes before surrounding keys and metadata.

| Synthetic record | Encoded bytes including LF |
| --- | ---: |
| Cancel request | 3,841 |
| Cancel acknowledgement | 906 |
| Worker terminal with bounded reason | 4,435 |
| Cancel indeterminate outcome | 4,245 |
| Recovery indeterminate record | 3,840 |
| Recovery authorization record | 3,701 |
| Run close | 305 |

These examples disprove a universal four-KiB control allowance. They are not a proof of the maximum accepted record: the coordinator's generic integer validator and owner-generation path do not share the wire protocol's uniform 63-bit ceiling. A numerical reserve needs explicit bounds and whole-record byte validation. Distinguish capacity reserved to settle admitted claims from a promise that all queued or released tasks can finish: current close refuses unfinished tasks, and a new queued-task terminal transition would be additional lifecycle design rather than an accounting detail. The developer is preparing a read-only proposal while R02 remains frozen for review.

## Required design before P1 admission

1. Derive an explicit maximum serialized size for each terminal, cancel, acknowledgement, owner-loss, recovery and run-close record. Include response/idempotency metadata, identifiers, UTF-8 expansion, timestamp, checksum and newline. Enforce these bounds at encoding, not only in display projections.
2. Bound permitted control counts per admitted attempt. Repeated semantic cancellation, new message identifiers, renewals and recovery commands must not consume an unlimited emergency allowance. Preserve honest idempotency and replay-conflict behavior.
3. Reserve against the maximum admitted active attempts, not the task count. A conservative formula is the sum of each outstanding required control/terminal allowance plus run-close/recovery headroom. Admission must satisfy used bytes plus the exact new record plus the post-admission reserve within the hard cap.
4. Keep the V3 proposal of four default active workers and separately qualified opt-in sixteen distinct from current MAX_TASKS=1024. Neither task count nor worker count alone proves the combination fits eight MiB. Numeric reserve values remain unapproved until record/count bounds are derived and tested.
5. Define old-run behavior when existing used bytes and active claims cannot fit the new reserve. Stop new ordinary admission; preserve histories, claims and uncertainty. A reserve cannot be retroactively guaranteed by relabelling an already-full journal or silently increasing retention limits.
6. Separate logical cap refusal from physical write/flush/fsync failure. Partial or uncertain persistence cannot claim a durable cancellation. Stop further launch admission and perform cleanup only against verified owned resources; the provider-neutral coordinator cannot invent ownership of external child processes.
7. Preserve the generation/owner fence and checksums. Changing segment encoding must retain old CRLF/LF read compatibility. R14 rollover cannot erase aggregate retention limits or substitute for R13's control reserve.

## Next acceptance probes

- Exact cap-minus-one/cap/cap-plus-one behavior using the bytes actually persisted on Windows and POSIX; fixed and variable timestamps, Unicode and mixed historical newlines.
- Ordinary admission stops at the soft boundary while an admitted claim can still record bounded cancel, acknowledgement and terminal/recovery outcomes at the hard boundary.
- Concurrent cancels, repeated command IDs, conflicting replays, owner loss and last-slot claim admission retain deterministic ordering and reserved capacity.
- Inject partial writes, flush/fsync failure and disk-full outcomes; distinguish not-written, indeterminate durability and acknowledged durable records without launching providers or acting on unrelated processes.

HANDOFF: after the R02 slice is stable, prepare one source-backed R13 implementation proposal covering exact serialization and numerical control reserve together. Obtain Fable technical approval, then implement and independently verify. Do not mark R13 complete from these two probes or from a larger configured cap.
