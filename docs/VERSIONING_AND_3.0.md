# Summon 2.x -> 3.0 release and migration contract

Status: 3.0.0 GA and 3.1 preview release contract. The release manifest is generated from the clean
immutable release commit and records aggregate results for the fixed suites, eight gates,
the migration packet, managed installs, and the redacted live-provider receipt. Keep the
manifest, host inventory, and per-run evidence in the private release bundle.

This document is intentionally operational. A version bump is not evidence of a
GA release. The release owner must generate evidence from a clean, immutable tree,
bind it to the source hash and Git commit, and retain the manifest with the
release artifact.

## 3.1 preview boundary

The `3.1.0-preview.1` artifact keeps `envelope: 1` and adds only optional evidence
fields. It is a preview of the evidence-integrity slice, not a certification of the
deliberate lifecycle, swarm worker conformance, chat lifecycle, custom-agent validation,
or live-provider gates described in `docs/SUMMON_3.1_PLAN.md`. A preview tag may be
published only from a clean commit with fresh source-bound suite evidence. Do not call
the artifact GA or infer live-provider behavior from an editorial model label.

## Compatibility boundary

- The JSON response **envelope remains `envelope: 1`** for the 2.x → 3.0
  transition. Additive fields are allowed; changing the meaning or type of an
  existing field requires an envelope migration and a separate compatibility
  decision.
- Durable journal schemas are read-before-write. A 3.0 reader must replay every
  supported 2.x prefix or return a typed `unsupported_schema`/`recovery_required`
  result without constructing a provider.
- 3.0 writes a versioned receipt, checkpoint, and install ownership manifest.
  Unknown future versions are rejected before filesystem or provider mutation.
- Telemetry remains opt-in, local, bounded JSONL. An upgrade preserves an explicit
  opt-in, but a clean install never enables telemetry silently. `SUMMON_TELEMETRY=0`
  wins for the current process.

## Upgrade procedure

1. Run `summon doctor --json` and save the output as a private, reviewed artifact.
2. Run `python install.py --dry-run` and resolve foreign trees, duplicate copies,
   and unmanaged plugin drift. The installer must never overwrite a foreign tree.
3. Stop active Summon browser surfaces and release owners. Do not remove durable
   run directories or conversation rooms.
4. Install the release artifact with `python install.py`; verify the ownership manifest,
   source hash, version, and companion `/council` and `/deliberate` files for every
   managed host.
5. Run the fixed release registry and bind its output:

   ```text
   python tools/release_gates.py --require-clean \
     --output "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/summon-release-evidence.json"
   python tools/release_manifest.py \
     --evidence-file "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/summon-release-evidence.json" \
     --output "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/summon-release-manifest.json" \
     --expected-version 3.0.0 --check
   ```

   The runner also writes one immutable `gate.<name>.json` artifact for each
   named GA gate beside the evidence file.  The evidence packet binds each
   artifact to the source payload hash, Git commit, fixed command registry, and
   producer hash.  A missing, edited, blocked, or stale artifact is a release
   failure; `--test`/`--gate` compatibility flags are never sufficient for GA.

6. Keep the previous release artifact until the manifest check, smoke tests, and
   one real operator run have completed. A failed or incomplete migration is a
   release failure, not a reason to infer compatibility.

## Rollback procedure

Rollback is explicit and must be tested in an isolated host home before GA:

1. Stop active surfaces and release the current owner.
2. Preserve `release-manifest.json`, evidence, logs, and the durable run/room
   directories for forensic replay.
3. Restore the previously verified owned skill tree from the retained release
   artifact, or run the installer for that exact version. Never copy over a
   foreign or user-owned directory.
4. Re-run `summon doctor --json`, verify the restored source hash/version, and
   replay existing journals read-only. A newer journal prefix must be reported as
   unsupported/recovery-required rather than silently downgraded.
5. Record the rollback reason and the restored manifest; do not delete `.previous`
   or other recovery material until the operator confirms the rollback is complete.

The current installer has bounded atomic swap recovery for an interrupted install;
the isolated upgrade, failure-injection, and rollback tests are machine-recorded by
the `migration_rollback` gate. Keep the previous release artifact until the operator
has completed a real upgrade and smoke run.

The live-provider gate is separate from the provider-inert test matrix. It only
passes when `tools/live_provider_gate.py` validates an explicitly reviewed,
redacted schema-2 receipt supplied through `SUMMON_LIVE_PROVIDER_RECEIPT`. The
receipt must bind one normal decision plus cancel and deadline safety cases,
owner/deadline/cancel fences, kill-switch behavior, no fallback/retry, clean
process teardown, and an `account_evidence_sha256` digest for reviewer-held
account proof. Raw account identifiers, credentials, prompts, and output never
belong in the release packet. Missing, stale, or malformed evidence produces
`blocked`, never an inferred success.

For the Claude route, generate the redacted digest locally before and after the
pilot and require the two values to match:

```text
python tools/account_evidence.py --profile default --config-dir "%USERPROFILE%\\.claude"
```

The reviewer copies only the resulting `account_evidence_sha256` into the receipt;
the profile metadata itself remains private and outside the release artifact.

## Release decision

The 3.0.0 candidate has a source-bound, reviewed Claude receipt. The fixed registry records
all 17 suites and all eight gates as `pass`, with a clean source tree and converged managed
installs. The Claude receipt closes that provider gate for 3.0.0. It does not certify Fable,
Gemini, or any other route; each provider must earn an independent receipt. Keep the receipt,
manifest, host inventory, and any account-evidence digest in the private release bundle.
