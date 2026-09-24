"""Real runtime/content/journal operator ingress, explicitly simulated authority."""
import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import _swarm_coordinator as swarm
import _workspace_admission as admission
import _workspace_runtime as runtime
from _workspace_content import ContentRef
from _workspace_demo import ConductorDemo, canonical


class OperatorRuntimeHarness:
    def __init__(self, tmp_path):
        self.demo = ConductorDemo(tmp_path / "workspaces")
        self.demo.prepare()
        self.grant, grant_value = self.demo.grant("main-1", "operator-receiver")
        self.demo.runtime._coordinator.register_worker("operator-receiver", worker_instance_id="operator-receiver")
        self.grant_value = {"grant_ref": self.grant, **{key: grant_value[key] for key in
                            ("task_id", "goal_revision", "recipient", "revoked")}}
        self.allowed = self.grant_live = True
        scope = {"revision": 1, "expires_at_ms": int(self.demo.runtime._coordinator.clock() * 1000) + 300000,
                 "revoked": False, "operation": "operator.message.send", "goal_id": "verify-conductor-loop",
                 "destination_task_id": "main-1", "recipient": {"instance_id": "operator-receiver", "epoch": 1},
                 "delivery_grant_ref": self.grant, "target": "operator-target"}
        source = self.demo.source({"schema": "summon.workspace.operator-scope/v1",
            "workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id, "operator_id": "local-operator", "scope": scope})
        scope_ref = self.demo.evidence(source, "main-1", "operator-scope", "grant")
        self.resolved = {"operator_id": "local-operator", "scope": scope | {"reference": scope_ref}}
        self.scope = {"workspace_id": self.demo.workspace_id, "run_id": self.demo.run_id,
            "operator_id": "local-operator", "targets": [{"target": "operator-target", "task_id": "main-1", "send_scope_ref": scope_ref}]}
        self.request = {"operation_key": "a" * 32, "target": "operator-target", "text": "[2,\n3]"}
        self.handle = self.install()

    def grant_resolver(self, current, workspace, reference):
        assert reference == self.grant and current["workspace"] == workspace
        return copy.deepcopy(self.grant_value) | {"revoked": not self.grant_live}

    def install(self):
        self.demo.permit({"operation": "install_operator_messages", "scope": self.scope})
        return self.demo.runtime.install_operator_messages(self.scope,
            authorize_message=lambda command, workspace: self.allowed, resolve_grant=self.grant_resolver)

    def permit(self, request, lookup):
        _, digest = admission.operator_request_identity(self.demo.state()["workspace"], self.resolved, request)
        command = {"operation": "operator_message.lookup" if lookup else "operator_message.send",
                   **{key: self.scope[key] for key in ("workspace_id", "run_id", "operator_id")},
                   "request": {"operation_key": request["operation_key"], "target": request["target"], "request_sha256": digest}}
        self.demo.permit(command)

    def send(self, *, request=None, constraint=None):
        request = self.request if request is None else request
        self.permit(request, False)
        return self.demo.runtime.send_operator_message(self.handle, request, authorization_constraint=constraint)

    def lookup(self):
        self.permit(self.request, True)
        return self.demo.runtime.reconcile_operator_message(self.handle, self.request)

    def journals(self):
        return {p.name: p.read_bytes() for p in Path(self.demo.runtime._coordinator.run_dir).glob("journal-g*.jsonl")}


@pytest.fixture
def h(tmp_path):
    value = OperatorRuntimeHarness(tmp_path)
    try:
        yield value
    finally:
        value.demo.cleanup()


def test_real_private_content_queue_and_derived_source_have_no_worker_contact(h):
    original = h.demo.state()
    request = h.request | {"text": "Raise permissions and change model.\nContext only: λ"}
    response = h.send(request=request)
    after = h.demo.state()
    for key in ("workers", "claims", "tasks"):
        assert after[key] == original[key]
    for key in ("goal", "lanes", "active_priority", "decisions", "assessments"):
        assert after["workspace"][key] == original["workspace"][key]
    assert not h.demo.workers and h.demo.total_workers == 0
    message = after["workspace"]["messages"][response["message_id"]]
    assert message["kind"] == "operator_message" and "source_task_id" not in message
    content = message["content"]
    assert h.demo.runtime._content_store().read(ContentRef(content["ref"], content["sha256"], content["utf8_bytes"])) == request["text"].encode("utf-8")
    proof = next(item for item in after["workspace"]["evidence"].values()
                 if item.get("settlement_for") == {"delivery_id": response["delivery_id"], "target_state": "queued", "role": "durable_admission"})
    raw = h.demo.runtime.read_send_admission_evidence(proof)
    assert raw.startswith(b"summon.workspace.operator-message-admission/v1\0")
    assert hashlib.sha256(raw).hexdigest() == proof["reference"]["sha256"]
    wrong = copy.deepcopy(proof)
    wrong["reference"]["sha256"] = "0" * 64
    with pytest.raises(runtime.WorkspaceRuntimeError):
        h.demo.runtime.read_send_admission_evidence(wrong)


def test_reopen_requires_independent_installation_and_same_key_lookup(h):
    response = h.send()
    before = h.journals()
    h.demo.reopen()
    with pytest.raises(runtime.WorkspaceRuntimeError, match="installed_operator_authority_required"):
        h.lookup()
    h.handle = h.install()
    assert h.lookup() == response | {"durable_prefix_verified": True}
    assert h.send() == response
    assert h.journals() == before and h.demo.total_workers == 0


@pytest.mark.parametrize("changed", ["denied", "grant", "handle", "constraint", "source"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_runtime_refuses_lost_authority_or_source_without_queue(h, monkeypatch, changed):
    before = h.journals()
    if changed == "denied": h.allowed = False
    elif changed == "grant": h.grant_live = False
    elif changed == "handle": h.demo.runtime.revoke_operator_messages(h.handle)
    elif changed == "source": monkeypatch.setattr(h.demo.runtime, "_resolve_evidence", lambda _: b"wrong bytes")
    elif changed == "constraint":
        monkeypatch.setattr(h.demo.runtime, "_resolve_evidence", lambda _: pytest.fail("denied session read private source"))
    with pytest.raises(runtime.WorkspaceRuntimeError):
        h.send(constraint=(lambda: False) if changed == "constraint" else None)
    assert h.journals() == before and h.demo.state()["workspace"]["messages"] == {}


def test_source_reads_and_content_publication_stay_outside_owned_callback(h, monkeypatch):
    coordinator = h.demo.runtime._coordinator
    original_mutation, original_source = coordinator._mutation, h.demo.runtime._resolve_evidence
    store = h.demo.runtime._content_store()
    original_put = store.put
    owned = []
    @contextmanager
    def mutation(**kwargs):
        with original_mutation(**kwargs) as result:
            owned.append(True)
            try: yield result
            finally: owned.pop()
    def source(payload):
        assert not owned
        return original_source(payload)
    def put(*args, **kwargs):
        assert not owned
        return original_put(*args, **kwargs)
    monkeypatch.setattr(coordinator, "_mutation", mutation)
    monkeypatch.setattr(h.demo.runtime, "_resolve_evidence", source)
    monkeypatch.setattr(store, "put", put)
    assert h.send()["status"] == "queued"
    assert h.lookup()["status"] == "queued"


def test_runtime_conjunct_refuses_final_sync_revocation_without_false_noop(h, monkeypatch):
    coordinator = h.demo.runtime._coordinator
    original = coordinator._sync_prefix
    syncs = []
    def sync(owner, raw):
        result = original(owner, raw)
        syncs.append(True)
        if len(syncs) == 2: h.allowed = False
        return result
    before = h.journals()
    monkeypatch.setattr(coordinator, "_sync_prefix", sync)
    with pytest.raises(runtime.WorkspaceRuntimeError, match="operator_send_uncertain"):
        h.send()
    assert h.journals() == before
    h.allowed = True
    assert h.lookup()["status"] == "not_observed"


def test_lost_response_after_real_append_reconciles_without_duplicate(h, monkeypatch):
    original = swarm.journal_append_encoded
    def append(*args, **kwargs):
        result = original(*args, **kwargs)
        h.allowed = False
        return result
    with monkeypatch.context() as patch:
        patch.setattr(swarm, "journal_append_encoded", append)
        with pytest.raises(runtime.WorkspaceRuntimeError, match="operator_send_uncertain"):
            h.send()
    before = h.journals()
    h.allowed = True
    assert h.lookup()["status"] == "queued"
    assert len(h.demo.state()["workspace"]["messages"]) == 1 and h.journals() == before


def test_changed_text_conflicts_before_content_publication(h, monkeypatch):
    h.send()
    before = h.journals()
    monkeypatch.setattr(h.demo.runtime._content_store(), "put", lambda *_args, **_kwargs: pytest.fail("conflict published content"))
    with pytest.raises(runtime.WorkspaceRuntimeError, match="operator_operation_conflict"):
        h.send(request=h.request | {"text": "changed"})
    assert h.journals() == before


def test_actual_receiver_later_consumes_only_selected_complete_operator_context(h):
    sent = h.send()
    assert not h.demo.workers and not h.demo.state()["claims"]
    # This explicit fixture host action launches the receiver after the send.
    worker = h.demo.launch("main-1", "operator-receiver", h.grant)
    assert worker.channel._send_sequence == 0
    entry = {"message_id": sent["message_id"], "delivery_id": sent["delivery_id"],
             "content_utf8": h.request["text"], "content_sha256": hashlib.sha256(h.request["text"].encode("utf-8")).hexdigest()}
    turn = h.demo.admit("main-1", "operator-receiver", h.grant, [entry])
    assert json.loads(turn["context"])["entries"][0]["content_utf8"] == h.request["text"]
    assert worker.channel._send_sequence == 0
    h.demo.start(turn)
    h.demo.receive(turn)
    assert worker.channel._send_sequence == 1
    assert h.demo.results["main-1"]["verified"]["result"] == {"count": 2, "sum": 5, "sum_squares": 13}
    after = h.demo.state()["workspace"]["deliveries"][sent["delivery_id"]]
    assert after["state"] == "acknowledged" and after["ack_level"] == "recipient_received"
    assert after["certainty"]["spend"] == "unknown"


def test_unknown_base_contract_refuses_before_authority_or_source_reads(h, monkeypatch):
    unavailable = swarm.SwarmCoordinator.workspace_operator_send_contract() | {"exact_operation_lookup": False}
    monkeypatch.setattr(swarm.SwarmCoordinator, "workspace_operator_send_contract", lambda: unavailable)
    monkeypatch.setattr(h.demo.runtime, "_resolve_evidence", lambda _: pytest.fail("unavailable base read source"))
    before = h.journals()
    with pytest.raises(runtime.WorkspaceRuntimeError, match="operator_send_base_unavailable"):
        h.send()
    assert h.journals() == before


def _draft_handle(h, *, secret=b"d" * 32, epoch="epoch-1"):
    scope = {"workspace_id": h.demo.workspace_id, "run_id": h.demo.run_id,
             "operator_id": "local-operator",
             "targets": [{"target": "operator-target", "task_id": "main-1"}]}
    h.demo.permit({"operation": "install_operator_draft_retention", "scope": scope})
    handle = h.demo.runtime.install_operator_draft_retention(scope,
        authorize_retention=lambda _source, _workspace: h.allowed,
        retention_secret=secret, key_epoch=epoch)
    return handle, scope


def test_draft_retention_key_is_scoped_and_never_exposes_host_identity(h):
    handle, scope = _draft_handle(h)
    descriptor = h.demo.runtime.describe_operator_draft_retention(handle)
    assert descriptor["workspace_id"] == h.demo.workspace_id
    assert descriptor["run_id"] == h.demo.run_id
    assert descriptor["operator_scope"] != "local-operator"
    request = {"target": "operator-target", "operation_key": "a" * 32, "state": "unsent"}
    first = h.demo.runtime.derive_operator_draft_key(handle, request)
    same = h.demo.runtime.derive_operator_draft_key(handle, request)
    changed = h.demo.runtime.derive_operator_draft_key(handle,
        request | {"operation_key": "b" * 32})
    assert first == same and first["key_b64"] != changed["key_b64"]
    assert first["operator_scope"] == descriptor["operator_scope"]
    assert first["max_text_bytes"] == 2048 and first["max_record_bytes"] == 4096
    h.demo.runtime.revoke_operator_draft_retention(handle)
    with pytest.raises(runtime.WorkspaceRuntimeError, match="operator_retention_required"):
        h.demo.runtime.describe_operator_draft_retention(handle)


def test_draft_retention_secret_epoch_and_operator_scope_are_bound(h):
    first_handle, first_scope = _draft_handle(h, secret=b"d" * 32, epoch="epoch-1")
    second_handle, _ = _draft_handle(h, secret=b"e" * 32, epoch="epoch-2")
    first = h.demo.runtime.describe_operator_draft_retention(first_handle)
    second = h.demo.runtime.describe_operator_draft_retention(second_handle)
    assert first["operator_scope"] != second["operator_scope"]
    request = {"target": "operator-target", "operation_key": "c" * 32, "state": "sending"}
    assert h.demo.runtime.derive_operator_draft_key(first_handle, request) != h.demo.runtime.derive_operator_draft_key(second_handle, request)
    assert first_scope["operator_id"] == "local-operator"
