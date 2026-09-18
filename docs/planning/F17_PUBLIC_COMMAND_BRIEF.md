# F17 public command completion

Status: BLOCKING, 2026-09-08. The lead verified the public host and parser seam;
a bounded source/test audit identifies the remaining disposition vocabulary.
Existing runtime and injected-adapter HTTP/browser tests are component evidence,
not proof the ordinary workspace entry exposes those operations.

## Initial implementation review, 2026-09-09

Accepted focused correction checkpoint: the lead independently passed seven
cases in 5.10 seconds on an owned source snapshot, covering client response
correlation/unknown handling and actual-host successful, stale, failed-adapter
and competing-generation refresh. All 254 watched source files and owned Python
copies stayed unchanged. The run used synthetic roots, isolated Python and
guards denying child process launches and non-loopback connects/binds. It
qualifies these bounded corrections, not full F17 or provider behavior. The
historical refresh-order/concurrency findings below are resolved by the complete
reentrant-lock transaction. Remaining public vocabulary/provisioning and full
end-to-end acceptance still apply.

Concurrency follow-up: the initial ordering fix uses separate short lock regions.
The lead executed unchanged refresh/authorization method AST with in-memory
policy/runtime/adapter doubles: generation 2 paused during installation,
generation 3 completed, then generation 2 also returned refreshed and replaced
it. No runtime store, network or provider was touched; source remained unchanged.
Serialize the complete refresh transaction or use an equivalent monotonic CAS
whose rollback cannot overwrite a newer generation. Retain deterministic
competing-refresh acceptance; successful serial refresh alone is insufficient.

Refresh-order source finding: the new handle captures the replacement policy
generation, but `OperatorCommandAdapter.__init__` immediately describes its
scope while the host still holds the old policy. The callback consequently
rejects a newer-generation refresh. Initial startup does not exercise this
ordering. Replace policy/handle/surface atomically under the owner/control
boundary, retaining rollback of the surface adapter as well as policy state.
Verify actual-host successful refresh, stale-generation refusal and failures
during adapter construction or prior-handle revocation. This is a source-derived
finding; no actual-host refresh fixture has been run by the lead yet.

Follow-up checkpoint: source now sanitizes URL/JSON/HTTP failures, compares
opened private-file identity, preflights authenticated host identity and maps
transport loss after POST to same-key lookup. The conductor reports 104 focused
tests; the lead has not treated that report as independent full acceptance.
Three pure unchanged-source probes still accept arbitrary action/state strings
and a true task/provider-action claim in successful command responses. Require
finite response enums/invariants and correlation to the original operation key
and applicable request identity. Invalid/oversize/malformed responses after POST
must also preserve unknown outcome and same-key reconciliation. No network or
mutation occurred in these lead probes. Ready patch integration stays ahead of
this bounded follow-up correction.

The conductor has added explicit private command-policy input, actual host
adapter wiring and a noninteractive client for the existing loopback host.
The original gaps below describe the starting point, not the current absence
of all implementation. F17 remains blocking until the complete journeys pass.

Lead source review identified expiry bypass during command discovery,
generation enforcement and explicit refresh requirements, and malformed action
type handling. The initial exact-delivery scope also needs a documented explicit
provisioning path for newly sent deliveries; membership must not grant rights.

The lead independently exercised unchanged client source with in-memory
dependency/HTTP doubles and no network calls. Malformed bracket URLs and
non-finite JSON escaped as ValueError; malformed HTTP escaped as BadStatusLine.
These must become fixed, sanitized errors. Review also requires comparing the
pre-permission-check file identity to the opened descriptor, validating public
response contracts, and preserving unknown submission outcomes after response
loss. A character-pattern check is not an error-category allowlist. Required
run/root arguments must not imply target binding while being ignored by the
request path. The conductor owns corrections and focused negative acceptance.

## Concrete gaps

1. `WorkspaceHost.start` installs message and draft adapters but omits
   `operator_commands`. Ordinary open/demo-create therefore has no disposition
   adapter; `/api/commands` returns `read_only_surface`.
2. `_cli.rewrite_subcommand` and `_workspace_entry.run_command` expose only
   create/open/inspect/demo-create. No noninteractive command reaches message
   send/lookup or disposition/lookup.
