# Fan-out (swarms) & council mode

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Fan-out (swarms)

**Native path — `--manifest`** (built from a real 80-run workload's orchestrator):

```bash
run_subagent.py --manifest jobs.json --concurrency agy=2,codex=3 --results-dir out/ --cwd <abs>
```

```json
{
  "defaults": {"timeout": "600s", "retries": 1},
  "jobs": [
    {"id": "rev-07", "agent": "reviewer", "prompt_file": "packets/07.md"},
    {"id": "rev-08", "agent": "reviewer", "prompt_file": "packets/08.md", "model": "gpt-5.6-sol"}
  ]
}
```

What you get: per-backend concurrency semaphores, one atomic envelope per job in
`--results-dir/<id>.json`, **skip-if-done resume** (re-running a swarm skips only
jobs whose prior envelope was `success`, and **re-dispatches** any that ended
`error`/`blocked`/`partial` — so a re-run retries the failures; delete a result
file to force a clean re-run), per-job retries, progress lines on stderr,
and a single summary JSON on stdout (`total/succeeded/failed/skipped/suspect`).
Job keys: `id, agent, prompt|prompt_file, cwd, cli, model, effort, timeout,
retries, json_schema, debug_dir` (defaults apply to all, per-job overrides win).
Relative `prompt_file`, `json_schema`, and `debug_dir` paths resolve against the
**manifest file's directory** (so the examples above just work). The manifest uses
the same agent discovery as a direct dispatch — you don't need `--agents-dir` if
your roster is where summon normally finds it. Every job always writes an envelope
to `<results-dir>/<id>.json`, even on a validation/spawn failure, so a failed job
is never zero-forensics.

Rules that still apply (manifest or manual):

1. **Payload in files, not prompts.** Write packets under `--cwd` and use
   `prompt_file` (or a short "Read X and follow it" prompt). Hard numbers: agy
   rejects prompts over ~28,000 chars (Windows argv limit); other CLIs degrade
   before they fail. Files under cwd also avoid `blocked` reads.
2. **Throttle per backend, not globally.** Safe starting points: 3-4 concurrent
   claude/codex/cursor; 2 concurrent agy (each run gets its own isolated
   profile, so concurrency is safe — the cap is for machine load, not
   correctness). Use `elapsed_ms` from completed envelopes to tune.
3. **Editing swarms get `--worktree`** (one per agent — manual dispatch, since
   manifest jobs share the cwd). Read-only swarms don't need it.
4. **Judge with the envelope**: branch on `status` and `report.status`, treat
   `blocked` and `suspect: true` as re-dispatch signals, use `--json-schema`
   for machine-readable verdicts, and sum `usage`/`cost_usd` for the bill.

**Manual path** (when you need per-job worktrees or custom scheduling): dispatch
each job with `--background`, which returns `{job_id, pid, result_file, job_dir,
record_file}` at once; completion = its `result_file` exists (atomically written =
complete). (`--background` and `--out` are separate mechanisms and can't be combined;
use `--manifest` when you want per-job result files at chosen paths.)

### The background job registry

`--background` writes a durable launch record (fsynced) BEFORE the child spawns, so a
job that dies before its result is never zero-forensics. Records and results live under
`--job-dir` (or `SUMMON_JOBS_DIR`, default `{tempdir}/subagents_jobs`); point it at a
durable path if the default temp location is volatile for you. Instead of polling the
result path yourself:

- `summon jobs list [--job-dir D] [--json]`: every job's state: `prepared` (launched,
  spawn unconfirmed), `running` (pid known, not asserted alive), a terminal status, or
  `unverified` (a result whose `job_nonce` does not match its record, or a legacy
  result with no record).
- `summon jobs status <id>`: one job's launch record + result envelope + derived state.
- `summon jobs wait <id> [--timeout]`: poll for a nonce-verified result; a stale file
  at the path is skipped until the real child writes, then the envelope is printed.

The child stamps a `job_nonce` into its result envelope so a result at a job's path can
be authenticated against the record that launched it; `status`/`wait` never trust an
unverifiable result. This is a single-user, single-machine registry (summon does not
defend it against other local users on a shared host). Liveness verification, cancel,
and reaping are a later addition.

## Council mode (`--council`) — decide by consensus

For a DECISION (not a task), convene a council of diverse models/personas, then a
chairman synthesizes a consensus recommendation:

```bash
run_subagent.py --council --question "SQL or NoSQL for this workload?" --cwd <abs>
run_subagent.py --council --question-file q.md \
  --members planner,reviewer,coder,pair --chairman fable --rounds 2 --cwd <abs>
```

- **Members** are agents (each encodes a model + persona); the default set is
  deliberately vendor-diverse AND **repo-capable** (`planner`/opus, `reviewer`/codex,
  `coder`/cursor, `pair`/sonnet-5) — all can read files under `--cwd`. Override with
  `--members`; author custom-persona members with `--new-agent`. A council of clones
  is pointless — keep it diverse.
  > **Note:** `agy` members (e.g. `researcher`) run in an isolated profile and can't
  > read `--cwd`, so they error out of a repo-inspection council (fine only for a
  > pure-reasoning question). The council envelope surfaces a warning when one is used.
