---
run-agent: opencode
provider: openrouter
model: openrouter/openrouter/fusion
permission: safe-edit
openrouter_options: '{"plugins":[{"id":"fusion","preset":"general-budget"}]}'
---

# OpenRouter Fusion through OpenCode

Use OpenRouter Fusion's panel-and-judge workflow through OpenCode's tool and
file loop. This bundled seat uses the `general-budget` preset. Change the
validated `preset` to `general-high` or `general-fast` when the task warrants
the extra cost or latency. Fusion is intended for research, critique, and
synthesis; use a concrete model for a short tactical edit.

Final report:
- SUMMARY: what you did
- FILES_CHANGED: paths changed, or none
- TESTS: commands and outcomes
- RISKS: remaining uncertainty
LEFT_BEHIND: none, or every resource intentionally left running

Required final fields:
STATUS: DONE | PARTIAL | BLOCKED
FOLLOW-UP: recommended next actions, or "none"
HANDOFF: context the orchestrator must pass into the next call, or "none"
