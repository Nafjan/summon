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

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_JOB_KEYS = ("id", "agent", "prompt", "prompt_file", "cwd", "cli", "model",
             "effort", "timeout", "retries", "json_schema", "debug_dir")
_DEFAULT_CAP = 3
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


def _job_backend(job: dict, agents_dir: str) -> str:
    """The backend a job will dispatch to (for the right semaphore): explicit
    cli > agent frontmatter run-agent > dispatcher default (codex)."""
    if job.get("cli"):
        return job["cli"]
    try:
        from _loader import load_agent
        run_agent = load_agent(agents_dir, job["agent"])[0]
        return run_agent or "codex"
    except Exception:  # noqa: BLE001 — the child will surface the real error
        return "codex"


def _normalize_jobs(doc, manifest_dir: str) -> tuple:
    """Returns (jobs, error). Applies defaults, resolves prompt_file, assigns ids."""
    if isinstance(doc, list):
        defaults, jobs_raw = {}, doc
    elif isinstance(doc, dict):
        defaults, jobs_raw = doc.get("defaults") or {}, doc.get("jobs")
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
        if job_id in seen:
            return None, f"duplicate job id {job_id!r}"
        seen.add(job_id)
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
    if ms <= 0:
        return default
    return max(1.0, ms / 1000)


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
    popen_extra = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", **popen_extra)
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
    if on_reap is not None and proc.poll() is not None:
        # Deregister ADJACENT to reap (before the caller's file reads) -- but ONLY once
        # the child is PROVEN terminated. If bounded _safe_communicate gave up while the
        # process is somehow still alive (returncode None -- a kill that has not yet
        # landed, or an unkillable proc), leave it REGISTERED so the council's kill loop
        # keeps targeting it; the run_stage finally is the eventual backstop when this
        # dispatch returns. Never drop a still-live child from enforcement.
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
    job_backends = {j["id"]: _job_backend(j, agents_dir) for j in jobs}
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
        from _executor import is_terminal_success
        prior = _existing_envelope(out_file)
        if is_terminal_success(prior):
            envelope, skipped = prior, True
        else:
            skipped = False
            with sems[backend]:
                try:
                    # Clear any stale envelope from a prior failed run FIRST, so
                    # that after a watchdog kill the absence of a fresh file means
                    # "this run failed" — not a masking re-read of the old result.
                    try:
                        os.remove(out_file)
                    except OSError:
                        pass
                    proc, spawn_err = _dispatch_child(_child_cmd(job, args, out_file),
                                                      _parent_timeout(job))
                    if spawn_err:
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
