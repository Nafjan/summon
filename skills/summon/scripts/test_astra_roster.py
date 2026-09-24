"""Provider-inert GPT-6 Astra roster and routing checks."""

from dataclasses import replace
from pathlib import Path

import _model_catalog as catalog
from _builder import AgentInvocation, build_invocation_args, model_backend_compatibility
from _executor import model_exact_policy
from _loader import load_agent, parse_frontmatter


AGENTS = Path(__file__).resolve().parents[1] / "agents"


def test_astra_seat_is_exact_read_only_and_high_effort():
    metadata, body = parse_frontmatter((AGENTS / "astra.md").read_text(encoding="utf-8"))
    assert metadata == {
        "run-agent": "codex",
        "model": "gpt-6-astra",
        "model-policy": "exact",
        "effort": "high",
        "permission": "read-only",
    }
    assert "VERDICT: APPROVE | CONCERNS | BLOCK" in body
    assert "HANDOFF:" in body and "LEFT_BEHIND:" in body
    assert model_exact_policy("astra", metadata) == (True, "frontmatter")
    assert model_exact_policy("astra") == (True, "named-seat")


def test_astra_bundled_definition_builds_one_exact_codex_selector():
    definition = load_agent(str(AGENTS), "astra")
    assert definition[0] == "codex"
    assert definition[5] == "gpt-6-astra"
    assert model_backend_compatibility("codex", definition[5]) is None
    invocation = AgentInvocation(
        cli="codex", model=definition[5], prompt="review", cwd=str(AGENTS),
        permission="read-only", effort=definition[7],
    )
    _cmd, args, _env = build_invocation_args(invocation)
    selectors = [value for index, value in enumerate(args)
                 if index > 0 and args[index - 1] in {"-m", "--model"}]
    assert selectors == ["gpt-6-astra"]
    assert "model_reasoning_effort=high" in args

    _cmd, max_args, _env = build_invocation_args(replace(invocation, effort="max"))
    assert "model_reasoning_effort=max" in max_args

    _cmd, xhigh_args, _env = build_invocation_args(replace(invocation, effort="xhigh"))
    assert "model_reasoning_effort=xhigh" in xhigh_args


def test_astra_catalog_identity_is_editorial_not_served_proof():
    display = catalog.display_for("codex", "gpt-6-astra")
    assert display["name"] == "Astra"
    assert display["version"] == "6"
    assert display["label"] == "frontier"
    assert display["lane"] == "architecture-planning-review"
    assert display["served_exact"] is False
