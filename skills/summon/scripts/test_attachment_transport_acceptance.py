"""Actual fake-child receipt of complete long private-file payloads."""

import hashlib
import json
import os
import sys

import pytest

import _builder
from _builder import AgentInvocation
from _executor import execute_agent
from _zcode import ZCodeTarget


@pytest.mark.parametrize("resume", [False, True], ids=['p001_case_001', 'p001_case_002'])
@pytest.mark.parametrize("fits", [False, True], ids=['p002_case_001', 'p002_case_002'])
def test_long_attachment_exact_bytes_and_budget_before_fake_child(tmp_path, monkeypatch, resume, fits):
    receiver = tmp_path / "receiver.py"
    observed = tmp_path / "observed.json"
    receiver.write_text(
        'import hashlib,json,sys\n'
        'from pathlib import Path\n'
        'args=sys.argv[1:]\n'
        'payload=Path(args[args.index("--attach")+1]).read_bytes()\n'
        'Path(__file__).with_name("observed.json").write_text(json.dumps({'
        '"bytes":len(payload),"sha256":hashlib.sha256(payload).hexdigest(),'
        '"resume":"--resume" in args}),encoding="utf-8")\n'
        'print(json.dumps({"response":"STATUS: DONE\\nSUMMARY: synthetic transport receipt\\nHANDOFF: none",'
        '"sessionId":"sess_fixture_transport"}))\n',
        encoding="utf-8",
    )
    prompt = '\ufeff' + ('Unicode λ 🙂; "quoted"\r\nnext line\n' * 2400)
    system_context = "synthetic system context"
    expected_text = (prompt + _builder._RESUME_REMINDER if resume else
                     f"[System Context]\n{system_context}\n\n[User Prompt]\n{prompt}")
    expected = expected_text.encode("utf-8")
    assert len(expected) > 64 * 1024
    target = ZCodeTarget(command=sys.executable, prefix_args=(str(receiver),), source="fixture")
    monkeypatch.setattr("_zcode.resolve_zcode_cli", lambda: target)
    # Only the literal fake receiver can execute. The yolo fields satisfy the
    # adapter's existing fixture prerequisites; no provider/config is accessed
    # and no claim about an installed ZCode permission boundary follows.
    invocation = AgentInvocation(
        cli="zcode", prompt=prompt, system_context=system_context, cwd=str(tmp_path),
        permission="yolo", isolated_lane=True, allow_tool_credentials=True,
        resume_id="sess_fixture_transport" if resume else None,
        # Successful fresh/resume turns exercise the adapter-owned default
        # capability; the over-budget branch supplies an explicit one-byte
        # smaller ceiling to prove preflight refusal.
        transport_capability=(
            None if fits else {
                "kind": "private_attachment",
                "platform": os.name,
                "max_attachment_bytes": len(expected) - 1,
            }
        ),
    )
    attachments = []
    original_write = _builder._write_zcode_attachment

    def tracked_write(text):
        from pathlib import Path
        path = original_write(text)
        attachments.append(Path(path))
        return path

    monkeypatch.setattr(_builder, "_write_zcode_attachment", tracked_write)
    result = execute_agent(invocation, timeout_ms=10000)
    assert len(attachments) == 1
    assert all(not path.exists() for path in attachments)
    if fits:
        assert observed.exists(), "exact-budget payload did not reach the fake receiver"
        actual = json.loads(observed.read_text(encoding="utf-8"))
        assert actual == {"bytes": len(expected),
                          "sha256": hashlib.sha256(expected).hexdigest(), "resume": resume}
        assert result.get("provider_contacted") is True  # the synthetic child boundary only
        assert result["transport_budget"]["status"] == "accepted"
        assert result["transport_budget"]["operation"] == ("resume" if resume else "fresh")
    else:
        assert not observed.exists()
        assert result["provider_contacted"] is False
        assert result["error_kind"] == "transport_budget_exceeded"
        assert result["attempt_status"] == "not_run"
        assert "fallback" not in result
