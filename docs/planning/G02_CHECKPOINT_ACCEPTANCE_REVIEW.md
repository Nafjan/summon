# G02 council checkpoint acceptance review

2026-09-08. Disposition: APPROVED / VERIFIED-FOR-3.5 at the provider-free
council preview scope. This review
preserves the approved advisory council design and does not authorize provider
calls, installation, commits or release. The conductor owns corrections and
shared integration. Native review is not a Fable endorsement.

## Confirmed findings

Final bounded closure: the lead verified all four integrated transport test
files match the approved packet, CI/release commands retain them, the unguarded
supplemental test is removed, and internal exact intake remains present. The
checkpoint module is byte-identical to accepted protocol source; AST comparison
shows only the separately reviewed prompt writer and dispatch functions changed
in council. Together with the 22-case public/protocol acceptance and seven-case
actual-process acceptance, this satisfies G02's explicit council-round context
requirement without changing budgets or promoting recommendations. This does
not add human messages to deliberation ballots or qualify other R12 adapters.
Earlier pending integration statements below are historical checkpoints.

Council transport correction is independently approved, 2026-09-08: seven
guarded actual-process cases pass in 5.921 seconds against current copied
source. Public pause/context-submit/continue reaches five actual synthetic
children with exact decoded context; direct limit and legacy external intake
controls remain separate. All 112 copied runtime files stayed stable during
the run; owned children/fixtures are cleaned. This closes the functional
intermediate-child gap identified below. Exact four-file canonical retention
and the coherent G02 acceptance check remain before whole-row closure; other
R12 adapter transport paths remain separate.

Shared integration checkpoint, 2026-09-08: the lead verified four runtime files
exactly match the accepted isolated correction, inspected the new actual
`run_subagent.main()` lifecycle and wrong-mode refusal, and independently ran
22 focused cases in 14.14 seconds. Guard hits and five-file source drift are
zero; the owned worker exited. Canonical compatibility-suite retention and both
format mappings are present; current inventory reference validators pass.
This accepts protocol/public-entry integration. Dispatch is synthetic at the
council seam; `R12_TRANSPORT_CLOSURE.md` records the remaining actual long-context
intermediate-child boundary. It is not proved by this suite.

Latest snapshot correction, 2026-09-08: independent review passes 21 focused
tests in 26.54 seconds without skips. Eight guarded actual worker invocations
satisfy nine explicit predicates, including old-history carry and both deadline
controls. After actual under-owner validation, an external replacement remains
preserved on disk, while all later prompts use the original validated content
and never the replacement. The validator returns checked envelope objects;
continuation retains and consumes that mapping. G02-SNAPSHOT-01 is corrected
on this isolated source. No source drift, guard hits or live handles remain.

The lead inspected the implementation, regression and independent predicates
and approves this correction for conductor integration. The current test file
still splits parser checks from direct council-handler execution. Retain the
already-required actual public-main pause/context/continue lifecycle and
wrong-mode refusal before whole-G02 closure, followed by source-bound integrated
retention. Earlier REVISE findings below are historical candidate evidence.

Latest independent correction checkpoint, 2026-09-08: the frozen 19-case suite
passes in 22.16 seconds. Actual retained old-producer/old-reader/new-reader
controls dispatch 5/0/0 stages. Pre-validation changes to round-one body and
attempt/contact facts refuse with zero dispatch. Fresh and continuation budgets
both retain 90 seconds after ten seconds of setup. These controls supersede
the corresponding earlier candidate failures below, within this tested scope.
The subsequently added twentieth candidate test is separate evidence.

### G02-SNAPSHOT-01: validated evidence is reread without its binding

The actual validator runs twice, including under ownership. A synthetic
replacement immediately after under-owner validation changes both result and
report summary. Continuation rereads that file, accepts it and dispatches round
two and chairman with the replacement in their prompts. This demonstrates
changed-content consumption, not merely acceptance of unused result text.

Source: `skills/summon/scripts/_council.py`, the call to
`_validate_between_round_ledger` and the later continuation round-one read loop.
Return and consume the same validated envelope snapshot throughout continuation;
do not perform an unchecked reread after validation. Preserve full evidence
binding and ownership checks. Retain this actual replacement case as a
regression, requiring either the original validated content or fail-closed
refusal, with no replacement reaching a downstream stage.

The review used a frozen copy with no runtime drift. The submitted test file
changed after copying; the 19-pass claim applies only to the copied baseline.
All guarded workers released ownership and exited, with zero forbidden-boundary
hits and cleaned fixtures. G02 remains REVISE pending this correction.

### G02-COMPAT-01: correction changes ordinary historical stage identity

The revised candidate adds `resolved_agents` unconditionally to `_exec_ctx`.
An independent guarded two-process compatibility fixture uses the actual
unchanged shared producer: the original two-round run dispatches five stages,
the original reader resumes it with zero dispatches, but the candidate reader
resumes the unchanged history by dispatching all five stages and returns
success. This is a new ordinary-resume regression in the correction, not a
failure of G02 context admission alone.

Preserve the previous ordinary stage-identity bytes. Additional G02 binding
facts belong to its explicit checkpoint/opt-in path, without weakening old
carry checks. Retain old-producer/new-reader evidence; candidate-produced
history read by the same candidate cannot prove backward compatibility.

