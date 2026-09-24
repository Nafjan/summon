# R12 transport closure

Architect decision, 2026-09-08. R12 is VERIFIED-FOR-3.5. This source-derived
crosswalk retains accepted preview evidence and identifies the remaining
transport boundaries. The inventory review ran no tests or provider probes.
Paths below are relative to `skills/summon/scripts/`.

## Current closure checkpoint

All identified local transport boundaries now have accepted evidence. The four
operator packet files match the approved corrected patch, and CI plus the fixed
release command include its entry test. The lead independently ran all three
newly retained R01/R02/R12 packets on the integrated tree: 12 passed in 18.34
seconds, including the four operator cases. This closes R12 at the documented
preview scope. Historical findings below are superseded at their tested scope;
they are not additional unresolved defects.

The lead independently passed four corrected operator-body cases in 14.433
seconds against 112 matching runtime files. Actual bootstrap/authentication,
handler JSON parsing, adapters and journal accept a complete 4096-byte envelope
and refuse 4097 bytes without changing files or state. Direct Unicode code-point
assertions verify BOM, lambda, non-BMP, quotes and CRLF before serialization;
decoded message content survives intact. Cancel uses an ordinary queued delivery
and its actual command capability. Windows ACL verification and descriptor final
path lookup have narrowly owned fixture substitutions; OS security qualification
remains L07. This is an in-memory handler test, not a browser or socket test.

| Operation | Final boundary and retained proof | Claim limit |
| --- | --- | --- |
| Workspace task, next turn, same-task and taskless-supervisor recovery | Ordered complete-message context and authenticated transport frame; accepted workspace transport/budget/demo fixtures. | Provider-neutral owned local adapter; no native child-session access. |
| Operator message and cancel | Actual handler complete JSON body 4096/+1 and downstream authenticated journal acceptance/refusal; four-case operator packet. | No browser rendering, network lifetime or OS ACL claim. |
| Claude, Codex, Cursor, OpenCode and AGY fresh/resume builders | Fresh complete input versus explicit resume prompt/session selection; executor measures final resolved argv and merged environment. | Builder transport support is distinct from governed resume certification; candidates stay candidates. |
| Gemini fresh; Kimi fresh | Gemini raw system file 8 MiB/+1 plus final argv; Kimi final argv. Gemini has four exact synthetic adapter cases. | Both builders explicitly refuse resume. |
| ZCode fresh/resume | Adapter-owned UTF-8 attachment, greater-than-64-KiB identity/cleanup and 8 MiB refusal, plus final argv guard. | Synthetic local transport proof does not qualify a live session. |
| ACP | Actual serialized pipe bytes 8 MiB/+1 including Windows CRLF; final frame checked before every write. | Resume is unsupported. Preflight session-ID reservation is conservative; late refusal after launch preserves contact/spend uncertainty. |
| OpenAI-compatible HTTP | Actual serialized Request.data 8 MiB/+1 before opener/admission. | Stateless request boundary, not native-session resume or a provider context limit. |
| ArkCLI fresh/resume | Actual resolved argv/environment Windows 32767/+1 including continuation handle and instructions. | Registry API classification does not imply a non-argv transport. |
| Council continuation | Seven actual-process cases cover owned intermediate prompt file and exact internal decoded intake; external BOM/newline compatibility remains. | Downstream adapter limits still apply; no universal long-prompt promise. |

The corrected fifteen-case adapter packet and seven-case council packet are
verified in canonical CI/release coverage with exact functional retention.
The operator four-file packet has the same verified retention. POSIX byte/NUL
calculations have local unit coverage; Windows execution does not establish
POSIX process behavior. No fallback, truncation, account, model or billing
permission is added by transport acceptance.

## Detailed acceptance history

Corrected adapter-boundary acceptance, 2026-09-08: APPROVED at the bounded local
Windows scope. The lead reviewed all functional fixtures and the restored
default-deny signal guard, then independently passed 15 cases in 9.131 seconds.
All 112 frozen runtime files stayed unchanged and all six current transport
sources match. Exact 8 MiB/+1 HTTP Request.data and actual ACP pipe bytes,
Gemini actual builder/file/argv and ArkCLI fresh/resume argv/environment limits
pass. Owned children and fixtures are cleaned; no provider/network was used.

The lead verified all four retained files match the approved portable packet,
with CI and the fixed release command referencing the acceptance test.
Unrelated Git snapshots and Job Object facilities are explicitly unavailable in
these byte fixtures. Vendor decoding, file-replacement races, positive full ACP
handshake and POSIX process execution are outside this packet; retain their
existing scope separately. Whole R12 still needs final crosswalk/operator-context
evidence reconciliation. The earlier mismatch below is corrected at the actual
tested Windows write boundary, not merely by a mocked size calculation.

