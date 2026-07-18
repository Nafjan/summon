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
    # Spawn failure: no handshake, no terminal event -> targeted falls back to
    # the guard-effective request; served stays None (no evidence); resolved
    # keeps legacy v1 semantics (None here).
    assert out["model"] == {"requested": "opus", "targeted": "opus", "served": None,
                            "resolved": None, "models_used": []}
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


def test_new_agent_refuses_to_write_into_bundled_roster():
    """--new-agent / --set-agent must REFUSE when the resolved roster dir IS the
    skill's bundled starter roster — enforcing bundled_roster_dir() as read-only
    in practice, not just by convention (a write there corrupts an installed
    skill and desyncs its ownership manifest)."""
    import subprocess as sp

    import _loader
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    bundled = _loader.bundled_roster_dir()
    assert bundled and os.path.isdir(bundled), "bundled roster should exist in a checkout"
    cwd = tempfile.mkdtemp(prefix="summon-guard-")
    victim = os.path.join(bundled, "guardtest_zzz.md")
    try:
        r = sp.run([sys.executable, script, "--new-agent", "guardtest_zzz",
                    "--agents-dir", bundled, "--cwd", cwd],
                   capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert "bundled" in (r.stdout + r.stderr).lower(), (r.stdout, r.stderr)
        assert not os.path.exists(victim), "guard failed: wrote INTO the bundled roster"
    finally:
        try:
            os.remove(victim)  # defensive: never leave an artifact in the repo roster
        except OSError:
            pass
        import shutil as _sh
        _sh.rmtree(cwd, ignore_errors=True)


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
        ra, _, _, _, perm, model, _, _ = load_agent(d, "raw")
        assert perm == "yolo" and model == "claude-sonnet-5" and ra == "claude"
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_bundled_roster_fallback_precedence_and_read_only():
    """A fresh install (empty project roster) still dispatches the bundled
    starter agents, the project roster always shadows the bundled one, and a
    name in neither still raises — the fallback is a lookup path, not a catch-all
    and never a write target."""
    from pathlib import Path as _P

    import _loader
    from _loader import list_agents, load_agent
    primary = tempfile.mkdtemp(prefix="summon-primary-")
    bundled = tempfile.mkdtemp(prefix="summon-bundled-")
    orig = _loader.bundled_roster_dir
    try:
        with open(os.path.join(bundled, "planner.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nrun-agent: claude\npermission: safe-edit\n---\n# Planner (bundled)\n")
        with open(os.path.join(bundled, "reviewer.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nrun-agent: codex\n---\n# Reviewer (bundled)\n")
        _loader.bundled_roster_dir = lambda: bundled

        # (1) empty primary -> falls back to the bundled file
        ra, _, _, fpath, _, _, _, _ = load_agent(primary, "planner")
        assert ra == "claude" and _P(fpath).resolve().parent == _P(bundled).resolve()

        # (2) a project agent of the same name shadows the bundled one
        with open(os.path.join(primary, "planner.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nrun-agent: cursor-agent\n---\n# Planner (project)\n")
        ra2, _, _, fpath2, _, _, _, _ = load_agent(primary, "planner")
        assert ra2 == "cursor-agent" and _P(fpath2).resolve().parent == _P(primary).resolve()

        # (3) list merges both, no duplicate on the shadowed name, bundled-only kept
        names = [a["name"] for a in list_agents(primary)]
        assert names.count("planner") == 1 and "reviewer" in names

        # (4) a name in neither dir still raises (not silently satisfied)
        try:
            load_agent(primary, "no_such_agent_zzz")
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass
    finally:
        _loader.bundled_roster_dir = orig
        import shutil as _sh
        _sh.rmtree(primary, ignore_errors=True)
        _sh.rmtree(bundled, ignore_errors=True)


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
        # API reported the model on the terminal response -> served evidence
        assert env["model"] == {"requested": "test-model", "targeted": "test-model",
                                "served": "test-model", "resolved": "test-model",
                                "models_used": []}
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
            # round-2 dispatches (tag g<N>-r2-*) emit a RANKING so consensus aggregates
            rank = "\nRANKING: A, B" if "-r2-" in tag else ""
            return {"status": "success", "result": f"{agent} says go{rank}",
                    "model": {"resolved": "claude-sonnet-5"},
                    "report": {"summary": f"{agent}: pick X"}}
        orig = _council._dispatch
        _council._dispatch = fake_dispatch
        try:
            args = argparse.Namespace(question="X or Y?", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=2,
                                      cwd=os.getcwd(), agents_dir=d, timeout=90000,
                                      run_dir=d)
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
    base["run_dir"] = d
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
    assert eff == "claude-opus-4-8" and note, (eff, note)
    # argv carries the fallback alias, not fable
    _, args, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="claude-fable-5"))
    assert _models(args) == ["claude-opus-4-8"], _models(args)

    # CC3: credit-only model flags in `args:` are scrubbed (both forms)
    _, a1, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="opus",
                                    extra_args=["--fallback-model", "claude-fable-5"]))
    assert "claude-fable-5" not in a1
    _, a2, _ = _bia(AgentInvocation(cli="claude", prompt="x", cwd=".", model="claude-fable-5",
                                    extra_args=["--model", "claude-fable-5"]))
    assert _models(a2) == ["claude-opus-4-8"], _models(a2)

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


def test_effort_frontmatter_backends_and_envelope():
    import _builder, _executor
    from _builder import AgentInvocation, build_invocation_args
    from _loader import load_agent
    # `effort:` frontmatter is parsed (the 8th load_agent field)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "e.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nrun-agent: claude\neffort: high\n---\nbody")
        assert load_agent(d, "e")[7] == "high"
    # codex maps effort -> model_reasoning_effort, clamping claude's xhigh/max to high
    _, a, _ = build_invocation_args(AgentInvocation(cli="codex", prompt="x", cwd=".", effort="max"))
    assert "model_reasoning_effort=high" in " ".join(a), a
    _, a, _ = build_invocation_args(AgentInvocation(cli="codex", prompt="x", cwd=".", effort="low"))
    assert "model_reasoning_effort=low" in " ".join(a)
    # claude passes --effort verbatim
    _, a, _ = build_invocation_args(AgentInvocation(cli="claude", prompt="x", cwd=".", effort="xhigh"))
    assert "--effort" in a and a[a.index("--effort") + 1] == "xhigh"
    # agy: thinking is a model-name suffix (Gemini Low/Medium/High), clamped for xhigh/max
    import run_subagent as _R
    assert _R._apply_gemini_thinking("Gemini 3.1 Pro (High)", "low") == "Gemini 3.1 Pro (Low)"
    assert _R._apply_gemini_thinking("Gemini 3.5 Flash", "max") == "Gemini 3.5 Flash (High)"
    # envelope surfaces the applied effort
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv, timeout_ms=None: ("nope", [], None)
    try:
        r = _executor.execute_agent(AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(), effort="high"), timeout_ms=500)
    finally:
        _executor.build_invocation_args = orig
    assert r.get("effort") == "high", r.get("effort")


