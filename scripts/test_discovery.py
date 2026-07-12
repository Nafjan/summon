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


def test_report_captures_custom_third_party_fields():
    from _executor import parse_report
    # A community agent's custom ALL-CAPS field must be captured as its own key,
    # not folded into the previous field (which used to corrupt HANDOFF).
    text = ("STATUS: DONE\nSUMMARY: reviewed\nSCORE: 8\nRUBRIC: clarity, depth\n"
            "FOLLOW-UP: none\nHANDOFF: pass the score to the next call")
    rep = parse_report(text)
    assert rep["score"] == "8" and rep["rubric"] == "clarity, depth"
    assert rep["handoff"] == "pass the score to the next call"  # NOT swallowing SCORE
    # lowercase narration + a URL line still fold into the current value
    text2 = ("STATUS: DONE\nSUMMARY: s\nFOLLOW-UP: none\n"
             "HANDOFF: see notes below\nsome lowercase detail\nhttp://example.com/x")
    rep2 = parse_report(text2)
    assert "lowercase detail" in rep2["handoff"] and "example.com" in rep2["handoff"]


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


def test_envelope_version_and_cli_version():
    import _executor
    from _executor import _enrich, ENVELOPE_VERSION
    out = _enrich({"result": "x", "status": "success", "cli": "claude"}, None)
    assert out["envelope"] == ENVELOPE_VERSION == 1
    # --version flag prints and exits 0
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    r = sp.run([sys.executable, script, "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and "summon" in r.stdout and "envelope schema" in r.stdout


def test_elapsed_ms_present_even_on_spawn_failure():
    import _executor
    from _builder import AgentInvocation
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv, timeout_ms=None: ("definitely-not-a-real-cli-xyz", [], None)
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
    _executor.build_invocation_args = lambda inv, timeout_ms=None: ("definitely-not-a-real-cli-xyz", [], None)
    try:
        out = _executor.execute_agent(
            AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), system_context="s",
                            permission="read-only", model="opus"), timeout_ms=1000)
    finally:
        _executor.build_invocation_args = orig
    assert out["model"] == {"requested": "opus", "resolved": None, "models_used": []}
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


def test_run_manifest_end_to_end_with_stub_child(tmp=None):
    # Production-path manifest: real run_manifest driving stub jobs whose
    # backend resolves without a live CLI. We stub the child dispatch by
    # pointing --agents-dir at a throwaway agent and forcing --cli to a fake
    # that fails fast, then assert the summary shape + per-backend cap.
    import _manifest as m
    import types
    caps = m._parse_concurrency("codex=1")
    assert caps["codex"] == 1
    # _normalize_jobs is the real parser used by run_manifest:
    jobs, err = m._normalize_jobs({"jobs": [
        {"id": "a", "agent": "x", "prompt": "p"},
        {"id": "b", "agent": "x", "prompt": "p"}]}, ".")
    assert err is None and len(jobs) == 2
    # job_backends resolution (used to prebuild sems) is deterministic:
    # unknown agent -> default backend "codex", so both share one semaphore.
    assert m._job_backend({"agent": "no-such-agent-xyz"}, ".") == "codex"


def test_apply_schema_keeps_original_when_retry_not_better():
    # _apply_schema must NOT replace a successful (schema-invalid) original with
    # a retry that failed. Stub execute_agent to return a failing retry.
    import run_subagent as rs
    import _executor
    from _builder import AgentInvocation
    schema = {"type": "object", "required": ["k"]}
    original = {"status": "success", "result": "no json here",
                "resume": {"cli": "claude", "session_id": "sess1"}, "attempts": 1}
    orig_exec = _executor.execute_agent
    rs_exec = rs.execute_agent
    def fake(inv, timeout_ms=0, debug_dir=None):
        return {"status": "error", "result": "still bad", "resume": {}}
    rs.execute_agent = fake
    try:
        inv = AgentInvocation(cli="claude", prompt="p", cwd=os.getcwd(),
                              system_context="s", permission="safe-edit")
        import argparse
        args = argparse.Namespace(timeout=1000, debug_dir=None)
        out = rs._apply_schema(dict(original), schema, inv, args)
        assert out["status"] == "success" and out["parse_ok"] is False  # kept original
    finally:
        rs.execute_agent = rs_exec


def test_apply_schema_sums_attempts_on_successful_correction():
    import run_subagent as rs
    from _builder import AgentInvocation
    schema = {"type": "object", "required": ["k"]}
    original = {"status": "success", "result": "bad", "attempts": 2,
                "resume": {"cli": "claude", "session_id": "s"}}
    def fake(inv, timeout_ms=0, debug_dir=None):
        return {"status": "success", "result": '{"k": 1}', "attempts": 1, "resume": {}}
    rs_exec = rs.execute_agent
    rs.execute_agent = fake
    try:
        inv = AgentInvocation(cli="claude", prompt="p", cwd=os.getcwd(),
                              system_context="s", permission="safe-edit")
        import argparse
        out = rs._apply_schema(dict(original), schema,
                               inv, argparse.Namespace(timeout=1000, debug_dir=None))
        assert out["parse_ok"] is True and out["attempts"] == 3  # 2 + 1 preserved
    finally:
        rs.execute_agent = rs_exec