- **`--rounds 2`** adds a cross-examination + peer-ranking round (à la Karpathy's
  llm-council): each member sees ALL positions anonymized (can't tell which is
  theirs → no favoritism), refines their stance, and **ranks** them best-to-worst.
  Votes aggregate (Borda) into `consensus_ranking` in the envelope, which the
  chairman weighs as one signal. `--rounds 1` (default) = independent positions only.
- **The chairman** (`--chairman`, default `fable`) reads all final positions and
  returns the decision, a confidence, the points of agreement, the dissents (named),
  and a next action — making the call even when the council is split.
- **Pass `--out` on any council you cannot afford to lose.** The council envelope is
  written atomically to `--out` after every phase (`council_state`: `round1_complete`
  / `round2_complete` / `final`, `failed` on validation errors), so a host-tool kill
  mid-synthesis still leaves every completed member position on disk.
- **The wall clock is additive.** Members run at most 3 concurrent per backend
  (waves), then the chairman runs after ALL members: worst case is about
  `rounds x waves x (timeout + 60s) + (timeout + 60s)`. The dispatcher prints this
  estimate to stderr before dispatching; set your host tool's timeout above it.
- **Council consumes a fixed flag set** (`--question`/`--question-file`, `--members`,
  `--chairman`, `--rounds`, `--cwd`, `--agents-dir`, `--timeout`, `--out`, `--run-dir`).
  Anything else (`--model`, `--json-schema`, `--worktree`, `--background`, `--retries`, ...)
  is rejected up front rather than silently ignored; member model/effort/permission
  come from each member agent's own definition.

Returns one council envelope: `{run_id, generation, question, rounds,
members:[{agent, model, position}], synthesis:{chairman, recommendation, report},
elapsed_ms}`. Progress → stderr. Use it for architecture calls, tech-selection, risk
judgments — anything where one model's blind spot is real and consensus (or named
dissent) is the deliverable.

## Durable, resumable council runs

Every council writes a **persistent run directory** (`{cwd}/.agents/runs/<run-id>/`;
override with `--run-dir` or `SUMMON_RUNS_DIR`), printed to stderr at the start and
returned as `run_id`/`run_dir`/`generation` in the envelope. It holds one atomic
envelope per stage (`g<N>-r1-<member>.json`, `g<N>-rankings.json`, `g<N>-chairman.json`),
a `receipt.json` binding the run's inputs, and an append-only `journal-g<N>.jsonl`.
Nothing is deleted on completion, so a council that dies mid-run leaves a complete,
inspectable record.

- **Resume:** `summon council resume <run-id>` re-runs only the stages that are
  missing, failed, or whose upstream inputs changed; every unchanged stage is
  **carried forward and never re-paid**. The question, members, chairman, and rounds
  come from the run's `receipt.json` (changing them means starting a fresh council, so
  those flags are rejected on a resume). A changed repo (`--cwd`), a retuned agent
  definition, or an edited earlier-stage output all invalidate the affected stage and
  everything downstream of it; stale files move to `superseded/` with their spend
  evidence intact.
- **Quorum:** `--quorum N` synthesizes only when at least N members' final stage
  succeeded (2..member-count). Below N the chairman is not dispatched (a `skipped`
  tombstone is recorded and superseded on a later run). Quorum never changes the
  top-level `status` (still `success` only if the synthesis succeeded and no member
  failed); the outcome is reported in `synthesis.quorum` and `synthesis.decision_status`
  (`full_participation` / `partial_participation` / `quorum_not_met` / `synthesis_failed`).
- **Chairman fallback:** `--chairman-fallback AGENT` runs a second synthesizer once if the
  primary chairman ends on any non-success outcome (only success suppresses it). Both
  results persist and appear as `synthesis.primary` and `synthesis.fallback`; the chosen
  recommendation is the fallback when it succeeded, else the primary. On a resume, a failed
  primary re-runs before the fallback (a non-success stage is never carried forward).
- **Per-stage timeouts:** `--member-timeout` and `--chair-timeout` give members and the
  chairman their own clocks (default: `--timeout`). The wall-clock estimate uses both and
  doubles the chairman phase when a fallback is configured.
- **Status:** `summon council status <run-id>` prints the run's derived state
  (per-stage status, generation, attempts, abandoned work) read-only; add `--json` for
  machines. It takes a generation-stable snapshot and reports `consistent: false` if
  the run changed mid-read.
- **How it stays correct:** one owner holds a leased lock per run (renewed after every
  stage), and each ownership period claims a fresh **generation** so a suspended-then-
  resumed process can never overwrite a successor's work; journal and state are
  segmented per generation. One documented limitation: the owner-lock stale-break has a
  sub-millisecond unlink window that pure-stdlib cross-platform file operations cannot
  fully close; because stage outputs are generation-namespaced, the worst case if it
  ever fired is one duplicate stage dispatch (wasted spend), never corrupted output.
  It requires a process suspended past its (600s+) lease resuming inside that exact
  window; single-machine use never hits it in practice.
