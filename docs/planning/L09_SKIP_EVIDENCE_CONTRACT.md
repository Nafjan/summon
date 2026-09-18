# L09 structured skip evidence

Architect implementation direction, 2026-09-08. Technical review remains
pending; this is not a Fable endorsement or release acceptance. The conductor
owns implementation after the current launch-observation correction packet.
This closes only the skip-evidence portion of L09 when independently verified;
rendered artifacts, toolchain retention and stale-consumer gates remain required.

## Current integration checkpoint

2026-09-08: real discovery/install/ACP harness callbacks and the two retained
discovery skip/leak regressions are integrated. CI's exact suite equality now
includes both structured outcomes and schema inventory; a full suite/gate
registry comparison prevents this specific consumer drift. The two discovery
cases and CI equality case independently pass. The conductor reports actual
custom-collector results of 31/31 install and 39/39 ACP with zero skips; no full
discovery capture was repeated for this checkpoint.

Both release consumers now validate exact gate-artifact fields and safe
diagnostic values, including after a recomputed digest. Suite-result wrappers
must contain exactly count, output digest and structured outcomes. Independent
synthetic review first reproduced ten gate-field and five suite-wrapper leaks;
the retained correction tests require both consumers to refuse them. Valid and
blocked producer shapes, historical sanitized preview reads and projected-record
equality remain covered. The integrated four-file release test partition reports
121 passes from the conductor; independent Python 3.13 privacy/outcome execution
passes 86 cases. Source review confirms the approved validation logic is present.

The previously captured workspace 436/436 and outcome 15/15 artifacts have valid
internal digests, complete outcomes and no reported skips/problems. They no
longer match current source after integration and are historical evidence only.
These checks do not qualify the full platform matrix, reviewed skip exclusions,
rendered/browser/toolchain evidence, or the final release candidate. The skip
policy remains empty; no required gate is waived. Use focused checks while
implementation continues and bind final captures to the eventual frozen source.

## Earlier candidate review checkpoints

Subsequent shared-source checkpoint: outcome and gate tests pass independently
(27 cases, 1.77 seconds, Python 3.12), with seven relevant source/test files
unchanged during execution. The whole-file generated-ID bypass is absent.
This is partial integration: discovery/install/ACP harness callbacks and the
candidate's two discovery skip/leak tests are not yet integrated. The new
producer marker-redaction case passes; matching consumer semantic/privacy
validation still needs evidence. These omissions remain L09 work and cannot be
erased by reporting the smaller passing test count.

Architect disposition on the subsequent generated-ID change: REVISE. A
whole-file exception for `test_workspace_plan.py` and
`test_workspace_protocol.py` was added with a "reviewed" comment, but the
architect has not accepted that identity contract. Redaction of parameter
representations is necessary; it does not prove deterministic identity or
stable targeting of skip decisions. Remove the bypass and use explicit complete
source-literal case IDs, or return a separately reviewed generated-identity
contract with behavioral evidence. Captures using the bypass are diagnostic
only and cannot establish this contract's qualification. Preserve all test
assertions and do not weaken privacy refusal to obtain a passing capture.

Integrated-source privacy finding, 2026-09-08: a synthetic direct call confirms
`release_gates._artifact` forwards arbitrary marker strings in `detail`,
`evidence_file` and `error_kind`; manifest intake retains the gate artifact.
This violates the intended safe release projection even though the outcome
collector redacts skipped-case reasons. The conductor must restrict diagnostic
values to source-defined safe codes/reviewed artifact names and prevent a
recomputed artifact hash from bypassing the same rule at the consumer. No real
private values, credentials or receipts were inspected in this reproduction.

The bounded native implementation stage completed on 2026-09-08. Architect
source review confirms a real fixed-command collector, shared outcome validator,
schema-2 intake/final-check integration, an empty source-bound exclusion policy,
legacy preview reads and custom-harness callbacks. The conductor may integrate
the candidate with the latest registry preserved; this is conditional integration
direction, not acceptance of L09 or a cross-vendor endorsement.

Independent Python 3.12 / pytest 9.1.1 execution of outcome and gate tests
returned 27 passed and one failed in 1.70 seconds. The failed test assumes that
absence of the external pytest-subtests package means the fixture is unavailable.
The current pytest supplies it internally: a separate actual capture retained
the skipped subtest, redacted its private reason and left it blocking. Correct
the test's capability detection and retain this real core-pytest coverage.
The helper's distinct Python 3.13.11 / pytest 8.4.2 command reported 29 passes,
including the shared Windows launch-policy fixture; do not merge the two counts.

