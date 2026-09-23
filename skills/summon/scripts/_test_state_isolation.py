"""Keep test runs out of the operator's real Summon state.

Without this, every test run that exercised a dispatch path appended events to
``~/.agents/summon-telemetry.jsonl`` (field record 2026-09-19..23: roughly a third of
the spool was pytest bursts and fixture models, indistinguishable from real use).
The overrides are environment variables so dispatcher subprocesses inherit them; a
test that needs a specific location still sets its own value, and one that needs
the home default removes the variable (``monkeypatch.delenv``).

Imported by each test root's ``conftest.py`` and by script-run suites such as
``test_discovery.py``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

ISOLATED_STATE = {
    "SUMMON_TELEMETRY_FILE": "summon-telemetry.jsonl",
    "SUMMON_TELEMETRY_CONFIG": "summon-telemetry.json",
    "SUMMON_REPORTS_DIR": "summon-reports",
    "SUMMON_AGY_CAPABILITY_CACHE": "summon-agy-capability.json",
}


def isolate() -> None:
    """Point every unset home-state override at a private per-run directory."""
    if all(name in os.environ for name in ISOLATED_STATE):
        return
    root = tempfile.mkdtemp(prefix="summon-test-state-")
    atexit.register(shutil.rmtree, root, True)
    for name, leaf in ISOLATED_STATE.items():
        os.environ.setdefault(name, os.path.join(root, leaf))
