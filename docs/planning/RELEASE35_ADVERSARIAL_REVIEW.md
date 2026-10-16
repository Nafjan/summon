# 3.5.0 adversarial review checkpoint

Updated 2026-09-19. This is a public, source-backed review record for the
locally committed 3.5.0 candidate. It contains no private run receipts,
machine paths, account state, or provider response data. The candidate has not
been published.

| ID | Severity | Finding | Evidence | Disposition |
| --- | --- | --- | --- | --- |
| AR350-01 | P1 | Public planning and backend documentation must not carry private machine, account, or receipt details; historical release history also needs an explicit publication hold. | `docs/planning/ASTRA_HANDOVER.md:3-6,40-45`; `skills/summon/references/backends.md:442-448`; registered privacy checks in `skills/summon/scripts/test_install_drift.py:18-51,215-241` | Current tracked-document scan is bounded and reports only relative file/rule names. Corrections are implemented in this review slice. Historical commits are retained for provenance and remain blocked from publication until the owner approves a clean staged scan. |
| AR350-02 | P1 | The strongest full capture is historical and does not qualify the later route/document commits or current HEAD. | `docs/planning/ASTRA_HANDOVER.md:15-34`; `docs/planning/RELEASE35_COVERAGE_MATRIX.md:13-16`; `docs/planning/RELEASE35_IMPLEMENTATION_HANDOFF.md:29-36` | Retain the historical packet as dated diagnostic evidence. A fresh clean HEAD-bound capture remains open. |
| AR350-03 | P2 | Catalog membership and a selected ModelArk route do not prove account entitlement, served identity, or tool/file capability. | `docs/planning/ASTRA_HANDOVER.md:58-65`; `skills/summon/references/backends.md:434-448` | Public text now separates catalog facts, route facts, adapter qualification, and current authorized receipts. |
| AR350-04 | P2 | Release notes and handoff text must identify 3.5.0 as locally committed and unpublished, and must qualify historical QA claims. | `CHANGELOG.md:6-20`; `docs/planning/EXPECTED_NEXT_RELEASE.md:1-20`; `docs/planning/RELEASE35_IMPLEMENTATION_HANDOFF.md:1-36` | Current checkpoint wording is corrected; device, privacy, clean-candidate, live-provider, and publication gates remain open where documented. |
| DOC1 | P2 | Scripts-only drift reporting could report convergence while installed documentation or references differed from the running source. | `skills/summon/scripts/_installs.py:92-177,533-676`; `install.py` `_drift_check`; `skills/summon/scripts/_doctor.py:759-854` | Implemented bounded payload fingerprints for the staged skill roots, preserved `scripts_sha256`, failed closed for mixed legacy records and unknown payloads, and kept internal managed-copy parity separate from source convergence. |
| AR350-05 | Informational | The product version bump is complete without rewriting compatibility history. | `plugin.json:4`; `skills/summon/scripts/run_subagent.py:104`; `skills/summon/scripts/_telemetry.py:43`; `skills/summon/scripts/mcp_server.py:18`; `docs/PHASE1_MIGRATION_ROLLBACK.md:3`; retained adapter scopes in `skills/summon/scripts/_resume_capabilities.py:32` and `skills/summon/scripts/_launch_qualification.py:59` | Canonical product surfaces are 3.5.0. The two `summon-executor/3.4.0` strings remain deliberately pinned as the versioned v2 adapter compatibility scope; historical rollback data and test fixtures also retain their original versions. |

## DOC1 behavior contract

The operational record retains the existing scripts digest and adds an
additive payload digest covering `SKILL.md`, `scripts`, `references`, `agents`,
and `examples`. The payload scan reads regular files only, normalizes text
line endings as the release manifest does, and rejects links, unreadable or
oversized files, and entry/depth/count limits as unknown. Any modern record
engages payload tracking; a mixed modern/legacy set cannot become green by
falling back to scripts-only data.

`managed_converged` continues to mean internal parity among installer-managed
copies for compatibility. `managed_source_converged` and generic
`converged` additionally require agreement with the running source and the
payload digest. Doctor output identifies managed payload drift as an install
action and unmanaged drift as owner-managed, without displaying foreign
payload names or private paths.

## Verification for this review slice

- The release contract reports canonical version `3.5.0`, converged and ready.
- The focused release contract, migration, and manifest partition passes 38/38.
- DOC1 scope tests pass 11/11.
- Installer and discovery regressions pass 607 tests with one explicit
  platform-specific skip.
- Provider-inert doctor discovery completes with payload tracking enabled. It
  reports the edited source and installed copies as non-converged, as expected;
  no installation refresh was performed.

These focused results do not replace a clean, current-HEAD full release capture.

## Post-release ModelArk route and identity triage

The installed-seat review found two source risks that are now tracked as
separate findings. They concern roster and decision evidence only; they do not
establish historical provider contact or billing.

| ID | Severity | Finding | Evidence | Disposition |
| --- | --- | --- | --- | --- |
| AR350-06 | P1 | A stale handoff described a Platform `/api/v3` ModelArk route as a subscription lane, which could reintroduce misleading onboarding prose during install or seat regeneration. | `docs/planning/SUMMON_DEV_HANDOFF.md:76-84,103-116`; `skills/summon/references/backends.md:333-381`; `skills/summon/agents/byteplus-coder.md:1-18` | Handoff wording now separates Platform API billing from Coding Plan quota and names only `byteplus-coder` as the shipped Coding Plan seat. The shared provider registry remains unchanged. The provider-inert decision projection records metered remote API routes as requiring spend. ModelArk Platform `/api/v3` needs both explicit operator PAYG consent and a declared key boundary; generic API routes retain explicit endpoint/key authority. Subscription and loopback routes remain non-metered. |
| AR350-07 | P1 | A dated requested model can receive an unversioned or different served identity. Treating that response as an alias would overstate exact-model evidence. | `skills/summon/references/model-catalog.json:341-360`; `skills/summon/scripts/test_model_routing.py:126-170`; `skills/summon/scripts/_executor.py` exact-model verification path | Exact-model verification remains fail-closed. The unversioned response is recorded as an identity qualification issue; no alias normalization, reroute, or second provider contact is authorized by this review. |

The new API spend projection is advisory and provider-inert. It does not
authorize a PAYG fallback, query usage, read a credential, or contact a
provider. ModelArk Platform `/api/v3` cannot be authorized by endpoint/key
declaration alone; it also requires explicit operator PAYG consent. A remote
route without a declared credential boundary remains ineligible in the
decision receipt, preserving fail-closed behavior for uncertain spend.
