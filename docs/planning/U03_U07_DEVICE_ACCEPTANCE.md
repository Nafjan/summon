# Remaining device acceptance for U03/U07

Prepared 2026-09-12; all observations below are still NOT RUN. This executes the
existing WORKSPACE_CHECKLIST_V3.md requirements. It does not add a new release
feature, waive a gate, or claim complete WCAG conformance.

## Test setup and ownership

The conductor prepares an ordinary `WorkspaceHost` with synthetic tasks,
context, canonical details and the existing bounded fault fixtures. Reuse
`test_workspace_canonical_rendered.py`, `test_workspace_recovery_rendered.py`
and their actual-host seed helpers. Do not replace status/authority handlers
with a standalone mock page. Keep provider/worker execution disabled.

The operator supplies the native browser/screen-reader or mobile-keyboard
interaction. Device availability has been requested; it is not yet established.
An installed Android system image alone is not a prepared test device. A mobile
connection must retain the host's loopback/origin/authentication boundary;
do not expose the workspace to the LAN or relax its host checks to make a phone
connect. Any forwarding must be explicitly confined to an owned test transport.

Use a fresh synthetic session. Record browser, operating-system, screen-reader
and keyboard versions, input mode, orientation and whether the device is physical
or an emulator. Retain the candidate/source binding privately. Record no account
identity, private path, bootstrap value or personal screen/audio content. Do not
capture authentication speech or images; post-login synthetic observations are
sufficient. The conductor closes owned listeners and test processes afterward.

The private fixture now includes the actual operator-message lost-response
boundary from `test_workspace_operator_runtime.py`. One guarded runtime smoke
passed in 1.668 seconds: post-append session-constraint loss produced uncertainty,
then an exact lookup found one queued message without another journal write.
AT05/MK04 use that context-message journey, including encrypted retention and
explicit same-operation recovery; cancellation/disposition coverage is separate.
The smoke did not execute HTTP, browser encryption, speech or a native keyboard.
Prepared fixture behavior and actual device observations remain distinct.

The ordinary-host/HTTP/desktop-browser composition subsequently passed in
36.900 seconds. After a real append and session expiry, reload/reauthentication
retained identical encrypted storage and restored the exact body/key/recipient.
One explicit lookup confirmed the original queued message without a second
send or journal write. Full headless Chromium, owned cleanup, unchanged source
and the exact executed script are retained privately. The first harness failure
remains preserved and unclassified; its correction changed only bounded waits
and safe diagnostics, without relaxing CSP or mutating application/storage state.
This prepares AT05/MK04; all actual speech and native keyboard observations below
remain NOT RUN.

## Screen-reader journey

| Case | Operator action and host stimulus | Required observation |
| --- | --- | --- |
| AT01 | Navigate the authenticated workspace with the screen reader and keyboard; select Task 1. | Goal, selected task, recipient selector and Context field have understandable names/states. Unknown owner/model evidence is announced as unknown, not as confirmed service. |
| AT02 | Open Reviews and decisions, inspect the seeded assessment and separately authorized artifact, then close the drawer. | Dialog context and available controls are understandable; keyboard navigation stays in the drawer. Closing returns to the opener. An unavailable body does not appear as an authorized action. |
| AT03 | Read an older journal item while the host advances a synthetic snapshot. Leave focus on the item. | The new-snapshot status is discoverable through the screen reader without taking focus or moving the reading anchor. Show latest remains reachable. |
| AT04 | Use the accepted bounded view-failure/recovery fixture while continuing to read. Include repeated identical retry status. | The first relevant status is available; unchanged repeats do not cause repeated interruptions. Recovery and changed errors remain discoverable. Record what was actually heard and the reader's live-region settings; do not infer speech from DOM mutations. |
| AT05 | Retain an unresolved synthetic context operation, expire the browser session through the fixture, then reauthenticate and inspect the same operation. | Authentication/recovery state is understandable. The pending operation remains unresolved until its explicit permitted recovery; no duplicate send or false success announcement occurs. |

Status changes should be exposed to assistive technology without requiring focus
on the message; this checklist observes the actual reader behavior as well as
the implementation. Speech wording may vary by reader. See W3C's explanatory
[Status Messages guidance](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html).

## Native mobile-keyboard journey

| Case | Operator action and host stimulus | Required observation |
| --- | --- | --- |
| MK01 | Select a recipient and focus Context with the native soft keyboard. Type short, multiline synthetic text. | The caret and editable area remain usable. Recipient and Queue context can be reached through ordinary native scrolling. Typing does not change recipient, move focus or submit automatically. |
| MK02 | Keep the keyboard open while the host publishes a pending snapshot and bounded reconnection notice. | Notices do not cover the editing position, discard the draft or pull the reading position to the newest event. Required actions remain reachable. |
| MK03 | Dismiss and reopen the keyboard; repeat in the supported alternate orientation. | Draft, recipient and pending-operation identity persist. No horizontal page scroll is needed for normal prose or controls. Record actual visual viewport and native keyboard behavior, not a desktop viewport approximation. |
| MK04 | With a synthetic operation unresolved, exercise the same expiry/reauthentication recovery as AT05, including keyboard dismissal. | Recovery controls remain usable, the encrypted pending operation is preserved, and the UI requires the same explicit action as the desktop journey. |

Native keyboard behavior is an existing Summon product requirement. It is not
proved by the WCAG minimum against author-created content obscuring focus; see
[Focus Not Obscured guidance](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html).
The separately accepted desktop zoom/reflow evidence remains in
U07_NATIVE_ZOOM_ACCEPTANCE.md. W3C also distinguishes device presentation limits
from a page's reflow behavior in its
[Reflow guidance](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html).

## Acceptance record

For each case retain PASS, FAIL or NOT RUN, the actual stimulus, observed outcome,
toolchain/input mode and any specific defect. A human observation can substantiate
speech or native input; a markup assertion, package inventory or model opinion
cannot stand in for it. Only the tested combination is qualified. Failures return
to the owning UI lane with a reproducible trigger; unchanged accepted desktop
tests need not be repeated unless a resulting fix affects them.

This checklist and a green automated suite alone do not close U03/U07. The lead
reviews the completed observations against the candidate, and the conductor
retains them through the existing final-evidence process. Final source, privacy,
platform and publication gates remain separate.
