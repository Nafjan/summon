#!/usr/bin/env python3
"""Standalone stdlib tests for deliberation policy and ballot validation."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _deliberation import (  # noqa: E402
    AttemptBinding, BallotBook, DeliberationError, DeliberationPolicy,
    LaunchSpec, resolve_quorum, summarize_attempt_events, validate_ballot,
)


def policy(rule: int | str = "2/3", seats: tuple[str, ...] = ("s1", "s2", "s3")):
    return DeliberationPolicy("decision", seats, ("red", "blue", "green"), rule, 6)


def binding(seat: str = "s1", turn: str = "t1", ordinal: int = 0):
    spec = LaunchSpec("scripted", "safe-id", b"private", "0" * 64)
    return AttemptBinding("a1", 7, "decision", seat, turn, ordinal, spec.digest)


def ballot_for(bind: AttemptBinding, **changes):
    value = {"schema_version": 1, "decision_id": bind.decision_id,
             "seat_id": bind.seat_id, "turn_id": bind.turn_id,
             "attempt_id": bind.attempt_id, "decision": "vote",
             "option_id": "green", "confidence": "high", "evidence_refs": []}
    value.update(changes)
    return value


class QuorumPolicyTests(unittest.TestCase):
    def test_fraction_uses_ceil_and_fixed_denominator(self):
        self.assertEqual(resolve_quorum(4, "2/3"), 3)
        self.assertEqual(resolve_quorum(5, "2/3"), 4)
        self.assertEqual(resolve_quorum(4, "all"), 4)

    def test_invalid_resolved_thresholds_fail_before_run(self):
        for value in (0, 4, True, 0.5, "0/3", "4/3", "2 / 3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DeliberationPolicy("decision", ("a", "b", "c"), ("x", "y"), value, 2)

    def test_policy_rejects_duplicate_seats_and_options(self):
        with self.assertRaises(ValueError):
            DeliberationPolicy("decision", ("a", "a"), ("x", "y"), "all", 2)
        with self.assertRaises(ValueError):
            DeliberationPolicy("decision", ("a", "b"), ("x", "x"), "all", 2)


class BallotTests(unittest.TestCase):
    def test_multi_option_vote_is_schedule_bound(self):
        bind = binding()
        result = validate_ballot(ballot_for(bind, option_id="blue"), bind, policy())
        self.assertIsNotNone(result)
        self.assertEqual(result.option_id, "blue")
        for key, wrong in (("seat_id", "s2"), ("turn_id", "later"),
                           ("attempt_id", "a2"), ("decision_id", "other"),
                           ("schema_version", 2), ("option_id", "purple")):
            with self.subTest(key=key):
                self.assertIsNone(validate_ballot(ballot_for(bind, **{key: wrong}), bind, policy()))

    def test_abstain_and_undecided_require_null_option(self):
        bind = binding()
        for decision in ("abstain", "undecided"):
            self.assertIsNotNone(validate_ballot(
                ballot_for(bind, decision=decision, option_id=None), bind, policy()))
            self.assertIsNone(validate_ballot(
                ballot_for(bind, decision=decision, option_id="red"), bind, policy()))

    def test_malformed_fields_are_inert(self):
        bind = binding()
        for changes in ({"confidence": "certain"}, {"decision": "approve"},
                        {"evidence_refs": "not-a-list"}, {"evidence_refs": [3]}):
            self.assertIsNone(validate_ballot(ballot_for(bind, **changes), bind, policy()))

    def test_revision_requires_later_turn_and_denominator_never_shrinks(self):
        pol = policy("2/3")
        book = BallotBook(pol)
        first_bind = binding("s1", "t1", 0)
        first = validate_ballot(ballot_for(first_bind, option_id="red"), first_bind, pol)
        self.assertTrue(book.record(first, 0))
        self.assertFalse(book.record(first, 0))
        later_bind = AttemptBinding("a2", 7, "decision", "s1", "t2", 2,
                                    first_bind.launch_spec_digest)
        later = validate_ballot(ballot_for(later_bind, option_id="blue",
                                           attempt_id="a2", turn_id="t2"), later_bind, pol)
        self.assertTrue(book.record(later, 2))
        self.assertEqual(book.tally(), {"red": 0, "blue": 1, "green": 0})
        self.assertEqual(pol.denominator, 3)
        self.assertEqual(pol.threshold, 2)

    def test_tie_at_low_explicit_threshold_has_no_candidate(self):
        pol = policy(1)
        book = BallotBook(pol)
        for index, option in enumerate(("red", "blue"), 1):
            bind = AttemptBinding(f"a{index}", 7, "decision", f"s{index}", f"t{index}",
                                  index, "0" * 64)
            vote = validate_ballot(ballot_for(bind, attempt_id=f"a{index}",
                                               seat_id=f"s{index}", turn_id=f"t{index}",
                                               option_id=option), bind, pol)
            self.assertTrue(book.record(vote, index))
        self.assertIsNone(book.candidate())


class AttemptProjectionTests(unittest.TestCase):
    def test_unfinished_attempt_is_uncertain_and_report_only(self):
        events = [{"event": "attempt_started", "attempt_id": "a1", "generation": 2,
                   "launch_spec_sha256": hashlib.sha256(b"x").hexdigest()}]
        summary = summarize_attempt_events(events)
        self.assertTrue(summary["uncertain_spend"])
        self.assertEqual(summary["stale_attempts"][0]["action"], "report_only")

    def test_finish_requires_same_generation_and_digest(self):
        events = [{"event": "attempt_started", "attempt_id": "a1", "generation": 2,
                   "launch_spec_sha256": "0" * 64},
                  {"event": "attempt_finished", "attempt_id": "a1", "generation": 3,
                   "launch_spec_sha256": "0" * 64}]
        self.assertTrue(summarize_attempt_events(events)["uncertain_spend"])
        events.append({"event": "attempt_started", "attempt_id": "a1", "generation": 3,
                       "launch_spec_sha256": "1" * 64})
        with self.assertRaises(DeliberationError):
            summarize_attempt_events(events)

if __name__ == "__main__":
    unittest.main()
