"""Public chat packet refusals must be bounded and free of private input."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_subagent as dispatcher


@pytest.mark.parametrize("case", ["missing", "invalid_json", "invalid_utf8", "oversized"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_public_chat_packet_input_refuses_without_private_output(tmp_path, monkeypatch, capsys, case):
    packet = tmp_path / "synthetic-private-evidence.json"
    marker = "synthetic-private-packet-body"
    if case == "invalid_json":
        packet.write_text('{"' + marker, encoding="utf-8")
    elif case == "invalid_utf8":
        packet.write_bytes(b"\xff" + marker.encode())
    elif case == "oversized":
        packet.write_bytes(b"x" * (512 * 1024 + 1))
    rooms = tmp_path / "rooms"

    def forbidden(*args, **kwargs):
        pytest.fail("chat packet validation started a child process", pytrace=False)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("SUMMON_TELEMETRY", "0")
    monkeypatch.setattr(sys, "argv", [
        "summon", "chat", "revalidate", "synthetic-room", "worker",
        "--evidence-file", str(packet), "--conversation-dir", str(rooms), "--json",
    ])
    escaped_type = None
    try:
        dispatcher.main()
    except SystemExit as completed:
        code = completed.code
    except Exception as exc:
        escaped_type = type(exc).__name__
    else:
        pytest.fail("public packet validation did not return a CLI status", pytrace=False)
    if escaped_type is not None:
        pytest.fail("public packet validation leaked an exception type: " + escaped_type, pytrace=False)
    captured = capsys.readouterr()
    public = captured.out + captured.err
    safe_output = str(tmp_path) not in public and tmp_path.name not in public and marker not in public
    assert safe_output, "public packet refusal exposed private path or packet contents"
    assert code == 1
    report = json.loads(captured.out)
    assert report["status"] == "blocked" and isinstance(report.get("error_kind"), str)
    assert not rooms.exists(), "invalid packet input created room state"
