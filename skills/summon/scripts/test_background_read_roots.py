"""Regression coverage for read-root propagation across detached dispatches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile


def test_foreground_and_background_read_allowlists_stay_in_parity():
    """Repeatable roots survive child argv reconstruction and launch recording."""
    import _background
    import _jobs
    from _builder import normalize_read_roots, read_allowlist

    with tempfile.TemporaryDirectory(prefix="summon-read-roots-background-") as root:
        roster = os.path.join(root, "roster")
        os.mkdir(roster)
        oracle = os.path.join(root, "oracle")
        board = os.path.join(root, "board")
        os.mkdir(oracle)
        os.mkdir(board)
        with open(os.path.join(roster, "reviewer.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "---\nrun-agent: claude\npermission: read-only\n"
                "model: claude-opus-5\n---\n# Reviewer\n"
            )

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
        roots = normalize_read_roots([oracle, board])
        result = subprocess.run(
            [
                sys.executable,
                script,
                "--agent",
                "reviewer",
                "--prompt",
                "inspect",
                "--cwd",
                root,
                "--agents-dir",
                roster,
                "--strict-agents-dir",
                "--read-root",
                oracle,
                "--read-root",
                board,
                "--timeout",
                "30s",
                "--dry-run",
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        foreground = json.loads(result.stdout)["read_allowlist"]
        assert foreground["requested_paths"] == list(roots), foreground
        assert foreground["effective_paths"] == [os.path.abspath(root), *roots], foreground

        args = argparse.Namespace(
            agent="reviewer",
            prompt="inspect",
            prompt_file=None,
            cwd=root,
            agents_dir=roster,
            strict_agents_dir=True,
            enable_roles=False,
            read_root=[oracle, board],
            _read_roots_cli=roots,
            allow_credit=False,
            allow_payg=False,
            allow_text_only=False,
            require_tools=False,
            no_contract_repair=False,
            timeout=30000,
            cli=None,
            model=None,
            effort=None,
            profile=None,
            resume=None,
            resume_profile=None,
            out=None,
            json_schema=None,
            debug_dir=None,
            retries=0,
            max_permission=None,
            gate_with=None,
            gate_timeout=None,
            worktree=None,
            artifacts=[],
        )
        child = _background.child_argv(args, "result.json")
        child_roots = [
            child[index + 1]
            for index, value in enumerate(child[:-1])
            if value == "--read-root"
        ]
        assert child_roots == list(roots), child

        background = read_allowlist("claude", "read-only", root, child_roots)
        assert background == foreground, (foreground, background)

        projected = _jobs.flags_projection(args)
        assert projected["read_root"] == list(roots), projected
        jobs = os.path.join(root, "jobs")
        job_id = _jobs.new_job_id()
        _jobs.write_prepared(
            jobs,
            job_id,
            nonce="r" * 32,
            agent="reviewer",
            prompt_sha256=None,
            cwd=root,
            flags=projected,
            summon={},
        )
        with open(_jobs.record_path(jobs, job_id), encoding="utf-8") as fh:
            record = json.load(fh)
        assert record["flags"]["read_root"] == foreground["requested_paths"], record


def test_background_bundle_freezes_execution_identity_before_source_changes():
    """A managed refresh cannot relabel a child after its parent launches it."""
    import _background
    from _receipt import scripts_sha256

    with tempfile.TemporaryDirectory(prefix="summon-background-bundle-") as root:
        source_scripts = os.path.join(root, "source", "scripts")
        os.makedirs(source_scripts)
        entry = os.path.join(source_scripts, "run_subagent.py")
        helper = os.path.join(source_scripts, "helper.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("# old entry\n")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write("VALUE = 'old'\n")
        expected_entry, execution = _background._freeze_background_bundle(
            root, "a" * 32, entry,
            {"version": "test", "script": entry, "scripts_sha256": scripts_sha256(source_scripts)})
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write("VALUE = 'new install'\n")
        assert execution["script"] == expected_entry
        assert execution["scripts_sha256"] == scripts_sha256(os.path.dirname(expected_entry))
        assert execution["scripts_sha256"] != scripts_sha256(source_scripts)
        with open(os.path.join(os.path.dirname(expected_entry), "helper.py"), encoding="utf-8") as fh:
            assert fh.read() == "VALUE = 'old'\n"


def test_background_result_with_different_scripts_digest_is_untrusted():
    """Nonce equality alone cannot bless a result from a replaced script tree."""
    import _jobs

    with tempfile.TemporaryDirectory(prefix="summon-background-identity-") as root:
        _jobs.ensure_jobs_dir(root)
        job_id = _jobs.new_job_id()
        _jobs.write_prepared(
            root, job_id, nonce="n" * 32, agent="reviewer", prompt_sha256=None,
            cwd=root, flags={}, summon={"scripts_sha256": "a" * 64})
        with open(_jobs.result_path(root, job_id), "w", encoding="utf-8") as fh:
            json.dump({"job_nonce": "n" * 32, "status": "success",
                       "summon": {"scripts_sha256": "b" * 64}}, fh)
        status = _jobs.job_status(root, job_id)
        assert status["state"] == "identity_mismatch" and status["trusted"] is False, status


def test_scripts_digest_covers_windows_dispatcher_launcher():
    """A managed Windows invocation helper is executable provenance, not docs."""
    from _receipt import scripts_sha256

    with tempfile.TemporaryDirectory(prefix="summon-launcher-digest-") as root:
        with open(os.path.join(root, "run_subagent.py"), "w", encoding="utf-8") as fh:
            fh.write("# dispatcher\n")
        before = scripts_sha256(root)
        with open(os.path.join(root, "summon.cmd"), "w", encoding="utf-8") as fh:
            fh.write("@echo off\n")
        after = scripts_sha256(root)
        assert before != after
