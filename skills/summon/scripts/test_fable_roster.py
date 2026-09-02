"""Provider-free successor routing and decision-lead contract checks."""

from pathlib import Path

import pytest

import _builder
import _council
import _executor
from _loader import parse_frontmatter


AGENTS = Path(__file__).resolve().parents[1] / "agents"


@pytest.mark.parametrize("seat", ["fable", "fable-api"])
def test_fable_decision_leads_are_pinned_and_exact(seat):
    metadata, body = parse_frontmatter((AGENTS / f"{seat}.md").read_text(encoding="utf-8"))
    assert metadata["model"] == "claude-fable-5-1"
    assert metadata["model-policy"] == "exact"
    assert "VERDICT: APPROVE | CONCERNS | BLOCK" in body
    assert "HANDOFF:" in body and "LEFT_BEHIND:" in body
    if seat == "fable":
        assert metadata["permission"] == "read-only"
        assert metadata["effort"] == "high"
    else:
        assert metadata["capability"] == "text-only"
        assert metadata["lifecycle"] == "retired"
        assert metadata["successor"] == "fable"
        assert _builder.model_backend_compatibility("openai-compat", metadata["model"])


def test_revision_verdict_is_machine_readable_and_default_chair_stays_balanced():
    assert _executor._review_verdict({"verdict": "CONCERNS"}) == "conditional"
    assert _executor._review_verdict({"verdict": "APPROVE"}) == "pass"
    assert _executor._review_verdict({"verdict": "BLOCK"}) == "block"
    assert _council.DEFAULT_CHAIRMAN == "architect"


@pytest.mark.parametrize("model", ["claude-fable-5", "claude-fable-5-1"])
def test_fable_billing_has_no_silent_substitution_or_assumed_subscription(monkeypatch, model):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for cli in ("claude", "cursor-agent"):
        assert _builder.resolve_billing_model(model, cli) == (model, None)
        assert _builder.premium_model_warning(model, cli)
    assert _builder.infer_dispatch_billing("claude", model)["source"] == "unknown"
    assert _builder.selects_plan_dependent_billing("opus", [f"--model={model}"])
    assert not _builder.selects_plan_dependent_billing(model, ["--model=claude-opus-5"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    assert _builder.infer_dispatch_billing("claude", model)["source"] == "api"
