# Summon Deliberation and Pro Product Plan

Status: preview implementation (live provider scheduler and browser execution still gated)
Target branch: `codex/deliberation-pro-plan`
Owner: Summon maintainers

Active root goal: deliver the first shippable local deliberation slice through the
owner-safe headless gate, then add the optional loopback observer/control surface. The
current UI direction contract is in `docs/DELIBERATION_UI_DESIGN.md`; it is a structural
Operate design only and does not open browser or provider execution.

This document is the implementation plan and current safety contract for a new bounded
agent-deliberation mode and the product boundary it creates for a possible Summon Pro
distribution. The current branch contains the kernel, durable command/status surface,
one-launch adapter seams, a fake-only deterministic scheduler, a side-effect-free
frozen-roster resolver, and a provider-inert invocation planner. The scheduler is deliberately injected and headless: it does
not contact a provider. A controlled subprocess adapter smoke path now exercises one
fake executable child through the existing executor, but it is integration-test-only: the CLI,
resume command, and scheduler still refuse to enable live provider turns. The roster
phase loads each definition snapshot once, binds
role/profile/memory/account/executable evidence, reports effective permission, and
requires explicit per-seat consent plus disposable worktree evidence for writable or
full-bypass seats. The invocation planner binds exact prompt bytes to the scheduler
request digest, copies mutable profile state defensively, and revalidates roster and
worktree evidence before each turn; it creates no worktree, profile, process, provider
request, or journal event. Fresh and resume execution remain explicitly blocked until
the full provider lifecycle and scheduler wiring are separately reviewed. A
provider-inert replay slice now validates generation-tagged journal records, receipt
identity, legal transitions, turn/attempt/ballot bindings, and recomputes consensus
from accepted ballots; it reports unmatched starts as uncertain spend rather than
silently retrying them. The kernel now accepts only a sealed, relationally validated
checkpoint for provider-inert restoration: restored attempts count against budget but
cannot recreate launch tokens, and pending turns remain inert until a future scheduler
regenerates and rehashes the prompt. It does not
authorize a release or a pricing decision.

## 1. Product decision

Agent deliberation is a credible foundation for Summon Pro, but the differentiator is
not a chat window. The product value is governed, observable, replayable, cross-vendor
work with explicit human control.

The open Summon core should retain the safety-critical execution engine. An optional Pro
layer can add a polished local control surface, reusable policy packs, run history, and
team-oriented workflows without making the dispatcher cloud-dependent.

### Open core

- Bounded, headless `deliberate` execution.
- Existing council, manifest, background, subprocess, ACP, and envelope primitives.
- Static participant roles and personas.
- Deterministic turn scheduling and termination.
- Structured ballots and fixed quorum rules.
- Durable journal, status, replay, resume, and cancellation.
- Resource cleanup and `LEFT_BEHIND` / `environment_handoff` reporting.
- A minimal local API contract that a third-party UI can consume.

### Optional Pro layer

- A polished local browser control surface.
- Saved deliberation templates and role packs.
- Visual run history, replay, filtering, and export.
- Model routing and fallback policies based on verified capability evidence.
- Spend and usage views using observed provider data.
- Organization policy packs and review workflows, if a later product decision supports
  them.

Safety limits, process cleanup, provenance, and privacy must not be paywalled. The Pro
layer must work without telemetry and must not require a hosted service for local runs.

## 2. Goals and non-goals

### Goals

1. Let 2-10 heterogeneous participants exchange bounded, auditable turns.
2. Make the dispatcher, not model output, the sole authority over control flow.
3. Support independent first positions, later cross-examination, structured ballots,
   human messages, and explicit approval or cancellation.
4. Preserve Summon's existing permission, hidden-process, cleanup, redaction, and
   envelope contracts.
5. Make every run inspectable while it is live and after it terminates.
6. Provide a stable boundary for council composition without changing council semantics.
7. Keep the core stdlib-only and usable from all supported hosts.

### Non-goals for the first release

- Autonomous agent-selected scheduling or agent-created participants.
- Persistent ACP conversations or invented ACP resume semantics.
- Concurrent peer-to-peer channels inside one run.
- Universal hard dollar-cost guarantees.
- Remote hosting, multi-user networking, or `0.0.0.0` binding.
- Recursive deliberations or arbitrary nested run graphs.
- Model-rendered HTML, executable links, or model-controlled browser actions.
- A replacement for the existing council mode.
- Copying Beautiful UI source before authoritative licensing and provenance are proven.

## 3. Design invariants

These are release-blocking invariants, not preferences.

