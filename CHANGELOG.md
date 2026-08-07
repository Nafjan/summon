# Changelog

The important, user-visible changes to summon. For the complete certification record,
regression notes, and test evidence, see the
[detailed engineering history](docs/ENGINEERING_CHANGELOG.md).

## [Unreleased]

### Added

- **BytePlus ModelArk:** built-in `byteplus-coding` provider for Coding Plan
  (`/api/coding/v3`) with `BYTEPLUS_CODING_API_KEY`, bundled `byteplus-coder`
  agent, subscription billing on successful Coding Plan calls, live roster cache
  from arkcli, and consent-gated one-shot PAYG (`/api/v3`) fallback
  (`--allow-payg` / frontmatter / env / prefs). Platform PAYG remains available
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
