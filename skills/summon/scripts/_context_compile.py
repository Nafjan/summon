"""Provider-inert, lossless context compilation for Summon Phase 1.

This module deliberately has no provider, process, network, credential, routing, or
session-reuse surface.  It compiles a small, versioned input envelope into a prompt
envelope suitable for inspection before another layer dispatches it.  The safe
profile is mechanical only: authority-bearing turns are copied byte-for-byte and in
order; only explicitly typed payloads can be deduplicated, externally referenced, or
tail-bounded.

The ``off`` profile is a compatibility escape hatch.  When supplied with the caller's
legacy serialized bytes, it returns those exact bytes rather than reserializing them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any, Mapping


INPUT_SCHEMA = "summon.context-input/v1"
OUTPUT_SCHEMA = "summon.context-output/v1"
LINEAGE_SCHEMA = "summon.context-lineage/v1"
TOKEN_ESTIMATE_METHOD = "utf8_bytes_div_4_ceiling/v1"
RECEIPT_PROOF_SCHEMA = "summon.context-receipt-proof/v1"
RECEIPT_BINDING_SCHEMA = "summon.context-receipt-binding/v1"
REFERENCE_PROOF_SCHEMA = "summon.context-reference-proof/v1"

MAX_BLOCKS = 256
MAX_BLOCK_BYTES = 512 * 1024
MAX_SERIALIZED_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_TAIL_BYTES = 16 * 1024
DEFAULT_DIAGNOSTIC_TAIL_BYTES = 2048

_AUTHORITY_KINDS = frozenset({
    "system_instruction", "developer_instruction", "user_instruction",
    "approval", "permission_boundary", "data_boundary", "spend_boundary",
    "report_contract",
})
_IMMUTABLE_KINDS = frozenset({"immutable_artifact", "validated_report"})
_DIAGNOSTIC_KINDS = frozenset({"diagnostic_chatter", "tool_chatter"})
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")


class ContextCompileError(ValueError):
    """A typed, public-safe compiler refusal."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class _VerifiedReferenceProof:
    """Opaque adapter-minted reference proof.

    A plain mapping is caller data, not evidence that a local target was opened
    and hash-read.  The trusted filesystem adapter is the only production
    caller of the private minting function below.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Mapping[str, Any], token: object):
        if token is not _REFERENCE_PROOF_TOKEN:
            raise ContextCompileError(
                "reference_proof_invalid", "reference proof was not minted by the target adapter")
        self._value = value


_REFERENCE_PROOF_TOKEN = object()


def _mint_verified_reference_proof(value: Mapping[str, Any]) -> _VerifiedReferenceProof:
    return _VerifiedReferenceProof(value, _REFERENCE_PROOF_TOKEN)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError, OverflowError) as exc:
        raise ContextCompileError("context_malformed", "context is not canonical JSON") from exc


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _reject_constant(value: str) -> None:
    raise ContextCompileError("context_nonfinite", "context contains a non-finite number")


def _parse_float(value: str) -> float:
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise ContextCompileError("context_malformed", "context number is invalid") from exc
    if not math.isfinite(parsed):
        raise ContextCompileError("context_nonfinite", "context contains a non-finite number")
    return parsed


def parse_context_json(raw: bytes | str) -> dict[str, Any]:
    """Parse a bounded JSON context while rejecting duplicate keys and NaN values."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_SERIALIZED_BYTES:
            raise ContextCompileError("context_too_large", "context exceeds the size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextCompileError("context_malformed", "context must be UTF-8") from exc
    elif isinstance(raw, str):
        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ContextCompileError("context_malformed", "context must be UTF-8") from exc
        if raw_size > MAX_SERIALIZED_BYTES:
            raise ContextCompileError("context_too_large", "context exceeds the size limit")
        text = raw
    else:
        raise ContextCompileError("context_malformed", "context must be JSON text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ContextCompileError("context_duplicate_key", "context contains a duplicate key")
            output[key] = value
        return output

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=_reject_constant,
                           parse_float=_parse_float)
    except ContextCompileError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeError,
            RecursionError, OverflowError) as exc:
        raise ContextCompileError("context_malformed", "context is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ContextCompileError("context_malformed", "context root must be an object")
    _check_finite(value)
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContextCompileError("context_malformed", f"{field} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContextCompileError("context_malformed", f"{field} is not UTF-8 text") from exc
    if len(encoded) > MAX_BLOCK_BYTES:
        raise ContextCompileError("context_too_large", f"{field} exceeds the size limit")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContextCompileError("context_malformed", f"{field} is invalid")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ContextCompileError("context_malformed", f"{field} must be a SHA-256 digest")
    return value


def _snapshot_mapping(value: Any, kind: str, depth: int = 0) -> dict[Any, Any]:
    """Recursively snapshot proof mappings before validating any of their fields."""
    if depth > 16:
        raise ContextCompileError(kind, "proof exceeds nesting limit")
    if not isinstance(value, Mapping):
        raise ContextCompileError(kind, "proof must be an object")
    try:
        snapshot = dict(value)
    except (KeyError, RuntimeError, TypeError, ValueError, RecursionError) as exc:
        raise ContextCompileError(kind, "proof could not be snapshotted") from exc
    output: dict[Any, Any] = {}
    for key, item in snapshot.items():
        if isinstance(item, Mapping):
            output[key] = _snapshot_mapping(item, kind, depth + 1)
        elif isinstance(item, list):
            if len(item) > 4096:
                raise ContextCompileError(kind, "proof exceeds item limit")
            output[key] = [
                _snapshot_mapping(entry, kind, depth + 1)
                if isinstance(entry, Mapping) else entry for entry in item]
        else:
            output[key] = item
    return output


def _content_reference(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ContextCompileError("context_malformed", "artifact reference must be content-addressed")
    _digest(value[7:], "artifact reference")
    return value


def _receipt_binding(report_id: str, report_body_sha256: str,
                     receipt_sha256: str) -> str:
    return _sha256(_canonical({
        "schema": RECEIPT_BINDING_SCHEMA,
        "report_id": report_id,
        "report_body_sha256": report_body_sha256,
        "receipt_sha256": receipt_sha256,
    }))


def make_receipt_proof(report_id: str, report_body: str,
                       receipt_sha256: str) -> dict[str, Any]:
    """Create the canonical provider-inert linkage used for diagnostic truncation."""
    checked_id = _identifier(report_id, "report id")
    checked_body = _text(report_body, "report body")
    checked_receipt = _digest(receipt_sha256, "receipt sha256")
    body_digest = _sha256(checked_body)
    return {
        "schema": RECEIPT_PROOF_SCHEMA,
        "report_id": checked_id,
        "report_body_sha256": body_digest,
        "receipt_sha256": checked_receipt,
        "binding_sha256": _receipt_binding(checked_id, body_digest, checked_receipt),
    }


def _receipt_proof(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    proof = _snapshot_mapping(value, "receipt_proof_invalid")
    expected = {
        "schema", "report_id", "report_body_sha256", "receipt_sha256",
        "binding_sha256",
    }
    if set(proof) != expected:
        raise ContextCompileError("receipt_proof_invalid", "diagnostic receipt proof is invalid")
    if proof.get("schema") != RECEIPT_PROOF_SCHEMA:
        raise ContextCompileError("receipt_proof_invalid", "diagnostic receipt proof schema is unsupported")
    report_id = _identifier(proof.get("report_id"), "receipt report id")
    report_digest = _digest(proof.get("report_body_sha256"), "report body sha256")
    receipt_digest = _digest(proof.get("receipt_sha256"), "receipt sha256")
    binding = _digest(proof.get("binding_sha256"), "receipt binding sha256")
    if binding != _receipt_binding(report_id, report_digest, receipt_digest):
        raise ContextCompileError("receipt_proof_invalid", "diagnostic receipt linkage is invalid")
    return {
        "schema": RECEIPT_PROOF_SCHEMA, "report_id": report_id,
        "report_body_sha256": report_digest, "receipt_sha256": receipt_digest,
        "binding_sha256": binding,
    }


def _check_finite(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ContextCompileError("context_too_deep", "context exceeds nesting limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContextCompileError("context_nonfinite", "context contains a non-finite number")
    if isinstance(value, Mapping):
        if len(value) > 4096:
            raise ContextCompileError("context_too_large", "context object exceeds item limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContextCompileError("context_malformed", "context object key must be text")
            _check_finite(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 4096:
            raise ContextCompileError("context_too_large", "context list exceeds item limit")
        for item in value:
            _check_finite(item, depth + 1)


def _snapshot_context_value(value: Any, depth: int = 0) -> Any:
    """Freeze caller-owned JSON containers before any later validation reads."""
    if depth > 64:
        raise ContextCompileError("context_too_deep", "context exceeds nesting limit")
    if isinstance(value, Mapping):
        try:
            snapshot = dict(value)
        except (KeyError, RuntimeError, TypeError, ValueError, RecursionError) as exc:
            raise ContextCompileError("context_malformed", "context could not be snapshotted") from exc
        if len(snapshot) > 4096:
            raise ContextCompileError("context_too_large", "context object exceeds item limit")
        return {key: _snapshot_context_value(item, depth + 1)
                for key, item in snapshot.items()}
    if isinstance(value, list):
        if len(value) > 4096:
            raise ContextCompileError("context_too_large", "context list exceeds item limit")
        return [_snapshot_context_value(item, depth + 1) for item in value]
    return value


def _parse_context(value: Mapping[str, Any] | bytes | str) -> dict[str, Any]:
    if isinstance(value, (bytes, str)):
        value = parse_context_json(value)
    if not isinstance(value, Mapping):
        raise ContextCompileError("context_malformed", "context must be an object")
    copied = _snapshot_context_value(value)
    _check_finite(copied)
    if set(copied) != {"schema", "blocks"} or copied.get("schema") != INPUT_SCHEMA:
        raise ContextCompileError("context_schema_unsupported", "context schema is unsupported")
    blocks = copied.get("blocks")
    if not isinstance(blocks, list) or not blocks or len(blocks) > MAX_BLOCKS:
        raise ContextCompileError("context_malformed", "context blocks are invalid")
    return {"schema": INPUT_SCHEMA, "blocks": [dict(item) if isinstance(item, Mapping) else item for item in blocks]}


def _proofs(value: _VerifiedReferenceProof | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, _VerifiedReferenceProof):
        raise ContextCompileError(
            "reference_proof_invalid", "reference proof was not minted by the target adapter")
    top = _snapshot_mapping(value._value, "reference_proof_invalid")
    if set(top) != {"schema", "references"}:
        raise ContextCompileError("reference_proof_invalid", "reference proof is invalid")
    if top.get("schema") != REFERENCE_PROOF_SCHEMA:
        raise ContextCompileError("reference_proof_invalid", "reference proof schema is unsupported")
    refs = _snapshot_mapping(top.get("references"), "reference_proof_invalid")
    if len(refs) > MAX_BLOCKS:
        raise ContextCompileError("reference_proof_invalid", "reference proof is invalid")
    output: dict[str, dict[str, Any]] = {}
    for ref, raw_proof in refs.items():
        _content_reference(ref)
        proof = _snapshot_mapping(raw_proof, "reference_proof_invalid")
        expected_fields = {
            "reference", "sha256", "verified_target", "verification_method",
            "target_locator",
        }
        if not isinstance(proof, Mapping) or set(proof) != expected_fields:
            raise ContextCompileError("reference_proof_invalid", "reference proof is invalid")
        if proof.get("reference") != ref or proof.get("verified_target") is not True:
            raise ContextCompileError("reference_proof_invalid", "reference target is not verified")
        digest = _digest(proof.get("sha256"), "reference sha256")
        if proof.get("verification_method") != "sha256-readback":
            raise ContextCompileError("reference_proof_invalid", "reference hash verification is unsupported")
        locator = _snapshot_mapping(proof.get("target_locator"), "reference_proof_invalid")
        if set(locator) != {"root", "path"}:
            raise ContextCompileError("reference_proof_invalid", "reference target locator is invalid")
        root = locator.get("root")
        path = locator.get("path")
        if (root != "cwd" or not isinstance(path, str) or not path
                or "\\" in path or path.startswith(("/", "../"))
                or "/../" in path or re.match(r"^[A-Za-z]:", path)):
            raise ContextCompileError("reference_proof_invalid", "reference target locator is invalid")
        output[ref] = {
            "reference": ref, "sha256": digest, "verified_target": True,
            "verification_method": "sha256-readback",
            "target_locator": {"root": root, "path": path},
        }
    return output


def _authority(block: Mapping[str, Any], index: int) -> dict[str, Any]:
    if set(block) != {"id", "plane", "kind", "body"}:
        raise ContextCompileError("context_malformed", "authority block shape is invalid")
    block_id = _identifier(block.get("id"), "block id")
    if block.get("plane") != "authority" or block.get("kind") not in _AUTHORITY_KINDS:
        raise ContextCompileError("context_malformed", "authority block type is invalid")
    body = _text(block.get("body"), "authority body")
    return {"id": block_id, "plane": "authority", "kind": block["kind"], "body": body,
            "_index": index}


def _immutable(block: Mapping[str, Any], index: int) -> dict[str, Any]:
    allowed = {"id", "plane", "kind", "body", "immutable", "stable", "externalize", "artifact_ref"}
    if set(block) - allowed or not {"id", "plane", "kind", "body", "immutable", "stable"} <= set(block):
        raise ContextCompileError("context_malformed", "immutable payload block shape is invalid")
    block_id = _identifier(block.get("id"), "block id")
    if block.get("plane") != "payload" or block.get("kind") not in _IMMUTABLE_KINDS:
        raise ContextCompileError("context_malformed", "immutable payload type is invalid")
    if block.get("immutable") is not True or not isinstance(block.get("stable"), bool):
        raise ContextCompileError("context_malformed", "immutable payload flags are invalid")
    if not isinstance(block.get("externalize", False), bool):
        raise ContextCompileError("context_malformed", "payload externalize flag is invalid")
    if "artifact_ref" in block:
        _content_reference(block["artifact_ref"])
    if block.get("externalize") is True and (not block.get("stable") or "artifact_ref" not in block):
        raise ContextCompileError("reference_proof_required", "stable reference proof is required")
    return {
        "id": block_id, "plane": "payload", "kind": block["kind"],
        "body": _text(block.get("body"), "payload body"),
        "immutable": True, "stable": block["stable"],
        "externalize": bool(block.get("externalize", False)),
        "artifact_ref": block.get("artifact_ref"), "_index": index,
    }


def _diagnostic(block: Mapping[str, Any], index: int) -> dict[str, Any]:
    allowed = {"id", "plane", "kind", "body", "typed_errors", "receipt_proof"}
    if set(block) - allowed or not {"id", "plane", "kind", "body", "typed_errors"} <= set(block):
        raise ContextCompileError("context_malformed", "diagnostic block shape is invalid")
    block_id = _identifier(block.get("id"), "block id")
    errors = block.get("typed_errors")
    if (block.get("plane") != "payload" or block.get("kind") not in _DIAGNOSTIC_KINDS
            or not isinstance(errors, list) or len(errors) > 128):
        raise ContextCompileError("context_malformed", "diagnostic block type is invalid")
    checked_errors: list[str] = []
    for error in errors:
        if not isinstance(error, str) or not _ERROR_RE.fullmatch(error):
            raise ContextCompileError("context_malformed", "typed error is invalid")
        checked_errors.append(error)
    proof = _receipt_proof(block.get("receipt_proof"))
    return {"id": block_id, "plane": "payload", "kind": block["kind"],
            "body": _text(block.get("body"), "diagnostic body"),
            "typed_errors": checked_errors, "receipt_proof": proof, "_index": index}


def _ordinary(block: Mapping[str, Any], index: int) -> dict[str, Any]:
    if set(block) != {"id", "plane", "kind", "body"}:
        raise ContextCompileError("context_malformed", "payload block shape is invalid")
    block_id = _identifier(block.get("id"), "block id")
    kind = _identifier(block.get("kind"), "payload kind")
    if block.get("plane") != "payload" or kind in _AUTHORITY_KINDS | _IMMUTABLE_KINDS | _DIAGNOSTIC_KINDS:
        raise ContextCompileError("context_malformed", "payload block type is invalid")
    return {"id": block_id, "plane": "payload", "kind": kind,
            "body": _text(block.get("body"), "payload body"), "_index": index}


def _blocks(context: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    ids: set[str] = set()
    aggregate_body_bytes = 0
    for index, raw in enumerate(context["blocks"]):
        if not isinstance(raw, Mapping):
            raise ContextCompileError("context_malformed", "context block must be an object")
        plane = raw.get("plane")
        kind = raw.get("kind")
        if plane == "authority":
            parsed = _authority(raw, index)
        elif kind in _IMMUTABLE_KINDS:
            parsed = _immutable(raw, index)
        elif kind in _DIAGNOSTIC_KINDS:
            parsed = _diagnostic(raw, index)
        else:
            parsed = _ordinary(raw, index)
        if parsed["id"] in ids:
            raise ContextCompileError("context_duplicate_block", "context contains a duplicate block id")
        aggregate_body_bytes += len(parsed["body"].encode("utf-8"))
        if aggregate_body_bytes > MAX_SERIALIZED_BYTES:
            raise ContextCompileError("context_too_large", "aggregate context bodies exceed the size limit")
        ids.add(parsed["id"])
        output.append(parsed)
    return output


def _tail_utf8(body: str, maximum: int) -> tuple[str, int]:
    raw = body.encode("utf-8")
    if len(raw) <= maximum:
        return body, 0
    start = len(raw) - maximum
    while start < len(raw) and raw[start] & 0xC0 == 0x80:
        start += 1
    tail = raw[start:].decode("utf-8")
    return tail, start


def _estimate(size: int) -> int:
    return (size + 3) // 4


def _rollback(source: bytes) -> dict[str, Any]:
    return {
        "schema": "summon.context-rollback/v1",
        "source_sha256": _sha256(source),
        "source_utf8_b64": base64.b64encode(source).decode("ascii"),
    }


def _block_mapping(source_block: Mapping[str, Any], output_block: Mapping[str, Any],
                   *, block_id: str, action: str, represented_body_sha256: str) -> dict[str, Any]:
    return {
        "source_id": block_id, "output_id": block_id, "action": action,
        "hash_domain": "canonical-json-block/v1",
        "source_block_sha256": _sha256(_canonical(dict(source_block))),
        "output_block_sha256": _sha256(_canonical(dict(output_block))),
        "source_body_sha256": represented_body_sha256,
        "represented_body_sha256": represented_body_sha256,
    }


def _retained_base(source_block: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": block["id"], "kind": block["kind"],
        "source_block_sha256": _sha256(_canonical(dict(source_block))),
        "body_sha256": _sha256(block["body"]),
    }


def _off_result(source: bytes, parsed: dict[str, Any],
                blocks: list[dict[str, Any]]) -> dict[str, Any]:
    mapping = []
    retained = []
    for block in blocks:
        source_block = parsed["blocks"][block["_index"]]
        mapping.append(_block_mapping(
            source_block, source_block, block_id=block["id"], action="preserved",
            represented_body_sha256=_sha256(block["body"])))
        retained.append({**_retained_base(source_block, block), "retention": "preserved"})
    lineage = _sha256(_canonical({
        "schema": LINEAGE_SCHEMA, "profile": "off",
        "source_sha256": _sha256(source), "mapping": mapping,
        "retained_evidence": retained,
    }))
    return {
        "schema": OUTPUT_SCHEMA, "profile": "off", "provider_contacted": False,
        "compiled_utf8": source.decode("utf-8"), "source_to_output": mapping,
        "before_bytes": len(source), "after_bytes": len(source),
        "token_estimate": {"method": TOKEN_ESTIMATE_METHOD, "before": _estimate(len(source)),
                           "after": _estimate(len(source))},
        "lineage_sha256": lineage, "retained_evidence": retained,
        "rollback": _rollback(source),
    }


def _compile_context(context: Mapping[str, Any] | bytes | str, *, profile: str = "safe",
                     reference_proof: Mapping[str, Any] | None = None,
                     diagnostic_tail_bytes: int = DEFAULT_DIAGNOSTIC_TAIL_BYTES,
                     legacy_serialized: bytes | str | None = None) -> dict[str, Any]:
    """Compile a context without contacting a provider.

    ``reference_proof`` is required for every payload block that requests
    ``externalize: true``.  It is checked before any output is produced, so a caller
    can fail before dispatch rather than accidentally sending an unresolved reference.
    """
    parsed = _parse_context(context)
    blocks = _blocks(parsed)
    if profile not in {"safe", "off"}:
        raise ContextCompileError("context_profile_unsupported", "context profile is unsupported")
    if (not isinstance(diagnostic_tail_bytes, int) or isinstance(diagnostic_tail_bytes, bool)
            or not 0 <= diagnostic_tail_bytes <= MAX_DIAGNOSTIC_TAIL_BYTES):
        raise ContextCompileError("context_malformed", "diagnostic tail limit is invalid")

    canonical_source = _canonical(parsed)
    if isinstance(context, bytes):
        true_source = context
    elif isinstance(context, str):
        true_source = context.encode("utf-8")
    else:
        true_source = canonical_source
    if len(true_source) > MAX_SERIALIZED_BYTES:
        raise ContextCompileError("context_too_large", "context exceeds the size limit")
    if legacy_serialized is None:
        legacy = true_source
    elif isinstance(legacy_serialized, bytes):
        legacy = legacy_serialized
    elif isinstance(legacy_serialized, str):
        legacy = legacy_serialized.encode("utf-8")
    else:
        raise ContextCompileError("context_malformed", "legacy serialization is invalid")
    if len(legacy) > MAX_SERIALIZED_BYTES:
        raise ContextCompileError("context_too_large", "legacy serialization exceeds the size limit")
    try:
        legacy.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextCompileError("context_malformed", "legacy serialization must be UTF-8") from exc
    if legacy != true_source:
        raise ContextCompileError(
            "legacy_serialization_mismatch",
            "legacy serialization does not match the actual context input")
    if profile == "off":
        return _off_result(legacy, parsed, blocks)

    proofs = _proofs(reference_proof)
    for block in blocks:
        if block.get("externalize"):
            proof = proofs.get(block["artifact_ref"])
            body_hash = _sha256(block["body"])
            if (proof is None or proof["sha256"] != body_hash
                    or block["artifact_ref"][7:] != body_hash):
                raise ContextCompileError("reference_proof_required", "stable reference cannot be resolved and hash-checked")

    compiled_blocks: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    # Include the exact body in the key; a digest match alone is not treated as
    # proof of byte identity for the deduplication decision.
    first_immutable: dict[tuple[str, str], str] = {}
    block_by_id = {block["id"]: block for block in blocks}
    for block in blocks:
        body_hash = _sha256(block["body"])
        evidence_base = _retained_base(parsed["blocks"][block["_index"]], block)
        output: dict[str, Any]
        action = "preserved"
        if block["plane"] == "authority":
            # Do not normalize, trim, reorder, deduplicate, or externalize authority.
            output = {"id": block["id"], "plane": "authority", "kind": block["kind"],
                      "body": block["body"]}
            retained.append({**evidence_base, "retention": "authority"})
        elif block["kind"] in _IMMUTABLE_KINDS and block.get("externalize"):
            action = "referenced"
            output = {"id": block["id"], "plane": "payload", "kind": block["kind"],
                      "artifact_ref": block["artifact_ref"], "sha256": body_hash,
                      "target_verified": True,
                      "verification_method": "sha256-readback",
                      "target_locator": dict(proofs[block["artifact_ref"]]["target_locator"])}
            retained.append({**evidence_base, "retention": "stable_reference",
                             "reference": block["artifact_ref"]})
        elif block["kind"] in _IMMUTABLE_KINDS:
            key = (block["kind"], block["body"])
            prior = first_immutable.get(key)
            if prior is not None:
                action = "deduplicated"
                output = {"id": block["id"], "plane": "payload", "kind": block["kind"],
                          "dedupe_of": prior, "sha256": body_hash}
                retained.append({**evidence_base, "retention": "dedupe_reference",
                                 "reference": prior})
            else:
                first_immutable[key] = block["id"]
                output = {"id": block["id"], "plane": "payload", "kind": block["kind"],
                          "body": block["body"], "immutable": True, "sha256": body_hash}
                retained.append({**evidence_base, "retention": "immutable_payload"})
        elif block["kind"] in _DIAGNOSTIC_KINDS:
            tail, omitted = _tail_utf8(block["body"], diagnostic_tail_bytes)
            if omitted:
                proof = block.get("receipt_proof")
                if not block["typed_errors"] or proof is None:
                    raise ContextCompileError(
                        "diagnostic_representation_required",
                        "diagnostic truncation requires typed errors and receipt proof")
                report = block_by_id.get(proof["report_id"])
                if (report is None or report.get("kind") != "validated_report"
                        or report.get("immutable") is not True
                        or report.get("_index", -1) >= block["_index"]
                        or _sha256(report["body"]) != proof["report_body_sha256"]):
                    raise ContextCompileError(
                        "diagnostic_representation_required",
                        "diagnostic truncation requires a linked preceding validated report")
            action = "tail_bounded" if omitted else "diagnostic_wrapped"
            output = {"id": block["id"], "plane": "payload", "kind": block["kind"],
                      "body_tail": tail, "content_sha256": body_hash,
                      "omitted_bytes": omitted, "typed_errors": list(block["typed_errors"])}
            if omitted:
                output["receipt_proof"] = dict(proof)
            evidence = {**evidence_base, "retention": "diagnostic",
                        "typed_errors": list(block["typed_errors"])}
            if omitted:
                evidence["receipt_proof"] = dict(proof)
            retained.append(evidence)
        else:
            output = {"id": block["id"], "plane": "payload", "kind": block["kind"],
                      "body": block["body"]}
            retained.append({**evidence_base, "retention": "payload"})
        compiled_blocks.append(output)
        mapping.append(_block_mapping(
            parsed["blocks"][block["_index"]], output, block_id=block["id"], action=action,
            represented_body_sha256=body_hash))

    compiled = {"schema": OUTPUT_SCHEMA, "profile": "safe", "blocks": compiled_blocks}
    compiled_bytes = _canonical(compiled)
    lineage = _sha256(_canonical({"schema": LINEAGE_SCHEMA, "profile": "safe",
                                  "source_sha256": _sha256(legacy), "compiled_sha256": _sha256(compiled_bytes),
                                  "mapping": mapping, "retained_evidence": retained}))
    return {
        "schema": OUTPUT_SCHEMA, "profile": "safe", "provider_contacted": False,
        "compiled_utf8": compiled_bytes.decode("utf-8"), "source_to_output": mapping,
        "before_bytes": len(legacy), "after_bytes": len(compiled_bytes),
        "token_estimate": {"method": TOKEN_ESTIMATE_METHOD, "before": _estimate(len(legacy)),
                           "after": _estimate(len(compiled_bytes))},
        "lineage_sha256": lineage, "retained_evidence": retained,
        "rollback": _rollback(legacy),
    }


def compile_context(context: Mapping[str, Any] | bytes | str, *, profile: str = "safe",
                    reference_proof: _VerifiedReferenceProof | None = None,
                    diagnostic_tail_bytes: int = DEFAULT_DIAGNOSTIC_TAIL_BYTES,
                    legacy_serialized: bytes | str | None = None) -> dict[str, Any]:
    """Typed public entry point; hostile runtime/parser failures never escape raw."""
    try:
        return _compile_context(
            context, profile=profile, reference_proof=reference_proof,
            diagnostic_tail_bytes=diagnostic_tail_bytes,
            legacy_serialized=legacy_serialized)
    except ContextCompileError:
        raise
    except UnicodeEncodeError as exc:
        raise ContextCompileError("context_malformed", "context must be UTF-8") from exc
    except RecursionError as exc:
        raise ContextCompileError("context_too_deep", "context exceeds nesting limit") from exc
    except OverflowError as exc:
        raise ContextCompileError("context_nonfinite", "context number is out of range") from exc
