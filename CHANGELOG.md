# Changelog

This file lists user-visible changes to Summon. For the certification record, regression
notes, and test evidence, see the [detailed engineering history](docs/ENGINEERING_CHANGELOG.md).

## [Unreleased]

- **ZCode and Z.AI Coding Plan routes:** added a native ZCode preview backend
  with provider-inert Windows/PATH/bundle discovery, hidden headless launch,
  owner-restricted prompt attachments, banner-tolerant terminal JSON parsing,
  session/usage capture, and explicit terminal-only liveness. Native ZCode
  refuses model pins and safe-edit because neither per-call model selection nor
  a safe-edit boundary is proven. The truthful generic seat is `zcode-native`;
  the misleading `zcode-coding-plan` name is retired with that successor.
  Experimental native yolo now requires both a
  disposable boundary and an explicit credential-access acknowledgement, which
  does not claim a sandbox, and every native tier scrubs inherited provider
  credential variables. Added distinct direct and OpenCode Z.AI
  Coding Plan GLM-5.3 Flash seats alongside the existing OpenRouter successor;
  retired Ox aliases and historical receipts remain unchanged. The direct seat
  is text-only, pins the coding endpoint, supports an explicit environment key
  and a bounded helper-config fallback, and never runs helper scripts or emits
  credentials/configuration locations. Z.AI usage remains diagnostic-only and
  schema-unverified pending a reviewed provider contract. Direct API request
  identity binds environment, Windows Credential Manager, Hermes/Nous,
  ArkCLI, and Z.AI-helper credential fallbacks by a private one-way identity;
  unavailable evidence stays unarmed rather than causing a false mismatch.

## [3.3.0] - 2026-08-28

- **Release-candidate boundary hardening:** provider subprocesses now receive an explicit,
  scrubbed environment snapshot on every dispatch path, including ordinary foreground
  turns. Summon refuses unsafe Windows batch-shim fallbacks before provider contact, uses
  the safe ArkCLI Node entry point when available, and reports zero attempts for these
  structural refusals. On POSIX, the AGY proxy stays in the supervised process group so a
  timeout can terminate its descendants as well as the proxy.
- **Deliberation evidence consistency:** live, server-sent event, and replay views now
  derive receipts from the authenticated checkpoint. Replay accepts model identity only
  after the matching finished attempt in the same generation, and a seat is exact-model
  verified only when, for every finished attempt, the requested and targeted identities
  match the provider-reported served identity. Public replay data hashes raw model
  identifiers.
- **Integrated Phase 1 operator and migration contract:** the shipped operator guide now
  joins fleet explanation and approval, advisory usage evidence, safe/off context
  compilation, adaptive job supervision, and portable-result consumption into one
  provider-inert workflow. A standalone standard-library consumer validates a checked-in
  portable golden without importing Summon internals and never grants authority. Windows
  launcher coverage proves unsafe raw multiline prompts fail before provider contact and
  `--prompt-file` preserves the exact text. The active migration gate is now bound to the
  canonical product version and explicitly preserves Phase 1 job, room, approval, usage,
  and telemetry state across refresh and uninstall; stale 3.0/3.1 prose can no longer make
  a current release appear ready. The fixed CI/release registry includes the integrated
  workflow.
- **Experimental portable result receipts:** `summon result project` now derives a
  compact, provider-inert compatibility receipt from an exact private dispatch envelope
  or an authenticated terminal background job. The private envelope remains authoritative.
  Portable output excludes prompts, model prose, transcripts, sessions, account facts,
  raw errors, and local paths; it recomputes exact-model proof, preserves unknown attempt
  evidence, and publishes content digests plus bounded metadata instead of mutable artifact
  path locators. `result validate` and the authority-free `reference` consumer reject unknown
  fields, forged cross-field claims, duplicate JSON keys, unsafe paths, and modified
  digests. Chat, council, deliberate, and swarm currently fail before provider contact
  because none exposes one authenticated terminal receipt with equivalent semantics.
- **AGY terminal and timeout correctness:** Summon now passes AGY an explicit
  `--print-timeout` derived from the same foreground or adaptive hard budget, strips caller
  attempts to override that boundary, and preserves the prompt boundary. AGY terminal
  states are typed instead of treating every result-shaped event as success: error and
  invalid fail, cancelled/interrupted block, waiting/running remain partial, and unknown
  states fail closed—even when the terminal event contains no response text. Isolated AGY
  credential profiles now bind cleanup to the live owner and cap dead-owner retention
  without shortening an active adaptive job.
