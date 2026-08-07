# Custom & API backends (openai-compat)

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Custom & API backends (`openai-compat`) — add any model

Beyond the five CLIs, an agent can run against **any OpenAI-compatible
`/chat/completions` API** — OpenRouter, OpenAI, Anthropic, Google (Gemini compat),
Groq, DeepSeek, Together, or a LOCAL server (Ollama, LM Studio, vLLM, llama.cpp).
Pure stdlib HTTP, no SDK. This bills your **API key/credits**, not a subscription
(cleaner for commercial/high-volume — see [TERMS.md](TERMS.md)).

```markdown
---
run-agent: openai-compat
provider: openrouter                 # or: openai / anthropic / google / groq / ollama / lmstudio / <your provider>
model: anthropic/claude-sonnet-5   # the API's model id
---
```
or point anywhere directly (no provider needed):
```markdown
---
run-agent: openai-compat
base_url: http://localhost:11434/v1  # local Ollama
api_key_env: ""                       # empty = no auth header
model: llama3.1
---
```

**Providers** resolve from built-ins + an optional `providers.json` in the agents
dir (or `~/.agents/providers.json`) — `{ "myprov": {"base_url": "...", "api_key_env":
"MY_KEY"} }` (see `providers.json.example`). The API key is read from the named env
var at dispatch (never stored). Everything else is identical: same envelope, same
`--manifest`/`--council`/`--json-schema`. Create these agents by hand or with
`--new-agent NAME --set run-agent=openai-compat --set model=...`. Resume isn't
supported (the API call is stateless). This is how you add local AI and multi-model
API access — and it makes `--council` a true multi-vendor board (à la OpenRouter).

---

## BytePlus ModelArk Coding Plan

BytePlus ModelArk offers a **Coding Plan** subscription that bundles a roster of
text-only coding models under a flat monthly fee (no per-token billing). Summon
ships a built-in `byteplus-coding` provider so you can use it with zero config
beyond an env var.

### Quick start

```markdown
---
run-agent: openai-compat
provider: byteplus-coding
model: deepseek-v4-pro
---
```

Set `BYTEPLUS_CODING_API_KEY` in your environment (the profile API key from
`arkcli auth status`, not the short-lived SSO id_token).

### Coding Plan vs PAYG — the `/api/coding/` trap

| Path | Billing |
|------|---------|
| `.../api/coding/v3` | **Coding Plan** subscription quota |
| `.../api/v3` (no `/coding/`) | Pay-as-you-go, per-token — bypasses plan |

The built-in provider hardcodes the correct Coding Plan endpoint. Summon
**refuses** to dispatch a `byteplus-coding` provider whose `base_url` doesn't
contain `/api/coding/` — this prevents silent PAYG billing.

### Model selection

The plan roster is an **eligibility catalog**, not a guarantee that every listed
model currently works through every API surface. Prefer models by task:

| Use case | Preferred model | Why |
|----------|-----------------|-----|
| Fast terminal loops, high-throughput fan-out | `deepseek-v4-flash` | Best default when latency and throughput matter |
| Hard coding, scoped implementation and PR work | `deepseek-v4-pro` | Stronger quality-oriented coding choice |
| Large repositories and long-horizon agent work | `glm-5.2` | Current GLM flagship with long context |
| Agentic coding specialist | `dola-seed-2.0-code` | Coding-specialized Seed 2.0 variant |
| Code/UI generation alternative | `kimi-k2.5` | Useful alternative when the primary choices underperform |
| General reasoning, not coding-specialized | `dola-seed-2.0-pro` / `dola-seed-2.0-lite` | Use only when a general model fits better |

**Strongly avoid legacy or superseded roster entries for new work:**

- `glm-5.1` — superseded by `glm-5.2`.
- `bytedance-seed-code` — older Seed 1.6 code preview; listed in the roster but
  rejected by live Coding Plan probes.
- `gpt-oss-120b` — older general model; listed in the roster but failed live
  Coding Plan chat/API probes.

Do not silently delete a temporarily broken model from the documented roster.
Keep it marked with its observed status so it can be re-tested after vendor
changes.

> **Note:** `auto` routing is an `arkcli` feature and is **not** a valid Chat
> Completions model id. Summon refuses `model: auto` against `/api/coding/`
> before the HTTP call (upstream otherwise returns a misleading
> `UnsupportedModel` / "does not support the coding plan" error). Always pin a
> concrete model ID.

