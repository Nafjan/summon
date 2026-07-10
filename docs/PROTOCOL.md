# The Summon orchestration protocol

How to run multi-agent work through summon without fooling yourself. These rules were
extracted from months of real cross-vendor orchestration; every one exists because
skipping it burned us at least once.

## The mental model

Sub-agents are **stateless one-shots** (unless you `--resume`). Each dispatch must carry
complete, self-contained context in `--prompt`. The dispatcher injects the agent
definition and `{cwd}/.agents/memory.md` for you — everything else you must say
explicitly. On any follow-up, pass forward what the previous call learned (its
`report.handoff`).

## Hard rules

1. **Verify before trusting.** Never take `STATUS: DONE` at face value. Read the actual
   diff after every editing agent; rerun targeted tests. Use the parsed `report` /
   `report_ok` fields and treat `suspect: true` as a re-dispatch signal, not a warning.
2. **Cross-vendor review.** No agent's work is reviewed by its own vendor. A model
   reviewing its own output shares its blind spots. Codex-written code goes to a Claude
   reviewer; Claude/Cursor-written code goes to Codex (`reviewer` / `adversarial-reviewer`).
3. **Substantive changes get adversarial review** before merge. Bundle small related
   changes and run one adversarial pass over the bundle.
4. **Resume, don't re-prime.** For a genuine follow-up to the SAME agent, pass the prior
   `resume.session_id` to `--resume` instead of a fresh call that re-sends the whole
   agent definition. Parallel *editing* agents each get their own `--worktree`.
5. **Track every delegation** (agent, backend, model, branch, finding) in your task list.

## The report contract

Every bundled agent ends its reply with a fenced report block:

```
STATUS: DONE | PARTIAL | BLOCKED
SUMMARY: <one line>
<work-product fields: FINDINGS / CHANGES / VERDICT / TESTS / ...>
FOLLOW-UP: <what should happen next, or "none">
HANDOFF: <context the NEXT call needs to continue this work>
```

The dispatcher parses this into `response["report"]` and sets `report_ok` when the
bookends are present. **HANDOFF is the chain-link field** — feed it into the next
dispatch's `--prompt`. A good HANDOFF names: the goal, files touched (+ commit SHA if
committed), constraints/non-goals, acceptance criteria, and unresolved risks.

## Named patterns

**Debate** — two vendors argue, you synthesize.
Dispatch the same design question to a Claude agent (`planner`) and a Codex agent
(`reviewer`), 2 rounds each (use `--resume` for round 2, feeding the other side's
argument). The orchestrator synthesizes; disagreement that survives round 2 is signal.

**Async build** — spec → build → cross-review, hands-free.
1. `planner` writes the spec (claude)
2. `implementer` builds it with `--worktree --background` (codex)
3. Poll the job file; when done, `quick-reviewer` (claude) reviews the branch diff —
   cross-vendor per hard rule 2
4. Merge only on a clean review.

**Competing hypotheses** — for gnarly bugs.
Dispatch `debugger` (codex) and `deep-debugger` (claude) the same repro in parallel
worktrees. Compare root causes: agreement = high confidence; disagreement = neither is
verified, keep digging.

**Consensus review** — for high-stakes diffs.
Same review brief to `reviewer` (codex) and `quick-reviewer` (claude). Agreement on a
finding = act on it. Findings only one flags = verify manually before acting.

## Shared memory

Put standing project context — conventions, constraints, durable decisions — in
`{cwd}/.agents/memory.md`. It is injected into every dispatch automatically (capped at
8 KB). Write it once; stop re-explaining your stack in every prompt.

## Billing notes

- The dispatcher strips `OPENAI_API_KEY` from codex children so delegations bill the
  ChatGPT subscription, never a metered API key left in your env. Opt out with
  `SUBAGENTS_ALLOW_OPENAI_KEY=1`.
- `usage` / `cost_usd` come back in every response that the backend reports them for —
  sum them across a chain to know what an orchestration actually cost.
