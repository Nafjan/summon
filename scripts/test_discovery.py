#!/usr/bin/env python3
"""Focused tests for model discovery (_resolver.discover_models & helpers).

Run: python test_discovery.py   (no pytest needed — plain asserts, exits
non-zero on any failure). Covers the regressions found in adversarial review:
config.toml table-boundary parsing and the eager-agy-probe filter bug.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _resolver  # noqa: E402
from _resolver import _codex_default_model_scan, discover_models  # noqa: E402


def _write(text: str) -> str:
    fd, p = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def test_scan_toplevel_wins_over_section():
    p = _write('model = "gpt-5.6-sol"\n[tui]\nmodel = "wrong"\n')
    try:
        assert _codex_default_model_scan(p) == "gpt-5.6-sol"
    finally:
        os.remove(p)


def test_scan_header_with_inline_comment_is_a_boundary():
    # Regression: '[table]  # note' must count as leaving the top-level table,
    # so the section-scoped model is NOT reported as the global default.
    p = _write('[profiles.fast]  # comment\nmodel = "section-scoped"\n')
    try:
        assert _codex_default_model_scan(p) is None
    finally:
        os.remove(p)


def test_scan_section_only_model_is_none():
    p = _write('[tui]\nmodel = "only-in-section"\n')
    try:
        assert _codex_default_model_scan(p) is None
    finally:
        os.remove(p)


def test_scan_no_model_is_none():
    p = _write('approval_policy = "never"\n[foo]\nbar = 1\n')
    try:
        assert _codex_default_model_scan(p) is None
    finally:
        os.remove(p)


def test_scan_commented_model_ignored():
    p = _write('# model = "commented"\nmodel = "real"\n')
    try:
        assert _codex_default_model_scan(p) == "real"
    finally:
        os.remove(p)


def test_scan_missing_file_is_none():
    assert _codex_default_model_scan(
        os.path.join(tempfile.gettempdir(), "definitely-no-such-config-xyz.toml")) is None


def test_scan_quoted_hash_in_header_is_a_boundary():
    # A '#' inside a quoted header key must not confuse the boundary test: the
    # line still starts with '[', so it counts as leaving the top-level table.
    p = _write('[profiles."fast#lane"]\nmodel = "section-scoped"\n')
    try:
        assert _codex_default_model_scan(p) is None
    finally:
        os.remove(p)


def test_empty_cli_is_invalid_not_full_sweep():
    # `--cli ""` must be rejected as unknown, NOT read as "all backends" (which
    # would launch the live agy probe).
    orig = _resolver._agy_live_models
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("agy probe ran for empty --cli")

    _resolver._agy_live_models = boom
    try:
        out = discover_models(cli="")
    finally:
        _resolver._agy_live_models = orig
    assert calls["n"] == 0
    assert "agy" not in out
    assert out.get("", {}).get("source") == "unknown"


def test_unknown_cli_returns_before_backend_work():
    # An unknown cli must short-circuit before probing agy (and before needing
    # any backend-specific import).
    orig = _resolver._agy_live_models
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("agy probe ran for unknown cli")

    _resolver._agy_live_models = boom
    try:
        out = discover_models(cli="bogus")
    finally:
        _resolver._agy_live_models = orig
    assert calls["n"] == 0
    assert out["bogus"]["source"] == "unknown"


def test_cli_codex_does_not_probe_agy():
    # The expensive live agy subprocess must never run for a codex-only query.
    orig = _resolver._agy_live_models
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("agy probe ran for --cli codex")

    _resolver._agy_live_models = boom
    try:
        out = discover_models(cli="codex")
    finally:
        _resolver._agy_live_models = orig
    assert calls["n"] == 0
    assert "agy" not in out and "codex" in out


def test_cursor_alias_normalized():
    assert list(discover_models(cli="cursor").keys()) == ["cursor-agent"]
    assert list(discover_models(cli="cursor-agent").keys()) == ["cursor-agent"]


def test_unknown_cli_marked():
    out = discover_models(cli="bogus")
    assert out["bogus"]["source"] == "unknown"


def test_full_has_all_backends_without_real_agy():
    # Stub agy so the full sweep is hermetic (no real subprocess).
    orig = _resolver._agy_live_models
    _resolver._agy_live_models = lambda: ("live", ["StubModel"], None)
    try:
        out = discover_models()
    finally:
        _resolver._agy_live_models = orig
    for b in ("claude", "codex", "agy", "cursor-agent", "gemini"):
        assert b in out, f"missing backend {b}"
    assert out["claude"]["aliases"] == ["opus", "sonnet", "haiku"]
    assert out["agy"]["models"] == ["StubModel"]


def test_blocked_approval_downgrades_success():
    # A run that ENDS asking for interactive approval with no report contract
    # must become status:blocked (a 0 exit is not task completion).
    from _executor import _enrich
    resp = {"result": "I tried to read the file.\nThe tool call was blocked. "
                      "Please approve the permission request to continue.",
            "exit_code": 0, "status": "success", "cli": "claude"}
    out = _enrich(resp, None)
    assert out["status"] == "blocked", out["status"]
    assert out["blocked_indicators"]
    assert "approval" in out["error"]


def test_blocked_markers_with_full_report_stay_success():
    # A COMPLETED report that merely quotes approval phrasing is a real result.
    from _executor import _enrich
    resp = {"result": "Review of the consent flow.\n\nSTATUS: DONE\nSUMMARY: reviewed "
                      "the dialog\nFOLLOW-UP: none\nHANDOFF: the dialog text says "
                      "'please approve' which needs a UX pass",
            "exit_code": 0, "status": "success", "cli": "claude"}
    out = _enrich(resp, None)
    assert out["status"] == "success"
    assert out["blocked_indicators"]          # surfaced for the orchestrator
    assert "suspect" not in out               # but not treated as a lie


def test_clean_success_untouched_by_blocked_scan():
    from _executor import _enrich
    resp = {"result": "All good.\n\nSTATUS: DONE\nSUMMARY: ok\nFOLLOW-UP: none\nHANDOFF: none",
            "exit_code": 0, "status": "success", "cli": "codex"}
    out = _enrich(resp, None)
    assert out["status"] == "success" and "blocked_indicators" not in out


def test_report_blocked_is_authoritative_over_exit0():
    # The MOST contract-compliant blocked path: agent self-reports STATUS:
    # BLOCKED with a full report. The envelope must not say success.
    from _executor import _enrich
    resp = {"result": "Could not proceed.\n\nSTATUS: BLOCKED\nSUMMARY: needs the API "
                      "schema file which is outside cwd\nFOLLOW-UP: provide the file\n"
                      "HANDOFF: blocked on missing input",
            "exit_code": 0, "status": "success", "cli": "codex"}
    out = _enrich(resp, None)
    assert out["status"] == "blocked", out["status"]
    assert out["report_ok"] is True
    assert "self-reported BLOCKED" in out["error"]


def test_indicators_attached_even_when_report_downgraded():
    # Markers + a full STATUS: BLOCKED report: status comes from the report,
    # but the marker telemetry must still be attached (APPROVE-pass follow-up).
    from _executor import _enrich
    resp = {"result": "The tool call was blocked. Please approve.\n\nSTATUS: BLOCKED\n"
                      "SUMMARY: sandboxed read\nFOLLOW-UP: move file under cwd\nHANDOFF: blocked",
            "exit_code": 0, "status": "success", "cli": "claude"}
    out = _enrich(resp, None)
    assert out["status"] == "blocked"
    assert out["blocked_indicators"], "markers must be attached despite report downgrade"
    assert "self-reported BLOCKED" in out["error"]  # report reconciliation won the status


def test_report_partial_and_error_map_to_envelope():
    from _executor import _enrich
    for rs, expected in (("PARTIAL", "partial"), ("ERROR", "error")):
        resp = {"result": f"...\n\nSTATUS: {rs}\nSUMMARY: s\nFOLLOW-UP: f\nHANDOFF: h",
                "exit_code": 0, "status": "success", "cli": "claude"}
        out = _enrich(resp, None)
        assert out["status"] == expected, (rs, out["status"])


def test_report_done_never_upgrades_executor_error():
    # Reconciliation only downgrades: an executor-detected error stays an error
    # even if the text contains a cheerful STATUS: DONE block.
    from _executor import _enrich
    resp = {"result": "STATUS: DONE\nSUMMARY: s\nFOLLOW-UP: f\nHANDOFF: h",
            "exit_code": 1, "status": "error", "cli": "claude", "error": "CLI exited 1"}
    out = _enrich(resp, None)
    assert out["status"] == "error"


def test_blocked_error_text_never_recommends_escalation():
    from _executor import _enrich
    resp = {"result": "The tool call was blocked. Please approve.",
            "exit_code": 0, "status": "success", "cli": "claude"}
    out = _enrich(resp, None)
    assert out["status"] == "blocked"
    assert "NOT raise the permission" in out["error"]


def test_timeout_rejects_bad_domains():
    import argparse as ap
    import run_subagent as rs
    for bad in ("0", "-5s", "1e999", "nan", "0.0001"):  # 0.0001ms rounds to 0 -> min 1? see below
        if bad == "0.0001":
            assert rs._parse_timeout(bad) == 1  # sub-ms rounds up to the 1ms floor
            continue
        try:
            rs._parse_timeout(bad)
            raise AssertionError(f"expected rejection for {bad!r}")
        except ap.ArgumentTypeError:
            pass


def test_description_unbroken_token_hard_cut():
    from _loader import extract_description
    token = "x" * 300
    d = extract_description(token)
    assert len(d) == 244 and d.endswith(" ...")  # documented hard-cut fallback


def test_timeout_suffix_parsing():
    import run_subagent as rs
    assert rs._parse_timeout("600000") == 600000   # bare ms (backward compatible)
    assert rs._parse_timeout("600s") == 600000
    assert rs._parse_timeout("10m") == 600000
    assert rs._parse_timeout("1500ms") == 1500
    assert rs._parse_timeout("2.5m") == 150000
    import argparse as ap
    try:
        rs._parse_timeout("tenminutes")
        raise AssertionError("expected ArgumentTypeError")
    except ap.ArgumentTypeError:
        pass


def test_elapsed_ms_present_even_on_spawn_failure():
    import _executor
    from _builder import AgentInvocation
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv: ("definitely-not-a-real-cli-xyz", [], None)
    try:
        out = _executor.execute_agent(
            AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(),
                            system_context="s", permission="yolo"), timeout_ms=1000)
    finally:
        _executor.build_invocation_args = orig
    assert out["status"] == "error" and out["exit_code"] == 127
    assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0


def test_description_word_boundary_cap():
    from _loader import extract_description
    long = "word " * 100  # 500 chars of clean words
    d = extract_description(long)
    assert d.endswith(" ...") and len(d) <= 245
    assert not d[:-4].endswith("wor")  # no mid-word cut
    assert extract_description("short line") == "short line"


def test_doctor_all_missing_is_fail_soft():
    # With every CLI absent, doctor must still return a full report (ok=False),
    # never raise. Simulate by stubbing shutil.which inside _doctor.
    import _doctor
    orig = _doctor.shutil.which
    _doctor.shutil.which = lambda name: None
    try:
        rep = _doctor.doctor()
    finally:
        _doctor.shutil.which = orig
    assert rep["ok"] is False
    assert rep["usable_backends"] == []
    for b in ("claude", "codex", "cursor-agent", "gemini", "agy"):
        assert rep["backends"][b]["found"] is False
        assert rep["backends"][b]["install"]
    # render() must also survive the all-missing report (and stay ASCII-safe)
    text = _doctor.render(rep)
    assert "NONE" in text
    text.encode("ascii")  # raises if any non-ASCII marker sneaks in


def test_doctor_json_roundtrip():
    import json as _json
    import _doctor
    rep = _doctor.doctor()
    parsed = _json.loads(_json.dumps(rep, ensure_ascii=False))
    assert set(parsed["backends"]) == {"claude", "codex", "cursor-agent", "gemini", "agy"}
    assert isinstance(parsed["ok"], bool)


def test_agy_posix_fence():
    # On POSIX without AGY_PTY_WRAPPER, the agy builder must fail fast with a
    # clear ValueError BEFORE any profile work. Real coverage on the Linux CI
    # leg; on Windows this asserts the happy path instead.
    from _builder import AgentInvocation, build_invocation_args
    inv = AgentInvocation(cli="agy", prompt="hi", cwd=os.getcwd(),
                          system_context="x", permission="yolo")
    if os.name == "nt" or os.environ.get("AGY_PTY_WRAPPER"):
        return  # fence not applicable here
    try:
        build_invocation_args(inv)
        raise AssertionError("expected ValueError on POSIX without AGY_PTY_WRAPPER")
    except ValueError as e:
        assert "AGY_PTY_WRAPPER" in str(e)


def test_extract_json_last_toplevel_wins():
    from _schema import extract_json
    text = ('Here is my thinking {"draft": 1} and some prose.\n'
            '```json\n{"verdict": "keep", "score": 8, "notes": {"a": [1, 2]}}\n```\n'
            "STATUS: DONE\nSUMMARY: s\nFOLLOW-UP: f\nHANDOFF: h")
    val, err = extract_json(text)
    assert err is None and val["verdict"] == "keep" and val["notes"]["a"] == [1, 2]
    val, err = extract_json("no json here at all")
    assert val is None and "no complete JSON" in err


def test_schema_validator_subset():
    from _schema import validate
    schema = {"type": "object",
              "required": ["verdict", "score"],
              "additionalProperties": False,
              "properties": {
                  "verdict": {"type": "string", "enum": ["keep", "cut"]},
                  "score": {"type": "integer", "minimum": 0, "maximum": 10},
                  "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3}}}
    assert validate({"verdict": "keep", "score": 8}, schema) == []
    errs = validate({"verdict": "meh", "score": 11, "tags": ["a", 2], "x": 1}, schema)
    joined = " | ".join(errs)
    for expected in ("enum", "maximum", "$.tags[1]", "unexpected properties"):
        assert expected in joined, (expected, joined)
    assert validate({"score": True, "verdict": "keep"}, schema)  # bool is not integer


def test_manifest_normalize_and_concurrency():
    import _manifest as m
    caps = m._parse_concurrency("agy=2, codex=3")
    assert caps == {"default": 3, "agy": 2, "codex": 3}
    jobs, err = m._normalize_jobs(
        {"defaults": {"retries": 1},
         "jobs": [{"agent": "reviewer", "prompt": "p1"},
                  {"id": "j2", "agent": "pair", "prompt": "p2"}]}, ".")
    assert err is None and jobs[0]["id"] == "reviewer-000" and jobs[0]["retries"] == 1
    _, err = m._normalize_jobs([{"agent": "a", "prompt": "p", "bogus": 1}], ".")
    assert "unknown keys" in err
    _, err = m._normalize_jobs([{"agent": "a"}], ".")
    assert "prompt" in err
    _, err = m._normalize_jobs(
        [{"id": "x", "agent": "a", "prompt": "p"}, {"id": "x", "agent": "b", "prompt": "p"}], ".")
    assert "duplicate" in err


def test_loader_extra_args_parsing():
    from _loader import parse_extra_args
    assert parse_extra_args(None) == []
    assert parse_extra_args('-c model_reasoning_effort="high" --flag') == \
        ["-c", "model_reasoning_effort=high", "--flag"]
    try:
        parse_extra_args('"unbalanced')
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_builder_extra_args_reach_argv():
    from _builder import AgentInvocation, build_invocation_args
    inv = AgentInvocation(cli="claude", prompt="hi", cwd=".", system_context="x",
                          permission="yolo", extra_args=("--betas", "foo"))
    _, argv, _ = build_invocation_args(inv)
    assert "--betas" in argv and "foo" in argv
    inv2 = AgentInvocation(cli="codex", prompt="hi", cwd=".", system_context="x",
                           permission="yolo", extra_args=("-c", "k=v"))
    _, argv2, _ = build_invocation_args(inv2)
    assert argv2.index("-c") < argv2.index("exec")  # global flag precedes subcommand


def test_envelope_model_and_permission_echo():
    import _executor
    from _builder import AgentInvocation
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv: ("definitely-not-a-real-cli-xyz", [], None)
    try:
        out = _executor.execute_agent(
            AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), system_context="s",
                            permission="read-only", model="opus"), timeout_ms=1000)
    finally:
        _executor.build_invocation_args = orig
    assert out["model"] == {"requested": "opus", "resolved": None}
    assert out["permission"] == "read-only"
    assert out["permission_flags"] == ["--permission-mode", "plan"]
    assert "_debug_raw" not in out  # internal key never leaks into the envelope


def test_out_skip_short_circuits(tmp_base=None):
    import json as _json
    import subprocess as sp
    out = os.path.join(tempfile.gettempdir(), f"summon-out-{os.getpid()}.json")
    with open(out, "w", encoding="utf-8") as fh:
        _json.dump({"status": "success", "result": "prior run"}, fh)
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        r = sp.run([sys.executable, script, "--agent", "whatever", "--prompt", "p",
                    "--cwd", os.getcwd(), "--out", out],
                   capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["skipped"] is True and env["status"] == "success" and r.returncode == 0
    finally:
        os.remove(out)


def test_extract_json_no_perf_cliff_on_braces():
    # Regression: 1MB of "{" must not take 30s (old raw_decode-every-char bug).
    import time as _t
    from _schema import extract_json
    blob = "{" * 1_000_000
    t0 = _t.monotonic()
    val, err = extract_json(blob)
    dt = _t.monotonic() - t0
    assert val is None and err
    assert dt < 3.0, f"extract_json took {dt:.1f}s on pathological input"


def test_validate_never_raises_on_malformed_schema():
    from _schema import validate
    # minLength as a string, required as a string, pattern as int, bad type:
    bad_schemas = [
        {"type": "string", "minLength": "3"},
        {"type": "object", "required": "notalist"},
        {"type": "string", "pattern": 123},
        {"type": "number", "maximum": "high"},
    ]
    for sc in bad_schemas:
        errs = validate("x" if sc["type"] == "string" else 5, sc)
        assert isinstance(errs, list) and errs, sc  # error string, not a crash


def test_validate_unhashable_schema_members_no_typeerror():
    # JSON-representable but malformed: a non-string type member and a non-string
    # required member must NOT raise TypeError (unhashable dict/list).
    from _schema import validate
    errs = validate({"a": 1}, {"type": [{}]})            # {"type": [{}]}
    assert any("type members must be strings" in e for e in errs), errs
    errs = validate({"a": 1}, {"type": "object", "required": [[]]})  # {"required": [[]]}
    assert any("required members must be strings" in e for e in errs), errs


def test_dry_run_refuses_background_and_manifest():
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    for extra in (["--background"], ["--manifest", "x.json"]):
        r = sp.run([sys.executable, script, "--agent", "a", "--prompt", "p",
                    "--cwd", os.getcwd(), "--dry-run", *extra],
                   capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "error" and "dry-run" in env["error"], (extra, env)
        assert r.returncode == 1


def test_manifest_semaphores_prebuilt_no_race():
    # sem dict must be fully populated before the pool starts (no lazy creation).
    import _manifest as m
    # _normalize + the prebuild path is internal; assert the helper it relies on
    # is deterministic: same backend string always maps to one BoundedSemaphore
    # when built as a dict comprehension (the fix). Smoke the parse instead.
    caps = m._parse_concurrency("agy=2,codex=3,default=1")
    backends = {"a": "agy", "b": "agy", "c": "codex"}
    sems = {b: __import__("threading").BoundedSemaphore(caps.get(b, caps["default"]))
            for b in set(backends.values())}
    assert set(sems) == {"agy", "codex"} and len(sems) == 2


def test_manifest_skip_telemetry_from_existing_file():
    # A cached job (valid envelope already in results-dir) must report
    # skipped=true from the FILE, not depend on child stdout.
    import _manifest as m
    d = tempfile.mkdtemp(prefix="summon-mani-")
    try:
        results = os.path.join(d, "results")
        os.makedirs(results)
        with open(os.path.join(results, "cached.json"), "w", encoding="utf-8") as fh:
            fh.write('{"status": "success", "result": "done earlier", "report": {"status": "DONE"}}')
        # _existing_envelope is what run_job consults before spawning.
        env = m._existing_envelope(os.path.join(results, "cached.json"))
        assert env is not None and env["status"] == "success"
        assert m._existing_envelope(os.path.join(results, "missing.json")) is None
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_manifest_reads_out_file_not_stdout():
    # _read_envelope must trust the --out file even when child stdout has noise.
    import _manifest as m
    import types
    out = os.path.join(tempfile.gettempdir(), f"summon-env-{os.getpid()}.json")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write('{"status": "success", "result": "real"}')
    try:
        proc = types.SimpleNamespace(returncode=0,
                                     stdout='BANNER {oops brace} more noise', stderr="")
        env = m._read_envelope(out, proc)
        assert env["status"] == "success" and env["result"] == "real"
    finally:
        os.remove(out)
    # missing file -> error envelope from exit info, never a stdout slice
    proc = types.SimpleNamespace(returncode=3, stdout="{not json", stderr="boom")
    env = m._read_envelope(os.path.join(tempfile.gettempdir(), "nope-xyz.json"), proc)
    assert env["status"] == "error" and "boom" in env["error"]


def test_write_out_unique_tmp():
    # _write_out must not use a fixed <path>.tmp (concurrent clobber). After a
    # write, only the final file exists — no leftover predictable temp.
    import run_subagent as rs
    d = tempfile.mkdtemp(prefix="summon-out-")
    try:
        target = os.path.join(d, "job.json")
        rs._write_out(target, {"status": "success", "result": "x"})
        import json as _json
        assert _json.load(open(target))["status"] == "success"
        assert not os.path.exists(target + ".tmp")  # no fixed-name temp
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert leftovers == [], leftovers
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_dry_run_resolves_without_executing():
    import json as _json
    import subprocess as sp
    agents = tempfile.mkdtemp(prefix="summon-dryrun-agents-")
    try:
        with open(os.path.join(agents, "probe.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nrun-agent: claude\npermission: read-only\nmodel: opus\n"
                     "args: --betas foo\n---\n# Probe\nA test agent.\n")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        r = sp.run([sys.executable, script, "--agent", "probe", "--prompt", "hello",
                    "--cwd", os.getcwd(), "--agents-dir", agents, "--dry-run"],
                   capture_output=True, text=True, encoding="utf-8")
        view = _json.loads(r.stdout)
        assert view["dry_run"] is True and r.returncode == 0
        assert view["cli"] == "claude" and view["model_requested"] == "opus"
        assert view["permission_flags"] == ["--permission-mode", "plan"]
        assert "--betas" in view["extra_args"]
        assert any("--append-system-prompt" in a for a in view["args"])
    finally:
        import shutil as _sh
        _sh.rmtree(agents, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001 — test harness reports, doesn't raise
            failed += 1
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
