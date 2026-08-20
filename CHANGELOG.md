# Changelog

This file lists user-visible changes to Summon. For the certification record, regression
notes, and test evidence, see the [detailed engineering history](docs/ENGINEERING_CHANGELOG.md).

## [Unreleased]

Development changes after 3.1.0 will be recorded here.

## [3.1.0] - 2026-08-20

- **Dispatch evidence guard:** ACP and subprocess completions that claim success without
  a usable result are now reported as the typed `empty_terminal_result` error instead of
  silently completing.
- **Model provenance:** envelopes distinguish `reported`, `inferred`, and `absent`
  `served_model_evidence`; malformed model identifiers are omitted from public metadata.
- **Retry safety:** typed empty results are not retried by fan-out or resume. The explicit
  `--retry-nonretryable` control permits one deliberate fresh attempt; a second empty
  result remains suppressed.
- **Deliberation ballot isolation:** live provider turns use a compact, non-interactive
  ballot-only system context instead of inheriting ordinary agent instructions for tools,
  plan mode, or long report blocks. This prevents live-boundary hangs while preserving
  the frozen roster and receipt identity used by ordinary deliberation planning.
- **Live-provider certification:** a bounded Claude Opus matrix covers a decided normal
  run, durable cancellation after provider contact, and conservative deadline handling
  with `uncertain_spend=true`. The source-bound receipt records exact served-model,
  profile, consent, owner/deadline/cancel fences, no fallback/retry, and clean teardown.
- **Release posture:** this is the 3.1.0 GA release for the evidence-integrity and
  governed-deliberation lane. Chat/swarm previews and other provider routes retain their
  own explicit boundaries; no provider claim is inferred from an editorial model label.
- **Documentation:** clarified the 3.1 GA boundary, unmanaged-copy policy, local
  diagnostics, and the distinction between chat turns, council context, and deliberation
  authority. Public docs contain no workstation-specific drift details.

## [3.0.0-ga] - 2026-08-18

- **Claude route isolation:** default Claude dispatches now pass an empty
  `--setting-sources` boundary so unrelated user/project `ANTHROPIC_*` settings
  (including a custom endpoint, model, or token) cannot silently reroute a
  subscription-backed Claude invocation. Intentional custom Claude-compatible
  routes remain available through an explicitly named private profile.
- **Release contract:** the source-bound version/migration contract, fixed gate
  registry, and immutable release evidence are now part of the 3.0.0 GA path.
- **Claude live-provider gate:** a reviewed read-only Opus matrix now covers a decided
  normal run, durable cancellation, and conservative deadline/uncertain-spend handling;
  the redacted schema-2 receipt is source-bound and other providers remain independently
  gated.

- **One open product:** Summon deliberation is part of the same public product as
  dispatch, council, and the browser observer. The public `/summon deliberate` command and thin `/deliberate`
  companion skill share the same receipt, journal, replay, recovery, and browser
  observer contracts.
- **Explicit governed-decision guidance:** agent instructions now distinguish
  dispatch, manifest, council, and deliberate, and require options, seats, quorum,
  rounds, attempt budget, deadline, and human-approval policy to be stated rather
  than inferred.
- **Fresh deliberation lane:** the public `deliberate` command now admits only a
  receipt-bound, one-round, one-attempt-per-seat run of enforceable read-only
  subprocess seats with executable evidence. The owner journals a queued cancel
  at the replay-safe attempt boundary; approval, resume, ACP/HTTP, Kimi, text-only,
  writable, and full-bypass routes remain explicitly gated.
- **Install convergence:** managed hosts receive the marked `/deliberate` companion
  without clobbering a foreign skill; provider-inert and live-gated readiness labels
  are now used consistently in the product docs.
- **Conversation atlas:** the authenticated loopback room now uses a dependency-free,
  Chatpack-informed messenger layout: searchable project/initiator rooms, grouped
  human/agent/lifecycle events, role/name/version identity chips, a redacted evidence
  drawer, cursor-aware reconnect, and separate context versus roster-agent turn
  composers. `chat open --chat-browser auto|builtin|ide|system|link` starts or reuses
  one surface; `link` is non-launching for CI/SSH. Chatpack itself is not installed.
- **Chat safety/readability slice:** live turn claims now use a journal cursor
  compare-and-swap across runtime instances; human messages expose bounded redacted
  previews to the atlas; continuation forks preserve the prompt that caused the fork;
  generic Authorization/Bearer/Basic/Cookie material is redacted and cancellation is
  recorded as a durable `turn_cancel_requested` command observable by another runtime.
- **Chat lifecycle fencing:** participant leases and process records now reject
  symlink/junction redirection, bind atlas reuse to project/explicit-roster digests,
  fence PID reuse with birth tokens, serialize Python 3.13 pipe cleanup, and use the
  shared Windows Job Object/POSIX process-group teardown path. Windows turns fail closed
  when the Job Object cannot be attached; this remains a local preview capability gate.
- **Transport evidence:** chat dispatch forwards the roster-declared subprocess/ACP
  transport to the actual dispatcher boundary instead of silently defaulting to
  subprocess while claiming ACP.
- **Swarm contract preview:** documented and validated the versioned
  `summon.swarm/v1` stdio frame boundary for future worker claims, leases, directed
  messages, fenced artifacts, and cancellation. Completion/artifact frames carry the
  task/attempt/lease-generation/request digest fence and unknown authority fields are
  rejected. This is a protocol contract only; the
  current `manifest` command remains batch fan-out and no native IDE swarm
  attachment is implied.
- **Model identity catalog:** role, name, version, and editorial frontier labels are
  now explicit display metadata; exact `model.served` evidence remains separate and
  unknown models never become dispatchable by implication.

### Certification and installation

