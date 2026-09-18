"""Public artifact identity acceptance with ordinary reopen/strict rebuild parity.

Metadata is synthetic; no artifact file is created or dereferenced. Writable
fixtures are individually minted TemporaryDirectory roots, never source ancestors.
Run with Python -B; no provider, browser, server, or installed account is used.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
_READ_ROOTS = (HERE.resolve(), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
_WRITE_ROOT = None
_GUARD_ACTIVE = False
_GUARD_HITS = []
_FORBIDDEN_IMPORTS = ('_credentials', '_windows_credentials', '_nous_credentials', '_auth',
                      '_telemetry', '_executor', '_apibackend', 'keyring', 'win32cred')


def _inside(path, root):
    return root is not None and (path == root or root in path.parents)


def _audit(event, args):
    if not _GUARD_ACTIVE:
        return
    blocked = event.startswith(('subprocess.', 'socket.', 'os.spawn', 'os.exec')) or event in {'os.system', 'os.startfile'}
    if event == 'import' and args:
        blocked = blocked or str(args[0]).startswith(_FORBIDDEN_IMPORTS)
    if event == 'open' and args and not isinstance(args[0], int):
        path = Path(os.fsdecode(args[0])).resolve()
        mode = args[1] or ''
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = any(char in mode for char in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        blocked = blocked or path.name == 'never-created.txt'
        blocked = blocked or (not _inside(path, _WRITE_ROOT) if writing else
                              not (_inside(path, _WRITE_ROOT) or any(_inside(path, root) for root in _READ_ROOTS)))
    if event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'}:
        paths = args[:2] if event == 'os.rename' else args[:1]
        blocked = blocked or any(not _inside(Path(os.fsdecode(path)).resolve(), _WRITE_ROOT) for path in paths)
    if blocked:
        _GUARD_HITS.append(event)
        raise AssertionError('synthetic artifact fixture boundary refused')


sys.addaudithook(_audit)
_GUARD_ACTIVE = True
try:
    from _swarm_coordinator import SwarmCoordinator, SwarmConflictError, rebuild_projection_from_journal
finally:
    _GUARD_ACTIVE = False


class ArtifactIdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        global _GUARD_ACTIVE, _WRITE_ROOT
        # Mint the explicitly owned fixture before activating its file fence.
        self.temp = tempfile.TemporaryDirectory(prefix='summon-artifact-identity-')
        _WRITE_ROOT = Path(self.temp.name).resolve()
        self.addCleanup(self.disable_guard)
        self.addCleanup(self.temp.cleanup)
        system = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
        system.update({key: self.temp.name for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')})
        system.update(PATH='', SUMMON_TELEMETRY='0', PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        self.environment = mock.patch.dict(os.environ, system, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        _GUARD_HITS.clear()
        _GUARD_ACTIVE = True
        self.clock = lambda: 2_000_000_000.0
        self.coordinator = SwarmCoordinator.create(
            self.temp.name, 'run-1', project_root_sha256='a' * 64,
            roster_definition_sha256='b' * 64,
            tasks=[{'task_id': 'task-1', 'request_sha256': 'c' * 64}],
            max_attempts=2, clock=self.clock)
        self.coordinator.register_worker('worker-1', worker_instance_id='instance-1', capabilities=['artifact'])
        claim = self.coordinator.claim('worker-1', 'task-1', request_sha256='c' * 64)
        self.body = dict(task_id='task-1', claim_id=claim['claim_id'], attempt=claim['attempt'],
                         lease_generation=claim['lease_generation'], request_sha256='c' * 64,
                         artifact_id='artifact-1', sha256='d' * 64, bytes_count=12,
                         media_type='text/plain', relative_path='never-created.txt')
        self.run_dir = Path(self.coordinator.run_dir)

    @staticmethod
    def disable_guard():
        global _GUARD_ACTIVE, _WRITE_ROOT
        _GUARD_ACTIVE = False
        _WRITE_ROOT = None

    def tearDown(self):
        self.assertEqual(_GUARD_HITS, [], 'unexpected process/network/private-file/import edge')

    def journals(self):
        return {p.name: p.read_bytes() for p in sorted(self.run_dir.glob('journal-g*.jsonl'))}

    def publish(self, coordinator=None, message_id='publication-1', **changes):
        return (coordinator or self.coordinator).publish_artifact('worker-1', message_id=message_id, **(self.body | changes))

    def reopen(self):
        return SwarmCoordinator(self.temp.name, 'run-1', clock=self.clock)

    def artifacts(self, coordinator):
        return [event for event in coordinator.events() if event.get('event') == 'artifact_published']

    def rebuild(self):
        return rebuild_projection_from_journal(self.run_dir, expected_run_id='run-1', expected_project_root_sha256='a' * 64)

    def assert_readers_agree(self, expected_ids):
        before = self.journals()
        reopened = self.reopen()
        self.assertNotEqual(reopened.status()['status'], 'missing')
        rows = self.artifacts(reopened)
        self.assertEqual([row['artifact_id'] for row in rows], expected_ids)
        ordinary, torn = reopened._load()
        self.assertFalse(torn)
        rebuilt = self.rebuild()
        self.assertEqual(ordinary['artifacts'], rebuilt['projection']['artifacts'])
        self.assertEqual([row['artifact_id'] for row in ordinary['artifacts']], expected_ids)
        self.assertFalse(rebuilt['launch_authority'])
        self.assertEqual(before, self.journals())
        self.assertFalse((self.run_dir / 'never-created.txt').exists())
        return reopened

    def test_exact_frame_replay_before_and_after_reopen_is_one_publication(self):
        first = self.publish()
        before = self.journals()
        self.assertEqual(first, self.publish())
        self.assertEqual(before, self.journals())
        reopened = self.assert_readers_agree(['artifact-1'])
        self.assertEqual(first, self.publish(reopened))
        self.assertEqual(before, self.journals())
        self.assert_readers_agree(['artifact-1'])

    def _new_frame_case(self, conflicting):
        first = self.publish()
        self.assertEqual(first['status'], 'artifact_published')
        self.assert_readers_agree(['artifact-1'])
        changes = {'sha256': 'e' * 64, 'bytes_count': 13} if conflicting else {}
        before = self.journals()
        for index, coordinator in enumerate((self.coordinator, self.reopen()), 2):
            with self.assertRaisesRegex(SwarmConflictError, 'artifact id was already published'):
                self.publish(coordinator, message_id='publication-' + str(index), **changes)
            self.assertEqual(before, self.journals())
        reopened = self.assert_readers_agree(['artifact-1'])
        self.assertEqual(first, self.publish(reopened))
        self.assertEqual(before, self.journals())

    def test_identical_metadata_new_frame_refuses_before_append(self):
        self._new_frame_case(False)

    def test_conflicting_metadata_new_frame_refuses_before_append(self):
        self._new_frame_case(True)

    def test_fresh_artifact_id_still_publishes_and_replays(self):
        self.publish()
        second = self.publish(message_id='publication-2', artifact_id='artifact-2', sha256='e' * 64, bytes_count=13)
        self.assertEqual(second['status'], 'artifact_published')
        before = self.journals()
        reopened = self.assert_readers_agree(['artifact-1', 'artifact-2'])
        self.assertEqual(second, self.publish(reopened, message_id='publication-2', artifact_id='artifact-2', sha256='e' * 64, bytes_count=13))
        self.assertEqual(before, self.journals())
        self.assert_readers_agree(['artifact-1', 'artifact-2'])


if __name__ == '__main__':
    unittest.main()


