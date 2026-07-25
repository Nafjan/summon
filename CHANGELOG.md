# Changelog

All notable changes to summon, following [Keep a Changelog](https://keepachangelog.com)
and [Semantic Versioning](https://semver.org). Versions track the dispatcher
(`run_subagent.py --version`). The response envelope carries its own schema version in the
`envelope` field (currently `1`); it bumps only on a breaking change to the response shape,
never on added fields.

## [0.10.4] - 2026-07-25

### Fixed
- **The Opus pin was two releases stale: dispatches ran Opus 4.8, not Opus 5.** The four Opus
  agents (`planner`, `architect`, `deep-debugger`, `security-auditor`) pinned
  `claude-opus-4-8` in their frontmatter, and `_OPUS_FALLBACK` -- the model the credit guard
  substitutes for Fable -- pinned the same. All now pin `claude-opus-5`, verified served on
  subscription billing by a live dispatch before the pin was changed.
- **The `opus` alias is still NOT a substitute for the pin.** Re-verified 2026-07-25:
  `--model opus` served `claude-opus-4-7`, two releases behind. Pinning a full id remains the
  only way to get the newest Opus; `models` output now says so explicitly.
- **The pin can no longer rot silently.** Four tests asserted the credit-guard fallback
  against the literal `"claude-opus-4-8"`, so bumping the constant would have left them
  asserting a dead model (and, before that, kept the stale pin looking correct). They now
  assert against `_builder._OPUS_FALLBACK` itself.

### Removed
- **The dead `~/.agents/skills/summon` copy.** Nothing loads skills from `~/.agents/skills/`
  (it holds the agent ROSTER, one level up), so that copy was never read -- it only rotted,
  once reaching v0.9.0 code behind a hand-edited `0.10.1` version string. `doctor` still
  probes the location as an unmanaged tripwire, so a third-party clone reappearing there is
  reported; it simply now reports absent.

## [0.10.3] - 2026-07-25

### Added
- **Antigravity is a first-class install host.** Antigravity keeps its file-based skills
  per PROFILE under `~/.gemini/antigravity*`, not in a root of its own, so `~/.gemini` alone
  only ever reached the Gemini CLI's skills dir -- Antigravity never saw summon, and any copy
  put there by hand was unmanaged (no ownership manifest, invisible to drift detection).
  `install.py` now targets each Antigravity profile (`antigravity`, `antigravity-cli`,
  `antigravity-ide`) like any other host, and `_installs.HOST_DIRS` tracks them so
  `doctor` reports their version and drift. Profiles that do not exist are simply not
  detected; profiles that share one physical dir collapse into a single record.

## [0.10.2] - 2026-07-23

### Security
- **The API key no longer follows a cross-host redirect.** `urllib` replays every original header
  on a redirect, so an `openai-compat` endpoint answering `302 Location: https://elsewhere/...`
  received `Authorization: Bearer <key>` verbatim -- a configured, compromised, or merely mistaken
  `base_url` could exfiltrate the credential with a single response. Same-origin redirects are
  ordinary API routing and are still followed; a cross-origin one is refused outright, naming the
  reason, rather than silently retried without the header (which would surface as a confusing 401).
  Found by cross-vendor adversarial review of this branch and reproduced with two local servers.

### Fixed
- **Env-authorized credit reaches the environment identity.** `env_override_for` recognized only
  the `--allow-credit` flag, not `SUMMON_ALLOW_CREDIT`/`SUMMON_ALLOW_FABLE`, so an env-authorized run
  hashed the stripped environment while the child received the credit-only remap.
- **The args-only credit fallback is in the request identity.** The guard pins the Opus fallback for
  an `args:`-only credit-only model, but the identity tracked only `--model`/frontmatter, so changing
  the fallback constant changed the dispatched model with the fingerprint unmoved.
- **Automatic retries carry the request's attestation.** The schema-correction and contract-repair
  retries built a fresh invocation by hand, so an account (or any other field) not in the
  constructor call was dropped -- an agy account swap between the first response and the retry ran
  under the original identity. Both retries now CLONE the original invocation and override only the
  retry-specific fields, so no field can be silently lost from these paths again.
- **The request identity loads the agent definition ONCE, from a single byte buffer.** The backend,
  endpoint, model defaults, effort, and agy-account decision were each derived from a SEPARATE
  `load_agent` read, so a definition swapped A -> B -> A mid-construction produced a hybrid identity
  -- A's content hash paired with B's resolved backend -- which among other things turned agy account
  attestation off for a real agy request. `load_agent_snapshot` now returns the parsed tuple, the
  frontmatter (for the endpoint) and the content hash from ONE `read_bytes`, and
  `build_request_identity` threads that single snapshot through every definition-derived field. The
  invariant is not merely "one call" but "every definition-derived value and digest originates from
  the exact same immutable bytes", so no field can come from a different read.
- **An agent definition, schema, memory, or agy account that APPEARS after fingerprinting is
  refused.** Attestation ran only when a hash had been recorded, so an input absent when the identity
  was built but present by dispatch shaped the run while contributing nothing to the identity naming
  it. The comparisons are now unconditional, and an explicit `_agy_account_checked` marker
  distinguishes "no account files then" from a pre-0.10.2 caller that recorded nothing.
- **A credit-only model selected only through `args:` now falls back to Opus.** The flag was
  scrubbed but not replaced, so the request reached the backend with no model at all and the
  vendor's own default answered. That still prevented the unauthorized credit spend, but it is not
  the documented behavior and silently answered on a model nobody chose.
- **Project memory is hashed and injected from the same read.** It was attested from one read and
  injected from another, so an agent could run under instructions the envelope did not name.
- **An input that appears AFTER fingerprinting is caught.** Attestation only ran when a hash had
  been recorded, so a `--json-schema` or `.agents/memory.md` that did not exist when the identity
  was built, but did by the time it was used, shaped the run while contributing nothing to the
  identity naming it.
- **`--allow-credit` reaches the environment identity.** The flag authorizes the run but only sets
  its env var later, so the identity hashed the unauthorized (stripped) environment while the
  authorized child received the credit-only remap.
- **Same-origin redirects normalize the default port.** `https://host/v1` to `https://host:443/v2`
  is the same origin and was being refused as cross-host, contradicting the stated guarantee.
- **An agent definition file's frontmatter can no longer be misread into a stronger permission.**
  Found by dogfooding summon's own manifest fan-out as a cross-vendor audit swarm over its
  under-reviewed modules; each fix ships a regression test verified to reproduce the pre-fix failure.
  - A **UTF-8 BOM** (added by any Windows editor that saves "UTF-8 with signature") hid the opening
    `---` fence, so the whole frontmatter was skipped: `run-agent` came back `None` and `permission`
    silently fell back to the `safe-edit` default even when the file declared `read-only`. Agent
    files are now read as `utf-8-sig`.
  - A **duplicate frontmatter key** silently last-wins, so `permission: read-only` followed later in
    the same block by `permission: yolo` ran as `yolo`. Ambiguous frontmatter is now rejected.
  - An agent file with **undecodable bytes** raised an uncaught `UnicodeDecodeError` traceback
    instead of the JSON error envelope the stdout contract promises.
- **Windows paths in an agent's `args:` are no longer mangled.** POSIX `shlex` rules ate the
  separators, so `args: --config C:\temp\foo` reached the backend as `C:tempfoo` -- a silently wrong
  path, not an error. Backslashes are made literal on Windows without losing the inner-quote
  stripping codex args depend on (`-c model_reasoning_effort="high"` still yields `...=high`), and
  UNC paths keep both leading separators. POSIX splitting is unchanged.
- **`--timeout 1e308` is rejected instead of overflowing.** The value is finite and positive, so it
  passed validation and became a 309-digit millisecond count that blew up far downstream as an
  `OverflowError` inside the executor's wait. Durations above 7 days are now an argparse error.
- **An externally killed dispatch is `partial`, not `success`.** A plain-text backend (no parsed
  terminal event) that was SIGTERMed from OUTSIDE summon -- a host-tool timeout, a CI cancel, a
  `docker stop` -- exited `-15`/`143` and, because those codes are in the success set, was reported
  `success` with whatever half-answer it had emitted. `is_terminal_success()` would then let `--out`
  and manifest resume SKIP the re-run, persisting the truncated answer. SIGTERM still counts as
  success on the branch that summon itself terminates (after a terminal event has been parsed);
  without that evidence it is now `partial`, with the output preserved.
- **A manifest job's roster is the one its own `cwd` selects.** A job with its own `cwd` loads its
  agent from that tree's `.agents`, but the scheduler resolved every job against the manifest's
  base roster -- so it could pick a different backend than the child dispatched to, bypassing that
  backend's concurrency cap.
- **An unpinned codex agent's configured default model is part of the request.** A codex agent with
  no `model:` takes its model from `~/.codex/config.toml`, so editing that file changes which model
  answers while the request looked unchanged, and the previous model's answer was served as
  current. Consulted only when nothing else pins a model -- not `--model`, not the definition's own
  `model:`, and not a selector in its `args:` (`-m`, `--model`, `-c model=`) -- so an agent that
  pins its model is never invalidated by a config edit for a model it does not run.
- **An environment control that cannot affect a request no longer invalidates it.** Credit
  authorization only changes the model on the `claude` backend, and `SUMMON_DEFAULT_EFFORT` only
  applies when no `--effort` was given; folding either in regardless made unrelated environment
  changes force fresh, paid dispatches.
- **A malformed provider entry is a clean error.** `{"tenant": {"base_url": 42}}` reached
  `.rstrip()` and raised `AttributeError`, which only the last-resort crash envelope caught.

  The CREDENTIAL counts, not just the name of the variable holding it. Recording only
  `api_key_env` meant two runs differing solely by which token was in `TENANT_TOKEN` fingerprinted
  identically, so one tenant's answer could be served to the other without their endpoint ever
  being called. What is stored is a one-way SHA-256 over a domain separator, the variable name and
  the value -- not the value. It cannot be reversed, and confirming a guess would require already
  holding a candidate token. It is written only where the result envelope goes, so treat a
  `--out`/`--results-dir` on synchronised or shared storage as carrying it too. summon's rule that secrets stay in the
  environment and out of artifacts is about the SECRET; a digest is not the secret, and the
  alternative was a wrong answer.

  Backend-native configuration read from the ENVIRONMENT counts too. A vendor CLI reads its own
  settings from the environment the child inherits, so `ANTHROPIC_BASE_URL` (or an API key, or a
  model override) could point the same summon request at a different endpoint or account with no
  summon flag changing, and the previous tenant's answer came back without the new one being
  called. Enumerating variables one vendor at a time was a losing game, so the rule is per-backend
  PREFIXES (`ANTHROPIC_*`, `OPENAI_*`/`CODEX_*`, `CURSOR_*`, `GEMINI_*`/`GOOGLE_*`) with every
  matching variable counted and values hashed one-way -- and then summon's OWN delta applied, so
  what is hashed is the environment the CHILD receives rather than whatever is merely named like a
  vendor's variables. That distinction is load-bearing: summon forwards `CLI_API_KEY` as
  `CURSOR_API_KEY` and strips `OPENAI_API_KEY` unless `SUBAGENTS_ALLOW_OPENAI_KEY=1`, so a changed
  Cursor key or a toggled Codex key changed the child's auth while the fingerprint stayed equal,
  and changing a stripped key invalidated results whose execution was identical. The builders and
  the identity derive that delta from one function. It is deliberately coarse: an unrelated
  variable under one of those prefixes will invalidate a stored answer, which is the safe
  direction, since a needless re-dispatch costs a run and a missed one returns the wrong answer.

  The endpoint the dispatch calls is the one that was fingerprinted. Resolving it separately for
  the identity and for the call meant a `providers.json` edit in between sent the work to one
  endpoint while stamping it as another -- and restoring the first then let the second's answer
  resume as its own. The identity carries the snapshot it resolved and the dispatch uses that pair.
- **Only the provider endpoint actually resolved is fingerprinted.** Hashing `providers.json`
  wholesale refused a perfectly good codex answer whenever an unrelated registry was touched,
  invalidated an agent with an inline `base_url:` (which never consults the registry at all), and
  invalidated a `tenant-a` agent when only `tenant-b` changed. The identity carries the resolved
  endpoint, which is what would actually change the answer, and only for `openai-compat`. An
  endpoint that cannot be RESOLVED at all (an agent naming a provider that no longer exists) is
  never reused: swallowing that into a missing field let a pre-fingerprint envelope hand back an
  answer from the old endpoint instead of letting the dispatch report the unknown provider.
- **A manifest job's backend is resolved the way dispatch resolves it.** The scheduler picked its
  concurrency slot with `run-agent or codex`, but an UNPINNED agent takes its backend from caller
  detection -- so under `CLAUDE_CODE=1` every such job dispatched to claude while being counted as
  codex. Claude's `--concurrency` cap was bypassed entirely and the per-backend telemetry named the
  wrong vendor.
- **The schema and project memory that run are the ones that were fingerprinted.** Both are hashed
  for the request identity and then read again -- the `--json-schema` to validate the result,
  `.agents/memory.md` to build the system context -- so a change in between meant validating against
  a contract, or running under instructions, that the envelope did not name. Both are now attested
  against the recorded hash, and the schema is parsed from the same bytes it is hashed from.
- **The agent definition that runs is the one that was fingerprinted.** The identity hashes the
  definition and the dispatch loads it again afterwards, so replacing the file in between ran one
  definition's model, permission and system context while stamping the envelope with another's
  request hash -- and restoring the first would then let the second's answer resume as its own.
  The dispatch compares the bytes the loader actually PARSED (not a fresh read of the path, which
  left an A-to-B-to-A window open) against the hash it recorded, and refuses rather than produce a
  mislabelled result. Same shape as the openai-compat endpoint snapshot and the agy
  account attestation.
- **A refused resume no longer destroys the answer it refused, or dispatches over it.** The
  manifest cleared a job's stored envelope BEFORE re-dispatching, so any wrong refusal turned a
  completed answer into an error envelope with nothing to fall back to -- and a clear that FAILED
  was swallowed, leaving the stale envelope at the authoritative path where the parent re-read it
  and reported the OLD answer as this run's result, with exit 0. A prior success is now ARCHIVED
  A pre-dispatch failure is written to `--out` as well, since that path is authoritative and a
  failure recorded nowhere is worse than none -- but it ARCHIVES whatever was there first. Writing
  the error straight over the path destroyed a stored success whenever the failure came before the
  resume block could preserve it (an early `--resume` + `--worktree` rejection, say).

  (`<id>.json.superseded`, the name CLAIMED with `O_CREAT|O_EXCL` so two runs sharing a
  `--results-dir` can never pick the same one and overwrite each other's archived answer) instead
  of deleted, and a clear that fails is a job error that dispatches nothing. Losing that race is
  success, not failure: the goal is an empty authoritative path, and a concurrent archiver having
  cleared it first satisfies it.

  An agent definition that is MISSING or MALFORMED means the request cannot be matched against
  anything, and the dispatch is the only thing that can report it -- so neither is reused, and that
  holds for pre-fingerprint envelopes too. Having one answer for old envelopes and another for new
  ones was a contradiction whose lenient half handed back results nobody could attribute.
- **An input that exists but cannot be hashed fails closed.** A hash failure was reported as plain
  `None`, which dropped the field from the fingerprint entirely -- so two DIFFERENT unhashable
  schemas produced the SAME fingerprint and one could be served as the answer to the other.
  "Absent" and "present but unidentifiable" are now distinguished, and the latter is never reused.
- **A manifest job's fields are type-checked before anything uses them.** A list-valued `cwd`
  reached `os.path.abspath` during identity construction and a list-valued `prompt_file` reached
  `os.path.isabs` -- both outside the per-job error handling, taking down the whole manifest with a
  `TypeError` instead of producing one job's error. Strings only: a numeric `cwd` is not a path, and
  accepting one contradicted the very message the check raises. A non-mapping `defaults` block is
  rejected too, rather than raising when it is merged into the first job.
- **An invalid agent name reaches the dispatch instead of being resumed over.** A path-traversing
  name like `../escape` was classified as merely "missing", so a pre-fingerprint envelope could be
  reused and the loader's own "Invalid agent name" was never surfaced.
- **`set-agent` accepts a BOM'd agent file.** The loader reads agent files as `utf-8-sig`, so a
  BOM'd file dispatched fine, but `agent set` rejected the very same file as "not ---delimited" --
  an inconsistency the user had no way to act on. The BOM is dropped on the way through rather than
  re-emitted, which also removes the byte behind the original silent-permission-fallback bug.
- **`set-agent` validates before it writes.** The result was parsed only AFTER the atomic
  replace, so an update that the parser then rejected had already been committed: the user was
  left with a mutated agent file and a failed command.
- **A typo in a frontmatter key is an error, not a silent default.** `permisson: read-only` was
  ignored, leaving `permission` at the stronger `safe-edit` default -- the same silent-escalation
  shape as the BOM bug. A key that is a near-miss of one summon reads is now rejected with a "did
  you mean" pointer; unrelated keys are still accepted and ignored, so an agent file can carry its
  own metadata. The comparison is case-normalized, since `PERMISSION: read-only` otherwise scored
  no match at all and escalated exactly like the lowercase typo. It stays a heuristic: an unusual
  but intended key can be caught (rename it) and a wild typo can still slip through.
- **One malformed agent file no longer crashes `--list`.** Making duplicate keys a hard error meant a
  single bad file took down the whole roster listing, hiding every other agent. Discovery is
  fail-soft again: the broken file is listed with an empty description.
- **A manifest job's `timeout` obeys the same ceiling as `--timeout`.** It is parsed independently,
  so `1e308` bypassed the dispatcher's cap and sized the PARENT watchdog to ~1.5e305 seconds, which
  raised `OverflowError` the moment it became a deadline -- killing the parent while its child ran
  on unmanaged. Non-finite and over-cap values are clamped.
- **The `openai-compat` endpoint lookup is BOM-tolerant too.** It re-reads the agent file to resolve
  `base_url`, and did so as plain UTF-8 while the loader had moved to `utf-8-sig`, so a BOM made the
  dispatch fail with a misleading "needs a provider or base_url".
- **`--out` / manifest resume no longer returns a stale answer as a fresh one.** The skip was keyed
  on the result file's PATH alone, so editing a manifest job's prompt while keeping its `id` came
  back `skipped` with the previous prompt's answer -- and pointing the same job at a different
  model did too. Every dispatch now stamps a `request_sha256` over the inputs that can change the
  answer (agent, prompt, cwd, cli, model, effort, and the json-schema by content), and BOTH resume paths
  compare it: the dispatcher's `--out` skip and the manifest PARENT, which short-circuits before
  spawning and so never reached the child's check. Timeout, retries and debug-dir are deliberately
  excluded, so raising a timeout does not force a re-pay. An envelope written before 0.10.2 has no
  fingerprint: it is still honored (unless the fields it does carry PROVE a difference), but the
  emitted envelope says the match was not verified (unless the fields it does carry PROVE a
  difference, which both paths honor identically). `--resume`, `--worktree` and `--allow-credit`
  count too: they continue a different conversation, run against a different tree, and change the
  effective model. A `--json-schema` is fingerprinted by its CONTENTS, since editing schema.json in
  place is a different contract for the same filename.

  The agent DEFINITION counts too, by content: editing an agent's `model:` or its instructions
  makes a stored answer stale, and neither the agent name nor the roster path shows it (a
  `SUB_AGENTS_DIR` pointed at a different roster resolves the same relative name). Hashing content
  rather than the directory is also the more correct identity, since two rosters holding a
  byte-identical definition really are the same request -- so the lexical roster path is NOT in the
  fingerprint, which would have contradicted that and re-paid for a moved roster. `.agents/memory.md`
  is in as well, since it is injected into the agent's system context: editing it changes the
  instructions an answer was produced under while every flag stays identical. So are
  `--resume-profile` and the env-backed controls `SUMMON_ALLOW_CREDIT` / `SUMMON_ALLOW_FABLE` /
  `SUMMON_DEFAULT_EFFORT`, which change the effective model and effort without ever being flags.

  A bare `--worktree` auto-names a fresh tree per run. A deterministic hash cannot express "never
  the same twice", so it is closed from both directions: the identity carries an `<auto>` marker
  (a bare-worktree result is not reusable by a plain-cwd run) and the skip additionally refuses the
  bare form (two bare runs do not reuse each other). A NAMED worktree is a stable location and
  resumes normally.

  Both resume paths are now built by ONE function. Each used to assemble its own identity dict, so
  a field added to one and not the other went unnoticed until resume silently misbehaved -- which
  happened twice: the child gained the env-backed controls while the manifest parent did not (with
  `SUMMON_DEFAULT_EFFORT` set, every manifest restart re-dispatched every finished job), and the
  parent was passed a narrower legacy fallback than the child. The two sides now hand raw inputs to
  a shared builder that owns every derived field, and a regression test parses the real child argv
  and requires the two fingerprints to match across a matrix of job shapes and environments.

  The identity describes the EFFECTIVE dispatch, not the raw flags. With no `--cli` and no
  `run-agent:` pin, the backend comes from caller detection, so the same command under
  `CLAUDE_CODE=1` and under `CODEX_CLI=1` goes to two different vendors -- those hashed alike, and
  the second run could reuse the first backend's answer. Credit authorization uses the exact same
  predicate dispatch uses -- which accepts only the literal `"1"`. The identity had counted ANY
  non-empty value as authorization, so `SUMMON_ALLOW_CREDIT=0` and `=1` hashed identically while
  selecting different effective models. An `openai-compat`
  agent's `provider:` resolves through a `providers.json` outside the agent file, so that registry
  is folded in too -- retargeting a provider sends the work elsewhere while agent, prompt and the
  definition's own bytes stay identical.

  A file hashed for the fingerprint is opened `O_NONBLOCK` (so a FIFO cannot block at `open`),
  gated on being a regular file, read in chunks so memory is bounded by the chunk rather than the
  file, and re-`fstat`ed afterwards -- a file rewritten mid-read can produce a hybrid digest
  matching neither version, so an unstable file reports no content identity rather than a wrong
  one. The read deadline guards NON-TERMINATION (a file being appended to faster than it can be
  read), not slowness: a healthy file on a slow share still hashes, where tripping on elapsed time
  alone had called it unreadable and made every resume against it pay for a fresh dispatch. An input that exists but cannot be hashed is never reused, since a missing hash is a hole in
  the identity and a hole is not a difference. The stability check catches accidental mutation, not
  a deliberate adversary, which matches summon's trust model where files under `--cwd` are trusted
  operator input.

  WHAT THE FINGERPRINT COVERS, exactly: the request as summon received it (agent, prompt, cwd, cli,
  model, effort, `--resume`, worktree, credit authorization, and `--resume-profile` only during an
  actual resume); file CONTENT -- not path -- for what summon
  reads itself (the agent definition, the `--json-schema`, `.agents/memory.md`; the same schema at
  two paths is the same contract, and the roster directory is likewise not part of the identity); what summon resolves (the
  effective backend including caller detection, the openai-compat endpoint and its credential,
  cursor's default model, codex's configured default when nothing else pins one, and the credit
  guard's own substituted fallback model); environment
  variables matching the resolved backend's PREFIXES (`ANTHROPIC_*`, `OPENAI_*`/`CODEX_*`,
  `CURSOR_*`, `GEMINI_*`/`GOOGLE_*`/`AGY_*`) after summon's own delta, minus any the dispatch
  overwrites for every run (`AGY_PTY_DEADLINE`, `GEMINI_SYSTEM_MD`) and normalizing the ones it
  DEFAULTS (`AGY_PTY_QUIET`, so unset and set-to-the-default hash alike); and for agy the account
  files of the profile the
  run will actually use -- the freshly copied one, or the one a `--resume-profile` resumes. It does NOT cover the vendor CLI's own installed state --
  its config beyond codex's `model`, its stored credentials, its signed-in account, its VERSION --
  nor environment outside those prefixes (an inherited `HTTP_PROXY`, say), nor summon's OWN version
  and unlisted built-in defaults -- upgrading summon can change what a re-run would produce without
  invalidating a stored answer, which is deliberate, since pinning the dispatcher would invalidate
  every stored result on every upgrade (`summon.version` and `summon.scripts_sha256` are in the
  envelope for a caller who wants to enforce it). A sub-agent's answer depends on the whole installation
  behind the CLI, and summon cannot enumerate that. So a matching fingerprint means "the same
  request, as summon defines a request" -- not "the same answer is guaranteed". If you need more
  than that, do not resume: delete the result file, or give each configuration its own
  `--out`/`--results-dir`.

  ONE thing stays outside on purpose: the repository state under `cwd`. Folding git HEAD in would
  invalidate every stored result on any unrelated commit, costing more than the staleness it
  prevents. It is recorded in the envelope as `git_head_before` for a caller that wants to enforce
  it.
- **An apostrophe in a Windows path gives an actionable error.** `args: --path C:\Users\O'Brien\config`
  failed with a bare "No closing quotation", because POSIX splitting reads the apostrophe as an
  opening quote. Single-quote grouping is kept (long-standing behavior some rosters rely on), so the
  error now names the cause and shows the double-quoted form that works.
- **Case-colliding manifest job ids are rejected.** `Foo` and `foo` are distinct ids but one
  `<id>.json` result file on Windows and macOS, so the two jobs overwrote each other's result and
  the second resumed off the first's envelope. Ids are now compared case-insensitively on every
  platform, so a manifest that works on Linux works everywhere.
- **Abbreviated flags no longer bypass the fan-out flag matrix.** argparse prefix matching accepted
  `--mod opus` for `--model`, but the matrix scans the raw argv by literal flag name -- so an
  abbreviation slipped past the "rejected, never silently dropped" guarantee and was then dropped
  anyway. The parser now runs with `allow_abbrev=False`; spell flags out.

## [0.10.1] - 2026-07-23

### Fixed
- **A malformed member or chairman envelope can no longer abort a council.** The 0.10.0 member-status
  allowlist introduced (and adjacent code already had) several paths where one unexpected JSON type
  from a child raised an uncaught exception mid-run, losing an in-progress or even an
  already-synthesized council. Found across five cross-vendor review rounds and each fixed with a
  regression test verified to reproduce the pre-fix failure:
  - a non-string `status` (list/dict) raised `TypeError: unhashable type` on the allowlist test;
  - a non-mapping `model`/`report` raised `AttributeError` in `_model_label()`/`_position()`;
  - a nested non-string `billing.source` raised building the billing set or in `sorted()`, and the
    aggregation's `A | B - {None}` bound `-` tighter than `|` (stripping `None` from only one side);
  - a scalar `warnings` on a CHAIRMAN envelope raised `TypeError: not iterable` AFTER synthesis
    (chairman envelopes bypass the member normalization, so they are normalized at the aggregation);
  - `_model_label()` could return a non-string, violating its `str | None` contract, and a malformed
    truthy `served` masked a valid `resolved` fallback.
  Member envelopes are sanitized once in `_member_view` (status allowlisted, model/report/billing
  shape-checked, warnings coerced to a list, position/`_raw` stringified); chairman envelopes and the
  emitted `synthesis` fields are normalized where they are consumed.

## [0.10.0] - 2026-07-23

Council fault-tolerance flags, install-hygiene duplicate detection, an internal extraction of the
kill machinery, and a trust-boundary hardening. Each landed behind a cross-vendor adversarial
review (codex); the newest set also went through abstract- and named-persona review passes.

### Added

**Council fault-tolerance (`--overall-timeout`, `--min-successful-members`)**
- `--overall-timeout <ms|Ns|Nm>` caps the WHOLE council's wall clock. On a breach a daemon
  watchdog process-tree-kills every in-flight member (so returning never blocks on their own child
  watchdogs, the host exit-124 this prevents), members that would start after the deadline are
  excluded without dispatching, and a PARTIAL envelope (`council_state: overall_timeout`) is
  emitted. The deadline is the authoritative monotonic clock, not merely the async flag, so a
  starved watchdog can never wave a paid child through.
- `--min-successful-members N` (early-exit): once N members SUCCEED in the final round, stop
  waiting: process-tree-kill the in-flight stragglers, self-exclude the queued ones, and chair the
  surviving quorum immediately (a pre-deadline happy path that complements `--overall-timeout`).
  Validated `2 <= N <= members` and `N >= --quorum`; last-round-only; checkpointed and resumable.
  Adds a `council_state: early_exit` envelope block.

**Install hygiene: duplicate 'summon' skill detection**
- `--doctor` and `install.py` now DETECT when a host would load more than one 'summon' skill (a
  stale `summon.pre-refresh-*` backup a pre-v0.9 refresh left beside the live skill, a hand-copied
  dupe), which the hash-based drift check alone could not see. Each duplicate is reported with its
  exact path plus a ready-to-run, QUOTED, platform-appropriate removal command
  (`Remove-Item`/`rm -rf`). DETECTION-ONLY: summon never auto-deletes a directory (a portable,
  TOCTOU-free recursive delete of a dir another process could swap is not achievable in stdlib, and
  an install script must not risk a user dir). An incomplete scan (size cap or read error) blocks a
  "converged" verdict rather than silently passing.

**Docs**
- `references/models.md`: the `cursor-agent` backend documented as a cross-vendor MODEL GATEWAY.
  With a Cursor subscription, `--cli cursor-agent --model <id>` reaches GPT-5.x, Claude, Gemini,
  Grok, GLM, and Kimi (summon forwards `--model` verbatim), a fallback that needs no per-vendor
  key. Covers billing (draws from the sub's included usage), the `NO ZDR` model flag, and the
  login-required failure mode.
- `references/fan-out.md`: large-file council guidance and an `--overall-timeout` budget example.

### Changed
- **Internal:** the overall-timeout / kill / early-exit machinery (~170 lines) was extracted out of
  the 900-line `run_council` into a cohesive `_KillRegistry` class (Thompson "do one thing well").
  Behavior-preserving: the method bodies are verbatim from the former closures, verified
  byte-for-byte and codex-CLEAN.
- The doctor and installer duplicate guidance now print a QUOTED removal command (an unquoted path
  with spaces on Windows would delete the wrong directory).

### Security
- **Member-status allowlist (defense-in-depth).** A council normalizes any status outside
  `{success, error, partial, blocked, excluded}` in an ingested member env to `error`, with a
  warning trail, so a garbled or rogue-producer status can never be shaped into a false
  `success`/`excluded`. This is NOT tamper resistance: the run dir is trusted operator input, and a
  forged `status: "success"` (a known value) is still accepted by carry_forward's success-gate;
  true authenticity would need signing and is out of scope for a trusted-operator run dir.

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

**Background job registry (read path)**
- `--background` now writes a durable launch record (fsynced) BEFORE the child spawns, so
  a job that dies before writing its result is still traceable to what was launched. The
  child stamps a `job_nonce` into its result envelope (on the normal AND crash paths) so a
  result at a job's path can be authenticated against its record.
- `--job-dir` / `SUMMON_JOBS_DIR` control where records and results land (default
  unchanged); records live in a `.summon-records/` subdir so a result glob never trips
  over them.
- `jobs list` / `jobs status <id>` / `jobs wait <id>` are read-only registry commands
  (flat `--jobs-list` / `--jobs-status` / `--jobs-wait`). State machine: `prepared`,
  `running` (pid known, not asserted alive), a terminal status, `unverified` (nonce
  mismatch or a legacy result), or `corrupt` (a record or result that exists but is
  unreadable, or an authenticated result with a malformed envelope). An unverifiable or
  corrupt result is never reported `trusted`, and a corrupt job still enumerates in
  `list` rather than vanishing. The registry is single-user and single-machine
  (documented; it does not defend against other local users on a shared host). Records
  and results are written whole (never a zero-byte window) and a symlink planted at a
  record/result path is refused rather than followed. Liveness verification, cancel, and
  reaping are a later addition.

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
