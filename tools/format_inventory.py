"""Bounded, source-only format inventory, independent of schema_inventory.

Validation checks metadata and source references, not runtime acceptance or an
absence of migration gaps. Literal discovery is lexical and limited to production
Python scripts (including embedded JavaScript). Numeric/unversioned records and
structural variants are manually enumerated; discovery is NOT all-format complete.
No runtime modules are imported and no private state is read.
"""
from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
LEGACY_SCHEMA = "summon.format-inventory/v1"
SCHEMA = "summon.format-inventory/v2"
VERSION = 2
SCRIPT_DIR = "skills/summon/scripts/"
CANDIDATE_SOURCES = frozenset()
LITERAL_RE = re.compile(r"['\"](summon\.[A-Za-z0-9_.:-]+/v[0-9]+)['\"]")
AUTHORITIES = frozenset({"private_authority", "private_evidence", "public_projection",
                         "recovery_state", "compatibility_fence", "durable_history",
                         "definition_input"})


def _ref(module, symbol, language="python"):
    return {"path": SCRIPT_DIR + module + ".py", "symbol": symbol, "language": language}


def _tool_ref(module, symbol, language="python"):
    """Reference a source-owned release tool outside the runtime script tree."""
    return {"path": "tools/" + module + ".py", "symbol": symbol, "language": language}


def _test_file(path, symbol, language="python"):
    """Reference a source-owned test file outside the runtime script tree."""
    return {"path": path, "symbol": symbol, "language": language}


def _format(identifier, version, producers, readers, *, authority, fixtures=(),
            gaps=(), discriminator="schema", kind="literal", binding=None, shape=(),
            reader_contract="required"):
    return {"id": identifier, "identity": {"kind": kind, "discriminator": discriminator,
            "version": version, "binding": binding, "shape": list(shape)},
            "reader_contract": reader_contract,
            "authority": authority, "producers": list(producers), "readers": list(readers),
            "fixtures": list(fixtures), "evidence_gaps": list(gaps), "declarations": []}


def _test(module, symbol):
    return _ref("test_" + module, symbol)


_FORMATS = []


def _add(identifier, version, producer, reader, **kwargs):
    _FORMATS.append(_format(identifier, version, [producer], [reader], **kwargs))


_add("council-between-round-checkpoint", "summon.council-between-round/v1",
     _ref("_council_between_round", "write_checkpoint"),
     _ref("_council_between_round", "read_checkpoint"),
     authority="private_authority", shape=["run_id", "source_generation", "round_one_evidence_sha256"],
     fixtures=[_test("council_between_round_acceptance", "test_changed_round_one_receipt_bytes_are_not_replayed")],
     gaps=["Provider-free lifecycle coverage does not certify every historical checkpoint producer or on-disk recovery after arbitrary process loss."])
_add("council-between-round-context", "summon.council-between-round-context/v1",
     _ref("_council_between_round", "submit_context"),
     _ref("_council_between_round", "read_context"),
     authority="private_authority", shape=["run_id", "checkpoint_sha256", "operation_key", "entries"],
     fixtures=[_test("council_between_round_acceptance", "test_public_main_pause_submit_continue_journey_and_wrong_mode_refusal")],
     gaps=["Context admission is provider-free and bounded; long-prompt transport through every backend remains a separate compatibility gate."])

# E07 accounting and context-policy formats are kept explicit rather than
# inferred from their shared schema literals.  The private records retain
# per-attempt uncertainty; only the bounded summaries cross a public status
# boundary.  These references establish source ownership and fixture coverage,
# not provider usage or release qualification.
_add("submission-accounting", "summon.submission-accounting/v1",
     _ref("_submission_accounting", "private_record"),
     _ref("_swarm_coordinator", "_validate_economics_accounting"),
     authority="private_evidence", shape=["attempt", "identity", "estimate", "reported", "contact"],
     fixtures=[_test("submission_accounting", "test_retries_count_repeated_material_and_missing_usage_without_leaking_identity"),
               _test("submission_accounting", "test_public_summary_preserves_reported_metric_in_malformed_overall_record")],
     gaps=["Provider-side billing and usage remain outside Summon's visible accounting boundary."])
_add("submission-summary", "summon.submission-summary/v1",
     _ref("_submission_accounting", "public_summary"),
     _ref("_swarm_coordinator", "SwarmCoordinator.status"),
     authority="public_projection", shape=["candidate_records", "physical_attempts", "reported", "unknown_spend"],
     fixtures=[_test("submission_accounting", "test_public_summary_ignores_numeric_metrics_not_marked_reported"),
               _test("submission_accounting_public_acceptance", "test_review_private_history_projections_and_durable_preservation")])
_add("context-policy", "summon.context-policy/v1",
     _ref("_context_policy", "make"),
     _ref("_context_policy", "validate"),
     authority="private_authority", shape=["policy_id", "mode", "revision", "implementation", "origin"],
     fixtures=[_test("context_policy", "test_operator_policy_is_strict_and_has_stable_public_projection"),
               _test("context_policy", "test_legacy_v1_is_explicitly_derived_and_cannot_be_used_as_operator_policy")])
_add("turn-economics", "summon.turn-economics/v1",
     _ref("_conversation_economics", "make"),
     _ref("_conversation_economics", "public_summary"),
     authority="private_evidence", shape=["turn", "policy", "source", "submission", "handoff"],
     fixtures=[_test("e07_context_economics", "test_turn_economics_is_owner_bound_and_publicly_redacted"),
               _test("e07_context_economics", "test_turn_economics_requires_actual_prompt_boundary")])
_add("turn-economics-summary", "summon.turn-economics-summary/v1",
     _ref("_conversation_economics", "public_summary"),
     _ref("_conversation_economics", "validate_public_summary"),
     authority="public_projection", shape=["status", "submission_state", "provider_contact", "unknown_spend"],
     fixtures=[_test("e07_context_economics", "test_public_summary_does_not_accept_private_policy_or_forged_contact")])
_add("chat-launch-guard-v2", "summon.chat-launch-guard/v2",
     _ref("_chat_launch_guard", "create_v2"),
     _ref("_chat_launch_guard", "before_launch_v2"),
     authority="private_authority", shape=["session_id", "participant", "policy_sha256", "payload_sha256", "context_selection_sha256"],
     fixtures=[_test("chat_launch_guard_acceptance", "test_v2_guard_binds_turn_policy_and_payload_before_handoff"),
               _test("chat_dispatcher_guard_v2_acceptance", "test_public_dispatcher_v2_guard_uses_actual_payload_and_physical_attempt")])
_add("workspace-plan-v2", "summon.workspace.plan/v2",
     _ref("_workspace_plan", "compile_plan_v2"),
     _ref("_workspace_plan", "validate_plan"),
     authority="private_authority", shape=["goal", "tasks", "operator_message_targets", "economics"],
     fixtures=[_test("workspace_policy_v2", "test_v2_requires_explicit_policy_and_economics_fence"),
               _test("workspace_policy_v2", "test_v1_remains_unchanged_and_v2_compiles_to_v2_protocol_records")])
_add("workspace-economics", "summon.workspace.economics/v1",
     _ref("_workspace_plan", "compile_plan_v2"),
     _ref("_workspace_plan", "_validate_economics_fence"),
     authority="private_authority", shape=["feature", "enabled", "max_records", "max_settlement_bytes"],
     fixtures=[_test("workspace_policy_v2", "test_v2_requires_explicit_policy_and_economics_fence")])
_add("workspace-admission-policy", "summon.workspace.admission-policy/v1",
     _ref("_workspace_budget", "validate_policy"),
     _ref("_workspace_budget", "validate_policy"),
     authority="private_authority", shape=["rate_window_ms", "rate_limit", "stream_limits", "oversize_head_action"],
     fixtures=[_test("workspace_budget", "test_message_acceptance_is_separate_from_execution_slot_deferral")])
_add("workspace-admission-snapshot", "summon.workspace.admission-snapshot/v1",
     _ref("_workspace_budget", "validate_snapshot"),
     _ref("_workspace_budget", "validate_snapshot"),
     authority="private_evidence", shape=["now_ms", "journal_used_bytes", "pending_messages", "stream_usage"],
     fixtures=[_test("workspace_budget", "test_message_acceptance_is_separate_from_execution_slot_deferral")])
_add("workspace-admission-request", "summon.workspace.admission-request/v1",
     _ref("_workspace_budget", "validate_request"),
     _ref("_workspace_budget", "validate_request"),
     authority="private_authority", shape=["selected_message_ids", "journal_bytes", "execution_slots_requested", "kind"],
     fixtures=[_test("workspace_budget", "test_message_acceptance_is_separate_from_execution_slot_deferral")])
_add("workspace-admission-result", "summon.workspace.admission-result/v1",
     _ref("_workspace_budget", "evaluate"),
     _ref("_workspace_budget", "result_sha256"),
     authority="private_evidence", shape=["status", "reason", "selected", "deferred"],
     fixtures=[_test("workspace_budget", "test_message_acceptance_is_separate_from_execution_slot_deferral")])
_add("workspace-admission-decision", "summon.workspace.admission-decision/v1",
     _ref("_workspace_budget", "bind_decision"),
     _ref("_workspace_budget", "consume_bound_decision"),
     authority="private_authority", shape=["policy", "snapshot", "request", "facts_identity_sha256", "journal_prefix_sha256"],
     fixtures=[_test("workspace_budget_integration", "test_bound_budget_decision_matches_request_and_current_journal"),
               _test("workspace_budget_integration", "test_bound_budget_decision_refuses_stale_identity_or_revision_before_callbacks")])
_add("workspace-turn-budget-admission", "summon.workspace.turn-budget-admission/v1",
     _ref("_swarm_coordinator", "SwarmCoordinator.admit_claim_selection"),
     _ref("_swarm_coordinator", "_validate_budget_admission"),
     authority="private_authority", shape=["status", "policy", "snapshot", "request", "decision"],
     fixtures=[_test("workspace_demo", "ConductorDemoTests.test_v2_economics_capacity_bound_covers_admitted_shapes_and_replay")])
_add("workspace-turn-economics-settlement", "summon.workspace.turn-economics-settlement/v1",
     _ref("_swarm_coordinator", "SwarmCoordinator.settle_workspace_turn_economics"),
     _ref("_swarm_coordinator", "SwarmCoordinator.settle_workspace_turn_economics"),
     authority="private_evidence", shape=["admission_id", "accounting", "submission_state", "result_status"],
     fixtures=[_test("workspace_demo", "ConductorDemoTests.test_v2_economics_path_derives_policy_bound_reservation_and_settles"),
               _test("workspace_demo", "ConductorDemoTests.test_v2_economics_rejects_contradictory_metric_field_state")])


_add("job-continuation-source", "summon.job-continuation-source/v1",
     _ref("_job_continuation", "write_private_source"), _ref("_job_continuation", "read_private_source"),
     authority="private_authority", fixtures=[_test("job_continuation", "test_private_source_round_trip_and_public_projection_redacts_capabilities")],
     gaps=["Historical v1 source shape is reader-only; the current producer emits the separately authenticated v2 profile-binding shape."])
next(item for item in _FORMATS if item["id"] == "job-continuation-source")["producers"] = []
_add("job-continuation-source-v2", "summon.job-continuation-source/v2",
     _ref("_job_continuation", "write_private_source"), _ref("_job_continuation", "read_private_source"),
     authority="private_authority", fixtures=[
         _test("job_continuation", "test_private_source_round_trip_and_public_projection_redacts_capabilities"),
         {"path": "tests/test_profile_resume_rotation_acceptance.py",
          "symbol": "ProfileResumeAcceptance.test_profile_rotation_before_child_load_refuses_without_baselining",
          "language": "python"},
         {"path": "tests/test_profile_resume_rotation_acceptance.py",
          "symbol": "ProfileResumeAcceptance.test_v2_profile_source_is_claim_bound_and_public_projection_redacts_it",
          "language": "python"}])
_add("job-launch-binding", "summon.job-launch-binding/v1",
     _ref("_job_continuation", "write_private_source"), _ref("_job_continuation", "read_launch_binding"),
     authority="private_evidence", fixtures=[_test("resume_revalidation_acceptance", "test_legacy_revalidation_refuses_before_any_sidecar_change")],
     gaps=["Historical source without this sidecar is readable; this does not qualify a launch."])
_add("job-continuation-public", "summon.job-continuation/v1",
     _ref("_job_continuation", "public_projection"), _ref("_jobs", "public_job_status"),
     authority="public_projection", fixtures=[_test("job_continuation", "test_private_source_round_trip_and_public_projection_redacts_capabilities")])
for identifier, producer, reader, shape, authority in (
    ("launch-observation-fresh", "observation", "valid_observation", ["observation_nonce", "observed_at_ns"], "private_evidence"),
    ("launch-observation-persisted", "binding_projection", "valid_projection", ["executable_sha256", "launch_material_sha256"], "private_evidence"),
):
    _add(identifier, "summon.resume-launch-observation/v1", _ref("_launch_binding", producer),
         _ref("_launch_binding", reader), authority=authority, shape=shape,
         fixtures=[_test("launch_binding", "test_projection_validator_is_distinct_from_fresh_observation")],
         gaps=["Shared schema literal has distinct fresh and persisted shapes; observation is not qualification."])
_add("launch-qualification", "summon.launch-qualification/v1", _ref("_launch_qualification", "issue"),
     _ref("_launch_qualification", "read"), authority="private_authority",
     fixtures=[_test("launch_qualification", "test_authenticated_qualification_matches_only_exact_observation"),
               _test("launch_qualification", "test_qualification_rejects_expiry_revocation_and_placeholder_version")])
_add("launch-qualification-revocation", "summon.launch-qualification-revocation/v1",
     _ref("_launch_qualification", "revoke"), _ref("_launch_qualification", "is_revoked"),
     authority="private_authority", fixtures=[_test("launch_qualification", "test_trusted_revocation_is_separate_from_signed_qualification")])
for identifier, version, producer, reader, shape, gaps in (
    ("job-resume-ledger-current", "summon.job-resume-ledger/v1", "reserve_request", "_validate_ledger", ["claims", "generation", "auth"], []),
    ("job-resume-ledger-legacy-claim", "summon.job-resume-ledger/v1", "reserve_request", "_validate_ledger", ["_LEGACY_CLAIM_KEYS"], ["Historical embedded claim shape is an explicit reader branch; producer equivalence remains unqualified."]),
    ("job-resume-claim-current", "summon.job-resume-claim/v1", "prepare_successor", "load_child_context", ["launch_binding", "launch_qualification"], []),
    ("job-resume-claim-legacy", "summon.job-resume-claim/v1", "prepare_successor", "load_child_context", ["legacy_claim: lacks launch_binding and launch_qualification"], ["Historical field-set branch remains reader-only; current writer does not produce that legacy shape."]),
):
    _add(identifier, version, _ref("_job_resume", producer), _ref("_job_resume", reader),
         authority="private_authority", shape=shape, gaps=gaps,
         fixtures=[_test("historical_job_claim_acceptance", "test_exact_claim_readers_preserve_bytes_and_public_projection"),
                   _test("historical_job_claim_acceptance", "test_historical_ledger_refuses_authenticated_malformed_shape"),
                   _test("historical_job_claim_acceptance", "test_historical_child_refuses_authenticated_malformed_shape")]
         + ([] if gaps else [_test("job_resume", "test_successor_claim_and_private_prompt_round_trip"),
                             _test("job_resume", "test_ledger_and_source_tampering_are_rejected")]))
_add("resume-prompt-contract", "summon.resume-prompt/v1", _ref("_job_resume", "_semantic_request"),
     _ref("_job_resume", "load_child_context"), authority="private_authority", discriminator="prompt_contract",
     fixtures=[_test("job_resume", "test_prompt_renderer_change_cannot_reuse_old_request")])
for identifier, version, producer in (
    ("job-resume-public", "summon.job-resume/v1", "public_projection"),
    ("resume-revalidation-public", "summon.resume-revalidation/v1", "revalidate_source"),
):
    _add(identifier, version, _ref("_job_resume", producer), _ref("_background", "run_jobs_query"),
         authority="public_projection", gaps=["Public output consumer compatibility is not certified by inventory references."],
         fixtures=[_test("job_resume", "test_successor_claim_and_private_prompt_round_trip")] if identifier == "job-resume-public" else
                  [_test("resume_revalidation_acceptance", "test_supported_legacy_revalidation_enables_only_matching_later_validator")])
