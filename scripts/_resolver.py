"""CLI resolution: detect calling environment, fall back to default.

Also hosts model discovery (:func:`discover_models`) — a live, best-effort
answer to "what can each backend run right now, and how do new models surface?"
so an orchestrator never has to rely on a stale hardcoded model list.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

# Valid backends come from the single registry in _builder (imported lazily to
# avoid an import cycle: _builder imports _loader, not _resolver).
def _valid_clis() -> tuple:
    try:
        from _builder import BACKEND_CLIS
        return BACKEND_CLIS
    except ImportError:
        return ("claude", "cursor-agent", "codex", "gemini", "agy", "openai-compat")


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
    except (FileNotFoundError, PermissionError, OSError):
        # /proc absent on macOS, may be unreadable under sandbox. Caller
        # detection is best-effort — fall through silently.
        pass

    return None


def resolve_cli(frontmatter_cli: str | None, default: str = "codex") -> str:
    """Resolve which CLI to use.

    Priority: frontmatter > caller detection > default.
    Invalid frontmatter values fall through to caller detection (lenient).
    """
    if frontmatter_cli and frontmatter_cli in _VALID_CLIS:
        return frontmatter_cli

    detected = detect_caller_cli()
    if detected:
        return detected

    return default


# --- Model discovery ----------------------------------------------------------
# Only ONE backend (agy) exposes a machine-readable model list; the others pick
# a model via --model/-m with no `models` subcommand. discover_models() is honest
# about this: it live-queries where it can, reads the CLI's own config where the
# default lives (codex), and otherwise returns the documented aliases/defaults
# clearly marked "static" — never a fabricated enumeration.

_CLAUDE_ALIASES = ("opus", "sonnet", "haiku")  # float to the latest release
_AGY_MODELS_TIMEOUT = 25


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
    """(source, models, note). Live `agy models` — the only backend with a
    machine-readable list. Fails soft: a missing/slow/erroring agy yields
    ("unavailable", [], reason) rather than raising."""
    exe = shutil.which("agy")
    if not exe:
        return "unavailable", [], "agy not on PATH"
    try:
        r = subprocess.run([exe, "models"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=_AGY_MODELS_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # errors="replace" prevents UnicodeDecodeError, but keep ValueError +
        # the broad SubprocessError base so NOTHING escapes the fail-soft contract.
        return "unavailable", [], f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return "unavailable", [], ((r.stderr or r.stdout or "").strip()[:200] or "non-zero exit")
    models = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return "live", models, None


def discover_models(cli: str | None = None) -> dict:
    """Report, per backend, what models are invocable and how new ones surface.

    Each entry carries a ``source`` so the caller knows how much to trust the list:
      - ``live``        queried from the CLI just now (only agy exposes this)
      - ``config``      read from the CLI's own default config (codex)
      - ``static``      documented aliases/defaults to pass via --model (the CLI
                        has no machine-readable list); NOT a live enumeration
      - ``unavailable`` a live query was attempted and failed (see ``note``)

    Pass ``cli`` (accepts "cursor" or "cursor-agent") to limit to one backend.
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

    # claude: no `models` subcommand. Model is chosen via --model, and the
    # opus/sonnet/haiku ALIASES auto-resolve to the latest release, so an
    # alias-pinned agent floats for free — no skill edit when a new model ships.
    if want("claude"):
        info["claude"] = {
            "source": "static",
            "aliases": list(_CLAUDE_ALIASES),
            "note": "Aliases auto-resolve to the latest model (float for free). "
                    "Full IDs (e.g. claude-opus-4-8, claude-fable-5) also accepted via --model.",
        }

    # codex: no `models` subcommand. Unpinned agents inherit ~/.codex/config.toml's
    # top-level `model`; -m/--model overrides per call.
    if want("codex"):
        codex_default = _codex_default_model()
        info["codex"] = {
            "source": "config" if codex_default else "static",
            "default": codex_default,
            "note": "Unpinned codex agents use ~/.codex/config.toml `model`. Any codex "
                    "model id works via --model; edit config.toml to move the default.",
        }

    # agy: the one backend with a live, machine-readable list (only probed when
    # in scope, per the early filter above).
    if want("agy"):
        src, models, note = _agy_live_models()
        info["agy"] = {"source": src, "models": models,
                       "note": note or "Live from `agy models` (Claude + Gemini + GPT-OSS lanes on the Google sub)."}

    # cursor-agent: no model list, no floating alias -> pinned default constant.
    if want("cursor-agent"):
        # Imported here (only when actually needed) so an unknown-cli or
        # non-cursor query never depends on _builder importing cleanly.
        from _builder import CURSOR_DEFAULT_MODEL
        info["cursor-agent"] = {
            "source": "static",
            "default": CURSOR_DEFAULT_MODEL,
            "note": "cursor-agent exposes no model list; --model accepts cursor model ids "
                    "(default = CURSOR_DEFAULT_MODEL in _builder.py, the single bump-point).",
        }

    # gemini: -m accepts model ids; only --list-extensions/--list-sessions exist.
    if want("gemini"):
        info["gemini"] = {
            "source": "static",
            "note": "gemini exposes no model list; -m/--model accepts gemini model ids; "
                    "unpinned uses gemini's own default.",
        }

    return info
