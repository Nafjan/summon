"""Provider-inert M3a fleet compiler and control-plane regression tests."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _cli
import _evidence
import _fleet
import _fleet_compile
import _loader


def _agents():
    return [
        {"name": "architect", "run_agent": "claude",
         "permission": "read-only", "model": "claude-opus-5",
         "source": "project"},
        {"name": "sol-review", "run_agent": "codex",
         "permission": "read-only", "model": "gpt-5.6-sol",
         "source": "project"},
        {"name": "kimi-worker", "run_agent": "kimi",
         "permission": "yolo", "model": "kimi-code/k3",
         "source": "project"},
    ]


def _proposal(tmp_path, **updates):
    kwargs = {
        "lane": "review",
        "seats": ["architect", "sol-review"],
        "agents": _agents(),
        "cwd": str(tmp_path),
        "permission_ceiling": "read-only",
    }
    kwargs.update(updates)
    return _fleet.proposal(**kwargs)


def test_fleet_subcommands_rewrite_to_distinct_provider_inert_mode():
    assert _cli.rewrite_subcommand([
        "fleet", "propose", "review", "--seats", "architect,sol-review",
        "--allow-subscription",
    ]) == ([
        "--fleet-action", "propose", "--fleet-lane", "review",
        "--fleet-seats", "architect,sol-review", "--fleet-allow-subscription",
    ], None)
    assert _cli.rewrite_subcommand(["fleet", "validate", "fleet.json"])[0] == [
        "--fleet-action", "validate", "--fleet-file", "fleet.json"]
    assert _cli.rewrite_subcommand([
        "fleet", "explain", "fleet.json", "review"])[0] == [
        "--fleet-action", "explain", "--fleet-file", "fleet.json",
        "--fleet-lane", "review"]


def test_pure_compiler_has_structural_import_and_output_barrier(tmp_path):
    source_path = Path(_fleet_compile.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".", 1)[0])
    assert imports <= {
        "__future__", "hashlib", "json", "re", "typing",
        "_backend_policy", "_evidence",
    }
    assert not imports.intersection({
        "subprocess", "socket", "urllib", "requests", "httpx", "_builder",
        "_executor", "_background", "_apibackend", "_resolver", "_loader",
    })

    _, fleet, plan = _proposal(tmp_path)
    payload = _evidence.verify(plan)
    forbidden = {
        "selected", "winner", "resolved_agent", "command", "argv", "env",
        "session", "credential", "executable",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert forbidden.isdisjoint(set(keys(payload)))
    assert payload["provider_contacted"] is False
    assert payload["authorization"] == "advisory_only"
    assert _fleet_compile.verify_plan_for_fleet(plan, fleet) == payload


def test_propose_validate_inspect_explain_are_deterministic_and_no_contact(tmp_path):
    report, fleet, plan = _proposal(tmp_path)
    repeated, repeated_fleet, repeated_plan = _proposal(tmp_path)
    assert report == repeated
    assert fleet == repeated_fleet
    assert plan == repeated_plan
    assert report["provider_contacted"] is False
    assert report["approval"] == {"state": "recording_available_not_activated"}

    catalog, _, unavailable = _fleet.catalog_snapshot(_agents())
    assert unavailable == []
    explained = _fleet.explain(
        fleet=fleet, plan=plan, catalog=catalog, lane_name="review")
    assert explained["provider_contacted"] is False
    assert explained["authorization"] == "advisory_only"
    assert explained["selection"] == {
        "seat": None,
        "status": "not_authorized",
        "reason": "approval_recorded_not_activated",
    }
    assert explained["authorization"] == "advisory_only"
    assert "approved dispatch authority" in explained["summary"]


def test_exact_agent_and_lane_names_cannot_collide(tmp_path):
    with pytest.raises(_evidence.EvidenceError, match="collide"):
        _proposal(tmp_path, lane="architect")


def test_unknown_seat_and_permission_widening_fail_during_compile_or_explain(tmp_path):
    fleet = _fleet_compile.build_draft(
        lane="review", seats=["missing-seat"], permission_ceiling="read-only")
    catalog, catalog_sha, _ = _fleet.catalog_snapshot(_agents())
    with pytest.raises(_evidence.EvidenceError, match="unknown roster seats"):
        _fleet_compile.compile_fleet(
            fleet=fleet, catalog=catalog,
            project_sha256=_fleet.project_digest(str(tmp_path)),
            catalog_sha256=catalog_sha)

    _, fleet, plan = _proposal(
        tmp_path, seats=["kimi-worker"], permission_ceiling="read-only")
    explained = _fleet.explain(
        fleet=fleet, plan=plan, catalog=catalog, lane_name="review")
    assert explained["selection"]["seat"] is None
    assert "permission_ceiling_exceeded" in explained["candidates"][0]["reasons"]


def test_fleet_schema_rejects_tampering_duplicate_keys_private_paths_and_secrets(tmp_path):
    _, fleet, plan = _proposal(tmp_path)
    tampered = json.loads(json.dumps(fleet))
    tampered["lanes"][0]["candidates"][0]["seat"] = "changed"
    with pytest.raises(_evidence.EvidenceError, match="digest mismatch"):
        _evidence.verify(tampered)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"summon.fleet/v1","schema":"summon.fleet/v1"}',
        encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="duplicate JSON field"):
        _fleet.load_fleet(str(duplicate))

    for forbidden in ("C:/private/seat", "token=abcdefghijklmnop"):
        bad = _evidence.verify(fleet)
        bad = json.loads(json.dumps(bad))
        bad["lanes"][0]["candidates"][0]["seat"] = forbidden
        with pytest.raises(_evidence.EvidenceError):
            _evidence.seal("summon.fleet/v1", bad)

    forged_plan = _evidence.verify(plan)
    forged_plan["authorization"] = "approved"
    with pytest.raises(_evidence.EvidenceError, match="cannot authorize"):
        _evidence.seal("summon.fleet-plan/v1", forged_plan)


def test_catalog_rejects_executable_and_private_metadata():
    base = _fleet.catalog_snapshot(_agents())[0][0]
    for mutation in (
        dict(base, command="provider --run"),
        dict(base, seat="C:/private/seat"),
        dict(base, model="token=abcdefghijklmnop"),
    ):
        with pytest.raises(_evidence.EvidenceError):
            _fleet_compile.validate_catalog([mutation])


def test_explicit_out_is_atomic_and_no_implicit_write(tmp_path):
    _, fleet, _ = _proposal(tmp_path)
    before = sorted(path.name for path in tmp_path.iterdir())
    assert before == []
    destination = tmp_path / "fleet.json"
    _fleet.write_json(str(destination), fleet)
    assert json.loads(destination.read_text(encoding="utf-8")) == fleet
    assert sorted(path.name for path in tmp_path.iterdir()) == ["fleet.json"]
    original = destination.read_bytes()
    with pytest.raises(_evidence.EvidenceError, match="already exists"):
        _fleet.write_json(str(destination), {"replacement": True})
    assert destination.read_bytes() == original


def test_explain_rejects_catalog_substitution_and_preserves_source(tmp_path):
    _, fleet, plan = _proposal(tmp_path, spend={
        "subscription": True, "credit": False, "payg": False,
        "max_provider_contacts": 1, "max_billable_attempts": 1,
        "max_parallel": 1,
    })
    catalog, _, _ = _fleet.catalog_snapshot(_agents())
    substituted = json.loads(json.dumps(catalog))
    substituted[0]["backend"] = "codex"
    substituted[0]["provider"] = "openai"
    substituted[0]["model"] = "gpt-5.6-luna"
    with pytest.raises(_evidence.EvidenceError, match="does not match"):
        _fleet.explain(
            fleet=fleet, plan=plan, catalog=substituted, lane_name="review")

    with pytest.raises(_evidence.EvidenceError, match="does not bind"):
        _fleet_compile.compile_fleet(
            fleet=fleet,
            catalog=catalog,
            project_sha256=_fleet.project_digest(str(tmp_path)),
            catalog_sha256=_fleet_compile.catalog_digest(substituted),
        )

    explained = _fleet.explain(
        fleet=fleet, plan=plan, catalog=catalog, lane_name="review")
    assert explained["candidates"][0]["source"] == "project"
    assert explained["candidates"][0]["dispatch_eligible"] is None


def test_shared_backend_policy_matches_builder_for_every_tier():
    import _backend_policy
    import _builder

    for backend in sorted(_backend_policy.KNOWN_BACKENDS | {"future-cli"}):
        for permission in _backend_policy.PERMISSION_ORDER:
            assert (_backend_policy.permission_enforcement(backend, permission)
                    == _builder.permission_enforcement(backend, permission))
    assert _backend_policy.permission_enforcement("future-cli", "yolo") == "unknown"
    assert _backend_policy.permission_enforcement("opencode", "read-only") == "enforced"
    assert _backend_policy.permission_enforcement("arkcli", "read-only") == "enforced"
    assert _backend_policy.permission_enforcement("openai-compat", "read-only") == "enforced"
    assert _backend_policy.provider_contact_required("future-cli") is True


def test_uppercase_agent_names_work_and_casefold_collisions_fail(tmp_path):
    agents = [{"name": "MyAgent", "run_agent": "claude",
               "permission": "read-only", "model": "claude-opus-5",
               "source": "project"}]
    report, fleet, plan = _fleet.proposal(
        lane="review", seats=["MyAgent"], agents=agents, cwd=str(tmp_path),
        permission_ceiling="read-only")
    assert report["lanes"][0]["candidates"] == [
        {"seat": "MyAgent", "priority": 0}]
    assert _fleet_compile.verify_plan_for_fleet(plan, fleet)["catalog_sha256"]

    colliding = agents + [dict(agents[0], name="myagent")]
    with pytest.raises(_evidence.EvidenceError, match="case folding"):
        _fleet.catalog_snapshot(colliding)


def test_inspect_is_roster_independent_and_projects_every_constraint(tmp_path):
    _, fleet, _ = _proposal(
        tmp_path,
        provider_allowlist=["anthropic"],
        model_allowlist=["claude-opus-5"],
        required_capabilities=["text"],
        corrective={"contract_repair": True, "retry": True,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": False, "payg": False,
               "max_provider_contacts": 2, "max_billable_attempts": 1,
               "max_parallel": 2},
    )
    projection = _fleet.inspect(fleet)
    constraints = projection["lanes"][0]["constraints"]
    assert constraints["provider_allowlist"] == ["anthropic"]
    assert constraints["model_allowlist"] == ["claude-opus-5"]
    assert constraints["required_capabilities"] == ["text"]
    assert constraints["corrective"]["contract_repair"] is True
    assert constraints["spend"]["max_provider_contacts"] == 2
    assert projection["lanes"][0]["candidates"] == [
        {"seat": "architect", "priority": 0},
        {"seat": "sol-review", "priority": 1},
    ]


def test_priority_only_changes_are_visible_to_inspect(tmp_path):
    _, fleet, _ = _proposal(tmp_path)
    payload = json.loads(json.dumps(_evidence.verify(fleet)))
    payload["lanes"][0]["candidates"][0]["priority"] = 10
    payload["lanes"][0]["candidates"][1]["priority"] = 20
    changed = _evidence.seal("summon.fleet/v1", payload)
    assert (_fleet.inspect(fleet)["lanes"][0]["candidates"]
            != _fleet.inspect(changed)["lanes"][0]["candidates"])


def test_catalog_uses_single_snapshot_provider_and_dispatch_default_permission(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    definitions = {
        "nous": ("openai-compat", "nous", "nous/current-model", None),
        "openrouter": ("opencode", "openrouter", "openrouter/z-ai/glm-5.3-flash", "yolo"),
        "byteplus": ("openai-compat", "byteplus-coding", "glm-5.2", "read-only"),
        "defaulted": ("openai-compat", "anthropic", "claude-fable-5", None),
        "undeclared": ("opencode", None, "stealth/not-a-provider", "safe-edit"),
    }
    for name, (backend, provider, model, permission) in definitions.items():
        lines = ["---", f"run-agent: {backend}"]
        if provider:
            lines.append(f"provider: {provider}")
        lines.append(f"model: {model}")
        if permission:
            lines.append(f"permission: {permission}")
        lines.extend(["---", f"# {name}", "Review."])
        (agents_dir / f"{name}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    listed = {item["name"]: item for item in _loader.list_agents(str(agents_dir))}
    assert listed["defaulted"]["permission"] == _loader.DEFAULT_PERMISSION == "safe-edit"
    assert listed["nous"]["provider"] == "nous"
    assert listed["openrouter"]["provider"] == "openrouter"
    assert listed["byteplus"]["provider"] == "byteplus-coding"
    assert all(len(listed[name]["definition_sha256"]) == 64 for name in definitions)

    catalog, _, unavailable = _fleet.catalog_snapshot(
        [listed[name] for name in definitions])
    by_seat = {item["seat"]: item for item in catalog}
    assert unavailable == []
    assert by_seat["nous"]["provider"] == "nous"
    assert by_seat["openrouter"]["provider"] == "openrouter"
    assert by_seat["byteplus"]["provider"] == "byteplus-coding"
    assert by_seat["defaulted"]["permission"] == "safe-edit"
    assert by_seat["defaulted"]["effective_permission"] == "safe-edit"
    assert by_seat["undeclared"]["provider"] == "unknown"
    assert by_seat["undeclared"]["provider_evidence"] == "unknown"
    _, unknown_fleet, unknown_plan = _fleet.proposal(
        lane="review", seats=["undeclared"],
        agents=[listed["undeclared"]], cwd=str(tmp_path),
        permission_ceiling="safe-edit")
    explained = _fleet.explain(
        fleet=unknown_fleet, plan=unknown_plan,
        catalog=[by_seat["undeclared"]], lane_name="review")
    assert "provider_identity_unknown" in explained["candidates"][0]["reasons"]
    assert explained["candidates"][0]["matches_declared_constraints"] is False


def test_provider_binding_preserves_registry_case_and_rejects_route_mismatch():
    base = {"permission": "safe-edit", "source": "project",
            "lifecycle": "active"}
    agents = [
        dict(base, name="Custom", run_agent="openai-compat",
             provider="MyProvider", provider_endpoint_mode="named_provider",
             model="custom-model"),
        dict(base, name="Inline", run_agent="openai-compat",
             provider="DescriptiveOnly", provider_endpoint_mode="inline_endpoint",
             model="custom-model"),
        dict(base, name="Mismatch", run_agent="opencode",
             provider="anthropic", provider_endpoint_mode="none",
             model="openrouter/z-ai/glm-5.3-flash"),
        dict(base, name="Retired", run_agent="opencode",
             provider="openrouter", provider_endpoint_mode="none",
             model="openrouter/stealth/ox-alpha", lifecycle="retired"),
        dict(base, name="UnknownLiteral", run_agent="opencode",
             provider="unknown", provider_endpoint_mode="none",
             model="unknown/future-model"),
    ]
    catalog, _, unavailable = _fleet.catalog_snapshot(agents)
    by_seat = {item["seat"]: item for item in catalog}
    assert by_seat["Custom"]["provider"] == "MyProvider"
    assert by_seat["Custom"]["provider_evidence"] == "named_registry"
    assert by_seat["Inline"]["provider"] == "unknown"
    assert by_seat["Inline"]["provider_evidence"] == "unknown"
    assert by_seat["UnknownLiteral"]["provider"] == "unknown"
    assert by_seat["UnknownLiteral"]["provider_evidence"] == "unknown"
    assert unavailable == ["Mismatch", "Retired"]


def test_named_provider_case_matches_the_runtime_registry_lookup(tmp_path):
    import _apibackend

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "providers.json").write_text(json.dumps({
        "MyProvider": {
            "base_url": "https://provider.invalid/v1",
            "api_key_env": "CUSTOM_API_KEY",
        }
    }), encoding="utf-8")
    (agents_dir / "custom.md").write_text(
        "---\nrun-agent: openai-compat\nprovider: MyProvider\n"
        "model: custom-model\npermission: read-only\n---\n# Custom\nReview.\n",
        encoding="utf-8")

    listed = {item["name"]: item for item in _loader.list_agents(str(agents_dir))}
    catalog, _, unavailable = _fleet.catalog_snapshot([listed["custom"]])
    assert unavailable == []
    assert catalog[0]["provider"] == "MyProvider"
    assert catalog[0]["provider_evidence"] == "named_registry"
    assert _apibackend.resolve_endpoint(
        {"provider": "MyProvider"}, str(agents_dir))[0] == \
        "https://provider.invalid/v1"


def test_retired_agent_refuses_before_provider_and_names_successor(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "old-seat.md").write_text(
        "---\nrun-agent: definitely-missing-provider\n"
        "model: old/model\npermission: yolo\nlifecycle: retired\n"
        "successor: new-seat\n---\n# Old\nRetired.\n", encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "--agent", "old-seat", "--prompt", "test",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--strict-agents-dir", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    envelope = json.loads(completed.stdout)
    assert envelope["error_kind"] == "agent_retired"
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert envelope["agent_resolution"]["successor"] == "new-seat"


@pytest.mark.parametrize(("backend", "model"), [
    ("openai-compat", "stealth/ox-alpha"),
    ("opencode", "openrouter/stealth/ox-alpha"),
    ("opencode", "nous/stealth/ox-alpha"),
])
def test_ended_ox_alias_is_route_retired_even_in_stale_custom_roster(
        tmp_path, backend, model):
    """An old user definition cannot bypass the provider-alias tombstone."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "old-ox.md").write_text(
        "---\n"
        f"run-agent: {backend}\n"
        f"model: {model}\n"
        "permission: read-only\n"
        "lifecycle: active\n"
        "---\n# Historical preview\nEnded.\n",
        encoding="utf-8")
    listed = {item["name"]: item for item in _loader.list_agents(str(agents_dir))}
    assert listed["old-ox"]["lifecycle"] == "retired"
    assert listed["old-ox"]["successor"] == "openrouter-glm-5-3-flash-opencode"
    active = {
        "name": "active-seat", "run_agent": "claude",
        "permission": "read-only", "model": "claude-opus-5",
        "lifecycle": "active", "source": "project",
    }
    catalog, _, unavailable = _fleet.catalog_snapshot([listed["old-ox"], active])
    assert [item["seat"] for item in catalog] == ["active-seat"]
    assert unavailable == ["old-ox"]

    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "--agent", "old-ox", "--prompt", "test",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--strict-agents-dir", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    envelope = json.loads(completed.stdout)
    assert envelope["error_kind"] == "agent_retired"
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert envelope["model"]["served"] is None
    assert envelope["agent_resolution"]["successor"] == \
        "openrouter-glm-5-3-flash-opencode"


