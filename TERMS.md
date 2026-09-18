# Terms of Service — read before you rely on summon

The six CLI backends do not call a provider API directly: Summon drives each vendor's
official CLI (`claude`, `codex`, `cursor-agent`, `gemini`, `kimi`, and `agy`) using the
account that you configured for that CLI. The API backends (`arkcli` and
`openai-compat`) make direct requests using the credentials and endpoint that you
configure. See [API-key backends](#api-key-backends-opt-in-and-cleaner-for-products).

Those CLIs support scripted or automated use, but the applicable provider terms and
product documentation control. This document is operational guidance, not legal advice.
Review the current terms before you automate a provider or use Summon with a shared,
commercial, or high-volume service.

## Generally fine (the intended use)
- **Driving the official CLIs headlessly.** Summon invokes the vendor CLI with the flags
  that the installed version supports. Check each vendor's current automation and usage
  rules before enabling unattended or high-volume runs.
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
- **Billing and authentication policies change.** Check the current provider documentation,
  account portal, and installed CLI behavior before relying on a subscription, API key, or
  automation route. Summon's `billing` field is an advisory classification, not an invoice
  or a guarantee that a run is included in a plan.
- **A vendor's CLI and SDK may have different contracts.** Summon invokes the configured
  CLI or endpoint adapter. Do not assume that credentials, billing, automation permissions,
  or model availability transfer between those products.

## API-key backends (opt-in, and cleaner for products)
The `openai-compat` backend sends its supported `/chat/completions` subset to the endpoint
and credentials you configure; a vendor catalog entry does not imply native support for a
different protocol such as Anthropic Messages.
Remote providers may charge an account, subscription, or credits; a local endpoint may not
have provider billing at all. Confirm the current commercial terms, data handling, and costs
for that exact endpoint. Summon does not provide a universal terms-compliance or billing
guarantee.

## Provider terms

Provider terms change. Review the current source for the backend you use:

- [Anthropic consumer terms](https://www.anthropic.com/legal/consumer-terms)
- [OpenAI terms of use](https://openai.com/policies/terms-of-use/)
- [Cursor terms of service](https://cursor.com/en-US/terms-of-service)
- [Google terms of service](https://policies.google.com/terms)

For other vendors, use the terms linked by that vendor's CLI or account portal.

## Bottom line
Personal/dev use of the official CLIs on your own accounts is the intended path and is
what summon is for. If you're going commercial, high-volume, or multi-user, move to
API-key backends. When in doubt, read the specific provider's current terms.
