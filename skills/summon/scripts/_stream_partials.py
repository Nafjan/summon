"""Additive streaming partials for long-running seats.

When ``SUMMON_STREAM_PARTIALS=1``, helpers may emit progress events as JSON
lines on **stderr**. The final stdout envelope shape is unchanged.

Partial event shape (all fields optional except ``type`` and ``ts``)::

    {
      "type": "summon.partial",
      "ts": <unix_ms>,
      "phase": "progress" | "started" | "tool" | "token" | "heartbeat",
      "cli": "<backend>",
      "message": "<human-readable>",
      "bytes": <int>,
      "elapsed_ms": <int>
    }

Hosts that do not understand these lines should ignore unknown stderr JSON.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


PARTIAL_TYPE = "summon.partial"


def stream_partials_enabled() -> bool:
    return os.environ.get("SUMMON_STREAM_PARTIALS", "").strip() in ("1", "true", "yes", "on")


def make_partial(phase: str = "progress", **fields: Any) -> dict[str, Any]:
    """Build a partial event dict (does not emit)."""
    ev: dict[str, Any] = {
        "type": PARTIAL_TYPE,
        "ts": int(time.time() * 1000),
        "phase": phase,
    }
    for k, v in fields.items():
        if v is not None and k not in ev:
            ev[k] = v
    return ev


def emit_partial(phase: str, **fields: Any) -> dict[str, Any] | None:
    """Emit one partial JSON line to stderr when enabled; return the event or None."""
    if not stream_partials_enabled():
        return None
    ev = make_partial(phase, **fields)
    try:
        sys.stderr.write(json.dumps(ev, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except OSError:
        return ev
    return ev
