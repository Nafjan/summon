# CI evidence acceptance checkpoint

Latest local execution: the full fixed workspace_recovery command passes
72/72 and separate lead verification confirms exact inventory, producer/output
bindings and cleanup. Core is still incomplete after two bounded timeouts;
UI remains an unactivated preparation. See L09_CANONICAL_CAPTURE_CHECKPOINT.md.
These source-bound local recordings do not constitute a completed CI run or
qualify a changed final candidate. The current 58/5 requirement count is unchanged.

Lead update, 2026-09-13: the rendered delivery producer now emits the
manifest's canonical hyphenated state-card filenames. A current `workspace_ui`
run passed 327/327, and the accessibility collector matched all 44 required
rendered files. A normal-budget full capture reached the combined gates but
browser security remained blocked by a safe Windows `publish_permission`
refusal in the shared temporary hierarchy; isolated affected cases pass. This
does not qualify a final candidate or actual AT/mobile behavior.

## windows-platform-skip-review

2026-09-13: two provider-inert cases are reviewed as Windows
non-applicability, not as passing evidence. The discovery FIFO-hang case and
the phase-0 POSIX process-group/FIFO cases require primitives that Windows does
not provide; Linux CI remains the execution platform for those regressions.
The exact node IDs, commands, literal skip reasons and `sys_platform=win32`
predicate are bound in `tools/release_skip_policy.json`. Missing tools,
permissions, provider credentials or unrelated skips remain blocking.

## strict-aggregate-capture-boundary

The release runner must produce one complete source-bound packet from the
current checkout. A runner timeout, partial console success marker, missing
collector output, stale rendered directory, source/Git drift, or an aggregate
run reconstructed only from isolated suites is a blocking outcome and must not
be converted into a pass. Isolated provider-free reruns are useful for
diagnosis and regression verification, but they do not replace the coherent
candidate capture. The final packet must show zero unexplained failures or
skips, the reviewed Windows exceptions only, complete rendered artifacts,
privacy validation, and the intentional live-provider gate state.

2026-09-12 disposition: CONDITIONAL. This checkpoint qualifies a bounded
independent review, not the release candidate or a completed CI run. The
initial requirement count was 58 verified and 5 blocking out of 63. R03/R10
were subsequently reopened and are now closed again after verified correction
and canonical retention; the current count is 58/5 (92%, rounded).

## Verified current corrections

Source inspection confirms rendered collection occurs before temporary output
cleanup, producer/reader rendered digests both use raw bytes, nested schema
readers reject booleans, and final checking requires matching Windows runtime
and platform qualification plus the required rendered file set. The separate
live-provider gate remains strict. A successful candidate artifact upload is
not final release approval.

The four release-tool test files ran together in an exact private source copy:
127 passed and one failed in 33.285 seconds. That failure expected a real Git
HEAD, which the copy deliberately lacked. The exact case then passed against
the checkout in a read-only, isolated process in 12.173 seconds. This is not a
claim of one clean 128-case invocation. Both completed runs had zero guard
violations, source changes, synthetic-home files, or bytecode files. Earlier
private harness failures are retained and do not qualify product behavior.

The combined run left `test_failure` loaded in the parent Python process,
confirming that the current test-local isolation correction is incomplete.
Ordinary passing assertions do not establish cleanup.

## Required finishing packet

The conductor owns one serialized integration packet for these findings. All
five now have corrections accepted by independent source review and the focused
execution below. All eight selected paths are now integrated and match the
reviewed composition, including the subsequent retention increment. The table
records the baseline defects and their required correction, not new open work.

| ID | Current evidence | Required correction and acceptance |
| --- | --- | --- |
| CE-01 | The Ubuntu informational job calls strict manifest intake before reporting unavailable Windows evidence. `_read_evidence` requires passing outcomes; the OS-privacy fixture skips on non-Windows and the skip policy has no exemptions. | Preserve strict general intake and final checking. Retain explicitly incomplete producer diagnostics on Ubuntu with source and structured-outcome validation. Do not manufacture a qualifying manifest, waive Windows checks, or turn unavailable outcomes into passes. Verify the actual diagnostic path and final refusal. |
| CE-02 | The accepted native-zoom test is absent from current fixed commands, producer/reader file requirements, and CI. | Integrate the already independently accepted test, full Chromium selection, six images and measurement JSON together. Retain the distinction between CSS viewport reflow, native zoom, and actual AT/mobile evidence. Verify exact packet retention and changed integration seams. |
| CE-03 | Both new release-evidence inventory rows describe numeric schema-1 records as unversioned. | Use the existing numeric kind, version 1 and `schema` discriminator. Preserve the outer schema-2 compatibility context and no-qualification caveats. Test the actual row identities and producer/reader references. |
| CE-04 | Windows uploads `.gates/*.json`; unrelated sibling JSON can survive because only rendered output requires a fresh directory, and intake validates embedded gate results. | Replace upload globs with finite reviewed filenames, including accepted zoom artifacts. Test that an unrelated file cannot enter the retained artifact selection. Preserve producer/reader binding and strict intake. |
| CE-05 | Both nested-collector isolation helpers omit the failed fixture module names; the combined run leaves `test_failure` loaded. | Save, evict and restore `test_failure` and `tests.test_failure` with the existing names. Add a same-name/different-root and failed-execution cleanup regression, then run the fixed combined outcomes/privacy command. Keep the correction test-local. |

