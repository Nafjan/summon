# Session handoff — GLM 5.3 Flash conductor → next agent

Handoff prepared 2026-09-27. The outgoing conductor (GLM 5.3 Flash via ZCode
individual coding plan) completed the 3.5.0 release execution, the ModelArk
route redesign, the multi-round adversarial review cycle, and the telemetry
plan. This document transfers ownership to the next agent.

## Task

Continue development of Summon from the current state. The 3.5.0 workspace
preview is committed, machine-accepted, and installed locally but NOT
published. The Coding Plan subscription route is the default for both
DeepSeek V4.1 Flash and GLM 5.3 Flash. The billing guard is live in the
installed copies. Your job: continue the backlog, monitor the telemetry, and
prepare for publication when the owner authorizes it.

## Context

Summon is a cross-vendor sub-agent dispatcher (72-seat operator roster +
bundled roster). The 3.5.0 release added the workspace preview, the
workspace budget race fix (clamped concurrent rate events), the ModelArk
billing guard (host-gated subscription classification + PAYG consent), and
the dispatcher friction fixes. The product version is 3.5.0 (contract
converged, ready).

The account constraint is critical: **API/PAYG billing is disabled** on this
ModelArk account; only the **subscription plan** is active. The Coding Plan
subscription serves `glm-5-3-flash` and `deepseek-v4-1-flash` via standard
`chat/completions` on `/api/coding/v3`. The Responses API path refuses
DeepSeek V4.1 Flash (provider-side). The Platform `/api/v3` route is
metered and requires explicit spend authorization.

## Relevant files

- `tools/release_gates.py` — fixed 27-suite gate registry; run with
  `--require-clean` + `--chromium-executable` for source-bound evidence
- `tools/release_manifest.py` — manifest profiles (`stable` vs
  `workspace-preview`); `--check --profile workspace-preview
  --expected-version 3.5.0 --evidence-file <json>` for acceptance
- `tools/release_contract.py` — version contract (plugin.json canonical)
- `skills/summon/scripts/_swarm_coordinator.py` — the clamp
  (`min(observed, captured_now)`) in workspace_budget_snapshot
- `skills/summon/scripts/_apibackend.py` — `classify_openai_compat_route`
  (subscription host-gate, PAYG consent, credential preflight),
  `_byteplus_route_parts` (shared URL canonicalizer)
- `skills/summon/scripts/_evidence.py` — `authority.api` projection +
  spend-consistency check (reverse implication documented as accepted risk)
- `skills/summon/scripts/test_workspace_economics_guarded_acceptance.py` —
  the pinning test (clamped event at age zero, count payload asserted,
  detached-gate pin)
- `docs/planning/TELEMETRY_ISSUES_PLAN_2026_09_25.md` — telemetry response
  plan with the as-of note
- `docs/planning/ASTRA_HANDOVER.md` — the previous handover (accurate for
  scope/context where not superseded)
- `docs/planning/RELEASE35_COVERAGE_MATRIX.md` — 63-row requirement matrix
- Private ledger: external release-evidence ledger (`LOOP_LEDGER.md`, retained outside the repository)

## Current state

- Branch `codex/modelark-billing-guard`, HEAD `07cc1b9` (clean tree, one
  untracked `.zcodeignore` committed as `b05078a`).
- Commits since the 3.5.0 base: race fix (342b716, 82807b5), clamp
  consensus (51bc2e1), R2 review fixes (4ac3869), as-of note (07cc1b9),
  ModelArk billing guard + R2 fixes (094d92e), handoff (b05078a), race
  race clamp refinement + KM-02 placement (349af69→51bc2e1 amended),
  ModelArk routes (443635e), docs (822bc63), billing guard review fixes
  (af9afc1→07cc1b9), handoff doc (4b08f40), telemetry plan (4be9cbd),
  convergence fix (5804ea9), handoff update (e96f6f0→…→HEAD).
- 125 focused release/routing/evidence tests green.
- Installs: 29 targets, `managed_converged: True`, all hosts carry the
  clamp + tolerance-free billing gate + Coding Plan seats + summon.ps1.
- Telemetry post-fix: 0 race recurrences, 0 future-markers,
  subscription-billing dominant.
- ZCode: unofficial CLI 3.14.3-28 (runtime 0.16.9) wired via `ZCODE_CLI`
  (User env → `bin/zcode.js`); `zcode-native` yolo dispatches verified.
