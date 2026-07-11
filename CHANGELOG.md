# Changelog

All notable changes to summon. Versions track the dispatcher (`run_subagent.py --version`).
The response envelope carries its own schema version in the `envelope` field (currently `1`);
it bumps only on a breaking change to the response shape, never on added fields.

## [0.9.0] — unreleased (pre-1.0)

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

### Naming
- Skill renamed `sub-agents` → **`summon`**; `sub-agents` retained as an optional alias.
