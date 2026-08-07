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
    model: anthropic/claude-sonnet-5    api_key_env: ""        # local, no key
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
    # BytePlus ModelArk Coding Plan (subscription quota, not per-token API credits).
    # WARNING: /api/v3 without /coding/ is the pay-as-you-go platform endpoint and
    # bypasses your Coding Plan subscription quota entirely.
    # Region default is ap-southeast (BytePlus international); override via providers.json.
    "byteplus-coding": {
        "base_url": "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        "api_key_env": "BYTEPLUS_CODING_API_KEY",
    },
}

# Providers whose base_url MUST contain /api/coding (subscription path).
# A wrong URL silently bills PAYG — refuse rather than warn-and-continue.
_CODING_PLAN_PROVIDERS = frozenset({"byteplus-coding"})

# Curated, non-exhaustive recommendations. The plan roster is dynamic and can
# contain legacy or temporarily broken entries, so do not present it as a list
# of known-good models. Re-check with `arkcli plans model-list --plan coding-plan`
# and validate an actual text call before promoting a model here.
_CODING_PLAN_MODEL_GUIDANCE = (
    "deepseek-v4-flash (fast terminal loops), deepseek-v4-pro (hard coding), "
    "glm-5.2 (long-horizon/large-repo work), dola-seed-2.0-code (agentic coding), "
    "or kimi-k2.5 (code/UI alternative). Avoid legacy/superseded roster entries "
    "such as glm-5.1, bytedance-seed-code, and gpt-oss-120b for new work"
)

# Listed-but-discouraged / known-broken for new work (still may appear in roster).
_CODING_PLAN_LEGACY_OR_BROKEN = frozenset({
    "auto",
    "ark-code-latest",
    "glm-5.1",
    "bytedance-seed-code",
    "gpt-oss-120b",
})

# Preferred presentation order when a live roster is available.
_CODING_PLAN_PREFERRED = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5.2",
    "dola-seed-2.0-code",
    "kimi-k2.5",
    "dola-seed-2.0-pro",
    "dola-seed-2.0-lite",
)

_ROSTER_CACHE_NAME = "byteplus-coding-roster.json"
_ROSTER_MAX_AGE_S = 14 * 24 * 3600  # align with handoff refresh cadence


def _roster_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".agents", _ROSTER_CACHE_NAME)


def refresh_coding_plan_roster(plan: str = "coding-plan",
                               timeout_s: float = 45.0) -> dict:
    """Fetch Coding Plan models via ``arkcli plans model-list`` and cache them.

    Returns ``{"fetched_at", "plan", "models":[{"id","name","output_name",...}],
    "source":"arkcli"}``. Raises RuntimeError when arkcli is missing or fails.
    """
    import subprocess
    import time
    from _spawn import run_flags
    cmd = ["arkcli", "plans", "model-list", "--plan", plan, "--format", "json"]
    # On Windows, prefer arkcli.cmd when PATH resolution is via npm shim.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, shell=False, **run_flags())
    except FileNotFoundError:
        # npm global shim
        cmd0 = cmd[:]
        cmd0[0] = "arkcli.cmd" if os.name == "nt" else "arkcli"
        try:
            proc = subprocess.run(
                cmd0, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s, shell=True, **run_flags())
        except FileNotFoundError as e:
            raise RuntimeError(
                "arkcli not found on PATH; install @byteplus/ark-cli to refresh "
                "the Coding Plan roster") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"arkcli plans model-list failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '')[:400]}")
    try:
        raw = json.loads(proc.stdout or "{}")
    except ValueError as e:
        raise RuntimeError("arkcli plans model-list returned non-JSON") from e
    models = []
    for m in (raw.get("models") or []):
        if not isinstance(m, dict):
            continue
        models.append({
            "id": m.get("model_id") or m.get("id") or "",
            "name": m.get("model_name") or "",
            "output_name": m.get("output_name") or m.get("model_name") or "",
            "description": m.get("description") or "",
            "enabled_thinking": bool(m.get("enabled_thinking")),
            "selected": bool(m.get("selected")),
        })
    payload = {
        "fetched_at": time.time(),
        "plan": raw.get("plan") or plan,
        "selected_model_id": raw.get("selected_model_id") or raw.get("ark_latest_model_id"),
        "models": models,
        "source": "arkcli",
    }
    path = _roster_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return payload


