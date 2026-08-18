#!/usr/bin/env python3
"""Validate the explicitly reviewed live-provider evidence packet (schema 2).

The release runner never guesses that a provider is available.  A real pilot
must first write a small, redacted JSON receipt and point this command at it via
``SUMMON_LIVE_PROVIDER_RECEIPT``.  The packet includes a normal decision plus
cancel and deadline safety cases; missing or malformed evidence is a bounded
blocked result, never a successful test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9_.:/@+() -]{1,160}$")
REQUIRED = {
    "provider", "backend", "model_requested", "model_served", "profile",
    "transport", "owner_generation", "attempts", "provider_calls",
    "account_evidence_sha256", "consent", "cleanup", "uncertain_spend", "receipt_sha256",
    "no_retry_fallback", "owner_fence", "deadline_fence", "cancel_fence",
    "kill_switch", "no_orphans", "safety_matrix",
}
ALLOWED = REQUIRED | {"schema"}
MATRIX_CASES = frozenset({"normal", "cancel", "deadline"})
MATRIX_REQUIRED = frozenset({
    "status", "attempts", "provider_calls", "uncertain_spend",
    "cleanup_verified", "cleanup_clean", "no_orphans",
    "no_post_deadline_contact", "no_retry_fallback", "no_fallback",
})
MATRIX_STATUSES = {
    "normal": frozenset({"decided"}),
    "cancel": frozenset({"cancelled", "failed", "adapter_indeterminate"}),
    "deadline": frozenset({"timed_out", "failed", "adapter_indeterminate"}),
}


def _blocked(kind: str, detail: str) -> int:
    print(json.dumps({
        "schema": 2, "gate": "live_provider", "status": "blocked",
        "error_kind": kind, "detail": detail,
    }, sort_keys=True))
    return 3


def _safe_text(value: object) -> bool:
    return isinstance(value, str) and SAFE_TEXT_RE.fullmatch(value) is not None


def validate(path: Path) -> tuple[bool, str]:
    try:
        if path.is_symlink() or path.stat().st_size > 32 * 1024:
            return False, "evidence file is symlinked or oversized"
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False, "evidence file cannot be read as JSON"
    if not isinstance(value, dict) or value.get("schema") != 2:
        return False, "evidence schema must be 2"
    unknown = sorted(set(value) - ALLOWED)
    if unknown:
        return False, "unknown evidence fields: " + ", ".join(unknown)
    missing = sorted(REQUIRED - set(value))
    if missing:
        return False, "missing evidence fields: " + ", ".join(missing)
    for key in ("provider", "backend", "model_requested", "model_served", "profile", "transport"):
        if not _safe_text(value.get(key)):
            return False, f"invalid {key}"
    if (isinstance(value.get("owner_generation"), bool)
            or not isinstance(value.get("owner_generation"), int)
            or value["owner_generation"] <= 0):
        return False, "owner_generation must be a positive integer"
    for key in ("attempts", "provider_calls"):
        if (isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
                or value[key] < 1 or value[key] > 64):
            return False, f"invalid {key}"
    if value.get("uncertain_spend") is not False:
        return False, "pilot must prove uncertain_spend=false"
    for key in ("no_retry_fallback", "owner_fence", "deadline_fence",
                "cancel_fence", "kill_switch", "no_orphans"):
        if value.get(key) is not True:
            return False, f"{key} evidence is required"
    consent = value.get("consent")
    if not isinstance(consent, dict) or consent.get("explicit") is not True:
        return False, "explicit consent evidence is required"
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("verified") is not True or cleanup.get("clean") is not True:
        return False, "verified clean cleanup is required"
    retained = cleanup.get("retained_resources", [])
    if retained != []:
        return False, "pilot cleanup retained resources"
    matrix = value.get("safety_matrix")
    if not isinstance(matrix, dict) or set(matrix) != MATRIX_CASES:
        return False, "safety_matrix must contain normal, cancel, and deadline cases"
    for case, expected_statuses in MATRIX_STATUSES.items():
        sample = matrix.get(case)
        if not isinstance(sample, dict) or set(sample) != MATRIX_REQUIRED:
            return False, f"{case} safety evidence fields are incomplete"
        if sample.get("status") not in expected_statuses:
            return False, f"{case} safety status is not accepted"
        for key in ("cleanup_verified", "cleanup_clean", "no_orphans",
                    "no_post_deadline_contact", "no_retry_fallback", "no_fallback"):
            if sample.get(key) is not True:
                return False, f"{case} safety evidence is incomplete"
        for key in ("attempts", "provider_calls"):
            number = sample.get(key)
            if (isinstance(number, bool) or not isinstance(number, int)
                    or number < 0 or number > 64):
                return False, f"{case} {key} is invalid"
        if not isinstance(sample.get("uncertain_spend"), bool):
            return False, f"{case} uncertain_spend is invalid"
    normal = matrix["normal"]
    if normal["uncertain_spend"] is not False:
        return False, "normal safety case must prove uncertain_spend=false"
    if normal["attempts"] != value["attempts"] or normal["provider_calls"] != value["provider_calls"]:
        return False, "normal safety counts do not match the receipt"
    if matrix["deadline"]["uncertain_spend"] is not True:
        return False, "deadline safety case must be conservative about spend"
    receipt_sha = value.get("receipt_sha256")
    if not isinstance(receipt_sha, str) or not HASH_RE.fullmatch(receipt_sha):
        return False, "receipt_sha256 must be a lowercase sha256"
    account_evidence_sha = value.get("account_evidence_sha256")
    if not isinstance(account_evidence_sha, str) or not HASH_RE.fullmatch(account_evidence_sha):
        return False, "account_evidence_sha256 must be a lowercase sha256"
    # No raw prompts, paths, argv, credentials, or model output are accepted in
    # the release packet.  The packet is identity and safety evidence only.
    forbidden = {"prompt", "result", "output", "argv", "cwd", "env", "api_key", "token", "secret"}
    if forbidden & set(value):
        return False, "evidence contains forbidden raw fields"
    return True, hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    raw = os.environ.get("SUMMON_LIVE_PROVIDER_RECEIPT")
    if not raw:
        return _blocked("live_provider_evidence_missing", "set SUMMON_LIVE_PROVIDER_RECEIPT to a reviewed redacted pilot receipt")
    ok, detail = validate(Path(raw))
    if not ok:
        return _blocked("live_provider_evidence_invalid", detail)
    print(json.dumps({
        "schema": 2, "gate": "live_provider", "status": "pass",
        "artifact_sha256": detail, "evidence_file": "redacted-live-provider-receipt.json",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
