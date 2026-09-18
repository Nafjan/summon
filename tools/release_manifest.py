#!/usr/bin/env python3
"""Generate a deterministic local release-evidence manifest for Summon.

This command is intentionally provider-inert.  It hashes the checked-in product
payload, records managed-install convergence, and accepts already-run test counts
and known gates as explicit facts; it never launches a model or changes an install.

Example::

    python tools/release_gates.py --output /tmp/summon-release-evidence.json
    python tools/release_manifest.py --evidence-file /tmp/summon-release-evidence.json \
        --output /tmp/summon-release-manifest.json

The ``--test``/``--gate`` flags are bounded compatibility inputs for preview
reports. A release check must use machine-produced evidence and will reject
failed/unrun facts, missing names, or a dirty source tree. The default
``stable`` profile requires every gate; the explicit ``workspace-preview``
profile accepts only the typed missing-live-evidence boundary documented in
the 3.5 workspace decision.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]


def _load_spawn_run_flags():
    """Load the source checkout's shared utility-spawn policy without path mutation.

    Release-manifest helpers may be loaded for an arbitrary target root, so the
    launch policy must come from this tool's own checkout rather than that target
    tree.  Failing closed here prevents a release check from silently regressing
    to visible Windows utility processes.
    """
    spawn_path = (Path(__file__).resolve().parents[1] / "skills" / "summon"
                  / "scripts" / "_spawn.py")
    if spawn_path.is_symlink() or not spawn_path.is_file():
        raise RuntimeError("shared spawn helper is missing from the release checkout")
    spec = importlib.util.spec_from_file_location(
        "_summon_release_spawn", spawn_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared spawn helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = getattr(module, "run_flags", None)
    if not callable(helper):
        raise RuntimeError("shared spawn helper does not expose run_flags")
    return helper


run_flags = _load_spawn_run_flags()
_PAYLOAD_ROOTS = (
    # Everything shipped by the installer/launcher, including the release
    # tooling used to verify the artifact itself. Tests and local evidence are
    # intentionally excluded from the product payload.
    "CHANGELOG.md", "LICENSE", "TERMS.md", "PRODUCT.md", "README.md",
    "install.py", "plugin.json", "summon.py", "mcp.json", "tools",
    "docs", "skills/summon", "skills/council", "skills/deliberate",
    # Verification inputs are bound to the same evidence hash.  They are not
    # installed payload, but a release claim must not be reusable after tests,
    # CI policy, or the gate runner itself changes.
    "tests", ".github/workflows",
)

# Must mirror install.py's SKILL_PAYLOAD.  The source skill also contains
# provider-inert tests used by the release evidence runner; those tests are
# source-bound by _PAYLOAD_ROOTS above but are intentionally not copied into
# host skill installations.
_MANAGED_SKILL_PAYLOAD = frozenset({
    "SKILL.md", "scripts", "references", "agents", "examples",
})

# These names are the release contract, not caller-provided labels. A
# manifest may still be generated for an intermediate preview with partial
# facts, but --check must contain every entry so missing evidence cannot be
# hidden by inventing a new name.
REQUIRED_TESTS = frozenset({
    "discovery", "install", "release_manifest", "acpbackend",
    "deliberation", "deliberation_resume", "model_catalog",
    "conversation", "conversation_runtime", "conversation_ui", "swarm_protocol",
    "swarm_coordinator", "workspace_plan", "workspace_core", "workspace_demo",
    "workspace_recovery", "workspace_ui",
    "model_routing", "release_contract",
    "account_evidence", "live_provider_gate",
    "release_gates", "release_outcomes", "telemetry_audit", "phase0_phase1", "phase0_launch",
    "schema_inventory",
})
REQUIRED_GATES = frozenset({
    "fake_lifecycle", "browser_security", "model_identity",
    "migration_rollback", "managed_installs", "telemetry_privacy",
    "accessibility", "live_provider",
})
REQUIRED_COMMANDS = {
    "discovery": "python skills/summon/scripts/test_discovery.py",
    "install": "python tests/test_install.py",
    "release_manifest": "python -m unittest tests.test_release_manifest",
    "acpbackend": "python tests/test_acpbackend.py",
    "conversation": "python -m unittest skills.summon.scripts.test_conversation skills.summon.scripts.test_conversation_ui",
    "conversation_runtime": "python -m unittest skills.summon.scripts.test_conversation_runtime",
    "swarm_protocol": "python -m unittest skills.summon.scripts.test_swarm_protocol",
    "swarm_coordinator": "python -m pytest -q skills/summon/scripts/test_swarm_migration_boundary_acceptance.py skills/summon/scripts/test_swarm_coordinator.py skills/summon/scripts/test_fork_and_claim_regressions.py skills/summon/scripts/test_swarm_projection_rebuild.py skills/summon/scripts/test_f01_artifact_identity_acceptance.py skills/summon/scripts/test_f01_strict_entity_acceptance.py skills/summon/scripts/test_workspace_rebuild_acceptance.py skills/summon/scripts/test_workspace_layout.py",
    "workspace_plan": "python -m pytest -q skills/summon/scripts/test_workspace_plan.py skills/summon/scripts/test_workspace_protocol.py skills/summon/scripts/test_workspace_entry.py skills/summon/scripts/test_workspace_preview.py",
    "workspace_core": "python -m pytest -q skills/summon/scripts/test_workspace_state.py skills/summon/scripts/test_workspace_protocol.py skills/summon/scripts/test_workspace_admission.py skills/summon/scripts/test_workspace_budget.py skills/summon/scripts/test_workspace_budget_integration.py skills/summon/scripts/test_transport_budget.py skills/summon/scripts/test_workspace_transport_budget.py skills/summon/scripts/test_workspace_reservation_acceptance.py skills/summon/scripts/test_workspace_runtime.py skills/summon/scripts/test_workspace_transport.py skills/summon/scripts/test_workspace_commands.py skills/summon/scripts/test_workspace_content.py skills/summon/scripts/test_workspace_layout.py skills/summon/scripts/test_workspace_operator_admission.py skills/summon/scripts/test_workspace_review_decision_acceptance.py tests/test_workspace_atomic_capacity.py tests/test_workspace_effect_capacity.py skills/summon/scripts/test_workspace_economics_guarded_acceptance.py",
    "workspace_demo": "python -m pytest -q skills/summon/scripts/test_workspace_demo.py",
    "workspace_recovery": "python -m pytest -q skills/summon/scripts/test_workspace_inbox.py skills/summon/scripts/test_workspace_inbox_recovery.py skills/summon/scripts/test_workspace_operator_reconciliation.py skills/summon/scripts/test_background_read_roots.py tests/test_migration_gate.py",
    "workspace_ui": "python -m pytest -q skills/summon/scripts/test_workspace_page.py skills/summon/scripts/test_workspace_view.py skills/summon/scripts/test_workspace_capability_snapshot.py skills/summon/scripts/test_workspace_view_schema_acceptance.py skills/summon/scripts/test_workspace_ui.py skills/summon/scripts/test_workspace_details.py skills/summon/scripts/test_workspace_operator_runtime.py skills/summon/scripts/test_workspace_operator_send.py skills/summon/scripts/test_workspace_worker_send.py skills/summon/scripts/test_workspace_loopback_acceptance.py skills/summon/scripts/test_workspace_encrypted_version_acceptance.py skills/summon/scripts/test_workspace_canonical_rendered.py skills/summon/scripts/test_workspace_delivery_rendered.py skills/summon/scripts/test_workspace_accessibility_rendered.py skills/summon/scripts/test_workspace_zoom_rendered.py skills/summon/scripts/test_workspace_recovery_rendered.py",
    "conversation_ui": "python -m unittest skills.summon.scripts.test_conversation_ui",
    "release_contract": "python -m unittest tests.test_release_contract",
    "account_evidence": "python -m unittest tests.test_account_evidence",
    "live_provider_gate": "python -m unittest tests.test_live_provider_gate",
    "release_gates": "python -m unittest tests.test_release_gates",
    "release_outcomes": "python -m pytest -q tests/test_release_outcomes.py tests/test_release_gate_artifact_privacy.py",
    "deliberation": "python -m unittest skills.summon.scripts.test_deliberation_engine skills.summon.scripts.test_deliberation_policy skills.summon.scripts.test_deliberation_adapter skills.summon.scripts.test_deliberation_cli skills.summon.scripts.test_deliberation_scheduler skills.summon.scripts.test_deliberation_agents skills.summon.scripts.test_deliberation_roster skills.summon.scripts.test_deliberation_invocation skills.summon.scripts.test_deliberation_replay skills.summon.scripts.test_deliberation_restore skills.summon.scripts.test_deliberation_recovery skills.summon.scripts.test_deliberation_phase_a skills.summon.scripts.test_deliberation_live skills.summon.scripts.test_deliberation_browser skills.summon.scripts.test_deliberation_ui",
    "deliberation_resume": "python -m unittest skills.summon.scripts.test_deliberation_resume",
    "model_catalog": "python -m pytest -q skills/summon/scripts/test_astra_roster.py skills/summon/scripts/test_model_catalog.py tests/test_gemini_roster.py",
    "model_routing": "python -m unittest skills.summon.scripts.test_model_routing",
    "telemetry_audit": "python -m unittest tests.test_telemetry_audit",
    "schema_inventory": "python -m pytest -q tests/test_schema_inventory.py tests/test_format_inventory.py tests/test_format_inventory_l02_acceptance.py tests/test_format_inventory_reader_contract.py tests/test_format_inventory_context.py tests/test_format_inventory_fleet.py tests/test_format_inventory_nonworkspace.py tests/test_format_inventory_workspace.py tests/test_format_inventory_final_structures.py",
"phase0_phase1": "python -m pytest -q skills/summon/scripts/test_phase0_contracts.py skills/summon/scripts/test_account_profiles.py skills/summon/scripts/test_astra_roster.py skills/summon/scripts/test_fable_roster.py skills/summon/scripts/test_claude_attribution.py skills/summon/scripts/test_install_drift.py skills/summon/scripts/test_kimi_timeout.py skills/summon/scripts/test_phase1_usage.py skills/summon/tests/test_phase1_usage_live.py skills/summon/scripts/test_phase1_usage_runner.py skills/summon/scripts/test_phase1_context_compile.py skills/summon/scripts/test_phase1_context_freshness.py skills/summon/scripts/test_phase1_context_source.py skills/summon/scripts/test_portable_result.py skills/summon/scripts/test_agy_1_1_22.py skills/summon/scripts/test_phase1_compatibility.py skills/summon/scripts/test_phase1_operator_workflow.py skills/summon/scripts/test_phase1_fleet_dispatch.py skills/summon/scripts/test_phase1_fleet_activation.py skills/summon/scripts/test_phase1_fleet.py skills/summon/scripts/test_phase1_fleet_approval.py skills/summon/scripts/test_phase1_fleet_runtime.py skills/summon/scripts/test_phase1_fleet_launch_evidence.py skills/summon/scripts/test_evidence_kernel.py skills/summon/scripts/test_job_control.py skills/summon/scripts/test_attempt_and_wait_regressions.py skills/summon/scripts/test_job_continuation.py skills/summon/scripts/test_background_publication_acceptance.py skills/summon/scripts/test_submission_accounting_public_acceptance.py skills/summon/scripts/test_submission_accounting.py skills/summon/scripts/test_submission_accounting_grant_acceptance.py skills/summon/scripts/test_submission_accounting_http_acceptance.py skills/summon/scripts/test_context_policy.py skills/summon/scripts/test_e07_context_economics.py skills/summon/scripts/test_workspace_policy_v2.py tests/test_e07_terminal_seal_acceptance.py skills/summon/scripts/test_e12_turn_fork_acceptance.py skills/summon/scripts/test_fork_and_claim_regressions.py skills/summon/scripts/test_r01_queued_version_acceptance.py skills/summon/scripts/test_r02_candidate_fork_acceptance.py skills/summon/scripts/test_job_resume.py tests/test_profile_resume_rotation_acceptance.py skills/summon/scripts/test_historical_job_claim_acceptance.py skills/summon/scripts/test_resume_capabilities.py skills/summon/scripts/test_evidence_guards.py skills/summon/scripts/test_opencode.py skills/summon/scripts/test_zcode.py skills/summon/scripts/test_background_read_roots.py skills/summon/scripts/test_codex_stream.py skills/summon/scripts/test_executor_ownership.py skills/summon/scripts/test_executor_observation_regressions.py skills/summon/scripts/test_nous_credentials.py skills/summon/scripts/test_windows_credentials.py skills/summon/scripts/test_launch_binding.py skills/summon/scripts/test_launch_qualification.py skills/summon/scripts/test_launch_observer_acceptance.py skills/summon/scripts/test_job_revocation_admission_acceptance.py skills/summon/scripts/test_r01_r02_launch_chain_acceptance.py skills/summon/scripts/test_council_reader_acceptance.py skills/summon/scripts/test_council_between_round_acceptance.py tests/test_council_intermediate_process_acceptance.py skills/summon/scripts/test_deliberation_public_history_acceptance.py skills/summon/scripts/test_transport_refusal_acceptance.py skills/summon/scripts/test_attachment_transport_acceptance.py skills/summon/scripts/test_transport_adapter_acceptance.py tests/test_transport_adapter_exact_acceptance.py tests/test_operator_transport_boundary_acceptance.py skills/summon/scripts/test_chat_launch_guard.py skills/summon/scripts/test_chat_launch_guard_acceptance.py skills/summon/scripts/test_resume_revalidation_acceptance.py skills/summon/scripts/test_jobs_revalidation_cli_acceptance.py skills/summon/scripts/test_roster_drift_acceptance.py skills/summon/scripts/test_chat_source_family_acceptance.py skills/summon/scripts/test_chat_guard_boundary_acceptance.py skills/summon/scripts/test_chat_revalidation_refusal_acceptance.py skills/summon/scripts/test_chat_source_family_runtime_acceptance.py skills/summon/scripts/test_chat_revalidation_input_acceptance.py skills/summon/scripts/test_chat_legacy_migration_acceptance.py skills/summon/scripts/test_chat_public_migration_journey_acceptance.py skills/summon/scripts/test_chat_migration_interruption_acceptance.py skills/summon/scripts/test_conversation_numeric_version_acceptance.py skills/summon/scripts/test_chat_dispatcher_guard_v2_acceptance.py skills/summon/scripts/test_chat_parent_runtime_acceptance.py",
    "phase0_launch": "python -m pytest -q skills/summon/scripts/test_dispatcher_launch_publication_acceptance.py skills/summon/scripts/test_launch_observer.py tests/test_windows_silent_launch.py",
}
# Gate commands are part of the release contract too.  They are intentionally
# fixed in source rather than accepted from the CLI, so a release evidence file
# cannot claim that an arbitrary command proved a safety property.
REQUIRED_GATE_COMMANDS = {
    "fake_lifecycle": "python -m unittest skills.summon.scripts.test_deliberation_phase_a skills.summon.scripts.test_deliberation_live skills.summon.scripts.test_deliberation_resume -q",
    "browser_security": "python -m pytest -q skills/summon/scripts/test_deliberation_browser.py skills/summon/scripts/test_conversation_ui.py skills/summon/scripts/test_deliberation_ui.py skills/summon/scripts/test_workspace_page.py skills/summon/scripts/test_workspace_view_schema_acceptance.py skills/summon/scripts/test_workspace_capability_snapshot.py skills/summon/scripts/test_workspace_ui.py skills/summon/scripts/test_workspace_details.py skills/summon/scripts/test_workspace_os_privacy.py skills/summon/scripts/test_workspace_encrypted_version_acceptance.py",
    "model_identity": "python -m unittest skills.summon.scripts.test_model_catalog skills.summon.scripts.test_model_routing skills.summon.scripts.test_deliberation_roster -q",
    # L04 is intentionally part of the same fixed rollback gate. Its test
    # requires a full Git history, Node/WebCrypto, and owned Windows junction
    # support; non-Windows evidence must remain blocked/unqualified rather
    # than being treated as a skipped pass.
    "migration_rollback": "python -m unittest tests.test_migration_gate tests.test_l04_composed_rollback -q",
    "managed_installs": "python tests/test_install.py",
    "telemetry_privacy": "python -m unittest tests.test_telemetry_gate tests.test_telemetry_audit tests.test_telemetry_producer -q",
    "accessibility": "python -m pytest -q skills/summon/scripts/test_conversation_ui.py skills/summon/scripts/test_deliberation_ui.py skills/summon/scripts/test_workspace_page.py skills/summon/scripts/test_workspace_view_schema_acceptance.py skills/summon/scripts/test_workspace_ui.py skills/summon/scripts/test_workspace_encrypted_version_acceptance.py skills/summon/scripts/test_workspace_accessibility_rendered.py skills/summon/scripts/test_workspace_zoom_rendered.py",
    "live_provider": "python tools/live_provider_gate.py",
}
PLATFORM_QUALIFICATION_KEYS = frozenset({
    "schema", "host", "windows_evidence", "windows_scoped_gates", "status",
})
WINDOWS_SCOPED_GATES = frozenset({
    "migration_rollback", "browser_security", "accessibility",
})
PLATFORM_GATE_STATUSES = frozenset({
    "pass", "blocked", "pending", "not_run", "waived", "unavailable",
})
RENDERED_PRODUCERS = ["workspace_ui", "browser_security", "accessibility"]
# A preview may retain a partial diagnostic bundle.  Final qualification is
# intentionally stricter: every successful producer/toolchain/measurement and
# state-card artifact from the fixed rendered fixtures must be present.
REQUIRED_RENDERED_FILES = frozenset({
    "browser-toolchain.json", "measurements.json", "u08-observations.json",
    "native-zoom-measurements.json",
    "native-zoom-1-workspace.png",
    "native-zoom-1-artifact.png",
    "native-zoom-2-workspace.png",
    "native-zoom-2-artifact.png",
    "native-zoom-4-workspace.png",
    "native-zoom-4-artifact.png",
    "canonical-artifact-separate-scope.png", "canonical-focal-task-scope.png",
    "terminal-guidance-and-model-uncertainty.png",
    "pending-action-authentication-expiry.png", "authentication-focus.png",
    "desktop-workspace-focus.png", "desktop-workspace.png", "desktop-drawer.png",
    "200-percent-equivalent-workspace.png", "200-percent-equivalent-drawer.png",
    "400-percent-equivalent-workspace.png", "400-percent-equivalent-drawer.png",
    "320-css-pixel-reflow-workspace.png", "320-css-pixel-reflow-drawer.png",
    "reduced-motion-timeline-focus.png",
    "u04-worker-accepted-and-queued.png", "u04-worker-included.png",
    "u04-worker-submission-started.png", "u04-worker-submitted.png",
    "u04-worker-acknowledged.png", "u04-worker-not-submitted-and-acknowledged.png",
    "u04-worker-state-card-accepted.png", "u04-worker-state-card-queued.png",
    "u04-worker-state-card-included-in-attempt.png",
    "u04-worker-state-card-submission-started.png",
    "u04-worker-state-card-submitted.png",
    "u04-worker-state-card-acknowledged.png",
    "u04-worker-state-card-not-submitted.png",
    "u08-writer-loss-pending.png", "u08-natural-backoff-stale-owner.png",
    "u08-coalesced-reconnection-notice.png", "u08-pending-key-during-backoff.png",
    "u08-authorized-same-key-continuation.png",
    "u08-unresolved-effects-after-recovery.png",
})
RENDERED_NAME = re.compile(
    r"(?:browser-toolchain|measurements|u08-observations|native-zoom-measurements)\.json|"
    r"native-zoom-(?:1|2|4)-(?:workspace|artifact)\.png|"
    r"(?:canonical-artifact-separate-scope|canonical-focal-task-scope|"
    r"terminal-guidance-and-model-uncertainty|pending-action-authentication-expiry|"
    r"authentication-focus|desktop-workspace-focus|"
    r"(?:desktop|200-percent-equivalent|400-percent-equivalent|320-css-pixel-reflow)-"
    r"(?:workspace|drawer)|reduced-motion-timeline-focus)\.png|"
    r"u04-worker-(?:accepted-and-queued|included|not-submitted-and-acknowledged|failure|"
    r"(?:submission-started|submitted|acknowledged|not-submitted)|"
    r"state-card-(?:accepted|queued|included-in-attempt|submission-started|submitted|"
    r"acknowledged|not-submitted))\.png|"
    r"u08-(?:writer-loss-pending|natural-backoff-stale-owner|"
    r"coalesced-reconnection-notice|pending-key-during-backoff|"
    r"authorized-same-key-continuation|unresolved-effects-after-recovery|failure-shell)\.png"
)
MAX_RENDERED_FILES = 64
MAX_RENDERED_FILE_BYTES = 8 * 1024 * 1024
_TEST_COUNT_RE = re.compile(r"^(0|[1-9][0-9]*)/(0|[1-9][0-9]*)$")
_GATE_STATUSES = frozenset({"pass", "blocked", "pending", "not_run", "waived"})
PREVIEW_GATE_RESULTS = {name: "not_run" for name in REQUIRED_GATES}

# Publication policy is deliberately explicit.  The stable/GA profile keeps
# the historical all-gates contract.  The workspace preview profile is the
# reviewed provider-inert 3.5 slice: it may retain exactly one blocked gate,
# live_provider, when the producer proves that no reviewed receipt was
# supplied.  A command-line profile is required to select that exception; it
# is never inferred from a blocked result or a version label.
RELEASE_PROFILE_SCHEMA = 1
STABLE_RELEASE_PROFILE = "stable"
WORKSPACE_PREVIEW_RELEASE_PROFILE = "workspace-preview"
RELEASE_PROFILES = frozenset({
    STABLE_RELEASE_PROFILE,
    WORKSPACE_PREVIEW_RELEASE_PROFILE,
})


def _release_profile_contract(name: str = STABLE_RELEASE_PROFILE) -> dict[str, object]:
    if name not in RELEASE_PROFILES:
        raise ValueError(f"unknown release profile: {name}")
    return {
        "schema": RELEASE_PROFILE_SCHEMA,
        "name": name,
        "live_provider": (
            "separate" if name == WORKSPACE_PREVIEW_RELEASE_PROFILE else "required"
        ),
    }


def _validated_release_profile(value: object | None) -> dict[str, object]:
    """Validate the explicit publication policy, defaulting old manifests to GA."""
    if value is None:
        return _release_profile_contract()
    if not isinstance(value, Mapping):
        raise ValueError("release profile is missing or malformed")
    expected = _release_profile_contract(value.get("name")) if isinstance(value.get("name"), str) else None
    if expected is None or dict(value) != expected:
        raise ValueError("release profile is unknown or stale")
    return expected


def _preview_live_provider_exception(manifest: Mapping[str, object],
                                     profile: Mapping[str, object]) -> bool:
    """Return true only for the explicit no-receipt preview boundary."""
    if profile.get("name") != WORKSPACE_PREVIEW_RELEASE_PROFILE:
        return False
    gates = manifest.get("known_gates")
    artifacts = manifest.get("gate_results")
    if not isinstance(gates, Mapping) or not isinstance(artifacts, Mapping):
        return False
    artifact = artifacts.get("live_provider")
    return (
        gates.get("live_provider") == "blocked"
        and isinstance(artifact, Mapping)
        and artifact.get("status") == "blocked"
        and artifact.get("error_kind") == "live_provider_evidence_missing"
        and artifact.get("detail", _OUTCOMES.GATE_REDACTED) == _OUTCOMES.GATE_REDACTED
    )


def _load_outcomes():
    spec = importlib.util.spec_from_file_location(
        "_release_outcomes", Path(__file__).with_name("release_outcomes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OUTCOMES = _load_outcomes()


def _validate_machine_outcomes(evidence, root, *, require_pass=True):
    if evidence.get("schema") != 2:
        raise ValueError("legacy evidence is preview-only; structured outcomes are required")
    tests = evidence.get("test_results", {})
    gates = evidence.get("gate_results", {})
    for kind, registry, results in (("suite", REQUIRED_COMMANDS, tests),
                                     ("gate", REQUIRED_GATE_COMMANDS, gates)):
        if not isinstance(results, Mapping) or set(results) != set(registry):
            raise ValueError("structured outcomes do not cover the fixed registry")
        for name, command in registry.items():
            if kind == "gate" and name == "live_provider":
                continue
            row = results[name]
            if not isinstance(row, Mapping):
                raise ValueError("invalid structured result")
            if kind == "suite" and (
                    set(row) != {"count", "output_sha256", "outcomes"}
                    or type(row["count"]) is not str
                    or type(row["output_sha256"]) is not str
                    or not isinstance(row["outcomes"], dict)):
                raise ValueError("invalid structured suite result fields")
            count = _OUTCOMES.validate_outcomes(
                row.get("outcomes"), root=root, kind=kind, name=name,
                command=command, source_hash=evidence.get("source_tree_sha256"),
                git_head=evidence.get("git_head"), require_pass=require_pass)
            if (row.get("output_sha256") != row["outcomes"]["output_sha256"]
                    or row.get("count" if kind == "suite" else "test_count") != count):
                raise ValueError("display count does not match structured outcomes")


def _version_contract(root: Path) -> dict[str, object]:
    """Load the root-local version contract without importing the caller's module."""
    path = root / "tools" / "release_contract.py"
    if not path.is_file() or path.is_symlink():
        return {
            "schema": 1,
            "version": {"canonical": None, "versions": {}, "converged": False,
                         "errors": ["release version contract is missing"]},
            "migration": {"path": "docs/PHASE1_MIGRATION_ROLLBACK.md", "present": False,
                           "complete": False, "missing": ["release contract"]},
            "ready": False,
        }
    spec = importlib.util.spec_from_file_location(
        f"_summon_release_contract_{hash(str(root)) & 0xfffffff}", path
    )
    if spec is None or spec.loader is None:
        return {
            "schema": 1,
            "version": {"canonical": None, "versions": {}, "converged": False,
                         "errors": ["release version contract cannot be loaded"]},
            "migration": {"path": "docs/PHASE1_MIGRATION_ROLLBACK.md", "present": False,
                           "complete": False, "missing": ["release contract"]},
            "ready": False,
        }
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.release_contract(root)


