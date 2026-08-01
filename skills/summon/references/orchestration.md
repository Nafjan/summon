# Orchestration and rules of engagement

> Part of the **summon** skill. See the main SKILL.md for core usage, and
> [fan-out.md](fan-out.md) for manifest/council mechanics.

How to drive summon as the delegation layer of a multi-agent workflow: what it
proves, what it only suggests, and which defaults quietly cost you correctness.
Project-agnostic and host-agnostic. Adopt the parts you need; every section is
written so a single orchestrator (human or agent) can act on it without a
house style guide.

Semantics below were verified against summon **1.1.0**. Model ids and alias
behaviour are volatile: re-check with `doctor`, `list`, `models`, and
`--dry-run` before a run you care about.

---

## 1. The one rule that matters

**A role name is not evidence. A model name is not evidence. Only the envelope
is evidence.**

Every other rule here follows from that. An agent that reports `STATUS: DONE`
has told you its opinion of its own work. The envelope tells you what actually
ran: which definition (by content hash), under which permissions, on which
backend, and whether a model demonstrably produced output. Orchestrate off the
envelope and the rest of the framework is mostly bookkeeping.

---

## 2. The orchestration loop

The shape that holds up across projects:

1. **Classify** the change by blast radius, not diff size. A one-line auth or
   migration change is high risk; a 400-line docs change usually is not.
2. **Decompose** into units with **non-overlapping file ownership**. Overlap is
   what turns parallelism into lost work.
3. **Dispatch** implementers, one writer per surface, each in its own worktree.
4. **Verify** by reading the diff and running the checks yourself. Never accept
   a self-report as proof.
5. **Review** cross-vendor (section 4) before anything is called done.
6. **Integrate** on the combined tree and re-run the gates there. Green in an
   isolated worktree does not prove green after merge.

Steps 4 and 6 are the ones under time pressure people drop, and they are the
two that actually catch problems.

---

## 3. Evidence: strong, weak, and absent

Record these from every dispatch that matters:

| Field | What it proves |
|---|---|
| `request_sha256` | The full request identity. A cached `--out` envelope with a different hash is not an answer to this request. |
| `agent_def.sha256` | Which definition bytes ran. Detects an edited or swapped agent. |
| `prompt_sha256` | Which prompt ran. |
| `permission` / `permission_flags` | The privilege actually granted (see section 5). |
| `status`, `exit_code` | `success` / `partial` / `blocked` / `error`; 124 = timeout, 127 = CLI missing. |
| `report_ok`, `suspect` | Whether the contract block parsed. `status:success` with `report_ok:false` sets `suspect` -- re-dispatch rather than trust. |
| `model.requested` / `.targeted` / `.served` | See below. These are three different claims. |
| `billing.source` | `subscription` / `api` / `credit`. Advisory; the vendor's billing is truth. |
| `gate` | Present when `--gate-with` ran: the verdict, the gate's own definition hash and model. A gated dispatch that reports NO gate field is indistinguishable from an ungated one, so treat its absence as unapproved. |

### The `model.served` trap

`served` is set from **either** a terminal event in which the backend names the
model, **or** the weaker inference `output_tokens > 0 AND targeted is known`.
When neither holds, `served` is `null`.

That second path means **`served` can simply echo what you asked for**. It
confirms that output was produced, not that the named model produced it. This
matters most on gateway backends, where you asked a router for a model and the
router is the only party who knows what it dispatched.

Telemetry coverage differs by backend: claude and codex expose session, usage
and cost; openai-compat returns the API's `usage`; agy exposes none. Anything
not in that list should be treated as unverified rather than assumed.

Practical rule: **if `served` is absent, say so.** Never promote `requested` or
`targeted` into a served claim. A governance framework that mandates "record
`model.served`" without this caveat will collect a field that sometimes proves
nothing.

---

## 4. Cross-vendor review, and why "cross-model" is not enough

Route work so that **no agent's output is reviewed by its own vendor**. A model
reviewing its own output shares its blind spots.

The subtler failure is **shared route**. Several backends can front the same
set of models: you can reach Claude, GPT, Gemini and Grok models through one
gateway CLI. A council whose members are four different *models* reached through
one *backend* has model diversity but not independence. It shares:

- one transport and one auth path (one outage takes all members down),
- one billing path,
- one remapping surface (a gateway-side model change silently moves everyone).

If independence is the point, vary the **backend**, not just the `--model`
string. Reserve single-gateway fan-out for throughput, where a shared failure
domain is acceptable.

These are three separate axes, and conflating them is how a council looks
independent while sharing a single point of failure:

| Axis | What varying it buys | What it does NOT buy |
|---|---|---|
| **Model provider** (Anthropic / OpenAI / Google / xAI) | Different training, different blind spots | Nothing about availability: one gateway can front all of them |
| **Backend** (`claude` / `codex` / `cursor-agent` / `agy`) | Separate transport, auth, billing and remap surface | Not automatically a different model provider: a gateway reaches many |
| **Account** | Independent quota and rate limits | Nothing about model or transport |

A review is cross-vendor when the **model provider** differs from the author's.
A council is fault-tolerant when the **backend** differs. Ask for both.

### Aliases lag. Pin full ids.

Floating aliases (`opus`, `sonnet`, `haiku`) resolve to whatever the vendor CLI
currently points them at, which is routinely **behind** the newest release. A
measurement taken 2026-07-25 saw `--model opus` serve `claude-opus-4-7` while
`claude-opus-5` was current -- but treat that as an ILLUSTRATION, not a fact you
can rely on today.

**Neither `models` nor `--dry-run` can verify this for you.** `models` lists the
aliases; `--dry-run` shows what summon will TARGET. Neither observes what the
vendor's alias actually expands to, or what served the request. The only way to
know is a live canary dispatch whose terminal telemetry names the model -- which
means a backend that reports one (claude or codex; see section 3). Anywhere else,
record the expansion as unverified rather than assuming it.

Pin full version ids in agent definitions and re-check them on a schedule. An
alias in a governance-critical reviewer definition is a silent downgrade.

---

## 5. Permissions and isolation

**Least privilege, and verify the mapping.** Permission labels are *not*
equivalent across backends. Summon normalizes the names, not the semantics.

The trap worth knowing: **agy has no workspace-write tier, so `safe-edit` maps
to the same full bypass as `yolo`.** Summon surfaces a warning, but a policy
that says "reviewers get `safe-edit`" grants a full bypass on that backend.
Confirm the real flags with `--dry-run` before trusting any permission label.

Defaults that hold up:

- Reviewers, councils, planners: **read-only**.
- Exactly **one** implementer per change set gets write access.
- Full bypass is not a routine tier. If a run needs it, isolate it, keep
  credentials and sensitive data out of it, and record why.

### `--max-permission`: clamp down, never up

`--max-permission {read-only,safe-edit}` caps a dispatch at that tier. It is a
CLAMP, not an override: an agent declaring `read-only` stays read-only under a
`safe-edit` ceiling. (The helper also keeps the declared tier when handed an
unrecognised ceiling, but the CLI rejects those at parse time, so that is an
internal safety property rather than something you can invoke.) Summon has
no general `--permission` flag on purpose -- one would let any caller hand any
agent full bypass, a larger hole than any it would close.

It also **drops the agent's `args:` passthrough**, because `extra_args` are
appended AFTER the permission flags and a definition carrying
`--dangerously-skip-permissions` would otherwise defeat the clamp entirely.

Use it when the ORCHESTRATOR knows an agent needs less authority than its
definition grants: a reviewer reused as a deliberator, a general-purpose agent
pointed at a read-only task. It is the honest way to reuse a capable definition
without editing it.

### Worktrees: know what summon does and does not isolate

`--worktree` isolates a **single dispatch**. Council and manifest modes do
**not** provide per-member worktrees. For parallel *editing* agents you must
dispatch manually, one `--worktree` each.

For repository deliberation (read-only council over a codebase):

- prepare a clean worktree at the intended baseline and pass it as `--cwd`;
- use read-only member definitions;
- never point concurrent members at a dirty active workspace.

Never reset, stash, or discard someone else's dirty worktree. Isolate new work
and report what you found.

### Backends that do not stand in `--cwd`

