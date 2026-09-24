# R02 first behavioral migration contract

Status: approved for Stage A implementation by FABLE-R02-01, 2026-09-05, under the binding conditions in FABLE_R02_APPROVAL.md. This is not completion of R01/R02. Stage B must supply authenticated chat qualification and its actual trusted producers.

## Behavior to implement

Remove the independent chat resume allowlist. At admission, use the canonical `_resume_capabilities.py` backend/transport declaration as a necessary eligibility gate for a saved handle. Revalidate that declaration and its trusted in-process registry scope immediately before starting the dispatcher. No source-only declaration grants permission or qualifies a session. Existing owner, roster, context, permission, model and journal checks continue to apply.

Claude/subprocess retains its current compatible preview continuation path, explicitly labelled as such. Authenticated governed Claude job resume remains unchanged. Codex/Cursor/OpenCode/ZCode subprocess continuation refuses as candidate; AGY/Gemini/Kimi and unsupported transport pairs refuse as unsupported. Fresh ordinary dispatch is unchanged. Do not modify the pure v1/v2 registries or promote another lane.

Remove automatic `_fork` calls from `start_turn`, including missing identity and incompatible identity paths. Refused evaluation returns a fork offer; it creates no child room and appends no fork or migration event. The existing explicit fork operation remains the only room-creation action for this journey. Rebind is deferred and must not appear as an available action.

## Exact new public refusal shape

Proposed schema: `summon.chat-resume-refusal/v1`.

| Field | Exact requirement |
| --- | --- |
| `schema` | Exact schema literal above |
| `status` | `blocked` |
| `reason_code` | One of the bounded codes below |
| `capability` | Exact existing resume-capabilities/v1 object for the canonical backend/transport pair; no additional fields |
| `execution_status` | `not_run` |
| `attempt_status` | `not_run` |
| `attempts` | Integer zero, never boolean |
| `provider_contacted` | Boolean false |
| `contact_scope` | `this_invocation_before_dispatch` |
| `action_offer` | Exactly `{"kind":"fork","automatic":false}` |

Reject unknown fields and wrong types if a projection is accepted at a boundary. This is a refusal result, not a permission grant, provider receipt or description of another active claim. It contains no prompt, native handle, account/profile label, private path or historical model claim. Session/turn routing belongs to the enclosing established API, not this nested authority-free object.

Bounded reasons for implemented checks:

- `resume_candidate`
- `resume_unsupported`
- `continuation_identity_missing`
- `continuation_identity_incompatible`
- `resume_capability_changed`

Do not invent expired, revoked, executable-qualified or authenticated-account reason codes until an actual observation/enforcement path exists. A registry scope/declaration change between admission and submission produces `resume_capability_changed` and prevents dispatch; it is not proof of live provider revocation detection.

The same shape is allowed after a durable turn claim only when the worker proves it has not invoked dispatcher Popen. It must then close that claimed turn as blocked using the existing finish event and retain the bounded reason. This is not a no-write evaluation. No early-refusal event is necessary when no turn was claimed.

Do not extend this shape to generic Popen failures or started dispatchers. The developer's proposed blanket zero-attempt spawn-failure result and one-attempt dispatcher-start result are rejected: a dispatcher process is not a provider attempt, and ambiguous creation/contact must remain unknown. Preserve existing outcomes there; full operation/attempt accounting remains separately designed.

## API and browser integration

- Runtime produces the same validated refusal at synchronous admission and the proved pre-dispatch worker boundary. It must never silently start fresh in the original room when a saved handle is refused.
- CLI emits the bounded result and exits nonzero for refusal. Preserve existing success behavior and exact governed job schemas.
- HTTP returns a conflict/refusal response for synchronous refusal, not 202 or a synthetic started result. A turn already durably accepted may finish blocked later; expose that distinct event and reason honestly.
- Browser handles the typed refusal explicitly. Preserve the unsent draft, clear only the matching provisional activity and display the reason plus the existing explicit fork action. For late pre-dispatch refusal, retain/recover the submitted draft without overwriting newer edits or another room's draft. Use accepted/queued wording until actual evidence establishes execution.
- Fork requires an explicit authenticated user action, creates a child context only and does not launch an agent. Do not infer authority from the refused message content. Preserve existing owner/recovery semantics and parent uncertainty.

Use existing event kinds. If a bounded refusal projection must be added to an existing finish payload, inspect both old-reader compatibility and the public allowlist, and test it explicitly. Do not add a migration/rollback event or rewrite prior records to make the change appear compatible.

## Implementation and verification boundary

Expected production consumers are `_conversation_runtime.py`, `_conversation.py`, `_conversation_ui.py` and `_conversation_page.py`, plus a small pure refusal helper only if that keeps validation shared. Prefer existing CLI routing; do not change `_cli.py` or `run_subagent.py` unless a verified consumer requirement demands it. Preserve the accepted capability registry, governed job implementation and pending roster hunks.

Provider-inert coverage must prove the backend/transport matrix, all three former allowlist sites, changed scope between admission and submission, identity refusal without automatic fork, explicit fork without provider launch, compatible Claude preview continuation and unchanged fresh dispatch. Include CLI exit and HTTP mapping, browser draft/activity behavior, late refusal after a durable claim, and existing cancellation/owner fences. Validate real public paths with fake providers; do not merely test the helper in isolation.

The touched conversation runtime tests currently contain owned Popen fixtures without shared flags. Apply the approved Windows helper policy to those touched launch fixtures, including generated descendants, and extend the lexical launch inventory to the touched test file. Preserve their lifecycle assertions. This is the first incremental W-06 ratchet, not a new Windows policy.

Run affected conversation runtime/journal/UI tests, unchanged capability/governed continuation compatibility tests, and relevant Windows launch guards. Browser execution evidence must distinguish actual rendering from source-string assertions. Use installed tooling only; no dependency installation or live-provider test is part of this change. Keep private fixtures and outputs outside public documentation.

## Migration, rollback and next gate

Provide a dedicated preview migration notice naming Codex/Cursor/AGY continuation demotions, other candidate/unsupported pairs, readable histories, explicit non-launching fork and unchanged ordinary dispatch. Compatible Claude preview is not newly certified. Existing labels or digests must not be sealed into fabricated account, served-model, executable or session evidence.

Rollback retains the canonical refusal boundary and readable histories. It may disable new qualified admissions; it must not restore the old allowlist or automatically replay/fork/rebind work. No feature flag may bypass the denial gate.

After Stage A, R01/R02 stay open for the authenticated observation/qualification producer, immutable private binding, actual executable/version and account evidence, freshness/revocation enforcement, strict consumer migration and independently qualified adapters. The full workspace/task/message objective remains unchanged.

HANDOFF: Fable approved this denial/UX step before Stage B, subject to A-01 through A-06 in FABLE_R02_APPROVAL.md. DEV-R02-01 implements it under delegated authority; the lead independently verifies the result. No installation, credentials, commit or release is included.