for kind, producer, reader in (("control", "queue_command", "_valid_command_log"),
                               ("heartbeat", "RuntimeControl.publish", "public_heartbeat")):
    for version in (1, 2):
        _add(f"job-{kind}-v{version}", f"summon.job-{kind}/v{version}",
             _ref("_job_control", producer), _ref("_job_control", reader),
             authority="private_authority" if kind == "control" else "private_evidence",
             fixtures=[_test("job_control", "test_control_summary_labels_valid_legacy_commands_unverified" if kind == "control" and version == 1 else
                             "test_queue_refuses_to_rewrite_live_legacy_log" if kind == "control" else
                             "test_jobs_status_accepts_legacy_inflight_nonce_but_never_exposes_it" if version == 1 else
                             "test_jobs_status_rejects_v2_heartbeat_with_legacy_nonce_bypass")],
             gaps=["Historical v1 is retained for restricted reader compatibility, not upgraded; no current writer for that exact shape is claimed."] if version == 1 else [])
for family, producer, reader, authority, shape in (
    ("job-launch", _ref("_jobs", "write_prepared"), _ref("_jobs", "_classify"), "private_authority", ["job_id", "nonce", "summon"]),
    ("job-terminal", _ref("run_subagent", "_emit"), _ref("_jobs", "_classify"), "public_projection", ["job_nonce", "status", "summon"]),
):
    for variant in ("legacy-nonce-only", "bundle-bound"):
        _add(f"{family}-{variant}", None, producer, reader, authority=authority,
             kind="unversioned", discriminator=None, shape=shape + (["summon.scripts_sha256 absent; legacy nonce-only classification"] if variant == "legacy-nonce-only" else ["summon.scripts_sha256 present; optional background_bundle binds prompt/context"]),
             fixtures=[_test("background_read_roots", "test_background_result_with_different_scripts_digest_is_untrusted")],
             gaps=["Structural variant is manually classified; product version is not a record schema. Referenced fixture covers bundle mismatch, not every legacy shape."])
_add("workspace-layout", "summon.workspace-layout/v1", _ref("_workspace_layout", "create_workspace_layout"),
     _ref("_workspace_layout", "resolve_workspace_layout"), authority="compatibility_fence", discriminator="format",
     fixtures=[_test("workspace_layout", "WorkspaceLayoutTests.test_unknown_format_and_changed_binding_refuse_without_mutation"), _test("workspace_layout", "WorkspaceLayoutTests.test_old_normal_mutation_refuses_before_acquire_or_repair_then_new_inner_works")])
_add("workspace-view", "summon.workspace.view/v1", _ref("_workspace_view", "project_workspace"),
     _ref("_workspace_page", "render", "embedded_javascript"), authority="public_projection",
     fixtures=[_test("workspace_view", "test_stable_bounded_timeline_pagination_and_stale_cursor_refusal"),
               _test("workspace_view_schema_acceptance", "test_view_schema_is_checked_before_confirmation_or_recovery_mutation"),
               _test("workspace_page", "test_actual_script_owner_snapshots_reconnect_and_never_evaluate_or_launch")],
     gaps=["Full rendered-browser and every valid v1 field shape are not certified by the provider-inert Node acceptance fixture."])
next(item for item in _FORMATS if item["id"] == "workspace-view")["readers"].insert(
    0, _ref("_workspace_page", "request", "embedded_javascript"))
for identifier, function, shape in (("workspace-cursor", "project_workspace", ["snapshot", "offset", "opaque HMAC token"]),
                                    ("workspace-anchor", "project_workspace", ["event identity", "scope", "opaque HMAC token"])):
    _add(identifier, None, _ref("_workspace_view", function), _ref("_workspace_view", function),
         authority="public_projection", kind="unversioned", discriminator=None, shape=shape,
         fixtures=[_test("workspace_view", "test_anchor_scope_and_history_refusal") if identifier.endswith("anchor") else
                   _test("workspace_view", "test_stable_bounded_timeline_pagination_and_stale_cursor_refusal")])
_add("workspace-pending-command-slot", "summon.workspace.pending-context/v1",
     _ref("_workspace_page", "savePending", "embedded_javascript"), _ref("_workspace_page", "restorePending", "embedded_javascript"),
     authority="recovery_state", discriminator="sessionStorage key",
     fixtures=[_test("workspace_page", "test_actual_script_serializes_intents_without_background_focus_loss_and_anchors_refresh")])
_add("workspace-pending-command-payload", None,
     _ref("_workspace_page", "savePending", "embedded_javascript"), _ref("_workspace_page", "validPending", "embedded_javascript"),
     authority="recovery_state", kind="unversioned", discriminator=None, shape=["workspace", "body"],
     fixtures=[_test("workspace_page", "test_actual_script_serializes_intents_without_background_focus_loss_and_anchors_refresh")])
_add("workspace-pending-message-slot", "summon.workspace.pending-message/v1",
     _ref("_workspace_page", "writeMessageRecord", "embedded_javascript"),
     _ref("_workspace_page", "readMessageRecord", "embedded_javascript"),
     authority="recovery_state", kind="literal", discriminator="sessionStorage key",
     fixtures=[_test("workspace_page", "test_message_composer_crypto_recovery_and_delivery_states")])
_add("workspace-pending-message-payload", 1,
     _ref("_workspace_page", "sealMessageRecord", "embedded_javascript"),
     _ref("_workspace_page", "validMessageRecord", "embedded_javascript"),
     authority="recovery_state", kind="numeric", discriminator="v", binding=None,
     shape=["ciphertext", "iv", "operation_key", "key_epoch"],
     fixtures=[_test("workspace_page", "test_message_composer_crypto_recovery_and_delivery_states"),
               _test("workspace_encrypted_version_acceptance", "test_numeric_v1_producer_record_recovers_same_operation_without_submission"),
               _test("workspace_encrypted_version_acceptance", "test_unsupported_numeric_record_version_retains_ciphertext_and_blocks_replacement")],
     gaps=["Synthetic storage and key material only; real browser storage/quota behavior and historical producers are not certified."])
_add("workspace-message-aad", 1,
     _ref("_workspace_page", "messageAad", "embedded_javascript"),
     _ref("_workspace_page", "restoreEncryptedMessage", "embedded_javascript"),
     authority="recovery_state", kind="numeric", discriminator="version", binding=None,
     shape=["workspace", "operator_scope", "operation_key", "state", "key_epoch"],
     fixtures=[_test("workspace_page", "test_message_composer_crypto_recovery_and_delivery_states"),
               _test("workspace_encrypted_version_acceptance", "test_actual_aes_gcm_aad_version_requires_exact_compatible_bytes")],
     gaps=["Synthetic browser storage and host-key inputs only; real host-key derivation and historical producers are not certified."])
for version, producer in ((1, "_host_config_value"), (2, "_plan_host_config_value")):
    _add(f"workspace-host-v{version}", f"summon.workspace-host/v{version}", _ref("_workspace_entry", producer),
         _ref("_workspace_entry", "_decode_host_config"), authority="private_authority",
         fixtures=[_test("workspace_entry", "test_host_config_rejects_cross_version_field_sets"),
                   _test("workspace_entry", "test_legacy_host_reopen_preserves_scope_identity_without_schema_upgrade")])
for suffix, producer, reader in (
    ("plan-authority", "compile_plan", "_write_plan_evidence"), ("plan-grants", "compile_plan", "_grant_resolver"),
    ("plan-scopes", "compile_plan", "_scope_from_workspace"),
    ("plan-project-binding", "binding_descriptors", "_verify_plan_host_files"),
    ("plan-recipient-binding", "binding_descriptors", "_verify_plan_host_files"),
    ("task-authority", "compile_plan", "_write_plan_evidence"),
):
    _add("workspace-" + suffix, "summon.workspace." + suffix + "/v1", _ref("_workspace_plan", producer),
         _ref("_workspace_entry", reader), authority="private_authority",
         fixtures=[_test("workspace_plan", "test_compile_generates_inert_authority_and_message_grants")],
         gaps=["Fixture covers compilation; individual embedded-format stale-reader refusal is not established by this reference."])
for identifier, producer, reader, shape in (
    ("conversation-room", "ConversationJournal.create", "ConversationJournal._validate_header", ["record=conversation_room"]),
    ("conversation-event", "ConversationJournal.append", "ConversationJournal._validate_records", ["record=conversation_event"]),
):
    _add(identifier, 2, _ref("_conversation", producer), _ref("_conversation", reader),
         authority="durable_history", kind="numeric", discriminator="schema_version", binding="SCHEMA_VERSION", shape=shape,
         fixtures=[_test("conversation", "ConversationJournalTests.test_legacy_room_id_is_readable_without_relaunch_or_duplicate_room")],
         gaps=["Reopen identity fixture does not establish every numeric-version refusal."])
# This optional payload is part of numeric conversation v2, not a new schema.
_FORMATS.append(_format("conversation-event-participant-replacement", 2,
     [_ref("_conversation_runtime", "ConversationRuntime.fork_turn"),
      _ref("_conversation_runtime", "ConversationRuntime._fork"),
      _ref("_conversation", "ConversationJournal.append")],
     [_ref("_conversation", "ConversationJournal._validate_records"),
      _ref("_conversation", "_public_payload"),
      _ref("_conversation_runtime", "ConversationRuntime._replacement_bindings"),
      _ref("_conversation_runtime", "ConversationRuntime._identity_from_config"),
      _ref("_conversation_runtime", "ConversationRuntime.fork_turn")],
     authority="durable_history", kind="numeric", discriminator="schema_version",
     binding="SCHEMA_VERSION",
     shape=["record=conversation_event", "event=fork_created",
            "payload.participant_replacement (optional)",
            "participant_replacement.from_participant", "participant_replacement.to_participant",
            "participant_replacement.agent_definition_sha256", "participant_replacement.permission_ceiling",
            "parent_session_id", "child_session_id", "parent_schema_version", "parent_history_sha256", "lineage"],
     fixtures=[_test("e12_turn_fork_acceptance", "test_replacement_uses_recorded_ceiling_without_original_catalog"),
               _test("e12_turn_fork_acceptance", "test_unknown_history_never_falls_back_to_current_catalog"),
               _test("e12_turn_fork_acceptance", "test_replacement_cannot_widen_recorded_authority"),
               _test("e12_turn_fork_acceptance", "test_same_name_descendant_preserves_frozen_replacement_definition"),
               _test("e12_turn_fork_acceptance", "test_second_participant_replacement_carries_first_binding_with_one_lineage"),
               _test("e12_turn_fork_acceptance", "test_ambiguous_replacement_records_refuse_without_launch"),
               _test("e12_turn_fork_acceptance", "test_replacement_projection_rejects_invalid_permission_types"),
               _test("e12_turn_fork_acceptance", "test_real_turn_refusal_then_explicit_nonlaunching_fork_retains_history")],
     gaps=["fork_turn constructs the optional binding; _fork persists one fork_created event per retained participant binding with identical parent lineage. Same-name descendants preserve all bindings; the parent journal remains unchanged and the selected new human prompt is recorded without launching a child worker.",
           "The historical permission ceiling constrains later identity construction; missing or malformed history refuses. Missing, removed or drifted original catalog entries are never an authority fallback; the replacement definition digest remains frozen.",
           "Journal validation and _public_payload validate/project the structure; _replacement_bindings additionally rejects duplicate participants and ambiguous parent lineage. A displayed binding or parent-history digest is not launch authorization or an authenticated provider identity.",
           "These are current v2 provider-free fixture references, not executed by inventory validation. Compatibility with older binaries that predate this optional payload, arbitrary historical journals and live provider continuation remains unqualified."]))
# Historical numeric identities are reader-only; current writers emit v2.
for identifier, reader, shape in (
    ("conversation-room-v1", "ConversationJournal._validate_header", ["record=conversation_room"]),
    ("conversation-event-v1", "ConversationJournal._validate_records", ["record=conversation_event"]),
):
    _FORMATS.append(_format(identifier, 1, [], [_ref("_conversation", reader)],
         authority="durable_history", kind="numeric", discriminator="schema_version",
         binding="HISTORICAL_SCHEMA_VERSION", shape=shape,
         fixtures=[_test("conversation_numeric_version_acceptance", "test_consistent_historical_v1_open_list_and_mutation_refusals_preserve_all_bytes"),
                   _test("conversation_numeric_version_acceptance", "test_explicit_historical_v1_fork_creates_linked_v2_without_parent_mutation_or_contact")],
         gaps=["Historical v1 is reader-only in the current runtime; unchanged v1 bytes remain subject to older binaries' behavior."]))
_add("fleet-approval-store", "summon.fleet-approval-store/v1",
     _ref("_fleet_approval", "_new_store"), _ref("_fleet_approval", "_validate_store"),
     authority="private_authority", shape=["store_id", "generation", "approvals", "revocations", "mac"],
     fixtures=[_test("phase1_fleet_approval", "test_store_and_inner_record_forgery_fail_closed"),
               _test("phase1_fleet_approval", "test_recorded_approval_is_authenticated_redacted_and_non_authoritative")],
     gaps=["Synthetic authenticated-store fixtures do not certify historical migration, host ACL enforcement or live activation."])
_add("fleet-approval", "summon.fleet-approval/v1",
     _ref("_fleet_approval", "approve"), _ref("_fleet_approval", "_validate_approval"),
     authority="private_authority", shape=["approval_id", "store_id", "bindings", "authority", "expires_at", "mac"],
     fixtures=[_test("phase1_fleet_approval", "test_store_and_inner_record_forgery_fail_closed"),
               _test("phase1_fleet_approval", "test_recorded_approval_is_authenticated_redacted_and_non_authoritative")],
     gaps=["Recorded approval is not activation; metadata and synthetic fixtures do not establish live dispatch permission."])
_add("fleet-activation-candidate", "summon.fleet-activation/v1",
     _ref("_fleet_activation", "freeze_activation_candidate"),
     _ref("_fleet_activation", "validate_activation_contract"),
     authority="private_evidence", shape=["authorization=evidence_only", "activation_allowed=false", "bindings", "attempt_policy", "sha256"],
     fixtures=[_test("phase1_fleet_activation", "test_contract_is_unbound_non_authority_and_private_values_are_only_digests"),
               _test("phase1_fleet_activation", "test_prompt_binding_and_contract_forgery_fail_closed")],
     gaps=["Unbound activation candidate is evidence only, never launch authority or live-provider qualification."])

for variant in ("legacy-unbound", "project-roster-bound"):
    _add("conversation-ui-" + variant, 1, _ref("_conversation_ui", "_write_surface_record"),
         _ref("_conversation_ui", "read_surface_record"), authority="private_authority", kind="numeric",
         discriminator="schema_version", shape=[variant],
         fixtures=[_test("conversation_ui", "ConversationUITests.test_legacy_surface_record_is_not_reused_for_bound_project")],
         gaps=["Legacy shape is reader compatibility only; current writer reference identifies its family."] if variant.startswith("legacy") else [])
for variant in ("legacy", "owner-bound"):
     _add("conversation-process-" + variant, 2, _ref("_conversation_runtime", "_write_process_record"),
         _ref("_conversation_runtime", "_read_process_record"), authority="private_evidence", kind="numeric",
         discriminator="schema_version", binding="PROCESS_RECORD_SCHEMA_VERSION", shape=[variant],
         fixtures=[_test("conversation_runtime", "ConversationRuntimeTests.test_legacy_process_record_lookup_is_read_only_and_does_not_duplicate_scope")],
         gaps=["Legacy lookup fixture does not establish every process-record schema/ownership refusal."])
_FORMATS.append(_format(
    "conversation-process-v1-reader", 1, [],
    [_ref("_conversation_runtime", "_read_process_record")],
    authority="private_evidence", kind="numeric", discriminator="schema_version",
    binding={"kind": "set_member", "symbol": "SUPPORTED_PROCESS_RECORD_SCHEMA_VERSIONS"},
    shape=["historical process-record/v1"],
    fixtures=[_test("conversation_numeric_version_acceptance",
                    "test_supported_process_version_allows_explicit_dead_fixture_recovery_with_unknown_effects"),
              _test("conversation_numeric_version_acceptance",
                    "test_actual_recovery_refuses_unsupported_process_version_before_probe_or_mutation"),
              _test("conversation_numeric_version_acceptance",
                    "test_actual_cancel_checks_process_schema_before_any_durable_mutation")],
    gaps=["Historical v1 reader only; the current producer emits v2.",
          "Synthetic recovery fixtures substitute process identity probes; metadata validation does not execute fixtures or qualify real OS recovery/cancellation."]))
