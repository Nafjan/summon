# Deliberate: when and how to use it

> Part of the **summon** skill. This is the operator and agent decision guide for
> the bounded `deliberate` run type. The durable contract is specified in
> [DELIBERATION_PRODUCT_PLAN.md](../../../docs/DELIBERATION_PRODUCT_PLAN.md).

## What `deliberate` is

`deliberate` is a governed decision run, not a prettier dispatch and not a chat
room. The caller fixes the question, participant seats, allowed options, quorum,
physical-attempt budget, and absolute deadline before a run can make progress.
Summon records those inputs in an immutable receipt and derives the result from a
generation-fenced journal.

Choose it when all of these are true:

- the work is a **decision** rather than an implementation task;
- there are at least two explicit options to compare;
- named seats/personas should form independent positions;
- a fixed quorum and a hard spend/time bound matter;
- a replayable audit trail, crash boundary, or explicit human approval/cancel
  boundary is part of the deliverable.

Do not choose it merely because a task is complicated. Complexity without a
decision policy is a normal dispatch or a manifest job.

## Choose the right Summon mode

| Need | Use | Why |
|---|---|---|
| One agent implements, researches, or reviews a task | ordinary `dispatch` | The task has one owner and no ballot policy. |
| Independent jobs should run in parallel | `manifest` / fan-out | Each job gets its own envelope and failure boundary. |
| Diverse opinions plus cross-examination and a chairman's synthesis | `council` | Council is the existing multi-round synthesis workflow and remains distinct from `deliberate`. |
| A receipt-bound decision with fixed options, quorum, attempts, and deadline | `deliberate` | The journal and scheduler, not model prose, own the decision boundary. |
| Inspect an existing run without provider work | `deliberate status` or `replay` | Read-only, checksum-verified views. |
| Repair a journal-proven crash boundary | `deliberate recover` | Provider-inert recovery under one owner. |
| Continue after a crash | `deliberate resume` | Explicit spend policy is required; an indeterminate attempt is never silently replayed. |
| Queue a stop request | `deliberate cancel` | Queued is not the same as durably applied. |
| Watch the local ledger | `deliberate open` | Opens/reuses one authenticated loopback surface; it does not start a provider. |

If the request names a decision but does not provide explicit options, seats, or
approval policy, ask for those facts. Do not invent a quorum or silently turn a
normal dispatch into a governed run.

## Council and deliberate are a handoff, not a mixed run

Use `council` first when the option space is unknown or the user wants open-ended
positions, cross-examination, and a chairman's synthesis. A council may discover
candidate options, but its prose is not a ballot. If the user wants a governed
decision afterward, present the candidate options and obtain explicit seats,
quorum, rounds, attempt budget, deadline, and approval policy, then start a fresh
`deliberate` receipt. Do not auto-promote council output, reuse a council chairman
as a hidden voter, or silently fall back between modes. A future "deliberation
round" may share bounded context through a sealed handoff artifact, but it must
still cross this explicit policy boundary.

## Current live-provider gate

The kernel, journal, replay, recovery, roster, invocation, and local observer
contracts are implemented and tested. The fresh CLI has a deliberately narrow
owner-bound lane: it admits only an explicit quorum, one round, one physical
attempt per seat, enforceable read-only subprocess seats with executable
evidence, and a bounded typed-cancel watcher. The receipt and `run_prepared`
record are durable before any provider contact, and the lane never retries,
falls back, enables ACP/HTTP/Kimi/text-only/writable/full-bypass seats, or
silently changes mode. `deliberate resume` and human approval remain
`integration_pending` until sealed checkpoint restore, provider identity
revalidation, and the command coordinator have their own gates. For a workflow
outside that lane, use `council` or ordinary dispatch only with the user's
consent; never treat a blocked result as an invitation to retry through another
mode.

## Safe command recipes

Run a preflight before a paid workflow:

```text
summon doctor --json
summon deliberate --question "Which option should we ship?" \
  --seats planner,security,operator \
  --options ship,hold \
  --quorum all --rounds 1 --max-attempts 3 --deadline 30m \
  --run-dir <private-runs-root> --cwd <project>
```

The command above is the canonical shape for the fresh read-only lane. Supply
`--quorum`, `--rounds 1`, `--max-attempts` equal to the seat count, and
`--deadline` explicitly; do not rely on parser defaults. It fails closed as
`integration_pending` when a seat is not an enforceable read-only subprocess or
its executable evidence is unavailable. `--require-human-approval`, text-only
consent, and full-authority consent are intentionally rejected by this lane
until their separate durable coordinators exist. Treat any blocked result as a
safety gate, not as an invitation to retry through another mode without the
user's consent.

For an existing run:

```text
summon deliberate status RUN_ID --run-dir <private-runs-root> --json    # derived state, no provider call
summon deliberate replay RUN_ID --run-dir <private-runs-root> --json    # bounded public journal, no provider call
summon deliberate recover RUN_ID --run-dir <private-runs-root> --json   # deterministic crash boundary only
summon deliberate cancel RUN_ID --run-dir <private-runs-root> --json    # queue a typed cancellation
summon deliberate open RUN_ID --run-dir <private-runs-root>             # observe it locally
```

`deliberate resume RUN_ID` is an explicit spend decision, not a generic retry.
Without `--retry-indeterminate`, a durable unmatched start remains blocked as
`uncertain_spend` and makes zero provider calls. With the flag, the new physical
attempt must still fit the sealed receipt budget and owner/deadline fences.

## Browser handoff

`deliberate open` starts at most one authenticated loopback surface per run and
reuses it on later calls. Use `--browser auto` (default) to prefer the host's
configured IDE/browser bridge, then the built-in Browser Harness when the host
advertises `iab`, then the system browser. Use `builtin` or `ide` to require one
of those integrations, `system` to skip them, and `link` in SSH/CI so the command
prints a URL without launching a tab. The URL contains a bearer token in its
fragment; never paste it into tickets, prompts, or telemetry.

The browser is an observation/control surface only. It cannot approve a decision
in the current preview, and opening it never activates a provider.

## Agent operating rules

When choosing a mode on a user's behalf:

1. State why this is a governed decision and list the fixed options, seats,
   quorum, deadline, and attempt budget.
2. Prefer read-only seats for review. Record explicit consent for text-only or
   full-authority seats; do not infer authority from a role name or model name.
3. Run `doctor --json` and inspect install drift before a significant run.
4. Keep `--run-dir` private and durable. Never put prompts, credentials, argv,
   profile paths, or raw model output in a public report or browser URL.
5. Read `status`/`replay` after a run and verify the receipt, decision, dissent,
   uncertain-spend state, deadline, and cleanup handoff before acting.
6. Treat `blocked`, `integration_pending`, `uncertain_spend`, owner loss, and
   `LEFT_BEHIND` as outcomes requiring review—not failures to hide with retries.
