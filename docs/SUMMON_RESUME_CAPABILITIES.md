# Resume capability contracts

Status: the v1 contract is preserved; the provider-inert v2 contract is a
source-level eligibility and evidence contract. It is not a launch grant and
does not certify a particular provider session.

## Authority boundary

`skills/summon/scripts/_resume_capabilities.py` is the single pure registry of
declared resume and queued-steering capability facts. It does not inspect PATH,
profiles, environment variables, files, credentials, session handles, or
providers. A registry generation and digest identify the canonical source
snapshot; matching a caller-supplied generation or digest proves consistency
only, not authentication or qualification.

The v2 row always contains `launch_permission: "not_granted"`. Runtime adapters
must independently authenticate the observed executable/profile, owner and
workspace fence, continuation source, exact model evidence, budget, and any
provider receipt before an eligible admission path can launch. Capability rows do
not contain per-delivery acknowledgements. Steering remains
`queued_for_resume`; a provider acknowledgement belongs to a delivery/attempt
receipt, not to the static registry.

## v1 compatibility

The existing `summon.resume-capabilities/v1` row is exact and remains readable:

```json
{
  "schema": "summon.resume-capabilities/v1",
  "backend": "claude",
  "transport": "subprocess",
  "resume_state": "certified",
  "resume_reason": "claude_subprocess_governed_lane",
  "steering_mode": "queued_for_resume",
  "live_steering_acknowledged": false
}
```

The v1 shape and values are not silently widened. Existing continuation source,
job-status, and historical room consumers keep their exact v1 validators. A
supported migration must create a separately bound v2 source/receipt; it may not
infer new authority from an old row. Fresh named-profile continuation sources
use `summon.job-continuation-source/v2`. That source binds the private selected
profile path and a bounded credential/config-state digest, and the successor
claim binds the complete source digest. The public continuation projection is
unchanged and never includes either private value. A v1 source remains readable
for inspection, but a named-profile resume with no v2 state binding refuses
before child load; the supported safe recovery is an explicit fresh invocation,
not baselining the current account. Revalidation that reconstructs this missing
selected-profile state binding is not implemented. The supported legacy-source
revalidation paths described below do not bypass that refusal.

## v2 shape

The exact v2 fields are:

```json
{
  "schema": "summon.resume-capabilities/v2",
  "registry_generation": 1,
  "registry_digest": "sha256:<canonical-registry-digest>",
  "operation": "resume",
  "backend": "claude",
  "transport": "subprocess",
  "adapter": "summon-cli-subprocess",
  "adapter_version_scope": "summon-executor/3.4.0",
  "external_cli_version_scope": "not_declared",
  "resume_state": "certified",
  "resume_reason": "claude_subprocess_governed_lane",
  "steering_mode": "queued_for_resume",
  "handle_kind": "provider_session",
  "evidence_requirements": {
    "exact_identity": true,
    "owner_fence": true,
    "fresh_handle": true,
    "provider_receipt": true
  },
  "qualification": {
    "provenance": "source_registry_only",
    "status": "unqualified",
    "executable_binding": "unavailable",
    "provider_receipt": "unavailable",
    "external_cli_version": "unavailable",
    "expiry": {"state": "unavailable", "expires_at": null}
  },
  "launch_permission": "not_granted"
}
```

`operation` is currently `resume` or `steer`. Unknown operations refuse. The
registry retains the current candidate and unsupported rows; adding an adapter
or changing a version scope requires a reviewed registry change and generation
bump. Candidate rows still require provider-specific evidence. Unsupported rows
remain fail-closed.

`serialize_capability_v2` uses deterministic UTF-8 JSON for the v2 contract.
The tests cover the actual bytes; v1 dictionary equality is not presented as a
byte-stability guarantee.

`adapter_version_scope` identifies the Summon executor contract that emitted
the declaration. It is not the installed external CLI version. The latter is
explicitly `not_declared` at this pure layer. Likewise, `qualification` is
source-only and unqualified: no executable binding, provider receipt, external
CLI version, or expiry observation is being claimed. `expiry.state=unavailable`
means that expiration was not observed; it does not mean that the capability
never expires. The certified named-profile Claude lane revalidates the
executable/profile, owner/workspace fence, continuation source, exact
provider/model receipt, and handle at admission. Candidate providers remain
intentionally unqualified until that same provider-specific R02 qualification
is implemented and reviewed; this is a qualification boundary, not a
migration or readability gap.

## Accepted 3.5 preview boundary

The current source includes authenticated launch observation and qualification,
revocation checks, and refusal when registry, adapter, or external-version
evidence changes during a queued wait. These provider-free acceptance results
apply to the existing certified named-profile Claude subprocess lane; they do
not authenticate a particular installed session or qualify candidate providers.

Explicit job/chat revalidation can record current authenticated evidence without
launching work or changing historical facts where the adapter's legacy-source
policy permits it. That path is distinct from rebuilding a missing selected-profile
state binding, which remains unsupported. Admission still requires a compatible
authenticated source and current profile state; rotation never authorizes an
account switch or automatic retry.

Workspace plan recipients and simulated workers are not continuation adapters.
Human-context delivery, same-key lookup, and scoped disposition controls carry
their own authority; they do not turn queued guidance into a provider acknowledgement,
grant a live launch, or certify universal native-session attachment.

The 3.5 preview remains unpublished, and `summon-executor/3.4.0` above remains the
current source declaration until deliberately changed. Provider-free acceptance
does not satisfy the separate final release gate for private live-provider evidence.

## Migration boundary

The conversation journal has an explicit mutable-version fence alongside this
capability registry. Current rooms use the current integer schema version. Historical
v1 rooms remain readable for history and inspection, but the current runtime refuses
to append a turn, cancellation, or recovery event to them. The explicit `chat fork`
operation snapshots and hashes the v1 bytes, then creates a new v2 lineage without
mutating the historical parent. A caller cannot silently reopen a legacy room with
fresh v1 accounting. Older readers refuse v2 records before mutation. Process-record
versions are also exact integers; an unknown, boolean, or floating-point version is
refused before any PID/liveness, cancellation, or recovery probe.

The accepted provider-free migration and dispatcher gates cover these boundaries,
including typed pre-dispatch refusal and the public projection's zero-attempt,
not-run, and absent-served-model fields. They do not grant provider access. Admission
still requires a separately authenticated v2 binding, canonical registry generation
and digest, adapter/version qualification, exact model evidence, ownership fencing,
and an eligible continuation source. Missing evidence is refused; it is never
invented. The named-profile Claude subprocess lane is the only currently certified
governed continuation path. Other adapters remain candidates or unsupported until
their provider-specific evidence is reviewed.

Steering is `queued_for_resume`: `jobs steer` records guidance for a later eligible
successor and does not inject a prompt into a live turn. No v2 contract change enables
account switching, retry, fallback, live steering, automatic inbox delivery, or
universal native IDE-session attachment.
