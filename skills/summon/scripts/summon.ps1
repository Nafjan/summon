# PowerShell-safe dispatcher entry point.
# Invoke run_subagent.py directly so PowerShell @args splatting preserves
# multi-line --prompt strings without cmd.exe / SUMMON_CMD_LAUNCHER barriers.
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) {
    & py -3 "$PSScriptRoot\run_subagent.py" @args
} else {
    & python "$PSScriptRoot\run_subagent.py" @args
}
exit $LASTEXITCODE
