"""Bounded desktop Chromium measurements against the ordinary synthetic host.

Reduced CSS viewports are zoom-equivalent layout checks, not browser zoom or
native-device evidence. These checks do not certify WCAG, screen-reader speech,
burst announcement coalescing, real mobile keyboards, or provider execution.
Tooling must already be installed; missing Chromium/Playwright fails, not skips.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
import os
from pathlib import Path
import struct
import sys
from urllib.parse import urlsplit
import zlib

import pytest

from _workspace_entry import WorkspaceHost
from test_workspace_details import _seed_canonical_review_and_decision
from test_workspace_entry import _free_port


# Read computed paint properties; never modify page styles or application state.
# Compositing supports the page's opaque surfaces and transparent descendants.
_PAINT = r"""({root, kind}) => {
 const rgba=s=>{const a=s.match(/[\d.]+/g).map(Number);return [a[0],a[1],a[2],a[3]??1]};
 const over=(a,b)=>{const alpha=a[3]+b[3]*(1-a[3]);return alpha?
   [0,1,2].map(i=>(a[i]*a[3]+b[i]*b[3]*(1-a[3]))/alpha).concat(alpha):[0,0,0,0]};
 const bg=e=>{let v=[0,0,0,0];for(let p=e;p;p=p.parentElement){v=over(v,rgba(getComputedStyle(p).backgroundColor));if(v[3]===1)break}return over(v,[255,255,255,1])};
 const lum=c=>c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
 const ratio=(a,b)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
 const visible=e=>!e.closest('[hidden],[inert]')&&e.getClientRects().length&&getComputedStyle(e).visibility==='visible';
 const base=document.querySelector(root), elements=kind==='text'?[base,...base.querySelectorAll('*')]:[base];
 return elements.filter(visible).filter(e=>kind!=='text'||
   [...e.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim())).map(e=>{
   const s=getComputedStyle(e),background=bg(e),adjacent=bg(e.parentElement),disabled=!!e.closest(':disabled');
   const result={tag:e.tagName.toLowerCase(),id:e.id,disabled,opacity:Number(s.opacity),
     font_size:parseFloat(s.fontSize),font_weight:Number(s.fontWeight),
     foreground:rgba(s.color),background,adjacent,text_ratio:ratio(over(rgba(s.color),background),background)};
   if(kind==='control'){result.border_width=parseFloat(s.borderTopWidth);result.border_color=rgba(s.borderTopColor);
     result.border_inside_ratio=ratio(over(result.border_color,background),background);
     result.border_outside_ratio=ratio(over(result.border_color,adjacent),adjacent)}
   if(kind==='focus'){result.focus_visible=e.matches(':focus-visible');result.outline_style=s.outlineStyle;
     result.outline_width=parseFloat(s.outlineWidth);result.outline_offset=parseFloat(s.outlineOffset);
     result.outline_color=rgba(s.outlineColor);result.focus_ratio=ratio(over(result.outline_color,adjacent),adjacent)}
   return result;
 });
}"""


def _png_pixels(raw):
    """Decode Chromium's 8-bit RGB/RGBA PNGs using only the standard library."""
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed = 8, bytearray()
    while offset < len(raw):
        size = struct.unpack_from(">I", raw, offset)[0]
        kind, data = raw[offset + 4:offset + 8], raw[offset + 8:offset + 8 + size]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data)
            assert depth == 8 and color in (2, 6) and interlace == 0
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(data)
        offset += size + 12
    stream, stride = zlib.decompress(compressed), width * channels
    rows, previous, offset = [], bytearray(stride), 0
    for _ in range(height):
        mode = stream[offset]
        row = bytearray(stream[offset + 1:offset + 1 + stride])
        assert mode in (0, 1, 2, 3, 4)
        for x in range(stride):
            left, up = row[x - channels] if x >= channels else 0, previous[x]
            corner = previous[x - channels] if x >= channels else 0
            if mode == 1:
                row[x] = (row[x] + left) & 255
            elif mode == 2:
                row[x] = (row[x] + up) & 255
            elif mode == 3:
                row[x] = (row[x] + (left + up) // 2) & 255
            elif mode == 4:
                estimate = left + up - corner
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
                predictor = (left, up, corner)[distances.index(min(distances))]
                row[x] = (row[x] + predictor) & 255
        rows.append(row)
        previous, offset = row, offset + stride + 1
    return width, height, lambda x, y: list(rows[y][x * channels:x * channels + 3])


def _contrast(foreground, background):
    def luminance(color):
        normalized = [value / 255 for value in color[:3]]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                  for value in normalized]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    values = [luminance(foreground), luminance(background)]
    return (max(values) + .05) / (min(values) + .05)


