"""Fixed-registry instrumentation wrapper for actual release test outcomes."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import platform
import runpy
import shlex
import sys
import unittest

import release_outcomes as contract
ROOT = Path(__file__).resolve().parents[1]
_ACTIVE = None


def load_manifest():
    spec = importlib.util.spec_from_file_location('_collector_manifest', ROOT / 'tools/release_manifest.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Capture:
    def __init__(self, root, kind, name, command, source_hash, git_head):
        self.root = Path(root)
        self.nodes = set(); self.rows = {}; self.subtests = []
        self.problems = set(); self.pytest_nodes = {}; self.subindices = {}
        self.binding = dict(kind=kind, name=name, command=command)
        machine = platform.machine().lower()
        self.value = dict(schema=1, **self.binding,
            execution_template=contract.execution_template(kind, name),
            source_tree_sha256=source_hash, git_head=git_head,
            producer_sha256=contract.file_hash(self.root / 'tools/release_gates.py'),
            collector_sha256=contract.file_hash(self.root / contract.COLLECTOR),
            skip_policy_sha256=contract.file_hash(self.root / contract.POLICY),
            output_sha256=hashlib.sha256(b'').hexdigest(),
            platform={'sys_platform': sys.platform, 'os_name': os.name,
                'machine': machine if contract.safe_token(machine) else 'unknown',
                'python_version': platform.python_version(), 'pytest_version': 'none', 'pytest_subtests_version': 'none'})

    def node(self, path, name):
        try: relative = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            self.problems.add('unsafe_test_identity'); return None
        node = relative + '::' + name
        if not contract.safe_node(node):
            self.problems.add('unsafe_test_identity'); return None
        return node

    def add(self, node):
        if node is None: return
        if node in self.nodes: self.problems.add('duplicate_test_identity')
        self.nodes.add(node)

    def row(self, node, outcome, phase='call', reason=None, parent=None):
        if node is None:
            self.problems.add('unmapped_test_result'); return
        row = dict(nodeid=node, outcome=outcome, phase=phase)
        if parent is not None: row['parent'] = parent
        if outcome == 'skipped':
            reason_hash = hashlib.sha256(str(reason).encode('utf-8')).hexdigest()
            review = contract.review_for(self.root, self.binding, node, reason_hash, self.value['platform'])
            row.update(reason_code='reviewed_literal' if review else 'unreviewed',
                reason=review['reason'] if review else contract.REDACTED,
                reason_sha256=reason_hash, review_id=review['review_id'] if review else None)
        if parent is not None: self.subtests.append(row); return
        # Teardown errors must override an earlier successful call or skip.
        previous = self.rows.get(node)
        if previous is None or outcome in {'errors', 'failed', 'xpassed'}:
            self.rows[node] = row
        elif previous['outcome'] == 'passed' and outcome != 'passed':
            self.rows[node] = row

    def finish(self, code, completed):
        missing = self.nodes - self.rows.keys()
        if missing: self.problems.add('missing_terminal_results')
        for n in missing: self.row(n, 'errors', 'collection')
        rows = [self.rows[n] for n in sorted(self.rows)]
        counts = {k: sum(r['outcome'] == k for r in rows) for k in contract.CATEGORIES}
        self.value.update(completed=bool(completed), exit_code=code,
            collected_nodeids=sorted(self.nodes), cases=rows,
            subtests=self.subtests, counts=counts, problems=sorted(self.problems))
        self.value['artifact_sha256'] = contract.digest(self.value)
        return self.value

    def pytest_collection_modifyitems(self, session, config, items):
        for index, item in enumerate(items):
            raw = item.nodeid
            parts = raw.split('::')
            suffix = '::'.join(parts[1:])
            if '[' in suffix:
                # Parameter reprs can contain credentials and paths. Explicit
                # source literal IDs are admitted; dynamic IDs refuse privately.
                base, param = suffix.split('[', 1); param = param[:-1]
                try:
                    tree = ast.parse(Path(item.path).read_text(encoding='utf-8'))
                    explicit = set()
                    for call in ast.walk(tree):
                        if not isinstance(call, ast.Call): continue
                        for kw in call.keywords:
                            if kw.arg == 'id' and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                explicit.add(kw.value.value)
                            if kw.arg == 'ids' and isinstance(kw.value, (ast.List, ast.Tuple)):
                                explicit.update(x.value for x in kw.value.elts if isinstance(x, ast.Constant) and isinstance(x.value, str))
                    # Pytest composes source-literal IDs from stacked
                    # parameterizations with a '-' separator. Accept only
                    # exact literal IDs or a composition whose segments are
                    # each independently declared and safe; never accept a
                    # runtime repr or arbitrary generated text.
                    segments = param.split('-')
                    safe = (
                        (param in explicit and contract.safe_token(param))
                        or (
                            len(segments) > 1
                            and all(segment in explicit for segment in segments)
                            and all(contract.safe_token(segment) for segment in segments)
                        )
                    )
                except (OSError, SyntaxError, UnicodeError): safe = False
                if safe:
                    suffix = base + '::' + param
                else:
                    suffix = base + '::case_' + str(index)
                    self.problems.add('unsafe_parameter_identity')
            node = self.node(item.path, suffix)
            self.pytest_nodes[raw] = node; self.add(node)

    def pytest_deselected(self, items):
        for item in items:
            node = self.pytest_nodes.get(item.nodeid)
            if node is None:
                node = self.node(item.path, '::'.join(item.nodeid.split('::')[1:]).split('[')[0])
                self.add(node)
            self.row(node, 'deselected', 'collection')
        self.problems.add('deselected_tests')

    def pytest_collectreport(self, report):
        if report.failed or report.skipped: self.problems.add('incomplete_collection')

    def pytest_runtest_logreport(self, report):
        node = self.pytest_nodes.get(report.nodeid)
        if hasattr(report, 'context'):
            parent = node
            if parent is None: self.problems.add('unmapped_subtest'); return
            index = self.subindices.get(parent, 0); self.subindices[parent] = index + 1
            sub = parent + '::subtest_' + str(index)
            outcome = 'passed' if report.passed else 'skipped' if report.skipped else 'failed'
            reason = report.longrepr[2] if report.skipped and isinstance(report.longrepr, tuple) else ''
            if reason.startswith('Skipped: '): reason = reason[9:]
            self.row(sub, outcome, report.when, reason, parent); return
        if report.failed: outcome = 'failed' if report.when == 'call' else 'errors'
        elif hasattr(report, 'wasxfail'): outcome = 'xfailed' if report.skipped else 'xpassed'
        elif report.skipped: outcome = 'skipped'
        elif report.when == 'call': outcome = 'passed'
        else: return
        reason = report.longrepr[2] if report.skipped and isinstance(report.longrepr, tuple) else ''
        if reason.startswith('Skipped: '): reason = reason[9:]
        self.row(node, outcome, report.when, reason)

    def unit_node(self, test):
        if hasattr(test, 'test_case'): test = test.test_case
        try:
            method = getattr(test, '_testMethodName')
            return self.node(inspect.getfile(type(test)), type(test).__qualname__ + '::' + method)
        except (AttributeError, TypeError, OSError): return None

    def unit_start(self, suite):
        def visit(test):
            if isinstance(test, unittest.TestSuite):
                for child in test: visit(child)
            else: self.add(self.unit_node(test))
        visit(suite)

    def unit_result(self, test, outcome, reason=None):
        node = self.unit_node(test)
        if node is None:
            # unittest's fixture holder has no TestCase method. Collection was
            # already enumerated before the runner, so expand only exact known
            # source/class identities; otherwise refuse unmapped evidence.
            import re
            match = re.fullmatch(r'(setUpClass|tearDownClass|setUpModule|tearDownModule) \(([A-Za-z0-9_.]+)\)', test.id())
            if match:
                fixture, identity = match.groups()
                module_name, _, cls = identity.rpartition('.') if 'Class' in fixture else (identity, '', '')
                module = sys.modules.get(module_name)
                path = getattr(module, '__file__', None)
                try:
                    prefix = Path(path).resolve().relative_to(self.root.resolve()).as_posix() + '::'
                    if cls: prefix += cls + '::'
                    affected = [n for n in self.nodes if n.startswith(prefix)]
                except (TypeError, ValueError, OSError): affected = []
                if affected:
                    for known in affected:
                        self.row(known, outcome, 'setup' if fixture.startswith('setUp') else 'teardown', reason)
                    return

        if hasattr(test, 'test_case'):
            if node is None: self.problems.add('unmapped_subtest'); return
            index = self.subindices.get(node, 0); self.subindices[node] = index + 1
            self.row(node + '::subtest_' + str(index), outcome, reason=reason, parent=node)
        else: self.row(node, outcome, reason=reason)


def custom_start(path, names):
    if _ACTIVE is None: return
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    expected = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')}
    if expected != set(names): _ACTIVE.problems.add('incomplete_custom_collection')
    for name in names: _ACTIVE.add(_ACTIVE.node(path, name))


def custom_result(path, name, outcome, reason=None):
    if _ACTIVE is not None: _ACTIVE.row(_ACTIVE.node(path, name), outcome, reason=reason)


def execute(capture, command):
    global _ACTIVE
    _ACTIVE = capture
    sys.modules['_summon_release_collector'] = sys.modules[__name__]
    args = shlex.split(command, posix=True)
    code = 2; completed = False
    original_argv = sys.argv[:]; original_path = sys.path[:]
    original_run = unittest.TextTestRunner.run
    def unit_run(runner, suite):
        capture.unit_start(suite)
        base = runner.resultclass
        class Result(base):
            def stopTest(self, test):
                node = capture.unit_node(test)
                subs = [r for r in capture.subtests if r['parent'] == node]
                if node is not None and node not in capture.rows and subs:
                    capture.row(node, 'failed' if any(r['outcome'] == 'failed' for r in subs) else 'passed')
                super().stopTest(test)
            def addSuccess(self, test): capture.unit_result(test, 'passed'); super().addSuccess(test)
            def addSkip(self, test, reason): capture.unit_result(test, 'skipped', reason); super().addSkip(test, reason)
            def addFailure(self, test, err): capture.unit_result(test, 'failed'); super().addFailure(test, err)
            def addError(self, test, err): capture.unit_result(test, 'errors'); super().addError(test, err)
            def addExpectedFailure(self, test, err): capture.unit_result(test, 'xfailed'); super().addExpectedFailure(test, err)
            def addUnexpectedSuccess(self, test): capture.unit_result(test, 'xpassed'); super().addUnexpectedSuccess(test)
            def addSubTest(self, test, subtest, err):
                capture.unit_result(subtest, 'passed' if err is None else 'failed')
                super().addSubTest(test, subtest, err)
        runner.resultclass = Result
        try: return original_run(runner, suite)
        finally: runner.resultclass = base
    try:
        if args[:3] == ['python', '-m', 'pytest']:
            sys.path.insert(0, str(capture.root))
            import pytest
            capture.value['platform']['pytest_version'] = pytest.__version__
            plugins = [capture]
            try:
                import pytest_subtests.plugin as subtests_plugin
            except ImportError:
                pass
            else:
                import importlib.metadata
                capture.value['platform']['pytest_subtests_version'] = importlib.metadata.version('pytest-subtests')
                plugins.append(subtests_plugin)
            code = int(pytest.main(args[3:], plugins=plugins)); completed = True
        elif args[:3] == ['python', '-m', 'unittest']:
            sys.path.insert(0, str(capture.root))
            unittest.TextTestRunner.run = unit_run
            sys.argv = ['unittest', *args[3:]]
            runpy.run_module('unittest', run_name='__main__'); code = 0; completed = True
        elif len(args) == 2 and args[0] == 'python' and args[1].endswith('.py'):
            path = capture.root / args[1]
            sys.argv = [str(path)]; sys.path.insert(0, str(path.parent))
            runpy.run_path(str(path), run_name='__main__'); code = 0; completed = True
        else: capture.problems.add('unsupported_command')
    except SystemExit as exc:
        code = exc.code if type(exc.code) is int else 0 if exc.code is None else 2
        completed = True
    except BaseException:
        capture.problems.add('collector_execution_error')
    finally:
        unittest.TextTestRunner.run = original_run
        sys.argv[:] = original_argv; sys.path[:] = original_path; _ACTIVE = None
    return capture.finish(code, completed)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=['suite', 'gate'], required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        registry = manifest.REQUIRED_COMMANDS if args.kind == 'suite' else manifest.REQUIRED_GATE_COMMANDS
        command = registry[args.name]
        if args.kind == 'gate' and args.name == 'live_provider': raise ValueError('distinct evidence type')
        output = Path(args.output)
        manifest._assert_external_path(ROOT, output, 'collector output')
        if output.is_symlink(): raise ValueError('unsafe output')
        before = manifest.source_tree_sha256(ROOT)
        git = manifest._git_facts(ROOT)
        capture = Capture(ROOT, args.kind, args.name, command, before, git.get('head'))
        result = execute(capture, command)
        if manifest.source_tree_sha256(ROOT) != before:
            result['completed'] = False; result['problems'].append('source_drift')
            result.pop('artifact_sha256'); result['artifact_sha256'] = contract.digest(result)
        manifest._atomic_write_text(output, json.dumps(result, sort_keys=True) + '\n')
        return 0  # Process success means a complete diagnostic file, not a passing gate.
    except BaseException:
        print('release collector: structured capture unavailable', file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