#### Roster freshness and availability checks

The static recommendations above were last manually checked on **2026-08-07**.
Summon also caches a live roster from arkcli at `~/.agents/byteplus-coding-roster.json`
(14-day freshness). Refresh anytime:

```powershell
arkcli auth status --format json
arkcli plans model-list --plan coding-plan --format json
python -c "import sys; sys.path.insert(0, r'<summon>/skills/summon/scripts'); from _apibackend import refresh_coding_plan_roster; print(refresh_coding_plan_roster())"
```

Use `plans model-list`, not `arkcli models list`; the latter is the full
marketplace and includes non-plan and multimodal models. After refreshing the
roster, make one minimal **text-only** call before changing recommendations.
Treat these as separate facts:

1. **Listed** — the plan catalog includes the model.
2. **Invocable** — a live Coding Plan text call succeeds.
3. **Recommended** — it is current and strong for a documented use case.

Only promote a model after all three are true. If a previously broken model
starts working, update its status and the last-checked date rather than assuming
that roster presence alone fixed it.

### OpenCode model sync

Prefer `arkcli helper` over hand-editing OpenCode's model catalog:

```powershell
arkcli helper list
arkcli helper configure opencode --profile coding-plan_ap-southeast-1_personal --model deepseek-v4-pro
```

This writes the Coding Plan provider + model into OpenCode. Reset with
`arkcli helper reset opencode` if you need to undo only arkcli-managed bits.

### Text-only constraint

All Coding Plan models are text-only. Do **not** send image/vision input — the
request will hard-fail with `Model do not support image input`. In Cursor, never
set a BytePlus Coding Plan model as the default Agent model (Cursor sends
screenshots as part of context).

### Region override

The built-in defaults to `ap-southeast` (BytePlus international). For other
regions, add a `byteplus-coding` override in `providers.json`:

```json
{
  "byteplus-coding": {
    "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "api_key_env": "BYTEPLUS_CODING_API_KEY"
  }
}
```

### `arkcli helper` vs `+connect`

| Command | What it does |
|---------|-------------|
| `arkcli helper configure <harness>` | Writes model/provider config for supported CLIs (Claude Code, Codex, OpenCode, Hermes) |
| `arkcli +connect` | Installs `arkcli-*` **documentation skills** into agents — not provider wiring |

For Summon, you only need the built-in provider + env var. `arkcli helper` is for
wiring other CLIs; `+connect` installs informational skills and is entirely
optional (can pollute agent skill pickers if run broadly).

### Billing note

When the resolved `base_url` contains `/api/coding/`, Summon reports
`billing.source: subscription` with a note that spend draws from Coding Plan
**subscription quota**, not per-token API credits. Consent-gated PAYG fallback
overrides this to `source: api` with an explicit PAYG note.

Generic `infer_billing("openai-compat")` remains `api` for other providers;
Coding Plan overlays the subscription source on the response envelope.

### PAYG fallback (consent-gated)

When a Coding Plan dispatch fails with a **quota/rate/plan-limit** or
**UnsupportedModel** error (model not on plan), Summon can automatically retry
once against the PAYG endpoint (`/api/v3`) using the same key. This retry only
fires when you've explicitly consented to PAYG billing through one of:

| Surface | Scope |
|---------|-------|
| `--allow-payg` flag | Single dispatch only (rejected for `--manifest`/`--council`). Propagates to `--background` children via argv; does **not** mutate the durable env var |
| Agent frontmatter `allow_payg: true` | Per-agent durable consent (also applies under fan-out; prefer env/prefs for intentional fleet-wide PAYG) |
| `SUMMON_ALLOW_BYTEPLUS_PAYG=1` env | Process / session (durable consent surface) |
| `~/.agents/summon.json` `{"allow_byteplus_payg": true}` | Persistent (all sessions) |

Without consent, the error message tells you how to enable it. The retry is
**never** attempted for auth failures (401/403), network errors, timeouts, or
generic 5xx.

When the PAYG retry succeeds, the envelope includes:

- `billing.note`: identifies the charge as PAYG per-token credits
- `fallback.from`/`.to`/`.reason`/`.primary_error`: full telemetry of what
  triggered the fallback

For **fan-out** (`--manifest`, `--council`): use the env var or the preference
file. The per-dispatch `--allow-payg` flag is deliberately rejected in these
modes so operators cannot accidentally authorize N parallel PAYG calls.
