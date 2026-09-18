"""Owned-pipe synthetic transport under a trusted OS-user/launcher model.

No hostile same-user isolation, durable admission, provider, scheduler or native
session attachment is provided. HMAC authenticates issued connections, not model
identity or permission. Bootstrap secrets travel only on dedicated owned pipes.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import struct
import subprocess
import sys
import threading

# The fixed worker runs with -I, which intentionally omits script-directory
# imports. Add only this trusted source directory to load the shared spawn flags.
_SOURCE_DIRECTORY = str(Path(__file__).resolve().parent)
if _SOURCE_DIRECTORY not in sys.path:
    sys.path.insert(0, _SOURCE_DIRECTORY)
from _spawn import popen_flags
import _workspace_admission as admission
import _workspace_budget as workspace_budget

PROTOCOL = "summon.workspace.pipe/v1"
MAX_FRAME_BYTES = 8192
MAX_CONTEXT_BYTES = 4096
FIXTURE_OPERATION = "sum_integers_v1"
CONTEXT_FIXTURE_OPERATION = "sum_context_integers_v1"
SEND_RESULT_SCHEMA = "summon.workspace.message-send-result/v1"
TRANSPORT_ADMISSION_SCHEMA = "summon.workspace.transport-admission/v1"
SUPERVISOR_PROTOCOL = "summon.workspace.supervisor-pipe/v1"
SUPERVISOR_CONSUMER_KIND = "owned_fixed_supervisor_consumer/v1"
MAX_SEQUENCE = 2**63 - 1
_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")


class TransportError(ValueError):
    """Safe bounded diagnostic; never includes frame bodies or secret material."""


class TransportAdmissionError(TransportError):
    """A bound workspace budget refused the owned transport before contact."""

    def __init__(self, reason, *, result=None):
        self.reason = reason
        self.result = copy.deepcopy(result) if isinstance(result, dict) else None
        super().__init__(reason)


def _fail(reason):
    raise TransportError(reason)


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail("invalid_fields")


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("invalid_id")


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        _fail("invalid_digest")


def _integer(value):
    if type(value) is not int or not 1 <= value <= MAX_SEQUENCE:
        _fail("invalid_integer")


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.01 <= value <= 30:
        _fail("invalid_io_timeout")


def _json(value):
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("invalid_json")
    if len(data) > MAX_FRAME_BYTES:
        _fail("frame_oversize")
    return data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def _decode(data):
    if type(data) is not bytes or not 1 <= len(data) <= MAX_FRAME_BYTES:
        _fail("frame_oversize_or_empty")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                          parse_constant=lambda _: _fail("nonfinite_json"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_json")


def pack(value):
    data = _json(value)
    return struct.pack(">I", len(data)) + data


def read_frame(reader):
    """Read exactly one bounded frame. The owner supplies lifetime cancellation.

    EOF/partial reads never become a receipt. This function cannot interrupt an
    arbitrary blocking reader; OwnedFakeWorker adds a bounded owner deadline.
    """
    def exact(size):
        parts, left = [], size
        while left:
            try:
                chunk = reader.read(left)
            except (OSError, ValueError):
                _fail("read_interrupted")
            if type(chunk) is not bytes or not chunk or len(chunk) > left:
                _fail("read_incomplete")
            parts.append(chunk)
            left -= len(chunk)
        return b"".join(parts)
    size = struct.unpack(">I", exact(4))[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        _fail("frame_oversize_or_empty")
    return exact(size)


@dataclass(frozen=True)
class WriteObservation:
    confirmed_bytes: int
    frame_bytes: int
    outcome: str


def write_frame(writer, frame: bytes) -> WriteObservation:
    """Observe writes without inferring recipient receipt or provider contact.

    A write exception can occur after unreported effects, including on the first
    call. Only an explicit zero-byte return before any confirmed write means
    zero_write. Use unbuffered owned pipes; this is not a flush/durability proof.
    """
    if type(frame) is not bytes or not 5 <= len(frame) <= MAX_FRAME_BYTES + 4:
        _fail("invalid_framed_bytes")
    expected = struct.unpack(">I", frame[:4])[0]
    if expected != len(frame) - 4:
        _fail("frame_length_mismatch")
    written = 0
    while written < len(frame):
        try:
            count = writer.write(frame[written:])
        except (OSError, ValueError):
            return WriteObservation(written, len(frame), "partial_or_unknown")
        if type(count) is not int or not 0 <= count <= len(frame) - written:
            return WriteObservation(written, len(frame), "partial_or_unknown")
        if count == 0:
            return WriteObservation(written, len(frame), "zero_write" if written == 0 else "partial_or_unknown")
        written += count
    return WriteObservation(written, len(frame), "full_write")


def _binding(value):
    _object(value, {"workspace_id", "run_id", "instance_id", "epoch", "task_id", "grant_id", "grant_sha256"})
    for key in ("workspace_id", "run_id", "instance_id", "task_id", "grant_id"):
        _id(value[key])
    _integer(value["epoch"])
    _digest(value["grant_sha256"])


def _fixture_values(context):
    """One fixed bounded computation input; never commands or expressions."""
    values = _decode(context.encode("utf-8"))
    if (type(values) is not list or not 1 <= len(values) <= 64
            or any(type(value) is not int or abs(value) > 1_000_000 for value in values)):
        _fail("invalid_fixture_values")
    return values


def _context_fixture_values(payload, binding=None):
    """Fixed integer data only; canonical message order is bound by its digest."""
    raw = payload["context"].encode("utf-8")
    envelope = _decode(raw)
    fields = {"schema", "plane", "task_id", "claim_id", "attempt", "lease_generation", "request_sha256", "entries"}
    _object(envelope, fields | ({"turn_id"} if type(envelope) is dict and "turn_id" in envelope else set()))
    if (_json(envelope) != raw or envelope["schema"] != "summon.workspace.context/v1"
            or envelope["plane"] != "payload"):
        _fail("invalid_fixture_context_envelope")
    for name in ("task_id", "claim_id"):
        _id(envelope[name])
    if "turn_id" in envelope:
        _id(envelope["turn_id"])
    _integer(envelope["attempt"])
    _integer(envelope["lease_generation"])
    _digest(envelope["request_sha256"])
    if (envelope["attempt"] > 1024 or envelope["claim_id"] != payload["attempt_id"]
            or binding is not None and envelope["task_id"] != binding["task_id"]):
        _fail("fixture_context_task_or_attempt_mismatch")
    entries = envelope["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= 8:
        _fail("invalid_fixture_context_entries")
    values, message_ids, delivery_ids = [], set(), set()
    for entry in entries:
        _object(entry, {"message_id", "delivery_id", "content_sha256", "content_utf8"})
        _id(entry["message_id"])
        _id(entry["delivery_id"])
        _digest(entry["content_sha256"])
        if (entry["message_id"] in message_ids or entry["delivery_id"] in delivery_ids
                or type(entry["content_utf8"]) is not str):
            _fail("invalid_fixture_context_entry")
        message_ids.add(entry["message_id"])
        delivery_ids.add(entry["delivery_id"])
        body = entry["content_utf8"].encode("utf-8")
        if hashlib.sha256(body).hexdigest() != entry["content_sha256"]:
            _fail("fixture_context_content_mismatch")
        values.extend(_fixture_values(entry["content_utf8"]))
        if len(values) > 64:
            _fail("fixture_aggregate_limit")
    if (entries[0]["message_id"] != payload["message_id"]
            or entries[0]["delivery_id"] != payload["delivery_id"]):
        _fail("fixture_context_first_entry_mismatch")
    return values


def _work_scope(kind, payload, binding):
    if kind == "fixture_work" and payload["operation"] == CONTEXT_FIXTURE_OPERATION:
        _context_fixture_values(payload, binding)


def _payload(kind, value):
    if kind == "ready":
        _object(value, {"challenge"})
        _digest(value["challenge"])
    elif kind == "message_send_request":
        admission.send_request(value)
    elif kind == "message_send_result":
        _object(value, {"schema", "operation_key", "request_sha256", "request_frame_sha256", "request_sequence",
                        "outcome", "reason", "result"})
        if value["schema"] != SEND_RESULT_SCHEMA:
            _fail("invalid_send_result_schema")
        _id(value["operation_key"])
        _digest(value["request_sha256"])
        _digest(value["request_frame_sha256"])
        _integer(value["request_sequence"])
        if type(value["outcome"]) is not str or value["reason"] is not None and type(value["reason"]) is not str:
            _fail("invalid_send_outcome")
        if value["outcome"] == "queued":
            result = value["result"]
            _object(result, {"status", "operation_key", "request_sha256", "message_id", "delivery_id", "stream_sequence",
                             "revision", "delivery_state", "execution_authorized"})
            if (value["reason"] is not None or result["status"] != "queued" or result["delivery_state"] != "queued"
                    or result["execution_authorized"] is not False
                    or any(result[key] != value[key] for key in ("operation_key", "request_sha256"))):
                _fail("invalid_send_commit_result")
            for key in ("message_id", "delivery_id"):
                _id(result[key])
            for key in ("stream_sequence", "revision"):
                _integer(result[key])
        elif value["outcome"] in {"refused", "indeterminate"}:
            if value["result"] is not None or value["reason"] not in {"not_authorized", "invalid_request", "admission_unavailable", "commit_uncertain"}:
                _fail("invalid_send_refusal")
        else:
            _fail("invalid_send_outcome")
    elif kind in {"context", "fixture_work"}:
        _object(value, {"message_id", "delivery_id", "attempt_id", "context", "context_sha256"}
                | ({"operation"} if kind == "fixture_work" else set()))
        for key in ("message_id", "delivery_id", "attempt_id"):
            _id(value[key])
        _digest(value["context_sha256"])
        if type(value["context"]) is not str:
            _fail("invalid_context")
        try:
            content = value["context"].encode("utf-8")
        except UnicodeError:
            _fail("invalid_context")
        if not 1 <= len(content) <= MAX_CONTEXT_BYTES or hashlib.sha256(content).hexdigest() != value["context_sha256"]:
            _fail("context_binding_or_size")
        if kind == "fixture_work":
            if value["operation"] == CONTEXT_FIXTURE_OPERATION:
                _context_fixture_values(value)
            elif value["operation"] == FIXTURE_OPERATION:
                _fixture_values(value["context"])
            else:
                _fail("unsupported_fixture_operation")
    elif kind in {"frame_received", "acknowledged", "fixture_result"}:
        _object(value, {"message_id", "delivery_id", "attempt_id", "context_sha256", "request_frame_sha256", "request_sequence"}
                | ({"operation", "result"} if kind == "fixture_result" else set()))
        for key in ("message_id", "delivery_id", "attempt_id"):
            _id(value[key])
        for key in ("context_sha256", "request_frame_sha256"):
            _digest(value[key])
        _integer(value["request_sequence"])
        if kind == "fixture_result":
            if type(value["operation"]) is not str or value["operation"] not in {FIXTURE_OPERATION, CONTEXT_FIXTURE_OPERATION}:
                _fail("unsupported_fixture_operation")
            result = value["result"]
            _object(result, {"count", "sum", "sum_squares"})
            if (type(result["count"]) is not int or not 1 <= result["count"] <= 64
                    or type(result["sum"]) is not int or abs(result["sum"]) > 64_000_000
                    or type(result["sum_squares"]) is not int or not 0 <= result["sum_squares"] <= 64_000_000_000_000):
                _fail("invalid_fixture_result")
    else:
        _fail("unsupported_kind")


class Channel:
    """Single-reader/single-writer issued connection, not an auth_context dict.

    Construction is a trusted in-process boundary, not a sandbox against callers
    importing Python. No key is exposed in repr, argv or diagnostic text.
    """
    _protocol = PROTOCOL
    _peers = {"supervisor": "worker", "worker": "supervisor"}
    _kinds = {"supervisor": frozenset({"context", "fixture_work", "message_send_result"}),
              "worker": frozenset({"ready", "frame_received", "acknowledged", "fixture_result", "message_send_request"})}
    _check_binding = staticmethod(_binding)
    _check_payload = staticmethod(_payload)
    _check_scope = staticmethod(_work_scope)

    def __init__(self, binding, key: bytes, nonce: str, role: str):
        self._check_binding(binding)
        if type(key) is not bytes or len(key) != 32 or type(role) is not str or role not in self._peers:
            _fail("invalid_channel_setup")
        _digest(nonce)
        self.binding = json.loads(_json(binding))
        self._key = key
        self.nonce = nonce
        self.role = role
        self._send_sequence = 0
        self._receive_sequence = 0
        self.revoked = False

    def revoke(self):
        self.revoked = True
        self._key = b""

    def encode(self, kind, payload):
        if self.revoked:
            _fail("channel_revoked")
        if kind not in self._kinds[self.role]:
            _fail("wrong_direction_kind")
        self._check_payload(kind, payload)
        self._check_scope(kind, payload, self.binding)
        sequence = self._send_sequence + 1
        _integer(sequence)
        unsigned = {"protocol": self._protocol, "nonce": self.nonce, "binding": self.binding,
                    "direction": self.role, "sequence": sequence, "kind": kind,
                    "payload": payload, "payload_sha256": hashlib.sha256(_json(payload)).hexdigest()}
        frame = pack(dict(unsigned, mac=hmac.new(self._key, _json(unsigned), hashlib.sha256).hexdigest()))
        self._send_sequence = sequence  # A failed send is never silently retried.
        return frame

    def receive(self, encoded: bytes):
        if self.revoked:
            _fail("channel_revoked")
        try:
            frame = _decode(encoded)
            _object(frame, {"protocol", "nonce", "binding", "direction", "sequence", "kind", "payload", "payload_sha256", "mac"})
            if _json(frame) != encoded:
                _fail("noncanonical_frame")
            self._check_binding(frame["binding"])
            if frame["protocol"] != self._protocol or frame["nonce"] != self.nonce or frame["binding"] != self.binding:
                _fail("wrong_connection_binding")
            expected_role = self._peers[self.role]
            if frame["direction"] != expected_role:
                _fail("wrong_direction")
            _integer(frame["sequence"])
            if frame["sequence"] != self._receive_sequence + 1:
                _fail("replay_or_sequence_gap")
            _digest(frame["mac"])
            unsigned = {k: v for k, v in frame.items() if k != "mac"}
            if not hmac.compare_digest(frame["mac"], hmac.new(self._key, _json(unsigned), hashlib.sha256).hexdigest()):
                _fail("authentication_failed")
            allowed = self._kinds[expected_role]
            if frame["kind"] not in allowed:
                _fail("wrong_direction_kind")
            self._check_payload(frame["kind"], frame["payload"])
            self._check_scope(frame["kind"], frame["payload"], self.binding)
            if frame["payload_sha256"] != hashlib.sha256(_json(frame["payload"])).hexdigest():
                _fail("payload_digest_mismatch")
            self._receive_sequence = frame["sequence"]
            return frame
        except (ValueError, TypeError):
            self.revoke()
            _fail("frame_refused_channel_revoked")


class BoundTransportAdmission:
    """Coordinator-issued, one-shot gate for an owned worker bootstrap.

    This adapter deliberately sits outside the legacy ``OwnedFakeWorker``
    constructor contract.  A coordinator supplies a bound decision and a
    current-state consumer; the gate rechecks that decision immediately before
    the worker's channel or child process exists.  The planned payload and
    binding are hashed at construction and must be identical at the gate.  No
    binding is fixed at issuance; it is not an independent live claim, grant or
    instance authentication check.  No boolean or worker name can mint
    admission.
    """

    def __init__(self, *, decision, policy, snapshot, request, binding,
                 planned_work, consume, turn_admission_id=None):
        if not callable(consume):
            raise TransportAdmissionError("coordinator_consumer_required")
        try:
            _binding(binding)
            # Validate the exact supported transport operation and compute the
            # complete deterministic frame before any child or pipe exists.
            _payload("fixture_work", planned_work)
            selected_ids = [planned_work["message_id"]]
            if planned_work["operation"] == CONTEXT_FIXTURE_OPERATION:
                context = _decode(planned_work["context"].encode("utf-8"))
                selected_ids = [entry["message_id"] for entry in context["entries"]]
                if not 1 <= len(selected_ids) <= 8:
                    raise TransportAdmissionError("transport_request_binding_invalid")
            probe = Channel(binding, b"\0" * 32, "0" * 64, "supervisor")
            frame = probe.encode("fixture_work", planned_work)
            checked_request = workspace_budget.validate_request(request)
            if (checked_request["kind"] != "message"
                    or checked_request["selected_message_ids"] != selected_ids
                    or checked_request["execution_slots_requested"] != 1):
                raise TransportAdmissionError("transport_request_binding_invalid")
            checked_policy = workspace_budget.validate_policy(policy)
            checked_snapshot = workspace_budget.validate_snapshot(snapshot)
            decision_copy = copy.deepcopy(decision)
            # Validate shape now, but consume only at the live pre-bootstrap
            # boundary so stale revision/prefix facts cannot pass.
            try:
                workspace_budget._object(decision_copy, {
                    "schema", "result", "result_sha256", "request_identity_sha256",
                    "facts_identity_sha256", "journal_revision", "journal_prefix_sha256"},
                    {"phase"})
                workspace_budget._validate_binding_digest(decision_copy["journal_prefix_sha256"],
                                                           "budget decision journal prefix digest")
            except workspace_budget.WorkspaceBudgetError:
                raise TransportAdmissionError("budget_decision_invalid") from None
            if decision_copy["schema"] != workspace_budget.DECISION_SCHEMA:
                raise TransportAdmissionError("budget_decision_invalid")
        except workspace_budget.WorkspaceBudgetError as exc:
            raise TransportAdmissionError("budget_request_invalid") from None
        except TransportAdmissionError:
            raise
        except TransportError as exc:
            raise TransportAdmissionError("transport_request_invalid") from None
        self._decision = decision_copy
        self._policy = checked_policy
        self._snapshot = checked_snapshot
        self._request = checked_request
        self._consume = consume
        if turn_admission_id is not None:
            _id(turn_admission_id)
        self._turn_admission_id = turn_admission_id
        self._binding_sha256 = hashlib.sha256(_json(binding)).hexdigest()
        self._planned_work_sha256 = hashlib.sha256(_json(planned_work)).hexdigest()
        self._transport_frame_bytes = len(frame)
        self._used = False

    def before_spawn(self, binding, planned_work):
        """Consume the exact bound decision before channel/Popen creation."""
        if self._used:
            raise TransportAdmissionError("transport_admission_already_consumed")
        try:
            _binding(binding)
            _payload("fixture_work", planned_work)
            if (hashlib.sha256(_json(binding)).hexdigest() != self._binding_sha256
                    or hashlib.sha256(_json(planned_work)).hexdigest() != self._planned_work_sha256):
                raise TransportAdmissionError("transport_request_binding_mismatch")
            consume_kwargs = dict(
                decision=copy.deepcopy(self._decision),
                policy=copy.deepcopy(self._policy),
                snapshot=copy.deepcopy(self._snapshot),
                request=copy.deepcopy(self._request),
            )
            if self._turn_admission_id is not None:
                consume_kwargs.update(binding=copy.deepcopy(binding),
                                      planned_work=copy.deepcopy(planned_work))
            envelope = self._consume(**consume_kwargs)
        except TransportAdmissionError:
            raise
        except Exception as exc:
            # Do not leak callback paths, journal details or provider errors.
            raise TransportAdmissionError("budget_binding_required") from None
        if type(envelope) is not dict or set(envelope) != {
                "schema", "result", "journal_revision", "journal_prefix_sha256",
                "provider_contacted", "state_mutated"}:
            raise TransportAdmissionError("coordinator_consumer_invalid")
        if (envelope["schema"] != TRANSPORT_ADMISSION_SCHEMA
                or envelope["provider_contacted"] is not False
                or type(envelope["state_mutated"]) is not bool
                or (self._turn_admission_id is None and envelope["state_mutated"] is not False)
                or (self._turn_admission_id is not None and envelope["state_mutated"] is not True)):
            raise TransportAdmissionError("coordinator_consumer_invalid")
        try:
            current = self._decision["result"] if self._turn_admission_id is not None else \
                workspace_budget.evaluate(self._policy, self._snapshot, self._request)
            if (envelope["result"] != current
                    or workspace_budget.result_sha256(envelope["result"])
                    != workspace_budget.result_sha256(self._decision["result"])):
                raise TransportAdmissionError("budget_decision_result_mismatch")
            if (envelope["journal_revision"] != self._decision["journal_revision"]
                    or envelope["journal_prefix_sha256"] != self._decision["journal_prefix_sha256"]):
                raise TransportAdmissionError("coordinator_consumer_invalid")
            if (type(envelope["journal_revision"]) is not int
                    or envelope["journal_revision"] < 0):
                raise workspace_budget.WorkspaceBudgetError("invalid current journal revision")
            workspace_budget._validate_binding_digest(envelope["journal_prefix_sha256"],
                                                       "current journal prefix digest")
        except workspace_budget.WorkspaceBudgetError as exc:
            raise TransportAdmissionError("coordinator_consumer_invalid") from None
        result = envelope["result"]
        if result["status"] != "accepted":
            raise TransportAdmissionError("budget_decision_blocked", result=result)
        if result["execution_status"] != "available":
            raise TransportAdmissionError("execution_slot_unavailable", result=result)
        self._used = True
        return {
            "schema": TRANSPORT_ADMISSION_SCHEMA,
            "status": "accepted",
            "result": copy.deepcopy(result),
            "transport_frame_bytes": self._transport_frame_bytes,
            "provider_contacted": False,
            "state_mutated": False,
        }


def _supervisor_binding(value):
    _object(value, {"workspace_id", "run_id", "endpoint_id", "owner_instance_id", "epoch",
                    "receiver_grant_id", "receiver_grant_sha256"})
    for name in ("workspace_id", "run_id", "endpoint_id", "owner_instance_id", "receiver_grant_id"):
        _id(value[name])
    _integer(value["epoch"])
    _digest(value["receiver_grant_sha256"])


def _supervisor_payload(kind, value):
    if kind == "ready":
        _payload(kind, value)
        return
    common = {"offer_id", "delivery_id", "message_id", "content_sha256"}
    if kind == "inbox_offer":
        _object(value, common | {"content_utf8", "possible_duplicate", "prior_offer_id"})
        if type(value["content_utf8"]) is not str or type(value["possible_duplicate"]) is not bool:
            _fail("invalid_inbox_offer")
        try:
            raw = value["content_utf8"].encode("utf-8")
        except UnicodeError:
            _fail("invalid_inbox_content")
        if not 1 <= len(raw) <= MAX_CONTEXT_BYTES or hashlib.sha256(raw).hexdigest() != value["content_sha256"]:
            _fail("inbox_content_binding_or_size")
        if value["prior_offer_id"] is not None:
            _id(value["prior_offer_id"])
        if value["possible_duplicate"] != (value["prior_offer_id"] is not None):
            _fail("inbox_duplicate_history_mismatch")
    elif kind == "inbox_receipt":
        _object(value, common | {"content_utf8_bytes", "request_frame_sha256", "request_sequence",
                                 "receipt_kind", "consumer_kind", "qualification"})
        _integer(value["content_utf8_bytes"])
        _integer(value["request_sequence"])
        _digest(value["request_frame_sha256"])
        if (value["content_utf8_bytes"] > MAX_CONTEXT_BYTES or value["receipt_kind"] != "supervisor_context_received"
                or value["consumer_kind"] != SUPERVISOR_CONSUMER_KIND or value["qualification"] != "simulated"):
            _fail("invalid_inbox_receipt")
    else:
        _fail("unsupported_supervisor_kind")
    for name in ("offer_id", "delivery_id", "message_id"):
        _id(value[name])
    _digest(value["content_sha256"])


class SupervisorChannel(Channel):
    """Separate taskless wire domain; never accepts worker/task frame kinds."""
    _protocol = SUPERVISOR_PROTOCOL
    _peers = {"host": "consumer", "consumer": "host"}
    _kinds = {"host": frozenset({"inbox_offer"}), "consumer": frozenset({"ready", "inbox_receipt"})}
    _check_binding = staticmethod(_supervisor_binding)
    _check_payload = staticmethod(_supervisor_payload)
    _check_scope = staticmethod(lambda *_: None)


class OwnedFakeWorker:
    """Own exactly one fixed synthetic child; no arbitrary executable selection."""
    def __init__(self, binding, timeout=3.0, *, fixed_send=None, fixed_send_count=1,
                 budget_admission=None, planned_work=None):
        _binding(binding)
        _timeout(timeout)
        if budget_admission is None and planned_work is not None:
            _fail("transport_admission_required")
        if budget_admission is not None and not isinstance(budget_admission, BoundTransportAdmission):
            _fail("transport_admission_invalid")
        if budget_admission is not None and planned_work is None:
            _fail("transport_work_plan_required")
        self._transport_admission_receipt = None
        if type(fixed_send_count) is not int or fixed_send_count not in {1, 2} or fixed_send is None and fixed_send_count != 1:
            _fail("fixed_send_count_bound")
        if fixed_send is not None:
            # Route-only fixture configuration. Content is computed by the child.
            if type(fixed_send) is not dict or "content" in fixed_send:
                _fail("fixed_route_only")
            fixed_send = admission.send_request({**fixed_send, "content": "[0]"})
            fixed_send.pop("content")
        # This is intentionally the only integrated pre-bootstrap seam.  The
        # legacy constructor remains unchanged when no gate is supplied.
        if budget_admission is not None:
            self._transport_admission_receipt = budget_admission.before_spawn(binding, planned_work)
        self.channel = Channel(binding, secrets.token_bytes(32), secrets.token_hex(32), "supervisor")
        self._challenge = secrets.token_hex(32)
        self.process = None
        self._closed = False
        self._ready = False
        self._pending = None
        self._completed_work = None
        self._reader = threading.Lock()
        self._send_handler = None
        self._active_send = None
        self._last_send_result = None
        self._fixed_send = fixed_send
        self._fixed_send_count = fixed_send_count
        try:
            # CPython passes only redirected standard handles with close_fds on
            # Windows, and closes unrelated descriptors on POSIX. No handle-list
            # widening or shell wrapper. stderr goes to an owned null handle.
            environment = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            self.process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--synthetic-worker"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            bufsize=0, close_fds=True, env=environment, **popen_flags())
            bootstrap = {"protocol": PROTOCOL, "binding": binding, "key": self.channel._key.hex(),
                         "nonce": self.channel.nonce, "challenge": self._challenge}
            if fixed_send is not None:
                bootstrap["fixed_send"] = fixed_send
                bootstrap["fixed_send_count"] = fixed_send_count
            observation = self._write_owned(pack(bootstrap), timeout)
            if observation.outcome != "full_write":
                _fail("bootstrap_write_uncertain")
            ready = self.receive(timeout)
            if ready["kind"] != "ready" or ready["payload"]["challenge"] != self._challenge:
                _fail("bootstrap_challenge_failed")
        except BaseException:
            self.close()
            raise

    def _write_owned(self, frame, timeout):
        """Bound a possibly stalled OS pipe write by owned-child termination."""
        _timeout(timeout)
        result = []
        def write():
            result.append(write_frame(self.process.stdin, frame))
        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.close()  # Closing the fixed child's read end releases OS writes.
            thread.join(1)
            confirmed = result[0].confirmed_bytes if result else 0
            return WriteObservation(confirmed, len(frame), "partial_or_unknown")
        return result[0]

    def install_send_handler(self, handler):
        """Trusted host only. A handler cannot replace an in-flight binding."""
        if not callable(handler) or self._closed or self._pending is not None or self._send_handler is not None:
            _fail("send_handler_installation_refused")
        self._send_handler = handler

    def observed_send(self, token):
        """Only the currently demultiplexed authentic request is observable."""
        if (token is not self._active_send or token is None or self._closed or self.channel.revoked
                or self._pending is None):
            _fail("observed_send_required")
        return {"binding": json.loads(_json(self.channel.binding)), **json.loads(_json(token)),
                "work_request": json.loads(_json(self._pending["payload"])),
                "context_identity": json.loads(_json(self._pending["context_identity"]))}

    def receive(self, timeout=3.0):
        _timeout(timeout)  # Invalid deadlines refuse before locks or any I/O.
        if not self._reader.acquire(blocking=False):
            _fail("single_reader_required")
        try:
            return self._receive_owned(timeout)
        finally:
            self._reader.release()

    def _receive_owned(self, timeout):
        if self._closed:
            _fail("channel_closed")
        _timeout(timeout)
        result = []
        def read():
            try:
                result.append(self.channel.receive(read_frame(self.process.stdout)))
            except (TransportError, OSError, ValueError):
                result.append(None)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive() or not result or result[0] is None:
            self.close()
            thread.join(1)
            _fail("response_incomplete_or_timeout")
        frame = result[0]
        if frame["kind"] == "message_send_request":
            if (self._send_handler is None or self._active_send is not None or self._pending is None
                    or self._pending["kinds"] != ["fixture_result"]
                    or self._pending.get("worker_send_count", 0) >= self._fixed_send_count):
                self.close()
                _fail("unexpected_worker_send")
            self._pending["worker_send_count"] = self._pending.get("worker_send_count", 0) + 1
            token = {"request": frame["payload"], "request_frame_sha256": hashlib.sha256(_json(frame)).hexdigest(),
                     "request_sequence": frame["sequence"]}
            self._active_send = token
            try:
                outcome = self._send_handler(self, token)
                _object(outcome, {"outcome", "reason", "result"})
                _, request_digest = admission.send_request_identity(self.channel.binding, frame["payload"])
                response = {"schema": SEND_RESULT_SCHEMA, "operation_key": frame["payload"]["operation_key"],
                            "request_sha256": request_digest, "request_frame_sha256": token["request_frame_sha256"],
                            "request_sequence": frame["sequence"], **outcome}
                encoded = self.channel.encode("message_send_result", response)
                written = self._write_owned(encoded, timeout)
                self._last_send_result = {"response": json.loads(_json(response)), "write_outcome": written.outcome}
                if written.outcome != "full_write":
                    self.close()
                    _fail("worker_send_response_uncertain")
            except BaseException:
                self.close()
                raise
            finally:
                self._active_send = None
            # Same reader owns the next frame. No queue or reciprocal read.
            return self._receive_owned(timeout)
        if not self._ready and frame["kind"] == "ready":
            self._ready = True
            return frame
        actual = frame["payload"]
        if frame["kind"] == "fixture_result":
            actual = {k: v for k, v in actual.items() if k not in {"operation", "result"}}
        if (self._pending is None or frame["kind"] != self._pending["kinds"][0]
                or actual != self._pending["payload"]
                or frame["kind"] == "fixture_result" and frame["payload"]["operation"] != self._pending["operation"]):
            self.close()
            _fail("receipt_binding_or_order_mismatch")
        self._pending["kinds"].pop(0)
        if frame["kind"] == "fixture_result":
            self._completed_work = {"binding": json.loads(_json(self.channel.binding)),
                                    **self._pending["payload"], "operation": self._pending["operation"],
                                    "selected_entries": self._pending["selected_entries"],
                                    "context_identity": self._pending["context_identity"]}
        if not self._pending["kinds"]:
            self._pending = None
        return frame

    def send_context(self, payload, timeout=3.0):
        return self._send_request("context", payload, timeout)

    def send_work(self, payload, timeout=3.0):
        """Send fixed fixture work, only after the host's durable authorization.

        The separate result is authenticated computation output, still advisory
        until independently verified. Neither it nor an ack completes a task.
        This transport itself supplies no execution grant or durable admission.
        """
        return self._send_request("fixture_work", payload, timeout)

    def _send_request(self, kind, payload, timeout):
        if self._closed:
            _fail("channel_closed")
        _timeout(timeout)
        if self._pending is not None:
            _fail("prior_receipts_pending")
        frame = self.channel.encode(kind, payload)
        self._completed_work = None  # A later write invalidates the prior cleanup qualification.
        digest = hashlib.sha256(frame[4:]).hexdigest()
        observation = self._write_owned(frame, timeout)
        if observation.outcome != "full_write":
            self.close()
        else:
            receipt = {k: payload[k] for k in ("message_id", "delivery_id", "attempt_id", "context_sha256")}
            receipt.update(request_frame_sha256=digest, request_sequence=self.channel._send_sequence)
            kinds = ["frame_received", "acknowledged"]
            if kind == "fixture_work":
                kinds.append("fixture_result")
            entries = [{"message_id": payload["message_id"], "delivery_id": payload["delivery_id"]}]
            context_identity = None  # Plain-array work has no compiled selection metadata.
            if kind == "fixture_work" and payload["operation"] == CONTEXT_FIXTURE_OPERATION:
                envelope = _decode(payload["context"].encode("utf-8"))
                entries = [{key: entry[key] for key in ("message_id", "delivery_id")}
                           for entry in envelope["entries"]]
                context_identity = {key: envelope[key] for key in
                                    ("task_id", "claim_id", "attempt", "lease_generation", "request_sha256")}
            self._pending = {"payload": receipt, "kinds": kinds, "operation": payload.get("operation"),
                             "selected_entries": entries, "context_identity": context_identity}
        return observation, digest

    def observed_effects(self):
        """Observe only this fixed child's latest authenticated completed work.

        This is neither a provider billing query nor mathematical verification.
        A live, replaced, interrupted or subsequently written channel refuses;
        restart cannot recreate this observation from a caller dictionary.
        """
        if (self._completed_work is None or self._pending is not None or not self._closed
                or self.process is None or self.process.poll() is None or not self.channel.revoked
                or not self.process.stdin.closed or not self.process.stdout.closed):
            _fail("fixed_effects_not_observed")
        return {**json.loads(_json(self._completed_work)), "launcher": "summon_owned_fake_worker/v1",
                "provider_invoked": False, "child_exit_observed": True, "pipes_closed": True,
                "channel_revoked": True, "exit_code": self.process.returncode}

    def close(self):
        self.channel.revoke()
        self._closed = True
        self._pending = None
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _fail("synthetic_child_cleanup_uncertain")
        finally:
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OwnedSupervisorConsumer:
    """Fixed taskless context sink, not a model, human, task or approval actor.

    Transport authenticates exact receipt only. The host must durably offer and
    independently fence the current endpoint before any send or acknowledgement.
    No receipt token survives a new offer, channel revocation or process restart.
    """
    _write_owned = OwnedFakeWorker._write_owned
    close = OwnedFakeWorker.close
    __enter__ = OwnedFakeWorker.__enter__
    __exit__ = OwnedFakeWorker.__exit__

    def __init__(self, binding, timeout=3.0):
        _supervisor_binding(binding)
        _timeout(timeout)
        self.channel = SupervisorChannel(binding, secrets.token_bytes(32), secrets.token_hex(32), "host")
        self.process = None
        self._closed = False
        self._ready = False
        self._pending = None
        self._reader = threading.Lock()
        self._writer = threading.Lock()
        self._receipt_token = self._receipt_data = None
        challenge = secrets.token_hex(32)
        try:
            environment = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            self.process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--synthetic-supervisor-consumer"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, close_fds=True, env=environment, **popen_flags())
            bootstrap = {"protocol": SUPERVISOR_PROTOCOL, "binding": binding, "key": self.channel._key.hex(),
                         "nonce": self.channel.nonce, "challenge": challenge}
            if self._write_owned(pack(bootstrap), timeout).outcome != "full_write":
                _fail("consumer_bootstrap_uncertain")
            ready = self._receive_frame(timeout)
            if ready["kind"] != "ready" or ready["payload"]["challenge"] != challenge:
                _fail("consumer_bootstrap_refused")
            self._ready = True
        except BaseException:
            self.close()
            raise

    def observe_consumer(self):
        if (self._closed or self.channel.revoked or not self._ready
                or self.process is None or self.process.poll() is not None):
            _fail("current_owned_consumer_required")
        return {"binding": json.loads(_json(self.channel.binding)),
                "consumer_kind": SUPERVISOR_CONSUMER_KIND, "qualification": "simulated"}

    def check_offer(self, payload):
        """Check complete wire fit without exposure, sequence advance or authority."""
        self.observe_consumer()
        if self._pending is not None:
            _fail("consumer_receipt_pending")
        _supervisor_payload("inbox_offer", payload)
        unsigned = {"protocol": SUPERVISOR_PROTOCOL, "nonce": self.channel.nonce, "binding": self.channel.binding,
            "direction": "host", "sequence": self.channel._send_sequence + 1, "kind": "inbox_offer",
            "payload": payload, "payload_sha256": hashlib.sha256(_json(payload)).hexdigest()}
        frame = pack(dict(unsigned, mac="0" * 64))
        return {"content_sha256": payload["content_sha256"],
                "content_utf8_bytes": len(payload["content_utf8"].encode("utf-8")), "frame_bytes": len(frame)}

    def send_offer(self, payload, timeout=3.0):
        """Expose only after the caller has committed its exact offered record."""
        _timeout(timeout)
        if not self._writer.acquire(blocking=False):
            _fail("single_consumer_writer_required")
        try:
            checked = self.check_offer(payload)
            frame = self.channel.encode("inbox_offer", payload)
            self._receipt_token = self._receipt_data = None
            observation = self._write_owned(frame, timeout)
            if observation.outcome != "full_write":
                self.close()
            else:
                self._pending = {key: payload[key] for key in ("offer_id", "delivery_id", "message_id", "content_sha256")}
                self._pending.update(content_utf8_bytes=checked["content_utf8_bytes"],
                    request_frame_sha256=hashlib.sha256(frame[4:]).hexdigest(), request_sequence=self.channel._send_sequence,
                    receipt_kind="supervisor_context_received", consumer_kind=SUPERVISOR_CONSUMER_KIND, qualification="simulated")
            return observation
        finally:
            self._writer.release()

    def _receive_frame(self, timeout):
        _timeout(timeout)
        if not self._reader.acquire(blocking=False):
            _fail("single_reader_required")
        try:
            return self._read_frame_locked(timeout)
        finally:
            self._reader.release()

    def _read_frame_locked(self, timeout):
        if self._closed:
            _fail("channel_closed")
        results = []
        def read():
            try:
                results.append(self.channel.receive(read_frame(self.process.stdout)))
            except (ValueError, OSError):
                results.append(None)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive() or not results or results[0] is None:
            self.close()
            thread.join(1)
            _fail("consumer_receipt_incomplete_or_timeout")
        return results[0]

    def receive_receipt(self, timeout=3.0):
        _timeout(timeout)
        if not self._reader.acquire(blocking=False):
            _fail("single_reader_required")
        try:
            self.observe_consumer()
            if self._pending is None:
                _fail("consumer_offer_required")
            frame = self._read_frame_locked(timeout)
            if frame["kind"] != "inbox_receipt" or frame["payload"] != self._pending:
                self.close()
                _fail("consumer_receipt_binding_mismatch")
            self._receipt_token = object()
            self._receipt_data = {"binding": json.loads(_json(frame["binding"])),
                                  "receipt": json.loads(_json(frame["payload"]))}
            self._pending = None
            return self._receipt_token
        finally:
            self._reader.release()

    def observe_receipt(self, token):
        self.observe_consumer()
        if token is None or token is not self._receipt_token or self._pending is not None:
            _fail("issued_consumer_receipt_required")
        return json.loads(_json(self._receipt_data))


def _synthetic_supervisor_consumer():
    """Only digest-check exact bounded text and acknowledge receipt; no actions."""
    bootstrap = _decode(read_frame(sys.stdin.buffer))
    _object(bootstrap, {"protocol", "binding", "key", "nonce", "challenge"})
    if bootstrap["protocol"] != SUPERVISOR_PROTOCOL:
        _fail("unsupported_consumer_bootstrap")
    _digest(bootstrap["key"])
    _digest(bootstrap["challenge"])
    channel = SupervisorChannel(bootstrap["binding"], bytes.fromhex(bootstrap["key"]), bootstrap["nonce"], "consumer")
    output = sys.stdout.buffer.raw
    if write_frame(output, channel.encode("ready", {"challenge": bootstrap["challenge"]})).outcome != "full_write":
        _fail("consumer_ready_uncertain")
    while True:
        raw = read_frame(sys.stdin.buffer)
        frame = channel.receive(raw)
        offer = frame["payload"]
        receipt = {key: offer[key] for key in ("offer_id", "delivery_id", "message_id", "content_sha256")}
        receipt.update(content_utf8_bytes=len(offer["content_utf8"].encode("utf-8")),
            request_frame_sha256=hashlib.sha256(raw).hexdigest(), request_sequence=frame["sequence"],
            receipt_kind="supervisor_context_received", consumer_kind=SUPERVISOR_CONSUMER_KIND, qualification="simulated")
        if write_frame(output, channel.encode("inbox_receipt", receipt)).outcome != "full_write":
            _fail("consumer_receipt_write_uncertain")


def _synthetic_worker():
    """Fixed receipt/computation worker. Text is never evaluated as commands."""
    bootstrap = _decode(read_frame(sys.stdin.buffer))
    _object(bootstrap, {"protocol", "binding", "key", "nonce", "challenge"}
            | ({"fixed_send", "fixed_send_count"} if "fixed_send" in bootstrap else set()))
    if bootstrap["protocol"] != PROTOCOL:
        _fail("unsupported_bootstrap")
    _digest(bootstrap["key"])
    _digest(bootstrap["challenge"])
    channel = Channel(bootstrap["binding"], bytes.fromhex(bootstrap["key"]), bootstrap["nonce"], "worker")
    fixed_send = bootstrap.get("fixed_send")
    if fixed_send is not None:
        fixed_send = admission.send_request({**fixed_send, "content": "[0]"})
        fixed_send.pop("content")
        if type(bootstrap["fixed_send_count"]) is not int or bootstrap["fixed_send_count"] not in {1, 2}:
            _fail("fixed_send_count_bound")
    # -u is unnecessary: explicitly use unbuffered underlying stdout here.
    output = sys.stdout.buffer.raw
    if write_frame(output, channel.encode("ready", {"challenge": bootstrap["challenge"]})).outcome != "full_write":
        _fail("ready_write_uncertain")
    while True:
        raw = read_frame(sys.stdin.buffer)
        frame = channel.receive(raw)
        payload = frame["payload"]
        receipt = {k: payload[k] for k in ("message_id", "delivery_id", "attempt_id", "context_sha256")}
        receipt.update(request_frame_sha256=hashlib.sha256(raw).hexdigest(), request_sequence=frame["sequence"])
        for kind in ("frame_received", "acknowledged"):
            if write_frame(output, channel.encode(kind, receipt)).outcome != "full_write":
                _fail("receipt_write_uncertain")
        if frame["kind"] == "fixture_work":
            values = (_context_fixture_values(payload, channel.binding)
                      if payload["operation"] == CONTEXT_FIXTURE_OPERATION else _fixture_values(payload["context"]))
            computed = {"count": len(values), "sum": sum(values),
                        "sum_squares": sum(value * value for value in values)}
            if fixed_send is not None:
                outgoing = admission.send_request({**fixed_send, "content": _json([computed["sum"]]).decode("utf-8")})
                for _ in range(bootstrap["fixed_send_count"]):
                    encoded = channel.encode("message_send_request", outgoing)
                    sent_sequence = channel._send_sequence
                    if write_frame(output, encoded).outcome != "full_write":
                        _fail("worker_send_uncertain")
                    response = channel.receive(read_frame(sys.stdin.buffer))
                    _, request_digest = admission.send_request_identity(channel.binding, outgoing)
                    if (response["kind"] != "message_send_result"
                            or response["payload"]["operation_key"] != outgoing["operation_key"]
                            or response["payload"]["request_sha256"] != request_digest
                            or response["payload"]["request_frame_sha256"] != hashlib.sha256(encoded[4:]).hexdigest()
                            or response["payload"]["request_sequence"] != sent_sequence):
                        _fail("worker_send_response_binding")
                fixed_send = None  # One bounded authored request per fixed child.
            result = dict(receipt, operation=payload["operation"], result=computed)
            if write_frame(output, channel.encode("fixture_result", result)).outcome != "full_write":
                _fail("result_write_uncertain")


if __name__ == "__main__":
    # Isolated mode omits the script directory on some Python versions.
    if sys.argv[1:] not in (["--synthetic-worker"], ["--synthetic-supervisor-consumer"]):
        raise SystemExit(2)
    try:
        (_synthetic_worker if sys.argv[1:] == ["--synthetic-worker"] else _synthetic_supervisor_consumer)()
    except (TransportError, OSError, ValueError):
        raise SystemExit(2)
