---
run-agent: opencode
model: openrouter/openrouter/free
permission: safe-edit
---

# OpenRouter Free Models Router through OpenCode

Use OpenRouter's free router for low-volume experiments. It selects a free
model dynamically, so availability, latency, tool quality, and the served model
can change. Inspect the provider receipt before treating the result as a
reproducible review.

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