3. `_workspace_commands.ACTIONS` offers cancel-queued and dispose-held only;
   linked deliveries are explicitly refused. F17 also requires explicit retain
   hold and proposal of a separately linked delivery under a fresh grant.

Source names above are under `skills/summon/scripts/`. The four choices remain
in scope; a shell brief describing a gated linked-recovery control does not
silently defer the normative checklist requirement.

## Integration direction

### Host authority decision, 2026-09-09

The ordinary-host adapter cannot safely be installed from the current message
scope. Independent source review confirms plan targets grant send/receive only;
`_authority["allowed"]` is a revocation switch, not a command grant. Runtime
command handles contain immutable exact delivery/action bindings (at most 128
targets, with a bounded handle count), so a startup snapshot also misses later
operator sends. The initial wiring-only helper correctly stopped without edits.

Use a separate explicit, versioned host command-policy input, independent of
the message plan and browser bootstrap credential. Do not widen existing v2
plan bytes or infer rights from journal membership. The policy must bind the
workspace/run, finite task/recipient constraints, allowed actions, validity,
generation/revision and a maximum active delivery count. No wildcard workspace,
unbounded lifetime or prose-derived grants. Absence means controls are unavailable
with an honest reason; it does not authorize a permissive default.

The existing owner resolves that policy against each eligible delivery into
exact current delivery/action grants. This supports future sends only because
the separately supplied policy explicitly permits them. Replace/revoke a bounded
scope atomically under owner fencing; do not accumulate immortal handles or
spawn another journal writer. Recheck policy validity, generation and current
delivery identity for description, execution and same-key reconciliation.
Reopen must reload current explicit policy and reinstall ephemeral handles;
receipts and historical grants cannot recreate present authority.

The ordinary CLI/host must expose explicit policy provisioning as a typed
operator control, with sensitive input transported privately. A browser message
or read credential cannot provision or broaden it. Policy updates are explicit
control operations; file changes alone must not silently expand a running scope.
Keep the existing exact-binding command request contract underneath this input.
The conductor owns concrete input/refresh APIs and focused tests; any inability
to preserve these boundaries comes back as a specific architectural finding.

Install a finite host-authorized disposition scope and the existing typed
adapter at the actual workspace entry. Actions may be absent for ineligible
deliveries, but the host must expose eligible operations and their reasons
truthfully. Do not infer authority from a sender name, prompt or task membership.

Expose the same request and lookup contracts through a noninteractive CLI
client to the existing owned foreground host. Do not create a second journal
writer or hidden daemon. Reuse authenticated opaque targets, operation keys,
current authority and generation checks. Missing host/authentication must refuse
without prompting or launching anything in noninteractive mode. Keep bootstrap
codes/session tokens out of argv, URLs, output, journals and telemetry.

Retain-hold records an explicit disposition without clearing uncertainty or
granting work. Cancel applies only to eligible unsubmitted context. Dead-letter
uses a typed reason. A linked-delivery proposal preserves parent lineage and
requires separate current recipient/grant validation before any new delivery;
it cannot transfer authority or imply execution. Agent/human prose does not
become a grant, vote or approval through these commands.

## Required acceptance

### Accepted focused refresh correction

2026-09-09: the lead independently passed twelve current client/host refresh
tests in 10.56 seconds on an owned copy. All 254 watched source files and owned
copies remained unchanged. Synthetic home and empty PATH, disabled telemetry,
autoload, bytecode/cache, denied child launch and non-loopback connections bounded
the run. Coverage includes private token-file provisioning, public refresh,
GET-only status, lost response, serialized refresh/rollback and a full bounded
history that refuses new mutation while preserving old-key reconciliation.
Source review confirms workspace/run checking precedes authority installation.

This supersedes the draft refresh defects below for the tested scope. Full F17
public send/provision/disposition/reopen and retain/link journeys remain required.
The per-host history is non-evicting and bounded; restart invalidates its control
credential and does not prove prior unknown outcomes. Do not rerun unchanged
focused checks merely to accumulate counts.

### Public refresh review requirements

The new refresh draft must bind expected workspace/run and intended policy
generation before authority installation. A response-only identity comparison
is too late. Use a typed control request with server-side identity checking;
an authenticated preflight does not replace that mutation-bound check.

