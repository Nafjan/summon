"""Owned current -> historical -> current rollback proposal; no installed state.
Run only after review: python -B tests/test_l04_composed_rollback.py
Missing Node or Windows junction support is an explicit prerequisite failure.
"""
from pathlib import Path
import ast, hashlib, io, json, os, shutil, subprocess, sys, tempfile, unittest, zipfile

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("l04_rollback_worker.py")
TITLE = "Merge pull request #31: Summon 3.4.0"
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents

def run(argv, cwd, env, data=None, timeout=120):
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            close_fds=True, creationflags=FLAGS) as child:
        try:
            out, err = child.communicate(data, timeout=timeout)
        except BaseException:
            child.kill(); child.communicate()
            raise AssertionError("owned child timeout/interruption") from None
        if child.returncode:
            raise AssertionError("owned child failed; private output withheld")
        return out

def environment(home, override=False):
    result = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        result[key] = str(home)
    result.update(PATH="", SUMMON_TELEMETRY="0", PYTHONDONTWRITEBYTECODE="1")
    if override:
        result["KIMI_CODE_HOME"] = str(home / "kimi-override")
    return result

def extract(zip_bytes, target):
    target.mkdir()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as bundle:
        for item in bundle.infolist():
            output = target / item.filename
            assert inside(output, target), "archive path escape"
            assert ((item.external_attr >> 16) & 0o170000) != 0o120000, "source link refused"
            if item.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(bundle.read(item))

def tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}

def literal(path, name, function=False):
    node = ast.parse(path.read_text(encoding="utf-8"))
    for entry in node.body:
        if function and isinstance(entry, ast.FunctionDef) and entry.name == name:
            return ast.literal_eval(next(x for x in entry.body if isinstance(x, ast.Return)).value)
        if isinstance(entry, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in entry.targets):
            return ast.literal_eval(entry.value)
    raise AssertionError("public fixture constant missing")

def webcrypto(node, source, home, state, key, retained=None):
    scripts = source / "skills/summon/scripts"
    # Import page producer in the fenced Python worker, not this extraction driver.
    page = (home / "probe/page.js").read_text(encoding="utf-8")
    harness = literal(scripts / "test_workspace_page.py", "_script_harness", True)
    fixture = scripts / "test_workspace_encrypted_version_acceptance.py"
    guards, checks = literal(fixture, "_GUARDS"), literal(fixture, "_CHECKS")
    replacements = {
        "Buffer.alloc(32,7).toString('base64url')": "key.key_b64",
        "operator_scope:'a'.repeat(64)": "operator_scope:key.operator_scope",
        "key_epoch:'epoch-1'": "key_epoch:key.key_epoch",
        "key_epoch:'epoch-1',max_text_bytes": "key_epoch:key.key_epoch,max_text_bytes",
    }
    for old, new in replacements.items():
        checks = checks.replace(old, new)
    marker = "let record=JSON.parse(stored.get(PENDING_MESSAGE_SLOT));"
    assert checks.count(marker) == 1
    if retained is None:
        checks = checks.replace(marker, marker + """
 assert.equal(boundaryHits,0);console.log(JSON.stringify({record}));return;""")
    else:
        checks = checks.replace(marker, marker + "\n record=retained;\n")
    config = ("const boundary='supported',version='supported',state=" + json.dumps(state)
              + ",key=" + json.dumps(key) + ",retained=" + json.dumps(retained) + ";\n")
    out = run([node], home, environment(home),
              (harness + guards + page + config + checks).encode(), timeout=20)
    value = json.loads(out)
    if retained is None:
        assert set(value) == {"record"}
        return value["record"]
    assert value == dict(passed=True, boundary="supported", state=state, version="supported")

