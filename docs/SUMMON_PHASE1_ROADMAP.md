# Summon Phase 1 roadmap

Status: executable plan endorsed by native Sol after exact Opus 5, Kimi K3,
the temporary Ox-Alpha alias, and named-persona adversarial participation. The
alias was later identified as GLM-5.3 Flash and is not assumed to remain available
or free.

Phase 1 makes Summon's existing dispatch, provenance, role-alias, chat, council,
deliberate, and swarm primitives easier to operate. It borrows useful control-plane
ideas from `delegate-skills` without adding parallel sources of routing authority or
weakening Summon's evidence model.

This phase begins by reconciling the already-tested Phase 0 candidate. Provider
calls, credential repair, deployment, publication, and automatic spending remain
separate governed actions. The delivery sequence below is binding: a later milestone
cannot bypass an earlier trust or compatibility gate.

## Executive delivery contract

Phase 1 is complete only when milestones M0 through M9 are implemented, reviewed,
documented, committed, and installed from one tested source identity. Publication is
a separate terminal operation after a fresh Fable endorsement of the exact release
candidate.

Every milestone follows the same loop:

1. record the source commit, index/worktree state, relevant digests, and test baseline;
2. write or freeze provider-inert contract tests before behavior changes;
3. implement one bounded slice through the canonical evidence and decision path;
4. run focused, negative, privacy, Windows, and backward-compatibility tests;
5. stage only an explicit reviewed file allowlist and inspect the complete cached diff;
6. obtain the required fresh model-review quorum and locally validate every finding;
7. fix blocking findings, rerun tests and review, then make one scoped commit;
8. prove an empty index and clean worktree before starting the next milestone.

No milestone may use a second routing authority, fabricate served-model evidence,
silently contact a provider, repair authentication, broaden permissions, authorize
PAYG, or write host instruction files. Review artifacts containing prompts, raw
results, account data, credentials, or workstation paths stay outside public history.

## User outcomes and acceptance signals

1. **Understand a route before spending.** A caller can ask why a lane or exact
   seat is eligible, what constraints win, and what remains unknown. Acceptance:
   one provider-inert command emits the effective requested lane/seat, resolution
   source, permission ceiling, spend boundary, roster/policy digests, and losing
   rules without dispatching or changing legacy exact-agent resolution.
2. **See account availability without false precision.** A caller can inspect
   fresh, provider-reported usage dimensions and their age. Acceptance: the UI and
   JSON distinguish subscription allowance, API balance, account credit, and rate
   limit; incomparable or stale observations never produce a scalar ranking.
3. **Spend fewer prompt tokens without changing authority.** A caller can preview
   and disable safe payload compaction. Acceptance: every authority turn stays
   byte-identical and ordered, every payload transformation is mapped and
   target-resolvable, and measured prompt-token savings are reported on a frozen
   corpus.
4. **Know why an agent is quiet.** A caller sees typed startup, generation,
   reconnect, tool, finalization, and terminal states. Acceptance: trusted
   transport events—not model prose—drive a small set of measured watchdogs.
5. **Hand results to another orchestrator safely.** A consumer receives a compact
   compatibility projection without local path or identity leakage. Acceptance:
   one real external consumer validates the projection before its schema is frozen.

## Non-negotiable invariants

- Exact provider/model requests, roster authority, permission ceilings, data
  boundaries, and explicit spend consent override all advisory preferences.
- Unknown usage, quota, price, capability, or served-model identity remains
  unknown. A catalog entry, target, handshake, or token count is not proof.
- Usage queries are read-only, bounded, redacted, and opt-in when they contact a
  provider. Authentication repair is a separate explicit action.
- Advice never silently dispatches, retries, changes providers, or authorizes
  PAYG/credits.
- Authority turns—system, developer, user, approvals, boundaries, and report
  contracts—are immutable, ordered, and never deduplicated or externalized.
- Raw prompts, results, credentials, account identifiers, and local paths do not
  enter public configuration, telemetry, or release evidence.
- New configuration follows propose -> inspect -> approve -> write and never edits
  host instruction files.
- Existing exact-agent, role-alias, strict-roster, permission, and legacy CLI
  behavior remains authoritative and backward compatible.

## One resolver and one effective decision receipt

Phase 1 does not create independent routing authorities. Existing roster seats and
approved role aliases remain canonical. A future lane is an explicitly namespaced
input (`--lane NAME`), never an implicit agent name or alias.