Refresh response loss, oversized or malformed success data must classify the
outcome as uncertain. Supply an actual read-only control reconciliation operation
bound to the requested generation and canonical policy identity or operation key.
An empty refresh body followed by a stale-generation refusal is not proof of
which policy was installed. Never suggest blind retry as recovery. Enforce exact
integer types for bounded generation/count and zero-provider claims.

Provision the separate control credential through a protected local file for
noninteractive use, without requiring reusable credentials to be copied from
stdout/stderr. Preserve the existing separate browser bootstrap contract.
Control authentication remains distinct from browser/read credentials.

### Current-source leadership checkpoint

The source review confirms that `_workspace_commands.ACTIONS` currently exposes
only `cancel_queued_context` and `dispose_held_context`. `bind_command` explicitly
refuses deliveries with a parent binding. Retain-hold and a separately authorized
linked-delivery proposal remain required; adding labels alone does not close them.

`WorkspaceHost.refresh_command_policy` has accepted generation/rollback/locking
coverage. Public refresh and GET-only refresh-status now have focused acceptance,
including private control-token files and bounded request history. The complete
public provisioning and refresh journey remains unproven. Existing policy
targets must already be present in the delivery map: an ordinary new send cannot
silently gain disposition rights. Complete explicit control provisioning for
new deliveries through the current owner, preserving separate authentication
from browser/read credentials and bounded current-generation scope.

The current send response projects `delivery_id` as an opaque reference, while
`_read_command_policy` requires an internal delivery ID present in the workspace
delivery map. The component journey obtains that ID through private coordinator
state. Public-entry acceptance must prove a supported owner-only provisioning
route across this boundary without private test introspection. Resolve references
under the current workspace/run and explicit owner authority; reference possession
is not authorization. Do not expose internal IDs in public projections to bypass
this gap. Until that route is demonstrated, classify provisioning as unproven,
not as a confirmed runtime defect or a completed user journey.

Source now includes the bounded owner-only bridge
`WorkspaceHost.provision_command_policy_for_operator_message`. It resolves a
sent-message operation against one coherent current journal prefix, verifies the
canonical operator-send record and current delivery/message binding, atomically
updates the configured private policy, and delegates to the normal serialized
refresh transaction. The browser bearer and opaque delivery handle cannot call
this method or supply a raw delivery ID; the returned refresh projection remains
ID-free. The loopback acceptance exercises this bridge followed by the separate
control-token status lookup, browser command, and fresh-authentication reopen.
This is source/loopback evidence only; retain/link and rendered acceptance remain
open.

The next acceptance packet must exercise this sequence through public entry
points: create/open, send, provision current delivery rights, inspect available
actions, perform a disposition, reconcile the same operation key, explicitly
refresh, and reopen. Include retain-hold, typed dead-letter reason and linked
proposal with fresh-grant refusal. Preserve the already accepted client response
validation and serialized host refresh regressions; component pass counts do not
substitute for this journey. No provider or second journal writer is authorized.

Exercise actual public host/CLI send, same-key lookup, eligible dispositions
and reopen. Include conflicting keys, stale authority/generation, scope expiry,
lost response, unknown commit outcome, linked-parent identity and fresh-grant
refusal. Verify no duplicate mutation, no provider launch, no second owner and
no secret-bearing output. Injecting an adapter only in the test fixture does
not prove `WorkspaceHost` wiring. Keep existing component tests and add the
small actual-entry coverage needed by the finished implementation.

The conductor may delegate bounded isolated implementation lanes and retains
sole shared integration. F17 stays the current implementation priority; this
brief does not authorize providers, account changes, installs, commits or release.

### Subsequent public HTTP checkpoint

The owner bridge is now exposed at `/api/commands/provision-message` with
separate control authentication. Its request uses a message operation key and
explicit actions, workspace/run identity and request ID. The public HTTP
send/provision/conflicting-action refusal/command/reopen lookup journey passed
independent isolated execution in 5.49 seconds, with 254 watched files and the
owned source unchanged. This supersedes the earlier direct-method-only gap at
the covered HTTP scope. Retain/link, complete negative authority/capacity/rollback
coverage and final combined acceptance remain required; F17 is not closed.

### Retain event/state checkpoint

The canonical `workspace_operator_disposition_recorded` event now records
`retain_held_context` with finite reasons and task/delivery-bound event references.
Independent isolated execution of `test_workspace_retain.py` passed eleven
cases in 0.51 seconds, with 255 watched source files and the owned source
unchanged. Covered behavior includes held-only validation, closed payload shape,
idempotent replay, state event bounds and no delivery mutation.

