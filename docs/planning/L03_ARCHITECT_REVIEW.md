# L03 journal-preserving reconstruction acceptance

Architect disposition, 2026-09-08: VERIFIED-FOR-3.5 at the existing local
workspace preview scope. This closes L03, not the migration or release campaign.

The normative requirement in `WORKSPACE_CHECKLIST_V3.md` is to rebuild indexes
without rewriting old journals, approvals or receipts, preserve historical
identity uncertainty, and avoid fabricated worker authentication. F03 defines
workspace indexes as derived projections over the existing authoritative journal.
It does not require a new persistent index writer or an index publication API.

## Source and verification

`_swarm_coordinator.rebuild_projection_from_journal` validates the existing
segmented prefix and reconstructs through the canonical reducer. It produces a
detached projection with segment/prefix identity, workspace revision and explicit
absence of launch authority. It does not acquire an owner or repair/publish data.
`WorkspaceRuntime.inspect` reconstructs its view from that journal's state;
`WorkspaceHost.inspect` reads registered evidence and returns bounded counts
without starting a listener or worker.

The independent current-worktree command selected
`test_swarm_projection_rebuild.py` and `test_workspace_rebuild_acceptance.py`:
**19 passed in 4.39 seconds**, exit 0, no skips. These are local synthetic
fixtures, not provider or installed-copy evidence.

The retained rebuild suite covers deterministic detached results, expected
identity, duplicate identities, bounds, malformed generations, legacy encoding,
unstable/torn/corrupt prefixes, reparse refusal and rich multisegment recovery.
This retains the previously accepted RB-01 through RB-03 behavior.

The new workspace integration case prepares an actual workspace journal, records
a declared worker and claim, and creates uncertain spend through the coordinator's
explicit synthetic interrupted-history recovery path. A fresh runtime and the
detached reconstruction agree on workspace, task, claim and worker state.
Repeated inspection preserves the exact full fixture tree, including journal
segments and opaque historical approval/receipt sidecars. Mutations through the
runtime owner path and all subprocess launches are forbidden during inspection.
Both reconstruction authority fields stay false; a recorded worker declaration
does not gain new authentication or model evidence.

The sidecars establish byte preservation, not validity of arbitrary legacy
approval/receipt schemas. Such readers and revalidation belong to R01/R02 and
L01/L02, which remain blocking. No historical bytes or identity labels are
rewritten by the accepted reconstruction path.

## Integration and limits

The conductor owns the new acceptance file after handoff and must retain it in
the fixed release registry. L09 still requires candidate-bound integrated
evidence, skip accounting and rendered/toolchain artifacts. Installed rollback,
privacy/export, unsupported formats, live adapters and release actions retain
their existing separate gates. No new Fable endorsement is claimed; the earlier
bounded RB packet endorsement is unchanged. Final candidate review remains due.
