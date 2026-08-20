from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _model_catalog as catalog


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_schema_and_requested_order(self):
        value = catalog.load_catalog()
        self.assertEqual(value["schema"], 1)
        entries = value["entries"]
        self.assertEqual([item["name"] for item in entries[:4]], ["Fable", "Sol", "Opus", "Kimi"])
        self.assertEqual([item["label"] for item in entries[:4]], ["frontier"] * 4)
        near = [item for item in entries if item["label"] == "near-frontier"]
        self.assertEqual([item["name"] for item in near],
                         ["Grok", "Gemini Flash", "GLM", "DeepSeek V4 Flash",
                          "Luna", "Terra", "Spark"])

    def test_keys_and_routes_are_unique_and_dispatchability_is_explicit(self):
        value = catalog.load_catalog()
        keys = [item["key"] for item in value["entries"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(next(item for item in value["entries"]
                              if item["name"] == "DeepSeek V4 Flash")["target"],
                         "deepseek-v4-flash-ga-260731")
        self.assertTrue(next(item for item in value["entries"]
                             if item["name"] == "DeepSeek V4 Pro")["dispatchable"])
        luna = next(item for item in value["entries"] if item["name"] == "Luna")
        self.assertEqual(luna["availability"], "config_observed")
        self.assertTrue(luna["dispatchable"])
        self.assertFalse(next(item for item in value["entries"]
                              if item["name"] == "Spark")["dispatchable"])

    def test_display_is_separate_from_served_evidence(self):
        display = catalog.display_for("agy", "gemini-3.7-flash-high")
        self.assertEqual(display["role"], "evidence research")
        self.assertEqual(display["name"], "Gemini Flash")
        self.assertEqual(display["version"], "3.7")
        self.assertEqual(display["availability"], "catalog_listed")
        self.assertFalse(display["served_exact"])
        self.assertNotIn("served", display)

    def test_display_can_be_derived_from_redacted_model_hash(self):
        model_hash = __import__("hashlib").sha256(
            b"gemini-3.7-flash-high").hexdigest()
        display = catalog.display_for_hash("agy", model_hash)
        self.assertEqual(display["name"], "Gemini Flash")
        self.assertEqual(display["version"], "3.7")
        self.assertIsNone(catalog.display_for_hash("agy", "f" * 64))

    def test_unknown_and_unverified_routes_do_not_become_dispatch_targets(self):
        self.assertIsNone(catalog.display_for("cursor-agent", "not-in-catalog"))
        entry = next(item for item in catalog.load_catalog()["entries"]
                     if item["name"] == "DeepSeek V4 Pro")
        self.assertEqual(entry["backend"], "arkcli")
        self.assertEqual(entry["target"], "deepseek-v4-pro-ga-260813")
        self.assertIsNone(catalog.display_for("unverified", "deepseek-v4-pro"))

    def test_catalog_digest_is_stable_and_json_is_valid(self):
        raw = json.loads(catalog._CATALOG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], 1)
        self.assertEqual(len(catalog.catalog_sha256()), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
