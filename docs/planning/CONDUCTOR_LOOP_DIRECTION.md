# Goal-directed multi-agent loop engineering

Owner direction, 2026-09-05. Summon should help a conductor pursue an explicit goal through effective multi-agent loops while delegating bounded detail work. The workspace and message fabric support that outcome; they are not the product's ultimate objective. Attention to detail remains necessary, but the conductor should integrate evidence and make decisions rather than personally investigate every issue.

This direction refines WORKSPACE_CHECKLIST_V3.md. It does not certify new runtime behavior, authorize additional provider spending, or replace the council and deliberation authority kernels.

## Product loop

Goal and acceptance criteria -> choose the next useful step -> assign bounded lanes -> gather evidence -> assess progress and remaining risk -> continue, revise, or finish.

The conductor owns the goal, priorities, dependencies, integration decisions and completion judgment. Specialist lanes own implementation, investigation, testing and adversarial review within their assignments. A lane returns findings and evidence against its acceptance criteria, identifies unresolved issues, and stops or requests an explicit extension. Its activity and confident prose do not establish progress or completion.

## Required behaviors

| Capability | Product behavior | Existing roadmap connection |
| --- | --- | --- |
| Persistent goal anchor | Keep the objective, observable success criteria, current milestone, constraints and unresolved decisions available across handoffs, restart and context compaction. Changes to the goal are explicit and retain history. | F01, F16, H03, H12 |
| Small active priority set | Show the next useful step and which lanes block it. Side issues remain visible with an owner and disposition, without automatically displacing the primary objective. | F03, F15, S05, U02 |
| Bounded delegation | Every lane has a parent goal/task, a concrete outcome, scope, authority, available resource budget, acceptance evidence, escalation trigger and return condition. Prefer independent lanes where useful work can continue concurrently. | F02, F05, F10, H11, S03-S05 |
| Focus checkpoints | At milestones, lane returns or configurable review boundaries, ask what evidence changed, whether current work advances the goal, and whether details should be delegated, deferred or escalated. A warning is advisory; it does not silently cancel work or change policy. | H02-H04, U02 |
| Evidence-based integration | Keep outcome, test/artifact evidence, limitations, model identity, uncertainty and HANDOFF distinct. Conductor acceptance is explicit. A review recommendation cannot silently become approval or a deliberation ballot. | R03, F08, G01-G08 |
| Bounded investigation | An investigation states what decision it informs and what evidence will end it. Repeated checks without new evidence trigger a focus checkpoint. Necessary reliability work can block a milestone when the lead records the reason. | H02, H03, R13 |
| Proportionate escalation | Escalate material contradictions, authority changes and unresolved blockers. Routine implementation detail stays with its lane. Severity, dependency impact and uncertainty determine escalation; verbosity and agent confidence do not. | F14, H04, G01-G08 |
| Reviewed practice updates | Maintain versioned orchestration playbooks with source/evaluation date, applicability, measured outcomes and migration notes. New advice is reviewed before adoption; remote text cannot rewrite runtime permissions or policy. | R01, R16, L01-L12 |

## First useful demonstration

Extend the first provider-inert two-worker slice with a visible goal and acceptance criteria. One worker encounters a secondary issue and sends a bounded investigation request. The supervisor assigns it within existing authority, continues independent goal work, receives a concise evidence-backed result, and explicitly integrates or defers it. Restart preserves the goal, lane status, pending deliveries and unresolved decisions. No background message launches a provider merely because it asks for help.

The first demo needs goal/task fields and projections over the chosen authoritative store, not a new supervisor engine or competing scheduler. Automatic delegation is available only inside a previously authorized policy and budget; otherwise Summon can prepare a proposed lane without launching it. The narrow fake-worker fixture establishes protocol behavior, not hostile same-user process isolation or live-provider effectiveness.

The concrete implementation demonstration uses two logical worker identities across three distinct task-bound attempt/channel instances, with at most two live at once. A channel's immutable task binding must not be reused for a successor task. Each fixed simulated worker computes a bounded deterministic result that the supervisor independently checks; authenticated message receipt alone is not work evidence or task completion. The successor task requires a fresh explicit decision and grant based on the first task's accepted evidence. The side investigation stays active while useful main evidence arrives. This proves the bounded local loop mechanics; it does not establish live-model effectiveness.

## Acceptance and measurement

### Re-entry after compaction or restart

Before the next consequential action, recover the authoritative goal, current
assigned task, latest owner instruction and recorded authority limits. Compare
the proposed action with that checkpoint. A summary, remembered historical task
or model-generated plan cannot introduce a new objective or permissions. If a
newer direct owner instruction exists, retain its scope explicitly and preserve
the interrupted task's handoff rather than silently abandoning it.

Recover the last actual failing or incomplete acceptance boundary and inspect
the corresponding live handle before restarting work. An observation timeout
does not establish that a process stopped. Distinguish an isolated candidate,
shared edits and integrated acceptance; a live conductor task is not a separate
helper handle. Keep private diagnostic findings outside public handoffs.

The 2026-09-08 campaign exposed a coordination ambiguity: the conductor moved
from isolated council acceptance to installation/auth diagnostics, and the
architect requested a scope check. The conductor confirmed a newer direct owner
instruction separately authorized that bounded health check. That instruction
takes precedence within its scope; this was not established unauthorized drift.
The conductor stopped diagnostics and returned to the preserved council task.
This supports recording owner redirections across task boundaries, not exposing
diagnostic findings or broadening the release campaign's provider authority.

Retained acceptance scenarios should include a stale summary suggesting an old
task, an unrelated provider-health recommendation, and a newer genuine owner
redirection. The first two must not expand launch/auth/spend authority; the last
must preserve the prior task and record the changed priority. Evaluate resumed
goal alignment and time to the next useful result, not the amount of repeated
research after a restart.

- A secondary non-blocking issue can be delegated while independent critical work continues; it cannot silently replace the goal or expand authority.
- A genuinely blocking issue pauses its dependent work with an explicit reason while unrelated authorized lanes may continue.
- A lane that reaches its return condition reports evidence and stops; repeated low-information activity does not justify unlimited extension.
- Duplicate messages, restarts, stale workers and lost acknowledgements preserve goal/task lineage and uncertainty without duplicate submission.
- Completion requires the declared acceptance evidence and disposition of required unresolved work. A model's success statement or elapsed activity is insufficient.
- Evaluate completed criteria, accepted/rejected handoffs, repeated investigations without new evidence, blocker age and time to useful integrated results. Keep body-free aggregate signals bounded and local. Do not optimize agent count, message count or token throughput as proxies for success.

HANDOFF: the product lead owns this direction. The workspace brief should demonstrate goal -> bounded delegation -> evidence -> next decision. Runtime implementation remains phased through R02/R13 and the first authenticated two-worker fixture. Windows reliability stays in its dedicated lane and does not consume the conductor's whole agenda.
