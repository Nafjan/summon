# Summon swarm protocol

Status: **contract-only preview**. This document and `_swarm_protocol.py` define
the first versioned wire boundary; they do not claim that external IDE swarms
are already attachable or that a durable coordinator has shipped.

## Why a separate protocol

`manifest` is reliable batch fan-out, `council` is a durable round-based
orchestrator, and `chat` is a human/agent conversation room. None is a shared
worker scheduler. Swarm coordination must therefore have its own owner, lease,
claim, artifact, and cancellation semantics. Conversation rooms may project
swarm events for people, but they never become the coordinator’s authority log.

## Wire format

Frames are newline-delimited JSON using `summon.swarm/v1`. Every frame carries:

```json
{"protocol":"summon.swarm/v1","run_id":"swarm-1","message_id":"m-1",
 "type":"hello","sent_at_ms":1730000000000,"payload":{}}
```

The framing layer bounds frames at 64 KiB and payloads at 48 KiB, rejects
unknown protocol versions, rejects unknown frame types, and uses strict ASCII
identifiers plus SHA-256 fingerprints for project, roster, request, envelope,
and artifact identity.

## Required lifecycle

1. `hello` / `hello_ack` negotiate the version, adapter identity, capabilities,
   message/artifact limits, cancellation support, and permission ceiling.
2. `register_worker` binds a worker instance to a project digest and roster
   definition digest.
3. `claim` / `renew` / `claim_released` use a claim id, attempt number, lease
   generation, expiry, and request digest. A late or stale worker can never
   complete a newer claim.
4. `send_message` names an explicit recipient (`worker`, `coordinator`, or
   `human`) and carries a content digest plus bounded redacted preview. Prose
   is never interpreted as approval or cancellation.
5. `publish_artifact` carries the task/claim/attempt/lease-generation/request
   fence plus an opaque artifact id, digest, bounded size, media type, and
   contained relative path or opaque URI. Opaque URIs are metadata only (the
   framing layer never dereferences them), use an allowlisted `artifact`,
   `https`, `http`, `ipfs`, `s3`, or `gs` scheme, and may not carry userinfo,
   query strings, or fragments. The artifact fence must match the claim that
   produced it; a stale worker cannot publish into a successor claim.
6. `cancel_requested` is acknowledged by `ack_cancel`, then ends as
   `cancelled` or `indeterminate`; uncertain spend is never silently retried.
7. `complete`, `fail`, or `task_blocked` carries the existing Summon envelope
   digest and the validated task/claim/attempt/lease-generation/request fence.

The envelope has a closed top-level schema (`protocol`, `run_id`,
`message_id`, `type`, `sent_at_ms`, `payload`). Unknown top-level or
state-changing payload fields are rejected rather than being treated as
forward-compatible authority. A future protocol version must explicitly
negotiate any new authority-bearing fields.

## Compatibility target

The first external adapter is stdio JSONL. IDE and CLI bridges can implement
the same frames without Summon pretending to control a host-native session.
Unknown major versions are refused. Additive display-only fields may be added
within a major version, but state-changing fields must be explicitly versioned.

## Implementation gates

The next implementation layers are deliberately staged:

- fake/in-memory adapter conformance tests;
- a durable `_rundir`-backed coordinator with owner/lease/generation fences;
- manifest jobs bridged into coordinator claims without changing legacy CLI
  envelopes;
- one local stdio adapter with process-tree cancellation;
- host-specific IDE bridges, each with claim/renew/message/artifact/cancel/
  reconnect conformance tests.

Until those gates pass, describe Summon as **batch fan-out plus council**, not
as interoperable collaborative swarming.
