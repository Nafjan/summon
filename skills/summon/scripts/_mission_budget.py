"""Attempt-scoped shared mission budget & hard pre-launch admission gate.

Implements ``summon.mission-budget/v1``:
- Single atomically replaced, HMAC-sealed ``budget.json`` under ``budget.lock``.
- Keyed by ``(mission_id, job_id, attempt_id)`` with monotonic ``observation_seq``.
- Only ``observation_scope == "attempt_total"`` updates an attempt's cumulative
  ``work_tokens`` (``max(prev_work_tokens, reported_work_tokens)``);
  ``last_step_snapshot`` and ``unestablished`` never overwrite or double-count
  ``attempt_total``.
- Hard pre-launch reservation (``reserve_launch``) under ``budget.lock`` holds a
  per-attempt token/cost reserve while ``in_flight`` so concurrent schedulers
  racing for the final slice cannot both launch.
- Settles in-flight reservations to actuals on completion (``settle_attempt``) and
  marks ``unknown_spend: True`` whenever ``provider_contacted`` is True (or
  indeterminate ``None``) without established token/cost telemetry.
- Note on provider latency: provider usage reporting is asynchronous per turn, so
  in-flight turns can overshoot the cap by at most the active turn's generation
  before termination or control-plane cancellation.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
from pathlib import Path

import _jobs


def _reject_reparse_ancestors(target: str | Path) -> None:
    """Reject symlinks and Windows junctions/reparse points along target and its ancestors."""
    path_obj = Path(target)
    for candidate in (path_obj, *path_obj.parents):
        try:
            attrs = getattr(candidate.stat(follow_symlinks=False), "st_file_attributes", 0)
            if candidate.is_symlink() or bool(attrs & 0x400):
                raise MissionBudgetError(
                    "unsafe_mission_root",
                    f"mission budget path may not contain symlinks or junctions: {candidate}",
                )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MissionBudgetError(
                "unsafe_mission_root",
                f"mission budget path cannot be inspected: {candidate}",
            ) from exc


MISSION_BUDGET_SCHEMA = "summon.mission-budget/v1"

_MISSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_ATTEMPT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")

DEFAULT_ATTEMPT_RESERVE_WORK_TOKENS = 4096
DEFAULT_ATTEMPT_RESERVE_COST_USD = 0.02
MAX_BUDGET_BYTES = 4 * 1024 * 1024


class MissionBudgetError(ValueError):
    """Structured error raised on budget validation or exhaustion."""

    def __init__(self, kind: str, message: str, *, budget_summary: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.budget_summary = budget_summary


class MissionBudgetExhaustedError(MissionBudgetError):
    """Raised when a mission budget ceiling is reached or exceeded."""

    def __init__(self, message: str, *, budget_summary: dict | None = None):
        super().__init__("mission_budget_exhausted", message, budget_summary=budget_summary)


def valid_mission_id(mission_id: object) -> bool:
    return isinstance(mission_id, str) and bool(_MISSION_ID_RE.fullmatch(mission_id))


def validate_mission_id(mission_id: object) -> str:
    if not valid_mission_id(mission_id):
        raise MissionBudgetError(
            "invalid_mission_id",
            f"invalid mission_id {mission_id!r}: expected 1..64 alphanumeric/._- chars",
        )
    return str(mission_id)


def _valid_job_key(job_id: object) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise MissionBudgetError(
            "invalid_job_id",
            f"invalid budget job_id {job_id!r}",
        )
    return job_id


def _valid_attempt_key(attempt_id: object) -> str:
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise MissionBudgetError(
            "invalid_attempt_id",
            f"invalid budget attempt_id {attempt_id!r}",
        )
    return attempt_id


def resolve_mission_budget_dir(mission_id: str, *, job_dir: str | None = None,
                               mission_dir: str | None = None) -> str:
    """Resolve the directory holding ``budget.json`` for ``mission_id``."""
    mid = validate_mission_id(mission_id)
    if mission_dir:
        base = os.path.abspath(mission_dir)
    else:
        root = _jobs.resolve_jobs_dir(job_dir)
        base = os.path.join(root, ".missions", mid)
    _reject_reparse_ancestors(base)
    return base


def _budget_paths(budget_dir: str) -> tuple[str, str, str]:
    budget_file = os.path.join(budget_dir, "budget.json")
    lock_file = os.path.join(budget_dir, "budget.lock")
    secret_file = os.path.join(budget_dir, ".secrets", "budget.secret")
    return budget_file, lock_file, secret_file


def budget_path(mission_id: str, *, job_dir: str | None = None,
                mission_dir: str | None = None) -> str:
    """Return the path to ``budget.json`` for ``mission_id``."""
    bdir = resolve_mission_budget_dir(mission_id, job_dir=job_dir, mission_dir=mission_dir)
    return _budget_paths(bdir)[0]


def _ensure_budget_dir(budget_dir: str) -> None:
    _reject_reparse_ancestors(budget_dir)
    os.makedirs(budget_dir, exist_ok=True)
    secrets_dir = os.path.join(budget_dir, ".secrets")
    _reject_reparse_ancestors(secrets_dir)
    os.makedirs(secrets_dir, exist_ok=True)
    if os.name != "nt":
        for d in (budget_dir, secrets_dir):
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


def _load_or_create_secret(secret_file: str) -> bytes:
    _reject_reparse_ancestors(secret_file)
    try:
        if os.path.lexists(secret_file):
            st = os.lstat(secret_file)
            if not stat.S_ISREG(st.st_mode):
                raise MissionBudgetError("corrupt_mission_budget", "budget.secret is not a regular file")
            raw = Path(secret_file).read_bytes().strip()
            if len(raw) >= 32:
                return raw
            raise MissionBudgetError("corrupt_mission_budget", "budget.secret is truncated")
    except MissionBudgetError:
        raise
    except OSError as exc:
        raise MissionBudgetError("corrupt_mission_budget", f"cannot read budget.secret: {exc}") from exc

    token = secrets.token_hex(32).encode("ascii")
    d = os.path.dirname(secret_file)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".budget-sec-", suffix=".tmp")
    try:
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        _jobs._replace_with_retry(tmp, secret_file)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise MissionBudgetError("corrupt_mission_budget", f"cannot write budget.secret: {exc}") from exc
    return token


def _compute_mac(secret: bytes, payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "mac"}
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hmac.new(secret, b"summon.mission-budget/v1:" + canonical, hashlib.sha256).hexdigest()


def _unlink_with_retry(path: str) -> None:
    for attempt in range(5):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))


@contextmanager
def _budget_lock(lock_file: str, timeout_sec: float = 5.0):
    _reject_reparse_ancestors(lock_file)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + max(0.2, timeout_sec)
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            stale = False
            try:
                with open(lock_file, encoding="ascii") as fh:
                    holder = fh.read().strip()
                holder_pid_str = holder.split(":", 1)[0]
                holder_pid = int(holder_pid_str)
                if _jobs._pid_liveness(holder_pid) == "dead":
                    stale = True
            except (OSError, ValueError):
                try:
                    if time.time() - os.path.getmtime(lock_file) > 15.0:
                        stale = True
                except OSError:
                    pass
            if stale:
                reap_path = f"{lock_file}.reap.{os.getpid()}.{uuid.uuid4().hex[:8]}"
                try:
                    os.replace(lock_file, reap_path)
                    _unlink_with_retry(reap_path)
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise MissionBudgetError("budget_lock_timeout", "timed out waiting for mission budget lock")
            time.sleep(0.015)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        yield
    finally:
        try:
            owned = False
            with open(lock_file, encoding="ascii") as fh:
                owned = fh.read().strip() == token
            if owned:
                _unlink_with_retry(lock_file)
        except OSError:
            pass


def _default_reserve_tokens(max_work_tokens: int | None) -> int:
    raw = os.environ.get("SUMMON_ATTEMPT_RESERVE_WORK_TOKENS")
    if raw is not None and raw.strip():
        try:
            val = int(raw.strip())
            if val >= 0:
                return val
        except ValueError:
            pass
    if max_work_tokens is not None and max_work_tokens > 0:
        return min(DEFAULT_ATTEMPT_RESERVE_WORK_TOKENS, max(1, max_work_tokens // 4))
    return DEFAULT_ATTEMPT_RESERVE_WORK_TOKENS


def _default_reserve_cost_usd(max_cost_usd: float | None) -> float:
    raw = os.environ.get("SUMMON_ATTEMPT_RESERVE_COST_USD")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if math.isfinite(val) and val >= 0.0:
                return val
        except ValueError:
            pass
    if max_cost_usd is not None and max_cost_usd > 0.0:
        return min(DEFAULT_ATTEMPT_RESERVE_COST_USD, max(0.0001, max_cost_usd / 4.0))
    return DEFAULT_ATTEMPT_RESERVE_COST_USD


def _initial_state(
    mission_id: str,
    *,
    max_work_tokens: int | None = None,
    max_cost_usd: float | None = None,
    fail_closed_on_unknown_spend: bool = True,
) -> dict:
    now = time.time()
    return {
        "schema": MISSION_BUDGET_SCHEMA,
        "mission_id": mission_id,
        "purpose": "mission_budget_ledger",
        "generation": 1,
        "created_at": now,
        "updated_at": now,
        "caps": {
            "max_work_tokens": max_work_tokens,
            "max_cost_usd": max_cost_usd,
            "fail_closed_on_unknown_spend": bool(fail_closed_on_unknown_spend),
        },
        "attempts": {},
        "exhausted": False,
        "exhausted_reason": None,
        "unknown_spend": False,
        "unknown_spend_attempts": [],
        "terminated_job_ids": [],
    }


def _read_sealed_state(budget_file: str, secret: bytes, expected_mission_id: str) -> dict | None:
    _reject_reparse_ancestors(budget_file)
    if not os.path.lexists(budget_file):
        return None
    obj, st = _jobs._read(budget_file)
    if st != _jobs._OK or not isinstance(obj, dict):
        raise MissionBudgetError("corrupt_mission_budget", f"mission budget file is unreadable ({st})")
    if obj.get("schema") != MISSION_BUDGET_SCHEMA or obj.get("mission_id") != expected_mission_id:
        raise MissionBudgetError("corrupt_mission_budget", "mission budget schema or mission_id mismatch")
    if obj.get("purpose") != "mission_budget_ledger":
        raise MissionBudgetError("corrupt_mission_budget", "mission budget purpose mismatch")
    gen = obj.get("generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        raise MissionBudgetError("corrupt_mission_budget", "mission budget generation is invalid")
    mac = obj.get("mac")
    if not isinstance(mac, str) or len(mac) != 64:
        raise MissionBudgetError("corrupt_mission_budget", "mission budget MAC is missing")
    expected_mac = _compute_mac(secret, obj)
    if not hmac.compare_digest(mac, expected_mac):
        raise MissionBudgetError("corrupt_mission_budget", "mission budget HMAC verification failed")
    if not isinstance(obj.get("attempts"), dict) or not isinstance(obj.get("caps"), dict):
        raise MissionBudgetError("corrupt_mission_budget", "mission budget structure is invalid")
    return obj


def _write_sealed_state(budget_file: str, secret: bytes, state: dict) -> dict:
    _reject_reparse_ancestors(budget_file)
    payload = {k: v for k, v in state.items() if k != "mac"}
    payload["updated_at"] = time.time()
    payload["mac"] = _compute_mac(secret, payload)
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(serialized) > MAX_BUDGET_BYTES:
        raise MissionBudgetError("mission_budget_overflow", "mission budget ledger exceeds size limit")
    _jobs._atomic_write_json(budget_file, payload)
    return payload


def _validate_caps(max_work_tokens: int | None, max_cost_usd: float | None) -> tuple[int | None, float | None]:
    norm_tokens: int | None = None
    if max_work_tokens is not None:
        if isinstance(max_work_tokens, bool) or not isinstance(max_work_tokens, int) or max_work_tokens <= 0:
            raise MissionBudgetError(
                "invalid_budget_cap",
                f"--mission-max-work-tokens must be a positive integer, got {max_work_tokens!r}",
            )
        norm_tokens = int(max_work_tokens)
    norm_cost: float | None = None
    if max_cost_usd is not None:
        if isinstance(max_cost_usd, bool) or not isinstance(max_cost_usd, (int, float)):
            raise MissionBudgetError(
                "invalid_budget_cap",
                f"--mission-max-cost-usd must be a positive finite number, got {max_cost_usd!r}",
            )
        f_cost = float(max_cost_usd)
        if not math.isfinite(f_cost) or f_cost <= 0.0:
            raise MissionBudgetError(
                "invalid_budget_cap",
                f"--mission-max-cost-usd must be a positive finite number, got {max_cost_usd!r}",
            )
        norm_cost = round(f_cost, 8)
    return norm_tokens, norm_cost


def _recompute_summary(state: dict) -> dict:
    """Derive totals, in-flight reservations, unknown spend, and top_spender."""
    caps = state.get("caps") if isinstance(state.get("caps"), dict) else {}
    max_tokens = caps.get("max_work_tokens")
    max_cost = caps.get("max_cost_usd")
    fail_closed_unknown = bool(caps.get("fail_closed_on_unknown_spend", True))

    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    total_known_tokens = 0
    total_reserved_tokens = 0
    total_known_cost = 0.0
    total_reserved_cost = 0.0
    in_flight_count = 0
    settled_count = 0
    unknown_attempts: list[str] = []

    by_job: dict[str, dict] = {}

    for key, rec in sorted(attempts.items()):
        if not isinstance(rec, dict):
            continue
        jid = str(rec.get("job_id") or "")
        aid = str(rec.get("attempt_id") or key)
        agent = rec.get("agent") if isinstance(rec.get("agent"), str) else None
        status = str(rec.get("status") or "in_flight")
        wt = rec.get("work_tokens")
        known_wt = wt if (isinstance(wt, int) and not isinstance(wt, bool) and wt >= 0) else 0
        res_wt = rec.get("reserved_work_tokens")
        reserved_wt = res_wt if (isinstance(res_wt, int) and not isinstance(res_wt, bool) and res_wt >= 0) else 0

        cost = rec.get("cost_usd")
        known_cost = float(cost) if (isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(float(cost)) and float(cost) >= 0.0) else 0.0
        res_cost = rec.get("reserved_cost_usd")
        reserved_cost = float(res_cost) if (isinstance(res_cost, (int, float)) and not isinstance(res_cost, bool) and math.isfinite(float(res_cost)) and float(res_cost) >= 0.0) else 0.0

        total_known_tokens += known_wt
        total_known_cost += known_cost

        if status == "in_flight":
            owner_pid = rec.get("owner_pid")
            owner_dead = False
            if isinstance(owner_pid, int) and not isinstance(owner_pid, bool) and owner_pid > 0:
                if _jobs._pid_liveness(owner_pid) == "dead":
                    owner_dead = True
                else:
                    expected_birth = rec.get("owner_birth")
                    if isinstance(expected_birth, str) and expected_birth:
                        try:
                            from _resource_governor import process_birth_token
                            actual_birth = process_birth_token(owner_pid)
                            if actual_birth is not None and actual_birth != expected_birth:
                                owner_dead = True
                        except Exception:
                            pass
            if owner_dead:
                rec["status"] = "abandoned_owner_dead"
                rec["reserved_work_tokens"] = 0
                rec["reserved_cost_usd"] = 0.0
                if rec.get("provider_contacted") is True and rec.get("observation_scope") != "attempt_total":
                    rec["unknown_spend"] = True
                status = "abandoned_owner_dead"
                settled_count += 1
            else:
                in_flight_count += 1
                # Effective in-flight hold is max(reserved, already-observed attempt_total)
                # so committed + remaining reserve never double-counts or under-counts.
                if reserved_wt > known_wt:
                    total_reserved_tokens += (reserved_wt - known_wt)
                if reserved_cost > known_cost:
                    total_reserved_cost += (reserved_cost - known_cost)
        else:
            settled_count += 1

        if rec.get("unknown_spend") is True:
            unknown_attempts.append(aid)

        if jid:
            entry = by_job.setdefault(jid, {
                "job_id": jid,
                "agent": agent,
                "work_tokens": 0,
                "cost_usd": 0.0,
                "attempts": 0,
                "unknown_spend": False,
            })
            if agent and not entry.get("agent"):
                entry["agent"] = agent
            entry["work_tokens"] += known_wt
            entry["cost_usd"] = round(entry["cost_usd"] + known_cost, 8)
            entry["attempts"] += 1
            if rec.get("unknown_spend") is True:
                entry["unknown_spend"] = True

    committed_tokens = total_known_tokens + total_reserved_tokens
    committed_cost = round(total_known_cost + total_reserved_cost, 8)
    total_known_cost = round(total_known_cost, 8)
    total_reserved_cost = round(total_reserved_cost, 8)

    unknown_spend = bool(unknown_attempts or state.get("unknown_spend"))
    exhausted = False
    exhausted_reason: str | None = None

    if state.get("forced_exhausted"):
        exhausted = True
        exhausted_reason = str(state.get("forced_exhausted_reason") or "mission cancelled")
    elif isinstance(max_tokens, int) and max_tokens > 0 and total_known_tokens >= max_tokens:
        exhausted = True
        exhausted_reason = f"work_tokens reached cap ({total_known_tokens} >= {max_tokens})"
    elif isinstance(max_cost, (int, float)) and max_cost > 0.0 and total_known_cost >= float(max_cost):
        exhausted = True
        exhausted_reason = f"cost_usd reached cap (${total_known_cost:.4f} >= ${float(max_cost):.4f})"
    elif unknown_spend and fail_closed_unknown and (max_tokens is not None or max_cost is not None):
        exhausted = True
        exhausted_reason = "unknown_spend detected under active mission budget cap"

    top_spender: dict | None = None
    if by_job:
        ranked = sorted(
            by_job.values(),
            key=lambda item: (item["work_tokens"], item["cost_usd"], item["job_id"]),
            reverse=True,
        )
        leader = ranked[0]
        if leader["work_tokens"] > 0 or leader["cost_usd"] > 0.0:
            share_pct = (
                round((leader["work_tokens"] / total_known_tokens) * 100.0, 2)
                if total_known_tokens > 0
                else None
            )
            top_spender = {
                "job_id": leader["job_id"],
                "agent": leader["agent"],
                "work_tokens": leader["work_tokens"],
                "cost_usd": leader["cost_usd"],
                "attempts": leader["attempts"],
                "share_of_known_work_tokens_pct": share_pct,
            }

    remaining_tokens = (
        max(0, int(max_tokens) - committed_tokens)
        if isinstance(max_tokens, int)
        else None
    )
    remaining_cost = (
        round(max(0.0, float(max_cost) - committed_cost), 8)
        if isinstance(max_cost, (int, float))
        else None
    )

    state["unknown_spend"] = unknown_spend
    state["unknown_spend_attempts"] = unknown_attempts
    state["exhausted"] = exhausted
    state["exhausted_reason"] = exhausted_reason

    return {
        "schema": MISSION_BUDGET_SCHEMA,
        "mission_id": state["mission_id"],
        "generation": state["generation"],
        "caps": {
            "max_work_tokens": max_tokens,
            "max_cost_usd": max_cost,
            "fail_closed_on_unknown_spend": fail_closed_unknown,
        },
        "total_known_work_tokens": total_known_tokens,
        "reserved_in_flight_work_tokens": total_reserved_tokens,
        "committed_work_tokens": committed_tokens,
        "remaining_work_tokens": remaining_tokens,
        "total_known_cost_usd": total_known_cost,
        "reserved_in_flight_cost_usd": total_reserved_cost,
        "committed_cost_usd": committed_cost,
        "remaining_cost_usd": remaining_cost,
        "attempt_count": len(attempts),
        "in_flight_attempts": in_flight_count,
        "settled_attempts": settled_count,
        "unknown_spend": unknown_spend,
        "unknown_spend_attempts": list(unknown_attempts),
        "exhausted": exhausted,
        "exhausted_reason": exhausted_reason,
        "top_spender": top_spender,
        "terminated_job_ids": list(state.get("terminated_job_ids") or []),
        "overshoot_note": (
            "Provider usage telemetry arrives per turn; an active turn may "
            "overshoot the ceiling by at most one turn before termination."
        ),
    }


def configure_mission_budget(
    mission_id: str,
    *,
    max_work_tokens: int | None = None,
    max_cost_usd: float | None = None,
    fail_closed_on_unknown_spend: bool = True,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict:
    """Create or tighten the HMAC-sealed ``budget.json`` for ``mission_id``."""
    mid = validate_mission_id(mission_id)
    norm_tokens, norm_cost = _validate_caps(max_work_tokens, max_cost_usd)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    _ensure_budget_dir(bdir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)
    with _budget_lock(lock_file):
        secret = _load_or_create_secret(secret_file)
        state = _read_sealed_state(budget_file, secret, mid)
        if state is None:
            state = _initial_state(
                mid,
                max_work_tokens=norm_tokens,
                max_cost_usd=norm_cost,
                fail_closed_on_unknown_spend=fail_closed_on_unknown_spend,
            )
        else:
            caps = state.setdefault("caps", {})
            if norm_tokens is not None:
                existing_tok = caps.get("max_work_tokens")
                caps["max_work_tokens"] = (
                    min(int(existing_tok), norm_tokens)
                    if isinstance(existing_tok, int) and existing_tok > 0
                    else norm_tokens
                )
            if norm_cost is not None:
                existing_cost = caps.get("max_cost_usd")
                caps["max_cost_usd"] = (
                    min(float(existing_cost), norm_cost)
                    if isinstance(existing_cost, (int, float)) and existing_cost > 0.0
                    else norm_cost
                )
            if fail_closed_on_unknown_spend:
                caps["fail_closed_on_unknown_spend"] = True
            state["generation"] = int(state.get("generation", 1)) + 1
        summary = _recompute_summary(state)
        _write_sealed_state(budget_file, secret, state)
        return summary


ensure_mission_budget = configure_mission_budget


def reserve_launch(
    mission_id: str,
    *,
    job_id: str,
    attempt_id: str,
    agent: str | None = None,
    attempt_kind: str = "initial",
    max_work_tokens: int | None = None,
    max_cost_usd: float | None = None,
    reserve_work_tokens: int | None = None,
    reserve_cost_usd: float | None = None,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict:
    """Enforce the hard pre-launch mission budget gate under ``budget.lock``.

    Reserves a per-attempt token/cost slice while ``in_flight`` so concurrent
    workers cannot race past the final remaining allowance. Raises
    ``MissionBudgetExhaustedError`` when the cap is already reached or the
    remaining unreserved allowance is insufficient.
    """
    mid = validate_mission_id(mission_id)
    jid = _valid_job_key(job_id)
    aid = _valid_attempt_key(attempt_id)
    norm_tokens, norm_cost = _validate_caps(max_work_tokens, max_cost_usd)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    _ensure_budget_dir(bdir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)

    with _budget_lock(lock_file):
        secret = _load_or_create_secret(secret_file)
        state = _read_sealed_state(budget_file, secret, mid)
        if state is None:
            state = _initial_state(
                mid,
                max_work_tokens=norm_tokens,
                max_cost_usd=norm_cost,
            )
        else:
            caps = state.setdefault("caps", {})
            if norm_tokens is not None:
                existing_tok = caps.get("max_work_tokens")
                caps["max_work_tokens"] = (
                    min(int(existing_tok), norm_tokens)
                    if isinstance(existing_tok, int) and existing_tok > 0
                    else norm_tokens
                )
            if norm_cost is not None:
                existing_cost = caps.get("max_cost_usd")
                caps["max_cost_usd"] = (
                    min(float(existing_cost), norm_cost)
                    if isinstance(existing_cost, (int, float)) and existing_cost > 0.0
                    else norm_cost
                )

        attempts = state.setdefault("attempts", {})
        if aid in attempts:
            # Idempotent re-read of an already-reserved attempt_id for the same job
            existing_rec = attempts[aid]
            if existing_rec.get("job_id") != jid:
                raise MissionBudgetError(
                    "attempt_id_collision",
                    f"attempt_id {aid!r} is already bound to job {existing_rec.get('job_id')!r}",
                )
            summary = _recompute_summary(state)
            if summary["exhausted"]:
                _write_sealed_state(budget_file, secret, state)
                raise MissionBudgetExhaustedError(
                    f"mission {mid!r} budget exhausted before launch: {summary['exhausted_reason']}",
                    budget_summary=summary,
                )
            return summary

        summary = _recompute_summary(state)
        caps = state["caps"]
        cap_tokens = caps.get("max_work_tokens")
        cap_cost = caps.get("max_cost_usd")

        if summary["exhausted"]:
            _write_sealed_state(budget_file, secret, state)
            raise MissionBudgetExhaustedError(
                f"mission {mid!r} budget exhausted before launch: {summary['exhausted_reason']}",
                budget_summary=summary,
            )

        eff_res_tokens = (
            int(reserve_work_tokens)
            if isinstance(reserve_work_tokens, int) and reserve_work_tokens >= 0
            else _default_reserve_tokens(cap_tokens if isinstance(cap_tokens, int) else None)
        )
        eff_res_cost = (
            float(reserve_cost_usd)
            if isinstance(reserve_cost_usd, (int, float)) and reserve_cost_usd >= 0.0
            else _default_reserve_cost_usd(float(cap_cost) if isinstance(cap_cost, (int, float)) else None)
        )

        if isinstance(cap_tokens, int) and cap_tokens > 0:
            if summary["committed_work_tokens"] + max(1, eff_res_tokens) > cap_tokens:
                state["exhausted"] = True
                state["exhausted_reason"] = (
                    f"insufficient remaining work_tokens for launch reservation "
                    f"(committed={summary['committed_work_tokens']}, "
                    f"reserve={max(1, eff_res_tokens)}, cap={cap_tokens})"
                )
                summary["exhausted"] = True
                summary["exhausted_reason"] = state["exhausted_reason"]
                _write_sealed_state(budget_file, secret, state)
                raise MissionBudgetExhaustedError(
                    f"mission {mid!r} budget cannot admit job {jid!r}: {state['exhausted_reason']}",
                    budget_summary=summary,
                )

        if isinstance(cap_cost, (int, float)) and float(cap_cost) > 0.0:
            if summary["committed_cost_usd"] + max(0.0001, eff_res_cost) > float(cap_cost) + 1e-9:
                state["exhausted"] = True
                state["exhausted_reason"] = (
                    f"insufficient remaining cost_usd for launch reservation "
                    f"(committed=${summary['committed_cost_usd']:.4f}, "
                    f"reserve=${max(0.0001, eff_res_cost):.4f}, cap=${float(cap_cost):.4f})"
                )
                summary["exhausted"] = True
                summary["exhausted_reason"] = state["exhausted_reason"]
                _write_sealed_state(budget_file, secret, state)
                raise MissionBudgetExhaustedError(
                    f"mission {mid!r} budget cannot admit job {jid!r}: {state['exhausted_reason']}",
                    budget_summary=summary,
                )

        try:
            from _resource_governor import process_birth_token
            my_birth = process_birth_token(os.getpid())
        except Exception:
            my_birth = None

        attempts[aid] = {
            "job_id": jid,
            "attempt_id": aid,
            "agent": agent if isinstance(agent, str) and agent else None,
            "attempt_kind": str(attempt_kind or "initial"),
            "status": "in_flight",
            "owner_pid": os.getpid(),
            "owner_birth": my_birth,
            "observation_seq": 0,
            "observation_scope": "unestablished",
            "work_tokens": 0,
            "cost_usd": 0.0,
            "reserved_work_tokens": eff_res_tokens if isinstance(cap_tokens, int) else 0,
            "reserved_cost_usd": round(eff_res_cost, 8) if isinstance(cap_cost, (int, float)) else 0.0,
            "provider_contacted": None,
            "unknown_spend": False,
            "started_at": time.time(),
            "updated_at": time.time(),
        }
        state["generation"] = int(state.get("generation", 1)) + 1
        updated_summary = _recompute_summary(state)
        _write_sealed_state(budget_file, secret, state)
        return updated_summary


def _apply_usage_observation(rec: dict, *, usage_metrics: dict | None,
                             cost_usd: float | None,
                             observation_seq: int | None = None) -> bool:
    """Update ``rec`` from ``usage_metrics`` only when scope is ``attempt_total``.

    Returns True if ``attempt_total`` token telemetry was established.
    """
    current_seq = int(rec.get("observation_seq") or 0)
    if observation_seq is not None:
        if not isinstance(observation_seq, int) or isinstance(observation_seq, bool):
            return rec.get("observation_scope") == "attempt_total"
        if observation_seq < current_seq:
            # Stale out-of-order observation; ignore
            return rec.get("observation_scope") == "attempt_total"
        rec["observation_seq"] = observation_seq
    else:
        rec["observation_seq"] = current_seq + 1

    established = rec.get("observation_scope") == "attempt_total"
    if isinstance(usage_metrics, dict):
        scope = usage_metrics.get("observation_scope")
        wt = usage_metrics.get("work_tokens")
        # STRICT INVARIANT (Astra P1 #7): Only observation_scope == "attempt_total"
        # updates the attempt's cumulative work_tokens. last_step_snapshot and
        # unestablished never overwrite or add to attempt_total.
        if (scope == "attempt_total"
                and isinstance(wt, int)
                and not isinstance(wt, bool)
                and wt >= 0):
            prev_wt = rec.get("work_tokens")
            prev_val = prev_wt if (isinstance(prev_wt, int) and not isinstance(prev_wt, bool) and prev_wt >= 0) else 0
            rec["work_tokens"] = max(prev_val, wt)
            rec["observation_scope"] = "attempt_total"
            established = True
        elif not established and isinstance(scope, str):
            rec["last_non_total_scope"] = scope

    if (isinstance(cost_usd, (int, float))
            and not isinstance(cost_usd, bool)
            and math.isfinite(float(cost_usd))
            and float(cost_usd) >= 0.0):
        prev_cost = rec.get("cost_usd")
        prev_c = float(prev_cost) if (isinstance(prev_cost, (int, float)) and not isinstance(prev_cost, bool) and float(prev_cost) >= 0.0) else 0.0
        rec["cost_usd"] = round(max(prev_c, float(cost_usd)), 8)
        rec["cost_established"] = True

    rec["updated_at"] = time.time()
    return established


def record_heartbeat_usage(
    mission_id: str,
    *,
    job_id: str,
    attempt_id: str,
    usage_metrics: dict | None = None,
    cost_usd: float | None = None,
    observation_seq: int | None = None,
    agent: str | None = None,
    attempt_kind: str | None = None,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict:
    """Record an in-flight heartbeat usage observation for ``(mission_id, job_id, attempt_id)``."""
    mid = validate_mission_id(mission_id)
    jid = _valid_job_key(job_id)
    aid = _valid_attempt_key(attempt_id)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    _ensure_budget_dir(bdir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)

    with _budget_lock(lock_file):
        secret = _load_or_create_secret(secret_file)
        state = _read_sealed_state(budget_file, secret, mid)
        if state is None:
            state = _initial_state(mid)
        attempts = state.setdefault("attempts", {})
        rec = attempts.get(aid)
        if not isinstance(rec, dict):
            rec = {
                "job_id": jid,
                "attempt_id": aid,
                "agent": agent if isinstance(agent, str) and agent else None,
                "attempt_kind": str(attempt_kind or "initial"),
                "status": "in_flight",
                "observation_seq": 0,
                "observation_scope": "unestablished",
                "work_tokens": 0,
                "cost_usd": 0.0,
                "reserved_work_tokens": 0,
                "reserved_cost_usd": 0.0,
                "provider_contacted": True,
                "unknown_spend": False,
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            attempts[aid] = rec
        elif rec.get("job_id") != jid:
            raise MissionBudgetError(
                "attempt_id_collision",
                f"attempt_id {aid!r} belongs to job {rec.get('job_id')!r}, not {jid!r}",
            )
        if agent and not rec.get("agent"):
            rec["agent"] = agent
        _apply_usage_observation(
            rec,
            usage_metrics=usage_metrics,
            cost_usd=cost_usd,
            observation_seq=observation_seq,
        )
        state["generation"] = int(state.get("generation", 1)) + 1
        summary = _recompute_summary(state)
        _write_sealed_state(budget_file, secret, state)

    if summary["exhausted"]:
        _enforce_budget_exhaustion_on_active_jobs(
            mid, job_dir=job_dir, mission_dir=mission_dir
        )
    return summary


def settle_attempt(
    mission_id: str,
    *,
    job_id: str,
    attempt_id: str,
    usage_metrics: dict | None = None,
    cost_usd: float | None = None,
    provider_contacted: bool | None = None,
    terminal_status: str = "settled",
    observation_seq: int | None = None,
    agent: str | None = None,
    attempt_kind: str = "initial",
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict:
    """Settle a completed/aborted attempt, releasing unused in-flight reserves."""
    mid = validate_mission_id(mission_id)
    jid = _valid_job_key(job_id)
    aid = _valid_attempt_key(attempt_id)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    _ensure_budget_dir(bdir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)

    with _budget_lock(lock_file):
        secret = _load_or_create_secret(secret_file)
        state = _read_sealed_state(budget_file, secret, mid)
        if state is None:
            state = _initial_state(mid)
        attempts = state.setdefault("attempts", {})
        rec = attempts.get(aid)
        if not isinstance(rec, dict):
            rec = {
                "job_id": jid,
                "attempt_id": aid,
                "agent": agent if isinstance(agent, str) and agent else None,
                "attempt_kind": str(attempt_kind or "initial"),
                "status": "in_flight",
                "observation_seq": 0,
                "observation_scope": "unestablished",
                "work_tokens": 0,
                "cost_usd": 0.0,
                "reserved_work_tokens": 0,
                "reserved_cost_usd": 0.0,
                "provider_contacted": provider_contacted,
                "unknown_spend": False,
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            attempts[aid] = rec
        elif rec.get("job_id") != jid:
            raise MissionBudgetError(
                "attempt_id_collision",
                f"attempt_id {aid!r} belongs to job {rec.get('job_id')!r}, not {jid!r}",
            )

        if agent and not rec.get("agent"):
            rec["agent"] = agent
        established = _apply_usage_observation(
            rec,
            usage_metrics=usage_metrics,
            cost_usd=cost_usd,
            observation_seq=observation_seq,
        )
        rec["status"] = str(terminal_status or "settled")
        rec["reserved_work_tokens"] = 0
        rec["reserved_cost_usd"] = 0.0
        rec["provider_contacted"] = provider_contacted
        rec["settled_at"] = time.time()

        caps = state.get("caps") if isinstance(state.get("caps"), dict) else {}
        cost_cap_active = isinstance(caps.get("max_cost_usd"), (int, float)) and float(caps["max_cost_usd"]) > 0.0
        cost_established = bool(rec.get("cost_established"))

        # Any attempt where provider was contacted (True) or indeterminate (None)
        # without established attempt_total token telemetry (or without cost when
        # a cost cap is active) marks unknown_spend=True.
        if provider_contacted is False:
            rec["unknown_spend"] = False
        else:
            missing_tokens = not established
            missing_cost = cost_cap_active and not cost_established
            rec["unknown_spend"] = bool(missing_tokens or missing_cost)

        state["generation"] = int(state.get("generation", 1)) + 1
        summary = _recompute_summary(state)
        _write_sealed_state(budget_file, secret, state)

    if summary["exhausted"]:
        _enforce_budget_exhaustion_on_active_jobs(
            mid, job_dir=job_dir, mission_dir=mission_dir
        )
    return summary


def settle_envelope(
    mission_id: str,
    *,
    job_id: str,
    envelope: dict | None,
    fallback_attempt_id: str | None = None,
    agent: str | None = None,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict:
    """Settle a dispatch envelope (and any distinct inner submission attempts) into ``budget.json``."""
    mid = validate_mission_id(mission_id)
    jid = _valid_job_key(job_id)
    env = envelope if isinstance(envelope, dict) else {}
    resolved_agent = (
        (env.get("agent") if isinstance(env.get("agent"), str) else None)
        or agent
    )
    history = env.get("attempt_history")
    if isinstance(history, list) and history:
        last_summary: dict | None = None
        settled_ids: set[str] = set()
        for idx, item in enumerate(history):
            if not isinstance(item, dict):
                continue
            item_aid = item.get("attempt_id")
            if not isinstance(item_aid, str) or not _ATTEMPT_ID_RE.fullmatch(item_aid):
                item_aid = (fallback_attempt_id or jid) if idx == 0 else f"{jid}-attempt-{idx + 1}"
            settled_ids.add(item_aid)
            item_um = item.get("usage_metrics") if isinstance(item.get("usage_metrics"), dict) else None
            if item_um is None and isinstance(item.get("runtime_control"), dict):
                rc_um = item["runtime_control"].get("usage_metrics")
                if isinstance(rc_um, dict):
                    item_um = rc_um
            if item_um is None and len(history) == 1 and isinstance(env.get("usage_metrics"), dict):
                item_um = env["usage_metrics"]
            raw_cost = item.get("cost_usd")
            item_cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and math.isfinite(float(raw_cost)) and float(raw_cost) >= 0.0
                else None
            )
            item_pc = item.get("provider_contacted")
            if not isinstance(item_pc, bool):
                if item.get("execution_status") == "not_run":
                    item_pc = False
                else:
                    item_pc = None
            last_summary = settle_attempt(
                mid,
                job_id=jid,
                attempt_id=item_aid,
                usage_metrics=item_um,
                cost_usd=item_cost,
                provider_contacted=item_pc,
                terminal_status=str(item.get("status") or env.get("status") or "settled"),
                agent=resolved_agent,
                attempt_kind=str(item.get("attempt_kind") or ("initial" if idx == 0 else "retry")),
                job_dir=job_dir,
                mission_dir=mission_dir,
            )
        if fallback_attempt_id and fallback_attempt_id not in settled_ids and _ATTEMPT_ID_RE.fullmatch(fallback_attempt_id):
            last_summary = settle_attempt(
                mid,
                job_id=jid,
                attempt_id=fallback_attempt_id,
                usage_metrics=None,
                cost_usd=0.0,
                provider_contacted=False,
                terminal_status="superseded_reservation",
                agent=resolved_agent,
                job_dir=job_dir,
                mission_dir=mission_dir,
            )
        if last_summary is not None:
            return last_summary

    aid = (
        env.get("attempt_id")
        if isinstance(env.get("attempt_id"), str) and _ATTEMPT_ID_RE.fullmatch(env["attempt_id"])
        else (fallback_attempt_id or jid)
    )
    um = env.get("usage_metrics") if isinstance(env.get("usage_metrics"), dict) else None
    if um is None and isinstance(env.get("runtime_control"), dict):
        rc_um = env["runtime_control"].get("usage_metrics")
        if isinstance(rc_um, dict):
            um = rc_um
    raw_cost = env.get("cost_usd")
    cost_usd = (
        float(raw_cost)
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and math.isfinite(float(raw_cost)) and float(raw_cost) >= 0.0
        else None
    )
    provider_contacted = env.get("provider_contacted")
    if not isinstance(provider_contacted, bool):
        # Blocked/not_run before provider contact
        if env.get("execution_status") == "not_run" or env.get("attempt_status") == "not_run":
            provider_contacted = False
        else:
            provider_contacted = None
    status_str = str(env.get("status") or "settled")
    summary = settle_attempt(
        mid,
        job_id=jid,
        attempt_id=aid,
        usage_metrics=um,
        cost_usd=cost_usd,
        provider_contacted=provider_contacted,
        terminal_status=status_str,
        agent=resolved_agent,
        job_dir=job_dir,
        mission_dir=mission_dir,
    )
    if fallback_attempt_id and fallback_attempt_id != aid and _ATTEMPT_ID_RE.fullmatch(fallback_attempt_id):
        summary = settle_attempt(
            mid,
            job_id=jid,
            attempt_id=fallback_attempt_id,
            usage_metrics=None,
            cost_usd=0.0,
            provider_contacted=False,
            terminal_status="superseded_reservation",
            agent=resolved_agent,
            job_dir=job_dir,
            mission_dir=mission_dir,
        )
    return summary


def read_mission_budget(
    mission_id: str,
    *,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> dict | None:
    """Read and verify the HMAC-sealed mission budget summary, or return None if absent."""
    mid = validate_mission_id(mission_id)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)
    if not os.path.lexists(budget_file):
        return None
    with _budget_lock(lock_file):
        secret = _load_or_create_secret(secret_file)
        state = _read_sealed_state(budget_file, secret, mid)
        if state is None:
            return None
        return _recompute_summary(state)


def _enforce_budget_exhaustion_on_active_jobs(
    mission_id: str,
    *,
    reason: str | None = None,
    mark_exhausted: bool = False,
    terminate_in_flight_owners: bool = False,
    job_dir: str | None = None,
    mission_dir: str | None = None,
) -> list[str]:
    """Queue authenticated ``cancel`` commands for active jobs in ``mission_id``."""
    mid = validate_mission_id(mission_id)
    root = _jobs.resolve_jobs_dir(job_dir)
    bdir = resolve_mission_budget_dir(mid, job_dir=job_dir, mission_dir=mission_dir)
    budget_file, lock_file, secret_file = _budget_paths(bdir)

    cancelled: list[str] = []
    if mark_exhausted or terminate_in_flight_owners or os.path.lexists(budget_file):
        try:
            _ensure_budget_dir(bdir)
            with _budget_lock(lock_file):
                secret = _load_or_create_secret(secret_file)
                state = _read_sealed_state(budget_file, secret, mid)
                if state is None and mark_exhausted:
                    state = _initial_state(mid)
                if isinstance(state, dict):
                    dirty = False
                    if mark_exhausted and not state.get("forced_exhausted"):
                        state["forced_exhausted"] = True
                        state["forced_exhausted_reason"] = (reason or "mission cancelled").strip()
                        state["exhausted"] = True
                        state["exhausted_reason"] = state["forced_exhausted_reason"]
                        dirty = True
                    if terminate_in_flight_owners:
                        import signal
                        attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
                        for rec in attempts.values():
                            if not isinstance(rec, dict) or rec.get("status") != "in_flight":
                                continue
                            pid = rec.get("owner_pid")
                            jid_rec = str(rec.get("job_id") or "")
                            if (isinstance(pid, int) and not isinstance(pid, bool)
                                    and pid > 0 and pid != os.getpid()
                                    and _jobs._pid_liveness(pid) == "alive"):
                                expected_birth = rec.get("owner_birth")
                                birth_ok = False
                                if isinstance(expected_birth, str) and expected_birth:
                                    try:
                                        from _resource_governor import process_birth_token
                                        actual_birth = process_birth_token(pid)
                                        if actual_birth is not None and actual_birth == expected_birth:
                                            birth_ok = True
                                    except Exception:
                                        birth_ok = False
                                if birth_ok:
                                    try:
                                        os.kill(pid, signal.SIGTERM)
                                    except OSError:
                                        pass
                                    if jid_rec and jid_rec not in cancelled:
                                        cancelled.append(jid_rec)
                            rec["status"] = "cancelled"
                            rec["reserved_work_tokens"] = 0
                            rec["reserved_cost_usd"] = 0.0
                            dirty = True
                    if dirty:
                        state["generation"] = int(state.get("generation", 1)) + 1
                        _write_sealed_state(budget_file, secret, state)
        except Exception:
            pass

    try:
        rows = _jobs.list_jobs(root)
    except OSError:
        rows = []

    from _job_control import queue_command

    for row in rows:
        if row.get("state") not in ("running", "prepared"):
            continue
        jid = row.get("job_id")
        if not isinstance(jid, str) or not _jobs.valid_job_id(jid):
            continue
        try:
            rec, st = _jobs._read(_jobs.record_path(root, jid))
        except (OSError, ValueError):
            continue
        if st != _jobs._OK or not isinstance(rec, dict):
            continue
        flags = rec.get("flags") if isinstance(rec.get("flags"), dict) else {}
        rec_mid = rec.get("mission_id") or flags.get("mission_id")
        if rec_mid != mid:
            continue
        try:
            queue_command(root, jid, "cancel")
            if jid not in cancelled:
                cancelled.append(jid)
        except Exception:
            pass

    if cancelled and os.path.lexists(budget_file):
        try:
            with _budget_lock(lock_file):
                secret = _load_or_create_secret(secret_file)
                state = _read_sealed_state(budget_file, secret, mid)
                if isinstance(state, dict):
                    existing = list(state.get("terminated_job_ids") or [])
                    for jid in cancelled:
                        if jid not in existing:
                            existing.append(jid)
                    state["terminated_job_ids"] = existing
                    state["generation"] = int(state.get("generation", 1)) + 1
                    _write_sealed_state(budget_file, secret, state)
        except Exception:
            pass
    return cancelled
