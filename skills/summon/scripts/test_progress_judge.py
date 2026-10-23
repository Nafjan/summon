"""Provider-free tests for checkpoint-time progress judgement (Phase 2)."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

import _job_control
import _progress_judge as pj
from _progress_signals import ProgressSignals


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Fingerprints:
    """Scripted fingerprint sequence; the first value is the construction baseline."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, _cwd):
        self.calls += 1
        return self.values.pop(0) if self.values else None


class SyncJudge:
    """Drives the async judge deterministically: measure, wait, then judge."""

    def __init__(self, inner):
        self.inner = inner
        assert inner.wait_idle()

    def judge(self, **kwargs):
        self.inner.prefetch()
        assert self.inner.wait_idle()
        return self.inner.judge(**kwargs)

    def projection(self):
        return self.inner.projection()


def judge(mode="enforce", *fps, checkpoint_ms=60_000):
    return SyncJudge(pj.ProgressJudge(checkpoint_ms=checkpoint_ms, mode=mode,
                                      workspace_cwd="unused",
                                      fingerprint_fn=Fingerprints(*fps)))


# --- decision rules ---------------------------------------------------------

def test_stream_activity_without_loop_extends():
    applied, rec = judge("enforce", "a", "a").judge(stream_active=True, signals=ProgressSignals())
    assert applied is True
    assert rec["reasons"] == ["stream_activity"]
    assert rec["workspace"] == "unchanged"


def test_tool_loop_discounts_stream_activity():
    signals = ProgressSignals(repeat_limit=3)
    for _ in range(3):
        signals.tool_call("Bash", {"command": "npm test"})
    applied, rec = judge("enforce", "a", "a").judge(stream_active=True, signals=signals)
    assert applied is False
    assert rec["legacy_active"] is True and rec["judged_active"] is False
    assert "stream_activity_discounted_tool_loop" in rec["reasons"]


def test_workspace_change_extends_even_without_stream_activity():
    applied, rec = judge("enforce", "a", "b").judge(stream_active=False, signals=None)
    assert applied is True
    assert rec["reasons"] == ["workspace_changed"]


def test_silent_in_flight_tool_extends_within_credit_bound():
    clock = Clock()
    signals = ProgressSignals(clock=clock)
    signals.tool_call("Bash", {"command": "cargo build"})
    signals.tool_started("tu_1")
    clock.now += 100          # 100 s silent build, checkpoint 60 s, credit 180 s
    applied, rec = judge("enforce", None, None).judge(stream_active=False, signals=signals)
    assert applied is True
    assert rec["reasons"] == ["tool_in_flight"]
    assert rec["workspace"] == "unmeasured"


def test_hung_tool_loses_credit_after_bound():
    clock = Clock()
    signals = ProgressSignals(clock=clock)
    signals.tool_started("tu_1")
    clock.now += 60 * pj.MAX_PENDING_CREDIT_CHECKPOINTS + 1
    applied, rec = judge("enforce", "a", "a").judge(stream_active=False, signals=signals)
    assert applied is False
    assert rec["reasons"] == ["tool_in_flight_too_long"]


def test_finished_tool_is_no_longer_in_flight():
    signals = ProgressSignals()
    signals.tool_started("tu_1")
    signals.tool_finished("tu_1")
    assert signals.oldest_pending_ms() is None


def test_no_evidence_is_reported_explicitly():
    applied, rec = judge("enforce", "a", "a").judge(stream_active=False, signals=ProgressSignals())
    assert applied is False
    assert rec["reasons"] == ["no_progress_evidence"]


def test_shadow_mode_applies_legacy_decision_but_records_disagreement():
    signals = ProgressSignals(repeat_limit=2)
    signals.tool_call("Bash", {"c": 1})
    signals.tool_call("Bash", {"c": 1})
    j = judge("shadow", "a", "a")
    applied, rec = j.judge(stream_active=True, signals=signals)
    assert applied is True                     # legacy still governs in shadow
    assert rec["judged_active"] is False
    assert j.projection()["disagreements"] == 1
    assert j.projection()["checkpoints"] == 1


