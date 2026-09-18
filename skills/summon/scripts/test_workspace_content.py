"""Provider-inert private-store tests; every filesystem fixture is temporary."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from contextlib import contextmanager
from unittest.mock import patch

import _workspace_content as wc
from _fleet_approval import _secure_file, _secure_private_root
from _spawn import popen_flags


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-content-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "content"
        _secure_private_root(str(self.root))
        self.store = wc.ContentStore(self.root)

    def assert_kind(self, kind, function, *args, **kwargs):
        with self.assertRaises(wc.ContentError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.kind, kind)
        self.assertNotIn(str(self.root), str(caught.exception))
        return caught.exception

    def orphan(self, name, raw=b""):
        path = self.root / name
        path.write_bytes(raw)
        _secure_file(str(path))
        return path

    def test_unicode_exactness_reopen_and_idempotent_ref(self):
        raw = "\ufeffline one\r\nمرحبا\n雪😀\x00end".encode("utf-8")
        ref = wc.prepare_content(raw)
        result = self.store.put(ref, raw)
        self.assertFalse(result.reused)
        self.assertEqual(result.file_durability, "file_fsync_confirmed")
        self.assertEqual(ref.payload_bytes, len(raw))
        self.assertEqual(ref.sha256, hashlib.sha256(raw).hexdigest())
        reopened = wc.ContentStore(self.root)
        self.assertEqual(reopened.read(ref), raw)
        self.assertTrue(reopened.put(ref, raw).reused)
        self.assertEqual(len([p for p in self.root.iterdir() if p.name.startswith("blob-")]), 1)

    def test_bad_utf8_empty_and_byte_limit(self):
        for raw in (b"\xff", "\ud800"):
            self.assert_kind("invalid_utf8", wc.prepare_content, raw)
        for raw in (b"", b"x" * 4097, "😀" * 1025):
            self.assert_kind("payload_limit", wc.prepare_content, raw)
        raw = "😀" * 1024
        ref = wc.prepare_content(raw)
        self.store.put(ref, raw)
        self.assertEqual(len(self.store.read(ref)), 4096)

    def test_invalid_refs_never_resolve_paths(self):
        base = wc.prepare_content("body")
        for value in ("../outside", "blob-" + "a" * 32 + ":stream", "C:\\secret",
                      "blob-" + "a" * 32 + "\n", "blob-" + "a" * 31, None):
            self.assert_kind("invalid_reference", self.store.read, replace(base, reference=value))
        self.assert_kind("invalid_reference", self.store.read, replace(base, payload_bytes=True))
        self.assert_kind("invalid_reference", self.store.read, replace(base, sha256="a" * 63))

    def test_missing_and_mismatch_never_empty_or_overwrite(self):
        ref = wc.prepare_content("correct")
        self.assert_kind("content_missing", self.store.read, ref)
        path = self.orphan(ref.reference, b"wrong")
        self.assert_kind("content_mismatch", self.store.read, ref)
        self.assert_kind("content_mismatch", self.store.put, ref, "correct")
        self.assertEqual(path.read_bytes(), b"wrong")
        self.assert_kind("request_mismatch", self.store.put, ref, "other")

    def test_digest_and_length_verified_independently(self):
        ref = wc.prepare_content("body")
        self.store.put(ref, "body")
        self.assert_kind("content_mismatch", self.store.read, replace(ref, sha256="0" * 64))
        self.assert_kind("content_mismatch", self.store.read, replace(ref, payload_bytes=3))

    def test_existing_root_required_and_not_claimed(self):
        missing = Path(self.temp.name) / "missing"
        self.assert_kind("private_root_invalid", wc.ContentStore, missing)
        self.assertFalse(missing.exists())
        with patch.object(wc, "_verify_private", side_effect=ValueError("unsafe")):
            self.assert_kind("private_root_invalid", wc.ContentStore, self.root)

    def test_reparse_validation_fail_closed(self):
        ref = wc.prepare_content("body")
        with patch.object(wc, "_reject_reparse_ancestors", side_effect=ValueError("reparse")):
            self.assert_kind("private_root_invalid", self.store.put, ref, "body")

    def test_real_symlink_is_refused(self):
        ref = wc.prepare_content("body")
        target = Path(self.temp.name) / "outside"
        target.write_bytes(b"body")
        try:
            os.symlink(target, self.root / ref.reference)
        except OSError:
            self.skipTest("host cannot create a symlink fixture")
        self.assert_kind("unsafe_entry", self.store.read, ref)
        self.assertEqual(target.read_bytes(), b"body")

    def test_hardlink_is_refused(self):
        original = self.orphan("original", b"body")
        ref = wc.prepare_content("body")
        os.link(original, self.root / ref.reference)
        self.assert_kind("unsafe_entry", self.store.read, ref)

    def test_opened_handle_path_unverifiable_refuses(self):
        ref = wc.prepare_content("body")
        self.store.put(ref, "body")
        with patch.object(wc, "_final_open_path", return_value=None):
            self.assert_kind("opened_file_changed", self.store.read, ref)

    def test_count_includes_zero_byte_orphans_and_unknown_names(self):
        for i in range(32):
            self.orphan(f"incomplete-{i}")
        ref = wc.prepare_content("body")
        self.assert_kind("store_limit", self.store.put, ref, "body")
        self.assertEqual(len(list(self.root.iterdir())), 33)  # fixed empty lock

    def test_exact_total_capacity_and_no_growth_on_reuse(self):
        raw = b"x" * 4096
        refs = [wc.prepare_content(raw) for _ in range(32)]
        for ref in refs:
            self.store.put(ref, raw)
        self.assertEqual(sum(p.stat().st_size for p in self.root.iterdir()), 131072)
        self.assertTrue(self.store.put(refs[0], raw).reused)
        self.assert_kind("store_limit", self.store.put, wc.prepare_content("x"), "x")

    def test_orphan_lengths_count_in_aggregate_admission(self):
        self.orphan("incomplete", b"x" * 100)
        with patch.object(wc, "MAX_AGGREGATE_PAYLOAD_BYTES", 104):
            self.store.put(wc.prepare_content("four"), "four")
            self.assert_kind("store_limit", self.store.put, wc.prepare_content("x"), "x")

    def test_bad_inventory_and_nonempty_lock_are_not_empty_store(self):
        ref = wc.prepare_content("body")
        with patch.object(wc.os, "scandir", side_effect=PermissionError("private")):
            self.assert_kind("inventory_unavailable", self.store.put, ref, "body")
        self.orphan(wc._LOCK_NAME, b"foreign")
        self.assert_kind("unsafe_lock", self.store.put, ref, "body")

    def test_fsync_failure_uncertain_reconcile_same_bytes_with_new_fsync(self):
        ref = wc.prepare_content("body")
        with patch.object(wc.os, "fsync", side_effect=OSError("private")):
            error = self.assert_kind("write_fsync", self.store.put, ref, "body")
        self.assertEqual(error.durability, "unknown")
        self.assertEqual(self.store.read(ref), b"body")  # reading is not durability
        original = wc.os.fsync
        with patch.object(wc.os, "fsync", wraps=original) as synced:
            self.assertTrue(self.store.put(ref, "body").reused)
            self.assertGreaterEqual(synced.call_count, 1)

    def _write_fault(self, action):
        original = os.fdopen

        class FaultStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def write(self, raw):
                if action == "short":
                    return self.stream.write(raw[:2])
                return self.stream.write(raw)
            def flush(self):
                if action == "flush":
                    raise OSError("private")
                return self.stream.flush()
            def fileno(self):
                return self.stream.fileno()

        def wrapper(fd, mode, **kwargs):
            stream = original(fd, mode, **kwargs)
            return FaultStream(stream) if mode == "r+b" else stream
        return patch.object(wc.os, "fdopen", side_effect=wrapper)

    def test_short_write_retained_and_never_overwritten(self):
        ref = wc.prepare_content("body")
        with self._write_fault("short"):
            error = self.assert_kind("write_write", self.store.put, ref, "body")
        self.assertEqual(error.durability, "unknown")
        self.assertEqual((self.root / ref.reference).read_bytes(), b"bo")
        self.assert_kind("content_mismatch", self.store.put, ref, "body")

    def test_flush_failure_is_uncertain(self):
        ref = wc.prepare_content("body")
        with self._write_fault("flush"):
            error = self.assert_kind("write_flush", self.store.put, ref, "body")
        self.assertEqual(error.durability, "unknown")
        self.assertTrue((self.root / ref.reference).exists())

    def test_namespace_boundary_explicit(self):
        ref = wc.prepare_content("body")
        if os.name == "nt":
            self.assert_kind("namespace_durability_unqualified", self.store.put,
                             ref, "body", require_namespace_durable=True)
            self.assertFalse((self.root / ref.reference).exists())
            result = self.store.put(ref, "body")
            self.assertEqual(result.namespace_durability, "unqualified")
        else:
            result = self.store.put(ref, "body", require_namespace_durable=True)
            self.assertEqual(result.namespace_durability, "directory_fsync_confirmed")

    def test_namespace_sync_failure_does_not_return_success(self):
        ref = wc.prepare_content("body")
        with patch.object(self.store, "_sync_namespace", side_effect=OSError("private")):
            error = self.assert_kind("write_namespace_sync", self.store.put, ref, "body")
        self.assertEqual(error.durability, "unknown")

    def test_read_error_never_becomes_empty_content(self):
        ref = wc.prepare_content("body")
        self.store.put(ref, "body")
        original = wc.os.open

        def denied(path, *args, **kwargs):
            if os.path.basename(path) == ref.reference:
                raise PermissionError("private")
            return original(path, *args, **kwargs)

        with patch.object(wc.os, "open", side_effect=denied):
            self.assert_kind("content_unreadable", self.store.read, ref)

    def test_post_write_lock_failure_preserves_uncertainty(self):
        ref = wc.prepare_content("body")
        original = wc._exclusive_control_lock

        @contextmanager
        def failed_release(path):
            with original(path):
                yield
            raise OSError("private")

        with patch.object(wc, "_exclusive_control_lock", side_effect=failed_release):
            error = self.assert_kind("store_io", self.store.put, ref, "body")
        self.assertEqual(error.durability, "unknown")
        self.assertTrue(self.store.put(ref, "body").reused)

    def test_provision_verifies_lock_without_creating_content(self):
        self.store.provision()
        self.assertEqual([p.name for p in self.root.iterdir()], [wc._LOCK_NAME])
        self.assertEqual((self.root / wc._LOCK_NAME).stat().st_size, 0)
        reopened = wc.ContentStore(self.root)
        reopened.provision()
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_first_use_unverified_lock_refuses_before_publication(self):
        # Model the interval after exclusive create but before ACL normalization.
        # The concurrent caller must not treat that interval as an empty store.
        secure = wc._secure_file
        verify = wc._verify_private
        observed = []

        def provision_window(path):
            def unverified(candidate, *, directory):
                if candidate == path:
                    raise ValueError("lock protection not normalized yet")
                return verify(candidate, directory=directory)

            with patch.object(wc, "_verify_private", side_effect=unverified):
                error = self.assert_kind("unsafe_entry", wc.ContentStore(self.root).provision)
                observed.append(error.kind)
            secure(path)

        with patch.object(wc, "_secure_file", side_effect=provision_window):
            self.store.provision()
        self.assertEqual(observed, ["unsafe_entry"])
        self.assertEqual([p.name for p in self.root.iterdir()], [wc._LOCK_NAME])
        self.store.provision()

    def test_cross_process_capacity_serialized(self):
        # Publication follows supervisor provisioning; this proves concurrent
        # capacity admission, not race-free lock bootstrap by arbitrary callers.
        self.store.provision()
        # Only one payload slot remains; independently opened stores must agree.
        for i in range(31):
            self.orphan(f"orphan-{i}")
        gate = Path(self.temp.name) / "start"
        code = '''import os, sys, time
from _workspace_content import ContentStore, ContentError, prepare_content
while not os.path.exists(sys.argv[2]): time.sleep(0.01)
try:
    ContentStore(sys.argv[1]).put(prepare_content("body"), "body")
    print("stored")
except ContentError as exc:
    print(exc.kind)
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SUMMON_TELEMETRY="0",
                   PYTHONIOENCODING="utf-8", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        processes = []
        try:
            for _ in range(4):
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", code, str(self.root), str(gate)],
                    cwd=str(Path(__file__).parent), env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, **popen_flags()))
            gate.write_text("go", encoding="utf-8")
            results = []
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, "private child failed")
                self.assertEqual(error, "", "private child emitted a diagnostic")
                results.append(output.strip())
            self.assertEqual(results.count("stored"), 1)
            self.assertEqual(results.count("store_limit"), 3)
            self.assertEqual(len(list(self.root.iterdir())), 33)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
