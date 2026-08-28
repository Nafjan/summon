"""CLI resolution: detect calling environment, fall back to default.

Also hosts model discovery (:func:`discover_models`) — a live, best-effort
answer to "what can each backend run right now, and how do new models surface?"
so an orchestrator never has to rely on a stale hardcoded model list.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

# Valid backends come from the single registry in _builder (imported lazily to
# avoid an import cycle: _builder imports _loader, not _resolver).
def _valid_clis() -> tuple:
    try:
        from _builder import BACKEND_CLIS
        return BACKEND_CLIS
    except ImportError:
        return ("claude", "cursor-agent", "codex", "gemini", "kimi", "agy",
                "opencode", "zcode", "arkcli", "openai-compat")


_VALID_CLIS = _valid_clis()


def detect_caller_cli() -> str | None:
    """Detect which CLI is calling this script (best-effort).

    Checks well-known env vars first, then falls back to a parent-process
    cmdline probe via /proc (Linux only — macOS lacks /proc and falls
    through silently).
    """
    if os.environ.get("CLAUDE_CODE"):
        return "claude"
    if os.environ.get("CURSOR_AGENT"):
        return "cursor-agent"
    if os.environ.get("CODEX_CLI"):
        return "codex"
    if os.environ.get("GEMINI_CLI"):
        return "gemini"
    if os.environ.get("KIMI_CODE"):
        return "kimi"

    try:
        ppid = os.getppid()
        cmdline_path = f"/proc/{ppid}/cmdline"
        if os.path.exists(cmdline_path):
            with open(cmdline_path) as f:
                cmdline = f.read().lower()
                if "claude" in cmdline:
                    return "claude"
                if "cursor" in cmdline:
                    return "cursor-agent"
                if "codex" in cmdline:
                    return "codex"
                if "gemini" in cmdline:
                    return "gemini"
                if "kimi" in cmdline:
                    return "kimi"
    except (FileNotFoundError, PermissionError, OSError):
        # /proc absent on macOS, may be unreadable under sandbox. Caller
        # detection is best-effort — fall through silently.
        pass

    return None


def resolve_cli(frontmatter_cli: str | None, default: str = "codex") -> str:
    """Resolve which CLI to use.

    Priority: frontmatter > caller detection > default. A frontmatter
    ``run-agent`` that is set but UNKNOWN is an authoring error and fails closed
    (raises ValueError) — silently falling through to the caller/codex would run
    the agent under the wrong vendor, permission model, and billing account.
    """
    if frontmatter_cli:
        if frontmatter_cli in _VALID_CLIS:
            return frontmatter_cli
        raise ValueError(
            f"unknown run-agent {frontmatter_cli!r} "
            f"(valid: {', '.join(sorted(_VALID_CLIS))})")

    detected = detect_caller_cli()
    if detected:
        return detected

    return default


# --- Model discovery ----------------------------------------------------------
# Only a subset of backends exposes a machine-readable model list; the others pick
# a model via --model/-m with no `models` subcommand. discover_models() is honest
# about this: it live-queries where it can, reads the CLI's own config where the
# default lives (codex), and otherwise returns the documented aliases/defaults
# clearly marked "static" — never a fabricated enumeration.

_CLAUDE_ALIASES = ("opus", "sonnet", "haiku")  # float to the latest release
_AGY_MODELS_TIMEOUT = 25
_AGY_MAX_MODELS = 10_000
_AGY_MAX_MODEL_ID = 256


def _agy_command(exe: str, *args: str, _platform: str | None = None) -> list[str]:
    """Build a shell-free AGY command, using cmd only for Windows shims."""
    platform = os.name if _platform is None else _platform
    if platform == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/d", "/c", exe, *args]
    return [exe, *args]


def _valid_agy_model_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not value or len(value) > _AGY_MAX_MODEL_ID
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        return None
    return value


def _parse_agy_json_models(stdout: str) -> list[str] | None:
    """Parse the documented `agy --output-format json models` envelope.

    ``None`` means the JSON contract was unavailable or malformed and permits
    the legacy text fallback.  An empty list is a valid machine response.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("status", "")).upper() != "SUCCESS":
        return None
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    values = data.get("models") if isinstance(data, dict) else None
    if not isinstance(command, dict) or command.get("name") != "models" \
            or not isinstance(values, list) or len(values) > _AGY_MAX_MODELS:
        return None
    models: list[str] = []
    seen: set[str] = set()
    for item in values:
        model_id = _valid_agy_model_id(item.get("id") if isinstance(item, dict) else None)
        if model_id is None:
            return None
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def _parse_agy_plain_models(stdout: str) -> list[str]:
    """Parse the pre-1.1.12 text list as a compatibility fallback."""
    models: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        model_id = _valid_agy_model_id(stripped.split("\t", 1)[0].split(None, 1)[0])
        if model_id is not None and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
        if len(models) >= _AGY_MAX_MODELS:
            break
    return models