for identifier, version, producer, reader, fixture in (
    ("chat-source-family", "summon.chat-source-family/v1", _ref("_chat_source_family", "ensure"), _ref("_chat_source_family", "read"), _test("chat_source_family_acceptance", "test_stale_qualification_writer_cannot_erase_revocation")),
    ("chat-migration-authority", "summon.chat-source-family-migration-authority/v1", _ref("_chat_source_family", "issue_migration_authority"), _ref("_chat_source_family", "validate_migration_authority"), None),
    ("chat-launch-qualification", "summon.chat-launch-qualification/v1", _ref("_chat_launch_qualification", "issue"), _ref("_chat_launch_qualification", "valid"), _test("chat_source_family_runtime_acceptance", "test_invalid_source_qualification_refuses_without_provider")),
    ("chat-launch-guard", "summon.chat-launch-guard/v1", _ref("_chat_launch_guard", "create"), _ref("_chat_launch_guard", "before_launch"), _test("chat_launch_guard", "ChatLaunchGuardTests.test_guard_is_authenticated_single_use_and_binds_owner")),
    ("chat-revalidation-packet", "summon.chat-revalidation-packet/v1", _ref("_conversation_runtime", "ConversationRuntime.revalidate_chat_packet"), _ref("_conversation_runtime", "ConversationRuntime.revalidate_chat_packet"), _test("chat_revalidation_input_acceptance", "test_public_chat_packet_input_refuses_without_private_output")),
    ("chat-revalidation-public", "summon.chat-revalidation/v1", _ref("_conversation_runtime", "ConversationRuntime.revalidate_chat_source"), _ref("_conversation_runtime", "ConversationRuntime.revalidate_chat_packet"), None),
    ("chat-resume-refusal", "summon.chat-resume-refusal/v1", _ref("_chat_resume", "build"), _ref("_chat_resume", "is_valid"), _test("chat_resume", "ChatResumeContractTests.test_historical_v1_refusal_is_readable_but_not_current_authority")),
):
    _add(identifier, version, producer, reader, authority="public_projection" if identifier.endswith(("public", "refusal")) else "private_authority",
         fixtures=[fixture] if fixture else [],
         gaps=[] if fixture else ["No format-specific behavioral fixture was inspected; references establish source existence only."])

# Usage evidence is advisory; authenticated cache bytes do not grant consent
# to query an account, spend, reset credits or change routing.
_FORMATS.append(_format("usage-snapshot", "summon.usage/v1",
    [_ref("_usage", "export_snapshot"), _ref("_usage", "synthetic_snapshot"),
     _ref("_usage", "import_snapshot")], [_ref("_usage", "_validate_snapshot")],
    authority="private_evidence", shape=["observations", "optional cached_at on local cache"],
    fixtures=[_test("phase1_usage", "test_usage_import_rejects_false_provider_source_and_preserves_old_cache"),
              _test("phase1_usage", "test_usage_import_accepts_small_clock_skew_but_rejects_far_future")],
    gaps=["Unsigned operator exports and their cache are not provider-attested evidence; import/export/status result envelopes sharing this literal are not exhaustively classified.",
          "Clock-skew fixtures do not establish recovery across arbitrary historical producers or clock rollback."]))
for identifier, schema, producer, reader, fixture, shape in (
    ("usage-live-private", "summon.usage-live-private/v1", "_write_store", "_validate_store",
     "test_store_and_anchor_tamper_or_rollback_mismatch_fail_closed", ["store_id", "generation", "last_seen_at", "observations", "mac"]),
    ("usage-live-anchor", "summon.usage-live-anchor/v1", "_write_store", "_validate_anchor",
     "test_store_and_anchor_tamper_or_rollback_mismatch_fail_closed", ["store_id", "generation", "store_mac", "mac"]),
    ("usage-live-checkpoint", "summon.usage-live-checkpoint/v1", "_write_checkpoint", "_validate_checkpoint",
     "test_joint_key_store_anchor_delete_or_replay_cannot_reset_initialized_state", ["store_path_sha256", "store_id", "generation", "store_mac", "mac"]),
    ("usage-live-key-binding", "summon.usage-live-key/v1", "_bind_key_to_store", "_key_binding",
     "test_key_checkpoint_is_authenticated", ["key", "store_binding", "store_binding_mac"]),
):
    _add(identifier, schema, _ref("_usage_live", producer), _ref("_usage_live", reader),
         authority="private_evidence", shape=shape,
         fixtures=[{"path": "skills/summon/tests/test_phase1_usage_live.py",
                    "symbol": fixture, "language": "python"}],
         gaps=["Authentication and local anti-replay evidence are not account-read or spend authorization; injected-runner fixtures do not certify live provider provenance.",
               "The composed reader checks clock, anchor, key binding and independent checkpoint; these references do not certify every historical or torn-write recovery path. Deletion of every private root by the local account owner requires an external trust anchor to resist."])
next(item for item in _FORMATS if item["id"] == "usage-live-private")["producers"].append(
    _ref("_usage_live", "refresh_codex"))
# The key schema covers both initial unbound material and its later store binding.
key_format = next(item for item in _FORMATS if item["id"] == "usage-live-key-binding")
key_format["producers"].append(_ref("_usage_live", "_load_key"))
key_format["readers"].append(_ref("_usage_live", "_load_key"))
key_format["evidence_gaps"].append("The initial key record has no store binding; this family reference does not claim historical unbound-key migration coverage.")
_add("usage-live-runner-plan", "summon.usage-live-runner-plan/v1",
     _ref("_usage_live", "codex_request_plan"), _ref("_usage_runner", "_validate_plan"),
     authority="definition_input", shape=["argv", "requests", "required_cli_version", "max_attempts", "dry_run"],
     fixtures=[{"path": "skills/summon/tests/test_phase1_usage_live.py",
                "symbol": "test_exact_codex_plan_is_bounded_one_attempt_and_disables_refresh_token", "language": "python"},
               {"path": "skills/summon/tests/test_phase1_usage_live.py",
                "symbol": "test_consent_and_dry_run_never_invoke_runner", "language": "python"}],
     gaps=["A version-pinned request definition is not proof of execution or authorization to query/spend; consent and the bounded runner remain separate gates. Fixture support does not certify installed CLI compatibility."])
_add("usage-live-public-status", "summon.usage-live-public/v1",
     _ref("_usage_live", "status"), _ref("_usage", "export_snapshot"),
     authority="public_projection", shape=["status=success", "observations", "capabilities"],
     fixtures=[_test("phase1_usage", "test_usage_example_and_export_are_deterministic_redacted_and_no_overwrite")],
     gaps=["The export consumer checks status and observations, not the schema discriminator; it de-attests portable observations to operator_export. Refresh, preflight and failure projections sharing this literal are not exhaustively classified.",
           "Freshness is local-command observation age; tri-state provider contact, account-read consent and spend authorization must not be inferred from this advisory projection."])
# The semantic request emits the contract; reservation persists it in a claim.
next(item for item in _FORMATS if item["id"] == "resume-prompt-contract")["producers"].append(
    _ref("_job_resume", "reserve_request"))

# Imported schema constants are declaration references, not invented writers.
next(item for item in _FORMATS if item["id"] == "workspace-host-v2")["declarations"] = [
    _ref("_workspace_plan", "PLAN_HOST_SCHEMA")]
packet = next(item for item in _FORMATS if item["id"] == "chat-revalidation-packet")
packet["authority"] = "definition_input"
packet["evidence_gaps"].append("No production packet writer was inspected: producer reference names the externally supplied input boundary.")
# A reader branch does not establish a historical writer. Empty producer lists
# are allowed only with an explicit gap and remain visible in gap_report().
for item in _FORMATS:
    if item["id"] in {"job-resume-ledger-legacy-claim", "job-resume-claim-legacy",
                      "job-control-v1", "job-heartbeat-v1", "chat-revalidation-packet"}:
        item["producers"] = []
        item["evidence_gaps"].append("No producer definition for this exact historical or external-input variant was established in the audited source.")
        # Schema declarations are source references, never substitutes for writers.
        if item["id"] == "chat-revalidation-packet":
            item["evidence_gaps"] = [gap for gap in item["evidence_gaps"] if "producer reference names" not in gap]
next(item for item in _FORMATS if item["id"] == "chat-revalidation-public")["readers"] = [_ref("_conversation", "run_command")]
# Telemetry's preference, local spool and shareable evidence have different
# trust/shape contracts even where numeric versions coincide.
_FORMATS.append(_format("telemetry-config", 1,
    [_ref("_telemetry", "set_enabled"), _ref("_telemetry", "_correlation_salt")],
    [_ref("_telemetry", "_read_config")], authority="private_authority",
    kind="numeric", binding="CONFIG_SCHEMA_VERSION", shape=["enabled", "salt"],
    fixtures=[{"path": "tests/test_telemetry_gate.py", "language": "python",
               "symbol": "TelemetryGateTests.test_config_schema_stays_one_and_public_view_strips_local_bindings"}],
    gaps=["_read_config accepts any bounded JSON object and ignores schema; this version describes writers, not strict reader admission."]))
_FORMATS.append(_format("telemetry-event-legacy", 1, [],
    [_ref("_telemetry", "_parse_event_lines"), _ref("_telemetry", "_sanitize_event")],
    authority="private_evidence", kind="numeric", binding="LEGACY_EVENT_SCHEMA_VERSION",
    shape=["event_id", "recorded_at", "status", "backend"],
    fixtures=[{"path": "tests/test_telemetry_gate.py", "language": "python",
               "symbol": "TelemetryGateTests.test_schema_one_event_is_imported_as_legacy_unknown"},
              {"path": "tests/test_telemetry_audit.py", "language": "python",
               "symbol": "TelemetryAuditTests.test_legacy_and_malformed_records_never_enter_denominator"}],
    gaps=["No historical v1 spool producer is identified in current source; fixture literals are not production writers.",
          "_parse_event_lines uses numeric equality, admitting bool/float aliases of supported integers without event-shape validation.",
          "_sanitize_event reprojects the public allowlist with schema 1 and legacy_unknown; this transformed public variant is not a native v2 event or a separately inventoried historical producer."]))
_FORMATS.append(_format("telemetry-event-spool", 2,
    [_ref("_telemetry", "event_from_envelope"), _ref("_telemetry", "_spool_projection"),
     _ref("_telemetry", "record")],
    [_ref("_telemetry", "_parse_event_lines"), _ref("_telemetry", "_sanitize_event")],
    authority="private_evidence", kind="numeric", binding="EVENT_SCHEMA_VERSION",
    shape=["event_kind", "terminal_kind", "operation_id", "event_sequence", "cohort"],
    fixtures=[{"path": "tests/test_telemetry_gate.py", "language": "python",
               "symbol": "TelemetryGateTests.test_schema_two_terminal_has_required_nulls_and_bounded_projection"},
              _test("discovery", "test_telemetry_spool_is_bounded")],
    gaps=["_spool_projection drops optional nulls but retains required nulls; the in-memory projection is not byte-identical to persisted JSONL.",
          "_parse_event_lines uses numeric equality, admitting bool/float aliases of supported integers without event-shape validation; public sanitization applies additional shape checks."]))
_FORMATS.append(_format("telemetry-event-public", 2,
    [_ref("_telemetry", "_sanitize_event"), _ref("_telemetry", "make_report")],
    [_ref("_telemetry", "_validate_public_evidence")],
    authority="public_projection", kind="numeric", binding="EVENT_SCHEMA_VERSION",
    shape=["source_trust", "event_kind", "operation_id=null", "event_sequence=null", "workspace=null"],
    fixtures=[{"path": "tests/test_telemetry_gate.py", "language": "python",
               "symbol": "TelemetryGateTests.test_config_schema_stays_one_and_public_view_strips_local_bindings"},
              {"path": "tests/test_telemetry_gate.py", "language": "python",
               "symbol": "TelemetryGateTests.test_reviewed_report_validator_requires_generated_shape_and_bounds"}],
    gaps=["_sanitize_event reprojects the public allowlist and removes local bindings; spool version equality alone does not establish public evidence validity.",
          "Reviewed-report validation is a separate stricter reader; these source references do not execute report submission or certify historical migration completeness."]))

# L02 durable families: nested authority/evidence remains part of its parent
# store; source references do not certify historical writer compatibility.
_add("background-launch-v2", "summon.background-launch/v2",
     _ref("_jobs", "write_prepared"), _ref("_jobs", "_accounting_owner"),
     authority="private_authority", shape=["job_id", "attempt_id", "accounting_handoff", "submission_accounting"],
     fixtures=[_test("submission_accounting", "test_background_owner_fence_interruption_and_no_lost_pid_update"),
               _test("submission_accounting", "test_accounting_migration_refuses_future_or_wrong_schema_without_change"),
               _test("submission_accounting", "test_supported_legacy_background_record_remains_readable")],
     gaps=["Legacy readability fixtures construct historical input; no old-writer execution or arbitrary process-loss compatibility is certified."])
_add("submission-accounting-handoff", "summon.submission-accounting-handoff/v1",
     _ref("_jobs", "write_prepared"), _ref("_jobs", "_accounting_owner"),
     authority="private_authority", shape=["owner", "root_attempt_id", "fence_sha256", "generation", "grants"],
     fixtures=[_test("submission_accounting", "test_background_accounting_grant_accepts_one_authorized_retry_only"),
               _test("submission_accounting", "test_aggregate_accounting_grants_refuse_before_first_contact")],
     gaps=["Nested accounting_handoff in background-launch/v2, not an independent store; no historical handoff writer or cross-version compatibility fixture is established."])
_add("workspace-protocol-v1", "summon.workspace/v1",
     _ref("_workspace_plan", "compile_plan"), _ref("_workspace_protocol", "validate_record"),
     authority="durable_history", discriminator="protocol", shape=["kind", "workspace_id", "run_id"],
     fixtures=[_test("workspace_plan", "test_compile_generates_inert_authority_and_message_grants"),
               _test("workspace_state", "test_feature_marker_required_and_old_pure_reducer_refuses_without_mutation")],
     gaps=["Compiler reference establishes goal/lane record production, not every workspace event writer; synthetic old-reducer refusal does not certify an executed historical writer."])
_add("workspace-protocol-v2", "summon.workspace/v2",
     _ref("_workspace_plan", "compile_plan_v2"), _ref("_workspace_protocol", "validate_record"),
     authority="durable_history", discriminator="protocol", shape=["kind", "workspace_id", "run_id"],
     fixtures=[_test("workspace_policy_v2", "test_v1_remains_unchanged_and_v2_compiles_to_v2_protocol_records"),
               _test("workspace_demo", "ConductorDemoTests.test_v2_economics_covers_full_turn_settlement_reopen_and_indeterminate_hold")],
     gaps=["V2 goal/lane conversion and current reopen coverage do not certify all record kinds or historical-writer/new-reader compatibility."])
_add("workspace-economics-reservation", "summon.workspace.economics-reservation/v1",
     _ref("_workspace_runtime", "WorkspaceRuntime._v2_economics_reservation"),
     _ref("_workspace_admission", "economics_reservation"),
     authority="private_authority", shape=["claim_id", "selection_sha256", "attempt_id", "payload_sha256", "material_sha256", "policy_sha256"],
     fixtures=[_test("workspace_demo", "ConductorDemoTests.test_v2_economics_path_derives_policy_bound_reservation_and_settles"),
               _test("workspace_demo", "ConductorDemoTests.test_v2_economics_covers_full_turn_settlement_reopen_and_indeterminate_hold")],
     gaps=["Nested turn-admission payload reservation, not an independent store; no historical reservation writer or cross-version acceptance fixture is established."])
_add("workspace-effects-observation", "summon.workspace.effects-observation/v1",
     _ref("_workspace_runtime", "fixed_effect_sources"),
     _ref("_workspace_protocol", "validate_effect_observations"),
     authority="private_evidence", shape=["kind", "resolution_kind", "qualification", "binding", "child_instance_id"],
     fixtures=[_test("workspace_runtime", "FixedResultVerificationTests.test_effect_sources_require_actual_matching_closed_owned_worker")],
     gaps=["Typed source bytes from the fixed owned-worker fixture are evidence, not task authority; no historical observation writer or compatibility with arbitrary worker effects is certified."])