The authenticated retain path is now wired through the trusted host and loopback
surface as `POST /api/dispositions` with same-key `POST
/api/dispositions/lookup`. The public body contains only an opaque delivery
handle, operation key and finite reason. The host resolves the current held
delivery and exactly two task/delivery-bound ordinary event references (excluding
settlement references), then appends the typed event through the owner writer.
Provider-free coverage now includes direct runtime idempotence and the actual
authenticated HTTP route; both retain tests pass. Legacy `/api/commands` remains
unchanged and does not accept the retain action.

This checkpoint does not claim a real recovery-reservation mutation, linked
replacement, or old-reader refusal. The coordinator derives reservation capacity
from resulting workspace state; unchanged delivery state supports the design but
does not replace the capacity-boundary test. Linked replacement remains
unfinished.

### Retain command proof review: revise

The initial runtime selects exactly two ordinary event references with matching
task and delivery and assigns request/decision roles by sorted reference ID.
Registration and matching scope do not prove either reference represents the
current request or its authenticated decision. Passing wiring tests do not close
this boundary.

Require trusted typed proof resolution or minting with explicit roles and actual
material bound to the current operation, action, reason, task, delivery and
authenticated authorization decision. Two unrelated historical ordinary refs,
swapped roles and mismatched operation/reason must refuse. Preserve fail-closed
behavior and the independently accepted event/state slice. This correction is
within the approved contract, not a new permission or scope decision.

### Subsequent typed-proof and linked-validation checkpoints

The sorted-reference implementation above was superseded by typed private-byte
resolution with digest verification and explicit request/decision roles bound to
the operation, action, reason, task and delivery. Independent isolated execution
passed fourteen retain/runtime/HTTP cases in 6.47 seconds, with 255 watched
source files and the owned source unchanged. This accepts that tested resolution
and fixture HTTP slice, not complete ordinary-host provisioning.

The current source subsequently adds owner-only proof provisioning and selects
matching proofs by operation rather than requiring only two historical references
for the delivery. These newer changes await independent ordinary-host and repeated
distinct-operation acceptance. Preserve historical evidence and test partial
provisioning, conflicting reuse, reservation capacity and old-reader refusal.

The isolated linked-replacement proposal and authority-shape guard passed six
independent cases in 0.17 seconds, with 256 watched inputs and the owned source
unchanged. Additive integration of that pure validation slice is cleared. It does
not authenticate caller-supplied source facts, authorize child mutation, or prove
atomic admission. Trusted source resolution, current authorization/fence checks,
one-child-per-parent enforcement and reservation preservation remain required.

F17 remains open until the complete public journeys and combined acceptance pass.

The new open-host provisioning test subsequently passed independent isolated
execution (one case, 5.26 seconds; 255 watched source files and the owned copy
unchanged). It proves owner-created typed proof material and a recorded retain
through the actual open host. It invokes the surface directly for one operation;
authenticated HTTP, repeated distinct operations and lookup after reopening
remain separate acceptance requirements.

The expanded version of that host test subsequently passed independently in
7.32 seconds (255 watched source files and owned copy unchanged). It now uses
authenticated HTTP for two distinct retain operations/reasons on one held
delivery, checks all four historical operation/role-bound proof references,
and performs same-key lookup after host restart and fresh authentication.
This supersedes the single-operation/direct-surface limitation above. Fixture
setup still supplies the held delivery and private policy, and target discovery
uses the host projection directly. Recovery-reservation capacity, old-reader
refusal, linked replacement and combined acceptance remain open.

### Public retain discovery and presentation gap

Current `collect_snapshot` and `WorkspaceSurface.snapshot` pass command, message
and draft adapters only; the page renders `operator_commands.actions_by_delivery`.
The disposition adapter is therefore reachable by HTTP but not exposed through
the ordinary snapshot/action flow. The accepted retain test obtains its opaque
target by calling the adapter internally. Add a bounded, scoped disposition
projection and explicit audit-only retain UI with finite reason selection,
same-key recovery and an honest unavailable state. Final public acceptance must
obtain the target through the authenticated snapshot rather than internal
adapter access. Keep retain separate from delivery-changing legacy commands.

