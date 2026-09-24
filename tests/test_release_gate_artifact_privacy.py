"""Strict public gate artifacts at intake and final release checking."""
from __future__ import annotations
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_collect as collect
import release_gates as gates
manifest = gates._MANIFEST
contract = manifest._OUTCOMES


@contextlib.contextmanager
def isolated_nested_collection():
    """Evict and restore all in-process collector bindings around a fixture run."""
    sentinel = object()
    module_names = (
        "test_fixture",
        "tests.test_fixture",
        "test_failure",
        "tests.test_failure",
        "_summon_release_collector",
    )
    saved_modules = {name: sys.modules.get(name, sentinel) for name in module_names}
    saved_quiet = os.environ.get("AGY_PTY_QUIET", sentinel)
    saved_active = collect._ACTIVE
    for name in module_names:
        sys.modules.pop(name, None)
    collect._ACTIVE = None
    try:
        yield
    finally:
        collect._ACTIVE = saved_active
        if saved_quiet is sentinel:
            os.environ.pop("AGY_PTY_QUIET", None)
        else:
            os.environ["AGY_PTY_QUIET"] = saved_quiet
        for name, value in saved_modules.items():
            if value is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    root = tmp_path_factory.mktemp("gate-artifact-fixture")
    (root / "tools").mkdir()
    (root / "tests").mkdir()
    for name in ("release_gates.py", "release_collect.py", "release_skip_policy.json"):
        shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
    path = root / "tests/test_fixture.py"
    path.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    source, head = "a" * 64, "b" * 40
    command = "python -m pytest -q tests/test_fixture.py"
    collected = {}
    for kind, name in (("suite", "fixture"), ("gate", "fake_lifecycle")):
        capture = collect.Capture(root, kind, name, command, source, head)
        previous = Path.cwd()
        os.chdir(root)
        try:
            with isolated_nested_collection():
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    collected[kind] = collect.execute(capture, command)
        finally:
            os.chdir(previous)
    suite, gate = collected["suite"], collected["gate"]
    artifact = gates._artifact("fake_lifecycle", status="pass", command=command,
        source_hash=source, git_head=head, output="", test_count="1/1", outcomes=gate)
    artifact["output_sha256"] = gate["output_sha256"]
    artifact["artifact_sha256"] = manifest._artifact_sha256(artifact)
    live_command = "python tools/live_provider_gate.py"
    live = gates._artifact("live_provider", status="pass", command=live_command,
        source_hash=source, git_head=head, output="", marker={"status": "pass",
        "artifact_sha256": "c" * 64, "evidence_file": contract.GATE_EVIDENCE_FILE})
    evidence = dict(schema=2, source_tree_sha256=source, git_head=head, git_status_clean=True,
        producer="tools/release_gates.py", producer_sha256=contract.file_hash(root / "tools/release_gates.py"),
        version_contract={"ready": True}, runtime={}, tests={"fixture": "1/1"},
        known_gates={"fake_lifecycle": "pass", "live_provider": "pass"},
        commands={"fixture": command}, gate_commands={"fake_lifecycle": command, "live_provider": live_command},
        test_results={"fixture": {"count": "1/1", "output_sha256": suite["output_sha256"], "outcomes": suite}},
        gate_results={"fake_lifecycle": artifact, "live_provider": live})
    return SimpleNamespace(root=root, source=source, head=head, evidence=evidence)


@pytest.fixture
def consumers(facts, monkeypatch):
    evidence = copy.deepcopy(facts.evidence)
    monkeypatch.setattr(manifest, "REQUIRED_TESTS", frozenset(evidence["tests"]))
    monkeypatch.setattr(manifest, "REQUIRED_COMMANDS", evidence["commands"])
    monkeypatch.setattr(manifest, "REQUIRED_GATES", frozenset(evidence["known_gates"]))
    monkeypatch.setattr(manifest, "REQUIRED_GATE_COMMANDS", evidence["gate_commands"])
    monkeypatch.setattr(manifest, "_version_contract", lambda root: {"ready": True})
    monkeypatch.setattr(manifest, "_git_facts", lambda root: {"head": facts.head, "dirty": False})
    return SimpleNamespace(**vars(facts), value=evidence)


