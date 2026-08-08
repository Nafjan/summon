# Changelog

The important, user-visible changes to summon. For the complete certification record,
regression notes, and test evidence, see the
[detailed engineering history](docs/ENGINEERING_CHANGELOG.md).

## [2.0.4] — 2026-08-08

Kill the leftover "agy cannot read `--cwd`" myth in code comments; clarify why
`researcher` is omitted from council defaults.

### Fixed

- **DEFAULT_MEMBERS comments** no longer list Antigravity in the default set or claim
  agy "reports no telemetry" absolutely — Summon's default subprocess path simply does
  not surface usage/`model.served`; read-only needs `SUMMON_ALLOW_UNENFORCED_READONLY=1`.
- **Removed dead** `_AGY_FILE_READ_RE` / `_agy_prompt_references_file` (unused since the
  retired no-cwd warning was deleted).
- Tests assert the myth string and dead regex stay gone.

## [2.0.3] — 2026-08-08

Docs clarity for reasoning effort / thinking levels (no runtime behavior change).

### Added

- **[references/effort.md](skills/summon/references/effort.md):** per-backend matrix for
  who honors `--effort`, Summon default `high` (claude/codex), agy Gemini explicit-only
  suffixes, and backends that ignore the flag (cursor/kimi/openai-compat/arkcli). Linked
  from SKILL.md, customizing, models, fan-out, and `--help`.

## [2.0.2] — 2026-08-08

Patch on 2.0.1 from round-2 adversarial review (GLM-5.2 + DeepSeek-V4-Flash via
arkcli, AGY Flash 3.6 via summon).

### Fixed

- **Text-seat `--out` fingerprint** now treats agent frontmatter `capability: text-only`
  as opted-in (same as `--allow-text-only` / `SUMMON_ALLOW_TEXT_ONLY=1`), using the
  already-loaded definition snapshot — so consent identity matches the gate.
- **`install.py --hosts`** applies the same `os.path.isdir` host-root filter as the
  profile-only path; refuses with a clear error when none of the requested roots exist.
- **Council chairman clamp** matches only known stages (`chairman`,
  `chairman-fallback`) after the `gN-` prefix — not every tag whose suffix merely
  starts with `chairman`.

## [2.0.1] — 2026-08-07

Patch on 2.0.0: text-seat honesty, T3 Summon-side support, timeout/PAYG budget
hardening, and adversarial-review fixes (Kimi K3 + GPT-5.6 Sol + Claude Opus).

### Added

- **T3 Code support (Summon-side):** `python install.py --profile t3` installs into
  Claude/Codex/Cursor skill roots T3 discovers; `doctor` / `onboard` report a `t3_code`
  readiness section; guide at `skills/summon/references/t3-code.md`. Not a native T3
  plugin and not an upstream T3 PR — Summon rides provider skill discovery.
  `--profile t3 --hosts` is intersected with the profile set (out-of-profile hosts refused).

- **Council/manifest text-seat gate:** ModelArk / `openai-compat` / `arkcli` members
  are auto-rejected in fan-out unless `SUMMON_ALLOW_TEXT_ONLY=1` (capability /
  `--allow-text-only` remain single-dispatch only). Pure-text councils still work
  with the env set deliberately. Fan-out children get `--require-tools` when that
  env is unset (blocks mid-run definition mutation to `capability: text-only`).
  `SUMMON_REQUIRE_TOOLS=1` also refuses fan-out text seats.

- **Banner:** `assets/banner.svg` lists kimi and modelark alongside the other backends.

- **Text-seat honesty:** `openai-compat` and Summon `arkcli` refuse by default
  unless `--allow-text-only` / `SUMMON_ALLOW_TEXT_ONLY=1` / `capability: text-only`.
  Machine-readable `blocked_reason: text_seat_no_tools` + `text_seat` recovery
  object (PATH-filtered `suggested_reroutes`). Opt-in still warns every run;
  `--require-tools` / `SUMMON_REQUIRE_TOOLS=1` overrides opt-in. House chat agents
  (`byteplus-coder`, `openrouter-example`, `fable-api`) declare `capability: text-only`.
  **Migration:** existing one-shot openai-compat scripts must pass the flag or set
  capability; do not auto-retry blocked text seats with `--allow-text-only`.

### Fixed

- **Timeout hardening:** Coding Plan → PAYG retry spends only the *remaining*
  wall-clock budget; skip PAYG when remaining is under 1s. Council
  `--overall-timeout` clamp reserves the parent watchdog margin and excludes
  stages that cannot afford child+margin.
- **Chairman read-only clamp** now matches generation-prefixed tags (`gN-chairman`).
- **Text-seat consent** is part of request identity for `--out` reuse (opt-in /
  require-tools cannot be skipped via a cached success).
- Manifest text-seat gate fails closed (no ImportError swallow; honest CLI resolve).

## [2.0.0] — 2026-08-07

