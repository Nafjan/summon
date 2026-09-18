from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_details as details
import _workspace_canonical
import _deliberation_store
import _rundir
from _workspace_view import ViewScope, _opaque as view_opaque
from _workspace_entry import WorkspaceEntryError, WorkspaceHost
from test_workspace_entry import _free_port
from test_workspace_ui import login, request


def _scope(host, *, kind="workspace_assessment", state="completed", body=True):
    return {
        "schema": details.SCOPE_SCHEMA,
        "workspace_id": host.workspace_id,
        "run_id": host.run_id,
        "targets": [{"source_kind": kind, "source_key": "review-opaque-source",
                      "task_id": "main-1", "state": state, "label": "Review result",
                      "revision": 4, "body_available": body}],
    }


def _evidence_scope(host):
    scope = _scope(host)
    scope["targets"].append({"source_kind": "workspace_evidence",
                              "source_key": "artifact-evidence-source",
                              "task_id": "main-1", "state": "completed",
                              "label": "Artifact evidence", "revision": 5,
                              "body_available": True})
    return scope


def _record(binding):
    return {"source_kind": binding["source_kind"], "task_id": binding["task_id"],
            "state": binding["state"], "title": "Bound review",
            "revision": binding["revision"], "summary": "Synthetic safe metadata",
            "disposition": "continue", "criteria": [{"label": "Scope", "result": "pass"}],
            "body": "Private detail body is available only through the body scope."}


def _seed_canonical_review_and_decision(host):
    source = host.demo.source({"kind": "canonical-review-artifact", "value": "provider-free"})
    reference = host.demo.evidence(source, "main-1", "canonical-review-artifact", "artifact")
    assessment = {
        "protocol": details.protocol.PROTOCOL,
        "kind": "assessment", "workspace_id": host.workspace_id,
        "run_id": host.run_id, "assessment_id": "main-1-canonical-assessment",
        "goal_id": "verify-conductor-loop", "goal_revision": 1,
        "task_id": "main-1", "lane_id": "main-1", "supervisor_id": "scripted-conductor",
        "criterion_results": [{"criterion_id": "checked-computation", "result": "met",
                               "evidence_ids": [reference["id"]]}],
        "evidence": [reference], "limitations": ["Provider-free canonical fixture"],
        "handoff": "Continue from the durable review record",
        "disposition": "continue_main", "next_decision": "criteria_satisfied",
        "reason": "Canonical workspace review was recorded by the journal writer",
    }
    host.demo.record("workspace_assessed", {"assessment": assessment}, "canonical-assessment")
    decision = {"decision_id": "canonical-successor", "goal_revision": 1,
                "assessment_id": assessment["assessment_id"], "from_task_id": "main-1",
                "to_task_id": "main-2"}
    host.demo.record("workspace_next_lane_selected", decision, "canonical-decision")
    return reference


