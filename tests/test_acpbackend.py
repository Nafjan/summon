#!/usr/bin/env python3
"""ACP backend tests — protocol client, permission policy, teardown, fallback.

Uses tests/fixtures/fake_acp_agent.py as the far end of the wire: a real
subprocess speaking NDJSON JSON-RPC, so the client is exercised end-to-end
(spawn, framing, permission round-trip, timeout, kill) without a vendor CLI.

Run: python -m pytest tests/test_acpbackend.py -q   (or plain: python tests/test_acpbackend.py)
"""
from __future__ import annotations

import os
import json
import stat
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "skills", "summon", "scripts")
sys.path.insert(0, SCRIPTS)

import _acpbackend  # noqa: E402
import _executor  # noqa: E402
from _builder import AgentInvocation, supports_acp  # noqa: E402

FIXTURE = os.path.join(REPO, "tests", "fixtures", "fake_acp_agent.py")


# --- helpers ------------------------------------------------------------------

def _patch_launch(monkeypatch=None):
    """Point the ACP spawn at the fixture instead of a vendor CLI, and skip the
    PATH capability probe (probe behavior has its own tests below). Restores on
    teardown when a pytest monkeypatch is supplied; otherwise the caller must
    restore via the returned tuple."""
    orig_launch = _executor._resolve_launch
    orig_probe = _acpbackend._probe_acp

    def fake_launch(command, args):
        return sys.executable, [FIXTURE, *args]

    _executor._resolve_launch = fake_launch
    _acpbackend._probe_acp = lambda cli: None
    return orig_launch, orig_probe


def _restore(patched):
    _executor._resolve_launch, _acpbackend._probe_acp = patched


def _inv(mode_cli="gemini", permission="yolo", model=None, prompt="say hi"):
    return AgentInvocation(cli=mode_cli, prompt=prompt, cwd=os.getcwd(),
                           permission=permission, transport="acp", model=model)


def _call(mode="happy", timeout_ms=30000, **inv_kw):
    patched = _patch_launch()
    try:
        # mode is passed to the fixture as the token after the acp flag: the
        # fake launcher appends *args, and ACP_ARGV["gemini"] == ["--acp"], so
        # inject the mode by rewriting ACP_ARGV for this call.
        orig_argv = dict(_acpbackend.ACP_ARGV)
        _acpbackend.ACP_ARGV["gemini"] = ["acp", mode]
        try:
            return _acpbackend.call(_inv(**inv_kw), timeout_ms)
        finally:
            _acpbackend.ACP_ARGV.update(orig_argv)
    finally:
        _restore(patched)


# --- protocol client ----------------------------------------------------------

def test_happy_path_envelope_shape():
    resp = _call("happy")
    assert resp["status"] == "success", resp
    assert resp["exit_code"] == 0
    assert "Hello from the fake agent." in resp["result"]
    assert resp["session_id"] == "fake-session-1"
    assert "tool_call fake read" in (resp.get("_debug_raw") or "")


def test_full_executor_envelope_and_resume_lane():
    """execute_agent(transport='acp') runs the enrich/stamp pipeline; the ACP
    session id must NOT leak into the resume lane (premortem T1)."""
    orig_argv = dict(_acpbackend.ACP_ARGV)
    _acpbackend.ACP_ARGV["gemini"] = ["acp", "happy"]
    patched = _patch_launch()
    try:
        resp = _executor.execute_agent(_inv(), timeout_ms=30000)
    finally:
        _acpbackend.ACP_ARGV.update(orig_argv)
        _restore(patched)
    assert resp["status"] == "success", resp.get("error")
    assert resp["transport"] == "acp"
    assert resp["report_ok"] is True          # report/verdict pipeline ran
    assert resp["resume"] == {"cli": "gemini", "session_id": None}
    assert resp["acp"] == {"session_id": "fake-session-1"}


def test_model_pinning_best_effort():
    resp = _call("happy", model="fake-model")
    assert resp.get("model_targeted") == "fake-model"
    resp = _call("happy", model="not-a-real-model")
    assert "model_targeted" not in resp
    assert any("model" in w for w in resp.get("warnings", []))


def test_permission_yolo_allows_once_never_always():
    resp = _call("permission-exec", permission="yolo")
    raw = resp.get("_debug_raw") or ""
    assert "-> allow" in raw and "'allow_once'" in raw, raw
    assert "allow_always" not in raw.split("-> allow")[1].splitlines()[0]