- **Opt-in safe context compilation:** ordinary dispatch now accepts a typed
  `summon.context-input/v1` packet through `--context-input-file`. The default safe
  profile mechanically compacts only typed payloads; ordinary file input rejects
  authority-plane blocks because a data file cannot authenticate host authority. It
  publishes hashes, action counts, lineage, rollback identity, and before/after token
  estimates in `--dry-run` without contacting a provider. External references require
  an exact `sha256:<digest>=FILE` binding that Summon hash-reads from `--cwd` with
  final-handle verification and emits as a cwd-relative locator; caller-authored
  verification booleans are not trusted. The proof covers compile time, so the receiver
  must re-hash before use. The
  `off` profile appends the exact source serialization, while dispatches with no context
  flags retain the legacy prompt bytes. Background children consume the exact compiled
  prompt frozen in their immutable per-job bundle, and result trust binds the terminal
  prompt digest to the launch record. Stale deliberation acceptance is additionally bound to
  a digest of the resolved run namespace, rechecked before every provider launch, so
  copying it to another run root cannot replay its one-run authority; the new acceptance
  and binding contracts use v2 schemas. Authenticated v1 evidence remains readable for
  historical status/replay but cannot authorize new live work.
- **Opt-in usage and credit refresh:** `summon usage refresh` now supports one reviewed,
  bounded Codex account-usage adapter. It requires an explicit provider allowlist plus
  `--allow-account-usage-read`, checks the exact supported CLI version and JSON-RPC plan,
  launches headlessly, keeps raw account responses private and transient, and never logs
  in, repairs authentication, dispatches, retries, or changes routing. Mixed or unsupported
  provider sets fail before contact. `usage export` produces a portable redacted snapshot,
  while `usage example` creates deterministic synthetic evidence for integrations. Usage
  dimensions remain separate and advisory; exact pins and spend boundaries still win.
  AGY and ArkCLI expose promising usage commands, but remain schema-unverified until
  version-pinned response fixtures and privacy mappings are reviewed.
- **Approved fleet launch:** an explicit `dispatch --lane` can now consume one exact,
  authenticated fleet approval for one single-candidate foreground subprocess attempt.
  The provider boundary revalidates the sealed fleet, roster, project, prompt/data proof,
  permission and spend ceilings, and an atomic launch claim. Retry, fallback, repair,
  resume, background, worktree, and authority expansion remain unavailable in this slice.
- **Authenticated fleet approval recording:** `summon fleet approval` can now inspect,
  record, list, and revoke authenticated, expiring local authority for one exact compiled
  fleet lane. Approval binds the sealed fleet and plan, project and roster catalog,
  lane digest, operation, authority ceilings, and generation; copied, forged, stale,
  expired, or revoked records fail closed. Mutations use mandatory generation
  compare-and-swap; idempotency includes the requested lifetime, so a shorter request
  cannot silently reuse longer authority. Windows private paths are reset to and verified
  against an owner-only ACL through native security-descriptor and current-process-token
  APIs, including pre-existing explicit grants and without executable lookup. Expired and
  revoked entries are compacted on the next mutation; relative
  path overrides and nonempty unsafe directories are refused, and new approvals reserve
  byte and generation headroom for revocation. Store-size failures and lock failures are rejected before corrupting
  state and expose no private path. Approval projections distinguish issuance generation
  from the current store generation. Store reads and writes share a collection-sized
  bounded parse contract, and explicit receipt targets are checked before authority is
  recorded. Public receipts
  redact private identities, paths, and authentication
  material and explicitly report `recorded_not_activated`: this slice cannot select,
  reserve, retry, resume, dispatch, or contact a provider.
- **Provider-inert fleet drafts:** `summon fleet propose`, `validate`, `inspect`,
  and `explain` now create and examine sealed lane constraints without selecting a
  winner, approving work, contacting a provider, or authorizing spend. Compiled plans
  bind the exact sanitized roster catalog and project directory object; substitutions,
  case-folding collisions, unavailable seats, unsupported capabilities, input clobbering,
  and private-path write errors fail closed. Inspection remains roster-independent,
  while explanation reports candidate priority, provenance, declared and effective
  permission, losing constraints, and unresolved evidence. Provider identity and the
  dispatch-default permission now come from the same immutable definition snapshot;
  OpenCode declarations must match the selector it executes, named API registry keys
  preserve exact case, and inline/undeclared routes stay explicitly unknown. Unknown
  identity is a losing explanation reason even without an allowlist.
  Explicit fleet outputs are no-clobber.
