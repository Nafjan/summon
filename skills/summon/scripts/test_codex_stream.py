"""Regression coverage for Codex terminal failure events."""

from __future__ import annotations

import json

from _stream import StreamProcessor


def test_codex_turn_failed_becomes_structured_terminal_error():
    processor = StreamProcessor()
    assert not processor.process_line(json.dumps({
        "type": "thread.started", "thread_id": "thread-test",
        "model": "gpt-5.6-luna",
    }))
    assert processor.handshake_model == "gpt-5.6-luna"
    assert processor.model is None

    assert processor.process_line(json.dumps({
        "type": "turn.failed",
        "error": {"message": "provider returned a bounded test failure"},
    }))
    assert processor.get_result() == {
        "type": "result",
        "result": "",
        "status": "error",
        "error": "provider returned a bounded test failure",
    }
    assert processor.model is None


def test_codex_turn_failed_without_detail_has_safe_fallback():
    processor = StreamProcessor()
    assert processor.process_line(json.dumps({"type": "turn.failed"}))
    assert processor.get_result()["error"] == "Codex reported that the turn failed"
