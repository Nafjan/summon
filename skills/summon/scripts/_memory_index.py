"""Stdlib SQLite FTS5 / BM25 memory and handoff search for Summon.

Implements ``summon.memory-search/v1``:
- Disposable acceleration cache stored at ``<cwd>/.agents/.cache/memory-index.sqlite3``
  (with automatic pure-Python BM25 fallback when SQLite FTS5 is absent, read-only,
  or corrupt).
- Indexes:
  1. ``<cwd>/.agents/memory.md`` (markdown section/paragraph chunks).
  2. Completed result envelopes under ``<cwd>/.agents/results/*.json``
     (``handoff_note``, ``report_text`` / ``summary``).
  3. Completed council/mission syntheses under ``<cwd>/.agents/runs/**/receipt.json``
     and ``<cwd>/.agents/missions/**/mission.json``.
- Strict Source Verification Before Serving (Astra P2 #13):
  - Every indexed chunk stores ``(repo_realpath_sha256, source_kind, source_path,
    source_sha256, byte_start, byte_end, normalization_version)``.
  - Before serving any hit, ``search_memory`` re-stats and hashes the underlying
    authoritative source file, verifies ``repo_realpath_sha256`` and ``source_sha256``,
    and slices/verifies the snippet directly from the source file, dropping deleted,
    modified, or cross-project records!
- Operator-safe FTS5 query sanitization: strips FTS operators (``AND``, ``OR``, ``NOT``,
  ``NEAR``, ``*``, ``:``, quotes) and double-quotes individual alphanumeric tokens so
  arbitrary user query strings never trigger ``sqlite3.OperationalError``.
- All returned snippets pass through secret redaction and prompt-injection fence
  stripping and carry ``trust_tier: "advisory_search_hit"``.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from _executor import _redact_output_secrets


MEMORY_SEARCH_SCHEMA = "summon.memory-search/v1"
NORMALIZATION_VERSION = 1
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
MAX_SNIPPET_CHARS = 800

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,64}")
_FTS_RESERVED = frozenset({"and", "or", "not", "near"})
_INJECTION_FENCE_RE = re.compile(
    r"(?i)</?(?:system|assistant|developer|user|context_handoff|tool_call|tool_result)\b[^>]*>"
)
_ANSI_OR_CTRL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _repo_realpath_sha256(cwd: str) -> str:
    real = os.path.normcase(os.path.realpath(os.path.abspath(cwd)))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()


def _cache_db_path(cwd: str) -> str:
    return os.path.join(os.path.abspath(cwd), ".agents", ".cache", "memory-index.sqlite3")


def _safe_sanitize_snippet(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    redacted = _redact_output_secrets(text)
    defenced = _INJECTION_FENCE_RE.sub("", redacted)
    no_ctrl = _ANSI_OR_CTRL_RE.sub("", defenced)
    cleaned = "\n".join(line.rstrip() for line in no_ctrl.splitlines()).strip()
    return cleaned[:MAX_SNIPPET_CHARS]


def _safe_sanitize_title(title: str, default: str = "memory.md") -> str:
    cleaned = _safe_sanitize_snippet(title).replace("\n", " ").strip()[:120]
    return cleaned or default


def _has_reparse_between(root: Path, target: Path) -> bool:
    """Return True if ``target`` or any ancestor inside ``root`` is a symlink/junction or escapes ``root``."""
    try:
        root_abs = Path(os.path.abspath(str(root)))
        target_abs = Path(os.path.abspath(str(target)))
        real_root = os.path.normcase(os.path.realpath(str(root_abs)))
        real_target = os.path.normcase(os.path.realpath(str(target_abs)))
        if os.path.commonpath([real_root, real_target]) != real_root:
            return True
        cur = target_abs
        while cur != root_abs and cur.parent != cur:
            if cur.is_symlink():
                return True
            if os.name == "nt":
                st = cur.lstat()
                if getattr(st, "st_file_attributes", 0) & 0x400:
                    return True
            cur = cur.parent
        return False
    except (OSError, ValueError):
        return True


def sanitize_fts_query(query: str) -> tuple[list[str], str]:
    """Extract safe alphanumeric search tokens and build a parameterized FTS5 MATCH string."""
    if not isinstance(query, str):
        return [], ""
    tokens: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(query):
        tok = m.group(0)
        low = tok.lower()
        if low in _FTS_RESERVED:
            continue
        if low not in seen:
            seen.add(low)
            tokens.append(low)
        if len(tokens) >= 24:
            break
    if not tokens:
        return [], ""
    # Quote each token as a literal phrase and combine with OR so partial matches rank via BM25
    fts_expr = " OR ".join(f'"{t}"' for t in tokens)
    return tokens, fts_expr


def _chunk_markdown_bytes(raw_bytes: bytes) -> list[tuple[int, int, str, str]]:
    """Split UTF-8 markdown bytes into (byte_start, byte_end, title, text) chunks."""
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    chunks: list[tuple[int, int, str, str]] = []
    current_title = "memory.md"
    buf_lines: list[str] = []
    chunk_byte_start = 0
    cursor_bytes = 0

    def _flush(end_byte: int) -> None:
        nonlocal buf_lines, chunk_byte_start
        joined = "".join(buf_lines).strip()
        if joined:
            chunks.append((chunk_byte_start, end_byte, _safe_sanitize_title(current_title), joined))
        buf_lines = []

    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        stripped = line.strip()
        if stripped.startswith("#"):
            _flush(cursor_bytes)
            current_title = _safe_sanitize_title(stripped.lstrip("#").strip(), "memory.md")
            chunk_byte_start = cursor_bytes
            buf_lines = [line]
        elif not stripped and sum(len(x) for x in buf_lines) >= 320:
            _flush(cursor_bytes)
            chunk_byte_start = cursor_bytes + line_bytes
        else:
            if not buf_lines:
                chunk_byte_start = cursor_bytes
            buf_lines.append(line)
        cursor_bytes += line_bytes

    _flush(cursor_bytes)
    return chunks


def _extract_json_summary_text(raw_bytes: bytes, source_kind: str) -> str:
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    if source_kind == "result_envelope":
        agent = data.get("agent")
        if isinstance(agent, str) and agent.strip():
            parts.append(f"agent: {agent.strip()}")
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            parts.append(f"summary: {summary.strip()}")
        hnote = data.get("handoff_note")
        if isinstance(hnote, dict):
            for field in ("summary", "handoff", "next_step", "decisions"):
                val = hnote.get(field)
                if isinstance(val, str) and val.strip():
                    parts.append(f"{field}: {val.strip()}")
        elif isinstance(hnote, str) and hnote.strip():
            parts.append(f"handoff: {hnote.strip()}")
        elif isinstance(data.get("report"), dict):
            rep = data["report"]
            for field in ("summary", "handoff", "risks"):
                val = rep.get(field)
                if isinstance(val, str) and val.strip():
                    parts.append(f"{field}: {val.strip()}")
    elif source_kind == "run_receipt":
        q = data.get("question") or data.get("objective")
        if isinstance(q, str) and q.strip():
            parts.append(f"question: {q.strip()}")
        syn = data.get("synthesis") or data.get("summary") or data.get("result")
        if isinstance(syn, str) and syn.strip():
            parts.append(f"synthesis: {syn.strip()}")
        elif isinstance(syn, dict):
            for k in ("summary", "decision", "result", "report_text"):
                v = syn.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(f"{k}: {v.strip()}")
    return "\n".join(parts).strip()


def _discover_sources(cwd: str) -> list[dict[str, Any]]:
    """Discover authoritative project-local memory, result envelopes, and run receipts."""
    root = Path(os.path.abspath(cwd))
    agents_root = root / ".agents"
    if not agents_root.is_dir() or _has_reparse_between(root, agents_root):
        return []

    repo_sha = _repo_realpath_sha256(str(root))
    records: list[dict[str, Any]] = []

    # 1. .agents/memory.md
    mem_file = agents_root / "memory.md"
    if mem_file.is_file() and not _has_reparse_between(root, mem_file):
        try:
            raw = mem_file.read_bytes()
            if 0 < len(raw) <= MAX_SOURCE_FILE_BYTES:
                file_sha = hashlib.sha256(raw).hexdigest()
                rel_path = os.path.relpath(str(mem_file), str(root)).replace("\\", "/")
                for b_start, b_end, title, chunk_text in _chunk_markdown_bytes(raw):
                    clean_snippet = _safe_sanitize_snippet(chunk_text)
                    if not clean_snippet:
                        continue
                    records.append({
                        "repo_realpath_sha256": repo_sha,
                        "source_kind": "memory_md",
                        "source_path": rel_path,
                        "source_sha256": file_sha,
                        "byte_start": b_start,
                        "byte_end": b_end,
                        "normalization_version": NORMALIZATION_VERSION,
                        "title": _safe_sanitize_title(title),
                        "content": clean_snippet,
                    })
        except OSError:
            pass

    # 2. .agents/results/*.json
    results_dir = agents_root / "results"
    if results_dir.is_dir() and not _has_reparse_between(root, results_dir):
        try:
            for entry in sorted(results_dir.glob("*.json"))[:200]:
                if not entry.is_file() or _has_reparse_between(root, entry):
                    continue
                raw = entry.read_bytes()
                if not (0 < len(raw) <= MAX_SOURCE_FILE_BYTES):
                    continue
                extracted = _extract_json_summary_text(raw, "result_envelope")
                clean_snippet = _safe_sanitize_snippet(extracted)
                if not clean_snippet:
                    continue
                file_sha = hashlib.sha256(raw).hexdigest()
                rel_path = os.path.relpath(str(entry), str(root)).replace("\\", "/")
                records.append({
                    "repo_realpath_sha256": repo_sha,
                    "source_kind": "result_envelope",
                    "source_path": rel_path,
                    "source_sha256": file_sha,
                    "byte_start": 0,
                    "byte_end": len(raw),
                    "normalization_version": NORMALIZATION_VERSION,
                    "title": _safe_sanitize_title(entry.stem, "result"),
                    "content": clean_snippet,
                })
        except OSError:
            pass

    # 3. .agents/runs/**/receipt.json and .agents/missions/**/(projection.json|mission.json)
    for subdir_name, filename in (
        ("runs", "receipt.json"),
        ("missions", "projection.json"),
        ("missions", "mission.json"),
    ):
        sub_root = agents_root / subdir_name
        if not sub_root.is_dir() or _has_reparse_between(root, sub_root):
            continue
        try:
            for entry in sorted(sub_root.glob(f"*/{filename}"))[:100]:
                if not entry.is_file() or _has_reparse_between(root, entry):
                    continue
                raw = entry.read_bytes()
                if not (0 < len(raw) <= MAX_SOURCE_FILE_BYTES):
                    continue
                extracted = _extract_json_summary_text(raw, "run_receipt")
                clean_snippet = _safe_sanitize_snippet(extracted)
                if not clean_snippet:
                    continue
                file_sha = hashlib.sha256(raw).hexdigest()
                rel_path = os.path.relpath(str(entry), str(root)).replace("\\", "/")
                records.append({
                    "repo_realpath_sha256": repo_sha,
                    "source_kind": "run_receipt",
                    "source_path": rel_path,
                    "source_sha256": file_sha,
                    "byte_start": 0,
                    "byte_end": len(raw),
                    "normalization_version": NORMALIZATION_VERSION,
                    "title": _safe_sanitize_title(f"{entry.parent.name}/{entry.name}", "receipt"),
                    "content": clean_snippet,
                })
        except OSError:
            pass

    # 4. .agents/missions/*/results/*.json
    missions_root = agents_root / "missions"
    if missions_root.is_dir() and not _has_reparse_between(root, missions_root):
        try:
            for entry in sorted(missions_root.glob("*/results/*.json"))[:200]:
                if not entry.is_file() or _has_reparse_between(root, entry):
                    continue
                raw = entry.read_bytes()
                if not (0 < len(raw) <= MAX_SOURCE_FILE_BYTES):
                    continue
                extracted = _extract_json_summary_text(raw, "result_envelope")
                clean_snippet = _safe_sanitize_snippet(extracted)
                if not clean_snippet:
                    continue
                file_sha = hashlib.sha256(raw).hexdigest()
                rel_path = os.path.relpath(str(entry), str(root)).replace("\\", "/")
                records.append({
                    "repo_realpath_sha256": repo_sha,
                    "source_kind": "result_envelope",
                    "source_path": rel_path,
                    "source_sha256": file_sha,
                    "byte_start": 0,
                    "byte_end": len(raw),
                    "normalization_version": NORMALIZATION_VERSION,
                    "title": _safe_sanitize_title(f"{entry.parent.parent.name}/{entry.stem}", "result"),
                    "content": clean_snippet,
                })
        except OSError:
            pass

    return records


def _verify_and_reconstruct_hit(cwd: str, row: dict[str, Any]) -> str | None:
    """Verify authoritative source file digest and reconstruct sanitized snippet before serving."""
    root = os.path.abspath(cwd)
    expected_repo_sha = _repo_realpath_sha256(root)
    if row.get("repo_realpath_sha256") != expected_repo_sha:
        return None
    if row.get("normalization_version") != NORMALIZATION_VERSION:
        return None

    rel_path = row.get("source_path")
    if not isinstance(rel_path, str) or not rel_path or os.path.isabs(rel_path) or ".." in rel_path.split("/"):
        return None
    full_path = os.path.abspath(os.path.join(root, rel_path))
    try:
        if os.path.commonpath([root, full_path]) != root:
            return None
        if _has_reparse_between(Path(root), Path(full_path)) or not os.path.isfile(full_path):
            return None
        raw = Path(full_path).read_bytes()
    except OSError:
        return None

    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != row.get("source_sha256"):
        return None

    source_kind = row.get("source_kind")
    b_start = int(row.get("byte_start") or 0)
    b_end = int(row.get("byte_end") or len(raw))
    if b_start < 0 or b_end > len(raw) or b_start >= b_end:
        return None

    if source_kind == "memory_md":
        sliced = raw[b_start:b_end].decode("utf-8", errors="replace")
        return _safe_sanitize_snippet(sliced)
    if source_kind in {"result_envelope", "run_receipt"}:
        extracted = _extract_json_summary_text(raw, source_kind)
        return _safe_sanitize_snippet(extracted)
    return None


def _pure_python_bm25_search(
    records: list[dict[str, Any]],
    tokens: list[str],
    *,
    cwd: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Pure-Python BM25 ranking over source-verified records."""
    if not records or not tokens:
        return []
    k1 = 1.5
    b = 0.75
    doc_tokens: list[Counter[str]] = []
    doc_lens: list[int] = []
    df: Counter[str] = Counter()

    for rec in records:
        words = [m.group(0).lower() for m in _TOKEN_RE.finditer(f"{rec.get('title', '')} {rec.get('content', '')}")]
        counts = Counter(words)
        doc_tokens.append(counts)
        doc_lens.append(len(words))
        for tok in set(words):
            df[tok] += 1

    n_docs = len(records)
    avgdl = (sum(doc_lens) / n_docs) if n_docs > 0 else 1.0
    scored: list[tuple[float, int]] = []

    for idx, rec in enumerate(records):
        counts = doc_tokens[idx]
        dl = doc_lens[idx]
        score = 0.0
        for q_tok in tokens:
            tf = counts.get(q_tok, 0)
            if tf <= 0:
                continue
            doc_freq = df.get(q_tok, 0)
            idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            denom = tf + k1 * (1.0 - b + b * (dl / max(1.0, avgdl)))
            score += idf * ((tf * (k1 + 1.0)) / denom)
        if score > 0.0:
            scored.append((score, idx))

    scored.sort(key=lambda item: (-item[0], records[item[1]]["source_path"], records[item[1]]["byte_start"]))
    hits: list[dict[str, Any]] = []
    for score, idx in scored:
        rec = records[idx]
        verified_snippet = _verify_and_reconstruct_hit(cwd, rec)
        if not verified_snippet:
            continue
        hits.append({
            "source_kind": rec["source_kind"],
            "source_path": rec["source_path"],
            "source_sha256": rec["source_sha256"],
            "byte_start": rec["byte_start"],
            "byte_end": rec["byte_end"],
            "title": _safe_sanitize_title(str(rec.get("title") or "")),
            "snippet": verified_snippet,
            "score": round(score, 4),
            "source_verified": True,
            "trust_tier": "advisory_search_hit",
        })
        if len(hits) >= limit:
            break
    return hits