def test_canonical_task_presentation_identity_is_coherent_over_http(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(
            body_source_keys=["workspace_assessment:main-1-canonical-assessment"])
        token, _ = login(host.surface)
        status, _, raw = request(host.surface, token=token)
        assert status == 200
        view = json.loads(raw)
        task_id = view["assessments"][0]["task_id"]
        assert task_id in {item["id"] for item in view["tasks"]}
        assert task_id == view["decisions"][0]["from_task_id"]
        targets = view["details"]["targets"]
        canonical = {kind: next(item for item in targets if item["source_kind"] == kind)
                     for kind in ("workspace_assessment", "workspace_decision", "workspace_evidence")}
        for target in canonical.values():
            # This is the equality used by page.openDetails(selectedTask).
            assert target["task_id"] == task_id
            assert target["id"].startswith("detail_")
            assert target["id"] not in {item["id"] for item in view["tasks"]}
        assessment = canonical["workspace_assessment"]
        assert assessment["id"] == details._opaque(
            host.surface._key, host.workspace_id, host.run_id, "detail",
            "workspace_assessment:main-1-canonical-assessment")
        status, _, raw = request(host.surface, "GET", "/api/details?task=" + task_id, token=token)
        assert status == 200
        listed = json.loads(raw)
        assert {item["id"] for item in listed["targets"]} == {
            item["id"] for item in targets if item["task_id"] == task_id}
        for target in canonical.values():
            status, _, raw = request(
                host.surface, token=token, target="/api/details?target=" + target["id"]
                + "&source=" + target["source_kind"] + "&task=" + task_id)
            assert status == 200
            record = json.loads(raw)
            assert record["id"] == target["id"]
            assert record["task_id"] == task_id
            assert "body" not in record
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": assessment["id"], "source": "workspace_assessment",
                                          "task": task_id})
        assert status == 200
        assert json.loads(raw)["task_id"] == task_id
        assert json.loads(raw)["body"]
        # A coherent presentation handle still grants neither authentication
        # nor the independently installed body scope for another detail item.
        status, _, _ = request(host.surface, target="/api/details?target=" + assessment["id"])
        assert status == 401
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": canonical["workspace_decision"]["id"],
                                          "source": "workspace_decision", "task": task_id})
        assert status == 403
        assert json.loads(raw)["error"] == "detail_body_scope_required"
    finally:
        host.stop()


def test_canonical_task_presentation_rejects_wrong_task_scope_and_type_before_reader(
        tmp_path, monkeypatch):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    calls = []
    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(
            body_source_keys=["workspace_assessment:main-1-canonical-assessment"])
        token, _ = login(host.surface)
        view = json.loads(request(host.surface, token=token)[2])
        task_id = view["assessments"][0]["task_id"]
        other_task = view["decisions"][0]["to_task_id"]
        assert other_task != task_id
        assert other_task in {item["id"] for item in view["tasks"]}
        target = next(item for item in view["details"]["targets"]
                      if item["source_kind"] == "workspace_assessment")

        def forbidden_reader(binding):
            calls.append(binding)
            raise AssertionError("refused handle reached a canonical reader")

        monkeypatch.setattr(host._workspace_details, "_resolve", forbidden_reader)
        monkeypatch.setattr(host._workspace_details, "_resolve_body", forbidden_reader)
        wrong_handles = [other_task,
            view_opaque(ViewScope("other-workspace", host.run_id, host.surface._key), "task", "main-1"),
            view_opaque(ViewScope(host.workspace_id, "other-run", host.surface._key), "task", "main-1"),
            view_opaque(ViewScope(host.workspace_id, host.run_id, b"x" * 32), "task", "main-1"),
            details._opaque(host.surface._key, host.workspace_id, host.run_id, "task", "main-1")]
        for wrong_task in wrong_handles:
            for path in ("/api/details?task=" + wrong_task,
                         "/api/details?target=" + target["id"] + "&task=" + wrong_task):
                status, _, raw = request(host.surface, token=token, target=path)
                assert status == 409
                assert json.loads(raw)["error"] == "detail_task_refused"
            status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                     payload={"target": target["id"], "source": "workspace_assessment",
                                              "task": wrong_task})
            assert status == 409
            assert json.loads(raw)["error"] == "detail_task_refused"
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"]
                                 + "&source=workspace_decision&task=" + task_id)
        assert status == 409
        assert json.loads(raw)["error"] == "detail_type_refused"
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": target["id"], "source": "workspace_decision",
                                          "task": task_id})
        assert status == 409
        assert json.loads(raw)["error"] == "detail_type_refused"
        assert calls == []
        host.revoke_workspace_details()
        status, _, raw = request(host.surface, token=token, target="/api/details?task=" + task_id)
        assert status == 403
        assert json.loads(raw)["error"] == "detail_scope_required"
        assert calls == []
    finally:
        host.stop()


