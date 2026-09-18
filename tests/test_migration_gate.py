from __future__ import annotations

import json
import hashlib
import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_install import _dest, _fake_home, _run

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "summon" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _conversation  # noqa: E402
import _background  # noqa: E402
import _fleet  # noqa: E402
import _fleet_approval  # noqa: E402
import _jobs  # noqa: E402
import _job_control  # noqa: E402
import _telemetry  # noqa: E402
import _usage  # noqa: E402
import _usage_live  # noqa: E402
from _workspace_demo import ConductorDemo  # noqa: E402
from _workspace_layout import WorkspaceLayoutError  # noqa: E402


_DURABLE_EXACT = {
    "summon-usage-v1.json", "summon-usage-live-v1.json",
    "summon-telemetry.json", "summon-telemetry.jsonl",
}
_DURABLE_PREFIXES = (
    "summon/", "summon-usage-live-v1.json.",
    "summon-usage-live-checkpoints/",
)


def _tree_manifest(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            len(raw := path.read_bytes()), hashlib.sha256(raw).hexdigest())
        for path in sorted(root.rglob("*")) if path.is_file()
        and ((relative := path.relative_to(root).as_posix()) in _DURABLE_EXACT
             or relative.startswith(_DURABLE_PREFIXES))
    }


def _usage_wire() -> str:
    values = [
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}},
        {"jsonrpc": "2.0", "id": 2,
         "result": {"account": {"id": "migration-fixture"}}},
        {"jsonrpc": "2.0", "id": 3,
         "result": {"rateLimits": {"primary": {
             "usedPercent": 25, "resetsAt": 1787792400}}}},
    ]
    return "\n".join(json.dumps(item) for item in values) + "\n"


def _materialize_workspace_fixture(root: Path) -> dict[str, str]:
    """Create one durable queued/held workspace without provider contact."""
    workspace = ConductorDemo(root / "workspaces", run_id="migration-workspace")
    try:
        workspace.prepare()
        grant, _ = workspace.grant("main-1", "worker-a-1")
        workspace.launch("main-1", "worker-a-1", grant)
        entry = workspace.message("main-1", "worker-a-1", grant, 1, "[2,3]")
        turn = workspace.admit("main-1", "worker-a-1", grant, [entry])
        workspace.start(turn)
        state = workspace.state()
        delivery = copy.deepcopy(state["workspace"]["deliveries"][entry["delivery_id"]])
        uncertainty = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        source = workspace.source({"kind": "migration-collision-observation",
                                  "delivery_id": entry["delivery_id"],
                                  "attempt_id": turn["response"]["claim_id"],
                                  "qualification": "simulated"})
        observation = workspace.evidence(
            source, "main-1", "migration-collision", "observation",
            entry["delivery_id"], "held_for_recovery", "hold_observation")
        delivery.update(state="held_for_recovery", reason="native_turn_collision",
                        certainty=uncertainty)
        event = workspace.event("workspace_delivery_advanced",
            {"delivery": delivery, "evidence": {"hold_observation": observation},
             "supported_ack_levels": []}, "migration-collision")
        workspace.permit(event)
        workspace.runtime.record_transport_observation(
            event, kind="native_turn_collision", evidence=observation)
        return {"run_id": workspace.run_id, "delivery_id": entry["delivery_id"],
                "attempt_id": turn["response"]["claim_id"]}
    finally:
        workspace.cleanup()