A backend can have full file tools and still be useless for repo work, because it
is not WHERE you think it is. That is a distinct failure from having no file
access, and it is worth checking for explicitly.

Summon hit exactly this with `agy`: it isolates that backend's `HOME` for auth
hygiene, and agy then resolved relative paths against a scratch dir inside the
isolated profile. A canary showed a relative lookup failing while the SAME file
at an absolute path read fine -- proof the tools worked and only the location was
wrong. Summon now passes `--add-dir <cwd>` and relative paths work (0.13.9).

That fix then produced the more important lesson, and it took two wrong answers to get
to it. Handing agy the workspace made it repo-capable at every permission level, including
`read-only`, which agy does not enforce. The first correction withheld the workspace at
read-only and called that containment. A later canary demolished it: a **declared**
read-only agy agent, given absolute paths, read a secret file back verbatim and created
another, both confirmed on disk. Withholding `--add-dir` only breaks RELATIVE paths. It was
never a boundary -- it was a boundary-shaped comment.

So summon now **fails closed**: a read-only agy dispatch is refused, with an explicit
opt-in for anyone who knowingly wants it on a throwaway checkout.

**A permission tier you cannot enforce is worse than no tier, because callers act on it.**
Offer the tier only where the backend honours it; elsewhere refuse, and say why. And when
you write a mitigation, test the thing an attacker would actually do -- the first canary
asked for a relative path and "passed", which is how a non-fix survived review, a release,
and its own changelog entry.

The failure mode this creates is nastier than a refusal: an agent that cannot
find your files may answer from the prompt alone and sound confident about code
it never opened. Do not infer capability from a plausible-sounding answer.

**Canary any unfamiliar backend before trusting it with repo-grounded work.** One
dispatch: put a unique token in a file, ask the agent to read it back BOTH by
relative name and by absolute path. The two results tell you which world you are
in -- no file access at all, file access from the wrong place (fixable by telling
the backend where the repo is), or working normally. This costs one call and
replaces a guess with a fact; summon's own documentation asserted the wrong
answer here for months because nobody spent that call.

---

## 6. Council: when it earns its cost

A council is for decisions where independent perspectives change the outcome:
architecture, migration strategy, risk acceptance. It is not a rubber stamp,
and a synthesis is not consensus.

A council you can defend:

- **independent members** (vary the backend, section 4) and a synthesizer that
  did not vote (`--chairman`, plus `--chairman-fallback` for an independent
  non-member fallback). Chairmen are dispatched **read-only automatically**: a
  chairman reads member positions and writes prose, so it never needs repository
  access, and a council question that induced it to write would be a change
  nobody authorised. Members are NOT clamped, since forming a position can
  legitimately require running things;
- **read-only** member profiles;
- **`--quorum N`** so a thin council does not synthesize from one survivor;
- **bounded** `--member-timeout` / `--chair-timeout`, and
  **`--overall-timeout`** as a HARD wall-clock budget for the whole deliberation.
  Per-stage timeouts do not bound the total: N members at T each can still run
  N x T. On breach summon process-tree-kills the in-flight members and emits a
  PARTIAL council envelope, which is the difference between a bounded result and
  your host tool killing the dispatcher with nothing to show;
- **`--min-successful-members N`** to stop waiting for stragglers once N members
  have succeeded in the final round. Distinct from `--quorum`, which decides
  whether synthesis runs at all: this one decides when to STOP WAITING. Set both
  and a slow member cannot hold the deliberation hostage;
- **durable output**: `--out` plus the run directory, so a kill does not lose
  paid work;
- recorded **dissent** and unresolved risks, not just the synthesis.

If quorum, independence, permissions, or receipts cannot be shown, call the
result an informal consultation rather than a governance artifact.

**Check what your chair costs.** The default chairman is `architect` (Opus 5).
It used to be `fable`, which meant every council omitting `--chairman` routed
SYNTHESIS -- the single most expensive stage -- to a credit-billed model at
roughly twice the price, without it being the stronger model for that work. If
your project pins its own chairman, check which model it actually resolves to;
`--dry-run` will tell you in one call.

