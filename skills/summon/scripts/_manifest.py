"""Native fan-out (``--manifest``): the swarm orchestrator every serious user
was rewriting by hand — thread pool, per-backend concurrency caps, per-job
result files with skip-if-done resume, retries, progress lines.

Manifest format (JSON):
    {
      "defaults": {"cwd": "...", "timeout": "600s", "retries": 1, ...},
      "jobs": [
        {"id": "review-07", "agent": "reviewer", "prompt": "...", ...},
        {"agent": "researcher", "prompt_file": "packets/j2.md", "model": "..."}
      ]
    }
A bare JSON array is accepted as the jobs list. Per-job keys override defaults:
id, agent, prompt | prompt_file, cwd, cli, model, effort, timeout, retries,
json_schema, debug_dir. Each job's envelope lands in
``<results-dir>/<id>.json`` (atomic; an existing valid envelope skips the job —
re-running a crashed swarm resumes where it stopped).

Progress goes to STDERR (one line per completion); STDOUT carries exactly one
summary JSON object, keeping the stdout-purity contract.

Trust model: a manifest is operator-owned local input and runs with the
operator's own filesystem authority. ``prompt_file`` paths are therefore NOT
sandboxed to the manifest directory (an absolute or ``../`` path is honored) —
the same trust you already grant by choosing to run the manifest. Do not feed
this an untrusted third-party manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import re
import subprocess
import uuid
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_JOB_KEYS = ("id", "agent", "prompt", "prompt_file", "cwd", "cli", "model",
             "effort", "timeout", "retries", "json_schema", "debug_dir")
_DEFAULT_CAP = 3
# The SAME duration ceiling --timeout enforces, so a manifest cannot size the parent
# watchdog past what the child would ever accept.
from _cli import _MAX_TIMEOUT_MS  # noqa: E402
from _executor import build_request_identity  # noqa: E402

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _fail(msg: str) -> int:
    print(json.dumps({"status": "error", "error": msg}, ensure_ascii=False))
    return 1


def _parse_concurrency(spec: str | None) -> dict:
    """'agy=2,codex=3,default=4' -> {'agy': 2, 'codex': 3, 'default': 4}."""
    caps = {"default": _DEFAULT_CAP}
    if not spec:
        return caps
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"--concurrency: expected name=N, got {part!r}")
        name, _, num = part.partition("=")
        n = int(num)
        if n < 1:
            raise ValueError(f"--concurrency: {name} must be >= 1")
        caps[name.strip()] = n
    return caps


def _clear_out_file(out_file: str, archive: bool) -> str | None:
    """Make the authoritative result path EMPTY before a dispatch writes to it.

    Returns None on success, or an error string. A prior success is archived (never
    overwritten -- an older archive is a real answer too) instead of deleted, so declining to
    reuse one cannot destroy it. Failure is REPORTED, not swallowed: leaving a stale envelope
    at the authoritative path meant a failed child was followed by the parent re-reading the
    old answer and reporting it, with exit 0, as this run's result.
    """
    if not os.path.exists(out_file):
        return None
    claimed = clear_err = cleanup_err = None
    try:
        if archive:
            # CLAIM the name atomically (O_CREAT|O_EXCL) instead of testing-then-taking it.
            # The check-then-act loop let two writers sharing a --results-dir choose the
            # same free name, and the second os.replace then overwrote the first's archived
            # answer. O_EXCL means exactly one writer can ever own a given name.
            base = out_file + ".superseded"
            dest, n, _denied, _last_denial = base, 0, 0, None
            while True:
                try:
                    # ONLY the open can collide. os.close() was once inside this try too,
                    # so a close() that failed was read as "that name is taken": the loop
                    # moved on having already set `claimed`, the next successful claim
                    # overwrote it, and the earlier reservation leaked as a permanent empty
                    # archive with its fd still open. A close failure is not a collision.
                    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except (FileExistsError, PermissionError) as _claim_err:
                    # PermissionError, not just FileExistsError: on WINDOWS a name whose
                    # file is pending deletion by a concurrent writer fails O_EXCL with
                    # EACCES rather than EEXIST. Treating that as fatal made a transient
                    # race abort the whole clear -- and because _write_error_out refuses to
                    # overwrite when archiving fails, the dispatch's real error envelope was
                    # then dropped and a stale success left in place. Skipping to the next
                    # index is safe: we are only choosing an UNUSED archive name.
                    if isinstance(_claim_err, PermissionError):
                        _denied += 1
                        _last_denial = _claim_err
                        # MEASURE, do not guess. Two heuristics were tried and both were
                        # defeated: a cumulative tally aborted on transient contention
                        # spread across a long run, and a consecutive streak was defeated
                        # by an unwritable directory whose occupied names return EEXIST and
                        # whose free names return EACCES -- the streak never reached two,
                        # so the loop ground through all 10k probes and then reported "too
                        # many superseded copies", which is the wrong diagnosis entirely
                        # (found by cross-vendor review, reproduced on a real WSL directory).
                        #
                        # The question the heuristics were approximating is simply "is this
                        # directory writable at all", so ask it directly, once, with a name
                        # nothing else can be holding.
                        if _denied > 64:
                            # A FRESH name every time. Keying it on _denied reused the name
                            # once the counter reset, so a probe whose cleanup failed (the
                            # remove is best-effort) would make every later probe fail
                            # O_EXCL with EEXIST -- read as "inconclusive" forever, silently
                            # disabling the measurement this exists to perform.
                            # uuid4, not a counter: a per-call sequence collides between
                            # CONCURRENT calls in the same process, and the manifest runs
                            # jobs in threads. The probe must be unique across processes AND
                            # threads or it measures someone else's file.
                            probe = f"{base}.wtest.{os.getpid()}.{uuid.uuid4().hex}"
                            try:
                                _pfd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            except PermissionError:
                                return (f"cannot archive the previous result at {out_file}: "
                                        f"permission denied claiming an archive name "
                                        f"({_claim_err})")
                            except OSError:
                                pass          # inconclusive; keep walking names
                            else:
                                # try/finally, matching the archive claim ten lines above:
                                # a close() that raises must not skip the remove and strand
                                # both the descriptor and the file. The two paths had the
                                # same shape and only one had the care.
                                try:
                                    os.close(_pfd)
                                finally:
                                    try:
                                        os.remove(probe)
                                    except OSError:
                                        pass
                                _denied = 0   # writable: the denials really were contention
                    n += 1
                    dest = f"{base}.{n}"
                    if n > 10_000:             # pathological; do not spin forever
                        if _last_denial is not None:
                            # Denials were seen along the way, so "too many copies" would
                            # name the wrong cause and send the reader to delete files that
                            # are not the problem.
                            return (f"cannot archive the previous result at {out_file}: "
                                    f"permission denied claiming an archive name "
                                    f"({_last_denial})")
                        return (f"cannot archive the previous result at {out_file}: "
                                "too many superseded copies")
                    continue
                # The name is OURS. Record the claim BEFORE closing, so a close() that
                # fails on a network filesystem still leaves the reservation visible to
                # the finally-cleanup instead of stranding it forever.
                claimed = dest
                os.close(fd)                       # OSError here -> outer handler + cleanup
                break
            os.replace(out_file, dest)
            claimed = None                     # the claim now holds the real content
        else:
            os.remove(out_file)
    except FileNotFoundError:
        # Someone else cleared this path first (two runs sharing a --results-dir). The GOAL
        # -- an empty authoritative path -- is met, so this is success, not failure. Only
        # report if the file is somehow still there.
        pass
    except OSError as e:
        # Recorded, NOT returned here: returning from the `try` would discard whatever the
        # `finally` below finds, and the reservation left behind is exactly the thing that
        # goes wrong when the clear fails.
        clear_err = f"cannot clear the previous result at {out_file}: {e}"
    finally:
        if claimed:                            # a name we reserved but never filled
            try:
                os.remove(claimed)
            except OSError as e:
                # Reported, not swallowed: an empty archive left behind is a real (if
                # small) mess, and every other failure on this path is reported. On Windows
                # a close() that raised can leave the handle open, and a file with an open
                # handle cannot be removed -- so this is a genuinely reachable state.
                cleanup_err = f"cannot remove the reserved archive {claimed}: {e}"
    if clear_err or cleanup_err:
        return "; ".join(x for x in (clear_err, cleanup_err) if x)
    if os.path.exists(out_file):
        return f"the previous result at {out_file} is still present after clearing it"
    return None


def _job_agents_dir(job: dict, args, base_cwd: str) -> str:
    """The roster a job's agent will actually be loaded from.

    A job with its own `cwd` resolves its agent from THAT tree's `.agents`, exactly as the
    child will. Resolving every job against the MANIFEST's base roster instead made the
    scheduler pick a different backend than the child dispatched to -- bypassing that
    backend's concurrency cap and labelling its telemetry with the wrong vendor.
    """
    from _loader import get_agents_dir
    return get_agents_dir(args.agents_dir, job.get("cwd") or base_cwd)


def _job_backend(job: dict, agents_dir: str) -> str:
    """The backend a job will dispatch to (for the right semaphore): explicit
    cli > agent frontmatter run-agent > dispatcher default (codex)."""
    if job.get("cli"):
        return job["cli"]
    try:
        from _loader import load_agent
        from _resolver import resolve_cli
        run_agent = load_agent(agents_dir, job["agent"])[0]
        # resolve_cli, NOT `or "codex"`: with an UNPINNED agent the backend comes from
        # CALLER DETECTION, so under CLAUDE_CODE=1 every such job dispatched to claude while
        # the scheduler counted it as codex -- claude's concurrency cap was bypassed
        # entirely and the per-backend telemetry named the wrong vendor.
        return resolve_cli(run_agent)
    except Exception:  # noqa: BLE001 — the child will surface the real error
        return "codex"


def _normalize_jobs(doc, manifest_dir: str) -> tuple:
    """Returns (jobs, error). Applies defaults, resolves prompt_file, assigns ids."""
    if isinstance(doc, list):
        defaults, jobs_raw = {}, doc
    elif isinstance(doc, dict):
        # `doc.get("defaults") or {}` ERASED the type of a falsey non-object, so `[]`, `0`
        # and `""` sailed through the check below. Only a genuinely absent (or null)
        # defaults block becomes {}.
        defaults, jobs_raw = doc.get("defaults"), doc.get("jobs")
        defaults = {} if defaults is None else defaults
        if not isinstance(defaults, dict):
            # `{**defaults, **raw}` on a list raises TypeError before any job is seen
            return None, f"manifest 'defaults' must be an object, got {type(defaults).__name__}"
    else:
        return None, "manifest must be a JSON object with 'jobs' or a JSON array"
    if not isinstance(jobs_raw, list) or not jobs_raw:
        return None, "manifest has no jobs"

    jobs, seen = [], set()
    for i, raw in enumerate(jobs_raw):
        if not isinstance(raw, dict):
            return None, f"job #{i} is not an object"
        job = {**defaults, **raw}
        unknown = set(job) - set(_JOB_KEYS)
        if unknown:
            return None, f"job #{i}: unknown keys {sorted(unknown)}"
        # Type-check every field used as a string BEFORE anything uses one. `prompt_file`
        # was read (os.path.isabs/join) and `cwd` reached os.path.abspath during identity
        # construction -- both OUTSIDE the per-job error handling -- so a list-valued one
        # took down the WHOLE manifest with a TypeError instead of producing one job's
        # error. str ONLY: a numeric `cwd` is not a path, and accepting int/float
        # contradicted the very message this raises.
        # json_schema is deliberately absent: it has its own check further down whose
        # message explains WHY it must be a path, and that wording is worth keeping.
        for _k in ("agent", "prompt", "prompt_file", "cwd", "cli", "model", "effort", "id",
                   "debug_dir"):
            if job.get(_k) is not None and not isinstance(job[_k], str):
                return None, (f"job #{i}: {_k} must be a string, got "
                              f"{type(job[_k]).__name__}")
        if not job.get("agent"):
            return None, f"job #{i}: 'agent' is required"
        if job.get("prompt") is not None and job.get("prompt_file") is not None:
            # Both PRESENT (even prompt: "") used to mean prompt silently won
            # (a defaults-level prompt_file was ignored wholesale). Ambiguous ->
            # rejected on presence, not truthiness.
            return None, f"job #{i}: give 'prompt' or 'prompt_file', not both"
        if job.get("prompt_file") and not job.get("prompt"):
            pf = job["prompt_file"]
            if not os.path.isabs(pf):
                pf = os.path.join(manifest_dir, pf)
            try:
                with open(pf, encoding="utf-8") as fh:
                    job["prompt"] = fh.read()
            except OSError as e:
                return None, f"job #{i}: cannot read prompt_file {pf}: {e}"
        if not job.get("prompt"):
            return None, f"job #{i}: needs 'prompt' or 'prompt_file'"
        # json_schema is forwarded as --json-schema, which the child reads as a
        # FILE PATH. A JSON object here would be str()-coerced into a Python repr
        # (single quotes -> invalid JSON) and fail opaquely in the child; reject
        # it up front with a clear message.
        if job.get("json_schema") is not None and not isinstance(job["json_schema"], str):
            return None, (f"job #{i}: json_schema must be a file path (string), "
                          f"got {type(job['json_schema']).__name__}")
        # prompt_file is resolved above; json_schema / debug_dir are also passed to
        # the child, which resolves relative paths against ITS cwd (the job's cwd,
        # NOT the manifest dir). Anchor them to the manifest dir here so a relative
        # path in the manifest works the way the docs' examples imply.
        for _key in ("json_schema", "debug_dir"):
            _v = job.get(_key)
            if isinstance(_v, str) and _v and not os.path.isabs(_v):
                job[_key] = os.path.join(manifest_dir, _v)
        job_id = str(job.get("id") or f"{job['agent']}-{i:03d}")
        if not _ID_RE.match(job_id) or ".." in job_id:
            return None, f"job #{i}: invalid id {job_id!r} (letters/digits/._-)"
        # Case-INSENSITIVE, because the id becomes `<results-dir>/<id>.json`: on Windows and
        # macOS `Foo` and `foo` are one file, so two such jobs would overwrite each other's
        # result (and the second would resume off the first's envelope). Rejecting on every
        # platform keeps a manifest that works here working there.
        if job_id.lower() in seen:
            return None, (f"duplicate job id {job_id!r} (ids are compared case-insensitively "
                          "because each becomes a <id>.json result file)")
        seen.add(job_id.lower())
        job["id"] = job_id
        jobs.append(job)
    return jobs, None


def _timeout_seconds(spec, default: float = 600.0) -> float:
    """Parse a job timeout the SAME way the child ``--timeout`` does — a bare
    number is MILLISECONDS, suffixes are ms/s/m — and return seconds. (The old
    version read a bare number as seconds and accepted 'h', disagreeing with the
    child and sizing the watchdog 1000x too large.) Only sizes the parent
    watchdog; the child enforces the real deadline, so odd input falls back to
    the default rather than raising."""
    if spec is None:
        return default
    s = str(spec).strip().lower()
    try:
        if s.endswith("ms"):
            ms = float(s[:-2])
        elif s.endswith("s"):
            ms = float(s[:-1]) * 1000
        elif s.endswith("m"):
            ms = float(s[:-1]) * 60_000
        else:
            ms = float(s)  # bare number == milliseconds, matching the child
    except ValueError:
        return default
    if not math.isfinite(ms) or ms <= 0:
        return default
    # Same ceiling the child's --timeout enforces. A finite-but-absurd manifest timeout
    # ('1e308') sized the PARENT watchdog to ~1.5e305 seconds and raised OverflowError
    # ("timestamp out of range for platform time_t") when it was turned into a deadline --
    # killing the parent while its child ran on unmanaged. Odd input falls back to the
    # default here rather than raising, so clamp rather than reject.
    return max(1.0, min(ms, _MAX_TIMEOUT_MS) / 1000)


def _parent_timeout(job: dict, floor: float = 90.0) -> float:
    """Backstop deadline for a child dispatch: the job's own budget + generous
    slack, kept well above the child's ``--timeout`` (which does the real
    enforcement) so a wedged child can't hold a concurrency slot forever."""
    return max(floor, _timeout_seconds(job.get("timeout")) * 1.5 + 60)


