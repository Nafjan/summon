"""arkcli subprocess backend — profile-auth chat dual-wired beside openai-compat.

Uses ``arkcli +chat --model <id> …`` with the active arkcli profile store.
Text-only; pin a concrete model (not ``auto``). Does not store or print API keys.

BytePlus Coding Plan HTTP seats remain on ``openai-compat``; this path is the
CLI dual-wire for the same subscription family.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from typing import Any


def _is_windows() -> bool:
    """Return whether the active runtime uses Windows launch semantics.

    Keeping this behind a tiny seam lets provider-inert tests exercise the
    Windows shim policy on POSIX without mutating the process-wide ``os.name``
    value (which also changes how ``pathlib.Path`` behaves).
    """
    return os.name == "nt"


def _arkcli_cmd() -> list[str]:
    """Resolve an invocable arkcli argv prefix.

    On Windows, prefer ``node <pkg>/scripts/run.js`` over ``cmd /c arkcli.cmd``
    so user prompts are NOT re-parsed by cmd.exe (metacharacters in prompts
    must not become shell injection). A shim without its package entry point
    is refused instead of falling back to a command shell.
    """
    if _is_windows():
        path = shutil.which("arkcli.cmd") or shutil.which("arkcli")
        if path and str(path).lower().endswith((".cmd", ".bat")):
            node_js = _arkcli_node_entry(path)
            if node_js:
                node = shutil.which("node") or "node"
                return [node, node_js]
            raise RuntimeError(
                "arkcli package entry point is unavailable; reinstall "
                "@byteplus/ark-cli so prompts are not routed through cmd.exe")
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
    if not shutil.which("arkcli") and not (_is_windows() and shutil.which("arkcli.cmd")):
        return _err(cli, "arkcli not found on PATH — install @byteplus/ark-cli "
                         "and run arkcli auth login", not_run=True)
    model = getattr(inv, "model", None) or ""
    if not model or str(model).strip().lower() in ("auto", "ark-code-latest"):
        return _err(cli, "arkcli backend needs a concrete model id "
                         "(not auto / ark-code-latest)", not_run=True)
    prompt = getattr(inv, "prompt", "") or ""
    system = getattr(inv, "system_context", "") or ""
    try:
        cmd = [*_arkcli_cmd(), "+chat", "--no-progress", "--model", str(model)]
    except RuntimeError as e:
        return _err(cli, str(e), not_run=True,
                    error_kind="unsafe_windows_launcher")
    if system:
        cmd.extend(["--instructions", system])
    resume = getattr(inv, "resume_id", None)
    if resume:
        cmd.extend(["--previous-response-id", str(resume)])
    # End-of-options so a prompt starting with "-" cannot be parsed as flags.
    cmd.append("--")
    cmd.append(prompt)
    from _spawn import run_flags, scrub_provider_env
    child_env = scrub_provider_env(dict(os.environ))
    # ArkCLI is registered as an API-kind route but launches a real CLI. It
    # therefore needs the same final argv/environment measurement as ordinary
    # subprocess adapters. This is a local OS ceiling only; it is not a claim
    # about the provider's context window or plan quota.
    try:
        from _transport_budget import evaluate_invocation
        capability = ({"kind": "argv", "platform": "nt",
                       "max_utf16_units": 32767,
                       "max_env_utf16_units": 32767}
                      if os.name == "nt" else
                      {"kind": "argv", "platform": "posix",
                       "max_single_argument_bytes": 131072,
                       "max_total_bytes": 2_000_000})
        payload_text = ((system + "\n\n" + prompt) if system else prompt)
        transport_budget = evaluate_invocation(
            operation_id=hashlib.sha256(
                payload_text.encode("utf-8", errors="surrogatepass")).hexdigest()[:32],
            content=payload_text, command=cmd[0], args=cmd[1:], env=child_env,
            platform=os.name, capability=capability)
    except Exception:
        return _err(cli, "arkcli transport capability preflight refused",
                    not_run=True, error_kind="transport_capability_invalid")
    if transport_budget["status"] == "blocked":
        response = _err(
            cli,
            "serialized arkcli invocation exceeds the local command-line "
            "transport ceiling; shorten the required prompt or use a bounded "
            "file-aware route",
            not_run=True,
            error_kind="transport_budget_exceeded",
        )
        response["transport_budget"] = transport_budget
        return response
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max(1, int(timeout_ms) / 1000.0),
            stdin=subprocess.DEVNULL, env=child_env,
            **run_flags())
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


def _err(cli: str, msg: str, *, not_run: bool = False,
         error_kind: str | None = None) -> dict:
    value = {"result": "", "status": "error", "exit_code": 1, "cli": cli,
             "error": msg, "backend_type": "arkcli_chat",
             "served_via": "arkcli_chat", "provider": {"driver": "arkcli"}}
    if not_run:
        value.update({
            "attempts": 0,
            "attempt_status": "not_run",
            "execution_status": "not_run",
            "provider_contacted": False,
            "result_usable": False,
            "retryable": False,
        })
    if error_kind:
        value["error_kind"] = error_kind
    return value
