"""Provider-free R13 journal encoder and owned-append contract tests."""

from __future__ import annotations

import errno
import builtins
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "summon", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import _rundir as rd  # noqa: E402


class _FailingFile:
    def __init__(self, wrapped, phase: str):
        self._wrapped = wrapped
        self._phase = phase

    def write(self, payload):
        if self._phase == "write":
            raise OSError("injected write failure")
        if self._phase == "disk-full":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        if self._phase == "short":
            return self._wrapped.write(payload[:-1])
        return self._wrapped.write(payload)

    def flush(self):
        if self._phase == "flush":
            raise OSError("injected flush failure")
        if self._phase == "interrupt":
            raise KeyboardInterrupt("injected interrupt after write")
        return self._wrapped.flush()

    def fileno(self):
        return self._wrapped.fileno()

    def close(self):
        result = self._wrapped.close()
        if self._phase == "close-error":
            raise RuntimeError("injected non-OSError close failure")
        return result


class JournalAppendTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = tempfile.mkdtemp(prefix="summon-r13-journal-")
        self.owner = rd.acquire_owner(self.run_dir, lease_sec=600)

    def tearDown(self):
        rd.release_owner(self.owner)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def test_encoder_and_append_use_exact_utf8_lf_bytes(self):
        record = {
            "event": "message_posted",
            "message_id": "m-ümlaut",
            "text": "Unicode ☃ and a logical newline\n stay escaped",
        }
        timestamp = 1700000000.25
        with mock.patch.object(rd.time, "time", side_effect=[timestamp]) as clock:
            rd.journal_append(self.run_dir, record, self.owner)
        clock.assert_called_once_with()
        raw = open(rd._journal_path(self.run_dir, self.owner.generation), "rb").read()
        self.assertEqual(raw, rd.encode_journal_record(record, timestamp=timestamp))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r\n", raw)
        parsed = json.loads(raw[:-1].decode("utf-8"))
        claimed = parsed.pop("sha256")
        canonical = json.dumps(
            parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(claimed, rd.hashlib.sha256(canonical).hexdigest())

    def test_namespaced_artifact_digest_is_preserved_separately_from_journal_checksum(self):
        record = {
            "event": "artifact_published",
            "artifact_id": "artifact-a",
            "artifact_sha256": "d" * 64,
        }
        payload = rd.encode_journal_record(record, timestamp=1.0)
        parsed = json.loads(payload[:-1].decode("utf-8"))
        self.assertEqual(parsed["artifact_sha256"], "d" * 64)
        self.assertIn("sha256", parsed)
        rd.journal_append_encoded(
            self.run_dir, payload, self.owner, expected_record=record
        )
        records, torn = rd.journal_read(self.run_dir)
        self.assertFalse(torn)
        self.assertEqual(records[0]["artifact_sha256"], "d" * 64)
        # Faithful old-reader parser: it knows only the historical ``sha256``
        # line checksum and must still accept the namespaced content digest.
        old_reader = json.loads(payload[:-1].decode("utf-8"))
        claimed = old_reader.pop("sha256")
        old_canonical = json.dumps(
            old_reader, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(claimed, rd.hashlib.sha256(old_canonical).hexdigest())
        self.assertEqual(old_reader["artifact_sha256"], "d" * 64)

    def test_raw_artifact_sha256_is_rejected_at_journal_serializer(self):
        with self.assertRaises(ValueError):
            rd.encode_journal_record(
                {"event": "artifact_published", "sha256": "d" * 64}, timestamp=1.0
            )

    def test_legacy_artifact_checksum_collision_is_not_silently_accepted(self):
        record = {"event": "artifact_published", "sha256": "d" * 64, "ts": 1.0}
        serialized = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        old_digest = rd.hashlib.sha256(serialized).hexdigest()
        # This reproduces the pre-seam collision: the journal checksum
        # overwrote the artifact's own sha256 field.  It must remain a torn or
        # corrupt legacy prefix, never be promoted as a valid artifact.
        old_line = json.dumps(
            {**record, "sha256": old_digest},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        path = rd._journal_path(self.run_dir, self.owner.generation)
        with open(path, "wb") as handle:
            handle.write(old_line)
        records, torn = rd._read_segment(path)
        self.assertEqual(records, [])
        self.assertTrue(torn)

    def test_preencoded_append_does_not_resample_or_restamp(self):
        record = {"event": "pre-admitted", "message_id": "frozen"}
        payload = rd.encode_journal_record(record, timestamp=1700000000.25)
        with mock.patch.object(rd.time, "time", side_effect=AssertionError("resampled")):
            rd.journal_append_encoded(
                self.run_dir, payload, self.owner, expected_record=record
            )
        raw = open(rd._journal_path(self.run_dir, self.owner.generation), "rb").read()
        self.assertEqual(raw, payload)

    def test_preencoded_append_rejects_mismatched_expected_record_before_open(self):
        payload = rd.encode_journal_record(
            {"event": "frozen", "message_id": "one"}, timestamp=1700000000.25
        )
        with self.assertRaises(ValueError):
            rd.journal_append_encoded(
                self.run_dir, payload, self.owner,
                expected_record={"event": "frozen", "message_id": "two"},
            )
        self.assertFalse(os.path.exists(rd._journal_path(self.run_dir, self.owner.generation)))

    def test_preencoded_append_requires_canonical_json_bytes(self):
        line = rd._journal_line({"event": "canonical", "ts": 1.0})
        parsed = json.loads(line)
        noncanonical = (
            json.dumps(parsed, ensure_ascii=False, sort_keys=False, separators=(", ", ": "))
            + "\n"
        ).encode("utf-8")
        with self.assertRaises(ValueError):
            rd.journal_append_encoded(self.run_dir, noncanonical, self.owner)
        self.assertFalse(os.path.exists(rd._journal_path(self.run_dir, self.owner.generation)))

    def test_preencoded_append_rejects_cross_run_and_cross_generation(self):
        other_run = tempfile.mkdtemp(prefix="summon-r13-other-run-")
        try:
            payload = rd.encode_journal_record({"event": "cross-run"}, timestamp=1.0)
            with self.assertRaises(rd.OwnershipLostError):
                rd.journal_append_encoded(other_run, payload, self.owner)
            self.assertFalse(os.path.exists(
                rd._journal_path(other_run, self.owner.generation)))

            wrong_generation = rd.encode_journal_record(
                {"event": "cross-generation", "generation": self.owner.generation + 1},
                timestamp=1.0,
            )
            with self.assertRaises(ValueError):
                rd.journal_append_encoded(self.run_dir, wrong_generation, self.owner)
            self.assertFalse(os.path.exists(
                rd._journal_path(self.run_dir, self.owner.generation)))
        finally:
            shutil.rmtree(other_run, ignore_errors=True)

    def test_owner_loss_before_write_leaves_no_journal_bytes(self):
        path = rd._journal_path(self.run_dir, self.owner.generation)
        with mock.patch.object(rd, "owner_still_current", return_value=False):
            with self.assertRaises(rd.OwnershipLostError):
                rd.journal_append(self.run_dir, {"event": "not-written"}, self.owner)
        self.assertFalse(os.path.exists(path))

    def _assert_injected_failure(self, phase: str):
        run_dir = tempfile.mkdtemp(prefix="summon-r13-failure-")
        owner = rd.acquire_owner(run_dir, lease_sec=600)
        try:
            real_open = builtins.open

            def injected_open(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if mode == "ab":
                    return _FailingFile(handle, phase)
                return handle

            patchers = [mock.patch.object(rd, "open", injected_open, create=True)]
            if phase == "fsync":
                patchers.append(
                    mock.patch.object(rd.os, "fsync", side_effect=OSError("injected fsync failure"))
                )
            with patchers[0]:
                fsync_patch = patchers[1] if len(patchers) > 1 else mock.patch.object(rd.os, "fsync")
                with fsync_patch:
                    with self.assertRaises(rd.JournalWriteError) as raised:
                        rd.journal_append(run_dir, {"event": "uncertain", "message_id": "m-1"}, owner)
            error = raised.exception
            self.assertEqual(error.phase, "write" if phase in {"short", "disk-full"} else phase)
            self.assertEqual(error.durability, "unknown")
            self.assertTrue(error.write_started)
            self.assertEqual(error.record_identity["message_id"], "m-1")
            self.assertEqual(error.owner_identity["generation"], owner.generation)
            self.assertNotIn(error.record_identity["record_sha256"], str(error))
            self.assertNotIn("uncertain", str(error))
            with self.assertRaises(rd.JournalWriteError) as poisoned:
                rd.journal_append(run_dir, {"event": "must-not-retry"}, owner)
            self.assertEqual(poisoned.exception.phase, "owner_poisoned")
        finally:
            rd.release_owner(owner)
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_write_short_disk_full_flush_and_fsync_failures_are_typed_and_poison_owner(self):
        for phase in ("write", "short", "disk-full", "flush", "fsync"):
            with self.subTest(phase=phase):
                self._assert_injected_failure(phase)

    def test_unexpected_interrupt_after_write_poison_owner_and_preserves_type(self):
        run_dir = tempfile.mkdtemp(prefix="summon-r13-interrupt-")
        owner = rd.acquire_owner(run_dir, lease_sec=600)
        try:
            real_open = builtins.open

            def injected_open(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if mode == "ab":
                    return _FailingFile(handle, "interrupt")
                return handle

            with mock.patch.object(rd, "open", injected_open, create=True):
                with self.assertRaises(KeyboardInterrupt):
                    rd.journal_append(run_dir, {"event": "interrupt"}, owner)
            with self.assertRaises(rd.JournalWriteError) as poisoned:
                rd.journal_append(run_dir, {"event": "must-not-retry"}, owner)
            self.assertEqual(poisoned.exception.phase, "owner_poisoned")
        finally:
            rd.release_owner(owner)
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_non_oserror_close_after_success_poison_owner_and_preserve_type(self):
        run_dir = tempfile.mkdtemp(prefix="summon-r13-close-")
        owner = rd.acquire_owner(run_dir, lease_sec=600)
        try:
            real_open = builtins.open

            def injected_open(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if mode == "ab":
                    return _FailingFile(handle, "close-error")
                return handle

            with mock.patch.object(rd, "open", injected_open, create=True):
                with self.assertRaises(RuntimeError):
                    rd.journal_append(run_dir, {"event": "close-after-success"}, owner)
            with self.assertRaises(rd.JournalWriteError) as poisoned:
                rd.journal_append(run_dir, {"event": "no-retry"}, owner)
            self.assertEqual(poisoned.exception.phase, "owner_poisoned")
        finally:
            rd.release_owner(owner)
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_non_oserror_close_after_flush_failure_preserves_primary(self):
        run_dir = tempfile.mkdtemp(prefix="summon-r13-close-primary-")
        owner = rd.acquire_owner(run_dir, lease_sec=600)
        try:
            real_open = builtins.open

            class FlushThenCloseFailure(_FailingFile):
                def __init__(self, wrapped):
                    super().__init__(wrapped, "flush")

                def close(self):
                    result = self._wrapped.close()
                    raise RuntimeError("injected close failure after flush failure")

            def injected_open(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if mode == "ab":
                    return FlushThenCloseFailure(handle)
                return handle

            with mock.patch.object(rd, "open", injected_open, create=True):
                with self.assertRaises(rd.JournalWriteError) as raised:
                    rd.journal_append(run_dir, {"event": "flush-primary"}, owner)
            self.assertEqual(raised.exception.phase, "flush")
            with self.assertRaises(rd.JournalWriteError) as poisoned:
                rd.journal_append(run_dir, {"event": "no-retry"}, owner)
            self.assertEqual(poisoned.exception.phase, "owner_poisoned")
        finally:
            rd.release_owner(owner)
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_open_failure_is_known_not_written_and_does_not_poison_owner(self):
        def refuse_open(path, mode="r", *args, **kwargs):
            if mode == "ab":
                raise OSError("injected open failure")
            return builtins.open(path, mode, *args, **kwargs)

        with mock.patch.object(rd, "open", refuse_open, create=True):
            with self.assertRaises(rd.JournalWriteError) as raised:
                rd.journal_append(self.run_dir, {"event": "open-fails"}, self.owner)
        self.assertEqual(raised.exception.phase, "open")
        self.assertEqual(raised.exception.durability, "not_written")
        self.assertFalse(raised.exception.write_started)
        self.assertFalse(self.owner.journal_poisoned)

    def test_owner_loss_after_sync_is_uncertain_and_poisoned(self):
        with mock.patch.object(
            rd, "owner_still_current", side_effect=[True, True, False]
        ):
            with self.assertRaises(rd.JournalWriteError) as raised:
                rd.journal_append(self.run_dir, {"event": "late-fence"}, self.owner)
        self.assertEqual(raised.exception.phase, "owner_check_after_sync")
        self.assertEqual(raised.exception.durability, "unknown")
        self.assertTrue(raised.exception.write_started)
        with self.assertRaises(rd.JournalWriteError) as poisoned:
            rd.journal_append(self.run_dir, {"event": "no-replay"}, self.owner)
        self.assertEqual(poisoned.exception.phase, "owner_poisoned")

    def test_owner_loss_after_close_is_uncertain_and_poisoned(self):
        with mock.patch.object(
            rd, "owner_still_current", side_effect=[True, True, True, False]
        ):
            with self.assertRaises(rd.JournalWriteError) as raised:
                rd.journal_append(self.run_dir, {"event": "late-close"}, self.owner)
        self.assertEqual(raised.exception.phase, "owner_check_after_close")
        self.assertTrue(self.owner.journal_poisoned)


if __name__ == "__main__":
    unittest.main()