def load_coding_plan_roster(*, refresh_if_stale: bool = False) -> dict | None:
    """Load cached Coding Plan roster; optionally refresh when older than 14 days.

    Missing cache returns None unless ``refresh_if_stale`` is set (then one
    arkcli fetch is attempted). Guidance callers should pass False so a cold
    machine stays offline and uses the static hint.
    """
    import time
    path = _roster_cache_path()
    data = None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            data = None
    except (OSError, ValueError):
        data = None
    if data is None:
        if not refresh_if_stale:
            return None
        try:
            return refresh_coding_plan_roster()
        except RuntimeError:
            return None
    stale = True
    if isinstance(data.get("fetched_at"), (int, float)):
        stale = (time.time() - float(data["fetched_at"])) > _ROSTER_MAX_AGE_S
    if refresh_if_stale and stale:
        try:
            return refresh_coding_plan_roster()
        except RuntimeError:
            return data
    return data


def coding_plan_model_guidance(*, refresh_if_stale: bool = False) -> str:
    """Task-oriented model hint, preferring a fresh arkcli roster when cached."""
    roster = load_coding_plan_roster(refresh_if_stale=refresh_if_stale)
    if not roster:
        return _CODING_PLAN_MODEL_GUIDANCE
    names = []
    for m in roster.get("models") or []:
        out = (m.get("output_name") or m.get("name") or "").strip()
        if out and out.lower() not in {x.lower() for x in _CODING_PLAN_LEGACY_OR_BROKEN}:
            names.append(out)
    if not names:
        return _CODING_PLAN_MODEL_GUIDANCE
    # Prefer known task fits first, then any remaining non-legacy roster names.
    ordered = [n for n in _CODING_PLAN_PREFERRED if n in names]
    for n in names:
        if n not in ordered:
            ordered.append(n)
    shown = ", ".join(ordered[:6])
    legacy = ", ".join(sorted(_CODING_PLAN_LEGACY_OR_BROKEN - {"auto", "ark-code-latest"}))
    return (
        f"{shown}. Avoid legacy/superseded roster entries such as {legacy} "
        f"for new work (roster cached from arkcli; refresh with "
        f"arkcli plans model-list --plan coding-plan)"
    )


def is_coding_plan_endpoint(base_url: str | None) -> bool:
    """True when the OpenAI-compat base URL is a BytePlus Coding Plan path.

    Matches on the URL *path* only (not query/fragment) so a PAYG URL cannot
    spoof Coding Plan detection via ``?x=/api/coding``.
    """
    if not base_url:
        return False
    from urllib.parse import urlsplit
    path = (urlsplit(base_url).path or "").lower()
    return "/api/coding" in path


def coding_plan_billing(base_url: str | None) -> dict | None:
    """Honest billing for Coding Plan URLs: subscription quota, not API credits."""
    if not is_coding_plan_endpoint(base_url):
        return None
    return {
        "source": "subscription",
        "note": "BytePlus Coding Plan (/api/coding/) - draws from subscription "
                "quota, not per-token API credits",
    }


def coding_plan_model_error(base_url: str | None, model: str | None) -> str | None:
    """Refuse known-bad Coding Plan model ids before paying for an HTTP 404.

    ``auto`` is an arkcli routing alias, not a ModelArk Chat Completions model —
    the upstream error falsely says the model 'does not support the coding plan'.
    """
    if not is_coding_plan_endpoint(base_url):
        return None
    mid = (model or "").strip()
    if mid.lower() == "auto":
        return (
            "BytePlus Coding Plan raw API does not accept model 'auto' "
            "(that routing is arkcli-only). Pin a recommended plan model: "
            f"{coding_plan_model_guidance()}. Refresh eligibility with: "
            "arkcli plans model-list --plan coding-plan"
        )
    return None


def _validate_coding_plan_url(provider: str, base_url: str) -> None:
    """Refuse Coding Plan providers pointed at the PAYG /api/v3 path."""
    if provider not in _CODING_PLAN_PROVIDERS:
        return
    if is_coding_plan_endpoint(base_url):
        return
    raise ValueError(
        f"provider {provider!r} must use the Coding Plan endpoint containing "
        f"/api/coding/ (got {base_url!r}). "
        f"Using /api/v3 without /coding/ silently bills pay-as-you-go and "
        f"bypasses your Coding Plan subscription quota. "
        f"Expected: https://ark.ap-southeast.bytepluses.com/api/coding/v3"
    )


def _rewrite_coding_plan_http_error(base_url: str, model: str, code: int, detail: str) -> str | None:
    """Make BytePlus UnsupportedModel errors actionable for Coding Plan users."""
    if not is_coding_plan_endpoint(base_url):
        return None
    low = (detail or "").lower()
    if "unsupportedmodel" not in low and "does not support the coding plan" not in low:
        return None
    return (
        f"Coding Plan rejected model {model!r} (HTTP {code}). "
        f"Pin a recommended plan model: {coding_plan_model_guidance()}. "
        f"'auto' is arkcli-only. Refresh eligibility with: "
        f"arkcli plans model-list --plan coding-plan. "
        f"Roster presence is not proof of live availability; validate a text call. "
        f"Upstream: {(detail or '')[:280]}"
    )


