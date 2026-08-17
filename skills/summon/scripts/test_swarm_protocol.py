from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _swarm_protocol import (PROTOCOL, SwarmProtocolError, encode_frame,
                             frame_sha256, make_frame, parse_frame)


class SwarmProtocolTests(unittest.TestCase):
    def _hello(self):
        return make_frame("hello", run_id="swarm-1", message_id="m-1", sent_at_ms=1,
                          payload={"adapter_id": "ide-bridge", "host_instance_nonce": "host-1",
                                   "versions": [PROTOCOL], "capabilities": ["cancel", "message"],
                                   "max_message_bytes": 12000, "max_artifact_bytes": 1024,
                                   "cancellation_supported": True, "permission_ceiling": "read-only"})

    def test_round_trip_is_canonical_and_digest_stable(self):
        frame = self._hello()
        encoded = encode_frame(frame)
        self.assertEqual(parse_frame(encoded), frame)
        self.assertEqual(frame_sha256(frame), frame_sha256(parse_frame(encoded)))

    def test_worker_registration_binds_project_and_roster(self):
        frame = make_frame("register_worker", run_id="swarm-1", message_id="m-2", sent_at_ms=2,
                           payload={"worker_id": "worker-a", "worker_instance_id": "instance-1",
                                    "project_root_sha256": "a" * 64,
                                    "roster_definition_sha256": "b" * 64,
                                    "capabilities": ["message"], "permission_ceiling": "read-only"})
        self.assertEqual(parse_frame(encode_frame(frame))["payload"]["worker_id"], "worker-a")

    def test_claim_requires_lease_and_request_digest(self):
        with self.assertRaises(SwarmProtocolError):
            make_frame("claim", run_id="swarm-1", message_id="m-3", sent_at_ms=3,
                       payload={"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                                "lease_generation": 1, "lease_expires_at_ms": 99})

    def test_message_has_explicit_recipient_and_digest(self):
        frame = make_frame("send_message", run_id="swarm-1", message_id="m-4", sent_at_ms=4,
                           payload={"from": "worker-a", "to": "coordinator",
                                    "content_sha256": "c" * 64, "content_chars": 7,
                                    "preview": "finished", "reply_to": "m-1"})
        self.assertEqual(parse_frame(encode_frame(frame))["type"], "send_message")

    def test_artifact_paths_cannot_escape(self):
        with self.assertRaises(SwarmProtocolError):
            make_frame("publish_artifact", run_id="swarm-1", message_id="m-5", sent_at_ms=5,
                       payload={"artifact_id": "artifact-a", "claim_id": "claim-a",
                                "task_id": "task-a", "attempt": 1,
                                "lease_generation": 1, "request_sha256": "a" * 64,
                                "sha256": "d" * 64, "bytes": 1, "media_type": "text/plain",
                                "relative_path": "../secret.txt"})
        for unsafe in (r"foo\..\secret.txt", r"C:\secret.txt", "foo/..\\secret.txt"):
            with self.assertRaises(SwarmProtocolError):
                make_frame("publish_artifact", run_id="swarm-1", message_id="m-5x",
                           sent_at_ms=5,
                           payload={"artifact_id": "artifact-a", "claim_id": "claim-a",
                                    "task_id": "task-a", "attempt": 1,
                                    "lease_generation": 1, "request_sha256": "a" * 64,
                                    "sha256": "d" * 64, "bytes": 1, "media_type": "text/plain",
                                    "relative_path": unsafe})
        with self.assertRaises(SwarmProtocolError):
            make_frame("publish_artifact", run_id="swarm-1", message_id="m-5y", sent_at_ms=5,
                       payload={"artifact_id": "artifact-a", "claim_id": "claim-a",
                                "task_id": "task-a", "attempt": 1,
                                "lease_generation": 1, "request_sha256": "a" * 64,
                                "sha256": "d" * 64, "bytes": 1, "media_type": "text/plain",
                                "opaque_uri": 123})

    def test_opaque_artifact_uris_are_allowlisted_metadata_only(self):
        base = {"artifact_id": "artifact-a", "claim_id": "claim-a", "task_id": "task-a",
                "attempt": 1, "lease_generation": 1, "request_sha256": "a" * 64,
                "sha256": "d" * 64, "bytes": 1, "media_type": "text/plain"}
        for scheme in ("artifact://artifact-a", "https://example.test/artifact-a",
                       "ipfs://bafybeigdyr", "s3://bucket/key", "gs://bucket/key"):
            with self.subTest(scheme=scheme):
                frame = make_frame("publish_artifact", run_id="swarm-1",
                                   message_id="m-uri-" + scheme.split(":", 1)[0], sent_at_ms=5,
                                   payload={**base, "opaque_uri": scheme})
                self.assertEqual(parse_frame(encode_frame(frame))["payload"]["opaque_uri"], scheme)
        for uri in ("file:///secret.txt", "javascript:alert(1)", "ftp://example.test/x",
                    "https://user:pass@example.test/x", "https://example.test/x?token=secret",
                    "https://example.test/x#fragment"):
            with self.subTest(uri=uri), self.assertRaises(SwarmProtocolError):
                make_frame("publish_artifact", run_id="swarm-1",
                           message_id="m-bad-uri", sent_at_ms=5,
                           payload={**base, "opaque_uri": uri})

    def test_claim_release_and_completion_are_fenced(self):
        released = make_frame(
            "claim_released", run_id="swarm-1", message_id="m-5r", sent_at_ms=5,
            payload={"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                     "lease_generation": 2, "request_sha256": "a" * 64})
        self.assertEqual(parse_frame(encode_frame(released))["payload"]["lease_generation"], 2)
        with self.assertRaises(SwarmProtocolError):
            make_frame("complete", run_id="swarm-1", message_id="m-5c", sent_at_ms=5,
                       payload={"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                                "lease_generation": 2, "request_sha256": "a" * 64,
                                "envelope_sha256": "b" * 64, "override_authority": True})
        with self.assertRaises(SwarmProtocolError):
            make_frame("complete", run_id="swarm-1", message_id="m-5c2", sent_at_ms=5,
                       payload={"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                                "lease_generation": "wrong", "request_sha256": "wrong",
                                "envelope_sha256": "b" * 64})

    def test_artifact_carries_the_claim_fence(self):
        base = {"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                "lease_generation": 2, "request_sha256": "a" * 64,
                "artifact_id": "artifact-a", "sha256": "d" * 64, "bytes": 1,
                "media_type": "text/plain", "relative_path": "out.txt"}
        frame = make_frame("publish_artifact", run_id="swarm-1", message_id="m-art", 
                           sent_at_ms=5, payload=base)
        self.assertEqual(parse_frame(encode_frame(frame))["payload"]["task_id"], "task-a")
        for key, value in (("lease_generation", "bad"), ("request_sha256", "bad")):
            mutated = dict(base)
            mutated[key] = value
            with self.subTest(key=key), self.assertRaises(SwarmProtocolError):
                make_frame("publish_artifact", run_id="swarm-1", message_id=f"m-{key}",
                           sent_at_ms=5, payload=mutated)

    def test_unknown_top_level_frame_fields_fail_closed(self):
        frame = self._hello()
        frame["future_authority"] = True
        with self.assertRaises(SwarmProtocolError):
            parse_frame(json.dumps(frame))

    def test_unknown_major_protocol_and_unknown_fields_fail_closed(self):
        frame = self._hello()
        frame["protocol"] = "summon.swarm/v2"
        with self.assertRaises(SwarmProtocolError):
            parse_frame(json.dumps(frame))

    def test_state_changing_frames_reject_unversioned_extra_fields(self):
        claim = {"task_id": "task-a", "claim_id": "claim-a", "attempt": 1,
                 "lease_generation": 1, "lease_expires_at_ms": 99,
                 "request_sha256": "a" * 64, "future_state": "mutate"}
        renew = {"claim_id": "claim-a", "lease_generation": 1, "future_state": "mutate"}
        message = {"from": "worker-a", "to": "coordinator", "content_sha256": "c" * 64,
                   "content_chars": 7, "preview": "finished", "future_state": "mutate"}
        cancel = {"claim_id": "claim-a", "lease_generation": 1, "future_state": "mutate"}
        for frame_type, payload in (("claim", claim), ("renew", renew),
                                    ("send_message", message), ("cancel_requested", cancel)):
            with self.subTest(frame_type=frame_type):
                with self.assertRaises(SwarmProtocolError):
                    make_frame(frame_type, run_id="swarm-1", message_id=f"extra-{frame_type}",
                               sent_at_ms=8, payload=payload)
        frame = self._hello()
        frame["payload"]["host_instance_nonce"] = "bad\nnonce"
        with self.assertRaises(SwarmProtocolError):
            parse_frame(json.dumps(frame))

    def test_empty_control_payloads_are_exact(self):
        frame = make_frame("poll", run_id="swarm-1", message_id="m-6", sent_at_ms=6)
        self.assertEqual(parse_frame(encode_frame(frame))["payload"], {})
        with self.assertRaises(SwarmProtocolError):
            make_frame("shutdown", run_id="swarm-1", message_id="m-7", sent_at_ms=7,
                       payload={"reason": "no"})


if __name__ == "__main__":
    unittest.main()
