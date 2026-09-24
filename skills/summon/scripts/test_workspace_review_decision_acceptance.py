"""Pure Review/Decision reader acceptance; no admission or producer provenance.

Assessment is the canonical Review; Decision is a workspace_next_lane_selected
payload. The existing Fixture supplies synthetic versioned events, not a live
conductor. It covers canonical event/record intake and identity preservation;
durable conductor admission and historical producer equivalence are separate.
"""
from pathlib import Path
import copy
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _workspace_state as state
from test_workspace_protocol import assessment, ref
from test_workspace_state import Fixture, decision_payload


def _event(entity):
    fixture = Fixture()
    if entity == 'review':
        fixture.register(ref(), task_id='main-1')
        record = assessment()
        record.update(task_id='main-1', lane_id='main-1')
        payload = {'assessment': record}
        kind = 'workspace_assessed'
    else:
        fixture.assess('main-1', 'main-result')
        payload = decision_payload()
        kind = 'workspace_next_lane_selected'
    return fixture, fixture.event(kind, payload)


@pytest.mark.parametrize('entity', ['review', 'decision'], ids=['p001_case_001', 'p001_case_002'])
@pytest.mark.parametrize('mutation', ['missing_protocol', 'future_protocol', 'unknown_field', 'authority_field'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_exact_entity_event_refuses_unknown_format_without_state_mutation(entity, mutation):
    fixture, event = _event(entity)
    # Review's strict record is independently versioned; Decision inherits its
    # enclosing workspace event protocol and has an exact payload field set.
    versioned = event['payload']['assessment'] if entity == 'review' else event
    fields = event['payload']['assessment'] if entity == 'review' else event['payload']
    if mutation == 'missing_protocol': versioned.pop('protocol')
    elif mutation == 'future_protocol': versioned['protocol'] = 'summon.workspace/v999'
    elif mutation == 'unknown_field': fields['future_field'] = 'synthetic'
    else: fields['approved'] = True
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError):
        state.apply_event(fixture.state, event, fixture.view)
    assert fixture.state == before


@pytest.mark.parametrize('entity', ['review', 'decision'], ids=['p003_case_001', 'p003_case_002'])
@pytest.mark.parametrize('conflicting', [False, True], ids=['identical_id', 'conflicting_id'])
def test_entity_identity_cannot_be_reused_with_a_fresh_operation(entity, conflicting):
    fixture, event = _event(entity)
    fixture.state = state.apply_event(fixture.state, event, fixture.view)
    duplicate = copy.deepcopy(event)
    duplicate['operation_key'] = 'fresh-operation'
    duplicate['expected_revision'] = fixture.state['revision']
    if conflicting:
        if entity == 'review': duplicate['payload']['assessment']['reason'] = 'Changed review content'
        else: duplicate['payload']['to_task_id'] = 'side-1'
    before = copy.deepcopy(fixture.state)
    with pytest.raises(state.WorkspaceStateError):
        state.apply_event(fixture.state, duplicate, fixture.view)
    assert fixture.state == before
    # Exact original event replay is still supported, with no extra identity.
    assert state.apply_event(fixture.state, event, fixture.view) == before
    assert len(fixture.state['assessments' if entity == 'review' else 'decisions']) == 1




