# Private workspace content: bounded helper contract

Status: implemented standalone helper, awaiting lead review and coordinator integration. No workspace message admission, provider/IPC adapter, release or hostile-worker-isolation qualification follows from this component. The governing design is `P1_VERTICAL_SLICE_BRIEF.md`.

## Interface and ownership

`skills/summon/scripts/_workspace_content.py` provides:

- `prepare_content(bytes_or_text) -> ContentRef`: validate complete strict UTF-8 and generate an opaque random reference, exact SHA-256 digest and byte length. The trusted supervisor retains this descriptor before attempting a write.
- `ContentStore(existing_private_root)`: verify an existing dedicated private directory and bind its filesystem identity. This constructor never creates or repairs the root.
- `provision()`: supervisor creates/acquires/verifies the fixed empty lock and checks the inventory before publishing the store to other cooperating callers. Creates no content and grants no authority.
- `put(descriptor, bytes_or_text, require_namespace_durable=False) -> ContentWrite`: create once, or verify and re-fsync an existing identical object; return explicit file and namespace durability observations.
- `read(descriptor) -> bytes`: verify reference syntax, containment, private file/handle identity, exact length, digest and UTF-8. A read does not establish durability.

Only the trusted supervisor calls these interfaces. The descriptor is a data type, not an authenticated worker grant. The helper creates no authority journal, scheduler, garbage collector, command channel or provider request. Message acceptance remains the coordinator's responsibility; an existing blob is not an accepted message or a delivery receipt.

## Privacy and serialization

Reuse the existing owner-private verification and file-hardening functions in `skills/summon/scripts/_fleet_approval.py`. Existing roots must already satisfy their OS-owner protection checks. Empty new files are secured before content is written. Reject symlink/reparse ancestors, nonregular entries and multiply linked files; compare an opened handle's resolved path and file identity using `skills/summon/scripts/_context_target.py:_final_open_path`. A platform unable to verify that handle path refuses the operation. Replacing the root after constructing a store also refuses.

All store reads, inventory admission and writes share one fixed zero-byte lock file, using `skills/summon/scripts/_job_control.py:_exclusive_control_lock`. This is an OS file lock across cooperating local processes, not an in-memory mutex or another authority store. Verify the lock's private, regular, single-link shape and unchanged identity around acquisition. Never remove or replace it. Contention uses the existing bounded wait and becomes `store_busy`; the helper does not steal locks or retry the operation automatically. The root must stay dedicated to this store, and all cooperating writers must use this interface.

The supervisor must call `provision()` successfully before publishing the root to concurrent callers. An empty lock becomes visible between exclusive creation and Windows ACL normalization; a concurrent first-use caller can conservatively receive `unsafe_entry` during that interval. This is safe refusal, not proof of race-free bootstrap. Provisioning establishes the verified lock before publication; it does not relax verification or turn an unsafe lock into an empty inventory. The concurrent-capacity qualification below assumes this explicit provision-before-publication sequence.

The local OS user, supervisor and existing private-root provisioning are trusted. ACLs protect against other ordinary OS users; they do not isolate arbitrary hostile same-user code capable of changing files, handles or supervisor memory. The helper does not certify protection against administrators, filesystem rollback or deliberately noncooperating writers. Reparse/identity checks fail closed on observed changes, without claiming an adversarial filesystem sandbox.

No exception includes a path or message body. The helper emits no logs or telemetry. References and digests are private implementation data, not automatically safe public identifiers. Caller output/export projections must omit private content and locators.

## Capacity arithmetic

The fixture configuration is 32 objects, at most 4,096 UTF-8 bytes per payload and 131,072 aggregate payload bytes. Complete blobs contain raw payload bytes only: zero serialized framing/metadata overhead. The fixed lock has zero file-length bytes and one directory entry. The descriptor belongs in the coordinator's separately budgeted metadata; this store does not duplicate it in sidecars.

Admission inventories every other directory entry, including unknown names, zero-byte or partial writes and unreferenced complete blobs. Every regular orphan consumes an object slot and its actual file length counts against aggregate byte admission. An oversized entry, directory, unsafe file, unreadable inventory or cap violation refuses the operation. Such failures are never interpreted as an empty store. A new blob is admitted only while holding the cross-process lock and after checking count plus its complete intended length; an identical existing blob needs no new slot. Consequently, repeated failed writes cannot grow unbounded numbers of uncounted temporary objects.

