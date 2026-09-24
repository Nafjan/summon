"""Independent non-launching chat migration refusal acceptance."""
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import test_conversation_runtime as fixtures
from _conversation_runtime import ConversationRuntime, ConversationRuntimeError


def test_missing_historical_turn_refuses_without_creating_authority(monkeypatch):
    fixture = fixtures.ConversationRuntimeTests()
    fixture.setUp()
    try:
        runtime = ConversationRuntime(
            fixture.root, cwd=fixture.project, agents_dir=fixture.roster,
            dispatcher=fixture.dispatcher, timeout_ms=10_000, strict_agents_dir=True,
        )

        def snapshot():
            return {
                str(path.relative_to(fixture.root)): path.read_bytes() if path.is_file() else None
                for path in fixture.root.rglob("*")
            }

        before = snapshot()

        def forbidden(*args, **kwargs):
            pytest.fail("non-launching revalidation attempted to start a process")

        monkeypatch.setattr(subprocess, "Popen", forbidden)
        with pytest.raises(ConversationRuntimeError, match="completed historical turn"):
            runtime.revalidate_chat_source(
                "room-1", "worker", turn_id="missing-source-turn",
                observation={}, qualification={},
            )
        unchanged = snapshot() == before
        assert unchanged, "refused migration created authority or changed room history"
    finally:
        fixture.tearDown()