def project(value):
    return dict(evidence=value, source_tree_sha256=value["source_tree_sha256"],
        git={"head": value["git_head"], "dirty": False}, version_contract={"ready": True},
        **{key: value[key] for key in ("tests", "known_gates", "test_results", "gate_results")})


def intake(consumers, value):
    path = consumers.root / "evidence.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return manifest._read_evidence(path, consumers.root, consumers.source)


def resign(value):
    value["artifact_sha256"] = manifest._artifact_sha256(value)


def refuse_both(consumers, value):
    with pytest.raises(ValueError, match="invalid machine gate artifact"):
        intake(consumers, value)
    failures = manifest._check_facts(project(value), consumers.root)
    assert any("invalid machine gate artifact" in failure for failure in failures)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(failures)


def validate_artifact(consumers, artifact):
    name = artifact["gate"]
    contract.validate_gate_artifact(artifact, root=consumers.root, name=name,
        status=artifact["status"], command=consumers.value["gate_commands"][name],
        source_hash=consumers.source, git_head=consumers.head)


def test_valid_serialized_producer_artifacts_pass_both_consumers(consumers):
    read = intake(consumers, consumers.value)
    assert manifest._check_facts(project(read), consumers.root) == []


@pytest.mark.parametrize("name,field", [
    pytest.param('fake_lifecycle', 'detail', id='fake_lifecycle-detail'),
    pytest.param('fake_lifecycle', 'error_kind', id='fake_lifecycle-error_kind'),
    pytest.param('fake_lifecycle', 'evidence_file', id='fake_lifecycle-evidence_file'),
    pytest.param('fake_lifecycle', 'unknown_marker', id='fake_lifecycle-unknown_marker'),
    pytest.param('fake_lifecycle', 'unknown_nested_marker', id='fake_lifecycle-unknown_nested_marker'),
    pytest.param('live_provider', 'detail', id='live_provider-detail'),
    pytest.param('live_provider', 'error_kind', id='live_provider-error_kind'),
    pytest.param('live_provider', 'evidence_file', id='live_provider-evidence_file'),
    pytest.param('live_provider', 'unknown_marker', id='live_provider-unknown_marker'),
    pytest.param('live_provider', 'unknown_nested_marker', id='live_provider-unknown_nested_marker'),
 ])
def test_recomputed_private_or_unknown_marker_refuses_both_consumers(consumers, name, field):
    artifact = consumers.value["gate_results"][name]
    canary = "SYNTHETIC_PRIVATE_MARKER"
    artifact[field] = {"text": canary} if field == "unknown_nested_marker" else canary
    if field == "evidence_file":
        artifact[field] = "C:/synthetic-private/" + canary + ".json"
    resign(artifact)
    refuse_both(consumers, consumers.value)


@pytest.mark.parametrize("field,value", [
    pytest.param("schema", True, id="boolean-schema"),
    pytest.param("schema", 1.0, id="float-schema"),
    pytest.param("schema", "1", id="string-schema"),
    pytest.param("schema", 2, id="future-schema"),
    pytest.param("status", True, id="boolean-status"),
    pytest.param("status", "pending", id="unsupported-status"),
    pytest.param("gate", ["live_provider"], id="nested-gate"),
    pytest.param("command", "python unknown.py", id="wrong-command"),
    pytest.param("source_tree_sha256", "d" * 64, id="wrong-source-binding"),
    pytest.param("git_head", "e" * 40, id="wrong-head-binding"),
    pytest.param("producer", "tools/other.py", id="wrong-producer"),
    pytest.param("output_sha256", None, id="null-output-digest"),
    pytest.param("error_kind", [], id="nested-diagnostic-code"),
    pytest.param("detail", None, id="null-detail"),
    pytest.param("detail", {"text": "gate_diagnostic_redacted"}, id="nested-detail"),
    pytest.param("evidence_file", ["redacted-live-provider-receipt.json"], id="nested-evidence-label"),
    pytest.param("evidence_sha256", True, id="boolean-receipt-digest"),
    pytest.param("evidence_sha256", "not-a-digest", id="malformed-receipt-digest"),
    pytest.param("outcomes", {}, id="live-outcomes-forbidden"),
    pytest.param("test_count", "1/1", id="live-test-count-forbidden"),
])
def test_recomputed_invalid_artifact_schema_types_and_bindings_refuse(consumers, field, value):
    artifact = consumers.value["gate_results"]["live_provider"]
    artifact[field] = value
    resign(artifact)
    refuse_both(consumers, consumers.value)


