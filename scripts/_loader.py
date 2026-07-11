"""Agent definition loading: frontmatter parsing, validation, file discovery."""

from __future__ import annotations

import os
import re
from pathlib import Path

PERMISSION_VALUES = ("read-only", "safe-edit", "yolo")
DEFAULT_PERMISSION = "safe-edit"

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


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
            frontmatter[key.strip()] = _unquote(value.strip())

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


def parse_extra_args(value: str | None) -> list:
    """`args:` frontmatter — arbitrary backend CLI flags, shlex-split so quoted
    values survive (e.g. args: -c model_reasoning_effort="high"). Returns []
    for missing/empty. Raises ValueError on unbalanced quotes."""
    if not value:
        return []
    import shlex
    try:
        return shlex.split(value)
    except ValueError as e:
        raise ValueError(f"invalid args: frontmatter ({e}): {value!r}") from e


def load_agent(agents_dir: str, agent_name: str) -> tuple[str | None, str, str, str, str, str | None, list]:
    """Load agent definition file and extract run-agent and permission settings.

    Returns (run_agent_cli, system_context, description, file_path, permission,
    model, extra_args).
    """
    validate_agent_name(agent_name)
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
            content = resolved.read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(content)
            run_agent = frontmatter.get("run-agent")
            permission = validate_permission(frontmatter.get("permission"))
            description = extract_description(body)
            return (run_agent, body.strip(), description, str(resolved), permission,
                    frontmatter.get("model") or None, parse_extra_args(frontmatter.get("args")))

    raise FileNotFoundError(f"Agent definition not found: {agent_name}")


def list_agents(agents_dir: str) -> list[dict]:
    """List all available agents in the directory.

    Returns list of {"name": str, "description": str}, sorted by name.
    Files that fail to parse are still listed with an empty description.
    """
    agents_path = Path(agents_dir)
    agents = []
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
                content = agent_file.read_text(encoding="utf-8")
                _, body = parse_frontmatter(content)
                description = extract_description(body)
                agents.append({"name": name, "description": description})
            except (OSError, UnicodeDecodeError):
                # Unreadable / binary file: still list it so caller sees it exists.
                agents.append({"name": name, "description": ""})

    return sorted(agents, key=lambda a: a["name"])


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