# L02 wire and in-process contracts: references are source evidence only.
for identifier, version, producer, reader, discriminator, authority, fixtures, gap in (
    ("workspace-worker-pipe", "summon.workspace.pipe/v1",
     _ref("_workspace_transport", "Channel.encode"), _ref("_workspace_transport", "Channel.receive"),
     "protocol", "compatibility_fence", [
         _test("workspace_transport", "test_eof_partial_header_body_and_oversize_refuse"),
         _test("workspace_transport", "test_wrong_channel_scope_tamper_and_type_confusion_revoke")],
     "Authenticated wire framing, not a durable store. Unknown-version bootstrap and handshake refusal fixtures were not established."),
    ("workspace-supervisor-pipe", "summon.workspace.supervisor-pipe/v1",
     _ref("_workspace_transport", "Channel.encode"), _ref("_workspace_transport", "Channel.receive"),
     "protocol", "compatibility_fence", [
         _test("workspace_transport", "test_supervisor_channel_refuses_cross_domain_cross_epoch_and_exact_wire_replay")],
     "SupervisorChannel inherits Channel encode/receive with its own protocol, roles and payload validators. This is a wire contract; unknown-version handshake coverage is not established."),
    ("workspace-compiled-context", "summon.workspace.context/v1",
     _ref("_workspace_runtime", "compile_turn_context"), _ref("_workspace_transport", "_context_fixture_values"),
     "schema", "private_evidence", [
         _test("workspace_transport", "test_compiled_context_malformed_or_cross_bound_refuses_before_frame_send")],
     "Payload envelope embedded in fixture work; source fixture includes a wrong-schema mutation, not historical-version migration certification."),
    ("workspace-message-send-request", "summon.workspace.message-send/v1",
     _ref("_workspace_transport", "_synthetic_worker"), _ref("_workspace_admission", "send_request"),
     "schema", "private_authority", [
         _test("workspace_transport", "test_worker_send_exact_wire_replay_revokes_but_new_sequence_preserves_semantic_request")],
     "Synthetic worker authors this request contract from fixed route input; it is not itself the durable send event. Unknown-version request fixture coverage was not established."),
    ("workspace-message-send-result", "summon.workspace.message-send-result/v1",
     _ref("_workspace_transport", "OwnedFakeWorker._receive_owned"), _ref("_workspace_transport", "_payload"),
     "schema", "private_evidence", [
         _test("workspace_transport", "test_actual_child_authors_computed_message_in_single_reader_and_keeps_result_correlated")],
     "Correlated wire response; the synthetic worker additionally checks request identity and sequence. Fixture source references do not certify runtime execution or unknown-version rejection."),
    ("workspace-transport-admission-envelope", "summon.workspace.transport-admission/v1",
     _ref("_swarm_coordinator", "SwarmCoordinator.workspace_transport_admission"),
     _ref("_workspace_transport", "BoundTransportAdmission.before_spawn"),
     "schema", "private_authority", [
         _test("workspace_transport_budget", "test_consumer_revision_or_prefix_mismatch_refuses_before_popen"),
         _test("workspace_transport_budget", "test_binding_payload_mismatch_and_duplicate_readback_are_one_shot")],
     "In-process coordinator envelope consumed before spawn. The same literal also labels a differently shaped adapter receipt; this entry does not map that receipt's consumer or certify every admission producer."),
    ("transport-budget-result", "summon.transport-budget/v1",
     _ref("_transport_budget", "evaluate_invocation"), _ref("_transport_budget", "result_sha256"),
     "schema", "private_evidence", [
         _test("transport_budget", "test_serialized_payload_budget_binds_exact_utf8_bytes_and_boundary"),
         _test("transport_budget", "test_result_is_bound_to_operation_and_content_digest")],
     "In-process budget result. result_sha256 is a serialization/hash consumer, not a schema/version validator or durable reader; unknown-version rejection is not established."),
):
    _add(identifier, version, producer, reader, discriminator=discriminator,
         authority=authority, fixtures=fixtures,
         gaps=[gap, "Fixture references were inspected as public source only; no runtime fixture was executed for this mapping."])
next(item for item in _FORMATS if item["id"] == "transport-budget-result")["producers"].append(
    _ref("_transport_budget", "evaluate_serialized_payload"))
next(item for item in _FORMATS if item["id"] == "workspace-supervisor-pipe")["declarations"].append(
    _ref("_workspace_transport", "SUPERVISOR_PROTOCOL"))

# Public input/output and callable contracts: these are not independent stores.
_FORMATS.append(_format("resume-capabilities-v2", "summon.resume-capabilities/v2",
    [_ref("_resume_capabilities", "resume_capability_v2")],
    [_ref("_resume_capabilities", "is_exact_capability_v2")],
    authority="public_projection",
    fixtures=[_test("resume_capabilities", "test_v2_rows_are_exact_and_never_grant_launch_permission"),
              _test("resume_capabilities", "test_v2_rejects_forged_scope_permission_and_extra_fields")],
    gaps=["Registry rows declare source capabilities, not fresh executable qualification or launch permission; the stored registry-scope projection is a distinct reduced shape."]))
_FORMATS.append(_format("resume-launch-scope", "summon.resume-launch-scope/v1",
    [_ref("_conversation_runtime", "_launch_scope_observation"),
     _ref("_conversation_runtime", "_compare_resume_launch_scope"),
     _ref("_resume_capabilities", "compare_launch_scope")],
    [_ref("_resume_capabilities", "compare_launch_scope"),
     _ref("_conversation_runtime", "_compare_resume_launch_scope")],
    authority="private_evidence",
    fixtures=[_test("resume_capabilities", "test_v2_launch_scope_match_is_precontact_only_and_preserves_v1_projection"),
              _test("resume_capabilities", "test_v2_launch_scope_refuses_stale_facts_before_contact")],
    gaps=["This literal labels both internal comparison facts and the returned public status/reason result. Internal facts are not automatically public; the public result is a distinct reduced shape. Neither grants contact authority or represents an independent durable store."]))
_FORMATS.append(_format("workspace-plan-v1", "summon.workspace.plan/v1", [],
    [_ref("_workspace_plan", "parse_plan_bytes"), _ref("_workspace_plan", "_validate_plan_v1"),
     _ref("_workspace_plan", "compile_plan")],
    authority="definition_input",
    fixtures=[_test("workspace_plan", "test_parse_is_strict_and_returns_a_detached_value"),
              _test("workspace_plan", "test_unknown_or_authority_fields_are_rejected"),
              _test("workspace_plan", "test_compile_generates_inert_authority_and_message_grants")],
    gaps=["Operator-authored input has no identified production writer; validation and compilation consume it and are not plan writers."]))
for identifier, literal, producer, reader, fixtures in (
    ("workspace-admission", "summon.workspace-admission/v1", "workspace_admission_contract", "_contract",
     [_test("workspace_runtime", "WorkspaceRuntimeTests.test_real_public_prepare_uses_actual_enabled_base_contract"),
      _test("workspace_runtime", "WorkspaceRuntimeTests.test_runtime_requires_the_budget_contract_shape_without_authorizing_work")]),
    ("workspace-operator-send-base", "summon.workspace.operator-send-base/v1", "workspace_operator_send_contract", "_operator_send_contract",
     [_test("workspace_operator_runtime", "test_unknown_base_contract_refuses_before_authority_or_source_reads")]),
    ("workspace-supervisor-inbox-base", "summon.workspace.supervisor-inbox-base/v1", "supervisor_inbox_contract", "_supervisor_contract", []),
    ("workspace-worker-send-base", "summon.workspace.worker-send-base/v1", "workspace_worker_send_contract", "_worker_send_contract",
     [_test("workspace_worker_send", "test_base_capability_does_not_claim_installed_authenticated_ingress")]),
):
    _add(identifier, literal, _ref("_swarm_coordinator", "SwarmCoordinator." + producer),
         _ref("_workspace_runtime", "WorkspaceRuntime." + reader),
         authority="compatibility_fence", fixtures=fixtures,
         gaps=["Callable trusted-host integration contract, not a serialized store or installed/authenticated ingress qualification."]
              + ([] if fixtures else ["No literal-specific supervisor base compatibility fixture established in this bounded source audit."]))
_add("workspace-supervisor-offer-budget", "summon.workspace.supervisor-offer-budget/v1",
     _ref("_swarm_coordinator", "SwarmCoordinator.consume_supervisor_offer_budget"),
     _ref("_swarm_coordinator", "_validate_supervisor_budget_admission"),
     authority="private_authority",
     shape=["status", "policy", "snapshot", "request", "decision", "journal_revision", "journal_prefix_sha256"],
     fixtures=[_test("workspace_demo", "ConductorDemoTests.test_supervisor_offer_budget_refuses_rate_stream_and_oversize_before_write")],
     gaps=["Nested budget_admission on the supervisor-offer consumption journal record and returned result; zero execution slots and consumed authority, not a separate store.",
           "The referenced refusal fixture does not establish literal-specific historical replay or malformed-version compatibility."])

# Fleet declarations/receipts remain separate from authenticated launch authority.
_FORMATS.append(_format("fleet-draft", "summon.fleet/v1",
    [_ref("_fleet_compile", "build_draft")],
    [_ref("_fleet_compile", "compile_fleet"), _ref("_fleet_compile", "verify_plan_for_fleet"),
     _ref("_evidence", "verify"), _ref("_evidence", "_validate_schema_value")],
    authority="definition_input", shape=["draft=true", "lanes", "sha256"],
    fixtures=[_test("phase1_fleet", "test_propose_validate_inspect_explain_are_deterministic_and_no_contact"),
              _test("phase1_fleet", "test_fleet_schema_rejects_tampering_duplicate_keys_private_paths_and_secrets")],
    gaps=["Digest-sealed public-safe draft constraints are declaration input, not authenticated approval or dispatch permission; no historical draft migration is established."]))
_FORMATS.append(_format("fleet-plan", "summon.fleet-plan/v1",
    [_ref("_fleet_compile", "compile_fleet")],
    [_ref("_fleet_compile", "verify_plan_for_fleet"), _ref("_evidence", "verify"),
     _ref("_evidence", "_validate_schema_value")],
    authority="public_projection", shape=["provider_contacted=false", "authorization=advisory_only", "fleet_sha256", "project_sha256", "catalog_sha256", "lanes", "sha256"],
    fixtures=[_test("phase1_fleet", "test_propose_validate_inspect_explain_are_deterministic_and_no_contact"),
              _test("phase1_fleet", "test_explain_rejects_catalog_substitution_and_preserves_source")],
    gaps=["Compiled public-safe plan is advisory_only and binds the draft/catalog/project; a digest seal is not private approval authentication or launch authority.",
          "Deterministic compiler fixtures do not establish live dispatch or historical plan compatibility."]))
_FORMATS.append(_format("fleet-dispatch-ledger", "summon.fleet-dispatch-ledger/v1",
    [_ref("_fleet_dispatch", "reserve_dispatch"), _ref("_fleet_dispatch", "reserve_activation_dispatch"),
     _ref("_fleet_dispatch", "_write_ledger")],
    [_ref("_fleet_dispatch", "_validate_ledger"), _ref("_fleet_dispatch", "_read_ledger")],
    authority="private_authority", shape=["store_id", "approval_id", "approval_generation", "bindings", "authority", "generation", "totals", "claims", "mac"],
    fixtures=[_test("phase1_fleet_dispatch", "test_authenticated_ledger_rejects_forgery_duplicate_nan_and_oversize"),
              _test("phase1_fleet_dispatch", "test_saved_valid_ledger_snapshot_cannot_reopen_launch_claim")],
    gaps=["Ledger MAC/shape validation alone is not current authority: the locked reader also reconciles approval, separate monotonic anchor and initialization marker.",
          "Synthetic tamper/replay fixtures do not qualify live launch, historical migration or deliberate rollback/deletion of every history artifact and its key by the local account owner."]))
_FORMATS.append(_format("fleet-dispatch-claim", "summon.fleet-dispatch-claim/v1",
    [_ref("_fleet_dispatch", "reserve_dispatch"), _ref("_fleet_dispatch", "reserve_activation_dispatch")],
    [_ref("_fleet_dispatch", "_validate_claim"), _ref("_fleet_dispatch", "get_claim")],
    authority="private_authority", shape=["claim_id", "request_id", "request_sha256", "request", "decision", "route", "phase", "slot reservations and consumption", "activation (optional)"],
    fixtures=[_test("phase1_fleet_dispatch", "test_idempotency_semantic_deduplication_and_request_conflict"),
              _test("phase1_fleet_dispatch", "test_activation_launch_claim_is_atomic_private_and_consumes_once")],
    gaps=["Nested claim is authenticated by its containing ledger, not an independently sealed capability; ordinary and activation requests have explicitly different nested fields.",
          "Reservation, permanent slot consumption and provider_contacted uncertainty are distinct; mocked lifecycle fixtures do not prove actual provider contact or every historical claim shape."]))
_FORMATS.append(_format("fleet-activation-binding", "summon.fleet-activation-binding/v1",
    [_ref("_fleet_dispatch", "reserve_activation_dispatch"), _ref("_fleet_dispatch", "claim_activation_provider_launch")],
    [_ref("_fleet_dispatch", "_validate_claim"), _ref("_fleet_dispatch", "preflight_activation_dispatch")],
    authority="private_authority", shape=["contract_sha256", "agent_definition_sha256", "request_identity_sha256", "private_binding_hmac", "billing_class", "attempt_policy", "final_launch_sha256 (after claim)"],
    fixtures=[_test("phase1_fleet_dispatch", "test_activation_reservation_is_private_provider_inert_and_revalidated"),
              _test("phase1_fleet_dispatch", "test_activation_launch_claim_is_atomic_private_and_consumes_once")],
    gaps=["Private binding is nested in an authenticated claim; the evidence-only activation candidate and its public receipt projection are not interchangeable with this HMAC-bound record.",
          "Final-launch digest is added only at claim consumption. Synthetic callback fixtures do not establish real executor compatibility or live activation."]))
_FORMATS.append(_format("fleet-dispatch-anchor", "summon.fleet-dispatch-anchor/v1",
    [_ref("_fleet_dispatch", "_reconcile_dispatch_anchor"), _ref("_fleet_dispatch", "_write_anchor")],
    [_ref("_fleet_dispatch", "_validate_anchor"), _ref("_fleet_dispatch", "_read_anchor")],
    authority="private_authority", shape=["store_id", "approval_id", "generation", "state=clean|prepared", "ledger_sha256", "pending_generation", "pending_ledger_sha256", "retired", "pending_retired", "mac"],
    fixtures=[_test("phase1_fleet_dispatch", "test_anchor_recovers_crash_before_ledger_replace"),
              _test("phase1_fleet_dispatch", "test_malformed_and_replayed_anchor_fail_closed")],
    gaps=["Separate MAC-bound monotonic anchor participates in prepared/clean ledger recovery; it is not a standalone dispatch permission.",
          "Injected write interruptions and replay fixtures do not certify arbitrary power loss or deliberate deletion/rollback of all trusted local artifacts."]))
_add("fleet-dispatch-initialization", "summon.fleet-dispatch-initialization/v1",
     _ref("_fleet_dispatch", "_write_initialization_marker"), _ref("_fleet_dispatch", "_read_initialization_marker"),
     authority="private_authority", shape=["store_id", "approval_id", "approval_generation", "mac"],
     fixtures=[_test("phase1_fleet_dispatch", "test_durable_initialization_marker_detects_both_dispatch_roots_deleted"),
               _test("phase1_fleet_dispatch", "test_initialization_marker_is_authenticated")],
     gaps=["Independent authenticated initialization marker prevents history reset while it survives; it is not sufficient to recover deleted claims or authorize launch.",
           "Fixtures do not establish protection after deliberate deletion of every marker/ledger/anchor/key or historical marker migration."])
