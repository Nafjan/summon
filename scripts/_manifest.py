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
        job_id = str(job.get("id") or f"{job['agent']}-{i:03d}")
        if not _ID_RE.match(job_id) or ".." in job_id:
            return None, f"job #{i}: invalid id {job_id!r} (letters/digits/._-)"
        if job_id in seen:
            return None, f"duplicate job id {job_id!r}"
        seen.add(job_id)
        job["id"] = job_id
        jobs.append(job)
    return jobs, None


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

    sems: dict = {}
    def sem_for(backend: str) -> threading.Semaphore:
        if backend not in sems:
            sems[backend] = threading.BoundedSemaphore(caps.get(backend, caps["default"]))
        return sems[backend]

    lock = threading.Lock()
    done_count = {"n": 0}
    started = time.monotonic()

    def run_job(job: dict) -> dict:
        out_file = os.path.join(results_dir, f"{job['id']}.json")
        backend = _job_backend(job, agents_dir)
        with sem_for(backend):
            t0 = time.monotonic()
            try:
                proc = subprocess.run(_child_cmd(job, args, out_file),
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      stdin=subprocess.DEVNULL)
                envelope = json.loads(proc.stdout[proc.stdout.index("{"):]) \
                    if "{" in proc.stdout else {"status": "error",
                                                "error": f"no envelope on stdout (exit {proc.returncode})"}
            except (OSError, ValueError) as e:
                envelope = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        status = envelope.get("status", "error")
        with lock:
            done_count["n"] += 1
            print(f"[{done_count['n']}/{len(jobs)}] {job['id']} "
                  f"backend={backend} status={status}"
                  f"{' (skipped)' if envelope.get('skipped') else ''} "
                  f"elapsed={int(time.monotonic() - t0)}s", file=sys.stderr, flush=True)
        return {"id": job["id"], "backend": backend, "status": status,
                "skipped": bool(envelope.get("skipped")),
                "result_file": out_file,
                "report_status": (envelope.get("report") or {}).get("status"),
                "suspect": envelope.get("suspect", False)}

    workers = min(len(jobs), sum(caps.get(b, caps["default"])
                                 for b in {_job_backend(j, agents_dir) for j in jobs}) or 1)
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