Source references: `.github/workflows/ci.yml`, `tools/release_gates.py`,
`tools/release_manifest.py`, `tools/release_skip_policy.json`,
`tools/format_inventory.py`, `tests/test_release_outcomes.py`,
`tests/test_release_gate_artifact_privacy.py`, and
`skills/summon/scripts/test_workspace_os_privacy.py`.

## Independent correction acceptance

The corrected four-file release-tool partition independently passed 138 cases
in 34.387 seconds (47.640 seconds outer runtime). The one Git-HEAD-dependent
case was deliberately deselected in this private copy; its earlier separate
checkout result remains historical and is not a result for this candidate.
The owned collector-subprocess case, which the preparer's harness could not
complete, passed here. Eight read-only Git calls, one owned synthetic Python
collector and one hidden standard-library Windows version call were observed.
There were zero guard violations, owned-source changes, leaked fixture aliases,
synthetic-home files or bytecode. This is one scoped run, not a complete CI run.

A separate read-only reviewer checked all eight changed paths against their
baseline and candidate bindings, finding no drift or actionable defect. The
accepted zoom test is retained exactly. Fixed commands, producer and reader
requirements agree with the finite retained sets: 54 Windows files and 53
Ubuntu files, each containing 44 rendered artifacts. Numeric schema identities,
failed-collection cleanup and strict final refusal remain intact. The lead also
inspected the diagnostic validator and relevant refusal/cleanup assertions.

The eight-file packet is integrated without widening diagnostic output or release
qualification. No final gate row is promoted by this scoped acceptance; actual CI,
final candidate/source/privacy and native-device evidence remain separate.

The subsequent runtime-regression retention increment changes only the existing
fixed registry, CI selections and the registry test. Applied privately after
this packet, it independently passes all 20 release-registry cases in 1.765
seconds, with zero guards, children, source changes, leaked aliases, home files
or bytecode. All four new runtime regression files are retained in their related
existing suites. The accepted Chromium selection and finite artifact lists stay
unchanged. This increment is now integrated and matches the accepted composition.

The next L09 preflight found selected parameterized tests lacking explicit
source-literal IDs, which the collector correctly treats as unsafe identities.
Reconcile those test identities without weakening the collector before a
qualifying complete capture. Existing scoped passes do not establish this
collection qualification or actual screen-reader/mobile-keyboard behavior.

The completed source-only inventory covers all eleven fixed pytest selections:
151 distinct test files and 330 parameterizations. Of these, 248 decorators in
80 files lack explicit IDs; 76 have complete IDs for statically sized values;
six have literal IDs for generated collections whose cardinality must be checked.
Four additional generated/name-based collections are among the missing-ID set.
No nonliteral/partial ID list, static count mismatch, parametrized fixture or
applicable conftest was found. This is source evidence, not a test execution.
The bounded private correction must retain every case, value, order, mark,
fixture and assertion, plus all already valid IDs. Acceptance includes an AST
comparison excluding only ID metadata and actual guarded collection through
the existing collector. A collection-only result is never runtime pass evidence.

The lead independently verified all eighty changed files and 248 ID additions
against current shared source: non-ID AST, values, cases, marks, fixtures and
assertions are unchanged; all existing IDs and five collector/policy inputs are
preserved. One corrected owned collection completed in 28.871 seconds, mapping
3,298 distinct union cases across all eleven fixed selections with zero identity
or collection problems, guard violations or source drift. Zero tests executed.
The runtime used synthetic unused Capture platform metadata solely to avoid
hostname/OS probes; it did not finish or publish release outcomes or qualify a
real platform. Both earlier preparer setup failures remain recorded. The accepted
test-only patch is now integrated: all 151 selected files match the collected
candidate after newline normalization, and the other 256 compared runtime/test/
configuration files are unchanged. Actual canonical suite execution remains open.

Actual mapped counts: swarm_coordinator 168; workspace_plan 453; workspace_core
841; workspace_recovery 72; workspace_ui 330; release_outcomes 95; model_catalog
15; schema_inventory 213; phase0_phase1 1,481; browser_security 204; accessibility
159. These selections overlap; do not add their counts as distinct executed tests.

## Completion boundary

Return the exact integrated files, changed-seam results and remaining findings
before another candidate capture. Retain previous accepted evidence where its
source and scope still match; do not buy another unchanged provider review.
Actual screen-reader and native mobile-keyboard cases remain NOT RUN in
U03_U07_DEVICE_ACCEPTANCE.md. Final privacy, integrated candidate and reserved
release actions remain separate. No installation, account change, provider
call, Git mutation or publication was performed by this independent review.
