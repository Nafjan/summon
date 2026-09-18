"""Provider-free browser journey for retained linked replacements.

This is a real local Chromium check against the loopback surface.  It uses a
synthetic disposable workspace only: no provider, credential, or external
network is contacted.
"""
from __future__ import annotations

import json
import copy
import os
import platform
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _workspace_commands import OperatorLinkedReplacementAdapter
from _workspace_ui import WorkspaceSurface
from _workspace_entry import WorkspaceHost
import _workspace_admission as admission
from test_workspace_entry import _free_port
from test_workspace_ui import login, request
from test_workspace_link_runtime import TestLinkedRuntime as _LinkedFixture


def test_real_browser_retains_same_key_through_expiry_and_lookup():
    playwright = pytest.importorskip("playwright.sync_api")
    fixture = _LinkedFixture()
    fixture.setup_method()
    surface = None
    browser = None
    context = None
    try:
        handle, proposal = fixture._install(fixture._authority())
        adapter = OperatorLinkedReplacementAdapter(fixture.demo.runtime, handle)
        surface = WorkspaceSurface(
            fixture.demo.runtime._coordinator,
            fixture.demo.workspace_id,
            operator_linked_replacements=adapter,
            presentation_key=b"b" * 32,
        )
        surface.start()
        snapshot = surface.snapshot()
        cap = snapshot["operator_linked_replacements"]
        opaque_workspace = snapshot["workspace_id"]
        parent = cap["targets"][0]["parent_target"]
        recipient = cap["targets"][0]["recipient_target"]

        before = copy.deepcopy(fixture.demo.state()["workspace"])
        parent_id = fixture.entry["delivery_id"]
        parent_before = copy.deepcopy(before["deliveries"][parent_id])
        evidence_dir = os.environ.get("SUMMON_F17_BROWSER_EVIDENCE_DIR")
        if evidence_dir:
            evidence_root = Path(evidence_dir)
            evidence_root.mkdir(parents=True, exist_ok=True)
        else:
            evidence_root = None
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.set_default_timeout(5000)
            submissions = []
            external_requests = []

            def block_external(route):
                host = urlsplit(route.request.url).hostname
                if host != "127.0.0.1":
                    external_requests.append(host or "missing-host")
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            def drop_link_response(route):
                request = route.request
                if (request.method == "POST"
                        and request.url.endswith("/api/linked-replacements")):
                    body = json.loads(request.post_data or "{}")
                    fixture.demo.permit({
                        "operation": "execute_operator_linked_replacement",
                        "proposal": {
                            "schema": "summon.workspace.linked-replacement-proposal/v1",
                            "action": "propose_linked_replacement",
                            "operation_key": body["operation_key"],
                            "parent_target": "opaque-parent",
                            "recipient_target": "opaque-recipient",
                        },
                    })
                    submissions.append(body["operation_key"])
                    route.fetch()
                    route.abort("failed")
                    return
                route.continue_()

            page.route("**/*", block_external)
            page.route("**/api/linked-replacements", drop_link_response)
            page.goto(surface.url, wait_until="domcontentloaded")
            page.locator("#code").fill(surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app").wait_for(state="visible")
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-panel").wait_for(state="visible")
            assert page.title() == "Summon workspace"
            first_screenshot = page.screenshot()
            assert len(first_screenshot) > 1000
            assert page.get_by_role("button", name="Link replacement").is_visible()

            # The request is admitted and appended, but its response is dropped
            # after the owner has durably recorded it. The browser must retain
            # exactly this operation key for reconciliation.
            page.get_by_role("button", name="Link replacement").click()
            page.locator("#linked-lookup").wait_for(state="visible")
            assert len(submissions) == 1
            operation_key = submissions[0]
            page.locator("#linked-status").wait_for()
            assert "did not confirm" in page.locator("#linked-status").inner_text().lower()
            after_append = fixture.demo.state()["workspace"]
            assert len(after_append["deliveries"]) == len(before["deliveries"]) + 1
            assert after_append["deliveries"][parent_id] == parent_before
            children = [item for key, item in after_append["deliveries"].items() if key != parent_id]
            assert len(children) == 1
            assert children[0]["parent_delivery_id"] == parent_id
            assert children[0]["inherited_uncertainty"] == ["cleanup", "contact", "spend"]
            assert page.evaluate(
                "key => sessionStorage.getItem(key)",
                "summon.workspace.pending-linked-replacement/v1",
            ) is not None
            if evidence_root:
                (evidence_root / "linked-after-dropped-response.png").write_bytes(page.screenshot())

            page.reload(wait_until="domcontentloaded")
            surface.issue_bootstrap()
            page.locator("#code").fill(surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app").wait_for(state="visible")
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-lookup").wait_for(state="visible")
            assert "uncertain" in page.locator("#linked-status").inner_text().lower()

            surface._session_deadline = time.monotonic() - 1
            page.locator("#linked-lookup").click()
            page.locator("#auth").wait_for(state="visible")
            assert page.evaluate(
                "key => sessionStorage.getItem(key)",
                "summon.workspace.pending-linked-replacement/v1",
            ) is not None

            surface.issue_bootstrap()
            page.locator("#code").fill(surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app").wait_for(state="visible")
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-lookup").wait_for(state="visible")
            assert page.locator("#linked-lookup").is_enabled()
            with page.expect_request(
                lambda request: request.url.endswith("/api/linked-replacements/lookup")
                and request.method == "POST"
            ) as lookup_info:
                page.locator("#linked-lookup").click()
            assert json.loads(lookup_info.value.post_data)["operation_key"] == operation_key
            for _ in range(100):
                if page.evaluate(
                    "key => sessionStorage.getItem(key) === null",
                    "summon.workspace.pending-linked-replacement/v1",
                ):
                    break
                page.wait_for_timeout(50)
            else:
                raise AssertionError("browser did not clear the same-key recovery record")
            assert page.locator("#linked-lookup").is_hidden()
            assert "choose one" in page.locator("#linked-status").inner_text().lower()
            final = fixture.demo.state()["workspace"]
            assert final["deliveries"][parent_id] == parent_before
            assert len([key for key in final["deliveries"] if key != parent_id]) == 1
            assert submissions == [operation_key]
            if evidence_root:
                (evidence_root / "linked-after-same-key-lookup.png").write_bytes(page.screenshot())
                (evidence_root / "receipt.json").write_text(json.dumps({
                    "schema": "summon.workspace.f17-browser-evidence/v1",
                    "browser": "chromium-headless",
                    "browser_version": browser.version,
                    "python_version": platform.python_version(),
                    "loopback_only": True,
                    "external_requests_blocked": len(external_requests),
                    "provider_contacted": False,
                    "dropped_response_recovered": True,
                    "same_key_lookup": True,
                    "submit_count": len(submissions),
                    "child_count": 1,
                    "parent_unchanged": True,
                }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            context.close()
            context = None
            browser.close()
            browser = None
            surface.stop()
            surface = None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if surface is not None:
            surface.stop()
        fixture.teardown_method()


def test_real_browser_recovers_same_key_through_ordinary_host_installation(tmp_path):
    """Exercise the browser journey through the shipped WorkspaceHost path.

    The earlier browser gate injected a pre-installed Surface adapter.  This
    variant uses the ordinary host installation API and reconciles the dropped
    response with the retained operation key through the shipped host surface.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    host = WorkspaceHost(str(tmp_path / "runs"), "run-a", _free_port(), mode="demo-create")
    host.start()
    browser = None
    context = None
    evidence_dir = os.environ.get("SUMMON_F17_BROWSER_EVIDENCE_DIR")
    evidence_root = Path(evidence_dir) if evidence_dir else None
    if evidence_root:
        evidence_root.mkdir(parents=True, exist_ok=True)
    try:
        demo = host.demo
        state = demo.state()["workspace"]
        parent_id = next(iter(state["deliveries"]))
        hold_source = demo.source({"kind": "browser-entry-linked-hold", "delivery_id": parent_id})
        hold_ref = demo.evidence(hold_source, "main-1", "browser-entry-linked-hold",
                                 "observation", parent_id, "held_for_recovery", "hold_observation")
        held = copy.deepcopy(state["deliveries"][parent_id])
        held.update(state="held_for_recovery", reason="recipient_drift", ack_level=None,
                    certainty={"contact": "unknown", "spend": "unknown", "cleanup": "unknown"})
        event = demo.event("workspace_delivery_advanced", {
            "delivery": held, "evidence": {"hold_observation": hold_ref},
            "supported_ack_levels": []}, "browser-entry-linked-hold-transition")
        host._record_install_command(event)
        demo.runtime.record_event(event)
        parent = demo.state()["workspace"]["deliveries"][parent_id]
        recipient = copy.deepcopy(parent["recipient"])
        sources = {}
        for role, category in (("authenticated_disposition", "event"),
                               ("remaining_authority", "grant"),
                               ("fresh_attempt_grant", "grant"),
                               ("physical_fence", "fence")):
            material = {"schema": "summon.workspace.linked-replacement-authority/v1",
                        "role": role, "parent_delivery_id": parent_id,
                        "task_id": "main-1", "recipient": recipient,
                        "generation": 3, "expires_at_ms": 10_000_000,
                        "revoked": False}
            source = demo.source(material)
            sources[role] = demo.evidence(source, "main-1", "browser-entry-linked-" + role,
                                          category, parent_id)
        authority = {"parent_delivery_id": parent_id, "parent_target": "opaque-parent",
                     "recipient_target": "opaque-recipient", "task_id": "main-1",
                     "recipient": recipient, "generation": 3, "now_ms": 1,
                     "sources": {role: {"reference": ref,
                                          "parent_delivery_id": parent_id,
                                          "task_id": "main-1", "recipient": copy.deepcopy(recipient),
                                          "generation": 3, "expires_at_ms": 10_000_000,
                                          "revoked": False}
                                 for role, ref in sources.items()}}
        scope = {"workspace_id": host.workspace_id, "run_id": host.run_id,
                 "targets": [{"parent_target": "opaque-parent",
                               "recipient_targets": ["opaque-recipient"]}]}
        host.install_operator_linked_replacements(
            scope, authorize_link=lambda _request, _workspace: True,
            resolve_link=lambda _parent, _recipient, _workspace: copy.deepcopy(authority))

        before = copy.deepcopy(demo.state()["workspace"])
        parent_before = copy.deepcopy(before["deliveries"][parent_id])
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.set_default_timeout(5000)
            submissions = []
            external_requests = []

            def block_external(route):
                host_name = urlsplit(route.request.url).hostname
                if host_name != "127.0.0.1":
                    external_requests.append(host_name or "missing-host")
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            def drop_link_response(route):
                if (route.request.method == "POST"
                        and route.request.url.endswith("/api/linked-replacements")):
                    body = json.loads(route.request.post_data or "{}")
                    submissions.append(body["operation_key"])
                    route.fetch()
                    route.abort("failed")
                    return
                route.continue_()

            page.route("**/*", block_external)
            page.route("**/api/linked-replacements", drop_link_response)
            page.goto(host.surface.url, wait_until="domcontentloaded")
            page.locator("#code").fill(host.surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-panel").wait_for(state="visible")
            assert page.get_by_role("button", name="Link replacement").is_visible()
            page.get_by_role("button", name="Link replacement").click()
            page.locator("#linked-lookup").wait_for(state="visible")
            assert len(submissions) == 1
            operation_key = submissions[0]
            page.locator("#linked-status").wait_for()
            assert "did not confirm" in page.locator("#linked-status").inner_text().lower()
            after_append = demo.state()["workspace"]
            assert len(after_append["deliveries"]) == len(before["deliveries"]) + 1
            assert after_append["deliveries"][parent_id] == parent_before
            assert len([item for key, item in after_append["deliveries"].items()
                        if key != parent_id]) == 1
            page.reload(wait_until="domcontentloaded")
            host.surface.issue_bootstrap()
            page.locator("#code").fill(host.surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-lookup").wait_for(state="visible")
            host.surface._session_deadline = time.monotonic() - 1
            page.locator("#linked-lookup").click()
            page.locator("#auth").wait_for(state="visible")
            assert page.evaluate(
                "key => sessionStorage.getItem(key)",
                "summon.workspace.pending-linked-replacement/v1",
            ) is not None
            host.surface.issue_bootstrap()
            page.locator("#code").fill(host.surface.bootstrap_code)
            page.locator("#login button").click()
            page.locator("#app[aria-busy='false']").wait_for()
            page.locator("#linked-lookup").wait_for(state="visible")
            with page.expect_request(
                lambda request: request.url.endswith("/api/linked-replacements/lookup")
                and request.method == "POST"
            ) as lookup_info:
                page.locator("#linked-lookup").click()
            assert json.loads(lookup_info.value.post_data)["operation_key"] == operation_key
            for _ in range(100):
                if page.evaluate(
                    "key => sessionStorage.getItem(key) === null",
                    "summon.workspace.pending-linked-replacement/v1",
                ):
                    break
                page.wait_for_timeout(50)
            else:
                raise AssertionError("ordinary host did not clear same-key recovery record")
            assert submissions == [operation_key]
            final = demo.state()["workspace"]
            assert final["deliveries"][parent_id] == parent_before
            assert len([key for key in final["deliveries"] if key != parent_id]) == 1
            if evidence_root:
                (evidence_root / "ordinary-host-after-same-key-lookup.png").write_bytes(page.screenshot())
                (evidence_root / "ordinary-host-receipt.json").write_text(
                    json.dumps({"schema": "summon.workspace.f17-ordinary-host-browser/v1",
                                "browser": "chromium-headless", "browser_version": browser.version,
                                "loopback_only": True, "external_requests_blocked": len(external_requests),
                                "provider_contacted": False, "dropped_response_recovered": True,
                                "same_key_lookup": True, "submit_count": len(submissions),
                                "child_count": 1, "parent_unchanged": True},
                               sort_keys=True, indent=2) + "\n", encoding="utf-8")
            context.close()
            context = None
            browser.close()
            browser = None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        host.stop()
