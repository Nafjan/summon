import hashlib

import pytest

from _conversation_economics import (TurnEconomicsError, make, public_summary,
                                     validate_public_summary)
from _conversation_runtime import (ConversationRuntime, ConversationRuntimeError,
                                   _chat_submission_state)
import _context_policy as context_policy


def _record(event, message_id, text, cursor):
    return {"event": event, "cursor": cursor, "actor_id": "sol",
            "payload": {"message_id": message_id, "text": text}}


def test_chat_context_uses_complete_messages_and_reports_omissions():
    policy = context_policy.make("off", policy_id="chat-context")
    records = [_record("human_message", "old", "old message", 1),
               _record("message_posted", "new", "new message", 2)]
    bundle = ConversationRuntime._context_bundle(
        records, "next", resumed=False, policy=policy)
    assert "old message" in bundle["prompt"]
    assert "new message" in bundle["prompt"]
    assert bundle["selected"] == ["old", "new"]
    assert bundle["omitted"] == []
    assert bundle["policy"]["mode"] == "off"


def test_chat_context_drops_only_whole_old_messages():
    policy = context_policy.make("off", policy_id="chat-context")
    records = [_record("human_message", "old", "x" * 20, 1),
               _record("message_posted", "new", "y" * 20, 2)]
    original = ConversationRuntime._context_bundle
    try:
        import _conversation_runtime as runtime
        runtime.MAX_CONTEXT_CHARS = 230
        bundle = original(records, "next", resumed=False, policy=policy)
    finally:
        runtime.MAX_CONTEXT_CHARS = 24_000
    assert "x" * 10 not in bundle["prompt"]
    assert bundle["selected"] == ["new"]
    assert bundle["omitted"] == ["old"]


def test_chat_context_refuses_newest_message_that_cannot_fit():
    policy = context_policy.make("off", policy_id="chat-context")
    records = [_record("human_message", "huge", "z" * 200, 1)]
    import _conversation_runtime as runtime
    old = runtime.MAX_CONTEXT_CHARS
    runtime.MAX_CONTEXT_CHARS = 64
    try:
        with pytest.raises(ConversationRuntimeError, match="exceeds bound"):
            ConversationRuntime._context_bundle(records, "next", resumed=False,
                                                policy=policy)
    finally:
        runtime.MAX_CONTEXT_CHARS = old


def test_resumed_chat_cannot_advertise_unsupported_safe_context_policy():
    policy = context_policy.make("safe", policy_id="chat-context")
    with pytest.raises(ConversationRuntimeError, match="supported typed source"):
        ConversationRuntime._context_bundle(
            [], "resumed", resumed=True, policy=policy)


def _economics(**overrides):
    base = dict(session_id="room", participant="sol", turn_id="turn-1",
                owner_generation=2, attempt_id="a" * 32,
                policy=context_policy.make("off", policy_id="chat-context"),
                source_selection_sha256="b" * 64, payload_sha256="c" * 64,
                source_count=2, omitted_count=1, status="success",
                submission_state="possible",
                envelope={"prompt_text": "hello", "usage": {"total_tokens": 0}},
                launch_observation=None)
    base.update(overrides)
    return make(**base)


def test_turn_economics_is_owner_bound_and_publicly_redacted():
    record = _economics()
    summary = public_summary(record)
    validate_public_summary(summary)
    assert summary["submission_state"] == "possible"
    assert summary["provider_contact"] == "unknown"
    assert summary["unknown_spend"] is True
    assert "attempt_id" not in str(summary)
    assert "payload_sha256" not in summary


def test_turn_economics_requires_actual_prompt_boundary():
    with pytest.raises(TurnEconomicsError, match="prompt"):
        _economics(envelope={})


def test_public_summary_does_not_accept_private_policy_or_forged_contact():
    summary = public_summary(_economics())
    forged = dict(summary)
    forged["provider_contact"] = "occurred"
    with pytest.raises(TurnEconomicsError):
        validate_public_summary(forged)
    forged = dict(summary)
    forged["context_policy"] = context_policy.make("off", policy_id="chat-context")
    with pytest.raises(TurnEconomicsError):
        validate_public_summary(forged)


def test_chat_submission_state_never_upgrades_contact_flag_to_confirmed():
    assert _chat_submission_state(refusal=object(), dispatch_attempted=False) == "not_submitted"
    assert _chat_submission_state(refusal=None, dispatch_attempted=True) == "possible"
    assert _chat_submission_state(refusal=None, dispatch_attempted=False) == "indeterminate"


def test_handoff_summary_requires_authenticated_guard_boundary():
    record = _economics(
        launch_observation={"schema": "summon.resume-launch-observation/v1"},
        handoff_verified=True,
    )
    assert record["handoff"]["verified"] is False
    assert public_summary(record)["handoff_verified"] is False
