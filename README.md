# Summon

**Summon any AI from any CLI.**

One command that lets Claude Code, Codex, Cursor, Gemini CLI — or a plain terminal —
dispatch work to *all* the others. Five vendors, one JSON contract, stdlib-only Python,
no daemon.

```
                          ┌──────────────────────┐
   any host CLI ────────► │       summon         │ ────► claude        (Anthropic)
   (claude, codex,        │  stdlib dispatcher   │ ────► codex         (OpenAI)
   cursor, gemini,        │  one JSON contract   │ ────► cursor-agent  (Cursor)
   or your terminal)      │  no server, no pip   │ ────► gemini        (Google)
                          └──────────────────────┘ ────► agy           (Antigravity)
```

Most multi-agent tools assume one specific CLI is the orchestrator. Summon inverts
that: **any CLI can be the boss.** Your Codex session can summon Claude for a review.
Your Claude session can summon Codex for an adversarial pass. Your terminal can summon
all five at once — each in its own git worktree, in the background.

## 60-second quickstart

```bash
git clone https://github.com/nsider/summon && cd summon
python install.py          # copies the skill into every AI CLI on your machine
python summon.py --doctor  # which backends are ready? what's missing?
```

Then point it at any project (absolute `--cwd`; run the shim from the repo, or use
the copy the installer placed in each host's `skills/summon/`):

```bash
python summon.py --agent reviewer --prompt "Review the diff on this branch" --cwd "$PWD"
```

Or just ask your AI CLI — the installed skill triggers on natural language:

> "Summon the adversarial reviewer on my last commit."

## Why summon

| | |
|---|---|
| **Cross-vendor, both directions** | 5 backends, and any of them can be the *host*. Cross-vendor review — no model grading its own homework — is a first-class workflow, not a hack. |
| **One structured contract** | Every dispatch returns JSON: parsed report block, `report_ok` lie-detection, session id, token usage, cost. Branch on fields, not vibes. |
| **Session resume** | `--resume <session_id>` continues a sub-agent's conversation without re-sending its whole definition. Follow-ups cost a fraction of fresh calls. |
| **Fan-out from anywhere** | `--background` + `--worktree` give *every* host the parallel-agents primitive only Claude Code has natively: N agents, N branches, no collisions. |
| **Honest model discovery** | `--list-models` reports what each backend can run — live-queried where the CLI exposes it, config-read or clearly marked `static` where it doesn't. New models need zero code changes. |
| **No daemon, no deps** | The dispatcher is pure Python stdlib — no server, no MCP, no pip install. (One exception: the optional agy backend's PTY wrapper needs `pip install pywinpty pyte`; the other four backends need nothing.) Auditable in an afternoon — which matters for a tool that can run with permissions bypassed. |
| **Billing guard** | Codex children get `OPENAI_API_KEY` stripped by default, so delegations bill your ChatGPT subscription — a stray env key can't silently flip you to metered API pricing. |

## What a dispatch returns

```json
{
  "status": "success",
  "result": "…the agent's full answer…",
  "report": {
    "status": "DONE",
    "summary": "Reviewed 4 files; 2 findings",
    "findings": "…",
    "handoff": "Fix the race in poller.py:88 first; tests in tests/test_poll.py cover it"
  },
  "report_ok": true,
  "session_id": "0197…",
  "usage": {"input_tokens": 12038, "output_tokens": 981},
  "cost_usd": 0.084,
  "resume": {"cli": "claude", "session_id": "0197…"}
}
```

- `report.handoff` → paste into the next dispatch. Chains without re-explaining.
- `report_ok: false` on a "success" → the response also gets `suspect: true`.
  Agents that skip their report contract don't get believed.
- `resume.session_id` → `--resume` for cheap follow-ups.

## The starter roster (20 agents included)

Planning & architecture on Claude (`planner`, `architect`, `deep-debugger`,
`security-auditor`), implementation & adversarial review on Codex (`implementer`,
`reviewer`, `adversarial-reviewer`, `debugger`, `test-author`), coding on Cursor
(`coder`, `bug-fixer`), research & docs & frontend on Antigravity (`researcher`,
`docs-writer`, `frontend`), plus general-purpose lanes (`pair`, `editor`,
`quick-reviewer`, `pr-prep`, `fable`, `antigravity`).

Each agent is a plain `.md` file with frontmatter — edit them, delete them, add your
own. `install.py` never overwrites an agent you already have.

```markdown
---
run-agent: codex        # which CLI executes this agent
permission: safe-edit   # read-only | safe-edit | yolo
---
# My Agent
…system context, task shape, report contract…
```

## Parallel fan-out

```bash
# three implementations racing, each isolated, none blocking your session
python summon.py --agent implementer --prompt "$SPEC" --cwd "$PWD" --worktree try-codex  --background
python summon.py --agent coder       --prompt "$SPEC" --cwd "$PWD" --worktree try-cursor --background
python summon.py --agent pair        --prompt "$SPEC" --cwd "$PWD" --worktree try-claude --background
# each returns {"job_id", "result_file", "pid"} immediately; poll the result files,
# diff the three branches, merge the winner
```

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full playbook: the report/HANDOFF
contract, cross-vendor review rules, and named patterns (debate, async build,
competing hypotheses, consensus review).

## How it compares

| | summon | agent-bridge / CCB / claude-codex-collab | cc-fleet | MCO |
|---|---|---|---|---|
| Vendors | **5** (incl. Antigravity headless — nobody else drives it) | 2–3 | Claude only | 2–3 |
| Any CLI as host | **yes** | mostly Claude-hosted | no | no |
| Structured report + lie-detection | **yes** | partial | no | no |
| Cost/usage telemetry | **yes** (where the backend emits it) | partial | no | no |
| Session resume | **yes** (claude/codex/cursor/agy) | some | n/a | no |
| Worktree + background fan-out | **yes, from any host** | no | yes (Claude-hosted) | no |
| Runtime footprint | **a folder of stdlib Python** | daemon / MCP server / npm tree | plugin | server |

(Honest caveats: those tools have better streaming UIs and bigger communities; summon
is a dispatcher, not a dashboard. Gemini resume isn't supported — its CLI can't
re-target a specific headless session.)

## Platform support

- **Windows**: battle-tested daily across all 5 backends (the .cmd-shim and console
  quirks other tools trip on are handled).
- **Linux/macOS**: claude, codex, cursor-agent, gemini fully supported; CI runs the
  suite on Ubuntu. The **agy** backend is Windows-only out of the box (its headless
  mode requires a ConPTY capture wrapper we bundle); on POSIX, bring your own PTY
  wrapper via `AGY_PTY_WRAPPER` or use the other four.
- **Python**: 3.10+ (3.11+ recommended; stdlib only).

## Security model

- Agent `permission:` maps to each CLI's own sandbox flags — `read-only`,
  `safe-edit` (default), or `yolo` (bypasses approvals: use only in repos you trust).
- The agy backend copies OAuth tokens into a per-invocation profile locked to your
  user (icacls on Windows, `0700` on POSIX) and isolated from your real profile.
- `--doctor` shows which CLI binaries would be dispatched to (path + version) and how
  each backend authenticates; it verifies presence, not credential validity.
- No network calls of its own, no telemetry phoned home. Processes it spawns: the
  backend CLIs, plus supporting tools where a feature needs them (`git` for
  `--worktree`, `icacls`/`chmod` for agy profile lockdown, a Python PTY wrapper for
  agy, and a detached copy of itself for `--background`).

## FAQ

**What happens when a vendor ships a new model?**
Nothing breaks and nothing needs updating: model strings pass through verbatim.
Agents pinned to aliases (`opus`, `sonnet`) float automatically; `--list-models`
tells you what's available; `--model` invokes it.

**Does it need API keys?**
No — it drives the CLIs you already log into (subscriptions). It deliberately
*strips* `OPENAI_API_KEY` from codex children (see billing guard).

**Truly zero pip installs?**
For claude/codex/cursor/gemini: yes, stdlib only. The optional agy backend needs
`pip install pywinpty pyte` for its PTY wrapper (Windows) — `--doctor` tells you if
that's missing.

**Why not MCP?**
MCP would add a server and a session dependency for what is fundamentally a
one-shot subprocess dispatch. A script you can read beats a protocol you have to
trust. (An MCP facade may come later; the JSON contract won't change.)

## Roadmap

- POSIX PTY wrapper for the agy backend (`script`/pty-based)
- Gemini resume when its CLI grows a stable session id
- Optional MCP facade

## Credits

Ideas were sharpened against the ecosystem: agent-bridge, CCB, claude-codex-collab,
cc-fleet, MCO, swarms, Omnigent. Summon started as a private skill dispatching real
work across five CLIs daily; this repo is that tool, generalized.

## License

[MIT](LICENSE)