One provider-inert resolver combines an exact agent or lane request with the
existing roster, approved aliases, permission ceiling, policy constraints, usage
observations, and spend authority. It emits one immutable effective decision receipt
containing:

- requested lane/agent, resolved seat, resolution source and precedence;
- roster, role-approval, policy, and project-identity digests;
- permission ceiling and effective data boundary;
- effective spend boundary plus authorization source and scope;
- each candidate's usage dimension, freshness, comparability, and losing rule;
- the winning rule, unresolved unknowns, and `provider_contacted: false`.

Policy may constrain a roster result but cannot rewrite its provider/model identity
or raise authority. Context carries no routing authority.

## Workstream A: route and lane explanation

First expose the current exact-seat resolution as a provider-inert explanation.
Then specify `summon.fleet/v1` as named lane requirements that resolve through the
same path. The first slice supports `fleet propose`, `validate`, `inspect`, and
`explain`; it cannot select or approve a route. The next provider-inert slice records
authenticated, expiring approval for an exact compiled lane but deliberately cannot
consume that authority by itself. A separately reviewed activation boundary can consume
one exact approval for one single-candidate foreground subprocess attempt after complete
revalidation; it has no retry, fallback, repair, resume, background, worktree, or authority
expansion. This first catalog admits active seats only. Deprecated seats remain an
explicit compatibility-dispatch concept, while retired seats fail in the shared roster
boundary before every provider-launch surface.

Approval is not a bare content hash. It binds schema version, content digest,
canonical project identity, selected roster digest, approval generation, and a
private local approval-store identity. Copying or retargeting the same bytes requires
new scoped approval. The private store and each approval are authenticated separately;
mutations require compare-and-swap against the current generation, expiry is mandatory,
and public receipts redact the store identity, local actor identity, MAC, key, and path.
Idempotency includes the requested lifetime: a different lifetime creates a distinct
issuance, while an exact active replay returns the existing issuance without mutation.
Expired and revoked entries remain inspectable until the next approval mutation, which
compacts them before recording new authority. New authority also reserves enough bounded
store bytes and generation capacity to revoke every active approval. Private path overrides must be absolute, and Summon
never claims a nonempty unsafe directory based on expected filenames; the documented
recovery path for a generation conflict is status then deliberate retry, while clock
rollback requires correcting the system clock.
Recorded approval remains `recorded_not_activated`; only the separate one-time reservation
and dispatch-consumption boundary can activate it. Exact agent names retain precedence,
and lane selection requires the explicit `--lane` namespace. Collision and legacy-flat-
form tests are mandatory.

## Workstream B: usage and credit awareness

Add a capability registry for provider usage sources. Each observation records:

- support state: `supported`, `unsupported`, `unavailable`, or `unknown`;
- observation source: local CLI, authenticated provider endpoint, or cache;
- observation and retrieval times, source latency, and an empirically justified TTL;
- normalized value and reset only within its vendor-defined dimension;
- dimension: subscription allowance, API balance, account credit, rate limit, or
  unknown.

Dimensions are displayed separately. Cross-dimension and cross-provider values are
`incomparable` unless a documented adapter proves common semantics; no combined
remaining fraction exists. Explanations use plain reasons such as `stale`,
`category_incomparable`, `exact_pin_preserved`, or `paid_route_not_authorized`.

`summon usage status` is provider-inert and reads a bounded local cache. A
provider-inert, schema-validating `usage import --from FILE` supplies a complete
workflow for operator-exported redacted observations. `usage export` writes a portable,
de-attested snapshot, and `usage example` creates deterministic synthetic evidence.
The first explicit live adapter supports a bounded Codex account read only when both an
allowlisted provider and `--allow-account-usage-read` are present. It validates the exact
CLI/version and request plan and never logs in, repairs auth, prints identifiers,
dispatches, retries, or alters routing. AGY and ArkCLI remain schema-unverified until
version-pinned fixtures and redaction mappings are reviewed.

Usage is advisory. The effective decision receipt records the exact request,
permission ceiling, spend boundary, authorization source, comparability result, and
winning rule. Stale or unknown usage cannot exclude a candidate or prove eligibility.

## Workstream C: liveness and terminal contracts

Start with three measured budgets: overall wall time, first trusted transport event,
and no trusted meaningful-generation event. Backend-specific diagnostics may label
tool, reconnect, and finalization activity, but do not become separate user knobs
until telemetry demonstrates a need.

