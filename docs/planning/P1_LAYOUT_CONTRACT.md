# New workspace run layout: published-address compatibility

Status: standalone helper implemented; coordinator/CLI wiring and the full P1
preview remain separate. The lead selected process-restart preview semantics,
not namespace or power-loss durability qualification.

`skills/summon/scripts/_workspace_layout.py` owns provisioning and read-only
resolution of **new** workspace layouts. It never acquires an owner, repairs a
journal, launches a worker or writes coordinator events. The existing
`_swarm_coordinator.py` remains the sole writer of inner authoritative state.

## Shape and interface

An existing owner-private runs root contains each published workspace run:

```text
<run-id>/
  workspace-layout.json
  generation.txt/                 # deliberately a directory
    <run-id>/                     # existing coordinator run directory
```

The bounded canonical manifest binds exactly `format`, `run_id` and
`inner_namespace`. The current format is `summon.workspace-layout/v1` and the
inner namespace is always `generation.txt`; no manifest-supplied path is
followed. The repeated inner run ID preserves existing coordinator/frame run
bindings.

- `create_workspace_layout(runs_root, run_id)` provisions a fresh layout and
  returns private coordinator inputs only after publication and validation.
- `resolve_workspace_layout(runs_root, run_id)` validates without acquiring,
  repairing or initializing anything, then returns the inner runs root and run
  ID for the existing coordinator.

New entry points must use the resolver before coordinator construction. Unknown
formats, wrong bindings, noncanonical manifests, missing required directories,
extra outer entries or extra entries alongside the inner run refuse. The helper
does not validate inner journal semantics; the coordinator owns that boundary.
An inner mandatory feature event remains necessary for semantic negotiation and
is not implemented by this layout helper.

Private roots/directories and manifests reuse `_fleet_approval.py` verification
and hardening. Reject reparse ancestors and multiply linked manifests. Manifest
reads are bounded, check the opened handle through `_context_target.py`, and
recheck file identity. Public errors omit paths and manifest bodies. Returned
namespace paths are private inputs and are excluded from the result's repr;
callers must still omit them from public output and receipts.

## Why old normal commands cannot repair this address

The unchanged old mutation path calls `_swarm_coordinator._fence_run_files`
before `acquire_owner` or `journal_repair`. That fence requires managed paths,
including `generation.txt`, to be regular files. The actual directory therefore
forces refusal before ownership writes or torn-tail repair. No fabricated owner
lock, stale timeout or polling convention participates in this guard.

An old status read may report the outer run as uninitialized; it cannot discover
or reinterpret the inner journal through the normal published address. This
guarantee covers ordinary old commands using the published root/run ID. It does
not restrict arbitrary same-user code deliberately selecting private inner
paths or directly changing the filesystem.

## Publication and failure

Build the complete private layout under a staging name beginning with a dot,
which is outside the existing run-ID language. The manifest is written once,
flushed, fsynced and closed before the layout is validated. Publish the complete
directory using a non-replacing rename: Windows `os.rename`, or Linux
`renameat2(RENAME_NOREPLACE)` where the OS/filesystem supports it. Unsupported
publication primitives fail closed. There is no replacing-rename fallback.

Existing destinations, including empty directories, are never adopted,
retrofitted or overwritten. Normal old histories stay unchanged. Failure before
publication leaves private staging evidence for explicit disposition; there is
no recursive cleanup. On Windows, a bounded retry handles only a transient
permission denial while the host inspects the newly-created private tree. An
error after the retry window, or after publication may have started, reports
publication uncertainty; the supervisor must inspect/resolve the same address
rather than retry creation or invent another run ID.

This provides complete layout visibility to ordinary process-restart readers.
It does not qualify directory-entry persistence across power loss. The Windows
branch was exercised on the Windows host; the Linux publication branch is
source-only and unqualified until its own platform run. Other platforms receive
typed unsupported-publication refusal. All code shares the existing trusted OS
user model; observed replacement/reparse changes refuse without claiming an
adversarial filesystem sandbox.

## Acceptance and integration handoff

`skills/summon/scripts/test_workspace_layout.py` exercises the current coordinator's
close/create commands through its unchanged legacy entry path. A source review
compared ASTs against repository `HEAD` for `__init__`, `_fence_run_files`,
`_acquire`, `_mutation`, `create` and `close`; all six matched. This is evidence
for the reviewed source entry path, not certification of every historical binary.
Fixtures preserve a valid inner journal plus a torn tail,
assert no owner-acquisition or repair calls, and compare every outer/inner file
byte before and after old-command refusal. A separate fixture adds a valid outer
journal with a torn tail and proves it remains untouched; new resolution refuses
that foreign outer history rather than adopting it. The valid layout resolves
to the existing inner coordinator, whose normal owner scope performs its own
repair.

Additional fixtures cover complete guard before publication, late destination
collision, unknown format, binding drift, missing/foreign layout, actual
symlink/hardlink refusal, manifest fsync failure and uncertain publication. All
fixtures are provider-inert and temporary; there are no test child processes.

HANDOFF: Integrate through the returned inner namespace while preserving the
existing coordinator as the only authority writer. Keep workspace feature
negotiation, R13 capacity/durability, message admission and private content
provisioning in their assigned lanes. Provision and validate the content-store
lock before publishing that store for concurrent use; do not infer this from
layout publication. No P1 completion, live-provider access, old-history migration,
power-loss qualification, commit or release follows from this component.
