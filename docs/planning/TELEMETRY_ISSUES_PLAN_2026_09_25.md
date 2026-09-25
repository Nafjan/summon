# Telemetry issues plan — Sep 19–25 usage window

Status: plan for the incoming development lead (Astra). Source: telemetry
analysis of 1,490 events (2026-09-19 → 2026-09-25, all on 3.5.0 — no
stale-copy usage) recorded in `LOOP_LEDGER.md` iteration 23. Companion:
`MODELARK_BILLING_GUARD_REVIEW.md` (in `.agents/handoff/`).

## P0 — Metered ModelArk platform spend (257 api-billed events)

Facts: 257 events billed `api` (peak 162 on Sep 23); 151 succeeded with
provider-`reported` served identity (real token consumption), 75 are
`unknown` (started, never terminated — spend uncertain), 29 error. The owner
states API/PAYG billing is disabled; only the ModelArk subscription is
active. Root cause: the `deepseek-41-flash` and `modelark-glm-coder` seats
resolved the Platform `/api/v3` route while describing it as
subscription-backed.

Actions:
1. Land the in-flight billing guard (`codex/modelark-billing-guard`):
   `classify_openai_compat_route` marks remote HTTP routes metered +
   `requires_spend`, with `authority.api` projection in decision evidence;
   fail-closed for remote-without-key. Review, test, commit.
2. Retire or consent-gate the two platform seats: either delete
   `deepseek-41-flash` / `modelark-glm-coder` from the operator roster or
   convert them to the Coding Plan route (`byteplus-coding` provider,
   unversioned model IDs — see the exact-model note below). Owner decision.
3. Owner: check the ModelArk console usage/billing page for Sep 19–25 to
   determine whether those calls were subscription-covered or metered.
4. Reconcile the 75 unknown api events as uncertain spend (no retroactive
   claims in either direction).

## P1 — Unknown outcomes (259 events, 17%)

All unknowns have `exit_code: None` and no terminal envelope — dispatches
killed or abandoned mid-flight (host teardown, operator abort, hangs).
Peak: 90 on Sep 23, aligned with the heaviest ModelArk usage day.

Actions:
1. Correlate the unknowns' `prompt_sha256`/`operation_id` with the jobs
   registry and host sessions to identify which seats hang or get killed
   (telemetry deliberately omits seat names; the correlation is a local,
   private step).
2. Reduce hang sources: the kimi-worker ACP flip (Sep 22) removed the
   largest known hang; re-measure the unknown rate for a week post-flip.
3. Add an interrupted-dispatch reconciliation surface (list uncertain
   operations, their billing class, and an explicit resolve/attest flow) so
   unknown spend stops being silent.

## P1 — Repeated identical-error clusters

- `report_contract` ×21 (Sep 19–21, exit 1): one seat repeatedly emitted a
  malformed report. Identify via `prompt_sha256` correlation, then fix the
  seat's report contract or verify the automatic contract-repair path.
- `timeout` ×14 (subprocess + yolo, no timeout stage recorded): same seat
  timing out repeatedly. Identify the seat, then either fix the hang or add
  stage classification (links to TIME1: several timeouts record no stage).

## P2 — missing_cli tail (23 events)

21 on Sep 19 (stale PATH hosts before the agy/kimi/cursor-agent CLIs were on
User PATH), 2 on Sep 22 (one host still needed a restart). Action: restart
any remaining long-lived hosts; no code change.

## P2 — ZCODE1: unofficial zcode-cli evaluation (this investigation)

Findings: `zcode-app-cli` 3.14.3-28 (npm, `zcode` command) bundles Desktop
runtime 0.16.9 and has a real headless surface (`-p`, `--cwd`, `--attach`,
`--disallowed-tools`, stdio `app-server`, documented host-integration
contract). Standalone headless smoke passed (`-p` → "OK") using its
configured providers — promising given the GLM failures via OpenCode and the
official Desktop runtime.

