"""Owned Chromium/ordinary-host checks; synthetic canonical records only.

This does not qualify provider execution, real ownership handoff, assistive
technology, mobile keyboards, or all delivery boundaries. No browser downloads.
"""
from __future__ import annotations

import copy
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import _council
from _workspace_entry import WorkspaceHost
from test_workspace_details import (_deliberation_receipt, _seed_canonical_review_and_decision,
                                    _seed_deliberation_terminal_run)
import _deliberation_store
import _rundir
from test_workspace_entry import _free_port


@pytest.fixture
def rendered(tmp_path):
    # Unavailable tooling is a failure here, never an acceptance skip.
    from playwright.sync_api import sync_playwright
    host = WorkspaceHost(str(tmp_path / "runs"), "rendered-run", _free_port(), mode="demo-create")
    host.start()
    forbidden = []
    browser = context = None
    try:
        with sync_playwright() as driver:
            options = {"headless": True, "args": [
                "--disable-background-networking", "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]}
            executable = os.environ.get("SUMMON_UI_CHROMIUM_EXECUTABLE")
            if executable:
                options["executable_path"] = executable
            browser = driver.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1100, "height": 800},
                                          service_workers="block")
            origin = urlsplit(host.surface.url)
            def bounded_route(route):
                target = urlsplit(route.request.url)
                if (target.scheme, target.hostname, target.port) != (origin.scheme, origin.hostname, origin.port):
                    forbidden.append("non-owned-page-request")
                    route.abort("blockedbyclient")
                else:
                    route.continue_()
            context.route("**/*", bounded_route)
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            page.set_default_timeout(6000)
            yield host, page, tmp_path
            assert forbidden == []
            evidence = os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR")
            if evidence:
                Path(evidence).mkdir(parents=True, exist_ok=True)
                (Path(evidence) / "browser-toolchain.json").write_text(json.dumps({
                    "browser": "Chromium", "browser_version": browser.version,
                    "playwright_version": importlib.metadata.version("playwright"),
                    "python_version": platform.python_version(), "platform": platform.platform(),
                    "headless": True, "viewport": {"width": 1100, "height": 800},
                    "network": "owned-loopback-only", "providers": "not invoked",
                }, indent=2) + "\n", encoding="utf-8")
            context.close()
            context = None
            browser.close()
            browser = None
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        host.stop()


def _login(host, page, *, first=False):
    if first:
        page.goto(host.surface.url, wait_until="domcontentloaded")
    else:
        host.surface.issue_bootstrap()
    page.locator("#code").fill(host.surface.bootstrap_code)
    page.locator("#login button").press("Enter")
    page.locator("#app[aria-busy='false']").wait_for()
    page.locator("#app").wait_for(state="visible")
    # Login deliberately opens a workspace-wide view. The fixture's sorted
    # main-1/main-2/side-1 lanes label main-1 as Task 1, even after its recorded
    # successor decision makes main-2 the priority. Select the reviewed task.
    return _select_task(page, "Task 1")


def _select_task(page, label):
    from playwright.sync_api import expect
    tasks = page.evaluate("current.tasks")
    matches = [task for task in tasks if task["label"] == label]
    assert len(matches) == 1, "the intended synthetic task must be present in the real view"
    task = matches[0]
    button = page.locator('#tasks button[data-task-id="' + task["id"] + '"]')
    def is_selected_view(response):
        url = urlsplit(response.url)
        return url.path == "/api/view" and parse_qs(url.query).get("task") == [task["id"]]
    with page.expect_response(is_selected_view) as observed:
        button.press("Enter")
    assert observed.value.status == 200
    snapshot = observed.value.json()
    assert snapshot["selected_task_id"] == task["id"]
    selected = next(row for row in snapshot["tasks"] if row["id"] == task["id"])
    expect(button).to_have_attribute("aria-current", "true")
    expect(page.locator("#task-title")).to_have_text(
        selected["label"] + ("  /  current priority" if selected["is_priority"] else ""))
    expect(page.locator("#identity")).to_have_text(
        "Agent: " + selected["agent"]["logical_label"] + "  /  worker: "
        + selected["agent"]["worker_label"]
        + "  /  requested model: unavailable  /  verified served model: unavailable")
    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
    displayed = page.evaluate("({task:current.selected_task_id, snapshot:current.snapshot, details:current.details})")
    assert displayed == {"task": snapshot["selected_task_id"], "snapshot": snapshot["snapshot"],
                         "details": snapshot["details"]}
    return snapshot