# ---------------------------------------------------------------------------
# PAYG consent-gated fallback
# ---------------------------------------------------------------------------

_PAYG_BILLING = {
    "source": "api",
    "note": "BytePlus PAYG (/api/v3) - per-token credits (consent-gated fallback)",
}

_PAYG_CONSENT_SURFACES = (
    "set SUMMON_ALLOW_BYTEPLUS_PAYG=1, or add "
    '{"allow_byteplus_payg": true} to ~/.agents/summon.json, '
    "or pass --allow-payg (single dispatch only)"
)


def payg_consent_allowed(allow_payg_flag: bool = False) -> bool:
    """True when the operator has consented to PAYG fallback billing.

    Sources (any one grants consent):
    - ``allow_payg_flag`` (from --allow-payg or frontmatter allow_payg: true)
    - env ``SUMMON_ALLOW_BYTEPLUS_PAYG=1``
    - ~/.agents/summon.json ``{"allow_byteplus_payg": true}``
    """
    if allow_payg_flag:
        return True
    if os.environ.get("SUMMON_ALLOW_BYTEPLUS_PAYG") == "1":
        return True
    prefs_path = os.path.join(os.path.expanduser("~"), ".agents", "summon.json")
    try:
        with open(prefs_path, encoding="utf-8") as fh:
            prefs = json.load(fh)
        if isinstance(prefs, dict) and prefs.get("allow_byteplus_payg") is True:
            return True
    except (OSError, ValueError):
        pass
    return False


def coding_to_payg_base_url(coding_url: str) -> str:
    """Rewrite a Coding Plan URL to its OpenAI-compat PAYG equivalent.

    Always produces ``…/api/v3`` on the same host (never bare ``…/api``), so
    both ``…/api/coding/v3`` and the Anthropic-compat ``…/api/coding`` root
    retry against the chat-completions PAYG path.
    Raises ValueError if input is not a Coding Plan URL.
    """
    if not is_coding_plan_endpoint(coding_url):
        raise ValueError(
            f"cannot rewrite non-Coding-Plan URL to PAYG: {coding_url!r}")
    from urllib.parse import urlsplit, urlunsplit
    import re
    parts = urlsplit(coding_url)
    new_path = re.sub(
        r"/api/coding(?:/v\d+)?/?", "/api/v3", parts.path or "",
        count=1, flags=re.IGNORECASE)
    # Collapse accidental double segments and ensure a clean /api/v3 ending.
    new_path = re.sub(r"/api/v3(?:/v\d+)?", "/api/v3", new_path, flags=re.IGNORECASE)
    new_path = new_path.rstrip("/") or "/api/v3"
    if "/api/v3" not in new_path.lower() or new_path == (parts.path or "").rstrip("/"):
        raise ValueError(f"URL rewrite produced no usable PAYG path: {coding_url!r}")
    return urlunsplit((parts.scheme, parts.netloc, new_path, "", ""))


