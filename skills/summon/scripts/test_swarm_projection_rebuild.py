"""F-01..F-09 provider-free journal projection rebuild acceptance tests."""
from __future__ import annotations

import builtins
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _rundir as rundir
import _swarm_coordinator as swarm
from _swarm_coordinator import (SwarmCoordinator, SwarmCoordinatorError,
                                SwarmCorruptError,
                                rebuild_projection_from_journal)


class ProjectionRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-rebuild-")
        self.addCleanup(self.temp.cleanup)
        self.project = "a" * 64
        self.roster = "b" * 64
        self.request = hashlib.sha256(b"task").hexdigest()
        self.coordinator = SwarmCoordinator.create(
            self.temp.name, "run-1", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[{"task_id": "task-1", "request_sha256": self.request}],
            max_attempts=2)
        self.run_dir = Path(self.coordinator.run_dir)

    def journals(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes()
                for path in sorted(self.run_dir.glob("journal-g*.jsonl"))}

    @staticmethod
    def records(raw: bytes) -> list[dict]:
        rows = []
        for line in raw.splitlines():
            value = json.loads(line.decode("utf-8"))
            value.pop("sha256")
            rows.append(value)
        return rows

    @staticmethod
    def encoded(records: list[dict], *, newline: bytes = b"\n",
                final_newline: bool = True) -> bytes:
        lines = []
        for record in records:
            payload = rundir.encode_journal_record(
                {key: value for key, value in record.items() if key != "ts"},
                timestamp=record.get("ts", 2_000_000_000.0))
            lines.append(payload.rstrip(b"\n"))
        result = newline.join(lines)
        return result + (newline if final_newline else b"")

    def test_f01_swarm_only_deterministic_detached_projection(self):
        before = self.journals()
        first = rebuild_projection_from_journal(
            self.run_dir, expected_run_id="run-1",
            expected_project_root_sha256=self.project)
        second = rebuild_projection_from_journal(self.run_dir)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], swarm.PROJECTION_REBUILD_SCHEMA)
        self.assertEqual(first["visibility"], "private_operator")
        self.assertFalse(first["launch_authority"])
        self.assertTrue(first["source"]["complete"])
        self.assertEqual(first["source"]["record_count"], 1)
        self.assertEqual(first["source"]["segments"][0]["bytes"],
                         len(next(iter(before.values()))))
        self.assertEqual(first["source"]["segments"][0]["sha256"],
                         hashlib.sha256(next(iter(before.values()))).hexdigest())
        self.assertEqual(first["source"]["prefix_sha256"],
                         self.coordinator._prefix_digest(before))
        projected = json.dumps(first["projection"], ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"),
                               allow_nan=False).encode("utf-8")
        self.assertEqual(first["projection_sha256"],
                         hashlib.sha256(projected).hexdigest())
        first["projection"]["tasks"].clear()
        self.assertTrue(second["projection"]["tasks"])
        self.assertEqual(before, self.journals())

    def test_f01_non_swarm_family_refuses(self):
        record = {"event": "run_prepared", "generation": 1,
                  "run_id": "run-1", "message_id": "other-family"}
        (self.run_dir / "journal-g1.jsonl").write_bytes(
            rundir.encode_journal_record(record, timestamp=2_000_000_000.0))
        with self.assertRaisesRegex(SwarmCorruptError, "unique leading preparation"):
            rebuild_projection_from_journal(self.run_dir)

    def test_f02_preparation_run_directory_and_expected_identity_match(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        records[0]["run_id"] = "run-2"
        path.write_bytes(self.encoded(records))
        with self.assertRaisesRegex(SwarmCorruptError, "differs from run directory"):
            rebuild_projection_from_journal(self.run_dir)
        with self.assertRaisesRegex(SwarmCorruptError, "expected swarm identity"):
            rebuild_projection_from_journal(self.run_dir, expected_run_id="run-2")

    def test_f02_expected_project_identity_match(self):
        with self.assertRaisesRegex(SwarmCorruptError, "expected project identity"):
            rebuild_projection_from_journal(
                self.run_dir, expected_project_root_sha256="c" * 64)

    def test_f02_unsupported_preparation_schema_refuses(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        records[0]["schema"] = 2
        path.write_bytes(self.encoded(records))
        with self.assertRaisesRegex(SwarmCorruptError, "unsupported.*schema"):
            rebuild_projection_from_journal(self.run_dir)

    def test_f03_strict_read_failure_never_becomes_empty_history(self):
        with mock.patch.object(Path, "read_bytes",
                               side_effect=PermissionError("private path")):
            with self.assertRaisesRegex(SwarmCoordinatorError,
                                        "cannot read swarm journal prefix"):
                rebuild_projection_from_journal(self.run_dir)

    def test_f04_duplicate_journal_message_identity_refuses(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        duplicate = {"event": "journal_repaired", "generation": 1,
                     "repaired_generation": 1,
                     "message_id": records[0]["message_id"]}
        path.write_bytes(self.encoded(records + [duplicate]))
        with self.assertRaisesRegex(SwarmCorruptError, "duplicate identity"):
            rebuild_projection_from_journal(self.run_dir)

    def test_f04_duplicate_claim_creation_refuses(self):
        self.coordinator.register_worker(
            "worker-1", worker_instance_id="instance-1")
        claim = self.coordinator.claim(
            "worker-1", "task-1", request_sha256=self.request)
        path = max(self.run_dir.glob("journal-g*.jsonl"))
        records = self.records(path.read_bytes())
        creation = next(record for record in records
                        if record.get("event") == "claim_granted")
        duplicate = dict(creation, message_id="duplicate-claim-record")
        self.assertEqual(duplicate["claim_id"], claim["claim_id"])
        path.write_bytes(self.encoded(records + [duplicate]))
        with self.assertRaisesRegex(SwarmCorruptError, "duplicate identity"):
            rebuild_projection_from_journal(self.run_dir)

    def test_f05_public_error_does_not_reflect_event_text(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        secret = "PRIVATE_EVENT_" + "x" * 400
        records.append({"event": secret, "generation": 1,
                        "message_id": "unknown-event"})
        path.write_bytes(self.encoded(records))
        with self.assertRaises(SwarmCorruptError) as caught:
            rebuild_projection_from_journal(self.run_dir)
        self.assertEqual(str(caught.exception), "invalid swarm journal event")
        self.assertNotIn("PRIVATE_EVENT", str(caught.exception))

    def test_f06_total_byte_and_record_bounds_refuse(self):
        size = sum(len(value) for value in self.journals().values())
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", size - 1):
            with self.assertRaisesRegex(SwarmCorruptError, "byte bound"):
                rebuild_projection_from_journal(self.run_dir)
        owner = rundir.acquire_owner(str(self.run_dir), 30.0)
        try:
            rundir.journal_append(
                str(self.run_dir),
                {"event": "journal_repaired", "generation": owner.generation,
                 "repaired_generation": 1, "message_id": "second-segment"},
                owner)
        finally:
            rundir.release_owner(owner)
        with mock.patch.object(swarm, "MAX_JOURNAL_RECORDS", 1):
            with self.assertRaisesRegex(SwarmCorruptError, "record bound"):
                rebuild_projection_from_journal(self.run_dir)

    def test_rb01_projection_overflow_is_static_corruption(self):
        with mock.patch.object(swarm, "MAX_PROJECTION_BYTES", 1):
            with self.assertRaisesRegex(
                    SwarmCorruptError,
                    "swarm projection cannot be encoded within the byte bound") as caught:
                rebuild_projection_from_journal(self.run_dir)
        self.assertNotIn("exceeds byte bound", str(caught.exception))

    def test_rb02_generation_requires_exact_integer_type(self):
        path = self.run_dir / "journal-g1.jsonl"
        original = self.records(path.read_bytes())
        for declared in (True, 1.0):
            path.write_bytes(self.encoded(
                [{**original[0], "generation": declared}]))
            with self.assertRaisesRegex(SwarmCorruptError, "generation mismatch"):
                rebuild_projection_from_journal(self.run_dir)
        path.write_bytes(self.encoded(original))

    def test_rb03_rich_multisegment_rebuild_matches_live_state_and_recovery(self):
        clock = lambda: 2_000_000_000.0
        rich = SwarmCoordinator.create(
            self.temp.name, "rich-run", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[
                {"task_id": "task-1", "request_sha256": self.request},
                {"task_id": "task-2", "request_sha256": hashlib.sha256(b"task-2").hexdigest()},
            ], max_attempts=2, clock=clock)
        rich.register_worker("worker-1", worker_instance_id="instance-1")
        rich.register_worker("worker-2", worker_instance_id="instance-2")
        claim_one = rich.claim("worker-1", "task-1", request_sha256=self.request)
        renewed = rich.renew(
            "worker-1", claim_one["claim_id"], claim_one["lease_generation"],
            lease_ms=45_000)
        self.assertEqual(renewed["status"], "renewed")
        posted = rich.send_message("worker-1", "worker-2", "handoff")
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(posted["to"], "worker-2")
        rich.publish_artifact(
            "worker-1", task_id="task-1", claim_id=claim_one["claim_id"],
            attempt=claim_one["attempt"], lease_generation=claim_one["lease_generation"],
            request_sha256=self.request, artifact_id="artifact-1", sha256="d" * 64,
            bytes_count=12, media_type="text/plain", relative_path="out.txt")
        rich.complete(
            "worker-1", task_id="task-1", claim_id=claim_one["claim_id"],
            attempt=claim_one["attempt"], lease_generation=claim_one["lease_generation"],
            request_sha256=self.request, envelope_sha256="e" * 64)

        task_two_request = hashlib.sha256(b"task-2").hexdigest()
        claim_two = rich.claim("worker-2", "task-2", request_sha256=task_two_request,
                               lease_ms=1_000)
        newest = max(Path(rich.run_dir).glob("journal-g*.jsonl"),
                     key=lambda path: int(path.stem.removeprefix("journal-g")))
        with newest.open("ab") as handle:
            handle.write(b'{"torn":')
        blocked = rich.acknowledge_indeterminate(
            "task-2", allow_retry=False, reason="recovery review",
            human_confirmed=True)
        self.assertEqual(blocked["status"], "blocked")

        live_state, torn, raw_map = rich._load_with_snapshot()
        self.assertFalse(torn)
        expected = copy.deepcopy(live_state)
        expected.pop("_prefix_fingerprint", None)
        expected.pop("torn_tail", None)
        rebuilt = rebuild_projection_from_journal(rich.run_dir)
        self.assertEqual(rebuilt["projection"], expected)
        self.assertTrue(expected["tasks"]["task-2"]["uncertain_spend"])
        self.assertEqual(expected["tasks"]["task-2"]["terminal"], "blocked")
        self.assertEqual(len(rebuilt["source"]["segments"]), len(raw_map))
        self.assertGreaterEqual(len(raw_map), 2)
        self.assertEqual(rebuilt["source"]["record_count"], len(rich._strict_snapshot()[0]))
        self.assertEqual(
            rebuilt["source"]["last_generation"],
            max(int(name.removeprefix("journal-g").removesuffix(".jsonl"))
                for name in raw_map))
        self.assertEqual(rebuilt["source"]["prefix_sha256"],
                         rich._prefix_digest(raw_map))
        self.assertTrue(all(
            segment["sha256"] == hashlib.sha256(raw_map[segment["name"]]).hexdigest()
            for segment in rebuilt["source"]["segments"]))

    def test_f07_narrow_crlf_and_unterminated_v1_compatibility(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        path.write_bytes(self.encoded(records, newline=b"\r\n"))
        result = rebuild_projection_from_journal(self.run_dir)
        self.assertEqual(result["compatibility"]["mode"], "legacy_v1_encoding")
        self.assertEqual(result["compatibility"]["features"], ["crlf_segments"])
        self.assertFalse(result["compatibility"]["launch_authority"])
        with self.assertRaisesRegex(SwarmCorruptError, "legacy swarm"):
            rebuild_projection_from_journal(self.run_dir, allow_legacy=False)

        path.write_bytes(self.encoded(records, final_newline=False))
        result = rebuild_projection_from_journal(self.run_dir)
        self.assertEqual(result["compatibility"]["features"],
                         ["unterminated_valid_final_line"])

    def test_f07_cr_only_line_encoding_refuses(self):
        path = self.run_dir / "journal-g1.jsonl"
        records = self.records(path.read_bytes())
        path.write_bytes(self.encoded(records, newline=b"\r"))
        with self.assertRaisesRegex(SwarmCorruptError, "line encoding"):
            rebuild_projection_from_journal(self.run_dir)

    def test_f08_no_write_mode_open_no_mutation_and_no_provider_import(self):
        before = self.journals()
        modes: list[str] = []
        imports: list[str] = []
        original_open = Path.open
        original_import = builtins.__import__

        def observed_open(path, mode="r", *args, **kwargs):
            modes.append(mode)
            return original_open(path, mode, *args, **kwargs)

        def observed_import(name, *args, **kwargs):
            imports.append(name)
            return original_import(name, *args, **kwargs)

        with mock.patch.object(Path, "open", new=observed_open), \
                mock.patch.object(builtins, "__import__", side_effect=observed_import):
            rebuild_projection_from_journal(self.run_dir)
        self.assertTrue(modes)
        self.assertTrue(all("w" not in mode and "a" not in mode and "+" not in mode
                            for mode in modes))
        self.assertFalse(any(name in {"_executor", "_builder", "_conversation_runtime"}
                             for name in imports))
        self.assertEqual(before, self.journals())

    def test_f09_reparse_attribute_refuses_without_platform_skip(self):
        original_stat = Path.stat
        journal = self.run_dir / "journal-g1.jsonl"

        def reparse_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == journal:
                fields = {name: getattr(value, name) for name in dir(value)
                          if name.startswith("st_")}
                fields["st_file_attributes"] = 0x400
                return SimpleNamespace(**fields)
            return value

        with mock.patch.object(Path, "stat", new=reparse_stat):
            with self.assertRaisesRegex(SwarmCoordinatorError,
                                        "managed file may not be a link"):
                rebuild_projection_from_journal(self.run_dir)

    def test_torn_unstable_generation_and_checksum_cases_refuse(self):
        path = self.run_dir / "journal-g1.jsonl"
        original = path.read_bytes()
        path.write_bytes(original + b'{"torn":')
        with self.assertRaisesRegex(SwarmCorruptError, "torn journal"):
            rebuild_projection_from_journal(self.run_dir)

        path.write_bytes(original + b'{"torn":')
        (self.run_dir / "journal-g2.jsonl").write_bytes(
            rundir.encode_journal_record(
                {"event": "journal_repaired", "generation": 2,
                 "repaired_generation": 1, "message_id": "newer-record"},
                timestamp=2_000_000_001.0))
        with self.assertRaisesRegex(SwarmCorruptError, "below the newest"):
            rebuild_projection_from_journal(self.run_dir)
        (self.run_dir / "journal-g2.jsonl").unlink()

        path.write_bytes(original.replace(b'"generation":1', b'"generation":2', 1))
        with self.assertRaises(SwarmCorruptError):
            rebuild_projection_from_journal(self.run_dir)

        records = self.records(original)
        extra = {"event": "journal_repaired", "generation": 1,
                 "repaired_generation": 1, "message_id": "later-valid"}
        corrupt = bytearray(self.encoded(records + [extra]))
        corrupt[0] = ord("[")
        path.write_bytes(bytes(corrupt))
        with self.assertRaisesRegex(SwarmCorruptError, "line 1"):
            rebuild_projection_from_journal(self.run_dir)

        first = original
        records = self.records(original)
        records[0]["max_attempts"] = 3
        changed = self.encoded(records)
        with mock.patch.object(Path, "read_bytes",
                               side_effect=[first, changed]):
            with self.assertRaisesRegex(SwarmCorruptError, "prefix unstable"):
                rebuild_projection_from_journal(self.run_dir)


if __name__ == "__main__":
    unittest.main()
