# Summon telemetry and recovery hardening plan

Status: the bounded operation-terminal slice shipped in Summon 3.2.0 after
independent Sol, Terra, Luna, and Fable review. This is a release-scope
diagnostic foundation, not a signed-evidence or production KPI certification.
Signed provenance/evidence registry verification and production
attempt/provider-turn emission remain deferred work.

This plan records the implementation and release boundary for the local
telemetry hardening. It complements [the Summon 3.2 plan](SUMMON_3.2_PLAN.md).
The plan is deliberately provider-neutral and is safe to publish: it contains no local
paths, account identifiers, session identifiers, or telemetry fingerprints.

## Why this work is needed

The current local spool is bounded and allow-listed, but it is not yet a
reliable measurement of product health. The reviewed data mixes production-like
runs with tests, reviews, orchestration, and older versions. It also shows four
diagnostic gaps that make failures harder to explain:

- provider authentication failures, especially repeated re-authentication
  flows, are not represented as a complete recovery lifecycle;
- exact model identity is sparse, and a target label is sometimes the only
  available evidence;
- timeout events do not consistently say which stage timed out;
- council and manifest paths emit a smaller event shape than normal dispatch,
  so report and model evidence cannot be compared consistently.

These are observability and recovery problems, not evidence that the aggregate
success rate is a production KPI. No GA decision may use the current mixed
spool until a clean, version-pinned cohort is available.

The gaps are also visible in the implementation contract: council and manifest
build smaller summaries than normal dispatch, timeout handling records only a
subset of stages, model targeting fields are not projected consistently, and
the auth path has no complete lifecycle record. The mixed spool is useful for
finding these gaps, but the implementation evidence—not a contaminated rate—is
what justifies the work.

## Goals and non-goals

### Goals

1. Make every diagnostic event attributable to a version-pinned operation and
   cohort without collecting user content.
2. Make authentication recovery understandable to both the user and the
   calling agent, including an explicit interactive-login handoff when
   autonomous recovery is not authorized.
3. Make requested, targeted, resolved, and served model identities distinct;
   exact pins must fail closed when authoritative served evidence is absent or
   differs.
4. Make dispatch, chat turns, council, deliberate, manifest, resume, and swarm
   lifecycle failures comparable through one event contract.
5. Provide provider-inert audits and release gates that catch silent fallback,
   missing evidence, privacy regressions, and telemetry loss before GA.

### Non-goals

- collecting prompts, results, raw provider output, credentials, account data,
  absolute paths, or a remote telemetry stream;
- turning a catalog label or a CLI argument into proof of `model.served`;
- claiming a provider root cause from aggregate telemetry alone;
- changing provider billing, quotas, or authentication policies;
- replacing an explicit user-authorized login with an unattended browser flow.

## Workstreams

### A. Establish a versioned event and cohort contract (P0)

Add additive fields to the local telemetry schema:

- `schema`, `summon_version`, and `event_kind`;
- `operation` (`dispatch`, `chat_turn`, `council`, `deliberate`, `manifest`,
  `resume`, `swarm`, `doctor`, or `onboarding`);
- `cohort` (`production_like`, `test`, `review`, `preflight`, `orchestration`,
  or immutable `legacy_unknown`), selected by a validated harness rather than
  guessed from a result;
- privacy-safe locally generated `operation_id`, `parent_operation_id`,
  `attempt_id`, and `provider_turn_id` values, plus a monotonic per-operation
  `event_sequence`;
- `backend`, `transport`, `permission`, and the existing normalized
  `failure_class` mapping, reused and extended rather than duplicated.
- `cohort_provenance_digest`, `provider_adapter_revision`, and
  `evidence_registry_revision` values observed from the adapter actually used;
  the audit compares them with the expected values in the release artifact.

The event grammar is explicit. `operation_started`, `attempt_started`,
`provider_turn_started`, `provider_turn_terminal`, `attempt_finished`,
`lifecycle`, `validation`, and `operation_terminal` are distinct event kinds.
Each provider invocation is one attempt; a retry creates a new attempt under
the same operation. A provider turn carries a `provider_turn_id` and its
parent attempt, and has exactly one `provider_turn_terminal`. An attempt and an
operation likewise have exactly one terminal event. Audits therefore count 30
terminal provider turns unambiguously, while retries and council members stay
separate from operation denominators.