The subsequent snapshot-based host journey currently fails independent execution:
the authenticated snapshot after reopening returns HTTP 409 instead of 200.
The separate wrong-task projection regression passes (combined one failed, one
passed in 7.91 seconds; 255 watched source files and owned copy unchanged).
The conductor has the failing node and boundary. Public discovery/reopen remains
unaccepted; earlier direct-discovery HTTP success does not supersede this failure.

After the retain event label was added to the view, the snapshot-based host
journey and wrong-task projection test both passed independently in an owned
copy (two cases, 8.57 seconds). That owned copy stayed unchanged, but the shared
source changed during execution. This proves the captured copy's repaired
journey; stable integrated-candidate acceptance remains pending.

The conductor then identified a stable slice. Independent execution passed four
focused cases in 11.17 seconds: public snapshot-based retain/reopen, wrong-task
projection refusal, authenticated retain route/idempotence and client disposition
validation. All 255 watched source files and the owned copy stayed unchanged.
The reproduced HTTP 409 is verified repaired at this scope. Rendered UI recovery,
recovery-reservation capacity, old-reader refusal, linked replacement and final
combined acceptance remain open; this checkpoint does not close F17.

The linked proposal/authority-shape guard is now integrated in the shared source.
Independent execution of its six collected cases passed in 0.18 seconds, with
256 watched source files and the owned copy unchanged. This accepts the additive
pure guard integration only; authenticated material resolution and atomic child
admission remain required.

### Historical-reader evidence boundary

Read-only history inspection, independently repeated by the lead, finds no
committed workspace state/admission reader: both paths are absent from HEAD
and have no all-ref history. The committed coordinator has no diff from the
verified `v3.2.1` coordinator. Do not construct a supposedly historical reader
by removing a branch from current code.

Separate historical published-layout mutation fencing from F17 event-version
refusal. Exercise the actual historical stack against the published workspace
address and verify all owned journal/sidecar bytes and directory entries remain
unchanged. A status result of missing is not unsupported-format recognition.
Historical mutation may acquire ownership and repair before replay, so eventual
unknown-event refusal alone does not prove no mutation.

If a supported pre-retain workspace packet exists, preserve its complete matching
reader dependencies and prove acceptance of the held prefix before rejection of
the actual retain-bearing journal. Identify its support/distribution status;
unreleased development snapshots do not implicitly establish a compatibility
promise. The current-reader invented-future-event test remains strictness
coverage only. Historical behavior has not yet been executed in this checkpoint.

Support-matrix decision: the committed 3.4.0 changelog/tree has no workspace
reader product, and EXPECTED_NEXT_RELEASE introduces it as an additive 3.5
preview. No prior released workspace reader is established by repository
evidence. An F17-specific pre-retain reader test is therefore not applicable
to that declared released baseline; do not block on an unshipped development
packet. This does not waive the actual historical ordinary-swarm layout fence,
current unknown-version refusal, or any separately evidenced distributed-reader
compatibility promise. Those applicable runtime checks still require execution.

The actual historical ordinary-swarm published-layout gate subsequently passed
independent execution: one unittest in 0.540 seconds, with 256 watched shared
source files and the owned copy unchanged. The test verifies a fixed historical
3.4.0 source identity, rejects the published address at the generation-file
directory fence, preserves complete owned journal/sidecar/directory snapshots,
creates no outer owner lock, and permits current inner recovery of a torn tail.
Git access was read-only; Python children were isolated and hidden. An initial
review-harness argv parsing refusal occurred before Git and was corrected in
the harness. This accepts the applicable historical layout boundary, not retain
reservation capacity, linked admission, or all of F17.

### Linked runtime review remains conditional

The first four linked-runtime tests passed independently in an unchanged owned
copy (12.93 seconds), while shared source changed during execution. Covered:
one child, immediate same-key reconciliation, tampered bytes, and forced
no-space refusal. Remaining findings: ambiguous journal I/O must not become a
definite capacity refusal through exception-text matching; the final append
callback must recheck installed link authority and fresh evidence; and a
used-bytes-plus-one limit does not prove preservation of recovery reservations.
Require the exact ordinary-versus-reserved boundary with permitted recovery
still appendable, plus later-event/reopen reconciliation and competing keys.
Do not widen the acceptance claim from these initial passing cases.

