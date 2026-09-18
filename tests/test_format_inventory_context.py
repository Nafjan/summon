"""Source-only metadata coverage for seven bounded context compiler families."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_context", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture(scope="module")
def entries():
    return {row["id"]: row for row in inventory.validate_inventory(ROOT)["formats"]}


def test_context_batch_covers_exactly_seven_literals_and_two_output_shapes(entries):
    expected = {"summon.context-" + suffix + "/v1" for suffix in
                ("input", "lineage", "output", "receipt-binding", "receipt-proof", "reference-proof", "rollback")}
    rows = [row for row in entries.values() if row["identity"]["version"] in expected]
    assert len(rows) == 8
    assert {row["identity"]["version"] for row in rows} == expected
    assert all(row["fixtures"] and row["evidence_gaps"] for row in rows)
    assert all(row["reader_contract"] == "required" for row in rows)
    assert all(row["authority"] != "public_projection" for row in rows)
    outer, prompt = entries["context-output-result"], entries["context-output-prompt"]
    assert outer["identity"]["version"] == prompt["identity"]["version"]
    assert outer["identity"]["shape"] != prompt["identity"]["shape"]
    assert "rollback" in outer["identity"]["shape"] and "blocks" in prompt["identity"]["shape"]
    assert outer["authority"] == prompt["authority"] == "private_evidence"
    assert all(row["readers"] == [inventory._ref("run_subagent", "_prepare_dispatch_context")]
               for row in (outer, prompt))
    report = inventory.gap_report(ROOT)
    assert not any(row["literal"] in expected for row in report["unmapped_literals"])
    assert report["producer_only_detached"] == ["swarm-projection-rebuild"]


def test_context_input_is_not_an_invented_authoring_or_dispatch_authority(entries):
    item = entries["context-input"]
    assert item["authority"] == "definition_input" and item["producers"] == []
    assert item["readers"] == [inventory._ref("_context_compile", "_parse_context")]
    assert "context-input" in inventory.gap_report(ROOT)["missing_producers"]
    assert inventory._test("phase1_compatibility", "ContextProfileOffCompatibilityGoldens.test_dispatch_context_rejects_file_claimed_authority") in item["fixtures"]


@pytest.mark.parametrize("identifier,writers", [
    ("context-lineage", ["_compile_context", "_off_result"]),
    ("context-receipt-binding", ["_receipt_binding"]),
], ids=['p001_case_001', 'p001_case_002'])
def test_hash_preimages_name_serialization_consumption_without_reader_guarantees(entries, identifier, writers):
    item = entries[identifier]
    assert item["producers"] == [inventory._ref("_context_compile", name) for name in writers]
    assert item["readers"] == [inventory._ref("_context_compile", "_canonical")]
    assert item["authority"] == "private_evidence"
    gaps = " ".join(item["evidence_gaps"])
    assert "preimage" in gaps and "generic JSON" in gaps and "schema-aware" in gaps
    assert "authority validation" in gaps


def test_context_proofs_keep_adapter_minting_and_local_linkage_distinct(entries):
    reference, receipt = entries["context-reference-proof"], entries["context-receipt-proof"]
    assert reference["producers"] == [inventory._ref("_context_target", "make_verified_reference_proof")]
    assert reference["readers"] == [inventory._ref("_context_compile", "_proofs")]
    assert "adapter-minted _VerifiedReferenceProof wrapper" in reference["identity"]["shape"]
    assert inventory._test("phase1_context_compile", "test_stable_external_reference_requires_target_hash_proof_before_output") in reference["fixtures"]
    assert receipt["producers"] == [inventory._ref("_context_compile", "make_receipt_proof"),
                                    inventory._ref("_context_compile", "_receipt_proof")]
    assert "not authenticated provider receipt provenance" in " ".join(receipt["evidence_gaps"])


def test_rollback_retains_private_bytes_and_only_claims_a_hash_consumer(entries):
    item = entries["context-rollback"]
    assert item["authority"] == "recovery_state"
    assert item["identity"]["shape"] == ["source_sha256", "source_utf8_b64"]
    assert item["producers"] == [inventory._ref("_context_compile", "_rollback")]
    assert item["readers"] == [inventory._ref("run_subagent", "_prepare_dispatch_context")]
    gaps = " ".join(item["evidence_gaps"])
    assert "consumes only source_sha256" in gaps
    assert "no production decoder, restore operation" in gaps
    assert "base64 is not redaction" in gaps
