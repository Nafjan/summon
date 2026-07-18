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
> (`claude-sonnet-5`, `claude-opus-4-8`) and re-verify when a new model ships; for
> **auto-float-when-it-works**, use the alias but confirm `model.served` is what you
> expect. This roster pins Sonnet explicitly (its alias lagged) and leaves `opus` as an
> alias (verified serving 4.8).

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
| `planner`, `architect`, `deep-debugger`, `security-auditor` | claude | `opus` → claude-opus-4-8 | planning, architecture, gnarly debugging, security audits |
| `fable` | claude | `claude-fable-5` | escalation tier: hardest problems, highest-stakes calls |
| `pair`, `editor`, `quick-reviewer`, `pr-prep` | claude | `claude-sonnet-5` | balanced general work, prose, fast reviews, PR prep |
| `reviewer`, `adversarial-reviewer`, `implementer`, `debugger`, `test-author` | codex | config default (gpt-5.6-sol at snapshot) | code review, adversarial passes, implementation, tests |
| `coder`, `bug-fixer` | cursor-agent | composer-2.5 | multi-step coding, bug fixing |
| `researcher`, `docs-writer`, `frontend`, `antigravity` | agy | Gemini default (pin via `model:`) | research, docs, frontend |

Cross-vendor routing rule of thumb: never have an agent's work reviewed by its own
vendor — send claude/cursor-written code to a codex reviewer and codex-written code to
a claude reviewer (see [docs/PROTOCOL.md](https://github.com/Nafjan/summon/blob/main/docs/PROTOCOL.md)).
