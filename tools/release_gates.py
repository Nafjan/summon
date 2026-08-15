#!/usr/bin/env python3
"""Run Summon's fixed release-test registry and emit bound evidence.

This runner is deliberately boring: it executes only the checked-in commands,
captures bounded output, and refuses to emit evidence when any command fails or
the source tree changes during the run. It never launches a provider directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "summon" / "scripts"
_CUSTOM_RESULT = re.compile(r"(?m)([0-9]+)/([0-9]+) passed\s*$")
_UNIT_RESULT = re.compile(r"Ran\s+([0-9]+)\s+tests?", re.IGNORECASE)


def _absolute_lexical(value: str | os.PathLike[str]) -> Path:
    """Absolute path without dereferencing a final symlink."""
    return Path(os.path.abspath(os.fspath(value)))


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Windows does not expose a directory fsync; the file itself is still
        # flushed before replace.
        pass


def _load_manifest_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_release_manifest_for_gates", Path(__file__).with_name("release_manifest.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load release manifest")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MANIFEST = _load_manifest_module()
# One canonical registry is shared by manifest validation, execution, and CI
# review.  The runner never accepts caller-supplied command text.
COMMANDS = dict(_MANIFEST.REQUIRED_COMMANDS)
REQUIRED_TESTS = frozenset(COMMANDS)
GATE_COMMANDS = dict(_MANIFEST.REQUIRED_GATE_COMMANDS)
REQUIRED_GATES = frozenset(_MANIFEST.REQUIRED_GATES)


def _canonical_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in {".md", ".json", ".py", ".txt", ".yml", ".yaml"}:
        data = data.replace(b"\r\n", b"\n")
    return data


def _source_hash() -> str:
    return _MANIFEST.source_tree_sha256(ROOT)


def _git_head() -> str:
    env = _MANIFEST._git_env()
    top = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "--show-toplevel"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=True,
    ).stdout.strip()
    if Path(top).resolve() != ROOT.resolve():
        raise RuntimeError("Git repository top-level does not match release root")
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "HEAD"], cwd=str(ROOT), env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=10, check=True,
    )
    return result.stdout.strip()


def _argv(name: str) -> list[str]:
    command = COMMANDS.get(name)
    if not isinstance(command, str):
        raise KeyError(name)
    args = shlex.split(command, posix=True)
    if not args or args[0] != "python":
        raise RuntimeError(f"invalid canonical command for {name}")
    return [sys.executable, *args[1:]]


def _gate_argv(name: str) -> list[str]:
    command = GATE_COMMANDS.get(name)
    if not isinstance(command, str):
        raise KeyError(name)
    args = shlex.split(command, posix=True)
    if not args or args[0] != "python":
        raise RuntimeError(f"invalid canonical gate command for {name}")
    return [sys.executable, *args[1:]]


def _parse_count(name: str, output: str) -> str:
    matches = list(_CUSTOM_RESULT.finditer(output))
    if matches:
        passed, total = matches[-1].groups()
        if passed != total or int(total) <= 0:
            raise RuntimeError(f"{name} reported a non-passing count {passed}/{total}")
        return f"{passed}/{total}"
    matches = list(_UNIT_RESULT.finditer(output))
    if matches and re.search(r"(?m)^OK\s*$", output):
        total = int(matches[-1].group(1))
        if total <= 0:
            raise RuntimeError(f"{name} reported no tests")
        return f"{total}/{total}"
    raise RuntimeError(f"{name} produced no recognized passing test count")


_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "PYTHONIOENCODING", "PYTHONUNBUFFERED", "LANG", "LC_ALL",
    "CI", "RUNNER_TEMP", "SUMMON_LIVE_PROVIDER_RECEIPT",
}


def _hermetic_environment() -> dict[str, str]:
    """Keep platform basics while removing credentials, proxies, and backend knobs."""
    env = {}
    for key, value in os.environ.items():
        if key.upper() not in _ENV_ALLOWLIST:
            continue
        env[key] = value
    env.update({
        "SUMMON_ACP_FALLBACK": "0",
        "SUMMON_TELEMETRY": "0",
        "SUMMON_TRANSIENT_RETRIES": "0",
        "CI": "1",
    })
    return env


def run_suite(name: str, timeout: float) -> tuple[str, str]:
    env = _hermetic_environment()
    result = subprocess.run(
        _argv(name), cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        tail = output[-2000:].replace("\x00", "")
        raise RuntimeError(f"{name} failed with exit {result.returncode}: {tail}")
    return _parse_count(name, output), hashlib.sha256(output.encode("utf-8")).hexdigest()


def _marker(output: str, name: str) -> dict[str, object] | None:
    """Read the final bounded JSON gate marker, if a gate emits one."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{") or len(line) > 4096:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("gate") == name:
            return value
    return None