1. **Control plane separation.** Model output is data. It cannot choose the next speaker,
   alter a budget, change a role or tier, declare human approval, create a participant,
   or terminate a run. Scheduling uses executor-owned evidence such as physical process
   exit, timeout, transport result, and validated parser state. It never uses reconciled
   report prose, report `STATUS`, `VERDICT`, or the executor's model-influenced normalized
   top-level status as control evidence.
2. **Physical hard bounds.** Every run has a maximum *physical backend launch attempt*
   count and an absolute wall deadline. A logical turn is not automatically one launch:
   retries, ACP fallback, gates, and automatic contract-repair resumes can each launch a
   provider. P1 therefore uses a one-physical-attempt adapter with those secondary paths
   disabled. Any later retry or fallback must be a separately journaled attempt that
   consumes the same budget before it is launched.
3. **Uncertain spend is explicit.** A crash after a durable attempt record may have spent
   provider quota. Resume consumes that attempt conservatively, exposes `uncertain_spend`,
   and requires an explicit retry policy or human approval. It never silently replays a
   possibly paid turn.
4. **Fixed quorum denominator.** Failed, skipped, or excluded seats do not silently
   lower the configured denominator.
5. **Verified identity taxonomy.** `declared`, `verified`, and `observed` identity,
   capability, model, and usage evidence remain distinct. Unknown stays unknown.
6. **Single writer.** One scheduler owner appends events and advances state. HTTP
   handlers only validate and queue commands.
7. **Bounded data.** Prompt packets, output, events, journal files, archives, HTTP
   bodies, connections, and UI buffers have explicit byte limits.
8. **No new egress.** The UI is loopback-only and offline. Backend egress remains the
   same as an ordinary Summon dispatch.
9. **Cleanup on controlled terminal paths.** Timeout, cancellation, ownership loss, and
   normal completion must kill and reap descendants and report retained resources. An
   abrupt interpreter death, power loss, or SIGKILL cannot execute in-process cleanup;
   the next owner must detect stale attempts and may reap only through a verified,
   reuse-safe process handle, job, or process-group identity. If that identity is not
   available, it must report rather than kill by a reused PID. Preventing continued
   provider consumption after abrupt death requires a surviving watchdog or OS
   containment mechanism and is not promised by the core scheduler. Verified adapter
   cleanup is separate from advisory `LEFT_BEHIND` / `environment_handoff` declarations.
10. **Existing behavior stays stable.** Council, manifest, background, ACP, install,
    and envelope contracts remain green.

## 4. Architecture

Deliberation is a sibling run type. It shares infrastructure with council, but it does
not share council's mutable state machine or infer council semantics from its fields.

```text
caller / optional browser
          |
          v
validated command queue
          |
          v
single deterministic deliberation scheduler
      |                   |
      v                   v
checksummed journal   dispatch-turn port
state projection      existing Summon executor
      |                   |
      +---------> status / replay / UI
```

### 4.1 Run layout

Use the existing fenced run-directory and journal implementation. Add a sibling root for
deliberation runs rather than a second database or a second durability system:

```text
<runs-root>/deliberations/<run-id>/
  state.json                 # rebuildable projection
  journal-g<N>.jsonl         # owner-generation-fenced events
  g<N>-m<seq>-<sha>.json     # generation-fenced message material
  ui.json                    # ephemeral endpoint metadata, if UI enabled
```

The journal is authoritative. `state.json` is a projection that can be rebuilt. Large
turn bodies are stored in bounded, owner-generation-fenced message files and referenced
by hash from journal records. A deposed owner must not be able to publish or overwrite
material after takeover. The existing journal's generation fencing, owner checks,
checksum validation, newest-tail repair, and fail-closed mid-file behavior are reused.
The public restore boundary accepts the one `journal_repaired` audit record that owner
acquisition writes after healing a predecessor tail, then replays only the prior
generation prefix. Any other current-generation material is refused until a sealed
resume-prelude contract exists.

Replay consumes tagged `(segment_generation, record)` pairs, not the flat status
projection. It requires exactly one receipt-bound `run_prepared` record, rejects a
record whose declared generation differs from its segment, and treats a durable start
without a matching finish as an indeterminate physical attempt. Candidate and decision
projection events are never authority: accepted ballots are revalidated and tallied
again. The headless kernel still returns an immutable checkpoint, while
`_deliberation_resume.reconcile_run` is now the provider-inert crash-prefix boundary:
it owns one fenced lease, repairs a newest torn tail, recovers only sealed command
batches, and appends only a receipt-derived consensus/approval transition. It never
launches a provider and it returns a bounded blocked receipt for uncertain or
non-deterministic work. The fresh/resume CLI and live provider path remain disabled.