### G02-CLI-01: intended public commands do not reach admission

The initial nine-test candidate checked rewritten argv prefixes but omitted
the actual parser and mode validator. Independent parser execution established:

- `council context submit RUN --context-file FILE` raised a `KeyError` for the
  missing mode hint instead of producing a bounded validation result.
- Public `--expect-generation` and `--operation-key` were not rewritten.
- Approved `--pause-after-round 1` was rejected by the boolean-only parser.

Source: `skills/summon/scripts/_cli.py`, `rewrite_subcommand`,
`unsupported_mode_flags`, `build_parser` and `MODE_HINTS`.

The conductor reports a correction and eleven passing candidate tests. That
correction is not independently accepted yet. Retain actual public argv,
parser, mode validation and main-handler tests, including wrong-mode refusal;
an argv-prefix assertion alone is insufficient.

### G02-DEADLINE-01: pre-admission time extends the original deadline

`run_council_continue` computes remaining milliseconds before loading agent
definitions, then passes that duration to `run_council`. The latter installs
its watchdog relative to a later monotonic start. Its under-owner expiry check
refuses already-expired work but does not remove time spent before that start.

The lead reproduced this through the actual continuation and registry boundary
with synthetic wall/monotonic clocks and a ten-second advance during the first
definition load. The original deadline had 90 seconds remaining; the installed
watchdog retained 100 seconds. No new dispatch occurred. The owned temporary
fixture was removed; no child process or provider was launched.

Source: `skills/summon/scripts/_council.py`, `run_council_continue`,
`run_council`, `_KillRegistry`.

Correction must derive a conservative monotonic deadline from the immutable
absolute deadline and account for all setup/ownership time. Check both expired
admission and still-positive elapsed setup. Fresh pause checkpoints must also
represent the original whole-run budget, not a deadline created after setup.

### G02-CARRY-01: altered round-one evidence causes a repeated stage

Independent synthetic cases changed either the persisted r1 input hash or its
status. Both continuations dispatched `r1-m1` again, then round two and the
chairman, returning success. `_validate_between_round_ledger` only checks a
stage object and string status; generic `run_member` falls back from rejected
carry to fresh execution. Bind the exact original stage evidence and prohibit
every round-one dispatch during G02 continuation. Missing or altered evidence
must refuse before any new stage, even if generic legacy resume could retry it.

### G02-SPEND-01: derived uncertainty is dropped at checkpoint admission

A synthetic originally failed, provider-contacted r1 produced per-member ledger
uncertainty true and checkpoint uncertainty false. Continuation ignored that
ledger flag, redispatched r1 and returned success. `_write_between_round_checkpoint`
and `_council_unknown_spend` must agree on the conservative aggregate, and
`_validate_between_round_ledger` must retain uncertain/missing facts. Do not
convert missing evidence to known spend or add retry authority through context.

### G02-ROSTER-01: under-owner comparison uses stale definition hashes

A synthetic definition replacement inside actual `acquire_owner` still allowed
round two and chairman execution. `run_council` compares cached pre-owner
`_agent_shas` with the checkpoint. Read and validate the actual definitions and
resolved execution inputs under ownership; comparing cached hashes is not a
fresh check. Preserve exact identity through the existing dispatch boundary.

## Evidence scope and next acceptance

Revised-candidate follow-up, 2026-09-08: seven guarded worker invocations
complete against frozen original/candidate copies without source drift. The
actual continuation-to-registry control now retains 90 seconds after ten
seconds of setup, closing the continuation conversion defect at this scope.
Fresh pause still records 100 seconds remaining while its live registry has
90 seconds: initialize that absolute deadline from the original run start.
Altered r1 result bytes and altered attempt/contact facts are still accepted,
followed by round two and chairman execution. Result text alone did not enter
the next prompt because the fixture's report summary took precedence; this
does not excuse accepting changed source evidence. Bind complete original
stage evidence, not only status and input hash, and consume the validated
snapshot. The ordinary compatibility regression above is also reproduced.
No broad suite was rerun; owned worker/runtime resources were cleaned.

The independent helper passed 27 cases in 2.50 seconds: eleven G02 tests after
the CLI revision and sixteen legacy reader tests. It then reproduced all four
undesired executions above. The lead inspected the complete reproducer and its
bounded results. Outer process/dispatch guards recorded zero unstubbed calls;
the environment used a synthetic home, empty PATH, disabled telemetry, disabled
pytest plugin autoload and no bytecode/cache. The 225-file baseline was stable
during this run, without implying stability across earlier CLI revisions.
Synthetic runtime directories were cleaned and no live handles remain.

Retain the new reproductions as regression tests against the corrected frozen
packet. Do not rerun unchanged broad suites merely to obtain another green
count. The public parser correction has candidate coverage; complete actual
main-handler coverage and independent integrated retention before closure.

Full G02 acceptance requires unchanged original budgets and authority, no
round-one rerun, immutable context/checkpoint bindings, genuine owner-held
validation, replay/interruption refusal and bounded public command behavior.
The existing legacy reader results remain useful within their original scope.
