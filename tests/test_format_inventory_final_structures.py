"""Source-only E12/fleet metadata reconciliation; runtime fixtures never import."""
from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_final_structures", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.fixture(scope="module")
def entries():
    return {row["id"]: row for row in inventory.validate_inventory(ROOT)["formats"]}


def test_replacement_is_optional_numeric_v2_history_with_real_constraint_readers(entries):
    row = entries["conversation-event-participant-replacement"]
    identity = row["identity"]
    assert (identity["kind"], identity["version"], identity["discriminator"], identity["binding"]) == (
        "numeric", 2, "schema_version", "SCHEMA_VERSION")
    assert row["authority"] == "durable_history"
    assert row["reader_contract"] == "required"
    assert {"record=conversation_event", "event=fork_created", "payload.participant_replacement (optional)",
            "participant_replacement.from_participant", "participant_replacement.to_participant",
            "participant_replacement.agent_definition_sha256", "participant_replacement.permission_ceiling",
            "parent_session_id", "child_session_id", "parent_schema_version", "parent_history_sha256", "lineage"} == set(identity["shape"])
    assert inventory._ref("_conversation_runtime", "ConversationRuntime.fork_turn") in row["producers"]
    assert inventory._ref("_conversation_runtime", "ConversationRuntime._fork") in row["producers"]
    for module, symbol in (("_conversation", "_public_payload"),
                           ("_conversation", "ConversationJournal._validate_records"),
                           ("_conversation_runtime", "ConversationRuntime._replacement_bindings"),
                           ("_conversation_runtime", "ConversationRuntime._identity_from_config"),
                           ("_conversation_runtime", "ConversationRuntime.fork_turn")):
        assert inventory._ref(module, symbol) in row["readers"]
    fixtures = {ref["symbol"] for ref in row["fixtures"]}
    assert {"test_replacement_uses_recorded_ceiling_without_original_catalog",
            "test_unknown_history_never_falls_back_to_current_catalog",
            "test_replacement_cannot_widen_recorded_authority",
            "test_same_name_descendant_preserves_frozen_replacement_definition",
            "test_second_participant_replacement_carries_first_binding_with_one_lineage",
            "test_ambiguous_replacement_records_refuse_without_launch"} <= fixtures
    gaps = " ".join(row["evidence_gaps"])
    for boundary in ("identical parent lineage", "parent journal remains unchanged", "without launching",
                     "never an authority fallback", "definition digest remains frozen", "not launch authorization",
                     "not executed by inventory validation", "older binaries"):
        assert boundary in gaps
    assert entries["conversation-event"]["identity"]["version"] == 2
    assert entries["conversation-event-v1"]["producers"] == []