def _deliberation_receipt(run_id):
    import hashlib
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "question_sha256": hashlib.sha256(b"canonical deliberation").hexdigest(),
        "decision_id": "canonical-decision", "seat_ids": ["one", "two"],
        "option_ids": ["yes", "no"], "quorum_rule": "all", "max_attempts": 2,
        "require_human_approval": False, "rounds": 1,
        "deadline_unix_ms": 4_000_000_000_000,
    }


def _seed_deliberation_terminal_run(root, run_id, terminal_state):
    receipt = _deliberation_receipt(run_id)
    path, owner = _deliberation_store.initialize_run(str(root), receipt)
    generation = owner.generation

    def append(event, **fields):
        _rundir.journal_append(path, {
            "event": event, "schema_version": 1, "generation": generation,
            **fields,
        }, owner=owner)

    append("state_transition", **{
        "from": "PREPARED", "to": "RUNNING", "reason": "started",
    })
    if terminal_state == "DECIDED":
        for ordinal, seat in enumerate(("one", "two")):
            turn_id = "turn-" + seat + "-" + str(ordinal)
            attempt_id = "attempt-" + seat + "-" + str(ordinal)
            append("turn_prepared", decision_id="canonical-decision", seat_id=seat,
                   turn_id=turn_id, turn_ordinal=ordinal, request_digest="a" * 64)
            append("attempt_started", attempt_id=attempt_id,
                   decision_id="canonical-decision", seat_id=seat,
                   turn_id=turn_id, turn_ordinal=ordinal, launch_spec_sha256="b" * 64)
            append("attempt_finished", attempt_id=attempt_id,
                   launch_spec_sha256="b" * 64, transport_ok=True, exit_code=0,
                   timed_out=False, parser_valid=True, ballot_valid=True)
            append("ballot_accepted", attempt_id=attempt_id, seat_id=seat,
                   turn_id=turn_id, turn_ordinal=ordinal,
                   decision_id="canonical-decision", decision="vote", option_id="yes")
        append("state_transition", **{
            "from": "RUNNING", "to": "DECIDED", "reason": "consensus",
            "decision_option": "yes",
        })
    else:
        reasons = {"UNRESOLVED": "max_rounds", "CANCELLED": "cancelled"}
        append("state_transition", **{
            "from": "RUNNING", "to": terminal_state,
            "reason": reasons[terminal_state],
        })
    _rundir.release_owner(owner)
    return path


