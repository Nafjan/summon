# R02 proposal review and next behavior boundary

Updated: 2026-09-05. DEV-R02-SPEC-01 initial proposal: REVISE. The lead requested a corrected read-only handoff; no behavioral implementation has been dispatched. Decisions follow LEADERSHIP_AUTHORITY.md. The accepted P0/Windows patch remains preserved.

## Source findings

| ID | Source evidence | Required decision |
| --- | --- | --- |
| R02-RV1 | `_conversation_runtime.py:_identity` hashes configured provider/profile labels for account fields and the target model for requested identity. `_finish` hashes the returned served string, or the literal unsealed sentinel when absent. These bytes match the accepted baseline. | Treat these as current consistency checks, not authenticated account/version observations. The lead initially alleged a target fallback in `_finish`; that allegation is withdrawn. Missing served data can already prevent matching continuation. |
| R02-RV2 | `_resume_capabilities.py` v2 declares source-only, unqualified evidence and fixed `launch_permission=not_granted`. | Do not turn a matching registry digest into session qualification or a public launch grant. Capability, private qualification, authorization and execution outcome need distinct contracts. |
| R02-RV3 | Chat first starts a dispatcher subprocess; provider contact occurs downstream. | A refusal proved before any dispatcher launch may describe that invocation as not run. After dispatch or uncertain process creation, preserve unknown contact/attempt facts unless authoritative evidence resolves them. Never describe a concurrent winner through the refused invocation. |
| R02-RV4 | `start_turn` invokes `_fork` automatically on unsupported or incompatible continuation; `_fork` writes parent and child histories. | A refusal should offer an explicit fork without creating it. The existing authenticated fork action performs mutation only when selected. Do not advertise an implemented rebind capability until it exists. |
| R02-RV5 | `_conversation.py` accepts only a fixed event set during reads and appends. | Adding migration/rollback events changes storage compatibility. Avoid history writes during evaluation; choose versioned storage deliberately if new event types become necessary. |
| R02-RV6 | The original proposal rolls new requests back to v1 behavior; chat's independent allowlist currently permits Codex, Cursor and AGY. | Rollback must preserve the stricter refusal boundary, readable histories and uncertainty. Never re-enable an unsafe fallback merely by disabling a feature flag. |
| R02-RV7 | Canonical registry uses backend/transport pairs. Codex/Cursor/OpenCode/ZCode subprocess are candidates; AGY/Gemini/Kimi subprocess are unsupported. | Preserve exact classifications and ordinary dispatch. Chat's outer subprocess does not make the inner ACP transport qualified for resume. |
| R02-RV8 | CLI management currently returns zero for every ordinary result; HTTP turns uses 202; the browser assumes a non-fork result started and clears its draft. | Wire typed refusal through CLI exit status, HTTP status and browser draft/active-state behavior together. A refused request must not look started or lose its prompt. |

## Lead proposal for Fable decision

The next implemented behavior should remove the duplicate chat allowlist and automatic fork mutation, rather than add another unused projection. Use the existing canonical backend/transport declaration as a necessary gate at admission and again before dispatcher launch. Candidate/unsupported continuation refuses with a bounded typed reason, no automatic room creation and an explicit existing fork action. Ordinary fresh dispatch and already authenticated governed Claude job continuation remain unchanged.

Two boundaries must be distinguished before approval:

1. **Registry/refusal migration:** remove the three allowlist decisions; wire honest refusal across runtime, CLI, HTTP and browser; preserve current compatible Claude preview checks without relabelling them as authenticated qualification. No new v2 admission, descriptor, history event, provider route or rebind capability is implied.
2. **Authenticated qualification migration:** implement the actual trusted observation/receipt producer, private binding, expiry/revocation and submission revalidation needed to qualify chat continuation and complete R01/R02. Do not manufacture these facts from old label hashes. Existing authenticated governed job sources have their own strict schema and HMAC validation; reusing that authority requires an explicit compatible contract, not casting chat records into it.

Fable must decide whether the first boundary can be accepted as an independently useful behavioral step while the second remains explicitly open, or whether the required qualification producer must ship in the same change. This is sequencing within the full roadmap; neither option permits claiming R01/R02 complete from a deny-only gate or retaining a duplicate policy source.

The migration notice must name affected preview continuation lanes, readable history, explicit non-launching fork, and unchanged ordinary dispatch. The existing v1 capability and governed continuation schemas stay exact. Rollback can disable new qualified operations while retaining refusal, never restore the duplicate allowlist.

## Acceptance requirements for the selected implementation

- All three allowlist sites use the canonical backend/transport gate, including submission revalidation after a queued wait. Reject a route or capability change before dispatch and retain honest late-refusal state after a durable turn claim.
- Candidate and unsupported cases cannot call Popen, create a room, append a fork, rewrite history or spend. Preserve the distinction between no-op evaluation and durable attempt closure.
- Existing compatible Claude behavior has source-accurate coverage and labels; malformed/missing historical evidence is never upgraded silently. Native attachment and live steering stay unqualified.
- Explicit fork requires the existing authenticated command, creates only a new room, preserves the user's context and does not dispatch. Recovery and ownership fences remain intact.
- CLI refusal exits nonzero with bounded JSON; HTTP refusal is not accepted/started; browser preserves draft, clears provisional activity and presents the typed reason plus a usable explicit fork action.
- Roster, model, permission, profile, transport, membership and owner changes are checked at the correct boundary. A changed requested label is not proof of a changed or unchanged account credential.
- Existing journals remain readable without new event kinds or rewritten receipts. Candidate refusal remains active under rollback.
- Test real public paths with fake providers and ownership/cancellation races. Preserve P0/Windows spawn flags and exact capability/governed-source contracts. Record what is proved, which observations remain missing and the next qualification gate.

HANDOFF: developer returns a revised source-backed proposal; lead resolves concrete choices with Fable, then dispatches implementation under the existing delegation. No installation, credential changes, commit or release is included.
