# Windows silent-launch guidance for Summon callers

Summon can suppress the console for subprocesses that it owns. The outer caller
still owns the first process window: a batch file cannot retroactively hide a
console allocated by `cmd.exe`, an IDE, an MCP host, or a terminal launcher.
Provider sign-in pages and other explicitly requested browser or authentication
UI remain intentional exceptions.

## Python callers

Code inside the Summon checkout should use the shared helpers. They keep the
Windows policy in one place and are no-ops for console creation on POSIX:

```python
import subprocess
import sys

from skills.summon.scripts._spawn import popen_flags, run_flags

completed = subprocess.run(
    [sys.executable, "-c", "print('provider-inert')"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    **run_flags(),
)

worker = subprocess.Popen(
    [sys.executable, "-c", "print('worker')"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    **popen_flags(),
)
try:
    stdout, stderr = worker.communicate(timeout=30)
finally:
    if worker.poll() is None:
        worker.kill()
        worker.communicate(timeout=5)
```

Do not replace captured output with `DEVNULL` just to hide a window. Preserve
stdout, stderr, exit status, timeouts, and structured JSON. Detached/background
launches must use `popen_flags(detached=True)` and retain their own lifecycle
record.

## PowerShell and .NET callers

For a caller that needs captured output, use `ProcessStartInfo` rather than a
plain visible `Start-Process` window:

```powershell
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $env:ComSpec
[void]$psi.ArgumentList.Add('/d')
[void]$psi.ArgumentList.Add('/s')
[void]$psi.ArgumentList.Add('/c')
[void]$psi.ArgumentList.Add('call')
[void]$psi.ArgumentList.Add('C:\path\to\summon.cmd')
[void]$psi.ArgumentList.Add('usage')
[void]$psi.ArgumentList.Add('status')
[void]$psi.ArgumentList.Add('--cache')
[void]$psi.ArgumentList.Add('C:\Temp\summon-usage.json')
[void]$psi.ArgumentList.Add('--json')
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $psi
[void]$process.Start()
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
if (-not $process.WaitForExit(30000)) {
    try {
        $process.Kill($true)
    }
    finally {
        [void]$process.WaitForExit(5000)
        $process.Dispose()
    }
    throw "Summon caller timed out"
}
try {
    $process.WaitForExit() # drain both async streams after process exit
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) { throw $stderr }
    $report = $stdout | ConvertFrom-Json
}
finally {
    $process.Dispose()
}
```

This recipe uses `cmd.exe` because `.cmd` files are batch scripts, not executable
images for `UseShellExecute = $false`. `ArgumentList` is available in modern
.NET/PowerShell. The block was exercised with PowerShell 7.6.5 on Windows;
callers on older Windows PowerShell should construct an
equivalently quoted `.Arguments` string and keep the same hidden, redirected,
UTF-8, asynchronous-drain, timeout, and cleanup settings. `Start-Process
-WindowStyle Hidden` is useful when no capture is required, but
`ProcessStartInfo` is the safer captured-output recipe. A host that launches
`cmd.exe` visibly can still flash before Summon starts; the host must apply its
own hidden/no-console setting.

## MCP and IDE hosts

The MCP or IDE host owns the process it starts. Configure that host's child
creation policy when it supports hidden windows, and pass Summon structured
arguments rather than interpolating multiline prompts into a batch command.
Use `--prompt-file` for multiline or metacharacter-rich prompts. Summon cannot
guarantee that an arbitrary vendor CLI, browser, updater, or descendant process
will never create its own UI after launch.

The silent-launch contract is therefore scoped to processes Summon or its
documented caller directly owns. It does not claim to inspect unrelated windows
or to suppress intentional authentication/browser UI.
