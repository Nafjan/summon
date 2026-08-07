"""arkcli subprocess backend — profile-auth chat dual-wired beside openai-compat.

Uses ``arkcli +chat --model <id> …`` with the active arkcli profile store.
Text-only; pin a concrete model (not ``auto``). Does not store or print API keys.

BytePlus Coding Plan HTTP seats remain on ``openai-compat``; this path is the
CLI dual-wire for the same subscription family.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def _arkcli_cmd() -> list[str]:
    """Resolve an invocable arkcli argv prefix.

    On Windows, prefer ``node <pkg>/scripts/run.js`` over ``cmd /c arkcli.cmd``
    so user prompts are NOT re-parsed by cmd.exe (metacharacters in prompts
    must not become shell injection). Falls back to the .cmd shim only when
    the npm package layout cannot be found.
    """
    if os.name == "nt":
        path = shutil.which("arkcli.cmd") or shutil.which("arkcli")
        if path and str(path).lower().endswith((".cmd", ".bat")):
            node_js = _arkcli_node_entry(path)
            if node_js:
                node = shutil.which("node") or "node"
                return [node, node_js]
            return ["cmd", "/c", path]
        if path:
            return [path]
    path = shutil.which("arkcli")
    return [path] if path else ["arkcli"]


def _arkcli_node_entry(shim_path: str) -> str | None:
    """Return absolute path to ``@byteplus/ark-cli`` ``scripts/run.js`` if present."""
    shim_dir = os.path.dirname(shim_path)
    candidates = [
        os.path.join(shim_dir, "node_modules", "@byteplus", "ark-cli",
                     "scripts", "run.js"),
    ]
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        candidates.append(os.path.join(
            appdata, "npm", "node_modules", "@byteplus", "ark-cli",
            "scripts", "run.js"))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def call(inv, timeout_ms: int) -> dict:
    cli = "arkcli"
    if not shutil.which("arkcli") and not (os.name == "nt" and shutil.which("arkcli.cmd")):
        return _err(cli, "arkcli not found on PATH — install @byteplus/ark-cli "
                         "and run arkcli auth login")
    model = getattr(inv, "model", None) or ""
    if not model or str(model).strip().lower() in ("auto", "ark-code-latest"):
        return _err(cli, "arkcli backend needs a concrete model id "
                         "(not auto / ark-code-latest)")
    prompt = getattr(inv, "prompt", "") or ""
    system = getattr(inv, "system_context", "") or ""
    cmd = [*_arkcli_cmd(), "+chat", "--no-progress", "--model", str(model)]
    if system:
        cmd.extend(["--instructions", system])
    resume = getattr(inv, "resume_id", None)
    if resume:
        cmd.extend(["--previous-response-id", str(resume)])
    # End-of-options so a prompt starting with "-" cannot be parsed as flags.
    cmd.append("--")
    cmd.append(prompt)
    from _spawn import run_flags
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max(1, int(timeout_ms) / 1000.0),
            stdin=subprocess.DEVNULL, **run_flags())
    except subprocess.TimeoutExpired:
        return _err(cli, f"arkcli +chat timed out after {timeout_ms}ms")
    except OSError as e:
        return _err(cli, f"arkcli spawn failed: {e}")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    text = out
    usage = None
    # Prefer JSON envelope from arkcli when present.
    if out.startswith("{"):
        try:
            payload = json.loads(out)
            if isinstance(payload, dict):
                text = (payload.get("content")
                        or payload.get("result")
                        or payload.get("text")
                        or out)
                if not isinstance(text, str):
                    text = json.dumps(text)
                usage = payload.get("usage")
        except ValueError:
            pass
    if proc.returncode != 0 and not text:
        return _err(cli, err or f"arkcli exited {proc.returncode}")
    resp: dict[str, Any] = {
        "result": text,
        "status": "success" if proc.returncode == 0 else "error",
        "exit_code": proc.returncode,
        "cli": cli,
        "model": {"requested": model, "targeted": model,
                  "served": model, "resolved": model, "models_used": []},
        "backend_type": "arkcli_chat",
        "served_via": "arkcli_chat",
        "provider": {"driver": "arkcli"},
        "billing": {"source": "subscription",
                    "note": "arkcli +chat via profile store (dual-wire with openai-compat HTTP)"},
    }
    if usage:
        resp["usage"] = usage
    if proc.returncode != 0:
        resp["error"] = err[:500] if err else f"arkcli exited {proc.returncode}"
        resp["status"] = "error"
    return resp


def _err(cli: str, msg: str) -> dict:
    return {"result": "", "status": "error", "exit_code": 1, "cli": cli,
            "error": msg, "backend_type": "arkcli_chat",
            "served_via": "arkcli_chat", "provider": {"driver": "arkcli"}}