@pytest.mark.parametrize("field", ["schema", "output_sha256", "evidence_sha256"],
                         ids=["missing-schema", "missing-output-digest", "missing-required-receipt"])
def test_missing_required_artifact_field_refuses(consumers, field):
    artifact = consumers.value["gate_results"]["live_provider"]
    del artifact[field]
    resign(artifact)
    refuse_both(consumers, consumers.value)


@pytest.mark.parametrize("field,value", [
    pytest.param("evidence_sha256", "c" * 64, id="non-live-receipt-forbidden"),
    pytest.param("test_count", True, id="boolean-count"),
    pytest.param("test_count", "2/2", id="count-mismatch"),
    pytest.param("outcomes", {}, id="invalid-outcomes"),
    pytest.param("output_sha256", "d" * 64, id="outcome-output-mismatch"),
])
def test_non_live_artifact_keeps_strict_outcome_binding(consumers, field, value):
    artifact = consumers.value["gate_results"]["fake_lifecycle"]
    artifact[field] = value
    resign(artifact)
    refuse_both(consumers, consumers.value)


def test_stale_digest_still_refuses_both_consumers(consumers):
    consumers.value["gate_results"]["live_provider"]["detail"] = contract.GATE_REDACTED
    refuse_both(consumers, consumers.value)


@pytest.mark.parametrize("code", ["gate_command_failed", "live_provider_evidence_missing",
                         "live_provider_evidence_invalid", "gate_diagnostic_redacted"],
                         ids=["command-failed", "receipt-missing", "receipt-invalid", "redacted"])
def test_source_defined_diagnostics_and_label_preserve_producer_shapes(consumers, code):
    # The producer permits the exact fixed evidence label on either gate kind.
    for name in ("fake_lifecycle", "live_provider"):
        artifact = consumers.value["gate_results"][name]
        artifact.update(error_kind=code, detail=contract.GATE_REDACTED,
                        evidence_file=contract.GATE_EVIDENCE_FILE)
        resign(artifact)
    assert manifest._check_facts(project(intake(consumers, consumers.value)), consumers.root) == []


@pytest.mark.parametrize("mode", ["pass", "blocked-marker", "command-error"],
                         ids=["pass-receipt", "blocked-marker", "command-error"])
def test_actual_live_gate_producer_branches_have_valid_safe_shapes(consumers, monkeypatch, mode):
    marker = {"gate": "live_provider", "status": "pass", "artifact_sha256": "c" * 64,
              "evidence_file": contract.GATE_EVIDENCE_FILE}
    if mode == "blocked-marker":
        marker.update(status="blocked", error_kind="live_provider_evidence_missing")
    output = json.dumps(marker) if mode != "command-error" else ""
    monkeypatch.setattr(gates, "_hermetic_environment", lambda: {})
    monkeypatch.setattr(gates.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0 if mode == "pass" else 1, stdout=output, stderr="SYNTHETIC_PRIVATE_MARKER"))
    status, artifact = gates.run_gate("live_provider", 1, source_hash=consumers.source,
                                      git_head=consumers.head)
    assert status == ("pass" if mode == "pass" else "blocked")
    validate_artifact(consumers, artifact)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(artifact)
    consumers.value["gate_results"]["live_provider"] = artifact
    consumers.value["known_gates"]["live_provider"] = status
    read = intake(consumers, consumers.value)
    failures = manifest._check_facts(project(read), consumers.root)
    assert not any("invalid machine gate artifact" in failure for failure in failures)
    assert (failures == []) is (status == "pass")


