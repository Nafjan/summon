"""Finite non-workspace format metadata checks; referenced runtimes never run."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("format_inventory_nonworkspace", ROOT / "tools/format_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)

# The finite source/literal omissions reviewed for this packet, not a promise of
# automatic discovery of every numeric, unversioned or structural contract.
TARGETS = {
    "_background": ("job-heartbeat/v1", "job-heartbeat/v2"),
    "_builder": ("agy-profile-lease/v2",),
    "_chat_resume": ("resume-capabilities/v1",),
    "_conversation_runtime": ("resume-capabilities/v2", "resume-launch-observation/v1"),
    "_decision": ("decision/v2",),
    "_deliberation_context": (
        "accept-stale-intent/v1", "accept-stale-intent/v2", "context-source-observation/v1",
        "deliberation-context-binding/v1", "deliberation-context-binding/v2",
        "deliberation-context-public/v1", "deliberation-context/v1"),
    "_evidence": ("decision/v2", "evidence/v1", "evidence/v2", "liveness/v1"),
    "_job_control": ("liveness/v1",),
    "_liveness": ("liveness/v1",),
    "_resume_capabilities": ("resume-capabilities/v1",),
    "_swarm_coordinator": ("swarm/v1",),
    "_swarm_protocol": ("swarm/v1",),
    "run_subagent": ("context-dispatch/v1", "decision/v2", "job-continuation/v1", "usage-status/v1"),
}


@pytest.fixture(scope="module")
def checked():
    return inventory.validate_inventory(ROOT)


@pytest.fixture(scope="module")
def entries(checked):
    return {row["id"]: row for row in checked["formats"]}


def test_finite_nonworkspace_literal_pairs_are_mapped_without_runtime_imports(checked):
    expected = {(inventory.SCRIPT_DIR + module + ".py", "summon." + version)
                for module, versions in TARGETS.items() for version in versions}
    assert len(expected) == 27
    assert not any("summon.workspace" in version for _, version in expected)
    assert expected <= set(inventory.discover_literals(ROOT))
    report = inventory.gap_report(ROOT, checked)
    unmapped = {(row["source"], row["literal"]) for row in report["unmapped_literals"]}
    assert not (expected & unmapped)
    assert not {pair for pair in unmapped if "summon.workspace" not in pair[1]}
    assert report["limits"] and report["manual_formats"] and report["evidence_gaps"]
    print({"format_rows": len(checked["formats"]), "new_source_literal_pairs": len(expected),
           "remaining_workspace_pairs": len(unmapped), "remaining_nonworkspace_pairs": 0})
    # conftest.py is pytest infrastructure, not a runtime module; its stem is shared
    # by every test root's conftest in sys.modules.
    runtime_names = {path.stem for path in (ROOT / inventory.SCRIPT_DIR).glob("*.py")
                     if not path.name.startswith("test_") and path.name != "conftest.py"}
    assert not (runtime_names & set(sys.modules))


def test_deliberation_input_and_prompt_share_literal_but_not_shape_or_reader(entries):
    supplied, prompt = entries["deliberation-context-input"], entries["deliberation-context-prompt"]
    assert supplied["identity"]["version"] == prompt["identity"]["version"]
    assert supplied["identity"]["shape"] == ["source", "freshness_policy", "entries"]
    assert prompt["identity"]["shape"] == ["freshness", "entries"]
    assert supplied["authority"] == "definition_input" and supplied["producers"] == []
    assert prompt["authority"] == "private_evidence"
    assert inventory._ref("_deliberation_context", "parse_context_packet") not in prompt["readers"]
    assert inventory._ref("_deliberation_scheduler", "_render_prompt") in prompt["readers"]


def test_historical_stale_intent_is_not_qualified_by_acceptance_none_fixture(entries):
    historical = entries["accept-stale-intent-v1"]
    current = entries["accept-stale-intent-v2"]
    assert historical["producers"] == historical["fixtures"] == []
    assert historical["authority"] == "private_evidence"
    assert "acceptance=None" in " ".join(historical["evidence_gaps"])
    assert current["producers"] == [] and current["fixtures"]
    assert current["authority"] == "private_authority"
    assert "runs_root_sha256" in current["identity"]["shape"]
    assert "runs_root_sha256" not in historical["identity"]["shape"]


def test_historical_binding_has_no_writer_or_current_prompt_reader(entries):
    old, current = entries["deliberation-context-binding-v1"], entries["deliberation-context-binding-v2"]
    assert old["producers"] == [] and old["authority"] == "private_evidence"
    assert inventory._ref("_deliberation_context", "parse_private_projection_readonly") in old["readers"]
    assert inventory._ref("_deliberation_context", "parse_private_projection") not in old["readers"]
    assert current["authority"] == "private_authority"
    assert "runs_root_sha256" in current["identity"]["shape"]
    assert any("acceptance=None" in gap for gap in old["evidence_gaps"])


def test_public_context_reader_is_rendering_not_schema_validation(entries):
    public = entries["deliberation-context-public"]
    assert public["authority"] == "public_projection"
    assert public["readers"] == [inventory._ref("_deliberation_ui", "render", "embedded_javascript")]
    assert any("does not validate" in gap for gap in public["evidence_gaps"])


def test_swarm_wire_protocol_is_distinct_from_nested_encoding_summary(entries):
    frame, summary = entries["swarm-wire-frame"], entries["swarm-encoding-compatibility"]
    assert frame["authority"] == "private_evidence"
    assert any("separate from coordinator membership, claim and lease authority" in gap
               for gap in frame["evidence_gaps"])
    assert frame["identity"]["version"] == summary["identity"]["version"]
    assert frame["identity"]["discriminator"] == "protocol"
    assert summary["identity"]["discriminator"] == "schema"
    assert frame["identity"]["shape"] != summary["identity"]["shape"]
    assert summary["readers"] == [inventory._ref("_swarm_coordinator", "rebuild_projection_from_journal")]
    assert any("no downstream schema reader" in gap for gap in summary["evidence_gaps"])
    assert entries["swarm-projection-rebuild"]["reader_contract"] == "producer_only_detached"
    assert entries["swarm-projection-rebuild"]["readers"] == []


def test_resume_registry_history_and_launch_observations_remain_distinct(entries):
    current, historical = entries["resume-capabilities-v1-current"], entries["resume-capabilities-v1-historical"]
    assert current["producers"] and historical["producers"] == []
    assert current["readers"] != historical["readers"]
    assert historical["authority"] == "private_evidence"
    scope = entries["chat-resume-registry-scope"]
    assert scope["identity"]["version"] == entries["resume-capabilities-v2"]["identity"]["version"]
    assert scope["identity"]["shape"] != entries["resume-capabilities-v2"]["identity"]["shape"]
    stored_reader = inventory._ref("_conversation_runtime", "_valid_stored_launch_observation")
    assert stored_reader in entries["launch-observation-persisted"]["readers"]
    assert stored_reader not in entries["launch-observation-fresh"]["readers"]
    assert inventory._ref("_conversation_runtime", "_launch_scope_observation") in entries["launch-observation-fresh"]["readers"]


@pytest.mark.parametrize("version", [1, 2], ids=['p001_case_001', 'p001_case_002'])
def test_private_heartbeat_and_public_projection_do_not_share_auth_material(entries, version):
    private, public = entries[f"job-heartbeat-v{version}"], entries[f"job-heartbeat-public-v{version}"]
    assert private["authority"] == "private_evidence"
    assert public["authority"] == "public_projection"
    assert "no auth or nonce" in public["identity"]["shape"]
    assert inventory._ref("_background", "run_jobs_query") in private["readers"]
    assert public["identity"]["version"] == private["identity"]["version"]
    if version == 1:
        assert private["producers"] == []
    assert any("legacy_unverified" in gap for gap in private["evidence_gaps"])


def test_liveness_forms_do_not_claim_serialized_emitter_authority(entries):
    rows = [entries[name] for name in ("liveness-snapshot", "liveness-sealed-evidence", "liveness-heartbeat-public")]
    assert len({tuple(row["identity"]["shape"]) for row in rows}) == 3
    assert {row["identity"]["version"] for row in rows} == {"summon.liveness/v1"}
    assert "sha256" in entries["liveness-sealed-evidence"]["identity"]["shape"]
    assert entries["liveness-heartbeat-public"]["authority"] == "public_projection"
    assert any("only the executor-held emitter" in gap for gap in entries["liveness-snapshot"]["evidence_gaps"])


def test_generic_v1_stays_writable_and_v2_is_not_an_implicit_migration(entries):
    first, second = entries["generic-evidence-v1"], entries["generic-evidence-v2"]
    assert first["producers"] == second["producers"] == [inventory._ref("_evidence", "seal")]
    assert first["identity"]["shape"] != second["identity"]["shape"]
    assert any("not reader-only" in gap for gap in first["evidence_gaps"])
    assert any("not authenticated provenance" in gap for gap in second["evidence_gaps"])


def test_output_wrappers_do_not_invent_fixtures_or_authority(entries):
    wrapper = entries["usage-status-combined"]
    assert wrapper["fixtures"] == [] and wrapper["evidence_gaps"]
    assert wrapper["readers"] == [inventory._ref("run_subagent", "_emit")]
    assert any("generic JSON/output consumer" in gap for gap in wrapper["evidence_gaps"])
    continuation = entries["job-continuation-public"]
    assert inventory._ref("run_subagent", "_emit") in continuation["producers"]
    assert any("No emitter-failure fixture" in gap for gap in continuation["evidence_gaps"])


@pytest.mark.parametrize("mutation", ["reader", "fixture", "missing_gap"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_new_refs_and_evidence_gaps_are_actually_validated(checked, mutation):
    value = copy.deepcopy(checked)
    if mutation == "missing_gap":
        row = next(row for row in value["formats"] if row["id"] == "accept-stale-intent-v1")
        row["evidence_gaps"] = []
    else:
        row = next(row for row in value["formats"] if row["id"] == "agy-profile-lease-v2")
        row["readers" if mutation == "reader" else "fixtures"][0]["symbol"] = "missing_inventory_reference"
    with pytest.raises(inventory.FormatInventoryError):
        inventory.validate_inventory(ROOT, value)