def _artifact(name: str, *, status: str, command: str, source_hash: str,
              git_head: str, output: str, test_count: str | None = None,
              marker: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "gate": name,
        "status": status,
        "command": command,
        "source_tree_sha256": source_hash,
        "git_head": git_head,
        "producer": "tools/release_gates.py",
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    if test_count is not None:
        payload["test_count"] = test_count
    if marker:
        # Keep only the fixed marker fields; no provider output or arbitrary
        # environment/path data enters a release artifact.
        # Never copy a producer's own artifact hash into the outer artifact:
        # doing so makes the outer hash depend on a value that is not retained
        # in the returned record, so release_manifest cannot recompute it.
        # A gate may still expose a bounded evidence-file name and error detail.
        for key in ("error_kind", "detail", "evidence_file"):
            if key in marker and isinstance(marker[key], str):
                payload[key] = marker[key]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    payload["artifact_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def _write_artifact(directory: Path | None, artifact: dict[str, object]) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"gate.{artifact['gate']}.json"
    if path.is_symlink():
        raise ValueError("refusing to replace a symlinked gate artifact")
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run_gate(name: str, timeout: float, *, source_hash: str, git_head: str,
             artifact_dir: Path | None = None) -> tuple[str, dict[str, object]]:
    command = GATE_COMMANDS[name]
    env = _hermetic_environment()
    result = subprocess.run(
        _gate_argv(name), cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    marker = _marker(output, name)
    if marker is not None and marker.get("status") == "blocked":
        status = "blocked"
        artifact = _artifact(name, status=status, command=command,
                             source_hash=source_hash, git_head=git_head,
                             output=output, marker=marker)
    elif result.returncode == 0:
        count = None
        if name != "live_provider":
            count = _parse_count(name, output)
        status = "pass"
        artifact = _artifact(name, status=status, command=command,
                             source_hash=source_hash, git_head=git_head,
                             output=output, test_count=count, marker=marker)
    else:
        status = "blocked"
        artifact = _artifact(name, status=status, command=command,
                             source_hash=source_hash, git_head=git_head,
                             output=output,
                             marker={"error_kind": "gate_command_failed",
                                     "detail": f"exit {result.returncode}"})
    _write_artifact(artifact_dir, artifact)
    return status, artifact


def build_evidence(timeout: float, *, require_clean: bool = False,
                   artifact_dir: Path | None = None) -> dict[str, object]:
    before_hash = _source_hash()
    head = _git_head()
    status_before = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        cwd=str(ROOT), env=_MANIFEST._git_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=10, check=True,
    ).stdout
    if require_clean and status_before.strip():
        raise RuntimeError("--require-clean refuses a dirty source tree")
    results = {name: run_suite(name, timeout) for name in COMMANDS}
    gate_results: dict[str, dict[str, object]] = {}
    gate_status: dict[str, str] = {}
    for name in sorted(GATE_COMMANDS):
        status, artifact = run_gate(
            name, timeout, source_hash=before_hash, git_head=head,
            artifact_dir=artifact_dir,
        )
        gate_status[name] = status
        gate_results[name] = artifact
    after_hash = _source_hash()
    status_after = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        cwd=str(ROOT), env=_MANIFEST._git_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=10, check=True,
    ).stdout
    after_head = _git_head()
    if before_hash != after_hash or head != after_head or status_before != status_after:
        raise RuntimeError("source or Git state changed while release suites ran")
    tests = {name: value[0] for name, value in results.items()}
    return {
        "schema": 1,
        "producer": "tools/release_gates.py",
        "producer_sha256": hashlib.sha256(_canonical_bytes(Path(__file__))).hexdigest(),
        "source_tree_sha256": before_hash,
        "git_head": head,
        "git_status_clean": not bool(status_before.strip()),
        "version_contract": _MANIFEST._version_contract(ROOT),
        "tests": tests,
        "known_gates": gate_status,
        "gate_results": gate_results,
        "gate_artifact_dir": (artifact_dir.name if artifact_dir is not None else None),
        "commands": dict(COMMANDS),
        "gate_commands": dict(GATE_COMMANDS),
        "test_results": {
            name: {"count": count, "output_sha256": output_sha256}
            for name, (count, output_sha256) in results.items()
        },
        "runtime": {"python": platform.python_version(), "system": platform.platform()},
        "captured_at_unix": int(time.time()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", help="write evidence JSON to this path")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--require-clean", action="store_true",
                        help="refuse to run unless the Git worktree is clean")
    args = parser.parse_args(argv)
    try:
        artifact_dir = None
        if args.output:
            output_path = _absolute_lexical(args.output)
            artifact_dir = output_path.with_name(output_path.stem + ".gates")
        evidence = build_evidence(args.timeout, require_clean=args.require_clean,
                                  artifact_dir=artifact_dir)
        encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = _absolute_lexical(args.output)
            if output.is_symlink():
                raise ValueError("refusing to replace a symlinked evidence output")
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp",
                                             dir=str(output.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, output)
                _fsync_parent(output)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        else:
            sys.stdout.write(encoded)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
