# Changelog

All notable changes to summon. Versions track the dispatcher (`run_subagent.py --version`).
The response envelope carries its own schema version in the `envelope` field (currently `1`);
it bumps only on a breaking change to the response shape, never on added fields.

## [0.9.0] — unreleased (pre-1.0)

### Fixed (multi-model ultrareview pass)
- **No false success on a backend error.** A claude `is_error` result (and gemini/
  cursor `status:error`) now surfaces as `status:"error"` instead of `success`
  (`_stream`/`_executor`) — the no-false-success guarantee now holds on the terminal
  event too, not just the report contract.
- **Timeouts can no longer be defeated by a grandchild holding stdout.** The driver
  kills the whole process tree (`taskkill /T` / `killpg`) and bounds `communicate()`;
  `--manifest` gains a parent-side watchdog so one wedged child can't stall the swarm.
- **Manifest/`--out` resume retries failures** — only a `success` envelope is terminal;
  `error`/`blocked`/`partial` jobs re-dispatch on a re-run.
- **Fail-closed on an unknown `run-agent`** (was a silent fall-through to codex — wrong
  vendor/permissions/billing). `extract_json` now handles primitive values; frontmatter
  no longer corrupts a value ending in a quote; the alias uninstall matches OUR
  frontmatter, not any file mentioning the marker (data-loss fix); the `openai-compat`
  key is redacted on the success path too; agy honors `--timeout`; auto worktree names
  can't collide under parallel fan-out; council size is bounded; `model.models_used`
  exposes every model a session touched (`resolved` is only the dominant one).
- Bounded reader queue, `run_job` catch-all, `json_schema` type-check, honest README
  ("no network calls" now excepts `openai-compat`; installer line count), plus SKILL.md
  caveats (agy has no `--cwd` file access, `status` is advisory under adversarial agents).



Cross-vendor sub-agent dispatcher over Claude, Codex, Cursor, Gemini, Antigravity, and
any OpenAI-compatible API.

### Command surface
- Git-style subcommands (`dispatch`, `list`, `models`, `doctor`, `manifest`, `council`,
  `agent new|set`, `version`) with the legacy flat `--flag` form kept working.

### Backends
- Six backends behind one registry (`BACKENDS` in `_builder.py`, the single place to add
  one — see `references/adding-a-backend.md`).
- **`openai-compat`** — call any OpenAI-compatible `/chat/completions` endpoint
  (OpenRouter, OpenAI, Anthropic, Google, Groq, DeepSeek, Together, local Ollama/LM
  Studio/vLLM) via stdlib HTTP; providers from built-ins + `providers.json`.
- **Council mode** (`council`) — vendor-diverse members answer, cross-examine, and rank
  each other anonymously (Borda `consensus_ranking`); a chairman synthesizes the decision.

### Core
- One stdlib-Python dispatcher; structured JSON envelope (`status`, parsed `report`,
  `report_ok`, `model.{requested,resolved}`, `permission`/`permission_flags`, `usage`,
  `cost_usd`, `billing.{source,note}`, `elapsed_ms`, `envelope` schema version).
- Envelope-status reconciliation: a self-reported `STATUS: BLOCKED/PARTIAL/ERROR` (or an
  interactive-approval request with no report) never surfaces as a false `success`.
- Session resume (`--resume`) for claude/codex/cursor/agy; per-call `--model`/`--effort`.
- **Reasoning effort** works across **claude, codex, and agy**: claude → `--effort`,
  codex → `-c model_reasoning_effort` (xhigh/max clamp to high), agy → a Gemini
  model's thinking suffix (`Gemini 3.1 Pro (High)`, applied only when set
  explicitly). Adds an `effort:` frontmatter field, defaults to **`high`**
  (precedence: `--effort` > frontmatter > `SUMMON_DEFAULT_EFFORT` > high; `none` =
  backend default), and surfaces the applied effort in the envelope's `effort`.
- Opus agents + the Fable fallback pin the explicit **`claude-opus-4-8`** (the
  `opus` alias currently lags to 4-7); council model labels never show blank
  (fall back to the requested model, show `alias -> version`).

### Fan-out & structured output
- `--manifest jobs.json --concurrency agy=2,codex=3 --results-dir` — per-backend
  semaphores, atomic per-job envelopes, skip-if-done resume.
- `--background` (detached, result-file completion), `--worktree` (isolated git worktrees).
- `--json-schema` — extract + validate the agent's final JSON, one corrective retry.
- `--out FILE` (atomic + skip-if-exists), `--retries N` (backoff).

### Roster & discovery
- `--list`, `--list-models` (live/config/static per backend), `--doctor` (backend health).
- `--new-agent` / `--set-agent` — scaffold/retune agent definitions from the CLI
  (frontmatter validated; injection-hardened).
- `args:` frontmatter passthrough for arbitrary backend flags.

### Install & safety
- `install.py` — ownership-manifested, staged atomic swaps, crash recovery, host-root
  locking; installs the skill as `summon` (`--with-alias` adds a thin `sub-agents` alias).
- agy backend runs in a fresh per-invocation, token-locked profile.
- Bundled agents default to `safe-edit` and carry an untrusted-content guard.
- **Credit-only model guard (Fable):** `claude-fable-5` left the Claude Max
  subscription and bills account credit, so a `claude` dispatch requesting it
  falls back to the `opus` alias (latest subscription Opus) with a `warnings`
  entry — unless `SUMMON_ALLOW_FABLE=1` (or `SUMMON_ALLOW_CREDIT=1`), which runs
  it on credit and marks `billing.source: "credit"` (or `"api"` if an
  `ANTHROPIC_API_KEY` is present). The guard is hardened against silent bypass:
  it also scrubs credit-only `--model`/`--fallback-model` values from an agent's
  `args:`, strips `ANTHROPIC_*` env vars that remap an alias to a credit-only
  model, and warns that a `--resume` keeps the session's original model. The
  bundled `fable-api` agent runs Fable metered via `ANTHROPIC_API_KEY`
  (openai-compat, unaffected).

### Naming
- Skill renamed `sub-agents` → **`summon`**; `sub-agents` retained as an optional alias.
