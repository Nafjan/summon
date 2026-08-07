"""Agent definition loading: frontmatter parsing, validation, file discovery."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from pathlib import Path

PERMISSION_VALUES = ("read-only", "safe-edit", "yolo")
DEFAULT_PERMISSION = "safe-edit"

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# sha256 of the bytes the most recent load actually PARSED, keyed by resolved path. The
# dispatch compares this (not a fresh read) with what the request identity recorded, so an
# A -> B -> A swap around the parse cannot slip past.
_LAST_PARSED_SHA: dict = {}


def last_parsed_sha(agent_file: str) -> str | None:
    """sha256 of the bytes the last load of `agent_file` parsed, if it was this process."""
    return _LAST_PARSED_SHA.get(str(agent_file))

# Every frontmatter key summon itself reads. Used ONLY for near-miss typo detection in
# parse_frontmatter -- an unrecognized key that is not a near-miss is still accepted and
# ignored, so an agent file can carry its own metadata.
KNOWN_FRONTMATTER_KEYS = ("run-agent", "permission", "model", "args", "effort",
                          "provider", "base_url", "api_key_env", "capability", "billing")


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body_without_frontmatter). Only handles
    flat key: value lines — no nested structures.
    """
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, content, re.DOTALL)

    if not match:
        return {}, content

    frontmatter_raw = match.group(1)
    body = match.group(2)

    frontmatter = {}
    for line in frontmatter_raw.split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, value = line.split(":", 1)
            key = key.strip()
            # A DUPLICATE key silently last-wins, which can ESCALATE privilege
            # (`permission: read-only` then `permission: yolo` ran as yolo).
            # Ambiguous frontmatter is rejected rather than guessed.
            if key in frontmatter:
                raise ValueError(f"duplicate frontmatter key {key!r}: remove one of them")
            # A key summon does not read is silently IGNORED, which is fine for extra
            # metadata but dangerous for a TYPO: `permisson: read-only` left permission at
            # the stronger safe-edit default, the same silent-escalation shape as the BOM and
            # duplicate-key bugs. Unknown keys stay allowed (agent files are the user's), but
            # one that is a near-miss of a key we DO read is almost certainly a typo, and a
            # typo in this file must not be resolved by defaulting.
            if key not in KNOWN_FRONTMATTER_KEYS:
                # Case-NORMALIZED: summon's keys are lowercase, and difflib is case
                # sensitive, so `PERMISSION: read-only` scored no match at all and was
                # silently ignored -- landing on the stronger safe-edit default, the very
                # escalation this check exists to stop. Best-effort by nature: a near-miss
                # is a heuristic, so an unusual-but-intended key can be caught (rename it)
                # and a wild typo can still slip through.
                near = difflib.get_close_matches(key.lower(), KNOWN_FRONTMATTER_KEYS,
                                                 n=1, cutoff=0.8)
                if near:
                    raise ValueError(
                        f"unknown frontmatter key {key!r}: did you mean {near[0]!r}? "
                        "(rename it, or pick a name that is not a near-miss of a summon key)")
            frontmatter[key] = _unquote(value.strip())

    return frontmatter, body


