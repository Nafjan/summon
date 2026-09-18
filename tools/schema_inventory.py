"""Machine-checked schema/reader inventory for the workspace preview.

This is a source-bound inventory, not a migration engine.  It deliberately
contains only repository-relative paths and schema literals; validation proves
that the named producers, readers, and regression fixtures still exist in the
current source tree.  It never launches a provider or mutates a workspace.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


INVENTORY_SCHEMA = "summon.schema-inventory/v1"
ROOT = Path(__file__).resolve().parents[1]


_ENTITIES = (
    {
        "name": "workspace",
        "schemas": ("summon.workspace/v1",),
        "producers": ("skills/summon/scripts/_workspace_protocol.py",),
        "consumers": ("skills/summon/scripts/_workspace_state.py",
                      "skills/summon/scripts/_workspace_runtime.py"),
        "legacy_policy": "feature_marker_required_refuse_unknown",
        "fixtures": (("skills/summon/scripts/test_workspace_state.py",
                       "test_feature_marker_required_and_old_pure_reducer_refuses_without_mutation"),),
    },
    {
        "name": "task_attempt",
        "schemas": ("summon.workspace.plan/v1", "summon.swarm/v1",
                    "summon.swarm.projection-rebuild/v1"),
        "producers": ("skills/summon/scripts/_workspace_plan.py",
                      "skills/summon/scripts/_swarm_protocol.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "consumers": ("skills/summon/scripts/_workspace_entry.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "legacy_policy": "immutable_id_explicit_rebind",
        "fixtures": (("skills/summon/scripts/test_workspace_plan.py",
                       "test_compile_generates_inert_authority_and_message_grants"),
                      ("skills/summon/scripts/test_workspace_entry.py",
                       "test_plan_destination_has_no_worker_mutation_authority"),
                      ("skills/summon/scripts/test_swarm_projection_rebuild.py",
                       "test_f01_swarm_only_deterministic_detached_projection"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_plan_readers_refuse_unsupported_schema_and_duplicate_identity"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_supported_plan_reads_and_compiles_the_same_task_identity")),
    },
    {
        "name": "worker",
        "schemas": ("summon.swarm/v1",),
        "producers": ("skills/summon/scripts/_swarm_protocol.py",),
        "consumers": ("skills/summon/scripts/_swarm_coordinator.py",),
        "legacy_policy": "registration_is_not_task_authority",
        "fixtures": (("skills/summon/scripts/test_swarm_protocol.py",
                       "test_worker_registration_binds_project_and_roster"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_public_registration_refuses_malformed_fields_without_mutation"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_public_registration_frame_reader_refuses_unknown_shape")),
    },
    {
        "name": "host_principal",
        "schemas": ("summon.workspace-host/v1", "summon.workspace-host/v2"),
        "producers": ("skills/summon/scripts/_workspace_entry.py",
                      "skills/summon/scripts/_workspace_plan.py"),
        "consumers": ("skills/summon/scripts/_workspace_entry.py",),
        "legacy_policy": "v1_preserve_no_schema_upgrade",
        "fixtures": (("skills/summon/scripts/test_workspace_entry.py",
                       "test_legacy_host_reopen_preserves_scope_identity_without_schema_upgrade"),
                      ("skills/summon/scripts/test_workspace_entry.py",
                       "test_host_config_rejects_cross_version_field_sets")),
    },
    {
        "name": "operator_authority",
        "schemas": ("summon.workspace.operator-message-grant/v1",
                     "summon.workspace.operator-scope/v1"),
        "producers": ("skills/summon/scripts/_workspace_plan.py",
                      "skills/summon/scripts/_workspace_entry.py"),
        "consumers": ("skills/summon/scripts/_workspace_runtime.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "legacy_policy": "scope_revalidated_at_append",
        "fixtures": (("skills/summon/scripts/test_workspace_operator_runtime.py",
                       "test_runtime_conjunct_refuses_final_sync_revocation_without_false_noop"),),
    },
    {
        "name": "worker_message",
        "schemas": ("summon.workspace.message-send/v1",
                     "summon.workspace.worker-send-base/v1"),
        "producers": ("skills/summon/scripts/_workspace_admission.py",),
        "consumers": ("skills/summon/scripts/_swarm_coordinator.py",
                      "skills/summon/scripts/_workspace_runtime.py"),
        "legacy_policy": "stale_claim_or_epoch_refuses",
        "fixtures": (("skills/summon/scripts/test_workspace_worker_send.py",
                       "test_current_authority_mismatches_refuse_with_unchanged_journal"),),
    },
    {
        "name": "operator_message",
        "schemas": ("summon.workspace.operator-send-base/v1",),
        "producers": ("skills/summon/scripts/_workspace_admission.py",),
        "consumers": ("skills/summon/scripts/_swarm_coordinator.py",),
        "legacy_policy": "operation_key_idempotent_conflict",
        "fixtures": (("skills/summon/scripts/test_workspace_operator_admission.py",
                       "test_actual_send_is_one_atomic_queue_and_reopened_lookup_never_resends"),),
    },
    {
        "name": "delivery_settlement",
        "schemas": ("summon.workspace/v1",),
        "producers": ("skills/summon/scripts/_workspace_protocol.py",),
        "consumers": ("skills/summon/scripts/_workspace_state.py",
                      "skills/summon/scripts/_workspace_runtime.py"),
        "legacy_policy": "typed_transition_preserves_uncertainty",
        "fixtures": (("skills/summon/scripts/test_workspace_protocol.py",
                       "test_effect_resolution_cannot_change_existing_delivery_facts"),
                      ("skills/summon/scripts/test_workspace_state.py",
                       "test_content_holds_need_scoped_registered_observation_and_preserve_facts")),
    },
    {
        "name": "inbox_command",
        "schemas": ("summon.workspace.operator-command-request/v1",
                     "summon.workspace.supervisor-inbox-base/v1"),
        "producers": ("skills/summon/scripts/_workspace_admission.py",
                      "skills/summon/scripts/_workspace_commands.py"),
        "consumers": ("skills/summon/scripts/_swarm_coordinator.py",
                      "skills/summon/scripts/_workspace_runtime.py"),
        "legacy_policy": "current_consumer_or_explicit_recovery_only",
        "fixtures": (("skills/summon/scripts/test_workspace_inbox_recovery.py",
                       "test_old_epoch_other_sender_same_endpoint_blocks_until_explicit_hold"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_public_operator_command_refuses_unsupported_source_schema_without_mutation"),
                      ("skills/summon/scripts/test_f01_strict_entity_acceptance.py",
                       "test_supported_operator_command_producer_is_read_durably_without_task_authority")),
    },
    {
        "name": "content_transport",
        "schemas": ("summon.workspace.pipe/v1", "summon.workspace.context/v1",
                     "summon.workspace.message-send-result/v1"),
        "producers": ("skills/summon/scripts/_workspace_transport.py",),
        "consumers": ("skills/summon/scripts/_workspace_runtime.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "legacy_policy": "complete_digest_bound_payload_only",
        "fixtures": (("skills/summon/scripts/test_workspace_transport.py",
                       "test_compiled_context_malformed_or_cross_bound_refuses_before_frame_send"),),
    },
    {
        "name": "turn_budget",
        "schemas": ("summon.workspace.admission-policy/v1",
                     "summon.workspace.admission-snapshot/v1",
                     "summon.workspace.admission-request/v1",
                     "summon.workspace.admission-result/v1",
                     "summon.workspace.admission-decision/v1",
                     "summon.workspace.turn-budget-admission/v1",
                     "summon.workspace.supervisor-offer-budget/v1"),
        "producers": ("skills/summon/scripts/_workspace_budget.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "consumers": ("skills/summon/scripts/_workspace_transport.py",
                      "skills/summon/scripts/_swarm_coordinator.py",
                      "skills/summon/scripts/_workspace_runtime.py"),
        "legacy_policy": "authoritative_preclaim_reservation_consumed_before_spawn",
        "fixtures": (("skills/summon/scripts/test_workspace_budget.py",
                       "test_message_acceptance_is_separate_from_execution_slot_deferral"),
                      ("skills/summon/scripts/test_workspace_reservation_acceptance.py",
                       "test_consumed_reservation_rebuilds_and_refuses_second_coordinator_object"),
                      ("skills/summon/scripts/test_workspace_reservation_acceptance.py",
                       "test_snapshot_binds_selected_recovery_successor_in_either_delivery_order"),
                      ("skills/summon/scripts/test_workspace_demo.py",
                       "test_supervisor_offer_budget_refuses_rate_stream_and_oversize_before_write")),
    },
    {
        "name": "artifact_effect_evidence",
        "schemas": ("summon.workspace.effects-observation/v1",),
        "producers": ("skills/summon/scripts/_workspace_protocol.py",),
        "consumers": ("skills/summon/scripts/_workspace_runtime.py",
                      "skills/summon/scripts/_workspace_state.py"),
        "legacy_policy": "source_bound_qualification_required",
        "fixtures": (("skills/summon/scripts/test_workspace_runtime.py",
                       "test_effect_sources_require_actual_matching_closed_owned_worker"),),
    },
    {
        "name": "artifact_publication",
        "schemas": ("summon.swarm/v1", "summon.swarm.projection-rebuild/v1"),
        "producers": ("skills/summon/scripts/_swarm_protocol.py",
                      "skills/summon/scripts/_swarm_coordinator.py"),
        "consumers": ("skills/summon/scripts/_swarm_coordinator.py",),
        "legacy_policy": "claim_fenced_identity_refuse_new_frame_reuse",
        "fixtures": (("skills/summon/scripts/test_swarm_coordinator.py",
                       "test_artifact_is_claim_fenced_and_never_dereferenced"),
                      ("skills/summon/scripts/test_swarm_coordinator.py",
                       "test_new_frame_reusing_artifact_id_refuses_before_append"),
                      ("skills/summon/scripts/test_f01_artifact_identity_acceptance.py",
                       "test_identical_metadata_new_frame_refuses_before_append"),
                      ("skills/summon/scripts/test_f01_artifact_identity_acceptance.py",
                       "test_fresh_artifact_id_still_publishes_and_replays"),
                      ("skills/summon/scripts/test_swarm_projection_rebuild.py",
                       "test_f01_swarm_only_deterministic_detached_projection")),
    },
    {
        "name": "workspace_review",
        "schemas": ("summon.workspace/v1",),
        "producers": ("skills/summon/scripts/_workspace_protocol.py",
                      "skills/summon/scripts/_workspace_state.py"),
        "consumers": ("skills/summon/scripts/_workspace_state.py",
                      "skills/summon/scripts/_workspace_view.py"),
        "legacy_policy": "assessment_identity_scoped_non_authority",
        "fixtures": (("skills/summon/scripts/test_workspace_review_decision_acceptance.py",
                       "test_exact_entity_event_refuses_unknown_format_without_state_mutation"),),
    },
    {
        "name": "workspace_decision",
        "schemas": ("summon.workspace/v1",),
        "producers": ("skills/summon/scripts/_workspace_protocol.py",
                      "skills/summon/scripts/_workspace_state.py"),
        "consumers": ("skills/summon/scripts/_workspace_state.py",
                      "skills/summon/scripts/_workspace_view.py"),
        "legacy_policy": "assessment_bound_next_lane_no_approval_authority",
        "fixtures": (("skills/summon/scripts/test_workspace_review_decision_acceptance.py",
                       "test_entity_identity_cannot_be_reused_with_a_fresh_operation"),),
    },
    {
        "name": "review_decision",
        "schemas": ("summon.decision/v2", "summon.deliberation-context/v1",
                     "summon.deliberation-context-binding/v2",
                     "summon.deliberation-context-binding/v1"),
        "producers": ("skills/summon/scripts/_decision.py",
                      "skills/summon/scripts/_deliberation_context.py"),
        "consumers": ("skills/summon/scripts/_deliberation_replay.py",
                      "skills/summon/scripts/_deliberation_context.py"),
        "legacy_policy": "legacy_context_readonly_not_authority",
         "fixtures": (("skills/summon/scripts/test_deliberation_replay.py",
                        "test_historical_v1_context_replays_read_only_but_is_not_current_authority"),),
    },
    {
        "name": "conversation_resume",
        "schemas": ("summon.resume-capabilities/v1", "summon.resume-capabilities/v2",
                     "summon.resume-launch-scope/v1",
                     "summon.job-continuation-source/v1",
                     "summon.job-continuation-source/v2"),
        "producers": ("skills/summon/scripts/_resume_capabilities.py",
                      "skills/summon/scripts/_conversation.py",
                      "skills/summon/scripts/_job_continuation.py"),
        "consumers": ("skills/summon/scripts/_conversation_runtime.py",
                      "skills/summon/scripts/_chat_resume.py",
                      "skills/summon/scripts/_jobs.py"),
        "legacy_policy": "v1_history_v2_precontact_scope",
        "fixtures": (("skills/summon/scripts/test_resume_capabilities.py",
                       "test_v2_launch_scope_refuses_stale_facts_before_contact"),
                      ("skills/summon/scripts/test_chat_resume.py",
                       "test_historical_v1_refusal_is_readable_but_not_current_authority"),
                      ("skills/summon/scripts/test_conversation_runtime.py",
                       "test_v2_launch_scope_refusal_happens_before_provider_contact"),
                      ("skills/summon/scripts/test_conversation_runtime.py",
                       "test_legacy_process_record_lookup_is_read_only_and_does_not_duplicate_scope"),
                      ("skills/summon/scripts/test_conversation.py",
                       "test_legacy_room_id_is_readable_without_relaunch_or_duplicate_room"),
                      ("skills/summon/scripts/test_job_continuation.py",
                       "test_private_source_round_trip_and_public_projection_redacts_capabilities"),
                      ("tests/test_profile_resume_rotation_acceptance.py",
                       "test_profile_rotation_before_child_load_refuses_without_baselining"),
                      ("tests/test_profile_resume_rotation_acceptance.py",
                       "test_legacy_profile_binding_is_readable_but_resume_refuses"),
                      ("tests/test_profile_resume_rotation_acceptance.py",
                       "test_profile_binding_resolve_failure_refuses_without_claim_mutation"),
                      ("tests/test_profile_resume_rotation_acceptance.py",
                       "test_v2_profile_source_is_claim_bound_and_public_projection_redacts_it"),
                      ("skills/summon/scripts/test_job_control.py",
                       "test_jobs_status_projects_authenticated_heartbeat_by_typed_schema")),
    },
)

# These names are the migration workstream's required entity vocabulary.  A
# named entity may intentionally bind to more than one conceptual row (for
# example, worker and operator messages have different admission authorities).
# Keeping the binding explicit prevents a broad conceptual row from hiding a
# missing producer, reader, schema, or focused fixture.
_NAMED_ENTITIES = (
    {"name": "workspace", "inventory_entities": ("workspace",)},
    {"name": "task", "inventory_entities": ("task_attempt",)},
    {"name": "attempt", "inventory_entities": ("task_attempt",)},
    {"name": "principal", "inventory_entities": ("host_principal",)},
    {"name": "worker", "inventory_entities": ("worker",)},
    {"name": "message", "inventory_entities": ("worker_message", "operator_message")},
    {"name": "delivery", "inventory_entities": ("delivery_settlement",)},
    {"name": "turn_budget", "inventory_entities": ("turn_budget",)},
    {"name": "command", "inventory_entities": ("inbox_command",)},
    {"name": "artifact", "inventory_entities": ("artifact_publication",)},
    {"name": "review", "inventory_entities": ("workspace_review",)},
    {"name": "decision", "inventory_entities": ("workspace_decision",)},
    {"name": "resume", "inventory_entities": ("conversation_resume",)},
)

# Legacy entry points are classified as readers/translators, never as launch
# authority.  Fixture references keep the compatibility claim source-bound.
_LEGACY_READERS = (
    {
        "name": "flat_cli_translation",
        "path": "skills/summon/scripts/_cli.py",
        "authority": "translation_only",
        "fixtures": (("skills/summon/scripts/test_phase1_compatibility.py",
                       "test_legacy_flat_argv_is_the_same_object_and_preserves_multiline_prompt"),),
    },
    {
        "name": "dispatcher_entry",
        "path": "skills/summon/scripts/run_subagent.py",
        "authority": "preflight_then_dispatch",
        "fixtures": (("skills/summon/scripts/test_phase0_contracts.py",
                       "_dispatch_with_retries"),),
    },
    {
        "name": "skill_entry_documentation",
        "path": "skills/summon/SKILL.md",
        "authority": "documentation_only",
        "fixtures": (("skills/summon/scripts/test_discovery.py",
                       "test_v8_every_public_flag_is_documented_in_skill_md"),),
    },
    {
        "name": "legacy_job_reader",
        "path": "skills/summon/scripts/_jobs.py",
        "authority": "read_only_projection",
        "fixtures": (("skills/summon/scripts/test_job_control.py",
                       "test_control_summary_labels_valid_legacy_commands_unverified"),),
    },
)

_SCHEMA_LITERAL_RE = re.compile(r"['\"](summon\.[A-Za-z0-9_.:-]+/v[0-9]+)['\"]")

SCHEMA_INVENTORY: dict[str, Any] = {
    "schema": INVENTORY_SCHEMA,
    "version": 1,
    "entities": [copy.deepcopy(entity) for entity in _ENTITIES],
    "named_entities": [copy.deepcopy(entity) for entity in _NAMED_ENTITIES],
    "legacy_readers": [copy.deepcopy(reader) for reader in _LEGACY_READERS],
}


class SchemaInventoryError(ValueError):
    """The source-bound schema inventory is malformed or stale."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SchemaInventoryError("inventory is not canonical JSON") from exc