def test_permission_readonly_rejects_execute():
    resp = _call("permission-exec", permission="read-only")
    raw = resp.get("_debug_raw") or ""
    assert "-> reject" in raw and "'reject_once'" in raw, raw


def test_permission_readonly_allows_read():
    resp = _call("permission-read", permission="read-only")
    raw = resp.get("_debug_raw") or ""
    assert "-> allow" in raw, raw


def test_permission_unclassifiable_options_cancel_turn():
    """Only allow_always is offered for an in-tier kind. Selecting it would
    grant standing permission beyond one dispatch, and the ACP option list has
    NO ordering guarantee, so picking by position can grant authority. The only
    fail-closed answer is the spec's cancelled outcome."""
    resp = _call("permission-no-once", permission="yolo")
    assert '"outcome": "cancelled"' in (resp.get("result") or ""), resp.get("result")
    assert "no safely classifiable option" in (resp.get("_debug_raw") or "")
    assert "allow_always" not in (resp.get("result") or "")


def test_permission_empty_options_cancel_turn():
    """An empty options list has no valid optionId at all; inventing one
    ('reject_once') answers with a value the agent never offered."""
    resp = _call("permission-empty", permission="yolo")
    assert '"outcome": "cancelled"' in (resp.get("result") or ""), resp.get("result")


def test_executor_acp_redacts_and_drops_debug_raw():
    """The ACP executor branch honors the same artifact contract as the
    subprocess path: _debug_raw never ships inside the envelope, and backend
    output is secret-redacted before any field persists (cross-vendor review:
    it used to pass through untouched)."""
    secret = "Authorization: Bearer live-secret"
    orig = _acpbackend.call

    def fake_call(inv, timeout_ms):
        return {"status": "error", "exit_code": 1, "result": None,
                "error": f"backend failed: {secret}",
                "_debug_raw": f"noise\n{secret}\nmore"}

    _acpbackend.call = fake_call
    try:
        resp = _executor.execute_agent(_inv(), timeout_ms=5000)
    finally:
        _acpbackend.call = orig
    assert "_debug_raw" not in resp
    assert "live-secret" not in json.dumps(resp), json.dumps(resp)[:400]


def test_prompt_response_without_stopreason_is_structural_error():
    """A session/prompt response missing stopReason is a protocol violation,
    not an empty success (the default used to be end_turn)."""
    resp = _call("no-stopreason")
    assert resp["status"] == "error", resp
    assert "stopReason" in (resp.get("error") or ""), resp.get("error")


def test_resume_over_acp_is_refused_not_silent_fresh_session():
    """An ACP session id is telemetry, not a resume handle; --resume with the
    ACP transport must refuse loudly instead of starting fresh work."""
    inv = AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                          permission="yolo", transport="acp",
                          resume_id="some-prior-session")
    resp = _acpbackend.call(inv, 5000)
    assert resp["status"] == "error"
    assert "resume is not supported over the ACP transport" in resp["error"]


def test_turn_state_accumulation_is_bounded():
    """A noisy or hostile child must not exhaust dispatcher memory mid-turn."""
    t = _acpbackend._TurnState()
    for _ in range(30):
        t.add_text("x" * 10_000)
    text, _, _ = t.snapshot()
    assert len(text) <= t._MAX_TEXT_CHARS + 100  # cap + marker
    assert "truncated" in text
    for i in range(t._MAX_DIAG_LINES + 50):
        t.add_diag(f"d{i}")
    _, diag, _ = t.snapshot()
    assert len(diag) == t._MAX_DIAG_LINES + 1  # cap + truncation marker
    assert "truncated" in diag[-1]


def test_wrong_protocol_version_is_structural_error():
    """An agent negotiating a different ACP version must not be driven as v1."""
    resp = _call("wrong-version")
    assert resp["status"] == "error"
    assert "protocol" in (resp.get("error") or "")


def test_missing_session_id_is_structural_error():
    """session/new without a usable sessionId is a protocol violation, not a
    session named None flowing into every later call."""
    resp = _call("bad-session")
    assert resp["status"] == "error"
    assert "sessionId" in (resp.get("error") or "")


