# Explicit effect resolution for the fixed worker demonstration

Status: implementation contract; simulated, provider-inert preview. Pure
validation is not provenance verification, execution permission, provider spend
evidence, or release qualification. Normal delivery acknowledgement and run close
continue to preserve unresolved effects.

## Durable event and allowed change

`workspace_effects_resolved` uses the existing workspace event envelope and an
exact payload:

```json
{
  "delivery": "the complete resulting delivery object",
  "resolution_kind": "fixed_worker_fixture/v1",
  "qualification": "simulated",
  "evidence": {
    "fixed_execution_scope": {"id": "scope-proof", "sha256": "source digest"},
    "owned_child_cleanup": {"id": "cleanup-proof", "sha256": "source digest"}
  }
}
```

The strings describing the delivery and digests above are explanatory
placeholders, not valid records. References require distinct opaque IDs and real
SHA-256 digests of their separately registered private source bytes.

`validate_effect_resolution` accepts an acknowledged original delivery with no
`parent_delivery_id` and no inherited uncertainty. Its complete previous certainty
must be `contact=occurred, spend=unknown, cleanup=unknown`. The only resulting
certainty is `contact=occurred, spend=not_incurred, cleanup=complete`. Every other
delivery field remains identical, including full selection, grant, recipient,
acknowledgement, state and identity. Known contrary facts cannot be overwritten;
partial resolution, pre-ack resolution and replacement-history recovery are not
supported. Repeated operations use existing event idempotency, not another
lifecycle transition. Historical launch, receipt and acknowledgement records
remain intact.

## Registered evidence and host observations

Both references bind to the delivery and its selected task. `fixed_execution_scope`
has category `observation`; `owned_child_cleanup` has category `fence`.
Each source has exactly these common fields:

- `schema`: `summon.workspace.effects-observation/v1`.
- `kind`: its evidence role.
- `resolution_kind`: `fixed_worker_fixture/v1`; `qualification`: `simulated`.
- `binding`: exact `protocol`, `workspace_id`, `run_id`, `delivery_id`,
  `message_id`, `recipient`, `grant_ref`, and complete `selection` from the
  durable delivery, with the existing strict types and bounds.
- `child_instance_id`: an opaque identity issued by the owning launcher.
- `request_frame_sha256` and `request_sequence`: the actual authenticated
  request correlation. Both sources must name the same child and request.

The execution source additionally requires `launcher=summon_owned_fake_worker/v1`,
`operation=sum_integers_v1` or `sum_context_integers_v1`, and the literal boolean
`provider_invoked=false`. The cleanup source additionally requires literal
`true` for `child_exit_observed`, `pipes_closed` and `channel_revoked`, plus the
observed integer `exit_code` in the signed-32 through unsigned-32 range. Nonzero
exit confirms exit, not successful computation. No raw PID, process path,
environment, channel secret, prompt or account information belongs in the event.

`effect_binding` and `validate_effect_observations` are pure consistency checks.
The actual fixed launcher must issue observations from its retained authenticated
request and independently observed child state. Under the coordinator mutation,
the trusted runtime must compare registered source bytes to observations from
that actual owned launcher and bind them to the durable selection. A caller's
JSON, simulated flag, source hash, generic resolver approval, worker prose or
arbitrary executable cannot establish these facts. Cleanup timeout retains
uncertainty. Loss of verifiable launcher state on restart does not manufacture
cleanup evidence or imply safe retry.

The fixed audited operation's lack of provider invocation applies only to this
simulated execution. The launcher is not a general network sandbox. Its evidence
must never resolve real-provider billing or inherited effects.

## Capacity and acceptance

Reserve one resolution wrapper and two evidence-registration slots before
launch, alongside the existing delivery and claim obligations. The virtual
settlement target is `effects_resolved`; its roles are the two names above.
Each registration consumes only its matching slot. An occupied slot cannot be
rebound or create duplicate credit. A branch that cannot reach acknowledgement
releases these unreachable finite slots; unresolved effects remain separately
represented. The generic uncertainty tail is not a substitute for this budget.

Acceptance requires exact-cap registration, resolution and close through the
real coordinator; unchanged journals on invalid-source refusal; independent
source observation from the actual fixed child; immutable receipt/history
replay; and idempotent retry without extra reserve credit. Provider adapters,
inherited-history resolution and power-loss qualification remain separate work.
