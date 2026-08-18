# Local diagnostics and telemetry

Summon diagnostics are disabled by default. When you enable them, Summon writes a bounded,
allow-listed JSONL record to local storage. The diagnostic path does not contact a provider or
send data to Summon.

## What Summon records

Diagnostics can include bounded status, timing, backend, model-target, and error metadata. They
do not include:

- prompt or result text;
- raw provider output;
- credentials or access tokens; or
- absolute local paths.

Summon can store deterministic SHA-256 fingerprints for local correlation. A fingerprint is not
the original value, but a low-entropy value can still be linkable. Treat diagnostic files as
private.

## Enable, inspect, and clear diagnostics

```text
summon telemetry enable
summon telemetry status --json
summon telemetry clear
summon telemetry disable
```

`clear` removes stored events but does not disable collection. `disable` turns collection off.
The `SUMMON_TELEMETRY=0` environment setting disables collection for the current process and its
children without changing the saved preference.

The storage location is platform-specific and is not part of the public release contract. Use
`summon telemetry status --json` to inspect the local state instead of copying a path into a
report.

## Submit a report

`summon bug-report` creates a Markdown report for review. Summon does not upload that report.
After you inspect and sanitize it, submit that exact file with:

```text
summon bug-report --submit-github --from REVIEWED_REPORT.md
```

GitHub submission is a separate, explicit action. Do not paste an unrevised diagnostic file
into a ticket or public issue.

## Release and support evidence

Release evidence and managed-install inventories belong in a private release bundle. Public
changelogs and release notes should contain aggregate test results only. Do not publish local
paths, account identifiers, session identifiers, or diagnostic fingerprints.