def _capture(page, name):
    directory = os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / (name + ".png")), mask=[page.locator("#code")])


def _open_details(page):
    page.locator("#details-open").focus()
    page.keyboard.press("Enter")
    page.locator("#details-drawer").wait_for(state="visible")
    assert page.locator("#details-close").evaluate("node => node === document.activeElement")


def _choose(page, label, heading):
    from playwright.sync_api import expect
    page.get_by_role("button", name=label, exact=True).press("Enter")
    expect(page.locator("#details-status")).to_contain_text("Typed detail loaded")
    page.locator("#details-body").get_by_role("heading", name=heading, exact=True).wait_for()


def test_canonical_focal_details_and_separate_body_scope_render_in_ordinary_host(rendered):
    host, page, root = rendered
    _seed_canonical_review_and_decision(host)
    artifact_text = "Owned synthetic artifact: the bounded review evidence is ready."
    artifact = host.demo.source(artifact_text.encode())
    reference = host.demo.evidence(artifact, "main-1", "rendered-artifact", "artifact")
    council = root / "council" / "review"
    assert _council._atomic_write_json(str(council / "receipt.json"), {
        "mode": "council", "run_id": "review", "question": "Synthetic advisory review",
        "status": "success", "council_state": "final", "generation": 1,
    }) is None
    deliberations = root / "deliberations"
    sources = []
    for state in ("DECIDED", "UNRESOLVED", "CANCELLED"):
        identifier = "deliberation-" + state.lower()
        _seed_deliberation_terminal_run(deliberations, identifier, state)
        sources.append({"source_key": identifier, "task_id": "main-1", "root": str(deliberations),
                        "run_id": identifier, "revision": 1, "state": "completed",
                        "label": "Deliberation " + state})
    _, owner = _deliberation_store.initialize_run(str(deliberations), _deliberation_receipt("pending"))
    _rundir.release_owner(owner)
    sources.append({"source_key": "pending", "task_id": "main-1", "root": str(deliberations),
                    "run_id": "pending", "revision": 1, "state": "pending", "label": "Pending review"})
    installed = host.install_canonical_workspace_details(
        council_sources=[{"source_key": "council", "task_id": task, "root": str(council),
                          "run_id": "review", "revision": 1, "state": "completed", "label": label}
                         for task, label in (("main-1", "Advisory council"),)],
        deliberation_sources=sources, body_source_keys=["workspace_evidence:" + reference["id"]])
    assert installed["canonical"] is True
    focal = _login(host, page, first=True)
    authorized = [item for item in focal["details"]["targets"]
                  if item["source_kind"] == "workspace_evidence" and item["body_available"]]
    assert len(authorized) == 1, "the explicit canonical artifact body scope must remain available"
    assert authorized[0]["task_id"] == focal["selected_task_id"], (
        "canonical detail and workspace view must use the same opaque task identity")
    initial_title = page.locator("#task-title").inner_text()
    _open_details(page)
    _choose(page, "Workspace assessment / workspace assessment", "Workspace assessment")
    detail = page.locator("#details-body")
    assert "Disposition: continue_main" in detail.inner_text()
    assert "checked-computation: met" in detail.inner_text()
    assert detail.get_by_role("button", name="Open separately authorized body").count() == 0
    target = page.evaluate("current.details.targets.find(item => item.source_kind === 'workspace_assessment')")
    refusal = page.evaluate("""async target => {
      try {await request('/api/details/body', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({target:target.id, source:target.source_kind, task:target.task_id})}); return {};
      } catch(error) {return {status:error.status, reason:error.message};}
    }""", target)
    assert refusal == {"status": 403, "reason": "detail_body_scope_required"}
    _choose(page, "Workspace decision / workspace decision", "Workspace decision")
    assert "Decision: successor_selected" in detail.inner_text()
    _choose(page, "Advisory council / council review", "Council review")
    assert "Type: council_review" in detail.inner_text()
    for state, label in (("DECIDED", "decision_recorded"), ("UNRESOLVED", "decision_unresolved"),
                         ("CANCELLED", "decision_cancelled")):
        _choose(page, "Deliberation " + state + " / deliberation decision", "Deliberation decision")
        assert "Decision: " + label in detail.inner_text()
        if state != "DECIDED":
            assert "decision_recorded" not in detail.inner_text()
    _choose(page, "Pending review / deliberation pending", "Deliberation pending")
    assert "Decision: pending" in detail.inner_text()
    assert "state: pending" in detail.inner_text()
    evidence_targets = page.evaluate("current.details.targets.filter(item => item.source_kind === 'workspace_evidence' && item.task_id === current.selected_task_id)")
    index = next(index for index, item in enumerate(evidence_targets) if item["body_available"])
    page.get_by_role("button", name="Workspace evidence / workspace evidence", exact=True).nth(index).press("Enter")
    detail.get_by_role("heading", name="Workspace evidence", exact=True).wait_for()
    assert artifact_text not in detail.inner_text()
    detail.get_by_role("button", name="Open separately authorized body").press("Enter")
    page.locator("pre[data-detail-body]").wait_for()
    assert page.locator("pre[data-detail-body]").inner_text() == artifact_text
    _capture(page, "canonical-artifact-separate-scope")
    assert page.locator("#task-title").inner_text() == initial_title
    assert page.locator("a[href*='token='],a[href*='bearer=']").count() == 0

    # Defer a real successful body response, then close/select a different
    # canonical detail. Selection generation must discard the stale response.
    pending = []
    def defer_body(route):
        pending.append((route, route.fetch()))
    page.route("**/api/details/body", defer_body)
    page.get_by_role("button", name="Workspace evidence / workspace evidence", exact=True).nth(index).press("Enter")
    detail.get_by_role("button", name="Open separately authorized body").press("Enter")
    pending_deadline = time.monotonic() + 6
    while not pending and time.monotonic() < pending_deadline:
        page.wait_for_timeout(20)
    assert len(pending) == 1 and pending[0][1].status == 200
    page.keyboard.press("Escape")
    assert page.locator("#details-open").evaluate("node => node === document.activeElement")
    _open_details(page)
    _choose(page, "Advisory council / council review", "Council review")
    with page.expect_response(lambda response: response.url.endswith("/api/details/body")):
        pending[0][0].fulfill(response=pending[0][1])
    page.wait_for_timeout(100)
    assert page.locator("pre[data-detail-body]").count() == 0
    assert "Council review" in detail.inner_text()
    page.unroute("**/api/details/body", defer_body)
    page.keyboard.press("Escape")
    # A real rail selection changes the focal scope; main-task details vanish.
    _select_task(page, "Task 2")
    _open_details(page)
    assert page.get_by_role("button", name="Advisory council / council review", exact=True).count() == 0
    assert page.get_by_role("button", name="Workspace assessment / workspace assessment", exact=True).count() == 0
    _capture(page, "canonical-focal-task-scope")


