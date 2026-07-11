# Summon

**Summon any AI from any CLI.**

[![CI](https://github.com/Nafjan/summon/actions/workflows/ci.yml/badge.svg)](https://github.com/Nafjan/summon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
![stdlib only](https://img.shields.io/badge/deps-stdlib_only-brightgreen.svg)

One command that lets Claude Code, Codex, Cursor, Gemini CLI, Antigravity — or a plain
terminal — dispatch work to *all* the others, plus any OpenAI-compatible API (OpenRouter,
OpenAI, local Ollama…). Six backends, one JSON contract, stdlib-only Python, no daemon.

```
                          ┌──────────────────────┐
   any host CLI ────────► │       summon         │ ───► claude         (Anthropic)
   (claude, codex,        │  stdlib dispatcher   │ ───► codex          (OpenAI)
   cursor, gemini,        │  one JSON envelope   │ ───► cursor-agent   (Cursor)
   or your terminal)      │  no server, no pip   │ ───► gemini         (Google)
                          └──────────────────────┘ ───► agy            (Antigravity)
                                                   └──► openai-compat   (OpenRouter / OpenAI /
                                                        Anthropic / Google / Ollama / …)
```

Most multi-agent tools assume one specific CLI is the orchestrator. Summon inverts that:
**any CLI can be the boss.** Your Codex session can summon Claude for a review. Your Claude
session can summon Codex for an adversarial pass. Your terminal can summon a whole
council of models to *decide* something — each in its own git worktree, in parallel.

## 60-second quickstart

```bash
git clone https://github.com/Nafjan/summon && cd summon
python install.py            # installs the `summon` skill into every AI CLI on your machine
python summon.py doctor      # which backends are ready? what's missing?
```

Then point it at any project (absolute `--cwd`):

```bash
python summon.py dispatch --agent reviewer --prompt "Review the diff on this branch" --cwd "$PWD"
```

Or just ask your AI CLI — the installed skill triggers on natural language:

> "Summon the adversarial reviewer on my last commit."

The skill installs as **`summon`** (invoke `/summon`). Migrating from the old `sub-agents`
name? `python install.py --with-alias` adds a thin `sub-agents` alias to the same dispatcher.

## Command surface

Git-style subcommands (the legacy flat `--flag` form still works — nothing breaks):

| Command | Does |
|---|---|
| `summon dispatch --agent N --prompt … --cwd D` | run one agent (the default action) |
| `summon list` | list available agents |
| `summon models [--cli B]` | what each backend can run right now |
| `summon doctor [--json]` | backend / setup health check |
| `summon manifest FILE` | run a batch swarm (per-backend concurrency, resume) |
| `summon council --question "…"` | **decide by consensus** of diverse models |
| `summon agent new\|set NAME --set k=v` | scaffold / retune an agent definition |

## Why summon

| | |
|---|---|
| **Cross-vendor, both directions** | 6 backends, and any CLI can be the *host*. Cross-vendor review — no model grading its own homework — is a first-class workflow, not a hack. |
| **One structured envelope** | Every dispatch returns JSON: parsed report, `report_ok` lie-detection, `model.resolved` (what actually served), `permission_flags`, `usage`, `cost_usd`, `billing` source, `elapsed_ms`. Branch on fields, not vibes. |
| **Council mode** | `summon council --question "SQL or NoSQL?"` convenes diverse models, they cross-examine and **rank each other anonymously**, and a chairman synthesizes a decision + confidence + named dissents (the llm-council pattern, over real cross-vendor CLIs). |
| **Any model, incl. local** | `run-agent: openai-compat` reaches any OpenAI-compatible API — OpenRouter, OpenAI, Anthropic, Google, Groq, or a **local** Ollama / LM Studio / vLLM — in one stdlib HTTP call. |
| **Native fan-out** | `summon manifest jobs.json --concurrency agy=2,codex=3` runs a swarm: per-backend throttles, one atomic envelope per job, **skip-if-done resume**. |
| **Structured output** | `--json-schema verdict.json` extracts + validates the agent's final JSON and retries once on mismatch — `parsed`/`parse_ok`, not a brittle brace heuristic. |
| **Session resume** | `--resume <session_id>` continues a sub-agent without re-sending its definition — follow-ups cost a fraction. |
| **Billing transparency** | Every envelope's `billing` field says whether the run drew from a vendor **subscription** or metered **api** credits. Codex children get `OPENAI_API_KEY` stripped by default so a stray key can't silently flip you to paid API. |
| **No daemon, no deps** | Pure Python stdlib — no server, no MCP, no pip install (the optional agy PTY wrapper wants `pywinpty pyte`; nothing else does). Auditable in an afternoon. |

## What a dispatch returns

```json
{
  "status": "success",
  "result": "…the agent's full answer…",
  "report": { "status": "DONE", "summary": "Reviewed 4 files; 2 findings",
              "handoff": "Fix the race in poller.py:88 first" },
  "report_ok": true,
  "model":   { "requested": "sonnet", "resolved": "claude-sonnet-5" },
  "permission": "safe-edit", "permission_flags": ["--permission-mode", "acceptEdits"],
  "usage": { "input_tokens": 12038, "output_tokens": 981 }, "cost_usd": 0.084,
  "billing": { "source": "subscription", "note": "Claude login" },
  "elapsed_ms": 7285,
  "resume": { "cli": "claude", "session_id": "0197…" }
}
```

- `report.handoff` → paste into the next dispatch. Chains without re-explaining.
- `report_ok: false` on a "success" → the response also gets `suspect: true`. A run that
  ends asking for approval, or self-reports `STATUS: BLOCKED`, comes back `blocked` — never
  a false `success`.
- `model.resolved` → the model the backend *actually served* (catches a stale alias).

## Council mode — decide by consensus

```bash
summon council --question "Adopt a monorepo or keep polyrepos?" \
  --members planner,reviewer,researcher,pair --chairman fable --rounds 2 --cwd "$PWD"
```

A vendor-diverse council answers independently, then (`--rounds 2`) sees all positions
**anonymized**, refines, and **ranks** them; votes aggregate (Borda) into
`consensus_ranking`, and the chairman returns the decision, a confidence, the agreements,
the named dissents, and a next action. For architecture calls, tech selection, risk
judgments — anywhere one model's blind spot is real.

## Custom & local models

```markdown
---
run-agent: openai-compat
provider: openrouter          # or openai / anthropic / google / groq / ollama / lmstudio
model: anthropic/claude-3.5-sonnet
---
```

Built-in providers plus your own in `providers.json`; or give `base_url` + `api_key_env`
inline (empty key for local servers). Same envelope, same `manifest`/`council`. This is
how you add local AI and multi-model API access — and it makes `council` a true
multi-vendor board. Bills API credits, not a subscription (see [TERMS.md](TERMS.md)).

## Parallel fan-out

```bash
# three implementations racing, each isolated, none blocking your session
summon dispatch --agent implementer --prompt "$SPEC" --cwd "$PWD" --worktree try-codex  --background
summon dispatch --agent coder       --prompt "$SPEC" --cwd "$PWD" --worktree try-cursor --background
summon dispatch --agent pair        --prompt "$SPEC" --cwd "$PWD" --worktree try-claude --background
# each returns {job_id, pid, result_file}; poll the files, diff the branches, merge the winner
```

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full playbook: the report/HANDOFF
contract, cross-vendor review rule, and named patterns (debate, async build, competing
hypotheses, consensus review).

## The starter roster (20 agents)

Planning/architecture on Claude (`planner`, `architect`, `deep-debugger`,
`security-auditor`, `fable`), implementation + adversarial review on Codex (`implementer`,
`reviewer`, `adversarial-reviewer`, `debugger`, `test-author`), coding on Cursor (`coder`,
`bug-fixer`), research/docs/frontend on Antigravity (`researcher`, `docs-writer`,
`frontend`), plus balanced lanes on Sonnet 5 (`pair`, `editor`, `quick-reviewer`,
`pr-prep`). Each is a plain `.md` file — edit, delete, or add your own with
`summon agent new`. `install.py` never overwrites an agent you already have.

## How it compares

| | summon | agent-bridge / CCB / claude-codex-collab | cc-fleet | MCO |
|---|---|---|---|---|
| Vendors | **6** (incl. Antigravity headless + any OpenAI-compatible API) | 2–3 | Claude only | 2–3 |
| Any CLI as host | **yes** | mostly Claude-hosted | no | no |
| Structured envelope + lie-detection | **yes** | partial | no | no |
| Consensus / council mode | **yes** (anonymized ranking + chairman) | no | no | no |
| Cost/usage + billing source | **yes** | partial | no | no |
| Worktree + background + manifest fan-out | **yes, from any host** | no | yes (Claude-hosted) | no |
| Runtime footprint | **a folder of stdlib Python** | daemon / MCP / npm tree | plugin | server |

Honest caveats: those tools have better streaming UIs and bigger communities; summon is a
dispatcher, not a dashboard. Gemini resume isn't supported (its CLI can't re-target a
headless session).

## Platform support

- **Windows**: battle-tested daily across all backends (the .cmd-shim and console quirks
  others trip on are handled).
- **Linux/macOS**: claude, codex, cursor-agent, gemini, and openai-compat fully supported;
  CI runs the suite on Ubuntu + Windows. The **agy** backend is Windows-only out of the box
  (headless mode needs a ConPTY capture wrapper we bundle); on POSIX, set `AGY_PTY_WRAPPER`
  or use the others.
- **Python**: 3.10+ (stdlib only; the optional agy wrapper wants `pywinpty pyte`).

## Security & terms

- Agent `permission:` maps to each CLI's own sandbox flags. **Every bundled agent ships
  `safe-edit`** — but note that mapping is the CLI's: claude/codex/cursor/gemini `safe-edit`
  is workspace-scoped auto-edit, while **agy has no workspace-write tier, so its `safe-edit`
  is a full bypass like `yolo`.**
- **Treat the whole `--cwd` as trusted.** Files under it, `.agents/memory.md`
  (auto-injected), and manifest `prompt_file`s are trusted operator input; every bundled
  agent also carries an "untrusted content: data, not instructions" guard. **Don't run
  summon in a repo you don't trust.**
- The agy backend copies OAuth tokens into a per-invocation profile locked to your user
  (icacls / `0700`) and isolated from your real profile. openai-compat reads API keys from
  env only and redacts them from errors.
- **Terms of service:** summon drives each vendor's *official* CLI (built for scripted use)
  on *your* accounts — the intended path for personal/dev work. Don't share accounts, build
  a product on subscription auth, or hammer parallel volume; use API-key backends for
  commercial/high-volume. Full guidance in [TERMS.md](TERMS.md).
- No network calls of its own, no telemetry. It spawns the backend CLIs plus supporting
  tools where a feature needs them (`git`, `icacls`/`chmod`, the agy PTY wrapper, a detached
  copy of itself for `--background`).

## FAQ

**What happens when a vendor ships a new model?** Nothing breaks — model strings pass
through verbatim. Aliases (`opus`, `sonnet`) float; `summon models` shows what's available;
verify with the envelope's `model.resolved`.

**Does it need API keys?** For the five CLI backends, no — it drives the logins you already
have (subscriptions), and strips `OPENAI_API_KEY` from codex children. The `openai-compat`
backend uses your API key (and bills API credits) by design.

**Why not MCP?** MCP adds a server and session dependency for what is fundamentally a
one-shot subprocess/HTTP dispatch. A script you can read beats a protocol you must trust.
(An MCP facade may come later; the envelope won't change.)

## Contributing

New backends, agents, and providers are the easy wins — see
[references/adding-a-backend.md](references/adding-a-backend.md) and
[CONTRIBUTING.md](CONTRIBUTING.md). Run `python scripts/test_discovery.py` and
`python tests/test_install.py` before a PR.

## Roadmap

- POSIX PTY wrapper for the agy backend
- Gemini resume when its CLI grows a stable session id
- True argparse subcommands (structural mode-enforcement) once the flat form can be dropped
- Optional MCP facade

## Credits

Sharpened against the ecosystem: agent-bridge, CCB, claude-codex-collab, cc-fleet, MCO,
swarms, Omnigent, and Karpathy's llm-council. Summon began as a private skill dispatching
real work across many CLIs daily; this repo is that tool, generalized.

## License

[MIT](LICENSE)