def test_manifest_path_resolution_and_agy_codex_telemetry():
    import _manifest, _executor
    from _builder import AgentInvocation
    # #1b: relative json_schema/debug_dir resolve against the MANIFEST dir
    with tempfile.TemporaryDirectory() as d:
        jobs, err = _manifest._normalize_jobs(
            {"jobs": [{"id": "j", "agent": "reviewer", "prompt": "p",
                       "json_schema": "s.json", "debug_dir": "dbg"}]}, d)
        assert err is None, err
        assert jobs[0]["json_schema"] == os.path.join(d, "s.json"), jobs[0]["json_schema"]
        assert jobs[0]["debug_dir"] == os.path.join(d, "dbg"), jobs[0]["debug_dir"]
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv, timeout_ms=None: ("nope", [], None)
    try:
        ra = _executor.execute_agent(
            AgentInvocation(cli="agy", prompt="Read seat_ar_editor.md and review", cwd=os.getcwd()), timeout_ms=500)
        rc = _executor.execute_agent(AgentInvocation(cli="codex", prompt="x", cwd=os.getcwd()), timeout_ms=500)
    finally:
        _executor.build_invocation_args = orig
    # #3: agy "read <file>" prompt surfaces a can't-read-files warning
    assert any("CANNOT read files" in w for w in ra.get("warnings", [])), ra.get("warnings")
    # #4: codex model.resolved falls back to the config default (when one is configured)
    from _resolver import _codex_default_model
    dflt = _codex_default_model()
    if dflt:
        assert rc["model"]["resolved"] == dflt, rc["model"]


def test_council_model_label_and_repo_capable_defaults():
    import _council as c
    # never blank: falls back to the requested model when the backend didn't resolve one
    assert c._model_label({"model": {"requested": "gpt-5.6-sol", "resolved": None}}) == "gpt-5.6-sol"
    # alias -> version made explicit
    assert c._model_label({"model": {"requested": "opus", "resolved": "claude-opus-4-7"}}) == "opus -> claude-opus-4-7"
    assert c._model_label({"model": {"requested": "m", "resolved": "m"}}) == "m"
    assert c._model_label({}) is None
    # default council is repo-capable — no agy member (agy can't read --cwd)
    assert "researcher" not in c.DEFAULT_MEMBERS, c.DEFAULT_MEMBERS


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


def test_preflight_openai_compat_skipped():
    # openai-compat has no binary; its HTTP errors are already structured.
    import run_subagent as r
    assert r._preflight_backend("openai-compat") is None


def test_preflight_present_backend_passes():
    # A backend on PATH pre-flights clean; real auth/runtime errors surface later.
    import run_subagent as r
    orig = r.shutil.which
    try:
        r.shutil.which = lambda name: "/usr/local/bin/" + name
        assert r._preflight_backend("codex") is None
    finally:
        r.shutil.which = orig


def test_preflight_missing_backend_returns_setup_error():
    # A missing CLI becomes an actionable setup error, not a raw spawn failure.
    import run_subagent as r
    import _doctor
    ow, od = r.shutil.which, _doctor.doctor
    try:
        r.shutil.which = lambda name: None
        _doctor.doctor = lambda a=None, b=None: {"usable_backends": ["claude"]}
        err = r._preflight_backend("codex")
        assert err is not None
        assert err["status"] == "error"
        assert err["cli"] == "codex"
        assert err["setup"]["backend"] == "codex"
        assert err["setup"]["usable_backends"] == ["claude"]
        assert err["warnings"]
        assert err["envelope"] == r._ENVELOPE_VERSION
        # documented error-envelope contract: result + exit_code 127 (CLI not found)
        assert err["result"] == ""
        assert err["exit_code"] == 127
        # actionable: names the install command AND the usable pivot backend
        assert "install" in err["error"].lower()
        assert "claude" in err["error"]
    finally:
        r.shutil.which, _doctor.doctor = ow, od


def test_preflight_unknown_backend_deferred():
    # A typo'd/unsupported backend name is NOT mislabeled "not installed"; it
    # returns None so downstream validation raises a proper "unknown backend".
    import run_subagent as r
    ow = r.shutil.which
    try:
        r.shutil.which = lambda name: None
        assert r._preflight_backend("totally-not-a-backend") is None
    finally:
        r.shutil.which = ow


def test_preflight_survives_missing_doctor():
    # An incomplete install missing _doctor.py must still yield a setup message,
    # never an uncaught ImportError from the pre-flight.
    import sys as _sys
    import run_subagent as r
    ow = r.shutil.which
    saved = _sys.modules.get("_doctor")
    try:
        r.shutil.which = lambda name: None
        _sys.modules["_doctor"] = None  # forces `from _doctor import ...` to raise
        err = r._preflight_backend("codex")
        assert err is not None and err["status"] == "error"
        assert err["exit_code"] == 127
        assert err["setup"]["usable_backends"] == []
    finally:
        r.shutil.which = ow
        if saved is not None:
            _sys.modules["_doctor"] = saved
        else:
            _sys.modules.pop("_doctor", None)


def test_preflight_no_usable_backend_points_to_doctor():
    # With nothing usable, steer the user to the full `doctor` checklist.
    import run_subagent as r
    import _doctor
    ow, od = r.shutil.which, _doctor.doctor
    try:
        r.shutil.which = lambda name: None
        _doctor.doctor = lambda a=None, b=None: {"usable_backends": []}
        err = r._preflight_backend("agy")
        assert err is not None and "doctor" in err["error"]
    finally:
        r.shutil.which, _doctor.doctor = ow, od


def test_preflight_doctor_failure_is_soft():
    # If the enrichment probe itself raises, still return a clean error envelope.
    import run_subagent as r
    import _doctor
    ow, od = r.shutil.which, _doctor.doctor
    try:
        r.shutil.which = lambda name: None

        def _boom(a=None, b=None):
            raise RuntimeError("probe exploded")

        _doctor.doctor = _boom
        err = r._preflight_backend("gemini")
        assert err is not None and err["status"] == "error"
        assert err["setup"]["usable_backends"] == []
    finally:
        r.shutil.which, _doctor.doctor = ow, od