def test_acp_child_env_comes_from_builder_minus_superseded_channels():
    """The ACP child must run under the builder-installed identity (kimi's
    isolated credential profile, etc.) -- never the ambient account's full
    profile -- minus the env channel ACP supersedes (GEMINI_SYSTEM_MD, which is
    prepended to the prompt on this transport instead)."""
    import _builder
    orig_build = _builder.build_invocation_args
    orig_popen = _acpbackend.subprocess.Popen
    captured = {}
    _builder.build_invocation_args = lambda inv: (
        "gemini", ["--acp"], {"KIMI_CODE_HOME": "/iso", "GEMINI_SYSTEM_MD": "/md"})

    def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return orig_popen(cmd, **kw)

    patched = _patch_launch()
    _acpbackend.subprocess.Popen = spy
    try:
        resp = _acpbackend.call(_inv(), 30000)
    finally:
        _builder.build_invocation_args = orig_build
        _acpbackend.subprocess.Popen = orig_popen
        _restore(patched)
    assert resp["status"] == "success", resp.get("error")
    assert captured["env"]["KIMI_CODE_HOME"] == "/iso"
    assert "GEMINI_SYSTEM_MD" not in captured["env"]


def test_acp_refuses_sub_yolo_tiers():
    """Reactive-only enforcement is not containment: read-only and safe-edit
    over ACP are refused, not warned (re-review finding, 2026-07-31)."""
    for tier in ("read-only", "safe-edit"):
        resp = _executor.execute_agent(_inv(permission=tier), timeout_ms=5000)
        assert resp["status"] == "error", (tier, resp)
        assert "cannot enforce" in (resp.get("error") or ""), (tier, resp.get("error"))


def test_fallback_skipped_for_sub_yolo_invocations():
    """A safe-edit subprocess failure must not attempt ACP recovery at all --
    the fallback would run the same prompt at a tier ACP cannot enforce."""
    import run_subagent as rs
    calls = []
    primary = {"status": "error", "exit_code": 1, "cli": "gemini",
               "error": "backend exploded mid-stream",
               "normalization_reason": "backend internal error"}
    orig_exec = rs.execute_agent
    rs.execute_agent = lambda *a, **kw: (calls.append(1) or dict(primary))

    class _Args:
        agent = "x"
        timeout = 5000
        debug_dir = None
        max_tool_output_bytes = None
        _receipt = None
        retries = 0
        no_acp_fallback = False
        gate_with = None

    try:
        inv = AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                              permission="safe-edit")
        rs._dispatch_with_retries(inv, _Args())
    finally:
        rs.execute_agent = orig_exec
    assert len(calls) == 1, calls


def test_fallback_gate_denial_keeps_primary_spend():
    """A gate that denies the ACP recovery must not erase the primary attempt's
    cost: the dispatch happened either way."""
    import run_subagent as rs
    primary = {"status": "error", "exit_code": 1, "cli": "gemini",
               "error": "backend exploded mid-stream",
               "normalization_reason": "backend internal error",
               "usage": {"total_tokens": 100}, "cost_usd": 0.5, "attempts": 0}
    orig_exec = rs.execute_agent
    orig_regate = rs._regate_or_none
    rs.execute_agent = lambda *a, **kw: dict(primary)
    rs._regate_or_none = lambda *a, **kw: {"approved": False, "reason": "gate said no"}

    class _Args:
        agent = "reviewer"
        timeout = 5000
        debug_dir = None
        max_tool_output_bytes = None
        _receipt = None
        retries = 0
        no_acp_fallback = False
        gate_with = "some-gate"

    try:
        inv = AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                              permission="yolo")
        out = rs._dispatch_with_retries(inv, _Args())
    finally:
        rs.execute_agent = orig_exec
        rs._regate_or_none = orig_regate
    assert out.get("cost_usd") == 0.5, out
    assert (out.get("usage") or {}).get("total_tokens") == 100, out


def test_refusal_maps_to_error():
    resp = _call("refusal")
    assert resp["status"] == "error"
    assert "refusal" in (resp.get("error") or "")


def test_malformed_lines_tolerated():
    resp = _call("malformed")
    assert resp["status"] == "success"
    assert "non-JSON stdout line skipped" in (resp.get("_debug_raw") or "")


def test_timeout_sends_cancel_and_returns_124():
    start = time.monotonic()
    resp = _call("slow", timeout_ms=2500)
    elapsed = time.monotonic() - start
    assert resp["exit_code"] == 124, resp
    assert resp["status"] == "error"
    # 2.5s budget + teardown grace, never the fixture's 3600s sleep.
    assert elapsed < 30, elapsed