@pytest.fixture
def accessibility_host(tmp_path):
    from playwright.sync_api import sync_playwright

    host = WorkspaceHost(str(tmp_path / "runs"), "accessibility-run", _free_port(), mode="demo-create")
    host.start()
    directory = Path(os.environ.get("SUMMON_UI_RENDERED_EVIDENCE_DIR", str(tmp_path / "evidence")))
    directory.mkdir(parents=True, exist_ok=True)
    report = {"scope": "synthetic ordinary host; desktop headless Chromium",
              "text": {}, "controls": {}, "focus": {}, "layouts": {},
              "unverified": ["actual browser 200%/400% zoom", "assistive-technology announcements", "burst announcement coalescing",
                             "real mobile keyboard", "native-device rendering", "full WCAG conformance"]}
    forbidden = []
    try:
        with sync_playwright() as driver:
            options = {"headless": True, "args": [
                "--disable-background-networking", "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]}
            executable = os.environ.get("SUMMON_UI_CHROMIUM_EXECUTABLE")
            if executable:
                options["executable_path"] = executable
            browser = driver.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1280, "height": 1024},
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
            # Keep rendered assertions bounded while allowing Windows
            # loopback scheduling jitter during the multi-viewport sequence.
            page.set_default_timeout(15000)
            report["toolchain"] = {"browser": "Chromium", "browser_version": browser.version,
                                   "playwright_version": importlib.metadata.version("playwright"),
                                   "python_version": sys.version.split()[0], "platform": sys.platform,
                                   "headless": True, "network": "owned-loopback-only"}
            try:
                yield host, page, directory, report
            finally:
                report["forbidden_requests"] = forbidden
                (directory / "measurements.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                context.close()
                browser.close()
            assert forbidden == []
    finally:
        host.stop()


def test_workspace_contrast_reflow_focus_and_reduced_motion(accessibility_host):
    from playwright.sync_api import expect

    host, page, directory, report = accessibility_host
    failures = []
    report["failures"] = failures
    def require(value, category):
        if not value:
            failures.append(category)
    def capture(name):
        page.screenshot(path=str(directory / (name + ".png")), mask=[page.locator("#code")])
    def paint_text(name, root):
        rows = page.evaluate(_PAINT, {"root": root, "kind": "text"})
        report["text"][name] = rows
        require(bool(rows), name + ":text-samples-required")
        for row in rows:
            if row["disabled"]:
                continue  # Inactive controls are excluded from the contrast requirement.
            large = row["font_size"] >= 24 or (row["font_size"] >= 18.6667 and row["font_weight"] >= 700)
            require(row["text_ratio"] >= (3 if large else 4.5), name + ":text:" + row["tag"] + ":" + row["id"])
    def paint_control(selector):
        row, = page.evaluate(_PAINT, {"root": selector, "kind": "control"})
        report["controls"][selector] = row
        require(row["border_width"] > 0 and min(row["border_inside_ratio"], row["border_outside_ratio"]) >= 3,
                "control-boundary:" + selector)
    def paint_focus(selector):
        # Keyboard modality plus native focus; no CSS or page-state substitution.
        page.keyboard.press("Tab")
        page.locator(selector).focus()
        row, = page.evaluate(_PAINT, {"root": selector, "kind": "focus"})
        report["focus"][selector] = row
        if row["outline_style"] == "auto":
            # Chromium paints a two-color native focus ring; computed
            # outline-color alone is not its rendered contrast. Its dark outer
            # stroke can sit outside the box while the visible white stroke
            # occupies its first two pixel rows. Sample both sides of that
            # boundary, away from the text; retain the same 3:1 requirement.
            box = page.locator(selector).bounding_box()
            assert box is not None
            row["native_focus_box"] = box
            image_width, image_height, pixel = _png_pixels(page.screenshot())
            samples = []
            for fraction in (.25, .5, .75):
                x = math.floor(box["x"] + box["width"] * fraction)
                for y in (math.floor(box["y"]) - 2, math.floor(box["y"]) - 1,
                          math.floor(box["y"]), math.floor(box["y"]) + 1,
                          math.ceil(box["y"] + box["height"]) - 2, math.ceil(box["y"] + box["height"]) - 1,
                          math.ceil(box["y"] + box["height"]), math.ceil(box["y"] + box["height"]) + 1):
                    if 0 <= x < image_width and 0 <= y < image_height:
                        color = pixel(x, y)
                        samples.append({"x": x, "y": y, "color": color, "ratio": _contrast(color, row["adjacent"])})
            assert samples, "native ring pixels must be measured"
            row["native_ring_samples"] = samples
            row["painted_focus_ratio"] = max(sample["ratio"] for sample in samples)
        else:
            row["painted_focus_ratio"] = row["focus_ratio"]
        require(row["focus_visible"] and row["outline_style"] != "none" and row["outline_width"] > 0
                and row["painted_focus_ratio"] >= 3, "focus-indicator:" + selector)

    _seed_canonical_review_and_decision(host)
    artifact = host.demo.source(("Synthetic reflow specimen: " + "W" * 160).encode())
    reference = host.demo.evidence(artifact, "main-1", "accessibility-artifact", "artifact")
    host.install_canonical_workspace_details(body_source_keys=["workspace_evidence:" + reference["id"]])
    page.goto(host.surface.url, wait_until="domcontentloaded")
    paint_text("authentication", "body")
    paint_control("#code")
    paint_focus("#code")
    capture("authentication-focus")
    page.locator("#code").fill(host.surface.bootstrap_code)
    page.locator("#login button").press("Enter")
    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
    page.locator("#tasks button").first.press("Enter")
    expect(page.locator("#tasks button").first).to_have_attribute("aria-current", "true")
    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
    page.locator("#message-text").fill("Synthetic context for layout measurement only")
    paint_text("workspace", "body")
    paint_control("#message-target")
    paint_control("#message-text")
    for selector in ("#message-text", ".goal summary", "#timeline", ".composer summary", "#details-open"):
        paint_focus(selector)
    capture("desktop-workspace-focus")

    # Attempt ordinary browser zoom shortcuts. Headless shell may ignore them;
    # the observation is recorded without converting viewport emulation into zoom.
    def zoom_geometry():
        return page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,visual_scale:visualViewport.scale})")
    page.locator("#details-open").focus()
    report["browser_zoom"] = {"before": zoom_geometry()}
    for _ in range(5):
        page.keyboard.press("Control+=")
    report["browser_zoom"]["after_five_increase_shortcuts"] = zoom_geometry()
    report["browser_zoom"]["changed"] = report["browser_zoom"]["before"] != report["browser_zoom"]["after_five_increase_shortcuts"]
    page.keyboard.press("Control+0")
    report["browser_zoom"]["after_reset_shortcut"] = zoom_geometry()

    # 640x512 and 320x256 have the CSS dimensions of 1280x1024 at 200%/400%.
    # They establish reflow only; they do not enlarge browser chrome or glyphs.
    for name, width, height in (("desktop", 1280, 1024), ("200-percent-equivalent", 640, 512),
                                ("400-percent-equivalent", 320, 256), ("320-css-pixel-reflow", 320, 800)):
        page.set_viewport_size({"width": width, "height": height})
        if page.locator(".goal details").get_attribute("open") is None:
            page.locator(".goal summary").press("Enter")
        geometry = page.evaluate(r"""() => {
          const root=document.documentElement;
          const horizontal=[...document.querySelectorAll('body *')].filter(e=>
            !e.closest('[hidden]')&&e.clientWidth>0&&e.scrollWidth>e.clientWidth+1&&
            ['auto','scroll'].includes(getComputedStyle(e).overflowX)).map(e=>({tag:e.tagName.toLowerCase(),id:e.id,
              client:e.clientWidth,scroll:e.scrollWidth}));
          const timeline=document.getElementById('timeline').getBoundingClientRect();
          const notice=[...document.querySelector('.notice').children].filter(e=>!e.hidden)
            .map(e=>e.getBoundingClientRect().bottom);
          return {width:innerWidth,height:innerHeight,document_width:root.scrollWidth,
            horizontal_scrollers:horizontal,notice_overlaps_timeline:Math.max(...notice)>timeline.top+1};
        }""")
        report["layouts"][name] = geometry
        require(geometry["document_width"] <= width + 1, name + ":page-horizontal-overflow")
        require(not geometry["horizontal_scrollers"], name + ":horizontal-content-scroll-required")
        require(not geometry["notice_overlaps_timeline"], name + ":notice-overlaps-timeline")
        capture(name + "-workspace")
        # Native focus and scrolling must expose each enabled form/navigation
        # control within the CSS viewport; vertical page/feed scrolling is allowed.
        reachable = []
        for selector in ("#tasks button", "#evidence-open", "#details-open", ".goal summary",
                         "#message-target", "#message-text", "#message-send", ".composer summary"):
            for element in page.locator(selector).all():
                if element.is_disabled():
                    continue
                element.focus()
                element.scroll_into_view_if_needed()
                box = element.bounding_box()
                good = bool(box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1
                            and box["y"] >= -1 and box["y"] + box["height"] <= height + 1)
                reachable.append({"selector": selector, "in_viewport": good})
                require(good, name + ":control-not-fully-reachable:" + selector)
        geometry["controls"] = reachable

        page.locator("#details-open").press("Enter")
        expect(page.locator("#details-close")).to_be_focused()
        target_index = page.evaluate("current.details.targets.filter(x=>x.task_id===current.selected_task_id&&x.source_kind==='workspace_evidence').findIndex(x=>x.body_available)")
        assert target_index >= 0, "separate canonical body scope is required"
        page.get_by_role("button", name="Workspace evidence / workspace evidence", exact=True).nth(target_index).press("Enter")
        page.get_by_role("button", name="Open separately authorized body", exact=True).press("Enter")
        page.locator("pre[data-detail-body]").wait_for()
        paint_text(name + "-drawer", "#details-drawer")
        paint_focus("#details-close")
        drawer = page.locator("#details-drawer").evaluate("e=>({client:e.clientWidth,scroll:e.scrollWidth,body_client:e.querySelector('pre').clientWidth,body_scroll:e.querySelector('pre').scrollWidth})")
        geometry["drawer"] = drawer
        require(drawer["scroll"] <= drawer["client"] + 1 and drawer["body_scroll"] <= drawer["body_client"] + 1,
                name + ":drawer-horizontal-overflow")
        page.keyboard.press("Shift+Tab")
        require(page.locator("#details-drawer").evaluate("e=>e.contains(document.activeElement)"), name + ":reverse-tab-escapes-drawer")
        page.keyboard.press("Tab")
        require(page.locator("#details-close").evaluate("e=>e===document.activeElement"), name + ":forward-tab-does-not-wrap")
        capture(name + "-drawer")
        page.keyboard.press("Escape")
        require(page.locator("#details-open").evaluate("e=>e===document.activeElement"), name + ":escape-focus-return")
        require(page.locator("#details-drawer").is_hidden() and page.locator("#scrim").is_hidden()
                and not page.locator("#app").evaluate("e=>e.inert"), name + ":escape-overlay-cleanup")

    page.set_viewport_size({"width": 1280, "height": 1024})
    page.emulate_media(reduced_motion="reduce")
    reduced = page.evaluate(r"""() => ({requested:matchMedia('(prefers-reduced-motion: reduce)').matches,
      animations:document.getAnimations().length,violations:[...document.querySelectorAll('*')].filter(e=>{
        const s=getComputedStyle(e);return s.scrollBehavior!=='auto'||s.animationName!=='none'||
          s.transitionDuration.split(',').some(v=>parseFloat(v)!==0)
      }).map(e=>({tag:e.tagName.toLowerCase(),id:e.id}))})""")
    report["reduced_motion"] = reduced
    require(reduced["requested"] and reduced["animations"] == 0 and not reduced["violations"], "reduced-motion-not-honored")
    page.locator("#skip").focus()
    page.locator("#skip").press("Enter")
    require(page.locator("#timeline").evaluate("e=>e===document.activeElement"), "skip-link-focus")
    paint_focus("#timeline")
    capture("reduced-motion-timeline-focus")
    report["live_regions"] = page.locator("[role=status],[role=alert],[aria-live]").evaluate_all(
        "els=>els.map(e=>({id:e.id,role:e.getAttribute('role'),live:e.getAttribute('aria-live')}))")
    assert host.demo.total_workers == 0
    assert failures == [], json.dumps(failures)
