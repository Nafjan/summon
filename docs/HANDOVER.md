# Handover: summon 0.18.0 → the road to 1.0.0

You are taking over `summon` from a Claude session that shipped 0.14.x → 0.18.0. You have
none of its context. This file is the whole transfer. Read it before touching anything.

**Repo:** `I:\Antigravity projects\summon` · branch `main` · clean · CI green
**Head:** `5506476` · dispatcher `__version__ = "0.18.0"`
**Suites:** `419/419` discovery, `22/22` install
**Open GitHub issues:** none

---

## 1. What summon is

A stdlib-only Python dispatcher that runs external AI CLIs (`claude`, `codex`,
`cursor-agent`, `gemini`, `agy`, `openai-compat`) as one-shot sub-agents and returns a
structured JSON envelope. Entry point `skills/summon/scripts/run_subagent.py`; the skill
contract is `skills/summon/SKILL.md`. It is consumed as a Claude Code / Codex *skill*, so
its docs are load-bearing: a wrong sentence in SKILL.md becomes a wrong control in someone
else's repo (that has already happened — see §5).

Run the suites:

```bash
python skills/summon/scripts/test_discovery.py
python tests/test_install.py
```

---

## 2. The bar you are being asked to clear

`docs/VERSIONING_AND_1.0_CRITERIA.md` defines C1–C7. Six are met or arguable. **One is
not, and it is the only one that matters:**

> **C4 — defect discovery has flattened.** Requires **three consecutive** cross-vendor
> adversarial rounds with **no BLOCK and no CONCERNS**.

**The counter is at 0 of 3, and it has never started.** Every round ever run has found
real, reproducible defects:

| Round | Verdict | Notes |
|---|---|---|
| Ten rounds over one branch (pre-0.15) | all BLOCK/CONCERNS | evidence table in the criteria doc |
| Certification round 1 | **BLOCK** | 5 findings incl. a cross-backend privilege bypass |
| Certification round 2 | CONCERNS | 6 findings |
| Codex review of 0.16.0 | CONCERNS | 3 real, incl. a security hole |
| **Certification round 3** (latest) | **BLOCK** | CRITICAL file-deletion + 5 confirmed |

The severity trend is downward — round 1 found a privilege bypass, round 3's CRITICAL was a
narrower file-identity bug in a two-hour-old feature — but **do not read a trend as a pass.**
Three clean rounds, or 1.0 is not earned.

**Do not claim a clean round you ran on your own work.** Cross-vendor means the reviewer is
not the author's vendor. A Codex-authored change gets reviewed by Claude, and vice versa.

---

## 3. Known outstanding work

Everything below is confirmed, with a repro. Nothing here is speculative.

### From certification round 3 (not yet fixed)

1. **`git worktree remove --force` destroys uncommitted work.**
   `skills/summon/scripts/run_subagent.py:1238`. On a gate denial the teardown force-removes
   the checkout and force-deletes the branch, so any file that appeared while the gate ran is
   gone, along with any new commit. A temp-repo repro confirmed both. Exploiting it needs a
   concurrent writer or a misbehaving gate, so it was rated WARNING/THEORETICAL — but the
   right shape is: preserve the tree whenever it is no longer pristine, and say so, rather
   than forcing.

2. **A regression test performs the restoration it claims to test.**
   `skills/summon/scripts/test_discovery.py:14388`
   (`test_v9_leak_detector_repairs_module_attributes_it_detects`). The test does the generic
   module-attribute repair *itself*, so deleting the real harness branch would not fail it.
   Its regression coverage is currently zero. This is the same wrong-layer defect described
   in §4 — in a test written *about* that defect.

### From the field report (deferred, not refused)

3. **A `codex` child could not run `rg`, while `rg` is on the parent's PATH.** It burned a
   full 480s timeout on this. Summon redirects `HOME`/`USERPROFILE` to an isolated
   per-invocation profile for auth hygiene, which is a plausible route to divergent PATH
   resolution — **but causation was not proven** and it may be pure codex-on-Windows
   behaviour. 0.18.0 makes the symptom loud (the timeout envelope now names the missing
   tool); the cause is still open. Worth an hour: it converts an available tool into a
   silent multi-minute stall.

4. **`model.served` is always `null` on agy.** agy emits no service telemetry, so summon can
   report what a session was *targeted at* but can never prove what answered. This is a real
   observability limit for anyone auditing which model did work. Not summon's bug; document
   it or find evidence agy does expose something.

