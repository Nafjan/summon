# Public readiness correction queue

Reviewed: 2026-09-05. Scope: focused current-claim review for R16/L08 while R01 implementation authorization is pending. This records proposed corrections; public product docs, changelogs, runtime source and pending roster changes were not edited. It is not a new release, live-provider certification, complete privacy audit or revalidation of historical private receipts.

Current reconciliation, 2026-09-12: RD01 and RD02 are independently accepted
after the roadmap's historical preview/GA distinction and release-scoped counts
were corrected. The public model/usage/privacy preflight corrections are also
accepted at the bounded scope in R16_L08_DOCUMENTATION_ACCEPTANCE.md. The
original findings and reviewed line numbers below remain historical evidence;
do not treat their unchanged wording as an instruction to repeat completed work.
Final candidate privacy, actual provider qualification and release-owner gates
remain separate.

## Verified findings

| ID | Classification | Evidence | Proposed correction and acceptance |
| --- | --- | --- | --- |
| RD01 | Confirmed documentation contradiction | `docs/SUMMON_PRODUCT_ROADMAP.md:12` calls v3.0.0-ga the historical provider-inert preview; `CHANGELOG.md:517` and `docs/ENGINEERING_CHANGELOG.md:599` describe that tag as historical GA certification and distinguish v3.0.0 as the preview | Reconcile the current roadmap's history against the release records. Preserve immutable tags and historical evidence; do not invent a new certification or rewrite old receipts. Acceptance: version/history labels agree across the three documents, with any unresolved historical fact explicitly marked. |
| RD02 | Confirmed stale current-evidence wording | Roadmap lines 13-16 and 155-187 reuse a 17-suite certification and old numeric counts as current/closed evidence. `tools/release_manifest.py:60` currently names 19 required suite groups and eight gates; `tools/release_gates.py` derives its command registry from that source | Put historical counts under an explicit release/version heading. Derive current required group names from the canonical registry; report current execution counts only from fresh source-bound evidence. Acceptance: no old pass count, warning-clean result or installation convergence is presented as a pass for the pending tree. This review counted registry groups, not passing tests. |
| RD03 | Confirmed release-posture inconsistency | Roadmap lines 5-6 call chat public preview, while its browser row at line 48 calls it unreleased post-GA preview. README lines 78-84 exposes the current room workflow | Label shipped public-preview code separately from unfinished lifecycle/UI qualifications. Acceptance: one consistent current posture, with the remaining gates named rather than inferred from an old milestone title. |
| RD04 | Confirmed glossary/usage inconsistency | Roadmap lines 124-129 define public preview through local/provider-inert use and live-provider-gated as closed. Its own room section permits explicit bounded live turns; line 44 calls Claude live-provider-gated while line 47 says its bounded historical gate passes | Separate implementation/release maturity, provider-contact behavior and operation-specific qualification. A preview may have an explicitly authorized live seam. Distinguish an archived qualified matrix from a currently closed expansion gate. Acceptance: glossary and readiness rows cannot imply either zero contact for all preview operations or umbrella live approval. |
| RD05 | Inaccurate message/turn wording | `docs/SUMMON_CONVERSATION_PLAN.md:46` calls an agent message a turn. Its command section correctly describes message append/inbox reads. `skills/summon/scripts/_conversation_runtime.py:_context_prompt` handles human/message-posted context and returns only the new prompt for resumed turns; it omits addressed agent_message events | State explicitly that addressed messages currently provide durable append/read APIs, not automatic provider-context delivery or a provider turn. Keep F11/F18 as implementation/verification work. Acceptance: examples distinguish stored, selected, submitted and acknowledged boundaries without implying inbox consumption exists today. |
| RD06 | Ambiguous continuation and cost claim | README line 704 describes a session handle as a cheap follow-up; roadmap lines 205-210 and the conversation plan list identity compatibility without explaining the separate governed capability gate. `_resume_capabilities.py` certifies only Claude subprocess; chat has a wider preview allowlist | Document raw backend continuation separately from authenticated governed jobs continuation. A handle is neither authority nor a cost guarantee. Link the source-derived R02 matrix and preserve explicit compatibility/permission checks. Acceptance: no generic handle implies governed resume for Codex/Cursor/AGY or promises lower spend. |
| RD07 | Current roster guidance awaiting pending-slice reconciliation | Roadmap lines 173-180 still prescribe the Flash 3.7 researcher pin, while the existing pending source/roster slice moves to Flash 3.8 and adds Astra. These are user-owned uncommitted changes | Reconcile the editorial guidance as part of the separately reviewed roster slice, with explicit unreleased versus published labels. Do not describe pending Astra/Flash definitions as a new release or relabel historical receipts. Acceptance: current-working-tree guidance and released-version history are distinct and internally consistent. |
| RD08 | Qualification scope needs explicit limits | Roadmap line 48 broadly lists accessibility/visual evidence; lines 79-82 demand rendered acceptance. `tools/release_manifest.py:105` maps the accessibility gate to UI unittest suites; inspected assertions check generated source/HTML and HTTP behavior | Describe exactly what existing gates exercise and cite any separate version-bound rendered evidence before claiming it. Keep L09/U07 rendered qualification as a future gate; do not discard useful HTTP/security tests. Acceptance: source assertions, HTTP behavior and actual rendered geometry/focus/contrast evidence are separately labelled. No conclusion about absent historical private visual evidence is made here. |

Line references identify the reviewed tree and must be refreshed if upstream documents change.

## Findings deliberately not promoted into defects

- `docs/SUMMON_SWARM_PROTOCOL.md` already describes batch fan-out plus a local durable coordinator and explicitly denies native IDE attachment. This reviewed wording does not claim dependency-aware scheduling; retain it and add graph capabilities only with S01/S02 evidence.
- README's explicit usage-read consent and queued-steering command description already preserve their authority boundaries. Do not weaken them during the workspace rewrite.
- The current deliberation plan explicitly gates approval and resume while describing a narrow fresh read-only lane. Historical provider-inert milestones are not automatically stale claims when their temporal scope is clear.
- Existing documented corrective retry behavior in ordinary structured extraction is not automatically evidence of an unauthorized fabric retry. Assess each operation's grant and contract separately.

## Correction sequence

1. Release/documentation owner verifies RD01 history and places old counts/qualification records under dated release headings.
2. Reconcile current maturity/contact/qualification vocabulary across README, PRODUCT, roadmap, conversation and deliberation plans.
3. Add the precise inbox and governed-continuation limitations; preserve the already-correct swarm and usage boundaries.
4. Coordinate RD07 with the pending roster review, keeping unpublished changes visibly separate.
5. Run a public-file privacy/reference check and verify all current claims against the exact source/gate registry. Rendered and live assertions need their own evidence; a documentation edit cannot supply it.

Owner: release/documentation maintainer, to be assigned under D05. The developer has received this queue for future execution under explicit authorization. No new implementation task or provider review is required merely to acknowledge these findings.
