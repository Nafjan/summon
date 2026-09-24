#!/usr/bin/env python3
"""Run Summon's fixed release-test registry and emit bound evidence.

This runner is deliberately boring: it executes only the checked-in commands,
captures structured outcomes, and refuses release acceptance for failed or
incomplete cases. Diagnostic evidence may retain blocking outcomes; source
changes invalidate the run. It never launches a provider directly.
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
_PYTEST_RESULT = re.compile(
    r"(?m)^[= ]*([1-9][0-9]*) passed"
    r"(?P<extras>(?:, [0-9]+ [a-z]+(?: passed)?)*) "
    r"in [0-9.]+s(?: \([^\r\n]+\))?[= ]*$"
)
_SAFE_ERROR_KINDS = frozenset({
    "gate_command_failed",
    "live_provider_evidence_missing",
    "live_provider_evidence_invalid",
})
_SAFE_EVIDENCE_FILE = "redacted-live-provider-receipt.json"
# These gates include Windows-only rollback/OS-privacy evidence.  A non-Windows
# run may execute the portable portions, but it must not project that run as
# qualification of the Windows release boundary.
WINDOWS_SCOPED_GATES = frozenset({
    "migration_rollback", "browser_security", "accessibility",
})
_RENDERED_FILE_PATTERNS = (
    re.compile(r"(?:browser-toolchain|measurements|u08-observations|native-zoom-measurements)\.json\Z"),
    re.compile(r"native-zoom-(?:1|2|4)-(?:workspace|artifact)\.png\Z"),
    re.compile(r"(?:canonical-artifact-separate-scope|canonical-focal-task-scope|"
               r"terminal-guidance-and-model-uncertainty|pending-action-authentication-expiry)\.png\Z"),
    re.compile(r"(?:authentication-focus|desktop-workspace-focus|"
               r"(?:desktop|200-percent-equivalent|400-percent-equivalent|320-css-pixel-reflow)-"
               r"(?:workspace|drawer)|reduced-motion-timeline-focus)\.png\Z"),
    re.compile(r"u04-worker-(?:accepted-and-queued|included|not-submitted-and-acknowledged|failure|"
               r"(?:submission-started|submitted|acknowledged|not-submitted)|"
               r"state-card-(?:accepted|queued|included-in-attempt|submission-started|submitted|"
               r"acknowledged|not-submitted))\.png\Z"),
    re.compile(r"u08-(?:writer-loss-pending|natural-backoff-stale-owner|"
               r"coalesced-reconnection-notice|pending-key-during-backoff|"
               r"authorized-same-key-continuation|unresolved-effects-after-recovery|failure-shell)\.png\Z"),
)
_MAX_RENDERED_FILES = 64
_MAX_RENDERED_FILE_BYTES = 8 * 1024 * 1024


def _absolute_lexical(value: str | os.PathLike[str]) -> Path:
    """Absolute path without dereferencing a final symlink."""
    return Path(os.path.abspath(os.fspath(value)))


def _assert_external_path(path: Path, label: str) -> None:
    """Reject evidence output inside the source checkout.

    Release evidence is bound to a clean source tree.  Resolve existing
    parents so a symlink/junction alias cannot bypass the containment check.
    """
    try:
        root_real = ROOT.resolve(strict=False)
        path_real = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path cannot be resolved safely") from exc
    try:
        path_real.relative_to(root_real)
    except ValueError:
        return
    raise ValueError(f"{label} must be outside the release source tree")


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
_SAFE_ERROR_KINDS = _MANIFEST._OUTCOMES.GATE_DIAGNOSTIC_CODES
_SAFE_EVIDENCE_FILE = _MANIFEST._OUTCOMES.GATE_EVIDENCE_FILE
# One canonical registry is shared by manifest validation, execution, and CI
# review.  The runner never accepts caller-supplied command text.
COMMANDS = dict(_MANIFEST.REQUIRED_COMMANDS)
REQUIRED_TESTS = frozenset(COMMANDS)
GATE_COMMANDS = dict(_MANIFEST.REQUIRED_GATE_COMMANDS)
REQUIRED_GATES = frozenset(_MANIFEST.REQUIRED_GATES)

# Git metadata reads can briefly exceed ten seconds on Windows when antivirus,
# filesystem indexing, or another test process is walking the same checkout.
# Keep this bounded, but do not let a healthy clean-tree check invalidate an
# otherwise complete release run because of a transient storage stall.
GIT_METADATA_TIMEOUT_SECONDS = 60.0
# The Windows aggregate is intentionally serialized.  The current provider-free
# evidence run showed the two large partitions completing below this bound,
# while the former 900-second default could expire under ordinary browser and
# process-tree contention.  This remains a bounded per-child timeout; it is not
# permission to accept partial output or to skip a slow partition.
RELEASE_SUITE_TIMEOUT_SECONDS = 1800.0


def _canonical_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in {".md", ".json", ".py", ".txt", ".yml", ".yaml"}:
        data = data.replace(b"\r\n", b"\n")
    return data


def _raw_sha256_file(path: Path) -> str:
    """Hash rendered artifacts byte-for-byte; line-ending normalization is not allowed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hash() -> str:
    return _MANIFEST.source_tree_sha256(ROOT)


