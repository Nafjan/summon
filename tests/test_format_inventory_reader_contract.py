"""Source-only reader-contract acceptance; referenced runtime fixtures never run."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_reader_contract", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "skills/summon/scripts/example.py"
    source.parent.mkdir(parents=True)
    source.write_text('SCHEMA = "summon.example/v1"\ndef produce(): pass\ndef read(): pass\n', encoding="utf-8")
    ref = lambda name: {"path": source.relative_to(tmp_path).as_posix(), "symbol": name, "language": "python"}
    item = inventory._format("example", "summon.example/v1", [ref("produce")], [ref("read")],
                             authority="private_evidence", gaps=["Synthetic source references only."])
    return tmp_path, source, {"schema": inventory.SCHEMA, "version": inventory.VERSION, "formats": [item]}


def detached(value):
    result = copy.deepcopy(value)
    result["formats"][0].update(reader_contract="producer_only_detached", readers=[])
    return result


def legacy(value):
    result = copy.deepcopy(value)
    result.update(schema="summon.format-inventory/v1", version=1)
    for item in result["formats"]:
        item.pop("reader_contract")
    return result


def test_v1_validation_and_serialization_preserve_original_shape(sample):
    root, _, value = sample
    old = legacy(value)
    assert inventory.validate_inventory(root, old) == old
    assert json.loads(inventory.canonical_inventory(root, old)) == old
    assert inventory.gap_report(root, old)["producer_only_detached"] == []
    assert old == legacy(value)


@pytest.mark.parametrize("mutation", ["empty_readers", "required_field", "detached_field"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_v1_cannot_opt_into_v2_reader_contract(sample, mutation):
    root, _, value = sample
    old = legacy(value)
    item = old["formats"][0]
    if mutation == "empty_readers":
        item["readers"] = []
    else:
        item["reader_contract"] = "required" if mutation == "required_field" else "producer_only_detached"
        if mutation == "detached_field": item["readers"] = []
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, old)


@pytest.mark.parametrize("schema,version", [
    ("summon.format-inventory/v1", 2), ("summon.format-inventory/v2", 1),
    ("summon.format-inventory/v1", True), ("summon.format-inventory/v2", True),
    ("summon.format-inventory/v2", False), ("summon.format-inventory/v2", 2.0),
    ("summon.format-inventory/v2", "2"), ("summon.format-inventory/v2", 3),
    ("summon.format-inventory/v3", 3), ("summon.format-inventory/v2", None),
    (None, 2), ([], 2),
], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007', 'p002_case_008', 'p002_case_009', 'p002_case_010', 'p002_case_011', 'p002_case_012'])
def test_inventory_version_pairs_remain_exact(sample, schema, version):
    root, _, value = sample
    value.update(schema=schema, version=version)
    with pytest.raises(inventory.FormatInventoryError, match="unsupported inventory version"):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("contract", [None, True, False, 1, [], {}, "", "Required", "producer_only", "producer_only_detached "], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007', 'p003_case_008', 'p003_case_009', 'p003_case_010'])
def test_v2_unknown_or_malformed_reader_contract_refuses(sample, contract):
    root, _, value = sample
    value["formats"][0]["reader_contract"] = contract
    with pytest.raises(inventory.FormatInventoryError, match="reader contract"):
        inventory.validate_inventory(root, value)


def test_v2_requires_explicit_contract_and_normal_readers(sample):
    root, _, value = sample
    missing = copy.deepcopy(value)
    del missing["formats"][0]["reader_contract"]
    with pytest.raises(inventory.FormatInventoryError, match="exact"):
        inventory.validate_inventory(root, missing)
    value["formats"][0]["readers"] = []
    with pytest.raises(inventory.FormatInventoryError, match="readers are required"):
        inventory.validate_inventory(root, value)


def test_detached_entry_is_reported_without_claiming_reader_coverage(sample):
    root, _, value = sample
    value = detached(value)
    assert inventory.validate_inventory(root, value) == value
    report = inventory.gap_report(root, value)
    assert report["producer_only_detached"] == ["example"]
    assert report["unmapped_literals"] == []
    assert report["evidence_gaps"] == [{"id": "example", "gaps": value["formats"][0]["evidence_gaps"]}]
    assert json.loads(inventory.canonical_inventory(root, value)) == value


@pytest.mark.parametrize("mutation", ["no_producer", "has_reader", "wrong_authority", "no_gap", "empty_gap",
                                    "stale_producer", "unsafe_producer", "unknown_literal", "fixture_as_reader",
                                    "unknown_field", "duplicate_producer"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005', 'p004_case_006', 'p004_case_007', 'p004_case_008', 'p004_case_009', 'p004_case_010', 'p004_case_011'])
def test_detached_exception_preserves_negative_boundaries(sample, mutation):
    root, source, value = sample
    value = detached(value)
    item = value["formats"][0]
    if mutation == "no_producer": item["producers"] = []
    elif mutation == "has_reader": item["readers"] = copy.deepcopy(item["producers"])
    elif mutation == "wrong_authority": item["authority"] = "private_authority"
    elif mutation == "no_gap": item["evidence_gaps"] = []
    elif mutation == "empty_gap": item["evidence_gaps"] = [" "]
    elif mutation == "stale_producer": item["producers"][0]["symbol"] = "missing"
    elif mutation == "unsafe_producer": item["producers"][0]["path"] = "../outside.py"
    elif mutation == "unknown_literal": item["identity"]["version"] = "summon.example/v99"
    elif mutation == "fixture_as_reader":
        fixture = source.with_name("test_example.py")
        fixture.write_text('def test_result(): pass\n', encoding="utf-8")
        item["readers"] = [{"path": fixture.relative_to(root).as_posix(), "symbol": "test_result", "language": "python"}]
    elif mutation == "unknown_field": item["reader_contract_reason"] = "extension"
    else: item["producers"].append(copy.deepcopy(item["producers"][0]))
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, value)


def test_rebuild_is_private_detached_and_runtime_schema_stays_v1():
    value = inventory.validate_inventory(ROOT)
    item = next(row for row in value["formats"] if row["id"] == "swarm-projection-rebuild")
    assert value["schema"] == "summon.format-inventory/v2" and value["version"] == 2
    assert item["identity"]["version"] == "summon.swarm.projection-rebuild/v1"
    assert item["reader_contract"] == "producer_only_detached"
    assert item["authority"] == "private_evidence" and item["readers"] == []
    assert item["producers"] == [inventory._ref("_swarm_coordinator", "rebuild_projection_from_journal")]
    assert item["fixtures"] == [inventory._test("swarm_projection_rebuild", "ProjectionRebuildTests.test_f01_swarm_only_deterministic_detached_projection")]
    report = inventory.gap_report(ROOT)
    assert report["producer_only_detached"] == [item["id"]]
    assert not any(row["literal"] == item["identity"]["version"] for row in report["unmapped_literals"])
    assert any(row["id"] == item["id"] and row["gaps"] for row in report["evidence_gaps"])


def test_frozen_v1_envelope_guard_rejects_v2_before_format_traversal(sample):
    # Exact old header predicate, frozen as a synthetic compatibility fixture.
    # This demonstrates the old envelope refusal, not execution of an old runtime.
    def old_envelope_guard(source):
        schema = "summon.format-inventory/v1"
        if source["schema"] != schema or type(source["version"]) is not int or source["version"] != 1:
            raise ValueError("unsupported inventory version")
        return source
    _, _, value = sample
    assert old_envelope_guard(legacy(value)) == legacy(value)
    with pytest.raises(ValueError, match="unsupported inventory version"):
        old_envelope_guard(value)
