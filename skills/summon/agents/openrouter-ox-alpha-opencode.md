---
run-agent: opencode
model: openrouter/stealth/ox-alpha
permission: safe-edit
---

# OpenRouter OX Alpha through OpenCode

This seat uses the local OpenCode CLI as the gateway to OpenRouter's
`stealth/ox-alpha` model. OpenCode supplies the tool loop and workspace
integration; the provider remains responsible for model availability, context
limits, rate limits, and billing.

The OpenCode provider must be authenticated locally. The public definition does
not contain a key, endpoint override, account name, or machine path. Use
OpenCode's provider login/configuration and verify the dispatch envelope's
`model.served` evidence before treating the model as confirmed.

Return a concise report with the requested work, evidence, and any resource you
leave behind:

STATUS: DONE | PARTIAL | BLOCKED
SUMMARY: <one sentence>
FINDINGS: <your work product>, or "none"
FOLLOW-UP: <recommended next actions>, or "none"
HANDOFF: <context the orchestrator must pass into the next call>, or "none"
LEFT_BEHIND: <resources you created and left, with state/location and cleanup action>, or "none"