class _ChildResult:
    """Duck-typed like a CompletedProcess for _read_envelope (returncode/
    stdout/stderr) plus a timed_out flag."""
    __slots__ = ("returncode", "stdout", "stderr", "timed_out")

    def __init__(self, returncode, stdout, stderr, timed_out):
        self.returncode, self.stdout, self.stderr, self.timed_out = (
            returncode, stdout, stderr, timed_out)


def _dispatch_child(cmd: list, timeout_sec: float, on_spawn=None, on_reap=None):
    """Run a child dispatch with a REAL parent watchdog. Returns
    ``(_ChildResult|None, error|None)``.

    ``subprocess.run(timeout=...)`` would kill only the immediate child on
    timeout and then block in an UNBOUNDED ``communicate()`` if a backend
    descendant still holds stdout — the same hang the executor fix removes. So we
    Popen, bound ``communicate()``, and on timeout kill the whole PROCESS TREE
    (``_kill_tree``) and drain with a bounded ``_safe_communicate``.

    ``on_spawn(proc)``, if given, is called with the live Popen right after spawn
    (before the blocking communicate) so a caller can register it for an external
    process-tree kill (e.g. the council's overall-timeout / early-exit). A killed
    child unblocks communicate() here and returns as a normal timed_out result.

    ``on_reap(proc)``, if given, is called the instant ``communicate()`` returns
    (the leader is reaped) — BEFORE any envelope file read — so a caller can
    UNREGISTER the child adjacent to its reap, shrinking the window in which an
    external snapshot-kill could still target the (now reaping) pid."""
    from _executor import _kill_tree, _safe_communicate
    from _spawn import popen_flags
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", **popen_flags())
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if on_spawn is not None:
        try:
            on_spawn(proc)
        except Exception:  # noqa: BLE001 — registration must never break the dispatch
            pass
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = _safe_communicate(proc)
    if on_reap is not None and not timed_out:
        # Deregister ADJACENT to reap -- but ONLY on a CLEAN communicate() return, which
        # is the sole reliable "whole tree is done" signal: communicate() returns only
        # after stdout+stderr hit EOF, and a descendant still holding stdout would have
        # BLOCKED it into the timeout path below. The leader's poll() is NOT a proxy for
        # this: a leader can exit while a stdout-holding grandchild lives, and killpg
        # needs the (now-registered) leader pid to reach that grandchild. So on the
        # timed_out path we deliberately do NOT deregister -- the child stays registered
        # for the council's kill loop / final teardown, which killpg the whole group.
        try:
            on_reap(proc)
        except Exception:  # noqa: BLE001 — deregistration must never break the dispatch
            pass
    return _ChildResult(proc.returncode, out, err, timed_out), None


