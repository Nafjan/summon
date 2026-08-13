#!/usr/bin/env python3
"""Focused tests for the receipt-bound one-attempt live integration seam."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_live as live
import _deliberation_roster as roster
import _deliberation_store as store
import _rundir
from _deliberation import DeliberationPolicy
from _deliberation_invocation import build_invocation_plans


class LiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "project"
        self.agents = self.cwd / ".agents"
        self.cwd.mkdir()
        self.agents.mkdir()
        for name, role in (("one", "reviewer"), ("two", "auditor")):
            (self.agents / f"{name}.md").write_text(
                f"---\nrun-agent: claude\npermission: read-only\n---\n# {role}\n",
                encoding="utf-8")
        self.roster = roster.freeze_roster(
            (roster.SeatRequest("one", "one", role="reviewer"),
             roster.SeatRequest("two", "two", role="auditor")),
            cwd=str(self.cwd), agents_dir=str(self.agents), role_enabled=False)
        self.policy = DeliberationPolicy(
            "decision", ("one", "two"), ("yes", "no"), "all", 2)
        self.question = "Should this change ship?"
        self.plans = build_invocation_plans(
            self.roster, decision_id="decision", cwd=str(self.cwd))
        self.timeout_ms = 5000
        self.unix_now_ms = int(time.time() * 1000)
        self.receipt = {
            "mode": "deliberation", "schema_version": 1, "run_id": "live-1",
            "decision_id": "decision", "seat_ids": ["one", "two"],
            "option_ids": ["yes", "no"], "quorum_rule": "all",
            "max_attempts": 2, "require_human_approval": False,
            "question_sha256": hashlib.sha256(self.question.encode()).hexdigest(),
            "roster_digest": self.roster.roster_digest,
            "rounds": 1, "deadline_unix_ms": self.unix_now_ms + 120_000, "created_at": 1.0,
            "provider_integration": {"name": "live-deliberation", "version": 1},
            "execution_contract": {
                "timeout_ms": self.timeout_ms, "retries": False,
                "acp_fallback": False, "gates": False, "report_repair": False,
                "allow_payg": False, "allow_secondary": False,
            },
            "plan_identity_by_seat": live._expected_plan_identity(
                self.roster, self.plans),
            "consent_hashes": {
                "full_authority": self.roster.as_dict()[
                    "full_authority_consent_sha256"],
                "text_only": self.roster.as_dict()["text_only_consent_sha256"],
            },
        }
        self.run_root = self.root / "runs"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, receipt=None, *, lease_sec=600.0):
        path, owner = store.initialize_run(
            str(self.run_root), receipt or self.receipt, lease_sec=lease_sec)
        self.addCleanup(lambda: self._release(owner))
        return path, owner

    @staticmethod
    def _release(owner):
        try:
            import _rundir
            _rundir.release_owner(owner)
        except Exception:
            pass

    def fake_executor(self, calls):
        def run(invocation, **kwargs):
            calls.append(invocation)
            control = kwargs["launch_control"]
            control.before_provider_launch({"backend": invocation.cli,
                                            "transport": invocation.transport})
            return {"result": "not-a-ballot", "exit_code": 0}
        return run

    def test_live_scheduler_is_receipt_bound_and_contacts_fake_once_per_turn(self):
        _path, owner = self.init()
        calls = []
        scheduler = live.build_live_scheduler(
            owner=owner, receipt=self.receipt, policy=self.policy,
            question=self.question, roster=self.roster, timeout_ms=self.timeout_ms,
            clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
            _executor_for_tests=self.fake_executor(calls))
        report = scheduler.run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(report.turns_started, 2)
        self.assertEqual(report.state, "ATTEMPT_BUDGET_EXHAUSTED")

    def test_live_scheduler_default_executor_reaches_one_controlled_fake_subprocess(self):
        path, owner = self.init()
        marker = self.root / "child-marker.txt"
        import _executor

        def fake_build(_invocation, _timeout_ms=None, *, resource_register=None):
            script = (
                "from pathlib import Path; "
                f"Path({str(marker)!r}).open('a', encoding='ascii').write('child\\n'); "
                "print('STATUS: DONE\\nSUMMARY: fake\\nFOLLOW-UP: none\\nHANDOFF: none')"
            )
            return sys.executable, ["-c", script], None

        with mock.patch.object(_executor, "build_invocation_args",
                               side_effect=fake_build):
            scheduler = live.build_live_scheduler(
                owner=owner, receipt=self.receipt, policy=self.policy,
                question=self.question, roster=self.roster, timeout_ms=self.timeout_ms,
                clock=lambda: 0.0, unix_now_ms=self.unix_now_ms)
            report = scheduler.run()
        self.assertEqual(marker.read_text(encoding="ascii").splitlines(),
                         ["child", "child"])
        self.assertEqual(report.turns_started, 2)

    def test_foreign_or_tampered_receipt_is_refused_before_any_scheduler(self):
        _path, owner = self.init()
        tampered = dict(self.receipt, roster_digest="f" * 64)
        with self.assertRaises(live.LiveDeliberationError):
            live.build_live_scheduler(
                owner=owner, receipt=tampered, policy=self.policy,
                question=self.question, roster=self.roster, timeout_ms=self.timeout_ms,
                clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
                _executor_for_tests=self.fake_executor([]))

    def test_question_and_roster_drift_are_refused(self):
        _path, owner = self.init()
        with self.assertRaises(live.LiveDeliberationError):
            live.bind_live_receipt(
                owner=owner, receipt=self.receipt, policy=self.policy,
                question="different", roster=self.roster,
                plans=self.plans, timeout_ms=self.timeout_ms,
                clock_now=0.0, unix_now_ms=self.unix_now_ms)

    def test_receipt_mutation_after_build_refuses_contact(self):
        path, owner = self.init()
        calls = []
        scheduler = live.build_live_scheduler(
            owner=owner, receipt=self.receipt, policy=self.policy,
            question=self.question, roster=self.roster, timeout_ms=self.timeout_ms,
            clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
            _executor_for_tests=self.fake_executor(calls))
        import _rundir
        _rundir.atomic_write_json(os.path.join(path, "receipt.json"),
                                  dict(self.receipt, created_at=2.0))
        report = scheduler.run()
        self.assertEqual(calls, [])
        self.assertEqual(report.error_kind, "adapter_indeterminate")

    def test_owner_loss_is_revalidated_before_scheduler_construction(self):
        _path, owner = self.init()
        import _rundir
        _rundir.release_owner(owner)
        with self.assertRaises(live.LiveDeliberationError):
            live.bind_live_receipt(
                owner=owner, receipt=self.receipt, policy=self.policy,
                question=self.question, roster=self.roster,
                plans=self.plans, timeout_ms=self.timeout_ms,
                clock_now=0.0, unix_now_ms=self.unix_now_ms)

    def build(self, owner, *, receipt=None, question=None, timeout_ms=None,
              cancel_requested=None, executor=None, deadline_clock=None,
              unix_now_ms=None, clock=None):
        kwargs = dict(
            owner=owner, receipt=receipt or self.receipt, policy=self.policy,
            question=self.question if question is None else question,
            roster=self.roster,
            timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
            clock=(lambda: 0.0) if clock is None else clock,
            unix_now_ms=self.unix_now_ms if unix_now_ms is None else unix_now_ms,
            cancel_requested=cancel_requested, deadline_clock=deadline_clock,
        )
        if executor is not None:
            kwargs["_executor_for_tests"] = executor
        return live.build_live_scheduler(**kwargs)

    def test_public_arbitrary_executor_keyword_is_rejected(self):
        _path, owner = self.init()
        with self.assertRaises(TypeError):
            live.build_live_scheduler(
                owner=owner, receipt=self.receipt, policy=self.policy,
                question=self.question, roster=self.roster,
                timeout_ms=self.timeout_ms, clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
                executor=self.fake_executor([]))

    def test_shared_cancel_reaches_active_launch_control(self):
        _path, owner = self.init()
        cancelled = [False]
        contacts = []

        def executor(invocation, **kwargs):
            cancelled[0] = True
            kwargs["launch_control"].before_provider_launch({"backend": invocation.cli})
            contacts.append(True)
            return {"result": "{}", "exit_code": 0}

        report = self.build(
            owner, cancel_requested=lambda: cancelled[0], executor=executor).run()
        self.assertEqual(contacts, [])
        self.assertTrue(report.uncertain_spend)

    def test_scheduler_cancel_event_reaches_child_control_without_construction_race(self):
        _path, owner = self.init()
        contacts = []

        def executor(invocation, **kwargs):
            kwargs["launch_control"].before_provider_launch({"backend": invocation.cli})
            contacts.append(True)
            return {"result": "{}", "exit_code": 0}

        scheduler = self.build(owner, executor=executor)
        scheduler.cancel()
        report = scheduler.run()
        self.assertEqual(contacts, [])
        self.assertEqual(report.state, "CANCELLED")

    def test_deadline_crossing_immediately_before_control_has_zero_contact(self):
        changed = dict(self.receipt, run_id="near-deadline",
                       deadline_unix_ms=self.unix_now_ms + 20_000)
        _path, owner = self.init(changed)
        now = [0.0]
        contacts = []

        def executor(invocation, **kwargs):
            now[0] = 20.0
            kwargs["launch_control"].before_provider_launch({"backend": invocation.cli})
            contacts.append(True)
            return {"result": "{}", "exit_code": 0}

        scheduler = live.build_live_scheduler(
            owner=owner, receipt=changed, policy=self.policy,
            question=self.question, roster=self.roster,
            timeout_ms=self.timeout_ms, clock=lambda: now[0], unix_now_ms=self.unix_now_ms,
            _executor_for_tests=executor)
        report = scheduler.run()
        self.assertEqual(contacts, [])
        self.assertTrue(report.uncertain_spend)

    def test_wall_deadline_fence_ignores_lingering_injected_clock(self):
        deadline_ms = self.unix_now_ms + 15_000
        changed = dict(self.receipt, run_id="wall-deadline",
                       deadline_unix_ms=deadline_ms)
        _path, owner = self.init(changed)
        contacts = []

        def executor(invocation, **kwargs):
            # The scheduler clock is intentionally stuck before its deadline;
            # the launch-control wall fence must still refuse contact.
            with mock.patch.object(live.time, "time",
                                   return_value=deadline_ms / 1000 + 1):
                kwargs["launch_control"].before_provider_launch(
                    {"backend": invocation.cli})
            contacts.append(True)
            return {"result": "{}", "exit_code": 0}

        report = self.build(owner, receipt=changed, executor=executor).run()
        self.assertEqual(contacts, [])
        self.assertTrue(report.uncertain_spend)

    def test_wall_deadline_fence_wins_at_result_boundary(self):
        deadline_ms = self.unix_now_ms + 15_000
        changed = dict(self.receipt, run_id="wall-result-deadline",
                       deadline_unix_ms=deadline_ms)
        _path, owner = self.init(changed)
        contacts = []
        monotonic = [0.0]

        def executor(invocation, **kwargs):
            contacts.append(True)
            kwargs["launch_control"].before_provider_launch(
                {"backend": invocation.cli})
            monotonic[0] = 1_000.0
            return {"result": json.dumps({
                "ballot": {"decision": "vote", "option_id": "yes"}
            }), "exit_code": 0}

        report = self.build(owner, receipt=changed, executor=executor,
                            clock=lambda: monotonic[0]).run()
        self.assertEqual(len(contacts), 1)
        self.assertNotEqual(report.state, "DECIDED")
        self.assertEqual(report.decision_option, None)

    def test_takeover_owner_can_use_original_prepared_receipt(self):
        _path, first = self.init()
        _rundir.release_owner(first)
        second = _rundir.acquire_owner(first.run_dir, 600)
        self.addCleanup(lambda: self._release(second))
        calls = []
        scheduler = self.build(second, executor=self.fake_executor(calls))
        scheduler.run()
        self.assertEqual(len(calls), 2)

    def test_provider_contract_plan_consent_and_timeout_mutations_refuse_contact(self):
        _path, owner = self.init()
        mutations = []
        mutations.append(dict(self.receipt, provider_integration={"name": "evil", "version": 1}))
        mutations.append(dict(self.receipt, provider_integration={
            "name": "live-deliberation", "version": 1, "extra": True}))
        bad_exec = dict(self.receipt["execution_contract"], retries=True)
        mutations.append(dict(self.receipt, execution_contract=bad_exec))
        unknown_exec = dict(self.receipt["execution_contract"], unknown=False)
        mutations.append(dict(self.receipt, execution_contract=unknown_exec))
        bad_plan = {key: dict(value) for key, value in
                    self.receipt["plan_identity_by_seat"].items()}
        bad_plan["one"]["snapshot_digest"] = "f" * 64
        mutations.append(dict(self.receipt, plan_identity_by_seat=bad_plan))
        unknown_plan = {key: dict(value) for key, value in
                        self.receipt["plan_identity_by_seat"].items()}
        unknown_plan["one"]["unknown"] = "f" * 64
        mutations.append(dict(self.receipt, plan_identity_by_seat=unknown_plan))
        missing_plan = dict(self.receipt["plan_identity_by_seat"])
        missing_plan.pop("two")
        mutations.append(dict(self.receipt, plan_identity_by_seat=missing_plan))
        bad_consent = dict(self.receipt["consent_hashes"], full_authority=["f" * 64])
        mutations.append(dict(self.receipt, consent_hashes=bad_consent))
        mutations.append(dict(self.receipt, consent_hashes={
            **self.receipt["consent_hashes"], "unknown": []}))
        for changed in mutations:
            with self.subTest(fields=set(changed)):
                with self.assertRaises(live.LiveDeliberationError):
                    live._validate_provider_contract(
                        changed, roster=self.roster, plans=self.plans,
                        timeout_ms=self.timeout_ms)
        with self.assertRaises(live.LiveDeliberationError):
            self.build(owner, timeout_ms=self.timeout_ms + 1,
                       executor=self.fake_executor([]))

    def test_question_timeout_deadline_and_nonfinite_json_guards(self):
        _path, owner = self.init()
        for question in ("   ", "x" * (live.MAX_LIVE_QUESTION_BYTES + 1)):
            with self.assertRaises(live.LiveDeliberationError):
                self.build(owner, question=question, executor=self.fake_executor([]))
        for timeout in (0, live.MAX_LIVE_TIMEOUT_MS + 1, True):
            with self.assertRaises(live.LiveDeliberationError):
                self.build(owner, timeout_ms=timeout, executor=self.fake_executor([]))
        with self.assertRaises(live.LiveDeliberationError):
            self.build(owner, deadline_clock=10.1, executor=self.fake_executor([]))
        with self.assertRaises(live.LiveDeliberationError):
            self.build(owner, executor=self.fake_executor([]),
                       unix_now_ms=-9_000_000_000_000_000_000)
        bad = dict(self.receipt, created_at=float("nan"))
        _rundir.atomic_write_json(os.path.join(owner.run_dir, "receipt.json"), bad)
        with self.assertRaises(live.LiveDeliberationError):
            self.build(owner, receipt=bad, executor=self.fake_executor([]))

    def test_owner_path_and_journal_errors_are_redacted_live_errors(self):
        _path, owner = self.init()
        wrong = self.root / "wrong-name"
        wrong.mkdir()
        (wrong / "owner.lock").write_bytes(owner.payload)
        (wrong / "receipt.json").write_text(
            __import__("json").dumps(self.receipt), encoding="utf-8")
        forged = _rundir.Owner(str(wrong), owner.nonce, owner.generation,
                               owner.lease_sec, owner.payload)
        with self.assertRaises(live.LiveDeliberationError):
            live._owner_receipt(forged, self.receipt)
        with mock.patch.object(_rundir, "journal_read_tagged",
                               side_effect=OSError("PRIVATE PATH")):
            with self.assertRaises(live.LiveDeliberationError) as caught:
                live._owner_receipt(owner, self.receipt)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_timeout_after_control_boundary_is_uncertain_and_not_retried(self):
        _path, owner = self.init()
        calls = []

        def timeout(invocation, **kwargs):
            kwargs["launch_control"].before_provider_launch({"backend": invocation.cli})
            calls.append(invocation.cli)
            return {"result": "", "exit_code": 124, "timeout": True}

        report = self.build(owner, executor=timeout).run()
        self.assertEqual(calls, ["claude"])
        self.assertTrue(report.uncertain_spend)

    def test_one_owner_can_activate_only_one_scheduler(self):
        _path, owner = self.init()
        first = self.build(owner, executor=self.fake_executor([]))
        self.assertIsNotNone(first)
        with self.assertRaises(live.LiveDeliberationError):
            self.build(owner, executor=self.fake_executor([]))

    def test_nonprepared_journal_cannot_be_reactivated_after_takeover(self):
        _path, first = self.init()
        self.build(first, executor=self.fake_executor([])).run()
        _rundir.release_owner(first)
        second = _rundir.acquire_owner(first.run_dir, 600)
        self.addCleanup(lambda: self._release(second))
        with self.assertRaises(live.LiveDeliberationError):
            self.build(second, executor=self.fake_executor([]))

    def test_timeout_must_fit_current_owner_lease(self):
        changed = dict(self.receipt)
        changed["run_id"] = "short-lease"
        changed["deadline_unix_ms"] = 1_000_000
        changed["execution_contract"] = dict(
            changed["execution_contract"], timeout_ms=5_000)
        _path, owner = self.init(changed, lease_sec=0.1)
        with self.assertRaises(live.LiveDeliberationError):
            live.build_live_scheduler(
                owner=owner, receipt=changed, policy=self.policy,
                question=self.question, roster=self.roster, timeout_ms=5_000,
                clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
                _executor_for_tests=self.fake_executor([]))

    def test_total_one_round_timeout_must_fit_receipt_deadline(self):
        changed = dict(self.receipt, run_id="short-deadline",
                       deadline_unix_ms=self.timeout_ms + 1)
        _path, owner = self.init(changed)
        with self.assertRaises(live.LiveDeliberationError):
            live.build_live_scheduler(
                owner=owner, receipt=changed, policy=self.policy,
                question=self.question, roster=self.roster,
                timeout_ms=self.timeout_ms, clock=lambda: 0.0, unix_now_ms=self.unix_now_ms,
                _executor_for_tests=self.fake_executor([]))

    def test_noncallable_cancel_source_is_refused_at_construction(self):
        _path, owner = self.init()
        with self.assertRaises(TypeError):
            self.build(owner, cancel_requested="not-callable",
                       executor=self.fake_executor([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
