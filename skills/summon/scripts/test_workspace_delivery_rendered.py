"""Remaining U04 worker boundaries in the real page, using synthetic protocol records.

This complements the accepted v3 rendered fixture/terminal selection. Submitted
and acknowledged are explicit simulated protocol observations, not evidence of
physical worker execution, provider contact, understanding or billing. Every
record uses the actual demo evidence and validated runtime transition producers.
"""
from __future__ import annotations

import copy
import datetime
import math
import os
from pathlib import Path

from _workspace_content import prepare_content
from _workspace_view import ViewScope, _opaque
from test_workspace_canonical_rendered import rendered, _login, _select_task, _capture


def _accepted_message(demo, template, index):
    """Stop at the actual admitted event; unlike demo.message, do not queue it."""
    body = "Owned accepted context; no turn has selected it."
    content = prepare_content(body)
    message = copy.deepcopy(template)
    message.update(message_id="u04-message-" + str(index),
                   sequence=demo.state()["workspace"]["streams"][template["stream_id"]]["sequence"] + 1,
                   content={"ref": content.reference, "sha256": content.sha256,
                            "utf8_bytes": content.payload_bytes})
    delivery = {"protocol": message["protocol"], "workspace_id": demo.workspace_id,
                "run_id": demo.run_id, "kind": "delivery", "delivery_id": "u04-delivery-" + str(index),
                "message_id": message["message_id"], "recipient": message["recipient"],
                "grant_ref": message["grant_ref"], "state": "accepted", "reason": None,
                "selection": None, "ack_level": None, "inherited_uncertainty": [],
                "certainty": {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"}}
    event = demo.event("workspace_message_admitted", {"message": message, "delivery": delivery}, "u04-accepted")
    demo.permit(event)
    demo.runtime.send_context(event, body)
    return delivery["delivery_id"], demo.state()["workspace"]["revision"]


def _advance_observation(demo, identifier, target, grant, source):
    roles = {
        "submission_started": {"recipient_owner_fence": "fence", "capacity_reservation": "fence",
                               "physical_attempt_reservation": "fence", "durable_launch_intent": "event"},
        "submitted": {"adapter_receipt": "adapter_receipt"},
        "acknowledged": {"ack_receipt": "adapter_receipt"},
        "not_submitted": {"no_contact_receipt": "event", "cleanup_evidence": "fence"},
    }[target]
    proofs = {role: demo.evidence(source, "main-1", "u04-" + target + "-" + role, category,
                                  identifier, target, role) for role, category in roles.items()}
    if target == "submission_started":
        proofs["current_grant"] = grant
        certainty = {"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
    elif target == "submitted":
        proofs["submission_boundary"] = "full_frame_receipt"
        certainty = {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"}
    elif target == "acknowledged":
        # The existing fixed simulation declares only recipient_received.
        proofs["ack_level"] = "recipient_received"
        certainty = None
    else:
        proofs.update(provider_contact=False, side_effects=False)
        certainty = {"contact": "none", "spend": "not_incurred", "cleanup": "not_started"}
    demo.advance(identifier, target, proofs, certainty)
    return demo.state()["workspace"]["revision"]


def _capture_state_card(card, state):
    """Retain each asserted state itself, including cards below the fold."""
    directory = os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR")
    if directory:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        card.scroll_into_view_if_needed()
        # The durable artifact contract uses hyphenated filenames even though
        # protocol state identifiers retain their snake_case spelling.
        artifact_state = state.replace("_", "-")
        card.screenshot(path=str(destination / ("u04-worker-state-card-" + artifact_state + ".png")))


def _assert_state(host, page, snapshot, identifier, state, *, revision=None):
    labels = {"accepted": "Accepted durably", "queued": "Queued for a later turn",
              "included_in_attempt": "Included in the selected attempt",
              "submission_started": "Submission started; effects uncertain",
              "submitted": "Submission receipt recorded", "acknowledged": "Acknowledgement recorded",
              "not_submitted": "Proven no contact; no automatic retry"}
    target = _opaque(ViewScope(host.workspace_id, host.run_id, host.surface._key), "delivery", identifier)
    index, item = next((index, item) for index, item in enumerate(snapshot["deliveries"]) if item["id"] == target)
    card = page.locator("#delivery-list .card").nth(index)
    card.get_by_role("heading", name=labels[state], exact=True).wait_for()
    text = card.inner_text()
    assert item["state"] == state and item["label"] == labels[state]
    expected = ({"contact": "unknown", "spend": "unknown", "cleanup": "unknown"}
                if state == "submission_started" else
                {"contact": "occurred", "spend": "unknown", "cleanup": "unknown"}
                if state in {"submitted", "acknowledged"} else
                {"contact": "none", "spend": "not_incurred", "cleanup": "not_started"}
                if state == "not_submitted" else
                {"contact": "not_attempted", "spend": "not_incurred", "cleanup": "not_started"})
    assert item["certainty"] == expected
    for fact, value in expected.items():
        assert fact + ": " + value in text
    acknowledgement = "recipient_received" if state == "acknowledged" else None
    assert item["acknowledgement"] == acknowledgement
    assert "Acknowledgement: " + (acknowledgement or "none") in text
    # Nonterminal boundaries have no terminal reason. Assert this finite absence
    # rather than invent a reason or expose source observations to the reader.
    assert item["reason"] is None and "Reason:" not in text and "Hold:" not in text
    assert item["automatic_retry"] is False and item["provider_billing_verified"] is False
    assert "token cost" not in text.lower()
    selected = state not in {"accepted", "queued"}
    assert (item["turn_id"] is not None) == selected
    assert "Stream sequence " + str(item["sequence"]) in text
    assert (" / selected turn" if selected else " / no selected turn") in text
    actions = item["action_guidance"]["actions"]
    assert actions == snapshot["operator_commands"]["actions_by_delivery"].get(target, [])
    if state == "queued":
        assert actions == ["cancel_queued_context"]
        assert item["action_guidance"]["unavailable_reason"] is None
        assert "Available in current scope: Cancel queued context" in text
        assert "none implies retry or resolves contact/spend uncertainty" in text
        assert card.get_by_role("button", name="Cancel queued context", exact=True).is_visible()
    else:
        assert actions == []
        assert item["action_guidance"]["unavailable_reason"] == "no_delivery_action_in_current_scope"
        assert "Delivery actions unavailable: no action is authorized" in text
        assert card.get_by_role("button").count() == 0
    if revision is not None:
        event = next(item for item in snapshot["timeline"]["items"] if item["revision"] == revision)
        assert type(event["occurred_at"]) in (int, float) and math.isfinite(event["occurred_at"])
        assert event["timestamp_reason"] == "journal_recorded"
        milliseconds = int(event["occurred_at"] * 1000)
        stamp = datetime.datetime.fromtimestamp(milliseconds / 1000, datetime.timezone.utc)
        expected_time = stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event_card = page.locator('#event-list [data-event-id="' + event["id"] + '"]')
        assert "Journal recorded at " + expected_time in event_card.inner_text()
    else:
        assert "Journal recorded at " in page.locator("#event-list").inner_text()
    assert "requested model: unavailable" in page.locator("#identity").inner_text()
    assert "verified served model: unavailable" in page.locator("#identity").inner_text()
    _capture_state_card(card, state)


def test_remaining_worker_delivery_boundaries_render_with_separate_evidence(rendered):
    host, page, _root = rendered
    try:
        demo = host.demo
        original = next(iter(demo.state()["workspace"]["deliveries"]))
        # A new canonical fixed-simulation receiver and stream avoid stealing
        # the original operator target or bypassing its unresolved FIFO prefix.
        grant, _ = demo.grant("main-1", "u04-fixed-receiver", suffix="u04-fixed-grant")
        # The canonical fixed transport validates integer-array payloads before
        # creating its gate; no worker is spawned or computation run here.
        entries = [demo.message("main-1", "u04-fixed-receiver", grant, index,
                                "[1,2]", source_task="main-1")
                   for index in (201, 202)]
        template = demo.state()["workspace"]["messages"][entries[-1]["message_id"]]
        accepted, accepted_revision = _accepted_message(demo, template, 203)
        snapshot = _login(host, page, first=True)
        _assert_state(host, page, snapshot, original, "queued")
        _assert_state(host, page, snapshot, accepted, "accepted", revision=accepted_revision)
        _capture(page, "u04-worker-accepted-and-queued")
        turn = demo.admit("main-1", "u04-fixed-receiver", grant, entries)
        assert turn["event"]["payload"]["supported_ack_levels"] == ["recipient_received"]
        revision = demo.state()["workspace"]["revision"]
        snapshot = _select_task(page, "Task 1")
        _assert_state(host, page, snapshot, entries[0]["delivery_id"], "included_in_attempt", revision=revision)
        _capture(page, "u04-worker-included")
        observation = demo.source({
            "kind": "explicit-synthetic-protocol-observations", "qualification": "rendered_protocol_fixture",
            "physical_worker_executed": False, "provider_invoked": False,
            "submission_boundary": "full_frame_receipt", "ack_level": "recipient_received",
            "no_contact_case": {"provider_contact": False, "side_effects": False},
        })
        for state in ("submission_started", "submitted", "acknowledged"):
            revision = _advance_observation(demo, entries[0]["delivery_id"], state, grant, observation)
            snapshot = _select_task(page, "Task 1")
            _assert_state(host, page, snapshot, entries[0]["delivery_id"], state, revision=revision)
            _capture(page, "u04-worker-" + state.replace("_", "-"))
        revision = _advance_observation(demo, entries[1]["delivery_id"], "not_submitted", grant, observation)
        snapshot = _select_task(page, "Task 1")
        _assert_state(host, page, snapshot, entries[1]["delivery_id"], "not_submitted", revision=revision)
        _assert_state(host, page, snapshot, entries[0]["delivery_id"], "acknowledged")
        assert "contact: occurred" in page.locator("#delivery-list").inner_text()
        assert "spend: unknown" in page.locator("#delivery-list").inner_text()
        assert demo.total_workers == 0 and demo.workers == {}
        assert demo.state()["workspace"]["deliveries"][original]["state"] == "queued"
        assert page.locator("a[href*='token='],a[href*='bearer=']").count() == 0
        _capture(page, "u04-worker-not-submitted-and-acknowledged")
    except Exception:
        _capture(page, "u04-worker-failure")
        raise