def _codex_default_model() -> str | None:
    """The model an unpinned codex sub-agent actually runs: the top-level
    ``model`` key in ~/.codex/config.toml.

    Uses a real TOML parse (stdlib ``tomllib``, 3.11+): it returns nested tables
    as sub-dicts, so a section-scoped ``model`` (e.g. under ``[tui...]``) is never
    confused with the global one. On 3.11+ tomllib is authoritative — a read or
    parse failure returns None (not a scanned guess). Only Python 3.10 (no
    tomllib) uses the best-effort line scan.
    """
    cfg = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    try:
        import tomllib  # 3.11+
    except ImportError:
        # Python 3.10 only: no stdlib TOML parser -> best-effort line scan.
        return _codex_default_model_scan(cfg)
    # tomllib is AUTHORITATIVE. On any read/parse failure return None rather than
    # fall back to the naive scanner: a file tomllib rejects is malformed, and
    # scanning it would be *less* correct, not more. Nested [section].model is
    # returned as a sub-dict, so top-level .get("model") is never confused with it.
    try:
        with open(cfg, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):  # ValueError == tomllib.TOMLDecodeError
        return None
    val = data.get("model")
    return val if isinstance(val, str) else None


def _codex_default_model_scan(cfg: str) -> str | None:
    """Best-effort line scan for the Python 3.10 no-tomllib path ONLY.

    A top-level line beginning with ``[`` is treated as a table header (in valid
    TOML an array is always a value, ``k = [...]``, so a line *starting* with ``[``
    is a table/array-of-tables header) — this correctly stops at headers that a
    naive ``endswith(']')`` missed, e.g. ``[t] # note`` or ``[p."a#b"]``. Approximate
    by nature: a ``model =`` line inside a multiline string, or a multiline array
    whose continuation line starts with ``[``, could be misread — acceptable for
    this 3.10-only path. 3.11+ never reaches here (tomllib is authoritative above)."""
    try:
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("["):
                    break  # left the top-level table
                m = re.match(r'model\s*=\s*["\']([^"\']+)["\']', s)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _agy_live_models() -> tuple[str, list, str | None]:
    """Discover AGY models through JSON, with a legacy text fallback.

    Fails soft: a missing/slow/erroring AGY yields ``unavailable`` rather than
    raising.  Discovery is advisory and never becomes served-model evidence.
    """
    exe = shutil.which("agy")
    if not exe:
        return "unavailable", [], "agy not on PATH"
    from _spawn import run_flags
    try:
        r = subprocess.run(_agy_command(exe, "--output-format", "json", "models"),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_AGY_MODELS_TIMEOUT, stdin=subprocess.DEVNULL, **run_flags())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return "unavailable", [], f"{type(e).__name__}: {e}"
    if r.returncode == 0:
        models = _parse_agy_json_models(r.stdout or "")
        if models is not None:
            return "live", models, None

    # Older AGY releases do not accept --output-format on the models command.
    # This is one bounded local discovery fallback, not a model dispatch, retry,
    # or served-identity claim.
    try:
        legacy = subprocess.run(_agy_command(exe, "models"), capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=_AGY_MODELS_TIMEOUT, stdin=subprocess.DEVNULL,
                                **run_flags())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return "unavailable", [], f"{type(e).__name__}: {e}"
    if legacy.returncode != 0:
        detail = (legacy.stderr or legacy.stdout or r.stderr or r.stdout or "").strip()[:200]
        return "unavailable", [], detail or "non-zero exit"
    models = _parse_agy_plain_models(legacy.stdout or "")
    if not models:
        return "unavailable", [], "agy models returned no valid model ids"
    return "live", models, "Legacy text fallback from `agy models`; upgrade AGY for JSON discovery."


def _opencode_live_models() -> tuple[str, list, str | None]:
    """(source, models, note) from OpenCode's configured model roster.

    OpenCode exposes a provider/model list through ``opencode models``.  Keep
    this fail-soft and treat the result as discovery only: a listed model may
    still be unavailable for the account or require a provider-specific key.
    """
    exe = shutil.which("opencode")
    if not exe:
        return "unavailable", [], "opencode not on PATH"
    try:
        from _spawn import run_flags
        cmd = (["cmd", "/c", exe, "models"] if os.name == "nt"
               and exe.lower().endswith((".cmd", ".bat")) else [exe, "models"])
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_AGY_MODELS_TIMEOUT, stdin=subprocess.DEVNULL,
                           **run_flags())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return "unavailable", [], f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return "unavailable", [], ((r.stderr or r.stdout or "").strip()[:200]
                                    or "non-zero exit")
    models = [ln.strip() for ln in (r.stdout or "").splitlines()
              if "/" in ln and not ln.lstrip().startswith(("Error", "Warning"))]
    return "live", models, None


