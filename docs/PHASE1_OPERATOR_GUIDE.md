# Phase 1 operator workflow

This guide joins Summon's Phase 1 controls into one workflow. Start provider-inert,
inspect the exact route and authority, and contact a provider only after the dry run
matches the intended seat, model, permission, data boundary, and spend boundary.
The authoritative current-version and rollback marker lives in
`docs/PHASE1_MIGRATION_ROLLBACK.md` so this workflow cannot silently drift from it.

Use `summon.cmd` on Windows and `summon` on POSIX. On Windows, put multiline prompts
or prompts containing command-shell metacharacters in a UTF-8 file and use
`--prompt-file`; the batch launcher rejects raw `--prompt` text it cannot transport
losslessly.

In every example, replace `<ABSOLUTE_PROJECT_DIR>` with the existing absolute path to
the project. Fleet and dispatch boundaries reject a relative working directory.

## 1. Inspect the installation and roster

```text
summon doctor --json
summon list --json
summon models --cli agy
```

These commands do not contact a model. `doctor` can probe installed client versions,
so a cold Windows start can take several seconds. Add `--probe` only when you intend a
live eligibility check.

## 2. Draft and explain a fleet lane

Create `review-task.txt`, then draft a lane without approving or launching it:

```text
summon fleet propose review --seats reviewer,architect \
  --permission-ceiling read-only --data-boundary local_sanitized \
  --allow-subscription --max-provider-contacts 1 \
  --max-billable-attempts 0 --max-parallel 1 \
  --cwd <ABSOLUTE_PROJECT_DIR> --out review-fleet.json --json
summon fleet validate review-fleet.json --cwd <ABSOLUTE_PROJECT_DIR> --json
summon fleet inspect review-fleet.json --json
summon fleet explain review-fleet.json review --cwd <ABSOLUTE_PROJECT_DIR> --json
```

`explain` reports candidates and losing constraints but selects nobody. A fleet file is
not authority. Record a short-lived approval only after reviewing the sealed plan:

```text
summon fleet approval status --json
summon fleet approval approve review-fleet.json review \
  --expires-in 1h --expect-generation GENERATION_FROM_STATUS \
  --cwd <ABSOLUTE_PROJECT_DIR> --json
```

Replace `GENERATION_FROM_STATUS` with the integer `generation` returned by the immediately
preceding status command. A new approval store starts at `0`; an existing store usually
does not.

Use the returned approval ID in one provider-inert launch preflight:

```text
summon dispatch --lane review --fleet-file review-fleet.json \
  --fleet-approval-id APPROVAL_ID --fleet-data-proof operator_attested \
  --prompt-file review-task.txt --cwd <ABSOLUTE_PROJECT_DIR> --dry-run --json
```

Only remove `--dry-run` when the effective route is correct. One approval permits one
foreground candidate and one physical attempt; it does not authorize retry, fallback,
login repair, background work, or authority expansion.

## 3. Inspect usage evidence without changing a route

```text
summon usage status --json
summon usage example --out usage-example.json --json
summon usage import --from usage-example.json --json
summon usage export --out usage-export.json --json
summon usage refresh --providers codex --allow-account-usage-read --dry-run --json
```

Imported and refreshed observations are advisory. Dimensions such as subscription
allowance, API balance, account credits, and rate limits are not interchangeable.
Exact model pins, permission ceilings, and explicit spend consent always win. Remove
`--dry-run` from `usage refresh` only to authorize the named account-status query; it
still does not run a model, log in, retry, or change routing.

## 4. Preview safe context compilation

Put typed payload blocks in a `summon.context-input/v1` JSON file, then compare the
safe compiler with the byte-preserving off switch:

```text
summon dispatch --agent reviewer --prompt-file review-task.txt \
  --cwd <ABSOLUTE_PROJECT_DIR> \
  --context-input-file context.json --context-profile safe --dry-run --json
summon dispatch --agent reviewer --prompt-file review-task.txt \
  --cwd <ABSOLUTE_PROJECT_DIR> \
  --context-input-file context.json --context-profile off --dry-run --json
```

The safe profile applies only mechanical, target-resolvable transformations and reports
hashes, action counts, and before/after estimates. The off profile appends the exact
source serialization. Omitting context flags leaves the legacy prompt path unchanged.
Accepted stale deliberation context remains visibly stale, is bound to one run namespace,
and never gains routing authority.

## 5. Run and supervise a long job

```text
summon dispatch --agent reviewer --prompt-file review-task.txt \
  --cwd <ABSOLUTE_PROJECT_DIR> \
  --background --adaptive-timeout --timeout 10m --max-runtime 4h --json
summon jobs status JOB_ID --json
summon jobs extend JOB_ID --duration 30m --json
summon jobs steer JOB_ID --message "Check the failing integration test first" --json
summon jobs cancel JOB_ID --json
```

The adaptive timeout is a progress checkpoint, not permission to run forever. Only
trusted provider events renew liveness. An authenticated `extend` command queued before
the current hard deadline advances both the live attempt's checkpoint and hard deadline,
up to the seven-day job cap; `jobs status` exposes the applied control generation in the
authenticated heartbeat. `steer` queues authenticated guidance for an
eligible successor; it is not claimed as live mid-turn injection. After a terminal
eligible Claude subprocess job, create one explicit successor with:

```text
summon jobs resume JOB_ID --message "Continue from the verified checkpoint" --json
```

Resume preserves the source authority ceiling, requires fresh spend consent, and does
not retry ambiguous provider contact.

## 6. Export a portable result

Keep private execution envelopes outside public repositories. Derive a redacted result:

```text
summon result project --kind dispatch --from private-envelope.json \
  --repo-root <ABSOLUTE_PROJECT_DIR> --out portable-result.json --json
summon result validate portable-result.json --json
summon result consume portable-result.json --adapter reference --json
python skills/summon/examples/phase1/consume_portable_result.py \
  portable-result.json
```

The standalone example imports no Summon modules and checks the experimental digest and
cross-field invariants. Neither consumer grants dispatch, routing, native-subagent, or
review authority. The private envelope remains authoritative, and the portable schema
stays experimental until independently maintained consumers adopt it.

## 7. Roll back safely

Revoke unused fleet approvals, stop or cancel owned jobs, retain private evidence, and
restore the prior reviewed managed artifact with its installer. Do not delete durable
job, room, approval, usage, or telemetry state to make a rollback appear clean. Re-run
`doctor --json` and verify the restored version and managed digest. See
[Phase 1 migration and rollback](PHASE1_MIGRATION_ROLLBACK.md).
