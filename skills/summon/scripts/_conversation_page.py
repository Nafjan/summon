"""Static, dependency-free conversation atlas page.

The page is deliberately a small messenger rather than a dashboard.  It owns
only presentation and cursor reconciliation; the Python journal and the
authenticated loopback handlers remain the authority boundary.
"""

from __future__ import annotations


_CSS = r"""
:root {
  color-scheme: dark;
  --ink: #0d1015;
  --rail: #11151b;
  --panel: #171c23;
  --panel-2: #1d242d;
  --panel-3: #252e39;
  --line: #34404d;
  --line-soft: #27313b;
  --text: #f3f6f7;
  --muted: #b5c0ca;
  --quiet: #8f9eaa;
  --cyan: #8ad7ff;
  --cyan-deep: #17384a;
  --amber: #f2c477;
  --amber-deep: #3b2d1b;
  --green: #9ed9ba;
  --red: #ffaaa4;
  --focus: #d2f1ff;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; background: var(--ink); color: var(--text); }
body { font: 14px/1.48 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; letter-spacing: -.006em; }
button, input, textarea, select { font: inherit; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; }
a { color: inherit; }
.skip { position: fixed; z-index: 20; top: -60px; left: 14px; padding: 10px 13px; border-radius: 8px; background: var(--cyan); color: #071018; font-weight: 750; }
.skip:focus { top: 14px; }
.atlas { display: grid; grid-template-columns: 286px minmax(0, 1fr) 278px; min-height: 100vh; }
.rail { min-width: 0; padding: 22px 16px 26px; border-right: 1px solid var(--line-soft); background: var(--rail); }
.brand { display: flex; align-items: center; gap: 10px; margin: 2px 5px 25px; font-size: 13px; font-weight: 800; letter-spacing: .18em; }
.brand-mark { display: grid; place-items: center; width: 30px; height: 30px; border: 1px solid var(--cyan); border-radius: 8px; color: var(--cyan); letter-spacing: 0; }
.rail-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 5px 9px; }
.rail-heading h2 { margin: 0; font-size: 13px; font-weight: 730; }
.rail-heading span { color: var(--quiet); font-size: 11px; font-variant-numeric: tabular-nums; }
.search { width: 100%; min-height: 42px; margin-bottom: 15px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 9px; outline: none; background: #0e1319; color: var(--text); }
.search::placeholder { color: var(--quiet); }
.search:focus-visible, textarea:focus-visible, select:focus-visible { border-color: var(--cyan); outline: 2px solid var(--focus); outline-offset: 2px; }
.room-groups { display: grid; gap: 17px; }
.room-group { min-width: 0; }
.project-line { display: flex; align-items: baseline; gap: 7px; margin: 0 5px 7px; color: var(--muted); font-size: 12px; font-weight: 720; overflow-wrap: anywhere; }
.project-line small { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.initiator-line { margin: 0 5px 4px; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .03em; overflow-wrap: anywhere; }
.room-button { position: relative; display: block; width: 100%; min-height: 64px; margin: 3px 0; padding: 9px 10px 9px 12px; border: 1px solid transparent; border-radius: 10px; background: transparent; color: var(--muted); text-align: left; }
.room-button:hover, .room-button:focus-visible { border-color: var(--line); background: var(--panel); color: var(--text); outline: none; }
.room-button.active { border-color: #416073; background: var(--cyan-deep); color: var(--text); }
.room-button.active::before { content: ""; position: absolute; top: 12px; bottom: 12px; left: 0; width: 2px; border-radius: 2px; background: var(--cyan); }
.room-name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 720; }
.room-detail { display: flex; align-items: center; gap: 7px; margin-top: 4px; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.mode-chip { display: inline-flex; align-items: center; min-height: 20px; padding: 2px 6px; border: 1px solid #4b6373; border-radius: 5px; color: var(--cyan); font-size: 9px; letter-spacing: .09em; text-transform: uppercase; }
.unread { width: 6px; height: 6px; border-radius: 50%; background: var(--amber); }
.empty { margin: 0; padding: 20px 5px; color: var(--muted); }
.visually-hidden { position: absolute !important; width: 1px !important; height: 1px !important; padding: 0 !important; margin: -1px !important; overflow: hidden !important; clip: rect(0, 0, 0, 0) !important; white-space: nowrap !important; border: 0 !important; }
.thread { min-width: 0; display: flex; min-height: 100vh; flex-direction: column; }
.topbar { position: sticky; z-index: 4; top: 0; display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 78px; padding: 16px clamp(20px, 4vw, 50px); border-bottom: 1px solid var(--line-soft); background: rgba(13, 16, 21, .97); }
.room-title { min-width: 0; }
.room-title h1 { margin: 0; overflow-wrap: anywhere; font-size: clamp(20px, 2.2vw, 30px); font-weight: 710; letter-spacing: -.035em; line-height: 1.12; }
.room-subtitle { display: flex; flex-wrap: wrap; gap: 7px 12px; margin-top: 7px; color: var(--muted); font-size: 12px; }
.status { display: inline-flex; flex: none; align-items: center; gap: 7px; min-height: 30px; padding: 6px 10px; border: 1px solid #49634f; border-radius: 7px; background: #142119; color: var(--green); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }
.status::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.status.degraded { border-color: #71502e; background: #2a2117; color: var(--amber); }
.status.blocked { border-color: #72403c; background: #2b1819; color: var(--red); }
.status.offline { border-color: var(--line); background: var(--panel); color: var(--muted); }
.thread-body { width: min(850px, 100%); margin: 0 auto; padding: 20px clamp(20px, 4vw, 52px) 0; }
.room-bar { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 12px 0 15px; border-bottom: 1px solid var(--line-soft); }
.room-bar-label { min-width: 0; }
.room-bar-label strong { display: block; overflow-wrap: anywhere; font-size: 13px; }
.room-origin { display: block; margin-top: 3px; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.facts { display: flex; flex: none; gap: 13px; color: var(--muted); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; font-variant-numeric: tabular-nums; }
.facts b { color: var(--text); font-weight: 700; }
.participants { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 13px; }
.participant { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 5px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); color: var(--muted); font-size: 11px; }
.participant::before { content: ""; width: 6px; height: 6px; flex: none; border-radius: 50%; background: var(--cyan); }
.timeline { display: flex; flex-direction: column; gap: 13px; margin: 21px 0 0; padding: 0; list-style: none; }
.timeline-empty { padding: 55px 10px; border: 1px dashed var(--line); border-radius: 11px; color: var(--muted); text-align: center; }
.message { max-width: min(680px, 88%); }
.message.human { align-self: flex-end; }
.message.agent { align-self: flex-start; }
.message-head { display: flex; align-items: baseline; gap: 9px; margin: 0 8px 5px; color: var(--muted); font-size: 11px; }
.message.human .message-head { justify-content: flex-end; }
.message-head strong { color: var(--text); font-weight: 720; }
.message-head time { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.bubble { padding: 12px 14px; border: 1px solid #3b5260; border-radius: 13px 13px 13px 4px; background: var(--panel-2); color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
.message.human .bubble { border-color: #715a32; border-radius: 13px 13px 4px 13px; background: var(--amber-deep); color: #fff1d4; }
.message-meta { margin: 6px 8px 0; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.system-event { align-self: center; width: min(680px, 100%); }
.system-chip { display: flex; align-items: center; gap: 9px; min-height: 38px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 8px; background: #141a20; color: var(--muted); }
.system-chip .event-mark { display: grid; place-items: center; width: 21px; height: 21px; border: 1px solid #60717d; border-radius: 5px; color: var(--cyan); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.system-chip strong { color: var(--text); font-weight: 680; }
.system-chip small { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.system-chip details { margin-left: auto; }
.system-chip summary { cursor: pointer; color: var(--cyan); font-size: 11px; }
.evidence { margin-top: 7px; padding: 9px 10px; border-top: 1px solid var(--line-soft); color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.working { display: grid; gap: 7px; margin: 18px 0 0; }
.working-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 11px; border: 1px solid #426076; border-radius: 8px; background: #15232d; color: var(--muted); }
.working-row strong { color: var(--text); }
.working-row button { min-height: 34px; padding: 5px 9px; border: 1px solid #d98680; border-radius: 6px; background: transparent; color: #ffbeb7; font-size: 12px; }
.composer { position: relative; z-index: 3; margin: 20px 0 0; padding: 14px 0 20px; border-top: 1px solid var(--line-soft); background: rgba(13, 16, 21, .98); }
.composer-tabs { display: flex; gap: 6px; margin-bottom: 10px; }
.tab { min-height: 34px; padding: 6px 11px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); color: var(--muted); }
.tab[aria-selected="true"] { border-color: var(--cyan); background: var(--cyan-deep); color: var(--text); }
.tab.agent[aria-selected="true"] { border-color: var(--amber); background: var(--amber-deep); }
.compose-grid { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 9px; background: #10151b; color: var(--text); }
textarea { min-height: 76px; max-height: 190px; resize: vertical; padding: 10px 12px; }
select { min-height: 44px; padding: 8px 10px; }
.compose-actions { display: grid; gap: 7px; min-width: 118px; }
.primary { min-height: 44px; padding: 9px 13px; border: 1px solid var(--cyan); border-radius: 8px; background: var(--cyan); color: #071018; font-weight: 760; }
.primary.agent-action { border-color: var(--amber); background: var(--amber); }
.primary:disabled { border-color: #5b6873; background: #39434c; color: #c7d0d5; }
.composer-caption { display: flex; justify-content: space-between; gap: 10px; margin-top: 7px; color: var(--muted); font-size: 11px; }
.composer-caption span:last-child { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.composer-note { min-height: 20px; margin: 7px 0 0; color: var(--amber); font-size: 12px; }
.composer-note.error { color: var(--red); }
.drawer { min-width: 0; padding: 22px 17px; border-left: 1px solid var(--line-soft); background: var(--rail); }
.drawer h2 { margin: 0; font-size: 13px; font-weight: 730; }
.drawer-section { padding: 0 0 19px; margin-bottom: 19px; border-bottom: 1px solid var(--line-soft); }
.drawer-section:last-child { border-bottom: 0; }
.drawer-label { margin: 0 0 9px; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; text-transform: uppercase; }
.fact-list { display: grid; gap: 10px; margin: 0; }
.fact-list div { min-width: 0; }
.fact-list dt { color: var(--quiet); font-size: 11px; }
.fact-list dd { margin: 2px 0 0; color: var(--text); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.boundary { padding: 12px; border: 1px solid #6d5534; border-radius: 9px; background: #241d15; color: #ffe9bc; font-size: 12px; }
.boundary strong { display: block; margin-bottom: 4px; color: var(--amber); }
.drawer .muted { color: var(--muted); }
.mobile-rail { display: none; }
.drawer-toggle { display: none; }
@media (max-width: 1120px) and (min-width: 761px) { .atlas { grid-template-columns: 250px minmax(0, 1fr); grid-template-rows: auto auto; } .rail { grid-row: 1 / span 2; } .drawer { display: block; grid-column: 2; grid-row: 2; border-top: 1px solid var(--line-soft); border-left: 0; } .thread-body { width: min(870px, 100%); } }
@media (max-width: 760px) {
  .atlas { display: block; }
  .rail { display: none; position: fixed; z-index: 10; inset: 0 auto 0 0; width: min(318px, 88vw); box-shadow: 14px 0 30px #0009; }
  .rail.open { display: block; }
  .mobile-rail { display: inline-flex; align-items: center; justify-content: center; min-width: 44px; min-height: 44px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
  .topbar { padding: 11px 16px; }
  .topbar .status { font-size: 10px; }
  .thread-body { padding: 12px 16px 0; }
  .room-bar { display: block; }
  .facts { margin-top: 9px; }
  .message { max-width: 94%; }
  .compose-grid { grid-template-columns: 1fr; }
  .compose-actions { grid-template-columns: minmax(0, 1fr) auto; }
  .primary { min-width: 118px; }
  .composer-caption { align-items: flex-start; flex-direction: column; gap: 3px; }
  .composer { position: relative; }
  .drawer-toggle { display: inline-flex; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation: none !important; } }

/* Beautiful UI-inspired workbench layer: compact cards, precise hairlines, and
   component states that make the journal feel like a live instrument. This is
   an independent stdlib implementation; no external UI package is loaded. */
:root {
  --ink: #0a0d11;
  --rail: #0e1218;
  --panel: #141a21;
  --panel-2: #1a222c;
  --panel-3: #202a35;
  --line: #344252;
  --line-soft: #222c37;
  --text: #f3f7fa;
  --muted: #b8c5cf;
  --quiet: #91a1ad;
  --blue: #6bcaff;
  --blue-deep: #133447;
  --lime: #b7f36b;
  --lime-deep: #263b1b;
  --amber: #ffd27e;
  --amber-deep: #3b2b18;
  --violet: #c7b1ff;
  --violet-deep: #2b2340;
  --coral: #ff9b8d;
  --coral-deep: #3a1f24;
  --focus: #e8f8ff;
}
body { background: var(--ink); color: var(--text); font-size: 14px; letter-spacing: -.008em; }
.atlas { grid-template-columns: 272px minmax(0, 1fr) 312px; background: var(--ink); }
.rail { padding: 24px 16px 28px; border-right-color: var(--line-soft); background: var(--rail); }
.brand { align-items: center; gap: 10px; margin: 0 6px 29px; letter-spacing: .16em; }
.brand-mark { width: 34px; height: 34px; border: 0; border-radius: 11px; background: var(--blue); color: var(--ink); box-shadow: 0 0 0 3px #6bcaff18; font-weight: 900; }
.brand::after { content: "LOCAL WORKBENCH"; margin-left: auto; color: var(--quiet); font: 9px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; }
.rail-heading { margin-bottom: 11px; }
.rail-heading h2 { color: var(--text); font-size: 12px; letter-spacing: .01em; }
.rail-heading span { color: var(--blue); font-size: 10px; }
.search { min-height: 44px; margin-bottom: 18px; padding: 10px 12px; border-color: var(--line); border-radius: 10px; background: #0a0f14; }
.search:focus-visible, textarea:focus-visible, select:focus-visible { border-color: var(--blue); outline-color: var(--blue); }
.room-groups { gap: 22px; }
.project-line { margin-bottom: 8px; color: var(--text); font-size: 11px; letter-spacing: .02em; }
.project-line small { color: var(--blue); font-size: 9px; }
.initiator-line { margin: 0 7px 5px; color: var(--quiet); font-size: 9px; letter-spacing: .08em; text-transform: uppercase; }
.room-button { min-height: 72px; margin: 4px 0; padding: 11px 12px 11px 14px; border-color: transparent; border-radius: 12px; }
.room-button:hover, .room-button:focus-visible { border-color: var(--line); background: var(--panel); }
.room-button.active { border-color: #39637b; background: var(--blue-deep); box-shadow: inset 0 0 0 1px #6bcaff16; }
.room-button.active::before { width: 3px; top: 15px; bottom: 15px; background: var(--blue); }
.room-name { font-size: 13px; }
.room-detail { margin-top: 7px; gap: 8px; color: var(--quiet); font-size: 9px; }
.mode-chip { min-height: 21px; border-color: #426c82; border-radius: 6px; color: var(--blue); font-size: 9px; letter-spacing: .1em; }
.unread { width: 7px; height: 7px; background: var(--lime); box-shadow: 0 0 0 3px #b7f36b22; }
.topbar { min-height: 88px; padding: 17px clamp(22px, 4vw, 54px); border-bottom-color: var(--line); background: #0a0d11f5; }
.topbar::before { content: "ROOM / DURABLE JOURNAL"; position: absolute; top: 10px; left: clamp(22px, 4vw, 54px); color: var(--quiet); font: 9px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .16em; }
.room-title { padding-top: 10px; }
.room-title h1 { font-size: clamp(22px, 2.6vw, 34px); letter-spacing: -.045em; }
.room-subtitle { margin-top: 8px; color: var(--muted); font-size: 12px; }
.status { min-height: 34px; padding: 7px 11px; border-color: #4c6b59; border-radius: 9px; background: #16231b; color: var(--lime); font-size: 10px; letter-spacing: .03em; box-shadow: 0 0 0 3px #b7f36b0e; }
.status::before { width: 7px; height: 7px; }
.status.degraded { border-color: #735e34; background: #2a2117; color: var(--amber); }
.status.blocked { border-color: #78464a; background: var(--coral-deep); color: var(--coral); }
.status.offline { border-color: var(--line); background: var(--panel); color: var(--muted); }
.thread-body { width: min(930px, 100%); padding: 25px clamp(22px, 4vw, 58px) 0; }
.room-bar { padding: 0 0 18px; border-bottom-color: var(--line); }
.room-bar-label strong { font-size: 14px; letter-spacing: -.01em; }
.room-origin { margin-top: 5px; color: var(--blue); font-size: 9px; letter-spacing: .12em; }
.facts { gap: 8px; color: var(--quiet); font-size: 9px; text-transform: uppercase; }
.facts span { padding: 6px 8px; border: 1px solid var(--line-soft); border-radius: 7px; background: var(--panel); }
.facts b { color: var(--text); margin-left: 3px; }
.participants { gap: 7px; margin-top: 15px; }
.participant { padding: 6px 9px; border-color: var(--line); border-radius: 8px; background: var(--panel); color: var(--text); font-size: 10px; }
.participant::before { width: 7px; height: 7px; background: var(--lime); }
.timeline { position: relative; gap: 17px; margin-top: 25px; }
.timeline::before { content: ""; position: absolute; top: 4px; bottom: 4px; left: 17px; width: 1px; background: var(--line-soft); }
.timeline > li { position: relative; z-index: 1; }
.message { max-width: min(710px, 90%); }
.message-head { margin: 0 10px 6px; color: var(--muted); font-size: 10px; }
.message-head strong { font-size: 11px; }
.message-head time { color: var(--quiet); font-size: 9px; }
.bubble { padding: 14px 16px; border-color: #3a5668; border-radius: 14px 14px 14px 5px; background: var(--panel-2); box-shadow: 0 1px 0 #ffffff08; }
.message.human .bubble { border-color: #8a6535; border-radius: 14px 14px 5px 14px; background: var(--amber-deep); color: #fff2d6; }
.message-meta { margin-top: 7px; color: var(--quiet); font-size: 9px; }
.system-event { width: min(710px, 100%); }
.system-chip { min-height: 46px; padding: 9px 12px; border-color: var(--line); border-radius: 10px; background: var(--panel); box-shadow: 0 1px 0 #ffffff06; }
.system-chip .event-mark { width: 23px; height: 23px; border-color: #5b7b8f; border-radius: 7px; color: var(--blue); font-size: 9px; }
.system-chip strong { font-size: 12px; }
.system-chip small { color: var(--muted); font-size: 9px; }
.system-chip summary { color: var(--blue); font-size: 10px; }
.system-event.round .system-chip { border-left: 1px solid var(--blue); background: #152633; }
.system-event.position .system-chip { border-left: 1px solid var(--lime); background: #18251d; }
.system-event.cross .system-chip { border-left: 1px solid var(--amber); background: #2a2118; }
.system-event.synthesis .system-chip { border-left: 1px solid var(--violet); background: var(--violet-deep); }
.system-event.turn .system-chip { border-left: 1px solid var(--blue); background: #152633; }
.system-event.finish .system-chip { border-left: 1px solid var(--lime); background: #18251d; }
.system-event.round .event-mark, .system-event.turn .event-mark { color: var(--blue); border-color: #5b7b8f; }
.system-event.position .event-mark, .system-event.finish .event-mark { color: var(--lime); border-color: #6a8d5b; }
.system-event.cross .event-mark { color: var(--amber); border-color: #957645; }
.system-event.synthesis .event-mark { color: var(--violet); border-color: #8874ad; }
.evidence { border-top-color: var(--line); color: var(--quiet); font-size: 9px; }
.working { margin: 19px 0 0; }
.working-row { padding: 10px 12px; border-color: #42647a; border-radius: 10px; background: var(--blue-deep); }
.working-row button { min-height: 38px; border-color: var(--coral); border-radius: 8px; background: transparent; color: var(--coral); }
.composer { position: relative; margin-top: 24px; padding: 15px 0 18px; border-top-color: var(--line); background: var(--ink); box-shadow: none; }
.composer-tabs { gap: 7px; margin-bottom: 11px; }
.tab { min-height: 44px; padding: 8px 12px; border-color: var(--line); border-radius: 9px; background: var(--panel); color: var(--muted); font-size: 11px; }
.tab[aria-selected="true"] { border-color: var(--blue); background: var(--blue-deep); color: var(--text); }
.tab.agent[aria-selected="true"] { border-color: var(--amber); background: var(--amber-deep); }
textarea, select { border-color: var(--line); border-radius: 10px; background: #0d1319; }
textarea { min-height: 88px; padding: 12px 13px; }
select { min-height: 46px; }
.primary { min-height: 46px; border: 0; border-radius: 9px; background: var(--blue); color: var(--ink); box-shadow: 0 5px 16px #6bcaff1c; }
.primary.agent-action { background: var(--amber); }
.primary:disabled { border: 1px solid var(--line); background: var(--panel-3); color: var(--quiet); box-shadow: none; }
.composer-caption { margin-top: 8px; color: var(--muted); font-size: 10px; }
.composer-caption span:last-child { color: var(--blue); font-size: 9px; }
.composer-note { color: var(--amber); font-size: 11px; }
.drawer { padding: 25px 18px; border-left-color: var(--line); background: var(--rail); }
.drawer h2 { font-size: 14px; letter-spacing: -.01em; }
.drawer-section { padding-bottom: 21px; margin-bottom: 21px; border-bottom-color: var(--line-soft); }
.drawer-label { color: var(--blue); font-size: 9px; letter-spacing: .14em; }
.boundary { padding: 14px; border-color: #876c3a; border-radius: 11px; background: var(--amber-deep); color: #fff0c8; font-size: 11px; box-shadow: inset 0 0 0 1px #ffd27e0e; }
.boundary strong { color: var(--amber); font-size: 11px; }
.fact-list { gap: 12px; }
.fact-list dt { color: var(--quiet); font-size: 10px; }
.fact-list dd { color: var(--text); font-size: 10px; }
.drawer .muted { color: var(--muted); font-size: 11px; line-height: 1.6; }
@media (max-width: 1120px) and (min-width: 761px) { .atlas { grid-template-columns: 250px minmax(0, 1fr); } }
@media (max-width: 760px) {
  .topbar { min-height: 78px; padding: 12px 16px; }
  .topbar::before { top: 7px; left: 16px; font-size: 8px; }
  .room-title { padding-top: 8px; }
  .room-title h1 { font-size: 23px; }
  .status { min-height: 32px; padding: 6px 8px; }
  .thread-body { padding: 18px 16px 0; }
  .room-bar { padding-bottom: 16px; }
  .facts { margin-top: 12px; }
  .facts span { padding: 6px 7px; }
  .timeline { margin-top: 20px; }
  .message { max-width: 95%; }
  .composer { margin-top: 20px; padding-bottom: calc(18px + env(safe-area-inset-bottom)); }
  .compose-grid { gap: 8px; }
  .drawer { border-top-color: var(--line); }
}
"""


