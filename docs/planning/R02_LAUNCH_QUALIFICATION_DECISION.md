# R02 launch qualification and legacy revalidation

Architect decision, 2026-09-08. This resolves the runtime interpretation of
the existing R01/R02 requirements. Implementation and independent acceptance
remain required; no release row is promoted by this decision.

## Evidence and authority

The canonical v2 registry remains a pure declaration. Its current
`qualification.status=unqualified` and `external_cli_version_scope=not_declared`
are honest statements about unavailable evidence. Neither value may be
translated into an observed version or a qualified host operation.

Current launch observation records executable and resolver-owned material
identity, observed vendor version when available, route, adapter and registry
generation, attempt identity, nonce and time. Content identity and vendor version
remain separate. A fresh observation must come through the trusted host producer
and be bound to the actual resolved provider launch, including a distinct gate
launch where applicable. Structural validity is not producer authentication.

Runtime qualification is a separate authenticated record binding the permitted
operation, backend/transport, adapter revision, accepted external version, material
contract and qualification evidence. Bind it to the canonical registry generation
and its explicit expiry/revocation scope. A copied declaration, caller-controlled
trust flag, matching hash or model recommendation cannot create this record.
Use existing private authority mechanisms with domain separation and strict
versioned readers; do not expose the record or its bearer material publicly.

For this migration, an absent external version or missing qualification refuses
current qualification with a typed reason. No generic content-only alternative
is approved. A future content-qualified adapter requires a separate, concrete
policy and evidence review. Candidate and unsupported routes retain their current
classifications. An exact served-model claim still requires its own authoritative
served-model evidence, independently of local executable/version qualification.

## Enforcement sequence

1. Read and authenticate the historical source and current operation grant.
2. Resolve the exact proposed launch and obtain its trusted observation.
3. Validate the separate qualification record against the observation, current
   registry/adapter generation, permitted version/material contract, expiry and
   revocation. Do not feed declaration text into an observed-version field.
4. Revalidate current owner, turn/successor, session, account/profile, model,
   permissions, routing and spend boundaries before durable consumption.
5. Consume the exact attempt's launch authority once, then reach provider Popen.
   Preserve uncertainty if consumption succeeds but the launch outcome is unknown.

Observation age and qualification expiry use explicit bounded units. Reject
replay, substitution, future timestamps, clock rollback beyond the declared
policy, bool-as-number and non-finite values. Unknown wrappers or incomplete
material descriptors cannot masquerade as fully measured native executables.

## Explicit legacy revalidation

Provide a separate authenticated, non-launching revalidation operation through
the supported public entry points. It takes an existing historical source and
current trusted qualification/observation, checks the unchanged authority and
identity constraints, and writes a new private seal linked to that source.
It does not edit old journals, approvals, receipts, model identity or spend facts,
and does not claim that newly measured facts were observed during the old run.

Any migration from an unversioned historical source must be explicitly supported
by the qualified adapter's compatibility policy. It cannot use a wildcard for
all legacy versions or all backends. Missing required history or qualification
refuses with a concrete reason and offers the existing explicit non-launching
fork action. Refusal alone must not create a room, fork or provider attempt.

Revalidation is not a retry, resume, budget extension, policy change or provider
grant. A later explicitly authorized continuation separately consumes launch
authority and rechecks the seal at the actual provider boundary. Preserve exact
v1 readers and original history while allowing supported sources to acquire
current evidence through this operation. The bounded public consumers are
`jobs revalidate JOB_ID --evidence-file FILE` and
`chat revalidate SESSION_ID PARTICIPANT --evidence-file FILE`; the private
packet must contain an already authenticated `observation` and `qualification`
issued by a reviewed adapter-side producer. A legacy chat room without a
source-family sidecar additionally requires a separately held
`SUMMON_CHAT_MIGRATION_KEY` authority; the packet's family MAC is not an issuer
credential. Summon validates the packet, writes the linked launch-binding and
qualification seals, and returns only a path-free receipt. It never signs
caller metadata or starts a child. Installed evidence issuance and trust-key
provisioning remain separate qualification gates. Unsupported routes, missing
history, forged authentication, missing authority and publication conflicts
refuse without a provider attempt. Publication is recoverable: the private
qualification is written before the family link, and an interrupted retry can
complete the same authenticated link without trusting an orphaned file.

## Acceptance and release scope

Retain positive and negative journeys with fake executables and authenticated
synthetic qualification records: supported version; absent/mismatched version;
executable, script or required-material replacement; registry/adapter revocation;
expired or substituted qualification; wrong owner/turn/successor; duplicate and
competing consumption; gate/main separation; and before/after-consumption failure.
The public legacy-revalidation journey must preserve original source bytes,
remain non-launching, and enable only the specifically qualified later operation.

Keep ordinary fresh dispatch, strict fleet/deliberation envelopes and historical
readability regressions. Passing tests that inject observations must not be
presented as qualification of an installed provider. Installed/live qualification
remains subject to its existing separate gate. This implementation campaign uses
no provider calls, account/auth changes, installations, commits or publication.

The conductor owns implementation and the final source/test handoff. The architect
owns independent acceptance against this decision and the existing migration
matrix. These requirements close the existing migration scope; they do not
authorize a broader adapter program or change the reviewed 3.5 preview cut.
