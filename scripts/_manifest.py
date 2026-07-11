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
    """Parse a job timeout ('900s', '10m', '2h', '500ms', or a bare number of
    seconds) to seconds. Falls back to the dispatcher default on anything odd —
    this only sizes the parent watchdog, the child enforces the real deadline."""
    if spec is None:
        return default
    s = str(spec).strip().lower()
    try:
        if s.endswith("ms"):
            return max(1.0, float(s[:-2]) / 1000)
        if s.endswith("s"):
            return max(1.0, float(s[:-1]))
        if s.endswith("m"):
            return max(1.0, float(s[:-1]) * 60)
        if s.endswith("h"):
            return max(1.0, float(s[:-1]) * 3600)
        return max(1.0, float(s))
    except ValueError:
        return default


def _parent_timeout(job: dict, floor: float = 90.0) -> float:
    """Backstop deadline for a child dispatch: the job's own budget + generous
    slack, kept well above the child's ``--timeout`` (which does the real
    enforcement) so a wedged child can't hold a concurrency slot forever."""
    return max(floor, _timeout_seconds(job.get("timeout")) * 1.5 + 60)


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
    agents_dir = args.agents_dir or os.path.join(base_cwd, ".agents")

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
        # Resume: only a SUCCESS envelope means "done". A prior error/blocked/
        # partial envelope is re-run, so re-launching a swarm RETRIES its
        # failures (what users expect) instead of skipping them permanently.
        prior = _existing_envelope(out_file)
        if prior is not None and prior.get("status") == "success":
            envelope, skipped = prior, True
        else:
            skipped = False
            with sems[backend]:
                try:
                    proc = subprocess.run(_child_cmd(job, args, out_file),
                                          capture_output=True, text=True,
                                          encoding="utf-8", errors="replace",
                                          stdin=subprocess.DEVNULL,
                                          timeout=_parent_timeout(job))
                    # The child wrote its envelope to --out (atomic). That file is
                    # AUTHORITATIVE — never parse the child's stdout, which a shell
                    # profile / hook banner can pollute with brace-containing noise.
                    envelope = _read_envelope(out_file, proc)
                except subprocess.TimeoutExpired:
                    # Parent watchdog: the child blew far past its own --timeout
                    # (a wedged child would otherwise hold this backend's slot and
                    # stall the whole swarm). subprocess.run already killed it;
                    # prefer any partial envelope it wrote before dying.
                    envelope = _existing_envelope(out_file) or {
                        "status": "error",
                        "error": f"child exceeded parent watchdog ({int(_parent_timeout(job))}s)"}
                except Exception as e:  # noqa: BLE001 — one job must never crash the pool
                    envelope = {"status": "error", "error": f"{type(e).__name__}: {e}"}
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
