"""Provider-inert root/child identity tests; fixtures contain synthetic text only."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _stream import StreamProcessor

LEAD = "claude-fable-5-1"
HELPER = "claude-sonnet-5"
TEXT = "Synthetic final decision."


def start():
    p = StreamProcessor(cli="claude")
    p.process_line(json.dumps({"type": "system", "subtype": "init",
                               "session_id": "synthetic-session", "model": LEAD}))
    return p


def assistant(p, *, model=LEAD, session="synthetic-session", parent=None,
              text=TEXT, reason="end_turn", omit_parent=False):
    event = {"type": "assistant", "session_id": session,
             "parent_tool_use_id": parent,
             "message": {"id": "synthetic-message", "model": model,
                         "stop_reason": reason,
                         "content": [{"type": "text", "text": text}]}}
    if omit_parent:
        event.pop("parent_tool_use_id")
    p.process_line(json.dumps(event))


def finish(p, *, text=TEXT, session="synthetic-session", model=None, usage=None):
    event = {"type": "result", "result": text, "session_id": session,
             "modelUsage": usage if usage is not None else {
                 LEAD: {"outputTokens": 10}, HELPER: {"outputTokens": 1000}}}
    if model is not None:
        event["model"] = model
    p.process_line(json.dumps(event))


def test_root_final_response_wins_over_larger_helper_usage():
    p = start()
    assistant(p)
    assistant(p, model=HELPER, parent="child-tool", text="Helper response")
    finish(p)
    assert p.model == LEAD
    assert p.model_evidence_source == "claude_root_response"
    assert p.models_used == [LEAD, HELPER]
    assert TEXT not in repr(p._claude_final_candidate)


def test_mixed_usage_without_response_binding_is_not_served_evidence():
    p = start()
    finish(p)
    assert p.model is None
    assert p.model_evidence_source == "claude_aggregate_usage"


def test_mismatched_response_or_session_is_unverified():
    for kwargs in ({"text": "Different response"}, {"session": "another-session"}):
        p = start()
        assistant(p)
        finish(p, **kwargs)
        assert p.model is None


def test_helper_unassociated_and_tool_progress_cannot_certify_root():
    for kwargs in ({"parent": "child-tool"}, {"omit_parent": True},
                   {"session": "another-session"}, {"reason": "tool_use"},
                   {"model": "https://invalid.example/model"}):
        p = start()
        assistant(p, **kwargs)
        finish(p)
        assert p.model is None


def test_later_root_progress_invalidates_old_final_candidate():
    p = start()
    assistant(p)
    assistant(p, text="Continuing work", reason="tool_use")
    finish(p)
    assert p.model is None


def test_terminal_explicit_model_remains_authoritative_and_keeps_usage():
    p = start()
    assistant(p)
    finish(p, model=HELPER)
    assert p.model == HELPER
    assert p.model_evidence_source == "terminal_model"
    assert p.models_used == [LEAD, HELPER]


def test_singleton_usage_retains_legacy_identity():
    p = start()
    finish(p, usage={LEAD: {"outputTokens": 10}})
    assert p.model == LEAD
    assert p.model_evidence_source == "terminal_single_model_usage"


def test_mismatched_root_text_prevents_singleton_backfill():
    p = start()
    assistant(p)
    finish(p, text="Different response", usage={HELPER: {"outputTokens": 10}})
    assert p.model is None


def test_foreign_root_session_prevents_singleton_and_explicit_model_backfill():
    for explicit in (None, LEAD):
        p = start()
        assistant(p, session="foreign-session")
        finish(p, session="foreign-session", model=explicit,
               usage={LEAD: {"outputTokens": 10}})
        assert p.model is None
        assert p.model_evidence_source == "claude_identity_conflict"


def test_foreign_terminal_session_prevents_singleton_backfill():
    p = start()
    finish(p, session="foreign-session", usage={LEAD: {"outputTokens": 10}})
    assert p.model is None
