# Model discovery & the bundled roster

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Summon model labels

Summon keeps editorial ranking separate from service evidence. The machine-readable
catalog is [`model-catalog.json`](model-catalog.json); it supplies display metadata
for roster cards and tooltips, but it never authorizes a dispatch or proves that an
account can serve a model. The dispatch envelope's exact `model.served` value remains
the only service evidence.

The current editorial bands are:

| Band | Order | Models | Typical lane |
|---|---:|---|---|
| **Frontier** | 1–8 | Astra, Fable (current and previous), Sol, Opus, Kimi, DeepSeek V4 Pro, Gemini Flash 3.8 | escalation, architecture, synthesis, high-context research, deep reasoning, vision and frontend review |
| **Near-frontier** | 1–7 | Grok 4.6, Gemini Flash 3.7, GLM 5.2, DeepSeek V4 Flash, Luna 5.6, Terra 5.6, Spark 5.3 | fast evidence, coding second opinions, and cost-efficient secondary work |

The Fable entry was refreshed on 2026-09-02, Flash 3.8 added on 2026-09-03, and Astra added on 2026-09-05; other bands retain their previous
editorial assessment. These are Summon-curated labels, not benchmark, safety, cost,
availability, or vendor claims. A tooltip may show the role, model name, version,
recommendation lane, catalog status, and whether an exact served-model match was
observed. It must not show profiles, accounts, paths, prompts, credentials, or raw
provider output. Unknown models remain `unverified` rather than being guessed.

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
> legacy field) — check it. For an exact named-model seat, `resolved` is never treated as
> a substitute for provider-authored `served` evidence; a missing or mismatching terminal
> identity blocks the seat. For an explicit Codex pin, `resolved` is never filled from the
> ambient config default when the provider emits no identity; that default is not evidence
> about the turn. For **guaranteed-latest**, pin the explicit version ID
> (`claude-sonnet-5`, `claude-opus-5`) and re-verify when a new model ships; for
> **auto-float-when-it-works**, use the alias but confirm `model.served` is what you
> expect. This roster pins EVERY claude agent to a full version id -- both aliases were
> observed lagging, so nothing here floats. A guard test binds this table to the
> agents' own frontmatter, because a hand-maintained roster drifts the moment a
> model ships (it had already drifted to `opus -> 4.8` while the agents were pinned
> to claude-opus-5).

`--list-models` answers "what can each backend run *right now*" live where the CLI
exposes it. Add `--refresh` in the subcommand form (`summon models --refresh`) or
`--refresh-models` in the flat form when a provider roster may have changed. Each entry is
tagged with a `source` so you know how much to trust it:
- `live` — queried just now (`agy --output-format json models`, `opencode models`, or an
  explicit ArkCLI refresh; older AGY versions use one labeled legacy-text fallback)
- `cache` — read from a provider roster cache; refresh explicitly when it is stale
- `config` — read from the CLI's own default config (`codex` → config.toml)
- `static` — documented aliases/defaults to pass via `--model` (CLI has no list)
- `unavailable` — a live query was attempted and failed (reason in `note`)

ArkCLI's Coding Plan roster is live when the CLI exposes it and otherwise comes from its
bounded local cache. Use `summon models --cli arkcli --refresh` after `arkcli auth login` to
refresh it. Codex does not expose a complete enumeration command, so its config default and
catalog candidates are advisory. In every case, the dispatch envelope's exact
`model.served` field is the authority for what actually ran; a catalog label or candidate
never proves account eligibility.

OpenCode discovery delegates to `opencode models` and returns provider/model selectors such
as `openrouter/z-ai/glm-5.3-flash` when the local OpenCode configuration and credentials expose
them. The former `stealth/ox-alpha` preview was reported as corresponding to this paid
model. Its direct and tool-enabled aliases are retired and stale custom definitions are
refused before provider contact; do not assume the old alias, identity, or free pricing
persists. The GLM selector is a currently documented successor route, not a permanent
provider commitment. Model names and availability can change without a Summon release. A live
list proves only that OpenCode listed the selector; it does not prove that the
account can serve it or that a gateway policy will permit it. Confirm a real dispatch and
inspect `model.served` plus `served_model_evidence`.