def test_baseline_rolls_forward_so_one_change_counts_once():
    j = judge("enforce", "a", "b", "b")
    assert j.judge(stream_active=False)[0] is True
    assert j.judge(stream_active=False)[0] is False


def test_fingerprint_errors_degrade_to_unmeasured():
    def broken(_cwd):
        raise RuntimeError("git exploded")
    j = SyncJudge(pj.ProgressJudge(checkpoint_ms=1000, mode="enforce", fingerprint_fn=broken))
    applied, rec = j.judge(stream_active=False)
    assert applied is False and rec["workspace"] == "unmeasured"


def test_judge_never_blocks_on_a_slow_measurement():
    import threading
    release = threading.Event()
    calls = []

    def slow(_cwd):
        calls.append(1)
        if len(calls) > 1:
            release.wait(10)
        return "fp"

    j = pj.ProgressJudge(checkpoint_ms=1000, mode="shadow", fingerprint_fn=slow)
    assert j.wait_idle()                      # baseline measured
    assert j.prefetch() is True               # slow measurement now in flight
    started = time.monotonic()
    applied, rec = j.judge(stream_active=True)
    assert time.monotonic() - started < 0.5   # did not wait for git
    assert applied is True and rec["workspace"] == "unmeasured"
    assert j.prefetch() is False              # one measurement at a time
    release.set()
    assert j.wait_idle()


def test_unconsumed_measurement_suppresses_duplicate_prefetch():
    fps = Fingerprints("a", "b", "c")
    j = pj.ProgressJudge(checkpoint_ms=1000, mode="enforce", fingerprint_fn=fps)
    assert j.wait_idle()
    assert j.prefetch() is True and j.wait_idle()
    assert j.prefetch() is False              # "b" not yet consumed by a checkpoint
    assert fps.calls == 2
    applied, rec = j.judge(stream_active=False)
    assert rec["workspace"] == "changed" and applied is True
    assert j.prefetch() is True and j.wait_idle()
    assert fps.calls == 3


@pytest.mark.parametrize("mode", ["off", "bogus"])
def test_judge_requires_an_active_mode(mode):
    with pytest.raises(ValueError):
        pj.ProgressJudge(checkpoint_ms=1000, mode=mode, fingerprint_fn=lambda _c: None)


@pytest.mark.parametrize("raw, expected", [
    ("enforce", "enforce"), ("OFF", "off"), (" shadow ", "shadow"), ("", "shadow"), ("x", "shadow"),
])
def test_mode_from_environment(raw, expected):
    assert pj.mode_from_environment({"SUMMON_PROGRESS_JUDGE": raw}) == expected


# --- workspace fingerprint ---------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    try:
        _git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init")
    return tmp_path


def test_fingerprint_changes_when_an_already_modified_file_changes_again(repo):
    tracked = repo / "a.txt"
    tracked.write_text("1", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a")
    clean = pj.workspace_fingerprint(str(repo))
    tracked.write_text("22", encoding="utf-8")
    first = pj.workspace_fingerprint(str(repo))
    tracked.write_text("333", encoding="utf-8")
    second = pj.workspace_fingerprint(str(repo))
    assert clean and first and second
    assert len({clean, first, second}) == 3


def test_fingerprint_is_stable_without_changes(repo):
    (repo / "u.txt").write_text("x", encoding="utf-8")
    assert pj.workspace_fingerprint(str(repo)) == pj.workspace_fingerprint(str(repo))


def test_fingerprint_from_a_subdirectory_resolves_paths_at_the_repo_root(repo):
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    tracked = sub / "m.py"
    tracked.write_text("1", encoding="utf-8")
    _git(repo, "add", "pkg/inner/m.py")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "m")
    tracked.write_text("22", encoding="utf-8")            # already dirty ...
    first = pj.workspace_fingerprint(str(sub))
    tracked.write_text("333", encoding="utf-8")           # ... and modified again
    second = pj.workspace_fingerprint(str(sub))
    assert first and second and first != second
    # Same state measured from the root and from the subdirectory must agree.
    assert pj.workspace_fingerprint(str(sub)) == pj.workspace_fingerprint(str(repo))