def _terminal_delivery(host, template, index, state):
    demo = host.demo
    item = demo.message("main-1", template["recipient"]["instance_id"], template["grant_ref"],
                        index, "Owned synthetic terminal context")
    identifier = item["delivery_id"]
    def advance(target, role, category, reason):
        timing = {"now_ms": 2000, "expires_at_ms": 1000} if target == "expired" else {}
        source = demo.source({"kind": "synthetic-terminal-observation", "state": target, **timing})
        proof = demo.evidence(source, "main-1", "terminal-" + str(index) + "-" + target,
                              category, identifier, target, role)
        value = copy.deepcopy(demo.state()["workspace"]["deliveries"][identifier])
        value.update(state=target, reason=reason, ack_level=None)
        if target == "held_for_recovery":
            value["certainty"] = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
        demo.record("workspace_delivery_advanced", {"delivery": value, "evidence": {role: proof, **timing},
                    "supported_ack_levels": []}, "terminal-transition-" + str(index) + "-" + target)
    if state in {"held_for_recovery", "dead_lettered"}:
        advance("held_for_recovery", "hold_observation", "observation", "contact_uncertain")
    if state != "held_for_recovery":
        role, category = {"rejected": ("validated_refusal", "event"),
                          "expired": ("expiry_observation", "observation"),
                          "cancelled": ("authorized_cancellation", "grant"),
                          "dead_lettered": ("authenticated_disposition", "event")}[state]
        advance(state, role, category, "synthetic-private-terminal-reason")
    return identifier