def _git_head() -> str:
    env = _MANIFEST._git_env()
    top = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "--show-toplevel"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=GIT_METADATA_TIMEOUT_SECONDS, check=True,
        **_MANIFEST.run_flags(),
    ).stdout.strip()
    if Path(top).resolve() != ROOT.resolve():
        raise RuntimeError("Git repository top-level does not match release root")
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "HEAD"], cwd=str(ROOT), env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=GIT_METADATA_TIMEOUT_SECONDS, check=True,
        **_MANIFEST.run_flags(),
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
    matches = list(_PYTEST_RESULT.finditer(output))
    if matches:
        passed = int(matches[-1].group(1))
        extras = matches[-1].group("extras") or ""
        counts = [(int(count), kind) for count, kind in re.findall(
            r", ([0-9]+) ([a-z]+)", extras)]
        unsupported = [(count, kind) for count, kind in counts
                       if kind in {"xfailed", "xpassed", "deselected"} and count]
        if unsupported:
            raise RuntimeError(
                f"{name} reported unsupported incomplete pytest outcomes")
        skipped = sum(count for count, kind in counts if kind == "skipped")
        return f"{passed}/{passed + skipped}"
    raise RuntimeError(f"{name} produced no recognized passing test count")


_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "PYTHONIOENCODING", "PYTHONUNBUFFERED", "LANG", "LC_ALL",
    "CI", "RUNNER_TEMP", "SUMMON_LIVE_PROVIDER_RECEIPT",
}


def _validated_external_directory(value: str | os.PathLike[str]) -> Path:
    """Return an owned, external directory for synthetic rendered evidence."""
    path = _absolute_lexical(value)
    _assert_external_path(path, "rendered evidence")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("rendered evidence directory must be a real directory")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("rendered evidence directory must not be a symlink")
    return path


def _prepare_rendered_evidence_directory(value: Path) -> Path:
    """Require a fresh run-owned rendered directory before any child starts."""
    path = _validated_external_directory(value)
    if any(path.iterdir()):
        raise RuntimeError("rendered evidence directory must be empty for a fresh run")
    return path