def test_post_turn_teardown_does_not_wait_for_exit():
    """Premortem T4: a long-lived agent that answered its turn must not cost
    the wall-clock timeout."""
    start = time.monotonic()
    resp = _call("lingering", timeout_ms=120000)
    elapsed = time.monotonic() - start
    assert resp["status"] == "success"
    assert elapsed < 30, elapsed


def test_supports_acp_gating():
    assert supports_acp("gemini") and supports_acp("kimi") and supports_acp("cursor-agent")
    assert not supports_acp("claude") and not supports_acp("agy")
    resp = _executor.execute_agent(
        AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(),
                        transport="acp"), timeout_ms=5000)
    assert resp["exit_code"] == 2 and "no acp transport" in resp["error"]


def test_unknown_transport_rejected():
    resp = _executor.execute_agent(
        AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                        transport="carrier-pigeon"), timeout_ms=5000)
    assert resp["exit_code"] == 2 and "unknown transport" in resp["error"]


def test_oversized_prompt_routes_over_acp():
    """A subprocess dispatch whose argv would exceed the OS limit is routed to
    the ACP transport instead of erroring (the executor's own routing, not the
    retry-loop fallback)."""
    orig_argv = dict(_acpbackend.ACP_ARGV)
    _acpbackend.ACP_ARGV["gemini"] = ["acp", "happy"]
    patched = _patch_launch()
    orig_measure = _executor.argv_length_error
    _executor.argv_length_error = lambda *a, **kw: "simulated argv overflow"
    try:
        resp = _executor.execute_agent(
            AgentInvocation(cli="gemini", prompt="x" * 40000, cwd=os.getcwd(),
                            permission="yolo"),
            timeout_ms=30000)
    finally:
        _executor.argv_length_error = orig_measure
        _acpbackend.ACP_ARGV.update(orig_argv)
        _restore(patched)
    assert resp["status"] == "success", resp.get("error")
    assert resp["transport"] == "acp"
    assert resp["fallback"]["reason"] == "argv-length"
    assert any("command-line length limit" in w for w in resp.get("warnings", []))


# --- capability probe (premortem T2) -------------------------------------------

def _fake_cli_dir(with_acp_token: bool) -> str:
    d = tempfile.mkdtemp(prefix="summon-acp-probe-")
    if os.name == "nt":
        body = "@echo " + ("usage: fake --acp flag" if with_acp_token
                           else "usage: fake [options] no protocol here")
        open(os.path.join(d, "gemini.cmd"), "w").write(body + "\n")
    else:
        body = "#!/bin/sh\necho " + ("usage: fake --acp flag" if with_acp_token
                                     else "usage: fake [options] no protocol here")
        p = os.path.join(d, "gemini")
        open(p, "w").write(body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return d


def test_probe_rejects_cli_without_acp_support():
    _acpbackend._reset_probe_cache()
    d = _fake_cli_dir(with_acp_token=False)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    try:
        verdict = _acpbackend._probe_acp("gemini")
    finally:
        os.environ["PATH"] = old_path
        _acpbackend._reset_probe_cache()
    assert verdict and "does not advertise ACP" in verdict


def test_probe_passes_cli_with_acp_support():
    _acpbackend._reset_probe_cache()
    d = _fake_cli_dir(with_acp_token=True)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old_path
    try:
        verdict = _acpbackend._probe_acp("gemini")
    finally:
        os.environ["PATH"] = old_path
        _acpbackend._reset_probe_cache()
    assert verdict is None


def test_probe_missing_cli():
    _acpbackend._reset_probe_cache()
    try:
        assert "CLI not found" in _acpbackend._probe_acp("summon-nonexistent-cli")
    finally:
        _acpbackend._reset_probe_cache()


# --- fallback predicate + wiring (premortem T3) ---------------------------------

def _result(**kw):
    base = {"status": "error", "exit_code": 1, "cli": "gemini",
            "error": "stream exploded", "normalization_reason": "", "result": ""}
    base.update(kw)
    return base


def test_fallback_predicate():
    import run_subagent as rs
    assert rs._acp_fallback_worthy(_result(exit_code=124))                       # timeout
    assert rs._acp_fallback_worthy(_result(normalization_reason="timed out; partial"))
    assert rs._acp_fallback_worthy(_result())                                    # stream/shape loss
    assert not rs._acp_fallback_worthy(_result(status="success"))
    assert not rs._acp_fallback_worthy(_result(status="blocked"))
    assert not rs._acp_fallback_worthy(_result(exit_code=127, error="CLI not found: gemini"))
    assert not rs._acp_fallback_worthy(_result(error="please log in to continue"))
    assert not rs._acp_fallback_worthy(_result(error="agy cannot enforce read-only"))
    assert not rs._acp_fallback_worthy(_result(
        error="the assembled command line is 40000 characters, over the Windows limit of 32767"))


def _args_ns(**kw):
    base = dict(timeout=5000, debug_dir=None, retries=0, gate_with=None,
                no_acp_fallback=False, agent=None, _receipt=None,
                max_tool_output_bytes=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_fallback_recovers_and_accounts_spend():
    import run_subagent as rs
    orig = rs.execute_agent
    calls = []

    def fake_execute(inv, **kw):
        calls.append(inv.transport)
        if inv.transport == "acp":
            return {"result": "recovered", "status": "success", "exit_code": 0,
                    "cli": inv.cli, "cost_usd": 0.50}
        return _result(cost_usd=0.25)

    rs.execute_agent = fake_execute
    try:
        out = rs._dispatch_with_retries(
            AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                            permission="yolo"), _args_ns())
    finally:
        rs.execute_agent = orig
    assert calls == ["subprocess", "acp"]
    assert out["status"] == "success"
    assert out["attempts"] == 2
    assert out["cost_usd"] == 0.75                      # both dispatches billed
    assert out["fallback"]["from"] == "subprocess"
    assert out["fallback"]["primary_status"] == "error"
    assert any("--no-acp-fallback" in w for w in out.get("warnings", []))


def test_fallback_failed_recovery_keeps_original():
    import run_subagent as rs
    orig = rs.execute_agent
    rs.execute_agent = lambda inv, **kw: (
        _result(error="acp also failed") if inv.transport == "acp" else _result())
    try:
        out = rs._dispatch_with_retries(
            AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                            permission="yolo"), _args_ns())
    finally:
        rs.execute_agent = orig
    assert out["status"] == "error"
    assert out["error"] == "stream exploded"            # original, richer envelope
    assert out["fallback"]["to"] == "acp"
    assert out["fallback"]["error"] == "acp also failed"
    assert out["attempts"] == 2


