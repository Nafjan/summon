"""Optional authenticated loopback observer for a deliberation run.

The surface is deliberately a thin, provider-inert view over the durable store.
It never launches a backend, trusts model prose, or mutates policy.  A browser
gets its bearer token in the URL fragment; the fragment is not sent in HTTP.
"""

from __future__ import annotations

import json
import argparse
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import _deliberation_store as _store
import _rundir


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_BODY_BYTES = 16 * 1024
MAX_CURSOR = 10_000_000
SSE_POLL_SECONDS = 0.5
SSE_MAX_SECONDS = 15.0
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
SURFACE_RECORD = ".summon-deliberation-ui.json"
MAX_SURFACE_RECORD_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_ACTIVE_CLIENTS = 32


class DeliberationUIError(Exception):
    """The local observer request was refused without provider side effects."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _safe_error(exc: BaseException) -> dict[str, str]:
    """Never reflect paths, prompts, or raw journal text through the UI."""
    return {"error_kind": "ui_request_failed",
            "error": f"deliberation request refused ({type(exc).__name__})"}


def _surface_record_path(root: str, run_id: str) -> str:
    return os.path.join(_store.run_dir(root, run_id), SURFACE_RECORD)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a no-op on every supported Windows
        # runtime.  Query-only OpenProcess avoids ever sending a termination
        # signal to the current process.
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() in (5,)  # access denied => live
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - stale metadata is fail-closed
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_surface_record(root: str, run_id: str) -> dict[str, object] | None:
    """Return a validated live-server record, or ``None`` for stale/missing."""
    if not isinstance(root, str) or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        return None
    try:
        path = _surface_record_path(root, run_id)
        path_obj = Path(path)
        if path_obj.is_symlink() or path_obj.stat().st_size > MAX_SURFACE_RECORD_BYTES:
            return None
        value = _rundir.read_json(path)
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("run_id") != run_id):
        return None
    pid = value.get("pid"); url = value.get("url"); token = value.get("token")
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(url, str) or not isinstance(token, str)
            or not TOKEN_RE.fullmatch(token)):
        return None
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    path = [part for part in parsed.path.split("/") if part]
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.query or not port or len(path) != 2 or path[0] != "runs"
            or path[1] != run_id or not parsed.fragment.startswith("token=")
            or secrets.compare_digest(parsed.fragment[6:], token) is False
            or not _pid_is_alive(pid)):
        return None
    return {"run_id": run_id, "pid": pid, "url": url, "token": token,
            "started_at": value.get("started_at")}


def _write_surface_record(root: str, run_id: str, *, pid: int, url: str, token: str) -> None:
    _rundir.atomic_write_json(_surface_record_path(root, run_id), {
        "schema_version": 1, "run_id": run_id, "pid": pid, "url": url,
        "token": token, "started_at": time.time(),
    })


def _remove_surface_record(root: str, run_id: str, *, pid: int, token: str) -> None:
    path = _surface_record_path(root, run_id)
    current = _rundir.read_json(path)
    if (isinstance(current, dict) and current.get("pid") == pid
            and isinstance(current.get("token"), str)
            and secrets.compare_digest(current["token"], token)):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _page(*, nonce: str) -> bytes:
    # Impeccable contract: the page is an archival flight recorder, not chat.
    markup = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summon deliberation ledger</title>
<style nonce="{nonce}">
::selection {{ background:#5365ff55; color:#fff }}
:root {{ color-scheme:dark; --ink:#0d0e10; --rail:#111214; --surface:#17191d; --surface-hi:#1d2026; --surface-low:#121417; --line:#2b2e35; --line-hi:#3a3e47; --text:#f5f5f3; --muted:#989ba5; --quiet:#9da1ab; --accent:#8794ff; --accent-strong:#a9b2ff; --accent-soft:#252a48; --warn:#e7c078; --danger:#ff8b86; --ok:#7ddbb6; }}
* {{ box-sizing:border-box }} html {{ background:var(--ink) }} body {{ margin:0; background:var(--ink); color:var(--text); font:14px/1.55 Inter,ui-sans-serif,system-ui,sans-serif; letter-spacing:-.005em; }}
.app-shell {{ min-height:100vh; display:grid; grid-template-columns:252px minmax(0,1fr); position:relative; }} .rail {{ border-right:1px solid #25272d; padding:30px 20px 22px; display:flex; flex-direction:column; gap:25px; background:var(--rail); }}
.brand {{ display:flex; align-items:center; gap:11px; font-weight:800; letter-spacing:.17em; font-size:.75rem; }} .brand-mark {{ display:grid; place-items:center; width:30px; height:30px; border:1px solid #6675f0; border-radius:9px; color:var(--accent-strong); font-weight:800; letter-spacing:0; box-shadow:0 0 0 4px #6574f00d; }} .rail-kicker {{ color:var(--quiet); font-size:.64rem; letter-spacing:.2em; text-transform:uppercase; }}
.rail-nav {{ display:grid; gap:3px; }} .nav-item {{ color:var(--muted); text-decoration:none; border-radius:8px; padding:10px 11px; display:flex; align-items:center; justify-content:space-between; gap:8px; }} .nav-item:hover, .nav-item:focus-visible, .nav-item.active {{ background:#24272e; color:var(--text); }} .nav-item:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }} .nav-count {{ color:var(--accent-strong); font:11px ui-monospace,SFMono-Regular,monospace; }}
.rail-footer {{ margin-top:auto; border-top:1px solid #25272d; padding-top:15px; color:var(--muted); font-size:.76rem; display:flex; align-items:center; gap:8px; }} .owner-dot {{ width:7px; height:7px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px #7ddbb615; }}
.workspace {{ min-width:0; max-width:1240px; width:100%; margin:0 auto; padding:38px clamp(22px,4.2vw,58px) 56px; }} .topbar {{ display:flex; align-items:center; justify-content:space-between; gap:22px; border-bottom:1px solid #25272d; padding-bottom:22px; }} .crumb {{ color:var(--quiet); font-size:.78rem; letter-spacing:.025em; overflow-wrap:anywhere; }}
h1 {{ margin:7px 0 0; max-width:100%; font-size:1.45rem; font-weight:650; letter-spacing:-.035em; overflow-wrap:anywhere; }} .status-chip {{ display:inline-flex; align-items:center; min-height:30px; border:1px solid var(--line-hi); border-radius:999px; padding:5px 11px; background:#1b1e24; color:var(--text); font-size:.76rem; font-weight:700; white-space:nowrap; }} .status-chip::before {{ content:""; width:6px; height:6px; border-radius:50%; background:var(--accent); margin-right:7px; }} .status-chip[data-state="WAITING_HUMAN"] {{ background:#26231c; border-color:#5b4d32; color:#f4d79b; }} .status-chip[data-state="WAITING_HUMAN"]::before {{ background:var(--warn); }} .status-chip[data-state="BLOCKED"] {{ background:#292023; border-color:#61363a; color:#ffb4af; }} .status-chip[data-state="BLOCKED"]::before {{ background:var(--danger); }} .status-chip[data-state="DECIDED"] {{ background:#172822; border-color:#356451; color:#a8eacc; }} .status-chip[data-state="DECIDED"]::before {{ background:var(--ok); }}
.global-alert {{ margin:14px 0 0; padding:11px 13px; border:1px solid #61363a; border-radius:10px; background:#292023; color:#ffb4af; font-size:.82rem; }} .global-alert[hidden] {{ display:none; }}
.decision-room {{ margin-top:25px; border:1px solid var(--line); border-radius:17px; padding:25px 27px 22px; background:#171a20; display:grid; grid-template-columns:minmax(0,1fr) minmax(350px,1.15fr); gap:32px; box-shadow:0 18px 50px #00000020; }} .room-copy {{ min-width:0; align-self:center; }} .section-index {{ color:var(--quiet); font:10px ui-monospace,SFMono-Regular,monospace; letter-spacing:.12em; text-transform:uppercase; }} .decision-room h2, .panel h2 {{ margin:0; font-size:1rem; font-weight:650; letter-spacing:-.02em; }} .room-title {{ margin:12px 0 0; font-size:clamp(1.6rem,2.8vw,2.25rem); font-weight:520; letter-spacing:-.05em; overflow-wrap:anywhere; }} .room-summary {{ color:var(--muted); margin:9px 0 0; max-width:48ch; }}
.metric-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; align-self:center; border:1px solid var(--line); border-radius:12px; overflow:hidden; background:var(--line); }} .metric {{ padding:14px 13px; background:#15171b; min-width:0; }} .metric-label {{ display:block; color:var(--quiet); font-size:.67rem; line-height:1.3; }} .metric-value {{ display:block; margin-top:8px; font-weight:650; font-size:.9rem; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }}
#seat-strip {{ grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:7px; margin-top:-15px; }} .seat-pill {{ display:inline-flex; align-items:center; gap:7px; padding:6px 9px; border:1px solid var(--line-hi); border-radius:999px; background:#15171b; color:var(--muted); font:11px ui-monospace,SFMono-Regular,monospace; }} .seat-pill::before {{ content:'·'; color:var(--quiet); font-size:18px; line-height:0; }} .seat-pill[data-state='ballot']::before {{ content:'✓'; color:var(--ok); }} .seat-pill[data-state='pending']::before {{ content:'·'; color:var(--warn); }} .model-tooltip {{ position:relative; display:inline-flex; align-items:center; min-height:24px; color:var(--accent-strong); text-decoration:underline dotted; text-underline-offset:3px; cursor:help; }} .model-tooltip:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; border-radius:4px; }} .model-tooltip::after {{ content:attr(data-tooltip); position:absolute; z-index:5; left:0; bottom:calc(100% + 9px); width:max-content; max-width:min(300px,78vw); padding:9px 11px; border:1px solid var(--line-hi); border-radius:8px; background:#0d0e10; color:var(--text); box-shadow:0 12px 28px #0009; font:12px/1.45 ui-sans-serif,system-ui,sans-serif; white-space:pre-line; opacity:0; pointer-events:none; transform:translateY(4px); transition:opacity .14s ease,transform .14s ease; }} .model-tooltip:hover::after, .model-tooltip:focus-visible::after {{ opacity:1; transform:translateY(0); }}
.content-grid {{ display:grid; grid-template-columns:minmax(0,1fr) 286px; gap:18px; margin-top:18px; }} .side-stack {{ display:grid; align-content:start; gap:18px; min-width:0; }} .panel {{ min-width:0; border:1px solid var(--line); border-radius:14px; background:var(--surface); padding:19px; }} .panel-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding-bottom:15px; border-bottom:1px solid var(--line); }} .panel-head h2 {{ margin-top:5px; }} .panel-count {{ color:var(--quiet); font:11px ui-monospace,SFMono-Regular,monospace; }}
#ledger {{ min-height:320px; margin-top:0; }} .round-divider {{ padding:14px 0 7px; color:var(--accent-strong); font:10px ui-monospace,SFMono-Regular,monospace; letter-spacing:.12em; text-transform:uppercase; }} .event {{ border-bottom:1px solid var(--line); padding:15px 0; }} .event:last-child {{ border-bottom:0; }} .event summary {{ cursor:pointer; list-style:none; }} .event summary::-webkit-details-marker {{ display:none; }} .event summary::after {{ content:'+'; float:right; color:#9da1ab; font:16px ui-monospace,SFMono-Regular,monospace; }} .event[open] summary::after {{ content:'−'; }} .event summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:5px; border-radius:4px; }} .event-row {{ display:grid; grid-template-columns:34px minmax(0,1fr) auto; align-items:center; gap:11px; }} .event-index {{ color:#9da1ab; font:11px ui-monospace,SFMono-Regular,monospace; }} .event-name {{ color:var(--text); font-size:.86rem; font-weight:600; overflow-wrap:anywhere; }} .event-meta {{ display:block; margin-top:4px; color:var(--muted); font-size:.76rem; overflow-wrap:anywhere; }} .event-mark {{ width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 4px #8794ff14; }} .event-facts {{ margin:11px 0 0 45px; color:#d9dde7; font-size:.8rem; overflow-wrap:anywhere; }} .event-technical {{ margin:10px 0 0 45px; border:1px solid var(--line); border-radius:8px; background:#101216; }} .event-technical summary {{ padding:8px 10px; color:var(--quiet); font:11px ui-monospace,SFMono-Regular,monospace; }} .event-technical summary::after {{ content:'+'; float:right; }} .event-technical[open] summary::after {{ content:'−'; }} .event-detail {{ white-space:pre-wrap; overflow-wrap:anywhere; margin:0; padding:10px; border-top:1px solid var(--line); color:#d9dde7; font:12px/1.55 ui-monospace,SFMono-Regular,monospace; }}
.evidence-list {{ display:grid; gap:0; margin-top:15px; }} .evidence-list p {{ margin:0; padding:10px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere; color:var(--muted); font-size:.8rem; }} .evidence-list p:last-child {{ border-bottom:0; }} .evidence-list strong {{ color:var(--text); font-weight:550; }} .human-gate {{ background:linear-gradient(145deg,#1d2027,#191b20); }} .human-gate[hidden] {{ display:none; }} .gate-state {{ color:var(--warn); font:10px ui-monospace,SFMono-Regular,monospace; letter-spacing:.12em; }} button {{ border:1px solid #a0aaff; border-radius:8px; background:var(--accent); color:#10121c; min-height:44px; padding:10px 16px; font:inherit; font-weight:750; cursor:pointer; }} button:focus-visible {{ outline:3px solid #cbd2ff; outline-offset:3px; }} button[disabled] {{ opacity:.45; cursor:not-allowed; }} .muted {{ color:var(--muted); }} .error {{ color:#ffb4af; }} .sr-only {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
@media (max-width:900px) {{ .app-shell {{ display:block; }} .rail {{ display:flex; flex-direction:row; align-items:center; gap:16px; padding:14px 18px; border-right:0; border-bottom:1px solid #25272d; }} .rail-kicker {{ display:none; }} .rail-nav {{ display:flex; overflow:auto; flex:1; }} .nav-item {{ white-space:nowrap; padding:8px 10px; }} .rail-footer {{ margin:0 0 0 auto; border:0; padding:0; white-space:nowrap; }} .workspace {{ padding-top:24px; }} }}
@media (max-width:760px) {{ .rail {{ flex-wrap:wrap; row-gap:10px; }} .rail-nav {{ order:3; flex-basis:100%; width:100%; }} .topbar {{ display:block; }} .status-chip {{ margin-top:12px; }} .decision-room {{ display:block; padding:19px; }} .metric-grid {{ margin-top:20px; grid-template-columns:repeat(2,minmax(0,1fr)); }} #seat-strip {{ margin-top:15px; }} .content-grid {{ grid-template-columns:1fr; }} .human-gate {{ position:sticky; bottom:10px; z-index:2; box-shadow:0 10px 30px #0008; }} }}
@media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important; transition:none!important; }} }}
</style></head><body>
<!-- THESIS: A deliberation is a governed decision ledger, not a chat stream. OWN-WORLD: archival flight-recorder instrument. STORY: durable truth, evidence, safe action. FIRST VIEWPORT: run bar, ledger, evidence rail. FORM: Operate ledger. FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->
<div class="app-shell"><aside class="rail" aria-label="Deliberation navigation"><div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span>SUMMON</span></div><div class="rail-kicker">Deliberation</div><nav class="rail-nav" aria-label="Run sections"><a class="nav-item active" href="#journal">Journal <span id="nav-count" class="nav-count">—</span></a><a class="nav-item" href="#evidence">Evidence</a><a class="nav-item" href="#human-boundary">Human gate</a></nav><div class="rail-footer"><span class="owner-dot" aria-hidden="true"></span><span id="connection">Owner connected</span></div></aside>
<main class="workspace"><header class="topbar"><div><div class="crumb">Local run / <span id="runline">run pending</span></div><h1>Deliberation control surface</h1></div><div id="state" class="status-chip" role="status" aria-live="polite" data-state="CONNECTING">CONNECTING</div></header><div id="global-alert" class="global-alert" role="alert" hidden></div>
<section class="decision-room" aria-labelledby="decision-room-title"><div class="room-copy"><div class="section-index">01 · decision state</div><h2 id="decision-room-title">Governed decision</h2><p id="decision-title" class="room-title">Deliberation in progress</p><p id="decision-subtitle" class="room-summary">The owner is assembling a durable, reviewable decision.</p></div><div class="metric-grid" aria-label="Decision metrics"><div class="metric"><span class="metric-label">Outcome / candidate</span><span id="metric-candidate" class="metric-value">—</span></div><div class="metric"><span class="metric-label">Quorum</span><span id="metric-quorum" class="metric-value">—</span></div><div class="metric"><span class="metric-label">Physical attempts</span><span id="metric-attempts" class="metric-value">—</span></div><div class="metric"><span class="metric-label">Rounds</span><span id="metric-rounds" class="metric-value">—</span></div><div class="metric"><span class="metric-label">Time remaining</span><span id="metric-remaining" class="metric-value">—</span></div><div class="metric"><span class="metric-label">Spend certainty</span><span id="metric-spend" class="metric-value">—</span></div></div><div id="seat-strip" aria-label="Seat status"></div></section>
<div class="content-grid"><section id="journal" class="panel" aria-labelledby="journal-title"><div class="panel-head"><div><div class="section-index">02 · durable events</div><h2 id="journal-title">Live journal</h2></div><span id="journal-count" class="panel-count">— events</span></div><div id="ledger"><p class="muted">Waiting for canonical replay…</p></div></section><div class="side-stack"><aside id="evidence" class="panel" aria-labelledby="evidence-title"><div class="panel-head"><div><div class="section-index">03 · evidence</div><h2 id="evidence-title">Run facts</h2></div><span class="panel-count">owner-bound</span></div><div id="evidence-list" class="evidence-list"><p class="muted">No run loaded.</p></div></aside><section id="human-boundary" class="panel human-gate" aria-labelledby="human-title" hidden><div class="panel-head"><div><div class="section-index">04 · human boundary</div><h2 id="human-title">Action shelf</h2></div><span id="gate-state" class="gate-state">ACTION NEEDED</span></div><p id="note" class="muted" role="status" aria-live="polite">Only cancel is enabled in this slice; approval actions remain coordinator-owned.</p><button id="cancel" disabled>Queue cancel</button></section></div></div><div id="update" class="sr-only" role="status" aria-live="polite"></div></main></div>
<script nonce="{nonce}">
(() => {{
  const token = location.hash.startsWith('#token=') ? decodeURIComponent(location.hash.slice(7)) : '';
  const run = location.pathname.split('/').filter(Boolean).pop() || '';
  const headers = token ? {{'Authorization':'Bearer '+token}} : {{}};
  let currentReceipt = null, currentOwner = null, currentDeadline = NaN, lastState = '';
  const state = document.querySelector('#state'), runline = document.querySelector('#runline'), ledger = document.querySelector('#ledger'), evidenceList = document.querySelector('#evidence-list'), cancel = document.querySelector('#cancel'), note = document.querySelector('#note'), update = document.querySelector('#update'), connection = document.querySelector('#connection'), humanGate = document.querySelector('#human-boundary'), globalAlert = document.querySelector('#global-alert'), seatStrip = document.querySelector('#seat-strip');
  function text(value) {{ return document.createTextNode(value == null ? '' : String(value)); }}
  function labelState(value) {{ return String(value || 'UNKNOWN').replaceAll('_', ' ').toLowerCase().replace(/(^|\\s)\\S/g, char => char.toUpperCase()); }}
  function eventSummary(record) {{
    const values = [record.event || 'event'];
    [['seat_id','seat'],['turn_ordinal','turn'],['attempt_id','attempt'],['option_id','option'],['to','state'],['reason','reason'],['transport','transport'],['parser_valid','parsed']].forEach(([key,label]) => {{ if (record[key] != null && ['string','number','boolean'].includes(typeof record[key])) values.push(label+': '+record[key]); }});
    return values.join(' · ');
  }}
  function eventTitle(record) {{
    const kind = String(record.event || 'event').replaceAll('_', ' ');
    return kind.replace(/(^|\\s)\\S/g, char => char.toUpperCase());
  }}
  function eventMeta(record) {{
    const values = [];
    [['seat_id','seat'],['turn_ordinal','turn'],['attempt_id','attempt'],['option_id','option'],['to','state'],['reason','reason'],['transport','transport'],['parser_valid','parsed']].forEach(([key,label]) => {{ if (record[key] != null && ['string','number','boolean'].includes(typeof record[key])) values.push(label+': '+record[key]); }});
    return values.join(' · ') || 'Durable journal fact';
  }}
  function eventRound(record, seats) {{
    const ordinal = Number(record.turn_ordinal);
    return Number.isInteger(ordinal) && ordinal >= 0 && seats > 0 ? Math.floor(ordinal / seats) + 1 : null;
  }}
  function eventFacts(record) {{
    const event = String(record.event || 'event');
    if (event === 'ballot_accepted') return 'Ballot accepted into the durable tally.';
    if (event === 'attempt_finished') return record.transport_ok === false ? 'Provider boundary reported a transport failure.' : (record.parser_valid === false ? 'Output was received but did not parse as a ballot.' : 'Attempt finished with bounded execution evidence.');
    if (event === 'state_transition') return record.to === 'DECIDED' ? 'Decision is durable and no further provider work is expected.' : (record.to === 'WAITING_HUMAN' ? 'A human boundary is now active.' : (record.to === 'CANCELLED' ? 'Cancellation is durable.' : 'Control state advanced.'));
    if (event === 'cleanup_receipt') return record.clean === true && record.verified === true ? 'Cleanup verified clean.' : 'Cleanup needs operator review.';
    return 'Durable journal fact; redacted technical fields are available below.';
  }}
  function modelIdentity(receipt, seat) {{
    const values = receipt && receipt.model_display_by_seat;
    const item = values && typeof values === 'object' ? values[seat] : null;
    if (!item || typeof item !== 'object') return null;
    const role = typeof item.role === 'string' ? item.role : '';
    const name = typeof item.name === 'string' ? item.name : '';
    const version = typeof item.version === 'string' ? item.version : '';
    if (!role || !name || !version) return null;
    return {{ role, name, version,
      label: typeof item.label === 'string' ? item.label : 'unclassified',
      lane: typeof item.lane === 'string' ? item.lane : 'unassigned',
      availability: typeof item.availability === 'string' ? item.availability : 'unverified',
      servedExact: item.served_exact === true }};
  }}
  function modelTooltip(identity) {{
    return ['Role: '+identity.role, 'Model: '+identity.name, 'Version: '+identity.version,
      'Lane: '+identity.lane, 'Label: '+identity.label, 'Availability: '+identity.availability,
      'Served exact: '+(identity.servedExact ? 'yes' : 'not verified')].join('\\n');
  }}
  function ownerAvailable(owner) {{
    if (owner === undefined) return true;
    if (!owner || owner.lease_expires == null) return false;
    const lease = Number(owner.lease_expires);
    return Number.isFinite(lease) && lease > Date.now() / 1000;
  }}
  function refreshCountdown() {{
    const metric = document.querySelector('#metric-remaining');
    if (!Number.isFinite(currentDeadline)) {{ metric.textContent = 'Bound'; return; }}
    const remaining = Math.max(0, currentDeadline - Date.now());
    metric.textContent = remaining < 60000 ? Math.ceil(remaining / 1000) + 's' : Math.ceil(remaining / 60000) + 'm';
  }}
  function alertText(data, blocked, ownerOk) {{
    if (blocked) return data.error_kind === 'torn_tail' ? 'The journal has an incomplete tail. Recovery is required before action.' : 'The canonical replay is blocked; no provider action was taken.';
    if (!ownerOk) return 'The owner lease is not active. This surface is observation-only until ownership returns.';
    return '';
  }}
  function render(data) {{
    if (Object.prototype.hasOwnProperty.call(data, 'receipt') && data.receipt) currentReceipt = data.receipt;
    if (Object.prototype.hasOwnProperty.call(data, 'owner')) currentOwner = data.owner;
    const receipt = data.receipt || currentReceipt || {{}}; const projection = data.projection || {{}}; const blocked = data.status === 'blocked' || Boolean(data.error_kind || data.error); const stateValue = String(projection.state || '').toUpperCase(); const ownerOk = ownerAvailable(data.owner === undefined ? currentOwner : data.owner);
    const terminal = ['DECIDED','CANCELLED','TIMED_OUT','FAILED','REJECTED','UNRESOLVED','ATTEMPT_BUDGET_EXHAUSTED'].includes(stateValue); const decision = projection.decision_option || projection.candidate_option;
    state.textContent = blocked ? 'BLOCKED' : labelState(projection.state || data.status); state.dataset.state = blocked ? 'BLOCKED' : stateValue; runline.textContent = (data.run_id || run) + (data.journal_records == null ? '' : ' · ' + data.journal_records + ' events'); connection.textContent = blocked ? 'Replay needs attention' : (ownerOk ? 'Owner connected' : 'Owner unavailable'); connection.dataset.state = blocked ? 'blocked' : (ownerOk ? 'connected' : 'unavailable');
    const alert = alertText(data, blocked, ownerOk); globalAlert.hidden = !alert; globalAlert.textContent = alert;
    document.querySelector('#decision-title').textContent = receipt.decision_id || 'Deliberation in progress'; document.querySelector('#decision-subtitle').textContent = blocked ? 'The durable replay is blocked; no provider action was taken.' : (!ownerOk ? 'The owner lease is not active; this view is read-only.' : stateValue === 'WAITING_HUMAN' ? 'Consensus is ready for a human decision.' : terminal ? (decision ? 'The governed decision is complete.' : 'The run closed without a decision.') : 'The owner is assembling a durable, reviewable decision.');
    const records = Array.isArray(data.records) ? data.records : []; const openKeys = new Set(Array.from(ledger.querySelectorAll('details[data-event-key][open]')).map(node => node.dataset.eventKey)); ledger.replaceChildren();
    if (records.length === 0) {{ const p=document.createElement('p'); p.className='muted'; p.append(text(blocked ? 'The canonical replay is blocked; retrying safely.' : 'No public events yet.')); ledger.append(p); }}
    else {{ let previousRound = null; const seats = Array.isArray(receipt.seat_ids) ? receipt.seat_ids.length : 0; records.forEach((record, i) => {{ const round = eventRound(record, seats); if (round !== null && round !== previousRound) {{ const divider=document.createElement('div'); divider.className='round-divider'; divider.append(text('Round '+round)); ledger.append(divider); previousRound=round; }} const item=document.createElement('details'); item.className='event'; item.dataset.eventKey = String(record.event || 'event') + ':' + i + ':' + String(record.attempt_id || record.command_id || ''); item.open = openKeys.has(item.dataset.eventKey) || i === records.length - 1; const summary=document.createElement('summary'); const row=document.createElement('div'); row.className='event-row'; const index=document.createElement('span'); index.className='event-index'; index.append(text(String(i+1).padStart(2,'0'))); const copy=document.createElement('span'); const title=document.createElement('span'); title.className='event-name'; title.append(text(eventTitle(record))); const meta=document.createElement('span'); meta.className='event-meta'; meta.append(text(eventMeta(record))); copy.append(title,meta); const mark=document.createElement('span'); mark.className='event-mark'; mark.setAttribute('aria-hidden','true'); row.append(index,copy,mark); summary.append(row); item.append(summary); const facts=document.createElement('div'); facts.className='event-facts'; facts.append(text(eventFacts(record))); item.append(facts); const technical=document.createElement('details'); technical.className='event-technical'; const technicalSummary=document.createElement('summary'); technicalSummary.append(text('Redacted technical fields')); technical.append(technicalSummary); const pre=document.createElement('pre'); pre.className='event-detail'; pre.append(text(JSON.stringify(record, null, 2))); technical.append(pre); item.append(technical); ledger.append(item); }}); }}
    const attempts = projection.physical_attempts || {{}}; const cleanup = projection.cleanup || null; const candidate = decision || '—'; currentDeadline = Number(receipt.deadline_unix_ms); document.querySelector('#metric-candidate').textContent = candidate; document.querySelector('#metric-quorum').textContent = receipt.quorum_rule || 'fixed policy'; document.querySelector('#metric-attempts').textContent = (attempts.started || 0) + ' / ' + (receipt.max_attempts || '—'); document.querySelector('#metric-rounds').textContent = receipt.rounds == null ? '—' : String(receipt.rounds); document.querySelector('#metric-spend').textContent = projection.uncertain_spend ? 'Uncertain' : 'Accounted'; refreshCountdown(); document.querySelector('#nav-count').textContent = data.journal_records == null ? String(records.length) : String(data.journal_records); document.querySelector('#journal-count').textContent = (data.journal_records == null ? records.length : data.journal_records) + ' events';
     seatStrip.replaceChildren(); const seatIds = Array.isArray(receipt.seat_ids) ? receipt.seat_ids : []; const ballots = Array.isArray(projection.ballots) ? projection.ballots : []; seatIds.forEach(seat => {{ const pill=document.createElement('span'); pill.className='seat-pill'; const hasBallot=ballots.some(ballot => ballot && ballot.seat_id === seat); pill.dataset.state=hasBallot ? 'ballot' : 'pending'; const identity=modelIdentity(receipt, seat); const label=identity ? (identity.name+' '+identity.version) : (hasBallot ? 'ballot recorded' : 'bound'); if (identity) {{ const tip=document.createElement('span'); tip.className='model-tooltip'; tip.tabIndex=0; tip.setAttribute('role','img'); tip.setAttribute('aria-label',modelTooltip(identity).replace(/\\n/g,', ')); tip.dataset.tooltip=modelTooltip(identity); tip.title=modelTooltip(identity); tip.append(text(label)); pill.append(text(seat+' · '),tip); }} else {{ pill.append(text(seat+' · '+label)); }} seatStrip.append(pill); }});
    evidenceList.replaceChildren(); const plans=receipt.plan_identity_by_seat||{{}}; const planLabels=Object.keys(plans).map(seat => {{ const plan=plans[seat]||{{}}; return seat+': '+(plan.cli||plan.transport||'bound'); }}).join(' · '); const lines=[['Run',data.run_id],['Decision',receipt.decision_id],['Seats',(receipt.seat_ids||[]).join(', ')],['Agents',planLabels||'Bound seat plans'],['Roster',receipt.roster_digest ? String(receipt.roster_digest).slice(0,12)+'…' : 'Bound to receipt'],['Cleanup',cleanup ? (cleanup.clean === true && cleanup.verified === true ? 'Verified clean' : 'Needs review') : 'Pending'],['Recovery',data.recovery_required ? 'Required' : 'Clear'],['Deadline',receipt.deadline_unix_ms ? new Date(receipt.deadline_unix_ms).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}}) : 'Bound to receipt']]; lines.forEach(([label,value]) => {{ const p=document.createElement('p'); const strong=document.createElement('strong'); strong.append(text(label+': ')); p.append(strong,text(value == null || value === '' ? '—' : value)); evidenceList.append(p); }});
    const waiting = stateValue === 'WAITING_HUMAN'; const actionable = waiting && !blocked && ownerOk; humanGate.hidden = !actionable; if (!actionable && (blocked || !ownerOk || terminal)) delete note.dataset.action; cancel.disabled = !token || !actionable || Boolean(note.dataset.action); note.className = blocked || !ownerOk ? 'error' : 'muted'; if (!note.dataset.action) note.textContent = blocked ? 'The replay is blocked; no action was sent.' : (!ownerOk ? 'Owner unavailable; no action was sent.' : (waiting ? 'Only cancel is enabled in this slice; approval actions remain coordinator-owned.' : '')); if (lastState !== stateValue || blocked) update.textContent = blocked ? 'The canonical replay is blocked.' : (waiting ? 'Human decision is required.' : terminal ? 'The governed decision is complete.' : 'Canonical replay updated.'); lastState = stateValue;
  }}
  async function snapshot() {{ const responses=await Promise.all([fetch('/api/v1/runs/'+encodeURIComponent(run)+'/snapshot',{{headers,cache:'no-store'}}),fetch('/api/v1/runs/'+encodeURIComponent(run)+'/replay',{{headers,cache:'no-store'}})]); const status=await responses[0].json(); const replay=await responses[1].json(); if(!responses[0].ok || !responses[1].ok) throw new Error('canonical replay unavailable'); render({{...status,...replay,receipt:status.receipt||currentReceipt,projection:replay.projection||status.projection}}); }}
  async function watch() {{
    const response=await fetch('/api/v1/runs/'+encodeURIComponent(run)+'/events',{{headers,cache:'no-store'}}); if(!response.ok || !response.body) throw new Error('event stream unavailable');
    const reader=response.body.getReader(); const decoder=new TextDecoder(); let buffer='';
    while (true) {{ const part=await reader.read(); if (part.done) break; buffer += decoder.decode(part.value,{{stream:true}}); const chunks=buffer.split('\\n\\n'); buffer=chunks.pop() || ''; for (const chunk of chunks) {{ const data=chunk.split('\\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()).join('\\n'); if (data) render(JSON.parse(data)); }} }}
    reader.releaseLock();
  }}
  async function connect() {{ try {{ await watch(); setTimeout(connect, 100); }} catch (error) {{ connection.textContent='Reconnecting safely'; state.textContent='BLOCKED'; state.dataset.state='BLOCKED'; note.className='error'; note.textContent='Live updates paused; retrying safely.'; setTimeout(connect, 3000); }} }}
  cancel.addEventListener('click', async () => {{ if (!window.confirm('Queue a durable cancel for this run?')) return; cancel.disabled=true; note.dataset.action='1'; note.textContent='Cancel request is being queued…'; try {{ const response=await fetch('/api/v1/runs/'+encodeURIComponent(run)+'/commands',{{method:'POST',headers:{{...headers,'Content-Type':'application/json'}},body:JSON.stringify({{action:'cancel',command_id:'ui-'+crypto.randomUUID()}})}}); const data=await response.json(); note.textContent=data.command_status === 'queued' ? 'Cancel queued durably; waiting for the owner.' : (data.note||data.error||'Cancel request recorded.'); await snapshot(); }} catch (error) {{ delete note.dataset.action; cancel.disabled=false; note.className='error'; note.textContent='Cancel could not be queued; retry when the owner is available.'; }} }});
  setInterval(refreshCountdown, 1000);
  snapshot().then(connect).catch(error => {{ render({{status:'blocked', error_kind:'replay_unavailable', run_id:run, projection:{{state:'BLOCKED'}}, records:[]}}); connection.textContent='Reconnecting safely'; globalAlert.hidden=false; globalAlert.textContent='The canonical replay could not be read; retrying safely.'; setTimeout(connect, 3000); }});
}})();
</script></body></html>'''
    return markup.encode("utf-8")


class _SurfaceHandler(BaseHTTPRequestHandler):
    server_version = "SummonDeliberationUI/1"

    def log_message(self, *_args):
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        semaphore = getattr(self.server, "client_semaphore", None)
        self._client_slot = bool(semaphore is None or semaphore.acquire(timeout=0.25))

    def handle(self):
        if not self._client_slot:
            self.close_connection = True
            return
        super().handle()

    def finish(self):
        try:
            super().finish()
        finally:
            if self._client_slot:
                semaphore = getattr(self.server, "client_semaphore", None)
                if semaphore is not None:
                    semaphore.release()

    @property
    def surface(self):
        return self.server.surface  # type: ignore[attr-defined]

    def _authorized(self, *, require_origin: bool = False) -> bool:
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{self.server.server_port}"
        if host != expected:
            return False
        origin = self.headers.get("Origin")
        if require_origin and origin != f"http://{expected}":
            return False
        auth = self.headers.get("Authorization", "")
        return secrets.compare_digest(auth, "Bearer " + self.surface.token)

    def _send_json(self, value: object, status: int = 200):
        data = _json_bytes(value)
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _run_id(self):
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) != 5 or parts[:4] != ["api", "v1", "runs", self.surface.run_id]:
            raise DeliberationUIError("unknown local API route")
        if not RUN_ID_RE.fullmatch(parts[3]):
            raise DeliberationUIError("invalid run id")
        return parts[3]

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/" or (parsed.path == f"/runs/{self.surface.run_id}/"):
            if self.headers.get("Host", "") != f"127.0.0.1:{self.server.server_port}":
                self._send_json(_safe_error(DeliberationUIError("host refused")), 400)
                return
            nonce = secrets.token_urlsafe(18); data = _page(nonce=nonce)
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        try:
            if not self._authorized(): raise DeliberationUIError("unauthorized")
            run_id = self._run_id()
            if parsed.path.endswith("/snapshot"):
                self._send_json(_store.inspect_run(self.surface.root, run_id)); return
            if parsed.path.endswith("/replay"):
                self._send_json(_store.replay_run(self.surface.root, run_id)); return
            if parsed.path.endswith("/events"):
                self._send_sse(run_id); return
            raise DeliberationUIError("unknown local API route")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_json(_safe_error(exc), 401 if str(exc) == "unauthorized" else 400)

    def _send_sse(self, run_id: str):
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Accel-Buffering", "no"); self.end_headers()
        deadline = time.monotonic() + SSE_MAX_SECONDS; previous = None
        while time.monotonic() < deadline and not self.surface.closed.is_set():
            try: payload = _store.replay_run(self.surface.root, run_id)
            except Exception as exc: payload = _safe_error(exc)
            encoded = _json_bytes(payload)
            if encoded != previous:
                try:
                    self.wfile.write(b"event: snapshot\ndata: " + encoded + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
                    return
                previous = encoded
            time.sleep(SSE_POLL_SECONDS)

    def do_POST(self):
        try:
            if not self._authorized(require_origin=True): raise DeliberationUIError("unauthorized")
            run_id = self._run_id()
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY_BYTES: raise DeliberationUIError("request body is too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or body.get("action") != "cancel":
                raise DeliberationUIError("only typed cancel is enabled in this surface")
            command_id = body.get("command_id")
            if command_id is not None and (not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", command_id)):
                raise DeliberationUIError("invalid command id")
            self._send_json(_store.queue_cancel(self.surface.root, run_id, command_id))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_json(_safe_error(exc), 401 if str(exc) == "unauthorized" else 400)


class DeliberationSurface:
    """Own one authenticated loopback UI server for one durable run."""

    def __init__(self, root: str, run_id: str, *, token: str | None = None):
        if not isinstance(root, str) or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise DeliberationUIError("invalid deliberation surface identity")
        self.root, self.run_id = root, run_id
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or not TOKEN_RE.fullmatch(self.token):
            raise DeliberationUIError("surface token is too short")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _SurfaceHandler)
        self._server.daemon_threads = True
        self._server.request_queue_size = MAX_ACTIVE_CLIENTS
        self._server.client_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
        self._server.surface = self  # type: ignore[attr-defined]
        self.closed = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.address[1]}/runs/{self.run_id}/#token={self.token}"

    def start(self) -> str:
        if self._thread is None:
            existing = read_surface_record(self.root, self.run_id)
            if existing is not None:
                raise DeliberationUIError("a deliberation surface is already running")
            self._thread = threading.Thread(target=self._server.serve_forever,
                                             name="summon-deliberation-ui", daemon=True)
            self._thread.start()
            # Direct API users may host a mock surface before a durable run
            # directory exists.  The registry is required only for the CLI
            # handoff and is therefore best-effort in that provider-inert case.
            try:
                if os.path.isdir(_store.run_dir(self.root, self.run_id)):
                    _write_surface_record(self.root, self.run_id, pid=os.getpid(),
                                           url=self.url, token=self.token)
            except (OSError, ValueError):
                pass
        return self.url

    def close(self) -> None:
        self.closed.set()
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None: self._thread.join(timeout=2)
        try:
            _remove_surface_record(self.root, self.run_id, pid=os.getpid(), token=self.token)
        except OSError:
            pass


def serve_foreground(root: str, run_id: str) -> int:
    """Serve one surface until interrupted; used by the CLI-launched child."""
    surface = DeliberationSurface(root, run_id)
    url = surface.start()
    print(json.dumps({"mode": "deliberation-ui", "status": "ready", "run_id": run_id,
                      "url": url}, ensure_ascii=True), flush=True)
    try:
        while not surface.closed.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        surface.close()
    return 0


__all__ = ["DeliberationSurface", "DeliberationUIError", "read_surface_record",
           "serve_foreground"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="serve a local Summon deliberation surface")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("root")
    parser.add_argument("run_id")
    parsed = parser.parse_args()
    if not parsed.serve:
        parser.error("--serve is required")
    try:
        raise SystemExit(serve_foreground(parsed.root, parsed.run_id))
    except (DeliberationUIError, _store.DeliberationStoreError) as exc:
        raise SystemExit(str(exc))
