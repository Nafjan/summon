# Fable R02 Stage A implementation approval

Date: 2026-09-05. FABLE-R02-01 completed successfully with authoritative reported served identity `claude-fable-5-1`, verified by the lead. STATUS: DONE. VERDICT: CONDITIONAL. Stage A implementation and its exact refusal schema/five reason codes are approved under A-01 through A-06 below. Final milestone acceptance requires source, tests and rendered evidence; no further routine owner permission is required.

Stage A may precede the authenticated qualification producer in Stage B. It removes the duplicate policy and automatic room mutation while preserving current compatible Claude preview behavior. It does not complete R01/R02, activate v2 qualification, certify another route, or authorize installation, credential changes, commits or releases.

## Binding implementation conditions

| ID | Condition and required closure |
| --- | --- |
| A-01 | Preserve the refused resume handle and original authorizing identity in native fields when closing a late refusal as blocked. `_resume_info` may recover that handle only from an exactly validated native refusal. Do not synthesize a new identity from an unrun turn. Restoring the original eligible registry state must resume the same handle; leaving it ineligible must refuse again rather than start fresh. Preserve the bounded prior scope-change reason where that remains the cause. Missing/malformed refusal lineage must fail closed, never fall through to fresh dispatch. |
| A-02 | Use a distinct refusal exception and an explicit `dispatch_attempted` flag set immediately before Popen. A null process reference is insufficient proof. Only the typed pre-dispatch path may emit the refusal object. Popen failures and other exceptions retain existing error/unknown semantics; do not invent provider attempts. If cancellation races with a proved pre-dispatch refusal, preserve the cancel event while the refusal closes the turn. Test a Popen-raised exception after the flag is set and ensure no refusal object is emitted. |
| A-03 | Add only bounded `refusal_reason` to the public turn-finished projection, checked against the five exact codes. Store the full refusal under native `refusal`, alongside retained private lineage; neither it nor handles become public. Use an explicit bounded summary that resume was refused before dispatch. Existing event kinds remain unchanged. Test old/new-reader compatibility and invalid-code refusal. |
| A-04 | The enclosing result uses `error_kind=chat_resume_refused`. CLI returns 1 for both early and late refusals carrying that marker. Leave unrelated error/timeout/cancelled exit behavior unchanged and track the residual gap. Synchronous HTTP refusal is 409 with exactly status, error_kind, session_id, participant, refusal and redaction. Preserve parsed response body/status on browser errors so typed handling is possible. Test both CLI paths and the exact HTTP envelope. |
| A-05 | Track pending submitted drafts by session, participant and accepted turn ID. On a matching late-refusal finish, restore only into an empty composer in the same room; otherwise offer an explicit restore action. Clear only matching activity. The recovery panel shows the typed reason and explicit fork, hides Mark blocked for an already refusal-closed turn, and never creates a room automatically. Remove obsolete auto-fork wording and the dead successful-fork submit branch. Require source checks and a manual rendered fake-dispatcher journey, clearly distinguished. |
| A-06 | Preserve lifecycle assertions while switching the compatible continuation fixture to Claude and adding explicit Codex/AGY cases. Add shared silent-launch flags to all three owned Popen fixtures and the generated child; extend the Windows inventory to the touched test file. Keep fresh-dispatch and backend/transport refusal coverage explicit so the fixture change cannot hide a regression. |

## Lead integration decisions

- Preserve the exact refusal object from R02_REFUSAL_CONTRACT.md. Bounded identity-mismatch detail, if needed, belongs only in a safe enclosing field and never inside that object; omit it when the reason code suffices.
- At the second identity check after owner acquisition, release the owner before returning refusal and do not append a turn start. Once a turn is durably claimed, a late refusal must close it; that path is not a no-write evaluation.
- Remove the now-unreachable AGY resume-profile argument branch while retaining private historical profile values. Registry lookups remain patchable for deterministic submission-drift tests; no live revocation watcher or executable qualification is claimed.
- A repeated blocked refusal must not erase its original lineage, even when a malformed native refusal is encountered. Preserve the pre-dispatch proof boundary rather than treating arbitrary blocked history as resumable.
- No rollback bypass flag or new event kind is authorized. The denial boundary remains active. Non-refusal identity and finish semantics remain unchanged except the new explicit conditional branch.
- Lead/Fable own the technical migration decision and notice draft under current delegation. Publishing or releasing that notice remains a separate existing boundary; no renewed per-slice owner question is needed now.

## Evidence limits and next work

Fable inspected source and ran no tests. The lead's nine-file baseline snapshot and 73-test collection are preparation, not passing implementation evidence. Independent acceptance will run affected conversation/journal/UI, capability/governed continuation and Windows guard tests and execute the rendered fake-provider journey. Preserve original roster changes and P0 reliability fixes.

The broader qualification producer, account/executable/version evidence, authenticated private descriptors, actual expiry/revocation, strict producer/consumer migration and new native adapters remain Stage B or later requirements. Existing generic CLI error exit semantics remain a separately tracked reliability issue.

HANDOFF: DEV-R02-01 implements the approved Stage A contract and A-01 through A-06. Return the actual diff, reason/lineage behavior, scoped test evidence, rendered-check preparation, migration notice and remaining gates. Lead verifies independently and returns the completed milestone to Fable. No installation, credential changes, commit or release.