- **GA certification:** published the clean source-bound `v3.0.0-ga` release
  after all 17 fixed suites (1,138/1,138) and all eight release gates passed.
- **Ark roster refresh:** the catalog now lists DeepSeek V4 Pro GA as the fifth editorial
  frontier lane and GLM 5.2 and DeepSeek V4 Flash GA as near-frontier value lanes. These
  labels are routing metadata, not live availability or served-model evidence.
- **Reviewed Claude pilot:** certified the bounded `claude-opus-5` normal,
  durable-cancel, and deadline/uncertain-spend cases with exact served-model,
  profile, account-evidence, cleanup, and consent receipts.
- **Install convergence:** all eight managed host copies match the release
  payload at 3.0.0. Unmanaged local copies are preserved and reported by
  `doctor`; this release record contains no workstation-specific drift data.
- **Engineering record:** the full certification, migration, rollback, and
  release-manifest evidence is recorded in
  [`docs/ENGINEERING_CHANGELOG.md`](docs/ENGINEERING_CHANGELOG.md).

The pre-existing `v3.0.0` entry below is retained as the historical
provider-inert/public-preview baseline. Fable, Gemini, Kimi, and other provider
routes remain independently gated.

## [3.0.0] - 2026-08-15

- Synchronized the plugin, dispatcher, and MCP companion to the 3.0.0 contract.
- This immutable tag is the historical provider-inert/public-preview baseline.
  The later `v3.0.0-ga` tag carries the clean source-bound GA certification.
- Multi-provider live deliberation, automatic fallback/retry, remote hosting, and
  unproven live continuation remain explicitly deferred. Provider expansion
  still requires an independent receipt and gate.

## [2.2.0] - 2026-08-12

### Highlights

- **Opt-in diagnostics:** `summon telemetry enable` captures bounded, categorized local
  dispatch metadata (including deterministic SHA-256 fingerprints for local correlation);
  `summon bug-report` creates a reviewable Markdown report. A second,
  explicit `--submit-github --from REVIEWED_REPORT.md` command submits that exact file through
  authenticated `gh`. Telemetry is disabled by default, contains no prompt/result text or
  credentials/absolute paths, and never phones home. Fingerprints can still correlate or
  reveal low-entropy values, so review before sharing.

### Fixed

- Council setup and overall-timeout exits now leave categorized local diagnostic events when
  telemetry is enabled; spool locking, bounded reads, and reviewed-report submission are
  ownership- and snapshot-safe.

### Install / upgrade

```bash
npx skills add Nafjan/summon
```

## [2.1.0] - 2026-08-11

### Highlights

- **Named local profiles:** route a Claude agent to an operator-owned config directory and,
  when needed, a pinned executable without putting machine paths or credentials in the
  public roster. Receipts carry the profile name and integrity digests.
- **Strict roster provenance:** add `--strict-agents-dir` to make governance-controlled
  dispatch fail closed when a named role is absent, while keeping the normal bundled/plugin
  fallback convenient by default.
- **Private role aliases (experimental):** propose and explicitly approve operator-owned
  aliases for existing roster definitions, then opt into them with `--enable-roles`. Exact
  names win; target and approval hashes are checked before dispatch and only digest-based
  provenance appears in receipts.

### Maintenance

- Added request-identity and background forwarding coverage for profile selection. Automatic
  cross-account retries remain intentionally out of scope because they can duplicate work.
- Background, manifest, and council role propagation is covered; malformed, chained, or
  retargeted aliases fail closed rather than silently falling back.

## [2.0.5] - 2026-08-09

### Highlights

- **Headless Windows launches:** dispatcher, utility, detached, and nested AGY
  processes now request both `CREATE_NO_WINDOW` and a hidden startup state, so
  routine Summon work no longer flashes black console windows. The skill and README
  include a caller checklist for bypassing visible `Start-Process`/`cmd /c start`
  wrappers and the legacy AGY PTY override.

### Fixed

- **ACP timeout cleanup:** Kimi and other ACP children are tree-killed before their leader can
  orphan a backend process; timeout envelopes now show one correct unit and the protocol phase
  Summon can prove.
- **Privacy hygiene:** parsed handoff fields and dry-run previews are redacted before they enter
  envelopes or debug artifacts; gate handoff declarations remain visible to the caller.
- **Health checks:** `doctor` now allows up to 45 seconds for a CLI's first `--version`
  response, preventing healthy but slow AGY/Gemini Windows starts from being reported as broken.

### Install / upgrade

```bash
npx skills add Nafjan/summon
```

## [2.0.4] - 2026-08-08

Kill the leftover "agy cannot read `--cwd`" myth in code comments; clarify why
`researcher` is omitted from council defaults.

### Fixed

- **DEFAULT_MEMBERS comments** no longer list Antigravity in the default set or claim
  agy "reports no telemetry" absolutely — Summon's default subprocess path simply does
  not surface usage/`model.served`; read-only needs `SUMMON_ALLOW_UNENFORCED_READONLY=1`.
- **Removed dead** `_AGY_FILE_READ_RE` / `_agy_prompt_references_file` (unused since the
  retired no-cwd warning was deleted).
- Tests assert the myth string and dead regex stay gone.

## [2.0.3] - 2026-08-08

Docs clarity for reasoning effort / thinking levels (no runtime behavior change).

### Added

- **[references/effort.md](skills/summon/references/effort.md):** per-backend matrix for
  who honors `--effort`, Summon default `high` (claude/codex), agy Gemini explicit-only
  suffixes, and backends that ignore the flag (cursor/kimi/openai-compat/arkcli). Linked
  from SKILL.md, customizing, models, fan-out, and `--help`.

## [2.0.2] - 2026-08-08

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

## [2.0.1] - 2026-08-07

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

## [2.0.0] - 2026-08-07

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
