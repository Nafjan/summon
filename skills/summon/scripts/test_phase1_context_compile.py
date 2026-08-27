"""Provider-inert contract tests for the Phase 1 M5 context compiler."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

import _context_compile as compiler


def sample(*blocks):
    return {"schema": compiler.INPUT_SCHEMA, "blocks": list(blocks)}


def authority(block_id="system", body="System rules\nKeep order.", kind="system_instruction"):
    return {"id": block_id, "plane": "authority", "kind": kind, "body": body}


def artifact(block_id="artifact", body="immutable body", **extra):
    return {"id": block_id, "plane": "payload", "kind": "immutable_artifact",
            "body": body, "immutable": True, "stable": False, **extra}


def diagnostic(block_id="trace", body="x", errors=None, **extra):
    return {"id": block_id, "plane": "payload", "kind": "tool_chatter", "body": body,
            "typed_errors": [] if errors is None else errors, **extra}


def report(block_id="report", body="validated"):
    return {"id": block_id, "plane": "payload", "kind": "validated_report", "body": body,
            "immutable": True, "stable": False}


def receipt_proof(report_id="report", report_body="validated", receipt_byte="1"):
    return compiler.make_receipt_proof(report_id, report_body, receipt_byte * 64)


def decoded(result):
    return json.loads(result["compiled_utf8"])


class NoGetMapping(Mapping):
    """A mapping whose multi-read convenience API is intentionally hostile."""

    def __init__(self, value):
        self.value = dict(value)

    def __getitem__(self, key):
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)

    def get(self, *_args, **_kwargs):  # pragma: no cover - must never be called
        raise AssertionError("original mapping was read after snapshot")


def test_authority_turns_are_byte_identical_ordered_and_never_deduplicated():
    first = "SYSTEM\r\nDo not modify data.\r\n"
    second = "USER\nReview exactly.\n"
    value = sample(authority("a", first), artifact("p", "one"), authority("u", second, "user_instruction"))
    result = compiler.compile_context(value)
    blocks = decoded(result)["blocks"]
    assert [(item["id"], item["body"]) for item in blocks if item["plane"] == "authority"] == [("a", first), ("u", second)]
    assert [item["action"] for item in result["source_to_output"]] == ["preserved", "preserved", "preserved"]
    assert result["provider_contacted"] is False


def test_safe_deduplicates_only_identical_explicit_immutable_payloads():
    result = compiler.compile_context(sample(
        artifact("one", "same"), artifact("two", "same"),
        {"id": "ordinary", "plane": "payload", "kind": "note", "body": "same"},
    ))
    blocks = decoded(result)["blocks"]
    assert blocks[1]["dedupe_of"] == "one"
    assert "body" not in blocks[1]
    assert blocks[2]["body"] == "same"
    assert [entry["action"] for entry in result["source_to_output"]] == ["preserved", "deduplicated", "preserved"]


def test_same_bytes_but_different_immutable_types_are_not_deduplicated():
    report = {"id": "report", "plane": "payload", "kind": "validated_report", "body": "same",
              "immutable": True, "stable": False}
    result = compiler.compile_context(sample(artifact("artifact", "same"), report))
    assert all(item["action"] == "preserved" for item in result["source_to_output"])


def test_different_immutable_bodies_are_never_deduplicated():
    result = compiler.compile_context(sample(artifact("a", "first"), artifact("b", "second")))
    assert [item["action"] for item in result["source_to_output"]] == ["preserved", "preserved"]


def test_stable_external_reference_requires_target_hash_proof_before_output():
    body = "frozen body"
    digest = hashlib.sha256(body.encode()).hexdigest()
    reference = "sha256:" + digest
    target = artifact("a", body, stable=True, externalize=True, artifact_ref=reference)
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(target))
    assert raised.value.kind == "reference_proof_required"
    proof = {"schema": "summon.context-reference-proof/v1", "references": {
        reference: {"reference": reference, "sha256": digest, "verified_target": True,
                    "verification_method": "sha256-readback"}}}
    result = compiler.compile_context(sample(target), reference_proof=proof)
    block = decoded(result)["blocks"][0]
    assert block == {"id": "a", "plane": "payload", "kind": "immutable_artifact", "artifact_ref": reference,
                     "sha256": digest, "target_verified": True,
                     "verification_method": "sha256-readback"}
    assert result["source_to_output"][0]["action"] == "referenced"


def test_reference_proof_refuses_wrong_hash_and_pathlike_reference():
    body = "body"
    reference = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    target = artifact("a", body, stable=True, externalize=True, artifact_ref=reference)
    bad = {"schema": "summon.context-reference-proof/v1", "references": {
        reference: {"reference": reference, "sha256": "0" * 64, "verified_target": True,
                    "verification_method": "sha256-readback"}}}
    with pytest.raises(compiler.ContextCompileError, match="resolved"):
        compiler.compile_context(sample(target), reference_proof=bad)
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(artifact("b", "body", stable=True, externalize=True,
                                                  artifact_ref="C:/private/path")))
    unverified = {"schema": "summon.context-reference-proof/v1", "references": {
        reference: {"reference": reference, "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "verified_target": False, "verification_method": "sha256-readback"}}}
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(target), reference_proof=unverified)
    assert raised.value.kind == "reference_proof_invalid"


def test_reference_and_receipt_proofs_are_snapshotted_before_field_validation():
    body = "frozen"
    digest = hashlib.sha256(body.encode()).hexdigest()
    reference = "sha256:" + digest
    raw_target = NoGetMapping({
        "reference": reference, "sha256": digest, "verified_target": True,
        "verification_method": "sha256-readback",
    })
    proof = NoGetMapping({
        "schema": "summon.context-reference-proof/v1",
        "references": NoGetMapping({reference: raw_target}),
    })
    raw_receipt = NoGetMapping(receipt_proof())
    result = compiler.compile_context(sample(
        artifact("a", body, stable=True, externalize=True, artifact_ref=reference),
        report(), diagnostic("d", "verbose", ["tool_missing"], receipt_proof=raw_receipt)),
        reference_proof=proof, diagnostic_tail_bytes=0)
    assert [item["action"] for item in result["source_to_output"]] == [
        "referenced", "preserved", "tail_bounded"]
    wrong_binding = {"schema": "summon.context-reference-proof/v1", "references": {
        reference: {"reference": "sha256:" + "f" * 64,
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "verified_target": True, "verification_method": "sha256-readback"}}}
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(
            artifact("a", body, stable=True, externalize=True, artifact_ref=reference)),
            reference_proof=wrong_binding)
    assert raised.value.kind == "reference_proof_invalid"


def test_diagnostic_tail_preserves_typed_errors_full_hash_and_omission_count():
    body = "prefix-" + ("é" * 30) + "-tail"
    result = compiler.compile_context(sample(
        report(), diagnostic("d", body, ["tool_missing", "provider_timeout"],
                             receipt_proof=receipt_proof())),
                                      diagnostic_tail_bytes=17)
    block = decoded(result)["blocks"][1]
    assert block["typed_errors"] == ["tool_missing", "provider_timeout"]
    assert block["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert block["omitted_bytes"] > 0 and block["body_tail"].endswith("-tail")
    assert result["source_to_output"][1]["action"] == "tail_bounded"


def test_diagnostic_never_drops_typed_errors_and_zero_tail_is_valid():
    result = compiler.compile_context(sample(
        report(), diagnostic("d", "verbose", ["authentication_failed"],
                             receipt_proof=receipt_proof(receipt_byte="2"))),
                                      diagnostic_tail_bytes=0)
    block = decoded(result)["blocks"][1]
    assert block["body_tail"] == ""
    assert block["typed_errors"] == ["authentication_failed"]
    assert block["omitted_bytes"] == len("verbose".encode())


def test_raw_input_off_profile_and_safe_rollback_preserve_exact_original_bytes():
    legacy = (b'{ "schema" : "summon.context-input/v1",\r\n'
              b'  "blocks" : [{"id":"system","plane":"authority",'
              b'"kind":"system_instruction","body":"System rules\\r\\n"}] }\r\n')
    result = compiler.compile_context(legacy, profile="off")
    safe = compiler.compile_context(legacy)
    assert result["compiled_utf8"].encode() == legacy
    assert result["before_bytes"] == result["after_bytes"] == len(legacy)
    assert all(item["action"] == "preserved" for item in result["source_to_output"])
    assert base64.b64decode(result["rollback"]["source_utf8_b64"]) == legacy
    assert base64.b64decode(safe["rollback"]["source_utf8_b64"]) == legacy
    assert safe["before_bytes"] == len(legacy)
    item = result["source_to_output"][0]
    assert item["source_block_sha256"] == item["output_block_sha256"]


def test_legacy_serialized_must_match_raw_or_canonical_mapping_source():
    value = sample(authority())
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(value, profile="off", legacy_serialized=b"unrelated")
    assert raised.value.kind == "legacy_serialization_mismatch"
    raw = json.dumps(value, indent=2)
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(raw, profile="off", legacy_serialized=compiler._canonical(value))


def test_off_profile_ignores_irrelevant_reference_proof_parameter():
    class Exploding:
        def __getattribute__(self, _name):
            raise AssertionError("off mode inspected an irrelevant proof")

    result = compiler.compile_context(sample(authority()), profile="off",
                                      reference_proof=Exploding())
    assert result["profile"] == "off"


def test_safe_dry_run_mapping_counts_lineage_and_rollback_are_deterministic():
    value = sample(authority(), artifact("one", "same"), artifact("two", "same"))
    first = compiler.compile_context(value)
    second = compiler.compile_context(value)
    assert first == second
    assert first["before_bytes"] > 0 and first["after_bytes"] > 0
    assert first["token_estimate"]["method"] == compiler.TOKEN_ESTIMATE_METHOD
    assert len(first["lineage_sha256"]) == 64
    restored = base64.b64decode(first["rollback"]["source_utf8_b64"])
    assert hashlib.sha256(restored).hexdigest() == first["rollback"]["source_sha256"]
    assert json.loads(restored)["schema"] == compiler.INPUT_SCHEMA
    for item in first["source_to_output"]:
        assert item["hash_domain"] == "canonical-json-block/v1"
        assert len(item["source_block_sha256"]) == 64
        assert len(item["output_block_sha256"]) == 64
        assert item["source_body_sha256"] == item["represented_body_sha256"]
    for result in (first, compiler.compile_context(value, profile="off")):
        assert result["retained_evidence"]
        assert all(isinstance(item, dict) for item in result["retained_evidence"])
        assert all({"id", "kind", "source_block_sha256", "body_sha256"} <= set(item)
                   for item in result["retained_evidence"])


@pytest.mark.parametrize("raw,kind", [
    ('{"schema":"summon.context-input/v1","schema":"summon.context-input/v1","blocks":[]}', "context_duplicate_key"),
    ('{"schema":"summon.context-input/v1","blocks":[],"n":NaN}', "context_nonfinite"),
])
def test_raw_parser_rejects_duplicate_keys_and_nonfinite_values(raw, kind):
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.parse_context_json(raw)
    assert raised.value.kind == kind


@pytest.mark.parametrize("raw,kind", [
    ('{"schema":"summon.context-input/v1","blocks":[],"n":1e400}', "context_nonfinite"),
    ("[" * 2000 + "0" + "]" * 2000, "context_malformed"),
])
def test_raw_parser_turns_overflow_and_extreme_nesting_into_typed_errors(raw, kind):
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.parse_context_json(raw)
    assert raised.value.kind == kind


def test_unicode_surrogates_and_extreme_mapping_nesting_are_typed_errors():
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.parse_context_json("\ud800")
    assert raised.value.kind == "context_malformed"
    nested = {}
    cursor = nested
    for _ in range(100):
        cursor["x"] = {}
        cursor = cursor["x"]
    value = sample(authority())
    value["extra"] = nested
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(value)
    assert raised.value.kind == "context_too_deep"


def test_duplicate_ids_malformed_shapes_and_nonfinite_nested_values_fail_closed():
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(authority("same"), authority("same", "again")))
    assert raised.value.kind == "context_duplicate_block"
    bad = artifact("a", "body")
    bad["immutable"] = False
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bad))
    nested = sample(authority())
    nested["blocks"][0]["body"] = "ok"
    nested["extra"] = float("nan")
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(nested)
    assert raised.value.kind in {"context_schema_unsupported", "context_nonfinite"}


def test_bounded_input_and_tail_and_unknown_profile_refuse_without_contact():
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(artifact("a", "x" * (compiler.MAX_BLOCK_BYTES + 1))))
    assert raised.value.kind == "context_too_large"
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(authority()), diagnostic_tail_bytes=compiler.MAX_DIAGNOSTIC_TAIL_BYTES + 1)
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(authority()), profile="lossy")
    assert raised.value.kind == "context_profile_unsupported"


def test_aggregate_mapping_body_budget_refuses_before_canonicalization(monkeypatch):
    value = sample(*(artifact(f"a{index}", "x" * compiler.MAX_BLOCK_BYTES)
                     for index in range(5)))

    def canonical_must_not_run(_value):
        raise AssertionError("oversized mapping reached canonical allocation")

    monkeypatch.setattr(compiler, "_canonical", canonical_must_not_run)
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(value)
    assert raised.value.kind == "context_too_large"


def test_authority_payload_and_diagnostic_contracts_are_strict():
    bad_authority = authority()
    bad_authority["externalize"] = True
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bad_authority))
    bad_diag = diagnostic(errors=["UPPERCASE"])
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bad_diag))
    bad_ordinary = {"id": "ordinary", "plane": "payload", "kind": "note", "body": "x", "extra": 1}
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bad_ordinary))
    bool_like = artifact("bool-like", "x")
    bool_like["externalize"] = 1
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bool_like))
    bool_like = artifact("stable-like", "x")
    bool_like["stable"] = 1
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(bool_like))


def test_diagnostic_truncation_requires_preceding_validated_report_and_receipt():
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(diagnostic("d", "verbose", ["tool_missing"])),
                                 diagnostic_tail_bytes=0)
    assert raised.value.kind == "diagnostic_representation_required"
    with pytest.raises(compiler.ContextCompileError):
        compiler.compile_context(sample(
            diagnostic("d", "verbose", ["tool_missing"],
                       receipt_proof=receipt_proof("later", "validated", "3")),
            report("later")), diagnostic_tail_bytes=0)
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(
            report(), diagnostic("d", "verbose", [], receipt_proof=receipt_proof())),
            diagnostic_tail_bytes=0)
    assert raised.value.kind == "diagnostic_representation_required"
    forged = receipt_proof()
    forged["report_body_sha256"] = "0" * 64
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(
            report(), diagnostic("d", "verbose", ["tool_missing"], receipt_proof=forged)),
            diagnostic_tail_bytes=0)
    assert raised.value.kind == "receipt_proof_invalid"
    wrong_report = compiler.make_receipt_proof("report", "different", "5" * 64)
    with pytest.raises(compiler.ContextCompileError) as raised:
        compiler.compile_context(sample(
            report(), diagnostic("d", "verbose", ["tool_missing"], receipt_proof=wrong_report)),
            diagnostic_tail_bytes=0)
    assert raised.value.kind == "diagnostic_representation_required"


def test_frozen_safe_and_off_compatibility_goldens():
    value = sample(authority("sys", "System\n"), artifact("a", "same"), artifact("b", "same"),
                   report(), diagnostic("diag", "one\ntwo\nthree", ["tool_missing"],
                                        receipt_proof=receipt_proof(receipt_byte="4")))
    safe = compiler.compile_context(value, diagnostic_tail_bytes=5)
    off = compiler.compile_context(value, profile="off")
    assert hashlib.sha256(safe["compiled_utf8"].encode()).hexdigest() == "14b31975fc5d1e22b1300c711ef234ae70b2f77c0e837ca3bca7fcb06177bb68"
    assert hashlib.sha256(off["compiled_utf8"].encode()).hexdigest() == "d61324ef8d23cb392a5fe1672382b0cd7da5bbeb380f12ab38b9a9a0e127053f"


def test_module_has_no_provider_process_network_or_credential_execution_surface():
    source = Path(compiler.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {"__future__", "base64", "hashlib", "json", "math", "re", "typing"}
    forbidden = {
        "subprocess", "socket", "requests", "http", "urllib", "asyncio", "os", "secrets",
        "sys", "importlib", "ctypes", "pathlib", "shutil", "pickle", "threading",
        "multiprocessing",
    }
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
    assert imports <= allowed
    assert not (imports & forbidden)
    forbidden_calls = {"open", "exec", "eval", "compile", "__import__", "input"}
    forbidden_attributes = {
        "open", "read", "read_bytes", "read_text", "write", "write_bytes", "write_text",
        "unlink", "remove", "rename", "replace", "system", "popen", "spawn", "run",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attributes
