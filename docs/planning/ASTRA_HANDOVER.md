# Astra handover — Summon development lead

Written 2026-09-19 by the outgoing conductor. Astra takes over development,
review, and release stewardship. Everything below is source-backed where
identified; private run ledgers and provider receipts remain outside public
source and are not reproduced here.

## 1. Current state

- The outgoing record named `822bc63`; subsequent documentation commits are
  now present and the current tree must be revalidated before any release
  claim. Product version **3.5.0** is converged across the contract sources
  (`tools/release_contract.py` reports ready), committed locally, and not
  published.
- Three commits tell the story:
  - `fb41dae` — **release: Summon 3.5.0 workspace preview** (352 files). The
    3.5.0 candidate. A clean-source canonical capture is bound to this
    commit: 27/27 fixed suites, 4,920 cases + 164 subtests passed, 0 failed,
    exactly 3 reviewed-policy skips, suites+gates aggregate 5,488 passed, 7/8
    gates pass with `live_provider=blocked` (intentional), Windows platform
    qualified, 44/44 rendered artifacts. `release_manifest.py --check
    --profile workspace-preview --expected-version 3.5.0` passed against that
    historical commit; `stable` refused on the intentionally missing
    live-provider evidence.
  - `443635e` — ModelArk platform routes (catalog entries for
    `glm-5-3-flash-260828` + `deepseek-v4-1-flash-260910`, arkcli
    namespace-compatibility fix + routing test, backends.md subscription
    note). Focused catalog/routing and inventory/discovery suites were green;
    **no full clean capture exists for this commit or the current HEAD**.
  - `822bc63` — docs correction (platform-vs-coding-plan route facts).
- NOT done (owner-reserved): push, PR, tag, GitHub release, publication
  decision. **Summon 3.5.0 is committed locally and contract-checked; it has
  not been published, and no current clean-candidate acceptance is implied.**
  The historical capture does not qualify the current tree; its retained
  history and any publication decision remain owner-gated.

## 2. Release gates and what remains

| Gate | State |
| --- | --- |
| workspace-preview manifest | **PASS** for the historical fb41dae candidate; retained evidence is outside public source |
| stable profile | Historical fb41dae evidence refused only on `live_provider=blocked`; the current HEAD has no qualifying full packet and still needs an authoritative provider receipt |
| U03/U07 device acceptance | NOT RUN — human operator with screen reader + native mobile keyboard; fixtures prepared (`U03_U07_DEVICE_ACCEPTANCE.md`) |
| L06 final privacy scan | Historical pre-staging scan was clean; this audit found public-document leaks that require correction, followed by a staged-candidate scan |
| L09 final retention / L12 | Historical disposition remains owner-gated; re-run clean-candidate retention and the owner publication decision for the current tree |
| HEAD full capture | **OPEN:** the current tree is not full-capture-bound after post-capture commits; focused partitions do not qualify it. Run a fresh `--require-clean` capture for HEAD-bound evidence. |

## 3. Review targets — the outgoing conductor's work to adversarially verify

1. **Release execution**: version bump 3.4.0→3.5.0 (plugin.json, dispatcher,
   telemetry, mcp_server, unique migration marker,
   `tests/test_release_contract.py` pin + fixtures). Deliberately NOT bumped:
   `summon-executor/3.4.0` identity-scope strings (`_launch_qualification.py`,
   `_resume_capabilities.py`) and the L04 rollback fixture pins
   (`tests/test_l04_composed_rollback.py`). CHANGELOG dated
   `[3.5.0] - 2026-09-18`; three bullets added after the fact (taskless
   supervisor inbox, council between-round checkpoint, Windows silent-launch)
   — each verified against feature sources by two independent reviewers.
2. **ModelArk integration** (commit `443635e`): catalog entries
   (`openai-compat/glm-5-3-flash-260828`, `openai-compat/deepseek-v4-1-flash-260910`;
   near-frontier membership pin extended deliberately in
   `test_model_catalog.py`); `_builder.py` routing preflight now treats
   `arkcli` as compatible with deepseek/zhipu namespaces (pinned by
   `test_modelark_subscription_models_are_arkcli_compatible`); backends.md
   dated route note. Live availability and served identity remain separately
   qualified.
