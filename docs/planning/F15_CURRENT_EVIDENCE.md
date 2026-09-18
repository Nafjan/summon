# F15 current evidence checkpoint

Status: implementation is still in progress; this is not release or provider
qualification.

The durable worker-turn reservation path is implemented and covered by the
provider-free transport/coordinator partitions. The supervisor-offer path now
projects the actual offered inbox message as a distinct zero-slot budget item,
binds rate/stream/byte facts, validates the current endpoint owner/lease/grant
inside the coordinator mutation, and persists a once-only pre-write record.

Latest retained command (Python 3.12):

```text
py -3.12 -m py_compile skills/summon/scripts/_swarm_coordinator.py skills/summon/scripts/_workspace_runtime.py skills/summon/scripts/_workspace_budget.py
py -3.12 -m pytest -q skills/summon/scripts/test_workspace_budget.py skills/summon/scripts/test_workspace_demo.py -k supervisor
```

Result: 3 passed, 1 failed, 22 deselected; elapsed 220.25 seconds. The
failure was the existing concurrent supervisor fixture failing during fixed
worker bootstrap with `response_incomplete_or_timeout`, before the offer
assertion. No provider or network call was made. The source tree did not
change during this run. The pytest process exited and no owned test child
remained.

The retained full-demo attempt then completed in 626.12 seconds with 14 tests
passed, 9 failures, and 7 subtests. The failures clustered in one shared
recovery projection defect: a queued/offered successor reused its logical
message ID, while the budget snapshot selected the first historical parent
delivery (held/dead-lettered) and hid the successor. The typed failures were
`supervisor_offer_budget_refused_budget_binding_required` during successor
exposure, `SwarmBudgetRefusal` during same-task recovery, and the persisted
endpoint recovery refusal. A separate `KeyError` was a stale test assertion:
the held-lane admission correctly refused before creating a `main-2` worker.

The coordinator now prefers the current pending delivery for duplicate logical
message IDs, preserving historical parent records while binding the budget to
the active recovery successor. Focused rechecks passed: endpoint recovery
2/2 subtests in 57.06s, same-task recovery 3/3 subtests in 66.73s, the
supervisor budget refusal case 1/1 in 14.11s, and the four-case recovery/hold
partition 3/4 tests plus 2 subtests in 460.80s before the stale `max_live`
expectation was corrected from 2 to 1. The complete demo then passed 17/17
tests with 13 subtests in 717.30s. No providers, network, credentials, or
installations were contacted.

The full-demo pass is a provider-free behavioral checkpoint only. The final
delta run passed 17 tests and 13 subtests in 577.37s. The affected
swarm/projection/transport/acceptance partition passed 86 tests in 135.32s;
the explicit held-parent/queued-successor lineage regression passed 1 test in
35.11s; and schema/release checks passed 38 tests in 206.85s. The preceding
broad workspace partition passed 793 tests and 69 subtests in 226.72s, and
the earlier fixed partition passed 123 tests in 166.69s. Python 3.12
compilation of the touched coordinator, runtime, budget, and demo test modules
also passes. These partitions ran with pytest plugin autoload disabled and did
not contact providers or the network.

Current F15 change inventory is limited to the durable budget/recovery seam:
`skills/summon/scripts/_swarm_coordinator.py`,
`skills/summon/scripts/_workspace_budget.py`,
`skills/summon/scripts/_workspace_runtime.py`,
`skills/summon/scripts/test_workspace_budget.py`,
`skills/summon/scripts/test_workspace_demo.py`,
`skills/summon/scripts/test_workspace_reservation_acceptance.py`,
`tools/schema_inventory.py`, `tests/test_schema_inventory.py`,
`tools/release_manifest.py`, and this checkpoint document. Other dirty files
in the working tree predate this packet and remain untouched.

F15 is now approved as a bounded provider-free 3.5 preview implementation
checkpoint by the independent architect/project-lead review. It is not a
release or provider qualification. The next authorized work is the R01/R02
capability seam in `RELEASE35_IMPLEMENTATION_HANDOFF.md`; no install, commit,
release, or provider call is implied here.
