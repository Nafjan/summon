"""Private owned worker for test_l04_composed_rollback; never invoke on managed roots."""
from pathlib import Path
import builtins, contextlib, hashlib, io, json, os, re, runpy, socket, subprocess, sys, tempfile
from unittest import mock

action, source_text, home_text, hosts = sys.argv[1:]
SOURCE, HOME = Path(source_text).resolve(), Path(home_text).resolve()
OWNED = Path(__file__).resolve().parent
assert OWNED.name.startswith("summon-l04-owned-")
def inside(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents
assert inside(SOURCE, OWNED) and inside(HOME, OWNED)
assert SOURCE.name in {"current", "historical"} and HOME.name in {"ordinary", "kimi_override", "shared_junction"}
assert action in {"install", "seed", "legacy_seed", "legacy_read", "recover"}
assert hosts == {"ordinary":"claude", "kimi_override":"kimi", "shared_junction":"antigravity,antigravity-cli"}[HOME.name]
assert all(os.environ.get(k) == str(HOME) for k in ("HOME","USERPROFILE","APPDATA","LOCALAPPDATA","TEMP","TMP"))
assert os.environ.get("PATH") == ""
assert not (set(os.environ) - {"HOME","USERPROFILE","APPDATA","LOCALAPPDATA","TEMP","TMP",
    "PATH","SUMMON_TELEMETRY","PYTHONDONTWRITEBYTECODE","SystemRoot","WINDIR","KIMI_CODE_HOME","SYSTEMROOT"})
if HOME.name == "kimi_override":
    assert os.environ["KIMI_CODE_HOME"] == str(HOME/"kimi-override")
EXPECTED_SKILL = SOURCE / "skills/summon"
HOST_ROOTS = {"ordinary":[HOME/".claude"],"kimi_override":[HOME/"kimi-override"],
             "shared_junction":[HOME/".gemini/antigravity",HOME/".gemini/antigravity-cli"]}[HOME.name]
INSTALLED = (HOST_ROOTS[0]/"skills/summon").resolve()
assert inside(INSTALLED, HOME)
READ_INSTALLED = action in {"legacy_read", "recover"}
SCRIPTS = INSTALLED/"scripts" if READ_INSTALLED else EXPECTED_SKILL/"scripts"
# Test helpers are copied separately for recovery. Their calculated scripts path
# is absent, so their imports resolve only through the selected installed tree.
HELPERS = HOME/"reader-fixtures"
sys.path[:0] = [str(HELPERS if READ_INSTALLED else SOURCE), str(SCRIPTS)]
sys.dont_write_bytecode = True
tempfile.tempdir = str(HOME)
PROBE = HOME / "probe"
TRANSPORT = SCRIPTS / "_workspace_transport.py"
TRANSPORT_SHA = hashlib.sha256(TRANSPORT.read_bytes()).digest() if TRANSPORT.exists() else None
READABLE = [SOURCE, HOME, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), Path(__file__).resolve()]
DENIED = ("_auth","_credentials","_windows_credentials","_nous_credentials","_arkcli_creds","keyring","win32cred")
hits, children = [], []
allow_spawn = False
def denied():
    hits.append("boundary")
    raise AssertionError("owned rollback boundary refused")
def audit(event, args):
    if event == "subprocess.Popen":
        if not allow_spawn: denied()
    elif event.startswith(("subprocess.","os.spawn","os.exec")) or event in {"os.system","os.startfile"}:
        denied()
    if event.startswith("socket."):
        if event == "socket.__new__":
            if args[1] != socket.AF_INET or args[2] != socket.SOCK_STREAM: denied()
        elif event == "socket.bind":
            if args[1][0] != "127.0.0.1": denied()
        elif event != "socket.gethostname":
            denied()
    if event == "ctypes.dlopen":
        library = args[0]
        if os.name == "nt":
            if not isinstance(library,str) or library.lower() not in {"advapi32","kernel32","advapi32.dll","kernel32.dll"}:
                denied()
        elif library is not None: denied()
    if event == "import" and str(args[0]).startswith(DENIED): denied()
    if event == "open" and not isinstance(args[0],int):
        if os.fsdecode(args[0]).lower() == os.devnull.lower(): return
        path = Path(os.fsdecode(args[0])).resolve()
        flags = args[2] if len(args)>2 and isinstance(args[2],int) else 0
        writing = any(c in (args[1] or "") for c in "wax+") or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)
        if writing and not inside(path,HOME): denied()
        if not writing and not any(inside(path,root) for root in READABLE): denied()
    if event in {"os.remove","os.rmdir","os.mkdir","os.rename","os.link","os.symlink","os.chmod","os.utime"}:
        paths = args[:2] if event in {"os.rename","os.link","os.symlink"} else args[:1]
        if any(not isinstance(p,int) and not inside(os.fsdecode(p),HOME) for p in paths): denied()