Current adapter packet review, 2026-09-08: REVISE. Source now includes finite
HTTP/ACP/system-file limits and ArkCLI final argv/environment measurement.
The three new adapter tests establish oversized refusals, not exact-limit
acceptance, and lack preconsumer private-state/process/network fences.

R12-ACP-WIRE-01 is independently reproduced with the actual `_AcpClient._send`
function and the same Windows UTF-8 text-wrapper policy used by Popen. An
89-byte measured frame is accepted at an 89-byte ceiling, but the text wrapper
writes 90 bytes because LF becomes CRLF. No process was created and source stayed
unchanged. Fix the measurement/write encoding agreement and verify actual owned
byte-receiver output; the function-only probe is not full child qualification.
Keep late frame refusal conservative after a child has already launched.

The source review also identifies a POSIX equality convention to reconcile:
the structured measurement includes argument NUL then rejects equality at its
declared limit, unlike the legacy builder. Tests must prove exact-limit and
limit-plus-one behavior. ACP's conservative ASCII session-ID reservation is
not an exact bound on a returned ID that is currently checked only as nonempty.
The final-frame guard still applies; avoid a stronger prelaunch claim.

Retain existing ZCode/workspace/council evidence. Next acceptance adds guarded
HTTP exact Request.data identity, ACP actual serialized writes, fresh/resume
ArkCLI argv/environment and actual Gemini system-file selection. Runtime fixes
remain conductor-owned; local synthetic tests cannot qualify a provider.

Implementation checkpoint, 2026-09-08: the conductor added owned UTF-8 prompt
files, an 8 MiB local ceiling and an internal exact-text intake to the council
hop. The lead inspected the writer, dispatch flag and UTF-8/newline-preserving
reader. Seven independent actual-process controls pass in 9.089 seconds,
including public council continuation, decoded CRLF/BOM identity, external
legacy normalization and bounded refusal. The lead reviewed the portable
fixtures and confirmed all 111 copied runtime files still match shared source.
The corrected pre-import audit fences are independently reviewed. The lead
rerun passes all seven cases in 5.921 seconds against current copied source;
all 112 copied runtime files stayed stable during execution, and owned fixtures
and children were cleaned. Seven functional methods are unchanged. Three
ancillary files differ from the helper's earlier snapshot; the council/parser/
intake/manifest sources match. This is not acceptance of unrelated accounting
or telemetry changes. The four-file portable packet is approved for exact
canonical integration, replacing the unguarded supplemental direct-only test.
This supersedes the earlier mocked-only transport checkpoint, without claiming
whole-R12 or live-provider qualification.

Implementation checkpoint, 2026-09-08: the remaining adapter-owned ceilings now
have provider-free final-boundary coverage. OpenAI-compatible requests measure
the exact UTF-8 `/chat/completions` body before the opener or durable launch
claim; ArkCLI measures the final launcher-resolved command and scrubbed
environment immediately before `subprocess.run`; Gemini's native system file
uses the same 8 MiB local attachment ceiling as its actual environment path;
and ACP measures a bounded prompt frame before child creation plus every exact
JSON-RPC line before write. Prelaunch oversize payloads emit a zero-attempt,
provider-contact-false refusal and never select a secondary route. A final ACP
frame refusal after child launch retains possible contact and uncertain spend.
The focused
adapter packet (`test_transport_adapter_acceptance.py`) and transport budget
tests pass, as does the existing ACP fixture suite. These are local synthetic
boundaries only; they do not qualify installed providers, provider context
windows, billing, or live sessions.

R12-TEXT-01, reproduced 2026-09-08: the actual intermediate process transports
exact prompt-file bytes, but the source-qualified child intake uses UTF-8-sig
and universal newlines. An admitted CRLF context reaches the file unchanged
and becomes LF in decoded `args.prompt`; a leading BOM is removed while an
internal BOM is preserved. The lead independently executed the exact main
intake AST with three owned synthetic controls and confirmed this behavior,
with unchanged source and cleaned fixtures. The independent process reviewer
also executes that frozen actual intake branch inside its synthetic child.

Preserve the previous generated prompt text on this internal hop. Use an
explicit internal exact-text intake or another lossless representation; keep
the existing external prompt-file BOM/newline compatibility policy unchanged.
Raw file digests alone cannot prove the decoded invocation's identity.