**Budget the wall clock.** Members run at most 3 concurrent per backend, so
roughly `rounds x waves x (timeout + 60s) + (timeout + 60s)`, where
`waves = ceil(same-backend members / 3)`. Set the host tool's timeout **above**
summon's `--timeout`, or the host kills the dispatcher before it can report.

---

## 7. Efficiency: stop re-paying for work

Most orchestration waste is re-running work that already succeeded.

- **`--out FILE`** writes the envelope atomically and **skips** the run if the
  file already holds a `status: success` envelope. Failures re-run. This is
  free swarm resume: give every job an `--out` and a rerun costs only what
  actually failed.
- **`council resume <run-id>`** re-runs only missing, failed, or input-changed
  stages and carries the rest forward. Prefer it over re-running an expensive
  council from scratch. `council status <run-id>` inspects state read-only.
- **`--background`** returns a job handle immediately; poll with
  `jobs list` / `jobs status ID` / `jobs wait ID`. A result is trusted only when
  its nonce matches the launch record.
- **`--retries N`** retries `error`/`partial` with backoff. `blocked` is never
  retried, because its cause is structural.
- **`--json-schema FILE`** validates the agent's final JSON against a contract
  and attaches `parsed` / `parse_ok` / `parse_errors`. Use it to make verdicts
  machine-checkable instead of prose an agent can drift from.
  Validation always runs; the ONE corrective retry does not. The correction is a
  `--resume` follow-up, so it only happens on a backend with a resume lane
  (claude, codex, cursor by session id; agy by profile). Gemini rejects resume
  and openai-compat is stateless, so there the verdict you get is the first one:
  `parse_ok: false` is final rather than a first attempt.
- **`--dry-run`** resolves the full dispatch, including model and permission
  flags, without spending anything. Cheapest way to catch a wrong model or an
  over-privileged profile.
- **`doctor --cwd <repo>`** enumerates every summon copy it can find and flags
  drift: the per-host installs, and any PROJECT-LOCAL copy at
  `<repo>/.agents/skills/summon`. That last one matters because `install.py`
  only refreshes host installs, so a vendored project copy is never updated by a
  normal install and rots silently -- which is how a copy ran months-old code
  behind a hand-edited version string, and how another kept a fixed bug after
  every host was patched. It is reported, never written: summon surfaces a
  deliberately vendored copy rather than overwriting it.
- **`--resume`** continues a session instead of re-sending the whole definition.

---

## 7a. Gating a privileged dispatch (`--gate-with`)

A common ask is "make weaker models request permission from a stronger one, and
escalate to a human only when the strong model is unsure". Summon supports this,
but at the **dispatch** boundary rather than the tool-call boundary: once a child
CLI is running there is no cross-vendor hook into its individual actions, so the
gate adjudicates the *request* before anything runs.

```bash
run_subagent.py --agent implementer --prompt "..." --cwd <abs> \
                --gate-with opus-review
```

The gate agent is dispatched **forced read-only**, regardless of what its own
definition declares. That is deliberate: if a gate could inherit write access,
naming a full-bypass profile as your own approver would turn the approval step
itself into privilege escalation.

It **fails closed**. Only an explicit `VERDICT: APPROVE` from a gate run that
completed lets the dispatch proceed. A denial, an error, a timeout, a blocked
gate, or an unparseable verdict all refuse, because "could not answer" must never
read as "approved". `VERDICT: UNCERTAIN` refuses *and* sets
`requires_human_review: true`, which is the escalation path: the gate declares it
cannot tell, and a person decides.

The gate's ruling, its model evidence, and its definition hash land in the
envelope's `gate` field, so a skipped or forged approval is detectable afterwards.

**Every dispatch path is gated, which took six attempts to get right.** This is
worth stating plainly because it is the failure mode most likely to recur in any
system you build on top. A gate authorises ONE execution, and summon turned out
to have several ways to execute:

- the initial dispatch;
- each `--retries` attempt (a retry is another execution, not a continuation);
- the `--json-schema` corrective follow-up, which re-dispatches with the ORIGINAL
  permission and so was a second write-capable run that no gate had approved;
- `--background`, whose detached child argv is rebuilt field by field, so the gate
  flag was simply absent and the whole dispatch ran ungated;
