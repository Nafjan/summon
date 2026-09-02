# Customizing agents & the roster

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Customizing agents (you, the calling agent, are expected to)

The bundled roster is a starting point, not a fixed menu. As the orchestrator you
have two levers — use them freely:

**1. Per-dispatch, no files touched** — override an agent's model, reasoning effort,
backend flags, or private login profile for a single call:
```bash
run_subagent.py --agent reviewer --model claude-sonnet-5 --effort high \
  --prompt "…" --cwd <abs>
```
`--model` accepts any model the backend supports (see [models.md](models.md)).
`--effort` is `low|medium|high|xhigh|max` (or `none` for the backend's own default).
**Not every CLI honors it** — claude/codex do (default `high`); agy Gemini only when
effort is set explicitly (model-name suffix); Kimi applies it through the disposable
profile for supported models (the bundled K3 seats use `max`); cursor/openai-compat/arkcli
ignore Summon `--effort`. Full matrix: [effort.md](effort.md). The prompt itself is your main
customization — the agent definition sets the role, the prompt sets the task.

**2. Durably — manage the roster from the CLI** (no hand-authored markdown needed):

```bash
# scaffold a new agent (house template: report contract + untrusted-content guard)
run_subagent.py --new-agent fact-checker \
  --set run-agent=codex --set permission=read-only --set model=gpt-5.6-sol
# then edit the body (purpose, Role, rubric) in the printed path

# retune an existing agent's frontmatter — body untouched, validated, atomic
run_subagent.py --set-agent pair --set model=claude-sonnet-5
run_subagent.py --set-agent reviewer --set 'args=-c model_reasoning_effort="high"'
run_subagent.py --set-agent probe --set model=        # empty value REMOVES the key
```

Settable keys: `run-agent` (claude/codex/cursor-agent/gemini/agy), `model`,
`permission` (`read-only`/`safe-edit`/`yolo`), `args` (extra backend flags), and
`profile` (a private registry name) —
values are validated before anything is written. `--new-agent` never overwrites;
`--set-agent` edits frontmatter only, leaving the body byte-identical.

### Multiple Claude and Codex accounts

When one backend has multiple local accounts or installations, keep the paths and
credentials out of the repository. Put named entries in `~/.agents/summon-profiles.json`:

```json
{
  "profiles": {
    "claude-work": {
      "cli": "claude",
      "config_dir": "<absolute private Claude account directory>",
      "auth_mode": "login"
    },
    "codex-work": {
      "cli": "codex",
      "config_dir": "<absolute private Codex account directory>",
      "auth_mode": "login"
    }
  }
}
```

Create each directory outside your project, with access restricted to your OS user.
Do not copy credentials from your personal account. Sign in once to each chosen account:

```text
summon auth repair claude --profile claude-work --allow-auth-repair
summon auth status --cli claude --profile claude-work --probe --json
summon auth repair codex --profile codex-work --allow-auth-repair
summon auth status --cli codex --profile codex-work --probe --json
```

The vendor opens its sign-in flow; choose the intended account yourself. With a profile,
`auth status --probe` runs only the vendor's auth-status command, not a model prompt.
Without `--probe`, it checks local profile resolution and reports auth as unverified.
Status output omits emails, organization identifiers, keys, and raw vendor output.

Then pass `--profile claude-work` or `--profile codex-work` on a dispatch. A trusted local
agent can also declare `profile: claude-work`. A missing profile, wrong backend, or failed
login never selects another account automatically. Existing agents without a profile
keep their current routing. Use neutral names if you share receipts: profile names are
visible, but their directories and credentials are not.

`auth_mode: login` removes inherited backend API keys, OAuth-token overrides, and route
overrides. Claude uses that account's user settings rather than project/local settings.
Codex uses a separate `CODEX_HOME`, file credential storage, and ChatGPT login with the
first-party provider. Explicit models are required for named Codex profiles. Both support
an optional `command` containing the absolute path to the real backend executable and
an optional `models` allowlist. Account mode rejects opaque backend `args:` overrides;
use the supported model, effort, and permission fields instead.

These are vendor configuration boundaries, **not OS sandboxes or provider-reported
account attestation**. Keep account directories private, check the selected account in
the vendor UI, and use your organization's data-handling rules. Managed organization
settings still apply; account-owned settings can configure credential helpers or
provider routes, so use a clean directory for a subscription account. Local auth/config
changes invalidate cached request identity; keyring-only changes are not file-attested.

Only subprocess dispatch supports named accounts. Claude resume accepts a session UUID
within the selected account home; use governed `jobs resume` to retain the original
profile binding. Named Codex account resume is not certified: start a fresh job with an
explicit context handoff. Do not transfer personal session transcripts to a work account
implicitly. Account profiles do not switch the host IDE's login.

Usage and credit availability remain unknown unless evidence is bound to the selected
account. Do not apply a personal subscription's remaining quota to a work profile or
automatically rotate accounts after quota/auth errors.

Existing custom-provider profiles can omit `auth_mode` (equivalent to `"profile"`),
which preserves their own configuration semantics. This is separate from Codex's native
`--profile` config presets: Summon's `--profile` selects a private backend home.

Vendor references: [Claude authentication](https://code.claude.com/docs/en/authentication)
and [Codex authentication](https://developers.openai.com/codex/auth).

Definitions are plain `.md` files in the agents dir (`--agents-dir`,
`$SUB_AGENTS_DIR`, or `{cwd}/.agents/`) and register **instantly** — no reload; the
next `--list`/dispatch sees them. You can still write or edit the files directly
(the scaffold exists because a hand-written definition tends to miss the
Final-report contract the dispatcher parses). Authoring a task-specific persona is
one `--new-agent` plus a body edit — do it whenever the standing roster doesn't fit.

**Different models per role is the whole point.** Give planning/architecture agents a
deep model (`opus`, `claude-fable-5-1`), balanced work a `claude-sonnet-5` agent, cheap
mechanical passes a lighter one, and fan a task across several models at once with
`--manifest` (per-job `model:`). Nothing here is baked into the skill — it's all in the
`.md` files and the flags you pass.
