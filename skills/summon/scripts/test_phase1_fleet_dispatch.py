"""Provider-inert M3b2 private dispatch-ledger regression tests."""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from _builder import AgentInvocation
import _evidence
import _fleet
import _fleet_activation
import _fleet_approval
import _fleet_compile
import _fleet_dispatch


def _agents(two: bool = False):
    values = [{
        "name": "alpha", "run_agent": "claude", "provider": "anthropic",
        "permission": "read-only", "model": "frontier-alpha",
        "source": "project", "lifecycle": "active",
    }]
    if two:
        values.append({
            "name": "beta", "run_agent": "codex", "provider": "openai",
            "permission": "read-only", "model": "frontier-beta",
            "source": "project", "lifecycle": "active",
        })
    return values


def _fleet_material(tmp_path, *, contacts=1, billable=1, parallel=1,
                    credit=False, payg=False, two=False,
                    data_boundary="local_sanitized"):
    agents = _agents(two)
    seats = [item["name"] for item in agents]
    _report, fleet, plan = _fleet.proposal(
        lane="review", seats=seats, agents=agents, cwd=str(tmp_path),
        permission_ceiling="read-only", data_boundary=data_boundary,
        corrective={"contract_repair": False, "retry": False,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": credit, "payg": payg,
               "max_provider_contacts": contacts,
               "max_billable_attempts": billable,
               "max_parallel": parallel})
    catalog, digest, unavailable = _fleet.catalog_snapshot(agents)
    assert not unavailable
    assert digest == _evidence.verify(plan)["catalog_sha256"]
    return fleet, plan, catalog