original_import = builtins.__import__
def guarded_import(name,*args,**kwargs):
    if name.startswith(DENIED): denied()
    return original_import(name,*args,**kwargs)
original_popen = subprocess.Popen
class OwnedPopen(original_popen):
    def __init__(self,args,*positional,**kwargs):
        global allow_spawn
        if not (isinstance(args,(tuple,list)) and len(args)==5
                and Path(args[0]).resolve()==Path(sys.executable).resolve()
                and list(args[1:3])==["-I","-B"] and Path(args[3]).resolve()==TRANSPORT
                and args[4] in {"--synthetic-worker","--synthetic-supervisor-consumer"}): denied()
        if hashlib.sha256(TRANSPORT.read_bytes()).digest()!=TRANSPORT_SHA: denied()
        if (kwargs.get("shell") or kwargs.get("close_fds") is not True
            or kwargs.get("stdin")!=subprocess.PIPE or kwargs.get("stdout")!=subprocess.PIPE
            or kwargs.get("stderr")!=subprocess.DEVNULL): denied()
        if os.name=="nt" and not kwargs.get("creationflags",0)&subprocess.CREATE_NO_WINDOW: denied()
        env=kwargs.get("env")
        if not isinstance(env,dict) or set(env)-{"SystemRoot","WINDIR","SYSTEMROOT","TEMP","TMP"}: denied()
        if any(not inside(env.get(k,""),HOME) for k in ("TEMP","TMP")): denied()
        kwargs["cwd"]=str(HOME)
        kwargs["env"]=dict(env,HOME=str(HOME),USERPROFILE=str(HOME),APPDATA=str(HOME),
            LOCALAPPDATA=str(HOME),PATH="",SUMMON_TELEMETRY="0",PYTHONDONTWRITEBYTECODE="1")
        allow_spawn=True
        try: super().__init__(args,*positional,**kwargs)
        finally: allow_spawn=False
        children.append(self)
builtins.__import__=guarded_import
subprocess.Popen=OwnedPopen
os.kill=lambda *args,**kwargs: denied()
sys.addaudithook(audit)
ROOT=HOME/".agents/summon"
os.environ.update(SUMMON_FLEET_APPROVAL_STORE=str(ROOT/"fleet/store.json"),
                  SUMMON_FLEET_APPROVAL_KEY=str(ROOT/"fleet/store.key"))
def save(name,value):
    (PROBE/name).write_text(json.dumps(value),encoding="utf-8")
def host_keys(mode):
    from _workspace_entry import WorkspaceHost
    from _workspace_page import page_bytes
    # OS chooses an available loopback port; no remote client connection is permitted.
    probe=socket.socket();probe.bind(("127.0.0.1",0));port=probe.getsockname()[1];probe.close()
    host=WorkspaceHost(str(ROOT/"host-runs"),"rollback-host",port,mode=mode)
    try:
        # HTTPServer uses getfqdn only to assign a server-name field.
        # Keep real listener/bind/host lifecycle and all DNS guards.
        def synthetic_loopback_name(hostname=""):
            if hostname != "127.0.0.1":
                denied()
            return "localhost"
        with mock.patch.object(socket,"getfqdn",synthetic_loopback_name):
            host.start()
        keys={state:host.runtime.derive_operator_draft_key(host._draft_handle,
              {"target":"operator-target","operation_key":"e"*32,"state":state})
              for state in ("unsent","sending","uncertain")}
        script=re.search(r'<script nonce="[^"]+">(.*?)</script>',page_bytes(nonce="n"*24).decode(),re.S)
        assert script
        (PROBE/"page.js").write_text(script.group(1),encoding="utf-8")
        return keys
    finally: host.stop()
