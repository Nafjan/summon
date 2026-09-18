# Recurring Windows console follow-up

Date: 2026-09-05. Status: OPEN. The owner reports frequent console/terminal/PowerShell flashes while many parallel coding tasks are active. Do not attribute all windows to Summon or claim workstation resolution from the previously accepted bounded 47-test source milestone.

## Evidence

- A passive 75-second trace recorded three Windows Terminal hosting windows marked visible by Win32, lasting approximately 1.4, 6.6 and 13.9 seconds. It also recorded three pseudoconsole handles. Pseudoconsole visibility flags alone do not establish a painted user-visible window. A second bounded trace adds window dimensions and cloaking state.
- The trace collects window class, lifetime and process ancestry only. It does not collect window titles, command lines, prompts, account data or screen contents. Raw identifiers remain private; public records contain aggregate findings only.
- The developer's read-only audit confirms ordinary production run/Popen sites use shared flags and the production guard covers non-test scripts. That is source evidence, not proof about every executing copy, outer host or vendor descendant.
- Concrete unflagged test launches remain, including taskkill calls in test_conversation_ui.py and child commands in test_conversation.py, test_agy_1_1_22.py and several deliberation/portable test modules. The lead's own conversation regression runs included affected helpers. These are actionable gaps regardless of the other parallel tasks.
- The legacy agy_pty_pyte.py path and externally allocated caller consoles remain separate possible causes. Current source selects the hidden stream proxy unless the legacy opt-in is explicit. No executing-copy or opt-in cause is established by the current trace.

## Refined attribution

A second 120-second passive trace observed two non-cloaked Windows Terminal hosting windows with nonzero dimensions, approximately one minute apart. One persisted for about 2.8 seconds; the other remained visible when observation ended. Associated zero-size pseudoconsole handles belonged to PowerShell processes whose retained ancestry reached the Windows Task Scheduler service. Terminal activation obscured direct parentage, so timing and ancestry do not conclusively identify every visible window's launcher.

A read-only scheduler inventory found one recent, once-per-minute interactive PowerShell action matching that cadence. It runs an external watchdog, outside this repository, and already includes hidden-window and noninteractive arguments. A narrow script inspection found no direct Start-Process command or ProcessStartInfo construction. This is a strong external contributor candidate, not proof that Summon is uninvolved or that every flash has the same cause. No task, watchdog script, credential, service or global terminal setting was changed. Workstation-specific names and raw observations remain private.

Next attribution gate: bind a naturally occurring scheduled process to its known action without retaining command-line contents, then correlate its window lifecycle. A proposed external launcher correction must preserve the watchdog's trigger, identity, arguments, working directory, exit status and overlap behavior; do not disable the watchdog to make the symptom disappear.

## Current work

The previously deferred W06 test-harness ratchet is now urgent. First correct real child launches in the modules used for independent verification, then inventory remaining test modules. Use shared run_flags/popen_flags without changing arguments, captures, timeouts, exit or cleanup semantics; preserve intentional mock-only negative tests. Extend guards to corrected files. Keep this delta distinguishable from R02 logic and roster changes.

Broad test runs are paused until their real launch sites are audited. The trace remains passive; no deliberately visible positive-control window is requested. R02's narrow generation correction follows hidden-launch parity. Other hosts' windows require attribution before any additional action; no global terminal setting, installation, credential change or release is implied.

HANDOFF: correlate non-cloaked console windows and retained process ancestry, verify the test-harness delta, and report the scoped outcome honestly. Do not turn the accepted bounded source milestone into a claim that every machine flash is fixed.

## Focus correction and delegated closure

The owner explicitly requested delegation and a return to the main workspace goal. A dedicated Windows subagent now owns the remaining actual discovery-test subprocess sites and structural guard. The main developer returned to R02/R13. The lead independently verified the current Windows launch suite: 12 passed, no failures, skips or source drift; this is bounded source evidence only.

FABLE-WIN-03 completed through Summon with authoritative reported matching served identity. It approved the scoped plan, verified the shared flag policy and kept external attribution uncertain. The lead deferred its suggestion of further multi-cycle tracing because the existing evidence already supports separating the external candidate from actionable Summon fixes. External action changes are not part of this delegation. A possible future windowless bootstrap would require preserved execution and cancellation ownership, explicit standard handles, exit propagation, isolated stub/forced-stop verification and a separately authorized reversible deployment.

HANDOFF amendment: finish and verify the bounded Summon test-launch delta, report remaining external uncertainty, then stop this subtask. Do not let the popup investigation gate unrelated provider-inert workspace design or become an open-ended workstation audit.
