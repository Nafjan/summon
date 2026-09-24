# Task navigation and delivery presentation acceptance

Architect acceptance, 2026-09-12: U01, U02 and U04 are verified for the
provider-inert 3.5 workspace preview. U03, U07, U08 and final candidate gates
remain separate and blocking; this is not release approval.

## Source and independent evidence

The integrated `_workspace_details.py`, `_workspace_page.py`,
`test_workspace_canonical_rendered.py` and
`test_workspace_delivery_rendered.py` under `skills/summon/scripts/` exactly
match the independently accepted source. Both new rendered test files are
retained in `tools/release_manifest.py`'s `workspace_ui` command and the
workspace UI command in `.github/workflows/ci.yml`.

- U01: ordinary authenticated host navigation renders separately typed
  workspace assessments, workspace decisions, advisory council reviews and
  fixed-option deliberation outcomes, including pending, decided, unresolved
  and cancelled states. The local facade authenticates scoped reads and body
  operations; `_workspace_ui.py` enforces exact loopback Host/Origin and
  separate session authentication. Detail type/task/run/workspace/key and body
  scope checks occur before source readers. The accepted actual-host negative
  cases include wrong task/type and revoked scope. No capability-bearing links
  or generic token-forwarding proxy are introduced. Legacy entry points remain.
- U02: a selected task retains the focal objective, work, messages, controls,
  evidence and typed decisions. Canonical task presentation identity now agrees
  between the view, details and lookup; opaque detail-item IDs remain a separate
  namespace. The real-browser case opens an explicitly scoped artifact body,
  refuses metadata-only body access, returns focus on Escape and ignores a late
  body response after closing. Selecting another task removes the former task's
  detail targets. The two actual-host identity regressions fail before the
  correction and pass after it.
- U04: real-browser cases cover all twelve normative worker-delivery states.
  Validated synthetic runtime producers preserve separate contact, spend,
  cleanup, acknowledgement and selected-turn facts. Five terminal/held states
  show finite reasons and scoped action guidance; seven other states cover
  accepted through acknowledged plus proven not-submitted. No-contact evidence
  does not imply automatic retry. Bound journal times are labelled
  `Journal recorded at`; absent model evidence remains unavailable. Neither
  completion nor acknowledgement fabricates per-message token cost.

The lead independently ran the canonical focal/artifact case in 12.486 seconds,
the terminal/expiry/reconciliation case in 59.623 seconds, and the complementary
seven-state case in 43.884 seconds. All passed with zero skips or guard
violations, unchanged owned source and an empty synthetic home. Earlier failed
fixtures and the reproduced focus defect remain recorded in
`REMAINING_UI_ACCEPTANCE.md`; these are separate passing runs, not one combined
command. Earlier independently accepted ten detail cases, eight canonical
consistency cases and the scoped page-script regressions remain applicable.

Actual toolchain: Chromium 149.0.7827.55, Playwright 1.52.0, Python 3.13.11,
Windows 11, headless 1100 by 800. Retained private synthetic screenshots include
the focal artifact, changed-task view, terminal guidance, authentication expiry
and seven individual delivery cards. Artifact/task views and acknowledged/
not-submitted cards were visually inspected. No private artifact paths, source
hashes, receipt bodies, account data or telemetry are published here.

The integration worker additionally reports 35 detail/page cases and a combined
three-case rendered run passing without skips, plus 23 registry checks. Lead
closure rests on independently executed cases and exact current retention,
not the worker's report alone.

## Limits retained

Synthetic validated submission observations are presentation evidence, not
physical worker receipts or provider qualification. Separate supervisor-inbox
modes do not change the normative twelve-state worker contract. No provider,
account or installation was exercised. The pending-action case covers session
expiry and same-operation reconciliation, not actual owner takeover. Actual
assistive-technology announcements, native mobile keyboards, remaining reflow
and recovery checks, full candidate privacy and platform gates remain open.
