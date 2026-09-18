# Canonical local capture checkpoint

2026-09-14 strict aggregate update. A corrected Windows run with an explicit
Chromium executable completed all 27 fixed suites with 4,920 passed cases, 3
reviewed-policy skips and zero failures/errors. Its Windows qualification and
fixed gates are useful diagnostic evidence, but the packet is not source-bound
release evidence while the worktree is dirty and the source remains 3.4.0.
The live-provider gate is intentionally blocked for missing authoritative
evidence. U03/U07 native-device evidence and final L06/L09/L12 candidate work
remain open.

2026-09-12. Instrumented local validation only; this is not a clean candidate,
completed CI, live-provider qualification or publication approval. Readiness
remains 58 verified / 5 blocking / 63 requirements (92%, rounded).

## Completed evidence

The complete fixed `workspace_recovery` suite passes 72/72 cases, with zero
failures, errors, skips, deselections or collection problems. The unchanged
`tools/release_gates.py` `_run_outcomes` invoked the actual
`tools/release_collect.py` collector. An explicit private environment seam added
startup auditing and disabled bytecode/cache writes; commands, case selection,
source/Git facts, outcomes and skip policy were preserved.

The outer invocation completed in 837.627 seconds. A separate lead verification
completed in 0.787 seconds and confirmed the exact prior collection inventory,
strict canonical validation, unchanged producer result, original console hash,
source/Git stability, collector activation and owned-process cleanup. Fifteen
guard receipts contain zero violations. Observed children include eleven
installer invocations confined to fresh synthetic homes, two fixed synthetic
workers and one dispatcher dry run; no real installation or provider ran.

These artifacts bind the frozen payload used during execution. This checkpoint
and later planning edits change the full payload hash. Retain the original
bindings; do not relabel these artifacts as final evidence for a changed source
tree. Runtime and test files were not changed by this checkpoint.

## Incomplete core captures

Both attempts used the entire fixed `workspace_core` command, whose prior
collection contains 841 cases. Neither produced a complete canonical outcome.

| Collector deadline | Outer elapsed | Private console progress | Disposition |
| --- | --- | --- | --- |
| 600 seconds | 615.332 seconds | 812 success markers, no failure markers | Timed out; not passing-case or suite evidence. |
| 1,200 seconds | 1,203.216 seconds | 826 success markers, no failure markers | Timed out; not passing-case or suite evidence. |

Both retained unchanged source/Git facts, reaped the owned direct child, closed
the owned job handle and reported no cleanup error. Completed descendant guard
records have no violations, but each terminated collector lacks a final guard
receipt. Do not infer a clean complete run from partial progress or cleanup.
Only the private deadline changed between attempts. The production runner now
uses a bounded 1,800-second aggregate default for serialized Windows capture;
this increases observation time without changing per-child bounds or allowing
partial acceptance. No test was excluded and no skip waiver was added.

## Bounded performance diagnostic

One unchanged existing demo case completed: one collected, one passed, exit zero,
221.833 seconds outer and 221.293 seconds in its instrumented driver. Source/Git
stability and owned cleanup passed; two guard receipts contain no violations.
The case is `ConductorDemoTests.test_public_demo_computes_two_iterations_and_closes_only_after_observed_effect_resolution`
in `skills/summon/scripts/test_workspace_demo.py`. It is scoped diagnostic
evidence, not a replacement for the full core command.

The main-thread profile identifies repeated record/load/snapshot processing and
deep copying as candidate investigation areas. Causal attribution remains
unproven: audit callbacks were untraced, no uninstrumented control was run,
children were not profiled, inclusive times overlap, and four rows report self
time greater than cumulative time. Do not sum those timings, assign them solely
to application code, or remove validation, fencing or audit facts to optimize
them. A next measurement must separate audit cost from application work before
choosing a correction or another complete capture.

## UI preparation and next actions

A private draft preserves the complete sixteen-file `workspace_ui` command and
its previously collected 330 cases. Python syntax checks and an independent
Node syntax-only check pass. No UI collector, Node preload, browser or listener
was activated. The default headless-shell case and full-Chromium rendered/zoom
cases remain distinct. Native cleanup/dependency admissions and the legacy
browser request boundary require review before activation. This draft does not
prove OS network isolation or a cause of reported console flashes.

Complete core diagnosis and capture, resolve and execute the full UI capture,
then retain all remaining fixed suites/gates and aggregate rendered evidence.
The recovery result does not close all of L09. U03/U07 native-device evidence,
L06 final candidate privacy and L12 reserved final release conditions remain
open. The designated conductor reports a separate owner-directed review round;
do not infer a recovery/UI handoff or release evidence from that activity.