A required release **lane** is one provider/backend × transport × model-policy
combination that the candidate claims to support (for example,
`codex × subprocess × exact_pin`). Operation type, permission tier, and
orchestration role are dimensions inside a lane, not separate sample floors.
The release harness freezes this matrix before collection. `doctor`,
`onboarding`, and `doctor --live` operations are always `preflight` and never
count toward a production-like lane floor; the cohort validator rejects an
attempt to label them `production_like`. They have their own funnel gates.

Deduplication is deterministic. Exact duplicate terminal records with the same
identity tuple and canonical payload collapse to one record. The tuples are
`(operation_id, attempt_id, provider_turn_id)` for a provider turn,
`(operation_id, attempt_id)` for an attempt, and `(operation_id)` for an
operation. Two terminals with the same tuple but different status, failure, or
model evidence are a `terminal_conflict`, excluded from all denominators and
required to reach zero before GA. Late non-terminal records become validation
events; an orphan terminal whose start was trimmed is classified as
`incomplete_truncated` rather than treated as a valid new operation. A started
turn with no terminal before owner loss, process kill, or the end of the
retained window is `incomplete_open`; it is excluded from denominators and is
not conflated with a trim orphan.

The canonical terminal payload is exactly: `terminal_kind`, `status`,
`execution_status`, `failure_class`, `report_ok`, `report_error_code`,
`result_usable`, `model_requested_class`, `model_served_class`,
`served_model_evidence`, `model_mismatch`, `auth_stage`, `auth_outcome`,
`interactive_required`, `remediation_code`, `auth_lifecycle_evidence`,
`timeout_stage`, and `exit_code`, with missing values normalized to null.
`event_sequence`,
`recorded_at`, `elapsed_ms`, warning counts, and raw identifiers are not part of
duplicate comparison. The audit reports zero unresolved terminal conflicts and
zero provenance/revision mismatches.

`operation_id` is a random 128-bit value unique within an installation and
collection window; `attempt_id` and `provider_turn_id` are random values unique
within their parent operation. The provenance digest and parent IDs scope
joins across windows. These identifiers are local-only and are never part of a
publishable export.

The caller cannot freely promote traffic into the GA cohort. New operations
default to `test`; `production_like` requires an immutable candidate
commit/package identity, a declared release-evidence source, and a collection
window recorded by the release harness. `test`, `review`, `preflight`, and
`orchestration` are never eligible for GA denominators. Imported schema-1
records are readable but classified `legacy_unknown`; that value is immutable
and cannot satisfy a release gate. A cohort validator rejects missing or
changed provenance instead of guessing.

Milestone 0 produces a private release-provenance artifact containing the
candidate commit/package digest, harness version, cohort declaration, collection
window, provider/required-lane matrix, and served-evidence adapter revisions.
The artifact is outside the repository and is the only authority allowed to
mark records `production_like`. The release harness writes canonical JSON and a
detached Ed25519 (or security-approved equivalent) signature; the audit verifies
that signature against an operator-managed public key before accepting the
artifact. The private signing key stays in an operator-approved secret store,
has a non-personal key ID, documented rotation and revocation, and is not
available to the collector process. The release approver verifies the artifact
with the public key and must be distinct from the collector and signer. Events
carry only the provenance digest and verification state. A provider or adapter
revision change during a collection window invalidates that cohort, and any
provenance binding change between collection and audit fails the audit.

The event builder remains the single allow-list boundary. Required-field
failures are visible as validation events rather than silently omitted. IDs
are locally generated opaque values; they are never derived from prompts,
paths, accounts, credentials, or provider session IDs. Unknown values are
bounded into explicit `unknown`/`other` buckets. Existing schema-1 records
remain readable.

### B. Use one emission path everywhere (P0)

Refactor council and manifest emission to use the same envelope projection as
normal dispatch. The common path must preserve the distinction between:

- an operation that never contacted a provider;
- a provider turn that returned a result;
- a report that was expected but invalid or absent; and
- an orchestration summary that has no provider-turn evidence.

Add `report_expected`, `report_ok`, and a structured `report_error_code` so
`report_ok: null` is meaningful for operations with no report contract. Keep
`failure_class` as the canonical classification. Existing schema-1 field
meanings, including `model_requested`, `model_served`, and legacy digests, are
frozen. A schema-2 reader must continue to parse schema-1 records through an
explicit compatibility branch; it must not reject them because the current
schema number changed. Schema-1 records have no trustworthy `event_kind`,
`operation`, provenance, or served-evidence state: the reader maps them to
`legacy_unknown` with nulls, does not backfill fields, and never pools their
model identifiers with schema-2 denominators without a schema dimension.