def _materialize_durable_state(home: str) -> tuple[Path, set[str]]:
    agents_root = Path(home, ".agents")
    _fleet_approval._secure_private_root(str(agents_root))
    root = agents_root / "summon"
    root.mkdir(parents=True, exist_ok=True)
    project = root / "project"
    project.mkdir()

    jobs = root / "jobs"
    _jobs.write_prepared(
        str(jobs), "a" * 32, nonce="migration-fixture", agent="reviewer",
        prompt_sha256="b" * 64, cwd=str(project), flags={"permission": "read-only"},
        summon={"version": "3.2.1", "scripts_sha256": "c" * 64})
    # A live owned record is retained for the migration rehearsal. The current
    # pytest process is the fixture owner; no child/provider is started.
    _jobs.update_spawned(str(jobs), "a" * 32, os.getpid())
    _job_control.queue_command(
        str(jobs), "a" * 32, "steer", message="Preserve the migration contract.")

    rooms = root / "rooms"
    room = _conversation.ConversationJournal.create(
        rooms, session_id="migration-room", project_id="summon",
        project_root=project, initiator_host="test", initiator_agent="reviewer",
        participants=[{"agent": "reviewer", "role": "reviewer",
                       "name": "Reviewer", "version": "fixture"}])
    room.append_human_message("Preserve this migration decision.")
    _materialize_workspace_fixture(root)

    env = {
        "SUMMON_FLEET_APPROVAL_STORE": str(root / "fleet" / "store.json"),
        "SUMMON_FLEET_APPROVAL_KEY": str(root / "fleet" / "store.key"),
        "SUMMON_USAGE_CACHE": str(agents_root / "summon-usage-v1.json"),
        "SUMMON_USAGE_LIVE_STORE": str(agents_root / "summon-usage-live-v1.json"),
        "SUMMON_USAGE_LIVE_CHECKPOINT_ROOT": str(
            agents_root / "summon-usage-live-checkpoints"),
        "SUMMON_TELEMETRY_CONFIG": str(agents_root / "summon-telemetry.json"),
        "SUMMON_TELEMETRY_FILE": str(agents_root / "summon-telemetry.jsonl"),
        # The release runner disables ambient telemetry. This fixture tests
        # durable opt-in state, so make its consent explicit and local.
        "SUMMON_TELEMETRY": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        agents = [{
            "name": "reviewer", "run_agent": "fixture-provider",
            "permission": "read-only", "model": "fixture-model",
            "source": "project", "lifecycle": "active",
        }]
        _report, fleet, plan = _fleet.proposal(
            lane="review", seats=["reviewer"], agents=agents,
            cwd=str(project), permission_ceiling="read-only",
            data_boundary="local_sanitized",
            corrective={"contract_repair": False, "retry": False,
                        "fallback": False, "continuation": False},
            spend={"subscription": True, "credit": False, "payg": False,
                   "max_provider_contacts": 1, "max_billable_attempts": 1,
                   "max_parallel": 1})
        _fleet_approval.approve(
            fleet=fleet, plan=plan, lane_name="review",
            expires_in_seconds=3600, expected_generation=0)

        snapshot = root / "usage-operator-export.json"
        _usage.write_synthetic_snapshot(str(snapshot))
        _usage.import_snapshot(str(snapshot), cache_path=env["SUMMON_USAGE_CACHE"])
        live = _usage_live.refresh_codex(
            allow_account_usage_read=True,
            runner=lambda _plan: {
                "stdout": _usage_wire(), "stderr": "", "exit_code": 0,
                "elapsed_ms": 1, "provider_contacted": False,
                "cli_version": _usage_live.CODEX_SCHEMA_CLI_VERSION,
            },
            store_file=env["SUMMON_USAGE_LIVE_STORE"],
            now="2026-08-27T00:00:00Z")
        if live["status"] != "success":
            raise AssertionError(live)

        _telemetry.set_enabled(True)
        if _telemetry.record({"status": "success", "provider_contacted": False}) is None:
            raise AssertionError("telemetry fixture was not recorded")

    required = {
        "summon/jobs", "summon/rooms", "summon/fleet",
        "summon/workspaces",
        "summon-usage-v1.json", "summon-usage-live-v1.json",
        "summon-usage-live-checkpoints", "summon-telemetry.json",
        "summon-telemetry.jsonl",
    }
    present_paths = [path.relative_to(agents_root).as_posix()
                     for path in agents_root.rglob("*") if path.is_file()]
    if not all(any(path == name or path.startswith(name + "/")
                   or path.startswith(name + ".") for path in present_paths)
               for name in required):
        raise AssertionError((required, present_paths))
    return agents_root, required


def _assert_durable_state_readable(home: str, *, expect_active: bool = True) -> None:
    agents_root = Path(home, ".agents")
    root = agents_root / "summon"
    env = {
        "SUMMON_FLEET_APPROVAL_STORE": str(root / "fleet" / "store.json"),
        "SUMMON_FLEET_APPROVAL_KEY": str(root / "fleet" / "store.key"),
        "SUMMON_USAGE_CACHE": str(agents_root / "summon-usage-v1.json"),
        "SUMMON_USAGE_LIVE_STORE": str(agents_root / "summon-usage-live-v1.json"),
        "SUMMON_USAGE_LIVE_CHECKPOINT_ROOT": str(
            agents_root / "summon-usage-live-checkpoints"),
        "SUMMON_TELEMETRY_CONFIG": str(agents_root / "summon-telemetry.json"),
        "SUMMON_TELEMETRY_FILE": str(agents_root / "summon-telemetry.jsonl"),
        "SUMMON_TELEMETRY": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        job = _jobs.job_status(str(root / "jobs"), "a" * 32)
        if not isinstance(job, dict) or job.get("job_id") != "a" * 32:
            raise AssertionError(job)
        if expect_active:
            if job.get("state") not in {"running", "spawned"}:
                raise AssertionError({"active_owned_job": job.get("state")})
        elif job.get("state") != "success":
            raise AssertionError({"settled_owned_job": job.get("state")})
        control = _job_control.control_summary(str(root / "jobs"), "a" * 32)
        if control.get("integrity") != "authenticated" or control["counts"]["steer"] != 1:
            raise AssertionError(control)
        room = _conversation.ConversationJournal.open(
            root / "rooms", "migration-room").as_dict(native=True)
        if room["room"]["session_id"] != "migration-room":
            raise AssertionError(room)
        workspace = ConductorDemo(root / "workspaces", run_id="migration-workspace")
        workspace.reopen()
        held = next(item for item in workspace.state()["workspace"]["deliveries"].values()
                    if item["state"] == "held_for_recovery")
        if held["reason"] != "native_turn_collision" or held["certainty"] != {
                "contact": "unknown", "spend": "unknown", "cleanup": "unknown"}:
            raise AssertionError(held)
        if workspace.runtime._worker_ingress:
            raise AssertionError("migration reader reconstructed a worker ingress")
        if _fleet_approval.status().get("initialized") is not True:
            raise AssertionError("fleet approval store is unreadable")
        if _usage.status(
                cache_path=env["SUMMON_USAGE_CACHE"]).get("status") != "success":
            raise AssertionError("usage cache is unreadable")
        if _usage_live.status(
                store_file=env["SUMMON_USAGE_LIVE_STORE"],
                now="2026-08-27T00:00:00Z").get("status") != "success":
            raise AssertionError("usage live store is unreadable")
        telemetry = _telemetry.status()
        if telemetry.get("enabled") is not True or telemetry.get("event_count", 0) < 1:
            raise AssertionError(telemetry)


def _workspace_manifest(root: Path) -> dict[str, tuple[int, str]]:
    """Hash the bounded provider-free workspace migration fixture."""
    return {
        path.relative_to(root).as_posix(): (
            len(raw := path.read_bytes()), hashlib.sha256(raw).hexdigest())
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class MigrationRollbackGateTests(unittest.TestCase):
    def test_workspace_lease_and_incompatible_reader_preserve_state(self):
        """Active execution leases refuse replacement; readers fail before mutation."""
        with tempfile.TemporaryDirectory(prefix="summon-workspace-migration-") as temporary:
            home = _fake_home()
            try:
                first = _run(home, "--hosts", "claude", "--no-agents")
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                durable, _namespaces = _materialize_durable_state(home)
                before = _tree_manifest(durable)
                _assert_durable_state_readable(home)

                # The execution lease is the product's actual install boundary.
                # It represents an owned immutable bundle still in use; refresh
                # and uninstall must refuse before touching either tree.
                lease_pair = _background._acquire_execution_lease(
                    str(Path(_dest(home), "scripts", "summon.cmd")))
                self.assertIsNotNone(lease_pair)
                lease, lease_token = lease_pair
                lease_path = Path(lease)
                dest = Path(_dest(home))
                dest_before = _tree_manifest(dest)
                refreshed = _run(home, "--hosts", "claude")
                self.assertNotEqual(refreshed.returncode, 0)
                removed = _run(home, "--hosts", "claude", "--uninstall")
                self.assertNotEqual(removed.returncode, 0)
                self.assertEqual(_tree_manifest(durable), before)
                self.assertEqual(_tree_manifest(dest), dest_before)
                self.assertTrue(lease_path.is_file())

                # Quiesce/settle the provider-free fixture through the durable
                # job result path, then remove only the fixture-owned lease.
                result = {
                    "status": "success", "job_nonce": "migration-fixture",
                    "prompt_sha256": "b" * 64,
                    "summon": {"version": "3.2.1", "scripts_sha256": "c" * 64},
                }
                _jobs._atomic_write_json(
                    _jobs.result_path(str(Path(home, ".agents", "summon", "jobs")),
                                      "a" * 32), result)
                self.assertEqual(_jobs.job_status(
                    str(Path(home, ".agents", "summon", "jobs")), "a" * 32)["state"],
                    "success")
                settled_before = _tree_manifest(durable)
                _background._release_execution_lease(lease, lease_token)
                self.assertFalse(lease_path.exists())

                refreshed = _run(home, "--hosts", "claude")
                self.assertEqual(refreshed.returncode, 0,
                                 refreshed.stdout + refreshed.stderr)
                _assert_durable_state_readable(home, expect_active=False)
                self.assertEqual(_tree_manifest(durable), settled_before)

                # A copied run with an incompatible layout must refuse before
                # mutating any byte in the complete run tree.
                workspace_root = durable / "summon" / "workspaces"
                incompatible = Path(temporary) / "incompatible-workspaces"
                shutil.copytree(workspace_root, incompatible)
                manifest = incompatible / "migration-workspace" / "workspace-layout.json"
                manifest.write_bytes(manifest.read_bytes().replace(b"/v1", b"/v0"))
                incompatible_before = _workspace_manifest(incompatible)
                with self.assertRaises(WorkspaceLayoutError):
                    ConductorDemo(incompatible, run_id="migration-workspace").reopen()
                self.assertEqual(_workspace_manifest(incompatible), incompatible_before)
            finally:
                shutil.rmtree(home, ignore_errors=True)

    def test_phase1_durable_state_survives_refresh_and_uninstall(self):
        home = _fake_home()
        try:
            first = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            durable, namespaces = _materialize_durable_state(home)
            _assert_durable_state_readable(home)
            before = _tree_manifest(durable)
            self.assertGreaterEqual(len(before), 10)
            self.assertTrue(all(any(
                path == name or path.startswith(name + "/")
                or path.startswith(name + ".") for path in before)
                for name in namespaces), (namespaces, before))
            refreshed = _run(home, "--hosts", "claude")
            self.assertEqual(refreshed.returncode, 0,
                             refreshed.stdout + refreshed.stderr)
            _assert_durable_state_readable(home)
            self.assertEqual(_tree_manifest(durable), before)
            removed = _run(home, "--hosts", "claude", "--uninstall")
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            _assert_durable_state_readable(home)
            self.assertEqual(_tree_manifest(durable), before)
            self.assertFalse(Path(_dest(home)).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_isolated_upgrade_keeps_owned_manifest_and_companions(self):
        home = _fake_home()
        try:
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads(Path(_dest(home), ".summon-install.json").read_text())
            self.assertEqual(manifest.get("installed_by"), "summon")
            self.assertTrue(Path(home, ".claude", "skills", "council", "SKILL.md").is_file())
            self.assertTrue(Path(home, ".claude", "skills", "deliberate", "SKILL.md").is_file())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_interrupted_swap_restores_previous_owned_tree(self):
        home = _fake_home()
        try:
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dest = Path(_dest(home))
            previous = Path(str(dest) + ".previous")
            os.replace(dest, previous)
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertFalse(previous.exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_foreign_tree_blocks_upgrade_without_mutation(self):
        home = _fake_home()
        try:
            dest = Path(_dest(home))
            dest.mkdir(parents=True)
            marker = dest / "user-owned.txt"
            marker.write_text("keep", encoding="utf-8")
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