def is_payg_fallback_worthy(http_code: int | None, detail: str) -> bool:
    """Should this Coding Plan HTTP error trigger a PAYG retry (if consented)?

    Only quota/rate/plan-limit and UnsupportedModel are fallback-worthy.
    Auth (401/403), network, timeout, and generic 5xx are NEVER fallback-worthy,
    even if their error body happens to contain words like ``quota``.
    """
    if http_code is None:
        return False
    # Hard deny: never bill PAYG because auth or infrastructure failed.
    if http_code in (401, 403) or http_code >= 500:
        return False
    low = (detail or "").lower()
    if http_code == 429:
        return True
    if not (400 <= http_code < 500):
        return False
    if "unsupportedmodel" in low or "does not support the coding plan" in low:
        return True
    # Explicit parentheses: quota alone, OR plan+(limit|exceed), OR rate+limit.
    if ("quota" in low
            or ("plan" in low and ("limit" in low or "exceed" in low))
            or ("rate" in low and "limit" in low)):
        return True
    return False


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
    base_url = base_url.rstrip("/")
    _validate_coding_plan_url(provider, base_url)
    return base_url, (api_key_env or "")


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
    _cp_model = coding_plan_model_error(inv.base_url, inv.model)
    if _cp_model:
        return _err(cli, _cp_model)

    api_key = os.environ.get(inv.api_key_env) if inv.api_key_env else None
    if inv.api_key_env and not api_key:
        msg = f"openai-compat: ${inv.api_key_env} is not set"
        if inv.api_key_env == "BYTEPLUS_CODING_API_KEY":
            msg += (" - set it to your Coding Plan profile API key "
                    "(from `arkcli auth status` / `arkcli auth apikey`), "
                    "not a short-lived SSO token")
        return _err(cli, msg)

    resp = _do_request(inv.base_url, inv.model, inv.system_context, inv.prompt,
                       api_key, timeout_ms, cli)

    # --- PAYG consent-gated fallback ---
    if (resp["status"] == "error"
            and is_coding_plan_endpoint(inv.base_url)
            and resp.get("_payg_fallback_worthy")):
        allow_payg = getattr(inv, "allow_payg", False)
        if not payg_consent_allowed(allow_payg):
            primary_err = resp.get("error", "")
            resp["error"] = (
                f"{primary_err}\n\n"
                f"PAYG fallback is available but consent is missing. "
                f"To allow a pay-as-you-go retry: {_PAYG_CONSENT_SURFACES}"
            )
            resp.pop("_payg_fallback_worthy", None)
            return resp
        try:
            payg_url = coding_to_payg_base_url(inv.base_url)
        except ValueError:
            resp.pop("_payg_fallback_worthy", None)
            return resp
        primary_error = resp.get("error", "")
        payg_resp = _do_request(payg_url, inv.model, inv.system_context,
                                inv.prompt, api_key, timeout_ms, cli)
        payg_resp.pop("_payg_fallback_worthy", None)
        if payg_resp["status"] == "success":
            payg_resp["billing"] = dict(_PAYG_BILLING)
            payg_resp["fallback"] = {
                "from": "coding-plan",
                "to": "payg",
                "reason": resp.get("_fallback_reason", "classified-error"),
                "primary_error": _redact(primary_error, api_key)[:300],
            }
            return payg_resp
        payg_resp["error"] = (
            f"Coding Plan error: {_redact(primary_error, api_key)[:200]}\n"
            f"PAYG retry also failed: {payg_resp.get('error', '')[:200]}"
        )
        return payg_resp

    resp.pop("_payg_fallback_worthy", None)
    resp.pop("_fallback_reason", None)
    return resp


def _do_request(base_url: str, model: str, system_context: str | None,
                prompt: str, api_key: str | None, timeout_ms: int,
                cli: str) -> dict:
    """Execute a single Chat Completions POST. Internal to call()."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_context or ""},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url + "/chat/completions", data=body,
                                 headers=headers, method="POST")
    try:
        with _opener().open(req, timeout=max(1, timeout_ms / 1000)) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        fallback_worthy = is_payg_fallback_worthy(e.code, detail or e.reason)
        rewritten = _rewrite_coding_plan_http_error(base_url, model, e.code, detail or e.reason)
        err_msg = rewritten or f"HTTP {e.code} from {base_url}: {detail or e.reason}"
        resp = _err(cli, _redact(err_msg, api_key))
        if fallback_worthy:
            resp["_payg_fallback_worthy"] = True
            resp["_fallback_reason"] = _classify_fallback_reason(e.code, detail)
        return resp
    except (urllib.error.URLError, TimeoutError) as e:
        return _err(cli, _redact(f"request failed ({base_url}): {e}", api_key))
    except (ValueError, http.client.HTTPException, OSError) as e:
        return _err(cli, _redact(f"bad/failed response from {base_url}: "
                                 f"{type(e).__name__}: {e}", api_key))

    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _err(cli, _redact(f"unexpected response shape: {json.dumps(payload)[:300]}", api_key))
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = json.dumps(text)
    text = _redact(text, api_key)

    resp = {"result": text, "exit_code": 0, "status": "success", "cli": cli}
    if isinstance(payload.get("usage"), dict):
        resp["usage"] = payload["usage"]
    if isinstance(payload.get("model"), str):
        resp["model_resolved"] = payload["model"]
    _bill = coding_plan_billing(base_url)
    if _bill:
        resp["billing"] = _bill
    return resp


def _classify_fallback_reason(http_code: int, detail: str) -> str:
    """Short reason tag for the fallback telemetry."""
    low = (detail or "").lower()
    if "unsupportedmodel" in low or "does not support the coding plan" in low:
        return "unsupported-model"
    if http_code == 429:
        return "rate-limit"
    if "quota" in low:
        return "quota-exhausted"
    return "plan-limit"


def _err(cli: str, msg: str) -> dict:
    return {"result": "", "exit_code": 1, "status": "error", "cli": cli, "error": msg}