3. **Known defect found and worked around, fixed in this review slice**: the
   installer copies the complete skill payload, but doctor and the post-install
   drift check previously hashed `scripts/` only. A docs-only change could
   therefore leave generic convergence green while installed references stayed
   stale. DOC1 adds a bounded payload fingerprint for `SKILL.md`, `scripts/`,
   `references/`, `agents/`, and `examples/`, with fail-closed unknown states.
4. **Machine-local wiring and provider receipts** are deliberately excluded
   from public planning. Account entitlements, credentials, subscription
   activation, adapter incidents, and request identifiers are private facts;
   public route documentation must state endpoint/product distinctions without
   claiming availability for a particular account.

## 4. 3.5.1 backlog (priority order)

- **DOC1 (complete in the current local candidate)**: doctor and post-install
  convergence cover the complete staged skill payload, including `references/`,
  while preserving the scripts receipt hash. Bounded unknown-state and docs-only
  refresh regressions are registered in the fixed release suite.
- **SKILL1**: SKILL.md is 132 KB (~40-44k tokens) loaded per invocation.
  Progressive-disclosure restructure: keep dispatch happy path + fail-closed
  safety rules + status handling inline; move the Parameters table (~37%) and
  response-fields dictionary (~13%) to `references/`. Target core ≤ ~45 KB.
  `test_discovery.py` pins exact SKILL.md strings — preserve or deliberately
  update them.
- **FS-01..FS-04** (rseen-reviewer, INFO/NIT): unify two disposition adapters
  on the coherent-snapshot helper (`_workspace_commands.py:394,526`); document
  that summary `unknown_spend` covers total-token coverage only
  (`_submission_accounting.py:257`); dead conditional in
  `_transport_budget.py:96-102`; consider production dispatch modules in the
  silent-launch AST inventory.
- **README1**: flip "Current release: 3.4.0" + the `--expected-version`
  example at actual publication (RV-04).
- **DOC2**: README `--runs-root` wording — the directory must not pre-exist
  (create secures it) or must already be owner-only; Git Bash `mktemp -d`
  dirs fail closed by design.
- **TIME1**: document that `--adaptive-timeout` applies to background jobs;
  foreground dispatches keep the fixed budget (observed).
- **OPENCODE1 / DEEPSEEK1**: upstream bug-report (modelark adapter) and
  BytePlus support request (DeepSeek V4.1 Flash coding-plan enablement), both
  owner-optional.

## 5. Authority boundaries (unchanged)

You may: implement, test, review, document, and **commit locally**; run
provider-inert gates, manifest checks, and doctor. Any provider/account,
credential, or subscription evidence requires a separately retained owner
receipt and must not be copied into public planning.
You must NOT (owner-reserved): push, PR, tag, publish, bump versions again,
enable PAYG/credit, repair credentials, or claim 3.5.0 is published. U03/U07
device observations require the human operator. Exact-model and
served-evidence gates stay fail-closed: catalog presence is not availability;
`inferred` is not `reported`.

## 6. Verification quickstart

```
git log --oneline -3 && git status --porcelain          # inspect current tree
python tools/release_contract.py                        # canonical 3.5.0, ready
python -m pytest -q tests/test_release_contract.py tests/test_migration_gate.py tests/test_release_manifest.py -p no:cacheprovider
python skills/summon/scripts/run_subagent.py doctor --json   # bounded local inventory; counts and auth/availability are environment facts
python tools/release_gates.py --require-clean --output <outside-tree>.json --chromium-executable <path>   # fresh HEAD-bound capture (~50 min)
python tools/release_manifest.py --check --profile workspace-preview --expected-version 3.5.0 --evidence-file <that json>
```

Machine-local notes, private receipts, credentials, and telemetry locations are
outside this public handover. Keep release evidence outside the source tree.
