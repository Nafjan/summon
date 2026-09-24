# The Summon orchestration protocol

How to run multi-agent work through summon without fooling yourself. These rules were
extracted from months of real cross-vendor orchestration; every one exists because
skipping it burned us at least once.

## The mental model

Sub-agents are **stateless one-shots** (unless you `--resume`). Each dispatch must carry
complete, self-contained context. Through `summon.cmd`, put every dispatch prompt in
a UTF-8 file and use `--prompt-file`; raw `--prompt` is refused even for simple text.
This protects the batch input boundary, not every backend's argv transport. The dispatcher
injects the agent
definition and `{cwd}/.agents/memory.md` for you — everything else you must say
explicitly. On any follow-up, pass forward what the previous call learned (its
`report.handoff`).

## Hard rules

1. **Verify before trusting.** Never take `STATUS: DONE` at face value. Read the actual
   diff after every editing agent; rerun targeted tests. Use the parsed `report` /
   `report_ok` fields and treat `suspect: true` as unverified work requiring review.
   Before another attempt, inspect the typed error, provider-contact and spend evidence,
   and `retryable`. A `retryable: false` result or uncertain contact/spend is not a
   retry cue; require explicit retry authority and reconcile the existing attempt first.
   A non-null `model.served` is not sufficient proof: exact identity also requires
   authoritative `reported` evidence and `named_model_verified: true`.
2. **Cross-vendor review.** No agent's work is reviewed by its own vendor. A model
   reviewing its own output shares its blind spots. Codex-written code goes to a Claude
   reviewer; Claude/Cursor-written code goes to Codex (`reviewer` / `adversarial-reviewer`).
3. **Substantive changes get adversarial review** before merge. Bundle small related
   changes and run one adversarial pass over the bundle.
4. **Continue only an eligible session.** A prior `resume.session_id` is a handle, not
   permission to resume. Check the route, model/profile identity, permission ceiling,
   and history for compatibility; governed continuation requires authenticated source
   evidence and fresh spend consent. If that path is unavailable or incompatible,
   preserve the history and handoff in an explicit fork where supported, or an
   explicitly authorized fresh task. Do not claim that a fresh call resumed the old
   session. Parallel *editing* agents each get their own `--worktree`.
5. **Track every delegation** (agent, backend, model, branch, finding) in your task list.

## Review-first landing boundary

Use [`REVIEW_BRIEF_STANDARD.md`](REVIEW_BRIEF_STANDARD.md) for implementer and reviewer
prompts. A child may edit the supplied working directory when its declared permission
and backend allow it, but it does not land work: no `git add`, `git commit`, `git push`,
`git merge`, `git restore`, `git stash`, or PR creation, and no reset/clean/discard/force-removal. The caller reads
the real diff, reruns the gates, and the designated reviewer owns every staging, commit,
push, merge, and PR action. Every child reports `LEFT_BEHIND`, including temporary files,
servers, containers, VMs, worktrees, and processes it created or intentionally left.

The boundary is a workflow contract, not a backend sandbox claim. Verify it from the
diff and receipt; do not infer it from a prompt or permission label.

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
dispatch's context (`--prompt-file` through `summon.cmd`). A good HANDOFF names: the
goal, files touched (+ commit SHA if committed), constraints/non-goals, acceptance
criteria, and unresolved risks.

## Named patterns

**Debate** — two vendors argue, you synthesize.
Dispatch the same design question to a Claude agent (`planner`) and a Codex agent
(`reviewer`), 2 rounds each. Feed the other side's argument into round 2 and use
`--resume` only when hard rule 4's eligibility and authority checks pass; otherwise
make the fork or fresh task explicit. The orchestrator synthesizes; disagreement
that survives round 2 is signal.

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

- The dispatcher strips `OPENAI_API_KEY` from Codex children by default, so the child
  uses the selected Codex CLI login. That does not prove a subscription, allowance,
  credit treatment, or invoice outcome; those remain provider/account facts. Opt out
  with `SUBAGENTS_ALLOW_OPENAI_KEY=1` when an explicit API-key route is intended.
- `usage` / `cost_usd` are included only when the backend reports them. They are
  provider-reported usage or estimates, not a universal invoice or remaining-credit
  measurement; retain the provider's billing records as authoritative.