Only allowlisted executor or transport events may reset a clock. Natural-language
progress never counts; unknown provenance cannot extend a deadline. Fixtures cover
forged, reordered, duplicate, and slow-drip events plus reconnect loops, zero-token
finishes, partial Kimi output, OpenCode step events, background-child death, timeout,
cancellation, and terminalization races. No watchdog silently retries or switches a
provider.

An explicit governed-continuation slice follows the watchdog foundation. It is not an
automatic retry: one authenticated terminal job, one private provider-reported handle,
one explicitly claimed steering generation, and one fresh physical attempt form an
at-most-once chain. Initial activation is provider-specific and opt-in. It requires exact
workspace and authority continuity, fresh gate/spend consent, and reported exact-model
evidence; unsupported backends fail before contact. Detached subprocesses do not support
acknowledged live steering, so `jobs steer` remains `queued_for_resume` until a backend
exposes and proves a real bidirectional injection API.

## Workstream D: safe payload efficiency

The compiler has two planes:

- **authority plane:** every instruction/approval/boundary/report-contract turn is
  preserved byte-for-byte and in order;
- **typed payload plane:** only explicitly typed immutable artifacts, validated
  reports, and diagnostic tails are eligible for mechanical compaction.

The pure compiler preserves already-authenticated authority for trusted host adapters,
but the ordinary `--context-input-file` surface is payload-only: a caller-controlled
file cannot mint system, approval, permission, spend, or report-contract authority.

The safe profile may deduplicate byte-identical typed payload blocks, replace an
artifact body with a content-addressed reference only after a compile-time final-handle
readback, and emit the expected digest plus a cwd-relative locator. The receiver must
re-hash before use because the file is not immutable across processes. The first
dispatch adapter refuses worktree, additional-root-only, and text-only-backend
references until a stronger receiver-verifiable locator exists. It may omit raw tool chatter only when a validated
report and receipt fully represent it, and bound diagnostic tails while retaining
typed errors. The compiler itself never reuses provider sessions. Governed continuation
is a separate, explicit operational-recovery path and never becomes an implicit context
optimization. Lossy semantic summarization remains a future opt-in profile. Resumed
sessions and lossy summaries are both prohibited for final adjudication.

Dry-run emits a source-to-output mapping, target-resolvability proof, retained
evidence, lineage digest, and estimated bytes/tokens. Optimization refuses before
provider contact if resolution cannot be proven. `--context-profile off` uses the
unmodified input. A frozen corpus measures actual serialized prompt tokens; estimates
are compared with provider-reported input tokens where available, without treating
missing provider usage as zero.

## Workstream E: context freshness

Do not introduce a second context vocabulary. Generalize the existing deliberation
canonical context packet only after the payload compiler needs it. Durable context
contains decisions, constraints, rationale, and artifact references, never routing
authority. It is bound to a source revision/digest and freshness policy.

Stale acceptance is one-run authority bound to actor, packet digest, source revision,
reason, and scope. The UI shows the mismatch and affected constraints, and the state
remains `accepted_stale`; it is never relabeled fresh. Transcript or tool-log
promotion is explicit, bounded, and reviewable.

## Workstream F: portable result projection and native adapters

The rich private envelope stays authoritative. A compact projection is not frozen as
v1 until one real external consumer validates it. The experimental-1 slice publishes
content hashes and bounded metadata only; mutable path locators are omitted. A later
locator schema requires handle-relative traversal on both POSIX and Windows before it
may expose repository-relative paths. Drive, UNC, device, absolute, traversal,
malformed, and symlink-escape input paths never reach the portable receipt.

Native Codex/Claude/IDE adapters remain explicit host integrations. External CLI
agents are not called native subagents unless the host exposes and attests that
relationship. Swarm adapters use the existing lease/message protocol and do not add
a second coordinator.

## Executable milestone plan

### M0 — Reconcile the baseline and freeze compatibility

- Preserve the verified OpenCode/Kimi/provenance working bytes and classify every
  dirty hunk before staging it. Remove generated review packets only after their
  decisions have been captured.
- Register every new test in CI and the fixed release-evidence registry.
- Freeze byte-for-byte legacy flat-CLI and `--context-profile off` goldens before
  compiler work.
- Record the earlier Windows loopback abort as a transient baseline observation;
  release still requires a fresh full-suite pass.