Product 2.0: Agent Plugin distribution + optional MCP facade + provider-driver SPI
on the same broker (envelope schema still `1`). Includes everything that landed as
the 1.2 onboard train, plus pack discovery wired into `--list` / load paths and an
explicit MCP local-trust ADR section.

### Added

- **Agent Plugin packaging:** root `plugin.json` (Agent Plugins 1.0.0) plus optional
  `mcp.json` stdio facade; keep `install.py` for skill-dir hosts. See
  [docs/ADR-mcp-facade.md](docs/ADR-mcp-facade.md).
- **Provider-driver SPI:** thin `cli` / `openai-compat` / `arkcli` / `acp` registry with
  additive envelope fields (`backend_type`, `served.via`, `provider.driver`) and a
  dual-wire `run-agent: arkcli` backend (`arkcli +chat`). See
  [docs/ADR-provider-drivers.md](docs/ADR-provider-drivers.md).
- **Agent packs:** optional `com.summon.agents/` extension namespace for third-party
  roster packs.
- **Streaming partials:** `SUMMON_STREAM_PARTIALS=1` emits JSONL progress on stderr
  without changing the final envelope.

- **Onboard (`--onboard`):** detect CLIs, record subscription prefs in
  `~/.agents/summon.json`, and surface them in `doctor` (never stores API secrets).
- **BytePlus key auto-resolve:** `BYTEPLUS_CODING_API_KEY` from env or local
  arkcli profile when unset; doctor reports presence and source only.
- **Dry-run hints:** `native_prefer_hint` when the host CLI matches the target
  backend, or when `openai-compat` is a single-shot chat seat.
- **Envelope telemetry:** additive `backend_type`, `served_via`, and
  `provider.driver` on dispatch results.
- **Agent tags:** optional frontmatter `capability:` / `billing:` copied to
  dry-run and result as `agent_tags`.
- **Transient retries:** `--transient-retries` / `SUMMON_TRANSIENT_RETRIES=1`
  for one conservative retry on timeout/5xx-style failures (never auth).

- **BytePlus ModelArk:** built-in `byteplus-coding` provider for Coding Plan
  (`/api/coding/v3`) with `BYTEPLUS_CODING_API_KEY`, bundled `byteplus-coder`
  agent, subscription billing on successful Coding Plan calls, live roster cache
  from arkcli, and consent-gated one-shot PAYG (`/api/v3`) fallback
  (`--allow-payg` / env / prefs). Platform PAYG remains available
  via inline `openai-compat` `base_url` without the Coding Plan provider.

- **Environment handoff:** every bundled agent now reports `LEFT_BEHIND`, and each envelope
  exposes it as `environment_handoff` so its caller can decide what to retain or clean up.
  This covers temporary paths, processes, servers, VMs, and container resources; summon
  never deletes them automatically.

- **Purposeful Kimi roster roles:** `kimi-worker` now identifies K3 as the high-context
  architecture/review worker, while `kimi-coder` pins K2.7 Coding for scoped implementation
  and debugging. Both retain Kimi's explicit full-authority worktree requirement.

### Fixed

- **Privacy hygiene:** handover material no longer publishes local profile paths or session
  identifiers, and preserved AGY evidence is ignored by default. BytePlus missing-key
  guidance no longer points at arkcli-private credential store paths or env var names.

## [1.1.0] - 2026-07-31

**Kimi Code joins Summon, alongside a more dependable AGY path.**

### Highlights

- **Use Summon directly in Kimi Code:** `install.py --hosts kimi` installs the complete skill at `~/.kimi-code/skills/summon`, ready for Kimi’s native `/skill:summon` workflow.
- **Call Kimi from any supported host:** the new `kimi` backend uses Kimi’s native `stream-json` protocol, supports model pins such as `kimi-code/k3` and `kimi-code/kimi-for-coding`, and returns the same structured Summon envelope as the other CLI backends.
- **Safer Kimi one-shots:** each Kimi child receives a fresh, ACL-locked runtime profile with no inherited sessions, logs, user skills, or MCP configuration. Because Kimi’s prompt mode has no enforceable workspace sandbox, Summon refuses misleading `read-only` and `safe-edit` declarations; the explicit `kimi-worker` is for trusted isolated worktrees.
- **AGY reliability pass:** a built-in cross-platform stream proxy replaces fragile terminal scraping where AGY exposes JSONL, with stronger event parsing and model-alias handling.

### Also improved

