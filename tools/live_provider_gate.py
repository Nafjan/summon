#!/usr/bin/env python3
"""Validate the explicitly reviewed live-provider evidence packet.

The release runner never guesses that a provider is available.  A real pilot
must first write a small, redacted JSON receipt and point this command at it via
``SUMMON_LIVE_PROVIDER_RECEIPT``.  Missing or malformed evidence is a bounded
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
    "consent", "cleanup", "uncertain_spend", "receipt_sha256",
}


def _blocked(kind: str, detail: str) -> int:
    print(json.dumps({
        "schema": 1, "gate": "live_provider", "status": "blocked",
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
    if not isinstance(value, dict) or value.get("schema") != 1:
        return False, "evidence schema must be 1"
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
    consent = value.get("consent")
    if not isinstance(consent, dict) or consent.get("explicit") is not True:
        return False, "explicit consent evidence is required"
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("verified") is not True or cleanup.get("clean") is not True:
        return False, "verified clean cleanup is required"
    receipt_sha = value.get("receipt_sha256")
    if not isinstance(receipt_sha, str) or not HASH_RE.fullmatch(receipt_sha):
        return False, "receipt_sha256 must be a lowercase sha256"
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
        "schema": 1, "gate": "live_provider", "status": "pass",
        "artifact_sha256": detail, "evidence_file": "redacted-live-provider-receipt.json",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