_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summon conversation atlas</title>
<style nonce="__NONCE__">__CSS__</style>
</head>
<body>
<!-- THESIS: A conversation is a chain of accountable handoffs, not a generic chat feed.
OWN-WORLD: A Beautiful UI-inspired precision workbench: obsidian surfaces, electric blue live state, lime activity, amber operator context, and evidence in the margins.
STORY: The operator sees who is speaking, what is durable, and what remains context versus authority.
FIRST VIEWPORT: Rooms on the left, one grouped thread in the center, and its redacted evidence rail on the right.
FORM: Operate / precision-workbench messenger; compact component cards and a cursor spine make the journal feel alive without turning prose into authority.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->
<a class="skip" href="#composer">Skip to composer</a>
<div class="atlas">
  <nav id="rail" class="rail" aria-label="Conversation rooms">
    <div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span>SUMMON</span></div>
    <div class="rail-heading"><h2>Conversation atlas</h2><span id="room-count">0</span></div>
    <input id="room-search" class="search" type="search" autocomplete="off" placeholder="Filter rooms" aria-label="Filter rooms">
    <div id="rooms" class="room-groups"><p class="empty">Loading rooms…</p></div>
  </nav>
  <main class="thread" aria-labelledby="title">
    <header class="topbar">
      <button id="rail-open" class="mobile-rail" type="button" aria-label="Open conversation rooms">Rooms</button>
      <div class="room-title"><h1 id="title">Choose a conversation</h1><div id="room-subtitle" class="room-subtitle">Rooms are grouped by project and initiating agent.</div></div>
      <div id="connection" class="status offline" role="status" aria-live="polite">Connecting</div>
    </header>
    <div class="thread-body">
      <section class="room-bar" aria-label="Current conversation">
        <div class="room-bar-label"><strong id="room-label">No room selected</strong><span id="room-origin" class="room-origin">LOCAL / REDACTED CONTEXT</span><div id="participants" class="participants"></div></div>
        <div class="facts"><span>cursor <b id="cursor">—</b></span><span>mode <b id="mode">—</b></span></div>
      </section>
      <div id="working" class="working" aria-live="polite"></div>
      <ol id="timeline" class="timeline" role="log" aria-label="Conversation timeline" aria-live="off"><li class="timeline-empty">Select a room to inspect its bounded event stream.</li></ol>
      <section id="composer" class="composer" aria-labelledby="composer-title">
        <div class="composer-tabs" role="tablist" aria-label="Conversation action">
          <button id="context-tab" class="tab" role="tab" aria-selected="true" type="button">Post context</button>
          <button id="agent-tab" class="tab agent" role="tab" aria-selected="false" type="button">Ask a roster agent</button>
        </div>
        <h2 id="composer-title" hidden>Conversation composer</h2>
        <div class="compose-grid">
          <div>
            <select id="participant" aria-label="Conversation participant" hidden><option value="">Choose an agent</option></select>
            <textarea id="message" maxlength="12000" placeholder="Add context for this room…"></textarea>
            <div class="composer-caption"><span id="caption">Saved as a human context event; never a control command.</span><span id="counter">0 / 12000</span></div>
          </div>
          <div class="compose-actions"><button id="send" class="primary" type="button" disabled>Post context</button><button id="run-cancel" type="button" hidden disabled>Cancel turn</button></div>
        </div>
        <p id="note" class="composer-note" role="status" aria-live="polite"></p>
      </section>
    </div>
  </main>
  <aside class="drawer" aria-label="Conversation evidence">
    <div class="drawer-section"><h2>Room evidence</h2><p class="drawer-label">public boundary</p><div class="boundary" data-authority="context + explicit turns"><strong>Context + explicit turns</strong>Human messages are context only. This surface cannot approve, vote, or change a deliberate run. An explicit roster-agent turn may launch one bounded provider process.</div></div>
    <div class="drawer-section"><p class="drawer-label">run facts</p><dl class="fact-list"><div><dt>Project</dt><dd id="fact-project">No room loaded</dd></div><div><dt>Root fingerprint</dt><dd id="fact-root">—</dd></div><div><dt>Initiator</dt><dd id="fact-initiator">—</dd></div><div><dt>Journal</dt><dd id="fact-journal">—</dd></div></dl></div>
    <div class="drawer-section"><p class="drawer-label">disclosure</p><p class="muted">Public redacted · local only. Private prompts, paths, credentials, native output, and provider controls stay outside this view.</p></div>
  </aside>