@pytest.mark.parametrize("status", ["pass", "blocked"], ids=["pass", "blocked"])
def test_actual_non_live_gate_producer_marker_status_preserved(consumers, monkeypatch, status):
    outcomes = copy.deepcopy(consumers.value["gate_results"]["fake_lifecycle"]["outcomes"])
    output = json.dumps({"gate": "fake_lifecycle", "status": status})
    import hashlib
    outcomes["output_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
    resign(outcomes)
    monkeypatch.setattr(gates, "ROOT", consumers.root)
    monkeypatch.setattr(gates, "GATE_COMMANDS", consumers.value["gate_commands"])
    monkeypatch.setattr(gates, "_run_outcomes", lambda *a, **k: ("1/1", output, outcomes))
    actual_status, artifact = gates.run_gate("fake_lifecycle", 1, source_hash=consumers.source,
                                            git_head=consumers.head)
    assert actual_status == status
    validate_artifact(consumers, artifact)
    consumers.value["gate_results"]["fake_lifecycle"] = artifact
    consumers.value["known_gates"]["fake_lifecycle"] = status
    read = intake(consumers, consumers.value)
    failures = manifest._check_facts(project(read), consumers.root)
    assert not any("invalid machine gate artifact" in failure for failure in failures)
    assert (failures == []) is (status == "pass")


def test_historical_preview_stays_sanitized_and_ineligible(consumers):
    value = copy.deepcopy(consumers.value)
    value["schema"] = 1
    value["gate_results"]["live_provider"]["detail"] = "SYNTHETIC_PRIVATE_MARKER"
    path = consumers.root / "historical.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    read = manifest._read_evidence(path, consumers.root, consumers.source, allow_legacy_preview=True)
    assert read["legacy_preview"] is True
    assert read["gate_results"] == {}
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(read)
    assert manifest._check_facts(read, consumers.root)


@pytest.mark.parametrize("field", ["outcomes", "test_count"],
                         ids=["missing-outcomes", "missing-test-count"])
def test_non_live_artifact_requires_outcome_fields(consumers, field):
    artifact = consumers.value["gate_results"]["fake_lifecycle"]
    del artifact[field]
    resign(artifact)
    refuse_both(consumers, consumers.value)


def test_actual_failed_framework_keeps_blocked_producer_shape_without_eligibility(consumers, monkeypatch):
    command = "python -m pytest -q tests/test_failure.py"
    (consumers.root / "tests/test_failure.py").write_text(
        "def test_failure():\n    assert False\n", encoding="utf-8")
    capture = collect.Capture(consumers.root, "gate", "fake_lifecycle", command,
                              consumers.source, consumers.head)
    previous = Path.cwd()
    os.chdir(consumers.root)
    try:
        with isolated_nested_collection():
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                outcomes = collect.execute(capture, command)
    finally:
        os.chdir(previous)
    assert outcomes["counts"]["failed"] == 1
    assert outcomes["exit_code"] != 0
    count = contract.validate_outcomes(outcomes, root=consumers.root, kind="gate",
        name="fake_lifecycle", command=command, source_hash=consumers.source,
        git_head=consumers.head, require_pass=False)
    consumers.value["gate_commands"]["fake_lifecycle"] = command
    monkeypatch.setattr(gates, "ROOT", consumers.root)
    monkeypatch.setattr(gates, "GATE_COMMANDS", consumers.value["gate_commands"])
    monkeypatch.setattr(gates, "_run_outcomes", lambda *a, **k: (count, "", outcomes))
    status, artifact = gates.run_gate("fake_lifecycle", 1, source_hash=consumers.source,
                                      git_head=consumers.head)
    assert status == "blocked"
    validate_artifact(consumers, artifact)
    consumers.value["gate_results"]["fake_lifecycle"] = artifact
    consumers.value["known_gates"]["fake_lifecycle"] = status
    evidence_path = diagnostic_fixture(consumers, monkeypatch)
    assert gates.validate_incomplete_diagnostics(evidence_path, consumers.root) is None
    with pytest.raises(ValueError, match="do not qualify"):
        manifest._read_evidence(evidence_path, consumers.root, consumers.source)
    failures = manifest._check_facts(project(consumers.value), consumers.root)
    assert any("structured release outcomes" in failure for failure in failures)
    assert not any("invalid machine gate artifact" in failure for failure in failures)



def refuse_suite_both(consumers, value):
    with pytest.raises(ValueError):
        intake(consumers, value)
    failures = manifest._check_facts(project(value), consumers.root)
    assert any("structured release outcomes" in failure for failure in failures)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(failures)


@pytest.mark.parametrize("field", ["detail", "error_kind", "evidence_file",
                                    "unknown_marker", "unknown_nested_marker"],
                         ids=["detail", "error-kind", "evidence-file", "unknown-scalar", "unknown-nested"])
def test_suite_wrapper_private_or_unknown_fields_refuse_both_consumers(consumers, field):
    row = consumers.value["test_results"]["fixture"]
    canary = "SYNTHETIC_PRIVATE_MARKER"
    row[field] = {"text": canary} if field == "unknown_nested_marker" else canary
    if field == "evidence_file":
        row[field] = "C:/synthetic-private/" + canary + ".json"
    # The copied wrapper has no digest of its own; the valid outcome is intact.
    refuse_suite_both(consumers, consumers.value)


@pytest.mark.parametrize("field", ["count", "output_sha256", "outcomes"],
                         ids=["missing-count", "missing-output-digest", "missing-outcomes"])
def test_suite_wrapper_missing_fields_refuse_both_consumers(consumers, field):
    del consumers.value["test_results"]["fixture"][field]
    refuse_suite_both(consumers, consumers.value)


@pytest.mark.parametrize("field,value", [
    pytest.param("count", True, id="boolean-count"),
    pytest.param("count", 1, id="integer-count"),
    pytest.param("count", ["1/1"], id="nested-count"),
    pytest.param("output_sha256", None, id="null-output-digest"),
    pytest.param("output_sha256", [], id="nested-output-digest"),
    pytest.param("outcomes", None, id="null-outcomes"),
    pytest.param("outcomes", [], id="list-outcomes"),
])
def test_suite_wrapper_types_refuse_both_consumers(consumers, field, value):
    consumers.value["test_results"]["fixture"][field] = value
    refuse_suite_both(consumers, consumers.value)


def test_suite_projection_equality_still_required(consumers):
    read = intake(consumers, consumers.value)
    valid = project(read)
    assert manifest._check_facts(valid, consumers.root) == []
    changed = copy.deepcopy(valid)
    changed["test_results"] = copy.deepcopy(changed["test_results"])
    changed["test_results"]["fixture"]["count"] = "2/2"
    assert any("structured release outcomes" in failure
               for failure in manifest._check_facts(changed, consumers.root))


def diagnostic_fixture(consumers, monkeypatch):
    value = consumers.value
    monkeypatch.setattr(manifest, "source_tree_sha256", lambda root: consumers.source)
    value["platform_qualification"] = dict(schema=1, host="non_windows", windows_evidence="unavailable",
        windows_scoped_gates={name: "unavailable" for name in manifest.WINDOWS_SCOPED_GATES}, status="incomplete")
    value["known_gates"]["live_provider"] = "blocked"
    value["gate_results"]["live_provider"] = gates._artifact("live_provider", status="blocked",
        command=value["gate_commands"]["live_provider"], source_hash=consumers.source,
        git_head=consumers.head, output="", marker={"error_kind": "live_provider_evidence_missing"})
    directory = consumers.root / "diagnostics.gates/rendered"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "native-zoom-measurements.json").write_bytes(b'{"synthetic":true}\r\n')
    value["rendered_evidence"] = gates._collect_rendered_evidence(directory, consumers.source)
    evidence_path = consumers.root / "diagnostics.json"
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    return evidence_path


