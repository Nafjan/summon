"""Source-only metadata acceptance; never execute the referenced runtime fixtures."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workspace_format_inventory", ROOT / "tools/format_inventory.py")
inventory = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(inventory)


class WorkspaceFormatMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value = inventory.validate_inventory(ROOT)
        cls.entries = {item["id"]: item for item in cls.value["formats"]}
        cls.report = inventory.gap_report(ROOT)

    def test_workspace_source_literal_pairs_are_mapped_without_global_completeness_claim(self):
        remaining = [row for row in self.report["unmapped_literals"]
                     if "summon.workspace" in row["literal"]
                     and (Path(row["source"]).name.startswith("_workspace_")
                          or Path(row["source"]).name == "_swarm_coordinator.py")]
        self.assertEqual(remaining, [])
        # Another independent lane may close the non-workspace literals. Do not
        # pin those unrelated omissions or mistake zero literals for acceptance.
        self.assertTrue(self.report["evidence_gaps"])
        self.assertTrue(any("not runtime acceptance" in line for line in self.report["limits"]))
        self.assertFalse(any(name.startswith(("_workspace_", "_swarm_coordinator", "_auth", "_profiles"))
                             for name in sys.modules))

    def test_private_source_and_public_outcome_have_separate_authority(self):
        source = self.entries["workspace-linked-replacement-authority"]
        result = self.entries["workspace-linked-replacement-result"]
        self.assertEqual(source["authority"], "private_authority")
        self.assertEqual(source["producers"], [])
        self.assertIn(source["id"], self.report["missing_producers"])
        self.assertTrue(any("No production issuer" in gap for gap in source["evidence_gaps"]))
        self.assertEqual(result["authority"], "public_projection")
        self.assertEqual({ref["symbol"] for ref in result["readers"]},
                         {"_validate_result", "performPendingLinked"})
        self.assertTrue(any("execution_authorized=false" in gap for gap in result["evidence_gaps"]))

    def test_disposition_request_shapes_and_role_bound_proofs_remain_distinct(self):
        callback = self.entries["workspace-disposition-authorization-request"]
        preimage = self.entries["workspace-disposition-request-preimage"]
        self.assertEqual(callback["identity"]["version"], preimage["identity"]["version"])
        self.assertIn("action=retain_held_context", callback["identity"]["shape"])
        self.assertNotIn("action=retain_held_context", preimage["identity"]["shape"])
        self.assertNotIn("request_ref", callback["identity"]["shape"])
        self.assertIn("request_ref", preimage["identity"]["shape"])
        self.assertEqual(preimage["readers"][0]["symbol"], "_canonical_event_bytes")
        self.assertTrue(any("no schema-aware" in gap for gap in preimage["evidence_gaps"]))
        request = self.entries["workspace-operator-disposition-proof-request"]
        decision = self.entries["workspace-operator-disposition-proof-decision"]
        self.assertEqual(request["identity"]["version"], decision["identity"]["version"])
        self.assertNotEqual(request["identity"]["shape"], decision["identity"]["shape"])

    def test_detail_listing_record_and_body_scope_are_not_collapsed(self):
        listing, record = (self.entries["workspace-detail-" + suffix] for suffix in ("list", "record"))
        self.assertEqual(listing["identity"]["version"], record["identity"]["version"])
        self.assertIn("status=listed", listing["identity"]["shape"])
        self.assertIn("status=detail", record["identity"]["shape"])
        self.assertNotEqual(listing["readers"], record["readers"])
        self.assertEqual(self.entries["workspace-detail-scope"]["authority"], "private_authority")
        body = self.entries["workspace-detail-body"]
        self.assertEqual(body["authority"], "private_evidence")
        self.assertTrue(any("separately installed body scope" in gap for gap in body["evidence_gaps"]))
        self.assertTrue(any("without a standalone schema validator" in gap for gap in body["evidence_gaps"]))

    def test_recovery_storage_keys_do_not_invent_payload_versions_or_retry_authority(self):
        for suffix in ("disposition", "linked-replacement"):
            with self.subTest(suffix=suffix):
                slot = self.entries["workspace-pending-" + suffix + "-slot"]
                payload = self.entries["workspace-pending-" + suffix + "-payload"]
                self.assertEqual(slot["identity"]["discriminator"], "sessionStorage key")
                self.assertEqual(payload["identity"]["kind"], "unversioned")
                self.assertIsNone(payload["identity"]["version"])
                self.assertIn(payload["id"], self.report["manual_formats"])
                self.assertTrue(any("not permission to retry" in gap for gap in payload["evidence_gaps"]))

    def test_capacity_exemplars_and_advertised_schema_names_are_not_runtime_reader_proof(self):
        for suffix in ("policy", "snapshot", "request", "decision"):
            with self.subTest(suffix=suffix):
                item = self.entries["workspace-capacity-admission-" + suffix]
                self.assertEqual(item["producers"], [inventory._ref("_swarm_coordinator", "_bound_record")])
                self.assertEqual(item["readers"], [inventory._ref("_swarm_coordinator", "_canonical_json")])
                self.assertTrue(any("not an accepted runtime record" in gap for gap in item["evidence_gaps"]))
        self.assertIn("result={}", self.entries["workspace-capacity-admission-decision"]["identity"]["shape"])
        normal = self.entries["workspace-admission-policy"]
        self.assertIn(inventory._ref("_workspace_runtime", "WorkspaceRuntime._contract"), normal["declarations"])
        self.assertNotIn(inventory._ref("_workspace_runtime", "WorkspaceRuntime._contract"), normal["readers"])

    def test_current_cross_result_reader_does_not_claim_historical_acceptance(self):
        receipt = self.entries["workspace-command-refresh"]
        status = self.entries["workspace-command-refresh-status"]
        self.assertNotEqual(receipt["identity"]["version"], status["identity"]["version"])
        self.assertEqual(receipt["readers"], status["readers"])
        self.assertTrue(any("not historical-version" in gap for gap in status["evidence_gaps"]))
        detached = self.entries["swarm-projection-rebuild"]
        self.assertEqual(detached["reader_contract"], "producer_only_detached")
        self.assertEqual(detached["readers"], [])
        self.assertIn(detached["id"], self.report["producer_only_detached"])


class WorkspaceInventoryValidatorControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "skills/summon/scripts"
        scripts.mkdir(parents=True)
        self.source = scripts / "sample.py"
        self.source.write_text('SCHEMA = "summon.workspace.sample/v1"\n'
                               "def write(): pass\nclass Reader:\n    def read(self): pass\n")
        (scripts / "test_sample.py").write_text("class Evidence:\n    def test_current(self): pass\n")
        self.value = {"schema": inventory.SCHEMA, "version": inventory.VERSION, "formats": [
            inventory._format("sample", "summon.workspace.sample/v1",
                              [inventory._ref("sample", "write")],
                              [inventory._ref("sample", "Reader.read")],
                              authority="private_evidence",
                              fixtures=[inventory._ref("test_sample", "Evidence.test_current")])]}

    def test_gap_discovery_does_not_suppress_new_literals_in_a_mapped_source(self):
        self.assertEqual(inventory.gap_report(self.root, self.value)["unmapped_literals"], [])
        self.source.write_text(self.source.read_text() + 'OTHER = "summon.workspace.unmapped/v8"\n')
        self.assertEqual(inventory.gap_report(self.root, self.value)["unmapped_literals"], [{
            "source": "skills/summon/scripts/sample.py",
            "literal": "summon.workspace.unmapped/v8", "status": "unmapped"}])

    def test_qualified_reader_and_fixture_definitions_are_required(self):
        for role, symbol in (("readers", "read"), ("fixtures", "test_current"),
                             ("readers", "Reader.missing")):
            with self.subTest(role=role, symbol=symbol):
                value = copy.deepcopy(self.value)
                value["formats"][0][role][0]["symbol"] = symbol
                with self.assertRaises(inventory.FormatInventoryError):
                    inventory.validate_inventory(self.root, value)

    def test_reader_absence_requires_explicit_detached_contract_and_gap(self):
        value = copy.deepcopy(self.value)
        item = value["formats"][0]
        item["readers"] = []
        with self.assertRaises(inventory.FormatInventoryError):
            inventory.validate_inventory(self.root, value)
        item["reader_contract"] = "producer_only_detached"
        with self.assertRaises(inventory.FormatInventoryError):
            inventory.validate_inventory(self.root, value)
        item["evidence_gaps"] = ["No production reader established; detached metadata only."]
        self.assertEqual(inventory.gap_report(self.root, value)["producer_only_detached"], ["sample"])
        value["schema"], value["version"] = inventory.LEGACY_SCHEMA, 1
        item.pop("reader_contract")
        with self.assertRaises(inventory.FormatInventoryError):
            inventory.validate_inventory(self.root, value)


if __name__ == "__main__":
    unittest.main()
