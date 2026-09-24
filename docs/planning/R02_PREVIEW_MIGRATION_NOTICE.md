# Chat continuation preview migration notice

Status: historical R02 Stage A implementation notice, followed by the current
accepted preview boundary below. This is not a release announcement and does
not certify a provider, account, external CLI, or installed continuation session.

## What changed

Summon now treats saved-session continuation as an operation-specific capability,
not as a generic property of an agent or a provider label. A continuation is
admitted only when the canonical resume-capability declaration still matches at
submission and the saved identity remains compatible. If that evidence is not
available, Summon refuses the continuation before dispatch and reports a bounded
reason. It does not silently start a fresh turn in the original room.

The compatible Claude subprocess preview path remains available under its existing
checks. It is not newly certified by this migration. Candidate and unsupported
lanes—including Codex, Cursor, OpenCode, ZCode, AGY/Gemini/Kimi, and unsupported
transport pairs—must use an explicit fresh turn or the existing explicit fork
action instead of an automatic continuation.

## What users should expect

- Existing room histories remain readable.
- New rooms use the current mutable journal schema. Historical v1 rooms remain
  readable and cannot be mutated in place: starting, cancelling, or recovering
  a turn there is refused. An explicit, non-mutating v1-to-v2 fork is supported;
  it snapshots and hashes the parent history, creates a new v2 lineage, and
  records the fork without appending to the historical parent. This prevents an
  older room reader from creating a fresh unaccounted v1 turn.
- A refused continuation is a blocked, no-contact result with an explicit reason.
- The submitted draft is retained or offered for explicit restoration; newer edits
  and other rooms are not overwritten.
- “Start a fresh conversation” is an explicit action. Summon does not create a
  child room or launch an agent automatically.
- A refused turn does not expose provider handles, profile/account labels, private
  paths, prompts, or historical model claims in the public event projection.
- Ordinary new dispatch, cancellation, recovery, and governed job continuation
  retain their existing behavior and contracts.

## Rollback and compatibility

The refusal boundary is part of the preview contract. A rollback may disable newly
qualified admissions, but it must not restore the former duplicate allowlist or
automatically replay or rebind work. Older readers continue to read the existing
v1 event kinds, but cannot open the current mutable room schema. The current
reader can inspect v1 history and create only the explicit hashed v2 fork without
writing the parent; the new public field is the bounded `refusal_reason` code.
Process-control records use the same strict integer-version rule before
cancellation or recovery probes.

Stage A alone did not complete R01/R02. Subsequent accepted provider-free packets
add authenticated observation/qualification, immutable private binding, revocation
and queued-version refusal for the existing certified preview lane. The current
authoritative disposition is recorded in RELEASE35_COVERAGE_MATRIX.md and the
capability document; this historical notice does not supersede it. Missing legacy
selected-profile state still refuses and cannot be reconstructed by revalidation.
Additional installed/provider qualification, calls, installation, credentials,
commits and releases remain outside this implementation notice.
