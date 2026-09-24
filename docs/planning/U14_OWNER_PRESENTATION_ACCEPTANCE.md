# U14 owner and passive-mode presentation acceptance

2026-09-08. Architect-reviewed candidate is integrated; the combined independent
partition passes 140 cases, including page/view, preview entry, council readers,
historical deliberation readers and the observer control seam. Production page
and view exactly match the rendered candidate. Page tests retain the same cases;
their only substantive harness difference reports synthetic assertion errors to
stderr. The ten separate entry-lifecycle cases also passed. The architect accepts
U14 for the existing minimal 3.5 preview cut after the exposed-route assessment
below. This is a bounded workspace-preview
assessment, not a live-supervisor, accessibility, browser-matrix or release claim.

## Requirement and observed behavior

### Release disposition: VERIFIED-FOR-3.5

The normative preview cut in `WORKSPACE_CHECKLIST_V3.md` explicitly defers P2
supervision. Current `_cli.py` workspace mode admits create/open/inspect and the
explicit demo-create fixture; it does not admit background dispatch flags.
`WorkspaceHost.start` opens a foreground-owned local HTTP shell and registers
context recipients without launching or resuming workers. The operator endpoints
queue/reconcile context only. The page exposes no implemented queue evaluator
or supervisor start action, and says so persistently. The conductor independently
confirmed this route enumeration from source.

Acceptance combines exact production equivalence to the rendered candidate,
the integrated 140-case partition, the ten dedicated foreground lifecycle cases,
and the native-browser observations below. Closing/reopening a browser does not
claim worker death, launch work, renew a lease or clear spend uncertainty. The
public shell's missing evaluator is explicitly unavailable, not a copyable
command that silently starts supervision. Active-owner projection fixtures prove
presentation only; they do not qualify a real supervisor.

This resolves the old matrix reference to full background-supervisor evidence
against the already adopted minimal preview scope; it does not introduce a new
scope cut. P2 owner-process delivery, U07/U08 full UI qualification and L09 final
rendered/toolchain artifacts remain separately open. Any future exposed evaluator
or background supervisor requires its own authorization/lifetime acceptance.

The current workspace surface can retain and display queued context but has no
implemented queue-evaluation control. Its guidance must say evaluation is
unavailable here and that queueing context or reopening the workspace does not
start workers. This does not remove a future adapter's separately authorized
evaluation path or imply that the local HTTP server is a supervisor.

| Requirement | Evidence |
| --- | --- |
| Persistent passive mode and sanitized owner identity | The page displays Passive and only the validated active, not-running, stale or unknown owner label. Source review and page/view fixtures cover inconsistent or missing observations. |
| Separate owner observations from current connectivity | Native browser inspection shows snapshot labels, explicit refresh for newer observations, and retained active ownership labelled connection-uncertain after failed view reads. Task, contact and spend values remain unchanged. |
| Honest recovery and expiry | Explicit refresh changes the recovered display to stale/passive and then not-running/passive. Session expiry hides the workspace, stops authenticated polling and resets the displayed owner to unknown with no confirmed snapshot. |
| No implicit evaluation or launch control | Queue-unavailable guidance remains present for passive and supervised snapshots. Behavioral tests and the synthetic HTTP request counter show no evaluation/launch request from rendering or refresh. |
| Readable owner and queue guidance | Native screenshots were inspected at 1280 by 720 and 375 by 812. At the narrower size, the document width remains 375 and both text regions fit within it. This is not a general responsive or WCAG assessment. |
| Owner changes participate in freshness binding | Independent use of the actual projector changes the snapshot binding for active versus stale observations without changing the synthetic workspace. |

## Source and verification

The candidate changes only `skills/summon/scripts/_workspace_page.py`,
`skills/summon/scripts/_workspace_view.py` and their two existing test files.
The mode schema and queue-guidance string field remain unchanged. Presentation
does not authorize work, add commands, start a daemon or reinterpret uncertainty.

The helper reports 115 passing page/view cases. Independent execution against
the final isolated source also passes 115 cases in 1.55 seconds with Python
3.12.10, pytest 9.1.1 and the existing Node harness. The helper reports Node
v25.2.1. Fake-DOM execution is behavioral evidence; the separate native in-app
browser observations above supply the scoped rendered checks.

The browser fixture used the actual candidate page and projector with synthetic
queued work and owner observations. Its polling control supplied synthetic
snapshot/revision changes; an independent projector check additionally verified
the real owner-to-snapshot binding. Transport failure and expiry were synthetic
HTTP responses, not provider or operating-system network tests. One selector
observation timed out before bounded reconnection completed; a subsequent
observation and explicit refresh verified recovery without restarting the page.

The fixture recorded one bootstrap, 155 view reads and zero unexpected requests.
The tab was closed, its temporary viewport override reset, and the fixture server
acknowledged shutdown and exited with code zero. No provider, real account,
credential, telemetry, installation, commit or release action occurred.

Private source bindings and a sanitized observation record are retained outside
the source tree. Screenshots were inspected in the task, not retained as standalone
image files. The browser surface was Codex In-app Browser; its exact engine
version was not captured. This does not satisfy L09's final rendered-artifact and
toolchain requirements or the full U07/U08 browser qualification.

## Integration follow-up

An additional independent check of current shared source passes ten selected
`test_workspace_entry.py` cases in 15.95 seconds on Python 3.12, with external
pytest plugins, telemetry, bytecode and pytest cache disabled. The selection
covers foreground output/start/interruption/cleanup failures, terminal envelopes,
host authority revocation and reopening to reconcile the same operator message.
This is current entry-lifecycle evidence, separate from the isolated UI patch;
it does not prove a background supervisor or promotion of the queued message to
a provider delivery. The test process exited successfully with no live handle.

The checklist's normative preview cut explicitly defers P2 supervision. Assess
the shipped passive shell and explicitly owned foreground host against U14;
do not infer that an active-owner projection fixture implements a supervisor.
Any claimed background delivery route still needs its own owner, lifetime and
authorized evaluation evidence. Integration and the exposed-route assessment
are now complete for the minimal preview, as recorded above.

The conductor has integrated the reviewed four-file packet and preserved unrelated
edits. Independent affected page/view and preview-entry checks pass within the
140-case partition. These results are reconciled with on-demand create/inspect,
explicit foreground-open and the supported-route enumeration above. The release
disposition uses that combined evidence, not the isolated candidate alone.