</div>
<div id="announce" class="visually-hidden" role="status" aria-live="polite"></div>
<script nonce="__NONCE__">__JS__</script>
</body>
</html>'''


_JS = r'''(() => {
  const token = location.hash.startsWith('#token=') ? location.hash.slice(7) : '';
  const headers = token ? {Authorization: 'Bearer ' + token} : {};
  const state = {
    selected: '', room: null, events: [], cursor: 0, rooms: {}, query: '', mode: 'context',
    active: {}, streamAbort: null, streamRetry: null, refreshTimer: null, streamCursor: 0,
    unread: {}, lastRoomIndex: ''
  };
  const $ = id => document.getElementById(id);
  const text = value => document.createTextNode(value == null ? '' : String(value));
  const key = (session, participant) => String(session) + '\u001f' + String(participant);
  const title = value => String(value || '').replaceAll('_', ' ');
  const modeLabel = value => String(value || 'chat').toUpperCase();
  const shortHash = value => value ? String(value).slice(0, 12) + '…' : '—';
  function announce(value) { $('announce').replaceChildren(text(value || '')); }
  function setConnection(label, kind) { const node = $('connection'); node.className = 'status ' + (kind || 'offline'); node.replaceChildren(text(label)); }
  async function getJSON(url, options = {}) {
    const requestHeaders = Object.assign({}, headers, options.headers || {});
    const response = await fetch(url, Object.assign({}, options, {headers: requestHeaders, cache: 'no-store'}));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'request refused');
    return data;
  }
  function identity(participant) {
    const list = state.room && Array.isArray(state.room.participants) ? state.room.participants : [];
    const found = list.find(item => String(item.agent || '') === String(participant || '')) || {};
    return {agent: String(found.agent || participant || 'agent'), role: String(found.role || 'participant'), name: String(found.name || found.agent || participant || 'Agent'), version: String(found.version || '')};
  }
  function identityLabel(participant, includeVersion = true) {
    const item = identity(participant);
    return [item.role, item.name, includeVersion ? item.version : ''].filter(Boolean).join(' · ');
  }
  function roomEntries() {
    const result = [];
    Object.values(state.rooms || {}).forEach(initiators => Object.values(initiators || {}).forEach(entries => (entries || []).forEach(room => result.push(room))));
    return result;
  }
  function filteredRoom(room) {
    const needle = state.query.trim().toLowerCase();
    if (!needle) return true;
    return [room.session_id, room.project_id, room.initiator_host, room.initiator_agent, room.mode].join(' ').toLowerCase().includes(needle);
  }
  function renderRooms() {
    const list = $('rooms'); list.replaceChildren();
    const groups = state.rooms || {}; let count = 0; let first = null;
    Object.entries(groups).forEach(([project, initiators]) => {
      const entries = Object.values(initiators || {}).flat().filter(filteredRoom);
      if (!entries.length) return;
      const group = document.createElement('section'); group.className = 'room-group';
      const projectLine = document.createElement('div'); projectLine.className = 'project-line';
      const projectText = project.split('@'); projectLine.append(text(projectText[0] || 'Project'));
      if (projectText[1]) { const digest = document.createElement('small'); digest.append(text(shortHash(projectText[1]))); projectLine.append(digest); }
      group.append(projectLine);
      Object.entries(initiators || {}).forEach(([initiator, rooms]) => {
        (rooms || []).filter(filteredRoom).forEach(room => {
          count += 1; if (!first) first = room.session_id;
          const who = document.createElement('div'); who.className = 'initiator-line'; who.append(text(initiator)); group.append(who);
          const button = document.createElement('button'); button.type = 'button'; button.className = 'room-button' + (room.session_id === state.selected ? ' active' : ''); button.dataset.session = room.session_id;
          const name = document.createElement('span'); name.className = 'room-name'; name.append(text(room.session_id));
          const detail = document.createElement('span'); detail.className = 'room-detail'; const chip = document.createElement('span'); chip.className = 'mode-chip'; chip.append(text(modeLabel(room.mode))); detail.append(chip);
          const countText = document.createElement('span'); countText.append(text(String(room.cursor || 0) + ' events')); detail.append(countText);
          if (state.unread[room.session_id] && room.session_id !== state.selected) { const dot = document.createElement('span'); dot.className = 'unread'; dot.title = 'New events'; detail.append(dot); }
          button.append(name, detail); button.addEventListener('click', () => select(room.session_id)); group.append(button);
        });
      });
      list.append(group);
    });
    $('room-count').replaceChildren(text(String(count)));
    if (!count) { const empty = document.createElement('p'); empty.className = 'empty'; empty.append(text(state.query ? 'No rooms match this filter.' : 'No conversations yet.')); list.append(empty); }
    if (!state.selected && first) select(first);
  }
  function updateHeader() {
    const room = state.room || {};
    $('title').replaceChildren(text(room.session_id || 'Choose a conversation'));
    $('room-label').replaceChildren(text(room.project_id ? room.project_id + ' / ' + room.session_id : 'No room selected'));
    $('room-subtitle').replaceChildren(text(room.session_id ? String(room.initiator_host || 'host') + ' · ' + String(room.initiator_agent || 'agent') : 'Rooms are grouped by project and initiating agent.'));
    $('cursor').replaceChildren(text(room.session_id ? String(state.cursor || 0) : '—'));
    $('mode').replaceChildren(text(room.session_id ? modeLabel(room.mode) : '—'));
    $('fact-project').replaceChildren(text(room.project_id || 'No room loaded'));
    $('fact-root').replaceChildren(text(shortHash(room.project_root_sha256)));
    $('fact-initiator').replaceChildren(text(room.session_id ? String(room.initiator_host || 'host') + ' / ' + String(room.initiator_agent || 'agent') : '—'));
    $('fact-journal').replaceChildren(text(room.session_id ? String(state.cursor || 0) + ' public events' : '—'));
    const chips = $('participants'); chips.replaceChildren();
    (Array.isArray(room.participants) ? room.participants : []).forEach(participant => { const chip = document.createElement('span'); chip.className = 'participant'; chip.title = identityLabel(participant.agent); chip.append(text(identityLabel(participant.agent))); chips.append(chip); });
  }
  function payloadPreview(record) {
    const payload = record && record.payload || {};
    return payload.preview || payload.summary || (payload.text_chars ? 'Human context · ' + String(payload.text_chars) + ' chars' : '');
  }
  function detailFor(record) {
    const details = document.createElement('details'); const summary = document.createElement('summary'); summary.append(text('Evidence · cursor ' + String(record.cursor || '—'))); details.append(summary);
    const body = document.createElement('div'); body.className = 'evidence';
    const payload = record.payload || {}; const parts = ['public redacted', 'generation ' + String(record.generation || 1), 'payload ' + shortHash(record.payload_sha256)];
    if (payload.model_served) parts.push('served ' + String(payload.model_served)); if (payload.transport) parts.push('transport ' + String(payload.transport));
    body.append(text(parts.join(' · '))); details.append(body); return details;
  }
  function eventIdentity(record) {
    const payload = record.payload || {}; return payload.participant || record.actor_id || payload.asker || 'system';
  }
  function renderEvent(record) {
    const event = String(record.event || 'event'); const payload = record.payload || {}; const who = eventIdentity(record); const preview = payloadPreview(record);
    if (event === 'human_message' || event === 'message_posted' || event === 'agent_message') {
      const article = document.createElement('li'); article.className = 'message ' + (event === 'human_message' ? 'human' : 'agent');
      const head = document.createElement('div'); head.className = 'message-head'; const strong = document.createElement('strong'); const heading = event === 'human_message' ? 'Operator context' : event === 'agent_message' ? identityLabel(payload.sender || who) : identityLabel(who); strong.append(text(heading)); const time = document.createElement('time'); time.append(text('cursor ' + String(record.cursor || '—'))); head.append(strong, time);
      const bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.append(text(preview || 'Redacted event')); const meta = document.createElement('div'); meta.className = 'message-meta'; meta.append(text(event === 'human_message' ? String(payload.text_chars || 0) + ' chars · ' + shortHash(payload.text_sha256) : event === 'agent_message' ? identityLabel(payload.sender || who, false) + ' → ' + identityLabel(payload.recipient || 'human', false) + ' · context only' : 'agent context · public redacted'));
      article.append(head, bubble, meta, detailFor(record)); return article;
    }
    const family = event === 'council_round_started' ? 'round' : event === 'position_submitted' ? 'position' : event === 'cross_exam' ? 'cross' : event === 'chair_synthesis' ? 'synthesis' : event === 'turn_started' ? 'turn' : event === 'turn_finished' ? 'finish' : 'default';
    const item = document.createElement('li'); item.className = 'system-event ' + family; const chip = document.createElement('div'); chip.className = 'system-chip'; const mark = document.createElement('span'); mark.className = 'event-mark'; mark.append(text(event === 'turn_started' ? 'RUN' : event === 'turn_finished' ? 'END' : event === 'council_round_started' ? 'RND' : event === 'position_submitted' ? 'POS' : event === 'cross_exam' ? 'ASK' : event === 'chair_synthesis' ? 'SYN' : event === 'fork_created' ? 'FORK' : 'LOG')); const copy = document.createElement('div'); const strong = document.createElement('strong'); strong.append(text(title(event))); const small = document.createElement('small'); const summary = preview || identityLabel(who, false) || 'journal event'; small.append(text(' · ' + summary)); copy.append(strong, small); chip.append(mark, copy, detailFor(record)); item.append(chip); return item;
  }
  function renderTimeline() {
    const timeline = $('timeline'); timeline.replaceChildren();
    if (!state.events.length) { const empty = document.createElement('li'); empty.className = 'timeline-empty'; empty.append(text('No events yet. Add context or ask a roster agent to begin.')); timeline.append(empty); return; }
    const fragment = document.createDocumentFragment(); state.events.forEach(record => fragment.append(renderEvent(record))); timeline.append(fragment);
  }
  function rebuildActive() {
    state.active = {};
    state.events.forEach(record => { const payload = record.payload || {}; const participant = payload.participant; if (!participant) return; const activeKey = key(state.selected, participant); if (record.event === 'turn_started') state.active[activeKey] = {participant: String(participant), turn_id: payload.turn_id || null}; if (record.event === 'turn_finished') delete state.active[activeKey]; });
  }
  function renderWorking() {
    const box = $('working'); box.replaceChildren(); const active = Object.values(state.active);
    active.forEach(item => { const row = document.createElement('div'); row.className = 'working-row'; const label = document.createElement('span'); const strong = document.createElement('strong'); strong.append(text(identityLabel(item.participant))); label.append(strong, text(' is working · durable turn in progress')); const cancel = document.createElement('button'); cancel.type = 'button'; cancel.append(text('Cancel turn')); cancel.addEventListener('click', () => cancelTurn(item.participant)); row.append(label, cancel); box.append(row); });
  }
  function renderRoom(data) {
    state.room = data.room || {}; state.events = Array.isArray(data.events) ? data.events.slice() : []; state.cursor = Number(data.cursor || state.events.length || 0); state.streamCursor = state.cursor; state.unread[state.selected] = false; rebuildActive(); updateHeader();
    const chooser = $('participant'); chooser.replaceChildren(); const emptyOption = document.createElement('option'); emptyOption.value = ''; emptyOption.append(text('Choose an agent')); chooser.append(emptyOption);
    (Array.isArray(state.room.participants) ? state.room.participants : []).forEach(item => { const option = document.createElement('option'); option.value = String(item.agent || ''); option.title = identityLabel(item.agent); option.append(text(identityLabel(item.agent))); chooser.append(option); });
    renderTimeline(); renderWorking(); updateComposer(); announce('Opened conversation ' + String(state.room.session_id || ''));
  }
  function appendRecord(record) {
    const cursor = Number(record && record.cursor); if (!Number.isSafeInteger(cursor)) return 'gap';
    if (cursor <= state.cursor) return 'duplicate';
    if (cursor !== state.cursor + 1) return 'gap';
    state.events.push(record); state.cursor = cursor; state.streamCursor = cursor; rebuildActive(); updateHeader(); renderWorking();
    const timeline = $('timeline'); if (timeline.firstElementChild && timeline.firstElementChild.classList.contains('timeline-empty')) timeline.replaceChildren(); timeline.append(renderEvent(record)); announce('New journal event ' + String(cursor)); return 'appended';
  }
  function updateComposer() {
    const hasRoom = Boolean(state.selected); const context = state.mode === 'context'; $('context-tab').setAttribute('aria-selected', context ? 'true' : 'false'); $('agent-tab').setAttribute('aria-selected', context ? 'false' : 'true'); $('participant').hidden = context; $('participant').disabled = !hasRoom; $('send').hidden = false; $('send').disabled = !hasRoom || !$('message').value.trim() || (!context && !$('participant').value); $('run-cancel').hidden = context; $('run-cancel').disabled = context || !hasRoom || !Object.keys(state.active).length; $('message').placeholder = context ? 'Add context for this room…' : 'Send a message to this participant…'; $('caption').replaceChildren(text(context ? 'Saved as a human context event; never a control command.' : 'Each turn is durably recorded; output is context, not approval. Identity drift creates a visible fork.')); $('send').classList.toggle('agent-action', !context); $('send').replaceChildren(text(context ? 'Post context' : 'Start turn')); }
  function stopStream() { if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; } if (state.streamRetry) { clearTimeout(state.streamRetry); state.streamRetry = null; } }
  async function syncRoom() { if (!state.selected) return; const room = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected)); renderRoom(room); }
  async function watch(session, after) {
    stopStream(); const controller = new AbortController(); state.streamAbort = controller; let cursor = Number(after) || 0;
    try {
      const response = await fetch('/api/v1/rooms/' + encodeURIComponent(session) + '/stream?after=' + encodeURIComponent(String(cursor)), {headers, cache: 'no-store', signal: controller.signal});
      if (!response.ok || !response.body || !response.body.getReader) throw new Error('stream unavailable');
      setConnection('Live connected', ''); const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (state.selected === session && !controller.signal.aborted) {
        const packet = await reader.read(); if (packet.done) break; buffer += decoder.decode(packet.value, {stream: true}); const chunks = buffer.split('\n\n'); buffer = chunks.pop() || '';
        for (const chunk of chunks) { const dataLine = chunk.split('\n').find(line => line.startsWith('data:')); if (!dataLine) continue; let record; try { record = JSON.parse(dataLine.slice(5).trim()); } catch (_) { await syncRoom(); continue; } const result = appendRecord(record); if (result === 'gap') { await syncRoom(); } cursor = state.cursor; }
      }
      if (state.selected === session && !controller.signal.aborted) throw new Error('stream ended');
    } catch (error) {
      if (controller.signal.aborted) return; setConnection('Live updates paused', 'degraded'); $('note').replaceChildren(text('Live updates paused; retrying safely.')); state.streamRetry = setTimeout(() => { if (state.selected === session) watch(session, state.cursor); }, 900);
    }
  }
  async function select(session) { state.selected = String(session || ''); stopStream(); renderRooms(); try { const data = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected)); renderRoom(data); setConnection('Live connected', ''); watch(state.selected, state.cursor); } catch (error) { setConnection('Reconnect needed', 'blocked'); $('note').replaceChildren(text('Room could not be read; retrying safely.')); announce('Room could not be read'); } }
  async function loadRooms() { const data = await getJSON('/api/v1/rooms'); const serialized = JSON.stringify(data.rooms || {}); if (serialized !== state.lastRoomIndex) { state.rooms = data.rooms || {}; state.lastRoomIndex = serialized; renderRooms(); } setConnection(state.selected ? 'Live connected' : 'Owner connected', ''); }
  async function refresh() { try { await loadRooms(); } catch (error) { setConnection('Reconnect needed', 'blocked'); $('note').replaceChildren(text('Conversation list could not be read; retrying safely.')); } }
  async function postContext() { const value = $('message').value; if (!state.selected || !value.trim()) return; $('send').disabled = true; try { await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/messages', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: value})}); $('message').value = ''; $('note').replaceChildren(text('Context saved durably.')); updateComposer(); await syncRoom(); } catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text('Context could not be saved; retry when the owner is available.')); } finally { updateComposer(); } }
  async function startTurn() { const participant = $('participant').value; const value = $('message').value; const activeKey = key(state.selected, participant); if (!state.selected || !participant || !value.trim() || state.active[activeKey]) return; state.active[activeKey] = {participant, turn_id: null}; renderWorking(); updateComposer(); $('note').className = 'composer-note'; $('note').replaceChildren(text('Turn is durably prepared; waiting for the agent…')); try { const result = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/turns', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant, message: value})}); $('message').value = ''; if (result.status === 'forked') $('note').replaceChildren(text('A new conversation fork was created; the provider session was not resumed.')); else $('note').replaceChildren(text('Turn started; other participants can work in parallel.')); await syncRoom(); } catch (error) { delete state.active[activeKey]; renderWorking(); $('note').className = 'composer-note error'; $('note').replaceChildren(text('Turn could not be started; the journal was left unchanged.')); } finally { updateComposer(); } }
  async function cancelTurn(participant) { const activeKey = key(state.selected, participant); if (!state.selected || !participant || !state.active[activeKey]) return; try { await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant})}); $('note').className = 'composer-note'; $('note').replaceChildren(text('Cancellation requested; the provider process will be stopped safely.')); } catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text('No active turn could be cancelled.')); } }
  $('context-tab').addEventListener('click', () => { state.mode = 'context'; updateComposer(); }); $('agent-tab').addEventListener('click', () => { state.mode = 'agent'; updateComposer(); }); $('send').addEventListener('click', () => state.mode === 'context' ? postContext() : startTurn()); $('run-cancel').addEventListener('click', () => { const first = Object.values(state.active)[0]; if (first) cancelTurn(first.participant); }); $('participant').addEventListener('change', updateComposer); $('message').addEventListener('input', () => { $('counter').replaceChildren(text(String($('message').value.length) + ' / 12000')); updateComposer(); }); $('message').addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); state.mode === 'context' ? postContext() : startTurn(); } }); $('room-search').addEventListener('input', event => { state.query = event.target.value; renderRooms(); }); $('rail-open').addEventListener('click', () => $('rail').classList.toggle('open'));
  $('participant').replaceChildren(); const choose = document.createElement('option'); choose.value = ''; choose.append(text('Choose an agent')); $('participant').append(choose);
  refresh(); state.refreshTimer = setInterval(refresh,10000); window.addEventListener('beforeunload', () => { clearInterval(state.refreshTimer); stopStream(); });
})();'''


def page_bytes(*, nonce: str) -> bytes:
    """Build the page with a nonce as the only request-specific value."""
    if not isinstance(nonce, str) or not nonce or any(ch in nonce for ch in '<>"\''):
        raise ValueError("invalid page nonce")
    markup = _HTML.replace("__NONCE__", nonce).replace("__CSS__", _CSS).replace("__JS__", _JS)
    return markup.encode("utf-8")


__all__ = ["page_bytes"]
