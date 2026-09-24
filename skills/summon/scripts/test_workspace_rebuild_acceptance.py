"""Provider-inert workspace reconstruction over preserved journal history."""

from pathlib import Path

from _swarm_coordinator import rebuild_projection_from_journal
from _workspace_runtime import WorkspaceRuntime


def test_workspace_reconstruction_preserves_history_identity_and_uncertainty(monkeypatch):
    # Import lazily: the fixture's unittest class must not be collected twice.
    from test_workspace_runtime import WorkspaceRuntimeTests, plan

    fixture = WorkspaceRuntimeTests()
    fixture.setUp()
    try:
        fixture.authorize.return_value = True
        prepared = plan()
        fixture.runtime.prepare(
            prepared, project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64,
        )
        coordinator = fixture.runtime._coordinator
        coordinator.register_worker("declared-worker", worker_instance_id="declared-instance")
        task = prepared["tasks"][0]
        coordinator.claim(
            "declared-worker", task["task_id"],
            request_sha256=task["request_sha256"], lease_ms=1000,
        )
        directory = Path(coordinator.run_dir)
        newest = max(directory.glob("journal-g*.jsonl"),
                     key=lambda item: int(item.stem.removeprefix("journal-g")))
        with newest.open("ab") as stream:
            stream.write(b'{"synthetic_torn_tail":')
        coordinator.acknowledge_indeterminate(
            task["task_id"], allow_retry=False, reason="synthetic recovery review",
            human_confirmed=True,
        )
        expected, torn = coordinator._load()
        assert not torn
        assert expected["tasks"][task["task_id"]]["uncertain_spend"] is True

        # These opaque historical sidecars test nonmutation only. They are not
        # represented as authenticated approvals or certified provider receipts.
        (directory / "historical-approval.json").write_bytes(b'{"opaque":"old approval bytes"}')
        (directory / "historical-receipt.json").write_bytes(b'{"model_identity":"unknown"}')
        before = fixture.snapshot()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("read-only reconstruction attempted a mutation or launch")

        monkeypatch.setattr(type(coordinator), "_acquire", forbidden)
        monkeypatch.setattr("subprocess.Popen", forbidden)
        reopened = WorkspaceRuntime(
            fixture.runs, "run", "workspace", authorize=forbidden,
            resolve_evidence=forbidden, clock=lambda: 1000.0,
        )
        first = reopened.inspect()
        rebuilt = rebuild_projection_from_journal(directory)
        second = reopened.inspect()

        assert first == second == fixture.runtime.inspect()
        assert rebuilt["projection"]["workspace"] == expected["workspace"]
        assert rebuilt["projection"]["workers"] == expected["workers"]
        assert rebuilt["projection"]["tasks"] == expected["tasks"]
        assert rebuilt["projection"]["claims"] == expected["claims"]
        assert rebuilt["source"]["workspace_revision"] == expected["workspace"]["revision"]
        assert len(rebuilt["source"]["segments"]) >= 2
        assert rebuilt["launch_authority"] is False
        assert rebuilt["compatibility"]["launch_authority"] is False
        assert fixture.snapshot() == before
    finally:
        fixture.doCleanups()