def test_fingerprint_unmeasurable_outside_a_repo_or_on_bad_input(tmp_path):
    assert pj.workspace_fingerprint(str(tmp_path / "missing")) is None
    assert pj.workspace_fingerprint(None) is None

    class Failed:
        returncode = 128
        stdout = b""
    assert pj.workspace_fingerprint(str(tmp_path), runner=lambda *a, **k: Failed()) is None

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 5)
    assert pj.workspace_fingerprint(str(tmp_path), runner=timeout) is None


def test_fingerprint_rejects_malformed_porcelain(tmp_path):
    class Weird:
        returncode = 0
        stdout = b"Z nonsense\0"
    assert pj.workspace_fingerprint(str(tmp_path), runner=lambda *a, **k: Weird()) is None


def test_porcelain_parser_handles_rename_records():
    raw = (b"1 .M N... 100644 100644 100644 abc abc a.txt\0"
           b"2 R. N... 100644 100644 100644 abc abc R100 new name.txt\0old.txt\0"
           b"? untracked dir/\0")
    assert pj._porcelain_paths(raw) == ["a.txt", "new name.txt", "untracked dir/"]


# --- heartbeat publication ----------------------------------------------------

def _runtime(tmp_path, clock):
    return _job_control.RuntimeControl(
        path=str(tmp_path / "c.json"), heartbeat=str(tmp_path / "h.json"),
        job_id="a" * 32, nonce="n" * 32, checkpoint_ms=60_000,
        max_runtime_ms=600_000, clock=clock, wall_clock=time.time)


def test_checkpoint_due_tracks_the_soft_deadline(tmp_path):
    clock = Clock()
    rc = _runtime(tmp_path, clock)
    assert rc.checkpoint_due() is False
    clock.now += 61
    assert rc.checkpoint_due() is True
    rc.checkpoint(active=True)
    assert rc.checkpoint_due() is False


def test_published_progress_is_authenticated_and_sanitized(tmp_path):
    clock = Clock()
    rc = _runtime(tmp_path, clock)
    _applied, record = judge("shadow", "a", "b").judge(stream_active=False)
    record["reasons"].append("<script>")       # hostile extra reason is dropped
    record["secret_extra"] = "sk-live-xyz"     # unknown keys are dropped
    rc.progress = record
    rc.publish({"schema": "summon.liveness/v1", "phase": "generation"}, force=True)
    raw = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))
    body = {k: v for k, v in raw.items() if k != "auth"}
    assert raw["auth"] == _job_control.heartbeat_auth("n" * 32, body)
    public = _job_control.public_heartbeat(raw, "authenticated")
    assert public["progress"]["reasons"] == ["workspace_changed"]
    assert "secret_extra" not in public["progress"]
    assert "sk-live" not in json.dumps(public)


def test_heartbeat_without_progress_is_unchanged(tmp_path):
    rc = _runtime(tmp_path, Clock())
    rc.publish({"schema": "summon.liveness/v1"}, force=True)
    raw = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))
    assert "progress" not in raw
    assert "progress" not in _job_control.public_heartbeat(raw, "authenticated")


# --- end-to-end through the real adaptive read loop ----------------------------

import queue

import _executor
from _liveness import LivenessTracker
from _stream import StreamProcessor


