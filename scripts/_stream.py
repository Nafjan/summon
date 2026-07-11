"""StreamProcessor: parse newline-delimited JSON output from various CLIs."""

from __future__ import annotations

import json


def _terminal_is_error(data: dict) -> bool:
    """True when a terminal result object reports failure. Claude sets
    ``is_error: true`` and an ``error_*`` subtype on API/turn failures; some
    CLIs put ``status`` of ``error``/``failed`` on the result. Guards the
    no-false-success contract at the stream layer."""
    if data.get("is_error") is True:
        return True
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype.startswith("error"):
        return True
    return data.get("status") in ("error", "failed")


class StreamProcessor:
    """Process streaming JSON output from various CLIs.

    Recognized formats:
    - Claude / Cursor: a single ``{"type": "result", "result": ...}`` line
    - Gemini stream: ``init`` then assistant ``message`` lines, ending with ``result``
    - Codex stream: ``thread.started`` then ``item.completed`` lines, ending with ``turn.completed``
    """

    def __init__(self):
        self.result_json = None
        self.gemini_parts = []
        self.codex_messages = []
        self.is_gemini = False
        self.is_codex = False
        # Telemetry captured from stream events (None when the CLI doesn't emit it):
        self.session_id = None  # claude session_id / codex thread_id / cursor chat id
        self.usage = None       # token usage dict
        self.cost_usd = None    # claude total_cost_usd
        self.model = None       # the model that actually SERVED the run, when reported
        self.models_used = []   # every model id seen in modelUsage (resolved is only the dominant one)
        self.is_error = False   # the terminal event itself reported an error (claude is_error / result status)

    def process_line(self, line: str) -> bool:
        """Process one line. Returns True when a terminal event is reached."""
        line = line.strip()
        if not line or self.result_json is not None:
            return False

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return False

        # A line can be valid JSON but not an object (e.g. a plain-text backend
        # whose answer line is the bare number ``42`` or a quoted string). Those
        # are not stream events — ignore them rather than crashing on .get().
        if not isinstance(data, dict):
            return False

        # Claude stream-json init: {"type":"system","subtype":"init","session_id":...}
        # (distinct from gemini's bare {"type":"init"}). Capture the session id so
        # the caller can resume this conversation later with --resume.
        if data.get("type") == "system" and data.get("subtype") == "init":
            if data.get("session_id"):
                self.session_id = data["session_id"]
            if data.get("model"):
                self.model = data["model"]
            return False

        if data.get("type") == "init":
            self.is_gemini = True
            if data.get("session_id"):
                self.session_id = data["session_id"]
            return False

        if data.get("type") == "thread.started":
            self.is_codex = True
            if data.get("thread_id"):
                self.session_id = data["thread_id"]
            if data.get("model"):
                self.model = data["model"]
            return False

        if self.is_gemini and data.get("type") == "message" and data.get("role") == "assistant":
            content = data.get("content", "")
            if isinstance(content, str):
                self.gemini_parts.append(content)
            return False

        if self.is_codex and data.get("type") == "item.completed":
            item = data.get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self.codex_messages.append(item["text"])
            return False

        # Codex: turn.completed signals end (and carries token usage)
        if self.is_codex and data.get("type") == "turn.completed":
            if isinstance(data.get("usage"), dict):
                self.usage = data["usage"]
            self.result_json = {
                "type": "result",
                "result": "\n".join(self.codex_messages),
                "status": "success",
            }
            return True

        # Result type signals completion
        if data.get("type") == "result":
            self._capture_telemetry(data)
            # A terminal result can itself report failure: claude sets is_error /
            # subtype "error_*"; gemini/cursor may carry status "error"/"failed".
            # Record it so build_final_response never stamps such a run "success"
            # (the no-false-success guarantee must hold on the terminal event too).
            self.is_error = _terminal_is_error(data)
            if self.is_gemini:
                status = data.get("status", "success")
                self.is_error = self.is_error or status in ("error", "failed")
                self.result_json = {
                    "type": "result",
                    "result": "".join(self.gemini_parts),
                    "status": "error" if self.is_error else status,
                }
            else:
                self.result_json = data
            return True

        # Fallback: first valid JSON without a type field (some cursor result
        # shapes). Capture telemetry here too, so a session/chat id isn't lost.
        if "type" not in data:
            self._capture_telemetry(data)
            self.result_json = data
            return True

        return False

    def _capture_telemetry(self, data: dict) -> None:
        """Pull session id / usage / cost from a terminal result object. Claude's
        result carries cost/usage/session_id; cursor's may carry a chat id."""
        if isinstance(data.get("usage"), dict):
            self.usage = data["usage"]
        if isinstance(data.get("total_cost_usd"), (int, float)):
            self.cost_usd = data["total_cost_usd"]
        for key in ("session_id", "chatId", "chat_id"):
            if data.get(key):
                self.session_id = data[key]
                break
        # Served model: claude's result carries modelUsage (a dict keyed by
        # model id); some CLIs put a flat "model" field on the result object.
        if isinstance(data.get("model"), str) and data["model"]:
            self.model = data["model"]
        elif isinstance(data.get("modelUsage"), dict) and data["modelUsage"]:
            # `resolved` is only the DOMINANT model (most output tokens). A claude
            # session often also uses a cheap auxiliary model (e.g. haiku for a
            # background step), so exposing every model id in `models_used` keeps
            # the telemetry honest — an orchestrator must not read `resolved` as
            # "the one model that served this run".
            def _out(v):
                return v.get("outputTokens", 0) if isinstance(v, dict) else 0
            self.models_used = sorted(data["modelUsage"])
            self.model = max(data["modelUsage"], key=lambda k: _out(data["modelUsage"][k]))

    def get_result(self):
        return self.result_json
