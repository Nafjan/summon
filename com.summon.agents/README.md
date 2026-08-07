# Summon agent packs (Agent Plugins extension namespace)

Third parties may ship **roster packs** as Agent Plugins that include a
`com.summon.agents/` directory:

```text
my-council-pack/
  plugin.json
  com.summon.agents/
    reviewer.md
    architect.md
```

Summon discovers these packs fail-soft from:

1. Plugin root sibling: `<plugin-root>/com.summon.agents/*.md`
2. User packs: `~/.agents/packs/<name>/com.summon.agents/*.md`

Agent markdown uses the same frontmatter as the bundled roster
(`run-agent`, `model`, `permission`, …). Packs never override an existing
name in the active agents directory unless explicitly selected.

This namespace is Summon-specific (reverse-domain extension). Other Agent
Plugins clients ignore it.
