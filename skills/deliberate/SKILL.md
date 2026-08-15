---
name: deliberate
description: Use for governed fixed-option decisions that require named seats, fixed quorum, hard attempt/deadline bounds, durable replay, or explicit human approval/cancel boundaries.
# summon-managed: deliberate-companion
allowed-tools: Bash Read
---

# Deliberate — governed decisions for Summon

`/deliberate` is the focused companion entry point for Summon's governed-decision
workflow. It owns no scheduler, scripts, schemas, or duplicate policy. Read the
canonical guide at `../summon/references/deliberation.md` before constructing,
inspecting, recovering, or resuming a run.

Use `/deliberate` when a decision has explicit options and named seats and must be
bounded by quorum, physical attempts, an absolute deadline, and a durable journal.
Use `/summon` for ordinary dispatch, manifest/fan-out, and council work. Council
remains the right choice for cross-examination and a chairman's synthesis.

Before invoking it, obtain—not infer—the question, 2–10 seat IDs, at least two
option IDs, quorum, rounds, maximum physical attempts, deadline, human-approval
policy, private durable run directory, and each seat's authority/consent/worktree
evidence. If any of those facts are missing, ask the user or choose another mode.

The CLI is the same dispatcher:

```text
../summon/scripts/run_subagent.py deliberate --question "…" --seats A,B \
  --options X,Y --quorum all --rounds 1 --max-attempts 4 --deadline 30m \
  --run-dir <private-runs-root> --cwd <project>
```

The current fresh/resume provider path is `integration_pending`; it never silently
falls back to another mode. `status`, `replay`, `recover`, `cancel`, and `open` are
provider-inert. An unmatched physical start is `uncertain_spend`; do not retry it
unless the user explicitly supplies `--retry-indeterminate`.
