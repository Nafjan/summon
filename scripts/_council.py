"""Council mode (``--council``) — decide by consensus of diverse models/personas.

Pose a question to N council members (agents, each encoding a model + persona),
optionally let them cross-examine each other's positions, then a chairman
synthesizes a consensus recommendation noting agreement and dissent. Inspired by
the llm-council / claude-council pattern.

    run_subagent.py --council --question "SQL or NoSQL for this?" --cwd <abs>
    run_subagent.py --council --question-file q.md \
        --members planner,reviewer,researcher,pair --chairman fable --rounds 2 --cwd <abs>

Flow: round 1 (independent positions, in parallel with per-backend concurrency)
-> optional round 2 (each member sees peers' anonymized positions and refines)
-> chairman reads all final positions and returns the consensus. Members are
just agents, so the diversity is real (different vendors AND models), and you can
author custom-persona members with --new-agent.

STDOUT carries exactly one council envelope; per-member progress goes to STDERR.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# A deliberately vendor-DIVERSE default (a council of clones is pointless): Claude
# Opus, Codex, Antigravity/Gemini, and Claude Sonnet — override with --members.
DEFAULT_MEMBERS = ["planner", "reviewer", "researcher", "pair"]
DEFAULT_CHAIRMAN = "fable"          # the escalation/synthesis tier
_POSITION_CAP = 4000                # chars of each position shared with peers/chairman
_PER_BACKEND_CAP = 3


def _fail(msg: str) -> int:
    print(json.dumps({"mode": "council", "status": "error", "error": msg}, ensure_ascii=False))
    return 1


def _position(envelope: dict) -> str:
    """A member's stated position: the report SUMMARY if present, else the result."""
    rep = envelope.get("report") or {}
    if rep.get("summary"):
        body = rep["summary"]
        if rep.get("findings"):
            body += "\n" + rep["findings"]
        return body[:_POSITION_CAP]
    return (envelope.get("result") or envelope.get("error") or "(no position)")[:_POSITION_CAP]


def _round1_prompt(question: str) -> str:
    return (
        "You are one of several independent advisors on a council. Answer this "
        "question on your own, substantively and decisively — do not hedge.\n\n"
        f"QUESTION:\n{question}\n\n"
        "Give your position and the reasoning behind it. End with your exact Final "
        "report block; put your one-line recommendation in SUMMARY and your "
        "supporting analysis in the work-product field.")


def _round2_prompt(question: str, peers: list) -> str:
    peer_text = "\n\n".join(f"[Advisor {chr(65+i)}]: {p}" for i, p in enumerate(peers))
    return (
        "Council round 2. You have now seen the other advisors' positions "
        "(anonymized). Reconsider yours: defend it, refine it, or change it — and "
        "say explicitly where you now AGREE and where you still DISAGREE, with why.\n\n"
        f"QUESTION:\n{question}\n\nOTHER ADVISORS' POSITIONS:\n{peer_text}\n\n"
        "End with your Final report block; SUMMARY = your final one-line position.")


def _chairman_prompt(question: str, members: list) -> str:
    positions = "\n\n".join(
        f"[{m['agent']} — {m.get('model') or m['backend']}]: {m['position']}"
        for m in members if m.get("position"))
    return (
        "You are the COUNCIL CHAIRMAN. Several advisors (different models and "
        "personas) have given their final positions on the question below. Your job "
        "is to synthesize a CONSENSUS RECOMMENDATION — and to make the call even if "
        "the council is split.\n\n"
        f"QUESTION:\n{question}\n\nADVISORS' FINAL POSITIONS:\n{positions}\n\n"
        "Deliver: (1) the DECISION you recommend, (2) your CONFIDENCE 0.0-1.0, "
        "(3) the points of AGREEMENT across advisors, (4) the points of DISSENT — "
        "name who dissented and why, (5) a concrete NEXT ACTION. Be decisive. End "
        "with your Final report block; put the decision + confidence in SUMMARY.")


