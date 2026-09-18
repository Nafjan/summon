# Summon workspace planning

STATUS: DONE

VERDICT: CONDITIONAL — adopt v3 as the planning baseline; implementation, owner decisions and release qualification remain outstanding. Three review rounds are complete. No implementation item or release gate has been marked complete.

- [Checklist v3](WORKSPACE_CHECKLIST_V3.md) — 125 tracked requirements, phase dependencies, delivery recovery rules and an explicit preview release cut.
- [Review disposition](WORKSPACE_REVIEW_LOG.md) — accepted, rejected, merged and limited findings, with independent source checks where applicable.
- [Implementation handoff](WORKSPACE_IMPLEMENTATION_HANDOFF.md) — proposed change sequence, five acceptance journeys, phase signals/gates, deferrals, owner decisions and risks.
- [Project lead status](PROJECT_LEAD_STATUS.md) — verified progress, corrections to developer handoffs and the next bounded task.
- [Expected next release](EXPECTED_NEXT_RELEASE.md) — provisional 3.5.0 preview changelog versus released 3.4.0, with readiness, exclusions and release gates.
- [Conductor-loop direction](CONDUCTOR_LOOP_DIRECTION.md) — goal focus, bounded delegation, evidence integration and explicit stopping conditions.
- [First workspace slice](P1_VERTICAL_SLICE_BRIEF.md) — addressed-message fixture and a repeated conductor-loop demo over existing coordinator tasks.
- [Task-first shell brief](P1_TASK_SHELL_BRIEF.md) — next UI increment, typed authority, secret-safe bootstrap, task/context journeys and required rendered qualification.
- [Fixed-fixture effect resolution](P1_EFFECT_RESOLUTION_CONTRACT.md) — explicit scoped launch/cleanup observations, reserved resolution and limits on simulated evidence.
- [Worker-origin ingress contract](P1_WORKER_INGRESS_CONTRACT.md) — approved next implementation: authenticated sender, atomic durable send, explicit recipient permission and required supervisor endpoint follow-on.
- [R13 implementation brief](R13_IMPLEMENTATION_BRIEF.md) — exact serialization and settlement reserve prerequisite; numerical bounds still require derivation.
- [R02 skeptical review and closure](FABLE_R02_STAGE_A_REVIEW.md) — verified third-turn failure, correction and independent closure evidence.
- [Workspace direction review](FABLE_WORKSPACE_DIRECTION_REVIEW.md) — accepted R02 milestone, P1 conductor-demo conditions and the R13 durability seam.
- [W06 delegated handoff](W06_DISCOVERY_HANDOFF.md) — bounded test-launch corrections and remaining external uncertainty.
- [Leadership authority](LEADERSHIP_AUTHORITY.md) — current owner delegation, Fable approval role and active work ledger; supersedes historical per-slice authorization blockers.
- [Fable P0 approval](FABLE_P0_APPROVAL.md) — verified reviewer identity, corrected public-emitter contract and nine binding implementation conditions.
- [P0 implementation review](P0_IMPLEMENTATION_REVIEW.md) — lead findings, candidate corrections and independent verification status.
- [Windows silent-launch plan](WINDOWS_SILENT_LAUNCH_PLAN.md) — owner-reported popup recurrence, verified launch gaps and proposed regression coverage.
- [Windows silent-launch caller guidance](WINDOWS_SILENT_LAUNCH_CALLERS.md) — provider-inert Python, PowerShell/.NET, batch, and MCP/IDE launch recipes with explicit ownership limits.
- [Fable Windows approval](FABLE_WINDOWS_APPROVAL.md) — approved implementation boundary and Windows verification conditions.
- [Windows implementation review](WINDOWS_IMPLEMENTATION_REVIEW.md) — six corrected findings, candidate closure evidence, and the remaining independent milestone gate.
- [Windows implementation review](WINDOWS_IMPLEMENTATION_REVIEW.md) — lead findings and closure requirements before independent acceptance.
- [R01 first-change decision](R01_CHANGE_DECISION.md) — explicitly authorized contract-and-fixture scope.
- [R02 proposal review](R02_PROPOSAL_REVIEW.md) — source-backed corrections and staged qualification decision.
- [R02 refusal contract](R02_REFUSAL_CONTRACT.md) — proposed first behavioral migration for Fable approval, including CLI/browser outcomes and explicit fork.
- [Fable R02 approval](FABLE_R02_APPROVAL.md) — approved Stage A implementation boundary, retained refusal lineage and six acceptance conditions.
- [R02 preview migration notice](R02_PREVIEW_MIGRATION_NOTICE.md) — user-facing behavior change for refused continuation and explicit fork.
- [R02 implementation review](R02_IMPLEMENTATION_REVIEW.md) — active lead findings and independent validation sequence.
- [R01/R02/R12 canonical retention](R01_R02_R12_CANONICAL_RETENTION.md) — exact queued-version, candidate-fork and operator-body acceptance files plus CI/release-manifest retention rules.
- [R13 capacity findings](R13_CAPACITY_FINDINGS.md) — reproduced missing control reserve and Windows byte-boundary undercount; no runtime changes yet.
- [R01 contract acceptance](R01_CONTRACT_ACCEPTANCE.md) — original conditional acceptance and historical regression failures; the later P0 review records the corrected 189-test pass. No runtime activation or release claim.
- [P0 reliability next change](P0_RELIABILITY_NEXT_CHANGE.md) — historical measured contention and fixture proposal, subsequently authorized, implemented and independently verified.
- [P0 reliability contract](P0_RELIABILITY_CONTRACT.md) — provider-inert liveness, process-ownership, resume-contention and structural-refusal contract for DEV-P0-01.
- [Readiness corrections](READINESS_CORRECTIONS.md) — source-backed public-claim corrections queued for R16/L08.
- [Checklist v1](WORKSPACE_CHECKLIST_V1.md) and [checklist v2](WORKSPACE_CHECKLIST_V2.md) — earlier planning baselines retained for comparison.

