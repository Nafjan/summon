"""Provider-inert derived usage metrics: work tokens and context occupancy.

Two different questions need two different token sums:

* **Work tokens** answer "how much did this attempt cost in compute?" for
  budgets and runaway detection. Cache *reads* are excluded: a long session
  re-reads a large cached prefix on every request, so counting reads turns a
  budget into a timer on session length rather than a measure of work.
* **Context occupancy** answers "how full is the model's context window?" for
  handoff and routing decisions. Cache reads *are* included here, because a
  cached prefix still occupies the window.

Provider usage dialects disagree about whether ``input_tokens`` already contains
cached input (OpenAI/Codex: yes; Anthropic/Claude: no). Every derived value
therefore carries its ``input_semantics`` and an ``evidence`` label, and this
module never invents a number it cannot derive from reported fields.
"""

from __future__ import annotations

import math

USAGE_SCHEMA = "summon.usage-metrics/v1"
CONTEXT_SCHEMA = "summon.context-occupancy/v1"

EXCLUDES_CACHE = "excludes_cache"
INCLUDES_CACHE = "includes_cache"
UNKNOWN = "unknown"

_INPUT_KEYS = ("input_tokens", "prompt_tokens", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens")
_CACHE_READ_KEYS = (
    "cache_read_input_tokens", "cache_read_tokens", "cacheReadTokens",
    "cacheReadInputTokens", "cached_input_tokens",
    "prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens",
)
_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens", "cache_write_tokens", "cacheWriteTokens",
    "cacheCreationInputTokens", "cache_write_input_tokens",
)
# Field spellings that identify a dialect regardless of which CLI produced it.
_ANTHROPIC_MARKERS = ("cache_read_input_tokens", "cache_creation_input_tokens",
                      "cacheReadInputTokens", "cacheCreationInputTokens")
_OPENAI_MARKERS = ("cached_input_tokens", "prompt_tokens_details", "input_tokens_details",
                   "prompt_tokens")
_CLI_SEMANTICS = {
    "claude": EXCLUDES_CACHE,
    "codex": INCLUDES_CACHE,
    "openai-compat": INCLUDES_CACHE,
}
_MAX_TOKENS = (1 << 53) - 1


def _number(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > _MAX_TOKENS:
        return None
    return int(value)


def _lookup(usage: dict, key: str):
    node = usage
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _first(usage: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _number(_lookup(usage, key))
        if value is not None:
            return value
    return None


def input_semantics(usage: object, *, cli: str | None = None) -> str:
    """Classify whether reported input tokens already include cached input."""
    if isinstance(usage, dict):
        if any(key in usage for key in _ANTHROPIC_MARKERS):
            return EXCLUDES_CACHE
        if any(key in usage for key in _OPENAI_MARKERS):
            return INCLUDES_CACHE
    return _CLI_SEMANTICS.get(cli or "", UNKNOWN)


def _fields(usage: dict) -> tuple[int | None, int | None, int | None, int | None]:
    return (_first(usage, _INPUT_KEYS), _first(usage, _OUTPUT_KEYS),
            _first(usage, _CACHE_READ_KEYS), _first(usage, _CACHE_WRITE_KEYS))


def work_tokens(usage: object, *, cli: str | None = None,
                observation_scope: str | None = None) -> dict:
    """Derive budget-grade work tokens (input + output + cache writes, no cache reads)."""
    out = {
        "schema": USAGE_SCHEMA,
        "work_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "input_semantics": UNKNOWN,
        "observation_scope": (observation_scope
                              if observation_scope in {"attempt_total", "last_step_snapshot"}
                              else "unestablished"),
        "evidence": "absent",
    }
    if not isinstance(usage, dict):
        return out
    inp, outp, cread, cwrite = _fields(usage)
    semantics = input_semantics(usage, cli=cli)
    out.update({"input_tokens": inp, "output_tokens": outp,
                "cache_read_tokens": cread, "cache_write_tokens": cwrite,
                "input_semantics": semantics})
    if inp is None or outp is None:
        out["evidence"] = "incomplete"
        return out
    writes = cwrite or 0
    if semantics == EXCLUDES_CACHE:
        out["work_tokens"] = inp + outp + writes
        out["evidence"] = "derived"
    elif semantics == INCLUDES_CACHE:
        if cread is not None and cread > inp:
            # Cached input larger than total input is internally inconsistent.
            out["evidence"] = "inconsistent_cache_fields"
            return out
        out["work_tokens"] = inp - (cread or 0) + outp + writes
        out["evidence"] = "derived"
    elif cread is None and cwrite is None:
        out["work_tokens"] = inp + outp
        out["evidence"] = "derived_no_cache_fields"
    else:
        out["evidence"] = "ambiguous_cache_semantics"
    return out


def context_occupancy(last_request_usage: object, context_window: object, *,
                      cli: str | None = None) -> dict:
    """Input-side context occupancy of the most recent single model request.

    ``last_request_usage`` must be a per-request usage record (for example one
    Claude assistant message), never a cumulative attempt total: summing many
    requests says nothing about how full one window is.
    """
    window = _number(context_window)
    if window == 0:
        window = None
    out = {
        "schema": CONTEXT_SCHEMA,
        "occupied_tokens": None,
        "window_tokens": window,
        "ratio": None,
        "over_window": None,
        "input_semantics": UNKNOWN,
        "evidence": "absent",
    }
    if not isinstance(last_request_usage, dict):
        return out
    inp, _outp, cread, cwrite = _fields(last_request_usage)
    semantics = input_semantics(last_request_usage, cli=cli)
    out["input_semantics"] = semantics
    if inp is None:
        out["evidence"] = "incomplete"
        return out
    if semantics == EXCLUDES_CACHE:
        occupied = inp + (cread or 0) + (cwrite or 0)
    elif semantics == INCLUDES_CACHE:
        occupied = inp + (cwrite or 0)
    elif cread is None and cwrite is None:
        occupied = inp
    else:
        out["evidence"] = "ambiguous_cache_semantics"
        return out
    out["occupied_tokens"] = occupied
    if window is None:
        out["evidence"] = "occupied_without_window"
        return out
    out["ratio"] = round(occupied / window, 4)
    out["over_window"] = occupied > window
    out["evidence"] = "reported"
    return out