Human command replay is boundary-atomic even though the journal is append-only. A
checkpoint distinguishes immutable commands consumed by the immediately following legal
state transition from an EOF command batch whose transition was interrupted. Command ids,
contiguous sequence, actions, transcript projection, policy fingerprint, and both command
sets are covered by the checkpoint seal. Restore refuses a pending command batch and an
indeterminate physical start is uncertain spend, not a resumable pending turn. A valid
recomputed-consensus prefix whose state transition was interrupted is now reconciled by
the owner-bound restore path: it appends the deterministic `DECIDED/consensus` or
`WAITING_HUMAN/approval_required` transition before exposing any next action. A pending
human command batch is completed only by the separate provider-inert reconciliation
boundary, which validates the sealed batch and writes the matching transition once;
legacy unsealed EOF commands remain blocked and are never guessed. Both paths make
zero provider calls.

The public headless status and replay readers now rebuild their control projection from
the tagged journal through the same canonical replay validator.  The generation-fenced
projection cache remains a rebuildable convenience only; mutating it cannot change
status, candidate, decision, attempt accounting, or the checkpoint seal.  Receipts that
omit the canonical replay policy fields are refused rather than silently defaulted.
The immutable receipt also requires a lowercase `question_sha256`; optional creation
timestamps are finite non-negative numbers and are never included in public status.
Concrete `LEFT_BEHIND` resource names remain an executor/native-envelope handoff. The
public replay view intentionally exposes only cleanup booleans and bounded state, so
private paths and model-supplied resource strings cannot become a browser/export leak.
The receipt's absolute `deadline_unix_ms` is also the restore authority: the engine
projects it into its injected monotonic clock and ignores the legacy caller deadline
override, so a resume cannot extend the durable schedule.

Restore is owner-bound: its public entry point derives generation, append destination,
and current-owner fencing from one `_rundir.Owner`, then verifies that owner's
`receipt.json` and tagged journal before accepting a checkpoint. It derives the receipt
hash and policy fingerprint from one canonical receipt snapshot. Schedule rounds and a
cross-process absolute deadline are now receipt-bound and sealed in replay/restore; the
provider scheduler remains disabled until it derives its execution bounds from that same
receipt rather than from new CLI defaults.

The explicit `deliberate recover RUN_ID` command exposes only the provider-inert
reconciliation boundary. It may repair a newest torn tail and complete a sealed
human-command or receipt-derived consensus boundary; it never consumes the command
inbox or invokes an executor. `deliberate resume` remains `integration_pending` until
the live one-attempt provider adapter and cleanup contract pass their separate gates.

### 4.2 State machine

Legal states:

```text
PREPARED -> RUNNING -> WAITING_HUMAN -> RUNNING
                    \-> SYNTHESIZING

terminal: DECIDED | UNRESOLVED | REJECTED | CANCELLED |
          TIMED_OUT | ATTEMPT_BUDGET_EXHAUSTED | FAILED
```

Terminal precedence is fixed and documented. The scheduler does not let a limit erase a
result that was already durably received. At each durable boundary it applies this
state-dependent algorithm:

1. Journal corruption, ownership loss, explicit cancel, and an absolute deadline are
   safety boundaries. They win over an in-flight result unless the result and its
   validated ballot were durably appended before that boundary event.
2. For a completed attempt, ingest the executor-owned result and validate its ballot
   before asking whether another launch is allowed. A valid consensus becomes a
   candidate decision even when this was the final permitted attempt.
3. If policy requires a human gate, transition to `WAITING_HUMAN` before any
   attempt/round/repetition limit can discard the candidate. In that state, the next
   authenticated command is processed by durable sequence: `cancel` wins over
   `approve`/`deny` at the same boundary; `approve` decides, `deny` rejects, and no
   command leaves the run waiting forever after its deadline.
4. Only when another launch is needed do physical-attempt, observed-cost, adapter
   failure, no-new-ballot, and maximum-round limits terminate the run. If no valid
   candidate exists, the relevant limit produces `ATTEMPT_BUDGET_EXHAUSTED`,
   `UNRESOLVED`, or the adapter failure state.

A condition whose evidence arrived after an earlier higher-priority terminal boundary
cannot replace that terminal result. Existing council precedence is unchanged; these
are deliberation-only policies.

### 4.3 Envelope-v1 mapping

Deliberation does not introduce a competing top-level status vocabulary. The existing
execution envelope remains authoritative for process and transport facts, including its
existing `status` and `execution_status` fields. Deliberation adds one versioned,
additive object rather than replacing or reinterpreting those fields:

```json
{
  "status": "<existing envelope-v1 value>",
  "execution_status": "<existing envelope-v1 value>",
  "deliberation": {
    "schema_version": 1,
    "decision_id": "...",
    "decision_status": "decided|unresolved|rejected|cancelled|...",
    "termination_reason": "consensus|deadline|attempt_budget|...",
    "quorum": {"denominator": 3, "valid": 2, "threshold": 2},
    "physical_attempts": {"started": 3, "finished": 2, "indeterminate": 1},
    "uncertain_spend": true
  }
}
```

The exact existing top-level values are preserved; the example is illustrative and is
not a permission to change envelope-v1 semantics. A native local envelope may retain
existing diagnostic metadata such as agent-definition source paths. The browser,
export, and public bug-report views use a separately redacted schema: they add no raw
credentials, secrets, or new private paths. Consumers must select the view they need
explicitly rather than treating a native local envelope as a public-safe artifact.

### 4.4 Participant manifest

Each seat is snapshotted and frozen in the initial receipt. The snapshot contains the
exact agent-definition bytes, profile/config identity, resolved permission, role
resolution, and executable identity used for the run. Before every turn and resume,
the adapter revalidates those bytes and identities against the snapshot. A mutation,
replacement executable, profile drift, or permission drift fails closed before spawn;
the scheduler never reloads a mutable definition and silently continues.

A seat contains:

- stable participant id;
- role, persona, domain focus, and objective;
- requested CLI, profile, model, transport, and permission;
- declared capabilities and required capabilities;
- operator-assigned tier label; weighted votes are deferred in v1;
- agent-definition bytes hash and source;
- resolved backend/profile evidence;
- observed served-model and usage evidence when the backend supplies it.

Roles and personas affect framing and display only. They are not security authority.
Capabilities are checked against the adapter and permission policy. A model's own claim
about identity, capability, or seniority is ignored.

### 4.5 Adapter contract

The scheduler calls a narrow internal port:

```text
prepare(turn_context) -> immutable launch_spec
launch(launch_spec, attempt_id) -> adapter_result
capabilities() -> verified_capability_manifest
cleanup() -> cleanup_receipt
```

The first adapter implementations wrap existing executor paths:

- subprocess adapter for Claude, Codex, Cursor, Gemini CLI, Kimi CLI, and agy;
- ACP adapter only where current Summon supports ACP, inheriting yolo-only semantics;
- openai-compat adapter work is deferred: the current stdlib HTTP transport cannot
  interrupt an active request, so the deliberation bridge refuses those seats until a
  bounded cancellable transport exists. Ordinary single-dispatch API calls are unchanged.

Each turn is a fresh bounded dispatch in v1. The current `FreshDispatchAdapter` requires
the owner generation, rechecks decision/seat/turn binding at launch, consumes each
prepared launch spec once, and deep-copies mutable invocation state. Its optional
per-turn invocation factory may change only the prompt; production scheduler wiring
must supply it and hash that exact prompt into `TurnContext.request_digest`. ACP session
ids remain telemetry, not
resume handles. The P1 adapter must disable implicit retries, transient retries, ACP
fallback, approval gates, and automatic schema/report repair. It exposes a two-step
launch boundary:

```text
prepare(turn_context) -> immutable launch_spec
scheduler_commit_attempt(launch_spec_identity) -> fsync(attempt_started)
launch(launch_spec, attempt_id) -> adapter_result
```

`prepare` may validate profiles and stage bounded temporary inputs but must not spawn a
process. The scheduler is the single writer: it validates the spec, writes and fsyncs
the redacted `attempt_started` event, and only then calls `launch` with the committed
attempt id. A launch spec is single-use: the scheduler atomically marks that attempt id
consumed, and duplicate or concurrent `launch` calls fail without spawning. The executor
must provide a before-spawn hook (or an equivalent internal port) so every `Popen`/ACP
process creation is downstream of that durable acknowledgement. The journal stores a
bounded command identity and hash, never raw argv, prompts, environment secrets, or
resolved private paths. A future adapter may support secondary attempts only if each
is separately prepared, journaled, budget-checked, and single-use. The adapter owns
process-tree termination, pipe closure, temporary-file cleanup, verified cleanup
receipts, and retained-resource reporting. In the controlled subprocess path, agy and
Kimi fresh credential profiles register with launch control as disposable resources
before the final provider boundary. The adapter validates that each path is a direct
`run-*` child of the backend's Summon-owned profile root, removes it during cleanup, and
reports only a bounded kind label if removal cannot be verified. Named/resumed profiles,
arbitrary paths, symlinks, and HTTP/API resources are not deletion capabilities. This
registration is not exposed to ordinary dispatches and does not make the live scheduler
available.