@pytest.fixture
def approved(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    fleet, plan, catalog = _fleet_material(tmp_path)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    return {
        "root": root, "fleet": fleet, "plan": plan, "catalog": catalog,
        "approval_id": receipt["approval"]["approval_id"], "cwd": str(tmp_path),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reserve(context, *, prompt="review this", request_id=None,
             billing=None, boundary=None):
    prompt_sha = _sha(prompt)
    seats = [item["seat"] for item in context["catalog"]]
    billing = billing or {seat: "subscription" for seat in seats}
    boundary = boundary or {
        "boundary": "local_sanitized", "proof": "operator_attested",
        "evidence_sha256": prompt_sha,
    }
    return _fleet_dispatch.reserve_dispatch(
        approval_id=context["approval_id"], fleet=context["fleet"],
        plan=context["plan"], catalog=context["catalog"], lane_name="review",
        cwd=context["cwd"],
        prompt_sha256=prompt_sha, billing_by_seat=billing,
        data_boundary=boundary, request_id=request_id)


def _claim(context, reservation):
    return _fleet_dispatch.claim_provider_launch(
        reservation, fleet=context["fleet"], plan=context["plan"],
        catalog=context["catalog"], lane_name="review", cwd=context["cwd"])


def _activation_material(context, *, claim_id="a" * 32, prompt="review this"):
    profile = Path(context["cwd"]) / ".claude-activation-profile"
    profile.mkdir(exist_ok=True)
    invocation = AgentInvocation(
        cli="claude", prompt=prompt, cwd=context["cwd"],
        permission="read-only", transport="subprocess", model="frontier-alpha",
        model_source="agent", model_exact_required=True,
        model_exact_source="agent", output_contract="report",
        profile_env={"CLAUDE_CONFIG_DIR": str(profile)})
    definition_sha = _sha("definition")
    request_sha = _sha("fresh request")
    payload = _evidence.verify(context["plan"])
    lane = next(item for item in payload["lanes"] if item["name"] == "review")
    approved_bindings = _fleet_approval._bindings(
        context["fleet"], context["plan"], lane)
    bindings = {
        "approval_id": context["approval_id"],
        **{key: value for key, value in approved_bindings.items() if key != "lane"},
        "prompt_sha256": _fleet_activation._sha(prompt),
        "request_identity_sha256": request_sha,
    }
    candidate = _fleet_activation.freeze_activation_candidate(
        bindings=bindings, claim_id=claim_id, seat="alpha",
        agent_definition_sha256=definition_sha, invocation=invocation)
    return invocation, candidate, definition_sha, request_sha


def _reserve_activation(context, **changes):
    invocation, candidate, definition_sha, request_sha = _activation_material(context)
    values = {
        "invocation": invocation, "activation_candidate": candidate,
        "agent_definition_sha256": definition_sha,
        "current_request_identity_sha256": request_sha,
    }
    values.update(changes)
    return _fleet_dispatch.reserve_activation_dispatch(
        approval_id=context["approval_id"], fleet=context["fleet"],
        plan=context["plan"], catalog=context["catalog"], lane_name="review",
        cwd=context["cwd"], data_boundary={
            "boundary": "local_sanitized", "proof": "operator_attested",
            "evidence_sha256": _fleet_activation._sha(values["invocation"].prompt)},
        **values)


def _launch_evidence(invocation, **changes):
    values = {
        "schema": _fleet_dispatch.LAUNCH_EVIDENCE_SCHEMA,
        "backend": invocation.cli,
        "transport": invocation.transport,
        "command_sha256": _sha("claude"),
        "argv_sha256": _sha("frozen argv"),
        "cwd_sha256": _fleet_dispatch._digest(os.path.normcase(os.path.realpath(
            invocation.cwd))),
        "env_names_sha256": _sha("CLAUDE_CONFIG_DIR"),
        "env_sha256": _sha("CLAUDE_CONFIG_DIR=private"),
    }
    values.update(changes)
    return values


def _claim_activation(context, reservation, **changes):
    invocation, candidate, definition_sha, request_sha = _activation_material(context)
    values = {
        "invocation": invocation,
        "activation_candidate": candidate,
        "agent_definition_sha256": definition_sha,
        "current_request_identity_sha256": request_sha,
    }
    values.update(changes)
    return _fleet_dispatch.claim_activation_provider_launch(
        reservation, fleet=context["fleet"], plan=context["plan"],
        catalog=context["catalog"], lane_name="review", cwd=context["cwd"],
        data_boundary={
            "boundary": "local_sanitized", "proof": "operator_attested",
            "evidence_sha256": _fleet_activation._sha(values["invocation"].prompt)},
        launch_evidence=values.pop("launch_evidence", _launch_evidence(values["invocation"])),
        **values)


def test_activation_reservation_is_private_provider_inert_and_revalidated(
        approved, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "credential-one")
    reservation = _reserve_activation(approved)
    claim = _fleet_dispatch.get_claim(reservation)
    assert claim["activation"]["private_binding_hmac"]
    assert claim["phase"] == "reserved"
    invocation, candidate, definition_sha, request_sha = _activation_material(approved)
    preflight = _fleet_dispatch.preflight_activation_dispatch(
        reservation, fleet=approved["fleet"], plan=approved["plan"],
        catalog=approved["catalog"], lane_name="review", cwd=approved["cwd"],
        data_boundary={"boundary": "local_sanitized", "proof": "operator_attested",
                       "evidence_sha256": _fleet_activation._sha(invocation.prompt)},
        invocation=invocation, activation_candidate=candidate,
        agent_definition_sha256=definition_sha,
        current_request_identity_sha256=request_sha)
    assert preflight["ready"] is True
    assert preflight["provider_contacted"] is False
    receipt = _fleet_dispatch.public_receipt(reservation)
    payload = _fleet_dispatch.verify_public_receipt(receipt)
    assert payload["activation"] == {
        "contract_sha256": candidate["sha256"],
        "billing_class": "subscription",
        "attempt_policy": _fleet_dispatch._activation_attempt_policy(),
    }
    public = json.dumps(receipt, sort_keys=True)
    assert "private_binding_hmac" not in public
    assert "credential-one" not in public
    assert definition_sha not in public
    assert request_sha not in public


def test_activation_launch_claim_is_atomic_private_and_consumes_once(approved):
    reservation = _reserve_activation(approved)
    claimed = _claim_activation(approved, reservation)
    assert claimed["phase"] == "provider_launch_claimed"
    assert claimed["provider_contacted"] is None
    assert claimed["contact_slot_consumed"] is True
    assert claimed["activation"]["final_launch_sha256"] == _fleet_dispatch._digest(
        _launch_evidence(_activation_material(approved)[0]))
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as duplicate:
        _claim_activation(approved, reservation)
    assert duplicate.value.kind == "fleet_dispatch_cas_conflict"
    public = json.dumps(_fleet_dispatch.public_receipt(reservation), sort_keys=True)
    for private_value in (
            "final_launch_sha256", "frozen argv", "CLAUDE_CONFIG_DIR",
            _sha("frozen argv"), _sha("CLAUDE_CONFIG_DIR")):
        assert private_value not in public


@pytest.mark.parametrize("field,value", [
    ("schema", "wrong.schema/v1"),
    ("backend", "codex"),
    ("transport", "acp"),
    ("argv_sha256", "not-a-digest"),
    ("cwd_sha256", _sha("different-cwd")),
])
def test_activation_launch_evidence_drift_refuses_before_capacity_consumption(
        approved, field, value):
    reservation = _reserve_activation(approved)
    invocation = _activation_material(approved)[0]
    evidence = _launch_evidence(invocation, **{field: value})
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        _claim_activation(approved, reservation, launch_evidence=evidence)
    assert refused.value.kind == "fleet_activation_launch_evidence_invalid"
    claim = _fleet_dispatch.get_claim(reservation)
    assert claim["phase"] == "reserved"
    assert claim["contact_slot_consumed"] is False
    assert claim["activation"].get("final_launch_sha256") is None


def test_activation_launch_claim_revalidates_private_hmac_and_route_before_cas(
        approved, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "credential-one")
    reservation = _reserve_activation(approved)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "credential-two")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as changed:
        _claim_activation(approved, reservation)
    assert changed.value.kind == "fleet_activation_context_changed"
    claim = _fleet_dispatch.get_claim(reservation)
    assert claim["phase"] == "reserved"
    assert claim["contact_slot_consumed"] is False


def test_activation_launch_claim_revalidates_route_and_billing_before_cas(
        approved, monkeypatch):
    reservation = _reserve_activation(approved)
    changed_catalog = json.loads(json.dumps(approved["catalog"]))
    changed_catalog[0]["model"] = "different-model"
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as route_changed:
        _fleet_dispatch.claim_activation_provider_launch(
            reservation, fleet=approved["fleet"], plan=approved["plan"],
            catalog=changed_catalog, lane_name="review", cwd=approved["cwd"],
            data_boundary={
                "boundary": "local_sanitized", "proof": "operator_attested",
                "evidence_sha256": _fleet_activation._sha("review this")},
            invocation=_activation_material(approved)[0],
            activation_candidate=_activation_material(approved)[1],
            agent_definition_sha256=_activation_material(approved)[2],
            current_request_identity_sha256=_activation_material(approved)[3],
            launch_evidence=_launch_evidence(_activation_material(approved)[0]))
    assert route_changed.value.kind == "fleet_dispatch_catalog_mismatch"
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "reserved"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-credential")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as billing_changed:
        _claim_activation(approved, reservation)
    assert billing_changed.value.kind == "fleet_dispatch_no_eligible_route"
    assert _fleet_dispatch.get_claim(reservation)["contact_slot_consumed"] is False


def test_concurrent_activation_launch_claim_has_one_capacity_consumer(approved):
    reservation = _reserve_activation(approved)

    def claim(_index):
        try:
            return _claim_activation(approved, reservation)["phase"]
        except _fleet_dispatch.FleetDispatchError as exc:
            return exc.kind

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(claim, range(8)))
    assert outcomes.count("provider_launch_claimed") == 1
    assert outcomes.count("fleet_dispatch_cas_conflict") == 7
    claim_state = _fleet_dispatch.get_claim(reservation)
    assert claim_state["contact_slot_consumed"] is True
    assert claim_state["activation"]["final_launch_sha256"]


