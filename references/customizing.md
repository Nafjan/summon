# Customizing agents & the roster

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Customizing agents (you, the calling agent, are expected to)

The bundled roster is a starting point, not a fixed menu. As the orchestrator you
have two levers — use them freely:

**1. Per-dispatch, no files touched** — override an agent's model, reasoning effort,
and backend flags for a single call:
```bash
run_subagent.py --agent reviewer --model claude-sonnet-5 --effort high \
  --prompt "…" --cwd <abs>
```
`--model` accepts any model the backend supports (see [models.md](models.md));
`--effort` is `low|medium|high|xhigh|max` (claude). The prompt itself is your main
customization — the agent definition sets the role, the prompt sets the task.

**2. Durably — manage the roster from the CLI** (no hand-authored markdown needed):

```bash
# scaffold a new agent (house template: report contract + untrusted-content guard)
run_subagent.py --new-agent fact-checker \
  --set run-agent=codex --set permission=read-only --set model=gpt-5.6-sol
# then edit the body (purpose, Role, rubric) in the printed path

# retune an existing agent's frontmatter — body untouched, validated, atomic
run_subagent.py --set-agent pair --set model=claude-sonnet-5
run_subagent.py --set-agent reviewer --set 'args=-c model_reasoning_effort="high"'
run_subagent.py --set-agent probe --set model=        # empty value REMOVES the key
```

Settable keys: `run-agent` (claude/codex/cursor-agent/gemini/agy), `model`,
`permission` (`read-only`/`safe-edit`/`yolo`), `args` (extra backend flags) —
values are validated before anything is written. `--new-agent` never overwrites;
`--set-agent` edits frontmatter only, leaving the body byte-identical.

Definitions are plain `.md` files in the agents dir (`--agents-dir`,
`$SUB_AGENTS_DIR`, or `{cwd}/.agents/`) and register **instantly** — no reload; the
next `--list`/dispatch sees them. You can still write or edit the files directly
(the scaffold exists because a hand-written definition tends to miss the
Final-report contract the dispatcher parses). Authoring a task-specific persona is
one `--new-agent` plus a body edit — do it whenever the standing roster doesn't fit.

**Different models per role is the whole point.** Give planning/architecture agents a
deep model (`opus`, `claude-fable-5`), balanced work a `claude-sonnet-5` agent, cheap
mechanical passes a lighter one, and fan a task across several models at once with
`--manifest` (per-job `model:`). Nothing here is baked into the skill — it's all in the
`.md` files and the flags you pass.
