---
run-agent: opencode
model: openrouter/openrouter/auto
permission: safe-edit
openrouter_options: '{"plugins":[{"id":"auto-router","cost_tier":"max"}]}'
---

# OpenRouter Auto through OpenCode

Use OpenRouter's Auto Router through OpenCode's tool and file loop. The `max`
cost tier asks OpenRouter to favor the strongest eligible model for the task;
the envelope's served-model evidence is the authority for what actually ran.
Do not claim a concrete model before the provider response identifies it.

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
