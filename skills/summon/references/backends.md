# Custom, CLI, and API backends

> Part of the **summon** skill. See the main SKILL.md for core usage.

## OpenCode CLI gateway — toolful access to compatible providers

OpenCode can act as a local gateway for providers it knows how to call. This
is different from Summon's direct `openai-compat` seat: OpenCode runs its own
agent/tool loop, so an OpenCode-backed seat can inspect and edit the workspace
when its Summon permission tier allows it. The selected model still needs to
support the tool-calling features that the task requires; OpenRouter maintains
a [tool-support model filter](https://openrouter.ai/docs/guides/features/tool-calling).

The retired compatibility-named Ox seat remains pinned to its historical selector.
Its distinct paid successor targets the model that the preview was revealed to be,
GLM 5.3 Flash, through OpenRouter:

```markdown
---
run-agent: opencode
provider: openrouter
model: openrouter/z-ai/glm-5.3-flash
permission: safe-edit
---
```

The equivalent command is `opencode run --format json --model
openrouter/z-ai/glm-5.3-flash "…"`. Summon supplies the working directory,
strips agent arguments that could change the model or directory, and maps its
permission tiers to OpenCode's `OPENCODE_PERMISSION` policy. Read-only and
safe-edit also pass `--auto`, but only with an explicit deny-by-default policy;
OpenCode's explicit denies still win, so this does not broaden the tier. A local
`OPENROUTER_API_KEY` takes
precedence; on Windows, Summon may bridge the private `summonOpenRouter`
Credential Manager entry into this child process for an OpenRouter model. The
secret is never written to the agent definition, command line, receipt,
telemetry, or debug file.

OpenCode's assistant `providerID`/`modelID` fields describe the model selected
before the provider call. They are target evidence, not an authoritative copy of
the provider response's `model` field. Current OpenCode streams can therefore
complete a useful Ox turn while an exact-model Summon seat remains
`served_model_unverified`. Do not promote those fields or retry an exact
governance seat merely because the session export repeats them. Use a direct
`openai-compat` OpenRouter/Nous text seat when provider-reported model identity is
mandatory, or treat the toolful OpenCode result as advisory until OpenCode exposes
the response model in its event contract.

On Windows, that restricted policy also denies an external working-directory
volume. Summon detects this in `dispatch --dry-run` and `doctor --cwd` before a
turn starts. It returns a machine-actionable `allowed_root` on the local
temporary volume plus `copy_sanitized_packet_and_refreeze`. Copy only the
review packet there, re-freeze any packet hashes after the copy, and dry-run
again. Do not switch a restricted seat to `yolo` merely to bypass this boundary on
a shared, sensitive, or live checkout. If the task is in a disposable clone/worktree,
`yolo` is the intended broad-authority OpenCode/Ox mode: it enables the complete tool
loop, but the caller must inspect `workspace_evidence`, the diff, tests, and cleanup
before integrating anything. A Git worktree is not an OS security boundary; use a
separate clone/Git directory, account, container, or VM when the child must not reach
shared Git metadata, credentials, private data, or live resources.

OpenCode `yolo` is therefore an explicit isolated lane: Summon requires
`--worktree` or `--isolated-lane` before it will launch broad authority. If a private
OpenRouter or Nous key must be bridged into that unrestricted child, also pass
`--isolated-lane` and `--allow-tool-credentials` and use a separate clone/Git directory,
account, container, or VM. A `--worktree` may add mutation isolation but never replaces
the explicit OS-boundary acknowledgement. Without both explicit consents, Summon scrubs
inherited provider variables and fails closed instead of handing a key to arbitrary shell
tools. A worktree is mutation isolation, not credential or OS containment.

If a headless turn emits `step_finish` with `reason: unknown`, zero tokens, and no
text, Summon rejects the empty completion and records
`opencode_diagnostic=unknown_finish_zero_tokens`. This is a provider/model
no-output symptom; `--auto` only answers non-denied permission requests and is not
the source of model output.

Summon also starts the child with OpenCode's project-discovery and external-code
guards: the documented `--pure` flag plus `OPENCODE_DISABLE_PROJECT_CONFIG=1`, `OPENCODE_PURE=1`,
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, and `OPENCODE_DISABLE_CLAUDE_CODE=1`.
This prevents a repository's `opencode.json`, `.opencode` plugins, or external
skill files from changing the provider or observing a bridged credential before
the turn starts. Keep these guards intact for headless dispatches; update an
older OpenCode installation if it does not support the documented flags.

Authenticate OpenCode with its provider flow (`opencode auth login` or
`/connect`), configure the provider in `opencode.json`, and verify with
`opencode models` before dispatching. OpenCode's official documentation covers
[providers](https://opencode.ai/docs/providers/), the
[CLI](https://opencode.ai/docs/cli/), and
[permissions](https://opencode.ai/docs/permissions/).

This gateway removes the *direct-seat* text-only limitation. It does not remove
model or service limits: the
provider's context window and output cap still apply, OpenCode may compact long
sessions, and operating-system/CLI transport limits still apply to the initial
prompt. For large inputs, put files under `--cwd` and ask the agent to read
them; do not paste an unbounded document into the prompt. Check
`model.served` in the Summon envelope; a requested model or OpenCode roster
entry is not provider-authored proof.

### GLM-5.3 Flash routes and native ZCode

Summon keeps the ended Ox Alpha preview identity retired. It does not relabel
historical Ox receipts. The distinct active GLM-5.3 Flash routes are optional:

| Route | Selector | Tools/files | Exact-model evidence |
|---|---|---|---|
| OpenRouter through OpenCode | `openrouter/z-ai/glm-5.3-flash` | Yes, in an isolated lane | OpenCode selection is advisory unless a provider response reports the model |
| Z.AI Coding Plan through OpenCode | `zai-coding-plan/glm-5.3-flash` | Yes, in an isolated lane | Same OpenCode provenance limitation |
| Direct Z.AI Coding Plan | `glm-5.3-flash` | No: one text request | Provider response model can support exact-model verification |
| Native ZCode | no Summon model selector | Native tool loop | Preview only: no reviewed per-call selector or provider-authored served-model receipt |

The direct Z.AI seat uses only the coding endpoint, not a general API endpoint.
It is an `openai-compat` text seat and therefore needs self-contained context
plus the normal text-seat consent. An explicit `ZAI_CODING_API_KEY` wins. If
that is absent, Summon can read only the plan-kind and API-key scalar from an
already-configured official Coding Plan helper profile; it never invokes the
helper, imports arbitrary YAML, copies the key to a child process, or includes
the key/configuration location in telemetry or receipts. `summon doctor` shows
only whether this route is resolvable. `summon usage refresh` remains
schema-unverified for this provider until a version-pinned, privacy-reviewed
usage response contract is available.

Native ZCode discovery checks `ZCODE_CLI`, PATH, Windows uninstall metadata,
and common application bundles without executing a registry command or reading
the app configuration. It launches hidden and headlessly, puts the complete
task in an owner-restricted UTF-8 attachment, removes that attachment after the
child exits, and tolerates a leading banner before the one terminal JSON object.
Because ZCode provides terminal JSON rather than a reviewed progress stream,
its liveness is terminal-only. It refuses `model:` and `safe-edit`; advisory
read-only requires the existing explicit unenforced-read-only opt-in, while
`yolo` requires a worktree or isolated lane. This keeps broad native authority
usable in disposable boundaries without inventing a model or a sandbox claim.

The ZCode bundle-discovery and attached-brief approach was informed by the
[MIT-licensed delegate-skills project](https://github.com/amElnagdy/delegate-skills).
Summon's implementation and provenance contract are independent.

### OpenRouter routers through OpenCode

OpenCode can use OpenRouter's concrete models and router aliases as model
selectors. Because OpenCode prefixes the OpenRouter model ID with its provider
ID, the selectors shown by `opencode models openrouter` are normally:

| OpenCode selector | OpenRouter behavior | Use it for |
|---|---|---|
| `openrouter/z-ai/glm-5.3-flash` | A paid pinned model; formerly the Ox Alpha preview | Reproducible tool/file work |
| `openrouter/openrouter/auto` | Auto Router | Let OpenRouter choose a paid model |
| `openrouter/openrouter/free` | Free Models Router | Low-volume experiments |
| `openrouter/openrouter/fusion` | Fusion model alias | Panel-and-judge synthesis |

`free` is deliberately non-deterministic: it chooses an available free model at
random after filtering for the request's capabilities. Pin a concrete `:free`
model when reproducibility matters. `auto` also resolves to a concrete model;
the provider's response, not the alias, is the source of truth. See the
[Free Models Router](https://openrouter.ai/docs/guides/routing/routers/free-router),
[Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router), and
[Fusion](https://openrouter.ai/docs/guides/features/plugins/fusion) documentation.

Fusion presets are request settings, not model names. A Summon OpenCode seat
may declare a bounded `openrouter_options` JSON value; Summon converts it into
an OpenCode child-only config overlay and uses OpenCode's OpenRouter provider
adapter so the plugin reaches the request body:

```markdown
---
run-agent: opencode
model: openrouter/openrouter/fusion
permission: safe-edit
openrouter_options: '{"plugins":[{"id":"fusion","preset":"general-budget"}]}'
---
```

The accepted Fusion presets are `general-high`, `general-budget`, and
`general-fast`. Auto Router settings use the matching `auto-router` plugin and
can constrain the model pool or set a `cost_tier` (`low`, `medium`, `high`,
`xhigh`, or `max`). Summon rejects unknown fields, mismatched router plugins,
and arbitrary request-body overrides; it never lets this setting change the
OpenCode working directory or permission policy. Fusion runs several underlying
completions, so reserve it for research, critique, and synthesis rather than
every short coding turn. Its panel, judge, and final model can differ from the
requested alias; record the provider response and `model.served` when auditing.

When `openrouter_options` is present, Summon asks OpenCode to use
`@openrouter/ai-sdk-provider` for that model. Supported OpenCode releases bundle
the adapter; if a local release does not, the dispatch fails clearly instead of
silently dropping the router settings. A normal concrete-model or alias seat
does not require this override.

### 429s and explicit alternate routes

An upstream HTTP 429 from OpenRouter is a provider-pool availability result, not
proof that the Summon seat or model selector is broken. The default is fail once;
`--transient-retries` (or `SUMMON_TRANSIENT_RETRIES=1`) permits one bounded
exponential-backoff retry. Summon does not silently switch an exact GLM 5.3 Flash request to
another model or provider, and an envelope with no `model.served`/report remains
non-verdict evidence.

If the OpenRouter pool remains unavailable, an operator can deliberately choose a
different configured route only when its live roster lists the successor and local
auth is valid. The historical Nous `stealth/ox-alpha` preview has ended, and Summon
retires that selector before provider contact; it is not assumed equivalent or free.
The direct API seat does not
provide OpenCode's file/tool loop. Auto, Free, and Fusion are deliberate routing
changes, not transparent GLM fallbacks; verify their served model before treating
the result as a named-model review.

## Custom & API backends (`openai-compat`) — direct text seat

The direct API backend can run against **any OpenAI-compatible
`/chat/completions` API** — OpenRouter, OpenAI, Anthropic, Google (Gemini compat),
Groq, DeepSeek, Together, or a LOCAL server (Ollama, LM Studio, vLLM, llama.cpp).
Pure stdlib HTTP, no SDK. This bills your **API key/credits**, not a subscription
(cleaner for commercial or high-volume use; see [provider terms](../../../TERMS.md)).

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

### OpenRouter credentials on Windows

For the built-in OpenRouter provider, `OPENROUTER_API_KEY` still takes precedence.
On Windows, when that variable is unset, Summon may read a local Credential Manager
entry named `summonOpenRouter`. For the direct API seat, the credential is used only
for the current HTTP request. For an OpenCode OpenRouter seat, it is bridged only into
that child process because OpenCode is the provider gateway; a yolo bridge additionally
requires the explicit isolated-lane and tool-credential flags above. In all cases the
secret is never placed in an agent definition, receipt, telemetry record, command line,
or debug file.
Other providers continue to use their configured environment variable or local
credential mechanism.

Create the entry without putting the key in shell history:

```powershell
cmdkey /generic:summonOpenRouter /user:ApiKey
```

When prompted, enter the OpenRouter key. Check presence with
`cmdkey /list:summonOpenRouter`; that command does not display the secret.

---

## BytePlus ModelArk (Coding Plan + Platform PAYG)

ModelArk is BytePlus's model platform. You can reach it two ways — both work
from Summon (`openai-compat`) and from `arkcli` (CLI chat / helpers):

| Path | Endpoint shape | Billing | Typical auth |
|------|----------------|---------|--------------|
| **Coding Plan** | `.../api/coding/v3` | Flat subscription quota | Coding Plan profile API key → `BYTEPLUS_CODING_API_KEY` |
| **Platform PAYG** | `.../api/v3` (no `/coding/`) | Per-token API credits | Platform / inference API key |

Mixing them up is the main footgun: pointing a Coding Plan key at `/api/v3`
(or the reverse) silently changes billing. Summon's built-in `byteplus-coding`
provider always targets `/api/coding/v3` and **refuses** a `byteplus-coding`
`base_url` that lacks `/api/coding/`.

### Path A — Coding Plan (subscription)

Zero-config beyond the env var:

```markdown
---
run-agent: openai-compat
provider: byteplus-coding
model: deepseek-v4-pro
---
```

On Windows use the bundled launcher: `scripts\summon.cmd --agent byteplus-coder --prompt "..."`.

Set `BYTEPLUS_CODING_API_KEY` to the **profile API key** from `arkcli auth status`
(not the short-lived SSO `id_token`). List profiles with `arkcli auth status`;
Coding Plan profiles are typically named like `coding-plan_<region>_<account>`.

### Path B — Platform PAYG (always per-token)

There is no separate built-in PAYG provider. Use inline OpenAI-compat config
(or `providers.json`) with the **platform** base URL:

```markdown
---
run-agent: openai-compat
base_url: https://ark.ap-southeast.bytepluses.com/api/v3
api_key_env: BYTEPLUS_API_KEY
model: deepseek-v4-pro
---
```

Use your Platform / inference API key (not the Coding Plan key unless you
intentionally accept PAYG). Region hosts differ (e.g. `ark.cn-beijing.volces.com`
for CN). This path never goes through the Coding Plan guardrail or the
consent-gated fallback — every successful call is `billing.source: api`.

### Via `arkcli` (CLI)

```powershell
arkcli auth status --format json          # profiles, keys, plan membership
arkcli +chat --model deepseek-v4-pro-ga-260813 "..." # exact Ark marketplace ID
arkcli helper list
arkcli helper configure opencode --profile <coding-plan-or-platform-profile> --model deepseek-v4-pro-ga-260813
```

`arkcli helper` wires supported CLIs (Claude Code, Codex, OpenCode, Hermes).
`arkcli +connect` only installs documentation skills — it does **not** configure
providers. For Summon you only need the env var + provider / inline `base_url`.
Reset with `arkcli helper reset <harness>` to undo only arkcli-managed bits.

### Coding Plan model selection

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

**Strongly avoid legacy or superseded roster entries for new Coding Plan work:**

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

The static Coding Plan recommendations above were last manually checked on **2026-08-07**;
the separate Ark marketplace editorial catalog was metadata-checked on **2026-08-18**.
Summon also caches a live roster from arkcli at `~/.agents/byteplus-coding-roster.json`
(14-day freshness). Refresh anytime:

```powershell
arkcli auth status --format json
arkcli plans model-list --plan coding-plan --format json
python -c "import sys; sys.path.insert(0, r'<summon>/skills/summon/scripts'); from _apibackend import refresh_coding_plan_roster; print(refresh_coding_plan_roster())"
```

Use `plans model-list`, not `arkcli models list`; the latter is the full
marketplace and includes non-plan and multimodal models. After refreshing the
roster, make one minimal **text-only** call before changing Coding Plan
recommendations. The 2026-08-18 marketplace check confirms these exact public
IDs and capabilities, but does not claim a successful inference or Coding Plan
entitlement:

| Editorial lane | Exact Ark marketplace ID | Metadata |
|---|---|---|
| Frontier / maximum thinking | `deepseek-v4-pro-ga-260813` | DeepSeek V4 Pro GA; thinking; 1M context |
| Near-frontier / long context | `glm-5-2-260617` | GLM 5.2; thinking; 1M context |
| Near-frontier / fast coding | `deepseek-v4-flash-ga-260731` | DeepSeek V4 Flash GA; thinking; 1M context |

Treat these as separate facts:

1. **Listed** — the plan catalog includes the model.
2. **Invocable** — a live Coding Plan text call succeeds.
3. **Recommended** — it is current and strong for a documented use case.

Only promote a model after all three are true. If a previously broken model
starts working, update its status and the last-checked date rather than assuming
that roster presence alone fixed it.

### Text-only constraint (Coding Plan)

Coding Plan models are text-only. Do **not** send image/vision input — the
request will hard-fail with `Model do not support image input`. In Cursor, never
set a Coding Plan model as the default Agent model (Cursor sends screenshots as
part of context). Platform PAYG multimodal endpoints are a separate product
surface — pin those models only on Path B with an explicit `/api/v3` `base_url`.

### Region override (Coding Plan)

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

### Billing note

When the resolved `base_url` contains `/api/coding/`, Summon reports
`billing.source: subscription` with a note that spend draws from Coding Plan
**subscription quota**, not per-token API credits. Consent-gated PAYG fallback
overrides this to `source: api` with an explicit PAYG note.

Generic `infer_billing("openai-compat")` remains `api` for other providers
(including Path B inline `/api/v3`); Coding Plan overlays the subscription
source on the response envelope.

### PAYG fallback from Coding Plan (consent-gated)

When a Coding Plan dispatch fails with a **quota/rate/plan-limit** or
**UnsupportedModel** error (model not on plan), Summon can automatically retry
once against the PAYG endpoint (`/api/v3`) using the same key. That retry is
**Platform PAYG billing** (per-token credits), not Coding Plan quota. This is for
Coding Plan users who occasionally need a model outside the plan — not a
substitute for Path B if you always want PAYG. The retry only fires when you've
explicitly consented through one of (any true wins; `--allow-payg` is
per-dispatch only and does not persist):

| Surface | Scope |
|---------|-------|
| `--allow-payg` flag | Single dispatch only (rejected for `--manifest`/`--council`). Propagates to `--background` children via argv; does **not** mutate the durable env var |
| `SUMMON_ALLOW_BYTEPLUS_PAYG=1` env | Process / session (durable consent surface) |
| `~/.agents/summon.json` `{"allow_byteplus_payg": true}` | Persistent (all sessions) |

Agent frontmatter `allow_payg: true` is **not** a consent grant (agent authors are not the bill payer). It may be noted as a request; operators still need one of the surfaces above.

Examples:

```powershell
scripts\summon.cmd --agent byteplus-coder --prompt "..." --allow-payg
```

```powershell
$env:SUMMON_ALLOW_BYTEPLUS_PAYG = "1"
scripts\summon.cmd --agent byteplus-coder --prompt "..."
```
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