- **Ox Alpha lifecycle correction:** every historical direct and tool-enabled
  `stealth/ox-alpha` route is retired and remains pinned to that selector, so existing
  receipts are never relabeled. Route-level tombstones also stop stale custom roster
  definitions before provider contact.
  A distinct successor targets paid `z-ai/glm-5.3-flash`. Retired seats fail before
  provider contact and identify their successor in a typed error. The lifecycle gate is
  shared by ordinary dispatch and the in-process live-deliberation roster boundary, so a
  retired definition cannot bypass it through another launch surface. Fleet catalogs admit
  only active seats; deprecated compatibility dispatch remains explicit and warned.
- **AGY 1.1.22 headless integration:** model discovery now prefers the documented
  JSON command envelope and retains one bounded legacy-text fallback. Nested init and
  step events now publish trusted session, target-model, tool, generation, and usage
  activity to the adaptive watchdog without turning progress metadata into served-model
  proof. Duplicate progress cannot renew liveness, and Windows shim discovery remains
  headless.
- **Activity-aware long jobs and governed continuation:** background dispatches now
  treat their timeout as a progress checkpoint by default, publish authenticated
  activity/idle diagnostics, and accept durable extend, cancel, and queued-steering
  commands. Eligible terminal Claude subprocess jobs can create one authenticated,
  idempotent successor with the same workspace and no greater authority. The successor
  preserves any gate, requires fresh spend consent, disables retries/fallback/repair,
  preserves the source's authenticated text-seat/tool requirement while scrubbing
  ambient text-only grants, and reuses queued steering exactly once. Proven pre-spawn failures are safely
  recoverable; ambiguous launch or registration failures remain indeterminate and do
  not relaunch automatically. A canonical child result may close the result lifecycle,
  but it never rewrites unknown provider contact into a Boolean; the resume projection
  remains blocked and recovery-required. Detached subprocess steering is explicitly queued for
  the successor—Summon does not claim the running model received it live.
- **Reliable concurrent job controls on Windows:** first-use control locking no
  longer writes a seed byte before acquiring the operating-system lock. Simultaneous
  extend, cancel, or steer commands are serialized without the intermittent
  `PermissionError` that could occur while creating a new lock file.
- **Refusal and public-proof invariants:** every structural pre-dispatch refusal
  (including read-root, backend, model, text-seat, and gate checks) now states
  `attempts:0`, `attempt_status:"not_run"`, `execution_status:"not_run"`, and
  `provider_contacted:false`. The public JSON and telemetry/report projections
  also force `model.served:null`, `served_model_evidence:"absent"`,
  `model_match:null`, and `named_model_verified:false`; contradictory compatibility
  fields cannot turn a refusal into a named-model claim. Retry and corrective
  aggregation preserves zero rather than inventing a paid attempt; the terminal
  operation status remains `error` or `blocked` according to the refusal path.
  Public report validation rejects forged `model_match:true` or
  `named_model_verified:true` values unless the trusted reported-evidence
  relationship is present.
- **Evidence-proof and exit clarity:** report parsing now accepts bounded Markdown
  heading fields such as `## VERDICT: BLOCK`. Envelopes expose tri-state
  `model_match` plus `named_model_verified`, and distinguish
  `raw_backend_exit_code` from Summon's `normalized_exit_code`; caller-supplied
  model-proof flags are never trusted.
- **Background terminal receipts:** detached children now publish parser/early-exit
  failures as typed envelopes, and terminal writes reuse the bounded Windows atomic
  replacement retry used by launch records. The same retry now protects explicit
  `--out` result replacement from transient antivirus/indexer sharing locks. A dead child remains `stale` rather than
  being mistaken for a provider result; no model evidence is synthesized.
- **OpenRouter 429 handling:** `--transient-retries` now recognizes explicit upstream
  HTTP 429/rate-limit errors, including the machine-readable `rate_limit_exceeded` token,
  and permits one bounded exponential-backoff retry. It remains
  opt-in, does not retry authentication failures, and never silently switches an Ox seat
  to another provider or model.