## Recommendation

Choose one user-facing workspace over the existing runtime, with council and deliberation retained as explicit typed modes. The next-minor proposal is a narrow P0/P1 preview with a minimal task-first UI. Long-horizon supervision, sixteen-worker qualification and native-session attachment earn separate evidence. An external CLI does not automatically gain access to private native sessions.

## Checklist map

| IDs | Scope | Items |
| --- | --- | --- |
| R01-R16 | Reliability, provenance, capacity and capability migration | 16 |
| F01-F18 | Durable task/message fabric and authenticated workers | 18 |
| H01-H14 | Long-horizon supervision and recovery | 14 |
| S01-S09 | Dependency swarm and scale | 9 |
| X01-X16 | Existing-session and native adapter investigation | 16 |
| U01-U14 | Onboarding, atlas redesign and accessibility | 14 |
| E01-E12 | Usage, context economics and model roster | 12 |
| G01-G08 | Council/deliberation authority preservation | 8 |
| L01-L12 | Privacy, migration and release qualification | 12 |
| D01-D06 | Owner decisions | 6 |

Each item remains unchecked. Requirements may need several small implementation changes; these are not 125 promised commits or an effort estimate.

## Review evidence

Seven requested advisory seats: four Gemini Flash 3.8 personas (product, interoperability, reliability, UX), Kimi, GLM and Opus. Each round receives the full revised checklist plus prior HANDOFF/dispositions. Four personas are one model family, not four independent votes.

| Round | Input | Usable reports | Limitation |
| --- | --- | --- | --- |
| 1 | v1 | 6 of 7 | Kimi timed out during inspection without a completed report |
| 2 | v2 | 6 of 7 | GLM name resolution failed without a usable report |
| 3 | v3 | 7 of 7 | All seven reported no remaining material checklist findings |

Flash identity evidence is inferred. Kimi's usable second- and third-round reports lack served-model evidence. GLM and Opus successful reports carry reported model evidence. Declared model targets and report validity do not themselves establish exact-model certification. Failure/unknown usage is retained; no automatic retry, route substitution or account switch was used.

Raw reports and execution receipts are private and are not included here. The public ledger contains lead-authored assessments, not pasted provider output. Review approval concerns checklist completeness; future source tests, live qualification and release-owner decisions remain mandatory.

Planning converged after three rounds: 19 usable reports from 21 bounded review calls. All seven seats approved checklist completeness in the closing round; this is advisory agreement, not independent-model quorum or exact-model certification. No fourth provider round is justified by the recorded findings.

Initial planning validation: all 125 IDs are unique and preserved across versions; repository-relative source references resolve; planning documents passed whitespace and privacy-pattern checks. Original source and pending roster files remained unchanged during that documentation-only phase. The later authorized contract implementation and its provider-inert regression results are recorded separately in R01_CONTRACT_ACCEPTANCE.md; future behavioral gates remain unchecked.