class SteppingClock:
    """Injected ``RuntimeControl`` clock that only moves when the stream is idle.

    Anchored to ``time.monotonic()``: ``_drive_process_loop`` compares
    ``RuntimeControl.hard_deadline``/``deadline`` with the real monotonic clock
    (the production default for ``RuntimeControl.clock``), so an arbitrary
    epoch such as 1000.0 makes the loop see an already-passed hard deadline.
    Reading the clock never advances it: the loop calls it several times per
    iteration (refresh/checkpoint_due/checkpoint/publish/expired), and stepping
    on every call would let ``expired()`` overshoot the soft deadline before
    ``checkpoint()`` ever observed it as due.
    """

    def __init__(self):
        self.now = time.monotonic()

    def __call__(self):
        return self.now


class SteppingQueue(queue.Queue):
    """Reader queue that advances the clock 15 s per idle read, without blocking."""

    def __init__(self, clock: SteppingClock):
        super().__init__()
        self.clock = clock

    def get(self, block=True, timeout=None):
        try:
            return super().get(block=False)
        except queue.Empty:
            self.clock.now += 15.0
            raise


def _looping_codex_run(monkeypatch, tmp_path, mode):
    clock = SteppingClock()
    events = SteppingQueue(clock)
    events.put(("line", json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"))
    for i in range(10):   # identical command, fresh provider ids: renews legacy liveness
        events.put(("line", json.dumps({"type": "item.completed", "item": {
            "id": f"item_{i}", "type": "command_execution", "command": "npm test",
            "exit_code": 1, "status": "failed"}}) + "\n"))

    class Stuck:
        returncode = None
        killed = False
        stdout = None
        stderr = None

        def poll(self):
            return -9 if self.killed else None

        def terminate(self):
            pass

    monkeypatch.setattr(_executor, "_spawn_reader", lambda _p: events)
    monkeypatch.setattr(_executor, "_kill_tree",
                        lambda t: (setattr(t, "killed", True), setattr(t, "returncode", -9)))
    monkeypatch.setattr(_executor, "_drain_to_eof", lambda _q: None)
    monkeypatch.setattr(_executor, "_safe_communicate", lambda _p, timeout=1.0: (None, ""))
    control = _job_control.RuntimeControl(
        path=str(tmp_path / "c.json"), heartbeat=str(tmp_path / "h.json"),
        job_id="b" * 32, nonce="n" * 32, checkpoint_ms=60_000,
        max_runtime_ms=600_000, clock=clock, wall_clock=time.time)
    tracker = LivenessTracker(attempt_id="loop", overall_ms=3_600_000,
                              first_event_ms=60_000, idle_ms=3_600_000)
    emitter = tracker.emitter()
    processor = StreamProcessor(event_observer=emitter, cli="codex")
    processor.progress_judge = pj.ProgressJudge(
        checkpoint_ms=60_000, mode=mode, fingerprint_fn=lambda _c: "same")
    response = _executor._drive_process_loop(
        Stuck(), "codex", 60_000, processor, parse_stream=True,
        liveness=tracker, liveness_emitter=emitter, runtime_control=control)
    return response, processor, control


def test_enforced_judge_stops_extending_a_tool_loop(monkeypatch, tmp_path):
    response, processor, control = _looping_codex_run(monkeypatch, tmp_path, "enforce")
    assert response["timeout"]["stage"] == "adaptive_attention_timeout"
    assert processor.progress_judge.last["reasons"] == [
        "stream_activity_discounted_tool_loop"]
    assert control.auto_extensions == 0
    public = _job_control.public_heartbeat(
        json.loads((tmp_path / "h.json").read_text(encoding="utf-8")), "authenticated")
    assert public["progress"]["judged_active"] is False


def test_shadow_judge_keeps_legacy_behaviour_but_records_disagreement(monkeypatch, tmp_path):
    response, processor, control = _looping_codex_run(monkeypatch, tmp_path, "shadow")
    assert response["timeout"]["stage"] == "adaptive_hard_timeout"
    assert control.auto_extensions >= 1
    assert processor.progress_judge.projection()["disagreements"] >= 1