- **Windows tool fallback and Kimi evidence:** a complete report is preserved when a
  child cannot run a convenience command such as `grep`; the envelope keeps the raw
  exit code and adds a typed, non-secret `tool_failure` diagnostic with cross-platform
  alternatives, but the fatal backend outcome remains an unusable error. Kimi assistant
  and isolated runtime records expose model/usage observations when available; because
  those records are child-writable, they are labeled inferred and cannot certify an exact
  named-model review.
- **Exact named-model provenance:** provenance-required named seats now fail closed when
  the provider reports a different dominant terminal model or no authoritative served-model
  receipt. `--require-exact-model` is available for custom seats, and built-in governance
  seats expose the policy in dry-run/envelopes. Auxiliary models remain visible in
  `model.models_used` without being silently accepted as the named seat.
- **Windows AGY popup guard:** Summon now keeps the bundled `agy_stream_proxy.py` as the
  default Windows wrapper and refuses to select the legacy `agy_pty_pyte.py` pseudo-console
  wrapper unless `AGY_ALLOW_LEGACY_PTY=1` is set deliberately. The shared hidden-launch
  flags still apply to the dispatcher, detached jobs, utilities, and nested AGY process;
  custom wrappers remain responsible for hiding any children they create.
- **Install-drift scope:** `doctor --json` now separates the installer gate
  (`installs.drift.managed_converged`) from the stricter all-copy `converged` result. Stale
  project and plugin copies remain visible as unmanaged drift instead of being reported as
  repairable by `install.py`; no unmanaged tree is overwritten implicitly.
- **Explicit credential boundary for broad OpenCode lanes:** broad OpenCode `yolo` now requires
  `--worktree` or `--isolated-lane`. A private OpenRouter or Nous credential is bridged into
  an unrestricted child only with both the explicit `--isolated-lane` and
  `--allow-tool-credentials` acknowledgements; a worktree alone is not an OS boundary.
  Otherwise inherited provider variables are scrubbed and dispatch fails closed. Public
  guidance now distinguishes mutation isolation from OS/credential isolation and adds the
  untrusted-content guard to the optional Ox definition; provider availability may change
  without a Summon release.
- **Kimi evidence wording:** Kimi K3 remains the maximum-thinking target for the bundled
  worker/coder seats, but the roster no longer tells callers to expect `model.served` when
  the active CLI does not emit provider-authored evidence. Successful null-provenance Kimi
  output remains advisory and reproducible, not a certified named-model review.
- **Kimi 0.38 stream parsing:** role records that carry a `type` field are now recognized,
  and untyped final verifier payloads stay content instead of becoming an unrelated terminal
  result. Non-zero Kimi exits remain errors; Summon never upgrades a failed child into a
  certified review. Provider-inert fixtures cover tool-then-report and verifier-only shapes.
- **Broad-authority isolated lanes:** Kimi, OpenCode/Ox, and agy/Antigravity guidance now
  explicitly supports full-authority code, UI, research, and review work in disposable
  clones or isolated worktrees. The optional Ox seat uses `yolo` so its tool loop is not
  artificially constrained while that provider route is available; callers must inspect
  mutation evidence, diffs, tests, and
  reports before integrating. The docs now state that a Git worktree is not an OS sandbox;
  use a separate clone/Git directory, account, container, or VM when protecting shared
  metadata or live resources. OpenCode `--auto` claims remain conditional on a versioned
  provider-inert acceptance. The stricter boundary remains for credentials, private or
  client data, provider spend, databases/migrations, deployments, shared Git, protected
  artifacts, and running stacks.
- **Codex failure events and OpenCode isolation:** Codex `turn.failed` events now become
  structured terminal errors instead of disappearing into a generic empty result. OpenCode
  headless turns now pass the documented `--pure` flag and reject external-drive working
  directories consistently even when additional read roots are requested.
- **Windows dispatcher launcher:** installed skills now include `scripts\\summon.cmd`.
  It selects a Python 3.10+ interpreter through `py -3`, so a stale Windows `.py`
  association cannot start Summon with an incompatible interpreter. Windows instructions now
  prefer this launcher over direct `.py` invocation.
- **Background provenance freeze:** managed background dispatches now execute from a
  durable immutable per-job scripts snapshot. The launch record retains the original
  launcher identity separately from the execution-bundle identity, and terminal results
  must match both the job nonce and frozen scripts digest before they are trusted. An
  install now briefly refuses while a managed launcher is taking that snapshot, preventing
  an in-flight job from being relabeled by an atomic skill refresh.
