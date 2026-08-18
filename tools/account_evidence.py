#!/usr/bin/env python3
"""Create a redacted account-identity digest for a reviewed live pilot.

This helper is deliberately local and provider-specific.  It reads the selected
Claude profile's non-secret ``oauthAccount`` metadata, hashes the stable account
and organization identifiers, and prints only the digest plus bounded provenance.
It never reads credentials, contacts a provider, or emits an email/account ID.

The resulting ``account_evidence_sha256`` belongs in a manually reviewed live
pilot receipt only after the operator captures the digest immediately before and
after the pilot and confirms the selected profile did not change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


MAX_CONFIG_BYTES = 512 * 1024
SAFE_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULT_CONFIG_DIR = Path.home() / ".claude"


class AccountEvidenceError(ValueError):
    """Raised when local profile metadata cannot prove an account identity."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _config_path(config_dir: Path) -> Path:
    root = config_dir.absolute()
    if root.is_symlink() or not root.is_dir():
        raise AccountEvidenceError("config directory must be a real directory")
    path = root / ".claude.json"
    if path.is_symlink() or not path.is_file():
        raise AccountEvidenceError("profile metadata file is missing or symlinked")
    resolved = path.resolve(strict=True)
    if resolved.parent != root.resolve(strict=True):
        raise AccountEvidenceError("profile metadata escaped the config directory")
    if resolved.stat().st_size > MAX_CONFIG_BYTES:
        raise AccountEvidenceError("profile metadata is oversized")
    return resolved


def account_digest(*, config_dir: Path, profile: str = "default") -> dict[str, object]:
    if not isinstance(profile, str) or SAFE_PROFILE.fullmatch(profile) is None:
        raise AccountEvidenceError("profile must be a short safe identifier")
    path = _config_path(config_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountEvidenceError("profile metadata is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("oauthAccount"), dict):
        raise AccountEvidenceError("profile has no oauthAccount metadata")
    account = value["oauthAccount"]
    required = ("accountUuid", "organizationUuid", "emailAddress")
    if any(not isinstance(account.get(key), str) or not account[key] for key in required):
        raise AccountEvidenceError("oauthAccount metadata is incomplete")
    identity = {
        "provider": "claude",
        "profile": profile,
        "account_uuid": account["accountUuid"],
        "organization_uuid": account["organizationUuid"],
        "email_address": account["emailAddress"],
        "billing_type": account.get("billingType", ""),
    }
    digest = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return {
        "schema": 1,
        "provider": "claude",
        "profile": profile,
        "account_evidence_sha256": digest,
        "source": "local_oauth_account_metadata",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    args = parser.parse_args(argv)
    try:
        result = account_digest(config_dir=args.config_dir, profile=args.profile)
    except AccountEvidenceError as exc:
        print(json.dumps({"schema": 1, "status": "blocked", "error": str(exc)}, sort_keys=True))
        return 3
    print(json.dumps({**result, "status": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
