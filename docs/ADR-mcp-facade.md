# ADR: MCP facade

**Status:** Accepted (Summon 1.2 / 2.0 train)
**Date:** 2026-08-07

## Context

Agent Plugin clients can surface MCP servers beside skills. Summon already has
a complete stdlib dispatcher (`run_subagent.py`). Phase C needs a second
**facade**, not a second implementation.

## Decision

1. Ship `skills/summon/scripts/mcp_server.py` — stdio JSON-RPC (MCP tools
   subset) with **no MCP SDK dependency**.
2. Declare it from root `mcp.json` using Agent Plugins schema
   (`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`) with
   `cwd: ${PLUGIN_ROOT}` and a plugin-relative script path.
3. Tools wrap existing entrypoints:
   - `summon_doctor`, `summon_list_agents`, `summon_dispatch`,
     `summon_council`, `summon_manifest`, `summon_onboard_status`
4. Skill + MCP share one broker; no chat UI, no remote control surface.

## Consequences

- Hosts that only load skills keep working unchanged.
- Schema version of `mcp.json` should track the same Agent Plugins 1.0.0
  generation as root `plugin.json` when present.
- Tool handlers must never return live API keys; onboard status reports
  credential **presence** only.

## Trust model

The MCP facade is a **local trusted-host** surface (stdio from a host the
operator already trusts). Tools accept `cwd` / manifest paths and can run
billable long dispatches. Do **not** expose this server on a network without
an additional allowlist/jail. Prefer structured JSON payloads over raw CLI
tails; when tails are returned they must be secret-scrubbed. Pin the Python
launcher carefully on Windows (`python` Store aliases can skew versions).