def _dispatch(agent: str, prompt: str, cwd: str, agents_dir: str,
              timeout: str, out_dir: str, tag: str) -> dict:
    """Run one agent via a run_subagent subprocess with --out (authoritative
    envelope). Returns the parsed envelope (or an error envelope)."""
    from _manifest import _read_envelope   # reuse the robust file-first reader
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_subagent.py")
    out_file = os.path.join(out_dir, f"{tag}.json")
    cmd = [sys.executable, script, "--agent", agent, "--prompt", prompt,
           "--cwd", cwd, "--out", out_file, "--timeout", timeout]
    if agents_dir:
        cmd += ["--agents-dir", agents_dir]
    try:
        proc = __import__("subprocess").run(cmd, capture_output=True, text=True,
                                            encoding="utf-8", errors="replace",
                                            stdin=__import__("subprocess").DEVNULL)
        return _read_envelope(out_file, proc)
    except OSError as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def run_council(args) -> int:
    """Entry point for ``--council``. Returns the process exit code."""
    if args.question_file:
        try:
            with open(args.question_file, encoding="utf-8") as fh:
                question = fh.read().strip()
        except OSError as e:
            return _fail(f"cannot read --question-file: {e}")
    else:
        question = (args.question or "").strip()
    if not question:
        return _fail("--council needs --question or --question-file")

    members = ([m.strip() for m in args.members.split(",") if m.strip()]
               if args.members else list(DEFAULT_MEMBERS))
    chairman = args.chairman or DEFAULT_CHAIRMAN
    rounds = max(1, min(2, args.rounds or 1))
    if len(members) < 2:
        return _fail("a council needs at least 2 members")

    from _loader import get_agents_dir, load_agent
    cwd = os.path.abspath(args.cwd or os.getcwd())
    agents_dir = args.agents_dir or get_agents_dir(None, cwd)
    # Validate everyone up front so a typo fails before any paid dispatch.
    for who in members + [chairman]:
        try:
            load_agent(agents_dir, who)
        except Exception as e:  # noqa: BLE001
            return _fail(f"council member/chairman {who!r} not found: {e}")

    import tempfile
    out_dir = tempfile.mkdtemp(prefix="summon-council-")
    timeout = args.timeout if isinstance(args.timeout, str) else "600s"
    started = time.monotonic()
    lock = threading.Lock()
    done = {"n": 0}
    sems: dict = {}

    def backend_of(agent: str) -> str:
        from _manifest import _job_backend
        return _job_backend({"agent": agent}, agents_dir)

    member_backend = {m: backend_of(m) for m in members}
    for b in set(member_backend.values()):
        sems[b] = threading.BoundedSemaphore(_PER_BACKEND_CAP)

    def run_member(agent: str, prompt: str, tag: str) -> dict:
        b = member_backend[agent]
        with sems[b]:
            env = _dispatch(agent, prompt, cwd, agents_dir, timeout, out_dir, tag)
        with lock:
            done["n"] += 1
            print(f"[council {done['n']}/{len(members)}] {agent} ({b}) "
                  f"status={env.get('status')}", file=sys.stderr, flush=True)
        return {"agent": agent, "backend": b,
                "model": (env.get("model") or {}).get("resolved"),
                "status": env.get("status"), "position": _position(env),
                "elapsed_ms": env.get("elapsed_ms")}

    # ---- round 1: independent positions -------------------------------------
    p1 = _round1_prompt(question)
    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        results = list(pool.map(lambda m: run_member(m, p1, f"r1-{m}"), members))

    # ---- round 2 (optional): cross-examination ------------------------------
    if rounds >= 2:
        done["n"] = 0
        by_agent = {r["agent"]: r for r in results}
        def refine(agent):
            peers = [r["position"] for r in results if r["agent"] != agent and r.get("position")]
            return run_member(agent, _round2_prompt(question, peers), f"r2-{agent}")
        with ThreadPoolExecutor(max_workers=len(members)) as pool:
            results = list(pool.map(refine, members))

    # ---- synthesis: the chairman calls it -----------------------------------
    print(f"[council] chairman {chairman} synthesizing…", file=sys.stderr, flush=True)
    chair_env = _dispatch(chairman, _chairman_prompt(question, results),
                          cwd, agents_dir, timeout, out_dir, "chairman")

    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)

    envelope = {
        "mode": "council",
        "envelope": 1,
        "question": question,
        "rounds": rounds,
        "members": results,
        "synthesis": {
            "chairman": chairman,
            "backend": backend_of(chairman),
            "model": (chair_env.get("model") or {}).get("resolved"),
            "status": chair_env.get("status"),
            "recommendation": chair_env.get("result") or chair_env.get("error"),
            "report": chair_env.get("report"),
        },
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "status": "success" if chair_env.get("status") == "success" else "partial",
    }
    print(json.dumps(envelope, ensure_ascii=False))
    return 0 if envelope["status"] == "success" else 1
