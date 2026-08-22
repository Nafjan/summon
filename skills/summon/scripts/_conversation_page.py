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
  /* Compatibility aliases for the earlier cyan vocabulary. Keep the visual
     system on the same blue token so focus, tabs, and primary actions agree. */
  --cyan: #6bcaff;
  --cyan-deep: #133447;
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
.mode-chip { display: inline-flex; align-items: center; min-height: 20px; padding: 2px 6px; border: 1px solid #4b6373; border-radius: 5px; color: var(--cyan); font-size: 10px; letter-spacing: .09em; text-transform: uppercase; }
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
@media (max-width: 1120px) and (min-width: 761px) { .atlas { grid-template-columns: 250px minmax(0, 1fr); grid-template-rows: auto auto; } .rail { grid-row: 1 / span 2; } .drawer { display: none; grid-column: 2; grid-row: 2; border-top: 1px solid var(--line-soft); border-left: 0; } .drawer.open { display: block; } .drawer-close { display: inline-flex; align-items: center; justify-content: center; min-width: 44px; min-height: 44px; margin: -8px 0 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); } .drawer-toggle { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); font-size: 11px; } .thread-body { width: min(870px, 100%); } }
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

/* Beautiful UI visual adaptation: the centered canvas, striped surround,
   dashed hairlines, compact cards, and restrained state palette are adapted
   from the MIT-licensed Beautiful UI reference. Summon keeps its own markup,
   journal semantics, accessibility behavior, and dependency-free runtime. */
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
.brand::after { content: "LOCAL ATLAS"; margin-left: auto; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; }
.rail-heading { margin-bottom: 11px; }
.rail-heading h2 { color: var(--text); font-size: 12px; letter-spacing: .01em; }
.rail-heading span { color: var(--blue); font-size: 10px; }
.search { min-height: 44px; margin-bottom: 18px; padding: 10px 12px; border-color: var(--line); border-radius: 10px; background: #0a0f14; }
.search:focus-visible, textarea:focus-visible, select:focus-visible { border-color: var(--blue); outline-color: var(--blue); }
.room-groups { gap: 22px; }
.project-line { margin-bottom: 8px; color: var(--text); font-size: 11px; letter-spacing: .02em; }
.project-line small { color: var(--blue); font-size: 10px; }
.initiator-line { margin: 0 7px 5px; color: var(--quiet); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.room-button { min-height: 72px; margin: 4px 0; padding: 11px 12px 11px 14px; border-color: transparent; border-radius: 12px; }
.room-button:hover, .room-button:focus-visible { border-color: var(--line); background: var(--panel); }
.room-button.active { border-color: #39637b; background: var(--blue-deep); box-shadow: inset 0 0 0 1px #6bcaff16; }
.room-button.active::before { width: 3px; top: 15px; bottom: 15px; background: var(--blue); }
.room-name { font-size: 13px; }
.room-detail { margin-top: 7px; gap: 8px; color: var(--quiet); font-size: 10px; }
.mode-chip { min-height: 21px; border-color: #426c82; border-radius: 6px; color: var(--blue); font-size: 10px; letter-spacing: .1em; }
.unread { width: 7px; height: 7px; background: var(--lime); box-shadow: 0 0 0 3px #b7f36b22; }
.topbar { min-height: 88px; padding: 17px clamp(22px, 4vw, 54px); border-bottom-color: var(--line); background: #0a0d11f5; }
.topbar::before { content: "ROOM / DURABLE JOURNAL"; position: absolute; top: 10px; left: clamp(22px, 4vw, 54px); color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .16em; }
.room-title { padding-top: 10px; }
.room-title h1 { font-size: clamp(22px, 2.6vw, 34px); letter-spacing: -.045em; }
.room-subtitle { margin-top: 8px; color: var(--muted); font-size: 12px; }
.status { min-height: 34px; padding: 7px 11px; border-color: #4c6b59; border-radius: 9px; background: #16231b; color: var(--lime); font-size: 10px; letter-spacing: .03em; box-shadow: 0 0 0 3px #b7f36b0e; }
.status::before { width: 7px; height: 7px; }
.status.degraded { border-color: #5e4d83; background: #251c38; color: var(--violet); }
.status.blocked { border-color: #78464a; background: var(--coral-deep); color: var(--coral); }
.status.offline { border-color: var(--line); background: var(--panel); color: var(--muted); }
.thread-body { width: min(930px, 100%); padding: 25px clamp(22px, 4vw, 58px) 0; }
.room-bar { padding: 0 0 18px; border-bottom-color: var(--line); }
.room-bar-label strong { font-size: 14px; letter-spacing: -.01em; }
.room-origin { margin-top: 5px; color: var(--blue); font-size: 10px; letter-spacing: .12em; }
.facts { gap: 8px; color: var(--quiet); font-size: 10px; text-transform: uppercase; }
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
.message-head time { color: var(--quiet); font-size: 10px; }
.bubble { padding: 14px 16px; border-color: #3a5668; border-radius: 14px 14px 14px 5px; background: var(--panel-2); box-shadow: 0 1px 0 #ffffff08; }
.message.human .bubble { border-color: #8a6535; border-radius: 14px 14px 5px 14px; background: var(--amber-deep); color: #fff2d6; }
.message-meta { margin-top: 7px; color: var(--quiet); font-size: 10px; }
.system-event { width: min(710px, 100%); }
.system-chip { min-height: 46px; padding: 9px 12px; border-color: var(--line); border-radius: 10px; background: var(--panel); box-shadow: 0 1px 0 #ffffff06; }
.system-chip .event-mark { width: 23px; height: 23px; border-color: #5b7b8f; border-radius: 7px; color: var(--blue); font-size: 10px; }
.system-chip strong { font-size: 12px; }
.system-chip small { color: var(--muted); font-size: 10px; }
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
.evidence { border-top-color: var(--line); color: var(--quiet); font-size: 10px; }
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
.composer-caption span:last-child { color: var(--blue); font-size: 10px; }
.composer-note { color: var(--amber); font-size: 11px; }
.drawer { padding: 25px 18px; border-left-color: var(--line); background: var(--rail); }
.drawer h2 { font-size: 14px; letter-spacing: -.01em; }
.drawer-section { padding-bottom: 21px; margin-bottom: 21px; border-bottom-color: var(--line-soft); }
.drawer-label { color: var(--blue); font-size: 10px; letter-spacing: .14em; }
.boundary { padding: 14px; border-color: #876c3a; border-radius: 11px; background: var(--amber-deep); color: #fff0c8; font-size: 11px; box-shadow: inset 0 0 0 1px #ffd27e0e; }
.boundary strong { color: var(--amber); font-size: 11px; }
.fact-list { gap: 12px; }
.fact-list dt { color: var(--quiet); font-size: 10px; }
.fact-list dd { color: var(--text); font-size: 10px; }
.drawer .muted { color: var(--muted); font-size: 11px; line-height: 1.6; }
@media (max-width: 1120px) and (min-width: 761px) { .atlas { grid-template-columns: 250px minmax(0, 1fr); } }
@media (max-width: 760px) {
  .topbar { min-height: 78px; padding: 12px 16px; }
  .topbar::before { top: 7px; left: 16px; font-size: 10px; }
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

/* Finish pass: make the atlas legible as an operator instrument, not a stream
   of implementation details. These rules intentionally sit last so the
   workbench tokens above remain the visual source of truth. */
button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.rail-close { display: none; }
.drawer-close { display: none; }
.rail-backdrop { display: none; }
.notice { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: min(930px, 100%); margin: 0 auto; padding: 10px clamp(22px, 4vw, 58px); border-bottom: 1px solid var(--line); background: var(--coral-deep); color: var(--coral); font-size: 12px; }
.notice[hidden] { display: none; }
.notice button { min-height: 44px; padding: 8px 11px; border: 1px solid currentColor; border-radius: 7px; background: transparent; color: inherit; font-size: 11px; }
.timeline-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 24px 0 -7px; color: var(--muted); }
.timeline-head strong { font-size: 12px; letter-spacing: .01em; }
.timeline-head small { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.stream-filters { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.stream-filter { min-width: 44px; min-height: 44px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); color: var(--muted); font-size: 10px; }
.stream-filter[aria-pressed="true"] { border-color: var(--blue); background: var(--blue-deep); color: var(--text); }
.latest { min-height: 44px; padding: 8px 10px; border: 1px solid var(--blue); border-radius: 7px; background: var(--blue-deep); color: var(--text); font-size: 11px; }
.latest[hidden] { display: none; }
.event-mark[aria-label] { cursor: help; }
.system-chip summary, .message summary { display: inline-flex; align-items: center; min-height: 44px; padding: 6px 8px; }
#run-cancel { min-height: 44px; padding: 8px 11px; border: 1px solid var(--coral); border-radius: 8px; background: transparent; color: var(--coral); font-size: 11px; }
.room-title h1 { text-wrap: balance; }
.room-title h1 { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; max-height: calc(2 * 1.18em); overflow: hidden; }
.room-subtitle .session-id { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.room-intent { max-width: 62ch; margin-top: 8px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); box-shadow: inset 0 1px 0 var(--blue); color: var(--text); font-size: 12px; line-height: 1.42; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; }
.facts { flex-wrap: wrap; justify-content: flex-end; }
.facts span[data-live="true"] { border-color: #42647a; color: var(--blue); }
.working-summary { color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.working-row button { min-height: 44px; padding: 8px 11px; }
.skip { min-height: 44px; }
.system-event.cancel .system-chip { border-left: 1px solid var(--coral); background: var(--coral-deep); }
.system-event.cleanup .system-chip { border-left: 1px solid var(--lime); background: var(--lime-deep); }
.system-event.fork .system-chip { border-left: 1px solid var(--violet); background: #251d3b; }
.system-event.context .system-chip { border-left: 1px solid var(--amber); background: var(--amber-deep); }
.identity-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 16px; }
.identity-heading span:first-child { color: var(--text); font-size: 11px; font-weight: 760; letter-spacing: .02em; }
.identity-heading span:last-child { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .03em; text-align: right; }
.participants { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px; margin-top: 8px; }
.participant { position: relative; display: grid; grid-template-columns: 8px minmax(0, 1fr); grid-template-rows: auto auto auto auto; align-items: start; gap: 2px 8px; min-width: 0; min-height: 66px; padding: 9px 10px; border-color: #395366; border-radius: 10px; background: linear-gradient(135deg, #17232d, #111920); color: var(--text); text-align: left; font: inherit; cursor: pointer; }
.participant:hover, .participant:focus-visible { border-color: var(--blue); background: #1a2a35; outline: none; }
.participant::before { grid-row: 1 / span 4; align-self: start; width: 8px; height: 8px; margin-top: 4px; background: var(--lime); box-shadow: 0 0 0 3px #b7f36b1d; }
.participant.unresolved { border-color: #705a36; border-style: dashed; background: #1b1814; }
.participant.unresolved::before { background: var(--amber); box-shadow: 0 0 0 3px #ffd27e1a; }
.participant-role { overflow: hidden; color: var(--blue); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .12em; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
.participant-name { overflow: hidden; font-size: 12px; font-weight: 780; text-overflow: ellipsis; white-space: nowrap; }
.participant-model { overflow: hidden; color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.participant.unresolved .participant-model { color: var(--quiet); }
.participant-provider { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.identity-summary { max-width: 34ch; }
.identity-help { margin: 5px 0 0; color: var(--quiet); font-size: 10px; }
.room-models { display: block; overflow: hidden; margin-top: 5px; color: var(--blue); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.message-model { color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.message-model.drift { color: var(--coral); }
.message-model.unsealed { color: var(--quiet); }
.event-time { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }
.system-proof { display: block; margin-top: 3px; color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.system-proof.matched { color: var(--lime); }
.system-proof.drift { color: var(--coral); }
.system-proof.unsealed { color: var(--quiet); }
.system-proof.awaiting { color: var(--blue); }
.proof-caption { display: block; margin-top: 2px; color: var(--muted); font-size: 10px; line-height: 1.35; }
.turn-recovery { display: grid; gap: 8px; margin: 14px 0 0; padding: 12px; border: 1px solid #705a36; border-radius: 10px; background: #1b1814; }
.turn-recovery[hidden] { display: none; }
.turn-recovery strong { color: var(--amber); font-size: 12px; }
.turn-recovery p { margin: 0; color: var(--muted); font-size: 11px; }
.turn-recovery-facts { color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.turn-recovery-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.turn-recovery-actions button { min-height: 44px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
.turn-recovery-actions .recover { border-color: var(--amber); color: var(--amber); }
.turn-recovery-actions .fork { border-color: var(--violet); color: var(--violet); }
.action-dialog { position: fixed; z-index: 30; inset: 0; display: grid; place-items: center; padding: 20px; background: #000b; }
.action-dialog[hidden] { display: none; }
.action-dialog-card { width: min(480px, 100%); display: grid; gap: 12px; padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); box-shadow: 0 24px 70px #000c; }
.action-dialog-card h2 { margin: 0; color: var(--text); font-size: 16px; }
.action-dialog-card p { margin: 0; color: var(--muted); font-size: 12px; }
.action-dialog-card input { min-height: 44px; width: 100%; padding: 9px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--ink); color: var(--text); }
.action-dialog-card input:focus-visible { border-color: var(--blue); outline: 2px solid var(--focus); outline-offset: 2px; }
.action-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
.action-dialog-actions button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2); color: var(--text); }
.action-dialog-actions .confirm { border-color: var(--amber); background: var(--amber-deep); color: var(--text); }
.message-role { color: var(--blue); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.working-row .working-model { display: block; margin-top: 2px; color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.working-row .working-identity { display: block; }
.identity-tooltip { cursor: help; }
.event-legend { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 6px; color: var(--quiet); font-size: 10px; }
.event-legend span { display: inline-flex; align-items: center; gap: 4px; }
.event-legend b { color: var(--text); font-weight: 720; }
.event-legend .proof-match b { color: var(--lime); }
.event-legend .proof-drift b { color: var(--coral); }
.event-legend .proof-awaiting b { color: var(--blue); }
.glossary { margin-top: 5px; color: var(--muted); font-size: 10px; }
.glossary summary { display: inline-flex; align-items: center; min-height: 38px; padding: 4px 6px; cursor: pointer; color: var(--cyan); }
.glossary p { max-width: 650px; margin: 3px 0 0; color: var(--muted); }
.room-session { display: block; overflow: hidden; margin-top: 4px; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.launch-review { display: grid; gap: 8px; margin-top: 10px; padding: 11px 12px; border: 1px solid #5d6d79; border-radius: 10px; background: #141d24; }
.launch-review[hidden] { display: none; }
.launch-review strong { color: var(--text); font-size: 12px; }
.launch-review p { margin: 0; color: var(--muted); font-size: 11px; }
.launch-review .review-facts { color: var(--lime); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }
.review-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.review-actions button { min-height: 42px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
.review-actions .confirm { border-color: var(--lime); background: var(--lime-deep); color: var(--text); }
@media (max-width: 760px) {
  .rail { z-index: 11; overflow-y: auto; }
  .rail-close { display: inline-flex; align-items: center; justify-content: center; float: right; min-width: 44px; min-height: 44px; margin: -8px 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
  .drawer-close { display: inline-flex; align-items: center; justify-content: center; float: right; min-width: 44px; min-height: 44px; margin: -8px 0 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
  .rail-backdrop { position: fixed; z-index: 10; inset: 0; display: block; background: #0009; }
  .rail-backdrop[hidden] { display: none; }
  .composer { position: relative; margin-left: -16px; margin-right: -16px; padding-left: 16px; padding-right: 16px; border-top-color: var(--line); }
  .drawer { display: none; }
  /* Keep the composer unobscured: evidence is a top sheet on small screens,
     while the durable composer remains available at the bottom of the room. */
  .drawer.open { position: fixed; z-index: 12; inset: 78px 12px auto; display: block; max-height: min(52vh, 430px); overflow-y: auto; border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 18px 36px #000b; }
  .drawer-toggle { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); font-size: 11px; }
  .notice { padding-left: 16px; padding-right: 16px; }
  .timeline-head { margin-top: 18px; }
  .latest { min-height: 44px; }
  .participants { grid-template-columns: 1fr; }
  .identity-heading { align-items: flex-start; flex-direction: column; gap: 3px; }
  .identity-heading span:last-child { text-align: left; }
}

/* Final interaction floor. Keep these after the workbench layer so hit areas
   cannot be reset by legacy tokens. */
.review-actions button, .working-row button, .glossary summary,
.system-chip summary, .message summary, .notice button, .latest { min-height: 44px; }
.glossary summary, .system-chip summary, .message summary { display: inline-flex; align-items: center; padding: 4px 6px; }
.facts, .message-meta, .evidence { font-size: 11px; }
  .fact-list dd { font-size: 12px; }

/* Beautiful UI adaptation layer. These are intentionally local tokens and
   selectors rather than a remote stylesheet: the MIT credit lives in
   THIRD_PARTY_NOTICES.md and no vendor runtime/font is loaded. */
:root {
  --ink: #17181a;
  --rail: #1c1d1f;
  --panel: #222326;
  --panel-2: #252629;
  --panel-3: #2b2c30;
  --line: #383a3f;
  --line-soft: #2c2e32;
  --text: #f1f2f4;
  --muted: #a2a5ac;
  --quiet: #767a82;
  --blue: #6c9dff;
  --blue-deep: #273244;
  --lime: #9fe4a7;
  --lime-deep: #233128;
  --amber: #f1bd7a;
  --amber-deep: #30291f;
  --violet: #c2b5ff;
  --violet-deep: #2b2938;
  --coral: #ff9b91;
  --coral-deep: #352427;
  --focus: #a9c5ff;
}
html, body {
  background: #17181a;
}
body {
  background-image: repeating-linear-gradient(135deg, #17181a 0 2px, #151618 2px 4px);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  letter-spacing: -.01em;
}
.atlas {
  width: min(1240px, calc(100% - 32px));
  margin: 0 auto;
  grid-template-columns: 250px minmax(0, 1fr) 266px;
  background: var(--rail);
  border-inline: 1px dashed var(--line);
}
.rail, .drawer {
  background: var(--rail);
}
.rail {
  border-right: 1px dashed var(--line);
}
.drawer {
  border-left: 1px dashed var(--line);
}
.thread {
  background: var(--rail);
}
.brand {
  margin-top: 4px;
  margin-bottom: 31px;
  letter-spacing: .12em;
}
.brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--blue);
  color: #15171a;
  box-shadow: 0 0 0 1px #ffffff14, 0 2px 8px #0005;
}
.search, textarea, select {
  background: var(--panel);
  border-color: var(--line);
  box-shadow: inset 0 1px 2px #0006;
}
.search { border-radius: 8px; }
.room-button {
  border-radius: 8px;
}
.room-button:hover, .room-button:focus-visible {
  border-color: var(--line-strong, #4a4c52);
  background: var(--panel);
}
.room-button.active {
  border-color: #4a4c52;
  background: var(--panel);
  box-shadow: 0 0 0 1px #ffffff08, 0 2px 8px #0003;
}
.room-button.active::before {
  width: 2px;
  background: var(--blue);
}
.mode-chip {
  border-color: #4b4e57;
  color: var(--blue);
}
.topbar {
  min-height: 96px;
  background: var(--rail);
  border-bottom: 1px dashed var(--line);
}
.topbar::before {
  color: var(--quiet);
}
.room-title h1 {
  font-size: clamp(22px, 2.35vw, 30px);
  font-weight: 680;
  -webkit-line-clamp: 1;
  max-height: 1.2em;
}
.status {
  border-radius: 999px;
  background: var(--panel);
  box-shadow: 0 0 0 1px #ffffff08, 0 2px 6px #0004;
}
.thread-body {
  width: 100%;
  padding-inline: 32px;
}
.room-bar {
  border-bottom: 1px dashed var(--line);
}
.facts span, .participant, .tab, .stream-filter {
  background: var(--panel);
  border-color: var(--line);
  box-shadow: 0 0 0 1px #ffffff05, 0 1px 2px #0003;
}
.facts span {
  border-radius: 999px;
}
.facts span[data-live="true"] {
  border-color: #4a638e;
}
.timeline {
  gap: 19px;
}
.timeline::before {
  background: repeating-linear-gradient(to bottom, var(--line) 0 3px, transparent 3px 7px);
}
.bubble, .system-chip, .working-row, .launch-review, .turn-recovery {
  background: var(--panel);
  border-color: var(--line);
  box-shadow: 0 0 0 1px #ffffff06, 0 2px 8px #0003;
}
.message.human .bubble {
  background: var(--amber-deep);
  border-color: #6b583e;
}
.system-chip {
  border-radius: 9px;
}
.system-event.round .system-chip, .system-event.turn .system-chip { background: #202a38; }
.system-event.position .system-chip, .system-event.finish .system-chip { background: var(--lime-deep); }
.system-event.cross .system-chip, .system-event.context .system-chip { background: var(--amber-deep); }
.system-event.synthesis .system-chip, .system-event.fork .system-chip { background: var(--violet-deep); }
.system-event.cancel .system-chip { background: var(--coral-deep); }
.system-event.cleanup .system-chip { background: var(--lime-deep); }
.composer {
  border-top: 1px dashed var(--line);
  background: var(--rail);
}
.tab[aria-selected="true"] {
  background: var(--blue-deep);
  border-color: var(--blue);
}
.primary {
  border: 0;
  border-radius: 8px;
  background: var(--blue);
  box-shadow: 0 2px 10px #0005;
}
.drawer-section {
  border-bottom: 1px dashed var(--line);
}
.boundary {
  background: var(--amber-deep);
  border-color: #6b583e;
  box-shadow: 0 0 0 1px #ffffff06, 0 2px 8px #0003;
}
.rail-credit {
  margin: 28px 7px 0;
  padding-top: 16px;
  border-top: 1px dashed var(--line);
  color: var(--quiet);
  font-size: 10px;
  line-height: 1.55;
}
.rail-credit strong { color: var(--muted); font-weight: 650; }
.rail-credit a { color: var(--blue); text-decoration: none; }
.rail-credit a:hover, .rail-credit a:focus-visible { text-decoration: underline; }
.room-models { white-space: normal; line-height: 1.35; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.participant-model { color: var(--blue); font-size: 11px; }
.participant-provider { color: var(--muted); font-size: 10px; letter-spacing: .02em; }
.participant-name { font-size: 13px; }
.participant-role { color: var(--quiet); }
.agent-picker { position: relative; margin: 0 0 8px; }
.agent-picker[hidden], .agent-picker-menu[hidden] { display: none; }
.agent-picker-button { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; width: 100%; min-height: 52px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text); text-align: left; box-shadow: 0 0 0 1px #ffffff05, 0 2px 8px #0003; }
.agent-picker-button:hover, .agent-picker-button:focus-visible, .agent-picker-button[aria-expanded="true"] { border-color: var(--blue); outline: none; box-shadow: 0 0 0 3px #6c9dff2b, 0 2px 8px #0003; }
.agent-picker-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 680; }
.agent-picker-meta { overflow: hidden; color: var(--blue); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.agent-picker-chevron { color: var(--muted); font-size: 16px; line-height: 1; }
.agent-picker-menu { position: absolute; z-index: 20; top: calc(100% + 7px); right: 0; left: 0; display: grid; gap: 3px; max-height: 260px; overflow-y: auto; padding: 6px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel-2); box-shadow: 0 14px 32px #000b, 0 0 0 1px #ffffff08; }
.agent-option { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 10px; width: 100%; min-height: 54px; padding: 8px 10px; border: 1px solid transparent; border-radius: 7px; background: transparent; color: var(--text); text-align: left; }
.agent-option:hover, .agent-option:focus-visible, .agent-option[aria-selected="true"] { border-color: var(--line); background: var(--blue-deep); outline: none; }
.agent-option-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 720; }
.agent-option-role { overflow: hidden; color: var(--quiet); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.agent-option-model { grid-row: 1 / span 2; align-self: center; color: var(--blue); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-align: right; }
@media (max-width: 760px) { .agent-picker-menu { position: fixed; top: auto; right: 16px; bottom: 16px; left: 16px; max-height: 45vh; } }
@media (max-width: 1120px) and (min-width: 761px) {
  .atlas { width: 100%; grid-template-columns: 250px minmax(0, 1fr); border-inline: 0; }
  /* Give the tablet thread a full-width room header. Keeping the facts beside
     the label at 768px leaves the label with a single-character column. */
  .room-bar { display: block; }
  .room-bar-label { max-width: 100%; }
  .facts { margin-top: 9px; flex-wrap: wrap; }
  .drawer-close { display: inline-flex; align-items: center; justify-content: center; min-width: 44px; min-height: 44px; margin: -8px 0 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); }
}
@media (max-width: 760px) {
  .atlas { width: 100%; border-inline: 0; }
  .thread-body { padding-inline: 16px; }
  .rail { border-right: 1px dashed var(--line); }
  .room-intent { max-width: 100%; }
  .room-title h1 { -webkit-line-clamp: 2; max-height: calc(2 * 1.18em); }
}

/* Atlas geometry contract: the browser window never owns the conversation
   scroll. A fixed shell plus one feed scroller keeps notices, activity, and
   the composer from moving the operator's reading position. */
html, body { height: 100%; min-height: 0; overflow: hidden; }
body { overscroll-behavior: none; }
.atlas { height: 100dvh; min-height: 0; overflow: hidden; }
.thread { height: 100%; min-height: 0; display: grid; grid-template-rows: 96px 48px minmax(0, 1fr); overflow: hidden; }
.topbar { position: relative; min-height: 96px; height: 96px; overflow: hidden; }
.status-stack { display: grid; gap: 5px; min-width: 184px; flex: none; }
.turn-status { min-height: 27px; padding: 5px 10px; border: 1px solid var(--line); border-radius: 999px; background: var(--panel); color: var(--muted); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.turn-status.active { border-color: #426c82; color: var(--blue); }
.turn-status.attention { border-color: #705a36; color: var(--amber); }
.notice { width: 100%; height: 48px; min-height: 48px; margin: 0; padding: 7px clamp(22px, 4vw, 54px); overflow: hidden; }
.notice[hidden] { display: flex; visibility: hidden; }
.thread-body { height: 100%; min-height: 0; width: min(930px, 100%); margin: 0 auto; display: grid; grid-template-rows: auto minmax(52px, auto) auto minmax(0, 1fr) auto; overflow: hidden; padding: 18px clamp(22px, 4vw, 58px) 0; }
.room-bar { max-height: 240px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.activity-dock { min-height: 52px; max-height: 138px; overflow-y: auto; overscroll-behavior: contain; padding: 7px 0; scrollbar-gutter: stable; }
.working { min-height: 0; max-height: 92px; overflow-y: auto; margin: 0; }
.turn-recovery { max-height: 118px; overflow-y: auto; margin: 0; }
.timeline-head { position: relative; min-height: 48px; max-height: 112px; margin: 10px 0 0; padding: 8px 0; overflow: hidden; }
.timeline-head > div { min-width: 0; padding-right: 160px; }
.latest { position: absolute; top: 5px; right: 0; min-width: 146px; }
.event-legend { max-height: 36px; overflow: hidden; }
.glossary { max-height: 36px; overflow: hidden; }
.timeline { min-height: 0; height: auto; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; margin: 0; padding: 12px 8px 24px 0; contain: layout paint; }
.composer { min-height: 142px; max-height: 246px; overflow-y: auto; margin: 0; padding: 12px 0 calc(14px + env(safe-area-inset-bottom)); }
.composer textarea { max-height: 120px; }
.message summary, .system-chip summary, .notice button, .latest { min-height: 44px; padding-top: 10px; padding-bottom: 10px; }
.drawer { min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
@media (max-width: 1120px) and (min-width: 761px) {
  .atlas { height: 100dvh; grid-template-columns: 250px minmax(0, 1fr); grid-template-rows: 100%; }
  .drawer.open { position: fixed; z-index: 12; inset: 18px 18px 18px 268px; display: block; max-height: none; overflow-y: auto; border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 18px 36px #000b; }
  .thread-body { width: 100%; padding-inline: 22px; }
  .room-bar { max-height: 160px; }
  .timeline-head { max-height: 96px; }
}
@media (max-width: 760px) {
  .atlas { display: grid; grid-template-columns: minmax(0, 1fr); height: 100dvh; }
  .thread { grid-template-rows: 96px 48px minmax(0, 1fr); }
  .topbar { padding-inline: 16px; }
  .status-stack { min-width: 0; max-width: 47%; }
  .turn-status { overflow: hidden; text-overflow: ellipsis; }
  .thread-body { width: 100%; padding: 10px 16px min(300px, 45dvh); }
  .room-bar { display: block; max-height: 172px; overflow: hidden; }
  .identity-help { display: none; }
  .identity-heading { margin-top: 7px; }
  .participants { display: flex; flex-wrap: nowrap; max-height: 82px; margin-top: 7px; padding-bottom: 4px; overflow-x: auto; overscroll-behavior: contain; }
  .participant { flex: 0 0 220px; min-height: 66px; }
  .facts { flex-wrap: wrap; gap: 6px 10px; margin-top: 8px; }
  .timeline-head { min-height: 58px; max-height: 72px; margin-top: 4px; padding-block: 4px; }
  .timeline-head > div { padding-right: 0; overflow: hidden; }
  .stream-filters { display: flex; flex-wrap: nowrap; max-width: 100%; margin-top: 4px; overflow-x: auto; overscroll-behavior: contain; }
  .event-legend, .glossary { display: none; }
  .latest { position: relative; top: auto; right: auto; min-width: 0; margin-left: auto; }
  .composer { position: fixed; left: 0; right: 0; bottom: 0; width: 100%; min-height: 164px; max-height: min(300px, 45dvh); margin: 0; padding: 12px 16px calc(14px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); box-shadow: 0 -12px 28px #0008; }
  .drawer.open { position: fixed; z-index: 12; inset: 76px 12px 12px; display: block; max-height: none; overflow-y: auto; border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 18px 36px #000b; }
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
    <button id="rail-close" class="rail-close" type="button" aria-label="Close conversation rooms">Close</button>
    <div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span>SUMMON</span></div>
    <div class="rail-heading"><h2>Conversation atlas</h2><span id="room-count">0</span></div>
    <input id="room-search" class="search" type="search" autocomplete="off" placeholder="Filter rooms" aria-label="Filter rooms">
    <div id="rooms" class="room-groups"><p class="empty">Loading conversations…</p></div>
    <p class="rail-credit"><strong>Built with a visual adaptation of <a href="https://www.beautifului.dev/" target="_blank" rel="noreferrer">Beautiful UI</a>.</strong><br>MIT-licensed reference; Summon keeps its own journal, security boundary, and runtime.</p>
  </nav>
  <main class="thread" aria-labelledby="title">
    <header class="topbar">
      <button id="rail-open" class="mobile-rail" type="button" aria-label="Open conversation rooms" aria-controls="rail" aria-expanded="false">Rooms</button>
      <div class="room-title"><h1 id="title">Choose a conversation</h1><div id="room-subtitle" class="room-subtitle">Conversations are grouped by project and the agent that started them.</div><div id="room-intent" class="room-intent">Choose a conversation to read its recorded timeline and, when needed, ask one roster agent for bounded help.</div></div>
      <div class="status-stack"><div id="connection" class="status offline" role="status" aria-live="polite">Connecting to local owner</div><div id="turn-status" class="turn-status" role="status" aria-live="polite">No active provider turn</div></div>
    </header>
    <div id="notice" class="notice" role="alert" hidden><span id="notice-text"></span><button id="notice-retry" type="button">Retry</button></div>
    <div class="thread-body">
        <section class="room-bar" aria-label="Current conversation">
        <div class="room-bar-label"><strong id="room-label">No conversation selected</strong><span id="room-origin" class="room-origin">LOCAL · REDACTED VIEW</span><div class="identity-heading"><span>Agents and models</span><span id="identity-summary">Role · name/version · model · provider</span></div><p class="identity-help">Select a card to ask that exact roster seat. The model line is the declared target; served identity appears in the timeline after a provider receipt.</p><div id="participants" class="participants"></div></div>
        <div class="facts"><span>events <b id="cursor">—</b></span><span>type <b id="mode">—</b></span><span data-live="true">updated <b id="updated">—</b></span><span><b id="working-count">0</b> working</span><button id="evidence-open" class="drawer-toggle" type="button" aria-controls="drawer" aria-expanded="false">Room details</button></div>
      </section>
      <div class="activity-dock" aria-label="Active provider work"><div id="working" class="working" aria-live="off"></div><section id="turn-recovery" class="turn-recovery" hidden aria-live="polite" aria-labelledby="turn-recovery-title">
        <strong id="turn-recovery-title">This agent turn needs attention</strong>
        <p id="turn-recovery-copy">The last agent turn did not close cleanly. Choose how to close it; Summon will not retry the provider automatically.</p>
        <div id="turn-recovery-facts" class="turn-recovery-facts"></div>
        <div class="turn-recovery-actions"><button id="turn-recover" class="recover" type="button">Mark blocked and close</button><button id="turn-fork" class="fork" type="button">Start a fresh conversation</button></div>
      </section></div>
      <div class="timeline-head"><div><strong>Recorded timeline</strong><small id="timeline-state"> waiting for a conversation</small><div class="stream-filters" role="group" aria-label="Filter recorded events"><button class="stream-filter" type="button" data-event-filter="all" aria-pressed="true">All</button><button class="stream-filter" type="button" data-event-filter="context" aria-pressed="false">Context</button><button class="stream-filter" type="button" data-event-filter="run" aria-pressed="false">Agent work</button><button class="stream-filter" type="button" data-event-filter="receipt" aria-pressed="false">Receipts</button></div><div class="event-legend" aria-label="Event legend"><span><b>Context</b> operator and agent notes</span><span><b>Agent work</b> provider turn started or finished</span><span><b>Receipt</b> recorded evidence and cleanup</span><span class="proof-match"><b>✓</b> model matches roster</span><span class="proof-drift"><b>↗</b> model differs from roster</span><span class="proof-awaiting"><b>…</b> waiting for provider receipt</span></div><details class="glossary"><summary>How to read this conversation</summary><p><b>Events</b> is the number of recorded entries. <b>Roster model</b> is what Summon expected; <b>served model</b> is what the provider reported. “Not verified” means one is missing, so Summon makes no match claim. A <b>fresh conversation</b> means continuation was refused and no provider session was resumed. <b>Shortcuts</b>: Ctrl/Cmd+Enter submits or opens review; / or Ctrl/Cmd+K focuses room search; Esc closes overlays.</p></details></div><button id="latest" class="latest" type="button" hidden>Jump to latest event</button></div>
      <ol id="timeline" class="timeline" role="log" aria-label="Recorded conversation timeline" aria-live="off"><li class="timeline-empty">Choose a conversation to read its recorded timeline.</li></ol>
      <section id="composer" class="composer" aria-labelledby="composer-title">
        <div class="composer-tabs" role="tablist" aria-label="Conversation action">
          <button id="context-tab" class="tab" role="tab" aria-controls="composer-panel" aria-selected="true" tabindex="0" type="button">Post context</button>
          <button id="agent-tab" class="tab agent" role="tab" aria-controls="composer-panel" aria-selected="false" tabindex="-1" type="button">Ask an agent</button>
        </div>
        <h2 id="composer-title" hidden>Conversation composer</h2>
        <div id="composer-panel" class="compose-grid" role="tabpanel" aria-labelledby="context-tab" tabindex="-1">
          <div>
            <div id="agent-picker" class="agent-picker" hidden><button id="agent-picker-button" class="agent-picker-button" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="agent-picker-menu"><span id="agent-picker-label" class="agent-picker-label">Choose an agent</span><span id="agent-picker-meta" class="agent-picker-meta">MODEL · —</span><span class="agent-picker-chevron" aria-hidden="true">⌄</span></button><div id="agent-picker-menu" class="agent-picker-menu" role="listbox" aria-label="Choose an agent" hidden></div></div>
            <select id="participant" aria-label="Conversation participant" hidden><option value="">Choose an agent</option></select>
            <label class="visually-hidden" for="message">Message</label><textarea id="message" maxlength="12000" placeholder="Add context for this conversation…"></textarea>
            <div class="composer-caption"><span id="caption">Saved as room context. It cannot approve or change a run.</span><span id="counter">0 / 12000</span></div>
            <div id="launch-review" class="launch-review" hidden aria-live="polite"><strong>Review agent request</strong><p id="review-copy">Choose an agent and review the model target before starting.</p><p id="review-facts" class="review-facts"></p><div class="review-actions"><button id="review-back" type="button">Keep editing</button><button id="review-confirm" class="confirm" type="button">Start this agent turn</button></div></div>
          </div>
          <div class="compose-actions"><button id="send" class="primary" type="button" disabled>Post context</button><button id="run-cancel" type="button" hidden disabled>Cancel turn</button></div>
        </div>
        <p id="note" class="composer-note" role="status" aria-live="polite"></p>
      </section>
    </div>
  </main>
  <aside id="drawer" class="drawer" role="complementary" aria-labelledby="drawer-title" aria-describedby="drawer-boundary">
    <button id="drawer-close" class="drawer-close" type="button" aria-label="Close evidence">Close</button>
    <div class="drawer-section"><h2 id="drawer-title">Room details</h2><p class="drawer-label">what this page can show</p><div id="drawer-boundary" class="boundary" data-authority="context + explicit turns"><strong>Context and agent turns</strong>Messages you post are context only. This page cannot approve, vote, or change a deliberate run. Starting an agent turn launches one bounded provider process after you review the selected model.</div></div>
    <div class="drawer-section"><p class="drawer-label">conversation facts</p><dl class="fact-list"><div><dt>Project</dt><dd id="fact-project">No conversation loaded</dd></div><div><dt>Project fingerprint</dt><dd id="fact-root">—</dd></div><div><dt>Started by</dt><dd id="fact-initiator">—</dd></div><div><dt>Agents and models</dt><dd id="fact-roster">—</dd></div><div><dt>Recorded events</dt><dd id="fact-journal">—</dd></div></dl></div>
    <div class="drawer-section"><p class="drawer-label">privacy</p><p class="muted">Local-only and redacted. Private prompts, paths, credentials, native output, and provider controls stay outside this view.</p></div>
  </aside>
</div>
<div id="rail-backdrop" class="rail-backdrop" hidden></div>
<div id="action-dialog" class="action-dialog" hidden role="dialog" aria-modal="true" aria-labelledby="action-dialog-title" aria-describedby="action-dialog-copy">
  <div class="action-dialog-card">
    <h2 id="action-dialog-title">Confirm action</h2>
    <p id="action-dialog-copy"></p>
    <input id="action-dialog-input" type="text" hidden maxlength="12000">
    <div class="action-dialog-actions"><button id="action-dialog-cancel" type="button">Keep editing</button><button id="action-dialog-confirm" class="confirm" type="button">Confirm</button></div>
  </div>
</div>
<div id="announce" class="visually-hidden" role="status" aria-live="polite"></div>
<script nonce="__NONCE__">__JS__</script>
</body>
</html>'''


_JS = r'''(() => {
  const ROOM_TIMEOUT_MS = __TIMEOUT_MS__;
  const token = location.hash.startsWith('#token=') ? location.hash.slice(7) : '';
  const headers = token ? {Authorization: 'Bearer ' + token} : {};
  const state = {
    selected: '', room: null, events: [], cursor: 0, rooms: {}, query: '', mode: 'context', eventFilter: 'all',
    active: {}, streamAbort: null, streamRetry: null, refreshTimer: null, ageTimer: null, streamCursor: 0,
    unread: {}, lastRoomIndex: '', drafts: {}, lastUpdated: null, streamLive: false, connectionKind: 'offline', feedPinned: true, railOpen: false, evidenceOpen: false, reviewing: false, recovery: null,
    dialogResolve: null, dialogReturnFocus: null
  };
  const $ = id => document.getElementById(id);
  const text = value => document.createTextNode(value == null ? '' : String(value));
  const key = (session, participant) => String(session) + '\u001f' + String(participant);
  const title = value => String(value || '').replaceAll('_', ' ');
  const modeLabel = value => String(value || 'chat').toUpperCase();
  const shortHash = value => value ? String(value).slice(0, 12) + '…' : '—';
  function eventTime(value) { const ms = Number(value); if (!Number.isFinite(ms) || ms <= 0) return ''; const date = new Date(ms); return date.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}); }
  function announce(value) { $('announce').replaceChildren(text(value || '')); }
  function setConnection(label, kind) { const nextKind = kind || ''; if (state.connectionKind === nextKind && $('connection').textContent === String(label)) return; state.connectionKind = nextKind; const node = $('connection'); node.className = 'status ' + nextKind; node.replaceChildren(text(label)); }
  function setNotice(message, retry = false) { const box = $('notice'); $('notice-text').replaceChildren(text(message || '')); box.hidden = !message; $('notice-retry').hidden = !retry; }
  function closeActionDialog(result) {
    const resolve = state.dialogResolve; state.dialogResolve = null;
    $('action-dialog').hidden = true; $('action-dialog-input').hidden = true;
    const focus = state.dialogReturnFocus; state.dialogReturnFocus = null;
    if (focus && focus.isConnected) focus.focus();
    if (resolve) resolve(result);
  }
  function openActionDialog(titleText, copy, options = {}) {
    return new Promise(resolve => {
      state.dialogResolve = resolve; state.dialogReturnFocus = document.activeElement;
      $('action-dialog-title').replaceChildren(text(titleText)); $('action-dialog-copy').replaceChildren(text(copy));
      const field = $('action-dialog-input'); const input = Boolean(options.input); field.hidden = !input; field.value = input ? String(options.defaultValue || '') : '';
      $('action-dialog-confirm').replaceChildren(text(options.confirmLabel || 'Confirm')); $('action-dialog').hidden = false;
      (input ? field : $('action-dialog-confirm')).focus();
    });
  }
  function setRailOpen(open) {
    state.railOpen = Boolean(open);
    if (state.railOpen && state.evidenceOpen) setEvidenceOpen(false, false);
    $('rail').classList.toggle('open', state.railOpen);
    $('rail-backdrop').hidden = !(state.railOpen || state.evidenceOpen);
    $('rail-open').setAttribute('aria-expanded', state.railOpen ? 'true' : 'false');
    if (state.railOpen) $('rail-close').focus();
    else if (window.matchMedia('(max-width: 760px)').matches) $('rail-open').focus();
  }
  function setEvidenceOpen(open, restoreFocus = true) {
    const mobile = window.matchMedia('(max-width: 760px)').matches;
    const tablet = window.matchMedia('(max-width: 1120px)').matches;
    state.evidenceOpen = (mobile || tablet) && Boolean(open);
    const drawer = $('drawer'); drawer.classList.toggle('open', state.evidenceOpen);
    $('evidence-open').setAttribute('aria-expanded', state.evidenceOpen ? 'true' : 'false');
    $('drawer').setAttribute('role', mobile && state.evidenceOpen ? 'dialog' : 'complementary');
    $('drawer').setAttribute('aria-modal', mobile && state.evidenceOpen ? 'true' : 'false');
    $('drawer').setAttribute('aria-hidden', (mobile || tablet) && !state.evidenceOpen ? 'true' : 'false');
    $('rail-backdrop').hidden = !(state.railOpen || state.evidenceOpen);
    if (state.evidenceOpen && mobile) $('drawer-close').focus();
    else if (state.evidenceOpen && tablet) { $('drawer-close').focus(); announce('Room evidence opened'); }
    else if (restoreFocus) $('evidence-open').focus();
  }
  function saveDraft() { if (state.selected) state.drafts[state.selected] = $('message').value; }
  function loadDraft() { $('message').value = state.drafts[state.selected] || ''; $('counter').replaceChildren(text(String($('message').value.length) + ' / 12000')); }
  function markUpdated() { state.lastUpdated = Date.now(); $('updated').replaceChildren(text('now')); }
  function relativeAge() { if (!state.lastUpdated) return 'waiting for a room'; const seconds = Math.max(0, Math.floor((Date.now() - state.lastUpdated) / 1000)); return seconds < 5 ? ' just now' : ' ' + seconds + 's ago'; }
  function updateAge() { if (!state.room || !state.lastUpdated) return; const age = Math.max(0, Math.floor((Date.now() - state.lastUpdated) / 1000)); $('updated').replaceChildren(text(relativeAge().trim())); $('timeline-state').replaceChildren(text(' · cursor ' + String(state.cursor || 0) + relativeAge())); if (age >= 30 && state.connectionKind === 'connected') setConnection('Live · stale', 'degraded'); if (Object.keys(state.active).length) updateWorkingElapsed(); }
  async function getJSON(url, options = {}) {
    const requestHeaders = Object.assign({}, headers, options.headers || {});
    const response = await fetch(url, Object.assign({}, options, {headers: requestHeaders, cache: 'no-store'}));
    const raw = await response.text(); let data = {};
    try { data = raw ? JSON.parse(raw) : {}; } catch (_) { data = {}; }
    if (!response.ok) throw new Error(data.error || 'request refused');
    return data;
  }
  function publicError(error, fallback) { const raw = error && typeof error.message === 'string' ? error.message.replaceAll(/(?:[A-Za-z]:[\\/]|\/)[^\s]{2,}/g, '[path]').slice(0, 140) : ''; return raw && raw !== 'request refused' ? fallback + ' · ' + raw : fallback; }
  function humanizeAgent(value) { return String(value || 'agent').replaceAll(/[-_.]+/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase()); }
  function compactText(value, limit = 72) {
    const clean = String(value || '').replaceAll(/\s+/g, ' ').trim();
    if (!clean) return '';
    if (clean.length <= limit) return clean;
    const clipped = clean.slice(0, limit + 1).replace(/\s+\S*$/, '').trim();
    return (clipped || clean.slice(0, limit).trim()) + '…';
  }
  function roomSubject(room) { return compactText(room && (room.subject || room.title || ''), 50); }
  function roomContext(room) { return compactText(room && (room.subject || room.title || ''), 180); }
  function identityFrom(found, fallback = 'agent') {
    const item = found && typeof found === 'object' ? found : {};
    const agent = String(item.agent || fallback || 'agent');
    return {agent, role: String(item.role || 'roster seat'), name: String(item.name || humanizeAgent(agent)), version: String(item.version || ''), model: String(item.model || ''), provider: String(item.provider || '')};
  }
  function identity(participant) {
    const list = state.room && Array.isArray(state.room.participants) ? state.room.participants : [];
    const found = list.find(item => String(item.agent || '') === String(participant || '')) || {};
    return identityFrom(found, participant);
  }
  function identityLabel(participant, includeVersion = true) {
    const item = identity(participant);
    return [item.name, includeVersion ? item.version : ''].filter(Boolean).join(' · ');
  }
  function identityModelLabel(participant) {
    const item = identity(participant);
    return item.model || 'Model not verified';
  }
  function modelProof(payload, participant) {
    const target = String(payload && payload.model_target || '');
    const served = String(payload && payload.model_served || '');
    if (!target && !served) return {label: 'MODEL NOT VERIFIED', className: 'unsealed', caption: 'No roster target or provider receipt is available, so no model match is claimed.', title: 'No declared target or served model evidence is present.'};
    if (!target && served) return {label: 'SERVED MODEL · ' + served, className: 'unsealed', caption: 'The provider reported a model, but this room has no roster target to compare.', title: 'A served identity exists, but no declared target is available for a match.'};
    if (!served) return {label: 'WAITING FOR MODEL RECEIPT · ' + (target || 'target missing'), className: 'awaiting', caption: 'The roster target is declared; the provider has not reported the served model yet.', title: 'The roster target is declared; provider-served identity has not been recorded.'};
    if (target && target !== served) return {label: 'MODEL DIFFERS · ' + target + ' → ' + served, className: 'drift', caption: 'The provider reported a different model; review before continuing.', title: 'The provider-served identity differs from the roster target.'};
    return {label: 'MODEL MATCH · ' + served, className: 'matched', caption: 'The roster target and provider-served model agree.', title: 'Declared target and provider-served identity match.'};
  }
  function identityTooltip(participant) {
    const item = identity(participant);
    const facts = ['agent id ' + item.agent, 'role ' + item.role, 'name ' + item.name];
    if (item.version) facts.push('version ' + item.version);
    facts.push(item.model ? 'roster model ' + item.model : 'model not verified in this conversation');
    if (item.provider) facts.push('provider ' + item.provider);
    return facts.join(' · ');
  }
  function closeAgentPicker() {
    const menu = $('agent-picker-menu'); const button = $('agent-picker-button');
    if (!menu || menu.hidden) return;
    menu.hidden = true; button.setAttribute('aria-expanded', 'false');
  }
  function selectParticipant(participant) {
    const value = String(participant || ''); $('participant').value = value; state.reviewing = false; closeAgentPicker(); updateAgentPicker(); updateComposer(); $('message').focus(); if (value) announce('Selected ' + identityLabel(value) + ' · ' + identityModelLabel(value));
  }
  function updateAgentPicker() {
    const box = $('agent-picker'); const menu = $('agent-picker-menu'); const button = $('agent-picker-button'); const chooser = $('participant');
    const context = state.mode !== 'agent'; const item = chooser.value ? identity(chooser.value) : null;
    box.hidden = context; button.disabled = !state.selected || !Array.isArray(state.room && state.room.participants) || !state.room.participants.length;
    $('agent-picker-label').replaceChildren(text(item ? item.role + ' · ' + item.name + (item.version ? ' · ' + item.version : '') : 'Choose an agent'));
    $('agent-picker-meta').replaceChildren(text(item ? 'MODEL · ' + (item.model || 'not verified') + ' · PROVIDER · ' + (item.provider || 'not recorded') : 'MODEL · —'));
    menu.querySelectorAll('.agent-option').forEach(option => { const selected = option.dataset.agent === chooser.value; option.setAttribute('aria-selected', selected ? 'true' : 'false'); });
    if (context) closeAgentPicker();
  }
  function renderAgentPicker() {
    const menu = $('agent-picker-menu'); menu.replaceChildren();
    const participants = Array.isArray(state.room && state.room.participants) ? state.room.participants : [];
    participants.forEach(raw => {
      const item = identityFrom(raw, raw && raw.agent); const option = document.createElement('button'); option.type = 'button'; option.className = 'agent-option'; option.setAttribute('role', 'option'); option.dataset.agent = item.agent; option.setAttribute('aria-selected', 'false'); option.title = identityTooltip(item.agent);
      const name = document.createElement('span'); name.className = 'agent-option-name'; name.append(text(item.name + (item.version ? ' · ' + item.version : '')));
      const role = document.createElement('span'); role.className = 'agent-option-role'; role.append(text(item.role));
      const model = document.createElement('span'); model.className = 'agent-option-model'; model.append(text((item.model || 'MODEL · not verified') + (item.provider ? ' · ' + item.provider : '')));
      option.append(name, role, model); option.addEventListener('click', () => selectParticipant(item.agent)); option.addEventListener('keydown', event => { if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return; event.preventDefault(); const options = Array.from(menu.querySelectorAll('.agent-option')); const index = options.indexOf(option); options[(index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus(); }); menu.append(option);
    });
    updateAgentPicker();
  }
  function toggleAgentPicker() {
    const menu = $('agent-picker-menu'); const button = $('agent-picker-button'); if (button.disabled) return;
    const open = menu.hidden; menu.hidden = !open; button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) { const selected = menu.querySelector('[aria-selected="true"]') || menu.querySelector('.agent-option'); selected?.focus(); }
  }
  function roomEntries(groups = state.rooms) {
    const result = [];
    Object.values(groups || {}).forEach(initiators => Object.values(initiators || {}).forEach(entries => (entries || []).forEach(room => result.push(room))));
    return result;
  }
  function filteredRoom(room) {
    const needle = state.query.trim().toLowerCase();
    if (!needle) return true;
    const rosterText = (Array.isArray(room.participants) ? room.participants : []).flatMap(item => [item.agent, item.role, item.name, item.version, item.model, item.provider]);
    return [room.session_id, room.subject, room.title, room.project_id, room.initiator_host, room.initiator_agent, room.mode, ...rosterText].join(' ').toLowerCase().includes(needle);
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
        const visibleRooms = (rooms || []).filter(filteredRoom); if (!visibleRooms.length) return;
        const who = document.createElement('div'); who.className = 'initiator-line'; who.append(text(initiator)); group.append(who);
        visibleRooms.forEach(room => {
          count += 1; if (!first) first = room.session_id;
          const button = document.createElement('button'); button.type = 'button'; button.className = 'room-button' + (room.session_id === state.selected ? ' active' : ''); button.dataset.session = room.session_id; if (room.session_id === state.selected) button.setAttribute('aria-current', 'page');
          const name = document.createElement('span'); name.className = 'room-name'; name.append(text(roomSubject(room) || (modeLabel(room.mode) + ' · ' + String(room.initiator_agent || 'conversation'))));
          const session = document.createElement('span'); session.className = 'room-session'; session.append(text(String(room.session_id || 'room')));
          const roomModels = document.createElement('span'); roomModels.className = 'room-models';
          const roster = Array.isArray(room.participants) ? room.participants : [];
          const rosterSummary = roster.slice(0, 3).map(item => { const seat = identityFrom(item, item.agent); return seat.role + ' · ' + seat.name + ' · ' + (seat.model || 'model not verified') + ' · ' + (seat.provider || 'provider not recorded'); });
          roomModels.append(text(rosterSummary.length ? 'MODEL · ' + rosterSummary.join('  /  ') + (roster.length > 3 ? '  /  +' + String(roster.length - 3) : '') : 'MODEL · none recorded'));
          const detail = document.createElement('span'); detail.className = 'room-detail'; const chip = document.createElement('span'); chip.className = 'mode-chip'; chip.append(text(modeLabel(room.mode))); detail.append(chip);
          const countText = document.createElement('span'); countText.append(text(String(room.cursor || 0) + ' events')); detail.append(countText);
          if (state.unread[room.session_id] && room.session_id !== state.selected) { const dot = document.createElement('span'); dot.className = 'unread'; dot.title = 'New events'; dot.setAttribute('aria-label', 'Unread events'); const unreadText = document.createElement('span'); unreadText.className = 'visually-hidden'; unreadText.append(text('Unread events')); detail.append(dot, unreadText); }
          button.setAttribute('aria-label', modeLabel(room.mode) + ' conversation by ' + String(room.initiator_agent || 'initiator') + ' · session ' + String(room.session_id) + ' · ' + (rosterSummary.length ? rosterSummary.join(', ') : 'no models recorded') + ' · ' + String(room.cursor || 0) + ' recorded events' + (state.unread[room.session_id] && room.session_id !== state.selected ? ' · unread events' : '')); button.append(name, session, roomModels, detail); button.addEventListener('click', () => select(room.session_id)); group.append(button);
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
    const roster = Array.isArray(room.participants) ? room.participants : [];
    const rawSubject = room.subject || room.title || '';
    const fallbackTitle = room.project_id ? String(room.project_id) + ' · ' + modeLabel(room.mode) : 'Choose a conversation';
    const intent = roomSubject(room) || fallbackTitle;
    $('title').replaceChildren(text(intent)); $('title').setAttribute('title', rawSubject || fallbackTitle);
    $('room-label').replaceChildren(text(room.session_id ? modeLabel(room.mode) + ' · ' + String(room.initiator_agent || 'conversation') : 'No room selected'));
    $('room-subtitle').replaceChildren();
    if (room.session_id) { const session = document.createElement('span'); session.className = 'session-id'; session.append(text(String(room.session_id) + ' · ' + String(room.initiator_host || 'host') + ' / ' + String(room.initiator_agent || 'agent'))); $('room-subtitle').append(session); }
    else $('room-subtitle').append(text('Conversations are grouped by project and the agent that started them.'));
    const contextSummary = roomContext(room);
    const intentText = room.session_id ? (contextSummary ? 'Context: ' + contextSummary : 'Context: No opening prompt recorded.') : 'Choose a conversation to read its recorded timeline and, when needed, ask one roster agent for bounded help.';
    $('room-intent').replaceChildren(text(room.session_id ? (contextSummary ? 'Context · ' + contextSummary : 'Context · No opening prompt recorded.') : 'Choose a conversation to read its recorded timeline and, when needed, ask one agent for bounded help.'));
    $('room-intent').replaceChildren(text(intentText)); $('room-intent').setAttribute('title', rawSubject || intentText); $('room-intent').setAttribute('aria-label', rawSubject ? 'Conversation context: ' + rawSubject : intentText);
    $('cursor').replaceChildren(text(room.session_id ? String(state.cursor || 0) : '—'));
    $('mode').replaceChildren(text(room.session_id ? modeLabel(room.mode) : '—'));
    $('timeline-state').replaceChildren(text(room.session_id ? ' · cursor ' + String(state.cursor || 0) + relativeAge() : ' waiting for a room'));
    $('fact-project').replaceChildren(text(room.project_id || 'No room loaded'));
    $('fact-root').replaceChildren(text(shortHash(room.project_root_sha256)));
    $('fact-initiator').replaceChildren(text(room.session_id ? String(room.initiator_host || 'host') + ' / ' + String(room.initiator_agent || 'agent') : '—'));
    $('fact-journal').replaceChildren(text(room.session_id ? String(state.cursor || 0) + ' recorded events' : '—'));
    const unresolved = roster.filter(item => !item.model).length;
    $('fact-roster').replaceChildren(text(roster.length ? String(roster.length) + ' agents · ' + (unresolved ? String(unresolved) + ' model not verified' : 'models declared') : '—'));
    $('identity-summary').replaceChildren(text(roster.length ? (unresolved ? String(unresolved) + ' model not verified · model shown per card' : 'model shown per card · served receipt in timeline') : 'no agents recorded'));
    const chips = $('participants'); chips.replaceChildren();
    roster.forEach(participant => {
      const item = identityFrom(participant, participant.agent);
      const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'participant' + (item.model ? '' : ' unresolved identity-tooltip'); chip.title = identityTooltip(item.agent); chip.setAttribute('aria-label', 'Ask ' + item.role + ' ' + item.name + (item.model ? ' on ' + item.model : '') + (item.provider ? ' via ' + item.provider : '') + ' to start an agent turn'); chip.addEventListener('click', () => { state.mode = 'agent'; selectParticipant(item.agent); });
      const role = document.createElement('span'); role.className = 'participant-role'; role.append(text(item.role));
      const name = document.createElement('strong'); name.className = 'participant-name'; name.append(text(item.name + (item.version ? ' · ' + item.version : '')));
      const model = document.createElement('span'); model.className = 'participant-model'; model.append(text(item.model ? 'MODEL · ' + item.model : 'MODEL · not verified'));
      const route = document.createElement('span'); route.className = 'participant-provider'; route.append(text(item.provider ? 'PROVIDER · ' + item.provider : 'PROVIDER · not recorded'));
      chip.append(role, name, model, route); chips.append(chip);
    });
  }
  function payloadPreview(record) {
    const payload = record && record.payload || {};
    return payload.preview || payload.summary || (payload.text_chars ? 'Human context · ' + String(payload.text_chars) + ' chars' : '');
  }
  function eventLabel(value) {
    const labels = {session_created: 'Conversation opened', council_round_started: 'Council round started', position_submitted: 'Position recorded', cross_exam: 'Cross-examination', chair_synthesis: 'Chair synthesis', turn_started: 'Agent turn started', turn_finished: 'Agent turn finished', fork_created: 'Fresh conversation created', message_posted: 'Context note', turn_cancel_requested: 'Stop request recorded', cleanup_receipt: 'Cleanup recorded'};
    return labels[String(value || '')] || title(value);
  }
  function detailFor(record) {
    const details = document.createElement('details'); const summary = document.createElement('summary'); summary.append(text('Evidence · cursor ' + String(record.cursor || '—'))); details.append(summary);
    const body = document.createElement('div'); body.className = 'evidence';
    const payload = record.payload || {}; const parts = ['public redacted', 'generation ' + String(record.generation || 1), 'payload ' + shortHash(record.payload_sha256)];
    if (payload.provider) parts.push('route ' + String(payload.provider)); if (payload.model_target) parts.push('target ' + String(payload.model_target)); if (payload.model_served) parts.push('served ' + String(payload.model_served)); if (payload.transport) parts.push('transport ' + String(payload.transport));
    body.append(text(parts.join(' · '))); details.append(body); return details;
  }
  function eventIdentity(record) {
    const payload = record.payload || {}; return payload.participant || record.actor_id || payload.asker || 'system';
  }
  function feedPinned() {
    const feed = $('timeline'); if (!feed) return true;
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 96;
  }
  function captureFeedAnchor() {
    const feed = $('timeline'); if (!feed) return {pinned: true, cursor: null, offset: 0};
    const feedRect = feed.getBoundingClientRect();
    const row = Array.from(feed.querySelectorAll('[data-cursor]')).find(node => {
      const rect = node.getBoundingClientRect(); return rect.bottom > feedRect.top + 1;
    });
    return {pinned: feedPinned(), cursor: row ? String(row.dataset.cursor || '') : null,
      offset: row ? row.getBoundingClientRect().top - feedRect.top : 0,
      scrollTop: feed.scrollTop, scrollHeight: feed.scrollHeight};
  }
  function restoreFeedAnchor(anchor) {
    const feed = $('timeline'); if (!feed || !anchor) return;
    requestAnimationFrame(() => {
      if (anchor.pinned) { feed.scrollTop = feed.scrollHeight; state.feedPinned = true; return; }
      const row = anchor.cursor && feed.querySelector('[data-cursor="' + CSS.escape(anchor.cursor) + '"]');
      if (row) {
        const feedRect = feed.getBoundingClientRect();
        feed.scrollTop += row.getBoundingClientRect().top - feedRect.top - anchor.offset;
      } else {
        feed.scrollTop = Math.max(0, Math.min(feed.scrollHeight - feed.clientHeight, anchor.scrollTop));
      }
      state.feedPinned = feedPinned();
    });
  }
  function renderEvent(record) {
    const event = String(record.event || 'event'); const payload = record.payload || {}; const who = eventIdentity(record); const preview = payloadPreview(record);
    const agentPost = event === 'agent_message' || (event === 'message_posted' && Boolean(payload.sender || payload.recipient || payload.participant));
    if (event === 'human_message' || agentPost) {
      const article = document.createElement('li'); article.className = 'message ' + (event === 'human_message' ? 'human' : 'agent');
      const head = document.createElement('div'); head.className = 'message-head'; const strong = document.createElement('strong'); const heading = event === 'human_message' ? 'Operator context' : identityLabel(payload.sender || who); strong.append(text(heading)); const model = document.createElement('span');
      if (event === 'human_message') { model.className = 'message-model'; model.append(text('human context')); }
      else { const proof = modelProof(payload, payload.sender || who); model.className = 'message-model ' + proof.className; model.title = proof.title; model.append(text(proof.label)); }
      const stamp = eventTime(record.created_at_ms); if (stamp) { const time = document.createElement('time'); time.className = 'event-time'; time.dateTime = new Date(Number(record.created_at_ms)).toISOString(); time.append(text(stamp)); head.append(strong, model, time); } else head.append(strong, model);
      const bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.append(text(preview || 'Redacted event')); const meta = document.createElement('div'); meta.className = 'message-meta'; meta.append(text(event === 'human_message' ? String(payload.text_chars || 0) + ' chars · ' + shortHash(payload.text_sha256) : identity(payload.sender || who).role + ' · ' + (identity(payload.sender || who).provider || 'route not recorded') + ' → ' + identityLabel(payload.recipient || 'human', false) + ' · context only'));
      article.dataset.cursor = String(record.cursor || ''); article.append(head, bubble, meta, detailFor(record)); return article;
    }
    const familyMap = {council_round_started: 'round', position_submitted: 'position', cross_exam: 'cross', chair_synthesis: 'synthesis', turn_started: 'turn', turn_finished: 'finish', turn_cancel_requested: 'cancel', cleanup_receipt: 'cleanup', fork_created: 'fork', message_posted: 'context'};
    const family = familyMap[event] || 'default';
    const shortMap = {turn_started: 'RUN', turn_finished: 'END', turn_cancel_requested: 'STOP', cleanup_receipt: 'CLEAN', message_posted: 'NOTE', council_round_started: 'RND', position_submitted: 'POS', cross_exam: 'ASK', chair_synthesis: 'SYN', fork_created: 'FORK'};
    const item = document.createElement('li'); item.className = 'system-event ' + family;
    const chip = document.createElement('div'); chip.className = 'system-chip';
    const mark = document.createElement('span'); mark.className = 'event-mark';
    mark.setAttribute('aria-label', eventLabel(event)); mark.title = eventLabel(event); mark.append(text(shortMap[event] || 'LOG'));
    const copy = document.createElement('div'); const strong = document.createElement('strong'); strong.append(text(eventLabel(event)));
    const small = document.createElement('small'); const summary = preview || (record.actor_kind === 'system' || who === 'system' ? 'system journal event' : identityLabel(who, false) + ' · ' + identityModelLabel(who)); const stamp = eventTime(record.created_at_ms); small.append(text(' · ' + summary + (stamp ? ' · ' + stamp : ''))); copy.append(strong, small);
    if (event === 'turn_started' || event === 'turn_finished') {
      const proof = event === 'turn_started'
        ? modelProof(payload, who)
        : modelProof(payload, who);
      const proofNode = document.createElement('span'); proofNode.className = 'system-proof ' + proof.className; proofNode.title = proof.title; proofNode.append(text(proof.label)); copy.append(proofNode);
      const captionNode = document.createElement('span'); captionNode.className = 'proof-caption'; captionNode.append(text(proof.caption)); copy.append(captionNode);
    }
    item.dataset.cursor = String(record.cursor || ''); chip.append(mark, copy, detailFor(record)); item.append(chip); return item;
  }
  function eventBucket(record) {
    const event = String(record && record.event || '');
    if (['human_message', 'message_posted', 'agent_message', 'position_submitted', 'cross_exam', 'chair_synthesis'].includes(event)) return 'context';
    if (['turn_started', 'turn_finished', 'turn_cancel_requested', 'fork_created'].includes(event)) return 'run';
    return 'receipt';
  }
  function visibleEvents() { return state.eventFilter === 'all' ? state.events : state.events.filter(record => eventBucket(record) === state.eventFilter); }
  function updateEventFilters() { document.querySelectorAll('[data-event-filter]').forEach(button => button.setAttribute('aria-pressed', button.dataset.eventFilter === state.eventFilter ? 'true' : 'false')); }
  function renderTimeline(preserve = true) {
    const anchor = preserve ? captureFeedAnchor() : null;
    const timeline = $('timeline'); timeline.replaceChildren();
    const events = visibleEvents();
    if (!events.length) { const empty = document.createElement('li'); empty.className = 'timeline-empty'; empty.append(text(state.events.length ? 'No events match this filter.' : 'No events yet. Add context or ask a roster agent to begin.')); timeline.append(empty); restoreFeedAnchor(anchor); return; }
    const fragment = document.createDocumentFragment(); events.forEach(record => fragment.append(renderEvent(record))); timeline.append(fragment);
    restoreFeedAnchor(anchor);
  }
  function rebuildActive() {
    const previous = state.active || {}; state.active = {};
    state.events.forEach(record => { const payload = record.payload || {}; const participant = payload.participant; if (!participant) return; const activeKey = key(state.selected, participant); if (record.event === 'turn_started') { const recordedStartedAt = Number(record.created_at_ms); state.active[activeKey] = {participant: String(participant), turn_id: payload.turn_id || null, startedAt: (Number.isFinite(recordedStartedAt) && recordedStartedAt > 0 ? recordedStartedAt : (previous[activeKey]?.startedAt || Date.now()))}; } if (record.event === 'turn_finished') delete state.active[activeKey]; });
  }
  function renderWorking() {
    const box = $('working'); box.replaceChildren(); const active = Object.values(state.active); $('working-count').replaceChildren(text(String(active.length)));
    const turnStatus = $('turn-status'); if (turnStatus) { turnStatus.className = 'turn-status' + (active.length ? ' active' : (state.recovery ? ' attention' : '')); turnStatus.replaceChildren(text(active.length ? String(active.length) + ' provider turn' + (active.length === 1 ? '' : 's') + ' in progress' : (state.recovery ? 'Turn needs attention' : 'No active provider turn'))); }
    $('working-count').parentElement.dataset.live = active.length ? 'true' : 'false';
    active.forEach(item => { const row = document.createElement('div'); row.className = 'working-row'; row.dataset.participant = item.participant; const label = document.createElement('span'); label.className = 'working-identity'; const strong = document.createElement('strong'); strong.append(text(identityLabel(item.participant))); const model = document.createElement('span'); model.className = 'working-model'; const elapsed = Math.max(0, Math.floor((Date.now() - Number(item.startedAt || Date.now())) / 1000)); model.append(text(identityModelLabel(item.participant) + ' · working ' + elapsed + 's · durable turn in progress')); label.append(strong, model); const cancel = document.createElement('button'); cancel.type = 'button'; cancel.append(text('Cancel turn')); cancel.addEventListener('click', () => cancelTurn(item.participant)); row.append(label, cancel); box.append(row); });
  }
  function updateWorkingElapsed() { $('working').querySelectorAll('.working-row').forEach(row => { const participant = row.dataset.participant; const item = state.active[key(state.selected, participant)]; if (!item) return; const elapsed = Math.max(0, Math.floor((Date.now() - Number(item.startedAt || Date.now())) / 1000)); const model = row.querySelector('.working-model'); if (model) model.replaceChildren(text(identityModelLabel(participant) + ' · working ' + elapsed + 's · durable turn in progress')); }); }
  function recoveryCandidate() {
    for (let index = state.events.length - 1; index >= 0; index -= 1) {
      const record = state.events[index]; const payload = record.payload || {};
      if (record.event !== 'turn_finished' || !payload.participant) continue;
      const participant = String(payload.participant); if (state.active[key(state.selected, participant)]) return null;
      const status = String(payload.status || ''); const target = String(payload.model_target || ''); const served = String(payload.model_served || '');
      const drift = Boolean(target && served && target !== served);
      if (['blocked', 'error', 'timeout'].includes(status) || drift) return {record, participant, status, drift};
      return null;
    }
    return null;
  }
  function renderRecovery() {
    const candidate = recoveryCandidate(); state.recovery = candidate; const panel = $('turn-recovery'); if (!candidate) { panel.hidden = true; return; }
    panel.hidden = false; const item = identity(candidate.participant); $('turn-recovery-copy').replaceChildren(text(candidate.drift ? 'The provider reported a different model than the roster expected. Start a fresh conversation instead of silently continuing.' : 'The last agent turn did not close cleanly. Choose how to close it; Summon will not retry the provider automatically.')); $('turn-recovery-facts').replaceChildren(text(identityLabel(candidate.participant) + ' · ' + (candidate.status || 'model mismatch') + ' · event ' + String(candidate.record.cursor || '—'))); $('turn-recover').hidden = candidate.drift; $('turn-fork').hidden = false; $('turn-recover').setAttribute('aria-label', 'Mark ' + item.name + ' turn blocked'); $('turn-fork').setAttribute('aria-label', 'Start a fresh conversation from ' + item.name + ' turn');
  }
  function renderRoom(data) {
    const previousRoom = state.room; const previousParticipant = $('participant').value;
    state.room = data.room || {}; state.events = Array.isArray(data.events) ? data.events.slice() : []; state.cursor = Number(data.cursor || state.events.length || 0); state.streamCursor = state.cursor; state.unread[state.selected] = false; $('latest').hidden = true; markUpdated(); rebuildActive(); updateHeader(); loadDraft();
    const chooser = $('participant'); chooser.replaceChildren(); const emptyOption = document.createElement('option'); emptyOption.value = ''; emptyOption.append(text('Choose an agent')); chooser.append(emptyOption);
    const participants = Array.isArray(state.room.participants) ? state.room.participants : [];
    participants.forEach(item => { const option = document.createElement('option'); option.value = String(item.agent || ''); option.title = identityTooltip(item.agent); option.append(text(identityLabel(item.agent) + ' — model: ' + identityModelLabel(item.agent))); chooser.append(option); });
    if (previousRoom && String(previousRoom.session_id || '') === state.selected
        && participants.some(item => String(item.agent || '') === previousParticipant)) chooser.value = previousParticipant;
    renderAgentPicker(); renderTimeline(false); renderRecovery(); renderWorking(); updateComposer(); setNotice('');
    requestAnimationFrame(() => { const feed = $('timeline'); if (feed) feed.scrollTop = feed.scrollHeight; state.feedPinned = true; });
    announce('Opened conversation ' + String(state.room.session_id || ''));
  }
  function appendRecord(record) {
    const cursor = Number(record && record.cursor); if (!Number.isSafeInteger(cursor)) return 'gap';
    if (cursor <= state.cursor) return 'duplicate';
    if (cursor !== state.cursor + 1) return 'gap';
    const anchor = captureFeedAnchor(); state.events.push(record); state.cursor = cursor; state.streamCursor = cursor; markUpdated(); rebuildActive(); updateHeader(); renderRecovery(); renderWorking();
    if (state.eventFilter === 'all') { const timeline = $('timeline'); if (timeline.firstElementChild && timeline.firstElementChild.classList.contains('timeline-empty')) timeline.replaceChildren(); const sentinel = timeline.querySelector('.feed-sentinel'); const node = renderEvent(record); if (sentinel) timeline.insertBefore(node, sentinel); else timeline.append(node); } else renderTimeline(false);
    restoreFeedAnchor(anchor); $('latest').hidden = Boolean(anchor.pinned); announce('New journal event ' + eventLabel(record.event) + ', cursor ' + String(cursor)); return 'appended';
  }
  function updateComposer() {
    const hasRoom = Boolean(state.selected); const context = state.mode === 'context'; const participant = $('participant').value; const item = participant ? identity(participant) : null; const target = item ? item.name + ' · ' + (item.model || 'model not verified') : 'Choose an agent'; const activeCount = Object.keys(state.active).length;
    $('context-tab').setAttribute('aria-selected', context ? 'true' : 'false'); $('context-tab').setAttribute('tabindex', context ? '0' : '-1'); $('agent-tab').setAttribute('aria-selected', context ? 'false' : 'true'); $('agent-tab').setAttribute('tabindex', context ? '-1' : '0'); $('composer-panel').setAttribute('aria-labelledby', context ? 'context-tab' : 'agent-tab'); $('participant').hidden = true; $('participant').disabled = !hasRoom; $('send').hidden = false;
    updateAgentPicker();
    const disconnected = state.connectionKind === 'blocked'; const launchBlocked = !context && disconnected; $('send').disabled = !hasRoom || !$('message').value.trim() || (!context && !$('participant').value) || launchBlocked; $('run-cancel').hidden = context || activeCount !== 1; $('run-cancel').disabled = context || !hasRoom || activeCount !== 1; $('message').placeholder = context ? 'Add context for this conversation…' : 'Send a message to this agent…';
    $('caption').replaceChildren(text(context ? 'Saved as room context. It cannot approve or change a run. Ctrl/Cmd+Enter posts it.' : 'Review before launch · ' + target + (item && item.provider ? ' via ' + item.provider : '') + ' · output is context, not approval. A model mismatch creates a visible fork. Ctrl/Cmd+Enter opens review.')); $('send').classList.toggle('agent-action', !context); $('send').replaceChildren(text(context ? 'Post context' : (state.reviewing ? 'Review open' : 'Review agent request')));
    const review = $('launch-review'); review.hidden = context || !state.reviewing; if (!review.hidden) { $('review-copy').replaceChildren(text(disconnected ? 'Reconnect to the local owner before starting an agent turn. The recorded timeline is not currently available.' : (item ? 'This starts one bounded provider process for the selected agent. Your message remains context only.' : 'Choose an agent before reviewing a request.'))); $('review-facts').replaceChildren(text(item ? ['agent ' + item.agent, 'role ' + item.role, 'model ' + (item.model || 'not verified'), 'provider ' + (item.provider || 'not recorded'), 'permission limited by room runtime', 'stops automatically after ' + formatDuration(ROOM_TIMEOUT_MS)].join(' · ') : 'No agent selected')); $('review-confirm').disabled = !hasRoom || !participant || !$('message').value.trim() || disconnected; }
  }
  function formatDuration(milliseconds) { const minutes = Math.max(1, Math.round(Number(milliseconds || 0) / 60000)); return minutes < 60 ? String(minutes) + 'm' : String(Math.round(minutes / 60)) + 'h'; }
  function stopStream() { if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; } if (state.streamRetry) { clearTimeout(state.streamRetry); state.streamRetry = null; } state.streamLive = false; }
  async function syncRoom() { if (!state.selected) return; const room = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected)); renderRoom(room); }
  async function watch(session, after) {
    stopStream(); const controller = new AbortController(); state.streamAbort = controller; let cursor = Number(after) || 0;
    try {
      const response = await fetch('/api/v1/rooms/' + encodeURIComponent(session) + '/stream?after=' + encodeURIComponent(String(cursor)), {headers, cache: 'no-store', signal: controller.signal});
      if (!response.ok || !response.body || !response.body.getReader) throw new Error('stream unavailable');
      state.streamLive = true; setConnection('Live', 'connected'); setNotice(''); $('note').className = 'composer-note'; $('note').replaceChildren(text('')); const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (state.selected === session && !controller.signal.aborted) {
        const packet = await reader.read(); if (packet.done) break; buffer += decoder.decode(packet.value, {stream: true}); const chunks = buffer.split('\n\n'); buffer = chunks.pop() || '';
        for (const chunk of chunks) { const dataLine = chunk.split('\n').find(line => line.startsWith('data:')); if (!dataLine) continue; let record; try { record = JSON.parse(dataLine.slice(5).trim()); } catch (_) { await syncRoom(); continue; } const result = appendRecord(record); if (result === 'gap') { await syncRoom(); } cursor = state.cursor; }
      }
      if (state.selected === session && !controller.signal.aborted) throw new Error('stream ended');
    } catch (error) {
      if (controller.signal.aborted) return; state.streamLive = false; setConnection('Updates paused', 'degraded'); setNotice('Live updates paused; retrying from recorded event ' + String(state.cursor) + '.', true); $('note').replaceChildren(text('The live connection paused; your recorded timeline is safe.')); state.streamRetry = setTimeout(() => { state.streamRetry = null; if (state.selected === session) watch(session, state.cursor); }, 900);
    }
  }
  async function select(session) { const previous = state.selected; saveDraft(); state.selected = String(session || ''); stopStream(); renderRooms(); try { const data = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected)); renderRoom(data); setConnection('Live', 'connected'); setRailOpen(false); watch(state.selected, state.cursor); } catch (error) { state.selected = previous; renderRooms(); setConnection('Reconnect needed', 'blocked'); setNotice(publicError(error, 'Conversation could not be read; retry when the local owner is available.'), true); announce('Conversation could not be read'); if (previous) watch(previous, state.cursor); } }
  async function loadRooms() { const data = await getJSON('/api/v1/rooms'); const nextRooms = data.rooms || {}; const previous = Object.fromEntries(roomEntries().map(room => [String(room.session_id || ''), room])); const serialized = JSON.stringify(nextRooms); if (serialized !== state.lastRoomIndex) { state.rooms = nextRooms; state.lastRoomIndex = serialized; roomEntries(nextRooms).forEach(room => { const id = String(room.session_id || ''); if (id && id !== state.selected && Number(room.cursor || 0) > Number(previous[id]?.cursor || 0)) state.unread[id] = true; }); renderRooms(); } const selectedRoom = roomEntries(nextRooms).find(room => String(room.session_id || '') === state.selected); const selectedAdvanced = Boolean(selectedRoom && Number(selectedRoom.cursor || 0) > Number(state.cursor || 0)); if (selectedAdvanced && !state.streamLive) syncRoom().catch(() => {}); if (!state.streamRetry) { if (state.streamLive) { setConnection('Live', 'connected'); setNotice(''); } else if (!state.selected) { setConnection('Local owner ready', 'connected'); setNotice(''); } else if (selectedRoom && Number(selectedRoom.cursor || 0) >= Number(state.cursor || 0)) { setConnection('Synced · checking', 'degraded'); setNotice('The recorded room is current; live updates are unavailable.', false); } else { setConnection('Live · checking', 'degraded'); setNotice('Live updates are unavailable; checking the recorded room.', true); } } }
  async function refresh() { try { await loadRooms(); } catch (error) { setConnection('Reconnect needed', 'blocked'); setNotice(publicError(error, 'Conversation list could not be read; retry when the local owner is available.'), true); } }
  async function postContext() { const value = $('message').value; if (!state.selected || !value.trim()) return; $('send').disabled = true; try { await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/messages', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: value})}); $('message').value = ''; saveDraft(); $('note').replaceChildren(text('Context saved to this conversation.')); updateComposer(); await syncRoom(); } catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text(publicError(error, 'Context could not be saved; retry when the local owner is available.'))); } finally { updateComposer(); } }
  function reviewTurn() { const participant = $('participant').value; if (!state.selected || !participant || !$('message').value.trim()) return; state.reviewing = true; updateComposer(); $('review-confirm').focus(); announce('Review agent request for ' + identityLabel(participant)); }
  async function launchTurn() { const participant = $('participant').value; const value = $('message').value; const activeKey = key(state.selected, participant); if (!state.selected || !participant || !value.trim() || state.active[activeKey]) return; state.reviewing = false; state.active[activeKey] = {participant, turn_id: null, startedAt: Date.now()}; renderWorking(); updateComposer(); $('note').className = 'composer-note'; $('note').replaceChildren(text('Request recorded; waiting for ' + identityLabel(participant) + '…')); try { const result = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/turns', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant, message: value, reviewed: true})}); $('message').value = ''; saveDraft(); if (result.status === 'forked') $('note').replaceChildren(text('A fresh conversation was created; the provider session was not resumed.')); else $('note').replaceChildren(text('Agent turn started. Other agents can continue in parallel.')); await syncRoom(); } catch (error) { delete state.active[activeKey]; renderWorking(); $('note').className = 'composer-note error'; $('note').replaceChildren(text(publicError(error, 'Agent turn could not start; the recorded timeline was left unchanged.'))); } finally { updateComposer(); $('message').focus(); } }
  function startTurn() { reviewTurn(); }
  async function cancelTurn(participant) { const activeKey = key(state.selected, participant); if (!state.selected || !participant || !state.active[activeKey]) return; const confirmed = await openActionDialog('Stop agent turn', 'Stop ' + identityLabel(participant) + '? Summon will record the request and stop the provider process.', {confirmLabel: 'Stop this turn'}); if (!confirmed) return; try { await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant})}); $('note').className = 'composer-note'; $('note').replaceChildren(text('Stop request recorded. The provider process is being stopped.')); } catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text(publicError(error, 'No active agent turn could be stopped.'))); } }
  async function recoverTurn() {
    const candidate = state.recovery; if (!candidate || !state.selected) return; const confirmed = await openActionDialog('Mark turn blocked', 'Close this unresolved turn as blocked? Summon will not retry the provider.', {confirmLabel: 'Mark blocked'}); if (!confirmed) return;
    try { await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/recover', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant: candidate.participant, confirm: true})}); $('note').className = 'composer-note'; $('note').replaceChildren(text('Turn marked blocked. No provider retry was performed.')); await syncRoom(); }
    catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text(publicError(error, 'Recovery could not be recorded; keep the room open for review.'))); }
  }
  async function forkTurn() {
    const candidate = state.recovery; if (!candidate || !state.selected) return; const defaultPrompt = $('message').value.trim() || 'Continue this work in a fresh conversation without resuming the provider session.'; const prompt = await openActionDialog('Start a fresh conversation', 'Context to carry into the new conversation. The provider session will not be resumed.', {input: true, defaultValue: defaultPrompt, confirmLabel: 'Create fresh conversation'}); if (!prompt || !prompt.trim()) return;
    try { const result = await getJSON('/api/v1/rooms/' + encodeURIComponent(state.selected) + '/fork', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({participant: candidate.participant, message: prompt.trim(), reason: candidate.drift ? 'model identity drift' : 'operator recovery fork'})}); $('note').className = 'composer-note'; $('note').replaceChildren(text('Fresh conversation created: ' + String(result.session_id || 'new room') + '. The provider session was not resumed.')); await refresh(); }
    catch (error) { $('note').className = 'composer-note error'; $('note').replaceChildren(text(publicError(error, 'Fork could not be created; the original room is unchanged.'))); }
  }
  function chooseMode(mode) { state.mode = mode; state.reviewing = false; updateComposer(); $('message').focus(); }
  $('context-tab').addEventListener('click', () => chooseMode('context')); $('agent-tab').addEventListener('click', () => chooseMode('agent')); $('turn-recover').addEventListener('click', recoverTurn); $('turn-fork').addEventListener('click', forkTurn);
  $('action-dialog-cancel').addEventListener('click', () => closeActionDialog(null)); $('action-dialog-confirm').addEventListener('click', () => { const field = $('action-dialog-input'); closeActionDialog(field.hidden ? true : field.value); }); $('action-dialog').addEventListener('click', event => { if (event.target === $('action-dialog')) closeActionDialog(null); });
  [$('context-tab'), $('agent-tab')].forEach((tab, index, tabs) => tab.addEventListener('keydown', event => { if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return; event.preventDefault(); const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length]; next.click(); next.focus(); }));
  $('agent-picker-button').addEventListener('click', toggleAgentPicker); document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('agent-picker-menu').hidden) { event.preventDefault(); closeAgentPicker(); $('agent-picker-button').focus(); } });
  $('send').addEventListener('click', () => state.mode === 'context' ? postContext() : startTurn()); $('review-back').addEventListener('click', () => { state.reviewing = false; updateComposer(); $('message').focus(); }); $('review-confirm').addEventListener('click', launchTurn); $('run-cancel').addEventListener('click', () => { const first = Object.values(state.active)[0]; if (first) cancelTurn(first.participant); }); $('participant').addEventListener('change', () => { state.reviewing = false; updateComposer(); }); $('message').addEventListener('input', () => { $('counter').replaceChildren(text(String($('message').value.length) + ' / 12000')); if (state.reviewing) state.reviewing = false; saveDraft(); updateComposer(); }); $('message').addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); state.mode === 'context' ? postContext() : startTurn(); } }); $('room-search').addEventListener('input', event => { state.query = event.target.value; renderRooms(); }); $('room-search').addEventListener('keydown', event => { if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return; const buttons = Array.from(document.querySelectorAll('.room-button')); if (!buttons.length) return; event.preventDefault(); const current = buttons.indexOf(document.activeElement); const next = buttons[(current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]; next.focus(); }); document.querySelectorAll('[data-event-filter]').forEach(button => button.addEventListener('click', () => { state.eventFilter = button.dataset.eventFilter || 'all'; updateEventFilters(); renderTimeline(); announce('Showing ' + state.eventFilter + ' events'); })); $('rail-open').addEventListener('click', () => setRailOpen(true)); $('rail-close').addEventListener('click', () => setRailOpen(false)); $('rail-backdrop').addEventListener('click', () => { if (state.railOpen) setRailOpen(false); if (state.evidenceOpen) setEvidenceOpen(false); }); $('evidence-open').addEventListener('click', () => setEvidenceOpen(!$('drawer').classList.contains('open'))); $('drawer-close').addEventListener('click', () => setEvidenceOpen(false)); $('latest').addEventListener('click', () => { const feed = $('timeline'); feed.scrollTo({top: feed.scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); state.feedPinned = true; $('latest').hidden = true; }); $('notice-retry').addEventListener('click', () => { setNotice(''); refresh(); if (state.selected) syncRoom().catch(() => {}); }); document.addEventListener('keydown', event => { const target = event.target; const typing = target && (target.matches('input, textarea, select, [contenteditable="true"]')); if (!typing && (event.key === '/' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k'))) { event.preventDefault(); $('room-search').focus(); $('room-search').select(); } if (state.evidenceOpen && window.matchMedia('(max-width: 760px)').matches && event.key === 'Tab') { const focusable = Array.from($('drawer').querySelectorAll('button, a, input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter(node => !node.disabled && node.offsetParent !== null); if (focusable.length) { const first = focusable[0]; const last = focusable[focusable.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } } } if (event.key === 'Escape') { if (!$('action-dialog').hidden) { event.preventDefault(); closeActionDialog(null); } else if (state.reviewing) { state.reviewing = false; updateComposer(); $('message').focus(); } else if (state.railOpen) setRailOpen(false); else if (state.evidenceOpen) setEvidenceOpen(false); } });
  $('participant').replaceChildren(); const choose = document.createElement('option'); choose.value = ''; choose.append(text('Choose an agent')); $('participant').append(choose);
  refresh(); state.refreshTimer = setInterval(refresh,10000); state.ageTimer = setInterval(updateAge,1000); window.addEventListener('beforeunload', () => { clearInterval(state.refreshTimer); clearInterval(state.ageTimer); stopStream(); });
})();'''


def page_bytes(*, nonce: str, timeout_ms: int = 600_000) -> bytes:
    """Build the page with a nonce as the only request-specific value."""
    if not isinstance(nonce, str) or not nonce or any(ch in nonce for ch in '<>"\''):
        raise ValueError("invalid page nonce")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 30 * 60 * 1000:
        raise ValueError("invalid page timeout")
    markup = _HTML.replace("__NONCE__", nonce).replace("__CSS__", _CSS).replace("__JS__", _JS.replace("__TIMEOUT_MS__", str(timeout_ms)))
    return markup.encode("utf-8")


__all__ = ["page_bytes"]