Deliberation permission policy is explicit per seat. P1 accepts enforceable read-only
seats and text-only seats only when the caller supplies an explicit, receipt-bound
`text_only_consent` for that seat; acknowledgement is never inferred from a role,
profile, or inherited environment. An agy seat, an ACP seat,
or any other full-bypass seat is refused by default. Enabling one requires explicit
full-authority consent and a disposable worktree for checkout hygiene; a worktree is
not a filesystem, credential, or process containment boundary. A stronger claim requires
a separately verified OS/container sandbox, and the adapter records which boundary was
actually verified. Deliberation never silently turns a declared review role into
full-authority execution.

#### Physical-attempt accounting

The scheduler's budget is measured in physical provider launches, not logical turns.
After `prepare` returns and before `launch` is called, the scheduler writes and fsyncs
an `attempt_started` event containing the seat, turn, resolved transport, bounded command
identity, and attempt id. A normal result writes `attempt_finished`; a crash or owner
loss leaves the attempt indeterminate. P1 disables
Summon's implicit secondary dispatch paths for deliberation: transient retries, ACP
fallback, approval gates, and automatic schema/report repair. If any of those paths are
enabled in a later phase, each physical launch gets its own attempt event and consumes
budget before launch. A scheduler must never infer one-launch accounting from an
executor envelope alone.

The scheduler's trusted control matrix is deliberately narrow:

| Field | Scheduler may use? | Reason |
|---|---:|---|
| process exit, timeout, transport result | yes | executor-owned evidence |
| validated parser state and bounded ballot | yes | schema and seat/attempt bound |
| report prose, report `STATUS`, `VERDICT`, `HANDOFF` | no | model-authored data |
| reconciled top-level `status` | no | may reflect model report normalization |
| requested model / declared tier | only as declaration | not served evidence |
| adapter/profile/permission evidence | yes | Summon-resolved policy |
| `model.served`, usage, cost | observed only | provider-supplied and nullable |

### 4.6 Context packet

Every turn receives a canonical packet containing:

- immutable question and policy;
- frozen seat identity and capability metadata;
- typed human messages;
- prior structured ballots;
- a deterministic last-K and byte-bounded transcript projection;
- explicit `context_elided` markers when older material was omitted;
- untrusted-content delimiters and a preamble.

The full raw journal is never pasted into every prompt. An LLM summary cannot replace
the audit trail. Any summarization step, if added later, is a normal configured seat and
cannot rewrite prior events.

### 4.7 Structured output

The model may return prose plus a bounded structured block:

```json
{
  "position": "...",
  "ballot": {
    "option_id": "one-of-the-immutable-decision-options|null",
    "decision": "vote|abstain|undecided",
    "confidence": "low|medium|high",
    "evidence_refs": ["event-hash"]
  },
  "follow_up": ["..."],
  "left_behind": []
}
```

Malformed or missing ballots are data-quality failures, not votes. Natural-language
agreement is never counted as consensus. A ballot is valid only when its seat id,
turn id, physical attempt id, decision id, and schema version match the immutable
schedule. A `vote` must carry an `option_id` from the immutable finite option list;
`abstain` and `undecided` must carry `null`. A seat can revise its ballot only
in a later scheduled turn; the revision replaces the earlier ballot for that same seat
and decision id and is journaled. Duplicate or out-of-order ballots are inert data.

## 5. Scheduler and termination policy

### Phase A: blind first positions

Each configured seat receives the original question without other seats' positions. This
preserves independent evidence and makes later convergence auditable.

### Phase B: bounded exchange

The scheduler follows a deterministic round-robin order. Each seat receives the bounded
projection and may critique, refine, or maintain its position. Parallel within-run turns
are deferred.

### Decision and quorum

The run has an immutable `decision_id` and a finite list of decision options. The initial
seat count is the quorum denominator. The default v1 policy is equal seat weight and a
configured threshold such as `all`, `2/3`, or an explicit integer. Fractional thresholds
use deterministic ceiling (`2/3` means `ceil(2N/3)`), and every resolved integer
threshold must satisfy `1 <= threshold <= N`; invalid policy is rejected before PREPARED.
If the immutable
policy requires human approval, a valid candidate enters `WAITING_HUMAN` instead of
becoming terminal. Missing, failed,
malformed, or excluded seats remain in the denominator and therefore produce
`UNRESOLVED` when the threshold cannot be reached. Ties and contradictory valid ballots
also produce `UNRESOLVED`; they do not get resolved by prose or by a model's claimed
seniority. Operator-authored seniority labels may be displayed in v1, but weighted votes
and chairman veto power are deferred until a later policy review.

### Stop conditions