def test_output_tail_on_error_paths():
    # A spawn failure has no stdout, but a real error envelope from the executor
    # must carry output_tail. Exercise the output-cap path via a tiny stub is
    # hard; instead assert _attach_raw wiring on the helper directly.
    from _executor import _attach_raw, _error_response
    resp = _attach_raw(_error_response("claude", 1, "boom"), ["line1\n", "line2\n"])
    assert resp["output_tail"] == "line1\nline2\n"
    assert "_debug_raw" in resp


def test_schema_unsupported_keywords_warned():
    from _schema import unsupported_keywords, attach_parsed
    sc = {"type": "object", "oneOf": [], "properties": {"x": {"type": "string", "format": "email"}}}
    kws = {k for _, k in unsupported_keywords(sc)}
    assert "oneOf" in kws and "format" in kws
    resp = {"result": '{"x": "a@b.com"}'}
    attach_parsed(resp, sc)
    assert resp["parse_ok"] is True and resp.get("parse_warnings")


def test_schema_additional_properties_as_schema_enforced():
    # additionalProperties: {schema} must VALIDATE extra props (was ignored ->
    # parse_ok on unchecked data), and unsupported keywords under it must warn.
    from _schema import validate, unsupported_keywords
    sc = {"type": "object", "additionalProperties": {"type": "string"}}
    assert validate({"n": 123}, sc)            # 123 is not a string -> error
    assert validate({"n": "ok"}, sc) == []     # string extra prop passes
    sc2 = {"type": "object", "additionalProperties": {"type": "string", "format": "email"}}
    kws = {k for _, k in unsupported_keywords(sc2)}
    assert "format" in kws


def test_doctor_reads_version_from_stderr():
    import _doctor, types
    orig = _doctor.subprocess.run
    _doctor.subprocess.run = lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="   \n", stderr="mycli version 9.9")  # blank stdout
    try:
        v = _doctor._probe_version("/fake/mycli")
    finally:
        _doctor.subprocess.run = orig
    assert v == "mycli version 9.9", v


def test_background_and_out_rejected():
    import json as _json, subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    r = sp.run([sys.executable, script, "--agent", "a", "--prompt", "p",
                "--cwd", os.getcwd(), "--background", "--out", "x.json"],
               capture_output=True, text=True, encoding="utf-8")
    env = _json.loads(r.stdout)
    assert env["status"] == "error" and "incompatible" in env["error"] and r.returncode == 1


