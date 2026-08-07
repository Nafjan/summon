# ADR: Provider-driver SPI

**Status:** Accepted (Summon 1.2 / 2.0 train)
**Date:** 2026-08-07

## Context

Summon historically branched on CLI name inside `_builder` / `_executor`
(subprocess argv vs openai-compat HTTP vs ACP). Phase C introduces a thin
provider-driver SPI so new seats (notably `arkcli +chat`) and host facades
(MCP) can share one vocabulary without rewriting the broker.

## Decision

1. Add `skills/summon/scripts/_drivers.py` with drivers:
   - `cli` — facade over named argv backends via `execute_agent`
   - `openai-compat` — Chat Completions HTTP (existing `_apibackend`)
   - `arkcli` — `api`-kind backend whose `call` runs `arkcli +chat` (profile
     store auth); dual-wired with openai-compat for Coding Plan HTTP seats
   - `acp` — facade over existing native ACP transports
2. Keep the existing `BACKENDS` registry as the execution truth; the SPI is a
   thin layer (`probe` / `resolve_model` / `dispatch_via_driver` / `cancel`)
   plus `enrich_envelope_from_cli` / `stamp_driver_fields`.
3. Envelope gains additive fields only: `provider.driver`, `backend_type`,
   `served.via` (and sibling `served_via`).
4. Document Summon `read-only` / `safe-edit` / `full` (`yolo`) ↔ provider-native
   modes with honest fallbacks (chat seats are advisory; ACP non-yolo refused).

## Consequences

- New backends register in `BACKENDS` and map into a driver id.
- MCP and future hosts may call `dispatch_via_driver` or the CLI unchanged.
- Permission translation lives in `PERMISSION_MAP` — not silent upgrades.
