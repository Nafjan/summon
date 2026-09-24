"""Projected capability changes invalidate snapshots without rebinding entities."""
import copy
import pytest
import _workspace_view as view
from test_workspace_view import Fixture, scope, render


def projected_inputs(operator):
    task = view._opaque(operator, 'task', 'main-1')
    return {
        'operator_messages': {'available': True, 'reason': None, 'targets': [
            {'id': 'operator_target_first', 'task_id': task, 'label': 'Task 1', 'available': True}]},
        'workspace_details': {'available': True, 'reason': None, 'targets': [
            {'id': 'detail_first', 'task_id': task, 'source_kind': 'workspace_evidence',
             'state': 'completed', 'label': 'Workspace evidence', 'revision': 1,
             'body_available': False}]},
    }


@pytest.mark.parametrize('change', ['message-availability', 'message-recipient',
    'detail-availability', 'detail-revision', 'detail-body'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_returned_capability_change_invalidates_snapshot_and_old_expectation(change):
    fixture, operator = Fixture(), scope(audience='operator')
    values = projected_inputs(operator)
    before_state = copy.deepcopy(fixture.state)
    initial = render(fixture, scope=operator, **values)
    changed = copy.deepcopy(values)
    if change == 'message-availability':
        changed['operator_messages'] = {'available': False, 'targets': [], 'reason': 'message_scope_required'}
    elif change == 'message-recipient':
        changed['operator_messages']['targets'][0].update(
            id='operator_target_second', task_id=view._opaque(operator, 'task', 'side-1'), label='Task 3')
    elif change == 'detail-availability':
        changed['workspace_details'] = {'available': False, 'targets': [], 'reason': 'detail_scope_required'}
    elif change == 'detail-revision':
        changed['workspace_details']['targets'][0]['revision'] += 1
    else:
        changed['workspace_details']['targets'][0]['body_available'] = True
    copied = copy.deepcopy(changed)
    refreshed = render(fixture, scope=operator, **changed)
    field = 'operator_messages' if change.startswith('message') else 'details'
    assert refreshed[field] != initial[field]
    assert refreshed['snapshot'] != initial['snapshot']
    with pytest.raises(view.WorkspaceViewError, match='^stale_snapshot$'):
        render(fixture, scope=operator, expected_snapshot=initial['snapshot'], **changed)
    assert refreshed['workspace_id'] == initial['workspace_id']
    assert refreshed['tasks'] == initial['tasks']
    assert refreshed['deliveries'] == initial['deliveries']
    assert fixture.state == before_state and changed == copied


def test_equivalent_validated_capabilities_keep_snapshot_and_detached_output_stable():
    fixture, operator = Fixture(), scope(audience='operator')
    values = projected_inputs(operator)
    initial = render(fixture, scope=operator, **values)
    reordered = {key: dict(reversed(list(value.items()))) for key, value in values.items()}
    again = render(fixture, scope=operator, expected_snapshot=initial['snapshot'], **reordered)
    assert again == initial
    values['operator_messages']['targets'][0]['label'] = 'Changed caller input'
    values['workspace_details']['targets'][0]['body_available'] = True
    assert initial['operator_messages']['targets'][0]['label'] == 'Task 1'
    assert initial['details']['targets'][0]['body_available'] is False