def _existing_envelope(out_file: str) -> dict | None:
    """A valid envelope already on disk for this job (swarm resume), else None."""
    try:
        with open(out_file, encoding="utf-8") as fh:
            env = json.load(fh)
        if isinstance(env, dict) and env.get("status"):
            return env
    except (OSError, ValueError):
        pass
    return None


def _read_envelope(out_file: str, proc) -> dict:
    """The child's --out file is the authoritative envelope. Fall back to the
    child's exit info only if the file is missing/corrupt (child crashed before
    writing) — NEVER slice stdout, which host banners can pollute."""
    env = _existing_envelope(out_file)
    if env is not None:
        return env
    # Combine BOTH streams: the real traceback often goes to stdout while stderr
    # only carries a shell/hook banner. `stderr or stdout` would surface just the
    # banner and lose the actual error, so concatenate (stdout first).
    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()[-500:]
    return {"status": "error",
            "error": f"child produced no valid envelope (exit {proc.returncode}): {combined}"}


def _job_identity(job: dict, args) -> dict:
    """Adapter: a manifest job's RAW inputs, handed to the one shared identity builder.

    Built from the SAME expressions _child_cmd forwards to the child, and it does no
    derivation of its own -- the shared builder owns every hash and every environment-backed
    field, so the parent's view and the child's cannot diverge by construction. (They did:
    the child gained the env-backed credit/effort controls while this side did not, so with
    SUMMON_DEFAULT_EFFORT set every manifest restart re-dispatched every finished job.)
    """
    return build_request_identity(
        agent=job["agent"], prompt=job["prompt"],
        cwd=os.path.abspath(job.get("cwd") or args.cwd or os.getcwd()),
        agents_dir=args.agents_dir, cli=job.get("cli"), model=job.get("model"),
        effort=job.get("effort"), json_schema=job.get("json_schema"))


