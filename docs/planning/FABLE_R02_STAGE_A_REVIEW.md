# R02 Stage A skeptical second review

Date: 2026-09-05. FABLE-R02-02 completed successfully through Summon with the exact-required Fable seat and reported served identity claude-fable-5-1. The lead verified the dispatcher result and source preservation. VERDICT: CONCERNS. The lead's decision is REVISE; Stage A is not accepted yet.

The owner designated the lead as product lead/main architect and Fable as the principal consultant and skeptical second reviewer. This report is technical advice; the lead independently verified its blocking finding before assigning a correction. It creates no provider, account, installation, commit or release authority.

## Blocking finding

F-01: unchanged same-participant continuation fails on the third turn. The fresh identity uses owner_generation=1 while persistent owner claims advance generations. Admission compares the previous finish's generation against that placeholder; the new post-claim and pre-Popen decisions also need the actual acquired generation. This is a latent baseline problem made material by Stage A's explicit refusal path.

The lead reproduced success, success, blocked through the local synthetic dispatcher with no source change. The third result reports continuation_identity_incompatible; only two fake calls occurred and no provider was contacted. Earlier passing tests exercised shorter successful chains and did not prove repeated continuation.

Required correction:

1. Make admission compatibility generation-neutral because ownership has not been acquired yet. Do not grant ownership from the placeholder.
2. Stamp the actual owner generation before the post-claim decision and the actual job-owner generation before the worker decision. Preserve stale-owner and lease fences.
3. Prove at least three consecutive unchanged turns resume the same handle after the first; prove another continuation after late refusal and restoration; prove a fresh/requested generation lower than the stored generation is refused at the real post-claim fence. The last comparison is deliberately stated precisely: a lower stored generation is not itself a regression.

## Advisory findings and disposition

| ID | Advice | Lead disposition |
| --- | --- | --- |
| F-02 | Carry original capability and registry scope explicitly through the post-claim execution object | Include with the narrow generation correction; do not rely only on transitive equality |
| F-03 | A restored-scope test name promises a drift half covered elsewhere | Correct the name or coverage in the focused test delta |
| F-04 | Typed-refusal handling after a dispatch attempt has no direct exception-injection test | Add a focused defensive test if the branch remains; never fabricate not-run facts after Popen was attempted |
| F-05 | Early-refusal recovery panel is page memory and disappears on room re-render; draft survives | Track as a separate preview UX follow-up; no silent retry or auto-fork |
| F-06 | A backend change before launch reports capability change before identity mismatch | Explain the bounded reason precedence in migration guidance |

Fable inspected the source and tests for A01-A06 and found no other blocker. Its full-runtime probe was limited by its Windows job environment; the lead's successful local reproduction supplies the decisive evidence. The 225-test gate, final 87-test focused run, reader compatibility and rendered journeys remain valid for their recorded scope and bytes; they do not contradict F-01 or establish milestone acceptance.

## Handoff

Closure update: the developer applied the generation-neutral admission and actual-owner stamping at both later fences, explicitly carried original capability/scope into post-claim execution, and added repeated-turn, restored-lineage and real post-claim stale-generation coverage with lock-release/no-extra-dispatch assertions. The lead independently ran the final conversation/runtime files: 63 passed and 2 subtests, no failures, skips or source drift across the four affected files. F-01 is locally closed. Final bounded Stage A acceptance remains pending a narrow Fable closure review; the original CONCERNS verdict is retained as review history, not silently relabelled.

DEV-R02-01 implements the narrow generation and explicit-lineage correction after the urgent Windows test-launch audit. The lead verifies the new repeated-continuation cases and affected runtime tests, preserves unrelated work, and asks Fable to review only the correction and closure evidence. No broad or cosmetic re-review is needed. R01/R02 Stage B and the full workspace roadmap remain open.

The review's outer Windows launcher initially failed because of command quoting. A provider-inert version invocation reproduced that wrapper failure, the corrected invocation passed dry-run with zero attempts and no provider contact, and the subsequent live review succeeded. The failed outer launch and final review are retained privately as separate evidence. A reviewer-reported harness plan artifact is not project source and is not used as proof.