---

## 4. The defect classes this repo actually has

This is the highest-value part of this handover. These are not hypotheses; each was hit
repeatedly and cost real time.

**A claim and the thing it describes, maintained separately, always drift.** Eleven
confirmed instances. A roster doc said four agents ran the `opus` alias while they pinned
`claude-opus-5`. An example agent pinned a model retired nine months earlier. `--gate-timeout`
promised "same grammar as `--timeout`" and had no parser at all. The criteria doc said
"0.14.x" through four minor releases. **The fix is never to correct the prose — it is to bind
the claim to its subject with a guard test.** There are ~12 such guards now; add one every
time you write a sentence about a file that lives somewhere else.

**Tests that pass one level above where the bug lives.** Hit at least eight times. A repair
test mocked the thing it was testing. A doctor test never called `render()`. The litter test's
negative cases were all caught by the *first* of two guards, so deleting the second left it
green. **Treat "my test passes" as unproven until you have mutated the code and watched it
fail.** Every fix in this repo's recent history ships with a mutation check; keep doing that.

**One input exercising two guards proves one.** Twice in a single day. If a check has two
conditions, write an input that only the second can reject.

**Comments state the correct rule while the code violates it.** The CRITICAL in round 3 was
a function whose docstring read *"deleting the wrong file in someone's repository is not
recoverable, so every check must pass"* — directly above an implementation that matched one
generic phrase anywhere in 4 KB. A stated principle is not a guard.

**Verification that lies.** A probe used `--cwd .`, which fails path validation *before* the
guard being tested, so a working guard looked broken. Another used a `hasattr` fallback and
reported a fix verified from the fallback's output. **Check that your probe reaches the code
you think it reaches.**

---

## 5. House rules (non-negotiable)

- **Commits:** author `Yousef AlNafjan <nsider@gmail.com>`, **no AI co-author trailer**.
  Message body explains *why*, including your own mistakes — the git log here is a real
  engineering record, not a changelog.
- **Stdlib only.** No new dependencies, ever.
- **Every change ships a test**, and the test must be mutation-verified.
- **Windows and POSIX both matter.** Primary dev is Windows 11 / PowerShell + Git Bash.
- **ASCII-only console output.** No em-dashes in prose.
- **Secrets are env-only and redacted** in every envelope and log.
- **Docs are code here.** SKILL.md and `references/` are consumed by other agents; treat a
  doc error as a defect with the same severity as a code error.
- Prefer editing an existing doc over adding a new one.

---

## 6. Security-relevant invariants — do not regress these

Each cost a review round to find. They are load-bearing.

- **`--gate-with` fails closed**, and the gate is forced read-only regardless of its own
  definition, so naming a write-capable approver cannot escalate.
- **A tier the backend cannot enforce is refused, not warned.** agy cannot enforce
  `read-only`; five canaries proved `--sandbox`, `--mode plan` and withholding `--add-dir`
  all fail to contain it. Fails closed unless explicitly waived.
- **`effective_permission(cli, permission)` is the truth for any capability census.** On agy,
  `safe-edit` runs the same full bypass as `yolo`. A census built from declared strings
  **understates** capability, and one that undercounts is worse than none because it is
  trusted. Report `effective`, never the declared string.
- **The agy litter sweep deletes a file in the caller's cwd.** It is the single most
  dangerous thing summon does. Identity is structural (glog preamble/record + machine record
  format) with a re-stat before unlink. **Do not loosen it.** A false negative leaves a file;
  a false positive is unrecoverable data loss.
- **`error_hint` is extracted from untrusted output.** Prompt echoes are excluded and markers
  are structural. Do not add loose-phrase matching.

---

## 7. What "done" looks like for this stint

1. Address the user's feedback (below).
2. Clear §3 items 1 and 2 — both are cheap and both are real.
3. Then run certification rounds **cross-vendor**, one at a time, fixing what each finds.
   Three consecutive clean rounds ⇒ 1.0.0 is earned. Anything less ⇒ it is not, and saying
   so is the job.

Report honestly. If a round finds nothing, say what you checked and how, so the clean
verdict is auditable — a clean round with no evidence is worth less than a BLOCK with a
repro.

---

## 8. The user's feedback for this stint

<!-- PASTE FEEDBACK HERE -->