def _validated_browser_executable(value: str | os.PathLike[str]) -> Path:
    """Validate an explicitly selected browser binary without trusting ambient env."""
    path = _absolute_lexical(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("explicit Chromium executable must be an existing regular file")
    return path


def _hermetic_environment(*, rendered_evidence_dir: Path | None = None,
                          chromium_executable: str | os.PathLike[str] | None = None
                          ) -> dict[str, str]:
    """Keep platform basics while removing credentials, proxies, and backend knobs.

    The two UI variables are opt-in function arguments, never inherited from the
    caller's environment.  The runner owns the evidence directory and only
    forwards a browser executable after explicit regular-file validation.
    """
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
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    if rendered_evidence_dir is not None:
        env["SUMMON_UI_RENDERED_EVIDENCE_DIR"] = str(
            _validated_external_directory(rendered_evidence_dir))
    if chromium_executable is not None:
        env["SUMMON_UI_CHROMIUM_EXECUTABLE"] = str(
            _validated_browser_executable(chromium_executable))
    return env


def _platform_qualification(gate_status: dict[str, str]) -> dict[str, object]:
    """Project platform-scoped release truth without promoting unavailable proof."""
    host_platform = "windows" if os.name == "nt" else "non_windows"
    windows_evidence = "available" if host_platform == "windows" else "unavailable"
    scoped_gates = {
        name: (gate_status.get(name) if host_platform == "windows" else "unavailable")
        for name in sorted(WINDOWS_SCOPED_GATES)
    }
    return {
        "schema": 1,
        "host": host_platform,
        "windows_evidence": windows_evidence,
        "windows_scoped_gates": scoped_gates,
        "status": (
            "qualified"
            if host_platform == "windows" and all(value == "pass" for value in scoped_gates.values())
            else "incomplete"
        ),
    }


def _rendered_file_allowed(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in _RENDERED_FILE_PATTERNS)


def _collect_rendered_evidence(directory: Path, source_hash: str) -> dict[str, object]:
    """Bind a fresh, bounded, source-attributed synthetic rendered bundle."""
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("rendered evidence directory is unavailable")
    entries = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or not _rendered_file_allowed(path.name):
            raise RuntimeError("rendered evidence contains an unexpected file")
        size = path.stat().st_size
        if size > _MAX_RENDERED_FILE_BYTES:
            raise RuntimeError("rendered evidence file exceeds the bounded size")
        entries.append({
            "name": path.name,
            "bytes": size,
            "sha256": _raw_sha256_file(path),
        })
    if len(entries) > _MAX_RENDERED_FILES:
        raise RuntimeError("rendered evidence contains too many files")
    return {
        "schema": 1,
        "policy": "synthetic-loopback-only",
        "producer": "tools/release_gates.py",
        "source_tree_sha256": source_hash,
        "producers": ["workspace_ui", "browser_security", "accessibility"],
        "files": entries,
    }

def validate_incomplete_diagnostics(path: Path, root: Path) -> None:
    """Validate Ubuntu producer diagnostics without constructing a manifest.

    Incomplete outcomes remain incomplete. This entry point grants no release
    eligibility and does not change strict manifest intake or final checking.
    """
    if path.is_symlink():
        raise ValueError("diagnostic evidence may not be a symlink")
    value = _MANIFEST._OUTCOMES.read_json(path)
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 2:
        raise ValueError("diagnostics require current structured evidence")
    source_hash = _MANIFEST.source_tree_sha256(root)
    git = _MANIFEST._git_facts(root)
    if (value.get("source_tree_sha256") != source_hash
            or not isinstance(value.get("git_head"), str)
            or value["git_head"] != git.get("head")
            or value.get("git_status_clean") is not True or git.get("dirty") is not False):
        raise ValueError("diagnostic source binding does not match a clean tree")
    if (value.get("producer") != "tools/release_gates.py"
            or value.get("producer_sha256") != _MANIFEST._sha256_file(root / "tools/release_gates.py")
            or value.get("version_contract") != _MANIFEST._version_contract(root)):
        raise ValueError("diagnostic producer or version contract does not match")
    if (value.get("commands") != _MANIFEST.REQUIRED_COMMANDS
            or value.get("gate_commands") != _MANIFEST.REQUIRED_GATE_COMMANDS
            or not isinstance(value.get("tests"), dict)
            or set(value["tests"]) != _MANIFEST.REQUIRED_TESTS
            or not isinstance(value.get("known_gates"), dict)
            or set(value["known_gates"]) != _MANIFEST.REQUIRED_GATES):
        raise ValueError("diagnostic registry does not match")
    qualification = _MANIFEST._validate_platform_qualification(value.get("platform_qualification"))
    if (qualification["host"] != "non_windows" or qualification["status"] != "incomplete"
            or qualification["windows_evidence"] != "unavailable"
            or set(qualification["windows_scoped_gates"].values()) != {"unavailable"}
            or value["known_gates"].get("live_provider") != "blocked"):
        raise ValueError("diagnostics cannot claim platform or live-provider qualification")
    _MANIFEST._validate_machine_outcomes(value, root, require_pass=False)
    for name, row in value["test_results"].items():
        if value["tests"][name] != row["count"]:
            raise ValueError("diagnostic display counts do not match")
    for name, artifact in value["gate_results"].items():
        _MANIFEST._OUTCOMES.validate_gate_artifact(
            artifact, root=root, name=name, status=value["known_gates"].get(name),
            command=_MANIFEST.REQUIRED_GATE_COMMANDS[name], source_hash=source_hash,
            git_head=value["git_head"])
    rendered = _MANIFEST._validate_rendered_evidence(value.get("rendered_evidence"), source_hash)
    directory = path.with_name(path.stem + ".gates") / "rendered"
    if _collect_rendered_evidence(directory, source_hash) != rendered:
        raise ValueError("diagnostic rendered files do not match their source binding")

def _child_environment(*, rendered_evidence_dir: Path | None = None,
                       chromium_executable: str | os.PathLike[str] | None = None
                       ) -> dict[str, str]:
    """Preserve the zero-argument test seam for ordinary non-rendered gates."""
    if rendered_evidence_dir is None and chromium_executable is None:
        return _hermetic_environment()
    return _hermetic_environment(
        rendered_evidence_dir=rendered_evidence_dir,
        chromium_executable=chromium_executable)


def _run_outcomes(kind, name, timeout, source_hash, git_head, *,
                  rendered_evidence_dir: Path | None = None,
                  chromium_executable: str | os.PathLike[str] | None = None):
    contract = _MANIFEST._OUTCOMES
    registry = COMMANDS if kind == "suite" else GATE_COMMANDS
    # The runtime-only output path is private; the source-bound template uses
    # a fixed placeholder and cannot disguise a free-form executed command.
    with tempfile.TemporaryDirectory(prefix="summon-release-collector-") as private:
        path = Path(private) / "outcomes.json"
        argv = [sys.executable, str(ROOT / contract.COLLECTOR), "--kind", kind,
                "--name", name, "--output", str(path)]
        result = subprocess.run(
            argv, cwd=str(ROOT), env=_child_environment(
                rendered_evidence_dir=rendered_evidence_dir,
                chromium_executable=chromium_executable), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False, **_MANIFEST.run_flags())
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0 or not path.is_file() or path.is_symlink():
            raise RuntimeError("structured test capture failed")
        if path.stat().st_size > contract.MAX_BYTES:
            raise RuntimeError("structured test capture exceeds limit")
        outcomes = contract.read_json(path)
        outcomes["output_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
        outcomes.pop("artifact_sha256", None)
        outcomes["artifact_sha256"] = contract.digest(outcomes)
        count = contract.validate_outcomes(
            outcomes, root=ROOT, kind=kind, name=name, command=registry[name],
            source_hash=source_hash, git_head=git_head, require_pass=False)
        return count, output, outcomes


def _invoke_outcomes(kind, name, timeout, source_hash, git_head, *,
                     rendered_evidence_dir: Path | None = None,
                     chromium_executable: str | os.PathLike[str] | None = None):
    """Call the outcome runner without widening legacy test seams."""
    options = {}
    if rendered_evidence_dir is not None:
        options["rendered_evidence_dir"] = rendered_evidence_dir
    if chromium_executable is not None:
        options["chromium_executable"] = chromium_executable
    return _run_outcomes(kind, name, timeout, source_hash, git_head, **options)


def run_suite(name: str, timeout: float, *, source_hash=None, git_head=None,
              rendered_evidence_dir: Path | None = None,
              chromium_executable: str | os.PathLike[str] | None = None):
    count, output, outcomes = _invoke_outcomes(
        "suite", name, timeout,
        source_hash if source_hash is not None else _source_hash(),
        git_head if git_head is not None else _git_head(),
        rendered_evidence_dir=rendered_evidence_dir,
        chromium_executable=chromium_executable)
    return count, hashlib.sha256(output.encode("utf-8")).hexdigest(), outcomes


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
              marker: dict[str, object] | None = None,
              outcomes: dict[str, object] | None = None) -> dict[str, object]:
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
    if outcomes is not None:
        payload["outcomes"] = outcomes
    if marker:
        # Keep only the fixed marker fields; no provider output or arbitrary
        # environment/path data enters a release artifact.
        # Never copy a producer's own outer-artifact hash into this record:
        # doing so would make the outer hash depend on a value that is not
        # retained before it is computed.  The live-provider producer's hash
        # is different: it is the digest of the reviewed receipt itself, so
        # retain it under an explicit field for release-manifest binding.
        # Never copy arbitrary provider/host diagnostics into a release
        # projection. Only source-defined codes and the single redacted
        # receipt label are public; raw detail remains in the private process
        # output digest.
        error_kind = marker.get("error_kind")
        if error_kind in _SAFE_ERROR_KINDS:
            payload["error_kind"] = error_kind
        elif isinstance(error_kind, str):
            payload["error_kind"] = _MANIFEST._OUTCOMES.GATE_REDACTED
        if marker.get("evidence_file") == _SAFE_EVIDENCE_FILE:
            payload["evidence_file"] = _SAFE_EVIDENCE_FILE
        if marker.get("detail") is not None:
            payload["detail"] = _MANIFEST._OUTCOMES.GATE_REDACTED
        if (name == "live_provider" and marker.get("status") == "pass"
                and isinstance(marker.get("artifact_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", marker["artifact_sha256"])):
            payload["evidence_sha256"] = marker["artifact_sha256"]
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
             artifact_dir: Path | None = None,
             rendered_evidence_dir: Path | None = None,
    chromium_executable: str | os.PathLike[str] | None = None
             ) -> tuple[str, dict[str, object]]:
    if name != "live_provider":
        count, output, outcomes = _invoke_outcomes(
            "gate", name, timeout, source_hash, git_head,
            rendered_evidence_dir=rendered_evidence_dir,
            chromium_executable=chromium_executable)
        marker = _marker(output, name)
        try:
            _MANIFEST._OUTCOMES.validate_outcomes(
                outcomes, root=ROOT, kind="gate", name=name,
                command=GATE_COMMANDS[name], source_hash=source_hash, git_head=git_head)
            status = "blocked" if marker is not None and marker.get("status") == "blocked" else "pass"
        except ValueError:
            status = "blocked"
        artifact = _artifact(name, status=status, command=GATE_COMMANDS[name],
            source_hash=source_hash, git_head=git_head, output=output,
            test_count=count, outcomes=outcomes, marker=marker)
        _write_artifact(artifact_dir, artifact)
        return status, artifact
    command = GATE_COMMANDS[name]
    env = _child_environment(
        rendered_evidence_dir=rendered_evidence_dir,
        chromium_executable=chromium_executable)
    result = subprocess.run(
        _gate_argv(name), cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
        **_MANIFEST.run_flags(),
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
                   artifact_dir: Path | None = None,
                   chromium_executable: str | os.PathLike[str] | None = None
                   ) -> dict[str, object]:
    before_hash = _source_hash()
    head = _git_head()
    status_before = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        cwd=str(ROOT), env=_MANIFEST._git_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=GIT_METADATA_TIMEOUT_SECONDS, check=True,
        **_MANIFEST.run_flags(),
    ).stdout
    if require_clean and status_before.strip():
        raise RuntimeError("--require-clean refuses a dirty source tree")
    # One owned directory is shared by all rendered checks in this run.  It is
    # retained only when the caller explicitly requested an external artifact
    # directory; otherwise it is deleted with the bounded run temporary.
    with tempfile.TemporaryDirectory(prefix="summon-release-rendered-") as private_rendered:
        rendered_evidence_dir = (
            (artifact_dir / "rendered") if artifact_dir is not None
            else Path(private_rendered)
        )
        _prepare_rendered_evidence_directory(rendered_evidence_dir)
        results = {
            name: run_suite(
                name, timeout, source_hash=before_hash, git_head=head,
                rendered_evidence_dir=rendered_evidence_dir,
                chromium_executable=chromium_executable)
            for name in COMMANDS
        }
        gate_results: dict[str, dict[str, object]] = {}
        gate_status: dict[str, str] = {}
        for name in sorted(GATE_COMMANDS):
            status, artifact = run_gate(
                name, timeout, source_hash=before_hash, git_head=head,
                artifact_dir=artifact_dir,
                rendered_evidence_dir=rendered_evidence_dir,
                chromium_executable=chromium_executable,
            )
            gate_status[name] = status
            gate_results[name] = artifact
        # Collect while the run-owned temporary directory is still alive.  A
        # caller-provided artifact directory remains available for manifest
        # validation and exact CI retention after this scope exits.
        rendered_evidence = _collect_rendered_evidence(
            rendered_evidence_dir, before_hash)
    after_hash = _source_hash()
    status_after = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        cwd=str(ROOT), env=_MANIFEST._git_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=GIT_METADATA_TIMEOUT_SECONDS, check=True,
        **_MANIFEST.run_flags(),
    ).stdout
    after_head = _git_head()
    if before_hash != after_hash or head != after_head or status_before != status_after:
        raise RuntimeError("source or Git state changed while release suites ran")
    tests = {name: value[0] for name, value in results.items()}
    platform_qualification = _platform_qualification(gate_status)
    return {
        "schema": 2,
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
        "platform_qualification": platform_qualification,
        "rendered_evidence": rendered_evidence,
        "commands": dict(COMMANDS),
        "gate_commands": dict(GATE_COMMANDS),
        "test_results": {
            name: {"count": count, "output_sha256": output_sha256, "outcomes": outcomes}
            for name, (count, output_sha256, outcomes) in results.items()
        },
        "runtime": {"python_version": platform.python_version(), "sys_platform": sys.platform,
                    "os_name": os.name},
        "captured_at_unix": int(time.time()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", help="write evidence JSON to this path")
    parser.add_argument("--timeout", type=float, default=RELEASE_SUITE_TIMEOUT_SECONDS)
    parser.add_argument("--require-clean", action="store_true",
                        help="refuse to run unless the Git worktree is clean")
    parser.add_argument(
        "--chromium-executable",
        help="explicit regular-file Chromium executable for rendered checks",
    )
    args = parser.parse_args(argv)
    try:
        artifact_dir = None
        output_path = None
        if args.output:
            output_path = _absolute_lexical(args.output)
            _assert_external_path(output_path, "release evidence output")
            artifact_dir = output_path.with_name(output_path.stem + ".gates")
        evidence = build_evidence(
            args.timeout,
            require_clean=args.require_clean,
            artifact_dir=artifact_dir,
            chromium_executable=args.chromium_executable,
        )
        encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if output_path is not None:
            if output_path.is_symlink():
                raise ValueError("refusing to replace a symlinked evidence output")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp",
                                             dir=str(output_path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, output_path)
                _fsync_parent(output_path)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        else:
            sys.stdout.write(encoded)
        return 0
    except subprocess.TimeoutExpired:
        print("release evidence: capture_timeout (timed out)", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Keep safe, machine-actionable path/contract refusals visible without
        # exposing arbitrary subprocess output.
        print(f"release evidence: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("release evidence: capture_or_publication_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