def test_typed_reader_maps_reject_unknown_source_kinds(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        scope = _scope(host)
        with pytest.raises(WorkspaceEntryError, match="detail_reader_invalid"):
            host.install_workspace_details(scope, readers={"unknown_kind": _record})
        with pytest.raises(WorkspaceEntryError, match="detail_reader_invalid"):
            host.install_workspace_details(scope, readers={"workspace_assessment": _record},
                                           body_scope=scope,
                                           body_readers={"unknown_kind": _record})
    finally:
        host.stop()


def test_canonical_readers_follow_workspace_council_and_deliberation_sources(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    council_path = tmp_path / "council" / "council-1"
    council_path.mkdir(parents=True)
    (council_path / "receipt.json").write_text(json.dumps({
        "mode": "council", "run_id": "council-1", "question": "Canonical review?",
        "status": "success", "council_state": "final",
    }), encoding="utf-8")
    deliberation_root = tmp_path / "deliberations"
    path, owner = _deliberation_store.initialize_run(
        str(deliberation_root), _deliberation_receipt("deliberation-1"))
    _rundir.release_owner(owner)
    try:
        reference = _seed_canonical_review_and_decision(host)
        installed = host.install_canonical_workspace_details(
            council_sources=[{"source_key": "council-1", "task_id": "main-1",
                              "root": str(council_path), "run_id": "council-1",
                              "revision": 1, "state": "completed", "label": "Council review"}],
            deliberation_sources=[{"source_key": "deliberation-1", "task_id": "main-1",
                                   "root": str(deliberation_root), "run_id": "deliberation-1",
                                   "revision": 1, "state": "pending", "label": "Deliberation pending"}],
            body_source_keys=["workspace_assessment:main-1-canonical-assessment",
                              "workspace_decision:canonical-successor",
                              "workspace_evidence:" + reference["id"],
                              "council_review:council-1",
                              "deliberation_pending:deliberation-1"],
        )
        assert installed["canonical"] is True
        assert installed["workspace_sources"] >= 3
        assert installed["external_sources"] == 2
        token, _ = login(host.surface)
        listed = json.loads(request(host.surface, token=token, target="/api/details")[2])
        kinds = {item["source_kind"] for item in listed["targets"]}
        assert {"workspace_assessment", "workspace_decision", "workspace_evidence",
                "council_review", "deliberation_pending"} <= kinds
        for kind in ("workspace_assessment", "workspace_decision", "workspace_evidence",
                     "council_review", "deliberation_pending"):
            target = next(item for item in listed["targets"] if item["source_kind"] == kind)
            status, _, raw = request(
                host.surface, token=token,
                target="/api/details?target=" + target["id"]
                + "&source=" + kind + "&task=" + target["task_id"])
            assert status == 200, (kind, raw.decode(errors="replace"))
            detail = json.loads(raw)
            assert detail["source_kind"] == kind
            assert detail["title"]
            body_target = next(item for item in listed["targets"]
                               if item["source_kind"] == kind
                               and item["body_available"] is True)
            status, _, raw = request(
                host.surface, "POST", "/api/details/body", token=token,
                payload={"target": body_target["id"], "source": kind,
                         "task": body_target["task_id"]})
            assert status == 200, (kind, raw.decode(errors="replace"))
            assert json.loads(raw)["body"]
        # The browser can never substitute a task or source kind for a bound
        # canonical record, and the resolver is not reached for the refusal.
        target = next(item for item in listed["targets"] if item["source_kind"] == "council_review")
        status, _, _ = request(host.surface, token=token,
                               target="/api/details?target=" + target["id"]
                               + "&source=workspace_decision&task=" + target["task_id"])
        assert status == 409
    finally:
        host.stop()


def test_canonical_scope_is_metadata_only_without_explicit_body_sources(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        reference = _seed_canonical_review_and_decision(host)
        installed = host.install_canonical_workspace_details()
        assert installed["canonical"] is True
        token, _ = login(host.surface)
        listed = json.loads(request(host.surface, token=token, target="/api/details")[2])
        evidence_target = next(item for item in listed["targets"]
                               if item["source_kind"] == "workspace_evidence")
        assert evidence_target["body_available"] is False
        status, _, _ = request(
            host.surface, "POST", "/api/details/body", token=token,
            payload={"target": evidence_target["id"], "source": "workspace_evidence",
                     "task": evidence_target["task_id"]})
        assert status == 403
    finally:
        host.stop()


def test_canonical_external_binding_refuses_stale_state_or_revision(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    council_path = tmp_path / "council" / "council-1"
    council_path.mkdir(parents=True)
    (council_path / "receipt.json").write_text(json.dumps({
        "mode": "council", "run_id": "council-1", "status": "success",
        "council_state": "final",
    }), encoding="utf-8")
    try:
        _seed_canonical_review_and_decision(host)
        with pytest.raises(WorkspaceEntryError,
                           match="canonical_binding_mismatch"):
            host.install_canonical_workspace_details(council_sources=[{
                "source_key": "council-1", "task_id": "main-1",
                "root": str(council_path), "run_id": "council-1",
                "revision": 99, "state": "pending", "label": "Council review",
            }])
    finally:
        host.stop()


def test_canonical_external_read_refuses_source_revision_drift(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    council_path = tmp_path / "council" / "council-1"
    council_path.mkdir(parents=True)
    receipt_path = council_path / "receipt.json"
    receipt_path.write_text(json.dumps({
        "mode": "council", "run_id": "council-1", "status": "success",
        "council_state": "final", "generation": 1,
    }), encoding="utf-8")
    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(
            council_sources=[{"source_key": "council-1", "task_id": "main-1",
                              "root": str(council_path), "run_id": "council-1",
                              "revision": 1, "state": "completed", "label": "Council review"}],
            body_source_keys=["council_review:council-1"],
        )
        receipt_path.write_text(json.dumps({
            "mode": "council", "run_id": "council-1", "status": "blocked",
            "council_state": "blocked", "generation": 2,
        }), encoding="utf-8")
        token, _ = login(host.surface)
        listed = json.loads(request(host.surface, token=token, target="/api/details")[2])
        target = next(item for item in listed["targets"] if item["source_kind"] == "council_review")
        status, _, raw = request(
            host.surface, token=token,
            target="/api/details?target=" + target["id"]
            + "&source=council_review&task=" + target["task_id"])
        assert status == 409
        assert json.loads(raw)["error"] == "canonical_binding_stale"
    finally:
        host.stop()


def test_canonical_council_http_body_uses_validated_receipt_after_mutation(
        tmp_path, monkeypatch):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    council_path = tmp_path / "council" / "council-1"
    council_path.mkdir(parents=True)
    receipt_path = council_path / "receipt.json"
    receipt_path.write_text(json.dumps({
        "mode": "council", "run_id": "council-1", "question": "before mutation",
        "status": "success", "council_state": "final", "generation": 1,
    }), encoding="utf-8")
    mutated = False
    original_read = _workspace_canonical._council._read_council_receipt

    def read_then_mutate(root, run_id):
        nonlocal mutated
        receipt = original_read(root, run_id)
        if not mutated:
            mutated = True
            receipt_path.write_text(json.dumps({
                "mode": "council", "run_id": "council-1", "question": "after mutation",
                "status": "blocked", "council_state": "blocked", "generation": 2,
            }), encoding="utf-8")
        return receipt

    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(
            council_sources=[{"source_key": "council-1", "task_id": "main-1",
                              "root": str(council_path), "run_id": "council-1",
                              "revision": 1, "state": "completed", "label": "Council review"}],
            body_source_keys=["council_review:council-1"],
        )
        monkeypatch.setattr(_workspace_canonical._council,
                            "_read_council_receipt", read_then_mutate)
        token, _ = login(host.surface)
        target = next(item for item in json.loads(
            request(host.surface, token=token, target="/api/details")[2])["targets"]
                      if item["source_kind"] == "council_review")
        status, _, raw = request(
            host.surface, "POST", "/api/details/body", token=token,
            payload={"target": target["id"], "source": "council_review",
                     "task": target["task_id"]})
        assert status == 200
        body = json.loads(json.loads(raw)["body"])
        assert body["question"] == "before mutation"
        assert body["status"] == "success"
        assert mutated is True
    finally:
        host.stop()


def test_canonical_deliberation_http_body_uses_validated_status_after_mutation(
        tmp_path, monkeypatch):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    deliberation_root = tmp_path / "deliberations"
    path, owner = _deliberation_store.initialize_run(
        str(deliberation_root), _deliberation_receipt("deliberation-1"))
    _rundir.release_owner(owner)
    mutated = False
    original_inspect = _workspace_canonical._deliberation_store.inspect_run

    def inspect_then_mutate(root, run_id):
        nonlocal mutated
        status = original_inspect(root, run_id)
        if not mutated:
            mutated = True
            successor = _rundir.acquire_owner(path, 600)
            try:
                _rundir.journal_append(path, {
                    "event": "state_transition", "schema_version": 1,
                    "generation": successor.generation, "from": "PREPARED",
                    "to": "RUNNING", "reason": "started",
                }, owner=successor)
            finally:
                _rundir.release_owner(successor)
        return status

    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(
            deliberation_sources=[{"source_key": "deliberation-1", "task_id": "main-1",
                                   "root": str(deliberation_root), "run_id": "deliberation-1",
                                   "revision": 1, "state": "pending",
                                   "label": "Deliberation pending"}],
            body_source_keys=["deliberation_pending:deliberation-1"],
        )
        monkeypatch.setattr(_workspace_canonical._deliberation_store,
                            "inspect_run", inspect_then_mutate)
        token, _ = login(host.surface)
        target = next(item for item in json.loads(
            request(host.surface, token=token, target="/api/details")[2])["targets"]
                      if item["source_kind"] == "deliberation_pending")
        status, _, raw = request(
            host.surface, "POST", "/api/details/body", token=token,
            payload={"target": target["id"], "source": "deliberation_pending",
                     "task": target["task_id"]})
        assert status == 200
        body = json.loads(json.loads(raw)["body"])
        assert body["projection"]["state"] == "prepared"
        assert body["pending_commands"] == 0
        assert mutated is True
    finally:
        host.stop()


def test_canonical_deliberation_terminal_outcomes_are_truthful(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    deliberation_root = tmp_path / "deliberations"
    cases = (("DECIDED", "decision_recorded"),
             ("UNRESOLVED", "decision_unresolved"),
             ("CANCELLED", "decision_cancelled"))
    sources = []
    for terminal_state, _expected_decision in cases:
        run_id = "deliberation-" + terminal_state.lower()
        _seed_deliberation_terminal_run(deliberation_root, run_id, terminal_state)
        sources.append({"source_key": run_id, "task_id": "main-1",
                        "root": str(deliberation_root), "run_id": run_id,
                        "revision": 1, "state": "completed",
                        "label": "Deliberation " + terminal_state})
    try:
        _seed_canonical_review_and_decision(host)
        host.install_canonical_workspace_details(deliberation_sources=sources)
        token, _ = login(host.surface)
        targets = json.loads(request(
            host.surface, token=token, target="/api/details")[2])["targets"]
        for terminal_state, expected_decision in cases:
            target = next(item for item in targets
                          if item["label"] == "Deliberation " + terminal_state)
            status, _, raw = request(
                host.surface, token=token,
                target="/api/details?target=" + target["id"]
                + "&source=deliberation_decision&task=" + target["task_id"])
            assert status == 200
            detail = json.loads(raw)
            assert detail["source_kind"] == "deliberation_decision"
            assert detail["state"] == "completed"
            assert detail["decision"] == expected_decision
    finally:
        host.stop()


def test_canonical_body_text_allows_multiline_and_rejects_controls():
    assert _workspace_canonical._body_text("line one\nline two\tready") == "line one\nline two\tready"
    with pytest.raises(_workspace_canonical.CanonicalDetailError,
                       match="canonical_detail_unavailable"):
        _workspace_canonical._body_text("bad\x00body")
    with pytest.raises(_workspace_canonical.CanonicalDetailError,
                       match="canonical_detail_unavailable"):
        _workspace_canonical._body_text("bad\u00adbody")
    with pytest.raises(_workspace_canonical.CanonicalDetailError,
                       match="canonical_detail_unavailable"):
        _workspace_canonical._safe_json({"value": float("nan")})


def test_typed_details_are_scoped_and_body_is_separate(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    try:
        scope = _scope(host)
        body_scope = {**scope, "targets": [{**scope["targets"][0], "body_available": True}]}
        host.install_workspace_details(scope, resolve=_record, body_scope=body_scope,
                                       resolve_body=_record)
        token, _ = login(host.surface)
        status, _, raw = request(host.surface, token=token)
        assert status == 200
        view = json.loads(raw)
        assert view["details"]["available"] is True
        target = view["details"]["targets"][0]
        assert "review-opaque-source" not in raw.decode()
        status, _, raw = request(host.surface, token=token, target="/api/details")
        assert status == 200
        listed = json.loads(raw)
        assert listed["schema"] == details.SCHEMA
        assert listed["targets"][0]["id"] == target["id"]
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"]
                                 + "&source=workspace_assessment&task=" + target["task_id"])
        assert status == 200
        record = json.loads(raw)
        assert record["source_kind"] == "workspace_assessment"
        assert record["task_id"] == target["task_id"]
        assert "main-1" not in raw.decode()
        assert "body" not in record
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": target["id"], "source": "workspace_assessment",
                                          "task": target["task_id"]})
        assert status == 200
        assert json.loads(raw)["body"].startswith("Private detail")
    finally:
        host.stop()


def test_evidence_is_a_distinct_typed_detail_and_reader_is_selected_by_kind(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    calls = []
    try:
        scope = _evidence_scope(host)
        def review(binding):
            calls.append(binding["source_kind"])
            return _record(binding)
        def evidence(binding):
            calls.append(binding["source_kind"])
            return {"source_kind": binding["source_kind"], "task_id": binding["task_id"],
                    "state": binding["state"], "title": "Artifact evidence",
                    "revision": binding["revision"], "summary": "Bound safe evidence metadata",
                    "body": "Evidence body is separately scoped."}
        host.install_workspace_details(scope, readers={"workspace_assessment": review,
                                                       "workspace_evidence": evidence},
                                       body_scope=scope, body_readers={"workspace_evidence": evidence})
        token, _ = login(host.surface)
        listed = json.loads(request(host.surface, token=token, target="/api/details")[2])
        evidence_target = next(item for item in listed["targets"]
                               if item["source_kind"] == "workspace_evidence")
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + evidence_target["id"]
                                 + "&source=workspace_evidence&task=" + evidence_target["task_id"])
        assert status == 200
        assert json.loads(raw)["source_kind"] == "workspace_evidence"
        assert calls == ["workspace_evidence"]
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": evidence_target["id"],
                                          "source": "workspace_evidence",
                                          "task": evidence_target["task_id"]})
        assert status == 200
        assert json.loads(raw)["body"].startswith("Evidence body")
        assert calls == ["workspace_evidence", "workspace_evidence"]
    finally:
        host.stop()


def test_wrong_type_task_revocation_and_metadata_only_fail_before_resolver(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    calls = []
    try:
        scope = _scope(host, kind="deliberation_pending", state="pending", body=False)
        body_scope = {**scope, "targets": [{**scope["targets"][0], "body_available": False}]}
        def resolve(binding):
            calls.append(binding)
            return _record(binding)
        host.install_workspace_details(scope, resolve=resolve, body_scope=body_scope,
                                       resolve_body=resolve)
        token, _ = login(host.surface)
        target = json.loads(request(host.surface, token=token, target="/api/details")[2])["targets"][0]
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"] + "&source=deliberation_decision")
        assert status == 409
        assert json.loads(raw)["error"] == "detail_type_refused"
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"] + "&task=task_wrong")
        assert status == 409
        assert json.loads(raw)["error"] == "detail_task_refused"
        status, _, raw = request(host.surface, "POST", "/api/details/body", token=token,
                                 payload={"target": target["id"]})
        assert status == 403
        assert json.loads(raw)["error"] == "detail_body_scope_required"
        assert calls == []
        host.revoke_workspace_details()
        status, _, raw = request(host.surface, token=token, target="/api/details")
        assert status == 403
        assert json.loads(raw)["error"] == "detail_scope_required"
    finally:
        host.stop()


def test_stale_binding_and_mismatched_resolver_fail_closed(tmp_path):
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    calls = []
    try:
        scope = _scope(host, kind="workspace_evidence", state="stale", body=False)
        original = json.loads(json.dumps(scope))
        def resolve(binding):
            calls.append(binding)
            return {"source_kind": binding["source_kind"], "task_id": binding["task_id"],
                    "state": "completed", "title": "Wrong state", "revision": 99}
        host.install_workspace_details(scope, resolve=resolve)
        scope["targets"][0]["source_kind"] = "council_review"
        token, _ = login(host.surface)
        target = json.loads(request(host.surface, token=token, target="/api/details")[2])["targets"][0]
        assert target["source_kind"] == original["targets"][0]["source_kind"]
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"])
        assert status == 409
        assert json.loads(raw)["error"] == "detail_binding_stale"
        assert calls == []
    finally:
        host.stop()

    host = WorkspaceHost(str(tmp_path / "runs-b"), "run-b", _free_port(), mode="demo-create")
    host.start()
    try:
        scope = _scope(host, kind="workspace_evidence", state="completed", body=False)
        host.install_workspace_details(scope, resolve=resolve)
        token, _ = login(host.surface)
        target = json.loads(request(host.surface, token=token, target="/api/details")[2])["targets"][0]
        status, _, raw = request(host.surface, token=token,
                                 target="/api/details?target=" + target["id"])
        assert status == 503
        assert json.loads(raw)["error"] == "detail_unavailable"
        assert len(calls) == 1
    finally:
        host.stop()


def test_real_browser_keyboard_detail_navigation(tmp_path):
    from urllib.parse import urlsplit

    playwright = __import__("pytest").importorskip("playwright.sync_api")
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    forbidden = []
    try:
        scope = _evidence_scope(host)
        host.install_workspace_details(
            scope, readers={"workspace_assessment": _record,
                            "workspace_evidence": _record},
            body_scope=scope, body_readers={"workspace_assessment": _record,
                                            "workspace_evidence": _record})
        with playwright.sync_playwright() as p:
            browser_options = {"headless": True, "args": [
                "--disable-background-networking", "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]}
            executable = os.environ.get("SUMMON_UI_CHROMIUM_EXECUTABLE")
            if executable:
                browser_options["executable_path"] = executable
            browser = p.chromium.launch(**browser_options)
            context = browser.new_context(viewport={"width": 1100, "height": 800},
                                          service_workers="block")
            origin = urlsplit(host.surface.url)
            def bounded_route(route):
                target = urlsplit(route.request.url)
                if (target.scheme, target.hostname, target.port) != (origin.scheme, origin.hostname, origin.port):
                    forbidden.append("non-owned-page-request")
                    route.abort("blockedbyclient")
                else:
                    route.continue_()
            def bounded_websocket(websocket):
                forbidden.append("websocket-request")
                websocket.close()
            context.route("**/*", bounded_route)
            context.route_web_socket("**/*", bounded_websocket)
            page = context.new_page()
            page.set_default_timeout(5000)
            page.goto(host.surface.url, wait_until="domcontentloaded")
            page.locator("#code").fill(host.surface.bootstrap_code)
            page.locator("#login button").press("Enter")
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#details-open").focus()
            page.keyboard.press("Enter")
            page.locator("#details-drawer").wait_for(state="visible")
            page.get_by_role("button", name="Review result / workspace assessment").press("Enter")
            page.locator("#details-body").get_by_role("heading", name="Bound review").wait_for()
            assert "Private detail body" not in page.locator("#details-body").inner_text()
            page.get_by_role("button", name="Open separately authorized body").press("Enter")
            page.locator("#details-body pre").wait_for()
            assert "Private detail body" in page.locator("#details-body pre").inner_text()
            assert page.get_by_role("button", name="Artifact evidence / workspace evidence").is_visible()
            assert forbidden == []
            evidence_dir = os.environ.get("SUMMON_U01_BROWSER_EVIDENCE_DIR")
            if evidence_dir:
                evidence_root = Path(evidence_dir)
                evidence_root.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(evidence_root / "u01-details-evidence.png"))
                (evidence_root / "receipt.json").write_text(json.dumps({
                    "schema": "summon.workspace.u01-browser-evidence/v1",
                    "loopback_only": True, "provider_contacted": False,
                    "typed_review_opened": True, "typed_evidence_listed": True,
                    "body_scope_separate": True,
                }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            page.locator("#details-close").press("Enter")
            page.locator("#details-drawer").wait_for(state="hidden")
            context.close()
            browser.close()
            assert forbidden == []
    finally:
        host.stop()