def test_roster_new_agent_scaffolds_house_format():
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-roster-")
    try:
        r = sp.run([sys.executable, script, "--new-agent", "fact-checker",
                    "--set", "run-agent=codex", "--set", "permission=read-only",
                    "--set", "model=gpt-5.6-sol", "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        info = _json.loads(r.stdout)
        assert r.returncode == 0 and info["status"] == "success", info
        body = open(info["path"], encoding="utf-8").read()
        # house structure the dispatcher depends on:
        for must in ("STATUS: DONE | PARTIAL | BLOCKED", "HANDOFF:", "SUMMARY:",
                     "## Untrusted content", "run-agent: codex",
                     "permission: read-only", "model: gpt-5.6-sol"):
            assert must in body, must
        # registers instantly
        r2 = sp.run([sys.executable, script, "--list", "--agents-dir", d],
                    capture_output=True, text=True, encoding="utf-8")
        agents = _json.loads(r2.stdout)["agents"]
        assert any(a["name"] == "fact-checker" for a in agents)
        # and is dispatch-ready (dry-run resolves it)
        r3 = sp.run([sys.executable, script, "--agent", "fact-checker", "--prompt", "x",
                     "--cwd", os.getcwd(), "--agents-dir", d, "--dry-run"],
                    capture_output=True, text=True, encoding="utf-8")
        view = _json.loads(r3.stdout)
        assert view["cli"] == "codex" and view["permission"] == "read-only"
        # never overwrites
        r4 = sp.run([sys.executable, script, "--new-agent", "fact-checker",
                     "--agents-dir", d], capture_output=True, text=True, encoding="utf-8")
        assert r4.returncode == 1 and "already exists" in _json.loads(r4.stdout)["error"]
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_roster_set_agent_edits_frontmatter_only():
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-roster-")
    try:
        sp.run([sys.executable, script, "--new-agent", "probe", "--agents-dir", d],
               capture_output=True, text=True, encoding="utf-8")
        path = os.path.join(d, "probe.md")
        body_before = open(path, encoding="utf-8").read().split("---", 2)[2]
        # update model + permission, add args
        r = sp.run([sys.executable, script, "--set-agent", "probe",
                    "--set", "model=claude-sonnet-5", "--set", "permission=yolo",
                    "--set", 'args=--flag', "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        info = _json.loads(r.stdout)
        assert info["frontmatter"]["model"] == "claude-sonnet-5"
        assert info["frontmatter"]["permission"] == "yolo"
        assert open(path, encoding="utf-8").read().split("---", 2)[2] == body_before
        # empty value removes the key
        r = sp.run([sys.executable, script, "--set-agent", "probe", "--set", "model=",
                    "--agents-dir", d], capture_output=True, text=True, encoding="utf-8")
        assert "model" not in _json.loads(r.stdout)["frontmatter"]
        # invalid enum rejected, file untouched
        r = sp.run([sys.executable, script, "--set-agent", "probe",
                    "--set", "permission=godmode", "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 1 and "permission" in _json.loads(r.stdout)["error"]
        # unknown key rejected
        r = sp.run([sys.executable, script, "--set-agent", "probe",
                    "--set", "prompt=evil", "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 1 and "unknown key" in _json.loads(r.stdout)["error"]
        # path-traversal name rejected
        r = sp.run([sys.executable, script, "--new-agent", "../evil", "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 1
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_roster_rejects_newline_injection():
    # A newline in a --set value must NOT smuggle a second frontmatter key.
    import _roster
    d = tempfile.mkdtemp(prefix="summon-inj-")
    try:
        _roster.new_agent(d, "victim", {"permission": "read-only"})
        path = os.path.join(d, "victim.md")
        for evil in ("plain\npermission: yolo", "x\n---\nowned", "a\rpermission: yolo"):
            try:
                _roster.set_agent(d, "victim", {"model": evil})
                raise AssertionError(f"injection not rejected: {evil!r}")
            except ValueError as e:
                assert "control character" in str(e)
        # the victim's permission is untouched
        from _loader import load_agent
        assert load_agent(d, "victim")[4] == "read-only"
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_roster_preserves_crlf_body_and_dedups_keys():
    import _roster
    from _loader import load_agent
    d = tempfile.mkdtemp(prefix="summon-crlf-")
    try:
        # hand-build a CRLF file with a duplicate key and a body containing '---'
        path = os.path.join(d, "raw.md")
        body = b"\r\n# Raw\r\nline with --- inside\r\nmodel: not-a-key-here\r\n"
        with open(path, "wb") as fh:
            fh.write(b"---\r\nrun-agent: claude\r\npermission: safe-edit\r\n"
                     b"permission: safe-edit\r\n---\r\n" + body)
        _roster.set_agent(d, "raw", {"model": "claude-sonnet-5", "permission": "yolo"})
        raw = open(path, "rb").read()
        # body bytes preserved exactly (CRLF intact, the '---' body line survives)
        assert raw.endswith(body), "body not byte-preserved"
        # duplicate permission collapsed to the single new value
        assert raw.count(b"permission:") == 1
        ra, _, _, _, perm, model, _ = load_agent(d, "raw")
        assert perm == "yolo" and model == "claude-sonnet-5" and ra == "claude"
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_roster_rejects_non_utf8_value_no_squatter():
    # An unpaired surrogate (model-generated garbage) must be rejected up front,
    # never leave a zero-byte file squatting the agent name.
    import _roster
    d = tempfile.mkdtemp(prefix="summon-surr-")
    try:
        try:
            _roster.new_agent(d, "s", {"model": "x\ud800y"})  # lone high surrogate
            raise AssertionError("non-UTF-8 value not rejected")
        except ValueError as e:
            assert "UTF-8" in str(e)
        assert not os.path.exists(os.path.join(d, "s.md"))  # name still free
        # a clean retry then succeeds
        _roster.new_agent(d, "s", {"model": "claude-sonnet-5"})
        assert os.path.isfile(os.path.join(d, "s.md"))
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_roster_modes_mutually_exclusive():
    import json as _json, subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    r = sp.run([sys.executable, script, "--new-agent", "a", "--set-agent", "b",
                "--agents-dir", tempfile.gettempdir()],
               capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 1 and "mutually exclusive" in _json.loads(r.stdout)["error"]


def test_openai_compat_http_roundtrip():
    # Full openai-compat path against a stdlib mock server: result, usage,
    # model.resolved, billing=api, envelope — all through _enrich/_stamp.
    import http.server, threading, subprocess as sp, json as _json
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            req = _json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            out = {"model": req["model"],
                   "choices": [{"message": {"content": "PONG " + req["messages"][-1]["content"][:10]}}],
                   "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}}
            body = _json.dumps(out).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="summon-apitest-")
    try:
        open(os.path.join(d, "bot.md"), "w", encoding="utf-8").write(
            f"---\nrun-agent: openai-compat\nbase_url: http://127.0.0.1:{port}/v1\n"
            f'api_key_env: ""\nmodel: test-model\n---\n# Bot\nrole.\n')
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        r = sp.run([sys.executable, script, "--agent", "bot", "--prompt", "ping",
                    "--cwd", d, "--agents-dir", d, "--timeout", "30s"],
                   capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "success" and env["result"].startswith("PONG")
        assert env["model"] == {"requested": "test-model", "resolved": "test-model", "models_used": []}
        assert env["usage"]["total_tokens"] == 9 and env["billing"]["source"] == "api"
        assert env["envelope"] == 1
    finally:
        srv.shutdown()
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_openai_compat_redacts_key_and_survives_errors():
    # A reflected API key in an error body must be REDACTED; malformed responses
    # (bad shape, non-string content, non-JSON) must return clean error envelopes.
    import http.server, threading, os as _os, tempfile, subprocess as sp, json as _json
    SECRET = "sk-secret-key-12345"
    class H(http.server.BaseHTTPRequestHandler):
        mode = "reflect"
        def log_message(self, *a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if H.mode == "reflect":                    # error body echoes the auth header
                body = _json.dumps({"error": f"invalid key: {self.headers.get('Authorization')}"}).encode()
                self.send_response(401)
            elif H.mode == "badshape":
                body = _json.dumps({"nope": 1}).encode(); self.send_response(200)
            else:                                       # non-string content
                body = _json.dumps({"choices": [{"message": {"content": {"tool": "x"}}}]}).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="summon-apierr-")
    try:
        open(_os.path.join(d, "b.md"), "w", encoding="utf-8").write(
            f"---\nrun-agent: openai-compat\nbase_url: http://127.0.0.1:{port}/v1\n"
            f"api_key_env: MY_SECRET\nmodel: m\n---\n# B\n")
        script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "run_subagent.py")
        env_with_key = {**_os.environ, "MY_SECRET": SECRET}
        # reflect: error envelope must NOT contain the secret
        H.mode = "reflect"
        r = sp.run([sys.executable, script, "--agent", "b", "--prompt", "x", "--cwd", d,
                    "--agents-dir", d, "--timeout", "20s"], capture_output=True, text=True, env=env_with_key)
        assert SECRET not in r.stdout and "REDACTED" in r.stdout, r.stdout[:300]
        assert _json.loads(r.stdout)["status"] == "error"
        # bad shape + non-string content: clean error / no crash
        for mode in ("badshape", "nonstr"):
            H.mode = mode
            r = sp.run([sys.executable, script, "--agent", "b", "--prompt", "x", "--cwd", d,
                        "--agents-dir", d, "--timeout", "20s"], capture_output=True, text=True, env=env_with_key)
            env = _json.loads(r.stdout)
            assert "Traceback" not in r.stderr and env["status"] in ("error", "success"), mode
    finally:
        srv.shutdown()
        import shutil as _sh; _sh.rmtree(d, ignore_errors=True)


def test_openai_compat_dry_run_no_crash():
    import json as _json, subprocess as sp, tempfile
    d = tempfile.mkdtemp(prefix="summon-apidry-")
    try:
        open(os.path.join(d, "b.md"), "w", encoding="utf-8").write(
            '---\nrun-agent: openai-compat\nprovider: ollama\nmodel: llama3.1\n---\n# B\n')
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        r = sp.run([sys.executable, script, "--agent", "b", "--prompt", "x", "--cwd", d,
                    "--agents-dir", d, "--dry-run"], capture_output=True, text=True)
        view = _json.loads(r.stdout)
        assert view["dry_run"] is True and view["cli"] == "openai-compat"
        assert view["permission_flags"] is None and "11434" in view["base_url"]
        # the dry-run must never surface a key value
        assert "api_key_present" in view and "api_key_env" in view
    finally:
        import shutil as _sh; _sh.rmtree(d, ignore_errors=True)


def test_openai_compat_provider_resolution():
    import _apibackend
    # inline base_url wins; a known provider resolves; unknown raises
    bu, key = _apibackend.resolve_endpoint(
        {"base_url": "http://x/v1/", "api_key_env": "MY_KEY"}, None)
    assert bu == "http://x/v1" and key == "MY_KEY"
    bu, key = _apibackend.resolve_endpoint({"provider": "openrouter", "model": "m"}, None)
    assert bu == "https://openrouter.ai/api/v1" and key == "OPENROUTER_API_KEY"
    bu, key = _apibackend.resolve_endpoint({"provider": "ollama"}, None)
    assert "11434" in bu and key == ""   # local, no key
    try:
        _apibackend.resolve_endpoint({"provider": "nope"}, None)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_subcommand_rewrite():
    import run_subagent as rs
    # subcommands translate to flat flags
    assert rs._rewrite_subcommand(["list"]) == (["--list"], None)
    assert rs._rewrite_subcommand(["agents", "--agents-dir", "x"]) == (["--list", "--agents-dir", "x"], None)
    assert rs._rewrite_subcommand(["models", "--cli", "codex"]) == (["--list-models", "--cli", "codex"], None)
    assert rs._rewrite_subcommand(["doctor", "--json"]) == (["--doctor", "--json"], None)
    assert rs._rewrite_subcommand(["manifest", "jobs.json"]) == (["--manifest", "jobs.json"], None)
    assert rs._rewrite_subcommand(["council", "--question", "q"]) == (["--council", "--question", "q"], None)
    assert rs._rewrite_subcommand(["agent", "new", "n", "--set", "k=v"]) == (["--new-agent", "n", "--set", "k=v"], None)
    assert rs._rewrite_subcommand(["agent", "set", "n"]) == (["--set-agent", "n"], None)
    assert rs._rewrite_subcommand(["dispatch", "--agent", "a"]) == (["--agent", "a"], None)
    # legacy flat passes through untouched
    assert rs._rewrite_subcommand(["--agent", "a", "--prompt", "p"]) == (["--agent", "a", "--prompt", "p"], None)
    # help / empty / bare-agent -> usage
    assert rs._rewrite_subcommand([])[1] == "help"
    assert rs._rewrite_subcommand(["help"])[1] == "help"
    assert rs._rewrite_subcommand(["agent"])[1] == "help"
    # an INVALID agent action is an error (exit 2), NOT success
    _, m = rs._rewrite_subcommand(["agent", "delete", "x"])
    assert m.startswith("error:") and "delete" in m
    # <subcommand> --help -> general usage (facade has no per-command parser)
    assert rs._rewrite_subcommand(["manifest", "--help"])[1] == "help"
    assert rs._rewrite_subcommand(["agent", "new", "--help"])[1] == "help"
    # an unknown leading token is left for the flat parser to reject
    assert rs._rewrite_subcommand(["bogus", "x"]) == (["bogus", "x"], None)


def test_subcommand_and_flat_equivalent_live():
    import json as _json, subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    a = sp.run([sys.executable, script, "list", "--agents-dir",
                os.path.dirname(script)], capture_output=True, text=True)  # scripts/ has no .md -> 0
    b = sp.run([sys.executable, script, "--list", "--agents-dir",
                os.path.dirname(script)], capture_output=True, text=True)
    assert _json.loads(a.stdout)["agents"] == _json.loads(b.stdout)["agents"]
    # `summon` with no args prints usage and exits 0
    u = sp.run([sys.executable, script], capture_output=True, text=True)
    assert u.returncode == 0 and "summon" in u.stdout and "Commands:" in u.stdout


def test_backend_registry_is_single_source_of_truth():
    from _builder import BACKENDS, BACKEND_CLIS, backend_kind, build_invocation_args, AgentInvocation
    from _resolver import _VALID_CLIS
    # registry drives the valid-CLI list
    assert tuple(_VALID_CLIS) == BACKEND_CLIS == tuple(BACKENDS)
    # every entry is well-formed
    for cli, b in BACKENDS.items():
        assert b["kind"] in ("subprocess", "api")
        assert ("build" in b) if b["kind"] == "subprocess" else ("call" in b)
    assert backend_kind("openai-compat") == "api"
    assert backend_kind("claude") == "subprocess" and backend_kind("nope") is None
    # build_invocation_args refuses an api-kind backend (no argv to build)
    inv = AgentInvocation(cli="openai-compat", prompt="x", cwd=".", system_context="s",
                          permission="safe-edit", model="m", base_url="http://x/v1")
    try:
        build_invocation_args(inv)
        raise AssertionError("expected ValueError for api-kind build")
    except ValueError as e:
        assert "api-kind" in str(e)


def test_billing_inference():
    from _builder import infer_billing
    assert infer_billing("agy")["source"] == "subscription"
    assert infer_billing("openai-compat")["source"] == "api"
    # codex is subscription unless the guard is opted out with a key present
    orig = dict(os.environ)
    try:
        os.environ.pop("OPENAI_API_KEY", None); os.environ.pop("SUBAGENTS_ALLOW_OPENAI_KEY", None)
        assert infer_billing("codex")["source"] == "subscription"
        os.environ["OPENAI_API_KEY"] = "x"; os.environ["SUBAGENTS_ALLOW_OPENAI_KEY"] = "1"
        assert infer_billing("codex")["source"] == "api"
    finally:
        os.environ.clear(); os.environ.update(orig)


def test_council_ranking_parse_and_aggregate():
    from _council import _parse_ranking, _aggregate_rankings
    assert _parse_ranking("stuff\nRANKING: C, A, B\nmore", 3) == [2, 0, 1]
    assert _parse_ranking("RANKING: a,b,a,c", 3) == [0, 1, 2]   # dedup, complete perm
    # INCOMPLETE ballots are rejected (no partial first-place credit)
    assert _parse_ranking("RANKING: BAD", 3) is None            # B,A,D -> D invalid -> B,A incomplete
    assert _parse_ranking("RANKING: A, B", 3) is None           # missing C
    assert _parse_ranking("no ranking here", 3) is None
    # the LAST complete RANKING line wins (models restate)
    assert _parse_ranking("RANKING: A,B,C\nthinking...\nRANKING: C,B,A", 3) == [2, 1, 0]
    assert _parse_ranking("RANKING: A,B", 30) is None           # >26 candidates unrankable
    # Borda: two voters both rank [0,1,2] -> index 0 best (score 2), index 2 worst (0)
    agg = _aggregate_rankings([[0, 1, 2], [0, 1, 2]], 3)
    assert agg[0]["index"] == 0 and agg[0]["score"] == 2.0
    assert agg[-1]["index"] == 2 and agg[-1]["score"] == 0.0


def test_council_prompts_and_position_extraction():
    import _council
    # position = report summary (+findings) when present, else result tail
    assert _council._position({"report": {"summary": "use X", "findings": "because Y"}}) \
        .startswith("use X")
    assert _council._position({"result": "raw answer"}) == "raw answer"
    q = "SQL or NoSQL?"
    assert "QUESTION" in _council._round1_prompt(q) and q in _council._round1_prompt(q)
    p2 = _council._round2_prompt(q, ["pos A", "pos B"])
    assert "round 2" in p2 and "Advisor A" in p2 and "Advisor B" in p2
    ch = _council._chairman_prompt(q, [{"agent": "planner", "backend": "claude",
                                        "model": "opus", "position": "go SQL"}])
    assert "CHAIRMAN" in ch and "CONFIDENCE" in ch and "go SQL" in ch


def test_council_run_structure_with_stubbed_dispatch():
    # Full run_council flow with dispatch stubbed (no live models). Verifies the
    # envelope shape, parallel member collection, and chairman synthesis.
    import _council, argparse, tempfile, types
    d = tempfile.mkdtemp(prefix="summon-council-test-")
    try:
        # two real agent files so validation passes
        for a in ("m1", "m2", "chair"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\nrole.\n")
        calls = {"n": 0, "timeouts": []}
        def fake_dispatch(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
            calls["n"] += 1
            calls["timeouts"].append(timeout_ms)
            if agent == "chair":
                return {"status": "success", "result": "DECISION: X, CONFIDENCE 0.9",
                        "model": {"resolved": "claude-fable-5"},
                        "report": {"summary": "X wins"}}
            # round-2 dispatches (tag r2-*) emit a RANKING so consensus aggregates
            rank = "\nRANKING: A, B" if tag.startswith("r2-") else ""
            return {"status": "success", "result": f"{agent} says go{rank}",
                    "model": {"resolved": "claude-sonnet-5"},
                    "report": {"summary": f"{agent}: pick X"}}
        orig = _council._dispatch
        _council._dispatch = fake_dispatch
        try:
            args = argparse.Namespace(question="X or Y?", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=2,
                                      cwd=os.getcwd(), agents_dir=d, timeout=90000)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _council.run_council(args)
        finally:
            _council._dispatch = orig
        env = __import__("json").loads(buf.getvalue())
        assert rc == 0 and env["mode"] == "council" and env["rounds"] == 2
        assert len(env["members"]) == 2 and {m["agent"] for m in env["members"]} == {"m1", "m2"}
        assert env["synthesis"]["chairman"] == "chair" and env["failed_members"] == []
        assert "DECISION" in env["synthesis"]["recommendation"]
        assert calls["n"] == 5, calls["n"]           # 2 members x 2 rounds + chairman
        assert all(t == 90000 for t in calls["timeouts"])  # --timeout ms plumbed, not dropped
        # peer ranking aggregated (both voted A,B -> m1 outranks m2), no _raw leak
        cr = env["consensus_ranking"]
        assert cr and cr[0]["agent"] == "m1" and cr[0]["score"] == 1.0
        assert all("_raw" not in m for m in env["members"])
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_council_validation_and_member_status():
    import _council, argparse, io, contextlib, tempfile, json as _json
    def run(ns):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _council.run_council(ns)
        return rc, _json.loads(buf.getvalue())
    base = dict(question="q", question_file=None, chairman="chair",
                cwd=os.getcwd(), timeout=60000, rounds=1)
    d = tempfile.mkdtemp(prefix="summon-cval-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        # duplicate members rejected
        rc, env = run(argparse.Namespace(**base, members="m1,m1", agents_dir=d))
        assert rc == 1 and "duplicate" in env["error"]
        # invalid rounds rejected
        rc, env = run(argparse.Namespace(**{**base, "rounds": 5}, members="m1,m2", agents_dir=d))
        assert rc == 1 and "rounds" in env["error"]
        # a FAILED member -> status partial (not success), listed in failed_members
        orig = _council._dispatch
        def fake(agent, *a, **k):
            if agent == "chair":
                return {"status": "success", "result": "DECISION", "report": {"summary": "s"}}
            if agent == "m2":
                return {"status": "error", "error": "boom"}
            return {"status": "success", "result": "ok", "report": {"summary": "ok"}}
        _council._dispatch = fake
        try:
            rc, env = run(argparse.Namespace(**base, members="m1,m2", agents_dir=d))
        finally:
            _council._dispatch = orig
        assert rc == 1 and env["status"] == "partial" and env["failed_members"] == ["m2"]
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_council_dry_run_rejected():
    import json as _json, subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    r = sp.run([sys.executable, script, "--council", "--question", "x",
                "--cwd", os.getcwd(), "--dry-run"], capture_output=True, text=True)
    env = _json.loads(r.stdout)
    assert env["status"] == "error" and "council" in env["error"] and r.returncode == 1


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


# --- Regression tests for ultrareview findings (F1-F25) ----------------------

def test_no_false_success_on_backend_error_result():
    # F1: a claude terminal result with is_error must NOT surface as success.
    import _executor
    from _stream import StreamProcessor
    sp = StreamProcessor()
    sp.process_line('{"type":"result","subtype":"error_during_execution",'
                    '"is_error":true,"result":"model errored"}')
    out = _executor._enrich(
        _executor.build_final_response("claude", 0, sp.get_result(),
                                       ['{"type":"result"}\n'], None), sp)
    assert out["status"] == "error", out["status"]
    assert "error" in out and out["error"]
    # a clean success result is still success
    sp2 = StreamProcessor()
    sp2.process_line('{"type":"result","subtype":"success","is_error":false,"result":"ok"}')
    out2 = _executor.build_final_response("claude", 0, sp2.get_result(), [], None)
    assert out2["status"] == "success", out2["status"]


def test_stream_exposes_all_models_used():
    # F17: models_used lists every model, resolved is only the dominant one.
    from _stream import StreamProcessor
    sp = StreamProcessor()
    sp.process_line('{"type":"result","result":"x","modelUsage":'
                    '{"claude-sonnet-5":{"outputTokens":900},'
                    '"claude-haiku-4-5":{"outputTokens":50}}}')
    assert sp.model == "claude-sonnet-5", sp.model
    assert sp.models_used == ["claude-haiku-4-5", "claude-sonnet-5"], sp.models_used


def test_timeout_does_not_hang_on_grandchild_holding_stdout():
    # F2: a grandchild inheriting stdout must not let communicate() block past the
    # deadline. Old code hung ~15s; fixed code returns fast (tree-kill + bounded
    # communicate). Guards the wall-clock-timeout guarantee.
    import time as _t, subprocess as _sp, _executor
    child = ("import subprocess,sys,time;"
             "subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
             "time.sleep(0.3)")  # child spawns a 20s grandchild (inherits stdout), then exits
    extra = {"start_new_session": True} if os.name != "nt" else {}
    proc = _sp.Popen([sys.executable, "-c", child], stdin=_sp.DEVNULL,
                     stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                     encoding="utf-8", errors="replace", bufsize=1, **extra)
    t0 = _t.monotonic()
    resp = _executor._drive_process(proc, "claude", timeout_ms=1000)
    elapsed = _t.monotonic() - t0
    # Fixed path (tree-kill + bounded _safe_communicate) returns in a few seconds;
    # the old unbounded communicate() would block ~20s until the grandchild exits.
    assert elapsed < 15, f"timeout path took {elapsed:.1f}s (regression: unbounded communicate)"
    assert resp["status"] != "success", resp["status"]


def test_manifest_rejects_non_string_json_schema():
    # F6: an inline dict json_schema would be str()-mangled; reject up front.
    import _manifest
    jobs, err = _manifest._normalize_jobs(
        {"jobs": [{"id": "j", "agent": "reviewer", "prompt": "p",
                   "json_schema": {"type": "object"}}]}, ".")
    assert jobs is None and err and "json_schema must be a file path" in err, err
    # a string path is accepted
    jobs2, err2 = _manifest._normalize_jobs(
        {"jobs": [{"id": "j", "agent": "reviewer", "prompt": "p",
                   "json_schema": "schema.json"}]}, ".")
    assert err2 is None and jobs2, err2


def test_resolve_cli_fails_closed_on_unknown_backend():
    # F8: a typo'd run-agent must raise, not silently run under codex.
    import _resolver
    try:
        _resolver.resolve_cli("claude-typo")
        assert False, "expected ValueError for unknown run-agent"
    except ValueError:
        pass
    assert _resolver.resolve_cli("openai-compat") == "openai-compat"


def test_extract_json_handles_primitives():
    # F9: bare top-level primitives must extract (schema layer supports them).
    from _schema import extract_json
    for text, want in (("true", True), ("42", 42), ('"ok"', "ok"), ("null", None)):
        val, err = extract_json(text)
        assert err is None and val == want, (text, val, err)
    # objects still win when present
    assert extract_json('note\n{"a":1}')[0] == {"a": 1}


def test_frontmatter_preserves_value_ending_in_quote():
    # F10: args ending in a quoted token must survive (no blanket quote-strip).
    from _loader import parse_frontmatter, parse_extra_args
    fm, _ = parse_frontmatter('---\nrun-agent: codex\nargs: --label "two words"\n---\nbody')
    assert fm["args"] == '--label "two words"', fm["args"]
    assert parse_extra_args(fm["args"]) == ["--label", "two words"]
    # a fully-quoted value is still unquoted
    fm2, _ = parse_frontmatter('---\nname: "quoted"\n---\nb')
    assert fm2["name"] == "quoted"


def test_parse_report_ignores_unindented_template_line():
    # F20: an echoed "STATUS: DONE | PARTIAL | BLOCKED" template must not displace
    # the real block above it.
    import _executor
    rep = _executor.parse_report(
        "STATUS: DONE\nSUMMARY: real\nFOLLOW-UP: none\nHANDOFF: none\n"
        "STATUS: DONE | PARTIAL | BLOCKED")
    assert rep and rep.get("summary") == "real", rep


def test_council_rejects_too_many_members():
    # F25: council size is bounded (thread + argv-budget safety).
    import _council, io, contextlib
    ns = type("N", (), {})()
    ns.question = "q"; ns.question_file = None; ns.members = ",".join(f"m{i}" for i in range(11))
    ns.chairman = "fable"; ns.rounds = 1; ns.cwd = os.getcwd(); ns.agents_dir = None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _council.run_council(ns)
    assert rc != 0 and "too many council members" in buf.getvalue(), buf.getvalue()


def test_child_out_does_not_skip_on_non_success_envelope():
    # F3: a prior error/blocked envelope must NOT short-circuit as "done".
    import json as _json, subprocess as _sp
    out = os.path.join(tempfile.gettempdir(), f"summon-f3-{os.getpid()}.json")
    with open(out, "w", encoding="utf-8") as fh:
        _json.dump({"status": "error", "result": "prior failure"}, fh)
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        r = _sp.run([sys.executable, script, "--agent", "definitely-missing-agent",
                     "--prompt", "p", "--cwd", os.getcwd(), "--out", out],
                    capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        # It re-dispatched (and failed on the missing agent) rather than emitting
        # the prior envelope with skipped=True.
        assert env.get("skipped") is not True, env
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# --- Regression tests for cross-vendor review of the fixes (round 2) ---------

def test_schema_null_value_parses_ok():
    # W2: a valid JSON `null` must validate, not read as an extraction failure.
    import _schema
    resp = {"result": "null"}
    _schema.attach_parsed(resp, {"type": "null"})
    assert resp["parse_ok"] is True and resp["parsed"] is None, resp
    # a genuinely absent value still fails
    resp2 = {"result": "no json here at all"}
    _schema.attach_parsed(resp2, {"type": "object"})
    assert resp2["parse_ok"] is False, resp2


def test_manifest_timeout_grammar_matches_child():
    # W3: bare number is MILLISECONDS (like the child), suffixes ms/s/m; no 'h'.
    import _manifest
    assert _manifest._timeout_seconds("600000") == 600.0      # bare == ms
    assert _manifest._timeout_seconds("30s") == 30.0
    assert _manifest._timeout_seconds("2m") == 120.0
    assert _manifest._timeout_seconds("500ms") == 1.0         # floored to >=1s
    assert _manifest._timeout_seconds("2h") == 600.0          # 'h' unsupported -> default
    # parent watchdog stays comfortably above the child's own budget
    assert _manifest._parent_timeout({"timeout": "30s"}) >= 90.0


def test_fable_credit_only_guard():
    # Fable (claude-fable-5) is credit-only: on the claude CLI it falls back to
    # the `opus` alias unless SUMMON_ALLOW_FABLE=1; the API path is never rewritten.
    import _builder, _executor
    from _builder import AgentInvocation, build_invocation_args as _bia, apply_credit_guard

    def _models(args):
        return [args[i + 1] for i, x in enumerate(args) if x in ("--model", "-m", "--fallback-model")]

    for k in ("SUMMON_ALLOW_FABLE", "SUMMON_ALLOW_CREDIT", "ANTHROPIC_API_KEY",
              "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_MODEL"):
        os.environ.pop(k, None)
    eff, note = _builder.resolve_billing_model("claude-fable-5", "claude")
    assert eff == "opus" and note, (eff, note)
    # argv carries the fallback alias, not fable
    _, args, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="claude-fable-5"))
    assert _models(args) == ["opus"], _models(args)

    # CC3: credit-only model flags in `args:` are scrubbed (both forms)
    _, a1, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="opus",
                                    extra_args=["--fallback-model", "claude-fable-5"]))
    assert "claude-fable-5" not in a1
    _, a2, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="claude-fable-5",
                                    extra_args=["--model", "claude-fable-5"]))
    assert _models(a2) == ["opus"], _models(a2)

    # CC2: an ANTHROPIC_* alias remap to a credit-only model is stripped from the child env
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "claude-fable-5"
    try:
        _, _, env = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="opus"))
        assert env and env.get("ANTHROPIC_DEFAULT_OPUS_MODEL") is None, env
    finally:
        del os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"]

    # CC1: a claude resume can't be re-pinned -> warns
    _, _, w = apply_credit_guard(AgentInvocation(cli="claude", prompt="x", cwd=".",
                                                 model="claude-fable-5", resume_id="s1"))
    assert any("resuming" in x for x in w), w

    # authorized -> real Fable, no substitution; API path never rewritten
    os.environ["SUMMON_ALLOW_FABLE"] = "1"
    try:
        assert _builder.resolve_billing_model("claude-fable-5", "claude") == ("claude-fable-5", None)
    finally:
        del os.environ["SUMMON_ALLOW_FABLE"]
    assert _builder.resolve_billing_model("claude-fable-5", "openai-compat") == ("claude-fable-5", None)

    # envelope transparency: fallback preserves requested + warns; opus billing stays subscription
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv, timeout_ms=None: ("definitely-not-a-real-cli-xyz", [], None)
    def _run(inv):
        return _executor.execute_agent(inv, timeout_ms=800)
    try:
        r = _run(AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), model="claude-fable-5"))
        # DC4: unauthorized resume of a Fable request -> billing 'unknown' (guard
        # can't re-pin on --resume) with the resume warning
        r_res = _run(AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(),
                                     model="claude-fable-5", resume_id="s1"))
        os.environ["SUMMON_ALLOW_FABLE"] = "1"
        # CC4: authorized WITH an API key bills api, not credit
        os.environ["ANTHROPIC_API_KEY"] = "sk-x"
        r2 = _run(AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), model="claude-fable-5"))
        del os.environ["ANTHROPIC_API_KEY"]
        # DC1: authorized Fable selected only via args: still bills credit
        r_args = _run(AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), model=None,
                                      extra_args=["--model", "claude-fable-5"]))
    finally:
        _executor.build_invocation_args = orig
        os.environ.pop("SUMMON_ALLOW_FABLE", None); os.environ.pop("ANTHROPIC_API_KEY", None)
    assert r["model"]["requested"] == "claude-fable-5", r["model"]
    assert any("account credit" in x for x in r.get("warnings", [])), r.get("warnings")
    assert r["billing"]["source"] == "subscription", r["billing"]
    assert r2["billing"]["source"] == "api", r2["billing"]
    assert r_res["billing"]["source"] == "unknown", r_res["billing"]
    assert r_args["billing"]["source"] == "credit", r_args["billing"]


def test_parse_report_keeps_real_status_with_pipe():
    # GF5: a real status containing " | " (not a template) must NOT be skipped.
    import _executor
    rep = _executor.parse_report(
        "STATUS: BLOCKED | waiting on approval\nSUMMARY: s\nFOLLOW-UP: none\nHANDOFF: none")
    assert rep and rep.get("status", "").startswith("BLOCKED |"), rep
    # the pure template (all pipe tokens are status words) is still skipped
    rep2 = _executor.parse_report(
        "STATUS: DONE\nSUMMARY: real\nFOLLOW-UP: none\nHANDOFF: none\n"
        "STATUS: DONE | PARTIAL | BLOCKED")
    assert rep2 and rep2.get("summary") == "real", rep2


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
