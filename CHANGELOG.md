# Changelog

All notable changes to summon, following [Keep a Changelog](https://keepachangelog.com)
and [Semantic Versioning](https://semver.org). Versions track the dispatcher
(`run_subagent.py --version`). The response envelope carries its own schema version in the
`envelope` field (currently `1`); it bumps only on a breaking change to the response shape,
never on added fields.

## [0.9.0] - unreleased (pre-1.0)

First tracked version: the cross-vendor sub-agent dispatcher over Claude, Codex, Cursor,
Gemini, Antigravity, and any OpenAI-compatible API. The sections below describe the 0.9.0
baseline and the iterative hardening on top of it.

### Added

**Dispatch and orchestration**
- One stdlib-Python dispatcher returning a structured JSON envelope: `status`, parsed
  `report`, `report_ok`, `model`, `permission`/`permission_flags`, `usage`, `cost_usd`,
  `billing`, `elapsed_ms`, and the `envelope` schema version.
- Git-style subcommands (`dispatch`, `list`, `models`, `doctor`, `manifest`, `council`,
  `agent new|set`, `version`); the legacy flat `--flag` form keeps working.
- Session resume (`--resume`) for claude/codex/cursor/agy, with per-call `--model` and
  `--effort` overrides.
- Council mode (`council`): vendor-diverse members answer, cross-examine, and rank each
  other anonymously (Borda `consensus_ranking`); a chairman synthesizes the decision.
- Fan-out: `--manifest` swarms with per-backend concurrency and skip-if-done resume,
  `--background` (detached, result-file completion), `--worktree` (isolated git worktrees),
  `--json-schema` (validate the agent's final JSON with one corrective retry), `--out`
  (atomic, skip-if-exists), and `--retries`.
- Roster and discovery: `--list`, `--list-models` (live/config/static per backend),
  `--doctor` (backend health), and `--new-agent`/`--set-agent` to scaffold and retune
  agent definitions from the CLI (frontmatter validated and injection-hardened).
- Reasoning effort across claude, codex, and agy, with an `effort:` frontmatter field
  (precedence `--effort` > frontmatter > `SUMMON_DEFAULT_EFFORT` > the built-in `high`).

**Backends**
- Six backends behind one registry (`BACKENDS` in `_builder.py`, the single place to add
  one; see `references/adding-a-backend.md`).
- `openai-compat`: call any OpenAI-compatible `/chat/completions` endpoint (OpenRouter,
  OpenAI, Anthropic, Google, Groq, DeepSeek, Together, local Ollama/LM Studio/vLLM) over
  stdlib HTTP, with providers from built-ins plus `providers.json`.

**Council synthesis controls**
- `--quorum N` gates whether the chairman runs: synthesis proceeds only when at least N
  members (2..member-count) succeeded; below N the chairman is skipped and a `skipped`
  tombstone is recorded. Quorum never changes the top-level `status` (which still requires
  the synthesis to succeed with no failed members); the outcome is reported in
  `synthesis.quorum` and `synthesis.decision_status`.
- `--chairman-fallback AGENT` runs a second synthesizer once when the primary chairman
  ends on any non-success outcome (only success suppresses it). Both outcomes persist as
  `synthesis.primary` / `synthesis.fallback`, with council warnings and billing aggregated
  across both.
- `--member-timeout` / `--chair-timeout` give member and chairman stages their own clocks
  (default: `--timeout`); the owner lease is sized on the longer of the two.

**Durable, resumable councils**
- Councils run on a persistent run directory (`{cwd}/.agents/runs/<run-id>/`; override with
  `--run-dir` or `SUMMON_RUNS_DIR`), replacing the throwaway temp dir that soft exits
  deleted and hard kills orphaned. Each stage envelope, a `receipt.json` binding the run's
  inputs, and an append-only per-generation journal persist; the envelope gains `run_id`
  and `generation`.
- `council resume <run-id>` re-runs only the stages that are missing, failed, or whose
  inputs changed, and carries every unchanged stage forward without re-dispatching.
  Question, members, chairman, and rounds come from the run's receipt. Stage input hashes
  bind the exact prompt plus execution identity (member, agent-definition hash, cwd), so a
  changed repo, retuned agent, or edited earlier-stage output invalidates that stage and
  everything downstream; superseded files are preserved under `superseded/`.
- `council status <run-id>` prints a read-only, generation-stable snapshot (per-stage
  status, generation, attempts, abandoned work; `--json` for machines).

**Provenance and telemetry**
- Provenance receipt on every dispatch envelope, including preflight errors:
  `summon.{version, script, scripts_sha256}` (one SHA-256 over all production modules),
  `agent_def.{file, sha256, agents_dir, source}`, `prompt_sha256` (the root prompt, never
  restamped by a schema retry), and `git_head_before`. A stale or divergent install is
  diagnosable from any single envelope.
- `model.targeted` and `model.served`, split by evidence: `targeted` is what the session
  was pointed at; `served` is set only on real service evidence (a terminal model report,
  or output tokens), never inferred from task status. `resolved` keeps its legacy behavior
  for compatibility until envelope v2.

**Flags**
- `--prompt-file` for direct dispatch (UTF-8, BOM tolerated, strict decoding; background
  children re-read the file). Ergonomics for long or awkwardly quoted prompts; backend
  argv limits still apply.
- `--allow-credit`: the per-dispatch flag form of `SUMMON_ALLOW_CREDIT=1`, rejected for
  fan-out modes where env inheritance would authorize every child silently.

### Changed

- **Fan-out flag matrix.** `--manifest` and `--council` used to silently ignore most
  dispatch flags (a council's `--out` was dropped without a word, losing the artifact a
  killed run was meant to save). Each mode now consumes a fixed whitelist and rejects
  anything else before any paid work, with a pointer to where the capability lives.
  Ambiguous input pairs are rejected too (`--prompt` with `--prompt-file`, `--question`
  with `--question-file`, a manifest job's `prompt` with `prompt_file`).
- **Council `--out` is checkpointed.** The council envelope is written to `--out` after
  every phase (`council_state`: `round1_complete`/`round2_complete`/`final`/`failed`), so a
  host-tool kill mid-synthesis leaves every completed member position on disk. A worst-case
  wall-clock estimate prints to stderr before dispatching.
- **agy `safe-edit` warning.** agy has no workspace-write tier, so `safe-edit` is a full
  permission bypass identical to `yolo`; every such dispatch (and its dry-run) now carries
  a warning that says so.
- **Documentation.** The self-contradicting host-timeout guidance is fixed (set the host
  timeout above the child deadline), and the "always list agents first" workflow is
  softened to once per session.

### Fixed

- **No false success on a backend error.** A claude `is_error` result, and gemini/cursor
  `status: error`, now surface as `status: "error"` instead of `success`, so the
  no-false-success guarantee holds on the terminal event, not only the report contract.
- **Timeouts survive a grandchild holding stdout.** The driver kills the whole process tree
  and bounds `communicate()`; `--manifest` gained a parent-side watchdog so one wedged
  child cannot stall the swarm.
- **Manifest and `--out` resume retry failures.** Only a `success` envelope is terminal;
  `error`/`blocked`/`partial` jobs re-dispatch on a re-run.
- **Fail-closed on an unknown `run-agent`** (previously a silent fall-through to codex,
  under the wrong vendor, permissions, and billing).
- **Council concurrency (durable-run protocol),** hardened across four codex adversarial
  review rounds, two of which caught real blockers the test suites missed:
  - One leased owner lock per run, renewed after every stage. The lock is immutable for its
    ownership period and renewals go to a nonce-named sidecar, so a suspended-then-resumed
    owner cannot overwrite or delete a successor's lock.
  - A fresh generation per ownership period namespaces all output files; journal and state
    are segmented per generation, giving a single writer per file by construction. Torn-tail
    recovery repairs a crashed predecessor's journal segment on takeover.
  - Known limitation: the owner-lock stale-break has a sub-millisecond unlink window that
    pure-stdlib, cross-platform file operations cannot fully close. Generation namespacing
    bounds the worst case to one duplicate stage dispatch (wasted spend, never corrupted
    output), and single-machine use does not hit it.
- **Assorted correctness:** `extract_json` handles primitive values; frontmatter no longer
  corrupts a value ending in a quote; the alias uninstall matches our own frontmatter
  rather than any file mentioning the marker (a data-loss fix); the `openai-compat` key is
  redacted on the success path; agy honors `--timeout`; auto worktree names cannot collide
  under parallel fan-out; council size is bounded; `model.models_used` lists every model a
  session touched; and a bounded reader queue, `run_job` catch-all, and `json_schema`
  type-check close remaining edge cases.

### Security

- **Credit-only model guard (Fable).** `claude-fable-5` left the Claude Max subscription and
  bills account credit, so a `claude` dispatch requesting it falls back to the latest
  subscription Opus with a `warnings` entry, unless `SUMMON_ALLOW_FABLE=1` (or
  `SUMMON_ALLOW_CREDIT=1`). The guard also scrubs credit-only `--model`/`--fallback-model`
  values from an agent's `args:`, strips `ANTHROPIC_*` env vars that remap an alias to a
  credit-only model, and warns that a `--resume` keeps the session's original model. The
  bundled `fable-api` agent runs Fable metered via `ANTHROPIC_API_KEY` (openai-compat) and
  is unaffected.
- `OPENAI_API_KEY` is stripped from codex children by default (subscription billing); opt
  out with `SUBAGENTS_ALLOW_OPENAI_KEY=1`.
- The agy backend runs in a fresh, per-invocation, token-locked profile; `openai-compat`
  keys are read from env only and redacted from any error output.
- `install.py` is ownership-manifested with staged atomic swaps, crash recovery, and
  host-root locking; it never overwrites an agent file you already have. Bundled agents
  default to `safe-edit` and carry an untrusted-content guard.

### Naming

- The skill was renamed `sub-agents` to `summon`; `sub-agents` is retained as an optional
  back-compat alias.
