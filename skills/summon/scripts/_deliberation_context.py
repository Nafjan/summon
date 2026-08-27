"""Provider-inert durable-context freshness for the canonical deliberation packet.

The module binds explicitly supplied durable decisions, constraints, rationale and
content-addressed artifacts to a verified source observation.  It has no provider,
routing, credential, session, process or filesystem surface.  Stale acceptance is
one-run authority for context use only and never changes a seat or dispatch decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


PACKET_SCHEMA = "summon.deliberation-context/v1"
OBSERVATION_SCHEMA = "summon.context-source-observation/v1"
ACCEPTANCE_SCHEMA = "summon.accept-stale-intent/v1"
BINDING_SCHEMA = "summon.deliberation-context-binding/v1"
PUBLIC_SCHEMA = "summon.deliberation-context-public/v1"
MAX_INPUT_BYTES = 256 * 1024
MAX_DEPTH = 64
MAX_NODES = 8_192
MAX_ENTRIES = 64
MAX_ENTRY_BODY_BYTES = 8 * 1024
# Leave room under the scheduler's 64 KiB prompt ceiling for the question,
# immutable policy/seat identity, transcript, provenance, and JSON framing.
MAX_TOTAL_BODY_BYTES = 24 * 1024
MAX_REASON_BYTES = 2 * 1024
MAX_LABEL_BYTES = 256
MAX_CLOCK_SKEW_MS = 300_000
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_MANIFEST_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_KINDS = frozenset({"git_commit", "revision_manifest"})
_ENTRY_KINDS = frozenset({"decision", "constraint", "rationale", "artifact_reference"})
_PROVENANCE_KINDS = frozenset({"authored", "explicit_promotion"})
_RELATIONS = frozenset({"same", "descendant", "diverged", "rewound", "unknown"})
_ACTOR_KINDS = frozenset({"human", "calling_agent"})


class ContextFreshnessError(ValueError):
    """Durable context or its one-run authority is structurally unsafe."""


def _reject_constant(value: str) -> None:
    raise ContextFreshnessError(f"non-finite JSON number is forbidden: {value}")


def _reject_float(value: str) -> None:
    raise ContextFreshnessError(f"floating JSON number is forbidden: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContextFreshnessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _text(value: object, name: str, *, max_bytes: int = MAX_LABEL_BYTES,
          pattern: re.Pattern[str] | None = None, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ContextFreshnessError(f"{name} must be text")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContextFreshnessError(f"{name} contains invalid Unicode") from exc
    if (nonempty and not value) or len(raw) > max_bytes or "\x00" in value:
        raise ContextFreshnessError(f"{name} is outside its text bound")
    if pattern is not None and not pattern.fullmatch(value):
        raise ContextFreshnessError(f"{name} has an invalid format")
    return value


def _integer(value: object, name: str, *, minimum: int = 0,
             maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextFreshnessError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ContextFreshnessError(f"{name} is outside its bound")
    return value


def _fields(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ContextFreshnessError(f"{name} fields are invalid")


def _plain_snapshot(value: object, *, depth: int = 0, counter: list[int] | None = None) -> object:
    if counter is None:
        counter = [0]
    if depth > MAX_DEPTH:
        raise ContextFreshnessError("context exceeds the nesting depth bound")
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise ContextFreshnessError("context exceeds the node bound")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            _text(value, "context text", max_bytes=MAX_INPUT_BYTES, nonempty=False)
        if isinstance(value, float):
            raise ContextFreshnessError("floating values are forbidden")
        return value
    if isinstance(value, Mapping):
        try:
            items = list(value.items())
        except Exception as exc:  # hostile custom Mapping must become a typed refusal
            raise ContextFreshnessError("context mapping could not be snapshotted") from exc
        result = {}
        for key, item in items:
            key = _text(key, "context object key", max_bytes=MAX_LABEL_BYTES)
            if key in result:
                raise ContextFreshnessError(f"duplicate context key: {key}")
            result[key] = _plain_snapshot(item, depth=depth + 1, counter=counter)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        try:
            items = list(value)
        except Exception as exc:
            raise ContextFreshnessError("context sequence could not be snapshotted") from exc
        return [_plain_snapshot(item, depth=depth + 1, counter=counter) for item in items]
    raise ContextFreshnessError("context contains an unsupported value")


def _load(value: object, name: str) -> dict:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) > MAX_INPUT_BYTES:
            raise ContextFreshnessError(f"{name} exceeds the byte bound")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContextFreshnessError(f"{name} is not valid UTF-8") from exc
        try:
            loaded = json.loads(text, object_pairs_hook=_pairs,
                                parse_constant=_reject_constant,
                                parse_float=_reject_float)
        except ContextFreshnessError:
            raise
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContextFreshnessError(f"{name} is not strict JSON") from exc
    elif isinstance(value, str):
        try:
            raw_text = value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContextFreshnessError(f"{name} contains invalid Unicode") from exc
        return _load(raw_text, name)
    else:
        loaded = value
    snapshot = _plain_snapshot(loaded)
    if not isinstance(snapshot, dict):
        raise ContextFreshnessError(f"{name} must be an object")
    raw = _canonical_bytes(snapshot)
    if len(raw) > MAX_INPUT_BYTES:
        raise ContextFreshnessError(f"{name} exceeds the canonical byte bound")
    return snapshot


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContextFreshnessError("context is not canonical JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ParsedContext:
    packet_sha256: str
    source_revision_sha256: str
    source: Mapping[str, object]
    freshness_policy: Mapping[str, int]
    entries: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class BoundContext:
    schema: str
    state: str
    packet_sha256: str
    source_revision_sha256: str
    binding_sha256: str
    run_id: str
    decision_id: str
    bound_at_unix_ms: int
    actual_age_ms: int
    actual_revision_delta: int
    mismatch_reasons: tuple[str, ...]
    routing_authority: bool
    source: Mapping[str, object]
    freshness_policy: Mapping[str, int]
    entries: tuple[Mapping[str, object], ...]
    observation: Mapping[str, object]
    acceptance: Mapping[str, object] | None


def parse_context_packet(value: object) -> ParsedContext:
    packet = _load(value, "durable context packet")
    _fields(packet, {"schema", "source", "freshness_policy", "entries"}, "context packet")
    if packet["schema"] != PACKET_SCHEMA:
        raise ContextFreshnessError("context packet schema is unsupported")
    source = packet["source"]
    if not isinstance(source, dict):
        raise ContextFreshnessError("context source must be an object")
    _fields(source, {"kind", "revision", "source_digest", "captured_at_unix_ms"},
            "context source")
    kind = _text(source["kind"], "source kind")
    if kind not in _SOURCE_KINDS:
        raise ContextFreshnessError("source kind is unsupported")
    pattern = _GIT_REVISION if kind == "git_commit" else _MANIFEST_REVISION
    revision = _text(source["revision"], "source revision", pattern=pattern)
    _text(source["source_digest"], "source digest", max_bytes=64, pattern=_HEX64)
    if (kind == "revision_manifest"
            and revision.removeprefix("sha256:") != source["source_digest"]):
        raise ContextFreshnessError(
            "revision manifest identity must equal its source digest")
    _integer(source["captured_at_unix_ms"], "source capture time")

    policy = packet["freshness_policy"]
    if not isinstance(policy, dict):
        raise ContextFreshnessError("freshness policy must be an object")
    _fields(policy, {"fresh_max_age_ms", "hard_max_age_ms", "hard_max_revision_delta"},
            "freshness policy")
    fresh = _integer(policy["fresh_max_age_ms"], "fresh maximum age", maximum=2**53 - 1)
    hard = _integer(policy["hard_max_age_ms"], "hard maximum age", maximum=2**53 - 1)
    delta = _integer(policy["hard_max_revision_delta"], "hard revision delta", maximum=1_000_000)
    if fresh > hard:
        raise ContextFreshnessError("fresh maximum age exceeds the hard maximum")

    entries = packet["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ContextFreshnessError("context entries are outside their count bound")
    seen = set()
    total_body = 0
    normalized_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContextFreshnessError("context entry must be an object")
        kind_value = entry.get("kind")
        expected = ({"id", "kind", "artifact_ref", "provenance"}
                    if kind_value == "artifact_reference"
                    else {"id", "kind", "body", "provenance"})
        _fields(entry, expected, "context entry")
        entry_id = _text(entry["id"], "entry id", pattern=_ENTRY_ID)
        if entry_id in seen:
            raise ContextFreshnessError("context entry ids must be unique")
        seen.add(entry_id)
        entry_kind = _text(entry["kind"], "entry kind")
        if entry_kind not in _ENTRY_KINDS:
            raise ContextFreshnessError("context entry kind is unsupported")
        if entry_kind == "artifact_reference":
            _text(entry["artifact_ref"], "artifact reference", max_bytes=71,
                  pattern=_ARTIFACT_REF)
        else:
            body = _text(entry["body"], "entry body", max_bytes=MAX_ENTRY_BODY_BYTES)
            total_body += len(body.encode("utf-8"))
        provenance = entry["provenance"]
        if not isinstance(provenance, dict):
            raise ContextFreshnessError("entry provenance must be an object")
        _fields(provenance, {"kind", "source_sha256"}, "entry provenance")
        if _text(provenance["kind"], "provenance kind") not in _PROVENANCE_KINDS:
            raise ContextFreshnessError("entry provenance kind is unsupported")
        _text(provenance["source_sha256"], "provenance digest", max_bytes=64,
              pattern=_HEX64)
        normalized_entries.append(entry)
    if total_body > MAX_TOTAL_BODY_BYTES:
        raise ContextFreshnessError("context entry bodies exceed the aggregate bound")
    packet_sha = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    revision_sha = hashlib.sha256(revision.encode("utf-8")).hexdigest()
    return ParsedContext(
        packet_sha, revision_sha, _freeze(source), _freeze(policy),
        tuple(_freeze(entry) for entry in normalized_entries))


def _parse_observation(value: object, parsed: ParsedContext) -> dict:
    observation = _load(value, "source observation")
    _fields(observation, {"schema", "kind", "captured_revision", "current_revision",
                          "current_source_digest", "relation", "revision_delta",
                          "verification_method", "observed_at_unix_ms"},
            "source observation")
    if observation["schema"] != OBSERVATION_SCHEMA:
        raise ContextFreshnessError("source observation schema is unsupported")
    kind = _text(observation["kind"], "observation source kind")
    if kind != parsed.source["kind"]:
        raise ContextFreshnessError("source observation kind does not match the packet")
    pattern = _GIT_REVISION if kind == "git_commit" else _MANIFEST_REVISION
    captured = _text(observation["captured_revision"], "captured revision", pattern=pattern)
    current = _text(observation["current_revision"], "current revision", pattern=pattern)
    if captured != parsed.source["revision"]:
        raise ContextFreshnessError("source observation captured revision does not match")
    _text(observation["current_source_digest"], "current source digest", max_bytes=64,
          pattern=_HEX64)
    relation = _text(observation["relation"], "revision relation")
    if relation not in _RELATIONS:
        raise ContextFreshnessError("revision relation is unsupported")
    delta = _integer(observation["revision_delta"], "observed revision delta",
                     maximum=1_000_000_000)
    method = _text(observation["verification_method"], "verification method")
    expected_method = "git-readback" if kind == "git_commit" else "sha256-readback"
    if method != expected_method:
        raise ContextFreshnessError("source verification method is not authoritative")
    _integer(observation["observed_at_unix_ms"], "observation time")
    if relation == "same" and (current != captured or delta != 0):
        raise ContextFreshnessError("same revision observation is inconsistent")
    if relation == "descendant" and (current == captured or delta < 1):
        raise ContextFreshnessError("descendant revision observation is inconsistent")
    if relation in {"diverged", "rewound", "unknown"} and delta != 0:
        raise ContextFreshnessError("unbounded revision relation cannot claim a delta")
    return observation


def _parse_acceptance(value: object) -> dict:
    acceptance = _load(value, "stale acceptance intent")
    _fields(acceptance, {"schema", "packet_sha256", "source_revision_sha256",
                         "run_id", "decision_id", "actor", "reason", "scope",
                         "expires_at_unix_ms", "max_age_ms", "max_revision_delta"},
            "stale acceptance intent")
    if acceptance["schema"] != ACCEPTANCE_SCHEMA:
        raise ContextFreshnessError("stale acceptance schema is unsupported")
    for key in ("packet_sha256", "source_revision_sha256"):
        _text(acceptance[key], key, max_bytes=64, pattern=_HEX64)
    _text(acceptance["run_id"], "acceptance run id", pattern=_RUN_ID)
    _text(acceptance["decision_id"], "acceptance decision id", pattern=_RUN_ID)
    actor = acceptance["actor"]
    if not isinstance(actor, dict):
        raise ContextFreshnessError("acceptance actor must be an object")
    _fields(actor, {"kind", "id"}, "acceptance actor")
    if _text(actor["kind"], "acceptance actor kind") not in _ACTOR_KINDS:
        raise ContextFreshnessError("acceptance actor kind is unsupported")
    _text(actor["id"], "acceptance actor id")
    _text(acceptance["reason"], "acceptance reason", max_bytes=MAX_REASON_BYTES)
    scope = acceptance["scope"]
    if not isinstance(scope, dict):
        raise ContextFreshnessError("acceptance scope must be an object")
    _fields(scope, {"entry_ids", "use"}, "acceptance scope")
    if scope["use"] != "deliberation_prompt":
        raise ContextFreshnessError("acceptance scope use is unsupported")
    ids = scope["entry_ids"]
    if not isinstance(ids, list) or not ids or len(ids) > MAX_ENTRIES:
        raise ContextFreshnessError("acceptance entry scope is invalid")
    normalized = [_text(item, "acceptance entry id", pattern=_ENTRY_ID) for item in ids]
    if len(set(normalized)) != len(normalized):
        raise ContextFreshnessError("acceptance entry scope contains duplicates")
    _integer(acceptance["expires_at_unix_ms"], "acceptance expiry")
    _integer(acceptance["max_age_ms"], "acceptance maximum age", maximum=2**53 - 1)
    _integer(acceptance["max_revision_delta"], "acceptance revision delta",
             maximum=1_000_000)
    return acceptance


def acceptance_identity(value: object) -> tuple[str, str]:
    """Return the strictly parsed one-run identity chosen by an operator."""
    acceptance = _parse_acceptance(value)
    return acceptance["run_id"], acceptance["decision_id"]


def bind_context(packet: object, observation: object, *, run_id: str,
                 decision_id: str, unix_now_ms: int,
                 accept_stale: object | None = None) -> BoundContext:
    parsed = packet if isinstance(packet, ParsedContext) else parse_context_packet(packet)
    run_id = _text(run_id, "run id", pattern=_RUN_ID)
    decision_id = _text(decision_id, "decision id", pattern=_RUN_ID)
    now = _integer(unix_now_ms, "context clock")
    observed = _parse_observation(observation, parsed)
    observed_at = observed["observed_at_unix_ms"]
    if abs(observed_at - now) > MAX_CLOCK_SKEW_MS:
        raise ContextFreshnessError("source observation clock is outside the allowed skew")
    captured_at = parsed.source["captured_at_unix_ms"]
    if captured_at - now > MAX_CLOCK_SKEW_MS:
        raise ContextFreshnessError("context capture clock is too far in the future")
    age = max(0, now - captured_at)
    delta = observed["revision_delta"]
    reasons: list[str] = []
    unbounded = False
    relation = observed["relation"]
    if relation == "same":
        if observed["current_source_digest"] != parsed.source["source_digest"]:
            reasons.append("same_revision_digest_mismatch")
            unbounded = True
    elif relation == "descendant":
        reasons.append("source_revision_advanced")
    else:
        reasons.append("revision_relation_unbounded")
        unbounded = True
    if age > parsed.freshness_policy["fresh_max_age_ms"]:
        reasons.append("age_exceeds_fresh_limit")
    if age > parsed.freshness_policy["hard_max_age_ms"]:
        reasons.append("age_exceeds_hard_limit")
        unbounded = True
    if delta > parsed.freshness_policy["hard_max_revision_delta"]:
        reasons.append("revision_delta_exceeds_hard_limit")
        unbounded = True

    acceptance = _parse_acceptance(accept_stale) if accept_stale is not None else None
    state = "fresh" if not reasons else "stale_refused"
    if reasons and acceptance is not None and not unbounded:
        entry_ids = {entry["id"] for entry in parsed.entries}
        acceptance_ids = set(acceptance["scope"]["entry_ids"])
        valid = (
            acceptance["packet_sha256"] == parsed.packet_sha256
            and acceptance["source_revision_sha256"] == parsed.source_revision_sha256
            and acceptance["run_id"] == run_id
            and acceptance["decision_id"] == decision_id
            and acceptance["expires_at_unix_ms"] > now
            and acceptance["max_age_ms"] <= parsed.freshness_policy["hard_max_age_ms"]
            and acceptance["max_revision_delta"]
                <= parsed.freshness_policy["hard_max_revision_delta"]
            and age <= acceptance["max_age_ms"]
            and delta <= acceptance["max_revision_delta"]
            and acceptance_ids == entry_ids
        )
        if valid:
            state = "accepted_stale"

    acceptance_sha = _sha(acceptance) if acceptance is not None else None
    identity = {
        "schema": BINDING_SCHEMA,
        "packet_sha256": parsed.packet_sha256,
        "source_revision_sha256": parsed.source_revision_sha256,
        "run_id": run_id,
        "decision_id": decision_id,
        "bound_at_unix_ms": now,
        "state": state,
        "actual_age_ms": age,
        "actual_revision_delta": delta,
        "mismatch_reasons": reasons,
        "observation_identity": {
            key: observed[key] for key in (
                "kind", "captured_revision", "current_revision",
                "current_source_digest", "relation", "revision_delta",
                "verification_method")
        },
        "acceptance_sha256": acceptance_sha,
        "routing_authority": False,
    }
    return BoundContext(
        BINDING_SCHEMA, state, parsed.packet_sha256, parsed.source_revision_sha256,
        _sha(identity), run_id, decision_id, now, age, delta, tuple(reasons), False,
        parsed.source, parsed.freshness_policy, parsed.entries, _freeze(observed),
        _freeze(acceptance) if acceptance is not None else None)


def public_projection(binding: BoundContext) -> dict:
    if not isinstance(binding, BoundContext):
        raise ContextFreshnessError("public context projection requires a binding")
    return {
        "schema": PUBLIC_SCHEMA,
        "state": binding.state,
        "packet_sha256": binding.packet_sha256,
        "binding_sha256": binding.binding_sha256,
        "actual_age_ms": binding.actual_age_ms,
        "actual_revision_delta": binding.actual_revision_delta,
        "mismatch_reasons": list(binding.mismatch_reasons),
        "entry_count": len(binding.entries),
        "affected_entry_count": len(binding.entries) if binding.state != "fresh" else 0,
        "constraint_count": sum(entry["kind"] == "constraint" for entry in binding.entries),
        "routing_authority": False,
    }


def prompt_projection(binding: BoundContext) -> dict:
    if not isinstance(binding, BoundContext):
        raise ContextFreshnessError("prompt context projection requires a binding")
    return {
        "schema": PACKET_SCHEMA,
        "freshness": {
            "state": binding.state,
            "packet_sha256": binding.packet_sha256,
            "actual_age_ms": binding.actual_age_ms,
            "actual_revision_delta": binding.actual_revision_delta,
            "mismatch_reasons": list(binding.mismatch_reasons),
            "routing_authority": False,
        },
        "entries": [_thaw(entry) for entry in binding.entries],
    }


def private_projection(binding: BoundContext) -> dict:
    """Return the full receipt-bound context evidence; never expose this in a UI."""
    if not isinstance(binding, BoundContext):
        raise ContextFreshnessError("private context projection requires a binding")
    return {
        "schema": BINDING_SCHEMA,
        "state": binding.state,
        "packet_sha256": binding.packet_sha256,
        "source_revision_sha256": binding.source_revision_sha256,
        "binding_sha256": binding.binding_sha256,
        "run_id": binding.run_id,
        "decision_id": binding.decision_id,
        "bound_at_unix_ms": binding.bound_at_unix_ms,
        "actual_age_ms": binding.actual_age_ms,
        "actual_revision_delta": binding.actual_revision_delta,
        "mismatch_reasons": list(binding.mismatch_reasons),
        "routing_authority": False,
        "source": _thaw(binding.source),
        "freshness_policy": _thaw(binding.freshness_policy),
        "entries": [_thaw(entry) for entry in binding.entries],
        "observation": _thaw(binding.observation),
        "acceptance": _thaw(binding.acceptance) if binding.acceptance is not None else None,
    }


def parse_private_projection(value: object) -> BoundContext:
    """Reproduce an immutable binding from a private receipt projection."""
    projection = _load(value, "private context projection")
    expected = {
        "schema", "state", "packet_sha256", "source_revision_sha256",
        "binding_sha256", "run_id", "decision_id", "bound_at_unix_ms",
        "actual_age_ms", "actual_revision_delta", "mismatch_reasons",
        "routing_authority", "source", "freshness_policy", "entries",
        "observation", "acceptance",
    }
    _fields(projection, expected, "private context projection")
    if projection["schema"] != BINDING_SCHEMA or projection["routing_authority"] is not False:
        raise ContextFreshnessError("private context projection authority is invalid")
    packet = {
        "schema": PACKET_SCHEMA,
        "source": projection["source"],
        "freshness_policy": projection["freshness_policy"],
        "entries": projection["entries"],
    }
    reproduced = bind_context(
        packet, projection["observation"], run_id=projection["run_id"],
        decision_id=projection["decision_id"],
        unix_now_ms=projection["bound_at_unix_ms"],
        accept_stale=projection["acceptance"])
    if private_projection(reproduced) != projection:
        raise ContextFreshnessError("private context projection binding is invalid")
    return reproduced


def revalidate_source(binding: BoundContext, observation: object, *,
                      unix_now_ms: int) -> bool:
    """Fail if the verified source identity changed after durable preparation."""
    if not isinstance(binding, BoundContext):
        raise ContextFreshnessError("source revalidation requires a binding")
    parsed = ParsedContext(
        binding.packet_sha256, binding.source_revision_sha256,
        binding.source, binding.freshness_policy, binding.entries)
    observed = _parse_observation(observation, parsed)
    prior = binding.observation
    identity_fields = (
        "kind", "captured_revision", "current_revision", "current_source_digest",
        "relation", "revision_delta", "verification_method")
    if any(observed[key] != prior[key] for key in identity_fields):
        raise ContextFreshnessError("context source drifted before provider launch")
    if observed["observed_at_unix_ms"] < prior["observed_at_unix_ms"]:
        raise ContextFreshnessError("context source observation clock drifted backwards")
    candidate = bind_context(
        parsed, observed, run_id=binding.run_id,
        decision_id=binding.decision_id, unix_now_ms=unix_now_ms,
        accept_stale=binding.acceptance)
    if candidate.state != binding.state:
        raise ContextFreshnessError(
            "context freshness authority drifted before provider launch")
    return True


__all__ = [
    "ACCEPTANCE_SCHEMA", "BINDING_SCHEMA", "BoundContext",
    "ContextFreshnessError", "OBSERVATION_SCHEMA", "PACKET_SCHEMA",
    "ParsedContext", "acceptance_identity", "bind_context", "parse_context_packet",
    "parse_private_projection", "private_projection", "prompt_projection",
    "public_projection", "revalidate_source",
]