def validate_inventory(root: Path = ROOT, value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate source paths, schema literals, and named regression fixtures."""
    root = Path(root).resolve()
    source = copy.deepcopy(dict(value or SCHEMA_INVENTORY))
    if source.get("schema") != INVENTORY_SCHEMA or source.get("version") != 1:
        raise SchemaInventoryError("unsupported schema inventory version")
    entities = source.get("entities")
    if type(entities) is not tuple and type(entities) is not list:
        raise SchemaInventoryError("schema inventory entities must be a list")
    names: set[str] = set()
    entity_by_name: dict[str, dict[str, Any]] = {}
    for entity in entities:
        if type(entity) is not dict:
            raise SchemaInventoryError("schema inventory entity must be an object")
        required = {"name", "schemas", "producers", "consumers", "legacy_policy", "fixtures"}
        if set(entity) != required:
            raise SchemaInventoryError("schema inventory entity fields are not exact")
        name = entity["name"]
        if type(name) is not str or not name or name in names:
            raise SchemaInventoryError("schema inventory entity name is invalid or duplicated")
        names.add(name)
        entity_by_name[name] = entity
        schemas = entity["schemas"]
        if type(schemas) not in (tuple, list) or not schemas or any(
                type(schema) is not str or not schema.startswith("summon.") for schema in schemas):
            raise SchemaInventoryError(f"schema inventory schemas invalid for {name}")
        paths = tuple(entity["producers"]) + tuple(entity["consumers"])
        if not paths or any(type(path) is not str or Path(path).is_absolute() for path in paths):
            raise SchemaInventoryError(f"schema inventory source paths invalid for {name}")
        texts: dict[str, str] = {}
        for relative in paths:
            path = root / relative
            if not path.is_file():
                raise SchemaInventoryError(f"schema inventory source is missing: {relative}")
            texts[relative] = path.read_text(encoding="utf-8")
        if any(not any(schema in text for text in texts.values()) for schema in schemas):
            raise SchemaInventoryError(f"schema literal is absent from source for {name}")
        policy = entity["legacy_policy"]
        if type(policy) is not str or not policy:
            raise SchemaInventoryError(f"legacy policy is missing for {name}")
        fixtures = entity["fixtures"]
        if type(fixtures) not in (tuple, list) or not fixtures:
            raise SchemaInventoryError(f"regression fixtures are missing for {name}")
        for fixture in fixtures:
            if type(fixture) not in (tuple, list) or len(fixture) != 2:
                raise SchemaInventoryError(f"fixture reference is malformed for {name}")
            relative, symbol = fixture
            if type(relative) is not str or Path(relative).is_absolute() or type(symbol) is not str or not symbol:
                raise SchemaInventoryError(f"fixture reference is unsafe for {name}")
            path = root / relative
            if not path.is_file() or symbol not in path.read_text(encoding="utf-8"):
                raise SchemaInventoryError(f"regression fixture is missing for {name}")
    named_entities = source.get("named_entities")
    if type(named_entities) not in (tuple, list) or not named_entities:
        raise SchemaInventoryError("named entity bindings are missing")
    named_names: set[str] = set()
    for binding in named_entities:
        if type(binding) is not dict or set(binding) != {"name", "inventory_entities"}:
            raise SchemaInventoryError("named entity binding fields are not exact")
        name = binding["name"]
        refs = binding["inventory_entities"]
        if type(name) is not str or not name or name in named_names:
            raise SchemaInventoryError("named entity binding name is invalid or duplicated")
        if type(refs) not in (tuple, list) or not refs or any(
                type(ref) is not str or ref not in entity_by_name for ref in refs):
            raise SchemaInventoryError(f"named entity binding is unresolved: {name}")
        for ref in refs:
            entity = entity_by_name[ref]
            if not entity["producers"] or not entity["consumers"] or not entity["schemas"] or not entity["fixtures"]:
                raise SchemaInventoryError(f"named entity binding is incomplete: {name}")
        named_names.add(name)
        expected_named = {"workspace", "task", "attempt", "principal", "worker", "message",
                          "delivery", "command", "artifact", "review", "decision", "resume",
                          "turn_budget"}
    if named_names != expected_named:
        raise SchemaInventoryError("named entity set is incomplete or has unsupported names")

    legacy_readers = source.get("legacy_readers")
    if type(legacy_readers) not in (tuple, list) or not legacy_readers:
        raise SchemaInventoryError("legacy reader inventory is missing")
    reader_names: set[str] = set()
    for reader in legacy_readers:
        if type(reader) is not dict or set(reader) != {"name", "path", "authority", "fixtures"}:
            raise SchemaInventoryError("legacy reader fields are not exact")
        name = reader["name"]
        relative = reader["path"]
        authority = reader["authority"]
        fixtures = reader["fixtures"]
        if (type(name) is not str or not name or name in reader_names or
                type(relative) is not str or Path(relative).is_absolute() or
                type(authority) is not str or not authority):
            raise SchemaInventoryError("legacy reader identity is invalid")
        if not (root / relative).is_file():
            raise SchemaInventoryError(f"legacy reader source is missing: {relative}")
        if type(fixtures) not in (tuple, list) or not fixtures:
            raise SchemaInventoryError(f"legacy reader fixtures are missing: {name}")
        for fixture in fixtures:
            if type(fixture) not in (tuple, list) or len(fixture) != 2:
                raise SchemaInventoryError(f"legacy reader fixture is malformed: {name}")
            fixture_path, symbol = fixture
            if (type(fixture_path) is not str or Path(fixture_path).is_absolute() or
                    type(symbol) is not str or not symbol):
                raise SchemaInventoryError(f"legacy reader fixture is unsafe: {name}")
            path = root / fixture_path
            if not path.is_file() or symbol not in path.read_text(encoding="utf-8"):
                raise SchemaInventoryError(f"legacy reader fixture is missing: {name}")
        reader_names.add(name)
    source["entities"] = [copy.deepcopy(entity) for entity in entities]
    source["named_entities"] = [copy.deepcopy(binding) for binding in named_entities]
    source["legacy_readers"] = [copy.deepcopy(reader) for reader in legacy_readers]
    return source


def schema_source_paths(root: Path = ROOT) -> tuple[str, ...]:
    """Return production files containing versioned ``summon.*`` literals."""
    root = Path(root).resolve()
    scripts = root / "skills" / "summon" / "scripts"
    result: list[str] = []
    for path in sorted(scripts.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        if _SCHEMA_LITERAL_RE.search(path.read_text(encoding="utf-8")):
            result.append(path.relative_to(root).as_posix())
    return tuple(result)


def unmapped_schema_sources(root: Path = ROOT, value: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Report schema-bearing production files not named as entity writers/readers.

    This is an audit result, not an allowlist.  A new schema-bearing source
    stays visible until a maintainer classifies it, so the inventory cannot be
    mistaken for a complete migration manifest.
    """
    checked = validate_inventory(root, value)
    covered = {path for entity in checked["entities"]
               for path in tuple(entity["producers"]) + tuple(entity["consumers"])}
    return tuple(path for path in schema_source_paths(root) if path not in covered)


def inventory_sha256(value: Mapping[str, Any] | None = None) -> str:
    """Return a deterministic digest of the logical inventory only."""
    return hashlib.sha256(_canonical(validate_inventory(value=value))).hexdigest()


__all__ = ["INVENTORY_SCHEMA", "SCHEMA_INVENTORY", "SchemaInventoryError",
           "inventory_sha256", "schema_source_paths", "unmapped_schema_sources",
           "validate_inventory"]
