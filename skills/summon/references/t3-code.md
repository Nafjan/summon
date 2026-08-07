# Summon + T3 Code

**Strong claim (honest):** Summon works with [T3 Code](https://t3.codes). T3 is an agent
control plane that drives Claude Code, Codex, Cursor, and other CLIs you already pay for.
It does **not** ship its own skill root — it discovers skills those providers already load
(Claude under `~/.claude/skills`, Codex under `~/.codex/skills`, Cursor under
`~/.cursor/skills`, plus project `.claude` / `.agents` trees).

Summon supports T3 by installing into those roots and by reporting readiness in
`doctor` / `onboard`. This is **not** a native T3 IDE plugin and does not require a
pull request to T3.

## `/summon` vs `$summon` (read this if you see "No matching command")

In T3’s composer:

| Prefix | What it is | Summon? |
|--------|------------|---------|
| `/` | T3-native / provider **slash commands** (`/model`, `/plan`, …) | **No** — `/summon` will show **No matching command** |
| `$` | Provider **skills** (Claude/Codex skill picker) | **Yes** — type `$summon` and pick the skill |

Summon is installed as a **skill**, not as a T3 slash command. Claude Code itself often maps
skills to `/summon` inside a Claude Code terminal; T3’s composer does **not** — use `$`.

Also switch the thread to a **Claude Code** or **Codex** harness (not a bare OpenAI chat
model like a Luna/GPT seat that never loads filesystem skills). Then:

1. Type `$` → search **summon** → insert the chip, **or**
2. Plain language: *“run summon doctor”* / *“use the summon skill to list agents”*

If `$` says no skills found, restart T3 after `python install.py --profile t3`, and confirm
doctor’s `t3_code.skill_hosts_with_summon` includes `claude` or `codex`.

## Why stack Summon under T3

If you use T3 to drive Claude or Codex sessions, Summon lets that same session call out to
extra subscriptions and APIs you already have — Kimi, BytePlus Coding Plan, OpenRouter,
cross-vendor councils — without leaving the T3 thread. T3 picks the host CLI; Summon is
the broker those CLIs invoke.

## One-time install

```bash
# From a summon checkout (or after npx skills add / Agent Plugin install):
python install.py --profile t3
python summon.py --doctor
```

`--profile t3` installs only into Claude / Codex / Cursor skill roots that already exist
on the machine. It never invents a fake `t3` host entry.

## Smoke checklist

1. T3 is installed and has run at least once (`~/.t3` exists).
2. At least one of Claude Code, Codex, or Cursor CLI has a home dir
   (`~/.claude` / `~/.codex` / `~/.cursor`).
3. `python install.py --profile t3` reports those hosts and copies Summon.
4. `python summon.py --doctor` shows a **t3 code** section: detected, Summon on the
   skill hosts you care about, and the matching provider binary on PATH.
5. In T3, open a **Claude** or **Codex** session (not a bare GPT chat seat). Type
   **`$summon`** (not `/summon`), or ask in plain language to run the summon skill.
6. Run doctor from that session, then a cheap dry-run dispatch (e.g. list agents).

If doctor says T3 is detected but Summon is missing on a host, re-run
`python install.py --profile t3`. If a provider binary is missing from PATH, install/sign
into that CLI first — T3 cannot drive what is not on the machine.

## What we do **not** claim

- Not a first-party T3 Agent Plugin / marketplace listing (unless T3 ships one later).
- Not automatic injection into every T3 provider (Grok Build / OpenCode are driveable in
  T3 but are not Summon install hosts today).
- Not a T3 slash command — `/summon` will never register in T3’s `/` menu.
- Not a substitute for signing into Claude Max, ChatGPT, Cursor, etc. Summon still
  dispatches to those CLIs under your existing logins.

## Doctor fields (`--json`)

Under `t3_code`:

| Field | Meaning |
|-------|---------|
| `detected` | `~/.t3` exists |
| `ready` | detected **and** Summon is on a skill host whose provider CLI is on PATH |
| `skill_hosts_with_summon` | which of claude/codex/cursor have the skill |
| `providers_on_path` | which T3-driveable binaries are findable |
| `hint` | next action in portable wording (no expanded home paths) |
