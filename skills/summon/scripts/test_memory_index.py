"""Tests for stdlib SQLite FTS5 / BM25 memory and handoff search (_memory_index.py)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _memory_index  # noqa: E402


def test_memory_index_fts5_and_bm25_fallback_and_operator_safety(tmp_path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir(parents=True)
    mem_file = agents_dir / "memory.md"
    mem_file.write_text(
        "# Architecture Decisions\n"
        "We chose a zero-socket HMAC-sealed control plane for background jobs.\n\n"
        "# Database Conventions\n"
        "Use parameterized SQLite FTS5 queries with BM25 fallback.\n",
        encoding="utf-8",
    )

    results_dir = agents_dir / "results"
    results_dir.mkdir()
    (results_dir / "task1.json").write_text(
        json.dumps(
            {
                "status": "success",
                "agent": "sol",
                "summary": "Implemented Kahn topological cycle detector in _manifest.py",
                "handoff_note": {
                    "schema": "summon.handoff-note/v1",
                    "summary": "Added DAG wave scheduler and upstream handoff forwarding.",
                    "handoff": "Ready for Phase 3 budget integration.",
                },
            }
        ),
        encoding="utf-8",
    )

    # 1. Hostile query with FTS operators (AND/OR/NOT/*/NEAR/":") must never raise OperationalError
    res_fts = _memory_index.search_memory(
        'AND OR NOT NEAR * "unclosed : topological OR hmac',
        cwd=str(tmp_path),
    )
    assert res_fts["schema"] == "summon.memory-search/v1"
    assert res_fts["engine"] in {"sqlite_fts5", "python_bm25_fallback"}
    assert len(res_fts["hits"]) >= 2
    assert all(h["source_verified"] is True for h in res_fts["hits"])
    assert all(h["trust_tier"] == "advisory_search_hit" for h in res_fts["hits"])

    # 2. Pure-Python BM25 fallback produces verified hits when forced
    res_fb = _memory_index.search_memory(
        "topological wave scheduler",
        cwd=str(tmp_path),
        force_fallback=True,
    )
    assert res_fb["engine"] == "python_bm25_fallback"
    assert len(res_fb["hits"]) >= 1
    assert res_fb["hits"][0]["source_path"] == ".agents/results/task1.json"


def test_memory_index_source_verification_drops_tampered_or_cross_repo_cache_rows(tmp_path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir(parents=True)
    mem_file = agents_dir / "memory.md"
    mem_file.write_text(
        "# Security Notes\n"
        "Never expose api_key=sk-secret1234567890 in search results.\n",
        encoding="utf-8",
    )

    res = _memory_index.search_memory("Security Notes", cwd=str(tmp_path))
    assert len(res["hits"]) == 1
    # Secret must be redacted!
    assert "sk-secret1234567890" not in res["hits"][0]["snippet"]
    assert "<redacted>" in res["hits"][0]["snippet"]

    # Verify that _verify_and_reconstruct_hit rejects a row with a mismatched source_sha256
    # or wrong repo_realpath_sha256
    bad_row = {
        "repo_realpath_sha256": "0" * 64,
        "source_kind": "memory_md",
        "source_path": ".agents/memory.md",
        "source_sha256": res["hits"][0]["source_sha256"],
        "byte_start": 0,
        "byte_end": 20,
        "normalization_version": _memory_index.NORMALIZATION_VERSION,
    }
    assert _memory_index._verify_and_reconstruct_hit(str(tmp_path), bad_row) is None

    bad_sha_row = dict(bad_row)
    bad_sha_row["repo_realpath_sha256"] = _memory_index._repo_realpath_sha256(str(tmp_path))
    bad_sha_row["source_sha256"] = "f" * 64
    assert _memory_index._verify_and_reconstruct_hit(str(tmp_path), bad_sha_row) is None


def test_index_workspace_memory_idempotent_sync_and_title_sanitization(tmp_path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "memory.md").write_text(
        "# Heading with api_key=sk-secret999999999999 and \x1b[31mANSI\x1b[0m\n"
        "Body about distributed consensus.\n",
        encoding="utf-8",
    )
    info1 = _memory_index.index_workspace_memory(str(tmp_path))
    assert info1["indexed_chunks"] == 1

    # Second call sees unchanged chunk digests and succeeds without error
    info2 = _memory_index.index_workspace_memory(str(tmp_path))
    assert info2["indexed_chunks"] == 1

    res = _memory_index.search_memory("distributed consensus", cwd=str(tmp_path))
    assert len(res["hits"]) == 1
    assert "sk-secret999999999999" not in res["hits"][0]["title"]
    assert "\x1b" not in res["hits"][0]["title"]


def test_cli_memory_search_query_flag_and_mission_goal_alias():
    import _cli
    argv, err = _cli.rewrite_subcommand(["memory", "search", "--query", "HOOK_BRIDGE", "--limit", "5"])
    assert err is None
    assert argv == ["--memory-action", "search", "--memory-query", "HOOK_BRIDGE", "--memory-limit", "5"]

    argv_eq, err_eq = _cli.rewrite_subcommand(["memory", "search", "--query=HOOK_BRIDGE", "--limit=3"])
    assert err_eq is None
    assert argv_eq == ["--memory-action", "search", "--memory-query", "HOOK_BRIDGE", "--memory-limit=3"]

    argv_m, err_m = _cli.rewrite_subcommand(["mission", "start", "--goal", "ship docs"])
    assert err_m is None
    assert argv_m == ["--mission-action", "start", "--mission-objective", "ship docs"]
