# Windows silent-launch implementation review

Updated: 2026-09-05. DEV-WIN-01: APPROVE from independent lead verification and final FABLE-WIN-02 milestone review. The release-tool runtime diff adds the approved shared loader and seven forwarded helper calls. Comparing against the original selected source snapshot confirms those hunks preserve pre-existing roster work. All seven findings below are closed. Milestone acceptance is separate from release qualification; FABLE_WINDOWS_APPROVAL.md carries the six non-blocking follow-ups.

| ID | Finding | Required closure |
| --- | --- | --- |
| WIN-RV1 | Initial PowerShell example directly starts a batch file with shell execution disabled and drains two redirected pipes sequentially | Execute a provider-inert recipe using an executable entry point, concurrent stream draining, explicit UTF-8 and bounded cleanup. Validate the documented recipe itself. |
| WIN-RV2 | Test labelled worker exercises utility flags only | Separate actual utility and normal-worker launch probes; retain detached probe and clean it up on failure. |
| WIN-RV3 | Actual batch dry-run checks output but does not observe the owned child's console | Add the approved stand-in batch hop with self-only console snapshot, preserved UTF-8/newlines and nonzero exit status. |
| WIN-RV4 | Forwarding test records but does not assert original argv and environment | Assert command and environment semantics at all seven source call sites alongside capture, errors, timeout, check and shared flags. |
| WIN-RV5 | AST guard lacks negative fixtures and aliased os.system detection | Exercise unsafe aliases, malformed source, shell launches and approved helper forms with synthetic snippets; retain the existing runtime guard. |
| WIN-RV6 | New module initially had only an ad hoc execution path | Include it in normal CI and the canonical provider-inert release partition without altering existing roster entries. |
| WIN-RV7 | Independent run: 106 passed, 1 failed, with no source drift. Cold Windows platform detection reached the globally mocked subprocess runner, adding a `ver` call to the forwarding fixture | Isolate platform metadata in the forwarding fixture explicitly. Do not warm a cache or accept an unexpected extra call. Inspect the indirect launch separately before alleging a runtime popup defect. |

These are routine corrections within delegated implementation authority. They do not require renewed owner permission. Final Fable milestone review will receive their disposition and independently checked evidence.

The lead inspected the local CPython fallback: platform detection can use `check_output(shell=True)` after WMI fails. Its Windows subprocess implementation applies hidden startup flags to shell launches. The observed failure establishes test-order dependence from the broad mock, not a demonstrated visible-window regression. This indirect standard-library boundary is distinct from the seven explicit tool sites; preserve that distinction in final claims.

## Candidate closure evidence

- WIN-RV1: the documented PowerShell recipe now invokes batch through `cmd.exe`, drains stdout and stderr asynchronously, uses explicit UTF-8, applies a 30-second timeout, waits after kill, and disposes in `finally`. The exact recipe was executed provider-inert with PowerShell 7.6.5.
- WIN-RV2: utility `run_flags`, normal-worker `popen_flags`, and detached `popen_flags(detached=True)` are separate probes; detached cleanup is bounded on failure.
- WIN-RV3: a stand-in `.cmd` hop observes only its owned child and preserves UTF-8 output, stderr, and exit code 7.
- WIN-RV4: forwarding tests assert original argv, environment, capture/text/encoding/errors/timeout/check semantics, and the shared no-console kwargs at all seven sites. Hermetic runner telemetry/plugin invariants are asserted.
- WIN-RV5: the AST guard fails on syntax errors, subprocess aliases without shared flags, `shell=True`, direct/aliased `os.system`, and scans every `tools/*.py`, `test_job_control.py`, and the new Windows module.
- WIN-RV6: `tests/test_windows_silent_launch.py` is registered in both the normal CI Phase 0/1 partition and the canonical `phase0_phase1` release command.
- WIN-RV7: the forwarding test isolates `platform.platform()` so the call ledger covers exactly the seven owned release-tool sites. On Windows, CPython's `_syscmd_ver` uses `shell=True` with hidden startup information; this is a standard-library metadata boundary, not a new direct Summon launch site. No runtime replacement was made, and no blanket silent-descendant claim is permitted.

Developer-reported candidate tests: Windows module 12 passed; release-tool modules 32 passed; existing spawn guards 3 passed; controlled readiness/liveness partition 8 passed. The first independent run failed as recorded in WIN-RV7; all 60 job-control tests passed in that run. No provider runtime was activated and no authentication, installation, credential, commit or release action was performed.

## Final independent evidence

- Corrected candidate: 47 passed in 55.04 seconds, no skips or exclusions: Windows module 12, release-tool tests 32, existing spawn guards 3. All bound source/test/caller-document files remained unchanged during execution.
- The 60 unchanged job-control tests passed in the earlier independent run; the full P0 gate independently passed 189 tests. Those results are retained without rerunning unchanged tests solely to create a combined count.
- Lead AST comparison confirms both release tools equal the original snapshot after removing only the approved loader, flag forwarding and test-registration additions. Original pending roster hunks remain intact.
- Twenty synthetic positive/negative subprocess-alias guard cases passed without executing the snippets. Three loader checks confirmed unchanged sys.path, checkout-relative helper selection and fail-closed missing helper behavior.
- The actual Markdown PowerShell example passed with only synthetic wrapper/cache substitutions: PowerShell 7.6.5, successful exit, missing synthetic cache and no provider contact. Earlier owned direct-child and batch probes preserved input, output, error and nonzero exit status with no console at the observation point.
- The intercepted CPython shell-launch probe was stopped before process creation and confirmed hidden startup flags. It did not set CREATE_NO_WINDOW; retain that narrower standard-library boundary. No native Linux run or full-lifetime/vendor-wide visual guarantee is claimed.

The .NET executable requirement is documented in [ProcessStartInfo.UseShellExecute](https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.processstartinfo.useshellexecute). Microsoft also describes redirected-stream deadlock risks in [Process.StandardOutput](https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.process.standardoutput).

Remaining boundary: a child self-observation is not a full-lifetime recording of every possible window. An external host owns any console allocated before Summon begins; vendor-created browser/authentication UI remains separately controlled. No installation or release claim follows from this patch.