_add("fleet-dispatch-public", "summon.fleet-dispatch/v1",
     _ref("_fleet_dispatch", "public_receipt"), _ref("_fleet_dispatch", "verify_public_receipt"),
     authority="public_projection", shape=["authorization=evidence_only", "approval", "bindings", "decision", "resolution", "claim", "activation (optional)", "sha256"],
     fixtures=[_test("phase1_fleet_dispatch", "test_public_receipt_forgery_is_detected_and_never_authoritative"),
               _test("phase1_fleet_dispatch", "test_activation_reservation_is_private_provider_inert_and_revalidated")],
     gaps=["Public receipt is digest-sealed evidence only; its verifier cannot recreate a private Reservation or establish current approval/launch authority.",
           "Redaction and tamper fixtures do not certify live provider activity or historical public-reader compatibility."])
_add("fleet-launch-evidence-base", "summon.fleet-launch-evidence/v1",
     _ref("_executor", "_subprocess_launch_evidence"), _ref("_fleet_dispatch", "_validate_launch_evidence"),
     authority="private_evidence", shape=["schema", "backend", "transport", "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256", "env_sha256"],
     fixtures=[_test("phase1_fleet_runtime", "test_launch_evidence_binds_exact_plan_without_exposing_values"),
               _test("phase1_fleet_dispatch", "test_activation_launch_evidence_drift_refuses_before_capacity_consumption"),
               _test("phase1_fleet_launch_evidence", "test_callback_commits_complete_evidence_once_and_keeps_it_private")],
     gaps=["The current helper still emits this exact eight-field base projection; the repaired fleet reader retains it as historical compatibility without payload/physical-attempt evidence. Digests are not a public release of command, environment or private binding evidence.",
           "The fleet reader accepts exact eight-, ten- and eleven-field envelopes. Superseded eight-plus-observation (nine fields), partial enrichment, unknown fields and null observations refuse. Acceptance of a historical envelope does not reconstruct missing payload or attempt proof.",
           "The historical-eight callback fixture uses the real builder helper, control and fleet module with an authenticated synthetic ledger; it establishes synthetic callback acceptance, not full executor execution or live provider qualification."])
_FORMATS.append(_format("fleet-launch-evidence-observed", "summon.fleet-launch-evidence/v1",
     [_ref("_executor", "execute_agent")],
     [_ref("_fleet_dispatch", "_validate_launch_evidence"),
      _ref("_job_resume", "_validate_launch_observation"),
      _ref("_chat_launch_guard", "_before_launch")],
     authority="private_evidence", shape=["schema", "backend", "transport", "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256", "env_sha256", "dispatch_payload_sha256", "attempt_id_sha256", "launch_observation"],
     fixtures=[_test("job_resume", "test_provider_launch_cas_allows_one_boundary"),
               _test("job_resume", "test_trusted_qualification_revocation_refuses_at_provider_boundary"),
               _test("chat_launch_guard_acceptance", "test_v2_guard_binds_turn_policy_and_payload_before_handoff"),
               _test("phase1_fleet_launch_evidence", "test_callback_commits_complete_evidence_once_and_keeps_it_private"),
               _test("phase1_fleet_launch_evidence", "test_invalid_current_evidence_does_not_consume_capacity"),
               _test("phase1_fleet_launch_evidence", "test_observation_shape_and_every_shared_binding_are_checked")],
     gaps=["execute_agent emits eleven fields only when its control requests an observation. FleetLaunchRuntime.control does not request observations, so this fleet reader path is contract compatibility, not the ordinary reachable fleet callback or live qualification.",
           "The repaired fleet reader validates every included digest, the observation's exact shape and shared envelope bindings; it adds no freshness, authenticated qualification or launch authority. Null observation, malformed or mismatched binding and superseded nine-field envelopes refuse.",
           "The helper's intermediate base-plus-observation shape has nine fields and may carry None on measurement failure; it is not this final enriched envelope. Resume consumes schema plus a valid fresh observation without requiring the fleet envelope's exact keys, then separately checks authenticated qualification.",
           "Chat guard consumes payload/attempt and fresh-observation fields but does not independently validate the fleet schema discriminator. Referenced resume/chat fixtures synthesize evidence; they do not establish the repaired fleet callback or live transport acceptance.",
            "The observed-eleven fixture establishes synthetic callback acceptance with the real builder helper, control, fleet module and authenticated synthetic ledger; final payload/attempt fields are synthesized. It remains control-contract compatibility because normal fleet control requests no observation, not full executor execution or live provider qualification. Inventory checks do not execute these fixtures."]))
_FORMATS.append(_format("fleet-launch-evidence-controlled", "summon.fleet-launch-evidence/v1",
     [_ref("_executor", "execute_agent")],
     [_ref("_executor", "ProviderLaunchControl.before_provider_launch"),
      _ref("_fleet_runtime", "FleetLaunchRuntime._claim"),
      _ref("_fleet_dispatch", "_validate_launch_evidence"),
      _ref("_chat_launch_guard", "_before_launch")],
     authority="private_evidence", shape=["schema", "backend", "transport", "command_sha256", "argv_sha256", "cwd_sha256", "env_names_sha256", "env_sha256", "dispatch_payload_sha256", "attempt_id_sha256"],
     fixtures=[_test("chat_launch_guard_acceptance", "test_v2_guard_binds_turn_policy_and_payload_before_handoff"),
               _test("chat_launch_guard_acceptance", "test_v2_guard_refuses_changed_payload_or_physical_attempt"),
               _test("phase1_fleet_launch_evidence", "test_callback_commits_complete_evidence_once_and_keeps_it_private"),
               _test("phase1_fleet_launch_evidence", "test_invalid_current_evidence_does_not_consume_capacity")],
     gaps=["Current execute_agent adds dispatch_payload_sha256 and attempt_id_sha256 to the eight-field builder result. ProviderLaunchControl forwards that mapping through FleetLaunchRuntime._claim to the repaired fleet validator: this is the reachable ten-field callback, not an observation-bearing fleet route.",
           "The fleet reader checks exact shape, every included digest, frozen cwd and raw UTF-8 prompt equality. It checks an explicit valid invocation attempt id when present; fleet activation forbids explicit ids, so the executor-generated attempt digest is audit evidence, not independently authenticated attempt proof.",
           "Chat guard references establish shared payload/attempt consumption only: ten fields alone fail its separate required fresh-observation check, and this reader does not independently validate the fleet schema discriminator. Synthetic guard fixtures do not establish fleet acceptance.",
            "The current-ten fixture establishes synthetic callback acceptance with the real builder helper, control, fleet module and authenticated synthetic ledger; final payload/attempt fields are synthesized. Source reachability and this callback acceptance do not establish full executor execution, provider contact or live qualification. Inventory checks do not execute these fixtures."]))
_add("fleet-terminal-projection", "summon.fleet-terminal-projection/v1",
     _ref("_fleet_runtime", "_result_projection"), _ref("_fleet_runtime", "_canonical_sha256"),
     authority="private_evidence", shape=["status", "execution_status", "exit codes", "provider_contacted", "error_kind", "report_ok", "model", "result_sha256"],
     fixtures=[_test("phase1_fleet_runtime", "test_runtime_claim_spawn_reap_terminalizes_once_without_private_evidence")],
     gaps=["Private terminal hash preimage omits result text but copies model/status metadata; its schema name does not make it a validated public projection.",
           "Generic canonical hashing consumes the mapping inside FleetLaunchRuntime.finalize, and only terminal_sha256 enters the claim. No schema-aware persisted projection reader is established.",
           "Fixture invokes synthetic lifecycle callbacks; it does not independently verify terminal preimage bytes or establish live provider execution."])

# Context compiler: private payloads, hash preimages, and returned results are
# separate shapes. A serializer is a consumer, not a schema/authority validator.
_FORMATS.append(_format("context-input", "summon.context-input/v1", [],
    [_ref("_context_compile", "_parse_context")], authority="definition_input",
    shape=["schema", "blocks"],
    fixtures=[_test("phase1_context_compile", "test_authority_turns_are_byte_identical_ordered_and_never_deduplicated"),
              _test("phase1_compatibility", "ContextProfileOffCompatibilityGoldens.test_dispatch_context_rejects_file_claimed_authority")],
    gaps=["Caller-authored input has no originating production writer established; _parse_context validates and reconstructs a normalized input mapping.",
          "The compiler can preserve trusted authority blocks, but ordinary dispatch rejects authority claimed by an input file; an input schema is not permission.",
          "Fixture sources establish bounded behavior, not every historical input producer or transport."]))
_FORMATS.append(_format("context-output-result", "summon.context-output/v1",
    [_ref("_context_compile", "_compile_context"), _ref("_context_compile", "_off_result")],
    [_ref("run_subagent", "_prepare_dispatch_context")], authority="private_evidence",
    shape=["profile", "provider_contacted", "compiled_utf8", "source_to_output", "before_bytes", "after_bytes",
           "token_estimate", "lineage_sha256", "retained_evidence", "rollback"],
    fixtures=[_test("phase1_context_compile", "test_raw_input_off_profile_and_safe_rollback_preserve_exact_original_bytes"),
              _test("phase1_compatibility", "ContextProfileOffCompatibilityGoldens.test_safe_context_is_appended_once_and_background_forwards_frozen_identity")],
    gaps=["Outer safe/off compiler result contains private compiled text and reversible source bytes; safe means mechanical compilation, not public redaction or new authority.",
          "Dispatch consumes fields from its local compiler result without independently validating the output schema; off compiled_utf8 preserves input bytes rather than a safe output-block envelope.",
          "The separate public dispatch summary is outside these seven families; no historical output-reader compatibility is established."]))
_add("context-output-prompt", "summon.context-output/v1",
     _ref("_context_compile", "_compile_context"), _ref("run_subagent", "_prepare_dispatch_context"),
     authority="private_evidence", shape=["profile", "blocks"],
     fixtures=[_test("phase1_context_compile", "test_frozen_safe_and_off_compatibility_goldens"),
               _test("phase1_context_compile", "test_safe_deduplicates_only_identical_explicit_immutable_payloads")],
     gaps=["Safe-profile inner prompt envelope shares the outer result schema literal but has a different shape; it is serialized into compiled_utf8 and can contain private payload or preserved authority text.",
           "Dispatch parses blocks to check reference use and embeds the text under its data boundary; it does not independently validate this schema discriminator or confer authority.",
           "Frozen prompt-byte fixtures do not establish a schema-aware historical consumer for every block shape."])
_FORMATS.append(_format("context-lineage", "summon.context-lineage/v1",
    [_ref("_context_compile", "_compile_context"), _ref("_context_compile", "_off_result")],
    [_ref("_context_compile", "_canonical")], authority="private_evidence",
    shape=["profile", "source_sha256", "compiled_sha256 (safe only)", "mapping", "retained_evidence"],
    fixtures=[_test("phase1_context_compile", "test_safe_dry_run_mapping_counts_lineage_and_rollback_are_deterministic")],
    gaps=["Private lineage envelope is a canonical hash preimage, not a returned or persisted standalone record; the safe preimage additionally binds compiled_sha256.",
          "_canonical consumes this mapping only as a generic JSON serializer; no schema-aware or persisted reader, discriminator validation or authority validation is established.",
          "The fixture checks deterministic results and digest length, not an independently reconstructed lineage preimage or historical hash-domain migration."]))
_add("context-receipt-binding", "summon.context-receipt-binding/v1",
     _ref("_context_compile", "_receipt_binding"), _ref("_context_compile", "_canonical"),
     authority="private_evidence", shape=["report_id", "report_body_sha256", "receipt_sha256"],
     fixtures=[_test("phase1_context_compile", "test_diagnostic_truncation_requires_preceding_validated_report_and_receipt")],
     gaps=["Ephemeral canonical hash preimage inside _receipt_binding; callers retain binding_sha256, not this envelope as a standalone store.",
           "_canonical is a generic JSON serialization consumer, not a schema-aware or persisted reader and not discriminator or authority validation.",
           "The malformed-linkage fixture does not authenticate a provider receipt or qualify historical binding versions; linkage is a local consistency check."])
_FORMATS.append(_format("context-receipt-proof", "summon.context-receipt-proof/v1",
    [_ref("_context_compile", "make_receipt_proof"), _ref("_context_compile", "_receipt_proof")],
    [_ref("_context_compile", "_receipt_proof"), _ref("_context_compile", "_compile_context")],
    authority="private_evidence", shape=["report_id", "report_body_sha256", "receipt_sha256", "binding_sha256"],
    fixtures=[_test("phase1_context_compile", "test_diagnostic_truncation_requires_preceding_validated_report_and_receipt"),
              _test("phase1_context_compile", "test_reference_and_receipt_proofs_are_snapshotted_before_field_validation")],
    gaps=["_receipt_proof validates and returns a normalized proof; diagnostic truncation also requires the matching preceding validated-report block and typed errors.",
          "A caller-provided receipt digest and its canonical binding are local linkage evidence, not authenticated provider receipt provenance or permission to execute.",
          "No historical receipt-proof version acceptance established by these source-inspected fixtures."]))
_add("context-reference-proof", "summon.context-reference-proof/v1",
     _ref("_context_target", "make_verified_reference_proof"), _ref("_context_compile", "_proofs"),
     authority="private_evidence", shape=["references", "adapter-minted _VerifiedReferenceProof wrapper"],
     fixtures=[_test("phase1_context_compile", "test_stable_external_reference_requires_target_hash_proof_before_output"),
               _test("phase1_context_compile", "test_trusted_reference_adapter_hash_reads_only_allowed_stable_targets")],
     gaps=["Production filesystem adapter hash-reads allowed local targets and mints an opaque proof wrapper; a plain caller mapping with the same schema and verified flag is refused.",
           "Reference evidence establishes bounded local readback at compilation, not later file stability, provider access or dispatch authority.",
           "No persisted opaque-proof reader or historical proof reconstruction is established."])
_add("context-rollback", "summon.context-rollback/v1",
     _ref("_context_compile", "_rollback"), _ref("run_subagent", "_prepare_dispatch_context"),
     authority="recovery_state", shape=["source_sha256", "source_utf8_b64"],
     fixtures=[_test("phase1_context_compile", "test_raw_input_off_profile_and_safe_rollback_preserve_exact_original_bytes"),
              _test("phase1_context_compile", "test_safe_dry_run_mapping_counts_lineage_and_rollback_are_deterministic")],
     gaps=["Private reversible source bytes may include authority text; base64 is not redaction. The rollback envelope is nested in the compiler result.",
           "The production dispatch reader consumes only source_sha256 for its summary; no production decoder, restore operation or schema-aware rollback reader is established.",
           "Fixture-only base64 restoration checks exact bytes and digest, not installed recovery or historical-version rollback compatibility."])

# Detached private result: fixture consumers are not production readers.
_FORMATS.append(_format("swarm-projection-rebuild", "summon.swarm.projection-rebuild/v1",
    [_ref("_swarm_coordinator", "rebuild_projection_from_journal")], [],
    authority="private_evidence", reader_contract="producer_only_detached",
    shape=["visibility", "source", "compatibility", "projection_sha256", "projection", "launch_authority"],
    fixtures=[_test("swarm_projection_rebuild", "ProjectionRebuildTests.test_f01_swarm_only_deterministic_detached_projection")],
    gaps=["No production consumer or persistence/recovery installation contract established; inspected callers are test fixtures, not readers.",
          "Private detached result carries launch_authority false; rebuilding neither installs state nor grants execution authority.",
          "Metadata declares the detached contract; source-reference validation does not prove runtime detachment or historical compatibility."]))

# Remaining non-workspace literal families. Source references are ownership and
# compatibility metadata; none of the referenced runtime fixtures run here.
_FORMATS.append(_format("accept-stale-intent-v1", "summon.accept-stale-intent/v1", [],
    [_ref("_deliberation_context", "_parse_legacy_acceptance"),
     _ref("_deliberation_context", "_reproduce_legacy_binding")],
    authority="private_evidence", shape=["packet_sha256", "source_revision_sha256", "run_id", "decision_id", "scope", "expires_at_unix_ms"],
    gaps=["Historical embedded acceptance is reader-only; no current authoring function for this exact namespace-unbound v1 shape was established.",
          "Inspected legacy binding fixtures use acceptance=None and do not cover a non-null v1 intent. No direct v1-intent fixture established; historical reproduction grants no current prompt authority."]))
