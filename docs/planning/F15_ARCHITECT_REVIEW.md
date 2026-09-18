# F15 architect acceptance

Decision: APPROVE for the provider-free 3.5 workspace preview, 2026-09-08.
This closes F15 under `RELEASE35_SCOPE_DECISION.md`. It does not approve the
whole release, installation, commits, publication or live-provider operation.

Subsequent-delta qualification, 2026-09-08: the conductor added explicit
`preferred_delivery_ids` after the accepted checkpoint. The earlier full-gate
results below qualify that checkpoint, not this later change. Independent
no-I/O checks pass for task and inbox successors in both insertion orders,
with historical parent state unchanged. Focused supervisor and reservation
tests pass. The final full demo completed with exit code 0: 17 tests and
13 subtests in 577.37 seconds. The affected coordinator/projection/reservation/
transport partition passed 86 tests in 135.32 seconds. The retained lineage
test was then corrected to distinguish a held parent from its queued successor;
that node passed in 35.11 seconds. The architect inspected both terminal results
and the corrected assertion. This closes the final delta at the same bounded
preview scope. F15 is frozen for the R01/R02 handoff; the release count remains
36 verified of 63, and the whole candidate is not release-ready.

## Requirements and evidence

| Requirement | Current source and verification |
| --- | --- |
| Reserve before worker creation | `_swarm_coordinator.py` derives authoritative budget facts and atomically reserves claim/selection; `consume_workspace_turn_budget` persists one-use launch intent before `_workspace_transport.py` creates the child. Reservation and transport-budget fixtures cover pre-spawn refusal, cancellation, expiry, missing/revoked/rotated grants and duplicate consumption. |
| Bind complete ordered context | `BoundTransportAdmission` binds the planned frame, worker identity and ordered selected entries. `test_multi_entry_context_preserves_order_and_accepts_before_popen` and mismatch cases cover multi-message transport; the full demo verifies computation from agent-authored context. |
| Retain durable authority and provenance | Reservation acceptance tests cover a second coordinator object, fresh Python process reload, prefix/revision mismatch, persisted consumption/launch intent and retention of subsequent records. Ownership and capacity checks remain in the sole coordinator writer. |
| Bound supervisor delivery separately | `_workspace_runtime.py` requires a durable offer-budget decision before `send_offer`. Coordinator consumption rechecks endpoint owner, lease, receiver grant and content. The budget evaluator refuses inbox execution slots and mixed inbox/task selection. The supervisor tests exercise pre-write rate/stream/oversize refusal and real delivery, revocation, concurrency and stale receipts. Injected snapshot cases prove the refusal boundary; they are not production load measurements. |
| Preserve recovery and parallel work | Projection now prefers a pending recovery successor over its preserved held/dead-lettered parent sharing the logical message ID. Endpoint and same-task recovery pass; full demo includes two-worker recovery/collision, two iterations and overlapping side work. Sequential A-to-B handoff expects one live worker; the separate parallel conductor still requires two live workers and three worker instances overall. |
| Preserve failure semantics | Refusal before admission creates neither a claim nor a worker. Launch intent and unknown exposure survive interruption; new recovery lineage does not erase parent uncertainty or reattach an old consumer. Full demo and reservation/transport suites cover these boundaries. |

## Verified commands and limits

Terminal output inspected by the architect:

- Complete `test_workspace_demo.py`: 17 tests and 13 subtests, 717.30 seconds.
- Workspace-core partition excluding the separately run demo: 793 tests and
  69 subtests, 226.72 seconds. Together these commands cover the current fixed
  `workspace_core` file list in `tools/release_manifest.py`.
- Coordinator, projection rebuild, transport-budget, reservation acceptance,
  schema inventory and release registry partition: 123 tests, 166.69 seconds.
  It includes both files in the fixed `swarm_coordinator` gate.

All three completed with exit code 0. The conductor confirms this source
checkpoint remained stable, with no provider/network/auth/install/commit or
release actions. The architect independently ran the 10-test pure budget suite,
30 affected budget-integration/transport tests, earlier reservation acceptance
and rebuild checks, and a no-I/O recovery insertion-order check. Counts overlap
and must not be added into a larger coverage claim.

Source review followed the complete worker/message/recovery caller inventory,
the pre-spawn gate, current-authority rechecks and final test assertions. This
acceptance is the architect's decision; the earlier exact Fable review approved
the rebuild packet and architectural scope, not this later complete diff.

Low-level detached/legacy transport constructors remain explicitly unqualified.
No universal CLI transport, native child-session attachment, live mid-turn
steering, unlimited concurrency, simultaneous multi-process workload scale,
power-loss durability or live-provider qualification is inferred from these
tests. The existing bounded cross-process owner checks and fresh-process replay
are narrower evidence. Other migration, UI, privacy and release gates stay open.

## Next handoff

Proceed with R01/R02 under `RELEASE35_IMPLEMENTATION_HANDOFF.md`: complete the
provider-free capability producer/consumer migration and current launch-bound
evidence seam. Preserve historical records and fail-closed authority. Keep
council/deliberation, exact-model, roster and spend boundaries intact. Return
source-backed acceptance evidence; do not activate or qualify live providers.