The state-machine algorithm above is the normative precedence. In particular, attempt
and round limits gate the next launch; they do not erase a validated result from the
last launch. A strict observed-usage threshold is an observed stop only: provider usage
is reported after a result and may overshoot the configured threshold by that physical
attempt. Unknown-cost adapters are rejected from cost-constrained mode rather than
treated as zero-cost. Exact structured repetition and no-new-ballot breakers apply only
when another launch would otherwise be selected.

No model-originated text is a stop signal. A chairman, if enabled in a later phase,
produces synthesis and a bounded recommendation. It cannot veto a hard stop, change the
denominator, or override computed quorum. Seniority labels are descriptive in the first
release; weighted votes and chairman authority require a separate policy review.

### Human commands

The CLI and UI may submit typed `message`, `approve`, `deny`, or `cancel` commands. The
handler validates the command, queues it, and acknowledges it only after the scheduler
durably appends it. The scheduler stamps `actor: human`; submitted text cannot impersonate
that actor or directly invoke tools.

## 6. Local UI and Beautiful UI decision

The UI is phase 3, after the headless engine passes replay, crash, cleanup, and adversarial
tests.

The server uses stdlib HTTP on numeric `127.0.0.1` and an ephemeral port. Observation is
fetch-streamed SSE. Human actions use authenticated JSON POST. Native `EventSource` is not
used because it cannot attach a bearer header; the browser uses `fetch()` with a streaming
response and the per-run token.

Required controls:

- 256-bit per-run random bootstrap token, delivered in a URL fragment and exchanged by
  browser code for a run-session token; the fragment never travels in an HTTP request;
- exact Host and Origin validation;
- no CORS, no remote assets, CSP, Referrer-Policy `no-referrer`;
- bounded POST bodies, connections, and event replay windows;
- text-only rendering with no model HTML, executable links, or action-shaped markup;
- monotonic command ids persisted by the scheduler so duplicate mutating commands fail;
- token invalidation after restart, ownership loss, or terminal shutdown;
- terminal event and lease-loss shutdown;
- replay cursor based on durable sequence numbers.

Beautiful UI is currently design inspiration only. The site presents useful primitives for
chat, thinking, streaming, approval, tool chips, and task rows, but no authoritative
license or source provenance was found during research. Do not copy or fetch its assets
until the copyright owner provides a usable license and provenance. The Pro frontend will
use a separately vetted, permissively licensed and independently branded component system.
Beautiful UI remains inspiration only until its code and design provenance are cleared.
The core remains dependency-free.

References:

- [Beautiful UI](https://www.beautifului.dev/)
- [AutoGen termination conditions](https://microsoft.github.io/autogen/0.4.9/user-guide/agentchat-user-guide/tutorial/termination.html)
- [LangChain subagent pattern](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
- [LangChain multi-agent patterns](https://docs.langchain.com/oss/python/langchain/multi-agent)

## 7. Open core and Pro packaging

The dispatcher and headless deliberation mode remain in the public Summon repository.
The Pro UI and product workflows should be a separate optional package or repository that
consumes the stable local event/API contract. It must not fork the scheduler.

Pro candidates, in order:

1. visual live transcript and controls;
2. saved templates and role packs;
3. run history, replay, export, and evidence filters;
4. verified model routing/fallback presets;
5. policy packs, spend dashboards, and organization workflows;
6. hosted collaboration only after a separate privacy and tenancy design.

No pricing, hosted service, or telemetry policy is decided by this document.

## 8. Implementation phases

### P0: specification and threat model

Deliverables:

- ADR with event vocabulary, state transitions, terminal precedence, identity taxonomy,
  ballot schema, context limits, and uncertain-spend rules;
- threat model covering model injection, local browser abuse, XSS, path traversal,
  process leaks, provider quota ambiguity, and journal corruption;
- capability and model-evidence matrix for all supported backends;
- Beautiful UI license/provenance request and a separate frontend licensing decision.

Exit gate: reviewers agree that no model output can affect control state and that every
controlled terminal path has bounded cleanup behavior. Abrupt death is covered by a
stale-owner detector and best-effort reaper; it is not described as synchronously clean.

### P1: headless engine

Deliverables:

- new `deliberate` command and run-directory namespace;
- single-writer scheduler with two static seats in tests;
- blind first round, bounded exchange, max attempts, absolute deadline;
- journal/state projection, status, replay, resume, and cancellation;
- fresh dispatch adapter using existing executor, with implicit retries, ACP fallback,
  approval gates, and automatic report repair disabled for P1;
- physical-attempt accounting that records an attempt before every provider spawn and
  charges every later retry or fallback against the same run budget;
- exact repetition and adapter-error breakers;
- uncertain-spend and `context_elided` evidence.

Exit gate: deterministic fake adapters pass all state, crash, replay, and termination
tests on Windows and POSIX, and an isolated fake-CLI integration harness proves the
real executor's before-spawn hook, exactly one process per committed attempt, disabled
retry/fallback/repair paths, snapshot revalidation, single-use launch tokens, and no
raw argv or prompt leakage into journals. No browser, chairman, background, or Pro UI
work proceeds before this gate.

Current branch evidence includes a provider-inert Phase-A composition harness. It binds
rounds and the absolute deadline to a canonical receipt, requires the same owner, snapshot,
profile revision, and generation at authorization and immediately before an injected fake
contact, consumes each launch spec once, and treats an exception after contact becomes
possible as uncertain spend. Fake resource cleanup uses an injected identity check; a
replacement or unverifiable resource is quarantined and never passed to the cleanup port.
Repeated cleanup is idempotent. This harness imports no executor, subprocess, network, PATH,
or profile-discovery surface and is not wired into the CLI or live scheduler construction.

This is not the P1 exit gate: no live roster/provider execution or CLI fresh/resume path is
enabled. Live activation still requires an owner-bound factory from the verified on-disk
receipt/roster, a reviewed mapping from the Phase-A authority into the existing executor
launch-control and disposable-profile tracking seams, crash injection at every durable
boundary using a fake executable, and independent adversarial review. HTTP/openai-compat,
ACP fallback/probes, implicit retries, gates, report repair, ambient provider/profile
selection, and browser/UI work remain disabled.

### P2: decision policy

Deliverables:

- structured ballots and fixed denominator;
- human CLI commands;
- descriptive operator-authored tier labels; weighted votes are deferred;
- bounded chairman synthesis without veto authority;
- observed-cost reporting; cost-constrained mode rejects unknown-cost adapters and
  explicitly documents that known usage is post-result and may overshoot by one
  physical attempt;
- provenance and capability gates.

Exit gate: adversarial model output cannot forge votes, identity, authority, or human
approval. Quorum tests cover missing, duplicate, malformed, and late ballots.

### P3: loopback UI

Deliverables:

- authenticated SSE and POST server;
- versioned local API plus a minimal open-core reference client using a vetted component
  system or approved licensed primitives;
- live transcript, roles/capabilities, pending human gate, stop/cancel, and replay cursor;
- UI lifecycle bound to run ownership.

Exit gate: browser security, XSS, CSRF, hostile Host/Origin, oversized body, duplicate
command id, stale-token-after-restart, lease loss, and reconnect tests pass. A live
session token may serve multiple legitimate commands; the gate does not claim that a
bearer token cannot be replayed during its valid lifetime. No external requests occur.

### P4: council and background composition

Deliverables:

- parent/child envelope references;
- an ordinary envelope-in/envelope-out composition contract that does not change council
  behavior;
- background job registration, cancellation, heartbeat, and orphan handling;
- bounded depth and inherited budgets.

Exit gate: council behavior is unchanged, nested recursion is rejected by default, and
orphaned jobs cannot keep serving a UI. Continued provider consumption after abrupt
parent death is asserted only when a surviving watchdog or verified OS containment is
present; otherwise the system must detect and report the orphan rather than promise
prevention. A council stage that changes existing council semantics is explicitly out
of scope.

### P5: Pro product layer

Deliverables:

- optional package consuming the stable event/API contract;
- templates, run history, replay/export, routing presets, and policy packs;
- release, license, SBOM, and privacy documentation.

Exit gate: Pro can be removed without changing open-core behavior, safety tests remain
green, and the polished frontend has no unreviewed source, asset, or design provenance.

## 9. Verification matrix

Every phase requires targeted tests plus mutation checks.

### Engine and durability

- state transition property tests;
- byte-equivalent replay and next-action reconstruction;
- crash injection after each durable boundary;
- uncertain-spend resume behavior;
- physical-attempt accounting across retries, ACP fallback, approval gates, and report
  repair; no launch may occur without a durable attempt record; prepare/fsync/spawn
  crash windows are covered;
- duplicate and concurrent launch calls for one attempt id are rejected without a
  second process;
- participant snapshot mutation, executable replacement, profile drift, and resume
  revalidation fail closed before spawn;
- final-attempt consensus, cost-threshold-plus-consensus, and every
  `WAITING_HUMAN` transition/race are covered;
- one-owner fencing and stale-generation rejection;
- torn newest-tail repair and mid-file corruption failure;
- bounded event/message/transcript sizes.

### Security

- prompt injection cannot alter scheduler fields;
- malformed ballots cannot count;
- self-claimed role/tier/capability is ignored;
- human actor stamping cannot be forged by text;
- model output renders as text only;
- path traversal, XSS, CSRF, stale-token-after-restart, duplicate command ids, and hostile
  Origin tests fail closed;
- abrupt parent death, stale owner takeover, and late writer attempts are tested;
- PID reuse and orphan handling fail closed: no kill by PID alone, and no claim of
  provider-consumption prevention without a surviving watchdog or verified OS sandbox;
- multi-option ballots, abstain/undecided ballots, and revision ordering are tested;
- redacted UI/export/public views contain no credentials, raw secrets, or newly introduced
  private paths. Native local envelopes retain their existing diagnostic contract.

### Process and backend matrix

- timeout and cancellation kill descendants on Windows and POSIX;
- agy stream proxy and legacy wrapper remain distinct;
- ACP remains yolo-only and does not gain false resume semantics;
- CLI and API seats with missing usage evidence are labeled honestly;
- every adapter reports `LEFT_BEHIND` and cleanup results;
- permission and consent tests cover enforceable read-only, receipt-bound text-only,
  agy full bypass,
  ACP yolo, explicit full-authority consent, worktree-only non-containment, verified OS
  sandbox opt-in, and refusal paths;
- journals contain no raw prompt/argv material, and the before-spawn hook proves that
  every provider launch follows a durable `attempt_started` event.

### Regression and release

- existing discovery and install suites remain green;
- all supported Python versions and Windows/Linux CI legs pass;
- three independent adversarial rounds over the immutable implementation candidate;
- public-data and secret scans pass;
- release notes distinguish open-core and Pro capabilities without claiming hosted or
  cost guarantees that do not exist.

## 10. Risk register

| Risk | Impact | Mitigation | Gate |
|---|---|---|---|
| Model output changes control flow | Critical | Typed boundary; scheduler-only state writes; normalized report status excluded | P1/P2 |
| Crash duplicates paid work | Critical | Durable physical-attempt record; `uncertain_spend`; no silent retry | P1 |
| Retry/fallback escapes the run budget | Critical | Disable implicit secondary paths in P1; charge every physical launch | P1 |
| Stale owner publishes after takeover | Critical | Generation-fenced journals and message material; owner checks | P1 |
| Local browser command injection | High | Token, Host/Origin checks, single writer, bounded commands | P3 |
| Context bomb or transcript growth | High | Byte/token caps; deterministic projection; elision events | P1 |
| Process or container leaks | High | Adapter cleanup contract and kill-tree tests | P1/P4 |
| False quorum after failures | High | Fixed denominator and typed ballots | P2 |
| Unverifiable model/tier evidence | Medium | declared/verified/observed separation | P2 |
| Beautiful UI license ambiguity | High | No copying until written provenance/license | P0/P3 |
| UI outlives run owner | Medium | Lease-aware shutdown, terminal event, and stale-token invalidation | P3/P4 |
| Pro fork diverges from core | Medium | Stable event/API contract and removal test | P5 |

## 11. Review record

The architecture was reviewed against the existing 2.2.0 implementation and the
following independent inputs:

- Claude route requested Opus for the exact-plan pass but served
  `claude-haiku-4-5-20251001`; this is not valid Opus served-model evidence. The route
  still supplied useful per-run-type policy feedback, but no Opus approval is claimed;
- Native Sol / GPT-5.6: uncertain paid work, fixed quorum denominator, cost honesty,
  provenance taxonomy, and Beautiful UI licensing block;
- GLM-5.2: deterministic scheduler, static provenance, bounded context, SSE, adapter
  cleanup;
- DeepSeek V4 Flash: replay tampering, context bombs, browser hijacking, and adapter
  boundary injection;
- Kimi K3: attempted but unavailable due provider quota; no approval claimed;
- Fable: attempted but unavailable due expired Claude OAuth; no approval claimed.

The reviewed draft incorporated the concrete blockers: physical-attempt accounting;
executor-owned control evidence; generation-fenced message material; controlled-path
cleanup plus stale-owner reaping; explicit decision options, ballot revisions, ties, and
fixed-denominator unresolved outcomes; additive envelope-v1 mapping; explicit permission
and consent gates; live-session token versus stale-token-after-restart semantics; equal
voting weights with seniority labels only; and a separate open-core reference client
boundary. Kimi and Fable were unavailable for this pass, so neither is cited as an
approval. The final exact-draft Sol read-only review was CLEAN after the last revisions;
it confirmed the launch-token, snapshot-revalidation, quorum-rounding, permission,
orphan, envelope, and integration-gate contracts. The implementation candidate still
requires the phase gates and independent adversarial verification specified above.
