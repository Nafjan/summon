# Summon development handoff

Prepared 2026-09-22 for the incoming development agent. Receiving agent starts
with zero context; this document is the complete briefing. Companion documents:
`ASTRA_HANDOVER.md` (previous handover, still accurate where not superseded),
`RELEASE35_COVERAGE_MATRIX.md` (authoritative requirement dispositions),
`RELEASE35_IMPLEMENTATION_HANDOFF.md` (conductor checkpoints).

## Task

Own the continued development, review, and release stewardship of Summon
(this repository, branch `codex/gemini-flash-3.8`,
HEAD `af9afc1`, clean tree, product version **3.5.0**). Investigate-then-implement:
verify every claim in this document against the source before acting on it, fix
what is broken, and advance the backlog in priority order. Commit locally with
the repo's conventions; every behavioral change needs a pinning test.

## Context

Summon is a cross-vendor sub-agent dispatcher (72-seat operator roster +
bundled roster) with a 3.5.0 workspace preview. The 3.5.0 release candidate is
committed and machine-accepted locally; publication is deliberately reserved
to the owner. The last full clean-source capture binds `fb41dae` (27/27
suites, 4,920 cases + 164 subtests passed, 3 reviewed-policy skips,
suites+gates 5,488 passed, Windows qualified, 44/44 rendered artifacts);
`release_manifest.py --check --profile workspace-preview --expected-version
3.5.0` **passes** against that evidence. Post-release commits (`443635e`
ModelArk routes, `822bc63` docs, `3e1a3f1` Astra handover, `5804ea9`
convergence fix, `a46cedb` dispatcher friction fixes, `af9afc1` review
hardening) carry focused-partition evidence only.

