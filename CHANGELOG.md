# Changelog

The important, user-visible changes to summon. For the complete certification record,
regression notes, and test evidence, see the
[detailed engineering history](docs/ENGINEERING_CHANGELOG.md).

## [Unreleased]

**Kimi Code joins Summon, alongside a more dependable AGY path.**

### Highlights

- **Use Summon directly in Kimi Code:** `install.py --hosts kimi` installs the complete skill at `~/.kimi-code/skills/summon`, ready for Kimi’s native `/skill:summon` workflow.
- **Call Kimi from any supported host:** the new `kimi` backend uses Kimi’s native `stream-json` protocol, supports K3 and K2.7 model pins, and returns the same structured Summon envelope as the other CLI backends.
- **Safer Kimi one-shots:** each Kimi child receives a fresh, ACL-locked runtime profile with no inherited sessions, logs, user skills, or MCP configuration. Because Kimi’s prompt mode has no enforceable workspace sandbox, Summon refuses misleading `read-only` and `safe-edit` declarations; the explicit `kimi-worker` is for trusted isolated worktrees.
- **AGY reliability pass:** a built-in cross-platform stream proxy replaces fragile terminal scraping where AGY exposes JSONL, with stronger event parsing and model-alias handling.

### Also improved

- Hardened debug argument redaction and boundary-flag handling.
- Made worktree cleanup preserve uncommitted work.
- Clarified wrapper readiness in diagnostics.

Release certification remains separate: three clean cross-vendor rounds are still required
before a 1.0 certification claim.

## [0.19.2] - 2026-07-29

### Fixed

- Kept the executor's status authoritative through report reconciliation.
- Represented unreadable post-run artifact state as unknown instead of unchanged.
- Contained worktree cleanup by resolved path, including through symlinks.

## [0.19.1] - 2026-07-29

### Fixed

- Prevented `jobs wait` from losing a result published as its child exited.
- Stopped treating a failed artifact read as an observed file change.

## [0.19.0] - 2026-07-29

### Added

- Separated successful execution from a reviewing agent's `pass`, `conditional`, or
  `block` verdict.
- Added resume reporting, loose-file provenance, document-audit templates, and
  liveness-aware background jobs.

### Fixed

- Preserved worktrees when a denied run contained new or ambiguous work.
- Corrected background-job liveness and stale-state reporting.

## [0.18.0] - 2026-07-28

### Security

- Restricted Antigravity cleanup to structurally verified generated logs and rechecked
  file identity immediately before deletion.

### Added

- Added roster permission lint and agent-definition provenance to dry runs and diagnostics.

### Fixed

- Corrected gate timeout parsing, worktree teardown, and timeout-cause reporting.

## Earlier development releases

The earlier 0.x series established the core dispatcher, six backend families, agent
rosters, structured reports, councils, manifest swarms, background jobs, isolated
worktrees, permission gates, model discovery, and installation diagnostics. Its detailed
release history remains available, version by version, in the
[engineering archive](docs/ENGINEERING_CHANGELOG.md).
