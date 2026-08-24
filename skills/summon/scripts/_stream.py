"""StreamProcessor: parse newline-delimited JSON output from various CLIs."""

from __future__ import annotations

import json


_OPENCODE_DOTTED_EVENTS = {
    "message.part.updated",
    "message.updated",
    "session.created",
    "session.updated",
}
_OPENCODE_PART_TYPES = {
    "text",
    "reasoning",
    "tool",
    "tool_use",
    "tool_result",
    "step_start",
    "step_finish",
    "snapshot",
    "patch",
    "file",
    "subtask",
    "agent",
    "retry",
    "compaction",
}


def _opencode_part_type(value) -> str | None:
    """Return the stable spelling used by the stream parser.

    OpenCode has emitted both ``step_start``/``step_finish`` and
    ``step-start``/``step-finish`` over its JSON event variants.  Keep the
    normalization local to the parser; the provider's raw event is never
    surfaced as public provenance.
    """
    if not isinstance(value, str):
        return None
    value = value.strip().lower().replace("-", "_")
    return value if value in _OPENCODE_PART_TYPES else None


def _unwrap_opencode_event(data: dict) -> dict:
    """Normalize OpenCode's dotted event envelopes without making them terminal.

    OpenCode 1.x has used both flattened events (``type: text``) and event
    envelopes such as ``type: message.part.updated`` whose payload lives under
    ``properties`` or ``data``.  The latter is also the shape persisted by the
    local event store.  Only recognized OpenCode event names are unwrapped;
    all other JSON remains untouched for the other backend parsers.
    """
    raw_type = data.get("type")
    if not isinstance(raw_type, str):
        return data
    event_type = raw_type.strip().lower()
    base_type = event_type
    suffix = event_type.rsplit(".", 1)
    if len(suffix) == 2 and suffix[1].isdigit():
        base_type = suffix[0]
    if base_type not in _OPENCODE_DOTTED_EVENTS:
        # Flat events still get the same hyphen/underscore part handling.
        part = data.get("part")
        if isinstance(part, dict):
            canonical = _opencode_part_type(part.get("type"))
            if canonical and canonical != part.get("type"):
                normalized = dict(data)
                normalized["part"] = {**part, "type": canonical}
                return normalized
        return data

    normalized = dict(data)
    # Both OpenCode CLI releases and the persisted event API have used these
    # payload keys.  Merge rather than replace so sessionID/time fields at the
    # outer layer remain available to the metadata collector.
    for key in ("data", "properties"):
        payload = normalized.get(key)
        if isinstance(payload, dict):
            normalized.update(payload)
    normalized.pop("data", None)
    normalized.pop("properties", None)
    normalized["_summon_opencode_event"] = base_type

    if base_type == "message.part.updated":
        part = normalized.get("part")
        canonical = _opencode_part_type(part.get("type") if isinstance(part, dict) else None)
        if isinstance(part, dict) and canonical:
            normalized["part"] = {**part, "type": canonical}
            normalized["type"] = canonical
        else:
            # Unknown progress parts must remain non-terminal and must not fall
            # through to Cursor's typeless/result handling.
            normalized["type"] = "opencode_part"
    elif base_type == "message.updated":
        normalized["type"] = "message_updated"
    elif base_type == "session.created":
        normalized["type"] = "session_created"
    else:
        normalized["type"] = "session_updated"
    return normalized


def _terminal_is_error(data) -> bool:
    """True when a terminal result object reports failure. Claude sets
    ``is_error: true`` and an ``error_*`` subtype on API/turn failures; some
    CLIs put ``status`` of ``error``/``failed`` on the result. Guards the
    no-false-success contract at the stream layer. Non-dict input (defensive —
    ``result_json`` is always a dict today) is treated as not-an-error."""
    if not isinstance(data, dict):
        return False
    if data.get("is_error") is True:
        return True
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype.startswith("error"):
        return True
    status = data.get("status")
    if isinstance(status, str):
        status = status.lower()
    return status in ("error", "failed")


