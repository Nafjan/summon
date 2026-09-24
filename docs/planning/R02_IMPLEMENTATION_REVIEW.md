# R02 Stage A implementation review

Updated: 2026-09-05. DEV-R02-01 is in progress under FABLE_R02_APPROVAL.md. No candidate acceptance or passing implementation gate is claimed yet. Preserve the full R01/R02 and workspace objectives.

## Early findings sent to the developer

| ID | Draft evidence | Required closure |
| --- | --- | --- |
| R02-IR1 | Initial inline native-refusal checks compared attempts to zero and the action dictionary by equality, allowing Python bool/int equivalence | One pure shared validator with exact scalar and nested types. Reject bool-as-int, int-as-bool, extra keys and malformed containers. Share reason constants with the public projection. |
| R02-IR2 | Retained refusal lineage must survive a restored registry while continuing to refuse under unresolved scope drift | Bind the relevant original admission scope privately, separate historical validation from current eligibility, test restored scope and continued drift, and never fall through to fresh dispatch on malformed lineage. Do not change the accepted v1 validator or relabel old facts. |
| R02-IR3 | Draft launchTurn used live state.selected after awaiting HTTP and unconditionally cleared/restored composer text; its active entry could also disappear through SSE before the response | Capture request room, participant and draft revision; update only matching state, preserve newer/other-room text, guard absent activity, and exercise HTTP/SSE reordering in the rendered journey. Remove the obsolete automatic-fork response branch. |
| R02-IR4 | Shared helper draft performed set membership before string validation and accepted true for historical v1 live_steering_acknowledged | Reject malformed scalar/list/dict values without leaking TypeError through public paths. Preserve literal false acknowledgement and exact nested types; historical data validation cannot manufacture a live acknowledgement. |

These are implementation corrections, not new owner permission requests. The lead will inspect the final representation and test evidence before closing either finding.

Intermediate independent helper probe: 23 malformed JSON-shaped cases tested with no source change during the probe and no providers. Twenty-two were rejected without unexpected exceptions. A historical capability with live_steering_acknowledged=true was still accepted; IR4 remains open and this result was sent to the developer. This is focused draft evidence, not final suite acceptance.

Subsequent source inspection confirms the helper now explicitly rejects historical live_steering_acknowledged=true and includes a regression assertion. Final independent execution remains pending. IR2 still requires a real first late-refusal path: manufacturing a correctly populated native finish cannot prove that the worker retained its original admission scope. The developer has this concrete closure requirement.

The lead prepared a private rendered harness using the existing synthetic runtime fixture and the real ConversationSurface. It supplies early candidate refusal, delayed worker scope drift, a second room, and independently controlled HTTP/worker delays. It has not been started or counted as rendered evidence; execution waits for the developer's candidate freeze. No provider was contacted during preparation.

## Intermediate rendered verification

The lead subsequently executed the harness against a private source snapshot while the developer continued corrections. This is actual browser interaction with the real ConversationSurface and a synthetic dispatcher, not source-string assertions or final candidate acceptance.

- Early candidate refusal preserved the composer and displayed the typed reason without another turn or room.
- Late scope refusal closed the accepted turn, restored the submitted draft into the empty composer, displayed the typed reason and removed the active-work row.
- Delaying HTTP while switching rooms preserved the other room's draft and the submitted draft in its originating room.
- A terminal event observed before the delayed HTTP response preserved newer same-room text; explicit restoration then restored the submitted text.
- Explicit fork created one context room. Synthetic room count rose from four to five; fake dispatcher calls remained at the three seed calls throughout. No provider was contacted.

Three visible defects were returned to the developer: obsolete automatic-fork help text, composer/cancel controls not recomputed after the terminal event, and a refused terminal still labelled as waiting for a model receipt. The developer reports corrections; the lead will inspect and rerender the corrected candidate before closing these findings. The snapshot server was instructed to stop after the journey. Private evidence includes its source fingerprints and sanitized result summary.

Corrected rendered rerun: all three defects are closed in the tested snapshot. The restored draft can immediately open review, cancelled-turn controls disappear after the refusal closes, the help text describes recovery controls, and the terminal identity card explicitly says CONTINUATION REFUSED. Fake dispatcher calls remained at the three seed calls. A non-blocking P4 follow-up remains: the character counter does not immediately recompute after automatic restoration. The second synthetic surface was instructed to stop after this check.

## Independent gate and preservation evidence

- The eight-module gate passed 225 tests in 98.59 seconds, with no failures, errors, skips, exclusions or source drift during execution: conversation, conversation runtime, conversation UI, chat refusal contract, resume capabilities, governed job continuation, governed job resume and Windows silent-launch tests. No provider calls. Private fingerprints bind the run.
- A real journal compatibility probe loaded the saved pre-R02 reader. It read a new native refusal finish; the current reader read an old success finish. The sole added public field was refusal_reason. Native refusal, identity and handle fields remained absent from the public projection.
- AST comparison against the saved baseline proves the _identity method is unchanged after removing the two new execution capability fields, and the ordinary _finish identity dictionary is unchanged. This is semantic source evidence for those constructions, not a claim that the entire refactored method has identical bytes.
- The developer is completing lasting first/prior-refusal lineage coverage, deterministic cancel/refusal ordering, unsupported backend/transport cases, both CLI result shapes and exact HTTP key-set assertions. These are already required acceptance cases; a green suite alone does not prove they were present. Only affected tests need rerunning after test-only additions.

Fable's final skeptical second review and the lead's milestone decision remain pending. The full workspace roadmap and Stage B qualification remain open.

Final candidate delta: the developer added the required first/prior-refusal, cancel-race, unsupported-lane, early/late CLI and HTTP-key coverage, removed trailing whitespace and corrected the restoration counter. Independent focused rerun: 87 tests passed in 32.28 seconds with no failures, errors, skips or source drift. The delta from the 225-test gate is confined to the three conversation test modules and the conversation page; these counts overlap and must not be summed. The developer separately reports 245 passing tests plus two subtests. The lead's final rendered check confirmed restored text and displayed counter both at 19 characters, with send enabled, closed cancel controls hidden and explicit refusal identity. The three seed fake calls stayed unchanged. Git diff --check is clean. Implementation bytes are held for final review.

## Independent review sequence

1. Review the completed diff against the private nine-file baseline, Fable A-01 through A-06 and the exact refusal contract. Classify every additional file, including the authorized Windows inventory ratchet; preserve pre-existing roster and P0 changes.
2. Verify early refusal creates no turn or room, late refusal closes the durable turn while retaining original native lineage, and actual Popen attempts cannot produce false not-run facts. Test cancel/refusal ordering and owner release.
3. Run relevant conversation/journal/UI, capability/governed continuation and Windows guard suites with telemetry, bytecode/cache writes and external plugin autoload disabled. The baseline collection was 73 tests, not a passing run. Record failures without exclusions.
4. Use an isolated fake dispatcher for the rendered browser journey. Check synchronous refusal, late refusal, same-room draft restoration, newer/other-room edits, independent activity and explicit fork with no provider launch. Keep artifacts private and distinguish actual rendering from static source assertions.
5. Review the migration notice and unchanged strict contracts, then return the source and verified evidence to Fable for final Stage A milestone acceptance. Stage B qualification remains open afterward.

HANDOFF: monitor the existing developer task; do not restart it after an observation timeout. R13_CAPACITY_FINDINGS.md is parallel investigation only and must not expand this patch.
