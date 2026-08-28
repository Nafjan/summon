# Phase 1 migration and rollback

Current product version: 3.3.0

This is the active migration contract for the Phase 1 control plane. Historical 2.x to
3.0 instructions remain in `VERSIONING_AND_3.0.md`, but a current release must satisfy
this document and the machine-checked canonical version.

## Compatibility boundary

- The response `envelope: 1` contract remains additive. Existing fields do not change
  meaning because fleet, usage, context, liveness, and portable-result fields exist.
- The experimental portable result is a redacted view, not a replacement for the private
  execution envelope and not an authority token.
- Fleet approval and usage cache state are local control-plane data. They never become
  provider credentials or implicit spend consent.
- Managed installs are ownership-bound. Project-vendored and other unmanaged copies are
  reported separately and are updated only through an explicitly authorized path.

## Phase 1 durable state

An upgrade can encounter these stores outside the managed skill payload:

- background job records, immutable per-job script bundles, controls, and continuations;
- conversation, council, deliberation, and swarm journals;
- the authenticated fleet approval store and its local key;
- imported usage cache, authenticated live-usage store, and anti-rollback checkpoint;
- opt-in local telemetry; and
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
6. Contact providers only after provider-inert convergence and the explicit release
   evidence gate pass.

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

## Release decision

`tools/release_contract.py` binds this document to the canonical version in
`plugin.json`. A stale version marker, incomplete migration language, dirty source tree,
failed fixed test, missing private live-provider evidence, or divergent managed install is
a release failure. A release note or model endorsement cannot override those gates.