def test_fallback_skipped_for_structural_failures():
    import run_subagent as rs
    orig = rs.execute_agent
    calls = []

    def fake_execute(inv, **kw):
        calls.append(inv.transport)
        return _result(exit_code=127, error="CLI not found: gemini")

    rs.execute_agent = fake_execute
    try:
        out = rs._dispatch_with_retries(
            AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                            permission="yolo"), _args_ns())
    finally:
        rs.execute_agent = orig
    assert calls == ["subprocess"]                      # no wasted recovery dispatch
    assert "fallback" not in out


def test_no_acp_fallback_kill_switch():
    import run_subagent as rs
    orig = rs.execute_agent
    calls = []
    rs.execute_agent = lambda inv, **kw: (calls.append(inv.transport), _result())[1]
    old_env = os.environ.get("SUMMON_ACP_FALLBACK")
    os.environ["SUMMON_ACP_FALLBACK"] = "0"
    try:
        out = rs._dispatch_with_retries(
            AgentInvocation(cli="gemini", prompt="x", cwd=os.getcwd(),
                            permission="yolo"), _args_ns())
    finally:
        rs.execute_agent = orig
        if old_env is None:
            os.environ.pop("SUMMON_ACP_FALLBACK", None)
        else:
            os.environ["SUMMON_ACP_FALLBACK"] = old_env
    assert calls == ["subprocess"]
    assert "fallback" not in out


# --- transport resolution (opt-in) ----------------------------------------------

def _write_agent(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".md", prefix="summon-agent-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_transport_frontmatter_and_flag():
    import run_subagent as rs
    agent = _write_agent("---\nrun-agent: gemini\ntransport: acp\n---\nDo things.\n")
    assert rs._transport_for_dispatch(agent, None) == "acp"
    assert rs._transport_for_dispatch(agent, "subprocess") == "subprocess"
    agent2 = _write_agent("---\nrun-agent: gemini\n---\nDo things.\n")
    assert rs._transport_for_dispatch(agent2, None) == "subprocess"
    assert rs._transport_for_dispatch(None, None) == "subprocess"
    bad = _write_agent("---\nrun-agent: gemini\ntransport: pigeon\n---\nDo things.\n")
    try:
        rs._transport_for_dispatch(bad, None)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "invalid transport" in str(e)


if __name__ == "__main__":
    # Plain-assert runner (no pytest required), matching tests/test_install.py.
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