### C. Make authentication recovery explicit (P0)

Define an auth lifecycle that can be used by Kimi, ArkCLI, Claude, Codex,
Gemini, and future providers:

```text
auth_stage: preflight | call | refresh | login | retry | handoff | terminal
auth_outcome: not_needed | refreshed | login_started | login_required |
               recovered | failed | declined | cancelled | unknown
interactive_required: true | false
remediation_code: bounded enum such as provider_login_required
```

The user-facing error must say which provider failed, whether the request was
sent, whether an automatic refresh was attempted, and what will happen next.
If the calling agent has explicit permission and the provider supports a safe
non-secret refresh, Summon may attempt it once. Browser login, device approval,
or token entry is never performed silently: Summon offers the exact command
and waits for a new, user-authorized request. Retries are bounded and linked to
the same operation; no retry may hide a change in provider or model.

Telemetry stores a bounded `remediation_code` (for example,
`provider_login_required`), not a command, URL, profile name, or path. The
user-facing command is derived at display time from the provider adapter. Each
supported provider has a versioned recovery matrix covering refresh success,
refresh failure, interactive handoff, cancellation, concurrency, and the
maximum automatic refresh/retry count. A terminal authentication failure may
not be retried automatically; the terminal rule overrides any per-provider
matrix allowance. The frozen remediation enum is `none`,
`provider_login_required`, `provider_refresh_failed`,
`provider_auth_cancelled`, `provider_quota_wait`, `provider_cli_missing`,
`provider_config_required`, and `unknown`. New values require a schema review;
free text is rejected at projection.

Add deterministic tests for expired tokens, refresh success, refresh failure,
interactive-required handoff, cancellation, concurrent refresh, and the Kimi
and ArkCLI re-authentication path. Tests assert that no token, URL, prompt, or
provider output enters telemetry.

### D. Make model routing and served evidence auditable (P0)

Record these separate fields for every provider turn:

- `model_requested` and `model_source`;
- `model_targeted` and `model_selector_source`;
- `model_resolved` (advisory only);
- `model_served` and `served_model_evidence` (`reported`, `inferred`, or
  `absent`);
- `model_requested_class` and `model_served_class` for bounded/public
  aggregation, without changing the schema-1 meaning of the identifier fields;
- `provider_adapter_revision` and `evidence_registry_revision` for the
  authoritative receipt parser;
- `model_mismatch` and a bounded `fallback_reason`.

Canonicalization may make aliases comparable, but it must never erase the raw
requested/served distinction before the trust decision. A versioned provider
evidence registry defines, for each supported provider and transport, the
accepted authoritative receipt fields, parser/adapter revision, alias rules,
and malformed/absent behavior. `inferred` evidence never satisfies an exact
pin. An exact pin is successful only when the registry accepts an authoritative
receipt and the canonical identities match. A missing or different receipt is
a terminal `served_model_unverified`/`served_model_mismatch` result, with no
silent provider, model, ACP, resume, contract-repair, or ordinary retry
fallback.

For Codex, the registry includes a versioned fixture for CLI 0.147.0 in which
`turn.completed` has no model field. That case is classified as
`evidence_surface_unsupported`/`absent`, not as evidence that routing selected a
different model. Native turn context, a handshake, a catalog entry, or a
sidecar label can support targeting diagnostics but can never be promoted to
`model.served`. A route-failure diagnosis requires a source-bound terminal
receipt and the adapter revision that parsed it.

For providers that expose multi-model usage (including Claude), fixtures cover
auxiliary-model usage and ambiguous receipts. Selecting the model with the most
output tokens is not authoritative unless the provider contract says it is; an
ambiguous receipt is `parser_unknown`/unverified rather than a model mismatch.

Telemetry keeps the schema-1 identifier fields bounded and compatible, and
adds safe model classes for aggregation. Schema-2 readers do not reinterpret
legacy strings as registry-verified identities. Arbitrary provider deployment
strings are compared internally before projection and become `unknown` (or a
non-reversible bounded classification) when they do not satisfy the registry;
tenant, account, URL, and path material must not be persisted. A change to
either revision invalidates the in-flight production-like cohort.

Add an audit that reports the full request-to-served cross-tab, including zero-
count and `other` buckets, rather than only the most common pairs. The audit
must distinguish a true mismatch from a missing served field and from an
unverified imported report, and from `parser_unknown` evidence. These buckets
remain separate in every audit cross-tab.

### E. Instrument lifecycle, timeouts, chat, and council (P1)