def _sync_sqlite_cache(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    """Ensure SQLite FTS5 tables exist and sync only when source digests/offsets change."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_realpath_sha256 TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            byte_start INTEGER NOT NULL,
            byte_end INTEGER NOT NULL,
            normalization_version INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts
        USING fts5(title, content, content='memory_chunks', content_rowid='chunk_id')
        """
    )
    desired_sig = [
        (
            r["repo_realpath_sha256"],
            r["source_path"],
            r["source_sha256"],
            int(r["byte_start"]),
            int(r["byte_end"]),
            int(r["normalization_version"]),
        )
        for r in records
    ]
    existing_rows = conn.execute(
        """
        SELECT repo_realpath_sha256, source_path, source_sha256, byte_start, byte_end, normalization_version
        FROM memory_chunks
        ORDER BY chunk_id ASC
        """
    ).fetchall()
    existing_sig = [
        (
            row[0],
            row[1],
            row[2],
            int(row[3]),
            int(row[4]),
            int(row[5]),
        )
        for row in existing_rows
    ]
    if existing_sig == desired_sig:
        return

    with conn:
        conn.execute("DELETE FROM memory_chunks")
        conn.execute("INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('rebuild')")
        for rec in records:
            cur = conn.execute(
                """
                INSERT INTO memory_chunks (
                    repo_realpath_sha256, source_kind, source_path, source_sha256,
                    byte_start, byte_end, normalization_version, title, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["repo_realpath_sha256"],
                    rec["source_kind"],
                    rec["source_path"],
                    rec["source_sha256"],
                    rec["byte_start"],
                    rec["byte_end"],
                    rec["normalization_version"],
                    rec["title"],
                    rec["content"],
                ),
            )
            rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_chunks_fts(rowid, title, content) VALUES (?, ?, ?)",
                (rowid, rec["title"], rec["content"]),
            )


def index_workspace_memory(
    cwd: str,
    *,
    results_dir: str | None = None,
) -> dict[str, Any]:
    """Pre-warm or refresh the workspace SQLite FTS5 memory cache."""
    _ = results_dir
    root = os.path.abspath(cwd or os.getcwd())
    records = _discover_sources(root)
    db_path = _cache_db_path(root)
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            _sync_sqlite_cache(conn, records)
            return {
                "schema": MEMORY_SEARCH_SCHEMA,
                "engine": "sqlite_fts5",
                "indexed_chunks": len(records),
                "cache_path": os.path.relpath(db_path, root).replace("\\", "/"),
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {
            "schema": MEMORY_SEARCH_SCHEMA,
            "engine": "python_bm25_fallback",
            "indexed_chunks": len(records),
        }


def search_memory(
    query: str,
    *,
    cwd: str | None = None,
    limit: int = 10,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Search project-local `.agents/memory.md`, `handoff_note`s, and run syntheses."""
    root = os.path.abspath(cwd or os.getcwd())
    max_hits = max(1, min(50, int(limit)))
    tokens, fts_expr = sanitize_fts_query(query)
    records = _discover_sources(root)

    if not tokens:
        return {
            "schema": MEMORY_SEARCH_SCHEMA,
            "query": query,
            "sanitized_tokens": [],
            "engine": "none",
            "indexed_chunks": len(records),
            "hits": [],
        }

    if not force_fallback:
        db_path = _cache_db_path(root)
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                conn.row_factory = sqlite3.Row
                _sync_sqlite_cache(conn, records)

                rows = conn.execute(
                    """
                    SELECT
                        c.repo_realpath_sha256,
                        c.source_kind,
                        c.source_path,
                        c.source_sha256,
                        c.byte_start,
                        c.byte_end,
                        c.normalization_version,
                        c.title,
                        c.content,
                        bm25(memory_chunks_fts) AS raw_bm25
                    FROM memory_chunks_fts
                    JOIN memory_chunks c ON memory_chunks_fts.rowid = c.chunk_id
                    WHERE memory_chunks_fts MATCH ?
                    ORDER BY raw_bm25 ASC
                    LIMIT ?
                    """,
                    (fts_expr, max_hits * 2),
                ).fetchall()

                hits: list[dict[str, Any]] = []
                for r in rows:
                    row_dict = dict(r)
                    verified_snippet = _verify_and_reconstruct_hit(root, row_dict)
                    if not verified_snippet:
                        continue
                    raw_score = float(row_dict.get("raw_bm25") or 0.0)
                    hits.append({
                        "source_kind": row_dict["source_kind"],
                        "source_path": row_dict["source_path"],
                        "source_sha256": row_dict["source_sha256"],
                        "byte_start": row_dict["byte_start"],
                        "byte_end": row_dict["byte_end"],
                        "title": _safe_sanitize_title(str(row_dict.get("title") or "")),
                        "snippet": verified_snippet,
                        "score": round(abs(raw_score), 4),
                        "source_verified": True,
                        "trust_tier": "advisory_search_hit",
                    })
                    if len(hits) >= max_hits:
                        break

                return {
                    "schema": MEMORY_SEARCH_SCHEMA,
                    "query": query,
                    "sanitized_tokens": tokens,
                    "engine": "sqlite_fts5",
                    "cache_path": os.path.relpath(db_path, root).replace("\\", "/"),
                    "indexed_chunks": len(records),
                    "hits": hits,
                }
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            pass

    fallback_hits = _pure_python_bm25_search(
        records, tokens, cwd=root, limit=max_hits
    )
    return {
        "schema": MEMORY_SEARCH_SCHEMA,
        "query": query,
        "sanitized_tokens": tokens,
        "engine": "python_bm25_fallback",
        "indexed_chunks": len(records),
        "hits": fallback_hits,
    }


def format_memory_search_human(payload: dict[str, Any]) -> str:
    """Render ``summon.memory-search/v1`` in a concise human-readable format."""
    hits = payload.get("hits") or []
    lines = [
        f"Memory Search: {payload.get('query')!r} "
        f"(engine={payload.get('engine')}, indexed={payload.get('indexed_chunks', 0)}, hits={len(hits)})"
    ]
    if not hits:
        lines.append("  No matching verified memory or handoff records found.")
        return "\n".join(lines)
    for idx, hit in enumerate(hits, start=1):
        lines.append(
            f"  [{idx}] {hit.get('source_path')} ({hit.get('source_kind')}, score={hit.get('score')})"
        )
        snippet_lines = str(hit.get("snippet") or "").splitlines()
        preview = " ".join(s.strip() for s in snippet_lines if s.strip())[:200]
        lines.append(f"      {preview}")
    return "\n".join(lines)
