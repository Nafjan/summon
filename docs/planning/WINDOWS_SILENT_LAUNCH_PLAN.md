# Windows silent launch correction

Status: DEV-WIN-01 implemented and provider-inert tested, 2026-09-05. Fable approved items 1-5. Owner report: some Summon calls briefly open a command, terminal or PowerShell window; all calls should be silent whenever possible. This remains a scoped launch-boundary fix, not proof that arbitrary vendor descendants or host-created windows are silent.

## Verified source findings

- Direct production subprocess calls in `skills/summon/scripts` use the shared `_spawn.popen_flags` or `_spawn.run_flags`. These combine Windows no-console creation with hidden startup information, while keeping detached workers separate. This inventory does not prove every external wrapper or vendor descendant is quiet.
- The two inspected managed copies of `_spawn.py` match the current helper. This is a focused helper comparison, not whole-install convergence evidence.
- The DEV-WIN-01 readiness fixture now uses the shared flags while preserving pipes, readiness, timeout, and cleanup behavior.
- All six `tools/release_gates.py` utility/test calls and the one `tools/release_manifest.py` Git call now use the shared helper. The release-tool tests assert the forwarded kwargs and preserve the original command, environment, capture, encoding, timeout, and check settings.
- Release evidence also records `platform.platform()`. On a cold Windows cache, CPython may perform its own `ver` metadata probe internally; CPython supplies hidden startup information for that shell call. This remains a standard-library boundary, not a claim that Summon controls arbitrary metadata or vendor descendants.
- An outer caller that invokes `summon.cmd` without hidden process creation can create a console before Summon executes. The batch file cannot retroactively prevent its own allocation. The lead's private review broker previously omitted those outer flags; all subsequent broker subprocess calls must use the shared hidden flags.

These are source-supported routes to a flash, not visual attribution of every window the owner observed. Do not claim the user's field issue is fully resolved without scoped Windows evidence.

## Independent owned-process probe

The lead ran two provider-inert Windows probes using the shared hidden flags: a direct owned Python child and an outer batch-wrapper chain to the same child. Both reported no console handle and no visible console at the child's observation point. Standard input, standard error and the deliberately nonzero exit status were preserved; standard output remained parseable structured data. No unhidden positive-control process was launched and no unrelated application windows were inspected. This supports the hidden caller recipe, not a claim that all possible vendor windows were observed throughout their lifetimes.

Windows documents that `CREATE_NO_WINDOW` suppresses a console application's console, but is ignored when combined with detached/new-console flags or used for non-console applications. Preserve the existing helper's separate worker and detached modes. [Microsoft process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)

`IsWindowVisible` checks a window's visibility style; an instantaneous result is not a complete record of earlier flashes. Keep the probe's claim limited accordingly. [Microsoft visibility API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-iswindowvisible)

## DEV-WIN-01 implementation

1. Done: shared hidden flags were added to the real subprocess fixture in `test_job_control.py`, preserving pipes, timeouts, readiness, and process ownership semantics.
2. Done: all seven subprocess sites in the two release tools use the shared helper. Existing release-manifest roster edits were preserved.
3. Done: `tests/test_windows_silent_launch.py` covers utility, worker, detached, release-tool forwarding, and provider-inert `summon.cmd` prompt-file/usage paths. The owned-process probe preserves UTF-8 stdout, stderr, and exit code 7 while asserting only the child’s own console state.
4. Done: the AST guard scans every `tools/*.py`, the new Windows test module, and `test_job_control.py`; syntax errors, aliases, missing shared flags, `shell=True`, and `os.system` fail the test.
5. Done: caller guidance is in `WINDOWS_SILENT_LAUNCH_CALLERS.md`, including Python, PowerShell/.NET, batch, and MCP/IDE ownership boundaries.

Requested visible browser/auth/UI interactions remain explicit exceptions. No auth repair, provider smoke test, account switch, installation, commit or release is part of this correction. Do not modify unrelated private smoke scripts or inspect credentials, prompts, receipt bodies or telemetry.

## Acceptance

- Windows worker, detached worker and utility kwargs use the shared helper and retain current POSIX process-group behavior.
- Actual release-tool calls carry suppression flags; fixed arguments and release gates remain unchanged.
- Real readiness/timeout tests remain passing with silent launches and verify child reaping.
- Provider-inert Windows outer-launch fixture preserves output, error status and argument boundaries and reports only aggregate visibility evidence for its owned process.
- Test and public-document evidence excludes private paths, titles, accounts, prompts, handles and raw receipts.
- Existing P0 reliability and R01 changes remain reviewable and preserved. No blanket statement that arbitrary third-party descendants or host-created windows can be suppressed by Summon after launch.

## Provider-inert verification

- `tests/test_windows_silent_launch.py`: 12 passed, including the executable PowerShell 7.6.5 `ProcessStartInfo` recipe.
- `tests/test_release_gates.py tests/test_release_manifest.py`: 32 passed.
- Existing runtime spawn guards: 3 passed.
- Controlled P0 readiness/liveness partition: 8 passed.
- The existing P0 four-suite gate remains 189 passed after the fixture correction.
- No provider, authentication flow, installation, credential, runtime, or release action was performed.

The implementation team is the existing developer task, with independent lead verification and Fable approval. This urgent Windows correction precedes R02 runtime activation and does not replace the broader workspace roadmap.