- the **contract auto-repair** resume, which is also a second execution -- and on a
  backend that cannot enforce read-only it keeps the task's own authority, so a
  gated `agy` task with a malformed report bought a full-bypass run nobody approved;
- and then, having gated all five, the gate itself was **adjudicating the wrong
  request**: it built its prompt from the caller's original arguments rather than
  from the invocation about to be dispatched, so every re-gate above asked about the
  FIRST task while a different one ran. An approval for a request nobody executes is
  worse than no gate, because it looks like coverage.

Each was found separately, and after each one the invariant looked satisfied. The
lesson generalises twice over: when you close a privilege hole, **enumerate the
execution paths rather than fixing the one in front of you** -- and then check that
the check itself is looking at the thing it is about to authorise. A refused correction records
`gate_correction_refused` instead of overwriting `gate`, because the original
approval authorised work that genuinely completed -- rewriting it would misreport
finished work as denied.

The gate's own `args:` are dropped for the same reason: an agent's `extra_args`
are appended AFTER the permission flags, so a gate definition carrying
`--dangerously-skip-permissions` would have defeated its own forced read-only.
Verdict parsing is line-anchored, embedded prompts are defanged, and two DISTINCT
verdicts refuse as ambiguous -- otherwise a crafted prompt could inject the
approval it wanted.

**What this does and does not give you.** No sub-agent can escalate itself: the
permission comes from the definition plus dispatch flags, which only the
orchestrator sets. But summon has no persistent policy engine, so it cannot
*force* you to use a gate. Keep every non-elite definition read-only and reserve
write-capable definitions for gated dispatches; then the only way to skip the
control is an orchestrator that deliberately bypasses it, and the receipts make
that visible.

Cost stays bounded if you gate by risk tier rather than universally.

---

## 8. Host adapters

The framework above is host-agnostic. Only the *in-IDE* delegation mechanism
differs. Keep host specifics in one table rather than scattered through policy:

| Host | Native in-IDE subagent | Notes |
|---|---|---|
| Claude Code | Task/Agent tool | Native agents share the session's repo access. |
| Cursor | Native agent/composer | Also usable as a summon backend (`cursor-agent`). |
| Codex CLI | none (dispatch via summon) | See [codex.md](codex.md) for permission/timeout setup. |
| Gemini CLI / Antigravity | host-native research subagents | Prefer these for repo-grounded Gemini work when the summon route is prompt-contained. |
| Any other | summon only | Everything here still applies. |

Two host-independent rules:

1. **Use the native subagent when the work needs the IDE's own repo context**
   (file search, editor state) and summon when you need a *different vendor*,
   an isolated permission or worktree boundary, or a structured envelope.
2. **The host's own tool timeout must exceed summon's `--timeout`**, with
   overhead to spare, or you lose the report.

---

## 9. Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Trusting `STATUS: DONE` | Self-report, not verification. Read the diff; run the checks. |
| Reporting a model you did not confirm | `served` may be inferred or absent. Say "unverified". |
| Cross-model but same-backend "independence" | Shared transport, auth, billing, and remap surface. |
| Aliases in reviewer definitions | Silent downgrade when the alias lags. |
| `safe-edit` assumed to mean workspace-write | On agy it is a full bypass. |
| Councils over a dirty workspace | Members read inconsistent state; results are unreproducible. |
| Parallel writers on overlapping files | Lost work. Partition by file ownership. |
| Re-running a whole council after a kill | Use `--out` and `council resume`. |
| Presenting an unreviewed draft as a basis for a decision | Approval of an unreviewed artifact launders it without traversing the control. |
| Asking the human which command to run | Mechanism is the orchestrator's job. Choose, state the choice, execute. |

---

## 10. Preflight

Before a run that matters:

```bash
run_subagent.py doctor                 # backends usable? install drift?
run_subagent.py --list                 # roster resolves?
run_subagent.py models --cli <backend> # what is invocable
run_subagent.py --agent X --prompt "..." --cwd <abs> --dry-run
```

Confirm from the dry run: the **backend**, the **effective model** (a full id,
not an alias), and the **permission flags**. Then dispatch, and record the
evidence from section 3.