def test_ce_incomplete_diagnostics_preserve_strict_intake(consumers, monkeypatch):
    evidence_path = diagnostic_fixture(consumers, monkeypatch)
    assert gates.validate_incomplete_diagnostics(evidence_path, consumers.root) is None
    assert consumers.value["known_gates"]["live_provider"] == "blocked"
    assert manifest._check_facts(project(consumers.value), consumers.root)
    (consumers.root / "diagnostics.gates/rendered/native-zoom-measurements.json").write_bytes(b'{"synthetic":true}\n')
    with pytest.raises(ValueError, match="rendered"):
        gates.validate_incomplete_diagnostics(evidence_path, consumers.root)


def test_workspace_preview_profile_accepts_only_explicit_missing_live_receipt(consumers):
    value = copy.deepcopy(consumers.value)
    value["known_gates"]["live_provider"] = "blocked"
    value["gate_results"]["live_provider"] = gates._artifact(
        "live_provider", status="blocked",
        command=value["gate_commands"]["live_provider"],
        source_hash=consumers.source, git_head=consumers.head, output="",
        marker={"error_kind": "live_provider_evidence_missing", "detail": "missing"},
    )
    projected = project(value)
    projected["release_profile"] = manifest._release_profile_contract(
        manifest.WORKSPACE_PREVIEW_RELEASE_PROFILE
    )
    assert manifest._check_facts(projected, consumers.root) == []

    stable = copy.deepcopy(projected)
    stable["release_profile"] = manifest._release_profile_contract(
        manifest.STABLE_RELEASE_PROFILE
    )
    assert any("gate is not passing: live_provider=blocked" in failure
               for failure in manifest._check_facts(stable, consumers.root))

    invalid = copy.deepcopy(projected)
    invalid["gate_results"]["live_provider"] = gates._artifact(
        "live_provider", status="blocked",
        command=value["gate_commands"]["live_provider"],
        source_hash=consumers.source, git_head=consumers.head, output="",
        marker={"error_kind": "live_provider_evidence_invalid", "detail": "invalid"},
    )
    assert any("gate is not passing: live_provider=blocked" in failure
               for failure in manifest._check_facts(invalid, consumers.root))


