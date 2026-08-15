"""Authenticated, provider-inert browser surface for conversation rooms.

The surface is intentionally separate from the deliberation control surface:
it can browse rooms, show bounded context, and append a human message, but it
cannot create a ballot, change policy, or launch a provider.  The URL bearer is
kept in the fragment and every API request still requires the header token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from _conversation import (ConversationError, ConversationJournal, _public_payload,
                           _root_path, _safe_id, group_rooms, list_rooms)


TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_BODY_BYTES = 16 * 1024
SSE_POLL_SECONDS = 0.5
SSE_MAX_SECONDS = 15.0
SURFACE_RECORD = ".summon-conversation-ui.json"
MAX_SURFACE_RECORD_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_ACTIVE_CLIENTS = 32


class ConversationUIError(ValueError):
    """A browser request was unsafe or the room could not be read."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"status": "blocked", "error_kind": "conversation_ui_request_failed",
            "error": f"conversation request refused ({type(exc).__name__})"}


def _surface_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / SURFACE_RECORD


def _surface_root_digest(root: str | os.PathLike[str]) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()


def _pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a query-only operation on every Windows
        # runtime.  OpenProcess with query-only access avoids signalling the
        # current process while still recognizing an access-denied live PID.
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - stale metadata is fail-closed
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def read_surface_record(root: str | os.PathLike[str]) -> dict[str, object] | None:
    try:
        canonical = _root_path(root, create=False)
        path = _surface_path(canonical)
        if path.is_symlink() or path.stat().st_size > MAX_SURFACE_RECORD_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    pid, url, token = value.get("pid"), value.get("url"), value.get("token")
    if (value.get("schema_version") != 1
            or value.get("root_sha256") != _surface_root_digest(canonical)
            or not _pid_is_alive(pid) or not isinstance(url, str)
            or len(url) > 512 or not url.startswith("http://127.0.0.1:")
            or not isinstance(token, str) or not TOKEN_RE.fullmatch(token)):
        return None
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not port or not (1 <= port <= 65535) or parsed.path != "/"
            or parsed.query or parsed.fragment != "token=" + token):
        return None
    return {"pid": pid, "url": url, "token": token}


def _write_surface_record(root: str, *, pid: int, url: str, token: str) -> None:
    target = _surface_path(_root_path(root, create=True))
    if target.exists() and target.is_symlink():
        raise ConversationUIError("surface record may not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".summon-conversation-ui-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "root_sha256": _surface_root_digest(target.parent),
                       "pid": pid, "url": url,
                       "token": token, "started_at": time.time()}, fh,
                      ensure_ascii=True, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _page(*, nonce: str) -> bytes:
    markup = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summon conversation atlas</title>
