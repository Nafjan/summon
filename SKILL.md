---
name: summon
description: Summon other AI CLIs (claude, codex, cursor-agent, gemini, agy) as sub-agents. Use when the user names an agent or sub-agent to run, references an agent definition, or delegates a task to another AI/agent.
allowed-tools: Bash Read
---

# Summon - Cross-Vendor Sub-Agents for Any AI CLI

Spawns external CLI AIs (claude, cursor-agent, codex, gemini, agy) as isolated sub-agents with dedicated
context. Supports session resume, per-call model/effort overrides, isolated git worktrees, background
dispatch, structured report parsing, and cost/usage telemetry — see Parameters and the response-field table.

## Resources

- **[run_subagent.py](scripts/run_subagent.py)** - Main execution script
- **[codex.md](references/codex.md)** - Codex-specific setup (permissions, timeout)

**Script Path**: Use absolute path `{SKILL_DIR}/scripts/run_subagent.py` where `{SKILL_DIR}` is the directory containing this SKILL.md file.

## CLI-Specific Notes

Check the corresponding reference for your environment:
- **Codex**: Read [references/codex.md](references/codex.md) BEFORE first execution

## Interpreting User Requests

Extract parameters from user's natural language request:

| Parameter | Source |
|-----------|--------|
| --agent | Agent name from user request (see selection rules below) |
| --prompt | Task instruction part (excluding agent specification) |
| --cwd | Current working directory (absolute path) |