def verify_installed_payload():
    # Both reviewed installers have this finite payload contract. Derive and
    # check it from the selected source rather than trust installed metadata.
    import ast
    module=ast.parse((SOURCE/"install.py").read_text(encoding="utf-8"))
    payload=next(ast.literal_eval(item.value) for item in module.body
                 if isinstance(item,ast.Assign) and any(
                     isinstance(target,ast.Name) and target.id=="SKILL_PAYLOAD"
                     for target in item.targets))
    assert payload==["SKILL.md","scripts","references","agents","examples"]
    expected={}
    for relative in payload:
        base=EXPECTED_SKILL/relative
        files=base.rglob("*") if base.is_dir() else [base]
        for path in files:
            if not path.is_file(): continue
            name=path.relative_to(EXPECTED_SKILL)
            # No cache inputs are allowed in this source-qualified rehearsal.
            assert not any(part in {"__pycache__",".pytest_cache"} for part in name.parts)
            assert path.suffix!=".pyc" and inside(path,EXPECTED_SKILL) and not path.is_symlink()
            expected[name.as_posix()]=path.read_bytes()
    assert expected
    for root in HOST_ROOTS:
        dest=(root/"skills/summon").resolve()
        assert inside(dest,HOME)
        actual={}
        for path in dest.rglob("*"):
            assert inside(path,dest) and not path.is_symlink()
            if path.is_file() and path.relative_to(dest).as_posix()!=".summon-install.json":
                actual[path.relative_to(dest).as_posix()]=path.read_bytes()
        assert actual==expected, "installed payload differs; values withheld"
        manifest=json.loads((dest/".summon-install.json").read_text())
        assert manifest.get("installed_by")=="summon"
def prepare_reader_helpers():
    (HELPERS/"tests").mkdir(parents=True,exist_ok=True)
    (HELPERS/"tests/__init__.py").write_text("",encoding="utf-8")
    for name in ("test_install.py","test_migration_gate.py"):
        (HELPERS/"tests"/name).write_bytes((SOURCE/"tests"/name).read_bytes())
    assert not (HELPERS/"skills").exists()
def verify_reader_origins():
    if not READ_INSTALLED: return
    for module in tuple(sys.modules.values()):
        filename=getattr(module,"__file__",None)
        if not filename: continue
        path=Path(filename).resolve()
        assert not inside(path,EXPECTED_SKILL/"scripts"), "extracted reader import refused"
    import _conversation,_fleet_approval
    required=[_conversation,_fleet_approval]
    if action=="recover":
        import _workspace_entry,_workspace_runtime,_swarm_coordinator,_jobs
        required.extend([_workspace_entry,_workspace_runtime,_swarm_coordinator,_jobs])
    assert all(inside(module.__file__,SCRIPTS) for module in required), "reader provenance refused"
def main():
    if action=="install":
        sys.argv=[str(SOURCE/"install.py"),"--hosts",hosts,"--no-agents"]
        try: runpy.run_path(str(SOURCE/"install.py"),run_name="__main__")
        except SystemExit as exc: assert exc.code in (None,0)
        verify_installed_payload()
    elif action=="seed":
        from tests import test_migration_gate as migration
        migration._materialize_durable_state(str(HOME))
        # Close only this synthetic prepared job; held workspace uncertainty is untouched.
        migration._jobs._atomic_write_json(migration._jobs.result_path(str(ROOT/"jobs"),"a"*32),
            {"status":"success","job_nonce":"migration-fixture","prompt_sha256":"b"*64,
             "summon":{"version":"3.2.1","scripts_sha256":"c"*64}})
        import _fleet_approval
        save("approval-generation.json",_fleet_approval.status()["generation"])
        save("keys.json",host_keys("demo-create"))
    elif action=="legacy_seed":
        from _conversation import ConversationJournal
        journal=ConversationJournal.create(ROOT/"rooms",session_id="legacy-room",
            project_id="summon",project_root=ROOT/"project",initiator_host="test",
            initiator_agent="reviewer",participants=[{"agent":"reviewer","role":"reviewer","name":"Reviewer","version":"fixture"}])
        journal.append_human_message("Synthetic historical control")
    elif action=="legacy_read":
        verify_installed_payload()
        from _conversation import ConversationJournal,ConversationError
        ConversationJournal.open(ROOT/"rooms","legacy-room")
        try: ConversationJournal.open(ROOT/"rooms","migration-room")
        except ConversationError as exc: assert str(exc)=="unsupported conversation room schema"
        else: raise AssertionError("historical v2 mutation fence missing")
        import _fleet_approval
        assert _fleet_approval.status()["generation"]==json.loads((PROBE/"approval-generation.json").read_text())
    elif action=="recover":
        verify_installed_payload()
        prepare_reader_helpers()
        from tests import test_migration_gate as migration
        migration._assert_durable_state_readable(str(HOME),expect_active=False)
        import _fleet_approval
        assert _fleet_approval.status()["generation"]==json.loads((PROBE/"approval-generation.json").read_text())
        save("recovered-keys.json",host_keys("open"))
try:
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        main()
        verify_reader_origins()
    assert not hits
finally:
    for child in children:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)
        for stream in (child.stdin,child.stdout,child.stderr):
            if stream is not None: stream.close()
    assert not hits
print("L04_PHASE_OK")
