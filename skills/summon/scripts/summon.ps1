# PowerShell-safe dispatcher entry point, mirroring summon.cmd.
# summon.cmd relies on cmd.exe batch semantics that behave unpredictably when
# invoked from pwsh background tasks (field report 2026-09-19: stdout/stderr
# piping to the task log hung or dropped). This launcher resolves the Python
# entry point directly and preserves stream piping.
# As with summon.cmd: use --prompt-file for every dispatch prompt.
$env:SUMMON_CMD_LAUNCHER = "1"
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Summon requires Python 3.10 or later. Install a current Python and retry."
    exit 1
}
& py -3 "$PSScriptRoot\run_subagent.py" @args
exit $LASTEXITCODE