Every operation receives a parent operation ID and per-attempt IDs. Record:

- `timeout_stage` (`queue`, `preflight`, `backend_execution`, `stream`,
  `report_validation`, `council_overall`, `owner_shutdown`, or `unknown`);
- deadline, elapsed, cancellation source, retry count, and whether spend is
  uncertain;
- owner health, stream health, provider-turn state, and final operation state
  as separate values;
- for council/deliberate: member start/finish/error state, quorum policy,
  partial-result policy, and chair/finalizer state;
- for chat/swarm: parent/child relationship, message cursor, reconnect,
  duplicate suppression, and user-visible terminal state.

Closing a browser tab must not be recorded as provider cancellation unless the
owner actually stopped the operation. Stream reconnects are bounded and
observable; a provider timeout, a stream loss, and an owner shutdown are never
collapsed into one generic error.

### F. Instrument onboarding and the diagnostic path (P1)

Track only the stage and outcome of first-run setup:

`install → doctor → provider_select → auth_check → model_check → permission_check
→ first_dispatch → recovery`.

For each stage record a bounded remediation code from the frozen Workstream C
enum, not the command's output. `doctor`, `doctor --live`, and `onboarding`
remain `preflight` even when they contact a provider; they do not count toward
the production-like lane floor. Their completion and recovery funnels have
separate stage gates.
`doctor` must be useful without provider contact, and `doctor --live` must be a
separate, explicit, bounded diagnostic. The UI and calling agent should expose
the same next action, including a clear `provider login` handoff when required.

### G. Build a provider-inert audit and reconciliation suite (P1)

Add `summon telemetry audit` (or an equivalent library command) that reads a
specified local spool and emits safe aggregate JSON/Markdown. It must include:

- version × cohort × status and backend × transport × failure class;
- auth lifecycle outcomes and recovery time buckets;
- request/target/resolved/served model evidence and mismatch pairs;
- timeout-stage and lifecycle outcomes;
- onboarding funnel outcomes;
- event validation, dropped/trimmed counts, oldest/newest timestamps, and
  spool-capacity state.

The audit must report denominator, numerator, and `unknown` counts together,
never silently discard nulls, and assert reconciliation invariants (for
example, status totals equal the sum of the status buckets). The command must
be deterministic, offline, and runnable in CI against fixtures. It must
reconcile event, attempt, provider-turn, and operation grains independently,
enforce one terminal result per attempt and operation, and surface duplicate
or late records. Because the spool is a bounded rolling window, the audit
reconciles only operations wholly contained in the retained window and puts
operations cut by trim into an explicit `incomplete_truncated` bucket excluded
from every denominator. An orphan terminal is included in that bucket, and a
production-like lane may not exceed a 5% incomplete share; the GA harness must
size the spool above the collection window so the expected share is zero. The
30-turn adequacy floor counts only turns surviving deduplication and exclusion.
It has two explicit modes: a private local audit (which may show fine-grained
timestamps) and a publishable export with time buckets, coarsened sparse cells,
no raw records, and no workstation inventory. Publishable export uses an
explicit allow-list: bucketed time, cohort/status counts, normalized
backend/transport/failure class, safe model classes/evidence classes, and
bucketed latency/recovery outcomes. IDs, provenance and revision bindings,
all digests, full timestamps, platform, and free-form fields are never
exportable; cells with fewer than `k=5` observations are suppressed or folded
into `other` before publication, and the deny-list is a secondary assertion.

### H. Privacy, retention, and support evidence (P0/P1)

Keep telemetry opt-in, local, bounded, fail-soft, and mode-controlled. Add
schema tests and fuzz cases containing credentials, JWTs, URLs, paths, prompts,
results, raw output, account IDs, and session IDs; none may survive projection
or bug-report generation. Test the ingestion, JSONL, private-audit,
publishable-export, bug-report, and release-artifact boundaries, including
nested/encoded/Unicode values, custom model names, and remediation inputs.
New correlation values use a per-install random salt and are excluded from
public export. The salt is generated once in the local telemetry configuration,
stored with the same local protection as the opt-in setting, and never written
to an event or report. `telemetry clear` rotates it; disable/re-enable and a
fresh install generate a new salt, so correlation never crosses an explicit
clear or installation boundary. Legacy `error_sha256`, `prompt_sha256`, and
`scripts_sha256` values remain local-only and are never included in publishable
output. Public exports bucket time and suppress sparse cells that could
fingerprint a workstation.

