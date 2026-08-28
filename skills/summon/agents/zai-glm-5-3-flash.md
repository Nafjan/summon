---
run-agent: openai-compat
provider: zai-coding-plan
model: glm-5.3-flash
model-policy: exact
permission: read-only
capability: text-only
---

# Z.AI Coding Plan GLM-5.3 Flash (direct text seat)

This optional direct Z.AI Coding Plan seat makes one OpenAI-compatible text
request to the coding-only endpoint. It does not provide a tool loop,
filesystem access, session resume, or a sandbox. Give it self-contained text
such as a design question, pasted diff, or review packet; use the OpenCode
Z.AI seat for toolful work in a disposable boundary.

The model is explicitly requested, but treat it as a named-model result only
when the provider returns a matching `model.served` with reported evidence.
The public definition contains no key, account information, endpoint override,
or local configuration path. Explicit `ZAI_CODING_API_KEY` takes precedence;
an existing official helper configuration is read only through a bounded,
non-symlink scalar reader and never copied to a child process or receipt.

Availability, quota, pricing, context limits, and provider model routing are
live facts. No fallback to another endpoint or model occurs automatically.

STATUS: DONE | PARTIAL | BLOCKED
SUMMARY: <one sentence>
FINDINGS: <work product or none>
FOLLOW-UP: <next action or none>
HANDOFF: <context for the next call or none>
LEFT_BEHIND: <resources and cleanup or none>
