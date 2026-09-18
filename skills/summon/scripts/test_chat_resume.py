"""Provider-inert tests for the typed chat continuation refusal contract."""

from __future__ import annotations

import unittest

from _chat_resume import build, is_stored_capability, is_valid
from _conversation import ConversationError, _public_payload
from _resume_capabilities import resume_capability


class ChatResumeContractTests(unittest.TestCase):
    def setUp(self):
        self.capability = resume_capability("codex", "subprocess")
        self.refusal = build("resume_candidate", self.capability)

    def test_exact_shape_and_types(self):
        self.assertTrue(is_valid(self.refusal))
        self.assertEqual(self.refusal["attempts"], 0)
        self.assertIs(type(self.refusal["attempts"]), int)
        self.assertIs(type(self.refusal["action_offer"]["automatic"]), bool)

    def test_bool_as_integer_and_integer_as_boolean_are_rejected(self):
        attempts = dict(self.refusal, attempts=False)
        self.assertFalse(is_valid(attempts))
        action = dict(self.refusal, action_offer={"kind": "fork", "automatic": 0})
        self.assertFalse(is_valid(action))
        historical = dict(self.refusal, capability=dict(self.capability,
                                                        live_steering_acknowledged=True))
        self.assertFalse(is_valid(historical))

    def test_extra_nested_keys_and_non_mapping_containers_are_rejected(self):
        self.assertFalse(is_valid(dict(self.refusal, extra="no")))
        self.assertFalse(is_valid(dict(self.refusal,
                                      action_offer={"kind": "fork", "automatic": False,
                                                     "extra": "no"})))
        self.assertFalse(is_valid([]))
        self.assertFalse(is_stored_capability([]))
        self.assertFalse(is_stored_capability({"resume_state": []}))

    def test_historical_v1_refusal_is_readable_but_not_current_authority(self):
        # A persisted v1 row may name an adapter that has since disappeared
        # from the current registry.  It remains readable for history, but it
        # must not be accepted as a current launch/refusal capability.
        legacy = dict(self.capability,
                      backend="legacy-cli",
                      resume_state="unsupported",
                      resume_reason="legacy_adapter_removed")
        historical = dict(self.refusal, capability=legacy)
        self.assertTrue(is_stored_capability(legacy))
        self.assertTrue(is_valid(historical, historical=True))
        self.assertFalse(is_valid(historical, historical=False))

        # The current builder is intentionally stricter than the historical
        # reader; callers must not silently relaunch from a legacy row.
        with self.assertRaises(ValueError):
            build("resume_capability_changed", legacy)

    def test_public_finish_projection_accepts_only_bounded_reason(self):
        projected = _public_payload("turn_finished", {
            "turn_id": "turn-1", "participant": "worker", "status": "blocked",
            "refusal_reason": "resume_candidate",
        })
        self.assertEqual(projected["refusal_reason"], "resume_candidate")
        with self.assertRaises(ConversationError):
            _public_payload("turn_finished", {
                "turn_id": "turn-1", "participant": "worker", "status": "blocked",
                "refusal_reason": "not-a-real-code",
            })


if __name__ == "__main__":
    unittest.main()