- Commit the reconciled candidate through explicit staging, converge only
  Summon-owned installs, and finish with a clean worktree.

### M1 — Canonical evidence family and pure decision kernel

- Introduce one versioned, canonical, hashed evidence family. Include
  `schema_version` in each digest preimage and fail closed on unknown versions,
  duplicate keys, malformed composites, and non-finite or unbounded numerics.
- Reuse the existing roster digest and path validator. Do not create competitors.
- Extract the sole decision authority into a pure module with no provider, process,
  network, installer, executor, or dispatch capability.
- Record the winning rule and every losing rule, including gate, permission,
  read-only, corrective-action, retry/fallback, freshness, and spend constraints.
- Represent enforcement as `enforced`, `unenforceable`, or `unknown`.
- Enforce the no-dispatch boundary with import and AST tests in CI.

### M2 — Trusted liveness and watchdogs

- Define trusted liveness before fleet execution. Only schema-valid stream-layer
  events attributed to the dispatched provider, session, and attempt may reset a
  timer; model prose and request-only records never count.
- Use one injected monotonic clock and explicit counters for trusted, meaningful,
  ignored, and untrusted events.
- Distinguish startup, first trusted output, generation silence, reconnect, tool
  activity, finalization, cancellation, and terminal timeout.
- Reuse captured Kimi and OpenCode fixtures and virtual time for deterministic tests.

### M2.5 — Governed continuation and queued steering

- Add a private authenticated continuation source bound to the terminal launch, exact
  reported model, provider handle, effective authority, spend class, gate decision, and
  actual workspace directory object. Public status exposes only an allowlisted capability
  projection.
- Add an atomic, idempotent claim ledger. One steering generation can create at most one
  successor provider process; ambiguous post-launch failure is indeterminate and never
  auto-retried.
- Start with Claude subprocess only after provider-specific continuity evidence. Treat
  Codex, Cursor, and OpenCode as unverified candidates until separately qualified. Keep
  AGY, Gemini, Kimi, ACP, and stateless/API routes unsupported until they expose a stable,
  attestable continuation handle.
- Re-resolve the agent and current policy, preserve equal authority and read roots, require
  fresh gate and spend consent, and prove the same workspace object before contact. A
  successor gets a fresh attempt, frozen bundle, liveness cache, and terminal receipt.
- Keep live steering false. `jobs steer` authenticates local queued intent; `jobs resume`
  may claim it for a later turn but never says the running provider received it.

### M3 — Fleet lanes and orchestration policy

- Implement `summon.fleet/v1` propose, validate, inspect, explain, and approval recording;
  add approval consumption and explicitly approved dispatch only in a separate reviewed
  slice.
- Keep policy as a typed constraint compiler that is structurally unable to route or
  dispatch. Feed its output to the M1 kernel.
- Bind approval to fleet digest, actor, operation, expiry, provider/model scope,
  permission ceiling, data boundary, corrective behavior, and spend ceiling.
- Route dispatch, manifest, chat, council, deliberate, and swarm through the same
  decision receipt. Reconcile the historical backend-style `provider` label with the new
  account/endpoint provider identity using additive evidence fields; never reinterpret old
  receipts. Retain the existing swarm lease coordinator.

### M4 — Usage, credits, and spend safety

- Retain provider-inert cache import/status, then add redacted export, synthetic
  examples, and explicit bounded live refresh with a provider allowlist and consent.
- Redact before parsing, normalization, cache write, diagnostics, or review output.
  Never retain raw provider output or account identifiers.
- Keep allowance, API balance, credit, rate limit, and unknown dimensions separate.
  Stale, unsupported, partial, or incomparable evidence never becomes a scalar score.
- Usage may explain or advise. It cannot authorize spend, reroute an exact request,
  enable PAYG, dispatch, retry, log in, or repair authentication.

### M5 — Safe context and token optimization

- Compile against the M0 compatibility corpus. Trusted adapters preserve authority
  byte-for-byte and in order; the ordinary file surface rejects authority and transforms
  only explicitly typed payload blocks.
- Limit the default safe profile to lossless deduplication, validated stable
  references, and bounded diagnostic/tool chatter with a target-resolvable map.
- Provide dry-run mapping, before/after byte and token counts, rollback, and a true
  off switch. Omitting context flags is identical to the frozen legacy path; explicit
  `off` preserves the supplied context packet's exact UTF-8 serialization.
