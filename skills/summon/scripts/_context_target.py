"""Trusted local target adapter for the provider-inert context compiler."""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Mapping

from _context_compile import (
    ContextCompileError, MAX_BLOCKS, MAX_BLOCK_BYTES, MAX_SERIALIZED_BYTES,
    REFERENCE_PROOF_SCHEMA, _mint_verified_reference_proof,
)


def _content_reference(value: object) -> str:
    if (not isinstance(value, str) or not value.startswith("sha256:")
            or len(value) != 71):
        raise ContextCompileError(
            "reference_proof_invalid", "artifact reference must be content-addressed")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ContextCompileError(
            "reference_proof_invalid", "artifact reference must be content-addressed") from exc
    return value


def _resolve_allowed_file(path: str, allowed_roots: list[str] | tuple[str, ...]) -> tuple[str, int, str]:
    """Resolve a target to one explicit root and a safe slash-relative locator."""
    if not isinstance(path, str) or not path or not allowed_roots:
        raise ContextCompileError("context_target_invalid", "context target is invalid")
    try:
        lexical_target = os.path.abspath(path)
        target = os.path.realpath(lexical_target)
        roots = [os.path.realpath(os.path.abspath(root)) for root in allowed_roots]
        matches = [
            (index, root) for index, root in enumerate(roots)
            if os.path.isdir(root)
            and os.path.normcase(os.path.commonpath((target, root))) == os.path.normcase(root)
        ]
    except (OSError, ValueError, TypeError):
        raise ContextCompileError("context_target_invalid", "context target is invalid")
    if not matches:
        raise ContextCompileError(
            "context_target_outside_allowlist",
            "context target is outside the explicit readable roots")
    # File references are reopened by a delegated agent using a cwd-relative
    # locator. Reject symlink/junction/reparse indirection instead of attempting
    # to reason about a path-swap race across process boundaries.
    if os.path.normcase(lexical_target) != os.path.normcase(target):
        raise ContextCompileError(
            "context_target_indirect", "context target uses an indirect filesystem path")
    index, root = matches[0]
    relative = os.path.relpath(target, root).replace("\\", "/")
    if relative in {"", "."} or relative.startswith("../") or relative.startswith("/"):
        raise ContextCompileError("context_target_invalid", "context target locator is invalid")
    return target, index, relative


def _final_open_path(fd: int) -> str | None:
    """Return the kernel-resolved name of an open handle where supported."""
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            handle = msvcrt.get_osfhandle(fd)
            size = 32768
            buf = ctypes.create_unicode_buffer(size)
            written = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(handle), buf, size, 0)
            if not written or written >= size:
                return None
            value = buf.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return os.path.realpath(os.path.abspath(value))
        except (ImportError, OSError, ValueError, AttributeError):
            return None
    proc_link = f"/proc/self/fd/{fd}"
    try:
        return os.path.realpath(os.readlink(proc_link))
    except OSError:
        return None


def _allowed_file(path: str, allowed_roots: list[str] | tuple[str, ...], *,
                  maximum_bytes: int = MAX_SERIALIZED_BYTES) -> tuple[bytes, int, str]:
    """Read one stable regular file through an explicit local allowlist."""
    target, root_index, relative = _resolve_allowed_file(path, allowed_roots)
    try:
        with open(target, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ContextCompileError(
                    "context_target_invalid", "context target is not a regular file")
            opened_before = _final_open_path(handle.fileno())
            if opened_before is None:
                raise ContextCompileError(
                    "context_target_unverifiable",
                    "context target final handle path could not be verified")
            if os.path.normcase(opened_before) != os.path.normcase(target):
                raise ContextCompileError(
                    "context_target_changed", "context target changed during verification")
            raw = handle.read(maximum_bytes + 1)
            after = os.fstat(handle.fileno())
            opened_after = _final_open_path(handle.fileno())
    except ContextCompileError:
        raise
    except OSError as exc:
        raise ContextCompileError(
            "context_target_unreadable", "context target could not be read") from exc
    if len(raw) > maximum_bytes:
        raise ContextCompileError("context_too_large", "context target exceeds the size limit")
    before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_id != after_id or after.st_size != len(raw):
        raise ContextCompileError(
            "context_target_changed", "context target changed during verification")
    if opened_after is None:
        raise ContextCompileError(
            "context_target_unverifiable",
            "context target final handle path could not be verified")
    if (os.path.normcase(opened_after) != os.path.normcase(target)
            or os.path.normcase(opened_before) != os.path.normcase(opened_after)):
        raise ContextCompileError(
            "context_target_changed", "context target changed during verification")
    return raw, root_index, relative


def read_context_file(path: str, allowed_roots: list[str] | tuple[str, ...]) -> bytes:
    """Read an input context from an allowed root without exposing its path."""
    raw, _, _ = _allowed_file(path, allowed_roots)
    return raw


def make_verified_reference_proof(
        reference_files: Mapping[str, str],
        allowed_roots: list[str] | tuple[str, ...]):
    """Hash-read CLI reference targets and create the compiler's trusted proof."""
    try:
        bindings = dict(reference_files)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ContextCompileError(
            "reference_proof_invalid", "reference bindings are invalid") from exc
    if len(bindings) > MAX_BLOCKS:
        raise ContextCompileError("reference_proof_invalid", "too many reference bindings")
    references = {}
    for reference, path in bindings.items():
        checked = _content_reference(reference)
        if not allowed_roots:
            raise ContextCompileError(
                "reference_proof_invalid", "reference bindings require a dispatch cwd")
        raw, _, relative = _allowed_file(
            path, [allowed_roots[0]], maximum_bytes=MAX_BLOCK_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if checked[7:] != digest:
            raise ContextCompileError(
                "reference_target_mismatch",
                "context reference does not match the hash-read target")
        references[checked] = {
            "reference": checked,
            "sha256": digest,
            "verified_target": True,
            "verification_method": "sha256-readback",
            "target_locator": {
                "root": "cwd",
                "path": relative,
            },
        }
    return _mint_verified_reference_proof(
        {"schema": REFERENCE_PROOF_SCHEMA, "references": references})