The aggregate bound covers actual regular-file lengths, including orphan lengths, rather than only referenced payload totals. It is **not a disk-allocation quota**: filesystem allocation units, directory entries, ACL metadata and journal metadata are separate overhead. No 131,072-byte total physical-directory claim is made. A hard filesystem allocation requirement needs an independently verified quota/budget. No garbage collection or deletion occurs without an authoritative reference view; incomplete objects remain visible to capacity admission.

## Write and recovery boundaries

The writer uses exclusive creation of the final opaque name. It never replaces or truncates an existing object. Existing atomic JSON replacement helpers are intentionally not used: replacement would violate immutability, and readable JSON/rename success alone would not establish the needed fsync boundary. Existing OS locking and privacy helpers are reused instead. A sharing denial fails closed; no automatic delete/rename retry or alternate reference is generated.

Success requires exact write length, explicit flush, successful file fsync, close, final root verification and reread of the exact expected bytes. The returned `file_durability` is `file_fsync_confirmed`. This records completed OS operations; it is not a hardware/storage-stack guarantee. POSIX additionally fsyncs the existing parent directory and returns `namespace_durability=directory_fsync_confirmed`.

Windows has no namespace durability qualification in this implementation. It returns `namespace_durability=unqualified` for the default file-fsync operation. `require_namespace_durable=True` refuses on Windows **before any content write**. A caller needing power-loss-safe creation must require that stronger boundary and hold admission until a qualified implementation exists. Do not reinterpret the default result as full namespace/power-loss certification merely because a process-restart fixture passes. Coordinator integration must explicitly select its required durability; the helper does not choose message-admission policy.

Lead integration decision: the initial Windows preview may use the **process-restart-only** boundary, explicitly selecting `require_namespace_durable=False` and preserving `namespace_durability=unqualified` in its private evidence. This is a declared preview limitation, not satisfaction or relaxation of a stronger namespace requirement. An admitted reference missing after restart must hold the affected delivery with missing-content evidence; do not reconstruct its bytes, mark it delivered, or automatically retry a provider. Any caller requiring stronger namespace durability still receives the existing pre-write refusal. That held-delivery behavior belongs to coordinator integration and is not claimed implemented by the content helper.

Write, flush, fsync, close, namespace-sync or final-verification failures return a typed `ContentError` with uncertainty. An interrupted write may leave an incomplete object occupying capacity. A lock-release failure after a successful write also reports unknown durability rather than a clean no-write refusal. Process termination may prevent any return, so the caller must treat the retained descriptor as requiring reconciliation.

Reconciliation is explicit: retry `put` with the **same descriptor and exact payload**. If a complete matching file exists, verify it and perform a new successful fsync sequence before returning a reused result. A mere successful read after failed fsync is not recovery evidence. If the existing object is partial or mismatched, refuse and preserve it; never overwrite, reconstruct from a preview, or silently move to another reference. A new reference, orphan disposition or message retry requires the caller's separate authority/capacity decision. The helper does not manufacture a durable refusal or recovery record in place of the coordinator journal.

## Verification and integration handoff

`skills/summon/scripts/test_workspace_content.py` uses private temporary roots and provider-inert fixtures. It covers Unicode/BOM/newline/NUL exactness, byte limits, missing/mismatched data, traversal/alternate-stream reference syntax, actual symlink/hardlink refusal, observed reparse/handle verification failure, private-root refusal, orphan count/length bounds, inventory/read failures, short write, flush/fsync/namespace-sync failure, post-write lock failure and same-reference reconciliation. Provisioning tests verify the empty lock and conservatively refuse a controlled unverified-ACL first-use interval. A four-process race with one remaining slot and a preprovisioned verified lock proves one successful admission and three capacity refusals under the declared cooperating-writer model; it does not prove concurrent bootstrap is refusal-free. Every test child uses shared hidden-launch flags and bounded cleanup.

The Windows tests exercise real ACL/private-root and file-lock behavior. The POSIX namespace branch still needs a POSIX run before platform qualification. A host unable to create a symlink fixture reports an explicit skip; a passing mocked reparse check cannot replace that host-specific evidence. Focused test results are reported separately by the implementation lane; no broad release gate is implied.

HANDOFF: Lead reviews the explicit durability distinction and the private helper dependency before coordinator wiring. Integrator must reserve message/control journal capacity under R13, persist only safe metadata in that journal, and select the required namespace durability before accepting a message. Maintain lock ordering as coordinator mutation scope then content-store lock; the content helper must never call back into coordinator mutation. Do not mark P1 complete or advertise Windows power-loss-safe blob publication, IPC authentication, provider delivery or hostile same-user isolation from this component.