**Agent Selection Rules** (when user doesn't specify agent name):
1. Run `--list` to get available agents
2. **0 agents**: Inform user no agents available, show setup instructions (see [Agent Definition Format](#agent-definition-format))
3. **1 agent**: Auto-select without asking
4. **2+ agents**: Show list with descriptions, ask user to choose

**Example**:
"Run code-reviewer on src/"
→ `--agent code-reviewer --prompt "Review src/" --cwd $(pwd)`

## Important: Permission and Timeout

This script executes external CLIs that require elevated permissions.

**Before first execution:**
1. Request elevated permissions via your CLI's tool parameters
2. Set tool timeout to match `--timeout` argument (default: 600000ms)

**For Codex CLI** (most common permission issues): See [references/codex.md](references/codex.md) for exact JSON parameter format.

## Workflow

### Step 0: Read CLI-Specific Setup (if applicable)

If you are running on Codex, read [references/codex.md](references/codex.md) first.

### Step 1: Check Health (first run) and List Agents

On a machine you haven't dispatched from before, run `--doctor` once — it reports which
backends are installed/usable and how to finish setting up the rest.

### Step 1b: List Available Agents

**Always list agents first** to discover available definitions:

```bash
scripts/run_subagent.py --list
```

Output:
```json
{"agents": [{"name": "code-reviewer", "description": "Reviews code..."}], "agents_dir": "/path/.agents"}
```

**If agents list is empty**:
1. Create `{cwd}/.agents/` directory
2. Add agent definition file (see [Agent Definition Format](#agent-definition-format))
3. Re-run `--list` to verify

### Step 2: Execute Agent

```bash
scripts/run_subagent.py \
  --agent <name> \
  --prompt "<task>" \
  --cwd <absolute-path>
```

### Step 3: Handle Response

Parse JSON output and check `status` field:

```json
{"result": "...", "exit_code": 0, "status": "success", "cli": "claude"}
```

**By status:**

| status | Meaning | Action |
|--------|---------|--------|
| `success` | Task completed | Use `result` directly |
| `blocked` | Sub-agent ended awaiting an interactive approval (CLI exited 0, but nobody can click approve in one-shot mode) | Raise the `permission` level or move referenced files under `--cwd`, then re-dispatch. `blocked_indicators` lists the markers seen |
| `partial` | Timeout but has output | Review partial `result`, may need retry |
| `error` | Execution failed | Check `error` field and `exit_code`, fix and retry |

**By exit_code** (when status is `error`):

| exit_code | Meaning | Resolution |
|-----------|---------|------------|
| 0 | Success | - |
| 124 | Timeout | Increase `--timeout` or simplify task |
| 127 | CLI not found | Install required CLI (claude, codex, etc.) |
| 1 | General error | Check `error` field in response |

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--list` | - | List available agents (no other params needed) |
| `--list-models` | - | Report invocable models per backend (no other params needed; add `--cli` to filter). See "Model discovery" below |
| `--doctor` | - | Check backend CLIs, wrapper deps, agents dir, git; add `--json` for machines. Run this FIRST on a new machine |
| `--agent` | Yes* | Agent definition name from --list |
| `--prompt` | Yes* | Task description to delegate |
| `--cwd` | Yes* | Working directory (absolute path) |
| `--timeout` | No | Bare ms or with suffix: `600s`, `10m` (default: 600000 = 10m). Set your host tool's own timeout ABOVE this value — the script needs a few seconds of overhead beyond the CLI deadline |
| `--cli` | No | Force CLI: `claude`, `cursor-agent`, `codex`, `gemini`, `agy` |
| `--model` | No | Override the agent's frontmatter model for this call |
| `--effort` | No | Reasoning effort (claude only): `low`\|`medium`\|`high`\|`xhigh`\|`max` |
| `--resume` | No | Continue a prior session: pass its `resume.session_id` (claude/codex/cursor) or `latest` for agy |
| `--resume-profile` | No | agy only: the `resume.profile` path returned by the prior agy call |
| `--worktree` | No | Run in an isolated git worktree (optional name; auto-named if bare) |
| `--background` | No | Dispatch detached; returns `{status:"background", job_id, result_file}` at once |

*Required when not using --list

## Chaining & continuity (response fields)

Every response carries structured fields for programmatic orchestration:

| Field | Use |
|-------|-----|
| `report` | Parsed report contract as a dict (`status`, `summary`, `handoff`, `follow_up`, plus work-product fields). Paste `report["handoff"]` into the next `--prompt`; branch on `report["status"]`. |
| `report_ok` | `true` when the full contract block is present. If `status:"success"` but `report_ok:false`, the response also has `suspect:true` — re-dispatch rather than trusting it. |
| `resume` | `{cli, session_id, profile?}`. Feed `session_id` to `--resume` (or `profile` to `--resume-profile` for agy) for a cheap follow-up that skips re-sending the agent definition. |
| `session_id`, `usage`, `cost_usd` | Telemetry (claude/codex expose all; agy exposes none). Track spend/tokens across a chain. |
| `elapsed_ms` | Wall-clock for the dispatch (present on every path, including errors). Use it to tune swarm concurrency. |
| `blocked_indicators` | Approval-request phrases found in the result tail. With a missing report contract the status is downgraded to `blocked`; with a complete report they are informational only. |
| `worktree` | `{path, branch}` when `--worktree` was used. Merge the branch and `git worktree remove` when done — cleanup is the orchestrator's job. |

**Shared memory:** if `{cwd}/.agents/memory.md` exists it is auto-injected into every
agent's context (project conventions, standing constraints, durable decisions) — put
things there once instead of re-explaining them in each `--prompt`.

## Model discovery (`--list-models`)

The skill never hardcodes a model allowlist — a `model:` string (frontmatter) or
`--model` (override) is passed through to the CLI verbatim, so **any model a backend
supports is invocable the moment it ships, with zero code changes.** How a *new* model
reaches an agent depends only on how that agent names its model:

| How the model is named | Example | When a new model ships |
|---|---|---|
| **Alias** (claude only) | `opus`, `sonnet` | **Auto-floats** — the CLI resolves the alias to the latest release. No action. |
| **Unpinned** | (no `model:`) | Floats with the CLI's own default (agy, gemini). |
| **CLI-config default** | codex | Uses `~/.codex/config.toml` `model`; move the default there or pass `--model`. |
| **Version ID** | `claude-fable-5`, `composer-2.5` | **Frozen** — bump the agent's `model:` (or `CURSOR_DEFAULT_MODEL` in `_builder.py`). |

`--list-models` answers "what can each backend run *right now*" live where the CLI
exposes it. Each entry is tagged with a `source` so you know how much to trust it:
- `live` — queried just now (`agy models` — the only backend with a real list)
- `config` — read from the CLI's own default config (`codex` → config.toml)
- `static` — documented aliases/defaults to pass via `--model` (CLI has no list)
- `unavailable` — a live query was attempted and failed (reason in `note`)

Prefer floating aliases (`opus`/`sonnet`) over pinned IDs unless an agent deliberately
needs a fixed model (e.g. `fable` = the escalation tier). Discover with `--list-models`,
invoke with `--model` — new models never require editing the skill itself.

## Fan-out (swarms) — the blessed pattern

The script is deliberately single-shot; swarms are built by composing `--background`.
The pattern that works (verified on 40-run workloads):

1. **Payload in files, not prompts.** Write each work packet under `--cwd`
   (e.g. `.agents/packets/job-07.md`) and keep the `--prompt` short: "Read
   .agents/packets/job-07.md and follow it." Hard numbers: agy rejects prompts
   over ~28,000 chars (Windows argv limit); other CLIs degrade before they fail.
   Files under cwd also avoid `blocked` reads.
2. **Dispatch with `--background`** — each call returns
   `{job_id, pid, result_file}` immediately. Completion = the `result_file`
   exists (the write is atomic; a present file is a complete JSON envelope).
3. **Throttle per backend, not globally.** Safe starting points: 3-4 concurrent
   claude/codex/cursor; 2 concurrent agy (each run gets its own isolated
   profile, so concurrency is safe — the cap is for machine load, not
   correctness). Use `elapsed_ms` from completed envelopes to tune.
4. **Editing swarms get `--worktree`** (one per agent, branches never collide).
   Read-only swarms don't need it.
5. **Judge with the envelope**: collect `report.status` / `report.handoff`,
   treat `blocked` and `suspect: true` as re-dispatch signals, and sum
   `usage`/`cost_usd` for the bill.

## Agent Definition Location

| Priority | Source | Path |
|----------|--------|------|
| 1 | Environment variable | `$SUB_AGENTS_DIR` |
| 2 | Default | `{cwd}/.agents/` |

To customize: `export SUB_AGENTS_DIR=/custom/path`

## Agent Definition Format

Place `.md` files in `.agents/` directory:

```markdown
---
run-agent: claude
permission: safe-edit
---

# Agent Name

Brief description of agent's purpose.

## Task
What this agent does.

## Output Format
How results should be structured.
```

**Critical**: The `run-agent` frontmatter determines which CLI executes the agent.

**Frontmatter fields:**

| Field | Values | Description |
|-------|--------|-------------|
| `run-agent` | `codex`, `claude`, `cursor-agent`, `gemini`, `agy` | Which CLI executes this agent |
| `permission` | `read-only`, `safe-edit` (default), `yolo` | Approval/sandbox level the sub-agent runs with |
| `model` | CLI-specific string (optional) | Pin this agent to a model; `--model` at dispatch overrides it |

**`model:` per-CLI semantics** (the string is passed to the CLI verbatim):

| CLI | Accepts | Example | Unpinned default |
|-----|---------|---------|------------------|
| claude | alias (floats to latest) or full ID | `opus`, `sonnet`, `claude-fable-5` | CLI's default |
| codex | any codex model id (`-m`) | `gpt-5.6-sol` | `~/.codex/config.toml` `model` |
| cursor-agent | cursor model ids | `composer-2.5` | `composer-2.5` |
| gemini | gemini model ids (`-m`) | `gemini-3.1-pro` | CLI's default |
| agy | display name or slug (see `agy models`) | `Claude Opus 4.6 (Thinking)`, `gemini-3.1-pro` | Gemini Flash tier |

Run `--list-models` to see what each backend can run right now.

**`permission` → exact per-CLI flags** (what the script actually passes — the
levels are NOT identical across CLIs; when behavior surprises you, check this table):

| Level | claude | codex | cursor-agent | gemini | agy |
|-------|--------|-------|--------------|--------|-----|
| `read-only` | `--permission-mode plan` | `-s read-only` | `--mode plan` | `--approval-mode plan` | `--sandbox` |
| `safe-edit` | `--permission-mode acceptEdits` | `-s workspace-write -c approval_policy=never` | `--trust` | `--approval-mode auto_edit` | `--dangerously-skip-permissions` |
| `yolo` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `-f --trust` | `-y` | `--dangerously-skip-permissions` |

Caveats worth knowing:
- `read-only` sandboxes differ: claude's plan mode can block even *reads* the
  prompt depends on (a blocked run now returns `status: blocked` — see the
  status table). If a read-only agent must read files, keep them under `--cwd`.
- agy has no true read-only: `--sandbox` is its closest mode, and `safe-edit`
  already maps to skip-permissions there (constrain by instruction instead).
- For investigation agents that only need to *read*, `yolo` +
  "do not modify files" in the agent body is often more reliable than
  `read-only` — several CLIs' plan modes end turns asking for approval.

## CLI Selection Priority

1. `--cli` argument (explicit override)
2. Agent definition `run-agent` frontmatter
3. Auto-detect caller environment
4. Default: `codex`

## Common Mistakes

| Mistake | Result | Fix |
|---------|--------|-----|
| Skip `--list` before execution | Agent not found error | Always run `--list` first |
| Use relative path for `--cwd` | Validation fails | Use absolute path |
| Ignore `status` field in response | Undetected errors | Always check `status` before using `result` |
| Prompt references files OUTSIDE `--cwd` | Sandboxed reads → run ends awaiting approval (`status: blocked`) | Put every input file under `--cwd` before dispatching |
| Very long prompts | agy hard-fails over ~28,000 chars; others degrade | File-based payload: write the packet under `--cwd`, prompt = "Read <file> and follow it" |
| `run-agent: gemini` on a deprecated/ineligible account | Multi-layer auth error (IneligibleTierError buried under warnings) | Run `--doctor` first; prefer `agy` for the Google lane |
| Host tool timeout ≤ `--timeout` | Host kills the script before it can report | Set the host's timeout above `--timeout` + a few seconds of overhead |
