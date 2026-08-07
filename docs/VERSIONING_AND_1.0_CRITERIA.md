# Versioning, and what summon 1.0.0 means

Status: **criteria met; 1.0.0 publication authorized on 2026-07-29.** Current line is 1.2.x.

> This line drifted (it said 0.14.x through four minor releases). If you are editing
> it, that is the eleventh instance of the defect this repo keeps hitting: a claim and
> the thing it describes, maintained separately.

This document exists because "is it 1.0 yet?" is otherwise answered by feel, and feel is
a poor instrument for a promise that binds every future release. Below: what 1.0.0
commits summon to, the criteria that decided it, and an honest scoring against them.

---

## What 1.0.0 commits to

Under semantic versioning, 1.0.0 is not a quality badge. It is a **promise about
change**: the public contract is stable, and breaking it requires 2.0.0. So the question
is not "is summon good?" but "is this contract one we are willing to freeze, and do we
understand it well enough to promise not to break it by accident?"

summon's public contract is five surfaces:

| Surface | What is promised |
|---|---|
| **CLI** | Flag and subcommand names, their semantics, and their defaults |
| **Envelope** | The response JSON. Already independently versioned (`envelope: 1`), which bumps only on a breaking SHAPE change, never on added fields |
| **Agent definitions** | Frontmatter keys (`run-agent`, `model`, `permission`, `effort`, `args`) and the bundled-roster fallback |
| **Report contract** | `STATUS` / `SUMMARY` / `HANDOFF` and the rest of the block agents are told to emit |
| **Exit codes** | 0 success, 124 timeout, 127 backend missing, 1 general |

Anything not on that list -- internal module layout, helper names, log wording -- is not
contract and may change in a patch.

---

## The criteria

Each is written so it can be checked, not felt.

### C1. The public surface is enumerated and frozen
Every public flag documented; no undocumented public flag; internal ones explicitly
`argparse.SUPPRESS`. A test enforces this rather than a promise to remember.

### C2. Documentation matches behaviour
No claim in the docs that measurement would contradict. Where a claim is volatile
(model ids, alias expansion, backend capability), it is dated and marked as a measurement
rather than a standing fact.

### C3. Green across the supported matrix
CI passes on every supported OS and Python version, and the suite is hermetic: no test
depends on a vendor CLI being installed, on a platform-only constant, or on winning a
race.

### C4. Defect discovery has flattened
**The load-bearing criterion.** Successive independent adversarial reviews of the same
surface return no new material findings. A version that still yields a BLOCK per review
round is not stable; it is merely un-reviewed.

Bar: **three consecutive review rounds over the changed surface with no BLOCK and no
CONCERN**, at least two of them cross-vendor.

### C5. Security controls survive adversarial review
The privilege controls (`--gate-with`, `--max-permission`, the permission tiers) have no
known bypass, and each has a regression test that has been mutation-verified -- i.e.
proven to fail when the fix is reverted.

### C6. The envelope never lies
Every evidence field is either populated with a true value or explicitly null. No field
that the docs describe as evidence may be structurally unreachable. A blocked dispatch
never carries an approval; an inferred value is never presented as a confirmed one.

### C7. Upgrade and drift are handled
Installed copies are discoverable and their staleness detectable, so an upgrade cannot
silently leave a host running old code.

---

## Honest scoring, 2026-07-29 (v1.0.0 candidate)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| C1 | Surface enumerated and frozen | **MET** | Every public flag is documented; a structural test parses the argparse spec and fails the suite on any undocumented flag. `--job-file` is the only suppressed one. |
| C2 | Docs match behaviour | **MET** | Version/sample claims are bound to `__version__`; volatile backend claims are measured and qualified; the 0.19 feedback corrections are regression-bound. |
| C3 | Green across the matrix | **MET** | CI was green on ubuntu/windows x 3.10/3.13 before the final surface; the corrected 1.0 candidate passes 429/429 discovery and 22/22 installer tests locally, with platform-only cases explicitly skipped where unavailable. |
| C4 | Defect discovery flattened | **MET (3/3)** | Three fresh Cursor/Composer adversarial reviews returned CLEAN on production-script hash `21aecdd7aa3dd84653b9c8ef5630e20869fce8b1b3e94d797b37c13a69b4c1e4`. Invalid, timed-out, repaired, resumed, BLOCK, and CONCERNS attempts were excluded. |
| C5 | Security controls hold | **MET** | Gate/clamp bypass fixes are mutation-verified. Round 1 exercised a real `--max-permission read-only` clamp; a separate current-surface gated dispatch explicitly approved and then returned CLEAN. No repo mutation occurred. |
| C6 | The envelope never lies | **MET** | `execution_status` now snapshots executor outcome independently from normalized `verdict`; `resumed`, model evidence, gate decisions, artifact stability, and null/unknown states are explicit and regression-bound. |
| C7 | Upgrade and drift handled | **MET** | `doctor` enumerates host installs, the running copy, and project-local copies; drift reports a stale copy by hash. |