Before acceptance, qualify the current fixed registry rather than only synthetic
fixtures. In particular, implicit IDs in `test_workspace_plan.py` and
`test_workspace_protocol.py` currently cause privacy-safe blocking outcomes;
give those cases explicit stable source-literal identities without changing
assertions. Preserve the registry additions made after the isolated baseline.
Full manifest/contract regression coverage, exact platform review decisions,
the remaining conditional custom-harness audit and rendered/toolchain evidence
are still required. No unreviewed skip is approved by this checkpoint.

## Verified problem

`tools/release_gates.py::_parse_count` permits skipped cases in a numeric ratio.
`tools/release_manifest.py::_passing_count` accepts that ratio, while both gate
artifact validators omit semantic test-outcome validation. The evidence reader
also drops runtime metadata. Counts alone cannot establish what actually ran.

The native read-only review additionally found standalone discovery tests that
print a skip or return on unavailable platform support, then receive PASS from
the custom runner. The architect independently confirmed the live-symlink and
FIFO branches and the runner in `skills/summon/scripts/test_discovery.py`.
Framework-only collection would therefore leave an evidence gap. No broad test
run was performed for this contract review.

## Required contract

Introduce versioned structured outcomes in the release evidence envelope.
Schema 2 is the proposed envelope boundary; preserve schema-1 historical reads,
but historical counts alone cannot satisfy this new release requirement. Do not
rewrite old evidence. Keep ordinary test commands and standalone harness
semantics; bind any fixed instrumentation wrapper to its actual executed
template as well as the canonical registry command.

Each suite and test-backed gate must retain:

- Exact registry kind/name/command, source and producer identity, collector
  revision, and Git binding, subject to the existing source-stability checks.
- Allowlisted platform and runtime versions, completion/exit state, collected
  test identities, reconciled terminal-case counts, and separately accounted
  subtests. Passing setup/teardown events are not extra passed test cases.
- One complete observed skip inventory with case, phase, safe reason code and
  safe literal reason. Collection skips without enumerable coverage cannot
  qualify mandatory evidence. Do not silently discard skipped subtests.
- Source-bound skip policy identity and the existing artifact/output bindings.

Capture actual pytest/unittest/custom-harness outcomes. Convert conditional
nonexecution in the custom harnesses to explicit skipped outcomes while retaining
their global-leak and failure behavior. Audit the fixed command registry for
other custom runners; do not infer completeness from pytest output alone.

A checked-in review policy must match exact registry command, test identity,
reason, platform predicate and stable repository-relative review decision.
Default unmatched skips to blocking. No wildcard, caller waiver, or producer
`approved=true` field may grant acceptance. Missing tools, permissions or
required infrastructure are not automatically platform exclusions.

A reviewed platform exclusion establishes only nonapplicability on that
platform. It cannot substitute for a required passing run on another platform.
Preserve the required release platform matrix independently of local results.
Keep live-provider receipt validation a distinct typed evidence path.

Use the same structural and semantic validator for evidence intake and final
manifest checks, for both suites and gates. Incomplete collection, inconsistent
counts, unknown categories, nonzero exit, errors, xfailed/xpassed/deselected
cases and zero executed tests cannot become release PASS. A recomputed artifact
hash must not legitimize invalid semantics.

Retain only safe source-relative test identifiers and approved literal reasons.
Dynamic parameter IDs or errors containing paths or sensitive values require a
bounded redacted refusal and an explicit safe identifier before qualification.
No stdout/stderr, environment, traceback or arbitrary host strings enter public
artifacts. Preserve external evidence storage and atomic/link-safe publication.

## Acceptance and handoff

The conductor should implement this as a bounded release-tooling packet, with
one writer and isolated writable lanes if delegated. No new provider attempts,
installs, credential/account changes, commits or publication are authorized.

Retain small real pytest, unittest and custom-runner fixtures for pass/skip,
subtests, fixture/collection errors, and the discovery conditional-skip case.
Cover exact reviewed exclusions versus missing-tool/unreviewed/wrong-platform
skips; duplicate/foreign identities; malformed counts; incomplete inventory;
nonzero exits despite printed success; timeouts; and failure accounting.
Test command/source/collector/policy drift and tampered artifacts with recomputed
hashes at both consumers. Verify old evidence stays readable but cannot pass the
new gate, safe runtime metadata survives intake, private text cannot escape,
and existing Windows silent-launch behavior is retained.

Before integration acceptance, independently review the actual schema and
producer/consumer diff. Required cross-vendor review remains a release-review
condition within separately available authorized capacity; none was performed
for this direction. No L09 requirement is promoted by this document.