def test_activation_same_class_credential_rotation_fails_preflight(approved, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "credential-one")
    reservation = _reserve_activation(approved)
    invocation, candidate, definition_sha, request_sha = _activation_material(approved)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "credential-two")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="private reservation"):
        _fleet_dispatch.preflight_activation_dispatch(
            reservation, fleet=approved["fleet"], plan=approved["plan"],
            catalog=approved["catalog"], lane_name="review", cwd=approved["cwd"],
            data_boundary={"boundary": "local_sanitized", "proof": "operator_attested",
                           "evidence_sha256": _fleet_activation._sha(invocation.prompt)},
            invocation=invocation, activation_candidate=candidate,
            agent_definition_sha256=definition_sha,
            current_request_identity_sha256=request_sha)


def test_activation_profile_content_rotation_fails_preflight(approved, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    profile = Path(approved["cwd"]) / ".claude-activation-profile"
    profile.mkdir(exist_ok=True)
    settings = profile / "settings.json"
    settings.write_text('{"account":"first"}', encoding="utf-8")
    reservation = _reserve_activation(approved)
    invocation, candidate, definition_sha, request_sha = _activation_material(approved)
    settings.write_text('{"account":"second"}', encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="private reservation"):
        _fleet_dispatch.preflight_activation_dispatch(
            reservation, fleet=approved["fleet"], plan=approved["plan"],
            catalog=approved["catalog"], lane_name="review", cwd=approved["cwd"],
            data_boundary={"boundary": "local_sanitized", "proof": "operator_attested",
                           "evidence_sha256": _fleet_activation._sha(invocation.prompt)},
            invocation=invocation, activation_candidate=candidate,
            agent_definition_sha256=definition_sha,
            current_request_identity_sha256=request_sha)


def test_activation_unknown_billing_and_stale_identity_refuse(approved, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as unknown:
        _reserve_activation(approved)
    assert unknown.value.kind == "fleet_activation_billing_unknown"
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    invocation, candidate, definition_sha, request_sha = _activation_material(approved)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as stale:
        _reserve_activation(
            approved, invocation=invocation, activation_candidate=candidate,
            agent_definition_sha256=_sha("changed"),
            current_request_identity_sha256=request_sha)
    assert stale.value.kind == "fleet_activation_binding_changed"


def test_activation_duplicate_is_idempotent_and_private_change_conflicts(
        approved, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "same")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(lambda _item: _reserve_activation(approved), range(8)))
    first = reservations[0]
    assert all(item == first for item in reservations)
    monkeypatch.setenv("ANTHROPIC_PRIVATE_TEST", "changed")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as conflict:
        _reserve_activation(approved)
    assert conflict.value.kind == "fleet_dispatch_request_conflict"


def test_module_is_provider_inert_and_has_no_launch_imports():
    tree = ast.parse(Path(_fleet_dispatch.__file__).read_text(encoding="utf-8"))
    imports = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                calls.add(node.func.id)
    assert not imports.intersection({
        "_executor", "_background", "_resolver", "_apibackend", "requests",
        "urllib", "httpx", "socket",
    })
    assert not calls.intersection({"Popen", "run", "call", "check_call", "check_output"})


def test_reservation_is_authenticated_deterministic_and_publicly_redacted(
        approved, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process called"))
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: pytest.fail("network called"))
    reservation = _reserve(approved)
    claim = _fleet_dispatch.get_claim(reservation)
    assert claim["phase"] == "reserved"
    assert claim["provider_contacted"] is False
    assert claim["request"]["bindings"]["fleet_sha256"] == approved["fleet"]["sha256"]
    assert claim["decision"]["schema"] == "summon.decision/v2"
    assert claim["route"]["seat"] == "alpha"

    receipt = _fleet_dispatch.public_receipt(reservation)
    payload = _fleet_dispatch.verify_public_receipt(receipt)
    assert payload["authorization"] == "evidence_only"
    assert payload["claim"]["phase"] == "reserved"
    assert payload["claim"]["provider_contacted"] is False
    public = json.dumps(receipt, sort_keys=True)
    private = Path(_fleet_dispatch.ledger_path(approved["approval_id"])).read_text(
        encoding="utf-8")
    for forbidden in (
            str(approved["root"]), "store_id", "approval_sha256", "request_sha256",
            "prompt_sha256", "evidence_sha256", '"mac"', "actor", "store.key",
            "review this"):
        assert forbidden not in public
    assert "review this" not in private


def test_exact_binding_catalog_billing_and_data_boundary_fail_closed(approved):
    changed_catalog = json.loads(json.dumps(approved["catalog"]))
    changed_catalog[0]["model"] = "other-model"
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="catalog"):
        _fleet_dispatch.reserve_dispatch(
            approval_id=approved["approval_id"], fleet=approved["fleet"],
            plan=approved["plan"], catalog=changed_catalog, lane_name="review",
            cwd=approved["cwd"],
            prompt_sha256=_sha("x"), billing_by_seat={"alpha": "subscription"},
            data_boundary={"boundary": "local_sanitized",
                           "proof": "operator_attested",
                           "evidence_sha256": _sha("x")})
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as billing:
        _reserve(approved, billing={"alpha": "payg"})
    assert billing.value.kind == "fleet_dispatch_no_eligible_route"
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="exact prompt"):
        _reserve(approved, boundary={
            "boundary": "local_sanitized", "proof": "operator_attested",
            "evidence_sha256": _sha("different")})
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="mismatched"):
        _reserve(approved, boundary={
            "boundary": "public", "proof": "public_prompt_verified",
            "evidence_sha256": _sha("review this")})