**Verdict: 7 of 7 met. 1.0.0 is warranted.**

---

## Current C4 closure evidence: three clean rounds on the corrected surface

The production surface froze at script hash
`21aecdd7aa3dd84653b9c8ef5630e20869fce8b1b3e94d797b37c13a69b4c1e4`.
All three counting reviews were fresh Cursor/Composer sessions over Codex-authored code
and used the same bounded evidence packet. No production file changed between rounds.

| Round | Job | Focus | Structured evidence | Verdict |
|---|---|---|---|---|
| 1 | `25aa0191accf4d3b977fd2c028112450` | Runtime billing, argv precedence, resume evidence | `status:success`, `execution_status:success`, `report_ok:true`, `verdict:pass`, one attempt, no repair/resume | **CLEAN** — notes only |
| 2 | `26d46c22aaf24e8182600a185e6133d8` | Negative cases, warning parity, security disposition | Same success/pass fields; one attempt, no repair/resume | **CLEAN** — no findings |
| 3 | `6901603cb84441c890ccdb05df975071` | Public-contract truth, cross-platform consistency | Same success/pass fields; one attempt, no repair/resume | **CLEAN** — no findings |

Cursor reported the pinned `composer-2.5` target and nonzero usage but does not expose a
served-model identity, so the record does not invent one. Failed Claude 529 attempts,
timeouts, malformed contracts, and repaired reports earned no credit.

Before these rounds, the requested strict static skill-security scan returned mechanical
`FAIL` (12 critical, 69 high). A fresh Claude security auditor manually triaged every
category and returned CLEAN: presence-only credential checks never emit secret values;
network, cleanup, symlink, chmod, and installer hits were hardened or documented
capabilities and fixtures rather than exploitable findings.

---

## Previous C4 closure evidence: three clean rounds on one surface

The functional surface froze at commit `12394b4236804dd38a579ccd12f9f34c5f013367`.
All three counting reviews were fresh Claude Sonnet 5 sessions over Codex-authored code.
They reviewed the same `d91c025..12394b4` range; no code changed between them.

That record justified the version bump at the time, but it did not certify the corrected
Fable telemetry added before publication. The current three-round record above supersedes
it for the published 1.0 surface.

| Round | Permission | Focus | Structured evidence | Verdict |
|---|---|---|---|---|
| 1 | read-only | Full feedback and section-3 surface | `status:success`, `execution_status:success`, `report_ok:true`, `verdict:pass`, one attempt, no repair/resume/suspect | **CLEAN** |
| 2 | safe-edit, explicit no-edit; Git verified unchanged | Security, concurrency, negative paths | Same success/pass fields; one attempt, no repair/resume/suspect | **CLEAN** |
| 3 | safe-edit, explicit no-edit; Git verified unchanged | Windows/POSIX, public contracts, test strength | Same success/pass fields; one attempt, no repair/resume/suspect | **CLEAN** |

Non-counting attempts are part of the audit trail: one Opus stream-idle error, one
reviewer timeout, and two narrative CLEAN results whose malformed/repair-resumed
contracts left the structured verdict null. None was promoted into clean credit.

Round 1 exercised `--max-permission read-only` on the same HEAD and its envelope
reported `permission:read-only`. The separate C5 soak then ran with a real
`--gate-with quick-reviewer` plus `--max-permission safe-edit`. The gate recorded
`approved:true`; the one-attempt main review returned `verdict:pass`; Git remained
unchanged.