A private run ledger (19+ iterations of evidence and dispositions) is kept
outside the source tree. The operator memory file (auto-injected into every
dispatch on the operator's machine) lives beside the operator roster — read it
before dispatching anything.

## Relevant files

- `tools/release_gates.py`, `tools/release_manifest.py`, `tools/release_outcomes.py`,
  `tools/release_skip_policy.json` — the fixed gate registry, source-bound
  evidence, manifest profiles (`stable` vs `workspace-preview`).
- `tools/release_contract.py` — version contract (plugin.json is canonical;
  dispatcher/telemetry/mcp_server + migration-doc marker must equal it).
- `skills/summon/scripts/run_subagent.py` — dispatcher entry (roster
  resolution, dry-run view, credential preflight, list table/filter).
- `skills/summon/scripts/_builder.py`, `_apibackend.py`, `_executor.py`,
  `_kimi_backend`/ACP paths, `_installs.py` — dispatch and install machinery.
- `skills/summon/references/model-catalog.json`, `references/backends.md`,
  `references/models.md` — editorial catalog and backend docs.
- `skills/summon/scripts/test_model_routing.py` (routing/credential/list
  tests), `tests/test_release_*.py`, `tests/test_migration_gate.py`,
  `skills/summon/scripts/test_discovery.py` (doc/leak/version pins).
- `install.py` — managed-install refresh (`python install.py`; does NOT cover
  the `.zcode\skills\summon` host — refresh that copy manually).
- `<local temp>\opencode\` — the only cwd OpenCode accepts for dispatches on
  the operator's machine (restricted permission policy).

## Current state

- **Release**: `fb41dae` is the machine-accepted 3.5.0 workspace-preview
  candidate (manifest PASS at that commit). Post-release commits `443635e`
  (ModelArk routes), `822bc63` (route-facts docs), `a46cedb` (dispatcher
  friction fixes), `af9afc1` (review hardening) carry focused-partition
  evidence (catalog/routing 22, inventory/discovery 124, model-routing 20)
  but no full clean capture yet.
- **Reviews**: the friction-fix commit was adversarially reviewed
  (rseen-reviewer: 1 blocking + 3 review + 3 nit) and all findings were fixed
  in `af9afc1` (real gate tests for `credential_missing` dry-run + live
  preflight; endpoint-gate parity; first-refusal-wins ordering; table column
  removed; dead guard removed). Independently re-verified (rseen-verifier:
  CONFIRMED, 20/20 tests both modes, ruff clean on changed lines).
- **Backends**: all 9 usable (claude 2.1.276, codex 0.153.4, cursor-agent via
  shim → `agent` 2026.09.18, gemini 0.43.0, kimi 2.0.1, agy 1.2.7, opencode
  1.18.29, zcode 0.16.5, arkcli 1.0.32). 72/72 seats dispatchable; 6 sit
  behind by-design consent gates.
- **ModelArk seats** (subscription lanes, live-verified with provider-reported
  served evidence): `deepseek-41-flash` (platform `/api/v3`, text-only,
  heavy-use default), `modelark-glm-coder` (platform, text-only), `glm-coder`
  (OpenCode `coding-plan/glm-5-3-flash`, **file-capable**, verified reading
  files through the tool loop).
- **Installs**: all managed copies converged to HEAD; `.zcode` copy refreshed
  manually (installer gap); two vendored 3.4.0 copies (`.agents\skills\summon`
  inside a third-party clone; `.cursor\plugins\local\summon` dev tree) are
  intentionally untouched.
- **Not done**: publication (push/PR/tag/release — owner-reserved); U03/U07
  device observations (human operator); stable profile (needs live-provider
  receipt); full clean capture bound to HEAD.

## What was tried

- **Vacuous test for the credential gate** — the first test only exercised
  helper functions that predate the commit; deleting the safety code left the
  suite green. Fixed in `af9afc1` with two real gate tests (dry-run view
  refusal; live `call()` preflight with `_do_request` asserted never invoked).
  Lesson: a gate test must fail when the guarded code is deleted.
- **Mid-capture source edit** — editing CHANGELOG while the canonical capture
  ran tripped the structured-outcome binding and discarded the run. Rule:
  the tree is frozen for the whole capture; edits happen before or after.
- **Auto-appending guardrail text to kimi prompts** — rejected: breaks the
  byte-identical prompt provenance contract. The working fix is `transport:
  acp` on yolo kimi seats (permission requests auto-answered; verified 34s
  file-read) with text-constrained prompts as the alternative.
- **ModelArk platform route via OpenCode** — OpenCode's own request shape
  errors against this provider (request identifiers retained privately); the
  coding-plan provider works. Upstream
  bug; use the `openai-compat` modelark route in Summon.
- **DeepSeek V4.1 Flash on the Coding Plan** — refused by BytePlus in all ID
  forms (provider-side; request IDs retained). Its only route is the platform
  API under subscription activation. API/PAYG billing is disabled on this
  account.
- **Silent reroutes/defaults** — deliberately rejected twice (no default seat
  for `--cli`, no silent provider switching); fail-closed with helpful reroute
  text is the house style.

## Decisions

- Fail closed with typed errors and helpful reroutes over silent fallbacks,
  everywhere.
- Editorial catalog membership pins (`test_model_catalog.py`) and the version
  pin (`test_release_contract.py`) move only as deliberate, committed acts.
- `summon-executor/3.4.0` identity-scope strings and the L04 3.4.0 rollback
  fixture pins stay: they bind historical records and the rollback baseline.
- Prompt bytes are immutable provenance: never mutate a caller's prompt
  (guardrails belong in seat definitions or transport).
- Subscription capacity only; no PAYG/credit; auth repairs surface the
  vendor command instead of running it.

## Acceptance criteria

- [ ] Every claim in this document independently verified against source
      before acting on it.
- [ ] Working tree stays committed (clean) at the end of any work session.
- [ ] `test_model_routing.py` (20), `test_release_contract.py` (7),
      `test_migration_gate.py`, `test_release_manifest.py` (26) pass in
      normal and `-O` modes after any change.
- [ ] New behavioral changes are pinned by tests that fail when the code is
      deleted (no vacuous tests).
- [ ] Any fresh full capture completes with zero unexplained failures and
      only reviewed-policy skips before a manifest check is claimed.
- [ ] Installed copies refreshed (`python install.py`) and
      `managed_converged: true` verified after any payload change; the
      `.zcode` copy refreshed manually alongside.
- [ ] U03/U07 device observations collected by a human operator before the
      accessibility gate is claimed.
- [ ] No statement that 3.5.0 is published unless the owner executed the
      publication and said so.

## Constraints

- Owner-reserved, never do without explicit instruction: push, PR, tag,
  GitHub release, publication, version bumps beyond the current state,
  credential changes, PAYG/credit enablement, auth-repair runs (surface the
  vendor command instead).
- Provider spend: subscription capacity only; bounded smokes; never retry
  auth/quota failures (surface the repair path).
- Keep separate: council advisory vs fixed-option deliberation authority;
  requested/targeted/served model identity; provider-inert evidence vs live
  qualification; estimate vs reported vs unknown spend.
- Privacy: never expose credentials, account details, private prompts, raw
  receipts, telemetry contents, or machine fingerprints in public documents;
  L06 scans before any publication.
- Preserve: pending roster/seat state, uncommitted operator work, the
  `.zcode` copy's manual-refresh requirement, the two vendored 3.4.0 copies
  (owner curation, not yours to delete).

## Handoff prompt (paste into the receiving agent)

```text
You are taking over development of Summon. Read
docs/planning/SUMMON_DEV_HANDOFF.md first and verify its claims against the
source before acting (docs/planning/ASTRA_HANDOVER.md and
RELEASE35_COVERAGE_MATRIX.md are the deeper contracts). Work only on branch
codex/gemini-flash-3.8. Commit locally; never push, publish, or bump versions
without the owner's explicit instruction. Provider use is subscription-only,
bounded, and never retried on auth/quota failure. Every behavioral change
needs a pinning test that fails when the code is deleted. Priority order:
verify the handover claims, then the 3.5.1 backlog in
ASTRA_HANDOVER.md section 4, then the owner's directives.
```