def _catalog_candidates(backend: str) -> list[dict]:
    """Return advisory catalog candidates without turning them into evidence."""
    try:
        from _model_catalog import load_catalog
        out = []
        for entry in load_catalog().get("entries", []):
            if entry.get("backend") != backend:
                continue
            out.append({
                "id": entry.get("target"),
                "name": entry.get("name"),
                "version": entry.get("version"),
                "label": entry.get("label"),
                "availability": entry.get("availability", "catalog_listed"),
                "dispatchable": bool(entry.get("dispatchable")),
            })
        return out
    except Exception:  # noqa: BLE001 - discovery is advisory and fail-soft
        return []


def discover_models(cli: str | None = None, *, refresh: bool = False) -> dict:
    """Report, per backend, what models are invocable and how new ones surface.

    Each entry carries a ``source`` so the caller knows how much to trust the list:
      - ``live``        queried from the CLI just now (agy or ArkCLI refresh)
      - ``config``      read from the CLI's own default config (codex)
      - ``static``      documented aliases/defaults to pass via --model (the CLI
                        has no machine-readable list); NOT a live enumeration
      - ``unavailable`` a live query was attempted and failed (see ``note``)

    Pass ``cli`` (accepts "cursor" or "cursor-agent") to limit to one backend.
    ``refresh=True`` may contact a provider roster endpoint where one exists;
    it never rewrites the editorial catalog and never proves account eligibility.
    """
    # Normalize + validate FIRST, before any import or backend work, so a
    # single-backend query never does another backend's work — critically,
    # `--cli codex` must not launch the live `agy` probe (a PATH exec that can
    # block for _AGY_MODELS_TIMEOUT seconds). `is not None` (not truthiness) so an
    # explicit empty `--cli ""` is rejected as invalid rather than read as "all".
    key = None
    if cli is not None:
        key = "cursor-agent" if cli in ("cursor", "cursor-agent") else cli
        if key not in _VALID_CLIS:
            return {key: {"source": "unknown", "note": f"no such backend: {cli!r}"}}

    def want(backend: str) -> bool:
        return key is None or key == backend

    info: dict = {}
    checked_at = int(time.time())

    def stamp(entry: dict, backend: str) -> dict:
        entry.setdefault("checked_at", checked_at)
        entry.setdefault("refresh_requested", bool(refresh))
        candidates = _catalog_candidates(backend)
        if candidates:
            entry.setdefault("catalog_candidates", candidates)
        return entry

    # claude: no `models` subcommand. Model is chosen via --model, and the
    # opus/sonnet/haiku ALIASES auto-resolve to the latest release, so an
    # alias-pinned agent floats for free — no skill edit when a new model ships.
    if want("claude"):
        info["claude"] = stamp({
            "source": "static",
            "aliases": list(_CLAUDE_ALIASES),
            "note": "Aliases auto-resolve to the latest model (float for free). "
                    "Full IDs (e.g. claude-opus-5, claude-fable-5) also accepted via --model. "
                    "NOTE: the aliases can LAG the newest release -- pin a full id to be sure.",
        }, "claude")

    # codex: no `models` subcommand. Unpinned agents inherit ~/.codex/config.toml's
    # top-level `model`; -m/--model overrides per call.
    if want("codex"):
        codex_default = _codex_default_model()
        info["codex"] = stamp({
            "source": "config" if codex_default else "static",
            "default": codex_default,
            "note": "Unpinned codex agents use ~/.codex/config.toml `model`. Any codex "
                    "model id works via --model; edit config.toml to move the default. "
                    "The CLI does not expose a complete model enumeration; catalog candidates "
                    "are advisory until a dispatch envelope proves model.served.",
        }, "codex")

    # agy: a backend with a live, machine-readable list (only probed when
    # in scope, per the early filter above).
    if want("agy"):
        src, models, note = _agy_live_models()
        info["agy"] = stamp({"source": src, "models": models,
                       "note": note or "Live from `agy models` (Claude + Gemini + GPT-OSS lanes on the Google sub)."}, "agy")

    # cursor-agent: no model list, no floating alias -> pinned default constant.
    if want("cursor-agent"):
        # Imported here (only when actually needed) so an unknown-cli or
        # non-cursor query never depends on _builder importing cleanly.
        from _builder import CURSOR_DEFAULT_MODEL
        info["cursor-agent"] = stamp({
            "source": "static",
            "default": CURSOR_DEFAULT_MODEL,
            "note": "cursor-agent exposes no model list; --model accepts cursor model ids "
                    "(default = CURSOR_DEFAULT_MODEL in _builder.py, the single bump-point).",
        }, "cursor-agent")

    # gemini: -m accepts model ids; only --list-extensions/--list-sessions exist.
    if want("gemini"):
        info["gemini"] = stamp({
            "source": "static",
            "note": "gemini exposes no model list; -m/--model accepts gemini model ids; "
                    "unpinned uses gemini's own default.",
        }, "gemini")

    # Kimi Code has no model-list command.  The selected default is in its own
    # TOML profile, but it can be an alias/provider-local identifier, so report
    # it as configuration rather than a fabricated live catalogue.
    if want("kimi"):
        home = os.environ.get("KIMI_CODE_HOME") or os.path.join(os.path.expanduser("~"), ".kimi-code")
        default = None
        try:
            import tomllib
            with open(os.path.join(home, "config.toml"), "rb") as fh:
                parsed = tomllib.load(fh)
            value = parsed.get("default_model")
            default = value if isinstance(value, str) else None
        except (ImportError, OSError, ValueError):
            pass
        info["kimi"] = stamp({
            "source": "config" if default else "static",
            "default": default,
            "note": "Kimi Code accepts --model aliases but exposes no model-list command; "
                    "the unpinned default comes from ~/.kimi-code/config.toml.",
        }, "kimi")

    if want("opencode"):
        src, models, note = _opencode_live_models()
        info["opencode"] = stamp({
            "source": src,
            "models": models,
            "note": note or "Live from `opencode models`; listed is not proof of account access.",
        }, "opencode")

    if want("zcode"):
        try:
            from _zcode import resolve_zcode_cli, zcode_version
            target = resolve_zcode_cli()
            version = zcode_version(target) if target else None
        except Exception:  # noqa: BLE001 - provider-free discovery stays fail-soft
            target, version = None, None
        info["zcode"] = stamp({
            "source": "local" if target else "unavailable",
            "version": version,
            "discovery_source": target.source if target else None,
            "models": [],
            "note": (
                "ZCode has no reviewed headless model selector. Its configured native "
                "provider/model is intentionally not read from private configuration and cannot be "
                "treated as model.served evidence. Use an explicit OpenCode Z.AI Coding Plan "
                "seat for a target-model selector."),
        }, "zcode")

    # ArkCLI/ModelArk exposes a Coding Plan roster. Keep the normal query
    # offline by reading the existing bounded cache; `--refresh` explicitly
    # asks arkcli to update it and may therefore require auth/network access.
    if want("arkcli"):
        try:
            from _apibackend import load_coding_plan_roster, refresh_coding_plan_roster
            note = None
            if refresh:
                # An explicit refresh means a fresh provider query, not merely
                # a stale-age check. Fall back to the bounded cache if the
                # provider is unavailable so discovery remains useful offline.
                try:
                    roster = refresh_coding_plan_roster()
                except Exception as exc:  # noqa: BLE001 - preserve cache on refresh failure
                    roster = load_coding_plan_roster(refresh_if_stale=False)
                    note = f"refresh unavailable: {type(exc).__name__}; using cache"
            else:
                roster = load_coding_plan_roster(refresh_if_stale=False)
        except Exception as exc:  # noqa: BLE001 - model discovery is fail-soft
            roster = None
            note = f"roster query unavailable: {type(exc).__name__}"
        if roster:
            fetched_at = roster.get("fetched_at")
            age_s = (max(0, int(time.time() - fetched_at))
                     if isinstance(fetched_at, (int, float)) else None)
            info["arkcli"] = stamp({
                "source": "live" if refresh and age_s is not None and age_s < 10 else "cache",
                "models": roster.get("models") or [],
                "selected_model_id": roster.get("selected_model_id"),
                "cache_age_s": age_s,
                "note": ("Coding Plan roster from arkcli; listed is not the same as "
                         "invocable or recommended. Verify model.served."),
            }, "arkcli")
        else:
            info["arkcli"] = stamp({
                "source": "unavailable", "models": [],
                "note": note or ("No Coding Plan roster cache. Run `summon models --cli "
                                  "arkcli --refresh` after `arkcli auth login`."),
            }, "arkcli")

    return info
