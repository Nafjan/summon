"""``openai-compat`` backend — call any OpenAI-compatible /chat/completions API.

Lets you add ANY model reachable over the OpenAI Chat Completions protocol as a
summon agent: OpenRouter, OpenAI, Anthropic, Google (Gemini compat), Groq, or a
LOCAL server (Ollama, LM Studio, vLLM, llama.cpp). Pure stdlib (urllib) — no SDK.

Unlike the CLI backends this bills your **API key / credits**, not a subscription
(see TERMS.md) — the clean path for commercial or high-volume use.

Agent frontmatter (two equivalent styles):

    ---                                   ---
    run-agent: openai-compat              run-agent: openai-compat
    provider: openrouter                  base_url: http://localhost:11434/v1
    model: anthropic/claude-3.5-sonnet    api_key_env: ""        # local, no key
    ---                                   model: llama3.1
                                          ---

Providers resolve from built-ins + an optional ``providers.json`` in the agents
dir (or ``~/.agents/providers.json``): { "myprovider": {"base_url": "...",
"api_key_env": "MY_KEY"} }. Inline base_url/api_key_env override a provider.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request


def _redact(text: str, secret: str | None) -> str:
    """Strip the API key value from any text that might reach the envelope/log —
    some endpoints reflect the Authorization header in error bodies."""
    if secret and text:
        return text.replace(secret, "***REDACTED***")
    return text

# Sensible defaults so common providers work with just `provider:` + `model:`.
BUILTIN_PROVIDERS = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    "openai":     {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "anthropic":  {"base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY"},
    "google":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                   "api_key_env": "GEMINI_API_KEY"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
    "together":   {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "ollama":     {"base_url": "http://localhost:11434/v1", "api_key_env": ""},   # local, no key
    "lmstudio":   {"base_url": "http://localhost:1234/v1", "api_key_env": ""},    # local, no key
}


def load_providers(agents_dir: str | None) -> dict:
    """Built-ins overlaid with providers.json from the agents dir and ~/.agents."""
    providers = {k: dict(v) for k, v in BUILTIN_PROVIDERS.items()}
    paths = []
    if agents_dir:
        paths.append(os.path.join(agents_dir, "providers.json"))
    paths.append(os.path.join(os.path.expanduser("~"), ".agents", "providers.json"))
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for name, cfg in data.items():
                    if isinstance(cfg, dict) and cfg.get("base_url"):
                        providers[name] = cfg
        except (OSError, ValueError):
            continue
    return providers


def resolve_endpoint(frontmatter: dict, agents_dir: str | None) -> tuple:
    """Return ``(base_url, api_key_env)`` from inline fields or a named provider.
    Raises ValueError on an unresolvable/unknown provider."""
    _raw_base = frontmatter.get("base_url")
    if _raw_base is not None and not isinstance(_raw_base, str):
        raise ValueError(f"base_url must be a string, got {type(_raw_base).__name__}")
    base_url = (_raw_base or "").strip()
    api_key_env = frontmatter.get("api_key_env")
    provider = (frontmatter.get("provider") or "").strip()
    if not base_url and provider:
        providers = load_providers(agents_dir)
        if provider not in providers:
            raise ValueError(f"unknown provider {provider!r} "
                             f"(known: {', '.join(sorted(providers))}, or set base_url)")
        entry = providers[provider]
        if not isinstance(entry, dict):
            raise ValueError(f"provider {provider!r} must be an object with a base_url, "
                             f"got {type(entry).__name__}")
        base_url = entry.get("base_url", "")
        if api_key_env is None:
            api_key_env = entry.get("api_key_env")
    # Type-check what a registry (or frontmatter) supplied: `{"base_url": 42}` reached
    # .rstrip() and raised AttributeError, which only the last-resort crash envelope caught.
    for _name, _val in (("base_url", base_url), ("api_key_env", api_key_env)):
        if _val is not None and not isinstance(_val, str):
            raise ValueError(f"{_name} must be a string, got {type(_val).__name__}")
    if not base_url:
        raise ValueError("openai-compat agent needs a `provider:` or a `base_url:`")
    return base_url.rstrip("/"), (api_key_env or "")


class _NoCrossHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Never carry the API credential to a host the operator did not configure.

    urllib's default handler replays ALL original headers on a redirect, so an endpoint
    answering `302 Location: https://attacker.example/...` received the `Authorization:
    Bearer <key>` header verbatim -- a configured (or compromised, or merely mistaken)
    base_url could exfiltrate the key with one response. Redirects to the SAME origin are
    still followed, since those are ordinary API routing; anything cross-origin is refused
    outright rather than followed without the header, because a silent downgrade to an
    unauthenticated request would produce a confusing 401 instead of naming the real
    problem.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit

        def origin(u):
            # Normalize the DEFAULT port: https://x/v1 and https://x:443/v2 are the SAME
            # origin, and refusing that as "cross-host" contradicted the documented
            # same-origin guarantee.
            parts = urlsplit(u)
            default = {"http": 80, "https": 443}.get(parts.scheme)
            return parts.scheme, parts.hostname, (parts.port or default)

        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if origin(req.full_url) != origin(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"refusing to follow a cross-host redirect to {new.scheme}://"
                f"{new.hostname} (the API credential would travel with it)",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    """An opener that will not hand the credential to another host on a redirect."""
    return urllib.request.build_opener(_NoCrossHostAuthRedirect)


def call(inv, timeout_ms: int) -> dict:
    """Make one Chat Completions request for the invocation. Returns a response
    dict in the same shape the subprocess backends produce (result/status/
    exit_code/cli + usage/cost_usd/model_resolved), so it flows through _enrich."""
    cli = "openai-compat"
    if not inv.model:
        return _err(cli, "openai-compat agent needs a `model:` (the API model id)")
    if not inv.base_url:
        return _err(cli, "openai-compat: no base_url resolved (set provider: or base_url:)")

    api_key = os.environ.get(inv.api_key_env) if inv.api_key_env else None
    if inv.api_key_env and not api_key:
        return _err(cli, f"openai-compat: ${inv.api_key_env} is not set")

    body = json.dumps({
        "model": inv.model,
        "messages": [
            {"role": "system", "content": inv.system_context or ""},
            {"role": "user", "content": inv.prompt},
        ],
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(inv.base_url + "/chat/completions", data=body,
                                 headers=headers, method="POST")
    # Every error string below is _redact()ed with the key before returning, so a
    # reflected Authorization header can never reach the envelope or --debug-dir.
    try:
        with _opener().open(req, timeout=max(1, timeout_ms / 1000)) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        return _err(cli, _redact(f"HTTP {e.code} from {inv.base_url}: {detail or e.reason}", api_key))
    except (urllib.error.URLError, TimeoutError) as e:
        return _err(cli, _redact(f"request failed ({inv.base_url}): {e}", api_key))
    except (ValueError, http.client.HTTPException, OSError) as e:
        # ValueError = bad JSON; HTTPException = IncompleteRead etc.; OSError =
        # mid-read socket failure. All become a clean error envelope.
        return _err(cli, _redact(f"bad/failed response from {inv.base_url}: "
                                 f"{type(e).__name__}: {e}", api_key))

    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _err(cli, _redact(f"unexpected response shape: {json.dumps(payload)[:300]}", api_key))
    if text is None:
        text = ""
    if not isinstance(text, str):
        # non-string content (e.g. a tool-call array) — stringify so downstream
        # report parsing (which does .splitlines()) never crashes.
        text = json.dumps(text)
    # Redact on the SUCCESS path too: a misbehaving/hostile endpoint can echo the
    # Authorization value back in the completion content, which would otherwise
    # land verbatim in the envelope and --debug-dir. Error paths already redact.
    text = _redact(text, api_key)

    resp = {"result": text, "exit_code": 0, "status": "success", "cli": cli}
    if isinstance(payload.get("usage"), dict):
        resp["usage"] = payload["usage"]
    if isinstance(payload.get("model"), str):
        resp["model_resolved"] = payload["model"]
    return resp


def _err(cli: str, msg: str) -> dict:
    return {"result": "", "exit_code": 1, "status": "error", "cli": cli, "error": msg}
