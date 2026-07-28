# Model discovery & the bundled roster

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Model discovery (`--list-models`)

The skill never hardcodes a model allowlist — a `model:` string (frontmatter) or
`--model` (override) is passed through to the CLI verbatim, so **any model a backend
supports is invocable the moment it ships, with zero code changes.** How a *new* model
reaches an agent depends only on how that agent names its model:

| How the model is named | Example | When a new model ships |
|---|---|---|
| **Alias** (claude only) | `opus`, `sonnet` | Floats to whatever the CLI *currently maps the alias to* — but that mapping can **LAG the newest release** (the CLI vendor controls it). Verify, don't assume. |
| **Unpinned** | (no `model:`) | Floats with the CLI's own default (agy, gemini). |
| **CLI-config default** | codex | Uses `~/.codex/config.toml` `model`; move the default there or pass `--model`. |
| **Version ID** | `claude-sonnet-5`, `claude-fable-5` | **Frozen** — exactly this model until you bump the agent's `model:` (or `CURSOR_DEFAULT_MODEL` in `_builder.py`). |

> **Aliases lag — verify with `model.served`.** An alias resolves to whatever the CLI
> maps it to *today*, which is not always the newest model. Observed: `--model sonnet`
> resolved to `claude-sonnet-4-6` while `claude-sonnet-5` was already available. Every
> dispatch envelope reports `model.served` (the model that actually did the work, on
> evidence; `model.targeted` is what the session was pointed at, and `resolved` is the
> legacy field) — check it. For **guaranteed-latest**, pin the explicit version ID
> (`claude-sonnet-5`, `claude-opus-5`) and re-verify when a new model ships; for
> **auto-float-when-it-works**, use the alias but confirm `model.served` is what you
> expect. This roster pins EVERY claude agent to a full version id -- both aliases were
> observed lagging, so nothing here floats. A guard test binds this table to the
> agents' own frontmatter, because a hand-maintained roster drifts the moment a
> model ships (it had already drifted to `opus -> 4.8` while the agents were pinned
> to claude-opus-5).

`--list-models` answers "what can each backend run *right now*" live where the CLI
exposes it. Each entry is tagged with a `source` so you know how much to trust it:
- `live` — queried just now (`agy models` — the only backend with a real list)
- `config` — read from the CLI's own default config (`codex` → config.toml)
- `static` — documented aliases/defaults to pass via `--model` (CLI has no list)
- `unavailable` — a live query was attempted and failed (reason in `note`)

Discover with `--list-models`, invoke with `--model`, verify with `model.served` —
using a new model never requires editing the skill code itself.

**Models newer than this document almost certainly exist.** These docs are a snapshot;
model strings pass through to the CLIs verbatim, so you can — and should — try IDs
that postdate anything written here (a future `claude-sonnet-6`, a new codex id, a new
agy display name) without waiting for a skill update. Cheap probe: dispatch a trivial
prompt with the candidate `--model` and check the envelope's `model.served`; an
unsupported ID fails fast with the CLI's own error, costing nothing but the attempt.
Never assume an alias has caught up to a launch — probe or pin.

## Bundled roster snapshot (2026-07 — `--list` is the live truth)

The definitive list is always `--list` (definitions register/edit instantly, so the
roster may have changed since this table). Models below were verified actually
serving via `model.resolved` at snapshot time:

| Agents | Backend | Model (verified) | Use for |
|---|---|---|---|
| `planner`, `architect`, `deep-debugger`, `security-auditor` | claude | `claude-opus-5` (pinned) | planning, architecture, gnarly debugging, security audits |
| `fable` | claude | `claude-fable-5` | escalation tier: hardest problems, highest-stakes calls |
| `pair`, `editor`, `quick-reviewer`, `pr-prep` | claude | `claude-sonnet-5` | balanced general work, prose, fast reviews, PR prep |
| `reviewer`, `adversarial-reviewer`, `implementer`, `debugger`, `test-author` | codex | config default (gpt-5.6-sol at snapshot) | code review, adversarial passes, implementation, tests |
| `coder`, `bug-fixer` | cursor-agent | composer-2.5 | multi-step coding, bug fixing |
| `researcher`, `docs-writer`, `frontend`, `antigravity` | agy | Gemini default (pin via `model:`) | research, docs, frontend |

Cross-vendor routing rule of thumb: never have an agent's work reviewed by its own
vendor — send claude/cursor-written code to a codex reviewer and codex-written code to
a claude reviewer (see [docs/PROTOCOL.md](https://github.com/Nafjan/summon/blob/main/docs/PROTOCOL.md)).

## Cursor as a cross-vendor model gateway (with a Cursor subscription)

The `cursor-agent` backend is not limited to Composer. A Cursor subscription exposes a
large, multi-vendor model roster through the SAME CLI: GPT-5.x (including the codex,
sol, terra, and luna families), Claude (Opus 4.5-4.8, Sonnet 4-5, Fable 5), Gemini 3.x,
Grok 4.5, GLM 5.2, and Kimi K2.7. Query the live list with `cursor-agent models` (it
changes as Cursor adds models). Because summon passes `--model` through verbatim, any of
them is reachable through summon with no code change:

```
run_subagent.py --agent coder --cli cursor-agent --model <cursor-model-id> --prompt "..."
run_subagent.py --agent coder --cli cursor-agent --model gpt-5.6-sol-high --prompt "..."
```

Cursor's parameterized model syntax works too (the string is forwarded untouched):
`--model '<cursor-model-id>[context=1m,effort=high,fast=false]'`. The ids above are
SYNTAX illustrations, not a current roster -- summon cannot enumerate cursor's models
(`--list-models` reports `source: static` for it), so check Cursor's own model list.

**Why this matters:** for a user who already has a Cursor sub, the `cursor-agent` backend
is a practical **cross-vendor fallback**: a way to reach GPT, Claude, Gemini, Grok, and
more WITHOUT a separate API key or per-vendor subscription for each. It pairs well with
the fan-out/council modes when a specific vendor's CLI is unavailable or rate-limited: point
a member at `--cli cursor-agent --model <id>` instead.

**Billing.** These draw from the Cursor subscription's own included usage (metered in
dollars, but included in the plan, not a separate bill). A CLI-login cursor dispatch
reports `billing.source: "subscription"` in the envelope. Note Cursor flags some models
`NO ZDR` (e.g. Fable 5); mind data retention if that matters for your work. As always,
`cost_usd`/`usage` are the CLI's list-price estimates, not your actual Cursor invoice.

**Login is required: a logged-out `cursor-agent` fails dispatches.** If the CLI's auth
expires, every cursor dispatch errors until you re-authenticate. Check with `cursor-agent
status` and fix with `cursor-agent login`. The dispatch envelope surfaces this as an `auth`
diagnostic when the CLI emits a recognized phrase; `--doctor` also probes backend
eligibility. If cursor dispatches start failing for no obvious reason, verify the login
first.
