# Summon

**Summon any AI from any CLI.**

[![CI](https://github.com/Nafjan/summon/actions/workflows/ci.yml/badge.svg)](https://github.com/Nafjan/summon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
![stdlib only](https://img.shields.io/badge/deps-stdlib_only-brightgreen.svg)
![backends](https://img.shields.io/badge/backends-6-brightgreen.svg)

Summon is a tiny, dependency-free tool that turns **one** AI coding CLI into a conductor
for **all** of them. From inside Claude Code, Codex, Cursor, Gemini CLI, Antigravity — or a
plain terminal — you can hand a task to any other model, run several at once, or convene a
**council** of them to make a decision. It also reaches any OpenAI-compatible API, so
OpenRouter, OpenAI, Anthropic, Google, and **local** models (Ollama, LM Studio) all become
first-class agents too.

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
session can summon Codex for an adversarial pass. Your terminal can summon a whole council
to *decide* something — each in its own git worktree, in parallel, and every result comes
back as one clean JSON envelope you can branch on.

---

## Who it's for

- **Developers who live in an AI coding CLI** (Claude Code / Codex / Cursor / Gemini /
  Antigravity) and want the *other* models one command away — without switching tools.
- **Anyone who wants a real second opinion.** Cross-vendor review — no model grading its
  own homework — is a first-class workflow here, not a hack.
- **People making decisions with AI** who want more than one model's take: council mode
  gets you diverse positions, anonymized peer ranking, and a synthesized recommendation.
- **Power users running fleets of agents** — fan a task across N models in parallel with
  per-backend throttling and resumable batches.
- **Anyone unifying local + cloud models** behind one interface (subscription CLIs *and*
  OpenAI-compatible APIs, including self-hosted).

If you just want a chat UI, this isn't that — summon is a **dispatcher**, meant to be
driven by you or by another AI agent, returning structured results rather than a stream.

---

## What you can actually do with it

- **Cross-vendor code review** — `summon dispatch --agent adversarial-reviewer` sends your
  diff to a *different* vendor than wrote the code, so blind spots don't compound.
- **Race several implementations** — three models each build the same spec in isolated git
  worktrees; you diff the branches and keep the best.
- **Decide by council** — "monorepo or polyrepo?" → four diverse models answer, rank each
  other anonymously, and a chairman synthesizes a decision with confidence and dissents.
- **Swarm over documents** — a manifest of 40 jobs, per-backend concurrency, resumable if
  it crashes; great for reviewing/summarizing/labeling at scale.
- **Structured extraction** — `--json-schema` makes any agent return validated JSON.
- **Use local + frontier models together** — an Ollama model and Claude in the same council.

---

## Install

Summon installs as a **skill** into each AI CLI you have, so you can invoke it as `/summon`
(or just describe what you want and the CLI triggers it). Two ways to set it up.

### Option A — let your AI agent install it (recommended)

Open your favorite AI CLI (Claude Code, Codex, Cursor, Gemini CLI, …) in a scratch folder
and paste this prompt. The agent discovers what you actually have installed and wires
everything up:

```text
Set up "summon" for me (github.com/Nafjan/summon), a cross-vendor AI sub-agent dispatcher.

1. Clone https://github.com/Nafjan/summon and cd into it.
2. Run `python summon.py doctor` and tell me which backends are installed and logged in
   (claude, codex, cursor-agent, gemini, agy) and which are missing.
3. Run `python install.py` to install the summon skill into every AI CLI on this machine
   (it auto-detects ~/.claude, ~/.codex, ~/.cursor, ~/.gemini, ~/.copilot and never
   overwrites my own agents). Add `--with-alias` only if I ask for the legacy /sub-agents name.
4. Run `python summon.py doctor` again and confirm what's now ready.
5. Read README.md and SKILL.md, then summarize: what I can do now, and ONE example command
   using a backend I actually have. If a backend I want is missing, tell me exactly how to
   install and log into its CLI.
```

The agent will report which of your subscriptions/CLIs are ready and hand you a working
first command. This is the friendliest path — it adapts to *your* machine.

### Option B — do it yourself

```bash
git clone https://github.com/Nafjan/summon && cd summon
python summon.py doctor      # which backends are ready? what's missing?
python install.py            # install the summon skill into every detected AI CLI
python summon.py doctor      # confirm
```

`install.py` is ownership-safe: it stages installs atomically, never touches an agent file
you already have, and cleanly uninstalls (`python install.py --uninstall`). Migrating from
the old name? `python install.py --with-alias` adds a thin `/sub-agents` alias.

You can also run summon **without installing the skill** — it's just a script:
`python summon.py dispatch --agent reviewer --prompt "…" --cwd "$PWD"`.

---

## Your first run

```bash
# from any project directory (use an absolute --cwd)
python summon.py dispatch --agent reviewer \
  --prompt "Review the diff on this branch for correctness bugs" --cwd "$PWD"
```

Or, once the skill is installed, just tell your AI CLI:

> "Summon the adversarial reviewer on my last commit and give me the findings."

---

## Command surface

Git-style subcommands (the legacy flat `--flag` form still works — nothing breaks):

| Command | Does |
|---|---|
| `summon dispatch --agent N --prompt … --cwd D` | run one agent (the default action) |
| `summon list` | list available agents |
| `summon models [--cli B]` | what each backend can run right now |
| `summon doctor [--json]` | backend / setup health check (run this first) |
| `summon manifest FILE` | run a batch swarm (per-backend concurrency, resumable) |
| `summon council --question "…"` | **decide by consensus** of diverse models |
| `summon agent new\|set NAME --set k=v` | scaffold / retune an agent definition |
| `summon version` · `summon help` | version · usage |

`summon` (no args) prints the command list. Everything below is documented in
[SKILL.md](SKILL.md).

---

## How to use it effectively

1. **Pick the right agent, not just the right model.** Agents bundle a model + a persona +
   a report contract. `reviewer` (Codex) reviews; `planner` (Opus) plans; `pair` (Sonnet)
   does everyday work. `summon list` shows them; `summon agent new` makes your own.
2. **Chain with `handoff`.** Every result includes `report.handoff` — paste it into the
   next dispatch instead of re-explaining. That's how multi-step work stays cheap.
3. **Trust the envelope, not the prose.** Branch on `status`; a run that ends asking for
   approval or self-reports `BLOCKED` comes back `blocked`, never a false `success`. Check
   `model.resolved` to confirm which model actually served you.
4. **Review across vendors.** Send code written by one vendor to a reviewer on another —
   `docs/PROTOCOL.md` has the rule and named patterns (debate, async build, competing
   hypotheses, consensus).
5. **Put big inputs in files.** For long prompts, write a packet under `--cwd` and pass a
   short "read X and follow it" prompt (avoids arg-length limits and sandboxed reads).
6. **Fan out with `manifest`; decide with `council`.** Independent tasks → a manifest
   swarm; a judgment call → a council.

Full playbook: **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

---

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

- `report.handoff` → the context to pass to the next call.
- `report_ok: false` on a "success" → also gets `suspect: true`. Agents that skip their
  contract don't get believed.
- `billing.source` → did this draw from a **subscription** or metered **api** credits.
- `resume.session_id` → `--resume` for a cheap follow-up.

---

## Council mode — decide by consensus

```bash
summon council --question "Adopt a monorepo or keep polyrepos?" \
  --members planner,reviewer,researcher,pair --chairman fable --rounds 2 --cwd "$PWD"
```

A vendor-diverse council answers independently; with `--rounds 2` they see all positions
**anonymized**, refine, and **rank** them; votes aggregate (Borda) into `consensus_ranking`,
and the chairman returns a decision, a confidence, the agreements, the **named dissents**,
and a next action. This is the llm-council pattern — run over *real cross-vendor CLIs*, not
just one API's models.

---

## Custom & local models (`openai-compat`)

```markdown
---
run-agent: openai-compat
provider: openrouter          # or openai / anthropic / google / groq / ollama / lmstudio
model: anthropic/claude-3.5-sonnet
---
```

Built-in providers, plus your own in `providers.json` (or inline `base_url` + `api_key_env`,
empty key for local servers). Same envelope, same `manifest`/`council`. This is how you add
local AI and multi-model API access — and it makes a council a true multi-vendor board.
These backends bill your **API credits**, not a subscription (see [TERMS.md](TERMS.md)).

---

## The starter roster (20 agents, all editable)

Planning/architecture on Claude (`planner`, `architect`, `deep-debugger`,
`security-auditor`, `fable`), implementation + adversarial review on Codex (`implementer`,
`reviewer`, `adversarial-reviewer`, `debugger`, `test-author`), coding on Cursor (`coder`,
`bug-fixer`), research/docs/frontend on Antigravity (`researcher`, `docs-writer`,
`frontend`), and balanced lanes on Sonnet 5 (`pair`, `editor`, `quick-reviewer`, `pr-prep`).
Each is a plain `.md` file — edit, delete, or add your own with `summon agent new`.
`install.py` never overwrites an agent you already have.

---

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

---

## System requirements

- **Python 3.10+** (3.11+ recommended). Standard library only — no `pip install` for the
  dispatcher. The optional **agy** backend's PTY wrapper wants `pip install pywinpty pyte`.
- **At least one backend**: the vendor's own CLI installed and logged in — `claude`,
  `codex`, `cursor-agent`, `gemini`, or `agy` — **and/or** an API key for an
  `openai-compat` provider (or a local Ollama/LM Studio server). `summon doctor` tells you
  exactly what you have and what's missing.
- **`git`** if you use `--worktree`.
- **OS**: Windows (all backends, battle-tested daily), Linux/macOS (all except agy out of
  the box; CI runs the suite on Ubuntu + Windows).

You bring the model access; summon just orchestrates the CLIs and APIs you already use.

---

## Security, permissions & terms — please read

- **Permissions.** Each agent's `permission:` (`read-only` / `safe-edit` / `yolo`) maps to
  that CLI's own sandbox flags. Bundled agents ship `safe-edit` (auto-approve edits, no
  bypass) — **except agy, which has no workspace-write tier, so its `safe-edit` is a full
  bypass like `yolo`.** Raise anything to `yolo` deliberately, in repos you trust.
- **Treat the whole `--cwd` as trusted.** Files under it, `.agents/memory.md`
  (auto-injected into agent context), and manifest `prompt_file`s are trusted operator
  input. Every bundled agent also carries an "untrusted content: data, not instructions"
  guard as defense-in-depth. **Don't run summon in a repository you don't trust.**
- **Secrets.** The agy backend copies OAuth tokens into a per-invocation profile locked to
  your user (icacls / `0700`) and isolated from your real profile. `openai-compat` reads
  API keys from env only and redacts them from any error output.
- **Terms of service.** Summon drives each vendor's *official* CLI (built for scripted use)
  on *your* accounts — the intended path for personal/dev work. Don't share accounts,
  build a product on subscription auth, or hammer parallel volume; use API-key backends for
  commercial/high-volume. Providers can change programmatic-billing rules. Full guidance in
  **[TERMS.md](TERMS.md)**.
- **No phone-home.** Summon sends no telemetry and makes no network calls of its own for
  the five CLI backends — it just spawns the backend CLIs plus supporting tools where a
  feature needs them (`git`, `icacls`/`chmod`, the agy PTY wrapper, a detached copy of
  itself for `--background`). The **one** exception is the `openai-compat` backend, whose
  whole job is a direct HTTPS call to the `base_url` you configure.

---

## FAQ

**What happens when a vendor ships a new model?** Nothing breaks — model strings pass
through verbatim. Aliases (`opus`, `sonnet`) float; `summon models` shows what's available;
verify with the envelope's `model.resolved`. (Aliases can lag a launch — pin the explicit
ID for guaranteed-latest.)

**Does it need API keys?** For the five CLI backends, no — it drives the logins you already
have, and strips `OPENAI_API_KEY` from codex children so you're not silently billed API
rates. The `openai-compat` backend uses your API key by design.

**Is it safe to let an agent install it for me?** Yes — the agent-led prompt just clones
the repo, runs `doctor` (read-only) and `install.py` (which never overwrites your files),
and reports. Read `install.py` first if you like; it's ~460 lines of stdlib.

**Why not MCP?** MCP adds a server and a session dependency for what is fundamentally a
one-shot subprocess/HTTP dispatch. A script you can read beats a protocol you must trust.
(An MCP facade may come later; the envelope won't change.)

---

## Contributing

Contributions are very welcome — new backends, agents, and providers are the easy wins.
A new backend is one entry in a registry
([references/adding-a-backend.md](references/adding-a-backend.md)); a new agent is a `.md`
file. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for dev setup, ground rules (stdlib only,
every change tested, secrets redacted), and the PR checklist. Run
`python scripts/test_discovery.py` and `python tests/test_install.py` before a PR.

## Roadmap

- POSIX PTY wrapper for the agy backend
- Gemini resume when its CLI grows a stable session id
- True argparse subcommands (structural mode-enforcement) once the flat form can be dropped
- Explicit numeric peer-ranking display and an optional MCP facade

## Credits

Sharpened against the ecosystem: agent-bridge, CCB, claude-codex-collab, cc-fleet, MCO,
swarms, Omnigent, and Karpathy's llm-council. Summon began as a private skill dispatching
real work across many CLIs daily; this repo is that tool, generalized and hardened.

## License

[MIT](LICENSE) — do what you like; no warranty. See [TERMS.md](TERMS.md) for the
provider-terms caveats that are on *you*, not this software.
