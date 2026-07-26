# Known issues

No open issues. Resolved entries are kept below with the measurement that closed them, so
the record shows what was actually verified rather than only what was claimed.

## Windows: process-tree kill cannot reach descendants of an exited leader

**RESOLVED in 0.15.1** ([#10](https://github.com/Nafjan/summon/issues/10)).

Windows children are assigned to a kernel **Job Object** at spawn, so
`TerminateJobObject` kills every member regardless of which processes have already exited --
the same guarantee POSIX gets from a process group. Reproduced live before the fix
(`taskkill` reported *"process not found"* and the orphan survived) and verified after;
the scenario is a regression test, skipped off Windows.

The job is created `KILL_ON_JOB_CLOSE`, so summon dying unexpectedly also tears down the
backend tree. `--background` children are excluded by design -- they are meant to outlive
the launcher.

**Residual, by design:** stock `subprocess.Popen` discards the child's thread handle, so the
textbook `CREATE_SUSPENDED` -> assign -> `ResumeThread` sequence is unreachable from the
stdlib. Assignment happens immediately after spawn, leaving a microsecond window against a
child that spends milliseconds starting a runtime before it can spawn anything. Where Job
Objects are unavailable (nested-job restrictions, older Windows) the previous `taskkill`
path is unchanged and still covers the common leader-alive case.

