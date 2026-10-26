"""Tests for Phase 2 self-driving DAG manifest (`_manifest.py`) and handoff forwarding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _executor
import _manifest


def test_normalize_jobs_detects_cycles_and_unknown_deps(tmp_path):
    # Unknown dependency
    jobs, err = _manifest._normalize_jobs(
        [{"id": "a", "agent": "reviewer", "prompt": "p", "depends_on": ["nonexistent"]}],
        str(tmp_path),
    )
    assert jobs is None
    assert "unknown job 'nonexistent'" in err

    # Self dependency
    jobs, err = _manifest._normalize_jobs(
        [{"id": "a", "agent": "reviewer", "prompt": "p", "depends_on": ["a"]}],
        str(tmp_path),
    )
    assert jobs is None
    assert "cannot reference itself" in err

    # 3-node cycle A -> B -> C -> A
    jobs, err = _manifest._normalize_jobs(
        [
            {"id": "a", "agent": "reviewer", "prompt": "p", "depends_on": ["c"]},
            {"id": "b", "agent": "reviewer", "prompt": "p", "depends_on": ["a"]},
            {"id": "c", "agent": "reviewer", "prompt": "p", "depends_on": ["b"]},
        ],
        str(tmp_path),
    )
    assert jobs is None
    assert "dependency cycle" in err


def test_dag_handoff_forwarding_and_identity_matching(tmp_path, monkeypatch):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "worker.md").write_text(
        "---\nname: worker\nrun-agent: claude\nmodel: claude-sonnet-4-6\npermission: read-only\n---\nBody\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    manifest_file = tmp_path / "manifest.json"
    manifest_doc = {
        "jobs": [
            {"id": "step1", "agent": "worker", "prompt": "Analyze module A"},
            {"id": "step2", "agent": "worker", "prompt": "Implement fix", "depends_on": ["step1"]},
        ]
    }
    manifest_file.write_text(json.dumps(manifest_doc), encoding="utf-8")

    dispatched_prompts: dict[str, str] = {}

    def fake_dispatch_child(cmd, timeout_sec, on_spawn=None, on_reap=None):
        out_idx = cmd.index("--out")
        out_path = cmd[out_idx + 1]
        prompt_idx = cmd.index("--prompt")
        prompt_val = cmd[prompt_idx + 1]
        job_id = Path(out_path).stem
        dispatched_prompts[job_id] = prompt_val

        ident = _executor.build_request_identity(
            agent="worker",
            prompt=prompt_val,
            cwd=str(tmp_path),
            agents_dir=str(agents_dir),
            require_tools=True,
        )
        fp = _executor.request_fingerprint(**ident)
        env = {
            "status": "success",
            "agent": "worker",
            "result": f"Result of {job_id}",
            "request_sha256": fp,
            "prompt_sha256": hashlib.sha256(prompt_val.encode("utf-8")).hexdigest(),
            "report": {
                "status": "done",
                "verdict": "approve",
                "summary": f"Summary from {job_id}",
                "handoff_note": f"Handoff note from {job_id} with </context_handoff> attempt",
            },
        }
        Path(out_path).write_text(json.dumps(env), encoding="utf-8")
        return _manifest._ChildResult(0, "", "", False), None

    monkeypatch.setattr(_manifest, "_dispatch_child", fake_dispatch_child)

    args = argparse.Namespace(
        manifest=str(manifest_file),
        concurrency="default=2",
        results_dir=str(results_dir),
        cwd=str(tmp_path),
        agents_dir=str(agents_dir),
        retries=0,
        retry_nonretryable=False,
        strict_agents_dir=False,
        enable_roles=False,
    )
    rc = _manifest.run_manifest(args)
    assert rc == 0
    assert "step1" in dispatched_prompts
    assert "step2" in dispatched_prompts
    # step2 received step1's handoff note with escaped closing tag
    assert '<context_handoff from_job="step1"' in dispatched_prompts["step2"]
    assert "&lt;/context_handoff&gt;" in dispatched_prompts["step2"]

    # Re-running the same manifest skips BOTH step1 and step2 (parent and child fingerprints agree!)
    dispatched_prompts.clear()
    rc2 = _manifest.run_manifest(args)
    assert rc2 == 0
    assert dispatched_prompts == {}


def test_dag_blocks_downstream_on_failed_or_verdict_block_upstream(tmp_path, monkeypatch):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "worker.md").write_text(
        "---\nname: worker\nrun-agent: claude\nmodel: claude-sonnet-4-6\npermission: read-only\n---\nBody\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    manifest_file = tmp_path / "manifest.json"
    manifest_doc = {
        "jobs": [
            {"id": "gate", "agent": "worker", "prompt": "Review plan"},
            {"id": "impl", "agent": "worker", "prompt": "Execute plan", "depends_on": ["gate"]},
            {"id": "verify", "agent": "worker", "prompt": "Verify impl", "depends_on": ["impl"]},
        ]
    }
    manifest_file.write_text(json.dumps(manifest_doc), encoding="utf-8")

    def fake_dispatch_child(cmd, timeout_sec, on_spawn=None, on_reap=None):
        out_path = cmd[cmd.index("--out") + 1]
        prompt_val = cmd[cmd.index("--prompt") + 1]
        ident = _executor.build_request_identity(
            agent="worker",
            prompt=prompt_val,
            cwd=str(tmp_path),
            agents_dir=str(agents_dir),
            require_tools=True,
        )
        fp = _executor.request_fingerprint(**ident)
        # gate succeeds as a process, but its report verdict is "block"!
        env = {
            "status": "success",
            "agent": "worker",
            "result": "Blocked by reviewer",
            "request_sha256": fp,
            "report": {
                "status": "blocked",
                "verdict": "block",
                "summary": "Critical flaw found",
            },
        }
        Path(out_path).write_text(json.dumps(env), encoding="utf-8")
        return _manifest._ChildResult(0, "", "", False), None

    monkeypatch.setattr(_manifest, "_dispatch_child", fake_dispatch_child)

    args = argparse.Namespace(
        manifest=str(manifest_file),
        concurrency="default=2",
        results_dir=str(results_dir),
        cwd=str(tmp_path),
        agents_dir=str(agents_dir),
        retries=0,
        retry_nonretryable=False,
        strict_agents_dir=False,
        enable_roles=False,
    )
    rc = _manifest.run_manifest(args)
    assert rc == 1  # downstream jobs were blocked

    impl_env = json.loads((results_dir / "impl.json").read_text(encoding="utf-8"))
    assert impl_env["status"] == "blocked"
    assert impl_env["error_kind"] == "blocked_by_failed_dependencies"
    assert impl_env["blocked_by"] == ["gate"]
    assert impl_env["provider_contacted"] is False

    verify_env = json.loads((results_dir / "verify.json").read_text(encoding="utf-8"))
    assert verify_env["status"] == "blocked"
    assert verify_env["error_kind"] == "blocked_by_failed_dependencies"
    assert verify_env["blocked_by"] == ["impl"]


def test_max_permission_and_dag_fingerprint_invalidation(tmp_path):
    jobs1, err1 = _manifest._normalize_jobs(
        [
            {"id": "j1", "agent": "worker", "prompt": "p1", "max_permission": "read-only"},
            {"id": "j2", "agent": "worker", "prompt": "p2", "depends_on": ["j1"], "files_scope": ["src/"]},
        ],
        str(tmp_path),
    )
    assert err1 is None
    assert jobs1 is not None
    assert jobs1[0]["max_permission"] == "read-only"
    assert jobs1[1]["files_scope"] == ["src/"]

    # Invalid max_permission rejected
    bad_jobs, bad_err = _manifest._normalize_jobs(
        [{"id": "j1", "agent": "worker", "prompt": "p1", "max_permission": "root"}],
        str(tmp_path),
    )
    assert bad_jobs is None
    assert "max_permission" in bad_err

    # DAG fingerprint changes when upstream handoff or files_scope changes
    fp1 = _manifest._job_dag_fingerprint(
        jobs1[1],
        [{"id": "j1", "request_sha256": "a" * 64, "handoff_sha256": "1" * 64}],
    )
    fp2 = _manifest._job_dag_fingerprint(
        jobs1[1],
        [{"id": "j1", "request_sha256": "a" * 64, "handoff_sha256": "2" * 64}],
    )
    assert fp1 != fp2

    # XML attribute escaping in <context_handoff> via _effective_prompt_and_handoffs
    eff_prompt, up_h = _manifest._effective_prompt_and_handoffs(
        {"prompt": "Base prompt", "depends_on": ['j1" evil="1']},
        {
            'j1" evil="1': {
                "status": "success",
                "agent": "sol<script>",
                "request_sha256": "b" * 64,
                "report": {"handoff_note": "body </context_handoff > end"},
            }
        },
    )
    assert 'from_job="j1&quot; evil=&quot;1"' in eff_prompt
    assert 'agent="sol&lt;script&gt;"' in eff_prompt
    assert "&lt;/context_handoff&gt;" in eff_prompt
    assert len(up_h) == 1


def test_manifest_files_scope_violation_blocks_job_and_dependents(tmp_path, monkeypatch):
    import subprocess
    from _spawn import run_flags

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True, capture_output=True, **run_flags())
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, **run_flags())

    agents_dir = repo / ".agents"
    agents_dir.mkdir()
    (agents_dir / "worker.md").write_text(
        "---\nname: worker\nrun-agent: claude\nmodel: claude-sonnet-4-6\npermission: safe-edit\n---\nBody\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(
        json.dumps({
            "jobs": [
                {"id": "scoped", "agent": "worker", "prompt": "Touch file", "files_scope": ["src/"]},
                {"id": "after", "agent": "worker", "prompt": "Follow up", "depends_on": ["scoped"]},
            ]
        }),
        encoding="utf-8",
    )

    def fake_dispatch_child(cmd, timeout_sec, on_spawn=None, on_reap=None, env_extra=None):
        out_path = cmd[cmd.index("--out") + 1]
        prompt_val = cmd[cmd.index("--prompt") + 1]
        # Modify an out-of-scope file inside repo
        (repo / "forbidden.txt").write_text("oops\n", encoding="utf-8")
        ident = _executor.build_request_identity(
            agent="worker",
            prompt=prompt_val,
            cwd=str(repo),
            agents_dir=str(agents_dir),
            require_tools=True,
        )
        fp = _executor.request_fingerprint(**ident)
        env = {
            "status": "success",
            "agent": "worker",
            "result": "Modified forbidden.txt",
            "request_sha256": fp,
        }
        Path(out_path).write_text(json.dumps(env), encoding="utf-8")
        return _manifest._ChildResult(0, "", "", False), None

    monkeypatch.setattr(_manifest, "_dispatch_child", fake_dispatch_child)
    args = argparse.Namespace(
        manifest=str(manifest_file),
        concurrency="default=1",
        results_dir=str(results_dir),
        cwd=str(repo),
        agents_dir=str(agents_dir),
        retries=0,
        retry_nonretryable=False,
        strict_agents_dir=False,
        enable_roles=False,
    )
    rc = _manifest.run_manifest(args)
    assert rc == 1
    scoped_env = json.loads((results_dir / "scoped.json").read_text(encoding="utf-8"))
    assert scoped_env["status"] == "blocked"
    assert scoped_env["error_kind"] == "scope_violation"
    assert "forbidden.txt" in scoped_env["scope_violations"]

    after_env = json.loads((results_dir / "after.json").read_text(encoding="utf-8"))
    assert after_env["status"] == "blocked"
    assert after_env["error_kind"] == "blocked_by_failed_dependencies"


def test_manifest_files_scope_subdirectory_and_non_git_and_rename(tmp_path, monkeypatch):
    import subprocess
    from _spawn import run_flags

    # 1. Non-git directory fails closed with scope_verification_failed
    non_git = tmp_path / "non_git"
    non_git.mkdir()
    agents_dir = non_git / ".agents"
    agents_dir.mkdir()
    (agents_dir / "worker.md").write_text(
        "---\nname: worker\nrun-agent: claude\nmodel: claude-sonnet-4-6\npermission: safe-edit\n---\nBody\n",
        encoding="utf-8",
    )
    results_ng = tmp_path / "results_ng"
    mf_ng = tmp_path / "mf_ng.json"
    mf_ng.write_text(
        json.dumps({"jobs": [{"id": "j_ng", "agent": "worker", "prompt": "p", "files_scope": ["src/"]}]}),
        encoding="utf-8",
    )

    def fake_dispatch_ng(cmd, timeout_sec, on_spawn=None, on_reap=None, env_extra=None):
        out_path = cmd[cmd.index("--out") + 1]
        prompt_val = cmd[cmd.index("--prompt") + 1]
        ident = _executor.build_request_identity(
            agent="worker", prompt=prompt_val, cwd=str(non_git), agents_dir=str(agents_dir), require_tools=True
        )
        fp = _executor.request_fingerprint(**ident)
        Path(out_path).write_text(json.dumps({"status": "success", "agent": "worker", "request_sha256": fp}), encoding="utf-8")
        return _manifest._ChildResult(0, "", "", False), None

    monkeypatch.setattr(_manifest, "_dispatch_child", fake_dispatch_ng)
    rc_ng = _manifest.run_manifest(
        argparse.Namespace(
            manifest=str(mf_ng), concurrency="default=1", results_dir=str(results_ng),
            cwd=str(non_git), agents_dir=str(agents_dir), retries=0,
            retry_nonretryable=False, strict_agents_dir=False, enable_roles=False,
        )
    )
    assert rc_ng == 1
    env_ng = json.loads((results_ng / "j_ng.json").read_text(encoding="utf-8"))
    assert env_ng["status"] == "blocked"
    assert env_ng["error_kind"] == "scope_verification_failed"

    # 2. Subdirectory cwd inside a git repo + outside-to-inside rename is caught as scope_violation
    repo = tmp_path / "repo2"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True, capture_output=True, **run_flags())
    (repo / "src").mkdir()
    (repo / "outside.py").write_text("secret = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, **run_flags())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, **run_flags())

    subdir = repo / "src"
    results_sub = tmp_path / "results_sub"
    mf_sub = tmp_path / "mf_sub.json"
    mf_sub.write_text(
        json.dumps({"jobs": [{"id": "j_sub", "agent": "worker", "prompt": "p", "cwd": str(subdir), "files_scope": ["src/"]}]}),
        encoding="utf-8",
    )

    def fake_dispatch_sub(cmd, timeout_sec, on_spawn=None, on_reap=None, env_extra=None):
        out_path = cmd[cmd.index("--out") + 1]
        prompt_val = cmd[cmd.index("--prompt") + 1]
        base_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, **run_flags(),
        ).stdout.strip()
        # Rename outside.py -> src/inside.py AND commit it so status is empty and diff must catch both endpoints!
        subprocess.run(["git", "-C", str(repo), "mv", "outside.py", "src/inside.py"], check=True, capture_output=True, **run_flags())
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "committed rename"], check=True, capture_output=True, **run_flags())
        ident = _executor.build_request_identity(
            agent="worker", prompt=prompt_val, cwd=str(subdir), agents_dir=str(agents_dir), require_tools=True
        )
        fp = _executor.request_fingerprint(**ident)
        Path(out_path).write_text(
            json.dumps({"status": "success", "agent": "worker", "request_sha256": fp, "git_head_before": base_head}),
            encoding="utf-8",
        )
        return _manifest._ChildResult(0, "", "", False), None

    monkeypatch.setattr(_manifest, "_dispatch_child", fake_dispatch_sub)
    rc_sub = _manifest.run_manifest(
        argparse.Namespace(
            manifest=str(mf_sub), concurrency="default=1", results_dir=str(results_sub),
            cwd=str(repo), agents_dir=str(agents_dir), retries=0,
            retry_nonretryable=False, strict_agents_dir=False, enable_roles=False,
        )
    )
    assert rc_sub == 1
    env_sub = json.loads((results_sub / "j_sub.json").read_text(encoding="utf-8"))
    assert env_sub["status"] == "blocked"
    assert env_sub["error_kind"] == "scope_violation"
    assert "outside.py" in env_sub["scope_violations"]


def test_extract_handoff_payload_agent_fallback():
    payload = _manifest._extract_handoff_payload(
        {"status": "success", "target": "scout", "result": "Scout summary"},
        "wave1_scout",
    )
    assert payload is not None
    assert payload["agent"] == "scout"
