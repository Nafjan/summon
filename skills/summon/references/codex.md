# Codex-Specific Notes

Prevents two common Codex failures: sandbox denial and timeout mismatch.

## Permission

This script accesses `~/.codex/sessions` and external CLI binaries.
Use escalated sandbox permissions from the first run for this skill.

## Timeout

The host tool's own timeout must sit ABOVE the script's `--timeout`, never equal to it: the
script keeps running for a few seconds AFTER the child's deadline to kill the process tree and
serialize its result envelope. A host timeout equal to or below `--timeout` kills the script
mid-report, which is the "no output" failure in Common Errors below.

- Script arg: `--timeout 600000` (the child deadline)
- Tool param: `timeout_ms: 660000` (ABOVE it: ~60s of headroom for teardown + reporting)
- Rule: **tool timeout > `--timeout`**, never equal. Scale the margin with the run: a
  `--council` or `--manifest` fan-out runs many children in sequence, so it needs a much
  larger host ceiling. The dispatcher prints its worst-case wall-clock estimate to stderr
  before dispatching; set the host timeout above THAT.

## Sub-Agent Execution

When running a sub-agent, operate as a broker: carry one run from start to terminal state.

### Allowed actions

1. Validate the requested agent
2. Start `run_subagent.py`
3. Stay attached to that run until it finishes, times out, or fails
4. Return the sub-agent result, or the failure/timeout outcome

### If the user asks a question mid-run

Answer briefly, then return to waiting on the same run.

## Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `Operation not permitted (os error 1)` | Sandbox restriction | Use escalated permissions |
| No output, then a kill | Tool timeout at or below `--timeout` | Set the tool timeout ABOVE `--timeout` (add margin for teardown + reporting) |
| `permission denied` on session files | Sandbox restriction | Use escalated permissions |
