#!/usr/bin/env python3
"""Fake ACP agent for tests — speaks newline-delimited JSON-RPC over stdio.

Usage: python fake_acp_agent.py acp [MODE]

Modes:
  happy            chunks + tool_call + report block, stopReason end_turn
  permission-read  asks permission for a read-kind tool call first
  permission-exec  asks permission for an execute-kind tool call first
  permission-no-once  offers ONLY allow_always for a read-kind call
  permission-empty   offers an empty options list for a read-kind call
  no-stopreason      answers session/prompt with a result missing stopReason
  slow             never answers session/prompt (timeout/cancel tests)
  malformed        emits a non-JSON line on stdout mid-stream
  lingering        answers the turn, then sleeps (teardown must not wait)
  refusal          stopReason refusal

`--help` prints text containing "acp" so _probe_acp passes; set env
FAKE_ACP_NO_HELP=1 to print help WITHOUT the token (probe-failure test).
"""
import json
import os
import sys
import time

REPORT = ("Final report\nSTATUS: DONE\nSUMMARY: fake turn completed\n"
          "FOLLOW-UP: none\nHANDOFF: none")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    if "--help" in sys.argv:
        if os.environ.get("FAKE_ACP_NO_HELP"):
            print("usage: fake [options]\nA fake agent. No protocol flags here.")
        else:
            print("usage: fake [options]\n  acp    run in acp mode")
        return
    if "--version" in sys.argv:
        print("fake-acp-agent 1.0")
        return

    mode = sys.argv[2] if len(sys.argv) > 2 else "happy"
    session_id = "fake-session-1"
    pending_permission = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg:
            # A response to OUR request (the permission answer).
            if pending_permission is not None and msg.get("id") == pending_permission:
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text",
                                           "text": "permission answered: "
                                                   + json.dumps(msg.get("result")) + "\n"}}}})
                pending_permission = None
            continue

        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1, "agentCapabilities": {}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "sessionId": session_id,
                "models": {"availableModels": [
                    {"modelId": "fake-model", "name": "Fake Model"}],
                    "currentModelId": "fake-model"}}})
        elif method == "session/set_model":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/cancel":
            print("cancel received", file=sys.stderr, flush=True)
        elif method == "session/prompt":
            if mode == "slow":
                time.sleep(3600)  # kill-tree must reap us; never answer
                continue
            if mode == "malformed":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
            if mode.startswith("permission-"):
                kind = "execute" if mode == "permission-exec" else "read"
                if mode == "permission-no-once":
                    options = [{"optionId": "allow_always", "name": "Always",
                                "kind": "allow_always"}]
                elif mode == "permission-empty":
                    options = []
                else:
                    options = [
                        {"optionId": "allow_always", "name": "Always",
                         "kind": "allow_always"},
                        {"optionId": "allow_once", "name": "Once",
                         "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "No",
                         "kind": "reject_once"}]
                pending_permission = 999
                send({"jsonrpc": "2.0", "id": 999,
                      "method": "session/request_permission",
                      "params": {"sessionId": session_id,
                                 "toolCall": {"toolCallId": "t1", "kind": kind,
                                              "title": "fake tool", "status": "pending"},
                                 "options": options}})
                # Wait for the answer before finishing the turn.
                for ans in sys.stdin:
                    amsg = json.loads(ans)
                    if amsg.get("id") == 999:
                        send({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": session_id, "update": {
                                  "sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text",
                                              "text": "permission answered: "
                                                      + json.dumps(amsg.get("result"))
                                                      + "\n"}}}})
                        break
            if mode == "no-stopreason":
                # Protocol violation: PromptResponse MUST carry stopReason.
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
                continue
            for chunk in ("Hello ", "from the fake agent.\n\n", REPORT):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": chunk}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "tool_call", "toolCallId": "t0",
                           "title": "fake read", "kind": "read",
                           "status": "completed"}}})
            stop = "refusal" if mode == "refusal" else "end_turn"
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": stop}})
            if mode == "lingering":
                time.sleep(3600)  # post-turn linger: teardown must not wait


if __name__ == "__main__":
    main()
