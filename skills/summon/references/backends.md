# Custom & API backends (openai-compat)

> Part of the **summon** skill. See the main SKILL.md for core usage.

## Custom & API backends (`openai-compat`) — add any model

Beyond the five CLIs, an agent can run against **any OpenAI-compatible
`/chat/completions` API** — OpenRouter, OpenAI, Anthropic, Google (Gemini compat),
Groq, DeepSeek, Together, or a LOCAL server (Ollama, LM Studio, vLLM, llama.cpp).
Pure stdlib HTTP, no SDK. This bills your **API key/credits**, not a subscription
(cleaner for commercial/high-volume — see [TERMS.md](TERMS.md)).

```markdown
---
run-agent: openai-compat
provider: openrouter                 # or: openai / anthropic / google / groq / ollama / lmstudio / <your provider>
model: anthropic/claude-3.5-sonnet   # the API's model id
---
```
or point anywhere directly (no provider needed):
```markdown
---
run-agent: openai-compat
base_url: http://localhost:11434/v1  # local Ollama
api_key_env: ""                       # empty = no auth header
model: llama3.1
---
```

**Providers** resolve from built-ins + an optional `providers.json` in the agents
dir (or `~/.agents/providers.json`) — `{ "myprov": {"base_url": "...", "api_key_env":
"MY_KEY"} }` (see `providers.json.example`). The API key is read from the named env
var at dispatch (never stored). Everything else is identical: same envelope, same
`--manifest`/`--council`/`--json-schema`. Create these agents by hand or with
`--new-agent NAME --set run-agent=openai-compat --set model=...`. Resume isn't
supported (the API call is stateless). This is how you add local AI and multi-model
API access — and it makes `--council` a true multi-vendor board (à la OpenRouter).
