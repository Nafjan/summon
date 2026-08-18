from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "account_evidence", ROOT / "tools" / "account_evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class AccountEvidenceTests(unittest.TestCase):
    def profile_dir(self, value: dict) -> Path:
        root = Path(tempfile.mkdtemp(prefix="summon-account-evidence-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / ".claude.json").write_text(json.dumps(value), encoding="utf-8")
        return root

    def test_digest_is_redacted_and_stable(self):
        value = {"oauthAccount": {
            "accountUuid": "account-123",
            "organizationUuid": "org-456",
            "emailAddress": "owner@example.test",
            "billingType": "subscription",
        }}
        root = self.profile_dir(value)
        result = MODULE.account_digest(config_dir=root, profile="default")
        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["profile"], "default")
        self.assertRegex(result["account_evidence_sha256"], r"^[0-9a-f]{64}$")
        rendered = json.dumps(result)
        self.assertNotIn("account-123", rendered)
        self.assertNotIn("owner@example.test", rendered)
        self.assertEqual(result, MODULE.account_digest(config_dir=root, profile="default"))

    def test_missing_or_malformed_metadata_fails_closed(self):
        root = self.profile_dir({})
        with self.assertRaises(MODULE.AccountEvidenceError):
            MODULE.account_digest(config_dir=root)
        root = self.profile_dir({"oauthAccount": {"accountUuid": "x"}})
        with self.assertRaises(MODULE.AccountEvidenceError):
            MODULE.account_digest(config_dir=root)

    def test_symlink_config_is_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="summon-account-evidence-link-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        target = root / "target"
        target.mkdir()
        (target / ".claude.json").write_text("{}", encoding="utf-8")
        alias = root / "alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaises(MODULE.AccountEvidenceError):
            MODULE.account_digest(config_dir=alias)


if __name__ == "__main__":
    unittest.main()
