# R01 first-change decision

Status: the user explicitly authorized this bounded contract-and-fixture implementation on 2026-09-05, preserving current runtime behavior and pending roster edits. This is the first bounded slice of R01, not completion of R01 or the workspace roadmap. Based on the developer's source-backed specification and project-lead inspection.

## Proposed scope

Add the pure versioned capability contract and its fixtures beside the existing exact v1 interfaces. Establish one canonical capability authority with explicit compatibility projections. Preserve all existing runtime routing, launch, resume, continuation-source and public-status behavior in this first change. R02 stages behavioral migration separately; R13 and F01-F18 remain required before the P1 preview admits work.

The proposal covers contract code, directly necessary provider-inert tests and public contract documentation. It does not cover provider probes, installs, credential changes, releases, commits, native adapters or dependency scheduling. Preserve pending Astra/Flash edits; do not reorganize them to make this change easier.

## Lead decisions on the proposed contract

| Topic | Decision and acceptance |
| --- | --- |
| Operation scope | Start with resume and queued steering contracts. Unknown operations refuse; this narrow initial schema must not imply other workspace or native operations are certified. |
| Adapter versions | Include explicit bounded adapter-version qualification scope and executable-observation binding in the contract design. Adapter identity alone is insufficient. Runtime observations stay outside the pure registry; no implicit wildcard version qualification. |
| Registry trust | Generation/digest are compared against the process-owned canonical registry and authenticated qualification provenance. Caller-supplied matching fields or a self-consistent hash do not authenticate authority. Test fabricated registry/qualification objects. |
| Capability versus acknowledgement | A registry describes whether an operation is supported or qualified. Actual receipt/acknowledgement belongs to the attempt/delivery evidence. Keep current steering queued-only; no static capability boolean can claim a live message was acknowledged. Preserve legacy v1's false acknowledgement field exactly. |
| Qualification freshness | Generation/digest and observed version checks are mandatory at admission. Expiry may be adapter-specific, but must use a named unit, finite bounded type and explicit null semantics; reject bool-as-number, invalid timestamps and rollback. No automatic live re-probe. |
| Registry purity | No PATH/profile/environment/filesystem/network/credential inspection inside the registry. Observation and authentication occur in runtime adapters; pass bounded validated evidence to pure evaluation. Preserve the current purity test. |
| Current Claude compatibility | Preserve the certified v1 lane and compatible historical continuation. R02 may explicitly revalidate and seal a migrated source when required evidence exists. Never invent missing historical evidence or declare all v1 Claude sources permanently read-only by default. If required evidence is missing, refuse with a concrete reason and offer an explicit non-launching fork. |
| Candidate lanes | Codex/Cursor/OpenCode/ZCode stay candidates; AGY and other unsupported lanes are not promoted. No new route certification in the first contract change. |
| Eligibility versus permission | A valid capability contract is not a provider-call grant. Even matching generation/version can establish only that gate's eligibility; session authentication, model evidence, project, authority, budget and owner fencing still apply. |
| Stable v1 representation | Preserve exact object fields/values and current callers. If byte-level stability is claimed, test the actual serializer as well; the current dictionary-equality tests alone do not prove bytes. |
| One source of authority | Keep compatibility projections derived from canonical facts, without two independently editable capability lists. Legacy readers remain distinct exact validators; no silent v1-to-v2 coercion. |

The proposed v2 field list is not accepted verbatim until version scope, receipt separation and trusted evidence inputs follow these decisions. Adding a field-shaped placeholder does not satisfy its validation contract.

## Producer and consumer map

| Repository source | Current contract | Treatment |
| --- | --- | --- |
| `skills/summon/scripts/_resume_capabilities.py` | Exact seven-field resume-capabilities/v1; deterministic pure rows | Add explicit v2 contract/evaluation and canonical compatibility projection; preserve v1 behavior |
| `skills/summon/scripts/_job_continuation.py` | Exact private continuation-source/v1 with authenticated bindings; exact public job-continuation/v1; route-specific Claude guard | Inventory and fixture current behavior now; change source/admission bindings only in staged R02 migration |
| `skills/summon/scripts/_jobs.py` | Exact public continuation field set and capability consistency checks | Preserve current status output now; separate exact v2 projection when R02 activates it |
| `skills/summon/scripts/_job_resume.py` | Consumes authenticated continuation source and governed successor context | Preserve current authority boundary; add drift/revocation admission wiring only with R02 |
| `skills/summon/scripts/_conversation_runtime.py` | Three duplicate allowlist decision sites | Leave behavior unchanged in first R01 contract slice; reconcile all three in R02 |
| `skills/summon/scripts/test_resume_capabilities.py` | Row, unknown-input, forgery and purity checks | Preserve all 16 current cases and add meaningful v2 positive/negative fixtures |

Current source-based subprocess matrix:

| Backend | Current chat allowlist | Governed registry | R02 policy |
| --- | --- | --- | --- |
| Claude | Yes | Certified | Preserve compatible authenticated continuation |
| Codex, Cursor | Yes | Candidate | Governed request refuses pending qualification; announce changed preview behavior and offer non-launching fork |
| AGY | Yes | Unsupported | Governed request refuses; announce changed preview behavior and offer non-launching fork |
| OpenCode, ZCode | No | Candidate | No new certification or implicit addition |
| Kimi, Gemini | No | Unsupported | No new certification or implicit addition |

The matrix was derived from current registry rows and the literal chat allowlist. It describes policy declarations, not live provider availability or individual session evidence. Ordinary dispatch is not being deprecated by this resume policy.

## First-change acceptance

1. Existing exact v1 registry and status/continuation fixtures pass without changed routing, new provider attempts or reclassified historical receipts.
2. V2 validates exact keys/types, bounded identifiers, known operation/adapter/version combinations, canonical generation/digest, qualification provenance and explicit expiry semantics.
3. Forged qualification, unknown versions/operations, arbitrary added fields, secret-like identifiers and a claimed live acknowledgement refuse without echoing private input.
4. Registry purity remains tested. Observation/admission fixtures remain provider-inert; matching metadata alone never produces permission to launch.
5. Canonical facts and v1/v2 projections cannot drift independently. Test cross-version consistency and preservation of candidate/unsupported rows.
6. The reviewed diff stays confined to the contract, its meaningful tests and contract documentation. Identify every touched consumer; do not rewrite surrounding runtime behavior.
7. Report which R01 requirements this first slice implements and which admission-time checks remain for R02. Do not mark all of R01 complete from a pure schema fixture.

The 16 original registry tests passed during the initial leadership review. The authorized implementation and independent verification are now recorded in R01_CONTRACT_ACCEPTANCE.md. The corrected contract suite contains 29 passing tests, but the full affected run has two unresolved failures; do not present it as a green release gate.

## Authorization decision

The user explicitly authorized this bounded contract-and-fixture implementation after reviewing the scope. That authorization permits source/test changes only within this slice; it does not authorize provider spending, installs, commits or releases. Runtime behavior migration remains a later reviewed change. The leadership/monitoring goal itself provides no additional permission.