---

## Historical C4 evidence: why 1.0 was previously blocked

`fix/agy-readonly-containment` was reviewed cross-vendor (codex adjudicating Claude-written
code) after every fix batch. In that earlier campaign the clean counter never started.

| Round | Verdict | Findings | The one that mattered |
|---|---|---|---|
| 1 | BLOCK | 4 | The security fix withheld `--add-dir` at read-only and called it containment |
| 2 | BLOCK | 5 | Canary 5: a *declared* read-only agy agent read a secret and wrote a file by absolute path. The containment was never containment |
| 3 | BLOCK | 5 | `SUMMON_ALLOW_UNENFORCED_READONLY` is inherited by every child, so it silently waived nested gates and clamps |
| 4 | CONCERNS | 9 | `doctor --probe` handed agy full authority over the caller's tree -- introduced by round 3's own fix |
| 5 | BLOCK | 3 | The contract repair was never re-gated; on agy it ran at full authority |
| 6 | BLOCK | 2+5 | The gate was adjudicating the *original* request, not the one about to run |
| 7 | (self) | 4 | A repaired envelope reported `success` + `exit_code 1` + empty `result` -- found by dogfooding |
| 8 | CONCERNS | 3 | Two manifest runs on one results dir: parent A served parent B's answer |
| 9 | CONCERNS | 4 | The exit-tuple fix was two fields of five; the leak detector missed most of what the suite patches |
| 10 | CONCERNS | 6 | Telemetry adoption was conditional; `original_exit` could not describe a retry chain |

**What the shape of that table means.** The findings did not taper. What *changed* was their
subject: rounds 1-6 found defects in summon, rounds 7-10 increasingly found defects in *the
previous round's fix* and in the test harness itself. Eight of the ten rounds found a defect
introduced or left behind by the round before it.

Four CHANGELOG claims had to be corrected because they described the case that was fixed as
though it were the whole problem. Two documented guarantees were measurably false and were
rewritten against a repro: "agy read-only cannot see your repository", and "wasteful
duplicate, not corruption".

**The honest read at the time.** A 0.x line that absorbs this much correction per round is healthy. The
same rate under a 1.0 promise would mean shipping breaking fixes to a frozen contract. C4 is
not a formality to wait out; it is the difference between "we have not found the bugs" and
"there are fewer bugs to find". The three-round 0.19.2 record above is the later evidence
that finally distinguished those states.

**What moved C4:** the same surface was reviewed three times with nothing material found,
all three cross-vendor, plus real-world use of `--gate-with` and `--max-permission` by a
reviewer that did not write them. It was the sustained absence of findings, not another
feature.

## Why C4 was the one that blocked

C1-C3, C6 and C7 describe the state of the codebase. C4 describes the state of our
KNOWLEDGE of it, and it is the only criterion that cannot be satisfied by doing more
work today -- only by doing work and then finding nothing.

The earlier record was unambiguous: nearly every independent check found something real, and
several found defects in code that had just been declared fixed. The `--gate-with`
feature alone went through five rounds, each closing a bypass the previous round had not
looked for. That healthy process produced the signal to wait; the later three-round clean
record produced the signal to freeze.

Freezing earlier would have meant promising not to break something whose shape was still
being learned.

## The gate to 1.0.0

1. **ACHIEVED:** Three consecutive clean review rounds over the changed surface (no BLOCK, no
   CONCERN), at least two cross-vendor. Reviews must be of the SAME surface -- rotating to
   fresh code resets the count, because it measures knowledge of a stable thing.
2. **ACHIEVED:** A soak period with no security finding: `--gate-with` and `--max-permission` used
   in real dispatches, not only tests.
3. **ACHIEVED:** C4 returned to MET when (1) was satisfied on the corrected surface.

No new feature closed this gate. Independent eyes finding nothing material on the stable
surface did.

## After 1.0

The public surfaces named above now follow semantic versioning. Backward-compatible fields
and capabilities may be added in 1.x; a breaking CLI/report/agent-definition contract change
requires 2.0. The response shape keeps its independent `envelope: 1` version and bumps that
number on a breaking envelope-shape change.