The revised linked-runtime suite passed independently: five tests in 16.15
seconds, with 257 watched shared source files and the owned copy unchanged.
This adds third-resolution expiry refusal, same-key lookup after an unrelated
append, and reserve arithmetic around retain admission. Exact coordinator
capacity reasons replace arbitrary journal-text matching; I/O remains uncertain.
Final-boundary handle revocation remains open: the append callback captures an
installed authority object without checking that the handle remains installed.
Require fresh request validation inside that callback and a deterministic
revocation regression. The retain test's timestamp margin and reserve equality
do not yet prove that a permitted recovery append succeeds at the boundary.
Public linked execution and rendered recovery remain separate acceptance gates.

The subsequent final-callback repair revalidates the handle before and after
fresh resolution; its deterministic revocation test passed independently.
The six-case owned-copy run nevertheless returned five passes and one failure
in 19.67 seconds, with all 257 watched shared files and the owned copy unchanged.
The retain test expected recorded at its computed exact capacity but received
uncertain. Its reported green result is therefore not independent acceptance;
resolve the boundary discrepancy without adding slack or weakening the check.

The discrepancy was traced to owner generation changing frozen record length
between refused attempts. Computing each attempt's limit from its actual frozen
bytes passed independently: six tests in 19.13 seconds, 257 watched shared
files and owned copy unchanged. Require restoration of the test's directly
assigned MAX_JOURNAL_BYTES before aggregate execution. This verifies the covered
exact capacity arithmetic, not an actual reserved recovery append or public
linked-task journey; those remain open.

The following checkpoint passed seven tests independently in 24.56 seconds,
with all 257 watched shared files and the owned copy unchanged. Test capacity
overrides now restore correctly. A separately authorized trusted recovery event
actually appends after retain at its exact synthetic capacity boundary; the
dead-lettered parent retains unknown certainty and a positive reserve. Accept
this bounded runtime recovery evidence. Proof registration precedes the bounded
append and limits are selected per attempt; this is not a public browser journey
or a complete fixed-budget lifecycle. Continue public linked execution,
reopen/collision and rendered acceptance without rerunning unchanged cases.

Linked client response binding was corrected after lead review: parent and
recipient must match the request, child handles are bounded opaque values,
recorded results require a child and not-observed results cannot assert one.
Independent client plus four refresh regressions passed 19 tests in 9.53 seconds,
with 258 watched shared files and the owned copy unchanged. New mismatch cases
cover parent, recipient and malformed child responses, returning lookup-required
uncertainty. Client fixtures mock HTTP and private-file validation; this is not
the ordinary-host linked journey or rendered recovery acceptance.

Two linked public-surface HTTP nodes subsequently passed independently in
7.19 seconds, with 258 watched shared files and the owned copy unchanged.
They cover opaque authenticated submission, forged-target refusal without
mutation and same-key lookup after stopping/starting the same surface. The
fixture retains its runtime, installed adapter/handle and presentation key;
it does not establish fresh runtime authority installation or ordinary host
reopening. Those remain the next integration gates.

Fresh WorkspaceHost/runtime reopening subsequently passed one independent test
in 7.67 seconds, with 258 watched shared files and the owned copy unchanged.
The fixture explicitly reinstalls link callbacks and authenticates lookup.
It also privately preauthorizes each execute proposal through
_record_install_command before installation and after reopen. Thus it proves
the bounded reinstall mechanism, not yet a supported operator workflow.
Connect proposal authorization to installed scoped host policy; require no
authority before reinstall and unchanged parent/one-child state after lookup.

The updated host journey passed independently in 7.39 seconds, with 258 watched
shared files and the owned copy unchanged. Execute-specific private grants are
removed: installed target-pair scope now participates in host authorization,
with runtime authority/source checks retained. Scope clears on stop/revoke;
the reopened snapshot exposes no link authority before explicit reinstall.
Accept this bounded host policy/reinstallation checkpoint. Parent/child state
equality, one-child collision behavior and rendered pending recovery still
need their remaining journey assertions.

The public-surface journey now separately authorizes its competing operation
key. Independent execution passed one test in 5.07 seconds, with 258 watched
shared files and the owned copy unchanged. It verifies sequential authorized
second-key refusal, unchanged parent, exactly one child and unchanged lookup.
This is not simultaneous racing-owner evidence. The new page pending-link test
executes JavaScript with stubbed DOM/fetch; it is useful control-state coverage,
not an actual rendered-browser recovery gate. Retain that distinction in the
final F17 acceptance packet.

