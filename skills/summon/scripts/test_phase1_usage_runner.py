from __future__ import annotations

import io
import json
import sys
import threading

import pytest

import _usage_live
import _usage_runner as runner


class Sink:
    def __init__(self):
        self.value = bytearray()

    def write(self, value):
        self.value.extend(value)
        return len(value)

    def flush(self):
        return None

    def close(self):
        return None


class Process:
    def __init__(self, stdout=b"", stderr=b"", code=0):
        self.stdin = Sink()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.code = code

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


class HangingProcess(Process):
    def __init__(self, stdout=b"", stderr=b""):
        super().__init__(stdout=stdout, stderr=stderr, code=None)


class PhasedStream:
    def __init__(self, first: bytes, after_kill: bytes, released: threading.Event):
        self._first = first
        self._after = after_kill
        self._released = released
        self._phase = 0

    def read1(self, _size):
        if self._phase == 0:
            self._phase = 1
            return self._first
        if self._phase == 1:
            self._phase = 2
            self._released.wait(timeout=2)
            return self._after
        return b""


class PipeProcess(HangingProcess):
    def __init__(self, stdout, stderr):
        self.stdin = Sink()
        self.stdout = stdout
        self.stderr = stderr
        self.code = None


class BlockingAfterFirstStream:
    def __init__(self, first: bytes, release: threading.Event):
        self._first = first
        self._release = release
        self._sent = False

    def read1(self, _size):
        if not self._sent:
            self._sent = True
            return self._first
        self._release.wait(timeout=5)
        return b""


def plan():
    return _usage_live.codex_request_plan(
        allow_account_usage_read=True, dry_run=False)


def test_runner_rejects_any_plan_mutation_before_spawn():
    value = plan()
    changed = json.loads(json.dumps(value))
    changed["requests"][2]["method"] = "account/write"
    calls = []
    with pytest.raises(runner.UsageRunnerError):
        runner.run_plan(changed, which=lambda _name: calls.append(True) or "codex")
    assert calls == []


def test_execute_uses_headless_shared_spawn_flags_and_preserves_jsonl(monkeypatch):
    process = Process(stdout=b'{"ok":true}\n')
    captured = {}
    attached = []
    closed = []

    def factory(args, **kwargs):
        captured.update({"args": args, "kwargs": kwargs})
        return process

    monkeypatch.setattr(runner, "popen_flags", lambda: {"fixture_hidden": True})
    result = runner._execute(
        ["codex", "app-server"], input_bytes=b'{"id":1}\n', timeout_ms=1000,
        max_stdout=1024, max_stderr=1024, popen_factory=factory,
        attach_job=lambda item: attached.append(item) or True,
        close_job=lambda item: closed.append(item) or True)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["fixture_hidden"] is True
    assert bytes(process.stdin.value) == b'{"id":1}\n'
    assert result["stdout"] == b'{"ok":true}\n'
    assert result["provider_contacted"] is None
    assert attached == [process]
    assert closed == [process]


def test_job_object_hook_failures_do_not_mask_bounded_result():
    process = Process(stdout=b'{"ok":true}\n')

    def fail(_item):
        raise OSError("job object unavailable")

    result = runner._execute(
        ["codex", "app-server"], input_bytes=b"", timeout_ms=1000,
        max_stdout=1024, max_stderr=1024,
        popen_factory=lambda *_a, **_k: process,
        attach_job=fail, close_job=fail)
    assert result["exit_code"] == 0
    assert result["stdout"] == b'{"ok":true}\n'


def test_execute_bounds_stdout_and_terminates_only_owned_child():
    process = Process(stdout=b"x" * 2048, code=0)
    killed = []
    result = runner._execute(
        ["codex", "app-server"], input_bytes=b"{}\n", timeout_ms=1000,
        max_stdout=32, max_stderr=32,
        popen_factory=lambda *_a, **_k: process,
        kill_tree=lambda item: killed.append(item))
    assert len(result["stdout"]) == 32
    assert result["exit_code"] == 1
    # The process had already exited, so no tree was killed merely for a
    # post-exit overflow; the bounded contract still fails closed.
    assert killed == []


