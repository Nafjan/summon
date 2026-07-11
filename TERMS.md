# Terms of Service — read before you rely on summon

summon does not call any provider API itself. It drives each vendor's **own official
CLI** (`claude`, `codex`, `cursor-agent`, `gemini`, `agy`) using **your** logged-in
account. Those CLIs are built by the vendors for scripted/automated use, so basic
personal and development automation is within their intended use — but a few things
are genuinely your responsibility. This is guidance, not legal advice; the providers'
terms are the source of truth and they change.

## Generally fine (the intended use)
- **Driving the official CLIs headlessly.** Anthropic's Consumer terms explicitly exempt
  Claude Code from the automated-access prohibition (it's their own tool for scripted
  use); Codex, cursor-agent, Gemini CLI, and agy are likewise official first-party CLIs.
  summon just invokes them with the flags they already support.
- **Your own accounts, your own machine, your own work.** Personal, interactive-adjacent
  development automation on subscriptions you pay for.

## Your responsibility — don't do these
- **Don't share or pool accounts.** OpenAI and others prohibit sharing credentials or
  making an account available to others. summon uses whatever account each CLI is logged
  into — keep that yours. Don't distribute a machine/container with your tokens baked in.
- **Don't build a product or third-party service on subscription auth.** Reselling access,
  or using a consumer subscription to *power* a SaaS/app for others, violates most
  providers' terms (explicitly OpenAI's). Personal/dev use ≠ powering a commercial service.
  If you're building a product, use **paid API keys** (see the `openai-compat` backend and
  `providers.json`), not a subscription CLI.
- **Mind parallel volume.** `--manifest` and `--council` fan out concurrent sessions.
  Heavy parallelism can hit rate limits and, at extremes, look like abuse to a provider.
  Keep concurrency reasonable (the per-backend caps exist for this); don't run a
  subscription flat-out around the clock.

## Things that can change under you
- **Programmatic billing may get metered.** In 2026 Anthropic proposed (then paused)
  moving programmatic `claude -p`/Agent-SDK usage off subscription limits onto metered
  credits. Providers can reintroduce this. Watch each CLI's release notes; summon's
  `billing` envelope field tells you which source a run *currently* draws from.
- **The Agent SDK is different from the CLI.** Anthropic's Agent SDK now requires an API
  key (OAuth/subscription tokens are refused). summon deliberately uses the **CLI**
  (`claude -p`), not the SDK — but if you point it at SDK-based tooling, API billing applies.

## API-key backends (opt-in, and cleaner for products)
The `openai-compat` backend (OpenRouter, OpenAI, Anthropic, Google, local Ollama/LM
Studio, …) uses **your API key and bills your API credits** — no subscription-ToS gray
area, and the right choice for anything commercial or high-volume. You are responsible
for those API costs and each provider's API terms.

## Bottom line
Personal/dev use of the official CLIs on your own accounts is the intended path and is
what summon is for. If you're going commercial, high-volume, or multi-user, move to
API-key backends. When in doubt, read the specific provider's current terms.