- zcode-acp-server 0.48.0 installed, ACP handshake verified, configured
  for `build` mode.

## What was tried

- **Bound-advance design** (342b716, 82807b5) — rejected by panel consensus:
  loosens the rate window's trailing edge fail-open, covers only 1 of 4
  call sites, leaves the None-now path unprotected. Replaced by the
  per-event clamp.
- **30s tolerance constant** — rejected: ungrounded, enables burst evasion
  while failing legitimate WSL2 sleep/resume. Replaced by the per-event
  clamp (no tolerance needed — the clamp absorbs ALL skew conservatively).
- **KM-02 re-sample in `admit_claim_selection`** — anchor error landed it in
  `_operator_message_operation` instead. Corrected in the commit message;
  the equivalent re-sample inside claim selection is deferred (post-clamp
  the consequence is conservative: the racing event counts at age zero,
  lease/snapshot share one sample = mutually consistent).
- **KM-04 reverse-implication enforcement** — attempted, broke 17 pinned
  evidence-kernel fixtures (subscription seats carry implicit spend
  authority without per-route objects). Reverted; documented as an
  accepted-risk comment in `_evidence.py`.
- **Vacuous credential test** (the first credential_missing test only
  exercised pre-existing helpers) — replaced with two real gate tests that
  fail when the guarded code is deleted.
- **Modelark opencode provider** — works for `coding-plan/glm-5-3-flash`
  (file-capable GLM lane verified) but errors for `modelark/*` models
  (upstream opencode adapter bug; refs retained in the ledger). Not a
  Summon defect.
- **The telemetry spool is bounded and trims oldest events** — counts cited
  at analysis time differ from later reads. The plan carries an as-of note;
  cite the aggregate export, never re-derive from the rolling file.

## Decisions

- Coding Plan subscription route is the default for both models (owner
  directive: API/PAYG billing disabled; sub plan only).
- The platform `/api/v3` route is consent-gated by the billing guard
  (requires_spend + PAYG consent; unavailable without the owner's
  explicit authorization while API billing is disabled).
- `ZCODE_CLI` User env → `zcode-app-cli/bin/zcode.js` (the unofficial CLI's
  real entry point; `.cmd` shims are rejected by design due to
  argument-reparse hazards).
- The `modelark` opencode provider's adapter bug is upstream — documented
  with error refs, not fixed by Summon.
- KM-04 reverse-implication enforcement is an accepted risk: the evidence
  kernel does not require credit/payg/api sources for subscription seats
  (they authorize via plan); documented in the code and in the R2 review.
- model_match: None (~77%) is a governance backlog item (owner/Astra):
  exact-evidence coverage needed before telemetry governs model spend.
- ZCODE1 (unofficial zcode-cli wiring) was resolved by the ZCODE_CLI env
  override (the unofficial CLI works through the existing zcode backend).

## Acceptance criteria

- [ ] HEAD bound to a fresh `--require-clean` canonical capture (27/27
      suites, zero unexplained failures)
- [ ] `release_manifest.py --check --profile workspace-preview
      --expected-version 3.5.0 --evidence-file <capture>` passes
- [ ] U03/U07 device observations collected by a human operator
- [ ] L06 privacy scan clean on the staged candidate
- [ ] Stable profile passes (requires the live-provider receipt)
- [ ] Owner publication decision executed
- [ ] Astra commits the billing-guard branch (or delegates to the next
      agent with sign-off)

## Constraints

- Owner-reserved, never do without explicit instruction: push, PR, tag,
  GitHub release, publication, version bumps, PAYG/credit enablement,
  credential changes, auth-repair runs.
- Provider spend: subscription capacity only (Coding Plan chat completions);
  bounded smokes; never retry auth/quota failures.
- Keep separate: council advisory vs fixed-option deliberation; requested/
  targeted/served model identity; provider-inert evidence vs live
  qualification; estimate vs reported vs unknown spend.
- The `.agents\skills\summon` copy inside the third-party
  `alirezarezvani/claude-skills` clone is a 3.4.0 vendor — do not update it;
  it belongs to that repo's curation.
- `npx skills update` will replace the `.claude` and `.zcode` summon copies
  with symlinks — if you run it, restore real copies with
  `python install.py --with-alias` and recopy `.zcode`.
- The `.zcodeignore` committed at HEAD is for zcode-app-cli compatibility;
  do not delete it.
