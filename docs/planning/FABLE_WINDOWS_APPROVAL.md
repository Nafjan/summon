# Fable Windows silent-launch approval

Date: 2026-09-05. Verdict: APPROVE for bounded implementation. The lead verified successful execution and authoritative reported served identity for `claude-fable-5-1`. This approves the plan, not an untested patch or a claim that every external host or vendor window is fixed.

The owner requested silent Windows calls wherever possible. `WINDOWS_SILENT_LAUNCH_PLAN.md` identifies verified gaps in the real timeout-test fixture, seven release-tool subprocess calls and outer caller behavior. The runtime's existing shared flags remain the single source of platform behavior.

## Binding conditions

| ID | Required closure |
| --- | --- |
| W-01 | The real readiness fixture uses `popen_flags`; readiness, timeout and reaping assertions remain intact. Preserve POSIX process ownership. |
| W-02 | Add shared flags to all seven release-tool calls without changing command, environment, capture, timeout or result semantics. A single loader in `release_manifest.py` loads `_spawn.py` relative to the tool checkout using `importlib.util`; `release_gates.py` reuses that manifest module's helper. No duplicated Windows constants or sys.path mutation. Missing helper fails closed. Preserve all pre-existing roster hunks. Tests verify actual kwargs at every call site and existing module loading. |
| W-03 | Add an AST guard over owned tools, the new test module and the readiness-test module. Syntax errors fail. Detect attribute and from-import subprocess forms for run/Popen/check_output/check_call/call, including aliases; recognize shared helper forwarding; flag `shell=True` and `os.system`. Keep the existing runtime guard. |
| W-04 | Provider-inert Windows tests cover utility, worker, detached and outer batch modes with real flags. Children inspect only their own console handle/visibility; detached mode has no console. Preserve stdout/stderr, nonzero exit status, UTF-8 and newlines. Test a stand-in batch hop and the real provider-inert prompt-file launcher. Skip with an explicit reason off Windows or when the required Python launcher is absent. No intentionally visible positive control or unrelated window enumeration. |
| W-05 | Dedicated caller guidance includes Python and captured-output PowerShell/.NET recipes, MCP host responsibility, intentional browser/auth UI, and the inability to retroactively suppress an externally allocated console. Do not discard output or promise arbitrary vendor descendants are hidden. |
| W-06 | Record a follow-up ratchet for other outer test launches identified by Fable. This is deferred, not required to approve the bounded patch. |

Lead clarification for W-04: a consoleless parent and child can both return a null handle. The test must reject inheritance of a non-null parent console, not fail the valid both-null case. `CREATE_NO_WINDOW` modes may assert a null child handle directly. This preserves the intended no-inherited-console guarantee and follows the independently observed headless case.

## Evidence and boundaries

Fable inspected the relevant runtime, tools, fixtures and CI configuration but ran no tests. Its approval requires final Windows test output and the actual diff. Native Linux/POSIX results must be reported only if executed; testing a platform branch on Windows is not a Linux run.

The lead's earlier direct-child and outer-batch probes preserved structured output, input, stderr and exit status with no console reported at the child's observation point. They are scoped process evidence, not a full-lifetime recording of all flashes. Raw review artifacts and harness notes stay private.

The developer implements W-01 through W-05. The lead verifies the new tests, both release-tool test modules, focused runtime spawn guards and readiness tests, then returns milestone evidence to Fable. Existing roster edits, R01 and the P0 reliability slice remain separately reviewable. No installation, credential change, provider probe, commit or release is authorized by this record.

## Final milestone acceptance: FABLE-WIN-02

Completed 2026-09-05. STATUS: DONE. VERDICT: APPROVE for DEV-WIN-01 and the bounded P0 test/contract slice. The lead verified successful review execution and authoritative reported served identity for `claude-fable-5-1`. Fable found no new blocking issues after inspecting the final source and lead evidence. This is milestone acceptance, not release or installation approval.

All W-01 through W-05 and corrected P0 F-01 through F-09 conditions are closed for this slice. Independent lead evidence is recorded in WINDOWS_IMPLEMENTATION_REVIEW.md and P0_IMPLEMENTATION_REVIEW.md. The failed cold-cache forwarding run remains recorded alongside the corrected 47-test pass; it is not excluded from the history. R01/R02 as whole roadmap requirements remain open.

| Follow-up | Non-blocking limitation and disposition |
| --- | --- |
| L-01 | The AST guard recognizes lexical helper names; it does not prove import provenance or cover every possible os/asyncio launch API. Extend the future W-06 guard ratchet. |
| L-02 | CPython platform metadata may use hidden shell startup without CREATE_NO_WINDOW. No popup was demonstrated; consider shell-free Windows metadata in a separate small change. |
| L-03 | Owned console probes are point-in-time observations. External hosts own their initial console; arbitrary vendor descendants remain outside this guarantee. |
| L-04 | Native Linux execution is pending. The configured Ubuntu/Windows CI matrix must produce fresh evidence before release qualification; local simulated POSIX checks do not replace it. |
| L-05 | Corrected passing fixtures do not conclusively diagnose every historical field incident. Retain honest incident classifications. |
| L-06 | Explicit operation/attempt scope for provider-contact facts remains a P1 design requirement; refused-invocation facts cannot describe another winner's claim. |

HANDOFF: preserve the independently tested patch and pending roster hunks. Continue DEV-R02-SPEC-01 as a read-only proposal before behavioral migration. Future changes to the accepted implementation require relevant regression verification and Fable review through delegated leadership. Keep installation, credential, commit and release boundaries separate; carry W-06 and L-01 through L-06 into their later work items.