Expose retention/clear behavior and spool health without exposing the storage
path in public reports. Release evidence and managed-install inventories stay
in a private bundle. Public documentation contains aggregate test outcomes
only.

## Sequencing and review gates

### Milestone 0 — freeze the baseline

Capture the current schema-1 behavior and corrected audit findings as private
review evidence. Agree on event grain, cohort provenance, the supported
provider/required-lane matrix, the provider served-evidence registry, field
ownership, and the privacy allow-list before changing emission code. No
baseline number is labelled a production KPI. Gate: the signed private
provenance artifact exists, the provider and adapter revision matrix is frozen,
the signature verifies against the configured public key, the lane definition
and signing-key separation/rotation policy are frozen, and schema-1
compatibility plus the event/attempt/turn/operation grammar have fixture tests.

### Milestone 1 — schema and common emission

Implement A and B with additive compatibility, fixture-based audits, and
privacy tests. Gate: every new event has a schema, version, event kind,
operation, cohort, status, failure class, and validation result; the release
harness emits the provenance artifact; and each attempt and operation has one
terminal record in a non-truncated fixture. Fixtures also cover
`incomplete_open`, and compare the runtime-observed adapter/evidence revisions
with the declared provenance values.

### Milestone 2 — auth and identity

Implement C and D, then run provider-inert fixtures plus an explicitly
authorized live smoke for every lane in the locked provider matrix. Gate: each
provider passes all recovery fixtures; exact pins either have authoritative
served evidence or end blocked; auth failures expose a recovery outcome and
never retry silently.

### Milestone 3 — lifecycle and onboarding

Implement E and F, including browser/chat/council fixtures and owner/stream
separation. Gate: every timeout has a stage, every reconnect/cancel has a
terminal state, and onboarding failures provide a deterministic next action.

### Milestone 4 — audit and privacy release candidate

Implement G and H. Run the audit against synthetic fixtures, the current local
spool, and a fresh version-pinned production-like cohort. Results from the
current mixed spool are diagnostic-only and cannot satisfy a release gate.
Gate: event, attempt, turn, and operation totals reconcile after
deduplication, with truncated work explicitly excluded; terminal conflicts and
provenance/revision mutations are zero; `incomplete_truncated` and
`incomplete_open` are reported separately and their combined share per
production-like lane is at or below 5%, with `k=5` sparse-cell suppression in
publishable output and the planned collection fitting within spool capacity;
no forbidden data survives boundary fuzzing;
spool trim/clear/disable and write-failure behavior are verified; and the
release artifact contains no private evidence. The publishable export enforces
the explicit field allow-list above plus the field-level deny-list for local
correlation, full-precision timestamps, platform, and inventory fields.

### Milestone 5 — GA decision

Use only the fresh cohort collected from the exact candidate commit and
installed CLI. A release reviewer must explicitly sign the evidence bundle.
The supported-provider and required-lane matrix is frozen before collection;
each lane must run every required fixture, including at least one successful
turn and one fail-closed turn whose assertion fails if the guard is removed.
The production-like sample floor is 30 terminal provider turns per required
lane surviving deduplication and truncation exclusion. It is a measurement-
adequacy floor, not a success target; the separate fixture and release gates
still require the expected success/fail-closed behavior. A shortfall gets a
written waiver recording the achieved count, reason, and named approver in the
private evidence bundle, but a waiver permits only a labelled preview or
continued testing; it cannot make the candidate GA. An authentication loop
means any automatic refresh/retry after a
terminal auth result, or any automatic interactive-login attempt; the gate is
zero in fixtures and zero unexplained in the clean cohort, with a named
adjudicator and disposition for every exception. The terminal no-retry rule
overrides a provider matrix allowance. An `organic` timeout is a
production-like timeout not created by deterministic fault injection; every
such timeout must have a non-`unknown` stage. The candidate is not GA-ready if
the spool-capacity precondition is absent, any required lane exceeds the 5%
incomplete cap, or any required lane is below the 30-turn floor, in addition to
if any required exact-pin lane lacks served evidence, any supported provider has
an unexplained authentication loop, any organic timeout stage is unknown, or
any P0/P1 privacy, lifecycle, or silent-fallback finding remains open. If
evidence is unavailable, ship a labelled preview rather than infer a pass.

## Acceptance checklist

- [ ] Schema-1 readers remain compatible and new fields are additive.
- [ ] Every new event has `event_kind`, `operation`, `cohort`, and a validation
      outcome.