- Do not let the context compiler initiate provider-session reuse, and do not add lossy
  semantic summarization in Phase 1. Governed M2.5 continuation remains a separate,
  explicit operational action and is invalid for final adjudication.

### M6 — Bounded context freshness

- Generalize the canonical deliberation packet rather than inventing a second context
  vocabulary.
- Bind `accepted_stale` to packet and revision digests, the resolved run-namespace
  digest, actor, reason, scope, expiry, maximum age, and maximum revision delta. Record
  the actual values, use a v2 acceptance/binding contract, and revalidate the namespace
  before scheduler creation and every provider launch so replay under another run root
  or a between-seat namespace swap is refused.
- Accepted stale evidence remains visibly stale and has no routing authority.

### M7 — Portable results and native/external adapters

- Derive a redacted compatibility projection from the authoritative private envelope.
- Omit mutable artifact path locators and expose only safe content hashes plus bounded
  metadata; defer locators until POSIX and Windows handle-relative traversal is proven.
- Require explicit native-adapter attestation. External CLI agents are not described
  as native subagents unless the host proves that relationship.
- Exercise dispatch, chat, council, deliberate, and swarm through the projection, and
  keep the schema experimental until one real external consumer validates it with
  synthetic or private-safe data.

### M8 — Integrated product and compatibility closure

- Validate the complete operator workflow: route explanation; usage inspection,
  import/export/refresh; liveness; approved fleet execution; context preview/off;
  bounded stale acceptance; and portable consumption.
- Update CLI help, skill docs, README, examples, migration notes, changelogs, release
  registry, scripts digest, and install payload in the same reviewed slices.
- Run legacy CLI, exact-agent, private-envelope, Windows launcher, privacy,
  migration/rollback, and external-consumer goldens; then reconverge owned installs.

### M9 — Release candidate, endorsement, and publication

- Run focused suites, the full supported Python/Windows matrix, packaging/install
  convergence, privacy scanning, documentation checks, and source-bound release gates.
- Obtain unanimous fresh approval from native Sol, exact Opus 5, and two independently
  attested non-OpenAI frontier seats. Kimi K3 remains a required advisory participant
  until its CLI exposes authoritative served-model identity; its child-observed identity
  cannot count as an exact vote. Use an independent frontier non-OpenAI seat that reports
  an exact served identity. A temporary or renamed model alias may participate only when
  its current identity, availability, billing boundary, and served-model evidence are
  explicit; never make release quorum depend on it.
- Obtain a final exact, reported, matched Fable endorsement of the immutable release
  candidate. Publish only after that endorsement, then verify the published artifact
  equals the reviewed artifact and reconverge owned local installs.
- Finish with clean commits, an empty index, and a clean worktree.

## Test-first review and release gates

- Write failing contract tests before implementation for every slice.
- Schema mutation/forgery, privacy, decision-precedence, and backward-compatibility
  tests pass provider-inertly.
- The Windows matrix covers case-fold collisions, drive/UNC/device/traversal paths,
  sharing violations during atomic replacement, CRLF/BOM input, long paths and
  references, and `summon.cmd` invocation.
- Decision tests prove exact requests and spend boundaries win over usage preferences
  and prohibit cross-dimension scalar ranking.
- Compiler goldens prove authority bytes/order are preserved, payload references are
  target-resolvable, and the off switch restores the legacy path.
- Liveness fixtures prove only trusted event classes reset clocks.
- Standard checkpoints require native Sol plus one fresh exact non-OpenAI model.
  Authority/privacy-critical M1, M3, M4 live refresh, M5, M7, and M9 require native
  Sol plus two fresh exact non-OpenAI models. M9 additionally requires Fable.
- A vote counts only when it is terminal and contract-valid, reports the exact served
  model, matches requested and served identity, and is neither partial nor blocked.
  Advisory output with absent, inferred, or mismatched model identity never counts.
- Every recommendation is locally validated before it changes the plan or code.
- BLOCKER/CRITICAL findings are fixed and re-reviewed. Release review distinguishes
  execution success, review verdict, and served-model attestation.

## Stop conditions

Stop the affected slice—but continue safe independent work—when a test gate fails,
review identity is unverified, a blocking finding remains, source/install hashes do
not match, authorization is missing, or evidence cannot be bound to the exact bytes.
Never lower review quorum, weaken fail-closed behavior, or infer release readiness
from version equality. Implementation completeness and GitHub publication are two
separate states.
