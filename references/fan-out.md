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
each job with `--background`, which returns `{job_id, pid, result_file}` at once;
completion = its `result_file` exists (atomically written = complete). Poll those.
(`--background` and `--out` are separate mechanisms and can't be combined — use
`--manifest` when you want per-job result files at chosen paths.)

## Council mode (`--council`) — decide by consensus

For a DECISION (not a task), convene a council of diverse models/personas, then a
chairman synthesizes a consensus recommendation:

```bash
run_subagent.py --council --question "SQL or NoSQL for this workload?" --cwd <abs>
run_subagent.py --council --question-file q.md \
  --members planner,reviewer,researcher,pair --chairman fable --rounds 2 --cwd <abs>
```

- **Members** are agents (each encodes a model + persona); the default set is
  deliberately vendor-diverse (`planner`/opus, `reviewer`/codex, `researcher`/agy,
  `pair`/sonnet-5). Override with `--members`; author custom-persona members with
  `--new-agent`. A council of clones is pointless — keep it diverse.
- **`--rounds 2`** adds a cross-examination + peer-ranking round (à la Karpathy's
  llm-council): each member sees ALL positions anonymized (can't tell which is
  theirs → no favoritism), refines their stance, and **ranks** them best-to-worst.
  Votes aggregate (Borda) into `consensus_ranking` in the envelope, which the
  chairman weighs as one signal. `--rounds 1` (default) = independent positions only.
- **The chairman** (`--chairman`, default `fable`) reads all final positions and
  returns the decision, a confidence, the points of agreement, the dissents (named),
  and a next action — making the call even when the council is split.

Returns one council envelope: `{question, rounds, members:[{agent, model, position}],
synthesis:{chairman, recommendation, report}, elapsed_ms}`. Progress → stderr.
Use it for architecture calls, tech-selection, risk judgments — anything where one
model's blind spot is real and consensus (or named dissent) is the deliverable.
