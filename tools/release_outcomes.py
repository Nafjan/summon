"""Source-bound release outcome contract; no console text is release authority."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

SCHEMA = 1
COLLECTOR = 'tools/release_collect.py'
POLICY = 'tools/release_skip_policy.json'
CATEGORIES = ('passed', 'skipped', 'failed', 'errors', 'xfailed', 'xpassed', 'deselected')
PLATFORM_KEYS = {'sys_platform', 'os_name', 'machine', 'python_version', 'pytest_version', 'pytest_subtests_version'}
MAX_CASES = 100000
MAX_BYTES = 32 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}')
NODE = re.compile(r'(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py(?:::[A-Za-z_][A-Za-z0-9_.-]*)+')
REDACTED = 'Unreviewed skip reason.'

GATE_ARTIFACT_SCHEMA = 1
GATE_STATUSES = frozenset({'pass', 'blocked'})
GATE_REDACTED = 'gate_diagnostic_redacted'
GATE_DIAGNOSTIC_CODES = frozenset({
    'gate_command_failed', 'live_provider_evidence_missing',
    'live_provider_evidence_invalid', GATE_REDACTED,
})
GATE_EVIDENCE_FILE = 'redacted-live-provider-receipt.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('structured JSON exceeds limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate structured JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def policy(root):
    value = read_json(Path(root) / POLICY)
    if not isinstance(value, dict) or set(value) != {'schema', 'reviews'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('invalid skip review policy')
    entries = value['reviews']
    if not isinstance(entries, list) or len(entries) > MAX_CASES:
        raise ValueError('invalid skip review policy')
    seen = set()
    for entry in entries:
        required = {'review_id', 'kind', 'name', 'command', 'nodeid', 'reason',
                    'platform', 'disposition', 'review_ref'}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError('invalid skip review entry')
        if (entry['kind'] not in {'suite', 'gate'} or not safe_node(entry['nodeid'])
            or not isinstance(entry['review_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', entry['review_id'])
            or entry['review_id'] in seen
            or entry['disposition'] not in {'not_applicable_here', 'blocking'}
            or not safe_reason(entry['reason'])
            or not isinstance(entry['platform'], dict)
            or 'sys_platform' not in entry['platform'] or not set(entry['platform']) <= PLATFORM_KEYS
            or any(not safe_token(v) for v in entry['platform'].values())
            or not isinstance(entry['name'], str) or not safe_token(entry['name'])
            or not isinstance(entry['command'], str)
            or not isinstance(entry['review_ref'], str)
            or not re.fullmatch(r'docs/[A-Za-z0-9_./-]+\.md#[A-Za-z0-9_-]+', entry['review_ref'])
            or '..' in entry['review_ref']):
            raise ValueError('invalid skip review entry')
        ref, anchor = entry['review_ref'].split('#', 1)
        path = Path(root) / ref
        if path.is_symlink() or not path.is_file() or not re.search(r'(?m)^#{1,6} ' + re.escape(anchor) + r'\s*$', path.read_text(encoding='utf-8')):
            raise ValueError('skip review decision is unavailable')
        seen.add(entry['review_id'])
    return value


def safe_token(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value))


def safe_node(value):
    return isinstance(value, str) and len(value) <= 1000 and bool(NODE.fullmatch(value))


def safe_reason(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and bool(re.fullmatch(r"[A-Za-z0-9 .,()'_-]+", value)))


def review_for(root, binding, nodeid, reason_hash, platform):
    matches = [r for r in policy(root)['reviews']
        if all(r[k] == binding[k] for k in ('kind', 'name', 'command'))
        and r['nodeid'] == nodeid
        and hashlib.sha256(r['reason'].encode('utf-8')).hexdigest() == reason_hash
        and all(platform.get(k) == v for k, v in r['platform'].items())]
    if len(matches) > 1:
        raise ValueError('ambiguous skip review policy')
    return matches[0] if matches else None


def execution_template(kind, name):
    return ['python', COLLECTOR, '--kind', kind, '--name', name, '--output', '<private-output>']


def validate_outcomes(value, *, root, kind, name, command, source_hash, git_head,
                      require_pass=True):
    """One semantic validator for intake and final checks, suites and gates."""
    required = {'schema', 'kind', 'name', 'command', 'execution_template',
        'source_tree_sha256', 'git_head', 'producer_sha256', 'collector_sha256',
        'skip_policy_sha256', 'output_sha256', 'platform', 'completed', 'exit_code', 'collected_nodeids',
        'cases', 'subtests', 'counts', 'problems', 'artifact_sha256'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('structured outcome fields are missing or unknown')
    expected = {'schema': SCHEMA, 'kind': kind, 'name': name, 'command': command,
        'execution_template': execution_template(kind, name),
        'source_tree_sha256': source_hash, 'git_head': git_head,
        'producer_sha256': file_hash(Path(root) / 'tools/release_gates.py'),
        'collector_sha256': file_hash(Path(root) / COLLECTOR),
        'skip_policy_sha256': file_hash(Path(root) / POLICY)}
    if type(value.get('schema')) is not int or any(value.get(k) != v for k, v in expected.items()):
        raise ValueError('structured outcome binding does not match source')
    payload = dict(value); payload.pop('artifact_sha256')
    if value['artifact_sha256'] != digest(payload):
        raise ValueError('structured outcome digest mismatch')
    if not isinstance(value['output_sha256'], str) or not HEX.fullmatch(value['output_sha256']):
        raise ValueError('invalid structured output binding')
    runtime = value['platform']
    if (not isinstance(runtime, dict) or set(runtime) != PLATFORM_KEYS
        or any(not safe_token(v) for v in runtime.values())):
        raise ValueError('invalid structured runtime')
    if type(value['completed']) is not bool or (value['exit_code'] is not None
            and type(value['exit_code']) is not int):
        raise ValueError('invalid structured completion')
    nodes = value['collected_nodeids']
    if (not isinstance(nodes, list) or len(nodes) > MAX_CASES
        or any(not safe_node(n) for n in nodes) or sorted(set(nodes)) != nodes):
        raise ValueError('invalid collected test identities')
    counts = value['counts']
    if (not isinstance(counts, dict) or set(counts) != set(CATEGORIES)
        or any(type(v) is not int or not 0 <= v <= MAX_CASES for v in counts.values())):
        raise ValueError('invalid structured outcome counts')
    cases = value['cases']; subs = value['subtests']
    if not isinstance(cases, list) or not isinstance(subs, list) or len(cases) + len(subs) > MAX_CASES:
        raise ValueError('invalid structured cases')
    seen = set(); actual = dict.fromkeys(CATEGORIES, 0); blocked = False
    binding = {'kind': kind, 'name': name, 'command': command}
    for is_subtest, row in (
        [(False, row) for row in cases] + [(True, row) for row in subs]
    ):
        fields = {'nodeid', 'outcome', 'phase'}
        if not isinstance(row, dict): raise ValueError('invalid case record')
        if row.get('outcome') == 'skipped':
            fields |= {'reason_code', 'reason', 'reason_sha256', 'review_id'}
        if is_subtest: fields.add('parent')
        if set(row) != fields or not safe_node(row['nodeid']) or row['nodeid'] in seen:
            raise ValueError('invalid or duplicate case identity')
        seen.add(row['nodeid'])
        if row['outcome'] not in CATEGORIES or row['phase'] not in {'setup', 'call', 'teardown', 'collection'}:
            raise ValueError('unknown terminal outcome')
        if is_subtest:
            if row['parent'] not in nodes: raise ValueError('foreign subtest parent')
        elif row['nodeid'] not in nodes:
            raise ValueError('foreign case identity')
        else: actual[row['outcome']] += 1
        if row['outcome'] == 'skipped':
            if not isinstance(row['reason_sha256'], str) or not HEX.fullmatch(row['reason_sha256']):
                raise ValueError('invalid skip reason binding')
            review = review_for(root, binding, row['nodeid'], row['reason_sha256'], runtime)
            if review is None:
                if (row['reason_code'], row['reason'], row['review_id']) != ('unreviewed', REDACTED, None):
                    raise ValueError('unreviewed private reason must be redacted')
                blocked = True
            else:
                if (row['reason_code'], row['reason'], row['review_id']) != ('reviewed_literal', review['reason'], review['review_id']):
                    raise ValueError('skip review binding mismatch')
                blocked |= review['disposition'] != 'not_applicable_here'
        elif row['outcome'] != 'passed': blocked = True
    if actual != counts or set(r['nodeid'] for r in cases) != set(nodes):
        raise ValueError('terminal cases do not reconcile with collection')
    problems = value['problems']
    if not isinstance(problems, list) or any(not safe_token(p) for p in problems):
        raise ValueError('invalid collection diagnostics')
    if require_pass and (blocked or problems or not value['completed']
            or value['exit_code'] != 0 or counts['passed'] == 0
            or any(counts[k] for k in CATEGORIES if k not in {'passed', 'skipped'})):
        raise ValueError('structured outcomes do not qualify release evidence')
    return f"{counts['passed']}/{counts['passed'] + counts['skipped']}"


def validate_gate_artifact(value, *, root, name, status, command, source_hash, git_head):
    """Validate the complete public gate envelope before intake or final use."""
    required = {'schema', 'gate', 'status', 'command', 'source_tree_sha256',
                'git_head', 'producer', 'output_sha256', 'artifact_sha256'}
    optional = {'error_kind', 'detail', 'evidence_file'}
    if name == 'live_provider':
        optional.add('evidence_sha256')
        if status == 'pass':
            required.add('evidence_sha256')
    else:
        required.update({'test_count', 'outcomes'})
    if (not isinstance(value, dict) or not required <= set(value)
            or not set(value) <= required | optional):
        raise ValueError('gate artifact fields are missing or unknown')
    expected = {'gate': name, 'status': status, 'command': command,
                'source_tree_sha256': source_hash, 'git_head': git_head,
                'producer': 'tools/release_gates.py'}
    if (type(value['schema']) is not int or value['schema'] != GATE_ARTIFACT_SCHEMA
            or any(type(value[key]) is not str or value[key] != expected_value
                   for key, expected_value in expected.items())
            or value['status'] not in GATE_STATUSES):
        raise ValueError('gate artifact binding does not match source')
    for key in ('source_tree_sha256', 'output_sha256', 'artifact_sha256', 'evidence_sha256'):
        if key in value and (type(value[key]) is not str or not HEX.fullmatch(value[key])):
            raise ValueError('invalid gate artifact digest')
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value['git_head']):
        raise ValueError('invalid gate artifact Git binding')
    if 'error_kind' in value and (type(value['error_kind']) is not str
            or value['error_kind'] not in GATE_DIAGNOSTIC_CODES):
        raise ValueError('invalid gate diagnostic code')
    if 'detail' in value and (type(value['detail']) is not str or value['detail'] != GATE_REDACTED):
        raise ValueError('invalid gate diagnostic detail')
    if 'evidence_file' in value and (type(value['evidence_file']) is not str
            or value['evidence_file'] != GATE_EVIDENCE_FILE):
        raise ValueError('invalid gate evidence label')
    if name != 'live_provider':
        count = validate_outcomes(value['outcomes'], root=root, kind='gate', name=name,
            command=command, source_hash=source_hash, git_head=git_head,
            require_pass=status == 'pass')
        if (type(value['test_count']) is not str or value['test_count'] != count
                or value['output_sha256'] != value['outcomes']['output_sha256']):
            raise ValueError('gate artifact outcome projection differs')
    payload = dict(value)
    payload.pop('artifact_sha256')
    if value['artifact_sha256'] != digest(payload):
        raise ValueError('gate artifact digest mismatch')
