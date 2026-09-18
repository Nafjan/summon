# F17 retain and linked-context implementation contract

Architect decision, 2026-09-09. Required F17 scope; provider-inert implementation
only. The conductor owns shared integration. This contract supplements the
public-command brief and does not authorize worker execution or provider contact.

## Source basis

`_workspace_protocol.validate_linked_delivery` already requires distinct child
identity, the same workspace/run/message, eligible parent state, required proof
reference shapes, a different grant reference and physical-fence reference for
held/dead-letter parents, recipient-transfer proof when needed, and inherited
uncertainty. These structural checks do not prove source authenticity, current
authorization, fence freshness or permitted recipient.
`_workspace_state.apply_event` already accepts `workspace_delivery_linked`,
requires registered evidence and rejects a second successor for one parent.
`_workspace_admission` already classifies and budgets that event. The fixed demo
exercises it through an explicitly permitted event. The ordinary operator API
has no equivalent typed operation. Reuse these invariants rather than replacing
them with a new task graph or a browser-controlled generic event append.

## Retain hold

Add an explicit `retain_held_context` command for a current held delivery.
It records operator intent; it does not perform a delivery transition. Do not
add held-to-held to the transition table or rewrite reason, acknowledgement,
certainty, inherited uncertainty, grant, selection or parent/child binding.

Use a strictly validated `workspace_operator_disposition_recorded` journal event.
Its payload contains only action (`retain_held_context`), delivery identity,
canonical request reference, authenticated decision reference and a finite
reason code. Initial reason vocabulary: `awaiting_evidence`,
`awaiting_authorization`, `operator_hold`. No free-text reason or fabricated
settlement proof is accepted. Registered references must bind this exact request,
delivery and task. The existing current owner validates authorization and scope
generation immediately before append, including the still-held state.

The trusted owner must resolve the references to private typed proof sources,
not merely check `category=event`. Each source uses
`summon.workspace.operator-disposition-proof/v1` and an explicit `request` or
`decision` role, with exact operation, action, delivery, task, reason and
installed-policy authority binding. Unrelated, stale, duplicated or swapped
roles refuse closed; the public operator body never supplies proof references or
bytes.

This event consumes normal event/journal capacity. It does not release or consume
a delivery's remaining recovery reservation merely because the operator retained
it. Do not add an unbounded disposition collection: lookup may use the bounded
journal and existing operation index. Replay preserves the delivery byte-for-byte
and advances only the ordinary journal/projection revision and operation record.
Reject an unsupported event/contract version before mutation; update the declared
feature/capability boundary so an older reader cannot silently ignore the event.

## Linked replacement

Expose a typed linked-replacement proposal through the current operator host.
The proposal supplies opaque parent and recipient targets plus an operation key;
the host resolves registered sources. Public requests cannot supply trusted proof
categories, decision bytes, grants or arbitrary journal payloads. A proposal alone
is not an accepted delivery, permission, provider retry or execution request.

An explicitly authorized submission of that proposal can queue one child using
the existing `workspace_delivery_linked` event. Require a separate installed
link action scope, bound to the parent and permitted recipient. Resolve current
remaining authority, authenticated disposition, fresh-attempt grant and physical
fence as required by the existing validator. If those sources are absent, stale,
unverifiable or unauthorized, refuse without creating a child. Do not fabricate
fresh authority from command-policy membership or the proposal itself.

Derive child identity deterministically from the canonical operation binding;
preserve the same message, parent lineage and union of unknown certainty facts.
Keep the parent unchanged. New child's own contact/spend/cleanup remain
not-attempted/not-incurred/not-started, with inherited uncertainty separately
visible. A new queued context is not a provider attempt. Response vocabulary must
distinguish proposal-only/refused, durably queued child and unknown outcome.
Do not automatically submit a previously refused proposal after a later grant.

Bind authorization and fresh evidence again inside the owner-fenced append.
The trusted host resolver must verify actual source bytes and their exact
parent/task/recipient binding, current generation, expiry, revocation and fence
freshness. Mere registration and category-correct references are insufficient:
existing grant/fence registration can omit delivery binding. Wrong-parent,
wrong-recipient, expired and revoked sources must refuse before append.
Historical replay validates the recorded bindings and evidence; it must not
require an old authorization to remain live today. Present mutations separately
require present authority.
Reserve the existing accepted-child and recovery obligations before acceptance.
Reuse the one-successor-per-parent rule across competing keys and after reopen.
For commands on an existing linked child, resolve and validate its actual parent;
do not simply remove the current parent-binding refusal or treat it as ordinary.
Reconstruct and verify the child's original linked-admission event from the
verified journal, then compare immutable lineage with the current child and
separately validate its current state and command scope. Do not pass an advanced
child into `validate_linked_delivery`, which admits only a queued successor with
untouched certainty. Do not call the ordinary initial-delivery binding validator
for a linked child: it intentionally refuses parent bindings.

## Request and recovery compatibility

Keep existing strict v1 cancel/dispose requests accepted unchanged. New action
payloads need an explicit versioned request contract; do not silently widen the
exact v1 field set. Include typed reason/recipient/proposal fields in canonical
request identity. The same key and identical request reconciles one result;
same key with changed action, reason, parent or recipient is a conflict.
Keep schemas and capability inventory synchronized with actual implementation.

Authorization expiry or revocation cannot be bypassed through lookup. Retained
history does not reinstall live permission. A denied lookup must not suggest a
blind retry. Evidence-only, torn/unknown append, response loss and reopen retain
honest uncertainty; only a verified current journal prefix proves completion.
No automatic replay of a mutation follows a malformed or lost response.

## Finite acceptance packet

1. Retain via ordinary authenticated host/CLI, then same-key lookup and reopen:
   one audit event and identical delivery fields; typed reason visible safely.
2. Duplicate identical retain versus conflicting reason/key; expired generation,
   wrong task/recipient, unregistered decision and capacity exhaustion refuse.
3. Linked proposal without fresh authority creates no child; explicitly scoped
   valid sources queue exactly one child via the existing event validator.
4. Changed recipient without transfer authority, copied grant, stale fence,
   dropped inherited uncertainty, mismatched message and duplicate successor
   all refuse before mutation.
5. Competing keys for one parent, lost response, interruption between private
   evidence and event, then same-key reconciliation/reopen: never two children
   and never an invented successful outcome.
6. Strict old request compatibility and stale-reader refusal; exact event-byte
   capacity/reservation checks; no provider, second owner, private output or
   worker execution. A component suite does not replace these public journeys.
7. A linked child later held for recovery supports authorized retain/dispose;
   same-key lookup after subsequent advancement verifies the original recorded
   operation without incorrectly re-admitting the child as a new queued delivery.

Native read-only contract review identified the source-authenticity and advanced
child distinctions above; the lead confirmed them against current validators.
This is not a provider-backed Fable review or runtime test evidence.

This is implementation direction, not a claim of completed or independently
approved runtime behavior. Return contract conflicts before widening any boundary.
