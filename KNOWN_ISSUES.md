# Known issues

## Windows: process-tree kill cannot reach descendants of an exited leader

Tracked in [#10](https://github.com/Nafjan/summon/issues/10).

`--overall-timeout` and council teardown kill in-flight children with `_kill_tree`
(`skills/summon/scripts/_executor.py`). On POSIX, `killpg(pgid, SIGKILL)` reaches the whole
process group even after the leader exits. On **Windows**, `taskkill /F /T /PID <leader>` walks
parent -> child PID links, so once the `run_subagent.py` leader has exited, a still-running
backend grandchild (one holding stdout) is orphaned and survives the kill.

- **Scope:** narrow. It requires the `run_subagent.py` leader to exit early while a detached
  backend outlives it and holds stdout. The common path (leader alive, so `taskkill /T` walks
  the live tree) works.
- **POSIX:** not affected (`killpg` targets the group via the PGID regardless of leader
  lifetime).
- **Consequence:** in that tail case a paid/fs-capable backend can keep running after the
  council envelope reports the deliberation ended.
- **Fix (planned):** Windows Job Objects (`CreateJobObjectW` + `AssignProcessToJobObject` +
  `TerminateJobObject`), a kernel handle that owns the whole tree independent of the leader's
  lifetime. Stdlib-only via `ctypes`; changes the spawn path for all dispatches, so it is its
  own focused pass. See #10.

`--overall-timeout` shipped POSIX-correct on this understanding (the queued-wave, monotonic
deadline gate, deregister-on-clean-EOF, and bounded teardown machinery are sound on both
platforms; only the Windows *reachability* of an exited leader's descendants is open).
