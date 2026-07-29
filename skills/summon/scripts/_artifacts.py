"""Loose-file provenance for document and artifact reviews.

Git HEAD identifies tracked repository inputs. Large audits often operate on loose
PDF/DOCX/XLSX files instead, so HEAD says nothing about the baseline the reviewer
was given. ``--artifact`` opts named files into a small, deterministic manifest:
path, byte size, SHA-256, and a page count where the file format exposes one
through the Python standard library.

The manifest is evidence about the bytes summon measured, not proof that a
third-party CLI read every byte. A second snapshot after dispatch records whether
the named baseline stayed stable while the review ran.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from xml.etree import ElementTree


_DOCX_APP_XML = "docProps/app.xml"
_MAX_METADATA_BYTES = 1024 * 1024


def _page_count(path: str) -> tuple[int | None, str | None]:
    """Return a documented page count when stdlib can read authoritative metadata.

    DOCX stores the last-saved page count in ``docProps/app.xml``. It can be stale
    until Word/LibreOffice saves the document, so the source is named explicitly.
    PDF page trees may be compressed into object streams; a regex count would look
    precise while being wrong, so PDF and other formats honestly return null.
    """
    if os.path.splitext(path)[1].lower() != ".docx":
        return None, None
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(_DOCX_APP_XML)
            if info.file_size > _MAX_METADATA_BYTES:
                return None, None
            raw = zf.read(info)
        root = ElementTree.fromstring(raw)
        node = next((n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "Pages"), None)
        value = int((node.text or "").strip()) if node is not None else 0
        if value > 0:
            return value, "docx_app_properties"
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        pass
    return None, None


def _contained_file(root: str, raw_path: str) -> tuple[str | None, str | None]:
    """Resolve one artifact under root, rejecting escapes and non-files."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "artifact paths must be non-empty strings"
    candidate = (raw_path if os.path.isabs(raw_path)
                 else os.path.join(root, raw_path))
    candidate = os.path.abspath(os.path.normpath(candidate))
    root_real = os.path.realpath(root)
    candidate_real = os.path.realpath(candidate)
    try:
        if os.path.commonpath((root_real, candidate_real)) != root_real:
            return None, f"artifact is outside --cwd: {raw_path}"
    except ValueError:
        return None, f"artifact is outside --cwd: {raw_path}"
    try:
        st = os.stat(candidate_real)
    except OSError as e:
        return None, f"cannot stat artifact {raw_path}: {e}"
    if not stat.S_ISREG(st.st_mode):
        return None, f"artifact is not a regular file: {raw_path}"
    return candidate_real, None


def build_manifest(paths, cwd: str | None) -> tuple[dict | None, str | None]:
    """Return ``(manifest, error)`` for repeatable ``--artifact`` inputs.

    Paths are constrained to ``cwd`` because that is the file surface the child is
    expected to inspect. The hash primitive is shared with request fingerprinting:
    it reads in bounded chunks and refuses a file whose size/mtime changes mid-read.
    """
    if not paths:
        return None, None
    if not cwd:
        return None, "--artifact requires --cwd"
    root = os.path.abspath(cwd)
    if not os.path.isdir(root):
        return None, f"--artifact cannot snapshot a missing cwd: {root}"

    # Imported lazily to avoid a module cycle: _executor calls build_manifest from
    # build_request_identity after _executor itself has finished importing.
    from _executor import content_sha

    files = []
    seen = set()
    for raw_path in paths:
        path, err = _contained_file(root, raw_path)
        if err:
            return None, err
        rel = os.path.relpath(path, os.path.realpath(root)).replace(os.sep, "/")
        key = os.path.normcase(rel)
        if key in seen:
            return None, f"duplicate --artifact path: {raw_path}"
        seen.add(key)
        try:
            before = os.stat(path)
            sha = content_sha(path)
            after = os.stat(path)
        except OSError as e:
            return None, f"cannot read artifact {raw_path}: {e}"
        if not sha or (before.st_size, before.st_mtime_ns) != (
                after.st_size, after.st_mtime_ns):
            return None, f"artifact changed or became unreadable while hashing: {raw_path}"
        pages, page_source = _page_count(path)
        try:
            final = os.stat(path)
        except OSError as e:
            return None, f"cannot re-stat artifact {raw_path}: {e}"
        if (before.st_size, before.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            return None, f"artifact changed while reading metadata: {raw_path}"
        files.append({
            "path": rel,
            "sha256": sha,
            "bytes": final.st_size,
            "page_count": pages,
            "page_count_source": page_source,
        })

    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("ascii")
    return {
        "version": 1,
        "root": root,
        "files": files,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }, None


def changed_paths(before: dict, after: dict | None) -> list[str]:
    """Artifact paths whose recorded identity differs between two manifests."""
    if not isinstance(after, dict):
        return [f.get("path") for f in before.get("files", []) if f.get("path")]
    old = {f.get("path"): (f.get("sha256"), f.get("bytes"))
           for f in before.get("files", []) if isinstance(f, dict)}
    new = {f.get("path"): (f.get("sha256"), f.get("bytes"))
           for f in after.get("files", []) if isinstance(f, dict)}
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
