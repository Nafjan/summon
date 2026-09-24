# F02 authentication acceptance

Decision: VERIFIED-FOR-3.5 at the reviewed provider-free preview scope,
2026-09-08. The lead reconciled current source, meaningful retained fixtures,
prior independent ingress/operator acceptance and fixed release commands.
This is evidence reconciliation, not a newly executed test receipt.

The threat model in P1_VERTICAL_SLICE_BRIEF.md trusts the OS user, launcher and
supervisor. It tests hostile protocol inputs confined to issued connections.
Names, PIDs or an HMAC key accessible to arbitrary same-user code do not provide
OS isolation; that stronger qualification remains explicitly refused.

| Required boundary | Current source and retained behavioral evidence |
| --- | --- |
| Bootstrap and authenticated responses | `_workspace_transport.OwnedFakeWorker` creates fresh key, nonce and challenge, transfers bootstrap through owned pipes and checks the response. `Channel` verifies protocol, direction, exact binding, sequence, MAC and payload digest. `test_workspace_transport.py` includes actual two-worker cross-channel rejection. |
| Scope and real observed principal | `_workspace_runtime._install_ingress` accepts an actual owned connection under explicit host authorization, with registered claim/instance/grant bindings. `_receive_worker_send` derives the request from its observed token and rechecks registration, claim, attempt, lease, selected context and source references at final admission. `test_workspace_worker_send.py` retains unchanged-journal authority refusals and final-recheck revocation. |
| Handle confinement and secret transport | Actual transport fixtures verify unrelated inheritable handles are not leaked and bootstrap secrets are absent from launch arguments. Hidden fixed interpreter workers have restricted environment and closed unrelated handles. |
| Rotation, expiry and revocation | Disconnect revokes the old channel; restart uses a new nonce/key. Runtime scope tests reject expired/revoked grants, and the actual demo revokes an observed sender before admission without recipient exposure. |
| Supervisor domain separation | SupervisorChannel uses separate protocol/direction/binding rules. Transport and demo fixtures retain wrong-domain/offer/epoch, replay, expired receipt and same-name replacement refusals. |
| Browser operator identity | `_workspace_ui.py` retains one-use bootstrap, hashed expiring sessions, exact Host/Origin checks and authentication rechecks. Actual handler and prior rendered operator evidence cover these separately from worker credentials. |
| Restart does not grant work | Runtime ingress is not restored by reading history. `test_workspace_entry.py` verifies fresh bootstrap and no resumed workers. Replacement execution needs new explicit grants. |

The prior independently accepted actual ingress gate is recorded in
P1_WORKER_INGRESS_CONTRACT.md; later supervisor and rendered operator acceptance
remain recorded in RELEASE35_IMPLEMENTATION_HANDOFF.md. Current tests are
retained in fixed `workspace_core`, `workspace_ui` and `browser_security`
commands. The final source-bound release run and reviewed skips remain L09/L12.

Source and test names in the table are under `skills/summon/scripts/`.
The base reducer's `authenticated_worker_ingress=false` remains accurate: the
runtime adapter authenticates connections, not the low-level reducer itself.
No live native reconnect, arbitrary hostile executable or provider adapter is
qualified. F17 public command wiring and L07 OS privacy remain separate gates;
their incompleteness must not be hidden by this authentication acceptance.
