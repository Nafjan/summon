"""Provider-free acceptance for the reviewed format-inventory metadata additions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_l02", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture
def small(tmp_path):
    scripts = tmp_path / "skills/summon/scripts"
    scripts.mkdir(parents=True)
    source = scripts / "example.py"
    source.write_text("SCHEMA = \"summon.example/v1\"\nVERSION = 1\n"
                      "def write(): pass\ndef read(): pass\n", encoding="utf-8")
    fixture = scripts / "test_example.py"
    fixture.write_text("class ReaderTests:\n    def test_round_trip(self): pass\n", encoding="utf-8")
    ref = lambda symbol: {"path": source.relative_to(tmp_path).as_posix(),
                          "symbol": symbol, "language": "python"}
    value = {"schema": inventory.SCHEMA, "version": inventory.VERSION, "formats": [
        inventory._format("example", "summon.example/v1", [ref("write")], [ref("read")],
                          authority="private_evidence", fixtures=[{
                              "path": fixture.relative_to(tmp_path).as_posix(),
                              "symbol": "ReaderTests.test_round_trip", "language": "python"}])]}
    return tmp_path, source, fixture, value


@pytest.mark.parametrize("expression", ["{1, VERSION}", "frozenset({1, VERSION})"], ids=['p001_case_001', 'p001_case_002'])
def test_numeric_set_binding_accepts_explicit_literal_member(small, expression):
    root, source, _, value = small
    source.write_text(source.read_text() + "\nSUPPORTED = " + expression + "\n")
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": 1,
        "binding": {"kind": "set_member", "symbol": "SUPPORTED"}, "shape": ["room"]}
    assert inventory.validate_inventory(root, value)


@pytest.mark.parametrize("expression", [
    "{2}", "{True}", "{1.0}", "{1, True}", "{1, 2.0}",
    "{1 + 0}", "{1, compute()}", "frozenset(load())", "frozenset({1}, bad=True)",
    "set({1})", "[1]", "{VERSION}", "{1, MISSING}", "{1, DYNAMIC}",
], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007', 'p002_case_008', 'p002_case_009', 'p002_case_010', 'p002_case_011', 'p002_case_012', 'p002_case_013', 'p002_case_014'])
def test_numeric_set_binding_rejects_dynamic_or_noninteger_members(small, expression):
    root, source, _, value = small
    source.write_text(source.read_text() + "\nDYNAMIC = compute()\nSUPPORTED = " + expression + "\n")
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": 1,
        "binding": {"kind": "set_member", "symbol": "SUPPORTED"}, "shape": ["room"]}
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("binding", [
    {}, {"kind": "other", "symbol": "SUPPORTED"}, {"kind": "set_member"},
    {"kind": "set_member", "symbol": "SUPPORTED", "extra": 1},
    {"kind": "set_member", "symbol": "a.b"}, {"kind": "set_member", "symbol": True},
    {"kind": "set_member", "symbol": "UNRELATED"},
], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007'])
def test_numeric_set_binding_rejects_malformed_binding(small, binding):
    root, source, _, value = small
    source.write_text(source.read_text() + "\nSUPPORTED = {1}\nUNRELATED = 1\n")
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": 1,
        "binding": binding, "shape": ["room"]}
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("version", [True, 1.0, 0, -1], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004'])
def test_numeric_set_binding_preserves_exact_version_validation(small, version):
    root, source, _, value = small
    source.write_text(source.read_text() + "\nSUPPORTED = {1}\n")
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": version,
        "binding": {"kind": "set_member", "symbol": "SUPPORTED"}, "shape": ["room"]}
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("literal", ["True", "1.0", "2", "1 + 0"], ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004'])
def test_scalar_numeric_binding_still_requires_exact_integer_constant(small, literal):
    root, source, _, value = small
    source.write_text(source.read_text().replace("VERSION = 1", "VERSION = " + literal))
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": 1,
        "binding": "VERSION", "shape": ["room"]}
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)


def test_historical_process_reader_binding_has_no_invented_producer():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    item = entries["conversation-process-v1-reader"]
    assert item["producers"] == []
    assert item["identity"]["version"] == 1
    assert item["identity"]["binding"] == {
        "kind": "set_member", "symbol": "SUPPORTED_PROCESS_RECORD_SCHEMA_VERSIONS"}
    assert len(item["fixtures"]) == 3
    assert len(item["evidence_gaps"]) == 2
    assert entries["conversation-process-legacy"]["identity"]["binding"] == "PROCESS_RECORD_SCHEMA_VERSION"


@pytest.mark.parametrize("assignment", ["SUPPORTED = {1}\nSUPPORTED = {2}", "def unrelated():\n    SUPPORTED = {1}"], ids=['p006_case_001', 'p006_case_002'])
def test_numeric_set_binding_rejects_ambiguous_or_local_assignment(small, assignment):
    root, source, _, value = small
    source.write_text(source.read_text() + "\n" + assignment + "\n")
    value["formats"][0]["identity"] = {
        "kind": "numeric", "discriminator": "schema_version", "version": 1,
        "binding": {"kind": "set_member", "symbol": "SUPPORTED"}, "shape": ["room"]}
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)


def test_l02_durable_wire_and_usage_families_keep_explicit_gaps():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    expected = {
        "background-launch-v2", "submission-accounting-handoff", "workspace-protocol-v1",
        "workspace-protocol-v2", "workspace-economics-reservation", "workspace-effects-observation",
        "workspace-worker-pipe", "workspace-supervisor-pipe", "workspace-compiled-context",
        "workspace-message-send-request", "workspace-message-send-result",
        "workspace-transport-admission-envelope", "transport-budget-result",
        "usage-live-private", "usage-live-runner-plan", "usage-live-public-status",
    }
    assert expected <= entries.keys()
    for identifier in expected:
        assert entries[identifier]["fixtures"]
        assert entries[identifier]["evidence_gaps"]
    assert entries["workspace-protocol-v1"]["identity"]["discriminator"] == "protocol"
    assert entries["workspace-protocol-v2"]["identity"]["discriminator"] == "protocol"
    assert entries["usage-live-public-status"]["authority"] == "public_projection"
    assert entries["usage-live-public-status"]["readers"]


def test_numeric_and_public_telemetry_metadata_preserve_separate_boundaries():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    expected = {
        "telemetry-config": (1, "CONFIG_SCHEMA_VERSION", "private_authority"),
        "telemetry-event-legacy": (1, "LEGACY_EVENT_SCHEMA_VERSION", "private_evidence"),
        "telemetry-event-spool": (2, "EVENT_SCHEMA_VERSION", "private_evidence"),
        "telemetry-event-public": (2, "EVENT_SCHEMA_VERSION", "public_projection"),
    }
    for name, (version, binding, authority) in expected.items():
        item = entries[name]
        assert item["identity"]["kind"] == "numeric"
        assert item["identity"]["version"] == version
        assert item["identity"]["binding"] == binding
        assert item["authority"] == authority
        assert item["fixtures"] and item["evidence_gaps"]
    assert entries["telemetry-event-legacy"]["producers"] == []
    assert entries["telemetry-event-spool"]["identity"]["shape"] != entries["telemetry-event-public"]["identity"]["shape"]
