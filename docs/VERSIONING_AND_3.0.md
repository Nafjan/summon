# Summon 2.x -> 3.0 release and migration contract

Status: release contract defined; the current source is version 3.0.0 candidate and is
not GA until the machine gates in `tools/release_manifest.py` pass.

This document is intentionally operational. A version bump is not evidence of a
GA release. The release owner must generate evidence from a clean, immutable tree,
bind it to the source hash and Git commit, and retain the manifest with the
release artifact.

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

1. Run `summon doctor --json` and save the output as a local, reviewed artifact.
2. Run `python install.py --dry-run` and resolve foreign trees, duplicate copies,
   and unmanaged plugin drift. The installer must never overwrite a foreign tree.
3. Stop active Summon browser surfaces and release owners. Do not remove durable
   run directories or conversation rooms.
4. Install the candidate with `python install.py`; verify the ownership manifest,
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
that is not, by itself, a versioned rollback implementation. The `migration_rollback`
GA gate remains blocked until the isolated upgrade, failure injection, and rollback
tests are machine-recorded as `pass`.

The live-provider gate is separate from the provider-inert test matrix.  It only
passes when `tools/live_provider_gate.py` validates an explicitly reviewed,
redacted receipt supplied through `SUMMON_LIVE_PROVIDER_RECEIPT`; missing or
malformed evidence produces `blocked`, never an inferred success.

## Release decision

`3.0.0` GA requires all fixed suites and every named gate to be `pass`, a clean
source tree, converged managed installs, a complete migration/rollback evidence
packet, and an independently reviewed live-provider receipt if live deliberation is
claimed. If the live-provider or any other safety gate is unavailable, ship a
versioned 2.x maintenance release or a clearly labelled `3.0.0-preview.N`; do not
call it GA.
