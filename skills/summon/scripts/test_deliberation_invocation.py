"""Mutation-oriented tests for provider-inert deliberation invocation planning."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_invocation as invplan
import _deliberation_roster as roster
from _deliberation import TurnContext


def _prompt(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class InvocationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "project"
        self.agents = self.cwd / ".agents"
        self.cwd.mkdir()
        self.agents.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_agent(self, *, cli="claude", permission="read-only", model=None,
                  transport=None, args=None, body="Definition body."):
        lines = ["---", f"run-agent: {cli}", f"permission: {permission}"]
        if model:
            lines.append(f"model: {model}")
        if transport:
            lines.append(f"transport: {transport}")
        if args:
            lines.append(f"args: {args}")
        lines += ["---", "# Seat", body, ""]
        (self.agents / "worker.md").write_text("\n".join(lines), encoding="utf-8")

    def proof(self, seat="one"):
        worktree = self.root / f"wt-{seat}"
        worktree.mkdir(exist_ok=True)
        common = self.root / "repo.git"
        git = common / "worktrees" / seat
        git.mkdir(parents=True, exist_ok=True)
        (common / "objects").mkdir(exist_ok=True)
        (common / "refs").mkdir(exist_ok=True)
        (common / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="ascii")
        commit = "0123456789abcdef0123456789abcdef01234567"
        (git / "HEAD").write_text(commit, encoding="ascii")
        (git / "commondir").write_text("../..", encoding="ascii")
        (git / "gitdir").write_text(f"gitdir: {worktree / '.git'}", encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {git}", encoding="utf-8")
        return roster.WorktreeProof(str(worktree), roster._digest_text(commit),
                                    isolated=True, disposable=True)

    def freeze(self, *, permission="read-only", cli="claude", transport=None,
               model=None, args=None, **kwargs):
        self.add_agent(cli=cli, permission=permission, model=model,
                       transport=transport, args=args)
        request = roster.SeatRequest("one", "worker", role="reviewer",
                                     persona="independent")
        return roster.freeze_roster((request,), cwd=str(self.cwd),
                                    agents_dir=str(self.agents), role_enabled=False,
                                    **kwargs)

    def context(self, text="hello", *, seat="one"):
        return TurnContext("decision", seat, "turn-1", 0, _prompt(text))

    def test_read_only_template_binds_exact_prompt_and_hides_agent_file(self):
        frozen = self.freeze(model="claude-sonnet-4-6", args="--extra safe")
        plans = invplan.build_invocation_plans(frozen, decision_id="decision",
                                               cwd=str(self.cwd))
        plan = plans["one"]
        result = plan.for_context(self.context("prompt"), "prompt")
        self.assertEqual(result.prompt, "prompt")
        self.assertIsNone(result.agent_file)
        self.assertIn(invplan.DELIBERATION_SYSTEM_SUFFIX, result.system_context)
        self.assertEqual(result.model, "claude-sonnet-4-6")
        self.assertEqual(result.extra_args, ("--extra", "safe"))
        self.assertTrue(plan.as_dict()["snapshot_digest"])

    def test_deliberation_contract_supersedes_conflicting_seat_report_contract(self):
        self.add_agent(
            body=("Every response MUST end with the exact Final report block. "
                  "Never return machine JSON."))
        frozen = roster.freeze_roster(
            (roster.SeatRequest("one", "worker", role="reviewer"),),
            cwd=str(self.cwd), agents_dir=str(self.agents), role_enabled=False)
        plan = invplan.build_invocation_plans(
            frozen, decision_id="decision", cwd=str(self.cwd))["one"]
        self.assertIn("supersede any generic human-facing report", plan.template.system_context)
        self.assertIn("Do not emit\nthat report block or prose", plan.template.system_context)

    def test_live_ballot_context_excludes_interactive_seat_instructions(self):
        self.add_agent(
            body=("Inspect the repository with tools, enter plan mode, and end "
                  "with a long Final report block."))
        frozen = roster.freeze_roster(
            (roster.SeatRequest("one", "worker", role="reviewer"),),
            cwd=str(self.cwd), agents_dir=str(self.agents), role_enabled=False)
        plan = invplan.build_invocation_plans(
            frozen, decision_id="decision", cwd=str(self.cwd), ballot_only=True)["one"]
        context = plan.template.system_context
        self.assertIn("Return exactly one JSON object", context)
        self.assertIn("Do not call tools", context)
        self.assertNotIn("Inspect the repository with tools", context)
        self.assertNotIn("Final report block", context)

    def test_ballot_only_requires_a_boolean(self):
        frozen = self.freeze()
        with self.assertRaises(invplan.InvocationPlanningError):
            invplan.build_invocation_plans(
                frozen, decision_id="decision", cwd=str(self.cwd), ballot_only="yes")

    def test_wrong_prompt_digest_is_refused_before_provider(self):
        frozen = self.freeze()
        plan = invplan.build_invocation_plans(frozen, decision_id="decision",
                                              cwd=str(self.cwd))["one"]
        with self.assertRaises(invplan.InvocationPlanningError):
            plan.for_context(self.context("expected"), "different")

    def test_cwd_cannot_escape_frozen_roster_scope(self):
        frozen = self.freeze()
        other = self.root / "private-other"
        other.mkdir()
        with self.assertRaises(invplan.InvocationPlanningError):
            invplan.build_invocation_plans(frozen, decision_id="decision",
                                           cwd=str(other))

    def test_stale_roster_is_refused_before_planning_and_turn_binding(self):
        path = self.agents / "worker.md"
        frozen = self.freeze()
        plans_before = invplan.build_invocation_plans(
            frozen, decision_id="decision", cwd=str(self.cwd))
        path.write_text(path.read_text(encoding="utf-8").replace(
            "permission: read-only", "permission: yolo"), encoding="utf-8")
        self.assertFalse(frozen.revalidate())
        with self.assertRaises(invplan.InvocationPlanningError):
            invplan.build_invocation_plans(frozen, decision_id="decision",
                                           cwd=str(self.cwd))
        with self.assertRaises(invplan.InvocationPlanningError):
            plans_before["one"].for_context(self.context("prompt"), "prompt")

    def test_context_seat_and_decision_are_bound(self):
        frozen = self.freeze()
        plan = invplan.build_invocation_plans(frozen, decision_id="decision",
                                              cwd=str(self.cwd))["one"]
        with self.assertRaises(invplan.InvocationPlanningError):
            plan.for_context(TurnContext("other", "one", "turn-1", 0,
                                         _prompt("x")), "x")
        with self.assertRaises(invplan.InvocationPlanningError):
            plan.for_context(TurnContext("decision", "two", "turn-1", 0,
                                         _prompt("x")), "x")

    def test_returned_profile_mapping_is_defensive(self):
        frozen = self.freeze()
        plans = invplan.build_invocation_plans(frozen, decision_id="decision",
                                               cwd=str(self.cwd))
        first = plans["one"].template
        self.assertEqual(first.profile_env, {})
        # The template remains independently copied even when a profile is used.
        profile = self.root / "profile"
        profile.mkdir()
        registry = self.root / "profiles.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}, clear=False):
            frozen = self.freeze()
            # Rebuild with a named profile through the roster's immutable override.
            frozen = roster.freeze_roster(
                (roster.SeatRequest("one", "worker"),), cwd=str(self.cwd),
                agents_dir=str(self.agents), role_enabled=False,
                profile_overrides={"one": "local"})
            plan = invplan.build_invocation_plans(frozen, decision_id="decision",
                                                  cwd=str(self.cwd))["one"]
            a = plan.template
            self.assertIsInstance(a.profile_env, dict)
            a.profile_env["CLAUDE_CONFIG_DIR"] = "MUTATED"
            self.assertNotEqual(plan.template.profile_env["CLAUDE_CONFIG_DIR"], "MUTATED")

    def test_template_mutation_is_detected_before_prompt_binding(self):
        frozen = self.freeze()
        plan = invplan.build_invocation_plans(frozen, decision_id="decision",
                                              cwd=str(self.cwd))["one"]
        plan._template.profile_env["INJECTED"] = "YES"
        with self.assertRaises(invplan.InvocationPlanningError):
            plan.for_context(self.context("prompt"), "prompt")

    def test_writable_seat_requires_exact_frozen_worktree_proof(self):
        proof = self.proof()
        frozen = self.freeze(permission="safe-edit", worktree_proofs={"one": proof})
        plans = invplan.build_invocation_plans(
            frozen, decision_id="decision", cwd=str(self.cwd),
            worktree_proofs={"one": proof})
        self.assertEqual(plans["one"].cwd, proof.path)
        altered = self.proof("other")
        with self.assertRaises(invplan.InvocationPlanningError):
            invplan.build_invocation_plans(
                frozen, decision_id="decision", cwd=str(self.cwd),
                worktree_proofs={"one": altered})

    def test_worktree_drift_is_refused_on_later_turn_binding(self):
        proof = self.proof()
        frozen = self.freeze(permission="safe-edit", worktree_proofs={"one": proof})
        plan = invplan.build_invocation_plans(
            frozen, decision_id="decision", cwd=str(self.cwd),
            worktree_proofs={"one": proof})["one"]
        marker = Path(proof.path) / ".git"
        target = Path(marker.read_text(encoding="utf-8").split(":", 1)[1].strip())
        (target / "HEAD").write_text("f" * 40, encoding="ascii")
        with self.assertRaises(invplan.InvocationPlanningError):
            plan.for_context(self.context("prompt"), "prompt")

    def test_full_bypass_and_agy_need_roster_gates(self):
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(permission="yolo")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(cli="agy", permission="read-only")

    def test_text_and_openai_compat_are_refused_before_any_call(self):
        # The roster currently refuses these even with text consent. This
        # assertion prevents a planner-only bypass from re-enabling them.
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(cli="openai-compat", permission="read-only",
                        text_only_consent=("one",))

    def test_redacted_plan_contains_no_prompt_cwd_or_private_args(self):
        frozen = self.freeze(args="--private-path C:\\Users\\nside\\secret")
        plan = invplan.build_invocation_plans(frozen, decision_id="secret-decision",
                                              cwd=str(self.cwd))["one"]
        raw = json.dumps(plan.as_dict(), sort_keys=True)
        self.assertNotIn("secret-decision", raw)
        self.assertNotIn(str(self.cwd), raw)
        self.assertNotIn("private-path", raw)
        self.assertNotIn("nside", raw)

    def test_planner_has_no_executor_or_subprocess_contact(self):
        self.add_agent()
        with mock.patch.dict(sys.modules, {"_executor": None}), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("contact")):
            frozen = roster.freeze_roster(
                (roster.SeatRequest("one", "worker"),), cwd=str(self.cwd),
                agents_dir=str(self.agents), role_enabled=False)
            plans = invplan.build_invocation_plans(
                frozen, decision_id="decision", cwd=str(self.cwd))
        self.assertEqual(tuple(plans), ("one",))


if __name__ == "__main__":
    unittest.main()
