"""L07 owned Windows ACL/link and in-process export journeys.

No alternate-principal login, network server, provider or real user state.
ACLs are permission policy, not encryption or protection from administrators
or the trusted OS user. In-process routing does not qualify HTTP transport.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import _fleet_approval as privacy
import _workspace_content as content
import _workspace_entry as entry
import _workspace_ui as ui
from _spawn import run_flags


def require(value, category):
    if not value:
        pytest.fail(category, pytrace=False)


def owned(path, root):
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    require(path != root and path.is_relative_to(root), "fixture_path_outside_owned_root")
    require(Path(os.path.realpath(path)).is_relative_to(root), "fixture_target_outside_owned_root")
    return path


@pytest.fixture
def windows(tmp_path):
    if os.name != "nt":
        pytest.skip("L07 Windows ACL/junction facilities unavailable on this OS")
    if not shutil.which("icacls") or not shutil.which("cmd"):
        pytest.skip("L07 required Windows ACL/junction tool unavailable")
    return tmp_path


def grant_read(path, root, *, inheritable=False):
    path = owned(path, root)
    result = subprocess.run(["icacls", str(path), "/grant",
        "*S-1-1-0:" + ("(OI)(CI)" if inheritable else "") + "R"],
        capture_output=True, timeout=15, **run_flags())
    require(result.returncode == 0, "synthetic_foreign_ace_setup_failed")
    snapshot = privacy._windows_acl_snapshot(str(path))
    rules = snapshot["rules"]
    if isinstance(rules, dict):
        rules = [rules]
    require(any(rule["sid"] == "S-1-1-0" for rule in rules), "synthetic_foreign_ace_not_observed")


def private(path, root, *, directory=False):
    path = owned(path, root)
    try:
        privacy._verify_private(str(path), directory=directory)
    except ValueError:
        pytest.fail("owner_scoped_protection_not_verified", pytrace=False)


def refused(kind, function, *args):
    with pytest.raises((entry.WorkspaceEntryError, content.ContentError)) as caught:
        function(*args)
    require(caught.value.kind == kind, "unexpected_safe_refusal_category")


def test_l07_inherited_parent_secures_empty_blob_and_host_token(windows, monkeypatch):
    parent = owned(windows / "inherited-parent", windows)
    parent.mkdir()
    sentinel = parent / "unrelated"
    sentinel.write_bytes(b"unchanged")
    grant_read(parent, windows, inheritable=True)
    parent_acl = privacy._windows_acl_snapshot(str(parent))
    root = parent / "content"
    privacy._secure_private_root(str(root))
    private(root, windows, directory=True)
    store = content.ContentStore(root)
    ref = content.prepare_content(b"synthetic-private-body")
    secure = content._secure_file
    observed = []
    def secure_empty(path):
        owned(path, windows)
        require(Path(path).stat().st_size == 0, "blob_hardened_after_private_write")
        secure(path)
        private(path, windows)
        observed.append(Path(path).name)
    monkeypatch.setattr(content, "_secure_file", secure_empty)
    store.put(ref, b"synthetic-private-body")
    require(ref.reference in observed, "new_blob_not_hardened_empty")
    token_path = parent / "host-token"
    original_write = os.write
    writes = []
    def protected_write(fd, raw):
        require(os.fstat(fd).st_size == 0, "host_private_write_not_initial")
        private(token_path, windows)
        writes.append(True)
        return original_write(fd, raw)
    with patch.object(entry.os, "write", protected_write):
        entry._write_private_token(str(token_path), "synthetic-control-token")
    require(bool(writes), "host_private_write_not_observed")
    private(token_path, windows)
    require(entry._read_private_token(str(token_path)) == "synthetic-control-token", "host_reopen_failed")
    require(content.ContentStore(root).read(ref) == b"synthetic-private-body", "content_reopen_failed")
    require(sentinel.read_bytes() == b"unchanged", "unrelated_sentinel_changed")
    require(privacy._windows_acl_snapshot(str(parent)) == parent_acl, "parent_acl_changed")


@pytest.mark.parametrize("failure", ["secure", "verify"], ids=['p001_case_001', 'p001_case_002'])
def test_l07_host_protection_failure_leaves_no_private_bytes(windows, failure):
    path = owned(windows / "token", windows)
    name = "_secure_file" if failure == "secure" else "_verify_private"
    with patch.object(entry, name, side_effect=ValueError("synthetic_protection_unavailable")), \
            patch.object(entry.os, "write", side_effect=AssertionError("private_write_before_protection")):
        refused("command_refresh_token_unavailable", entry._write_private_token, str(path), "synthetic-token")
    require(path.read_bytes() == b"", "failed_protection_left_private_bytes")
    refused("command_refresh_token_exists", entry._write_private_token, str(path), "another-token")


@pytest.mark.parametrize("target", ["root", "blob", "host"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_l07_reopen_refuses_added_read_ace_without_repair_or_private_read(windows, target):
    root = windows / "content"
    privacy._secure_private_root(str(root))
    store = content.ContentStore(root)
    ref = content.prepare_content(b"synthetic-body")
    store.put(ref, b"synthetic-body")
    token = windows / "host-token"
    entry._write_private_token(str(token), "synthetic-token")
    changed = {"root": root, "blob": root / ref.reference, "host": token}[target]
    grant_read(changed, windows)
    acl = privacy._windows_acl_snapshot(str(changed))
    with patch.object(content.ContentStore, "_read_locked", side_effect=AssertionError("private_content_read")), \
            patch.object(entry.os, "read", side_effect=AssertionError("private_token_read")):
        if target == "host":
            refused("command_refresh_token_unavailable", entry._read_private_token, str(token))
            refused("command_refresh_token_exists", entry._write_private_token, str(token), "replacement")
        else:
            refused("private_root_invalid" if target == "root" else "unsafe_entry", store.read, ref)
            refused("private_root_invalid" if target == "root" else "unsafe_entry", store.put, ref, b"synthetic-body")
    require(privacy._windows_acl_snapshot(str(changed)) == acl, "reopen_repaired_acl")
    require((root / ref.reference).read_bytes() == b"synthetic-body", "reopen_mutated_content")
    require(token.read_bytes() == b"synthetic-token", "reopen_mutated_token")


def test_l07_token_reopen_refuses_actual_junction_before_private_read(windows):
    target = owned(windows / "reopen-target", windows)
    privacy._secure_private_root(str(target))
    token = owned(target / "host-token", windows)
    entry._write_private_token(str(token), "synthetic-token")
    require(entry._read_private_token(str(token)) == "synthetic-token", "direct_reopen_control_failed")
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"unchanged")
    token_acl = privacy._windows_acl_snapshot(str(token))
    link = owned(windows / "reopen-junction", windows)
    result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, timeout=15, **run_flags())
    require(result.returncode == 0, "owned_junction_setup_failed")
    try:
        require(bool(os.lstat(link).st_file_attributes & 0x400), "actual_reparse_attribute_missing")
        through = owned(link / "host-token", windows)
        with patch.object(entry.os, "read", side_effect=AssertionError("private_token_read_before_reparse_refusal")):
            refused("command_refresh_token_unavailable", entry._read_private_token, str(through))
        require(token.read_bytes() == b"synthetic-token", "junction_reopen_changed_token")
        require(sentinel.read_bytes() == b"unchanged", "junction_reopen_changed_sentinel")
        require(privacy._windows_acl_snapshot(str(token)) == token_acl, "junction_reopen_changed_acl")
    finally:
        owned(link, windows)
        os.rmdir(link)
    require(entry._read_private_token(str(token)) == "synthetic-token", "direct_reopen_after_refusal_failed")
    require(sentinel.read_bytes() == b"unchanged", "junction_cleanup_changed_sentinel")


def test_l07_token_reopen_unverifiable_ancestor_refuses_before_read(windows):
    token = owned(windows / "host-token", windows)
    entry._write_private_token(str(token), "synthetic-token")
    with patch.object(entry, "_reject_reparse_ancestors", side_effect=ValueError("inspection_unavailable")), \
            patch.object(entry.os, "read", side_effect=AssertionError("private_token_read_before_inspection")):
        refused("command_refresh_token_unavailable", entry._read_private_token, str(token))
    require(entry._read_private_token(str(token)) == "synthetic-token", "reopen_after_inspection_failure_failed")


def test_l07_policy_replace_secures_empty_temporary_before_private_write(windows, monkeypatch):
    root = owned(windows / "policy-root", windows)
    root.mkdir()
    privacy._secure_private_root(str(root))
    policy = owned(root / "policy.json", windows)
    policy.write_bytes(b"{\"generation\":1}")
    entry._secure_file(str(policy))
    observations = []
    secure = entry._secure_file

    def observe(path):
        candidate = Path(path)
        observations.append((candidate.name, candidate.stat().st_size))
        secure(path)

    monkeypatch.setattr(entry, "_secure_file", observe)
    previous = entry._replace_private_bytes(str(policy), b"{\"generation\":2}")
    require(previous == b"{\"generation\":1}", "policy_previous_bytes_missing")
    temporary_sizes = [size for name, size in observations if name.startswith(".summon-policy-")]
    require(temporary_sizes and temporary_sizes[0] == 0, "policy_temporary_not_hardened_empty")
    require(all(size == 0 for size in temporary_sizes), "policy_private_bytes_before_hardening")
    private(policy, windows)
    require(policy.read_bytes() == b"{\"generation\":2}", "policy_replace_failed")


def test_l07_policy_replace_failure_closes_descriptor_and_removes_temporary(windows, monkeypatch):
    root = owned(windows / "policy-failure-root", windows)
    root.mkdir()
    privacy._secure_private_root(str(root))
    policy = owned(root / "policy.json", windows)
    policy.write_bytes(b"{\"generation\":1}")
    entry._secure_file(str(policy))
    created = []
    real_mkstemp = entry.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(entry.tempfile, "mkstemp", capture_mkstemp)
    temporary_paths = []

    def fail_hardening(path):
        temporary_paths.append(path)
        raise ValueError("synthetic_protection_unavailable")

    monkeypatch.setattr(entry, "_secure_file", fail_hardening)
    with pytest.raises(entry.WorkspaceEntryError) as caught:
        entry._replace_private_bytes(str(policy), b"{\"generation\":2}")
    require(caught.value.kind == "command_policy_unavailable", "policy_failure_category_changed")
    require(created and temporary_paths, "policy_failure_temp_not_created")
    descriptor, temporary = created[0]
    with pytest.raises(OSError):
        os.fstat(descriptor)
    require(not Path(temporary).exists(), "policy_failure_temporary_survived")
    require(not Path(temporary_paths[0]).exists(), "policy_failure_private_bytes_survived")
    require(policy.read_bytes() == b"{\"generation\":1}", "policy_failure_changed_old_policy")


def test_l07_actual_junction_refuses_content_root_and_host_create(windows):
    target = owned(windows / "junction-target", windows)
    privacy._secure_private_root(str(target))
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"unchanged")
    link = owned(windows / "junction", windows)
    result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, timeout=15, **run_flags())
    require(result.returncode == 0, "owned_junction_setup_failed")
    try:
        require(bool(os.lstat(link).st_file_attributes & 0x400), "actual_reparse_attribute_missing")
        refused("private_root_invalid", content.ContentStore, link)
        refused("command_refresh_token_unavailable", entry._write_private_token,
                str(link / "host-token"), "synthetic-token")
        require(not (target / "host-token").exists(), "junction_target_mutated")
        require(sentinel.read_bytes() == b"unchanged", "junction_sentinel_changed")
    finally:
        owned(link, windows)
        os.rmdir(link)  # Remove only the verified owned junction, never its target.
    require(sentinel.read_bytes() == b"unchanged", "junction_cleanup_changed_target")


def test_l07_actual_hardlinks_refused_at_content_and_host_boundaries(windows):
    root = windows / "content"
    privacy._secure_private_root(str(root))
    store = content.ContentStore(root)
    ref = content.prepare_content(b"synthetic-body")
    store.put(ref, b"synthetic-body")
    original = owned(root / ref.reference, windows)
    alias = owned(windows / "blob-alias", windows)
    os.link(original, alias)
    require(os.stat(original).st_nlink == 2, "actual_hardlink_not_observed")
    refused("unsafe_entry", store.read, ref)
    refused("unsafe_entry", store.put, ref, b"synthetic-body")
    token = owned(windows / "host-token", windows)
    entry._write_private_token(str(token), "synthetic-token")
    os.link(token, owned(windows / "token-alias", windows))
    refused("command_refresh_token_unavailable", entry._read_private_token, str(token))
    refused("command_refresh_token_exists", entry._write_private_token, str(token), "replacement")
    require(alias.read_bytes() == b"synthetic-body", "hardlink_content_changed")
    require(token.read_bytes() == b"synthetic-token", "hardlink_token_changed")


def test_l07_unverifiable_open_path_fails_closed(windows):
    root = windows / "content"
    privacy._secure_private_root(str(root))
    store = content.ContentStore(root)
    ref = content.prepare_content(b"synthetic-body")
    store.put(ref, b"synthetic-body")
    with patch.object(content, "_final_open_path", return_value=None):
        refused("opened_file_changed", store.read, ref)
    require(store.read(ref) == b"synthetic-body", "unverifiable_path_changed_content")


def test_l07_actual_export_route_and_projector_are_metadata_only(tmp_path):
    from _workspace_runtime import WorkspaceRuntime
    from test_workspace_runtime import plan
    prepared = plan()
    body = b"hello"  # Deliberately low entropy: its digest is also private.
    descriptor = content.prepare_content(body)
    evidence = {"reference": {"id": descriptor.reference, "sha256": descriptor.sha256},
                "category": "artifact", "task_id": "main-2"}
    def authorize(event, state):
        return ((event == {"operation": "prepare", "plan": prepared} and state is None)
                or event.get("event") in {"workspace_feature", "workspace_goal_defined", "workspace_lane_defined"}
                or event.get("event") == "workspace_evidence_registered" and event.get("payload") == evidence)
    def resolve_evidence(payload):
        require(payload == evidence, "unexpected_evidence_read")
        return runtime._content_store().read(descriptor)
    runtime = WorkspaceRuntime.new(tmp_path / "runs", "run", "workspace", authorize=authorize,
        resolve_evidence=resolve_evidence, clock=lambda: 2000.0)
    runtime.prepare(prepared, project_root_sha256="a" * 64, roster_definition_sha256="b" * 64)
    runtime._content_store().put(descriptor, body)
    current, _ = runtime._coordinator._load()
    runtime.record_event({"event": "workspace_evidence_registered", "protocol": prepared["events"][0]["protocol"],
        "workspace_id": "workspace", "run_id": "run", "operation_key": "l07-artifact",
        "expected_revision": current["workspace"]["revision"], "payload": evidence})
    surface = ui.WorkspaceSurface(runtime._coordinator, "workspace")
    # Supply lifecycle state in process: no socket, HTTP server or worker starts.
    surface._server = SimpleNamespace(server_port=12345)
    surface._key = b"s" * 32
    token = surface._exchange(surface.issue_bootstrap())
    files = lambda: {str(p.relative_to(tmp_path)): p.read_bytes()
                     for p in tmp_path.rglob("*") if p.is_file()}
    before = files()
    def get(path, supplied=token):
        handler = object.__new__(ui._Handler)
        handler.server = SimpleNamespace(server_port=12345, surface=surface)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:12345"
        handler.headers["Authorization"] = "Bearer " + supplied
        output = []
        handler._send = lambda status, body, **_: output.append((status, body))
        handler.do_GET()
        return output[0]
    status, exported = get("/api/export")
    require(status == 200, "metadata_export_failed")
    wire = json.dumps(exported)
    forbidden = [str(tmp_path), token, "a" * 64, "b" * 64, body.decode(),
                 descriptor.reference, hashlib.sha256(body).hexdigest(),
                 prepared["events"][1]["payload"]["goal"]["objective"]]
    if os.name == "nt":
        forbidden.append(privacy._effective_user_sid())
    require(not any(value and value in wire for value in forbidden), "metadata_export_exposed_private_material")
    require(exported["evidence"]["bodies_available"] is False, "export_exposed_evidence_bodies")
    require(exported["evidence"]["count"] == 1, "permitted_evidence_metadata_missing")
    require(exported["goal"]["text_available"] is False, "export_exposed_goal_body")
    require(get("/api/view")[0] == 200, "scoped_view_success_control_failed")
    require(get("/api/export", "forged")[0] == 401, "export_accepted_forged_bearer")
    require(get("/api/export?body=true")[0] == 400, "read_bearer_widened_export_scope")
    require(get("/api/export/body")[0] == 404, "unsupported_body_export_available")
    require(files() == before, "export_or_refusal_mutated_journal")


def test_l07_toolchain_categories(record_property):
    record_property("os", platform.system())
    record_property("os_release", platform.release())
    record_property("os_version", platform.version())
    record_property("python", platform.python_version())
    record_property("icacls_available", bool(shutil.which("icacls")))
    record_property("cmd_available", bool(shutil.which("cmd")))
