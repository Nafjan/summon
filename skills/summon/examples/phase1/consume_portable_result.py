#!/usr/bin/env python3
"""Independent, provider-free consumer for Summon's experimental portable result.

This example intentionally imports no Summon modules. It demonstrates the minimum
strict checks an external orchestrator must perform before accepting the public
projection as data. Acceptance never grants execution, routing, or native-subagent
authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


SCHEMA = "summon.portable-result/experimental-1"
MAX_BYTES = 256 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+\-]{0,127}$")
SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MEDIA_TYPE = re.compile(r"^[a-z][a-z0-9.+-]{0,63}/[a-z0-9.+-]{1,127}$")
ROOT_KEYS = {"schema", "source", "outcome", "contact", "model", "artifacts",
             "attestation", "integrity"}
STATUSES = {"success", "partial", "blocked", "error", "unknown"}
EXECUTION = {"success", "partial", "blocked", "error", "not_run", "unknown"}
ATTEMPT_STATES = {"completed", "failed", "cancelled", "timed_out", "not_run", "unknown"}
EVIDENCE = {"reported", "inferred", "absent"}
VERDICTS = {"pass", "revise", "block", "conditional", "endorse", "unknown"}
MUTATION = {"none", "observed", "unknown"}
STABILITY = {"stable", "unstable", "unknown"}
TRANSPORTS = {"subprocess", "api", "acp", "manifest", "council", "unknown"}
RETRY = {"none", "retried", "fallback"}
MAX_ARTIFACT_BYTES = 1 << 50


class InvalidPortableResult(ValueError):
    pass


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise InvalidPortableResult("duplicate JSON key")
        value[key] = item
    return value


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise InvalidPortableResult(f"invalid {label} keys")
    return value


def _sha(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _identifier(value, *, slug=False, limit=128, nullable=False):
    if value is None and nullable:
        return True
    pattern = SLUG if slug else IDENTIFIER
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(0x21 <= ord(char) <= 0x7e for char in value)
            and pattern.fullmatch(value) is not None)


def _nullable_bool(value):
    return value is None or isinstance(value, bool)


def _bounded_int(value, low, high, *, nullable=False):
    if value is None and nullable:
        return True
    return (isinstance(value, int) and not isinstance(value, bool)
            and low <= value <= high)


def _canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _same_file(left, right):
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _is_reparse(path):
    try:
        value = path.lstat()
    except OSError:
        return True
    return (path.is_symlink()
            or bool(getattr(value, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _read_bounded_regular_file(path):
    source = Path(path)
    try:
        before = source.lstat()
        if _is_reparse(source) or not stat.S_ISREG(before.st_mode):
            raise InvalidPortableResult("portable result must be a regular non-link file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if (not _same_file(before, opened)
                    or opened.st_size < 1 or opened.st_size > MAX_BYTES):
                raise InvalidPortableResult("invalid portable result byte size")
            chunks = []
            remaining = MAX_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        if (not data or len(data) > MAX_BYTES or not _same_file(opened, after)
                or opened.st_size != after.st_size or len(data) != after.st_size
                or _is_reparse(source)):
            raise InvalidPortableResult("portable result changed during read")
        return data
    except InvalidPortableResult:
        raise
    except OSError as exc:
        raise InvalidPortableResult("portable result is unavailable") from exc


def load(path):
    data = _read_bounded_regular_file(path)
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda _item: (_ for _ in ()).throw(
                InvalidPortableResult("non-finite JSON value")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidPortableResult("invalid portable result JSON") from exc
    return value


def validate(value):
    root = _object(value, ROOT_KEYS, "root")
    if root["schema"] != SCHEMA:
        raise InvalidPortableResult("unknown portable result schema")

    source = _object(root["source"], {
        "surface", "source_schema", "receipt_sha256", "binding_sha256",
        "relationship", "native_adapter_attested"}, "source")
    if (source["surface"] not in {"dispatch", "job"}
            or source["relationship"] != "external"
            or source["native_adapter_attested"] is not False
            or not _identifier(source["source_schema"])
            or not _sha(source["receipt_sha256"])):
        raise InvalidPortableResult("invalid external source relationship")
    binding = source["binding_sha256"]
    if (source["surface"] == "job") != _sha(binding):
        raise InvalidPortableResult("invalid source binding")

    outcome = _object(root["outcome"], {
        "status", "execution_status", "attempts", "attempt_status", "report_ok",
        "result_usable", "verdict", "error_kind", "raw_backend_exit_code",
        "normalized_exit_code"}, "outcome")
    if (outcome["status"] not in STATUSES
            or outcome["execution_status"] not in EXECUTION
            or outcome["attempt_status"] not in ATTEMPT_STATES
            or not _bounded_int(outcome["attempts"], 0, 100, nullable=True)
            or not _nullable_bool(outcome["report_ok"])
            or not _nullable_bool(outcome["result_usable"])
            or outcome["verdict"] not in VERDICTS | {None}
            or not _identifier(outcome["error_kind"], slug=True, limit=64,
                               nullable=True)
            or not _bounded_int(outcome["raw_backend_exit_code"],
                                -(2 ** 31), 2 ** 31 - 1, nullable=True)
            or not _bounded_int(outcome["normalized_exit_code"],
                                -(2 ** 31), 2 ** 31 - 1, nullable=True)):
        raise InvalidPortableResult("invalid outcome facts")

    contact = _object(root["contact"], {
        "provider_contacted", "retry_or_fallback"}, "contact")
    if (not _nullable_bool(contact["provider_contacted"])
            or contact["retry_or_fallback"] not in RETRY):
        raise InvalidPortableResult("invalid contact facts")

    model = _object(root["model"], {
        "requested", "targeted", "served", "served_model_evidence",
        "model_match", "named_model_verified"}, "model")
    if any(not _identifier(model[key], nullable=True)
           for key in ("requested", "targeted", "served")):
        raise InvalidPortableResult("invalid model identifier")
    reported_complete = (
        model["served_model_evidence"] == "reported"
        and all(isinstance(model[key], str) and model[key]
                for key in ("requested", "targeted", "served")))
    exact = reported_complete and model["requested"] == model["targeted"] == model["served"]
    expected_match = True if exact else (False if reported_complete else None)
    if (model["served_model_evidence"] not in EVIDENCE
            or not _nullable_bool(model["model_match"])
            or not isinstance(model["named_model_verified"], bool)
            or model["model_match"] is not expected_match
            or model["named_model_verified"] is not exact):
        raise InvalidPortableResult("forged or inconsistent model proof")

    no_contact = (contact["provider_contacted"] is False
                  or outcome["execution_status"] == "not_run"
                  or outcome["attempt_status"] == "not_run"
                  or outcome["attempts"] == 0)
    if no_contact and not (
            outcome["attempts"] == 0
            and outcome["execution_status"] == "not_run"
            and outcome["attempt_status"] == "not_run"
            and contact["provider_contacted"] is False
            and contact["retry_or_fallback"] == "none"
            and outcome["status"] not in {"success", "partial"}
            and outcome["report_ok"] is not True
            and outcome["result_usable"] is not True
            and outcome["verdict"] is None
            and model["served"] is None
            and model["served_model_evidence"] == "absent"
            and model["model_match"] is None
            and model["named_model_verified"] is False):
        raise InvalidPortableResult("invalid no-contact projection")

    artifacts = _object(root["artifacts"], {
        "workspace_mutation", "artifact_stability", "bytes", "files"}, "artifacts")
    if (artifacts["workspace_mutation"] not in MUTATION
            or artifacts["artifact_stability"] not in STABILITY
            or not _bounded_int(artifacts["bytes"], 0, MAX_ARTIFACT_BYTES,
                                nullable=True)
            or not isinstance(artifacts["files"], list)
            or len(artifacts["files"]) > 64):
        raise InvalidPortableResult("invalid artifact list")
    for item in artifacts["files"]:
        if (not isinstance(item, dict)
                or set(item) not in ({"sha256"}, {"sha256", "bytes"},
                                     {"sha256", "media_type"},
                                     {"sha256", "bytes", "media_type"})
                or not _sha(item["sha256"])
                or ("bytes" in item and not _bounded_int(
                    item["bytes"], 0, MAX_ARTIFACT_BYTES))
                or ("media_type" in item and (
                    not isinstance(item["media_type"], str)
                    or MEDIA_TYPE.fullmatch(item["media_type"]) is None))):
            raise InvalidPortableResult("invalid artifact identity")
    if no_contact and not (
            artifacts["workspace_mutation"] == "unknown"
            and artifacts["artifact_stability"] == "unknown"
            and artifacts["bytes"] is None
            and artifacts["files"] == []):
        raise InvalidPortableResult("invalid no-contact artifacts")

    attestation = _object(root["attestation"], {
        "agent", "provider", "backend", "transport"}, "attestation")
    if (any(not _identifier(attestation[key], slug=True, limit=64, nullable=True)
            for key in ("agent", "provider", "backend"))
            or attestation["transport"] not in TRANSPORTS):
        raise InvalidPortableResult("invalid attestation")
    integrity = _object(root["integrity"], {
        "summon_version", "scripts_sha256", "projection_sha256"}, "integrity")
    if (not _identifier(integrity["summon_version"], limit=64, nullable=True)
            or (integrity["scripts_sha256"] is not None
                and not _sha(integrity["scripts_sha256"]))
            or not _sha(integrity["projection_sha256"])):
        raise InvalidPortableResult("invalid projection digest")
    unsigned = json.loads(json.dumps(root))
    unsigned["integrity"].pop("projection_sha256")
    if _canonical_digest(unsigned) != integrity["projection_sha256"]:
        raise InvalidPortableResult("projection digest mismatch")
    return root


def consume(value):
    projection = validate(value)
    return {
        "schema": "summon.portable-consumer/experimental-1",
        "status": "accepted",
        "authority_granted": False,
        "source_surface": projection["source"]["surface"],
        "source_receipt_sha256": projection["source"]["receipt_sha256"],
        "projection_sha256": projection["integrity"]["projection_sha256"],
        "execution_status": projection["outcome"]["execution_status"],
        "result_usable": projection["outcome"]["result_usable"],
        "named_model_verified": projection["model"]["named_model_verified"],
        "artifact_count": len(projection["artifacts"]["files"]),
    }


def main(argv):
    if len(argv) != 2:
        print("usage: consume_portable_result.py FILE", file=sys.stderr)
        return 2
    try:
        result = consume(load(argv[1]))
    except (OSError, InvalidPortableResult):
        print("portable result rejected", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