def test_typed_billing_can_make_priority_candidate_ineligible(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    fleet, plan, catalog = _fleet_material(
        tmp_path, contacts=2, billable=1, parallel=2, two=True)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    context = {"root": root, "fleet": fleet, "plan": plan, "catalog": catalog,
               "approval_id": receipt["approval"]["approval_id"], "cwd": str(tmp_path)}
    reservation = _reserve(
        context, billing={"alpha": "payg", "beta": "subscription"})
    assert reservation.route["seat"] == "beta"
    candidates = _evidence.verify(reservation.decision)["candidates"]
    alpha = next(item for item in candidates if item["seat"] == "alpha")
    assert alpha["eligible"] is False
    assert "billing_class_not_authorized" in alpha["losing_rules"]


def test_forged_effective_permission_and_enforcement_cannot_bypass_ceiling(
        tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    fleet, _plan, catalog = _fleet_material(tmp_path)
    forged = json.loads(json.dumps(catalog))
    forged[0]["effective_permission"] = "yolo"
    forged[0]["enforcement"] = "enforced"
    digest = _fleet_compile.catalog_digest(forged)
    plan = _fleet_compile.compile_fleet(
        fleet=fleet, catalog=forged,
        project_sha256=_fleet.project_digest(str(tmp_path)),
        catalog_sha256=digest)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    context = {"root": root, "fleet": fleet, "plan": plan, "catalog": forged,
               "approval_id": receipt["approval"]["approval_id"], "cwd": str(tmp_path)}
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        _reserve(context)
    assert refused.value.kind == "fleet_dispatch_no_eligible_route"


@pytest.mark.parametrize("declared", ["public", "private_local"])
def test_public_proof_is_prompt_bound_and_private_local_is_not_implemented(
        tmp_path, monkeypatch, declared):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    fleet, plan, catalog = _fleet_material(tmp_path, data_boundary=declared)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    context = {"root": root, "fleet": fleet, "plan": plan, "catalog": catalog,
               "approval_id": receipt["approval"]["approval_id"], "cwd": str(tmp_path)}
    proof = "public_prompt_verified" if declared == "public" else "local_backend_verified"
    boundary = {"boundary": declared, "proof": proof,
                "evidence_sha256": _sha("different")}
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as refused:
        _reserve(context, boundary=boundary)
    assert refused.value.kind == "fleet_dispatch_data_boundary_unsatisfied"
    if declared == "public":
        boundary["evidence_sha256"] = _sha("review this")
        assert _reserve(context, boundary=boundary).route["seat"] == "alpha"
    else:
        boundary["evidence_sha256"] = _sha("review this")
        with pytest.raises(_fleet_dispatch.FleetDispatchError) as unsupported:
            _reserve(context, boundary=boundary)
        assert unsupported.value.kind == "fleet_dispatch_data_boundary_unsatisfied"


def test_idempotency_semantic_deduplication_and_request_conflict(approved):
    first = _reserve(approved, request_id="1" * 32)
    same = _reserve(approved, request_id="1" * 32)
    semantic_duplicate = _reserve(approved, request_id="2" * 32)
    assert same.claim_id == first.claim_id
    assert semantic_duplicate.claim_id == first.claim_id
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as caught:
        _reserve(approved, prompt="different", request_id="1" * 32)
    assert caught.value.kind == "fleet_dispatch_request_conflict"


def test_launch_claim_consumes_slot_permanently_even_when_spawn_fails(approved):
    reservation = _reserve(approved)
    claimed = _claim(approved, reservation)
    assert claimed["phase"] == "provider_launch_claimed"
    assert claimed["contact_slot_consumed"] is True
    assert claimed["provider_contacted"] is None
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as duplicate:
        _claim(approved, reservation)
    assert duplicate.value.kind == "fleet_dispatch_cas_conflict"
    failed = _fleet_dispatch.mark_spawn_failed(reservation)
    assert failed["provider_contacted"] is False
    assert failed["contact_slot_consumed"] is True
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as exhausted:
        _reserve(approved, prompt="another")
    assert exhausted.value.kind == "fleet_dispatch_contact_ceiling"
    terminal = _fleet_dispatch.mark_terminal(
        reservation, expected_phase="spawn_failed",
        terminal_sha256=_sha("failure"), provider_contacted=False)
    assert terminal["phase"] == "terminal"


def test_launch_claim_revalidates_live_catalog_before_consuming_capacity(approved):
    reservation = _reserve(approved)
    changed = json.loads(json.dumps(approved["catalog"]))
    changed[0]["model"] = "drifted-model"
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as drift:
        _fleet_dispatch.claim_provider_launch(
            reservation, fleet=approved["fleet"], plan=approved["plan"],
            catalog=changed, lane_name="review", cwd=approved["cwd"])
    assert drift.value.kind == "fleet_dispatch_catalog_mismatch"
    claim = _fleet_dispatch.get_claim(reservation)
    assert claim["phase"] == "reserved"
    assert claim["contact_slot_consumed"] is False
    assert _claim(approved, reservation)["phase"] == "provider_launch_claimed"


def test_cancel_releases_reserved_capacity_but_cannot_race_a_launch(approved):
    reservation = _reserve(approved)
    cancelled = _fleet_dispatch.cancel_pre_spawn(reservation)
    assert cancelled["phase"] == "cancelled_pre_spawn"
    assert cancelled["contact_slot_reserved"] is False
    replacement = _reserve(approved, prompt="replacement")
    assert replacement.claim_id != reservation.claim_id
    with pytest.raises(_fleet_dispatch.FleetDispatchError):
        _claim(approved, reservation)


def test_spawn_reap_terminal_and_post_spawn_indeterminate_are_honest(approved):
    reservation = _reserve(approved)
    _claim(approved, reservation)
    spawned = _fleet_dispatch.mark_spawned(reservation)
    assert spawned["provider_contacted"] is True
    indeterminate = _fleet_dispatch.mark_indeterminate(
        reservation, expected_phase="spawned", provider_contacted=True)
    assert indeterminate["phase"] == "indeterminate"
    assert indeterminate["provider_contacted"] is True
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="reconciliation"):
        _fleet_dispatch.mark_terminal(
            reservation, expected_phase="indeterminate",
            terminal_sha256=_sha("reconciled"), provider_contacted=True)
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "indeterminate"


def test_reaped_lifecycle_and_invalid_contact_rewrite(approved):
    reservation = _reserve(approved)
    _claim(approved, reservation)
    _fleet_dispatch.mark_spawned(reservation)
    reaped = _fleet_dispatch.mark_reaped(reservation)
    assert reaped["phase"] == "reaped"
    with pytest.raises(_fleet_dispatch.FleetDispatchError):
        _fleet_dispatch.mark_terminal(
            reservation, expected_phase="reaped",
            terminal_sha256=_sha("done"), provider_contacted=False)
    terminal = _fleet_dispatch.mark_terminal(
        reservation, expected_phase="reaped",
        terminal_sha256=_sha("done"), provider_contacted=True)
    assert terminal["provider_contacted"] is True


def test_concurrent_duplicate_reservation_has_one_claim(approved):
    def reserve(_index):
        return _reserve(approved)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(16)))
    assert len({item.claim_id for item in results}) == 1
    value = json.loads(Path(
        _fleet_dispatch.ledger_path(approved["approval_id"])).read_text(encoding="utf-8"))
    assert len(value["claims"]) == 1