<style nonce="{nonce}">
:root {{ color-scheme:dark; --ink:#0d0e10; --rail:#111214; --panel:#17191d; --panel-hi:#1d2027; --line:#2b2e35; --text:#f5f5f3; --muted:#9da1ab; --quiet:#7f8794; --accent:#9aa6ff; --human:#f4d79b; --ok:#7ddbb6; }}
* {{ box-sizing:border-box }} html,body {{ margin:0; min-height:100%; background:var(--ink); color:var(--text); font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif }}
body {{ letter-spacing:-.005em }} .shell {{ min-height:100vh; display:grid; grid-template-columns:290px minmax(0,1fr) }}
aside {{ border-right:1px solid #25272d; background:var(--rail); padding:25px 18px; overflow:auto }} .brand {{ display:flex; gap:10px; align-items:center; font-weight:800; letter-spacing:.18em; font-size:.82rem }} .mark {{ display:grid; place-items:center; width:30px; height:30px; border:1px solid #7582ef; border-radius:9px; color:var(--accent); font-weight:800; letter-spacing:0 }}
.eyebrow {{ margin:29px 0 8px; color:var(--quiet); font:12px ui-monospace,monospace; letter-spacing:.14em; text-transform:uppercase }} .group {{ margin:0 0 18px }} .group-title {{ color:var(--muted); font-size:.82rem; overflow-wrap:anywhere }} .initiator {{ margin-top:9px; color:var(--quiet); font:11px ui-monospace,monospace; letter-spacing:.04em }}
.room-link {{ display:block; width:100%; margin:3px 0 0; padding:9px 10px; border:1px solid transparent; border-radius:9px; background:transparent; color:var(--muted); text-align:left; cursor:pointer; font:inherit }} .room-link:hover,.room-link:focus-visible,.room-link.active {{ border-color:var(--line); background:#202329; color:var(--text); outline:none }} .room-link strong {{ display:block; font-size:.82rem; font-weight:650; overflow-wrap:anywhere }} .room-link span {{ display:block; margin-top:2px; color:var(--quiet); font:11px ui-monospace,monospace }}
.main {{ min-width:0; max-width:1250px; width:100%; margin:0 auto; padding:35px clamp(21px,4.3vw,62px) 60px }} header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:22px; padding-bottom:22px; border-bottom:1px solid #25272d }} .crumb {{ color:var(--quiet); font:12px ui-monospace,monospace; letter-spacing:.05em }} h1 {{ margin:9px 0 0; font-size:clamp(1.7rem,3.1vw,2.55rem); line-height:1.05; font-weight:560; letter-spacing:-.03em; overflow-wrap:anywhere }} .badge {{ display:inline-flex; align-items:center; min-height:30px; padding:5px 11px; border:1px solid #3c455e; border-radius:99px; color:var(--accent); background:#1a1e2c; font:11px ui-monospace,monospace; white-space:nowrap }} .badge::before {{ content:'•'; margin-right:7px; color:var(--ok) }}
.summary {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(250px,.7fr); gap:18px; margin-top:22px }} .card {{ min-width:0; border:1px solid var(--line); border-radius:15px; background:var(--panel); padding:19px }} .card h2 {{ margin:0; font-size:1rem; font-weight:650 }} .meta {{ margin-top:8px; color:var(--muted); overflow-wrap:anywhere }} .mode {{ display:inline-flex; margin-top:13px; padding:4px 8px; border:1px solid #46506f; border-radius:99px; color:var(--accent); font:11px ui-monospace,monospace; text-transform:uppercase; letter-spacing:.1em }}
.events {{ margin-top:18px }} .event {{ padding:15px 0; border-bottom:1px solid var(--line) }} .event:last-child {{ border-bottom:0 }} .event-head {{ display:flex; gap:12px; align-items:baseline }} .event-title {{ font-weight:650; overflow-wrap:anywhere }} .event-index {{ color:var(--quiet); font:12px ui-monospace,monospace }} .event-meta {{ margin-top:4px; color:var(--muted); font-size:.82rem; overflow-wrap:anywhere }} .event-body {{ margin:10px 0 0; padding:11px 12px; border:1px solid #394158; border-radius:8px; background:var(--panel-hi); color:#dce0eb; white-space:pre-wrap; overflow-wrap:anywhere }} .event-body.human {{ border-color:#6a5733; color:#f8e7bf }} .event-tech {{ margin-top:9px; color:var(--quiet); font:11px ui-monospace,monospace }}
.composer {{ position:sticky; bottom:12px; margin-top:18px; box-shadow:0 5px 14px #0008 }} textarea {{ width:100%; min-height:92px; resize:vertical; border:1px solid #434a5f; border-radius:9px; background:#101216; color:var(--text); padding:11px; font:inherit }} textarea:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px }} .compose-row {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px }} .context-note {{ color:var(--quiet); font-size:.9rem }} button {{ min-height:44px; border:1px solid #aab4ff; border-radius:8px; background:var(--accent); color:#10121c; padding:10px 15px; font:inherit; font-weight:750; cursor:pointer }} button:disabled {{ opacity:1; cursor:not-allowed; background:#343a52; border-color:#6976c9; color:#dbe0ff }} button:focus-visible {{ outline:3px solid #cbd2ff; outline-offset:3px }} .empty {{ color:var(--muted); padding:28px 0 }} .error {{ color:#ffb4af; }}
@media (max-width:800px) {{ .shell {{ display:block }} aside {{ border-right:0; border-bottom:1px solid #25272d; max-height:310px }} .summary {{ grid-template-columns:1fr }} header {{ display:block }} .badge {{ margin-top:14px }} .compose-row {{ align-items:flex-start; flex-direction:column }} button {{ width:100% }} }}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none!important }} }}
</style></head><body><div class="shell"><aside aria-label="Conversation rooms"><div class="brand"><span class="mark" aria-hidden="true">S</span><span>SUMMON</span></div><div class="eyebrow">Conversation atlas</div><div id="rooms"><p class="empty">Loading rooms…</p></div></aside><main class="main"><header><div><div class="crumb">LOCAL / REDACTED CONTEXT</div><h1 id="title">Choose a conversation</h1></div><div id="connection" class="badge" role="status" aria-live="polite">Connecting</div></header><section class="summary"><div class="card"><h2 id="room-label">No room selected</h2><p id="room-meta" class="meta">Rooms are grouped by project and the application that started them.</p><span id="mode" class="mode">—</span></div><div class="card"><h2>Authority boundary</h2><p class="meta">Human messages are context only. This surface cannot approve, vote, launch, or change a deliberate run.</p><span class="mode">provider-inert</span></div></section><section class="card events" aria-labelledby="events-title"><h2 id="events-title">Conversation timeline</h2><div id="timeline"><p class="empty">Select a room to inspect its bounded event stream.</p></div></section><section class="card composer" aria-labelledby="compose-title"><h2 id="compose-title">Add context</h2><textarea id="message" maxlength="12000" placeholder="Ask a question or add context for the room…"></textarea><div class="compose-row"><span class="context-note">Saved as a human context event; never a control command.</span><button id="send" disabled>Post context</button></div><p id="note" class="error" role="status" aria-live="polite"></p></section></main></div><script nonce="{nonce}">
(() => {{
  const token = location.hash.startsWith('#token=') ? decodeURIComponent(location.hash.slice(7)) : '';
  const headers = token ? {{Authorization:'Bearer '+token}} : {{}};
  let selected = '', rooms = {{}}, timer = null;
  const $ = id => document.getElementById(id), list = $('rooms'), timeline = $('timeline'), send = $('send'), message = $('message'), note = $('note');
  const text = value => document.createTextNode(value == null ? '' : String(value));
  function escMode(value) {{ return String(value || 'chat').toUpperCase(); }}
  function renderRooms(grouped) {{
    rooms = grouped || {{}}; list.replaceChildren(); let first = null;
    Object.entries(rooms).forEach(([project, initiators]) => {{ const group=document.createElement('section'); group.className='group'; const title=document.createElement('div'); title.className='group-title'; title.append(text(project)); group.append(title); Object.entries(initiators).forEach(([initiator, entries]) => {{ const who=document.createElement('div'); who.className='initiator'; who.append(text(initiator)); group.append(who); (entries || []).forEach(room => {{ if (!first) first=room.session_id; const button=document.createElement('button'); button.className='room-link'+(room.session_id===selected?' active':''); button.dataset.session=room.session_id; const strong=document.createElement('strong'); strong.append(text(room.session_id)); const small=document.createElement('span'); small.append(text(escMode(room.mode)+' · '+String(room.cursor||0)+' events')); button.append(strong,small); button.addEventListener('click', () => select(room.session_id)); group.append(button); }}); }}); list.append(group); }}); if (!selected && first) select(first); if (!first) {{ $('title').textContent='No conversations yet'; $('connection').textContent='Owner connected'; }}
  }}
  function renderRoom(data) {{ const room=data.room||{{}}, events=Array.isArray(data.events)?data.events:[]; $('title').textContent=room.session_id || 'Conversation'; $('room-label').textContent=(room.project_id||'Project')+' / '+(room.session_id||'room'); $('room-meta').textContent=(room.initiator_host||'host')+' · '+(room.initiator_agent||'agent')+' · '+String(room.cursor||events.length)+' events'; $('mode').textContent=escMode(room.mode); timeline.replaceChildren(); if (!events.length) {{ const p=document.createElement('p'); p.className='empty'; p.append(text('No events yet.')); timeline.append(p); }} events.forEach((record,index) => {{ const item=document.createElement('article'); item.className='event'; const head=document.createElement('div'); head.className='event-head'; const n=document.createElement('span'); n.className='event-index'; n.append(text(String(index+1).padStart(2,'0'))); const title=document.createElement('span'); title.className='event-title'; title.append(text(String(record.event||'event').replaceAll('_',' '))); head.append(n,title); item.append(head); const meta=document.createElement('div'); meta.className='event-meta'; meta.append(text((record.actor_id||'system')+' · generation '+String(record.generation||1))); item.append(meta); const payload=record.payload||{{}}; if (payload.summary || payload.text || payload.text_sha256) {{ const body=document.createElement('div'); body.className='event-body'+(record.event==='human_message'?' human':''); body.append(text(payload.text || payload.summary || ('Human context · '+String(payload.text_chars||0)+' chars · '+String(payload.text_sha256||'')))); item.append(body); }} const tech=document.createElement('div'); tech.className='event-tech'; tech.append(text('public redacted · payload '+String(record.payload_sha256||'').slice(0,12)+'…')); item.append(tech); timeline.append(item); }}); send.disabled=!selected; }}
  async function getJSON(url, options={{}}) {{ const requestHeaders=Object.assign({{}},headers,options.headers||{{}}); const response=await fetch(url,Object.assign({{}},options,{{headers:requestHeaders,cache:'no-store'}})); const data=await response.json(); if(!response.ok) throw new Error(data.error||'request refused'); return data; }}
  async function loadRooms() {{ const data=await getJSON('/api/v1/rooms'); renderRooms(data.rooms); $('connection').textContent='Owner connected'; }}
  async function select(session) {{ selected=session; document.querySelectorAll('.room-link').forEach(node => node.classList.toggle('active',node.dataset.session===session)); try {{ renderRoom(await getJSON('/api/v1/rooms/'+encodeURIComponent(session))); note.textContent=''; }} catch(error) {{ note.textContent='Room could not be read; retrying safely.'; }} }}
  send.addEventListener('click', async () => {{ const value=message.value; if(!selected || !value.trim()) return; send.disabled=true; try {{ await getJSON('/api/v1/rooms/'+encodeURIComponent(selected)+'/messages',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:value}})}}); message.value=''; await select(selected); }} catch(error) {{ note.textContent='Context could not be saved; retry when the owner is available.'; }} finally {{ send.disabled=!selected; }} }});
  async function refresh() {{ try {{ await loadRooms(); if(selected) await select(selected); }} catch(error) {{ $('connection').textContent='Reconnect needed'; }} }}
  refresh(); timer=setInterval(refresh,2000); window.addEventListener('beforeunload',() => clearInterval(timer));
}})();</script></body></html>'''
    return markup.encode("utf-8")


class _ConversationHandler(BaseHTTPRequestHandler):
    server_version = "SummonConversationUI/1"

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
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host", "") != expected:
            return False
        if require_origin and self.headers.get("Origin") != f"http://{expected}":
            return False
        return secrets.compare_digest(self.headers.get("Authorization", ""),
                                     "Bearer " + self.surface.token)

    def _send_json(self, value: object, status: int = 200):
        data = _json_bytes(value)
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _session(self) -> str:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) not in (4, 5) or parts[:3] != ["api", "v1", "rooms"]:
            raise ConversationUIError("unknown conversation route")
        session = _safe_id(parts[3], "session id")
        if len(parts) == 5 and parts[4] not in {"messages", "events"}:
            raise ConversationUIError("unknown conversation route")
        return session

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            if self.headers.get("Host", "") != f"127.0.0.1:{self.server.server_port}":
                self._send_json(_safe_error(ConversationUIError("host refused")), 400); return
            nonce = secrets.token_urlsafe(18); data = _page(nonce=nonce)
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        try:
            if not self._authorized():
                raise ConversationUIError("unauthorized")
            if parsed.path == "/api/v1/rooms":
                self._send_json({"status": "ok", "rooms": list_rooms(self.surface.root),
                                 "redaction": "public-redacted"}); return
            session = self._session()
            journal = ConversationJournal.open(self.surface.root, session)
            if parsed.path.endswith("/events"):
                query = parse_qs(parsed.query); after = query.get("after", ["0"])[0]
                try: cursor = int(after)
                except ValueError as exc: raise ConversationUIError("invalid cursor") from exc
                self._send_json({"status": "ok", "room": journal.room.as_dict(),
                                 "events": journal.events(after_cursor=cursor),
                                 "cursor": journal.room.cursor,
                                 "redaction": "public-redacted"}); return
            self._send_json({"status": "ok", **journal.as_dict(), "redaction": "public-redacted"})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_json(_safe_error(exc), 401 if str(exc) == "unauthorized" else 400)

    def do_POST(self):
        try:
            if not self._authorized(require_origin=True):
                raise ConversationUIError("unauthorized")
            session = self._session()
            if not self.path.split("?", 1)[0].endswith("/messages"):
                raise ConversationUIError("only human messages may be posted")
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise ConversationUIError("request body is too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or not isinstance(body.get("message"), str):
                raise ConversationUIError("message must be text")
            event_id = body.get("message_id")
            if event_id is not None:
                _safe_id(event_id, "message id")
            journal = ConversationJournal.open(self.surface.root, session)
            event = journal.append_human_message(body["message"], message_id=event_id)
            self._send_json({"status": "posted", "event": event,
                             "cursor": journal.room.cursor,
                             "redaction": "public-redacted"})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            self._send_json(_safe_error(exc), 401 if str(exc) == "unauthorized" else 400)


class ConversationSurface:
    """One authenticated loopback room atlas for a private conversation root."""

    def __init__(self, root: str, *, token: str | None = None):
        if not isinstance(root, str) or not root:
            raise ConversationUIError("invalid conversation root")
        try:
            self.root = str(_root_path(root, create=True))
        except ConversationError as exc:
            raise ConversationUIError("invalid conversation root") from exc
        self.token = token or secrets.token_urlsafe(32)
        if not TOKEN_RE.fullmatch(self.token):
            raise ConversationUIError("surface token is invalid")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ConversationHandler)
        self._server.daemon_threads = True
        self._server.request_queue_size = MAX_ACTIVE_CLIENTS
        self._server.client_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
        self._server.surface = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.address[1]}/#token={self.token}"

    def start(self) -> str:
        if self._thread is None:
            existing = read_surface_record(self.root)
            if existing is not None:
                raise ConversationUIError("a conversation surface is already running")
            self._thread = threading.Thread(target=self._server.serve_forever,
                                             name="summon-conversation-ui", daemon=True)
            self._thread.start()
            _write_surface_record(self.root, pid=os.getpid(), url=self.url, token=self.token)
        return self.url

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            record = json.loads(_surface_path(self.root).read_text(encoding="utf-8"))
            if (isinstance(record, dict) and record.get("pid") == os.getpid()
                    and secrets.compare_digest(str(record.get("token", "")), self.token)):
                _surface_path(self.root).unlink()
        except (OSError, ValueError):
            pass


def serve_foreground(root: str) -> int:
    surface = ConversationSurface(root)
    url = surface.start()
    print(json.dumps({"mode": "conversation-ui", "status": "ready", "url": url},
                     ensure_ascii=True), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        surface.close()
    return 0


__all__ = ["ConversationSurface", "ConversationUIError", "read_surface_record", "serve_foreground"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="serve a local Summon conversation surface")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("root")
    args = parser.parse_args()
    raise SystemExit(serve_foreground(args.root) if args.serve else 2)
