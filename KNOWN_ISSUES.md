# Known issues

## ACP transport: by-design limitations (phase 1)

**BY DESIGN** (documented, not defects; candidates for phase 2):

- The ACP session id is **not a resume handle** — `session/load` is unimplemented, so
  `resume.session_id` is deliberately `None` for ACP-served runs and the id lives under
  `acp.session_id`. (Letting it leak into the resume lane would re-dispatch the
  subprocess path with a wrong-namespace id on cursor-agent.)
- **No system-prompt channel** in ACP: the agent definition is prepended to the user
  prompt rather than passed via a dedicated flag (an envelope warning says so).
- **Model pinning is best-effort**: the stable ACP schema has no model on `session/new`;
  summon tries `session/set_model` where the session advertises a model list and warns
  otherwise. `usage`/`cost_usd` depend on what the backend emits over ACP and may be
  absent.
- **claude/codex have no native ACP** (adapter shims exist but are out of scope), and
  **agy has none at all** — the agy PTY-scrape class of failures is not covered by the
  ACP fallback.
- **The failure fallback re-executes the prompt** (default on): like `--retries`, a
  primary run that performed side effects before failing can perform them again over
  ACP. The predicate is narrow by failure class (timeouts, output-shape losses, pipe
  failures; never auth/CLI-missing/refusals), and the envelope records the attempt in
  `fallback`/`attempts` — use `--no-acp-fallback` for non-idempotent tasks.
- **ACP permission enforcement is reactive-only** (review finding, 2026-07-31): no
  permission flags travel to an ACP agent, so containment would depend on the agent
  choosing to send `session/request_permission` — unverified on real CLIs (gemini is
  frozen for individuals, cursor-agent untested, kimi is yolo-only everywhere). summon
  therefore refuses sub-yolo tiers over ACP; a tier can be re-opened per backend once
  reactive enforcement or a `session/set_mode` mapping is proven on a working account.

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