class ComposedRollbackTests(unittest.TestCase):
    def test_owned_historical_rollback_and_recovery(self):
        git, node = shutil.which("git"), shutil.which("node")
        self.assertTrue(git and node, "explicit prerequisite: Git and Node/WebCrypto")
        with tempfile.TemporaryDirectory(prefix="summon-l04-owned-") as raw:
            owned = Path(raw).resolve()
            # Git is used only for fixed local read operations. No checkout or hooks.
            env = environment(owned)
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0")
            matches = run([git, "--no-pager", "log", "--all", "--format=%H",
                           "--fixed-strings", "--grep=" + TITLE], ROOT, env).decode().splitlines()
            self.assertEqual(len(matches), 1, "historical selection ambiguous/unavailable")
            revision = matches[0]
            self.assertRegex(revision, r"^[0-9a-f]{40,64}$")
            archive = run([git, "--no-pager", "archive", "--format=zip", revision], ROOT, env)
            historical = owned / "historical"
            extract(archive, historical)
            self.assertEqual(json.loads((historical / "plugin.json").read_text())["version"], "3.4.0")
            self.assertFalse(list((historical / "skills/summon/scripts").glob("_workspace*.py")))
            current = owned / "current"
            current.mkdir()
            # Tracked public skill files plus direct Python runtime/test source.
            # Never recurse through unknown JSON/runtime stores.
            names = run([git, "--no-pager", "ls-files", "-z", "--", "skills"],
                        ROOT, env).decode().split("\0")
            public = {name for name in names if name}
            public.update(path.relative_to(ROOT).as_posix()
                          for path in (ROOT/"skills/summon/scripts").glob("*.py"))
            source_bytes = {}
            for relative in sorted(public):
                path = ROOT / relative
                self.assertTrue(path.is_file() and not path.is_symlink() and inside(path,ROOT/"skills"))
                target = current / relative
                target.parent.mkdir(parents=True,exist_ok=True)
                source_bytes[relative] = path.read_bytes()
                target.write_bytes(source_bytes[relative])
            for relative in ("install.py", "plugin.json", "tests/__init__.py",
                             "tests/test_install.py", "tests/test_migration_gate.py"):
                source = ROOT / relative
                if relative == "tests/__init__.py" and not source.exists():
                    continue
                self.assertTrue(source.is_file() and inside(source, ROOT))
                target = current / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            worker = owned / "l04_rollback_worker.py"
            worker.write_bytes(WORKER.read_bytes())
            frozen_sources = tree(current), tree(historical), worker.read_bytes()
            for branch in ("ordinary", "kimi_override", "shared_junction"):
                with self.subTest(branch=branch):
                    home = owned / branch
                    home.mkdir()
                    (home / "probe").mkdir()
                    if branch == "ordinary":
                        (home / ".claude").mkdir()
                        hosts = "claude"
                    elif branch == "kimi_override":
                        (home / "kimi-override").mkdir()
                        hosts = "kimi"
                    else:
                        self.assertEqual(os.name, "nt", "explicit prerequisite: Windows junction branch")
                        import _winapi
                        self.assertTrue(hasattr(_winapi, "CreateJunction"),
                                        "explicit prerequisite: owned junction facility")
                        shared = home / ".gemini/shared"
                        shared.mkdir(parents=True)
                        for alias in ("antigravity", "antigravity-cli"):
                            link = home / ".gemini" / alias
                            self.assertTrue(inside(shared, home) and inside(link.parent, home))
                            _winapi.CreateJunction(str(shared), str(link))
                        hosts = "antigravity,antigravity-cli"
                    child_env = environment(home, branch == "kimi_override")
                    def phase(action, source):
                        out = run([sys.executable, "-I", "-B", str(worker),
                                   action, str(source), str(home), hosts],
                                  owned, child_env)
                        self.assertIn(out, (b"L04_PHASE_OK\n", b"L04_PHASE_OK\r\n"))
                    phase("install", current)
                    phase("seed", current)
                    phase("legacy_seed", historical)
                    keys = json.loads((home / "probe/keys.json").read_text())
                    records = {s: webcrypto(node, current, home, s, keys[s])
                               for s in ("unsent", "sending", "uncertain")}
                    browser = home / "browser"
                    browser.mkdir()
                    pending = browser / "pending.json"
                    pending.write_text(json.dumps(records), encoding="utf-8")
                    before, pending_bytes = tree(home / ".agents"), pending.read_bytes()
                    phase("install", historical)
                    phase("legacy_read", historical)
                    self.assertTrue(tree(home / ".agents") == before, "durable state changed; values withheld")
                    self.assertTrue(pending.read_bytes() == pending_bytes, "pending ciphertext changed; values withheld")
                    phase("install", current)
                    self.assertTrue(tree(home / ".agents") == before, "durable state changed; values withheld")
                    phase("recover", current)
                    recovered = json.loads((home / "probe/recovered-keys.json").read_text())
                    self.assertTrue(recovered == keys, "retention identity changed; values withheld")
                    for state in records:
                        webcrypto(node, current, home, state, recovered[state], records[state])
                    self.assertTrue(pending.read_bytes() == pending_bytes, "pending ciphertext changed; values withheld")
            self.assertTrue((tree(current), tree(historical), worker.read_bytes()) == frozen_sources, "owned source changed; values withheld")
            self.assertTrue(all((ROOT/name).read_bytes()==raw for name,raw in source_bytes.items()),
                            "source changed during owned rehearsal; retry a frozen candidate")

if __name__ == "__main__":
    unittest.main()