def _declared_launch_shapes():
    """Interpret only the finite module-level set expressions, never source code."""
    tree = ast.parse((ROOT / "skills/summon/scripts/_fleet_dispatch.py").read_text(encoding="utf-8"))
    names = {"_LAUNCH_EVIDENCE_FIELDS", "_CURRENT_LAUNCH_EVIDENCE_FIELDS", "_LAUNCH_EVIDENCE_SHAPES"}
    values = {}

    def finite(node):
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return node.value
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.Set):
            return frozenset(finite(item) for item in node.elts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return finite(node.left) | finite(node.right)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset" and len(node.args) == 1 and not node.keywords):
            return frozenset(finite(node.args[0]))
        raise AssertionError("launch shape declaration needs a new source review")

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in names:
                assert name not in values
                values[name] = finite(node.value)
    assert set(values) == names
    return values["_LAUNCH_EVIDENCE_SHAPES"]


@pytest.mark.parametrize("identifier,count", [
    ("fleet-launch-evidence-base", 8),
    ("fleet-launch-evidence-controlled", 10),
    ("fleet-launch-evidence-observed", 11),
], ids=["historical_eight", "current_ten", "observed_eleven"])
def test_fleet_metadata_matches_exact_source_declared_envelopes(entries, identifier, count):
    row = entries[identifier]
    assert row["identity"]["version"] == "summon.fleet-launch-evidence/v1"
    assert row["authority"] == "private_evidence"
    shape = frozenset(row["identity"]["shape"])
    assert len(shape) == count
    declared = _declared_launch_shapes()
    assert shape in declared
    assert {len(fields) for fields in declared} == {8, 10, 11}
    assert inventory._ref("_fleet_dispatch", "_validate_launch_evidence") in row["readers"]


def test_fleet_reachability_and_observation_qualification_remain_separate(entries):
    base = entries["fleet-launch-evidence-base"]
    current = entries["fleet-launch-evidence-controlled"]
    observed = entries["fleet-launch-evidence-observed"]
    assert "launch_observation" not in current["identity"]["shape"]
    assert set(observed["identity"]["shape"]) == set(current["identity"]["shape"]) | {"launch_observation"}
    assert inventory._ref("_executor", "ProviderLaunchControl.before_provider_launch") in current["readers"]
    assert inventory._ref("_fleet_runtime", "FleetLaunchRuntime._claim") in current["readers"]
    assert observed["producers"] == [inventory._ref("_executor", "execute_agent")]
    assert "reachable ten-field callback" in " ".join(current["evidence_gaps"])
    assert "executor-generated attempt digest is audit evidence" in " ".join(current["evidence_gaps"])
    assert "contract compatibility" in " ".join(observed["evidence_gaps"])
    assert "does not request observations" in " ".join(observed["evidence_gaps"])
    assert "adds no freshness, authenticated qualification or launch authority" in " ".join(observed["evidence_gaps"])
    assert "nine fields" in " ".join(base["evidence_gaps"])
    assert "partial enrichment, unknown fields and null observations refuse" in " ".join(base["evidence_gaps"])
    assert "without requiring the fleet envelope's exact keys" in " ".join(observed["evidence_gaps"])
    assert "not independently validate the fleet schema discriminator" in " ".join(observed["evidence_gaps"])
    callback = inventory._test("phase1_fleet_launch_evidence", "test_callback_commits_complete_evidence_once_and_keeps_it_private")
    refusal = inventory._test("phase1_fleet_launch_evidence", "test_invalid_current_evidence_does_not_consume_capacity")
    observation = inventory._test("phase1_fleet_launch_evidence", "test_observation_shape_and_every_shared_binding_are_checked")
    for row in (base, current, observed):
        assert callback in row["fixtures"]
        assert "synthetic callback acceptance" in " ".join(row["evidence_gaps"])
        assert "authenticated synthetic ledger" in " ".join(row["evidence_gaps"])
        assert "full executor execution" in " ".join(row["evidence_gaps"])
        assert "No repaired" not in " ".join(row["evidence_gaps"])
    for row in (current, observed):
        assert refusal in row["fixtures"]
    assert observation in observed["fixtures"]
    assert "normal fleet control requests no observation" in " ".join(observed["evidence_gaps"])


@pytest.mark.parametrize("mutation", ["version", "reader", "fixture"], ids=[
    "replacement_unknown_numeric_version", "replacement_missing_qualified_reader", "replacement_missing_fixture",
])
def test_replacement_reference_and_numeric_drift_refuse(entries, mutation):
    row = copy.deepcopy(entries["conversation-event-participant-replacement"])
    if mutation == "version":
        row["identity"]["version"] = 3
    elif mutation == "reader":
        row["readers"][0]["symbol"] = "ConversationJournal._missing_replacement_reader"
    else:
        row["fixtures"][0]["symbol"] = "test_missing_replacement_acceptance"
    value = {"schema": inventory.SCHEMA, "version": inventory.VERSION, "formats": [row]}
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(ROOT, value)


def test_literal_report_keeps_manual_and_runtime_limits(entries):
    report = inventory.gap_report(ROOT)
    assert "conversation-event-participant-replacement" in report["manual_formats"]
    assert report["producer_only_detached"] == ["swarm-projection-rebuild"]
    assert report["evidence_gaps"]
    assert any("not runtime acceptance" in limit for limit in report["limits"])


def test_release_evidence_nested_records_are_exactly_two_source_bound_rows(entries):
    rows = {
        identifier: entries[identifier]
        for identifier in (
            "release-evidence-platform-qualification",
            "release-evidence-rendered-bundle",
        )
    }
    assert set(rows) == {
        "release-evidence-platform-qualification",
        "release-evidence-rendered-bundle",
    }
    expected = {
        "release-evidence-platform-qualification": (
            "_platform_qualification",
            "_validate_platform_qualification",
            [
                "schema", "host", "windows_evidence", "windows_scoped_gates", "status",
            ],
        ),
        "release-evidence-rendered-bundle": (
            "_collect_rendered_evidence",
            "_validate_rendered_evidence",
            [
                "schema", "policy", "producer", "source_tree_sha256", "producers",
                "files[name,bytes,sha256]",
            ],
        ),
    }
    for identifier, (producer, reader, nested_shape) in expected.items():
        row = rows[identifier]
        assert row["identity"] == {
            "kind": "numeric",
            "discriminator": "schema",
            "version": 1,
            "binding": None,
            "shape": ["outer_schema=release-evidence/v2", *nested_shape],
        }
        assert row["authority"] == "private_evidence"
        assert row["producers"] == [inventory._tool_ref("release_gates", producer)]
        assert row["readers"] == [inventory._tool_ref("release_manifest", reader)]
        assert len(row["fixtures"]) == 2
        assert all(item["path"].startswith("tests/") for item in row["fixtures"])
        assert row["evidence_gaps"]
