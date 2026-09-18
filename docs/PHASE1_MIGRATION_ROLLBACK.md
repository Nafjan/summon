# Phase 1 migration and rollback

Current product version: 3.5.0

This is the active migration contract for the Phase 1 control plane. Historical 2.x to
3.0 instructions remain in `VERSIONING_AND_3.0.md`, but a current release must satisfy
this document and the machine-checked canonical version.

The 3.5.0 release advances the canonical marker to 3.5.0 and ships the
compatibility behavior described below as a provider-free workspace preview.
It neither authorizes live qualification nor waives a fixed publication gate;
unsupported live adapters and native device acceptance remain separately gated.

## Compatibility boundary

- The response `envelope: 1` contract remains additive. Existing fields do not change
  meaning because fleet, usage, context, liveness, and portable-result fields exist.
- The experimental portable result is a redacted view, not a replacement for the private
  execution envelope and not an authority token.
- Fleet approval and usage cache state are local control-plane data. They never become
  provider credentials or implicit spend consent.
- Version 3.4 adds account login mode to invocation identity. Re-derive affected fleet
  activations after upgrading; a prior structural digest cannot authorize a changed
  account selection. Named Codex account resumes remain unsupported.
- Managed installs are ownership-bound. Project-vendored and other unmanaged copies are
  reported separately and are updated only through an explicitly authorized path.

### Conversation v1/v2 boundary

Historical v1 conversation rooms remain available for read-only history and inspection.
The current reader refuses new turns, cancellation, recovery, or continuation on those
bytes; `chat fork` is the explicit migration path and creates a separately bound v2
lineage without mutating the v1 parent. Older readers refuse v2 records before mutation.
The named-profile Claude subprocess lane is the only currently certified governed
continuation path after exact identity, ownership, handle, and provider evidence checks.
Other adapters are candidates or unsupported until their own evidence is reviewed.
`jobs steer` queues guidance for a later eligible successor; it is not live mid-turn
injection, and Summon does not claim universal native IDE-session attachment.

For the existing certified lane, supported job/chat legacy-source revalidation
can bind separately authenticated current observations without launching work.
It does not recreate unavailable selected-profile history. Fresh named-profile
job sources bind profile state in the v2 source; a missing legacy binding or
rotated state refuses before child load. Use an explicit fresh invocation for
that unsupported recovery, preserving the prior history and authority ceiling;
never baseline the current account or silently switch profiles.

Roster drift also remains explicit: a trusted-host replacement fork freezes the
replacement definition under recorded permission restrictions and preserves the
parent history. A fork is not launch qualification, and retired selectors are
not reactivated by retaining their historical receipts.

## Phase 1 durable state

An upgrade can encounter these stores outside the managed skill payload:

- background job records, immutable per-job script bundles, controls, and continuations;
- conversation, council, deliberation, and swarm journals;
- the authenticated fleet approval store and its local key;
- imported usage cache, authenticated live-usage store, and anti-rollback checkpoint;
- opt-in local telemetry; and
- published workspace runs: the append-only workspace journal, pending/held
  message deliveries, owner/attempt uncertainty, and evidence references; and
- operator-selected portable result and release-evidence outputs.

The installer must preserve them. A release or rollback must not delete, rewrite, or
publish them. Paths, account identifiers, prompts, responses, sessions, credentials, and
raw diagnostic output stay in the private operator evidence bundle.

## Upgrade procedure

1. Start from the immutable release commit and run the provider-inert test and release
   registries.
2. Run `summon doctor --json` and `python install.py --dry-run`. Review managed drift,
   unmanaged copies, duplicate discovery roots, and active jobs.
3. Stop new launches. Let active owned children terminalize or explicitly cancel them;
   do not replace a script tree underneath an active child.
4. Keep a reviewed copy of the previous artifact. Run the ownership-safe installer and
   verify every intended managed destination matches the reviewed scripts digest.
5. Re-run `doctor --json`, the Windows launcher checks, migration gate, privacy gate, and
   release registry. Dry-run one exact roster seat with a prompt file.
6. After provider-inert convergence, perform only the separately authorized
   live-provider qualification required by the final release gates. Final
   publication still requires the complete source-bound evidence and a separate
   release-owner decision; preview acceptance grants neither contact nor publication.

## Rollback procedure

1. Stop new dispatches and record the current private release evidence.
2. Cancel or wait for active children. Never relabel an in-flight receipt with the
   replacement installation's digest.
3. Restore the previous reviewed managed artifact through its ownership-safe installer.
   Do not overwrite foreign trees or manually remove shared locks.
4. Preserve all Phase 1 durable state listed above. A prior reader must either replay a
   supported schema or return a typed unsupported/recovery-required result.
5. Re-run `doctor --json`, verify the restored version and digest, and execute the fixed
   provider-inert migration/rollback gate.
6. Record the rollback reason privately. Remove recovery material only after the operator
   confirms the restored installation works.

## Workspace migration rehearsal

The provider-free migration gate exercises a workspace that contains a queued
message and a held delivery with an active-attempt uncertainty record. The
current reader must reopen the exact bytes without creating a worker or
resuming an attempt. A layout with an incompatible format is rejected before
mutation. The gate also proves that an active execution lease refuses refresh
and uninstall, then that an explicitly settled provider-free run survives the
normal refresh path byte-for-byte. Encrypted operator draft persistence is a
browser/UI gate, not a plaintext migration file format. This rehearsal is a
compatibility check only; it does not authorize provider contact, native-session
attachment, or automatic resume.

The accepted preview rehearsal also covers current-source to 3.4.0-source and
back in owned synthetic homes, preserving encrypted pending work, approval state,
and uncertainty. It is not a claim about a real operator installation. Durable
workspace operation identities, holds, linked lineage, and accounting settlements
must remain readable or refuse explicitly after reopen; unknown outcomes require
same-key reconciliation, not replay with a new key. Token accounting does not
clear uncertain spend or grant routing authority.

## Release decision

`tools/release_contract.py` binds this document to the canonical version in
`plugin.json`. For a stable/public release, a stale version marker, incomplete
migration language, dirty source tree, failed fixed test, missing private
live-provider evidence, or divergent managed install is a release failure. A
release note or model endorsement cannot override those gates. The separate
provider-free workspace-preview profile is explicitly labelled below and is
not stable/public release evidence.

Provider-free preview acceptance records a narrower implementation result.
The release manifest now has two explicit profiles. The default `stable`
profile requires every fixed gate, including `live_provider`, to pass. The
opt-in `workspace-preview` profile is reserved for the reviewed provider-free
3.5 workspace slice: it still requires every suite and every other gate to
pass, and accepts only a machine-recorded `live_provider=blocked` result with
the exact `live_provider_evidence_missing` reason. A failed or invalid live
gate, an unreviewed skip, dirty source, missing rendered evidence or divergent
managed install remains a failure. This profile produces preview evidence only;
it cannot satisfy stable/public-release publication. A version label or model
endorsement never selects the profile implicitly.
