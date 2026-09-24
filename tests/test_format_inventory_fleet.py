"""Source-only acceptance for ten fleet literals; no runtime fixtures execute."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_fleet", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture(scope="module")
def entries():
    return {row["id"]: row for row in inventory.validate_inventory(ROOT)["formats"]}


def test_fleet_batch_has_ten_literals_and_only_real_shape_variants(entries):
    literals = {"summon." + name + "/v1" for name in (
        "fleet", "fleet-plan", "fleet-activation-binding", "fleet-dispatch-anchor",
        "fleet-dispatch-claim", "fleet-dispatch-initialization", "fleet-dispatch-ledger",
        "fleet-dispatch", "fleet-launch-evidence", "fleet-terminal-projection")}
    rows = [row for row in entries.values() if row["identity"]["version"] in literals]
    assert len(rows) == 12
    assert {row["identity"]["version"] for row in rows} == literals
    assert all(row["fixtures"] and row["evidence_gaps"] and row["readers"] for row in rows)
    report = inventory.gap_report(ROOT)
    assert not any(row["literal"] in literals for row in report["unmapped_literals"])
    assert report["producer_only_detached"] == ["swarm-projection-rebuild"]


def test_fleet_declarations_and_public_receipts_do_not_replace_private_authority(entries):
    assert entries["fleet-draft"]["authority"] == "definition_input"
    assert entries["fleet-plan"]["authority"] == "public_projection"
    assert entries["fleet-dispatch-public"]["authority"] == "public_projection"
    for name in ("fleet-dispatch-ledger", "fleet-dispatch-claim", "fleet-activation-binding",
                 "fleet-dispatch-anchor", "fleet-dispatch-initialization"):
        assert entries[name]["authority"] == "private_authority"
    assert "authorization=advisory_only" in entries["fleet-plan"]["identity"]["shape"]
    assert "authorization=evidence_only" in entries["fleet-dispatch-public"]["identity"]["shape"]
    assert entries["fleet-dispatch-public"]["readers"] == [inventory._ref("_fleet_dispatch", "verify_public_receipt")]


def test_fleet_recovery_metadata_names_separate_authenticated_readers(entries):
    for identifier, writer, reader in (
        ("fleet-dispatch-anchor", "_write_anchor", "_validate_anchor"),
        ("fleet-dispatch-ledger", "_write_ledger", "_read_ledger"),
        ("fleet-dispatch-initialization", "_write_initialization_marker", "_read_initialization_marker"),
    ):
        item = entries[identifier]
        assert inventory._ref("_fleet_dispatch", writer) in item["producers"]
        assert inventory._ref("_fleet_dispatch", reader) in item["readers"]
        assert "mac" in item["identity"]["shape"]
        assert any("deliberate" in gap for gap in item["evidence_gaps"])
    assert inventory._ref("_fleet_dispatch", "claim_activation_provider_launch") in entries["fleet-activation-binding"]["producers"]


def test_launch_evidence_variants_preserve_actual_consumer_boundaries(entries):
    names = ("fleet-launch-evidence-base", "fleet-launch-evidence-observed", "fleet-launch-evidence-controlled")
    rows = [entries[name] for name in names]
    assert all(row["authority"] == "private_evidence" for row in rows)
    assert len({tuple(row["identity"]["shape"]) for row in rows}) == 3
    fleet_reader = inventory._ref("_fleet_dispatch", "_validate_launch_evidence")
    assert rows[0]["readers"] == [fleet_reader]
    assert fleet_reader in rows[1]["readers"] and fleet_reader in rows[2]["readers"]
    assert inventory._ref("_job_resume", "_validate_launch_observation") in rows[1]["readers"]
    assert inventory._ref("_chat_launch_guard", "_before_launch") in rows[2]["readers"]
    assert [len(row["identity"]["shape"]) for row in rows] == [8, 11, 10]
    assert "dispatch_payload_sha256" in rows[2]["identity"]["shape"]
    assert "attempt_id_sha256" in rows[2]["identity"]["shape"]
    assert "historical compatibility" in " ".join(rows[0]["evidence_gaps"])
    assert "nine fields" in " ".join(rows[0]["evidence_gaps"])
    assert "reachable ten-field callback" in " ".join(rows[2]["evidence_gaps"])
    assert "contract compatibility" in " ".join(rows[1]["evidence_gaps"])
    assert "does not independently validate" in " ".join(rows[2]["evidence_gaps"])


def test_terminal_projection_is_a_private_hash_preimage_not_a_public_reader(entries):
    item = entries["fleet-terminal-projection"]
    assert item["authority"] == "private_evidence"
    assert item["producers"] == [inventory._ref("_fleet_runtime", "_result_projection")]
    assert item["readers"] == [inventory._ref("_fleet_runtime", "_canonical_sha256")]
    assert "No schema-aware persisted projection reader" in " ".join(item["evidence_gaps"])
