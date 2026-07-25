# Orchestration and rules of engagement

> Part of the **summon** skill. See the main SKILL.md for core usage, and
> [fan-out.md](fan-out.md) for manifest/council mechanics.

How to drive summon as the delegation layer of a multi-agent workflow: what it
proves, what it only suggests, and which defaults quietly cost you correctness.
Project-agnostic and host-agnostic. Adopt the parts you need; every section is
written so a single orchestrator (human or agent) can act on it without a
house style guide.

Semantics below were verified against summon **0.10.4**. Model ids and alias
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

### Aliases lag. Pin full ids.

Floating aliases (`opus`, `sonnet`, `haiku`) resolve to whatever the vendor CLI
currently points them at, which is routinely **behind** the newest release. As
of 2026-07-25, `--model opus` served `claude-opus-4-7` while `claude-opus-5`
was current, two releases back.

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

### Prompt-contained backends

Some backends cannot read the repository under `--cwd` and only see the prompt
text. Sending them a "review this repo" task yields confident output about code
they never read. Check backend capability before assigning repo-grounded work,
and prefer repo-capable backends for anything that must cite real files.

---

## 6. Council: when it earns its cost

A council is for decisions where independent perspectives change the outcome:
architecture, migration strategy, risk acceptance. It is not a rubber stamp,
and a synthesis is not consensus.

A council you can defend:

- **independent members** (vary the backend, section 4) and a synthesizer that
  did not vote (`--chairman`, plus `--chairman-fallback` for an independent
  non-member fallback);
- **read-only** member profiles;
- **`--quorum N`** so a thin council does not synthesize from one survivor;
- **bounded** `--member-timeout` / `--chair-timeout`;
- **durable output**: `--out` plus the run directory, so a kill does not lose
  paid work;
- recorded **dissent** and unresolved risks, not just the synthesis.

If quorum, independence, permissions, or receipts cannot be shown, call the
result an informal consultation rather than a governance artifact.

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
  and attaches `parsed` / `parse_ok` / `parse_errors`, with one corrective
  retry. Use it to make verdicts machine-checkable instead of prose an agent
  can drift from.
- **`--dry-run`** resolves the full dispatch, including model and permission
  flags, without spending anything. Cheapest way to catch a wrong model or an
  over-privileged profile.
- **`--resume`** continues a session instead of re-sending the whole definition.

---

## 7a. Gating a privileged dispatch (`--gate-with`)

A common ask is "make weaker models request permission from a stronger one, and
escalate to a human only when the strong model is unsure". Summon supports this,
but at the **dispatch** boundary rather than the tool-call boundary: once a child
CLI is running there is no cross-vendor hook into its individual actions, so the
gate adjudicates the *request* before anything runs.

```bash
run_subagent.py --agent implementer --prompt "..." --cwd <abs>                 --gate-with opus-review
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