- [ ] Event, attempt, provider-turn, and operation grains are documented;
      duplicate/late events, terminal keys, conflicting-terminal rejection,
      and one-terminal invariants are tested.
- [ ] The required-lane definition is frozen as provider/backend × transport ×
      model-policy; IDs have explicit installation/window/parent uniqueness.
- [ ] Provenance signing uses an operator-managed key with documented rotation
      and revocation; collector, signer, and release approver roles are
      separated.
- [ ] GA cohort provenance is immutable and validated; legacy/test/review/
      preflight/orchestration records cannot enter GA denominators.
- [ ] Council and manifest use the common emission path.
- [ ] `report_expected`, `report_ok`, and report errors reconcile.
- [ ] Auth recovery and interactive handoff work for Kimi/ArkCLI and the other
      supported providers without exposing secrets.
- [ ] Terminal authentication results never trigger an automatic retry; the
      provider recovery matrix cannot override that invariant.
- [ ] A versioned provider recovery matrix and served-evidence registry are
      checked into the private release evidence, with adapter revisions.
- [ ] Runtime-observed adapter/evidence revisions match the signed cohort
      artifact; drift rejects the cohort.
- [ ] Exact model pins fail closed on missing, malformed, or different served
      evidence; no silent fallback or automatic replay remains.
- [ ] Full model mismatch and missing-evidence cross-tabs are available.
- [ ] Every timeout has a stage; owner, stream, provider, and cancellation
      states are distinct.
- [ ] Chat/council/swarm parent-child and reconnect states are observable.
- [ ] Onboarding stages expose deterministic remediation.
- [ ] Audit denominators, nulls, `other`, dropped events, and spool capacity
      are visible and event/attempt/turn/operation totals reconcile.
- [ ] `incomplete_truncated` and `incomplete_open` are separate, production-
      like lanes stay at or below 5%, and the collection window fits spool
      capacity.
- [ ] Private and publishable audit modes are distinct; timestamps and sparse
      cells are coarsened in publishable output; cells below `k=5` are hidden
      or folded into `other`.
- [ ] Publishable output is generated from an explicit field allow-list; the
      deny-list is tested as a secondary defense.
- [ ] Publishable export has a field-level deny-list for `error_sha256`,
      `prompt_sha256`, `scripts_sha256`, full-precision `recorded_at`, platform,
      and any other local correlation or inventory field. New correlation
      values use a per-install random salt and are excluded from public export.
- [ ] Privacy projection, audit, report-generation, and release-artifact fuzz
      tests pass.
- [ ] The release harness records the 30-turn adequacy floor, or (for a
      preview/testing decision only) a waiver with achieved count, reason, and
      named approver; it also records non-unknown organic timeout stages, named
      auth-loop adjudications, and mutation checks for fail-closed fixtures. A
      floor waiver can never authorize GA.
- [ ] Cohort, provenance, adapter, and evidence-registry bindings are unchanged
      between collection and audit, or the cohort is rejected.
- [ ] A fresh, version-pinned cohort and exact-candidate release evidence are
      reviewed; no open P0/P1 finding remains.

## Review record

The plan was reviewed against the corrected, mixed-cohort telemetry findings.
The record below contains model identity and verdict evidence only; it omits
local paths, account details, session identifiers, and diagnostic fingerprints.

- **Claude Opus 5** — requested/targeted/served `claude-opus-5`, served evidence
  `reported`; final review `APPROVE`, no blockers. Two non-blocking wording
  nits were incorporated before this record was written.
- **Gemini 3.7 Flash High** — requested/targeted/served
  `gemini-3.7-flash-high`, served evidence `inferred`; its textual review was
  `APPROVE`, but it is advisory rather than model-verified because inferred
  evidence cannot establish an exact provider identity.
- **GPT-5.6 Sol advisory** — returned useful adversarial findings, but served
  evidence was absent, so the external Summon response is not counted as a
  verified Sol approval. Native Sol independently verified the local selector,
  parser, and guard behavior, then required the evidence-surface corrections
  now present in Workstream D; its re-review of the corrected plan was
  `APPROVE`. This native validation is not an external provider served-model
  attestation.
- **Fable final lane** — the requested `claude-fable-5` call returned a
  provider usage-credit error and served `claude-haiku-4-5-20251001`; no Fable
  verdict or approval is claimed.

**Decision:** approved to begin Milestone 0 implementation based on the
reported-evidence Claude Opus approval, with native Sol validation of the local
guard and the advisory Gemini review. A later Fable review may be added as an
additional escalation, but it is not represented as having happened.
