# Reasoning effort & thinking levels

> Part of the **summon** skill. Calling agents: read this before assuming
> `--effort` does the same thing on every backend.

Summon exposes **one** portable control: `--effort` (plus frontmatter / env).
Only some backends honor it. Others ignore it or encode thinking in the **model
name** instead.

## How to set it (calling agent)

| Surface | Example |
|---------|---------|
| Dispatch flag | `--effort high` |
| Agent frontmatter | `effort: high` |
| Environment | `SUMMON_DEFAULT_EFFORT=high` |
| Manifest job | `"effort": "high"` |
| Background child | forwarded `--effort` from parent |

**Allowed values:** `low` · `medium` · `high` · `xhigh` · `max`  
**Opt out of Summon's default:** `none` · `default` · `off` → leave the backend's own default alone.

**Precedence (highest wins):**

1. `--effort` on the dispatch
2. Agent frontmatter `effort:`
3. `SUMMON_DEFAULT_EFFORT`
4. Built-in Summon default: **`high`** (claude / codex only — see table)

## Who honors it

| Backend | Honors `--effort`? | What Summon actually does | Default if you set nothing |
|---------|--------------------|---------------------------|----------------------------|
| **claude** | Yes | Passes `--effort <level>` to the CLI | **`high`** (Summon default) |
| **codex** | Yes | `-c model_reasoning_effort=<level>`; **`xhigh`/`max` clamp to `high`** | **`high`** (Summon default) |
| **agy** + **Gemini** model | Yes, but only when **explicit** | Rewrites the model display name to `… (Low\|Medium\|High)`; `xhigh`/`max` → `High` | **No rewrite** — keeps whatever is in `model:` (e.g. already `(High)`). `SUMMON_DEFAULT_EFFORT` / built-in `high` do **not** change agy Gemini models |
| **agy** + non-Gemini (e.g. Claude Thinking) | No via `--effort` | Pin thinking in `model:` itself, e.g. `Claude Opus 4.6 (Thinking)`. Explicit `--effort` prints a note and is ignored | Model string as written |
| **cursor-agent** | No | Ignored (stderr note if you set it explicitly). Cursor's own `[effort=…]` syntax inside `--model 'id[…]'` is forwarded **verbatim** — that is Cursor's knob, not Summon `--effort` | Cursor / model-string default |
| **kimi** | No | Ignored | Kimi default |
| **gemini** CLI | No | Ignored (CLI is frozen; prefer agy) | CLI default |
| **openai-compat** / **arkcli** | No | Ignored. Provider APIs may support `reasoning_effort` / thinking, but Summon does **not** map `--effort` onto those requests today | Provider / model default |

If you pass `--effort` (or frontmatter `effort:`) on a backend that ignores it,
Summon prints a short stderr note and clears the effort so the envelope does not
claim a level that was never applied.

## Envelope: what you can read back

| Field | When |
|-------|------|
| `effort` | Set for **claude** / **codex** to the level actually applied; `null` means backend default (`none` / ignored / cleared) |
| `model.requested` | For **agy** Gemini, shows the suffix Summon asked for (e.g. `Gemini 3.1 Pro (High)`) |
| `model.served` | What the backend reported it ran (when available) |

## Practical rules for orchestrators

1. **Claude / Codex:** `--effort` is the right lever; default is already `high`.
2. **Agy Gemini:** either put the thinking tier in `model:` (`Gemini 3.6 Flash (High)`), **or** pass `--effort` / frontmatter `effort:` explicitly. Do not rely on `SUMMON_DEFAULT_EFFORT` alone.
3. **Cursor:** put effort in the Cursor model string if needed; do not expect Summon `--effort` to change it.
4. **Text seats (openai-compat / arkcli):** no Summon effort mapping — pick a model that thinks by default, or use a toolful CLI.
5. Not every Gemini variant has every tier (e.g. some Pros have no Medium). An unavailable suffix fails at agy; check `model.requested`.

## Related

- [models.md](models.md) — roster / `--list-models`
- [customizing.md](customizing.md) — per-dispatch and durable frontmatter edits
- [backends.md](backends.md) — openai-compat / arkcli / Coding Plan
