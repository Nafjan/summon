"""U08 preview recovery through the ordinary host and current browser page.

The journal writer really loses its generation at a controlled append boundary;
this is not a P2 supervisor lease or process handoff. Passive/unknown remains the
truthful owner presentation. Synthetic held context retains unknown effects.
Natural browser timers measure initial retry growth and announcement coalescing;
the existing page-script tests separately cover repeated capped intervals.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect

import _rundir
import _swarm_coordinator
from test_workspace_canonical_rendered import rendered, _login, _select_task, _terminal_delivery


PENDING_SLOT = "summon.workspace.pending-context/v1"


def _capture(page, name, selector):
    directory = os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR")
    if directory:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        page.locator(selector).screenshot(path=str(destination / (name + ".png")), mask=[page.locator("#code")])


def _retained(page):
    return page.evaluate("sessionStorage.getItem('summon.workspace.pending-context/v1')")


def _uncertainty(host, page, held_id, initial):
    assert host.demo.state()["workspace"]["deliveries"][held_id] == initial
    held = page.locator("#delivery-list .card").filter(
        has=page.get_by_role("heading", name="Held for explicit recovery", exact=True))
    assert held.count() == 1
    assert "contact: unknown" in held.inner_text()
    assert "spend: unknown" in held.inner_text()
    assert "cleanup: unknown" in held.inner_text()
    assert "Acknowledgement: none" in held.inner_text()
    assert "verified served model: unavailable" in page.locator("#identity").inner_text()
    assert host.demo.total_workers == 0 and host.demo.workers == {}


def test_writer_loss_retains_pending_key_and_natural_reconnect_is_bounded(rendered, monkeypatch):
    host, page, _root = rendered
    demo = host.demo
    workspace = demo.state()["workspace"]
    original_id, original = next(iter(workspace["deliveries"].items()))
    template = workspace["messages"][original["message_id"]]
    held_id = _terminal_delivery(host, template, 301, "held_for_recovery")
    held_initial = copy.deepcopy(demo.state()["workspace"]["deliveries"][held_id])
    initial = _login(host, page, first=True)
    # The ordinary rendered fixture uses a short default for local assertions.
    # Windows scheduling can delay the loopback command response while the
    # writer-fencing hook performs its bounded journal transition; keep the
    # recovery test bounded, but allow that synthetic request a wider window.
    page.set_default_timeout(15000)
    assert initial["mode"] == {"mode": "passive", "owner_state": "unknown", "verified_active": False}

    requests, failures, notices, loss = [], [], [], {}
    def observe_request(request):
        path = urlsplit(request.url).path
        if path in {"/api/commands", "/api/commands/lookup"}:
            requests.append((path, json.loads(request.post_data)))
    page.on("request", observe_request)
    original_append = _swarm_coordinator.journal_append_encoded

    def lose_writer(run_dir, payload, owner, *, expected_record=None):
        event = (expected_record or {}).get("workspace_event", {})
        delivery = event.get("payload", {}).get("delivery", {})
        if not loss and event.get("event") == "workspace_delivery_advanced" and delivery.get("reason") == "cancel_queued_context":
            assert delivery["delivery_id"] == original_id
            assert _rundir.owner_still_current(owner)
            # Controlled real generation transfer, after the command evidence
            # was durably recorded, before its delivery transition is written.
            _rundir.release_owner(owner)
            successor = _rundir.acquire_owner(run_dir, 90)
            loss.update(predecessor=owner, successor=successor, fenced=False)
            assert successor.generation > owner.generation
            assert not _rundir.owner_still_current(owner)
            try:
                return original_append(run_dir, payload, owner, expected_record=expected_record)
            except _rundir.OwnershipLostError:
                loss["fenced"] = True
                raise
        return original_append(run_dir, payload, owner, expected_record=expected_record)

    monkeypatch.setattr(_swarm_coordinator, "journal_append_encoded", lose_writer)
    def refuse_view(route):
        failures.append(time.monotonic())
        route.abort("connectionfailed")
    try:
        queued = page.locator("#delivery-list .card").filter(
            has=page.get_by_role("heading", name="Queued for a later turn", exact=True))
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/commands") as observed:
            queued.get_by_role("button", name="Cancel queued context", exact=True).press("Enter")
        assert observed.value.status == 200
        result = observed.value.json()
        assert result["status"] == "uncertain" and result["retry_with_new_key"] is False
        assert result["task_or_provider_action"] is False and result["target_state"] is None
        expect(page.locator("#command-status")).to_contain_text("Outcome is uncertain")
        assert loss["fenced"] and _rundir.owner_still_current(loss["successor"])
        assert not _rundir.owner_still_current(loss["predecessor"])
        retained = _retained(page)
        body = json.loads(retained)["body"]
        assert requests == [("/api/commands", body)] and result["operation_key"] == body["operation_key"]
        assert demo.state()["workspace"]["deliveries"][original_id]["state"] == "queued"
        assert demo.state()["workspace"]["revision"] > initial["workspace_revision"]
        assert page.evaluate("current.snapshot") == initial["snapshot"]
        _uncertainty(host, page, held_id, held_initial)
        _capture(page, "u08-writer-loss-pending", "#command-panel")

        # Observe DOM announcements without changing application state, clocks,
        # timers, request implementations, capability data or CSP.
        page.expose_function("u08RecordNotice", lambda text: notices.append(text))
        page.evaluate("""() => new MutationObserver(() => {
            globalThis.u08RecordNotice(document.getElementById('notice').textContent);
        }).observe(document.getElementById('notice'), {childList:true, characterData:true, subtree:true})""")
        page.route("**/api/view**", refuse_view)
        deadline = time.monotonic() + 44
        while len(failures) < 4 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert len(failures) == 4, "four natural retries must fit the bounded initial-growth window"
        while page.evaluate("loading") and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert page.evaluate("({delay, loading, timer:pollTimer!==null})") == {
            "delay": 30000, "loading": False, "timer": True}
        intervals = [right - left for left, right in zip(failures, failures[1:])]
        for actual, base in zip(intervals, (4, 8, 16)):
            # Browser/Python route dispatch adds bounded observation overhead;
            # actual scheduler jitter is 80-120%, checked separately in script.
            assert base * .8 - .15 <= actual <= base * 1.2 + 1.5
        reconnection = "Reconnection pending. A browser network hint or timeout does not establish worker failure or resolve spend."
        assert notices == [reconnection]
        failure_notice_count = len(notices)
        assert page.locator("#notice").inner_text() == reconnection
        assert "Connection uncertain / last confirmed snapshot" in page.locator("#connection").inner_text()
        assert "Supervisor owner: unknown / last confirmed snapshot / connection uncertain" in page.locator("#owner-state").inner_text()
        assert page.evaluate("current.snapshot") == initial["snapshot"]
        assert _retained(page) == retained and requests == [("/api/commands", body)]
        _uncertainty(host, page, held_id, held_initial)
        _capture(page, "u08-natural-backoff-stale-owner", "header.top")
        _capture(page, "u08-coalesced-reconnection-notice", "#notice")
        _capture(page, "u08-pending-key-during-backoff", "#command-panel")

        page.unroute("**/api/view**", refuse_view)
        refreshed = _select_task(page, "Task 1")
        assert refreshed["workspace_revision"] > initial["workspace_revision"]
        assert page.evaluate("delay") == 2000
        assert _retained(page) == retained and len(requests) == 1
        assert "Supervisor owner: unknown / confirmed snapshot only" in page.locator("#owner-state").inner_text()
        assert _rundir.owner_still_current(loss["successor"])
        _rundir.release_owner(loss["successor"])
        assert not _rundir.owner_still_current(loss["successor"])
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/commands/lookup") as lookup:
            page.locator("#command-lookup").press("Enter")
        assert lookup.value.status == 200
        assert lookup.value.json()["status"] == "pending"
        assert lookup.value.json()["operation_key"] == body["operation_key"]
        assert lookup.value.json()["retry_with_new_key"] is False
        expect(page.locator("#command-status")).to_contain_text("The decision is durable")
        assert _retained(page) == retained
        _capture(page, "u08-authorized-same-key-continuation", "#command-panel")
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/commands") as completed:
            page.locator("#command-continue").press("Enter")
        assert completed.value.status == 200 and completed.value.json()["status"] == "recorded"
        assert completed.value.json()["operation_key"] == body["operation_key"]
        expect(page.locator("#delivery-list")).to_contain_text("Cancelled")
        assert requests == [("/api/commands", body), ("/api/commands/lookup", body), ("/api/commands", body)]
        assert _retained(page) is None
        assert demo.state()["workspace"]["deliveries"][original_id]["state"] == "cancelled"
        _uncertainty(host, page, held_id, held_initial)
        held = page.locator("#delivery-list .card").filter(
            has=page.get_by_role("heading", name="Held for explicit recovery", exact=True))
        directory = os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR")
        if directory:
            held.screenshot(path=str(Path(directory) / "u08-unresolved-effects-after-recovery.png"))
            (Path(directory) / "u08-observations.json").write_text(json.dumps({
                "journal_writer_loss": "actual generation transfer at controlled append boundary",
                "supervisor_transition": "not claimed; passive unknown",
                "initial_retry_intervals_seconds": intervals, "observed_next_base_ms": 30000,
                "success_base_ms": 2000, "unchanged_failure_announcements": failure_notice_count,
                "same_operation_body_all_three_requests": True, "workers_started": 0,
                "provider_contact": "not invoked; held effects are synthetic protocol observations",
            }, indent=2), encoding="utf-8")
    except Exception:
        _capture(page, "u08-failure-shell", "body")
        raise
    finally:
        if loss:
            _rundir.release_owner(loss["successor"])