The conductor added a real Chromium restoration/expiry/lookup test and reported
one pass in 6.63 seconds. Lead source review found the operation is pre-recorded
through runtime and pending browser storage is injected. Screenshot bytes are
checked for size but discarded. Treat this as conductor-reported real-browser
restoration coverage, not complete rendered submission/recovery acceptance.
Require actual button submission, a lost response after durable append, reload
and authentication recovery using the original key, no second submission,
unchanged parent/uncertainty and one child, plus retained sanitized visual and
toolchain evidence. Independent execution remains pending.

### Public linked host/client/page integration checkpoint

The linked replacement seam is now wired through the ordinary authenticated
surface and noninteractive client. `WorkspaceSurface` accepts a separately
installed `OperatorLinkedReplacementAdapter`; snapshots expose only opaque
parent/recipient handles; `POST /api/linked-replacements` and the same-key
`/lookup` route preserve immutable parent lineage and never retry implicitly.
`WorkspaceHost.install_operator_linked_replacements` provides the bounded
trusted-host installation hook and requires the host to supply the already
authorized resolver; the in-memory handle is revoked on stop/restart. The
browser page has a pending-link panel with session-storage recovery and same
operation-key lookup, with no raw source, grant or delivery identifiers.

Provider-free evidence: linked public HTTP/reopen nodes passed independently;
view/client compatibility and page behavior passed in focused partitions.
Fresh-runtime host reinstall/reopen with actual authority/source material and
competing-key/collision coverage remain required before F17 can be marked
complete. No provider, install, commit or release action is implied by this
checkpoint.

### Independent rendered recovery checkpoint

The lead independently replayed the current real Chromium browser test from an
owned source copy: 1 passed in 7.16 seconds; shared and copied sources remained
unchanged. The existing installed browser was used without installation. This
supersedes the earlier pre-recorded-operation/browser-storage limitation: the
current journey clicks the real submission button, drops its response after the
append, reloads, expires authentication, reauthenticates, and looks up the same
key. Assertions cover one submission, one child and unchanged parent uncertainty.
The lead inspected retained dropped-response and post-lookup desktop screenshots;
the linked panel is within the composer and no overlap is visible at that size.
This test uses an injected trusted WorkspaceSurface fixture, so ordinary-host
installation/reopen coverage remains separate evidence, not one end-to-end browser
claim. The conductor's broader 105-pass partition is reported evidence only.
F17 closure awaits a consolidated criterion-to-evidence packet; this checkpoint
does not close the entire requirement or change release progress.

Independent concurrent linked-writer checkpoint: the new barrier-synchronized,
independently authorized competing-key test passed in 4.36 seconds. All 259
watched source files and the owned test copy remained unchanged. It proves one
successor, an explicit loser outcome, unchanged parent and preserved inherited
uncertainty for two threads using one runtime. It does not claim independent
process recovery. Ordinary-host browser and advanced-child recovery evidence
remain pending in the conductor's combined packet.

Advanced-child recovery checkpoint independently passed: one test in 8.83 seconds,
259 watched files and owned source unchanged. The actual linked child advances
to held, records typed retain and same-key lookup, then typed disposal and lookup.
Assertions preserve parent bytes, child message/parent lineage and all inherited
unknown effects. Ordinary-host rendered recovery remains the next independent
check; this does not alone close F17.

Ordinary-host Chromium recovery independently passed one test in 8.12 seconds,
with shared and copied source unchanged. The real host installation API, browser
submission, dropped response, reload, expiry and reauthentication recover the
same key with one child and unchanged parent. Retained desktop screenshot was
inspected. Host restart remains separately tested; the browser fixture itself
reuses one host despite its current reinstall name.
Final contract item 7 still needs original retain/link lookup after the child has
subsequently advanced: the current new test looks up retain before disposal.
Requested the precise post-disposal assertion before complete F17 acceptance.

Final advanced-history checkpoint: original linked operation and original retain
both reconcile successfully after the child is disposed. Independent replay
passed one test in 7.57 seconds, with 259 watched source files and the owned copy
unchanged. Together with independent concurrent-writer and ordinary-host browser
checks, this covers the three final gaps in the conductor's F17 acceptance matrix.
Final consolidated closure handoff and status reconciliation remain pending;
no additional provider qualification or independent-process recovery is implied.