@pytest.mark.parametrize(("declared_backend", "declared_model", "overrides"), [
    ("opencode", "openrouter/current-model",
     ["--model", "openrouter/stealth/ox-alpha"]),
    ("codex", "nous/stealth/ox-alpha", ["--cli", "opencode"]),
    ("codex", "current-model",
     ["--cli", "opencode", "--model", "nous/stealth/ox-alpha"]),
    ("openai-compat", "current-model", ["--model", "stealth/ox-alpha"]),
])
def test_effective_cli_and_model_overrides_cannot_bypass_ended_route_tombstone(
        tmp_path, declared_backend, declared_model, overrides):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "active-seat.md").write_text(
        "---\n"
        f"run-agent: {declared_backend}\n"
        f"model: {declared_model}\n"
        "permission: read-only\n"
        "lifecycle: active\n"
        "---\n# Active declaration\nTest override.\n",
        encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "--agent", "active-seat", "--prompt", "test",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--strict-agents-dir", "--dry-run", "--json", *overrides],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    envelope = json.loads(completed.stdout)
    assert envelope["error_kind"] == "agent_retired"
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert envelope["attempt_status"] == "not_run"
    assert envelope["execution_status"] == "not_run"
    assert envelope["model"]["served"] is None
    assert envelope["served_model_evidence"] == "absent"
    assert envelope["agent_resolution"]["successor"] == \
        "openrouter-glm-5-3-flash-opencode"


def test_real_dispatch_override_hits_route_tombstone_before_backend_preflight(tmp_path):
    """The effective-route gate is shared by dry-run and real dispatch."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "active-seat.md").write_text(
        "---\n"
        "run-agent: codex\n"
        "model: current-model\n"
        "permission: read-only\n"
        "lifecycle: active\n"
        "---\n# Active declaration\nTest override.\n",
        encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "--agent", "active-seat", "--prompt", "test",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--strict-agents-dir", "--cli", "opencode", "--model",
         "nous/stealth/ox-alpha", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    envelope = json.loads(completed.stdout)
    assert envelope["error_kind"] == "agent_retired"
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert envelope["attempt_status"] == "not_run"
    assert envelope["execution_status"] == "not_run"
    assert envelope["model"]["served"] is None
    assert envelope["served_model_evidence"] == "absent"
    assert envelope["agent_resolution"]["successor"] == \
        "openrouter-glm-5-3-flash-opencode"


def test_bounded_read_unknown_capability_and_unrunnable_seat_are_actionable(tmp_path):
    huge = tmp_path / "huge.json"
    huge.write_bytes(b"x" * ((1 << 20) + 1))
    with pytest.raises(_evidence.EvidenceError, match="exceeds"):
        _fleet.load_fleet(str(huge))
    with pytest.raises(_evidence.EvidenceError, match="unsupported capabilities"):
        _fleet_compile.build_draft(
            lane="review", seats=["architect"], permission_ceiling="read-only",
            required_capabilities=["typo_capability"])

    agents = _agents() + [{"name": "BrokenSeat", "run_agent": None,
                           "permission": None, "model": None,
                           "source": "project"}]
    fleet = _fleet_compile.build_draft(
        lane="review", seats=["BrokenSeat"], permission_ceiling="read-only")
    with pytest.raises(_evidence.EvidenceError, match="without runnable definitions"):
        _fleet.compile_document(fleet=fleet, agents=agents, cwd=str(tmp_path))


def test_write_errors_are_redacted_and_dangling_symlink_is_refused(tmp_path, monkeypatch):
    _, fleet, _ = _proposal(tmp_path)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(target)
    except OSError:
        link = None
    if link is not None:
        with pytest.raises(_evidence.EvidenceError, match="symbolic-link"):
            _fleet.write_json(str(link), fleet)

    def denied(_self, *args, **kwargs):
        raise PermissionError(r"C:\private\owner\fleet.json")

    monkeypatch.setattr(Path, "resolve", denied)
    with pytest.raises(_evidence.EvidenceError, match="could not be written") as captured:
        _fleet.write_json(str(tmp_path / "out.json"), fleet)
    assert "private" not in str(captured.value)


def test_cli_rejects_clobber_and_action_specific_flags_before_roster_or_provider(tmp_path):
    script = Path(__file__).with_name("run_subagent.py")
    fleet_path = tmp_path / "fleet.json"
    _, fleet, _ = _proposal(tmp_path)
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")

    for argv, message in (
        (["fleet", "validate", str(fleet_path), "--out", str(fleet_path)],
         "must not replace"),
        (["fleet", "validate", str(fleet_path), "--allow-payg"],
         "does not accept propose-only flags"),
    ):
        completed = subprocess.run(
            [sys.executable, str(script), *argv, "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert completed.returncode == 1
        envelope = json.loads(completed.stdout)
        assert message in envelope["error"]
        assert envelope["provider_contacted"] is False
        assert envelope["attempts"] == 0
        assert envelope["execution_status"] == "not_run"

    existing = tmp_path / "existing.json"
    existing.write_text("do not replace", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), "fleet", "propose", "review",
         "--seats", "architect", "--cwd", str(tmp_path),
         "--agents-dir", str(Path(__file__).parents[1] / "agents"),
         "--out", str(existing), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 1
    assert existing.read_text(encoding="utf-8") == "do not replace"
    assert json.loads(completed.stdout)["provider_contacted"] is False


def test_fleet_rejects_plan_input_absent_lane_and_dispatch_only_flags(tmp_path):
    script = Path(__file__).with_name("run_subagent.py")
    _, fleet, plan = _proposal(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(_evidence.EvidenceError, match="unsupported schema"):
        _fleet.load_fleet(str(plan_path))

    catalog, _, _ = _fleet.catalog_snapshot(_agents())
    with pytest.raises(_evidence.EvidenceError, match="existing lane"):
        _fleet.explain(
            fleet=fleet, plan=plan, catalog=catalog, lane_name="missing")

    cases = [
        [sys.executable, str(script), "fleet", "propose", "review",
         "--seats", "architect", "--cwd", str(tmp_path),
         "--max-permission", "safe-edit", "--json"],
        [sys.executable, str(script), "--fleet-action", "propose",
         "--fleet-lane", "review", "--fleet-seats", "architect",
         "--cwd", str(tmp_path), "--allow-payg", "--json"],
    ]
    for argv in cases:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert completed.returncode == 1
        envelope = json.loads(completed.stdout)
        assert envelope["provider_contacted"] is False
        assert envelope["attempts"] == 0


def test_fleet_help_documents_every_proposal_policy_and_no_authority():
    help_text = _cli.command_usage("fleet")
    for flag in (
        "--provider", "--model", "--capability", "--permission-ceiling",
        "--data-boundary", "--allow-contract-repair", "--allow-retry",
        "--allow-fallback", "--allow-continuation", "--allow-subscription",
        "--allow-credit", "--allow-payg", "--max-provider-contacts",
        "--max-billable-attempts", "--max-parallel",
    ):
        assert flag in help_text
    assert "never approval or spend consent" in help_text


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_cmd_full_fleet_control_plane_never_contacts_provider(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "reviewer.md").write_text(
        "---\nrun-agent: definitely-missing-provider-cli\n"
        "permission: read-only\nmodel: future-model\n---\n# Reviewer\nReview.",
        encoding="utf-8")
    wrapper = Path(__file__).with_name("summon.cmd")
    fleet_path = tmp_path / "fleet.json"
    base = ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper)]
    proposed = subprocess.run(
        [*base, "fleet", "propose", "review", "--seats", "reviewer",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--out", str(fleet_path), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr
    proposed_json = json.loads(proposed.stdout)
    assert proposed_json["provider_contacted"] is False
    assert fleet_path.exists()

    validated = subprocess.run(
        [*base, "fleet", "validate", str(fleet_path), "--cwd", str(tmp_path),
         "--agents-dir", str(agents_dir), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert json.loads(validated.stdout)["provider_contacted"] is False

    # Inspect is intentionally roster-independent: it must remain usable after
    # the live roster changes or disappears and therefore rejects roster/cwd flags.
    inspected = subprocess.run(
        [*base, "fleet", "inspect", str(fleet_path), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert json.loads(inspected.stdout)["provider_contacted"] is False

    explained = subprocess.run(
        [*base, "fleet", "explain", str(fleet_path), "review",
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert explained.returncode == 0, explained.stdout + explained.stderr
    projection = json.loads(explained.stdout)
    assert projection["provider_contacted"] is False
    assert projection["selection"]["seat"] is None
    assert projection["selection"]["status"] == "not_authorized"
    assert "permission_not_enforced" in projection["candidates"][0]["reasons"]
    assert "definitely-missing-provider-cli" not in explained.stderr
