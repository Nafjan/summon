# Subscription-first routing

Product direction requested 2026-09-12. Prefer approved subscription routes
with sufficient fresh quota before considering approved credit or pay-as-you-go
routes. This is a follow-on implementation requirement; it does not expand the
current 3.5.0 release blocker count or claim automatic routing is implemented.

## Current source boundary

`skills/summon/scripts/_usage.py` normalizes imported usage observations,
dimensions, units, timestamps and freshness. Imports remain operator evidence.
`_usage_live.py:CAPABILITIES` enables a narrowly version-bound Codex fixture
path; Claude/Kimi are unsupported and ArkCLI/AGY/Z.AI paths are schema-unverified.
An editorial catalog entry does not establish a working account-usage adapter.
`_usage_advisory.py:project` explicitly returns advisory-only, exact-pin-preserving
explanations. `_fleet_compile.py` records subscription/credit/payg permissions,
provider/model allowlists and finite contact/attempt/parallel ceilings. These
foundations do not implement universal quota tracking or quota-based routing.

No real account query, credential read, billing action or model dispatch was
performed for this decision. Private diagnostic history cannot replace fresh,
account-scoped provider quota evidence.

## Required policy

- Persist a versioned operator policy naming permitted account/profile routes,
  acceptable model/capability pools, quota-read scope, refresh cadence, reserve
  thresholds and paid-fallback limits. Configure this once; do not repeatedly
  request consent for operations already inside the policy. Changes outside
  its scope require separate explicit authority.
- Filter candidates by task quality/capability requirements, exact pins,
  data boundary, permissions and model-provenance gates before cost preference.
  A cheap route cannot replace a required exact model or approved vendor.
- Prefer eligible subscription capacity above its configured reserve. Consider
  every applicable short/long/model-specific quota window and reset time;
  one healthy window does not cancel an exhausted shared window. Headroom may
  include a conservative task-size estimate, clearly distinct from measured use.
- Keep allowance, rate limits, currency balances and credit units separate.
  Track shared quota pools explicitly so different seats/profiles do not double
  count one balance. Account switches invalidate the old scope binding.
- Unknown, stale, unsupported and exhausted are distinct. Unknown is neither
  free capacity nor zero. Refresh only through a verified, consented adapter;
  if eligibility still cannot be established, hold or use another already
  authorized eligible route. A dashboard must show unsupported coverage honestly.
- Paid fallback requires an already configured billing-class and spending
  boundary. If no sufficient approved fallback exists, queue/hold and explain
  the reason. Do not purchase/reset credits, repair authentication, switch
  credentials or exceed a ceiling as a side effect of quota tracking.
- Reserve capacity for local concurrent admissions and revalidate freshness
  at launch. Other applications can consume the same quota; local reservations
  are conservative scheduling controls, not authoritative provider accounting.
- Freeze routing for an admitted attempt. Reconsider at a new task or supported
  explicit follow-up/fork boundary, preserving historical identities. Fixed
  council/deliberation seats cannot be silently replaced by an economy policy.
- Provider failure or interrupted/uncertain contact must preserve the attempt
  and its possible spend. Depletion is not permission to duplicate uncertain work
  on another provider. Reconcile before any separately authorized retry.
- Query adapters use bounded hidden processes, timeouts, caching and backoff.
  Store private observations locally; public explanations contain finite reasons,
  freshness and policy outcomes without account identifiers or raw responses.

## Acceptance before activation

1. Fixture-qualified per-provider reads with no model call, auth repair, reset,
   billing mutation or unsupported private-session scraping.
2. Fresh subscription wins over paid within the same eligible pool; a route
   below reserve loses to a healthy approved subscription alternative.
3. All exhausted/stale/unknown combinations preserve the configured paid limit
   or produce an explicit hold. Missing usage never fabricates unlimited quota.
4. Shared-pool parallel claims and external depletion cannot erase reservations
   or authorize an unbudgeted paid fallback; refresh failures remain fail-closed.
5. Exact model, quality, profile, data and permission constraints win over quota
   preference. Old attempts/receipts/ballots remain immutable through remaps.
6. Multi-window/reset/clock-skew/unit/account-scope tests preserve comparability
   limits and distinguish estimates, observed balances and uncertainty.
7. Consent revocation, expiry, provider outages and process interruption stop
   refresh/launch safely without visible helper windows or private output leaks.
8. UI explains why a route was selected, held or changed; opt-out and static
   routing remain available. Unsupported providers stay visibly unsupported.

## Delivery order

Finish the existing 3.5.0 preview and its evidence gates. Then implement the
versioned policy and provider-inert selector/reservation tests, followed by
separately qualified quota adapters and controlled activation. Reuse the current
usage, fleet authority and task admission contracts. Do not create a second
scheduler or convert usage advice into implicit execution/spend authority.
