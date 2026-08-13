# Deliberation Control Surface

Status: design contract for the optional post-headless-gate loopback observer; the
reference surface is provider-inert and not a CLI auto-start.

## Direction

The surface is a governed decision ledger shaped like an archival flight-recorder
instrument, not a chat client. Its job is to let an operator answer three questions in
seconds: what is durably true now, what evidence produced it, and what human action is
safe next.

The direction is the third grounded Operate structure from the Impeccable concept round:
an instrument panel whose persistent geometry makes state legible. The visual world is
matte petrol and uncoated evidence-paper surfaces, graphite rules, and one vermilion
commit signal. There is no neon glow, decorative glass, gradient text, or fake telemetry.
Measurements use tabular numerals; prose uses a quiet workhorse sans. Color is never the
only state channel.

## First viewport

1. A compact run bar spans the top: run name, durable state, owner/connection health,
   absolute time remaining, physical attempts, rounds, quorum, and uncertain-spend flag.
2. The main ledger occupies the reading column. Turns are grouped by round and ordered by
   durable journal sequence. Each turn separates participant role, observed executor
   facts, ballot/evidence, and collapsed model prose.
3. The evidence rail holds the fixed seat roster, capability/permission evidence,
   quorum rule, policy fingerprint, and cleanup/LEFT_BEHIND handoff. It becomes a drawer
   below the ledger on narrow screens.
4. A persistent action shelf appears only for `WAITING_HUMAN`: approve, deny, cancel, and
   bounded message actions show queued -> durable -> applied/rejected as distinct states.

## Interaction and states

- Live updates arrive through a versioned redacted snapshot/event contract; the browser
  never infers authority from model text.
- Reconnect announces stale data and requests a canonical snapshot when a cursor gap is
  detected. A lost owner or terminal run visibly closes the action shelf.
- Empty, loading, disconnected, expired, corrupt, cleanup-failed, uncertain-spend, and
  retained-resource states have explicit text and icon/shape treatment.
- Keyboard focus is visible; all controls have names; announcements use a live region;
  reduced-motion mode removes nonessential transitions.
- The page is optimized for a narrow desktop window beside an IDE and remains usable at
  phone width without shrinking the complete network into unreadable miniature text.

## Provenance boundary

Beautiful UI informed the functional categories to study (streaming state, task rows,
approval affordances), not the source, assets, tokens, or composition. No Beautiful UI
code or asset is copied. The visual system above is authored for Summon's governed-ledger
mechanism and must be validated against actual redacted run data before UI implementation.

## Sequencing gate

The reference stdlib surface now lives in `_deliberation_ui.py` and consumes only the
redacted store contract. It remains provider-inert and policy-inert: browser commands can
queue only typed cancel until the durable coordinator applies richer human commands.
CLI/resume activation, hosted collaboration, and background/council composition remain
outside this slice.

<!--
  Impeccable direction contract for the eventual root layout:
  THESIS: A deliberation is a governed decision ledger, not a chat stream.
  OWN-WORLD: Archival flight-recorder instrument: matte petrol, evidence paper, graphite rules, vermilion commit signal, no glow.
  STORY: The operator sees durable truth, traces evidence, and performs only an explicit safe human action.
  FIRST VIEWPORT: Run bar above round ledger, evidence rail at right, action shelf only in WAITING_HUMAN.
  FORM: Grounded Operate structure 3 from seed bf89d2b6; challengers were weighed as inspiration only.
  FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
-->
