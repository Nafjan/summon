# Contributing to summon

Thanks for helping! summon is deliberately small — one stdlib-Python dispatcher, no
daemon, no deps. Keep it that way.

## Dev setup

```bash
git clone https://github.com/Nafjan/summon && cd summon
python scripts/test_discovery.py    # dispatcher tests (mocked backends; no live CLIs needed)
python tests/test_install.py        # installer safety tests (isolated fake HOMEs)
python summon.py doctor             # see which real backends you have
```

Both suites are plain-assert, no pytest, and run on Python 3.10–3.13. CI runs them on
Ubuntu + Windows.

## Ground rules

- **Stdlib only.** No third-party runtime deps in the dispatcher. (The optional agy PTY
  wrapper is the one documented exception.)
- **The envelope is a contract.** If you change response fields, bump `ENVELOPE_VERSION`
  in `_executor.py` and update `CHANGELOG.md` + SKILL.md. Adding a field doesn't bump it.
- **Every change ships with a test.** For a backend, mock a server (api) or assert argv
  (subprocess). For the installer, use an isolated fake HOME.
- **Windows + POSIX both matter.** Guard OS-specific branches; ASCII-only console output.
- **Never log or echo secrets.** API keys come from env and must be redacted from errors
  (see `_apibackend._redact`).

## Common contributions

### Add a backend
One entry in the `BACKENDS` registry (`scripts/_builder.py`) — `subprocess` (build an
argv) or `api` (make the request). Full contract in
[references/adding-a-backend.md](references/adding-a-backend.md).

### Add an agent
A `.md` file in `agents/` with `run-agent` + `permission` frontmatter and the report
contract in the body — or `summon agent new NAME`. Include the "Untrusted content" guard.

### Add a provider
An OpenAI-compatible endpoint: add to `providers.json` (or a built-in in
`_apibackend.BUILTIN_PROVIDERS`). See `providers.json.example`.

## PR checklist

1. Both test suites green.
2. New behavior covered by a test.
3. Docs updated (SKILL.md / references / CHANGELOG).
4. No new runtime dependency.
5. Secrets redacted; OS branches guarded.

## Layout

```
summon.py            entry-point shim -> scripts/run_subagent.py
install.py           install the skill into host CLIs (ownership-safe)
scripts/             the dispatcher (run_subagent + _* helper modules)
agents/              starter agent definitions (.md)
references/          on-demand deep-dive docs (models, backends, fan-out, …)
docs/PROTOCOL.md     the orchestration playbook
examples/            manifest + json-schema examples
tests/               installer safety tests (dispatcher tests live in scripts/)
SKILL.md             the loaded skill instructions (agent-facing)
TERMS.md             terms-of-service guidance
```
