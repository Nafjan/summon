"""Optional authenticated loopback observer for a deliberation run.

The surface is deliberately a thin, provider-inert view over the durable store.
It never launches a backend, trusts model prose, or mutates policy.  A browser
gets its bearer token in the URL fragment; the fragment is not sent in HTTP.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import _deliberation_store as _store


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_BODY_BYTES = 16 * 1024
MAX_CURSOR = 10_000_000
SSE_POLL_SECONDS = 0.5
SSE_MAX_SECONDS = 15.0
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class DeliberationUIError(Exception):
    """The local observer request was refused without provider side effects."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _safe_error(exc: BaseException) -> dict[str, str]:
    """Never reflect paths, prompts, or raw journal text through the UI."""
    return {"error_kind": "ui_request_failed",
            "error": f"deliberation request refused ({type(exc).__name__})"}


def _page(*, nonce: str) -> bytes:
    # Impeccable contract: the page is an archival flight recorder, not chat.
    markup = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summon deliberation ledger</title>
<style nonce="{nonce}">
:root {{ color-scheme: dark; --petrol:#102b31; --paper:#e9e2d4; --graphite:#263438; --vermilion:#d85b43; --muted:#aebdb9; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--petrol); color:var(--paper); font:15px/1.5 system-ui,sans-serif; }}
main {{ max-width:1180px; margin:auto; padding:24px; }} header {{ display:flex; justify-content:space-between; gap:20px; border-bottom:1px solid #547076; padding-bottom:18px; }}
h1 {{ margin:0; font-size:clamp(1.4rem,3vw,2.2rem); letter-spacing:-.03em; }} .eyebrow {{ color:var(--muted); text-transform:uppercase; letter-spacing:.12em; font-size:.72rem; }}
.grid {{ display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:20px; margin-top:20px; }} section, aside {{ background:var(--paper); color:var(--graphite); border-radius:14px; padding:18px; box-shadow:0 12px 35px #07181b66; }}
section h2, aside h2 {{ margin-top:0; font-size:1rem; }} #ledger {{ min-height:320px; }} .event {{ border-top:1px solid #b7b0a4; padding:13px 0; }} .event:first-child {{ border-top:0; }}
.stamp {{ color:#6a7774; font:12px ui-monospace,SFMono-Regular,monospace; }} .badge {{ display:inline-block; border-radius:999px; padding:2px 9px; background:var(--vermilion); color:white; font-weight:700; }}
button {{ border:0; border-radius:8px; background:var(--vermilion); color:white; padding:10px 14px; font:inherit; cursor:pointer; }} button:focus-visible {{ outline:3px solid #f2bf69; outline-offset:2px; }}
button[disabled] {{ opacity:.5; cursor:not-allowed; }} .muted {{ color:#5e6c6a; }} .error {{ color:#9b2f23; }} pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
@media (max-width:760px) {{ main {{ padding:14px; }} .grid {{ grid-template-columns:1fr; }} header {{ display:block; }} }}
@media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important; transition:none!important; }} }}
</style></head><body>
<!-- THESIS: A deliberation is a governed decision ledger, not a chat stream. OWN-WORLD: archival flight-recorder instrument. STORY: durable truth, evidence, safe action. FIRST VIEWPORT: run bar, ledger, evidence rail. FORM: Operate ledger. FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->
<main><header><div><div class="eyebrow">Summon / local ledger</div><h1>Deliberation control surface</h1></div><div id="state" class="badge" aria-live="polite">CONNECTING</div></header>
<div class="grid"><section aria-labelledby="ledger-title"><h2 id="ledger-title">Durable journal</h2><div id="ledger" aria-live="polite"><p class="muted">Waiting for a canonical snapshot…</p></div></section>
<aside aria-labelledby="evidence-title"><h2 id="evidence-title">Evidence rail</h2><div id="evidence"><p class="muted">No run loaded.</p></div><hr><h2>Human boundary</h2><button id="cancel" disabled>Queue cancel</button><p id="note" class="muted">Only cancel is enabled until the durable command coordinator is wired.</p></aside></div></main>
<script nonce="{nonce}">
(() => {{
  const token = location.hash.startsWith('#token=') ? decodeURIComponent(location.hash.slice(7)) : '';
  const run = location.pathname.split('/').filter(Boolean).pop() || '';
  const headers = token ? {{'Authorization':'Bearer '+token}} : {{}};
  const state = document.querySelector('#state'), ledger = document.querySelector('#ledger'), evidence = document.querySelector('#evidence'), cancel = document.querySelector('#cancel'), note = document.querySelector('#note');
  function text(value) {{ return document.createTextNode(value == null ? '' : String(value)); }}
  function render(data) {{
    const projection = data.projection || {{}}; state.textContent = projection.state || data.status || 'UNKNOWN';
    ledger.replaceChildren(); const records = data.records || data.journal_records || [];
    if (!Array.isArray(records) || records.length === 0) {{ const p=document.createElement('p'); p.className='muted'; p.append(text('No public events yet.')); ledger.append(p); }}
    else records.forEach((record, i) => {{ const item=document.createElement('article'); item.className='event'; const stamp=document.createElement('div'); stamp.className='stamp'; stamp.append(text('#'+(i+1)+'  '+(record.event || 'event'))); item.append(stamp); const pre=document.createElement('pre'); pre.append(text(JSON.stringify(record, null, 2))); item.append(pre); ledger.append(item); }});
    evidence.replaceChildren(); const lines=[['Run',data.run_id],['Decision',data.receipt && data.receipt.decision_id],['Seats',data.receipt && (data.receipt.seat_ids||[]).join(', ')],['Journal records',data.journal_records]];
    lines.forEach(([label,value]) => {{ const p=document.createElement('p'); const strong=document.createElement('strong'); strong.append(text(label+': ')); p.append(strong,text(value == null ? '—' : value)); evidence.append(p); }});
    cancel.disabled = !token || ['DECIDED','CANCELLED','TIMED_OUT','FAILED','REJECTED','UNRESOLVED','ATTEMPT_BUDGET_EXHAUSTED'].includes(String(projection.state||''));
  }}
  async function snapshot() {{ const response=await fetch('/api/v1/runs/'+encodeURIComponent(run)+'/snapshot',{{headers}}); const data=await response.json(); if(!response.ok) throw new Error(data.error||'snapshot failed'); render(data); }}
  cancel.addEventListener('click', async () => {{ cancel.disabled=true; const response=await fetch('/api/v1/runs/'+encodeURIComponent(run)+'/commands',{{method:'POST',headers:{{...headers,'Content-Type':'application/json'}},body:JSON.stringify({{action:'cancel',command_id:'ui-'+crypto.randomUUID()}})}}); const data=await response.json(); note.textContent=data.note||data.error||'Cancel queued.'; await snapshot(); }});
  snapshot().catch(error => {{ state.textContent='BLOCKED'; note.className='error'; note.textContent='The local ledger could not be read.'; }});
  setInterval(() => snapshot().catch(() => {{}}), 2000);
}})();
</script></body></html>'''
    return markup.encode("utf-8")


class _SurfaceHandler(BaseHTTPRequestHandler):
    server_version = "SummonDeliberationUI/1"

    def log_message(self, *_args):
        return

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
                self.wfile.write(b"event: snapshot\ndata: " + encoded + b"\n\n"); self.wfile.flush(); previous = encoded
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
        except Exception as exc:
            self._send_json(_safe_error(exc), 401 if str(exc) == "unauthorized" else 400)


class DeliberationSurface:
    """Own a loopback UI server; no browser is opened automatically."""

    def __init__(self, root: str, run_id: str, *, token: str | None = None):
        if not isinstance(root, str) or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise DeliberationUIError("invalid deliberation surface identity")
        self.root, self.run_id = root, run_id
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or not TOKEN_RE.fullmatch(self.token):
            raise DeliberationUIError("surface token is too short")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _SurfaceHandler)
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
            self._thread = threading.Thread(target=self._server.serve_forever,
                                             name="summon-deliberation-ui", daemon=True)
            self._thread.start()
        return self.url

    def close(self) -> None:
        self.closed.set()
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None: self._thread.join(timeout=2)


__all__ = ["DeliberationSurface", "DeliberationUIError"]
