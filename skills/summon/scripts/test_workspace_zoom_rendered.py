"""Native desktop page zoom through owned Chromium settings; no emulation.

One fresh persistent normal profile, actual ordinary workspace host, synthetic
canonical records, and separately scoped body content. Missing native zoom
proof fails. This does not qualify AT speech or native mobile keyboards.
"""
from __future__ import annotations

import base64
import importlib.metadata
import json
import os
from pathlib import Path
import struct
import sys
import time
from urllib.parse import urlsplit

from _workspace_entry import WorkspaceHost
from test_workspace_details import _seed_canonical_review_and_decision
from test_workspace_entry import _free_port


def test_native_page_zoom_200_and_400_in_ordinary_workspace(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    directory = Path(os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR", str(tmp_path / "evidence")))
    directory.mkdir(parents=True, exist_ok=True)
    report = {"phase": "preparing", "mechanism": "native settings page zoom; fresh persistent normal profile",
              "zoom": {}, "failures": [], "forbidden_requests": [],
              "limits": ["actual assistive-technology announcements unverified", "native mobile keyboards unverified"]}
    host = WorkspaceHost(str(tmp_path / "runs"), "native-zoom-run", _free_port(), mode="demo-create")
    host.start()
    context = None
    failures = report["failures"]
    def require(value, reason):
        if not value:
            failures.append(reason)
    def boundary():
        assert not report["forbidden_requests"], "non-owned browser request refused"
    try:
        _seed_canonical_review_and_decision(host)
        artifact_text = "Synthetic native zoom artifact: " + "W" * 120
        artifact = host.demo.source(artifact_text.encode())
        reference = host.demo.evidence(artifact, "main-1", "native-zoom-artifact", "artifact")
        host.install_canonical_workspace_details(body_source_keys=["workspace_evidence:" + reference["id"]])
        driver = sync_playwright().start()
        try:
            report["phase"] = "launching full Chromium"
            executable = os.environ["SUMMON_UI_CHROMIUM_EXECUTABLE"]
            context = driver.chromium.launch_persistent_context(
                str(tmp_path / "owned-profile"), executable_path=executable, channel="chromium",
                headless=True, no_viewport=True, service_workers="block", timeout=25000,
                args=["--window-size=1280,1024", "--disable-background-networking", "--disable-component-update",
                      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
            context.set_default_timeout(5000)
            origin = urlsplit(host.surface.url)
            allowed_internal = {("chrome", "settings"), ("chrome", "resources"), ("chrome", "theme")}
            def bounded_route(route):
                target = urlsplit(route.request.url)
                if ((target.scheme, target.hostname, target.port) == (origin.scheme, origin.hostname, origin.port)
                        or (target.scheme, target.hostname) in allowed_internal):
                    route.continue_()
                else:
                    report["forbidden_requests"].append("non-owned-browser-request")
                    route.abort("blockedbyclient")
            context.route("**/*", bounded_route)
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            settings = context.pages[0] if context.pages else context.new_page()
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            version = cdp.send("Browser.getVersion")
            report["toolchain"] = {"product": version["product"], "protocol_version": version["protocolVersion"],
                                   "playwright": importlib.metadata.version("playwright"),
                                   "python": sys.version.split()[0], "platform": sys.platform,
                                   "headless": True, "persistent": True, "viewport_emulation": False}
            window = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {"windowId": window["windowId"],
                                                "bounds": {"width": 1280, "height": 1024, "windowState": "normal"}})
            fixed_bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
            report["native_window_bounds"] = fixed_bounds
            assert fixed_bounds["width"] == 1280 and fixed_bounds["height"] == 1024, "fixed native window required"

            report["phase"] = "opening native Page zoom setting"
            settings.goto("chrome://settings/appearance", wait_until="domcontentloaded", timeout=10000)
            zoom_control = settings.locator("settings-appearance-page #zoomLevel")
            zoom_control.wait_for(state="visible", timeout=10000)
            boundary()
            available = zoom_control.locator("option").evaluate_all("rows=>rows.map(row=>Number(row.value))")
            report["native_factors_available"] = {str(value): value in available for value in (1, 2, 4)}
            assert all(value in available for value in (1, 2, 4)), "required native zoom options unavailable"

            page.goto(host.surface.url, wait_until="domcontentloaded")
            page.locator("#code").fill(host.surface.bootstrap_code)
            page.locator("#login button").press("Enter")
            expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
            page.locator("#tasks button").first.press("Enter")
            expect(page.locator("#tasks button").first).to_have_attribute("aria-current", "true")
            expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
            page.locator("#message-text").fill("Synthetic context for native zoom measurement")
            page.locator(".goal summary").press("Enter")

            def capture_native(name):
                expect(page.locator("#code")).to_be_hidden()
                response = cdp.send("Page.captureScreenshot", {
                    "format": "png", "fromSurface": True,
                    "captureBeyondViewport": False,
                })
                raw = base64.b64decode(response["data"], validate=True)
                assert raw[:8] == b"\x89PNG\r\n\x1a\n"
                (directory / name).write_bytes(raw)
                return list(struct.unpack_from(">II", raw, 16))

            native_control_geometry = """element => {
                const r=element.getBoundingClientRect(), v=window.visualViewport;
                return {rect:{left:r.left,top:r.top,right:r.right,bottom:r.bottom,
                              width:r.width,height:r.height},
                        viewport:{left:v.offsetLeft,top:v.offsetTop,width:v.width,height:v.height,scale:v.scale},
                        innerWidth,innerHeight,scrollX,scrollY,
                        focused:document.activeElement===element};
            }"""
            baseline_geometry = baseline_image = baseline_artifact = None
            for factor in (1, 2, 4):
                label = str(factor)
                report["phase"] = "native zoom " + label
                result = report["zoom"][label] = {}
                zoom_control.select_option(str(factor))
                readback = settings.evaluate("() => chrome.settingsPrivate.getDefaultZoom()")
                result["native_setting_readback"] = readback
                assert abs(readback - factor) < .001, "native setting readback mismatch"
                boundary()
                deadline = time.monotonic() + 5
                while True:
                    metrics = cdp.send("Page.getLayoutMetrics")
                    zoom = metrics.get("cssVisualViewport", {}).get("zoom")
                    if isinstance(zoom, (int, float)) and abs(zoom - factor) < .001:
                        break
                    if time.monotonic() >= deadline:
                        result["target_zoom"] = zoom
                        raise AssertionError("target native page zoom proof unavailable")
                    page.wait_for_timeout(40)
                result["target_zoom"] = zoom
                result["target_scale"] = metrics["cssVisualViewport"]["scale"]
                assert abs(result["target_scale"] - 1) < .001, "pinch scale must remain one"
                bounds = cdp.send("Browser.getWindowForTarget")["bounds"]
                result["window_bounds"] = bounds
                assert bounds == fixed_bounds, "native window bounds changed"

                geometry = page.evaluate(r"""() => {
                  const root=document.documentElement;
                  return {width:innerWidth,height:innerHeight,document_width:root.scrollWidth,
                    dpr:devicePixelRatio,visual_scale:visualViewport.scale,
                    font_size:getComputedStyle(document.querySelector('h1')).fontSize,
                    heading_css_height:document.querySelector('h1').getBoundingClientRect().height,
                    horizontal_scrollers:[...document.querySelectorAll('body *')].filter(e=>
                      !e.closest('[hidden]')&&e.checkVisibility()&&e.clientWidth>0&&e.scrollWidth>e.clientWidth+1&&
                      ['auto','scroll'].includes(getComputedStyle(e).overflowX)).map(e=>({tag:e.tagName.toLowerCase(),id:e.id}))};
                }""")
                result["geometry"] = geometry
                assert abs(geometry["visual_scale"] - 1) < .001, "DOM pinch scale must remain one"
                if factor == 1:
                    baseline_geometry = geometry
                else:
                    require(abs(geometry["width"] - baseline_geometry["width"] / factor) <= 20,
                            label + ":native-css-width-ratio")
                require(geometry["document_width"] <= geometry["width"] + 1, label + ":page-horizontal-overflow")
                require(not geometry["horizontal_scrollers"], label + ":horizontal-content-scroll-required")

                # Native focus and scrolling; no viewport or style substitution.
                result["reachable_controls"] = []
                for selector in ("#tasks button", ".goal summary", "#evidence-open", "#details-open",
                                 "#message-target", "#message-text", "#message-send", ".composer summary"):
                    for element in page.locator(selector).all():
                        if element.is_disabled():
                            continue
                        before = element.evaluate(native_control_geometry)
                        element.focus()
                        element.scroll_into_view_if_needed()
                        after = element.evaluate(native_control_geometry)
                        box = element.bounding_box()
                        rect, visible = after["rect"], after["viewport"]
                        reachable = bool(rect["width"] > 0 and rect["height"] > 0
                                         and rect["left"] >= visible["left"] - 1
                                         and rect["top"] >= visible["top"] - 1
                                         and rect["right"] <= visible["left"] + visible["width"] + 1
                                         and rect["bottom"] <= visible["top"] + visible["height"] + 1)
                        result["reachable_controls"].append({"selector": selector, "reachable": reachable,
                                                             "before": before, "after": after,
                                                             "playwright_box_diagnostic": box})
                        require(reachable, label + ":unreachable-control:" + selector)
                page.locator(".goal summary").focus()
                page.locator(".goal summary").scroll_into_view_if_needed()
                dimensions = capture_native("native-zoom-" + label + "-workspace.png")
                result["screenshot_pixels"] = dimensions
                if factor == 1:
                    baseline_image = dimensions
                else:
                    require(dimensions == baseline_image, label + ":physical-screenshot-dimensions-changed")

                page.locator("#details-open").press("Enter")
                expect(page.locator("#details-close")).to_be_focused()
                page.get_by_role("button", name="Workspace assessment / workspace assessment", exact=True).press("Enter")
                expect(page.locator("#details-body")).to_contain_text("Disposition: continue_main")
                require(page.locator("#details-body").get_by_role("button", name="Open separately authorized body").count() == 0,
                        label + ":metadata-without-body-scope")
                index = page.evaluate("current.details.targets.filter(x=>x.task_id===current.selected_task_id&&x.source_kind==='workspace_evidence').findIndex(x=>x.body_available)")
                assert index >= 0, "focal separately authorized artifact required"
                page.get_by_role("button", name="Workspace evidence / workspace evidence", exact=True).nth(index).press("Enter")
                page.get_by_role("button", name="Open separately authorized body", exact=True).press("Enter")
                expect(page.locator("pre[data-detail-body]")).to_have_text(artifact_text)
                drawer = page.locator("#details-drawer").evaluate("e=>({client:e.clientWidth,scroll:e.scrollWidth,body_client:e.querySelector('pre').clientWidth,body_scroll:e.querySelector('pre').scrollWidth})")
                result["drawer"] = drawer
                require(drawer["scroll"] <= drawer["client"] + 1 and drawer["body_scroll"] <= drawer["body_client"] + 1,
                        label + ":drawer-horizontal-overflow")
                page.locator("pre[data-detail-body]").scroll_into_view_if_needed()
                artifact_dimensions = capture_native("native-zoom-" + label + "-artifact.png")
                result["artifact_screenshot_pixels"] = artifact_dimensions
                if factor == 1:
                    baseline_artifact = artifact_dimensions
                else:
                    require(artifact_dimensions == baseline_artifact,
                            label + ":artifact-physical-screenshot-dimensions-changed")
                page.locator("#details-close").focus()
                page.keyboard.press("Shift+Tab")
                require(page.locator("#details-drawer").evaluate("e=>e.contains(document.activeElement)"), label + ":drawer-tab-containment")
                page.keyboard.press("Tab")
                require(page.locator("#details-close").evaluate("e=>e===document.activeElement"), label + ":drawer-tab-wrap")
                page.keyboard.press("Escape")
                require(page.locator("#details-open").evaluate("e=>e===document.activeElement"), label + ":drawer-focus-return")
                require(page.locator("#details-drawer").is_hidden() and page.locator("#scrim").is_hidden()
                        and not page.locator("#app").evaluate("e=>e.inert"), label + ":drawer-cleanup")
                boundary()
            assert host.demo.total_workers == 0
            report["phase"] = "completed native measurements"
            assert failures == [], json.dumps(failures)
        finally:
            try:
                if context is not None:
                    context.close()
                    context = None
            finally:
                driver.stop()
    finally:
        (directory / "native-zoom-measurements.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        host.stop()