def test_mode_flag_matrix_rejects_swallowed_flags():
    # Flags a fan-out mode does not consume must be REJECTED, not silently
    # dropped (field case: council --out never written). Zero paid dispatches:
    # rejection happens before any backend work, so these run fast.
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    cases = [
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--model", "opus"],
         "--model"),
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--worktree"],
         "--worktree"),
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--json-schema", "s.json"],
         "--json-schema"),
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--background"],
         "--background"),
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--retries", "2"],
         "--retries"),
        (["--manifest", "jobs.json", "--out", "o.json"], "--out"),
        (["--manifest", "jobs.json", "--worktree", "w"], "--worktree"),
        (["--manifest", "jobs.json", "--background"], "--background"),
        (["--manifest", "jobs.json", "--list"], "--list"),
    ]
    for argv, flag in cases:
        r = sp.run([sys.executable, script, *argv], capture_output=True, text=True,
                   encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "error" and flag in env["error"], (argv, env)
        assert "silently ignored" in env["error"], env["error"]
        assert r.returncode == 1, (argv, r.returncode)
    # Supported flags still pass the matrix: council --out reaches council
    # validation (fails on the missing question, NOT on the flag matrix).
    r = sp.run([sys.executable, script, "--council", "--cwd", os.getcwd(),
                "--out", os.path.join(tempfile.gettempdir(), "cx.json")],
               capture_output=True, text=True, encoding="utf-8")
    env = _json.loads(r.stdout)
    assert "silently ignored" not in (env.get("error") or ""), env
    assert "--question" in env["error"], env


def test_council_out_checkpoints_and_final():
    # --out must hold a round1_complete checkpoint BY THE TIME the chairman
    # runs (that snapshot is what survives a host-tool kill mid-synthesis),
    # then be replaced by the final envelope.
    import _council, argparse, io, contextlib, json as _json
    d = tempfile.mkdtemp(prefix="summon-cout-")
    out = os.path.join(d, "council.json")
    seen = {}
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\nrole.\n")

        def fake_dispatch(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
            if agent == "chair":
                with open(out, encoding="utf-8") as fh:  # checkpoint must already exist
                    seen["at_chair"] = _json.load(fh)
                return {"status": "success", "result": "DECISION: X",
                        "report": {"summary": "X"}}
            return {"status": "success", "result": f"{agent} ok",
                    "report": {"summary": f"{agent} pos"}}

        orig = _council._dispatch
        _council._dispatch = fake_dispatch
        try:
            args = argparse.Namespace(question="X or Y?", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=1,
                                      cwd=os.getcwd(), agents_dir=d, timeout=90000,
                                      out=out, run_dir=d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = _council.run_council(args)
        finally:
            _council._dispatch = orig
        assert rc == 0
        cp = seen["at_chair"]
        assert cp["council_state"] == "round1_complete" and cp["status"] == "in_progress"
        assert len(cp["members"]) == 2 and all("_raw" not in m and "_env" not in m
                                               for m in cp["members"])
        # checkpoints carry the FULL member envelopes, not just capped summaries
        assert len(cp["member_envelopes"]) == 2, cp.get("member_envelopes")
        assert all("result" in e and e.get("status") == "success"
                   for e in cp["member_envelopes"]), cp["member_envelopes"]
        final = _json.loads(open(out, encoding="utf-8").read())
        assert final["council_state"] == "final" and final["status"] == "success"
        assert "member_envelopes" not in final  # final keeps the v1 shape
        assert all("_env" not in m for m in final["members"])
        stdout_env = _json.loads(buf.getvalue())
        assert stdout_env == final  # file and stdout agree
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_council_out_written_on_fail_and_question_conflict():
    import _council, argparse, io, contextlib, json as _json
    d = tempfile.mkdtemp(prefix="summon-cfail-")
    try:
        out = os.path.join(d, "fail.json")
        # validation failure -> error envelope lands in --out too
        args = argparse.Namespace(question="", question_file=None, members=None,
                                  chairman=None, rounds=1, cwd=os.getcwd(),
                                  agents_dir=d, timeout=60000, out=out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _council.run_council(args)
        assert rc == 1
        env = _json.loads(open(out, encoding="utf-8").read())
        assert env["status"] == "error" and env["council_state"] == "failed"
        # question + question-file together is ambiguous -> rejected
        qf = os.path.join(d, "q.md")
        open(qf, "w", encoding="utf-8").write("file question")
        args2 = argparse.Namespace(question="inline too", question_file=qf,
                                   members=None, chairman=None, rounds=1,
                                   cwd=os.getcwd(), agents_dir=d, timeout=60000,
                                   out=None)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = _council.run_council(args2)
        env2 = _json.loads(buf2.getvalue())
        assert rc2 == 1 and "not both" in env2["error"], env2
        # reverse-empty escape: an EMPTY --question-file with --question is
        # still two competing inputs (presence on both sides)
        args3 = argparse.Namespace(question="q", question_file="",
                                   members=None, chairman=None, rounds=1,
                                   cwd=os.getcwd(), agents_dir=d, timeout=60000,
                                   out=None)
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            rc3 = _council.run_council(args3)
        env3 = _json.loads(buf3.getvalue())
        assert rc3 == 1 and "not both" in env3["error"], env3
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_council_ceiling_estimate_on_stderr():
    # The additive-clock preflight line: 2 claude members = 1 wave/round; with
    # rounds=2 and timeout 90s (+60s margin) -> 2*1*150 + 150 = 450s.
    import _council, argparse, io, contextlib
    d = tempfile.mkdtemp(prefix="summon-ceil-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        def fake(agent, *a, **k):
            return {"status": "success", "result": "ok", "report": {"summary": "s"}}
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            args = argparse.Namespace(question="q", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=2,
                                      cwd=os.getcwd(), agents_dir=d, timeout=90000,
                                      out=None, run_dir=d)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                _council.run_council(args)
        finally:
            _council._dispatch = orig
        text = err.getvalue()
        assert "worst-case wall clock ~450s" in text, text
        assert "ABOVE" in text, text
        # homogeneous 4-member council: ceil(4/3)=2 waves -> 1x2x150 + 150 = 450s
        for a in ("m3", "m4"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        _council._dispatch = fake
        try:
            args4 = argparse.Namespace(question="q", question_file=None,
                                       members="m1,m2,m3,m4", chairman="chair",
                                       rounds=1, cwd=os.getcwd(), agents_dir=d,
                                       timeout=90000, out=None, run_dir=d)
            err4 = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err4):
                _council.run_council(args4)
        finally:
            _council._dispatch = orig
        text4 = err4.getvalue()
        assert "worst-case wall clock ~450s" in text4 and "2 wave(s)" in text4, text4
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_manifest_rejects_prompt_and_prompt_file():
    import _manifest as m
    jobs, err = m._normalize_jobs(
        {"jobs": [{"agent": "a", "prompt": "p", "prompt_file": "f.md"}]}, os.getcwd())
    assert jobs is None and "not both" in err, (jobs, err)
    # defaults-level prompt_file + per-job prompt is the sneaky variant
    jobs2, err2 = m._normalize_jobs(
        {"defaults": {"prompt_file": "f.md"}, "jobs": [{"agent": "a", "prompt": "p"}]},
        os.getcwd())
    assert jobs2 is None and "not both" in err2, (jobs2, err2)
    # presence, not truthiness: an EMPTY prompt plus prompt_file is still ambiguous
    jobs3, err3 = m._normalize_jobs(
        {"jobs": [{"agent": "a", "prompt": "", "prompt_file": "f.md"}]}, os.getcwd())
    assert jobs3 is None and "not both" in err3, (jobs3, err3)


def test_prompt_file_load_conflicts_and_bom():
    import json as _json
    import subprocess as sp
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-pf-")
    try:
        # A tiny probe agent: the dry-run view truncates argv tokens at 400
        # chars, so the user prompt must land inside that window to be assertable.
        agents = os.path.join(d, "roster")
        os.makedirs(agents)
        open(os.path.join(agents, "pf-probe.md"), "w", encoding="utf-8").write(
            "---\nrun-agent: codex\npermission: read-only\n---\n# probe\ntiny.\n")
        pf = os.path.join(d, "task.md")
        with open(pf, "w", encoding="utf-8-sig") as fh:  # utf-8 WITH BOM on purpose
            fh.write("Review THE-MAGIC-TOKEN please → carefully")
        # happy path via --dry-run: prompt content reaches the resolved argv,
        # BOM stripped, no dispatch executed
        r = sp.run([sys.executable, script, "--agent", "pf-probe", "--prompt-file", pf,
                    "--cwd", os.getcwd(), "--agents-dir", agents, "--dry-run"],
                   capture_output=True, text=True, encoding="utf-8")
        view = _json.loads(r.stdout)
        assert view.get("dry_run") is True, view
        assert any("THE-MAGIC-TOKEN" in a for a in view["args"]), view["args"]
        assert not any("﻿" in a for a in view["args"])
        # --prompt + --prompt-file -> rejected
        r2 = sp.run([sys.executable, script, "--agent", "pf-probe", "--prompt", "x",
                     "--prompt-file", pf, "--cwd", os.getcwd(), "--agents-dir", agents],
                    capture_output=True, text=True, encoding="utf-8")
        env2 = _json.loads(r2.stdout)
        assert env2["status"] == "error" and "not both" in env2["error"], env2
        # missing file -> clean error, no traceback
        r3 = sp.run([sys.executable, script, "--agent", "pf-probe",
                     "--prompt-file", os.path.join(d, "nope.md"),
                     "--cwd", os.getcwd(), "--agents-dir", agents],
                    capture_output=True, text=True, encoding="utf-8")
        env3 = _json.loads(r3.stdout)
        assert env3["status"] == "error" and "cannot read --prompt-file" in env3["error"], env3
        # empty file -> clean error
        ef = os.path.join(d, "empty.md")
        open(ef, "w", encoding="utf-8").close()
        r4 = sp.run([sys.executable, script, "--agent", "pf-probe", "--prompt-file", ef,
                     "--cwd", os.getcwd(), "--agents-dir", agents],
                    capture_output=True, text=True, encoding="utf-8")
        env4 = _json.loads(r4.stdout)
        assert env4["status"] == "error" and "is empty" in env4["error"], env4
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_prompt_file_and_allow_credit_in_child_argv():
    # A --background child re-reads the FILE (small argv, no mojibake) and
    # keeps the credit authorization.
    import argparse
    import run_subagent as r
    ns = argparse.Namespace(agent="a", prompt="LOADED-TEXT", prompt_file="C:/t/p.md",
                            allow_credit=True, cwd="C:/w", agents_dir=None,
                            timeout=600000, cli=None, model=None, effort=None,
                            resume=None, resume_profile=None, out=None,
                            json_schema=None, debug_dir=None, retries=0, worktree=None)
    argv = r._child_argv(ns, "res.json")
    assert "--prompt-file" in argv and "C:/t/p.md" in argv, argv
    assert "LOADED-TEXT" not in argv, argv
    assert "--allow-credit" in argv, argv
    ns.prompt_file, ns.allow_credit = None, False
    argv2 = r._child_argv(ns, "res.json")
    assert "--prompt" in argv2 and "LOADED-TEXT" in argv2 and "--allow-credit" not in argv2


def test_allow_credit_flag_dry_run_and_fanout_rejection():
    import json as _json
    import subprocess as sp
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "run_subagent.py")
    agents = os.path.join(here, "..", "agents")
    # env scrubbed of any ambient authorization so the test is deterministic
    env = {k: v for k, v in os.environ.items()
           if k not in ("SUMMON_ALLOW_FABLE", "SUMMON_ALLOW_CREDIT", "ANTHROPIC_API_KEY")}
    base = [sys.executable, script, "--agent", "planner", "--prompt", "x",
            "--cwd", os.getcwd(), "--agents-dir", agents,
            "--model", "claude-fable-5", "--dry-run"]
    r = sp.run(base, capture_output=True, text=True, encoding="utf-8", env=env)
    view = _json.loads(r.stdout)
    assert view["model_effective"] == "claude-opus-4-8", view  # guard fell back
    r2 = sp.run(base + ["--allow-credit"], capture_output=True, text=True,
                encoding="utf-8", env=env)
    view2 = _json.loads(r2.stdout)
    assert view2["model_effective"] == "claude-fable-5", view2   # authorized
    assert view2["billing_predicted"]["source"] == "credit", view2
    # fan-out modes must REJECT the flag (env inheritance would silently
    # authorize every child)
    r3 = sp.run([sys.executable, script, "--council", "--question", "q",
                 "--cwd", os.getcwd(), "--allow-credit"],
                capture_output=True, text=True, encoding="utf-8", env=env)
    env3 = _json.loads(r3.stdout)
    assert env3["status"] == "error" and "--allow-credit" in env3["error"], env3


def test_agy_safe_edit_warning_helper_and_dry_run():
    from _builder import agy_permission_warning
    assert agy_permission_warning("agy", "safe-edit")
    assert agy_permission_warning("agy", "yolo") is None
    assert agy_permission_warning("agy", "read-only") is None
    assert agy_permission_warning("claude", "safe-edit") is None
    import json as _json
    import subprocess as sp
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-agyw-")
    try:
        open(os.path.join(d, "agy-agent.md"), "w", encoding="utf-8").write(
            "---\nrun-agent: agy\npermission: safe-edit\n---\n# agy agent\nrole.\n")
        r = sp.run([sys.executable, script, "--agent", "agy-agent", "--prompt", "x",
                    "--cwd", os.getcwd(), "--agents-dir", d, "--dry-run"],
                   capture_output=True, text=True, encoding="utf-8")
        view = _json.loads(r.stdout)
        warns = view.get("warnings") or []
        assert sum("workspace-write tier" in w for w in warns) == 1, warns
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_stream_handshake_separate_from_terminal_model():
    # The init handshake announces the TARGETED model before any inference; it
    # must never fill the terminal/served slot (field case: a failed Fable run
    # reported resolved: claude-fable-5 with all-zero usage).
    from _stream import StreamProcessor
    p = StreamProcessor()
    p.process_line('{"type":"system","subtype":"init","session_id":"s1","model":"claude-fable-5"}')
    assert p.handshake_model == "claude-fable-5" and p.model is None
    p.process_line('{"type":"result","is_error":true,"result":"","usage":{"output_tokens":0}}')
    assert p.model is None and p.handshake_model == "claude-fable-5"
    p2 = StreamProcessor()
    p2.process_line('{"type":"thread.started","thread_id":"t1","model":"gpt-x"}')
    assert p2.handshake_model == "gpt-x" and p2.model is None
    # a terminal event that DOES name a model still lands in .model (served lane)
    p3 = StreamProcessor()
    p3.process_line('{"type":"system","subtype":"init","model":"claude-h"}')
    p3.process_line('{"type":"result","result":"ok","model":"claude-t"}')
    assert p3.handshake_model == "claude-h" and p3.model == "claude-t"


def test_receipt_and_model_evidence_on_error_dispatch():
    # One real (unpaid) dispatch to a dead local endpoint: the error envelope
    # must carry the full provenance receipt AND honest model evidence.
    import hashlib
    import json as _json
    import subprocess as sp
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-rcpt-")
    try:
        af = os.path.join(d, "dead-api.md")
        open(af, "w", encoding="utf-8").write(
            "---\nrun-agent: openai-compat\nbase_url: http://127.0.0.1:9\n"
            "api_key_env:\nmodel: probe-model\n---\n# dead api\nrole.\n")
        r = sp.run([sys.executable, script, "--agent", "dead-api", "--prompt", "hello",
                    "--cwd", os.getcwd(), "--agents-dir", d, "--timeout", "8s"],
                   capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "error", env
        # model honesty: pointed at probe-model, nothing served
        assert env["model"]["targeted"] == "probe-model", env["model"]
        assert env["model"]["served"] is None, env["model"]
        # receipt: dispatcher identity + agent identity + prompt hash + git head
        s = env["summon"]
        assert s["version"] and len(s["scripts_sha256"]) == 64, s
        assert os.path.basename(s["script"]) == "run_subagent.py", s
        ad = env["agent_def"]
        assert ad["file"].endswith("dead-api.md") and ad["source"] == "explicit", ad
        import pathlib
        assert pathlib.Path(ad["agents_dir"]) == pathlib.Path(d).resolve(), ad
        with open(af, "rb") as fh:
            assert ad["sha256"] == hashlib.sha256(fh.read()).hexdigest(), ad
        assert env["prompt_sha256"] == hashlib.sha256(b"hello").hexdigest(), env
        gh = sp.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                    cwd=os.getcwd())
        head = gh.stdout.strip() if gh.returncode == 0 else ""
        assert env.get("git_head_before") == (head or None), (env.get("git_head_before"), head)
        # a different cwd: the key is present and matches git's OWN answer for
        # that directory (None where git finds no repo; a tempdir can legally
        # sit under an enclosing repo, e.g. a dotfiles-managed home, and git's
        # walk-up semantics are the correct provenance there)
        r5 = sp.run([sys.executable, script, "--agent", "dead-api", "--prompt", "hello",
                     "--cwd", d, "--agents-dir", d, "--timeout", "8s"],
                    capture_output=True, text=True, encoding="utf-8")
        env5 = _json.loads(r5.stdout)
        gh5 = sp.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True)
        head5 = gh5.stdout.strip() if gh5.returncode == 0 else ""
        assert "git_head_before" in env5, env5
        assert env5["git_head_before"] == (head5 or None), (env5["git_head_before"], head5)
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_receipt_helper_deterministic_and_sources():
    import argparse
    import run_subagent as r
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.abspath(os.path.join(here, "..", "agents"))
    planner = os.path.join(bundled, "planner.md")
    saved = os.environ.pop("SUB_AGENTS_DIR", None)
    try:
        assert r._receipt_base() == r._receipt_base()  # deterministic
        ns = argparse.Namespace(agents_dir=None, prompt="p")
        a1 = r._receipt_agent(ns, planner)
        assert a1["agent_def"]["source"] == "bundled", a1["agent_def"]
        # agents_dir records the dir that ACTUALLY served the file, absolute
        import pathlib
        assert pathlib.Path(a1["agent_def"]["agents_dir"]) == pathlib.Path(bundled).resolve()
        # explicit --agents-dir wins the label when the file is not bundled
        ns2 = argparse.Namespace(agents_dir="rel/dir", prompt="p")
        a2 = r._receipt_agent(ns2, os.path.join(here, "run_subagent.py"))
        assert a2["agent_def"]["source"] == "explicit", a2["agent_def"]
        assert os.path.isabs(a2["agent_def"]["agents_dir"])  # never relative
        # default chain -> project; env tier -> env; prompt hash only when given
        ns3 = argparse.Namespace(agents_dir=None, prompt=None)
        a3 = r._receipt_agent(ns3, os.path.join(here, "run_subagent.py"))
        assert a3["agent_def"]["source"] == "project", a3["agent_def"]
        os.environ["SUB_AGENTS_DIR"] = "/e"
        a4 = r._receipt_agent(ns3, os.path.join(here, "run_subagent.py"))
        assert a4["agent_def"]["source"] == "env", a4["agent_def"]
        assert r._receipt_prompt(None) == {} and "prompt_sha256" in r._receipt_prompt("x")
    finally:
        os.environ.pop("SUB_AGENTS_DIR", None)
        if saved is not None:
            os.environ["SUB_AGENTS_DIR"] = saved


def test_mode_matrix_default_values_and_early_combos():
    # Presence-based detection: a flag equal to its default is still explicit;
    # and query modes must not run while silently dropping the fan-out mode.
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    cases = [
        (["--manifest", "jobs.json", "--timeout", "600000"], "--timeout"),  # == default
        (["--manifest", "jobs.json", "--doctor"], "--doctor"),
        (["--manifest", "jobs.json", "--list-models"], "--list-models"),
        (["--council", "--question", "x", "--cwd", os.getcwd(), "--doctor"], "--doctor"),
    ]
    for argv, flag in cases:
        r = sp.run([sys.executable, script, *argv], capture_output=True, text=True,
                   encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "error" and flag in env["error"], (argv, env)
        assert r.returncode == 1, (argv, r.returncode)
    # empty values on EITHER side are still two competing inputs (presence)
    r2 = sp.run([sys.executable, script, "--agent", "a", "--prompt", "",
                 "--prompt-file", "x.md", "--cwd", os.getcwd()],
                capture_output=True, text=True, encoding="utf-8")
    env2 = _json.loads(r2.stdout)
    assert env2["status"] == "error" and "not both" in env2["error"], env2
    r3 = sp.run([sys.executable, script, "--agent", "a", "--prompt", "x",
                 "--prompt-file", "", "--cwd", os.getcwd()],
                capture_output=True, text=True, encoding="utf-8")
    env3 = _json.loads(r3.stdout)
    assert env3["status"] == "error" and "not both" in env3["error"], env3


def test_receipt_on_missing_agent_and_preflight():
    # Provenance matters MOST when the dispatch fails early: a missing agent
    # (which install / roster looked?) and a missing backend must both carry
    # the receipt; preflight also carries git_head_before.
    import json as _json
    import subprocess as sp
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    d = tempfile.mkdtemp(prefix="summon-rmiss-")
    try:
        # missing agent -> error envelope with summon identity, no agent_def
        r = sp.run([sys.executable, script, "--agent", "nope-agent-xyz", "--prompt", "p",
                    "--cwd", os.getcwd(), "--agents-dir", d],
                   capture_output=True, text=True, encoding="utf-8")
        env = _json.loads(r.stdout)
        assert env["status"] == "error" and "not found" in env["error"], env
        assert len(env["summon"]["scripts_sha256"]) == 64, env.get("summon")
        assert "git_head_before" in env and "agent_def" not in env, env
        # the ROOT prompt hash is already known and must be present here too
        import hashlib as _hl
        assert env["prompt_sha256"] == _hl.sha256(b"p").hexdigest(), env
        # missing backend (PATH emptied) -> preflight setup error with FULL receipt
        open(os.path.join(d, "gm.md"), "w", encoding="utf-8").write(
            "---\nrun-agent: gemini\npermission: read-only\n---\n# gm\nrole.\n")
        env_clean = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
        env_clean["PATH"] = ""
        r2 = sp.run([sys.executable, script, "--agent", "gm", "--prompt", "p",
                     "--cwd", os.getcwd(), "--agents-dir", d],
                    capture_output=True, text=True, encoding="utf-8", env=env_clean)
        env2 = _json.loads(r2.stdout)
        assert env2["status"] == "error" and env2["exit_code"] == 127, env2
        assert len(env2["summon"]["scripts_sha256"]) == 64, env2.get("summon")
        assert env2["agent_def"]["file"].endswith("gm.md"), env2.get("agent_def")
        assert "prompt_sha256" in env2 and "git_head_before" in env2, env2
        # invalid effort (a post-load validation error) also carries the receipt.
        # Probed via openai-compat, which SKIPS backend preflight -- a CLI agent
        # would 127 on machines without that CLI before effort validation runs.
        open(os.path.join(d, "oc.md"), "w", encoding="utf-8").write(
            "---\nrun-agent: openai-compat\nbase_url: http://127.0.0.1:9\n"
            "api_key_env:\nmodel: m\n---\n# oc\nrole.\n")
        r3 = sp.run([sys.executable, script, "--agent", "oc", "--prompt", "p",
                     "--cwd", os.getcwd(), "--agents-dir", d, "--effort", "bogus"],
                    capture_output=True, text=True, encoding="utf-8")
        env3 = _json.loads(r3.stdout)
        assert env3["status"] == "error" and "invalid effort" in env3["error"], env3
        assert len(env3["summon"]["scripts_sha256"]) == 64, env3
        assert env3["agent_def"]["file"].endswith("oc.md"), env3
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_model_targeted_from_handshake_not_terminal():
    # targeted = handshake else guard-effective; the TERMINAL model is served
    # evidence and must never pollute targeted (their difference is the signal).
    import json as _json
    import _executor
    from _builder import AgentInvocation
    line1 = _json.dumps({"type": "system", "subtype": "init",
                         "model": "hand-A", "session_id": "s"})
    line2 = _json.dumps({"type": "result", "result": "ok", "model": "served-B",
                         "usage": {"output_tokens": 5}})
    py = f"print({line1!r});print({line2!r})"
    orig = _executor.build_invocation_args
    _executor.build_invocation_args = lambda inv, timeout_ms=None: (sys.executable, ["-c", py], None)
    try:
        out = _executor.execute_agent(
            AgentInvocation(cli="claude", prompt="x", cwd=os.getcwd(),
                            system_context="s", model="req-C"), timeout_ms=30000)
    finally:
        _executor.build_invocation_args = orig
    m = out["model"]
    assert m["requested"] == "req-C", m
    assert m["targeted"] == "hand-A", m       # handshake, NOT the terminal model
    assert m["served"] == "served-B", m       # terminal report = service evidence
    assert m["resolved"] == "served-B", m     # legacy v1: handshake-or-terminal


def test_council_out_write_failure_surfaces_out_error():
    # A checkpoint/final write failure never kills the council but must be
    # carried forward as out_error on the stdout envelope.
    import _council, argparse, io, contextlib, json as _json
    d = tempfile.mkdtemp(prefix="summon-cwerr-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(d, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        blocked = os.path.join(d, "iamadir")   # a DIRECTORY at the --out path:
        os.makedirs(blocked)                   # os.replace onto it fails on Windows+POSIX
        def fake(agent, *a, **k):
            return {"status": "success", "result": "ok", "report": {"summary": "s"}}
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            args = argparse.Namespace(question="q", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=1,
                                      cwd=os.getcwd(), agents_dir=d, timeout=60000,
                                      out=blocked, run_dir=d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = _council.run_council(args)
        finally:
            _council._dispatch = orig
        env = _json.loads(buf.getvalue())
        assert rc == 0 and env["status"] == "success", env.get("status")
        assert "out_error" in env and "failed to write" in env["out_error"], env.get("out_error")
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_council_fresh_run_persists_run_dir_artifacts():
    # The whole point of B1: a council run leaves a complete, resumable record.
    import _council, _rundir as rd, argparse, io, contextlib, json as _json, glob as _glob
    root = tempfile.mkdtemp(prefix="summon-cfresh-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(root, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        def fake(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
            if agent == "chair":
                return {"status": "success", "result": "DECISION: X",
                        "usage": {"output_tokens": 3}, "report": {"summary": "X"}}
            rank = "\nRANKING: A, B" if tag.startswith("g1-r2-") else ""
            return {"status": "success", "result": f"{agent} ok{rank}",
                    "usage": {"output_tokens": 2}, "report": {"summary": agent}}
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            args = argparse.Namespace(question="X or Y?", question_file=None,
                                      members="m1,m2", chairman="chair", rounds=2,
                                      cwd=os.getcwd(), agents_dir=root, timeout=90000,
                                      out=None, run_dir=root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = _council.run_council(args)
        finally:
            _council._dispatch = orig
        env = _json.loads(buf.getvalue())
        assert rc == 0 and env["run_id"].startswith("council-") and env["generation"] == 1
        run = env["run_dir"]
        assert os.path.isdir(run), run
        # receipt binds the run's inputs
        receipt = rd.read_json(os.path.join(run, "receipt.json"))
        assert receipt["question"] == "X or Y?" and receipt["members"] == ["m1", "m2"]
        # generation persisted; ownership released cleanly
        assert open(os.path.join(run, rd.GENERATION_FILE), encoding="utf-8").read().strip() == "1"
        assert rd.read_owner(run) is None
        # every stage landed generation-namespaced with its input hash
        names = {os.path.basename(p) for p in _glob.glob(os.path.join(run, "g1-*.json"))}
        assert {"g1-r1-m1.json", "g1-r1-m2.json", "g1-r2-m1.json", "g1-r2-m2.json",
                "g1-rankings.json", "g1-chairman.json"} <= names, names
        st = rd.read_json(os.path.join(run, "g1-r1-m1.json"))
        assert st["status"] == "success" and len(st["input_sha256"]) == 64
        # journal: started/finished per dispatch (5) + rankings computed
        recs, torn = rd.journal_read(run)
        assert not torn
        events = [r["event"] for r in recs]
        assert events.count("attempt_started") == 5, events
        assert events.count("attempt_finished") == 5, events
        assert "stage_computed" in events
        fin = [r for r in recs if r["event"] == "attempt_finished"]
        assert all(f["status"] == "success" and f["usage"] for f in fin)
        # derived state reached synthesis
        state = rd.read_json(os.path.join(run, "state.json"))
        assert state["phase"] == "synthesized" and state["stages"]["chairman"] == "success"
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def _council_stub_and_runner():
    """Shared fixture for resume tests: a counting stub + a run() helper."""
    import _council, argparse, io, contextlib, json as _json
    calls = {"n": 0}
    def fake(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
        calls["n"] += 1
        if agent == "chair":
            return {"status": "success", "result": "DECISION: X", "report": {"summary": "X"}}
        rank = "\nRANKING: A, B" if "-r2-" in tag else ""
        return {"status": "success", "result": f"{agent} ok{rank}",
                "report": {"summary": agent}}
    def run(ns):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = _council.run_council(ns)
        return rc, _json.loads(buf.getvalue())
    return _council, argparse, calls, fake, run


def test_council_resume_full_carry_then_selective_rerun():
    # The resume economics contract: unchanged work is NEVER re-paid; only the
    # missing stage re-runs, and its unchanged downstream still carries.
    _council, argparse, calls, fake, run = _council_stub_and_runner()
    root = tempfile.mkdtemp(prefix="summon-cres-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(root, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            rc1, env1 = run(argparse.Namespace(
                question="X or Y?", question_file=None, members="m1,m2",
                chairman="chair", rounds=2, cwd=os.getcwd(), agents_dir=root,
                timeout=90000, out=None, run_dir=root))
            assert rc1 == 0 and calls["n"] == 5, calls["n"]
            run_id = env1["run_id"]
            resume_ns = argparse.Namespace(
                question=None, question_file=None, members=None, chairman=None,
                rounds=None, cwd=os.getcwd(), agents_dir=root, timeout=90000,
                out=None, run_dir=root, resume_run=run_id)
            # resume with nothing changed: EVERYTHING carries, zero dispatches
            calls["n"] = 0
            rc2, env2 = run(resume_ns)
            assert rc2 == 0 and calls["n"] == 0, calls["n"]
            assert env2["run_id"] == run_id and env2["generation"] == 2
            assert env2["status"] == "success" and env2["consensus_ranking"], env2["status"]
            assert {m["agent"] for m in env2["members"]} == {"m1", "m2"}
            # drop every generation of ONE member's r1 -> exactly that re-runs
            for g in (1, 2):
                p = os.path.join(env2["run_dir"], f"g{g}-r1-m2.json")
                if os.path.isfile(p):
                    os.unlink(p)
            calls["n"] = 0
            rc3, env3 = run(resume_ns)
            assert rc3 == 0 and env3["generation"] == 3
            assert calls["n"] == 1, calls["n"]  # r1-m2 only; r2/rankings/chair carried
        finally:
            _council._dispatch = orig
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def test_council_resume_upstream_change_invalidates_downstream():
    # Generation hashes: a changed r1 OUTPUT flows into r2's INPUT sha, so r2,
    # rankings, and the chairman all re-run; their stale files get superseded.
    _council, argparse, calls, _unused_fake, run = _council_stub_and_runner()
    import _rundir as rd
    root = tempfile.mkdtemp(prefix="summon-cinv-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(root, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")

        # An INPUT-SENSITIVE stub: round-2 output must actually reflect the
        # tampered upstream position, or the chairman's inputs would be
        # genuinely unchanged and a carry would be the CORRECT outcome.
        def fake(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
            calls["n"] += 1
            if agent == "chair":
                return {"status": "success", "result": "DECISION: X",
                        "report": {"summary": "X"}}
            mark = "X" if "m1C" in prompt else ""
            rank = "\nRANKING: A, B" if "-r2-" in tag else ""
            return {"status": "success", "result": f"{agent}{mark} ok{rank}",
                    "report": {"summary": f"{agent}{mark}"}}
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            rc1, env1 = run(argparse.Namespace(
                question="X or Y?", question_file=None, members="m1,m2",
                chairman="chair", rounds=2, cwd=os.getcwd(), agents_dir=root,
                timeout=90000, out=None, run_dir=root))
            assert rc1 == 0
            rdir = env1["run_dir"]
            # tamper the r1-m1 OUTPUT (envelopes are authoritative; the tampered
            # position must invalidate everything downstream on resume)
            p = os.path.join(rdir, "g1-r1-m1.json")
            env = rd.read_json(p)
            env["result"] = "m1 CHANGED"
            env["report"] = {"summary": "m1C"}
            rd.atomic_write_json(p, env)
            calls["n"] = 0
            rc2, env2 = run(argparse.Namespace(
                question=None, question_file=None, members=None, chairman=None,
                rounds=None, cwd=os.getcwd(), agents_dir=root, timeout=90000,
                out=None, run_dir=root, resume_run=env1["run_id"]))
            assert rc2 == 0 and env2["generation"] == 2
            assert calls["n"] == 3, calls["n"]   # r2-m1 + r2-m2 + chairman
            # the changed upstream really propagated into round 2's outputs
            assert any(m["position"].endswith("X") for m in env2["members"]), env2["members"]
            # stale downstream files moved to superseded/, spend evidence intact
            sup = os.path.join(rdir, "superseded", "g1")
            assert os.path.isfile(os.path.join(sup, "g1-r2-m1.json")), os.listdir(rdir)
            assert os.path.isfile(os.path.join(sup, "g1-chairman.json"))
            assert not os.path.isfile(os.path.join(rdir, "g1-r2-m1.json"))
            # the CARRIED r1 originals stay in place
            assert os.path.isfile(os.path.join(rdir, "g1-r1-m1.json"))
            recs, _ = rd.journal_read(rdir)
            assert any(r["event"] == "superseded" for r in recs)
        finally:
            _council._dispatch = orig
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def test_council_status_snapshot():
    import _council, argparse, io, contextlib, json as _json
    _c, _a, calls, fake, run = _council_stub_and_runner()
    root = tempfile.mkdtemp(prefix="summon-cstat-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(root, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            rc1, env1 = run(_a.Namespace(
                question="q", question_file=None, members="m1,m2", chairman="chair",
                rounds=1, cwd=os.getcwd(), agents_dir=root, timeout=60000,
                out=None, run_dir=root))
        finally:
            _council._dispatch = orig
        assert rc1 == 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _council.run_council_status(_a.Namespace(
                council_status=env1["run_id"], run_dir=root, json=True,
                cwd=os.getcwd()))
        view = _json.loads(buf.getvalue())
        assert rc == 0 and view["consistent"] is True and view["owner"] is None
        assert view["phase"] == "synthesized"
        assert view["stages"]["chairman"]["status"] == "success"
        assert view["attempts"]["started"] == view["attempts"]["finished"] == 3
        assert view["abandoned_attempts"] == 0
        # human rendering is ASCII and mentions the run id
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = _council.run_council_status(_a.Namespace(
                council_status=env1["run_id"], run_dir=root, json=False,
                cwd=os.getcwd()))
        text = buf2.getvalue()
        assert rc2 == 0 and env1["run_id"] in text and text.isascii(), text[:200]
        # unknown id -> exit 1
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            rc3 = _council.run_council_status(_a.Namespace(
                council_status="council-00000000-000000-dead", run_dir=root,
                json=True, cwd=os.getcwd()))
        assert rc3 == 1 and "unknown" in _json.loads(buf3.getvalue())["error"]
        # takeover DURING a status scan -> consistent:false (owner nonce/gen
        # changes between the before-scan and after-scan reads, twice)
        import _rundir as rd
        seq = iter([{"nonce": "x" * 32, "generation": 1, "pid": 1, "lease_expires": 9e11},
                    {"nonce": "y" * 32, "generation": 2, "pid": 2, "lease_expires": 9e11},
                    {"nonce": "z" * 32, "generation": 3, "pid": 3, "lease_expires": 9e11},
                    {"nonce": "w" * 32, "generation": 4, "pid": 4, "lease_expires": 9e11}])
        orig_ro = rd.read_owner
        rd.read_owner = lambda run_dir: next(seq, None)
        try:
            buf4 = io.StringIO()
            with contextlib.redirect_stdout(buf4):
                _council.run_council_status(_a.Namespace(
                    council_status=env1["run_id"], run_dir=root, json=True, cwd=os.getcwd()))
            assert _json.loads(buf4.getvalue())["consistent"] is False
        finally:
            rd.read_owner = orig_ro
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def test_council_renews_lease_after_every_stage():
    # v3.1: renewal is PER STAGE, not per round, so a multi-wave round cannot
    # expire a live owner. The stub records the lease sidecar's expiry seen
    # before each dispatch; it must strictly advance.
    import _council, _rundir as rd, argparse, io, contextlib, json as _json
    root = tempfile.mkdtemp(prefix="summon-renew-")
    try:
        for a in ("m1", "m2", "chair"):
            open(os.path.join(root, a + ".md"), "w", encoding="utf-8").write(
                "---\nrun-agent: claude\npermission: safe-edit\n---\n# " + a + "\n")
        seen = []
        def fake(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag):
            # out_dir is the run dir; find the single lease-*.json sidecar
            import glob as _g
            for lp in _g.glob(os.path.join(out_dir, "lease-*.json")):
                s = rd.read_json(lp)
                if s:
                    seen.append(s["lease_expires"])
            return {"status": "success", "result": f"{agent} ok"
                    + ("\nRANKING: A, B" if "-r2-" in tag else ""),
                    "report": {"summary": agent}}
        orig = _council._dispatch
        _council._dispatch = fake
        try:
            args = argparse.Namespace(question="q", question_file=None, members="m1,m2",
                                      chairman="chair", rounds=2, cwd=os.getcwd(),
                                      agents_dir=root, timeout=90000, out=None, run_dir=root)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _council.run_council(args)
        finally:
            _council._dispatch = orig
        assert rc == 0
        # at least the r2 + chairman dispatches saw a sidecar; those values are
        # non-decreasing (renew after every stage only extends)
        assert len(seen) >= 2, seen
        assert seen == sorted(seen), seen
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def test_rundir_id_validation_and_containment():
    import _rundir as rd
    for good in ("council-20260718-1200-ab12", "a", "run.1_x-Y"):
        assert rd.validate_run_id(good) == good
    for bad in ("", "..", "a..b", "-lead", ".lead", "x" * 65, "a/b", "a\\b",
                "trailing.", "CON", "con", "NUL.txt", "com7", "LPT9.log", "prn.a.b"):
        try:
            rd.validate_run_id(bad)
            raise AssertionError(f"accepted bad id: {bad!r}")
        except ValueError:
            pass
    root = tempfile.mkdtemp(prefix="summon-runsroot-")
    try:
        p = rd.run_path(root, "ok-run")
        import pathlib
        assert pathlib.Path(p).parent == pathlib.Path(root).resolve()
        assert rd.stage_path("/r", 3, "r1-m1").endswith("g3-r1-m1.json")
        try:
            rd.stage_path("/r", 1, "bad/stage")
            raise AssertionError("accepted bad stage")
        except ValueError:
            pass
    finally:
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)


def test_rundir_owner_lifecycle_generations():
    import _rundir as rd
    d = tempfile.mkdtemp(prefix="summon-own-")
    try:
        o1 = rd.acquire_owner(d, lease_sec=600)
        assert o1.generation == 1
        # held -> clean error naming the owner
        try:
            rd.acquire_owner(d, lease_sec=600)
            raise AssertionError("second acquire should have failed")
        except rd.OwnerHeldError as e:
            assert e.pid == os.getpid()
        rd.renew_owner(o1)  # lease advances without error while ours
        rd.release_owner(o1)
        # clean-release resume claims generation max+1 (persisted outside the lock)
        o2 = rd.acquire_owner(d, lease_sec=600)
        assert o2.generation == 2, o2.generation
        rd.release_owner(o2)
        rd.release_owner(o2)  # double release is a no-op
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_rundir_takeover_fencing_and_foreign_lock():
    import json as _json
    import time
    import _rundir as rd
    d = tempfile.mkdtemp(prefix="summon-take-")
    try:
        o1 = rd.acquire_owner(d, lease_sec=600)
        # force-expire the lease on disk (simulates a suspended owner)
        lock = os.path.join(d, rd.OWNER_LOCK)
        data = _json.loads(open(lock, encoding="utf-8").read())
        data["lease_expires"] = time.time() - 5
        open(lock, "w", encoding="utf-8").write(_json.dumps(data))
        o2 = rd.acquire_owner(d, lease_sec=600)     # takeover
        assert o2.generation == o1.generation + 1
        # the deposed owner cannot renew, and its release must NOT remove the
        # successor's lock
        try:
            rd.renew_owner(o1)
            raise AssertionError("deposed owner renewed")
        except rd.OwnershipLostError:
            pass
        rd.release_owner(o1)
        assert rd.read_owner(d) and rd.read_owner(d)["nonce"] == o2.nonce
        rd.release_owner(o2)
        # foreign/malformed lock is NEVER auto-broken
        open(lock, "w", encoding="utf-8").write("{malformed")
        try:
            rd.acquire_owner(d, lease_sec=600)
            raise AssertionError("broke a foreign lock")
        except rd.OwnerLockForeignError:
            pass
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_rundir_journal_checksums_torn_tail_and_repair():
    import _rundir as rd
    d = tempfile.mkdtemp(prefix="summon-jrnl-")
    try:
        o = rd.acquire_owner(d, lease_sec=600)
        rd.journal_append(d, {"event": "started", "stage": "r1-m1", "n": 1})
        rd.journal_append(d, {"event": "finished", "stage": "r1-m1", "n": 1,
                              "status": "success", "note": "unicode → ok"})
        recs, torn = rd.journal_read(d)
        assert len(recs) == 2 and not torn
        assert recs[0]["event"] == "started" and "ts" in recs[0]
        # torn tail: a partial line is repairable, and repair records itself
        with open(os.path.join(d, rd.JOURNAL_FILE), "a", encoding="utf-8") as fh:
            fh.write('{"event":"finis')
        recs2, torn2 = rd.journal_read(d)
        assert torn2 and len(recs2) == 2
        assert rd.journal_repair(d, o) is True
        recs3, torn3 = rd.journal_read(d)
        assert not torn3 and recs3[-1]["event"] == "journal_repaired"
        # mid-file corruption raises, never auto-repairs
        lines = open(os.path.join(d, rd.JOURNAL_FILE), encoding="utf-8").read().splitlines()
        lines[0] = lines[0].replace("started", "sabotag")
        open(os.path.join(d, rd.JOURNAL_FILE), "w", encoding="utf-8").write("\n".join(lines) + "\n")
        try:
            rd.journal_read(d)
            raise AssertionError("mid-file corruption not detected")
        except rd.JournalCorruptError:
            pass
        rd.release_owner(o)
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_rundir_review_races_lock_fencing_and_journal():
    # The five interleavings the adversarial review REPRODUCED against the
    # first design, pinned forever.
    import json as _json
    import time
    import _rundir as rd
    d = tempfile.mkdtemp(prefix="summon-race-")
    try:
        # (1) crash between lock creation and generation.txt: the lock itself
        # names its generation, so a successor never reuses it
        lock = os.path.join(d, rd.OWNER_LOCK)
        now = time.time()
        open(lock, "w", encoding="utf-8").write(_json.dumps({
            "summon_owner": True, "nonce": "a" * 32, "pid": 1,
            "generation": 5, "acquired_at": now - 100, "lease_expires": now - 5}))
        o = rd.acquire_owner(d, lease_sec=600)
        assert o.generation == 6, o.generation
        # (2) renewal goes to the NONCE-NAMED sidecar and extends the effective
        # expiry without ever touching owner.lock (immutable => byte-testable)
        before = open(lock, "rb").read()
        base_exp = rd.read_owner(d)["lease_expires"]
        rd.renew_owner(o)
        assert open(lock, "rb").read() == before          # lock untouched
        side = rd.read_json(os.path.join(d, f"lease-{o.nonce}.json"))
        assert side and side["lease_expires"] > base_exp - 1
        assert rd._effective_expiry(d, rd.read_owner(d)) >= side["lease_expires"]
        # (3) release racing a successor: replace the lock with a successor's
        # record; the deposed owner's release must leave it in place
        succ = {"summon_owner": True, "nonce": "b" * 32, "pid": 2,
                "generation": 7, "acquired_at": now, "lease_expires": now + 600}
        open(lock, "w", encoding="utf-8").write(_json.dumps(succ))
        rd.release_owner(o)
        assert rd.read_owner(d)["nonce"] == "b" * 32     # successor intact
        # (4) journal fencing: the deposed owner's append RAISES and writes
        # nothing (single-writer guarantee survives a suspended parent)
        rd.journal_append(d, {"event": "probe"})          # unfenced write, baseline
        n_before = len(rd.journal_read(d)[0])
        try:
            rd.journal_append(d, {"event": "evil"}, owner=o)
            raise AssertionError("deposed owner journaled")
        except rd.OwnershipLostError:
            pass
        assert len(rd.journal_read(d)[0]) == n_before
        # (5) deposed carry-forward withdraws its copy and raises
        rd.atomic_write_json(rd.stage_path(d, 6, "r1-x"),
                             {"status": "success", "input_sha256": "c" * 64})
        try:
            rd.carry_forward(d, o, "r1-x", 6, "c" * 64)
            raise AssertionError("deposed owner carried forward")
        except rd.OwnershipLostError:
            pass
        assert not os.path.isfile(rd.stage_path(d, o.generation, "r1-x"))
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def test_rundir_carry_forward_validation():
    import _rundir as rd
    d = tempfile.mkdtemp(prefix="summon-carry-")
    try:
        o1 = rd.acquire_owner(d, lease_sec=600)
        sha = rd.content_sha256({"question": "q", "positions": ["a"]})
        rd.atomic_write_json(rd.stage_path(d, 1, "r1-m1"),
                             {"status": "success", "result": "pos", "input_sha256": sha})
        rd.atomic_write_json(rd.stage_path(d, 1, "r1-m2"),
                             {"status": "error", "input_sha256": sha})
        rd.release_owner(o1)
        o2 = rd.acquire_owner(d, lease_sec=600)
        # valid stage carries forward with provenance + journal record
        assert rd.carry_forward(d, o2, "r1-m1", 1, sha) is True
        copied = rd.read_json(rd.stage_path(d, 2, "r1-m1"))
        assert copied["carried_from_generation"] == 1 and copied["result"] == "pos"
        recs, _ = rd.journal_read(d)
        assert recs[-1]["event"] == "carried_forward"
        # non-success never carries; upstream-hash mismatch never carries AND
        # leaves NO current-generation residue (else the child --out skip reuses
        # the stale file instead of re-running)
        assert rd.carry_forward(d, o2, "r1-m2", 1, sha) is False
        assert not os.path.isfile(rd.stage_path(d, 2, "r1-m2"))
        assert rd.carry_forward(d, o2, "r1-m1", 1, "0" * 64) is False
        # generation fallback scan: delete generation.txt, files imply max=2
        os.unlink(os.path.join(d, rd.GENERATION_FILE))
        rd.release_owner(o2)
        o3 = rd.acquire_owner(d, lease_sec=600)
        assert o3.generation == 3, o3.generation
        rd.release_owner(o3)
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


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
