# Adding a backend

Every backend summon can dispatch to is registered in **one place** — the `BACKENDS`
dict in `scripts/_builder.py`. There are two kinds; pick the one that fits.

## The registry

```python
BACKENDS = {
    "claude":       {"kind": "subprocess", "build": _build_claude_args},
    "codex":        {"kind": "subprocess", "build": _build_codex_args},
    "cursor-agent": {"kind": "subprocess", "build": _build_cursor_args},
    "gemini":       {"kind": "subprocess", "build": _build_gemini_args},
    "agy":          {"kind": "subprocess", "build": _build_agy_args, "side_effects": True},
    "openai-compat": {"kind": "api", "call": _api_call},
}
```

`BACKEND_CLIS` (and the resolver's `_VALID_CLIS`, `--doctor`, `--list-models`) all derive
from this dict — add an entry and the rest follows.

## Kind 1 — `subprocess` (drive a CLI)

Provide `build(inv) -> (command, args, env_override_or_None)`. The executor spawns
`command` with `args`, merges `env_override` (a value of `None` for a key *removes* it —
that's how codex's `OPENAI_API_KEY` gets stripped), streams stdout, and shapes the
envelope. Your builder is otherwise pure: given the `AgentInvocation` it returns argv.

- If your build must touch the filesystem (agy creates a per-invocation profile), set
  `"side_effects": True` so preview paths (`--dry-run`) know not to call `build()` as a
  pure preview.
- Map permission levels by adding a row to `_PERMISSION_MAPPING`; if your backend has no
  sandbox concept, leave it out and `permission_flags` reports `None`.

## Kind 2 — `api` (call an endpoint yourself)

Provide `call(inv, timeout_ms) -> response_dict`. No subprocess is spawned; you make the
request and return a dict shaped like `{result, status, exit_code, cli, usage?,
model_resolved?, error?}`. It flows through the same `_enrich`/`_stamp`, so the final
envelope (report parsing, `billing`, `model`, `elapsed_ms`, …) is identical to a
subprocess backend's. `openai-compat` in `_apibackend.py` is the reference implementation
(one stdlib HTTP POST). Read config from the agent frontmatter fields you add to
`AgentInvocation` (like `base_url`/`api_key_env`), resolved in `run_subagent` before
dispatch. **Redact secrets** from any error string you return (see `_apibackend._redact`).

## Checklist

1. Write the `build`/`call` function.
2. Add one `BACKENDS` entry.
3. `permission`/`billing` mappings if relevant.
4. A test (mock a server for `api`, assert argv for `subprocess`).
5. Document any new frontmatter fields.
