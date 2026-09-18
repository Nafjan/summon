from __future__ import annotations
import ast
import copy
import contextlib
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import release_collect as collect
import release_outcomes as contract
import release_gates as gates
manifest = gates._MANIFEST


@contextlib.contextmanager
def isolated_nested_collection():
    """Keep nested in-process collectors from inheriting or leaking state.

    These tests intentionally run the release collector inside the pytest
    process.  Preserve every loader/collector binding that belongs to the
    outer test, evict synthetic fixture modules while the nested run owns the
    names, and restore the active capture after it returns.  This is test
    isolation only; production collection remains process-local and explicit.
    """
    sentinel = object()
    module_names = (
        "test_fixture",
        "tests.test_fixture",
        "test_failure",
        "tests.test_failure",
        "_summon_release_collector",
    )
    saved_modules = {name: sys.modules.get(name, sentinel) for name in module_names}
    saved_quiet = os.environ.get("AGY_PTY_QUIET", sentinel)
    saved_active = collect._ACTIVE
    for name in module_names:
        sys.modules.pop(name, None)
    collect._ACTIVE = None
    try:
        yield
    finally:
        collect._ACTIVE = saved_active
        if saved_quiet is sentinel:
            os.environ.pop("AGY_PTY_QUIET", None)
        else:
            os.environ["AGY_PTY_QUIET"] = saved_quiet
        for name, value in saved_modules.items():
            if value is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class StructuredOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='summon-outcomes-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'tools').mkdir(); (self.root / 'tests').mkdir()
        for name in ('release_gates.py', 'release_collect.py', 'release_skip_policy.json'):
            shutil.copyfile(ROOT / 'tools' / name, self.root / 'tools' / name)
        # The production skip policy is source-bound to its public review
        # anchor. Keep that anchor available in the synthetic root so an
        # unrelated fixture skip is evaluated as an unreviewed skip rather
        # than failing policy loading before terminal accounting.
        review_docs = self.root / 'docs' / 'planning'
        review_docs.mkdir(parents=True)
        shutil.copyfile(
            ROOT / 'docs' / 'planning' / 'CI_EVIDENCE_ACCEPTANCE.md',
            review_docs / 'CI_EVIDENCE_ACCEPTANCE.md',
        )
        self.command = 'python -m pytest -q tests/test_fixture.py'
        self.source = 'a' * 64; self.head = 'b' * 40

    def run_fixture(self, text, framework='pytest', code=None):
        path = self.root / 'tests/test_fixture.py'; path.write_text(text, encoding='utf-8')
        command = self.command if framework == 'pytest' else 'python -m unittest test_fixture' if framework == 'unittest' else 'python tests/test_fixture.py'
        self.command = command
        capture = collect.Capture(self.root, 'suite', 'fixture', command, self.source, self.head)
        old = Path.cwd(); os.chdir(self.root); sys.path.insert(0, str(path.parent))
        try:
            with isolated_nested_collection():
                with mock.patch.dict(os.environ, {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    value = collect.execute(capture, command)
        finally:
            os.chdir(old); sys.path.pop(0)
        return json.loads(json.dumps(value))

    def validate(self, value, **kwargs):
        return contract.validate_outcomes(value, root=self.root, kind='suite', name='fixture',
            command=self.command, source_hash=self.source, git_head=self.head, **kwargs)

    def resign(self, value):
        value.pop('artifact_sha256', None); value['artifact_sha256'] = contract.digest(value)
        return value


    def test_ce_nested_failure_cleanup_restores_aliases_and_ambient_quiet(self):
        names = ("test_failure", "tests.test_failure")
        for quiet in (None, "ambient-value"):
            with mock.patch.dict(sys.modules, {name: object() for name in names}):
                with mock.patch.dict(os.environ, {}, clear=False):
                    if quiet is None:
                        os.environ.pop("AGY_PTY_QUIET", None)
                    else:
                        os.environ["AGY_PTY_QUIET"] = quiet
                    saved = {name: sys.modules[name] for name in names}
                    with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                        with isolated_nested_collection():
                            self.assertTrue(all(name not in sys.modules for name in names))
                            sys.modules.update({name: object() for name in names})
                            os.environ["AGY_PTY_QUIET"] = "nested"
                            raise RuntimeError("synthetic failure")
                    self.assertEqual(os.environ.get("AGY_PTY_QUIET"), quiet)
                    self.assertTrue(all(sys.modules[name] is saved[name] for name in names))

    def test_actual_pytest_pass_and_complete_serialized_inventory(self):
        value = self.run_fixture('def test_one():\n    assert True\n')
        self.assertEqual(self.validate(value), '1/1')
        self.assertEqual(value['collected_nodeids'], ['tests/test_fixture.py::test_one'])
        self.assertEqual(value['counts']['passed'], 1)

    def test_actual_unittest_subtest_skip_is_not_parent_pass_authority(self):
        value = self.run_fixture('import unittest\nclass T(unittest.TestCase):\n def test_one(self):\n  with self.subTest(secret="PRIVATE_VALUE"):\n   self.skipTest("PRIVATE_PATH/token")\n', 'unittest')
        self.assertEqual(len(value['subtests']), 1)
        self.assertEqual(value['subtests'][0]['outcome'], 'skipped')
        self.assertNotIn('PRIVATE', json.dumps(value))
        with self.assertRaises(ValueError): self.validate(value)

    def test_actual_unittest_pass(self):
        value = self.run_fixture('import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\n','unittest')
        self.assertEqual(self.validate(value), '1/1')

    def test_actual_skip_requires_exact_review_policy(self):
        text = 'import pytest\ndef test_one(): pass\ndef test_two(): pytest.skip("Windows-only contract")\n'
        value = self.run_fixture(text)
        self.assertEqual(self.validate(value, require_pass=False), '1/2')
        with self.assertRaises(ValueError): self.validate(value)
        (self.root/'docs').mkdir(exist_ok=True); (self.root/'docs/review.md').write_text('## decision-one\n',encoding='utf-8')
        review = dict(review_id='one',kind='suite',name='fixture',command=self.command,
            nodeid='tests/test_fixture.py::test_two',reason='Windows-only contract',
            platform={'sys_platform':sys.platform},disposition='not_applicable_here',review_ref='docs/review.md#decision-one')
        (self.root/'tools/release_skip_policy.json').write_text(json.dumps({'schema':1,'reviews':[review]}),encoding='utf-8')
        value = self.run_fixture(text)
        self.assertEqual(self.validate(value), '1/2')
        for field, replacement in [('command','python -m pytest -k other'),('source_tree_sha256','c'*64),('collector_sha256','d'*64),('skip_policy_sha256','e'*64)]:
            bad=copy.deepcopy(value);bad[field]=replacement;self.resign(bad)
            with self.assertRaises(ValueError):self.validate(bad)
        bad=copy.deepcopy(value);bad['platform']['sys_platform']='other';self.resign(bad)
        with self.assertRaises(ValueError):self.validate(bad)

    def test_actual_unittest_fixture_error_blocks(self):
        value=self.run_fixture('import unittest\nclass T(unittest.TestCase):\n @classmethod\n def setUpClass(cls): raise RuntimeError("PRIVATE")\n def test_one(self): pass\n','unittest')
        with self.assertRaises(ValueError):self.validate(value)
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_actual_pytest_unsupported_outcomes_and_teardown(self):
        fixtures=[
          'import pytest\n@pytest.mark.xfail\ndef test_one(): assert False\n',
          'import pytest\n@pytest.mark.xfail\ndef test_one(): pass\n',
          'import pytest\n@pytest.fixture\ndef broken():\n yield\n raise RuntimeError("PRIVATE")\ndef test_one(broken): pass\n',
          'raise RuntimeError("PRIVATE")\n',
        ]
        for text in fixtures:
            value=self.run_fixture(text)
            with self.assertRaises(ValueError):self.validate(value)
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_custom_callbacks_preserve_skip_and_failed_terminal(self):
        text='''import sys
import unittest
def test_one(): pass
def test_two(): raise unittest.SkipTest("missing tool")
if __name__ == "__main__":
 c=sys.modules["_summon_release_collector"]
 c.custom_start(__file__,["test_one","test_two"])
 for name in ["test_one","test_two"]:
  try: globals()[name](); c.custom_result(__file__,name,"passed")
  except unittest.SkipTest as e: c.custom_result(__file__,name,"skipped",str(e))
'''
        value=self.run_fixture(text,'custom')
        self.assertEqual(self.validate(value,require_pass=False),'1/2')
        with self.assertRaises(ValueError):self.validate(value)

    def test_recomputed_hash_cannot_hide_bad_semantics(self):
        value=self.run_fixture('def test_one(): pass\n')
        for mutate in [lambda v:v['counts'].update(passed=True),lambda v:v['counts'].update(passed=2),lambda v:v.update(exit_code=7),lambda v:v.update(completed=False),lambda v:v['cases'].append(dict(v['cases'][0])),lambda v:v['cases'][0].update(outcome='invented'),lambda v:v['cases'][0].update(nodeid='tests/test_fixture.py::foreign')]:
            bad=copy.deepcopy(value);mutate(bad);self.resign(bad)
            with self.assertRaises(ValueError):self.validate(bad)

    def test_same_semantics_at_manifest_intake_and_final_check(self):
        value=self.run_fixture('def test_one(): pass\n')
        evidence={'schema':2,'source_tree_sha256':self.source,'git_head':self.head,
            'test_results':{'fixture':{'count':'1/1','output_sha256':value['output_sha256'],'outcomes':value}},'gate_results':{}}
        with mock.patch.object(manifest,'REQUIRED_COMMANDS',{'fixture':self.command}),mock.patch.object(manifest,'REQUIRED_GATE_COMMANDS',{}):
            manifest._validate_machine_outcomes(evidence,self.root)
            bad=copy.deepcopy(evidence);bad['test_results']['fixture']['outcomes']['exit_code']=9
            self.resign(bad['test_results']['fixture']['outcomes'])
            with self.assertRaises(ValueError):manifest._validate_machine_outcomes(bad,self.root)
            failures=manifest._check_facts({'evidence':bad,'test_results':bad['test_results'],'gate_results':{}},self.root)
            self.assertTrue(any('structured' in f for f in failures))
            with self.assertRaises(ValueError):manifest._validate_machine_outcomes(dict(evidence,schema=1),self.root)

    def test_real_wrapper_subprocess_produces_serialized_outcomes(self):
        for name in ('release_outcomes.py','release_manifest.py','release_contract.py'):
            shutil.copyfile(ROOT/'tools'/name,self.root/'tools'/name)
        spawn=self.root/'skills/summon/scripts/_spawn.py';spawn.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'skills/summon/scripts/_spawn.py',spawn)
        (self.root/'tests/__init__.py').write_text('',encoding='utf-8')
        (self.root/'tests/test_release_contract.py').write_text(
            'import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\n',encoding='utf-8')
        source=manifest.source_tree_sha256(self.root)
        with mock.patch.object(gates,'ROOT',self.root):
            count, output, value=gates._run_outcomes('suite','release_contract',20,source,None)
        self.assertEqual(count,'1/1')
        self.assertEqual(value['output_sha256'],hashlib.sha256(output.encode('utf-8')).hexdigest())
        self.assertEqual(value['execution_template'],contract.execution_template('suite','release_contract'))
        self.assertEqual(value['counts']['passed'],1)
        self.assertNotIn(str(self.root),json.dumps(value))

    def test_serialized_intake_and_final_check_share_gate_semantics(self):
        value=self.run_fixture('def test_one(): pass\n')
        # Re-run the same actual fixture under its gate identity.
        capture=collect.Capture(self.root,'gate','browser_security',self.command,self.source,self.head)
        old=Path.cwd();os.chdir(self.root)
        try:
            with isolated_nested_collection():
                with mock.patch.dict(os.environ, {'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}):
                    gate_value=collect.execute(capture,self.command)
        finally:os.chdir(old)
        artifact=gates._artifact('browser_security',status='pass',command=self.command,
            source_hash=self.source,git_head=self.head,output='',test_count='1/1',outcomes=gate_value)
        evidence=dict(schema=2,producer='tools/release_gates.py',
            producer_sha256=contract.file_hash(self.root/'tools/release_gates.py'),
            source_tree_sha256=self.source,git_head=self.head,git_status_clean=True,
            version_contract={'ready':True},tests={},test_results={},commands={},
            known_gates={'browser_security':'pass'},gate_results={'browser_security':artifact},
            gate_commands={'browser_security':self.command},runtime={'python_version':'3.13.11','private':'PRIVATE'})
        path=self.root/'evidence.json'
        with mock.patch.object(manifest,'REQUIRED_COMMANDS',{}),mock.patch.object(manifest,'REQUIRED_TESTS',frozenset()),mock.patch.object(manifest,'REQUIRED_GATE_COMMANDS',{'browser_security':self.command}),mock.patch.object(manifest,'REQUIRED_GATES',frozenset({'browser_security'})),mock.patch.object(manifest,'_version_contract',return_value={'ready':True}),mock.patch.object(manifest,'_git_facts',return_value={'head':self.head,'dirty':False}):
            path.write_text(json.dumps(evidence),encoding='utf-8')
            read=manifest._read_evidence(path,self.root,self.source)
            self.assertEqual(read['runtime'],{'python_version':'3.13.11'})
            projected=dict(evidence=read,version_contract={'ready':True},source_tree_sha256=self.source,
                git={'head':self.head,'dirty':False},tests={},test_results={},
                known_gates=read['known_gates'],gate_results=read['gate_results'])
            self.assertEqual(manifest._check_facts(projected,self.root),[])
            gate_value['counts']['passed']=2;self.resign(gate_value)
            artifact['outcomes']=gate_value
            artifact['artifact_sha256']=manifest._artifact_sha256(artifact)
            path.write_text(json.dumps(evidence),encoding='utf-8')
            with self.assertRaises(ValueError):manifest._read_evidence(path,self.root,self.source)
            projected['evidence']=evidence;projected['gate_results']=evidence['gate_results']
            self.assertTrue(any('structured' in x for x in manifest._check_facts(projected,self.root)))

    def test_pytest_subtests_require_plugin_and_deselection_blocks(self):
        value=self.run_fixture('def test_one(subtests):\n with subtests.test(secret="PRIVATE"):\n  import pytest\n  pytest.skip("PRIVATE_PATH/token")\n')
        # Runtime capability can be present even when optional plugin metadata
        # is unavailable. Trust the captured terminal shape, not the label.
        if value['subtests']:
            self.assertEqual(len(value['subtests']),1)
        else:
            self.assertTrue(
                value['counts']['errors'] > 0
                or 'incomplete_collection' in value['problems']
                or 'collector_execution_error' in value['problems'])
        self.assertNotIn('PRIVATE',json.dumps(value))
        with self.assertRaises(ValueError):self.validate(value)
        self.command='python -m pytest -q tests/test_fixture.py -k test_one'
        value=self.run_fixture('def test_one(): pass\ndef test_two(): pass\n')
        self.assertGreater(value['counts']['deselected'],0)
        with self.assertRaises(ValueError):self.validate(value)

    def test_version_contract_names_structured_evidence_without_claiming_run(self):
        import release_contract
        with mock.patch.object(release_contract,'version_facts',return_value={'converged':True}),mock.patch.object(release_contract,'migration_contract',return_value={'complete':True}):
            value=release_contract.release_contract(self.root)
        self.assertEqual(value['test_evidence'],{'schema':2,'skip_policy':'tools/release_skip_policy.json'})
        self.assertNotIn('passed',value['test_evidence'])

    def test_duplicate_json_keys_refuse(self):
        path=self.root/'bad.json';path.write_text('{"schema":1,"schema":2}',encoding='utf-8')
        with self.assertRaises(ValueError):contract.read_json(path)

    def test_stale_schema_one_is_sanitized_preview_only(self):
        path=self.root/'legacy.json'
        path.write_text(json.dumps({'schema':1,'tests':{'fixture':'1/2'},
            'known_gates':{'browser_security':'pass'},'runtime':{'private':'PRIVATE'},
            'gate_results':{'private':'PRIVATE'}}),encoding='utf-8')
        read=manifest._read_evidence(path,self.root,self.source,allow_legacy_preview=True)
        self.assertTrue(read['legacy_preview'])
        self.assertEqual(read['tests'],{'fixture':'1/2'})
        self.assertNotIn('PRIVATE',json.dumps(read))
        with self.assertRaises(ValueError):manifest._validate_machine_outcomes(read,self.root)

    def test_actual_discovery_harness_keeps_skip_leak_failure(self):
        text=(ROOT/'skills/summon/scripts/test_discovery.py').read_text(encoding='utf-8')
        harness=text[text.rindex('if __name__ == "__main__":'):]
        fixture="""import sys
import unittest
_state={'value':object()}
def _global_fingerprint(): return dict(_state)
def _repair_global_leaks(before, leaked): _state.update(before)
def test_one(): pass
def test_two():
 _state['value']=object()
 raise unittest.SkipTest('missing tool')
"""+harness
        value=self.run_fixture(fixture,'custom')
        self.assertEqual(value['counts']['failed'],1)
        self.assertEqual(value['counts']['skipped'],0)
        self.assertEqual(value['exit_code'],1)
        with self.assertRaises(ValueError):self.validate(value)

    def test_discovery_has_no_implicit_direct_return_skip_and_keeps_leak_override(self):
        path=ROOT/'skills/summon/scripts/test_discovery.py'; text=path.read_text(encoding='utf-8')
        tree=ast.parse(text)
        def returns(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda)):continue
                if isinstance(child,ast.Return):yield child
                yield from returns(child)
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and node.name.startswith('test_'):
                self.assertEqual(list(returns(node)),[],node.name)
        self.assertIn('if _outcome == "skipped":\n                skipped -= 1',text)
        self.assertIn('_collector.custom_result(__file__, t.__name__, _outcome, _reason)',text)

if __name__=='__main__':unittest.main()
