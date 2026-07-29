# Versioning, and what summon 1.0.0 would mean

Status: **criteria defined; 1.0.0 NOT yet claimed.** Current line is 0.18.x.

> This line drifted (it said 0.14.x through four minor releases). If you are editing
> it, that is the eleventh instance of the defect this repo keeps hitting: a claim and
> the thing it describes, maintained separately.

This document exists because "is it 1.0 yet?" is otherwise answered by feel, and feel is
a poor instrument for a promise that binds every future release. Below: what 1.0.0 would
commit summon to, the criteria that decide it, and an honest scoring against them.

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

## Honest scoring, 2026-07-25 (v0.14.0)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| C1 | Surface enumerated and frozen | **MET** | 54 public flags, all documented; a structural test parses the argparse spec and fails the suite on any undocumented flag. `--job-file` is the only suppressed one. |
| C2 | Docs match behaviour | **MET (newly)** | Three false claims corrected today, each by measurement: the Opus alias pin, the guide's version stamp, and the agy `--cwd` claim. Guards now bind the version stamp and the default-chairman claim to their real values. |
| C3 | Green across the matrix | **MET** | CI green on ubuntu/windows x 3.10/3.13. Achieved today after five non-hermetic tests were fixed -- CI had been red on every push for a full day. |
| C4 | Defect discovery flattened | **NOT MET, decisively** | **Ten** consecutive cross-vendor rounds over ONE branch. Every round returned BLOCK or CONCERNS with reproducible findings. Zero clean rounds. See the evidence table below. |
| C5 | Security controls hold | **PARTIAL** | Four `--gate-with` bypasses were found and fixed after shipping (background, gate `args:`, verdict injection, retries), then a fifth (the schema-correction path). All fixes are mutation-verified. But they were found in sequence, each after the previous was declared complete. |
| C6 | The envelope never lies | **MET (newly)** | `gate.approved` could contradict `status: blocked` (fixed); `model.served` can be inferred and is now documented as such; the gate definition hash was null and is now populated. |
| C7 | Upgrade and drift handled | **MET** | `doctor` enumerates host installs, the running copy, and project-local copies; drift reports a stale copy by hash. |

**Verdict: 5 of 7 met, 1 partial, 1 not met. 1.0.0 is not yet warranted.**

---


## The C4 evidence: ten rounds over one branch

`fix/agy-readonly-containment` was reviewed cross-vendor (codex adjudicating Claude-written
code) after every fix batch. The bar is three consecutive clean rounds. The counter never
started.

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

**The honest read.** A 0.x line that absorbs this much correction per round is healthy. The
same rate under a 1.0 promise would mean shipping breaking fixes to a frozen contract. C4 is
not a formality to wait out; it is the difference between "we have not found the bugs" and
"there are fewer bugs to find", and this branch cannot yet tell those apart.

**What would actually move C4:** the same surface reviewed three times with nothing new
found, at least twice cross-vendor, plus real-world use of `--gate-with` and
`--max-permission` by someone who did not write them. Not more features. The *absence* of
findings, sustained.

## Why C4 is the one that blocks

C1-C3, C6 and C7 describe the state of the codebase. C4 describes the state of our
KNOWLEDGE of it, and it is the only criterion that cannot be satisfied by doing more
work today -- only by doing work and then finding nothing.

Today's record is unambiguous: nearly every independent check found something real, and
several found defects in code that had just been declared fixed. The `--gate-with`
feature alone went through five rounds, each closing a bypass the previous round had not
looked for. That is a healthy process producing an honest signal, and the signal says the
surface is still yielding.

Freezing a contract while the discovery rate is that high would mean promising not to
break something we are still learning the shape of.

## The gate to 1.0.0

1. **Three consecutive clean review rounds** over the changed surface (no BLOCK, no
   CONCERN), at least two cross-vendor. Reviews must be of the SAME surface -- rotating to
   fresh code resets the count, because it measures knowledge of a stable thing.
2. **A soak period with no security finding**: `--gate-with` and `--max-permission` used
   in real dispatches, not only tests.
3. **C5 upgraded to MET** by (1) and (2) together.

None of that requires new features. It requires the absence of new findings, which is a
thing only time and independent eyes can supply.

## Until then

0.x with a maintained CHANGELOG is the honest label. The envelope already carries its own
`envelope: 1` schema version, so consumers who need a stability guarantee today have one
at the layer that actually matters for integration -- the response shape -- without the
dispatcher pretending to a stability it has not yet demonstrated.
