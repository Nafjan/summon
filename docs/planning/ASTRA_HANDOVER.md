# Astra handover — Summon development lead

Written 2026-09-19 by the outgoing conductor (GLM 5.3 Flash session). Astra
(GPT-6 Astra, exact-pinned `codex/gpt-6-astra`) takes over development,
review, and release stewardship. Everything below is source-backed; the
private run ledger at `I:\Antigravity projects\summon-evidence-350\LOOP_LEDGER.md`
records 19 iterations of dispositions and evidence.

## 1. Current state

- Branch `codex/gemini-flash-3.8`, HEAD `822bc63`, clean tree. Product version
  **3.5.0** (converged: plugin.json, dispatcher `__version__`, telemetry,
  mcp_server, migration-doc marker; `tools/release_contract.py` ready=True).
- Three commits tell the story:
  - `fb41dae` — **release: Summon 3.5.0 workspace preview** (352 files). The
    3.5.0 release candidate. A clean-source canonical capture is BOUND TO THIS
    COMMIT: 27/27 fixed suites, 4,920 cases + 164 subtests passed, 0 failed,
    exactly 3 reviewed-policy skips, suites+gates aggregate 5,488 passed, 7/8
    gates pass with `live_provider=blocked` (intentional), Windows platform
    qualified, 44/44 rendered artifacts. `release_manifest.py --check
    --profile workspace-preview --expected-version 3.5.0` **PASSES** against
    it (exit 0); `stable` refuses only on `live_provider`.
  - `443635e` — ModelArk platform routes (catalog entries for
    `glm-5-3-flash-260828` + `deepseek-v4-1-flash-260910`, arkcli
    namespace-compatibility fix + routing test, backends.md subscription
    note). Focused suites green (catalog/routing 22, inventory/discovery
    124); **no full clean capture exists for this commit or HEAD**.
  - `822bc63` — docs correction (platform-vs-coding-plan route facts).
- NOT done (owner-reserved): push, PR, tag, GitHub release, publication
  decision. **Summon 3.5.0 is committed and machine-accepted locally; it has
  not been published.**

## 2. Release gates and what remains

| Gate | State |
| --- | --- |
| workspace-preview manifest | **PASS** at fb41dae (evidence: `summon-evidence-350\release_evidence_350_clean.json`, `manifest_350_workspace_preview.json`) |
| stable profile | Refuses ONLY `live_provider=blocked` — needs an authoritative provider receipt |
| U03/U07 device acceptance | NOT RUN — human operator with screen reader + native mobile keyboard; fixtures prepared (`U03_U07_DEVICE_ACCEPTANCE.md`) |
| L06 final privacy scan | Pre-staging scan clean; must be rerun on the staged/committed publication candidate |
| L09 final retention / L12 | Require the clean committed candidate (exists) + owner publication decision |
| HEAD full capture | **OPEN: HEAD `822bc63` is not full-capture-bound** (two post-release commits came after the last full capture; focused partitions green). Run a fresh `--require-clean` capture when you want HEAD-bound evidence. |

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
   dated subscription note. Live smokes: both models dispatch with
   provider-`reported` served evidence.
3. **Known defect found and worked around, fix still OPEN**: install.py's
   convergence gate hashes `scripts/` only — a **docs-only commit can skip
   propagation into installed copies** (observed: backends.md/catalog
   corrections did not reach any managed copy until a forced reinstall).
   Structural fix candidate: extend the convergence/inventory gate to include
   `references/` (and test it). This is backlog item DOC1.
4. **Machine-local wiring** (not in the repo; documented in
   `C:\Users\nside\.agents\memory.md` which auto-injects into every dispatch):
   `modelark` provider in `~/.agents/providers.json` (`MODELARK_API_KEY`,
   User scope); operator seats `deepseek-41-flash`, `modelark-glm-coder`
   (openai-compat, platform `/api/v3`) and `glm-coder` (opencode
   `coding-plan/glm-5-3-flash`, **file-capable**, live-verified reading
   files); cursor-agent shim at `C:\Users\nside\.local\bin\cursor-agent.cmd`
   → `AppData\Local\cursor-agent\agent.cmd`.
5. **Provider facts you must not un-learn**: DeepSeek V4.1 Flash is NOT a
   Coding-Plan model (`/api/coding/v3` refuses it in all ID forms; request
   IDs retained in the ledger) — its only route is the Platform `/api/v3`
   under the subscription activation. API/PAYG billing is disabled on this
   account. OpenCode's `modelark` platform-provider adapter errors upstream
   (refs `err_5ccf986a`, `err_9288ba06`, `err_dbb35055`, `err_b84cce21`) —
   the coding-plan provider works. GLM's Coding-Plan route via arkcli 1.0.32
   requires SSO (`arkcli auth login`; done 2026-09-19).

## 4. 3.5.1 backlog (priority order)

- **DOC1**: install.py convergence gate must cover `references/` (docs-only
  releases silently skip installed copies today). Add test.
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
gates/manifest/doctor; dispatch subscription seats for bounded smokes and
reviews (records: `MODELARK_API_KEY` User-scope env; arkcli SSO done).
You must NOT (owner-reserved): push, PR, tag, publish, bump versions again,
enable PAYG/credit, repair credentials, or claim 3.5.0 is published. U03/U07
device observations require the human operator. Exact-model and
served-evidence gates stay fail-closed: catalog presence is not availability;
`inferred` is not `reported`.

## 6. Verification quickstart

```
git log --oneline -3 && git status --porcelain          # 822bc63, clean
python tools/release_contract.py                        # canonical 3.5.0, ready
python -m pytest -q tests/test_release_contract.py tests/test_migration_gate.py tests/test_release_manifest.py -p no:cacheprovider
python skills/summon/scripts/run_subagent.py doctor --json   # 9 usable backends (with current User PATH)
python tools/release_gates.py --require-clean --output <outside-tree>.json --chromium-executable <path>   # fresh HEAD-bound capture (~50 min)
python tools/release_manifest.py --check --profile workspace-preview --expected-version 3.5.0 --evidence-file <that json>
```

Machine notes (PATH, OpenCode cwd restriction, runs-root ACL contract,
zcode-copy manual refresh) are in `C:\Users\nside\.agents\memory.md` and the
ledger. Telemetry: `~\.agents\summon-telemetry.jsonl` (enabled).