@pytest.mark.parametrize("field", ["source_tree_sha256", "producer_sha256", "counts", "platform", "live"],
                         ids=["source", "producer", "outcome-count", "platform-promotion", "live-promotion"])
def test_ce_diagnostics_reject_forged_or_promoted_evidence(consumers, monkeypatch, field):
    evidence_path = diagnostic_fixture(consumers, monkeypatch)
    value = consumers.value
    if field in {"source_tree_sha256", "producer_sha256"}:
        value[field] = "f" * 64
    elif field == "counts":
        value["test_results"]["fixture"]["outcomes"]["counts"]["passed"] = 99
    elif field == "platform":
        value["platform_qualification"]["status"] = "qualified"
    else:
        value["known_gates"]["live_provider"] = "pass"
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        gates.validate_incomplete_diagnostics(evidence_path, consumers.root)


@pytest.mark.parametrize("quiet", [None, "ambient-value"], ids=["absent-quiet", "existing-quiet"])
def test_ce_same_name_failed_collections_restore_modules_and_environment(tmp_path, monkeypatch, quiet):
    if quiet is None:
        monkeypatch.delenv("AGY_PTY_QUIET", raising=False)
    else:
        monkeypatch.setenv("AGY_PTY_QUIET", quiet)
    names = ("test_failure", "tests.test_failure")
    prior = {name: sys.modules.get(name) for name in names}
    for index in (1, 2):
        root = tmp_path / str(index)
        (root / "tests").mkdir(parents=True)
        (root / "tools").mkdir()
        for name in ("release_gates.py", "release_collect.py", "release_skip_policy.json"):
            shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
        (root / "tests/test_failure.py").write_text(
            "import os\nos.environ['AGY_PTY_QUIET']='nested'\ndef test_failure_" + str(index) + "():\n    assert False\n", encoding="utf-8")
        command = "python -m pytest -q tests/test_failure.py"
        capture = collect.Capture(root, "gate", "fake_lifecycle", command, "a" * 64, "b" * 40)
        previous = Path.cwd()
        try:
            os.chdir(root)
            with isolated_nested_collection(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                outcomes = collect.execute(capture, command)
        finally:
            os.chdir(previous)
        assert outcomes["counts"]["failed"] == 1
        assert outcomes["collected_nodeids"] == [f"tests/test_failure.py::test_failure_{index}"]
        assert all(sys.modules.get(name) is prior[name] for name in names)
        assert os.environ.get("AGY_PTY_QUIET") == quiet