def test_execute_enforces_per_response_timeout_and_kills_only_owned_child():
    process = HangingProcess(stdout=b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    killed = []
    clock = iter([0.0, 0.0, 0.006, 0.017, 0.018])

    def kill(item):
        killed.append(item)
        item.code = 124

    result = runner._execute(
        ["codex", "app-server"], input_bytes=b"{}\n", timeout_ms=100,
        command_timeout_ms=10, progress_probe=runner._response_progress,
        max_stdout=1024, max_stderr=1024,
        popen_factory=lambda *_a, **_k: process,
        monotonic=lambda: next(clock), sleep=lambda _value: None, kill_tree=kill)
    assert killed == [process]
    assert result["exit_code"] == 124
    assert result["error_kind"] == "usage_refresh_command_timeout"
    assert result["provider_contacted"] is None


def test_only_reviewed_response_ids_renew_command_liveness():
    assert runner._response_progress(
        b'{"jsonrpc":"2.0","method":"notice"}\nnot-json\n') == 0
    assert runner._response_progress(
        b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
        b'{"jsonrpc":"2.0","id":9,"result":{}}\n') == 1


def test_real_pipe_progress_is_visible_and_completion_reaps_owned_child():
    script = (
        "import json,time\n"
        "for value in (1,2,3):\n"
        " print(json.dumps({'jsonrpc':'2.0','id':value,'result':{}}), flush=True)\n"
        " time.sleep(0.4)\n"
        "time.sleep(5)\n"
    )
    killed = []

    def kill(process):
        killed.append(process)
        process.kill()

    result = runner._execute(
        [sys.executable, "-u", "-c", script], input_bytes=b"",
        timeout_ms=3000, command_timeout_ms=700,
        progress_probe=runner._response_progress, completion_target=3,
        max_stdout=4096, max_stderr=4096, kill_tree=kill)
    assert result["exit_code"] == 0
    assert result["error_kind"] is None
    assert runner._response_progress(result["stdout"]) == 3
    assert killed and 700 < result["elapsed_ms"] < 3000


def test_real_silent_pipe_hits_command_timeout_and_reaps_owned_child():
    killed = []

    def kill(process):
        killed.append(process)
        process.kill()

    result = runner._execute(
        [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
        input_bytes=b"", timeout_ms=1500, command_timeout_ms=300,
        progress_probe=runner._response_progress, completion_target=3,
        max_stdout=4096, max_stderr=4096, kill_tree=kill)
    assert result["exit_code"] == 124
    assert result["error_kind"] == "usage_refresh_command_timeout"
    assert killed and result["elapsed_ms"] < 1500


def test_local_jsonrpc_errors_do_not_prove_provider_contact():
    wire = b"\n".join([
        b'{"jsonrpc":"2.0","id":2,"error":{"code":-32601}}',
        b'{"jsonrpc":"2.0","id":3,"error":{"code":-32601}}',
    ]) + b"\n"
    assert runner._contact_from_stdout(wire) is None


@pytest.mark.parametrize("late_stream", ["stdout", "stderr"])
def test_late_pipe_overflow_beats_logical_completion(late_stream):
    wire = b"\n".join([
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
    ]) + b"\n"
    released = threading.Event()
    if late_stream == "stdout":
        stdout = PhasedStream(wire, b"overflow", released)
        stderr = io.BytesIO(b"")
        stdout_limit = len(wire)
        stderr_limit = 1024
    else:
        stdout = io.BytesIO(wire)
        stderr = PhasedStream(b".", b"x" * 16, released)
        stdout_limit = 1024
        stderr_limit = 8
    process = PipeProcess(stdout, stderr)

    def kill(item):
        item.code = 1
        released.set()

    result = runner._execute(
        ["codex", "app-server"], input_bytes=b"", timeout_ms=1000,
        command_timeout_ms=500, progress_probe=runner._response_progress,
        completion_target=3, max_stdout=stdout_limit, max_stderr=stderr_limit,
        popen_factory=lambda *_a, **_k: process, kill_tree=kill)
    assert result["exit_code"] == 1
    assert result["error_kind"] == "backend_execution_failed"


def test_completed_exchange_fails_if_owned_child_cannot_be_reaped():
    wire = b"\n".join([
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
    ]) + b"\n"
    process = HangingProcess(stdout=wire)
    result = runner._execute(
        ["codex", "app-server"], input_bytes=b"", timeout_ms=1000,
        command_timeout_ms=500, progress_probe=runner._response_progress,
        completion_target=3, max_stdout=1024, max_stderr=1024,
        popen_factory=lambda *_a, **_k: process, kill_tree=lambda _item: None)
    assert process.poll() is None
    assert result["exit_code"] == 1
    assert result["error_kind"] == "backend_execution_failed"


def test_completed_exchange_fails_while_pipe_drain_remains_live():
    wire = b"\n".join([
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
    ]) + b"\n"
    release = threading.Event()
    process = PipeProcess(BlockingAfterFirstStream(wire, release), io.BytesIO(b""))

    def kill(item):
        item.code = 1

    try:
        result = runner._execute(
            ["codex", "app-server"], input_bytes=b"", timeout_ms=1000,
            command_timeout_ms=500, progress_probe=runner._response_progress,
            completion_target=3, max_stdout=1024, max_stderr=1024,
            popen_factory=lambda *_a, **_k: process, kill_tree=kill)
        assert result["exit_code"] == 1
        assert result["error_kind"] == "backend_execution_failed"
    finally:
        release.set()


def test_run_plan_returns_version_and_exact_request_sequence(monkeypatch):
    wire = b'\n'.join([
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
    ]) + b'\n'
    version_process = Process(stdout=b"codex-cli 0.147.0\n")
    app_process = Process(stdout=wire)
    processes = [version_process, app_process]
    commands = []

    def factory(args, **_kwargs):
        commands.append(args)
        return processes.pop(0)

    monkeypatch.setattr(runner, "popen_flags", lambda: {})
    result = runner.run_plan(
        plan(), which=lambda _name: "codex", popen_factory=factory)
    assert commands == [["codex", "--version"], ["codex", "app-server"]]
    assert result["cli_version"] == _usage_live.CODEX_SCHEMA_CLI_VERSION
    assert result["exit_code"] == 0
    assert result["provider_contacted"] is True
    sent = [json.loads(line) for line in bytes(app_process.stdin.value).splitlines()]
    assert [item["method"] for item in sent] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read"]


def test_version_mismatch_refuses_before_account_process(monkeypatch):
    version_process = Process(stdout=b"codex-cli 9.9.9\n")
    calls = []

    def factory(args, **_kwargs):
        calls.append(args)
        return version_process

    monkeypatch.setattr(runner, "popen_flags", lambda: {})
    result = runner.run_plan(plan(), which=lambda _name: "codex", popen_factory=factory)
    assert calls == [["codex", "--version"]]
    assert result["provider_contacted"] is False
    assert result["cli_version"] == "codex-cli 9.9.9"


def test_runner_missing_binary_is_provider_inert():
    result = runner.run_plan(plan(), which=lambda _name: None)
    assert result["provider_contacted"] is False
    assert result["exit_code"] == 1
    assert result["stdout"] == b""
