# Summon 3.2 plan

Status: release contract for Summon 3.2.x. The bounded routing, roster, privacy,
and chat-shell work described here shipped in 3.2.0; the 3.2.1 patch adds the
OpenCode gateway and startup-isolation hardening. Rendered-browser,
provider-specific, and signed-evidence gates remain explicitly separate.

Summon 3.2 has two linked goals:

1. Make an explicit Codex model request trustworthy. A request for Sol must not
   silently become Luna because a later selector, ambient configuration, resume
   handle, retry path, or provider response changed the route.
2. Make the local conversation atlas calm and predictable. The browser window
   must not jiggle when notices or turn state change, and the user must always
   know what can be done, which agent is selected, and which model/provider
   identity is declared versus observed.

The plan was reviewed through independent planning lanes covering native Sol,
Gemini Flash, DeepSeek Pro, and GLM. Their shared conclusion was to make model
routing fail closed first, then graduate the chat surface only through rendered
browser and lifecycle evidence. A model label, a fake receipt, or a unit-test
pass is not proof of what a provider served.

## 1. Codex model-selection contract

Resolve one immutable selection before profile lookup, workspace setup, or
provider contact. The record carries:

- requested model and source (`cli`, `frontmatter`, `legacy_args`, `profile_default`,
  `ambient_config`, or `provider_default`);
- one canonical launch selector;
- targeted handshake identity;
- terminal served identity and evidence class (`reported`, `inferred`, or `absent`);
- exact-required flag, configuration/profile revision digests, and executable revision.

Precedence is explicit CLI pin, agent frontmatter, one legacy selector when no
pin exists, a verified profile default, ambient Codex configuration, then an
explicitly unpinned provider default. A profile model list is an allowlist, not
a selector.

Agent `-m`, `--model`, and `-c model=...` selectors are parsed and attested. A
single canonical `-m` is emitted. Agreeing duplicates are collapsed; conflicting
selectors block before `Popen` with `model_selection_conflict` and no provider
contact. The selector is included in dry-run output without private paths or
configuration contents.

An explicit Codex pin requires an authoritative terminal served-model receipt:

- exact match: success and usable result;
- different handshake: `target_model_mismatch`, blocked, no retry;
- missing or malformed terminal identity: `served_model_unverified`, blocked,
  result unusable;
- different terminal identity: `served_model_mismatch`, blocked, result unusable.

These outcomes are terminal across ordinary retries, transient retries, ACP or
backend fallback, schema/report repair, manifests, council, deliberate, chat,
and swarm workers. A later operator-directed fresh request is a new request and
must have a new fingerprint; automatic replay is not allowed.

## 2. Resume and identity fencing

At first contact, a Codex resume binding records the backend, opaque session
digest, original requested/targeted/served model, evidence class, agent-definition
digest, profile/config/executable revisions, permission, project, transport, and
generation. A resume is allowed only when the binding and the new request agree.

Legacy resume handles without an exact binding remain readable but are not
exact-model-resumable. Summon offers a fresh or forked run instead of inventing
proof. A verified Sol session can resume only as Sol; adding a different `-m`
does not rebind it. A resumed Luna or missing receipt is the same terminal blocked
outcome as a fresh mismatch.

Codex profile isolation is not assumed from another vendor's profile mechanism.
It is added only after a controlled CLI experiment proves its native config-home
and precedence behavior. Until then, named Codex profiles remain unsupported or
advisory, with a clear diagnostic.

## 3. Diagnostics, roster, and privacy

`doctor` reports provider-inert facts: executable/version support, selector source
and conflicts, safe config/profile digests, evidence support, and resume-binding
health. `doctor --live codex --model ... --confirm-provider-call` is a separate,
bounded diagnostic that writes a source-bound artifact outside the repository.
It performs no automatic retry and never includes prompts, results, credentials,
absolute paths, raw session IDs, or account data.

Roster refresh remains explicit. Cached, configured, static, and live sources are
labelled separately. Catalog names and frontier/editorial labels never become
dispatch proof; every pinned live route needs its own exact `model.served`
receipt. Unknown or unverified models stay non-dispatchable for strict lanes.

## 4. Conversation atlas redesign

### Stable shell and scroll ownership

- `html`, `body`, and the atlas use `100dvh` and do not scroll.
- The center column has stable header, status/activity, feed, and composer rows.
- One dedicated feed scroller owns conversation scrolling; rail and evidence
  panes scroll independently.
- Notices, latest/new-event controls, activity, and recovery use reserved-height
  slots or overlays, never variable-height insertion above the feed.
- Tablet and mobile evidence/room navigation use modal sheets with focus return,
  Escape, inert background, and safe-area handling.

### Anchoring and long journals

Capture pinned state and the first visible cursor before each update. Reconcile
events by cursor rather than replacing the entire timeline. A pinned user stays
pinned; an unpinned user keeps the same visible event and offset while a new-event
indicator appears. Add bounded pagination and a bounded DOM window for long
journals; do not claim arbitrary-history virtualization until it is tested.

### Clear identity and authority

Every agent choice shows, in order, role, agent name/version, target model, and
provider. Every completed turn shows served-model evidence beside the author:
match, differs, reported without target, inferred, or unavailable. The UI never
relabels the target as verified. Context messages remain visually and
semantically separate from provider turns and kernel receipts.

### Lifetime and errors

Owner health, event-stream health, and provider-turn state are separate. Closing
the browser tab does not stop the detached local owner or provider work; explicit
stop/restart controls govern that lifetime. Owner loss preserves the timeline,
disables mutations, and offers reconnect/restart. Stream loss is nonterminal and
uses bounded backoff. Provider timeout, cancellation, recovery, and uncertain
spend are distinct from browser connectivity.

## 5. Compatibility and release gates

3.1 journals and envelope version 1 remain readable. New fields are additive;
legacy `model.resolved` is advisory. New consumers branch on status,
`result_usable`, `model.served`, and `served_model_evidence`.

Provider-inert CI must cover:

1. selector precedence and conflict refusal before process launch;
2. exact-one canonical Codex selector construction;
3. served match, served mismatch, missing receipt, malformed receipt, and
   handshake mismatch fixtures;
4. one-launch/no-retry behavior across dispatch and orchestration paths;
5. resume binding and legacy-handle refusal;
6. redacted telemetry and additive 3.1 compatibility;
7. browser geometry, zero window-scroll, pinned/unpinned anchoring, bounded
   pagination, owner-vs-stream failure, responsive sheets, keyboard focus, and
   reduced-motion behavior.

Live release evidence must be generated against the exact candidate commit and
installed CLI. A Codex Sol claim requires a real provider-reported served Sol
receipt in that artifact. If the CLI cannot emit authoritative served identity,
the named-model certification lane remains blocked; the release must not infer Sol from
`-m`, `thread.started`, output tokens, or a catalog label. This does not make the Codex
backend preview-only: ordinary Codex dispatch remains supported, with named-model
provenance clearly marked unavailable when the receipt is absent. The lane graduates when
the CLI or adapter exposes a provider-authored terminal model receipt and the live
match/mismatch/missing-receipt matrix passes.

3.2 GA requires a clean immutable source tree, synchronized metadata, all fixed
suites and browser gates, converged managed installs, and no open P0/P1 review
finding. The release may ship supported Codex dispatch while keeping the explicit
named-model certification lane honestly gated.
