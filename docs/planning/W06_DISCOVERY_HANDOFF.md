# W06 discovery test launcher handoff

Date: 2026-09-05. Scope: remaining discovery test launches and regression guards. This is a source change, not installation, release, or proof that every workstation popup is resolved.

## Changes

- `skills/summon/scripts/test_discovery.py`: audited 112 executable direct subprocess calls. Added shared flags to 98 previously unguarded calls; 14 already used the shared helpers. Arguments and named keyword arguments remain structurally identical. The timeout fixture now gets its existing POSIX session behavior through `popen_flags()`.
- Three generated lifecycle fixture payloads now use shared `run_flags()` for their descendants. Two previously omitted hidden flags; one copied a Windows flag locally. `run_flags()` preserves descendant membership in the leader's POSIX process group, so cleanup behavior is retained.
- `tests/test_windows_silent_launch.py`: extended the inventory to discovery and the eight additional modules already corrected by the developer. Added generated-payload coverage for all three lifecycle fixtures and an imported subprocess alias negative fixture. Existing quoted git-mutation fixtures remain unchanged; no broad mock exclusion is required.

## Verification

- Initial bounded gate: 68 passed, one failed, zero skips or errors, in 203.576 seconds. It selected all 52 discovery functions with direct launches, four existing structural/platform/mutation regressions, and the 13-test Windows module. The failure was a new fixture source-directory expression referencing an undefined variable before its process launch; that expression was corrected. A guard-only negative fixture was added while the initial run was active, so this run is not represented as a frozen final-tree gate.
- Final focused gate: 16 passed in 10.044 seconds, zero failures, errors or skips, with no source drift. It covers all 13 Windows silent-launch tests and all three modified descendant lifecycle tests, including the corrected failure. Do not add overlapping test counts together.
- AST comparison confirms all 112 direct launch sites preserve their arguments and named keyword arguments. The quoted mutation-guard function is structurally unchanged. The source inventory now requires shared flags at all direct launch sites; generated descendants have separate payload checks.

Raw logs and test fixtures remain private. The runs use disabled telemetry, disabled plugin autoload, no bytecode/cache writes, and the shared hidden-launch flags on the test runner itself. No live provider review or provider invocation was performed by this delegation. The full discovery suite was not run; testing stayed within the audited affected functions.

## Scope and limitations

Fable's separately verified consultation approved this bounded plan, not this patch. The product lead deferred further workstation traces to restore focus on the workspace roadmap. An external scheduled action remains a contributor candidate; timing does not prove which process allocated every visible Terminal window. No scheduler, external watchdog, global terminal setting, credential, or provider configuration was changed.

Potential follow-up documentation wording: hidden launch flags cover Summon-owned process creation. A vendor may explicitly create a console or GUI window later, and an outer host may allocate a console before Summon starts. Avoid treating either possibility as a proven cause or automatically blaming the caller.

HANDOFF: independently verify the bounded patch and targeted test results, then resume the main workspace implementation. Retain the end-to-end Windows issue as open until its executing caller and descendants are demonstrated. No additional passive trace or external bootstrap is needed for this handoff.
