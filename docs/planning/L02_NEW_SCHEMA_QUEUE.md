# L02 new-schema closure queue

2026-09-09 source-only comparison against the verified local 3.4.0 source
candidate found 29 new schema literals absent from format inventory. Another
43 unrepresented literals already existed in that source candidate. These
counts precede pending metadata integration and are not counts of defects.
The historical source candidate is not a signed/published-package attestation.

The bounded read-only audit classified all 29. No family was established as
only a hash-domain label. Some records are nested in other records, some are
wire/public inputs or outputs, and others are in-process capability contracts.
Do not invent a separate on-disk store for each literal. Existing semantic
associations in schema inventory do not replace explicit format metadata.

The six-family durable packet and seven-family wire packet are now accepted
for integration after independent source review and 86/74 source-only tests
in 3.19/1.78 seconds respectively, with owned Python sources unchanged. They
are not canonically integrated or runtime-certified. Their identical metadata
allowlist addition for the actual `protocol` discriminator must be applied once.
The table's lane-active labels below describe the preceding dispatch checkpoint;
both bounded grants have ended.

Eight stable input/capability mappings are also accepted for integration after
lead source review and 74 metadata tests in 3.49 seconds. Internal launch-scope
facts remain private evidence even though a reduced comparison result shares
the literal. The producer-only rebuild projection and seven authority/observation
families remain outside this accepted batch. Missing precise compatibility
fixtures stay recorded gaps; metadata acceptance does not close L02 by itself.

All names below have the `summon.` prefix. Exact symbols and behavioral fixture
references must be source-checked in each patch; suite names alone are not
adequate metadata acceptance. No referenced runtime test was run for this audit.

| Batch | Families | Required distinction |
| --- | --- | --- |
| Durable and nested evidence (6) | `background-launch/v2`, `submission-accounting-handoff/v1`, `workspace/v1`, `workspace/v2`, `workspace.economics-reservation/v1`, `workspace.effects-observation/v1` | Parent/container relation, current versus historical writers, authority versus evidence, exact supported-reader behavior. Owned mapping lane active. |
| Wire and spawn (7) | `workspace.pipe/v1`, `workspace.supervisor-pipe/v1`, `workspace.context/v1`, `workspace.message-send/v1`, `workspace.message-send-result/v1`, `workspace.transport-admission/v1`, `transport-budget/v1` | Framing/handshake, payload bounds, one-shot admission and actual producer/consumer references. Owned mapping lane active. |
| Authority and observation (7) | `workspace-command-policy/v1`, `workspace.operator-command-request/v1`, `workspace.operator-decision/v1`, `workspace.operator-message-grant/v1`, `workspace.operator-scope/v1`, `workspace.supervisor-offer-observation/v1`, `workspace.supervisor-receipt-observation/v1` | Bytes versus reference ownership; present authorization is distinct from stored evidence. Final F17 policy/request mappings wait for stable integration. |
| Public inputs/results and contracts (9) | `resume-capabilities/v2`, `resume-launch-scope/v1`, `swarm.projection-rebuild/v1`, `workspace.plan/v1`, `workspace-admission/v1`, `workspace.operator-send-base/v1`, `workspace.supervisor-inbox-base/v1`, `workspace.worker-send-base/v1`, `workspace.supervisor-offer-budget/v1` | User input, derived public output, nested consumed budget and callable capability contracts are different roles. No fictional deserializer for a producer-only output. |

The supervisor observation/pipe and workspace-v2 rows still require precise
compatibility-fixture selection. An existing relevant suite does not prove
literal-specific compatibility. Operator-authored policy/plan inputs may have
no production writer and must say so. The detached rebuild output had no
separate production deserializer identified; classify this limitation honestly
instead of weakening the inventory validator to make it fit.

This queue prioritizes new producers before release. It does not waive the
remaining historical fleet/context/usage and other explicitly required family
classification, nor replace the retained historical-reader/rollback gates.
All shared integration belongs to the conductor. No provider contact, runtime
state inspection, installation, commit or publication is authorized here.