_FORMATS.append(_format("accept-stale-intent-v2", "summon.accept-stale-intent/v2", [],
    [_ref("_deliberation_context", "_parse_acceptance"),
     _ref("_deliberation_context", "acceptance_identity"),
     _ref("_deliberation_context", "bind_context"),
     _ref("_deliberation_store", "_run_fresh_live")],
    authority="private_authority", shape=["packet_sha256", "source_revision_sha256", "runs_root_sha256", "run_id", "decision_id", "scope", "expires_at_unix_ms"],
    fixtures=[_test("phase1_context_freshness", "test_stale_acceptance_cannot_be_replayed_in_a_different_runs_root"),
              _test("deliberation_cli", "DeliberationCliTests.test_stale_acceptance_identity_is_reachable_from_fresh_cli")],
    gaps=["Externally authored one-run context-use authority; no production authoring function established. It does not authorize routing, change a seat, or certify provider execution."]))
_FORMATS.append(_format("context-source-observation", "summon.context-source-observation/v1",
    [_ref("_deliberation_context_source", "observe_source"),
     _ref("_deliberation_context_source", "_manifest_observation")],
    [_ref("_deliberation_context", "_parse_observation"),
     _ref("_deliberation_context", "revalidate_source")], authority="private_evidence",
    shape=["captured_revision", "current_revision", "current_source_digest", "relation", "revision_delta", "verification_method", "observed_at_unix_ms"],
    fixtures=[_test("phase1_context_source", "test_git_capture_and_same_observation"),
              _test("phase1_context_source", "test_manifest_is_hash_read_back_and_change_is_unknown"),
              _test("phase1_context_freshness", "test_source_kind_and_verification_method_must_match_packet_contract")],
    gaps=["Git fixture uses an injected runner and manifest fixture uses owned bytes; schema/reference validation is not proof of fresh local readback or provider access."]))
_FORMATS.append(_format("deliberation-context-binding-v1", "summon.deliberation-context-binding/v1", [],
    [_ref("_deliberation_context", "parse_private_projection_readonly"),
     _ref("_deliberation_store", "_public_receipt"),
     _ref("_deliberation_replay", "_receipt_metadata")], authority="private_evidence",
    shape=["binding_sha256", "observation", "acceptance", "entries", "no runs_root_sha256"],
    fixtures=[_test("phase1_context_freshness", "test_authenticated_v1_binding_is_readable_but_cannot_be_prompt_authority"),
              _test("deliberation_public_history_acceptance", "test_historical_context_public_durable_reader_is_read_only"),
              _test("deliberation_resume", "ResumeTests.test_legacy_v1_context_recovery_refuses_before_owner_or_journal_mutation")],
    gaps=["No current durable v1 writer: reconstruction is read-only. Replay requires allow_legacy_context; current prompt and recovery paths refuse legacy authority.",
          "Inspected historical fixtures reconstruct fresh bindings with acceptance=None; arbitrary historical producers and embedded v1 stale acceptance remain unqualified."]))
_add("deliberation-context-binding-v2", "summon.deliberation-context-binding/v2",
     _ref("_deliberation_context", "private_projection"),
     _ref("_deliberation_context", "parse_private_projection"),
     authority="private_authority", shape=["binding_sha256", "runs_root_sha256", "state", "observation", "acceptance", "entries"],
     fixtures=[_test("phase1_context_freshness", "test_private_projection_round_trips_and_detects_forgery"),
               _test("deliberation_live", "LiveIntegrationTests.test_durable_context_is_prompt_bound_and_publicly_redacted")],
     gaps=["Receipt-bound context admission only, with routing_authority false. Synthetic binding and fake-executor fixtures do not certify live provider behavior or historical migration."])
_add("deliberation-context-public", "summon.deliberation-context-public/v1",
     _ref("_deliberation_context", "public_projection"),
     _ref("_deliberation_ui", "render", "embedded_javascript"),
     authority="public_projection", shape=["state", "packet_sha256", "binding_sha256", "actual_age_ms", "actual_revision_delta", "entry_count", "optional legacy_read_only"],
     fixtures=[_test("phase1_context_freshness", "test_public_projection_omits_private_context_actor_reason_and_revision"),
               _test("deliberation_public_history_acceptance", "test_historical_context_public_durable_reader_is_read_only")],
      gaps=["The UI consumes projection fields but does not validate this schema literal. No literal-specific browser acceptance fixture established; v1 historical projection additionally carries legacy_read_only true."])
_FORMATS.append(_format("deliberation-context-input", "summon.deliberation-context/v1", [],
    [_ref("_deliberation_context", "parse_context_packet"),
     _ref("_deliberation_store", "_run_fresh_live")], authority="definition_input",
    shape=["source", "freshness_policy", "entries"],
    fixtures=[_test("phase1_context_freshness", "test_parser_deep_snapshots_mutable_input_and_digest_binds_body"),
              _test("phase1_context_freshness", "test_packet_rejects_unknown_fields_duplicate_ids_and_invalid_artifact_refs")],
    gaps=["External durable input has no identified production authoring function. Readback parsers reconstruct this shape for validation, not durable authoring.",
          "The same literal labels a structurally incompatible prompt projection; parse_context_packet does not consume that prompt shape."]))
_FORMATS.append(_format("deliberation-context-prompt", "summon.deliberation-context/v1",
    [_ref("_deliberation_context", "prompt_projection")],
    [_ref("_deliberation_scheduler", "_prompt_packet"), _ref("_deliberation_scheduler", "_render_prompt")],
    authority="private_evidence", shape=["freshness", "entries"],
    fixtures=[_test("phase1_context_freshness", "test_prompt_projection_preserves_historical_ox_identity_without_relabeling"),
              _test("deliberation_live", "LiveIntegrationTests.test_durable_context_is_prompt_bound_and_publicly_redacted")],
    gaps=["Nested private prompt context, not a public projection or durable input packet. Scheduler assembly/JSON serialization consumes it; no standalone schema-aware prompt reader or provider certification is claimed."]))

for version, shape, fixture, gap in (
    (1, ["schema", "sha256", "bounded generic payload"],
     "test_evidence_v1_keeps_its_original_generic_canonical_contract",
     "v1 remains generically writable/readable; it is not reader-only or automatically upgraded to v2, and it lacks v2's kind/value and private-text contract."),
    (2, ["schema", "sha256", "kind", "value"],
     "test_v2_generic_evidence_schema_is_exact_and_private_text_is_rejected",
     "v2 enforces kind/value and rejects obvious private text; a canonical digest is not authenticated provenance or comprehensive privacy qualification."),
):
    _add(f"generic-evidence-v{version}", f"summon.evidence/v{version}",
         _ref("_evidence", "seal"), _ref("_evidence", "verify"),
         authority="private_evidence", shape=shape,
         fixtures=[_test("evidence_kernel", fixture)],
         gaps=[gap, "Generic sealing/verification functions are production API surfaces, not evidence of a persisted store or every historical producer."])
_FORMATS.append(_format("decision-evidence-v2", "summon.decision/v2",
    [_ref("_decision", "decide"), _ref("run_subagent", "_reseal_effective_decision")],
    [_ref("_evidence", "verify"), _ref("_evidence", "_validate_schema_value")],
    authority="public_projection", shape=["schema", "sha256", "provider_contacted", "request", "resolution", "authority", "candidates", "unknowns", "digests"],
    fixtures=[_test("evidence_kernel", "test_decision_exact_agent_and_spend_constraints_win"),
              _test("evidence_kernel", "test_decision_verifier_recomputes_every_visible_constraint_rule"),
              _test("evidence_kernel", "test_decision_verifier_binds_spend_and_usage_projections")],
    gaps=["Provider-inert routing explanation with optional effective/preflight/usage projections. Its digest and visible authority fields are not execution, spend approval, or independently authenticated source evidence."]))

# Release evidence schema 2 owns two bounded nested records.  They remain
# separate inventory rows because platform qualification and retained rendered
# files have different producers/readers and must not be inferred from the
# outer evidence object alone.  This is source mapping, not proof that a
# particular machine has completed the Windows or browser gates.
_FORMATS.append(_format(
    "release-evidence-platform-qualification", 1,
    [_tool_ref("release_gates", "_platform_qualification")],
    [_tool_ref("release_manifest", "_validate_platform_qualification")],
    authority="private_evidence", kind="numeric", discriminator="schema",
    shape=["outer_schema=release-evidence/v2", "schema", "host",
           "windows_evidence", "windows_scoped_gates", "status"],
    fixtures=[
        _test_file("tests/test_release_gates.py",
                   "ReleaseGateRunnerTests.test_non_windows_platform_qualification_never_claims_windows_proof"),
        _test_file("tests/test_release_manifest.py",
                   "ReleaseManifestTests.test_platform_qualification_is_scoped_and_fail_closed"),
    ],
    gaps=["Nested platform qualification is a source-bound projection; unavailable non-Windows evidence cannot qualify Windows release gates, and this row does not claim a completed host run."]))
_FORMATS.append(_format(
    "release-evidence-rendered-bundle", 1,
    [_tool_ref("release_gates", "_collect_rendered_evidence")],
    [_tool_ref("release_manifest", "_validate_rendered_evidence")],
    authority="private_evidence", kind="numeric", discriminator="schema",
    shape=["outer_schema=release-evidence/v2", "schema", "policy",
           "producer", "source_tree_sha256", "producers", "files[name,bytes,sha256]"],
    fixtures=[
        _test_file("tests/test_release_gates.py",
                   "ReleaseGateRunnerTests.test_rendered_bundle_is_bounded_source_bound_and_rejects_stale_files"),
        _test_file("tests/test_release_manifest.py",
                   "ReleaseManifestTests.test_rendered_evidence_requires_canonical_source_bound_files"),
    ],
    gaps=["Nested rendered metadata binds a bounded synthetic loopback bundle by raw bytes; it does not certify external browser provenance or release qualification by itself."]))
_FORMATS.append(_format("liveness-snapshot", "summon.liveness/v1",
    [_ref("_liveness", "LivenessTracker.snapshot")],
    [_ref("_job_control", "RuntimeControl.publish"), _ref("_job_control", "public_heartbeat")],
    authority="private_evidence", shape=["schema", "phase", "elapsed_ms", "expired", "counts", "trusted activity timestamps"],
    fixtures=[_test("evidence_kernel", "test_liveness_only_trusted_meaningful_events_reset_idle_clock"),
              _test("evidence_kernel", "test_liveness_rejects_forged_duplicate_and_zero_token_progress")],
    gaps=["Serialized snapshot is evidence; only the executor-held emitter capability supplies live trust. Replaying the dict cannot acquire that capability or reset clocks."]))
_add("liveness-sealed-evidence", "summon.liveness/v1",
     _ref("_evidence", "seal"), _ref("_evidence", "verify"),
     authority="private_evidence", shape=["schema", "sha256", "phase", "elapsed_ms", "expired", "counts"],
     fixtures=[_test("evidence_kernel", "test_canonical_digest_binds_schema_and_mapping_order"),
               _test("evidence_kernel", "test_liveness_schema_rejects_individual_malformed_values")],
     gaps=["Generic evidence API supports a sealed liveness payload distinct from the unsealed tracker snapshot. No production caller sealing tracker snapshots or dedicated sealed-liveness round-trip fixture was established."])
_add("liveness-heartbeat-public", "summon.liveness/v1",
     _ref("_job_control", "public_heartbeat"), _ref("_background", "run_jobs_query"),
     authority="public_projection", shape=["schema when recognized", "allowlisted partial phase/timestamps/counts"],
     fixtures=[_test("job_control", "test_jobs_status_projects_authenticated_heartbeat_by_typed_schema")],
     gaps=["Nested public heartbeat projection can omit malformed fields; it is not the exact tracker snapshot or sealed evidence shape. Jobs query embeds the projection, not a schema-aware liveness validator."])

for version in (1, 2):
    heartbeat = next(item for item in _FORMATS if item["id"] == f"job-heartbeat-v{version}")
    heartbeat["readers"].append(_ref("_background", "run_jobs_query"))
    heartbeat["evidence_gaps"].append(
        "Background status authenticates v2 with the full-body MAC; v1 accepts its legacy job-bound tag or nonce and labels the result legacy_unverified. Neither is upgraded in place.")
    _add(f"job-heartbeat-public-v{version}", f"summon.job-heartbeat/v{version}",
         _ref("_job_control", "public_heartbeat"), _ref("_background", "run_jobs_query"),
         authority="public_projection", shape=["integrity", "allowlisted job/attempt fields", "optional redacted liveness", "no auth or nonce"],
         fixtures=[_test("job_control", "test_jobs_status_accepts_legacy_inflight_nonce_but_never_exposes_it" if version == 1 else
                         "test_jobs_status_projects_authenticated_heartbeat_by_typed_schema")],
         gaps=["Public projection shares its literal with the private heartbeat but omits authentication material. The reader embeds this allowlist after separate authentication; historical v1 remains unverified evidence."])
_add("agy-profile-lease-v2", "summon.agy-profile-lease/v2",
     _ref("_builder", "_agy_create_profile_lease"), _ref("_builder", "_agy_lease_metadata"),
     authority="private_authority", shape=["expires_at", "owner_pid", "owner_birth", "nonce"],
     fixtures=[_test("agy_1_1_22", "test_agy_profile_lease_is_locked_before_publication"),
               _test("agy_1_1_22", "test_lease_metadata_is_bound_to_profile_token")],
     gaps=["Host resource-retention lease only. Token-bound metadata, an OS-held lock and process-birth checks are distinct boundaries; metadata alone is not liveness or dispatch authority. No earlier lease schema migration was established."])
_add("agy-capability-cache-v1", "summon.agy-capability/v1",
     _ref("_builder", "_store_agy_capability"), _ref("_builder", "_agy_capability_cached"),
     authority="private_evidence", shape=["schema", "supported (normalized realpath|mtime_ns:size keys, bounded)"],
     fixtures=[_test("agy_1_1_22", "test_agy_capability_positive_answer_survives_a_new_process"),
               _test("agy_1_1_22", "test_concurrent_agy_dispatches_share_one_capability_probe")],
     gaps=["Local optimisation cache of positive `agy --help` probes only. A changed binary identity is re-probed; negative or stalled probes are never persisted. It is not qualification of served model, account, or runtime behaviour."])
_add("resume-capabilities-v1-current", "summon.resume-capabilities/v1",
     _ref("_resume_capabilities", "resume_capability"), _ref("_resume_capabilities", "is_exact_capability"),
     authority="public_projection", shape=["backend", "transport", "resume_state", "resume_reason", "steering_mode", "live_steering_acknowledged", "current registry equality"],
     fixtures=[_test("resume_capabilities", "test_each_known_backend_transport_has_a_exact_safe_capability"),
               _test("resume_capabilities", "test_capability_schema_rejects_extra_fields_and_forged_claims")],
     gaps=["Current v1 writer remains supported; registry capability is not launch qualification or acknowledgement of live steering."])
_FORMATS.append(_format("resume-capabilities-v1-historical", "summon.resume-capabilities/v1", [],
    [_ref("_chat_resume", "is_stored_capability")], authority="private_evidence",
    shape=["backend", "transport", "resume_state", "resume_reason", "steering_mode", "live_steering_acknowledged", "historical bounded values without current registry equality"],
    fixtures=[_test("chat_resume", "ChatResumeContractTests.test_historical_v1_refusal_is_readable_but_not_current_authority")],
    gaps=["Historical values for removed adapters are reader-only; the current registry writer cannot reproduce every old row. History readability does not authorize current continuation."]))
_add("chat-resume-registry-scope", "summon.resume-capabilities/v2",
     _ref("_conversation_runtime", "_resume_registry_scope"), _ref("_conversation_runtime", "_is_stored_registry_scope"),
     authority="private_evidence", shape=["schema", "registry_generation", "registry_digest", "adapter", "adapter_version_scope", "external_cli_version_scope"],
     fixtures=[_test("conversation_runtime", "ConversationRuntimeTests.test_restored_original_scope_resumes_same_handle")],
     gaps=["Reduced stored scope is structurally distinct from the full v2 capability row. Shape/type checks do not establish fresh launch observation; the fixture covers continuation scope retention, not every malformed reduced field."])
next(item for item in _FORMATS if item["id"] == "launch-observation-fresh")["readers"].extend([
    _ref("_conversation_runtime", "_launch_scope_observation"),
    _ref("_conversation_runtime", "_compare_resume_launch_scope")])
next(item for item in _FORMATS if item["id"] == "launch-observation-persisted")["readers"].append(
    _ref("_conversation_runtime", "_valid_stored_launch_observation"))

