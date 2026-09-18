# U07 native browser zoom acceptance

Scoped acceptance, 2026-09-12. Actual browser page zoom is independently
verified for the ordinary synthetic workspace journey. U07 remains open for
assistive-technology announcements and native mobile-keyboard evidence. This
record does not claim complete WCAG conformance or release readiness.

Lead update, 2026-09-13: the exact zoom test is now present in the fixed
workspace/accessibility commands and CI artifact allowlist. The current
provider-free UI run passed 327/327 and the rendered collector matched all 44
required files. The retention work below is historical; actual
assistive-technology and native mobile-keyboard qualification remains open.

## Executed evidence

The lead ran the corrected private `test_workspace_zoom_rendered.py` against
a fresh copy of the current source: one case passed in 13.762 seconds, with
an outer duration of 14.283 seconds. The 268 copied existing Python files
still matched the shared source after execution. Exact executed test and guard
bytes, measurements, screenshots and the earlier failed packet are retained
privately. The conductor owns test integration and fixed-command/CI retention.

Toolchain: full Chromium 149.0.7827.55, Playwright 1.52.0, Python 3.13.11,
Windows, headless. The installed Playwright/browser revision mismatch is
explicit; no dependency or browser was installed. One fresh owned persistent
profile used native Chromium settings, without viewport or pinch emulation.

| Measurement | Baseline | 200% | 400% |
| --- | --- | --- | --- |
| Native setting and target page zoom | 1 | 2 | 4 |
| Pinch scale | 1 | 1 | 1 |
| Native window dimensions | 1280 by 1024 | unchanged | unchanged |
| Full workspace and artifact PNG dimensions | 1258 by 870 | unchanged | unchanged |
| Reachable checked enabled controls | 10 of 10 | 10 of 10 | 10 of 10 |
| Horizontal content scrollers | none | none | none |

The same journey verifies canonical workspace assessment inspection, refusal
of body access without separate scope, authorized focal artifact content,
drawer wrapping, keyboard containment, Escape/opener return and inert/scrim
cleanup. The lead inspected baseline, 200% and 400% workspace images and the
400% artifact image. Vertical scrolling remains available and expected.

No workers ran. The Python guard recorded zero process/network-boundary
violations; browser requests were restricted to the owned loopback host and
finite built-in settings resources. The protocol record contains zero forbidden
emulation calls. One admitted Playwright driver and its browser descendants
were confined to an owned kill-on-close job, which was closed after completion.
The synthetic home remained empty and no source bytecode was created. These
controls do not claim an operating-system network sandbox.

## Correction to the failed measurement

The prior helper run established native zoom but failed two screenshot-size
assertions and one textarea check. Its evidence remains preserved. Playwright's
clipped screenshot path did not account for native page zoom. The corrected
probe uses native `Page.captureScreenshot` without a clip, after verifying the
authentication code field is hidden. All six captures retain their full size.

Control and visible-viewport bounds now come from one native JavaScript read;
the original one-CSS-pixel tolerance is unchanged. At 400%, the textarea's
bottom is 218.402 CSS pixels and the fractional visible height is 217.5, placing
it within that tolerance after native focus/scrolling. The previous comparison
used rounded `innerHeight` 217 and therefore failed. The measured Playwright box
agrees with the native control rectangle; no blanket coordinate-scale mismatch
is claimed. No product code or acceptance threshold was changed.

## Remaining retention and qualification

The original retention request below is retained as audit history. Its fixed
command and finite artifact-list portion is now integrated; only the actual
device/assistive-technology qualification remains unverified.

Integrate the exact new test with an explicit full-Chromium executable or a
reviewed installed-toolchain fallback. Retain it in the applicable fixed
workspace/accessibility commands and CI, and bind its bounded measurement/PNG
outputs through the existing release-evidence mechanism. Any required wiring
change must be reviewed before treating integration as accepted.

Actual screen-reader announcements, notification coalescing as heard through
assistive technology, and native mobile-keyboard interaction remain unverified.
Accessibility-tree inspection, emulated devices and native desktop zoom cannot
substitute for those checks. The authoritative requirement count is unchanged.
