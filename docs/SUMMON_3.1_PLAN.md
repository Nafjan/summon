# Summon 3.1 plan

Status: candidate planning target. Summon 3.0.0 remains the current GA release;
no 3.1 tag or release exists until the gates below are recorded on one clean
source commit.

Summon 3.1 is a trust-and-reliability release. It should make the preview
surfaces durable and useful without turning preview behavior into an implied
provider, IDE, or hosted-service guarantee.

## 1. Outcomes

### 1.1 Shared authority and execution contract

Dispatch, council, deliberate, chat, and swarm must share a versioned execution
plan and authority envelope. The envelope records the requested model and
served-model evidence, permission ceiling, consent, budget, deadline,
transport, workspace binding, delegation depth, and continuation identity.

The execution kernel must enforce the envelope before side effects. A child
run receives an explicit subset of its parent's authority. Agent prose never
changes permissions, quorum, options, approval state, or provider routing.

The durable lifecycle is journal-first and idempotent:

`prepared -> claimed -> started -> working -> waiting-human -> terminal`

Cancellation, timeout, owner loss, cleanup failure, and uncertain spend are
typed terminal or recovery states. Recovery never silently retries a physical
attempt.

### 1.2 Council-to-deliberate governance

`/council` remains open-ended. It may collect positions, cross-examination,
chair synthesis, dissent, and human context. Its recommendation is a bounded,
redacted context artifact, never a ballot.

Promotion is an explicit human action that creates a fresh `/deliberate`
receipt. The human confirms the immutable option IDs, named seats, quorum,
round count, attempt budget, deadline, and approval policy. Deliberate then
provides deterministic multi-round scheduling, replay, recovery, cancellation,
and terminal evidence.

No natural-language agreement is counted as a vote. No hidden council state is
inherited as authority, and promotion never starts provider work implicitly.

### 1.3 Durable local collaboration

The local swarm coordinator and chat atlas should be useful across processes
without depending on an IDE being open.

3.1 should deliver a reference `summon.swarm/v1` stdio JSONL worker adapter with
claim and lease generations, idempotent messages, claim-bound artifacts,
cancellation, and explicit uncertain-spend recovery. Native IDE attachment,
session hijacking, and remote networking remain out of scope.

The chat atlas should become a coherent daily preview surface: clear room
intent, role/name/version model identity, human-context versus agent-turn
separation, addressed agent-to-agent context messages, cursor-safe reconnect,
owner and terminal state, redaction, and WCAG 2.2 AA behavior. It remains
preview until its own lifecycle and live-provider gates pass.

Custom agents become useful through `summon agents validate`: their source,
definition digest, role, and prompt contribution are auditable, while model,
transport, workspace, permission, consent, and worktree authority remain
receipt-bound. A custom manifest can never elevate authority.

## 2. Explicit deferrals

The following are not 3.1 acceptance criteria:

- implicit IDE or editor session attachment;
- remote, hosted, multi-user, or `0.0.0.0` operation;
- recursive or dynamic agent spawning;
- silent retries, provider fallback, or prose-derived ballots;
- a general-purpose external message broker or cloud artifact store;
- unrestricted live deliberation or a blanket live-provider claim;
- custom-agent marketplaces and broad model/vendor expansion.

## 3. Architecture boundaries

1. Keep the local JSONL journals, receipt binding, loopback authentication,
   and stdlib-first implementation as the source of truth.
2. Reuse one owner, lease, generation, process-containment, and evidence model
   across conversation, deliberation, and swarm. Do not create a second
   in-memory control plane.
3. Keep public projections allow-listed and redacted. Prompts, argv, cwd,
   credentials, profile data, raw provider output, and private account data do
   not cross the public journal, browser, telemetry, or release boundary.
4. Require exact served-model and profile/account evidence for every live
   provider route. Editorial frontier labels are not execution evidence.
5. Treat external workers as untrusted adapters. They must authenticate their
   handshake, respect claim fences, prove artifact ownership, and handle
   replayed or stale messages idempotently.

## 4. Ordered delivery plan

### Weeks 1–2: contract and threat-model lock

- Freeze the authority envelope, plan, evidence, journal, swarm, and migration
  schemas.
- Publish preview-graduation scorecards for chat, swarm, and deliberate.
- Review prompt injection, stale-owner, PID reuse, symlink, redaction, and
  uncertain-spend threats.

Acceptance: schemas and compatibility rules are reviewed; every proposed
state-changing action has an owner, generation, idempotency key, and evidence
field; no open P0/P1 contract contradiction remains.

### Weeks 3–4: fake-provider deliberate lifecycle

- Implement multi-round scheduling and sealed human command batches.
- Exercise durable-before-spawn, replay, torn-tail repair, owner takeover,
  cancel, deadline, cleanup/`LEFT_BEHIND`, and uncertain spend.
- Make the council recommendation-to-deliberate promotion path receipt-bound.

Acceptance: crash and takeover matrices pass repeatedly with zero duplicate
physical launches and zero provider calls during recovery.

