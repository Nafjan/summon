"""Independent synthetic legacy migration checks; no provider qualification."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _job_continuation as continuation
import _job_resume as resume
import _launch_qualification as qualification
from test_job_resume import _eligible_source, _launch_evidence


def _legacy_packet(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    # Only the synthetic fixture loses its modern sidecar, representing v1 history.
    Path(continuation.launch_binding_path(root, job_id)).unlink()
    return root, job_id, qualification.read(root, job_id), _launch_evidence()


def _files(root):
    return {p.relative_to(root): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


def test_supported_legacy_revalidation_enables_only_matching_later_validator(tmp_path):
    root, job_id, qualified, evidence = _legacy_packet(tmp_path)
    source_file = Path(continuation.continuation_path(root, job_id))
    original = source_file.read_bytes()
    result = resume.revalidate_source(
        root, job_id, observation=evidence["launch_observation"], qualification=qualified)
    assert result["status"] == "revalidated"
    assert result["provider_contacted"] is False and result["launch_started"] is False
    assert source_file.read_bytes() == original
    context = SimpleNamespace(
        root=root, source_job_id=job_id,
        source=continuation.read_private_source(root, job_id),
        launch_binding=continuation.read_launch_binding(root, job_id),
        launch_qualification=qualified,
    )
    assert context.launch_binding is not None
    assert resume._validate_launch_observation(context, evidence, gate=False) == context.launch_binding
    changed = dict(evidence, launch_observation=dict(
        evidence["launch_observation"], external_cli_version="synthetic-other/2.0"))
    with pytest.raises(resume.ResumeError):
        resume._validate_launch_observation(context, changed, gate=False)


@pytest.mark.parametrize("reason", ["forged_auth", "revoked"], ids=['p001_case_001', 'p001_case_002'])
def test_legacy_revalidation_refuses_before_any_sidecar_change(tmp_path, reason):
    root, job_id, qualified, evidence = _legacy_packet(tmp_path)
    if reason == "forged_auth":
        qualified = dict(qualified, auth="0" * 64)
        expected = "launch_qualification_auth_failed"
    else:
        qualification.revoke(root, job_id, qualified["revocation_id"])
        expected = "resume_launch_qualification_revoked"
    before = _files(root)
    with pytest.raises(resume.ResumeError) as refused:
        resume.revalidate_source(
            root, job_id, observation=evidence["launch_observation"], qualification=qualified)
    assert refused.value.kind == expected
    after = _files(root)
    assert set(before) == set(after), "refused revalidation published a sidecar"
    assert all(before[p] == after[p] for p in before), "refused revalidation changed stored bytes"