def _child_cmd(job: dict, args, out_file: str) -> list:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    cmd = [sys.executable, script,
           "--agent", job["agent"], "--prompt", job["prompt"],
           "--cwd", os.path.abspath(job.get("cwd") or args.cwd or os.getcwd()),
           "--out", out_file]
    if args.agents_dir:
        cmd += ["--agents-dir", args.agents_dir]
    for key, flag in (("cli", "--cli"), ("model", "--model"), ("effort", "--effort"),
                      ("timeout", "--timeout"), ("json_schema", "--json-schema"),
                      ("debug_dir", "--debug-dir")):
        if job.get(key):
            cmd += [flag, str(job[key])]
    retries = job.get("retries", args.retries)
    if retries:
        cmd += ["--retries", str(retries)]
    return cmd


def run_manifest(args) -> int:
    """Entry point for ``--manifest``. Returns the process exit code."""
    try:
        with open(args.manifest, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        return _fail(f"cannot load manifest {args.manifest}: {e}")

    jobs, err = _normalize_jobs(doc, os.path.dirname(os.path.abspath(args.manifest)))
    if err:
        return _fail(err)
    try:
        caps = _parse_concurrency(args.concurrency)
    except ValueError as e:
        return _fail(str(e))

    base_cwd = os.path.abspath(args.cwd or os.getcwd())
    results_dir = os.path.abspath(args.results_dir or os.path.join(base_cwd, ".agents", "results"))
    os.makedirs(results_dir, exist_ok=True)
    # Use the SAME agent discovery as a direct dispatch (get_agents_dir), not a bare
    # <cwd>/.agents — otherwise a manifest without --agents-dir couldn't find the
    # roster and every job silently fell back to the default backend.
    from _loader import get_agents_dir
    agents_dir = get_agents_dir(args.agents_dir, base_cwd)

    # Pre-build one semaphore per backend BEFORE the pool starts — lazy creation
    # from multiple worker threads is a check-then-act race that can exceed a
    # backend's cap. Resolve each job's backend once here (also reused below).
    job_backends = {j["id"]: _job_backend(j, _job_agents_dir(j, args, base_cwd))
                    for j in jobs}
    sems: dict = {b: threading.BoundedSemaphore(caps.get(b, caps["default"]))
                  for b in set(job_backends.values())}

    lock = threading.Lock()
    done_count = {"n": 0}
    started = time.monotonic()

    def run_job(job: dict) -> dict:
        out_file = os.path.join(results_dir, f"{job['id']}.json")
        backend = job_backends[job["id"]]
        t0 = time.monotonic()
        # Resume: a valid envelope already on disk means this job completed on a
        # prior run. Short-circuit HERE (before taking a semaphore slot or
        # spawning a child) so the skip is both accurate AND free — the child's
        # own --out skip would have marked it, but only in stdout the parent no
        # longer reads.
        # Resume: only a TERMINAL success means "done". A prior error/blocked/
        # partial -- OR a SUSPECT success (status=success but report_ok=false) --
        # is re-run, so re-launching a swarm RETRIES its failures AND its
        # unparseable results instead of skipping them permanently. Shared with
        # the direct --out skip via is_terminal_success so both agree.
        # The parent skips WITHOUT spawning, so the child's own identity check never runs
        # for a manifest job -- the parent has to make the same check itself, or editing a
        # job's prompt while keeping its id silently returns the previous answer.
        from _executor import (envelope_answers_request, is_terminal_success,
                               request_fingerprint)
        prior = _existing_envelope(out_file)
        _ident = _job_identity(job, args)
        _fp = request_fingerprint(**_ident)
        # Pass the legacy-fallback fields too. Without them the parent and the child
        # DISAGREE on pre-0.10.2 envelopes: the child re-dispatches on a proven prompt/agent
        # mismatch while the parent, which never reaches the child, reused the stale answer.
        _reusable, _note = envelope_answers_request(
            prior, _fp, hashlib.sha256(str(_ident["prompt"]).encode("utf-8")).hexdigest(),
            _ident["agent"], _ident)
        if is_terminal_success(prior) and _reusable:
            if _note:
                _pw = prior.get("warnings")
                _pw = _pw if isinstance(_pw, list) else ([] if _pw is None else [str(_pw)])
                prior["warnings"] = _pw + [_note]
            envelope, skipped = prior, True
        else:
            skipped = False
            envelope = None
            with sems[backend]:
                try:
                    # Clear any stale envelope from a prior failed run FIRST, so
                    # that after a watchdog kill the absence of a fresh file means
                    # "this run failed" — not a masking re-read of the old result.
                    # A prior SUCCESS we merely declined to reuse is ARCHIVED rather than
                    # deleted, so a false refusal cannot destroy a completed answer.
                    clear_err = _clear_out_file(out_file, is_terminal_success(prior))
                    if clear_err:
                        # Swallowing this was a FALSE SUCCESS: the stale envelope stayed at
                        # the authoritative path, the child failed, and the parent re-read
                        # the old answer and reported it as this run's result with exit 0.
                        # If the path cannot be cleared we must not dispatch over it.
                        envelope, proc, spawn_err = {"status": "error", "error": clear_err}, None, None
                    else:
                        proc, spawn_err = _dispatch_child(_child_cmd(job, args, out_file),
                                                          _parent_timeout(job))
                    if envelope is not None:
                        pass
                    elif spawn_err:
                        envelope = {"status": "error", "error": spawn_err}
                    elif proc.timed_out and _existing_envelope(out_file) is None:
                        # Watchdog fired and the child wrote nothing (tree killed).
                        envelope = {"status": "error",
                                    "error": f"child exceeded parent watchdog "
                                             f"({int(_parent_timeout(job))}s); process tree killed"}
                    else:
                        # The child's --out file is AUTHORITATIVE — never parse the
                        # child's stdout, which a shell/hook banner can pollute.
                        envelope = _read_envelope(out_file, proc)
                except Exception as e:  # noqa: BLE001 — one job must never crash the pool
                    envelope = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        # Always leave forensics: if the job ran (not skipped) but the child wrote
        # NO --out envelope (early validation error, spawn failure, watchdog kill),
        # persist the error envelope ourselves — so `result_file` in the summary
        # actually exists and a failed job is never zero-forensics.
        if not skipped and not os.path.exists(out_file):
            try:
                with open(out_file, "w", encoding="utf-8") as fh:
                    json.dump(envelope, fh, ensure_ascii=False)
            except OSError:
                pass
        status = envelope.get("status", "error")
        with lock:
            done_count["n"] += 1
            print(f"[{done_count['n']}/{len(jobs)}] {job['id']} "
                  f"backend={backend} status={status}"
                  f"{' (skipped)' if skipped else ''} "
                  f"elapsed={int(time.monotonic() - t0)}s", file=sys.stderr, flush=True)
        return {"id": job["id"], "backend": backend, "status": status,
                "skipped": skipped,
                # WHY it failed, in the summary itself. Without it a job that could not clear
                # its own result path reported `status: error` while `result_file` still
                # pointed at the STALE SUCCESS envelope, with nothing anywhere saying so.
                **({"error": envelope["error"]} if envelope.get("error") else {}),
                "result_file": out_file,
                "report_status": (envelope.get("report") or {}).get("status"),
                "suspect": envelope.get("suspect", False)}

    workers = min(len(jobs), max(1, sum(caps.get(b, caps["default"])
                                        for b in set(job_backends.values()))))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(run_job, jobs))

    failed = [o["id"] for o in outcomes if o["status"] != "success"]
    summary = {
        "manifest": os.path.abspath(args.manifest),
        "total": len(jobs),
        "succeeded": len(jobs) - len(failed),
        "failed": failed,
        "skipped": [o["id"] for o in outcomes if o["skipped"]],
        "suspect": [o["id"] for o in outcomes if o.get("suspect")],
        "results_dir": results_dir,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "jobs": outcomes,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not failed else 1