The actual top-level CLI exception net emits bounded error/not-run terminals
with zero attempts and provider contact false for oversize input and injected
prompt-file creation failure. Library-handler escape alone therefore does not
establish a missing public terminal envelope. Keep these as refusal controls,
not a new release blocker; complete transport packet acceptance remains pending.

| Path | Existing boundary/evidence | Remaining work |
| --- | --- | --- |
| Workspace task, next-turn and same-task recovery | `_workspace_runtime.py` selects 1–8 complete ordered messages within a 4096-byte context envelope; `_workspace_transport.py` bounds the authenticated frame to 8192 bytes plus framing prefix. Accepted F15 and transport-budget fixtures cover ordered selection and refusal before spawn. | Retain existing evidence; do not repeat it under a universal-native qualification requirement. |
| Taskless supervisor delivery/recovery | The same bounds apply, with durable offer/current binding before exposure. Existing transport/demo tests exercise exact receipt and oversize-before-write refusal. | Retain accepted evidence. The old one-message-only matrix description is stale. |
| Browser operator context/control | `_workspace_ui.py` bounds the complete HTTP JSON body to 4096 bytes and validates strict JSON. | Retain operator-context-specific Unicode/serialization boundary evidence in the final crosswalk. Bootstrap coverage alone is insufficient. |
| Native ZCode fresh/resume | Adapter-owned 8 MiB UTF-8 attachment; final argv remains checked. Accepted synthetic-child cases prove exact bytes above 64 KiB, cleanup and oversize refusal. | Retain accepted local evidence without claiming an installed/live session. |
| Ordinary subprocess adapters | `_executor.py` checks final launcher-resolved argv and merged environment through `_builder.argv_length_error`. Fresh/resume builders select different complete inputs; Gemini's system file now has an explicit 8 MiB local file budget. | Retain the fresh/resume crosswalk. Caller prompt-file input does not bypass downstream argv limits. |
| ACP and OpenAI-compatible HTTP | `_acpbackend.py` writes serialized `session/prompt`; `_apibackend.py` constructs the final HTTP JSON body. Both use an explicit local 8 MiB serialized boundary. Known oversize input is refused before contact; ACP rechecks each final JSON-RPC line and preserves uncertainty after launch. | Retain synthetic final-write evidence and document it as an operational ceiling, not a provider limit. Do not invent provider context-window or billing limits. |
| ArkCLI | `_arkcli_backend.py` builds instructions, continuation ID and prompt argv, despite registry kind `api`. Its direct `subprocess.run` now measures the final command and scrubbed environment through the shared argv budget. | Retain zero-contact refusal evidence; installed ArkCLI and provider behavior remain separately qualified. |
| Council checkpoint continuation to child dispatcher | `_council_between_round.py` accepts a context packet up to 64 KiB. `_council._dispatch` now writes a bounded owned UTF-8 prompt file and requests exact internal intake. Seven actual-process controls independently pass with pre-import audit fencing, verifying admitted context and separate 8 MiB direct transport. | Retain the exact approved four-file packet in canonical coverage. Downstream adapter limits still apply; this is not universal long-prompt transport. |

## Implementation disposition

1. For the new council journey, use the existing child prompt-file intake for
   the intermediate parent-to-dispatcher hop, with an owned bounded UTF-8 file,
   exact content, lifecycle cleanup and existing permission/provenance checks.
   This does not claim universal provider transport: the child still applies
   its final adapter guard. If a complete required prompt cannot fit, refuse
   before contact and preserve admitted context and original uncertainty.
   Cover the actual synthetic child, not only a patched council dispatch.
2. Add ArkCLI's final argv/environment measurement at its real launch boundary,
   preserving existing launcher selection and explicit billing consent.
3. Declare finite local limits for ACP, HTTP and native system-file paths.
   Reuse an established adapter limit where one exists; otherwise use an
   explicit 8 MiB Summon operational ceiling, documented as a local bound and
   never as provider capability. Preflight complete known content before
   resource creation and check the exact final serialized request before write.
   Never truncate, silently drop required context or switch route on refusal.
4. Retain the existing documented legacy argv-to-ACP selection policy and its
   kill switches while qualifying the destination boundary. Explicit structured
   capability failure continues to prohibit fallback. Do not broaden automatic
   routing, accounts, models or billing permission through this work.

Test each changed boundary at the declared ceiling and beyond it, including
UTF-8/UTF-16 as applicable, escaping, newlines, BOM policy, envelope overhead,
fresh/resume selection, pre-contact refusal and preserved required content.
Use synthetic local executables/streams only. Coordinate API/ACP payload changes
with E07 accounting so both inspect the same final request representation.
The conductor owns implementation, integration and scoped helper assignments.
No provider, account, installation, commit or release authority is added.