- **OpenCode headless completion guard:** OpenCode read-only and safe-edit turns now use
  `--auto` together with Summon's explicit deny-by-default permission policy. Explicit
  denies remain in force, while allowed read/list/edit tools no longer wait for an
  interactive approval that a one-shot child cannot answer. The event parser also keeps
  typeless OpenCode progress packets out of the legacy cursor terminal path. A clean EOF
  without `step_finish` is retained as a suspect, non-verdict outcome so a first progress
  sentence cannot be mistaken for a completed review.
- **OpenCode zero-output diagnostics:** a headless `step_finish` with `reason: unknown`,
  zero tokens, and no text is now identified as an upstream/provider no-output completion.
  Summon rejects it as an empty result, keeps the `step_start` model as target-only, and
  records bounded stream diagnostics; `--auto` is not treated as the cause and no fallback
  is attempted.
- **Provider-auth diagnostics:** OpenCode's missing-cookie credential error and direct API
  missing-key failures now normalize to a terminal `authentication_failed` envelope with
  an explicit login/configuration path. Summon still never retries or exposes credentials.
- **Agent validation diagnostics:** `agents validate` reports legacy flat global `*.md`
  rosters separately and includes a safe failure detail instead of an opaque manifest-error
  label.
- **OpenCode timeout diagnostics:** headless OpenCode timeouts with no usable output are
  now typed as non-retryable provider timeouts with explicit local-auth guidance. Summon
  does not infer an authentication failure or silently spend another provider turn.
- **Kimi timeout diagnostics and fallback control:** when Kimi emits assistant text before
  the dispatcher deadline but never reaches clean EOF, the envelope now retains a bounded,
  redacted `partial` snapshot for diagnosis. It is explicitly advisory: it is not a report,
  result, resume/cache input, or model evidence, and `report_ok` remains false. Kimi ACP
  recovery is no longer automatic; use `--allow-kimi-acp-fallback` (or
  `SUMMON_KIMI_ACP_FALLBACK=1`) when a deliberate second provider turn is worth the
  duplicate-spend and limited ACP tool surface.
- **Timeout documentation:** public invocation examples now use explicit `ms`, `s`, or `m`
  units. Bare numeric values remain backward-compatible milliseconds, but dispatches reject
  ambiguous bare sub-second budgets; `jobs wait` keeps its short-poll compatibility.
- **Multi-root read-only preflight:** Claude and Gemini read-only seats can now declare
  explicit additional directories with `--read-root` or `read-roots`. Dry-runs report the
  requested and effective readable paths; unsupported backends refuse instead of implying
  that an out-of-workspace file was available.
- **Background read-root propagation:** detached jobs now forward every validated repeatable
  `--read-root` into the child and retain the canonical roots in the launch record. Foreground
  dry-runs and background dispatches therefore describe the same read allowlist.
- **Nous/Hermes local bridge:** direct OpenAI-compatible and OpenCode Nous seats can resolve
  `NOUS_API_KEY` from the local Hermes profile without placing credentials in agent definitions,
  child-independent config, receipts, or telemetry.
- **Nous auth recovery guidance:** a rejected legacy Hermes profile key is now classified as a
  non-retryable authentication failure with explicit Portal re-auth guidance; Summon does not
  silently retry or switch providers.
- **Nous direct transport:** direct OpenAI-compatible requests now send a normal API
  `User-Agent`/`Accept` pair, avoiding provider-edge 403/1010 rejections caused by the default
  Python transport fingerprint.
- **Hermes OpenRouter credential bridge:** when the named `summonOpenRouter` Windows credential
  is absent, local dispatches can use `OPENROUTER_API_KEY` from the explicit Hermes `.env` file;
  environment and WCM precedence remain unchanged.

## [3.2.1] - 2026-08-22

- **Codex certification scope clarified:** Codex remains a supported first-class backend.
  Only provenance-required named-model claims, such as `sol-review`, are blocked when the
  CLI does not emit authoritative `model.served` evidence. Summon records the requested and
  targeted model, leaves `served` unset, and never silently substitutes Luna. Certification
  requires a provider-authored terminal receipt plus match, mismatch, and missing-receipt
  fixtures and a fresh live run.