def test_cancel_vs_launch_cas_has_exactly_one_winner(approved):
    reservation = _reserve(approved)

    def transition(which):
        try:
            if which == "cancel":
                return "cancel", _fleet_dispatch.cancel_pre_spawn(reservation)["phase"]
            return "launch", _claim(approved, reservation)["phase"]
        except _fleet_dispatch.FleetDispatchError as exc:
            return which, exc.kind

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transition, ("cancel", "launch")))
    successes = [item for item in outcomes if item[1] in {
        "cancelled_pre_spawn", "provider_launch_claimed"}]
    conflicts = [item for item in outcomes if item[1] == "fleet_dispatch_cas_conflict"]
    assert len(successes) == len(conflicts) == 1


def test_parallel_and_billable_reservation_ceilings(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    fleet, plan, catalog = _fleet_material(
        tmp_path, contacts=2, billable=1, parallel=1, credit=True)
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    context = {"root": root, "fleet": fleet, "plan": plan, "catalog": catalog,
               "approval_id": receipt["approval"]["approval_id"], "cwd": str(tmp_path)}
    first = _reserve(context, billing={"alpha": "subscription"})
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as parallel:
        _reserve(context, prompt="second", billing={"alpha": "subscription"})
    assert parallel.value.kind == "fleet_dispatch_parallel_ceiling"
    _fleet_dispatch.cancel_pre_spawn(first)
    billable_first = _reserve(context, billing={"alpha": "credit"})
    _claim(context, billable_first)
    _fleet_dispatch.mark_spawn_failed(billable_first)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as billable:
        _reserve(context, prompt="second", billing={"alpha": "credit"})
    assert billable.value.kind == "fleet_dispatch_billable_ceiling"


def test_revocation_wins_before_launch_but_lifecycle_can_still_be_recorded(approved):
    reservation = _reserve(approved)
    status = _fleet_approval.status()
    _fleet_approval.revoke(
        approved["approval_id"], expected_generation=status["generation"])
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as inactive:
        _claim(approved, reservation)
    assert inactive.value.kind == "fleet_dispatch_approval_inactive"
    cancelled = _fleet_dispatch.cancel_pre_spawn(reservation)
    assert cancelled["phase"] == "cancelled_pre_spawn"


def test_revocation_after_launch_does_not_hide_known_lifecycle(approved):
    reservation = _reserve(approved)
    _claim(approved, reservation)
    status = _fleet_approval.status()
    _fleet_approval.revoke(
        approved["approval_id"], expected_generation=status["generation"])
    assert _fleet_dispatch.mark_spawned(reservation)["provider_contacted"] is True


def test_authenticated_ledger_rejects_forgery_duplicate_nan_and_oversize(approved):
    reservation = _reserve(approved)
    path = Path(_fleet_dispatch.ledger_path(approved["approval_id"]))
    original = path.read_bytes()
    value = json.loads(original)
    value["claims"][reservation.claim_id]["phase"] = "spawned"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="authentication"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(original)
    duplicate = original.decode("utf-8").rstrip()[:-1] + ',"generation":1}'
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="duplicate"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(original)
    nan = original.decode("utf-8").replace('"generation":1', '"generation":NaN', 1)
    path.write_text(nan, encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="non-finite"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(b"x" * (_fleet_dispatch.MAX_LEDGER_BYTES + 1))
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="oversized"):
        _fleet_dispatch.get_claim(reservation)


def test_saved_valid_ledger_snapshot_cannot_reopen_launch_claim(approved):
    reservation = _reserve(approved)
    path = Path(_fleet_dispatch.ledger_path(approved["approval_id"]))
    saved_reserved = path.read_bytes()
    _claim(approved, reservation)
    claimed = path.read_bytes()
    assert claimed != saved_reserved

    path.write_bytes(saved_reserved)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as replay:
        _fleet_dispatch.get_claim(reservation)
    assert replay.value.kind == "fleet_dispatch_ledger_replay"
    assert "replay" in str(replay.value)


def test_anchor_recovers_crash_before_ledger_replace(approved, monkeypatch):
    original_promote = _fleet_dispatch._promote_staged_ledger
    calls = {"count": 0}

    def fail_once(staged, path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated pre-replace failure")
        return original_promote(staged, path)

    monkeypatch.setattr(_fleet_dispatch, "_promote_staged_ledger", fail_once)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as failed:
        _reserve(approved)
    assert failed.value.kind == "fleet_dispatch_io_failed"
    reservation = _reserve(approved)
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "reserved"
    anchor = json.loads(Path(
        _fleet_dispatch.anchor_path(approved["approval_id"])).read_text(encoding="utf-8"))
    assert anchor["state"] == "clean"
    assert anchor["generation"] == 1


def test_anchor_detects_authenticated_ledger_deletion(approved):
    reservation = _reserve(approved)
    assert Path(_fleet_dispatch.anchor_root()) != Path(_fleet_dispatch.ledger_root())
    Path(_fleet_dispatch.ledger_path(approved["approval_id"])).unlink()
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as deleted:
        _fleet_dispatch.get_claim(reservation)
    assert deleted.value.kind == "fleet_dispatch_ledger_replay"


def test_separate_anchor_detects_whole_ledger_directory_deletion(approved):
    reservation = _reserve(approved)
    anchor = Path(_fleet_dispatch.anchor_path(approved["approval_id"]))
    assert anchor.exists()
    ledger_root = Path(_fleet_dispatch.ledger_root()).resolve()
    assert ledger_root.parent == Path(approved["root"]).resolve()
    shutil.rmtree(ledger_root)
    assert anchor.exists()
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as deleted:
        _fleet_dispatch.get_claim(reservation)
    assert deleted.value.kind == "fleet_dispatch_ledger_replay"


def test_durable_initialization_marker_detects_both_dispatch_roots_deleted(approved):
    reservation = _reserve_activation(approved)
    marker = Path(_fleet_dispatch.initialization_path(approved["approval_id"]))
    assert marker.exists()
    assert marker.parent not in {
        Path(_fleet_dispatch.ledger_root()), Path(_fleet_dispatch.anchor_root())}
    shutil.rmtree(_fleet_dispatch.ledger_root())
    shutil.rmtree(_fleet_dispatch.anchor_root())
    assert marker.exists()
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as deleted:
        _fleet_dispatch.get_claim(reservation)
    assert deleted.value.kind == "fleet_dispatch_ledger_replay"
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as reset:
        _reserve_activation(approved)
    assert reset.value.kind == "fleet_dispatch_ledger_replay"


def test_legacy_empty_anchor_is_marked_before_first_ledger_and_cannot_reset(approved):
    approval_id = approved["approval_id"]
    with _fleet_approval._store_lock():
        key = _fleet_approval._load_key(create=False)
        store = _fleet_approval._read_store(key, _fleet_approval._now())
        _fleet_dispatch._reconcile_dispatch_anchor(
            store, key, approval_id, None, ledger_retired=False)
    marker = Path(_fleet_dispatch.initialization_path(approval_id))
    assert Path(_fleet_dispatch.anchor_path(approval_id)).exists()
    assert not marker.exists()

    _reserve_activation(approved)
    assert marker.exists()
    shutil.rmtree(_fleet_dispatch.ledger_root())
    shutil.rmtree(_fleet_dispatch.anchor_root())
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as reset:
        _reserve_activation(approved)
    assert reset.value.kind == "fleet_dispatch_ledger_replay"


def test_initialization_marker_is_authenticated(approved):
    reservation = _reserve(approved)
    path = Path(_fleet_dispatch.initialization_path(approved["approval_id"]))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["approval_generation"] += 1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as tampered:
        _fleet_dispatch.get_claim(reservation)
    assert tampered.value.kind == "fleet_dispatch_ledger_replay"


def test_anchor_recovers_crash_after_ledger_replace(approved, monkeypatch):
    original_commit = _fleet_dispatch._commit_dispatch_anchor
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _evidence.EvidenceError("simulated post-replace failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(_fleet_dispatch, "_commit_dispatch_anchor", fail_once)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as failed:
        _reserve(approved)
    assert failed.value.kind == "fleet_dispatch_anchor_failed"
    reservation = _reserve(approved)
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "reserved"
    anchor = json.loads(Path(
        _fleet_dispatch.anchor_path(approved["approval_id"])).read_text(encoding="utf-8"))
    assert anchor["state"] == "clean"
    assert anchor["generation"] == 1


def test_anchor_recovers_ambiguous_prepare_write(approved, monkeypatch):
    original_prepare = _fleet_dispatch._prepare_dispatch_anchor
    calls = {"count": 0}

    def commit_then_fail(*args, **kwargs):
        generation = original_prepare(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise _evidence.EvidenceError("simulated ambiguous store write")
        return generation

    monkeypatch.setattr(
        _fleet_dispatch, "_prepare_dispatch_anchor", commit_then_fail)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as failed:
        _reserve(approved)
    assert failed.value.kind == "fleet_dispatch_anchor_failed"
    pending = Path(_fleet_dispatch.ledger_path(approved["approval_id"]) + ".pending")
    assert pending.exists()
    reservation = _reserve(approved)
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "reserved"
    assert not pending.exists()


def test_v1_approval_store_bytes_do_not_change_when_anchor_is_written(approved):
    store_path = Path(_fleet_approval.store_path())
    before = store_path.read_bytes()
    assert _fleet_approval.status()["generation"] == 1
    reservation = _reserve(approved)
    assert _fleet_dispatch.get_claim(reservation)["phase"] == "reserved"
    assert store_path.read_bytes() == before
    value = json.loads(before)
    assert value["schema"] == "summon.fleet-approval-store/v1"
    assert set(value) == {
        "schema", "store_id", "generation", "last_seen_at",
        "approvals", "revocations", "mac"}
    key = Path(_fleet_approval.key_path()).read_bytes()
    assert _fleet_approval._validate_store(
        value, key, _fleet_approval._now()) == value
    assert Path(_fleet_dispatch.anchor_path(approved["approval_id"])).exists()


def test_ordinary_approve_revoke_remains_v1_reader_compatible(approved):
    status = _fleet_approval.status()
    _fleet_approval.revoke(
        approved["approval_id"], expected_generation=status["generation"])
    store_path = Path(_fleet_approval.store_path())
    key = Path(_fleet_approval.key_path()).read_bytes()
    revoked = json.loads(store_path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema", "store_id", "generation", "last_seen_at",
        "approvals", "revocations", "mac"}
    assert revoked["schema"] == "summon.fleet-approval-store/v1"
    assert set(revoked) == expected_fields
    assert _fleet_approval._validate_store(
        revoked, key, _fleet_approval._now()) == revoked

    _fleet_approval.approve(
        fleet=approved["fleet"], plan=approved["plan"], lane_name="review",
        expires_in_seconds=3600, expected_generation=revoked["generation"])
    reissued = json.loads(store_path.read_text(encoding="utf-8"))
    assert reissued["schema"] == "summon.fleet-approval-store/v1"
    assert set(reissued) == expected_fields
    assert _fleet_approval._validate_store(
        reissued, key, _fleet_approval._now()) == reissued


def test_deleted_anchor_cannot_be_recreated_from_existing_ledger(approved):
    reservation = _reserve(approved)
    Path(_fleet_dispatch.anchor_path(approved["approval_id"])).unlink()
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as replay:
        _fleet_dispatch.get_claim(reservation)
    assert replay.value.kind == "fleet_dispatch_ledger_replay"


def test_malformed_and_replayed_anchor_fail_closed(approved):
    reservation = _reserve(approved)
    anchor_path = Path(_fleet_dispatch.anchor_path(approved["approval_id"]))
    saved = anchor_path.read_bytes()
    value = json.loads(saved)
    value["generation"] = "not-an-integer"
    anchor_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as malformed:
        _fleet_dispatch.get_claim(reservation)
    assert malformed.value.kind == "fleet_dispatch_ledger_replay"

    anchor_path.write_bytes(saved)
    _claim(approved, reservation)
    anchor_path.write_bytes(saved)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as replayed:
        _fleet_dispatch.get_claim(reservation)
    assert replayed.value.kind == "fleet_dispatch_ledger_replay"


def test_anchor_rejects_duplicate_nan_oversize_and_hardlink(approved):
    reservation = _reserve(approved)
    path = Path(_fleet_dispatch.anchor_path(approved["approval_id"]))
    original = path.read_bytes()
    duplicate = original.decode("utf-8").rstrip()[:-1] + ',"generation":1}'
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="duplicate"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(original)
    nan = original.decode("utf-8").replace('"generation":1', '"generation":NaN', 1)
    path.write_text(nan, encoding="utf-8")
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="non-finite"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(b"x" * (_fleet_dispatch.MAX_ANCHOR_BYTES + 1))
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="oversized"):
        _fleet_dispatch.get_claim(reservation)

    path.write_bytes(original)
    link = path.with_name("linked-anchor.json")
    try:
        os.link(path, link)
    except OSError:
        return
    try:
        with pytest.raises(_fleet_dispatch.FleetDispatchError, match="single-link"):
            _fleet_dispatch.get_claim(reservation)
    finally:
        link.unlink()


def test_ledger_hardlink_and_private_mode_fail_closed(approved):
    reservation = _reserve(approved)
    path = Path(_fleet_dispatch.ledger_path(approved["approval_id"]))
    link = path.with_name("linked.json")
    try:
        os.link(path, link)
    except OSError:
        pytest.skip("hard links unavailable")
    try:
        with pytest.raises(_evidence.EvidenceError, match="single-link"):
            _fleet_dispatch.get_claim(reservation)
    finally:
        link.unlink()
    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(_evidence.EvidenceError, match="permissions"):
            _fleet_dispatch.get_claim(reservation)


def test_launch_claim_is_crash_stable_and_blocks_retry(approved):
    reservation = _reserve(approved)
    _claim(approved, reservation)
    # Simulate a process disappearing between the durable CAS and provider Popen.
    reconstructed = _fleet_dispatch.Reservation(
        approval_id=reservation.approval_id, claim_id=reservation.claim_id,
        request_id=reservation.request_id, request_sha256=reservation.request_sha256,
        decision=json.loads(json.dumps(reservation.decision)),
        route=json.loads(json.dumps(reservation.route)))
    claim = _fleet_dispatch.get_claim(reconstructed)
    assert claim["phase"] == "provider_launch_claimed"
    assert claim["provider_contacted"] is None
    assert claim["contact_slot_consumed"] is True
    with pytest.raises(_fleet_dispatch.FleetDispatchError):
        _claim(approved, reconstructed)


def test_public_receipt_forgery_is_detected_and_never_authoritative(approved):
    reservation = _reserve(approved)
    receipt = _fleet_dispatch.public_receipt(reservation)
    forged = json.loads(json.dumps(receipt))
    forged["claim"]["phase"] = "spawned"
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="digest"):
        _fleet_dispatch.verify_public_receipt(forged)
    resealed = json.loads(json.dumps(receipt))
    resealed["resolution"]["model"] = "C:\\private\\model"
    payload = {key: value for key, value in resealed.items()
               if key not in {"schema", "sha256"}}
    resealed["sha256"] = hashlib.sha256(
        _fleet_dispatch.PUBLIC_SCHEMA.encode("ascii") + b"\0"
        + _fleet_dispatch._canonical(payload)).hexdigest()
    with pytest.raises(_fleet_dispatch.FleetDispatchError, match="public text"):
        _fleet_dispatch.verify_public_receipt(resealed)
    # A valid public receipt has no API that can be converted back to Reservation.
    assert not hasattr(_fleet_dispatch, "reserve_from_public_receipt")
    assert not hasattr(_fleet_dispatch, "claim_from_public_receipt")


@pytest.mark.parametrize("mutation", ["cyclic", "deep", "oversize"])
def test_public_receipt_is_bounded_before_digesting(approved, mutation):
    receipt = _fleet_dispatch.public_receipt(_reserve(approved))
    if mutation == "cyclic":
        receipt["resolution"]["model"] = receipt
    elif mutation == "deep":
        nested = "model"
        for _ in range(100):
            nested = [nested]
        receipt["resolution"]["model"] = nested
    else:
        receipt["resolution"]["model"] = "m" * (
            _fleet_dispatch.MAX_PUBLIC_RECEIPT_BYTES + 1)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as invalid:
        _fleet_dispatch.verify_public_receipt(receipt)
    assert invalid.value.kind == "fleet_dispatch_receipt_invalid"


def test_mutated_in_process_handle_cannot_retarget_route(approved):
    reservation = _reserve(approved)
    forged_route = dict(reservation.route)
    forged_route["model"] = "other-model"
    forged = _fleet_dispatch.Reservation(
        approval_id=reservation.approval_id, claim_id=reservation.claim_id,
        request_id=reservation.request_id, request_sha256=reservation.request_sha256,
        decision=reservation.decision, route=forged_route)
    with pytest.raises(_fleet_dispatch.FleetDispatchError) as caught:
        _claim(approved, forged)
    assert caught.value.kind == "fleet_dispatch_claim_untrusted"
