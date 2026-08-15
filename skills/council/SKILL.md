---
name: council
description: Use for open-ended, vendor-diverse judgment with independent positions, cross-examination, and a chaired synthesis; promote a result to deliberate only after a human fixes the options and policy.
# summon-managed: council-companion
allowed-tools: Bash Read
---

# Council — open-ended judgment for Summon

`/council` is the focused companion entry point for Summon's existing council
workflow. It owns no scheduler, scripts, schemas, or duplicate policy. Read
`../summon/references/deliberation.md` for the boundary between council and the
fixed-option `deliberate` workflow.

Use `/council` when the question is open-ended, the useful output is independent
positions plus cross-examination, or a chairman should synthesize disagreement.
Use `/deliberate` only after a person has fixed at least two option IDs, named
seats, quorum, rounds, physical-attempt budget, absolute deadline, and the
human-approval policy.

The CLI is the same dispatcher:

```text
../summon/scripts/run_subagent.py council --question "…" \
  --members planner,security,operator --chairman architect --rounds 2 \
  --cwd <project> --run-dir <private-runs-root>
```

Council may produce a recommendation or candidate option set, but that prose is
not a durable ballot and cannot authorize a deliberate run. To continue into a
governed decision, show the candidate options to the user, obtain explicit
policy values, and start a fresh `summon deliberate` receipt. Never auto-convert,
retry, or fall back between the two modes.
