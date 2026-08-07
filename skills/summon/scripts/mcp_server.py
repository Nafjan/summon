"""Minimal stdio MCP server for Summon (stdlib JSON-RPC).

Every tool handler is isolated: exceptions become structured MCP errors so one
broken backend cannot crash the whole server (premortem T3/T9).
Responses are structured JSON text — never ANSI/CLI tables.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUN = SCRIPTS / "run_subagent.py"


def _rpc_ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_err(id_, code: int, message: str, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _run_summon(args: list[str], timeout_s: float = 120.0) -> dict:
    cmd = [sys.executable, str(RUN), *args]
    from _spawn import run_flags
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, cwd=str(SCRIPTS.parent.parent.parent),
            **run_flags())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "timeout_s": timeout_s}
    except OSError as e:
        return {"ok": False, "error": f"spawn_failed: {e}"}
    out = (proc.stdout or "").strip()
    # Prefer last JSON object on stdout.
    payload = None
    if out.startswith("{"):
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
    if payload is None and out:
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
    result = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "payload": payload,
    }
    # Never return raw CLI tails when structured JSON is available — stderr/stdout
    # can echo auth headers, dotenv paths, or key fragments (ADR-mcp-facade).
    if payload is None:
        result["stdout_tail"] = _scrub_secretish(out[-2000:] if out else "")
        result["stderr_tail"] = _scrub_secretish((proc.stderr or "")[-1000:])
    return result


def _scrub_secretish(text: str) -> str:
    """Best-effort scrub of key-shaped tokens before returning MCP error tails."""
    if not text:
        return text
    import re
    text = re.sub(r"ark-[A-Za-z0-9_-]{8,}", "ark-***REDACTED***", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+",
                  r"\1=***REDACTED***", text)
    home = os.path.expanduser("~")
    if home and home in text:
        text = text.replace(home, "~")
    return text


TOOLS = [
    {"name": "summon_doctor",
     "description": "Run summon doctor (--json). Optional probe=true for live checks.",
     "inputSchema": {"type": "object",
                     "properties": {"probe": {"type": "boolean"}},
                     "additionalProperties": False}},
    {"name": "summon_list_agents",
     "description": "List available summon agents.",
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False}},
    {"name": "summon_dispatch",
     "description": "Dispatch one agent; returns the JSON envelope.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "agent": {"type": "string"},
                         "prompt": {"type": "string"},
                         "cwd": {"type": "string"},
                         "model": {"type": "string"},
                         "timeout_ms": {"type": "integer"},
                     },
                     "required": ["agent", "prompt"],
                     "additionalProperties": False}},
    {"name": "summon_council",
     "description": "Run council mode with a question.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "question": {"type": "string"},
                         "cwd": {"type": "string"},
                         "members": {"type": "string"},
                         "rounds": {"type": "integer"},
                     },
                     "required": ["question"],
                     "additionalProperties": False}},
    {"name": "summon_manifest",
     "description": "Run a manifest swarm from a file path.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "file": {"type": "string"},
                         "cwd": {"type": "string"},
                         "concurrency": {"type": "integer"},
                     },
                     "required": ["file"],
                     "additionalProperties": False}},
    {"name": "summon_onboard_status",
     "description": "Detect CLIs and return onboard prefs status (no secrets).",
     "inputSchema": {"type": "object",
                     "properties": {"write": {"type": "boolean"}},
                     "additionalProperties": False}},
]


def _call_tool(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    try:
        if name == "summon_doctor":
            args = ["--doctor", "--json"]
            if arguments.get("probe"):
                args.append("--probe")
            return _run_summon(args, timeout_s=180.0)
        if name == "summon_list_agents":
            return _run_summon(["--list"], timeout_s=30.0)
        if name == "summon_dispatch":
            args = ["--agent", str(arguments["agent"]),
                    "--prompt", str(arguments["prompt"])]
            if arguments.get("cwd"):
                args += ["--cwd", str(arguments["cwd"])]
            if arguments.get("model"):
                args += ["--model", str(arguments["model"])]
            if arguments.get("timeout_ms"):
                args += ["--timeout", f"{int(arguments['timeout_ms'])}ms"]
            return _run_summon(args, timeout_s=max(30.0, int(arguments.get("timeout_ms") or 120000) / 1000.0 + 5))
        if name == "summon_council":
            args = ["--council", "--question", str(arguments["question"])]
            if arguments.get("cwd"):
                args += ["--cwd", str(arguments["cwd"])]
            if arguments.get("members"):
                args += ["--members", str(arguments["members"])]
            if arguments.get("rounds"):
                args += ["--rounds", str(int(arguments["rounds"]))]
            return _run_summon(args, timeout_s=600.0)
        if name == "summon_manifest":
            args = ["--manifest", str(arguments["file"])]
            if arguments.get("cwd"):
                args += ["--cwd", str(arguments["cwd"])]
            if arguments.get("concurrency"):
                args += ["--concurrency", str(int(arguments["concurrency"]))]
            return _run_summon(args, timeout_s=600.0)
        if name == "summon_onboard_status":
            args = ["--onboard", "--json"]
            if not arguments.get("write"):
                args.append("--no-write")
            return _run_summon(args, timeout_s=60.0)
        return {"ok": False, "error": f"unknown_tool:{name}"}
    except Exception as e:  # noqa: BLE001 — never crash the MCP process
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _handle(msg: dict):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return _rpc_ok(mid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "summon", "version": "2.0.0"},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _rpc_ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = _call_tool(name, arguments)
        text = json.dumps(result, ensure_ascii=False)
        return _rpc_ok(mid, {
            "content": [{"type": "text", "text": text}],
            "isError": not result.get("ok", False),
        })
    if method == "ping":
        return _rpc_ok(mid, {})
    return _rpc_err(mid, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            resp = _handle(msg)
        except Exception as e:  # noqa: BLE001
            resp = _rpc_err(msg.get("id"), -32603, f"{type(e).__name__}: {e}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