class StreamProcessor:
    """Process streaming JSON output from various CLIs.

    Recognized formats:
    - Claude / Cursor: a single ``{"type": "result", "result": ...}`` line
    - Gemini stream: ``init`` then assistant ``message`` lines, ending with ``result``
    - Codex stream: ``thread.started`` then ``item.completed`` lines, ending with
      ``turn.completed`` or ``turn.failed``
    """

    def __init__(self):
        self.result_json = None
        self.gemini_parts = []
        self.codex_messages = []
        self.is_gemini = False
        self.is_codex = False
        self.is_kimi = False
        self.kimi_parts = []
        # OpenCode's `run --format json` emits step_start/text/step_finish
        # events and defines success at clean EOF rather than a terminal result
        # object.  Keep a separate accumulator so those events are not mistaken
        # for an incomplete stream.
        self.is_opencode = False
        self.opencode_parts = []
        self.opencode_event_count = 0
        self.opencode_step_finish_seen = False
        # OpenCode can serialize an otherwise terminal-looking step with an
        # ``unknown`` finish reason and all-zero usage when its provider/model
        # produced no usable completion. Keep this diagnostic separate from
        # ordinary completion evidence; ``step_finish`` alone is not proof that
        # a response was generated.
        self.opencode_finish_reason = None
        self.opencode_zero_output_finish = False
        self.opencode_zero_token_finish = False
        # Telemetry captured from stream events (None when the CLI doesn't emit it):
        self.session_id = None  # claude session_id / codex thread_id / cursor chat id
        self.usage = None       # token usage dict
        self.cost_usd = None    # claude total_cost_usd
        # Two model slots, split by EVIDENCE: the init handshake announces what
        # the session is POINTED AT before any inference happens, so it must
        # never masquerade as the served model (field case: a failed Fable
        # dispatch reported the handshake model as `resolved` with zero tokens).
        self.handshake_model = None  # from init / thread.started (targeted, not served)
        self.model = None       # from the TERMINAL event only (model field / modelUsage)
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

        # OpenCode 1.x has two JSONL dialects in the wild.  Normalize the
        # dotted event envelope before any backend-specific terminal checks so
        # ``message.part.updated`` cannot be mistaken for a generic result (or
        # silently ignored as an unknown event).
        data = _unwrap_opencode_event(data)

        # Claude stream-json init: {"type":"system","subtype":"init","session_id":...}
        # (distinct from gemini's bare {"type":"init"}). Capture the session id so
        # the caller can resume this conversation later with --resume.
        if data.get("type") == "system" and data.get("subtype") == "init":
            if data.get("session_id"):
                self.session_id = data["session_id"]
            if data.get("model"):
                self.handshake_model = data["model"]
            return False

        # Agy stream-json emits session events under `event` instead of `type`
        # for the legacy one-shot stream (init/step_update/result). Capture the
        # session and model details there so resume can continue later and
        # model provenance stays accurate.
        if data.get("event") == "init":
            if data.get("conversation_id"):
                self.session_id = data["conversation_id"]
            if data.get("model"):
                self.handshake_model = data["model"]
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
                self.handshake_model = data["model"]
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

        # Codex emits ``turn.failed`` for provider/runtime failures. It is a
        # terminal event, not an ordinary progress record. Older Summon
        # versions ignored it, waited for EOF, and then reported only the raw
        # non-zero exit with no structured terminal result. Preserve bounded
        # provider detail as an error result so the executor can attach its
        # normal diagnostics while keeping model service evidence absent.
        if data.get("type") == "turn.failed":
            self.is_codex = True
            self.is_error = True
            detail = data.get("error")
            if isinstance(detail, dict):
                for key in ("message", "detail", "reason", "code"):
                    value = detail.get(key)
                    if isinstance(value, str) and value.strip():
                        detail = value
                        break
                else:
                    detail = None
            if not isinstance(detail, str) or not detail.strip():
                for key in ("message", "detail", "reason"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        detail = value
                        break
            if not isinstance(detail, str) or not detail.strip():
                detail = "Codex reported that the turn failed"
            self.result_json = {
                "type": "result",
                "result": "",
                "status": "error",
                "error": detail[:2000],
            }
            return True

        # Kimi Code's stream-json protocol is JSONL messages rather than
        # terminal events.  ANY role-bearing record (system/user/tool/meta/
        # assistant) is conversational, never a terminal result -- including a
        # leading system record before the first assistant message, which must
        # not be mistaken for a cursor-style typeless result and truncate the
        # run at line one.  Assistant content accumulates until EOF.
        if "type" not in data and "event" not in data and isinstance(data.get("role"), str):
            self.is_kimi = True
            if data["role"] == "assistant":
                content = data.get("content", "")
                if isinstance(content, str):
                    self.kimi_parts.append(content)
                elif isinstance(content, list):
                    self.kimi_parts.extend(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str))
            return False

        # OpenCode's JSON event protocol (v1.x) uses step_start, text,
        # tool_use/tool_result, and step_finish packets.  There is no final
        # result packet; a clean EOF is the terminal signal.  Capture model and
        # usage telemetry wherever a provider/part exposes it, without turning
        # the handshake into served-model evidence.
        if data.get("type") in {
            "step_start", "text", "reasoning", "tool_use", "tool_result",
            "step_finish", "session_created", "message_updated",
            "session_updated", "opencode_part",
        } and (data.get("sessionID") or data.get("session_id")
               or data.get("part") is not None
               or data.get("_summon_opencode_event")):
            self.is_opencode = True
            self.opencode_event_count += 1
            if data.get("type") == "step_finish":
                self.opencode_step_finish_seen = True
                self._capture_opencode_finish(data)
            self._capture_opencode_metadata(data)
            if data.get("type") == "text":
                part = data.get("part")
                text = part.get("text") if isinstance(part, dict) else data.get("text")
                if isinstance(text, str):
                    self.opencode_parts.append(text)
            return False

        if data.get("type") in {"error", "session_error", "message_error"}:
            self.is_opencode = True
            self.is_error = True
            self._capture_opencode_metadata(data)
            detail = data.get("error") or data.get("message") or data.get("text") or "OpenCode provider error"
            self.result_json = {
                "type": "result", "result": "", "status": "error",
                "error": str(detail)[:2000],
            }
            return True

        # A few OpenCode builds have emitted the event envelope without the
        # top-level ``type`` while retaining the session/part shape.  Do not
        # send that progress packet through the legacy cursor fallback below:
        # doing so turns the first text/tool update into a terminal result and
        # makes the child look as if it completed after one sentence.  The
        # explicit part-type allowlist keeps cursor's typeless ``result``
        # objects terminal and avoids treating arbitrary JSON as OpenCode.
        if ("type" not in data and "event" not in data
                and (data.get("sessionID") or data.get("session_id"))
                and isinstance(data.get("part"), dict)
                and data["part"].get("type") in {
                    "text", "reasoning", "tool", "step-start", "step-finish",
                    "snapshot", "patch", "file", "subtask", "agent", "retry",
                    "compaction",
                }):
            self.is_opencode = True
            self.opencode_event_count += 1
            part = data["part"]
            if part.get("type") == "step-finish":
                self.opencode_step_finish_seen = True
                self._capture_opencode_finish(data, part)
            self._capture_opencode_metadata(data)
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                self.opencode_parts.append(part["text"])
            return False

        # Codex: turn.completed signals end (and carries token usage)
        if self.is_codex and data.get("type") == "turn.completed":
            if isinstance(data.get("usage"), dict):
                self.usage = data["usage"]
            # Newer Codex builds may expose the provider-served identity on the
            # terminal event. A thread.started model is only a handshake target;
            # never promote it to served evidence. Accept the documented/common
            # spellings but keep malformed values out of the processor state so
            # the executor can classify the result as unverified.
            containers = [data]
            for nested_key in ("result", "turn", "metadata", "response"):
                nested = data.get(nested_key)
                if isinstance(nested, dict):
                    containers.append(nested)
            for container in containers:
                for key in ("model", "served_model", "servedModel", "model_id"):
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        self.model = value.strip()
                        break
                if self.model:
                    break
            self.result_json = {
                "type": "result",
                "result": "\n".join(self.codex_messages),
                "status": "success",
            }
            return True

        # Result type signals completion
        if data.get("type") == "result" or data.get("event") == "result":
            self._capture_telemetry(data)
            # agy's `result` event wraps terminal output under a `result`
            # object. The payload is intentionally similar to a terminal event:
            # `status` + `response` + `usage`, but keyed differently from
            # Claude/Gemini.
            payload = data.get("result")
            if isinstance(payload, dict) and "response" in payload:
                raw_status = payload.get("status")
                if raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
                    status = "success"
                elif isinstance(raw_status, str):
                    status = raw_status.lower()
                else:
                    status = str(raw_status).lower()
                self.is_error = _terminal_is_error(payload)
                self.result_json = {
                    "type": "result",
                    "result": payload.get("response", ""),
                    "status": "error" if self.is_error else status,
                }
                if raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
                    self.result_json["blank_status_fallback"] = True
                self._capture_telemetry(payload)
                if payload.get("error"):
                    self.result_json["error"] = payload.get("error")
                return True
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
                if data.get("error"):  # keep the terminal error for a precise message
                    self.result_json["error"] = data["error"]
            else:
                self.result_json = data
            return True

        # Fallback: first valid JSON without a `type` field (some cursor result
        # shapes). Capture telemetry here too, so a session/chat id isn't lost.
        # AGY also emits non-terminal `event` records (`step_update`), so we
        # must not treat any event-bearing packet as completion.
        if "type" not in data and "event" not in data:
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

    def _capture_opencode_metadata(self, data: dict) -> None:
        """Capture bounded telemetry from an OpenCode JSON event."""
        if not isinstance(data, dict):
            return
        for key in ("sessionID", "session_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                self.session_id = value.strip()
                break
        containers = [data]
        part = data.get("part")
        if isinstance(part, dict):
            containers.append(part)
        info = data.get("info")
        if isinstance(info, dict):
            containers.append(info)
        part_type = part.get("type") if isinstance(part, dict) else None
        # OpenCode's step_start carries ``modelID`` for the session/model
        # selection, not a provider-authored terminal served-model receipt.
        # Keep it as the handshake target so a zero-output finish cannot be
        # reported as served merely because the session announced its model.
        is_handshake = (
            data.get("type") in {"step_start", "step-start", "session_created"}
            or part_type == "step-start"
            or data.get("_summon_opencode_event") in {
                "message.updated", "session.created"
            }
        )
        if is_handshake:
            for container in containers:
                for key in ("model", "modelID", "model_id"):
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        self.handshake_model = value.strip()
                        break
                if self.handshake_model:
                    break
        else:
            for container in containers:
                # Only explicit terminal/provider model fields count as served
                # evidence. modelID in an ordinary progress packet remains a
                # target-style hint and is never promoted on its own.
                for key in ("model", "served_model", "servedModel"):
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        self.model = value.strip()
                        break
                if self.model:
                    break
        for container in containers:
            tokens = container.get("tokens")
            if isinstance(tokens, dict):
                self.usage = {
                    "input_tokens": tokens.get("input", 0),
                    "output_tokens": tokens.get("output", 0),
                    "total_tokens": tokens.get("total", 0),
                    "reasoning_tokens": tokens.get("reasoning", 0),
                }
                cache = tokens.get("cache")
                if isinstance(cache, dict):
                    self.usage["cache_read_tokens"] = cache.get("read", 0)
                    self.usage["cache_write_tokens"] = cache.get("write", 0)
                break
            usage = container.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
                break
        for container in containers:
            cost = container.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.cost_usd = cost
                break

    def _capture_opencode_finish(self, data: dict, part: dict | None = None) -> None:
        """Capture safe diagnostics from an OpenCode ``step_finish`` event.

        OpenCode's JSON stream carries the finish reason and usage under the
        ``part`` object. The values are provider-controlled, so keep only a
        short reason and boolean zero-output indicators; never copy arbitrary
        provider payloads into a receipt.
        """
        finish = part if isinstance(part, dict) else data.get("part")
        if not isinstance(finish, dict):
            finish = data if isinstance(data, dict) else {}
        reason = finish.get("reason")
        if isinstance(reason, str) and reason.strip():
            self.opencode_finish_reason = reason.strip()[:80]
        self.opencode_zero_output_finish = False
        self.opencode_zero_token_finish = False
        tokens = finish.get("tokens")
        if not isinstance(tokens, dict):
            return

        def _number_from(container: dict, name: str) -> float | None:
            value = container.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return None

        def _number(name: str) -> float | None:
            return _number_from(tokens, name)

        output = _number("output")
        total = _number("total")
        input_tokens = _number("input")
        reasoning = _number("reasoning")
        cache = tokens.get("cache")
        cache_read = cache_write = None
        if isinstance(cache, dict):
            cache_read = _number_from(cache, "read")
            cache_write = _number_from(cache, "write")
        if output is not None:
            self.opencode_zero_output_finish = output <= 0
        # OpenCode does not consistently emit ``total`` (the observed shape
        # contains input/output/reasoning plus cache counters only).  A missing
        # aggregate must not turn an otherwise unambiguous all-zero finish into
        # a false negative.  Require output plus at least one complete token
        # counter and consider every numeric counter that the provider supplied.
        available = [value for value in (
            output, total, input_tokens, reasoning, cache_read, cache_write)
            if value is not None]
        if output is not None and len(available) >= 2:
            self.opencode_zero_token_finish = all(value <= 0 for value in available)

    def get_result(self):
        return self.result_json

    def finalize_stream(self) -> None:
        """Finish protocols whose success is defined by clean EOF, not an event."""
        if self.result_json is None and self.is_kimi:
            self.result_json = {
                "type": "result",
                "result": "\n".join(p for p in self.kimi_parts if p),
                "status": "success",
            }
        if self.result_json is None and self.is_opencode:
            self.result_json = {
                "type": "result",
                "result": "".join(self.opencode_parts),
                "status": "error" if self.is_error else "success",
            }