def _passing_count(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = _TEST_COUNT_RE.fullmatch(value)
    if not match:
        return None
    passed, total = int(match.group(1)), int(match.group(2))
    # Machine-produced pytest evidence can include platform-specific skips.
    # A nonzero command exit is rejected by release_gates before this compact
    # count is emitted, so 0 < passed <= total means all executed tests passed.
    if total <= 0 or passed <= 0 or passed > total:
        return None
    return passed, total


def _validate_platform_qualification(value: object) -> dict[str, object]:
    """Validate the source-defined platform qualification projection."""
    if not isinstance(value, Mapping) or set(value) != PLATFORM_QUALIFICATION_KEYS:
        raise ValueError("platform qualification evidence is missing or has unknown fields")
    if (type(value["schema"]) is not int or value["schema"] != 1
            or type(value["host"]) is not str
            or value["host"] not in {"windows", "non_windows"}):
        raise ValueError("invalid platform qualification schema or host")
    host = value["host"]
    expected_availability = "available" if host == "windows" else "unavailable"
    if value["windows_evidence"] != expected_availability:
        raise ValueError("platform qualification availability does not match host")
    if type(value["status"]) is not str or value["status"] not in {"qualified", "incomplete"}:
        raise ValueError("invalid platform qualification status")
    scoped = value["windows_scoped_gates"]
    if (not isinstance(scoped, Mapping)
            or set(scoped) != WINDOWS_SCOPED_GATES
            or any(type(status) is not str or status not in PLATFORM_GATE_STATUSES
                   for status in scoped.values())):
        raise ValueError("invalid platform-scoped gate evidence")
    if host == "non_windows":
        if value["status"] != "incomplete" or set(scoped.values()) != {"unavailable"}:
            raise ValueError("non-Windows evidence cannot qualify Windows gates")
    elif value["status"] == "qualified" and any(status != "pass" for status in scoped.values()):
        raise ValueError("qualified Windows evidence contains a non-passing gate")
    elif value["status"] == "incomplete" and all(status == "pass" for status in scoped.values()):
        raise ValueError("incomplete Windows evidence contains only passing gates")
    return {
        "schema": 1,
        "host": host,
        "windows_evidence": value["windows_evidence"],
        "windows_scoped_gates": dict(scoped),
        "status": value["status"],
    }


def _validate_rendered_evidence(value: object, source_hash: str) -> dict[str, object]:
    """Validate the bounded rendered bundle metadata without trusting paths."""
    required = {"schema", "policy", "producer", "source_tree_sha256", "producers", "files"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("rendered evidence is missing or has unknown fields")
    if (type(value["schema"]) is not int or value["schema"] != 1
            or value["policy"] != "synthetic-loopback-only"
            or value["producer"] != "tools/release_gates.py"
            or value["source_tree_sha256"] != source_hash
            or value["producers"] != RENDERED_PRODUCERS):
        raise ValueError("rendered evidence binding is invalid")
    files = value["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_RENDERED_FILES:
        raise ValueError("rendered evidence file list is invalid")
    seen = set()
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {"name", "bytes", "sha256"}:
            raise ValueError("rendered evidence file entry is invalid")
        name = entry["name"]
        if (not isinstance(name, str) or name in seen or not RENDERED_NAME.fullmatch(name)
                or not isinstance(entry["bytes"], int) or isinstance(entry["bytes"], bool)
                or not 0 <= entry["bytes"] <= MAX_RENDERED_FILE_BYTES
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("rendered evidence file entry is invalid")
        seen.add(name)
    if [entry["name"] for entry in files] != sorted(seen):
        raise ValueError("rendered evidence file list is not canonical")
    return {
        "schema": 1, "policy": "synthetic-loopback-only",
        "producer": "tools/release_gates.py", "source_tree_sha256": source_hash,
        "producers": list(RENDERED_PRODUCERS),
        "files": [dict(entry) for entry in files],
    }


def _rendered_evidence_is_complete(value: Mapping[str, object]) -> bool:
    """Return whether the fixed successful rendered producer set is complete."""
    files = value.get("files")
    return isinstance(files, list) and {
        entry.get("name") for entry in files if isinstance(entry, Mapping)
    } == REQUIRED_RENDERED_FILES


def _canonical_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in {".md", ".json", ".py", ".txt", ".yml", ".yaml"}:
        data = data.replace(b"\r\n", b"\n")
    return data


def _link_like(path: Path) -> bool:
    """Reject ordinary symlinks and Windows junction/reparse directories."""
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    try:
        return bool(checker and checker())
    except OSError:
        return True


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_canonical_bytes(path)).hexdigest()


def _raw_sha256_file(path: Path) -> str:
    """Hash retained rendered bytes exactly as the producer emitted them."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_env() -> dict[str, str]:
    """Prevent ambient Git redirection/config from changing release facts."""
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith("GIT_") or upper.startswith("GITCONFIG"):
            env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError("refusing to replace a symlinked output")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _assert_external_path(root: Path, path: Path, label: str) -> None:
    """Reject release artifacts that would dirty the source checkout.

    The release contract keeps evidence and manifests outside the checkout so
    their creation cannot invalidate the clean-tree/source-hash proof.  Resolve
    existing parents (including junctions/symlinks) before checking containment;
    a lexical prefix check would allow an alias into the repository.
    """
    try:
        root_real = root.resolve(strict=False)
        path_real = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path cannot be resolved safely") from exc
    try:
        path_real.relative_to(root_real)
    except ValueError:
        return
    raise ValueError(f"{label} must be outside the release source tree")


def _tree_fingerprint(
    root: Path,
    *,
    exclude: frozenset[str] = frozenset(),
    include_top_level: frozenset[str] | None = None,
) -> tuple[str | None, set[str], str | None]:
    """Hash a bounded regular-file tree and return ``(digest, files, error)``.

    Install convergence must cover the complete skill, not merely production
    Python files.  Symlinks/junction-like entries and unreadable/non-regular
    files are errors rather than silently hashed as empty content.
    """
    if not root.is_dir() or _link_like(root):
        return None, set(), "tree is missing or symlinked"
    digest = hashlib.sha256()
    files: set[str] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Reserved pytest runtime directories are not product payload.
            # An ordinary file with the same name is still bound below.
            dirnames[:] = [name for name in dirnames if name != ".pytest_cache"]
            dirnames.sort()
            filenames.sort()
            relative_dir = Path(dirpath).relative_to(root)
            if include_top_level is not None and not relative_dir.parts:
                dirnames[:] = [name for name in dirnames if name in include_top_level]
            for dirname in list(dirnames):
                item = Path(dirpath) / dirname
                if _link_like(item):
                    return None, files, f"symlinked directory: {dirname}"
            for filename in filenames:
                if filename in exclude or filename == "*.pyc":
                    continue
                item = Path(dirpath) / filename
                if item.is_symlink() or not item.is_file():
                    return None, files, f"non-regular file: {filename}"
                relative = item.relative_to(root).as_posix()
                if (include_top_level is not None
                        and Path(relative).parts[0] not in include_top_level):
                    continue
                if relative.endswith(".pyc") or "__pycache__" in Path(relative).parts:
                    continue
                data = _canonical_bytes(item)
                name = relative.encode("utf-8")
                digest.update(len(name).to_bytes(8, "big"))
                digest.update(name)
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
                files.add(relative)
    except (OSError, UnicodeError, ValueError) as exc:
        return None, files, type(exc).__name__
    return digest.hexdigest(), files, None


def _included_files(root: Path) -> list[Path]:
    def fail_walk(error):
        raise error

    files: list[Path] = []
    for relative in _PAYLOAD_ROOTS:
        path = root / relative
        if _link_like(path):
            raise ValueError(f"release payload contains a symlink: {relative}")
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for directory, directories, filenames in os.walk(path, followlinks=False, onerror=fail_walk):
                directories[:] = [name for name in directories if name != ".pytest_cache"]
                for name in directories + filenames:
                    item = Path(directory) / name
                    if _link_like(item):
                        raise ValueError(
                            f"release payload contains a symlink: {item.relative_to(root).as_posix()}"
                        )
                    if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                        files.append(item)
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def source_tree_sha256(root: Path = ROOT) -> str:
    """Hash the release payload with length-prefixed relative paths and bytes."""
    digest = hashlib.sha256()
    for path in _included_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        # Text payloads are canonicalized to LF so an identical commit hashes
        # identically on Windows checkouts with core.autocrlf and on POSIX.
        data = _canonical_bytes(path)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _version(root: Path) -> str:
    try:
        value = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        version = value.get("version")
        return version if isinstance(version, str) and version else "unknown"
    except (OSError, ValueError, TypeError):
        return "unknown"


def _git_facts(root: Path) -> dict[str, object]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-c", f"safe.directory={root.resolve()}",
                 "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
                cwd=str(root), env=_git_env(), capture_output=True,
                text=True, encoding="utf-8", timeout=5, check=False,
                **run_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    top = run("rev-parse", "--show-toplevel")
    try:
        top_ok = top is not None and Path(top).resolve() == root.resolve()
    except OSError:
        top_ok = False
    head = run("rev-parse", "HEAD") if top_ok else None
    status = run("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none") if top_ok else None
    return {
        "head": head,
        "dirty": None if status is None else bool(status),
        "status_available": status is not None,
        "changed_paths": None if status is None else len(status.splitlines()),
    }


def _managed_install_classification(records: list[Mapping[str, object]],
                                    report: Mapping[str, object],
                                    managed: list[Mapping[str, object]]) -> dict[str, object]:
    """Classify the managed host set without letting unmanaged drift mask it.

    ``_installs.drift_report`` intentionally reports one global convergence bit. A
    local/project/plugin copy is allowed to be different and is surfaced separately;
    only missing, unknown, duplicate, truncated, drifted, or invalid *managed*
    records block the managed-install release gate.
    """
    managed_records = [item for item in records if item.get("managed")]
    managed_missing = [item.get("label") for item in managed_records
                       if not item.get("present")]
    managed_present = [item for item in managed_records if item.get("present")]
    managed_labels = {item.get("label") for item in managed_present}
    managed_unknown = [item.get("label") for item in report.get("unknown", ())
                       if item.get("managed")]
    managed_duplicates = [item for item in report.get("duplicates", ())
                          if item.get("label") in managed_labels]
    managed_scan_truncated = [item.get("label") for item in report.get("scan_truncated", ())
                              if item.get("label") in managed_labels]
    managed_drift = [item.get("label") for item in report.get("drifted", ())
                     if item.get("managed")]
    managed_invalid = [item.get("label") for item in managed
                       if not item.get("ownership_valid") or not item.get("payload_matches_source")]
    converged = (bool(managed_present) and not managed_missing and not managed_unknown
                 and not managed_duplicates and not managed_scan_truncated
                 and not managed_drift
                 and all(item.get("ownership_valid") and item.get("payload_matches_source")
                         for item in managed))
    return {
        "managed_converged": converged,
        "managed_present": bool(managed_present) and not managed_missing,
        "managed_missing": managed_missing,
        "managed_unknown": managed_unknown,
        "managed_duplicates": managed_duplicates,
        "managed_scan_truncated": managed_scan_truncated,
        "managed_drift": managed_drift,
        "managed_invalid": managed_invalid,
    }


def _install_facts(root: Path) -> dict[str, object]:
    scripts = root / "skills" / "summon" / "scripts"
    # Do not import the running repository's _installs module for an arbitrary
    # root.  A missing payload is a missing/unknown install namespace and must
    # fail `--check`, even when the caller happens to run the manifest tool from
    # a fully installed checkout.
    if not scripts.is_dir():
        return {"available": False, "error": "missing_summon_scripts"}
    try:
        # Load the detector under a root-specific name and restore import state
        # afterward.  A caller can build manifests for multiple roots in one
        # process; generic ``import _installs`` would otherwise pin the first
        # root's sibling imports in sys.modules.
        old_path = list(sys.path)
        saved = {name: sys.modules.get(name) for name in ("_installs", "_receipt")}
        for name in saved:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location(
            f"_summon_manifest_installs_{hash(str(root)) & 0xfffffff}",
            scripts / "_installs.py",
        )
        if spec is None or spec.loader is None:
            raise ImportError("cannot load install detector")
        _installs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_installs)

        records = _installs.enumerate_installs(
            running_scripts_dir=str(scripts), project_dir=str(root)
        )
        report = _installs.drift_report(records)
    except Exception as exc:  # noqa: BLE001 - manifest remains useful if a host is odd
        return {"available": False, "error": type(exc).__name__}
    finally:
        if "old_path" in locals():
            sys.path[:] = old_path
        if "saved" in locals():
            for name, value in saved.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    reference = report.get("reference_sha")
    source_skill_hash, source_skill_files, source_skill_error = _tree_fingerprint(
        root / "skills" / "summon",
        exclude=frozenset({".summon-install.json"}),
        include_top_level=_MANAGED_SKILL_PAYLOAD,
    )
    source_companions = {}
    for companion in ("council", "deliberate"):
        source_path = root / "skills" / companion / "SKILL.md"
        source_companions[companion] = _sha256_file(source_path) if source_path.is_file() else None

    def installed_facts(item: Mapping[str, object]) -> dict[str, object]:
        scripts_dir = item.get("scripts_dir")
        if not isinstance(scripts_dir, str):
            return {"ownership_valid": False, "payload_matches_source": False,
                    "payload_error": "missing scripts path"}
        skill_root = Path(scripts_dir).parent
        skills_root = skill_root.parent
        manifest_path = skill_root / ".summon-install.json"
        ownership_valid = False
        payload_error = None
        manifest_files: set[str] = set()
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            installed_at = data.get("installed_at") if isinstance(data, Mapping) else None
            installed_version = data.get("version") if isinstance(data, Mapping) else None
            listed = data.get("files") if isinstance(data, Mapping) else None
            if (not isinstance(data, Mapping) or data.get("installed_by") != "summon"
                    or not isinstance(installed_at, int) or isinstance(installed_at, bool)
                    or installed_at <= 0 or not isinstance(installed_version, str)
                    or installed_version != _version(root) or not isinstance(listed, list)):
                payload_error = "invalid ownership manifest"
            else:
                for name in listed:
                    if (not isinstance(name, str) or not name or name.startswith(("/", "\\"))
                            or ".." in Path(name).parts):
                        payload_error = "unsafe ownership manifest file list"
                        break
                    manifest_files.add(Path(name).as_posix())
                if payload_error is None:
                    actual_hash, actual_files, tree_error = _tree_fingerprint(
                        skill_root, exclude=frozenset({".summon-install.json"})
                    )
                    if tree_error is not None:
                        payload_error = tree_error
                    elif manifest_files != actual_files:
                        payload_error = "ownership manifest file list does not match tree"
                    elif source_skill_error is not None or source_skill_hash is None:
                        payload_error = "source skill tree is not hashable"
                    elif actual_files != source_skill_files:
                        payload_error = "installed payload file set differs from source"
                    else:
                        ownership_valid = True
                        companion_ok = True
                        companion_errors = []
                        for companion, source_hash in source_companions.items():
                            companion_dir = skills_root / companion
                            companion_file = companion_dir / "SKILL.md"
                            try:
                                extras = [p for p in companion_dir.iterdir()
                                          if p.name != "SKILL.md"]
                            except OSError:
                                extras = [companion_dir]
                            if (source_hash is None or not companion_dir.is_dir()
                                    or _link_like(companion_dir)
                                    or not companion_file.is_file()
                                    or _link_like(companion_file)
                                    or extras
                                    or _sha256_file(companion_file) != source_hash):
                                companion_ok = False
                                companion_errors.append(companion)
                        if not companion_ok:
                            return {
                                "ownership_valid": True,
                                "payload_sha256": actual_hash,
                                "source_payload_sha256": source_skill_hash,
                                "payload_matches_source": False,
                                "payload_error": "companion payload differs: " + ", ".join(companion_errors),
                            }
                        return {
                            "ownership_valid": True,
                            "payload_sha256": actual_hash,
                            "source_payload_sha256": source_skill_hash,
                            "payload_matches_source": actual_hash == source_skill_hash,
                            "payload_error": None if actual_hash == source_skill_hash
                            else "installed payload hash differs from source",
                        }
        except (OSError, ValueError, TypeError, UnicodeError):
            payload_error = "ownership manifest unreadable"
        return {
            "ownership_valid": ownership_valid,
            "payload_sha256": None,
            "source_payload_sha256": source_skill_hash,
            "payload_matches_source": False,
            "payload_error": payload_error or "invalid installed payload",
        }

    managed = []
    for item in report.get("hashed", ()):
        if not item.get("managed") or not item.get("present"):
            continue
        facts = installed_facts(item)
        managed.append({
            "label": item.get("label"),
            "version": item.get("version"),
            "sha256": item.get("sha256"),
            "matches_source": bool(reference and item.get("sha256") == reference),
            **facts,
        })
    drifted = [
        {"label": item.get("label"), "sha256": item.get("sha256")}
        for item in report.get("drifted", ()) if not item.get("managed")
    ]
    stale_managed = [item.get("label") for item in report.get("drifted", ())
                     if item.get("managed")]
    classification = _managed_install_classification(records, report, managed)
    return {
        "available": True,
        "scripts_sha256": reference,
        "managed": managed,
        **classification,
        "unmanaged_drift": drifted,
        "unknown": [item.get("label") for item in report.get("unknown", ())],
        "duplicates": report.get("duplicates", ()),
        "scan_truncated": report.get("scan_truncated", ()),
    }


def _pairs(values: Iterable[str], label: str,
           *, allowed: frozenset[str] | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--{label} expects NAME=VALUE, got {value!r}")
        name, fact = value.split("=", 1)
        if (not name or not fact or name in result or
                (allowed is not None and name not in allowed)):
            raise ValueError(f"invalid or duplicate --{label}: {value!r}")
        if label == "test":
            if _passing_count(fact) is None:
                raise ValueError(f"invalid --test count: {value!r}")
        if label == "gate" and fact not in _GATE_STATUSES:
            raise ValueError(f"invalid --gate status: {value!r}")
        result[name] = fact
    return dict(sorted(result.items()))


def _artifact_sha256(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("artifact_sha256", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_facts(manifest: Mapping[str, object], root: Path = ROOT, *,
                 require_platform_qualification: bool = False,
                 release_profile: str | None = None) -> list[str]:
    """Return deterministic release-contract failures for ``--check``."""
    failures: list[str] = []
    try:
        profile = _validated_release_profile(
            _release_profile_contract(release_profile)
            if release_profile is not None else manifest.get("release_profile")
        )
    except ValueError as exc:
        failures.append(str(exc))
        profile = _release_profile_contract()
    evidence_for_outcomes = manifest.get("evidence")
    try:
        _validate_machine_outcomes(evidence_for_outcomes or {}, root)
        # The projected results must be the same records that intake checked.
        for key in ("test_results", "gate_results"):
            if manifest.get(key) != evidence_for_outcomes.get(key):
                raise ValueError("manifest outcome projection differs from evidence")
    except (ValueError, OSError, TypeError, KeyError):
        failures.append("structured release outcomes are missing, invalid, or blocking")
    version_contract = manifest.get("version_contract")
    if not isinstance(version_contract, Mapping) or version_contract.get("ready") is not True:
        failures.append("version and migration contract is not ready")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping):
        failures.append("machine-produced evidence is required")
    elif evidence.get("source_tree_sha256") != manifest.get("source_tree_sha256"):
        failures.append("evidence does not match the current source payload")
    elif evidence.get("git_head") != (
        manifest.get("git", {}).get("head")
        if isinstance(manifest.get("git"), Mapping) else None
    ):
        failures.append("evidence does not match the current Git HEAD")
    elif evidence.get("producer") != "tools/release_gates.py":
        failures.append("evidence producer is not the release gate runner")
    elif not isinstance(evidence.get("producer_sha256"), str):
        failures.append("evidence producer hash is missing")
    elif not isinstance(evidence.get("commands"), Mapping):
        failures.append("evidence command registry is missing")
    elif dict(evidence["commands"]) != REQUIRED_COMMANDS:
        failures.append("evidence command registry does not match required suites")
    elif not isinstance(evidence.get("gate_commands"), Mapping):
        failures.append("evidence gate command registry is missing")
    elif dict(evidence["gate_commands"]) != REQUIRED_GATE_COMMANDS:
        failures.append("evidence gate command registry does not match required gates")
    git = manifest.get("git")
    if not isinstance(git, Mapping) or not git.get("head"):
        failures.append("git head is unavailable")
    elif git.get("dirty") is None:
        failures.append("source tree cleanliness is unavailable")
    elif git.get("dirty"):
        failures.append("source tree is dirty")
    evidence = manifest.get("evidence")
    platform_qualification = (
        evidence.get("platform_qualification")
        if isinstance(evidence, Mapping) else None
    )
    if platform_qualification is None:
        if require_platform_qualification:
            failures.append("platform qualification evidence is missing")
    else:
        try:
            qualified = _validate_platform_qualification(platform_qualification)
            if require_platform_qualification:
                if qualified["host"] != "windows" or qualified["status"] != "qualified":
                    failures.append("Windows platform qualification is required for --check")
                elif not isinstance(evidence.get("known_gates"), Mapping) or any(
                        evidence["known_gates"].get(name) != "pass"
                        for name in WINDOWS_SCOPED_GATES):
                    failures.append("Windows platform qualification does not match known gates")
                runtime = evidence.get("runtime")
                if (not isinstance(runtime, Mapping)
                        or runtime.get("os_name") != "nt"
                        or runtime.get("sys_platform") != "win32"):
                    failures.append("Windows platform qualification does not match runtime host")
        except (TypeError, ValueError):
            failures.append("platform qualification evidence is invalid")
    rendered_evidence = (
        evidence.get("rendered_evidence")
        if isinstance(evidence, Mapping) else None
    )
    if rendered_evidence is None:
        if require_platform_qualification:
            failures.append("rendered evidence binding is missing")
    else:
        try:
            _validate_rendered_evidence(
                rendered_evidence, manifest.get("source_tree_sha256"))
            if require_platform_qualification and not _rendered_evidence_is_complete(rendered_evidence):
                failures.append("rendered evidence bundle is incomplete")
        except (TypeError, ValueError):
            failures.append("rendered evidence binding is invalid")

    tests = manifest.get("tests")
    if not isinstance(tests, Mapping):
        failures.append("test evidence is missing")
    else:
        missing = sorted(REQUIRED_TESTS - set(tests))
        if missing:
            failures.append("missing test evidence: " + ", ".join(missing))
        unknown = sorted(set(tests) - REQUIRED_TESTS)
        if unknown:
            failures.append("unknown test evidence: " + ", ".join(unknown))
        for name, fact in tests.items():
            if not isinstance(name, str) or not isinstance(fact, str):
                failures.append("test evidence must be string name/count pairs")
                break
            if _passing_count(fact) is None:
                failures.append(f"test evidence is not a passing count: {name}")
    test_values = tests if isinstance(tests, Mapping) else {}
    results = manifest.get("test_results")
    if not isinstance(results, Mapping) or set(results) != REQUIRED_TESTS:
        failures.append("machine test result evidence is missing or incomplete")
    else:
        for name, result in results.items():
            if (not isinstance(result, Mapping)
                    or result.get("count") != test_values.get(name)
                    or not isinstance(result.get("output_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", result["output_sha256"])):
                failures.append(f"invalid machine test result evidence: {name}")
    gates = manifest.get("known_gates")
    if not isinstance(gates, Mapping):
        failures.append("gate evidence is missing")
    else:
        missing = sorted(REQUIRED_GATES - set(gates))
        if missing:
            failures.append("missing gate evidence: " + ", ".join(missing))
        invalid = sorted(set(gates) - REQUIRED_GATES)
        if invalid:
            failures.append("unknown gate evidence: " + ", ".join(invalid))
        for name, status in gates.items():
            if not isinstance(name, str) or not isinstance(status, str):
                failures.append("gate evidence must be string name/status pairs")
                break
            if (name == "live_provider"
                    and _preview_live_provider_exception(manifest, profile)):
                continue
            if status != "pass":
                failures.append(f"gate is not passing: {name}={status}")
    gate_results = manifest.get("gate_results")
    if not isinstance(gate_results, Mapping) or set(gate_results) != REQUIRED_GATES:
        failures.append("machine gate artifact evidence is missing or incomplete")
    elif isinstance(gates, Mapping):
        for name, artifact in gate_results.items():
            current_head = (manifest.get("git", {}).get("head")
                            if isinstance(manifest.get("git"), Mapping) else None)
            try:
                _OUTCOMES.validate_gate_artifact(
                    artifact, root=root, name=name, status=gates.get(name),
                    command=REQUIRED_GATE_COMMANDS.get(name),
                    source_hash=manifest.get("source_tree_sha256"), git_head=current_head)
            except (ValueError, OSError, TypeError, KeyError):
                failures.append(f"invalid machine gate artifact evidence: {name}")
    return failures


def build_manifest(root: Path = ROOT, *, tests: Mapping[str, str] | None = None,
                   gates: Mapping[str, str] | None = None,
                   evidence: Mapping[str, object] | None = None,
                   release_profile: str = STABLE_RELEASE_PROFILE) -> dict[str, object]:
    profile = _release_profile_contract(release_profile)
    source_hash = source_tree_sha256(root)
    version_contract = _version_contract(root)
    return {
        "schema": 1,
        "product": "summon",
        "release_profile": profile,
        "version": _version(root),
        "version_contract": version_contract,
        "source_tree_sha256": source_hash,
        "git": _git_facts(root),
        "installs": _install_facts(root),
        "tests": dict(sorted((tests or {}).items())),
        "test_results": dict(sorted(((evidence or {}).get("test_results", {}) or {}).items())),
        "known_gates": dict(sorted((gates or {}).items())),
        "gate_results": dict(sorted(((evidence or {}).get("gate_results", {}) or {}).items())),
        "evidence": dict(evidence or {}),
    }


def _read_evidence(path: Path, root: Path, source_hash: str, *, allow_legacy_preview=False) -> dict[str, object]:
    """Read a machine-produced evidence file without trusting its path/name."""
    if path.is_symlink():
        raise ValueError("evidence file may not be a symlink")
    value = _OUTCOMES.read_json(path)
    if (not isinstance(value, Mapping) or type(value.get("schema")) is not int
            or value.get("schema") not in {1, 2}):
        raise ValueError("unsupported evidence file schema")
    if value["schema"] == 1 and allow_legacy_preview:
        # Historical counts stay readable, with no transferred execution or
        # approval authority. Do not copy old arbitrary artifacts/runtime text.
        tests, gates = value.get("tests"), value.get("known_gates")
        if not isinstance(tests, Mapping) or not isinstance(gates, Mapping):
            raise ValueError("invalid historical evidence")
        if any(not _OUTCOMES.safe_token(k) or _passing_count(v) is None
               for k, v in tests.items()):
            raise ValueError("invalid historical test counts")
        if any(not _OUTCOMES.safe_token(k) or not isinstance(v, str) or v not in _GATE_STATUSES
               for k, v in gates.items()):
            raise ValueError("invalid historical gate statuses")
        return {"schema": 1, "legacy_preview": True,
                "tests": dict(tests), "known_gates": dict(gates),
                "test_results": {}, "gate_results": {},
                "version_contract": {"ready": False}}
    if value.get("source_tree_sha256") != source_hash:
        raise ValueError("evidence source hash does not match the current tree")
    if value.get("producer") != "tools/release_gates.py":
        raise ValueError("evidence producer is not tools/release_gates.py")
    expected_contract = _version_contract(root)
    if value.get("version_contract") != expected_contract:
        raise ValueError("evidence version/migration contract does not match the current tree")
    producer = root / "tools" / "release_gates.py"
    if not producer.is_file() or value.get("producer_sha256") != _sha256_file(producer):
        raise ValueError("evidence producer hash does not match the current tree")
    tests = value.get("tests")
    gates = value.get("known_gates")
    if not isinstance(tests, Mapping) or not isinstance(gates, Mapping):
        raise ValueError("evidence must contain tests and known_gates mappings")
    # Reuse the same strict parser for facts supplied by the evidence producer.
    test_pairs = _pairs(
        [f"{name}={fact}" for name, fact in tests.items()],
        "test", allowed=REQUIRED_TESTS,
    )
    gate_pairs = _pairs(
        [f"{name}={status}" for name, status in gates.items()],
        "gate", allowed=REQUIRED_GATES,
    )
    if set(gate_pairs) != REQUIRED_GATES:
        raise ValueError("evidence gate results must exactly cover the fixed gate registry")
    commands = value.get("commands", {})
    if not isinstance(commands, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in commands.items()
    ):
        raise ValueError("evidence commands must be a string mapping")
    if dict(commands) != REQUIRED_COMMANDS:
        raise ValueError("evidence commands do not match the fixed release registry")
    gate_commands = value.get("gate_commands", {})
    if not isinstance(gate_commands, Mapping) or dict(gate_commands) != REQUIRED_GATE_COMMANDS:
        raise ValueError("evidence gate command registry does not match the fixed registry")
    platform_qualification = None
    if "platform_qualification" in value:
        platform_qualification = _validate_platform_qualification(
            value["platform_qualification"])
    rendered_evidence = None
    if "rendered_evidence" in value:
        rendered_evidence = _validate_rendered_evidence(
            value["rendered_evidence"], source_hash)
        rendered_dir = path.with_name(path.stem + ".gates") / "rendered"
        if (not rendered_dir.is_dir() or rendered_dir.is_symlink()
                or sorted(item.name for item in rendered_dir.iterdir())
                != [entry["name"] for entry in rendered_evidence["files"]]):
            raise ValueError("rendered evidence files are missing, stale, or unexpected")
        for entry in rendered_evidence["files"]:
            artifact = rendered_dir / entry["name"]
            if (artifact.is_symlink() or not artifact.is_file()
                    or artifact.stat().st_size != entry["bytes"]
                    or _raw_sha256_file(artifact) != entry["sha256"]):
                raise ValueError("rendered evidence file digest does not match")
    results = value.get("test_results")
    if not isinstance(results, Mapping) or set(results) != REQUIRED_TESTS:
        raise ValueError("evidence machine test results must exactly cover the required suites")
    for name, result in results.items():
        if (not isinstance(result, Mapping) or result.get("count") != test_pairs.get(name)
                or not isinstance(result.get("output_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["output_sha256"])):
            raise ValueError(f"invalid machine test result evidence: {name}")
    gate_results = value.get("gate_results")
    if not isinstance(gate_results, Mapping) or set(gate_results) != REQUIRED_GATES:
        raise ValueError("evidence machine gate artifacts must exactly cover the fixed gates")
    for name, artifact in gate_results.items():
        try:
            _OUTCOMES.validate_gate_artifact(
                artifact, root=root, name=name, status=gate_pairs.get(name),
                command=REQUIRED_GATE_COMMANDS.get(name),
                source_hash=source_hash, git_head=value.get("git_head"))
        except (ValueError, OSError, TypeError, KeyError):
            raise ValueError(f"invalid machine gate artifact evidence: {name}") from None
    git_head = value.get("git_head")
    current_git = _git_facts(root)
    current_head = current_git.get("head")
    if not isinstance(git_head, str) or git_head != current_head:
        raise ValueError("evidence Git HEAD does not match the current tree")
    if value.get("git_status_clean") is not True:
        raise ValueError("evidence was not produced from a clean Git tree")
    if current_git.get("dirty") is not False:
        raise ValueError("current Git tree cleanliness is unavailable")
    if value["schema"] == 2:
        _validate_machine_outcomes(value, root)
    projected = {
        "schema": value["schema"],
        "runtime": {k: v for k, v in (value.get("runtime") if isinstance(value.get("runtime"), Mapping) else {}).items()
                    if k in _OUTCOMES.PLATFORM_KEYS and _OUTCOMES.safe_token(v)},
        "source_tree_sha256": source_hash,
        "git_head": git_head,
        "git_status_clean": True,
        "producer": "tools/release_gates.py",
        "producer_sha256": _sha256_file(producer),
        "version_contract": expected_contract,
        "tests": test_pairs,
        "test_results": {name: dict(results[name]) for name in sorted(results)},
        "known_gates": gate_pairs,
        "gate_results": {name: dict(gate_results[name]) for name in sorted(gate_results)},
        "commands": dict(sorted(commands.items())),
        "gate_commands": dict(sorted(gate_commands.items())),
    }
    if platform_qualification is not None:
        projected["platform_qualification"] = platform_qualification
    if rendered_evidence is not None:
        projected["rendered_evidence"] = rendered_evidence
    return projected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    parser.add_argument("--output", help="write JSON to this path instead of stdout")
    parser.add_argument("--test", action="append", default=[], metavar="NAME=COUNT",
                        help="already-run test count, repeatable")
    parser.add_argument("--gate", action="append", default=[], metavar="NAME=STATUS",
                        help="known release gate, repeatable")
    parser.add_argument(
        "--check", action="store_true",
        help="exit 2 unless machine evidence and the selected release profile pass",
    )
    parser.add_argument(
        "--profile", choices=sorted(RELEASE_PROFILES),
        default=STABLE_RELEASE_PROFILE,
        help=("publication policy: stable requires every gate; "
               "workspace-preview permits only the explicit missing live-provider receipt"),
    )
    parser.add_argument("--expected-version",
                        help="require the manifest version to equal this release version")
    parser.add_argument("--evidence-file",
                        help="machine-produced JSON evidence bound to this source hash")
    args = parser.parse_args(argv)
    try:
        root = Path(os.path.abspath(args.root))
        output_path = (Path(os.path.abspath(args.output))
                       if args.output else None)
        evidence_path = (Path(os.path.abspath(args.evidence_file))
                         if args.evidence_file else None)
        if output_path is not None:
            _assert_external_path(root, output_path, "manifest output")
        if evidence_path is not None:
            _assert_external_path(root, evidence_path, "evidence input")
        source_hash = source_tree_sha256(root)
        evidence = (_read_evidence(evidence_path, root, source_hash,
                                   allow_legacy_preview=not args.check)
                    if evidence_path is not None else None)
        if evidence is not None and (args.test or args.gate):
            raise ValueError("--evidence-file cannot be combined with --test/--gate")
        manifest = build_manifest(
            root,
            tests=(evidence or {}).get("tests") if evidence else
                   _pairs(args.test, "test", allowed=REQUIRED_TESTS),
            gates=(evidence or {}).get("known_gates") if evidence else
                   _pairs(args.gate, "gate", allowed=REQUIRED_GATES),
            evidence=evidence,
            release_profile=args.profile,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path is not None:
        _atomic_write_text(output_path, encoded)
    else:
        sys.stdout.write(encoded)
    installs = manifest["installs"]
    if args.expected_version is not None and manifest.get("version") != args.expected_version:
        print(
            "release check: version does not match --expected-version: "
            f"{manifest.get('version')!r} != {args.expected_version!r}",
            file=sys.stderr,
        )
        return 2
    if args.check:
        failures = _check_facts(
            manifest, root,
            require_platform_qualification=args.check,
            release_profile=args.profile,
        )
        if not args.evidence_file:
            failures.append("--evidence-file is required for --check")
        if not installs.get("available"):
            failures.append("managed-install inventory is unavailable")
        elif not installs.get("managed_converged"):
            failures.append("managed installs are not converged")
        if installs.get("managed_invalid"):
            failures.append("managed install payload/ownership is invalid")
        if failures:
            for failure in failures:
                print(f"release check: {failure}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
