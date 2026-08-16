from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "live_provider_gate", ROOT / "tools" / "live_provider_gate.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def _case(status: str, *, uncertain: bool, attempts: int, calls: int) -> dict:
    return {
        "status": status,
        "attempts": attempts,
        "provider_calls": calls,
        "uncertain_spend": uncertain,
        "cleanup_verified": True,
        "cleanup_clean": True,
        "no_orphans": True,
        "no_post_deadline_contact": True,
        "no_retry_fallback": True,
        "no_fallback": True,
    }


def _receipt() -> dict:
    return {
        "schema": 2,
        "provider": "claude",
        "backend": "claude",
        "model_requested": "claude-opus-4-7",
        "model_served": "claude-opus-4-7",
        "profile": "default",
        "transport": "subprocess",
        "owner_generation": 1,
        "attempts": 2,
        "provider_calls": 2,
        "consent": {"explicit": True},
        "cleanup": {"verified": True, "clean": True,
                    "retained_resources": []},
        "uncertain_spend": False,
        "receipt_sha256": "a" * 64,
        "no_retry_fallback": True,
        "owner_fence": True,
        "deadline_fence": True,
        "cancel_fence": True,
        "kill_switch": True,
        "no_orphans": True,
        "safety_matrix": {
            "normal": _case("decided", uncertain=False, attempts=2, calls=2),
            "cancel": _case("cancelled", uncertain=True, attempts=1, calls=1),
            "deadline": _case("failed", uncertain=True, attempts=1, calls=1),
        },
    }


class LiveProviderGateTests(unittest.TestCase):
    def write(self, value: dict) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        with handle:
            json.dump(value, handle, sort_keys=True)
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_schema_two_requires_three_safety_cases(self):
        path = self.write(_receipt())
        ok, detail = MODULE.validate(path)
        self.assertTrue(ok, detail)
        missing = _receipt()
        del missing["safety_matrix"]["deadline"]
        path = self.write(missing)
        ok, detail = MODULE.validate(path)
        self.assertFalse(ok)
        self.assertIn("safety_matrix", detail)

    def test_safety_counts_and_conservative_deadline_are_bound(self):
        value = _receipt()
        value["safety_matrix"]["normal"]["provider_calls"] = 1
        path = self.write(value)
        ok, detail = MODULE.validate(path)
        self.assertFalse(ok)
        self.assertIn("normal safety counts", detail)

        value = _receipt()
        value["safety_matrix"]["deadline"]["uncertain_spend"] = False
        path = self.write(value)
        ok, detail = MODULE.validate(path)
        self.assertFalse(ok)
        self.assertIn("deadline safety", detail)

    def test_unknown_and_raw_fields_fail_closed(self):
        value = _receipt()
        value["unreviewed_claim"] = True
        path = self.write(value)
        ok, detail = MODULE.validate(path)
        self.assertFalse(ok)
        self.assertIn("unknown evidence fields", detail)

        value = _receipt()
        value["safety_matrix"]["normal"]["prompt"] = "private"
        path = self.write(value)
        ok, detail = MODULE.validate(path)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