def _unquote(value: str) -> str:
    """Strip only a MATCHED pair of surrounding quotes. The old blanket
    ``strip("\\"'")`` chewed characters off any value that merely ended in a
    quote — e.g. ``args: --label "two words"`` lost its trailing ``"`` and became
    an unterminated string that then failed ``shlex.split``."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def extract_description(body: str) -> str:
    """First non-heading line of the body, capped at 240 chars with an explicit
    ellipsis — on a word boundary when the line has spaces; a single unbroken
    240+ char token is cut hard at 240 (nothing better exists for it)."""
    for line in body.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            if len(line) <= 240:
                return line
            cut = line[:240].rsplit(" ", 1)[0] or line[:240]
            return cut + " ..."
    return ""


def validate_agent_name(agent_name: str) -> str:
    """Validate agent name to prevent path traversal. Raises ValueError on bad input."""
    if not agent_name or not _AGENT_NAME_PATTERN.match(agent_name):
        raise ValueError(f"Invalid agent name: {agent_name!r}")
    return agent_name


def validate_permission(value: str | None) -> str:
    """Validate permission frontmatter value. None/empty → DEFAULT_PERMISSION."""
    if value is None or value == "":
        return DEFAULT_PERMISSION
    if value not in PERMISSION_VALUES:
        raise ValueError(
            f"Invalid permission: {value!r}. Must be one of: {list(PERMISSION_VALUES)}"
        )
    return value


def _literal_backslashes(value: str) -> str:
    r"""Windows: make a backslash in `args:` a literal path separator, not a POSIX escape.

    shlex's POSIX rules ATE the separators -- `--config C:\temp\foo` split to `C:tempfoo`,
    silently pointing a backend at the wrong path. `posix=False` keeps backslashes but also
    stops stripping the INNER quotes that codex args depend on (`-c key="high"` must yield
    `key=high`), so instead we pre-double every backslash POSIX would have consumed and leave
    the rest of POSIX quoting intact. Rules: inside single quotes a backslash is already
    literal (untouched); a backslash before a quote character is a genuine quote escape (kept);
    everything else doubles -- including `\\`, so a UNC path `\\server\share` survives.
    """
    out, i, in_single, in_double = [], 0, False, False
    while i < len(value):
        ch = value[i]
        if ch == "\\" and not in_single:
            # Consume the WHOLE run of backslashes, because whether the next quote is a
            # delimiter depends on the run's PARITY (the CommandLineToArgvW rule Windows
            # users write to): 2n backslashes before a quote are n literal separators and
            # the quote still delimits, 2n+1 are n separators plus a literal quote. Handling
            # one backslash at a time made `--dir "C:\temp\\"` (the conventional quoted path
            # ending in a separator) an unterminated quotation instead of `C:\temp\`.
            j = i
            while j < len(value) and value[j] == "\\":
                j += 1
            run, nxt = j - i, (value[j] if j < len(value) else "")
            # Only a quote that DELIMITS here can be escaped. Inside double quotes an
            # apostrophe is ordinary text, so applying parity to it halved a legitimate
            # run of separators (`--path "C:\dir\\\\\'draft"` lost one).
            if nxt in (('"',) if in_double else ('"', "'")):
                out.append("\\\\" * (run // 2))    # the literal separators
                if run % 2:
                    out.append("\\" + nxt)        # odd one out: a genuine quote escape
                    i = j + 1
                    continue
                i = j                           # even: fall through so the quote delimits
                continue
            out.append("\\\\" * run)              # plain separators (incl. a UNC prefix)
            i = j
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        out.append(ch)
        i += 1
    return "".join(out)


def parse_extra_args(value: str | None) -> list:
    """`args:` frontmatter — arbitrary backend CLI flags, shlex-split so quoted
    values survive (e.g. args: -c model_reasoning_effort="high"). Returns []
    for missing/empty. Raises ValueError on unbalanced quotes."""
    if not value:
        return []
    import shlex
    try:
        # Windows only: POSIX hosts genuinely use backslash as a shell escape, so their
        # splitting stays byte-identical.
        return shlex.split(_literal_backslashes(value) if os.name == "nt" else value)
    except ValueError as e:
        hint = ""
        # POSIX splitting treats an apostrophe as a quote, so an ordinary Windows path
        # like C:\Users\O'Brien\config fails with a bare "No closing quotation".
        # Single-quote GROUPING is kept (long-standing behaviour some rosters rely on),
        # so the fix is to make the error say what to do instead of guessing intent.
        if "'" in str(value) and "quotation" in str(e):
            hint = (" -- an apostrophe is read as an opening quote here, so a Windows\n"
                    " path containing one must be double-quoted, e.g. "
                    'args: --path "C:\\Users\\O\'Brien\\config"')
        raise ValueError(f"invalid args: frontmatter ({e}): {value!r}{hint}") from e


def bundled_roster_dir() -> str | None:
    """The starter roster shipped INSIDE the skill (``<skill>/agents``), resolved
    relative to this module (``<skill>/scripts/_loader.py`` -> ``../agents``).

    Present in an installed skill (``npx skills add`` / ``install.py`` copy the
    ``agents/`` dir alongside ``scripts/``); absent in a bare dev checkout, where
    it is simply a no-op. Used ONLY as a READ-ONLY lookup fallback so a fresh
    install with no project ``.agents/`` can still dispatch the bundled agents.
    Never a write target — ``--new-agent`` writes to the project roster returned
    by :func:`get_agents_dir`, not here."""
    d = Path(__file__).resolve().parent.parent / "agents"
    return str(d) if d.is_dir() else None


def _load_agent_snapshot_from(agents_dir: str, agent_name: str):
    """Load ``agent_name`` from ONE directory as a SNAPSHOT: ``(tuple, frontmatter, sha)``
    all derived from the SAME immutable byte buffer, or ``(None, None, None)`` if no
    ``.md``/``.txt`` file is there. Raises ValueError on an escaping name.

    Returning the tuple, the frontmatter AND the hash from one read is the whole point: a
    caller that re-reads the file for the hash or the frontmatter is checking a DIFFERENT
    read, so a definition edited A -> B -> A between those reads produced a HYBRID (one
    field from A, another from B). One buffer leaves no window."""
    agents_path = Path(agents_dir)
    agents_root = agents_path.resolve()

    for ext in [".md", ".txt"]:
        agent_file = agents_path / f"{agent_name}{ext}"
        # Defense in depth: ensure resolved path stays inside agents_dir.
        # is_relative_to (not str.startswith) so '/tmp/agents' does not match
        # '/tmp/agents-evil/...'.
        resolved = agent_file.resolve()
        if not resolved.is_relative_to(agents_root):
            raise ValueError(f"Invalid agent name: {agent_name!r}")
        if resolved.exists():
            # utf-8-SIG: an editor-added BOM must not hide the frontmatter. With plain utf-8
            # the leading BOM made the `---` fence unrecognizable, so run-agent/permission
            # silently fell back to defaults, loading the WRONG backend at a STRONGER
            # permission than the file declared. Undecodable bytes now raise a clean
            # ValueError (the CLI emits its JSON error envelope) instead of a traceback.
            try:
                # Read the RAW BYTES once; the hash, the decode and thus the frontmatter all
                # come from THIS buffer. A caller that re-hashes the path afterwards is
                # checking a different read, so a file changed and changed back (A -> B -> A)
                # around the parse passed the check while B's instructions had actually been
                # loaded. Hashing what was parsed leaves no window at all.
                raw = resolved.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                _LAST_PARSED_SHA[str(resolved)] = sha
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as e:
                raise ValueError(f"agent file is not valid UTF-8: {resolved} ({e})") from e
            except OSError as e:
                raise ValueError(f"cannot read agent file {resolved}: {e}") from e
            frontmatter, body = parse_frontmatter(content)
            run_agent = frontmatter.get("run-agent")
            permission = validate_permission(frontmatter.get("permission"))
            description = extract_description(body)
            tup = (run_agent, body.strip(), description, str(resolved), permission,
                   frontmatter.get("model") or None,
                   parse_extra_args(frontmatter.get("args")),
                   frontmatter.get("effort") or None)
            return tup, frontmatter, sha
    return None, None, None


def _load_agent_from(agents_dir: str, agent_name: str):
    """Back-compat: the result tuple only (no frontmatter/sha), or None if absent."""
    return _load_agent_snapshot_from(agents_dir, agent_name)[0]


def load_agent_snapshot(agents_dir: str, agent_name: str):
    """Load a definition ONCE and return ``(tuple, frontmatter, sha)`` from one byte buffer.

    Same lookup as :func:`load_agent` (project dir, then the bundled roster). Callers that
    need the hash and the frontmatter alongside the tuple use this so every
    definition-derived value provably comes from the SAME read -- no A -> B -> A hybrid.
    """
    validate_agent_name(agent_name)

    tup, fm, sha = _load_agent_snapshot_from(agents_dir, agent_name)
    if tup is not None:
        return tup, fm, sha

    bundled = bundled_roster_dir()
    if bundled and Path(bundled).resolve() != Path(agents_dir).resolve():
        tup, fm, sha = _load_agent_snapshot_from(bundled, agent_name)
        if tup is not None:
            return tup, fm, sha

    raise FileNotFoundError(f"Agent definition not found: {agent_name}")


def load_agent(agents_dir: str, agent_name: str) -> tuple[str | None, str, str, str, str, str | None, list, str | None]:
    """Load agent definition file and extract run-agent and permission settings.

    Looks in ``agents_dir`` first; if not found there, falls back to the starter
    roster bundled inside the skill (read-only), so a fresh install works before
    the user has set up a project ``.agents/`` roster.

    Returns (run_agent_cli, system_context, description, file_path, permission,
    model, extra_args, effort).
    """
    return load_agent_snapshot(agents_dir, agent_name)[0]


def _list_agents_in(agents_dir: str) -> list[dict]:
    """List agents in ONE directory (unsorted). ``.md`` wins over ``.txt``."""
    agents_path = Path(agents_dir)
    agents: list[dict] = []
    seen_names: set[str] = set()

    if not agents_path.exists():
        return agents

    for ext in [".md", ".txt"]:
        for agent_file in agents_path.glob(f"*{ext}"):
            name = agent_file.stem
            # Prefer .md over .txt — first ext wins.
            if name in seen_names:
                continue
            seen_names.add(name)

            try:
                content = agent_file.read_text(encoding="utf-8-sig")
                fm, body = parse_frontmatter(content)
                description = extract_description(body)
                agents.append({"name": name, "description": description,
                               "run_agent": (fm or {}).get("run-agent"),
                               "permission": (fm or {}).get("permission")})
            except (OSError, UnicodeDecodeError, ValueError):
                # Unreadable / binary / malformed-frontmatter file: still list it so the
                # caller sees it exists. ValueError matters since duplicate frontmatter keys
                # became a hard error -- ONE bad agent file must not crash the whole roster
                # (--list is the discovery path; a crash there hides every other agent).
                agents.append({"name": name, "description": ""})

    return agents


def list_agents(agents_dir: str) -> list[dict]:
    """List all available agents, sorted by name.

    Returns {"name", "description"} for every agent in ``agents_dir``, plus any
    from the skill's bundled starter roster that aren't already present (the
    project dir wins on a name collision). Files that fail to parse are still
    listed with an empty description.
    """
    agents = _list_agents_in(agents_dir)
    for a in agents:
        a.setdefault("source", "project")
    seen = {a["name"] for a in agents}

    bundled = bundled_roster_dir()
    if bundled and Path(bundled).resolve() != Path(agents_dir).resolve():
        for a in _list_agents_in(bundled):
            if a["name"] not in seen:
                seen.add(a["name"])
                # WHICH roster served each name, so a caller can tell a project definition
                # from a bundled fallback without dispatching it (field report, 2026-07-28).
                a["source"] = "bundled"
                agents.append(a)

    return sorted(agents, key=lambda a: a["name"])


def explicit_dir_fallback_warning(agents_dir_arg, agent_file) -> str | None:
    """Warn when the caller NAMED a roster directory and got a bundled definition anyway.

    The bundled fallback is right when no directory was stated. It is an intent violation
    when one was: a governance control was written mandating `--agents-dir` in the belief
    that it guaranteed roster provenance, and it does not -- resolution falls through to the
    bundled roster silently, and only the receipt's `agent_def.source` reveals it (field
    report, 2026-07-28). Saying so at dispatch is the difference between a control that
    works and one that only looks like it does.
    """
    if not agents_dir_arg or not agent_file:
        return None
    bundled = bundled_roster_dir()
    if not bundled:
        return None
    try:
        served = Path(agent_file).resolve().parent
        if served != Path(bundled).resolve():
            return None
        if served == Path(agents_dir_arg).resolve():
            return None          # they explicitly pointed AT the bundled roster
    except OSError:
        return None
    return ("--agents-dir named %r, but this definition was served from the BUNDLED roster "
            "(%s) because the name was not found there. `--agents-dir` selects which "
            "directory is SEARCHED; it does not guarantee provenance -- check "
            "`agent_def.source` for that." % (str(agents_dir_arg), bundled))


def get_agents_dir(args_agents_dir: str | None, args_cwd: str | None) -> str:
    """Determine agents directory.

    Priority: --agents-dir > $SUB_AGENTS_DIR > {cwd}/.agents/
    """
    if args_agents_dir:
        return args_agents_dir

    env_dir = os.environ.get("SUB_AGENTS_DIR")
    if env_dir:
        return env_dir

    if args_cwd:
        return str(Path(args_cwd) / ".agents")

    # Fallback for --list without --cwd
    return str(Path.cwd() / ".agents")


def discover_agent_packs(plugin_root: str | None = None) -> list[dict]:
    """Fail-soft discovery of optional ``com.summon.agents`` roster packs.

    When ``plugin_root`` is set, only that root's ``com.summon.agents/`` is
    scanned. When omitted, checks ``$PLUGIN_ROOT`` (if set) and
    ``~/.agents/packs/*/com.summon.agents``. Missing paths and bad
    ``index.json`` are skipped — never raises for empty/absent packs.
    """
    roots: list[Path] = []
    if plugin_root is not None:
        roots.append(Path(plugin_root) / "com.summon.agents")
    else:
        env_root = os.environ.get("PLUGIN_ROOT")
        if env_root:
            roots.append(Path(env_root) / "com.summon.agents")
        packs_home = Path.home() / ".agents" / "packs"
        try:
            if packs_home.is_dir():
                for child in sorted(packs_home.iterdir()):
                    roots.append(child / "com.summon.agents")
        except OSError:
            pass

    found: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            key = str(root.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        agents: list[str] = []
        index_meta: dict = {}
        index_path = root / "index.json"
        try:
            if index_path.is_file():
                import json as _json
                raw = _json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    index_meta = {k: raw[k] for k in ("name", "description") if k in raw}
                    listed = raw.get("agents")
                    if isinstance(listed, list):
                        for item in listed:
                            if isinstance(item, str) and item.endswith(".md"):
                                p = root / item
                                if p.is_file() and p.name.lower() != "readme.md":
                                    agents.append(str(p))
        except (OSError, ValueError, TypeError):
            index_meta = {}
            agents = []
        if not agents:
            try:
                agents = sorted(
                    str(p) for p in root.glob("*.md")
                    if p.is_file() and p.name.lower() != "readme.md"
                )
            except OSError:
                agents = []
        found.append({
            "path": str(root),
            "agents": agents,
            **index_meta,
        })
    return found
