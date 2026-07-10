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