OpenRouter aliases exposed by OpenCode commonly appear as
`openrouter/openrouter/auto`, `openrouter/openrouter/free`, and
`openrouter/openrouter/fusion`. `free` is random and `auto` is task-aware; both
must be audited from the provider's served model. Fusion presets are request
plugins and are available through the bounded `openrouter_options` field on an
OpenCode agent, not by inventing a model suffix. See
[backends.md](backends.md#openrouter-routers-through-opencode) for the exact
selectors and examples.

Discover with `--list-models`, invoke with `--model`, verify with `model.served` —
using a new model never requires editing the skill code itself.

**Models newer than this document almost certainly exist.** These docs are a snapshot;
model strings pass through to the CLIs verbatim, so you can — and should — try IDs
that postdate anything written here (a future `claude-sonnet-6`, a new codex id, a new
agy display name) without waiting for a skill update. Cheap probe: dispatch a trivial
prompt with the candidate `--model` and check the envelope's `model.served`; an
unsupported ID may fail before service, but a probe can still consume provider quota,
credits, or time. Treat it as an authorized provider call and inspect the receipt.
Never assume an alias has caught up to a launch — probe or pin.

## Bundled roster snapshot (2026-08)

`--list` reports the roster definitions visible to the selected Summon installation;
it does not prove provider availability, account eligibility, or the model ultimately
served. The entries below are editorial targets and snapshot observations, not current
service proofs.
At snapshot time, the strongest available evidence was `model.resolved` or
`model.served` where the backend emits it, plus an explicit Kimi stream target
selection and response for Kimi. Some Kimi CLI versions include a model
and usage on an assistant JSONL record; Summon exposes that bounded, child-observed source as
`model.evidence_source: kimi_assistant_record`. Kimi 0.38 instead writes
post-response `usage.record` accounting and `turn.ended:completed` into the fresh
isolated profile for that invocation. Summon accepts a single, positive-output,
completed, non-conflicting model from that runtime journal as
`kimi_wire_usage_record`. Both sources are labeled `served_model_evidence: inferred`
because the child can write them; neither can certify an exact named-model vote.
Request records, profile configuration, stale or linked
journals, mixed models, and incomplete turns never mint identity. Versions that
emit neither form still truthfully retain `model.served: null` and
`served_model_evidence: absent`.

| Agents | Backend | Model target / snapshot status | Use for |
|---|---|---|---|
| `astra` | codex | `gpt-6-astra` (pinned target; require reported service) | consequential architecture, planning, research synthesis, and adversarial review; read-only, high effort |
| `planner`, `architect`, `deep-debugger`, `security-auditor` | claude | `claude-opus-5` (pinned) | planning, architecture, gnarly debugging, security audits |
| `fable` | claude | `claude-fable-5-1` (target; verify service) | lead architecture/design/strategy, orchestration plans, final technical approval and escalation; read-only, high effort |
| `fable-api` | openai-compat | unavailable legacy route | retired: direct adapter lacks native Anthropic Messages support; explicitly select `fable` |
| `pair`, `editor`, `quick-reviewer`, `pr-prep` | claude | `claude-sonnet-5` | balanced general work, prose, fast reviews, PR prep |
| `reviewer`, `adversarial-reviewer`, `implementer`, `debugger`, `test-author` | codex | CLI config default (inspect `summon models --cli codex --refresh`) | code review, adversarial passes, implementation, tests |
| `sol-review` | codex | `gpt-5.6-sol` (pinned) | adversarial architecture and release review |
| `terra-review` | codex | `gpt-5.6-terra` (pinned candidate; require `model.served`) | balanced, cost-conscious review |
| `luna-review` | codex | `gpt-5.6-luna` (pinned candidate; require `model.served`) | cheap, high-reasoning secondary review |
| `luna` / explicit Codex candidate | codex | `gpt-5.6-luna` (config-observed; verify `model.served`) | cost-efficient, high-reasoning secondary lane |
| `terra` / explicit Codex candidate | codex | `gpt-5.6-terra` (declared, unverified) | balanced secondary lane; do not pin until served evidence |
| `spark` / explicit Codex candidate | codex | `gpt-5.3-spark` (declared, unverified) | fast/quota-isolated candidate; do not pin until served evidence |
| `coder`, `bug-fixer` | cursor-agent | composer-2.5 | multi-step coding, bug fixing |
| `kimi-worker` | kimi | `kimi-code/k3` + `effort: max` (pinned target; provider evidence may be absent) | maximum-thinking architecture, independent review, broad repository research, ambiguous multi-file work |
| `kimi-coder` | kimi | `kimi-code/k3` + `effort: max` (pinned target; provider evidence may be absent) | maximum-thinking scoped implementation, refactoring, debugging, focused verification |
| `kimi-k27-coder` | kimi | `kimi-code/kimi-for-coding` (explicit lower-context seat; provider evidence may be absent) | deliberate K2.7 implementation/debugging trade-off |
| `flash-reviewer` | agy | `gemini-3.8-flash-high` (advisory target, not certified identity) | vision, research, persona reviews, fast `/council` secondary |
| `researcher` | agy | `gemini-3.8-flash-high` (exact policy; absent served proof blocks) | exact-model research; AGY currently cannot satisfy its identity gate |
| `docs-writer`, `frontend`, `antigravity` | agy | `gemini-3.8-flash-high` (pinned target) | docs, frontend evaluation/design, scoped implementation |
| explicit ArkCLI Coding Plan seat | arkcli | refreshable plan roster (for example `glm-5-2-260617`) | fast value/coding-plan secondary; verify `model.served` |

Kimi's standard final-report contract is unchanged: `STATUS`, `SUMMARY`,
`FOLLOW-UP`, and `HANDOFF` are all required. A response containing only
`STATUS`, `VERDICT`, and `HANDOFF` is preserved and may be useful, but it is
`report_ok: false` and `suspect: true` because it omitted the required bookends.
Summon does not manufacture missing model evidence or silently relax that
contract.

The `astra` seat pins the exact OpenAI model instead of inheriting the Codex default.
It is the normal OpenAI ceiling for difficult planning and review, while Fable remains
available for an independent cross-vendor escalation or final decision. OpenAI describes
Astra as its most capable model and reports lower estimated cost per task than earlier
models despite higher per-token pricing; actual subscription usage and task cost remain
provider/account facts. A target or catalog entry does not certify a run—check the
provider-reported served identity. See the [official model guide](https://developers.openai.com/api/docs/guides/latest-model).

The five bundled Gemini seats are pinned rather than floating with AGY's default.
AGY exposes `gemini-3.8-flash-high`, `-medium`, and `-low`; an explicit effort override
can select another tier without changing the model generation. Older explicit pins
remain older pins, not aliases to 3.8. Listing is not proof of service or quota.

[Google's model card](https://deepmind.google/models/model-cards/gemini-3-8-flash/)
describes multimodal model capabilities, and the
[Antigravity model roster](https://antigravity.google/docs/models/) lists Flash 3.8.
Summon's transport must actually deliver an image/video before a seat can claim
inspection. Prefer native multimodal access when that delivery is unavailable;
frames or transcripts are partial substitutes and must be labeled as such.
Use Flash for research, persona reviews, and frontend evaluation/design; distinct
personas are not independent models. Coding is useful in a disposable clone/worktree,
with bounded checkpoints and local verification at the original dispatch directory.
AGY can drift, cannot enforce read-only, and currently lacks authoritative served-model
identity. Preserve those caveats rather than disabling useful advisory work or
counting it as a certified named-model vote. No default grants unlimited provider spend.
Use `flash-reviewer` for this advisory work. The existing `researcher` exact-model
contract remains unchanged: missing authoritative served identity means `blocked`
and `result_usable:false`, even when the provider completed a substantive report.

The named Codex review seats are pinned on purpose. A seat named for Sol or Terra
must not inherit the account's default model; if the provider cannot serve the
requested target, the dispatch is a routing failure and the envelope must be treated
as such. `summon --list --json` also exposes each seat's declared `model` and
`effort`, while a completed dispatch remains the only proof of `model.served`.

Luna is a separate, intentionally selectable cost-efficient lane. “Luna Max” refers
to the `gpt-5.6-luna` model at the provider's maximum reasoning setting, not a second
model ID. Summon's portable Codex effort mapping currently clamps `max` to `high`,
so a seat must not advertise Max unless its receipt proves the provider setting that
was applied. Keep Luna available as a distinct seat rather than using it as a silent
fallback for Sol or Terra.

Cross-vendor routing rule of thumb: never have an agent's work reviewed by its own
vendor — send claude/cursor-written code to a codex reviewer and codex-written code to
a claude reviewer (see [docs/PROTOCOL.md](https://github.com/Nafjan/summon/blob/main/docs/PROTOCOL.md)).

## Cursor as a cross-vendor model gateway (with a Cursor subscription)

The `cursor-agent` backend is not limited to Composer. Depending on the plan, account,
CLI version, and current vendor policy, a Cursor subscription may expose a
multi-vendor model roster through the same CLI, including examples such as GPT-5.x,
Claude, Gemini, Grok, GLM, and Kimi. Cursor's model page is a useful reference, not
proof of a local account's entitlement. When a CLI exposes no machine-readable model
list, treat the exact CLI slug and account eligibility as live
facts: pass the candidate through, then require `model.served` to match before ranking
it or adding it to a pinned agent:

```
run_subagent.py --agent coder --cli cursor-agent --model <cursor-model-id> --prompt "..."
run_subagent.py --agent coder --cli cursor-agent --model gpt-5.6-sol-high --prompt "..."
# Candidate probe; do not assume this slug or silently fall back:
run_subagent.py --agent coder --cli cursor-agent --model grok-4.6 --prompt "..."
```

**Grok 4.6 lane (candidate secondary).** Cursor describes Grok 4.6 as a high-capability,
near-frontier model
for long-horizon coding and knowledge work. That makes it a strong candidate for a
second-opinion/coding seat, not an automatic replacement for the pinned Gemini Flash
3.7 evidence lane: Cursor's transport has different billing, retention, permission, and
read-only guarantees, and a local dispatch must prove `model.served`, `status`, and the
expected permission envelope first. Summon does **not** pin Grok 4.6 without a
verified exact-model receipt.

Cursor's parameterized model syntax works too (the string is forwarded untouched):
`--model '<cursor-model-id>[context=1m,effort=high,fast=false]'`. That `[effort=…]`
is **Cursor's** knob inside the model id — Summon's separate `--effort` flag does
**not** rewrite cursor-agent dispatches (see [effort.md](effort.md)). The ids above are
SYNTAX illustrations, not a current roster -- summon cannot enumerate cursor's models
(`--list-models` reports `source: static` for it), so check Cursor's own model list.

**Why this matters:** for a user who already has an eligible Cursor account, the
`cursor-agent` backend may provide a practical **cross-vendor route** without a separate
API key for each model. It does not guarantee access without another subscription, and
it does not bypass provider terms, quotas, retention, or model availability. It pairs
well with fan-out/council modes when the local Cursor roster exposes the requested model:
point a member at `--cli cursor-agent --model <id>` and verify the receipt.

**Billing.** Summon may infer a subscription-backed Cursor route from local routing and
environment evidence and classify it as `billing.source: "subscription"`; that field is advisory and
does not establish remaining allowance, included usage, credits, or invoice treatment.
Provider plan and account terms are authoritative. Note Cursor flags some models
`NO ZDR` (for example, Fable 5); mind data retention if that matters for your work.
As always, `cost_usd`/`usage` are the CLI's list-price estimates, not your actual
Cursor invoice.

**Login is required: a logged-out `cursor-agent` fails dispatches.** If the CLI's auth
expires, every cursor dispatch errors until you re-authenticate. Check with `cursor-agent
status` and fix with `cursor-agent login`. The dispatch envelope surfaces this as an `auth`
diagnostic when the CLI emits a recognized phrase; `--doctor` also probes backend
eligibility. If cursor dispatches start failing for no obvious reason, verify the login
first.