### Weeks 5–6: swarm adapter and custom-agent validation

- Ship the reference stdio JSONL worker adapter.
- Enforce claim/lease generations, artifact fences, stale-worker rejection,
  cancellation, and bounded event admission.
- Add `summon agents validate` evidence and prompt-only custom-agent composition.

Acceptance: three-process conformance, lease expiry, stale completion,
artifact integrity, cancellation, and no-authority-elevation tests pass.

### Weeks 7–8: chat atlas hardening

- Add model role/name/version to participant and turn presentation.
- Finish cursor-safe SSE reconnect and polling fallback, owner/terminal states,
  addressed context messaging, redaction, room intent, and mobile/accessibility
  behavior.

Acceptance: real-journal browser tests cover reconnect, cursor gaps, owner loss,
terminal closure, parallel participants, hostile text, and secret-free HTTP,
SSE, DOM, and telemetry projections.

### Weeks 9–10: adversarial release candidate

- Run multi-vendor control-plane, process, privacy, migration, and UI reviews.
- Run the fixed test registry, install convergence, migration/rollback,
  accessibility, and visual evidence gates.
- Run one independently gated live-provider pilot only if its exact receipt,
  cancellation, deadline, cleanup, and uncertain-spend evidence is complete.

Acceptance: clean immutable source, reproducible release manifest, no open P0/P1,
and truthful GA/preview labels. If the live-provider gate is blocked, publish a
3.1 preview rather than calling the gated lane GA.

## 5. Release gates

3.1 is ready for a GA decision only when all of these are machine-recorded:

- stable 3.0 regression suites remain green;
- authority-subset, prompt-injection, schema, and migration tests pass;
- fake-provider lifecycle is deterministic and exactly-once;
- swarm protocol and worker conformance pass across processes;
- browser reconnect, owner, terminal, redaction, and accessibility tests pass;
- custom-agent validation cannot elevate authority;
- telemetry remains opt-in, local, bounded, and redacted;
- every live provider has an independent exact `model.served` evidence packet;
- the source tree is clean and the release manifest is source-bound.

No single model review, unit-test suite, installed CLI, or editorial frontier
label is sufficient evidence for GA.

## 6. Current candidate review and release decision

The current 3.1 candidate adds a narrow evidence-integrity slice before the
larger lifecycle work above:

- A provider or ACP envelope that claims success without a nonblank string
  result becomes the typed `empty_terminal_result` error. The original backend
  exit code is retained, the normalized exit code is nonzero, and the result is
  nonretryable by default.
- Model identity is bounded before it enters a public envelope. The
  `served_model_evidence` value is `reported`, `inferred`, or `absent`; an
  absent value is a warning, not evidence that a target model served the run.
  Malformed, path-like, URL-like, traversal, and token-shaped values are
  omitted rather than echoed.
- Automatic fan-out/resume handling does not repeat a typed empty result. The
  explicit `--retry-nonretryable` control permits one operator-directed fresh
  attempt; a second empty result remains suppressed.

The independent review wave used these named lanes and counted only reports
that actually completed:

| Lane | Result | Evidence boundary |
|---|---|---|
| Claude Fable 5 | Safe to land; preview only | Final adjudication of the supplied packet; no repository mutation |
| Claude Opus 5 | Safe to land; preview only | Final adjudication plus the same explicit gate list |
| Gemini 3.7 Flash High | Pass for the evidence slice | Toolful source review; served identity was recorded as inferred evidence |
| DeepSeek V4 Pro | Preview only | Text-seat review of the supplied release brief; no filesystem access |
| GLM 5.2 | Preview only | Text-seat review of the supplied release brief; no filesystem access |
| Kimi K3 | No report | No endorsement is inferred from an unavailable review lane |

The consensus is therefore: land the evidence slice only after local
verification, and publish at most a `3.1` preview. Do not call it GA, do not
claim live-provider behavior, and do not describe an unavailable reviewer as an
approval.

Before a preview tag or GitHub release, the release owner must record all of
the following against the exact commit being tagged:

1. A clean tree and synchronized version/changelog metadata.
2. A fresh complete test-suite run, the discovery suite, and the focused ACP
   and evidence-guard suites, with zero failures and captured external
   evidence.
3. Additive-envelope compatibility against the 3.0 consumer fixtures.
4. Explicit tests for missing, null, empty, whitespace-only, and non-string
   terminal results; typed retry suppression; the one-attempt explicit retry;
   and second-empty suppression.
5. Public documentation for `suspect` (diagnostic failure metadata only) and
   `served_model_evidence` (`reported`/`inferred`/`absent`).
6. A source-bound release manifest that contains the actual review statuses and
   does not include private prompts, receipts, paths, account data, or
   telemetry.

The deliberate lifecycle, swarm conformance, chat lifecycle, custom-agent
validation, and live-provider evidence gates remain separate 3.1-to-GA work;
passing this evidence slice does not close them.