_FORMATS.append(_format("swarm-wire-frame", "summon.swarm/v1",
    [_ref("_swarm_protocol", "make_frame"), _ref("_swarm_protocol", "encode_frame")],
    [_ref("_swarm_protocol", "parse_frame")], authority="private_evidence", discriminator="protocol",
    shape=["protocol", "run_id", "message_id", "type", "sent_at_ms", "payload"],
    fixtures=[_test("swarm_protocol", "SwarmProtocolTests.test_round_trip_is_canonical_and_digest_stable"),
              _test("swarm_protocol", "SwarmProtocolTests.test_unknown_major_protocol_and_unknown_fields_fail_closed")],
    gaps=["Wire payload validation is separate from coordinator membership, claim and lease authority. A syntactically valid frame does not authorize state mutation; hello/hello_ack negotiate the same protocol literal."]))
_add("swarm-encoding-compatibility", "summon.swarm/v1",
     _ref("_swarm_coordinator", "_swarm_v1_compatibility"),
     _ref("_swarm_coordinator", "rebuild_projection_from_journal"),
     authority="private_evidence", shape=["schema", "mode", "features", "launch_authority"],
     fixtures=[_test("swarm_projection_rebuild", "ProjectionRebuildTests.test_f07_narrow_crlf_and_unterminated_v1_compatibility")],
     gaps=["This schema-labeled compatibility summary is not a protocol-discriminated wire frame. The production consumer only nests it in the detached rebuild result; no downstream schema reader or persistence/installation contract is established.",
           "Only CRLF and a valid unterminated final line are recognized legacy encodings; launch_authority remains false."])
_add("context-dispatch", "summon.context-dispatch/v1",
     _ref("run_subagent", "_prepare_dispatch_context"), _ref("run_subagent", "_prepare_dispatch_context"),
     authority="public_projection", shape=["profile", "source_sha256", "compiled_sha256", "lineage_sha256", "dispatch_prompt_sha256", "actions", "rollback_source_sha256"],
     fixtures=[_test("phase1_compatibility", "ContextProfileOffCompatibilityGoldens.test_safe_context_is_appended_once_and_background_forwards_frozen_identity"),
               _test("phase1_compatibility", "ContextProfileOffCompatibilityGoldens.test_foreground_caller_cannot_inject_internal_context_metadata")],
     gaps=["Summary is bound to a compiled dispatch prompt. Inherited background metadata requires separate job/nonce/bundle checks; summary fields alone grant no context or provider authority. No historical dispatch-summary migration established."])
_add("usage-status-combined", "summon.usage-status/v1",
     _ref("run_subagent", "main"), _ref("run_subagent", "_emit"),
     authority="public_projection", shape=["status", "provider_contacted", "advisory_only", "imported", "live"],
     gaps=["Combined usage-status wrapper is produced only when a live-store is selected. _emit is a generic JSON/output consumer, not a schema-aware validator or stored-wrapper reader.",
           "No fixture for this exact combined wrapper was established; imported/live component tests do not qualify the wrapper, live quota truth, or routing authority."])
next(item for item in _FORMATS if item["id"] == "job-continuation-public")["producers"].append(
    _ref("run_subagent", "_emit"))
next(item for item in _FORMATS if item["id"] == "job-continuation-public")["evidence_gaps"].append(
    "Terminal emission can produce an unavailable/unsupported fallback with the same exact public field set when private source sealing fails. No emitter-failure fixture for that fallback was established; available continuations require separately authenticated private state.")

# L02 workspace remainder: declaration coverage is not reader certification.
# Budget names in the coordinator/runtime contract are advertised schema facts,
# not additional producers or readers of the nested budget payloads.
for identifier in ("workspace-admission-policy", "workspace-admission-snapshot",
                   "workspace-admission-request", "workspace-admission-result"):
    item = next(row for row in _FORMATS if row["id"] == identifier)
    item["declarations"].extend([
        _ref("_swarm_coordinator", "SwarmCoordinator.workspace_admission_contract"),
        _ref("_workspace_runtime", "WorkspaceRuntime._contract")])
    item["evidence_gaps"].append(
        "These contract references advertise/check schema names only; they do not parse budget payloads or qualify a historical reader.")
next(row for row in _FORMATS if row["id"] == "workspace-admission")["declarations"].append(
    _ref("_workspace_admission", "ADMISSION_SCHEMA"))
next(row for row in _FORMATS if row["id"] == "workspace-operator-send-base")["declarations"].append(
    _ref("_workspace_admission", "OPERATOR_SEND_SCHEMA"))
next(row for row in _FORMATS if row["id"] == "workspace-protocol-v1")["declarations"].append(
    _ref("_swarm_coordinator", "SwarmCoordinator.workspace_admission_contract"))

# Maximal reservation exemplars are serialized for byte sizing, not replayed as
# accepted policy/snapshot/request/decision records. In particular result={} is
# not a valid bound decision. Keep those structural variants separate.
for suffix, shape in (
    ("policy", ["maximal stream_limits", "maximal integer widths", "synthetic capacity exemplar"]),
    ("snapshot", ["maximal pending_messages", "maximal stream_usage", "synthetic capacity exemplar"]),
    ("request", ["maximal selected_message_ids", "journal_bytes", "synthetic capacity exemplar"]),
    ("decision", ["result={}", "maximal identity digests", "synthetic capacity exemplar"]),
):
    _add("workspace-capacity-admission-" + suffix,
         "summon.workspace.admission-" + suffix + "/v1",
         _ref("_swarm_coordinator", "_bound_record"),
         _ref("_swarm_coordinator", "_canonical_json"),
         authority="private_evidence", shape=shape,
         gaps=["Nested synthetic capacity exemplar, not an accepted runtime record or historical producer.",
               "_canonical_json is a generic serialization consumer for sizing; it does not validate this schema or confer authority.",
               "No literal-specific behavioral fixture is claimed; metadata checks do not certify the capacity bound."])

item = next(row for row in _FORMATS if row["id"] == "workspace-economics")
item["producers"].append(_ref("_workspace_demo", "ConductorDemo.prepare"))
item["readers"].extend([_ref("_swarm_coordinator", "_validate_economics_config"),
                        _ref("_workspace_protocol", "validate_goal_plan_v2")])
item["evidence_gaps"].append(
    "Demo preparation supplies a simulated economics fence; current validators do not establish installed historical reader compatibility.")
item = next(row for row in _FORMATS if row["id"] == "workspace-compiled-context")
item["declarations"].append(_ref("_workspace_demo", "ConductorDemo.admit"))
item["evidence_gaps"].append(
    "Demo admit advertises context_format in an authorization request; that occurrence does not produce or parse the context envelope.")
item = next(row for row in _FORMATS if row["id"] == "workspace-message-send-request")
item["producers"].extend([_ref("_workspace_demo", "ConductorDemo.supervisor_message_setup"),
                          _ref("_workspace_demo", "ConductorDemo.worker_message_setup")])
item["evidence_gaps"].append(
    "The added demo routes are explicit simulated send configurations, not native-provider messaging qualification.")

_add("workspace-command-policy", "summon.workspace-command-policy/v1",
     _ref("_workspace_entry", "WorkspaceHost.provision_command_policy_for_operator_message"),
     _ref("_workspace_entry", "_read_command_policy"), authority="private_authority",
     shape=["workspace_id", "run_id", "generation", "expires_at_ms", "max_active_deliveries", "targets"],
     fixtures=[_test("workspace_entry", "test_open_accepts_only_explicit_private_command_policy")],
     gaps=["Initial policy is separately operator-supplied; the producer extends an existing current policy for one authorized message.",
           "Private file protection and current-generation validation are separate from browser credentials; no historical policy migration is established."])
next(row for row in _FORMATS if row["id"] == "workspace-command-policy")["declarations"].append(
    _ref("_workspace_commands", "COMMAND_POLICY_SCHEMA"))

_add("workspace-operator-command-request", "summon.workspace.operator-command-request/v1",
     _ref("_workspace_commands", "bind_command"),
     _ref("_workspace_runtime", "WorkspaceRuntime._operator_request"),
     authority="private_evidence", shape=["operation_key", "action", "delivery", "task_id", "content"],
     fixtures=[_test("workspace_commands", "test_binding_is_immutable_bounded_and_reconstructable_after_disposition")],
     gaps=["Canonical request bytes bind immutable delivery facts; current installed host policy separately authorizes them.",
           "Current reconstruction fixture is not historical-version or provider execution qualification."])
next(row for row in _FORMATS if row["id"] == "workspace-operator-command-request")["readers"].append(
    _ref("_workspace_entry", "WorkspaceHost._authorize"))
_add("workspace-operator-decision", "summon.workspace.operator-decision/v1",
     _ref("_workspace_runtime", "WorkspaceRuntime._operator_request"),
     _ref("_workspace_runtime", "WorkspaceRuntime._record_operator_phase"),
     authority="private_evidence", shape=["decision=authorized", "request_sha256", "request"],
     gaps=["Persisted private canonical decision bytes are read back and compared exactly before the owner-fenced append; the reader is not a standalone schema decoder.",
           "No detached decision-import or historical migration contract, and no literal-specific behavioral fixture, is established."])

# The same disposition-request literal describes two different private records:
# an authorization callback argument and a post-proof canonical hash preimage.
_add("workspace-disposition-authorization-request", "summon.workspace.operator-disposition-request/v1",
     _ref("_workspace_runtime", "WorkspaceRuntime._operator_disposition_binding"),
     _ref("_workspace_entry", "WorkspaceHost._authorize"), authority="private_evidence",
     shape=["action=retain_held_context", "operation_key", "delivery_id", "task_id", "reason"],
     gaps=["Ephemeral callback argument; the installed policy decides authority, not the supplied schema.",
           "No persisted or historical reader and no literal-specific behavioral fixture is established."])
_add("workspace-disposition-request-preimage", "summon.workspace.operator-disposition-request/v1",
     _ref("_workspace_runtime", "WorkspaceRuntime._operator_disposition_request"),
     _ref("_workspace_admission", "_canonical_event_bytes"), authority="private_evidence",
     shape=["operation_key", "delivery_id", "task_id", "reason", "request_ref", "decision_ref"],
     gaps=["Ephemeral canonical hash preimage omits action and adds two resolved proof references; it is not the authorization callback shape.",
           "_canonical_event_bytes serializes bytes generically; there is no schema-aware persisted preimage reader or historical migration evidence."])
_add("workspace-operator-disposition-payload", "summon.workspace.operator-disposition/v1",
     _ref("_workspace_admission", "build_operator_disposition_event"),
     _ref("_workspace_admission", "operator_disposition_request"), authority="durable_history",
     shape=["action", "delivery_id", "task_id", "request_ref", "decision_ref", "reason"],
     fixtures=[_test("workspace_retain", "test_retain_payload_has_finite_closed_shape"),
               _test("workspace_retain", "test_unknown_future_retain_event_refuses_without_mutation")],
     gaps=["Nested retain-hold event payload is audit/disposition history, not execution, retry or release authority.",
           "The synthetic future-event refusal is not an executed historical-reader compatibility claim."])
for role in ("request", "decision"):
    _add("workspace-operator-disposition-proof-" + role,
         "summon.workspace.operator-disposition-proof/v1",
         _ref("_workspace_runtime", "WorkspaceRuntime.provision_operator_disposition_proofs"),
         _ref("_workspace_admission", "canonical_operator_disposition_proof"),
         authority="private_evidence", shape=["role=" + role, "operation_key", "action", "delivery_id",
                                              "task_id", "reason", "authority=installed_operator_policy"],
         fixtures=[_test("workspace_retain", "test_typed_retain_proofs_bind_roles_operation_reason_and_scope")],
         gaps=["Current owner mints and registers two separately role-bound sources; a public reference alone is not proof.",
               "Source-inspected fixtures do not qualify historical proof issuers or real-provider work."])
_add("workspace-linked-replacement-proposal", "summon.workspace.linked-replacement-proposal/v1",
     _ref("_workspace_commands", "OperatorLinkedReplacementAdapter._bind"),
     _ref("_workspace_admission", "linked_replacement_proposal"),
     authority="definition_input", shape=["action", "operation_key", "parent_target", "recipient_target"],
     fixtures=[_test("workspace_link_proposal", "test_linked_replacement_proposal_is_typed_opaque_and_detached")],
     gaps=["Adapter resolves public presentation handles into installed scope identifiers before creating this proposal; shape validation grants no child authority.",
           "Current host-source validation and owner-fenced append remain separate; no historical or native-provider compatibility is certified."])
next(row for row in _FORMATS if row["id"] == "workspace-linked-replacement-proposal")["readers"].append(
    _ref("_workspace_entry", "WorkspaceHost._authorize"))
_FORMATS.append(_format("workspace-linked-replacement-authority",
    "summon.workspace.linked-replacement-authority/v1", [],
    [_ref("_workspace_runtime", "WorkspaceRuntime._linked_source_matches")],
    authority="private_authority", shape=["role", "parent_delivery_id", "task_id", "recipient",
                                         "generation", "expires_at_ms", "revoked"],
    fixtures=[_test("workspace_link_runtime", "TestLinkedRuntime.test_missing_or_mismatched_source_bytes_refuse_before_append")],
    gaps=["No production issuer is established: installed host supplies separately registered source bytes; fixture construction is not a production producer.",
          "The reader verifies exact current source binding. Historical issuer/reader compatibility and live worker qualification remain unestablished."]))

for identifier, literal, producer, js_reader, fixture in (
    ("workspace-operator-disposition-result", "summon.workspace.operator-disposition-result/v1",
     "OperatorDispositionAdapter._present", "performPendingDisposition",
     "test_client_validates_authenticated_retain_disposition"),
    ("workspace-linked-replacement-result", "summon.workspace.linked-replacement-result/v1",
     "OperatorLinkedReplacementAdapter._present", "performPendingLinked",
     "test_client_validates_linked_replacement_identity"),
):
    _FORMATS.append(_format(identifier, literal, [_ref("_workspace_commands", producer)],
        [_ref("_workspace_client", "_validate_result"), _ref("_workspace_page", js_reader, "embedded_javascript")],
        authority="public_projection",
        shape=(["operation_key", "action", "target", "task_id", "reason", "revision"]
               if identifier == "workspace-operator-disposition-result" else
               ["operation_key", "parent_target", "recipient_target", "child_delivery", "revision"]),
        fixtures=[_test("workspace_client", fixture)],
        gaps=["Public result retains execution_authorized=false and retry_with_new_key=false; observation does not authorize a new operation.",
              "Client fixture supplies synthetic responses. Embedded JavaScript consumption is source-inspected, not executed here or qualified as a historical reader."]))

for suffix, producer, fixture in (
    ("command-refresh", "refresh_command_policy",
     "test_refresh_uses_separate_control_header_and_reconciles_by_request_id"),
    ("command-refresh-status", "refresh_command_policy_status",
     "test_refresh_status_is_read_only_and_accepts_history_not_retained"),
):
    _FORMATS.append(_format("workspace-" + suffix, "summon.workspace." + suffix + "/v1",
        [_ref("_workspace_entry", "WorkspaceHost." + producer)],
        [_ref("_workspace_client", "_validate_refresh_result"), _ref("_workspace_client", "refresh_status")],
        authority="public_projection", shape=["status", "workspace_id", "run_id", "generation",
                                             "target_count", "provider_calls=0", "request_id"],
        fixtures=[_test("workspace_client", fixture)],
        gaps=["Status lookup can return the original command-refresh receipt for a retained key; history_not_retained is not proof that a new refresh is safe.",
              "The client accepts both current literals; this is cross-result compatibility, not historical-version or persistent restart qualification."]))
_add("workspace-endpoint-identity", "summon.workspace.identity/v1",
     _ref("_workspace_ui", "WorkspaceSurface.identity"),
     _ref("_workspace_client", "_validate_identity"), authority="public_projection",
     shape=["workspace_id", "run_id"],
     fixtures=[_test("workspace_client", "test_client_preflights_identity_and_validates_command")],
     gaps=["Opaque endpoint scope observation is authenticated by the surrounding transport, not by these strings or schema alone.",
           "Synthetic client fixture does not prove provider identity, worker presence or historical endpoint compatibility."])