def test_terminal_guidance_and_pending_action_expiry_render_in_ordinary_host(rendered):
    host, page, _root = rendered
    workspace = host.demo.state()["workspace"]
    original_id, original = next(iter(workspace["deliveries"].items()))
    template = workspace["messages"][original["message_id"]]
    for index, state in enumerate(("rejected", "expired", "cancelled", "dead_lettered", "held_for_recovery"), 101):
        _terminal_delivery(host, template, index, state)
    focal = _login(host, page, first=True)
    identity = page.locator("#identity").inner_text()
    assert "requested model: unavailable" in identity and "verified served model: unavailable" in identity
    cards = page.locator("#delivery-list .card")
    expected = (("Rejected", "rejected reason unavailable"), ("Expired", "expired reason unavailable"),
                ("Cancelled", "cancelled reason unavailable"), ("Explicitly disposed", "dead lettered reason unavailable"))
    for label, reason in expected:
        card = cards.filter(has=page.get_by_role("heading", name=label, exact=True))
        assert card.count() == 1
        content = card.inner_text()
        assert "Reason: " + reason in content
        assert "Hold:" not in content
        assert "Delivery actions unavailable: no action is authorized" in content
    held = cards.filter(has=page.get_by_role("heading", name="Held for explicit recovery", exact=True))
    assert "Hold: contact uncertain" in held.inner_text()
    assert "contact: unknown" in held.inner_text() and "spend: unknown" in held.inner_text()
    dead = cards.filter(has=page.get_by_role("heading", name="Explicitly disposed", exact=True))
    assert "contact: unknown" in dead.inner_text() and "spend: unknown" in dead.inner_text()
    assert "synthetic-private-terminal-reason" not in page.locator("body").inner_text()
    assert "Journal recorded at " in page.locator("#timeline").inner_text()
    queued = cards.filter(has=page.get_by_role("heading", name="Queued for a later turn", exact=True))
    assert "Available in current scope: Cancel queued context" in queued.inner_text()
    assert "none implies retry or resolves contact/spend uncertainty" in queued.inner_text()
    _capture(page, "terminal-guidance-and-model-uncertainty")
    assert focal["mode"] == {"mode": "passive", "owner_state": "unknown", "verified_active": False}
    assert "Supervisor owner: unknown / confirmed snapshot only" in page.locator("#owner-state").inner_text()

    commands = []
    def expire_at_admission(route):
        commands.append(json.loads(route.request.post_data))
        host.surface._session_deadline = time.monotonic() - 1
        route.continue_()
    page.route("**/api/commands", expire_at_admission)
    with page.expect_response(lambda response: response.url.endswith("/api/commands") and response.status == 401):
        queued.get_by_role("button", name="Cancel queued context", exact=True).press("Enter")
    page.locator("#auth").wait_for(state="visible")
    assert len(commands) == 1
    retained = page.evaluate("sessionStorage.getItem('summon.workspace.pending-context/v1')")
    assert retained and json.loads(retained)["body"] == commands[0]
    assert "remain unresolved" in page.locator("#auth-message").inner_text()
    assert host.demo.state()["workspace"]["deliveries"][original_id]["state"] == "queued"
    _capture(page, "pending-action-authentication-expiry")
    page.unroute("**/api/commands", expire_at_admission)
    _login(host, page)
    page.locator("#command-lookup").wait_for(state="visible")
    assert page.evaluate("sessionStorage.getItem('summon.workspace.pending-context/v1')") == retained
    assert "uncertain" in page.locator("#command-status").inner_text().lower()
    assert host.demo.total_workers == 0