Wiring blocker (verified): Summon's `ZCODE_CLI` override accepts only a real
executable — `_target` rejects `.cmd` shims because cmd shims reparse
arguments — and the npm package ships no `.exe`. Default discovery therefore
selects the registry Desktop runtime 0.16.5 instead.

Options, in order of preference:
1. Summon-side: allow `ZCODE_CLI` to carry a node + entry-script pair (or a
   small JSON config: `{node, args, cwd}`) so JS-entry CLIs can be targeted
   without an exe shim. Add tests with a fake target.
2. OS-side: create a real `.exe` shim (e.g. a tiny compiled wrapper) and set
   `ZCODE_CLI` to it.
3. Upstream: ask zcode-app-cli to ship an exe entry point.
Then: re-run doctor (expect 0.16.9 + `app-cli` source), a live zcode-native
GLM dispatch, and the model-pin/safe-edit refusal suite (zcode still refuses
model pins and unproven safe-edit).

## Explicit non-goals

- No normalization of DeepSeek dated vs unversioned model identities — the
  exact-model gate correctly refused (`AR350-07`).
- No historical spend claims for the api-billed window (owner checks the
  console; telemetry records the exposure, not the charges).


## Review revisions (5-seat panel, 2026-09-25)

Reviewed by kimi-worker (Kimi K3, ACP), flash-reviewer + panel-skeptic (AGY
Gemini Flash 3.8), deepseek-r8-review (Coding Plan V4 Pro), and
panel-deepseek-flash-or (OpenRouter V4.1 Flash). All five returned CONCERNS
with convergent findings; the plan is revised accordingly:

1. **P0 addition (blocking, all five seats)**: the billing guard is not done
   until it reaches executing copies — review+commit must be followed by
   `python install.py` and the manual `.zcode` copy refresh. All managed
   copies currently evaluate `/api/v3` as `requires_spend: false`.
2. **P0 re-sequencing (kimi, r8, panel-or, skeptic)**: quarantine or
   consent-gate the two platform seats FIRST (stop-loss); correlation and the
   owner console check run in parallel (read-only); seat deletion comes last,
   after a dependency audit (roster references, catalog pins, saved dispatch
   configs) and only with an owner decision. Converting to `byteplus-coding`
   may trip the exact-model gate (AR350-07 dated-vs-unversioned identity) —
   test before converting.
3. **Root-cause correction (all five)**: the claim that the kimi ACP flip
   "removed the largest known hang" is contradicted by chronology — unknowns
   peaked at 90 on Sep 23, the day AFTER the flip, on the heaviest ModelArk
   day. Lead hypothesis is now: unknowns concentrate on metered-route heavy
   usage; the ACP fix addresses approval hangs only (single 34s test).
4. **P1 additions (all five)**: three dropped cohorts get triage actions —
   111 blocked events (classify by refusal gate), 49 partial events (cut-off
   and schema-failure analysis), and 67 `billing_source: unknown` events
   (classify by endpoint; a hole in exactly the evidence surface the billing
   guard depends on). Plus a dispatcher-level fix so interrupted dispatches
   terminate with a typed envelope instead of `exit_code: None`.
5. **TIME1 promoted**: 14 timeouts recorded no stage; the telemetry schema
   gap itself gets an action, not just the seats.
6. **Corrections**: missing_cli is "identified and pending", not resolved
   (2 events on Sep 22; ~3 days of post-fix data); the api accounting is
   151 success + 75 unknown + 29 error + 2 partial = 257 (reconciliation
   includes partials); a residual bypass exists while
   `SUMMON_ALLOW_BYTEPLUS_PAYG=1` or a saved operator preference is set;
   success criterion for the unknown-rate re-measure: below 5% of dispatches
   over a 7-day window.
7. **ZCODE1 demoted** to the 3.5.1 backlog (over-weighted in this doc per
   flash-reviewer); the investigation findings above stand.