- **Kimi K3 defaults:** `kimi-worker` and `kimi-coder` now pin K3 with maximum supported
  thinking through the isolated profile. The former K2.7 coding seat remains available as
  `kimi-k27-coder`. Kimi effort is now exposed as requested profile configuration rather than
  silently discarded; it is not treated as provider-authored served-model evidence.
- **OpenCode gateway:** added an `opencode` subprocess backend with JSON event parsing,
  model/variant/session boundary enforcement, per-tier permission policies, live model
  discovery, and a bundled OpenRouter OX Alpha seat. OpenCode provides a tool/file loop for
  compatible providers; direct `openai-compat` seats remain text-only. Model and provider
  context limits still apply.
- **OpenRouter routers through OpenCode:** OpenCode seats can use the OpenRouter `auto`,
  `free`, and `fusion` aliases. A bounded `openrouter_options` field carries documented
  Auto Router and Fusion plugin settings, including `general-high`, `general-budget`, and
  `general-fast`, without allowing arbitrary request-body or permission overrides.
- **OpenCode startup isolation:** headless OpenCode dispatches now disable repository-level
  config/plugins and external skill discovery before any private provider credential is
  bridged into the child process.

## [3.2.0] - 2026-08-22

- **Codex model routing guard:** explicit Codex model requests now collapse agreeing
  `-m`/`--model`/`-c model=...` selectors into one canonical launch selector. Conflicting
  selectors are refused before provider contact. An explicit pin without an authoritative
  terminal served-model receipt is blocked rather than reported as a successful,
  verified run; these trust failures are not retried or silently rerouted.
- **Codex identity display:** an explicit model request no longer inherits the ambient
  Codex default in legacy `model.resolved` when no handshake or served-model identity is
  emitted. The envelope now makes the missing evidence visible instead of making a Sol
  request appear to have run on Luna.
- **Named model seats:** added explicitly pinned `sol-review` (`gpt-5.6-sol`),
  `terra-review` (`gpt-5.6-terra`), and `luna-review` (`gpt-5.6-luna`) seats.
  `--list --json` now shows each seat's
  declared backend, permission, model, and effort so an unpinned seat cannot be
  mistaken for a named model. Terra remains a candidate until its receipt proves
  `model.served`; Luna remains a separate, explicit cost-efficient lane.
- **Conversation atlas stability:** the local chat surface now reserves status and activity
  space, gives the timeline its own scroll owner, preserves a visible feed anchor during
  updates, and keeps role/name/version/model/provider identity together in agent choices.
  The atlas remains subject to rendered-browser and lifecycle gates before chat GA.
- **Next-release contract:** added [`docs/SUMMON_3.2_PLAN.md`](docs/SUMMON_3.2_PLAN.md),
  which defines Codex resume fencing, live served-model evidence, roster freshness,
  privacy boundaries, and the fixed-shell chat acceptance gates.
- **Authentication recovery:** expired or invalid provider credentials now produce a typed,
  actionable repair plan. `summon auth status` is read-only; `summon auth repair BACKEND`
  requires `--allow-auth-repair`, never captures credentials, and never retries the failed
  dispatch implicitly.
- **Kimi OAuth refreshes:** isolated Kimi dispatch homes now persist a validated provider
  token rotation back to the source credential file atomically, while refusing to overwrite
  a newer login or concurrent refresh. Kimi authentication failures and rate limits are
  terminal, clearly explained, and never trigger a hidden retry or provider switch.
- **Telemetry clarity:** permission-tier refusals now have their own bounded failure class,
  so local diagnostics distinguish an unenforceable permission request from authentication,
  quota, and generic backend failures.
- **Telemetry operation audit:** added a schema-2, opt-in, local-only operation-terminal
  event contract with immutable operation context, bounded HMAC correlation, strict public
  report validation, malformed-input fail-soft handling, and an offline audit that rejects
  unbound model/auth evidence. This release does not claim signed evidence provenance or
  production attempt/turn metrics.
- **Roster freshness:** `summon models --refresh` explicitly refreshes provider rosters where
  supported. Cached, configured, static, and live sources are distinguished, while exact
  `model.served` evidence remains the authority for what ran. ArkCLI is now a first-class
  discovery/auth lane; Codex candidates remain advisory when its CLI cannot enumerate models.
- **CI reliability:** release evidence now checks the current 3.2.0 version, includes the
  telemetry audit suite, and the Windows deliberation UI syntax test validates a temporary
  JavaScript file instead of waiting on a
  stdin EOF path that can hang on Windows.

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
