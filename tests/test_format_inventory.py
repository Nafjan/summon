"""Metadata acceptance only; does not execute the referenced runtime fixtures."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture
def small(tmp_path):
    scripts = tmp_path / "skills/summon/scripts"
    scripts.mkdir(parents=True)
    source = scripts / "example.py"
    source.write_text('SCHEMA = "summon.example/v1"\nVERSION = 1\n'
                      'def write(): pass\ndef read(): pass\n', encoding="utf-8")
    fixture = scripts / "test_example.py"
    fixture.write_text('class ReaderTests:\n    def test_round_trip(self): pass\n', encoding="utf-8")
    ref = lambda symbol: {"path": source.relative_to(tmp_path).as_posix(),
                          "symbol": symbol, "language": "python"}
    value = {"schema": inventory.SCHEMA, "version": inventory.VERSION, "formats": [
        inventory._format("example", "summon.example/v1", [ref("write")], [ref("read")],
                          authority="private_evidence", fixtures=[{
                              "path": fixture.relative_to(tmp_path).as_posix(),
                              "symbol": "ReaderTests.test_round_trip", "language": "python"}])]}
    return tmp_path, source, fixture, value


def test_current_manifest_references_validate_without_claiming_runtime_acceptance():
    value = inventory.validate_inventory(ROOT)
    entries = {item["id"]: item for item in value["formats"]}
    assert len(entries) >= 40
    fresh = entries["launch-observation-fresh"]
    stored = entries["launch-observation-persisted"]
    assert fresh["identity"]["version"] == stored["identity"]["version"]
    assert fresh["identity"]["shape"] != stored["identity"]["shape"]
    assert entries["job-resume-claim-legacy"]["evidence_gaps"]
    legacy_fixtures = entries["job-resume-claim-legacy"]["fixtures"]
    assert {item["symbol"] for item in legacy_fixtures} >= {
        "test_exact_claim_readers_preserve_bytes_and_public_projection",
        "test_historical_ledger_refuses_authenticated_malformed_shape",
        "test_historical_child_refuses_authenticated_malformed_shape",
    }
    report = inventory.gap_report(ROOT)
    # Literal coverage does not remove historical/runtime evidence gaps.
    assert report["evidence_gaps"]
    assert entries["usage-snapshot"]["identity"]["version"] == "summon.usage/v1"
    assert entries["usage-live-public-status"]["readers"] == [inventory._ref("_usage", "export_snapshot")]
    assert entries["fleet-dispatch-public"]["authority"] == "public_projection"
    assert "workspace-pending-message-payload" in report["manual_formats"]
    assert report["limits"]
    # Returned data is detached from the module's manifest.
    value["formats"].clear()
    assert inventory.FORMAT_INVENTORY["formats"]


def test_second_unknown_format_in_an_already_covered_source_remains_visible(small):
    root, source, _, value = small
    assert inventory.gap_report(root, value)["unmapped_literals"] == []
    source.write_text(source.read_text() + '\nOTHER = "summon.second/v7"\n', encoding="utf-8")
    assert inventory.gap_report(root, value)["unmapped_literals"] == [{
        "source": source.relative_to(root).as_posix(), "literal": "summon.second/v7", "status": "unmapped"}]


def test_same_literal_in_a_different_source_is_not_implicitly_covered(small):
    root, source, _, value = small
    other = source.with_name("other.py")
    other.write_text('SCHEMA = "summon.example/v1"\n', encoding="utf-8")
    assert inventory.gap_report(root, value)["unmapped_literals"] == [{
        "source": other.relative_to(root).as_posix(), "literal": "summon.example/v1", "status": "unmapped"}]


def test_integrated_council_formats_report_unmapped_new_literals(small):
    root, source, _, value = small
    candidate = source.with_name("_council_between_round.py")
    candidate.write_text('SCHEMA = "summon.candidate/v1"\ndef produce(): pass\n', encoding="utf-8")
    report = inventory.gap_report(root, value)
    assert report["unmapped_literals"] == [{"source": candidate.relative_to(root).as_posix(),
                                            "literal": "summon.candidate/v1", "status": "unmapped"}]
    example = next(item for item in value["formats"] if item["id"] == "example")
    example["producers"][0].update(path=candidate.relative_to(root).as_posix(), symbol="produce")
    example["readers"][0].update(path=candidate.relative_to(root).as_posix(), symbol="produce")
    with pytest.raises(inventory.FormatInventoryError, match="literal format version"):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("path", ["../example.py", "tools/../example.py", "/etc/example.py",
                                 "C:/outside.py", "C:outside.py", "//server/share/file.py",
                                 r"..\example.py", "tools//file.py", "./example.py",
                                 "skills/summon/scripts/example.py:stream", ""], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005', 'p001_case_006', 'p001_case_007', 'p001_case_008', 'p001_case_009', 'p001_case_010', 'p001_case_011'])
@pytest.mark.parametrize("role", ["producers", "readers", "fixtures", "declarations"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_unsafe_paths_are_refused_for_every_reference_role(small, path, role):
    root, _, _, value = small
    ref = copy.deepcopy(value["formats"][0]["producers"][0])
    ref["path"] = path
    value["formats"][0][role] = [ref]
    with pytest.raises(inventory.FormatInventoryError, match="path|repository|source"):
        inventory.validate_inventory(root, value)


def test_symlink_escape_is_refused_before_source_read(small, tmp_path):
    root, source, _, value = small
    # The sibling file is a synthetic fixture, never host data.
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text(source.read_text(), encoding="utf-8")
    link = source.with_name("escape.py")
    try:
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("file symlink creation unavailable")
        value["formats"][0]["producers"][0]["path"] = link.relative_to(root).as_posix()
        with pytest.raises(inventory.FormatInventoryError, match="repository|source"):
            inventory.validate_inventory(root, value)
        with pytest.raises(inventory.FormatInventoryError, match="repository|source"):
            inventory.discover_literals(root)
    finally:
        if link.is_symlink():
            link.unlink()
        outside.unlink()


@pytest.mark.parametrize("mutation", ["inventory_version", "bool_version", "literal", "symbol", "fixture", "comment_only", "unqualified_method", "missing_file"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007', 'p003_case_008'])
def test_stale_versions_symbols_and_fixtures_refuse(small, mutation):
    root, source, fixture, value = small
    item = value["formats"][0]
    if mutation == "inventory_version": value["version"] = 3
    elif mutation == "bool_version": value["version"] = True
    elif mutation == "literal": item["identity"]["version"] = "summon.example/v999"
    elif mutation == "symbol": item["readers"][0]["symbol"] = "missing"
    elif mutation == "fixture": item["fixtures"][0]["symbol"] = "ReaderTests.test_missing"
    elif mutation == "comment_only": fixture.write_text('# class ReaderTests: def test_round_trip(self): pass\n', encoding="utf-8")
    elif mutation == "unqualified_method": item["fixtures"][0]["symbol"] = "test_round_trip"
    else: source.unlink()
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("location", ["inventory", "format", "identity", "producer", "fixture"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003', 'p004_case_004', 'p004_case_005'])
def test_unknown_fields_refuse_at_each_structured_boundary(small, location):
    root, _, _, value = small
    item = value["formats"][0]
    target = {"inventory": value, "format": item, "identity": item["identity"],
              "producer": item["producers"][0], "fixture": item["fixtures"][0]}[location]
    target["future"] = "not silently accepted"
    with pytest.raises(inventory.FormatInventoryError, match="exact"):
        inventory.validate_inventory(root, value)


@pytest.mark.parametrize("duplicate", ["id", "record", "reference"], ids=['p005_case_001', 'p005_case_002', 'p005_case_003'])
def test_duplicates_refuse(small, duplicate):
    root, _, _, value = small
    item = copy.deepcopy(value["formats"][0])
    if duplicate == "reference":
        value["formats"][0]["readers"].append(copy.deepcopy(item["readers"][0]))
    else:
        if duplicate == "record": item["id"] = "renamed-copy"
        value["formats"].append(item)
    with pytest.raises(inventory.FormatInventoryError, match="duplicat"):
        inventory.validate_inventory(root, value)


def test_numeric_and_unversioned_formats_are_explicit_manual_entries(small):
    root, source, _, value = small
    item = value["formats"][0]
    item["identity"] = {"kind": "numeric", "discriminator": "schema_version", "version": 1,
                        "binding": "VERSION", "shape": ["room"]}
    assert inventory.validate_inventory(root, value)
    assert inventory.gap_report(root, value)["manual_formats"] == ["example"]
    item["identity"]["version"] = 2
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)
    item["identity"].update(version=True)
    with pytest.raises(inventory.FormatInventoryError, match="numeric"):
        inventory.validate_inventory(root, value)
    item["identity"] = {"kind": "unversioned", "discriminator": None, "version": None,
                        "binding": None, "shape": ["room", "cursor"]}
    assert inventory.validate_inventory(root, value)
    source.write_text(source.read_text() + '\nUNMAPPED_NUMERIC = {"version": 99}\n', encoding="utf-8")
    assert not any(row["literal"] == 99 for row in inventory.gap_report(root, value)["unmapped_literals"])
    item["identity"]["shape"] = []
    with pytest.raises(inventory.FormatInventoryError, match="unversioned"):
        inventory.validate_inventory(root, value)


def test_missing_fixtures_need_an_explicit_gap_and_empty_input_never_uses_default(small):
    root, _, _, value = small
    value["formats"][0]["fixtures"] = []
    with pytest.raises(inventory.FormatInventoryError, match="fixture"):
        inventory.validate_inventory(root, value)
    value["formats"][0]["evidence_gaps"] = ["Historical refusal remains unproven."]
    assert inventory.gap_report(root, value)["evidence_gaps"]
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, {})


def test_canonical_output_and_gap_order_are_deterministic_and_detached(small):
    root, source, _, value = small
    source.write_text(source.read_text() + '\nX = "summon.z/v1"\nY = "summon.a/v1"\n', encoding="utf-8")
    first = inventory.canonical_inventory(root, value)
    assert json.loads(first) == value
    assert inventory.canonical_inventory(root, copy.deepcopy(value)) == first
    report = inventory.gap_report(root, value)
    assert [row["literal"] for row in report["unmapped_literals"]] == ["summon.a/v1", "summon.z/v1"]
    report["evidence_gaps"].append({"id": "invented", "gaps": []})
    assert inventory.gap_report(root, value)["evidence_gaps"] == []


def test_telemetry_numeric_metadata_preserves_reader_and_projection_limits():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    expected = {
        "telemetry-config": (1, "CONFIG_SCHEMA_VERSION", "private_authority"),
        "telemetry-event-legacy": (1, "LEGACY_EVENT_SCHEMA_VERSION", "private_evidence"),
        "telemetry-event-spool": (2, "EVENT_SCHEMA_VERSION", "private_evidence"),
        "telemetry-event-public": (2, "EVENT_SCHEMA_VERSION", "public_projection"),
    }
    report = inventory.gap_report(ROOT)
    for name, (version, binding, authority) in expected.items():
        item = entries[name]
        assert item["identity"]["kind"] == "numeric"
        assert item["identity"]["version"] == version
        assert item["identity"]["binding"] == binding
        assert item["authority"] == authority
        assert name in report["manual_formats"]
        assert item["fixtures"] and item["evidence_gaps"]
    assert entries["telemetry-event-legacy"]["producers"] == []
    assert "telemetry-event-legacy" in report["missing_producers"]
    assert "ignores schema" in " ".join(entries["telemetry-config"]["evidence_gaps"])
    for name in ("telemetry-event-legacy", "telemetry-event-spool"):
        assert "bool/float aliases" in " ".join(entries[name]["evidence_gaps"])
    assert entries["telemetry-event-spool"]["identity"]["shape"] != entries["telemetry-event-public"]["identity"]["shape"]
    assert "public allowlist" in " ".join(entries["telemetry-event-public"]["evidence_gaps"])


def test_durable_family_sources_preserve_nested_records_and_compatibility_gaps():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    expected = {
        "background-launch-v2": "write_prepared",
        "submission-accounting-handoff": "write_prepared",
        "workspace-protocol-v1": "compile_plan",
        "workspace-protocol-v2": "compile_plan_v2",
        "workspace-economics-reservation": "WorkspaceRuntime._v2_economics_reservation",
        "workspace-effects-observation": "fixed_effect_sources",
    }
    for identifier, writer in expected.items():
        item = entries[identifier]
        assert [ref["symbol"] for ref in item["producers"]] == [writer]
        assert item["fixtures"] and item["evidence_gaps"]
    for identifier in ("workspace-protocol-v1", "workspace-protocol-v2"):
        assert entries[identifier]["identity"]["discriminator"] == "protocol"
    for identifier in ("submission-accounting-handoff", "workspace-economics-reservation"):
        assert any("not an independent store" in gap for gap in entries[identifier]["evidence_gaps"])


@pytest.mark.parametrize("field", ["schema", "format", "protocol", "prompt_contract", "sessionStorage key"], ids=['p006_case_001', 'p006_case_002', 'p006_case_003', 'p006_case_004', 'p006_case_005'])
def test_literal_discriminator_accepts_only_explicit_contract_fields(small, field):
    root, _, _, value = small
    value["formats"][0]["identity"]["discriminator"] = field
    assert inventory.validate_inventory(root, value)["formats"][0]["identity"]["discriminator"] == field


@pytest.mark.parametrize("field", ["Protocol", "event", "version", "protocol ", "", None, 1], ids=['p007_case_001', 'p007_case_002', 'p007_case_003', 'p007_case_004', 'p007_case_005', 'p007_case_006', 'p007_case_007'])
def test_literal_discriminator_refuses_unrecognized_fields(small, field):
    root, _, _, value = small
    value["formats"][0]["identity"]["discriminator"] = field
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(root, value)


def test_protocol_discriminator_validates_wire_identity_without_schema_relabeling(small):
    root, _, _, value = small
    value["formats"][0]["identity"]["discriminator"] = "protocol"
    checked = inventory.validate_inventory(root, value)
    assert checked["formats"][0]["identity"]["discriminator"] == "protocol"
    value["formats"][0]["identity"]["discriminator"] = "unrecognized_wire_field"
    with pytest.raises(inventory.FormatInventoryError, match="literal format version"):
        inventory.validate_inventory(root, value)


def test_public_contract_batch_keeps_input_and_callable_roles_explicit():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    assert entries["workspace-plan-v1"]["producers"] == []
    assert entries["workspace-plan-v1"]["authority"] == "definition_input"
    for name in ("workspace-admission", "workspace-operator-send-base",
                 "workspace-supervisor-inbox-base", "workspace-worker-send-base"):
        item = entries[name]
        assert item["authority"] == "compatibility_fence"
        assert item["producers"][0]["symbol"].startswith("SwarmCoordinator.")
        assert item["readers"][0]["symbol"].startswith("WorkspaceRuntime.")
        assert item["evidence_gaps"]
    budget = entries["workspace-supervisor-offer-budget"]
    assert budget["authority"] == "private_authority"
    assert budget["producers"][0]["symbol"] == "SwarmCoordinator.consume_supervisor_offer_budget"
    assert any("Nested budget_admission" in gap for gap in budget["evidence_gaps"])
    assert len(entries["resume-launch-scope"]["producers"]) == 3
    assert entries["resume-capabilities-v2"]["fixtures"]
    assert "swarm-projection-rebuild" in inventory.gap_report(ROOT)["producer_only_detached"]
    assert not any(row["literal"] == "summon.swarm.projection-rebuild/v1"
                   for row in inventory.gap_report(ROOT)["unmapped_literals"])


def test_stable_operator_authority_is_distinct_from_supervisor_byte_observations():
    entries = {item["id"]: item for item in inventory.validate_inventory(ROOT)["formats"]}
    for suffix in ("operator-message-grant", "operator-scope"):
        assert entries["workspace-" + suffix]["authority"] == "private_authority"
    for suffix in ("offer", "receipt"):
        item = entries["workspace-supervisor-" + suffix + "-observation"]
        assert item["authority"] == "private_evidence"
        assert item["readers"] == [inventory._ref("_workspace_runtime", "WorkspaceRuntime.supervisor_inbox_command")]
        assert not item["fixtures"]
        assert any("No literal-specific behavioral fixture" in gap for gap in item["evidence_gaps"])
