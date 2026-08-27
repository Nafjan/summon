"""StreamProcessor: parse newline-delimited JSON output from various CLIs."""

from __future__ import annotations

import hashlib
import json
import re


_KIMI_PARTIAL_MAX_CHARS = 32 * 1024
_KIMI_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+() -]{0,159}$")
_MAX_LIVENESS_SEMANTIC_IDENTITIES = 4096


def _safe_kimi_model_id(value) -> str | None:
    """Accept only a bounded provider model identifier from a Kimi record."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (not candidate or "://" in candidate or "\\" in candidate
            or _KIMI_MODEL_ID_RE.fullmatch(candidate) is None):
        return None
    low = candidate.lower()
    if any(marker in low for marker in (
            "bearer ", "secret", "password", "api-key", "api_key", "token/")):
        return None
    return candidate


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

    def __init__(self, event_observer=None):
        self._event_observer = event_observer
        self._tool_progress = 0
        # Some provider stream dialects do not assign an event id to progress
        # records.  Keep only fixed-size hashes of their semantic payloads, so
        # a replay cannot manufacture fresh liveness without retaining model
        # text, tool arguments, or provider identifiers in parser state.
        self._liveness_semantic_ids: set[str] = set()
        self.result_json = None
        self.gemini_parts = []
        self.codex_messages = []
        self.is_gemini = False
        self.is_codex = False
        self.is_kimi = False
        self.kimi_parts = []
        # Kimi has no terminal-success record: a clean EOF is the only
        # completion boundary. Keep the normal accumulator unchanged for
        # successful runs, but maintain a bounded tail for a timeout snapshot.
        # The snapshot is diagnostic only and is never used as ``result``.
        self._kimi_partial_text = ""
        self._kimi_partial_part_count = 0
        self._kimi_partial_captured_chars = 0
        self._kimi_partial_truncated_chars = 0
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
        self.progress_usage = None  # advisory partial usage; never model-service evidence
        self.cost_usd = None    # claude total_cost_usd
        # Two model slots, split by EVIDENCE: the init handshake announces what
        # the session is POINTED AT before any inference happens, so it must
        # never masquerade as the served model (field case: a failed Fable
        # dispatch reported the handshake model as `resolved` with zero tokens).
        self.handshake_model = None  # from init / thread.started (targeted, not served)
        self.model = None       # from the TERMINAL event only (model field / modelUsage)
        self.models_used = []   # every model id seen in modelUsage (resolved is only the dominant one)
        self.model_evidence_source = None
        self.is_error = False   # the terminal event itself reported an error (claude is_error / result status)

    def _remember_semantic_activity(self, kind: str, identity: object) -> str | None:
        """Return a new activity digest, or ``None`` for a semantic replay.

        A line without a provider event id has no transport-level replay guard.
        We conservatively use a canonical, private digest of parser-recognized
        semantic fields.  The bounded cache stores only the digest.  If a
        provider cannot distinguish two identical no-id tool invocations, it
        cannot renew liveness on the second one; accepting that ambiguity would
        permit replayed packets to hold an adaptive lease open indefinitely.
        """
        try:
            encoded = json.dumps(
                [kind, identity], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return None
        digest = hashlib.sha256(encoded).hexdigest()
        if digest in self._liveness_semantic_ids:
            return None
        # Do not evict: rotating a bounded LRU lets an old replay become
        # "new" again and hold an adaptive lease indefinitely. Once the
        # conservative semantic budget is saturated, further output/tool
        # activity cannot renew liveness, even if it carries an outer provider
        # id. This availability tradeoff is intentionally fail-closed.
        if len(self._liveness_semantic_ids) >= _MAX_LIVENESS_SEMANTIC_IDENTITIES:
            return None
        self._liveness_semantic_ids.add(digest)
        return digest

    def _liveness(self, kind: str, data: dict | None = None, *,
                  semantic_identity: object = None, **fields) -> None:
        """Emit one parser-recognized event through the executor-owned capability."""
        if self._event_observer is None:
            return
        data = data if isinstance(data, dict) else {}
        session = data.get("session_id") or data.get("sessionID") \
            or data.get("thread_id") or data.get("conversation_id") or self.session_id
        source_id = data.get("event_id") or data.get("id")
        if kind in {"output_text", "tool_activity"}:
            semantic_digest = self._remember_semantic_activity(kind, semantic_identity)
            if semantic_digest is None:
                return
            # Some incremental protocols reuse a message/part id while its
            # content grows. Bind that outer id to the semantic delta instead
            # of treating changed work as an id replay. The observer receives
            # only a digest, never the provider id or semantic payload.
            source_id = hashlib.sha256(
                f"{source_id or ''}:{semantic_digest}".encode("ascii")
            ).hexdigest()
        try:
            self._event_observer.emit(
                kind, session_id=session if isinstance(session, str) else None,
                source_event_id=source_id if isinstance(source_id, str) else None,
                **fields)
        except Exception:  # liveness evidence must never break stream parsing
            pass

    def _bind_session(self, value, data: dict) -> None:
        """Bind or explicitly rotate the parser session for liveness evidence."""
        if not isinstance(value, str) or not value.strip():
            self._liveness("transport_started", data)
            return
        value = value.strip()
        old = self.session_id
        if old == value:
            return
        if old and self._event_observer is not None:
            try:
                accepted = self._event_observer.reconnect(
                    old_session_id=old, new_session_id=value,
                    source_event_id=(data.get("event_id") or data.get("id")))
                if accepted:
                    self.session_id = value
            except Exception:
                pass
        else:
            self.session_id = value
            self._liveness("transport_started", data)

    @staticmethod
    def _meaningful_chars(value) -> int:
        """Count visible generation for liveness without altering result text."""
        return len(value.strip()) if isinstance(value, str) else 0

    def process_line(self, line: str) -> bool:
        """Process one line. Returns True when a terminal event is reached."""
        line = line.strip()
        if not line or self.result_json is not None:
            return False

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Kimi can pretty-print a JSON payload over several lines. Once a
            # Kimi role record has identified the stream, preserve those lines
            # as assistant output instead of dropping them or letting a later
            # fragment reach a different backend's terminal fallback.
            if self.is_kimi:
                self._record_kimi_assistant(line)
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
            self._bind_session(data.get("session_id"), data)
            if data.get("model"):
                self.handshake_model = data["model"]
            return False

        # Agy stream-json emits session events under `event` instead of `type`.
        # Current releases put run configuration under the matching `init`
        # payload and step details under `step_update`; older releases also put
        # selected fields at the top level.  Read both shapes without flattening
        # arbitrary payload keys into the generic parser namespace.
        if data.get("event") == "init":
            init = data.get("init") if isinstance(data.get("init"), dict) else {}
            self._bind_session(data.get("conversation_id") or init.get("conversation_id"),
                               data)
            model = init.get("model") or data.get("model")
            if isinstance(model, str) and model:
                # The init packet describes the selected/targeted model.  It is
                # not a provider-reported served-model receipt.
                self.handshake_model = model
            return False

        if data.get("event") == "step_update":
            # AGY progress packets are trusted transport activity. Only explicit
            # assistant text or monotonically increasing tool steps reset idle.
            step = (data.get("step_update")
                    if isinstance(data.get("step_update"), dict) else data)
            self._bind_session(step.get("conversation_id") or data.get("conversation_id"),
                               data)
            # Step usage is provider telemetry, but a model-looking field on a
            # progress packet is never served-model evidence.  Avoid the generic
            # terminal telemetry helper here so progress cannot mint identity.
            if isinstance(step.get("usage"), dict):
                # Keep partial progress accounting separate from terminal usage.
                # The executor may use terminal output-token usage as weak,
                # explicitly inferred service evidence; a progress packet must
                # never activate even that weaker path.
                self.progress_usage = step["usage"]
            content = (step.get("text_delta") or step.get("response")
                       or step.get("content"))
            if isinstance(content, str) and content:
                self._liveness("output_text", data,
                               output_chars=self._meaningful_chars(content),
                               semantic_identity=("agy_text", self.session_id,
                                                  step.get("step_index"),
                                                  step.get("state"), content))
            elif (step.get("step_type") == "tool"
                  or step.get("tool") or step.get("tool_name")):
                tool = step.get("tool") or step.get("tool_name") or "agy_tool"
                step_index = step.get("step_index")
                if isinstance(step_index, int) and not isinstance(step_index, bool) \
                        and 0 <= step_index <= (1 << 62) - 1:
                    # ACTIVE then DONE for one step must be monotonic, while an
                    # exact replay of either packet remains a duplicate.
                    progress = step_index * 2 + (1 if step.get("state") == "DONE" else 0)
                else:
                    self._tool_progress += 1
                    progress = self._tool_progress
                self._liveness("tool_activity", data, tool_id=str(tool)[:128],
                               progress=progress,
                               semantic_identity=("agy_tool",
                                                  self.session_id, step.get("step_index"),
                                                  step.get("state"), tool))
            else:
                self._liveness("stream_event", data)
            return False

        if data.get("type") == "init":
            self.is_gemini = True
            self._bind_session(data.get("session_id"), data)
            return False

        if data.get("type") == "thread.started":
            self.is_codex = True
            self._bind_session(data.get("thread_id"), data)
            if data.get("model"):
                self.handshake_model = data["model"]
            return False

        # Claude stream-json emits complete assistant messages between init and
        # result. Text and tool-use blocks are executor-recognized progress;
        # their contents are never copied into the liveness projection.
        if data.get("type") == "assistant" and isinstance(data.get("message"), dict):
            message = data["message"]
            content = message.get("content")
            if isinstance(content, list):
                for index, block in enumerate(content):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        self._liveness("output_text", data,
                                       output_chars=self._meaningful_chars(block["text"]),
                                       semantic_identity=("claude_text", message.get("id"),
                                                          index, block["text"]))
                    elif block.get("type") in {"tool_use", "server_tool_use"}:
                        self._tool_progress += 1
                        self._liveness("tool_activity", data, tool_id="claude_tool",
                                       progress=self._tool_progress,
                                       semantic_identity=("claude_tool", message.get("id"),
                                                          index, block))
            return False

        if data.get("type") == "user" and isinstance(data.get("message"), dict):
            content = data["message"].get("content")
            tool_results = [block for block in content if isinstance(block, dict)
                            and block.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            if tool_results:
                self._tool_progress += 1
                self._liveness("tool_activity", data, tool_id="claude_tool_result",
                               progress=self._tool_progress,
                               semantic_identity=("claude_tool_result",
                                                  data["message"].get("id"), tool_results))
            else:
                self._liveness("stream_event", data)
            return False

        if self.is_gemini and data.get("type") == "message" and data.get("role") == "assistant":
            content = data.get("content", "")
            if isinstance(content, str):
                self.gemini_parts.append(content)
                self._liveness("output_text", data,
                               output_chars=self._meaningful_chars(content),
                               semantic_identity=("gemini_text", content))
            return False

        if self.is_codex and data.get("type") == "item.completed":
            item = data.get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self.codex_messages.append(item["text"])
                self._liveness("output_text", data,
                               output_chars=self._meaningful_chars(item["text"]),
                               semantic_identity=("codex_text", item.get("id"),
                                                  item["text"]))
            elif isinstance(item, dict):
                self._tool_progress += 1
                self._liveness("tool_activity", data, tool_id="codex_item",
                               progress=self._tool_progress,
                               semantic_identity=("codex_item", item))
            return False

        if self.is_gemini and data.get("type") in {"tool_call", "tool_result"}:
            self._tool_progress += 1
            self._liveness("tool_activity", data, tool_id="gemini_tool",
                           progress=self._tool_progress,
                           semantic_identity=("gemini_tool", data.get("type"),
                                              data.get("tool_call_id"),
                                              data.get("tool_use_id"), data.get("name"),
                                              data.get("args") or data.get("arguments")))
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
            self._liveness("terminal", data)
            return True

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
                    self._liveness("output_text", data,
                                   output_chars=self._meaningful_chars(text),
                                   semantic_identity=("opencode_text", part.get("id"),
                                                      text))
            elif data.get("type") in {"tool_use", "tool_result"}:
                self._tool_progress += 1
                part = data.get("part") if isinstance(data.get("part"), dict) else {}
                tool_id = part.get("callID") or part.get("id") or data.get("id") or "tool"
                self._liveness("tool_activity", data, tool_id=str(tool_id)[:128],
                               progress=self._tool_progress,
                               semantic_identity=("opencode_tool", part))
            elif data.get("type") == "step_finish":
                self._liveness("finalizing", data)
            else:
                self._liveness("stream_event", data)
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
            self._liveness("terminal", data)
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
                self._liveness("output_text", data,
                               output_chars=self._meaningful_chars(part["text"]),
                               semantic_identity=("opencode_part_text", part.get("id"),
                                                  part["text"]))
            elif part.get("type") in {"tool", "patch", "file", "subtask", "agent"}:
                self._tool_progress += 1
                tool_id = part.get("callID") or part.get("id") or part.get("type")
                self._liveness("tool_activity", data, tool_id=str(tool_id)[:128],
                               progress=self._tool_progress,
                               semantic_identity=("opencode_part", part))
            elif part.get("type") == "step-finish":
                self._liveness("finalizing", data)
            else:
                self._liveness("stream_event", data)
            return False

        # Kimi Code's stream-json protocol is conversational JSONL rather than
        # a terminal-event protocol. Newer Kimi releases include ``type`` on
        # role records (for example ``system.version``), so the old
        # type-less-only guard missed the stream entirely. This branch comes
        # after OpenCode's recognized event shapes so an OpenCode part carrying
        # a role is still parsed as OpenCode.
        if isinstance(data.get("role"), str) and not self.is_opencode:
            self._capture_kimi_record(data)
            role = str(data.get("role") or "").strip().lower()
            if role == "assistant":
                content = data.get("content")
                chars = self._meaningful_chars(content)
                if data.get("tool_calls") or data.get("tool_call"):
                    self._tool_progress += 1
                    self._liveness("tool_activity", data, tool_id="kimi_tool",
                                   progress=self._tool_progress,
                                   semantic_identity=("kimi_tool",
                                                      data.get("tool_calls")
                                                      or data.get("tool_call")))
                if chars:
                    self._liveness("output_text", data, output_chars=chars,
                                   semantic_identity=("kimi_text", content))
                elif not (data.get("tool_calls") or data.get("tool_call")):
                    self._liveness("stream_event", data)
            else:
                self._liveness("stream_event", data)
            return False

        # Once Kimi has been identified, an untyped object is content, not a
        # cursor-style terminal result. Kimi commonly emits a final verifier
        # object without ``role``; preserving it lets the normal report parser
        # inspect the content while clean EOF remains the only Kimi completion
        # boundary. A non-zero child exit is still handled by the executor.
        if self.is_kimi:
            self._record_kimi_payload(data)
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
            self._liveness("terminal", data)
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
                self._liveness("terminal", data)
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
            self._liveness("terminal", data)
            return True

        # Fallback: first valid JSON without a `type` field (some cursor result
        # shapes). Capture telemetry here too, so a session/chat id isn't lost.
        # AGY also emits non-terminal `event` records (`step_update`), so we
        # must not treat any event-bearing packet as completion.
        if "type" not in data and "event" not in data:
            self._capture_telemetry(data)
            self.result_json = data
            self._liveness("terminal", data)
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

    def _capture_kimi_metadata(self, data: dict) -> None:
        """Capture provider-authored identity/usage from Kimi JSONL records.

        Kimi commonly emits only role-bearing records and no terminal result.
        When an assistant record carries an explicit model field, it is the
        strongest available served-model evidence. A system/meta record is kept
        as a handshake target only. If neither appears, the executor leaves
        ``model.served`` null and marks provenance absent rather than guessing
        from the requested K3 profile.
        """
        if not isinstance(data, dict):
            return
        role = str(data.get("role") or "").strip().lower()
        containers = [data]
        for key in ("metadata", "meta", "response"):
            nested = data.get(key)
            if isinstance(nested, dict):
                containers.append(nested)

        if role == "assistant":
            for container in containers:
                found_model = None
                for key in (
                    "served_model", "servedModel", "model", "model_id",
                    "modelId", "model_name",
                ):
                    model = _safe_kimi_model_id(container.get(key))
                    if model:
                        found_model = model
                        break
                if found_model:
                    if self.model is None:
                        self.model = found_model
                    self.model_evidence_source = "kimi_assistant_record"
                    if found_model not in self.models_used:
                        self.models_used.append(found_model)
        elif role in {"system", "meta", "user"} and not self.handshake_model:
            # These records can identify the selected session/model but are not
            # evidence that an assistant completion was served by that model.
            for container in containers:
                for key in ("model", "model_id", "modelId", "model_name"):
                    model = _safe_kimi_model_id(container.get(key))
                    if model:
                        self.handshake_model = model
                        break
                if self.handshake_model:
                    break

        # Some Kimi versions attach usage to assistant records. Normalize the
        # common spellings so the executor can expose inferred provenance only
        # when output-token evidence is actually present.
        for container in containers:
            usage = container.get("usage") or container.get("token_usage")
            if not isinstance(usage, dict):
                continue
            normalized = {}
            for target, keys in (
                ("input_tokens", ("input_tokens", "prompt_tokens", "input")),
                ("output_tokens", ("output_tokens", "completion_tokens", "output")),
                ("total_tokens", ("total_tokens", "total")),
                ("reasoning_tokens", ("reasoning_tokens", "reasoning")),
            ):
                for key in keys:
                    value = usage.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        normalized[target] = value
                        break
            if normalized:
                self.usage = normalized
                break
        for container in containers:
            cost = container.get("cost_usd", container.get("cost"))
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.cost_usd = cost
                break

    def _capture_kimi_record(self, data: dict) -> None:
        """Capture a Kimi role record, including records that carry ``type``."""
        self.is_kimi = True
        self._capture_kimi_metadata(data)
        if str(data.get("role") or "").strip().lower() != "assistant":
            return
        content = data.get("content", "")
        if isinstance(content, str):
            self._record_kimi_assistant(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    self._record_kimi_assistant(part["text"])

    def _record_kimi_payload(self, data: dict) -> None:
        """Keep a non-role Kimi JSON payload as bounded, non-terminal content."""
        try:
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            payload = str(data)
        self._record_kimi_assistant(payload)

    def _capture_opencode_metadata(self, data: dict) -> None:
        """Capture bounded telemetry from an OpenCode JSON event."""
        if not isinstance(data, dict):
            return
        for key in ("sessionID", "session_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                self._bind_session(value, data)
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

    def _record_kimi_assistant(self, content: str) -> None:
        """Accumulate Kimi text and a bounded pre-EOF diagnostic tail.

        The full ``kimi_parts`` list remains the clean-EOF success path for
        compatibility. The separate bounded buffer prevents an unbounded
        timeout forensic artifact from growing with a long or hostile stream.
        """
        self.kimi_parts.append(content)
        if not content:
            return
        self._kimi_partial_part_count += 1
        self._kimi_partial_captured_chars += len(content)
        separator = 1 if self._kimi_partial_text else 0
        if len(content) >= _KIMI_PARTIAL_MAX_CHARS:
            removed = len(self._kimi_partial_text) + separator
            removed += len(content) - _KIMI_PARTIAL_MAX_CHARS
            self._kimi_partial_truncated_chars += max(0, removed)
            self._kimi_partial_text = content[-_KIMI_PARTIAL_MAX_CHARS:]
            return
        combined = self._kimi_partial_text + ("\n" if separator else "") + content
        if len(combined) > _KIMI_PARTIAL_MAX_CHARS:
            removed = len(combined) - _KIMI_PARTIAL_MAX_CHARS
            self._kimi_partial_truncated_chars += removed
            combined = combined[-_KIMI_PARTIAL_MAX_CHARS:]
        self._kimi_partial_text = combined

    def kimi_partial_snapshot(self) -> dict | None:
        """Return bounded, explicitly non-authoritative pre-EOF Kimi text."""
        if not self.is_kimi or not self._kimi_partial_text:
            return None
        return {
            "text": self._kimi_partial_text,
            "authoritative": False,
            "source": "stream_parts_pre_eof",
            "finalized": False,
            "part_count": self._kimi_partial_part_count,
            "captured_chars": self._kimi_partial_captured_chars,
            "bytes_retained": len(self._kimi_partial_text.encode("utf-8")),
            "truncated": bool(self._kimi_partial_truncated_chars),
            "truncated_chars": self._kimi_partial_truncated_chars,
        }

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
        self._liveness("terminal", {})
