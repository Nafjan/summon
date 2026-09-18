"""Provider-inert council pause/context/continue acceptance."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path
from unittest import mock

import pytest

import _council
import _council_between_round as between
import _cli
import _loader
import _rundir
from test_discovery import _council_stub_and_runner


def _args(root, *, rounds=2, pause=False, overall=300000):
    return argparse.Namespace(
        question="X or Y?", question_file=None, members="m1,m2",
        chairman="chair", rounds=rounds, cwd=str(root), agents_dir=str(root),
        timeout=60000, out=None, run_dir=str(root), results_dir=None,
        resume_run=None, council=True, pause_after_round=pause,
        overall_timeout=overall, member_timeout=None, chair_timeout=None,
        quorum=None, min_successful=None, chairman_fallback=None,
        strict_agents_dir=False, enable_roles=False,
        allow_kimi_acp_fallback=False, council_continue=False,
        between_round_context=None, council_original_deadline_unix_ms=None,
    )


def _run(ns):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = _council.run_council(ns)
    return code, json.loads(out.getvalue())


def _known_dispatch(fake):
    """Give the provider-free fixture the ledger facts a real envelope carries."""
    def wrapped(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag, **kwargs):
        result = fake(agent, prompt, cwd, agents_dir, timeout_ms, out_dir, tag, **kwargs)
        result.update({"attempts": 1, "attempt_id": f"{tag}-attempt",
                       "provider_contacted": False, "uncertain_spend": False})
        return result
    return wrapped


@pytest.fixture
def council(tmp_path):
    for agent in ("m1", "m2", "chair"):
        (tmp_path / f"{agent}.md").write_text(
            "---\nrun-agent: claude\npermission: safe-edit\n---\n# fixture\n",
            encoding="utf-8")
    _module, _argparse, calls, fake, _unused = _council_stub_and_runner()
    return tmp_path, calls, fake


def _context_file(path, checkpoint, *, key="a" * 32, text="Use the evidence"):
    value = {
        "schema": between.CONTEXT_SCHEMA,
        "run_id": checkpoint["run_id"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "checkpoint_generation": checkpoint["source_generation"],
        "operation_key": key,
        "entries": [{"kind": "note", "author": "human", "text": text}],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def _submit(root, run_id, context_path, generation, key="a" * 32):
    args = argparse.Namespace(
        council_context_submit=run_id, council_context_file=str(context_path),
        council_operation_key=key, council_expect_generation=generation,
        run_dir=str(root), cwd=str(root), json=True, out=None)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        code = _council.run_council_context_submit(args)
    return code, json.loads(output.getvalue())


def test_pause_requires_finite_overall_timeout_before_dispatch(council):
    root, calls, fake = council
    args = _args(root, pause=True, overall=None)
    with mock.patch.object(_council, "_dispatch", fake):
        code, result = _run(args)
    assert code == 1 and "finite --overall-timeout" in result["error"]
    assert calls["n"] == 0


def test_pause_submit_is_idempotent_and_continue_uses_round_two_only(council):
    root, calls, fake = council
    args = _args(root, pause=True)
    known = _known_dispatch(fake)
    with mock.patch.object(_council, "_dispatch", known):
        code, paused = _run(args)
    assert code == 0 and paused["status"] == "partial"
    assert paused["council_state"] == "awaiting_context"
    assert paused["between_round"]["provider_contacted"] is False
    assert calls["n"] == 2
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    assert checkpoint["attempt_ledger"]["round_1"]["m1"]["attempts"] == 1
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    code1, first = _submit(root, paused["run_id"], context_path,
                           checkpoint["source_generation"])
    code2, second = _submit(root, paused["run_id"], context_path,
                            checkpoint["source_generation"])
    assert code1 == code2 == 0
    assert first["status"] == second["status"] == "context_submitted"
    assert first["provider_contacted"] is second["provider_contacted"] is False
    calls["n"] = 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    with mock.patch.object(_council, "_dispatch", known):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            rc = _council.run_council_continue(continuation)
    if rc != 0:
        print("CONTINUE_OUTPUT", output.getvalue())
    assert rc == 0
    final = json.loads(output.getvalue())
    assert final["status"] == "success"
    assert calls["n"] == 3


def test_conflicting_context_and_generation_mismatch_refuse_before_provider(council):
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", fake):
        code, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    conflicting = root / "conflict.json"
    _context_file(conflicting, checkpoint, key="b" * 32, text="conflict")
    code, result = _submit(root, paused["run_id"], conflicting,
                           checkpoint["source_generation"], key="b" * 32)
    assert code == 1 and "conflicting council context" in result["error"]
    bad = argparse.Namespace(
        council_continue=paused["run_id"], council_expect_generation=999,
        run_dir=str(root), cwd=str(root), out=None, json=True)
    calls["n"] = 0
    assert _council.run_council_continue(bad) == 1
    assert calls["n"] == 0


def test_unknown_spend_blocks_continue_after_context_admission(council):
    root, calls, fake = council

    def uncertain(*args, **kwargs):
        result = fake(*args, **kwargs)
        result.update({"attempts": 1, "attempt_id": f"{args[6]}-attempt",
                       "uncertain_spend": True, "provider_contacted": True})
        return result

    with mock.patch.object(_council, "_dispatch", uncertain):
        code, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    assert checkpoint["uncertain_spend"] is True
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    calls["n"] = 0
    bad = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True)
    assert _council.run_council_continue(bad) == 1
    assert calls["n"] == 0


def test_final_and_ordinary_resume_cannot_replay_or_bypass_between_round_gate(council):
    root, calls, fake = council
    known = _known_dispatch(fake)
    with mock.patch.object(_council, "_dispatch", known):
        code, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    calls["n"] = 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    with mock.patch.object(_council, "_dispatch", known):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            assert _council.run_council_continue(continuation) == 0
    assert calls["n"] == 3
    # A second explicit continue sees the terminal generation and never pays again.
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 3
    # The legacy resume surface cannot bypass the pending/consumed G02 sidecar.
    resume = _args(root, pause=False)
    resume.resume_run = paused["run_id"]
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council(resume) == 1
    assert "between-round" in output.getvalue()


def test_missing_round_one_evidence_is_not_replayed(council):
    root, calls, fake = council
    known = _known_dispatch(fake)
    with mock.patch.object(_council, "_dispatch", known):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    missing = _rundir.stage_path(paused["run_dir"], checkpoint["source_generation"], "r1-m1")
    Path(missing).unlink()
    calls["n"] = 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 0


def test_unknown_round_one_ledger_is_not_replayed(council):
    """A legacy/partial envelope cannot be treated as a paid round-one fact."""
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", fake):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    calls["n"] = 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    # The provider-free fake intentionally omits attempts/identity fields.
    # The continuation must refuse before any round-two dispatch.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 0


def _admit_context(root, paused):
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    return checkpoint


def _continue_args(root, paused, checkpoint):
    return argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )


def test_changed_round_one_input_is_not_replayed(council):
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = _admit_context(root, paused)
    stage = Path(_rundir.stage_path(paused["run_dir"], checkpoint["source_generation"], "r1-m1"))
    envelope = json.loads(stage.read_text(encoding="utf-8"))
    envelope["input_sha256"] = "0" * 64
    stage.write_text(json.dumps(envelope), encoding="utf-8")
    calls["n"] = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 1
    assert calls["n"] == 0


def test_changed_round_one_status_is_not_replayed(council):
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = _admit_context(root, paused)
    stage = Path(_rundir.stage_path(paused["run_dir"], checkpoint["source_generation"], "r1-m1"))
    envelope = json.loads(stage.read_text(encoding="utf-8"))
    envelope["status"] = "error"
    stage.write_text(json.dumps(envelope), encoding="utf-8")
    calls["n"] = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 1
    assert calls["n"] == 0


def test_changed_round_one_receipt_bytes_are_not_replayed(council):
    """A status/input match is not enough: bind the complete original receipt."""
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = _admit_context(root, paused)
    stage = Path(_rundir.stage_path(paused["run_dir"], checkpoint["source_generation"], "r1-m1"))
    envelope = json.loads(stage.read_text(encoding="utf-8"))
    envelope["result"] = "tampered after the checkpoint"
    stage.write_text(json.dumps(envelope), encoding="utf-8")
    calls["n"] = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 1
    assert calls["n"] == 0


def test_changed_round_one_attempt_facts_are_not_replayed(council):
    """Attempt/contact facts are part of the immutable stage evidence."""
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = _admit_context(root, paused)
    stage = Path(_rundir.stage_path(paused["run_dir"], checkpoint["source_generation"], "r1-m1"))
    envelope = json.loads(stage.read_text(encoding="utf-8"))
    envelope["provider_contacted"] = True
    stage.write_text(json.dumps(envelope), encoding="utf-8")
    calls["n"] = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 1
    assert calls["n"] == 0


def test_legacy_checkpoint_without_full_evidence_binding_refuses(council):
    """A pre-binding pause record cannot silently regain continuation authority."""
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    checkpoint.pop("round_one_evidence_sha256")
    between.write_checkpoint(paused["run_dir"], checkpoint)
    calls["n"] = 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 0


def test_owner_validated_round_one_snapshot_survives_post_validation_replacement(council):
    """A stage replacement after validation cannot enter round two or synthesis."""
    root, calls, fake = council
    prompts = []

    def known(*args, **kwargs):
        prompts.append(args[1])
        result = fake(*args, **kwargs)
        result.update({"attempts": 1, "attempt_id": f"{args[6]}-attempt",
                       "provider_contacted": False, "uncertain_spend": False})
        return result

    with mock.patch.object(_council, "_dispatch", known):
        _, paused = _run(_args(root, pause=True))
    checkpoint = _admit_context(root, paused)
    original_validate = _council._validate_between_round_ledger
    calls_to_validate = {"n": 0}

    def validate_then_replace(*args, **kwargs):
        calls_to_validate["n"] += 1
        snapshot = original_validate(*args, **kwargs)
        # The first call is the pre-owner check.  Mutate only after the
        # owner-held validation, which is the boundary the continuation must
        # protect from a second unchecked stage-file read.
        if calls_to_validate["n"] == 2:
            run_dir, checked = args[0], args[1]
            stage_path = _rundir.stage_path(
                run_dir, checked["source_generation"], "r1-m1")
            envelope = _rundir.read_json(stage_path)
            envelope["result"] = "POST-VALIDATION REPLACEMENT"
            envelope["report"] = {"summary": "POST-VALIDATION REPLACEMENT"}
            _rundir.atomic_write_json(stage_path, envelope)
        return snapshot

    continuation = _continue_args(root, paused, checkpoint)
    with mock.patch.object(_council, "_validate_between_round_ledger",
                           side_effect=validate_then_replace), \
            mock.patch.object(_council, "_dispatch", known):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            assert _council.run_council_continue(continuation) == 0
    assert calls_to_validate["n"] == 2
    assert not any("POST-VALIDATION REPLACEMENT" in prompt for prompt in prompts[2:])


def test_round_one_identity_keeps_ordinary_context_bytes(council):
    """G02 alias bindings must not alter ordinary council stage identities."""
    root, _calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    stage = json.loads(Path(_rundir.stage_path(
        paused["run_dir"], checkpoint["source_generation"], "r1-m1")).read_text())
    legacy_ctx = {
        "cwd": str(root.resolve()),
        "agents_dir": str(root.resolve()),
        "strict_agents_dir": False,
        "enable_roles": False,
        "roles": {},
        **_council._optin_stage_ctx(),
    }
    expected = _rundir.content_sha256({
        "prompt": _council._round1_prompt("X or Y?"),
        "member": "m1",
        "agent_sha": checkpoint["member_definition_sha256"]["m1"],
        **legacy_ctx,
    })
    assert stage["input_sha256"] == expected
    assert "resolved_agents" not in legacy_ctx


def test_fresh_pause_deadline_starts_before_setup(council):
    """Roster/ownership setup must consume, not extend, the original budget."""
    root, _calls, fake = council
    class Clock:
        wall = 1_000_000.0
        mono = 10.0

        def time(self):
            return self.wall

        def monotonic(self):
            return self.mono

    clock = Clock()
    original_load = _loader.load_agent

    def delayed_load(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        clock.wall += 10.0
        clock.mono += 10.0
        return loaded

    with mock.patch.object(_council, "time", clock), \
            mock.patch.object(_loader, "load_agent", side_effect=delayed_load), \
            mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True, overall=120000))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    assert checkpoint["original_deadline_unix_ms"] == 1_000_120_000


def test_failed_contacted_round_one_sets_uncertain_spend_and_blocks(council):
    root, calls, fake = council

    def failed(*args, **kwargs):
        result = fake(*args, **kwargs)
        result.update({"status": "error", "attempts": 1,
                       "attempt_id": f"{args[6]}-attempt",
                       "provider_contacted": True, "uncertain_spend": False})
        return result

    with mock.patch.object(_council, "_dispatch", failed):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    assert checkpoint["uncertain_spend"] is True
    checkpoint = _admit_context(root, paused)
    calls["n"] = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 1
    assert calls["n"] == 0


def test_continue_passes_original_deadline_budget_from_preflight_start(council):
    root, calls, fake = council
    with mock.patch.object(_council, "_dispatch", _known_dispatch(fake)):
        _, paused = _run(_args(root, pause=True, overall=120000))
    checkpoint = _admit_context(root, paused)
    # Keep the test provider-free: intercept the final continuation handoff and
    # inspect the budget computed before ownership/roster setup can consume it.
    observed = {}
    def capture(call):
        observed["timeout"] = call.overall_timeout
        observed["start"] = call._council_run_start_monotonic
        return 0
    with mock.patch.object(_council, "run_council", side_effect=capture):
        assert _council.run_council_continue(_continue_args(root, paused, checkpoint)) == 0
    assert 0 < observed["timeout"] <= checkpoint["original_overall_timeout_ms"]
    assert isinstance(observed["start"], (int, float))


def test_public_council_subcommands_use_gated_handlers():
    argv, mode = _cli.rewrite_subcommand(
        ["council", "context", "submit", "run-1", "--context-file", "ctx.json",
         "--expect-generation", "3", "--operation-key", "a" * 32])
    assert mode is None
    assert argv[:2] == ["--council-context-submit", "run-1"]
    assert argv[2:] == ["--council-context-file", "ctx.json",
                        "--council-expect-generation", "3",
                        "--council-operation-key", "a" * 32]
    argv, mode = _cli.rewrite_subcommand(["council", "continue", "run-1"])
    assert mode is None
    assert argv == ["--council-continue", "run-1"]


def test_public_council_flags_parse_without_mode_keyerror():
    argv, mode = _cli.rewrite_subcommand(
        ["council", "context", "submit", "run-1", "--context-file", "ctx.json",
         "--expect-generation", "3", "--operation-key", "a" * 32])
    assert mode is None
    parser = _cli.build_parser("3.4.0", 1)
    args = parser.parse_args(argv + ["--json"])
    assert args.council_context_submit == "run-1"
    assert args.council_context_file == "ctx.json"
    assert args.council_expect_generation == 3
    assert args.council_operation_key == "a" * 32
    assert _cli.unsupported_mode_flags(argv + ["--json"], args) is None


def test_pause_after_round_accepts_explicit_round_number():
    parser = _cli.build_parser("3.4.0", 1)
    args = parser.parse_args(["--council", "--question", "X",
                              "--rounds", "2", "--pause-after-round", "1"])
    assert args.pause_after_round == 1


def _main(argv):
    """Run the public entry point and retain only its JSON/stdout contract."""
    import run_subagent

    output = io.StringIO()
    errors = io.StringIO()
    with mock.patch("sys.argv", ["summon", *argv]), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            run_subagent.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        else:
            code = 0
    return code, output.getvalue(), errors.getvalue()


def test_public_main_pause_submit_continue_journey_and_wrong_mode_refusal(council):
    """Exercise argv rewriting, parser dispatch, and the real gated handlers."""
    root, calls, fake = council
    known = _known_dispatch(fake)
    base = ["--cwd", str(root), "--agents-dir", str(root), "--run-dir", str(root),
            "--timeout", "60s", "--overall-timeout", "300s", "--members", "m1,m2",
            "--chairman", "chair", "--rounds", "2"]
    with mock.patch.object(_council, "_dispatch", known):
        code, stdout, _ = _main(["council", "--question", "X or Y?",
                                 "--pause-after-round", "1", *base])
    assert code == 0, (stdout, _)
    paused = json.loads(stdout)
    assert paused["council_state"] == "awaiting_context"
    assert calls["n"] == 2
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "main-context.json"
    _context_file(context_path, checkpoint)
    with mock.patch.object(_council, "_dispatch", known):
        code, stdout, _ = _main(["council", "context", "submit", paused["run_id"],
                                 "--context-file", str(context_path),
                                 "--expect-generation", str(checkpoint["source_generation"]),
                                 "--operation-key", "a" * 32, "--run-dir", str(root),
                                 "--cwd", str(root), "--json"])
    assert code == 0
    assert json.loads(stdout)["status"] == "context_submitted"
    calls["n"] = 0
    with mock.patch.object(_council, "_dispatch", known):
        code, stdout, _ = _main(["council", "continue", paused["run_id"],
                                 "--expect-generation", str(checkpoint["source_generation"]),
                                 "--run-dir", str(root), "--cwd", str(root), "--json"])
    assert code == 0
    assert json.loads(stdout)["status"] == "success"
    assert calls["n"] == 3

    # A context-submit lane cannot accept ordinary council flags.  The public
    # entry point must refuse before a handler or provider-facing dispatch runs.
    calls["n"] = 0
    code, _stdout, stderr = _main(["council", "context", "submit", paused["run_id"],
                                   "--context-file", str(context_path), "--question", "wrong"])
    assert code == 1
    assert "does not support" in (stderr + _stdout).lower()
    assert calls["n"] == 0


def test_council_dispatch_uses_owned_prompt_file_for_large_intermediate_context(tmp_path):
    import _manifest

    seen = {}

    class Result:
        timed_out = False

    def child(cmd, watchdog, **kwargs):
        seen["cmd"] = list(cmd)
        index = cmd.index("--prompt-file") + 1
        prompt_path = Path(cmd[index])
        seen["prompt"] = prompt_path.read_text(encoding="utf-8")
        seen["exists_during_child"] = prompt_path.exists()
        return Result(), None

    with mock.patch.object(_manifest, "_dispatch_child", child), \
            mock.patch.object(_manifest, "_read_envelope", return_value={"status": "success"}):
        prompt = "context\n" + ("x" * 70000)
        result = _council._dispatch("m1", prompt, str(tmp_path), str(tmp_path),
                                    1000, str(tmp_path), "g1-r2-m1")
    assert result["status"] == "success"
    assert seen["prompt"] == prompt
    assert seen["exists_during_child"] is True
    assert "--prompt" not in seen["cmd"]
    assert not list(tmp_path.glob(".summon-council-prompt-*.txt"))


def test_owner_rechecks_replaced_context_and_deadline(council):
    root, calls, fake = council
    known = _known_dispatch(fake)
    with mock.patch.object(_council, "_dispatch", known):
        _, paused = _run(_args(root, pause=True))
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    context_path = root / "context.json"
    _context_file(context_path, checkpoint)
    assert _submit(root, paused["run_id"], context_path,
                   checkpoint["source_generation"])[0] == 0
    continuation = argparse.Namespace(
        council_continue=paused["run_id"],
        council_expect_generation=checkpoint["source_generation"],
        run_dir=str(root), cwd=str(root), out=None, json=True,
    )
    original_acquire = _council._rd.acquire_owner

    def replace_context(run_dir, lease_sec):
        owner = original_acquire(run_dir, lease_sec)
        replacement = _context_file(root / "replacement.json", checkpoint,
                                    key="b" * 32, text="replacement")
        replacement.update(submitted_at_unix_ms=1, writer_generation=1)
        _rundir.atomic_write_json(between.context_path(run_dir), replacement)
        return owner

    calls["n"] = 0
    with mock.patch.object(_council._rd, "acquire_owner", side_effect=replace_context), \
         mock.patch.object(_council, "_dispatch", known):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 0

    # Recreate a valid context/checkpoint with a deadline that passes between
    # the pre-owner check and the owner-held recheck.
    checkpoint = between.read_checkpoint(paused["run_dir"], paused["run_id"])
    base = time.time()
    checkpoint["original_deadline_unix_ms"] = int((base + 0.15) * 1000)
    checkpoint = between.write_checkpoint(paused["run_dir"], checkpoint)
    Path(between.context_path(paused["run_dir"])).unlink()
    _context_file(context_path, checkpoint)
    code, submitted = _submit(root, paused["run_id"], context_path,
                               checkpoint["source_generation"])
    assert code == 0, submitted
    # The checkpoint's old generation is still awaiting context; fake time
    # makes the first check pass and the owner-held check fail deterministically.
    ticks = iter((base + 0.1, base + 0.2))
    with mock.patch.object(_council.time, "time", side_effect=lambda: next(ticks, base + 0.2)), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert _council.run_council_continue(continuation) == 1
    assert calls["n"] == 0
