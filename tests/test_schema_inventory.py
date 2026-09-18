from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "schema_inventory", ROOT / "tools" / "schema_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_source_bound_inventory_is_deterministic_and_complete():
    value = inventory.validate_inventory(ROOT)
    assert value["schema"] == "summon.schema-inventory/v1"
    assert {item["name"] for item in value["entities"]} == {
        "workspace", "task_attempt", "host_principal", "operator_authority",
        "worker", "worker_message", "operator_message", "delivery_settlement",
        "inbox_command", "content_transport", "turn_budget", "artifact_effect_evidence",
        "artifact_publication", "workspace_review", "workspace_decision",
        "review_decision", "conversation_resume",
    }
    assert {item["name"] for item in value["named_entities"]} == {
        "workspace", "task", "attempt", "principal", "worker", "message",
        "delivery", "command", "artifact", "review", "decision", "resume",
        "turn_budget",
    }
    assert {item["name"] for item in value["legacy_readers"]} == {
        "flat_cli_translation", "dispatcher_entry", "skill_entry_documentation",
        "legacy_job_reader",
    }
    resume = next(item for item in value["entities"]
                  if item["name"] == "conversation_resume")
    assert "skills/summon/scripts/_job_continuation.py" in resume["producers"]
    assert "skills/summon/scripts/_jobs.py" in resume["consumers"]
    assert ("skills/summon/scripts/test_job_continuation.py",
            "test_private_source_round_trip_and_public_projection_redacts_capabilities") in resume["fixtures"]
    assert ("skills/summon/scripts/test_job_control.py",
            "test_jobs_status_projects_authenticated_heartbeat_by_typed_schema") in resume["fixtures"]
    task_attempt = next(item for item in value["entities"]
                        if item["name"] == "task_attempt")
    assert "summon.swarm.projection-rebuild/v1" in task_attempt["schemas"]
    assert ("skills/summon/scripts/test_swarm_projection_rebuild.py",
            "test_f01_swarm_only_deterministic_detached_projection") in task_attempt["fixtures"]
    assert inventory.inventory_sha256(value) == inventory.inventory_sha256(value)


def test_inventory_binds_fixture_names_and_rejects_schema_drift():
    value = copy.deepcopy(inventory.SCHEMA_INVENTORY)
    value["entities"][0] = dict(value["entities"][0])
    value["entities"][0]["schemas"] = ("summon.workspace/v999",)
    with pytest.raises(inventory.SchemaInventoryError, match="schema literal"):
        inventory.validate_inventory(ROOT, value)


def test_inventory_rejects_missing_or_unsafe_source_and_fixture_paths():
    value = copy.deepcopy(inventory.SCHEMA_INVENTORY)
    value["entities"][0] = dict(value["entities"][0])
    value["entities"][0]["producers"] = ("C:/outside.py",)
    with pytest.raises(inventory.SchemaInventoryError, match="source paths"):
        inventory.validate_inventory(ROOT, value)

    value = copy.deepcopy(inventory.SCHEMA_INVENTORY)
    value["entities"][0] = dict(value["entities"][0])
    value["entities"][0]["fixtures"] = (("missing.py", "test_missing"),)
    with pytest.raises(inventory.SchemaInventoryError, match="regression fixture"):
        inventory.validate_inventory(ROOT, value)


def test_inventory_reports_unmapped_schema_sources_instead_of_claiming_completeness():
    sources = inventory.schema_source_paths(ROOT)
    gaps = inventory.unmapped_schema_sources(ROOT)
    assert sources
    assert gaps
    assert "skills/summon/scripts/run_subagent.py" in gaps
    assert "skills/summon/scripts/_usage.py" in gaps
    assert all(path in sources for path in gaps)


def test_inventory_rejects_unresolved_named_entity_or_legacy_reader():
    value = copy.deepcopy(inventory.SCHEMA_INVENTORY)
    value["named_entities"][0] = {"name": "workspace", "inventory_entities": ("missing",)}
    with pytest.raises(inventory.SchemaInventoryError, match="named entity binding"):
        inventory.validate_inventory(ROOT, value)

    value = copy.deepcopy(inventory.SCHEMA_INVENTORY)
    value["legacy_readers"][0] = dict(value["legacy_readers"][0], path="missing.py")
    with pytest.raises(inventory.SchemaInventoryError, match="legacy reader source"):
        inventory.validate_inventory(ROOT, value)