# Session-storage keys carry literal versions; their stored JSON payloads do not.
for suffix, save, restore, validate in (
    ("disposition", "savePendingDisposition", "restorePendingDisposition", "validPendingDisposition"),
    ("linked-replacement", "savePendingLinked", "restorePendingLinked", "validPendingLinked"),
):
    _add("workspace-pending-" + suffix + "-slot", "summon.workspace.pending-" + suffix + "/v1",
         _ref("_workspace_page", save, "embedded_javascript"),
         _ref("_workspace_page", restore, "embedded_javascript"),
         authority="recovery_state", discriminator="sessionStorage key",
         shape=["versioned storage key", "unversioned workspace/body payload"],
         gaps=["A storage-slot version is not a payload discriminator or authority; unreadable/unknown outcomes retain the original operation.",
               "Only current source functions are referenced; no historical storage-reader or literal-specific behavioral fixture is claimed."])
    _add("workspace-pending-" + suffix + "-payload", None,
         _ref("_workspace_page", save, "embedded_javascript"),
         _ref("_workspace_page", validate, "embedded_javascript"),
         authority="recovery_state", kind="unversioned", discriminator=None,
         shape=["workspace", "body", "operation_key", "target/reason" if suffix == "disposition" else "parent_target/recipient_target"],
         gaps=["Stored JSON has no own version; volatile sending/status/revision state is reconstructed, not persisted in this payload.",
               "Preserving an operation key is not permission to retry; source checks do not qualify browser storage or historical recovery."])

for suffix, shape, reader in (
    ("list", ["status=listed", "targets"], "openDetails"),
    ("record", ["status=detail", "id", "task_id", "source_kind", "state", "title", "revision", "body_available"], "detailButton"),
):
    _add("workspace-detail-" + suffix, "summon.workspace.detail/v1",
         _ref("_workspace_details", "WorkspaceDetailAdapter.list_or_detail"),
         _ref("_workspace_page", reader, "embedded_javascript"),
         authority="public_projection", shape=shape,
         fixtures=[_test("workspace_details", "test_typed_details_are_scoped_and_body_is_separate")],
         gaps=["Listed targets and selected detail share a literal but have different status and field shapes.",
               "The browser consumes result fields without a standalone schema validator; source-only metadata does not certify rendered or historical-reader acceptance."])
_add("workspace-detail-body", "summon.workspace.detail-body/v1",
     _ref("_workspace_details", "_safe_record"),
     _ref("_workspace_page", "detailButton", "embedded_javascript"),
     authority="private_evidence", shape=["source_kind", "task_id", "body"],
     fixtures=[_test("workspace_details", "test_typed_details_are_scoped_and_body_is_separate")],
     gaps=["Body is returned raw only through separately installed body scope; metadata access alone does not authorize it. Separate authenticated body access does not make this a public or export-safe format.",
           "Browser reads body fields without a standalone schema validator; no historical body-format compatibility is established."])
_add("workspace-detail-scope", "summon.workspace.detail-scope/v1",
     _ref("_workspace_canonical", "build"), _ref("_workspace_details", "_scope"),
     authority="private_authority", shape=["workspace_id", "run_id", "targets"],
     fixtures=[_test("workspace_details", "test_wrong_type_task_revocation_and_metadata_only_fail_before_resolver")],
     gaps=["Metadata and body scopes share this shape but are separately installed and independently enforced.",
           "Canonical builder derives current sources only; the fixture does not certify historical source issuers or external-session authority."])

# Stable operator authority sources remain distinct from observed supervisor proof.
_add("workspace-operator-message-grant", "summon.workspace.operator-message-grant/v1",
     _ref("_workspace_plan", "compile_plan"), _ref("_workspace_entry", "_grant_resolver"),
     authority="private_authority", shape=["workspace_id", "run_id", "task_id", "recipient", "operation", "execution_authorized"],
     fixtures=[_test("workspace_plan", "test_compile_generates_inert_authority_and_message_grants")],
     gaps=["Plan grant is immutable aggregate entry source for operator.message.receive with execution_authorized false; it does not authorize worker execution or spend.",
           "Compiler fixture checks inert grant fields; no literal-specific historical-version or grant-reader refusal fixture established."])
_add("workspace-operator-scope", "summon.workspace.operator-scope/v1",
     _ref("_workspace_plan", "compile_plan"),
     _ref("_workspace_runtime", "WorkspaceRuntime._operator_message_facts"),
     authority="private_authority", shape=["workspace_id", "run_id", "operator_id", "scope"],
     fixtures=[_test("workspace_operator_runtime", "test_runtime_refuses_lost_authority_or_source_without_queue")],
     gaps=["Exact source bytes and digest bind the operator send scope; current authority and destination validation are additional checks, not implied by the schema literal.",
           "Fixture uses a synthetic scope source and wrong bytes; no historical-version or complete aggregate scope-reader compatibility established."])
next(item for item in _FORMATS if item["id"] == "workspace-operator-scope")["producers"].append(
    _ref("_workspace_entry", "_demo_scope"))
for identifier, producer in (
    ("workspace-supervisor-offer-observation", "prepare_supervisor_offer"),
    ("workspace-supervisor-receipt-observation", "supervisor_receipt_source"),
):
    _add(identifier, "summon." + identifier.replace("workspace-", "workspace.", 1) + "/v1",
         _ref("_workspace_runtime", "WorkspaceRuntime." + producer),
         _ref("_workspace_runtime", "WorkspaceRuntime.supervisor_inbox_command"),
         authority="private_evidence",
         shape=["private canonical source bytes", "current installed consumer binding"],
         gaps=["Reader compares exact registered evidence bytes with prepared offer or freshly derived receipt bytes; schema labeling alone grants no exposure, acknowledgement, authority or execution.",
               "No literal-specific behavioral fixture established in this bounded source audit; references establish source ownership, not historical-version acceptance.",
               "Runtime fixtures were inspected only as source and were not executed."])

FORMAT_INVENTORY = {"schema": SCHEMA, "version": VERSION, "formats": _FORMATS}


class FormatInventoryError(ValueError):
    """Invalid or stale inventory metadata; never a runtime approval."""


def _fail(message):
    raise FormatInventoryError(message)


def _exact(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        _fail(label + " fields are not exact")


def _nonempty(value):
    return type(value) is str and bool(value.strip())


def _path(root, relative):
    if (not _nonempty(relative) or "\\" in relative or ":" in relative
            or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        _fail("unsafe repository path")
    try:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            _fail("source is not a repository-contained file")
    except (OSError, RuntimeError, ValueError) as exc:
        raise FormatInventoryError("source is missing or outside repository") from exc
    return path


def _definitions(tree):
    result = set()
    def walk(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if not isinstance(node, ast.ClassDef):
                    result.add(name)
                walk(node.body, name + ".")
    walk(tree.body)
    return result


def _numeric_set_member(tree, symbol, version):
    """Recognize a module-level set/frozenset of exact integer literals/names.

    Only a direct literal member witnesses the requested historical version.
    Names may describe other members only when uniquely assigned integer
    constants at module scope. No calls, arithmetic or source evaluation occurs.
    """
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
    values = assignments.get(symbol, [])
    if len(values) != 1:
        return False
    members = values[0]
    if isinstance(members, ast.Call):
        if (not isinstance(members.func, ast.Name) or members.func.id != "frozenset"
                or len(members.args) != 1 or members.keywords):
            return False
        members = members.args[0]
    if not isinstance(members, ast.Set):
        return False
    found = False
    for member in members.elts:
        if isinstance(member, ast.Constant) and type(member.value) is int and member.value > 0:
            found = found or member.value == version
        elif isinstance(member, ast.Name):
            refs = assignments.get(member.id, [])
            if (len(refs) != 1 or not isinstance(refs[0], ast.Constant)
                    or type(refs[0].value) is not int or refs[0].value < 1):
                return False
        else:
            return False
    return found


def validate_inventory(root=ROOT, value=None):
    """Return a detached inventory after reference checks, NOT acceptance proof."""
    root = Path(root).resolve(strict=True)
    source = copy.deepcopy(FORMAT_INVENTORY if value is None else value)
    _exact(source, {"schema", "version", "formats"}, "inventory")
    if (type(source["version"]) is not int or type(source["schema"]) is not str
            or (source["schema"], source["version"]) not in {(LEGACY_SCHEMA, 1), (SCHEMA, VERSION)}):
        _fail("unsupported inventory version")
    if type(source["formats"]) is not list or not source["formats"]:
        _fail("formats must be a nonempty list")
    cache, identifiers, records = {}, set(), set()
    def reference(ref, fixture=False):
        _exact(ref, {"path", "symbol", "language"}, "reference")
        path = _path(root, ref["path"])
        if ref["path"] in CANDIDATE_SOURCES:
            _fail("candidate source cannot be inventoried as accepted")
        if not _nonempty(ref["symbol"]) or type(ref["language"]) is not str or ref["language"] not in {"python", "embedded_javascript"}:
            _fail("invalid source symbol or language")
        if ref["path"] not in cache:
            try:
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except (OSError, UnicodeError, SyntaxError) as exc:
                raise FormatInventoryError("source cannot be parsed") from exc
            cache[ref["path"]] = (text, tree, _definitions(tree))
        text, tree, definitions = cache[ref["path"]]
        if ref["language"] == "python":
            found = ref["symbol"] in definitions or (not fixture and any(
                isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == ref["symbol"]
                for target in node.targets) for node in tree.body))
        else:
            found = bool(re.search(r"\b(?:async\s+)?function\s+" + re.escape(ref["symbol"]) + r"\s*\(", text))
        if not found:
            _fail("source symbol definition is missing: " + ref["path"] + ":" + ref["symbol"])
        if fixture and (ref["language"] != "python" or not ref["symbol"].split(".")[-1].startswith("test_")):
            _fail("fixture must name a Python test definition")
        return text, tree
    for item in source["formats"]:
        fields = {"id", "identity", "authority", "producers", "readers", "fixtures", "evidence_gaps", "declarations"}
        if source["version"] == VERSION:
            fields.add("reader_contract")
        _exact(item, fields, "format")
        # v1 retains its exact field set and unconditional reader requirement.
        reader_contract = item.get("reader_contract", "required")
        if type(reader_contract) is not str or reader_contract not in {"required", "producer_only_detached"}:
            _fail("unsupported reader contract")
        identifier = item["id"]
        if not _nonempty(identifier) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier) or identifier in identifiers:
            _fail("format id is invalid or duplicated")
        identifiers.add(identifier)
        try:
            record_key = json.dumps({key: val for key, val in item.items() if key != "id"}, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise FormatInventoryError("format metadata is not canonical JSON") from exc
        if record_key in records:
            _fail("duplicate format record")
        records.add(record_key)
        if type(item["authority"]) is not str or item["authority"] not in AUTHORITIES:
            _fail("unsupported authority classification")
        inputs = []
        for role in ("producers", "readers", "declarations", "fixtures"):
            refs = item[role]
            if type(refs) is not list or (role == "readers" and reader_contract == "required" and not refs):
                _fail("source references must be lists; readers are required")
            seen = set()
            for ref in refs:
                data = reference(ref, role == "fixtures")
                key = json.dumps(ref, sort_keys=True)
                if key in seen:
                    _fail("duplicate reference")
                seen.add(key)
                if role != "fixtures":
                    inputs.append(data)
        gaps = item["evidence_gaps"]
        if type(gaps) is not list or any(not _nonempty(gap) for gap in gaps) or len(set(gaps)) != len(gaps):
            _fail("evidence gaps must be distinct nonempty strings")
        if reader_contract == "producer_only_detached":
            if (not item["producers"] or item["readers"]
                    or item["authority"] != "private_evidence" or not gaps):
                _fail("producer-only detached contract requires producers, no readers, private evidence and an explicit gap")
        if not item["producers"] and not gaps:
            _fail("missing producer requires an explicit evidence gap")
        if not item["fixtures"] and not gaps:
            _fail("format needs an inspected fixture reference or explicit evidence gap")
        identity = item["identity"]
        _exact(identity, {"kind", "discriminator", "version", "binding", "shape"}, "identity")
        kind, field, version, binding, shape = (identity[k] for k in ("kind", "discriminator", "version", "binding", "shape"))
        if type(shape) is not list or any(not _nonempty(field) for field in shape) or len(set(shape)) != len(shape):
            _fail("shape must be distinct nonempty strings")
        if kind == "literal":
            if (type(field) is not str or field not in {"schema", "format", "protocol", "prompt_contract", "sessionStorage key"} or type(version) is not str or not LITERAL_RE.fullmatch(repr(version))
                    or binding is not None or not any(version in LITERAL_RE.findall(text) for text, _ in inputs)):
                _fail("literal format version is absent or invalid: " + identifier)
        elif kind == "numeric":
            if not _nonempty(field) or type(version) is not int or version < 1 or (binding is not None and type(binding) is not dict and not _nonempty(binding)):
                _fail("numeric format version is invalid")
            if type(binding) is dict:
                _exact(binding, {"kind", "symbol"}, "numeric set binding")
                if (binding["kind"] != "set_member" or type(binding["symbol"]) is not str
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding["symbol"])):
                    _fail("numeric set binding is invalid")
                found = any(_numeric_set_member(tree, binding["symbol"], version)
                            for _, tree in inputs)
            elif binding:
                found = any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == binding for t in node.targets)
                            and isinstance(node.value, ast.Constant) and type(node.value.value) is int and node.value.value == version
                            for _, tree in inputs for node in ast.walk(tree))
            else:
                pattern = r"(?:['\"]" + re.escape(field) + r"['\"]|\b" + re.escape(field) + r")\s*:\s*" + str(version) + r"\b"
                found = any(re.search(pattern, text) for text, _ in inputs)
            if not found:
                _fail("numeric version binding is absent")
        elif kind == "unversioned":
            if field is not None or version is not None or binding is not None or not shape:
                _fail("unversioned identity needs an explicit shape and no version")
        else:
            _fail("unsupported identity kind")
    return source


def discover_literals(root=ROOT):
    """Lexical source/literal pairs, including candidates; no runtime imports."""
    root = Path(root).resolve(strict=True)
    result = set()
    for path in sorted((root / SCRIPT_DIR).glob("*.py")):
        if path.name.startswith("test_"):
            continue
        relative = path.relative_to(root).as_posix()
        safe = _path(root, relative)
        for literal in LITERAL_RE.findall(safe.read_text(encoding="utf-8")):
            result.add((relative, literal))
    return tuple(sorted(result))


def gap_report(root=ROOT, value=None):
    """Report source/literal omissions plus manual/evidence limits, even on success."""
    checked = validate_inventory(root, value)
    covered = {(ref["path"], item["identity"]["version"])
               for item in checked["formats"] if item["identity"]["kind"] == "literal"
               for role in ("producers", "readers", "declarations") for ref in item[role]}
    return {
        "unmapped_literals": [{"source": path, "literal": literal,
                               "status": "candidate_only" if path in CANDIDATE_SOURCES else "unmapped"}
                               for path, literal in discover_literals(root) if (path, literal) not in covered],
        "producer_only_detached": sorted(item["id"] for item in checked["formats"]
                                         if item.get("reader_contract") == "producer_only_detached"),
        "missing_producers": sorted(item["id"] for item in checked["formats"] if not item["producers"]),
        "manual_formats": sorted(item["id"] for item in checked["formats"] if item["identity"]["kind"] != "literal"),
        "evidence_gaps": [{"id": item["id"], "gaps": list(item["evidence_gaps"])}
                          for item in sorted(checked["formats"], key=lambda item: item["id"]) if item["evidence_gaps"]],
        "limits": ["Validation proves reference existence, not runtime acceptance or zero migration gaps.",
                   "Producer-only detached entries have no established production reader; their classification is explicit metadata, not runtime proof.",
                   "Numeric, unversioned and structural variants are manually inventoried; automatic completeness is not claimed.",
                   "Literal discovery is lexical within production Python scripts, including embedded JavaScript.",
                   "Embedded JavaScript symbols are checked lexically; Python definitions use AST qualification.",
                    "Fleet, usage, context and remaining formats require separate classification; G02 checkpoint/context references are source-bound but not a claim of universal transport or historical recovery."],
    }


def canonical_inventory(root=ROOT, value=None):
    """Stable JSON for the logical manifest, without runtime or private hashes."""
    checked = validate_inventory(root, value)
    checked["formats"].sort(key=lambda item: item["id"])
    return json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
