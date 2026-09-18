# R01/R02/R12 canonical retention

Status: exact approved packets integrated and independently verified, 2026-09-08.

This file is the canonical crosswalk for the three approved packets. All six
files below now match the reviewed patch text; the three entry tests are included
in CI and the fixed release command. The lead independently ran them against
the integrated source: 12 passed in 18.34 seconds. Earlier wording incorrectly
substituted older, narrower tests and claimed retention; that error is corrected.
Those older tests remain valuable but do not replace these packets. This file does
not claim live-provider qualification, installation convergence, commit
approval, or release readiness. The named tests are the source of truth; the
longer planning notes describe context and historical findings only.

## R01 queued-version packet

The approved queued-version producer/consumer and public-emitter packet is retained in:

- `skills/summon/scripts/test_r01_queued_version_acceptance.py`

The packet covers changed registry generation, adapter version and external
CLI version at the real executor fence, plus the supported synthetic control
and no-contact public refusal, including unchanged ledger bytes. The lead's
independent approved run passed four cases. The test is retained in the fixed
release command in `tools/release_manifest.py` and corresponding CI command in
`.github/workflows/ci.yml`. The existing
`test_r01_r02_launch_chain_acceptance.py` covers observation propagation and
substituted material; it is not the queued-version packet.

## R02 candidate-fork packet

The approved candidate refusal and explicit non-launching fork packet is retained in:

- `skills/summon/scripts/test_r02_candidate_fork_acceptance.py`

The four-case packet proves that candidate/unsupported lanes refuse without contact and
that an explicit fork preserves the parent history and prompt without starting
a child turn. It is provider-free and does not certify a native-session
adapter. Its canonical release/CI references are present. The existing E12 turn/fork
test covers identity changes, not this complete candidate/unsupported lane table.

The user-facing compatibility and migration contract still needs reconciliation in:

- `docs/planning/R02_PREVIEW_MIGRATION_NOTICE.md`
- `docs/SUMMON_RESUME_CAPABILITIES.md`
- `CHANGELOG.md`

Remove stale future-tense implementation claims only where source and accepted
evidence prove completion. Limit the older-reader version fence to newly created
v2 rooms. The current legacy-v1 fork path still refuses and remains an assigned
L02 correction; do not advertise it as implemented before acceptance.

## R12 operator-body packet

The approved corrected operator-body packet retains all four files:

- `tests/test_operator_transport_boundary_acceptance.py`
- `tests/fixtures/operator_boundary/operator_boundary_worker.py`
- `tests/fixtures/operator_boundary/operator_harness.py`
- `tests/fixtures/operator_boundary/audit_fence.py`

The lead independently passed four cases using actual authenticated in-memory
operator handlers and exact 4096-byte acceptance / 4097-byte refusal. Retain the
corrected direct Unicode seed, not the earlier mojibake fixture. The entry
test is retained in the fixed release manifest and CI. Existing adapter attachment/refusal
tests and `tests/test_transport_adapter_exact_acceptance.py` cover distinct
transport boundaries; they do not exercise this operator request-body packet.

## Retention rule

Any source change touching these boundaries must rerun the exact files above,
preserve their CI/release-manifest entries, and record a new bounded receipt.
Do not report the packet as independently reviewed merely because its test
names remain present. A future release may supersede a packet only with a
reviewed replacement that names the new source files and updates this
crosswalk in the same change.