- **ACP transport (phase 1):** `gemini`, `kimi`, and `cursor-agent` can run over the Agent
  Client Protocol (JSON-RPC stdio) instead of a one-shot argv spawn. Three engagements:
  automatic recovery attempt when a subprocess dispatch fails in a transport-fixable way
  (narrow predicate — never for auth/spawn/structural failures), automatic routing for
  prompts over the OS argv limit, and explicit opt-in via `transport: acp` frontmatter or
  `--transport acp`. New flags: `--transport`, `--no-acp-fallback` (env
  `SUMMON_ACP_FALLBACK=0`). Envelopes record `transport`, `fallback` (the recovery
  attempt, doubling as telemetry for scoping ACP phase 2 — claude/codex adapters — on
  measured fallback rates), and `acp.session_id` (telemetry only; the resume lane stays
  empty). ACP is yolo-only: permission flags cannot travel over the protocol, so
  `read-only`/`safe-edit` are refused rather than run with reactive-only containment
  (within yolo, permission requests are auto-answered allow-once only, fail closed);
  model pinning is best-effort with a warning. `--doctor` reports per-CLI ACP support.
- Hardened debug argument redaction and boundary-flag handling.
- Clarified wrapper readiness in diagnostics.
- Accept markdown-bold report fields (`**STATUS:**`) from markdown-rendered backends such as
  AGY, avoiding an unnecessary corrective resume.

Release certification remains separate: three clean cross-vendor rounds are still required
before these additions earn a certification claim.

## [1.0.0] - 2026-07-29

**Summon any AI, from any CLI.** Version 1.0 establishes the stable public contract for
the capabilities built throughout the 0.x series. The highlights below are a milestone
recap; the final release also corrects plan-dependent Fable billing telemetry.

### Highlights

- **Cross-vendor orchestration from the tool you already use.** Send work to Claude,
  Codex, Cursor, Gemini, Antigravity, an OpenAI-compatible API, or a local model without
  leaving your current CLI or agent app.
- **Councils for decisions, swarms for scale.** Run vendor-diverse deliberations with
  anonymized peer ranking, or fan out resumable manifest jobs with per-backend
  concurrency.
- **Safer parallel implementation.** Isolate editing agents in Git worktrees, preview
  resolved runs before spending tokens, clamp permissions on backends that enforce them,
  and require an independent gate before the child agent runs.
- **Results built for automation.** Every run returns a stable JSON envelope separating
  execution state from the agent's verdict, with model provenance, warnings, resume
  state, and structured report or handoff data when available.
- **Reliable structured output.** Validate responses against summon's documented JSON
  Schema subset, surface unsupported keywords in `parse_warnings`, and use a corrective
  retry on backends that support resume.
- **Auditable work beyond Git.** Track loose documents by SHA-256, detect changes during
  a run, and use the bundled document-audit templates for claim-led reviews.
- **Operational confidence.** Background jobs survive the parent session, `doctor`
  surfaces backend readiness and installation drift, and every dispatch identifies the
  exact installed script copy that ran.
- **A small core.** The dispatcher is standard-library Python and needs no summon server.
  The optional Antigravity PTY bridge uses `pywinpty` and `pyte`.

### Stable contract

The documented CLI, agent definitions, report fields, and exit codes are now public API.
The response envelope remains independently versioned as `envelope: 1`, allowing
backward-compatible additions throughout the 1.x line.

### Fixed

- Reported Fable billing as plan-dependent instead of assuming every Claude plan includes it.

### Release confidence

The corrected runtime surface earned three consecutive clean cross-vendor adversarial
reviews after a clean manual security audit. The final release passes 429 discovery tests
and 22 installer tests. The exact acceptance criteria and evidence are documented in
[Versioning and 1.0 criteria](docs/VERSIONING_AND_1.0_CRITERIA.md).

## [0.19.2] - 2026-07-29

### Fixed

- Kept the executor's status authoritative through report reconciliation.
- Represented unreadable post-run artifact state as unknown instead of unchanged.
- Contained worktree cleanup by resolved path, including through symlinks.

## [0.19.1] - 2026-07-29

### Fixed

- Prevented `jobs wait` from losing a result published as its child exited.
- Stopped treating a failed artifact read as an observed file change.

## [0.19.0] - 2026-07-29

### Added

- Separated successful execution from a reviewing agent's `pass`, `conditional`, or
  `block` verdict.
- Added resume reporting, loose-file provenance, document-audit templates, and
  liveness-aware background jobs.

### Fixed

- Preserved worktrees when a denied run contained new or ambiguous work.
- Corrected background-job liveness and stale-state reporting.

## [0.18.0] - 2026-07-28

### Security

- Restricted Antigravity cleanup to structurally verified generated logs and rechecked
  file identity immediately before deletion.

### Added

- Added roster permission lint and agent-definition provenance to dry runs and diagnostics.

### Fixed

- Corrected gate timeout parsing, worktree teardown, and timeout-cause reporting.

## Earlier development releases

The earlier 0.x series established the core dispatcher, six backend families, agent
rosters, structured reports, councils, manifest swarms, background jobs, isolated
worktrees, permission gates, model discovery, and installation diagnostics. Its detailed
release history remains available, version by version, in the
[engineering archive](docs/ENGINEERING_CHANGELOG.md).
